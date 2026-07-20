"""Trace ROI baseline utilities.

The raw gem5 stats.txt may include setup/drain cycles outside the traced ROI.
These helpers derive the baseline from the trace itself: per-core commit_tick
endpoints for cycles and functional macro-head counting for instructions.
"""
from __future__ import annotations

import math
import os
import re
from typing import Dict, List, Optional, Tuple

import pyarrow.parquet as pq

from data.build_windows import (
    is_macro_head,
    load_core_files,
    merge_rec_lab,
    read_aligned_parquet,
    read_jsonl,
)


_CORE_NUMCYC = re.compile(r"(?:cores|switch)(\d+)\.core\.numCycles\s+([0-9.]+)")
_CORE_INSTS = re.compile(r"(?:cores|switch)(\d+)\.core\.commitStats0\.numInsts\s+([0-9.]+)")
ROI_COLS = [
    "thread_id", "micro_seq", "macro_pc", "is_microop", "is_last_microop",
    "commit_tick",
]


def parse_gem5_stats(stats_path: str) -> Tuple[float, float, float]:
    """Parse full-run gem5 stats.txt CPI for reference only."""
    cyc, ins = {}, {}
    with open(stats_path) as f:
        for ln in f:
            m = _CORE_NUMCYC.search(ln)
            if m:
                cyc[int(m.group(1))] = float(m.group(2))
                continue
            m = _CORE_INSTS.search(ln)
            if m:
                ins[int(m.group(1))] = float(m.group(2))
    sum_c = sum(cyc.values())
    sum_i = sum(ins.values())
    return sum_c, sum_i, (sum_c / sum_i if sum_i > 0 else float("nan"))


def load_workload_rows(trace_dir: str,
                       max_rows_per_core: int = 0) -> Dict[int, List[dict]]:
    """Load aligned parquet if available, otherwise merge raw rec/lab jsonl."""
    files = load_core_files(trace_dir)
    if len(files) < 2:
        raise RuntimeError(f"{trace_dir}: <2 cores")
    merged = {}
    for c, fp in sorted(files.items()):
        if "aligned" in fp:
            merged[c] = read_aligned_parquet(
                fp["aligned"], max_rows=max_rows_per_core)
        else:
            recs = read_jsonl(fp["rec"], max_rows=max_rows_per_core)
            labs = read_jsonl(fp["lab"], max_rows=max_rows_per_core)
            merged[c] = merge_rec_lab(recs, labs)
    return merged


def count_macros(win: List[dict], prev: Optional[dict] = None) -> int:
    """Count macro instructions from rec fields, independent of commit_tick."""
    n = 0
    prev_local = prev
    for r in win:
        if is_macro_head(r, prev_local):
            n += 1
        prev_local = r
    return n


def compute_trace_roi_stats(merged: Dict[int, List[dict]],
                            tick_per_cycle: int,
                            t_start_global_tick: int = 0) -> dict:
    """Derive ROI stats directly from trace rows.

    When ``t_start_global_tick > 0``, rows whose commit_tick is below the
    threshold are treated as pre-ROI warmup and excluded from cycles/instr
    accounting. Per-core first_tick is floored at ``t_start_global_tick``.
    """
    per_core = {}
    sum_cycles = 0.0
    sum_instr = 0.0
    sum_uops = 0.0
    missing_label_uops = 0
    t_floor = int(t_start_global_tick)
    for c, seq in sorted(merged.items()):
        macro = 0
        uops = 0
        valid_ticks: List[int] = []
        ct0 = 0
        prev = None
        for r in seq:
            ct = int(r.get("_commit_tick", r.get("commit_tick", 0)) or 0)
            is_head = is_macro_head(r, prev)
            if ct <= 0:
                ct0 += 1
            elif ct >= t_floor:
                valid_ticks.append(ct)
                uops += 1
                if is_head:
                    macro += 1
            prev = r
        cycles = 0.0
        first_tick = last_tick = 0
        if len(valid_ticks) >= 2:
            first_tick = min(valid_ticks)
            last_tick = max(valid_ticks)
            cycles = (last_tick - first_tick) / float(tick_per_cycle)
        per_core[c] = {
            "cycles": cycles,
            "instr": float(macro),
            "uops": float(uops),
            "cpi": cycles / macro if macro > 0 else float("nan"),
            "cpi_macro": cycles / macro if macro > 0 else float("nan"),
            "cpi_uop": cycles / uops if uops > 0 else float("nan"),
            "first_tick": first_tick,
            "last_tick": last_tick,
            "missing_label_uops": ct0,
            "rows": len(seq),
        }
        sum_cycles += cycles
        sum_instr += float(macro)
        sum_uops += float(uops)
        missing_label_uops += ct0
    return {
        "cycles": sum_cycles,
        "instr": sum_instr,
        "uops": sum_uops,
        "cpi": sum_cycles / sum_instr if sum_instr > 0 else float("nan"),
        "cpi_macro": sum_cycles / sum_instr if sum_instr > 0 else float("nan"),
        "cpi_uop": sum_cycles / sum_uops if sum_uops > 0 else float("nan"),
        "missing_label_uops": missing_label_uops,
        "per_core": per_core,
    }


