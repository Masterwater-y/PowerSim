#!/usr/bin/env python
"""Eval CLI."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.eval.evaluator import evaluate_dir
from tcsim.utils.config import TCSimConfig
from tcsim.dataset.torch_dataset import discover_rollout_dirs


def _manifest_dirs(path: str, split: str):
    with open(path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for item in manifest.get("splits", {}).get(split, []):
        value = item.get("rollout_dir") if isinstance(item, dict) else item
        if value:
            out.append(value if os.path.isabs(value) else os.path.join(base, value))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--rollout_root")
    source.add_argument("--manifest")
    ap.add_argument("--split", default="test_mechanism")
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", default=os.path.join(ROOT, "configs", "mvp.yaml"))
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()

    cfg = TCSimConfig.load(args.config)
    sub = (
        _manifest_dirs(args.manifest, args.split)
        if args.manifest else discover_rollout_dirs(args.rollout_root)
    )
    if not sub:
        raise SystemExit("no rollout directories found for evaluation")
    report = evaluate_dir(args.ckpt, sub, cfg, device=args.device, out_path=args.out)
    for k, v in report.items():
        if k == "resident_exposure_bucket":
            continue
        print(f"  {k}: {v}")
    print(f"[eval] wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
