"""eval.py — 加载训练好的 LoRA+head，对 windows.jsonl 做推理并报告指标。

用法：
  python eval/eval.py --data data/windows/windows.jsonl --ckpt ckpt/phase0
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import torch
from torch.utils.data import DataLoader

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model.llm_wrapper import LLMSimModel, WrapperConfig, build_tokenizer
from train.dataset import WindowDataset, make_collate
from train.loss import invert_pred
from eval.metrics import evaluate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--bs", type=int, default=2)
    ap.add_argument("--max-len", type=int, default=8192)
    args = ap.parse_args()

    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    tok = build_tokenizer()
    cfg = WrapperConfig(max_len=args.max_len)
    model = LLMSimModel(cfg, tok).to(device)

    # 加载 LoRA + head
    lora_dir = os.path.join(args.ckpt, "lora_best")
    if os.path.isdir(lora_dir):
        from peft import PeftModel  # noqa
        model.backbone.load_adapter(lora_dir, adapter_name="default")
    head_pt = os.path.join(args.ckpt, "head_best.pt")
    if os.path.isfile(head_pt):
        sd = torch.load(head_pt, map_location=device)
        model.head.load_state_dict(sd["head"])
    model.eval()

    ds = WindowDataset(args.data, tok, max_len=args.max_len)
    dl = DataLoader(ds, batch_size=args.bs, shuffle=False,
                    collate_fn=make_collate(tok.pad_token_id))

    preds, trues = [], []
    with torch.no_grad():
        for b in dl:
            b = {k: v.to(device) for k, v in b.items()}
            raw = model(b["input_ids"], b["attention_mask"], b["query_pos"])
            pmu = invert_pred(raw.float())              # [B,nc,K] 原始量纲
            mask = b["core_mask"].bool()
            preds.append(pmu[mask].cpu().numpy())
            trues.append(b["label"][mask].cpu().numpy())
    pred = np.concatenate(preds, axis=0)
    true = np.concatenate(trues, axis=0)
    res = evaluate(pred, true)
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
