#!/usr/bin/env python3
"""Initial real-data gate for the 256-macro native-token contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.macro_v29_rollout import MacroV29ModelPredictor, model_free_rollout
from eval.macro_v29_scheduler import oracle_rollout
from train.macro_v29_dataset import (
    DEFAULT_K_MACRO,
    MacroContractError,
    PackedCoreMacroView,
    PackedTraceMacroContext,
    ParquetInstructionResolver,
    assert_model_input_allowlist,
    collate_macro_contexts,
)


def first_resolvable_cursor(
    view: PackedCoreMacroView,
    resolver: ParquetInstructionResolver,
    *,
    search_limit: int,
) -> int:
    for proposed in range(min(search_limit, view.n_macros)):
        if proposed:
            state_tick = int(view.macro_end_tick[proposed - 1])
            cursor = view.cursor_at_tick(state_tick)
        else:
            cursor = 0
        stop = min(view.n_macros, cursor + view.k_macro)
        report = resolver.coverage(view.macro_pc[cursor:stop])
        if report["n_missing"] == 0 and report["n_invalid"] == 0:
            return cursor
    raise MacroContractError(
        f"no resolvable {view.k_macro}-macro window in first {search_limit} cursors"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-root", required=True)
    parser.add_argument("--static-dict", required=True)
    parser.add_argument(
        "--tokenizer", default="Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )
    parser.add_argument("--k-macro", type=int, default=DEFAULT_K_MACRO)
    parser.add_argument("--stride-macro", type=int, default=256)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--sample-windows-per-core", type=int, default=3)
    parser.add_argument("--head-search-limit", type=int, default=64)
    parser.add_argument(
        "--free-rollout-steps", type=int, default=8,
        help="bounded label-free tiny-model rollout steps",
    )
    parser.add_argument("--out", default="")
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="write all findings but return zero even when strict gates fail",
    )
    args = parser.parse_args()

    from transformers import AutoTokenizer

    root = Path(args.trace_root)
    meta = json.loads((root / "meta.json").read_text())
    tick_per_cycle = float(meta["tick_per_cycle"])
    core_ids = [int(value) for value in meta["core_ids"]]
    resolver = ParquetInstructionResolver(args.static_dict)
    tokenizer = AutoTokenizer.from_pretrained(
        args.tokenizer, local_files_only=True,
    )
    tokenizer_size = len(tokenizer)
    views: List[PackedCoreMacroView] = []
    core_reports: List[Dict[str, Any]] = []
    failures: List[str] = []
    token_lengths: List[int] = []

    for core_id in core_ids:
        view = PackedCoreMacroView(
            root / "cores" / str(core_id),
            tick_per_cycle=tick_per_cycle,
            k_macro=int(args.k_macro),
        )
        views.append(view)
        structure = view.validate_contract()
        tie_guard = int(args.k_macro) - int(args.stride_macro)
        if tie_guard > 0 and int(structure["max_same_tick_macros"]) > tie_guard:
            failures.append(
                f"core {core_id}: max same-tick group "
                f"{structure['max_same_tick_macros']} > tie guard {tie_guard}"
            )
        coverage = resolver.coverage(view.macro_pc)
        if coverage["n_missing"] or coverage["n_invalid"]:
            failures.append(
                f"core {core_id}: static coverage missing={coverage['n_missing']} "
                f"invalid={coverage['n_invalid']}"
            )
        cursor = first_resolvable_cursor(
            view, resolver, search_limit=int(args.head_search_limit),
        )
        if cursor:
            failures.append(
                f"core {core_id}: first {cursor} macro(s) are not native-resolvable"
            )
        first_tick = int(meta["sample_grid"]["start_tick"])
        if cursor:
            first_tick = max(first_tick, int(view.macro_end_tick[cursor - 1]))
        last_tick = max(first_tick, int(view.macro_end_tick[-1]) - 1)
        sample_ticks = sorted(set(
            int(value) for value in np.linspace(
                first_tick, last_tick, int(args.sample_windows_per_core),
            )
        ))
        sampled = []
        for state_tick in sample_ticks:
            sample_cursor = view.cursor_at_tick(state_tick)
            window = view.window_from_cursor(
                sample_cursor, state_tick=state_tick,
            )
            view.attach_native_tokens(
                window,
                resolver,
                tokenizer,
                max_tokens=int(args.max_tokens),
            )
            length = int(len(window.model_inputs["input_ids"]))
            token_lengths.append(length)
            sampled.append({
                "cursor": int(sample_cursor),
                "tokens": length,
                "valid_macros": int(window.control["n_valid_macros"]),
            })
        core_reports.append({
            "core_id": core_id,
            **structure,
            "static_coverage": coverage,
            "first_resolvable_cursor": int(cursor),
            "sampled_windows": sampled,
        })

    if len(tokenizer) != tokenizer_size:
        failures.append("tokenizer vocabulary changed during validation")

    oracle = oracle_rollout(
        [view.macro_end_tick for view in views],
        [view.macro_uop_begin for view in views],
        [view.macro_uop_end for view in views],
        tick_per_cycle=tick_per_cycle,
        start_tick=int(meta["sample_grid"]["start_tick"]),
        k_macro=int(args.k_macro),
        target_stride_macro=int(args.stride_macro),
    )
    expected_macros = sum(view.n_macros for view in views)
    expected_uops = sum(view.n_uops for view in views)
    if int(oracle["total_macros"]) != expected_macros:
        failures.append("oracle macro total mismatch")
    if int(oracle["total_uops"]) != expected_uops:
        failures.append("oracle UOP total mismatch")

    context_builder = PackedTraceMacroContext(
        root, k_macro=int(args.k_macro),
    )
    context_windows = context_builder.context_at_tick(
        int(meta["sample_grid"]["start_tick"]),
        resolver,
        tokenizer,
        max_tokens=int(args.max_tokens),
    )
    for window in context_windows:
        assert_model_input_allowlist(window)
    context_batch = collate_macro_contexts(
        [context_windows], pad_token_id=int(tokenizer.pad_token_id or 0),
    )
    expected_shapes = {
        "chunk_summary": (len(core_ids), 38),
        "relation_features": (len(core_ids), 22),
        "state_features": (len(core_ids), 5),
        "uarch_features": (len(core_ids), 29),
    }
    context_shapes = {
        key: tuple(int(value) for value in context_batch[key].shape)
        for key in (*expected_shapes, "uop_fields", "dynamic_uop_fields",
                    "uop_valid_mask", "uop_to_macro")
    }
    for key, expected in expected_shapes.items():
        if context_shapes[key] != expected:
            failures.append(
                f"context {key} shape {context_shapes[key]} != {expected}"
            )
    ragged_rows = len(core_ids)
    if (
        context_batch["uop_fields"].ndim != 3
        or tuple(context_batch["uop_fields"].shape[::2])
        != (ragged_rows, 26)
    ):
        failures.append(f"invalid ragged uop_fields shape {context_shapes['uop_fields']}")
    if (
        context_batch["dynamic_uop_fields"].shape[:2]
        != context_batch["uop_fields"].shape[:2]
        or context_batch["dynamic_uop_fields"].shape[-1] != 8
    ):
        failures.append("ragged dynamic/static UOP shapes differ")
    if context_batch["uop_valid_mask"].shape != context_batch["uop_to_macro"].shape:
        failures.append("ragged mask/segment shapes differ")
    valid_uops = context_batch["uop_valid_mask"].sum(dim=1)
    counted_uops = context_batch["uop_count"].sum(dim=1)
    if not bool((valid_uops == counted_uops).all().item()):
        failures.append("ragged valid UOP count does not equal macro segment counts")
    free_windows = context_builder.context_from_cursors(
        {core_id: 0 for core_id in core_ids},
        resolver,
        tokenizer,
        state_tick=None,
        state_time_cycles=0.0,
        include_labels=False,
        last_commit_cycles={core_id: 0.0 for core_id in core_ids},
        max_tokens=int(args.max_tokens),
    )
    free_label_keys = sorted({
        key for window in free_windows for key in window.labels
    })
    if free_label_keys:
        failures.append(f"free context exposes labels: {free_label_keys}")
    for window in free_windows:
        assert_model_input_allowlist(window)

    import time
    import torch
    from torch import nn
    from model.macro_v29_model import (
        MacroV29Config, MacroV29TimingModel, macro_v29_loss,
    )

    class TinyNativeBackbone(nn.Module):
        def __init__(self, vocab_size: int, width: int):
            super().__init__()
            self.embedding = nn.Embedding(vocab_size, width)
            self.projection = nn.Linear(width, width)

        def forward(self, input_ids, attention_mask, **kwargs):
            value = self.projection(self.embedding(input_ids))
            return type("TinyOutput", (), {
                "hidden_states": None, "last_hidden_state": value,
            })()

    torch.manual_seed(17)
    tiny = MacroV29TimingModel(
        TinyNativeBackbone(tokenizer_size, 32),
        MacroV29Config(d_llm=32, d_model=64, d_field=4, n_heads=4),
    ).eval()
    model_batch = {
        key: value for key, value in context_batch.items()
        if torch.is_tensor(value)
    }
    started = time.perf_counter()
    tiny_output = tiny(model_batch)
    tiny_loss = macro_v29_loss(tiny_output, model_batch)
    tiny_seconds = time.perf_counter() - started
    tiny_monotonic = bool(torch.all(
        tiny_output["commit_time_macro"][:, 1:]
        >= tiny_output["commit_time_macro"][:, :-1]
    ).item())
    if not tiny_monotonic:
        failures.append("tiny real-context model output is not monotonic")
    if not bool(torch.isfinite(tiny_loss["total"]).item()):
        failures.append("tiny real-context model loss is not finite")

    predictor = MacroV29ModelPredictor(
        context_builder,
        resolver,
        tokenizer,
        tiny,
        device="cpu",
        max_tokens=int(args.max_tokens),
    )
    predicted_rollout = model_free_rollout(
        context_builder,
        predictor,
        target_stride_macro=int(args.stride_macro),
        max_steps=int(args.free_rollout_steps),
    )
    if int(predicted_rollout["total_consumed_macros"]) <= 0:
        failures.append("label-free tiny-model rollout made no macro progress")
    if predicted_rollout["free_context_label_keys"]:
        failures.append(
            "label-free tiny-model rollout exposed labels: "
            f"{predicted_rollout['free_context_label_keys']}"
        )
    if not bool(predicted_rollout["functional_state"]["available"]):
        failures.append("label-free rollout did not update functional state")
    if bool(predicted_rollout["functional_state"]["raw_line_ids_exposed"]):
        failures.append("functional rollout exposed raw physical-line IDs")
    if int(predicted_rollout["total_consumed_uops"]) != sum(
        int(value) for value in predicted_rollout["uop_cursors"].values()
    ):
        failures.append("bounded rollout UOP accounting differs from cursors")

    report = {
        "status": "PASS" if not failures else "FAIL",
        "dataset_schema": "global-time-v29-macro-native-2",
        "trace_id": meta["trace_id"],
        "workload": meta["workload"],
        "k_macro": int(args.k_macro),
        "stride_macro": int(args.stride_macro),
        "tail_lookahead_macro": int(args.k_macro) - int(args.stride_macro),
        "boundary_policy": "allow_zero_cycle_replan",
        "uop_representation": "ragged-segment-no-truncation",
        "max_tokens": int(args.max_tokens),
        "tokenizer": args.tokenizer,
        "tokenizer_size": tokenizer_size,
        "token_lengths": {
            "n": len(token_lengths),
            "min": min(token_lengths) if token_lengths else 0,
            "max": max(token_lengths) if token_lengths else 0,
            "mean": float(np.mean(token_lengths)) if token_lengths else 0.0,
        },
        "cores": core_reports,
        "oracle_rollout": oracle,
        "tiny_model_smoke": {
            "seconds": tiny_seconds,
            "commit_shape": list(tiny_output["commit_time_macro"].shape),
            "monotonic": tiny_monotonic,
            "loss": float(tiny_loss["total"].detach()),
        },
        "tiny_model_free_rollout": predicted_rollout,
        "multi_core_context": {
            "sample_ptr": context_batch["sample_ptr"].tolist(),
            "shapes": {key: list(value) for key, value in context_shapes.items()},
            "free_context_label_keys": free_label_keys,
        },
        "failures": failures,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n")
    if failures and not args.diagnostic:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
