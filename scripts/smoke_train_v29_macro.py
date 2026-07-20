#!/usr/bin/env python3
"""Tiny overfit smoke on one real 256-macro multi-core context."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import torch
from torch import nn

from model.macro_v29_model import (
    MacroV29Config,
    MacroV29TimingModel,
    macro_v29_loss,
)
from train.macro_v29_dataset import (
    PackedTraceMacroContext,
    ParquetInstructionResolver,
    collate_macro_contexts,
)


class TinyNativeBackbone(nn.Module):
    def __init__(self, vocab_size: int, width: int):
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, width)
        self.projection = nn.Linear(width, width)

    def forward(self, input_ids, attention_mask, **kwargs):
        value = self.projection(self.embedding(input_ids))
        return type("TinyOutput", (), {
            "hidden_states": None,
            "last_hidden_state": value,
        })()


def gradient_norm(module: nn.Module) -> float:
    total = 0.0
    for parameter in module.parameters():
        if parameter.grad is not None:
            total += float(parameter.grad.detach().float().square().sum())
    return total ** 0.5


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--static-dict", required=True)
    parser.add_argument(
        "--tokenizer", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--steps", type=int, default=60)
    parser.add_argument("--lr", type=float, default=3.0e-3)
    parser.add_argument("--seed", type=int, default=3)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer, local_files_only=True,
    )
    context = PackedTraceMacroContext(args.trace_root)
    resolver = ParquetInstructionResolver(args.static_dict)
    windows = context.context_at_tick(
        context.roi_origin_tick, resolver, tokenizer,
    )
    batch = collate_macro_contexts(
        [windows], pad_token_id=int(tokenizer.pad_token_id or 0),
    )
    tensor_batch = {
        key: value for key, value in batch.items() if torch.is_tensor(value)
    }
    torch.manual_seed(int(args.seed))
    model = MacroV29TimingModel(
        TinyNativeBackbone(len(tokenizer), 32),
        MacroV29Config(d_llm=32, d_model=64, d_field=4, n_heads=4),
    )
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(args.lr))
    checkpoints = sorted(set([
        0,
        min(10, int(args.steps)),
        int(args.steps) // 2,
        int(args.steps),
    ]))
    curve = []
    gradient_report = {}
    started = time.perf_counter()
    for step in range(int(args.steps) + 1):
        predictions = model(tensor_batch)
        losses = macro_v29_loss(predictions, tensor_batch)
        if step in checkpoints:
            curve.append({
                "step": step,
                **{
                    key: float(value.detach())
                    for key, value in losses.items()
                },
            })
        if step >= int(args.steps):
            break
        optimizer.zero_grad(set_to_none=True)
        losses["total"].backward()
        if step == 0:
            gradient_report = {
                "native_backbone": gradient_norm(model.backbone),
                "numeric_encoder": gradient_norm(model.numeric_encoder),
                "timing_head": gradient_norm(model.gap_head),
            }
        torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
        optimizer.step()
    elapsed = time.perf_counter() - started
    initial = float(curve[0]["total"])
    final = float(curve[-1]["total"])
    failures = []
    if not final < initial * 0.5:
        failures.append(
            f"loss did not fall by 50%: initial={initial} final={final}"
        )
    for name, value in gradient_report.items():
        if not value > 0:
            failures.append(f"{name} gradient is zero")
    report = {
        "status": "PASS" if not failures else "FAIL",
        "kind": "tiny-real-context-overfit-not-semantic-accuracy",
        "trace_id": context.meta["trace_id"],
        "tokenizer": args.tokenizer,
        "rows": len(windows),
        "tokens_per_row": [
            int(window.model_inputs["attention_mask"].sum())
            for window in windows
        ],
        "steps": int(args.steps),
        "lr": float(args.lr),
        "seconds": elapsed,
        "loss_ratio": final / max(initial, 1.0e-12),
        "gradients_at_step0": gradient_report,
        "curve": curve,
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
