#!/usr/bin/env python3
"""Build a label-free v29 deployment cache from functional parquet streams."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.v29.builder import build_functional_trace_cache


def _floats(value: str):
    return [float(part.strip()) for part in value.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-dir", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--horizons", default="16,32,64,128,256,512,1024")
    parser.add_argument("--sample-period-cycles", type=float, default=64.0)
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    artifact = build_functional_trace_cache(
        args.trace_dir,
        args.out,
        horizons=_floats(args.horizons),
        sample_period_cycles=args.sample_period_cycles,
        trace_id=args.trace_id or None,
        overwrite=args.overwrite,
    )
    print(
        f"[v29 functional build] trace={artifact.trace_id} "
        f"cores={artifact.n_cores} uops={artifact.n_uops} out={artifact.out_dir}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
