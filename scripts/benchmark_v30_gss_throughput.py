#!/usr/bin/env python3
"""Measure frozen-v29 model throughput with a zero-init causal GSS adapter."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from typing import Any, Mapping

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in os.sys.path:
    os.sys.path.insert(0, REPO_ROOT)

from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences  # noqa: E402
from tcsim.v29.model import build_model  # noqa: E402
from tcsim.v30.gss import (  # noqa: E402
    GSS_CATEGORICAL_CARDINALITIES,
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSS_G1_CONTINUOUS_FIELDS,
)


G1_CONTINUOUS_INDICES = tuple(
    GSS_CONTINUOUS_FIELDS.index(name) for name in GSS_G1_CONTINUOUS_FIELDS
)


CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def torch_load(path: str, device: torch.device) -> Mapping[str, Any]:
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


class CausalGSSResidualAdapter(nn.Module):
    """Parallel causal adapter; zero output preserves canonical v29 exactly."""

    def __init__(self, token_dim: int, adapter_dim: int, heads: int, field_dim: int) -> None:
        super().__init__()
        if adapter_dim % heads:
            raise ValueError("adapter_dim must be divisible by heads")
        self.heads = int(heads)
        self.head_dim = int(adapter_dim) // int(heads)
        self.embeddings = nn.ModuleList([
            nn.Embedding(int(cardinality), int(field_dim))
            for cardinality in GSS_CATEGORICAL_CARDINALITIES
        ])
        encoded_dim = len(GSS_CATEGORICAL_FIELDS) * int(field_dim) + len(GSS_G1_CONTINUOUS_FIELDS)
        self.gss_encoder = nn.Sequential(
            nn.LayerNorm(encoded_dim),
            nn.Linear(encoded_dim, int(adapter_dim)),
            nn.GELU(),
            nn.Linear(int(adapter_dim), int(adapter_dim)),
        )
        self.token_norm = nn.LayerNorm(int(token_dim))
        self.q_projection = nn.Linear(int(token_dim), int(adapter_dim), bias=False)
        self.k_projection = nn.Linear(int(adapter_dim), int(adapter_dim), bias=False)
        self.v_projection = nn.Linear(int(adapter_dim), int(adapter_dim), bias=False)
        self.output = nn.Linear(int(adapter_dim), int(token_dim), bias=False)
        self.register_buffer(
            "causal_mask",
            torch.ones((256, 256), dtype=torch.bool).tril(),
            persistent=False,
        )
        nn.init.zeros_(self.output.weight)

    def forward(
        self,
        token: torch.Tensor,
        categorical: torch.Tensor,
        continuous: torch.Tensor,
        memory_mask: torch.Tensor,
    ) -> torch.Tensor:
        parts = [
            embedding(categorical[..., index])
            for index, embedding in enumerate(self.embeddings)
        ]
        gss = self.gss_encoder(torch.cat(parts + [continuous], dim=-1))
        gss = gss * memory_mask.unsqueeze(-1).to(gss.dtype)
        rows, length, _ = token.shape
        q = self.q_projection(self.token_norm(token))
        k = self.k_projection(gss)
        v = self.v_projection(gss)
        q = q.view(rows, length, self.heads, self.head_dim).transpose(1, 2)
        k = k.view(rows, length, self.heads, self.head_dim).transpose(1, 2)
        v = v.view(rows, length, self.heads, self.head_dim).transpose(1, 2)
        key_mask = memory_mask.bool().clone()
        key_mask[:, 0] = True  # zero/sentinel key prevents an all-masked prefix
        causal = self.causal_mask[:length, :length]
        allowed = causal.view(1, 1, length, length) & key_mask.view(rows, 1, 1, length)
        attended = F.scaled_dot_product_attention(q, k, v, attn_mask=allowed)
        attended = attended.transpose(1, 2).reshape(rows, length, -1)
        return self.output(attended)


def device_batch(batch: Mapping[str, Any], device: torch.device) -> dict[str, Any]:
    return {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }


def sidecar_batch(sidecar_path: str, batch: Mapping[str, Any]):
    meta = load_json(os.path.join(sidecar_path, "meta.json"))
    if tuple(meta["categorical_fields"]) != GSS_CATEGORICAL_FIELDS:
        raise RuntimeError("GSS sidecar categorical contract mismatch")
    rows, length, _ = batch["per_uop_fields"].shape
    categorical = np.zeros(
        (rows, length, len(GSS_CATEGORICAL_FIELDS)), dtype=np.int64,
    )
    continuous = np.zeros(
        (rows, length, len(GSS_G1_CONTINUOUS_FIELDS)), dtype=np.float32,
    )
    memory_mask = np.zeros((rows, length), dtype=np.bool_)
    core_slots = batch["core_slots"].numpy()
    cursors = batch["cursors"].numpy()
    valid = batch["valid_uop_mask"].numpy()
    memory_events = 0
    for row in range(rows):
        core = int(core_slots[row])
        cursor = int(cursors[row])
        count = int(valid[row].sum())
        core_dir = os.path.join(sidecar_path, "cores", str(core))
        indices = np.load(os.path.join(core_dir, "index.npy"), mmap_mode="r")
        cat = np.load(os.path.join(core_dir, "categorical.npy"), mmap_mode="r")
        cont = np.load(os.path.join(core_dir, "continuous.npy"), mmap_mode="r")
        begin = int(np.searchsorted(indices, cursor, side="left"))
        end = int(np.searchsorted(indices, cursor + count, side="left"))
        positions = np.asarray(indices[begin:end], dtype=np.int64) - cursor
        categorical[row, positions] = cat[begin:end]
        continuous[row, positions] = cont[begin:end, G1_CONTINUOUS_INDICES]
        memory_mask[row, positions] = True
        memory_events += len(positions)
    return (
        torch.from_numpy(categorical),
        torch.from_numpy(continuous),
        torch.from_numpy(memory_mask),
        memory_events,
    )


def cuda_blocks(callable_, warmup: int, blocks: int, iterations: int) -> list[float]:
    for _ in range(warmup):
        callable_()
    torch.cuda.synchronize()
    values = []
    for _ in range(blocks):
        start = torch.cuda.Event(enable_timing=True)
        end = torch.cuda.Event(enable_timing=True)
        start.record()
        for _ in range(iterations):
            callable_()
        end.record()
        end.synchronize()
        values.append(float(start.elapsed_time(end)) / iterations)
    return values


def stats(values: list[float]) -> Mapping[str, float]:
    ordered = sorted(values)
    return {
        "median_ms": float(statistics.median(values)),
        "min_ms": float(min(values)),
        "max_ms": float(max(values)),
        "p95_ms": float(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default=os.path.join(REPO_ROOT, "data/v29_global_time_dataset/manifest.json"),
    )
    parser.add_argument(
        "--checkpoint", default=os.path.join(REPO_ROOT, "ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt"),
    )
    parser.add_argument(
        "--sidecar-root", default=os.path.join(REPO_ROOT, "data/v30_gss_ready_sidecars"),
    )
    parser.add_argument("--workload", default="W_v28_redis_heldout")
    parser.add_argument("--cores", type=int, default=32)
    parser.add_argument("--adapter-dim", type=int, default=128)
    parser.add_argument("--adapter-heads", type=int, default=4)
    parser.add_argument("--field-dim", type=int, default=8)
    parser.add_argument("--warmup", type=int, default=8)
    parser.add_argument("--blocks", type=int, default=9)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "logs/v30_gss_throughput.json"))
    args = parser.parse_args()
    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError("this benchmark requires CUDA")
    manifest = load_json(os.path.abspath(args.manifest))
    matches = [
        dict(row) for row in manifest["splits"]["development_heldout"]
        if str(row["workload"]) == args.workload and int(row["n_cores"]) == args.cores
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one benchmark trace, got {len(matches)}")
    row = matches[0]
    dataset = V29GlobalTimeDataset([row], sequence_length=1, sequence_stride=1)
    item = dataset[len(dataset) // 2]
    cpu_batch = collate_v29_sequences([item])
    sidecar_path = os.path.join(args.sidecar_root, "traces", str(row["trace_id"]))
    build_started = time.perf_counter()
    categorical_cpu, continuous_cpu, memory_mask_cpu, memory_events = sidecar_batch(
        sidecar_path, cpu_batch,
    )
    feature_build_ms = (time.perf_counter() - build_started) * 1000.0
    batch = device_batch(cpu_batch, device)
    categorical = categorical_cpu.to(device)
    continuous = continuous_cpu.to(device)
    memory_mask = memory_mask_cpu.to(device)
    payload = torch_load(os.path.abspath(args.checkpoint), torch.device("cpu"))
    model = build_model(payload["config"]["model"], payload["config"]["chunk"]["horizons"])
    model.load_state_dict(payload["model"], strict=True)
    model.to(device).eval()
    d_dyn = int(payload["config"]["model"]["d_dyn"])
    adapter = CausalGSSResidualAdapter(
        d_dyn, args.adapter_dim, args.adapter_heads, args.field_dim,
    ).to(device).eval()
    amp = torch.autocast(device_type="cuda", dtype=torch.bfloat16)
    with torch.inference_mode(), amp:
        static = model.static_encoder(batch["per_uop_fields"])

    def baseline_call():
        with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            return model.forward_from_static(batch, static, include_horizon_outputs=False)

    def hook(_module, inputs):
        token = inputs[0]
        delta = adapter(token, categorical, continuous, memory_mask)
        return (token + delta,)

    baseline_output = baseline_call()["commit_time"]
    baseline_values = cuda_blocks(
        baseline_call, args.warmup, args.blocks, args.iterations,
    )
    handle = model.gap_head.register_forward_pre_hook(hook)
    try:
        adapter_output = baseline_call()["commit_time"]
        adapter_values = cuda_blocks(
            baseline_call, args.warmup, args.blocks, args.iterations,
        )
    finally:
        handle.remove()
    max_abs = float((baseline_output - adapter_output).abs().max().item())
    if max_abs != 0.0:
        raise RuntimeError(f"zero-init adapter changed v29 output: {max_abs}")

    transfer_values = []
    for _ in range(args.blocks):
        torch.cuda.synchronize()
        started = time.perf_counter()
        for _ in range(args.iterations):
            _cat = categorical_cpu.to(device)
            _cont = continuous_cpu.to(device)
            _mask = memory_mask_cpu.to(device)
        torch.cuda.synchronize()
        transfer_values.append(
            (time.perf_counter() - started) * 1000.0 / args.iterations
        )
    base = stats(baseline_values)
    gss = stats(adapter_values)
    transfer = stats(transfer_values)
    report = {
        "schema_version": "tcsim-v30-gss-throughput-benchmark-1",
        "trace_id": str(row["trace_id"]),
        "checkpoint": os.path.abspath(args.checkpoint),
        "rows": int(cpu_batch["per_uop_fields"].shape[0]),
        "tokens": int(cpu_batch["valid_uop_mask"].sum().item()),
        "memory_events": int(memory_events),
        "memory_fraction": memory_events / max(1, int(cpu_batch["valid_uop_mask"].sum().item())),
        "adapter": {
            "dimension": int(args.adapter_dim),
            "heads": int(args.adapter_heads),
            "field_dimension": int(args.field_dim),
            "parameters": sum(parameter.numel() for parameter in adapter.parameters()),
            "zero_init_max_abs_timing_delta": max_abs,
        },
        "baseline_model_from_static": base,
        "gss_causal_adapter_model_from_static": gss,
        "model_latency_delta_ms": gss["median_ms"] - base["median_ms"],
        "model_latency_relative_change": gss["median_ms"] / base["median_ms"] - 1.0,
        "model_throughput_relative_change": base["median_ms"] / gss["median_ms"] - 1.0,
        "sidecar_feature_slice_ms_once": feature_build_ms,
        "feature_h2d_transfer": transfer,
        "benchmark": {
            "warmup": args.warmup,
            "blocks": args.blocks,
            "iterations_per_block": args.iterations,
            "dtype": "bfloat16-autocast",
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle_out:
        json.dump(report, handle_out, indent=2, ensure_ascii=False)
        handle_out.write("\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
