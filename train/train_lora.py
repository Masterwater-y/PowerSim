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
import math
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
from model import tokenizer as tk
from model.regression_head import PMU_KEYS
from train.dataset import WindowDataset, make_collate
from train.loss import PMULoss

LABEL_VERSION = "v12_summary_pack_split_heads_l2_no_mshr_no_iside"


class SkipFirstEpochSampler:
    """Wrap a PyTorch sampler and skip already-consumed samples by index.

    This is used for interrupted DDP runs. It avoids physically iterating
    through thousands of cached batches just to reach the previous position.
    """

    def __init__(self, base_sampler, skip_samples: int = 0):
        self.base_sampler = base_sampler
        self.skip_samples = max(0, int(skip_samples))
        self.epoch = 0

    def set_epoch(self, epoch: int) -> None:
        self.epoch = int(epoch)
        if hasattr(self.base_sampler, "set_epoch"):
            self.base_sampler.set_epoch(epoch)

    def _remaining_skip(self) -> int:
        per_epoch = len(self.base_sampler)
        return max(0, self.skip_samples - self.epoch * per_epoch)

    def __iter__(self):
        indices = list(iter(self.base_sampler))
        rem = self._remaining_skip()
        if rem:
            indices = indices[min(rem, len(indices)):]
        return iter(indices)

    def __len__(self) -> int:
        rem = self._remaining_skip()
        return max(0, len(self.base_sampler) - min(rem, len(self.base_sampler)))


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


def parse_loss_keys(raw: str | None) -> list[str]:
    if raw is None or not raw.strip():
        return list(PMU_KEYS)
    keys = [x.strip() for x in raw.split(",") if x.strip()]
    unknown = [k for k in keys if k not in PMU_KEYS]
    if unknown:
        raise ValueError(f"unknown --loss-keys {unknown}; valid={PMU_KEYS}")
    return list(dict.fromkeys(keys))


def freeze_inactive_head_groups(model: LLMSimModel, active_keys: list[str],
                                rank: int) -> None:
    """Freeze metric heads whose keys are not part of the active loss.

    The full 8-dim output is kept for checkpoint/eval compatibility, but
    inactive heads should not receive gradients or AdamW decay in head-only
    ablations.
    """
    active = set(active_keys)
    groups = [
        ("cpi_head", {"cpi_uop"}, model.head.cpi_head),
        ("branch_head", {"branch_miss"}, model.head.branch_head),
        ("cache_miss_head", {
            "l1d_ld_miss", "l1d_st_miss", "l2_ld_miss",
            "l2_st_miss", "llc_miss",
        }, model.head.cache_miss_head),
        ("dtlb_head", {"dtlb_miss"}, model.head.dtlb_head),
    ]
    frozen = []
    for name, keys, module in groups:
        if not (active & keys):
            module.requires_grad_(False)
            frozen.append(name)
    if rank == 0 and frozen:
        print(f"[model] frozen inactive heads: {', '.join(frozen)}",
              flush=True)


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

    def forward(self, input_ids, attention_mask, query_pos, label, core_mask,
                uops=None, is_uop=None, uop_fields=None,
                side_feats=None, denoms=None, is_attn_feat=None,
                attn_feat_ids=None, attn_feat_values=None):
        pred = self.model(
            input_ids, attention_mask, query_pos,
            is_uop=is_uop, uop_fields=uop_fields, side_feats=side_feats,
            is_attn_feat=is_attn_feat, attn_feat_ids=attn_feat_ids,
            attn_feat_values=attn_feat_values,
        )
        loss, logs = self.loss_fn(pred.float(), label, core_mask,
                                  uops=uops, denoms=denoms)
        return loss, logs


