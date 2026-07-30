#!/usr/bin/env python3
"""Heldout oracle-window comparison of v29, fixed G1 scales, and learned gate."""
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
from scripts.evaluate_v30_gss_residual_scale_sweep import (
    _empty_raw,
    _equal_average,
    _finalize,
    _merge,
    _relative,
)
from scripts.train_v29 import _manifest_sources
from tcsim.utils.io import dump_json
from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences
from tcsim.v29.model import _monotonic_prefix_sum, build_model


VARIANTS = ("base", "scale_0.25", "scale_1", "gate")


def _empty_gate_stats() -> Dict[str, float]:
    return {
        "sum": 0.0,
        "square_sum": 0.0,
        "count": 0.0,
        "memory_sum": 0.0,
        "memory_count": 0.0,
        "nonmemory_sum": 0.0,
        "nonmemory_count": 0.0,
        "below_0.5": 0.0,
        "above_0.9": 0.0,
        "active_sum": 0.0,
        "active_square_sum": 0.0,
        "active_count": 0.0,
        "minimum": 1.0,
        "maximum": 0.0,
    }


def _add_gate_stats(
    stats: Dict[str, float],
    gate: torch.Tensor,
    delta: torch.Tensor,
    batch: Mapping[str, torch.Tensor],
) -> None:
    valid = batch["valid_uop_mask"].bool()
    memory = batch["gss_memory_mask"].bool() & valid
    nonmemory = valid & ~memory
    selected = gate[valid].float()
    if not selected.numel():
        return
    stats["sum"] += float(selected.sum())
    stats["square_sum"] += float(selected.square().sum())
    stats["count"] += int(selected.numel())
    stats["memory_sum"] += float(gate[memory].float().sum())
    stats["memory_count"] += int(memory.sum())
    stats["nonmemory_sum"] += float(gate[nonmemory].float().sum())
    stats["nonmemory_count"] += int(nonmemory.sum())
    stats["below_0.5"] += int((selected < 0.5).sum())
    stats["above_0.9"] += int((selected > 0.9).sum())
    active = valid & (delta.float().square().mean(dim=-1) > 1.0e-12)
    active_gate = gate[active].float()
    stats["active_sum"] += float(active_gate.sum())
    stats["active_square_sum"] += float(active_gate.square().sum())
    stats["active_count"] += int(active_gate.numel())
    stats["minimum"] = min(stats["minimum"], float(selected.min()))
    stats["maximum"] = max(stats["maximum"], float(selected.max()))


def _merge_gate(items: Iterable[Mapping[str, float]]) -> Dict[str, float]:
    out = _empty_gate_stats()
    out["minimum"] = 1.0
    out["maximum"] = 0.0
    for item in items:
        for key in (
            "sum", "square_sum", "count", "memory_sum", "memory_count",
            "nonmemory_sum", "nonmemory_count", "below_0.5", "above_0.9",
            "active_sum", "active_square_sum", "active_count",
        ):
            out[key] += float(item[key])
        out["minimum"] = min(out["minimum"], float(item["minimum"]))
        out["maximum"] = max(out["maximum"], float(item["maximum"]))
    return out


def _finalize_gate(raw: Mapping[str, float]) -> Dict[str, float]:
    count = max(1.0, float(raw["count"]))
    mean = float(raw["sum"]) / count
    variance = max(0.0, float(raw["square_sum"]) / count - mean * mean)
    active_count = max(1.0, float(raw["active_count"]))
    active_mean = float(raw["active_sum"]) / active_count
    active_variance = max(
        0.0,
        float(raw["active_square_sum"]) / active_count
        - active_mean * active_mean,
    )
    return {
        "mean": mean,
        "std": variance ** 0.5,
        "minimum": float(raw["minimum"]),
        "maximum": float(raw["maximum"]),
        "memory_mean": float(raw["memory_sum"])
        / max(1.0, float(raw["memory_count"])),
        "nonmemory_mean": float(raw["nonmemory_sum"])
        / max(1.0, float(raw["nonmemory_count"])),
        "fraction_below_0.5": float(raw["below_0.5"]) / count,
        "fraction_above_0.9": float(raw["above_0.9"]) / count,
        "active_fraction": float(raw["active_count"]) / count,
        "active_mean": active_mean,
        "active_std": active_variance ** 0.5,
    }


