#!/usr/bin/env python3
"""Replay the configured full Tournament BPU from functional trace only."""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tcsim.branch_replay import (  # noqa: E402
    ReplayConfig,
    attach_v29_meta_evaluation,
    discover_aligned_files,
    iter_aligned_events,
    replay_core_streams,
)
from tcsim.utils.io import dump_json  # noqa: E402


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trace-dir", required=True,
        help="directory containing current TCSim *.aligned.parquet files",
    )
    parser.add_argument(
        "--config-json",
        help="predictor JSON or v29 meta.json; defaults to trace config.ini",
    )
    parser.add_argument("--output", help="write JSON report to this path")
    parser.add_argument(
        "--evaluation-meta",
        help=(
            "optional v29 meta.json whose aggregate gem5 miss counts are "
            "joined only after replay"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _arguments()
    if args.config_json:
        with open(args.config_json, "r", encoding="utf-8") as handle:
            config_source = json.load(handle)
    else:
        # This reads only static configuration metadata.  It does not invoke,
        # link to or import gem5.
        from tcsim.v29.features import load_trace_profile

        config_source, _decoder = load_trace_profile(args.trace_dir)
    config = ReplayConfig.from_mapping(config_source)
    streams = [
        (core_id, iter_aligned_events(path))
        for core_id, path in discover_aligned_files(args.trace_dir)
    ]
    report = replay_core_streams(streams, config)
    if args.evaluation_meta:
        with open(args.evaluation_meta, "r", encoding="utf-8") as handle:
            attach_v29_meta_evaluation(report, json.load(handle))
    if args.output:
        dump_json(args.output, report)
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
