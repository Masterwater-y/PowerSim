#!/usr/bin/env python3
"""Summarize CPI tail error and signed bias from eval log directories.

This is a read-only diagnostic tool. It intentionally bins by label CPI
quantiles instead of workload names so it can be used as a generalization
guardrail rather than a workload-specific training signal.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import re
import statistics
from typing import Iterable


def _quantile(vals: list[float], q: float) -> float:
    if not vals:
        return float("nan")
    xs = sorted(vals)
    if len(xs) == 1:
        return xs[0]
    pos = min(max(q, 0.0), 1.0) * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _mean(vals: Iterable[float]) -> float:
    vals = list(vals)
    return statistics.mean(vals) if vals else float("nan")


def _median(vals: Iterable[float]) -> float:
    vals = list(vals)
    return statistics.median(vals) if vals else float("nan")


def _fmt_pct(v: float) -> str:
    if math.isnan(v):
        return "-"
    return f"{v * 100:.2f}%"


def _fmt_num(v: float) -> str:
    if math.isnan(v):
        return "-"
    return f"{v:.4g}"


def _load_json_tail(path: pathlib.Path) -> list[dict]:
    text = path.read_text(errors="replace")
    starts = list(re.finditer(r"^\[\s*$", text, re.MULTILINE))
    if not starts:
        return []
    try:
        obj = json.loads(text[starts[-1].start():])
    except json.JSONDecodeError:
        return []
    return obj if isinstance(obj, list) else []


def load_rows(logdir: pathlib.Path) -> list[dict]:
    rows = []
    for path in sorted(logdir.glob("*.log")):
        for rec in _load_json_tail(path):
            pred = rec.get("pred_cpi_uop")
            label = rec.get("label_cpi_uop")
            roi = rec.get("roi_stats_cpi_uop")
            if not isinstance(pred, (int, float)):
                continue
            target = label if isinstance(label, (int, float)) else roi
            if not isinstance(target, (int, float)) or target <= 0:
                continue
            signed = pred / target - 1.0
            rows.append({
                "workload": rec.get("workload", path.stem),
                "windows": int(rec.get("windows", 0) or 0),
                "pred": float(pred),
                "label": float(target),
                "abs_err": abs(signed),
                "signed": signed,
                "win_mape": rec.get("win_mape_cpi_uop"),
            })
    return rows


def print_dashboard(logdir: pathlib.Path, rows: list[dict]) -> None:
    print(f"\n## {logdir}")
    if not rows:
        print("No completed eval JSON records found.")
        return

    abs_err = [r["abs_err"] for r in rows]
    signed = [r["signed"] for r in rows]
    labels = [r["label"] for r in rows]
    print(
        "overall: "
        f"n={len(rows)} "
        f"mean_abs={_fmt_pct(_mean(abs_err))} "
        f"median_abs={_fmt_pct(_median(abs_err))} "
        f"max_abs={_fmt_pct(max(abs_err))} "
        f"mean_signed={_fmt_pct(_mean(signed))} "
        f"median_signed={_fmt_pct(_median(signed))}"
    )

    cuts = [
        ("p00-p50", float("-inf"), _quantile(labels, 0.50)),
        ("p50-p80", _quantile(labels, 0.50), _quantile(labels, 0.80)),
        ("p80-p95", _quantile(labels, 0.80), _quantile(labels, 0.95)),
        ("p95-p100", _quantile(labels, 0.95), float("inf")),
    ]
    print("\nlabel-CPI bins:")
    print("| bin | n | label range | mean abs | mean signed | median signed |")
    print("|---|---:|---:|---:|---:|---:|")
    for name, lo, hi in cuts:
        if math.isinf(lo):
            subset = [r for r in rows if r["label"] <= hi]
        elif math.isinf(hi):
            subset = [r for r in rows if r["label"] > lo]
        else:
            subset = [r for r in rows if lo < r["label"] <= hi]
        s_abs = [r["abs_err"] for r in subset]
        s_signed = [r["signed"] for r in subset]
        label_range = f"{_fmt_num(lo)}..{_fmt_num(hi)}"
        print(
            f"| {name} | {len(subset)} | {label_range} | "
            f"{_fmt_pct(_mean(s_abs))} | {_fmt_pct(_mean(s_signed))} | "
            f"{_fmt_pct(_median(s_signed))} |"
        )

    print("\nmost under-predicted:")
    for r in sorted(rows, key=lambda x: x["signed"])[:8]:
        print(
            f"- {r['workload']}: signed={_fmt_pct(r['signed'])} "
            f"abs={_fmt_pct(r['abs_err'])} pred={r['pred']:.4g} "
            f"label={r['label']:.4g} windows={r['windows']}"
        )

    print("\nmost over-predicted:")
    for r in sorted(rows, key=lambda x: x["signed"], reverse=True)[:8]:
        print(
            f"- {r['workload']}: signed={_fmt_pct(r['signed'])} "
            f"abs={_fmt_pct(r['abs_err'])} pred={r['pred']:.4g} "
            f"label={r['label']:.4g} windows={r['windows']}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("logdirs", nargs="+", help="eval_parallel_* log dirs")
    args = ap.parse_args()
    for raw in args.logdirs:
        logdir = pathlib.Path(raw)
        print_dashboard(logdir, load_rows(logdir))


if __name__ == "__main__":
    main()
