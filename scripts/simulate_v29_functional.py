#!/usr/bin/env python3
"""Simulate one label-free functional trace with a v29 checkpoint."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.io import dump_json
from tcsim.v29.dataset import V29FunctionalStore
from tcsim.v29.inference import load_checkpoint_runner, run_free_running


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ckpt", required=True)
    parser.add_argument("--functional-cache", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--amp-dtype", choices=("fp32", "bf16", "fp16"), default="bf16")
    parser.add_argument(
        "--sdpa-backend",
        choices=("auto", "flash", "no_flash", "efficient", "math"),
        default="",
    )
    parser.add_argument("--target-stride", type=int, default=None)
    parser.add_argument("--min-step-cycles", type=float, default=None)
    parser.add_argument("--max-step-cycles", type=float, default=None)
    parser.add_argument("--max-no-progress-steps", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=0)
    parser.add_argument("--no-static-cache", action="store_true")
    args = parser.parse_args()

    store = V29FunctionalStore(args.functional_cache)
    runner = load_checkpoint_runner(
        args.ckpt,
        device=args.device,
        amp_dtype=args.amp_dtype,
        sdpa_backend=args.sdpa_backend or None,
        static_cache=not args.no_static_cache,
    )
    scheduler = runner.config.scheduler
    report = run_free_running(
        store,
        runner,
        target_stride=int(
            args.target_stride
            if args.target_stride is not None else scheduler.get("target_stride", 32)
        ),
        min_step_cycles=float(
            args.min_step_cycles
            if args.min_step_cycles is not None else scheduler.get("min_step_cycles", 4.0)
        ),
        max_step_cycles=float(
            args.max_step_cycles
            if args.max_step_cycles is not None else scheduler.get("max_step_cycles", 1024.0)
        ),
        max_no_progress_steps=int(
            args.max_no_progress_steps
            if args.max_no_progress_steps is not None
            else scheduler.get("max_no_progress_steps", 64)
        ),
        max_steps=args.max_steps,
    )
    dump_json(args.out, report)
    print(
        json.dumps({
            "trace_id": report["trace_id"],
            "complete": report["complete"],
            "steps": report["steps"],
            "retired_uops": report["retired_uops"],
            "predicted_micro_cpi": report["predicted_micro_cpi"],
            "predicted_macro_cpi": report["predicted_macro_cpi"],
            "predicted_branch_misses": report["predicted_branch_misses"],
            "predicted_branch_miss_rate": report["predicted_branch_miss_rate"],
            "out": os.path.abspath(args.out),
        }, ensure_ascii=False, indent=2),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
