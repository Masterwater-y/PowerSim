"""Training loop for the TCSim MVP."""
from __future__ import annotations

from contextlib import nullcontext
import json
import math
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, WeightedRandomSampler
from torch.utils.data.distributed import DistributedSampler

from ..dataset.torch_dataset import TCSimSampleDataset, collate_variable_active
from ..model.tcsim_model import TCSimModel, StaticEmbeddingCache
from ..utils.config import TCSimConfig
from ..utils.io import dump_json
from .losses import compute_losses


@dataclass
class TrainState:
    step: int = 0
    best_val: float = float("inf")


def build_model_from_cfg(cfg: TCSimConfig) -> TCSimModel:
    return TCSimModel(
        d_field=int(cfg.model.get("d_field", 16)),
        d_static=int(cfg.model.get("d_static", 128)),
        d_dyn=int(cfg.model.get("d_dyn", 128)),
        n_heads=int(cfg.model.get("n_dyn_heads", 4)),
        n_layers=int(cfg.model.get("n_dyn_layers", 1)),
        ffn_dim=(
            int(cfg.model["ffn_dim"])
            if cfg.model.get("ffn_dim") is not None else None
        ),
        cross_target_block=int(cfg.model.get("cross_target_block", 0)),
        sdpa_backend=str(cfg.model.get("sdpa_backend", "auto")),
        dropout=float(cfg.model.get("dropout", 0.1)),
        max_K=int(cfg.chunk.get("K", 256)) + 32,
    )


def _to_device(batch: Dict, device) -> Dict:
    # sample_ptr is layout/control metadata.  The model and losses use it only
    # to form Python slice ranges, so moving it to CUDA would force a device
    # synchronization for every range lookup.
    cpu_control_keys = {"sample_ptr"}
    out = {}
    for k, v in batch.items():
        if isinstance(v, torch.Tensor) and k not in cpu_control_keys:
            out[k] = v.to(device)
        else:
            out[k] = v
    return out


def _setup_distributed(device: str) -> Tuple[bool, int, int, int, torch.device]:
    is_ddp = "RANK" in os.environ and "WORLD_SIZE" in os.environ
    rank = int(os.environ.get("RANK", "0"))
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    world = int(os.environ.get("WORLD_SIZE", "1"))
    use_cuda = str(device).startswith("cuda") or str(device) == "auto"
    backend = "nccl" if torch.cuda.is_available() and use_cuda else "gloo"
    if is_ddp and not dist.is_initialized():
        dist.init_process_group(backend=backend)
    if torch.cuda.is_available() and use_cuda:
        torch.cuda.set_device(local_rank)
        torch_device = torch.device(f"cuda:{local_rank}")
    elif str(device) == "auto" and torch.cuda.is_available():
        torch_device = torch.device("cuda")
    else:
        torch_device = torch.device(device)
    return is_ddp, rank, local_rank, world, torch_device


def _barrier(is_ddp: bool) -> None:
    if is_ddp and dist.is_initialized():
        dist.barrier()


def _model_state_dict(model: torch.nn.Module) -> Dict[str, torch.Tensor]:
    module = model.module if isinstance(model, DDP) else model
    return module.state_dict()


def _save_checkpoint(
    path: str,
    model: torch.nn.Module,
    opt: torch.optim.Optimizer,
    state: TrainState,
    metrics_history: List[dict],
    cfg: TCSimConfig,
) -> None:
    torch.save(
        {
            "model": _model_state_dict(model),
            "optimizer": opt.state_dict(),
            "step": int(state.step),
            "best_val": float(state.best_val),
            "metrics_history": list(metrics_history),
            "config": {
                "chunk": cfg.chunk,
                "scheduler": cfg.scheduler,
                "uarch": cfg.uarch,
                "model": cfg.model,
                "train": cfg.train,
            },
        },
        path,
    )


def _load_checkpoint(
    path: str,
    model: torch.nn.Module,
    opt: Optional[torch.optim.Optimizer],
    device: torch.device,
) -> Tuple[int, float, List[dict]]:
    payload = torch.load(path, map_location=device)
    if isinstance(payload, dict) and "model" in payload:
        model.load_state_dict(payload["model"])
        if opt is not None and payload.get("optimizer"):
            opt.load_state_dict(payload["optimizer"])
        return (
            int(payload.get("step", 0) or 0),
            float(payload.get("best_val", float("inf"))),
            list(payload.get("metrics_history", [])),
        )
    model.load_state_dict(payload)
    return 0, float("inf"), []


