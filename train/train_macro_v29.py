#!/usr/bin/env python3
"""Train the v29 256-macro timing model.

The mainline consumes frozen offline semantics as one soft token per macro;
the historical native-token expansion remains available as an explicit A/B
baseline.  Both paths optimize only real commit/prefix/progress/drift/branch
labels.  No teacher checkpoint or distillation target is used.
"""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from functools import partial
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
import time
from typing import Any, Dict, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import torch
import torch.distributed as dist
from torch import nn
from torch.utils.data import DataLoader, DistributedSampler, Sampler

from model.macro_v29_model import (  # noqa: E402
    MacroV29Config,
    MacroV29TimingModel,
    macro_v29_loss,
)
from train.macro_v29_dataset import (  # noqa: E402
    MacroContractError,
    MacroV29SequenceDataset,
    SEMANTIC_TEXT_VARIANTS,
    collate_macro_sequences,
)


SEMANTIC_VARIANTS = (
    "real", "pseudo", "mnemonic_shuffle", "register_rename",
    "random_init", "side_only", "llm_only",
)


def json_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def tokenizer_fingerprint(tokenizer: Any) -> str:
    vocab = tokenizer.get_vocab()
    return json_fingerprint({
        "class": type(tokenizer).__name__,
        "vocab": sorted((str(token), int(index)) for token, index in vocab.items()),
        "special_tokens_map": getattr(tokenizer, "special_tokens_map", {}),
    })


def attach_backbone_provenance(
    backbone: nn.Module,
    config: Any,
) -> nn.Module:
    config_payload = (
        config.to_dict() if hasattr(config, "to_dict")
        else {
            "class": type(config).__name__,
            "hidden_size": getattr(config, "hidden_size", None),
        }
    )
    backbone._macro_base_model_commit = str(  # type: ignore[attr-defined]
        getattr(config, "_commit_hash", None) or "local-unversioned"
    )
    backbone._macro_base_config_fingerprint = json_fingerprint(  # type: ignore[attr-defined]
        config_payload
    )
    return backbone


class TinyNativeBackbone(nn.Module):
    """CPU-only contract/smoke backbone; never a semantic accuracy baseline."""

    def __init__(self, vocab_size: int, width: int = 32):
        super().__init__()
        self.config = type("TinyConfig", (), {"hidden_size": int(width)})()
        self.embedding = nn.Embedding(int(vocab_size), int(width))
        self.projection = nn.Linear(int(width), int(width))

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        inputs_embeds=None,
        **kwargs,
    ):
        if (input_ids is None) == (inputs_embeds is None):
            raise ValueError("provide exactly one of input_ids or inputs_embeds")
        embedded = self.embedding(input_ids) if input_ids is not None else inputs_embeds
        hidden = self.projection(embedded)
        return type("TinyOutput", (), {
            "hidden_states": None,
            "last_hidden_state": hidden,
        })()


def interleaved_trace_indices(
    trace_ids: Sequence[str],
    *,
    seed: int = 0,
) -> list[int]:
    """Seed-shuffle within traces, then round-robin across traces."""
    buckets: Dict[str, list[int]] = {}
    for index, trace_id in enumerate(trace_ids):
        buckets.setdefault(str(trace_id), []).append(index)
    ordered = []
    names = sorted(buckets)
    rng = random.Random(int(seed))
    for name in names:
        rng.shuffle(buckets[name])
    maximum = max((len(values) for values in buckets.values()), default=0)
    for offset in range(maximum):
        for name in names:
            values = buckets[name]
            if offset < len(values):
                ordered.append(values[offset])
    return ordered


class DistributedEvalSampler(Sampler[int]):
    """Disjoint rank-strided validation coverage without padded duplicates."""

    def __init__(
        self,
        dataset: Any,
        *,
        num_replicas: int,
        rank: int,
        indices: Sequence[int] | None = None,
    ):
        self.size = int(len(dataset))
        self.num_replicas = int(num_replicas)
        self.rank = int(rank)
        if self.num_replicas <= 0 or not 0 <= self.rank < self.num_replicas:
            raise ValueError("invalid distributed eval rank/world")
        self.indices = tuple(
            range(self.size) if indices is None else (int(value) for value in indices)
        )
        if (
            len(set(self.indices)) != len(self.indices)
            or any(value < 0 or value >= self.size for value in self.indices)
        ):
            raise ValueError("distributed eval indices must be unique and in range")

    def __iter__(self):
        return iter(self.indices[self.rank::self.num_replicas])

    def __len__(self) -> int:
        if self.rank >= len(self.indices):
            return 0
        return (len(self.indices) - 1 - self.rank) // self.num_replicas + 1


class CoreCountBucketedDistributedSampler(Sampler[int]):
    """Keep every DDP global step on one active-core count.

    The dataset remains sequence-uniform: indices are only shuffled and
    grouped by core count, with at most ``world-1`` padding samples per bucket.
    This avoids a c1 rank waiting for a c32 rank on nearly every mixed-core
    step while preserving all c1/c4/c8/c16/c32 examples in every epoch.
    """

    def __init__(
        self,
        dataset: Any,
        *,
        num_replicas: int,
        rank: int,
        seed: int = 0,
    ) -> None:
        self.size = int(len(dataset))
        self.num_replicas = int(num_replicas)
        self.rank = int(rank)
        self.seed = int(seed)
        self.epoch = 0
        if self.num_replicas <= 0 or not 0 <= self.rank < self.num_replicas:
            raise ValueError("invalid bucketed distributed rank/world")
        core_counts = tuple(int(value) for value in dataset.sample_core_counts)
        if len(core_counts) != self.size:
            raise ValueError("sample_core_counts must align with the dataset")
        self.core_counts = core_counts
        buckets: Dict[int, list[int]] = {}
        for index, core_count in enumerate(core_counts):
            if core_count <= 0:
                raise ValueError("sample core counts must be positive")
            buckets.setdefault(core_count, []).append(index)
        self.buckets = {
            core_count: tuple(indices)
            for core_count, indices in sorted(buckets.items())
        }
        self.num_samples = sum(
            math.ceil(len(indices) / self.num_replicas)
            for indices in self.buckets.values()
        )

    def set_epoch(self, epoch: int) -> None:
        self.epoch = int(epoch)

    def __iter__(self):
        generator = torch.Generator()
        generator.manual_seed(self.seed + self.epoch)
        global_steps: list[list[int]] = []
        for indices in self.buckets.values():
            permutation = torch.randperm(
                len(indices), generator=generator,
            ).tolist()
            shuffled = [indices[offset] for offset in permutation]
            padded_size = (
                math.ceil(len(shuffled) / self.num_replicas)
                * self.num_replicas
            )
            if padded_size > len(shuffled):
                repeats = math.ceil(
                    (padded_size - len(shuffled)) / len(shuffled)
                )
                shuffled.extend(
                    (shuffled * repeats)[:padded_size - len(shuffled)]
                )
            global_steps.extend([
                shuffled[offset:offset + self.num_replicas]
                for offset in range(0, padded_size, self.num_replicas)
            ])
        step_order = torch.randperm(
            len(global_steps), generator=generator,
        ).tolist()
        return iter([
            global_steps[step][self.rank] for step in step_order
        ])

    def __len__(self) -> int:
        return self.num_samples


