#!/usr/bin/env python3
"""Compare two single-trace v29 rollout reports."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Mapping


def _load(path: str) -> Mapping[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    traces = report.get("traces", [])
    if len(traces) != 1 or not isinstance(traces[0].get("free_running"), Mapping):
        raise RuntimeError(f"expected one free-running trace in {path}")
    return traces[0]["free_running"]


def _summary(row: Mapping[str, Any]) -> Dict[str, Any]:
    forwards = max(1, int(row.get("model_forwards", 0)))
    steps = max(1, int(row.get("steps", 0)))
    timing = row.get("timing_breakdown", {})
    return {
        "steps": int(row.get("steps", 0)),
        "model_forwards": int(row.get("model_forwards", 0)),
        "retired_uops": int(row.get("retired_uops", 0)),
        "uops_per_s": float(row.get("uops_per_s", 0.0)),
        "wall_ms_per_step": 1000.0 * float(row.get("elapsed_s", 0.0)) / steps,
        "model_ms_per_forward": (
            1000.0 * float(row.get("model_forward_seconds", 0.0)) / forwards
        ),
        "context_ms_per_forward": (
            1000.0 * float(timing.get("context_build_seconds", 0.0)) / forwards
        ),
        "predict_ms_per_forward": (
            1000.0 * float(timing.get("predict_seconds", 0.0)) / forwards
        ),
        "gpu_peak_memory_bytes": int(row.get("gpu_peak_memory_bytes", 0)),
        "legacy_cross_attention_layer_calls": int(
            row.get("legacy_cross_attention_layer_calls", 0)
        ),
        "shared_kv_cross_attention_layer_calls": int(
            row.get("shared_kv_cross_attention_layer_calls", 0)
        ),
        "separate_qrkv_projection_layer_calls": int(
            row.get("separate_qrkv_projection_layer_calls", 0)
        ),
        "fused_qrkv_projection_layer_calls": int(
            row.get("fused_qrkv_projection_layer_calls", 0)
        ),
        "gss_pmu_only": bool(row.get("gss_pmu_only", False)),
        "gss_hot_path_backend": row.get("gss_hot_path_backend"),
        "gss_preview_calls": int(row.get("gss_preview_calls", 0)),
        "gss_commit_calls": int(row.get("gss_commit_calls", 0)),
        "gss_commit_ms_per_step": (
            1000.0 * float(row.get("gss_commit_seconds", 0.0)) / steps
        ),
        "gss_canonical_state": row.get("gss_canonical_state"),
        "cache_miss_pmu_error": row.get("cache_miss_pmu_error"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("optimized")
    args = parser.parse_args()
    baseline = _summary(_load(args.baseline))
    optimized = _summary(_load(args.optimized))
    base_uops = float(baseline["uops_per_s"])
    opt_uops = float(optimized["uops_per_s"])
    base_model = float(baseline["model_ms_per_forward"])
    opt_model = float(optimized["model_ms_per_forward"])
    result = {
        "schema": "tcsim-v29-forward-rollout-comparison-1",
        "baseline": baseline,
        "optimized": optimized,
        "delta": {
            "uops_per_s_gain": opt_uops - base_uops,
            "uops_per_s_speedup": opt_uops / max(base_uops, 1.0e-12),
            "model_ms_reduction": base_model - opt_model,
            "model_latency_speedup": base_model / max(opt_model, 1.0e-12),
            "gpu_peak_reduction_bytes": (
                int(baseline["gpu_peak_memory_bytes"])
                - int(optimized["gpu_peak_memory_bytes"])
            ),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
