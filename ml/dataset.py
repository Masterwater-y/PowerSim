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
)
SCALAR_U64 = ('macro_pc', 'micro_pc', 'vaddr', 'paddr', 'cacheline_addr')

# 训练用列（不读 fetch_tick/ready_tick/commit_tick 的原值，只用 latency）
FEATURE_COLS = (
    list(SCALAR_BOOL)
    + list(SCALAR_SMALL_INT)
    + ['macro_pc_id']
    + [f'd{i}' for i in range(4)]
    + [f'pc{i}' for i in range(4)]
    + ['vaddr', 'paddr', 'cacheline_addr']
)
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
        self.global_index: List[Tuple[int, int]] = []   # (part_idx, row_idx)

        cols = list(dict.fromkeys(list(FEATURE_COLS) + list(LABEL_COLS) + list(ID_COLS)))
        for w in self.workloads:
            path = os.path.join(spec.root, f'workload={w}', 'part-000.parquet')
            tbl = pq.read_table(path, columns=cols, memory_map=True)
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
                feats[k] = tbl[k].to_numpy(zero_copy_only=False)
            labels = {}
            for k in LABEL_COLS:
                # LABEL_COLS 当前与 FEATURE_COLS 不相交（oracle 仅作为 input
                # feature，不再作为 label），这里仅保留独立 label 通路。
                labels[k] = tbl[k].to_numpy(zero_copy_only=False)

            part = _Partition(workload=w, n=n, starts=starts, ends=ends,
                              feats=feats, labels=labels)
            self.parts.append(part)
            for r in range(n):
                self.global_index.append((len(self.parts) - 1, r))

        self._index_arr = np.asarray(self.global_index, dtype=np.int64)

        # 静态缓存：每行所属段的起点（用于左 pad mask）
        self._row_seg_start: List[np.ndarray] = []
        for p in self.parts:
            arr = np.empty(p.n, dtype=np.int64)
            for s, e in zip(p.starts, p.ends):
                arr[s:e] = s
            self._row_seg_start.append(arr)

    def __len__(self) -> int:
        return len(self.global_index)

    def num_features(self) -> Dict[str, int]:
        """各 embedding 的 vocab 大小，供 Model 构造使用。"""
        return {
            'macro_pc_vocab': int(self.meta.get('n_macro_pc',
                                                 self._scan_vocab_size())),
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
        mx = 0
        for p in self.parts:
            mx = max(mx, int(p.feats['macro_pc_id'].max()) + 1)
        return mx

    def __getitem__(self, idx: int) -> Dict[str, np.ndarray]:
        part_idx, row = self._index_arr[idx]
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
        # 1) bool/small int / macro_pc_id 直接取
        feat = {}
        for k in (list(SCALAR_BOOL) + list(SCALAR_SMALL_INT) + ['macro_pc_id']):
            feat[k] = p.feats[k][sl]
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
