#!/usr/bin/env python3
"""Audit raw_v27 workload/core-count coverage and sampled label distributions.

The audit reads Parquet metadata for every main ``W_*`` workload and, unless
``--metadata-only`` is set, samples deterministic regions across each core.  It never
loads timing oracle columns as model features; timing is used only for label
distribution diagnostics.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, List, Sequence

import numpy as np
import pyarrow.parquet as pq

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.chunker.functional_features import (
    RAW_TRACE_SCHEMA_VERSION,
    load_uarch_profile,
    predictor_hash,
    tick_per_cycle_from_profile,
    uarch_hash,
)


CORE_RE = re.compile(r"(?:switch|cores)(\d*)\.core")
CYC_RE = re.compile(r"board\.processor\.(?:switch|cores)(\d*)\.core\.numCycles\s+(\d+)")
ROOT_RE = re.compile(r"(?:^|_)c(\d+)(?:_|$)")
V28_REQUIRED_COLUMNS = {
    "paddr", "cacheline_paddr", "mispredicted",
    "branch_taken", "branch_target", "branch_next_pc", "branch_history",
    "is_branch_cond", "is_branch_indirect", "is_call", "is_return",
}

PLANNED_TRAIN = {
    "W_int_alu_dense", "W_int_div_serial", "W_fp_alu_dense",
    "W_simd_sse_dense",
    "W_stream_seq_L2", "W_stream_seq_DRAM", "W_random_DRAM",
    "W_chase_DRAM",
    "W_coh_read_share", "W_coh_write_share", "W_coh_false_share",
    "W_coh_asym_rw",
    "W_phase_coh_onset",
    "W_skew_hot_cold", "W_ranking_mix_private",
}
PLANNED_HELDOUT = {"W_phase_coh_decay"}


def _core_id(path: str) -> int:
    match = CORE_RE.search(os.path.basename(path))
    return int(match.group(1) or 0) if match else 0


def _quantile(values: Sequence[float], q: float) -> float:
    if not values:
        return float("nan")
    return float(np.quantile(np.asarray(values, dtype=np.float64), q))


def _read_cycles(path: str) -> Dict[int, int]:
    out: Dict[int, int] = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            match = CYC_RE.search(line)
            if match:
                out[int(match.group(1) or 0)] = int(match.group(2))
    return out


def _label_span_cycles(parquet_path: str, tpc: float) -> float:
    """Trace-visible fetch-to-final-commit cycles from Parquet statistics."""
    pf = pq.ParquetFile(parquet_path)
    fetch_min: Any = None
    commit_max: Any = None
    for rg_idx in range(pf.num_row_groups):
        rg = pf.metadata.row_group(rg_idx)
        for col_idx in range(rg.num_columns):
            col = rg.column(col_idx)
            stats = col.statistics
            if stats is None or not stats.has_min_max:
                continue
            if col.path_in_schema == "fetch_tick":
                value = int(stats.min)
                fetch_min = value if fetch_min is None else min(fetch_min, value)
            elif col.path_in_schema == "commit_tick":
                value = int(stats.max)
                commit_max = value if commit_max is None else max(commit_max, value)
    if fetch_min is None or commit_max is None or commit_max <= fetch_min:
        return float("nan")
    return float(commit_max - fetch_min) / max(1e-12, float(tpc))


def _signature(
    columns: Dict[str, np.ndarray], start: int, end: int, address_mode: str,
) -> str:
    digest = hashlib.sha1()
    names = ["op_class", "is_load", "is_store", "is_atomic", "is_branch", "n_src", "n_dst"]
    if address_mode == "absolute":
        names.append("vaddr")
    for name in names:
        digest.update(np.ascontiguousarray(columns[name][start:end]).tobytes())
    if address_mode == "normalized":
        addr = columns["vaddr"][start:end].astype(np.uint64, copy=False)
        mem = (
            columns["is_load"][start:end]
            | columns["is_store"][start:end]
            | columns["is_atomic"][start:end]
        ).astype(bool)
        normalized = np.zeros(len(addr), dtype=np.int64)
        if np.any(mem):
            lines = (addr[mem] >> np.uint64(6)).astype(np.int64, copy=False)
            normalized[mem] = lines - lines[0]
        digest.update(normalized.tobytes())
    return digest.hexdigest()


def _region_indices(n_row_groups: int, n_regions: int) -> List[int]:
    if n_row_groups <= 0:
        return []
    n = max(1, min(int(n_regions), n_row_groups))
    return sorted(set(int(round(x)) for x in np.linspace(0, n_row_groups - 1, n)))


def _sample_core(
    parquet_path: str, K: int, max_uops: int, tpc: float, n_regions: int,
) -> Dict[str, Any]:
    pf = pq.ParquetFile(parquet_path)
    names = [
        "commit_tick", "op_class", "is_load", "is_store", "is_atomic",
        "is_branch", "is_branch_cond", "is_branch_indirect", "is_call",
        "is_return", "branch_taken", "branch_target", "branch_next_pc",
        "branch_history", "mispredicted", "n_src", "n_dst", "vaddr", "paddr",
    ]
    regions: List[Dict[str, Any]] = []
    totals = Counter()
    total_rows = 0
    distinct_line_sum = 0.0
    op_hist = np.zeros(90, dtype=np.int64)
    branch_invariant_violations = 0
    paddr_valid_mem = 0
    total_mem = 0
    for rg in _region_indices(pf.num_row_groups, n_regions):
        raw_table = pf.read_row_group(rg, columns=names)
        start_row = max(0, (len(raw_table) - max_uops) // 2)
        table = raw_table.slice(start_row, max_uops)
        cols = {name: table[name].to_numpy(zero_copy_only=False) for name in names}
        n = len(cols["commit_tick"])
        n_chunks = n // K
        cpis: List[float] = []
        noaddr: List[str] = []
        withaddr: List[str] = []
        normalized: List[str] = []
        for chunk in range(n_chunks):
            start = chunk * K
            end = start + K
            noaddr.append(_signature(cols, start, end, "none"))
            withaddr.append(_signature(cols, start, end, "absolute"))
            normalized.append(_signature(cols, start, end, "normalized"))
            if chunk > 0:
                prev_tick = int(cols["commit_tick"][start - 1])
                end_tick = int(cols["commit_tick"][end - 1])
                if end_tick > prev_tick:
                    cpis.append((end_tick - prev_tick) / tpc / K)
                else:
                    cpis.append(float("nan"))
            else:
                cpis.append(float("nan"))
        regions.append({
            "row_group": rg,
            "cpi": cpis,
            "signature_noaddr": noaddr,
            "signature_withaddr": withaddr,
            "signature_normalized": normalized,
        })
        for key in ("is_load", "is_store", "is_atomic", "is_branch"):
            totals[key] += int(np.sum(cols[key]))
        vals, counts = np.unique(cols["op_class"], return_counts=True)
        for value, count in zip(vals, counts):
            if 0 <= int(value) < len(op_hist):
                op_hist[int(value)] += int(count)
        mem = cols["is_load"] + cols["is_store"] + cols["is_atomic"]
        branch = cols["is_branch"].astype(bool)
        subtype = (
            cols["is_branch_cond"] | cols["is_branch_indirect"]
            | cols["is_call"] | cols["is_return"]
        ).astype(bool)
        taken = cols["branch_taken"].astype(bool)
        branch_invariant_violations += int(np.sum(subtype & ~branch))
        branch_invariant_violations += int(np.sum(cols["mispredicted"].astype(bool) & ~branch))
        branch_invariant_violations += int(np.sum(taken & ~branch))
        branch_invariant_violations += int(np.sum(
            branch & taken & (cols["branch_target"] != cols["branch_next_pc"])
        ))
        branch_invariant_violations += int(np.sum(
            branch & ~taken & (cols["branch_target"] != 0)
        ))
        total_mem += int(np.sum(mem > 0))
        paddr_valid_mem += int(np.sum((mem > 0) & (cols["paddr"] != 0)))
        line = (cols["vaddr"].astype(np.uint64) >> np.uint64(6))[mem > 0]
        distinct_line_sum += float(len(np.unique(line)))
        total_rows += n

    digest_noaddr = hashlib.sha1()
    digest_normalized = hashlib.sha1()
    for region in regions:
        for value in region["signature_noaddr"]:
            digest_noaddr.update(value.encode("ascii"))
        for value in region["signature_normalized"]:
            digest_normalized.update(value.encode("ascii"))
    return {
        "regions": regions,
        "digest_noaddr": digest_noaddr.hexdigest(),
        "digest_normalized": digest_normalized.hexdigest(),
        "load_frac": totals["is_load"] / max(1, total_rows),
        "store_frac": totals["is_store"] / max(1, total_rows),
        "atomic_frac": totals["is_atomic"] / max(1, total_rows),
        "branch_frac": totals["is_branch"] / max(1, total_rows),
        "distinct_lines_per_uop": distinct_line_sum / max(1, total_rows),
        "opclass_hist": (op_hist / max(1, int(op_hist.sum()))).tolist(),
        "branch_invariant_violations": branch_invariant_violations,
        "paddr_valid_mem_frac": paddr_valid_mem / max(1, total_mem),
    }


def audit_workload(
    path: str, expected_cores: int, K: int, max_uops: int,
    n_regions: int, metadata_only: bool,
) -> Dict[str, Any]:
    workload = os.path.basename(path)
    trace_dir = os.path.join(path, "tao_trace")
    parquets = sorted(glob.glob(os.path.join(trace_dir, "*.aligned.parquet")), key=_core_id)
    missing_columns = {}
    for parquet_path in parquets:
        available = set(pq.ParquetFile(parquet_path).schema_arrow.names)
        missing = sorted(V28_REQUIRED_COLUMNS - available)
        if missing:
            missing_columns[str(_core_id(parquet_path))] = missing
    roi_path = os.path.join(trace_dir, "roi_boundaries.jsonl")
    roi_events: Dict[int, Counter] = defaultdict(Counter)
    if os.path.isfile(roi_path):
        with open(roi_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    event = json.loads(line)
                    roi_events[int(event.get("core_id", -1))][str(event.get("event", ""))] += 1
    rows = {_core_id(p): int(pq.ParquetFile(p).metadata.num_rows) for p in parquets}
    cycles = _read_cycles(os.path.join(path, "stats.txt"))
    profile = load_uarch_profile(trace_dir)
    try:
        branch_predictor_hash = predictor_hash(profile)
    except RuntimeError:
        branch_predictor_hash = ""
    tpc = tick_per_cycle_from_profile(profile)
    label_cycles = {
        _core_id(p): _label_span_cycles(p, tpc) for p in parquets
    }
    full_cpis = [cycles[c] / rows[c] for c in sorted(set(rows) & set(cycles)) if rows[c] > 0]
    label_cpis = [
        label_cycles[c] / rows[c] for c in sorted(set(rows) & set(label_cycles))
        if rows[c] > 0 and math.isfinite(label_cycles[c])
    ]
    result: Dict[str, Any] = {
        "workload": workload,
        "expected_cores": expected_cores,
        "trace_cores": len(parquets),
        "complete": len(parquets) == expected_cores,
        "stats_complete": len(cycles) == expected_cores,
        "rows_min": min(rows.values()) if rows else 0,
        "rows_max": max(rows.values()) if rows else 0,
        "rows_total": sum(rows.values()),
        "full_cpi_mean": (
            float(sum(cycles.values()) / max(1, sum(rows.values())))
            if rows and cycles else float("nan")
        ),
        "full_cpi_core_min": min(full_cpis) if full_cpis else float("nan"),
        "full_cpi_core_max": max(full_cpis) if full_cpis else float("nan"),
        "full_cpi_core_cv": statistics.pstdev(full_cpis) / max(1e-12, statistics.mean(full_cpis)) if len(full_cpis) > 1 else 0.0,
        "label_span_cpi_mean": (
            float(sum(label_cycles[c] for c in set(rows) & set(label_cycles)
                      if math.isfinite(label_cycles[c])) / max(1, sum(
                          rows[c] for c in set(rows) & set(label_cycles)
                          if math.isfinite(label_cycles[c])
                      ))) if rows else float("nan")
        ),
        "label_span_cpi_core_min": min(label_cpis) if label_cpis else float("nan"),
        "label_span_cpi_core_max": max(label_cpis) if label_cpis else float("nan"),
        "label_span_cpi_core_cv": (
            statistics.pstdev(label_cpis) / max(1e-12, statistics.mean(label_cpis))
            if len(label_cpis) > 1 else 0.0
        ),
        "tick_per_cycle": tpc,
        "uarch_hash": uarch_hash(profile),
        "raw_trace_schema": RAW_TRACE_SCHEMA_VERSION,
        "schema_valid": not missing_columns,
        "missing_columns_by_core": missing_columns,
        "roi_boundary_valid": bool(parquets) and all(
            roi_events[core]["begin"] == 1 and roi_events[core]["end"] == 1
            for core in rows
        ),
        "predictor_hash": branch_predictor_hash,
        "predictor_profile_valid": bool(branch_predictor_hash),
        "profile_l2_size_b": int(profile.get("cache", {}).get("l2", {}).get("size_b", 0)),
        "profile_l3_size_b": int(profile.get("cache", {}).get("l3", {}).get("size_b", 0)),
        "profile_l3_num_banks": int(profile.get("cache", {}).get("l3", {}).get("num_banks", 0)),
        "profile_dram_num_channels": int(profile.get("dram", {}).get("num_channels", 0)),
    }
    if metadata_only or not parquets or missing_columns:
        return result

    sampled = [_sample_core(p, K, max_uops, tpc, n_regions) for p in parquets]
    all_cpi = [
        float(v) for item in sampled for region in item["regions"] for v in region["cpi"]
        if math.isfinite(float(v)) and float(v) > 0
    ]
    cross_std: List[float] = []
    baseline_abs: List[float] = []
    identifiable_centered: List[float] = []
    identifiable_slow_kl: List[float] = []
    noaddr_identical = 0
    withaddr_identical = 0
    valid_aligned = 0
    region_cpi_p50: List[float] = []
    n_common_regions = min(len(item["regions"]) for item in sampled)
    for region_idx in range(n_common_regions):
        region_values: List[float] = []
        n_chunks = min(len(item["regions"][region_idx]["cpi"]) for item in sampled)
        for idx in range(1, n_chunks):
            ys = [float(item["regions"][region_idx]["cpi"][idx]) for item in sampled]
            region_values.extend(y for y in ys if math.isfinite(y) and y > 0)
            if all(math.isfinite(y) and y > 0 for y in ys):
                log_y = np.log(np.asarray(ys, dtype=np.float64))
                cross_std.append(float(np.std(log_y)))
                abs_err = np.abs(log_y)
                baseline_abs.append(float(np.mean(np.where(
                    abs_err <= 0.3, 0.5 * abs_err ** 2,
                    0.3 * (abs_err - 0.15),
                ))))
                valid_aligned += 1
                if len({item["regions"][region_idx]["signature_noaddr"][idx] for item in sampled}) == 1:
                    noaddr_identical += 1
                if len({item["regions"][region_idx]["signature_withaddr"][idx] for item in sampled}) == 1:
                    withaddr_identical += 1
                signatures = [
                    (
                        item["regions"][region_idx]["signature_noaddr"][idx],
                        item["regions"][region_idx]["signature_normalized"][idx],
                    )
                    for item in sampled
                ]
                grouped: Dict[Any, List[int]] = defaultdict(list)
                for core_idx, signature in enumerate(signatures):
                    grouped[signature].append(core_idx)
                if len(grouped) >= 2:
                    group_y = np.asarray([
                        float(np.mean(log_y[indices])) for indices in grouped.values()
                    ])
                    group_w = np.asarray([
                        float(len(indices)) for indices in grouped.values()
                    ])
                    group_w /= group_w.sum()
                    centered_y = group_y - float(np.sum(group_w * group_y))
                    spread = float(np.sqrt(np.sum(group_w * centered_y ** 2)))
                    if spread >= 0.10:
                        center_abs = np.abs(centered_y)
                        identifiable_centered.append(float(np.sum(group_w * np.where(
                            center_abs <= 0.3, 0.5 * center_abs ** 2,
                            0.3 * (center_abs - 0.15),
                        ))))
                        logits = centered_y / 0.30
                        logits -= float(np.max(logits))
                        probs = np.exp(logits)
                        probs /= probs.sum()
                        identifiable_slow_kl.append(float(np.sum(
                            probs * (np.log(np.maximum(probs, 1e-12)) + math.log(len(probs)))
                        )))
        region_cpi_p50.append(_quantile(region_values, 0.50))
    for key in ("load_frac", "store_frac", "atomic_frac", "branch_frac", "distinct_lines_per_uop"):
        result[f"sample_{key}"] = float(np.mean([x[key] for x in sampled]))
    result.update({
        "sample_chunk_count": len(all_cpi),
        "sample_cpi_p10": _quantile(all_cpi, 0.10),
        "sample_cpi_p50": _quantile(all_cpi, 0.50),
        "sample_cpi_p90": _quantile(all_cpi, 0.90),
        "sample_cpi_p99": _quantile(all_cpi, 0.99),
        "sample_cpi_3_10_frac": sum(3.0 <= x < 10.0 for x in all_cpi) / max(1, len(all_cpi)),
        "sample_cpi_ge10_frac": sum(x >= 10.0 for x in all_cpi) / max(1, len(all_cpi)),
        "sample_cpi_ge20_frac": sum(x >= 20.0 for x in all_cpi) / max(1, len(all_cpi)),
        "sample_cpi_ge40_frac": sum(x >= 40.0 for x in all_cpi) / max(1, len(all_cpi)),
        "cross_core_logstd_p50": _quantile(cross_std, 0.50),
        "cross_core_logstd_p90": _quantile(cross_std, 0.90),
        "cross_core_spread_ge010_frac": sum(x >= 0.10 for x in cross_std) / max(1, len(cross_std)),
        "cross_core_spread_ge025_frac": sum(x >= 0.25 for x in cross_std) / max(1, len(cross_std)),
        "identifiable_spread_context_frac": len(identifiable_centered) / max(1, valid_aligned),
        "baseline_abs_log_huber_zero_mean": float(np.mean(baseline_abs)) if baseline_abs else 0.0,
        "baseline_identifiable_centered_huber_mean": (
            float(np.mean(identifiable_centered)) if identifiable_centered else 0.0
        ),
        "baseline_identifiable_slow_kl_mean": (
            float(np.mean(identifiable_slow_kl)) if identifiable_slow_kl else 0.0
        ),
        "noaddr_identical_frac": noaddr_identical / max(1, valid_aligned),
        "withaddr_identical_frac": withaddr_identical / max(1, valid_aligned),
        "sample_region_cpi_p50": region_cpi_p50,
        "sample_core0_noaddr_digest": sampled[0]["digest_noaddr"],
        "sample_core0_normalized_digest": sampled[0]["digest_normalized"],
        "sample_opclass_hist": np.mean(
            np.asarray([x["opclass_hist"] for x in sampled], dtype=np.float64), axis=0,
        ).tolist(),
        "sample_branch_invariant_violations": int(sum(
            item["branch_invariant_violations"] for item in sampled
        )),
        "sample_paddr_valid_mem_frac": float(np.mean([
            item["paddr_valid_mem_frac"] for item in sampled
        ])),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root-glob",
        default="/data00/yinhaolang/TSim/data/raw_v27_0_cold16_seed*_c*",
    )
    parser.add_argument("--K", type=int, default=256)
    parser.add_argument("--sample-uops-per-core", type=int, default=16384)
    parser.add_argument("--sample-regions", type=int, default=7)
    parser.add_argument("--metadata-only", action="store_true")
    parser.add_argument(
        "--contract-file", default="",
        help="optional workload contract JSON overriding the built-in v27 sets",
    )
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    planned_train = set(PLANNED_TRAIN)
    planned_heldout = set(PLANNED_HELDOUT)
    acceptance: Dict[str, float] = {}
    acceptance_overrides: Dict[str, Dict[str, float]] = {}
    if args.contract_file:
        with open(args.contract_file, "r", encoding="utf-8") as fh:
            contract = json.load(fh)
        planned_train = set(contract.get("train", []))
        planned_heldout = set(contract.get("heldout_business", contract.get("heldout", [])))
        acceptance = {
            str(k): float(v) for k, v in dict(contract.get("acceptance", {})).items()
        }
        acceptance_overrides = {
            str(workload): {
                str(k): float(v) for k, v in dict(limits).items()
            }
            for workload, limits in dict(
                contract.get("acceptance_overrides", {})
            ).items()
        }

    roots = sorted(glob.glob(args.root_glob))
    rows: List[Dict[str, Any]] = []
    actual_names = set()
    for root in roots:
        matches = ROOT_RE.findall(os.path.basename(root.rstrip(os.sep)))
        if not matches:
            continue
        n_core = int(matches[-1])
        workload_dirs = sorted(
            p for p in glob.glob(os.path.join(root, "W_*")) if os.path.isdir(p)
        )
        for workload_dir in workload_dirs:
            actual_names.add(os.path.basename(workload_dir))
            row = audit_workload(
                workload_dir, n_core, args.K, args.sample_uops_per_core,
                args.sample_regions,
                args.metadata_only,
            )
            row["root"] = os.path.basename(root)
            rows.append(row)
            print(
                f"[{row['root']}] {row['workload']}: cores={row['trace_cores']}/{n_core} "
                f"uops={row['rows_min']}..{row['rows_max']} CPI={row['full_cpi_mean']:.4g}",
                flush=True,
            )

    exact_pairs: List[Dict[str, Any]] = []
    if not args.metadata_only:
        # Compare distinct workload definitions only within one raw root.  A
        # seed0/seed1 copy of the same workload is expected to be similar and
        # must not be reported as a workload collision.
        by_core: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_core[(str(row.get("root", "")), int(row["expected_cores"]))].append(row)
        for (root_name, n_core), group in sorted(by_core.items()):
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    if group[i]["workload"] == group[j]["workload"]:
                        continue
                    same_noaddr = (
                        group[i].get("sample_core0_noaddr_digest")
                        == group[j].get("sample_core0_noaddr_digest")
                    )
                    same_normalized = (
                        group[i].get("sample_core0_normalized_digest")
                        == group[j].get("sample_core0_normalized_digest")
                    )
                    if same_noaddr or same_normalized:
                        exact_pairs.append({
                            "n_cores": n_core,
                            "root": root_name,
                            "workload_a": group[i]["workload"],
                            "workload_b": group[j]["workload"],
                            "same_noaddr": same_noaddr,
                            "same_normalized_address": same_normalized,
                            "cpi_a": group[i]["full_cpi_mean"],
                            "cpi_b": group[j]["full_cpi_mean"],
                        })
    violations: List[Dict[str, Any]] = []
    in_contract = planned_train | planned_heldout
    for row in rows:
        if row["workload"] not in in_contract:
            continue
        row_acceptance = dict(acceptance)
        row_acceptance.update(acceptance_overrides.get(row["workload"], {}))
        hard_checks = (
            ("complete", bool(row.get("complete"))),
            ("schema_valid", bool(row.get("schema_valid"))),
            ("roi_boundary_valid", bool(row.get("roi_boundary_valid"))),
            ("predictor_profile_valid", bool(row.get("predictor_profile_valid"))),
            (
                "sample_branch_invariant_violations",
                int(row.get("sample_branch_invariant_violations", 0)) == 0,
            ),
        )
        for field, passed in hard_checks:
            if not passed:
                violations.append({
                    "root": row.get("root"), "workload": row["workload"],
                    "field": field, "actual": row.get(field),
                    "gate": "v28.1_hard_contract", "limit": "pass",
                })
        checks = (
            ("rows_min", "records_per_core_min", lambda actual, limit: actual >= limit),
            ("rows_max", "records_per_core_max", lambda actual, limit: actual <= limit),
            ("sample_atomic_frac", "sample_atomic_frac_max", lambda actual, limit: actual <= limit),
            ("sample_cpi_p50", "sample_cpi_p50_max", lambda actual, limit: actual <= limit),
            ("sample_cpi_p99", "sample_cpi_p99_max", lambda actual, limit: actual <= limit),
            ("sample_cpi_ge10_frac", "sample_cpi_ge10_frac_max", lambda actual, limit: actual <= limit),
            ("sample_cpi_ge40_frac", "sample_cpi_ge40_frac_max", lambda actual, limit: actual <= limit),
            ("full_cpi_mean", "full_cpi_mean_max", lambda actual, limit: actual <= limit),
            ("full_cpi_core_max", "full_cpi_per_core_max", lambda actual, limit: actual <= limit),
            ("profile_l2_size_b", "profile_l2_size_b", lambda actual, limit: actual == limit),
            ("profile_l3_size_b", "profile_l3_size_b", lambda actual, limit: actual == limit),
            ("profile_l3_num_banks", "profile_l3_num_banks", lambda actual, limit: actual == limit),
            ("profile_dram_num_channels", "profile_dram_num_channels", lambda actual, limit: actual == limit),
        )
        for field, gate, predicate in checks:
            if gate not in row_acceptance or field not in row:
                continue
            actual = float(row[field])
            limit = float(row_acceptance[gate])
            if not math.isfinite(actual) or not predicate(actual, limit):
                violations.append({
                    "root": row.get("root"), "workload": row["workload"],
                    "field": field, "actual": actual, "gate": gate, "limit": limit,
                })

    report = {
        "roots": roots,
        "n_slices": len(rows),
        "all_complete": all(bool(r["complete"]) for r in rows),
        "actual_workloads": sorted(actual_names),
        "planned_train_missing": sorted(planned_train - actual_names),
        "planned_heldout_missing": sorted(planned_heldout - actual_names),
        "unexpected_main": sorted(actual_names - planned_train - planned_heldout),
        "heldout_present": sorted(actual_names & planned_heldout),
        "acceptance_violations": violations,
        "contract_status": "pass" if not violations else "blocked",
        "exact_functional_pair_collisions": exact_pairs,
        "rows": rows,
    }
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, allow_nan=True)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
