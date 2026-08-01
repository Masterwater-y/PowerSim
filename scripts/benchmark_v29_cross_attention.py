#!/usr/bin/env python3
"""Validate and benchmark legacy versus shared-K/V cross-core attention.

The CPU checks are always run and need no pytest.  When CUDA is available the
script additionally benchmarks the production c32/K=256/BF16 shape and reports
peak temporary allocation for each backend.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Callable, Dict, Sequence
import warnings

import torch

warnings.filterwarnings(
    "ignore",
    message=r"flex_attention called without torch\.compile",
)


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcsim.model.tcsim_model import FunctionalInteractionBlock  # noqa: E402


def compare_cpu_cases() -> Dict[str, Any]:
    cases = (
        ((1,), 7),
        ((2,), 7),
        ((3,), 11),
        ((2, 3), 7),
        ((1, 2, 4), 5),
    )
    worst_max = 0.0
    worst_mean = 0.0
    partial_fallback_exact = True
    for case_index, (sample_cores, length) in enumerate(cases):
        torch.manual_seed(1729 + case_index)
        rows = sum(sample_cores)
        dimension = 32
        block = FunctionalInteractionBlock(
            d_dyn=dimension,
            n_heads=4,
            dropout=0.0,
            ffn_dim=64,
            cross_attention_backend="flex_shared_kv",
        ).eval()
        r = torch.randn(rows, length, dimension)
        k = torch.randn_like(r)
        v = torch.randn_like(r)
        valid = torch.ones(rows, length, dtype=torch.bool)
        ptr_values = [0]
        for count in sample_cores:
            ptr_values.append(ptr_values[-1] + count)
        sample_ptr = torch.tensor(ptr_values, dtype=torch.long)
        with torch.inference_mode():
            expected = block._cross_attention_legacy(
                r, k, v, valid, sample_ptr,
            )
            actual = block._cross_attention_flex_shared_kv(
                r, k, v, sample_ptr,
            )
            difference = (expected - actual).abs()
            worst_max = max(worst_max, float(difference.max()))
            worst_mean = max(worst_mean, float(difference.mean()))

            partial = valid.clone()
            partial[-1, -2:] = False
            fallback = block._cross_attention(
                r, k, v, partial, sample_ptr, all_windows_valid=False,
            )
            fallback_expected = block._cross_attention_legacy(
                r, k, v, partial, sample_ptr,
            )
            partial_fallback_exact &= torch.equal(fallback, fallback_expected)
    if worst_max > 1.0e-5 or not partial_fallback_exact:
        raise RuntimeError(
            "shared-K/V CPU equivalence failed: "
            f"max={worst_max} partial_exact={partial_fallback_exact}"
        )
    return {
        "cases": len(cases),
        "max_abs_difference": worst_max,
        "max_mean_abs_difference": worst_mean,
        "partial_window_fallback_exact": partial_fallback_exact,
    }


def cuda_measure(
    function: Callable[[], torch.Tensor],
    *,
    warmup: int,
    blocks: int,
    iterations: int,
) -> Dict[str, float]:
    for _ in range(warmup):
        function()
    torch.cuda.synchronize()
    timings = []
    for _ in range(blocks):
        begin = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        begin.record()
        for _ in range(iterations):
            function()
        end.record()
        end.synchronize()
        timings.append(float(begin.elapsed_time(end)) / iterations)
    ordered = sorted(timings)
    return {
        "median_ms": float(statistics.median(timings)),
        "minimum_ms": float(min(timings)),
        "p95_ms": float(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]),
    }


def cuda_peak_bytes(function: Callable[[], torch.Tensor]) -> int:
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    baseline = int(torch.cuda.memory_allocated())
    output = function()
    torch.cuda.synchronize()
    peak = int(torch.cuda.max_memory_allocated())
    del output
    return peak - baseline


def benchmark_cuda(args: argparse.Namespace) -> Dict[str, Any] | None:
    if not torch.cuda.is_available():
        return None
    device = torch.device(args.device)
    dtype = torch.bfloat16
    torch.manual_seed(args.seed)
    block = FunctionalInteractionBlock(
        d_dyn=args.dimension,
        n_heads=args.heads,
        dropout=0.0,
        ffn_dim=args.dimension * 4,
        cross_attention_backend="flex_shared_kv",
    ).to(device).eval()
    r = torch.randn(
        args.cores, args.length, args.dimension, device=device, dtype=dtype,
    )
    k = torch.randn_like(r)
    v = torch.randn_like(r)
    valid = torch.ones(args.cores, args.length, dtype=torch.bool, device=device)
    sample_ptr = torch.tensor([0, args.cores], dtype=torch.long)

    def legacy() -> torch.Tensor:
        return block._cross_attention_legacy(r, k, v, valid, sample_ptr)

    def shared() -> torch.Tensor:
        return block._cross_attention_flex_shared_kv(r, k, v, sample_ptr)

    with torch.inference_mode():
        expected = legacy()
        actual = shared()
        torch.cuda.synchronize()
        difference = (expected.float() - actual.float()).abs()
        correctness = {
            "max_abs_difference": float(difference.max()),
            "mean_abs_difference": float(difference.mean()),
        }
        legacy_peak = cuda_peak_bytes(legacy)
        shared_peak = cuda_peak_bytes(shared)
        legacy_timing = cuda_measure(
            legacy,
            warmup=args.warmup,
            blocks=args.blocks,
            iterations=args.iterations,
        )
        shared_timing = cuda_measure(
            shared,
            warmup=args.warmup,
            blocks=args.blocks,
            iterations=args.iterations,
        )
    legacy_median = legacy_timing["median_ms"]
    shared_median = shared_timing["median_ms"]
    replicated_elements = (
        args.cores * (args.cores - 1) * args.length * args.dimension
    )
    return {
        "device": str(device),
        "device_name": torch.cuda.get_device_name(device),
        "dtype": str(dtype),
        "shape": {
            "cores": args.cores,
            "length": args.length,
            "dimension": args.dimension,
            "heads": args.heads,
        },
        "legacy": {**legacy_timing, "peak_temporary_bytes": legacy_peak},
        "flex_shared_kv": {
            **shared_timing,
            "peak_temporary_bytes": shared_peak,
        },
        "latency_speedup": legacy_median / shared_median,
        "latency_reduction": 1.0 - shared_median / legacy_median,
        "peak_temporary_reduction_bytes": legacy_peak - shared_peak,
        "theoretical_legacy_replicated_kv_bytes": replicated_elements * 2 * 2,
        "correctness": correctness,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--cores", type=int, default=32)
    parser.add_argument("--length", type=int, default=256)
    parser.add_argument("--dimension", type=int, default=960)
    parser.add_argument("--heads", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--blocks", type=int, default=7)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()
    if args.cores <= 0 or args.length <= 0 or args.dimension <= 0:
        raise SystemExit("cores, length, and dimension must be positive")
    if args.dimension % args.heads:
        raise SystemExit("dimension must be divisible by heads")
    report: Dict[str, Any] = {
        "schema": "tcsim-v29-cross-attention-benchmark-1",
        "cpu_equivalence": compare_cpu_cases(),
        "cuda_benchmark": benchmark_cuda(args),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
