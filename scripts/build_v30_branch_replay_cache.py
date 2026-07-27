#!/usr/bin/env python3
"""Build immutable configured-branch-replay sidecars for B1--B3."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import os
import shutil
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.branch_replay import (  # noqa: E402
    REPLAY_CACHE_CONTRACT,
    ReplayConfig,
    discover_aligned_files,
    iter_aligned_events,
)
from tcsim.branch_replay.io import REPLAY_CACHE_ARRAY_NAMES  # noqa: E402
from tcsim.utils.io import dump_json, load_json  # noqa: E402
from tcsim.v29.branch_features import (  # noqa: E402
    BRANCH_COLD_PREFIX,
    BRANCH_EVENT_FILE,
    BRANCH_EVENT_NAMES,
    BRANCH_HISTORY_FILE,
    BRANCH_HISTORY_NAMES,
    BRANCH_INDEX_FILE,
    build_core_features,
    build_core_features_from_events,
    contract_metadata,
    validate_sidecar_metadata,
)
from tcsim.v29.contracts import FIELD_INDEX  # noqa: E402


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


def _core_uops(base_meta: Mapping[str, Any]) -> Dict[int, int]:
    return {
        int(item["core_id"]): int(item["n_uops"])
        for item in base_meta["cores"]
    }


def _metadata_matches(sidecar_dir: str, base_meta: Mapping[str, Any]) -> bool:
    try:
        metadata = load_json(os.path.join(sidecar_dir, "meta.json"))
        core_ids = [int(value) for value in base_meta["core_ids"]]
        core_uops = _core_uops(base_meta)
        validate_sidecar_metadata(
            metadata,
            base_meta=base_meta,
            core_ids=core_ids,
            core_uops=core_uops,
        )
        for core_id in core_ids:
            core_dir = os.path.join(sidecar_dir, "cores", str(core_id))
            event = np.load(os.path.join(core_dir, BRANCH_EVENT_FILE), mmap_mode="r")
            history = np.load(
                os.path.join(core_dir, BRANCH_HISTORY_FILE), mmap_mode="r",
            )
            indices = np.load(
                os.path.join(core_dir, BRANCH_INDEX_FILE), mmap_mode="r",
            )
            expected_branches = next(
                int(item["n_branches"])
                for item in base_meta["cores"]
                if int(item["core_id"]) == core_id
            )
            if event.dtype != np.uint8 or event.shape != (
                expected_branches, len(BRANCH_EVENT_NAMES),
            ):
                return False
            if history.dtype != np.uint8 or history.shape != (
                core_uops[core_id], len(BRANCH_HISTORY_NAMES),
            ):
                return False
            if indices.dtype != np.uint32 or indices.shape != (expected_branches,):
                return False
        return True
    except Exception:
        return False


def _base_arrays(cache_dir: str, core_id: int) -> Dict[str, Any]:
    core_dir = os.path.join(cache_dir, "cores", str(core_id))
    names = ("fields", "macro_pc", *REPLAY_CACHE_ARRAY_NAMES)
    missing = [
        name for name in names
        if not os.path.isfile(os.path.join(core_dir, name + ".npy"))
    ]
    if missing:
        raise RuntimeError(
            f"base cache lacks functional branch replay arrays core={core_id}: "
            + ", ".join(missing)
        )
    return {
        name: np.load(os.path.join(core_dir, name + ".npy"), mmap_mode="r")
        for name in names
    }


def _raw_events_and_indices(
    cache_dir: str,
    aligned_path: str,
    core_id: int,
) -> Tuple[np.ndarray, List[Any]]:
    core_dir = os.path.join(cache_dir, "cores", str(core_id))
    branch = np.load(os.path.join(core_dir, "branch.npy"), mmap_mode="r")
    fields = np.load(os.path.join(core_dir, "fields.npy"), mmap_mode="r")
    macro_pc = np.load(os.path.join(core_dir, "macro_pc.npy"), mmap_mode="r")
    indices = np.flatnonzero(np.asarray(branch, dtype=np.uint8)).astype(
        np.int64, copy=False,
    )
    events = list(iter_aligned_events(aligned_path))
    if len(events) != len(indices):
        raise RuntimeError(
            f"raw/cache branch count mismatch core={core_id}: "
            f"{len(events)} != {len(indices)}"
        )
    for ordinal, (index, event) in enumerate(zip(indices, events)):
        kind = int(fields[int(index), FIELD_INDEX["branch_kind"]])
        taken = int(fields[int(index), FIELD_INDEX["branch_taken"]]) == 2
        observed = (
            int(event.pc), bool(event.conditional), bool(event.indirect),
            bool(event.call), bool(event.return_), bool(event.taken),
        )
        expected = (
            int(macro_pc[int(index)]), bool(kind & 0x2), bool(kind & 0x4),
            bool(kind & 0x8), bool(kind & 0x10), taken,
        )
        if observed != expected:
            raise RuntimeError(
                "raw/cache functional branch alignment mismatch "
                f"core={core_id} ordinal={ordinal} uop={int(index)} "
                f"observed={observed} expected={expected}"
            )
    return indices, events


def _build_one(args: Tuple[str, str, str, bool, int]) -> Dict[str, Any]:
    cache_dir, trace_dir, sidecar_dir, overwrite, cold_prefix = args
    started = time.time()
    base_meta = load_json(os.path.join(cache_dir, "meta.json"))
    if not overwrite and _metadata_matches(sidecar_dir, base_meta):
        metadata = load_json(os.path.join(sidecar_dir, "meta.json"))
        return {
            "cache_dir": cache_dir,
            "branch_replay_dir": sidecar_dir,
            "trace_id": str(base_meta["trace_id"]),
            "status": "reused",
            "elapsed_s": time.time() - started,
            "storage_bytes": int(metadata.get("storage_bytes", 0)),
            "branches": int(metadata.get("branches", 0)),
            "replayed_misses": int(metadata.get("replayed_misses", 0)),
            "predictor_config_hash": str(metadata["predictor_config_hash"]),
        }

    replay_config = ReplayConfig.from_mapping(base_meta)
    suffix = hashlib.sha256(
        f"{os.getpid()}:{time.time_ns()}:{sidecar_dir}".encode("utf-8")
    ).hexdigest()[:12]
    temporary = f"{sidecar_dir}.tmp-{suffix}"
    if os.path.exists(temporary):
        shutil.rmtree(temporary)
    os.makedirs(os.path.join(temporary, "cores"), exist_ok=False)
    core_ids = [int(value) for value in base_meta["core_ids"]]
    core_uops = _core_uops(base_meta)
    total_bytes = 0
    total_branches = 0
    total_misses = 0
    core_reports: List[Dict[str, Any]] = []
    aligned_by_core = dict(discover_aligned_files(trace_dir))
    if set(aligned_by_core) != set(core_ids):
        raise RuntimeError(
            f"raw/cache core mismatch: raw={sorted(aligned_by_core)} "
            f"cache={sorted(core_ids)}"
        )
    compact_source = (
        base_meta.get("functional_branch_replay_contract")
        == REPLAY_CACHE_CONTRACT
    )
    try:
        for core_id in core_ids:
            if compact_source:
                base_arrays = _base_arrays(cache_dir, core_id)
                branch_indices = np.asarray(
                    base_arrays["replay_branch_index"], dtype=np.uint32,
                )
                event, history, report = build_core_features(
                    base_arrays,
                    replay_config,
                    n_uops=core_uops[core_id],
                    cold_prefix_branches=int(cold_prefix),
                )
            else:
                branch_indices, events = _raw_events_and_indices(
                    cache_dir, aligned_by_core[core_id], core_id,
                )
                event, history, report = build_core_features_from_events(
                    branch_indices,
                    events,
                    replay_config,
                    n_uops=core_uops[core_id],
                    cold_prefix_branches=int(cold_prefix),
                )
            if int(report["functional_history_mismatches"]) != 0:
                raise RuntimeError(
                    "functional branch history mismatch while building sidecar "
                    f"core={core_id}: {report['functional_history_mismatches']}"
                )
            core_out = os.path.join(temporary, "cores", str(core_id))
            os.makedirs(core_out, exist_ok=False)
            np.save(os.path.join(core_out, BRANCH_EVENT_FILE), event)
            np.save(os.path.join(core_out, BRANCH_HISTORY_FILE), history)
            branch_indices = np.asarray(branch_indices, dtype=np.uint32)
            np.save(os.path.join(core_out, BRANCH_INDEX_FILE), branch_indices)
            total_bytes += int(event.nbytes + history.nbytes + branch_indices.nbytes)
            total_branches += int(report["branches"])
            total_misses += int(report["replayed_misses"])
            core_reports.append({"core_id": core_id, **report})

        metadata = {
            **contract_metadata(
                replay_config,
                cold_prefix_branches=int(cold_prefix),
            ),
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
                    "functional_branch_replay_contract",
                )
            },
            "branches": total_branches,
            "replayed_misses": total_misses,
            "functional_source": (
                REPLAY_CACHE_CONTRACT if compact_source else "raw-aligned-functional"
            ),
            "core_reports": core_reports,
            "quality": {
                "status": "pass",
                "strict_prefix": True,
                "future_branch_events_consumed": 0,
                "functional_history_mismatches": 0,
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
        "branch_replay_dir": sidecar_dir,
        "trace_id": str(base_meta["trace_id"]),
        "status": "built",
        "elapsed_s": time.time() - started,
        "storage_bytes": total_bytes,
        "branches": total_branches,
        "replayed_misses": total_misses,
        "predictor_config_hash": str(metadata["predictor_config_hash"]),
    }


def _selected_records(
    manifest: Mapping[str, Any], split_names: Sequence[str],
) -> Iterable[Mapping[str, Any]]:
    for split in split_names:
        records = manifest.get("splits", {}).get(split)
        if records is None:
            raise ValueError(f"manifest has no split {split!r}")
        for record in records:
            if not isinstance(record, Mapping) or not record.get("cache_dir"):
                raise ValueError(f"branch replay split {split!r} has invalid record")
            yield record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=os.path.join(ROOT, "data", "v29_global_time_dataset", "manifest.json"),
    )
    parser.add_argument(
        "--out", default=os.path.join(ROOT, "data", "v30_branch_replay_dataset"),
    )
    parser.add_argument("--splits", default="train,validation")
    parser.add_argument(
        "--workers", type=int, default=min(16, max(1, (os.cpu_count() or 8) // 4)),
    )
    parser.add_argument("--cold-prefix-branches", type=int, default=BRANCH_COLD_PREFIX)
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
    if int(args.workers) <= 0 or int(args.cold_prefix_branches) < 0:
        raise SystemExit("workers must be positive and cold prefix non-negative")
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
        trace_dir_value = record.get("trace_dir")
        if not trace_dir_value:
            trace_dir_value = load_json(os.path.join(cache_dir, "meta.json")).get(
                "trace_dir"
            )
        if not trace_dir_value:
            raise RuntimeError(f"trace has no aligned functional source: {cache_dir}")
        trace_dir = _source_path(str(trace_dir_value), manifest_dir)
        jobs.append((
            cache_dir,
            trace_dir,
            sidecar,
            bool(args.overwrite),
            int(args.cold_prefix_branches),
        ))

    os.makedirs(output_root, exist_ok=True)
    print(
        f"[v30-branch-cache] unique_traces={len(jobs)} workers={args.workers} "
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
                    f"[v30-branch-cache][ERROR] {cache_dir}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            results.append(result)
            print(
                f"[v30-branch-cache {completed}/{len(jobs)}] "
                f"{result['status']} {result['trace_id']} "
                f"branches={result['branches']:,} "
                f"misses={result['replayed_misses']:,} "
                f"{result['elapsed_s']:.1f}s",
                flush=True,
            )

    predictor_config_hashes = sorted({
        str(item["predictor_config_hash"]) for item in results
    })
    if len(predictor_config_hashes) != 1:
        raise RuntimeError(
            "all traces in one v30 branch-replay dataset must use the same "
            "configured branch predictor; found hashes="
            f"{predictor_config_hashes}"
        )

    derived = copy.deepcopy(manifest)
    # Every selected trace uses the same predictor contract in the current
    # dataset; retain a representative contract at manifest level and let the
    # loader verify every trace independently.
    first_meta = load_json(os.path.join(jobs[0][0], "meta.json")) if jobs else None
    if first_meta is None:
        raise RuntimeError("branch cache build selected no traces")
    replay_config = ReplayConfig.from_mapping(first_meta)
    derived["branch_replay_features"] = {
        **contract_metadata(
            replay_config,
            cold_prefix_branches=int(args.cold_prefix_branches),
        ),
        "base_manifest": manifest_path,
        "materialized_splits": list(split_names),
        "unique_traces": len(jobs),
        "quality": {
            "status": "pass",
            "strict_prefix": True,
            "future_branch_events_consumed": 0,
        },
    }
    for split, records in derived.get("splits", {}).items():
        if split not in split_names:
            continue
        for record in records:
            cache_dir = _source_path(str(record["cache_dir"]), manifest_dir)
            record["cache_dir"] = cache_dir
            record["branch_replay_dir"] = sidecar_by_cache[cache_dir]
            history = record.get("long_history_dir")
            if history:
                record["long_history_dir"] = _source_path(
                    str(history), manifest_dir,
                )

    derived_path = os.path.join(output_root, "manifest.json")
    dump_json(derived_path, derived)
    report = {
        "status": "pass",
        "manifest": derived_path,
        "unique_traces": len(jobs),
        "built": sum(item["status"] == "built" for item in results),
        "reused": sum(item["status"] == "reused" for item in results),
        "storage_bytes": sum(int(item.get("storage_bytes", 0)) for item in results),
        "branches": sum(int(item.get("branches", 0)) for item in results),
        "replayed_misses": sum(
            int(item.get("replayed_misses", 0)) for item in results
        ),
        "predictor_config_hash": predictor_config_hashes[0],
        "elapsed_s": time.time() - started,
        "results": sorted(results, key=lambda item: item["trace_id"]),
    }
    dump_json(os.path.join(output_root, "build_report.json"), report)
    print(
        f"[v30-branch-cache] PASS manifest={derived_path} "
        f"elapsed={report['elapsed_s']:.1f}s "
        f"storage={report['storage_bytes'] / (1024 ** 3):.3f}GiB",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
