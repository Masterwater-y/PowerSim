#!/usr/bin/env python3
"""Build functional-only v30 Exposure-v1 sidecars and an attached manifest."""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import glob
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from typing import Any, Mapping, Sequence

import numpy as np
import pyarrow.parquet as pq


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in os.sys.path:
    os.sys.path.insert(0, REPO_ROOT)

from tcsim.v29.dataset import V29TraceStore  # noqa: E402
from tcsim.v30.exposure import (  # noqa: E402
    EXPOSURE_CAUSAL_FIELDS,
    EXPOSURE_FIELDS,
    EXPOSURE_MAX_LOOKAHEAD,
    EXPOSURE_MAX_PRODUCERS,
    EXPOSURE_SCHEMA_VERSION,
    EXPOSURE_WINDOW_FIELDS,
    build_causal_exposure,
    exposure_distribution_summary,
)
from tcsim.v30.exposure_sidecar import EXPOSURE_SIDECAR_SCHEMA  # noqa: E402


FUNCTIONAL_COLUMNS = (
    "core_id", "producer_dists", "is_load", "is_store", "is_atomic",
)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _records(
    manifest: Mapping[str, Any],
    splits: Sequence[str],
    workload_pattern: str,
    core_counts: Sequence[int],
    max_traces: int,
) -> list[dict[str, Any]]:
    pattern = re.compile(workload_pattern) if workload_pattern else None
    allowed_cores = set(int(value) for value in core_counts)
    selected: dict[str, dict[str, Any]] = {}
    for split in splits:
        for raw in manifest.get("splits", {}).get(split, []):
            row = dict(raw)
            if pattern is not None and not pattern.search(str(row["workload"])):
                continue
            if allowed_cores and int(row["n_cores"]) not in allowed_cores:
                continue
            selected.setdefault(str(row["trace_id"]), row)
    rows = sorted(selected.values(), key=lambda row: str(row["trace_id"]))
    return rows[:max_traces] if max_traces > 0 else rows


def _producer_matrix(column: Any) -> np.ndarray:
    values = column.combine_chunks()
    flat = values.values.to_numpy(zero_copy_only=False)
    return np.asarray(flat, dtype=np.uint32).reshape(
        len(values), EXPOSURE_MAX_PRODUCERS,
    )


def _column_numpy(table: Any, name: str, dtype: Any) -> np.ndarray:
    return table[name].combine_chunks().to_numpy(
        zero_copy_only=False,
    ).astype(dtype, copy=False)


def build_trace(row: Mapping[str, Any], output_dir: str) -> Mapping[str, Any]:
    started = time.perf_counter()
    store = V29TraceStore(str(row["cache_dir"]))
    paths = sorted(glob.glob(os.path.join(str(row["trace_dir"]), "*.aligned.parquet")))
    if len(paths) != len(store.core_ids):
        raise RuntimeError(
            f"aligned parquet/core mismatch {len(paths)} != {len(store.core_ids)}: "
            f"{row['trace_id']}"
        )
    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(os.path.join(output_dir, "meta.json")):
        raise RuntimeError(f"refusing to overwrite populated output {output_dir}")

    core_meta = []
    total_uops = 0
    weighted_nonzero = 0.0
    weighted_max = 0.0
    for path in paths:
        table = pq.read_table(path, columns=list(FUNCTIONAL_COLUMNS))
        core_values = _column_numpy(table, "core_id", np.int64)
        unique = np.unique(core_values)
        if len(unique) != 1:
            raise RuntimeError(f"parquet contains multiple core IDs: {path}")
        core_id = int(unique[0])
        if core_id not in store.cores:
            raise RuntimeError(f"unexpected exposure core {core_id}: {path}")
        producer = _producer_matrix(table["producer_dists"])
        load = _column_numpy(table, "is_load", np.uint8) > 0
        store_mask = _column_numpy(table, "is_store", np.uint8) > 0
        atomic = _column_numpy(table, "is_atomic", np.uint8) > 0
        memory = load | store_mask | atomic
        expected_access = np.asarray(store.cores[core_id]["access"], dtype=np.uint8)
        if len(producer) != len(expected_access):
            raise RuntimeError(f"raw/cache exposure UOP mismatch core={core_id}")
        if not np.array_equal(memory, expected_access > 0):
            mismatch = int(np.flatnonzero(memory != (expected_access > 0))[0])
            raise RuntimeError(
                f"raw/cache exposure memory mismatch core={core_id} uop={mismatch}"
            )
        causal = build_causal_exposure(producer, memory)
        finite, nonzero, maximum = exposure_distribution_summary(causal)
        if finite != 1.0 or maximum > 1.0 + 1.0e-6:
            raise RuntimeError(f"invalid exposure values core={core_id}")
        core_dir = os.path.join(output_dir, "cores", str(core_id))
        os.makedirs(core_dir, exist_ok=True)
        np.save(os.path.join(core_dir, "causal.npy"), causal.astype(np.float16))
        np.save(os.path.join(core_dir, "producer_distance.npy"), producer)
        total_uops += len(producer)
        weighted_nonzero += nonzero * causal.size
        weighted_max = max(weighted_max, maximum)
        core_meta.append({
            "core_id": core_id,
            "n_uops": int(len(producer)),
            "n_memory_uops": int(memory.sum()),
            "causal_nonzero_fraction": nonzero,
            "causal_max": maximum,
        })

    elapsed = time.perf_counter() - started
    contract = {
        "schema_version": EXPOSURE_SIDECAR_SCHEMA,
        "feature_schema": EXPOSURE_SCHEMA_VERSION,
        "trace_id": str(row["trace_id"]),
        "cache_dir": os.path.abspath(str(row["cache_dir"])),
        "max_lookahead": EXPOSURE_MAX_LOOKAHEAD,
        "max_producers": EXPOSURE_MAX_PRODUCERS,
        "causal_fields": list(EXPOSURE_CAUSAL_FIELDS),
        "window_fields": list(EXPOSURE_WINDOW_FIELDS),
        "output_fields": list(EXPOSURE_FIELDS),
        "causal_dtype": "float16",
        "producer_distance_dtype": "uint32",
        "consumer_features_are_window_local": True,
        "uses_timing_or_microarchitecture_oracle": False,
        "source_columns": list(FUNCTIONAL_COLUMNS),
        "cores": sorted(core_meta, key=lambda value: int(value["core_id"])),
        "n_uops": int(total_uops),
        "causal_nonzero_fraction": weighted_nonzero / max(
            1, total_uops * len(EXPOSURE_CAUSAL_FIELDS)
        ),
        "causal_max": weighted_max,
        "elapsed_seconds": elapsed,
        "uops_per_second": total_uops / max(elapsed, 1.0e-9),
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
    }
    payload = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    contract["contract_hash"] = hashlib.sha256(payload).hexdigest()
    _dump_json(os.path.join(output_dir, "meta.json"), contract)
    return contract


