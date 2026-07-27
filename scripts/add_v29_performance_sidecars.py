#!/usr/bin/env python3
"""Atomically add exact resource/macro performance sidecars to v29 caches."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, Iterable

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tcsim.v29.builder import (  # noqa: E402
    _build_performance_sidecars,
    _exact_compact_codes,
)
from tcsim.v29.contracts import (  # noqa: E402
    RESOURCE_COMPACT_INDEX,
    RESOURCE_KEY_INDEX,
)
from tcsim.v29.dataset import (  # noqa: E402
    FUNCTIONAL_CONTAINER_SCHEMA,
    V29FunctionalStore,
    V29TraceStore,
)


def _selected_caches(manifest: Dict[str, Any], splits: Iterable[str]):
    seen = set()
    for split in splits:
        rows = manifest.get("splits", {}).get(split)
        if not isinstance(rows, list):
            raise RuntimeError(f"unknown v29 split {split!r}")
        for row in rows:
            cache_dir = str(row.get("cache_dir", ""))
            if cache_dir and cache_dir not in seen:
                seen.add(cache_dir)
                yield Path(cache_dir).resolve()


def _link_inputs(cache_dir: Path, staging: Path, core_meta: list[dict]):
    cache_dir = cache_dir.resolve()
    staging = staging.resolve()
    (staging / "cores").mkdir(parents=True)
    for item in core_meta:
        core_id = int(item["core_id"])
        source = cache_dir / "cores" / str(core_id)
        target = staging / "cores" / str(core_id)
        target.mkdir()
        for name in ("resource.npy", "macro_pc.npy"):
            os.symlink(source / name, target / name)


def _verify_installed(cache_dir: Path, *, full: bool) -> None:
    """Fully verify every installed ID against authoritative source arrays."""
    metadata = json.loads((cache_dir / "meta.json").read_text(encoding="utf-8"))
    store_type = (
        V29FunctionalStore
        if metadata.get("container_schema") == FUNCTIONAL_CONTAINER_SCHEMA
        else V29TraceStore
    )
    store = store_type(str(cache_dir))
    if not full:
        return
    contract = dict(store.performance_sidecar_contract or {})
    radices = contract.get("resource_radices", {})
    specs = {
        "llc_set": ("llc_bank", "llc_set"),
        "dram_bank": ("dram_channel", "dram_rank", "dram_bank"),
        "dram_row": (
            "dram_channel", "dram_rank", "dram_bank", "dram_row",
        ),
    }
    for core_id in store.core_ids:
        arrays = store.cores[int(core_id)]
        expected_macro = store.macro_pc_table[
            np.asarray(arrays["macro_id"], dtype=np.int64)
        ]
        if not np.array_equal(expected_macro, arrays["macro_pc"]):
            raise RuntimeError(
                f"v29 full macro-ID verification failed core={core_id}"
            )
        resources = np.asarray(arrays["resource"], dtype=np.int64)
        compact = np.asarray(arrays["resource_compact"], dtype=np.uint32)
        for name, columns in specs.items():
            maxima = [int(value) - 1 for value in radices[name]]
            indices = [RESOURCE_KEY_INDEX[column] for column in columns]
            expected = _exact_compact_codes(resources[:, indices], maxima)
            actual = compact[:, RESOURCE_COMPACT_INDEX[name]]
            if not np.array_equal(expected, actual):
                raise RuntimeError(
                    "v29 full resource-ID verification failed "
                    f"core={core_id} resource={name}"
                )


def _install_one(
    cache_dir: Path,
    *,
    force: bool,
    full_reverify_existing: bool = False,
) -> None:
    cache_dir = cache_dir.resolve()
    meta_path = cache_dir / "meta.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    existing = dict(metadata.get("performance_sidecars", {}) or {})
    if existing and not force:
        _verify_installed(cache_dir, full=bool(full_reverify_existing))
        mode = "full" if full_reverify_existing else "contract+sampled"
        print(
            f"[v29 sidecar] reuse+verified({mode}) cache={cache_dir}",
            flush=True,
        )
        return
    core_meta = [dict(item) for item in metadata.get("cores", [])]
    if not core_meta:
        raise RuntimeError(f"v29 cache has no core metadata: {cache_dir}")
    staging = cache_dir.with_name(f"{cache_dir.name}.sidecars.tmp-{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    _link_inputs(cache_dir, staging, core_meta)
    try:
        contract = _build_performance_sidecars(str(staging), core_meta)
        for item in core_meta:
            core_id = int(item["core_id"])
            for name in ("resource_compact.npy", "macro_id.npy"):
                os.replace(
                    staging / "cores" / str(core_id) / name,
                    cache_dir / "cores" / str(core_id) / name,
                )
        os.replace(staging / "macro_pc_table.npy", cache_dir / "macro_pc_table.npy")
        updated = dict(metadata)
        updated["performance_sidecars"] = contract
        new_meta = cache_dir / f"meta.json.tmp-{os.getpid()}"
        new_meta.write_text(
            json.dumps(updated, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(new_meta, meta_path)
        _verify_installed(cache_dir, full=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    print(f"[v29 sidecar] PASS cache={cache_dir}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default=str(ROOT / "data/v29_global_time_dataset/manifest.json"),
    )
    parser.add_argument(
        "--splits",
        default=(
            "train,validation,development_heldout,seed0_inference,"
            "deployment_inference,final_untouched"
        ),
    )
    parser.add_argument("--cache-dir", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--full-reverify-existing",
        action="store_true",
        help="rescan every UOP even when an exact-built sidecar already exists",
    )
    args = parser.parse_args()

    caches = [Path(value).resolve() for value in args.cache_dir]
    if not caches:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        if manifest.get("schema_version") != "tcsim-v29-manifest-1":
            raise SystemExit("unsupported v29 manifest")
        splits = [value.strip() for value in args.splits.split(",") if value.strip()]
        caches = list(_selected_caches(manifest, splits))
    for index, cache_dir in enumerate(caches, 1):
        print(f"[v29 sidecar] {index}/{len(caches)} cache={cache_dir}", flush=True)
        _install_one(
            cache_dir,
            force=bool(args.force),
            full_reverify_existing=bool(args.full_reverify_existing),
        )
    print(f"[v29 sidecar] COMPLETE caches={len(caches)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