def load_init_ckpt(model, loss_fn, ckpt_dir, device, rank):
    """续训：从已有 ckpt 目录加载权重作为训练初始值。

    与 eval/eval_quota_cycles.py 的加载口径一致：
      - lora_latest/ 或 lora_best/   -> 作为当前 adapter 权重继续训练
      - head_latest.pt 或 head_best.pt -> head / log_var / new_token_embedding
    加载后所有相关参数仍 requires_grad，可继续被优化。
    """
    import os as _os

    candidates = [
        ("latest", _os.path.join(ckpt_dir, "lora_latest"),
         _os.path.join(ckpt_dir, "head_latest.pt")),
        ("best", _os.path.join(ckpt_dir, "lora_best"),
         _os.path.join(ckpt_dir, "head_best.pt")),
    ]
    kind = None
    lora_dir = None
    head_pt = None
    for cand_kind, cand_lora, cand_head in candidates:
        if _os.path.isdir(cand_lora) and _os.path.isfile(cand_head):
            kind, lora_dir, head_pt = cand_kind, cand_lora, cand_head
            break
    if kind is None:
        for cand_kind, cand_lora, cand_head in candidates:
            if _os.path.isfile(cand_head):
                kind, lora_dir, head_pt = cand_kind, cand_lora, cand_head
                break
    if kind is None:
        if rank == 0:
            print(f"[resume][WARN] no head_latest.pt/head_best.pt in {ckpt_dir}",
                  flush=True)
        return {}

    if _os.path.isdir(lora_dir):
        # 真续训：把旧 LoRA 权重直接灌进当前可训练的 default adapter，
        # 继续训练同一个 adapter（不 merge，不新建 adapter，语义最清晰）。
        from peft import load_peft_weights, set_peft_model_state_dict
        old_w = load_peft_weights(lora_dir, device=str(device))
        res = set_peft_model_state_dict(model.backbone, old_w,
                                        adapter_name="default")
        missing = getattr(res, "missing_keys", None)
        unexpected = getattr(res, "unexpected_keys", None)
        if rank == 0:
            print(f"[resume] loaded LoRA into default adapter from {lora_dir} "
                  f"(missing={len(missing) if missing else 0}, "
                  f"unexpected={len(unexpected) if unexpected else 0})",
                  flush=True)
    else:
        if rank == 0:
            print(f"[resume][WARN] no lora_{kind} in {ckpt_dir}", flush=True)

    if _os.path.isfile(head_pt):
        sd = torch.load(head_pt, map_location=device)
        ckpt_mc = sd.get("max_cores")
        ckpt_vs = sd.get("vocab_size")
        ckpt_lv = sd.get("label_version")
        cur_vs = int(model.input_embedding.weight.shape[0])
        ok = True
        if ckpt_mc is not None and int(ckpt_mc) != int(tk.MAX_CORES):
            if rank == 0:
                print(f"[resume][WARN] max_cores mismatch: "
                      f"ckpt={ckpt_mc} cur={tk.MAX_CORES}; refuse to load head",
                      flush=True)
            ok = False
        if ckpt_vs is not None and int(ckpt_vs) != cur_vs:
            if rank == 0:
                print(f"[resume][WARN] vocab_size mismatch: "
                      f"ckpt={ckpt_vs} cur={cur_vs}; refuse to load head",
                      flush=True)
            ok = False
        if ckpt_lv != LABEL_VERSION:
            if rank == 0:
                print(f"[resume][WARN] label_version mismatch: "
                      f"ckpt={ckpt_lv} expected={LABEL_VERSION}; refuse to load head",
                      flush=True)
            ok = False
        if not ok:
            return {}
        try:
            model.head.load_state_dict(sd["head"])
        except RuntimeError as e:
            if rank == 0:
                print(f"[resume][WARN] head shape mismatch; skip head load: {e}",
                      flush=True)
        if "uop_encoder" in sd:
            model.uop_encoder.load_state_dict(sd["uop_encoder"])
        if "attn_feat_encoder" in sd:
            model.attn_feat_encoder.load_state_dict(sd["attn_feat_encoder"])
        if "side_proj" in sd:
            model.side_proj.load_state_dict(sd["side_proj"])
        if "side_mlp" in sd:
            model.side_mlp.load_state_dict(sd["side_mlp"])
        if "side_gate" in sd:
            model.side_gate.load_state_dict(sd["side_gate"])
        if "side_gamma" in sd:
            with torch.no_grad():
                model.side_gamma.copy_(
                    sd["side_gamma"].to(model.side_gamma.device)
                )
        if "log_var" in sd:
            with torch.no_grad():
                loss_fn.log_var.copy_(sd["log_var"].to(loss_fn.log_var.device))
        if "log_var_cycles" in sd:
            with torch.no_grad():
                loss_fn.log_var_cycles.copy_(
                    sd["log_var_cycles"].to(loss_fn.log_var_cycles.device))
        if "new_token_embedding" in sd:
            with torch.no_grad():
                start = sd["new_token_start"]
                emb = model.input_embedding.weight
                emb[start:] = sd["new_token_embedding"].to(emb.dtype).to(device)
            if rank == 0:
                print(f"[resume] loaded head/new_token_embedding from "
                      f"{head_pt} kind={kind} (step={sd.get('step')} "
                      f"val_loss={sd.get('val_loss')})", flush=True)
        else:
            if rank == 0:
                print(f"[resume][WARN] {head_pt} 缺 new_token_embedding",
                      flush=True)
        return {"kind": kind, "step": sd.get("step"),
                "val_loss": sd.get("val_loss")}
    else:
        if rank == 0:
            print(f"[resume][WARN] no head_{kind}.pt in {ckpt_dir}", flush=True)
    return {}


