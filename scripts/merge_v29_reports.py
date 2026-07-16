#!/usr/bin/env python3
"""Merge independent v29 inference shards without pooled-metric shortcuts."""
from __future__ import annotations

import argparse
import glob
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.io import load_json
from tcsim.v29.inference import aggregate_trace_reports, write_evaluation_report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    paths = []
    for pattern in args.inputs:
        matches = glob.glob(pattern)
        paths.extend(matches if matches else [pattern])
    unique = {}
    runs = []
    failures = []
    for path in sorted(set(os.path.abspath(value) for value in paths)):
        report = load_json(path)
        if report.get("schema_version") != "tcsim-v29-evaluation-report-1":
            raise RuntimeError(f"not a v29 evaluation report: {path}")
        runs.append(report.get("run", {}))
        failures.extend(report.get("run", {}).get("failures", []))
        for row in report.get("traces", []):
            key = (
                str(row.get("checkpoint_id", "")),
                str(row.get("trace_id", "")),
                str(row.get("requested_mode", "")),
            )
            if key in unique and unique[key] != row:
                raise RuntimeError(f"conflicting duplicate trace report: {key}")
            unique[key] = row
    shared_run = {}
    if runs:
        for key in (
            "checkpoint", "checkpoint_id", "checkpoint_step", "manifest",
            "cache_root", "splits", "mode", "target_stride",
            "min_step_cycles_advisory", "max_step_cycles",
            "max_no_progress_steps", "evaluation_contract",
            "oracle_rollout_context_consumed", "predicted_context_used_for_labels",
        ):
            values = [run.get(key) for run in runs]
            if any(value != values[0] for value in values[1:]):
                raise RuntimeError(f"incompatible v29 shard run metadata for {key}")
            shared_run[key] = values[0]
    merged = aggregate_trace_reports(
        list(unique.values()),
        run={
            **shared_run,
            "merged_shards": len(runs),
            "source_reports": sorted(set(os.path.abspath(value) for value in paths)),
            "failures": failures,
            "oracle_rollout_context_consumed": False,
            "predicted_context_used_for_labels": False,
        },
    )
    outputs = write_evaluation_report(args.out, merged)
    print(
        f"[v29 merge] shards={len(runs)} traces={len(unique)} "
        f"failures={len(failures)} json={outputs['json']} text={outputs['text']}",
        flush=True,
    )
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
