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
                                budget_tok: int) -> Tuple[int, int]:
    """按程序序累加真实 encode_uop 长度，装到 budget_tok 即收口到上一条
    完整 macro 边界。返回 (end_index, got_macro)。

    与 tpm 反算不同：每条 µop 的 token 长度精确测量，不依赖估计；
    收口对齐 macro head，保证窗口不切半条 macro（与 aggregate_pmu/部署一致）。
    """
    n = len(seq)
    if start >= n:
        return start, 0
    prev = seq[start - 1] if start > 0 else None
    tok = 0
    macro_n = 0
    last_safe_end = start          # 上一条完整 macro 收尾位置（含）
    last_safe_macros = 0           # 到 last_safe_end 时累计的完整 macro 数
    i = start
    while i < n:
        rec = seq[i]
        is_head = is_macro_head(rec, prev)
        if is_head and i > start:
            # 进入新 macro：当前 i 之前的 µop 构成一段完整 macro
            last_safe_end = i
            last_safe_macros = macro_n
        tok_len = len(tk.encode_uop(rec))
        if tok + tok_len > budget_tok and last_safe_end > start:
            # 装不下当前 µop，且已有至少一条完整 macro，停在 last_safe_end
            return last_safe_end, last_safe_macros
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


def _prev_macro_end(seq: List[dict], end: int) -> int:
    """Return an exclusive end index that does not cut the tail macro."""
    end = min(end, len(seq))
    while end > 0:
        rec = seq[end - 1]
        if rec.get("is_microop", 0) == 0 or rec.get("is_last_microop", 0) == 1:
            break
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
                                     uop_token_cost: int = 6
                                     ) -> Tuple[int, int, int]:
    """Backward quota window ending at a macro boundary.

    Returns (start_index, end_index, got_macro). The selected slice
    seq[start:end] is as long as possible under budget_tok and starts/ends on
    dynamic macro boundaries. This keeps the most recent context before a
    tail-time anchor.
    """
    end = _prev_macro_end(seq, end)
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
                            uop_field_schema: str = "v9") -> dict:
    """Shared sample serialization for multi-core window builders."""
    if query_placement not in {"tail", "segment", "tail_local"}:
        raise ValueError(f"unknown query_placement={query_placement!r}")
    if uop_field_schema not in {"v9", "v26_14"}:
        raise ValueError(f"unknown uop_field_schema={uop_field_schema!r}")
    field_count = (
        tk.V26_UOP_FIELD_COUNT
        if uop_field_schema == "v26_14" else tk.V9_UOP_FIELD_COUNT
    )
    global_tokens, side_feats = build_cross_core_features(
        per_core_windows, cores)
    out_tokens: List[str] = []
    is_uop: List[int] = []
    uop_fields: List[List[int]] = []

    def append_token(tok: str) -> None:
        out_tokens.append(tok)
        is_uop.append(0)
        uop_fields.append([0] * field_count)

    def append_uop(rec: dict) -> None:
        out_tokens.append("<UOP>")
        is_uop.append(1)
        if uop_field_schema == "v26_14":
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
        for w in win:
            append_uop(w)
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
                     uop_field_schema: str = "v9") -> List[dict]:
    """方案TQ：tail-aligned quota，最终默认切窗策略。

    - 以全局 T_end 为锚点，每核取 commit_tick <= T_end 的最后完整 macro
      作为窗口尾部，使窗口尾部在物理时间上尽可能对齐。
    - 从尾部向前扩展一个公共时间跨度，直到每个 core 至少有 256 uop。
    - 不是每核固定 256 uop；快核/高吞吐核在同一时间跨度内可以更多。
    - 不再为了填满 max_len 扩大窗口；max_len 只作为安全上界。
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


def build_samples_quota(merged_by_core: Dict[int, List[dict]], wname: str,
                        cfg: dict, max_len: int,
                        ratio_lo: float, ratio_hi: float,
                        overhead: int = 64, budget_frac: float = 0.95,
                        rng_seed: int = 0) -> List[dict]:
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
            end, got = take_macro_window_by_budget(seq, cursor[c], budget_c)
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
                     uop_field_schema: str = "v9") -> tuple:
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
        )
    else:
        samples = build_samples(merged_by_core, wd, cfg, window, stride)
    shard_path = os.path.join(out_dir, f"{wd}.jsonl")
    with open(shard_path, "w") as fout:
        for s in samples:
            fout.write(json.dumps(s, separators=(",", ":")) + "\n")
    return wd, True, len(samples), shard_path, f"[ok] {wd}: cores={len(files)} samples={len(samples)}"


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
    n_no_feature = 0
    with open(shard_path) as f:
        for ln_idx, ln in enumerate(f):
            if not ln.startswith("{"):
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            v = _extract_feature_vec(rec)
            if v is None:
                n_no_feature += 1
                continue
            vecs.append(v)
            valid_idx.append(ln_idx)

    n_total = len(valid_idx)
    if n_total == 0:
        return [], {
            "n_total": 0,
            "n_kept": 0,
            "n_dropped": 0,
            "n_no_feature": n_no_feature,
            "threshold": float(threshold),
        }
    if threshold <= 0 or n_total == 1:
        return list(valid_idx), {
            "n_total": n_total,
            "n_kept": n_total,
            "n_dropped": 0,
            "n_no_feature": n_no_feature,
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

    n_kept = len(keep_indices)
    return keep_indices, {
        "n_total": n_total,
        "n_kept": n_kept,
        "n_dropped": n_total - n_kept,
        "n_no_feature": n_no_feature,
        "threshold": float(threshold),
    }


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
    ap.add_argument("--uop-field-schema", choices=["v9", "v26_14"],
                    default="v9",
                    help="Structured UOP field schema written to windows.jsonl. "
                         "Use v26_14 for v26 clean training; it requires "
                         "rebuilding windows and tensor cache.")
    ap.add_argument("--no-cache", action="store_true",
                    help="跳过自动生成 ids cache（仅产 jsonl）")
    ap.add_argument("--cache-max-len", type=int, default=0,
                    help="ids cache 的 max-len；默认取 quota-max-len 或 tq-max-len")
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
                      args.uop_field_schema): wd
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
            effective_n = len(keep_set) if keep_set is not None else nsamp
            cap = cap_by_name.get(wd, cap_default)
            if cap is None or effective_n <= cap:
                # 写所有 dedup-kept 行（或所有行，若关闭 dedup）
                if keep_set is None:
                    with open(shard_path) as fin:
                        shutil.copyfileobj(fin, fout)
                    total += nsamp
                else:
                    written = 0
                    with open(shard_path) as fin:
                        for idx, line in enumerate(fin):
                            if idx in keep_set:
                                fout.write(line)
                                written += 1
                    total += written
                continue
            # 需要 cap 下采样
            if keep_set is None:
                keep = sorted(cap_rng.sample(range(nsamp), cap))
            else:
                keep = sorted(cap_rng.sample(sorted(keep_set), cap))
            keep_idx_set = set(keep)
            written = 0
            with open(shard_path) as fin:
                for idx, line in enumerate(fin):
                    if idx in keep_idx_set:
                        fout.write(line)
                        written += 1
            print(f"[cap] workload={wd} nsamp={nsamp} "
                  f"dedup_kept={effective_n} -> kept={written} (cap={cap})")
            total += written

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
