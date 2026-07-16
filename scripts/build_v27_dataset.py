#!/usr/bin/env python3
"""Plan and optionally materialize the v27.0-cold16 oracle-context dataset.

Seed0 is split deterministically at the complete oracle-context sample level
for development validation.  Seed1 is deployment-only and never enters the
training-time train/validation splits.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import glob
import json
import os
import re
import sys
from typing import Any, Dict, Iterable, List, Sequence

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.dataset.rollout_builder import build_and_dump_trace
from tcsim.chunker.functional_features import FEATURE_SCHEMA_VERSION, PACKED_SCHEMA_VERSION
from tcsim.utils.config import TCSimConfig
from tcsim.utils.io import dump_json


TRAIN_WORKLOADS = {
    "W_int_alu_dense", "W_int_div_serial", "W_fp_alu_dense",
    "W_simd_sse_dense",
    "W_stream_seq_L2", "W_stream_seq_DRAM", "W_random_DRAM",
    "W_chase_DRAM",
    "W_coh_read_share", "W_coh_write_share", "W_coh_false_share",
    "W_coh_asym_rw",
    "W_phase_coh_onset",
    "W_skew_hot_cold", "W_ranking_mix_private",
}
HELDOUT_WORKLOADS = {"W_phase_coh_decay"}
EXPECTED_CORES = {1, 4, 8, 16, 32}
VALIDATION_CORES = {4, 8, 16, 32}
HELDOUT_SPLIT = "test_mechanism"
MANIFEST_SCHEMA_VERSION = "v27.0-cold16-oracle-functional-manifest-2"
CORE_RE = re.compile(r"(?:^|_)c(\d+)(?:_|$)")
SEED_RE = re.compile(r"seed(?:A|B|_)?(\d+)", re.IGNORECASE)


def _csv_ints(text: str) -> set:
    return {int(value) for value in text.split(",") if value.strip()}


def _root_meta(path: str) -> tuple:
    name = os.path.basename(path.rstrip(os.sep))
    core_matches = CORE_RE.findall(name)
    if not core_matches:
        raise ValueError(f"cannot parse core count from {name}")
    seed_match = SEED_RE.search(name)
    seed = int(seed_match.group(1)) if seed_match else 0
    return int(core_matches[-1]), seed


def _discover(root_glob: str, out_root: str) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for raw_root in sorted(glob.glob(root_glob)):
        if not os.path.isdir(raw_root):
            continue
        n_cores, seed = _root_meta(raw_root)
        for workload in sorted(os.listdir(raw_root)):
            trace_dir = os.path.join(raw_root, workload, "tao_trace")
            if not workload.startswith("W_") or not os.path.isdir(trace_dir):
                continue
            rollout_dir = os.path.join(
                os.path.abspath(out_root), "rollouts",
                os.path.basename(raw_root.rstrip(os.sep)), workload,
            )
            entries.append({
                "raw_root": os.path.abspath(raw_root),
                "trace_dir": os.path.abspath(trace_dir),
                "workload": workload,
                "n_cores": n_cores,
                "seed": seed,
                "rollout_dir": rollout_dir,
            })
    return entries


def _accepted_by_workload_override(
    violation: Dict[str, Any], overrides: Dict[str, Dict[str, float]],
) -> bool:
    limits = overrides.get(str(violation.get("workload", "")), {})
    gate = str(violation.get("gate", ""))
    if gate not in limits:
        return False
    try:
        actual = float(violation["actual"])
        limit = float(limits[gate])
    except (KeyError, TypeError, ValueError):
        return False
    if gate.endswith("_max"):
        return actual <= limit
    if gate.endswith("_min"):
        return actual >= limit
    return actual == limit


def _quality(
    entries: Sequence[Dict[str, Any]],
    audit: Dict[str, Any],
    acceptance_overrides: Dict[str, Dict[str, float]],
) -> Dict[str, Any]:
    present = {(e["seed"], e["n_cores"], e["workload"]) for e in entries}
    train_seeds = sorted({e["seed"] for e in entries}) or [0]
    primary_seed = train_seeds[0]
    missing = [
        {"seed": primary_seed, "n_cores": core, "workload": workload}
        for core in sorted(EXPECTED_CORES)
        for workload in sorted(TRAIN_WORKLOADS | HELDOUT_WORKLOADS)
        if (primary_seed, core, workload) not in present
    ]
    normalized_collisions = [
        row for row in audit.get("exact_functional_pair_collisions", [])
        if row.get("same_normalized_address")
    ]
    raw_acceptance_violations = list(audit.get("acceptance_violations", []))
    accepted_violations = [
        row for row in raw_acceptance_violations
        if _accepted_by_workload_override(row, acceptance_overrides)
    ]
    acceptance_violations = [
        row for row in raw_acceptance_violations
        if not _accepted_by_workload_override(row, acceptance_overrides)
    ]
    blockers = []
    if not audit.get("rows"):
        blockers.append("missing stratified audit report")
    if missing:
        blockers.append(f"missing {len(missing)} workload/core cells")
    if normalized_collisions:
        blockers.append(f"{len(normalized_collisions)} normalized functional pair collisions")
    if acceptance_violations:
        blockers.append(f"{len(acceptance_violations)} workload acceptance violations")
    return {
        "status": "pass" if not blockers else "blocked",
        "blockers": blockers,
        "missing_cells": missing,
        "normalized_functional_collisions": normalized_collisions,
        "acceptance_violations": acceptance_violations,
        "accepted_acceptance_violations": accepted_violations,
        "acceptance_overrides": acceptance_overrides,
        "sync_semantics_in_scope": False,
    }


def _assign_splits(
    entries: Sequence[Dict[str, Any]], train_seeds: set, deployment_seeds: set,
    sample_validation_percent: int, sample_split_seed: int,
) -> Dict[str, List[Dict[str, Any]]]:
    splits: Dict[str, List[Dict[str, Any]]] = {
        "train": [], "validation": [], "test_mechanism": [],
        "test_business": [], "deployment_inference": [], "excluded": [],
    }
    split_base = {
        "unit": "complete_oracle_context_sample",
        "strategy": "sha256_mod_100",
        "validation_percent": int(sample_validation_percent),
        "seed": int(sample_split_seed),
    }
    for raw in entries:
        entry = dict(raw)
        workload = entry["workload"]
        seed = int(entry["seed"])
        # A deployment seed is an independent end-to-end replay corpus.  It
        # contains both the training-family workloads and the business
        # heldouts; keep all of them together so seed1 evaluation cannot
        # silently mix seed0 heldouts into its report.
        if (
            seed in deployment_seeds
            and workload in (TRAIN_WORKLOADS | HELDOUT_WORKLOADS)
        ):
            split = "deployment_inference"
            entry["split"] = split
            splits[split].append(entry)
        elif workload in HELDOUT_WORKLOADS:
            split = HELDOUT_SPLIT
            entry["split"] = split
            splits[split].append(entry)
        elif workload in TRAIN_WORKLOADS and seed in train_seeds:
            train_entry = dict(entry)
            train_entry["split"] = "train"
            # c01 is train-only.  c04/c08/c16/c32 share the deterministic
            # seed0 development partition with their matching val sources.
            if int(entry["n_cores"]) in VALIDATION_CORES:
                train_entry["sample_split"] = {**split_base, "partition": "train"}
            splits["train"].append(train_entry)
            if int(entry["n_cores"]) in VALIDATION_CORES:
                val_entry = dict(entry)
                val_entry["split"] = "validation"
                val_entry["sample_split"] = {**split_base, "partition": "validation"}
                splits["validation"].append(val_entry)
        else:
            split = "excluded"
            entry["split"] = split
            splits[split].append(entry)
    return splits


def _build_one(
    entry: Dict[str, Any], config_path: str, input_format: str,
) -> str:
    """Materialize one independent trace cache; safe to run in a child process."""
    cfg = TCSimConfig.load(config_path)
    raw_tpc = cfg.uarch.get("tick_per_cycle", "auto")
    tpc = None if str(raw_tpc).lower() == "auto" else float(raw_tpc)
    raw_budget = cfg.scheduler.get("max_forward_budget", 0)
    budget = None if raw_budget is None or int(raw_budget) <= 0 else int(raw_budget)
    build_and_dump_trace(
        entry["trace_dir"],
        out_dir=entry["rollout_dir"],
        K=cfg.K,
        epsilon=cfg.epsilon,
        tick_per_cycle=tpc,
        max_forward_budget=budget,
        max_resident_exposure=int(cfg.scheduler.get("max_resident_exposure", 0)),
        trace_id=None,
        cache_format="packed",
        input_format=input_format,
    )
    return entry["rollout_dir"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--raw-root-glob",
        default="/data00/yinhaolang/TSim/data/raw_v27_0_cold16_seed*_c*",
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default=os.path.join(ROOT, "configs", "mvp.yaml"))
    parser.add_argument(
        "--input-format", choices=("auto", "raw", "aligned"), default="aligned",
        help="trace source; cold16 collection converts and retains aligned parquet",
    )
    parser.add_argument("--audit-report", default="")
    parser.add_argument(
        "--contract-file", default="",
        help="optional workload contract JSON overriding the built-in v27 sets",
    )
    parser.add_argument("--train-seeds", default="0")
    parser.add_argument(
        "--deployment-seeds", default="1",
        help="seeds reserved exclusively for deployment-side inference",
    )
    parser.add_argument("--sample-validation-percent", type=int, default=10)
    parser.add_argument("--sample-split-seed", type=int, default=20260714)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--allow-provisional", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument(
        "--workers", type=int, default=1,
        help="independent trace-cache builders; keep bounded for c32 memory/IO",
    )
    args = parser.parse_args()

    global TRAIN_WORKLOADS, HELDOUT_WORKLOADS, EXPECTED_CORES
    global VALIDATION_CORES, HELDOUT_SPLIT, MANIFEST_SCHEMA_VERSION
    contract: Dict[str, Any] = {}
    if args.contract_file:
        with open(args.contract_file, "r", encoding="utf-8") as fh:
            contract = json.load(fh)
        TRAIN_WORKLOADS = set(contract.get("train", []))
        HELDOUT_WORKLOADS = set(
            contract.get("heldout_business", contract.get("heldout", []))
        )
        EXPECTED_CORES = set(int(x) for x in contract.get("expected_cores", EXPECTED_CORES))
        VALIDATION_CORES = set(
            int(x) for x in contract.get("validation_cores", VALIDATION_CORES)
        )
        HELDOUT_SPLIT = "test_business"
        MANIFEST_SCHEMA_VERSION = str(
            contract.get("schema_version", "custom-oracle-functional-manifest-1")
        )

    os.makedirs(args.out, exist_ok=True)
    entries = _discover(args.raw_root_glob, args.out)
    if not entries:
        raise SystemExit(f"no traces matched {args.raw_root_glob}")
    audit: Dict[str, Any] = {}
    if args.audit_report:
        with open(args.audit_report, "r", encoding="utf-8") as fh:
            audit = json.load(fh)
    acceptance_overrides = {
        str(workload): {
            str(k): float(v) for k, v in dict(limits).items()
        }
        for workload, limits in dict(
            contract.get("acceptance_overrides", {})
        ).items()
    }
    quality = _quality(entries, audit, acceptance_overrides)
    if not 0 < int(args.sample_validation_percent) < 100:
        raise SystemExit("--sample-validation-percent must be in (0, 100)")
    splits = _assign_splits(
        entries,
        _csv_ints(args.train_seeds),
        _csv_ints(args.deployment_seeds),
        int(args.sample_validation_percent),
        int(args.sample_split_seed),
    )
    if not splits["validation"]:
        quality["blockers"].append("no seed0 development-validation rollout sources")
        quality["status"] = "blocked"
    else:
        required_validation = {
            (core, workload)
            for core in sorted(VALIDATION_CORES)
            for workload in TRAIN_WORKLOADS
        }
        observed_validation = {
            (int(entry["n_cores"]), str(entry["workload"]))
            for entry in splits["validation"]
        }
        missing_validation = sorted(required_validation - observed_validation)
        if missing_validation:
            quality["blockers"].append(
                f"missing {len(missing_validation)} required development-validation cells"
            )
            quality["status"] = "blocked"
        quality["missing_validation_cells"] = [
            {"n_cores": core, "workload": workload}
            for core, workload in missing_validation
        ]
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "quality": quality,
        "split_policy": {
            "train_validation_unit": "complete_oracle_context_sample",
            "train_seeds": sorted(_csv_ints(args.train_seeds)),
            "deployment_seeds": sorted(_csv_ints(args.deployment_seeds)),
            "development_validation": {
                "seed": int(args.sample_split_seed),
                "validation_percent": int(args.sample_validation_percent),
                "unit": "complete_oracle_context_sample",
            },
            "heldout_workloads": sorted(HELDOUT_WORKLOADS),
            "sample_level_random_split": False,
        },
        "input_format": args.input_format,
        "splits": splits,
    }
    manifest_path = os.path.join(args.out, "manifest.json")
    dump_json(manifest_path, manifest)
    print(
        f"[plan] status={quality['status']} train={len(splits['train'])} "
        f"validation={len(splits['validation'])} "
        f"test={len(splits['test_mechanism']) + len(splits['test_business'])}"
    )
    for blocker in quality["blockers"]:
        print(f"[quality][BLOCK] {blocker}")
    print(f"[plan] manifest={manifest_path}")

    if not args.build:
        return 0
    if quality["status"] != "pass" and not args.allow_provisional:
        print("[build] refused: fix raw cube or pass --allow-provisional", file=sys.stderr)
        return 2

    build_entries = [
        entry for split in (
            "train", "validation", "test_mechanism", "test_business",
            "deployment_inference",
        )
        for entry in splits[split]
    ]
    unique_build_entries: List[Dict[str, Any]] = []
    seen_rollouts = set()
    for entry in build_entries:
        path = entry["rollout_dir"]
        if path not in seen_rollouts:
            seen_rollouts.add(path)
            unique_build_entries.append(entry)
    build_entries = unique_build_entries
    if args.max_traces > 0:
        build_entries = build_entries[:args.max_traces]
    pending: List[tuple[int, Dict[str, Any]]] = []
    for index, entry in enumerate(build_entries, 1):
        meta_path = os.path.join(entry["rollout_dir"], "meta.json")
        if args.skip_existing and os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
                packed = existing.get("packed", {})
                resource_path = os.path.join(
                    entry["rollout_dir"], packed.get("relative_dir", "packed"),
                    "resource.npy",
                )
                reusable = (
                    existing.get("feature_schema") == FEATURE_SCHEMA_VERSION
                    and packed.get("schema_version") == PACKED_SCHEMA_VERSION
                    and os.path.isfile(resource_path)
                )
            except (OSError, ValueError, TypeError):
                reusable = False
            if reusable:
                print(f"[build {index}/{len(build_entries)}] skip {entry['workload']} c{entry['n_cores']:02d}")
                continue
            print(
                f"[build {index}/{len(build_entries)}] rebuild stale cache "
                f"{entry['workload']} c{entry['n_cores']:02d}"
            )
        pending.append((index, entry))

    if args.workers < 1:
        raise SystemExit("--workers must be >= 1")
    if args.workers == 1:
        for index, entry in pending:
            print(f"[build {index}/{len(build_entries)}] {entry['workload']} c{entry['n_cores']:02d}")
            _build_one(entry, args.config, args.input_format)
        return 0

    print(f"[build] workers={args.workers} pending={len(pending)}")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_build_one, entry, args.config, args.input_format): (index, entry)
            for index, entry in pending
        }
        for future in as_completed(futures):
            index, entry = futures[future]
            future.result()
            print(
                f"[build {index}/{len(build_entries)}] done "
                f"{entry['workload']} c{entry['n_cores']:02d}",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
