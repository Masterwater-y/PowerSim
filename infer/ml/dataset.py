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
    # P0-A：d-side timing-functional 字段
    'd_mshr_depth', 'dtlb_hit', 'd_walker_levels',
    'd_walker_dram_misses', 'd_bank_id',
    # V10.3 A：d-side LLC set residency / lru_pos
    'd_llc_set_residency', 'd_llc_set_lru_pos',
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
# 'macro_pc' / 'micro_pc' 仅用于在 dataloader 内派生相对结构特征：
#   - is_macro_head
#   - uop_pos_in_macro
#   - i_group_head / i_group_pos
# 不再把 macro_pc_id / i_group_bkt 这类绝对 PC 身份信号送入模型。
FEATURE_COLS = (
    list(SCALAR_BOOL)
    + list(SCALAR_SMALL_INT)
    + list(SCALAR_P1C)
    + list(SCALAR_V10_3_B)
    + list(SCALAR_V10_3_C)
    + [f'd{i}' for i in range(4)]
    + [f'pc{i}' for i in range(4)]
    + ['vaddr', 'paddr', 'cacheline_addr', 'cacheline_paddr',
       'macro_pc', 'micro_pc']
)
# V10.1：模型输入侧不再消费的列（仅 dataloader 内部使用 / 兼容字段）
_FEAT_EXCLUDE_FOR_MODEL = ('macro_pc',)
FETCH_AUX_LABEL_COLS = (
    'fetch_base_latency',
    'fetch_after_mispred_latency',
    'fetch_residual_tail_latency',
    'fetch_after_mispred_k4',
)
LABEL_COLS = ('fetch_latency', 'execution_latency', 'mispredicted',
              'is_fetch_group_head') + FETCH_AUX_LABEL_COLS
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


def build_log_bucket_centers(n_bucket: int, cycle_max: float) -> np.ndarray:
    edges = np.linspace(0.0, np.log1p(float(cycle_max)), n_bucket + 1,
                        dtype=np.float64)
    return ((edges[:-1] + edges[1:]) * 0.5).astype(np.float32)


def bucketize_latency_cycles(values: np.ndarray, n_bucket: int,
                             cycle_max: float) -> np.ndarray:
    vals = np.maximum(values.astype(np.float64), 0.0)
    edges = np.linspace(0.0, np.log1p(float(cycle_max)), n_bucket + 1,
                        dtype=np.float64)
    idx = np.searchsorted(edges[1:-1], np.log1p(vals), side='right')
    return idx.astype(np.int64)


