#!/usr/bin/env python3
"""Audit retired DynInst branch labels against gem5 BPred retirement stats.

The final gem5 stats dump can extend past an individual core's TaoTrace target.
This tool therefore treats a core as scope-aligned only when the BPred committed
branch population and the per-core TaoTrace PMU branch population differ by no
more than a caller-selected ratio.  It is a diagnostic for existing datasets,
not a replacement oracle for a fresh trace collected with the persistent
``taotrace-retired-bpred-v1`` source.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path


SCHEMA = "fastsim-branch-miss-oracle-audit-v1"
STAT_PREFIX = r"board\.processor\.switch(\d+)\.core\.branchPred\."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("validation_summary", type=Path)
    parser.add_argument(
        "--max-branch-skew-ratio",
        type=float,
        default=0.001,
        help=(
            "Maximum |final BPred committed - trace branches| / trace "
            "branches for a scope-aligned core (default: 0.001)."
        ),
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()
    if not 0.0 <= args.max_branch_skew_ratio <= 1.0:
        parser.error("--max-branch-skew-ratio must be in [0, 1]")
    return args


def indexed_stat(text: str, suffix: str) -> dict[int, int]:
    pattern = re.compile(STAT_PREFIX + re.escape(suffix) + r"\s+([0-9.eE+-]+)")
    return {
        int(match.group(1)): int(float(match.group(2)))
        for match in pattern.finditer(text)
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def error_metrics(
    rows: list[dict], reference_field: str, predicted_field: str
) -> dict:
    apes = []
    absolute_error = 0
    signed_error = 0
    reference_total = 0
    predicted_total = 0
    for row in rows:
        reference = int(row[reference_field])
        predicted = int(row[predicted_field])
        difference = predicted - reference
        absolute_error += abs(difference)
        signed_error += difference
        reference_total += reference
        predicted_total += predicted
        if reference == 0:
            if predicted == 0:
                apes.append(0.0)
        else:
            apes.append(100.0 * abs(difference) / reference)
    denominator = reference_total
    return {
        "rows": len(rows),
        "finite_ape_rows": len(apes),
        "mape_percent": sum(apes) / len(apes) if apes else None,
        "p50_ape_percent": percentile(apes, 0.50),
        "p90_ape_percent": percentile(apes, 0.90),
        "p99_ape_percent": percentile(apes, 0.99),
        "wape_percent": (
            100.0 * absolute_error / denominator if denominator else None
        ),
        "signed_bias_percent": (
            100.0 * signed_error / denominator if denominator else None
        ),
        "absolute_error_total": absolute_error,
        "predicted_total": predicted_total,
        "reference_total": reference_total,
    }


def fastsim_path(summary_path: Path, case: dict) -> Path:
    candidate = summary_path.parent / "cases" / case["case_id"] / "fastsim.json"
    if candidate.is_file():
        return candidate
    validation = candidate.with_name("validation.json")
    if validation.is_file():
        configured = json.loads(validation.read_text()).get("fastsim_output")
        if configured:
            return Path(configured)
    raise ValueError(f"{case['case_id']}: FastSim output is unavailable")


def collect_rows(summary_path: Path) -> list[dict]:
    summary = json.loads(summary_path.read_text())
    rows = []
    for case in summary.get("cases", []):
        case_id = str(case["case_id"])
        result_dir = Path(case["result_dir"])
        stats_path = result_dir / "stats.txt"
        if not stats_path.is_file():
            raise ValueError(f"{case_id}: missing {stats_path}")
        stats_text = stats_path.read_text(encoding="utf-8", errors="replace")
        committed = indexed_stat(stats_text, "committed_0::total")
        mispredicted = indexed_stat(stats_text, "mispredicted_0::total")
        fastsim = json.loads(fastsim_path(summary_path, case).read_text())
        fastsim_cores = fastsim.get("cores", [])
        oracle_paths = sorted(
            (result_dir / "oracle").glob("kernel-events-core*.json")
        )
        if not oracle_paths:
            raise ValueError(f"{case_id}: no per-core kernel-events oracle")
        for oracle_path in oracle_paths:
            oracle = json.loads(oracle_path.read_text())
            core = int(oracle["core_id"])
            if core not in committed or core not in mispredicted:
                raise ValueError(f"{case_id}/core{core}: missing BPred stats")
            if core >= len(fastsim_cores):
                raise ValueError(f"{case_id}/core{core}: missing FastSim core")
            pmu = oracle["pmu_user_plus_kernel"]
            trace_branches = int(pmu["branches"])
            bpred_branches = int(committed[core])
            skew = abs(bpred_branches - trace_branches)
            rows.append(
                {
                    "case_id": case_id,
                    "core": core,
                    "trace_branches": trace_branches,
                    "fastsim_branches": int(fastsim_cores[core]["branches"]),
                    "gem5_bpred_committed": bpred_branches,
                    "branch_population_skew": skew,
                    "branch_population_skew_ratio": (
                        skew / trace_branches if trace_branches else 0.0
                    ),
                    "legacy_dyninst_misses": int(pmu["branch_misses"]),
                    "gem5_bpred_misses": int(mispredicted[core]),
                    "fastsim_misses": int(
                        fastsim_cores[core]["branch_misses"]
                    ),
                    "legacy_label_gap": (
                        int(mispredicted[core]) - int(pmu["branch_misses"])
                    ),
                }
            )
    return rows


def build_report(rows: list[dict], max_skew_ratio: float) -> dict:
    aligned = [
        row
        for row in rows
        if row["branch_population_skew_ratio"] <= max_skew_ratio
    ]
    return {
        "schema": SCHEMA,
        "scope": "per-core rows whose final BPred population matches trace scope",
        "diagnostic_only": True,
        "max_branch_skew_ratio": max_skew_ratio,
        "all_rows": len(rows),
        "scope_aligned_rows": len(aligned),
        "legacy_dyninst_reference": error_metrics(
            aligned, "legacy_dyninst_misses", "fastsim_misses"
        ),
        "gem5_bpred_reference": error_metrics(
            aligned, "gem5_bpred_misses", "fastsim_misses"
        ),
        "legacy_label_gap": {
            "gem5_bpred_total": sum(
                row["gem5_bpred_misses"] for row in aligned
            ),
            "legacy_dyninst_total": sum(
                row["legacy_dyninst_misses"] for row in aligned
            ),
            "missing_from_legacy_total": sum(
                row["legacy_label_gap"] for row in aligned
            ),
        },
        "rows": aligned,
    }


def format_value(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.6f}%"


def render_markdown(report: dict) -> str:
    legacy = report["legacy_dyninst_reference"]
    bpred = report["gem5_bpred_reference"]
    gap = report["legacy_label_gap"]
    lines = [
        "# Branch-miss oracle scope audit",
        "",
        f"- all per-core rows: {report['all_rows']}",
        f"- scope-aligned rows: {report['scope_aligned_rows']}",
        f"- maximum branch-population skew: {report['max_branch_skew_ratio']:.6g}",
        "- status: diagnostic only; recollection with taotrace-retired-bpred-v1 is required",
        "",
        "| reference | MAPE | P99 | WAPE | predicted | reference |",
        "|---|---:|---:|---:|---:|---:|",
        (
            "| legacy retirement-time DynInst comparison | "
            f"{format_value(legacy['mape_percent'])} | "
            f"{format_value(legacy['p99_ape_percent'])} | "
            f"{format_value(legacy['wape_percent'])} | "
            f"{legacy['predicted_total']:,} | {legacy['reference_total']:,} |"
        ),
        (
            "| gem5 BPred committed miss stat | "
            f"{format_value(bpred['mape_percent'])} | "
            f"{format_value(bpred['p99_ape_percent'])} | "
            f"{format_value(bpred['wape_percent'])} | "
            f"{bpred['predicted_total']:,} | {bpred['reference_total']:,} |"
        ),
        "",
        (
            "The legacy commit-time comparison omitted "
            f"{gap['missing_from_legacy_total']:,} of "
            f"{gap['gem5_bpred_total']:,} committed BPred misses across the "
            "scope-aligned rows."
        ),
        "",
        "| case/core | branches | skew | legacy | BPred | FastSim |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["rows"]:
        lines.append(
            f"| {row['case_id']}/c{row['core']} | "
            f"{row['trace_branches']:,} | "
            f"{row['branch_population_skew']:,} | "
            f"{row['legacy_dyninst_misses']:,} | "
            f"{row['gem5_bpred_misses']:,} | "
            f"{row['fastsim_misses']:,} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    rows = collect_rows(args.validation_summary.resolve())
    report = build_report(rows, args.max_branch_skew_ratio)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    else:
        print(encoded, end="")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
