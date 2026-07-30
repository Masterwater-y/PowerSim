#!/usr/bin/env python3
"""Estimate B1 training impact from v29 timing/branch gradient alignment.

The canonical checkpoint is never updated.  For deterministic validation and
development-heldout sequences, the script compares gradients of the retained
timing objective with the weighted branch auxiliary objective on parameters
that B1 would keep (shared trunk and gap head).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import time
from collections import defaultdict
from contextlib import nullcontext
from typing import Any, Mapping, Sequence

import numpy as np
import torch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in os.sys.path:
    os.sys.path.insert(0, REPO_ROOT)

from tcsim.v29.dataset import (  # noqa: E402
    V29GlobalTimeDataset,
    collate_v29_sequences,
)
from tcsim.v29.losses import compute_v29_losses  # noqa: E402
from tcsim.v29.model import build_model  # noqa: E402


SCHEMA_VERSION = "tcsim-v30-b1-gradient-alignment-audit-1"
CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def _records(manifest: Mapping[str, Any], splits: Sequence[str]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for split in splits:
        for raw in manifest.get("splits", {}).get(split, []):
            row = dict(raw)
            trace_id = str(row["trace_id"])
            row["audit_split"] = split
            selected.setdefault(trace_id, row)
    return sorted(selected.values(), key=lambda row: str(row["trace_id"]))


def _torch_load(path: str, device: torch.device) -> Mapping[str, Any]:
    try:
        return torch.load(path, map_location=device, weights_only=False)
    except TypeError:
        return torch.load(path, map_location=device)


def _device_batch(batch: Mapping[str, Any], device: torch.device) -> dict[str, Any]:
    return {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }


def _sample_indices(
    dataset: V29GlobalTimeDataset, per_trace: int
) -> list[int]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, trace_id in enumerate(dataset.sample_trace_ids):
        groups[str(trace_id)].append(index)
    selected = []
    for trace_id in sorted(groups):
        values = groups[trace_id]
        count = min(max(1, per_trace), len(values))
        positions = np.linspace(0, len(values) - 1, num=count, dtype=np.int64)
        selected.extend(values[int(position)] for position in positions)
    return selected


def _parameter_group(name: str) -> str | None:
    if name.startswith("static_encoder."):
        return "static_encoder"
    if name.startswith("interaction.layers."):
        pieces = name.split(".")
        return f"qkvr_layer_{int(pieces[2]):02d}"
    if name.startswith("interaction.final_norm."):
        return "interaction_final_norm"
    if name.startswith("interaction."):
        return "interaction_input_and_gate"
    if name.startswith("gap_head."):
        return "gap_head"
    return None


def _loss_kwargs(config: Mapping[str, Any]) -> dict[str, Any]:
    train = config["train"]
    return {
        "weights": train["loss_weights"],
        "time_beta": float(train.get("time_huber_beta", 0.2)),
        "progress_count_beta": float(train.get("progress_count_beta", 8.0)),
        "branch_count_beta": float(train.get("branch_count_beta", 1.0)),
    }


def _new_accumulator() -> dict[str, Any]:
    return {
        "batches": 0,
        "dot": 0.0,
        "timing_norm2": 0.0,
        "branch_norm2": 0.0,
        "batch_cosine_sum": 0.0,
        "batch_cosine_count": 0,
        "positive_batches": 0,
        "negative_batches": 0,
        "zero_batches": 0,
    }


def _update(
    accumulator: dict[str, Any], dot: float, timing_norm2: float, branch_norm2: float
) -> None:
    accumulator["batches"] += 1
    accumulator["dot"] += dot
    accumulator["timing_norm2"] += timing_norm2
    accumulator["branch_norm2"] += branch_norm2
    denominator = math.sqrt(max(0.0, timing_norm2 * branch_norm2))
    if denominator > 0:
        cosine = dot / denominator
        accumulator["batch_cosine_sum"] += cosine
        accumulator["batch_cosine_count"] += 1
        if cosine > 1.0e-8:
            accumulator["positive_batches"] += 1
        elif cosine < -1.0e-8:
            accumulator["negative_batches"] += 1
        else:
            accumulator["zero_batches"] += 1
    else:
        accumulator["zero_batches"] += 1


def _finalize(accumulator: Mapping[str, Any]) -> dict[str, Any]:
    timing_norm2 = float(accumulator["timing_norm2"])
    branch_norm2 = float(accumulator["branch_norm2"])
    denominator = math.sqrt(max(0.0, timing_norm2 * branch_norm2))
    batch_count = int(accumulator["batch_cosine_count"])
    return {
        **dict(accumulator),
        "pooled_cosine": float(accumulator["dot"]) / denominator if denominator else None,
        "mean_batch_cosine": (
            float(accumulator["batch_cosine_sum"]) / batch_count
            if batch_count else None
        ),
        "weighted_branch_to_timing_gradient_norm": (
            math.sqrt(branch_norm2 / timing_norm2) if timing_norm2 > 0 else None
        ),
        "positive_batch_fraction": (
            int(accumulator["positive_batches"]) / batch_count if batch_count else None
        ),
        "negative_batch_fraction": (
            int(accumulator["negative_batches"]) / batch_count if batch_count else None
        ),
    }


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v30 B1 timing/branch gradient alignment audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Checkpoint: `{report['selection']['checkpoint']}`",
        f"- Sampled sequences: `{report['selection']['sampled_sequences']}`",
        "- Model updates/optimizer steps: `0`",
        "",
        "The branch gradient includes the configured v29 weights for token and count losses. Only parameters retained by B1 are included.",
        "",
        "| scope | parameter group | pooled cosine | mean batch cosine | branch/timing norm | positive batches |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for scope, groups in report["alignment"].items():
        for group, row in groups.items():
            pooled = row["pooled_cosine"]
            mean = row["mean_batch_cosine"]
            ratio = row["weighted_branch_to_timing_gradient_norm"]
            positive = row["positive_batch_fraction"]
            lines.append(
                f"| `{scope}` | `{group}` | "
                f"{pooled if pooled is not None else float('nan'):+.5f} | "
                f"{mean if mean is not None else float('nan'):+.5f} | "
                f"{ratio if ratio is not None else float('nan'):.5f} | "
                f"{100*positive if positive is not None else float('nan'):.1f}% |"
            )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- Positive cosine means the branch auxiliary locally helps the retained timing path; deleting it is a regularization risk.",
        "- Negative cosine means local gradient conflict; B1 may help optimization.",
        "- A tiny branch/timing norm means removal is unlikely to materially change the shared path even if cosine is noisy.",
        "- This is a local checkpoint diagnostic, not a guarantee about a from-scratch B1 run.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=os.path.join(REPO_ROOT, "data/v30_branch_replay_dataset/manifest.json"),
    )
    parser.add_argument(
        "--checkpoint",
        default=os.path.join(REPO_ROOT, "ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt"),
    )
    parser.add_argument("--splits", default="validation,development_heldout")
    parser.add_argument("--sequences-per-trace", type=int, default=1)
    parser.add_argument("--max-sequences", type=int, default=0)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--progress-every", type=int, default=10)
    parser.add_argument(
        "--out",
        default=os.path.join(REPO_ROOT, "logs", f"v30_b1_no_train_gradients_{timestamp}"),
    )
    args = parser.parse_args()
    if args.sequences_per_trace <= 0:
        raise ValueError("sequences-per-trace must be positive")
    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable")
    manifest_path = os.path.abspath(args.manifest)
    checkpoint = os.path.abspath(args.checkpoint)
    rows = _records(_load_json(manifest_path), _csv(args.splits))
    dataset = V29GlobalTimeDataset(rows, sequence_length=4, sequence_stride=4)
    selected = _sample_indices(dataset, args.sequences_per_trace)
    if args.max_sequences > 0:
        selected = selected[: args.max_sequences]
    payload = _torch_load(checkpoint, torch.device("cpu"))
    config = payload["config"]
    model = build_model(config["model"], config["chunk"]["horizons"])
    model.load_state_dict(payload["model"], strict=True)
    model.to(device)
    model.eval()
    parameter_entries = [
        (name, parameter, _parameter_group(name))
        for name, parameter in model.named_parameters()
        if _parameter_group(name) is not None
    ]
    parameters = [parameter for _name, parameter, _group in parameter_entries]
    groups = [str(group) for _name, _parameter, group in parameter_entries]
    row_meta = {str(row["trace_id"]): dict(row) for row in rows}
    accumulators: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(_new_accumulator)
    )
    started = time.perf_counter()
    for ordinal, index in enumerate(selected, start=1):
        item = dataset[int(index)]
        meta = row_meta[str(item["trace_id"])]
        batch = _device_batch(collate_v29_sequences([item]), device)
        amp = (
            torch.autocast(device_type="cuda", dtype=torch.bfloat16)
            if device.type == "cuda" else nullcontext()
        )
        with amp:
            predictions = model(batch)
            losses = compute_v29_losses(
                predictions, batch, **_loss_kwargs(config),
            )
            weights = config["train"]["loss_weights"]
            timing_loss = (
                float(weights.get("commit_time", 1.0)) * losses.commit_time
                + float(weights.get("prefix_bce", 0.5)) * losses.prefix_bce
                + float(weights.get("progress_count", 0.5)) * losses.progress_count
                + float(weights.get("cumulative", 0.25)) * losses.cumulative
            )
            branch_loss = (
                float(weights.get("branch_token", 0.1)) * losses.branch_token
                + float(weights.get("branch_count", 0.1)) * losses.branch_count
            )
        timing_gradients = torch.autograd.grad(
            timing_loss, parameters, retain_graph=True, allow_unused=True,
        )
        branch_gradients = torch.autograd.grad(
            branch_loss, parameters, retain_graph=False, allow_unused=True,
        )
        per_group: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        for group, timing_gradient, branch_gradient in zip(
            groups, timing_gradients, branch_gradients,
        ):
            if timing_gradient is None or branch_gradient is None:
                continue
            timing_value = timing_gradient.detach().float()
            branch_value = branch_gradient.detach().float()
            per_group[group][0] += float((timing_value * branch_value).sum().cpu())
            per_group[group][1] += float(timing_value.square().sum().cpu())
            per_group[group][2] += float(branch_value.square().sum().cpu())
        retained_total = [0.0, 0.0, 0.0]
        for values in per_group.values():
            for value_index, value in enumerate(values):
                retained_total[value_index] += value
        per_group["retained_total"] = retained_total
        scopes = (
            "all",
            f"role:{meta.get('workload_role', 'unknown')}",
            f"core:{int(meta['n_cores'])}",
            f"split:{meta.get('audit_split', 'unknown')}",
        )
        for group, (dot, timing_norm2, branch_norm2) in per_group.items():
            for scope in scopes:
                _update(accumulators[scope][group], dot, timing_norm2, branch_norm2)
        del predictions, losses, timing_gradients, branch_gradients, batch
        if ordinal % max(1, args.progress_every) == 0:
            print(
                f"[v30-b1-gradient] {ordinal}/{len(selected)} "
                f"elapsed={time.perf_counter()-started:.1f}s",
                flush=True,
            )
    alignment = {
        scope: {
            group: _finalize(accumulator)
            for group, accumulator in sorted(group_values.items())
        }
        for scope, group_values in sorted(accumulators.items())
    }
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
        "selection": {
            "manifest": manifest_path,
            "checkpoint": checkpoint,
            "checkpoint_step": int(payload["step"]),
            "splits": list(_csv(args.splits)),
            "sequences_per_trace": args.sequences_per_trace,
            "sampled_sequences": len(selected),
            "elapsed_seconds": time.perf_counter() - started,
        },
        "alignment": alignment,
    }
    output = os.path.abspath(args.out)
    os.makedirs(output, exist_ok=True)
    json_path = os.path.join(output, "report.json")
    markdown_path = os.path.join(output, "report.md")
    _dump_json(json_path, report)
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(f"[v30-b1-gradient] json={json_path} markdown={markdown_path}")
    print(_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