# ---------------------------- 主 Dataset
@dataclass
class DatasetSpec:
    root: str
    context_len: int = 128
    workloads: Optional[List[str]] = None     # None = 全用
    label_log1p: bool = True                  # latency 是否 log1p
    seed: int = 1234
    exec_bucket_count: int = 16
    exec_bucket_cycle_max: float = 262144.0


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
        self.workload_to_id = {w: i for i, w in enumerate(self.workloads)}
        self.exec_bucket_centers = build_log_bucket_centers(
            spec.exec_bucket_count, spec.exec_bucket_cycle_max)
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
                if k in available:
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
        self._latency_quantiles = self.meta.get('latency_quantiles')
        self._tail_exec_thr = self._init_tail_exec_thresholds()
        self._tail_fetch_thr = self._init_tail_fetch_thresholds()
        self._ensure_fetch_aux_labels()

    def _init_tail_exec_thresholds(self) -> Dict[str, Tuple[float, float]]:
        q = (self.meta.get('latency_quantiles') or {}).get('by_workload', {})
        out = {}
        part_by_workload = {p.workload: p for p in self.parts}
        for w in self.workloads:
            exec_q = (q.get(w) or {}).get('execution_latency', {})
            if exec_q:
                p95 = float(exec_q.get('0.95', exec_q.get('0.99', 0.0)))
                p99 = float(exec_q.get('0.99', p95))
            else:
                exec_lat = part_by_workload[w].labels['execution_latency'].astype(
                    np.float64, copy=False)
                p95 = float(np.quantile(exec_lat, 0.95))
                p99 = float(np.quantile(exec_lat, 0.99))
            out[w] = (p95, p99)
        return out

    def _init_tail_fetch_thresholds(self) -> Dict[str, Tuple[float, float]]:
        q = (self.meta.get('latency_quantiles') or {}).get('by_workload', {})
        out = {}
        part_by_workload = {p.workload: p for p in self.parts}
        for w in self.workloads:
            fetch_q = (q.get(w) or {}).get('fetch_latency', {})
            if fetch_q:
                p95 = float(fetch_q.get('0.95', fetch_q.get('0.99', 0.0)))
                p99 = float(fetch_q.get('0.99', p95))
            else:
                fetch_lat = part_by_workload[w].labels['fetch_latency'].astype(
                    np.float64, copy=False)
                p95 = float(np.quantile(fetch_lat, 0.95))
                p99 = float(np.quantile(fetch_lat, 0.99))
            out[w] = (p95, p99)
        return out

    def _ensure_fetch_aux_labels(self) -> None:
        """Derive conservative fetch decomposition labels when parquet lacks them.

        The decomposition is intentionally observational and clock-preserving:
          fetch_total = fetch_base + fetch_after_mispred + fetch_residual_tail
        after_mispred is assigned to rows within K=4 rows after a committed
        mispredicted macro branch in the same thread.  residual_tail absorbs
        non-branch-window p95+ fetch gaps.  It is not a simulator-internal
        stall-reason oracle.
        """
        K = 4
        for p in self.parts:
            have_all = all(k in p.labels for k in FETCH_AUX_LABEL_COLS)
            if have_all:
                continue
            fetch = np.maximum(
                p.labels['fetch_latency'].astype(np.float64, copy=False), 0.0)
            events = (
                (p.feats['is_branch'] > 0)
                & ((p.feats['is_last_microop'] > 0)
                   | (p.feats['is_microop'] == 0))
                & (p.labels['mispredicted'] > 0)
            )
            after = np.zeros(p.n, dtype=bool)
            for s, e in zip(p.starts, p.ends):
                ev = events[s:e].astype(np.int32, copy=False)
                prefix = np.empty(ev.size + 1, dtype=np.int32)
                prefix[0] = 0
                np.cumsum(ev, out=prefix[1:])
                idx = np.arange(ev.size, dtype=np.int64)
                lo = np.maximum(idx - K, 0)
                # prefix[idx] excludes the current row, so the branch row itself
                # is not attributed to its own recovery gap.
                after[s:e] = (prefix[idx] - prefix[lo]) > 0
            p95_thr, _ = self._tail_fetch_thr[p.workload]
            residual = (fetch >= p95_thr) & (~after)
            after_lat = np.where(after, fetch, 0.0)
            residual_lat = np.where(residual, fetch, 0.0)
            base_lat = np.maximum(fetch - after_lat - residual_lat, 0.0)
            p.labels['fetch_after_mispred_k4'] = after.astype(np.int8)
            p.labels['fetch_after_mispred_latency'] = after_lat.astype(np.float64)
            p.labels['fetch_residual_tail_latency'] = residual_lat.astype(np.float64)
            p.labels['fetch_base_latency'] = base_lat.astype(np.float64)

    def __len__(self) -> int:
        return self._total_rows

    def num_features(self) -> Dict[str, int]:
        """各 embedding 的 vocab 大小，供 Model 构造使用。"""
        return {
            'context_len': self.spec.context_len,
            'addr_bucket': 16,
            'dist_bucket': 9,
            'pc_vocab': 16,           # 0..6 + sentinel 255 -> 编码为 0..7，用 16 富余
            'mesi_vocab': 8,
            'coh_vocab': 8,
            'path_vocab': 8,
            'src_max': 4,
        }

    def label_positive_rates(self) -> Dict[str, float]:
        """V9.8: 自动统计稀有正例标签的 pos_weight。"""
        pos = {'mispredicted': 0, 'is_fetch_group_head': 0,
               'exec_tail_p95': 0, 'exec_tail_p99': 0,
               'fetch_tail_p95': 0, 'fetch_tail_p99': 0,
               'fetch_after_mispred_k4': 0}
        valid_mispred = 0
        tot = 0
        for p in self.parts:
            tot += p.n
            if 'mispredicted' in p.labels:
                mask = (
                    (p.feats['is_branch'] > 0)
                    & ((p.feats['is_last_microop'] > 0)
                       | (p.feats['is_microop'] == 0))
                )
                valid_mispred += int(mask.sum())
                pos['mispredicted'] += int(((p.labels['mispredicted'] > 0) & mask).sum())
            if 'is_fetch_group_head' in p.labels:
                pos['is_fetch_group_head'] += int((p.labels['is_fetch_group_head'] > 0).sum())
            p95_thr, p99_thr = self._tail_exec_thr[p.workload]
            exec_lat = p.labels['execution_latency'].astype(np.float64, copy=False)
            pos['exec_tail_p95'] += int((exec_lat >= p95_thr).sum())
            pos['exec_tail_p99'] += int((exec_lat >= p99_thr).sum())
            fp95_thr, fp99_thr = self._tail_fetch_thr[p.workload]
            fetch_lat = p.labels['fetch_latency'].astype(np.float64, copy=False)
            pos['fetch_tail_p95'] += int((fetch_lat >= fp95_thr).sum())
            pos['fetch_tail_p99'] += int((fetch_lat >= fp99_thr).sum())
            pos['fetch_after_mispred_k4'] += int(
                (p.labels['fetch_after_mispred_k4'] > 0).sum())
        if tot == 0:
            return {k: 0.5 for k in pos}
        return {
            'mispredicted': pos['mispredicted'] / max(valid_mispred, 1),
            'mispred_valid_rate': valid_mispred / tot,
            'is_fetch_group_head': pos['is_fetch_group_head'] / tot,
            'exec_tail_p95': pos['exec_tail_p95'] / tot,
            'exec_tail_p99': pos['exec_tail_p99'] / tot,
            'fetch_tail_p95': pos['fetch_tail_p95'] / tot,
            'fetch_tail_p99': pos['fetch_tail_p99'] / tot,
            'fetch_after_mispred_k4': pos['fetch_after_mispred_k4'] / tot,
        }

    def latency_quantiles(self) -> Dict[str, object]:
        if self._latency_quantiles is not None:
            return self._latency_quantiles
        probs = (0.5, 0.9, 0.95, 0.99, 0.999)
        by_workload = {}
        fetch_all = []
        exec_all = []
        for p in self.parts:
            fetch = p.labels['fetch_latency'].astype(np.float64, copy=False)
            exe = p.labels['execution_latency'].astype(np.float64, copy=False)
            fetch_all.append(fetch)
            exec_all.append(exe)
            by_workload[p.workload] = {
                'rows': int(p.n),
                'fetch_latency': {
                    str(q): float(np.quantile(fetch, q)) for q in probs
                },
                'execution_latency': {
                    str(q): float(np.quantile(exe, q)) for q in probs
                },
            }
        self._latency_quantiles = {
            'probs': list(probs),
            'global': {
                'rows': int(sum(a.size for a in fetch_all)),
                'fetch_latency': {
                    str(q): float(np.quantile(np.concatenate(fetch_all), q))
                    for q in probs
                },
                'execution_latency': {
                    str(q): float(np.quantile(np.concatenate(exec_all), q))
                    for q in probs
                },
            },
            'by_workload': by_workload,
        }
        return self._latency_quantiles

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
        # 1) bool/small int 直接取；oracle i-side 字段已从 schema 移除。
        feat = {}
        small_int_keys_for_model = tuple(
            k for k in SCALAR_SMALL_INT if k not in _FEAT_EXCLUDE_FOR_MODEL
        )
        for k in (list(SCALAR_BOOL) + list(small_int_keys_for_model)
                  + list(SCALAR_P1C) + list(SCALAR_V10_3_B)
                  + list(SCALAR_V10_3_C)):
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
        # V10 方案 B：paddr-line 真值桶（与 cline_bucket 维度独立）。
        # COMPAT-OLD-50M: 旧数据 cacheline_paddr 列在 __init__ 已被 fallback
        # 成 cacheline_addr，此处计算结果与 cline_bucket 完全相同——等同于
        # 旧 schema 下 _MemCoh.cline_p_bucket 信号退化为常量倍数。全 V10+ 后
        # cacheline_paddr 才是真正的 paddr-line 桶。
        feat['cline_p_bucket'] = hash_addr_bucket(p.feats['cacheline_paddr'][sl])

        # 4) 结构/前端派生：只保留相对位置，不编码绝对 PC 身份
        #    - is_macro_head: 当前 µop 是否为本动态 macro 的首条
        #    - uop_pos_in_macro: 当前 µop 在 macro 内的 0-based 位置
        #    - i_group_head: 与上一行不同 cacheline 时为 1（fetch-group 起点）
        #    - i_group_pos: 在当前 cacheline group 内的 0-based 位置（clamp 0..15）
        #    跨 thread 边界时不连续——用 row_seg_start 对齐。
        macro_pc_win = p.feats['macro_pc'][sl].astype(np.uint64)
        is_micro_win = p.feats['is_microop'][sl].astype(np.int32)
        is_last_micro_win = p.feats['is_last_microop'][sl].astype(np.int32)
        i_cl_win = (macro_pc_win >> np.uint64(6)).astype(np.int64)
        macro_head = np.zeros(ctx_len, dtype=np.int32)
        uop_pos = np.zeros(ctx_len, dtype=np.int32)
        i_group_head = np.zeros(ctx_len, dtype=np.int32)
        i_group_pos = np.zeros(ctx_len, dtype=np.int32)
        prev_macro_pc = None
        prev_i_cl = None
        prev_is_micro = 0
        prev_is_last_micro = 0
        if ctx_start > seg_start:
            prev_macro_pc = int(p.feats['macro_pc'][ctx_start - 1])
            prev_i_cl = int(p.feats['macro_pc'][ctx_start - 1] >> np.uint64(6))
            prev_is_micro = int(p.feats['is_microop'][ctx_start - 1])
            prev_is_last_micro = int(p.feats['is_last_microop'][ctx_start - 1])
        macro_running = 0
        i_group_running = 0
        for i in range(ctx_len):
            cur_macro_pc = int(macro_pc_win[i])
            prev_macro_ended = (prev_macro_pc is None or
                                prev_is_micro == 0 or
                                prev_is_last_micro == 1)
            cur_macro_head = 1 if (prev_macro_ended or
                                   cur_macro_pc != prev_macro_pc) else 0
            if cur_macro_head:
                macro_running = 0
            else:
                macro_running += 1
            macro_head[i] = cur_macro_head
            uop_pos[i] = min(macro_running, 15)

            cur_i_cl = int(i_cl_win[i])
            cur_i_group_head = 1 if (prev_i_cl is None or cur_i_cl != prev_i_cl) else 0
            if cur_i_group_head:
                i_group_running = 0
            else:
                i_group_running += 1
            i_group_head[i] = cur_i_group_head
            i_group_pos[i] = min(i_group_running, 15)

            prev_macro_pc = cur_macro_pc
            prev_i_cl = cur_i_cl
            prev_is_micro = int(is_micro_win[i])
            prev_is_last_micro = int(is_last_micro_win[i])
        feat['is_macro_head'] = macro_head
        feat['uop_pos_in_macro'] = uop_pos
        feat['i_group_head'] = i_group_head
        feat['i_group_pos'] = i_group_pos

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
        fetch_raw = np.float32(max(float(lbl['fetch_latency']), 0.0))
        exec_raw = np.float32(max(float(lbl['execution_latency']), 0.0))
        fetch_base_raw = np.float32(max(
            float(p.labels['fetch_base_latency'][row]), 0.0))
        fetch_after_mispred_raw = np.float32(max(
            float(p.labels['fetch_after_mispred_latency'][row]), 0.0))
        fetch_residual_tail_raw = np.float32(max(
            float(p.labels['fetch_residual_tail_latency'][row]), 0.0))
        if self.spec.label_log1p:
            lbl['fetch_latency_t'] = np.log1p(fetch_raw).astype(np.float32)
            lbl['execution_latency_t'] = np.log1p(exec_raw).astype(np.float32)
            fetch_base_t = np.log1p(fetch_base_raw).astype(np.float32)
            fetch_after_mispred_t = np.log1p(fetch_after_mispred_raw).astype(np.float32)
            fetch_residual_tail_t = np.log1p(fetch_residual_tail_raw).astype(np.float32)
        else:
            lbl['fetch_latency_t'] = fetch_raw
            lbl['execution_latency_t'] = exec_raw
            fetch_base_t = fetch_base_raw
            fetch_after_mispred_t = fetch_after_mispred_raw
            fetch_residual_tail_t = fetch_residual_tail_raw
        exec_bucket = int(bucketize_latency_cycles(
            np.asarray([exec_raw], dtype=np.float64),
            self.spec.exec_bucket_count,
            self.spec.exec_bucket_cycle_max)[0])
        exec_residual = np.float32(
            lbl['execution_latency_t'] - self.exec_bucket_centers[exec_bucket])
        tail_p95_thr, tail_p99_thr = self._tail_exec_thr[p.workload]
        fetch_tail_p95_thr, fetch_tail_p99_thr = self._tail_fetch_thr[p.workload]
        is_branch = int(p.feats['is_branch'][row]) > 0
        is_microop = int(p.feats['is_microop'][row]) > 0
        is_last_microop = int(p.feats['is_last_microop'][row]) > 0
        mispred_mask = is_branch and (is_last_microop or not is_microop)

        return {
            'feat': feat,
            'attn_mask': attn_mask,
            'fetch_lat': lbl['fetch_latency_t'],
            'exec_lat': lbl['execution_latency_t'],
            'mispred': np.float32(lbl['mispredicted']),
            'mispred_mask': np.float32(mispred_mask),
            # V9.7 方案 B：fetch group head 辅助 label（detailed-only）
            'head': np.float32(lbl.get('is_fetch_group_head', 0)),
            'fetch_lat_raw': fetch_raw,
            'fetch_base': fetch_base_t,
            'fetch_after_mispred': fetch_after_mispred_t,
            'fetch_residual_tail': fetch_residual_tail_t,
            'fetch_base_raw': fetch_base_raw,
            'fetch_after_mispred_raw': fetch_after_mispred_raw,
            'fetch_residual_tail_raw': fetch_residual_tail_raw,
            'fetch_tail_p95': np.float32(fetch_raw >= fetch_tail_p95_thr),
            'fetch_tail_p99': np.float32(fetch_raw >= fetch_tail_p99_thr),
            'fetch_tail_p95_thr': np.float32(fetch_tail_p95_thr),
            'fetch_tail_p99_thr': np.float32(fetch_tail_p99_thr),
            'fetch_after_mispred_mask': np.float32(
                p.labels['fetch_after_mispred_k4'][row] > 0),
            'fetch_residual_tail_mask': np.float32(fetch_residual_tail_raw > 0.0),
            'exec_lat_raw': exec_raw,
            'exec_bucket': np.int64(exec_bucket),
            'exec_residual': exec_residual,
            'exec_tail_p95': np.float32(exec_raw >= tail_p95_thr),
            'exec_tail_p99': np.float32(exec_raw >= tail_p99_thr),
            'exec_tail_p95_thr': np.float32(tail_p95_thr),
            'exec_tail_p99_thr': np.float32(tail_p99_thr),
            'workload_id': np.int64(self.workload_to_id[p.workload]),
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
        'mispred_mask': torch.tensor([b['mispred_mask'] for b in batch], dtype=torch.float32),
        # V9.7 方案 B：head 辅助 label
        'head':     torch.tensor([b['head']     for b in batch], dtype=torch.float32),
        'fetch_lat_raw': torch.tensor([b['fetch_lat_raw'] for b in batch], dtype=torch.float32),
        'fetch_base': torch.tensor([b['fetch_base'] for b in batch], dtype=torch.float32),
        'fetch_after_mispred': torch.tensor([b['fetch_after_mispred'] for b in batch], dtype=torch.float32),
        'fetch_residual_tail': torch.tensor([b['fetch_residual_tail'] for b in batch], dtype=torch.float32),
        'fetch_base_raw': torch.tensor([b['fetch_base_raw'] for b in batch], dtype=torch.float32),
        'fetch_after_mispred_raw': torch.tensor([b['fetch_after_mispred_raw'] for b in batch], dtype=torch.float32),
        'fetch_residual_tail_raw': torch.tensor([b['fetch_residual_tail_raw'] for b in batch], dtype=torch.float32),
        'fetch_tail_p95': torch.tensor([b['fetch_tail_p95'] for b in batch], dtype=torch.float32),
        'fetch_tail_p99': torch.tensor([b['fetch_tail_p99'] for b in batch], dtype=torch.float32),
        'fetch_tail_p95_thr': torch.tensor([b['fetch_tail_p95_thr'] for b in batch], dtype=torch.float32),
        'fetch_tail_p99_thr': torch.tensor([b['fetch_tail_p99_thr'] for b in batch], dtype=torch.float32),
        'fetch_after_mispred_mask': torch.tensor([b['fetch_after_mispred_mask'] for b in batch], dtype=torch.float32),
        'fetch_residual_tail_mask': torch.tensor([b['fetch_residual_tail_mask'] for b in batch], dtype=torch.float32),
        'exec_lat_raw': torch.tensor([b['exec_lat_raw'] for b in batch], dtype=torch.float32),
        'exec_bucket': torch.tensor([b['exec_bucket'] for b in batch], dtype=torch.long),
        'exec_residual': torch.tensor([b['exec_residual'] for b in batch], dtype=torch.float32),
        'exec_tail_p95': torch.tensor([b['exec_tail_p95'] for b in batch], dtype=torch.float32),
        'exec_tail_p99': torch.tensor([b['exec_tail_p99'] for b in batch], dtype=torch.float32),
        'exec_tail_p95_thr': torch.tensor([b['exec_tail_p95_thr'] for b in batch], dtype=torch.float32),
        'exec_tail_p99_thr': torch.tensor([b['exec_tail_p99_thr'] for b in batch], dtype=torch.float32),
        'workload_id': torch.tensor([b['workload_id'] for b in batch], dtype=torch.long),
    }
