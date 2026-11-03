#!/usr/bin/env python3
"""Run dual-scope FastSim inference on an FST v7 formal FS dataset.

This runner scores cycles/user-UOP and true macro-instruction perf-like CPI.
PMU accuracy remains blocked when
the source dataset index says its TaoTrace uarch profile failed the identity
gate; predicted PMU counters are retained in each FastSim JSON for later use
with a regenerated oracle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from validate_kernel_events_oracle import validate_document


PMU_CONTRACT_ID = "perf-gem5-fastsim-x86-fs-v1"


def parse_args() -> argparse.Namespace:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fastsim", type=Path, default=Path("build/fastsim"))
    parser.add_argument(
        "--user-config",
        type=Path,
        default=project / "configs/gem5-v28_1-fs-user.cfg",
        help="Frozen FS user-only profile (default: maintained profile).",
    )
    parser.add_argument(
        "--kernel-config",
        type=Path,
        default=project / "configs/gem5-v28_1-fs-user-plus-kernel.cfg",
        help="Frozen FS user+kernel profile (default: maintained profile).",
    )
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--dtlb-miss-model",
        choices=("se_atomic", "timing_walk"),
        default="timing_walk",
        help="Use delayed translation timing for FS; se_atomic is an SE control.",
    )
    parser.add_argument(
        "--dtlb-page-walk-latency",
        type=int,
        default=12,
        help="C4-calibrated fixed walker service, frozen for C8 validation.",
    )
    parser.add_argument(
        "--fetch-buffer-refill-latency",
        type=int,
        default=1,
        help="Target L0-I resident-hit empty-cycle delay.",
    )
    return parser.parse_args()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def distribution(values: list[float]) -> dict[str, float | int]:
    return {
        "count": len(values), "mean": sum(values) / len(values),
        "min": min(values),
        "p50": percentile(values, 50), "p90": percentile(values, 90),
        "p99": percentile(values, 99), "max": max(values),
    }


def scoped_throughput(report: dict[str, Any]) -> dict[str, float]:
    scope_metrics = report.get("scope_metrics")
    if not isinstance(scope_metrics, dict):
        raise RuntimeError("FastSim report lacks canonical scope_metrics")
    throughput = scope_metrics.get("throughput")
    if not isinstance(throughput, dict):
        raise RuntimeError(
            "FastSim report lacks canonical scope_metrics.throughput"
        )
    top_level = report.get("throughput")
    if not isinstance(top_level, dict):
        raise RuntimeError("FastSim report lacks top-level throughput")
    return {
        "measurement_uops_per_second": float(
            throughput["user_uops_per_second"]
        ),
        "end_to_end_uops_per_second": float(
            throughput["end_to_end_user_uops_per_second"]
        ),
        # Retain the old mixed denominator only as an explicitly named
        # diagnostic. It divides measured user UOPs by warmup+ROI wall time
        # and is not an accepted throughput gate.
        "legacy_mixed_uops_per_second": float(
            top_level["uops_per_second"]
        ),
    }


def run_case(
    case: dict[str, Any], output: Path, fastsim: Path,
    user_config: Path, kernel_config: Path, force: bool,
    dtlb_miss_model: str, dtlb_page_walk_latency: int,
    fetch_buffer_refill_latency: int,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    result_dir = Path(case["result_dir"])
    oracle_document = json.loads(
        (result_dir / "oracle" / "kernel_events.json").read_text(
            encoding="utf-8"
        )
    )
    oracle_validation = validate_document(oracle_document, 0.0)
    if not oracle_validation.get("formal_pmu_eligible", False):
        raise RuntimeError(f"{case_id}: oracle lacks the P0 v3 contract")
    case_output = output / "cases" / case_id
    case_output.mkdir(parents=True, exist_ok=True)
    manifest = result_dir / "tao_trace" / "manifest.txt"
    dtlb_args = ["--dtlb-miss-model", dtlb_miss_model]
    if dtlb_miss_model == "timing_walk":
        dtlb_args.extend(
            ["--dtlb-page-walk-latency", str(dtlb_page_walk_latency)]
        )
    common = [
        "--manifest", str(manifest), "--cores", str(case["cores"]),
        "--fetch-buffer-refill-latency",
        str(fetch_buffer_refill_latency),
        "--allow-cross-page-without-virtual-token", "true",
        *dtlb_args, "--allow-mmio-escape", "true",
        "--dram-size", str(3 * 1024**3),
    ]
    reports = {}
    for scope, config in (
        ("user", user_config), ("user-plus-kernel", kernel_config)
    ):
        report = case_output / f"{scope}.json"
        if force or not report.is_file():
            command = [
                str(fastsim), "simulate", "--measurement-scope", scope,
                "--config", str(config), *common, "--output", str(report),
            ]
            started = time.monotonic()
            result = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"{case_id}/{scope}: exit={result.returncode}: {result.stdout}"
                )
            elapsed = time.monotonic() - started
            print(f"[done] {case_id}/{scope} {elapsed:.1f}s", flush=True)
        stats = json.loads(report.read_text(encoding="utf-8"))
        if stats.get("measurement_scope") != scope:
            raise RuntimeError(f"{report}: measurement scope mismatch")
        expected = int(case["measurement_records"])
        if int(stats["scope_metrics"]["user_trace_uops"]) != expected:
            raise RuntimeError(f"{report}: user denominator mismatch")
        reports[scope] = stats
    oracle = oracle_document["aggregate"]
    row: dict[str, Any] = {
        "case_id": case_id, "cores": int(case["cores"]),
        "workload": str(case["workload"]),
        "measurement_records": int(case["measurement_records"]),
    }
    for scope, uop_key, perf_key in (
        (
            "user",
            "cycles_per_user_uop_user",
            "perf_like_cpi_user",
        ),
        (
            "user-plus-kernel",
            "cycles_per_user_uop_user_plus_kernel",
            "perf_like_cpi_user_plus_kernel",
        ),
    ):
        metrics = reports[scope]["scope_metrics"]
        predicted = float(metrics["cycles_per_user_uop"])
        reference = float(oracle[uop_key])
        predicted_perf = float(metrics["perf_like_cpi"])
        reference_perf = float(oracle[perf_key])
        row[scope] = {
            "predicted_cycles_per_user_uop": predicted,
            "reference_cycles_per_user_uop": reference,
            "cycles_per_user_uop_signed_error": predicted / reference - 1.0,
            "cycles_per_user_uop_absolute_error": abs(
                predicted / reference - 1.0
            ),
            "predicted_perf_like_cpi": predicted_perf,
            "reference_perf_like_cpi": reference_perf,
            "perf_like_cpi_status": metrics["perf_like_cpi_status"],
            "perf_like_cpi_signed_error": (
                predicted_perf / reference_perf - 1.0
            ),
            "perf_like_cpi_absolute_error": abs(
                predicted_perf / reference_perf - 1.0
            ),
            **scoped_throughput(reports[scope]),
            "report": str(case_output / f"{scope}.json"),
        }
    return row


def summarize(rows: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    return {
        "cycles_per_user_uop_ape": distribution(
            [
                float(row[scope]["cycles_per_user_uop_absolute_error"])
                for row in rows
            ]
        ),
        "perf_like_cpi_ape": distribution(
            [
                float(row[scope]["perf_like_cpi_absolute_error"])
                for row in rows
            ]
        ),
        "throughput_measurement_uops_per_second": distribution(
            [
                float(row[scope]["measurement_uops_per_second"])
                for row in rows
            ]
        ),
        "throughput_end_to_end_uops_per_second": distribution(
            [
                float(row[scope]["end_to_end_uops_per_second"])
                for row in rows
            ]
        ),
        "throughput_legacy_mixed_uops_per_second": distribution(
            [
                float(row[scope]["legacy_mixed_uops_per_second"])
                for row in rows
            ]
        ),
    }


def main() -> int:
    args = parse_args()
    if args.dtlb_page_walk_latency <= 0:
        raise SystemExit("--dtlb-page-walk-latency must be positive")
    dataset = args.dataset.resolve()
    user_config = args.user_config.resolve()
    kernel_config = args.kernel_config.resolve()
    for label, path in (("user", user_config), ("user+kernel", kernel_config)):
        if not path.is_file():
            raise SystemExit(f"missing {label} config: {path}")
    index = json.loads((dataset / "index.json").read_text(encoding="utf-8"))
    if index.get("oracle_validity", {}).get("pmu_contract_id") != PMU_CONTRACT_ID:
        raise SystemExit("dataset index lacks the P0 PMU contract identity")
    cases = sorted(index["cases"], key=lambda row: (int(row["cores"]), row["workload"]))
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = {
            executor.submit(
                run_case, case, output, args.fastsim.resolve(),
                user_config, kernel_config, args.force,
                args.dtlb_miss_model, args.dtlb_page_walk_latency,
                args.fetch_buffer_refill_latency,
            ): case
            for case in cases
        }
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception as error:  # noqa: BLE001 - report every failed case
                failures.append(str(error))
                print(f"[fail] {error}", flush=True)
    if failures:
        raise SystemExit("\n".join(failures[:20]))
    rows.sort(key=lambda row: (row["cores"], row["workload"]))
    calibration = [row for row in rows if int(row["cores"]) == 4]
    held_out = [row for row in rows if int(row["cores"]) > 4]
    summary = {
        "schema": "fastsim-fst-v7-formal-cpi-validation-v3",
        "dataset": str(dataset), "cases": rows,
        "dtlb_miss_model": args.dtlb_miss_model,
        "dtlb_page_walk_latency": (
            args.dtlb_page_walk_latency
            if args.dtlb_miss_model == "timing_walk" else None
        ),
        "fetch_buffer_refill_latency": args.fetch_buffer_refill_latency,
        "profiles": {
            "user": {
                "path": str(user_config),
                "sha256": sha256(user_config),
            },
            "user-plus-kernel": {
                "path": str(kernel_config),
                "sha256": sha256(kernel_config),
            },
        },
        "splits": {
            "calibration": {
                scope: summarize(calibration, scope)
                for scope in ("user", "user-plus-kernel")
            },
            "held-out": {
                scope: summarize(held_out, scope)
                for scope in ("user", "user-plus-kernel")
            },
            "all": {
                scope: summarize(rows, scope)
                for scope in ("user", "user-plus-kernel")
            },
        },
        "pmu_accuracy": index["oracle_validity"]["pmu"],
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary["splits"], sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
