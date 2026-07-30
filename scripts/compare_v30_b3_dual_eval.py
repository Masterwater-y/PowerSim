#!/usr/bin/env python3
"""Compare two completed v29/v30 full free-running evaluation reports."""
from __future__ import annotations

import argparse
import json
import os
from statistics import fmean
from typing import Any, Dict, Iterable, Mapping


METRICS = (
    "micro_cpi_mape",
    "macro_cpi_mape",
    "makespan_mape",
    "scheduler_window_cpi_uop_weighted_mape",
    "steps_per_s",
    "uops_per_s",
)


def load(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    if int(report.get("trace_count", 0)) <= 0:
        raise RuntimeError(f"empty evaluation report: {path}")
    failures = report.get("run", {}).get("failures", [])
    if failures:
        raise RuntimeError(f"evaluation report contains failures: {path}")
    return report


def checkpoint_summary(report: Mapping[str, Any]) -> Dict[str, Any]:
    run = report["run"]
    return {
        "checkpoint": run["checkpoint"],
        "checkpoint_id": run["checkpoint_id"],
        "checkpoint_step": int(run["checkpoint_step"]),
        "trace_count": int(report["trace_count"]),
        "evaluation_contract": run["evaluation_contract"],
    }


def by_core(report: Mapping[str, Any]) -> Dict[int, Mapping[str, Any]]:
    return {
        int(row["n_cores"]): row for row in report.get("by_core_count", [])
    }


def overall_macro(report: Mapping[str, Any], name: str) -> float:
    field = {
        "micro_cpi_mape": "micro_cpi_abs_relative_error",
        "macro_cpi_mape": "macro_cpi_abs_relative_error",
        "makespan_mape": "makespan_abs_relative_error",
        "scheduler_window_cpi_uop_weighted_mape": (
            "scheduler_window_cpi_uop_weighted_mape"
        ),
        "steps_per_s": "steps_per_s",
        "uops_per_s": "uops_per_s",
    }[name]
    values = [
        float(trace["free_running"][field])
        for trace in report.get("traces", [])
        if trace.get("free_running", {}).get("complete")
        and trace["free_running"].get(field) is not None
    ]
    return fmean(values) if values else float("nan")


def comparison_row(
    scope: str,
    left: Mapping[str, Any],
    right: Mapping[str, Any],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {"scope": scope}
    for metric in METRICS:
        a = float(left[metric])
        b = float(right[metric])
        row[metric] = {
            "exact_60k": a,
            "best_through_90k": b,
            "delta": b - a,
            "relative_change": ((b / a) - 1.0) if a else None,
        }
    return row


def text_table(rows: Iterable[Mapping[str, Any]], meta: Mapping[str, Any]) -> str:
    lines = [
        "TCSim v30 B3 full free-running checkpoint comparison",
        "",
        f"exact_60k checkpoint step: {meta['exact_60k']['checkpoint_step']}",
        (
            "best-through-90k checkpoint step: "
            f"{meta['best_through_90k']['checkpoint_step']}"
        ),
        f"traces per checkpoint: {meta['exact_60k']['trace_count']}",
        "",
        (
            "scope        cpi60(%)  cpi90best(%)  delta(pp)  "
            "makespan60(%)  makespan90best(%)  steps/s60  steps/s90best"
        ),
    ]
    for row in rows:
        cpi = row["micro_cpi_mape"]
        makespan = row["makespan_mape"]
        speed = row["steps_per_s"]
        lines.append(
            f"{row['scope']:<12} "
            f"{100*cpi['exact_60k']:>8.3f} "
            f"{100*cpi['best_through_90k']:>13.3f} "
            f"{100*cpi['delta']:>10.3f} "
            f"{100*makespan['exact_60k']:>13.3f} "
            f"{100*makespan['best_through_90k']:>17.3f} "
            f"{speed['exact_60k']:>10.2f} "
            f"{speed['best_through_90k']:>13.2f}"
        )
    lines.extend([
        "",
        "Negative delta(pp) means best-through-90k has lower CPI error.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exact-60k", required=True)
    parser.add_argument("--best-through-90k", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    left = load(args.exact_60k)
    right = load(args.best_through_90k)
    left_meta = checkpoint_summary(left)
    right_meta = checkpoint_summary(right)
    if left_meta["trace_count"] != right_meta["trace_count"]:
        raise RuntimeError("checkpoint reports have different trace counts")
    if left_meta["evaluation_contract"] != right_meta["evaluation_contract"]:
        raise RuntimeError("checkpoint reports use different evaluation contracts")

    left_cores = by_core(left)
    right_cores = by_core(right)
    if set(left_cores) != set(right_cores):
        raise RuntimeError("checkpoint reports have different core-count coverage")

    overall_left = {
        metric: overall_macro(left, metric) for metric in METRICS
    }
    overall_right = {
        metric: overall_macro(right, metric) for metric in METRICS
    }
    rows = [comparison_row("all-macro", overall_left, overall_right)]
    rows.extend(
        comparison_row(f"c{core:02d}", left_cores[core], right_cores[core])
        for core in sorted(left_cores)
    )
    result = {
        "schema_version": "tcsim-v30-b3-dual-eval-comparison-1",
        "exact_60k": left_meta,
        "best_through_90k": right_meta,
        "rows": rows,
    }
    os.makedirs(args.out, exist_ok=True)
    json_path = os.path.join(args.out, "comparison.json")
    text_path = os.path.join(args.out, "comparison.txt")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(text_table(rows, result))
    print(f"[v30-b3-compare] json={json_path} text={text_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
