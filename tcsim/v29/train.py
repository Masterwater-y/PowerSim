"""DDP-capable v29 training loop."""
from __future__ import annotations

import math
import os
import random
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, DistributedSampler, WeightedRandomSampler

from ..utils.config import TCSimConfig
from ..utils.io import dump_json
from .contracts import CHECKPOINT_SCHEMA_VERSION
from .dataset import V29GlobalTimeDataset, collate_v29_sequences
from .losses import compute_v29_losses
from .model import (
    BRANCH_MODE_NEURAL_HEAD,
    BRANCH_MODES_WITHOUT_HEAD,
    LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION,
    TCSimV29Model,
    build_model,
)
from .sampling import (
    COVERAGE_FIRST_TRACE_BALANCED_SAMPLER,
    CoverageFirstTraceBalancedSampler,
)


@dataclass
class TrainState:
    step: int = 0
    best_validation: float = float("inf")
    best_post_coverage_validation: float = float("inf")


CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


def _to_device(batch: Dict[str, Any], device: torch.device) -> Dict[str, Any]:
    return {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }


def _setup_distributed(device: str) -> Tuple[bool, int, int, int, torch.device]:
    distributed = "RANK" in os.environ and "WORLD_SIZE" in os.environ
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    world = int(os.environ.get("WORLD_SIZE", "1"))
    cuda_requested = str(device) in {"auto", "cuda"} or str(device).startswith("cuda:")
    if distributed and not dist.is_initialized():
        dist.init_process_group(
            backend="nccl" if torch.cuda.is_available() and cuda_requested else "gloo"
        )
    if torch.cuda.is_available() and cuda_requested:
        torch.cuda.set_device(local_rank)
        torch_device = torch.device(f"cuda:{local_rank}")
    elif str(device) == "auto" and torch.cuda.is_available():
        torch_device = torch.device("cuda")
    else:
        torch_device = torch.device(device)
    return distributed, rank, local_rank, world, torch_device


def _amp_dtype(name: str, device: torch.device):
    if device.type != "cuda":
        return None
    value = str(name).lower()
    if value in {"none", "off", "fp32", "float32"}:
        return None
    if value in {"bf16", "bfloat16"}:
        return torch.bfloat16
    if value in {"fp16", "float16"}:
        return torch.float16
    raise ValueError(f"unsupported v29 amp dtype {name!r}")


def _attention_profile_events(profiler: Any) -> List[str]:
    events = []
    for event in profiler.key_averages():
        key = str(event.key)
        lowered = key.lower()
        if "scaled_dot_product" not in lowered and "attention" not in lowered:
            continue
        cpu_us = float(getattr(event, "self_cpu_time_total", 0.0) or 0.0)
        cuda_us = float(getattr(event, "self_cuda_time_total", 0.0) or 0.0)
        events.append(f"{key} cpu_us={cpu_us:.0f} cuda_us={cuda_us:.0f}")
    return sorted(events)


def _raw_model(model: torch.nn.Module) -> torch.nn.Module:
    return model.module if isinstance(model, DDP) else model


def _dataset_contract(dataset: V29GlobalTimeDataset) -> Dict[str, Any]:
    first = dataset.stores[0].meta
    keys = (
        "raw_trace_schema", "dataset_schema", "model_input_contract",
        "feature_schema", "branch_contract", "resource_decoder_schema",
        "resource_decoder_hash", "predictor_hash", "horizons",
        "sample_period_cycles", "dimensions",
    )
    contract = {key: first[key] for key in keys}
    if dataset.stores[0].long_history_contract is not None:
        contract["long_history"] = dict(
            dataset.stores[0].long_history_contract
        )
    if dataset.stores[0].branch_feature_contract is not None:
        contract["branch_features"] = dict(
            dataset.stores[0].branch_feature_contract
        )
    return contract


