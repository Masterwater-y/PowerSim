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
from tcsim.v29.contracts import (
    BRANCH_CONTRACT_VERSION,
    DATASET_SCHEMA_VERSION,
    FEATURE_SCHEMA_VERSION,
    MODEL_INPUT_CONTRACT,
    RESOURCE_DECODER_SCHEMA_VERSION,
    normalized_horizons,
)


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


def _nested_value(data: Dict[str, Any], path: Sequence[str]) -> Any:
    value: Any = data
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def _hardware_profile_violations(
    meta: Dict[str, Any], acceptance: Dict[str, Any], record: Dict[str, Any],
) -> List[Dict[str, Any]]:
    checks = {
        "profile_l2_size_b": ("cache", "l2", "size_b"),
        "profile_l3_size_b": ("cache", "l3", "size_b"),
        "profile_l3_num_banks": ("cache", "l3", "num_banks"),
        "profile_dram_num_channels": ("dram", "num_channels"),
    }
    profile = dict(meta.get("uarch_profile", {}) or {})
    violations = []
    for contract_key, path in checks.items():
        if contract_key not in acceptance:
            continue
        expected = int(acceptance[contract_key])
        actual = _nested_value(profile, path)
        try:
            observed = int(actual)
        except (TypeError, ValueError):
            observed = None
        if observed != expected:
            violations.append({
                "cache_dir": record.get("cache_dir"),
                "workload": record.get("workload"),
                "seed": record.get("seed"),
                "n_cores": record.get("n_cores"),
                "field": ".".join(path),
                "expected": expected,
                "observed": observed,
            })
    return violations


def _job(args: Tuple[Any, ...]) -> Dict[str, Any]:
    (
        trace_dir, out_dir, horizons, sample_period, block_cycles,
        max_samples, min_uops_per_core, max_uops_per_core,
        max_full_uop_cpi, overwrite,
    ) = args
    artifact = build_trace_cache(
        trace_dir,
        out_dir,
        K=256,
        horizons=horizons,
        sample_period_cycles=sample_period,
        block_cycles=block_cycles,
        max_samples=max_samples,
        min_uops_per_core=min_uops_per_core,
        max_uops_per_core=max_uops_per_core,
        max_full_uop_cpi=max_full_uop_cpi,
        overwrite=overwrite,
    )
    return {
        "cache_dir": artifact.out_dir,
        "trace_id": artifact.trace_id,
        "observed_n_cores": artifact.n_cores,
        "n_uops": artifact.n_uops,
        "n_samples": artifact.n_samples,
    }