def _finalize_core_stats(macro: int, uops: int, first_tick: int, last_tick: int,
                         missing_label_uops: int, rows: int,
                         tick_per_cycle: int) -> dict:
    cycles = 0.0
    if first_tick > 0 and last_tick > first_tick:
        cycles = (last_tick - first_tick) / float(tick_per_cycle)
    return {
        "cycles": cycles,
        "instr": float(macro),
        "uops": float(uops),
        "cpi": cycles / macro if macro > 0 else float("nan"),
        "cpi_macro": cycles / macro if macro > 0 else float("nan"),
        "cpi_uop": cycles / uops if uops > 0 else float("nan"),
        "first_tick": first_tick,
        "last_tick": last_tick,
        "missing_label_uops": missing_label_uops,
        "rows": rows,
    }


def _combine_per_core(per_core: Dict[int, dict]) -> dict:
    sum_cycles = sum(v["cycles"] for v in per_core.values())
    sum_instr = sum(v["instr"] for v in per_core.values())
    sum_uops = sum(v.get("uops", 0.0) for v in per_core.values())
    missing_label_uops = sum(v["missing_label_uops"] for v in per_core.values())
    return {
        "cycles": sum_cycles,
        "instr": sum_instr,
        "uops": sum_uops,
        "cpi": sum_cycles / sum_instr if sum_instr > 0 else float("nan"),
        "cpi_macro": sum_cycles / sum_instr if sum_instr > 0 else float("nan"),
        "cpi_uop": sum_cycles / sum_uops if sum_uops > 0 else float("nan"),
        "missing_label_uops": missing_label_uops,
        "per_core": per_core,
    }


def _attach_gem5_reference(roi: dict, stats_path: str | None) -> dict:
    if stats_path and os.path.isfile(stats_path):
        g_cyc, g_ins, g_cpi = parse_gem5_stats(stats_path)
        roi["gem5_full"] = {
            "cycles": g_cyc,
            "instr": g_ins,
            "cpi": g_cpi,
            "cpi_delta_vs_roi": g_cpi - roi["cpi"],
            "cpi_relerr_vs_roi": (
                abs(g_cpi - roi["cpi"]) / abs(roi["cpi"])
                if not math.isnan(roi["cpi"]) and roi["cpi"] != 0 else float("nan")
            ),
        }
    return roi


def compute_trace_roi_stats_streaming(trace_dir: str,
                                      tick_per_cycle: int) -> dict:
    """Stream aligned parquet files to derive ROI stats without loading traces."""
    files = load_core_files(trace_dir)
    if len(files) < 2:
        raise RuntimeError(f"{trace_dir}: <2 cores")
    per_core = {}
    for c, fp in sorted(files.items()):
        if "aligned" not in fp:
            # Fallback keeps compatibility for raw jsonl-only traces. Large
            # current train8 workloads have aligned parquet and use streaming.
            rows = load_workload_rows(trace_dir)
            return compute_trace_roi_stats(rows, tick_per_cycle)
        pf = pq.ParquetFile(fp["aligned"])
        macro = 0
        uops = 0
        rows = 0
        missing_label_uops = 0
        first_tick = 0
        last_tick = 0
        prev = None
        for batch in pf.iter_batches(columns=ROI_COLS, batch_size=262144):
            for r in batch.to_pylist():
                rows += 1
                ct = int(r.get("commit_tick", 0) or 0)
                if ct <= 0:
                    missing_label_uops += 1
                else:
                    first_tick = ct if first_tick == 0 else min(first_tick, ct)
                    last_tick = max(last_tick, ct)
                    uops += 1
                if is_macro_head(r, prev):
                    macro += 1
                prev = r
        per_core[c] = _finalize_core_stats(
            macro=macro,
            uops=uops,
            first_tick=first_tick,
            last_tick=last_tick,
            missing_label_uops=missing_label_uops,
            rows=rows,
            tick_per_cycle=tick_per_cycle,
        )
    return _combine_per_core(per_core)


def compute_workload_roi_stats(trace_dir: str, tick_per_cycle: int,
                               stats_path: str | None = None) -> dict:
    """Compute trace ROI stats and optionally attach full gem5 stats reference."""
    roi = compute_trace_roi_stats_streaming(trace_dir, tick_per_cycle)
    return _attach_gem5_reference(roi, stats_path)