def estimate_log_cpi_quantile(ds, q: float, max_samples: int = 0) -> float:
    """Estimate a log-CPI quantile from labels already present in the dataset."""
    q = min(1.0, max(0.0, float(q)))
    n = len(ds)
    if n <= 0:
        return 0.0
    if max_samples and n > max_samples:
        stride = max(1, n // max_samples)
    else:
        stride = 1
    vals = []
    for i in range(0, n, stride):
        label = torch.as_tensor(ds[i]["label"], dtype=torch.float32)
        vals.append(torch.log(label[:, 0].clamp(min=1e-6)))
    if not vals:
        return 0.0
    return float(torch.quantile(torch.cat(vals), q).item())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/ckpt/phase0")
    ap.add_argument("--cache-path", default=None,
                    help="显式指定 dataset tensor/ids cache；用于同一 jsonl "
                         "按不同 base tokenizer 保存多份 cache")
    ap.add_argument("--base-model", default="Qwen/Qwen3-0.6B-Base",
                    help="HuggingFace backbone name/path, e.g. Qwen/Qwen3-4B-Base")
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
    ap.add_argument("--eval-every", type=int, default=500)
    ap.add_argument("--eval-batches", type=int, default=0,
                    help="每个 rank 验证的最大 batch 数；0=全部")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--num-workers", type=int, default=2,
                    help="DataLoader 的 num_workers，每 rank 各自起这么多")
    ap.add_argument("--skip-train-batches", type=int, default=0,
                    help="启动后先按当前 sampler 顺序跳过多少个训练 dataloader batch，"
                         "不做 forward/backward；用于从中断 run 的未消费窗口附近续训")
    ap.add_argument("--step-offset", type=int, default=0,
                    help="续训时已有的全局 optimizer step 数；仅影响日志、eval gate "
                         "和 checkpoint 里保存的 step")
    ap.add_argument("--use-tstart", dest="use_tstart", action="store_true",
                    default=True,
                    help="兼容旧命令的 no-op；timing 现在由 attention/side 特征承载")
    ap.add_argument("--no-use-tstart", dest="use_tstart", action="store_false",
                    help="兼容旧命令的 no-op；若要消融 timing，请用 --tstart-source zero 重建数据")
    ap.add_argument("--init-ckpt", default=None,
                    help="续训：从已有 ckpt 目录加载 lora_best + head_best.pt "
                         "(含 head/new_token_embedding) 作为初始权重")
    ap.add_argument("--tail-cpi-loss", action="store_true",
                    help="启用 high-CPI tail 低估修正和 low-CPI 高估 guardrail")
    ap.add_argument("--tail-q", type=float, default=0.80,
                    help="自动估计 tail midpoint 的 CPI 分位数")
    ap.add_argument("--tail-log-mid", type=float, default=None,
                    help="手动指定 log(CPI) tail midpoint；默认从数据估计")
    ap.add_argument("--tail-quantile-max-samples", type=int, default=0,
                    help="估计 tail 分位数最多扫描多少个样本；0=全量")
    ap.add_argument("--tail-tau", type=float, default=0.4,
                    help="tail sigmoid 平滑温度，log-space")
    ap.add_argument("--tail-base-lambda", type=float, default=0.0,
                    help="额外 tail base Huber 权重；通常先保持 0")
    ap.add_argument("--tail-under-lambda", type=float, default=0.25,
                    help="tail 低估惩罚权重")
    ap.add_argument("--tail-low-over-lambda", type=float, default=0.25,
                    help="low-CPI 高估 guardrail 权重")
    ap.add_argument("--tail-low-over-margin-frac", type=float, default=0.05,
                    help="low-CPI 区域允许的相对高估 margin，例如 0.05")
    ap.add_argument("--loss-keys", default="",
                    help="逗号分隔的 PMU loss keys；空=全部。"
                         "v14A 推荐 cpi_uop,branch_miss")
    ap.add_argument("--cycles-loss-mode",
                    choices=["per_core", "window", "off"],
                    default="per_core",
                    help="cycles loss 形式；window=按窗口聚合总周期")
    ap.add_argument("--cycles-delta", type=float, default=0.1,
                    help="cycles Huber delta")
    ap.add_argument("--lambda-inv", type=float, default=0.1,
                    help="CPI 物理下界 invariance loss 权重")
    ap.add_argument("--lambda-phys", type=float, default=0.05,
                    help="PMU functional upper-bound soft constraint 权重")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    torch.manual_seed(args.seed)
    is_ddp, rank, local_rank, world = setup_ddp()
    device = f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu"
    if is_main(rank):
        os.makedirs(args.out, exist_ok=True)
        print(f"[ddp] is_ddp={is_ddp} world_size={world}", flush=True)

    if is_main(rank):
        print(f"[model] base_model = {args.base_model}", flush=True)
        print("[model] timing features = attention+side (tstart_proj removed)",
              flush=True)
    tok = build_tokenizer(args.base_model)
    cfg = WrapperConfig(base_model=args.base_model, max_len=args.max_len)
    model = LLMSimModel(cfg, tok).to(device)
    active_loss_keys = parse_loss_keys(args.loss_keys)
    tail_enabled = bool(args.tail_cpi_loss)
    loss_fn = PMULoss(
        lambda_inv=args.lambda_inv,
        lambda_phys=args.lambda_phys,
        cycles_delta=args.cycles_delta,
        cycles_loss_mode=args.cycles_loss_mode,
        loss_keys=active_loss_keys,
        tail_base_lambda=args.tail_base_lambda if tail_enabled else 0.0,
        tail_under_lambda=args.tail_under_lambda if tail_enabled else 0.0,
        tail_low_over_lambda=(
            args.tail_low_over_lambda if tail_enabled else 0.0
        ),
        tail_tau=args.tail_tau,
        tail_low_over_margin=math.log1p(
            max(0.0, float(args.tail_low_over_margin_frac))
        ),
        tail_log_mid=args.tail_log_mid if tail_enabled else None,
    ).to(device)
    resume_meta = {}
    if args.init_ckpt:
        resume_meta = load_init_ckpt(model, loss_fn, args.init_ckpt,
                                     device, rank)
    freeze_inactive_head_groups(model, active_loss_keys, rank)
    train_module = TrainModule(model, loss_fn)
    core = model  # 始终指向底层 LLMSimModel（取参数 / 存权重用）

    cache_path = args.cache_path or WindowDataset.default_cache_path(
        args.data, args.max_len
    )
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
        print(
            "[loss] active_keys="
            f"{','.join(active_loss_keys)} "
            f"cycles_mode={args.cycles_loss_mode} "
            f"lambda_inv={args.lambda_inv:.4f} "
            f"lambda_phys={args.lambda_phys:.4f}",
            flush=True,
        )

    if tail_enabled:
        if args.tail_log_mid is None:
            tail_tensor = torch.zeros(1, device=device)
            if is_main(rank):
                tail_tensor.fill_(estimate_log_cpi_quantile(
                    ds, args.tail_q, args.tail_quantile_max_samples
                ))
            if is_ddp:
                dist.broadcast(tail_tensor, src=0)
            tail_log_mid = float(tail_tensor.item())
        else:
            tail_log_mid = float(args.tail_log_mid)
        loss_fn.set_tail_log_mid(tail_log_mid)
        if is_main(rank):
            print(
                "[loss] tail_cpi enabled "
                f"log_mid={tail_log_mid:.6f} "
                f"mid_cpi={math.exp(tail_log_mid):.6f} "
                f"q={args.tail_q:.2f} tau={args.tail_tau:.3f} "
                f"base={args.tail_base_lambda:.3f} "
                f"under={args.tail_under_lambda:.3f} "
                f"low_over={args.tail_low_over_lambda:.3f} "
                f"low_margin_frac={args.tail_low_over_margin_frac:.3f}",
                flush=True,
            )

    collate = make_collate(tok.pad_token_id)
    nw = args.num_workers
    skip_batches = max(0, int(args.skip_train_batches))
    step_offset = max(0, int(args.step_offset))
    skip_samples_per_rank = skip_batches * int(args.bs)
    if is_ddp:
        base_train_sampler = DistributedSampler(
            train_ds, num_replicas=world, rank=rank, shuffle=True,
            drop_last=True,
        )
        train_sampler = (
            SkipFirstEpochSampler(base_train_sampler, skip_samples_per_rank)
            if skip_samples_per_rank else base_train_sampler
        )
        train_dl = DataLoader(train_ds, batch_size=args.bs,
                              sampler=train_sampler, collate_fn=collate,
                              num_workers=nw, drop_last=True,
                              persistent_workers=nw > 0)
        val_sampler = DistributedSampler(val_ds, num_replicas=world,
                                         rank=rank, shuffle=False,
                                         drop_last=False)
        val_dl = DataLoader(val_ds, batch_size=args.bs, sampler=val_sampler,
                            collate_fn=collate, num_workers=nw,
                            persistent_workers=nw > 0)
    else:
        train_sampler = None
        train_dl = DataLoader(train_ds, batch_size=args.bs, shuffle=True,
                              collate_fn=collate, num_workers=nw,
                              drop_last=True,
                              persistent_workers=nw > 0)
        val_dl = DataLoader(val_ds, batch_size=args.bs, shuffle=False,
                            collate_fn=collate, num_workers=nw,
                            persistent_workers=nw > 0)

    if is_ddp:
        train_module = DDP(train_module, device_ids=[local_rank],
                           find_unused_parameters=False)

    head_params = (list(core.head.parameters())
                   + list(core.uop_encoder.parameters())
                   + list(core.attn_feat_encoder.parameters())
                   + list(core.side_proj.parameters())
                   + list(core.side_mlp.parameters())
                   + list(core.side_gate.parameters())
                   + [core.side_gamma]
                   + list(loss_fn.parameters()))
    head_params = [p for p in head_params if p.requires_grad]
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
        if skip_batches:
            mode = "sampler_offset" if is_ddp else "dataloader_consume"
            print(f"[data] skip_train_batches={skip_batches} mode={mode} "
                  f"skip_samples_per_rank={skip_samples_per_rank}",
                  flush=True)
        if step_offset:
            print(f"[resume] step_offset={step_offset} "
                  f"resume_meta={resume_meta}", flush=True)

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
                loss, _ = train_module(
                    b["input_ids"], b["attention_mask"],
                    b["query_pos"], b["label"], b["core_mask"],
                    b["uops"], b.get("is_uop"), b.get("uop_fields"),
                    b.get("side_feats"), b.get("denoms"),
                    b.get("is_attn_feat"), b.get("attn_feat_ids"),
                    b.get("attn_feat_values"),
                )
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
    if is_main(rank):
        best_pt = os.path.join(args.out, "head_best.pt")
        if os.path.isfile(best_pt):
            try:
                best_sd = torch.load(best_pt, map_location="cpu")
                if best_sd.get("label_version") == LABEL_VERSION:
                    best = float(best_sd.get("val_loss", best))
                    print(f"[resume] keep previous best_val_loss={best:.4f} "
                          f"from {best_pt} step={best_sd.get('step')}",
                          flush=True)
            except Exception as e:
                print(f"[resume][WARN] failed to read previous best: {e}",
                      flush=True)
    epoch = 0
    wall_t0 = time.time()
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

    def next_raw_batch():
        nonlocal data_iter
        try:
            return next(data_iter)
        except StopIteration:
            data_iter = new_iter()
            return next(data_iter)

    def next_batch():
        b = next_raw_batch()
        return {k: v.to(device) for k, v in b.items()}

    accum = max(1, args.grad_accum)
    if skip_batches and not is_ddp:
        if is_main(rank):
            print(f"[data] skip_train_batches={skip_batches} "
                  f"(dataloader batches per rank, no optimizer update)",
                  flush=True)
        skip_t0 = time.time()
        for i in range(skip_batches):
            _ = next_raw_batch()
            if (i + 1) % 200 == 0 and is_main(rank):
                print(f"[data] skipped {i + 1}/{skip_batches} train batches",
                      flush=True)
        if is_main(rank):
            print(f"[data] skip_done batches={skip_batches} "
                  f"time={time.time() - skip_t0:.1f}s", flush=True)
        # Training throughput / wall summaries should describe real updates,
        # not the resume-position scan.
        wall_t0 = time.time()
        win_t0 = wall_t0

    def save_ckpt(kind: str, global_step: int, val_loss: float) -> None:
        dbg(rank, f"save_{kind}_start step={global_step}")
        new_emb = core.input_embedding.weight.detach()[
            core.new_token_start:].cpu().clone()
        torch.save({
            "head": core.head.state_dict(),
            "uop_encoder": core.uop_encoder.state_dict(),
            "attn_feat_encoder": core.attn_feat_encoder.state_dict(),
            "side_proj": core.side_proj.state_dict(),
            "side_mlp": core.side_mlp.state_dict(),
            "side_gate": core.side_gate.state_dict(),
            "side_gamma": core.side_gamma.detach().cpu(),
            "timing_features": "attention_side",
            "use_tstart": False,
            "log_var": loss_fn.log_var.detach().cpu(),
            "log_var_cycles": loss_fn.log_var_cycles.detach().cpu(),
            "loss_config": {
                "active_loss_keys": list(loss_fn.active_loss_keys),
                "cycles_loss_mode": str(loss_fn.cycles_loss_mode),
                "cycles_delta": float(loss_fn.cycles_delta),
                "lambda_inv": float(loss_fn.lambda_inv),
                "lambda_phys": float(loss_fn.lambda_phys),
            },
            "tail_loss": {
                "enabled": bool(tail_enabled),
                "tail_log_mid": (
                    float(loss_fn.tail_log_mid.detach().cpu().item())
                    if loss_fn.tail_enabled else None
                ),
                "tail_tau": float(loss_fn.tail_tau),
                "tail_base_lambda": float(loss_fn.tail_base_lambda),
                "tail_under_lambda": float(loss_fn.tail_under_lambda),
                "tail_low_over_lambda": float(loss_fn.tail_low_over_lambda),
                "tail_low_over_margin": float(loss_fn.tail_low_over_margin),
            },
            "new_token_start": core.new_token_start,
            "n_new_tokens": core.n_new_tokens,
            "new_token_embedding": new_emb,
            "step": int(global_step), "val_loss": float(val_loss),
            "max_cores": int(tk.MAX_CORES),
            "vocab_size": int(len(tok)),
            "label_version": LABEL_VERSION,
        }, os.path.join(args.out, f"head_{kind}.pt"))
        core.backbone.save_pretrained(os.path.join(args.out, f"lora_{kind}"))
        dbg(rank, f"save_{kind}_done step={global_step}")

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
                loss, logs = train_module(
                    b["input_ids"], b["attention_mask"],
                    b["query_pos"], b["label"], b["core_mask"],
                    b["uops"], b.get("is_uop"), b.get("uop_fields"),
                    b.get("side_feats"), b.get("denoms"),
                    b.get("is_attn_feat"), b.get("attn_feat_ids"),
                    b.get("attn_feat_values"),
                )
                (loss / accum).backward()
            last_logs = logs
        torch.nn.utils.clip_grad_norm_(core.trainable_parameters(), 1.0)
        optim.step()
        logs = last_logs
        step += 1
        global_step = step_offset + step
        # 全局吞吐（× world_size 近似全局 batch）
        win_samples += micro_samples * world
        win_tokens += micro_tokens * world
        glob_samples += micro_samples * world
        glob_tokens += micro_tokens * world

        if global_step % args.log_every == 0 and is_main(rank):
            if torch.cuda.is_available():
                torch.cuda.synchronize(device)
            dt = time.time() - win_t0
            sps = win_samples / dt if dt > 0 else 0
            tps = win_tokens / dt if dt > 0 else 0
            l_cpi = logs.get("L_cpi_uop", logs.get("L_cpi"))
            tail_dbg = ""
            if tail_enabled:
                tail_dbg = (
                    f" L_tail_under={logs['L_tail_under'].item():.4f}"
                    f" L_tail_low_over={logs['L_tail_low_over'].item():.4f}"
                )
            print(
                f"[step {global_step}] loss={logs['loss'].item():.4f} "
                f"L_cpi_uop={l_cpi.item():.4f} "
                f"L_cycles={logs['L_cycles'].item():.4f}{tail_dbg} "
                f"L_inv={logs['L_inv'].item():.4f} | "
                f"throughput: {sps:.1f} samp/s, {tps:.0f} tok/s",
                flush=True,
            )
            win_t0 = time.time(); win_samples = 0; win_tokens = 0
        if global_step % args.eval_every == 0:
            dbg(rank, f"eval_gate step={global_step} is_main={is_main(rank)}")
            vl = run_val()  # 所有 rank 必须一起调用
            if is_main(rank):
                print(f"[eval step {global_step}] val_loss={vl:.4f}",
                      flush=True)
                save_ckpt("latest", global_step, vl)
                if vl < best:
                    best = vl
                    save_ckpt("best", global_step, vl)
            if is_ddp:
                dist.barrier()  # 等 rank0 存盘完成，保持各 rank 同步

    dbg(rank, f"train_loop_done step={step_offset + step}/"
        f"{step_offset + args.steps}")

    if is_ddp:
        dbg(rank, "enter_barrier")
        dist.barrier()
        dbg(rank, "leave_barrier")
    if is_main(rank):
        if torch.cuda.is_available():
            torch.cuda.synchronize(device)
        total_dt = time.time() - wall_t0
        print("=" * 60, flush=True)
        print(f"[DONE] steps={step_offset + args.steps} "
              f"run_steps={args.steps} step_offset={step_offset} "
              f"world_size={world} "
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
