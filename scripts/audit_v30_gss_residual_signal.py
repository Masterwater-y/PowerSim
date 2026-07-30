#!/usr/bin/env python3
"""Audit whether causal GSS state explains canonical-v29 heldout residuals.

No parameter is trained.  Canonical v29 predictions are frozen, analytic
train-set lookup means are fit on Redis-base events, and those fixed means are
applied to Redis-heldout.  G1 adds access-local cache state; G2 additionally
adds a lagged per-core LLC-miss-pressure bucket.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np
import torch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in os.sys.path:
    os.sys.path.insert(0, REPO_ROOT)

from tcsim.v29.contracts import FIELD_INDEX  # noqa: E402
from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences  # noqa: E402
from tcsim.v29.model import build_model  # noqa: E402
from tcsim.v30.gss import GSS_CATEGORICAL_FIELDS, GSS_CONTINUOUS_FIELDS  # noqa: E402


SCHEMA_VERSION = "tcsim-v30-gss-no-train-residual-audit-1"
CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}
CAT = {name: index for index, name in enumerate(GSS_CATEGORICAL_FIELDS)}
CONT = {name: index for index, name in enumerate(GSS_CONTINUOUS_FIELDS)}


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _ints(value: str) -> tuple[int, ...]:
    return tuple(int(item) for item in _csv(value))


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


def _records(
    manifest: Mapping[str, Any],
    splits: Sequence[str],
    workload_regex: str,
    core_counts: Sequence[int],
    max_traces: int,
) -> list[dict[str, Any]]:
    pattern = re.compile(workload_regex) if workload_regex else None
    allowed_cores = set(int(value) for value in core_counts)
    selected: dict[str, dict[str, Any]] = {}
    for split in splits:
        for raw in manifest.get("splits", {}).get(split, []):
            row = dict(raw)
            if pattern is not None and not pattern.search(str(row["workload"])):
                continue
            if allowed_cores and int(row["n_cores"]) not in allowed_cores:
                continue
            selected.setdefault(str(row["trace_id"]), row)
    rows = sorted(selected.values(), key=lambda row: str(row["trace_id"]))
    return rows[:max_traces] if max_traces > 0 else rows


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


def _sample_indices(dataset: V29GlobalTimeDataset, per_trace: int) -> list[int]:
    groups: dict[str, list[int]] = {}
    for index, trace_id in enumerate(dataset.sample_trace_ids):
        groups.setdefault(str(trace_id), []).append(index)
    selected = []
    for trace_id in sorted(groups):
        values = groups[trace_id]
        count = min(max(1, int(per_trace)), len(values))
        positions = np.linspace(0, len(values) - 1, num=count, dtype=np.int64)
        selected.extend(values[int(position)] for position in positions)
    return selected


@dataclass
class GSSSidecar:
    clock_source: str
    index: dict[int, np.ndarray]
    categorical: dict[int, np.ndarray]
    continuous: dict[int, np.ndarray]


def _load_sidecar(root: str, trace_id: str) -> GSSSidecar:
    path = os.path.join(root, "traces", trace_id)
    meta = _load_json(os.path.join(path, "meta.json"))
    if not bool(meta.get("features_are_pre_access")):
        raise RuntimeError(f"GSS sidecar is not pre-access causal: {path}")
    if bool(meta.get("timestamp_is_model_visible")):
        raise RuntimeError(f"GSS sidecar leaks timestamp values: {path}")
    if tuple(meta.get("categorical_fields", ())) != GSS_CATEGORICAL_FIELDS:
        raise RuntimeError(f"GSS categorical contract mismatch: {path}")
    if tuple(meta.get("continuous_fields", ())) != GSS_CONTINUOUS_FIELDS:
        raise RuntimeError(f"GSS continuous contract mismatch: {path}")
    indices = {}
    categorical = {}
    continuous = {}
    for core_meta in meta["cores"]:
        core = int(core_meta["core_id"])
        core_dir = os.path.join(path, "cores", str(core))
        indices[core] = np.load(os.path.join(core_dir, "index.npy"), mmap_mode="r")
        categorical[core] = np.load(
            os.path.join(core_dir, "categorical.npy"), mmap_mode="r",
        )
        continuous[core] = np.load(
            os.path.join(core_dir, "continuous.npy"), mmap_mode="r",
        )
        if not (
            len(indices[core]) == len(categorical[core]) == len(continuous[core])
        ):
            raise RuntimeError(f"GSS sidecar length mismatch core={core}: {path}")
    return GSSSidecar(
        clock_source=str(meta["clock_source"]),
        index=indices,
        categorical=categorical,
        continuous=continuous,
    )


@dataclass
class EventArrays:
    core_count: np.ndarray
    mem_kind: np.ndarray
    reuse: np.ndarray
    l1_pressure: np.ndarray
    l2_pressure: np.ndarray
    llc_pressure: np.ndarray
    hit_level: np.ndarray
    miss_kind: np.ndarray
    llc_position: np.ndarray
    llc_miss_ema_bucket: np.ndarray
    log_residual: np.ndarray
    cycle_residual: np.ndarray
    residual_valid: np.ndarray

    def __len__(self) -> int:
        return int(self.hit_level.shape[0])


def _subset(data: EventArrays, selected: np.ndarray) -> EventArrays:
    return EventArrays(**{
        name: getattr(data, name)[selected]
        for name in EventArrays.__dataclass_fields__
    })


def _concat(parts: list[np.ndarray], width: int | None = None) -> np.ndarray:
    if parts:
        return np.concatenate(parts, axis=0)
    shape = (0, int(width)) if width is not None else (0,)
    return np.empty(shape, dtype=np.float64)


def _span_residual(
    true_time: np.ndarray,
    pred_time: np.ndarray,
    position: int,
    distance: int,
    valid_count: int,
) -> tuple[float, float, bool]:
    if distance < 0:
        start = position + int(distance)
        endpoint = position - 1
        if start < 0 or endpoint < start:
            return 0.0, 0.0, False
    else:
        start = position
        endpoint = min(valid_count - 1, position + int(distance))
    true_before = float(true_time[start - 1]) if start else 0.0
    pred_before = float(pred_time[start - 1]) if start else 0.0
    true_span = max(0.0, float(true_time[endpoint]) - true_before)
    pred_span = max(0.0, float(pred_time[endpoint]) - pred_before)
    return (
        math.log1p(true_span) - math.log1p(pred_span),
        true_span - pred_span,
        True,
    )


def _extract(
    rows: Sequence[Mapping[str, Any]],
    *,
    checkpoint: str,
    sidecar_root: str,
    device: torch.device,
    sequences_per_trace: int,
    distances: Sequence[int],
    progress_every: int,
) -> tuple[EventArrays, dict[str, Any]]:
    dataset = V29GlobalTimeDataset(rows, sequence_length=1, sequence_stride=1)
    selected = _sample_indices(dataset, sequences_per_trace)
    payload = _torch_load(checkpoint, torch.device("cpu"))
    config = payload["config"]
    model = build_model(config["model"], config["chunk"]["horizons"])
    model.load_state_dict(payload["model"], strict=True)
    model.to(device).eval()
    trace_meta = {str(row["trace_id"]): dict(row) for row in rows}
    sidecars = {
        trace_id: _load_sidecar(sidecar_root, trace_id)
        for trace_id in trace_meta
    }
    clocks = sorted({sidecar.clock_source for sidecar in sidecars.values()})
    if len(clocks) != 1:
        raise RuntimeError(f"mixed GSS clock sources: {clocks}")
    parts: dict[str, list[np.ndarray]] = {
        name: [] for name in (
            "core_count", "mem_kind", "reuse", "l1_pressure", "l2_pressure",
            "llc_pressure", "hit_level", "miss_kind", "llc_position",
            "llc_miss_ema_bucket", "log_residual", "cycle_residual",
            "residual_valid",
        )
    }
    started = time.perf_counter()
    amp = (
        torch.autocast(device_type="cuda", dtype=torch.bfloat16)
        if device.type == "cuda" else torch.autocast(device_type="cpu", enabled=False)
    )
    with torch.inference_mode():
        for ordinal, dataset_index in enumerate(selected, start=1):
            item = dataset[int(dataset_index)]
            trace_id = str(item["trace_id"])
            row_meta = trace_meta[trace_id]
            sidecar = sidecars[trace_id]
            cpu_batch = collate_v29_sequences([item])
            batch = _device_batch(cpu_batch, device)
            with amp:
                static = model.static_encoder(batch["per_uop_fields"])
                prediction = model.forward_from_static(
                    batch, static, include_horizon_outputs=False,
                )
            pred_time = prediction["commit_time"].float().cpu().numpy()
            true_time = cpu_batch["commit_time_target"].numpy()
            valid = cpu_batch["valid_uop_mask"].numpy().astype(np.bool_)
            fields = cpu_batch["per_uop_fields"].numpy()
            cores = cpu_batch["core_slots"].numpy()
            cursors = cpu_batch["cursors"].numpy()
            for batch_row in range(valid.shape[0]):
                valid_count = int(valid[batch_row].sum())
                if valid_count <= 0:
                    continue
                core = int(cores[batch_row])
                cursor = int(cursors[batch_row])
                indices = sidecar.index[core]
                begin = int(np.searchsorted(indices, cursor, side="left"))
                end = int(np.searchsorted(indices, cursor + valid_count, side="left"))
                if end <= begin:
                    continue
                uop_indices = np.asarray(indices[begin:end], dtype=np.int64)
                positions = uop_indices - cursor
                cat = np.asarray(sidecar.categorical[core][begin:end], dtype=np.uint8)
                cont = np.asarray(sidecar.continuous[core][begin:end], dtype=np.float32)
                count = len(positions)
                log_residual = np.zeros((count, len(distances)), dtype=np.float64)
                cycle_residual = np.zeros_like(log_residual)
                residual_valid = np.zeros_like(log_residual, dtype=np.bool_)
                for event_index, position_value in enumerate(positions):
                    position = int(position_value)
                    for distance_index, distance in enumerate(distances):
                        log_value, cycle_value, usable = _span_residual(
                            true_time[batch_row], pred_time[batch_row],
                            position, int(distance), valid_count,
                        )
                        log_residual[event_index, distance_index] = log_value
                        cycle_residual[event_index, distance_index] = cycle_value
                        residual_valid[event_index, distance_index] = usable
                selected_fields = fields[batch_row, positions]
                parts["core_count"].append(
                    np.full(count, int(row_meta["n_cores"]), dtype=np.int16),
                )
                for name, field_name in (
                    ("mem_kind", "mem_kind"),
                    ("reuse", "reuse_distance"),
                    ("l1_pressure", "l1_set_pressure"),
                    ("l2_pressure", "l2_set_pressure"),
                    ("llc_pressure", "llc_set_pressure"),
                ):
                    parts[name].append(
                        selected_fields[:, FIELD_INDEX[field_name]].astype(
                            np.uint8, copy=False,
                        )
                    )
                parts["hit_level"].append(cat[:, CAT["proxy_hit_level"]])
                parts["miss_kind"].append(cat[:, CAT["proxy_miss_kind"]])
                parts["llc_position"].append(cat[:, CAT["llc_pre_access_position"]])
                ema = cont[:, CONT["core_llc_miss_rate_ema"]]
                parts["llc_miss_ema_bucket"].append(
                    np.minimum(7, np.floor(ema * 8.0).astype(np.uint8))
                )
                parts["log_residual"].append(log_residual)
                parts["cycle_residual"].append(cycle_residual)
                parts["residual_valid"].append(residual_valid)
            if progress_every > 0 and ordinal % progress_every == 0:
                print(
                    f"[v30-gss-audit] {ordinal}/{len(selected)} sequences "
                    f"events={sum(len(value) for value in parts['hit_level'])} "
                    f"elapsed={time.perf_counter()-started:.1f}s",
                    flush=True,
                )
    arrays = EventArrays(
        core_count=_concat(parts["core_count"]).astype(np.int16, copy=False),
        mem_kind=_concat(parts["mem_kind"]).astype(np.uint8, copy=False),
        reuse=_concat(parts["reuse"]).astype(np.uint8, copy=False),
        l1_pressure=_concat(parts["l1_pressure"]).astype(np.uint8, copy=False),
        l2_pressure=_concat(parts["l2_pressure"]).astype(np.uint8, copy=False),
        llc_pressure=_concat(parts["llc_pressure"]).astype(np.uint8, copy=False),
        hit_level=_concat(parts["hit_level"]).astype(np.uint8, copy=False),
        miss_kind=_concat(parts["miss_kind"]).astype(np.uint8, copy=False),
        llc_position=_concat(parts["llc_position"]).astype(np.uint8, copy=False),
        llc_miss_ema_bucket=_concat(parts["llc_miss_ema_bucket"]).astype(np.uint8, copy=False),
        log_residual=_concat(parts["log_residual"], len(distances)),
        cycle_residual=_concat(parts["cycle_residual"], len(distances)),
        residual_valid=_concat(parts["residual_valid"], len(distances)).astype(np.bool_),
    )
    metadata = {
        "traces": len(rows),
        "trace_ids": [str(row["trace_id"]) for row in rows],
        "dataset_sequences": len(dataset),
        "sampled_sequences": len(selected),
        "events": len(arrays),
        "clock_source": clocks[0],
        "elapsed_seconds": time.perf_counter() - started,
    }
    del model
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return arrays, metadata


def _keys(data: EventArrays, mode: str) -> np.ndarray:
    key = data.core_count.astype(np.int64)
    for values, radix in (
        (data.mem_kind, 5),
        (data.reuse, 10),
        (data.l1_pressure, 11),
        (data.l2_pressure, 11),
        (data.llc_pressure, 11),
    ):
        key = key * radix + values.astype(np.int64)
    if mode in {"g1", "g2"}:
        llc_position = np.minimum(data.llc_position, 16)
        for values, radix in (
            (data.hit_level, 5),
            (data.miss_kind, 4),
            (llc_position, 17),
        ):
            key = key * radix + values.astype(np.int64)
    if mode == "g2":
        key = key * 8 + data.llc_miss_ema_bucket.astype(np.int64)
    return key


def _lookup_means(keys: np.ndarray, values: np.ndarray):
    unique, inverse = np.unique(keys, return_inverse=True)
    count = np.bincount(inverse).astype(np.int64)
    sums = np.bincount(inverse, weights=values)
    return unique, count, sums / np.maximum(1, count)


def _lookup(keys: np.ndarray, unique: np.ndarray, values: np.ndarray):
    positions = np.searchsorted(unique, keys)
    clipped = np.minimum(positions, max(0, len(unique) - 1))
    found = (positions < len(unique)) & (unique[clipped] == keys)
    output = np.zeros(len(keys), dtype=np.float64)
    if len(unique):
        output[found] = values[clipped[found]]
    return output, found, clipped


def _evaluate(
    train: EventArrays,
    evaluation: EventArrays,
    distances: Sequence[int],
    shrinkage: float,
) -> Mapping[str, Any]:
    rows = []
    for column, distance in enumerate(distances):
        train_valid = train.residual_valid[:, column]
        eval_valid = evaluation.residual_valid[:, column]
        train_values = train.log_residual[train_valid, column]
        eval_values = evaluation.log_residual[eval_valid, column]
        predictions = {}
        supports = {}
        counts_report = {}
        base_keys_train = _keys(train, "base")[train_valid]
        base_keys_eval = _keys(evaluation, "base")[eval_valid]
        base_unique, base_count, base_mean = _lookup_means(base_keys_train, train_values)
        base_prediction, base_found, _ = _lookup(base_keys_eval, base_unique, base_mean)
        global_mean = float(train_values.mean()) if len(train_values) else 0.0
        base_prediction[~base_found] = global_mean
        predictions["base"] = base_prediction
        supports["base"] = float(base_found.mean())
        counts_report["base_cells"] = len(base_unique)
        for mode in ("g1", "g2"):
            train_keys = _keys(train, mode)[train_valid]
            eval_keys = _keys(evaluation, mode)[eval_valid]
            unique, count, mean = _lookup_means(train_keys, train_values)
            raw, found, positions = _lookup(eval_keys, unique, mean)
            weight = np.zeros(len(eval_keys), dtype=np.float64)
            if len(unique):
                weight[found] = count[positions[found]] / (
                    count[positions[found]] + float(shrinkage)
                )
            predictions[mode] = base_prediction + weight * (raw - base_prediction)
            supports[mode] = float(found.mean())
            counts_report[f"{mode}_cells"] = len(unique)
        zero_mae = float(np.mean(np.abs(eval_values)))
        base_mae = float(np.mean(np.abs(eval_values - predictions["base"])))
        g1_mae = float(np.mean(np.abs(eval_values - predictions["g1"])))
        g2_mae = float(np.mean(np.abs(eval_values - predictions["g2"])))
        rows.append({
            "distance_uops": int(distance),
            "train_events": int(train_valid.sum()),
            "evaluation_events": int(eval_valid.sum()),
            "zero_correction_log_mae": zero_mae,
            "base_lookup_log_mae": base_mae,
            "g1_lookup_log_mae": g1_mae,
            "g2_lookup_log_mae": g2_mae,
            "g1_minus_base": g1_mae - base_mae,
            "g2_minus_base": g2_mae - base_mae,
            "g1_relative_change": g1_mae / base_mae - 1.0 if base_mae else None,
            "g2_relative_change": g2_mae / base_mae - 1.0 if base_mae else None,
            "support": supports,
            "cells": counts_report,
        })
    return {"rows": rows}


def _hit_effect(data: EventArrays, distances: Sequence[int]) -> list[dict[str, Any]]:
    output = []
    for column, distance in enumerate(distances):
        valid = data.residual_valid[:, column]
        for level, name in ((1, "L1"), (2, "L2"), (3, "LLC"), (4, "MEMORY")):
            selected = valid & (data.hit_level == level)
            if not bool(selected.any()):
                continue
            output.append({
                "distance_uops": int(distance),
                "hit_level": name,
                "events": int(selected.sum()),
                "mean_log_residual": float(data.log_residual[selected, column].mean()),
                "mean_cycle_residual": float(data.cycle_residual[selected, column].mean()),
                "mean_absolute_log_residual": float(
                    np.abs(data.log_residual[selected, column]).mean()
                ),
            })
    return output


def _evaluate_by_core_count(
    train: EventArrays,
    evaluation: EventArrays,
    distances: Sequence[int],
    shrinkage: float,
) -> Mapping[str, Any]:
    output = {}
    shared = sorted(set(train.core_count.tolist()) & set(evaluation.core_count.tolist()))
    for cores in shared:
        train_selected = train.core_count == int(cores)
        evaluation_selected = evaluation.core_count == int(cores)
        output[str(int(cores))] = {
            "train_events": int(train_selected.sum()),
            "evaluation_events": int(evaluation_selected.sum()),
            **_evaluate(
                _subset(train, train_selected),
                _subset(evaluation, evaluation_selected),
                distances,
                shrinkage,
            ),
        }
    return output


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# v30 GSS no-training residual audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Checkpoint: `{report['selection']['checkpoint']}`",
        f"- Teacher clock: `{report['train']['clock_source']}` (timestamp values are not model inputs)",
        f"- Train/evaluation events: `{report['train']['events']:,}/{report['evaluation']['events']:,}`",
        f"- Lookup shrinkage: `{report['selection']['shrinkage']}` events",
        "",
        "G1 adds access-local pre-state. G2 adds G1 plus lagged per-core LLC-miss EMA. Negative `Gx-base` is better.",
        "",
        "| relative UOP span | base log-MAE | G1 | G1-base | G2 | G2-base | G1/G2 support |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in report["heldout_lookup"]["rows"]:
        lines.append(
            f"| {row['distance_uops']:+d} | {row['base_lookup_log_mae']:.6f} | "
            f"{row['g1_lookup_log_mae']:.6f} | {row['g1_minus_base']:+.6f} "
            f"({100*row['g1_relative_change']:+.2f}%) | "
            f"{row['g2_lookup_log_mae']:.6f} | {row['g2_minus_base']:+.6f} "
            f"({100*row['g2_relative_change']:+.2f}%) | "
            f"{100*row['support']['g1']:.1f}%/{100*row['support']['g2']:.1f}% |"
        )
    lines.extend([
        "",
        "## G1 by core count",
        "",
        "| cores | span | evaluation events | G1-base | relative | support |",
        "|---:|---:|---:|---:|---:|---:|",
    ])
    for cores, result in report["heldout_lookup_by_core_count"].items():
        for row in result["rows"]:
            if int(row["distance_uops"]) not in (0, 32, 128):
                continue
            lines.append(
                f"| {cores} | {row['distance_uops']:+d} | {row['evaluation_events']} | "
                f"{row['g1_minus_base']:+.6f} | "
                f"{100*row['g1_relative_change']:+.2f}% | "
                f"{100*row['support']['g1']:.1f}% |"
            )
    lines.extend([
        "",
        "Interpretation guardrails:",
        "",
        "- Improvement at positive spans with little/no improvement at negative spans supports causal forward value.",
        "- Improvement only at span 0 may be local correlation rather than downstream memory exposure.",
        "- Poor support means Redis-base does not cover the Redis-heldout GSS states; training data coverage must be fixed before changing model capacity.",
        "- This audit ranks information value; it does not prove a neural adapter can realize the same correction.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default=os.path.join(REPO_ROOT, "data/v29_global_time_dataset/manifest.json"),
    )
    parser.add_argument(
        "--checkpoint", default=os.path.join(REPO_ROOT, "ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt"),
    )
    parser.add_argument(
        "--sidecar-root", default=os.path.join(REPO_ROOT, "data/v30_gss_ready_sidecars"),
    )
    parser.add_argument("--train-splits", default="train")
    parser.add_argument("--evaluation-splits", default="development_heldout")
    parser.add_argument("--train-workload-regex", default="redis_base$")
    parser.add_argument("--evaluation-workload-regex", default="redis_heldout$")
    parser.add_argument("--core-counts", default="4,8,16,32")
    parser.add_argument("--sequences-per-trace", type=int, default=16)
    parser.add_argument("--distances", default="-64,-32,-16,0,16,32,64,128")
    parser.add_argument("--shrinkage", type=float, default=32.0)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max-train-traces", type=int, default=0)
    parser.add_argument("--max-evaluation-traces", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=16)
    parser.add_argument(
        "--out", default=os.path.join(REPO_ROOT, "logs", f"v30_gss_no_train_residual_{timestamp}"),
    )
    args = parser.parse_args()
    distances = _ints(args.distances)
    if not distances or any(abs(value) >= 256 for value in distances):
        raise ValueError("distances must have absolute value below 256")
    if args.sequences_per_trace <= 0 or args.shrinkage < 0:
        raise ValueError("invalid sampling/shrinkage configuration")
    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable")
    manifest_path = os.path.abspath(args.manifest)
    checkpoint = os.path.abspath(args.checkpoint)
    sidecar_root = os.path.abspath(args.sidecar_root)
    manifest = _load_json(manifest_path)
    train_rows = _records(
        manifest, _csv(args.train_splits), args.train_workload_regex,
        _ints(args.core_counts),
        args.max_train_traces,
    )
    evaluation_rows = _records(
        manifest, _csv(args.evaluation_splits), args.evaluation_workload_regex,
        _ints(args.core_counts),
        args.max_evaluation_traces,
    )
    if not train_rows or not evaluation_rows:
        raise RuntimeError("train/evaluation trace selection is empty")
    train, train_meta = _extract(
        train_rows, checkpoint=checkpoint, sidecar_root=sidecar_root,
        device=device, sequences_per_trace=args.sequences_per_trace,
        distances=distances, progress_every=args.progress_every,
    )
    evaluation, evaluation_meta = _extract(
        evaluation_rows, checkpoint=checkpoint, sidecar_root=sidecar_root,
        device=device, sequences_per_trace=args.sequences_per_trace,
        distances=distances, progress_every=args.progress_every,
    )
    if train_meta["clock_source"] != evaluation_meta["clock_source"]:
        raise RuntimeError("train/evaluation GSS clock mismatch")
    payload = _torch_load(checkpoint, torch.device("cpu"))
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
        "selection": {
            "manifest": manifest_path,
            "checkpoint": checkpoint,
            "checkpoint_step": int(payload["step"]),
            "sidecar_root": sidecar_root,
            "train_splits": list(_csv(args.train_splits)),
            "evaluation_splits": list(_csv(args.evaluation_splits)),
            "train_workload_regex": args.train_workload_regex,
            "evaluation_workload_regex": args.evaluation_workload_regex,
            "core_counts": list(_ints(args.core_counts)),
            "sequences_per_trace": int(args.sequences_per_trace),
            "distances_uops": list(distances),
            "shrinkage": float(args.shrinkage),
        },
        "train": train_meta,
        "evaluation": evaluation_meta,
        "heldout_lookup": _evaluate(
            train, evaluation, distances, float(args.shrinkage),
        ),
        "heldout_lookup_by_core_count": _evaluate_by_core_count(
            train, evaluation, distances, float(args.shrinkage),
        ),
        "train_hit_effect": _hit_effect(train, distances),
        "evaluation_hit_effect": _hit_effect(evaluation, distances),
    }
    output = os.path.abspath(args.out)
    os.makedirs(output, exist_ok=True)
    _dump_json(os.path.join(output, "report.json"), report)
    markdown = _markdown(report)
    with open(os.path.join(output, "report.md"), "w", encoding="utf-8") as handle:
        handle.write(markdown)
    print(markdown)
    print(f"[v30-gss-audit] output={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
