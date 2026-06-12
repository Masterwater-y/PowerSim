"""train_lora.py — LLMSim Phase0 训练入口（支持 8 卡 DDP + 吞吐统计）。

单卡：
  python train/train_lora.py --data data/windows/windows.jsonl --out ckpt/phase0

8 卡 DDP（推荐）：
  torchrun --nproc_per_node=8 train/train_lora.py \
      --data data/windows/windows.jsonl --out ckpt/phase0 --steps 200 --bs 2

会在 rank0 报告：
  - 每 log 间隔的 samples/sec、tokens/sec
  - 训练总 wall-time、平均吞吐
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time

import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, random_split
from torch.utils.data.distributed import DistributedSampler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.llm_wrapper import LLMSimModel, WrapperConfig, build_tokenizer
from train.dataset import WindowDataset, make_collate
from train.loss import PMULoss


def setup_ddp():
    """返回 (is_ddp, rank, local_rank, world_size)。"""
    if "RANK" in os.environ and "WORLD_SIZE" in os.environ:
        rank = int(os.environ["RANK"])
        local_rank = int(os.environ.get("LOCAL_RANK", 0))
        world = int(os.environ["WORLD_SIZE"])
        dist.init_process_group(backend="nccl")
        torch.cuda.set_device(local_rank)
        return True, rank, local_rank, world
    return False, 0, 0, 1


def is_main(rank):
    return rank == 0


def _nullcontext():
    return contextlib.nullcontext()


def dbg(rank, msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[dbg][rank {rank}][{ts}] {msg}", flush=True)


class TrainModule(torch.nn.Module):
    """把 backbone+head 与 loss 合到一个 Module，使其整体被 DDP 包裹。

    这样：
      - 训练时调用 wrapper.forward()，DDP 才会注册反向 allreduce 钩子；
      - loss 里的可学习 log_var 也作为本 Module 的参数被 DDP 同步。
    """

    def __init__(self, model, loss_fn):
        super().__init__()
        self.model = model
        self.loss_fn = loss_fn

    def forward(self, input_ids, attention_mask, query_pos, label, core_mask):
        pred = self.model(input_ids, attention_mask, query_pos)
        loss, logs = self.loss_fn(pred.float(), label, core_mask)
        return loss, logs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/ckpt/phase0")
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--bs", type=int, default=2)
    ap.add_argument("--grad-accum", type=int, default=1,
                    help="梯度累积步数：等效 batch = bs*world*grad_accum")
    ap.add_argument("--lr-lora", type=float, default=2e-4)
    ap.add_argument("--lr-head", type=float, default=1e-3)
    ap.add_argument("--lr-emb", type=float, default=1e-3)
    ap.add_argument("--max-len", type=int, default=4096)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--log-every", type=int, default=20)
    ap.add_argument("--eval-every", type=int, default=50)
    ap.add_argument("--eval-batches", type=int, default=0,
                    help="每个 rank 验证的最大 batch 数；0=全部")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    torch.manual_seed(args.seed)
    is_ddp, rank, local_rank, world = setup_ddp()
    device = f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu"
    if is_main(rank):
        os.makedirs(args.out, exist_ok=True)
        print(f"[ddp] is_ddp={is_ddp} world_size={world}", flush=True)

    tok = build_tokenizer()
    cfg = WrapperConfig(max_len=args.max_len)
    model = LLMSimModel(cfg, tok).to(device)
    loss_fn = PMULoss().to(device)
    train_module = TrainModule(model, loss_fn)
    if is_ddp:
        train_module = DDP(train_module, device_ids=[local_rank],
                           find_unused_parameters=False)
    core = model  # 始终指向底层 LLMSimModel（取参数 / 存权重用）

    cache_path = WindowDataset.default_cache_path(args.data, args.max_len)
    if is_main(rank):
        dbg(rank, f"dataset_cache_require path={cache_path}")
    ds = WindowDataset(args.data, tok, max_len=args.max_len,
                       cache_path=cache_path, require_cache=True)
    n_val = max(1, int(len(ds) * args.val_frac))
    n_train = len(ds) - n_val
    g = torch.Generator().manual_seed(args.seed)
    train_ds, val_ds = random_split(ds, [n_train, n_val], generator=g)
    if is_main(rank):
        print(f"[data] total={len(ds)} train={n_train} val={n_val}",
              flush=True)

    collate = make_collate(tok.pad_token_id)
    if is_ddp:
        train_sampler = DistributedSampler(train_ds, num_replicas=world,
                                           rank=rank, shuffle=True,
                                           drop_last=True)
        train_dl = DataLoader(train_ds, batch_size=args.bs,
                              sampler=train_sampler, collate_fn=collate,
                              num_workers=2, drop_last=True)
        val_sampler = DistributedSampler(val_ds, num_replicas=world,
                                         rank=rank, shuffle=False,
                                         drop_last=False)
        val_dl = DataLoader(val_ds, batch_size=args.bs, sampler=val_sampler,
                            collate_fn=collate, num_workers=2)
    else:
        train_sampler = None
        train_dl = DataLoader(train_ds, batch_size=args.bs, shuffle=True,
                              collate_fn=collate, num_workers=2,
                              drop_last=True)
        val_dl = DataLoader(val_ds, batch_size=args.bs, shuffle=False,
                            collate_fn=collate, num_workers=2)

    head_params = list(core.head.parameters()) + list(loss_fn.parameters())
    emb_weight = core.input_embedding.weight
    lora_params = [p for n, p in core.backbone.named_parameters()
                   if p.requires_grad and p is not emb_weight]
    optim = torch.optim.AdamW([
        {"params": lora_params, "lr": args.lr_lora},
        {"params": head_params, "lr": args.lr_head},
        {"params": [emb_weight], "lr": args.lr_emb},
    ])
    if is_main(rank):
        n_train_p = sum(p.numel() for p in core.trainable_parameters())
        n_eff = (n_train_p - emb_weight.shape[0] * emb_weight.shape[1]
                 + core.n_new_tokens * emb_weight.shape[1])
        print(f"[model] trainable params = {n_train_p/1e6:.2f}M "
              f"(effective after emb mask = {n_eff/1e6:.2f}M, "
              f"new_tokens={core.n_new_tokens})", flush=True)
        eff_bs = args.bs * world * max(1, args.grad_accum)
        print(f"[model] effective global batch = {eff_bs} "
              f"(bs={args.bs} x world={world} x accum={args.grad_accum})",
              flush=True)

    def run_val():
        """所有 rank 协同验证：各跑自己分片，再 allreduce 求全局平均。

        必须所有 rank 一起调用，否则 DDP 集合通信会死锁。
        """
        train_module.eval()
        tot = torch.zeros(1, device=device)
        cnt = torch.zeros(1, device=device)
        with torch.no_grad():
            for bi, b in enumerate(val_dl):
                if args.eval_batches and bi >= args.eval_batches:
                    break
                b = {k: v.to(device) for k, v in b.items()}
                loss, _ = train_module(b["input_ids"], b["attention_mask"],
                                       b["query_pos"], b["label"],
                                       b["core_mask"])
                tot += loss.detach().float()
                cnt += 1
        if is_ddp:
            dist.all_reduce(tot, op=dist.ReduceOp.SUM)
            dist.all_reduce(cnt, op=dist.ReduceOp.SUM)
        train_module.train()
        return (tot / cnt.clamp(min=1)).item()

    train_module.train()
    step = 0
    best = float("inf")
    epoch = 0
    t_start = time.time()
    win_t0 = time.time()
    win_samples = 0
    win_tokens = 0
    glob_samples = 0
    glob_tokens = 0

    def new_iter():
        nonlocal epoch
        if is_ddp:
            train_sampler.set_epoch(epoch)
        epoch += 1
        return iter(train_dl)

    data_iter = new_iter()

    def next_batch():
        nonlocal data_iter
        try:
            b = next(data_iter)
        except StopIteration:
            data_iter = new_iter()
            b = next(data_iter)
        return {k: v.to(device) for k, v in b.items()}

    accum = max(1, args.grad_accum)
    while step < args.steps:
        optim.zero_grad()
        last_logs = None
        micro_samples = 0
        micro_tokens = 0
        for micro in range(accum):
            b = next_batch()
            micro_samples += b["input_ids"].size(0)
            micro_tokens += int(b["attention_mask"].sum().item())
            is_last = (micro == accum - 1)
            # 非最后一个 micro-step 用 no_sync 跳过 allreduce，最后一步才同步梯度
            sync_ctx = (train_module.no_sync()
                        if is_ddp and not is_last
                        else _nullcontext())
            with sync_ctx:
                loss, logs = train_module(b["input_ids"], b["attention_mask"],
                                          b["query_pos"], b["label"],
                                          b["core_mask"])
                (loss / accum).backward()
            last_logs = logs
        torch.nn.utils.clip_grad_norm_(core.trainable_parameters(), 1.0)
        optim.step()
        logs = last_logs
        step += 1
        # 全局吞吐（× world_size 近似全局 batch）
        win_samples += micro_samples * world
        win_tokens += micro_tokens * world
        glob_samples += micro_samples * world
        glob_tokens += micro_tokens * world

        if step % args.log_every == 0 and is_main(rank):
            if torch.cuda.is_available():
                torch.cuda.synchronize(device)
            dt = time.time() - win_t0
            sps = win_samples / dt if dt > 0 else 0
            tps = win_tokens / dt if dt > 0 else 0
            print(f"[step {step}] loss={logs['loss'].item():.4f} "
                  f"L_cpi={logs['L_cpi'].item():.4f} "
                  f"L_inv={logs['L_inv'].item():.4f} | "
                  f"throughput: {sps:.1f} samp/s, {tps:.0f} tok/s",
                  flush=True)
            win_t0 = time.time(); win_samples = 0; win_tokens = 0
        if step % args.eval_every == 0:
            dbg(rank, f"eval_gate step={step} is_main={is_main(rank)}")
            vl = run_val()  # 所有 rank 必须一起调用
            if is_main(rank):
                print(f"[eval step {step}] val_loss={vl:.4f}", flush=True)
                if vl < best:
                    best = vl
                    dbg(rank, f"save_best_start step={step}")
                    new_emb = core.input_embedding.weight.detach()[
                        core.new_token_start:].cpu().clone()
                    torch.save({
                        "head": core.head.state_dict(),
                        "log_var": loss_fn.log_var.detach().cpu(),
                        "new_token_start": core.new_token_start,
                        "n_new_tokens": core.n_new_tokens,
                        "new_token_embedding": new_emb,
                        "step": step, "val_loss": vl,
                    }, os.path.join(args.out, "head_best.pt"))
                    core.backbone.save_pretrained(
                        os.path.join(args.out, "lora_best"))
                    dbg(rank, f"save_best_done step={step}")
            if is_ddp:
                dist.barrier()  # 等 rank0 存盘完成，保持各 rank 同步

    dbg(rank, f"train_loop_done step={step}/{args.steps}")

    if is_ddp:
        dbg(rank, "enter_barrier")
        dist.barrier()
        dbg(rank, "leave_barrier")
    if is_main(rank):
        if torch.cuda.is_available():
            torch.cuda.synchronize(device)
        total_dt = time.time() - t_start
        print("=" * 60, flush=True)
        print(f"[DONE] steps={args.steps} world_size={world} "
              f"best_val_loss={best:.4f}", flush=True)
        print(f"[WALL] total_time={total_dt:.1f}s "
              f"({total_dt/max(args.steps,1)*1000:.1f} ms/step)", flush=True)
        print(f"[THROUGHPUT-avg] {glob_samples/total_dt:.1f} samp/s, "
              f"{glob_tokens/total_dt:.0f} tok/s "
              f"(global over {world} GPUs)", flush=True)
        print("=" * 60, flush=True)
    if is_ddp:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
