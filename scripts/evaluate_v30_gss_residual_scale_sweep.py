#!/usr/bin/env python3
"""One-pass oracle-window sweep of multiplicative GSS residual scales."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from scripts.evaluate_v30_gss_p1_oracle import (
    METRICS,
    _add_variant,
    _device_batch,
    _selected_indices,
    _torch_load,
)
from scripts.train_v29 import _manifest_sources
from tcsim.utils.io import dump_json
from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences
from tcsim.v29.model import _monotonic_prefix_sum, build_model


def _parse_scales(value: str) -> tuple[float, ...]:
    scales = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    if not scales or any(not 0.0 <= scale <= 2.0 for scale in scales):
        raise ValueError("scales must be a non-empty comma list in [0,2]")
    if 0.0 not in scales or 1.0 not in scales:
        raise ValueError("scale sweep must contain both 0 and 1")
    if len(set(scales)) != len(scales):
        raise ValueError("scale sweep contains duplicates")
    return tuple(sorted(scales))


def _scale_key(scale: float) -> str:
    return f"{scale:g}"


def _empty_raw() -> Dict[str, float]:
    return {
        **{f"{name}_sum": 0.0 for name in METRICS},
        "commit_count": 0.0,
        "memory_count": 0.0,
        "nonmemory_count": 0.0,
        "progress_count": 0.0,
    }


def _finalize(raw: Mapping[str, float]) -> Dict[str, float]:
    return {
        "commit_log_mae": raw["commit_log_mae_sum"]
        / max(1.0, raw["commit_count"]),
        "memory_commit_log_mae": raw["memory_commit_log_mae_sum"]
        / max(1.0, raw["memory_count"]),
        "nonmemory_commit_log_mae": raw["nonmemory_commit_log_mae_sum"]
        / max(1.0, raw["nonmemory_count"]),
        "progress_mae": raw["progress_mae_sum"]
        / max(1.0, raw["progress_count"]),
    }


def _merge(items: Iterable[Mapping[str, float]]) -> Dict[str, float]:
    merged = _empty_raw()
    for item in items:
        for key, value in item.items():
            merged[key] += float(value)
    return merged


def _relative(metrics: Mapping[str, float], base: Mapping[str, float]):
    return {
        name: 100.0 * (metrics[name] / max(1.0e-12, base[name]) - 1.0)
        for name in METRICS
    }


def _equal_average(rows: Iterable[Mapping[str, float]]) -> Dict[str, float]:
    values = list(rows)
    if not values:
        raise ValueError("cannot average an empty metric group")
    return {
        name: sum(row[name] for row in values) / len(values)
        for name in METRICS
    }


def _group_summary(
    per_trace: list[Mapping[str, Any]],
    scales: tuple[float, ...],
    field: str,
) -> Dict[str, Any]:
    grouped: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in per_trace:
        grouped[str(row[field])].append(row)
    out: Dict[str, Any] = {}
    for group, rows in sorted(grouped.items()):
        metrics = {
            _scale_key(scale): _equal_average(
                row["metrics"][_scale_key(scale)] for row in rows
            )
            for scale in scales
        }
        base = metrics[_scale_key(0.0)]
        out[group] = {
            "traces": len(rows),
            "metrics": metrics,
            "relative_percent": {
                key: _relative(value, base) for key, value in metrics.items()
            },
        }
    return out


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v30 G1 residual-scale oracle sweep", "",
        "> ready-clock teacher-order oracle-window evaluation; not rollout.",
        "", "## Aggregate", "",
        "| scale | weighting | commit log | memory log | nonmemory log | progress | wins/traces |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for scale in report["scales"]:
        key = _scale_key(float(scale))
        for weighting in ("token_weighted", "trace_equal"):
            relative = report["aggregate"][weighting]["relative_percent"][key]
            wins = report["aggregate"]["trace_commit_wins"][key]
            lines.append(
                f"| {key} | {weighting} | {relative['commit_log_mae']:+.2f}% | "
                f"{relative['memory_commit_log_mae']:+.2f}% | "
                f"{relative['nonmemory_commit_log_mae']:+.2f}% | "
                f"{relative['progress_mae']:+.2f}% | {wins}/{report['traces']} |"
            )
    lines.extend([
        "", "## Workload trace-equal commit-log relative", "",
        "| workload | " + " | ".join(_scale_key(float(s)) for s in report["scales"]) + " |",
        "|---|" + "---:|" * len(report["scales"]),
    ])
    for workload, row in report["by_workload"].items():
        values = [
            row["relative_percent"][_scale_key(float(scale))]["commit_log_mae"]
            for scale in report["scales"]
        ]
        lines.append(
            f"| {workload} | " + " | ".join(f"{value:+.2f}%" for value in values) + " |"
        )
    lines.extend([
        "", "## Core-count trace-equal commit-log relative", "",
        "| cores | " + " | ".join(_scale_key(float(s)) for s in report["scales"]) + " |",
        "|---:|" + "---:|" * len(report["scales"]),
    ])
    for cores, row in report["by_core"].items():
        values = [
            row["relative_percent"][_scale_key(float(scale))]["commit_log_mae"]
            for scale in report["scales"]
        ]
        lines.append(
            f"| {cores} | " + " | ".join(f"{value:+.2f}%" for value in values) + " |"
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
    parser.add_argument("--scales", default="0,0.25,0.5,0.75,1")
    parser.add_argument(
        "--out", default="logs/v30_gss_p1_g1_scale_sweep_development_oracle.json",
    )
    args = parser.parse_args()
    scales = _parse_scales(args.scales)
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
    per_trace_raw = {
        trace_id: {_scale_key(scale): _empty_raw() for scale in scales}
        for trace_id in metadata
    }
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
                gaps = {
                    _scale_key(scale): F.softplus(
                        model.gap_head(token + scale * delta).squeeze(-1).float(),
                        beta=model.gap_softplus_beta,
                    )
                    for scale in scales
                }
            valid = batch["valid_uop_mask"].bool()
            for scale in scales:
                key = _scale_key(scale)
                commit_time = _monotonic_prefix_sum(gaps[key] * valid)
                progress = torch.sigmoid(
                    (horizons[None, None, :] - commit_time.unsqueeze(-1))
                    / model.commit_temperature
                ).mul(valid.unsqueeze(-1)).sum(dim=1)
                _add_variant(
                    per_trace_raw[trace_id][key], commit_time, progress, batch,
                )
            delta_abs_sum += float(delta.abs().sum())
            delta_count += int(delta.numel())
            if index % 256 == 0:
                print(f"[G1 scale sweep] {index}/{len(selected)}", flush=True)

    per_trace: list[Dict[str, Any]] = []
    for trace_id in sorted(per_trace_raw):
        metrics = {
            key: _finalize(raw) for key, raw in per_trace_raw[trace_id].items()
        }
        base = metrics[_scale_key(0.0)]
        per_trace.append({
            "trace_id": trace_id,
            **metadata[trace_id],
            "samples": int(selected_counts[trace_id]),
            "metrics": metrics,
            "relative_percent": {
                key: _relative(value, base) for key, value in metrics.items()
            },
        })
    token_metrics = {
        _scale_key(scale): _finalize(_merge(
            row[_scale_key(scale)] for row in per_trace_raw.values()
        ))
        for scale in scales
    }
    trace_metrics = {
        _scale_key(scale): _equal_average(
            row["metrics"][_scale_key(scale)] for row in per_trace
        )
        for scale in scales
    }
    base_key = _scale_key(0.0)
    trace_wins = {
        _scale_key(scale): sum(
            row["metrics"][_scale_key(scale)]["commit_log_mae"]
            < row["metrics"][base_key]["commit_log_mae"]
            for row in per_trace
        )
        for scale in scales
    }
    report = {
        "schema_version": "tcsim-v30-gss-residual-scale-sweep-1",
        "checkpoint": os.path.abspath(args.checkpoint),
        "checkpoint_step": int(payload["step"]),
        "split": args.split,
        "teacher_order": "ready_tick_then_core_then_uop_v1",
        "free_running_rollout": False,
        "scales": list(scales),
        "samples_per_trace": int(args.samples_per_trace),
        "samples": len(selected),
        "traces": len(per_trace),
        "mean_adapter_delta_abs": delta_abs_sum / max(1, delta_count),
        "aggregate": {
            "token_weighted": {
                "metrics": token_metrics,
                "relative_percent": {
                    key: _relative(value, token_metrics[base_key])
                    for key, value in token_metrics.items()
                },
            },
            "trace_equal": {
                "metrics": trace_metrics,
                "relative_percent": {
                    key: _relative(value, trace_metrics[base_key])
                    for key, value in trace_metrics.items()
                },
            },
            "trace_commit_wins": trace_wins,
        },
        "by_workload": _group_summary(per_trace, scales, "workload"),
        "by_core": _group_summary(per_trace, scales, "n_cores"),
        "per_trace": per_trace,
    }
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dump_json(out, report)
    markdown = os.path.splitext(out)[0] + ".md"
    with open(markdown, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(json.dumps(report["aggregate"], indent=2), flush=True)
    print(f"[G1 scale sweep] json={out} markdown={markdown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
