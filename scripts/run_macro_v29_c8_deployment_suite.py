#!/usr/bin/env python3
"""Run the 23-trace c8 deployment suite with one worker per GPU."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--dataset-manifest", required=True)
    parser.add_argument("--static-manifest", required=True)
    parser.add_argument("--semantic-cache", default="")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--gpus", default="0,1,2,3,4,5,6,7")
    parser.add_argument("--split", default="deployment_inference")
    parser.add_argument("--cores", type=int, default=8)
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--stride-macro", type=int, default=256)
    parser.add_argument("--max-step-cycles", type=float, default=1024.0)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--tmp-root", required=True)
    parser.add_argument(
        "--posthoc-intervention",
        choices=(
            "full_real", "no_offline_hidden", "no_lora",
            "semantic_permute", "no_llm_branch",
        ),
        default="full_real",
    )
    parser.add_argument("--intervention-seed", type=int, default=20260719)
    parser.add_argument("--activation-diagnostics", action="store_true")
    return parser.parse_args()


def load_static_map(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        result[str(row["binary_name"])] = str(row["parquet"])
    return result


def true_makespan_cycles(meta: dict[str, Any]) -> float:
    cores = list(meta["cores"])
    first = min(int(row["first_commit_tick"]) for row in cores)
    last = max(int(row["last_commit_tick"]) for row in cores)
    return float(last - first) / float(meta["tick_per_cycle"])


def finite_mean(values: list[float]) -> float | None:
    selected = [float(value) for value in values if math.isfinite(float(value))]
    return sum(selected) / len(selected) if selected else None


def run_one(
    *,
    repo: Path,
    run_dir: Path,
    semantic_cache: Path | None,
    output_root: Path,
    tmp_root: Path,
    gpu: str,
    index: int,
    row: dict[str, Any],
    static_map: dict[str, str],
    max_steps: int,
    stride_macro: int,
    max_step_cycles: float,
    progress_every: int,
    split: str,
    trace_count: int,
    posthoc_intervention: str,
    intervention_seed: int,
    activation_diagnostics: bool,
    launcher_lock: threading.Lock,
) -> dict[str, Any]:
    workload = str(row["workload"])
    binary_name = workload.removeprefix("W_")
    if binary_name not in static_map:
        raise RuntimeError(f"no static parquet for {binary_name}")
    stem = f"{index:02d}_{workload}"
    json_path = output_root / "reports" / f"{stem}.json"
    log_path = output_root / "logs" / f"{stem}.log"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    gpu_tmp = tmp_root / f"g{gpu}"
    gpu_tmp.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(repo / "eval/rollout_macro_v29_checkpoint.py"),
        "--run-dir", str(run_dir),
        "--trace-root", str(row["cache_dir"]),
        "--static-dict", static_map[binary_name],
        "--max-steps", str(max_steps),
        "--stride-macro", str(stride_macro),
        "--max-step-cycles", str(max_step_cycles),
        "--device", "cuda:0",
        "--output", str(json_path),
        "--progress-every", str(progress_every),
        "--trace-index", str(index + 1),
        "--trace-count", str(trace_count),
        "--split", str(split),
        "--workload-role", str(row["workload_role"]),
        "--seed", str(row["seed"]),
        "--gpu-id", str(gpu),
        "--log-path", str(log_path),
        "--posthoc-intervention", str(posthoc_intervention),
        "--intervention-seed", str(intervention_seed),
    ]
    if semantic_cache is not None:
        command.extend(["--semantic-cache-root", str(semantic_cache)])
    if activation_diagnostics:
        command.append("--activation-diagnostics")
    environment = dict(os.environ)
    environment.update({
        "CUDA_VISIBLE_DEVICES": gpu,
        "TMPDIR": str(gpu_tmp),
        "HF_HUB_OFFLINE": environment.get("HF_HUB_OFFLINE", "1"),
        "TRANSFORMERS_OFFLINE": environment.get("TRANSFORMERS_OFFLINE", "1"),
        "TOKENIZERS_PARALLELISM": "false",
        "PYTHONPATH": (
            f"{repo}:/data00/yinhaolang/TCSim:"
            f"{environment.get('PYTHONPATH', '')}"
        ),
    })
    with launcher_lock:
        print(
            f"[deploy launch] [{index + 1}/{trace_count}] gpu={gpu} "
            f"{workload} log={log_path}",
            flush=True,
        )
    with log_path.open("w") as stream:
        process = subprocess.Popen(
            command,
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
        )
        assert process.stdout is not None
        for line in process.stdout:
            stream.write(line)
            stream.flush()
            with launcher_lock:
                sys.stdout.write(line)
                sys.stdout.flush()
        returncode = int(process.wait())
    if returncode != 0 or not json_path.is_file():
        with launcher_lock:
            print(
                f"[deploy fail] [{index + 1}/{trace_count}] gpu={gpu} "
                f"{workload} returncode={returncode} log={log_path}",
                flush=True,
            )
        return {
            "status": "FAIL",
            "workload": workload,
            "gpu": gpu,
            "returncode": returncode,
            "log": str(log_path),
        }
    report = json.loads(json_path.read_text())
    rollout = report["rollout"]
    metrics = report.get("deployment_metrics", {})
    meta = json.loads((Path(row["cache_dir"]) / "meta.json").read_text())
    truth = true_makespan_cycles(meta)
    predicted = float(rollout["global_time_cycles"])
    signed_error = (predicted - truth) / max(truth, 1.0e-12)
    record = {
        "status": str(report["status"]),
        "workload": workload,
        "workload_role": str(row["workload_role"]),
        "seed": int(row["seed"]),
        "n_cores": int(row["n_cores"]),
        "gpu": gpu,
        "trace_id": str(row["trace_id"]),
        "posthoc_intervention": str(report["posthoc_intervention"]),
        "complete": bool(rollout["complete"]),
        "exactly_once": rollout["exactly_once"] is not None,
        "steps": int(rollout["steps"]),
        "retired_macros": int(rollout["total_consumed_macros"]),
        "retired_uops": int(rollout["total_consumed_uops"]),
        "elapsed_s": float(rollout["elapsed_s"]),
        "aggregate_macro_per_s": float(rollout["aggregate_macro_per_s"]),
        "aggregate_uop_per_s": float(rollout["aggregate_uop_per_s"]),
        "retired_macros_per_model_forward": float(
            rollout.get("retired_macros_per_model_forward", 0.0)
        ),
        "mean_step_ms": float(rollout["mean_step_ms"]),
        "predicted_makespan_cycles": predicted,
        "true_makespan_cycles": truth,
        "makespan_signed_error": signed_error,
        "makespan_absolute_error": abs(signed_error),
        "macro_cpi_absolute_error": metrics.get(
            "macro_cpi_abs_relative_error"
        ),
        "macro_cpi_signed_error": metrics.get("macro_cpi_signed_error"),
        "micro_cpi_absolute_error": metrics.get(
            "micro_cpi_abs_relative_error"
        ),
        "micro_cpi_signed_error": metrics.get("micro_cpi_signed_error"),
        "core_cycle_mape_mean": metrics.get("core_cycle_mape_mean"),
        "core_cycle_mape_p50": metrics.get("core_cycle_mape_p50"),
        "core_cycle_mape_p90": metrics.get("core_cycle_mape_p90"),
        "core_cycle_mape_p99": metrics.get("core_cycle_mape_p99"),
        "core_cycle_signed_bias": metrics.get("core_cycle_signed_bias"),
        "branch_miss_count_absolute_error": metrics.get(
            "branch_miss_count_abs_relative_error"
        ),
        "branch_miss_rate_absolute_error_pp": metrics.get(
            "branch_miss_rate_abs_error_pp"
        ),
        "steps_per_s": float(rollout.get("steps_per_s", 0.0)),
        "capped_steps": int(rollout.get("capped_steps", 0)),
        "zero_core_rows": int(rollout.get("zero_core_rows", 0)),
        "gpu_peak_allocated_bytes": int(
            rollout.get("gpu_peak_allocated_bytes", 0)
        ),
        "gpu_peak_reserved_bytes": int(
            rollout.get("gpu_peak_reserved_bytes", 0)
        ),
        "predictor_timing": rollout.get("predictor_timing"),
        "report": str(json_path),
        "log": str(log_path),
    }
    with launcher_lock:
        print(
            f"[deploy done] [{index + 1}/{trace_count}] gpu={gpu} {workload} "
            f"complete={record['complete']} "
            f"macro/s={record['aggregate_macro_per_s']:.1f} "
            f"makespan-error={100.0 * abs(signed_error):.2f}% "
            f"report={json_path}",
            flush=True,
        )
    return record


def main() -> int:
    args = parse_args()
    repo = Path(__file__).resolve().parents[1]
    run_dir = Path(args.run_dir).resolve()
    run_contract = json.loads((run_dir / "run.json").read_text())
    input_mode = str(run_contract.get("semantic_input_mode", "native_token"))
    semantic_cache = (
        Path(args.semantic_cache).resolve() if args.semantic_cache else None
    )
    if input_mode == "cached_macro_soft_token" and semantic_cache is None:
        raise ValueError("cached-macro deployment requires --semantic-cache")
    if input_mode == "learned_null_macro_token" and semantic_cache is not None:
        raise ValueError("learned-null E deployment forbids --semantic-cache")
    output_root = Path(args.output_root).resolve()
    tmp_root = Path(args.tmp_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    tmp_root.mkdir(parents=True, exist_ok=True)
    required_files = [
        run_dir / "run.json",
        run_dir / "final_report.json",
        Path(args.dataset_manifest),
        Path(args.static_manifest),
    ]
    if semantic_cache is not None:
        required_files.append(semantic_cache / "manifest.json")
    for required in required_files:
        if not required.is_file():
            raise FileNotFoundError(required)
    manifest = json.loads(Path(args.dataset_manifest).read_text())
    rows = [
        dict(row) for row in manifest["splits"][args.split]
        if int(row["n_cores"]) == int(args.cores)
    ]
    if len(rows) != 23:
        raise RuntimeError(
            f"expected 23 c8 {args.split} traces, found {len(rows)}"
        )
    static_map = load_static_map(Path(args.static_manifest))
    gpus = [value.strip() for value in args.gpus.split(",") if value.strip()]
    if not gpus:
        raise ValueError("--gpus cannot be empty")
    if int(args.progress_every) < 0:
        raise ValueError("--progress-every must be non-negative")
    assignments = [rows[index::len(gpus)] for index in range(len(gpus))]
    launcher_lock = threading.Lock()

    def worker(gpu_index: int) -> list[dict[str, Any]]:
        records = []
        for local_index, row in enumerate(assignments[gpu_index]):
            original_index = rows.index(row)
            records.append(run_one(
                repo=repo,
                run_dir=run_dir,
                semantic_cache=semantic_cache,
                output_root=output_root,
                tmp_root=tmp_root,
                gpu=gpus[gpu_index],
                index=original_index,
                row=row,
                static_map=static_map,
                max_steps=int(args.max_steps),
                stride_macro=int(args.stride_macro),
                max_step_cycles=float(args.max_step_cycles),
                progress_every=int(args.progress_every),
                split=str(args.split),
                trace_count=len(rows),
                posthoc_intervention=str(args.posthoc_intervention),
                intervention_seed=int(args.intervention_seed),
                activation_diagnostics=bool(args.activation_diagnostics),
                launcher_lock=launcher_lock,
            ))
        return records

    records: list[dict[str, Any]] = []
    suite_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(gpus)) as executor:
        futures = [executor.submit(worker, index) for index in range(len(gpus))]
        for future in as_completed(futures):
            records.extend(future.result())
    suite_elapsed = time.perf_counter() - suite_started
    records.sort(key=lambda value: str(value["workload"]))
    successes = [record for record in records if record["status"] == "PASS"]
    throughput = [record["aggregate_macro_per_s"] for record in successes]
    absolute_error = [record["makespan_absolute_error"] for record in successes]
    signed_error = [record["makespan_signed_error"] for record in successes]
    macro_cpi_error = [
        float(record["macro_cpi_absolute_error"])
        for record in successes
        if record.get("macro_cpi_absolute_error") is not None
    ]
    branch_count_error = [
        float(record["branch_miss_count_absolute_error"])
        for record in successes
        if record.get("branch_miss_count_absolute_error") is not None
    ]
    total_macros = sum(record["retired_macros"] for record in successes)
    total_elapsed = sum(record["elapsed_s"] for record in successes)
    execution_pass = (
        len(successes) == len(rows)
        and all(record["complete"] and record["exactly_once"] for record in successes)
    )
    summary = {
        "status": "PASS" if execution_pass else "FAIL",
        "schema_version": "llmsim-macro-v29-deployment-eval-1",
        "contract": "macro-v29-c8-deployment-suite-v1",
        "run": {
            "run_dir": str(run_dir),
            "checkpoint": str(
                json.loads((run_dir / "final_report.json").read_text())["checkpoint"]
            ),
            "dataset_manifest": str(Path(args.dataset_manifest).resolve()),
            "split": str(args.split),
            "core_counts": [int(args.cores)],
            "gpu_count": len(gpus),
            "target_stride_macro": int(args.stride_macro),
            "max_step_cycles": float(args.max_step_cycles),
            "progress_every": int(args.progress_every),
            "model_context_uses_oracle_timing": False,
            "throughput_headline_unit": "macro/s",
            "posthoc_intervention": str(args.posthoc_intervention),
            "intervention_seed": int(args.intervention_seed),
            "activation_diagnostics": bool(args.activation_diagnostics),
            "wall_seconds": float(suite_elapsed),
        },
        "run_dir": str(run_dir),
        "split": str(args.split),
        "cores": int(args.cores),
        "trace_count": len(rows),
        "gpu_count": len(gpus),
        "max_steps": int(args.max_steps),
        "stride_macro": int(args.stride_macro),
        "progress_every": int(args.progress_every),
        "posthoc_intervention": str(args.posthoc_intervention),
        "intervention_seed": int(args.intervention_seed),
        "wall_seconds": float(suite_elapsed),
        "complete_traces": sum(bool(record.get("complete")) for record in successes),
        "throughput": {
            "mean_trace_macro_per_s": finite_mean(throughput),
            "median_trace_macro_per_s": (
                statistics.median(throughput) if throughput else None
            ),
            "min_trace_macro_per_s": min(throughput) if throughput else None,
            "max_trace_macro_per_s": max(throughput) if throughput else None,
            "weighted_macro_per_s": (
                total_macros / total_elapsed if total_elapsed > 0.0 else None
            ),
            "suite_wall_macro_per_s": (
                total_macros / suite_elapsed if suite_elapsed > 0.0 else None
            ),
            "traces_at_least_10k": sum(value >= 10000.0 for value in throughput),
            "gate_mean_at_least_10k": bool(
                throughput and finite_mean(throughput) >= 10000.0
            ),
        },
        "accuracy": {
            "mean_absolute_makespan_error": finite_mean(absolute_error),
            "median_absolute_makespan_error": (
                statistics.median(absolute_error) if absolute_error else None
            ),
            "max_absolute_makespan_error": (
                max(absolute_error) if absolute_error else None
            ),
            "mean_signed_makespan_error": finite_mean(signed_error),
            "mean_macro_cpi_absolute_error": finite_mean(macro_cpi_error),
            "mean_branch_miss_count_absolute_error": finite_mean(
                branch_count_error
            ),
        },
        "by_core_count": [{
            "n_cores": int(args.cores),
            "traces": len(records),
            "complete_free_running": sum(
                bool(record.get("complete")) for record in successes
            ),
            "macro_per_s_mean": finite_mean(throughput),
            "macro_per_s_median": (
                statistics.median(throughput) if throughput else None
            ),
            "makespan_mape": finite_mean(absolute_error),
            "macro_cpi_mape": finite_mean(macro_cpi_error),
        }],
        "records": records,
        "traces": records,
    }
    destination = output_root / "summary.json"
    destination.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    mean_rate = summary["throughput"]["mean_trace_macro_per_s"]
    median_rate = summary["throughput"]["median_trace_macro_per_s"]
    suite_rate = summary["throughput"]["suite_wall_macro_per_s"]
    print("\n" + "=" * 96, flush=True)
    print(f"## c{int(args.cores):02d} deployment suite complete", flush=True)
    print(
        f"[result] status={summary['status']} "
        f"complete={summary['complete_traces']}/{summary['trace_count']} "
        f"mean-macro/s={float(mean_rate or 0.0):.1f} "
        f"makespan-MAPE={100.0 * float(summary['accuracy']['mean_absolute_makespan_error'] or 0.0):.3f}%",
        flush=True,
    )
    print(
        "  throughput macro/s mean/median/min/max = "
        f"{float(mean_rate or 0.0):.1f} / "
        f"{float(median_rate or 0.0):.1f} / "
        f"{float(summary['throughput']['min_trace_macro_per_s'] or 0.0):.1f} / "
        f"{float(summary['throughput']['max_trace_macro_per_s'] or 0.0):.1f}; "
        f"suite-wall aggregate={float(suite_rate or 0.0):.1f}",
        flush=True,
    )
    print(
        f"  10K gate traces={summary['throughput']['traces_at_least_10k']}/"
        f"{summary['trace_count']} "
        f"mean-pass={summary['throughput']['gate_mean_at_least_10k']} "
        f"wall={suite_elapsed:.3f}s",
        flush=True,
    )
    print(f"[persisted] deployment_report={destination}", flush=True)
    print("=" * 96, flush=True)
    return 0 if execution_pass else 2


if __name__ == "__main__":
    raise SystemExit(main())
