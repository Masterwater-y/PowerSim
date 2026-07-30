#!/usr/bin/env python3
"""Trace-equal oracle-window evaluation of a trained v30 GSS P1 adapter.

This is a mechanism probe, not a free-running rollout: GSS inputs are the
ready-clock teacher-order pre-access sidecars.  The baseline and candidate
share one frozen backbone evaluation; only the zero/learned GSS residual is
different.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from scripts.train_v29 import _manifest_sources
from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences
from tcsim.v29.model import _monotonic_prefix_sum, build_model
from tcsim.utils.io import dump_json


CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}
METRICS = (
    "commit_log_mae", "memory_commit_log_mae", "nonmemory_commit_log_mae",
    "progress_mae",
)


def _torch_load(path: str) -> Mapping[str, Any]:
    try:
        return torch.load(path, map_location="cpu", weights_only=False)
    except TypeError:
        return torch.load(path, map_location="cpu")


def _selected_indices(dataset: V29GlobalTimeDataset, per_trace: int) -> list[int]:
    by_trace: Dict[str, list[int]] = defaultdict(list)
    for index, trace_id in enumerate(dataset.sample_trace_ids):
        by_trace[str(trace_id)].append(index)
    selected: list[int] = []
    for trace_id in sorted(by_trace):
        values = by_trace[trace_id]
        count = min(int(per_trace), len(values))
        if count == 1:
            positions = [len(values) // 2]
        else:
            positions = [
                int(round(i * (len(values) - 1) / (count - 1)))
                for i in range(count)
            ]
        selected.extend(values[position] for position in positions)
    return selected


def _device_batch(batch: Mapping[str, Any], device: torch.device) -> Dict[str, Any]:
    return {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }


def _empty_stats() -> Dict[str, Dict[str, float]]:
    return {
        variant: {
            **{f"{name}_sum": 0.0 for name in METRICS},
            "commit_count": 0.0,
            "memory_count": 0.0,
            "nonmemory_count": 0.0,
            "progress_count": 0.0,
        }
        for variant in ("base", "gss")
    }


def _add_variant(
    stats: Dict[str, float],
    commit_time: torch.Tensor,
    progress: torch.Tensor,
    batch: Mapping[str, torch.Tensor],
) -> None:
    valid = batch["valid_uop_mask"].bool()
    memory = batch["gss_memory_mask"].bool() & valid
    nonmemory = valid & ~memory
    true_time = batch["commit_time_target"].clamp(min=0.0)
    error = (
        torch.log1p(commit_time.clamp(min=0.0))
        - torch.log1p(true_time)
    ).abs()
    stats["commit_log_mae_sum"] += float(error[valid].sum())
    stats["memory_commit_log_mae_sum"] += float(error[memory].sum())
    stats["nonmemory_commit_log_mae_sum"] += float(error[nonmemory].sum())
    stats["progress_mae_sum"] += float(
        (progress - batch["progress_target"]).abs().sum()
    )
    stats["commit_count"] += int(valid.sum())
    stats["memory_count"] += int(memory.sum())
    stats["nonmemory_count"] += int(nonmemory.sum())
    stats["progress_count"] += int(progress.numel())


def _finalize(raw: Mapping[str, Mapping[str, float]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for variant in ("base", "gss"):
        values = raw[variant]
        out[variant] = {
            "commit_log_mae": values["commit_log_mae_sum"]
            / max(1.0, values["commit_count"]),
            "memory_commit_log_mae": values["memory_commit_log_mae_sum"]
            / max(1.0, values["memory_count"]),
            "nonmemory_commit_log_mae": values["nonmemory_commit_log_mae_sum"]
            / max(1.0, values["nonmemory_count"]),
            "progress_mae": values["progress_mae_sum"]
            / max(1.0, values["progress_count"]),
        }
    out["relative_percent"] = {
        name: 100.0 * (
            out["gss"][name] / max(1e-12, out["base"][name]) - 1.0
        )
        for name in METRICS
    }
    return out


def _merge_stats(items: Iterable[Mapping[str, Mapping[str, float]]]):
    merged = _empty_stats()
    for item in items:
        for variant in ("base", "gss"):
            for key, value in item[variant].items():
                merged[variant][key] += float(value)
    return merged


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v30 GSS P1 oracle-window evaluation", "",
        "> 这是 ready-clock teacher-order oracle-window 机制评估，不是 free-running rollout。",
        "", "## Aggregate", "",
        "| weighting | metric | base | GSS | relative |",
        "|---|---|---:|---:|---:|",
    ]
    for weighting in ("token_weighted", "trace_equal"):
        row = report["aggregate"][weighting]
        for name in METRICS:
            lines.append(
                f"| {weighting} | {name} | {row['base'][name]:.6f} | "
                f"{row['gss'][name]:.6f} | {row['relative_percent'][name]:+.2f}% |"
            )
    lines.extend(["", "## Per trace", "",
                  "| workload | cores | commit log | memory log | progress |",
                  "|---|---:|---:|---:|---:|"])
    for row in report["per_trace"]:
        relative = row["metrics"]["relative_percent"]
        lines.append(
            f"| {row['workload']} | {row['n_cores']} | "
            f"{relative['commit_log_mae']:+.2f}% | "
            f"{relative['memory_commit_log_mae']:+.2f}% | "
            f"{relative['progress_mae']:+.2f}% |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default="data/v30_gss_ready_dataset/manifest.json",
    )
    parser.add_argument(
        "--checkpoint",
        default="ckpt/tcsim_v30_gss_p1_frozen_adapter_5k_seed1234/best.pt",
    )
    parser.add_argument("--split", default="development_heldout")
    parser.add_argument("--samples-per-trace", type=int, default=128)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--device", default="cuda")
    parser.add_argument(
        "--out", default="logs/v30_gss_p1_5k_development_oracle.json",
    )
    args = parser.parse_args()
    if args.samples_per_trace <= 0:
        raise ValueError("samples-per-trace must be positive")
    sources = _manifest_sources(os.path.abspath(args.manifest), args.split)
    if not sources or any("gss_sidecar_dir" not in row for row in sources):
        raise RuntimeError("selected split is missing formal GSS sidecars")
    dataset = V29GlobalTimeDataset(
        sources, sequence_length=1, sequence_stride=1,
    )
    selected = _selected_indices(dataset, args.samples_per_trace)
    selected_counts = Counter(
        str(dataset.sample_trace_ids[index]) for index in selected
    )
    loader = DataLoader(
        dataset, batch_size=1, sampler=selected, shuffle=False,
        num_workers=int(args.num_workers), pin_memory=True,
        collate_fn=collate_v29_sequences,
    )
    payload = _torch_load(os.path.abspath(args.checkpoint))
    model = build_model(
        payload["config"]["model"], payload["config"]["chunk"]["horizons"],
    )
    model.load_state_dict(payload["model"], strict=True)
    if model.gss_adapter is None:
        raise RuntimeError("checkpoint has no GSS adapter")
    device = torch.device(args.device)
    model.to(device).eval()
    metadata = {
        str(row["trace_id"]): {
            "workload": str(row["workload"]),
            "n_cores": int(row["n_cores"]),
        }
        for row in sources
        if str(row["trace_id"]) in selected_counts
    }
    per_trace_raw = {trace_id: _empty_stats() for trace_id in metadata}
    delta_abs_sum = 0.0
    delta_count = 0
    amp_enabled = device.type == "cuda"
    horizons = model.horizons.to(device)
    with torch.inference_mode():
        for index, cpu_batch in enumerate(loader, 1):
            trace_id = str(cpu_batch["trace_id"][0])
            batch = _device_batch(cpu_batch, device)
            with torch.autocast(
                device_type=device.type, dtype=torch.bfloat16,
                enabled=amp_enabled,
            ):
                static = model.static_encoder(batch["per_uop_fields"])
                token, _core = model.interaction(static, batch)
                delta = model.gss_adapter(
                    token, batch["gss_uop_categorical"],
                    batch["gss_uop_continuous"], batch["gss_memory_mask"],
                )
                base_gap = F.softplus(
                    model.gap_head(token).squeeze(-1).float(),
                    beta=model.gap_softplus_beta,
                )
                gss_gap = F.softplus(
                    model.gap_head(token + delta).squeeze(-1).float(),
                    beta=model.gap_softplus_beta,
                )
            valid = batch["valid_uop_mask"].bool()
            base_time = _monotonic_prefix_sum(base_gap * valid)
            gss_time = _monotonic_prefix_sum(gss_gap * valid)
            base_progress = torch.sigmoid(
                (horizons[None, None, :] - base_time.unsqueeze(-1))
                / model.commit_temperature
            ).mul(valid.unsqueeze(-1)).sum(dim=1)
            gss_progress = torch.sigmoid(
                (horizons[None, None, :] - gss_time.unsqueeze(-1))
                / model.commit_temperature
            ).mul(valid.unsqueeze(-1)).sum(dim=1)
            _add_variant(
                per_trace_raw[trace_id]["base"], base_time, base_progress, batch,
            )
            _add_variant(
                per_trace_raw[trace_id]["gss"], gss_time, gss_progress, batch,
            )
            delta_abs_sum += float(delta.abs().sum())
            delta_count += int(delta.numel())
            if index % 256 == 0:
                print(f"[P1 oracle] {index}/{len(selected)}", flush=True)
    per_trace = []
    for trace_id in sorted(per_trace_raw):
        per_trace.append({
            "trace_id": trace_id,
            **metadata[trace_id],
            "samples": int(selected_counts[trace_id]),
            "metrics": _finalize(per_trace_raw[trace_id]),
        })
    token_weighted = _finalize(_merge_stats(per_trace_raw.values()))
    trace_equal: Dict[str, Any] = {"base": {}, "gss": {}, "relative_percent": {}}
    for variant in ("base", "gss"):
        for name in METRICS:
            trace_equal[variant][name] = sum(
                row["metrics"][variant][name] for row in per_trace
            ) / len(per_trace)
    for name in METRICS:
        trace_equal["relative_percent"][name] = 100.0 * (
            trace_equal["gss"][name]
            / max(1e-12, trace_equal["base"][name]) - 1.0
        )
    report = {
        "schema_version": "tcsim-v30-gss-p1-oracle-eval-1",
        "checkpoint": os.path.abspath(args.checkpoint),
        "checkpoint_step": int(payload["step"]),
        "split": args.split,
        "teacher_order": "ready_tick_then_core_then_uop_v1",
        "free_running_rollout": False,
        "samples_per_trace": int(args.samples_per_trace),
        "samples": len(selected),
        "traces": len(per_trace),
        "mean_adapter_delta_abs": delta_abs_sum / max(1, delta_count),
        "aggregate": {
            "token_weighted": token_weighted,
            "trace_equal": trace_equal,
        },
        "per_trace": per_trace,
    }
    out = os.path.abspath(args.out)
    parent = os.path.dirname(out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    dump_json(out, report)
    markdown = os.path.splitext(out)[0] + ".md"
    with open(markdown, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(json.dumps(report["aggregate"], indent=2), flush=True)
    print(f"[P1 oracle] json={out} markdown={markdown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
