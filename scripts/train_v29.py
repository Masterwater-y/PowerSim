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
        history = source.get("long_history_dir")
        if history:
            source["long_history_dir"] = (
                history if os.path.isabs(history) else os.path.join(base, history)
            )
        branch_replay = source.get("branch_replay_dir")
        if branch_replay:
            source["branch_replay_dir"] = (
                branch_replay
                if os.path.isabs(branch_replay)
                else os.path.join(base, branch_replay)
            )
        out.append(source)
    return out


def _cache_path(source):
    return os.path.abspath(
        str(source.get("cache_dir")) if isinstance(source, dict) else str(source)
    )


def _validate_train_validation_separation(train_sources, validation_sources):
    train_by_path = {_cache_path(source): source for source in train_sources}
    validation_by_path = {_cache_path(source): source for source in validation_sources}
    for path in sorted(set(train_by_path) & set(validation_by_path)):
        train = train_by_path[path]
        validation = validation_by_path[path]
        if not isinstance(train, dict) or not isinstance(validation, dict):
            raise RuntimeError(f"v29 train/validation cache overlap without block policy: {path}")
        train_policy = dict(train.get("sample_split", {}) or {})
        validation_policy = dict(validation.get("sample_split", {}) or {})
        train_partition = train_policy.pop("partition", None)
        validation_partition = validation_policy.pop("partition", None)
        if not (
            train_partition == "train"
            and validation_partition == "validation"
            and train_policy == validation_policy
        ):
            raise RuntimeError(
                "v29 train/validation overlap is not a matching disjoint block split: "
                f"{path}"
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--cache-root")
    parser.add_argument(
        "--allow-unpartitioned-cache-root",
        action="store_true",
        help="diagnostic/smoke only; bypasses the non-leaky manifest split contract",
    )
    parser.add_argument("--train-split", default="train")
    parser.add_argument("--validation-split", default="validation")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--config", default=os.path.join(ROOT, "configs", "v29_100m.yaml"),
    )
    parser.add_argument("--device", default="auto")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--resume", default=None)
    parser.add_argument(
        "--init-checkpoint",
        default=None,
        help=(
            "initialize a fresh frozen_memory_probe from an E0 v29 checkpoint; "
            "unlike --resume, optimizer/step/history are not restored"
        ),
    )
    parser.add_argument(
        "--sdpa-backend",
        choices=("auto", "flash", "no_flash", "efficient", "math"),
        default=None,
    )
    parser.add_argument(
        "--amp-dtype", choices=("fp32", "bf16", "fp16"), default=None,
    )
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument(
        "--profile-attention", dest="profile_attention", action="store_true",
    )
    profile.add_argument(
        "--no-profile-attention", dest="profile_attention", action="store_false",
    )
    parser.set_defaults(profile_attention=None)
    args = parser.parse_args()

    config = TCSimConfig.load(args.config)
    if args.sdpa_backend is not None:
        config.model = {**config.model, "sdpa_backend": args.sdpa_backend}
    train_overrides = dict(config.train)
    if args.amp_dtype is not None:
        train_overrides["amp_dtype"] = args.amp_dtype
    if args.profile_attention is not None:
        train_overrides["profile_attention"] = bool(args.profile_attention)
    config.train = train_overrides
    if args.manifest:
        with open(args.manifest, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
        if manifest.get("schema_version") != "tcsim-v29-manifest-1":
            raise SystemExit("unsupported v29 manifest schema")
        if manifest.get("quality", {}).get("status") != "pass":
            raise SystemExit(
                "v29 manifest quality is not pass: "
                + "; ".join(map(str, manifest.get("quality", {}).get("blockers", [])))
            )
        train_sources = _manifest_sources(args.manifest, args.train_split)
        validation_sources = _manifest_sources(args.manifest, args.validation_split)
    else:
        if not args.allow_unpartitioned_cache_root:
            raise SystemExit(
                "--cache-root is unpartitioned and unsafe for formal training; "
                "use a pass-quality --manifest (or explicitly add "
                "--allow-unpartitioned-cache-root for a diagnostic smoke)"
            )
        train_sources = discover_trace_caches(args.cache_root)
        validation_sources = []
    if not train_sources:
        raise SystemExit("no v29 training caches")
    _validate_train_validation_separation(train_sources, validation_sources)
    result = train_one_run(
        train_sources,
        validation_sources,
        args.out,
        config,
        device=args.device,
        max_steps=args.max_steps,
        resume=args.resume,
        init_checkpoint=args.init_checkpoint,
    )
    if int(os.environ.get("RANK", "0")) == 0:
        print(f"[v29 train] {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
