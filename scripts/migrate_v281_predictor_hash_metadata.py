#!/usr/bin/env python3
"""Migrate v28.1 rollout metadata to the semantic predictor hash."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tcsim.chunker.functional_features import load_uarch_profile, predictor_hash


def _load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    manifest = _load(Path(args.manifest))
    sources: Dict[str, Dict[str, Any]] = {}
    for rows in manifest.get("splits", {}).values():
        for row in rows:
            rollout_dir = str(row.get("rollout_dir", ""))
            trace_dir = str(row.get("trace_dir", ""))
            if not rollout_dir or not trace_dir:
                continue
            previous = sources.setdefault(rollout_dir, row)
            if str(previous.get("trace_dir")) != trace_dir:
                raise RuntimeError(f"conflicting trace_dir for {rollout_dir}")

    changed = 0
    hashes = set()
    migrated_hashes: Dict[str, str] = {}
    for rollout_dir, source in sorted(sources.items()):
        meta_path = Path(rollout_dir) / "meta.json"
        meta = _load(meta_path)
        old_hash = str(meta.get("predictor_hash", ""))
        cache_key = old_hash or str(source["trace_dir"])
        new_hash = migrated_hashes.get(cache_key)
        if new_hash is None:
            new_hash = predictor_hash(load_uarch_profile(str(source["trace_dir"])))
            migrated_hashes[cache_key] = new_hash
        hashes.add(new_hash)
        if old_hash != new_hash:
            changed += 1
            if args.write:
                meta["predictor_hash"] = new_hash
                _write_atomic(meta_path, meta)

    print(
        f"rollouts={len(sources)} changed={changed} write={args.write} "
        f"semantic_hashes={sorted(hashes)}"
    )
    if len(hashes) != 1:
        raise RuntimeError(
            f"expected one semantic predictor configuration, got {sorted(hashes)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
