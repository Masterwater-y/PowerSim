#!/usr/bin/env python3
"""Validate and benchmark target-query-preserving remote-K/V compression."""
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
from tcsim.v29.contracts import (  # noqa: E402
    CHUNK_SUMMARY_NAMES,
    DYNAMIC_FIELD_SIZES,
    RELATION_FEATURE_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)
from tcsim.v29.model import FunctionalInteractionV29  # noqa: E402


def _block(dimension: int, heads: int, anchors: int, positional: int):
    return FunctionalInteractionBlock(
        d_dyn=dimension,
        n_heads=heads,
        dropout=0.0,
        ffn_dim=dimension * 2,
        cross_attention_backend="query_preserving_kv",
        cross_anchor_count=anchors,
        cross_anchor_positional_count=positional,
        cross_anchor_stabilization=True,
        cross_anchor_fp32_training=True,
    )


def validate_cpu(anchors: int, positional: int) -> Dict[str, Any]:
    torch.manual_seed(20260802)
    block = _block(64, 4, anchors, positional).train()
    rows, length = 6, 19
    r = torch.randn(rows, length, 64, requires_grad=True)
    k = torch.randn_like(r, requires_grad=True)
    v = torch.randn_like(r, requires_grad=True)
    valid = torch.ones(rows, length, dtype=torch.bool)
    valid[-1, 13:] = False
    sample_ptr = torch.tensor([0, 3, 6], dtype=torch.long)
    output = block._cross_attention_query_preserving_kv(
        r, k, v, valid, sample_ptr,
    )
    if not bool(torch.isfinite(output).all()):
        raise RuntimeError("query-preserving K/V CPU output is non-finite")
    output.square().mean().backward()
    anchor_gradients = {
        name: parameter.grad
        for name, parameter in block.named_parameters()
        if "cross_anchor_" in name
    }
    bad_gradients = [
        name for name, gradient in anchor_gradients.items()
        if gradient is None or not bool(torch.isfinite(gradient).all())
    ]
    if bad_gradients:
        raise RuntimeError(f"bad anchor gradients: {bad_gradients}")

    with torch.no_grad():
        baseline = block._cross_attention_query_preserving_kv(
            r, k, v, valid, sample_ptr,
        )
        modified_r = r.detach().clone()
        modified_r[0, 0, 0] += 0.5
        modified = block._cross_attention_query_preserving_kv(
            modified_r, k, v, valid, sample_ptr,
        )
        difference = (modified - baseline).abs()
        changed_target = float(difference[0, 0].max())
        difference[0, 0] = 0
        other_target_max = float(difference.max())
    if changed_target <= 0.0 or other_target_max != 0.0:
        raise RuntimeError(
            "target-query independence failed: "
            f"changed={changed_target} other={other_target_max}"
        )
    return {
        "output_shape": list(output.shape),
        "output_finite": True,
        "partial_window_supported": True,
        "anchors_per_core": anchors,
        "positional_anchors": positional,
        "content_anchors": anchors - positional,
        "all_anchor_gradients_finite": True,
        "target_query_change_max": changed_target,
        "other_target_change_max": other_target_max,
        "anchor_parameter_count": sum(
            parameter.numel() for name, parameter in block.named_parameters()
            if "cross_anchor_" in name
        ),
    }


def validate_interaction_cpu(anchors: int, positional: int) -> Dict[str, Any]:
    """Exercise local-only/cross layer selection through the v29 interaction."""
    torch.manual_seed(20260803)
    interaction = FunctionalInteractionV29(
        d_static=48,
        d_dyn=64,
        d_dynamic_field=8,
        n_heads=4,
        n_layers=4,
        ffn_dim=128,
        dropout=0.0,
        cross_target_block=0,
        sdpa_backend="auto",
        cross_attention_backend="query_preserving_kv",
        qrkv_projection_backend="separate",
        cross_latent_count=0,
        cross_latent_stabilization=False,
        cross_latent_fp32_training=True,
        cross_anchor_count=anchors,
        cross_anchor_positional_count=positional,
        cross_anchor_stabilization=True,
        cross_anchor_fp32_training=True,
        cross_attention_layers=(2, 4),
        long_history_dim=0,
        long_history_hidden=32,
        branch_mode="neural_head",
        branch_field_dim=8,
        branch_hidden=32,
    ).train()
    rows, length = 4, 17
    static_tokens = torch.randn(rows, length, 48)
    dynamic = torch.stack([
        torch.randint(0, int(size), (rows, length))
        for size in DYNAMIC_FIELD_SIZES
    ], dim=-1)
    valid = torch.ones(rows, length, dtype=torch.bool)
    valid[-1, 12:] = False
    batch = {
        "dynamic_uop_fields": dynamic,
        "chunk_summary": torch.randn(rows, len(CHUNK_SUMMARY_NAMES)),
        "relation_features": torch.randn(rows, len(RELATION_FEATURE_NAMES)),
        "uarch_features": torch.randn(rows, len(UARCH_FEATURE_NAMES)),
        "state_features": torch.randn(rows, len(STATE_FEATURE_NAMES)),
        "valid_uop_mask": valid,
        "sample_ptr": torch.tensor([0, 2, 4], dtype=torch.long),
        "branch_mask": torch.zeros(rows, length, dtype=torch.bool),
    }
    token, core = interaction(static_tokens, batch)
    (token.square().mean() + core.square().mean()).backward()
    backends = [layer.cross_attention_backend for layer in interaction.layers]
    expected = [
        "local_only", "query_preserving_kv",
        "local_only", "query_preserving_kv",
    ]
    if backends != expected:
        raise RuntimeError(f"cross layer selection mismatch: {backends}")
    bad = [
        name for name, parameter in interaction.named_parameters()
        if parameter.grad is None or not bool(torch.isfinite(parameter.grad).all())
    ]
    if bad:
        raise RuntimeError(f"interaction has missing/non-finite gradients: {bad[:8]}")
    return {
        "token_shape": list(token.shape),
        "core_shape": list(core.shape),
        "layer_backends": backends,
        "all_parameter_gradients_finite": True,
    }


