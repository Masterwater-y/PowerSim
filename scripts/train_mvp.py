#!/usr/bin/env python
"""Phase 3 CLI: train the TCSim MVP model on prebuilt rollout dirs."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.train.loop import train_one_run
from tcsim.dataset.torch_dataset import discover_rollout_dirs
from tcsim.utils.config import TCSimConfig


def _manifest_dirs(path: str, split: str):
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    raw = manifest.get("splits", {}).get(split, manifest.get(split, []))
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for item in raw:
        value = item.get("rollout_dir") if isinstance(item, dict) else item
        if not value:
            continue
        rollout_dir = value if os.path.isabs(value) else os.path.join(base, value)
        if isinstance(item, dict) and item.get("sample_split"):
            out.append({
                "rollout_dir": rollout_dir,
                "sample_split": dict(item["sample_split"]),
            })
        else:
            out.append(rollout_dir)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--rollout_root", help="parent dir with per-trace subdirs")
    source.add_argument("--manifest", help="explicit trace-level split manifest")
    ap.add_argument("--train_split", default="train")
    ap.add_argument("--val_split", default="validation")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(ROOT, "configs", "mvp.yaml"))
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--max_steps", type=int, default=None)
    ap.add_argument("--epochs", type=int, default=None)
    ap.add_argument("--resume", default=None)
    args = ap.parse_args()

    cfg = TCSimConfig.load(args.config)
    if args.epochs is not None:
        cfg.train["epochs"] = args.epochs
    if args.manifest:
        train_dirs = _manifest_dirs(args.manifest, args.train_split)
        val_dirs = _manifest_dirs(args.manifest, args.val_split)
    else:
        # Never manufacture a leaky sample/random validation split.  With a
        # bare rollout root all traces train; use a manifest for validation.
        train_dirs = discover_rollout_dirs(args.rollout_root)
        val_dirs = []
    if not train_dirs:
        raise SystemExit("no training rollout directories found")
    rank = int(os.environ.get("RANK", "0"))
    if rank == 0:
        print(f"[train] train={len(train_dirs)} val={len(val_dirs)}")
    result = train_one_run(train_dirs, val_dirs, args.out, cfg,
                           device=args.device, max_steps=args.max_steps,
                           resume_path=args.resume)
    if rank == 0:
        print(f"[train] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
