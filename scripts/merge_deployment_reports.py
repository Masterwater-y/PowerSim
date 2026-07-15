#!/usr/bin/env python3
"""Merge trace-level deployment reports emitted by independent GPU shards."""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.inference.deployment import aggregate_trace_reports, write_report  # noqa: E402
from tcsim.inference.reporting import write_deployment_text_report  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--text-out",
        help="TSim-style text report (default: report.txt beside --out)",
    )
    args = parser.parse_args()
    paths = []
    for pattern in args.inputs:
        paths.extend(glob.glob(pattern))
    paths = sorted(set(paths))
    if not paths:
        raise SystemExit("no shard reports matched")
    traces = []
    runs = []
    seen = set()
    for path in paths:
        with open(path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
        runs.append(report.get("run", {}))
        for row in report.get("traces", []):
            key = str(row.get("trace_id"))
            if key in seen:
                raise SystemExit(f"duplicate trace_id across shard reports: {key}")
            seen.add(key)
            traces.append(row)
    merged = aggregate_trace_reports(
        traces,
        run={
            "merged_shards": len(paths),
            "shard_reports": [os.path.abspath(path) for path in paths],
            "checkpoint": runs[0].get("checkpoint") if runs else None,
            "split": runs[0].get("split") if runs else None,
            "model_input_source": "packed_functional_chunks_only",
            "oracle_rollout_context_consumed": False,
            "prediction_latch": "new_chunk_exact_once",
        },
    )
    write_report(args.out, merged)
    text_out = args.text_out or os.path.join(
        os.path.dirname(os.path.abspath(args.out)), "report.txt"
    )
    write_deployment_text_report(text_out, merged, source=args.out)
    agg = merged["aggregate"]
    print(
        f"[merge] shards={len(paths)} traces={agg['n_traces']} "
        f"chunks={agg['n_chunks']} out={args.out} text_out={text_out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
