#!/usr/bin/env python3
"""Paired comparison for the v30 B3 inference-only branch-scale audit."""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
from collections import defaultdict
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple


TraceKey = Tuple[str, int, int, str]


def _load(path: str) -> Mapping[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping) or not isinstance(value.get("traces"), list):
        raise RuntimeError(f"not a merged v29 report: {path}")
    return value


def _key(row: Mapping[str, Any]) -> TraceKey:
    return (
        str(row["workload"]),
        int(row["n_cores"]),
        int(row["seed"]),
        str(row["source_split"]),
    )


def _complete_rows(report: Mapping[str, Any]) -> Dict[TraceKey, Mapping[str, Any]]:
    rows: Dict[TraceKey, Mapping[str, Any]] = {}
    for trace in report["traces"]:
        free = trace.get("free_running")
        if not isinstance(free, Mapping) or not bool(free.get("complete")):
            continue
        key = _key(trace)
        if key in rows:
            raise RuntimeError(f"duplicate trace identity: {key}")
        rows[key] = trace
    return rows


def _metrics(trace: Mapping[str, Any]) -> Tuple[float, float, float]:
    free = trace["free_running"]
    pred = float(free["pred_roi_cpi"])
    true = float(free["true_roi_cpi"])
    error = abs(pred - true) / true
    signed = (pred - true) / true
    if not all(math.isfinite(value) for value in (pred, true, error, signed)):
        raise RuntimeError(f"non-finite ROI metric for {_key(trace)}")
    return error, signed, pred


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return (sum(rows) / len(rows)) if rows else float("nan")


def _summarize_pairs(
    pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
) -> Dict[str, Any]:
    base_error = []
    variant_error = []
    base_signed = []
    variant_signed = []
    deltas = []
    pred_deltas = []
    step_relative_deltas = []
    uops_per_forward_relative_deltas = []
    for baseline, variant in pairs:
        be, bs, bp = _metrics(baseline)
        ve, vs, vp = _metrics(variant)
        base_error.append(be)
        variant_error.append(ve)
        base_signed.append(bs)
        variant_signed.append(vs)
        deltas.append(ve - be)
        pred_deltas.append((vp - bp) / float(variant["free_running"]["true_roi_cpi"]))
        base_free = baseline["free_running"]
        variant_free = variant["free_running"]
        base_steps = float(base_free["steps"])
        variant_steps = float(variant_free["steps"])
        base_upf = float(base_free["retired_uops_per_model_forward"])
        variant_upf = float(variant_free["retired_uops_per_model_forward"])
        step_relative_deltas.append((variant_steps - base_steps) / base_steps)
        uops_per_forward_relative_deltas.append((variant_upf - base_upf) / base_upf)
    delta_mean = _mean(deltas)
    delta_std = statistics.stdev(deltas) if len(deltas) > 1 else 0.0
    delta_sem = delta_std / math.sqrt(len(deltas)) if deltas else float("nan")
    return {
        "trace_count": len(pairs),
        "baseline_roi_error_pct": 100.0 * _mean(base_error),
        "variant_roi_error_pct": 100.0 * _mean(variant_error),
        "roi_error_delta_pp": 100.0 * delta_mean,
        "paired_delta_std_pp": 100.0 * delta_std,
        "paired_delta_sem_pp": 100.0 * delta_sem,
        "paired_delta_normal95_low_pp": 100.0 * (delta_mean - 1.96 * delta_sem),
        "paired_delta_normal95_high_pp": 100.0 * (delta_mean + 1.96 * delta_sem),
        "baseline_signed_bias_pct": 100.0 * _mean(base_signed),
        "variant_signed_bias_pct": 100.0 * _mean(variant_signed),
        "prediction_shift_vs_true_pp": 100.0 * _mean(pred_deltas),
        "scheduler_steps_relative_delta_pct": 100.0 * _mean(step_relative_deltas),
        "retired_uops_per_forward_relative_delta_pct": (
            100.0 * _mean(uops_per_forward_relative_deltas)
        ),
        "improved_traces": sum(delta < 0.0 for delta in deltas),
        "worsened_traces": sum(delta > 0.0 for delta in deltas),
        "unchanged_traces": sum(delta == 0.0 for delta in deltas),
        "median_paired_delta_pp": 100.0 * statistics.median(deltas),
        "max_improvement_pp": -100.0 * min(deltas),
        "max_regression_pp": 100.0 * max(deltas),
    }


