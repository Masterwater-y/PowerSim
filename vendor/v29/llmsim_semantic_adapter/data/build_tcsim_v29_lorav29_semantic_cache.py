#!/usr/bin/env python3
"""Materialize the static macro cache produced by a trained LoRA-v29 adapter.

LoRA-v29 is trained through ``L_v29`` but its input is still a deterministic
static macro prompt.  Deployment therefore does not need to run Qwen online:
this tool replays the exact immutable batch=4 prompt program once, freezes the
adapted vectors, and records both adapter and training-checkpoint provenance.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

import numpy as np
import torch


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from data.build_macro_v29_semantic_cache import (  # noqa: E402
    ATTENTION_IMPLEMENTATION,
    RECOMPUTE_ABS_TOLERANCE,
    adapter_directory_fingerprint,
    build_static_prompt_records,
    encode_records,
    file_sha256,
    json_fingerprint,
    tokenizer_fingerprint,
    verify_encoded_prefix,
)
from train.macro_v29_dataset import (  # noqa: E402
    CachedSemanticSource,
    MacroContractError,
    ParquetInstructionResolver,
)


CHECKPOINT_SCHEMA = "tcsim-v29-frozen-base-lora-v29-checkpoint-1"
ADAPTER_SCHEMA = "tcsim-v29-lora-v29-detachable-adapter-1"


def _torch_load(path: Path) -> Any:
    try:
        return torch.load(
            path, map_location="cpu", weights_only=False, mmap=True,
        )
    except (TypeError, RuntimeError):
        return torch.load(path, map_location="cpu", weights_only=False)


def _read_adapter_contract(
    checkpoint_path: Path,
    base_cache: CachedSemanticSource,
    base_model: Path,
) -> tuple[Mapping[str, Any], Path, str, str]:
    payload = _torch_load(checkpoint_path)
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA:
        raise MacroContractError("not a LoRA-v29 trainable checkpoint")
    if payload.get("mode") != "lora" or not payload.get("lora"):
        raise MacroContractError("adapted cache requires a non-empty LoRA checkpoint")
    step = int(payload.get("step", -1))
    adapter = Path(str(payload.get("adapter_path", ""))).resolve()
    if not adapter.is_dir() or adapter.name != f"step_{step:08d}":
        raise MacroContractError(
            f"checkpoint adapter path/step mismatch: step={step} path={adapter}"
        )
    adapter_config_path = adapter / "adapter_config.json"
    adapter_weights = adapter / "adapter_model.safetensors"
    if not adapter_config_path.is_file() or not adapter_weights.is_file():
        raise MacroContractError("detachable adapter artifacts are incomplete")
    adapter_config = json.loads(adapter_config_path.read_text())
    configured_base = Path(
        str(adapter_config.get("base_model_name_or_path", ""))
    ).resolve()
    if configured_base != base_model:
        raise MacroContractError(
            f"adapter/base model mismatch: {configured_base} != {base_model}"
        )
    if int(adapter_config.get("r", -1)) != int(
        payload["contract"]["qwen"]["lora_rank"]
    ):
        raise MacroContractError("adapter rank differs from checkpoint contract")
    if bool(adapter_config.get("inference_mode")) is not True:
        raise MacroContractError("saved LoRA adapter is not in inference mode")
    contract = payload.get("contract", {})
    cached_contract = contract.get("semantic_cache", {})
    if cached_contract.get("manifest_hash") != base_cache.manifest_hash:
        raise MacroContractError("LoRA checkpoint/base semantic cache mismatch")
    requested_base = Path(str(contract.get("qwen", {}).get("base_model", ""))).resolve()
    if requested_base != base_model:
        raise MacroContractError("LoRA checkpoint/Qwen base path mismatch")
    adapter_fp = adapter_directory_fingerprint(adapter)
    checkpoint_sha = file_sha256(checkpoint_path)
    return payload, adapter, adapter_fp, checkpoint_sha


def _validate_reusable_cache(
    output: Path,
    *,
    base_hash: str,
    adapter_fp: str,
    checkpoint_sha: str,
) -> bool:
    manifest_path = output / "manifest.json"
    if not manifest_path.is_file():
        return False
    manifest = json.loads(manifest_path.read_text())
    expected = {
        "derived_from_semantic_cache_manifest_hash": base_hash,
        "semantic_encoder_adapter_schema": ADAPTER_SCHEMA,
        "semantic_encoder_lora_adapter_fingerprint": adapter_fp,
        "lora_v29_training_checkpoint_sha256": checkpoint_sha,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return False
    cache = CachedSemanticSource(output)
    for entry in cache.manifest["binaries"]:
        cache._load_binary(entry["parquet"])
    print(
        f"[LoRA-v29 cache] reuse root={output} "
        f"manifest_sha256={cache.manifest_hash}",
        flush=True,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-cache-root", required=True)
    parser.add_argument("--training-checkpoint", required=True)
    parser.add_argument("--base-model", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-prompt-tokens", type=int, default=256)
    parser.add_argument("--verify-samples", type=int, default=4)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    base_cache_root = Path(args.base_cache_root).resolve()
    checkpoint_path = Path(args.training_checkpoint).resolve()
    base_model = Path(args.base_model).resolve()
    output = Path(args.out).resolve()
    base_cache = CachedSemanticSource(base_cache_root)
    payload, adapter, adapter_fp, checkpoint_sha = _read_adapter_contract(
        checkpoint_path, base_cache, base_model,
    )
    if str(Path(base_cache.manifest["semantic_encoder_model"]).resolve()) != str(
        base_model
    ):
        raise MacroContractError("base cache encoder path differs from Qwen base")
    print(
        f"[LoRA-v29 cache preflight] step={payload['step']} "
        f"adapter_fp={adapter_fp} checkpoint_sha={checkpoint_sha}",
        flush=True,
    )
    if args.preflight_only:
        return 0
    if not args.force and (output / "manifest.json").is_file():
        if _validate_reusable_cache(
            output,
            base_hash=base_cache.manifest_hash,
            adapter_fp=adapter_fp,
            checkpoint_sha=checkpoint_sha,
        ):
            return 0
        raise MacroContractError(
            "existing output cache does not match this LoRA/checkpoint; "
            "choose a fresh --out path or pass --force explicitly"
        )

    from peft import PeftModel
    from transformers import AutoModel, AutoTokenizer

    device = torch.device(args.device)
    if device.type != "cuda" or not torch.cuda.is_available():
        raise MacroContractError("Qwen3-14B adapted cache generation requires CUDA")
    dtype_name = str(
        base_cache.manifest.get("semantic_encoder_compute_dtype", "bf16")
    )
    dtype_by_name = {
        "bf16": torch.bfloat16,
        "fp16": torch.float16,
        "fp32": torch.float32,
    }
    if dtype_name not in dtype_by_name:
        raise MacroContractError(f"unsupported cache compute dtype {dtype_name!r}")
    dtype = dtype_by_name[dtype_name]
    batch_size = int(base_cache.manifest["semantic_encoder_batch_size"])
    if batch_size != int(payload["contract"]["gradient_cache"]["microbatch"]):
        raise MacroContractError("cache and LoRA canonical microbatch differ")

    tokenizer = AutoTokenizer.from_pretrained(base_model, local_files_only=True)
    tokenizer.padding_side = "right"
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    if tokenizer_fingerprint(tokenizer) != base_cache.manifest["tokenizer_fingerprint"]:
        raise MacroContractError("tokenizer differs from immutable semantic cache")
    model = AutoModel.from_pretrained(
        base_model,
        torch_dtype=dtype,
        attn_implementation=str(
            base_cache.manifest.get(
                "semantic_encoder_attention_implementation",
                ATTENTION_IMPLEMENTATION,
            )
        ),
        local_files_only=True,
    )
    model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model = model.to(device).eval()
    for parameter in model.parameters():
        parameter.requires_grad_(False)

    shard_root = output / "shards"
    shard_root.mkdir(parents=True, exist_ok=True)
    binary_entries = []
    build_reports = []
    started_all = time.perf_counter()
    prompt_schema = str(base_cache.manifest["semantic_prompt_schema_version"])
    for source_entry in base_cache.manifest["binaries"]:
        binary_hash = str(source_entry["binary_hash"])
        parquet = str(Path(source_entry["parquet"]).resolve())
        _loaded_hash, source_arrays = base_cache._load_binary(parquet)
        pcs = np.asarray(source_arrays["pcs"], dtype=np.uint64)
        resolver = ParquetInstructionResolver(parquet)
        records = build_static_prompt_records(
            resolver,
            (int(value) for value in pcs),
            prompt_schema_version=prompt_schema,
            previous_instructions=4,
        )
        started = time.perf_counter()
        semantic, anchor = encode_records(
            records,
            tokenizer,
            model,
            device=device,
            batch_size=batch_size,
            max_prompt_tokens=int(args.max_prompt_tokens),
        )
        anchor_error = float(np.max(np.abs(
            anchor.astype(np.float32)
            - np.asarray(source_arrays["anchor"], dtype=np.float32)
        )))
        if anchor_error > RECOMPUTE_ABS_TOLERANCE:
            raise MacroContractError(
                f"LoRA unexpectedly changed input embedding anchor: {anchor_error}"
            )
        source_keys = np.asarray(source_arrays["semantic_key_hashes"])
        key_hashes = np.asarray([
            json_fingerprint({
                "source_key": bytes(value).decode("ascii"),
                "adapter_fingerprint": adapter_fp,
                "training_checkpoint_sha256": checkpoint_sha,
            }).encode("ascii")
            for value in source_keys
        ], dtype="S64")
        relative = f"shards/{binary_hash}.npz"
        destination = output / relative
        temporary = destination.with_suffix(".tmp.npz")
        np.savez(
            str(temporary).removesuffix(".npz"),
            pcs=pcs,
            semantic=semantic,
            anchor=anchor,
            semantic_key_hashes=key_hashes,
        )
        try:
            with np.load(temporary, allow_pickle=False) as stored:
                verification = verify_encoded_prefix(
                    records,
                    stored["semantic"],
                    stored["anchor"],
                    tokenizer,
                    model,
                    device=device,
                    batch_size=batch_size,
                    max_prompt_tokens=int(args.max_prompt_tokens),
                    verify_samples=int(args.verify_samples),
                )
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        build_reports.append({
            "binary_hash": binary_hash,
            "n_pcs": int(len(pcs)),
            "base_anchor_max_abs_error": anchor_error,
            **verification,
            "elapsed_s": time.perf_counter() - started,
        })
        binary_entries.append({
            "binary_hash": binary_hash,
            "parquet": parquet,
            "parquet_sha256": file_sha256(parquet),
            "cache_file": relative,
            "n_pcs": int(len(pcs)),
            "pc_set_hash": json_fingerprint([int(value) for value in pcs]),
            "shard_sha256": file_sha256(destination),
        })
        print(
            f"[LoRA-v29 cache] built {binary_hash} pcs={len(pcs)} "
            f"seconds={time.perf_counter() - started:.1f}",
            flush=True,
        )

    manifest = copy.deepcopy(base_cache.manifest)
    for key in ("build_reports", "total_elapsed_s", "built_at"):
        manifest.pop(key, None)
    manifest.update({
        "binaries": binary_entries,
        "build_reports": build_reports,
        "total_elapsed_s": time.perf_counter() - started_all,
        "derived_from_semantic_cache": str(base_cache_root),
        "derived_from_semantic_cache_manifest_hash": base_cache.manifest_hash,
        "semantic_encoder_adapter_schema": ADAPTER_SCHEMA,
        "semantic_encoder_lora_adapter": str(adapter),
        "semantic_encoder_lora_adapter_fingerprint": adapter_fp,
        "lora_v29_training_checkpoint": str(checkpoint_path),
        "lora_v29_training_checkpoint_sha256": checkpoint_sha,
        "lora_v29_training_step": int(payload["step"]),
        "lora_v29_training_best_validation": float(payload["best_validation"]),
        "semantic_encoder_lora_training_contract": {
            "objective": payload["contract"].get("loss", {
                "task": "L_v29",
            }),
            "base_v29": payload["contract"]["base_v29"],
            "data": payload["contract"]["data"],
            "qwen": payload["contract"]["qwen"],
        },
    })
    manifest_path = output / "manifest.json"
    temporary_manifest = output / "manifest.json.tmp"
    temporary_manifest.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )
    os.replace(temporary_manifest, manifest_path)
    loaded = CachedSemanticSource(output)
    for entry in loaded.manifest["binaries"]:
        loaded._load_binary(entry["parquet"])
    print(
        f"[LoRA-v29 cache done] root={output} "
        f"manifest_sha256={loaded.manifest_hash}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
