#!/usr/bin/env python3
"""Validate functional address-stream features against window CPI.

This is an offline diagnostic script. It does not change tokenizer/cache/model
files. It replays the current quota windowing policy, derives two layers of
functional-only candidate features, and reports how much each feature explains
window CPI.

Layer 1 candidates:
  - current slot4/5 proxies: hashed vline/vpage window statistics
  - replacement slot4: exact cache-line reuse-distance bucket
  - replacement slot5: cache-line stride bucket

Layer 2 candidates:
  - window-level conditioning: distinct lines/pages, memory mix, RD/stride
    histograms, normalized workset size

Input-feature rule: feature derivation only uses functional trace fields
(`vaddr`, `cacheline_addr`, `size`, `is_load`, `is_store`, `is_atomic` and
program-order fields). `commit_tick` is used only to compute the evaluation
label CPI.
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures as cf
import csv
import glob
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from model import tokenizer as tk  # noqa: E402


CORE_RE = re.compile(r"(?:cores|switch)(\d+)\.core")
ALIGNED_PARQUET_COLS = [
    "core_id", "thread_id", "micro_seq", "seq_num",
    "macro_pc", "micro_pc", "vaddr", "paddr",
    "cacheline_addr", "cacheline_paddr", "size",
    "is_load", "is_store", "is_atomic",
    "is_branch", "is_branch_cond", "is_branch_indirect",
    "is_call", "is_return", "is_int", "is_fp",
    "is_simd", "is_serialize", "is_microop", "is_last_microop",
    "n_src", "n_dst", "producer_dists", "producer_classes",
    "commit_tick", "mispredicted",
]


RD_LABELS = [
    "nonmem",
    "cold",
    "le8",
    "le64",
    "le512",
    "le4k",
    "le32k",
    "le256k",
    "gt256k",
]

STRIDE_LABELS = [
    "nonmem",
    "first",
    "same_line",
    "plus1",
    "minus1",
    "plus2_8",
    "minus2_8",
    "plus9_64",
    "minus9_64",
    "large",
]


def load_core_files(trace_dir: str) -> Dict[int, dict]:
    out: Dict[int, dict] = {}
    for p in glob.glob(os.path.join(trace_dir, "*.aligned.parquet")):
        m = CORE_RE.search(p)
        if m:
            out.setdefault(int(m.group(1)), {})["aligned"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.records.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            out.setdefault(int(m.group(1)), {})["rec"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.labels.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            out.setdefault(int(m.group(1)), {})["lab"] = p

    keep: Dict[int, dict] = {}
    for core, paths in out.items():
        if "aligned" in paths:
            keep[core] = {"aligned": paths["aligned"]}
        elif "rec" in paths and "lab" in paths:
            keep[core] = {"rec": paths["rec"], "lab": paths["lab"]}
    return keep


def read_jsonl(path: str) -> List[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s.startswith("{"):
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                continue
    return rows


def read_aligned_parquet(path: str) -> List[dict]:
    try:
        import pyarrow.parquet as pq
    except ModuleNotFoundError as e:
        raise ModuleNotFoundError(
            "reading *.aligned.parquet requires pyarrow; use jsonl raw traces "
            "or install pyarrow in this Python environment"
        ) from e

    rows = []
    pf = pq.ParquetFile(path)
    cols = [c for c in ALIGNED_PARQUET_COLS if c in pf.schema.names]
    for batch in pf.iter_batches(columns=cols, batch_size=65536):
        for row in batch.to_pylist():
            row["_commit_tick"] = row.get("commit_tick", 0)
            row["_mispredicted"] = row.get("mispredicted", 0)
            rows.append(row)
    return rows


def merge_rec_lab(recs: List[dict], labs: List[dict]) -> List[dict]:
    lab_idx = {(r["thread_id"], r["micro_seq"]): r for r in labs}
    merged = []
    for rec in recs:
        lab = lab_idx.get((rec["thread_id"], rec["micro_seq"]))
        if lab is None:
            continue
        rec["_commit_tick"] = lab.get("commit_tick", 0)
        rec["_mispredicted"] = lab.get("mispredicted", 0)
        merged.append(rec)
    merged.sort(key=lambda x: (x["thread_id"], x["micro_seq"]))
    return merged


def is_macro_head(rec: dict, prev: Optional[dict]) -> bool:
    if prev is None:
        return True
    prev_ended = (prev.get("is_microop", 0) == 0
                  or prev.get("is_last_microop", 0) == 1)
    return prev_ended or rec.get("macro_pc") != prev.get("macro_pc")


def take_macro_window_by_budget(seq: List[dict], start: int,
                                budget_tok: int) -> Tuple[int, int]:
    n = len(seq)
    if start >= n:
        return start, 0
    prev = seq[start - 1] if start > 0 else None
    tok = 0
    macro_n = 0
    last_safe_end = start
    last_safe_macros = 0
    i = start
    while i < n:
        rec = seq[i]
        head = is_macro_head(rec, prev)
        if head and i > start:
            last_safe_end = i
            last_safe_macros = macro_n
        tok_len = len(tk.encode_uop(rec))
        if tok + tok_len > budget_tok and last_safe_end > start:
            return last_safe_end, last_safe_macros
        tok += tok_len
        if head:
            macro_n += 1
        prev = rec
        i += 1
    if macro_n >= 1:
        return i, macro_n
    return last_safe_end, last_safe_macros


def _prev_macro_end(seq: List[dict], end: int) -> int:
    end = min(end, len(seq))
    while end > 0:
        rec = seq[end - 1]
        if rec.get("is_microop", 0) == 0 or rec.get("is_last_microop", 0) == 1:
            break
        end -= 1
    return end


def _macro_start(seq: List[dict], end: int) -> int:
    i = end - 1
    while i > 0 and not is_macro_head(seq[i], seq[i - 1]):
        i -= 1
    return i


def take_macro_window_back_by_budget(seq: List[dict], end: int,
                                     budget_tok: int) -> Tuple[int, int, int]:
    end = _prev_macro_end(seq, end)
    if end <= 0:
        return end, end, 0
    start = end
    tok = 0
    macro_n = 0
    while start > 0:
        m_start = _macro_start(seq, start)
        macro_tok = sum(len(tk.encode_uop(rec)) for rec in seq[m_start:start])
        if tok + macro_tok > budget_tok and macro_n > 0:
            break
        if tok + macro_tok > budget_tok:
            return start, end, 0
        tok += macro_tok
        macro_n += 1
        start = m_start
    return start, end, macro_n


def sample_tq_fill(rng: random.Random) -> float:
    u = rng.random()
    if u < 0.70:
        return rng.uniform(0.92, 1.00)
    if u < 0.95:
        return rng.uniform(0.85, 0.92)
    return rng.uniform(0.75, 0.85)


class Fenwick:
    def __init__(self, n: int):
        self.n = n
        self.bit = [0] * (n + 2)

    def add(self, idx0: int, delta: int) -> None:
        i = idx0 + 1
        while i <= self.n + 1:
            self.bit[i] += delta
            i += i & -i

    def sum(self, idx0: int) -> int:
        if idx0 < 0:
            return 0
        i = idx0 + 1
        out = 0
        while i > 0:
            out += self.bit[i]
            i -= i & -i
        return out

    def range_sum(self, lo0: int, hi0: int) -> int:
        if hi0 < lo0:
            return 0
        return self.sum(hi0) - self.sum(lo0 - 1)


def is_mem(rec: dict) -> bool:
    return bool(rec.get("is_load") or rec.get("is_store") or rec.get("is_atomic"))


def line_addr(rec: dict) -> Optional[int]:
    if not is_mem(rec):
        return None
    for key in ("cacheline_addr", "cacheline_paddr"):
        v = rec.get(key)
        if v not in (None, 0, "0"):
            return int(v)
    vaddr = int(rec.get("vaddr", 0) or 0)
    if vaddr:
        return vaddr >> 6
    paddr = int(rec.get("paddr", 0) or 0)
    if paddr:
        return paddr >> 6
    return None


def page_addr(rec: dict) -> Optional[int]:
    if not is_mem(rec):
        return None
    vaddr = int(rec.get("vaddr", 0) or 0)
    if vaddr:
        return vaddr >> 12
    paddr = int(rec.get("paddr", 0) or 0)
    if paddr:
        return paddr >> 12
    line = line_addr(rec)
    if line is not None:
        return line >> 6
    return None


def rd_bucket(rd: Optional[int]) -> int:
    if rd is None:
        return 0
    if rd < 0:
        return 1
    if rd <= 8:
        return 2
    if rd <= 64:
        return 3
    if rd <= 512:
        return 4
    if rd <= 4096:
        return 5
    if rd <= 32768:
        return 6
    if rd <= 262144:
        return 7
    return 8


def stride_bucket(delta: Optional[int]) -> int:
    if delta is None:
        return 0
    if delta == 10**30:
        return 1
    if delta == 0:
        return 2
    if delta == 1:
        return 3
    if delta == -1:
        return 4
    if 2 <= delta <= 8:
        return 5
    if -8 <= delta <= -2:
        return 6
    if 9 <= delta <= 64:
        return 7
    if -64 <= delta <= -9:
        return 8
    return 9


def annotate_addr_stream(seq: List[dict], rd_window: int) -> None:
    """Add functional-only bounded sliding RD/stride/hash annotations.

    RD is exact only within the latest rd_window memory references. Older
    reuses are mapped to the far bucket. This matches model context limits and
    keeps preprocessing at O(log rd_window) per memory op.
    """
    mem_positions: List[Tuple[int, int]] = []
    for i, rec in enumerate(seq):
        line = line_addr(rec)
        if line is not None:
            mem_positions.append((i, line))

    bit = Fenwick(len(mem_positions) + 1)
    last_pos: Dict[int, int] = {}
    seen_lines = set()
    last_line: Optional[int] = None
    expire_i = 0

    mem_i = 0
    for seq_i, line in mem_positions:
        rec = seq[seq_i]
        if rd_window > 0:
            expire_before = mem_i - rd_window
            while expire_i < expire_before:
                old_line = mem_positions[expire_i][1]
                if last_pos.get(old_line) == expire_i:
                    bit.add(expire_i, -1)
                    del last_pos[old_line]
                expire_i += 1

        prev_pos = last_pos.get(line)
        if prev_pos is None:
            rd = -1 if line not in seen_lines else 10**12
        else:
            rd = bit.range_sum(prev_pos + 1, mem_i - 1)
        if prev_pos is not None:
            bit.add(prev_pos, -1)
        bit.add(mem_i, 1)
        last_pos[line] = mem_i
        seen_lines.add(line)

        delta = 10**30 if last_line is None else line - last_line
        last_line = line

        rec["_addr_line"] = line
        rec["_addr_page"] = page_addr(rec)
        rec["_rd_bucket"] = rd_bucket(rd)
        rec["_stride_bucket"] = stride_bucket(delta)
        rec["_vline_bucket"] = tk.vline_bucket(rec)
        rec["_vpage_bucket"] = tk.vpage_bucket(rec)
        mem_i += 1

    for rec in seq:
        if "_rd_bucket" not in rec:
            rec["_addr_line"] = None
            rec["_addr_page"] = None
            rec["_rd_bucket"] = 0
            rec["_stride_bucket"] = 0
            rec["_vline_bucket"] = tk.vline_bucket(rec)
            rec["_vpage_bucket"] = tk.vpage_bucket(rec)


def entropy_norm(values: Sequence[int]) -> float:
    if not values:
        return 0.0
    c = Counter(values)
    n = float(len(values))
    h = -sum((v / n) * math.log(v / n) for v in c.values())
    return h / math.log(max(len(c), 2))


def ratio_count(counter: Counter, key: int, denom: int) -> float:
    return float(counter.get(key, 0)) / float(denom) if denom > 0 else 0.0


def cpi_label(win: List[dict], tick_per_cycle: int) -> Optional[Tuple[float, int, float]]:
    if len(win) < 2:
        return None
    ticks = [int(w.get("_commit_tick", 0) or 0) for w in win]
    if any(t <= 0 for t in ticks):
        return None
    cycles = (max(ticks) - min(ticks)) / float(tick_per_cycle)
    if cycles <= 0:
        return None

    instr = 0
    prev = None
    for rec in win:
        if is_macro_head(rec, prev):
            instr += 1
        prev = rec
    if instr <= 0:
        return None
    return cycles / float(instr), instr, cycles


def window_features(win: List[dict]) -> Dict[str, float]:
    n_uop = len(win)
    mem = [r for r in win if is_mem(r)]
    n_mem = len(mem)
    n_load = sum(1 for r in mem if r.get("is_load"))
    n_store = sum(1 for r in mem if r.get("is_store"))
    lines = [r.get("_addr_line") for r in mem if r.get("_addr_line") is not None]
    pages = [r.get("_addr_page") for r in mem if r.get("_addr_page") is not None]
    vl = [int(r.get("_vline_bucket", 0)) for r in mem]
    vp = [int(r.get("_vpage_bucket", 0)) for r in mem]
    rd = Counter(int(r.get("_rd_bucket", 0)) for r in win)
    st = Counter(int(r.get("_stride_bucket", 0)) for r in win)

    out: Dict[str, float] = {
        "basic.uops": float(n_uop),
        "basic.mem_ratio": float(n_mem) / float(max(n_uop, 1)),
        "basic.load_frac_mem": float(n_load) / float(max(n_mem, 1)),
        "basic.store_frac_mem": float(n_store) / float(max(n_mem, 1)),
        "current_hash.vline_unique_frac": float(len(set(vl))) / float(max(n_mem, 1)),
        "current_hash.vpage_unique_frac": float(len(set(vp))) / float(max(n_mem, 1)),
        "current_hash.vline_entropy_norm": entropy_norm(vl),
        "current_hash.vpage_entropy_norm": entropy_norm(vp),
        "layer2.distinct_lines": float(len(set(lines))),
        "layer2.distinct_pages": float(len(set(pages))),
        "layer2.lines_per_kuop": 1000.0 * float(len(set(lines))) / float(max(n_uop, 1)),
        "layer2.pages_per_kuop": 1000.0 * float(len(set(pages))) / float(max(n_uop, 1)),
        "layer2.lines_per_mem": float(len(set(lines))) / float(max(n_mem, 1)),
        "layer2.pages_per_mem": float(len(set(pages))) / float(max(n_mem, 1)),
    }
    for i, name in enumerate(RD_LABELS):
        out[f"layer1.rd_{name}_frac"] = ratio_count(rd, i, n_uop)
    for i, name in enumerate(STRIDE_LABELS):
        out[f"layer1.stride_{name}_frac"] = ratio_count(st, i, n_uop)
    return out


def load_core_seq(paths: dict, max_uops: int, rd_window: int) -> List[dict]:
    if "aligned" in paths:
        seq = read_aligned_parquet(paths["aligned"])
    else:
        seq = merge_rec_lab(read_jsonl(paths["rec"]), read_jsonl(paths["lab"]))
    seq.sort(key=lambda x: (int(x.get("thread_id", 0)), int(x.get("micro_seq", 0))))
    if max_uops > 0:
        seq = seq[:max_uops]
    annotate_addr_stream(seq, rd_window)
    return seq


@dataclass
class WorkloadResult:
    rows: List[dict]
    windows: int
    dropped: int


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s.strip("/"))


def replay_quota_workload(
    raw_root: str,
    workload: str,
    max_len: int,
    ratio_lo: float,
    ratio_hi: float,
    overhead: int,
    budget_frac: float,
    seed: int,
    tick_per_cycle: int,
    max_windows: int,
    max_uops_per_core: int,
    rd_window: int,
) -> WorkloadResult:
    trace_dir = os.path.join(raw_root, workload, "tao_trace")
    core_files = load_core_files(trace_dir)
    if not core_files:
        raise FileNotFoundError(f"no core trace files under {trace_dir}")

    seq_by_core = {
        c: load_core_seq(paths, max_uops_per_core, rd_window)
        for c, paths in sorted(core_files.items())
    }
    cores = sorted(seq_by_core)
    total_budget = int((max_len - overhead) * budget_frac)
    rng = random.Random(seed)
    cursor = {c: 0 for c in cores}
    rows: List[dict] = []
    seg = 0
    dropped = 0

    while True:
        ratios = [rng.uniform(ratio_lo, ratio_hi) for _ in cores]
        ratio_sum = sum(ratios)
        per_core: Dict[int, Tuple[List[dict], float, int, float]] = {}
        ok = True
        for ci, c in enumerate(cores):
            budget_c = max(1, int(round(total_budget * ratios[ci] / ratio_sum)))
            seq = seq_by_core[c]
            end, got = take_macro_window_by_budget(seq, cursor[c], budget_c)
            if got < 1 or end <= cursor[c]:
                ok = False
                break
            win = seq[cursor[c]:end]
            lab = cpi_label(win, tick_per_cycle)
            if lab is None:
                ok = False
                break
            cpi, instr, cycles = lab
            per_core[c] = (win, cpi, instr, cycles)

        if not ok:
            dropped += 1
            break

        for c, (win, cpi, instr, cycles) in per_core.items():
            feats = window_features(win)
            row = {
                "raw_root": os.path.basename(raw_root.rstrip("/")),
                "workload": workload,
                "seg": seg,
                "core": c,
                "cpi": cpi,
                "instr": instr,
                "cycles": cycles,
            }
            row.update(feats)
            rows.append(row)
        for c, (win, _cpi, _instr, _cycles) in per_core.items():
            cursor[c] += len(win)

        seg += 1
        if max_windows > 0 and seg >= max_windows:
            break

    return WorkloadResult(rows=rows, windows=seg, dropped=dropped)


def replay_tq_workload(
    raw_root: str,
    workload: str,
    max_len: int,
    ratio_lo: float,
    ratio_hi: float,
    overhead: int,
    budget_frac: float,
    seed: int,
    tick_per_cycle: int,
    max_windows: int,
    max_uops_per_core: int,
    target_windows: int,
    min_fill: float,
    rd_window: int,
) -> WorkloadResult:
    trace_dir = os.path.join(raw_root, workload, "tao_trace")
    core_files = load_core_files(trace_dir)
    if not core_files:
        raise FileNotFoundError(f"no core trace files under {trace_dir}")

    seq_by_core = {
        c: load_core_seq(paths, max_uops_per_core, rd_window)
        for c, paths in sorted(core_files.items())
    }
    cores = sorted(seq_by_core)
    ticks = {
        c: [int(r.get("_commit_tick", 0) or 0) for r in seq_by_core[c]]
        for c in cores
    }
    valid_ticks = {c: [t for t in ticks[c] if t > 0] for c in cores}
    if any(len(valid_ticks[c]) < 2 for c in cores):
        return WorkloadResult(rows=[], windows=0, dropped=1)

    t_lo = max(valid_ticks[c][0] for c in cores)
    t_hi = min(valid_ticks[c][-1] for c in cores)
    if t_hi <= t_lo:
        return WorkloadResult(rows=[], windows=0, dropped=1)
    if target_windows <= 0:
        target_windows = max_windows if max_windows > 0 else 1200
    stride_tick = max(1, int((t_hi - t_lo) / max(target_windows, 1)))

    base_budget = int((max_len - overhead) * budget_frac)
    rng = random.Random(seed)
    rows: List[dict] = []
    seg = 0
    dropped = 0
    k = 0

    while True:
        if max_windows > 0 and seg >= max_windows:
            break
        T_end = t_lo + k * stride_tick
        if T_end > t_hi:
            break
        k += 1

        target_fill = sample_tq_fill(rng)
        total_budget = max(1, int(base_budget * target_fill))
        ratios = [rng.uniform(ratio_lo, ratio_hi) for _ in cores]
        ratio_sum = sum(ratios)
        per_core: Dict[int, Tuple[List[dict], float, int, float]] = {}
        ok = True
        for ci, c in enumerate(cores):
            budget_c = max(1, int(round(total_budget * ratios[ci] / ratio_sum)))
            seq = seq_by_core[c]
            all_ticks = ticks[c]
            end = bisect.bisect_right(all_ticks, T_end)
            start, end, got = take_macro_window_back_by_budget(seq, end, budget_c)
            if got < 1 or end <= start:
                ok = False
                break
            win = seq[start:end]
            lab = cpi_label(win, tick_per_cycle)
            if lab is None:
                ok = False
                break
            cpi, instr, cycles = lab
            per_core[c] = (win, cpi, instr, cycles)

        if not ok:
            dropped += 1
            continue

        tokens_total = 1 + 4 + 1 + len(cores) * 2 + len(cores)
        tokens_total += sum(6 * len(per_core[c][0]) for c in cores)
        fill_ratio = tokens_total / float(max_len)
        if tokens_total > max_len or fill_ratio < min_fill:
            dropped += 1
            continue
        t_ends = [
            max(int(r.get("_commit_tick", 0) or 0) for r in per_core[c][0])
            for c in cores
        ]
        end_skew_cycle = (max(t_ends) - min(t_ends)) / float(tick_per_cycle)

        for c, (win, cpi, instr, cycles) in per_core.items():
            feats = window_features(win)
            row = {
                "raw_root": os.path.basename(raw_root.rstrip("/")),
                "workload": workload,
                "seg": seg,
                "core": c,
                "cpi": cpi,
                "instr": instr,
                "cycles": cycles,
                "cut.mode": "tq",
                "cut.fill_ratio": fill_ratio,
                "cut.target_fill": target_fill,
                "cut.end_skew_cycle": end_skew_cycle,
            }
            row.update(feats)
            rows.append(row)
        seg += 1

    return WorkloadResult(rows=rows, windows=seg, dropped=dropped)


def discover_workloads(raw_root: str) -> List[str]:
    if not os.path.isdir(raw_root):
        return []
    out = []
    for name in sorted(os.listdir(raw_root)):
        if os.path.isdir(os.path.join(raw_root, name, "tao_trace")):
            out.append(name)
    return out


def write_feature_rows(rows: List[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")


def process_task(task: dict) -> dict:
    t0 = time.time()
    raw_root = task["raw_root"]
    workload = task["workload"]
    if task["mode"] == "tq":
        res = replay_tq_workload(
            raw_root=raw_root,
            workload=workload,
            max_len=task["max_len"],
            ratio_lo=task["ratio_lo"],
            ratio_hi=task["ratio_hi"],
            overhead=task["overhead"],
            budget_frac=task["budget_frac"],
            seed=task["seed"],
            tick_per_cycle=task["tick_per_cycle"],
            max_windows=task["max_windows"],
            max_uops_per_core=task["max_uops_per_core"],
            target_windows=task["tq_target_windows"],
            min_fill=task["tq_min_fill"],
            rd_window=task["rd_window"],
        )
    else:
        res = replay_quota_workload(
            raw_root=raw_root,
            workload=workload,
            max_len=task["max_len"],
            ratio_lo=task["ratio_lo"],
            ratio_hi=task["ratio_hi"],
            overhead=task["overhead"],
            budget_frac=task["budget_frac"],
            seed=task["seed"],
            tick_per_cycle=task["tick_per_cycle"],
            max_windows=task["max_windows"],
            max_uops_per_core=task["max_uops_per_core"],
            rd_window=task["rd_window"],
        )

    root_name = safe_name(os.path.basename(raw_root.rstrip("/")) or raw_root)
    feature_path = os.path.join(
        task["work_dir"], f"{root_name}.{safe_name(workload)}.features.jsonl")
    write_feature_rows(res.rows, feature_path)
    return {
        "raw_root": raw_root,
        "workload": workload,
        "mode": task["mode"],
        "windows": res.windows,
        "rows": len(res.rows),
        "dropped": res.dropped,
        "feature_path": feature_path,
        "elapsed_s": time.time() - t0,
    }


def pearson(x: Sequence[float], y: Sequence[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    vx = sum((v - mx) ** 2 for v in x)
    vy = sum((v - my) ** 2 for v in y)
    if vx <= 0 or vy <= 0:
        return 0.0
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    return cov / math.sqrt(vx * vy)


def ranks(vals: Sequence[float]) -> List[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and vals[order[j]] == vals[order[i]]:
            j += 1
        r = (i + j - 1) / 2.0
        for k in range(i, j):
            out[order[k]] = r
        i = j
    return out


def quantile_bins(vals: Sequence[float], n_bins: int) -> List[int]:
    if not vals:
        return []
    uniq = sorted(set(vals))
    if len(uniq) <= 1:
        return [0] * len(vals)
    sorted_vals = sorted(vals)
    cuts = []
    for i in range(1, n_bins):
        idx = min(len(sorted_vals) - 1, max(0, int(round(i * len(sorted_vals) / n_bins))))
        cuts.append(sorted_vals[idx])
    out = []
    for v in vals:
        b = 0
        while b < len(cuts) and v > cuts[b]:
            b += 1
        out.append(b)
    return out


def normalized_mi(x: Sequence[float], y: Sequence[float], bins: int) -> float:
    xb = quantile_bins(x, bins)
    yb = quantile_bins(y, bins)
    if len(set(xb)) <= 1 or len(set(yb)) <= 1:
        return 0.0
    n = float(len(xb))
    cx = Counter(xb)
    cy = Counter(yb)
    cxy = Counter(zip(xb, yb))
    mi = 0.0
    for (a, b), cnt in cxy.items():
        pxy = cnt / n
        px = cx[a] / n
        py = cy[b] / n
        mi += pxy * math.log(pxy / (px * py))
    hy = -sum((cnt / n) * math.log(cnt / n) for cnt in cy.values())
    return mi / hy if hy > 0 else 0.0


def feature_group(name: str) -> str:
    if name.startswith("current_hash."):
        return "current_hash_slot4_5"
    if name.startswith("layer1.rd_"):
        return "layer1_rd_slot4_candidate"
    if name.startswith("layer1.stride_"):
        return "layer1_stride_slot5_candidate"
    if name.startswith("layer2."):
        return "layer2_window_conditioning"
    return "basic"


def write_metrics(rows: List[dict], out_csv: str, mi_bins: int) -> List[dict]:
    if not rows:
        raise RuntimeError("no feature rows generated")
    y = [float(r["cpi"]) for r in rows]
    feature_names = sorted(k for k in rows[0].keys()
                           if k not in {
                               "raw_root", "workload", "seg", "core",
                               "cpi", "instr", "cycles",
                               "cut.mode", "cut.fill_ratio",
                               "cut.target_fill", "cut.end_skew_cycle",
                           })
    metrics = []
    for name in feature_names:
        x = [float(r.get(name, 0.0)) for r in rows]
        p = pearson(x, y)
        s = pearson(ranks(x), ranks(y))
        mi = normalized_mi(x, y, mi_bins)
        metrics.append({
            "feature": name,
            "group": feature_group(name),
            "n": len(rows),
            "pearson": p,
            "spearman": s,
            "nmi_to_cpi": mi,
            "abs_spearman": abs(s),
        })
    metrics.sort(key=lambda r: (r["abs_spearman"], r["nmi_to_cpi"]), reverse=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(metrics[0].keys()))
        w.writeheader()
        w.writerows(metrics)
    return metrics


def write_group_summary(metrics: List[dict], out_csv: str) -> None:
    groups = sorted(set(m["group"] for m in metrics))
    rows = []
    for g in groups:
        ms = [m for m in metrics if m["group"] == g]
        top = max(ms, key=lambda m: (m["abs_spearman"], m["nmi_to_cpi"]))
        rows.append({
            "group": g,
            "n_features": len(ms),
            "top_feature": top["feature"],
            "top_abs_spearman": top["abs_spearman"],
            "top_nmi_to_cpi": top["nmi_to_cpi"],
            "mean_abs_spearman": sum(m["abs_spearman"] for m in ms) / len(ms),
            "mean_nmi_to_cpi": sum(m["nmi_to_cpi"] for m in ms) / len(ms),
        })
    rows.sort(key=lambda r: (r["top_abs_spearman"], r["top_nmi_to_cpi"]), reverse=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def default_roots() -> List[str]:
    candidates = [
        os.path.join(ROOT, "data/raw_train11_8c_500k"),
        os.path.join(ROOT, "data/raw_train12_mlp_light_8c_500k_ff"),
    ]
    return [p for p in candidates if os.path.isdir(p)]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["tq", "quota"], default="tq",
                    help="window replay mode; default uses final tail-aligned quota.")
    ap.add_argument("--raw-root", action="append", default=[],
                    help="raw trace root; can be repeated. Default: train11 + mlp_light if present.")
    ap.add_argument("--workload", action="append", default=[],
                    help="workload name; can be repeated. Default: all discovered workloads.")
    ap.add_argument("--out-dir", default=os.path.join(ROOT, "logs/addr_feature_validation"))
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--ratio-lo", type=float, default=0.5)
    ap.add_argument("--ratio-hi", type=float, default=2.0)
    ap.add_argument("--overhead", type=int, default=64)
    ap.add_argument("--budget-frac", type=float, default=0.95)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tick-per-cycle", type=int, default=333)
    ap.add_argument("--max-windows", type=int, default=200,
                    help="max replayed windows per workload; 0 means all.")
    ap.add_argument("--tq-target-windows", type=int, default=0,
                    help="TQ time anchors per workload; default=max-windows or 1200.")
    ap.add_argument("--tq-min-fill", type=float, default=0.70,
                    help="drop TQ windows below this fill ratio.")
    ap.add_argument("--max-uops-per-core", type=int, default=0,
                    help="debug limiter; 0 means all uops.")
    ap.add_argument("--rd-window", type=int, default=8192,
                    help="bounded sliding reuse-distance window in memory refs; <=0 means exact full-trace RD.")
    ap.add_argument("--jobs", type=int, default=2,
                    help="parallel workload workers. Keep modest because each worker reads 8-core traces.")
    ap.add_argument("--mi-bins", type=int, default=8)
    ap.add_argument("--write-features", action="store_true",
                    help="also write per-core-window feature rows JSONL.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    raw_roots = args.raw_root or default_roots()
    if not raw_roots:
        raise SystemExit("no --raw-root provided and no default raw roots found")
    os.makedirs(args.out_dir, exist_ok=True)
    work_dir = os.path.join(args.out_dir, ".work")
    os.makedirs(work_dir, exist_ok=True)

    tasks = []
    for raw_root in raw_roots:
        workloads = args.workload or discover_workloads(raw_root)
        if not workloads:
            print(f"[warn] no workloads found under {raw_root}", file=sys.stderr)
            continue
        for workload in workloads:
            tasks.append({
                "raw_root": raw_root,
                "workload": workload,
                "mode": args.mode,
                "max_len": args.max_len,
                "ratio_lo": args.ratio_lo,
                "ratio_hi": args.ratio_hi,
                "overhead": args.overhead,
                "budget_frac": args.budget_frac,
                "seed": args.seed,
                "tick_per_cycle": args.tick_per_cycle,
                "max_windows": args.max_windows,
                "max_uops_per_core": args.max_uops_per_core,
                "tq_target_windows": args.tq_target_windows,
                "tq_min_fill": args.tq_min_fill,
                "rd_window": args.rd_window,
                "work_dir": work_dir,
            })

    if not tasks:
        raise SystemExit("no workload tasks discovered")

    run_summary = []
    feature_paths = []
    jobs = max(1, min(args.jobs, len(tasks)))
    print(f"[start] mode={args.mode} rd_window={args.rd_window} "
          f"tasks={len(tasks)} jobs={jobs} work_dir={work_dir}",
          flush=True)
    with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
        future_map = {}
        for i, task in enumerate(tasks, 1):
            print(f"[queue] {i}/{len(tasks)} "
                  f"{os.path.basename(task['raw_root'].rstrip('/'))}/{task['workload']}",
                  flush=True)
            future_map[ex.submit(process_task, task)] = task

        done = 0
        for fut in cf.as_completed(future_map):
            task = future_map[fut]
            done += 1
            try:
                item = fut.result()
            except Exception as e:
                print(f"[warn] {done}/{len(tasks)} failed "
                      f"{task['raw_root']}/{task['workload']}: {e}",
                      file=sys.stderr, flush=True)
                continue
            run_summary.append(item)
            feature_paths.append(item["feature_path"])
            print(f"[ok] {done}/{len(tasks)} "
                  f"{os.path.basename(item['raw_root'].rstrip('/'))}/{item['workload']} "
                  f"windows={item['windows']} rows={item['rows']} "
                  f"dropped={item['dropped']} elapsed={item['elapsed_s']:.1f}s "
                  f"features={item['feature_path']}",
                  flush=True)

    all_rows: List[dict] = []
    for path in feature_paths:
        with open(path) as f:
            for line in f:
                s = line.strip()
                if s:
                    all_rows.append(json.loads(s))

    if not all_rows:
        raise SystemExit("no rows generated")

    metrics_csv = os.path.join(args.out_dir, "metrics.csv")
    group_csv = os.path.join(args.out_dir, "group_summary.csv")
    summary_json = os.path.join(args.out_dir, "run_summary.json")
    metrics = write_metrics(all_rows, metrics_csv, args.mi_bins)
    write_group_summary(metrics, group_csv)
    with open(summary_json, "w") as f:
        json.dump(run_summary, f, indent=2)
    if args.write_features:
        feat_jsonl = os.path.join(args.out_dir, "features.jsonl")
        with open(feat_jsonl, "w") as f:
            for path in feature_paths:
                with open(path) as fin:
                    for line in fin:
                        f.write(line)

    print(f"[done] rows={len(all_rows)}")
    print(f"[done] metrics={metrics_csv}")
    print(f"[done] group_summary={group_csv}")
    print(f"[done] run_summary={summary_json}")
    print("[top10]")
    for m in metrics[:10]:
        print(f"{m['group']},{m['feature']},"
              f"spearman={m['spearman']:.4f},"
              f"pearson={m['pearson']:.4f},"
              f"nmi={m['nmi_to_cpi']:.4f}")


if __name__ == "__main__":
    main()
