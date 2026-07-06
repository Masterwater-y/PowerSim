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
from train.dataset import WindowDataset, make_collate
from train.loss import PMULoss

LABEL_VERSION = "v22_split_direct_no_dtlb"
LOG_VAR_MIN = -6.0
LOG_VAR_MAX = 6.0


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


def all_ranks_finite(local_ok: bool, device: str, is_ddp: bool) -> bool:
    flag = torch.tensor(1.0 if local_ok else 0.0, device=device)
    if is_ddp:
        dist.all_reduce(flag, op=dist.ReduceOp.MIN)
    return bool(flag.item() > 0.5)


class TrainModule(torch.nn.Module):
    """把 backbone+head 与 loss 合到一个 Module，使其整体被 DDP 包裹。

    这样：
      - 训练时调用 wrapper.forward()，DDP 才会注册反向 allreduce 钩子；
      - loss 状态也作为本 Module 的参数/缓冲一起跟随设备和 DDP。
    """

    def __init__(self, model, loss_fn):
        super().__init__()
        self.model = model
        self.loss_fn = loss_fn

    def forward(self, input_ids, attention_mask, query_pos, label, core_mask,
                t_start, uops=None, is_uop=None, uop_fields=None,
                side_feats=None, denoms=None, local_pos=None):
        pred = self.model(
            input_ids, attention_mask, query_pos, t_start,
            is_uop=is_uop, uop_fields=uop_fields, side_feats=side_feats,
            local_pos=local_pos,
            core_mask=core_mask,
        )
        loss, logs = self.loss_fn(pred.float(), label, core_mask,
                                  uops=uops, denoms=denoms)
        return loss, logs