def init_distributed() -> tuple[int, int, int]:
    if "LOCAL_RANK" not in os.environ:
        return 0, 1, 0
    if not torch.cuda.is_available():
        raise RuntimeError("DDP macro training requires CUDA/NCCL")
    local_rank = int(os.environ["LOCAL_RANK"])
    torch.cuda.set_device(local_rank)
    if not dist.is_initialized():
        dist.init_process_group(backend="nccl", init_method="env://")
    return dist.get_rank(), dist.get_world_size(), local_rank


def static_dictionary_map(path: str | Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("schema_version") != "real-x86-objdump-wide-v2":
            raise MacroContractError(
                f"static dictionary is not v2: {row.get('binary_name')}"
            )
        if row.get("scan_scope") != "all_cores":
            raise MacroContractError(
                f"static dictionary lacks all-core coverage: "
                f"{row.get('binary_name')}"
            )
        result[str(row["binary_name"])] = str(row["parquet"])
    return result


def load_macro_sources(
    manifest_path: str | Path,
    static_manifest: str | Path,
    *,
    split: str,
    cores: set[int],
    workloads: set[str],
    workload_roles: set[str],
    max_sources: int,
    partition_override: str | None = None,
) -> list[Dict[str, Any]]:
    manifest = json.loads(Path(manifest_path).read_text())
    rows = manifest.get("splits", {}).get(split)
    if not isinstance(rows, list):
        raise MacroContractError(f"manifest has no list split {split!r}")
    static = static_dictionary_map(static_manifest)
    sources = []
    for row in rows:
        workload = str(row["workload"])
        if cores and int(row["n_cores"]) not in cores:
            continue
        if workloads and workload not in workloads:
            continue
        if workload_roles and str(row.get("workload_role", "")) not in workload_roles:
            continue
        policy = row.get("sample_split")
        if not isinstance(policy, Mapping):
            if partition_override not in {None, "all"}:
                raise MacroContractError(
                    f"source lacks guarded sample_split: {row['trace_id']}"
                )
            policy = {
                "validation_percent": 10,
                "seed": 20260716,
                "guard_cycles": float(max(manifest["horizons"])),
                "require_full_lookahead_within_block": True,
                "partition": "all",
            }
        binary_name = workload.removeprefix("W_")
        static_path = static.get(binary_name)
        if not static_path:
            raise MacroContractError(f"no static dictionary for {workload}")
        normalized_policy = dict(policy)
        if partition_override is not None:
            normalized_policy["partition"] = str(partition_override)
        sources.append({
            "trace_root": str(row["cache_dir"]),
            "static_dict": static_path,
            "sample_split": normalized_policy,
        })
        if max_sources > 0 and len(sources) >= max_sources:
            break
    if not sources:
        raise MacroContractError(
            f"no sources for split={split} cores={sorted(cores)} "
            f"workloads={sorted(workloads)} roles={sorted(workload_roles)}"
        )
    return sources


def build_backbone(args: argparse.Namespace, tokenizer: Any) -> tuple[nn.Module, int]:
    if args.tiny_backbone:
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(int(args.seed))
            backbone = TinyNativeBackbone(len(tokenizer), width=args.tiny_width)
        attach_backbone_provenance(backbone, backbone.config)
        return backbone, int(args.tiny_width)
    from transformers import AutoConfig, AutoModel

    # Timing consumes hidden states only.  The bare model avoids computing
    # unused vocabulary logits for every assembly token.
    model_config = AutoConfig.from_pretrained(
        args.base_model, local_files_only=not bool(args.allow_download),
    )
    model_config.output_hidden_states = False
    model_config.use_cache = False
    if args.semantic_variant == "random_init":
        with torch.random.fork_rng(devices=[]):
            torch.manual_seed(int(args.seed))
            backbone = AutoModel.from_config(model_config)
    else:
        backbone = AutoModel.from_pretrained(
            args.base_model,
            config=model_config,
            torch_dtype=(
                torch.bfloat16 if args.dtype == "bf16"
                else torch.float16 if args.dtype == "fp16"
                else torch.float32
            ),
            attn_implementation="sdpa",
            local_files_only=not bool(args.allow_download),
        )
    backbone.config.output_hidden_states = False
    backbone.config.use_cache = False
    hidden = int(backbone.config.hidden_size)
    if args.freeze_backbone:
        for parameter in backbone.parameters():
            parameter.requires_grad_(False)
        attach_backbone_provenance(backbone, model_config)
        return backbone, hidden
    try:
        from peft import LoraConfig, TaskType, get_peft_model
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("peft is required for macro LoRA training") from exc
    for parameter in backbone.parameters():
        parameter.requires_grad_(False)
    lora = LoraConfig(
        r=int(args.lora_r),
        lora_alpha=int(args.lora_alpha),
        lora_dropout=float(args.lora_dropout),
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        bias="none",
        task_type=TaskType.FEATURE_EXTRACTION,
    )
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(int(args.seed) + 1)
        backbone = get_peft_model(backbone, lora)
    attach_backbone_provenance(backbone, model_config)
    if args.gradient_checkpointing:
        backbone.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False},
        )
        backbone.enable_input_require_grads()
    return backbone, hidden


def text_variant_for(semantic_variant: str) -> str:
    return (
        semantic_variant
        if semantic_variant in SEMANTIC_TEXT_VARIANTS else "real"
    )


def semantic_mode_for(semantic_variant: str) -> str:
    if semantic_variant == "side_only":
        return "side_only"
    if semantic_variant == "llm_only":
        return "llm_only"
    return "fusion"


def build_timing_model(
    args: argparse.Namespace,
    tokenizer: Any,
) -> MacroV29TimingModel:
    online_backbone_type = str(getattr(
        args, "online_backbone_type", "qwen_lora",
    ))
    semantic_source = str(getattr(
        args,
        "semantic_source",
        "learned_null" if str(getattr(
            args, "semantic_input_mode", "native_token",
        )) == "learned_null_macro_token" else "real_cache",
    ))
    if online_backbone_type == "qwen_lora":
        backbone, d_llm = build_backbone(args, tokenizer)
    elif online_backbone_type == "causal_transformer":
        backbone = None
        d_llm = int(getattr(args, "online_input_dim", 1536))
    else:
        raise MacroContractError(
            f"unsupported online backbone type {online_backbone_type!r}"
        )
    core_mixer_mode = getattr(args, "core_mixer_mode", None)
    if core_mixer_mode is None:
        # run.json files created before the vNext schema used the legacy
        # one-summary-per-core mixer.  New CLI runs always record the mode.
        core_mixer_mode = (
            "macro_cross_attention"
            if str(getattr(args, "architecture_schema", ""))
            == "macro-v29-soft-cross-core-1"
            else "summary"
        )
    # Trainable timing parameters must start identically across semantic
    # variants, independent of RNG consumed while constructing the backbone.
    with torch.random.fork_rng(devices=[]):
        torch.manual_seed(int(args.seed) + 2)
        semantic_input = str(
            getattr(args, "semantic_input_mode", "native_token")
        )
        return MacroV29TimingModel(
            backbone,
            MacroV29Config(
                d_llm=d_llm,
                d_model=int(args.d_model if not args.tiny_backbone else 64),
                d_field=int(args.d_field if not args.tiny_backbone else 4),
                n_heads=int(args.n_heads if not args.tiny_backbone else 4),
                freeze_backbone=False,
                semantic_mode=semantic_mode_for(args.semantic_variant),
                semantic_input_mode=semantic_input,
                semantic_dim=(
                    int(args.semantic_dim)
                    if semantic_input in {
                        "cached_macro_soft_token", "learned_null_macro_token",
                    } else None
                ),
                online_backbone_type=online_backbone_type,
                semantic_source=semantic_source,
                online_transformer_layers=int(getattr(
                    args, "online_transformer_layers", 5,
                )),
                online_transformer_heads=int(getattr(
                    args, "online_transformer_heads", 8,
                )),
                online_transformer_ffn_multiplier=int(getattr(
                    args, "online_transformer_ffn_multiplier", 4,
                )),
                cross_ffn_multiplier=int(getattr(
                    args, "cross_ffn_multiplier", 4,
                )),
                cross_target_block=int(getattr(
                    args, "cross_target_block", 0,
                )),
                cross_gate_init=float(getattr(
                    args, "cross_gate_init", -2.0,
                )),
                backbone_core_chunk_size=int(getattr(
                    args, "backbone_core_chunk_size", 0,
                )),
                backbone_chunk_checkpoint=bool(getattr(
                    args, "backbone_chunk_checkpoint", False,
                )),
                core_mixer_mode=str(core_mixer_mode),
                anchor_policy=str(getattr(
                    args, "anchor_policy", "mean_native_input_embedding",
                )),
            ),
        )


