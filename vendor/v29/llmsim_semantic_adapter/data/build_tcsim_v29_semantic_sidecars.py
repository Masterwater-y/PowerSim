#!/usr/bin/env python3
"""Build versioned semantic-row mappings for TCSim v29 macro IDs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, Iterable


REPO = Path(__file__).resolve().parents[1]
TCSIM_ROOT = Path("/data00/yinhaolang/TCSim")
for path in (REPO, TCSIM_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from tcsim.v29.dataset import (  # noqa: E402
    FUNCTIONAL_CONTAINER_SCHEMA,
    V29FunctionalStore,
    V29TraceStore,
)
from train.macro_v29_dataset import CachedSemanticSource, MacroContractError  # noqa: E402
from train.tcsim_v29_semantic_dataset import _load_static_manifest  # noqa: E402
from train.tcsim_v29_semantic_sidecar import (  # noqa: E402
    build_semantic_id_sidecar,
)


def _sources(manifest: Dict[str, Any], splits: Iterable[str]):
    seen = set()
    for split in splits:
        rows = manifest.get("splits", {}).get(split)
        if not isinstance(rows, list):
            raise MacroContractError(f"unknown TCSim manifest split {split!r}")
        for row in rows:
            cache_dir = str(row.get("cache_dir", ""))
            if cache_dir and cache_dir not in seen:
                seen.add(cache_dir)
                yield cache_dir


def _load_store(cache_dir: str):
    metadata = json.loads(
        (Path(cache_dir) / "meta.json").read_text(encoding="utf-8")
    )
    store_type = (
        V29FunctionalStore
        if metadata.get("container_schema") == FUNCTIONAL_CONTAINER_SCHEMA
        else V29TraceStore
    )
    return store_type(cache_dir)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default="/data00/yinhaolang/TCSim/data/v29_global_time_dataset/manifest.json",
    )
    parser.add_argument("--semantic-cache-root", required=True)
    parser.add_argument("--static-manifest", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--splits",
        default=(
            "train,validation,development_heldout,seed0_inference,"
            "deployment_inference,final_untouched"
        ),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "tcsim-v29-manifest-1":
        raise SystemExit("unsupported TCSim v29 manifest")
    semantic_cache = CachedSemanticSource(args.semantic_cache_root)
    static_rows = _load_static_manifest(args.static_manifest)
    splits = [value.strip() for value in args.splits.split(",") if value.strip()]
    cache_dirs = list(_sources(manifest, splits))
    if not cache_dirs:
        raise SystemExit("no TCSim caches selected")
    built = 0
    for index, cache_dir in enumerate(cache_dirs, 1):
        store = _load_store(cache_dir)
        workload = str(store.meta.get("workload", ""))
        row = static_rows.get(workload.removeprefix("W_"))
        if row is None:
            raise MacroContractError(
                f"no static dictionary for workload {workload!r}"
            )
        output = build_semantic_id_sidecar(
            store,
            semantic_cache,
            str(Path(str(row["parquet"])).resolve()),
            args.out,
            force=bool(args.force),
        )
        built += 1
        print(
            f"[semantic sidecar] {index}/{len(cache_dirs)} "
            f"trace={store.trace_id} out={output}",
            flush=True,
        )
    print(f"[semantic sidecar] PASS traces={built} root={Path(args.out).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
