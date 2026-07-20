#!/usr/bin/env python3
"""P0 online-Qwen speed ceiling for [active_core_rows,256,D] soft tokens."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys
import time

import torch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument(
        "--dtype", choices=("fp32", "bf16", "fp16"), default="bf16",
    )
    parser.add_argument("--rows", type=int, default=8)
    parser.add_argument("--macros", type=int, default=256)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=50)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise RuntimeError(
            "P0 acceptance timing requires an available CUDA GPU; CPU numbers "
            "must not be reported against the 150 ms gate"
        )
    dtype = {
        "fp32": torch.float32,
        "bf16": torch.bfloat16,
        "fp16": torch.float16,
    }[args.dtype]
    from transformers import AutoConfig, AutoModel

    config = AutoConfig.from_pretrained(args.base_model, local_files_only=True)
    config.output_hidden_states = False
    config.use_cache = False
    model = AutoModel.from_pretrained(
        args.base_model,
        config=config,
        torch_dtype=dtype,
        attn_implementation="sdpa",
        local_files_only=True,
    ).to(device).eval()
    embedding = model.get_input_embeddings()
    generator = torch.Generator(device=device).manual_seed(20260719)
    ids = torch.randint(
        0,
        int(embedding.num_embeddings),
        (int(args.rows), int(args.macros)),
        generator=generator,
        device=device,
    )
    with torch.no_grad():
        soft_macro = embedding(ids)
    attention = torch.ones(
        int(args.rows), int(args.macros), dtype=torch.long, device=device,
    )

    @torch.no_grad()
    def invoke():
        return model(
            inputs_embeds=soft_macro,
            attention_mask=attention,
            output_hidden_states=False,
            use_cache=False,
        ).last_hidden_state

    for _ in range(int(args.warmup)):
        invoke()
    torch.cuda.synchronize(device)
    latencies_ms = []
    for _ in range(int(args.iterations)):
        started = time.perf_counter()
        result = invoke()
        torch.cuda.synchronize(device)
        latencies_ms.append(1000.0 * (time.perf_counter() - started))
    ordered = sorted(latencies_ms)
    p50 = float(statistics.median(ordered))
    p95 = float(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))])
    report = {
        "status": "PASS" if p50 <= 150.0 else "FAIL",
        "kind": "macro-v29-semantic-p0-online-qwen-ceiling",
        "base_model": str(args.base_model),
        "revision": str(getattr(config, "_commit_hash", None)),
        "device": str(device),
        "dtype": str(dtype),
        "input_shape": [int(args.rows), int(args.macros), int(config.hidden_size)],
        "single_batched_call_per_iteration": True,
        "warmup": int(args.warmup),
        "iterations": int(args.iterations),
        "p50_ms": p50,
        "p95_ms": p95,
        "mean_ms": float(statistics.mean(latencies_ms)),
        "position_per_s_at_p50": float(
            int(args.rows) * int(args.macros) / (p50 / 1000.0)
        ),
        "p0_gate_ms": 150.0,
        "output_shape": list(result.shape),
        "note": "speed ceiling only; excludes cache/context/heads/scheduler",
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())