def tensor_batch(batch: Mapping[str, Any], device: torch.device) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in batch.items():
        if torch.is_tensor(value):
            result[key] = value.to(device, non_blocking=True)
        elif key == "sample_period_cycles":
            result[key] = float(value)
    return result


def autocast_context(device: torch.device, dtype_name: str):
    enabled = device.type == "cuda" and dtype_name in {"bf16", "fp16"}
    dtype = torch.bfloat16 if dtype_name == "bf16" else torch.float16
    return torch.autocast(device_type=device.type, dtype=dtype, enabled=enabled)


def all_ranks_true(local_value: bool, device: torch.device) -> bool:
    """Return True only when every distributed rank reports True."""

    flag = torch.tensor(
        1 if local_value else 0, dtype=torch.int32, device=device,
    )
    if dist.is_initialized():
        dist.all_reduce(flag, op=dist.ReduceOp.MIN)
    return bool(flag.item())


def batch_diagnostic(
    raw: Mapping[str, Any],
    batch: Mapping[str, Any],
    *,
    rank: int,
    losses: Mapping[str, torch.Tensor] | None = None,
    predictions: Mapping[str, torch.Tensor] | None = None,
) -> Dict[str, Any]:
    """Build a compact, JSON-safe description of a failing local batch."""

    def tensor_values(key: str) -> list[int]:
        value = raw.get(key)
        if not torch.is_tensor(value):
            return []
        return [int(item) for item in value.detach().cpu().reshape(-1).tolist()]

    def nonfinite_names(values: Mapping[str, torch.Tensor] | None) -> list[str]:
        return sorted(
            str(name) for name, value in (values or {}).items()
            if torch.is_tensor(value)
            and not bool(torch.isfinite(value.detach()).all().item())
        )

    attention = batch.get("attention_mask")
    valid = batch.get("valid_macro_mask")
    target = batch.get("commit_time_target_macro")
    result: Dict[str, Any] = {
        "rank": int(rank),
        "trace_id": [str(value) for value in raw.get("trace_id", [])],
        "sample_indices": tensor_values("sample_indices"),
        "sample_ticks": tensor_values("sample_ticks"),
        "sample_block_ids": tensor_values("sample_block_ids"),
        "nonfinite_losses": nonfinite_names(losses),
        "nonfinite_predictions": nonfinite_names(predictions),
    }
    if torch.is_tensor(attention):
        result["token_lengths"] = [
            int(value) for value in attention.detach().sum(dim=1).cpu().tolist()
        ]
    if torch.is_tensor(valid):
        result["valid_macros"] = [
            int(value) for value in valid.detach().sum(dim=1).cpu().tolist()
        ]
    if torch.is_tensor(target):
        finite_target = target.detach()[torch.isfinite(target.detach())]
        result["target_time_min"] = (
            float(finite_target.min().item()) if finite_target.numel() else None
        )
        result["target_time_max"] = (
            float(finite_target.max().item()) if finite_target.numel() else None
        )
    if losses is not None:
        result["losses"] = {
            str(name): float(value.detach().float().item())
            for name, value in losses.items()
        }
    return result


def nonfinite_gradient_details(
    module: nn.Module,
    *,
    limit: int = 32,
) -> list[Dict[str, Any]]:
    """Describe trainable gradients containing NaN or Inf values."""

    details: list[Dict[str, Any]] = []
    for name, parameter in module.named_parameters():
        gradient = parameter.grad
        if gradient is None or bool(torch.isfinite(gradient).all().item()):
            continue
        detached = gradient.detach()
        finite = detached[torch.isfinite(detached)]
        details.append({
            "name": str(name),
            "shape": list(detached.shape),
            "dtype": str(detached.dtype),
            "nonfinite": int((~torch.isfinite(detached)).sum().item()),
            "finite_abs_max": (
                float(finite.abs().max().item()) if finite.numel() else None
            ),
        })
        if len(details) >= int(limit):
            break
    return details


def raise_distributed_nonfinite(
    stage: str,
    local_details: Mapping[str, Any] | None,
    *,
    rank: int,
) -> None:
    """Raise the same actionable non-finite error on every rank."""

    gathered: list[Mapping[str, Any] | None] = [None]
    if dist.is_initialized():
        gathered = [None for _ in range(dist.get_world_size())]
        dist.all_gather_object(gathered, local_details)
    else:
        gathered[0] = local_details
    failures = [value for value in gathered if value is not None]
    payload = json.dumps(failures, sort_keys=True, allow_nan=True)
    raise FloatingPointError(
        f"non-finite {stage}; failing_rank_batches={payload}"
    )