def _build_job(row: Mapping[str, Any], destination: str) -> tuple[str, Mapping[str, Any]]:
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".exposure-build-", dir=parent)
    try:
        report = build_trace(row, temporary)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return str(row["trace_id"]), report


def _write_derived_manifest(
    source: Mapping[str, Any], output_path: str, sidecar_root: str,
) -> None:
    derived = json.loads(json.dumps(source))
    attached = 0
    for rows in derived.get("splits", {}).values():
        for row in rows:
            if not isinstance(row, dict) or "trace_id" not in row:
                continue
            path = os.path.join(sidecar_root, "traces", str(row["trace_id"]))
            if not os.path.isfile(os.path.join(path, "meta.json")):
                continue
            row["exposure_sidecar_dir"] = path
            attached += 1
    derived["exposure_sidecars"] = {
        "schema_version": EXPOSURE_SIDECAR_SCHEMA,
        "feature_schema": EXPOSURE_SCHEMA_VERSION,
        "root": sidecar_root,
        "attached_split_rows": attached,
    }
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    _dump_json(os.path.abspath(output_path), derived)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default=os.path.join(
            REPO_ROOT, "data/v30_gss_ready_dataset/manifest.json",
        ),
    )
    parser.add_argument("--splits", default="train,validation")
    parser.add_argument("--workload-regex", default="")
    parser.add_argument("--core-counts", default="1,4,8,16,32")
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument(
        "--out-root", default=os.path.join(
            REPO_ROOT, "data/v30_exposure_v1_sidecars",
        ),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--write-manifest", default=os.path.join(
            REPO_ROOT, "data/v30_exposure_v1_dataset/manifest.json",
        ),
    )
    args = parser.parse_args()
    manifest_path = os.path.abspath(args.manifest)
    manifest = _load_json(manifest_path)
    rows = _records(
        manifest,
        _csv(args.splits),
        args.workload_regex,
        tuple(int(value) for value in _csv(args.core_counts)),
        args.max_traces,
    )
    if not rows:
        raise RuntimeError("no exposure traces selected")
    output_root = os.path.abspath(args.out_root)
    os.makedirs(output_root, exist_ok=True)
    if args.workers <= 0:
        raise ValueError("workers must be positive")

    reports = []
    pending = []
    for ordinal, row in enumerate(rows, start=1):
        destination = os.path.join(output_root, "traces", str(row["trace_id"]))
        if os.path.exists(destination):
            if not args.overwrite:
                report = _load_json(os.path.join(destination, "meta.json"))
                if (
                    report.get("feature_schema") != EXPOSURE_SCHEMA_VERSION
                    or str(report.get("trace_id")) != str(row["trace_id"])
                    or os.path.abspath(str(report.get("cache_dir", "")))
                    != os.path.abspath(str(row["cache_dir"]))
                ):
                    raise RuntimeError(f"existing exposure schema mismatch: {destination}")
                reports.append(report)
                print(
                    f"[v30-exposure] reuse {ordinal}/{len(rows)} {row['trace_id']}",
                    flush=True,
                )
                continue
            shutil.rmtree(destination)
        pending.append((row, destination))

    wall_started = time.perf_counter()
    if pending:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(args.workers, len(pending)),
        ) as executor:
            futures = {
                executor.submit(_build_job, row, destination): row
                for row, destination in pending
            }
            completed = len(reports)
            for future in concurrent.futures.as_completed(futures):
                trace_id, report = future.result()
                reports.append(report)
                completed += 1
                print(
                    f"[v30-exposure] built {completed}/{len(rows)} {trace_id} "
                    f"uops={report['n_uops']:,} "
                    f"rate={report['uops_per_second']:,.0f}/s",
                    flush=True,
                )
    elapsed = time.perf_counter() - wall_started
    reports.sort(key=lambda value: str(value["trace_id"]))
    summary = {
        "schema_version": "tcsim-v30-exposure-sidecar-set-1",
        "feature_schema": EXPOSURE_SCHEMA_VERSION,
        "manifest": manifest_path,
        "splits": list(_csv(args.splits)),
        "traces": len(reports),
        "n_uops": sum(int(value["n_uops"]) for value in reports),
        "wall_seconds": elapsed,
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
    }
    _dump_json(os.path.join(output_root, "summary.json"), summary)
    if args.write_manifest:
        _write_derived_manifest(manifest, args.write_manifest, output_root)
    print(
        f"[v30-exposure] complete traces={len(reports)} "
        f"uops={summary['n_uops']:,} wall={elapsed:.1f}s "
        f"manifest={os.path.abspath(args.write_manifest) if args.write_manifest else '-'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
