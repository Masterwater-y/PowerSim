#!/usr/bin/env python
"""Phase 2 CLI: chunk + run epsilon scheduler + dump rollout for a set of traces."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.dataset.rollout_builder import build_and_dump_trace, find_trace_dirs
from tcsim.utils.config import TCSimConfig


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--workloads", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(ROOT, "configs", "mvp.yaml"))
    ap.add_argument("--epsilon", type=float, default=None)
    ap.add_argument("--K", type=int, default=None)
    ap.add_argument("--budget", type=int, default=None)
    args = ap.parse_args()

    cfg = TCSimConfig.load(args.config)
    K = args.K or cfg.K
    epsilon = args.epsilon if args.epsilon is not None else cfg.epsilon
    raw_budget = args.budget if args.budget is not None else cfg.scheduler.get("max_forward_budget", 0)
    budget = None if raw_budget is None or int(raw_budget) <= 0 else int(raw_budget)
    raw_tpc = cfg.uarch.get("tick_per_cycle", "auto")
    tpc = None if str(raw_tpc).lower() == "auto" else float(raw_tpc)

    wl = [x for x in args.workloads.split(",") if x] or None
    dirs = find_trace_dirs(args.raw, wl)
    if not dirs:
        print(f"[rollout] no traces under {args.raw}")
        return 1
    for td in dirs:
        workload = os.path.basename(os.path.dirname(td))
        raw_root = os.path.basename(os.path.abspath(args.raw).rstrip(os.sep))
        sub = os.path.join(args.out, raw_root, workload)
        art = build_and_dump_trace(
            td, out_dir=sub,
            K=K, epsilon=epsilon,
            tick_per_cycle=tpc,
            max_forward_budget=budget,
            max_resident_exposure=int(cfg.scheduler.get("max_resident_exposure", 0)),
            trace_id=None,
        )
        print(
            f"[rollout] {raw_root}/{workload}: chunks={art.n_chunks} samples={art.n_samples} "
            f"stats={art.stats} -> {sub}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
