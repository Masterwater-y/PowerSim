#!/usr/bin/env python3
"""Build causal cache-only v30 GSS sidecars from true timestamp order.

Each memory UOP receives the GSS state observed immediately before that access.
The cache transition is identical for all clock policies; only global event
ordering changes.  Formal v30 uses raw ``commit_tick`` because the deployed
model predicts commit cycles and the state machine must obey the same time
contract.  Ready/issue remain diagnostic-only options.  Timestamp values are
never written into model-visible arrays.
"""
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

from tcsim.v29.contracts import RESOURCE_KEY_INDEX  # noqa: E402
from tcsim.v29.dataset import V29TraceStore  # noqa: E402
from tcsim.v30.gss import (  # noqa: E402
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSS_SCHEMA_VERSION,
    GSSFeatureEngine,
    GSSGeometry,
)


SIDECAR_SCHEMA = "tcsim-v30-gss-teacher-sidecar-1"


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


def _clock_array(table: Any, clock: str) -> np.ndarray:
    if clock == "ready":
        return table["ready_tick"].to_numpy(zero_copy_only=False).astype(
            np.int64, copy=False,
        )
    if clock == "issue":
        fetch = table["fetch_tick"].to_numpy(zero_copy_only=False).astype(
            np.int64, copy=False,
        )
        delta = table["issue_tick"].to_numpy(zero_copy_only=False).astype(
            np.int64, copy=False,
        )
        return fetch + delta
    if clock == "commit":
        return table["commit_tick"].to_numpy(zero_copy_only=False).astype(
            np.int64, copy=False,
        )
    raise ValueError(f"unsupported clock {clock!r}")


def _raw_clocks(
    row: Mapping[str, Any], store: V29TraceStore, clock: str,
) -> dict[int, np.ndarray]:
    paths = sorted(glob.glob(os.path.join(str(row["trace_dir"]), "*.aligned.parquet")))
    if len(paths) != len(store.core_ids):
        raise RuntimeError(
            f"aligned parquet/core mismatch {len(paths)} != {len(store.core_ids)}: "
            f"{row['trace_id']}"
        )
    columns = ["core_id", "commit_tick"]
    if clock == "ready":
        columns.append("ready_tick")
    elif clock == "issue":
        columns.extend(("fetch_tick", "issue_tick"))
    output: dict[int, np.ndarray] = {}
    for path in paths:
        table = pq.read_table(path, columns=columns)
        core_values = table["core_id"].to_numpy(zero_copy_only=False)
        unique = np.unique(core_values)
        if len(unique) != 1:
            raise RuntimeError(f"parquet contains multiple core IDs: {path}")
        core = int(unique[0])
        if core not in store.cores or core in output:
            raise RuntimeError(f"unexpected/duplicate core {core}: {path}")
        cached_commit = np.asarray(store.cores[core]["commit_tick"], dtype=np.int64)
        raw_commit = table["commit_tick"].to_numpy(zero_copy_only=False).astype(
            np.int64, copy=False,
        )
        if not np.array_equal(raw_commit, cached_commit):
            mismatch = int(np.flatnonzero(raw_commit != cached_commit)[0])
            raise RuntimeError(
                f"raw/cache UOP alignment mismatch core={core} index={mismatch}"
            )
        values = _clock_array(table, clock)
        if len(values) != len(cached_commit):
            raise RuntimeError(f"clock length mismatch core={core}")
        output[core] = values
    return output


