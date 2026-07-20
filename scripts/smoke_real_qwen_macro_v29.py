#!/usr/bin/env python3
"""Real local-Qwen smoke on a tiny tail of a real macro-v29 trace.

This is deliberately not an accuracy test.  It uses two real dynamic macros
so a CPU-only environment can still verify the actual Qwen weights, hidden
state interface, native-token spans, timing loss, and optional LoRA gradient.
"""
from __future__ import annotations

from argparse import ArgumentParser, Namespace
import gc
import json
from pathlib import Path
import sys
import time
from typing import Dict

import torch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from model.macro_v29_model import macro_v29_loss
from train.macro_v29_dataset import (
    PackedTraceMacroContext,
    ParquetInstructionResolver,
    collate_macro_contexts,
)
from train.train_macro_v29 import (
    build_timing_model,
    load_trainable,
    save_trainable,
    tensor_batch,
)


def gradient_norms(model: torch.nn.Module) -> Dict[str, float]:
    squared = {"backbone": 0.0, "numeric": 0.0, "timing": 0.0}
    for name, parameter in model.named_parameters():
        if parameter.grad is None:
            continue
        value = float(parameter.grad.detach().float().square().sum())
        if name.startswith("backbone."):
            group = "backbone"
        elif name.startswith(("numeric_encoder.", "side_projection.")):
            group = "numeric"
        else:
            group = "timing"
        squared[group] += value
    return {key: value ** 0.5 for key, value in squared.items()}


