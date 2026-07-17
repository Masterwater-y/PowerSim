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

from tcsim.v29.dataset import (
    CONTEXT_BUILDER,
    CONTEXT_PHASE_NAMES,
    CONTEXT_TIMING_CONTRACT,
    V29TraceStore,
)
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
from tcsim.v29.model import TIMING_ACCUMULATION_CONTRACT


CONTEXT_LOG_PHASES = CONTEXT_PHASE_NAMES + ("call_overhead",)
CONTEXT_LOG_LABELS = {
    "active_core_selection": "select",
    "per_core_window": "window",
    "cross_core_features": "cross-core",
    "state_and_targets": "state+targets",
    "tensor_assembly": "tensor",
    "call_overhead": "overhead",
}


def _context_phase_ms_text(values: Mapping[str, Any], divisor: float = 1.0) -> str:
    divisor = max(1.0e-12, float(divisor))
    return " / ".join(
        f"{1000.0 * float(values.get(name, 0.0)) / divisor:.2f}"
        for name in CONTEXT_LOG_PHASES
    )


def _context_phase_labels() -> str:
    return "/".join(CONTEXT_LOG_LABELS[name] for name in CONTEXT_LOG_PHASES)


def _csv_ints(value: str):
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def _csv_strings(value: str):
    return {part.strip() for part in value.split(",") if part.strip()}


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_") or "trace"


