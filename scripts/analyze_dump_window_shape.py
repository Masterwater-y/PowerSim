#!/usr/bin/env python3
"""Summarize functional instruction shape for eval window dumps."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.build_windows import is_macro_head  # noqa: E402
from data.roi_stats import load_workload_rows  # noqa: E402


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--trace-dir", required=True)
    return ap.parse_args()


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def main() -> None:
    args = parse_args()
    rows = [json.loads(l) for l in open(args.dump) if l.strip()]
    rows.sort(key=lambda r: float(r.get("progress_after", 0.0) or 0.0))
    merged = load_workload_rows(args.trace_dir)
    print(
        "d prog pred label resid macro uops uop_per_macro "
        "mem% ld% st% br_per_macro mispred_per_br serialize% int% fp% simd%"
    )
    for d in range(10):
        chunk = rows[int(len(rows) * d / 10):int(len(rows) * (d + 1) / 10)]
        if not chunk:
            continue
        stats = defaultdict(float)
        for obj in chunk:
            for co in obj.get("cores", []):
                c = int(co["core_id"])
                start = int(co["cursor_start"])
                end = int(co["cursor_end"])
                seq = merged[c]
                prev = seq[start - 1] if start > 0 else None
                for rec in seq[start:end]:
                    stats["uops"] += 1
                    if is_macro_head(rec, prev):
                        stats["macro"] += 1
                    if int(rec.get("is_load", 0) or 0):
                        stats["ld"] += 1
                    if int(rec.get("is_store", 0) or 0):
                        stats["st"] += 1
                    if int(rec.get("is_atomic", 0) or 0):
                        stats["atomic"] += 1
                    if int(rec.get("is_branch", 0) or 0):
                        stats["br"] += 1
                        if int(rec.get("_mispredicted", rec.get("mispredicted", 0)) or 0):
                            stats["misp"] += 1
                    if int(rec.get("is_serialize", 0) or 0):
                        stats["ser"] += 1
                    if int(rec.get("is_int", 0) or 0):
                        stats["int"] += 1
                    if int(rec.get("is_fp", 0) or 0):
                        stats["fp"] += 1
                    if int(rec.get("is_simd", 0) or 0):
                        stats["simd"] += 1
                    prev = rec
        pred = mean([float(r.get("pred_cpi", 0.0) or 0.0) for r in chunk])
        label = mean([float(r.get("label_cpi", 0.0) or 0.0) for r in chunk])
        mem = stats["ld"] + stats["st"] + stats["atomic"]
        print(
            d,
            f"{chunk[0]['progress_after'] * 100:.1f}-{chunk[-1]['progress_after'] * 100:.1f}",
            f"{pred:.3f}",
            f"{label:.3f}",
            f"{pred - label:.3f}",
            int(stats["macro"]),
            int(stats["uops"]),
            f"{stats['uops'] / max(stats['macro'], 1):.2f}",
            f"{mem / max(stats['uops'], 1) * 100:.1f}",
            f"{stats['ld'] / max(mem, 1) * 100:.1f}",
            f"{stats['st'] / max(mem, 1) * 100:.1f}",
            f"{stats['br'] / max(stats['macro'], 1):.2f}",
            f"{stats['misp'] / max(stats['br'], 1) * 100:.1f}",
            f"{stats['ser'] / max(stats['uops'], 1) * 100:.1f}",
            f"{stats['int'] / max(stats['uops'], 1) * 100:.1f}",
            f"{stats['fp'] / max(stats['uops'], 1) * 100:.1f}",
            f"{stats['simd'] / max(stats['uops'], 1) * 100:.1f}",
        )


if __name__ == "__main__":
    main()
