#!/usr/bin/env python3
"""Verify and benchmark the v29 fused native context backend.

This is an integration check, not a pytest suite.  It compares every tensor
produced from real cache cursors against the NumPy reference and then measures
free-running context construction with labels disabled.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Iterable, Mapping

import numpy as np
import torch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcsim.v29.dataset import (  # noqa: E402
    CONTEXT_PHASE_NAMES,
    NUMPY_CONTEXT_BUILDER,
    V29TraceStore,
)


def sample_indices(length: int, count: int, *, offset: int = 0) -> np.ndarray:
    if length <= 0 or count <= 0:
        raise ValueError("sample length and count must be positive")
    count = min(length, count)
    indices = np.linspace(0, length - 1, count, dtype=np.int64)
    if offset:
        indices = (indices + int(offset)) % length
    return np.unique(indices)


def reference_store(cache: str) -> V29TraceStore:
    store = V29TraceStore(cache)
    store._native_context = None
    store.context_builder = NUMPY_CONTEXT_BUILDER
    return store


def verify_exact(
    native: V29TraceStore,
    reference: V29TraceStore,
    indices: Iterable[int],
) -> Dict[str, float]:
    worst: Dict[str, float] = {}
    for index in indices:
        actual = native.context_at(int(index))
        expected = reference.context_at(int(index))
        if actual.keys() != expected.keys():
            raise RuntimeError(
                f"context key mismatch at sample {index}: "
                f"{actual.keys()} != {expected.keys()}"
            )
        for name, actual_value in actual.items():
            expected_value = expected[name]
            if isinstance(actual_value, torch.Tensor):
                if actual_value.dtype.is_floating_point:
                    difference = float(
                        (actual_value - expected_value).abs().max()
                    ) if actual_value.numel() else 0.0
                else:
                    difference = float(
                        (actual_value != expected_value).sum()
                    )
            else:
                difference = 0.0 if actual_value == expected_value else 1.0
            worst[name] = max(worst.get(name, 0.0), difference)
            if difference != 0.0:
                raise RuntimeError(
                    f"context mismatch sample={index} field={name} "
                    f"difference={difference}"
                )
    return worst


def benchmark(
    store: V29TraceStore,
    warmup_indices: Iterable[int],
    measured_indices: Iterable[int],
) -> Dict[str, Any]:
    last_commit = {core_id: 0.0 for core_id in store.core_ids}

    def run(index: int) -> np.ndarray:
        tick = int(store.sample_ticks[index])
        cycles = (tick - store.roi_origin_tick) / store.tpc
        phase_before = dict(store.context_phase_seconds)
        started = time.perf_counter()
        store.context_from_cursors(
            store.sample_cursors[index],
            state_time_cycles=cycles,
            include_labels=False,
            last_commit_cycles=last_commit,
        )
        total = time.perf_counter() - started
        phases = [
            store.context_phase_seconds[name] - phase_before[name]
            for name in CONTEXT_PHASE_NAMES
        ]
        return np.asarray([total, *phases], dtype=np.float64)

    for index in warmup_indices:
        run(int(index))
    store.reset_runtime_stats()
    rows = np.stack([run(int(index)) for index in measured_indices]) * 1000.0
    names = ("total", *CONTEXT_PHASE_NAMES)
    metrics: Dict[str, Mapping[str, float]] = {}
    for column, name in enumerate(names):
        values = rows[:, column]
        metrics[name] = {
            "median_ms": float(np.median(values)),
            "mean_ms": float(np.mean(values)),
            "p95_ms": float(np.percentile(values, 95)),
        }
    return {
        "builder": store.context_builder,
        "samples": int(len(rows)),
        "metrics": metrics,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", required=True)
    parser.add_argument("--verify-samples", type=int, default=24)
    parser.add_argument("--benchmark-samples", type=int, default=96)
    parser.add_argument("--warmup", type=int, default=5)
    args = parser.parse_args()

    cache = os.path.abspath(args.cache)
    native = V29TraceStore(cache)
    if native._native_context is None:
        raise SystemExit(
            "native context backend is unavailable for this cache; build it "
            "with scripts/build_v29_context_native.py and ensure the cache "
            "contains v29 performance sidecars"
        )
    reference = reference_store(cache)
    verify_indices = sample_indices(len(native.sample_ticks), args.verify_samples)
    worst = verify_exact(native, reference, verify_indices)

    warmup_indices = sample_indices(
        len(native.sample_ticks), args.warmup, offset=17,
    )
    measured_indices = sample_indices(
        len(native.sample_ticks), args.benchmark_samples, offset=97,
    )
    native.reset_runtime_stats()
    reference.reset_runtime_stats()
    native_benchmark = benchmark(native, warmup_indices, measured_indices)
    reference_benchmark = benchmark(reference, warmup_indices, measured_indices)
    native_total = native_benchmark["metrics"]["total"]["median_ms"]
    reference_total = reference_benchmark["metrics"]["total"]["median_ms"]
    report = {
        "cache": cache,
        "exact_samples": int(len(verify_indices)),
        "tensor_max_difference": worst,
        "native": native_benchmark,
        "numpy_reference": reference_benchmark,
        "median_speedup": float(reference_total / native_total),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
