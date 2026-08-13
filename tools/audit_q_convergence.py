#!/usr/bin/env python3
"""Run and summarize FastSim time-epoch Q convergence on existing traces.

This tool never launches gem5.  It reuses the aligned functional traces and
the already captured gem5 statistics through validate_tcsim_c4_c8.py, then
compares adjacent FastSim Q values.  Q is selected from convergence and
throughput; it is deliberately not selected by whichever value happens to
match gem5 best.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Iterable


DEFAULT_WORKLOADS = (
    "W_v28_gofeed_base",
    "W_v28_redis_heldout",
    "W_v28_mysql_base",
    "W_v28_memory_seq_moderate",
    "W_v28_cache_L2_mixed",
    "W_v28_pytorch_base",
    "W_v28_int_alu_dense",
)

# FastSim count fields in validate_tcsim_c4_c8.py's per-case summary.
COUNT_METRICS = {
    "branch_miss": "branch_miss_fastsim",
    "l1d_miss": "l1d_miss_fastsim",
    "private_l2_miss": "private_l2_miss_fastsim",
    "llc_miss": "llc_tag_vs_ruby_demand_miss_fastsim",
    "cha_lookup": "cha_llc_lookup_fastsim",
    "dtlb_access": "dtlb_access_fastsim",
    "dtlb_miss": "dtlb_miss_fastsim",
}


def type7_quantile(values: Iterable[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def relative_change(coarse: float, fine: float) -> float | None:
    if fine == 0:
        return None
    return (coarse - fine) / fine


def resolve(project: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (project / path).resolve()


def run_validation(args: argparse.Namespace, project: Path, q: int) -> None:
    output = args.out_dir / f"q{q:04d}"
    command = [
        args.driver,
        str(args.validator),
        "--fastsim",
        str(args.fastsim),
        "--config",
        str(args.config),
        "--raw-data-root",
        str(args.raw_data_root),
        "--seed",
        str(args.seed),
        "--out-dir",
        str(output),
        "--scratch-dir",
        str(args.scratch_dir),
        "--data-python",
        args.data_python,
        "--interval-max-cycles",
        str(q),
    ]
    for cores in args.cores:
        command.extend(["--cores", str(cores)])
    for workload in args.workloads:
        command.extend(["--workload", workload])
    if args.reuse_existing:
        command.append("--reuse-existing")
    print(f"[Q={q}] {' '.join(command)}", flush=True)
    subprocess.run(command, cwd=project, check=True)


def load_cases(output: Path, q: int) -> dict[tuple[int, str], dict]:
    path = output / f"q{q:04d}" / "summary.json"
    if not path.is_file():
        raise SystemExit(f"missing Q={q} summary: {path}")
    document = json.loads(path.read_text())
    return {
        (int(case["cores"]), str(case["workload"])): case
        for case in document["cases"]
    }


def build_rows(
    output: Path, q_values: list[int]
) -> tuple[list[dict], dict[int, dict[tuple[int, str], dict]]]:
    cases_by_q = {q: load_cases(output, q) for q in q_values}
    rows: list[dict] = []
    for coarse_q, fine_q in zip(q_values, q_values[1:]):
        coarse_cases = cases_by_q[coarse_q]
        fine_cases = cases_by_q[fine_q]
        keys = sorted(set(coarse_cases) & set(fine_cases))
        if set(coarse_cases) != set(fine_cases):
            raise SystemExit(
                f"Q={coarse_q} and Q={fine_q} contain different cases"
            )
        for cores, workload in keys:
            coarse = coarse_cases[(cores, workload)]
            fine = fine_cases[(cores, workload)]
            coarse_cpi = float(coarse["uop_cpi_fastsim"])
            fine_cpi = float(fine["uop_cpi_fastsim"])
            row = {
                "coarse_q": coarse_q,
                "fine_q": fine_q,
                "cores": cores,
                "workload": workload,
                "coarse_cpi": coarse_cpi,
                "fine_cpi": fine_cpi,
                "gem5_cpi": float(coarse["uop_cpi_gem5"]),
                "cpi_signed_change": relative_change(coarse_cpi, fine_cpi),
                "cpi_absolute_change": abs(
                    relative_change(coarse_cpi, fine_cpi) or 0.0
                ),
                "coarse_gem5_signed_error": float(
                    coarse["uop_cpi_signed_error"]
                ),
                "fine_gem5_signed_error": float(
                    fine["uop_cpi_signed_error"]
                ),
                "coarse_uops_per_second": float(
                    coarse["fastsim_uops_per_second"]
                ),
                "fine_uops_per_second": float(
                    fine["fastsim_uops_per_second"]
                ),
            }
            for name, field in COUNT_METRICS.items():
                coarse_count = int(coarse[field])
                fine_count = int(fine[field])
                row[f"{name}_coarse"] = coarse_count
                row[f"{name}_fine"] = fine_count
                row[f"{name}_signed_change"] = relative_change(
                    coarse_count, fine_count
                )
            rows.append(row)
    return rows, cases_by_q


def aggregate_rows(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[int, int, int], list[dict]] = {}
    for row in rows:
        key = (row["coarse_q"], row["fine_q"], row["cores"])
        groups.setdefault(key, []).append(row)
    aggregates = []
    for (coarse_q, fine_q, cores), group in sorted(groups.items()):
        cpi_changes = [row["cpi_absolute_change"] for row in group]
        aggregate = {
            "coarse_q": coarse_q,
            "fine_q": fine_q,
            "cores": cores,
            "workloads": len(group),
            "cpi_mean_absolute_change": sum(cpi_changes) / len(group),
            "cpi_median_absolute_change": type7_quantile(
                cpi_changes, 0.5
            ),
            "cpi_p90_absolute_change": type7_quantile(cpi_changes, 0.9),
            "cpi_p99_absolute_change": type7_quantile(cpi_changes, 0.99),
            "cpi_maximum_absolute_change": max(cpi_changes),
            "coarse_min_uops_per_second": min(
                row["coarse_uops_per_second"] for row in group
            ),
            "fine_min_uops_per_second": min(
                row["fine_uops_per_second"] for row in group
            ),
            "cpi_two_percent_gate": type7_quantile(cpi_changes, 0.99)
            <= 0.02,
        }
        for name in COUNT_METRICS:
            coarse_total = sum(row[f"{name}_coarse"] for row in group)
            fine_total = sum(row[f"{name}_fine"] for row in group)
            absolute_delta = sum(
                abs(row[f"{name}_coarse"] - row[f"{name}_fine"])
                for row in group
            )
            aggregate[f"{name}_count_weighted_absolute_change"] = (
                absolute_delta / fine_total if fine_total else None
            )
        aggregates.append(aggregate)
    return aggregates


def write_outputs(
    output: Path,
    q_values: list[int],
    rows: list[dict],
    aggregates: list[dict],
) -> None:
    document = {
        "schema": "fastsim-q-convergence-v1",
        "q_parameter": "sim.interval_max_cycles",
        "q_values": q_values,
        "selection_policy": {
            "description": (
                "Choose Q from adjacent-Q convergence and throughput, not "
                "from the smallest gem5 error."
            ),
            "cpi_p99_absolute_change_limit": 0.02,
            "minimum_uops_per_second": 5_000_000,
        },
        "aggregates": aggregates,
        "cases": rows,
    }
    (output / "q-convergence.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    )
    if rows:
        with (output / "q-convergence.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    lines = [
        "# FastSim Q convergence audit",
        "",
        "Q is `sim.interval_max_cycles`. It is selected from FastSim-to-"
        "FastSim convergence and throughput, never by choosing the Q that "
        "happens to match gem5 best.",
        "",
        "| Coarse Q | Fine Q | Cores | Cases | CPI P99 change | CPI max "
        "change | Coarse min UOP/s | Fine min UOP/s | 2% gate |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for item in aggregates:
        lines.append(
            "| {coarse_q} | {fine_q} | {cores} | {workloads} | "
            "{p99:.3%} | {maximum:.3%} | {coarse_speed:.3f}M | "
            "{fine_speed:.3f}M | {gate} |".format(
                coarse_q=item["coarse_q"],
                fine_q=item["fine_q"],
                cores=item["cores"],
                workloads=item["workloads"],
                p99=item["cpi_p99_absolute_change"],
                maximum=item["cpi_maximum_absolute_change"],
                coarse_speed=item["coarse_min_uops_per_second"] / 1e6,
                fine_speed=item["fine_min_uops_per_second"] / 1e6,
                gate="PASS" if item["cpi_two_percent_gate"] else "FAIL",
            )
        )
    lines.extend(
        [
            "",
            "Per-case signed CPI changes, gem5 errors, throughput, and PMU "
            "count changes are in `q-convergence.csv` and "
            "`q-convergence.json`.",
            "",
        ]
    )
    (output / "q-convergence.md").write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastsim", default="build/fastsim")
    parser.add_argument(
        "--config", default="configs/gem5/v28_1-time-epoch.cfg"
    )
    parser.add_argument(
        "--validator", default="tools/validate_tcsim_c4_c8.py"
    )
    parser.add_argument(
        "--raw-data-root", default="/data00/yinhaolang/TSim/data"
    )
    parser.add_argument("--data-python", default=sys.executable)
    parser.add_argument("--driver", default=sys.executable)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cores", action="append", type=int)
    parser.add_argument("--workload", action="append")
    parser.add_argument("--q", action="append", type=int)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--scratch-dir")
    parser.add_argument("--reuse-existing", action="store_true")
    parser.add_argument(
        "--summarize-only",
        action="store_true",
        help="do not launch simulations; summarize existing qXXXX outputs",
    )
    args = parser.parse_args()

    project = Path(__file__).resolve().parent.parent
    args.fastsim = resolve(project, args.fastsim)
    args.config = resolve(project, args.config)
    args.validator = resolve(project, args.validator)
    args.raw_data_root = Path(args.raw_data_root).resolve()
    args.out_dir = Path(args.out_dir).resolve()
    args.scratch_dir = (
        Path(args.scratch_dir).resolve()
        if args.scratch_dir
        else project / "tmp" / "q-convergence-scratch"
    )
    args.cores = sorted(set(args.cores or [4, 8, 16, 32]))
    args.workloads = args.workload or list(DEFAULT_WORKLOADS)
    args.q_values = sorted(set(args.q or [1024, 512, 256]), reverse=True)
    if len(args.q_values) < 2 or any(q <= 0 for q in args.q_values):
        raise SystemExit("provide at least two positive --q values")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.scratch_dir.mkdir(parents=True, exist_ok=True)

    if not args.summarize_only:
        for q in args.q_values:
            run_validation(args, project, q)
    rows, _ = build_rows(args.out_dir, args.q_values)
    aggregates = aggregate_rows(rows)
    write_outputs(args.out_dir, args.q_values, rows, aggregates)
    print(f"summary={args.out_dir / 'q-convergence.json'}", flush=True)


if __name__ == "__main__":
    main()
