"""build_windows.py — gem5 ROI 原始 trace -> 窗口级训练样本 jsonl。

严格遵守 docs/design.md 的修正：
  - 程序序切窗（按 micro_seq / pos_in_thread），不依赖 commit_tick 排序输入。
  - 多核拼接按 core_id 段串接（不按 cycle）。
  - 输入只用 functional 字段（records.micro 的架构态子集）。
  - 标签全部窗口聚合 PMU，主目标用比率（CPI / MPKI / miss-rate）。
  - cycles 用窗口内 commit_tick 端点差（除以 tick_per_cycle）。

输入目录结构（taogen gem5 输出）：
  <raw>/<WNAME>/tao_trace/board.processor.cores<C>.core.tao_trace.tao_trace.records.micro.jsonl
  <raw>/<WNAME>/tao_trace/...labels.micro.jsonl
（--ff-atomic 模式下 SimObject 路径会变成 board.processor.switch<C>.*，CORE_RE 同时兼容两种）

输出：
  <out>/windows.jsonl  每行一个样本（含 tokens / label / 元数据）
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures as cf
import glob
import json
import math
import os
import random
import re
import shutil
import sys
import time
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

import numpy as np
try:
    import pyarrow.parquet as pq
except ImportError:  # lightweight smoke tests may not have pyarrow installed
    pq = None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from model import tokenizer as tk  # noqa: E402
from model.shared_state import (  # noqa: E402
    SS_CORE_FEATURE_KEYS,
    SS_GLOBAL_FEATURE_KEYS,
    SharedStateFeatureEngine,
    build_trace_cache,
)


CORE_RE = re.compile(r"(?:cores|switch)(\d*)\.core")
ALIGNED_PARQUET_COLS = [
    "core_id", "thread_id", "micro_seq", "seq_num",
    "macro_pc", "micro_pc", "vaddr", "paddr",
    "cacheline_addr", "cacheline_paddr", "size",
    "is_load", "is_store", "is_atomic",
    "is_branch", "is_branch_cond", "is_branch_indirect",
    "is_call", "is_return", "is_int", "is_fp",
    "is_simd", "is_serialize", "is_microop", "is_last_microop",
    "op_class",
    "n_src", "n_dst", "producer_dists", "producer_classes",
    "path_class", "coh_oracle", "i_path_class",
    "d_mshr_depth", "dtlb_hit", "itlb_hit",
    "fetch_tick", "issue_tick", "complete_tick", "commit_tick",
    "ready_tick", "ready_source", "mispredicted",
]

# path_class 阈值（见 config/pmu_keys.yaml）
PC_L2 = 1      # >=1 视为 L1 miss
PC_DRAM = 4    # >=4 视为 LLC miss
# coh_oracle: 2=REMOTE_HIT_CLEAN 3=REMOTE_HIT_DIRTY
COH_REMOTE = {2, 3}

# label 维度顺序（与 pmu_keys.yaml keys 顺序一致）
PMU_KEYS = [
    "cpi_uop",
    "branch_miss",
    "l1d_ld_miss",
    "l1d_st_miss",
    "l2_ld_miss",
    "l2_st_miss",
    "llc_miss",
    "dtlb_miss",
]
CPI_KEY = "cpi_uop"


def _mean_label_value(sample: dict, key: str) -> Optional[float]:
    label_keys = list(sample.get("label_keys") or PMU_KEYS)
    try:
        idx = label_keys.index(key)
    except ValueError:
        return None
    vals = []
    for row in sample.get("label") or []:
        try:
            vals.append(float(row[idx]))
        except Exception:
            continue
    if not vals:
        return None
    return float(sum(vals) / len(vals))


def _variant_by_cpi(workload: str, mean_cpi: Optional[float]) -> str:
    if mean_cpi is None:
        return workload
    cpi = float(mean_cpi)
    if workload == "W_chase_dram":
        if cpi < 3.0:
            return "W_chase_low"
        if cpi < 10.0:
            return "W_chase_mid"
        if cpi < 25.0:
            return "W_chase_high"
        return "W_chase_extreme"
    if workload == "W_false_sharing":
        if cpi < 10.0:
            return "W_fs_low"
        if cpi < 25.0:
            return "W_fs_mid"
        if cpi < 45.0:
            return "W_fs_high"
        return "W_fs_extreme"
    if workload == "W_stream":
        if cpi < 8.0:
            return "W_stream_low"
        if cpi < 20.0:
            return "W_stream_high"
        return "W_stream_extreme"
    if workload == "W_feed_ranking":
        return "W_feed_low" if cpi < 8.0 else "W_feed_high"
    if workload == "W_fp_compute_dense":
        return "W_fpcd_low" if cpi < 5.0 else "W_fpcd_high"
    if workload == "W_ads_ctr":
        return "W_adsctr_low" if cpi < 3.0 else "W_adsctr_high"
    return workload


def _relabel_by_cpi(sample: dict) -> dict:
    workload = str(sample.get("workload") or "")
    mean_cpi = _mean_label_value(sample, CPI_KEY)
    sample["mean_cpi_uop"] = float(mean_cpi) if mean_cpi is not None else 0.0
    sample["workload_variant"] = _variant_by_cpi(workload, mean_cpi)
    return sample


def _dedup_hard_keep_by_cpi(sample: dict) -> bool:
    mean_cpi = _mean_label_value(sample, CPI_KEY)
    if mean_cpi is None:
        return False
    cpi = float(mean_cpi)
    return cpi >= 30.0 or cpi <= 0.35 or (3.0 <= cpi <= 10.0)


class Fenwick:
    def __init__(self, n: int):
        self.n = int(n)
        self.bit = [0] * (self.n + 1)

    def add(self, i: int, delta: int) -> None:
        while i <= self.n:
            self.bit[i] += delta
            i += i & -i

    def sum(self, i: int) -> int:
        s = 0
        i = min(int(i), self.n)
        while i > 0:
            s += self.bit[i]
            i -= i & -i
        return s


def is_mem_rec(rec: dict) -> bool:
    return bool(rec.get("is_load") or rec.get("is_store") or rec.get("is_atomic"))


def functional_cacheline(rec: dict) -> Optional[int]:
    """Functional address stream key used for RD/stride.

    Prefer vaddr so the feature remains available without physical/microarch
    oracle state. cacheline_addr is accepted as a fallback for pre-normalized
    traces where vaddr is absent.
    """
    v = int(rec.get("vaddr", 0) or 0)
    if v != 0:
        return v >> 6
    cl = int(rec.get("cacheline_addr", 0) or 0)
    if cl != 0:
        return cl
    return None


def annotate_rd_stride(seq: List[dict], rd_window: int = 8192) -> None:
    """Annotate each record with bounded sliding RD and stride buckets.

    RD is exact within the recent rd_window memory references on the same
    core. Reuse older than that is collapsed into RD_FAR.
    """
    mem_total = sum(1 for r in seq if is_mem_rec(r))
    bit = Fenwick(max(1, mem_total + 2))
    active_pos: Dict[int, int] = {}
    seen_lines = set()
    active_queue = deque()
    last_line: Optional[int] = None
    mem_idx = 0

    for rec in seq:
        if not is_mem_rec(rec):
            rec["_rd_bucket"] = tk.RD_NONMEM
            rec["_stride_bucket"] = tk.ST_NONMEM
            continue

        mem_idx += 1
        line = functional_cacheline(rec)
        stride_delta = None if last_line is None or line is None else line - last_line
        rec["_stride_bucket"] = tk.stride_bucket_from_delta(stride_delta)

        expire_before = mem_idx - int(rd_window)
        while active_queue and active_queue[0][0] < expire_before:
            old_pos, old_line = active_queue.popleft()
            if active_pos.get(old_line) == old_pos:
                bit.add(old_pos, -1)
                del active_pos[old_line]

        if line is None:
            rec["_rd_bucket"] = tk.RD_COLD
        else:
            prev = active_pos.get(line)
            if prev is None:
                rec["_rd_bucket"] = tk.RD_FAR if line in seen_lines else tk.RD_COLD
            else:
                rd = bit.sum(mem_idx - 1) - bit.sum(prev)
                rec["_rd_bucket"] = tk.rd_bucket_from_distance(rd)
                bit.add(prev, -1)
            bit.add(mem_idx, 1)
            active_pos[line] = mem_idx
            active_queue.append((mem_idx, line))
            seen_lines.add(line)
            last_line = line


class RecentLineTracker:
    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self.q = deque()
        self.counts = defaultdict(int)

    def seen(self, line: int) -> bool:
        return self.counts.get(line, 0) > 0

    def add(self, line: int) -> None:
        if self.capacity <= 0:
            return
        self.q.append(line)
        self.counts[line] += 1
        while len(self.q) > self.capacity:
            old = self.q.popleft()
            self.counts[old] -= 1
            if self.counts[old] <= 0:
                del self.counts[old]

    def working_set_size(self) -> int:
        return len(self.counts)


def annotate_functional_proxies(seq: List[dict]) -> None:
    """Annotate per-uop functional-only warm-state hints.

    These fields are computed from same-core program-order history only. They
    deliberately avoid label/timing fields such as path_class, miss status, or
    misprediction.
    """
    trackers = {
        8192: RecentLineTracker(8192),
        65536: RecentLineTracker(65536),
    }
    prev: Optional[dict] = None
    for rec in seq:
        head = is_macro_head(rec, prev)
        if int(rec.get("is_microop", 0) or 0) == 0:
            rec["_macro_pos_bucket"] = tk.MACRO_POS_SINGLE
        elif head and int(rec.get("is_last_microop", 0) or 0):
            rec["_macro_pos_bucket"] = tk.MACRO_POS_SINGLE
        elif head:
            rec["_macro_pos_bucket"] = tk.MACRO_POS_FIRST
        elif int(rec.get("is_last_microop", 0) or 0):
            rec["_macro_pos_bucket"] = tk.MACRO_POS_LAST
        else:
            rec["_macro_pos_bucket"] = tk.MACRO_POS_MIDDLE
        rec["_seen_line_8k"] = 0
        rec["_seen_line_64k"] = 0
        rec["_recent_ws_64k"] = trackers[65536].working_set_size()
        if not is_mem_rec(rec):
            prev = rec
            continue
        line = functional_cacheline(rec)
        if line is None:
            prev = rec
            continue
        rec["_seen_line_8k"] = 1 if trackers[8192].seen(line) else 0
        rec["_seen_line_64k"] = 1 if trackers[65536].seen(line) else 0
        rec["_recent_ws_64k"] = trackers[65536].working_set_size()
        for tr in trackers.values():
            tr.add(line)
        prev = rec


def annotate_cross_core_functional_proxies(
    seqs_by_core: Dict[int, List[dict]],
) -> None:
    """Annotate per-uop deployable cross-core cache/coherence proxies.

    This is a functional replay over program-order events. It does not use
    commit_tick, path_class, coh_oracle, miss status, or any timing label.
    """
    for seq in seqs_by_core.values():
        for rec in seq:
            rec["_line_role_bucket"] = tk.LINE_ROLE_NONMEM
            rec["_xcore_mem_bucket"] = tk.XCORE_NONMEM
            rec["_coherence_bucket"] = tk.COH_NONMEM
            rec["_fanout_bucket"] = 0
            rec["_fanout_proxy"] = 0.0

    events = []
    for c, seq in seqs_by_core.items():
        for idx, rec in enumerate(seq):
            if not is_mem_rec(rec):
                continue
            line = functional_cacheline(rec)
            events.append((
                int(rec.get("micro_seq", idx) or idx),
                int(c),
                int(idx),
                line,
                rec,
            ))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    line_state: Dict[int, dict] = {}
    for _ms, c, _idx, line, rec in events:
        if line is None:
            rec["_line_role_bucket"] = tk.LINE_ROLE_UNKNOWN
            rec["_xcore_mem_bucket"] = tk.XCORE_NO_LINE
            rec["_coherence_bucket"] = tk.COH_NO_LINE
            continue

        st = line_state.get(line)
        is_ld = bool(rec.get("is_load", 0) or 0)
        is_st = bool(rec.get("is_store", 0) or rec.get("is_atomic", 0) or 0)
        if st is None:
            st = {
                "access_cores": set(),
                "reader_cores": set(),
                "writer_cores": set(),
                "last_writer_core": None,
                "owner_switch_count": 0,
                "access_count": 0,
            }
            line_state[line] = st

        access_cores = st["access_cores"]
        reader_cores = st["reader_cores"]
        writer_cores = st["writer_cores"]
        last_writer = st["last_writer_core"]
        remote_readers = set(reader_cores) - {c}
        remote_writers = set(writer_cores) - {c}
        remote_writer = last_writer is not None and last_writer != c
        fanout = max(
            len(remote_readers),
            len(remote_writers),
            int(st["owner_switch_count"]),
        )
        rec["_fanout_proxy"] = float(fanout)
        rec["_fanout_bucket"] = tk.log_count_bucket(fanout)

        if not access_cores:
            line_role = tk.LINE_ROLE_FIRST
        elif remote_writer:
            line_role = tk.LINE_ROLE_REMOTE
        elif len(writer_cores | ({c} if is_st else set())) >= 2:
            line_role = tk.LINE_ROLE_MULTIWRITER
        elif len(access_cores | {c}) >= 2:
            line_role = tk.LINE_ROLE_SHARED
        elif int(st["access_count"]) >= 8:
            line_role = tk.LINE_ROLE_HOT
        else:
            line_role = tk.LINE_ROLE_PRIVATE
        rec["_line_role_bucket"] = line_role

        if is_ld and remote_writer:
            xcore = tk.XCORE_READ_AFTER_REMOTE_STORE
            coh = tk.COH_REMOTE_MODIFIED_READ
        elif is_st and remote_writer:
            xcore = tk.XCORE_STORE_AFTER_REMOTE_STORE
            coh = tk.COH_REMOTE_OWNER_TRANSFER
        elif is_st and remote_readers:
            xcore = tk.XCORE_STORE_TO_SHARED_LINE
            coh = tk.COH_STORE_INVALIDATE_READERS
        elif remote_writer:
            xcore = tk.XCORE_LAST_WRITER_OTHER
            coh = tk.COH_UNKNOWN
        elif last_writer == c and is_st:
            xcore = tk.XCORE_LAST_WRITER_SELF
            coh = tk.COH_LOCAL_OWNED_STORE
        elif remote_writers:
            xcore = tk.XCORE_RECENT_WRITER_OTHER
            coh = tk.COH_UNKNOWN
        elif remote_readers:
            xcore = tk.XCORE_RECENT_READER_OTHER
            coh = tk.COH_SHARED_LOAD if is_ld else tk.COH_UNKNOWN
        else:
            xcore = tk.XCORE_PRIVATE
            coh = tk.COH_LOCAL_PRIVATE
        rec["_xcore_mem_bucket"] = xcore
        rec["_coherence_bucket"] = coh

        access_cores.add(c)
        st["access_count"] = int(st["access_count"]) + 1
        if is_ld:
            reader_cores.add(c)
        if is_st:
            if last_writer is not None and last_writer != c:
                st["owner_switch_count"] = int(st["owner_switch_count"]) + 1
            writer_cores.add(c)
            st["last_writer_core"] = c


def _prefix_sum(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values)
    return np.concatenate([
        np.zeros((1,), dtype=np.float64),
        np.cumsum(arr.astype(np.float64, copy=False)),
    ])


def _prefix_count(values: np.ndarray) -> np.ndarray:
    arr = np.asarray(values)
    return np.concatenate([
        np.zeros((1,), dtype=np.int64),
        np.cumsum(arr.astype(np.int64, copy=False)),
    ])


def _span_sum(prefix: np.ndarray, start: int, end: int) -> float:
    return float(prefix[int(end)] - prefix[int(start)])


def annotate_columnar_trace(trace: ColumnCoreTrace,
                            rd_window: int = 8192) -> None:
    """Columnar equivalent of same-core RD/stride/functional annotations."""
    n = len(trace)
    mem = trace.mem_mask()
    line = trace.line_keys()
    rd = np.full((n,), tk.RD_NONMEM, dtype=np.int16)
    stride = np.full((n,), tk.ST_NONMEM, dtype=np.int16)
    seen8 = np.zeros((n,), dtype=np.int8)
    seen64 = np.zeros((n,), dtype=np.int8)
    ws64 = np.zeros((n,), dtype=np.int32)

    bit = Fenwick(max(1, int(mem.sum()) + 2))
    active_pos: Dict[int, int] = {}
    active_queue = deque()
    seen_lines = set()
    tr8 = RecentLineTracker(8192)
    tr64 = RecentLineTracker(65536)
    last_line: Optional[int] = None
    mem_idx = 0
    for i in range(n):
        if not bool(mem[i]):
            continue
        mem_idx += 1
        ln = int(line[i])
        cur_line = ln if ln != 0 else None
        stride[i] = tk.stride_bucket_from_delta(
            None if last_line is None or cur_line is None
            else cur_line - last_line
        )

        expire_before = mem_idx - int(rd_window)
        while active_queue and active_queue[0][0] < expire_before:
            old_pos, old_line = active_queue.popleft()
            if active_pos.get(old_line) == old_pos:
                bit.add(old_pos, -1)
                del active_pos[old_line]

        if cur_line is None:
            rd[i] = tk.RD_COLD
        else:
            prev = active_pos.get(cur_line)
            if prev is None:
                rd[i] = tk.RD_FAR if cur_line in seen_lines else tk.RD_COLD
            else:
                dist = bit.sum(mem_idx - 1) - bit.sum(prev)
                rd[i] = tk.rd_bucket_from_distance(dist)
                bit.add(prev, -1)
            bit.add(mem_idx, 1)
            active_pos[cur_line] = mem_idx
            active_queue.append((mem_idx, cur_line))
            seen_lines.add(cur_line)

        ws64[i] = tr64.working_set_size()
        if cur_line is not None:
            seen8[i] = 1 if tr8.seen(cur_line) else 0
            seen64[i] = 1 if tr64.seen(cur_line) else 0
            tr8.add(cur_line)
            tr64.add(cur_line)
            last_line = cur_line

    macro_pos = np.full((n,), tk.MACRO_POS_UNKNOWN, dtype=np.int16)
    macro_head = np.zeros((n,), dtype=np.int8)
    macro_pc = trace.col("macro_pc", 0)
    is_micro = trace.col("is_microop", 0)
    is_last = trace.col("is_last_microop", 0)
    for i in range(n):
        if i == 0:
            head = True
        else:
            prev_ended = int(is_micro[i - 1]) == 0 or int(is_last[i - 1]) == 1
            head = bool(prev_ended or macro_pc[i] != macro_pc[i - 1])
        macro_head[i] = 1 if head else 0
        if int(is_micro[i]) == 0:
            macro_pos[i] = tk.MACRO_POS_SINGLE
        elif head and int(is_last[i]) == 1:
            macro_pos[i] = tk.MACRO_POS_SINGLE
        elif head:
            macro_pos[i] = tk.MACRO_POS_FIRST
        elif int(is_last[i]) == 1:
            macro_pos[i] = tk.MACRO_POS_LAST
        else:
            macro_pos[i] = tk.MACRO_POS_MIDDLE

    trace.ann["rd_bucket"] = rd
    trace.ann["stride_bucket"] = stride
    trace.ann["seen8"] = seen8
    trace.ann["seen64"] = seen64
    trace.ann["recent_ws64"] = ws64
    trace.ann["macro_pos_bucket"] = macro_pos
    trace.ann["macro_head"] = macro_head


def annotate_columnar_cross_core(
    traces_by_core: Dict[int, ColumnCoreTrace],
) -> None:
    """Columnar equivalent of cross-core functional proxy annotations."""
    for tr in traces_by_core.values():
        n = len(tr)
        tr.ann["line_role"] = np.full(
            (n,), tk.LINE_ROLE_NONMEM, dtype=np.int16)
        tr.ann["xcore"] = np.full((n,), tk.XCORE_NONMEM, dtype=np.int16)
        tr.ann["coherence"] = np.full((n,), tk.COH_NONMEM, dtype=np.int16)
        tr.ann["fanout_bucket"] = np.zeros((n,), dtype=np.int16)
        tr.ann["fanout_proxy"] = np.zeros((n,), dtype=np.float32)

    events = []
    for c, tr in traces_by_core.items():
        mem = tr.mem_mask()
        line = tr.line_keys()
        micro_seq = tr.col("micro_seq", 0)
        idxs = np.nonzero(mem)[0]
        for idx in idxs.tolist():
            events.append((int(micro_seq[idx]), int(c), int(idx), int(line[idx])))
    events.sort(key=lambda x: (x[0], x[1], x[2]))

    line_state: Dict[int, dict] = {}
    for _ms, c, idx, line in events:
        tr = traces_by_core[c]
        if line == 0:
            tr.ann["line_role"][idx] = tk.LINE_ROLE_UNKNOWN
            tr.ann["xcore"][idx] = tk.XCORE_NO_LINE
            tr.ann["coherence"][idx] = tk.COH_NO_LINE
            continue
        st = line_state.get(line)
        is_ld = bool(tr.col("is_load", 0)[idx])
        is_st = bool(tr.store_mask()[idx])
        if st is None:
            st = {
                "access_cores": set(),
                "reader_cores": set(),
                "writer_cores": set(),
                "last_writer_core": None,
                "owner_switch_count": 0,
                "access_count": 0,
            }
            line_state[line] = st

        access_cores = st["access_cores"]
        reader_cores = st["reader_cores"]
        writer_cores = st["writer_cores"]
        last_writer = st["last_writer_core"]
        remote_readers = set(reader_cores) - {c}
        remote_writers = set(writer_cores) - {c}
        remote_writer = last_writer is not None and last_writer != c
        fanout = max(
            len(remote_readers),
            len(remote_writers),
            int(st["owner_switch_count"]),
        )
        tr.ann["fanout_proxy"][idx] = float(fanout)
        tr.ann["fanout_bucket"][idx] = tk.log_count_bucket(fanout)

        if not access_cores:
            line_role = tk.LINE_ROLE_FIRST
        elif remote_writer:
            line_role = tk.LINE_ROLE_REMOTE
        elif len(writer_cores | ({c} if is_st else set())) >= 2:
            line_role = tk.LINE_ROLE_MULTIWRITER
        elif len(access_cores | {c}) >= 2:
            line_role = tk.LINE_ROLE_SHARED
        elif int(st["access_count"]) >= 8:
            line_role = tk.LINE_ROLE_HOT
        else:
            line_role = tk.LINE_ROLE_PRIVATE
        tr.ann["line_role"][idx] = line_role

        if is_ld and remote_writer:
            xcore = tk.XCORE_READ_AFTER_REMOTE_STORE
            coh = tk.COH_REMOTE_MODIFIED_READ
        elif is_st and remote_writer:
            xcore = tk.XCORE_STORE_AFTER_REMOTE_STORE
            coh = tk.COH_REMOTE_OWNER_TRANSFER
        elif is_st and remote_readers:
            xcore = tk.XCORE_STORE_TO_SHARED_LINE
            coh = tk.COH_STORE_INVALIDATE_READERS
        elif remote_writer:
            xcore = tk.XCORE_LAST_WRITER_OTHER
            coh = tk.COH_UNKNOWN
        elif last_writer == c and is_st:
            xcore = tk.XCORE_LAST_WRITER_SELF
            coh = tk.COH_LOCAL_OWNED_STORE
        elif remote_writers:
            xcore = tk.XCORE_RECENT_WRITER_OTHER
            coh = tk.COH_UNKNOWN
        elif remote_readers:
            xcore = tk.XCORE_RECENT_READER_OTHER
            coh = tk.COH_SHARED_LOAD if is_ld else tk.COH_UNKNOWN
        else:
            xcore = tk.XCORE_PRIVATE
            coh = tk.COH_LOCAL_PRIVATE
        tr.ann["xcore"][idx] = xcore
        tr.ann["coherence"][idx] = coh

        access_cores.add(c)
        st["access_count"] = int(st["access_count"]) + 1
        if is_ld:
            reader_cores.add(c)
        if is_st:
            if last_writer is not None and last_writer != c:
                st["owner_switch_count"] = int(st["owner_switch_count"]) + 1
            writer_cores.add(c)
            st["last_writer_core"] = c


def build_columnar_prefixes(trace: ColumnCoreTrace) -> None:
    n = len(trace)
    mem = trace.mem_mask()
    load = trace.bool_col("is_load")
    store = trace.bool_col("is_store")
    atomic = trace.bool_col("is_atomic")
    store_like = store | atomic
    branch = trace.bool_col("is_branch")
    cond = trace.bool_col("is_branch_cond")
    indirect = trace.bool_col("is_branch_indirect")
    path = trace.col("path_class", 0)
    i_path = trace.col("i_path_class", 0)
    dtlb_hit = trace.col("dtlb_hit", 1)
    itlb_hit = trace.col("itlb_hit", 1)
    mshr = trace.col("d_mshr_depth", 0)
    coh = trace.col("coh_oracle", 0)
    mispred = trace.col("mispredicted", 0)
    head = trace.ann.get("macro_head", np.ones((n,), dtype=np.int8)).astype(bool)

    prefix_defs = {
        "instr_retired": head,
        "fetch_groups": head,
        "branch_count": branch,
        "cond_branch_count": branch & cond,
        "indirect_branch_count": branch & indirect,
        "loads": load,
        "stores": store,
        "atomics": atomic,
        "mem_ops": mem,
        "branch_miss": branch & (mispred.astype(np.int64) != 0),
        "l1d_ld_miss": load & (path >= PC_L2),
        "l1d_st_miss": store_like & (path >= PC_L2),
        "l2_ld_miss": load & (path >= 2),
        "l2_st_miss": store_like & (path >= 2),
        "l1i_miss": head & (i_path >= PC_L2),
        "llc_miss": mem & (path >= PC_DRAM),
        "dtlb_miss": mem & (dtlb_hit.astype(np.int64) == 0),
        "itlb_miss": head & (itlb_hit.astype(np.int64) == 0),
        "inv_recv": mem & np.isin(coh, list(COH_REMOTE)),
    }
    for key, arr in prefix_defs.items():
        trace.prefix[key] = _prefix_count(arr)
    trace.prefix["mshr_sum"] = _prefix_sum(np.where(mem, mshr, 0))

    # Summary/dedup helper prefixes.
    rd = trace.ann.get("rd_bucket", np.full((n,), tk.RD_COLD))
    st = trace.ann.get("stride_bucket", np.full((n,), tk.ST_FIRST))
    op = trace.col("op_class", 0)
    is_fp = trace.bool_col("is_fp")
    is_int = trace.bool_col("is_int")
    is_simd = trace.bool_col("is_simd")
    is_ser = trace.bool_col("is_serialize")
    summary_bools = {
        "op_int_alu_ratio": (~mem) & (~branch) & ((op == 1) | is_int),
        "op_int_mul_ratio": (~mem) & (~branch) & (op == 2),
        "op_int_divmod_ratio": (~mem) & (~branch) & (op == 3),
        "op_fp_alu_ratio": (~mem) & (~branch)
            & (np.isin(op, list(FP_ALU_CLASSES)) | is_fp),
        "op_fp_mul_fma_ratio": (~mem) & (~branch)
            & np.isin(op, list(FP_MUL_FMA_CLASSES)),
        "op_fp_divsqrt_ratio": (~mem) & (~branch)
            & np.isin(op, list(FP_DIVSQRT_CLASSES)),
        "op_simd_ratio": (~mem) & (~branch)
            & (np.isin(op, list(SIMD_CLASSES)) | is_simd),
        "op_load_ratio": load,
        "op_store_ratio": store,
        "op_cond_branch_ratio": cond,
        "op_indirect_branch_ratio": indirect,
        "op_atomic_fence_sys_ratio": atomic | is_ser | (op == 88),
        "load_rd_hot_count": load & np.isin(rd, [tk.RD_LE8, tk.RD_LE64]),
        "load_rd_cold_count": load & np.isin(rd, [tk.RD_COLD, tk.RD_FAR]),
        "store_rd_hot_count": store & np.isin(rd, [tk.RD_LE8, tk.RD_LE64]),
        "store_rd_cold_count": store & np.isin(rd, [tk.RD_COLD, tk.RD_FAR]),
        "stream_stride_count": mem & np.isin(
            st, [tk.ST_P1, tk.ST_M1, tk.ST_P2_8, tk.ST_M2_8]),
        "large_stride_count": mem & np.isin(
            st, [tk.ST_P9_64, tk.ST_M9_64, tk.ST_LARGE]),
    }
    for key, arr in summary_bools.items():
        trace.prefix[key] = _prefix_count(arr)
    trace.prefix["seen8"] = _prefix_count(trace.ann.get("seen8", np.zeros(n)))
    trace.prefix["seen64"] = _prefix_count(trace.ann.get("seen64", np.zeros(n)))
    trace.prefix["recent_ws64_sum"] = _prefix_sum(
        trace.ann.get("recent_ws64", np.zeros(n)))

    dep_count = np.zeros((n,), dtype=np.int32)
    dep_short = np.zeros((n,), dtype=np.int32)
    dep_sum = np.zeros((n,), dtype=np.float32)
    for i, ds in enumerate(trace.producer_dists):
        vals = []
        for d in ds or []:
            try:
                di = int(d)
            except Exception:
                continue
            if di < 0:
                continue
            vals.append(di)
        if vals:
            dep_count[i] = len(vals)
            dep_short[i] = sum(1 for x in vals if x <= 4)
            dep_sum[i] = float(sum(vals))
    trace.prefix["dep_count"] = _prefix_count(dep_count)
    trace.prefix["dep_short"] = _prefix_count(dep_short)
    trace.prefix["dep_sum"] = _prefix_sum(dep_sum)


def columnar_pmu_from_span(trace: ColumnCoreTrace, start: int, end: int,
                           tick_per_cycle: int) -> Optional[dict]:
    start = int(start)
    end = int(end)
    if end - start < 2:
        return None
    ticks = trace.ticks
    if start < 0 or end > len(trace):
        return None
    cycles = (float(ticks[end - 1]) - float(ticks[start])) / float(tick_per_cycle)
    if cycles <= 0:
        return None

    def cnt(k: str) -> float:
        return _span_sum(trace.prefix[k], start, end)

    instr_retired = cnt("instr_retired")
    branch_count = cnt("branch_count")
    cond_branch_count = cnt("cond_branch_count")
    indirect_branch_count = cnt("indirect_branch_count")
    loads = cnt("loads")
    stores = cnt("stores")
    atomics = cnt("atomics")
    mem_ops = cnt("mem_ops")
    branch_miss = cnt("branch_miss")
    l1d_ld_miss = cnt("l1d_ld_miss")
    l1d_st_miss = cnt("l1d_st_miss")
    l2_ld_miss = cnt("l2_ld_miss")
    l2_st_miss = cnt("l2_st_miss")
    l1i_miss = cnt("l1i_miss")
    llc_miss = cnt("llc_miss")
    dtlb_miss = cnt("dtlb_miss")
    itlb_miss = cnt("itlb_miss")
    inv_recv = cnt("inv_recv")
    mshr_sum = cnt("mshr_sum")

    def safe_div(a, b):
        return float(a) / float(b) if b > 0 else 0.0

    uops = end - start
    cpi_uop = safe_div(cycles, uops)
    cpi_macro = safe_div(cycles, instr_retired) if instr_retired > 0 else float("nan")
    return {
        "cycles": cycles,
        "instr_retired": instr_retired,
        "uops": uops,
        "t_start_tick": float(ticks[start]),
        "cpi_uop": cpi_uop,
        "cpi_macro": cpi_macro,
        "cpi": cpi_macro,
        "branch_miss": float(branch_miss),
        "l1d_ld_miss": float(l1d_ld_miss),
        "l1d_st_miss": float(l1d_st_miss),
        "l2_ld_miss": float(l2_ld_miss),
        "l2_st_miss": float(l2_st_miss),
        "l1i_miss": float(l1i_miss),
        "llc_miss": float(llc_miss),
        "mpki_br": 1000.0 * safe_div(branch_miss, max(instr_retired, 1)),
        "branch_mispred_frac": safe_div(branch_miss, max(branch_count, 1)),
        "mr_l1d_ld": safe_div(l1d_ld_miss, max(loads, 1)),
        "mr_l1d_st": safe_div(l1d_st_miss, max(stores, 1)),
        "mr_l1i": safe_div(l1i_miss, max(cnt("fetch_groups"), 1)),
        "mr_llc": safe_div(llc_miss, max(mem_ops, 1)),
        "dtlb_miss": float(dtlb_miss),
        "itlb_miss": float(itlb_miss),
        "inv_recv": float(inv_recv),
        "mshr_avg": safe_div(mshr_sum, max(mem_ops, 1)),
        "_denoms": {
            "branch_count": branch_count,
            "cond_branch_count": cond_branch_count,
            "indirect_branch_count": indirect_branch_count,
            "loads": loads,
            "stores": stores,
            "atomics": atomics,
            "fetch_groups": cnt("fetch_groups"),
            "mem_ops": mem_ops,
        },
    }


def _pc_entropy_norm(pcs: List[int]) -> float:
    if not pcs:
        return 0.0
    counts = defaultdict(int)
    for pc in pcs:
        counts[pc] += 1
    n = float(len(pcs))
    ent = 0.0
    for c in counts.values():
        p = c / n
        ent -= p * math.log2(max(p, 1e-12))
    return ent / max(math.log2(max(len(counts), 2)), 1.0)


def _mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


INT_ALU_CLASSES = {1}
INT_MUL_CLASSES = {2}
INT_DIVMOD_CLASSES = {3}
FP_ALU_CLASSES = {4, 5, 6, 10}
FP_MUL_FMA_CLASSES = {7, 8}
FP_DIVSQRT_CLASSES = {9, 11}
SIMD_CLASSES = set(range(12, 56))
SIMD_DIVSQRT_CLASSES = {23, 24, 29}


def _safe_ratio(num: float, den: float) -> float:
    return float(num) / float(den) if den > 0 else 0.0


def _percentile(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    return float(np.percentile(np.asarray(xs, dtype=np.float32), p))


def _log2p1(x: float) -> float:
    return math.log2(max(0.0, float(x)) + 1.0)


def _macro_pc_value(rec: dict) -> int:
    return int(rec.get("macro_pc", rec.get("micro_pc", 0)) or 0)


def _is_simd_op(rec: dict, oc: Optional[int] = None) -> bool:
    if rec.get("is_simd"):
        return True
    if oc is None:
        oc = tk.opclass_id(rec)
    return int(oc) in SIMD_CLASSES


def _is_divsqrt_op(rec: dict, oc: Optional[int] = None) -> bool:
    if oc is None:
        oc = tk.opclass_id(rec)
    oc = int(oc)
    return oc in INT_DIVMOD_CLASSES or oc in FP_DIVSQRT_CLASSES \
        or oc in SIMD_DIVSQRT_CLASSES


def _op_mix_bucket(rec: dict) -> Optional[str]:
    """Return one main arithmetic/control op-mix bucket for the uop."""
    oc = tk.opclass_id(rec)
    if rec.get("is_branch"):
        return None
    if is_mem_rec(rec):
        return None
    if _is_simd_op(rec, oc):
        return "op_simd_ratio"
    if oc in INT_MUL_CLASSES:
        return "op_int_mul_ratio"
    if oc in INT_DIVMOD_CLASSES:
        return "op_int_divmod_ratio"
    if oc in FP_MUL_FMA_CLASSES:
        return "op_fp_mul_fma_ratio"
    if oc in FP_DIVSQRT_CLASSES:
        return "op_fp_divsqrt_ratio"
    if oc in FP_ALU_CLASSES or rec.get("is_fp"):
        return "op_fp_alu_ratio"
    if oc in INT_ALU_CLASSES or rec.get("is_int"):
        return "op_int_alu_ratio"
    return None


def _normalized_entropy(vals: List[int]) -> float:
    if not vals:
        return 0.0
    counts = defaultdict(int)
    for v in vals:
        counts[v] += 1
    if len(counts) <= 1:
        return 0.0
    n = float(len(vals))
    ent = 0.0
    for c in counts.values():
        p = c / n
        ent -= p * math.log2(max(p, 1e-12))
    return ent / max(math.log2(len(counts)), 1.0)


def build_core_summary_tokens(win: List[dict]) -> Tuple[List[str], dict]:
    n_uop = max(1, len(win))
    mem_n = load_n = store_n = 0
    op_counts = {k: 0 for k in tk.SUMMARY_FEATURE_KEYS
                 if k.startswith("op_")}
    lines = set()
    pages = set()
    seen8 = seen64 = 0
    ws64 = []
    pcs = []
    bb_lens = []
    cur_bb_len = 0
    dep_total = dep_short = 0
    dep_dists = []
    load_hot = load_cold = 0
    store_hot = store_cold = 0
    stream_stride = large_stride = 0
    addr_dep_load = 0
    raw_depths: List[int] = []
    load_use_depths: List[int] = []
    div_use_depths: List[int] = []
    indirect_targets_by_pc: Dict[int, List[int]] = defaultdict(list)
    all_indirect_targets: List[int] = []

    raw_depth = [0] * len(win)
    load_use_depth = [0] * len(win)
    div_use_depth = [0] * len(win)

    for idx, rec in enumerate(win):
        pc = _macro_pc_value(rec)
        pcs.append(pc)
        cur_bb_len += 1
        bucket = _op_mix_bucket(rec)
        if bucket is not None:
            op_counts[bucket] = op_counts.get(bucket, 0) + 1
        if rec.get("is_load"):
            op_counts["op_load_ratio"] = op_counts.get("op_load_ratio", 0) + 1
        if rec.get("is_store"):
            op_counts["op_store_ratio"] = op_counts.get("op_store_ratio", 0) + 1
        if rec.get("is_branch_cond"):
            op_counts["op_cond_branch_ratio"] = (
                op_counts.get("op_cond_branch_ratio", 0) + 1
            )
        if rec.get("is_branch_indirect"):
            op_counts["op_indirect_branch_ratio"] = (
                op_counts.get("op_indirect_branch_ratio", 0) + 1
            )
        if rec.get("is_atomic") or rec.get("is_serialize") \
                or tk.opclass_id(rec) == 88:
            op_counts["op_atomic_fence_sys_ratio"] = (
                op_counts.get("op_atomic_fence_sys_ratio", 0) + 1
            )

        max_raw = 0
        max_load_use = 0
        max_div_use = 0
        consumes_load = False
        for d in (rec.get("producer_dists") or []):
            try:
                di = int(d)
            except Exception:
                continue
            if di < 0:
                continue
            dep_total += 1
            dep_dists.append(float(di))
            if di <= 4:
                dep_short += 1
            if di <= 0:
                continue
            prod_idx = idx - di
            if 0 <= prod_idx < idx:
                prod = win[prod_idx]
                max_raw = max(max_raw, raw_depth[prod_idx] + 1)
                if prod.get("is_load") or load_use_depth[prod_idx] > 0:
                    consumes_load = True
                    max_load_use = max(
                        max_load_use, load_use_depth[prod_idx] + 1)
                if _is_divsqrt_op(prod) or div_use_depth[prod_idx] > 0:
                    max_div_use = max(
                        max_div_use, div_use_depth[prod_idx] + 1)
        raw_depth[idx] = max_raw
        load_use_depth[idx] = max_load_use
        div_use_depth[idx] = max_div_use
        raw_depths.append(max_raw)
        load_use_depths.append(max_load_use)
        div_use_depths.append(max_div_use)
        if rec.get("is_load") and consumes_load:
            addr_dep_load += 1

        if int(rec.get("is_branch", 0) or 0):
            if int(rec.get("is_branch_indirect", 0) or 0):
                target = (
                    _macro_pc_value(win[idx + 1]) if idx + 1 < len(win)
                    else pc
                )
                indirect_targets_by_pc[pc].append(target)
                all_indirect_targets.append(target)
            bb_lens.append(cur_bb_len)
            cur_bb_len = 0
        if is_mem_rec(rec):
            mem_n += 1
            is_load = bool(rec.get("is_load"))
            is_store = bool(rec.get("is_store"))
            load_n += 1 if is_load else 0
            store_n += 1 if is_store else 0
            seen8 += int(rec.get("_seen_line_8k", 0) or 0)
            seen64 += int(rec.get("_seen_line_64k", 0) or 0)
            ws64.append(float(rec.get("_recent_ws_64k", 0) or 0))
            line = functional_cacheline(rec)
            if line is not None:
                lines.add(line)
                pages.add(line >> 6)
            rd = tk.rd_bucket(rec)
            if is_load:
                load_hot += 1 if rd in (tk.RD_LE8, tk.RD_LE64) else 0
                load_cold += 1 if rd in (tk.RD_COLD, tk.RD_FAR) else 0
            if is_store:
                store_hot += 1 if rd in (tk.RD_LE8, tk.RD_LE64) else 0
                store_cold += 1 if rd in (tk.RD_COLD, tk.RD_FAR) else 0
            st = tk.stride_bucket(rec)
            if st in (tk.ST_P1, tk.ST_M1, tk.ST_P2_8, tk.ST_M2_8):
                stream_stride += 1
            if st in (tk.ST_P9_64, tk.ST_M9_64, tk.ST_LARGE):
                large_stride += 1
    if cur_bb_len:
        bb_lens.append(cur_bb_len)

    mem_den = max(mem_n, 1)
    dep_den = max(dep_total, 1)
    indirect_entropy = 0.0
    indirect_switch_num = 0
    indirect_switch_den = 0
    fanouts = []
    for targets in indirect_targets_by_pc.values():
        indirect_entropy += len(targets) * _normalized_entropy(targets)
        fanouts.append(float(len(set(targets))))
        for a, b in zip(targets, targets[1:]):
            indirect_switch_num += 1 if a != b else 0
        indirect_switch_den += max(len(targets) - 1, 0)
    if all_indirect_targets:
        indirect_entropy /= float(len(all_indirect_targets))
    target_counts = defaultdict(int)
    for t in all_indirect_targets:
        target_counts[t] += 1
    indirect_top_ratio = (
        max(target_counts.values()) / float(len(all_indirect_targets))
        if all_indirect_targets else 0.0
    )

    summary = {
        **{k: op_counts.get(k, 0) / float(n_uop)
           for k in op_counts.keys()},
        "load_rd_hot_ratio": _safe_ratio(load_hot, max(load_n, 1)),
        "load_rd_cold_ratio": _safe_ratio(load_cold, max(load_n, 1)),
        "store_rd_hot_ratio": _safe_ratio(store_hot, max(store_n, 1)),
        "store_rd_cold_ratio": _safe_ratio(store_cold, max(store_n, 1)),
        "stream_stride_ratio": _safe_ratio(stream_stride, mem_den),
        "large_stride_ratio": _safe_ratio(large_stride, mem_den),
        "addr_dep_load_ratio": _safe_ratio(addr_dep_load, max(load_n, 1)),
        "short_dep_ratio": _safe_ratio(dep_short, dep_den),
        "dep_dist_mean_log": _log2p1(_mean(dep_dists)),
        "raw_chain_depth_p95": _percentile([float(x) for x in raw_depths], 95),
        "raw_chain_depth_max_log": _log2p1(max(raw_depths) if raw_depths else 0),
        "load_use_chain_p95": _percentile(
            [float(x) for x in load_use_depths if x > 0], 95),
        "div_use_chain_p95": _percentile(
            [float(x) for x in div_use_depths if x > 0], 95),
        "indirect_target_entropy": indirect_entropy,
        "indirect_target_fanout_log": _log2p1(_mean(fanouts)),
        "indirect_target_switch_rate": _safe_ratio(
            indirect_switch_num, indirect_switch_den),
        "indirect_top_target_ratio": indirect_top_ratio,
        "distinct_lines": len(lines),
        "distinct_pages": len(pages),
        "seen_line_rate_8k": seen8 / float(mem_den),
        "seen_line_rate_64k": seen64 / float(mem_den),
        "recent_ws_size_64k": _mean(ws64),
        "pc_entropy": _pc_entropy_norm(pcs),
        "basic_block_len_mean": _mean([float(x) for x in bb_lens]),
    }
    for k in tk.SUMMARY_FEATURE_KEYS:
        summary.setdefault(k, 0.0)
    return tk.core_summary_tokens(summary), summary


def build_columnar_core_summary(trace: ColumnCoreTrace,
                                start: int, end: int) -> dict:
    """Fast summary used for columnar dedup plans.

    It keeps the same key schema as build_core_summary_tokens, but computes the
    expensive dependency-chain/indirect-target refinements conservatively from
    prefixable counters. Selected samples still materialize exact UOP fields.
    """
    start = int(start)
    end = int(end)
    n_uop = max(1, end - start)
    mem_n = max(_span_sum(trace.prefix["mem_ops"], start, end), 1.0)
    load_n = max(_span_sum(trace.prefix["loads"], start, end), 1.0)
    store_n = max(_span_sum(trace.prefix["stores"], start, end), 1.0)
    dep_n = max(_span_sum(trace.prefix["dep_count"], start, end), 1.0)

    def ratio_count(k: str, den: float = n_uop) -> float:
        return float(_span_sum(trace.prefix[k], start, end)) / float(max(den, 1.0))

    line = trace.line_keys()[start:end]
    mem = trace.mem_mask()[start:end]
    lines = line[mem & (line != 0)]
    pages = lines >> 6 if len(lines) else lines
    pc = trace.col("macro_pc", 0)[start:end]
    if pc.size == 0:
        pc_entropy = 0.0
    else:
        vals, counts = np.unique(pc, return_counts=True)
        if len(vals) <= 1:
            pc_entropy = 0.0
        else:
            p = counts.astype(np.float64) / float(max(counts.sum(), 1))
            ent = float(-(p * np.log2(np.maximum(p, 1e-12))).sum())
            pc_entropy = ent / max(math.log2(len(vals)), 1.0)

    dep_sum = _span_sum(trace.prefix["dep_sum"], start, end)
    dep_mean_log = _log2p1(dep_sum / dep_n)
    recent_ws = _span_sum(trace.prefix["recent_ws64_sum"], start, end) / mem_n
    summary = {
        "op_int_alu_ratio": ratio_count("op_int_alu_ratio"),
        "op_int_mul_ratio": ratio_count("op_int_mul_ratio"),
        "op_int_divmod_ratio": ratio_count("op_int_divmod_ratio"),
        "op_fp_alu_ratio": ratio_count("op_fp_alu_ratio"),
        "op_fp_mul_fma_ratio": ratio_count("op_fp_mul_fma_ratio"),
        "op_fp_divsqrt_ratio": ratio_count("op_fp_divsqrt_ratio"),
        "op_simd_ratio": ratio_count("op_simd_ratio"),
        "op_load_ratio": ratio_count("op_load_ratio"),
        "op_store_ratio": ratio_count("op_store_ratio"),
        "op_cond_branch_ratio": ratio_count("op_cond_branch_ratio"),
        "op_indirect_branch_ratio": ratio_count("op_indirect_branch_ratio"),
        "op_atomic_fence_sys_ratio": ratio_count("op_atomic_fence_sys_ratio"),
        "load_rd_hot_ratio": ratio_count("load_rd_hot_count", load_n),
        "load_rd_cold_ratio": ratio_count("load_rd_cold_count", load_n),
        "store_rd_hot_ratio": ratio_count("store_rd_hot_count", store_n),
        "store_rd_cold_ratio": ratio_count("store_rd_cold_count", store_n),
        "stream_stride_ratio": ratio_count("stream_stride_count", mem_n),
        "large_stride_ratio": ratio_count("large_stride_count", mem_n),
        "addr_dep_load_ratio": 0.0,
        "short_dep_ratio": ratio_count("dep_short", dep_n),
        "dep_dist_mean_log": dep_mean_log,
        "raw_chain_depth_p95": 0.0,
        "raw_chain_depth_max_log": 0.0,
        "load_use_chain_p95": 0.0,
        "div_use_chain_p95": 0.0,
        "indirect_target_entropy": 0.0,
        "indirect_target_fanout_log": 0.0,
        "indirect_target_switch_rate": 0.0,
        "indirect_top_target_ratio": 0.0,
        "distinct_lines": int(len(np.unique(lines))) if len(lines) else 0,
        "distinct_pages": int(len(np.unique(pages))) if len(pages) else 0,
        "seen_line_rate_8k": ratio_count("seen8", mem_n),
        "seen_line_rate_64k": ratio_count("seen64", mem_n),
        "recent_ws_size_64k": recent_ws,
        "pc_entropy": pc_entropy,
        "basic_block_len_mean": 0.0,
    }
    for k in tk.SUMMARY_FEATURE_KEYS:
        summary.setdefault(k, 0.0)
    return summary


def _store_slot_mask(rec: dict) -> int:
    v = int(rec.get("vaddr", 0) or 0)
    size = int(rec.get("size", 0) or 0)
    if v == 0 or size <= 0:
        return 0
    start = max(0, min(7, (v & 63) // 8))
    end = max(0, min(7, ((v & 63) + size - 1) // 8))
    mask = 0
    for b in range(start, end + 1):
        mask |= 1 << b
    return mask


def _rate_level_token(name: str, value: float) -> str:
    return f"<G_{name}_{tk.global_level_bucket(value)}>"


def build_cross_core_features(
    per_core_windows: Dict[int, Tuple[List[dict], dict]],
    cores: List[int],
) -> Tuple[List[str], List[List[float]]]:
    """Functional-only cross-core summary for v9 side tensor/global tokens."""
    n_core = max(len(cores), 1)
    uops_total = sum(len(per_core_windows[c][0]) for c in cores)
    max_uops = max([len(per_core_windows[c][0]) for c in cores] or [1])

    events = []
    core_stats = {
        c: {
            "mem": 0, "loads": 0, "stores": 0, "random_loads": 0,
            "shared_store": 0, "shared_load": 0, "mw_store": 0,
            "lines": set(), "pages": set(),
        }
        for c in cores
    }
    access_cores: Dict[int, set] = defaultdict(set)
    store_cores: Dict[int, set] = defaultdict(set)
    store_masks: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
    global_lines = set()
    global_pages = set()
    large_stride = 0
    stream_stride = 0
    cold_or_large_loads = 0
    mem_total = 0
    load_total = 0
    store_total = 0

    for c in cores:
        win, _pmu = per_core_windows[c]
        for idx, rec in enumerate(win):
            if not is_mem_rec(rec):
                continue
            line = functional_cacheline(rec)
            if line is None:
                continue
            is_ld = int(rec.get("is_load", 0) or 0)
            is_st = int(rec.get("is_store", 0) or rec.get("is_atomic", 0) or 0)
            st_bucket = tk.stride_bucket(rec)
            rd_bucket = tk.rd_bucket(rec)
            mem_total += 1
            load_total += 1 if is_ld else 0
            store_total += 1 if is_st else 0
            large_stride += 1 if st_bucket in (
                tk.ST_P9_64, tk.ST_M9_64, tk.ST_LARGE
            ) else 0
            stream_stride += 1 if st_bucket in (
                tk.ST_P1, tk.ST_M1, tk.ST_P2_8, tk.ST_M2_8
            ) else 0
            is_random_load = bool(is_ld and (
                st_bucket in (tk.ST_P9_64, tk.ST_M9_64, tk.ST_LARGE)
                or rd_bucket in (tk.RD_COLD, tk.RD_FAR)
            ))
            cold_or_large_loads += 1 if is_random_load else 0
            access_cores[line].add(c)
            global_lines.add(line)
            global_pages.add(line >> 6)
            core_stats[c]["mem"] += 1
            core_stats[c]["loads"] += 1 if is_ld else 0
            core_stats[c]["stores"] += 1 if is_st else 0
            core_stats[c]["random_loads"] += 1 if is_random_load else 0
            core_stats[c]["lines"].add(line)
            core_stats[c]["pages"].add(line >> 6)
            if is_st:
                store_cores[line].add(c)
                store_masks[line][c] |= _store_slot_mask(rec)
            events.append((
                int(rec.get("micro_seq", idx) or idx), c, line, is_ld, is_st
            ))

    shared_lines = {line for line, cs in access_cores.items() if len(cs) >= 2}
    multi_writer_lines = {
        line for line, cs in store_cores.items() if len(cs) >= 2
    }
    shared_store_count = 0
    multi_writer_store_count = 0
    shared_load_count = 0
    for _ms, c, line, is_ld, is_st in events:
        if is_ld and line in shared_lines:
            shared_load_count += 1
            core_stats[c]["shared_load"] += 1
        if is_st and line in shared_lines:
            shared_store_count += 1
            core_stats[c]["shared_store"] += 1
        if is_st and line in multi_writer_lines:
            multi_writer_store_count += 1
            core_stats[c]["mw_store"] += 1

    events.sort(key=lambda x: (x[0], x[1]))
    last_store_core: Dict[int, int] = {}
    owner_switch = 0
    recent_cores: Dict[int, set] = defaultdict(set)
    fanout_sum = 0.0
    fanout_max = 0
    for _ms, c, line, is_ld, is_st in events:
        if is_st:
            prev_c = last_store_core.get(line)
            if prev_c is not None and prev_c != c:
                owner_switch += 1
            last_store_core[line] = c
            fanout = len(recent_cores[line] - {c})
            fanout_sum += float(fanout)
            fanout_max = max(fanout_max, fanout)
            recent_cores[line] = {c}
        elif is_ld:
            recent_cores[line].add(c)

    disjoint_pairs = 0
    overlap_pairs = 0
    all_pairs = 0
    for by_core in store_masks.values():
        cs = list(by_core.keys())
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                all_pairs += 1
                if by_core[cs[i]] & by_core[cs[j]]:
                    overlap_pairs += 1
                else:
                    disjoint_pairs += 1

    def ratio(a: float, b: float) -> float:
        return float(a) / float(b) if b > 0 else 0.0

    max_writer = max([len(cs) for cs in store_cores.values()] or [0])
    pair_num = sum((len(cs) * (len(cs) - 1)) / 2.0
                   for cs in store_cores.values())
    pair_den = max((n_core * (n_core - 1)) / 2.0, 1.0)
    shared_store_rate = ratio(shared_store_count, store_total)
    pairwise_pressure = min(1.0, ratio(pair_num, pair_den))
    random_pressure = ratio(cold_or_large_loads, max(load_total, 1))

    global_values = {
        "log1p_active_cores": math.log1p(n_core),
        "log1p_uops_window_total": math.log1p(uops_total),
        "log1p_global_distinct_data_lines": math.log1p(len(global_lines)),
        "log1p_global_distinct_data_pages": math.log1p(len(global_pages)),
        "shared_store_rate": shared_store_rate,
        "multi_writer_line_frac": ratio(len(multi_writer_lines), len(global_lines)),
        "max_writer_cores_per_line_log": math.log1p(max_writer),
        "writer_core_coverage": ratio(max_writer, n_core),
        "pairwise_writer_pressure": pairwise_pressure,
        "store_owner_switch_rate": ratio(owner_switch, store_total),
        "inval_fanout_proxy_mean": ratio(fanout_sum, store_total),
        "disjoint_store_slot_pair_rate": ratio(disjoint_pairs, all_pairs),
        "aggregate_load_density": ratio(load_total, uops_total),
        "aggregate_mem_density": ratio(mem_total, uops_total),
        "global_large_stride_rate": ratio(large_stride, mem_total),
        "random_access_pressure": random_pressure,
        "lines_per_kuop_global": 1000.0 * ratio(len(global_lines), uops_total),
        "pages_per_kuop_global": 1000.0 * ratio(len(global_pages), uops_total),
    }

    side_feats: List[List[float]] = []
    for c in cores:
        _win, pmu = per_core_windows[c]
        den = pmu.get("_denoms", {}) or {}
        st = core_stats[c]
        vals = dict(global_values)
        vals.update({
            "log1p_uops_core": math.log1p(float(pmu.get("uops", 0.0) or 0.0)),
            "log1p_instr_retired": math.log1p(
                float(pmu.get("instr_retired", 0.0) or 0.0)
            ),
            "core_fill_ratio": ratio(float(pmu.get("uops", 0.0) or 0.0), max_uops),
            "log1p_branch_count": math.log1p(den.get("branch_count", 0.0) or 0.0),
            "log1p_cond_branch_count": math.log1p(
                den.get("cond_branch_count", 0.0) or 0.0
            ),
            "log1p_indirect_branch_count": math.log1p(
                den.get("indirect_branch_count", 0.0) or 0.0
            ),
            "log1p_load_count": math.log1p(den.get("loads", 0.0) or 0.0),
            "log1p_store_count": math.log1p(den.get("stores", 0.0) or 0.0),
            "log1p_atomic_count": math.log1p(den.get("atomics", 0.0) or 0.0),
            "log1p_mem_ops": math.log1p(den.get("mem_ops", 0.0) or 0.0),
            "log1p_distinct_data_lines_core": math.log1p(len(st["lines"])),
            "log1p_distinct_data_pages_core": math.log1p(len(st["pages"])),
            "core_shared_store_rate": ratio(st["shared_store"], st["stores"]),
            "core_shared_load_rate": ratio(st["shared_load"], st["loads"]),
            "core_multi_writer_store_rate": ratio(st["mw_store"], st["stores"]),
            "core_random_load_density": ratio(st["random_loads"], max(len(per_core_windows[c][0]), 1)),
        })
        side_feats.append([float(vals.get(k, 0.0)) for k in tk.SIDE_FEATURE_KEYS])

    global_tokens = [
        f"<G_NCORE_{tk.global_ncore_bucket(n_core)}>",
        _rate_level_token("SHARED_WRITE", shared_store_rate),
        _rate_level_token("PAIRWISE_PRESSURE", pairwise_pressure),
        _rate_level_token("RANDOM_LOAD", random_pressure),
    ]
    return global_tokens, side_feats


def _core_id_from_path(path: str) -> Optional[int]:
    m = CORE_RE.search(path)
    if not m:
        return None
    # gem5 stdlib may omit the numeric suffix for single-core runs:
    # board.processor.cores.core.tao_trace...
    return int(m.group(1) or 0)


def load_core_files(trace_dir: str) -> Dict[int, dict]:
    """返回 {core_id: {'rec': path, 'lab': path}}。"""
    out: Dict[int, dict] = defaultdict(dict)
    for p in glob.glob(os.path.join(trace_dir, "*.aligned.parquet")):
        c = _core_id_from_path(p)
        if c is not None:
            out[c]["aligned"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.records.micro.jsonl")):
        c = _core_id_from_path(p)
        if c is not None:
            out[c]["rec"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.labels.micro.jsonl")):
        c = _core_id_from_path(p)
        if c is not None:
            out[c]["lab"] = p
    keep = {}
    for c, v in out.items():
        if "aligned" in v:
            keep[c] = {"aligned": v["aligned"]}
        elif "rec" in v and "lab" in v:
            keep[c] = {"rec": v["rec"], "lab": v["lab"]}
    return keep


def read_jsonl(path: str, max_rows: int = 0) -> List[dict]:
    rows = []
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                pass
            if max_rows and len(rows) >= max_rows:
                break
    return rows


def read_aligned_parquet(path: str, max_rows: int = 0) -> List[dict]:
    """Read an aligned parquet trace into a list of per-uop dicts.

    Avoids ``RecordBatch.to_pylist`` (which has a per-cell Python conversion
    cost that scales with the number of columns). Instead we read each
    column to numpy once and assemble dicts by indexing the numpy arrays.
    When ``max_rows`` is positive, stop after reading that many rows. This is
    intended for bounded diagnostics; production/eval callers use the default.
    """
    if pq is None:
        raise ImportError("pyarrow is required to read aligned parquet traces")
    pf = pq.ParquetFile(path)
    rows: List[dict] = []
    for batch in pf.iter_batches(columns=ALIGNED_PARQUET_COLS,
                                 batch_size=65536):
        n = batch.num_rows
        if n == 0:
            continue
        col_lists: Dict[str, list] = {}
        pd_lists: Optional[list] = None
        pc_lists: Optional[list] = None
        for name in batch.schema.names:
            col = batch.column(name)
            if name == "producer_dists":
                pd_lists = col.to_pylist()
                continue
            if name == "producer_classes":
                pc_lists = col.to_pylist()
                continue
            col_lists[name] = col.to_numpy(zero_copy_only=False).tolist()
        col_names = list(col_lists.keys())
        col_seqs = [col_lists[k] for k in col_names]
        for i in range(n):
            if max_rows and len(rows) >= max_rows:
                return rows
            row = {k: col_seqs[j][i] for j, k in enumerate(col_names)}
            row["producer_dists"] = pd_lists[i] if pd_lists is not None else []
            row["producer_classes"] = pc_lists[i] if pc_lists is not None else []
            row["_commit_tick"] = row.get("commit_tick", 0)
            row["_mispredicted"] = row.get("mispredicted", 0)
            rows.append(row)
    return rows


class ColumnCoreTrace:
    """Columnar aligned-parquet core trace for direct TQ tensor-cache builds."""

    def __init__(self, cols: Dict[str, np.ndarray],
                 producer_dists: List[list],
                 producer_classes: List[list]):
        self.cols = cols
        self.producer_dists = producer_dists
        self.producer_classes = producer_classes
        self.n = int(len(cols.get("commit_tick", [])))
        self.ann: Dict[str, np.ndarray] = {}
        self.prefix: Dict[str, np.ndarray] = {}

    def __len__(self) -> int:
        return self.n

    def col(self, name: str, default: int = 0) -> np.ndarray:
        arr = self.cols.get(name)
        if arr is not None:
            return arr
        return np.full((self.n,), default, dtype=np.int64)

    @property
    def ticks(self) -> np.ndarray:
        return self.col("commit_tick", 0).astype(np.int64, copy=False)

    def bool_col(self, name: str) -> np.ndarray:
        return self.col(name, 0).astype(bool, copy=False)

    def line_keys(self) -> np.ndarray:
        cached = self.ann.get("line_key")
        if cached is not None:
            return cached
        v = self.col("vaddr", 0).astype(np.int64, copy=False)
        cl = self.col("cacheline_addr", 0).astype(np.int64, copy=False)
        line = np.where(v != 0, v >> 6, cl)
        line = line.astype(np.int64, copy=False)
        self.ann["line_key"] = line
        return line

    def shared_line_keys(self) -> np.ndarray:
        cached = self.ann.get("shared_line_key")
        if cached is not None:
            return cached
        src = None
        for name in ("cacheline_paddr", "cacheline_addr", "paddr", "vaddr"):
            arr = self.cols.get(name)
            if arr is not None:
                src = arr.astype(np.int64, copy=False)
                break
        if src is None:
            line = np.zeros((self.n,), dtype=np.int64)
        else:
            line = (src & ~np.int64(63)).astype(np.int64, copy=False)
        self.ann["shared_line_key"] = line
        return line

    def mem_mask(self) -> np.ndarray:
        cached = self.ann.get("mem_mask")
        if cached is not None:
            return cached.astype(bool, copy=False)
        mem = self.bool_col("is_load") | self.bool_col("is_store") \
            | self.bool_col("is_atomic")
        self.ann["mem_mask"] = mem
        return mem

    def store_mask(self) -> np.ndarray:
        cached = self.ann.get("store_mask")
        if cached is not None:
            return cached.astype(bool, copy=False)
        st = self.bool_col("is_store") | self.bool_col("is_atomic")
        self.ann["store_mask"] = st
        return st

    def _scalar(self, name: str, idx: int, default: int = 0):
        arr = self.cols.get(name)
        if arr is None:
            return default
        v = arr[int(idx)]
        try:
            return int(v)
        except Exception:
            return v.item() if hasattr(v, "item") else v

    def _ann_scalar(self, name: str, idx: int, default=0):
        arr = self.ann.get(name)
        if arr is None:
            return default
        return arr[int(idx)]

    def rec(self, idx: int) -> dict:
        i = int(idx)
        rec = {k: self._scalar(k, i, 0) for k in self.cols.keys()}
        rec["producer_dists"] = (
            self.producer_dists[i] if i < len(self.producer_dists) else []
        )
        rec["producer_classes"] = (
            self.producer_classes[i] if i < len(self.producer_classes) else []
        )
        rec["_commit_tick"] = rec.get("commit_tick", 0)
        rec["_mispredicted"] = rec.get("mispredicted", 0)
        rec["_rd_bucket"] = int(self._ann_scalar("rd_bucket", i, tk.RD_COLD))
        rec["_stride_bucket"] = int(
            self._ann_scalar("stride_bucket", i, tk.ST_FIRST))
        rec["_macro_pos_bucket"] = int(
            self._ann_scalar("macro_pos_bucket", i, tk.MACRO_POS_UNKNOWN))
        rec["_seen_line_8k"] = int(self._ann_scalar("seen8", i, 0))
        rec["_seen_line_64k"] = int(self._ann_scalar("seen64", i, 0))
        rec["_recent_ws_64k"] = int(self._ann_scalar("recent_ws64", i, 0))
        rec["_line_role_bucket"] = int(
            self._ann_scalar("line_role", i, tk.LINE_ROLE_NONMEM))
        rec["_xcore_mem_bucket"] = int(
            self._ann_scalar("xcore", i, tk.XCORE_NONMEM))
        rec["_coherence_bucket"] = int(
            self._ann_scalar("coherence", i, tk.COH_NONMEM))
        rec["_fanout_bucket"] = int(self._ann_scalar("fanout_bucket", i, 0))
        rec["_fanout_proxy"] = float(self._ann_scalar("fanout_proxy", i, 0.0))
        return rec

    def window_records(self, start: int, end: int) -> List[dict]:
        return [self.rec(i) for i in range(int(start), int(end))]


def read_aligned_parquet_columnar(path: str) -> ColumnCoreTrace:
    """Read aligned parquet into column arrays without per-UOP dicts."""
    if pq is None:
        raise ImportError("pyarrow is required to read aligned parquet traces")
    table = pq.read_table(path, columns=ALIGNED_PARQUET_COLS)
    cols: Dict[str, np.ndarray] = {}
    producer_dists: List[list] = []
    producer_classes: List[list] = []
    for name in table.column_names:
        col = table[name].combine_chunks()
        if name == "producer_dists":
            producer_dists = col.to_pylist()
            continue
        if name == "producer_classes":
            producer_classes = col.to_pylist()
            continue
        cols[name] = np.asarray(col.to_numpy(zero_copy_only=False))
    ticks = cols.get("commit_tick")
    if ticks is not None:
        mask = np.asarray(ticks) > 0
        if not bool(mask.all()):
            for k, arr in list(cols.items()):
                cols[k] = np.asarray(arr)[mask]
            keep_idx = np.nonzero(mask)[0].tolist()
            if producer_dists:
                producer_dists = [producer_dists[i] for i in keep_idx]
            if producer_classes:
                producer_classes = [producer_classes[i] for i in keep_idx]
    n = len(next(iter(cols.values()))) if cols else 0
    if not producer_dists:
        producer_dists = [[] for _ in range(n)]
    if not producer_classes:
        producer_classes = [[] for _ in range(n)]
    return ColumnCoreTrace(cols, producer_dists, producer_classes)


def merge_rec_lab(recs: List[dict], labs: List[dict]) -> List[dict]:
    """按 (thread_id, micro_seq) 对齐 records 与 labels。"""
    lab_idx = {(r["thread_id"], r["micro_seq"]): r for r in labs}
    merged = []
    for r in recs:
        key = (r["thread_id"], r["micro_seq"])
        lab = lab_idx.get(key)
        if lab is None:
            continue
        r["_commit_tick"] = lab.get("commit_tick", 0)
        r["_mispredicted"] = lab.get("mispredicted", 0)
        merged.append(r)
    # 程序序：按 (thread_id, micro_seq) 升序，不依赖 commit_tick
    merged.sort(key=lambda x: (x["thread_id"], x["micro_seq"]))
    return merged


def is_macro_head(rec: dict, prev: Optional[dict]) -> bool:
    """动态 macro 首条 µop：上一条 macro 结束或 macro_pc 变化。"""
    if prev is None:
        return True
    prev_ended = (prev.get("is_microop", 0) == 0
                  or prev.get("is_last_microop", 0) == 1)
    return prev_ended or rec.get("macro_pc") != prev.get("macro_pc")


def aggregate_pmu(window: List[dict], tick_per_cycle: int,
                  prev: Optional[dict] = None) -> Optional[dict]:
    """对一个核窗口聚合 PMU 标签（绝对计数 + 派生比率分母）。

    若窗口内存在任何 commit_tick<=0 的 µop（outer-join 救回的 lab 缺失项），
    直接返回 None 丢弃整个窗口，避免 cycles 端点差与 PMU 计数口径不一致
    污染训练标签。
    """
    if len(window) < 2:
        return None
    if any(w.get("_commit_tick", 0) <= 0 for w in window):
        return None
    ticks = [w["_commit_tick"] for w in window]
    t_start_tick = min(ticks)
    cycles = (max(ticks) - t_start_tick) / float(tick_per_cycle)
    if cycles <= 0:
        return None

    instr_retired = 0   # macro 指令数
    branch_count = cond_branch_count = indirect_branch_count = 0
    loads = stores = atomics = mem_ops = fetch_groups = 0
    branch_miss = l1d_ld_miss = l1d_st_miss = l1i_miss = llc_miss = 0
    l2_ld_miss = l2_st_miss = 0
    dtlb_miss = itlb_miss = inv_recv = 0
    mshr_sum = mshr_n = 0

    prev_local = prev
    for w in window:
        head = is_macro_head(w, prev_local)
        if head:
            instr_retired += 1
            fetch_groups += 1
            # i-side miss 仅在 fetch-group head 计
            if int(w.get("i_path_class", 0)) >= PC_L2:
                l1i_miss += 1
            if int(w.get("itlb_hit", 1)) == 0:
                itlb_miss += 1
        is_ld = int(w.get("is_load", 0))
        is_st = int(w.get("is_store", 0))
        is_at = int(w.get("is_atomic", 0))
        if int(w.get("is_branch", 0)):
            branch_count += 1
            if int(w.get("is_branch_cond", 0)):
                cond_branch_count += 1
            if int(w.get("is_branch_indirect", 0)):
                indirect_branch_count += 1
            if int(w.get("_mispredicted", 0)):
                branch_miss += 1
        pc = int(w.get("path_class", 0))
        if is_ld:
            loads += 1
            if pc >= PC_L2:
                l1d_ld_miss += 1
            if pc >= 2:
                l2_ld_miss += 1
        if is_st:
            stores += 1
            if pc >= PC_L2:
                l1d_st_miss += 1
            if pc >= 2:
                l2_st_miss += 1
        if is_at:
            atomics += 1
            if pc >= PC_L2:
                l1d_st_miss += 1
            if pc >= 2:
                l2_st_miss += 1
        if is_ld or is_st or is_at:
            mem_ops += 1
            if pc >= PC_DRAM:
                llc_miss += 1
            if int(w.get("dtlb_hit", 1)) == 0:
                dtlb_miss += 1
            mshr_sum += int(w.get("d_mshr_depth", 0))
            mshr_n += 1
            if int(w.get("coh_oracle", 0)) in COH_REMOTE:
                inv_recv += 1
        prev_local = w

    def safe_div(a, b):
        return float(a) / float(b) if b > 0 else 0.0

    uops = len(window)
    cpi_uop = safe_div(cycles, uops)
    cpi_macro = safe_div(cycles, instr_retired) if instr_retired > 0 else float("nan")
    # 标签：绝对值 + 分母（分母来自 functional，可在推理时复算）
    return {
        "cycles": cycles,
        "instr_retired": instr_retired,
        "uops": uops,
        "t_start_tick": float(t_start_tick),  # 该核窗口首条 commit_tick（绝对）
        # 比率主目标
        "cpi_uop": cpi_uop,
        "cpi_macro": cpi_macro,
        # 旧 "cpi" 别名 = cpi_macro，留给未升级的诊断脚本，PMU_KEYS 不再含 "cpi"
        "cpi": cpi_macro,
        # 训练主标签：miss 绝对计数（loss/model 侧以 log1p 空间回归）
        "branch_miss": float(branch_miss),
        "l1d_ld_miss": float(l1d_ld_miss),
        "l1d_st_miss": float(l1d_st_miss),
        "l2_ld_miss": float(l2_ld_miss),
        "l2_st_miss": float(l2_st_miss),
        "l1i_miss": float(l1i_miss),
        "llc_miss": float(llc_miss),
        # 诊断兼容字段：不再进入 PMU_KEYS
        "mpki_br": 1000.0 * safe_div(branch_miss, max(instr_retired, 1)),
        "branch_mispred_frac": safe_div(branch_miss, max(branch_count, 1)),
        "mr_l1d_ld": safe_div(l1d_ld_miss, max(loads, 1)),
        "mr_l1d_st": safe_div(l1d_st_miss, max(stores, 1)),
        "mr_l1i": safe_div(l1i_miss, max(fetch_groups, 1)),
        "mr_llc": safe_div(llc_miss, max(mem_ops, 1)),
        # 计数头（log1p 在 dataset 侧做）
        "dtlb_miss": float(dtlb_miss),
        "itlb_miss": float(itlb_miss),
        "inv_recv": float(inv_recv),
        # direct
        "mshr_avg": safe_div(mshr_sum, max(mshr_n, 1)),
        # 分母（供推理反算绝对值）
        "_denoms": {
            "branch_count": branch_count,
            "cond_branch_count": cond_branch_count,
            "indirect_branch_count": indirect_branch_count,
            "loads": loads,
            "stores": stores,
            "atomics": atomics,
            "fetch_groups": fetch_groups,
            "mem_ops": mem_ops,
        },
    }


def build_samples_align(merged_by_core: Dict[int, List[dict]], wname: str,
                        cfg: dict, n_per_core: int,
                        stride_tick: int, target_windows: int = 0) -> List[dict]:
    """方案A：固定每核指令数 N + 近似跨核时间对齐。

    - 全局时间锚点 T_k = t_lo + k*stride_tick（在全局时间轴上推进）。
    - 每个核取 commit_tick >= T_k 的连续前 N 条 µop -> <Cc> 段。
    - 各核段长度恒为 N（上下文均等，解决固定 ΔT 的核间不均）；
      各核都从物理时刻 T_k 附近起步（近似对齐，解决指令切窗的跨核错位）。
    - 标签按该 N 条窗口聚合（CPI=cycles/N，语义与指令切窗一致，稳定）。

    stride_tick==0 且 target_windows>0 时，按本负载时间跨度自适应：
      stride_tick = (t_hi - t_lo) / target_windows
    解决不同负载 CPI 差异巨大导致固定 stride 窗口数失衡的问题。

    依据：验证2 显示跨核争用对时间差 median 仅 9 cycle，N 条覆盖的时间宽度
    虽因核速不同而异，争用双方仍大概率落在各核的同一 T_k 锚定窗内。
    """
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    # 预取每核 (idx -> commit_tick) 升序数组与 µop 序列（已程序序，tick 单调）
    seqs = {}
    ticks = {}
    for c in cores:
        s = [w for w in merged_by_core[c] if w["_commit_tick"] > 0]
        if len(s) < n_per_core + 1:
            return []
        seqs[c] = s
        ticks[c] = [w["_commit_tick"] for w in s]
    t_lo = min(ticks[c][0] for c in cores)
    t_hi = max(ticks[c][-1] for c in cores)

    if stride_tick <= 0:
        if target_windows <= 0:
            target_windows = 3000
        stride_tick = max(1, int((t_hi - t_lo) / target_windows))
    print(f"[align] {wname}: span={t_hi-t_lo} tick, "
          f"stride_tick={stride_tick}, N={n_per_core}", file=sys.stderr)

    import bisect
    samples = []
    seg = 0
    n_dropped = 0
    k = 0
    while True:
        T_k = t_lo + k * stride_tick
        if T_k > t_hi:
            break
        k += 1
        per_core_windows = {}
        ok = True
        for c in cores:
            # 该核第一条 commit_tick >= T_k 的位置
            j = bisect.bisect_left(ticks[c], T_k)
            if j + n_per_core > len(seqs[c]):
                ok = False
                break
            win = seqs[c][j:j + n_per_core]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            n_dropped += 1
            continue
        # 跨核相对起点（cycle），以本窗各核最小 t_start 为零点
        min_ts = min(per_core_windows[c][1]["t_start_tick"] for c in cores)
        t_start_rel = [
            (per_core_windows[c][1]["t_start_tick"] - min_ts) / float(tpc)
            for c in cores
        ]
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[kk] for kk in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},A_N{n_per_core},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": int(n_per_core),
            "n_per_core": int(n_per_core),
            "stride_tick": int(stride_tick),
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"]
                              for c in cores],
            "uops_per_core": [per_core_windows[c][1]["uops"] for c in cores],
            "cpi_macro_per_core": [per_core_windows[c][1]["cpi_macro"]
                                   for c in cores],
            "t_start_rel": t_start_rel,
        })
        seg += 1
    if n_dropped:
        print(f"[align] {wname}: dropped {n_dropped} windows (尾部不足N), "
              f"kept {len(samples)}", file=sys.stderr)
    return samples


def build_samples_timewin(merged_by_core: Dict[int, List[dict]], wname: str,
                          cfg: dict, dt_tick: int,
                          max_per_core: int) -> List[dict]:
    """方案1 v1：按全局物理时间区间 [T0+k·ΔT, T0+(k+1)·ΔT) 切窗。

    与 build_samples（按指令下标切）不同：同一窗口内各核装的是
    commit_tick 落在同一物理时间区间的 µop，从而跨核物理时间对齐。

    - dt_tick: 时间窗宽 ΔT（单位 tick）。
    - max_per_core: 每核段指令数上限（token 预算），超出截断尾部。
      标签按截断后窗口聚合（保持 input 与 label 一致）。
    """
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    # 各核已按程序序排好（merge_rec_lab 保证），commit_tick 单调
    # 取每核首/尾有效 tick，定全局时间范围
    core_ticks = {}
    for c in cores:
        ts = [w["_commit_tick"] for w in merged_by_core[c]
              if w["_commit_tick"] > 0]
        if len(ts) < 2:
            return []
        core_ticks[c] = ts
    t_lo = min(ct[0] for ct in core_ticks.values())
    t_hi = max(ct[-1] for ct in core_ticks.values())
    n_win = int((t_hi - t_lo) // dt_tick) + 1

    # 每核维护一个游标，顺序扫过 µop 落入时间窗（commit_tick 单调，O(N)）
    cursor = {c: 0 for c in cores}
    samples = []
    seg = 0
    n_dropped = 0
    for k in range(n_win):
        w_lo = t_lo + k * dt_tick
        w_hi = w_lo + dt_tick
        per_core_windows = {}
        ok = True
        for c in cores:
            seq = merged_by_core[c]
            i = cursor[c]
            win = []
            while i < len(seq):
                ct = seq[i]["_commit_tick"]
                if ct <= 0:
                    i += 1
                    continue
                if ct >= w_hi:
                    break
                if ct >= w_lo:
                    win.append(seq[i])
                i += 1
            cursor[c] = i
            # token 预算截断（保留窗口内最早的 max_per_core 条）
            if max_per_core and len(win) > max_per_core:
                win = win[:max_per_core]
            pmu = aggregate_pmu(win, int(cfg.get("tick_per_cycle", 333)))
            if pmu is None:
                ok = False
            per_core_windows[c] = (win, pmu)
        if not ok:
            n_dropped += 1
            continue
        # 时间窗下 cycles 应≈ΔT/tpc（固定）；用 aggregate 的端点差即可
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[kk] for kk in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},DT{dt_tick},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": int(dt_tick),          # 复用字段：此处存 ΔT(tick)
            "dt_tick": int(dt_tick),
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"]
                              for c in cores],
            "uops_per_core": [per_core_windows[c][1]["uops"] for c in cores],
            "cpi_macro_per_core": [per_core_windows[c][1]["cpi_macro"]
                                   for c in cores],
            # 时间窗下各核起点已天然对齐到 w_lo，保留 0 占位以兼容旧 schema
            "t_start_rel": [0.0] * len(cores),
        })
        seg += 1
    if n_dropped:
        print(f"[timewin] {wname}: dropped {n_dropped}/{n_win} windows "
              f"(某核<2指令), kept {len(samples)}", file=sys.stderr)
    return samples


def take_macro_window_by_budget(seq: List[dict], start: int,
                                budget_tok: int,
                                macro_snap_max_retreat: int = 32
                                ) -> Tuple[int, int]:
    """按程序序累加真实 encode_uop 长度，达到 budget_tok 时优先收口到上一条
    完整 macro 边界。返回 (end_index, got_macro)。

    macro_snap_max_retreat 控制 macro-snap 的最大回退距离（µop 数）：
      - >0：短 macro 依然 snap 到边界（保 cpi_macro 精度）；若从当前位置到
        last_safe_end 回退超过该阈值（长 macro 触发），则直接在 µop 边界收口。
      - =0：完全禁用 macro-snap，永远在 µop 边界收口。
      - <0：保持旧行为，一律 macro-snap 无上限（长 macro 会大量回退）。

    主 label cpi_uop 只依赖 (max_commit_tick - min_commit_tick) / uops，不需
    要 macro 边界；改动的代价仅是诊断字段 cpi_macro 有 <1% 系统误差（首尾各
    半条 macro 的 instr_retired 少 1）。
    """
    n = len(seq)
    if start >= n:
        return start, 0
    prev = seq[start - 1] if start > 0 else None
    tok = 0
    macro_n = 0
    last_safe_end = start          # 上一条完整 macro 收尾位置（含）
    last_safe_macros = 0           # 到 last_safe_end 时累计的完整 macro 数
    last_head_idx = start          # 最近一次 macro head 的 index（用于超长 macro 判定）
    i = start
    while i < n:
        rec = seq[i]
        is_head = is_macro_head(rec, prev)
        if is_head and i > start:
            # 进入新 macro：当前 i 之前的 µop 构成一段完整 macro
            last_safe_end = i
            last_safe_macros = macro_n
        if is_head:
            last_head_idx = i
        tok_len = len(tk.encode_uop(rec))
        if tok + tok_len > budget_tok:
            # 装不下当前 µop：优先 macro-snap；触发 max-retreat 兜底则在 µop 边界收口
            if last_safe_end > start:
                if macro_snap_max_retreat >= 0:
                    retreat = i - last_safe_end
                    if macro_snap_max_retreat == 0 or retreat > macro_snap_max_retreat:
                        # 长 macro 兜底：直接在 µop 边界收口
                        return i, macro_n
                return last_safe_end, last_safe_macros
            # 从未见到完整 macro：如果允许 µop 边界，直接切
            if macro_snap_max_retreat == 0 or (
                macro_snap_max_retreat > 0
                and (i - last_head_idx) > macro_snap_max_retreat
            ):
                return i, macro_n
            # 保守行为：等到完整 macro 或耗尽
        tok += tok_len
        if is_head:
            macro_n += 1
        prev = rec
        i += 1
    # 扫到 trace 末尾：若末尾本身是完整 macro 收尾，直接用 i；否则回退
    if i >= n:
        # 末尾整段已被吃完；要求至少一条完整 macro
        if macro_n >= 1:
            return i, macro_n
        return last_safe_end, last_safe_macros
    return last_safe_end, last_safe_macros


def _prev_macro_end(seq: List[dict], end: int,
                    macro_snap_max_retreat: int = 32) -> int:
    """Return an exclusive end index near the tail commit-tick anchor.

    macro_snap_max_retreat 控制 macro-snap 的最大回退距离（µop 数）：
      - >0：从 end 向前回退到上一条完整 macro 收尾；若回退超过该阈值（当前
        位置正处于超长 macro 内部，例如 REP-prefix / gather / microcode
        fallback 展开出的几千条 µop），则不回退，直接在当前 µop 边界收口。
        这样跨核 T_end 对齐的 skew 上界为 max_retreat 条 µop 对应的 cycle 数。
      - =0：完全禁用 macro-snap，永远返回 end。
      - <0：保持旧行为，无上限回退到 macro 边界（可能导致大 skew）。
    """
    end = min(end, len(seq))
    if macro_snap_max_retreat == 0:
        return end
    orig_end = end
    while end > 0:
        rec = seq[end - 1]
        if rec.get("is_microop", 0) == 0 or rec.get("is_last_microop", 0) == 1:
            return end
        if macro_snap_max_retreat > 0 and orig_end - end >= macro_snap_max_retreat:
            # 长 macro 触发保护：不回退，直接在 µop 边界收口
            return orig_end
        end -= 1
    return end


def _macro_start(seq: List[dict], end: int) -> int:
    """Return the inclusive start index of the macro ending at exclusive end."""
    i = end - 1
    while i > 0 and not is_macro_head(seq[i], seq[i - 1]):
        i -= 1
    return i


def take_macro_window_back_by_budget(seq: List[dict], end: int,
                                     budget_tok: int,
                                     uop_token_cost: int = 6,
                                     macro_snap_max_retreat: int = 32
                                     ) -> Tuple[int, int, int]:
    """Backward quota window ending at a macro boundary.

    Returns (start_index, end_index, got_macro). The selected slice
    seq[start:end] is as long as possible under budget_tok and starts/ends on
    dynamic macro boundaries. This keeps the most recent context before a
    tail-time anchor.
    """
    end = _prev_macro_end(seq, end, macro_snap_max_retreat)
    if end <= 0:
        return end, end, 0

    start = end
    tok = 0
    macro_n = 0
    while start > 0:
        m_start = _macro_start(seq, start)
        macro_tok = 0
        for _rec in seq[m_start:start]:
            macro_tok += int(uop_token_cost)
        if tok + macro_tok > budget_tok and macro_n > 0:
            break
        if tok + macro_tok > budget_tok and macro_n == 0:
            return start, end, 0
        tok += macro_tok
        macro_n += 1
        start = m_start
    return start, end, macro_n


def sample_tq_fill(rng: random.Random) -> float:
    """Training fill jitter matched to deployment: mostly 85%+ loaded."""
    u = rng.random()
    if u < 0.70:
        return rng.uniform(0.92, 1.00)
    if u < 0.95:
        return rng.uniform(0.85, 0.92)
    return rng.uniform(0.75, 0.85)


def encode_multicore_sample(tokens: List[str], labels: List[List[float]],
                            per_core_windows: Dict[int, Tuple[List[dict], dict]],
                            cores: List[int], cfg: dict, sample_meta: dict,
                            query_placement: str = "tail",
                            uop_field_schema: str = "v9",
                            shared_features: Optional[dict] = None) -> dict:
    """Shared sample serialization for multi-core window builders."""
    if query_placement not in {"tail", "segment", "tail_local"}:
        raise ValueError(f"unknown query_placement={query_placement!r}")
    if uop_field_schema not in {"v9", "v26_14", "v27_ss"}:
        raise ValueError(f"unknown uop_field_schema={uop_field_schema!r}")
    if uop_field_schema == "v27_ss":
        field_count = tk.V27_UOP_FIELD_COUNT
    elif uop_field_schema == "v26_14":
        field_count = tk.V26_UOP_FIELD_COUNT
    else:
        field_count = tk.V9_UOP_FIELD_COUNT
    global_tokens, side_feats = build_cross_core_features(
        per_core_windows, cores)
    if shared_features:
        key_to_idx = {k: i for i, k in enumerate(tk.SIDE_FEATURE_KEYS)}
        core_feats = shared_features.get("core", {}) or {}
        global_feats = list(shared_features.get("global", []) or [])
        for ci, c in enumerate(cores):
            row = side_feats[ci]
            for name, val in zip(
                    SS_CORE_FEATURE_KEYS,
                    core_feats.get(c, [0.0] * len(SS_CORE_FEATURE_KEYS))):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)
            for name, val in zip(SS_GLOBAL_FEATURE_KEYS, global_feats):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)
    out_tokens: List[str] = []
    is_uop: List[int] = []
    uop_fields: List[List[int]] = []

    def append_token(tok: str) -> None:
        out_tokens.append(tok)
        is_uop.append(0)
        uop_fields.append([0] * field_count)

    def append_uop(core: int, pos: int, rec: dict) -> None:
        out_tokens.append("<UOP>")
        is_uop.append(1)
        if uop_field_schema == "v27_ss":
            ss = (
                (shared_features or {})
                .get("uop", {})
                .get(core, [])
            )
            rec2 = dict(rec)
            rec2["_ss_uop_fields"] = ss[pos] if pos < len(ss) else []
            uop_fields.append(tk.encode_uop_fields_v27(rec2))
        elif uop_field_schema == "v26_14":
            uop_fields.append(tk.encode_uop_fields_v26(rec))
        else:
            uop_fields.append(tk.encode_uop_fields(rec))

    for tok in ["<SYS>"] + tk.cfg_tokens(cfg) + ["<TRACE>"] + global_tokens:
        append_token(tok)
    core_split = []
    core_summaries = []
    for ci, c in enumerate(cores):
        win, _pmu = per_core_windows[c]
        append_token(f"<C{ci}_BEGIN>")
        summary_tokens, summary = build_core_summary_tokens(win)
        for tok in summary_tokens:
            append_token(tok)
        for pos, w in enumerate(win):
            append_uop(c, pos, w)
        if query_placement == "tail_local":
            append_token(f"<LOCAL_C{ci}>")
        if query_placement == "segment":
            append_token(f"<QUERY_C{ci}>")
        append_token(f"<C{ci}_END>")
        core_split.append(len(win))
        core_summaries.append(summary)
    append_token("<TRACE_END>")
    if query_placement in {"tail", "tail_local"}:
        for ci in range(len(cores)):
            append_token(f"<QUERY_C{ci}>")

    sample = dict(sample_meta)
    sample.update({
        "tokens": out_tokens,
        "is_uop": is_uop,
        "uop_fields": uop_fields,
        "global_tokens": global_tokens,
        "side_feats": side_feats,
        "legacy_token_len": len(out_tokens) + 5 * sum(core_split),
        "core_split": core_split,
        "core_summary": core_summaries,
        "label": labels,
        "label_keys": PMU_KEYS,
        "query_placement": query_placement,
        "uop_field_schema": uop_field_schema,
        "uop_field_count": field_count,
        "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
        "instr_retired": [per_core_windows[c][1]["instr_retired"]
                          for c in cores],
        "uops_per_core": [per_core_windows[c][1]["uops"] for c in cores],
        "cpi_macro_per_core": [per_core_windows[c][1]["cpi_macro"]
                               for c in cores],
    })
    return sample


def _uop_field_count_for_schema(uop_field_schema: str) -> int:
    if uop_field_schema == "v27_ss":
        return tk.V27_UOP_FIELD_COUNT
    if uop_field_schema == "v26_14":
        return tk.V26_UOP_FIELD_COUNT
    return tk.V9_UOP_FIELD_COUNT


def build_samples_tq(merged_by_core: Dict[int, List[dict]], wname: str,
                     cfg: dict, max_len: int,
                     target_windows: int = 1200,
                     stride_tick: int = 0,
                     ratio_lo: float = 0.5,
                     ratio_hi: float = 2.0,
                     min_fill: float = 0.0,
                     max_end_skew_cycle: float = 0.0,
                     overhead: int = 320,
                     budget_frac: float = 0.95,
                     min_uops_per_core: int = 256,
                     rng_seed: int = 0,
                     query_placement: str = "tail",
                     uop_field_schema: str = "v9",
                     shared_state_features: bool = False) -> List[dict]:
    """方案TQ：tail-aligned quota，最终默认切窗策略。

    - 以全局 T_end 为锚点，每核用 `bisect_right(commit_tick, T_end)` 取该核
      commit_tick <= T_end 的最后一条 µop 作为窗口尾部（µop 边界，不 macro-snap）。
    - 从尾部向前扩展一个公共时间跨度，直到每个 core 至少有 min_uops_per_core
      条 µop。
    - 不是每核固定 min uop；快核/高吞吐核在同一时间跨度内可以更多。
    - max_len 只作为安全上界，不再尝试填满。

    因窗口在 µop 边界切，主 label cpi_uop=(max_commit_tick-min_commit_tick)/uops
    定义干净；诊断字段 cpi_macro 在首尾 macro 中间切时会有 <1% 系统误差
    （instr_retired 少 1）。跨核 T_end skew 只受 µop 间 commit_tick 间隔限制，
    不会因某核走到超长 macro（REP-prefix / gather 展开出的几千条 µop）而放大。
    """
    tpc = int(cfg.get("tick_per_cycle", 333))
    cores = sorted(merged_by_core.keys())
    n_core = len(cores)
    if n_core < 1:
        return []

    seqs: Dict[int, List[dict]] = {}
    ticks: Dict[int, List[int]] = {}
    for c in cores:
        seq = [w for w in merged_by_core[c] if w.get("_commit_tick", 0) > 0]
        if len(seq) < 2:
            return []
        seqs[c] = seq
        ticks[c] = [int(w["_commit_tick"]) for w in seq]

    t_lo = max(ticks[c][0] for c in cores)
    t_hi = min(ticks[c][-1] for c in cores)
    if t_hi <= t_lo:
        return []
    if stride_tick <= 0:
        target_windows = max(1, int(target_windows or 1200))
        stride_tick = max(1, int((t_hi - t_lo) / target_windows))

    # v9 composite encoding spends one transformer position per uop. Account
    # for control/config/global/query plus per-core summary tokens explicitly.
    local_extra = n_core if query_placement == "tail_local" else 0
    v9_overhead = (
        1 + len(tk.cfg_tokens(cfg)) + 1 + 4
        + n_core * (2 + len(tk.SUMMARY_TOKEN_FEATURES))
        + local_extra + 1 + n_core
    )
    effective_overhead = max(int(overhead), int(v9_overhead))
    base_budget = int((max_len - effective_overhead) * budget_frac)
    min_uops_per_core = max(1, int(min_uops_per_core))
    floor_total = n_core * min_uops_per_core
    if floor_total > base_budget:
        raise ValueError(
            f"TQ min_uops_per_core={min_uops_per_core} with n_core={n_core} "
            f"needs {floor_total} uop positions, exceeds budget={base_budget} "
            f"(max_len={max_len}, overhead={effective_overhead})"
        )
    print(f"[tq] {wname}: max_len={max_len} base_budget={base_budget} "
          f"overhead={effective_overhead} min_uops/core={min_uops_per_core} "
          f"stride_tick={stride_tick} target_windows={target_windows} "
          f"query_placement={query_placement} "
          f"uop_field_schema={uop_field_schema}",
          file=sys.stderr)
    shared_engine = (
        SharedStateFeatureEngine.from_merged_by_core(seqs)
        if shared_state_features else None
    )

    samples: List[dict] = []
    dropped_low_fill = 0
    dropped_skew = 0
    dropped_bad = 0
    seg = 0
    k = 0
    while True:
        T_end = t_lo + k * stride_tick
        if T_end > t_hi:
            break
        k += 1

        ends: Dict[int, int] = {}
        floor_ticks: List[int] = []
        ok = True
        for c in cores:
            end = bisect.bisect_right(ticks[c], T_end)
            if end < min_uops_per_core:
                ok = False
                break
            ends[c] = end
            floor_ticks.append(ticks[c][end - min_uops_per_core])
        if not ok:
            dropped_bad += 1
            continue

        # Common time-aligned start. The core with the oldest floor tick is the
        # limiting core; every other core may contribute more than the floor.
        T_start = min(floor_ticks)
        spans: Dict[int, Tuple[int, int]] = {}
        uop_total = 0
        for c in cores:
            start = bisect.bisect_left(ticks[c], T_start)
            end = ends[c]
            spans[c] = (start, end)
            uop_total += max(0, end - start)
        if uop_total + effective_overhead > max_len:
            dropped_bad += 1
            continue

        per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
        for c in cores:
            start, end = spans[c]
            if end <= start or end - start < min_uops_per_core:
                ok = False
                break
            win = seqs[c][start:end]
            pmu = aggregate_pmu(
                win, tpc, prev=seqs[c][start - 1] if start > 0 else None)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            dropped_bad += 1
            continue

        t_starts = [per_core_windows[c][1]["t_start_tick"] for c in cores]
        t_ends = [
            max(int(w["_commit_tick"]) for w in per_core_windows[c][0])
            for c in cores
        ]
        end_skew_cycle = (max(t_ends) - min(t_ends)) / float(tpc)
        if max_end_skew_cycle > 0 and end_skew_cycle > max_end_skew_cycle:
            dropped_skew += 1
            continue

        labels = [
            [per_core_windows[c][1][kk] for kk in PMU_KEYS]
            for c in cores
        ]
        shared_features = None
        if shared_engine is not None:
            # Teacher-state construction: only replay history strictly before
            # this window's common start.  Current-window accesses are not
            # visible to the current sample.
            shared_engine.advance_to_tick(T_start)
            shared_features = shared_engine.window_features(
                per_core_windows, cores)
        min_tstart = min(t_starts)
        min_tend = min(t_ends)
        sample = encode_multicore_sample(
            tokens=[],
            labels=labels,
            per_core_windows=per_core_windows,
            cores=cores,
            cfg=cfg,
            sample_meta={
                "id": f"{wname},TQ,seg{seg:05d}",
                "workload": wname,
                "cfg_hash": cfg.get("cfg_hash", "A0"),
                "n_core": n_core,
                "w_ops": int(sum(len(per_core_windows[c][0])
                                 for c in cores) / n_core),
                "max_len": int(max_len),
                "mode": "tq",
                "min_uops_per_core": int(min_uops_per_core),
                "target_fill": float(floor_total / max(base_budget, 1)),
                "t_start_tick": int(T_start),
                "t_end_tick": int(T_end),
                "tq_span_tick": int(T_end - T_start),
                "stride_tick": int(stride_tick),
                "t_start_rel": [
                    (ts - min_tstart) / float(tpc) for ts in t_starts
                ],
                "t_end_rel": [
                    (te - min_tend) / float(tpc) for te in t_ends
                ],
                "end_skew_cycle": float(end_skew_cycle),
            },
            query_placement=query_placement,
            uop_field_schema=uop_field_schema,
            shared_features=shared_features,
        )
        sample["fill_ratio"] = len(sample["tokens"]) / float(max_len)
        if len(sample["tokens"]) > max_len:
            dropped_bad += 1
            continue
        if sample["fill_ratio"] < min_fill:
            dropped_low_fill += 1
            continue
        samples.append(sample)
        seg += 1

    print(f"[tq] {wname}: produced {len(samples)} windows "
          f"(drop_bad={dropped_bad}, drop_low_fill={dropped_low_fill}, "
          f"drop_skew={dropped_skew})", file=sys.stderr)
    return samples


def build_tq_thin_plans(merged_by_core: Dict[int, List[dict]], wname: str,
                        cfg: dict, max_len: int,
                        target_windows: int = 1200,
                        stride_tick: int = 0,
                        min_fill: float = 0.0,
                        max_end_skew_cycle: float = 0.0,
                        overhead: int = 320,
                        budget_frac: float = 0.95,
                        min_uops_per_core: int = 256,
                        query_placement: str = "tail",
                        uop_field_schema: str = "v9") -> List[dict]:
    """Build lightweight TQ window plans for direct tensor-cache output.

    This deliberately preserves the old TQ candidate order, labels, summary
    features, ids, and variant inputs, but delays expensive UOP field encoding
    until after dedup/cap has selected windows.
    """
    tpc = int(cfg.get("tick_per_cycle", 333))
    cores = sorted(merged_by_core.keys())
    n_core = len(cores)
    if n_core < 1:
        return []

    seqs: Dict[int, List[dict]] = {}
    ticks: Dict[int, List[int]] = {}
    for c in cores:
        seq = [w for w in merged_by_core[c] if w.get("_commit_tick", 0) > 0]
        if len(seq) < 2:
            return []
        seqs[c] = seq
        ticks[c] = [int(w["_commit_tick"]) for w in seq]

    t_lo = max(ticks[c][0] for c in cores)
    t_hi = min(ticks[c][-1] for c in cores)
    if t_hi <= t_lo:
        return []
    if stride_tick <= 0:
        target_windows = max(1, int(target_windows or 1200))
        stride_tick = max(1, int((t_hi - t_lo) / target_windows))

    cfg_tokens = tk.cfg_tokens(cfg)
    local_extra = n_core if query_placement == "tail_local" else 0
    v9_overhead = (
        1 + len(cfg_tokens) + 1 + len(tk.GLOBAL_TOKEN_FEATURES)
        + n_core * (2 + len(tk.SUMMARY_TOKEN_FEATURES))
        + local_extra + 1 + n_core
    )
    effective_overhead = max(int(overhead), int(v9_overhead))
    base_budget = int((max_len - effective_overhead) * budget_frac)
    min_uops_per_core = max(1, int(min_uops_per_core))
    floor_total = n_core * min_uops_per_core
    if floor_total > base_budget:
        raise ValueError(
            f"TQ min_uops_per_core={min_uops_per_core} with n_core={n_core} "
            f"needs {floor_total} uop positions, exceeds budget={base_budget} "
            f"(max_len={max_len}, overhead={effective_overhead})"
        )
    print(f"[tq-thin] {wname}: max_len={max_len} "
          f"base_budget={base_budget} overhead={effective_overhead} "
          f"min_uops/core={min_uops_per_core} stride_tick={stride_tick} "
          f"target_windows={target_windows} "
          f"query_placement={query_placement} "
          f"uop_field_schema={uop_field_schema}",
          file=sys.stderr)

    field_count = _uop_field_count_for_schema(uop_field_schema)
    plans: List[dict] = []
    dropped_low_fill = 0
    dropped_skew = 0
    dropped_bad = 0
    seg = 0
    k = 0
    while True:
        T_end = t_lo + k * stride_tick
        if T_end > t_hi:
            break
        k += 1

        ends: Dict[int, int] = {}
        floor_ticks: List[int] = []
        ok = True
        for c in cores:
            end = bisect.bisect_right(ticks[c], T_end)
            if end < min_uops_per_core:
                ok = False
                break
            ends[c] = end
            floor_ticks.append(ticks[c][end - min_uops_per_core])
        if not ok:
            dropped_bad += 1
            continue

        T_start = min(floor_ticks)
        spans: Dict[int, Tuple[int, int]] = {}
        uop_total = 0
        for c in cores:
            start = bisect.bisect_left(ticks[c], T_start)
            end = ends[c]
            spans[c] = (start, end)
            uop_total += max(0, end - start)
        if uop_total + effective_overhead > max_len:
            dropped_bad += 1
            continue

        per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
        for c in cores:
            start, end = spans[c]
            if end <= start or end - start < min_uops_per_core:
                ok = False
                break
            win = seqs[c][start:end]
            pmu = aggregate_pmu(
                win, tpc, prev=seqs[c][start - 1] if start > 0 else None)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            dropped_bad += 1
            continue

        t_starts = [per_core_windows[c][1]["t_start_tick"] for c in cores]
        t_ends = [
            max(int(w["_commit_tick"]) for w in per_core_windows[c][0])
            for c in cores
        ]
        end_skew_cycle = (max(t_ends) - min(t_ends)) / float(tpc)
        if max_end_skew_cycle > 0 and end_skew_cycle > max_end_skew_cycle:
            dropped_skew += 1
            continue

        labels = [
            [per_core_windows[c][1][kk] for kk in PMU_KEYS]
            for c in cores
        ]
        min_tstart = min(t_starts)
        min_tend = min(t_ends)
        core_split: List[int] = []
        core_summaries: List[dict] = []
        summary_token_total = 0
        for c in cores:
            win, _pmu = per_core_windows[c]
            summary_tokens, summary = build_core_summary_tokens(win)
            summary_token_total += len(summary_tokens)
            core_split.append(len(win))
            core_summaries.append(summary)

        segment_extra = n_core if query_placement == "segment" else 0
        token_len = (
            1 + len(cfg_tokens) + 1 + len(tk.GLOBAL_TOKEN_FEATURES)
            + n_core * 2 + summary_token_total
            + sum(core_split) + local_extra + segment_extra + 1
        )
        if query_placement in {"tail", "tail_local"}:
            token_len += n_core
        fill_ratio = token_len / float(max_len)
        if token_len > max_len:
            dropped_bad += 1
            continue
        if fill_ratio < min_fill:
            dropped_low_fill += 1
            continue

        pmu_rows = [per_core_windows[c][1] for c in cores]
        plan = {
            "_thin_tq_plan": True,
            "_cores": list(cores),
            "_spans": {int(c): tuple(spans[c]) for c in cores},
            "_pmu_rows": pmu_rows,
            "id": f"{wname},TQ,seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": n_core,
            "w_ops": int(sum(core_split) / n_core),
            "max_len": int(max_len),
            "mode": "tq",
            "min_uops_per_core": int(min_uops_per_core),
            "target_fill": float(floor_total / max(base_budget, 1)),
            "t_start_tick": int(T_start),
            "t_end_tick": int(T_end),
            "tq_span_tick": int(T_end - T_start),
            "stride_tick": int(stride_tick),
            "t_start_rel": [
                (ts - min_tstart) / float(tpc) for ts in t_starts
            ],
            "t_end_rel": [
                (te - min_tend) / float(tpc) for te in t_ends
            ],
            "end_skew_cycle": float(end_skew_cycle),
            "fill_ratio": float(fill_ratio),
            "core_split": core_split,
            "core_summary": core_summaries,
            "label": labels,
            "label_keys": PMU_KEYS,
            "query_placement": query_placement,
            "uop_field_schema": uop_field_schema,
            "uop_field_count": field_count,
            "denoms": [pmu["_denoms"] for pmu in pmu_rows],
            "instr_retired": [pmu["instr_retired"] for pmu in pmu_rows],
            "uops_per_core": [pmu["uops"] for pmu in pmu_rows],
            "cpi_macro_per_core": [pmu["cpi_macro"] for pmu in pmu_rows],
        }
        plans.append(plan)
        seg += 1

    print(f"[tq-thin] {wname}: produced {len(plans)} window plans "
          f"(drop_bad={dropped_bad}, drop_low_fill={dropped_low_fill}, "
          f"drop_skew={dropped_skew})", file=sys.stderr)
    return plans


def encode_tq_plan_sample(plan: dict,
                          merged_by_core: Dict[int, List[dict]],
                          cfg: dict,
                          query_placement: str,
                          uop_field_schema: str,
                          shared_engine: Optional[SharedStateFeatureEngine] = None,
                          trace_cache: Optional[dict] = None) -> dict:
    cores = [int(c) for c in plan["_cores"]]
    spans = {int(c): tuple(plan["_spans"][int(c)]) for c in cores}
    pmu_rows = list(plan["_pmu_rows"])
    per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
    for ci, c in enumerate(cores):
        start, end = spans[c]
        per_core_windows[c] = (merged_by_core[c][start:end], pmu_rows[ci])

    shared_features = None
    if shared_engine is not None:
        shared_engine.advance_to_tick(int(plan.get("t_start_tick", 0) or 0))
        if trace_cache is not None:
            shared_features = shared_engine.window_features_cached(
                spans, trace_cache, cores)
        else:
            shared_features = shared_engine.window_features(
                per_core_windows, cores)

    labels = list(plan["label"])
    meta_skip = {
        "_thin_tq_plan", "_cores", "_spans", "_pmu_rows",
        "core_split", "core_summary", "label", "label_keys",
        "query_placement", "uop_field_schema", "uop_field_count",
        "denoms", "instr_retired", "uops_per_core", "cpi_macro_per_core",
        "mean_cpi_uop", "workload_variant",
    }
    sample_meta = {k: v for k, v in plan.items() if k not in meta_skip}
    sample = encode_multicore_sample(
        tokens=[],
        labels=labels,
        per_core_windows=per_core_windows,
        cores=cores,
        cfg=cfg,
        sample_meta=sample_meta,
        query_placement=query_placement,
        uop_field_schema=uop_field_schema,
        shared_features=shared_features,
    )
    sample["fill_ratio"] = plan.get(
        "fill_ratio", len(sample["tokens"]) / float(plan.get("max_len", 1) or 1))
    sample["mean_cpi_uop"] = plan.get("mean_cpi_uop", 0.0)
    sample["workload_variant"] = plan.get(
        "workload_variant", sample.get("workload", ""))
    return sample


def build_tq_columnar_plans(traces_by_core: Dict[int, ColumnCoreTrace],
                            wname: str, cfg: dict, max_len: int,
                            target_windows: int = 1200,
                            stride_tick: int = 0,
                            min_fill: float = 0.0,
                            max_end_skew_cycle: float = 0.0,
                            overhead: int = 320,
                            budget_frac: float = 0.95,
                            min_uops_per_core: int = 256,
                            query_placement: str = "tail",
                            uop_field_schema: str = "v9") -> List[dict]:
    tpc = int(cfg.get("tick_per_cycle", 333))
    cores = sorted(traces_by_core.keys())
    n_core = len(cores)
    if n_core < 1:
        return []
    if any(len(traces_by_core[c]) < 2 for c in cores):
        return []
    ticks = {c: traces_by_core[c].ticks for c in cores}
    t_lo = max(int(ticks[c][0]) for c in cores)
    t_hi = min(int(ticks[c][-1]) for c in cores)
    if t_hi <= t_lo:
        return []
    if stride_tick <= 0:
        target_windows = max(1, int(target_windows or 1200))
        stride_tick = max(1, int((t_hi - t_lo) / target_windows))

    cfg_tokens = tk.cfg_tokens(cfg)
    local_extra = n_core if query_placement == "tail_local" else 0
    segment_extra = n_core if query_placement == "segment" else 0
    v9_overhead = (
        1 + len(cfg_tokens) + 1 + len(tk.GLOBAL_TOKEN_FEATURES)
        + n_core * (2 + len(tk.SUMMARY_TOKEN_FEATURES))
        + local_extra + segment_extra + 1 + n_core
    )
    effective_overhead = max(int(overhead), int(v9_overhead))
    base_budget = int((max_len - effective_overhead) * budget_frac)
    min_uops_per_core = max(1, int(min_uops_per_core))
    floor_total = n_core * min_uops_per_core
    if floor_total > base_budget:
        raise ValueError(
            f"TQ min_uops_per_core={min_uops_per_core} with n_core={n_core} "
            f"needs {floor_total} uop positions, exceeds budget={base_budget}"
        )
    print(f"[tq-columnar] {wname}: max_len={max_len} "
          f"base_budget={base_budget} overhead={effective_overhead} "
          f"min_uops/core={min_uops_per_core} stride_tick={stride_tick} "
          f"target_windows={target_windows} "
          f"query_placement={query_placement} "
          f"uop_field_schema={uop_field_schema}",
          file=sys.stderr)

    field_count = _uop_field_count_for_schema(uop_field_schema)
    plans: List[dict] = []
    dropped_bad = 0
    dropped_low_fill = 0
    dropped_skew = 0
    seg = 0
    k = 0
    while True:
        T_end = t_lo + k * stride_tick
        if T_end > t_hi:
            break
        k += 1

        ends: Dict[int, int] = {}
        floor_ticks: List[int] = []
        ok = True
        for c in cores:
            end = int(np.searchsorted(ticks[c], T_end, side="right"))
            if end < min_uops_per_core:
                ok = False
                break
            ends[c] = end
            floor_ticks.append(int(ticks[c][end - min_uops_per_core]))
        if not ok:
            dropped_bad += 1
            continue

        T_start = min(floor_ticks)
        spans: Dict[int, Tuple[int, int]] = {}
        uop_total = 0
        for c in cores:
            start = int(np.searchsorted(ticks[c], T_start, side="left"))
            end = ends[c]
            spans[c] = (start, end)
            uop_total += max(0, end - start)
        if uop_total + effective_overhead > max_len:
            dropped_bad += 1
            continue

        pmu_rows = []
        core_split = []
        core_summaries = []
        t_starts = []
        t_ends = []
        for c in cores:
            start, end = spans[c]
            if end <= start or end - start < min_uops_per_core:
                ok = False
                break
            pmu = columnar_pmu_from_span(traces_by_core[c], start, end, tpc)
            if pmu is None:
                ok = False
                break
            pmu_rows.append(pmu)
            core_split.append(end - start)
            core_summaries.append(
                build_columnar_core_summary(traces_by_core[c], start, end))
            t_starts.append(float(pmu["t_start_tick"]))
            t_ends.append(float(ticks[c][end - 1]))
        if not ok:
            dropped_bad += 1
            continue

        end_skew_cycle = (max(t_ends) - min(t_ends)) / float(tpc)
        if max_end_skew_cycle > 0 and end_skew_cycle > max_end_skew_cycle:
            dropped_skew += 1
            continue

        summary_token_total = len(tk.SUMMARY_TOKEN_FEATURES) * n_core
        token_len = (
            1 + len(cfg_tokens) + 1 + len(tk.GLOBAL_TOKEN_FEATURES)
            + n_core * 2 + summary_token_total
            + sum(core_split) + local_extra + segment_extra + 1
        )
        if query_placement in {"tail", "tail_local"}:
            token_len += n_core
        fill_ratio = token_len / float(max_len)
        if token_len > max_len:
            dropped_bad += 1
            continue
        if fill_ratio < min_fill:
            dropped_low_fill += 1
            continue

        min_tstart = min(t_starts)
        min_tend = min(t_ends)
        labels = [[pmu[kk] for kk in PMU_KEYS] for pmu in pmu_rows]
        plan = {
            "_columnar_tq_plan": True,
            "_cores": list(cores),
            "_spans": {int(c): tuple(spans[c]) for c in cores},
            "_pmu_rows": pmu_rows,
            "id": f"{wname},TQ,seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": n_core,
            "w_ops": int(sum(core_split) / n_core),
            "max_len": int(max_len),
            "mode": "tq",
            "min_uops_per_core": int(min_uops_per_core),
            "target_fill": float(floor_total / max(base_budget, 1)),
            "t_start_tick": int(T_start),
            "t_end_tick": int(T_end),
            "tq_span_tick": int(T_end - T_start),
            "stride_tick": int(stride_tick),
            "t_start_rel": [
                (ts - min_tstart) / float(tpc) for ts in t_starts
            ],
            "t_end_rel": [
                (te - min_tend) / float(tpc) for te in t_ends
            ],
            "end_skew_cycle": float(end_skew_cycle),
            "fill_ratio": float(fill_ratio),
            "core_split": core_split,
            "core_summary": core_summaries,
            "label": labels,
            "label_keys": PMU_KEYS,
            "query_placement": query_placement,
            "uop_field_schema": uop_field_schema,
            "uop_field_count": field_count,
            "denoms": [pmu["_denoms"] for pmu in pmu_rows],
            "instr_retired": [pmu["instr_retired"] for pmu in pmu_rows],
            "uops_per_core": [pmu["uops"] for pmu in pmu_rows],
            "cpi_macro_per_core": [pmu["cpi_macro"] for pmu in pmu_rows],
        }
        plans.append(plan)
        seg += 1

    print(f"[tq-columnar] {wname}: produced {len(plans)} window plans "
          f"(drop_bad={dropped_bad}, drop_low_fill={dropped_low_fill}, "
          f"drop_skew={dropped_skew})", file=sys.stderr)
    return plans


def build_uop_field_cache(
    merged_by_core: Dict[int, List[dict]],
    uop_field_schema: str,
) -> Dict[int, List[List[int]]]:
    """Precompute static UOP fields once per core for thin direct-cache builds."""
    cache: Dict[int, List[List[int]]] = {}
    if uop_field_schema in {"v26_14", "v27_ss"}:
        encoder = tk.encode_uop_fields_v26
    else:
        encoder = tk.encode_uop_fields
    for c, seq in merged_by_core.items():
        cache[int(c)] = [encoder(rec) for rec in seq]
    return cache


def build_samples_quota(merged_by_core: Dict[int, List[dict]], wname: str,
                        cfg: dict, max_len: int,
                        ratio_lo: float, ratio_hi: float,
                        overhead: int = 64, budget_frac: float = 0.95,
                        rng_seed: int = 0,
                        macro_snap_max_retreat: int = 32) -> List[dict]:
    """方案Q（B 简化版）：token-budget 全核归一化分配 + macro 抖动 + 真值 t_start_rel。

    与"等分 budget/N × ratio"路径区别：
      ratio_c ~ U(lo, hi)  每核独立采样
      budget_c = total_budget · ratio_c / Σ ratio_c   归一化保证总预算不超
    单核可拿到接近 ratio_hi/(N·mean) × total_budget 的 token（远超 budget/N），
    覆盖部署 OnlineQuotaPlanner 跨核借调时落后核拿到的大窗样本，避免 OOD。

    每核段独立切（不强求跨核 cycle 对齐），t_start_rel 仍用真值 commit_tick。
    贪心装窗：累加真实 encode_uop 长度直到 budget_c，收口到上一条完整 macro 边界。
    """
    rng = random.Random(rng_seed)
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    n_core = len(cores)
    if n_core < 1:
        return []

    total_budget = int((max_len - overhead) * budget_frac)
    print(f"[quota] {wname}: total_budget={total_budget} "
          f"ratio=[{ratio_lo:.2f},{ratio_hi:.2f}] cores={n_core}",
          file=sys.stderr)

    cursor = {c: 0 for c in cores}
    samples: List[dict] = []
    seg = 0
    while True:
        per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
        ok = True
        ratios = [rng.uniform(ratio_lo, ratio_hi) for _ in cores]
        ratio_sum = sum(ratios)
        for ci, c in enumerate(cores):
            budget_c = max(1, int(round(total_budget * ratios[ci] / ratio_sum)))
            seq = merged_by_core[c]
            end, got = take_macro_window_by_budget(
                seq, cursor[c], budget_c,
                macro_snap_max_retreat=macro_snap_max_retreat,
            )
            if got < 1 or end <= cursor[c]:
                ok = False
                break
            win = seq[cursor[c]:end]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
            cursor[c] = end
        if not ok:
            break

        # 真值跨核相对起点（cycle）
        min_ts = min(per_core_windows[c][1]["t_start_tick"] for c in cores)
        t_start_rel = [
            (per_core_windows[c][1]["t_start_tick"] - min_ts) / float(tpc)
            for c in cores
        ]

        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[kk] for kk in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(n_core):
            tokens.append(f"<QUERY_C{ci}>")

        # 安全网：理论上 budget_per_core·ratio_hi·N + overhead ≤ max_len
        # 仍 assert 一道避免 overhead 估错时静默 OOC。
        if len(tokens) > max_len:
            seg += 1
            continue

        samples.append({
            "id": f"{wname},Q,seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": n_core,
            "w_ops": int(sum(core_split) / n_core),
            "max_len": int(max_len),
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"]
                              for c in cores],
            "uops_per_core": [per_core_windows[c][1]["uops"] for c in cores],
            "cpi_macro_per_core": [per_core_windows[c][1]["cpi_macro"]
                                   for c in cores],
            "t_start_rel": t_start_rel,
        })
        seg += 1
    print(f"[quota] {wname}: produced {len(samples)} windows", file=sys.stderr)
    return samples


def build_samples(merged_by_core: Dict[int, List[dict]], wname: str,
                  cfg: dict, W: int, stride: int) -> List[dict]:
    """对齐多核程序序起点切窗，每窗一个样本。"""
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    min_len = min(len(merged_by_core[c]) for c in cores)
    samples = []
    seg = 0
    for t in range(0, min_len - W + 1, stride):
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        ok = True
        per_core_windows = {}
        for c in cores:
            win = merged_by_core[c][t:t + W]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            continue
        # 跨核相对起始时间：以本窗口 8 核最小 t_start 为零点，单位 cycle
        min_tstart = min(per_core_windows[c][1]["t_start_tick"] for c in cores)
        t_start_rel = [
            (per_core_windows[c][1]["t_start_tick"] - min_tstart) / float(tpc)
            for c in cores
        ]
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[k] for k in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},W{W},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": W,
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,                 # [n_core, K]
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"] for c in cores],
            "uops_per_core": [per_core_windows[c][1]["uops"] for c in cores],
            "cpi_macro_per_core": [per_core_windows[c][1]["cpi_macro"]
                                   for c in cores],
            "t_start_rel": t_start_rel,      # [n_core] 跨核相对起始时间(cycle)
        })
        seg += 1
    return samples


def process_workload(wd: str, raw_root: str, out_dir: str,
                     cfg: dict, window: int, stride: int,
                     dt_tick: int = 0, max_per_core: int = 0,
                     align_n: int = 0, stride_tick: int = 0,
                     target_windows: int = 0,
                     quota_max_len: int = 0,
                     quota_ratio_lo: float = 0.5,
                     quota_ratio_hi: float = 1.0,
                     quota_seed: int = 0,
                     tq_max_len: int = 0,
                     tq_target_windows: int = 1200,
                     tq_stride_tick: int = 0,
                     tq_ratio_lo: float = 0.5,
                     tq_ratio_hi: float = 2.0,
                     tq_min_fill: float = 0.0,
                     tq_max_end_skew_cycle: float = 0.0,
                     tq_seed: int = 0,
                     tq_min_uops_per_core: int = 256,
                     rd_window: int = 8192,
                     query_placement: str = "tail",
                     uop_field_schema: str = "v9",
                     shared_state_features: bool = False,
                     macro_snap_max_retreat: int = 32) -> tuple:
    """单个 workload 构建 shard，返回 (wd, ok, samples, shard_path, message)。

    模式优先级：quota_max_len>0 走旧方案Q；否则 align_n>0 走方案A；
    否则 dt_tick>0 走时间切窗；否则 tq_max_len>0 走默认最终方案TQ；
    否则指令切窗。
    """
    trace_dir = os.path.join(raw_root, wd, "tao_trace")
    if not os.path.isdir(trace_dir):
        return wd, False, 0, "", f"[skip] {wd}: no tao_trace"

    files = load_core_files(trace_dir)
    if len(files) < 1:
        return wd, False, 0, "", f"[skip] {wd}: no cores"

    t0 = time.time()
    print(f"[start] {wd}: reading {len(files)} cores ...", file=sys.stderr)
    merged_by_core = {}
    for ci, (c, fp) in enumerate(sorted(files.items())):
        if "aligned" in fp:
            merged_by_core[c] = read_aligned_parquet(fp["aligned"])
        else:
            recs = read_jsonl(fp["rec"])
            labs = read_jsonl(fp["lab"])
            merged_by_core[c] = merge_rec_lab(recs, labs)
        print(f"[read] {wd}: core {ci+1}/{len(files)} "
              f"({len(merged_by_core[c])} µops, {time.time()-t0:.0f}s)",
              file=sys.stderr)

    print(f"[build] {wd}: read done in {time.time()-t0:.0f}s, slicing ...",
          file=sys.stderr)

    for seq in merged_by_core.values():
        annotate_rd_stride(seq, rd_window=rd_window)
        annotate_functional_proxies(seq)
    annotate_cross_core_functional_proxies(merged_by_core)

    if quota_max_len > 0:
        samples = build_samples_quota(
            merged_by_core, wd, cfg, quota_max_len,
            quota_ratio_lo, quota_ratio_hi, rng_seed=quota_seed,
            macro_snap_max_retreat=macro_snap_max_retreat,
        )
    elif align_n > 0:
        samples = build_samples_align(merged_by_core, wd, cfg,
                                      align_n, stride_tick, target_windows)
    elif dt_tick > 0:
        samples = build_samples_timewin(merged_by_core, wd, cfg,
                                        dt_tick, max_per_core)
    elif tq_max_len > 0:
        samples = build_samples_tq(
            merged_by_core, wd, cfg, tq_max_len,
            target_windows=tq_target_windows,
            stride_tick=tq_stride_tick,
            ratio_lo=tq_ratio_lo,
            ratio_hi=tq_ratio_hi,
            min_fill=tq_min_fill,
            max_end_skew_cycle=tq_max_end_skew_cycle,
            min_uops_per_core=tq_min_uops_per_core,
            rng_seed=tq_seed,
            query_placement=query_placement,
            uop_field_schema=uop_field_schema,
            shared_state_features=shared_state_features,
        )
    else:
        samples = build_samples(merged_by_core, wd, cfg, window, stride)
    shard_path = os.path.join(out_dir, f"{wd}.jsonl")
    with open(shard_path, "w") as fout:
        for s in samples:
            _relabel_by_cpi(s)
            fout.write(json.dumps(s, separators=(",", ":")) + "\n")
    return wd, True, len(samples), shard_path, f"[ok] {wd}: cores={len(files)} samples={len(samples)}"


def process_workload_direct_cache_columnar(
        wd: str,
        files: Dict[int, dict],
        cache_dir: str,
        cfg: dict,
        tq_max_len: int,
        tq_target_windows: int,
        tq_stride_tick: int,
        tq_min_fill: float,
        tq_max_end_skew_cycle: float,
        tq_min_uops_per_core: int,
        rd_window: int,
        query_placement: str,
        uop_field_schema: str,
        shared_state_features: bool,
        cache_max_len: int,
        cache_label_keys: Optional[List[str]],
        cache_shard_size: int,
        per_workload_cap: Optional[str],
        per_workload_cap_seed: int,
        dedup_threshold: float) -> tuple:
    """Fast aligned-parquet direct TQ path: column arrays + prefix labels."""
    from train.dataset import build_tensor_cache_shard

    t0 = time.time()
    print(f"[start-columnar] {wd}: reading {len(files)} cores ...",
          file=sys.stderr)
    traces_by_core: Dict[int, ColumnCoreTrace] = {}
    for ci, (c, fp) in enumerate(sorted(files.items())):
        traces_by_core[int(c)] = read_aligned_parquet_columnar(fp["aligned"])
        print(f"[read-columnar] {wd}: core {ci+1}/{len(files)} "
              f"({len(traces_by_core[int(c)])} µops, "
              f"{time.time()-t0:.0f}s)", file=sys.stderr)

    print(f"[build-columnar] {wd}: read done in {time.time()-t0:.0f}s, "
          "annotating/prefixing ...", file=sys.stderr)
    for tr in traces_by_core.values():
        annotate_columnar_trace(tr, rd_window=rd_window)
    annotate_columnar_cross_core(traces_by_core)
    for tr in traces_by_core.values():
        build_columnar_prefixes(tr)

    samples = build_tq_columnar_plans(
        traces_by_core, wd, cfg, tq_max_len,
        target_windows=tq_target_windows,
        stride_tick=tq_stride_tick,
        min_fill=tq_min_fill,
        max_end_skew_cycle=tq_max_end_skew_cycle,
        min_uops_per_core=tq_min_uops_per_core,
        query_placement=query_placement,
        uop_field_schema=uop_field_schema,
    )
    for s in samples:
        _relabel_by_cpi(s)

    dedup_thr = float(dedup_threshold or 0.0)
    if dedup_thr > 0:
        keep_idx, dedup_stats = _dedup_samples(samples, dedup_thr)
        keep_set = set(keep_idx)
        print(f"[dedup] {wd}: {dedup_stats['n_total']} -> "
              f"{dedup_stats['n_kept']} "
              f"(drop={dedup_stats['n_dropped']}) thr={dedup_thr}",
              file=sys.stderr)
    else:
        keep_set = set(range(len(samples)))
        dedup_stats = {
            "n_total": len(samples),
            "n_kept": len(samples),
            "n_dropped": 0,
            "n_no_feature": 0,
            "n_hard_keep": 0,
            "threshold": dedup_thr,
        }

    cap_default, cap_by_name = _parse_per_workload_cap(per_workload_cap)
    cap_rng = random.Random(per_workload_cap_seed)
    groups: Dict[str, List[int]] = defaultdict(list)
    for idx in sorted(keep_set):
        rec = samples[idx]
        key = str(rec.get("workload_variant") or rec.get("workload") or wd)
        groups[key].append(idx)

    selected_idx: List[int] = []
    for key, rows in sorted(groups.items()):
        cap = cap_by_name.get(key, cap_by_name.get(wd, cap_default))
        if cap is None or len(rows) <= cap:
            selected_idx.extend(rows)
            continue
        keep_pos = set(cap_rng.sample(range(len(rows)), cap))
        kept_rows = [idx for pos, idx in enumerate(rows) if pos in keep_pos]
        selected_idx.extend(kept_rows)
        print(f"[cap] workload={wd} variant={key} "
              f"dedup_kept={len(rows)} -> kept={len(kept_rows)} "
              f"(cap={cap})", file=sys.stderr)
    selected_idx.sort()

    label_keys = list(cache_label_keys or PMU_KEYS)
    shared_engine = None
    trace_cache = None
    if shared_state_features:
        shared_engine = shared_engine_from_columnar(traces_by_core)
        trace_cache = columnar_trace_cache(traces_by_core)

    shard_infos = []
    safe_wd = re.sub(r"[^A-Za-z0-9_.-]+", "_", wd)
    chunk_size = max(1, int(cache_shard_size))
    written = 0
    chunk_no = 0
    for start in range(0, len(selected_idx), chunk_size):
        cache_samples = []
        for idx in selected_idx[start:start + chunk_size]:
            cs = _cache_sample_from_columnar_plan(
                samples[idx],
                traces_by_core,
                cfg,
                int(cache_max_len),
                label_keys,
                shared_engine=shared_engine,
                trace_cache=trace_cache,
            )
            if cs is not None:
                cache_samples.append(cs)
        if not cache_samples:
            continue
        blob = build_tensor_cache_shard(cache_samples)
        shard_name = f"shard-{safe_wd}-{chunk_no:05d}.pt"
        _torch_save_atomic(os.path.join(cache_dir, shard_name), blob)
        shard_infos.append({
            "file": shard_name,
            "count": int(blob.get("count", len(cache_samples))),
            "max_n_core": int(blob.get("max_n_core", 0)),
        })
        written += int(blob.get("count", len(cache_samples)))
        chunk_no += 1

    return (
        wd,
        True,
        written,
        shard_infos,
        dedup_stats,
        f"[ok] {wd}: cores={len(files)} samples={len(samples)} "
        f"selected={written} thin=1 columnar=1 prefix=1",
    )


def process_workload_direct_cache(wd: str, raw_root: str, cache_dir: str,
                                  cfg: dict, window: int, stride: int,
                                  dt_tick: int, max_per_core: int,
                                  align_n: int, stride_tick: int,
                                  target_windows: int,
                                  quota_max_len: int,
                                  quota_ratio_lo: float,
                                  quota_ratio_hi: float,
                                  quota_seed: int,
                                  tq_max_len: int,
                                  tq_target_windows: int,
                                  tq_stride_tick: int,
                                  tq_ratio_lo: float,
                                  tq_ratio_hi: float,
                                  tq_min_fill: float = 0.0,
                                  tq_max_end_skew_cycle: float = 0.0,
                                  tq_seed: int = 0,
                                  tq_min_uops_per_core: int = 256,
                                  rd_window: int = 8192,
                                  query_placement: str = "tail",
                                  uop_field_schema: str = "v9",
                                  shared_state_features: bool = False,
                                  macro_snap_max_retreat: int = 32,
                                  cache_max_len: int = 32768,
                                  cache_label_keys: Optional[List[str]] = None,
                                  cache_shard_size: int = 512,
                                  per_workload_cap: Optional[str] = None,
                                  per_workload_cap_seed: int = 0,
                                  dedup_threshold: float = 0.0,
                                  direct_thin_plan: bool = True) -> tuple:
    """Build one workload and write selected samples directly as tensor shards."""
    from train.dataset import build_tensor_cache_shard

    trace_dir = os.path.join(raw_root, wd, "tao_trace")
    if not os.path.isdir(trace_dir):
        return wd, False, 0, [], {}, f"[skip] {wd}: no tao_trace"

    files = load_core_files(trace_dir)
    if len(files) < 1:
        return wd, False, 0, [], {}, f"[skip] {wd}: no cores"

    if (direct_thin_plan and tq_max_len > 0 and quota_max_len <= 0
            and align_n <= 0 and dt_tick <= 0
            and all("aligned" in fp for fp in files.values())):
        return process_workload_direct_cache_columnar(
            wd=wd,
            files=files,
            cache_dir=cache_dir,
            cfg=cfg,
            tq_max_len=tq_max_len,
            tq_target_windows=tq_target_windows,
            tq_stride_tick=tq_stride_tick,
            tq_min_fill=tq_min_fill,
            tq_max_end_skew_cycle=tq_max_end_skew_cycle,
            tq_min_uops_per_core=tq_min_uops_per_core,
            rd_window=rd_window,
            query_placement=query_placement,
            uop_field_schema=uop_field_schema,
            shared_state_features=shared_state_features,
            cache_max_len=cache_max_len,
            cache_label_keys=cache_label_keys,
            cache_shard_size=cache_shard_size,
            per_workload_cap=per_workload_cap,
            per_workload_cap_seed=per_workload_cap_seed,
            dedup_threshold=dedup_threshold,
        )

    t0 = time.time()
    print(f"[start] {wd}: reading {len(files)} cores ...", file=sys.stderr)
    merged_by_core = {}
    for ci, (c, fp) in enumerate(sorted(files.items())):
        if "aligned" in fp:
            merged_by_core[c] = read_aligned_parquet(fp["aligned"])
        else:
            recs = read_jsonl(fp["rec"])
            labs = read_jsonl(fp["lab"])
            merged_by_core[c] = merge_rec_lab(recs, labs)
        print(f"[read] {wd}: core {ci+1}/{len(files)} "
              f"({len(merged_by_core[c])} µops, {time.time()-t0:.0f}s)",
              file=sys.stderr)

    print(f"[build] {wd}: read done in {time.time()-t0:.0f}s, slicing ...",
          file=sys.stderr)
    for seq in merged_by_core.values():
        annotate_rd_stride(seq, rd_window=rd_window)
        annotate_functional_proxies(seq)
    annotate_cross_core_functional_proxies(merged_by_core)

    thin_mode = False
    if (direct_thin_plan and tq_max_len > 0 and quota_max_len <= 0
            and align_n <= 0 and dt_tick <= 0):
        samples = build_tq_thin_plans(
            merged_by_core, wd, cfg, tq_max_len,
            target_windows=tq_target_windows,
            stride_tick=tq_stride_tick,
            min_fill=tq_min_fill,
            max_end_skew_cycle=tq_max_end_skew_cycle,
            min_uops_per_core=tq_min_uops_per_core,
            query_placement=query_placement,
            uop_field_schema=uop_field_schema,
        )
        thin_mode = True
    elif quota_max_len > 0:
        samples = build_samples_quota(
            merged_by_core, wd, cfg, quota_max_len,
            quota_ratio_lo, quota_ratio_hi, rng_seed=quota_seed,
            macro_snap_max_retreat=macro_snap_max_retreat,
        )
    elif align_n > 0:
        samples = build_samples_align(merged_by_core, wd, cfg,
                                      align_n, stride_tick, target_windows)
    elif dt_tick > 0:
        samples = build_samples_timewin(merged_by_core, wd, cfg,
                                        dt_tick, max_per_core)
    elif tq_max_len > 0:
        samples = build_samples_tq(
            merged_by_core, wd, cfg, tq_max_len,
            target_windows=tq_target_windows,
            stride_tick=tq_stride_tick,
            ratio_lo=tq_ratio_lo,
            ratio_hi=tq_ratio_hi,
            min_fill=tq_min_fill,
            max_end_skew_cycle=tq_max_end_skew_cycle,
            min_uops_per_core=tq_min_uops_per_core,
            rng_seed=tq_seed,
            query_placement=query_placement,
            uop_field_schema=uop_field_schema,
            shared_state_features=shared_state_features,
        )
    else:
        samples = build_samples(merged_by_core, wd, cfg, window, stride)

    for s in samples:
        _relabel_by_cpi(s)

    dedup_thr = float(dedup_threshold or 0.0)
    if dedup_thr > 0:
        keep_idx, dedup_stats = _dedup_samples(samples, dedup_thr)
        keep_set = set(keep_idx)
        print(f"[dedup] {wd}: {dedup_stats['n_total']} -> "
              f"{dedup_stats['n_kept']} "
              f"(drop={dedup_stats['n_dropped']}) thr={dedup_thr}",
              file=sys.stderr)
    else:
        keep_set = set(range(len(samples)))
        dedup_stats = {
            "n_total": len(samples),
            "n_kept": len(samples),
            "n_dropped": 0,
            "n_no_feature": 0,
            "n_hard_keep": 0,
            "threshold": dedup_thr,
        }

    cap_default, cap_by_name = _parse_per_workload_cap(per_workload_cap)
    cap_rng = random.Random(per_workload_cap_seed)
    groups: Dict[str, List[int]] = defaultdict(list)
    for idx in sorted(keep_set):
        rec = samples[idx]
        key = str(rec.get("workload_variant") or rec.get("workload") or wd)
        groups[key].append(idx)

    selected_idx: List[int] = []
    for key, rows in sorted(groups.items()):
        cap = cap_by_name.get(key, cap_by_name.get(wd, cap_default))
        if cap is None or len(rows) <= cap:
            selected_idx.extend(rows)
            continue
        keep_pos = set(cap_rng.sample(range(len(rows)), cap))
        kept_rows = [idx for pos, idx in enumerate(rows) if pos in keep_pos]
        selected_idx.extend(kept_rows)
        print(f"[cap] workload={wd} variant={key} "
              f"dedup_kept={len(rows)} -> kept={len(kept_rows)} "
              f"(cap={cap})", file=sys.stderr)
    selected_idx.sort()

    label_keys = list(cache_label_keys or PMU_KEYS)
    shard_infos = []
    safe_wd = re.sub(r"[^A-Za-z0-9_.-]+", "_", wd)
    chunk_size = max(1, int(cache_shard_size))
    written = 0
    chunk_no = 0
    shared_engine = None
    trace_cache = None
    if thin_mode and shared_state_features:
        shared_engine = SharedStateFeatureEngine.from_merged_by_core(
            merged_by_core)
        trace_cache = {
            int(c): build_trace_cache(seq)
            for c, seq in merged_by_core.items()
        }
    for start in range(0, len(selected_idx), chunk_size):
        cache_samples = []
        for idx in selected_idx[start:start + chunk_size]:
            sample = samples[idx]
            if thin_mode:
                cs = _cache_sample_from_tq_plan(
                    sample,
                    merged_by_core,
                    cfg,
                    int(cache_max_len),
                    label_keys,
                    {},
                    shared_engine=shared_engine,
                    trace_cache=trace_cache,
                )
            else:
                cs = _cache_sample_from_window_sample(
                    sample, int(cache_max_len), label_keys)
            if cs is not None:
                cache_samples.append(cs)
        if not cache_samples:
            continue
        blob = build_tensor_cache_shard(cache_samples)
        shard_name = f"shard-{safe_wd}-{chunk_no:05d}.pt"
        _torch_save_atomic(os.path.join(cache_dir, shard_name), blob)
        shard_infos.append({
            "file": shard_name,
            "count": int(blob.get("count", len(cache_samples))),
            "max_n_core": int(blob.get("max_n_core", 0)),
        })
        written += int(blob.get("count", len(cache_samples)))
        chunk_no += 1
    return (
        wd,
        True,
        written,
        shard_infos,
        dedup_stats,
        f"[ok] {wd}: cores={len(files)} samples={len(samples)} "
        f"selected={written} thin={int(thin_mode)}",
    )


FEATURE_SCALAR_KEYS = list(tk.SUMMARY_FEATURE_KEYS)


def _extract_feature_vec(sample: dict) -> Optional[np.ndarray]:
    """从 sample 的 core_summary 提取 v8 36 维特征（per-window 取核 mean）。

    与 scripts/ood_holdout_scan.py 的 extract_sample_vec 保持一致。
    """
    cs_list = sample.get("core_summary") or []
    if not cs_list:
        return None
    scalars: List[List[float]] = [[] for _ in FEATURE_SCALAR_KEYS]
    for cs in cs_list:
        if not isinstance(cs, dict):
            continue
        for i, k in enumerate(FEATURE_SCALAR_KEYS):
            v = cs.get(k, 0.0)
            try:
                scalars[i].append(float(v))
            except Exception:
                scalars[i].append(0.0)
    if not any(scalars):
        return None
    return np.array(
        [float(np.mean(xs)) if xs else 0.0 for xs in scalars],
        dtype=np.float32,
    )


def _dedup_shard(shard_path: str, threshold: float) -> Tuple[List[int], dict]:
    """读取 shard jsonl -> v8 特征 -> per-workload z-score -> 贪心 NN 去重。

    返回 (keep_indices, stats)。
      keep_indices：在 shard 行号空间下，保留的样本序号（已排序）。
      stats：{ 'n_total', 'n_kept', 'n_dropped', 'n_no_feature', 'threshold' }
    """
    vecs: List[np.ndarray] = []
    valid_idx: List[int] = []
    hard_keep_idx: List[int] = []
    n_no_feature = 0
    with open(shard_path) as f:
        for ln_idx, ln in enumerate(f):
            if not ln.startswith("{"):
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            if _dedup_hard_keep_by_cpi(rec):
                hard_keep_idx.append(ln_idx)
                continue
            v = _extract_feature_vec(rec)
            if v is None:
                n_no_feature += 1
                continue
            vecs.append(v)
            valid_idx.append(ln_idx)

    n_total = len(valid_idx)
    hard_keep_set = set(hard_keep_idx)
    total_with_hard = n_total + len(hard_keep_set)
    if n_total == 0:
        return sorted(hard_keep_set), {
            "n_total": total_with_hard,
            "n_kept": len(hard_keep_set),
            "n_dropped": 0,
            "n_no_feature": n_no_feature,
            "n_hard_keep": len(hard_keep_set),
            "threshold": float(threshold),
        }
    if threshold <= 0 or n_total == 1:
        keep = sorted(hard_keep_set | set(valid_idx))
        return keep, {
            "n_total": total_with_hard,
            "n_kept": len(keep),
            "n_dropped": 0,
            "n_no_feature": n_no_feature,
            "n_hard_keep": len(hard_keep_set),
            "threshold": float(threshold),
        }

    X = np.stack(vecs, axis=0)
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd_safe = np.where(sd < 1e-6, 1.0, sd)
    Z = (X - mu) / sd_safe

    thr2 = float(threshold) * float(threshold)
    kept_rows: List[np.ndarray] = []
    keep_indices: List[int] = []
    kept_arr: Optional[np.ndarray] = None
    for i in range(n_total):
        z = Z[i]
        if kept_arr is None:
            kept_rows.append(z)
            kept_arr = z[None, :]
            keep_indices.append(valid_idx[i])
            continue
        diff = kept_arr - z[None, :]
        d2 = (diff * diff).sum(axis=1)
        if float(d2.min()) >= thr2:
            kept_rows.append(z)
            kept_arr = np.stack(kept_rows, axis=0)
            keep_indices.append(valid_idx[i])

    keep = sorted(hard_keep_set | set(keep_indices))
    n_kept = len(keep)
    return keep, {
        "n_total": total_with_hard,
        "n_kept": n_kept,
        "n_dropped": n_total - len(keep_indices),
        "n_no_feature": n_no_feature,
        "n_hard_keep": len(hard_keep_set),
        "threshold": float(threshold),
    }


def _dedup_samples(samples: List[dict], threshold: float) -> Tuple[List[int], dict]:
    """In-memory variant of _dedup_shard for direct tensor-cache builds."""
    vecs: List[np.ndarray] = []
    valid_idx: List[int] = []
    hard_keep_idx: List[int] = []
    n_no_feature = 0
    for idx, rec in enumerate(samples):
        if _dedup_hard_keep_by_cpi(rec):
            hard_keep_idx.append(idx)
            continue
        v = _extract_feature_vec(rec)
        if v is None:
            n_no_feature += 1
            continue
        vecs.append(v)
        valid_idx.append(idx)

    n_total = len(valid_idx)
    hard_keep_set = set(hard_keep_idx)
    total_with_hard = n_total + len(hard_keep_set)
    if n_total == 0:
        return sorted(hard_keep_set), {
            "n_total": total_with_hard,
            "n_kept": len(hard_keep_set),
            "n_dropped": 0,
            "n_no_feature": n_no_feature,
            "n_hard_keep": len(hard_keep_set),
            "threshold": float(threshold),
        }
    if threshold <= 0 or n_total == 1:
        keep = sorted(hard_keep_set | set(valid_idx))
        return keep, {
            "n_total": total_with_hard,
            "n_kept": len(keep),
            "n_dropped": 0,
            "n_no_feature": n_no_feature,
            "n_hard_keep": len(hard_keep_set),
            "threshold": float(threshold),
        }

    X = np.stack(vecs, axis=0)
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd_safe = np.where(sd < 1e-6, 1.0, sd)
    Z = (X - mu) / sd_safe

    thr2 = float(threshold) * float(threshold)
    kept_rows: List[np.ndarray] = []
    keep_indices: List[int] = []
    kept_arr: Optional[np.ndarray] = None
    for i in range(n_total):
        z = Z[i]
        if kept_arr is None:
            kept_rows.append(z)
            kept_arr = z[None, :]
            keep_indices.append(valid_idx[i])
            continue
        diff = kept_arr - z[None, :]
        d2 = (diff * diff).sum(axis=1)
        if float(d2.min()) >= thr2:
            kept_rows.append(z)
            kept_arr = np.stack(kept_rows, axis=0)
            keep_indices.append(valid_idx[i])

    keep = sorted(hard_keep_set | set(keep_indices))
    return keep, {
        "n_total": total_with_hard,
        "n_kept": len(keep),
        "n_dropped": n_total - len(keep_indices),
        "n_no_feature": n_no_feature,
        "n_hard_keep": len(hard_keep_set),
        "threshold": float(threshold),
    }


def _remap_label_rows(sample: dict, wanted_keys: List[str]) -> Optional[List[List[float]]]:
    label_keys = list(sample.get("label_keys") or PMU_KEYS)
    labels = sample.get("label")
    if labels is None:
        return None
    try:
        idx = [label_keys.index(k) for k in wanted_keys]
    except ValueError:
        return None
    out = []
    for row in labels:
        try:
            out.append([float(row[i]) for i in idx])
        except Exception:
            return None
    return out


def _pad_side_feats_for_cache(raw, n_core: int) -> List[List[float]]:
    width = len(tk.SIDE_FEATURE_KEYS)
    if raw is None:
        return [[0.0] * width for _ in range(n_core)]
    out = []
    for ci in range(n_core):
        row = list(raw[ci]) if ci < len(raw) else []
        vals = [float(x) for x in row[:width]]
        if len(vals) < width:
            vals.extend([0.0] * (width - len(vals)))
        out.append(vals)
    return out


def _cache_sample_from_window_sample(sample: dict, max_len: int,
                                     label_keys: List[str]) -> Optional[dict]:
    label = _remap_label_rows(sample, label_keys)
    if label is None:
        return None
    nc = int(sample.get("n_core", 0) or 0)
    if nc <= 0:
        return None
    split_src = sample.get(
        "core_split",
        sample.get("uops_per_core", sample.get("instr_retired", [])),
    )
    core_split = [int(round(float(x))) for x in list(split_src)[:nc]]
    if len(core_split) != nc or any(x < 0 for x in core_split):
        return None
    if sum(core_split) > int(max_len):
        return None
    raw_fields = sample.get("uop_fields")
    raw_is_uop = sample.get("is_uop")
    if raw_fields is None or raw_is_uop is None:
        return None
    field_count = int(sample.get("uop_field_count") or tk.V26_UOP_FIELD_COUNT)
    uop_fields = []
    for row, flag in zip(raw_fields, raw_is_uop):
        if not bool(flag):
            continue
        vals = [int(v) for v in list(row or [])[:field_count]]
        if len(vals) < field_count:
            return None
        uop_fields.append(vals)
    if len(uop_fields) != sum(core_split):
        return None
    return {
        "label": label,
        "n_core": nc,
        "core_split": core_split,
        "instr_retired": sample["instr_retired"][:nc],
        "uops": sample.get("uops_per_core", core_split)[:nc],
        "t_start_rel": sample.get("t_start_rel", [0.0] * nc)[:nc],
        "uop_fields": uop_fields,
        "uop_field_schema": sample.get("uop_field_schema", "v26_14"),
        "uop_field_count": field_count,
        "side_feats": _pad_side_feats_for_cache(sample.get("side_feats"), nc),
        "denoms": sample.get("denoms"),
        "meta": {
            "id": sample.get("id", ""),
            "workload": sample.get("workload", ""),
            "cfg_hash": sample.get("cfg_hash", ""),
            "mode": sample.get("mode", ""),
            "w_ops": sample.get("w_ops", 0),
            "target_fill": sample.get("target_fill", 0.0),
            "fill_ratio": sample.get("fill_ratio", 0.0),
            "t_start_tick": sample.get("t_start_tick", 0),
            "t_end_tick": sample.get("t_end_tick", 0),
            "tq_span_tick": sample.get("tq_span_tick", 0),
            "stride_tick": sample.get("stride_tick", 0),
            "end_skew_cycle": sample.get("end_skew_cycle", 0.0),
        },
    }


def _cache_sample_from_tq_plan(
    plan: dict,
    merged_by_core: Dict[int, List[dict]],
    cfg: dict,
    max_len: int,
    label_keys: List[str],
    uop_field_cache: Dict[int, List[List[int]]],
    shared_engine: Optional[SharedStateFeatureEngine] = None,
    trace_cache: Optional[dict] = None,
) -> Optional[dict]:
    label = _remap_label_rows(plan, label_keys)
    if label is None:
        return None
    cores = [int(c) for c in plan.get("_cores", [])]
    nc = len(cores)
    if nc <= 0:
        return None
    core_split = [int(x) for x in plan.get("core_split", [])[:nc]]
    if len(core_split) != nc or any(x < 0 for x in core_split):
        return None
    if sum(core_split) > int(max_len):
        return None

    spans = {int(c): tuple(plan["_spans"][int(c)]) for c in cores}
    pmu_rows = list(plan.get("_pmu_rows") or [])
    per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
    for ci, c in enumerate(cores):
        start, end = spans[c]
        pmu = pmu_rows[ci] if ci < len(pmu_rows) else {}
        per_core_windows[c] = (merged_by_core[c][start:end], pmu)

    shared_features = None
    if shared_engine is not None:
        shared_engine.advance_to_tick(int(plan.get("t_start_tick", 0) or 0))
        if trace_cache is not None:
            shared_features = shared_engine.window_features_cached(
                spans, trace_cache, cores)
        else:
            shared_features = shared_engine.window_features(
                per_core_windows, cores)

    _global_tokens, side_feats = build_cross_core_features(
        per_core_windows, cores)
    if shared_features:
        key_to_idx = {k: i for i, k in enumerate(tk.SIDE_FEATURE_KEYS)}
        core_feats = shared_features.get("core", {}) or {}
        global_feats = list(shared_features.get("global", []) or [])
        for ci, c in enumerate(cores):
            row = side_feats[ci]
            for name, val in zip(
                    SS_CORE_FEATURE_KEYS,
                    core_feats.get(c, [0.0] * len(SS_CORE_FEATURE_KEYS))):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)
            for name, val in zip(SS_GLOBAL_FEATURE_KEYS, global_feats):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)

    schema = str(plan.get("uop_field_schema") or "v26_14")
    field_count = int(plan.get("uop_field_count")
                      or _uop_field_count_for_schema(schema))
    if schema in {"v26_14", "v27_ss"}:
        static_encoder = tk.encode_uop_fields_v26
    else:
        static_encoder = tk.encode_uop_fields
    uop_fields: List[List[int]] = []
    for c in cores:
        start, end = spans[c]
        cached_rows = uop_field_cache.get(c)
        if cached_rows is not None:
            base_rows = cached_rows[start:end]
            if len(base_rows) != end - start:
                return None
        else:
            base_rows = [
                static_encoder(rec)
                for rec in merged_by_core[c][start:end]
            ]
        if schema == "v27_ss":
            ss_rows = (
                (shared_features or {})
                .get("uop", {})
                .get(c, [])
            )
            if len(ss_rows) < len(base_rows):
                return None
            for base, ss in zip(base_rows, ss_rows):
                row = list(base) + [int(x) for x in list(ss)]
                if len(row) < field_count:
                    return None
                uop_fields.append(row[:field_count])
        else:
            for base in base_rows:
                row = list(base)
                if len(row) < field_count:
                    return None
                uop_fields.append(row[:field_count])
    if len(uop_fields) != sum(core_split):
        return None

    return {
        "label": label,
        "n_core": nc,
        "core_split": core_split,
        "instr_retired": plan["instr_retired"][:nc],
        "uops": plan.get("uops_per_core", core_split)[:nc],
        "t_start_rel": plan.get("t_start_rel", [0.0] * nc)[:nc],
        "uop_fields": uop_fields,
        "uop_field_schema": schema,
        "uop_field_count": field_count,
        "side_feats": _pad_side_feats_for_cache(side_feats, nc),
        "denoms": plan.get("denoms"),
        "meta": {
            "id": plan.get("id", ""),
            "workload": plan.get("workload", ""),
            "cfg_hash": plan.get("cfg_hash", ""),
            "mode": plan.get("mode", ""),
            "w_ops": plan.get("w_ops", 0),
            "target_fill": plan.get("target_fill", 0.0),
            "fill_ratio": plan.get("fill_ratio", 0.0),
            "t_start_tick": plan.get("t_start_tick", 0),
            "t_end_tick": plan.get("t_end_tick", 0),
            "tq_span_tick": plan.get("tq_span_tick", 0),
            "stride_tick": plan.get("stride_tick", 0),
            "end_skew_cycle": plan.get("end_skew_cycle", 0.0),
        },
    }


def columnar_trace_cache(
    traces_by_core: Dict[int, ColumnCoreTrace],
) -> Dict[int, dict]:
    out = {}
    for c, tr in traces_by_core.items():
        out[int(c)] = {
            "line": tr.shared_line_keys(),
            "mem": tr.mem_mask(),
            "store": tr.store_mask(),
        }
    return out


def shared_engine_from_columnar(
    traces_by_core: Dict[int, ColumnCoreTrace],
) -> SharedStateFeatureEngine:
    eng = SharedStateFeatureEngine()
    events = []
    for c, tr in traces_by_core.items():
        mem_idx = np.nonzero(tr.mem_mask())[0]
        ticks = tr.ticks
        line = tr.shared_line_keys()
        store = tr.store_mask()
        load = tr.bool_col("is_load")
        atomic = tr.bool_col("is_atomic")
        micro_seq = tr.col("micro_seq", 0)
        for idx in mem_idx.tolist():
            ln = int(line[idx])
            rec = {
                "is_load": int(load[idx]),
                "is_store": int(store[idx] and not bool(atomic[idx])),
                "is_atomic": int(atomic[idx]),
                "cacheline_addr": int(ln),
                "cacheline_paddr": int(ln),
                "micro_seq": int(micro_seq[idx]),
            }
            events.append((int(ticks[idx]), int(c), rec))
    events.sort(key=lambda x: (x[0], x[1], int(x[2].get("micro_seq", 0) or 0)))
    eng._teacher_events = events
    return eng


def _cache_sample_from_columnar_plan(
    plan: dict,
    traces_by_core: Dict[int, ColumnCoreTrace],
    cfg: dict,
    max_len: int,
    label_keys: List[str],
    shared_engine: Optional[SharedStateFeatureEngine] = None,
    trace_cache: Optional[dict] = None,
) -> Optional[dict]:
    label = _remap_label_rows(plan, label_keys)
    if label is None:
        return None
    cores = [int(c) for c in plan.get("_cores", [])]
    nc = len(cores)
    if nc <= 0:
        return None
    core_split = [int(x) for x in plan.get("core_split", [])[:nc]]
    if len(core_split) != nc or any(x < 0 for x in core_split):
        return None
    if sum(core_split) > int(max_len):
        return None

    spans = {int(c): tuple(plan["_spans"][int(c)]) for c in cores}
    pmu_rows = list(plan.get("_pmu_rows") or [])
    per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
    for ci, c in enumerate(cores):
        start, end = spans[c]
        pmu = pmu_rows[ci] if ci < len(pmu_rows) else {}
        per_core_windows[c] = (
            traces_by_core[c].window_records(start, end),
            pmu,
        )

    shared_features = None
    if shared_engine is not None:
        shared_engine.advance_to_tick(int(plan.get("t_start_tick", 0) or 0))
        if trace_cache is not None:
            shared_features = shared_engine.window_features_cached(
                spans, trace_cache, cores)
        else:
            shared_features = shared_engine.window_features(
                per_core_windows, cores)

    _global_tokens, side_feats = build_cross_core_features(
        per_core_windows, cores)
    if shared_features:
        key_to_idx = {k: i for i, k in enumerate(tk.SIDE_FEATURE_KEYS)}
        core_feats = shared_features.get("core", {}) or {}
        global_feats = list(shared_features.get("global", []) or [])
        for ci, c in enumerate(cores):
            row = side_feats[ci]
            for name, val in zip(
                    SS_CORE_FEATURE_KEYS,
                    core_feats.get(c, [0.0] * len(SS_CORE_FEATURE_KEYS))):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)
            for name, val in zip(SS_GLOBAL_FEATURE_KEYS, global_feats):
                idx = key_to_idx.get(name)
                if idx is not None:
                    row[idx] = float(val)

    schema = str(plan.get("uop_field_schema") or "v26_14")
    field_count = int(plan.get("uop_field_count")
                      or _uop_field_count_for_schema(schema))
    uop_fields: List[List[int]] = []
    for c in cores:
        win, _pmu = per_core_windows[c]
        if schema == "v27_ss":
            ss_rows = (
                (shared_features or {})
                .get("uop", {})
                .get(c, [])
            )
            if len(ss_rows) < len(win):
                return None
            for rec, ss in zip(win, ss_rows):
                rec["_ss_uop_fields"] = ss
                row = tk.encode_uop_fields_v27(rec)
                if len(row) < field_count:
                    return None
                uop_fields.append(row[:field_count])
        elif schema == "v26_14":
            for rec in win:
                row = tk.encode_uop_fields_v26(rec)
                if len(row) < field_count:
                    return None
                uop_fields.append(row[:field_count])
        else:
            for rec in win:
                row = tk.encode_uop_fields(rec)
                if len(row) < field_count:
                    return None
                uop_fields.append(row[:field_count])
    if len(uop_fields) != sum(core_split):
        return None

    return {
        "label": label,
        "n_core": nc,
        "core_split": core_split,
        "instr_retired": plan["instr_retired"][:nc],
        "uops": plan.get("uops_per_core", core_split)[:nc],
        "t_start_rel": plan.get("t_start_rel", [0.0] * nc)[:nc],
        "uop_fields": uop_fields,
        "uop_field_schema": schema,
        "uop_field_count": field_count,
        "side_feats": _pad_side_feats_for_cache(side_feats, nc),
        "denoms": plan.get("denoms"),
        "meta": {
            "id": plan.get("id", ""),
            "workload": plan.get("workload", ""),
            "cfg_hash": plan.get("cfg_hash", ""),
            "mode": plan.get("mode", ""),
            "w_ops": plan.get("w_ops", 0),
            "target_fill": plan.get("target_fill", 0.0),
            "fill_ratio": plan.get("fill_ratio", 0.0),
            "t_start_tick": plan.get("t_start_tick", 0),
            "t_end_tick": plan.get("t_end_tick", 0),
            "tq_span_tick": plan.get("tq_span_tick", 0),
            "stride_tick": plan.get("stride_tick", 0),
            "end_skew_cycle": plan.get("end_skew_cycle", 0.0),
        },
    }


def _torch_save_atomic(path: str, obj: object) -> None:
    import torch

    tmp = path + ".tmp"
    torch.save(obj, tmp)
    os.replace(tmp, path)


def _parse_per_workload_cap(spec: Optional[str]) -> Tuple[Optional[int], Dict[str, int]]:
    if spec is None:
        return None, {}
    spec = spec.strip()
    if not spec:
        return None, {}
    if "=" not in spec and "," not in spec:
        return int(spec), {}
    default_cap: Optional[int] = None
    by_name: Dict[str, int] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            default_cap = int(part)
            continue
        name, val = part.split("=", 1)
        name = name.strip()
        cap = int(val.strip())
        if name == "default":
            default_cap = cap
        else:
            by_name[name] = cap
    return default_cap, by_name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/data00/yinhaolang/LLMSim/data/raw")
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/data/windows")
    ap.add_argument("--window", type=int, default=1024)
    ap.add_argument("--stride", type=int, default=512)
    ap.add_argument("--dt-tick", type=int, default=0,
                    help=">0 启用时间切窗，ΔT 单位 tick（如 100000）")
    ap.add_argument("--max-per-core", type=int, default=0,
                    help="时间切窗每核段指令上限（token 预算），0=不截断")
    ap.add_argument("--align-n", type=int, default=0,
                    help=">0 启用方案A：固定每核指令数 N + 近似时间对齐")
    ap.add_argument("--stride-tick", type=int, default=0,
                    help="方案A 全局时间锚点步长（tick），0=按 target-windows 自适应")
    ap.add_argument("--target-windows", type=int, default=3000,
                    help="方案A 每负载目标窗口数（stride-tick=0 时生效）")
    ap.add_argument("--quota-max-len", type=int, default=0,
                    help=">0 启用方案Q：按 token-budget 装满 + macro 抖动")
    ap.add_argument("--quota-ratio-lo", type=float, default=0.3,
                    help="方案Q 每核归一化前的 ratio 下界（U(lo, hi)）")
    ap.add_argument("--quota-ratio-hi", type=float, default=1.7,
                    help="方案Q 每核归一化前的 ratio 上界")
    ap.add_argument("--quota-seed", type=int, default=0,
                    help="方案Q 抖动随机种子")
    ap.add_argument("--tq-max-len", type=int, default=32768,
                    help="默认最终方案TQ：tail-aligned quota 的 max-len；0=关闭")
    ap.add_argument("--tq-target-windows", type=int, default=1200,
                    help="方案TQ 每负载目标窗口数（tq-stride-tick=0 时生效）")
    ap.add_argument("--tq-stride-tick", type=int, default=0,
                    help="方案TQ 全局尾部时间锚点步长，0=按 tq-target-windows 自适应")
    ap.add_argument("--tq-ratio-lo", type=float, default=0.5,
                    help="兼容旧参数；v9 min-uops floor 后不再使用")
    ap.add_argument("--tq-ratio-hi", type=float, default=2.0,
                    help="兼容旧参数；v9 min-uops floor 后不再使用")
    ap.add_argument("--tq-min-fill", type=float, default=0.0,
                    help="方案TQ 低于该 fill_ratio 的训练窗丢弃；v9 默认不按满窗过滤")
    ap.add_argument("--tq-max-end-skew-cycle", type=float, default=0.0,
                    help="方案TQ 尾部 commit 时间最大偏斜；0=只记录不丢弃")
    ap.add_argument("--tq-seed", type=int, default=0,
                    help="兼容旧参数；v9 min-uops floor 后不再使用")
    ap.add_argument("--tq-min-uops-per-core", type=int, default=256,
                    help="v9 TQ 每核最小 uop 数；默认 256，不再尽量填满上下文")
    ap.add_argument("--rd-window", type=int, default=8192,
                    help="bounded sliding RD 窗口，单位是每核 memory reference 数")
    ap.add_argument("--query-placement", choices=["tail", "segment", "tail_local"],
                    default="tail",
                    help="tail=v9: queries after TRACE_END; "
                         "segment=v15: each QUERY_Ci before Ci_END; "
                         "tail_local=v16: LOCAL_Ci in segment plus tail queries")
    ap.add_argument("--uop-field-schema", choices=["v9", "v26_14", "v27_ss"],
                    default="v9",
                    help="Structured UOP field schema written to windows.jsonl. "
                         "Use v26_14 for v26 clean training, or v27_ss for "
                         "shared-state features; both require rebuilding "
                         "windows and tensor cache.")
    ap.add_argument("--shared-state-features", action="store_true",
                    help="Enable lagged shared-system features. Current "
                         "implementation uses functional cacheline owner/"
                         "sharer/history state and writes v27_ss fields.")
    ap.add_argument("--macro-snap-max-retreat", type=int, default=32,
                    help="切窗时 macro-snap 最大回退 µop 数。>0：短 macro 依然"
                         "snap 到边界（保 cpi_macro 精度），超长 macro"
                         "（REP/gather/microcode 展开出的几千条 µop）触发保护"
                         "在 µop 边界收口，避免跨核 T_end 大偏斜。"
                         "0=完全禁用 macro-snap。<0=保持旧行为无上限回退。"
                         "只影响 quota 路径（build_samples_quota）；TQ 主路径"
                         "本来就在 µop 边界切。")
    ap.add_argument("--no-cache", action="store_true",
                    help="跳过自动生成 ids cache（仅产 jsonl）")
    ap.add_argument("--cache-max-len", type=int, default=0,
                    help="ids cache 的 max-len；默认取 quota-max-len 或 tq-max-len")
    ap.add_argument("--direct-tensor-cache", action="store_true",
                    help="直接输出 v26/v27 tensor cache，不写完整 windows.jsonl。"
                         "会写一个小 stub windows.jsonl 仅用于训练侧 meta 校验。")
    ap.add_argument("--direct-cache-out", default=None,
                    help="--direct-tensor-cache 输出目录；默认 "
                         "<out>/windows.maxlen${max_len}.tensor_cache")
    ap.add_argument("--direct-cache-shard-size", type=int, default=512,
                    help="direct tensor cache 每个 .pt shard 的样本数")
    ap.add_argument("--no-direct-thin-plan", action="store_true",
                    help="Disable thin-plan optimization in direct TQ tensor "
                         "cache builds. Intended for correctness/benchmark "
                         "comparison against the legacy direct path.")
    ap.add_argument("--cache-label-keys", default=None,
                    help="direct tensor cache 存储的 label key 逗号列表")
    ap.add_argument("--workloads", nargs="*", default=None)
    ap.add_argument("--uarch-config", default="arch_A")
    ap.add_argument("--per-workload-cap", default=None,
                    help="按 workload 限制写入 jsonl 的窗口数。支持:\n"
                         "  - 单一整数: 应用到全部 workload，例如 1200\n"
                         "  - name=N 列表，逗号分隔，例如 W_phased_mix=1200,W_chase_dram=1500\n"
                         "  - default=N 可作为兜底值，与 name=N 同存")
    ap.add_argument("--per-workload-cap-seed", type=int, default=0,
                    help="--per-workload-cap 均匀下采样随机种子")
    ap.add_argument("--dedup-threshold", type=float, default=0.0,
                    help="窗口去重 L2 阈值（per-workload z-score 空间）。"
                         "0=关闭；推荐 0.05。在 per-workload cap 之前执行。")
    ap.add_argument("--dedup-jobs", type=int, default=0,
                    help="去重并行 workload 数；0=与 --jobs 一致")
    ap.add_argument("--dedup-report",
                    default=None,
                    help="去重统计 JSON 输出路径；默认 <out>/dedup_report.json")
    ap.add_argument("--jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    args = ap.parse_args()
    if args.shared_state_features and args.uop_field_schema != "v27_ss":
        raise SystemExit(
            "--shared-state-features requires --uop-field-schema v27_ss"
        )

    import yaml
    cfg_path = "/data00/yinhaolang/LLMSim/config/uarch_configs.yaml"
    with open(cfg_path) as f:
        all_cfg = yaml.safe_load(f)
    cfg = all_cfg["configs"][args.uarch_config]

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "windows.jsonl")
    shard_dir = os.path.join(args.out, ".shards")
    wdirs = sorted([d for d in os.listdir(args.raw)
                    if os.path.isdir(os.path.join(args.raw, d))
                    and d.startswith("W") and not d.startswith("probe_")])
    if args.workloads:
        wdirs = [d for d in wdirs if d in args.workloads]

    if not wdirs:
        with open(out_path, "w"):
            pass
        print(f"[done] total samples=0 -> {out_path}")
        return

    if args.direct_tensor_cache:
        from train.dataset import (
            MANIFEST_NAME,
            TENSOR_CACHE_FORMAT,
            build_cache_meta,
        )

        cache_max_len = (
            args.cache_max_len
            or args.quota_max_len
            or args.tq_max_len
            or 0
        )
        if cache_max_len <= 0:
            raise SystemExit(
                "--direct-tensor-cache requires --cache-max-len, "
                "--quota-max-len, or --tq-max-len"
            )
        if args.uop_field_schema == "v27_ss":
            direct_field_count = tk.V27_UOP_FIELD_COUNT
        elif args.uop_field_schema == "v26_14":
            direct_field_count = tk.V26_UOP_FIELD_COUNT
        else:
            direct_field_count = tk.V9_UOP_FIELD_COUNT
        label_keys = (
            [x.strip() for x in args.cache_label_keys.split(",") if x.strip()]
            if args.cache_label_keys else list(PMU_KEYS)
        )
        cache_dir = args.direct_cache_out or (
            f"{out_path[:-6]}.maxlen{cache_max_len}.tensor_cache"
        )
        if os.path.isdir(cache_dir):
            shutil.rmtree(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)
        with open(out_path, "w") as f:
            json.dump({
                "direct_tensor_cache": True,
                "uop_field_schema": args.uop_field_schema,
                "uop_field_count": int(direct_field_count),
                "label_keys": label_keys,
            }, f, separators=(",", ":"))
            f.write("\n")

        jobs = max(1, min(args.jobs, len(wdirs)))
        dedup_thr = float(args.dedup_threshold or 0.0)
        shards = []
        total = 0
        dedup_stats: Dict[str, dict] = {}
        print(f"[direct-cache] out={cache_dir} jobs={jobs} "
              f"shard_size={args.direct_cache_shard_size} "
              f"label_keys={','.join(label_keys)}")
        with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
            future_map = {
                ex.submit(
                    process_workload_direct_cache,
                    wd, args.raw, cache_dir,
                    cfg, args.window, args.stride,
                    args.dt_tick, args.max_per_core,
                    args.align_n, args.stride_tick,
                    args.target_windows,
                    args.quota_max_len,
                    args.quota_ratio_lo, args.quota_ratio_hi,
                    args.quota_seed,
                    args.tq_max_len,
                    args.tq_target_windows,
                    args.tq_stride_tick,
                    args.tq_ratio_lo,
                    args.tq_ratio_hi,
                    args.tq_min_fill,
                    args.tq_max_end_skew_cycle,
                    args.tq_seed,
                    args.tq_min_uops_per_core,
                    args.rd_window,
                    args.query_placement,
                    args.uop_field_schema,
                    args.shared_state_features,
                    args.macro_snap_max_retreat,
                    int(cache_max_len),
                    label_keys,
                    args.direct_cache_shard_size,
                    args.per_workload_cap,
                    args.per_workload_cap_seed,
                    dedup_thr,
                    not args.no_direct_thin_plan,
                ): wd
                for wd in wdirs
            }
            for fut in cf.as_completed(future_map):
                wd, ok, nsamp, shard_infos, stats, msg = fut.result()
                stream = sys.stdout if ok else sys.stderr
                print(msg, file=stream)
                if not ok:
                    continue
                total += int(nsamp)
                shards.extend(shard_infos)
                if stats:
                    dedup_stats[wd] = stats

        shards.sort(key=lambda x: x["file"])
        meta = build_cache_meta(
            out_path,
            int(cache_max_len),
            max_cores=tk.MAX_CORES,
            label_keys=label_keys,
        )
        _torch_save_atomic(os.path.join(cache_dir, MANIFEST_NAME), {
            "format": TENSOR_CACHE_FORMAT,
            "meta": meta,
            "total_samples": int(total),
            "shards": shards,
        })
        if dedup_thr > 0:
            report_path = args.dedup_report or os.path.join(
                args.out, "dedup_report.json")
            os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
            total_orig = sum(s["n_total"] for s in dedup_stats.values())
            total_kept = sum(s["n_kept"] for s in dedup_stats.values())
            with open(report_path, "w") as f:
                json.dump({
                    "threshold": dedup_thr,
                    "total_pre_dedup": total_orig,
                    "total_post_dedup": total_kept,
                    "per_workload": dedup_stats,
                }, f, indent=2)
            print(f"[dedup] report -> {report_path} "
                  f"(pre={total_orig} post={total_kept})")
        print(f"[done] direct tensor samples={total} -> {cache_dir}")
        print(f"[done] stub windows={out_path}")
        return

    if os.path.isdir(shard_dir):
        shutil.rmtree(shard_dir)
    os.makedirs(shard_dir, exist_ok=True)

    total = 0
    results = {}
    jobs = max(1, min(args.jobs, len(wdirs)))
    with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
        future_map = {
            ex.submit(process_workload, wd, args.raw, shard_dir,
                      cfg, args.window, args.stride,
                      args.dt_tick, args.max_per_core,
                      args.align_n, args.stride_tick,
                      args.target_windows,
                      args.quota_max_len,
                      args.quota_ratio_lo, args.quota_ratio_hi,
                      args.quota_seed,
                      args.tq_max_len,
                      args.tq_target_windows,
                      args.tq_stride_tick,
                      args.tq_ratio_lo,
                      args.tq_ratio_hi,
                      args.tq_min_fill,
                      args.tq_max_end_skew_cycle,
                      args.tq_seed,
                      args.tq_min_uops_per_core,
                      args.rd_window,
                      args.query_placement,
                      args.uop_field_schema,
                      args.shared_state_features,
                      args.macro_snap_max_retreat): wd
            for wd in wdirs
        }
        for fut in cf.as_completed(future_map):
            wd, ok, nsamp, shard_path, msg = fut.result()
            results[wd] = (ok, nsamp, shard_path, msg)
            stream = sys.stdout if ok else sys.stderr
            print(msg, file=stream)

    dedup_thr = float(args.dedup_threshold or 0.0)
    dedup_jobs = int(args.dedup_jobs or args.jobs)
    dedup_jobs = max(1, dedup_jobs)
    dedup_keep: Dict[str, Optional[set]] = {}
    dedup_stats: Dict[str, dict] = {}
    if dedup_thr > 0:
        dedup_tasks = [
            (wd, results[wd][2])
            for wd in wdirs
            if results[wd][0] and results[wd][1] > 0
        ]
        print(f"[dedup] threshold={dedup_thr} jobs={dedup_jobs} "
              f"workloads={len(dedup_tasks)}")
        with cf.ProcessPoolExecutor(max_workers=dedup_jobs) as ex:
            futs = {
                ex.submit(_dedup_shard, sp, dedup_thr): wd
                for wd, sp in dedup_tasks
            }
            for fut in cf.as_completed(futs):
                wd = futs[fut]
                keep_idx, stats = fut.result()
                dedup_keep[wd] = set(keep_idx)
                dedup_stats[wd] = stats
                frac = (stats["n_dropped"] / stats["n_total"]
                        if stats["n_total"] else 0.0)
                print(f"[dedup] {wd}: {stats['n_total']} -> "
                      f"{stats['n_kept']} (drop={stats['n_dropped']}, "
                      f"{frac:.1%}) thr={dedup_thr}")

    with open(out_path, "w") as fout:
        cap_default, cap_by_name = _parse_per_workload_cap(args.per_workload_cap)
        cap_rng = random.Random(args.per_workload_cap_seed)
        for wd in wdirs:
            ok, nsamp, shard_path, _ = results[wd]
            if not ok:
                continue
            keep_set: Optional[set] = dedup_keep.get(wd) if dedup_thr > 0 else None
            groups: Dict[str, List[int]] = defaultdict(list)
            with open(shard_path) as fin:
                for idx, line in enumerate(fin):
                    if keep_set is not None and idx not in keep_set:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        groups[wd].append(idx)
                        continue
                    key = str(
                        rec.get("workload_variant")
                        or rec.get("workload")
                        or wd
                    )
                    groups[key].append(idx)

            selected_idx: set = set()
            for key, rows in sorted(groups.items()):
                cap = cap_by_name.get(key, cap_by_name.get(wd, cap_default))
                if cap is None or len(rows) <= cap:
                    selected_idx.update(rows)
                    continue
                keep_pos = set(cap_rng.sample(range(len(rows)), cap))
                kept_idx = [idx for pos, idx in enumerate(rows)
                            if pos in keep_pos]
                selected_idx.update(kept_idx)
                print(f"[cap] workload={wd} variant={key} "
                      f"dedup_kept={len(rows)} -> kept={len(kept_idx)} "
                      f"(cap={cap})")

            with open(shard_path) as fin:
                for idx, line in enumerate(fin):
                    if idx in selected_idx:
                        fout.write(line)
            total += len(selected_idx)

    if dedup_thr > 0:
        report_path = args.dedup_report or os.path.join(args.out, "dedup_report.json")
        os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
        total_orig = sum(s["n_total"] for s in dedup_stats.values())
        total_kept = sum(s["n_kept"] for s in dedup_stats.values())
        with open(report_path, "w") as f:
            json.dump({
                "threshold": dedup_thr,
                "total_pre_dedup": total_orig,
                "total_post_dedup": total_kept,
                "per_workload": dedup_stats,
            }, f, indent=2)
        print(f"[dedup] report -> {report_path} "
              f"(pre={total_orig} post={total_kept})")

    shutil.rmtree(shard_dir, ignore_errors=True)
    print(f"[done] total samples={total} -> {out_path}")

    if total > 0 and not args.no_cache:
        cache_max_len = args.cache_max_len or args.quota_max_len or args.tq_max_len or 0
        if cache_max_len <= 0:
            print("[cache] skip: 无法推断 max-len；指定 --cache-max-len、--quota-max-len 或 --tq-max-len",
                  file=sys.stderr)
        else:
            import subprocess
            repo_root = "/data00/yinhaolang/LLMSim"
            cache_dir = f"{out_path[:-6]}.maxlen{cache_max_len}.ids_cache"
            print(f"[cache] preparing ids cache @ max-len={cache_max_len} -> {cache_dir}",
                  flush=True)
            cmd = [
                sys.executable,
                f"{repo_root}/scripts/prepare_dataset_cache.py",
                "--data", out_path,
                "--max-len", str(cache_max_len),
            ]
            rc = subprocess.run(cmd, cwd=repo_root).returncode
            if rc != 0:
                print(f"[cache] FAILED rc={rc}", file=sys.stderr)
                sys.exit(rc)
            print(f"[cache] done -> {cache_dir}")


if __name__ == "__main__":
    main()
