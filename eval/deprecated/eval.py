"""eval.py — 加载训练好的 LoRA+head，对 windows.jsonl/cache 做推理并报告指标。

用法：
  python eval/eval.py --data data/windows/windows.jsonl --ckpt ckpt/phase0 \
    --max-len 32768 --require-cache --device cuda
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import torch
from torch.utils.data import DataLoader, Subset

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.regression_head import PMU_KEYS
from model.llm_wrapper import LLMSimModel, WrapperConfig, build_tokenizer
from train.dataset import WindowDataset, make_collate
from train.loss import invert_pred
from eval.metrics import evaluate


def load_workload_index(jsonl_path: str) -> tuple[dict[str, list[int]], int]:
    idx: dict[str, list[int]] = {}
    n = 0
    with open(jsonl_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            idx.setdefault(rec.get("workload", "unknown"), []).append(n)
            n += 1
    return idx, n


def load_model(args, tok, device: str) -> tuple[LLMSimModel, bool]:
    cfg = WrapperConfig(max_len=args.max_len)
    model = LLMSimModel(cfg, tok).to(device)

    lora_dir = os.path.join(args.ckpt, "lora_best")
    if os.path.isdir(lora_dir):
        model.backbone.load_adapter(lora_dir, adapter_name="loaded")
        model.backbone.set_adapter("loaded")
    else:
        print(f"[WARN] missing LoRA adapter: {lora_dir}", flush=True)

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
            print("[WARN] ckpt 缺 new_token_embedding，推理结果无效！",
                  flush=True)
    else:
        print(f"[WARN] missing head checkpoint: {head_pt}", flush=True)
    model.eval()
    return model, use_tstart


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--bs", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=8192)
    ap.add_argument("--device", default=None,
                    help="cuda / cuda:0 / cpu；默认自动选择 cuda")
    ap.add_argument("--require-cache", action="store_true",
                    help="只读取已构建 ids_cache，避免评估时重建 cache")
    ap.add_argument("--no-cache", action="store_true",
                    help="强制从 windows.jsonl 现算 ids/qpos，不读取也不写 ids_cache")
    ap.add_argument("--workload", action="append", default=[],
                    help="只评估指定 workload，可重复传入；默认全量")
    ap.add_argument("--max-samples", type=int, default=0,
                    help="最多评估多少个 windows 样本（0=不限），用于 smoke")
    ap.add_argument("--num-workers", type=int, default=0)
    ap.add_argument("--progress-every", type=int, default=20,
                    help="每多少个 batch 输出一次累计评估指标；0=关闭")
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    if args.require_cache and args.no_cache:
        raise SystemExit("[err] --require-cache and --no-cache are mutually exclusive")

    t_all0 = time.perf_counter()
    timing = {}

    print(f"[init] device={device} data={args.data} ckpt={args.ckpt} "
          f"max_len={args.max_len} require_cache={args.require_cache} "
          f"no_cache={args.no_cache}",
          flush=True)
    t0 = time.perf_counter()
    tok = build_tokenizer()
    timing["tokenizer_s"] = time.perf_counter() - t0
    print(f"[time] tokenizer_s={timing['tokenizer_s']:.3f}", flush=True)

    t0 = time.perf_counter()
    model, use_tstart = load_model(args, tok, device)
    timing["model_ckpt_s"] = time.perf_counter() - t0
    print(f"[init] model ready, use_tstart={use_tstart}", flush=True)
    print(f"[time] model_ckpt_s={timing['model_ckpt_s']:.3f}", flush=True)

    t0 = time.perf_counter()
    ds = WindowDataset(args.data, tok, max_len=args.max_len,
                       cache_path=WindowDataset.default_cache_path(
                           args.data, args.max_len),
                       require_cache=args.require_cache,
                       use_cache=not args.no_cache)
    timing["dataset_preprocess_s"] = time.perf_counter() - t0
    print(f"[data] mode={ds.mode} windows={len(ds)} cache={ds.cache_path}",
          flush=True)
    print(f"[time] dataset_preprocess_s={timing['dataset_preprocess_s']:.3f}",
          flush=True)

    t0 = time.perf_counter()
    selected = None
    if args.workload:
        wl_index, jsonl_total = load_workload_index(args.data)
        if jsonl_total != len(ds):
            raise SystemExit(
                f"[err] jsonl/cache 样本数不一致: jsonl={jsonl_total} "
                f"cache={len(ds)}，无法按 workload 过滤"
            )
        selected = []
        for w in args.workload:
            rows = wl_index.get(w, [])
            print(f"[data] workload={w} windows={len(rows)}", flush=True)
            selected.extend(rows)
        selected = sorted(set(selected))
    if selected is not None:
        if args.max_samples > 0:
            selected = selected[:args.max_samples]
        ds_eval = Subset(ds, selected)
    elif args.max_samples > 0:
        ds_eval = Subset(ds, list(range(min(args.max_samples, len(ds)))))
    else:
        ds_eval = ds
    timing["workload_filter_s"] = time.perf_counter() - t0
    print(f"[data] eval_windows={len(ds_eval)}", flush=True)
    print(f"[time] workload_filter_s={timing['workload_filter_s']:.3f}",
          flush=True)

    dl = DataLoader(ds_eval, batch_size=args.bs, shuffle=False,
                    collate_fn=make_collate(tok.pad_token_id),
                    num_workers=args.num_workers)

    t0 = time.perf_counter()
    preds, trues = [], []
    with torch.no_grad():
        for bi, b in enumerate(dl):
            b = {k: v.to(device) for k, v in b.items()}
            ts = b["t_start"] if use_tstart else None
            raw = model(b["input_ids"], b["attention_mask"], b["query_pos"], ts)
            pmu = invert_pred(raw.float())              # [B,nc,K] 原始量纲
            mask = b["core_mask"].bool()
            preds.append(pmu[mask].cpu().numpy())
            trues.append(b["label"][mask].cpu().numpy())
            if args.progress_every > 0 and (bi + 1) % args.progress_every == 0:
                cur_pred = np.concatenate(preds, axis=0)
                cur_true = np.concatenate(trues, axis=0)
                cur = evaluate(cur_pred, cur_true)
                print(
                    f"[eval] batches={bi + 1} "
                    f"samples={min((bi + 1) * args.bs, len(ds_eval))} "
                    f"core_windows={cur_pred.shape[0]} "
                    f"mae_cpi_uop={cur['mae_cpi_uop']:.4f} "
                    f"mape_cpi_uop={cur['mape_cpi_uop']:.4f} "
                    f"cpi10={cur['cpi_within_10pct']:.3f} "
                    f"cpi20={cur['cpi_within_20pct']:.3f}",
                    flush=True,
                )
    if device.startswith("cuda"):
        torch.cuda.synchronize()
    timing["forward_eval_s"] = time.perf_counter() - t0
    print(f"[time] forward_eval_s={timing['forward_eval_s']:.3f}", flush=True)

    pred = np.concatenate(preds, axis=0)
    true = np.concatenate(trues, axis=0)
    res = evaluate(pred, true)
    res["num_core_windows"] = int(pred.shape[0])
    res["pmu_keys"] = PMU_KEYS
    timing["total_s"] = time.perf_counter() - t_all0
    res["timing_s"] = timing
    print(
        "[time] summary "
        + " ".join(f"{k}={v:.3f}" for k, v in timing.items()),
        flush=True,
    )
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
