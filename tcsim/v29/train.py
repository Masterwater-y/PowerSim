"""DDP-capable v29 training loop."""
from __future__ import annotations

import math
import os
import random
import time
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
from .model import build_model


@dataclass
class TrainState:
    step: int = 0
    best_validation: float = float("inf")


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
    return {key: first[key] for key in keys}


def _save_checkpoint(
    path: str,
    model: torch.nn.Module,
    optimizer: torch.optim.Optimizer,
    state: TrainState,
    config: TCSimConfig,
    contract: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
) -> None:
    torch.save({
        "checkpoint_schema": CHECKPOINT_SCHEMA_VERSION,
        "model": _raw_model(model).state_dict(),
        "optimizer": optimizer.state_dict(),
        "step": int(state.step),
        "best_validation": float(state.best_validation),
        "contract": dict(contract),
        "config": {
            "chunk": dict(config.chunk),
            "scheduler": dict(config.scheduler),
            "uarch": dict(config.uarch),
            "model": dict(config.model),
            "train": dict(config.train),
        },
        "history": list(history),
    }, path)


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
    ), list(payload.get("history", []))


def _loss_kwargs(config: TCSimConfig) -> Dict[str, Any]:
    return {
        "weights": dict(config.train.get("loss_weights", {})),
        "time_beta": float(config.train.get("time_huber_beta", 0.2)),
        "progress_count_beta": float(config.train.get("progress_count_beta", 8.0)),
        "branch_count_beta": float(config.train.get("branch_count_beta", 1.0)),
    }


@torch.no_grad()
def evaluate(
    model: torch.nn.Module,
    loader: DataLoader,
    device: torch.device,
    config: TCSimConfig,
) -> Dict[str, float]:
    model.eval()
    amp = _amp_dtype(str(config.train.get("amp_dtype", "none")), device)
    totals = torch.zeros(10, dtype=torch.float64, device=device)
    for batch in loader:
        batch = _to_device(batch, device)
        with torch.autocast(
            device_type=device.type,
            dtype=amp,
            enabled=amp is not None,
        ):
            predictions = model(batch)
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

    batch_size = int(config.train.get("batch_samples", 1))
    sampler = None
    shuffle = True
    drop_last = False
    if bool(config.train.get("trace_balanced_sampling", True)):
        weights = [
            1.0 / max(1, train_data.trace_sample_counts[trace_id])
            for trace_id in train_data.sample_trace_ids
        ]
        sampler = WeightedRandomSampler(
            weights,
            num_samples=(math.ceil(len(train_data) / world) if distributed else len(train_data)),
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
    if validation_data is not None:
        validation_loader = DataLoader(
            validation_data,
            batch_size=batch_size,
            shuffle=False,
            sampler=(range(rank, len(validation_data), world) if distributed else None),
            collate_fn=collate_v29_sequences,
            num_workers=int(config.train.get("num_workers", 0)),
            pin_memory=torch_device.type == "cuda",
        )

    model = build_model(config.model, contract["horizons"]).to(torch_device)
    if distributed:
        model = DDP(
            model,
            device_ids=[local_rank] if torch_device.type == "cuda" else None,
        )
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(config.train.get("lr", 1e-4)),
        weight_decay=float(config.train.get("weight_decay", 5e-2)),
    )
    state = TrainState()
    history: List[Dict[str, Any]] = []
    if resume:
        state, history = _load_checkpoint(
            resume, _raw_model(model), optimizer, torch_device, contract,
        )
    os.makedirs(out_dir, exist_ok=True)
    amp = _amp_dtype(str(config.train.get("amp_dtype", "none")), torch_device)
    scaler = torch.amp.GradScaler(
        "cuda", enabled=torch_device.type == "cuda" and amp == torch.float16,
    )
    epochs = int(config.train.get("epochs", 100))
    log_every = int(config.train.get("log_every", 20))
    eval_every = int(config.train.get("eval_every", 1000))
    save_every = int(config.train.get("save_every", 500))
    gradient_clip = float(config.train.get("gradient_clip", 5.0))
    if rank == 0:
        print(
            f"[v29 train] train_sequences={len(train_data)} "
            f"val_sequences={len(validation_data) if validation_data else 0} "
            f"ddp={distributed} rank={rank}/{world} device={torch_device} "
            f"start={state.step} target={max_steps}",
            flush=True,
        )
    started = time.time()
    for epoch in range(epochs):
        if isinstance(sampler, DistributedSampler):
            sampler.set_epoch(epoch)
        model.train()
        for batch in train_loader:
            if max_steps is not None and state.step >= int(max_steps):
                break
            state.step += 1
            batch = _to_device(batch, torch_device)
            optimizer.zero_grad(set_to_none=True)
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
            torch.nn.utils.clip_grad_norm_(model.parameters(), gradient_clip)
            scaler.step(optimizer)
            scaler.update()
            if losses.monotonic_violations:
                raise RuntimeError("v29 monotonic head invariant failed")
            if rank == 0 and state.step % log_every == 0:
                print(
                    f"[v29 ep={epoch} step={state.step}] total={float(losses.total):.5f} "
                    f"time={float(losses.commit_time):.5f} "
                    f"prefix={float(losses.prefix_bce):.5f} "
                    f"count={float(losses.progress_count):.5f} "
                    f"cum={float(losses.cumulative):.5f} "
                    f"branch={float(losses.branch_token):.5f}/"
                    f"{float(losses.branch_count):.5f} "
                    f"progress_mae={float(losses.progress_mae):.3f}",
                    flush=True,
                )
            if rank == 0 and save_every > 0 and state.step % save_every == 0:
                _save_checkpoint(
                    os.path.join(out_dir, "last.pt"), model, optimizer, state,
                    config, contract, history,
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
                    if metrics["val_total"] < state.best_validation:
                        state.best_validation = metrics["val_total"]
                        _save_checkpoint(
                            os.path.join(out_dir, "best.pt"), model, optimizer,
                            state, config, contract, history,
                        )
                    dump_json(os.path.join(out_dir, "metrics.json"), history)
                    print(f"[v29 eval step={state.step}] {metrics}", flush=True)
                if distributed:
                    dist.barrier()
                model.train()
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
        "contract": contract,
    }
