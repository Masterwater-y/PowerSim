#!/usr/bin/env python
"""End-to-end smoke test for TCSim.

Generates a tiny synthetic tao_trace, runs Phase 0→2 (chunk + rollout +
label join) for two workloads, then trains for a few steps and evaluates.
Expected wall time on CPU: ~15 s.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.dataset.synth import synth_default
from tcsim.dataset.rollout_builder import build_and_dump_trace, find_trace_dirs
from tcsim.utils.config import TCSimConfig
from tcsim.train.loop import train_one_run


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--epochs", type=int, default=2)
    args = ap.parse_args()

    tmp = args.workdir or tempfile.mkdtemp(prefix="tcsim_smoke_")
    print(f"[smoke] workdir = {tmp}")
    raw_root = os.path.join(tmp, "raw")
    cache_root = os.path.join(tmp, "cache")
    train_root = os.path.join(tmp, "runs", "smoke")

    print("[smoke] generating synthetic traces...")
    t0 = time.time()
    trace_dirs = synth_default(raw_root, n_uops=1024)
    print(f"[smoke]   wrote {len(trace_dirs)} trace dirs in {time.time()-t0:.2f}s")

    cfg = TCSimConfig.load(os.path.join(ROOT, "configs", "mvp.yaml"))
    cfg.train["epochs"] = args.epochs
    cfg.train["batch_samples"] = 4

    print("[smoke] building chunks + rollout for each trace...")
    per_dirs = []
    for td in trace_dirs:
        out_dir = os.path.join(cache_root, os.path.basename(os.path.dirname(td)))
        art = build_and_dump_trace(
            td,
            out_dir=out_dir,
            K=cfg.K,
            epsilon=cfg.epsilon,
            tick_per_cycle=500.0,  # synthetic generator uses 500 ticks/cycle
            max_forward_budget=int(cfg.scheduler.get("max_forward_budget", 4096)),
            max_resident_exposure=int(cfg.scheduler.get("max_resident_exposure", 0)),
        )
        per_dirs.append(out_dir)
        print(
            f"[smoke]   {out_dir}: chunks={art.n_chunks} samples={art.n_samples} "
            f"stats={art.stats}"
        )

    train_dirs = per_dirs[:1]
    val_dirs = per_dirs[1:]
    print(f"[smoke] training on {train_dirs}, validating on {val_dirs}")
    result = train_one_run(train_dirs, val_dirs, train_root, cfg, device="cpu")
    print(f"[smoke] train result = {result}")

    from tcsim.eval.evaluator import evaluate_dir
    best_ckpt = os.path.join(train_root, "best.pt")
    if not os.path.exists(best_ckpt):
        best_ckpt = os.path.join(train_root, "last.pt")
    report = evaluate_dir(
        best_ckpt,
        val_dirs or train_dirs,
        cfg,
        device="cpu",
        out_path=os.path.join(train_root, "eval_report.json"),
    )
    print("[smoke] eval report:")
    print(json.dumps({k: v for k, v in report.items() if k != "resident_exposure_bucket"}, indent=2))
    print(f"[smoke] resident_exposure_bucket = {report.get('resident_exposure_bucket', {})}")
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
