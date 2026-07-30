#!/usr/bin/env python3
"""Estimate B2 timing value from canonical-v29 residuals without training.

For replayed branch events, the audit measures canonical-v29 cumulative timing
residuals after the branch.  Analytic train-set lookup means are then applied
to development-heldout events.  The baseline lookup excludes the replay event;
the B2 lookup adds it.  This is an information/value audit, not model fitting.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import torch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in os.sys.path:
    os.sys.path.insert(0, REPO_ROOT)

from tcsim.v29.contracts import FIELD_INDEX  # noqa: E402
from tcsim.v29.dataset import (  # noqa: E402
    V29GlobalTimeDataset,
    collate_v29_sequences,
)
from tcsim.v29.model import build_model  # noqa: E402


SCHEMA_VERSION = "tcsim-v30-b2-residual-signal-audit-1"
CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


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
    manifest: Mapping[str, Any], splits: Sequence[str], max_traces: int
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for split in splits:
        for raw in manifest.get("splits", {}).get(split, []):
            row = dict(raw)
            trace_id = str(row["trace_id"])
            if trace_id not in selected:
                selected[trace_id] = row
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


def _sample_indices(
    dataset: V29GlobalTimeDataset, per_trace: int
) -> list[int]:
    groups: dict[str, list[int]] = defaultdict(list)
    for index, trace_id in enumerate(dataset.sample_trace_ids):
        groups[str(trace_id)].append(index)
    selected = []
    for trace_id in sorted(groups):
        values = groups[trace_id]
        count = min(max(1, int(per_trace)), len(values))
        positions = np.linspace(0, len(values) - 1, num=count, dtype=np.int64)
        selected.extend(values[int(position)] for position in positions)
    return selected


@dataclass
class EventArrays:
    workload: np.ndarray
    core_count: np.ndarray
    branch_kind: np.ndarray
    cold: np.ndarray
    event_code: np.ndarray
    log_residual: np.ndarray
    cycle_residual: np.ndarray

    def __len__(self) -> int:
        return int(self.event_code.shape[0])


def _concat(parts: list[np.ndarray], *, width: int | None = None) -> np.ndarray:
    if parts:
        return np.concatenate(parts, axis=0)
    shape = (0, width) if width is not None else (0,)
    return np.empty(shape, dtype=np.float64)


def _extract(
    rows: Sequence[Mapping[str, Any]],
    *,
    checkpoint: str,
    device: torch.device,
    sequences_per_trace: int,
    distances: Sequence[int],
    progress_every: int,
) -> tuple[EventArrays, dict[str, Any]]:
    dataset = V29GlobalTimeDataset(
        rows,
        sequence_length=4,
        sequence_stride=4,
    )
    selected = _sample_indices(dataset, sequences_per_trace)
    payload = _torch_load(checkpoint, torch.device("cpu"))
    config = payload["config"]
    model = build_model(config["model"], config["chunk"]["horizons"])
    model.load_state_dict(payload["model"], strict=True)
    model.to(device)
    model.eval()
    trace_meta = {str(row["trace_id"]): dict(row) for row in rows}
    workload_names = sorted({str(row["workload"]) for row in rows})
    workload_ids = {name: index for index, name in enumerate(workload_names)}
    workload_parts: list[np.ndarray] = []
    core_parts: list[np.ndarray] = []
    kind_parts: list[np.ndarray] = []
    cold_parts: list[np.ndarray] = []
    event_parts: list[np.ndarray] = []
    log_parts: list[np.ndarray] = []
    cycle_parts: list[np.ndarray] = []
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
            branch = cpu_batch["branch_mask"].numpy().astype(np.bool_) & valid
            event = cpu_batch["branch_replay_event"].numpy().astype(np.uint8)
            fields = cpu_batch["per_uop_fields"].numpy()
            workload_id = workload_ids[str(row_meta["workload"])]
            n_cores = int(row_meta["n_cores"])
            for batch_row in range(valid.shape[0]):
                valid_count = int(valid[batch_row].sum())
                if valid_count <= 0:
                    continue
                positions = np.flatnonzero(branch[batch_row, :valid_count])
                if not len(positions):
                    continue
                event_rows = event[batch_row, positions]
                code = (
                    event_rows[:, 0]
                    + 2 * event_rows[:, 1]
                    + 4 * event_rows[:, 2]
                    + 8 * event_rows[:, 3]
                ).astype(np.uint8)
                log_residual = np.empty((len(positions), len(distances)), dtype=np.float64)
                cycle_residual = np.empty_like(log_residual)
                for event_index, position_value in enumerate(positions):
                    position = int(position_value)
                    true_before = float(true_time[batch_row, position - 1]) if position else 0.0
                    pred_before = float(pred_time[batch_row, position - 1]) if position else 0.0
                    for distance_index, distance in enumerate(distances):
                        endpoint = min(valid_count - 1, position + int(distance))
                        true_span = max(
                            0.0, float(true_time[batch_row, endpoint]) - true_before,
                        )
                        pred_span = max(
                            0.0, float(pred_time[batch_row, endpoint]) - pred_before,
                        )
                        log_residual[event_index, distance_index] = (
                            math.log1p(true_span) - math.log1p(pred_span)
                        )
                        cycle_residual[event_index, distance_index] = true_span - pred_span
                workload_parts.append(np.full(len(positions), workload_id, dtype=np.int16))
                core_parts.append(np.full(len(positions), n_cores, dtype=np.int16))
                kind_parts.append(
                    fields[batch_row, positions, FIELD_INDEX["branch_kind"]].astype(
                        np.int16, copy=False,
                    )
                )
                cold_parts.append(event_rows[:, 3].astype(np.uint8, copy=False))
                event_parts.append(code)
                log_parts.append(log_residual)
                cycle_parts.append(cycle_residual)
            if progress_every > 0 and ordinal % progress_every == 0:
                elapsed = time.perf_counter() - started
                events = sum(len(part) for part in event_parts)
                print(
                    f"[v30-b2-residual] {ordinal}/{len(selected)} sequences "
                    f"events={events} elapsed={elapsed:.1f}s",
                    flush=True,
                )
    arrays = EventArrays(
        workload=_concat(workload_parts).astype(np.int16, copy=False),
        core_count=_concat(core_parts).astype(np.int16, copy=False),
        branch_kind=_concat(kind_parts).astype(np.int16, copy=False),
        cold=_concat(cold_parts).astype(np.uint8, copy=False),
        event_code=_concat(event_parts).astype(np.uint8, copy=False),
        log_residual=_concat(log_parts, width=len(distances)),
        cycle_residual=_concat(cycle_parts, width=len(distances)),
    )
    metadata = {
        "traces": len(rows),
        "dataset_sequences": len(dataset),
        "sampled_sequences": len(selected),
        "events": len(arrays),
        "workload_ids": {str(index): name for name, index in workload_ids.items()},
        "elapsed_seconds": time.perf_counter() - started,
    }
    del model
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return arrays, metadata


def _keys(data: EventArrays, include_event: bool) -> np.ndarray:
    # Cardinalities are deliberately explicit so the integer key is stable.
    key = data.core_count.astype(np.int64)
    key = key * 16 + data.branch_kind.astype(np.int64)
    key = key * 2 + data.cold.astype(np.int64)
    if include_event:
        key = key * 16 + data.event_code.astype(np.int64)
    return key


def _lookup_means(
    train_keys: np.ndarray, train_values: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    unique, inverse = np.unique(train_keys, return_inverse=True)
    count = np.bincount(inverse).astype(np.int64)
    sums = np.stack([
        np.bincount(inverse, weights=train_values[:, column])
        for column in range(train_values.shape[1])
    ], axis=1)
    return unique, count, sums / np.maximum(count[:, None], 1)


def _lookup(
    keys: np.ndarray,
    unique: np.ndarray,
    means: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    positions = np.searchsorted(unique, keys)
    clipped = np.minimum(positions, max(0, len(unique) - 1))
    found = (positions < len(unique)) & (unique[clipped] == keys)
    output = np.zeros((len(keys), means.shape[1]), dtype=np.float64)
    if len(unique):
        output[found] = means[clipped[found]]
    return output, found


def _lookup_evaluation(
    train: EventArrays,
    evaluation: EventArrays,
    distances: Sequence[int],
) -> dict[str, Any]:
    base_unique, _base_count, base_means = _lookup_means(
        _keys(train, False), train.log_residual,
    )
    event_unique, _event_count, event_means = _lookup_means(
        _keys(train, True), train.log_residual,
    )
    base_prediction, base_found = _lookup(
        _keys(evaluation, False), base_unique, base_means,
    )
    event_prediction, event_found = _lookup(
        _keys(evaluation, True), event_unique, event_means,
    )
    event_prediction[~event_found] = base_prediction[~event_found]
    rows = []
    for index, distance in enumerate(distances):
        values = evaluation.log_residual[:, index]
        zero_mae = float(np.mean(np.abs(values)))
        base_mae = float(np.mean(np.abs(values - base_prediction[:, index])))
        event_mae = float(np.mean(np.abs(values - event_prediction[:, index])))
        rows.append({
            "distance_uops": int(distance),
            "zero_correction_log_mae": zero_mae,
            "base_lookup_log_mae": base_mae,
            "event_lookup_log_mae": event_mae,
            "event_incremental_delta": event_mae - base_mae,
            "event_relative_change": (
                event_mae / base_mae - 1.0 if base_mae else None
            ),
        })
    return {
        "base_key_support": float(base_found.mean()),
        "event_key_support": float(event_found.mean()),
        "rows": rows,
    }


def _miss_effect(
    data: EventArrays,
    workload_names: Mapping[str, str],
    distances: Sequence[int],
) -> dict[str, Any]:
    miss = (data.event_code & 1) != 0
    pooled = []
    for column, distance in enumerate(distances):
        miss_values = data.log_residual[miss, column]
        hit_values = data.log_residual[~miss, column]
        pooled.append({
            "distance_uops": int(distance),
            "miss_events": int(len(miss_values)),
            "hit_events": int(len(hit_values)),
            "miss_mean_log_residual": float(np.mean(miss_values)),
            "hit_mean_log_residual": float(np.mean(hit_values)),
            "miss_minus_hit": float(np.mean(miss_values) - np.mean(hit_values)),
            "miss_mean_cycle_residual": float(np.mean(data.cycle_residual[miss, column])),
            "hit_mean_cycle_residual": float(np.mean(data.cycle_residual[~miss, column])),
        })
    cells = []
    for workload_id in np.unique(data.workload):
        for core in np.unique(data.core_count[data.workload == workload_id]):
            selected = (data.workload == workload_id) & (data.core_count == core)
            selected_miss = selected & miss
            selected_hit = selected & ~miss
            if selected_miss.sum() < 20 or selected_hit.sum() < 20:
                continue
            deltas = [
                float(
                    data.log_residual[selected_miss, column].mean()
                    - data.log_residual[selected_hit, column].mean()
                )
                for column in range(len(distances))
            ]
            cells.append({
                "workload": workload_names[str(int(workload_id))],
                "n_cores": int(core),
                "miss_events": int(selected_miss.sum()),
                "hit_events": int(selected_hit.sum()),
                "miss_minus_hit": deltas,
            })
    consistency = []
    for column, distance in enumerate(distances):
        pooled_sign = np.sign(pooled[column]["miss_minus_hit"])
        comparable = [row for row in cells if row["miss_minus_hit"][column] != 0]
        same = sum(
            np.sign(row["miss_minus_hit"][column]) == pooled_sign
            for row in comparable
        )
        consistency.append({
            "distance_uops": int(distance),
            "cells": len(comparable),
            "same_sign_fraction": same / max(1, len(comparable)),
        })
    return {"pooled": pooled, "cell_consistency": consistency, "cells": cells}


def _markdown(report: Mapping[str, Any]) -> str:
    lookup = report["development_heldout_lookup"]
    lines = [
        "# v30 B2 canonical-v29 residual signal audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Checkpoint: `{report['selection']['checkpoint']}`",
        f"- Train/evaluation events: `{report['train']['events']:,}/{report['evaluation']['events']:,}`",
        f"- Base/event lookup support: `{100*lookup['base_key_support']:.3f}%/{100*lookup['event_key_support']:.3f}%`",
        "",
        "## Analytic lookup on development-heldout",
        "",
        "The base key is `(core count, branch kind, cold)`; the B2 key adds the four-bit replay event.",
        "",
        "| distance after branch | no correction log-MAE | base lookup | B2 event lookup | B2-base | relative |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in lookup["rows"]:
        lines.append(
            f"| {row['distance_uops']} | {row['zero_correction_log_mae']:.6f} | "
            f"{row['base_lookup_log_mae']:.6f} | {row['event_lookup_log_mae']:.6f} | "
            f"{row['event_incremental_delta']:+.6f} | "
            f"{100*row['event_relative_change']:+.2f}% |"
        )
    lines.extend([
        "",
        "## Miss versus hit residual position",
        "",
        "| set | distance | miss-hit log residual | same-sign workload×core cells |",
        "|---|---:|---:|---:|",
    ])
    for name in ("train_miss_effect", "evaluation_miss_effect"):
        effect = report[name]
        consistency = {
            int(row["distance_uops"]): row for row in effect["cell_consistency"]
        }
        for row in effect["pooled"]:
            cell = consistency[int(row["distance_uops"])]
            lines.append(
                f"| {name.replace('_miss_effect', '')} | {row['distance_uops']} | "
                f"{row['miss_minus_hit']:+.6f} | "
                f"{100*cell['same_sign_fraction']:.1f}% ({cell['cells']}) |"
            )
    lines.extend([
        "",
        "## Decision rule",
        "",
        "- A negative `B2-base` means replay event state explains heldout residual beyond branch kind/cold context.",
        "- A positive value means the train event penalty table transfers worse than a context-only table; B2 is then a shortcut risk unless routing/exposure is changed.",
        "- Increasing miss-hit residual only after the branch supports causal forward routing; a large distance-0 effect is more compatible with local correlation or attribution leakage.",
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
    parser.add_argument("--train-splits", default="train")
    parser.add_argument("--evaluation-splits", default="development_heldout")
    parser.add_argument("--sequences-per-trace", type=int, default=8)
    parser.add_argument("--distances", default="0,16,32,64,128")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--max-train-traces", type=int, default=0)
    parser.add_argument("--max-evaluation-traces", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=50)
    parser.add_argument(
        "--out",
        default=os.path.join(REPO_ROOT, "logs", f"v30_b2_no_train_residual_{timestamp}"),
    )
    args = parser.parse_args()
    if args.sequences_per_trace <= 0:
        raise ValueError("sequences-per-trace must be positive")
    distances = _ints(args.distances)
    if not distances or any(value < 0 or value >= 256 for value in distances):
        raise ValueError("distances must be in [0,255]")
    device = torch.device(args.device)
    if device.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but unavailable")
    manifest_path = os.path.abspath(args.manifest)
    checkpoint = os.path.abspath(args.checkpoint)
    manifest = _load_json(manifest_path)
    train_rows = _records(
        manifest, _csv(args.train_splits), args.max_train_traces,
    )
    evaluation_rows = _records(
        manifest, _csv(args.evaluation_splits), args.max_evaluation_traces,
    )
    train, train_meta = _extract(
        train_rows,
        checkpoint=checkpoint,
        device=device,
        sequences_per_trace=args.sequences_per_trace,
        distances=distances,
        progress_every=args.progress_every,
    )
    evaluation, evaluation_meta = _extract(
        evaluation_rows,
        checkpoint=checkpoint,
        device=device,
        sequences_per_trace=args.sequences_per_trace,
        distances=distances,
        progress_every=args.progress_every,
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
        "selection": {
            "manifest": manifest_path,
            "checkpoint": checkpoint,
            "checkpoint_step": int(_torch_load(checkpoint, torch.device("cpu"))["step"]),
            "train_splits": list(_csv(args.train_splits)),
            "evaluation_splits": list(_csv(args.evaluation_splits)),
            "sequences_per_trace": args.sequences_per_trace,
            "distances_uops": list(distances),
        },
        "train": train_meta,
        "evaluation": evaluation_meta,
        "development_heldout_lookup": _lookup_evaluation(
            train, evaluation, distances,
        ),
        "train_miss_effect": _miss_effect(
            train, train_meta["workload_ids"], distances,
        ),
        "evaluation_miss_effect": _miss_effect(
            evaluation, evaluation_meta["workload_ids"], distances,
        ),
    }
    output = os.path.abspath(args.out)
    os.makedirs(output, exist_ok=True)
    json_path = os.path.join(output, "report.json")
    markdown_path = os.path.join(output, "report.md")
    _dump_json(json_path, report)
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(f"[v30-b2-residual] json={json_path} markdown={markdown_path}")
    print(_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
