#!/usr/bin/env python3
"""Real frozen-Qwen smoke for offline semantics and online macro soft tokens.

This bounded CPU/GPU check encodes a few real static instructions, writes a
versioned smoke cache, gathers it through the deployment data path, and calls
the same real Qwen once with 256 macro positions.  It is not a throughput or
accuracy result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import torch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from data.build_macro_v29_semantic_cache import (
    ANCHOR_POLICY,
    CACHE_SCHEMA_VERSION,
    CONTEXT_POLICY,
    POOLING_POLICY,
    PROMPT_SCHEMA_VERSION,
    build_static_records,
    encode_records,
    json_fingerprint,
    model_artifact_fingerprint,
    tokenizer_fingerprint,
)
from model.macro_v29_model import MacroV29Config, MacroV29TimingModel, macro_v29_loss
from train.macro_v29_dataset import (
    CachedSemanticSource,
    PackedTraceMacroContext,
    ParquetInstructionResolver,
    collate_macro_contexts,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--static-dict", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--tail-macros", type=int, default=2)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--dtype", choices=("fp32", "bf16", "fp16"), default="bf16",
    )
    args = parser.parse_args()

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but is unavailable")
    dtype = {
        "fp32": torch.float32,
        "bf16": torch.bfloat16,
        "fp16": torch.float16,
    }[args.dtype]
    if device.type == "cpu" and dtype == torch.float16:
        raise ValueError("CPU real-Qwen smoke supports fp32 or bf16")

    from transformers import AutoConfig, AutoModel, AutoTokenizer

    output_root = Path(args.output)
    output_root.mkdir(parents=True, exist_ok=True)
    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model, local_files_only=True,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    config = AutoConfig.from_pretrained(args.base_model, local_files_only=True)
    config.output_hidden_states = False
    config.use_cache = False
    artifact_fp = model_artifact_fingerprint(
        args.base_model, allow_download=False,
    )
    revision = str(getattr(config, "_commit_hash", None) or artifact_fp)
    config_fp = json_fingerprint(config.to_dict())
    tokenizer_fp = tokenizer_fingerprint(tokenizer)
    load_started = time.perf_counter()
    backbone = AutoModel.from_pretrained(
        args.base_model,
        config=config,
        torch_dtype=dtype,
        attn_implementation="sdpa",
        local_files_only=True,
    ).to(device).eval()
    for parameter in backbone.parameters():
        parameter.requires_grad_(False)
    load_s = time.perf_counter() - load_started

    resolver = ParquetInstructionResolver(args.static_dict)
    context = PackedTraceMacroContext(args.trace_root)
    if len(context.core_ids) != 1:
        raise RuntimeError("bounded real-Qwen smoke requires a c1 trace")
    core_id = int(context.core_ids[0])
    view = context.views[core_id]
    requested_tail = int(args.tail_macros)
    if not 1 <= requested_tail <= min(256, view.n_macros):
        raise ValueError("tail-macros is out of range")
    candidate_cursor = view.n_macros - requested_tail
    state_tick = int(view.macro_end_tick[candidate_cursor]) - 1
    cursor = int(view.cursor_at_tick(state_tick))
    n_tail = int(view.n_macros - cursor)
    if not 1 <= n_tail <= 256:
        raise RuntimeError(
            f"tick-aligned tail contains {n_tail} macros, outside [1,256]"
        )
    dynamic_pcs = [
        int(value) for value in view.macro_pc[cursor:cursor + n_tail]
    ]
    records = build_static_records(
        resolver,
        dynamic_pcs,
        encoder_provenance={
            "semantic_encoder_model_revision": revision,
            "semantic_encoder_artifact_fingerprint": artifact_fp,
            "semantic_encoder_config_fingerprint": config_fp,
            "tokenizer_hash": tokenizer_fp,
        },
    )
    encode_started = time.perf_counter()
    semantic, anchor = encode_records(
        records,
        tokenizer,
        backbone,
        device=device,
        batch_size=len(records),
        max_prompt_tokens=256,
    )
    encode_s = time.perf_counter() - encode_started

    cache_root = output_root / "real_qwen_smoke_cache"
    shard_root = cache_root / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    shard_relative = f"shards/{resolver.binary_hash}.npz"
    np.savez(
        cache_root / shard_relative,
        pcs=np.asarray([row["pc"] for row in records], dtype=np.uint64),
        semantic=semantic,
        anchor=anchor,
        semantic_key_hashes=np.asarray([
            str(row["semantic_key_hash"]).encode("ascii") for row in records
        ], dtype="S64"),
    )
    shard_path = cache_root / shard_relative
    parquet_path = Path(args.static_dict).resolve()
    cached_pcs = [int(row["pc"]) for row in records]
    pc_set_hash = hashlib.sha256(json.dumps(
        cached_pcs, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    manifest = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "semantic_encoder_model": str(args.base_model),
        "semantic_encoder_revision": revision,
        "semantic_encoder_artifact_fingerprint": artifact_fp,
        "semantic_encoder_config_fingerprint": config_fp,
        "tokenizer_fingerprint": tokenizer_fp,
        "semantic_prompt_schema_version": PROMPT_SCHEMA_VERSION,
        "semantic_pooling_policy": POOLING_POLICY,
        "context_policy": CONTEXT_POLICY,
        "semantic_dim": int(semantic.shape[1]),
        "anchor_dim": int(anchor.shape[1]),
        "anchor_policy": ANCHOR_POLICY,
        "offline_encoder_frozen": True,
        "smoke_partial_cache": True,
        "model_facing_identity_fields": [],
        "binaries": [{
            "binary_hash": resolver.binary_hash,
            "parquet": str(parquet_path),
            "cache_file": shard_relative,
            "n_pcs": len(records),
            "parquet_sha256": hashlib.sha256(
                parquet_path.read_bytes()
            ).hexdigest(),
            "shard_sha256": hashlib.sha256(shard_path.read_bytes()).hexdigest(),
            "pc_set_hash": pc_set_hash,
        }],
    }
    (cache_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    semantic_cache = CachedSemanticSource(cache_root)
    windows = context.context_from_cursors(
        {core_id: cursor},
        resolver,
        None,
        state_tick=state_tick,
        state_time_cycles=(
            state_tick - context.roi_origin_tick
        ) / context.tick_per_cycle,
        include_labels=True,
        last_commit_cycles=None,
        semantic_cache=semantic_cache,
        parquet_path=str(Path(args.static_dict).resolve()),
    )
    batch = collate_macro_contexts([windows])
    batch = {
        key: value.to(device) if torch.is_tensor(value) else value
        for key, value in batch.items()
    }
    torch.manual_seed(20260719)
    model = MacroV29TimingModel(
        backbone,
        MacroV29Config(
            d_llm=int(config.hidden_size),
            d_model=64,
            d_field=4,
            n_heads=4,
            freeze_backbone=True,
            semantic_input_mode="cached_macro_soft_token",
            semantic_dim=int(semantic.shape[1]),
            anchor_policy=ANCHOR_POLICY,
        ),
    ).to(device).eval()
    forward_started = time.perf_counter()
    with torch.no_grad():
        predictions = model(batch)
        losses = macro_v29_loss(predictions, batch)
    if device.type == "cuda":
        torch.cuda.synchronize(device)
    forward_s = time.perf_counter() - forward_started
    valid = batch["valid_macro_mask"].bool()
    failures = []
    if int(valid.sum()) != n_tail:
        failures.append("valid macro count mismatch")
    if not bool(torch.isfinite(losses["total"]).item()):
        failures.append("real-Qwen timing loss is non-finite")
    if not bool(torch.all(
        predictions["commit_time_macro"][:, 1:]
        >= predictions["commit_time_macro"][:, :-1]
    ).item()):
        failures.append("commit time is not monotonic")
    report = {
        "status": "PASS" if not failures else "FAIL",
        "kind": "real-frozen-qwen-offline-and-online-soft-token-smoke",
        "base_model": str(args.base_model),
        "revision": revision,
        "artifact_fingerprint": artifact_fp,
        "backbone_class": type(backbone).__name__,
        "backbone_dtype": str(next(backbone.parameters()).dtype),
        "semantic_shape": list(semantic.shape),
        "online_input_shape": [1, 256, int(config.hidden_size)],
        "valid_macros": int(valid.sum()),
        "load_s": load_s,
        "offline_encode_s": encode_s,
        "online_forward_and_heads_s": forward_s,
        "loss": float(losses["total"]),
        "failures": failures,
        "throughput_or_accuracy_evidence": False,
        "gpu_available": torch.cuda.is_available(),
    }
    destination = output_root / "real_qwen_semantic_smoke_report.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, destination)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
