"""train_phase1.py — Phase 1 semantic-gate training loop.

Single-file DDP training for the Phase 1 semantic gate.  Trains one variant
(``--variant real|pseudo|shuffle|register_rename|side_only``) and evaluates on
family_ood c01 (primary gate) and seed_ood c04 (secondary diagnostic).

Usage (single-GPU smoke):
  /data00/yinhaolang/infer/.venv/bin/python train/train_phase1.py \\
      --variant real --max-steps 200 --batch-size 2 --output ckpt/phase1_real_smoke

DDP (8 GPUs, full 8000 step run):
  /data00/yinhaolang/infer/.venv/bin/torchrun --standalone --nproc-per-node=8 \\
      train/train_phase1.py --variant real --max-steps 8000 --batch-size 4 \\
      --output ckpt/phase1_real_8000
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import torch
import torch.distributed as dist
import torch.nn.functional as F
from torch.utils.data import DataLoader, DistributedSampler

_REPO = "/data00/yinhaolang/LLMSim"
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from model.phase1_model import Phase1Model, Phase1ModelConfig, phase1_loss  # noqa: E402
from train.dataset_phase1 import (  # noqa: E402
    Phase1Config, Phase1MacroChunkDataset, collate_phase1, VARIANTS,
)


def _is_ddp() -> bool:
    return "LOCAL_RANK" in os.environ


def _init_ddp() -> Tuple[int, int, int]:
    """Init NCCL DDP.  Returns (rank, world_size, local_rank)."""
    if not _is_ddp():
        return 0, 1, 0
    if not dist.is_initialized():
        dist.init_process_group(backend="nccl", init_method="env://")
    local_rank = int(os.environ.get("LOCAL_RANK", 0))
    torch.cuda.set_device(local_rank)
    return dist.get_rank(), dist.get_world_size(), local_rank


def _wape(pred: torch.Tensor, tgt: torch.Tensor, valid: torch.Tensor) -> float:
    m = valid.float()
    if m.sum() < 1:
        return float("nan")
    num = (torch.abs(pred - tgt) * m).sum().item()
    den = (torch.abs(tgt) * m).sum().item()
    return float(num / max(1e-6, den))


def _mape(pred: torch.Tensor, tgt: torch.Tensor, valid: torch.Tensor) -> float:
    m = valid.float()
    if m.sum() < 1:
        return float("nan")
    err = (torch.abs(pred - tgt) / tgt.clamp_min(1e-4)) * m
    return float((err.sum() / m.sum().clamp_min(1.0)).item())


def build_loader(cfg: Phase1Config, tokenizer, batch_size: int, world_size: int,
                 rank: int, shuffle: bool, num_workers: int) -> DataLoader:
    ds = Phase1MacroChunkDataset(cfg, tokenizer)
    if world_size > 1:
        sampler = DistributedSampler(ds, num_replicas=world_size, rank=rank,
                                      shuffle=shuffle, drop_last=False)
    else:
        sampler = None
    return DataLoader(
        ds, batch_size=batch_size, sampler=sampler,
        shuffle=(sampler is None and shuffle),
        num_workers=num_workers, pin_memory=True,
        collate_fn=lambda b: collate_phase1(b, pad_id=tokenizer.pad_token_id or 0),
        drop_last=False, persistent_workers=(num_workers > 0),
    )


def evaluate(model: torch.nn.Module, loader: DataLoader, device: torch.device,
             max_batches: int = 0) -> Dict[str, float]:
    model.eval()
    preds: List[float] = []
    tgts: List[float] = []
    valids: List[float] = []
    with torch.no_grad():
        for bi, batch in enumerate(loader):
            if max_batches and bi >= max_batches:
                break
            for k, v in list(batch.items()):
                if isinstance(v, torch.Tensor):
                    batch[k] = v.to(device, non_blocking=True)
            out = model(batch)
            pred = out["pred_log_cpi_macro"].float().detach().cpu()
            tgt_log = torch.log(batch["cpi_macro"].clamp_min(1e-4)).float().detach().cpu()
            tgt = batch["cpi_macro"].float().detach().cpu()
            valid = batch["valid"].float().detach().cpu()
            pred_lin = pred.exp()
            preds.append(pred_lin)
            tgts.append(tgt)
            valids.append(valid)
    if not preds:
        return {"n": 0}
    p = torch.cat(preds)
    y = torch.cat(tgts)
    v = torch.cat(valids)
    return {
        "n": int(v.sum().item()),
        "wape": _wape(p, y, v),
        "mape": _mape(p, y, v),
        "mean_pred": float((p * v).sum().item() / max(1.0, v.sum().item())),
        "mean_label": float((y * v).sum().item() / max(1.0, v.sum().item())),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="/data00/yinhaolang/LLMSim/data/v28_1/manifest.parquet")
    ap.add_argument("--chunks-root", default="/data00/yinhaolang/LLMSim/data/v28_1/chunks")
    ap.add_argument("--prompts-root", default="/data00/yinhaolang/LLMSim/data/v28_1/prompts")
    ap.add_argument("--variant", choices=VARIANTS, default="real")
    ap.add_argument("--cores", default="1", help="comma-separated core counts for train")
    ap.add_argument("--val-cores", default="1")
    ap.add_argument("--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct")
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--batch-size", type=int, default=2)
    ap.add_argument("--max-steps", type=int, default=200)
    ap.add_argument("--warmup-frac", type=float, default=0.03)
    ap.add_argument("--lr-lora", type=float, default=1e-4)
    ap.add_argument("--lr-head", type=float, default=5e-4)
    ap.add_argument("--weight-decay", type=float, default=0.05)
    ap.add_argument("--grad-clip", type=float, default=1.0)
    ap.add_argument("--huber-delta", type=float, default=0.3)
    ap.add_argument("--eval-every", type=int, default=200)
    ap.add_argument("--save-every", type=int, default=2000,
                    help="save trainable-only checkpoint every N steps; final "
                         "checkpoint always saved at end")
    ap.add_argument("--log-every", type=int, default=20)
    ap.add_argument("--num-workers", type=int, default=2)
    ap.add_argument("--dtype", default="bf16")
    ap.add_argument("--output", required=True)
    ap.add_argument("--eval-only", action="store_true")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    rank, world_size, local_rank = _init_ddp()
    device = torch.device(f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu")
    is_main = (rank == 0)
    torch.manual_seed(args.seed + rank)
    random.seed(args.seed + rank)

    os.makedirs(args.output, exist_ok=True)
    if is_main:
        with open(os.path.join(args.output, "args.json"), "w") as fh:
            json.dump(vars(args), fh, indent=2)

    # Tokenizer
    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.base_model)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    train_cores = [int(c) for c in args.cores.split(",") if c]
    val_cores = [int(c) for c in args.val_cores.split(",") if c]

    train_cfg = Phase1Config(
        manifest_path=args.manifest,
        chunks_root=args.chunks_root,
        prompts_root=args.prompts_root,
        split="train",
        cores=train_cores,
        variant=args.variant,
        max_tokens=args.max_tokens,
        tokenizer_name=args.base_model,
        seed=args.seed,
    )
    train_loader = None
    if not args.eval_only:
        train_loader = build_loader(train_cfg, tok, args.batch_size, world_size,
                                    rank, shuffle=True, num_workers=args.num_workers)
        if is_main:
            print(f"[phase1] train samples = {len(train_loader.dataset)} "
                  f"(cores={train_cores}, variant={args.variant})", flush=True)

    val_family_cfg = Phase1Config(
        manifest_path=args.manifest, chunks_root=args.chunks_root,
        prompts_root=args.prompts_root, split="family_ood",
        cores=val_cores, variant=args.variant,
        max_tokens=args.max_tokens, tokenizer_name=args.base_model, seed=args.seed,
    )
    val_family_loader = build_loader(val_family_cfg, tok, args.batch_size,
                                      world_size, rank, shuffle=False,
                                      num_workers=args.num_workers)

    val_seed_loader = None
    val_seed_cfg = Phase1Config(
        manifest_path=args.manifest, chunks_root=args.chunks_root,
        prompts_root=args.prompts_root, split="seed_ood",
        cores=val_cores, variant=args.variant,
        max_tokens=args.max_tokens, tokenizer_name=args.base_model, seed=args.seed,
    )
    # Only rank 0 pays the cost of scanning all seed_ood chunks; broadcast the
    # decision so the other ranks skip that ~30s dataset-construction pass.
    has_seed_ood = 0
    if is_main:
        try:
            has_seed_ood = 1 if len(
                Phase1MacroChunkDataset(val_seed_cfg, tok).index
            ) > 0 else 0
        except Exception:
            has_seed_ood = 0
    if world_size > 1:
        flag = torch.tensor([int(has_seed_ood)], device=device)
        dist.broadcast(flag, src=0)
        has_seed_ood = int(flag.item())
    if has_seed_ood:
        val_seed_loader = build_loader(val_seed_cfg, tok, args.batch_size,
                                        world_size, rank, shuffle=False,
                                        num_workers=args.num_workers)

    # Model
    model_cfg = Phase1ModelConfig(
        base_model=args.base_model,
        side_only=(args.variant == "side_only"),
        dtype=args.dtype,
    )
    model = Phase1Model(model_cfg).to(device)
    if world_size > 1:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[local_rank], output_device=local_rank,
            find_unused_parameters=True,
        )
    trainable = [p for p in model.parameters() if p.requires_grad]
    # Separate head-vs-lora LR groups.
    head_params, lora_params = [], []
    core = model.module if hasattr(model, "module") else model
    for name, p in core.named_parameters():
        if not p.requires_grad:
            continue
        if "head" in name or "dyn_enc" in name or "static_proj" in name or "macro_norm" in name:
            head_params.append(p)
        else:
            lora_params.append(p)
    optim = torch.optim.AdamW(
        [
            {"params": head_params, "lr": args.lr_head},
            {"params": lora_params, "lr": args.lr_lora},
        ],
        weight_decay=args.weight_decay,
    )
    warmup_steps = max(1, int(args.max_steps * args.warmup_frac))
    def lr_lambda(step: int) -> float:
        if step < warmup_steps:
            return float(step + 1) / warmup_steps
        progress = (step - warmup_steps) / max(1, args.max_steps - warmup_steps)
        return 0.5 * (1.0 + math.cos(math.pi * progress))
    scheduler = torch.optim.lr_scheduler.LambdaLR(optim, lr_lambda)

    if args.eval_only:
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=True):
            fam = evaluate(model, val_family_loader, device)
            seed = evaluate(model, val_seed_loader, device) if val_seed_loader else {"n": 0}
        if is_main:
            print(f"[phase1 eval] family_ood={fam} seed_ood={seed}", flush=True)
        return 0

    global_step = 0
    t0 = time.time()
    while global_step < args.max_steps:
        if isinstance(train_loader.sampler, DistributedSampler):
            train_loader.sampler.set_epoch(global_step)
        for batch in train_loader:
            if global_step >= args.max_steps:
                break
            for k, v in list(batch.items()):
                if isinstance(v, torch.Tensor):
                    batch[k] = v.to(device, non_blocking=True)
            model.train()
            with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=True):
                out = model(batch)
                loss = phase1_loss(
                    pred=out["pred_log_cpi_macro"],
                    target=batch["cpi_macro"],
                    valid=batch["valid"],
                    huber_delta=args.huber_delta,
                )
            if not torch.isfinite(loss):
                if is_main:
                    print(f"[phase1] step {global_step}: non-finite loss, skipping", flush=True)
                optim.zero_grad(set_to_none=True)
                global_step += 1
                continue
            loss.backward()
            torch.nn.utils.clip_grad_norm_(trainable, args.grad_clip)
            optim.step()
            scheduler.step()
            optim.zero_grad(set_to_none=True)
            if is_main and global_step % args.log_every == 0:
                dt = time.time() - t0
                lr = optim.param_groups[0]["lr"]
                print(f"[phase1] step {global_step} loss={loss.item():.4f} lr_head={lr:.2e} "
                      f"dt={dt:.1f}s", flush=True)
            if (global_step + 1) % args.eval_every == 0 or global_step == args.max_steps - 1:
                with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=True):
                    fam = evaluate(model, val_family_loader, device, max_batches=64)
                if is_main:
                    print(f"[phase1 val step={global_step}] family_ood={fam}", flush=True)
            if is_main and (global_step + 1) % args.save_every == 0:
                _save_ckpt(core, args.output, global_step)
            global_step += 1
    if is_main:
        _save_ckpt(core, args.output, global_step)
        with torch.autocast(device_type="cuda", dtype=torch.bfloat16, enabled=True):
            fam = evaluate(model, val_family_loader, device)
            seed = evaluate(model, val_seed_loader, device) if val_seed_loader else {"n": 0}
        report = {"variant": args.variant, "family_ood": fam, "seed_ood": seed,
                  "steps": global_step, "elapsed_s": time.time() - t0}
        with open(os.path.join(args.output, "final_report.json"), "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"[phase1 done] {json.dumps(report)}", flush=True)
    if world_size > 1:
        dist.barrier()
        dist.destroy_process_group()
    return 0


def _save_ckpt(core: torch.nn.Module, out_dir: str, step: int) -> None:
    """Save only trainable parameters (LoRA + head + dyn_enc + projs).

    Full-model state_dict for Qwen2.5-Coder-1.5B is ~3 GB; the frozen backbone
    is identical for every checkpoint we could produce, so storing it every
    500 steps would waste disk. LoRA adapters + dynamic encoder + head is
    around 30 MB and is all we need to resume/eval.
    """
    trainable_state = {
        k: v.detach().cpu()
        for k, v in core.state_dict().items()
        if any(p is v for p in core.parameters() if p.requires_grad)
    }
    # peft named-parameters use ``.default.weight`` suffixes; catch by requires_grad.
    if not trainable_state:
        # fallback: include everything that peft would export as trainable
        trainable_state = {
            name: p.detach().cpu()
            for name, p in core.named_parameters()
            if p.requires_grad
        }
    path = os.path.join(out_dir, f"trainable_step{step}.pt")
    tmp = path + ".tmp"
    torch.save({"state_dict": trainable_state, "step": step,
                "note": "trainable-only (LoRA + head + dyn_enc + projs)"},
               tmp)
    os.replace(tmp, path)
    latest = os.path.join(out_dir, "trainable_latest.pt")
    try:
        if os.path.exists(latest) or os.path.islink(latest):
            os.remove(latest)
    except FileNotFoundError:
        pass
    os.symlink(os.path.basename(path), latest)


if __name__ == "__main__":
    sys.exit(main())