def reduced_validation(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    *,
    dtype_name: str,
    max_batches: int,
) -> Dict[str, Any]:
    model.eval()
    # absolute error, target magnitude, valid macro count, total loss sum,
    # context-batch count, signed error
    totals = torch.zeros(6, dtype=torch.float64, device=device)
    trace_totals: Dict[str, list[float]] = {}
    with torch.no_grad():
        for batch_index, raw in enumerate(loader):
            if max_batches > 0 and batch_index >= max_batches:
                break
            batch = tensor_batch(raw, device)
            with autocast_context(device, dtype_name):
                predictions = model(batch)
                losses = macro_v29_loss(predictions, batch)
            valid = batch["valid_macro_mask"].bool()
            target = batch["commit_time_target_macro"].double()
            predicted = predictions["commit_time_macro"].double()
            totals[0] += torch.abs(predicted - target)[valid].sum()
            totals[1] += torch.abs(target)[valid].sum()
            totals[2] += valid.sum()
            totals[3] += losses["total"].double()
            totals[4] += 1
            totals[5] += (predicted - target)[valid].sum()
            row_abs = (
                torch.abs(predicted - target) * valid
            ).sum(dim=1).detach().double().cpu().tolist()
            row_target = (
                torch.abs(target) * valid
            ).sum(dim=1).detach().double().cpu().tolist()
            row_valid = valid.sum(dim=1).detach().cpu().tolist()
            sequence_ids = raw["trace_id"]
            row_sequences = batch["row_sequence"].detach().cpu().tolist()
            for row, sequence_index in enumerate(row_sequences):
                trace_id = str(sequence_ids[int(sequence_index)])
                aggregate = trace_totals.setdefault(trace_id, [0.0, 0.0, 0.0])
                aggregate[0] += float(row_abs[row])
                aggregate[1] += float(row_target[row])
                aggregate[2] += float(row_valid[row])
    if dist.is_initialized():
        dist.all_reduce(totals, op=dist.ReduceOp.SUM)
        gathered: list[Dict[str, list[float]] | None] = [
            None for _ in range(dist.get_world_size())
        ]
        dist.all_gather_object(gathered, trace_totals)
        merged: Dict[str, list[float]] = {}
        for shard in gathered:
            for trace_id, values in (shard or {}).items():
                aggregate = merged.setdefault(trace_id, [0.0, 0.0, 0.0])
                for index, value in enumerate(values):
                    aggregate[index] += float(value)
        trace_totals = merged
    per_trace = {
        trace_id: {
            "absolute_error": values[0],
            "target_magnitude": values[1],
            "valid_macros": int(values[2]),
            "commit_wape": values[0] / max(values[1], 1.0e-12),
        }
        for trace_id, values in sorted(trace_totals.items())
    }
    return {
        "commit_wape": float((totals[0] / totals[1].clamp_min(1e-12)).item()),
        "valid_macros": int(totals[2].item()),
        "mean_loss": float((totals[3] / totals[4].clamp_min(1)).item()),
        "batches": int(totals[4].item()),
        "commit_signed_bias": float((
            totals[5] / totals[1].clamp_min(1e-12)
        ).item()),
        "per_trace": per_trace,
    }


def save_trainable(
    model: nn.Module,
    output: Path,
    step: int,
    *,
    contract: Mapping[str, Any] | None = None,
) -> Path:
    core = model.module if hasattr(model, "module") else model
    state = {
        name: parameter.detach().cpu()
        for name, parameter in core.named_parameters()
        if parameter.requires_grad
    }
    path = output / f"trainable_step{int(step):08d}.pt"
    temporary = path.with_suffix(path.suffix + ".tmp")
    torch.save({
        "checkpoint_schema": "macro-v29-trainable-v2",
        "dataset_schema": "global-time-v29-macro-native-2",
        "semantic_run_contract": "macro-v29-semantic-run-v4",
        "step": int(step),
        "contract": dict(contract or {}),
        "state_dict": state,
    }, temporary)
    os.replace(temporary, path)
    return path