def _amp_dtype(name: str, device: torch.device):
    if device.type != "cuda":
        return None
    value = str(name or "none").lower()
    if value in {"none", "off", "fp32", "float32"}:
        return None
    if value in {"bf16", "bfloat16"}:
        return torch.bfloat16
    if value in {"fp16", "float16", "half"}:
        return torch.float16
    raise ValueError(f"unsupported amp_dtype={name}")


def _attention_profile_events(profiler) -> List[str]:
    """Return concise evidence of the attention kernels selected by PyTorch."""
    events: List[str] = []
    for event in profiler.key_averages():
        key = str(event.key)
        low = key.lower()
        if "scaled_dot_product" not in low and "attention" not in low:
            continue
        cpu_us = float(getattr(event, "self_cpu_time_total", 0.0) or 0.0)
        cuda_us = float(getattr(event, "self_cuda_time_total", 0.0) or 0.0)
        events.append(f"{key} cpu_us={cpu_us:.0f} cuda_us={cuda_us:.0f}")
    return sorted(events)


def train_one_run(
    train_dirs: List[str],
    val_dirs: List[str],
    out_dir: str,
    cfg: TCSimConfig,
    device: str = "cpu",
    verbose: bool = True,
    max_steps: Optional[int] = None,
    resume_path: Optional[str] = None,
) -> Dict[str, float]:
    is_ddp, rank, local_rank, world, torch_device = _setup_distributed(device)
    verbose = bool(verbose and rank == 0)
    os.makedirs(out_dir, exist_ok=True)
    train_ds = TCSimSampleDataset(train_dirs)
    val_ds = TCSimSampleDataset(val_dirs) if val_dirs else None
    if verbose:
        print(
            f"[train] train_sources={len(train_dirs)} train_samples={len(train_ds)} "
            f"val_sources={len(val_dirs)} val_samples={len(val_ds) if val_ds else 0}"
        )

    batch_samples = int(cfg.train.get("batch_samples", 8))
    sampler = None
    shuffle = True
    drop_last = False
    if bool(cfg.train.get("trace_balanced_sampling", True)):
        sample_weights = [
            1.0 / max(1, int(train_ds.trace_sample_counts[trace_id]))
            for trace_id in train_ds.sample_trace_ids
        ]
        sampler = WeightedRandomSampler(
            sample_weights,
            num_samples=(
                math.ceil(len(train_ds) / max(1, world))
                if is_ddp else len(train_ds)
            ),
            replacement=True,
            generator=torch.Generator().manual_seed(
                int(cfg.train.get("seed", 1234)) + rank
            ),
        )
        shuffle = False
        drop_last = True if is_ddp else False
    elif is_ddp:
        sampler = DistributedSampler(
            train_ds, num_replicas=world, rank=rank, shuffle=True, drop_last=True,
        )
        shuffle = False
        drop_last = True
    train_loader = DataLoader(
        train_ds,
        batch_size=batch_samples,
        shuffle=shuffle and sampler is None,
        sampler=sampler,
        collate_fn=collate_variable_active,
        num_workers=int(cfg.train.get("num_workers", 0)),
        drop_last=drop_last,
        pin_memory=torch_device.type == "cuda",
    )
    val_loader = (
        DataLoader(
            val_ds,
            batch_size=batch_samples,
            shuffle=False,
            # Do not use DistributedSampler here: its padding would duplicate
            # validation samples.  A strided range gives every rank a
            # disjoint, deterministic subset and collectively covers val_ds.
            sampler=(range(rank, len(val_ds), world) if is_ddp else None),
            collate_fn=collate_variable_active,
            num_workers=int(cfg.train.get("num_workers", 0)),
            pin_memory=torch_device.type == "cuda",
        )
        if val_ds is not None
        else None
    )

    model = build_model_from_cfg(cfg).to(torch_device)
    param_count = sum(p.numel() for p in model.parameters())
    trainable_count = sum(p.numel() for p in model.parameters() if p.requires_grad)
    resume_path = resume_path or os.environ.get("RESUME_CKPT") or os.environ.get("INIT_CKPT")
    state = TrainState()
    metrics_history: List[dict] = []
    if resume_path:
        state.step, state.best_val, metrics_history = _load_checkpoint(
            resume_path, model, None, torch_device,
        )
    if is_ddp:
        model = DDP(
            model,
            device_ids=[local_rank] if torch_device.type == "cuda" else None,
        )
    opt = torch.optim.AdamW(
        model.parameters(),
        lr=float(cfg.train.get("lr", 3e-4)),
        weight_decay=float(cfg.train.get("weight_decay", 1e-4)),
    )
    if resume_path:
        state.step, state.best_val, metrics_history = _load_checkpoint(
            resume_path,
            model.module if isinstance(model, DDP) else model,
            opt,
            torch_device,
        )
    weights = cfg.train.get("loss_weights", {})
    prefix_lens = list(cfg.train.get("prefix_lens", [4, 8, 16, 32]))
    huber_log = float(cfg.train.get("huber_delta_log", 0.3))
    spread_threshold = float(cfg.train.get("centered_spread_threshold", 0.10))
    epochs = int(cfg.train.get("epochs", 5))
    eval_every = int(cfg.train.get("eval_every", 0) or 0)
    save_every = int(cfg.train.get("save_every", 500) or 0)
    log_every = int(cfg.train.get("log_every", 20) or 20)
    amp_dtype = _amp_dtype(str(cfg.train.get("amp_dtype", "none")), torch_device)
    profile_attention = bool(cfg.train.get("profile_attention", False))
    profile_attention_done = False
    scaler = torch.amp.GradScaler(
        "cuda",
        enabled=torch_device.type == "cuda" and amp_dtype == torch.float16,
    )

    if verbose:
        print(
            f"[train] ddp={is_ddp} rank={rank}/{world} device={torch_device} "
            f"params={param_count:,} trainable={trainable_count:,} "
            f"start_step={state.step} target={max_steps} "
            f"sdpa={cfg.model.get('sdpa_backend', 'auto')} "
            f"amp={cfg.train.get('amp_dtype', 'none')}"
        )
    for epoch in range(epochs):
        if isinstance(sampler, DistributedSampler):
            sampler.set_epoch(epoch)
        model.train()
        t0 = time.time()
        for batch in train_loader:
            if max_steps and state.step >= max_steps:
                break
            state.step += 1
            batch = _to_device(batch, torch_device)
            opt.zero_grad()
            should_profile = (
                profile_attention
                and not profile_attention_done
                and rank == 0
                and torch_device.type == "cuda"
            )
            profile_ctx = (
                torch.profiler.profile(
                    activities=[torch.profiler.ProfilerActivity.CPU,
                                torch.profiler.ProfilerActivity.CUDA],
                    record_shapes=True,
                    profile_memory=True,
                ) if should_profile else nullcontext()
            )
            with profile_ctx as profiler:
                with torch.autocast(
                    device_type=torch_device.type,
                    dtype=amp_dtype,
                    enabled=amp_dtype is not None,
                ):
                    preds = model(batch)
                    loss_out = compute_losses(
                        preds, batch,
                        weights=weights,
                        huber_delta_log=huber_log,
                        prefix_lens=prefix_lens,
                        centered_spread_threshold=spread_threshold,
                    )
                    loss = (
                        loss_out.total
                        if loss_out.n_committed > 0
                        else preds["log_cpi"].sum() * 0.0
                    )
                scaler.scale(loss).backward()
                scaler.unscale_(opt)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
                scaler.step(opt)
                scaler.update()
            if should_profile:
                profile_attention_done = True
                events = _attention_profile_events(profiler)
                print(
                    "[attention-profile] "
                    f"sdpa_backend={cfg.model.get('sdpa_backend', 'auto')} "
                    f"amp_dtype={cfg.train.get('amp_dtype', 'none')}"
                )
                for event in events:
                    print(f"[attention-profile] {event}")
            if verbose and state.step % log_every == 0:
                print(
                    f"[ep {epoch} step {state.step}] "
                    f"loss={float(loss_out.total):.4f} "
                    f"abs={float(loss_out.log_cpi):.4f} "
                    f"center={float(loss_out.centered):.4f} "
                    f"branch={float(loss_out.branch_miss):.4f} "
                    f"prefix={float(loss_out.prefix):.4f} "
                    f"spread={float(loss_out.pred_spread):.3f}/{float(loss_out.true_spread):.3f} "
                    f"n_committed={loss_out.n_committed}"
                )
            if rank == 0 and save_every and state.step % save_every == 0:
                _save_checkpoint(
                    os.path.join(out_dir, "last.pt"),
                    model, opt, state, metrics_history, cfg,
                )
            if eval_every and val_loader is not None and state.step % eval_every == 0:
                _barrier(is_ddp)
                # Every DDP rank evaluates its own disjoint validation shard.
                # evaluate() all-reduces the metric accumulators so rank 0
                # receives the same global result as a serial full pass.
                val_metric = evaluate(model, val_loader, torch_device, cfg)
                if rank == 0:
                    row = {
                        "epoch": epoch,
                        "step": state.step,
                        "elapsed_s": time.time() - t0,
                    }
                    row.update(val_metric)
                    if val_metric and val_metric["val_total"] < state.best_val:
                        state.best_val = val_metric["val_total"]
                        _save_checkpoint(
                            os.path.join(out_dir, "best.pt"),
                            model, opt, state, metrics_history, cfg,
                        )
                    metrics_history.append(row)
                    dump_json(os.path.join(out_dir, "metrics.json"), metrics_history)
                    print(f"[eval step {state.step}] {val_metric}")
                _barrier(is_ddp)
                model.train()
            if max_steps and state.step >= max_steps:
                break
        val_metric = None
        if val_loader is not None and eval_every <= 0:
            _barrier(is_ddp)
            if rank == 0:
                raw_model = model.module if isinstance(model, DDP) else model
                val_metric = evaluate(raw_model, val_loader, torch_device, cfg)
            _barrier(is_ddp)
        if rank == 0:
            row = {"epoch": epoch, "step": state.step, "elapsed_s": time.time() - t0}
            if val_metric is not None:
                row.update(val_metric)
                if val_metric["val_total"] < state.best_val:
                    state.best_val = val_metric["val_total"]
                    _save_checkpoint(
                        os.path.join(out_dir, "best.pt"),
                        model, opt, state, metrics_history, cfg,
                    )
            metrics_history.append(row)
            dump_json(os.path.join(out_dir, "metrics.json"), metrics_history)
            _save_checkpoint(
                os.path.join(out_dir, "last.pt"),
                model, opt, state, metrics_history, cfg,
            )
        if max_steps and state.step >= max_steps:
            break

    if rank == 0:
        _save_checkpoint(
            os.path.join(out_dir, "last.pt"),
            model, opt, state, metrics_history, cfg,
        )
    if rank == 0 and val_loader is None:
        _save_checkpoint(
            os.path.join(out_dir, "best.pt"),
            model, opt, state, metrics_history, cfg,
        )
        state.best_val = float("nan")
    _barrier(is_ddp)
    return {"steps": state.step, "best_val_total": state.best_val}


