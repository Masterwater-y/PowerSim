#!/usr/bin/env python3
"""Build compact prefix-only long-history sidecars for a v29 manifest."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
import shutil
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.io import dump_json, load_json
from tcsim.v29.long_history import (
    LONG_HISTORY_BASE_FEATURE_NAMES,
    LONG_HISTORY_CHECKPOINT_STRIDE,
    build_core_features,
    contract_metadata,
    validate_sidecar_metadata,
)


def _source_path(path: str, manifest_dir: str) -> str:
    return os.path.abspath(
        path if os.path.isabs(path) else os.path.join(manifest_dir, path)
    )


def _safe_trace_path(trace_id: str) -> str:
    parts = [
        part for part in str(trace_id).replace("\\", "/").split("/")
        if part not in {"", ".", ".."}
    ]
    if not parts:
        raise ValueError(f"invalid trace_id {trace_id!r}")
    return os.path.join(*parts)


def _sidecar_dir(output_root: str, trace_id: str) -> str:
    return os.path.join(output_root, "traces", _safe_trace_path(trace_id))


def _metadata_matches(
    sidecar_dir: str,
    base_meta: Mapping[str, Any],
) -> bool:
    try:
        metadata = load_json(os.path.join(sidecar_dir, "meta.json"))
        core_ids = [int(value) for value in base_meta["core_ids"]]
        core_uops = {
            int(item["core_id"]): int(item["n_uops"])
            for item in base_meta["cores"]
        }
        validate_sidecar_metadata(
            metadata,
            trace_id=str(base_meta["trace_id"]),
            core_ids=core_ids,
            core_uops=core_uops,
        )
        for core_id in core_ids:
            checkpoints = np.load(
                os.path.join(sidecar_dir, "cores", str(core_id), "checkpoints.npy"),
                mmap_mode="r",
            )
            features = np.load(
                os.path.join(sidecar_dir, "cores", str(core_id), "features.npy"),
                mmap_mode="r",
            )
            if (
                checkpoints.ndim != 1
                or features.shape != (
                    len(checkpoints), len(LONG_HISTORY_BASE_FEATURE_NAMES)
                )
                or int(checkpoints[0]) != 0
                or int(checkpoints[-1]) != core_uops[core_id]
            ):
                return False
        return True
    except Exception:
        return False


def _build_one(args: Tuple[str, str, bool]) -> Dict[str, Any]:
    cache_dir, sidecar_dir, overwrite = args
    started = time.time()
    base_meta = load_json(os.path.join(cache_dir, "meta.json"))
    if not overwrite and _metadata_matches(sidecar_dir, base_meta):
        metadata = load_json(os.path.join(sidecar_dir, "meta.json"))
        return {
            "cache_dir": cache_dir,
            "long_history_dir": sidecar_dir,
            "trace_id": str(base_meta["trace_id"]),
            "status": "reused",
            "elapsed_s": time.time() - started,
            "storage_bytes": int(metadata.get("storage_bytes", 0)),
        }

    suffix = hashlib.sha256(
        f"{os.getpid()}:{time.time_ns()}:{sidecar_dir}".encode("utf-8")
    ).hexdigest()[:12]
    temporary = f"{sidecar_dir}.tmp-{suffix}"
    if os.path.exists(temporary):
        shutil.rmtree(temporary)
    os.makedirs(os.path.join(temporary, "cores"), exist_ok=False)
    core_ids = [int(value) for value in base_meta["core_ids"]]
    core_uops = {
        int(item["core_id"]): int(item["n_uops"])
        for item in base_meta["cores"]
    }
    dtlb_entries = int(
        base_meta.get("uarch_profile", {})
        .get("tlb", {})
        .get("dtlb", {})
        .get("entries", 64)
    )
    total_bytes = 0
    try:
        for core_id in core_ids:
            base_core = os.path.join(cache_dir, "cores", str(core_id))
            n_uops = core_uops[core_id]
            lines = np.load(
                os.path.join(base_core, "functional_line.npy"), mmap_mode="r",
            )
            pages = np.load(
                os.path.join(base_core, "functional_page.npy"), mmap_mode="r",
            )
            checkpoints, features = build_core_features(
                lines,
                pages,
                n_uops=n_uops,
                dtlb_entries=dtlb_entries,
                checkpoint_stride=LONG_HISTORY_CHECKPOINT_STRIDE,
            )
            core_out = os.path.join(temporary, "cores", str(core_id))
            os.makedirs(core_out, exist_ok=False)
            np.save(os.path.join(core_out, "checkpoints.npy"), checkpoints)
            np.save(os.path.join(core_out, "features.npy"), features)
            total_bytes += int(checkpoints.nbytes + features.nbytes)

        metadata = {
            **contract_metadata(),
            "trace_id": str(base_meta["trace_id"]),
            "core_ids": core_ids,
            "core_uops": {str(key): value for key, value in core_uops.items()},
            "base_contract": {
                key: base_meta.get(key)
                for key in (
                    "raw_trace_schema",
                    "dataset_schema",
                    "feature_schema",
                    "model_input_contract",
                    "predictor_hash",
                    "resource_decoder_hash",
                )
            },
            "quality": {
                "status": "pass",
                "strict_prefix": True,
                "future_uops_consumed": 0,
            },
            "storage_bytes": total_bytes,
        }
        dump_json(os.path.join(temporary, "meta.json"), metadata)
        os.makedirs(os.path.dirname(sidecar_dir), exist_ok=True)
        if os.path.exists(sidecar_dir):
            if not overwrite:
                raise RuntimeError(
                    f"invalid sidecar exists; rerun with --overwrite: {sidecar_dir}"
                )
            shutil.rmtree(sidecar_dir)
        os.replace(temporary, sidecar_dir)
    except Exception:
        if os.path.exists(temporary):
            shutil.rmtree(temporary)
        raise
    return {
        "cache_dir": cache_dir,
        "long_history_dir": sidecar_dir,
        "trace_id": str(base_meta["trace_id"]),
        "status": "built",
        "elapsed_s": time.time() - started,
        "storage_bytes": total_bytes,
    }


def _selected_records(
    manifest: Mapping[str, Any],
    split_names: Sequence[str],
) -> Iterable[Mapping[str, Any]]:
    for split in split_names:
        records = manifest.get("splits", {}).get(split)
        if records is None:
            raise ValueError(f"manifest has no split {split!r}")
        for record in records:
            if not isinstance(record, Mapping) or not record.get("cache_dir"):
                raise ValueError(f"long-history split {split!r} has invalid record")
            yield record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        default=os.path.join(ROOT, "data", "v29_global_time_dataset", "manifest.json"),
    )
    parser.add_argument(
        "--out",
        default=os.path.join(ROOT, "data", "v29_long_history_dataset"),
    )
    parser.add_argument("--splits", default="train,validation")
    parser.add_argument(
        "--workers", type=int, default=min(16, max(1, (os.cpu_count() or 8) // 4)),
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    manifest_path = os.path.abspath(args.manifest)
    output_root = os.path.abspath(args.out)
    manifest_dir = os.path.dirname(manifest_path)
    manifest = load_json(manifest_path)
    if manifest.get("schema_version") != "tcsim-v29-manifest-1":
        raise SystemExit("unsupported base v29 manifest")
    if manifest.get("quality", {}).get("status") != "pass":
        raise SystemExit("base v29 manifest quality is not pass")
    split_names = tuple(
        value.strip() for value in args.splits.split(",") if value.strip()
    )
    if not split_names:
        raise SystemExit("--splits must not be empty")

    records_by_cache: Dict[str, Mapping[str, Any]] = {}
    for record in _selected_records(manifest, split_names):
        cache_dir = _source_path(str(record["cache_dir"]), manifest_dir)
        records_by_cache.setdefault(cache_dir, record)
    jobs = []
    sidecar_by_cache: Dict[str, str] = {}
    for cache_dir, record in sorted(records_by_cache.items()):
        trace_id = str(record.get("trace_id") or load_json(
            os.path.join(cache_dir, "meta.json")
        )["trace_id"])
        sidecar = _sidecar_dir(output_root, trace_id)
        sidecar_by_cache[cache_dir] = sidecar
        jobs.append((cache_dir, sidecar, bool(args.overwrite)))

    os.makedirs(output_root, exist_ok=True)
    print(
        f"[v29-long-cache] unique_traces={len(jobs)} workers={args.workers} "
        f"splits={','.join(split_names)} out={output_root}",
        flush=True,
    )
    started = time.time()
    results: List[Dict[str, Any]] = []
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=max(1, int(args.workers)),
    ) as executor:
        future_by_cache = {
            executor.submit(_build_one, job): job[0] for job in jobs
        }
        for completed, future in enumerate(
            concurrent.futures.as_completed(future_by_cache), start=1,
        ):
            cache_dir = future_by_cache[future]
            try:
                result = future.result()
            except Exception as exc:
                print(
                    f"[v29-long-cache][ERROR] {cache_dir}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            results.append(result)
            print(
                f"[v29-long-cache {completed}/{len(jobs)}] "
                f"{result['status']} {result['trace_id']} "
                f"{result['elapsed_s']:.1f}s",
                flush=True,
            )

    derived = copy.deepcopy(manifest)
    derived["long_history"] = {
        **contract_metadata(),
        "base_manifest": manifest_path,
        "materialized_splits": list(split_names),
        "unique_traces": len(jobs),
        "quality": {
            "status": "pass",
            "strict_prefix": True,
            "future_uops_consumed": 0,
        },
    }
    for split, records in derived.get("splits", {}).items():
        if split not in split_names:
            continue
        for record in records:
            cache_dir = _source_path(str(record["cache_dir"]), manifest_dir)
            record["cache_dir"] = cache_dir
            record["long_history_dir"] = sidecar_by_cache[cache_dir]
    derived_path = os.path.join(output_root, "manifest.json")
    dump_json(derived_path, derived)
    report = {
        "status": "pass",
        "manifest": derived_path,
        "unique_traces": len(jobs),
        "built": sum(item["status"] == "built" for item in results),
        "reused": sum(item["status"] == "reused" for item in results),
        "storage_bytes": sum(int(item.get("storage_bytes", 0)) for item in results),
        "elapsed_s": time.time() - started,
        "results": sorted(results, key=lambda item: item["trace_id"]),
    }
    dump_json(os.path.join(output_root, "build_report.json"), report)
    print(
        f"[v29-long-cache] PASS manifest={derived_path} "
        f"elapsed={report['elapsed_s']:.1f}s "
        f"new_storage={report['storage_bytes'] / (1024 ** 3):.3f}GiB",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