def _trace_paths(
    out_dir: str,
    source: Mapping[str, Any],
    store: V29TraceStore,
    *,
    trace_log_dir: str = "",
):
    workload = str(source.get("workload", store.meta.get("workload", "trace")))
    seed = source.get("seed", "unknown")
    if trace_log_dir:
        parent = os.path.join(trace_log_dir, f"c{len(store.core_ids):02d}")
        os.makedirs(parent, exist_ok=True)
        return "", os.path.join(parent, f"{_safe_name(workload)}.log")
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
    parser.add_argument(
        "--worker-report", default="",
        help="optional exact worker JSON path for v28-compatible sharded layout",
    )
    parser.add_argument(
        "--trace-log-dir", default="",
        help="optional shared trace_logs directory using cXX/workload.log paths",
    )
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
    parser.add_argument(
        "--oracle-drift-diagnostics",
        action="store_true",
        help=(
            "collect per-step oracle cursor/head drift during free rollout; "
            "disabled by default because it does not affect scheduling"
        ),
    )
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
    worker_report = os.path.abspath(args.worker_report) if args.worker_report else ""
    trace_log_dir = os.path.abspath(args.trace_log_dir) if args.trace_log_dir else ""
    if bool(worker_report) != bool(trace_log_dir):
        raise SystemExit("--worker-report and --trace-log-dir must be used together")
    if worker_report:
        os.makedirs(os.path.dirname(worker_report), exist_ok=True)
        os.makedirs(trace_log_dir, exist_ok=True)
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
        "timing_accumulation": TIMING_ACCUMULATION_CONTRACT,
        "free_fast_path": True,
        "context_builder": CONTEXT_BUILDER,
        "context_timing": CONTEXT_TIMING_CONTRACT,
        "cpu_window_cache": "last-window-per-core",
        "oracle_drift_diagnostics": bool(args.oracle_drift_diagnostics),
        "static_cache": not args.no_static_cache,
        "amp_dtype": args.amp_dtype,
        "sdpa_backend": args.sdpa_backend or runner.checkpoint_meta["sdpa_backend"],
    }
    state_jsonl = worker_report + ".traces.jsonl" if worker_report else ""
    existing_by_cache: Dict[str, Dict[str, Any]] = {}
    if worker_report and args.resume and os.path.isfile(state_jsonl):
        with open(state_jsonl, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    existing = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"invalid resume JSONL {state_jsonl}:{line_number}: {exc}"
                    ) from exc
                if (
                    existing.get("checkpoint_id") == runner.checkpoint_meta["checkpoint_id"]
                    and existing.get("evaluation_contract") == evaluation_contract
                ):
                    cache_key = os.path.abspath(str(existing.get("cache_dir", "")))
                    if cache_key:
                        existing_by_cache[cache_key] = existing
    state_handle = None
    if worker_report:
        # Compact the resume file to one compatible row per trace before new
        # results are appended.  This gives interrupted workers v28-style
        # trace-granular resume without retaining stale checkpoint contracts.
        state_handle = open(state_jsonl, "w", encoding="utf-8")
        for existing in existing_by_cache.values():
            state_handle.write(json.dumps(existing, ensure_ascii=False) + "\n")
        state_handle.flush()
    reports = []
    failures = []
    for trace_index, source in enumerate(sources):
        store = V29TraceStore(str(source["cache_dir"]))
        json_path, log_path = _trace_paths(
            out_dir, source, store, trace_log_dir=trace_log_dir,
        )
        existing = existing_by_cache.get(os.path.abspath(store.cache_dir))
        if not worker_report and args.resume and os.path.isfile(json_path):
            with open(json_path, "r", encoding="utf-8") as handle:
                existing = json.load(handle)
        if existing is not None:
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
        log_handle = open(log_path, "w", encoding="utf-8")
        event_count: Dict[str, int] = {}
        phase_started: Dict[str, float] = {}

        def write_trace(line: str = "", *, error: bool = False) -> None:
            print(line, file=sys.stderr if error else sys.stdout, flush=True)
            log_handle.write(line + "\n")
            log_handle.flush()

        amp_dtype = (
            "fp32" if runner.amp_dtype is None
            else str(runner.amp_dtype).replace("torch.", "")
        )
        write_trace()
        write_trace("=" * 96)
        write_trace(
            f"## [{trace_index + 1}/{len(sources)}] {workload} "
            f"c{len(store.core_ids):02d} samples={len(store)} K={store.K} seed={seed}"
        )
        write_trace("   evaluator=v29 workload_source=v28-compatible trace suite")
        write_trace(
            f"   cache={store.cache_dir} split={source.get('split')} "
            f"uops={store.meta.get('n_uops', 'unknown')}"
        )
        write_trace(
            f"   checkpoint={runner.checkpoint_meta['checkpoint']} "
            f"step={runner.checkpoint_meta['step']} device={runner.device} "
            f"amp={amp_dtype} sdpa={runner.checkpoint_meta['sdpa_backend']}"
        )
        write_trace(
            f"   mode={args.mode} target_stride={target_stride} "
            f"step_cycles={min_step:g}..{max_step:g} "
            f"max_no_progress={max_no_progress}"
        )
        write_trace(
            "   free_fast_path=on horizon_outputs=off "
            f"oracle_drift={'on' if args.oracle_drift_diagnostics else 'off'} "
            f"progress_every={args.progress_every}"
        )
        write_trace(
            f"   context_builder={CONTEXT_BUILDER} "
            f"context_timing={CONTEXT_TIMING_CONTRACT} "
            "cpu_window_cache=last-window-per-core "
            f"gpu_static_cache={'off' if args.no_static_cache else 'last-window'}"
        )
        write_trace(f"   independent_log={log_path}")

        def emit(event: Mapping[str, Any]) -> None:
            phase = str(event.get("phase", "unknown"))
            event_count[phase] = event_count.get(phase, 0) + 1
            phase_started.setdefault(phase, time.perf_counter())
            every = int(args.progress_every)
            pre_throttled = bool(event.get("_pre_throttled", False))
            if not pre_throttled and (
                every <= 0 or event_count[phase] % every != 0
            ):
                return
            elapsed = max(
                1.0e-9, time.perf_counter() - phase_started[phase],
            )
            if phase == "oracle_one_step":
                sample = int(event.get("sample", 0))
                samples = int(event.get("samples", 0))
                rows = int(event.get("rows", 0))
                tokens = int(event.get("tokens", 0))
                percentage = 100.0 * sample / max(1, samples)
                line = (
                    f"   [{workload} c{len(store.core_ids):02d}] oracle "
                    f"sample={sample}/{samples} progress={percentage:.1f}% "
                    f"rows={rows} uops={tokens} rows/s={rows / elapsed:.1f} "
                    f"uops/s={tokens / elapsed:.0f} wall={elapsed:.1f}s"
                )
            elif phase == "free_running":
                step = int(event.get("step", 0))
                retired = int(event.get("retired_uops", 0))
                total = int(event.get("total_uops", 0))
                percentage = 100.0 * retired / max(1, total)
                if "running_predicted_roi_uop_cpi" in event:
                    line = (
                        f"   [{workload} c{len(store.core_ids):02d}] window={step} "
                        f"progress={percentage:.1f}% uops={retired}/{total} "
                        f"uops/s={retired / elapsed:.0f} running ROI-CPI "
                        f"pred={float(event['running_predicted_roi_uop_cpi']):.4f} "
                        f"label={float(event['running_true_roi_uop_cpi']):.4f} "
                        f"err={float(event['running_roi_uop_cpi_abs_relative_error']) * 100.0:.2f}%"
                    )
                else:
                    line = (
                        f"   [{workload} c{len(store.core_ids):02d}] window={step} "
                        f"progress={percentage:.1f}% uops={retired}/{total} "
                        f"uops/s={retired / elapsed:.0f}"
                    )
            else:
                line = "[{}] {} wall={:.1f}s".format(
                    phase,
                    " ".join(
                        f"{key}={value}" for key, value in event.items()
                        if key != "phase" and not str(key).startswith("_")
                    ),
                    elapsed,
                )
            write_trace(line)
            if phase == "free_running":
                write_trace(
                    f"      active={int(event.get('active_cores', 0))} "
                    f"forwards={step} useful_uops/step="
                    f"{float(event.get('retired_uops_per_step', retired / max(1, step))):.1f} "
                    f"global/delta={float(event.get('global_time', 0.0)):.1f}/"
                    f"{float(event.get('delta', 0.0)):.1f} "
                    f"avg_step={1000.0 * elapsed / max(1, step):.2f}ms "
                    f"wall={elapsed:.1f}s"
                )
                phase_ms = event.get("context_phase_avg_ms", {})
                if isinstance(phase_ms, Mapping):
                    write_trace(
                        "      context avg ms/forward "
                        f"total/{_context_phase_labels()} = "
                        f"{float(event.get('context_total_avg_ms', 0.0)):.2f} / "
                        + " / ".join(
                            f"{float(phase_ms.get(name, 0.0)):.2f}"
                            for name in CONTEXT_LOG_PHASES
                        )
                    )

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
            # The deployment rollout is the primary v29 validation.  In both
            # mode run it first so v28-style running ROI-CPI appears
            # immediately; oracle one-step is the secondary diagnostic.
            if args.mode in {"both", "free"}:
                phase_started["free_running"] = time.perf_counter()
                trace_report["free_running"] = run_free_running(
                    store,
                    runner,
                    source=source,
                    target_stride=target_stride,
                    min_step_cycles=min_step,
                    max_step_cycles=max_step,
                    max_no_progress_steps=max_no_progress,
                    max_steps=args.max_free_steps,
                    collect_oracle_drift=args.oracle_drift_diagnostics,
                    progress_interval=args.progress_every,
                    progress=emit,
                )
            if args.mode in {"both", "oracle"}:
                phase_started["oracle_one_step"] = time.perf_counter()
                trace_report["oracle_one_step"] = evaluate_oracle_one_step(
                    store,
                    runner,
                    max_samples=args.max_oracle_samples,
                    progress=emit,
                )
                oracle = trace_report["oracle_one_step"]
                write_trace()
                write_trace(f"## {workload} c{len(store.core_ids):02d} oracle one-step complete")
                write_trace(
                    "  commit-time log MAE/p50/p90/p99 = "
                    f"{oracle['commit_time_log_error']['mae']:.6f} / "
                    f"{oracle['commit_time_log_error']['p50_abs']:.6f} / "
                    f"{oracle['commit_time_log_error']['p90_abs']:.6f} / "
                    f"{oracle['commit_time_log_error']['p99_abs']:.6f}; "
                    "cycle MAE/p99 = "
                    f"{oracle['commit_time_cycle_error']['mae']:.3f} / "
                    f"{oracle['commit_time_cycle_error']['p99_abs']:.3f}"
                )
                write_trace(
                    "  branch token BCE/Brier/AUC/ECE = "
                    f"{oracle['branch_token']['bce']:.6f} / "
                    f"{oracle['branch_token']['brier']:.6f} / "
                    f"{oracle['branch_token']['auc_histogram']} / "
                    f"{oracle['branch_token']['ece']:.6f}"
                )
                for horizon, row in oracle["horizons"].items():
                    write_trace(
                        f"  horizon={float(horizon):g} progress MAE/p90/p99="
                        f"{row['progress']['mae']:.3f}/"
                        f"{row['progress']['p90_abs']:.3f}/"
                        f"{row['progress']['p99_abs']:.3f}; "
                        f"branch-count MAE={row['branch_count']['mae']:.3f}; "
                        f"branch-rate={row['predicted_branch_rate']:.6f}/"
                        f"{row['true_branch_rate']:.6f} "
                        f"abs={row['branch_rate_abs_error_pp']:.3f} pp"
                    )
                write_trace(
                    f"  throughput rows/s={oracle['core_rows_per_s']:.1f} "
                    f"uops/s={oracle['uops_per_s']:.0f}; "
                    f"elapsed={oracle['elapsed_s']:.2f}s; "
                    f"CPU-window-cache hit={oracle['cpu_window_cache_hit_rate']:.2%}; "
                    f"GPU-static-cache hit={oracle['static_cache_hit_rate']:.2%}; "
                    f"GPU peak={oracle['gpu_peak_memory_bytes'] / (1024 ** 3):.2f} GiB"
                )
                oracle_context_phases = oracle.get("context_phase_seconds", {})
                oracle_context_calls = max(1, int(oracle.get("context_calls", 0)))
                if isinstance(oracle_context_phases, Mapping):
                    write_trace(
                        "  context phases avg ms/context "
                        f"{_context_phase_labels()} = "
                        f"{_context_phase_ms_text(oracle_context_phases, oracle_context_calls)}"
                    )
            if worker_report:
                assert state_handle is not None
                state_handle.write(json.dumps(trace_report, ensure_ascii=False) + "\n")
                state_handle.flush()
                persisted_path = state_jsonl
            else:
                with open(json_path, "w", encoding="utf-8") as handle:
                    json.dump(trace_report, handle, indent=2, ensure_ascii=False)
                persisted_path = json_path
            if "free_running" in trace_report:
                free = trace_report["free_running"]
                drift_summary = (
                    "interval-offset-p99="
                    f"{free['oracle_cursor_interval_abs_offset_cycles']['p99']:.2f}"
                    if free["oracle_drift_diagnostics_enabled"]
                    else "oracle-drift=off"
                )
                summary = (
                    f"complete={free['complete']} scope={free['metric_scope']} "
                    f"ROI-CPI={free['pred_roi_cpi']:.5f}/"
                    f"{free['true_roi_cpi']:.5f} "
                    f"error={free['roi_cpi_error'] * 100:.2f}% "
                    f"branch={free['predicted_branch_miss_rate']:.5f}/"
                    f"{free['true_branch_miss_rate']:.5f} "
                    f"{drift_summary} "
                    f"steps/s={free['steps_per_s']:.2f}"
                )
                timing = free["timing_breakdown"]
                steps = max(1, int(free["steps"]))
                context_phases = timing.get("context_phase_seconds", {})
                if not isinstance(context_phases, Mapping):
                    context_phases = {}
                context_total = max(
                    1.0e-12, float(timing["context_build_seconds"]),
                )
                context_phase_shares = " / ".join(
                    f"{100.0 * float(context_phases.get(name, 0.0)) / context_total:.1f}%"
                    for name in CONTEXT_LOG_PHASES
                )
                detail_lines = [
                    "  rollout "
                    f"complete={free['complete']} scope={free['metric_scope']} "
                    f"steps={free['steps']} global_cycles={free['global_time_cycles']:.3f} "
                    f"uops={free['retired_uops']}/{free['full_true_uops']} "
                    f"macros={free['retired_macros']}/{free['full_true_macros']}",
                    "  ROI CPI (consumed valid-label UOPs) pred/label = "
                    f"{free['pred_roi_cpi']:.6f} / {free['true_roi_cpi']:.6f}; "
                    f"relative error = {free['roi_cpi_error'] * 100.0:.3f}%; "
                    f"rollout coverage = {free['roi_completion_fraction'] * 100.0:.2f}%",
                    "  per-core ROI CPI error mean/p50/p90/p99 = "
                    f"{free['core_roi_cpi_mape_mean'] * 100.0:.3f}% / "
                    f"{free['core_roi_cpi_mape_p50'] * 100.0:.3f}% / "
                    f"{free['core_roi_cpi_mape_p90'] * 100.0:.3f}% / "
                    f"{free['core_roi_cpi_mape_p99'] * 100.0:.3f}%; "
                    f"signed bias = {free['core_roi_cpi_signed_bias'] * 100.0:.3f}%",
                    "  cycles pred/label = "
                    f"{free['predicted_cycles_sum']:.3f} / {free['true_cycles_sum']:.3f}; "
                    f"makespan pred/label/error = {free['predicted_makespan']:.3f} / "
                    f"{free['true_makespan']:.3f} / "
                    f"{free['makespan_abs_relative_error'] * 100.0:.3f}%",
                    "  branch miss count pred/label = "
                    f"{free['predicted_branch_misses']:.3f} / {free['true_branch_misses']}; "
                    f"retired branches = {free['branch_opportunities']}; "
                    f"rate pred/label = {free['predicted_branch_miss_rate'] * 100.0:.3f}% / "
                    f"{free['true_branch_miss_rate'] * 100.0:.3f}%; "
                    f"abs delta = {free['branch_miss_rate_abs_error_pp']:.3f} percentage-points",
                    "  scheduler steps/forwards/target-stride/uops-per-forward = "
                    f"{free['steps']} / {free['model_forwards']} / "
                    f"{free['target_stride']} / {free['retired_uops_per_model_forward']:.1f}; "
                    f"overshoot/min-step/no-progress = {free['stride_overshoot_rows']} / "
                    f"{free['advisory_min_step_violations']} / {free['no_progress_steps']}",
                    "  throughput uops/s="
                    f"{free['uops_per_s']:.1f} windows/s={free['steps_per_s']:.3f}; "
                    f"avg step={1000.0 * free['elapsed_s'] / max(1, free['steps']):.2f}ms; "
                    f"wall={free['elapsed_s']:.3f}s; "
                    f"CPU-window-cache hit rate="
                    f"{free['cpu_window_cache_hit_rate'] * 100.0:.2f}%; "
                    f"GPU-static-cache hit rate="
                    f"{free['static_cache_hit_rate'] * 100.0:.2f}%; "
                    f"GPU peak={free['gpu_peak_memory_bytes'] / (1024 ** 3):.2f} GiB",
                    "  cache CPU-window hits/misses/entries = "
                    f"{free['cpu_window_cache_hits']} / "
                    f"{free['cpu_window_cache_misses']} / "
                    f"{free['cpu_window_cache_entries']}; "
                    "GPU-static hits/misses/evictions = "
                    f"{free['static_cache_hits']} / "
                    f"{free['static_cache_misses']} / "
                    f"{free['static_cache_evictions']}",
                    "  timing avg ms/forward context/predict/model/D2H/scheduler = "
                    f"{1000.0 * timing['context_build_seconds'] / steps:.2f} / "
                    f"{1000.0 * timing['predict_seconds'] / steps:.2f} / "
                    f"{1000.0 * float(free.get('model_forward_seconds', 0.0)) / steps:.2f} / "
                    f"{1000.0 * float(free.get('output_transfer_seconds', 0.0)) / steps:.2f} / "
                    f"{1000.0 * timing['scheduler_seconds'] / steps:.2f}; "
                    f"fast-path calls={int(free.get('free_fast_path_calls', 0))}",
                    "  context phases avg ms/forward "
                    f"{_context_phase_labels()} = "
                    f"{_context_phase_ms_text(context_phases, steps)}",
                    "  context phase share "
                    f"{_context_phase_labels()} = {context_phase_shares}",
                ]
                if free["oracle_drift_diagnostics_enabled"]:
                    detail_lines.append(
                        "  oracle cursor-interval |offset| p50/p90/p99/max = "
                        f"{free['oracle_cursor_interval_abs_offset_cycles']['p50']:.3f} / "
                        f"{free['oracle_cursor_interval_abs_offset_cycles']['p90']:.3f} / "
                        f"{free['oracle_cursor_interval_abs_offset_cycles']['p99']:.3f} / "
                        f"{free['oracle_cursor_interval_abs_offset_cycles']['max']:.3f} cycles; "
                        f"abs slope={free['oracle_cursor_interval_abs_slope_cycles_per_cycle']:.8f}"
                    )
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
            write_trace()
            write_trace(f"## {workload} c{len(store.core_ids):02d} complete")
            write_trace("[result] " + summary)
            for line in detail_lines:
                write_trace(line)
            write_trace(f"[persisted] trace_result={persisted_path}")
            reports.append(trace_report)
            write_trace("=" * 96)
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
            write_trace(
                f"[v29 FAIL] c{len(store.core_ids)} {workload}: {exc}",
                error=True,
            )
            if args.fail_fast:
                raise
        finally:
            log_handle.close()

    if state_handle is not None:
        state_handle.close()

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
    if worker_report:
        with open(worker_report, "w", encoding="utf-8") as handle:
            json.dump(aggregate, handle, indent=2, ensure_ascii=False)
        paths = {"json": worker_report, "text": ""}
    else:
        paths = write_evaluation_report(out_dir, aggregate)
    print(render_text_report(aggregate), end="", flush=True)
    if worker_report:
        print(
            f"[v29 worker] report={paths['json']} traces={state_jsonl}",
            flush=True,
        )
    else:
        print(f"[v29 report] json={paths['json']} text={paths['text']}", flush=True)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
