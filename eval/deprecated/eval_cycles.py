"""eval_cycles.py — 全局 CPI 验证：sum(cycles)/sum(macro) 误差。

实验设计：
  选定若干 workload 的 functional trace 窗口 -> 模型推理每核每窗口 CPI ->
  cycles_pred = cpi_pred * macro(=instr_retired, functional 已知) ->
  全局聚合 CPI_global = Σ(cpi*macro) / Σ(macro)  （等价 Σcycles/Σmacro）。

报告三方对比：
  1. pred   : 模型预测
  2. label  : 窗口标签真值 Σ(cpi_label*macro)/Σmacro  —— 纯模型预测能力
  3. gem5   : stats.txt 全程 Σ(numCycles)/Σ(commitStats numInsts) —— 含窗口采样偏差

用法：
  python eval/eval_cycles.py \
    --data data/windows_train8_w512/windows.jsonl \
    --ckpt ckpt/train8_w512_embfix \
    --max-len 32768 --bs 1 \
    --workload W_compute_int:data/raw_8w_8c_500k/W_compute_int/stats.txt \
    --workload W_chase_dram:data/raw_8w_8c_500k/W_chase_dram/stats.txt \
    --workload W_branch_storm:data/raw_fix3_8c_500k/W_branch_storm/stats.txt \
    --max-windows 200
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

import numpy as np
import torch
import torch.distributed as dist
from torch.utils.data import DataLoader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.llm_wrapper import LLMSimModel, WrapperConfig, build_tokenizer
from model.regression_head import PMU_KEYS
from train.dataset import WindowDataset, make_collate
from train.loss import invert_pred

CPI_IDX = PMU_KEYS.index("cpi_uop")
_CORE_NUMCYC = re.compile(r"(?:cores|switch)(\d+)\.core\.numCycles\s+([0-9.]+)")
_CORE_INSTS = re.compile(
    r"(?:cores|switch)(\d+)\.core\.commitStats0\.numInsts\s+([0-9.]+)")


def setup_ddp():
    """返回 (is_ddp, rank, local_rank, world)。与训练同款 env 识别。"""
    if "RANK" in os.environ and "WORLD_SIZE" in os.environ:
        rank = int(os.environ["RANK"])
        local_rank = int(os.environ.get("LOCAL_RANK", 0))
        world = int(os.environ["WORLD_SIZE"])
        dist.init_process_group(backend="nccl")
        torch.cuda.set_device(local_rank)
        return True, rank, local_rank, world
    return False, 0, 0, 1


def parse_gem5_stats(stats_path: str):
    """返回 (sum_cycles, sum_insts, global_cpi) —— 全核聚合。"""
    cyc, ins = {}, {}
    with open(stats_path) as f:
        for ln in f:
            m = _CORE_NUMCYC.search(ln)
            if m:
                cyc[int(m.group(1))] = float(m.group(2))
                continue
            m = _CORE_INSTS.search(ln)
            if m:
                ins[int(m.group(1))] = float(m.group(2))
    sum_c = sum(cyc.values())
    sum_i = sum(ins.values())
    return sum_c, sum_i, (sum_c / sum_i if sum_i > 0 else float("nan"))


def load_workload_index(jsonl_path: str):
    """返回 (idx, total)。idx={workload: [行号...]}；行号为 jsonl 中 json 行的顺序号。

    注意：仅当 cache 未丢弃任何样本（len(ds)==total）时，行号才与 cache 下标对齐。
    调用方需校验。
    """
    idx = {}
    n = 0
    with open(jsonl_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            idx.setdefault(rec["workload"], []).append(n)
            n += 1
    return idx, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--bs", type=int, default=1)
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--workload", action="append", default=[],
                    help="格式 NAME:stats_path，可多次")
    ap.add_argument("--max-windows", type=int, default=0,
                    help="每个 workload 最多评估多少窗口（0=全部）")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    is_ddp, rank, local_rank, world = setup_ddp()
    if is_ddp:
        device = f"cuda:{local_rank}"
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    def log(msg):
        if rank == 0:
            print(msg, flush=True)

    log(f"[init] is_ddp={is_ddp} world={world} device={device} "
        f"loading tokenizer+model ...")

    tok = build_tokenizer()
    cfg = WrapperConfig(max_len=args.max_len)
    model = LLMSimModel(cfg, tok).to(device)
    log("[init] base model on device, loading adapter+head ...")

    lora_dir = os.path.join(args.ckpt, "lora_best")
    if os.path.isdir(lora_dir):
        model.backbone.load_adapter(lora_dir, adapter_name="loaded")
        model.backbone.set_adapter("loaded")
    head_pt = os.path.join(args.ckpt, "head_best.pt")
    use_tstart = False
    if os.path.isfile(head_pt):
        sd = torch.load(head_pt, map_location=device)
        model.head.load_state_dict(sd["head"])
        if "tstart_proj" in sd:
            model.tstart_proj.load_state_dict(sd["tstart_proj"])
        use_tstart = bool(sd.get("use_tstart", False))
        if "new_token_embedding" in sd:
            with torch.no_grad():
                start = sd["new_token_start"]
                emb = model.input_embedding.weight
                emb[start:] = sd["new_token_embedding"].to(emb.dtype).to(device)
        else:
            log("[WARN] ckpt 缺 new_token_embedding，推理用随机初始化，结果无效！")
    model.eval()
    log(f"[init] model ready (eval mode, use_tstart={use_tstart}). "
        f"loading dataset cache ...")

    # 整库 dataset（读 cache），再按 workload 行号子集化
    ds = WindowDataset(args.data, tok, max_len=args.max_len,
                       cache_path=WindowDataset.default_cache_path(
                           args.data, args.max_len),
                       require_cache=True)
    log(f"[init] dataset cache loaded: {len(ds)} windows")
    wl_index, jsonl_total = load_workload_index(args.data)
    collate = make_collate(tok.pad_token_id)
    if jsonl_total != len(ds):
        log(f"[FATAL] cache 丢弃了样本：jsonl={jsonl_total} cache={len(ds)}，"
            f"行号与 cache 下标不对齐，无法按 workload 子集化。\n"
            f"请用与 cache 相同的 max-len（当前 {args.max_len}），"
            f"或重建 cache 后再评估。")
        if is_ddp:
            dist.destroy_process_group()
        sys.exit(1)
    targets = []
    for spec in args.workload:
        name, _, stats_path = spec.partition(":")
        targets.append((name, stats_path))

    log("=" * 78)
    log(f"全局 CPI 验证  (cache={ds.mode}, total_windows={len(ds)}, world={world})")
    log("=" * 78)

    summary = []
    for name, stats_path in targets:
        rows = wl_index.get(name, [])
        if not rows:
            log(f"[skip] {name}: 在 {args.data} 中无样本")
            continue
        if args.max_windows:
            rows = rows[:args.max_windows]
        n_total = len(rows)
        # 跨 rank 分片：每个 rank 取 rows[rank::world]
        my_rows = rows[rank::world] if is_ddp else rows
        sub = torch.utils.data.Subset(ds, my_rows)
        dl = DataLoader(sub, batch_size=args.bs, shuffle=False,
                        collate_fn=collate, num_workers=2)

        sum_cyc_pred = 0.0
        sum_cyc_label = 0.0
        sum_macro = 0.0
        ape_sum = 0.0       # per-core-window CPI 相对误差累加
        ape_cnt = 0.0
        n_batches = len(dl)
        t0 = time.time()
        log(f"\n## {name}: 开始推理 {n_total} 窗口（本 rank {len(my_rows)}）"
            f" / {n_batches} batch x {world} ranks ...")
        with torch.no_grad():
            for bi, b in enumerate(dl):
                ids = b["input_ids"].to(device)
                attn = b["attention_mask"].to(device)
                qpos = b["query_pos"].to(device)
                ts = b["t_start"].to(device) if use_tstart else None
                raw = model(ids, attn, qpos, ts)
                pmu = invert_pred(raw.float()).cpu().numpy()  # [B,nc,K]
                label = b["label"].numpy()                    # [B,nc,K]
                macro = b["instr_retired"].numpy()            # [B,nc]
                mask = b["core_mask"].numpy().astype(bool)    # [B,nc]

                cpi_pred = pmu[:, :, CPI_IDX]
                cpi_label = label[:, :, CPI_IDX]
                m = mask
                sum_cyc_pred += float((cpi_pred * macro * m).sum())
                sum_cyc_label += float((cpi_label * macro * m).sum())
                sum_macro += float((macro * m).sum())
                ape = np.abs(cpi_pred - cpi_label) / (np.abs(cpi_label) + 1e-6)
                ape_sum += float(ape[m].sum())
                ape_cnt += float(m.sum())

                if (bi + 1) % 10 == 0 or (bi + 1) == n_batches:
                    el = time.time() - t0
                    rate = (bi + 1) / max(el, 1e-9)
                    run_pred = sum_cyc_pred / max(sum_macro, 1e-9)
                    run_lab = sum_cyc_label / max(sum_macro, 1e-9)
                    log(f"   [{name}] rank0 {bi + 1}/{n_batches} batch "
                        f"({rate * world:.1f} win-batch/s all-ranks, {el:.0f}s) "
                        f"running CPI(rank0) pred={run_pred:.4f} "
                        f"label={run_lab:.4f}")

        # 跨 rank 聚合
        if is_ddp:
            t = torch.tensor(
                [sum_cyc_pred, sum_cyc_label, sum_macro, ape_sum, ape_cnt],
                dtype=torch.float64, device=device)
            dist.all_reduce(t, op=dist.ReduceOp.SUM)
            sum_cyc_pred, sum_cyc_label, sum_macro, ape_sum, ape_cnt = \
                t.tolist()

        cpi_pred_g = sum_cyc_pred / max(sum_macro, 1e-9)
        cpi_label_g = sum_cyc_label / max(sum_macro, 1e-9)
        g_cyc, g_ins, cpi_gem5 = parse_gem5_stats(stats_path)

        err_vs_label = abs(cpi_pred_g - cpi_label_g) / abs(cpi_label_g)
        err_vs_gem5 = abs(cpi_pred_g - cpi_gem5) / abs(cpi_gem5)
        label_vs_gem5 = abs(cpi_label_g - cpi_gem5) / abs(cpi_gem5)
        win_mape = (ape_sum / ape_cnt) if ape_cnt > 0 else float("nan")

        log(f"\n## {name}   (windows={n_total})")
        log(f"  全局CPI  pred ={cpi_pred_g:.4f}")
        log(f"  全局CPI  label={cpi_label_g:.4f}   (窗口标签聚合)")
        log(f"  全局CPI  gem5 ={cpi_gem5:.4f}   (stats.txt 全程)")
        log(f"  误差  pred vs label = {err_vs_label*100:.2f}%   <- 纯模型预测能力")
        log(f"  误差  pred vs gem5  = {err_vs_gem5*100:.2f}%   <- 端到端(含窗口采样偏差)")
        log(f"  参考  label vs gem5 = {label_vs_gem5*100:.2f}%   <- 窗口采样本身的偏差")
        log(f"  per-window CPI MAPE = {win_mape*100:.2f}%")
        summary.append({
            "workload": name,
            "cpi_pred": cpi_pred_g, "cpi_label": cpi_label_g,
            "cpi_gem5": cpi_gem5,
            "err_pred_vs_label_pct": err_vs_label * 100,
            "err_pred_vs_gem5_pct": err_vs_gem5 * 100,
            "label_vs_gem5_pct": label_vs_gem5 * 100,
            "per_window_cpi_mape_pct": win_mape * 100,
            "windows": n_total,
        })

    log("\n" + "=" * 78)
    log("SUMMARY (JSON)")
    log(json.dumps(summary, indent=2, ensure_ascii=False))

    if is_ddp:
        dist.barrier()
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
