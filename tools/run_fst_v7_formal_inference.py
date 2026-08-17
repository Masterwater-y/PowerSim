#!/usr/bin/env python3
"""Run dual-scope FastSim inference on an FST v7 formal FS dataset.

This runner intentionally scores CPI only.  PMU accuracy remains blocked when
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


def run_case(
    case: dict[str, Any], output: Path, fastsim: Path,
    user_config: Path, kernel_config: Path, force: bool,
    dtlb_miss_model: str, dtlb_page_walk_latency: int,
    fetch_buffer_refill_latency: int,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    result_dir = Path(case["result_dir"])
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
    oracle = json.loads(
        (result_dir / "oracle" / "kernel_events.json").read_text(encoding="utf-8")
    )["aggregate"]
    row: dict[str, Any] = {
        "case_id": case_id, "cores": int(case["cores"]),
        "workload": str(case["workload"]),
        "measurement_records": int(case["measurement_records"]),
    }
    for scope, oracle_key in (
        ("user", "cpi_user"),
        ("user-plus-kernel", "cpi_user_plus_kernel"),
    ):
        predicted = float(reports[scope]["scope_metrics"]["cpi"])
        reference = float(oracle[oracle_key])
        row[scope] = {
            "predicted_cpi": predicted, "reference_cpi": reference,
            "signed_error": predicted / reference - 1.0,
            "absolute_error": abs(predicted / reference - 1.0),
            "uops_per_second": float(reports[scope]["throughput"]["uops_per_second"]),
            "report": str(case_output / f"{scope}.json"),
        }
    return row


def summarize(rows: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    return {
        "cpi_ape": distribution([float(row[scope]["absolute_error"]) for row in rows]),
        "throughput_uops_per_second": distribution(
            [float(row[scope]["uops_per_second"]) for row in rows]
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
        "schema": "fastsim-fst-v7-formal-cpi-validation-v1",
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
