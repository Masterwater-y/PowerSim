#!/usr/bin/env python3
"""Compare the four same-checkpoint GSS deployment ablations."""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from typing import Any, Dict, Iterable, Mapping


MODES = ("gap0", "state-disabled", "predicted-order", "teacher-order")


def _load(path: str) -> Mapping[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, Mapping) or not isinstance(value.get("traces"), list):
        raise SystemExit(f"invalid evaluation report: {path}")
    return value


def _index(report: Mapping[str, Any], mode: str) -> Dict[str, Mapping[str, Any]]:
    result: Dict[str, Mapping[str, Any]] = {}
    for row in report["traces"]:
        contract = row.get("evaluation_contract", {})
        observed = contract.get("gss_ablation_mode")
        if observed != mode:
            raise SystemExit(
                f"report mode mismatch: expected {mode}, observed {observed}"
            )
        key = str(row["trace_id"])
        if key in result:
            raise SystemExit(f"duplicate trace in {mode}: {key}")
        result[key] = row
    return result


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / max(1, len(rows))


def main() -> int:
    parser = argparse.ArgumentParser()
    for mode in MODES:
        parser.add_argument(f"--{mode}", required=True)
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args()

    paths = {mode: getattr(args, mode.replace("-", "_")) for mode in MODES}
    reports = {mode: _load(path) for mode, path in paths.items()}
    indexed = {mode: _index(reports[mode], mode) for mode in MODES}
    trace_sets = {mode: set(rows) for mode, rows in indexed.items()}
    reference = trace_sets["gap0"]
    if any(values != reference for values in trace_sets.values()):
        counts = {mode: len(values) for mode, values in trace_sets.items()}
        raise SystemExit(f"ablation trace sets differ: {counts}")
    if not reference:
        raise SystemExit("ablation reports contain no traces")

    checkpoint_ids = {
        str(row.get("checkpoint_id", ""))
        for rows in indexed.values() for row in rows.values()
    }
    if len(checkpoint_ids) != 1:
        raise SystemExit("ablation reports do not use one checkpoint")

    rows = []
    for trace_id in sorted(reference):
        modes = {mode: indexed[mode][trace_id] for mode in MODES}
        identity = modes["gap0"]
        values = {}
        for mode, row in modes.items():
            free = row.get("free_running", {})
            if not free.get("complete"):
                raise SystemExit(f"incomplete {mode} rollout: {trace_id}")
            values[mode] = {
                "roi_cpi_error": float(free["roi_cpi_error"]),
                "pred_roi_cpi": float(free["pred_roi_cpi"]),
                "true_roi_cpi": float(free["true_roi_cpi"]),
                "elapsed_s": float(free["elapsed_s"]),
                "uops_per_s": float(free["uops_per_s"]),
            }
        rows.append({
            "trace_id": trace_id,
            "workload": str(identity.get("workload", "")),
            "n_cores": int(identity.get("n_cores", 0)),
            "seed": int(identity.get("seed", -1)),
            "modes": values,
            "contrasts": {
                "geometry_shortcut_vs_gap0": (
                    values["state-disabled"]["roi_cpi_error"]
                    - values["gap0"]["roi_cpi_error"]
                ),
                "online_state_vs_state_disabled": (
                    values["predicted-order"]["roi_cpi_error"]
                    - values["state-disabled"]["roi_cpi_error"]
                ),
                "teacher_order_vs_predicted_order": (
                    values["teacher-order"]["roi_cpi_error"]
                    - values["predicted-order"]["roi_cpi_error"]
                ),
                "teacher_state_vs_state_disabled": (
                    values["teacher-order"]["roi_cpi_error"]
                    - values["state-disabled"]["roi_cpi_error"]
                ),
            },
        })

    groups: Dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    groups["all"] = rows
    for row in rows:
        groups[row["workload"]].append(row)
    summaries = {}
    for name, group in groups.items():
        summaries[name] = {
            "n": len(group),
            "roi_cpi_error": {
                mode: _mean(
                    row["modes"][mode]["roi_cpi_error"] for row in group
                )
                for mode in MODES
            },
            "contrasts": {
                key: _mean(row["contrasts"][key] for row in group)
                for key in rows[0]["contrasts"]
            },
        }

    output = {
        "schema_version": "tcsim-v30-gss-same-ckpt-ablation-v1",
        "checkpoint_id": next(iter(checkpoint_ids)),
        "trace_count": len(rows),
        "modes": list(MODES),
        "summaries": summaries,
        "traces": rows,
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out_json)), exist_ok=True)
    with open(args.out_json, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    lines = [
        "# v30 GSS same-checkpoint targeted ablation",
        "",
        f"- traces: {len(rows)}",
        f"- checkpoint ID: `{next(iter(checkpoint_ids))}`",
        "- lower ROI-CPI error is better; contrast values are percentage-point changes",
        "",
        "| group | n | gap0 | state-disabled | predicted-order | teacher-order | online-state vs disabled | teacher vs predicted |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    order = ["all"] + sorted(name for name in summaries if name != "all")
    for name in order:
        summary = summaries[name]
        errors = summary["roi_cpi_error"]
        contrasts = summary["contrasts"]
        lines.append(
            f"| {name} | {summary['n']} "
            f"| {100.0 * errors['gap0']:.3f}% "
            f"| {100.0 * errors['state-disabled']:.3f}% "
            f"| {100.0 * errors['predicted-order']:.3f}% "
            f"| {100.0 * errors['teacher-order']:.3f}% "
            f"| {100.0 * contrasts['online_state_vs_state_disabled']:+.3f} pp "
            f"| {100.0 * contrasts['teacher_order_vs_predicted_order']:+.3f} pp |"
        )
    lines.extend((
        "",
        "Interpretation:",
        "",
        "- predicted-order minus state-disabled isolates deployable online cache-state content;",
        "- teacher-order minus predicted-order isolates event-order/closed-loop damage;",
        "- state-disabled minus gap0 exposes memory-geometry or adapter/router shortcuts;",
        "- teacher-order is an oracle attribution diagnostic, not a deployment result.",
        "",
    ))
    with open(args.out_md, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print(f"[gss-ablation] summary={args.out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