def load_trainable(
    model: nn.Module,
    checkpoint: str | Path,
    *,
    expected_contract: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    """Strictly restore every and only trainable parameter."""

    payload = torch.load(
        checkpoint, map_location="cpu", weights_only=True,
    )
    if payload.get("checkpoint_schema") not in {
        "macro-v29-trainable-v1", "macro-v29-trainable-v2",
    }:
        raise MacroContractError("unsupported macro trainable checkpoint schema")
    if payload.get("dataset_schema") != "global-time-v29-macro-native-2":
        raise MacroContractError("checkpoint dataset schema mismatch")
    observed_contract = payload.get("contract")
    if not isinstance(observed_contract, Mapping):
        raise MacroContractError("checkpoint lacks a contract mapping")
    for key, expected in dict(expected_contract or {}).items():
        if observed_contract.get(key) != expected:
            raise MacroContractError(
                f"checkpoint contract {key}={observed_contract.get(key)!r} "
                f"!= {expected!r}"
            )
    state = payload.get("state_dict")
    if not isinstance(state, Mapping):
        raise MacroContractError("checkpoint lacks a trainable state_dict")
    core = model.module if hasattr(model, "module") else model
    parameters = dict(core.named_parameters())
    expected_names = {
        name for name, parameter in parameters.items()
        if parameter.requires_grad
    }
    observed_names = set(state)
    if observed_names != expected_names:
        raise MacroContractError(
            "checkpoint trainable keys differ: "
            f"missing={sorted(expected_names - observed_names)[:8]} "
            f"unexpected={sorted(observed_names - expected_names)[:8]}"
        )
    with torch.no_grad():
        for name in sorted(expected_names):
            value = state[name]
            parameter = parameters[name]
            if tuple(value.shape) != tuple(parameter.shape):
                raise MacroContractError(
                    f"checkpoint shape mismatch for {name}: "
                    f"{tuple(value.shape)} != {tuple(parameter.shape)}"
                )
            parameter.copy_(value.to(
                device=parameter.device, dtype=parameter.dtype,
            ))
    return {
        "step": int(payload.get("step", 0)),
        "contract": dict(observed_contract),
        "n_trainable_tensors": len(expected_names),
    }


def parse_csv_ints(value: str) -> set[int]:
    return {int(item) for item in value.split(",") if item.strip()}


def parse_csv_strings(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default="/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json",
    )
    parser.add_argument(
        "--static-manifest",
        default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict/manifest.jsonl",
    )
    parser.add_argument("--train-split", default="train")
    parser.add_argument(
        "--validation-split", default="train",
        help="source trace list for macro validation; partition is recomputed "
             "as validation (default uses the same seed0 base traces as train)",
    )
    parser.add_argument(
        "--cores", default="8",
        help="active-core counts; semantic mainline defaults to the requested c8 scope",
    )
    parser.add_argument("--workloads", default="")
    parser.add_argument("--train-workload-roles", default="")
    parser.add_argument("--validation-workload-roles", default="")
    parser.add_argument("--max-train-sources", type=int, default=0)
    parser.add_argument("--max-validation-sources", type=int, default=0)
    parser.add_argument(
        "--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument(
        "--semantic-variant", choices=SEMANTIC_VARIANTS, default="real",
        help="capacity-matched semantic gate/control",
    )
    parser.add_argument(
        "--semantic-input-mode",
        choices=(
            "native_token", "cached_macro_soft_token",
            "learned_null_macro_token",
        ),
        default="cached_macro_soft_token",
        help="mainline uses one cached semantic soft token per macro; select "
             "native_token only for the retained baseline",
    )
    parser.add_argument(
        "--experiment-variant", choices=("A", "B", "E"), default="A",
        help="A=Qwen+real cache, B=ordinary Transformer+real cache, "
             "E=ordinary Transformer+learned-null input",
    )
    parser.add_argument(
        "--online-backbone-type",
        choices=("qwen_lora", "causal_transformer"),
        default="qwen_lora",
    )
    parser.add_argument(
        "--semantic-source",
        choices=("real_cache", "learned_null"),
        default="real_cache",
    )
    parser.add_argument("--online-input-dim", type=int, default=1536)
    parser.add_argument("--online-transformer-layers", type=int, default=5)
    parser.add_argument("--online-transformer-heads", type=int, default=8)
    parser.add_argument(
        "--online-transformer-ffn-multiplier", type=int, default=4,
    )
    parser.add_argument("--allow-download", action="store_true")
    parser.add_argument("--tiny-backbone", action="store_true")
    parser.add_argument("--tiny-width", type=int, default=32)
    parser.add_argument("--freeze-backbone", action="store_true")
    parser.add_argument("--gradient-checkpointing", action="store_true")
    parser.add_argument("--lora-r", type=int, default=32)
    parser.add_argument("--lora-alpha", type=int, default=64)
    parser.add_argument("--lora-dropout", type=float, default=0.05)
    parser.add_argument("--d-model", type=int, default=384)
    parser.add_argument("--d-field", type=int, default=8)
    parser.add_argument("--n-heads", type=int, default=8)
    parser.add_argument("--cross-ffn-multiplier", type=int, default=4)
    parser.add_argument(
        "--core-mixer-mode",
        choices=("macro_cross_attention",),
        default="macro_cross_attention",
        help="vNext uses per-macro cross-core attention; legacy summary mode "
             "is selected automatically only when loading old run.json",
    )
    parser.add_argument(
        "--cross-target-block", type=int, default=0,
        help="target cores per cross-SDPA block; 0 processes all targets",
    )
    parser.add_argument("--cross-gate-init", type=float, default=-2.0)
    parser.add_argument(
        "--backbone-core-chunk-size", type=int, default=0,
        help="online-Qwen core rows per exact forward chunk; 0 uses all rows",
    )
    parser.add_argument(
        "--backbone-chunk-checkpoint", action="store_true",
        help="recompute each online-Qwen row chunk during backward",
    )
    parser.add_argument("--sequence-length", type=int, default=4)
    parser.add_argument("--sequence-stride", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument(
        "--core-count-bucketed-sampler", action="store_true",
        help="group DDP global steps by active-core count for efficient "
             "mixed-core training",
    )
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--lr-head", type=float, default=3e-4)
    parser.add_argument("--lr-lora", type=float, default=1e-4)
    parser.add_argument("--lr-online-backbone", type=float, default=3e-4)
    parser.add_argument("--weight-decay", type=float, default=0.05)
    parser.add_argument("--warmup-fraction", type=float, default=0.03)
    parser.add_argument(
        "--lr-schedule-steps", type=int, default=0,
        help="total steps used by the LR schedule; 0 uses --max-steps. "
             "This permits faithful short diagnostic runs of a longer job.",
    )
    parser.add_argument("--gradient-clip", type=float, default=1.0)
    parser.add_argument(
        "--diagnose-gradients-from-step", type=int, default=-1,
        help="debug only: from this zero-based step, inspect each rank's "
             "local gradients before manually synchronizing them",
    )
    parser.add_argument(
        "--detect-anomaly-from-step", type=int, default=-1,
        help="debug only: enable autograd anomaly detection from this step",
    )
    parser.add_argument(
        "--detect-anomaly-rank", type=int, default=-1,
        help="rank on which to detect anomalies; -1 enables every rank",
    )
    parser.add_argument("--dtype", choices=("fp32", "bf16", "fp16"), default="bf16")
    parser.add_argument(
        "--allow-cudnn-sdpa", action="store_true",
        help="allow the cuDNN scaled-dot-product-attention backend. Disabled "
             "by default because its BF16 backward produced reproducible NaNs "
             "for a valid c08 training batch on this stack.",
    )
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--eval-every", type=int, default=100)
    parser.add_argument("--eval-batches", type=int, default=32)
    parser.add_argument("--save-every", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument(
        "--init-trainable", default="",
        help="strictly initialize trainable tensors from a v1 macro checkpoint",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--token-cache-root", default="",
        help="prebuilt macro-v29 native-token cache root; when set the "
             "dataset skips online tokenization for cached PCs",
    )
    parser.add_argument(
        "--semantic-cache-root", default="",
        help="required versioned frozen-Qwen semantic cache for the mainline",
    )
    parser.add_argument(
        "--target-stride-macro", type=int, default=256,
        help="deployment scheduler contract recorded with the checkpoint",
    )
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if int(args.d_model) <= 0 or int(args.d_model) % int(args.n_heads) != 0:
        raise MacroContractError("--d-model must be positive and divisible by --n-heads")
    if int(args.cross_ffn_multiplier) <= 0:
        raise MacroContractError("--cross-ffn-multiplier must be positive")
    if int(args.cross_target_block) < 0:
        raise MacroContractError("--cross-target-block must be non-negative")
    if int(args.backbone_core_chunk_size) < 0:
        raise MacroContractError("--backbone-core-chunk-size must be non-negative")
    if int(args.online_input_dim) <= 0:
        raise MacroContractError("--online-input-dim must be positive")
    if int(args.online_transformer_layers) <= 0:
        raise MacroContractError("--online-transformer-layers must be positive")
    if int(args.online_transformer_heads) <= 0:
        raise MacroContractError("--online-transformer-heads must be positive")
    if int(args.online_transformer_ffn_multiplier) <= 0:
        raise MacroContractError(
            "--online-transformer-ffn-multiplier must be positive"
        )
    effective_d_model = int(args.d_model if not args.tiny_backbone else 64)
    if effective_d_model % int(args.online_transformer_heads) != 0:
        raise MacroContractError(
            "effective d_model must be divisible by --online-transformer-heads"
        )

    expected_variant_contract = {
        "B": ("causal_transformer", "real_cache", "cached_macro_soft_token"),
        "E": (
            "causal_transformer", "learned_null",
            "learned_null_macro_token",
        ),
    }
    observed_variant_contract = (
        str(args.online_backbone_type), str(args.semantic_source),
        str(args.semantic_input_mode),
    )
    if str(args.experiment_variant) == "A":
        valid_variant_contracts = {
            ("qwen_lora", "real_cache", "cached_macro_soft_token"),
            ("qwen_lora", "real_cache", "native_token"),
        }
        expected_description = "qwen_lora + real semantics + cached/native input"
    else:
        valid_variant_contracts = {
            expected_variant_contract[str(args.experiment_variant)]
        }
        expected_description = repr(next(iter(valid_variant_contracts)))
    if observed_variant_contract not in valid_variant_contracts:
        raise MacroContractError(
            f"experiment variant {args.experiment_variant} requires "
            "online_backbone/semantic_source/input_mode="
            f"{expected_description}, got "
            f"{observed_variant_contract}"
        )

    if args.semantic_input_mode == "cached_macro_soft_token":
        if not args.semantic_cache_root:
            raise MacroContractError(
                "mainline cached_macro_soft_token training requires "
                "--semantic-cache-root"
            )
        if args.token_cache_root:
            raise MacroContractError(
                "--token-cache-root is only valid for native-token A/B runs"
            )
        if args.init_trainable:
            raise MacroContractError(
                "the semantic mainline starts new task modules from fresh "
                "initialization; --init-trainable is forbidden"
            )
    elif args.semantic_cache_root:
        raise MacroContractError(
            "--semantic-cache-root requires cached_macro_soft_token mode"
        )
    if args.semantic_input_mode == "learned_null_macro_token":
        if args.token_cache_root:
            raise MacroContractError(
                "learned-null E must not receive --token-cache-root"
            )
        if args.init_trainable:
            raise MacroContractError(
                "the retrained E control requires fresh initialization"
            )

    rank, world, local_rank = init_distributed()
    device = torch.device(
        f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu"
    )
    if (
        args.online_backbone_type == "qwen_lora"
        and not args.tiny_backbone
        and device.type != "cuda"
    ):
        raise RuntimeError(
            "real Qwen macro training requires CUDA; use --tiny-backbone only "
            "for contract/training-loop smoke"
        )
    if device.type == "cuda" and not bool(args.allow_cudnn_sdpa):
        # torch 2.12/cuDNN on H20 reproducibly returned NaN from
        # ScaledDotProductCudnnAttentionBackward0 for a finite c08 batch.  Keep
        # the SDPA model interface, but let PyTorch select Flash or the
        # memory-efficient implementation instead of the unstable cuDNN path.
        torch.backends.cuda.enable_cudnn_sdp(False)
    is_main = rank == 0
    torch.manual_seed(int(args.seed) + rank)
    random.seed(int(args.seed) + rank)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model, local_files_only=not bool(args.allow_download),
    )
    tokenizer_size = len(tokenizer)
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise MacroContractError("tokenizer has neither pad nor EOS token")
        tokenizer.pad_token = tokenizer.eos_token
    if len(tokenizer) != tokenizer_size:
        raise MacroContractError("setting padding unexpectedly changed vocabulary")

    cores = parse_csv_ints(args.cores)
    workloads = parse_csv_strings(args.workloads)
    train_workload_roles = parse_csv_strings(args.train_workload_roles)
    validation_workload_roles = parse_csv_strings(args.validation_workload_roles)
    train_sources = load_macro_sources(
        args.manifest,
        args.static_manifest,
        split=args.train_split,
        cores=cores,
        workloads=workloads,
        workload_roles=train_workload_roles,
        max_sources=int(args.max_train_sources),
        partition_override="train",
    )
    validation_sources = load_macro_sources(
        args.manifest,
        args.static_manifest,
        split=args.validation_split,
        cores=cores,
        workloads=workloads,
        workload_roles=validation_workload_roles,
        max_sources=int(args.max_validation_sources),
        partition_override=(
            "validation" if args.validation_split == args.train_split
            else None
        ),
    )
    train_dataset = MacroV29SequenceDataset(
        train_sources,
        tokenizer,
        semantic_variant=text_variant_for(args.semantic_variant),
        semantic_input_mode=str(args.semantic_input_mode),
        sequence_length=int(args.sequence_length),
        sequence_stride=int(args.sequence_stride),
        max_tokens=int(args.max_tokens),
        token_cache_root=(args.token_cache_root or None),
        semantic_cache_root=(args.semantic_cache_root or None),
    )
    validation_dataset = MacroV29SequenceDataset(
        validation_sources,
        tokenizer,
        semantic_variant=text_variant_for(args.semantic_variant),
        semantic_input_mode=str(args.semantic_input_mode),
        sequence_length=int(args.sequence_length),
        sequence_stride=int(args.sequence_stride),
        max_tokens=int(args.max_tokens),
        token_cache_root=(args.token_cache_root or None),
        semantic_cache_root=(args.semantic_cache_root or None),
    )
    if world > 1 and bool(args.core_count_bucketed_sampler):
        train_sampler = CoreCountBucketedDistributedSampler(
            train_dataset,
            num_replicas=world,
            rank=rank,
            seed=int(args.seed),
        )
    elif world > 1:
        train_sampler = DistributedSampler(
            train_dataset, num_replicas=world, rank=rank,
            shuffle=True, drop_last=False, seed=int(args.seed),
        )
    else:
        train_sampler = None
    train_generator = torch.Generator()
    train_generator.manual_seed(int(args.seed))
    validation_sampler = DistributedEvalSampler(
        validation_dataset,
        num_replicas=world,
        rank=rank,
        indices=interleaved_trace_indices(
            validation_dataset.sample_trace_ids, seed=int(args.seed),
        ),
    )
    collator = partial(
        collate_macro_sequences, pad_token_id=int(tokenizer.pad_token_id),
    )
    train_loader = DataLoader(
        train_dataset,
        batch_size=int(args.batch_size),
        sampler=train_sampler,
        shuffle=train_sampler is None,
        num_workers=int(args.num_workers),
        pin_memory=device.type == "cuda",
        collate_fn=collator,
        drop_last=False,
        persistent_workers=int(args.num_workers) > 0,
        generator=train_generator,
    )
    validation_loader = DataLoader(
        validation_dataset,
        batch_size=int(args.batch_size),
        sampler=validation_sampler,
        shuffle=False,
        num_workers=int(args.num_workers),
        pin_memory=device.type == "cuda",
        collate_fn=collator,
        drop_last=False,
        persistent_workers=int(args.num_workers) > 0,
    )

    semantic_cache_contract: Dict[str, Any] = {}
    if args.semantic_input_mode == "cached_macro_soft_token":
        if train_dataset.semantic_cache is None or validation_dataset.semantic_cache is None:
            raise MacroContractError("semantic dataset lost its cache source")
        semantic_cache_contract = train_dataset.semantic_cache.contract
        if validation_dataset.semantic_cache.contract != semantic_cache_contract:
            raise MacroContractError("train/validation semantic cache contracts differ")
        args.semantic_dim = int(semantic_cache_contract["semantic_dim"])
        args.anchor_policy = str(semantic_cache_contract["anchor_policy"])
        cache_anchor_dim = int(semantic_cache_contract["anchor_dim"])
        if (
            args.online_backbone_type == "causal_transformer"
            and int(args.online_input_dim) != cache_anchor_dim
        ):
            raise MacroContractError(
                f"--online-input-dim={args.online_input_dim} must equal "
                f"semantic cache anchor_dim={cache_anchor_dim}"
            )
        if not bool(args.tiny_backbone):
            cache_manifest = train_dataset.semantic_cache.manifest
            if bool(cache_manifest.get("smoke_partial_cache", False)):
                raise MacroContractError(
                    "partial smoke semantic caches cannot train a real backbone"
                )
            if "SYNTHETIC" in str(
                cache_manifest["semantic_encoder_model"]
            ).upper():
                raise MacroContractError(
                    "synthetic semantic caches cannot train a real backbone"
                )
            selection = cache_manifest.get("selection_contract")
            if not isinstance(selection, Mapping):
                raise MacroContractError(
                    "real semantic training requires a cache selection_contract"
                )
            cached_cores = {
                int(value) for value in selection.get("cores", [])
            }
            if not cores.issubset(cached_cores):
                raise MacroContractError(
                    f"semantic cache cores {sorted(cached_cores)} do not cover "
                    f"training cores {sorted(cores)}"
                )
    elif args.semantic_input_mode == "learned_null_macro_token":
        args.semantic_dim = int(args.online_input_dim)
        args.anchor_policy = "learned-null-shared-macro-token-v1"
    else:
        args.semantic_dim = 0
        args.anchor_policy = "none"

    model = build_timing_model(args, tokenizer).to(device)
    core_before_ddp = model
    online_capacity_parameters = int(sum(
        parameter.numel()
        for name, parameter in core_before_ddp.named_parameters()
        if parameter.requires_grad and (
            name.startswith("backbone.")
            or name.startswith("online_transformer.")
            or name.startswith("asm_projection.")
        )
    ))
    a_reference_online_capacity = 9_309_568
    online_capacity_relative_difference = abs(
        online_capacity_parameters - a_reference_online_capacity
    ) / a_reference_online_capacity
    if (
        args.online_backbone_type == "causal_transformer"
        and not bool(args.tiny_backbone)
        and online_capacity_relative_difference >= 0.05
    ):
        raise MacroContractError(
            "ordinary online backbone violates the A/B/E 5% capacity match: "
            f"observed={online_capacity_parameters} "
            f"A_reference={a_reference_online_capacity} "
            f"relative_difference={online_capacity_relative_difference:.6f}"
        )
    if (
        args.semantic_input_mode == "cached_macro_soft_token"
        and int(semantic_cache_contract["anchor_dim"])
        != int(core_before_ddp.config.d_llm)
    ):
        raise MacroContractError(
            f"semantic anchor_dim={semantic_cache_contract['anchor_dim']} "
            f"!= online backbone hidden={core_before_ddp.config.d_llm}"
        )
    checkpoint_contract = {
        "base_model": str(args.base_model),
        "experiment_variant": str(args.experiment_variant),
        "semantic_input_mode": str(args.semantic_input_mode),
        "semantic_source": str(args.semantic_source),
        "semantic_dim": int(args.semantic_dim),
        "anchor_policy": str(args.anchor_policy),
        "semantic_variant": str(args.semantic_variant),
        "tiny_backbone": bool(args.tiny_backbone),
        "tiny_width": int(args.tiny_width),
        "d_model": int(args.d_model if not args.tiny_backbone else 64),
        "d_macro": int(args.d_model if not args.tiny_backbone else 64),
        "d_field": int(args.d_field if not args.tiny_backbone else 4),
        "n_heads": int(args.n_heads if not args.tiny_backbone else 4),
        "architecture_schema": "macro-v29-soft-cross-core-1",
        "core_mixer_mode": str(args.core_mixer_mode),
        "core_mixer": "full-cross-core-macro-sdpa",
        "cross_layers": 1,
        "cross_ffn_multiplier": int(args.cross_ffn_multiplier),
        "cross_target_block": int(args.cross_target_block),
        "cross_gate_init": float(args.cross_gate_init),
        "backbone_core_chunk_size": int(args.backbone_core_chunk_size),
        "backbone_chunk_checkpoint": bool(args.backbone_chunk_checkpoint),
        "tokenizer_size": int(tokenizer_size),
        "seed": int(args.seed),
        "online_backbone_model": (
            str(args.base_model)
            if args.online_backbone_type == "qwen_lora"
            else "ordinary-causal-macro-transformer-v1"
        ),
        "online_backbone_type": str(args.online_backbone_type),
        "online_transformer_layers": int(args.online_transformer_layers),
        "online_transformer_heads": int(args.online_transformer_heads),
        "online_transformer_ffn_multiplier": int(
            args.online_transformer_ffn_multiplier
        ),
        "online_input_dim": int(core_before_ddp.config.d_llm),
        "online_capacity_parameters": online_capacity_parameters,
        "online_capacity_reference_A": a_reference_online_capacity,
        "online_capacity_relative_difference": (
            online_capacity_relative_difference
        ),
        "online_backbone_revision": str(getattr(
            model.backbone, "_macro_base_model_commit", "not-applicable",
        )),
        "online_backbone_config_fingerprint": str(getattr(
            model.backbone,
            "_macro_base_config_fingerprint",
            "not-applicable",
        )),
        "online_backbone_input_unit": (
            "macro" if args.semantic_input_mode in {
                "cached_macro_soft_token", "learned_null_macro_token",
            } else "native_token"
        ),
        "task_init_source": "fresh",
        "init_timing_checkpoint": None,
        "supervision_mode": "real_labels_only",
        "distillation_enabled": False,
        "K_macro": 256,
        "target_stride_macro": int(args.target_stride_macro),
        **semantic_cache_contract,
    }
    initialized_from = None
    if args.init_trainable:
        initialized_from = load_trainable(
            model,
            args.init_trainable,
            expected_contract=checkpoint_contract,
        )
    if world > 1:
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[local_rank],
            output_device=local_rank,
            find_unused_parameters=False,
        )
    # Model construction (especially random-init controls) may consume a
    # different amount of RNG.  Reset stochastic training RNG so dropout and
    # other sampling remain variant-matched.
    torch.manual_seed(int(args.seed) + rank)
    core = model.module if hasattr(model, "module") else model
    head_parameters = []
    lora_parameters = []
    ordinary_backbone_parameters = []
    for name, parameter in core.named_parameters():
        if not parameter.requires_grad:
            continue
        if name.startswith("backbone."):
            lora_parameters.append(parameter)
        elif name.startswith("online_transformer."):
            ordinary_backbone_parameters.append(parameter)
        else:
            head_parameters.append(parameter)
    groups = [{"params": head_parameters, "lr": float(args.lr_head)}]
    if lora_parameters:
        groups.append({"params": lora_parameters, "lr": float(args.lr_lora)})
    if ordinary_backbone_parameters:
        groups.append({
            "params": ordinary_backbone_parameters,
            "lr": float(args.lr_online_backbone),
        })
    optimizer = torch.optim.AdamW(
        groups, weight_decay=float(args.weight_decay),
    )
    schedule_steps = (
        int(args.lr_schedule_steps)
        if int(args.lr_schedule_steps) > 0 else int(args.max_steps)
    )
    if schedule_steps < int(args.max_steps):
        raise ValueError("--lr-schedule-steps cannot be smaller than --max-steps")
    warmup = max(1, int(schedule_steps * args.warmup_fraction))

    def lr_factor(step: int) -> float:
        if step < warmup:
            return float(step + 1) / warmup
        progress = (step - warmup) / max(1, schedule_steps - warmup)
        return 0.5 * (1.0 + math.cos(math.pi * min(1.0, progress)))

    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_factor)
    metadata = {
        **vars(args),
        "dataset_schema": "global-time-v29-macro-native-2",
        "semantic_run_contract": "macro-v29-semantic-run-v4",
        "semantic_text_variant": (
            text_variant_for(args.semantic_variant)
        ),
        "semantic_mode": core.config.semantic_mode,
        "pretrained_backbone": args.online_backbone_type == "qwen_lora",
        "task_initialization": "fresh-real-label-supervised-no-distillation",
        "checkpoint_contract": checkpoint_contract,
        "initialized_from": initialized_from,
        "tokenizer_size": tokenizer_size,
        "tokenizer_fingerprint": tokenizer_fingerprint(tokenizer),
        "base_model_commit": getattr(
            core.backbone, "_macro_base_model_commit", "not-applicable",
        ),
        "backbone_config_fingerprint": getattr(
            core.backbone, "_macro_base_config_fingerprint", "not-applicable",
        ),
        "parameter_groups": {
            "timing_head": int(sum(p.numel() for p in head_parameters)),
            "qwen_lora": int(sum(p.numel() for p in lora_parameters)),
            "ordinary_online_transformer": int(sum(
                p.numel() for p in ordinary_backbone_parameters
            )),
        },
        "train_sources": len(train_sources),
        "validation_sources": len(validation_sources),
        "train_sequences": len(train_dataset),
        "validation_sequences": len(validation_dataset),
        "world_size": world,
        "device": str(device),
        "cudnn_sdpa_enabled": (
            bool(torch.backends.cuda.cudnn_sdp_enabled())
            if device.type == "cuda" else None
        ),
        "train_order_policy": (
            "core-count-bucketed-ddp-v1"
            if bool(args.core_count_bucketed_sampler) and world > 1
            else "seeded-random-v1"
        ),
        "trainable_init_policy": "isolated-seed-domains-v1",
        "validation_order_policy": "seeded-within-trace-round-robin-v2",
        "memory_optimization_schema": (
            "qwen-row-checkpoint-cross-target-block-v1"
            if args.online_backbone_type == "qwen_lora"
            else "ordinary-causal-sdpa-cross-target-block-v1"
        ),
        "trainable_parameters": int(sum(
            parameter.numel() for parameter in core.parameters()
            if parameter.requires_grad
        )),
    }
    if is_main:
        (output / "run.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        print(f"[macro train] {json.dumps(metadata, sort_keys=True)}", flush=True)

    global_step = 0
    started = time.perf_counter()
    stop = False
    epoch = 0
    last_validation: Dict[str, Any] | None = None
    last_train_losses: Dict[str, float] | None = None
    trainable_parameters = [
        parameter for parameter in core.parameters() if parameter.requires_grad
    ]
    if device.type == "cuda":
        torch.cuda.reset_peak_memory_stats(device)
    while not stop and global_step < int(args.max_steps):
        if train_sampler is not None:
            train_sampler.set_epoch(epoch)
        for raw in train_loader:
            batch = tensor_batch(raw, device)
            model.train()
            optimizer.zero_grad(set_to_none=True)
            manual_gradient_sync = (
                dist.is_initialized()
                and int(args.diagnose_gradients_from_step) >= 0
                and global_step >= int(args.diagnose_gradients_from_step)
            )
            detect_anomaly = (
                int(args.detect_anomaly_from_step) >= 0
                and global_step >= int(args.detect_anomaly_from_step)
                and int(args.detect_anomaly_rank) in {-1, rank}
            )
            sync_context = model.no_sync() if manual_gradient_sync else nullcontext()
            anomaly_context = (
                torch.autograd.detect_anomaly(check_nan=True)
                if detect_anomaly else nullcontext()
            )
            with sync_context, anomaly_context:
                with autocast_context(device, args.dtype):
                    predictions = model(batch)
                    losses = macro_v29_loss(predictions, batch)
                loss_vector = torch.stack([
                    value.detach().float() for value in losses.values()
                ])
                local_forward_finite = bool(
                    torch.isfinite(loss_vector).all().item()
                )
                if not all_ranks_true(local_forward_finite, device):
                    details = (
                        batch_diagnostic(
                            raw, batch, rank=rank, losses=losses,
                            predictions=predictions,
                        )
                        if not local_forward_finite else None
                    )
                    raise_distributed_nonfinite(
                        f"forward/loss at step {global_step}", details, rank=rank,
                    )
                losses["total"].backward()

            if manual_gradient_sync:
                gradients = [
                    parameter.grad for parameter in trainable_parameters
                    if parameter.grad is not None
                ]
                local_norm = torch.nn.utils.get_total_norm(
                    gradients, norm_type=2.0,
                    error_if_nonfinite=False,
                )
                local_gradient_finite = bool(torch.isfinite(local_norm).item())
                if not all_ranks_true(local_gradient_finite, device):
                    details = None
                    if not local_gradient_finite:
                        details = batch_diagnostic(
                            raw, batch, rank=rank, losses=losses,
                            predictions=predictions,
                        )
                        details["local_gradient_norm"] = float(local_norm.item())
                        details["nonfinite_gradients"] = (
                            nonfinite_gradient_details(core)
                        )
                    raise_distributed_nonfinite(
                        f"local backward gradients at step {global_step}",
                        details,
                        rank=rank,
                    )
                for parameter in trainable_parameters:
                    if parameter.grad is None:
                        continue
                    dist.all_reduce(parameter.grad, op=dist.ReduceOp.SUM)
                    parameter.grad.div_(world)

            gradients = [
                parameter.grad for parameter in trainable_parameters
                if parameter.grad is not None
            ]
            grad_norm = torch.nn.utils.get_total_norm(
                gradients, norm_type=2.0,
                error_if_nonfinite=False,
            )
            local_gradient_finite = bool(torch.isfinite(grad_norm).item())
            if not all_ranks_true(local_gradient_finite, device):
                details = None
                if not local_gradient_finite:
                    details = batch_diagnostic(
                        raw, batch, rank=rank, losses=losses,
                        predictions=predictions,
                    )
                    details["gradient_norm"] = float(grad_norm.item())
                    details["nonfinite_gradients"] = nonfinite_gradient_details(core)
                optimizer.zero_grad(set_to_none=True)
                raise_distributed_nonfinite(
                    f"synchronized backward gradients at step {global_step}",
                    details,
                    rank=rank,
                )
            torch.nn.utils.clip_grads_with_norm_(
                trainable_parameters,
                float(args.gradient_clip),
                grad_norm,
            )
            optimizer.step()
            scheduler.step()
            global_step += 1
            if is_main and (
                global_step == 1 or global_step % int(args.log_every) == 0
            ):
                values = {
                    key: float(value.detach()) for key, value in losses.items()
                }
                last_train_losses = values
                print(
                    f"[macro train step={global_step}] losses={values} "
                    f"grad_norm={float(grad_norm.detach())} "
                    f"gpu_peak_allocated_gib={float(torch.cuda.max_memory_allocated(device) / (1024 ** 3)) if device.type == 'cuda' else 0.0:.3f} "
                    f"seconds={time.perf_counter() - started:.1f}",
                    flush=True,
                )
            evaluate_now = (
                global_step % int(args.eval_every) == 0
                or global_step >= int(args.max_steps)
                or bool(args.dry_run)
            )
            if evaluate_now:
                metrics = reduced_validation(
                    model,
                    validation_loader,
                    device,
                    dtype_name=args.dtype,
                    max_batches=(1 if args.dry_run else int(args.eval_batches)),
                )
                last_validation = metrics
                if is_main:
                    print(
                        f"[macro validation step={global_step}] {metrics}",
                        flush=True,
                    )
            if is_main and int(args.save_every) > 0 and (
                global_step % int(args.save_every) == 0
            ):
                save_trainable(
                    model, output, global_step, contract=checkpoint_contract,
                )
            if bool(args.dry_run) or global_step >= int(args.max_steps):
                stop = True
                break
        epoch += 1
    peak_memory = torch.tensor(
        [
            float(torch.cuda.max_memory_allocated(device))
            if device.type == "cuda" else 0.0
        ],
        dtype=torch.float64,
        device=device,
    )
    if dist.is_initialized():
        dist.all_reduce(peak_memory, op=dist.ReduceOp.MAX)
    peak_memory_gib = float(peak_memory.item() / (1024 ** 3))
    if is_main:
        checkpoint = save_trainable(
            model, output, global_step, contract=checkpoint_contract,
        )
        final = {
            "status": "PASS",
            "kind": (
                "tiny-training-loop-contract-smoke"
                if args.tiny_backbone else "macro-v29-abe-training"
            ),
            "steps": global_step,
            "elapsed_s": time.perf_counter() - started,
            "checkpoint": str(checkpoint),
            "semantic_variant": str(args.semantic_variant),
            "validation_split": str(args.validation_split),
            "validation": last_validation,
            "last_train_losses": last_train_losses,
            "checkpoint_contract": checkpoint_contract,
            "initialized_from": initialized_from,
            "max_rank_peak_allocated_gib": peak_memory_gib,
        }
        (output / "final_report.json").write_text(
            json.dumps(final, indent=2, sort_keys=True) + "\n"
        )
        print(f"[macro train done] {json.dumps(final, sort_keys=True)}", flush=True)
    if dist.is_initialized():
        dist.barrier()
        dist.destroy_process_group()
    return 0


if __name__ == "__main__":
    sys.exit(main())
