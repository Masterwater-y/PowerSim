#!/usr/bin/env python3
"""Run a checkpoint in predicted-state deployment mode over packed traces."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import traceback

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.inference.deployment import (  # noqa: E402
    ModelContextPredictor,
    append_trace_jsonl,
    aggregate_trace_reports,
    discover_packed_rollouts,
    evaluate_packed_rollouts,
    load_checkpoint_model,
    load_manifest_rollouts,
    write_report,
)


def _csv_ints(value: str):
    return [int(part.strip()) for part in value.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Free-running full-QKVR inference.  Packed chunks are read directly; "
            "oracle rollout.jsonl contexts are never consumed."
        )
    )
    parser.add_argument("--ckpt", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--rollout-root")
    parser.add_argument("--split", default="deployment_inference")
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default="")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--amp-dtype", choices=("fp32", "bf16", "fp16"), default="")
    parser.add_argument(
        "--sdpa-backend",
        choices=("auto", "flash", "no_flash", "efficient", "math"),
        default="",
    )
    parser.add_argument("--epsilon", type=float, default=None)
    parser.add_argument("--max-resident-exposure", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument(
        "--max-chunks-per-core", type=int, default=0,
        help="bounded prefix for smoke/profile runs; 0 evaluates the full trace",
    )
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument(
        "--core-counts", default="",
        help="optional comma-separated physical core counts, for example 8,32",
    )
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--static-cache-entries", type=int, default=256)
    parser.add_argument("--prefix-lens", default="4,8,16,32")
    parser.add_argument("--no-oracle-schedule", action="store_true")
    parser.add_argument("--ignore-sync-force", action="store_true")
    parser.add_argument("--dump-steps-dir", default="")
    parser.add_argument(
        "--progress-every-steps", type=int, default=200,
        help="emit a TSim-style running ROI/timing line every N scheduler windows; 0 disables",
    )
    parser.add_argument(
        "--trace-jsonl", default="",
        help="incremental per-trace results; defaults to <out>.traces.jsonl",
    )
    parser.add_argument(
        "--trace-log-dir", default="",
        help="one complete log per <core-count>/<workload>",
    )
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    split_names = [name.strip() for name in args.split.split(",") if name.strip()]
    if args.manifest:
        sources = []
        for split_name in split_names:
            sources.extend(load_manifest_rollouts(args.manifest, split_name))
        # A rollout may be referenced by more than one manifest split (for
        # example seed0 train/validation sample partitions).  Deployment reads
        # whole packed traces, so evaluate every physical trace exactly once.
        unique = {}
        for row in sources:
            unique.setdefault(os.path.abspath(str(row["rollout_dir"])), row)
        sources = sorted(
            unique.values(),
            key=lambda row: (
                int(row.get("n_cores", 0)),
                str(row.get("workload", "")),
                str(row["rollout_dir"]),
            ),
        )
    else:
        sources = discover_packed_rollouts(args.rollout_root)
    selected_core_counts = set(_csv_ints(args.core_counts))
    if selected_core_counts:
        sources = [
            row for row in sources
            if int(row.get("n_cores", 0)) in selected_core_counts
        ]
    if not sources:
        detail = (
            f"manifest split(s) {split_names!r} are empty"
            if args.manifest else f"no packed rollout cache under {args.rollout_root}"
        )
        raise SystemExit(
            f"{detail}; collect seed1 and rebuild the manifest/cache before deployment eval"
        )
    if args.max_traces > 0:
        sources = sources[: args.max_traces]
    if args.num_shards < 1 or not 0 <= args.shard_index < args.num_shards:
        raise SystemExit("require num_shards>=1 and 0<=shard_index<num_shards")
    all_source_count = len(sources)
    sources = [
        row for index, row in enumerate(sources)
        if index % args.num_shards == args.shard_index
    ]
    shard_source_count = len(sources)
    if not sources:
        raise SystemExit(
            f"shard {args.shard_index}/{args.num_shards} has no traces "
            f"(selected total={all_source_count})"
        )

    trace_jsonl = args.trace_jsonl or (args.out + ".traces.jsonl")
    if not args.resume and os.path.exists(trace_jsonl):
        os.remove(trace_jsonl)
    checkpoint_stat = os.stat(args.ckpt)
    resume_checkpoint_id = (
        f"{os.path.abspath(args.ckpt)}:{checkpoint_stat.st_size}:"
        f"{checkpoint_stat.st_mtime_ns}"
    )
    completed_by_id = {}
    if args.resume and os.path.isfile(trace_jsonl):
        with open(trace_jsonl, "r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if (
                    row.get("trace_id")
                    and row.get("checkpoint_id") == resume_checkpoint_id
                ):
                    completed_by_id[str(row["trace_id"])] = row

    def source_trace_info(row):
        with open(os.path.join(row["rollout_dir"], "meta.json"), "r", encoding="utf-8") as handle:
            meta = json.load(handle)
        counts = [int(value) for value in meta["packed"]["core_counts"].values()]
        if args.max_chunks_per_core > 0:
            counts = [min(value, args.max_chunks_per_core) for value in counts]
        return str(meta["trace_id"]), sum(counts)

    source_info = {
        os.path.abspath(str(row["rollout_dir"])): source_trace_info(row)
        for row in sources
    }
    selected_ids = {value[0] for value in source_info.values()}
    completed_by_id = {
        trace_id: row for trace_id, row in completed_by_id.items()
        if trace_id in selected_ids
        and int(row.get("n_chunks", -1)) == next(
            total for candidate, total in source_info.values() if candidate == trace_id
        )
    }
    existing_rows = [
        completed_by_id[trace_id] for trace_id in sorted(selected_ids)
        if trace_id in completed_by_id
    ]
    sources = [
        row for row in sources
        if source_info[os.path.abspath(str(row["rollout_dir"]))][0] not in completed_by_id
    ]
    if not sources:
        report = aggregate_trace_reports(
            existing_rows,
            run={
                "checkpoint": os.path.abspath(args.ckpt),
                "split": args.split,
                "num_shards": args.num_shards,
                "shard_index": args.shard_index,
                "resumed_traces": len(existing_rows),
                "trace_jsonl": os.path.abspath(trace_jsonl),
                "oracle_rollout_context_consumed": False,
            },
        )
        write_report(args.out, report)
        print(
            f"[deploy] shard already complete via resume: traces={len(existing_rows)} "
            f"report={args.out}", flush=True,
        )
        return 0

    model, cfg, checkpoint_meta = load_checkpoint_model(
        args.ckpt,
        device=args.device,
        config_path=args.config or None,
        sdpa_backend=args.sdpa_backend or None,
    )
    amp_dtype = args.amp_dtype or str(cfg.train.get("amp_dtype", "bf16"))
    epsilon = cfg.epsilon if args.epsilon is None else float(args.epsilon)
    max_exposure = (
        int(cfg.scheduler.get("max_resident_exposure", 0))
        if args.max_resident_exposure is None else int(args.max_resident_exposure)
    )
    predictor = ModelContextPredictor(
        model,
        device=args.device,
        checkpoint_id=checkpoint_meta["checkpoint_id"],
        amp_dtype=amp_dtype,
        static_cache_entries=args.static_cache_entries,
    )
    prefix_lens = _csv_ints(args.prefix_lens)
    started = time.time()
    trace_log_dir = args.trace_log_dir or os.path.join(
        os.path.dirname(os.path.abspath(args.out)), "trace_logs"
    )
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    current_trace_log = {"handle": None, "path": None}

    class TeeStream:
        def __init__(self, primary, mirror):
            self.primary = primary
            self.mirror = mirror

        def write(self, value):
            self.primary.write(value)
            self.mirror.write(value)
            return len(value)

        def flush(self):
            self.primary.flush()
            self.mirror.flush()

        def __getattr__(self, name):
            return getattr(self.primary, name)

    def close_trace_log():
        handle = current_trace_log.get("handle")
        if handle is None:
            return
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        current_trace_log["handle"] = None
        current_trace_log["path"] = None

    print(
        f"[deploy] pending={len(sources)} resumed={len(existing_rows)} "
        f"shard_total={shard_source_count} selected_total={all_source_count} "
        f"shard={args.shard_index}/{args.num_shards} device={args.device} "
        f"K={cfg.K} epsilon={epsilon:g} amp={amp_dtype} "
        f"sdpa={checkpoint_meta['sdpa_backend']}",
        flush=True,
    )

    def pct(value):
        try:
            number = float(value)
            return f"{100.0 * number:.2f}%" if math.isfinite(number) else "-"
        except (TypeError, ValueError):
            return "-"

    def trace_start(index, total, trace):
        close_trace_log()
        core_dir = os.path.join(trace_log_dir, f"c{len(trace.core_ids):02d}")
        os.makedirs(core_dir, exist_ok=True)
        safe_workload = trace.workload.replace(os.sep, "_")
        log_path = os.path.join(core_dir, f"{safe_workload}.log")
        handle = open(log_path, "w", encoding="utf-8", buffering=1)
        current_trace_log["handle"] = handle
        current_trace_log["path"] = log_path
        sys.stdout = TeeStream(original_stdout, handle)
        sys.stderr = TeeStream(original_stderr, handle)
        print("\n" + "=" * 96, flush=True)
        print(
            f"## [{index}/{total}] {trace.workload} c{len(trace.core_ids):02d} "
            f"chunks={trace.total_chunks} K={trace.K} seed={trace.seed}",
            flush=True,
        )
        print(f"   packed_trace={trace.rollout_dir}", flush=True)
        print(
            f"   checkpoint={checkpoint_meta['checkpoint']} step={checkpoint_meta['step']} "
            f"device={args.device} amp={amp_dtype} sdpa={checkpoint_meta['sdpa_backend']} "
            f"epsilon={epsilon:g}",
            flush=True,
        )
        print(f"   independent_log={log_path}", flush=True)

    def step_progress(index, total, trace, row):
        window = int(row["step"]) + 1
        every = int(args.progress_every_steps)
        if every <= 0 or (window % every != 0 and row["committed_chunks"] < row["total_chunks"]):
            return
        elapsed = max(1e-9, float(row["wall_seconds"]))
        forwards = int(row.get("model_forwards", 0))
        forward_ms = (
            1000.0 * float(row.get("model_forward_seconds", 0.0)) / max(1, forwards)
        )
        progress_value = 100.0 * row["committed_chunks"] / max(1, row["total_chunks"])
        print(
            f"   [{trace.workload} c{len(trace.core_ids):02d}] window={window} "
            f"progress={progress_value:.1f}% chunks={row['committed_chunks']}/{row['total_chunks']} "
            f"uops/s={row['committed_uops']/elapsed:.0f} "
            f"running ROI-CPI pred={row.get('running_pred_roi_cpi', float('nan')):.4f} "
            f"label={row.get('running_true_roi_cpi', float('nan')):.4f} "
            f"err={pct(row.get('running_roi_cpi_error'))}",
            flush=True,
        )
        print(
            f"      active/new/commit/resident={row['active_cores']}/"
            f"{row['new_chunk_rows']}/{row['committed_this_step']}/{len(row['slow_cores'])} "
            f"forwards={forwards} avg_forward={forward_ms:.2f}ms "
            f"wall={elapsed:.1f}s",
            flush=True,
        )

    def progress(index, total, row):
        row["checkpoint_id"] = checkpoint_meta["checkpoint_id"]
        row["amp_dtype"] = amp_dtype
        row["sdpa_backend"] = checkpoint_meta["sdpa_backend"]
        row["max_resident_exposure"] = max_exposure
        row["force_sync_fast"] = not args.ignore_sync_force
        append_trace_jsonl(trace_jsonl, row)
        throughput = row["throughput"]
        branch_relative_error = row.get(
            "branch_miss_relative_error",
            row.get("branch_miss_rate_relative_error", float("nan")),
        )
        print(f"\n## {row['workload']} c{row['n_cores']:02d} complete", flush=True)
        print(
            f"  ROI CPI (all valid-label UOPs) pred/label = "
            f"{row['pred_roi_cpi']:.4f} / {row['true_roi_cpi']:.4f}; "
            f"relative error = {pct(row['roi_cpi_error'])}; "
            f"label coverage = {pct(row['roi_label_coverage'])}",
            flush=True,
        )
        print(
            f"  chunk CPI MAPE (per-core 256-UOP chunk) mean/p50/p90/p99 = "
            f"{pct(row['chunk_cpi_mape_mean'])} / {pct(row['chunk_cpi_mape_p50'])} / "
            f"{pct(row['chunk_cpi_mape_p90'])} / {pct(row['chunk_cpi_mape_p99'])}; "
            f"signed bias = {pct(row['chunk_cpi_signed_bias'])}",
            flush=True,
        )
        print(
            f"  scheduler-window CPI MAPE mean/p50/p90/p99 = "
            f"{pct(row['window_cpi_mape_mean'])} / {pct(row['window_cpi_mape_p50'])} / "
            f"{pct(row['window_cpi_mape_p90'])} / {pct(row['window_cpi_mape_p99'])}",
            flush=True,
        )
        print(
            f"  per-core ROI CPI error mean/p50/p90/p99 = "
            f"{pct(row['core_roi_cpi_mape_mean'])} / {pct(row['core_roi_cpi_mape_p50'])} / "
            f"{pct(row['core_roi_cpi_mape_p90'])} / {pct(row['core_roi_cpi_mape_p99'])}; "
            f"signed bias = {pct(row['core_roi_cpi_signed_bias'])}",
            flush=True,
        )
        print(
            f"  cycles pred/label = {row['pred_cycle_sum']:.1f} / {row['true_cycle_sum']:.1f}; "
            f"makespan pred/label/error = {row['pred_makespan']:.1f} / "
            f"{row['true_makespan']:.1f} / {pct(row['makespan_error'])}",
            flush=True,
        )
        print(
            f"  branch miss count pred/label = {row['pred_branch_misses']:.2f} / "
            f"{row['true_branch_misses']:.0f}; retired branches = "
            f"{row['retired_branches']}; rate pred/label = "
            f"{pct(row['pred_branch_miss_rate'])} / {pct(row['true_branch_miss_rate'])}; "
            f"relative error = {pct(branch_relative_error)}; abs delta = "
            f"{100.0*row['branch_miss_rate_abs_error']:.3f} percentage-points; "
            f"counted exactly once at commit",
            flush=True,
        )
        print(
            f"  scheduler windows/forwards/resident-events/max-exposure = "
            f"{row['n_steps']} / {row['n_model_forwards']} / "
            f"{row['n_resident_events']} / {row['max_exposure']}; "
            f"resident-row fraction = {pct(row['resident_row_fraction'])}",
            flush=True,
        )
        print(
            f"  throughput uops/s={throughput['committed_uops_per_second']:.0f} "
            f"chunks/s={throughput['committed_chunks_per_second']:.1f} "
            f"windows/s={throughput['scheduler_steps_per_second']:.1f} "
            f"context-rows/s={throughput['model_context_rows_per_second']:.1f}; "
            f"avg forward/step={throughput['avg_model_forward_ms']:.2f}/"
            f"{throughput['avg_scheduler_step_ms']:.2f}ms",
            flush=True,
        )
        print(
            f"  timing wall/model/non-model = {row['wall_seconds']:.2f}/"
            f"{row['model_forward_seconds']:.2f}/{row['non_model_seconds']:.2f}s; "
            f"static-cache hit/miss/rate={row['static_cache_hits']}/"
            f"{row['static_cache_misses']}/{pct(row['static_cache_hit_rate'])}; "
            f"GPU peak alloc/reserved={row.get('gpu_peak_allocated_gib', 0.0):.2f}/"
            f"{row.get('gpu_peak_reserved_gib', 0.0):.2f} GiB",
            flush=True,
        )
        if "fast_set_exact_rate" in row:
            print(
                f"  predicted-vs-oracle scheduler fast-set exact/Jaccard = "
                f"{pct(row['fast_set_exact_rate'])} / {pct(row['fast_set_jaccard_mean'])}; "
                f"first divergence window = {row['first_fast_set_divergence_step']}",
                flush=True,
            )
        print(
            f"[deploy {index}/{total}] persisted trace_result={trace_jsonl}",
            flush=True,
        )
        close_trace_log()

    try:
        traces = evaluate_packed_rollouts(
            sources,
            predictor,
            epsilon=epsilon,
            max_resident_exposure=max_exposure,
            max_steps=args.max_steps,
            max_chunks_per_core=args.max_chunks_per_core,
            oracle_schedule=not args.no_oracle_schedule,
            force_sync_fast=not args.ignore_sync_force,
            prefix_lens=prefix_lens,
            step_dump_dir=args.dump_steps_dir or None,
            trace_start=trace_start,
            step_progress=step_progress,
            progress=progress,
        )
    except BaseException:
        if current_trace_log.get("handle") is not None:
            traceback.print_exc(file=sys.stderr)
        raise
    finally:
        close_trace_log()
    traces = existing_rows + traces
    run_meta = {
        **checkpoint_meta,
        "manifest": os.path.abspath(args.manifest) if args.manifest else None,
        "rollout_root": os.path.abspath(args.rollout_root) if args.rollout_root else None,
        "split": ",".join(split_names),
        "splits": split_names,
        "amp_dtype": amp_dtype,
        "epsilon": epsilon,
        "max_resident_exposure": max_exposure,
        "max_chunks_per_core": args.max_chunks_per_core,
        "core_counts": sorted(selected_core_counts),
        "oracle_schedule": not args.no_oracle_schedule,
        "num_shards": args.num_shards,
        "shard_index": args.shard_index,
        "wall_seconds": time.time() - started,
        "resumed_traces": len(existing_rows),
        "trace_jsonl": os.path.abspath(trace_jsonl),
        "trace_log_dir": os.path.abspath(trace_log_dir),
        "progress_every_steps": args.progress_every_steps,
        "static_cache_entries": args.static_cache_entries,
        "model_input_source": "packed_functional_chunks_only",
        "oracle_rollout_context_consumed": False,
        "prediction_latch": "new_chunk_exact_once",
    }
    report = aggregate_trace_reports(traces, run=run_meta)
    write_report(args.out, report)
    agg = report["aggregate"]
    print(
        f"[deploy] complete traces={agg['n_traces']} chunks={agg['n_chunks']} "
        f"ROI-CPI pred/label={agg['pred_roi_cpi']:.4f}/{agg['true_roi_cpi']:.4f} "
        f"error={100*agg['global_roi_cpi_error']:.2f}% "
        f"branch-count pred/label={agg['pred_branch_misses']:.2f}/"
        f"{agg['true_branch_misses']:.0f} "
        f"branch-rate pred/label={100*agg['pred_branch_miss_rate']:.3f}%/"
        f"{100*agg['true_branch_miss_rate']:.3f}% "
        f"branch-error={100*agg['branch_miss_relative_error']:.2f}% "
        f"branch-abs={100*agg['branch_miss_rate_abs_error']:.3f}pp "
        f"report={args.out}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
