#!/usr/bin/env python3
"""Train the frozen-v29 B2 bridge or detachable Qwen LoRA with only L_v29.

This is a deliberately separate adapter-finetuning lineage.  It never writes
the immutable v29 checkpoint and never includes L_static/L_sem.  In LoRA mode
it uses an exact two-pass Gradient Cache: v29 first differentiates compact
semantic leaves, then Qwen microbatches are replayed with those leaf gradients.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import numpy as np
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader


REPO = Path(__file__).resolve().parents[1]
TCSIM_ROOT = Path(os.environ.get("TCSIM_ROOT", "/data00/yinhaolang/TCSim"))
for root in (REPO, TCSIM_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

from tcsim.utils.config import TCSimConfig  # noqa: E402
from tcsim.v29 import train as v29_train  # noqa: E402
from tcsim.v29.losses import compute_v29_losses  # noqa: E402

from model.tcsim_v29_semantic import (  # noqa: E402
    BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
    BOUNDED_RESIDUAL_FUSION_ARCHITECTURES,
    LEGACY_FUSION_ARCHITECTURE,
    MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE,
    RESIDUAL_FUSION_ARCHITECTURES,
    SUPPORTED_FUSION_ARCHITECTURES,
    build_semantic_model,
)
from scripts.train_tcsim_v29_b2e2 import (  # noqa: E402
    _manifest_sources,
    _validate_separation,
)
from train.macro_v29_dataset import CachedSemanticSource  # noqa: E402
from train.tcsim_v29_exact_resume import (  # noqa: E402
    ExactResumeWeightedRandomSampler,
)
from train.tcsim_v29_semantic_dataset import (  # noqa: E402
    TCSimV29SemanticDataset,
    collate_tcsim_v29_semantic,
)


CHECKPOINT_SCHEMA = "tcsim-v29-frozen-base-lora-v29-checkpoint-1"
RUN_SCHEMA = "tcsim-v29-frozen-base-lora-v29-run-1"
RANK_STATE_SCHEMA = "tcsim-v29-frozen-base-lora-v29-rank-state-1"
MODES = frozenset({"bridge-warmup", "frozen", "lora"})


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str,
    ).encode()).hexdigest()


def _atomic_torch_save(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        torch.save(value, temporary)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(value: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _torch_load(path: str | Path, device: Any = "cpu") -> Any:
    try:
        return torch.load(
            str(path), map_location=device, weights_only=False, mmap=True,
        )
    except (TypeError, RuntimeError):
        try:
            return torch.load(
                str(path), map_location=device, weights_only=False,
            )
        except TypeError:
            return torch.load(str(path), map_location=device)


def _raw(module: torch.nn.Module) -> torch.nn.Module:
    return module.module if isinstance(module, DDP) else module


def _move_batch(batch: Dict[str, Any], device: torch.device) -> Dict[str, Any]:
    return v29_train._to_device(batch, device)


def _loss_kwargs(config: TCSimConfig) -> Dict[str, Any]:
    return v29_train._loss_kwargs(config)


@torch.no_grad()
def _assert_residual_identity(
    model: torch.nn.Module,
    batch: Dict[str, Any],
    device: torch.device,
) -> None:
    """Fail closed unless a fresh residual adapter is exactly base v29."""

    model.eval()
    moved = _move_batch(batch, device)
    base_batch = {
        key: value for key, value in moved.items()
        if not key.startswith("semantic_")
    }
    base = model.backbone(base_batch)
    adapted = model(moved)
    if base.keys() != adapted.keys():
        raise RuntimeError("residual identity changed the v29 output contract")
    failures = []
    max_abs = 0.0
    for key in base:
        left = base[key]
        right = adapted[key]
        if left.is_floating_point():
            difference = float((left - right).abs().max())
            max_abs = max(max_abs, difference)
        else:
            difference = 0.0 if torch.equal(left, right) else float("inf")
        if not torch.equal(left, right):
            failures.append(f"{key}:{difference:.9g}")
    if failures:
        raise RuntimeError(
            "fresh residual adapter is not exact v29 identity: "
            + ", ".join(failures)
        )
    print(
        f"[LoRA-v29 residual identity PASS] max_abs={max_abs:.9g}",
        flush=True,
    )


def _select_cores(sources: Sequence[Any], cores: set[int]) -> List[Any]:
    selected = [
        source for source in sources
        if isinstance(source, Mapping) and int(source.get("n_cores", -1)) in cores
    ]
    if not selected:
        raise RuntimeError(f"no sources selected for cores={sorted(cores)}")
    return selected


def _tokenize_chunks(
    tokenizer: Any,
    prompts: Sequence[str],
    *,
    microbatch: int,
    max_prompt_tokens: int,
    token_cache: Dict[str, tuple[int, ...]] | None = None,
) -> tuple[List[Dict[str, torch.Tensor]], int]:
    chunks: List[Dict[str, torch.Tensor]] = []
    total_tokens = 0
    for begin in range(0, len(prompts), int(microbatch)):
        texts = list(prompts[begin:begin + int(microbatch)])
        if token_cache is None:
            encoded = tokenizer(
                texts,
                padding=True,
                add_special_tokens=False,
                truncation=False,
                return_tensors="pt",
            )
        else:
            rows: List[tuple[int, ...]] = []
            for text in texts:
                ids = token_cache.get(text)
                if ids is None:
                    result = tokenizer(
                        text,
                        padding=False,
                        add_special_tokens=False,
                        truncation=False,
                    )
                    ids = tuple(int(value) for value in result["input_ids"])
                    if not ids:
                        raise RuntimeError("semantic tokenizer returned no IDs")
                    token_cache[text] = ids
                rows.append(ids)
            width = max(len(row) for row in rows)
            input_ids = torch.full(
                (len(rows), width),
                int(tokenizer.pad_token_id),
                dtype=torch.long,
            )
            attention_mask = torch.zeros_like(input_ids)
            for row_index, ids in enumerate(rows):
                count = len(ids)
                input_ids[row_index, :count] = torch.tensor(
                    ids, dtype=torch.long,
                )
                attention_mask[row_index, :count] = 1
            encoded = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }
        lengths = encoded["attention_mask"].sum(dim=1)
        if int(lengths.max()) > int(max_prompt_tokens):
            raise RuntimeError(
                f"online semantic prompt exceeds {max_prompt_tokens} tokens"
            )
        total_tokens += int(lengths.sum())
        chunks.append({key: value for key, value in encoded.items()})
    if not chunks:
        raise RuntimeError("v29 batch has no semantic prompts")
    return chunks, total_tokens


def _encode_chunk(
    encoder: torch.nn.Module,
    chunk: Mapping[str, torch.Tensor],
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
) -> torch.Tensor:
    encoded = {key: value.to(device, non_blocking=True) for key, value in chunk.items()}
    mask = encoded["attention_mask"]
    position_ids = mask.long().cumsum(dim=-1) - 1
    position_ids.masked_fill_(mask == 0, 0)
    with torch.autocast(
        device_type=device.type,
        dtype=amp_dtype,
        enabled=amp_dtype is not None,
    ):
        output = encoder(
            **encoded,
            position_ids=position_ids,
            output_hidden_states=False,
            use_cache=False,
        )
        hidden = output.last_hidden_state
        positions = torch.arange(mask.shape[1], device=device).unsqueeze(0)
        last = (positions * mask.long()).max(dim=1).values
        return hidden[
            torch.arange(hidden.shape[0], device=device), last,
        ]


def gradient_cache_leaf_embeddings(
    encoder: torch.nn.Module,
    chunks: Sequence[Mapping[str, torch.Tensor]],
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
) -> torch.Tensor:
    """First GC pass: return detached semantic leaves in prompt order."""

    parts: List[torch.Tensor] = []
    with torch.no_grad():
        for chunk in chunks:
            parts.append(_encode_chunk(
                encoder, chunk, device, amp_dtype=amp_dtype,
            ))
    return torch.cat(parts, dim=0).detach().requires_grad_(True)


def gradient_cache_replay_backward(
    encoder: torch.nn.Module,
    chunks: Sequence[Mapping[str, torch.Tensor]],
    embedding_gradient: torch.Tensor,
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
) -> None:
    """Second GC pass: replay chunks and inject the exact leaf gradient."""

    offset = 0
    for chunk in chunks:
        count = int(chunk["input_ids"].shape[0])
        output = _encode_chunk(encoder, chunk, device, amp_dtype=amp_dtype)
        gradient = embedding_gradient[offset:offset + count].to(output.dtype)
        torch.autograd.backward(output, gradient)
        offset += count
    if offset != int(embedding_gradient.shape[0]):
        raise RuntimeError("Gradient Cache prompt/gradient count mismatch")


@dataclass
class ReplayLeaf:
    chunk: Mapping[str, torch.Tensor]
    leaf: torch.Tensor
    start: int
    end: int
    retained_output: torch.Tensor | None
    producer_stream: torch.cuda.Stream | None


@dataclass(frozen=True)
class CanonicalGroupWave:
    """Several independent canonical groups packed only on the batch axis."""

    encoded: Mapping[str, torch.Tensor]
    spans: tuple[tuple[int, int], ...]


def pack_canonical_group_waves(
    chunks: Sequence[Mapping[str, torch.Tensor]],
    *,
    parallel_groups: int,
    pad_token_id: int,
) -> List[CanonicalGroupWave]:
    """Pack independent groups without ever sharing an attention sequence.

    Rows are concatenated on the batch axis.  Sequence padding is extended to
    the widest group in each wave, while every row keeps its original mask and
    position IDs.  `spans` is the lossless inverse mapping back to canonical
    group order.
    """

    factor = int(parallel_groups)
    if factor <= 0:
        raise ValueError("parallel_groups must be positive")
    waves: List[CanonicalGroupWave] = []
    for begin in range(0, len(chunks), factor):
        groups = list(chunks[begin:begin + factor])
        if not groups:
            continue
        if any(set(group) != {"input_ids", "attention_mask"} for group in groups):
            raise RuntimeError("canonical packing requires input_ids/attention_mask")
        width = max(int(group["input_ids"].shape[1]) for group in groups)
        ids_parts: List[torch.Tensor] = []
        mask_parts: List[torch.Tensor] = []
        spans: List[tuple[int, int]] = []
        offset = 0
        for group in groups:
            input_ids = group["input_ids"]
            attention_mask = group["attention_mask"]
            if input_ids.ndim != 2 or attention_mask.shape != input_ids.shape:
                raise RuntimeError("invalid canonical group tensor shape")
            extra = width - int(input_ids.shape[1])
            if extra:
                input_ids = torch.nn.functional.pad(
                    input_ids, (0, extra), value=int(pad_token_id),
                )
                attention_mask = torch.nn.functional.pad(
                    attention_mask, (0, extra), value=0,
                )
            count = int(input_ids.shape[0])
            ids_parts.append(input_ids)
            mask_parts.append(attention_mask)
            spans.append((offset, offset + count))
            offset += count
        waves.append(CanonicalGroupWave(
            encoded={
                "input_ids": torch.cat(ids_parts, dim=0),
                "attention_mask": torch.cat(mask_parts, dim=0),
            },
            spans=tuple(spans),
        ))
    return waves


def encode_canonical_group_waves(
    encoder: torch.nn.Module,
    waves: Sequence[CanonicalGroupWave],
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
) -> List[torch.Tensor]:
    """Encode packed waves and restore exact canonical group ordering."""

    outputs: List[torch.Tensor] = []
    for wave in waves:
        packed = _encode_chunk(
            encoder, wave.encoded, device, amp_dtype=amp_dtype,
        )
        outputs.extend(packed[start:end] for start, end in wave.spans)
    return outputs


def encode_independent_stream_groups(
    encoder: torch.nn.Module,
    chunks: Sequence[Mapping[str, torch.Tensor]],
    streams: Sequence[torch.cuda.Stream],
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
    track_gradients: Sequence[bool] | None = None,
) -> tuple[List[torch.Tensor], List[torch.cuda.Stream | None]]:
    """Run unchanged canonical groups concurrently on independent streams.

    Unlike batch-axis packing, every Qwen call retains its original batch and
    sequence shape.  Each wave is synchronized before its results are exposed,
    so callers can restore canonical ordering without cross-stream races.
    """

    if track_gradients is None:
        track_gradients = [torch.is_grad_enabled()] * len(chunks)
    if len(track_gradients) != len(chunks):
        raise ValueError("track_gradients/chunks length mismatch")
    if not streams:
        outputs: List[torch.Tensor] = []
        for chunk, tracked in zip(chunks, track_gradients):
            with torch.set_grad_enabled(bool(tracked)):
                outputs.append(_encode_chunk(
                    encoder, chunk, device, amp_dtype=amp_dtype,
                ))
        return outputs, [None] * len(outputs)
    if device.type != "cuda":
        raise RuntimeError("canonical CUDA streams require a CUDA device")

    outputs: List[torch.Tensor | None] = [None] * len(chunks)
    producers: List[torch.cuda.Stream | None] = [None] * len(chunks)
    factor = len(streams)
    # Model parameters and inputs are prepared on the current stream.  Make
    # every worker wait once without forcing a host-side global synchronize.
    current = torch.cuda.current_stream(device)
    for stream in streams:
        stream.wait_stream(current)
    for begin in range(0, len(chunks), factor):
        active = chunks[begin:begin + factor]
        for local, chunk in enumerate(active):
            stream = streams[local]
            with torch.cuda.stream(stream):
                with torch.set_grad_enabled(bool(track_gradients[begin + local])):
                    outputs[begin + local] = _encode_chunk(
                        encoder, chunk, device, amp_dtype=amp_dtype,
                    )
            producers[begin + local] = stream
        for stream in streams[:len(active)]:
            stream.synchronize()
    if any(value is None for value in outputs):
        raise RuntimeError("stream encoder failed to produce every group")
    return [value for value in outputs if value is not None], producers


def hybrid_gradient_cache_embeddings(
    encoder: torch.nn.Module,
    chunks: Sequence[Mapping[str, torch.Tensor]],
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
    retain_groups: int,
    streams: Sequence[torch.cuda.Stream] = (),
) -> tuple[torch.Tensor, List[ReplayLeaf], int]:
    """Build exact leaves while retaining a suffix of canonical groups.

    The established Gradient Cache implementation accumulates LoRA gradients
    in canonical-group order.  Every group therefore remains a detached v29
    leaf and is backpropagated explicitly in that same order.  Retention only
    decides whether that exact Qwen graph is kept or recomputed; it must not
    alter forward values or floating-point gradient accumulation order.
    """

    total = len(chunks)
    requested = int(retain_groups)
    retained = total if requested < 0 else min(requested, total)
    cutoff = total - retained
    parts: List[torch.Tensor] = []
    metadata: List[tuple[
        Mapping[str, torch.Tensor], int, int, torch.Tensor | None,
        torch.cuda.Stream | None,
    ]] = []
    offset = 0
    tracked = [index >= cutoff for index in range(total)]
    values, producers = encode_independent_stream_groups(
        encoder,
        chunks,
        streams,
        device,
        amp_dtype=amp_dtype,
        track_gradients=tracked,
    )
    for index, (chunk, value, producer) in enumerate(zip(
        chunks, values, producers,
    )):
        retained_output = value if tracked[index] else None
        count = int(value.shape[0])
        parts.append(value)
        metadata.append((
            chunk, offset, offset + count, retained_output, producer,
        ))
        offset += count
    # Preserve the established Gradient Cache topology exactly: v29 receives
    # one leaf made by detaching the concatenated semantic tensor, not a cat of
    # independently detached per-group leaves.
    leaf = torch.cat(parts, dim=0).detach().requires_grad_(True)
    replay = [
        ReplayLeaf(
            chunk=chunk,
            leaf=leaf,
            start=start,
            end=end,
            retained_output=retained_output,
            producer_stream=producer if retained_output is not None else None,
        )
        for chunk, start, end, retained_output, producer in metadata
    ]
    return leaf, replay, retained


def hybrid_gradient_cache_replay_backward(
    encoder: torch.nn.Module,
    replay: Sequence[ReplayLeaf],
    device: torch.device,
    *,
    amp_dtype: torch.dtype | None,
    streams: Sequence[torch.cuda.Stream] = (),
) -> None:
    """Backpropagate every canonical group in the established GC order."""

    factor = max(1, len(streams))
    current = (
        torch.cuda.current_stream(device) if streams and device.type == "cuda"
        else None
    )
    for begin in range(0, len(replay), factor):
        wave = list(replay[begin:begin + factor])
        missing = [entry for entry in wave if entry.retained_output is None]
        fresh: dict[int, tuple[torch.Tensor, torch.cuda.Stream | None]] = {}
        if missing:
            outputs, producers = encode_independent_stream_groups(
                encoder,
                [entry.chunk for entry in missing],
                streams,
                device,
                amp_dtype=amp_dtype,
                track_gradients=[True] * len(missing),
            )
            fresh = {
                id(entry): (output, producer)
                for entry, output, producer in zip(missing, outputs, producers)
            }
        for entry in wave:
            if entry.leaf.grad is None:
                raise RuntimeError("L_v29 did not reach a replay semantic leaf")
            gradient = entry.leaf.grad[entry.start:entry.end].detach()
            if not torch.isfinite(gradient).all():
                raise RuntimeError("non-finite replay semantic gradient")
            if entry.retained_output is None:
                output, producer = fresh[id(entry)]
            else:
                output = entry.retained_output
                producer = entry.producer_stream
            if producer is None:
                torch.autograd.backward(output, gradient.to(output.dtype))
            else:
                if current is not None:
                    producer.wait_stream(current)
                with torch.cuda.stream(producer):
                    torch.autograd.backward(output, gradient.to(output.dtype))
                # Shared LoRA gradients must never race across group streams.
                producer.synchronize()


def _load_qwen_lora(args: argparse.Namespace, device: torch.device):
    try:
        from peft import LoraConfig, TaskType, get_peft_model
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers and peft are required for LoRA mode") from exc

    tokenizer = AutoTokenizer.from_pretrained(
        args.qwen_model, local_files_only=True,
    )
    tokenizer.padding_side = "right"
    if tokenizer.pad_token_id is None:
        if tokenizer.eos_token_id is None:
            raise RuntimeError("Qwen tokenizer has no pad/eos token")
        tokenizer.pad_token = tokenizer.eos_token
    dtype = torch.bfloat16 if device.type == "cuda" else torch.float32
    base = AutoModel.from_pretrained(
        args.qwen_model,
        torch_dtype=dtype,
        attn_implementation="sdpa",
        local_files_only=True,
    )
    for parameter in base.parameters():
        parameter.requires_grad_(False)
    lora = get_peft_model(base, LoraConfig(
        r=int(args.lora_rank),
        lora_alpha=int(args.lora_alpha),
        lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        bias="none",
        task_type=TaskType.FEATURE_EXTRACTION,
    )).to(device)
    # Evaluation mode disables every base-model dropout.  LoRA parameters are
    # still differentiable, and lora_dropout is deliberately zero for replay.
    lora.eval()
    trainable = [parameter for parameter in lora.parameters() if parameter.requires_grad]
    if not trainable:
        raise RuntimeError("PEFT created no trainable LoRA parameters")
    return tokenizer, lora, trainable


def _per_parameter_average_gradients(
    parameters: Iterable[torch.nn.Parameter],
    *,
    world: int,
) -> None:
    if world <= 1:
        return
    for parameter in parameters:
        if parameter.grad is None:
            parameter.grad = torch.zeros_like(parameter)
        dist.all_reduce(parameter.grad, op=dist.ReduceOp.SUM)
        parameter.grad.div_(float(world))


def _rank_state_path(root: Path, step: int, rank: int) -> Path:
    return root / "resume_state" / f"step_{int(step):08d}" / f"rank_{rank:05d}.pt"


def _capture_rank_state(
    root: Path,
    *,
    step: int,
    rank: int,
    world: int,
    device: torch.device,
    contract_fingerprint: str,
) -> None:
    payload = {
        "schema_version": RANK_STATE_SCHEMA,
        "step": int(step),
        "rank": int(rank),
        "world": int(world),
        "contract_fingerprint": str(contract_fingerprint),
        "python_rng_state": random.getstate(),
        "numpy_rng_state": np.random.get_state(),
        "torch_cpu_rng_state": torch.get_rng_state(),
        "torch_cuda_rng_state": (
            torch.cuda.get_rng_state(device) if device.type == "cuda" else None
        ),
    }
    _atomic_torch_save(payload, _rank_state_path(root, step, rank))


def _restore_rank_state(
    resume_checkpoint: Path,
    *,
    step: int,
    rank: int,
    world: int,
    device: torch.device,
    expected_contract_fingerprint: str,
) -> None:
    path = _rank_state_path(resume_checkpoint.parent, step, rank)
    if not path.is_file():
        raise RuntimeError(f"exact rank RNG state is missing: {path}")
    payload = _torch_load(path, "cpu")
    expected = {
        "schema_version": RANK_STATE_SCHEMA,
        "step": int(step),
        "rank": int(rank),
        "world": int(world),
        "contract_fingerprint": str(expected_contract_fingerprint),
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise RuntimeError(
                f"rank resume mismatch {key}: {payload.get(key)!r} != {value!r}"
            )
    random.setstate(payload["python_rng_state"])
    np.random.set_state(payload["numpy_rng_state"])
    torch.set_rng_state(payload["torch_cpu_rng_state"])
    if device.type == "cuda":
        torch.cuda.set_rng_state(payload["torch_cuda_rng_state"], device)


def _save_adapter(qwen: torch.nn.Module, out: Path, step: int) -> str:
    destination = out / "adapters" / f"step_{int(step):08d}"
    if destination.exists():
        return str(destination)
    temporary = destination.with_name(f"{destination.name}.tmp-{os.getpid()}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    qwen.save_pretrained(temporary, safe_serialization=True)
    os.replace(temporary, destination)
    return str(destination)


def _checkpoint_payload(
    *,
    mode: str,
    step: int,
    best_validation: float,
    semantic_model: torch.nn.Module,
    bridge_optimizer: torch.optim.Optimizer,
    lora_optimizer: torch.optim.Optimizer | None,
    qwen: torch.nn.Module | None,
    contract: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    adapter_path: str | None,
) -> Dict[str, Any]:
    lora_state = None
    if qwen is not None:
        from peft import get_peft_model_state_dict

        lora_state = {
            key: value.detach().cpu()
            for key, value in get_peft_model_state_dict(qwen).items()
        }
    return {
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "mode": str(mode),
        "step": int(step),
        "best_validation": float(best_validation),
        "bridge": _raw(semantic_model).semantic_bridge.state_dict(),
        "bridge_optimizer": bridge_optimizer.state_dict(),
        "lora": lora_state,
        "lora_optimizer": (
            None if lora_optimizer is None else lora_optimizer.state_dict()
        ),
        "adapter_path": adapter_path,
        "contract": dict(contract),
        "history": list(history),
    }


def _save_checkpoint(
    path: Path,
    *,
    mode: str,
    step: int,
    best_validation: float,
    semantic_model: torch.nn.Module,
    bridge_optimizer: torch.optim.Optimizer,
    lora_optimizer: torch.optim.Optimizer | None,
    qwen: torch.nn.Module | None,
    contract: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    adapter_path: str | None,
) -> None:
    _atomic_torch_save(_checkpoint_payload(
        mode=mode,
        step=step,
        best_validation=best_validation,
        semantic_model=semantic_model,
        bridge_optimizer=bridge_optimizer,
        lora_optimizer=lora_optimizer,
        qwen=qwen,
        contract=contract,
        history=history,
        adapter_path=adapter_path,
    ), path)


@torch.no_grad()
def _evaluate(
    semantic_model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
    config: TCSimConfig,
    *,
    qwen: torch.nn.Module | None,
    tokenizer: Any,
    microbatch: int,
    max_prompt_tokens: int,
    amp_dtype: torch.dtype | None,
    token_cache: Dict[str, tuple[int, ...]] | None,
) -> Dict[str, float]:
    model = _raw(semantic_model)
    model.eval()
    if qwen is not None:
        qwen.eval()
    totals = torch.zeros(10, dtype=torch.float64, device=device)
    for batch in loader:
        if qwen is not None:
            chunks, _tokens = _tokenize_chunks(
                tokenizer,
                batch["semantic_prompts"],
                microbatch=microbatch,
                max_prompt_tokens=max_prompt_tokens,
                token_cache=token_cache,
            )
            parts = [
                _encode_chunk(qwen, chunk, device, amp_dtype=amp_dtype)
                for chunk in chunks
            ]
            expanded = torch.cat(parts, dim=0)
            batch["semantic_values"] = expanded.index_select(
                0, batch["semantic_prompt_select"].to(device).long(),
            )
        batch = _move_batch(batch, device)
        with torch.autocast(
            device_type=device.type,
            dtype=amp_dtype,
            enabled=amp_dtype is not None,
        ):
            losses = compute_v29_losses(
                model(batch), batch, **_loss_kwargs(config),
            )
        totals += torch.tensor([
            float(losses.total),
            float(losses.commit_time),
            float(losses.prefix_bce),
            float(losses.progress_count),
            float(losses.cumulative),
            float(losses.branch_token),
            float(losses.branch_count),
            float(losses.commit_log_mae),
            float(losses.progress_mae),
            1.0,
        ], dtype=torch.float64, device=device)
    if dist.is_initialized():
        dist.all_reduce(totals, op=dist.ReduceOp.SUM)
    names = (
        "total", "commit_time", "prefix_bce", "progress_count", "cumulative",
        "branch_token", "branch_count", "commit_log_mae", "progress_mae",
    )
    count = max(1.0, float(totals[-1]))
    return {f"val_{name}": float(totals[i]) / count for i, name in enumerate(names)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=sorted(MODES), required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--base-checkpoint", required=True)
    parser.add_argument("--semantic-cache-root", required=True)
    parser.add_argument("--semantic-sidecar-root", required=True)
    parser.add_argument("--static-manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--resume", default=None)
    parser.add_argument("--cores", default="4,8")
    parser.add_argument("--max-steps", type=int, required=True)
    parser.add_argument("--seed", type=int, default=20260722)
    parser.add_argument("--bridge-lr", type=float, default=1e-4)
    parser.add_argument(
        "--fusion-architecture",
        choices=sorted(SUPPORTED_FUSION_ARCHITECTURES),
        default=LEGACY_FUSION_ARCHITECTURE,
    )
    parser.add_argument("--semantic-adapter-hidden-dim", type=int, default=1024)
    parser.add_argument("--semantic-slot-count", type=int, default=4)
    parser.add_argument("--semantic-attention-heads", type=int, default=4)
    parser.add_argument(
        "--semantic-max-residual-rms-ratio", type=float, default=0.05,
        help="hard per-token semantic residual RMS cap relative to base hidden",
    )
    parser.add_argument(
        "--semantic-residual-penalty-weight", type=float, default=0.0,
        help="weight for mean squared applied/base RMS residual regularization",
    )
    parser.add_argument("--lora-lr", type=float, default=1e-5)
    parser.add_argument("--weight-decay", type=float, default=0.05)
    parser.add_argument("--gradient-clip", type=float, default=5.0)
    parser.add_argument("--save-every", type=int, default=100)
    parser.add_argument("--eval-every", type=int, default=100)
    parser.add_argument("--log-every", type=int, default=10)
    parser.add_argument("--validation-max-sequences", type=int, default=64)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--qwen-model", default=str(REPO / "data/models/Qwen/Qwen3-14B"))
    parser.add_argument("--qwen-microbatch", type=int, default=4)
    parser.add_argument(
        "--retain-groups",
        default="0",
        help="canonical Qwen groups whose first-pass graph is retained; integer or all",
    )
    parser.add_argument(
        "--parallel-canonical-groups",
        type=int,
        choices=(1, 2, 4),
        default=1,
        help="independent unchanged batch=4 Qwen calls per CUDA-stream wave",
    )
    parser.add_argument("--disable-prompt-token-cache", action="store_true")
    parser.add_argument("--max-prompt-tokens", type=int, default=256)
    parser.add_argument("--lora-rank", type=int, default=16)
    parser.add_argument("--lora-alpha", type=int, default=32)
    parser.add_argument("--device", default="auto")
    args = parser.parse_args()

    if str(args.retain_groups).lower() == "all":
        retain_groups = -1
    else:
        retain_groups = int(args.retain_groups)
        if retain_groups < 0:
            raise SystemExit("--retain-groups must be non-negative or 'all'")
    if int(args.parallel_canonical_groups) > 1 and not torch.cuda.is_available():
        raise SystemExit("parallel canonical groups require CUDA")
    if int(args.num_workers) != 0:
        raise SystemExit("exact paired pilot currently requires --num-workers 0")
    if not 0.0 < float(args.semantic_max_residual_rms_ratio) <= 1.0:
        raise SystemExit("--semantic-max-residual-rms-ratio must be in (0,1]")
    if int(args.semantic_slot_count) <= 0:
        raise SystemExit("--semantic-slot-count must be positive")
    if int(args.semantic_attention_heads) <= 0:
        raise SystemExit("--semantic-attention-heads must be positive")
    if float(args.semantic_residual_penalty_weight) < 0.0:
        raise SystemExit("--semantic-residual-penalty-weight must be non-negative")
    cores = {int(value) for value in args.cores.split(",") if value.strip()}
    if not cores:
        raise SystemExit("--cores is empty")
    distributed, rank, local_rank, world, device = v29_train._setup_distributed(
        args.device,
    )
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    # All model initializers are rank-identical.  DDP then verifies/broadcasts
    # the bridge while the LoRA replicas are synchronized by manual all-reduce.
    random.seed(int(args.seed))
    np.random.seed(int(args.seed) % (2**32))
    torch.manual_seed(int(args.seed))
    if device.type == "cuda":
        torch.cuda.manual_seed(int(args.seed))

    config = TCSimConfig.load(args.config)
    config.train = {
        **config.train,
        "num_workers": 0,
        "validation_max_sequences": int(args.validation_max_sequences),
    }
    sequence_length = int(config.chunk.get("sequence_length", 4))
    sequence_stride = int(config.chunk.get("sequence_stride", sequence_length))
    all_train = _manifest_sources(args.manifest, "train")
    all_validation = _manifest_sources(args.manifest, "validation")
    train_sources = _select_cores(all_train, cores)
    validation_sources = _select_cores(all_validation, cores)
    _validate_separation(train_sources, validation_sources)

    include_prompts = args.mode == "lora"
    dataset_kwargs = dict(
        variant="b2-lora" if include_prompts else "b2-frozen",
        semantic_cache_root=args.semantic_cache_root,
        static_manifest=args.static_manifest,
        semantic_dim=5120,
        semantic_sidecar_root=args.semantic_sidecar_root,
        include_semantic_prompts=include_prompts,
        sequence_length=sequence_length,
        sequence_stride=sequence_stride,
    )
    train_data = TCSimV29SemanticDataset(train_sources, **dataset_kwargs)
    validation_data = TCSimV29SemanticDataset(validation_sources, **dataset_kwargs)
    cache = CachedSemanticSource(args.semantic_cache_root)
    cache_batch_size = int(cache.manifest["semantic_encoder_batch_size"])
    if include_prompts and int(args.qwen_microbatch) != cache_batch_size:
        raise RuntimeError(
            "zero-LoRA fairness requires the immutable cache batch size: "
            f"qwen_microbatch={args.qwen_microbatch} cache={cache_batch_size}"
        )

    base_checkpoint = Path(args.base_checkpoint).resolve()
    base_sha = _sha256(base_checkpoint)
    base_payload = _torch_load(base_checkpoint, "cpu")
    if int(base_payload.get("step", -1)) != 59000:
        raise RuntimeError(
            f"expected authoritative v29 best at step 59000, got {base_payload.get('step')}"
        )
    base_config = dict(base_payload.get("config", {}))
    current_config = {
        "chunk": dict(config.chunk),
        "scheduler": dict(config.scheduler),
        "uarch": dict(config.uarch),
        "model": dict(config.model),
    }
    for key, value in current_config.items():
        if base_config.get(key) != value:
            raise RuntimeError(f"v29 base/config mismatch for {key}")
    horizons = tuple(float(value) for value in config.chunk["horizons"])
    semantic_model = build_semantic_model(
        config.model,
        horizons,
        variant="b2-lora" if include_prompts else "b2-frozen",
        semantic_dim=int(cache.semantic_dim),
        fusion_architecture=args.fusion_architecture,
        semantic_adapter_hidden_dim=int(args.semantic_adapter_hidden_dim),
        semantic_max_residual_rms_ratio=float(
            args.semantic_max_residual_rms_ratio
        ),
        semantic_slot_count=int(args.semantic_slot_count),
        semantic_attention_heads=int(args.semantic_attention_heads),
    )
    semantic_model.backbone.load_state_dict(base_payload["model"], strict=True)
    del base_payload
    for parameter in semantic_model.backbone.parameters():
        parameter.requires_grad_(False)
    semantic_model = semantic_model.to(device)
    if (
        args.resume is None
        and args.fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES
    ):
        identity_batch = collate_tcsim_v29_semantic([validation_data[0]])
        _assert_residual_identity(semantic_model, identity_batch, device)
    if distributed:
        semantic_model = DDP(
            semantic_model,
            device_ids=[local_rank] if device.type == "cuda" else None,
        )
    bridge_parameters = list(_raw(semantic_model).semantic_bridge.parameters())
    bridge_optimizer = torch.optim.AdamW(
        bridge_parameters,
        lr=float(args.bridge_lr),
        weight_decay=float(args.weight_decay),
    )

    tokenizer = None
    qwen = None
    lora_parameters: List[torch.nn.Parameter] = []
    lora_optimizer = None
    qwen_streams: tuple[torch.cuda.Stream, ...] = ()
    prompt_token_cache: Dict[str, tuple[int, ...]] | None = (
        None if args.disable_prompt_token_cache else {}
    )
    if include_prompts:
        cached_encoder = Path(
            str(cache.manifest["semantic_encoder_model"])
        ).resolve()
        requested_encoder = Path(args.qwen_model).resolve()
        if cached_encoder != requested_encoder:
            raise RuntimeError(
                "online Qwen/cache encoder path mismatch: "
                f"{requested_encoder} != {cached_encoder}"
            )
        tokenizer, qwen, lora_parameters = _load_qwen_lora(args, device)
        if int(cache.semantic_dim) != int(qwen.config.hidden_size):
            raise RuntimeError("Qwen hidden size/semantic cache dimension mismatch")
        lora_optimizer = torch.optim.AdamW(
            lora_parameters,
            lr=float(args.lora_lr),
            weight_decay=float(args.weight_decay),
        )
        if int(args.parallel_canonical_groups) > 1:
            qwen_streams = tuple(
                torch.cuda.Stream(device=device)
                for _ in range(int(args.parallel_canonical_groups))
            )

    contract_common = {
        "run_schema": RUN_SCHEMA,
        "base_v29": {
            "path": str(base_checkpoint),
            "sha256": base_sha,
            "checkpoint_step": 59000,
            "frozen": True,
        },
        "loss": {
            "total": (
                "L_v29"
                if float(args.semantic_residual_penalty_weight) == 0.0
                else "L_v29 + lambda_residual * L_residual"
            ),
            "task": "L_v29",
            "semantic_residual_penalty_weight": float(
                args.semantic_residual_penalty_weight
            ),
            "L_static": False,
            "L_sem": False,
        },
        "data": {
            "manifest": str(Path(args.manifest).resolve()),
            "cores": sorted(cores),
            "sequence_length": sequence_length,
            "sequence_stride": sequence_stride,
            "train_sources": len(train_sources),
            "validation_sources": len(validation_sources),
        },
        "semantic_cache": {
            "root": str(Path(args.semantic_cache_root).resolve()),
            "manifest_hash": cache.manifest_hash,
            "prompt_schema": cache.manifest["semantic_prompt_schema_version"],
            "pooling": cache.manifest["semantic_pooling_policy"],
        },
        "bridge": {
            "fusion_architecture": str(args.fusion_architecture),
            "semantic_adapter_hidden_dim": int(
                args.semantic_adapter_hidden_dim
            ),
            "semantic_slot_count": (
                int(args.semantic_slot_count)
                if args.fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "semantic_attention_heads": (
                int(args.semantic_attention_heads)
                if args.fusion_architecture
                == MULTISLOT_BOUNDED_RESIDUAL_FUSION_ARCHITECTURE
                else None
            ),
            "injection_location": (
                "after_static_dynamic_side_sum_before_full_qkvr"
                if args.fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES
                else "before_tcsim_v29_full_qkvr_in_static_token_space"
            ),
            "initialization": (
                "zero_final_projection_exact_v29_identity"
                if args.fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES
                else "legacy_random_projection_post_layernorm"
            ),
            "max_residual_rms_ratio": (
                float(args.semantic_max_residual_rms_ratio)
                if args.fusion_architecture
                in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                else None
            ),
            "residual_penalty_weight": float(
                args.semantic_residual_penalty_weight
            ),
            "strict_bypass": (
                "exact_complete_semantic_branch_bypass"
                if args.fusion_architecture in RESIDUAL_FUSION_ARCHITECTURES
                else None
            ),
            "lr": float(args.bridge_lr),
            "weight_decay": float(args.weight_decay),
            "scheduler": "constant_none_matches_tcsim_v29",
        },
        "gradient_cache": {
            "enabled": include_prompts,
            "microbatch": int(args.qwen_microbatch),
            "max_prompt_tokens": int(args.max_prompt_tokens),
            "retain_groups": "all" if retain_groups < 0 else retain_groups,
            "parallel_canonical_groups": int(args.parallel_canonical_groups),
            "parallel_mechanism": "independent_cuda_streams_batch_shape_unchanged",
            "hybrid_exact_replay": True,
            "replay_order": "canonical_group_order_matches_established_gc",
            "lora_dropout": 0.0,
            "gradient_sync": "per-parameter_preserves_established_order",
            "prompt_token_cache": prompt_token_cache is not None,
        },
        "qwen": (
            None if not include_prompts else {
                "base_model": str(Path(args.qwen_model).resolve()),
                "base_frozen": True,
                "lora_rank": int(args.lora_rank),
                "lora_alpha": int(args.lora_alpha),
                "lora_lr": float(args.lora_lr),
                "target_modules": ["q_proj", "k_proj", "v_proj", "o_proj"],
                "merged": False,
            }
        ),
        "seed": int(args.seed),
        "world_size": int(world),
    }
    # The paired sampler/RNG contract deliberately excludes branch identity
    # and Qwen fields, so warmup can fork into frozen and LoRA at one cursor.
    paired_contract = {
        key: value for key, value in contract_common.items()
        if key not in {"gradient_cache", "qwen"}
    }
    paired_fingerprint = _json_fingerprint(paired_contract)

    start_step = 0
    best_validation = float("inf")
    history: List[Dict[str, Any]] = []
    resume_payload = None
    if args.resume:
        resume_path = Path(args.resume).resolve()
        resume_payload = _torch_load(resume_path, device)
        if resume_payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA:
            raise RuntimeError("resume is not a LoRA-v29 adapter checkpoint")
        stored = dict(resume_payload.get("contract", {}))
        stored_paired = {
            key: value for key, value in stored.items()
            if key not in {"gradient_cache", "qwen", "mode"}
        }
        if _json_fingerprint(stored_paired) != paired_fingerprint:
            raise RuntimeError("resume paired data/base/bridge contract mismatch")
        start_step = int(resume_payload["step"])
        if start_step >= int(args.max_steps):
            raise RuntimeError("resume step already reached --max-steps")
        _raw(semantic_model).semantic_bridge.load_state_dict(
            resume_payload["bridge"], strict=True,
        )
        bridge_optimizer.load_state_dict(resume_payload["bridge_optimizer"])
        if str(resume_payload.get("mode")) == str(args.mode):
            if include_prompts and stored.get("gradient_cache") != (
                contract_common["gradient_cache"]
            ):
                raise RuntimeError(
                    "exact same-mode resume requires an identical efficiency contract"
                )
            best_validation = float(
                resume_payload.get("best_validation", float("inf"))
            )
            history = list(resume_payload.get("history", []))
        else:
            # A paired fork starts a new validation lineage.  Carrying the
            # warmup best could prevent a branch-local best.pt from ever being
            # emitted even though its optimizer/sampler/RNG state is correct.
            best_validation = float("inf")
            history = []
        if include_prompts and resume_payload.get("lora") is not None:
            from peft import set_peft_model_state_dict

            set_peft_model_state_dict(qwen, resume_payload["lora"])
            if resume_payload.get("lora_optimizer") is None:
                raise RuntimeError("LoRA resume lacks optimizer state")
            lora_optimizer.load_state_dict(resume_payload["lora_optimizer"])
        _restore_rank_state(
            resume_path,
            step=start_step,
            rank=rank,
            world=world,
            device=device,
            expected_contract_fingerprint=paired_fingerprint,
        )

    weights = [
        1.0 / max(1, train_data.trace_sample_counts[trace_id])
        for trace_id in train_data.sample_trace_ids
    ]
    samples_per_rank = (
        math.ceil(len(train_data) / world) if distributed else len(train_data)
    )
    sampler = ExactResumeWeightedRandomSampler(
        weights,
        samples_per_rank,
        replacement=True,
        generator=torch.Generator().manual_seed(int(args.seed) + rank),
        resume_step=start_step,
        batch_size=1,
        drop_last=distributed,
    )
    loader_generator = torch.Generator().manual_seed(
        int(args.seed) + rank + 10_000_019,
    )
    train_loader = DataLoader(
        train_data,
        batch_size=1,
        sampler=sampler,
        collate_fn=collate_tcsim_v29_semantic,
        num_workers=0,
        pin_memory=device.type == "cuda",
        drop_last=distributed,
        generator=loader_generator,
    )
    validation_indices = v29_train._balanced_validation_indices(
        validation_data, int(args.validation_max_sequences),
    )
    validation_loader = DataLoader(
        validation_data,
        batch_size=1,
        sampler=(
            validation_indices[rank::world]
            if distributed else validation_indices
        ),
        collate_fn=collate_tcsim_v29_semantic,
        num_workers=0,
        pin_memory=device.type == "cuda",
        generator=torch.Generator().manual_seed(
            int(args.seed) + rank + 20_000_033,
        ),
    )

    run_contract = {**contract_common, "mode": args.mode}
    if rank == 0:
        _atomic_json({
            **run_contract,
            "paired_contract_fingerprint": paired_fingerprint,
            "start_step": start_step,
            "target_step": int(args.max_steps),
            "train_sequences": len(train_data),
            "validation_sequences": len(validation_indices),
        }, out / "run.json")
        trainable_bridge = sum(p.numel() for p in bridge_parameters)
        trainable_lora = sum(p.numel() for p in lora_parameters)
        print(
            f"[LoRA-v29] mode={args.mode} cores={sorted(cores)} "
            f"rank={rank}/{world} start={start_step} target={args.max_steps} "
            f"train={len(train_data)} val={len(validation_indices)} "
            f"bridge_params={trainable_bridge:,} lora_params={trainable_lora:,} "
            f"loss=L_v29+residual_penalty({float(args.semantic_residual_penalty_weight):g}) "
            f"base_sha={base_sha[:12]} "
            f"retain_groups={'all' if retain_groups < 0 else retain_groups} "
            f"parallel_groups={int(args.parallel_canonical_groups)} "
            "grad_sync=per-parameter "
            f"token_cache={prompt_token_cache is not None}",
            flush=True,
        )

    amp_dtype = torch.bfloat16 if device.type == "cuda" else None
    step = start_step
    started = time.perf_counter()
    interval_started = started
    interval_prompts = 0
    interval_tokens = 0
    interval_retained_groups = 0
    interval_replayed_groups = 0
    latest_losses = None
    while step < int(args.max_steps):
        semantic_model.train()
        exhausted = True
        for batch in train_loader:
            exhausted = False
            if step >= int(args.max_steps):
                break
            step += 1
            bridge_optimizer.zero_grad(set_to_none=True)
            if lora_optimizer is not None:
                lora_optimizer.zero_grad(set_to_none=True)

            chunks = None
            replay_leaves: List[ReplayLeaf] = []
            retained_groups = 0
            prompt_count = 0
            token_count = 0
            if include_prompts:
                cached_semantic_values = batch["semantic_values"]
                chunks, token_count = _tokenize_chunks(
                    tokenizer,
                    batch["semantic_prompts"],
                    microbatch=int(args.qwen_microbatch),
                    max_prompt_tokens=int(args.max_prompt_tokens),
                    token_cache=prompt_token_cache,
                )
                prompt_count = len(batch["semantic_prompts"])
                expanded_semantic, replay_leaves, retained_groups = (
                    hybrid_gradient_cache_embeddings(
                        qwen,
                        chunks,
                        device,
                        amp_dtype=amp_dtype,
                        retain_groups=retain_groups,
                        streams=qwen_streams,
                    )
                )
                selected_semantic = expanded_semantic.index_select(
                    0,
                    batch["semantic_prompt_select"].to(device).long(),
                )
                if step == start_step + 1 and (
                    resume_payload is None
                    or resume_payload.get("lora") is None
                ):
                    recompute_error = (
                        selected_semantic.detach().float()
                        - cached_semantic_values.to(device).float()
                    ).abs().max()
                    if distributed:
                        dist.all_reduce(recompute_error, op=dist.ReduceOp.MAX)
                    if float(recompute_error) != 0.0:
                        raise RuntimeError(
                            "zero-LoRA online/cache semantic mismatch: "
                            f"max_abs={float(recompute_error):.6g}"
                        )
                    if rank == 0:
                        print(
                            "[LoRA-v29 alignment] zero-LoRA/cache "
                            f"max_abs={float(recompute_error):.6g}",
                            flush=True,
                        )
                batch["semantic_values"] = selected_semantic
            batch = _move_batch(batch, device)
            with torch.autocast(
                device_type=device.type,
                dtype=amp_dtype,
                enabled=amp_dtype is not None,
            ):
                predictions = semantic_model(batch)
                losses = compute_v29_losses(
                    predictions, batch, **_loss_kwargs(config),
                )
                residual_penalty = (
                    _raw(semantic_model).semantic_bridge
                    .residual_regularization_loss()
                    if args.fusion_architecture
                    in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                    else losses.total * 0.0
                )
                optimization_loss = losses.total + (
                    float(args.semantic_residual_penalty_weight)
                    * residual_penalty
                )
                residual_max_ratio = (
                    _raw(semantic_model).semantic_bridge.residual_max_ratio()
                    if args.fusion_architecture
                    in BOUNDED_RESIDUAL_FUSION_ARCHITECTURES
                    else losses.total.detach() * 0.0
                )
            optimization_loss.backward()
            if not torch.isfinite(optimization_loss):
                raise RuntimeError("non-finite L_v29/residual objective")
            if include_prompts:
                hybrid_gradient_cache_replay_backward(
                    qwen,
                    replay_leaves,
                    device,
                    amp_dtype=amp_dtype,
                    streams=qwen_streams,
                )
                _per_parameter_average_gradients(
                    lora_parameters, world=world,
                )
                lora_norm = torch.nn.utils.clip_grad_norm_(
                    lora_parameters, float(args.gradient_clip),
                )
                if not torch.isfinite(lora_norm) or float(lora_norm) <= 0.0:
                    raise RuntimeError(f"invalid LoRA gradient norm {lora_norm}")
            bridge_norm = torch.nn.utils.clip_grad_norm_(
                bridge_parameters, float(args.gradient_clip),
            )
            if not torch.isfinite(bridge_norm) or float(bridge_norm) <= 0.0:
                raise RuntimeError(f"invalid bridge gradient norm {bridge_norm}")
            bridge_optimizer.step()
            if lora_optimizer is not None:
                lora_optimizer.step()
            interval_prompts += prompt_count
            interval_tokens += token_count
            interval_retained_groups += retained_groups
            interval_replayed_groups += sum(
                entry.retained_output is None for entry in replay_leaves
            )
            latest_losses = losses

            should_save = (
                (int(args.save_every) > 0 and step % int(args.save_every) == 0)
                or step == int(args.max_steps)
            )
            should_eval = (
                int(args.eval_every) > 0
                and step % int(args.eval_every) == 0
            )
            if should_save or should_eval:
                _capture_rank_state(
                    out,
                    step=step,
                    rank=rank,
                    world=world,
                    device=device,
                    contract_fingerprint=paired_fingerprint,
                )
                if distributed:
                    dist.barrier()
                if rank == 0:
                    adapter_path = (
                        _save_adapter(qwen, out, step) if qwen is not None else None
                    )
                    _save_checkpoint(
                        out / "last.pt",
                        mode=args.mode,
                        step=step,
                        best_validation=best_validation,
                        semantic_model=semantic_model,
                        bridge_optimizer=bridge_optimizer,
                        lora_optimizer=lora_optimizer,
                        qwen=qwen,
                        contract=run_contract,
                        history=history,
                        adapter_path=adapter_path,
                    )
                if distributed:
                    dist.barrier()

            if should_eval:
                metrics = _evaluate(
                    semantic_model,
                    validation_loader,
                    device,
                    config,
                    qwen=qwen,
                    tokenizer=tokenizer,
                    microbatch=int(args.qwen_microbatch),
                    max_prompt_tokens=int(args.max_prompt_tokens),
                    amp_dtype=amp_dtype,
                    token_cache=prompt_token_cache,
                )
                # Validation is deterministic, but capture again so last.pt,
                # history/best metadata and every rank's RNG state describe
                # the same post-validation boundary after a clean eval.
                _capture_rank_state(
                    out,
                    step=step,
                    rank=rank,
                    world=world,
                    device=device,
                    contract_fingerprint=paired_fingerprint,
                )
                if distributed:
                    dist.barrier()
                if rank == 0:
                    row = {
                        "step": step,
                        "elapsed_s": time.perf_counter() - started,
                        **metrics,
                    }
                    history.append(row)
                    if metrics["val_total"] < best_validation:
                        best_validation = metrics["val_total"]
                        adapter_path = (
                            _save_adapter(qwen, out, step)
                            if qwen is not None else None
                        )
                        _save_checkpoint(
                            out / "best.pt",
                            mode=args.mode,
                            step=step,
                            best_validation=best_validation,
                            semantic_model=semantic_model,
                            bridge_optimizer=bridge_optimizer,
                            lora_optimizer=lora_optimizer,
                            qwen=qwen,
                            contract=run_contract,
                            history=history,
                            adapter_path=adapter_path,
                        )
                    adapter_path = (
                        _save_adapter(qwen, out, step)
                        if qwen is not None else None
                    )
                    _save_checkpoint(
                        out / "last.pt",
                        mode=args.mode,
                        step=step,
                        best_validation=best_validation,
                        semantic_model=semantic_model,
                        bridge_optimizer=bridge_optimizer,
                        lora_optimizer=lora_optimizer,
                        qwen=qwen,
                        contract=run_contract,
                        history=history,
                        adapter_path=adapter_path,
                    )
                    _atomic_json(history, out / "metrics.json")
                    print(f"[LoRA-v29 eval step={step}] {metrics}", flush=True)
                if distributed:
                    dist.barrier()
                semantic_model.train()
                if qwen is not None:
                    qwen.eval()

            if rank == 0 and (
                step % int(args.log_every) == 0 or step == int(args.max_steps)
            ):
                now = time.perf_counter()
                elapsed = max(now - interval_started, 1e-9)
                steps_done = int(args.log_every)
                if step == int(args.max_steps) and step % int(args.log_every):
                    steps_done = step % int(args.log_every)
                print(
                    f"[LoRA-v29 step={step}] objective={float(optimization_loss.detach()):.6f} "
                    f"L_v29={float(losses.total.detach()):.6f} "
                    f"residual_penalty={float(residual_penalty.detach()):.8f} "
                    f"residual_max_ratio={float(residual_max_ratio):.6f} "
                    f"time={float(losses.commit_time.detach()):.6f} "
                    f"prefix={float(losses.prefix_bce.detach()):.6f} "
                    f"step_s={elapsed / max(1, steps_done):.3f} "
                    f"prompts_s={interval_prompts / elapsed:.1f} "
                    f"tokens_s={interval_tokens / elapsed:.1f} "
                    f"retain_groups_avg={interval_retained_groups/max(1,steps_done):.1f} "
                    f"replay_groups_avg={interval_replayed_groups/max(1,steps_done):.1f} "
                    f"gpu_peak_gib={torch.cuda.max_memory_allocated(device)/2**30:.2f}"
                    if device.type == "cuda" else
                    f"[LoRA-v29 step={step}] objective={float(optimization_loss.detach()):.6f} "
                    f"L_v29={float(losses.total.detach()):.6f} "
                    f"residual_penalty={float(residual_penalty.detach()):.8f} "
                    f"step_s={elapsed / max(1, steps_done):.3f}",
                    flush=True,
                )
                interval_started = now
                interval_prompts = 0
                interval_tokens = 0
                interval_retained_groups = 0
                interval_replayed_groups = 0
                if device.type == "cuda":
                    torch.cuda.reset_peak_memory_stats(device)
        if step >= int(args.max_steps):
            break
        if exhausted:
            raise RuntimeError("training loader yielded no batches")
        # ExactResumeWeightedRandomSampler produces a new deterministic draw
        # on every subsequent iterator, so long runs can cross epoch bounds.

    if latest_losses is None:
        raise RuntimeError("training consumed no batches")
    if rank == 0:
        print(
            f"[LoRA-v29] COMPLETE mode={args.mode} step={step} "
            f"elapsed_s={time.perf_counter()-started:.1f} best={best_validation}",
            flush=True,
        )
    if distributed:
        dist.barrier()
        dist.destroy_process_group()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
