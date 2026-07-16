#!/usr/bin/env python3
"""Train TCSim v29 from a v29 manifest or cache root."""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.config import TCSimConfig
from tcsim.v29.dataset import discover_trace_caches
from tcsim.v29.train import train_one_run


def _manifest_sources(path: str, split: str):
    with open(path, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    base = os.path.dirname(os.path.abspath(path))
    out = []
    for item in manifest.get("splits", {}).get(split, []):
        if not isinstance(item, dict):
            value = str(item)
            out.append(value if os.path.isabs(value) else os.path.join(base, value))
            continue
        value = item.get("cache_dir")
        if not value:
            continue
        source = dict(item)
        source["cache_dir"] = value if os.path.isabs(value) else os.path.join(base, value)
        out.append(source)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--cache-root")
    parser.add_argument("--train-split", default="train")
    parser.add_argument("--validation-split", default="validation")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--config", default=os.path.join(ROOT, "configs", "v29_100m.yaml"),
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--resume", default=None)
    args = parser.parse_args()

    config = TCSimConfig.load(args.config)
    if args.manifest:
        with open(args.manifest, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if manifest.get("quality", {}).get("status") != "pass":
            raise SystemExit(
                "v29 manifest quality is not pass: "
                + "; ".join(map(str, manifest.get("quality", {}).get("blockers", [])))
            )
        train_sources = _manifest_sources(args.manifest, args.train_split)
        validation_sources = _manifest_sources(args.manifest, args.validation_split)
    else:
        train_sources = discover_trace_caches(args.cache_root)
        validation_sources = []
    if not train_sources:
        raise SystemExit("no v29 training caches")
    result = train_one_run(
        train_sources,
        validation_sources,
        args.out,
        config,
        device=args.device,
        max_steps=args.max_steps,
        resume=args.resume,
    )
    if int(os.environ.get("RANK", "0")) == 0:
        print(f"[v29 train] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