def main() -> int:
    parser = ArgumentParser()
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--static-dict", required=True)
    parser.add_argument(
        "--base-model", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--mode", choices=("frozen", "lora"), default="frozen")
    parser.add_argument("--tail-macros", type=int, default=2)
    parser.add_argument("--device", default="cpu")
    parser.add_argument(
        "--roundtrip-dir", default="",
        help="after a LoRA optimizer step, strictly save/reload and compare",
    )
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("requested CUDA but torch.cuda.is_available() is false")
    torch.manual_seed(1234)
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model, local_files_only=True,
    )
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    resolver = ParquetInstructionResolver(args.static_dict)
    context = PackedTraceMacroContext(args.trace_root)
    if len(context.core_ids) != 1:
        raise RuntimeError("real-Qwen CPU smoke requires a single-core trace")
    core_id = int(context.core_ids[0])
    view = context.views[core_id]
    n_tail = int(args.tail_macros)
    if not 1 <= n_tail <= min(256, view.n_macros):
        raise ValueError("tail-macros must be in [1,256]")
    cursor = view.n_macros - n_tail
    state_tick = (
        int(view.macro_end_tick[cursor - 1])
        if cursor > 0 else int(context.core_meta[core_id]["roi_begin_tick"])
    )
    windows = context.context_from_cursors(
        {core_id: cursor},
        resolver,
        tokenizer,
        state_tick=state_tick,
        state_time_cycles=(
            state_tick - context.roi_origin_tick
        ) / context.tick_per_cycle,
        include_labels=True,
        last_commit_cycles=None,
        max_tokens=4096,
    )
    batch = collate_macro_contexts(
        [windows], pad_token_id=int(tokenizer.pad_token_id),
    )
    model_args = Namespace(
        tiny_backbone=False,
        tiny_width=32,
        seed=1234,
        base_model=args.base_model,
        allow_download=False,
        semantic_variant="real",
        freeze_backbone=args.mode == "frozen",
        gradient_checkpointing=False,
        lora_r=8,
        lora_alpha=16,
        lora_dropout=0.0,
        d_model=64,
        d_field=4,
        n_heads=4,
    )
    load_started = time.perf_counter()
    model = build_timing_model(model_args, tokenizer).to(device)
    load_seconds = time.perf_counter() - load_started
    model.train(args.mode == "lora")
    model_batch = tensor_batch(batch, device)
    started = time.perf_counter()
    output = model(model_batch)
    losses = macro_v29_loss(output, model_batch)
    forward_seconds = time.perf_counter() - started
    backward_seconds = 0.0
    gradients = {"backbone": 0.0, "numeric": 0.0, "timing": 0.0}
    failures: list[str] = []
    if args.mode == "lora":
        started = time.perf_counter()
        losses["total"].backward()
        backward_seconds = time.perf_counter() - started
        gradients = gradient_norms(model)

    trainable_parameters = int(sum(
        parameter.numel() for parameter in model.parameters()
        if parameter.requires_grad
    ))
    backbone_class = type(model.backbone).__name__
    roundtrip = None
    if args.roundtrip_dir:
        if args.mode != "lora":
            raise ValueError("checkpoint roundtrip requires --mode lora")
        optimizer = torch.optim.AdamW(
            [
                parameter for parameter in model.parameters()
                if parameter.requires_grad
            ],
            lr=1.0e-4,
        )
        optimizer.step()
        model.eval()
        with torch.no_grad():
            reference = model(model_batch)["commit_time_macro"].detach().cpu()
        checkpoint_contract = {
            "base_model": args.base_model,
            "semantic_variant": "real",
            "tiny_backbone": False,
            "tiny_width": 32,
            "d_model": 64,
            "d_field": 4,
            "n_heads": 4,
            "tokenizer_size": len(tokenizer),
            "seed": 1234,
        }
        roundtrip_directory = Path(args.roundtrip_dir)
        roundtrip_directory.mkdir(parents=True, exist_ok=True)
        checkpoint = save_trainable(
            model,
            roundtrip_directory,
            1,
            contract=checkpoint_contract,
        )
        del optimizer
        del model
        gc.collect()
        reloaded = build_timing_model(model_args, tokenizer).to(device).eval()
        load_report = load_trainable(
            reloaded,
            checkpoint,
            expected_contract=checkpoint_contract,
        )
        with torch.no_grad():
            actual = reloaded(model_batch)["commit_time_macro"].detach().cpu()
        maximum_error = float((actual - reference).abs().max())
        roundtrip = {
            "checkpoint": str(checkpoint),
            "maximum_commit_error": maximum_error,
            "load": load_report,
        }
        if maximum_error > 1.0e-6:
            failures.append(
                f"checkpoint roundtrip commit error {maximum_error} exceeds 1e-6"
            )
        model = reloaded

    valid = model_batch["valid_macro_mask"].bool()
    monotonic = bool(torch.all(
        output["commit_time_macro"][:, 1:]
        >= output["commit_time_macro"][:, :-1]
    ).item())
    if not monotonic:
        failures.append("commit time is not monotonic")
    if not bool(torch.isfinite(losses["total"]).item()):
        failures.append("loss is not finite")
    if int(valid.sum()) != n_tail:
        failures.append("valid macro count differs from requested tail")
    if args.mode == "lora" and gradients["backbone"] <= 0.0:
        failures.append("LoRA backbone received no timing-loss gradient")
    report = {
        "status": "PASS" if not failures else "FAIL",
        "contract": "macro-v29-real-qwen-tail-smoke-v1",
        "mode": args.mode,
        "device": str(device),
        "base_model": args.base_model,
        "backbone_class": backbone_class,
        "tokenizer_size": len(tokenizer),
        "valid_macros": int(valid.sum()),
        "native_tokens": int(model_batch["attention_mask"].sum()),
        "uops": int(model_batch["uop_valid_mask"].sum()),
        "commit_shape": list(output["commit_time_macro"].shape),
        "monotonic": monotonic,
        "losses": {
            key: float(value.detach()) for key, value in losses.items()
        },
        "gradient_norms": gradients,
        "trainable_parameters": trainable_parameters,
        "checkpoint_roundtrip": roundtrip,
        "load_seconds": load_seconds,
        "forward_seconds": forward_seconds,
        "backward_seconds": backward_seconds,
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n")
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