def _save_checkpoint(
    path: str,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    state: TrainState,
    config: TCSimConfig,
    contract: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> None:
    payload = {
        "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
        "model": _raw_model(model).state_dict(),
        "optimizer": optimizer.state_dict(),
        "step": int(state.step),
        "best_validation": float(state.best_validation),
        "best_post_coverage_validation": float(
            state.best_post_coverage_validation
        ),
        "contract": dict(contract),
        "config": {
            "chunk": dict(config.chunk),
            "scheduler": dict(config.scheduler),
            "uarch": dict(config.uarch),
            "model": dict(config.model),
            "train": dict(config.train),
        },
        "history": list(history),
    }
    # The watchdog may inspect checkpoints while training is running.  Write
    # beside the destination and replace atomically so it never loads a
    # partially serialized model/optimizer state.
    temporary = f"{path}.tmp-{os.getpid()}"
    try:
        torch.save(payload, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _torch_load(path: str, device: torch.device) -> Any:
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:  # older torch
        return torch.load(path, map_location=device)


def _load_checkpoint(
    path: str,
    model: torch.nn.Module,
    optimizer: Optional[torch.optim.Optimizer],
    device: torch.device,
    contract: Mapping[str, Any],
) -> Tuple[TrainState, List[Dict[str, Any]]]:
    payload = _torch_load(path, device)
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("checkpoint is not a v29 checkpoint")
    if payload.get("contract") != dict(contract):
        raise RuntimeError("v29 checkpoint/cache contract mismatch")
    model.load_state_dict(payload["model"])
    if optimizer is not None and payload.get("optimizer"):
        optimizer.load_state_dict(payload["optimizer"])
    return TrainState(
        step=int(payload.get("step", 0)),
        best_validation=float(payload.get("best_validation", float("inf"))),
        best_post_coverage_validation=float(
            payload.get("best_post_coverage_validation", float("inf"))
        ),
    ), list(payload.get("history", []))


def _contract_without_long_history(
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    out = dict(contract)
    out.pop("long_history", None)
    return out


def _initialize_frozen_memory_probe(
    path: str,
    model: TCSimV29Model,
    contract: Mapping[str, Any],
) -> Dict[str, Any]:
    """Load the exact E0 state while allowing only the new correction modules."""
    payload = _torch_load(path, torch.device("cpu"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("v29 frozen probe initialization is not a checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("v29 frozen probe initialization is not a v29 checkpoint")
    source_contract = payload.get("contract")
    if not isinstance(source_contract, Mapping):
        raise RuntimeError("v29 frozen probe initialization lacks a contract")
    if (
        _contract_without_long_history(source_contract)
        != _contract_without_long_history(contract)
    ):
        raise RuntimeError(
            "v29 frozen probe E0/cache base contract mismatch "
            "(long_history is the only allowed contract difference)"
        )
    incompatible = model.load_state_dict(payload["model"], strict=False)
    allowed_missing_prefixes = (
        "memory_history_projection.",
        "memory_correction_head.",
    )
    disallowed_missing = [
        key for key in incompatible.missing_keys
        if not key.startswith(allowed_missing_prefixes)
    ]
    if disallowed_missing or incompatible.unexpected_keys:
        raise RuntimeError(
            "v29 frozen probe E0 state mismatch: "
            f"missing={disallowed_missing} "
            f"unexpected={list(incompatible.unexpected_keys)}"
        )
    expected_new = {
        key for key in model.state_dict()
        if key.startswith(allowed_missing_prefixes)
    }
    observed_new = set(incompatible.missing_keys)
    if observed_new != expected_new:
        raise RuntimeError(
            "v29 frozen probe did not isolate exactly the new correction state: "
            f"missing={sorted(observed_new)} expected={sorted(expected_new)}"
        )
    metadata = {
        "source_checkpoint": os.path.abspath(path),
        "source_step": int(payload.get("step", 0)),
        "source_best_validation": float(
            payload.get("best_validation", float("nan"))
        ),
        "new_state_keys": sorted(observed_new),
    }
    del payload
    return metadata


def _configure_frozen_memory_probe(model: TCSimV29Model) -> List[str]:
    """Freeze E0 completely and expose only the opt-in correction parameters."""
    if (
        model.long_history_mode
        != LONG_HISTORY_MODE_MEMORY_GATED_CORRECTION
    ):
        raise RuntimeError(
            "frozen_memory_probe requires "
            "long_history_mode=memory_gated_timing_correction"
        )
    if (
        model.memory_history_projection is None
        or model.memory_correction_head is None
    ):
        raise RuntimeError("frozen_memory_probe correction modules are missing")
    for parameter in model.parameters():
        parameter.requires_grad_(False)
    for module in (
        model.memory_history_projection,
        model.memory_correction_head,
    ):
        for parameter in module.parameters():
            parameter.requires_grad_(True)
    names = [
        name for name, parameter in model.named_parameters()
        if parameter.requires_grad
    ]
    if not names:
        raise RuntimeError("frozen_memory_probe has no trainable parameters")
    return names


def _set_frozen_memory_probe_train_mode(model: TCSimV29Model) -> None:
    """Keep the frozen E0 backbone at inference semantics while training Corr."""
    model.static_encoder.eval()
    model.interaction.eval()
    model.gap_head.eval()
    if model.branch_head is not None:
        model.branch_head.eval()
    assert model.memory_history_projection is not None
    assert model.memory_correction_head is not None
    model.memory_history_projection.train()
    model.memory_correction_head.train()


def _loss_kwargs(config: TCSimConfig) -> Dict[str, Any]:
    return {
        "weights": dict(config.train.get("loss_weights", {})),
        "time_beta": float(config.train.get("time_huber_beta", 0.2)),
        "progress_count_beta": float(config.train.get("progress_count_beta", 8.0)),
        "branch_count_beta": float(config.train.get("branch_count_beta", 1.0)),
    }


def _balanced_validation_indices(
    dataset: V29GlobalTimeDataset,
    maximum: int,
) -> List[int]:
    """Select deterministic, time-spread, trace-equal validation sequences."""
    total = len(dataset)
    maximum = int(maximum)
    if maximum <= 0 or total <= maximum:
        return list(range(total))
    by_trace: Dict[str, List[int]] = {}
    for index, trace_id in enumerate(dataset.sample_trace_ids):
        by_trace.setdefault(str(trace_id), []).append(index)
    names = sorted(by_trace)
    # Never silently omit a validation trace merely because a cap was set too
    # low.  The effective cap is at least one sequence per trace.
    budget = min(total, max(maximum, len(names)))
    allocation = {name: 0 for name in names}
    allocated = 0
    while allocated < budget:
        progressed = False
        for name in names:
            if allocated >= budget:
                break
            if allocation[name] >= len(by_trace[name]):
                continue
            allocation[name] += 1
            allocated += 1
            progressed = True
        if not progressed:
            break

    selected_by_trace: Dict[str, List[int]] = {}
    for name in names:
        indices = by_trace[name]
        count = allocation[name]
        if count <= 0:
            selected_by_trace[name] = []
        elif count >= len(indices):
            selected_by_trace[name] = list(indices)
        elif count == 1:
            selected_by_trace[name] = [indices[len(indices) // 2]]
        else:
            positions = [
                int(round(position * (len(indices) - 1) / (count - 1)))
                for position in range(count)
            ]
            selected_by_trace[name] = [indices[position] for position in positions]

    # Interleave traces so rank-strided DDP partitioning remains balanced.
    out: List[int] = []
    for position in range(max(allocation.values(), default=0)):
        for name in names:
            values = selected_by_trace[name]
            if position < len(values):
                out.append(values[position])
    if len(out) != budget or len(set(out)) != len(out):
        raise RuntimeError("invalid v29 balanced validation subset")
    return out


@torch.no_grad()
def evaluate(
    model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
    config: TCSimConfig,
) -> Dict[str, float]:
    model.eval()
    # Validation ranks may own one different number of sequences.  Calling the
    # DDP wrapper would broadcast buffers on every forward and can deadlock
    # when one rank reaches the final all-reduce first.  Parameters are already
    # synchronized after training; local validation uses the raw module and
    # reduces only the final metric accumulators.
    evaluation_model = _raw_model(model)
    amp = _amp_dtype(str(config.train.get("amp_dtype", "none")), device)
    totals = torch.zeros(10, dtype=torch.float64, device=device)
    for batch in loader:
        batch = _to_device(batch, device)
        with torch.autocast(
            device_type=device.type,
            dtype=amp,
            enabled=amp is not None,
        ):
            predictions = evaluation_model(batch)
            losses = compute_v29_losses(predictions, batch, **_loss_kwargs(config))
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
        if losses.monotonic_violations:
            raise RuntimeError("v29 model produced a monotonicity violation")
    if dist.is_available() and dist.is_initialized():
        dist.all_reduce(totals, op=dist.ReduceOp.SUM)
    count = max(1.0, float(totals[-1]))
    names = (
        "total", "commit_time", "prefix_bce", "progress_count", "cumulative",
        "branch_token", "branch_count", "commit_log_mae", "progress_mae",
    )
    return {f"val_{name}": float(totals[index]) / count for index, name in enumerate(names)}


def train_one_run(
    train_sources: Sequence[Any],
    validation_sources: Sequence[Any],
    out_dir: str,
    config: TCSimConfig,
    *,
    device: str = "auto",
    max_steps: Optional[int] = None,
    resume: Optional[str] = None,
    init_checkpoint: Optional[str] = None,
) -> Dict[str, Any]:
    distributed, rank, local_rank, world, torch_device = _setup_distributed(device)
    seed = int(config.train.get("seed", 1234))
    random.seed(seed + rank)
    torch.manual_seed(seed + rank)
    sequence_length = int(config.chunk.get("sequence_length", 4))
    sequence_stride = int(config.chunk.get("sequence_stride", sequence_length))
    train_data = V29GlobalTimeDataset(
        train_sources,
        sequence_length=sequence_length,
        sequence_stride=sequence_stride,
    )
    validation_data = V29GlobalTimeDataset(
        validation_sources,
        sequence_length=sequence_length,
        sequence_stride=sequence_stride,
    ) if validation_sources else None
    contract = _dataset_contract(train_data)
    if validation_data is not None and _dataset_contract(validation_data) != contract:
        raise RuntimeError("v29 training/validation contracts differ")
    configured_horizons = tuple(float(value) for value in config.chunk.get("horizons", []))
    if configured_horizons and configured_horizons != tuple(contract["horizons"]):
        raise RuntimeError("v29 config/cache horizon mismatch")
    configured_history_dim = int(config.model.get("long_history_dim", 0))
    cached_history_dim = int(
        contract.get("long_history", {}).get("output_dim", 0)
    )
    if configured_history_dim != cached_history_dim:
        raise RuntimeError(
            "v29 config/cache long-history dimension mismatch: "
            f"{configured_history_dim} != {cached_history_dim}"
        )
    branch_mode = str(
        config.model.get("branch_mode", BRANCH_MODE_NEURAL_HEAD)
    ).strip().lower()
    cached_branch_contract = contract.get("branch_features")
    if branch_mode in BRANCH_MODES_WITHOUT_HEAD and cached_branch_contract is None:
        raise RuntimeError(
            f"v29 branch_mode={branch_mode} requires branch replay sidecars"
        )
    branch_weights = dict(config.train.get("loss_weights", {}))
    if branch_mode in BRANCH_MODES_WITHOUT_HEAD and (
        float(branch_weights.get("branch_token", 0.0)) != 0.0
        or float(branch_weights.get("branch_count", 0.0)) != 0.0
    ):
        raise RuntimeError(
            f"v29 branch_mode={branch_mode} requires zero branch_token/branch_count loss"
        )

    batch_size = int(config.train.get("batch_samples", 1))
    sampler = None
    shuffle = True
    drop_last = False
    if bool(config.train.get("trace_balanced_sampling", True)):
        if bool(config.train.get("coverage_first_sampling", False)):
            sampler = CoverageFirstTraceBalancedSampler(
                train_data.sample_trace_ids,
                num_replicas=world if distributed else 1,
                rank=rank if distributed else 0,
                seed=seed,
            )
        else:
            weights = [
                1.0 / max(1, train_data.trace_sample_counts[trace_id])
                for trace_id in train_data.sample_trace_ids
            ]
            sampler = WeightedRandomSampler(
                weights,
                num_samples=(
                    math.ceil(len(train_data) / world)
                    if distributed else len(train_data)
                ),
                replacement=True,
                generator=torch.Generator().manual_seed(seed + rank),
            )
        shuffle = False
        drop_last = distributed
    elif distributed:
        sampler = DistributedSampler(
            train_data, num_replicas=world, rank=rank, shuffle=True, drop_last=True,
        )
        shuffle = False
        drop_last = True
    train_loader = DataLoader(
        train_data,
        batch_size=batch_size,
        shuffle=shuffle and sampler is None,
        sampler=sampler,
        collate_fn=collate_v29_sequences,
        num_workers=int(config.train.get("num_workers", 0)),
        pin_memory=torch_device.type == "cuda",
        drop_last=drop_last,
    )
    validation_loader = None
    validation_indices: List[int] = []
    if validation_data is not None:
        validation_indices = _balanced_validation_indices(
            validation_data,
            int(config.train.get("validation_max_sequences", 0)),
        )
        validation_loader = DataLoader(
            validation_data,
            batch_size=batch_size,
            shuffle=False,
            sampler=(
                validation_indices[rank::world]
                if distributed else validation_indices
            ),
            collate_fn=collate_v29_sequences,
            num_workers=int(config.train.get("num_workers", 0)),
            pin_memory=torch_device.type == "cuda",
        )

    if resume and init_checkpoint:
        raise RuntimeError("v29 --resume and --init-checkpoint are mutually exclusive")
    frozen_probe = bool(config.train.get("frozen_memory_probe", False))
    if init_checkpoint and not frozen_probe:
        raise RuntimeError(
            "v29 --init-checkpoint is restricted to frozen_memory_probe"
        )
    if frozen_probe and not resume and not init_checkpoint:
        raise RuntimeError(
            "fresh frozen_memory_probe requires an E0 --init-checkpoint"
        )
    model = build_model(config.model, contract["horizons"]).to(torch_device)
    initialization: Optional[Dict[str, Any]] = None
    if init_checkpoint and (rank == 0 or not distributed):
        initialization = _initialize_frozen_memory_probe(
            init_checkpoint, model, contract,
        )
        print(
            "[v29 frozen-probe init] "
            f"source={initialization['source_checkpoint']} "
            f"source_ckpt_step={initialization['source_step']} "
            f"new_state_keys={len(initialization['new_state_keys'])}",
            flush=True,
        )
    trainable_names: List[str] = []
    if frozen_probe:
        trainable_names = _configure_frozen_memory_probe(model)
        if init_checkpoint:
            config.train = {
                **config.train,
                "init_checkpoint": os.path.abspath(init_checkpoint),
            }
    if distributed:
        model = DDP(
            model,
            device_ids=[local_rank] if torch_device.type == "cuda" else None,
        )
    trainable_parameters = [
        parameter for parameter in model.parameters()
        if parameter.requires_grad
    ]
    if not trainable_parameters:
        raise RuntimeError("v29 training has no trainable parameters")
    optimizer = torch.optim.AdamW(
        trainable_parameters,
        lr=float(config.train.get("lr", 1e-4)),
        weight_decay=float(config.train.get("weight_decay", 5e-2)),
    )
    state = TrainState()
    history: List[Dict[str, Any]] = []
    if resume:
        state, history = _load_checkpoint(
            resume, _raw_model(model), optimizer, torch_device, contract,
        )
    sampler_steps_per_epoch = (
        sampler.num_samples
        if isinstance(sampler, CoverageFirstTraceBalancedSampler)
        else len(train_loader)
    )
    start_epoch = (
        state.step // sampler_steps_per_epoch
        if isinstance(sampler, CoverageFirstTraceBalancedSampler) else 0
    )
    start_epoch_offset = (
        state.step % sampler_steps_per_epoch
        if isinstance(sampler, CoverageFirstTraceBalancedSampler) else 0
    )
    os.makedirs(out_dir, exist_ok=True)
    amp = _amp_dtype(str(config.train.get("amp_dtype", "none")), torch_device)
    scaler = torch.amp.GradScaler(
        "cuda", enabled=torch_device.type == "cuda" and amp == torch.float16,
    )
    profile_attention = bool(config.train.get("profile_attention", False))
    profile_attention_done = False
    epochs = int(config.train.get("epochs", 100))
    log_every = int(config.train.get("log_every", 20))
    eval_every = int(config.train.get("eval_every", 1000))
    save_every = int(config.train.get("save_every", 500))
    gradient_clip = float(config.train.get("gradient_clip", 5.0))
    sampling_name = (
        COVERAGE_FIRST_TRACE_BALANCED_SAMPLER
        if isinstance(sampler, CoverageFirstTraceBalancedSampler)
        else type(sampler).__name__ if sampler is not None else "shuffle"
    )
    if rank == 0:
        parameter_count = sum(
            parameter.numel() for parameter in _raw_model(model).parameters()
        )
        trainable_count = sum(
            parameter.numel() for parameter in _raw_model(model).parameters()
            if parameter.requires_grad
        )
        print(
            f"[v29 train] train_sequences={len(train_data)} "
            f"val_sequences={len(validation_indices)}"
            f"/{len(validation_data) if validation_data else 0} "
            f"ddp={distributed} rank={rank}/{world} device={torch_device} "
            f"params={parameter_count:,} trainable={trainable_count:,} "
            f"frozen_probe={frozen_probe} "
            f"start={state.step} target={max_steps} "
            f"sdpa={config.model.get('sdpa_backend', 'auto')} "
            f"amp={config.train.get('amp_dtype', 'none')} "
            f"profile_attention={profile_attention} "
            f"sampling={sampling_name} "
            f"steps_per_epoch={sampler_steps_per_epoch} "
            f"start_epoch={start_epoch} offset={start_epoch_offset}",
            flush=True,
        )
        if frozen_probe:
            print(
                "[v29 frozen-probe params] " + ",".join(trainable_names),
                flush=True,
            )
    started = time.time()
    if (
        validation_loader is not None
        and bool(config.train.get("evaluate_at_start", False))
        and state.step == 0
    ):
        if distributed:
            dist.barrier()
        metrics = evaluate(model, validation_loader, torch_device, config)
        if rank == 0:
            row = {
                "epoch": -1,
                "step": 0,
                "elapsed_s": time.time() - started,
                **metrics,
            }
            history.append(row)
            state.best_validation = metrics["val_total"]
            _save_checkpoint(
                os.path.join(out_dir, "best.pt"), model, optimizer,
                state, config, contract, history,
            )
            dump_json(os.path.join(out_dir, "metrics.json"), history)
            print(f"[v29 eval step=0] {metrics}", flush=True)
        if distributed:
            dist.barrier()
    for epoch in range(start_epoch, epochs):
        if isinstance(sampler, CoverageFirstTraceBalancedSampler):
            sampler.set_epoch(
                epoch,
                start_offset=(
                    start_epoch_offset if epoch == start_epoch else 0
                ),
            )
        elif isinstance(sampler, DistributedSampler):
            sampler.set_epoch(epoch)
        model.train()
        if frozen_probe:
            _set_frozen_memory_probe_train_mode(_raw_model(model))
        for batch in train_loader:
            if max_steps is not None and state.step >= int(max_steps):
                break
            state.step += 1
            batch = _to_device(batch, torch_device)
            optimizer.zero_grad(set_to_none=True)
            should_profile = bool(
                profile_attention
                and not profile_attention_done
                and rank == 0
                and torch_device.type == "cuda"
            )
            profile_context = (
                torch.profiler.profile(
                    activities=[
                        torch.profiler.ProfilerActivity.CPU,
                        torch.profiler.ProfilerActivity.CUDA,
                    ],
                    record_shapes=True,
                    profile_memory=True,
                ) if should_profile else nullcontext()
            )
            with profile_context as profiler:
                with torch.autocast(
                    device_type=torch_device.type,
                    dtype=amp,
                    enabled=amp is not None,
                ):
                    predictions = model(batch)
                    losses = compute_v29_losses(
                        predictions, batch, **_loss_kwargs(config),
                    )
                scaler.scale(losses.total).backward()
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(
                    trainable_parameters, gradient_clip,
                )
                scaler.step(optimizer)
                scaler.update()
            if should_profile:
                profile_attention_done = True
                print(
                    "[v29 attention-profile] "
                    f"sdpa_backend={config.model.get('sdpa_backend', 'auto')} "
                    f"amp_dtype={config.train.get('amp_dtype', 'none')}",
                    flush=True,
                )
                for event in _attention_profile_events(profiler):
                    print(f"[v29 attention-profile] {event}", flush=True)
            if losses.monotonic_violations:
                raise RuntimeError("v29 monotonic head invariant failed")
            if rank == 0 and state.step % log_every == 0:
                correction_summary = ""
                if (
                    "memory_correction_logit" in predictions
                    and "memory_correction_mask" in predictions
                ):
                    correction = predictions["memory_correction_logit"]
                    correction_mask = predictions["memory_correction_mask"].bool()
                    selected = correction[correction_mask]
                    if selected.numel():
                        correction_summary = (
                            f" corr_mean={float(selected.mean()):.5f}"
                            f" corr_abs={float(selected.abs().mean()):.5f}"
                            f" corr_pos={float((selected > 0).float().mean()):.3f}"
                            f" corr_neg={float((selected < 0).float().mean()):.3f}"
                            f" base_logit={float(predictions['base_gap_logit'][correction_mask].mean()):.5f}"
                        )
                print(
                    f"[v29 ep={epoch} step={state.step}] "
                    f"total={float(losses.total.detach()):.5f} "
                    f"time={float(losses.commit_time.detach()):.5f} "
                    f"prefix={float(losses.prefix_bce.detach()):.5f} "
                    f"count={float(losses.progress_count.detach()):.5f} "
                    f"cum={float(losses.cumulative.detach()):.5f} "
                    f"branch={float(losses.branch_token.detach()):.5f}/"
                    f"{float(losses.branch_count.detach()):.5f} "
                    f"progress_mae={float(losses.progress_mae.detach()):.3f}"
                    f"{correction_summary}",
                    flush=True,
                )
            if validation_loader is not None and eval_every > 0 and state.step % eval_every == 0:
                if distributed:
                    dist.barrier()
                metrics = evaluate(model, validation_loader, torch_device, config)
                if rank == 0:
                    row = {
                        "epoch": epoch,
                        "step": state.step,
                        "elapsed_s": time.time() - started,
                        **metrics,
                    }
                    history.append(row)
                    save_best = metrics["val_total"] < state.best_validation
                    save_post_coverage = (
                        isinstance(
                            sampler, CoverageFirstTraceBalancedSampler,
                        )
                        and state.step >= sampler_steps_per_epoch
                        and metrics["val_total"]
                        < state.best_post_coverage_validation
                    )
                    if save_best:
                        state.best_validation = metrics["val_total"]
                    if save_post_coverage:
                        state.best_post_coverage_validation = metrics["val_total"]
                    if save_best:
                        _save_checkpoint(
                            os.path.join(out_dir, "best.pt"), model, optimizer,
                            state, config, contract, history,
                        )
                    if save_post_coverage:
                        _save_checkpoint(
                            os.path.join(
                                out_dir, "best_post_coverage.pt",
                            ),
                            model, optimizer, state, config, contract, history,
                        )
                        print(
                            "[v29 checkpoint] "
                            f"best_post_coverage step={state.step} "
                            f"val_total={metrics['val_total']:.8f} "
                            f"coverage_step={sampler_steps_per_epoch}",
                            flush=True,
                        )
                    dump_json(os.path.join(out_dir, "metrics.json"), history)
                    print(f"[v29 eval step={state.step}] {metrics}", flush=True)
                if distributed:
                    dist.barrier()
                model.train()
                if frozen_probe:
                    _set_frozen_memory_probe_train_mode(_raw_model(model))
            # Save the resumable state after validation so last.pt contains
            # any best/best-post-coverage updates made at this same step.
            if rank == 0 and save_every > 0 and state.step % save_every == 0:
                _save_checkpoint(
                    os.path.join(out_dir, "last.pt"), model, optimizer, state,
                    config, contract, history,
                )
        if max_steps is not None and state.step >= int(max_steps):
            break
    if rank == 0:
        _save_checkpoint(
            os.path.join(out_dir, "last.pt"), model, optimizer, state,
            config, contract, history,
        )
        if validation_loader is None:
            _save_checkpoint(
                os.path.join(out_dir, "best.pt"), model, optimizer, state,
                config, contract, history,
            )
    if distributed:
        dist.barrier()
    return {
        "steps": state.step,
        "best_validation": state.best_validation,
        "best_post_coverage_validation": (
            state.best_post_coverage_validation
        ),
        "validation_sequences": len(validation_indices),
        "validation_sequences_total": len(validation_data) if validation_data else 0,
        "contract": contract,
        "frozen_memory_probe": frozen_probe,
        "trainable_parameters": sum(
            parameter.numel() for parameter in _raw_model(model).parameters()
            if parameter.requires_grad
        ),
    }