def _existing_ok(
    path: str,
    horizons: Sequence[float],
    sample_period: float,
    min_uops_per_core: int,
    max_uops_per_core: int,
    max_full_uop_cpi: float,
) -> bool:
    meta_path = os.path.join(path, "meta.json")
    if not os.path.isfile(meta_path):
        return False
    try:
        meta = load_json(meta_path)
    except Exception:
        return False
    return (
        meta.get("dataset_schema") == DATASET_SCHEMA_VERSION
        and meta.get("feature_schema") == FEATURE_SCHEMA_VERSION
        and meta.get("model_input_contract") == MODEL_INPUT_CONTRACT
        and meta.get("branch_contract") == BRANCH_CONTRACT_VERSION
        and meta.get("resource_decoder_schema") == RESOURCE_DECODER_SCHEMA_VERSION
        and int(meta.get("K", -1)) == 256
        and bool(meta.get("cores"))
        and tuple(float(value) for value in meta.get("horizons", [])) == tuple(horizons)
        and float(meta.get("sample_period_cycles", -1)) == float(sample_period)
        and int(meta.get("min_uops_per_core_contract", -1))
        == int(min_uops_per_core)
        and int(meta.get("max_uops_per_core_contract", -1))
        == int(max_uops_per_core)
        and float(meta.get("max_full_uop_cpi_contract", -1))
        == float(max_full_uop_cpi)
        and bool(meta.get("collection_provenance", {}).get("ff_atomic_verified"))
        and bool(meta.get("quality", {}).get("ff_atomic_verified"))
        and bool(meta.get("quality", {}).get("synchronous_roi_start"))
        and int(meta.get("quality", {}).get(
            "roi_atomic_uops", meta.get("quality", {}).get("atomic_uops", -1)
        )) == 0
        and all(
            (
                int(min_uops_per_core) <= int(core.get("n_uops", -1))
                <= int(max_uops_per_core)
                and float(core.get("full_uop_cpi", float("inf")))
                <= float(max_full_uop_cpi)
            )
            for core in meta.get("cores", [])
        )
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
    parser.add_argument("--min-uops-per-core", type=int, default=None)
    parser.add_argument("--max-uops-per-core", type=int, default=None)
    parser.add_argument("--max-full-uop-cpi", type=float, default=None)
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
    acceptance = dict(contract.get("acceptance", {}) or {})
    min_uops_per_core = int(
        args.min_uops_per_core
        if args.min_uops_per_core is not None
        else acceptance.get("records_per_core_min", 500000)
    )
    max_uops_per_core = int(
        args.max_uops_per_core
        if args.max_uops_per_core is not None
        else acceptance.get("records_per_core_max", 1000000)
    )
    max_full_uop_cpi = float(
        args.max_full_uop_cpi
        if args.max_full_uop_cpi is not None
        else acceptance.get("full_cpi_per_core_max", 10.0)
    )
    if min_uops_per_core <= 0 or max_uops_per_core < min_uops_per_core:
        raise SystemExit("invalid per-core UOP bounds")
    if max_full_uop_cpi <= 0:
        raise SystemExit("max-full-uop-cpi must be positive")
    expected_seed0_cores = {
        int(value) for value in contract.get("expected_cores", [1, 4, 8, 16, 32])
    }
    expected_deployment_cores = {
        int(value) for value in contract.get("validation_cores", [4, 8, 16, 32])
    }
    raw_roots = []
    for path in sorted(glob.glob(args.raw_root_glob)):
        if not os.path.isdir(path):
            continue
        try:
            seed, cores = _root_metadata(path)
        except ValueError:
            continue
        allowed = expected_seed0_cores if seed == 0 else expected_deployment_cores
        if seed in seeds and cores in core_counts and cores in allowed:
            raw_roots.append((path, seed, cores))
    if not raw_roots:
        raise SystemExit("no matching raw roots")

    expected_root_pairs = {
        (seed, cores)
        for seed in seeds
        for cores in (
            expected_seed0_cores if seed == 0 else expected_deployment_cores
        )
        if cores in core_counts
    }
    roots_by_pair: Dict[Tuple[int, int], List[str]] = {}
    for path, seed, cores in raw_roots:
        roots_by_pair.setdefault((seed, cores), []).append(path)
    missing_raw_roots = [
        {"seed": seed, "n_cores": cores}
        for seed, cores in sorted(expected_root_pairs - set(roots_by_pair))
    ]
    duplicate_raw_roots = [
        {"seed": seed, "n_cores": cores, "roots": sorted(paths)}
        for (seed, cores), paths in sorted(roots_by_pair.items())
        if len(paths) != 1
    ]

    out_root = os.path.abspath(args.out)
    trace_root = os.path.join(out_root, "traces")
    os.makedirs(trace_root, exist_ok=True)
    jobs = []
    records: List[Dict[str, Any]] = []
    missing_inputs: List[Dict[str, Any]] = []
    for raw_root, seed, cores in raw_roots:
        for workload in all_workloads:
            trace_dir = os.path.join(raw_root, workload, "tao_trace")
            if not os.path.isdir(trace_dir):
                missing_inputs.append({
                    "raw_root": os.path.abspath(raw_root),
                    "workload": workload,
                    "seed": seed,
                    "n_cores": cores,
                    "expected_trace_dir": os.path.abspath(trace_dir),
                })
                continue
            cache_dir = os.path.join(
                trace_root, os.path.basename(raw_root.rstrip("/")), workload,
            )
            base = {
                "raw_root": os.path.abspath(raw_root),
                "trace_dir": os.path.abspath(trace_dir),
                "cache_dir": os.path.abspath(cache_dir),
                "workload": workload,
                "workload_role": (
                    "train_base" if workload in train_workloads
                    else "business_heldout"
                ),
                "seed": seed,
                "n_cores": cores,
            }
            if not args.overwrite and _existing_ok(
                cache_dir,
                horizons,
                args.sample_period_cycles,
                min_uops_per_core,
                max_uops_per_core,
                max_full_uop_cpi,
            ):
                meta = load_json(os.path.join(cache_dir, "meta.json"))
                records.append({
                    **base,
                    "trace_id": meta["trace_id"],
                    "observed_n_cores": int(meta.get("n_cores", -1)),
                    "n_uops": int(meta["n_uops"]),
                    "n_samples": int(meta["n_samples"]),
                })
                continue
            jobs.append((base, (
                trace_dir, cache_dir, horizons, args.sample_period_cycles,
                args.block_cycles, args.max_samples, min_uops_per_core,
                max_uops_per_core, max_full_uop_cpi,
                bool(args.overwrite or os.path.exists(cache_dir)),
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
                    if int(result["observed_n_cores"]) != int(base["n_cores"]):
                        raise RuntimeError(
                            "raw-root/core stream mismatch "
                            f"expected={base['n_cores']} "
                            f"observed={result['observed_n_cores']}"
                        )
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
    profile_violations: List[Dict[str, Any]] = []
    core_count_violations: List[Dict[str, Any]] = []
    for record in records:
        observed = int(record.get("observed_n_cores", -1))
        if observed != int(record["n_cores"]):
            core_count_violations.append({
                "cache_dir": record["cache_dir"],
                "workload": record["workload"],
                "seed": record["seed"],
                "expected": int(record["n_cores"]),
                "observed": observed,
            })
        meta = load_json(os.path.join(record["cache_dir"], "meta.json"))
        profile_violations.extend(
            _hardware_profile_violations(meta, acceptance, record)
        )
    validation_cores = expected_deployment_cores
    split_policy = {
        "validation_percent": 10,
        "seed": 20260716,
        "guard_cycles": max(horizons),
        "require_full_lookahead_within_block": True,
    }
    splits: Dict[str, List[Dict[str, Any]]] = {
        "train": [],
        "validation": [],
        "development_heldout": [],
        "seed0_inference": [],
        "deployment_inference": [],
        "final_untouched": [],
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
        elif seed == 1:
            # Seed1 has already been used repeatedly for development diagnosis.
            splits["deployment_inference"].append(dict(record))
        else:
            # Seed2+ is a separately named, one-shot final-test corpus.  It is
            # never silently pooled into development deployment results.
            splits["final_untouched"].append(dict(record))
    blockers = []
    if missing_raw_roots:
        blockers.append(f"{len(missing_raw_roots)} requested raw roots are missing")
    if duplicate_raw_roots:
        blockers.append(f"{len(duplicate_raw_roots)} seed/core pairs have duplicate raw roots")
    if missing_inputs:
        blockers.append(f"{len(missing_inputs)} workload trace directories are missing")
    if failures:
        blockers.append(f"{len(failures)} trace cache builds failed")
    if core_count_violations:
        blockers.append(f"{len(core_count_violations)} cache/core counts mismatch")
    if profile_violations:
        blockers.append(f"{len(profile_violations)} hardware profile checks failed")
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
        "data_acceptance": {
            "min_uops_per_core": min_uops_per_core,
            "max_uops_per_core": max_uops_per_core,
            "max_full_uop_cpi": max_full_uop_cpi,
            "hardware_profile": {
                key: int(value) for key, value in acceptance.items()
                if key.startswith("profile_")
            },
            "require_ff_atomic_to_o3_ruby": True,
            "roi_atomic_uops": 0,
        },
        "contract_file": os.path.abspath(args.contract_file),
        "quality": {
            "status": "pass" if not blockers else "fail",
            "blockers": blockers,
            "failures": failures,
            "missing_raw_roots": missing_raw_roots,
            "duplicate_raw_roots": duplicate_raw_roots,
            "missing_inputs": missing_inputs,
            "core_count_violations": core_count_violations,
            "hardware_profile_violations": profile_violations,
        },
        "splits": splits,
    }
    dump_json(os.path.join(out_root, "manifest.json"), manifest)
    print(
        f"[v29 manifest] traces={len(records)} train={len(splits['train'])} "
        f"val={len(splits['validation'])} heldout={len(splits['development_heldout'])} "
        f"deployment={len(splits['deployment_inference'])} "
        f"untouched={len(splits['final_untouched'])} "
        f"status={manifest['quality']['status']}",
        flush=True,
    )
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
