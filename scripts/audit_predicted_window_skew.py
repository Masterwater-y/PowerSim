#!/usr/bin/env python3
"""Measure true-time skew inside free-running predicted scheduler windows.

This is a deployment diagnostic only.  It never consumes ``rollout.jsonl``
and never constructs training samples from predicted contexts.  True chunk
intervals are reconstructed from the packed per-core additive labels, then
looked up by the chunk IDs selected by the predicted scheduler.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any, Dict, List

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.inference.deployment import (  # noqa: E402
    DeploymentRunner,
    ModelContextPredictor,
    PackedTrace,
    load_checkpoint_model,
    load_manifest_rollouts,
)


METRICS = (
    "true_nonoverlap_gap",
    "true_start_spread",
    "true_end_spread",
    "true_midpoint_spread",
    "true_chunk_index_spread",
    "pred_true_clock_error_spread",
)


def _stats(values: List[float]) -> Dict[str, float]:
    array = np.asarray(values, dtype=np.float64)
    if not array.size:
        return {
            "count": 0,
            "mean": float("nan"),
            "p50": float("nan"),
            "p90": float("nan"),
            "p99": float("nan"),
            "max": float("nan"),
        }
    return {
        "count": int(array.size),
        "mean": float(array.mean()),
        "p50": float(np.quantile(array, 0.50)),
        "p90": float(np.quantile(array, 0.90)),
        "p99": float(np.quantile(array, 0.99)),
        "max": float(array.max()),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


class TrueTimeline:
    def __init__(self, trace: PackedTrace) -> None:
        self.start: Dict[int, np.ndarray] = {}
        self.end: Dict[int, np.ndarray] = {}
        self.valid_count: Dict[int, int] = {}
        self.n_invalid_labels = 0
        scalar = trace.arrays["scalar"]
        for core in trace.core_ids:
            offset = int(trace.core_offsets[core])
            count = int(trace.core_counts[core])
            rows = scalar[offset:offset + count]
            valid = (rows[:, 11] > 0.5) & (rows[:, 9] > 0.0)
            self.n_invalid_labels += int((~valid).sum())
            invalid_index = np.flatnonzero(~valid)
            # A missing additive delta makes this core's true cumulative clock
            # unknowable from that chunk onward.  Keep only the exact prefix;
            # do not interpolate or manufacture a label for diagnostic use.
            self.valid_count[core] = (
                int(invalid_index[0]) if invalid_index.size else count
            )
            delta = np.array(rows[:, 9], dtype=np.float64, copy=True)
            delta[~valid] = 0.0
            end = np.cumsum(delta)
            self.start[core] = np.concatenate((np.zeros(1), end[:-1]))
            self.end[core] = end


def _csv_ints(value: str) -> set[int]:
    return {int(part.strip()) for part in value.split(",") if part.strip()}


def _csv_strings(value: str) -> set[str]:
    return {part.strip() for part in value.split(",") if part.strip()}


def _source_rows(
    path: str,
    split: str,
    shard_count: int,
    shard_index: int,
    core_counts: set[int],
    workloads: set[str],
) -> List[dict]:
    rows = load_manifest_rollouts(path, split)
    unique: Dict[str, dict] = {}
    for row in rows:
        unique.setdefault(os.path.abspath(str(row["rollout_dir"])), row)
    ordered = sorted(
        (
            row for row in unique.values()
            if (not core_counts or int(row.get("n_cores", 0)) in core_counts)
            and (not workloads or str(row.get("workload", "")) in workloads)
        ),
        key=lambda row: (
            int(row.get("n_cores", 0)),
            str(row.get("workload", "")),
            str(row["rollout_dir"]),
        ),
    )
    return [row for index, row in enumerate(ordered) if index % shard_count == shard_index]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--split", default="deployment_inference")
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--amp-dtype", default="bf16")
    parser.add_argument("--sdpa-backend", default="auto")
    parser.add_argument("--epsilon", type=float, default=None)
    parser.add_argument("--static-cache-entries", type=int, default=256)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument("--core-counts", default="")
    parser.add_argument("--workloads", default="")
    parser.add_argument("--progress-every", type=int, default=1)
    args = parser.parse_args()

    if args.num_shards < 1 or not 0 <= args.shard_index < args.num_shards:
        raise SystemExit("require num_shards>=1 and 0<=shard_index<num_shards")
    sources = _source_rows(
        args.manifest,
        args.split,
        args.num_shards,
        args.shard_index,
        _csv_ints(args.core_counts),
        _csv_strings(args.workloads),
    )
    if args.max_traces > 0:
        sources = sources[:args.max_traces]
    if not sources:
        raise SystemExit("selected shard has no traces")

    model, cfg, checkpoint = load_checkpoint_model(
        args.ckpt,
        device=args.device,
        sdpa_backend=args.sdpa_backend,
    )
    epsilon = float(cfg.epsilon if args.epsilon is None else args.epsilon)
    predictor = ModelContextPredictor(
        model,
        device=args.device,
        checkpoint_id=checkpoint["checkpoint_id"],
        predictor_hash=checkpoint["predictor_hash"],
        amp_dtype=args.amp_dtype,
        static_cache_entries=args.static_cache_entries,
    )
    runner = DeploymentRunner(
        epsilon=epsilon,
        max_resident_exposure=int(cfg.scheduler.get("max_resident_exposure", 0)),
        force_sync_fast=True,
    )

    all_values: Dict[str, List[float]] = {name: [] for name in METRICS}
    all_full_values: Dict[str, List[float]] = {name: [] for name in METRICS}
    trace_reports: List[dict] = []
    started_all = time.perf_counter()
    for trace_index, source in enumerate(sources, 1):
        trace = PackedTrace(str(source["rollout_dir"]), source=source)
        timeline = TrueTimeline(trace)
        values: Dict[str, List[float]] = {name: [] for name in METRICS}
        full_values: Dict[str, List[float]] = {name: [] for name in METRICS}
        worst: Dict[str, Any] = {}
        skipped_invalid_timeline_windows = 0

        def sink(row: Dict[str, Any]) -> None:
            nonlocal skipped_invalid_timeline_windows
            records = row["core_records"]
            if len(records) < 2:
                return
            cores = np.asarray([int(record["core_id"]) for record in records])
            chunks = np.asarray([int(record["chunk_id"]) for record in records])
            if any(
                int(chunk) >= timeline.valid_count[int(core)]
                for core, chunk in zip(cores, chunks)
            ):
                skipped_invalid_timeline_windows += 1
                return
            true_start = np.asarray([
                timeline.start[int(core)][int(chunk)]
                for core, chunk in zip(cores, chunks)
            ])
            true_end = np.asarray([
                timeline.end[int(core)][int(chunk)]
                for core, chunk in zip(cores, chunks)
            ])
            pred_end = np.asarray([float(record["E_pred"]) for record in records])
            midpoint = 0.5 * (true_start + true_end)
            metric = {
                "true_nonoverlap_gap": max(
                    0.0, float(true_start.max() - true_end.min())
                ),
                "true_start_spread": float(np.ptp(true_start)),
                "true_end_spread": float(np.ptp(true_end)),
                "true_midpoint_spread": float(np.ptp(midpoint)),
                "true_chunk_index_spread": float(np.ptp(chunks)),
                "pred_true_clock_error_spread": float(np.ptp(pred_end - true_end)),
            }
            is_full = len(records) == len(trace.core_ids)
            for name, value in metric.items():
                values[name].append(value)
                all_values[name].append(value)
                if is_full:
                    full_values[name].append(value)
                    all_full_values[name].append(value)
            gap = metric["true_nonoverlap_gap"]
            if not worst or gap > float(worst["true_nonoverlap_gap"]):
                worst.update({
                    **metric,
                    "step": int(row["step"]),
                    "active_cores": len(records),
                    "min_chunk_id": int(chunks.min()),
                    "max_chunk_id": int(chunks.max()),
                })

        started = time.perf_counter()
        run = runner.run(trace, predictor, step_sink=sink)
        report = {
            "trace_id": trace.trace_id,
            "workload": trace.workload,
            "seed": trace.seed,
            "n_cores": len(trace.core_ids),
            "n_chunks": trace.total_chunks,
            "n_steps": run.summary["n_steps"],
            "n_invalid_cycle_labels": timeline.n_invalid_labels,
            "n_skipped_invalid_timeline_windows": skipped_invalid_timeline_windows,
            "roi_cpi_error": run.summary["roi_cpi_error"],
            "fast_set_metrics_available": False,
            "all_multicore_windows": {name: _stats(value) for name, value in values.items()},
            "full_core_windows": {name: _stats(value) for name, value in full_values.items()},
            "worst_window": worst,
            "wall_seconds": time.perf_counter() - started,
        }
        trace_reports.append(report)
        if args.progress_every > 0 and (
            trace_index % args.progress_every == 0 or trace_index == len(sources)
        ):
            gap = report["all_multicore_windows"]["true_nonoverlap_gap"]
            print(
                f"[skew {trace_index}/{len(sources)}] {trace.workload} "
                f"c{len(trace.core_ids)} gap_p99/max={gap['p99']:.0f}/{gap['max']:.0f} "
                f"wall={report['wall_seconds']:.1f}s",
                flush=True,
            )

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    report = {
        "schema_version": "tcsim-predicted-window-true-time-skew-1",
        "contract": {
            "training_data_from_predicted_windows": False,
            "predicted_windows_used_for_deployment_diagnostics_only": True,
            "true_interval_origin": "packed additive per-core cycle labels",
            "true_nonoverlap_gap": "max(0,max(true_start)-min(true_end))",
        },
        "run": {
            "checkpoint": checkpoint["checkpoint"],
            "checkpoint_step": checkpoint["step"],
            "manifest": os.path.abspath(args.manifest),
            "split": args.split,
            "epsilon": epsilon,
            "num_shards": args.num_shards,
            "shard_index": args.shard_index,
            "wall_seconds": time.perf_counter() - started_all,
        },
        "all_multicore_windows": {
            name: _stats(value) for name, value in all_values.items()
        },
        "full_core_windows": {
            name: _stats(value) for name, value in all_full_values.items()
        },
        "traces": trace_reports,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(_json_safe(report), handle, indent=2, sort_keys=True)
        handle.write("\n")
    np.savez_compressed(
        args.out + ".windows.npz",
        **{name: np.asarray(value) for name, value in all_values.items()},
        **{
            "full_" + name: np.asarray(value)
            for name, value in all_full_values.items()
        },
    )
    print(f"[skew] wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
