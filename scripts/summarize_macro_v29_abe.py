#!/usr/bin/env python3
"""Summarize paired single-core-count A/B/E deployment results."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--a-summary", required=True)
    parser.add_argument("--b-summary", required=True)
    parser.add_argument("--e-summary", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--bootstrap-seed", type=int, default=1234)
    return parser.parse_args()


def finite_mean(values: list[float]) -> float | None:
    selected = [float(value) for value in values if math.isfinite(float(value))]
    return sum(selected) / len(selected) if selected else None


def percentile(values: list[float], quantile: float) -> float | None:
    selected = sorted(
        float(value) for value in values if math.isfinite(float(value))
    )
    if not selected:
        return None
    if len(selected) == 1:
        return selected[0]
    position = (len(selected) - 1) * float(quantile)
    lower = int(position)
    upper = min(lower + 1, len(selected) - 1)
    weight = position - lower
    return selected[lower] * (1.0 - weight) + selected[upper] * weight


def signed_relative_error(predicted: float, truth: float) -> float:
    return (float(predicted) - float(truth)) / max(abs(float(truth)), 1.0e-12)


def trace_metrics(record: dict[str, Any]) -> dict[str, Any]:
    """Read one persisted rollout without pretending v29 has fixed chunks."""

    report_path = Path(str(record["report"]))
    report = json.loads(report_path.read_text())
    deployment = dict(report.get("deployment_metrics", {}))
    rollout = dict(report.get("rollout", report.get("free_running", {})))
    predicted_cpi = float(deployment["predicted_macro_cpi"])
    true_cpi = float(deployment["true_macro_cpi"])
    predicted_makespan = float(deployment["predicted_makespan"])
    true_makespan = float(deployment["true_makespan"])

    core_abs: list[float] = []
    core_signed: list[float] = []
    for row in deployment.get("per_core", []):
        predicted = float(row["predicted_cycles"])
        truth = float(row["true_cycles"])
        core_abs.append(abs(signed_relative_error(predicted, truth)))
        core_signed.append(signed_relative_error(predicted, truth))

    timing = rollout.get("predictor_timing", {})
    timing_mean = timing.get("mean_ms", {}) if isinstance(timing, dict) else {}
    return {
        "workload": str(record["workload"]),
        "workload_role": str(record.get("workload_role", "")),
        "roi_macro_cpi_abs_error": abs(
            signed_relative_error(predicted_cpi, true_cpi)
        ),
        "roi_macro_cpi_signed_error": signed_relative_error(
            predicted_cpi, true_cpi
        ),
        "makespan_abs_error": abs(
            signed_relative_error(predicted_makespan, true_makespan)
        ),
        "makespan_signed_error": signed_relative_error(
            predicted_makespan, true_makespan
        ),
        "core_roi_cpi_mape_mean": finite_mean(core_abs),
        "core_roi_cpi_mape_p90": percentile(core_abs, 0.90),
        "core_roi_cpi_mape_p99": percentile(core_abs, 0.99),
        "core_roi_cpi_signed_bias": finite_mean(core_signed),
        "branch_relative_error": deployment.get(
            "branch_miss_count_abs_relative_error"
        ),
        "branch_abs_error_pp": deployment.get(
            "branch_miss_rate_abs_error_pp"
        ),
        "macro_per_s": float(rollout["aggregate_macro_per_s"]),
        "uop_per_s": float(rollout["aggregate_uop_per_s"]),
        "macro_per_forward": float(
            rollout["retired_macros_per_model_forward"]
        ),
        "steps_per_s": float(rollout["steps_per_s"]),
        "mean_step_ms": float(rollout["mean_step_ms"]),
        "predict_ms": float(timing_mean.get("predict_total", 0.0)),
        "context_ms": float(timing_mean.get("context", 0.0)),
        "collate_ms": float(timing_mean.get("collate", 0.0)),
        "model_ms": float(timing_mean.get("online_model", 0.0)),
        "output_ms": float(timing_mean.get("output_copy", 0.0)),
        "gpu_peak_allocated_bytes": int(
            rollout.get("gpu_peak_allocated_bytes", 0)
        ),
        "complete": bool(rollout.get("complete")),
        "exactly_once": rollout.get("exactly_once") is not None,
        "capped_steps": int(rollout.get("capped_steps", 0)),
        "zero_core_rows": int(rollout.get("zero_core_rows", 0)),
        "steps": int(rollout["steps"]),
        "report": str(report_path),
    }


def bootstrap_ci(values: list[float], seed: int) -> list[float] | None:
    if not values:
        return None
    generator = random.Random(int(seed))
    count = len(values)
    estimates = sorted(
        sum(values[generator.randrange(count)] for _ in range(count)) / count
        for _ in range(20_000)
    )
    return [estimates[499], estimates[19_499]]


def heldout(record: dict[str, Any]) -> bool:
    return "heldout" in str(record.get("workload_role", "")).lower()


def aggregate(summary: dict[str, Any]) -> dict[str, Any]:
    records = [
        dict(row) for row in summary["records"]
        if row.get("status") == "PASS"
    ]

    def scope(rows: list[dict[str, Any]]) -> dict[str, Any]:
        traces = [trace_metrics(row) for row in rows]

        def values(key: str) -> list[float]:
            return [
                float(row[key]) for row in traces
                if row.get(key) is not None
            ]

        roi_abs = values("roi_macro_cpi_abs_error")
        roi_signed = values("roi_macro_cpi_signed_error")
        makespan_abs = values("makespan_abs_error")
        makespan_signed = values("makespan_signed_error")
        core_mean = values("core_roi_cpi_mape_mean")
        core_p90 = values("core_roi_cpi_mape_p90")
        core_p99 = values("core_roi_cpi_mape_p99")
        core_signed = values("core_roi_cpi_signed_bias")
        branch_relative = values("branch_relative_error")
        branch_abs_pp = values("branch_abs_error_pp")
        macro_per_s = values("macro_per_s")
        uop_per_s = values("uop_per_s")
        macro_per_forward = values("macro_per_forward")
        steps_per_s = values("steps_per_s")
        step_ms = values("mean_step_ms")
        gpu_bytes = values("gpu_peak_allocated_bytes")
        return {
            "count": len(traces),
            "roi_macro_cpi": {
                "mape_mean": finite_mean(roi_abs),
                "mape_p50": percentile(roi_abs, 0.50),
                "mape_p90": percentile(roi_abs, 0.90),
                "signed_bias": finite_mean(roi_signed),
            },
            "core_roi_cpi": {
                "mape_mean": finite_mean(core_mean),
                "mape_p90_mean": finite_mean(core_p90),
                "mape_p99_mean": finite_mean(core_p99),
                "signed_bias": finite_mean(core_signed),
            },
            "makespan": {
                "mape_mean": finite_mean(makespan_abs),
                "mape_p50": percentile(makespan_abs, 0.50),
                "mape_p90": percentile(makespan_abs, 0.90),
                "signed_bias": finite_mean(makespan_signed),
            },
            "branch": {
                "relative_error_mean": finite_mean(branch_relative),
                "relative_error_p50": percentile(branch_relative, 0.50),
                "relative_error_p90": percentile(branch_relative, 0.90),
                "absolute_error_pp_mean": finite_mean(branch_abs_pp),
            },
            "throughput": {
                "macro_per_s_mean": finite_mean(macro_per_s),
                "uop_per_s_mean": finite_mean(uop_per_s),
                "macro_per_forward_mean": finite_mean(macro_per_forward),
                "steps_per_s_mean": finite_mean(steps_per_s),
                "mean_step_ms": finite_mean(step_ms),
                "predict_ms": finite_mean(values("predict_ms")),
                "context_ms": finite_mean(values("context_ms")),
                "collate_ms": finite_mean(values("collate_ms")),
                "model_ms": finite_mean(values("model_ms")),
                "output_ms": finite_mean(values("output_ms")),
                "gpu_peak_allocated_bytes_max": max(gpu_bytes) if gpu_bytes else None,
            },
            "execution": {
                "complete_traces": sum(bool(row["complete"]) for row in traces),
                "exactly_once_traces": sum(
                    bool(row["exactly_once"]) for row in traces
                ),
                "capped_steps": sum(int(row["capped_steps"]) for row in traces),
                "zero_core_rows": sum(int(row["zero_core_rows"]) for row in traces),
            },
            "drift": {
                "status": "not_collected",
                "reason": (
                    "the completed rollout predates v29-compatible post-transition "
                    "cursor-interval drift collection"
                ),
            },
            # Compatibility aliases used by earlier A/B/E consumers.
            "macro_cpi_mape": finite_mean(roi_abs),
            "makespan_mape": finite_mean(makespan_abs),
        }

    base = [row for row in records if not heldout(row)]
    unseen = [row for row in records if heldout(row)]
    unseen_no_redis = [
        row for row in unseen if "redis" not in str(row["workload"]).lower()
    ]
    return {
        "all": scope(records),
        "base": scope(base),
        "heldout": scope(unseen),
        "heldout_no_redis": scope(unseen_no_redis),
        "mean_trace_macro_per_s": summary["throughput"][
            "mean_trace_macro_per_s"
        ],
        "weighted_macro_per_s": summary["throughput"]["weighted_macro_per_s"],
    }


def paired(
    first: dict[str, Any],
    second: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    first_rows = {
        str(row["workload"]): row for row in first["records"]
        if row.get("status") == "PASS"
    }
    second_rows = {
        str(row["workload"]): row for row in second["records"]
        if row.get("status") == "PASS"
    }
    names = sorted(set(first_rows) & set(second_rows))
    rows = []
    for name in names:
        lhs, rhs = first_rows[name], second_rows[name]
        rows.append({
            "workload": name,
            "workload_role": str(lhs.get("workload_role", "")),
            "macro_cpi_delta_second_minus_first": (
                float(rhs["macro_cpi_absolute_error"])
                - float(lhs["macro_cpi_absolute_error"])
            ),
            "makespan_delta_second_minus_first": (
                float(rhs["makespan_absolute_error"])
                - float(lhs["makespan_absolute_error"])
            ),
        })

    def scope(selected: list[dict[str, Any]], offset: int) -> dict[str, Any]:
        cpi = [row["macro_cpi_delta_second_minus_first"] for row in selected]
        makespan = [
            row["makespan_delta_second_minus_first"] for row in selected
        ]
        return {
            "count": len(selected),
            "mean_macro_cpi_delta": finite_mean(cpi),
            "median_macro_cpi_delta": statistics.median(cpi) if cpi else None,
            "macro_cpi_first_better": sum(value > 0.0 for value in cpi),
            "macro_cpi_bootstrap_95ci": bootstrap_ci(cpi, seed + offset),
            "mean_makespan_delta": finite_mean(makespan),
            "makespan_bootstrap_95ci": bootstrap_ci(
                makespan, seed + offset + 1,
            ),
        }

    unseen = [row for row in rows if heldout(row)]
    unseen_no_redis = [
        row for row in unseen if "redis" not in row["workload"].lower()
    ]
    return {
        "all": scope(rows, 0),
        "heldout": scope(unseen, 10),
        "heldout_no_redis": scope(unseen_no_redis, 20),
        "per_workload": rows,
    }


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * float(value):.3f}%"


def main() -> int:
    args = parse_args()
    paths = {
        "A": Path(args.a_summary).resolve(),
        "B": Path(args.b_summary).resolve(),
        "E": Path(args.e_summary).resolve(),
    }
    summaries = {key: json.loads(path.read_text()) for key, path in paths.items()}
    for key, summary in summaries.items():
        if summary.get("status") != "PASS" or int(summary.get("trace_count", 0)) != 23:
            raise RuntimeError(f"{key} is not a complete 23-trace PASS summary")
        if int(summary.get("stride_macro", 0)) != 256:
            raise RuntimeError(f"{key} does not use stride_macro=256")
    workload_sets = {
        key: {str(row["workload"]) for row in value["records"]}
        for key, value in summaries.items()
    }
    if len({frozenset(value) for value in workload_sets.values()}) != 1:
        raise RuntimeError("A/B/E summaries do not contain identical workloads")
    core_counts = {int(value.get("cores", -1)) for value in summaries.values()}
    if len(core_counts) != 1 or next(iter(core_counts)) <= 0:
        raise RuntimeError("A/B/E summaries do not use one identical core count")
    core_count = next(iter(core_counts))

    report = {
        "schema_version": "llmsim-macro-v29-abe-summary-2",
        "status": "PASS",
        "core_count": core_count,
        "evaluation_contract": {
            "reference": "TCSim v29 single-global-time free-running",
            "advance_policy": "variable per-core macro prefix under one shared delta",
            "target_stride_macro": 256,
            "target_stride_semantics": (
                "candidate commit-time index, not a fixed per-core advance count"
            ),
            "max_step_cycles": 1024.0,
            "headline_aggregation": "workload/trace equal mean",
            "fixed_chunk_mape": "not_applicable",
            "legacy_scheduler_window_mape": "not_relabeled",
            "oracle_timing_in_model_context": False,
            "drift_diagnostics": "not_collected_in_existing_rollouts",
        },
        "interpretation": {
            "A_vs_B": "pretrained Qwen+LoRA value with real semantic input held",
            "B_vs_E": "real cached semantic input value in ordinary Transformer",
            "A_vs_E": "complete LLM semantic pipeline value",
            "positive_delta": "the first named variant has lower error",
        },
        "source_summaries": {key: str(path) for key, path in paths.items()},
        "variants": {key: aggregate(value) for key, value in summaries.items()},
        "paired": {
            "A_vs_B": paired(summaries["A"], summaries["B"], seed=args.bootstrap_seed),
            "B_vs_E": paired(summaries["B"], summaries["E"], seed=args.bootstrap_seed + 100),
            "A_vs_E": paired(summaries["A"], summaries["E"], seed=args.bootstrap_seed + 200),
        },
    }
    output = Path(args.output_root).resolve()
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    lines = [
        f"# Macro-v29 A/B/E c{core_count} deployment comparison", "",
        f"All variants use c{core_count}, seed=1234, S=1, 30K steps and the same 23 deployment traces.",
        "",
        "The rollout follows the current TCSim-v29 contract: stride=256 selects a candidate commit-time position; one shared delta advances a variable macro prefix on each core.",
        "", "## TCSim-v29-aligned accuracy", "",
        "| variant | set | n | ROI mean | ROI p50 | ROI p90 | ROI bias | core MAPE | makespan | branch rel. | branch abs |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in ("A", "B", "E"):
        row = report["variants"][key]
        for scope_name in ("all", "base", "heldout", "heldout_no_redis"):
            scope = row[scope_name]
            roi = scope["roi_macro_cpi"]
            core = scope["core_roi_cpi"]
            makespan = scope["makespan"]
            branch = scope["branch"]
            lines.append(
                f"| {key} | {scope_name} | {scope['count']} | "
                f"{percent(roi['mape_mean'])} | {percent(roi['mape_p50'])} | "
                f"{percent(roi['mape_p90'])} | {percent(roi['signed_bias'])} | "
                f"{percent(core['mape_mean'])} | "
                f"{percent(makespan['mape_mean'])} | "
                f"{percent(branch['relative_error_mean'])} | "
                f"{float(branch['absolute_error_pp_mean'] or 0.0):.3f} pp |"
            )
    lines.extend([
        "", "## Variable-prefix efficiency", "",
        "| variant | macro/s | UOP/s | macro/forward | steps/s | step ms | context ms | model ms | peak GiB | complete/exact |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for key in ("A", "B", "E"):
        scope = report["variants"][key]["all"]
        throughput = scope["throughput"]
        execution = scope["execution"]
        peak = throughput["gpu_peak_allocated_bytes_max"]
        lines.append(
            f"| {key} | {float(throughput['macro_per_s_mean'] or 0.0):.1f} | "
            f"{float(throughput['uop_per_s_mean'] or 0.0):.1f} | "
            f"{float(throughput['macro_per_forward_mean'] or 0.0):.1f} | "
            f"{float(throughput['steps_per_s_mean'] or 0.0):.3f} | "
            f"{float(throughput['mean_step_ms'] or 0.0):.2f} | "
            f"{float(throughput['context_ms'] or 0.0):.2f} | "
            f"{float(throughput['model_ms'] or 0.0):.2f} | "
            f"{float(peak or 0.0) / (1024 ** 3):.2f} | "
            f"{execution['complete_traces']}/{execution['exactly_once_traces']} |"
        )
    lines.extend([
        "", "## Paired error delta", "",
        "Positive means the first variant is better (second minus first).", "",
        "| comparison | ROI CPI delta | first better | heldout CPI delta | makespan delta |",
        "|---|---:|---:|---:|---:|",
    ])
    for key in ("A_vs_B", "B_vs_E", "A_vs_E"):
        row = report["paired"][key]
        lines.append(
            f"| {key} | {percent(row['all']['mean_macro_cpi_delta'])} | "
            f"{row['all']['macro_cpi_first_better']}/{row['all']['count']} | "
            f"{percent(row['heldout']['mean_macro_cpi_delta'])} | "
            f"{percent(row['all']['mean_makespan_delta'])} |"
        )
    lines.extend([
        "", "## Metric availability", "",
        "- Fixed chunk/window CPI MAPE is intentionally not reported: it belongs to the older fixed-chunk contract.",
        "- Cursor-interval drift was not collected by these completed rollouts and is explicitly marked unavailable in JSON.",
        "- All accuracy aggregates above are workload/trace-equal, not globally pooled by macro count.",
    ])
    (output / "summary.md").write_text("\n".join(lines) + "\n")
    print(f"[ABE summary] {output / 'summary.json'}")
    print(f"[ABE summary] {output / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
