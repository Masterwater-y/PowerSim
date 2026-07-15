#!/usr/bin/env python
"""Phase 0 CLI: chunk a real tao_trace directory into chunks.parquet.

Usage:
    python scripts/build_chunks.py \
        --raw /path/to/raw_v27_ffatomic_seed0_c04 \
        --workloads W_chase_DRAM,W_stream_seq_DRAM \
        --out data/mvp_chunks \
        --K 256
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.chunker.fixed_chunk import build_trace, chunk_to_row, CHUNK_COLS, LABEL_COLS
from tcsim.dataset.rollout_builder import find_trace_dirs
from tcsim.utils.parquet import write_table


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--workloads", default="", help="comma-separated; empty = all")
    ap.add_argument("--out", required=True)
    ap.add_argument("--K", type=int, default=256)
    ap.add_argument("--tick_per_cycle", type=float, default=None,
                    help="override; default derives from uarch_profile.json")
    args = ap.parse_args()

    wl = [x for x in args.workloads.split(",") if x] or None
    dirs = find_trace_dirs(args.raw, wl)
    if not dirs:
        print(f"[build_chunks] no traces under {args.raw}")
        return 1
    os.makedirs(args.out, exist_ok=True)
    for td in dirs:
        workload = os.path.basename(os.path.dirname(td))
        raw_root = os.path.basename(os.path.abspath(args.raw).rstrip(os.sep))
        chunks, labels = build_trace(
            td, K=args.K, trace_id=None, tick_per_cycle=args.tick_per_cycle,
        )
        trace_id = chunks[0].trace_id if chunks else f"{raw_root}/{workload}"
        sub = os.path.join(args.out, raw_root, workload)
        os.makedirs(sub, exist_ok=True)
        write_table(os.path.join(sub, "chunks.parquet"),
                    [chunk_to_row(c) for c in chunks], schema_cols=CHUNK_COLS)
        write_table(os.path.join(sub, "labels.parquet"), labels, schema_cols=LABEL_COLS)
        print(f"[build_chunks] {trace_id}: {len(chunks)} chunks, {len(labels)} labels -> {sub}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
