#!/usr/bin/env python3
"""Build v29 common-time caches and a non-leaky split manifest."""
from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import re
import sys
import traceback
from typing import Any, Dict, Iterable, List, Sequence, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.io import dump_json, load_json
from tcsim.v29.builder import build_trace_cache
from tcsim.v29.contracts import DATASET_SCHEMA_VERSION, normalized_horizons


CORE_RE = re.compile(r"_c(\d+)$")
SEED_RE = re.compile(r"seed(\d+)")


def _csv_floats(value: str) -> Tuple[float, ...]:
    return normalized_horizons(
        float(part.strip()) for part in value.split(",") if part.strip()
    )


def _root_metadata(path: str) -> Tuple[int, int]:
    name = os.path.basename(path.rstrip("/"))
    core_match = CORE_RE.search(name)
    seed_match = SEED_RE.search(name)
    if not core_match or not seed_match:
        raise ValueError(f"cannot infer seed/core count from raw root {name!r}")
    return int(seed_match.group(1)), int(core_match.group(1))


def _workloads(contract: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    train = [str(value) for value in contract.get("train", [])]
    heldout = [str(value) for value in contract.get("heldout_business", [])]
    if not train:
        raise ValueError("workload contract has no train workloads")
    return train, heldout


def _job(args: Tuple[Any, ...]) -> Dict[str, Any]:
    (
        trace_dir, out_dir, horizons, sample_period, block_cycles,
        max_samples, overwrite,
    ) = args
    artifact = build_trace_cache(
        trace_dir,
        out_dir,
        K=256,
        horizons=horizons,
        sample_period_cycles=sample_period,
        block_cycles=block_cycles,
        max_samples=max_samples,
        overwrite=overwrite,
    )
    return {
        "cache_dir": artifact.out_dir,
        "trace_id": artifact.trace_id,
        "n_cores": artifact.n_cores,
        "n_uops": artifact.n_uops,
        "n_samples": artifact.n_samples,
    }


def _existing_ok(path: str, horizons: Sequence[float], sample_period: float) -> bool:
    meta_path = os.path.join(path, "meta.json")
    if not os.path.isfile(meta_path):
        return False
    try:
        meta = load_json(meta_path)
    except Exception:
        return False
    return (
        meta.get("dataset_schema") == DATASET_SCHEMA_VERSION
        and tuple(float(value) for value in meta.get("horizons", [])) == tuple(horizons)
        and float(meta.get("sample_period_cycles", -1)) == float(sample_period)
        and meta.get("quality", {}).get("status") == "pass"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raw-root-glob",
        default="/data00/yinhaolang/TSim/data/raw_v28_1_business_a2_sharedzipf_seed*_c*",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--contract-file",
        default=os.path.join(ROOT, "configs", "v28_business_workloads.json"),
    )
    parser.add_argument("--horizons", default="16,32,64,128,256,512,1024")
    parser.add_argument("--sample-period-cycles", type=float, default=64.0)
    parser.add_argument("--block-cycles", type=float, default=65536.0)
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 8) // 4))
    parser.add_argument("--max-samples", type=int, default=None)
    parser.add_argument("--seeds", default="0,1")
    parser.add_argument("--core-counts", default="1,4,8,16,32")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()

    horizons = _csv_floats(args.horizons)
    if not any(abs(value - args.sample_period_cycles) < 1e-6 for value in horizons):
        raise SystemExit("sample-period-cycles must be included in --horizons")
    seeds = {int(part) for part in args.seeds.split(",") if part.strip()}
    core_counts = {int(part) for part in args.core_counts.split(",") if part.strip()}
    contract = load_json(args.contract_file)
    train_workloads, heldout_workloads = _workloads(contract)
    all_workloads = train_workloads + heldout_workloads
    raw_roots = []
    for path in sorted(glob.glob(args.raw_root_glob)):
        if not os.path.isdir(path):
            continue
        try:
            seed, cores = _root_metadata(path)
        except ValueError:
            continue
        if seed in seeds and cores in core_counts:
            raw_roots.append((path, seed, cores))
    if not raw_roots:
        raise SystemExit("no matching raw roots")

    out_root = os.path.abspath(args.out)
    trace_root = os.path.join(out_root, "traces")
    os.makedirs(trace_root, exist_ok=True)
    jobs = []
    records: List[Dict[str, Any]] = []
    for raw_root, seed, cores in raw_roots:
        for workload in all_workloads:
            trace_dir = os.path.join(raw_root, workload, "tao_trace")
            if not os.path.isdir(trace_dir):
                continue
            cache_dir = os.path.join(
                trace_root, os.path.basename(raw_root.rstrip("/")), workload,
            )
            base = {
                "raw_root": os.path.abspath(raw_root),
                "trace_dir": os.path.abspath(trace_dir),
                "cache_dir": os.path.abspath(cache_dir),
                "workload": workload,
                "seed": seed,
                "n_cores": cores,
            }
            if not args.overwrite and _existing_ok(cache_dir, horizons, args.sample_period_cycles):
                meta = load_json(os.path.join(cache_dir, "meta.json"))
                records.append({
                    **base,
                    "trace_id": meta["trace_id"],
                    "n_uops": int(meta["n_uops"]),
                    "n_samples": int(meta["n_samples"]),
                })
                continue
            jobs.append((base, (
                trace_dir, cache_dir, horizons, args.sample_period_cycles,
                args.block_cycles, args.max_samples, args.overwrite,
            )))

    failures = []
    if jobs:
        with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as executor:
            future_to_base = {
                executor.submit(_job, job): base for base, job in jobs
            }
            completed = 0
            for future in concurrent.futures.as_completed(future_to_base):
                base = future_to_base[future]
                completed += 1
                try:
                    result = future.result()
                    records.append({**base, **result})
                    print(
                        f"[v29 build {completed}/{len(jobs)}] ok "
                        f"{base['workload']} c{base['n_cores']} seed{base['seed']} "
                        f"samples={result['n_samples']}",
                        flush=True,
                    )
                except Exception as exc:
                    failure = {**base, "error": repr(exc), "traceback": traceback.format_exc()}
                    failures.append(failure)
                    print(
                        f"[v29 build {completed}/{len(jobs)}] FAIL "
                        f"{base['workload']} c{base['n_cores']} seed{base['seed']}: {exc}",
                        file=sys.stderr, flush=True,
                    )
                    if args.fail_fast:
                        for pending in future_to_base:
                            pending.cancel()
                        break

    records.sort(key=lambda item: (item["seed"], item["n_cores"], item["workload"]))
    validation_cores = {4, 8, 16, 32}
    split_policy = {
        "validation_percent": 10,
        "seed": 20260716,
        "guard_cycles": max(horizons),
    }
    splits: Dict[str, List[Dict[str, Any]]] = {
        "train": [],
        "validation": [],
        "development_heldout": [],
        "seed0_inference": [],
        "deployment_inference": [],
    }
    for record in records:
        workload = str(record["workload"])
        seed = int(record["seed"])
        cores = int(record["n_cores"])
        if seed == 0:
            splits["seed0_inference"].append(dict(record))
            if workload in train_workloads:
                splits["train"].append({
                    **record,
                    "sample_split": {**split_policy, "partition": "train"},
                })
                if cores in validation_cores:
                    splits["validation"].append({
                        **record,
                        "sample_split": {**split_policy, "partition": "validation"},
                    })
            elif workload in heldout_workloads and cores in validation_cores:
                splits["development_heldout"].append(dict(record))
        else:
            # seed1 and later seeds are never training-time validation.
            splits["deployment_inference"].append(dict(record))
    blockers = []
    if failures:
        blockers.append(f"{len(failures)} trace cache builds failed")
    if not splits["train"]:
        blockers.append("train split is empty")
    if not splits["validation"]:
        blockers.append("validation split is empty")
    manifest = {
        "schema_version": "tcsim-v29-manifest-1",
        "dataset_schema": DATASET_SCHEMA_VERSION,
        "horizons": list(horizons),
        "sample_period_cycles": args.sample_period_cycles,
        "block_cycles": args.block_cycles,
        "contract_file": os.path.abspath(args.contract_file),
        "quality": {
            "status": "pass" if not blockers else "fail",
            "blockers": blockers,
            "failures": failures,
        },
        "splits": splits,
    }
    dump_json(os.path.join(out_root, "manifest.json"), manifest)
    print(
        f"[v29 manifest] traces={len(records)} train={len(splits['train'])} "
        f"val={len(splits['validation'])} heldout={len(splits['development_heldout'])} "
        f"deployment={len(splits['deployment_inference'])} status={manifest['quality']['status']}",
        flush=True,
    )
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
