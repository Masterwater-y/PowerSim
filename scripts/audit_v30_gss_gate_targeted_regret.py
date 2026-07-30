#!/usr/bin/env python3
"""Localize learned-GSS-gate regret on BVC, PyTorch, and Redis heldout."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Mapping

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader

from scripts.evaluate_v30_gss_p1_oracle import (
    _device_batch,
    _selected_indices,
    _torch_load,
)
from scripts.train_v29 import _manifest_sources
from tcsim.utils.io import dump_json
from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences
from tcsim.v29.model import _monotonic_prefix_sum, build_model


TARGET_WORKLOADS = (
    "W_v28_bvc_encoder_heldout",
    "W_v28_pytorch_heldout",
    "W_v28_redis_heldout",
)
SHORT_WORKLOAD = {
    "W_v28_bvc_encoder_heldout": "BVC",
    "W_v28_pytorch_heldout": "PyTorch",
    "W_v28_redis_heldout": "Redis",
}
ERROR_NAMES = ("base", "scale_0.25", "scale_1", "gate")
GATE_MINIMUM = 0.25
GATE_MIDPOINT = 0.625
GATE_HISTOGRAM_BINS = 60
GATE_CALIBRATION_EDGES = np.asarray(
    [0.25, 0.40, 0.55, 0.70, 0.85, 0.95, 1.000001],
    dtype=np.float64,
)


def _empty_stats() -> Dict[str, Any]:
    return {
        "tokens": 0,
        "error_sum": {name: 0.0 for name in ERROR_NAMES},
        "gap_error_sum": {name: 0.0 for name in ERROR_NAMES},
        "active_tokens": 0,
        "gate_sum": 0.0,
        "gate_square_sum": 0.0,
        "gate_minimum": 1.0,
        "gate_maximum": 0.0,
        "gate_vs_fixed_oracle_sum": 0.0,
        "gate_vs_fixed_oracle_positive_sum": 0.0,
        "gate_vs_fixed_oracle_positive_count": 0,
        "gate_vs_base_sum": 0.0,
        "gate_vs_base_positive_sum": 0.0,
        "gate_vs_base_positive_count": 0,
        "decisive_tokens": 0,
        "prefer_full_count": 0,
        "gate_high_count": 0,
        "gate_choice_correct_count": 0,
        "missed_full_count": 0,
        "overstrong_count": 0,
        "gap_decisive_tokens": 0,
        "gap_prefer_full_count": 0,
        "gap_gate_choice_correct_count": 0,
        "gap_positive_histogram": np.zeros(GATE_HISTOGRAM_BINS, dtype=np.int64),
        "gap_negative_histogram": np.zeros(GATE_HISTOGRAM_BINS, dtype=np.int64),
        "positive_histogram": np.zeros(GATE_HISTOGRAM_BINS, dtype=np.int64),
        "negative_histogram": np.zeros(GATE_HISTOGRAM_BINS, dtype=np.int64),
        "calibration_count": np.zeros(
            len(GATE_CALIBRATION_EDGES) - 1, dtype=np.int64,
        ),
        "calibration_prefer_full": np.zeros(
            len(GATE_CALIBRATION_EDGES) - 1, dtype=np.int64,
        ),
    }


def _histogram_auc(positive: np.ndarray, negative: np.ndarray) -> float | None:
    positives = int(positive.sum())
    negatives = int(negative.sum())
    if not positives or not negatives:
        return None
    cumulative_negative = 0.0
    concordant = 0.0
    for positive_count, negative_count in zip(positive, negative):
        concordant += float(positive_count) * (
            cumulative_negative + 0.5 * float(negative_count)
        )
        cumulative_negative += float(negative_count)
    return concordant / float(positives * negatives)


def _update_stats(
    stats: Dict[str, Any],
    values: Mapping[str, np.ndarray],
    mask: np.ndarray,
) -> None:
    count = int(mask.sum())
    if not count:
        return
    stats["tokens"] += count
    for name in ERROR_NAMES:
        stats["error_sum"][name] += float(values[f"error_{name}"][mask].sum())
        stats["gap_error_sum"][name] += float(
            values[f"gap_error_{name}"][mask].sum()
        )

    active = mask & values["active"]
    active_count = int(active.sum())
    if active_count:
        gate = values["gate"][active]
        stats["active_tokens"] += active_count
        stats["gate_sum"] += float(gate.sum())
        stats["gate_square_sum"] += float(np.square(gate).sum())
        stats["gate_minimum"] = min(stats["gate_minimum"], float(gate.min()))
        stats["gate_maximum"] = max(stats["gate_maximum"], float(gate.max()))

        fixed_oracle = np.minimum(
            values["error_scale_0.25"][active],
            values["error_scale_1"][active],
        )
        fixed_regret = values["error_gate"][active] - fixed_oracle
        stats["gate_vs_fixed_oracle_sum"] += float(fixed_regret.sum())
        positive_fixed = np.maximum(fixed_regret, 0.0)
        stats["gate_vs_fixed_oracle_positive_sum"] += float(
            positive_fixed.sum()
        )
        stats["gate_vs_fixed_oracle_positive_count"] += int(
            (fixed_regret > 0.0).sum()
        )

        base_regret = (
            values["error_gate"][active] - values["error_base"][active]
        )
        stats["gate_vs_base_sum"] += float(base_regret.sum())
        stats["gate_vs_base_positive_sum"] += float(
            np.maximum(base_regret, 0.0).sum()
        )
        stats["gate_vs_base_positive_count"] += int((base_regret > 0.0).sum())

    gap_decisive = mask & values["gap_decisive"]
    gap_decisive_count = int(gap_decisive.sum())
    if gap_decisive_count:
        gap_prefer_full = values["gap_prefer_full"][gap_decisive]
        gap_gate = values["gate"][gap_decisive]
        gap_gate_high = gap_gate >= GATE_MIDPOINT
        stats["gap_decisive_tokens"] += gap_decisive_count
        stats["gap_prefer_full_count"] += int(gap_prefer_full.sum())
        stats["gap_gate_choice_correct_count"] += int(
            (gap_gate_high == gap_prefer_full).sum()
        )
        gap_histogram_index = np.floor(
            (gap_gate - GATE_MINIMUM) / (1.0 - GATE_MINIMUM)
            * GATE_HISTOGRAM_BINS
        ).astype(np.int64)
        gap_histogram_index = np.clip(
            gap_histogram_index, 0, GATE_HISTOGRAM_BINS - 1,
        )
        stats["gap_positive_histogram"] += np.bincount(
            gap_histogram_index[gap_prefer_full],
            minlength=GATE_HISTOGRAM_BINS,
        )
        stats["gap_negative_histogram"] += np.bincount(
            gap_histogram_index[~gap_prefer_full],
            minlength=GATE_HISTOGRAM_BINS,
        )

    decisive = mask & values["decisive"]
    decisive_count = int(decisive.sum())
    if not decisive_count:
        return
    prefer_full = values["prefer_full"][decisive]
    gate = values["gate"][decisive]
    gate_high = gate >= GATE_MIDPOINT
    stats["decisive_tokens"] += decisive_count
    stats["prefer_full_count"] += int(prefer_full.sum())
    stats["gate_high_count"] += int(gate_high.sum())
    stats["gate_choice_correct_count"] += int((gate_high == prefer_full).sum())
    stats["missed_full_count"] += int((prefer_full & ~gate_high).sum())
    stats["overstrong_count"] += int((~prefer_full & gate_high).sum())

    histogram_index = np.floor(
        (gate - GATE_MINIMUM) / (1.0 - GATE_MINIMUM)
        * GATE_HISTOGRAM_BINS
    ).astype(np.int64)
    histogram_index = np.clip(
        histogram_index, 0, GATE_HISTOGRAM_BINS - 1,
    )
    stats["positive_histogram"] += np.bincount(
        histogram_index[prefer_full], minlength=GATE_HISTOGRAM_BINS,
    )
    stats["negative_histogram"] += np.bincount(
        histogram_index[~prefer_full], minlength=GATE_HISTOGRAM_BINS,
    )
    calibration_index = np.searchsorted(
        GATE_CALIBRATION_EDGES, gate, side="right",
    ) - 1
    calibration_index = np.clip(
        calibration_index, 0, len(GATE_CALIBRATION_EDGES) - 2,
    )
    stats["calibration_count"] += np.bincount(
        calibration_index, minlength=len(GATE_CALIBRATION_EDGES) - 1,
    )
    stats["calibration_prefer_full"] += np.bincount(
        calibration_index[prefer_full],
        minlength=len(GATE_CALIBRATION_EDGES) - 1,
    )

def _finalize_stats(stats: Mapping[str, Any]) -> Dict[str, Any]:
    tokens = max(1, int(stats["tokens"]))
    active_tokens = max(1, int(stats["active_tokens"]))
    decisive_tokens = max(1, int(stats["decisive_tokens"]))
    errors = {
        name: float(stats["error_sum"][name]) / tokens
        for name in ERROR_NAMES
    }
    gap_errors = {
        name: float(stats["gap_error_sum"][name]) / tokens
        for name in ERROR_NAMES
    }
    base = max(1.0e-12, errors["base"])
    gate_mean = float(stats["gate_sum"]) / active_tokens
    gap_decisive_tokens = max(1, int(stats["gap_decisive_tokens"]))
    gate_variance = max(
        0.0,
        float(stats["gate_square_sum"]) / active_tokens - gate_mean ** 2,
    )
    calibration = []
    for index, count in enumerate(stats["calibration_count"]):
        calibration.append({
            "gate_range": [
                float(GATE_CALIBRATION_EDGES[index]),
                float(GATE_CALIBRATION_EDGES[index + 1]),
            ],
            "tokens": int(count),
            "oracle_prefer_full_rate": (
                float(stats["calibration_prefer_full"][index]) / int(count)
                if int(count) else None
            ),
        })
    return {
        "tokens": int(stats["tokens"]),
        "active_tokens": int(stats["active_tokens"]),
        "active_fraction": float(stats["active_tokens"]) / tokens,
        "commit_log_mae": errors,
        "commit_relative_percent": {
            name: 100.0 * (value / base - 1.0)
            for name, value in errors.items()
        },
        "gap_log_mae": gap_errors,
        "gap_relative_percent": {
            name: 100.0 * (
                value / max(1.0e-12, gap_errors["base"]) - 1.0
            ) for name, value in gap_errors.items()
        },
        "gate": {
            "mean": gate_mean,
            "std": gate_variance ** 0.5,
            "minimum": float(stats["gate_minimum"]),
            "maximum": float(stats["gate_maximum"]),
        },
        "active_regret": {
            "gate_minus_best_fixed_mean": float(
                stats["gate_vs_fixed_oracle_sum"]
            ) / active_tokens,
            "positive_gate_minus_best_fixed_mean": float(
                stats["gate_vs_fixed_oracle_positive_sum"]
            ) / active_tokens,
            "fraction_worse_than_best_fixed": float(
                stats["gate_vs_fixed_oracle_positive_count"]
            ) / active_tokens,
            "gate_minus_base_mean": float(stats["gate_vs_base_sum"])
            / active_tokens,
            "positive_gate_minus_base_mean": float(
                stats["gate_vs_base_positive_sum"]
            ) / active_tokens,
            "fraction_worse_than_base": float(
                stats["gate_vs_base_positive_count"]
            ) / active_tokens,
        },
        "decisive": {
            "tokens": int(stats["decisive_tokens"]),
            "fraction_of_active": float(stats["decisive_tokens"])
            / active_tokens,
            "oracle_prefer_full_rate": float(stats["prefer_full_count"])
            / decisive_tokens,
            "gate_high_rate": float(stats["gate_high_count"])
            / decisive_tokens,
            "threshold_accuracy": float(stats["gate_choice_correct_count"])
            / decisive_tokens,
            "missed_full_rate": float(stats["missed_full_count"])
            / decisive_tokens,
            "overstrong_rate": float(stats["overstrong_count"])
            / decisive_tokens,
            "gate_score_auc": _histogram_auc(
                stats["positive_histogram"], stats["negative_histogram"],
            ),
            "calibration": calibration,
        },
        "local_gap_decisive": {
            "tokens": int(stats["gap_decisive_tokens"]),
            "fraction_of_active": float(stats["gap_decisive_tokens"])
            / active_tokens,
            "oracle_prefer_full_rate": float(stats["gap_prefer_full_count"])
            / gap_decisive_tokens,
            "threshold_accuracy": float(
                stats["gap_gate_choice_correct_count"]
            ) / gap_decisive_tokens,
            "gate_score_auc": _histogram_auc(
                stats["gap_positive_histogram"],
                stats["gap_negative_histogram"],
            ),
        },
    }


def _phase_name(position: np.ndarray) -> np.ndarray:
    return np.where(
        position < 64, "P0[0,64)",
        np.where(
            position < 128, "P1[64,128)",
            np.where(position < 192, "P2[128,192)", "P3[192,256)"),
        ),
    )


def _distance_name(distance: np.ndarray) -> np.ndarray:
    return np.where(
        distance < 0, "before_first_memory",
        np.where(
            distance == 0, "current_memory",
            np.where(
                distance <= 3, "after_memory_1_3",
                np.where(distance <= 15, "after_memory_4_15", "after_memory_16_plus"),
            ),
        ),
    )


def _density_name(density: np.ndarray) -> np.ndarray:
    return np.where(
        density == 0.0, "density_0",
        np.where(
            density <= 0.05, "density_0_05",
            np.where(
                density <= 0.15, "density_05_15",
                np.where(density <= 0.30, "density_15_30", "density_30_plus"),
            ),
        ),
    )


def _effect_name(effect: np.ndarray) -> np.ndarray:
    return np.where(
        effect < 0.005, "effect_lt_0.005",
        np.where(
            effect < 0.02, "effect_0.005_0.02",
            np.where(effect < 0.05, "effect_0.02_0.05", "effect_ge_0.05"),
        ),
    )


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v30 GSS Gate targeted regret localization", "",
        "> Only BVC, PyTorch, and Redis development-heldout are included.",
        "> This is ready-clock teacher-order oracle-window diagnosis, not rollout.",
        "", "## Workload summary", "",
        "| workload | alpha=.25 | alpha=1 | gate | gate mean | prefix AUC | local-gap AUC | local-gap error | worse-base |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for workload in ("BVC", "PyTorch", "Redis"):
        row = report["by_workload"][workload]
        rel = row["commit_relative_percent"]
        decisive = row["decisive"]
        lines.append(
            f"| {workload} | {rel['scale_0.25']:+.2f}% | "
            f"{rel['scale_1']:+.2f}% | {rel['gate']:+.2f}% | "
            f"{row['gate']['mean']:.3f} | "
            f"{decisive['gate_score_auc']:.3f} | "
            f"{row['local_gap_decisive']['gate_score_auc']:.3f} | "
            f"{row['gap_relative_percent']['gate']:+.2f}% | "
            f"{row['active_regret']['fraction_worse_than_base']:.1%} |"
        )
    lines.extend([
        "", "## Workload/core", "",
        "| workload | cores | gate relative | gate mean | oracle-full | gate-high | accuracy | AUC |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for key, row in report["by_workload_core"].items():
        workload, cores = key.split("/C")
        decisive = row["decisive"]
        auc = decisive["gate_score_auc"]
        lines.append(
            f"| {workload} | {cores} | "
            f"{row['commit_relative_percent']['gate']:+.2f}% | "
            f"{row['gate']['mean']:.3f} | "
            f"{decisive['oracle_prefer_full_rate']:.1%} | "
            f"{decisive['gate_high_rate']:.1%} | "
            f"{decisive['threshold_accuracy']:.1%} | "
            f"{auc:.3f} |"
        )
    for section, title in (
        ("by_workload_phase", "Prefix phase"),
        ("by_workload_distance", "Distance from latest memory event"),
        ("by_workload_density", "Prefix memory density"),
        ("by_workload_effect", "Fixed-scale timing-effect magnitude"),
    ):
        lines.extend([
            "", f"## {title}", "",
            "| group | gate relative | gate mean | oracle-full | gate-high | accuracy | worse-base |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ])
        for key, row in report[section].items():
            decisive = row["decisive"]
            lines.append(
                f"| {key} | {row['commit_relative_percent']['gate']:+.2f}% | "
                f"{row['gate']['mean']:.3f} | "
                f"{decisive['oracle_prefer_full_rate']:.1%} | "
                f"{decisive['gate_high_rate']:.1%} | "
                f"{decisive['threshold_accuracy']:.1%} | "
                f"{row['active_regret']['fraction_worse_than_base']:.1%} |"
            )
    lines.extend(["", "## Gate calibration", ""])
    for workload in ("BVC", "PyTorch", "Redis"):
        lines.extend([
            f"### {workload}", "",
            "| gate range | decisive tokens | oracle prefers alpha=1 |",
            "|---|---:|---:|",
        ])
        for row in report["by_workload"][workload]["decisive"]["calibration"]:
            if not row["tokens"]:
                continue
            low, high = row["gate_range"]
            lines.append(
                f"| [{low:.2f},{high:.2f}) | {row['tokens']:,} | "
                f"{row['oracle_prefer_full_rate']:.1%} |"
            )
        lines.append("")
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
    parser.add_argument("--samples-per-trace", type=int, default=128)
    parser.add_argument("--decisive-margin", type=float, default=0.01)
    parser.add_argument("--num-workers", type=int, default=4)
    parser.add_argument("--device", default="cuda")
    parser.add_argument(
        "--out",
        default="logs/v30_gss_gate_targeted_bvc_pytorch_redis_regret.json",
    )
    args = parser.parse_args()
    if args.samples_per_trace <= 0 or args.decisive_margin < 0.0:
        raise ValueError("invalid targeted-audit sample count or margin")
    all_sources = _manifest_sources(
        os.path.abspath(args.manifest), "development_heldout",
    )
    sources = [
        row for row in all_sources if str(row["workload"]) in TARGET_WORKLOADS
    ]
    if len(sources) != 12:
        raise RuntimeError(f"expected 12 targeted traces, got {len(sources)}")
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
    metadata = {
        str(row["trace_id"]): {
            "workload": str(row["workload"]),
            "short_workload": SHORT_WORKLOAD[str(row["workload"])],
            "n_cores": int(row["n_cores"]),
        } for row in sources
    }
    payload = _torch_load(os.path.abspath(args.checkpoint))
    model = build_model(
        payload["config"]["model"], payload["config"]["chunk"]["horizons"],
    )
    model.load_state_dict(payload["model"], strict=True)
    if model.gss_adapter is None or model.gss_exposure_gate is None:
        raise RuntimeError("checkpoint lacks GSS adapter/exposure gate")
    device = torch.device(args.device)
    model.to(device).eval()
    groups: Dict[str, Dict[str, Dict[str, Any]]] = {
        "by_workload": defaultdict(_empty_stats),
        "by_workload_core": defaultdict(_empty_stats),
        "by_workload_phase": defaultdict(_empty_stats),
        "by_workload_distance": defaultdict(_empty_stats),
        "by_workload_density": defaultdict(_empty_stats),
        "by_workload_effect": defaultdict(_empty_stats),
        "by_trace": defaultdict(_empty_stats),
    }
    overall = _empty_stats()
    amp_enabled = device.type == "cuda"
    with torch.inference_mode():
        for batch_index, cpu_batch in enumerate(loader, 1):
            trace_id = str(cpu_batch["trace_id"][0])
            trace_meta = metadata[trace_id]
            workload = str(trace_meta["short_workload"])
            cores = int(trace_meta["n_cores"])
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
            commit = {
                name: _monotonic_prefix_sum(gap)
                for name, gap in gaps.items()
            }
            true_time = batch["commit_time_target"].clamp(min=0.0).float()
            true_gap = torch.cat([
                true_time[:, :1],
                (true_time[:, 1:] - true_time[:, :-1]).clamp(min=0.0),
            ], dim=1)
            memory = batch["gss_memory_mask"].bool()
            rows, length = memory.shape
            position = torch.arange(
                length, device=device, dtype=torch.long,
            ).view(1, -1).expand(rows, -1)
            event_position = torch.where(
                memory, position, torch.full_like(position, -1),
            )
            last_memory = torch.cummax(event_position, dim=1).values
            distance = torch.where(
                last_memory >= 0, position - last_memory,
                torch.full_like(position, -1),
            )
            density = memory.cumsum(dim=1).float() / (position.float() + 1.0)
            log_true = torch.log1p(true_time)
            error = {
                name: (torch.log1p(value.clamp(min=0.0)) - log_true).abs()
                for name, value in commit.items()
            }
            gap_error = {
                name: (
                    torch.log1p(value.clamp(min=0.0))
                    - torch.log1p(true_gap)
                ).abs()
                for name, value in gaps.items()
            }
            scale_effect = (
                torch.log1p(commit["scale_1"])
                - torch.log1p(commit["scale_0.25"])
            ).abs()
            delta_rms = delta.float().square().mean(dim=-1).sqrt()
            selected_valid = valid.detach().cpu().numpy().astype(bool)

            def flat(tensor: torch.Tensor) -> np.ndarray:
                return tensor.detach().float().cpu().numpy()[selected_valid]

            values: Dict[str, np.ndarray] = {
                "gate": flat(gate),
                "active": flat(delta_rms) > 1.0e-6,
                "position": position.detach().cpu().numpy()[selected_valid],
                "memory": memory.detach().cpu().numpy()[selected_valid],
                "distance": distance.detach().cpu().numpy()[selected_valid],
                "density": flat(density),
                "scale_effect": flat(scale_effect),
            }
            for name in ERROR_NAMES:
                values[f"error_{name}"] = flat(error[name])
                values[f"gap_error_{name}"] = flat(gap_error[name])
            values["prefer_full"] = (
                values["error_scale_1"] < values["error_scale_0.25"]
            )
            values["decisive"] = (
                values["active"]
                & (
                    np.abs(
                        values["error_scale_1"]
                        - values["error_scale_0.25"]
                    ) >= float(args.decisive_margin)
                )
            )
            values["gap_prefer_full"] = (
                values["gap_error_scale_1"]
                < values["gap_error_scale_0.25"]
            )
            values["gap_decisive"] = (
                values["active"]
                & (
                    np.abs(
                        values["gap_error_scale_1"]
                        - values["gap_error_scale_0.25"]
                    ) >= float(args.decisive_margin)
                )
            )
            all_mask = np.ones(len(values["gate"]), dtype=bool)
            _update_stats(overall, values, all_mask)
            _update_stats(groups["by_workload"][workload], values, all_mask)
            _update_stats(
                groups["by_workload_core"][f"{workload}/C{cores}"],
                values, all_mask,
            )
            _update_stats(groups["by_trace"][trace_id], values, all_mask)
            categories = {
                "by_workload_phase": _phase_name(values["position"]),
                "by_workload_distance": _distance_name(values["distance"]),
                "by_workload_density": _density_name(values["density"]),
                "by_workload_effect": _effect_name(values["scale_effect"]),
            }
            for section, category in categories.items():
                for name in np.unique(category):
                    _update_stats(
                        groups[section][f"{workload}/{name}"],
                        values, category == name,
                    )
            if batch_index % 128 == 0:
                print(
                    f"[targeted gate regret] {batch_index}/{len(selected)}",
                    flush=True,
                )

    report: Dict[str, Any] = {
        "schema_version": "tcsim-v30-gss-gate-targeted-regret-1",
        "checkpoint": os.path.abspath(args.checkpoint),
        "checkpoint_step": int(payload["step"]),
        "split": "development_heldout",
        "workloads": list(TARGET_WORKLOADS),
        "traces": len(sources),
        "samples": len(selected),
        "samples_per_trace": int(args.samples_per_trace),
        "decisive_margin_log_error": float(args.decisive_margin),
        "teacher_order": "ready_tick_then_core_then_uop_v1",
        "free_running_rollout": False,
        "overall": _finalize_stats(overall),
    }
    for section, rows in groups.items():
        report[section] = {
            key: _finalize_stats(value) for key, value in sorted(rows.items())
        }
    report["trace_metadata"] = {
        trace_id: {
            **value,
            "samples": int(selected_counts[trace_id]),
        } for trace_id, value in sorted(metadata.items())
    }
    out = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    dump_json(out, report)
    markdown = os.path.splitext(out)[0] + ".md"
    with open(markdown, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(json.dumps({
        "overall": report["overall"],
        "by_workload": report["by_workload"],
    }, indent=2), flush=True)
    print(
        f"[targeted gate regret] json={out} markdown={markdown}", flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
