#!/usr/bin/env python3
"""Training-free distribution audit for v30 B2 configured-replay events.

The audit reads only materialized branch sidecars.  It measures train/eval
support overlap, distribution shift, and how strongly the compact event
histogram identifies a workload.  No model or optimizer is constructed.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence

import numpy as np


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENT_FIELDS = (
    "replay_full_miss",
    "replay_direction_miss",
    "replay_target_miss",
    "replay_cold_state",
)
SCHEMA_VERSION = "tcsim-v30-b2-feature-distribution-audit-1"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.tmp-{os.getpid()}"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(temporary, path)


def _records(
    manifest: Mapping[str, Any], splits: Sequence[str]
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for split in splits:
        for raw in manifest.get("splits", {}).get(split, []):
            row = dict(raw)
            trace_id = str(row["trace_id"])
            row.setdefault("selected_splits", []).append(split)
            if trace_id in selected:
                selected[trace_id]["selected_splits"].append(split)
            else:
                selected[trace_id] = row
    return sorted(selected.values(), key=lambda row: str(row["trace_id"]))


def _event_code(event: np.ndarray) -> np.ndarray:
    values = np.asarray(event, dtype=np.uint8)
    if values.ndim != 2 or values.shape[1] != len(EVENT_FIELDS):
        raise ValueError(f"event shape {values.shape} != [B,{len(EVENT_FIELDS)}]")
    weights = np.asarray([1, 2, 4, 8], dtype=np.uint8)
    return (values * weights[None, :]).sum(axis=1, dtype=np.uint8)


def _trace_summary(row: Mapping[str, Any]) -> dict[str, Any]:
    sidecar = os.path.abspath(str(row["branch_replay_dir"]))
    meta = _load(os.path.join(sidecar, "meta.json"))
    if meta.get("quality", {}).get("status") != "pass":
        raise RuntimeError(f"branch sidecar quality is not pass: {sidecar}")
    if tuple(meta.get("event_names", [])) != EVENT_FIELDS:
        raise RuntimeError(f"unexpected branch event contract: {sidecar}")
    counts = np.zeros(16, dtype=np.int64)
    fields = np.zeros(len(EVENT_FIELDS), dtype=np.int64)
    per_core: list[dict[str, Any]] = []
    for core_id in meta["core_ids"]:
        event = np.load(
            os.path.join(sidecar, "cores", str(int(core_id)), "event.npy"),
            mmap_mode="r",
        )
        codes = _event_code(event)
        core_counts = np.bincount(codes, minlength=16).astype(np.int64)
        core_fields = np.asarray(event, dtype=np.int64).sum(axis=0)
        counts += core_counts
        fields += core_fields
        per_core.append({
            "core_id": int(core_id),
            "branches": int(len(event)),
            "combination_counts": core_counts.tolist(),
            "field_counts": core_fields.tolist(),
        })
    branches = int(counts.sum())
    probability = counts.astype(np.float64) / max(1, branches)
    # The histogram plus explicit marginal rates is intentionally redundant:
    # nearest-centroid accuracy then has a direct workload-shortcut meaning.
    signature = np.concatenate((probability, fields / max(1, branches)))
    return {
        "trace_id": str(row["trace_id"]),
        "workload": str(row["workload"]),
        "workload_role": str(row.get("workload_role", "unknown")),
        "seed": int(row["seed"]),
        "n_cores": int(row["n_cores"]),
        "selected_splits": list(row.get("selected_splits", [])),
        "branches": branches,
        "combination_counts": counts.tolist(),
        "combination_probability": probability.tolist(),
        "field_counts": fields.tolist(),
        "field_rates": (fields / max(1, branches)).tolist(),
        "signature": signature.tolist(),
        "per_core": per_core,
        "branch_replay_dir": sidecar,
    }


def _aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    combination = np.sum(
        [np.asarray(row["combination_counts"], dtype=np.int64) for row in rows],
        axis=0,
        dtype=np.int64,
    )
    fields = np.sum(
        [np.asarray(row["field_counts"], dtype=np.int64) for row in rows],
        axis=0,
        dtype=np.int64,
    )
    branches = int(combination.sum())
    return {
        "traces": len(rows),
        "branches": branches,
        "combination_counts": combination.tolist(),
        "combination_probability": (
            combination.astype(np.float64) / max(1, branches)
        ).tolist(),
        "field_counts": fields.tolist(),
        "field_rates": (fields / max(1, branches)).tolist(),
    }


def _js_divergence(left: np.ndarray, right: np.ndarray) -> float:
    p = np.asarray(left, dtype=np.float64)
    q = np.asarray(right, dtype=np.float64)
    p = p / max(float(p.sum()), 1.0e-30)
    q = q / max(float(q.sum()), 1.0e-30)
    midpoint = 0.5 * (p + q)

    def kl(a: np.ndarray, b: np.ndarray) -> float:
        selected = a > 0
        return float(np.sum(a[selected] * np.log2(a[selected] / b[selected])))

    return 0.5 * kl(p, midpoint) + 0.5 * kl(q, midpoint)


def _distance(left: Sequence[float], right: Sequence[float]) -> float:
    # Hellinger on the 16-state event distribution plus a small marginal-rate
    # term.  It is bounded and stable for rare target-miss combinations.
    a = np.asarray(left, dtype=np.float64)
    b = np.asarray(right, dtype=np.float64)
    hist = math.sqrt(
        max(0.0, float(np.square(np.sqrt(a[:16]) - np.sqrt(b[:16])).sum()))
    ) / math.sqrt(2.0)
    marginal = float(np.abs(a[16:] - b[16:]).mean())
    return hist + 0.25 * marginal


def _base_name(workload: str) -> str:
    suffix = "_heldout"
    return workload[:-len(suffix)] + "_base" if workload.endswith(suffix) else workload


def _centroids(
    rows: Iterable[Mapping[str, Any]], *, exclude_trace: str | None = None
) -> dict[tuple[int, str], np.ndarray]:
    values: dict[tuple[int, str], list[np.ndarray]] = defaultdict(list)
    for row in rows:
        if exclude_trace is not None and str(row["trace_id"]) == exclude_trace:
            continue
        values[(int(row["n_cores"]), str(row["workload"]))].append(
            np.asarray(row["signature"], dtype=np.float64)
        )
    return {key: np.mean(items, axis=0) for key, items in values.items()}


def _classify(
    queries: Sequence[Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
    *,
    map_heldout_to_base: bool,
    leave_trace_out: bool,
    same_core: bool,
) -> dict[str, Any]:
    details = []
    correct = 0
    ranks = []
    for query in queries:
        centroids = _centroids(
            references,
            exclude_trace=str(query["trace_id"]) if leave_trace_out else None,
        )
        signature = query["signature"]
        core = int(query["n_cores"])
        if same_core:
            candidates = sorted(
                (_distance(signature, centroid), workload)
                for (candidate_core, workload), centroid in centroids.items()
                if candidate_core == core
            )
        else:
            by_workload: dict[str, list[np.ndarray]] = defaultdict(list)
            for (_candidate_core, workload), centroid in centroids.items():
                by_workload[workload].append(centroid)
            candidates = sorted(
                (_distance(signature, np.mean(items, axis=0)), workload)
                for workload, items in by_workload.items()
            )
        expected = (
            _base_name(str(query["workload"]))
            if map_heldout_to_base else str(query["workload"])
        )
        predicted = candidates[0][1] if candidates else None
        rank = next(
            (index + 1 for index, (_value, name) in enumerate(candidates)
             if name == expected),
            None,
        )
        is_correct = predicted == expected
        correct += int(is_correct)
        if rank is not None:
            ranks.append(rank)
        details.append({
            "trace_id": str(query["trace_id"]),
            "n_cores": core,
            "workload": str(query["workload"]),
            "expected_base": expected,
            "predicted_base": predicted,
            "correct": is_correct,
            "expected_rank": rank,
            "nearest_distance": candidates[0][0] if candidates else None,
            "expected_distance": (
                candidates[rank - 1][0] if rank is not None else None
            ),
            "top3": [
                {"workload": name, "distance": value}
                for value, name in candidates[:3]
            ],
        })
    return {
        "queries": len(details),
        "top1_accuracy": correct / max(1, len(details)),
        "mean_expected_rank": float(np.mean(ranks)) if ranks else None,
        "details": details,
    }


def _group_shift(
    train: Sequence[Mapping[str, Any]], evaluation: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    train_groups: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    eval_groups: dict[tuple[int, str], list[Mapping[str, Any]]] = defaultdict(list)
    for row in train:
        train_groups[(int(row["n_cores"]), str(row["workload"]))].append(row)
    for row in evaluation:
        eval_groups[(int(row["n_cores"]), _base_name(str(row["workload"])))].append(row)
    output = []
    for key, eval_rows in sorted(eval_groups.items()):
        if key not in train_groups:
            continue
        train_aggregate = _aggregate(train_groups[key])
        eval_aggregate = _aggregate(eval_rows)
        output.append({
            "n_cores": key[0],
            "base_workload": key[1],
            "heldout_workloads": sorted({str(row["workload"]) for row in eval_rows}),
            "js_divergence_bits": _js_divergence(
                np.asarray(train_aggregate["combination_counts"]),
                np.asarray(eval_aggregate["combination_counts"]),
            ),
            "train_full_miss_rate": train_aggregate["field_rates"][0],
            "eval_full_miss_rate": eval_aggregate["field_rates"][0],
            "full_miss_rate_delta": (
                eval_aggregate["field_rates"][0]
                - train_aggregate["field_rates"][0]
            ),
        })
    return output


def _markdown(report: Mapping[str, Any]) -> str:
    train = report["aggregate"]["train"]
    evaluation = report["aggregate"]["evaluation"]
    support = report["support"]
    heldout = report["classification"]["heldout_to_base"]
    train_class = report["classification"]["train_leave_trace_out"]
    lines = [
        "# v30 B2 branch-event distribution audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Train/evaluation traces: `{train['traces']}/{evaluation['traces']}`",
        f"- Train/evaluation branches: `{train['branches']:,}/{evaluation['branches']:,}`",
        f"- Joint-state JS divergence: `{report['shift']['pooled_js_divergence_bits']:.6f}` bits",
        f"- Evaluation events in unseen train states: `{100.0 * support['unseen_event_fraction']:.6f}%`",
        f"- Train leave-trace-out workload top-1: `{100.0 * train_class['top1_accuracy']:.2f}%`",
        f"- Heldout -> matching base workload top-1: `{100.0 * heldout['top1_accuracy']:.2f}%`",
        "",
        "## Marginal event rates",
        "",
        "| field | train | evaluation | delta |",
        "|---|---:|---:|---:|",
    ]
    for index, name in enumerate(EVENT_FIELDS):
        left = float(train["field_rates"][index])
        right = float(evaluation["field_rates"][index])
        lines.append(
            f"| `{name}` | {100*left:.4f}% | {100*right:.4f}% | "
            f"{100*(right-left):+.4f} pp |"
        )
    lines.extend([
        "",
        "## Base versus heldout family shift",
        "",
        "| cores | base workload | JS bits | train miss | heldout miss | delta |",
        "|---:|---|---:|---:|---:|---:|",
    ])
    for row in report["shift"]["by_family_core"]:
        lines.append(
            f"| {row['n_cores']} | `{row['base_workload']}` | "
            f"{row['js_divergence_bits']:.6f} | "
            f"{100*row['train_full_miss_rate']:.3f}% | "
            f"{100*row['eval_full_miss_rate']:.3f}% | "
            f"{100*row['full_miss_rate_delta']:+.3f} pp |"
        )
    lines.extend([
        "",
        "## Interpretation",
        "",
        "- High train workload-classification accuracy means B2 events are a strong workload fingerprint and therefore a shortcut risk.",
        "- Low heldout-to-base accuracy means heldout branch-event histograms shift away from their matching base workload; this provides separability, but it may encode workload-version identity rather than a transferable timing mechanism.",
        "- Support overlap is necessary but not sufficient: a state can be seen in train while its timing penalty changes with ILP, memory pressure, or recovery exposure.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=os.path.join(REPO_ROOT, "data/v30_branch_replay_dataset/manifest.json"),
    )
    parser.add_argument("--train-splits", default="train")
    parser.add_argument("--evaluation-splits", default="development_heldout")
    parser.add_argument(
        "--out",
        default=os.path.join(REPO_ROOT, "logs", f"v30_b2_no_train_distribution_{timestamp}"),
    )
    args = parser.parse_args()

    manifest_path = os.path.abspath(args.manifest)
    output = os.path.abspath(args.out)
    manifest = _load(manifest_path)
    train_rows = _records(manifest, _csv(args.train_splits))
    eval_rows = _records(manifest, _csv(args.evaluation_splits))
    if not train_rows or not eval_rows:
        raise RuntimeError("train/evaluation selection must both be non-empty")
    train = [_trace_summary(row) for row in train_rows]
    evaluation = [_trace_summary(row) for row in eval_rows]
    train_aggregate = _aggregate(train)
    eval_aggregate = _aggregate(evaluation)
    train_counts = np.asarray(train_aggregate["combination_counts"], dtype=np.int64)
    eval_counts = np.asarray(eval_aggregate["combination_counts"], dtype=np.int64)
    unseen = (train_counts == 0) & (eval_counts > 0)
    report = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
        "selection": {
            "manifest": manifest_path,
            "train_splits": list(_csv(args.train_splits)),
            "evaluation_splits": list(_csv(args.evaluation_splits)),
        },
        "event_fields": list(EVENT_FIELDS),
        "event_code": "full + 2*direction + 4*target + 8*cold",
        "aggregate": {
            "train": train_aggregate,
            "evaluation": eval_aggregate,
        },
        "support": {
            "train_seen_states": np.flatnonzero(train_counts > 0).tolist(),
            "evaluation_seen_states": np.flatnonzero(eval_counts > 0).tolist(),
            "evaluation_unseen_states": np.flatnonzero(unseen).tolist(),
            "unseen_event_count": int(eval_counts[unseen].sum()),
            "unseen_event_fraction": float(
                eval_counts[unseen].sum() / max(1, eval_counts.sum())
            ),
        },
        "shift": {
            "pooled_js_divergence_bits": _js_divergence(train_counts, eval_counts),
            "by_family_core": _group_shift(train, evaluation),
        },
        "classification": {
            "train_leave_trace_out": _classify(
                train, train,
                map_heldout_to_base=False,
                leave_trace_out=True,
                same_core=False,
            ),
            "heldout_to_base": _classify(
                evaluation, train,
                map_heldout_to_base=True,
                leave_trace_out=False,
                same_core=True,
            ),
        },
        "traces": {
            "train": train,
            "evaluation": evaluation,
        },
    }
    os.makedirs(output, exist_ok=True)
    json_path = os.path.join(output, "report.json")
    markdown_path = os.path.join(output, "report.md")
    _dump(json_path, report)
    with open(markdown_path, "w", encoding="utf-8") as handle:
        handle.write(_markdown(report))
    print(f"[v30-b2-distribution] json={json_path} markdown={markdown_path}")
    print(_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
