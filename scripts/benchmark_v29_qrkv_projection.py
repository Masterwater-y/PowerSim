#!/usr/bin/env python3
"""Correctness and latency benchmark for eval-only fused Q/R/K/V projection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Callable, Dict, Tuple

import torch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcsim.model.tcsim_model import (
    FunctionalInteractionBlock,
    QRKV_PROJECTION_BACKEND_FUSED,
    QRKV_PROJECTION_BACKEND_SEPARATE,
)


Projection = Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]


def _flatten(values: Projection) -> torch.Tensor:
    return torch.cat(values, dim=-1)


def _make_block(dimension: int, heads: int) -> FunctionalInteractionBlock:
    return FunctionalInteractionBlock(
        dimension,
        n_heads=heads,
        dropout=0.0,
        ffn_dim=dimension * 4,
        qrkv_projection_backend=QRKV_PROJECTION_BACKEND_SEPARATE,
    ).eval()


def _separate(block: FunctionalInteractionBlock, h: torch.Tensor) -> Projection:
    return (
        block.q_proj(h),
        block.r_proj(h),
        block.k_proj(h),
        block.v_proj(h),
    )


@torch.inference_mode()
def cpu_equivalence() -> Dict[str, Any]:
    torch.manual_seed(29)
    block = _make_block(32, 4)
    h = torch.randn(3, 7, 32)
    state_keys_before = tuple(block.state_dict().keys())
    parameter_count_before = sum(p.numel() for p in block.parameters())
    expected = _flatten(_separate(block, h))
    block.qrkv_projection_backend = QRKV_PROJECTION_BACKEND_FUSED
    actual = _flatten(block._project_qrkv(h))
    state_keys_after = tuple(block.state_dict().keys())
    parameter_count_after = sum(p.numel() for p in block.parameters())
    difference = (actual - expected).abs()
    return {
        "max_abs_difference": float(difference.max()),
        "mean_abs_difference": float(difference.mean()),
        "state_dict_unchanged": state_keys_before == state_keys_after,
        "parameter_count_unchanged": parameter_count_before == parameter_count_after,
        "fused_calls": int(block.fused_qrkv_projection_calls),
    }


def _measure_cuda(
    function: Callable[[], Projection],
    *,
    warmup: int,
    repeats: int,
    iterations: int,
    device: torch.device,
) -> Dict[str, Any]:
    for _ in range(warmup):
        function()
    torch.cuda.synchronize(device)
    torch.cuda.empty_cache()
    baseline = int(torch.cuda.memory_allocated(device))
    torch.cuda.reset_peak_memory_stats(device)
    samples = []
    for _ in range(repeats):
        begin = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        begin.record()
        for _ in range(iterations):
            function()
        end.record()
        torch.cuda.synchronize(device)
        samples.append(float(begin.elapsed_time(end)) / iterations)
    peak = int(torch.cuda.max_memory_allocated(device))
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, int(0.95 * len(ordered)))
    return {
        "median_ms": statistics.median(samples),
        "minimum_ms": min(samples),
        "p95_ms": ordered[p95_index],
        "peak_temporary_bytes": max(0, peak - baseline),
    }


@torch.inference_mode()
def cuda_benchmark(args: argparse.Namespace) -> Dict[str, Any]:
    device = torch.device(args.device)
    torch.manual_seed(29)
    torch.cuda.manual_seed_all(29)
    torch.set_float32_matmul_precision("high")
    block = _make_block(args.dimension, args.heads).to(device)
    h = torch.randn(
        args.cores,
        args.length,
        args.dimension,
        device=device,
        dtype=torch.bfloat16,
    )
    autocast = lambda: torch.autocast(
        device_type="cuda", dtype=torch.bfloat16, enabled=True,
    )

    def run_separate() -> Projection:
        with autocast():
            return _separate(block, h)

    def run_fused() -> Projection:
        block.qrkv_projection_backend = QRKV_PROJECTION_BACKEND_FUSED
        with autocast():
            return block._project_qrkv(h)

    # Materialize the packed eval weight before both correctness and timing.
    run_fused()
    torch.cuda.synchronize(device)
    expected = _flatten(run_separate())
    actual = _flatten(run_fused())
    difference = (actual.float() - expected.float()).abs()
    correctness = {
        "max_abs_difference": float(difference.max()),
        "mean_abs_difference": float(difference.mean()),
    }
    del expected, actual, difference
    torch.cuda.synchronize(device)

    separate_report = _measure_cuda(
        run_separate,
        warmup=args.warmup,
        repeats=args.repeats,
        iterations=args.iterations,
        device=device,
    )
    fused_report = _measure_cuda(
        run_fused,
        warmup=args.warmup,
        repeats=args.repeats,
        iterations=args.iterations,
        device=device,
    )
    separate_median = float(separate_report["median_ms"])
    fused_median = float(fused_report["median_ms"])
    return {
        "device": str(device),
        "device_name": torch.cuda.get_device_name(device),
        "dtype": str(h.dtype),
        "shape": {
            "cores": args.cores,
            "length": args.length,
            "dimension": args.dimension,
            "heads": args.heads,
        },
        "correctness": correctness,
        "separate": separate_report,
        "fused": fused_report,
        "latency_speedup": separate_median / fused_median,
        "latency_reduction": 1.0 - fused_median / separate_median,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--cores", type=int, default=32)
    parser.add_argument("--length", type=int, default=256)
    parser.add_argument("--dimension", type=int, default=960)
    parser.add_argument("--heads", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=25)
    parser.add_argument("--iterations", type=int, default=20)
    args = parser.parse_args()
    report: Dict[str, Any] = {
        "schema": "tcsim-v29-qrkv-projection-benchmark-1",
        "cpu_equivalence": cpu_equivalence(),
    }
    if torch.cuda.is_available():
        report["cuda_benchmark"] = cuda_benchmark(args)
    else:
        report["cuda_benchmark"] = {"skipped": "CUDA is unavailable"}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
