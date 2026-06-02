"""V9.5 parquet dataset: 顺序窗口采样 + 多任务标签输出。

设计协议（与 tools/pack_to_parquet.py 写出的 schema 严格对齐）：
  - 数据集根目录是 hive 分区：workload=W*/part-000.parquet
  - 每个分区内行已按 (core_id, thread_id, micro_seq) 升序排序，
    pos_in_thread 是 thread 内 0-based 行号。
  - 训练样本 = 锚点 + 历史窗口 N=128。锚点 t 在 thread 起点附近时，
    左侧用 pad 填充（mask=0）。
  - 内存策略：__init__ 时 mmap parquet 文件，预读所有列到 numpy
    （3M × 50 列约 600 MB，可常驻），DataLoader worker 通过 fork
    共享只读页，零拷贝。比 row-group 反复 read 更快、CPU 占用更低。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

# 只在需要时 import torch / pyarrow。
import pyarrow.parquet as pq


# ---------------------------- 字段分组（必须与 pack_to_parquet.py 一致）
SCALAR_BOOL = (
    'is_load', 'is_store', 'is_atomic', 'is_branch', 'is_branch_cond',
    'is_branch_indirect', 'is_call', 'is_return', 'is_int', 'is_fp',
    'is_simd', 'is_serialize', 'is_microop', 'is_last_microop',
)
SCALAR_SMALL_INT = (
    'n_src', 'n_dst', 'size',
    'mesi_before', 'coh_oracle', 'sharer_bucket', 'owner_dist',
    'dirty_owner', 'path_class', 'inval_fanout', 'same_line_recent',
    'oracle_source',
    'i_path_class', 'i_coh_oracle', 'i_mesi_before', 'i_oracle_source',
    # P0-A：d-side / i-side 各 5 字段（来自 simulator.hpp / tao_trace.cc）
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    'i_mshr_depth', 'itlb_hit', 'i_walker_levels',
    'i_walker_dram_misses', 'i_bank_id',
    # V10.3 A：LLC set residency / lru_pos（d/i 各 2 列，bit-exact）
    'd_llc_set_residency', 'd_llc_set_lru_pos',
    'i_llc_set_residency', 'i_llc_set_lru_pos',
)
# P1-C：上下文窗口派生 7 列（packer 在 sort 之后写入，严格因果）
SCALAR_P1C = (
    'mem_density_W64', 'branch_density_W64', 'unique_cl_W64',
    'cl_reuse_dist_log', 'pc_freq_W64', 'time_since_last_branch_log',
    'bank_conflict_W64',
)
# V10.3 B：长窗口 unique_cl（W256 / W1024）
SCALAR_V10_3_B = ('unique_cl_W256', 'unique_cl_W1024')
# V10.3 C：DRAM 简化派生（bank_id + bank_freq_W256 + row_freq_W256）
SCALAR_V10_3_C = ('dram_bank_id', 'dram_bank_freq_W256', 'dram_row_freq_W256')
# V10 方案 B：cacheline_paddr (paddr-line 真值) 进入 _MemCoh 嵌入。
# COMPAT-OLD-50M: 旧 50M parquet 不含 cacheline_paddr 列，__init__ 装载时
# 回退到 cacheline_addr (vaddr-line)，下游 cline_p_bucket 等同 cline_bucket。
# 全 V10+ 重采后可去掉 fallback 并把该列加入 schema 强制校验。
SCALAR_U64 = ('macro_pc', 'micro_pc', 'vaddr', 'paddr',
              'cacheline_addr', 'cacheline_paddr')

# 训练用列（不读 fetch_tick/ready_tick/commit_tick 的原值，只用 latency）
# V10.1 i-side 改造：增加 'macro_pc' 用于在 dataloader 内派生 fetch-group 特征
# (i_group_head / i_group_pos / i_group_bkt)；同时 SCALAR_SMALL_INT 中
# 'i_oracle_source' 仍保留以兼容旧 parquet schema，但不会出现在派生 feat 字典中
# （由 __getitem__ 显式过滤），亦不再被模型嵌入消费。
FEATURE_COLS = (
    list(SCALAR_BOOL)
    + list(SCALAR_SMALL_INT)
    + list(SCALAR_P1C)
    + list(SCALAR_V10_3_B)
    + list(SCALAR_V10_3_C)
    + [f'd{i}' for i in range(4)]
    + [f'pc{i}' for i in range(4)]
    + ['macro_pc_id']
    + ['vaddr', 'paddr', 'cacheline_addr', 'cacheline_paddr', 'macro_pc']
)
# V10.1：模型输入侧不再消费的列（仅 dataloader 内部使用 / 兼容字段）
_FEAT_EXCLUDE_FOR_MODEL = ('i_oracle_source', 'macro_pc')
LABEL_COLS = ('fetch_latency', 'execution_latency', 'mispredicted',
              'is_fetch_group_head')
ID_COLS = ('core_id', 'thread_id', 'pos_in_thread')


# ---------------------------- 离散化 / 桶化（轻量、可在 Dataset 端完成）
def hash_addr_bucket(arr: np.ndarray, n_bucket: int = 16) -> np.ndarray:
    """对 64-bit 地址做高位 hash 桶化（保留 cacheline 粒度）。"""
    a = (arr.astype(np.uint64) >> np.uint64(6))      # /64：cacheline 对齐
    # splitmix64
    a = a ^ (a >> np.uint64(30))
    a = (a * np.uint64(0xbf58476d1ce4e5b9)) & np.uint64(0xffffffffffffffff)
    a = a ^ (a >> np.uint64(27))
    a = (a * np.uint64(0x94d049bb133111eb)) & np.uint64(0xffffffffffffffff)
    a = a ^ (a >> np.uint64(31))
    return (a % np.uint64(n_bucket)).astype(np.int32)


def bucketize_dist(arr: np.ndarray) -> np.ndarray:
    """producer_dists 桶化：[0,1,2,3,4-7,8-15,16-31,32-63,64+] -> 9 bins。"""
    out = np.zeros_like(arr, dtype=np.int32)
    a = arr.astype(np.int64)
    out[(a >= 0) & (a <= 3)] = a[(a >= 0) & (a <= 3)]
    out[(a >= 4) & (a <= 7)] = 4
    out[(a >= 8) & (a <= 15)] = 5
    out[(a >= 16) & (a <= 31)] = 6
    out[(a >= 32) & (a <= 63)] = 7
    out[a >= 64] = 8
    out[a < 0] = 0  # -1 sentinel -> 0（合并到"无"，无所谓，pc sentinel 单独编码）
    return out


# ---------------------------- 主 Dataset
@dataclass
class DatasetSpec:
    root: str
    context_len: int = 128
    workloads: Optional[List[str]] = None     # None = 全用
    label_log1p: bool = True                  # latency 是否 log1p
    seed: int = 1234


@dataclass
class _Partition:
    workload: str
    n: int
    starts: np.ndarray         # 每个 (core,tid) 段在本分区的起始行号
    ends: np.ndarray           # 起始 + 长度
    feats: Dict[str, np.ndarray]   # 列名 -> 已加载 ndarray
    labels: Dict[str, np.ndarray]


class ParquetWindowDataset:
    """torch-friendly Dataset；支持 num_workers fork（不持有 pyarrow handle）。"""

    def __init__(self, spec: DatasetSpec):
        self.spec = spec
        meta_path = os.path.join(spec.root, 'meta.json')
        with open(meta_path) as f:
            self.meta = json.load(f)
        all_workloads = self.meta['workloads']
        self.workloads = spec.workloads or all_workloads
        self.parts: List[_Partition] = []
        # V9.8: 50M 行用 Python list 内存占用 ~1.5GB；改为 cum_offsets + searchsorted
        # 进一步拆解：global idx -> (part_idx, row_in_part) 全部从 ndarray 计算。
        part_sizes: List[int] = []

        cols = list(dict.fromkeys(list(FEATURE_COLS) + list(LABEL_COLS) + list(ID_COLS)))
        # COMPAT-OLD-50M warn 只打一次
        _compat_warn_emitted = False
        for w in self.workloads:
            path = os.path.join(spec.root, f'workload={w}', 'part-000.parquet')
            # COMPAT-OLD-50M: 旧 50M parquet 不含 cacheline_paddr 列，先看真实
            # schema 决定要请求哪些列；缺失列在装载后统一用 cacheline_addr 兜底。
            # 全 V10+ 重采后此判定可删，直接 read_table(columns=cols)。
            available = set(pq.ParquetFile(path).schema_arrow.names)
            cols_to_read = [c for c in cols if c in available]
            tbl = pq.read_table(path, columns=cols_to_read, memory_map=True)
            n = tbl.num_rows
            cid = tbl['core_id'].to_numpy()
            tid = tbl['thread_id'].to_numpy()
            # 段边界（同一 thread 连续）
            new_thread = np.empty(n, dtype=bool)
            new_thread[0] = True
            new_thread[1:] = (cid[1:] != cid[:-1]) | (tid[1:] != tid[:-1])
            starts = np.where(new_thread)[0]
            ends = np.concatenate([starts[1:], np.array([n], dtype=np.int64)])

            feats = {}
            for k in FEATURE_COLS:
                if k in available:
                    feats[k] = tbl[k].to_numpy(zero_copy_only=False)
                elif k == 'cacheline_paddr':
                    # COMPAT-OLD-50M: 缺失时回退到 cacheline_addr（vaddr-line）。
                    if not _compat_warn_emitted:
                        import sys as _sys
                        print(f"[dataset][COMPAT-OLD-50M] cacheline_paddr 列缺失"
                              f" -> fallback cacheline_addr (workload={w})",
                              file=_sys.stderr)
                        _compat_warn_emitted = True
                    feats[k] = tbl['cacheline_addr'].to_numpy(zero_copy_only=False)
                else:
                    # 兜底：缺失列填 0（理论上不会触发，仅防御）
                    feats[k] = np.zeros(n, dtype=np.int64)
            labels = {}
            for k in LABEL_COLS:
                # LABEL_COLS 当前与 FEATURE_COLS 不相交（oracle 仅作为 input
                # feature，不再作为 label），这里仅保留独立 label 通路。
                labels[k] = tbl[k].to_numpy(zero_copy_only=False)

            part = _Partition(workload=w, n=n, starts=starts, ends=ends,
                              feats=feats, labels=labels)
            self.parts.append(part)
            part_sizes.append(n)

        # cum_offsets[i] = sum(part_sizes[:i+1])，搜索 idx -> 第一个 cum>idx 即 part
        self._part_sizes = np.asarray(part_sizes, dtype=np.int64)
        self._cum_offsets = np.cumsum(self._part_sizes)
        self._total_rows = int(self._cum_offsets[-1]) if len(self._cum_offsets) else 0

        # 静态缓存：每行所属段的起点（用于左 pad mask）
        self._row_seg_start: List[np.ndarray] = []
        for p in self.parts:
            arr = np.empty(p.n, dtype=np.int64)
            for s, e in zip(p.starts, p.ends):
                arr[s:e] = s
            self._row_seg_start.append(arr)

    def __len__(self) -> int:
        return self._total_rows

    def num_features(self) -> Dict[str, int]:
        """各 embedding 的 vocab 大小，供 Model 构造使用。"""
        return {
            # macro_pc_id 不再作为模型输入；保留键名仅兼容旧日志 / cfg。
            'macro_pc_vocab': 1,
            'context_len': self.spec.context_len,
            'addr_bucket': 16,
            'dist_bucket': 9,
            'pc_vocab': 16,           # 0..6 + sentinel 255 -> 编码为 0..7，用 16 富余
            'mesi_vocab': 8,
            'coh_vocab': 8,
            'path_vocab': 8,
            'src_max': 4,
        }

    def _scan_vocab_size(self) -> int:
        return 1

    def label_positive_rates(self) -> Dict[str, float]:
        """V9.8: 自动统计稀有正例标签的 pos_weight。"""
        pos = {'mispredicted': 0, 'is_fetch_group_head': 0}
        tot = 0
        for p in self.parts:
            tot += p.n
            for k in pos:
                if k in p.labels:
                    pos[k] += int((p.labels[k] > 0).sum())
        if tot == 0:
            return {k: 0.5 for k in pos}
        return {k: (pos[k] / tot) for k in pos}

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        # cum_offsets[part_idx-1] <= idx < cum_offsets[part_idx]
        part_idx = int(np.searchsorted(self._cum_offsets, idx, side='right'))
        prev = int(self._cum_offsets[part_idx - 1]) if part_idx > 0 else 0
        row = int(idx - prev)
        p = self.parts[part_idx]
        N = self.spec.context_len
        # 锚点 t 在 thread 内的左边界
        seg_start = int(self._row_seg_start[part_idx][row])
        ctx_start = max(seg_start, row + 1 - N)
        ctx_len = (row + 1) - ctx_start
        pad_left = N - ctx_len

        # 取窗口
        sl = slice(ctx_start, row + 1)

        # ---- features
        # 1) bool/small int 直接取；新模型默认不消费 macro_pc_id，
        #    但旧 ckpt 兼容路径仍会读取它，因此这里保留该列。
        #    V10.1: i_oracle_source 不送入模型（恒值 / 分布偏移），从 feat 中剔除
        feat = {}
        small_int_keys_for_model = tuple(
            k for k in SCALAR_SMALL_INT if k not in _FEAT_EXCLUDE_FOR_MODEL
        )
        for k in (list(SCALAR_BOOL) + list(small_int_keys_for_model) + list(SCALAR_P1C) + list(SCALAR_V10_3_B) + list(SCALAR_V10_3_C)):
            feat[k] = p.feats[k][sl]
        feat['macro_pc_id'] = p.feats['macro_pc_id'][sl].astype(np.int32)
        # 2) producer dist/pc
        for i in range(4):
            feat[f'd{i}'] = bucketize_dist(p.feats[f'd{i}'][sl])
            pc = p.feats[f'pc{i}'][sl].astype(np.int32)
            pc = np.where(pc == 255, 7, pc)        # sentinel -> 7
            pc = np.clip(pc, 0, 7)
            feat[f'pc{i}'] = pc
        # 3) 地址桶
        feat['vaddr_bucket'] = hash_addr_bucket(p.feats['vaddr'][sl])
        feat['paddr_bucket'] = hash_addr_bucket(p.feats['paddr'][sl])
        feat['cline_bucket'] = hash_addr_bucket(p.feats['cacheline_addr'][sl])
        # V10 方案 B：paddr-line 真值桶（与 cline_bucket 维度独立）。
        # COMPAT-OLD-50M: 旧数据 cacheline_paddr 列在 __init__ 已被 fallback
        # 成 cacheline_addr，此处计算结果与 cline_bucket 完全相同——等同于
        # 旧 schema 下 _MemCoh.cline_p_bucket 信号退化为常量倍数。全 V10+ 后
        # cacheline_paddr 才是真正的 paddr-line 桶。
        feat['cline_p_bucket'] = hash_addr_bucket(p.feats['cacheline_paddr'][sl])

        # 4) V10.1 i-side 改造：派生 fetch-group 特征
        #    - i_group_bkt: macro_pc & ~63 的 16-桶哈希 (cacheline 身份)
        #    - i_group_head: 与上一行不同 cacheline 时为 1（fetch-group 起点）
        #    - i_group_pos: 在当前 cacheline group 内的 0-based 位置（clamp 0..15）
        #    跨 thread 边界时不连续——用 row_seg_start 对齐：窗口起点位于段起点
        #    时 head=1 强制成立。
        macro_pc_win = p.feats['macro_pc'][sl].astype(np.uint64)
        i_cl_win = (macro_pc_win >> np.uint64(6)).astype(np.int64)
        # i_group_head: 与窗口前一行不同则置 1；窗口首行用上下文边界判断
        head = np.zeros(ctx_len, dtype=np.int32)
        if ctx_len > 1:
            head[1:] = (i_cl_win[1:] != i_cl_win[:-1]).astype(np.int32)
        # 窗口首行：若不是段首则参考前一行 i_cl，否则视为新 group 起点
        if ctx_start > seg_start:
            prev_cl = int((p.feats['macro_pc'][ctx_start - 1] >> np.uint64(6)))
            head[0] = int(int(i_cl_win[0]) != prev_cl)
        else:
            head[0] = 1
        # i_group_pos: 在 head=1 的位置重置为 0，否则 +1
        pos = np.zeros(ctx_len, dtype=np.int32)
        running = 0
        for i in range(ctx_len):
            if head[i]:
                running = 0
            else:
                running += 1
            pos[i] = running
        np.clip(pos, 0, 15, out=pos)
        feat['i_group_head'] = head
        feat['i_group_pos'] = pos
        feat['i_group_bkt'] = hash_addr_bucket(macro_pc_win)

        # 左 pad（在前面拼 0）
        if pad_left > 0:
            for k, v in feat.items():
                pad = np.zeros((pad_left,) + v.shape[1:], dtype=v.dtype)
                feat[k] = np.concatenate([pad, v], axis=0)
        attn_mask = np.concatenate([
            np.zeros(pad_left, dtype=np.int8),
            np.ones(ctx_len, dtype=np.int8),
        ])

        # ---- labels（仅取锚点位置）
        lbl = {}
        for k in LABEL_COLS:
            lbl[k] = p.labels[k][row]
        if self.spec.label_log1p:
            lbl['fetch_latency_t'] = np.log1p(max(int(lbl['fetch_latency']), 0)).astype(np.float32)
            lbl['execution_latency_t'] = np.log1p(max(int(lbl['execution_latency']), 0)).astype(np.float32)
        else:
            lbl['fetch_latency_t'] = np.float32(lbl['fetch_latency'])
            lbl['execution_latency_t'] = np.float32(lbl['execution_latency'])

        return {
            'feat': feat,
            'attn_mask': attn_mask,
            'fetch_lat': lbl['fetch_latency_t'],
            'exec_lat': lbl['execution_latency_t'],
            'mispred': np.float32(lbl['mispredicted']),
            # V9.7 方案 B：fetch group head 辅助 label（detailed-only）
            'head': np.float32(lbl.get('is_fetch_group_head', 0)),
        }


# ---------------------------- Collator
def collate(batch: List[Dict]) -> Dict:
    import torch
    B = len(batch)
    N = batch[0]['attn_mask'].shape[0]
    feat_keys = list(batch[0]['feat'].keys())
    out_feat = {}
    for k in feat_keys:
        a = np.stack([b['feat'][k] for b in batch], axis=0)
        out_feat[k] = torch.from_numpy(a).long()
    attn_mask = torch.from_numpy(np.stack([b['attn_mask'] for b in batch], axis=0)).bool()
    return {
        'feat': out_feat,
        'attn_mask': attn_mask,
        'fetch_lat': torch.tensor([b['fetch_lat'] for b in batch], dtype=torch.float32),
        'exec_lat': torch.tensor([b['exec_lat'] for b in batch], dtype=torch.float32),
        'mispred':  torch.tensor([b['mispred']  for b in batch], dtype=torch.float32),
        # V9.7 方案 B：head 辅助 label
        'head':     torch.tensor([b['head']     for b in batch], dtype=torch.float32),
    }
