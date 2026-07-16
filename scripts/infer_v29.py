#!/usr/bin/env python3
"""Evaluate a v29 checkpoint with oracle one-step and free-running rollout."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from typing import Any, Dict, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.v29.dataset import V29TraceStore
from tcsim.v29.inference import (
    aggregate_trace_reports,
    discover_sources,
    evaluate_oracle_one_step,
    load_checkpoint_runner,
    load_manifest_sources,
    render_text_report,
    run_free_running,
    write_evaluation_report,
)


def _csv_ints(value: str):
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def _csv_strings(value: str):
    return {part.strip() for part in value.split(",") if part.strip()}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "trace"


def _trace_paths(out_dir: str, source: Mapping[str, Any], store: V29TraceStore):
    workload = str(source.get("workload", store.meta.get("workload", "trace")))
    seed = source.get("seed", "unknown")
    parent = os.path.join(out_dir, "traces", f"c{len(store.core_ids)}")
    os.makedirs(parent, exist_ok=True)
    stem = f"{_safe_name(workload)}_seed{_safe_name(seed)}"
    return os.path.join(parent, stem + ".json"), os.path.join(parent, stem + ".log")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "v29 evaluation: oracle labels are used only by one-step metrics and "
            "post-transition drift audits; free-running contexts use predicted cursors."
        )
    )
    parser.add_argument("--ckpt", required=True)
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--manifest")
    source_group.add_argument("--cache-root")
    parser.add_argument(
        "--splits", default="seed0_inference,development_heldout",
        help="comma-separated manifest splits",
    )
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--mode", choices=("both", "oracle", "free"), default="both")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--amp-dtype", choices=("fp32", "bf16", "fp16"), default="bf16")
    parser.add_argument(
        "--sdpa-backend",
        choices=("auto", "flash", "no_flash", "efficient", "math"),
        default="",
    )
    parser.add_argument("--no-static-cache", action="store_true")
    parser.add_argument("--core-counts", default="")
    parser.add_argument("--workloads", default="")
    parser.add_argument("--seeds", default="")
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument("--max-oracle-samples", type=int, default=0)
    parser.add_argument("--max-free-steps", type=int, default=0)
    parser.add_argument("--target-stride", type=int, default=None)
    parser.add_argument("--min-step-cycles", type=float, default=None)
    parser.add_argument("--max-step-cycles", type=float, default=None)
    parser.add_argument("--max-no-progress-steps", type=int, default=None)
    parser.add_argument("--progress-every", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    if args.num_shards < 1 or not 0 <= args.shard_index < args.num_shards:
        raise SystemExit("require num-shards>=1 and 0<=shard-index<num-shards")
    split_names = [value.strip() for value in args.splits.split(",") if value.strip()]
    sources = (
        load_manifest_sources(args.manifest, split_names)
        if args.manifest else discover_sources(args.cache_root)
    )
    core_counts = _csv_ints(args.core_counts)
    workloads = _csv_strings(args.workloads)
    seeds = _csv_ints(args.seeds)
    if core_counts:
        sources = [row for row in sources if int(row.get("n_cores", 0)) in core_counts]
    if workloads:
        sources = [row for row in sources if str(row.get("workload", "")) in workloads]
    if seeds:
        sources = [row for row in sources if int(row.get("seed", -1)) in seeds]
    sources = [
        row for index, row in enumerate(sources)
        if index % args.num_shards == args.shard_index
    ]
    if args.max_traces > 0:
        sources = sources[:args.max_traces]
    if not sources:
        raise SystemExit("no v29 trace caches selected")

    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    runner = load_checkpoint_runner(
        args.ckpt,
        device=args.device,
        amp_dtype=args.amp_dtype,
        sdpa_backend=args.sdpa_backend or None,
        static_cache=not args.no_static_cache,
    )
    scheduler = runner.config.scheduler
    target_stride = int(
        args.target_stride
        if args.target_stride is not None else scheduler.get("target_stride", 32)
    )
    min_step = float(
        args.min_step_cycles
        if args.min_step_cycles is not None else scheduler.get("min_step_cycles", 4.0)
    )
    max_step = float(
        args.max_step_cycles
        if args.max_step_cycles is not None else scheduler.get("max_step_cycles", 1024.0)
    )
    max_no_progress = int(
        args.max_no_progress_steps
        if args.max_no_progress_steps is not None
        else scheduler.get("max_no_progress_steps", 64)
    )
    evaluation_contract = {
        "mode": args.mode,
        "max_oracle_samples": int(args.max_oracle_samples),
        "max_free_steps": int(args.max_free_steps),
        "target_stride": target_stride,
        "min_step_cycles_advisory": min_step,
        "max_step_cycles": max_step,
        "max_no_progress_steps": max_no_progress,
        "static_cache": not args.no_static_cache,
        "amp_dtype": args.amp_dtype,
        "sdpa_backend": args.sdpa_backend or runner.checkpoint_meta["sdpa_backend"],
    }
    reports = []
    failures = []
    for trace_index, source in enumerate(sources):
        store = V29TraceStore(str(source["cache_dir"]))
        json_path, log_path = _trace_paths(out_dir, source, store)
        if args.resume and os.path.isfile(json_path):
            with open(json_path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
            if (
                existing.get("checkpoint_id") == runner.checkpoint_meta["checkpoint_id"]
                and existing.get("evaluation_contract") == evaluation_contract
            ):
                reports.append(existing)
                print(
                    f"[v29 {trace_index + 1}/{len(sources)}] resume "
                    f"c{len(store.core_ids)} {existing.get('workload')}",
                    flush=True,
                )
                continue
        workload = str(source.get("workload", store.meta.get("workload", "")))
        seed = source.get("seed")
        print(
            f"[v29 {trace_index + 1}/{len(sources)}] start "
            f"c{len(store.core_ids)} {workload} seed={seed}",
            flush=True,
        )
        log_handle = open(log_path, "w", encoding="utf-8")
        event_count: Dict[str, int] = {}

        def emit(event: Mapping[str, Any]) -> None:
            phase = str(event.get("phase", "unknown"))
            event_count[phase] = event_count.get(phase, 0) + 1
            every = int(args.progress_every)
            if every <= 0 or event_count[phase] % every != 0:
                return
            line = "[{}] {}".format(
                phase,
                " ".join(f"{key}={value}" for key, value in event.items() if key != "phase"),
            )
            print(line, flush=True)
            log_handle.write(line + "\n")
            log_handle.flush()

        try:
            trace_report: Dict[str, Any] = {
                "trace_id": store.trace_id,
                "workload": workload,
                "workload_role": source.get("workload_role"),
                "seed": seed,
                "n_cores": len(store.core_ids),
                "cache_dir": store.cache_dir,
                "source_split": source.get("split"),
                "source_splits": list(source.get("source_splits", [source.get("split")])),
                "checkpoint": runner.checkpoint_meta["checkpoint"],
                "checkpoint_id": runner.checkpoint_meta["checkpoint_id"],
                "checkpoint_step": runner.checkpoint_meta["step"],
                "requested_mode": args.mode,
                "evaluation_contract": evaluation_contract,
            }
            if args.mode in {"both", "oracle"}:
                trace_report["oracle_one_step"] = evaluate_oracle_one_step(
                    store,
                    runner,
                    max_samples=args.max_oracle_samples,
                    progress=emit,
                )
            if args.mode in {"both", "free"}:
                trace_report["free_running"] = run_free_running(
                    store,
                    runner,
                    source=source,
                    target_stride=target_stride,
                    min_step_cycles=min_step,
                    max_step_cycles=max_step,
                    max_no_progress_steps=max_no_progress,
                    max_steps=args.max_free_steps,
                    progress=emit,
                )
            with open(json_path, "w", encoding="utf-8") as handle:
                json.dump(trace_report, handle, indent=2, ensure_ascii=False)
            if "free_running" in trace_report:
                free = trace_report["free_running"]
                summary = (
                    f"complete={free['complete']} scope={free['metric_scope']} "
                    f"micro-CPI={free['predicted_micro_cpi']:.5f}/"
                    f"{free['true_micro_cpi']:.5f} "
                    f"MAPE={free['micro_cpi_abs_relative_error'] * 100:.2f}% "
                    f"branch={free['predicted_branch_miss_rate']:.5f}/"
                    f"{free['true_branch_miss_rate']:.5f} "
                    "interval-offset-p99="
                    f"{free['oracle_cursor_interval_abs_offset_cycles']['p99']:.2f} "
                    f"steps/s={free['steps_per_s']:.2f}"
                )
                detail_lines = [
                    "[rollout] "
                    f"complete={free['complete']} scope={free['metric_scope']} "
                    f"steps={free['steps']} global_cycles={free['global_time_cycles']:.3f} "
                    f"uops={free['retired_uops']}/{free['full_true_uops']} "
                    f"macros={free['retired_macros']}/{free['full_true_macros']}",
                    "[cpi] "
                    f"micro={free['predicted_micro_cpi']:.6f}/{free['true_micro_cpi']:.6f} "
                    f"error={free['micro_cpi_abs_relative_error'] * 100.0:.3f}% "
                    f"macro={free['predicted_macro_cpi']:.6f}/{free['true_macro_cpi']:.6f} "
                    f"error={free['macro_cpi_abs_relative_error'] * 100.0:.3f}%",
                    "[cycles] "
                    f"sum={free['predicted_cycles_sum']:.3f}/{free['true_cycles_sum']:.3f} "
                    f"makespan={free['predicted_makespan']:.3f}/{free['true_makespan']:.3f} "
                    f"error={free['makespan_abs_relative_error'] * 100.0:.3f}%",
                    "[branch] "
                    f"opportunities={free['branch_opportunities']} "
                    f"misses={free['predicted_branch_misses']:.3f}/"
                    f"{free['true_branch_misses']} "
                    f"rate={free['predicted_branch_miss_rate']:.6f}/"
                    f"{free['true_branch_miss_rate']:.6f} "
                    f"rate_abs={free['branch_miss_rate_abs_error_pp']:.3f} pp",
                    "[drift] "
                    f"interval_abs_p50={free['oracle_cursor_interval_abs_offset_cycles']['p50']:.3f} "
                    f"p90={free['oracle_cursor_interval_abs_offset_cycles']['p90']:.3f} "
                    f"p99={free['oracle_cursor_interval_abs_offset_cycles']['p99']:.3f} "
                    f"max={free['oracle_cursor_interval_abs_offset_cycles']['max']:.3f} "
                    f"abs_slope={free['oracle_cursor_interval_abs_slope_cycles_per_cycle']:.8f}",
                    "[throughput] "
                    f"elapsed={free['elapsed_s']:.3f}s steps/s={free['steps_per_s']:.3f} "
                    f"uops/s={free['uops_per_s']:.1f} "
                    f"static_cache_hit={free['static_cache_hit_rate']:.4f} "
                    f"gpu_peak_bytes={free['gpu_peak_memory_bytes']}",
                ]
                detail_lines.extend(
                    "[core] "
                    f"id={core['core_id']} uops={core['retired_uops']} "
                    f"cycles={core['predicted_cycles']:.3f}/{core['true_cycles']:.3f} "
                    f"cycle_error={core['cycle_abs_relative_error'] * 100.0:.3f}% "
                    f"branch_rate={core['predicted_branch_miss_rate']:.6f}/"
                    f"{core['true_branch_miss_rate']:.6f} "
                    f"final_progress_error={core['final_oracle_progress_error_uops']}"
                    for core in free["per_core"]
                )
            else:
                oracle = trace_report["oracle_one_step"]
                summary = (
                    f"oracle-log-MAE={oracle['commit_time_log_error']['mae']:.5f} "
                    f"branch-Brier={oracle['branch_token']['brier']:.5f}"
                )
                detail_lines = []
            if "oracle_one_step" in trace_report:
                oracle = trace_report["oracle_one_step"]
                detail_lines.append(
                    "[oracle] "
                    f"samples={oracle['samples']} rows={oracle['active_core_rows']} "
                    f"commit_log_mae={oracle['commit_time_log_error']['mae']:.6f} "
                    f"commit_cycle_mae={oracle['commit_time_cycle_error']['mae']:.3f} "
                    f"branch_brier={oracle['branch_token']['brier']:.6f} "
                    f"branch_auc={oracle['branch_token']['auc_histogram']}"
                )
            log_handle.write("[result] " + summary + "\n")
            for line in detail_lines:
                log_handle.write(line + "\n")
            log_handle.flush()
            reports.append(trace_report)
            print(f"[v29 result] c{len(store.core_ids)} {workload}: {summary}", flush=True)
        except Exception as exc:
            failure = {
                "cache_dir": store.cache_dir,
                "workload": workload,
                "n_cores": len(store.core_ids),
                "error": repr(exc),
                "traceback": traceback.format_exc(),
            }
            failures.append(failure)
            log_handle.write(failure["traceback"] + "\n")
            print(
                f"[v29 FAIL] c{len(store.core_ids)} {workload}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            if args.fail_fast:
                raise
        finally:
            log_handle.close()

    aggregate = aggregate_trace_reports(
        reports,
        run={
            "created_unix": time.time(),
            "checkpoint": runner.checkpoint_meta["checkpoint"],
            "checkpoint_id": runner.checkpoint_meta["checkpoint_id"],
            "checkpoint_step": runner.checkpoint_meta["step"],
            "manifest": os.path.abspath(args.manifest) if args.manifest else None,
            "cache_root": os.path.abspath(args.cache_root) if args.cache_root else None,
            "splits": split_names,
            "mode": args.mode,
            "num_shards": args.num_shards,
            "shard_index": args.shard_index,
            "target_stride": target_stride,
            "min_step_cycles_advisory": min_step,
            "max_step_cycles": max_step,
            "max_no_progress_steps": max_no_progress,
            "evaluation_contract": evaluation_contract,
            "failures": failures,
            "oracle_rollout_context_consumed": False,
            "predicted_context_used_for_labels": False,
        },
    )
    paths = write_evaluation_report(out_dir, aggregate)
    print(render_text_report(aggregate), end="", flush=True)
    print(f"[v29 report] json={paths['json']} text={paths['text']}", flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
