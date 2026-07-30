#!/usr/bin/env python3
"""Microbenchmark dense/packed GSS attention and three-anchor gap heads."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import torch
import torch.nn as nn

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tcsim.v30.gss import GSS_CATEGORICAL_FIELDS, GSS_CONTINUOUS_FIELDS
from tcsim.v30.model import CausalGSSResidualAdapter


def measure(function, *, warmup: int, iterations: int) -> float:
    for _ in range(warmup):
        function()
    torch.cuda.synchronize()
    begin = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    begin.record()
    for _ in range(iterations):
        function()
    end.record()
    torch.cuda.synchronize()
    return float(begin.elapsed_time(end)) / iterations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--length", type=int, default=256)
    parser.add_argument("--events", type=int, default=20)
    parser.add_argument("--token-dim", type=int, default=960)
    parser.add_argument("--adapter-dim", type=int, default=128)
    parser.add_argument("--warmup", type=int, default=50)
    parser.add_argument("--iterations", type=int, default=300)
    args = parser.parse_args()
    device = torch.device("cuda")
    rows, length, events = args.rows, args.length, args.events
    adapter = CausalGSSResidualAdapter(
        token_dim=args.token_dim, adapter_dim=args.adapter_dim,
        heads=4, field_dim=8, max_K=length,
    ).to(device).eval()
    gap_head = nn.Sequential(
        nn.Linear(args.token_dim, args.token_dim), nn.GELU(),
        nn.Linear(args.token_dim, 1),
    ).to(device).eval()
    token = torch.randn(rows, length, args.token_dim, device=device)
    categorical = torch.zeros(
        rows, length, len(GSS_CATEGORICAL_FIELDS),
        dtype=torch.long, device=device,
    )
    continuous = torch.zeros(
        rows, length, len(GSS_CONTINUOUS_FIELDS), device=device,
    )
    memory = torch.zeros(rows, length, dtype=torch.bool, device=device)
    selected = torch.linspace(2, length - 1, events, device=device).long()
    memory[:, selected] = True
    categorical[:, selected, 0] = 3
    categorical[:, selected, 7] = 1
    categorical[:, selected, 8] = 1
    compact_categorical = categorical[:, selected]
    compact_continuous = continuous[:, selected]
    compact_positions = selected.view(1, -1).expand(rows, -1)
    compact_valid = torch.ones(rows, events, dtype=torch.bool, device=device)
    compact_memory = compact_valid.clone()
    delta = torch.randn_like(token)

    def dense_adapter():
        return adapter(token, categorical, continuous, memory)

    def packed_adapter():
        return adapter(
            token, categorical, continuous, memory,
            event_categorical=compact_categorical,
            event_continuous=compact_continuous,
            event_positions=compact_positions,
            event_valid=compact_valid,
            event_is_memory=compact_memory,
        )

    def sequential_heads():
        return tuple(gap_head(value) for value in (
            token, token + 0.25 * delta, token + delta,
        ))

    def batched_heads():
        values = torch.stack((
            token, token + 0.25 * delta, token + delta,
        ), dim=0).reshape(3 * rows, length, args.token_dim)
        return gap_head(values)

    with torch.inference_mode(), torch.autocast("cuda", dtype=torch.bfloat16):
        result = {
            "dense_adapter_ms": measure(
                dense_adapter, warmup=args.warmup, iterations=args.iterations,
            ),
            "packed_adapter_ms": measure(
                packed_adapter, warmup=args.warmup, iterations=args.iterations,
            ),
            "sequential_gap_heads_ms": measure(
                sequential_heads, warmup=args.warmup, iterations=args.iterations,
            ),
            "batched_gap_heads_ms": measure(
                batched_heads, warmup=args.warmup, iterations=args.iterations,
            ),
        }
    result["packed_adapter_change"] = (
        result["packed_adapter_ms"] / result["dense_adapter_ms"] - 1.0
    )
    result["batched_gap_head_change"] = (
        result["batched_gap_heads_ms"] / result["sequential_gap_heads_ms"] - 1.0
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