def load_init_ckpt(model, loss_fn, ckpt_dir, device, rank):
    """续训：从已有 ckpt 目录加载权重作为训练初始值。

    与 eval/eval_quota_cycles.py 的加载口径一致：
      - lora_best/        -> 作为新 adapter 载入并设为激活；其权重保持可训练
      - head_best.pt      -> head / tstart_proj / loss state / new_token_embedding
    加载后所有相关参数仍 requires_grad，可继续被优化。
    """
    import os as _os

    lora_dir = _os.path.join(ckpt_dir, "lora_best")
    if _os.path.isdir(lora_dir):
        if getattr(model.cfg, "tiny_transformer", False):
            tiny_pt = _os.path.join(lora_dir, "pytorch_model.bin")
            if not _os.path.isfile(tiny_pt):
                if rank == 0:
                    print(f"[resume][WARN] tiny checkpoint missing "
                          f"{tiny_pt}", flush=True)
            else:
                sd = torch.load(tiny_pt, map_location=device)
                missing, unexpected = model.backbone.load_state_dict(
                    sd, strict=False)
                if rank == 0:
                    print(f"[resume] loaded tiny backbone from {lora_dir} "
                          f"(missing={len(missing)} "
                          f"unexpected={len(unexpected)})", flush=True)
        else:
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
            print(f"[resume][WARN] no lora_best in {ckpt_dir}", flush=True)

    head_pt = _os.path.join(ckpt_dir, "head_best.pt")
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
        if ckpt_vs is not None and int(ckpt_vs) > cur_vs:
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
            return
        ckpt_mode = sd.get("cpi_head_mode", "direct")
        cur_mode = getattr(model.head, "cpi_head_mode", "direct")
        try:
            if ckpt_mode == cur_mode:
                model.head.load_state_dict(sd["head"])
            else:
                missing, unexpected = model.head.load_state_dict(
                    sd["head"], strict=False)
                if rank == 0:
                    print(f"[resume][WARN] cpi_head_mode mismatch: "
                          f"ckpt={ckpt_mode} cur={cur_mode}; partial head load "
                          f"(missing={len(missing)} unexpected={len(unexpected)})",
                          flush=True)
        except RuntimeError as e:
            if rank == 0:
                print(f"[resume][WARN] head shape mismatch; skip head load: {e}",
                      flush=True)
        if "tstart_proj" in sd:
            model.tstart_proj.load_state_dict(sd["tstart_proj"])
        if "uop_encoder" in sd:
            model.uop_encoder.load_state_dict(sd["uop_encoder"])
        if "side_proj" in sd:
            model.side_proj.load_state_dict(sd["side_proj"])
        if "local_proj" in sd:
            model.local_proj.load_state_dict(sd["local_proj"])
        ckpt_fuse_mode = str(sd.get("local_fuse_mode", "add"))
        cur_fuse_mode = getattr(model, "local_fuse_mode", "add")
        if ckpt_fuse_mode != cur_fuse_mode and rank == 0:
            print(f"[resume][WARN] local_fuse_mode mismatch: "
                  f"ckpt={ckpt_fuse_mode} cur={cur_fuse_mode}",
                  flush=True)
        if "local_bind_fuse" in sd:
            model.local_bind_fuse.load_state_dict(sd["local_bind_fuse"])
        elif cur_fuse_mode == "bind_concat" and rank == 0:
            print("[resume][WARN] checkpoint missing local_bind_fuse; "
                  "using fresh bind_concat fuse init", flush=True)
        load_loss_state = (
            getattr(loss_fn, "loss_weight_mode", "uncertainty")
            == "uncertainty"
        )
        if "log_var" in sd and load_loss_state:
            with torch.no_grad():
                loss_fn.log_var.copy_(sd["log_var"].to(loss_fn.log_var.device))
        elif "log_var" in sd and rank == 0:
            print("[resume] skip checkpoint log_var because "
                  "loss_weight_mode=fixed", flush=True)
        if "log_var_cycles" in sd and load_loss_state:
            with torch.no_grad():
                loss_fn.log_var_cycles.copy_(
                    sd["log_var_cycles"].to(loss_fn.log_var_cycles.device))
        if "new_token_embedding" in sd:
            with torch.no_grad():
                start = model.new_token_start
                emb = model.input_embedding.weight
                old = sd["new_token_embedding"].to(emb.dtype).to(device)
                cur_tokens = tk.all_special_tokens()
                if old.shape[0] == len(cur_tokens):
                    emb[start:start + len(cur_tokens)] = old
                    mapped = int(old.shape[0])
                else:
                    legacy_tokens = tk.all_special_tokens_without_local()
                    mapped = 0
                    if old.shape[0] == len(legacy_tokens):
                        cur_idx = {tok: i for i, tok in enumerate(cur_tokens)}
                        for old_i, tok in enumerate(legacy_tokens):
                            new_i = cur_idx.get(tok)
                            if new_i is not None:
                                emb[start + new_i] = old[old_i]
                                mapped += 1
                    else:
                        n = min(old.shape[0], emb.shape[0] - int(start))
                        emb[start:start + n] = old[:n]
                        mapped = int(n)
            if rank == 0:
                print(f"[resume] loaded head/tstart/new_token_embedding from "
                      f"{head_pt} (step={sd.get('step')} "
                      f"val_loss={sd.get('val_loss')} "
                      f"mapped_new_tokens={mapped}/{model.n_new_tokens})",
                      flush=True)
        else:
            if rank == 0:
                print(f"[resume][WARN] {head_pt} 缺 new_token_embedding",
                      flush=True)
    else:
        if rank == 0:
            print(f"[resume][WARN] no head_best.pt in {ckpt_dir}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/ckpt/phase0")
    ap.add_argument("--cache-path", default=None,
                    help="显式指定 dataset tensor/ids cache；用于同一 jsonl "
                         "按不同 base tokenizer 保存多份 cache")
    ap.add_argument("--base-model", default="Qwen/Qwen3-0.6B-Base",
                    help="HuggingFace backbone name/path, e.g. Qwen/Qwen3-4B-Base")
    ap.add_argument("--cpi-head-mode", choices=["direct"],
                    default="direct",
                    help="direct per-core CPI head; delta is removed in v22")
    ap.add_argument("--local-fuse-mode", choices=["add", "bind_concat"],
                    default="add",
                    help="add=v16 query+local_proj(local); "
                         "bind_concat=v16 plus explicit QUERY/LOCAL concat fuse")
    ap.add_argument("--lambda-rank", type=float, default=0.0,
                    help="legacy ignored in v22; rank loss is disabled")
    ap.add_argument("--lambda-spread", type=float, default=0.0,
                    help="legacy ignored in v22; spread loss is disabled")
    ap.add_argument("--lambda-inv", type=float, default=0.0,
                    help="physical invariance penalty weight; default off")
    ap.add_argument("--lambda-phys", type=float, default=0.0,
                    help="PMU bound penalty weight; default off")
    ap.add_argument("--loss-weight-mode", choices=["fixed", "uncertainty"],
                    default="fixed",
                    help="fixed uses explicit coefficients; uncertainty keeps "
                         "legacy learned log_var weighting")
    ap.add_argument("--lambda-cpi-abs", type=float, default=1.0,
                    help="fixed-mode weight for per-core absolute log-CPI loss")
    ap.add_argument("--lambda-cycles", "--lambda-cycles-window",
                    dest="lambda_cycles", type=float, default=1.0,
                    help="fixed-mode weight for window-level cycles loss")
    ap.add_argument("--lambda-aux-pmu", type=float, default=0.05,
                    help="fixed-mode mean weight for non-CPI PMU heads")
    ap.add_argument("--lambda-centered-cpi", type=float, default=0.3,
                    help="fixed-mode weight for centered per-core log-CPI loss")
    ap.add_argument("--centered-min-std", type=float, default=0.30,
                    help="diagnostic high-spread threshold for logging; "
                         "not a hard training gate")
    ap.add_argument("--centered-ref-std", type=float, default=0.30,
                    help="reference label log-CPI std for centered-loss weights")
    ap.add_argument("--centered-weight-min", type=float, default=0.10,
                    help="minimum per-window weight multiplier for centered CPI")
    ap.add_argument("--centered-weight-max", type=float, default=3.0,
                    help="maximum per-window weight multiplier for centered CPI")
    ap.add_argument("--centered-delta", type=float, default=0.1,
                    help="Huber delta for centered log-CPI residuals")
    ap.add_argument("--rank-gap", type=float, default=0.10,
                    help="minimum absolute log-CPI gap for rank loss pairs")
    ap.add_argument("--rank-tau", type=float, default=0.10,
                    help="temperature for pairwise rank loss")
    ap.add_argument("--spread-min-std", type=float, default=0.03,
                    help="enable spread loss only when label log-CPI std exceeds this")
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--bs", "--batch-size", dest="bs", type=int, default=2)
    ap.add_argument("--grad-accum", type=int, default=1,
                    help="梯度累积步数：等效 batch = bs*world*grad_accum")
    ap.add_argument("--lr-lora", type=float, default=2e-4)
    ap.add_argument("--lr-head", type=float, default=1e-3)
    ap.add_argument("--lr-emb", type=float, default=1e-3)
    ap.add_argument("--max-len", type=int, default=4096)
    ap.add_argument("--tiny-transformer", action="store_true",
                    help="replace Qwen+LoRA with the v25a self-trained tiny Transformer")
    ap.add_argument("--tiny-d-model", type=int, default=320)
    ap.add_argument("--tiny-n-layers", type=int, default=8)
    ap.add_argument("--tiny-n-heads", type=int, default=8)
    ap.add_argument("--tiny-ffn-dim", type=int, default=1280)
    ap.add_argument("--tiny-rope-theta", type=float, default=10000.0)
    ap.add_argument("--dropout", type=float, default=0.1,
                    help="tiny Transformer residual/FFN dropout")
    ap.add_argument("--attn-dropout", type=float, default=0.1,
                    help="tiny Transformer attention dropout on the SDPA fallback")
    ap.add_argument("--warmup-steps", type=int, default=0,
                    help="linear warmup steps; 0 preserves legacy constant LR")
    ap.add_argument("--lr-min-ratio", type=float, default=1.0,
                    help="cosine final LR / peak LR ratio after warmup")
    ap.add_argument("--weight-decay", type=float, default=0.0)
    ap.add_argument("--adam-beta1", type=float, default=0.9)
    ap.add_argument("--adam-beta2", type=float, default=0.999)
    ap.add_argument("--grad-clip", type=float, default=1.0)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--log-every", type=int, default=20)
    ap.add_argument("--eval-every", type=int, default=500)
    ap.add_argument("--save-every", type=int, default=0,
                    help="在 eval gate 上额外保存 step_XXXXXX checkpoint；"
                         "0=只保存 best。建议与 --eval-every 相同")
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
    ap.add_argument("--use-tstart", action="store_true",
                    help="注入每核窗口相对 T_start 跨核时间锚点特征")
    ap.add_argument("--init-ckpt", default=None,
                    help="续训：从已有 ckpt 目录加载 lora_best + head_best.pt "
                         "(含 head/tstart_proj/new_token_embedding) 作为初始权重")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    torch.manual_seed(args.seed)
    is_ddp, rank, local_rank, world = setup_ddp()
    device = f"cuda:{local_rank}" if torch.cuda.is_available() else "cpu"
    if is_main(rank):
        os.makedirs(args.out, exist_ok=True)
        print(f"[ddp] is_ddp={is_ddp} world_size={world}", flush=True)

    if is_main(rank):
        model_kind = "tiny_transformer" if args.tiny_transformer else "qwen_lora"
        print(f"[model] kind = {model_kind}", flush=True)
        print(f"[model] base_model = {args.base_model}", flush=True)
    tok = build_tokenizer(args.base_model)
    cfg = WrapperConfig(
        base_model=args.base_model,
        max_len=args.max_len,
        cpi_head_mode=args.cpi_head_mode,
        local_fuse_mode=args.local_fuse_mode,
        tiny_transformer=bool(args.tiny_transformer),
        tiny_d_model=args.tiny_d_model,
        tiny_n_layers=args.tiny_n_layers,
        tiny_n_heads=args.tiny_n_heads,
        tiny_ffn_dim=args.tiny_ffn_dim,
        tiny_dropout=args.dropout,
        tiny_attn_dropout=args.attn_dropout,
        tiny_rope_theta=args.tiny_rope_theta,
    )
    model = LLMSimModel(cfg, tok).to(device)
    loss_fn = PMULoss(
        lambda_inv=args.lambda_inv,
        lambda_phys=args.lambda_phys,
        rank_gap=args.rank_gap,
        rank_tau=args.rank_tau,
        spread_min_std=args.spread_min_std,
        loss_weight_mode=args.loss_weight_mode,
        lambda_cpi_abs=args.lambda_cpi_abs,
        lambda_cycles=args.lambda_cycles,
        lambda_aux_pmu=args.lambda_aux_pmu,
        lambda_centered_cpi=args.lambda_centered_cpi,
        centered_min_std=args.centered_min_std,
        centered_ref_std=args.centered_ref_std,
        centered_weight_min=args.centered_weight_min,
        centered_weight_max=args.centered_weight_max,
        centered_delta=args.centered_delta,
    ).to(device)
    if args.init_ckpt:
        load_init_ckpt(model, loss_fn, args.init_ckpt, device, rank)
    train_module = TrainModule(model, loss_fn)
    if is_ddp:
        train_module = DDP(train_module, device_ids=[local_rank],
                           find_unused_parameters=False)
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
        if args.tiny_transformer:
            print(f"[model] tiny d_model={args.tiny_d_model} "
                  f"layers={args.tiny_n_layers} heads={args.tiny_n_heads} "
                  f"ffn_dim={args.tiny_ffn_dim} dropout={args.dropout} "
                  f"attn_dropout={args.attn_dropout} "
                  f"position=RoPE theta={args.tiny_rope_theta}",
                  flush=True)
        print(f"[model] cpi_head_mode={args.cpi_head_mode} "
              f"local_fuse_mode={args.local_fuse_mode} "
              "rank_spread=disabled", flush=True)
        print(f"[loss] mode={args.loss_weight_mode} "
              f"cpi_abs={args.lambda_cpi_abs} "
              f"cycles={args.lambda_cycles} "
              f"aux_pmu={args.lambda_aux_pmu} "
              f"centered_cpi={args.lambda_centered_cpi} "
              f"inv={args.lambda_inv} phys={args.lambda_phys}", flush=True)
        print(f"[loss] centered_min_std={args.centered_min_std} "
              f"centered_ref_std={args.centered_ref_std} "
              f"centered_weight_min={args.centered_weight_min} "
              f"centered_weight_max={args.centered_weight_max} "
              f"centered_delta={args.centered_delta}", flush=True)
        if args.save_every:
            print(f"[ckpt] save_every={args.save_every} "
                  f"(periodic snapshots under {args.out}/step_XXXXXX)",
                  flush=True)
        print(f"[optim] lr_backbone={args.lr_lora} lr_head={args.lr_head} "
              f"lr_emb={args.lr_emb} weight_decay={args.weight_decay} "
              f"betas=({args.adam_beta1}, {args.adam_beta2}) "
              f"warmup_steps={args.warmup_steps} "
              f"lr_min_ratio={args.lr_min_ratio} "
              f"grad_clip={args.grad_clip}", flush=True)

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

    head_params = (list(core.head.parameters())
                   + list(core.tstart_proj.parameters())
                   + list(core.uop_encoder.parameters())
                   + list(core.side_proj.parameters())
                   + list(core.local_proj.parameters())
                   + list(core.local_bind_fuse.parameters())
                   + list(loss_fn.parameters()))
    emb_weight = core.input_embedding.weight
    lora_params = [p for n, p in core.backbone.named_parameters()
                   if p.requires_grad and p is not emb_weight]
    optim = torch.optim.AdamW([
        {"params": lora_params, "lr": args.lr_lora},
        {"params": head_params, "lr": args.lr_head},
        {"params": [emb_weight], "lr": args.lr_emb},
    ], betas=(args.adam_beta1, args.adam_beta2),
       weight_decay=args.weight_decay)

    def lr_scale(step_idx: int) -> float:
        warmup = max(0, int(args.warmup_steps))
        total = max(1, int(args.steps))
        min_ratio = float(args.lr_min_ratio)
        if warmup > 0 and step_idx < warmup:
            return float(step_idx + 1) / float(warmup)
        if total <= warmup:
            return 1.0
        progress = float(step_idx - warmup) / float(max(1, total - warmup))
        progress = min(max(progress, 0.0), 1.0)
        cosine = 0.5 * (1.0 + math.cos(progress * math.pi))
        return min_ratio + (1.0 - min_ratio) * cosine

    scheduler = None
    if args.warmup_steps > 0 or args.lr_min_ratio != 1.0:
        scheduler = torch.optim.lr_scheduler.LambdaLR(optim, lr_scale)
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
            print(f"[resume] step_offset={step_offset}", flush=True)

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
                ts = b["t_start"] * (1.0 if args.use_tstart else 0.0)
                loss, _ = train_module(
                    b["input_ids"], b["attention_mask"],
                    b["query_pos"], b["label"], b["core_mask"], ts,
                    b["uops"], b.get("is_uop"), b.get("uop_fields"),
                    b.get("side_feats"), b.get("denoms"),
                    b.get("local_pos"),
                )
                tot += loss.detach().float()
                cnt += 1
        if is_ddp:
            dist.all_reduce(tot, op=dist.ReduceOp.SUM)
            dist.all_reduce(cnt, op=dist.ReduceOp.SUM)
        train_module.train()
        return (tot / cnt.clamp(min=1)).item()

    def save_eval_checkpoint(dest_dir: str, global_step: int,
                             val_loss: float, label: str) -> None:
        """Save an eval-compatible checkpoint directory.

        The loader expects head_best.pt + lora_best/.  In tiny mode,
        lora_best/ contains the full TinyTransformer state rather than a PEFT
        adapter; the directory name is kept for compatibility with existing
        scripts.
        """
        os.makedirs(dest_dir, exist_ok=True)
        new_emb = core.input_embedding.weight.detach()[
            core.new_token_start:].cpu().clone()
        torch.save({
            "head": core.head.state_dict(),
            "tstart_proj": core.tstart_proj.state_dict(),
            "uop_encoder": core.uop_encoder.state_dict(),
            "side_proj": core.side_proj.state_dict(),
            "local_proj": core.local_proj.state_dict(),
            "local_bind_fuse": core.local_bind_fuse.state_dict(),
            "use_tstart": bool(args.use_tstart),
            "base_model": args.base_model,
            "head_hidden": int(core.cfg.head_hidden),
            "uop_field_dim": int(core.cfg.uop_field_dim),
            "tiny_transformer": bool(args.tiny_transformer),
            "tiny_d_model": int(args.tiny_d_model),
            "tiny_n_layers": int(args.tiny_n_layers),
            "tiny_n_heads": int(args.tiny_n_heads),
            "tiny_ffn_dim": int(args.tiny_ffn_dim),
            "tiny_rope_theta": float(args.tiny_rope_theta),
            "tiny_dropout": float(args.dropout),
            "tiny_attn_dropout": float(args.attn_dropout),
            "cpi_head_mode": args.cpi_head_mode,
            "local_fuse_mode": args.local_fuse_mode,
            "loss_weight_mode": args.loss_weight_mode,
            "lambda_cpi_abs": float(args.lambda_cpi_abs),
            "lambda_cycles": float(args.lambda_cycles),
            "lambda_aux_pmu": float(args.lambda_aux_pmu),
            "lambda_centered_cpi": float(args.lambda_centered_cpi),
            "lambda_inv": float(args.lambda_inv),
            "lambda_phys": float(args.lambda_phys),
            "lr_backbone": float(args.lr_lora),
            "lr_head": float(args.lr_head),
            "lr_emb": float(args.lr_emb),
            "warmup_steps": int(args.warmup_steps),
            "lr_min_ratio": float(args.lr_min_ratio),
            "weight_decay": float(args.weight_decay),
            "adam_beta1": float(args.adam_beta1),
            "adam_beta2": float(args.adam_beta2),
            "grad_clip": float(args.grad_clip),
            "rank_gap": float(args.rank_gap),
            "rank_tau": float(args.rank_tau),
            "spread_min_std": float(args.spread_min_std),
            "centered_min_std": float(args.centered_min_std),
            "centered_ref_std": float(args.centered_ref_std),
            "centered_weight_min": float(args.centered_weight_min),
            "centered_weight_max": float(args.centered_weight_max),
            "centered_delta": float(args.centered_delta),
            "log_var": loss_fn.log_var.detach().cpu(),
            "log_var_cycles": loss_fn.log_var_cycles.detach().cpu(),
            "new_token_start": core.new_token_start,
            "n_new_tokens": core.n_new_tokens,
            "new_token_embedding": new_emb,
            "step": global_step,
            "val_loss": val_loss,
            "max_cores": int(tk.MAX_CORES),
            "vocab_size": int(len(tok)),
            "label_version": LABEL_VERSION,
            "checkpoint_label": label,
        }, os.path.join(dest_dir, "head_best.pt"))
        core.backbone.save_pretrained(os.path.join(dest_dir, "lora_best"))

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
                ts = b["t_start"] * (1.0 if args.use_tstart else 0.0)
                loss, logs = train_module(
                    b["input_ids"], b["attention_mask"],
                    b["query_pos"], b["label"], b["core_mask"], ts,
                    b["uops"], b.get("is_uop"), b.get("uop_fields"),
                    b.get("side_feats"), b.get("denoms"),
                    b.get("local_pos"),
                )
                if not all_ranks_finite(
                        bool(torch.isfinite(loss.detach()).item()),
                        device, is_ddp):
                    msg = (f"non-finite loss before backward at "
                           f"global_step={step_offset + step + 1} "
                           f"micro={micro}")
                    dbg(rank, msg)
                    raise FloatingPointError(msg)
                (loss / accum).backward()
            last_logs = logs
        grad_norm = torch.nn.utils.clip_grad_norm_(
            core.trainable_parameters(), args.grad_clip)
        optim.step()
        if scheduler is not None:
            scheduler.step()
        if loss_fn.loss_weight_mode == "uncertainty":
            with torch.no_grad():
                loss_fn.log_var.clamp_(LOG_VAR_MIN, LOG_VAR_MAX)
                loss_fn.log_var_cycles.clamp_(LOG_VAR_MIN, LOG_VAR_MAX)
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
            zero = torch.tensor(0.)
            print(f"[step {global_step}] loss={logs['loss'].item():.4f} "
                  f"L_cpi_uop={l_cpi.item():.4f} "
                  f"L_branch_miss={logs.get('L_branch_miss', torch.tensor(0.)).item():.4f} "
                  f"L_llc_miss={logs.get('L_llc_miss', torch.tensor(0.)).item():.4f} "
                  f"L_cycles={logs.get('L_cycles', torch.tensor(0.)).item():.4f} "
                  f"L_centered={logs.get('L_centered_cpi', zero).item():.4f} "
                  f"label_std={logs.get('label_log_cpi_std', zero).item():.4f} "
                  f"pred_std={logs.get('pred_log_cpi_std', zero).item():.4f} "
                  f"high_frac={logs.get('high_spread_frac', zero).item():.3f} "
                  f"L_inv={logs['L_inv'].item():.4f} | "
                  f"grad_norm={float(grad_norm):.3f} "
                  f"throughput: {sps:.1f} samp/s, {tps:.0f} tok/s",
                  flush=True)
            win_t0 = time.time(); win_samples = 0; win_tokens = 0
        if global_step % args.eval_every == 0:
            dbg(rank, f"eval_gate step={global_step} is_main={is_main(rank)}")
            vl = run_val()  # 所有 rank 必须一起调用
            if is_main(rank):
                print(f"[eval step {global_step}] val_loss={vl:.4f}", flush=True)
                if args.save_every and global_step % args.save_every == 0:
                    snap_dir = os.path.join(args.out, f"step_{global_step:06d}")
                    dbg(rank, f"save_snapshot_start step={global_step} dir={snap_dir}")
                    save_eval_checkpoint(
                        snap_dir, global_step, vl, label="periodic")
                    dbg(rank, f"save_snapshot_done step={global_step} dir={snap_dir}")
                if vl < best:
                    best = vl
                    dbg(rank, f"save_best_start step={global_step}")
                    save_eval_checkpoint(
                        args.out, global_step, vl, label="best")
                    dbg(rank, f"save_best_done step={global_step}")
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