def build_trace(
    row: Mapping[str, Any], output_dir: str, *, clock: str,
) -> Mapping[str, Any]:
    started = time.perf_counter()
    store = V29TraceStore(str(row["cache_dir"]))
    geometry = GSSGeometry.from_trace_meta(store.meta)
    clocks = _raw_clocks(row, store, clock)
    core_memory: dict[int, dict[str, np.ndarray]] = {}
    total_uops = 0
    total_memory = 0
    total_valid = 0
    for core in store.core_ids:
        arrays = store.cores[core]
        access = np.asarray(arrays["access"], dtype=np.uint8)
        indices = np.flatnonzero(access > 0).astype(np.uint32, copy=False)
        resource = np.asarray(arrays["resource"][indices], dtype=np.int64)
        valid = resource[:, RESOURCE_KEY_INDEX["physical_line"]] >= 0
        total_uops += len(access)
        total_memory += len(indices)
        total_valid += int(valid.sum())
        if not bool(valid.all()):
            raise RuntimeError(
                f"phase-1 GSS requires physical address for every memory UOP: "
                f"trace={row['trace_id']} core={core} valid={valid.sum()}/{len(valid)}"
            )
        event_clock = np.asarray(clocks[core][indices], dtype=np.int64)
        if bool((event_clock <= 0).any()):
            raise RuntimeError(f"non-positive {clock} clock for memory UOP core={core}")
        core_memory[core] = {
            "index": indices,
            "access": access[indices].astype(np.uint8, copy=False),
            "resource": resource,
            "clock": event_clock,
            "categorical": np.zeros(
                (len(indices), len(GSS_CATEGORICAL_FIELDS)), dtype=np.uint8,
            ),
            "continuous": np.zeros(
                (len(indices), len(GSS_CONTINUOUS_FIELDS)), dtype=np.float32,
            ),
        }

    event_clock = np.concatenate([core_memory[c]["clock"] for c in store.core_ids])
    event_core = np.concatenate([
        np.full(len(core_memory[c]["index"]), c, dtype=np.int16)
        for c in store.core_ids
    ])
    event_ordinal = np.concatenate([
        np.arange(len(core_memory[c]["index"]), dtype=np.uint32)
        for c in store.core_ids
    ])
    event_uop = np.concatenate([core_memory[c]["index"] for c in store.core_ids])
    order = np.lexsort((event_uop, event_core, event_clock))
    engine = GSSFeatureEngine(geometry)
    for flat_index in order:
        core = int(event_core[flat_index])
        ordinal = int(event_ordinal[flat_index])
        values = core_memory[core]
        resource = values["resource"][ordinal]
        feature = engine.access(
            core=core,
            physical_line=int(resource[RESOURCE_KEY_INDEX["physical_line"]]),
            l1_set=int(resource[RESOURCE_KEY_INDEX["l1_set"]]),
            l2_set=int(resource[RESOURCE_KEY_INDEX["l2_set"]]),
            llc_set=int(resource[RESOURCE_KEY_INDEX["llc_set"]]),
            llc_bank=int(resource[RESOURCE_KEY_INDEX["llc_bank"]]),
            access_kind=int(values["access"][ordinal]),
        )
        values["categorical"][ordinal] = feature.categorical
        values["continuous"][ordinal] = feature.continuous

    os.makedirs(output_dir, exist_ok=True)
    if os.path.exists(os.path.join(output_dir, "meta.json")):
        raise RuntimeError(f"refusing to overwrite populated output {output_dir}")
    core_meta = []
    for core in store.core_ids:
        values = core_memory[core]
        core_dir = os.path.join(output_dir, "cores", str(core))
        os.makedirs(core_dir, exist_ok=True)
        np.save(os.path.join(core_dir, "index.npy"), values["index"])
        np.save(os.path.join(core_dir, "categorical.npy"), values["categorical"])
        np.save(
            os.path.join(core_dir, "continuous.npy"),
            values["continuous"].astype(np.float16),
        )
        core_meta.append({
            "core_id": int(core),
            "n_uops": int(len(store.cores[core]["access"])),
            "n_memory_uops": int(len(values["index"])),
        })
    elapsed = time.perf_counter() - started
    contract = {
        "schema_version": SIDECAR_SCHEMA,
        "engine_schema": GSS_SCHEMA_VERSION,
        "trace_id": str(row["trace_id"]),
        "cache_dir": os.path.abspath(str(row["cache_dir"])),
        "clock_source": clock,
        "order_policy": f"{clock}_tick_then_core_then_uop_v1",
        "features_are_pre_access": True,
        "timestamp_is_model_visible": False,
        "replacement": {"l1d": "lru", "l2": "tree_plru", "llc": "tree_plru"},
        "geometry": geometry.__dict__,
        "categorical_fields": list(GSS_CATEGORICAL_FIELDS),
        "continuous_fields": list(GSS_CONTINUOUS_FIELDS),
        "categorical_dtype": "uint8",
        "continuous_dtype": "float16",
        "cores": core_meta,
        "n_uops": int(total_uops),
        "n_memory_uops": int(total_memory),
        "physical_address_coverage_all_uops": total_valid / max(1, total_uops),
        "physical_address_coverage_memory_uops": total_valid / max(1, total_memory),
        "elapsed_seconds": elapsed,
        "events_per_second": total_memory / max(elapsed, 1e-9),
        "state_summary": dict(engine.state_summary()),
        "generated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(),
    }
    contract_payload = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
    contract["contract_hash"] = hashlib.sha256(contract_payload).hexdigest()
    _dump_json(os.path.join(output_dir, "meta.json"), contract)
    return contract