def _measure(
    function: Callable[[], torch.Tensor],
    *,
    warmup: int,
    repeats: int,
    iterations: int,
) -> Dict[str, float]:
    for _ in range(warmup):
        function()
    torch.cuda.synchronize()
    timings = []
    for _ in range(repeats):
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


def benchmark_cuda(args: argparse.Namespace) -> Dict[str, Any] | None:
    if not torch.cuda.is_available():
        return None
    device = torch.device(args.device)
    torch.manual_seed(args.seed)
    block = _block(
        args.dimension, args.heads, args.anchors, args.positional,
    ).to(device).eval()
    dtype = torch.bfloat16
    r = torch.randn(
        args.cores, args.length, args.dimension, device=device, dtype=dtype,
    )
    k = torch.randn_like(r)
    v = torch.randn_like(r)
    valid = torch.ones(
        args.cores, args.length, device=device, dtype=torch.bool,
    )
    sample_ptr = torch.tensor([0, args.cores], dtype=torch.long)

    def autocast():
        return torch.autocast(
            device_type="cuda", dtype=torch.bfloat16, enabled=True,
        )

    def legacy() -> torch.Tensor:
        with autocast():
            return block._cross_attention_legacy(r, k, v, valid, sample_ptr)

    def compressed() -> torch.Tensor:
        with autocast():
            return block._cross_attention_query_preserving_kv(
                r, k, v, valid, sample_ptr,
            )

    with torch.inference_mode():
        result = compressed()
        if not bool(torch.isfinite(result).all()):
            raise RuntimeError("query-preserving K/V CUDA output is non-finite")
        legacy_timing = _measure(
            legacy, warmup=args.warmup,
            repeats=args.repeats, iterations=args.iterations,
        )
        compressed_timing = _measure(
            compressed, warmup=args.warmup,
            repeats=args.repeats, iterations=args.iterations,
        )
    legacy_scores = (
        args.cores * args.length * (args.cores - 1) * args.length
    )
    compressed_scores = (
        args.cores * args.anchors * args.length
        + args.cores * args.length * (args.cores - 1) * args.anchors
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
            "anchors": args.anchors,
            "positional_anchors": args.positional,
        },
        "legacy": legacy_timing,
        "query_preserving_kv": compressed_timing,
        "latency_speedup": (
            legacy_timing["median_ms"] / compressed_timing["median_ms"]
        ),
        "theoretical_score_elements": {
            "legacy": legacy_scores,
            "query_preserving_kv": compressed_scores,
        },
        "theoretical_score_reduction": 1.0 - compressed_scores / legacy_scores,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--cores", type=int, default=32)
    parser.add_argument("--length", type=int, default=256)
    parser.add_argument("--dimension", type=int, default=960)
    parser.add_argument("--heads", type=int, default=15)
    parser.add_argument("--anchors", type=int, default=16)
    parser.add_argument("--positional", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()
    if min(
        args.cores, args.length, args.dimension, args.heads, args.anchors,
    ) <= 0:
        raise SystemExit("shape arguments must be positive")
    if not 0 < args.positional < args.anchors:
        raise SystemExit("positional must be in (0,anchors)")
    if args.dimension % args.heads:
        raise SystemExit("dimension must be divisible by heads")
    report: Dict[str, Any] = {
        "schema": "tcsim-v29-query-preserving-kv-benchmark-1",
        "cpu_validation": validate_cpu(args.anchors, args.positional),
        "interaction_cpu_validation": validate_interaction_cpu(
            args.anchors, args.positional,
        ),
        "cuda_benchmark": benchmark_cuda(args),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