def evaluate(model: torch.nn.Module, loader, device: torch.device, cfg: TCSimConfig) -> Dict[str, float]:
    if loader is None:
        return {}
    model.eval()
    weights = cfg.train.get("loss_weights", {})
    prefix_lens = list(cfg.train.get("prefix_lens", [4, 8, 16, 32]))
    huber_log = float(cfg.train.get("huber_delta_log", 0.3))
    spread_threshold = float(cfg.train.get("centered_spread_threshold", 0.10))
    tot = 0.0
    lc = 0.0
    centered = 0.0
    pf = 0.0
    ep = 0.0
    bm = 0.0
    n = 0
    per_core_mape_sum = 0.0
    per_core_mape_n = 0
    amp_dtype = _amp_dtype(str(cfg.train.get("amp_dtype", "none")), device)
    with torch.no_grad():
        for batch in loader:
            batch = _to_device(batch, device)
            with torch.autocast(
                device_type=device.type,
                dtype=amp_dtype,
                enabled=amp_dtype is not None,
            ):
                preds = model(batch)
                loss_out = compute_losses(
                    preds, batch,
                    weights=weights,
                    huber_delta_log=huber_log,
                    prefix_lens=prefix_lens,
                    centered_spread_threshold=spread_threshold,
                )
            if loss_out.n_committed == 0:
                continue
            tot += float(loss_out.total)
            lc += float(loss_out.log_cpi)
            centered += float(loss_out.centered)
            pf += float(loss_out.prefix)
            ep += float(loss_out.endpoint)
            bm += float(loss_out.branch_miss)
            n += 1
            m = batch["label_mask"].bool()
            if m.sum().item() > 0:
                p = preds["pred_delta_cycles"][m]
                t = batch["delta_cycles"][m].clamp(min=1.0)
                per_core_mape_sum += float(((p - t).abs() / t).sum())
                per_core_mape_n += int(m.sum().item())
    # Keep all ranks in this collective, including a possible empty local
    # shard, then report the globally reduced validation metric on rank 0.
    totals = torch.tensor(
        [tot, lc, centered, pf, ep, bm, float(n), per_core_mape_sum, float(per_core_mape_n)],
        device=device,
        dtype=torch.float64,
    )
    if dist.is_available() and dist.is_initialized():
        dist.all_reduce(totals, op=dist.ReduceOp.SUM)
    tot, lc, centered, pf, ep, bm, n, per_core_mape_sum, per_core_mape_n = totals.tolist()
    if n <= 0:
        return {}
    return {
        "val_total": tot / n,
        "val_log_cpi": lc / n,
        "val_centered": centered / n,
        "val_prefix": pf / n,
        "val_endpoint": ep / n,
        "val_branch_miss": bm / n,
        "val_per_core_mape": per_core_mape_sum / max(1, per_core_mape_n),
    }
