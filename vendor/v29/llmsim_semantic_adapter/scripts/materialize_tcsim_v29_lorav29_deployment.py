#!/usr/bin/env python3
"""Compose a deployable B2 checkpoint from frozen v29 plus a trained bridge."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys
from typing import Any, Mapping

import torch


REPO = Path(__file__).resolve().parents[1]
TCSIM_ROOT = Path(os.environ.get("TCSIM_ROOT", "/data00/yinhaolang/TCSim"))
for root in (REPO, TCSIM_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from model.tcsim_v29_semantic import (  # noqa: E402
    BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
    BOUNDED_RESIDUAL_FUSION_ARCHITECTURES,
    LEGACY_FUSION_ARCHITECTURE,
    MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
    RESIDUAL_FUSION_ARCHITECTURES,
    SEMANTIC_EXPERIMENT_SCHEMA,
    build_semantic_model,
)
from train.macro_v29_dataset import CachedSemanticSource  # noqa: E402
from train.tcsim_v29_semantic_train import (  # noqa: E402
    SEMANTIC_CHECKPOINT_SCHEMA,
)


TRAINABLE_SCHEMA = "tcsim-v29-frozen-base-lora-v29-checkpoint-1"


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path) -> Any:
    try:
        return torch.load(path, map_location="cpu", weights_only=False, mmap=True)
    except (TypeError, RuntimeError):
        return torch.load(path, map_location="cpu", weights_only=False)


def _save(value: Any, destination: Path) -> None:
    if destination.exists():
        raise RuntimeError(f"refusing to overwrite {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp-{os.getpid()}")
    try:
        torch.save(value, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trainable-checkpoint", required=True)
    parser.add_argument("--base-v29-checkpoint", required=True)
    parser.add_argument("--semantic-cache-root", required=True)
    parser.add_argument("--static-manifest", required=True)
    parser.add_argument("--variant", choices=("b2-lora", "b2-frozen"), required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--reuse-existing", action="store_true")
    args = parser.parse_args()

    trainable_path = Path(args.trainable_checkpoint).resolve()
    base_path = Path(args.base_v29_checkpoint).resolve()
    static_manifest = Path(args.static_manifest).resolve()
    destination = Path(args.out).resolve()
    trainable = _load(trainable_path)
    base = _load(base_path)
    if trainable.get("checkpoint_schema") != TRAINABLE_SCHEMA:
        raise RuntimeError("unsupported trainable LoRA-v29 checkpoint")
    expected_mode = "lora" if args.variant == "b2-lora" else "frozen"
    if trainable.get("mode") != expected_mode:
        raise RuntimeError(
            f"variant={args.variant} requires mode={expected_mode}, "
            f"got {trainable.get('mode')}"
        )
    if int(base.get("step", -1)) != 59000:
        raise RuntimeError("deployment composition requires v29 best step 59000")
    base_sha = _sha256(base_path)
    base_reference = trainable["contract"]["base_v29"]
    if base_reference.get("sha256") != base_sha:
        raise RuntimeError("trainable checkpoint/base v29 SHA mismatch")

    cache = CachedSemanticSource(Path(args.semantic_cache_root).resolve())
    bridge_contract = dict(trainable.get("contract", {}).get("bridge", {}))
    fusion_architecture = str(bridge_contract.get(
        "fusion_architecture", LEGACY_FUSION_ARCHITECTURE,
    ))
    adapter_hidden_dim = int(bridge_contract.get(
        "semantic_adapter_hidden_dim", 1024,
    ))
    semantic_slot_count = int(bridge_contract.get(
        "semantic_slot_count", 4,
    ) or 4)
    semantic_attention_heads = int(bridge_contract.get(
        "semantic_attention_heads", 4,
    ) or 4)
    max_residual_rms_ratio = float(bridge_contract.get(
        "max_residual_rms_ratio", 0.05,
    ) or 0.05)
    trained_cache_hash = trainable["contract"]["semantic_cache"]["manifest_hash"]
    if args.variant == "b2-lora":
        if cache.manifest.get(
            "derived_from_semantic_cache_manifest_hash"
        ) != trained_cache_hash:
            raise RuntimeError("adapted cache does not derive from training cache")
        checkpoint_sha = _sha256(trainable_path)
        if cache.manifest.get("lora_v29_training_checkpoint_sha256") != checkpoint_sha:
            raise RuntimeError("adapted cache was not built from this best checkpoint")
    elif cache.manifest_hash != trained_cache_hash:
        raise RuntimeError("frozen deployment cache differs from training cache")

    trainable_sha = _sha256(trainable_path)
    static_manifest_sha = _sha256(static_manifest)
    if destination.exists():
        if not args.reuse_existing:
            raise RuntimeError(f"refusing to overwrite {destination}")
        existing = _load(destination)
        existing_contract = existing.get("contract", {})
        semantic_contract = existing_contract.get("semantic", {})
        materialization = existing_contract.get(
            "lora_v29_deployment_materialization", {}
        )
        expected = {
            "checkpoint_schema": existing.get("checkpoint_schema")
            == SEMANTIC_CHECKPOINT_SCHEMA,
            "variant": semantic_contract.get("variant") == args.variant,
            "semantic_cache": semantic_contract.get(
                "semantic_cache_manifest_hash"
            ) == cache.manifest_hash,
            "trainable_checkpoint": materialization.get(
                "trainable_checkpoint_sha256"
            ) == trainable_sha,
            "base_checkpoint": materialization.get(
                "base_v29_checkpoint_sha256"
            ) == base_sha,
            "fusion_architecture": existing_contract.get(
                "semantic_fusion", {}
            ).get("architecture") == fusion_architecture,
            "semantic_adapter_hidden_dim": existing_contract.get(
                "semantic_fusion", {}
            ).get("semantic_adapter_hidden_dim") == adapter_hidden_dim,
            "semantic_slot_count": existing_contract.get(
                "semantic_fusion", {}
            ).get("semantic_slot_count") == (
                semantic_slot_count
                if fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "static_manifest": existing_contract.get(
                "static_manifest", {}
            ).get("sha256") == static_manifest_sha,
            "step": int(existing.get("step", -1)) == int(trainable["step"]),
        }
        failed = [name for name, valid in expected.items() if not valid]
        if failed:
            raise RuntimeError(
                "existing materialized checkpoint provenance mismatch: "
                + ", ".join(failed)
            )
        print(
            f"[materialize reuse] variant={args.variant} "
            f"step={trainable['step']} checkpoint={destination}",
            flush=True,
        )
        return 0

    config = dict(base["config"])
    model_config = dict(config["model"])
    horizons = tuple(float(value) for value in base["contract"]["horizons"])
    model = build_semantic_model(
        model_config,
        horizons,
        variant=args.variant,
        semantic_dim=cache.semantic_dim,
        fusion_architecture=fusion_architecture,
        semantic_adapter_hidden_dim=adapter_hidden_dim,
        semantic_max_residual_rms_ratio=max_residual_rms_ratio,
        semantic_slot_count=semantic_slot_count,
        semantic_attention_heads=semantic_attention_heads,
    )
    model.backbone.load_state_dict(base["model"], strict=True)
    model.semantic_bridge.load_state_dict(trainable["bridge"], strict=True)

    semantic_contract = {
        "variant": args.variant,
        "semantic_source": (
            "frozen_lora_static_macro_cache"
            if args.variant == "b2-lora" else "frozen_static_macro_cache"
        ),
        "offline_encoder_frozen": True,
        **cache.contract,
        "semantic_intervention": {
            key: value for key, value in cache.intervention_report.items()
            if key != "binaries"
        },
    }
    contract = dict(base["contract"])
    contract.update({
        "semantic_experiment_schema": SEMANTIC_EXPERIMENT_SCHEMA,
        "semantic": semantic_contract,
        "semantic_fusion": {
            "architecture": fusion_architecture,
            "semantic_adapter_hidden_dim": adapter_hidden_dim,
            "semantic_slot_count": (
                semantic_slot_count
                if fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "semantic_attention_heads": (
                semantic_attention_heads
                if fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "max_residual_rms_ratio": (
                max_residual_rms_ratio
                if fusion_architecture
                in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                else None
            ),
            "location": bridge_contract.get(
                "injection_location", "before_tcsim_v29_full_qkvr",
            ),
            "mapping": "macro_cache_to_each_uop_by_macro_pc",
            "transport": "per_sequence_unique_table_plus_uop_index",
            "projection": (
                f"RMSNorm({cache.semantic_dim})->Linear({cache.semantic_dim},"
                f"{adapter_hidden_dim})->SiLU->KVSlots("
                f"{semantic_slot_count}x{int(model_config.get('d_dyn', 384))})"
                f"->UOPAttention(heads={semantic_attention_heads})"
                f"->ZeroLinear({int(model_config.get('d_dyn', 384))},"
                f"{int(model_config.get('d_dyn', 384))})"
                if fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else
                f"RMSNorm({cache.semantic_dim})->Linear({cache.semantic_dim},"
                f"{adapter_hidden_dim})->SiLU->ZeroLinear("
                f"{adapter_hidden_dim},{int(model_config.get('d_dyn', 384))})"
                if fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES
                else f"RMSNorm({cache.semantic_dim})->Linear("
                f"{cache.semantic_dim},{int(model_config.get('d_static', 256))})"
            ),
            "gate": (
                "dynamic_sigmoid_with_hard_per_token_rms_bound"
                if fusion_architecture
                in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                else "sigmoid_linear_residual"
            ),
            "gate_bias": float(model_config.get("semantic_gate_bias", -2.0)),
            "base_function_at_initialization": (
                "exact_identity"
                if fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES
                else "not_identity"
            ),
        },
        "static_manifest": {
            "path": str(static_manifest),
            "sha256": static_manifest_sha,
        },
        "lora_v29_deployment_materialization": {
            "trainable_checkpoint": str(trainable_path),
            "trainable_checkpoint_sha256": trainable_sha,
            "base_v29_checkpoint": str(base_path),
            "base_v29_checkpoint_sha256": base_sha,
            "loss": trainable["contract"].get("loss", {
                "task": "L_v29",
            }),
            "fusion_architecture": fusion_architecture,
            "semantic_adapter_hidden_dim": adapter_hidden_dim,
            "semantic_slot_count": (
                semantic_slot_count
                if fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "semantic_attention_heads": (
                semantic_attention_heads
                if fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "semantic_max_residual_rms_ratio": (
                max_residual_rms_ratio
                if fusion_architecture
                in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                else None
            ),
            "adapter_path": trainable.get("adapter_path"),
            "adapter_fingerprint": cache.manifest.get(
                "semantic_encoder_lora_adapter_fingerprint"
            ),
        },
    })
    output = {
        "checkpoint_schema": SEMANTIC_CHECKPOINT_SCHEMA,
        "model": model.state_dict(),
        "step": int(trainable["step"]),
        "best_validation": float(trainable["best_validation"]),
        "contract": contract,
        "config": config,
        "history": list(trainable.get("history", [])),
    }
    _save(output, destination)
    reloaded = _load(destination)
    if (
        reloaded.get("checkpoint_schema") != SEMANTIC_CHECKPOINT_SCHEMA
        or reloaded.get("contract", {}).get("semantic", {}).get("variant")
        != args.variant
    ):
        raise RuntimeError("materialized checkpoint failed reload validation")
    print(
        f"[materialize PASS] variant={args.variant} step={trainable['step']} "
        f"checkpoint={destination}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