def _build_job(
    row: Mapping[str, Any], destination: str, clock: str,
) -> tuple[str, Mapping[str, Any]]:
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".gss-build-", dir=parent)
    try:
        report = build_trace(row, temporary, clock=clock)
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return str(row["trace_id"]), report


def _write_derived_manifest(
    source: Mapping[str, Any], output_path: str, sidecar_root: str, clock: str,
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
            row["gss_sidecar_dir"] = path
            attached += 1
    derived["gss_sidecars"] = {
        "schema_version": SIDECAR_SCHEMA,
        "root": sidecar_root,
        "attached_split_rows": attached,
        "clock_source": str(clock),
    }
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    _dump_json(os.path.abspath(output_path), derived)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", default=os.path.join(REPO_ROOT, "data/v29_global_time_dataset/manifest.json"),
    )
    parser.add_argument("--splits", default="train,development_heldout")
    parser.add_argument("--workload-regex", default="")
    parser.add_argument("--core-counts", default="1,4,8,16,32")
    parser.add_argument("--clock", choices=("ready", "issue", "commit"), default="commit")
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument(
        "--out-root", default=os.path.join(REPO_ROOT, "data/v30_gss_commit_sidecars"),
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--write-manifest", default="")
    args = parser.parse_args()
    manifest = _load_json(os.path.abspath(args.manifest))
    rows = _records(
        manifest,
        _csv(args.splits),
        args.workload_regex,
        tuple(int(value) for value in _csv(args.core_counts)),
        args.max_traces,
    )
    if not rows:
        raise RuntimeError("no traces selected")
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
                meta = _load_json(os.path.join(destination, "meta.json"))
                if meta.get("clock_source") != args.clock:
                    raise RuntimeError(f"existing sidecar clock mismatch: {destination}")
                reports.append(meta)
                print(f"[v30-gss] reuse {ordinal}/{len(rows)} {row['trace_id']}", flush=True)
                continue
            shutil.rmtree(destination)
        pending.append((row, destination))
    wall_started = time.perf_counter()
    if pending:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(args.workers, len(pending)),
        ) as executor:
            futures = {
                executor.submit(_build_job, row, destination, args.clock): row
                for row, destination in pending
            }
            completed = len(reports)
            for future in concurrent.futures.as_completed(futures):
                trace_id, report = future.result()
                reports.append(report)
                completed += 1
                print(
                    f"[v30-gss] built {completed}/{len(rows)} {trace_id} "
                    f"events={report['n_memory_uops']:,} "
                    f"rate={report['events_per_second']:,.0f}/s "
                    f"mem-paddr={100*report['physical_address_coverage_memory_uops']:.2f}%",
                    flush=True,
                )
    wall_elapsed = time.perf_counter() - wall_started
    reports.sort(key=lambda row: str(row["trace_id"]))
    summary = {
        "schema_version": "tcsim-v30-gss-sidecar-set-1",
        "manifest": os.path.abspath(args.manifest),
        "splits": list(_csv(args.splits)),
        "workload_regex": args.workload_regex,
        "clock_source": args.clock,
        "traces": len(reports),
        "n_memory_uops": sum(int(row["n_memory_uops"]) for row in reports),
        "elapsed_seconds": sum(float(row["elapsed_seconds"]) for row in reports),
        "wall_elapsed_seconds": wall_elapsed,
        "workers": int(args.workers),
        "trace_ids": [str(row["trace_id"]) for row in reports],
    }
    summary["events_per_second"] = summary["n_memory_uops"] / max(
        1e-9, summary["elapsed_seconds"],
    )
    _dump_json(os.path.join(output_root, "summary.json"), summary)
    if args.write_manifest:
        if args.clock != "commit":
            raise RuntimeError("formal v30 manifest requires commit-clock sidecars")
        _write_derived_manifest(
            manifest, args.write_manifest, output_root, args.clock,
        )
        print(f"[v30-gss] manifest={os.path.abspath(args.write_manifest)}", flush=True)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