def _group_summary(
    pairs: Sequence[Tuple[Mapping[str, Any], Mapping[str, Any]]],
    field: str,
) -> Dict[str, Any]:
    grouped = defaultdict(list)
    for baseline, variant in pairs:
        if field == "n_cores":
            value = str(int(variant[field]))
        else:
            value = str(variant.get(field, ""))
        grouped[value].append((baseline, variant))
    return {
        value: _summarize_pairs(rows)
        for value, rows in sorted(grouped.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument(
        "--variant", action="append", required=True,
        help="LABEL=REPORT_JSON; may be repeated",
    )
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    baseline = _complete_rows(_load(args.baseline))
    variants = []
    for item in args.variant:
        if "=" not in item:
            raise SystemExit(f"invalid --variant {item!r}; expected LABEL=PATH")
        label, path = item.split("=", 1)
        variants.append((label, os.path.abspath(path), _complete_rows(_load(path))))

    report: Dict[str, Any] = {
        "schema_version": "v30-b3-branch-scale-paired-audit-v1",
        "baseline": os.path.abspath(args.baseline),
        "interpretation_limit": (
            "Inference-time ablation of a jointly trained B3 checkpoint measures "
            "dependency/sensitivity, not the counterfactual accuracy of a separately "
            "trained B1 or B2 model."
        ),
        "variants": {},
    }
    for label, path, variant_rows in variants:
        missing = sorted(set(variant_rows) - set(baseline))
        if missing:
            raise RuntimeError(f"baseline lacks {len(missing)} rows for {label}: {missing[0]}")
        pairs = [(baseline[key], variant_rows[key]) for key in sorted(variant_rows)]
        if not pairs:
            raise RuntimeError(f"variant {label} has no complete traces")
        report["variants"][label] = {
            "report": path,
            "overall": _summarize_pairs(pairs),
            "by_source_split": _group_summary(pairs, "source_split"),
            "by_workload_role": _group_summary(pairs, "workload_role"),
            "by_workload": _group_summary(pairs, "workload"),
            "by_core_count": _group_summary(pairs, "n_cores"),
        }

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "comparison.json")
    md_path = os.path.join(out_dir, "comparison.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")

    lines = [
        "# v30 B3 branch residual no-training audit",
        "",
        report["interpretation_limit"],
        "",
        "| variant | traces | baseline error | variant error | delta | normal 95% | improved | pred shift |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, value in report["variants"].items():
        row = value["overall"]
        lines.append(
            f"| {label} | {row['trace_count']} | "
            f"{row['baseline_roi_error_pct']:.4f}% | "
            f"{row['variant_roi_error_pct']:.4f}% | "
            f"{row['roi_error_delta_pp']:+.4f} pp | "
            f"[{row['paired_delta_normal95_low_pp']:+.4f}, "
            f"{row['paired_delta_normal95_high_pp']:+.4f}] | "
            f"{row['improved_traces']}/{row['trace_count']} | "
            f"{row['prediction_shift_vs_true_pp']:+.4f} pp |"
        )
    for label, value in report["variants"].items():
        lines.extend(["", f"## {label} by workload", ""])
        lines.extend([
            "| workload | n | baseline | variant | delta | improved |",
            "|---|---:|---:|---:|---:|---:|",
        ])
        for workload, row in value["by_workload"].items():
            lines.append(
                f"| {workload} | {row['trace_count']} | "
                f"{row['baseline_roi_error_pct']:.4f}% | "
                f"{row['variant_roi_error_pct']:.4f}% | "
                f"{row['roi_error_delta_pp']:+.4f} pp | "
                f"{row['improved_traces']}/{row['trace_count']} |"
            )
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"comparison_json={json_path}")
    print(f"comparison_md={md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