def _group_summary(
    rows: Iterable[Mapping[str, Any]], field: str,
) -> Dict[str, Any]:
    grouped: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row[field])].append(row)
    out: Dict[str, Any] = {}
    for name, group in sorted(grouped.items()):
        metrics = {
            variant: _equal_average(
                row["metrics"][variant] for row in group
            ) for variant in VARIANTS
        }
        out[name] = {
            "traces": len(group),
            "metrics": metrics,
            "relative_percent": {
                variant: _relative(metrics[variant], metrics["base"])
                for variant in VARIANTS
            },
            "gate": _finalize_gate(_merge_gate(
                row["gate_raw"] for row in group
            )),
        }
    return out


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v30 GSS gate-only heldout evaluation", "",
        "> ready-clock teacher-order oracle-window evaluation; not rollout.",
        "", "## Aggregate", "",
        "| variant | weighting | commit log | memory log | nonmemory log | progress | wins/traces |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        for weighting in ("token_weighted", "trace_equal"):
            rel = report["aggregate"][weighting]["relative_percent"][variant]
            lines.append(
                f"| {variant} | {weighting} | {rel['commit_log_mae']:+.2f}% | "
                f"{rel['memory_commit_log_mae']:+.2f}% | "
                f"{rel['nonmemory_commit_log_mae']:+.2f}% | "
                f"{rel['progress_mae']:+.2f}% | "
                f"{report['aggregate']['trace_commit_wins'][variant]}/{report['traces']} |"
            )
    gate = report["gate"]
    lines.extend([
        "", "## Gate distribution", "",
        f"- mean/std: {gate['mean']:.4f} / {gate['std']:.4f}",
        f"- min/max: {gate['minimum']:.4f} / {gate['maximum']:.4f}",
        f"- memory/nonmemory mean: {gate['memory_mean']:.4f} / {gate['nonmemory_mean']:.4f}",
        f"- effective-residual mean/std: {gate['active_mean']:.4f} / {gate['active_std']:.4f} (active {gate['active_fraction']:.2%})",
        f"- fraction <0.5 / >0.9: {gate['fraction_below_0.5']:.2%} / {gate['fraction_above_0.9']:.2%}",
        "", "## Workload trace-equal commit-log relative", "",
        "| workload | alpha=0.25 | alpha=1 | gate | gate mean |",
        "|---|---:|---:|---:|---:|",
    ])
    for workload, row in report["by_workload"].items():
        rel = row["relative_percent"]
        lines.append(
            f"| {workload} | {rel['scale_0.25']['commit_log_mae']:+.2f}% | "
            f"{rel['scale_1']['commit_log_mae']:+.2f}% | "
            f"{rel['gate']['commit_log_mae']:+.2f}% | {row['gate']['mean']:.4f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default="data/v30_gss_ready_dataset/manifest.json",
    )
    parser.add_argument(
        "--checkpoint",
        default="ckpt/tcsim_v30_gss_gate_only_2k_seed1234/best.pt",
    )
    parser.add_argument("--split", default="development_heldout")
    parser.add_argument("--samples-per-trace", type=int, default=128)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--device", default="cuda")
    parser.add_argument(
        "--out", default="logs/v30_gss_gate_only_2k_best_development_oracle.json",
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
    if model.gss_adapter is None or model.gss_exposure_gate is None:
        raise RuntimeError("checkpoint lacks the GSS adapter or exposure gate")
    device = torch.device(args.device)
    model.to(device).eval()
    metadata = {
        str(row["trace_id"]): {
            "workload": str(row["workload"]),
            "n_cores": int(row["n_cores"]),
        }
        for row in sources if str(row["trace_id"]) in selected_counts
    }
    per_trace_raw = {
        trace_id: {variant: _empty_raw() for variant in VARIANTS}
        for trace_id in metadata
    }
    per_trace_gate = {
        trace_id: _empty_gate_stats() for trace_id in metadata
    }
    horizons = model.horizons.to(device)
    amp_enabled = device.type == "cuda"
    with torch.inference_mode():
        for index, cpu_batch in enumerate(loader, 1):
            trace_id = str(cpu_batch["trace_id"][0])
            batch = _device_batch(cpu_batch, device)
            valid = batch["valid_uop_mask"].bool()
            with torch.autocast(
                device_type=device.type, dtype=torch.bfloat16,
                enabled=amp_enabled,
            ):
                static = model.static_encoder(batch["per_uop_fields"])
                token, _core = model.interaction(static, batch)
                delta = model.gss_adapter(
                    token, batch["gss_uop_categorical"],
                    batch["gss_uop_continuous"], batch["gss_memory_mask"],
                ) * valid.unsqueeze(-1)
                gate = model.gss_exposure_gate(
                    token, delta, batch["gss_memory_mask"],
                )
                states = {
                    "base": token,
                    "scale_0.25": token + 0.25 * delta,
                    "scale_1": token + delta,
                    "gate": token + gate.unsqueeze(-1) * delta,
                }
                gaps = {
                    name: F.softplus(
                        model.gap_head(state).squeeze(-1).float(),
                        beta=model.gap_softplus_beta,
                    ) * valid
                    for name, state in states.items()
                }
            for variant in VARIANTS:
                commit = _monotonic_prefix_sum(gaps[variant])
                progress = torch.sigmoid(
                    (horizons[None, None, :] - commit.unsqueeze(-1))
                    / model.commit_temperature
                ).mul(valid.unsqueeze(-1)).sum(dim=1)
                _add_variant(
                    per_trace_raw[trace_id][variant], commit, progress, batch,
                )
            _add_gate_stats(per_trace_gate[trace_id], gate, delta, batch)
            if index % 256 == 0:
                print(f"[GSS gate oracle] {index}/{len(selected)}", flush=True)

    per_trace: list[Dict[str, Any]] = []
    for trace_id in sorted(per_trace_raw):
        metrics = {
            variant: _finalize(per_trace_raw[trace_id][variant])
            for variant in VARIANTS
        }
        per_trace.append({
            "trace_id": trace_id,
            **metadata[trace_id],
            "samples": int(selected_counts[trace_id]),
            "metrics": metrics,
            "relative_percent": {
                variant: _relative(metrics[variant], metrics["base"])
                for variant in VARIANTS
            },
            "gate": _finalize_gate(per_trace_gate[trace_id]),
            "gate_raw": per_trace_gate[trace_id],
        })
    token_metrics = {
        variant: _finalize(_merge(
            row[variant] for row in per_trace_raw.values()
        )) for variant in VARIANTS
    }
    trace_metrics = {
        variant: _equal_average(
            row["metrics"][variant] for row in per_trace
        ) for variant in VARIANTS
    }
    report = {
        "schema_version": "tcsim-v30-gss-gate-oracle-eval-1",
        "checkpoint": os.path.abspath(args.checkpoint),
        "checkpoint_step": int(payload["step"]),
        "split": args.split,
        "teacher_order": "ready_tick_then_core_then_uop_v1",
        "free_running_rollout": False,
        "samples_per_trace": int(args.samples_per_trace),
        "samples": len(selected),
        "traces": len(per_trace),
        "gate": _finalize_gate(_merge_gate(per_trace_gate.values())),
        "aggregate": {
            "token_weighted": {
                "metrics": token_metrics,
                "relative_percent": {
                    variant: _relative(token_metrics[variant], token_metrics["base"])
                    for variant in VARIANTS
                },
            },
            "trace_equal": {
                "metrics": trace_metrics,
                "relative_percent": {
                    variant: _relative(trace_metrics[variant], trace_metrics["base"])
                    for variant in VARIANTS
                },
            },
            "trace_commit_wins": {
                variant: sum(
                    row["metrics"][variant]["commit_log_mae"]
                    < row["metrics"]["base"]["commit_log_mae"]
                    for row in per_trace
                ) for variant in VARIANTS
            },
        },
        "by_workload": _group_summary(per_trace, "workload"),
        "by_core": _group_summary(per_trace, "n_cores"),
        "per_trace": per_trace,
    }
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dump_json(out, report)
    markdown = os.path.splitext(out)[0] + ".md"
    with open(markdown, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(json.dumps({
        "checkpoint_step": report["checkpoint_step"],
        "aggregate": report["aggregate"],
        "gate": report["gate"],
    }, indent=2), flush=True)
    print(f"[GSS gate oracle] json={out} markdown={markdown}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
