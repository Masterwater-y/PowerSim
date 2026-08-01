#!/usr/bin/env python3
"""Validate and benchmark hierarchical latent cross-core attention."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
from typing import Any, Callable, Dict

import torch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tcsim.model.tcsim_model import FunctionalInteractionBlock  # noqa: E402


def validate_cpu(latent_count: int) -> Dict[str, Any]:
    torch.manual_seed(1729)
    block = FunctionalInteractionBlock(
        d_dyn=64,
        n_heads=4,
        dropout=0.0,
        ffn_dim=128,
        cross_attention_backend="hierarchical_latent",
        cross_latent_count=latent_count,
        cross_latent_stabilization=True,
    )
    rows, length = 6, 17
    x = torch.randn(rows, length, 64, requires_grad=True)
    valid = torch.ones(rows, length, dtype=torch.bool)
    valid[-1, 11:] = False
    sample_ptr = torch.tensor([0, 3, 6], dtype=torch.long)
    gate = torch.rand(rows, 64)
    output = block(x, valid, sample_ptr, gate)
    loss = output.square().mean()
    loss.backward()
    gradient = block.cross_latent_queries.grad
    if not torch.isfinite(output).all() or gradient is None:
        raise RuntimeError("latent CPU forward/backward validation failed")
    if not torch.isfinite(gradient).all() or float(gradient.abs().sum()) == 0.0:
        raise RuntimeError("latent queries did not receive a finite gradient")
    if output[~valid].abs().count_nonzero():
        raise RuntimeError("latent attention did not zero invalid output tokens")
    latent_gradients = {
        name: parameter.grad
        for name, parameter in block.named_parameters()
        if "cross_latent_" in name
    }
    missing_or_bad = [
        name for name, value in latent_gradients.items()
        if value is None or not bool(torch.isfinite(value).all())
    ]
    if missing_or_bad:
        raise RuntimeError(
            f"latent stabilizers lack finite gradients: {missing_or_bad}"
        )
    return {
        "output_shape": list(output.shape),
        "partial_window_supported": True,
        "output_finite": True,
        "latent_gradient_l1": float(gradient.abs().sum()),
        "latent_parameter_count": int(block.cross_latent_queries.numel()),
        "all_latent_parameter_gradients_finite": True,
        "extra_latent_residual_gate": False,
        "latent_attention_training_dtype": "float32",
    }


def measure_cuda(
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


def peak_cuda_bytes(function: Callable[[], torch.Tensor]) -> int:
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
        cross_attention_backend="hierarchical_latent",
        cross_latent_count=args.latents,
        cross_latent_stabilization=True,
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

    def latent() -> torch.Tensor:
        return block._cross_attention_hierarchical_latent(
            r, k, v, valid, sample_ptr,
        )

    with torch.inference_mode():
        latent_output = latent()
        if not torch.isfinite(latent_output).all():
            raise RuntimeError("latent CUDA output is not finite")
        legacy_peak = peak_cuda_bytes(legacy)
        latent_peak = peak_cuda_bytes(latent)
        legacy_timing = measure_cuda(
            legacy,
            warmup=args.warmup,
            blocks=args.blocks,
            iterations=args.iterations,
        )
        latent_timing = measure_cuda(
            latent,
            warmup=args.warmup,
            blocks=args.blocks,
            iterations=args.iterations,
        )
    legacy_scores = args.cores * args.length * (args.cores - 1) * args.length
    latent_scores = (
        args.cores * args.latents * args.length
        + args.cores * args.latents * (args.cores - 1) * args.latents
        + args.cores * args.length * args.latents
    )
    return {
        "device": str(device),
        "device_name": torch.cuda.get_device_name(device),
        "dtype": str(dtype),
        "shape": {
            "cores": args.cores,
            "length": args.length,
            "latents_per_core": args.latents,
            "dimension": args.dimension,
            "heads": args.heads,
        },
        "legacy": {**legacy_timing, "peak_temporary_bytes": legacy_peak},
        "hierarchical_latent": {
            **latent_timing,
            "peak_temporary_bytes": latent_peak,
        },
        "latency_speedup": (
            legacy_timing["median_ms"] / latent_timing["median_ms"]
        ),
        "latency_reduction": (
            1.0 - latent_timing["median_ms"] / legacy_timing["median_ms"]
        ),
        "theoretical_attention_score_reduction": 1.0 - latent_scores / legacy_scores,
        "theoretical_score_elements": {
            "legacy": legacy_scores,
            "hierarchical_latent": latent_scores,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--cores", type=int, default=32)
    parser.add_argument("--length", type=int, default=256)
    parser.add_argument("--latents", type=int, default=32)
    parser.add_argument("--dimension", type=int, default=960)
    parser.add_argument("--heads", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--blocks", type=int, default=7)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()
    if min(args.cores, args.length, args.latents, args.dimension, args.heads) <= 0:
        raise SystemExit("all shape arguments must be positive")
    if args.dimension % args.heads:
        raise SystemExit("dimension must be divisible by heads")
    report: Dict[str, Any] = {
        "schema": "tcsim-v29-hierarchical-latent-benchmark-1",
        "cpu_validation": validate_cpu(args.latents),
        "cuda_benchmark": benchmark_cuda(args),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
