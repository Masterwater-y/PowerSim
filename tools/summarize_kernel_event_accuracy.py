#!/usr/bin/env python3
"""Summarize user-only and user+kernel FastSim accuracy reports separately."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Optional


SCHEMA = "fastsim-kernel-event-accuracy-v3"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--split", choices=("calibration", "held-out"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument(
        "--allow-diagnostic",
        action="store_true",
        help="Allow reports produced from legacy/non-formal oracles.",
    )
    return parser.parse_args()


def finite_ape(row: dict) -> Optional[float]:
    value = row.get("absolute_percentage_error")
    return float(value) if value is not None and math.isfinite(value) else None


def percentile(values: list[float], fraction: float) -> Optional[float]:
    """Return a linearly interpolated sample percentile (R/NumPy type 7)."""
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


def aggregate_errors(
    rows: list[dict], weights: Optional[list[int]] = None
) -> dict:
    apes = []
    for row in rows:
        value = finite_ape(row)
        if value is not None:
            apes.append(value)
    if weights is None:
        predicted = [float(row["predicted"]) for row in rows]
        reference = [float(row["reference"]) for row in rows]
    else:
        predicted = [float(row["predicted"]) * weight for row, weight in zip(rows, weights)]
        reference = [float(row["reference"]) * weight for row, weight in zip(rows, weights)]
    reference_total = sum(reference)
    absolute_error_total = sum(
        abs(left - right) for left, right in zip(predicted, reference)
    )
    signed_error_total = sum(predicted) - reference_total
    return {
        "cases": len(rows),
        "ape_cases": len(apes),
        "mape_percent": statistics.mean(apes) if apes else None,
        "p50_ape_percent": percentile(apes, 0.50),
        "p90_ape_percent": percentile(apes, 0.90),
        "p99_ape_percent": percentile(apes, 0.99),
        # Compatibility alias for consumers of summary-v1.
        "median_ape_percent": percentile(apes, 0.50),
        "max_ape_percent": max(apes) if apes else None,
        "wape_percent": (
            absolute_error_total / reference_total * 100.0
            if reference_total
            else None
        ),
        "signed_bias_percent": (
            signed_error_total / reference_total * 100.0
            if reference_total
            else None
        ),
        "predicted_total": sum(predicted),
        "reference_total": reference_total,
    }


def aggregate_values(values: list[float]) -> dict:
    return {
        "samples": len(values),
        "mean_uops_per_second": statistics.mean(values) if values else None,
        "p50_uops_per_second": percentile(values, 0.50),
        "p90_uops_per_second": percentile(values, 0.90),
        "p99_uops_per_second": percentile(values, 0.99),
        # Compatibility alias for consumers of summary-v1.
        "median_uops_per_second": percentile(values, 0.50),
        "minimum_uops_per_second": min(values) if values else None,
        "maximum_uops_per_second": max(values) if values else None,
    }


def workload_name(report: Path, document: dict) -> str:
    parent = report.parent.name
    if parent:
        return parent
    return Path(document["oracle"]).parent.parent.name


def uops_per_second(scope: dict) -> Optional[float]:
    for field in (
        "user_uops_per_second",
        "uops_per_second",
        "measurement_uops_per_second",
        "end_to_end_user_uops_per_second",
        "end_to_end_uops_per_second",
    ):
        value = scope.get(field)
        if value is not None:
            return float(value)
    return None


def percent(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def main() -> int:
    args = parse_args()
    documents = []
    for report in args.reports:
        document = json.loads(report.read_text())
        if document.get("schema") != SCHEMA:
            raise SystemExit(f"{report}: expected schema {SCHEMA}")
        if not document.get("formal_oracle_eligible", False) and not (
            args.allow_diagnostic
        ):
            raise SystemExit(
                f"{report}: oracle is diagnostic; pass --allow-diagnostic "
                "only for a clearly labeled non-formal summary"
            )
        documents.append((report, document))

    weights = [int(document["n_user"]) for _, document in documents]
    event_status = documents[0][1].get("pmu_event_status", {})
    if any(
        document.get("pmu_event_status", {}) != event_status
        for _, document in documents[1:]
    ):
        raise SystemExit("PMU event dictionaries differ across reports")
    cycles_per_user_uop = {
        scope: aggregate_errors(
            [
                document["cycles_per_user_uop"][scope]
                for _, document in documents
            ],
            weights,
        )
        for scope in ("user", "user_plus_kernel")
    }
    perf_like_cpi = {
        scope: aggregate_errors(
            [document["perf_like_cpi"][scope] for _, document in documents],
            [
                int(document["retired_instruction_denominators"][scope])
                for _, document in documents
            ],
        )
        for scope in ("user", "user_plus_kernel")
    }
    pmu_fields = sorted(
        set.intersection(
            *(
                set(document["pmu"]["user"])
                & set(document["pmu"]["user_plus_kernel"])
                for _, document in documents
            )
        )
    )
    pmu = {
        scope: {
            field: aggregate_errors(
                [document["pmu"][scope][field] for _, document in documents]
            )
            for field in pmu_fields
        }
        for scope in ("user", "user_plus_kernel")
    }
    component_names = sorted(documents[0][1]["kernel_cycle_components"])
    components = {
        name: aggregate_errors(
            [
                document["kernel_cycle_components"][name]
                for _, document in documents
            ]
        )
        for name in component_names
    }
    event_names = sorted(documents[0][1]["kernel_event_counts"])
    events = {
        name: aggregate_errors(
            [document["kernel_event_counts"][name] for _, document in documents]
        )
        for name in event_names
    }
    rows = []
    for report, document in documents:
        row = {
            "workload": workload_name(report, document),
            "n_user": int(document["n_user"]),
            "cycles_per_user_uop_user_predicted": document[
                "cycles_per_user_uop"
            ]["user"]["predicted"],
            "cycles_per_user_uop_user_reference": document[
                "cycles_per_user_uop"
            ]["user"]["reference"],
            "cycles_per_user_uop_user_ape_percent": finite_ape(
                document["cycles_per_user_uop"]["user"]
            ),
            "cycles_per_user_uop_user_plus_kernel_predicted": document[
                "cycles_per_user_uop"
            ]["user_plus_kernel"]["predicted"],
            "cycles_per_user_uop_user_plus_kernel_reference": document[
                "cycles_per_user_uop"
            ]["user_plus_kernel"]["reference"],
            "cycles_per_user_uop_user_plus_kernel_ape_percent": finite_ape(
                document["cycles_per_user_uop"]["user_plus_kernel"]
            ),
            "perf_like_cpi_user_ape_percent": finite_ape(
                document["perf_like_cpi"]["user"]
            ),
            "perf_like_cpi_user_plus_kernel_ape_percent": finite_ape(
                document["perf_like_cpi"]["user_plus_kernel"]
            ),
            "user_uops_per_second": uops_per_second(
                document["throughput"]["user"]
            ),
            "user_plus_kernel_uops_per_second": uops_per_second(
                document["throughput"]["user_plus_kernel"]
            ),
            "report": str(report.resolve()),
        }
        rows.append(row)
    user_throughputs = [
        float(row["user_uops_per_second"])
        for row in rows
        if row["user_uops_per_second"] is not None
    ]
    combined_throughputs = [
        float(row["user_plus_kernel_uops_per_second"])
        for row in rows
        if row["user_plus_kernel_uops_per_second"] is not None
    ]
    payload = {
        "schema": "fastsim-kernel-event-accuracy-summary-v1",
        "split": args.split,
        "formal_oracle_eligible": all(
            document.get("formal_oracle_eligible", False)
            for _, document in documents
        ),
        "formal_accounting_eligible": all(
            document.get("formal_oracle_eligible", False)
            for _, document in documents
        ),
        "cases": len(documents),
        "cycles_per_user_uop": cycles_per_user_uop,
        "perf_like_cpi": perf_like_cpi,
        "pmu": pmu,
        "pmu_event_status": event_status,
        "pmu_event_groups": {
            mapping: sorted(
                field
                for field, status in event_status.items()
                if status.get("mapping") == mapping
            )
            for mapping in ("strict", "proxy", "diagnostic", "unavailable")
        },
        "kernel_cycle_components": components,
        "kernel_event_counts": events,
        "throughput": {
            "user": aggregate_values(user_throughputs),
            "user_plus_kernel": aggregate_values(combined_throughputs),
        },
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    if args.csv_output:
        args.csv_output.parent.mkdir(parents=True, exist_ok=True)
        with args.csv_output.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    if args.markdown_output:
        lines = [
            f"# Kernel-event accuracy ({args.split})",
            "",
            "| Workload | Cycles/user-UOP user APE | Cycles/user-UOP user+kernel APE | Perf-like CPI user APE | Perf-like CPI user+kernel APE | User M uops/s | User+kernel M uops/s |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in rows:
            lines.append(
                "| {workload} | {ua:.2f}% | {ka:.2f}% | {up:.2f}% | {kp:.2f}% | {ut:.2f} | {kt:.2f} |".format(
                    workload=row["workload"],
                    ua=row["cycles_per_user_uop_user_ape_percent"],
                    ka=row["cycles_per_user_uop_user_plus_kernel_ape_percent"],
                    up=row["perf_like_cpi_user_ape_percent"],
                    kp=row["perf_like_cpi_user_plus_kernel_ape_percent"],
                    ut=row["user_uops_per_second"] / 1e6,
                    kt=row["user_plus_kernel_uops_per_second"] / 1e6,
                )
            )
        lines.extend(
            [
                "",
                "## Aggregate cycles per user UOP accuracy",
                "",
                "| Scope | Mean APE (MAPE) | P50 APE | P90 APE | P99 APE | WAPE | Bias | Maximum APE |",
                "|---|---:|---:|---:|---:|---:|---:|---:|",
                "| User | {mape} | {p50} | {p90} | {p99} | {wape} | {bias} | {maximum} |".format(
                    mape=percent(cycles_per_user_uop["user"]["mape_percent"]),
                    p50=percent(cycles_per_user_uop["user"]["p50_ape_percent"]),
                    p90=percent(cycles_per_user_uop["user"]["p90_ape_percent"]),
                    p99=percent(cycles_per_user_uop["user"]["p99_ape_percent"]),
                    wape=percent(cycles_per_user_uop["user"]["wape_percent"]),
                    bias=percent(cycles_per_user_uop["user"]["signed_bias_percent"]),
                    maximum=percent(cycles_per_user_uop["user"]["max_ape_percent"]),
                ),
                "| User+kernel | {mape} | {p50} | {p90} | {p99} | {wape} | {bias} | {maximum} |".format(
                    mape=percent(cycles_per_user_uop["user_plus_kernel"]["mape_percent"]),
                    p50=percent(cycles_per_user_uop["user_plus_kernel"]["p50_ape_percent"]),
                    p90=percent(cycles_per_user_uop["user_plus_kernel"]["p90_ape_percent"]),
                    p99=percent(cycles_per_user_uop["user_plus_kernel"]["p99_ape_percent"]),
                    wape=percent(cycles_per_user_uop["user_plus_kernel"]["wape_percent"]),
                    bias=percent(cycles_per_user_uop["user_plus_kernel"]["signed_bias_percent"]),
                    maximum=percent(cycles_per_user_uop["user_plus_kernel"]["max_ape_percent"]),
                ),
                "",
                "## Aggregate PMU accuracy",
                "",
                "APE percentiles use workload-equal samples and R/NumPy Type-7 "
                "linear interpolation. A row with reference=0 and predicted>0 "
                "has no finite relative error, so it is excluded from the APE "
                "distribution but remains in WAPE; `APE cases` makes that "
                "coverage explicit.",
            ]
        )
        for scope, title in (
            ("user", "User PMU"),
            ("user_plus_kernel", "User+kernel PMU"),
        ):
            lines.extend(
                [
                    "",
                    f"### {title}",
                    "",
                    "| Counter | Contract status | APE cases | Mean APE (MAPE) | P50 APE | P90 APE | P99 APE | WAPE | Bias |",
                    "|---|---|---:|---:|---:|---:|---:|---:|---:|",
                ]
            )
            for field in pmu_fields:
                metrics = pmu[scope][field]
                lines.append(
                    "| {field} | {mapping} | {ape_cases}/{cases} | {mape} | {p50} | {p90} | {p99} | {wape} | {bias} |".format(
                        field=field,
                        mapping=event_status.get(field, {}).get(
                            "mapping", "diagnostic"
                        ),
                        ape_cases=metrics["ape_cases"],
                        cases=metrics["cases"],
                        mape=percent(metrics["mape_percent"]),
                        p50=percent(metrics["p50_ape_percent"]),
                        p90=percent(metrics["p90_ape_percent"]),
                        p99=percent(metrics["p99_ape_percent"]),
                        wape=percent(metrics["wape_percent"]),
                        bias=percent(metrics["signed_bias_percent"]),
                    )
                )
        lines.extend(
            [
                "",
                "## Kernel event model accuracy",
                "",
                "| Quantity | Mean APE (MAPE) | P50 APE | P90 APE | P99 APE | WAPE | Bias | Predicted total | Reference total |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for name, metrics in events.items():
            lines.append(
                "| {name} events | {mape} | {p50} | {p90} | {p99} | {wape} | {bias} | {predicted:.0f} | {reference:.0f} |".format(
                    name=name,
                    mape=percent(metrics["mape_percent"]),
                    p50=percent(metrics["p50_ape_percent"]),
                    p90=percent(metrics["p90_ape_percent"]),
                    p99=percent(metrics["p99_ape_percent"]),
                    wape=percent(metrics["wape_percent"]),
                    bias=percent(metrics["signed_bias_percent"]),
                    predicted=metrics["predicted_total"],
                    reference=metrics["reference_total"],
                )
            )
        for name, metrics in components.items():
            lines.append(
                "| {name} | {mape} | {p50} | {p90} | {p99} | {wape} | {bias} | {predicted:.0f} | {reference:.0f} |".format(
                    name=name,
                    mape=percent(metrics["mape_percent"]),
                    p50=percent(metrics["p50_ape_percent"]),
                    p90=percent(metrics["p90_ape_percent"]),
                    p99=percent(metrics["p99_ape_percent"]),
                    wape=percent(metrics["wape_percent"]),
                    bias=percent(metrics["signed_bias_percent"]),
                    predicted=metrics["predicted_total"],
                    reference=metrics["reference_total"],
                )
            )
        lines.extend(
            [
                "",
                "Idle and blocked wall time are diagnostic coverage only and "
                "are excluded from user+active-kernel CPI. Scheduler and "
                "unknown-kernel remain explicit rather than being absorbed "
                "into another event class.",
                "",
                "## Throughput",
                "",
                "| Scope | Mean M uops/s | P50 | P90 | P99 | Minimum |",
                "|---|---:|---:|---:|---:|---:|",
                "| User | {mean:.2f} | {p50:.2f} | {p90:.2f} | {p99:.2f} | {minimum:.2f} |".format(
                    mean=payload["throughput"]["user"]["mean_uops_per_second"] / 1e6,
                    p50=payload["throughput"]["user"]["p50_uops_per_second"] / 1e6,
                    p90=payload["throughput"]["user"]["p90_uops_per_second"] / 1e6,
                    p99=payload["throughput"]["user"]["p99_uops_per_second"] / 1e6,
                    minimum=payload["throughput"]["user"]["minimum_uops_per_second"] / 1e6,
                ),
                "| User+kernel | {mean:.2f} | {p50:.2f} | {p90:.2f} | {p99:.2f} | {minimum:.2f} |".format(
                    mean=payload["throughput"]["user_plus_kernel"]["mean_uops_per_second"] / 1e6,
                    p50=payload["throughput"]["user_plus_kernel"]["p50_uops_per_second"] / 1e6,
                    p90=payload["throughput"]["user_plus_kernel"]["p90_uops_per_second"] / 1e6,
                    p99=payload["throughput"]["user_plus_kernel"]["p99_uops_per_second"] / 1e6,
                    minimum=payload["throughput"]["user_plus_kernel"]["minimum_uops_per_second"] / 1e6,
                ),
                "",
            ]
        )
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text("\n".join(lines))
    print(json.dumps({"cases": len(documents), "output": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
