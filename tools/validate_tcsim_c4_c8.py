#!/usr/bin/env python3
"""Batch FastSim validation on TCSim's raw v28.1 multicore corpus."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable


COMPARISON_FIELDS = {
    "macro_cpi": "aggregate_macro_cpi",
    "uop_cpi": "aggregate_uop_cpi",
    "l1d_miss": "l1d_demand_misses",
    "private_l2_miss": "private_l2_demand_misses",
    "cha_llc_lookup": "cha_llc_lookups_vs_shared_llc_demand_accesses",
    "branch_miss": "branch_misses",
    "dtlb_access": "dtlb_accesses",
    "dtlb_miss": "dtlb_misses",
    "o3_iq_full": "o3_rename_iq_full_events",
    "llc_tag_vs_functional_path": "llc_tag_misses_vs_functional_memory_path",
    "llc_tag_vs_ruby_demand_miss": (
        "shared_llc_tag_misses_vs_ruby_demand_misses"
    ),
}
COUNT_METRICS = {
    "l1d_miss",
    "private_l2_miss",
    "cha_llc_lookup",
    "branch_miss",
    "dtlb_access",
    "dtlb_miss",
    "o3_iq_full",
    "llc_tag_vs_functional_path",
    "llc_tag_vs_ruby_demand_miss",
}
BUSINESS_BASE_PREFIXES = (
    "W_v28_bvc_encoder_",
    "W_v28_flink_",
    "W_v28_gofeed_",
    "W_v28_marine_",
    "W_v28_mysql_",
    "W_v28_pytorch_",
    "W_v28_redis_",
)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def write_run_manifest(
    output: Path,
    project: Path,
    fastsim: Path,
    config: Path,
    raw_roots: dict[int, Path],
    workloads: list[str],
    reuse_existing: bool,
) -> None:
    identity_paths = {
        "fastsim_binary": fastsim,
        "config": config,
        "simulator_source": project / "src" / "simulator.cpp",
        "stats_schema": project / "include" / "fastsim" / "types.hpp",
        "validation_tool": Path(__file__).resolve(),
    }
    cmake_cache = project / "build" / "CMakeCache.txt"
    if cmake_cache.is_file():
        identity_paths["cmake_cache"] = cmake_cache
    missing = [str(path) for path in identity_paths.values()
               if not path.is_file()]
    if missing:
        raise SystemExit(
            "cannot record validation identity; missing: "
            + ", ".join(missing)
        )
    manifest = {
        "schema": "fastsim-validation-run-identity-1",
        "created_at": datetime.now().astimezone().isoformat(),
        "command": [sys.executable, *sys.argv],
        "reuse_existing": reuse_existing,
        "inputs": {
            name: {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            for name, path in identity_paths.items()
        },
        "raw_data_roots": {
            str(cores): str(path) for cores, path in raw_roots.items()
        },
        "workloads": workloads,
    }
    (output / "run-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )


def comparison_map(validation: dict) -> dict[str, dict]:
    return {item["name"]: item for item in validation["comparison"]}


def classify_domain(workload: str) -> str:
    if workload.endswith("_heldout"):
        return "heldout"
    if workload.endswith("_base") and workload.startswith(
        BUSINESS_BASE_PREFIXES
    ):
        return "business_base"
    return "mechanism"


def load_case(case_dir: Path, cores: int, workload: str) -> dict:
    validation = json.loads((case_dir / "validation.json").read_text())
    stats = json.loads((case_dir / "fastsim-stats.json").read_text())
    comparisons = comparison_map(validation)
    frontier = stats["causal_frontier"]
    totals = stats["totals"]
    accepted_uops = frontier.get(
        "interval_accepted_uops",
        frontier.get("interval_committed_uops", 0),
    )
    batch_events = frontier.get("batch_memory_events", 0)
    private_events = frontier.get("interval_private_memory_events", 0)
    escape_events = frontier.get("interval_escape_memory_events", 0)
    row = {
        "cores": cores,
        "workload": workload,
        "domain": classify_domain(workload),
        "fastsim_mips": stats["throughput"]["mips"],
        "fastsim_uops_per_second": stats["throughput"]["uops_per_second"],
        "frontier_waits": frontier["frontier_waits"],
        "interval_steps": frontier.get("interval_steps", 0),
        "interval_zero_progress_steps": frontier.get(
            "interval_zero_progress_steps", 0
        ),
        "interval_accepted_uops": accepted_uops,
        "interval_active_prefixes": frontier.get(
            "interval_active_prefixes", 0
        ),
        "max_interval_accepted_uops": frontier.get(
            "max_interval_accepted_uops",
            frontier.get("max_interval_committed_uops", 0),
        ),
        "epoch_lookahead_chunks": frontier.get(
            "epoch_lookahead_chunks", 0
        ),
        "epoch_inflight_memory_uops": frontier.get(
            "epoch_inflight_memory_uops", 0
        ),
        "epoch_corrected_horizon_violations": frontier.get(
            "epoch_corrected_horizon_violations", 0
        ),
        "epoch_corrected_issue_horizon_events": frontier.get(
            "epoch_corrected_issue_horizon_events", 0
        ),
        "epoch_corrected_issue_horizon_uops": frontier.get(
            "epoch_corrected_issue_horizon_uops", 0
        ),
        "epoch_corrected_issue_horizon_cycles": frontier.get(
            "epoch_corrected_issue_horizon_cycles", 0
        ),
        "epoch_corrected_issue_horizon_max_cycles": frontier.get(
            "epoch_corrected_issue_horizon_max_cycles", 0
        ),
        "epoch_advanced_cycles": frontier.get(
            "epoch_advanced_cycles", 0
        ),
        "batch_memory_events": batch_events,
        "interval_private_memory_events": private_events,
        "interval_escape_memory_events": escape_events,
        "retired_uops": totals["retired_uops"],
        "memory_accesses": totals["memory_accesses"],
        "uop_conservation_ok": accepted_uops == totals["retired_uops"],
        "memory_event_conservation_ok": (
            batch_events == totals["memory_accesses"]
        ),
        "memory_partition_ok": (
            private_events + escape_events == batch_events
        ),
        "private_preview_epochs": frontier.get(
            "private_preview_epochs", 0
        ),
        "private_preview_partial_epochs": frontier.get(
            "private_preview_partial_epochs", 0
        ),
        "private_preview_events": frontier.get(
            "private_preview_events", 0
        ),
        "private_preview_unsafe_events": frontier.get(
            "private_preview_unsafe_events", 0
        ),
        "private_preview_safe_cores": frontier.get(
            "private_preview_safe_cores", 0
        ),
        "private_preview_unsafe_cores": frontier.get(
            "private_preview_unsafe_cores", 0
        ),
        "private_preview_bypass_epochs": frontier.get(
            "private_preview_bypass_epochs", 0
        ),
        "private_preview_bypass_events": frontier.get(
            "private_preview_bypass_events", 0
        ),
        "materialized_escape_events": frontier.get(
            "materialized_escape_events", 0
        ),
        "state_certificate_failures": frontier.get(
            "state_certificate_failures", 0
        ),
        "state_certificate_wall_ns": frontier.get(
            "state_certificate_wall_ns", 0
        ),
        "timing_certificate_failures": frontier.get(
            "timing_certificate_failures", 0
        ),
        "timing_reweave_passes": frontier.get(
            "timing_reweave_passes", 0
        ),
        "replayed_shared_events": frontier.get(
            "replayed_shared_events", 0
        ),
        "canonical_fallback_epochs": frontier.get(
            "canonical_fallback_epochs", 0
        ),
        "corrected_arrival_candidate_epochs": frontier.get(
            "corrected_arrival_candidate_epochs", 0
        ),
        "corrected_arrival_conflict_components": frontier.get(
            "corrected_arrival_conflict_components", 0
        ),
        "corrected_arrival_component_events": frontier.get(
            "corrected_arrival_component_events", 0
        ),
        "corrected_arrival_max_component_events": frontier.get(
            "corrected_arrival_max_component_events", 0
        ),
        "corrected_arrival_replay_epochs": frontier.get(
            "corrected_arrival_replay_epochs", 0
        ),
        "corrected_arrival_replayed_events": frontier.get(
            "corrected_arrival_replayed_events", 0
        ),
        "corrected_arrival_stable_epochs": frontier.get(
            "corrected_arrival_stable_epochs", 0
        ),
        "corrected_arrival_fallback_epochs": frontier.get(
            "corrected_arrival_fallback_epochs", 0
        ),
        "causal_timing_candidate_epochs": frontier.get(
            "causal_timing_candidate_epochs", 0
        ),
        "causal_timing_noop_epochs": frontier.get(
            "causal_timing_noop_epochs", 0
        ),
        "causal_timing_stable_epochs": frontier.get(
            "causal_timing_stable_epochs", 0
        ),
        "causal_timing_fallback_epochs": frontier.get(
            "causal_timing_fallback_epochs", 0
        ),
        "causal_timing_deferred_epochs": frontier.get(
            "causal_timing_deferred_epochs", 0
        ),
        "causal_closure_components": frontier.get(
            "causal_closure_components", 0
        ),
        "causal_closure_events": frontier.get(
            "causal_closure_events", 0
        ),
        "causal_max_closure_events": frontier.get(
            "causal_max_closure_events", 0
        ),
        "causal_timing_passes": frontier.get(
            "causal_timing_passes", 0
        ),
        "causal_timing_replayed_events": frontier.get(
            "causal_timing_replayed_events", 0
        ),
        "causal_timing_wall_ns": frontier.get(
            "causal_timing_wall_ns", 0
        ),
        "dram_frfcfs_candidate_epochs": frontier.get(
            "dram_frfcfs_candidate_epochs", 0
        ),
        "dram_frfcfs_bypass_epochs": frontier.get(
            "dram_frfcfs_bypass_epochs", 0
        ),
        "dram_frfcfs_bypass_requests": frontier.get(
            "dram_frfcfs_bypass_requests", 0
        ),
        "dram_frfcfs_candidate_queue_cycles": frontier.get(
            "dram_frfcfs_candidate_queue_cycles", 0
        ),
        "dram_frfcfs_bypass_queue_cycles": frontier.get(
            "dram_frfcfs_bypass_queue_cycles", 0
        ),
        "dram_frfcfs_selection_window_sum": frontier.get(
            "dram_frfcfs_selection_window_sum", 0
        ),
        "dram_frfcfs_selection_window_max": frontier.get(
            "dram_frfcfs_selection_window_max", 0
        ),
        "dram_frfcfs_effective_selection_window": frontier.get(
            "dram_frfcfs_effective_selection_window", 0
        ),
        "dram_frfcfs_stable_epochs": frontier.get(
            "dram_frfcfs_stable_epochs", 0
        ),
        "dram_frfcfs_fallback_epochs": frontier.get(
            "dram_frfcfs_fallback_epochs", 0
        ),
        "dram_frfcfs_requests": frontier.get(
            "dram_frfcfs_requests", 0
        ),
        "dram_frfcfs_passes": frontier.get(
            "dram_frfcfs_passes", 0
        ),
        "dram_frfcfs_reordered_requests": frontier.get(
            "dram_frfcfs_reordered_requests", 0
        ),
        "dram_frfcfs_row_hits": frontier.get(
            "dram_frfcfs_row_hits", 0
        ),
        "dram_frfcfs_row_misses": frontier.get(
            "dram_frfcfs_row_misses", 0
        ),
        "dram_frfcfs_max_pending": frontier.get(
            "dram_frfcfs_max_pending", 0
        ),
        "dram_frfcfs_max_admitted_pending": frontier.get(
            "dram_frfcfs_max_admitted_pending", 0
        ),
        "dram_frfcfs_saturated_selections": frontier.get(
            "dram_frfcfs_saturated_selections", 0
        ),
        "dram_frfcfs_page_policy_scanned_requests": frontier.get(
            "dram_frfcfs_page_policy_scanned_requests", 0
        ),
        "dram_frfcfs_outside_window_row_hits": frontier.get(
            "dram_frfcfs_outside_window_row_hits", 0
        ),
        "dram_frfcfs_outside_window_bank_conflicts": frontier.get(
            "dram_frfcfs_outside_window_bank_conflicts", 0
        ),
        "dram_frfcfs_row_cap_precharges": frontier.get(
            "dram_frfcfs_row_cap_precharges", 0
        ),
        "dram_frfcfs_adaptive_precharges": frontier.get(
            "dram_frfcfs_adaptive_precharges", 0
        ),
        "dram_frfcfs_wall_ns": frontier.get(
            "dram_frfcfs_wall_ns", 0
        ),
        "max_batch_memory_events": frontier.get(
            "max_batch_memory_events", 0
        ),
        "reordered_memory_event_pairs": frontier.get(
            "reordered_memory_event_pairs", 0
        ),
        "same_line_reordered_pairs": frontier.get(
            "same_line_reordered_pairs", 0
        ),
        "sparse_scoreboard_seeds": frontier.get(
            "sparse_scoreboard_seeds", 0
        ),
        "sparse_scoreboard_materialized_uops": frontier.get(
            "sparse_scoreboard_materialized_uops", 0
        ),
        "sparse_scoreboard_absorbed_edges": frontier.get(
            "sparse_scoreboard_absorbed_edges", 0
        ),
        "sparse_scoreboard_cross_epoch_edges": frontier.get(
            "sparse_scoreboard_cross_epoch_edges", 0
        ),
        "sparse_scoreboard_rob_crossings": frontier.get(
            "sparse_scoreboard_rob_crossings", 0
        ),
        "sparse_scoreboard_lq_crossings": frontier.get(
            "sparse_scoreboard_lq_crossings", 0
        ),
        "sparse_scoreboard_sq_crossings": frontier.get(
            "sparse_scoreboard_sq_crossings", 0
        ),
        "sparse_resource_candidates": frontier.get(
            "sparse_resource_candidates", 0
        ),
        "sparse_resource_issue_moves": frontier.get(
            "sparse_resource_issue_moves", 0
        ),
        "sparse_resource_issue_collision_cycles": frontier.get(
            "sparse_resource_issue_collision_cycles", 0
        ),
        "sparse_resource_writeback_moves": frontier.get(
            "sparse_resource_writeback_moves", 0
        ),
        "sparse_resource_writeback_collision_cycles": frontier.get(
            "sparse_resource_writeback_collision_cycles", 0
        ),
        "response_activity_candidates": frontier.get(
            "response_activity_candidates", 0
        ),
        "response_activity_certified_segments": frontier.get(
            "response_activity_certified_segments", 0
        ),
        "response_activity_certified_uops": frontier.get(
            "response_activity_certified_uops", 0
        ),
        "response_activity_fallback_segments": frontier.get(
            "response_activity_fallback_segments", 0
        ),
        "branch_outcome_coverage": validation["input_coverage"][
            "branch_outcome_coverage"
        ],
        "per_core_uop_cpi_mape": validation["per_core"]["uop_cpi_mape"],
        "per_core_dtlb_access_mape": validation["per_core"].get(
            "dtlb_access_mape"
        ),
        "per_core_dtlb_miss_mape": validation["per_core"].get(
            "dtlb_miss_mape"
        ),
        "per_core_o3_iq_full_mape": validation["per_core"].get(
            "o3_rename_iq_full_mape"
        ),
    }
    for field in (
        "response_critical_total_cycles",
        "response_critical_rename_free_list_cycles",
        "response_critical_dispatch_bandwidth_cycles",
        "response_critical_rob_capacity_cycles",
        "response_critical_iq_capacity_cycles",
        "response_critical_lq_capacity_cycles",
        "response_critical_sq_capacity_cycles",
        "response_critical_dependency_cycles",
        "response_critical_sequencer_cycles",
        "response_critical_l1_mshr_cycles",
        "response_critical_l2_mshr_cycles",
        "response_critical_memory_response_cycles",
        "response_critical_commit_bandwidth_cycles",
        "response_critical_tso_store_cycles",
        "response_critical_unattributed_cycles",
        "response_critical_conserved",
    ):
        row[field] = totals.get(field, 0)
    for short_name, comparison_name in COMPARISON_FIELDS.items():
        item = comparisons.get(comparison_name)
        row[f"{short_name}_fastsim"] = item["fastsim"] if item else None
        row[f"{short_name}_gem5"] = item["reference"] if item else None
        row[f"{short_name}_signed_error"] = (
            item["signed_relative_error"] if item else None
        )
        row[f"{short_name}_absolute_error"] = (
            item["absolute_relative_error"] if item else None
        )
    return row


def percentile(values: list[float], fraction: float) -> float | None:
    """Return the linearly interpolated sample percentile (R/NumPy type 7)."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * fraction
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    weight = rank - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def aggregate_rows(
    rows: list[dict], core_count: int, domain: str | None = None
) -> dict:
    def domain_matches(row: dict) -> bool:
        if domain is None:
            return True
        if domain == "train_base":
            return row["domain"] != "heldout"
        return row["domain"] == domain

    selected = [
        row
        for row in rows
        if row["cores"] == core_count
        and domain_matches(row)
    ]
    metrics = {}
    for short_name in COMPARISON_FIELDS:
        absolute = [
            row[f"{short_name}_absolute_error"]
            for row in selected
            if row[f"{short_name}_absolute_error"] is not None
        ]
        signed = [
            row[f"{short_name}_signed_error"]
            for row in selected
            if row[f"{short_name}_signed_error"] is not None
        ]
        if not absolute:
            continue
        worst = max(
            (
                row for row in selected
                if row[f"{short_name}_absolute_error"] is not None
            ),
            key=lambda row: row[f"{short_name}_absolute_error"],
        )
        metric = {
            "eligible_workloads": len(absolute),
            "workload_equal_mean_absolute_error": statistics.fmean(absolute),
            "median_absolute_error": statistics.median(absolute),
            "p90_absolute_error": percentile(absolute, 0.9),
            "p99_absolute_error": percentile(absolute, 0.99),
            "maximum_absolute_error": max(absolute),
            "worst_workload": worst["workload"],
            "worst_workload_reference": worst[f"{short_name}_gem5"],
            "workload_equal_mean_signed_error": statistics.fmean(signed),
        }
        if short_name in COUNT_METRICS:
            pairs = [
                (row[f"{short_name}_fastsim"], row[f"{short_name}_gem5"])
                for row in selected
                if row[f"{short_name}_fastsim"] is not None
                and row[f"{short_name}_gem5"] is not None
            ]
            total_reference = sum(reference for _, reference in pairs)
            if total_reference:
                metric["count_weighted_absolute_error"] = sum(
                    abs(predicted - reference) for predicted, reference in pairs
                ) / total_reference
                metric["pooled_signed_error"] = (
                    sum(predicted for predicted, _ in pairs) - total_reference
                ) / total_reference
        metrics[short_name] = metric
    per_core_cpi = [
        row["per_core_uop_cpi_mape"]
        for row in selected
        if row["per_core_uop_cpi_mape"] is not None
    ]
    interval_steps = sum(row["interval_steps"] for row in selected)
    interval_uops = sum(row["interval_accepted_uops"] for row in selected)
    active_prefixes = sum(row["interval_active_prefixes"] for row in selected)
    batch_events = sum(row["batch_memory_events"] for row in selected)
    escape_events = sum(
        row["interval_escape_memory_events"] for row in selected
    )
    return {
        "cores": core_count,
        "domain": domain or "all",
        "workloads": len(selected),
        "metrics": metrics,
        "median_fastsim_mips": statistics.median(
            row["fastsim_mips"] for row in selected
        ),
        "median_fastsim_uops_per_second": statistics.median(
            row["fastsim_uops_per_second"] for row in selected
        ),
        "p10_fastsim_uops_per_second": percentile(
            [row["fastsim_uops_per_second"] for row in selected], 0.1
        ),
        "minimum_fastsim_uops_per_second": min(
            row["fastsim_uops_per_second"] for row in selected
        ),
        "minimum_throughput_workload": min(
            selected, key=lambda row: row["fastsim_uops_per_second"]
        )["workload"],
        "total_frontier_waits": sum(row["frontier_waits"] for row in selected),
        "conservation": {
            "uop_mismatches": sum(
                not row["uop_conservation_ok"] for row in selected
            ),
            "memory_event_mismatches": sum(
                not row["memory_event_conservation_ok"] for row in selected
            ),
            "memory_partition_mismatches": sum(
                not row["memory_partition_ok"] for row in selected
            ),
        },
        "interval_audit": {
            "steps": interval_steps,
            "accepted_uops": interval_uops,
            "mean_uops_per_step": (
                interval_uops / interval_steps if interval_steps else 0.0
            ),
            "mean_uops_per_active_prefix": (
                interval_uops / active_prefixes if active_prefixes else 0.0
            ),
            "maximum_uops_per_step": max(
                (row["max_interval_accepted_uops"] for row in selected),
                default=0,
            ),
            "zero_progress_steps": sum(
                row["interval_zero_progress_steps"] for row in selected
            ),
            "lookahead_chunks": sum(
                row["epoch_lookahead_chunks"] for row in selected
            ),
            "inflight_memory_uops": sum(
                row["epoch_inflight_memory_uops"] for row in selected
            ),
            "corrected_horizon_violations": sum(
                row["epoch_corrected_horizon_violations"]
                for row in selected
            ),
            "advanced_cycles": sum(
                row["epoch_advanced_cycles"] for row in selected
            ),
            "batch_memory_events": batch_events,
            "private_memory_events": sum(
                row["interval_private_memory_events"] for row in selected
            ),
            "escape_memory_events": escape_events,
            "private_preview_epochs": sum(
                row["private_preview_epochs"] for row in selected
            ),
            "private_preview_partial_epochs": sum(
                row["private_preview_partial_epochs"] for row in selected
            ),
            "private_preview_events": sum(
                row["private_preview_events"] for row in selected
            ),
            "private_preview_unsafe_events": sum(
                row["private_preview_unsafe_events"] for row in selected
            ),
            "private_preview_safe_cores": sum(
                row["private_preview_safe_cores"] for row in selected
            ),
            "private_preview_unsafe_cores": sum(
                row["private_preview_unsafe_cores"] for row in selected
            ),
            "private_preview_bypass_epochs": sum(
                row["private_preview_bypass_epochs"] for row in selected
            ),
            "private_preview_bypass_events": sum(
                row["private_preview_bypass_events"] for row in selected
            ),
            "materialized_escape_events": sum(
                row["materialized_escape_events"] for row in selected
            ),
            "response_activity_candidates": sum(
                row["response_activity_candidates"] for row in selected
            ),
            "response_activity_certified_segments": sum(
                row["response_activity_certified_segments"]
                for row in selected
            ),
            "response_activity_certified_uops": sum(
                row["response_activity_certified_uops"]
                for row in selected
            ),
            "response_activity_fallback_segments": sum(
                row["response_activity_fallback_segments"]
                for row in selected
            ),
            "state_certificate_failures": sum(
                row["state_certificate_failures"] for row in selected
            ),
            "state_certificate_wall_ns": sum(
                row["state_certificate_wall_ns"] for row in selected
            ),
            "timing_certificate_failures": sum(
                row["timing_certificate_failures"] for row in selected
            ),
            "timing_reweave_passes": sum(
                row["timing_reweave_passes"] for row in selected
            ),
            "replayed_shared_events": sum(
                row["replayed_shared_events"] for row in selected
            ),
            "canonical_fallback_epochs": sum(
                row["canonical_fallback_epochs"] for row in selected
            ),
            "corrected_arrival_candidate_epochs": sum(
                row["corrected_arrival_candidate_epochs"]
                for row in selected
            ),
            "corrected_arrival_conflict_components": sum(
                row["corrected_arrival_conflict_components"]
                for row in selected
            ),
            "corrected_arrival_component_events": sum(
                row["corrected_arrival_component_events"]
                for row in selected
            ),
            "corrected_arrival_max_component_events": max(
                (
                    row["corrected_arrival_max_component_events"]
                    for row in selected
                ),
                default=0,
            ),
            "corrected_arrival_replay_epochs": sum(
                row["corrected_arrival_replay_epochs"]
                for row in selected
            ),
            "corrected_arrival_replayed_events": sum(
                row["corrected_arrival_replayed_events"]
                for row in selected
            ),
            "corrected_arrival_stable_epochs": sum(
                row["corrected_arrival_stable_epochs"]
                for row in selected
            ),
            "corrected_arrival_fallback_epochs": sum(
                row["corrected_arrival_fallback_epochs"]
                for row in selected
            ),
            "causal_timing_candidate_epochs": sum(
                row["causal_timing_candidate_epochs"] for row in selected
            ),
            "causal_timing_noop_epochs": sum(
                row["causal_timing_noop_epochs"] for row in selected
            ),
            "causal_timing_stable_epochs": sum(
                row["causal_timing_stable_epochs"] for row in selected
            ),
            "causal_timing_fallback_epochs": sum(
                row["causal_timing_fallback_epochs"] for row in selected
            ),
            "causal_timing_deferred_epochs": sum(
                row["causal_timing_deferred_epochs"] for row in selected
            ),
            "causal_closure_components": sum(
                row["causal_closure_components"] for row in selected
            ),
            "causal_closure_events": sum(
                row["causal_closure_events"] for row in selected
            ),
            "causal_max_closure_events": max(
                (row["causal_max_closure_events"] for row in selected),
                default=0,
            ),
            "causal_timing_passes": sum(
                row["causal_timing_passes"] for row in selected
            ),
            "causal_timing_replayed_events": sum(
                row["causal_timing_replayed_events"] for row in selected
            ),
            "causal_timing_wall_ns": sum(
                row["causal_timing_wall_ns"] for row in selected
            ),
            "dram_frfcfs_candidate_epochs": sum(
                row["dram_frfcfs_candidate_epochs"] for row in selected
            ),
            "dram_frfcfs_bypass_epochs": sum(
                row["dram_frfcfs_bypass_epochs"] for row in selected
            ),
            "dram_frfcfs_bypass_requests": sum(
                row["dram_frfcfs_bypass_requests"] for row in selected
            ),
            "dram_frfcfs_candidate_queue_cycles": sum(
                row["dram_frfcfs_candidate_queue_cycles"]
                for row in selected
            ),
            "dram_frfcfs_bypass_queue_cycles": sum(
                row["dram_frfcfs_bypass_queue_cycles"]
                for row in selected
            ),
            "dram_frfcfs_selection_window_sum": sum(
                row["dram_frfcfs_selection_window_sum"]
                for row in selected
            ),
            "dram_frfcfs_selection_window_max": max(
                (
                    row["dram_frfcfs_selection_window_max"]
                    for row in selected
                ),
                default=0,
            ),
            "dram_frfcfs_effective_selection_window": max(
                (
                    row["dram_frfcfs_effective_selection_window"]
                    for row in selected
                ),
                default=0,
            ),
            "dram_frfcfs_stable_epochs": sum(
                row["dram_frfcfs_stable_epochs"] for row in selected
            ),
            "dram_frfcfs_fallback_epochs": sum(
                row["dram_frfcfs_fallback_epochs"] for row in selected
            ),
            "dram_frfcfs_requests": sum(
                row["dram_frfcfs_requests"] for row in selected
            ),
            "dram_frfcfs_passes": sum(
                row["dram_frfcfs_passes"] for row in selected
            ),
            "dram_frfcfs_reordered_requests": sum(
                row["dram_frfcfs_reordered_requests"]
                for row in selected
            ),
            "dram_frfcfs_row_hits": sum(
                row["dram_frfcfs_row_hits"] for row in selected
            ),
            "dram_frfcfs_row_misses": sum(
                row["dram_frfcfs_row_misses"] for row in selected
            ),
            "dram_frfcfs_max_pending": max(
                (row["dram_frfcfs_max_pending"] for row in selected),
                default=0,
            ),
            "dram_frfcfs_max_admitted_pending": max(
                (
                    row["dram_frfcfs_max_admitted_pending"]
                    for row in selected
                ),
                default=0,
            ),
            "dram_frfcfs_saturated_selections": sum(
                row["dram_frfcfs_saturated_selections"]
                for row in selected
            ),
            "dram_frfcfs_page_policy_scanned_requests": sum(
                row["dram_frfcfs_page_policy_scanned_requests"]
                for row in selected
            ),
            "dram_frfcfs_outside_window_row_hits": sum(
                row["dram_frfcfs_outside_window_row_hits"]
                for row in selected
            ),
            "dram_frfcfs_outside_window_bank_conflicts": sum(
                row["dram_frfcfs_outside_window_bank_conflicts"]
                for row in selected
            ),
            "dram_frfcfs_row_cap_precharges": sum(
                row["dram_frfcfs_row_cap_precharges"]
                for row in selected
            ),
            "dram_frfcfs_adaptive_precharges": sum(
                row["dram_frfcfs_adaptive_precharges"]
                for row in selected
            ),
            "dram_frfcfs_wall_ns": sum(
                row["dram_frfcfs_wall_ns"] for row in selected
            ),
            "escape_event_fraction": (
                escape_events / batch_events if batch_events else 0.0
            ),
            "mean_memory_events_per_step": (
                batch_events / interval_steps if interval_steps else 0.0
            ),
            "maximum_memory_events_per_batch": max(
                (row["max_batch_memory_events"] for row in selected),
                default=0,
            ),
            "reordered_memory_event_pairs": sum(
                row["reordered_memory_event_pairs"] for row in selected
            ),
            "same_line_reordered_pairs": sum(
                row["same_line_reordered_pairs"] for row in selected
            ),
        },
        "workload_equal_mean_per_core_uop_cpi_mape": statistics.fmean(
            per_core_cpi
        ),
    }


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.3f}%"


def write_outputs(output: Path, rows: list[dict], core_counts: list[int]) -> None:
    aggregates = [aggregate_rows(rows, cores) for cores in core_counts]
    acceptance_by_core = [
        {
            "cores": aggregate["cores"],
            "uop_cpi_p99_absolute_error": aggregate["metrics"]["uop_cpi"][
                "p99_absolute_error"
            ],
            "minimum_fastsim_uops_per_second": aggregate[
                "minimum_fastsim_uops_per_second"
            ],
            "minimum_throughput_workload": aggregate[
                "minimum_throughput_workload"
            ],
            "cpi_p99_pass": aggregate["metrics"]["uop_cpi"][
                "p99_absolute_error"
            ]
            <= 0.10,
            "throughput_pass": aggregate["minimum_fastsim_uops_per_second"]
            >= 5_000_000,
        }
        for aggregate in aggregates
    ]
    domains = [
        domain
        for domain in ("train_base", "mechanism", "business_base", "heldout")
        if domain == "train_base" or any(row["domain"] == domain for row in rows)
    ]
    domain_aggregates = [
        aggregate_rows(rows, cores, domain)
        for cores in core_counts
        for domain in domains
        if any(
            row["cores"] == cores
            and (
                (domain == "train_base" and row["domain"] != "heldout")
                or row["domain"] == domain
            )
            for row in rows
        )
    ]
    report = {
        "schema": "fastsim-tcsim-v28.1-multicore-validation-5",
        "metric_policy": {
            "cpi": "sum per-core cycles divided by sum retired UOPs/macros",
            "aggregation": "workload-equal; zero-reference metrics are excluded",
            "percentile": (
                "linear interpolation at (N-1)*q (R/NumPy type 7); "
                "P99 is evaluated separately for every core count"
            ),
            "llc": (
                "CHA lookup is a direct Ruby demand-access comparison; LLC tag "
                "miss versus Ruby demand miss remains diagnostic"
            ),
            "o3_iq": (
                "FastSim delayed-dispatch events versus gem5 rename-block/"
                "partial-progress events; reported as a pressure proxy, not "
                "an identical event counter"
            ),
        },
        "acceptance": {
            "uop_cpi_p99_absolute_error_limit": 0.10,
            "minimum_uops_per_second": 5_000_000,
            "per_core_count": acceptance_by_core,
            "all_pass": all(
                item["cpi_p99_pass"] and item["throughput_pass"]
                for item in acceptance_by_core
            ),
        },
        "aggregates": aggregates,
        "domain_aggregates": domain_aggregates,
        "cases": rows,
    }
    (output / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )

    fieldnames = list(rows[0])
    with (output / "summary.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    lines = [
        "# TCSim v28.1 multicore FastSim validation",
        "",
        "All errors below are workload-equal absolute relative errors.",
        "",
        "| Cores | Workloads | UOP CPI mean / median / p90 / p99 / max | "
        "signed bias | per-core CPI MAPE |",
        "|---:|---:|---:|---:|---:|",
    ]
    for aggregate in aggregates:
        metrics = aggregate["metrics"]
        cpi = metrics["uop_cpi"]
        lines.append(
            f"| {aggregate['cores']} | {aggregate['workloads']} | "
            f"{percent(cpi['workload_equal_mean_absolute_error'])} / "
            f"{percent(cpi['median_absolute_error'])} / "
            f"{percent(cpi['p90_absolute_error'])} / "
            f"{percent(cpi['p99_absolute_error'])} / "
            f"{percent(cpi['maximum_absolute_error'])} | "
            f"{percent(cpi['workload_equal_mean_signed_error'])} | "
            f"{percent(aggregate['workload_equal_mean_per_core_uop_cpi_mape'])} |"
        )
    lines.extend(
        [
            "",
            "## Acceptance gates",
            "",
            "CPI P99 and throughput are gated independently for each core count.",
            "",
            "| Cores | CPI P99 (<=10%) | Min UOP/s (>=5M) | Slowest workload | Pass |",
            "|---:|---:|---:|---|:---:|",
        ]
    )
    for item in acceptance_by_core:
        passed = item["cpi_p99_pass"] and item["throughput_pass"]
        lines.append(
            f"| {item['cores']} | "
            f"{percent(item['uop_cpi_p99_absolute_error'])} | "
            f"{item['minimum_fastsim_uops_per_second'] / 1e6:.3f}M | "
            f"{item['minimum_throughput_workload']} | "
            f"{'PASS' if passed else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "## Throughput and conservation",
            "",
            "Throughput is simulator-only wall time. Mismatch columns count "
            "failed cases.",
            "",
            "| Cores | Min / P10 / median UOP/s | Median MIPS | UOP mismatch | "
            "Memory-event mismatch | Private/escape mismatch |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for aggregate in aggregates:
        conservation = aggregate["conservation"]
        lines.append(
            f"| {aggregate['cores']} | "
            f"{aggregate['minimum_fastsim_uops_per_second'] / 1e6:.3f}M / "
            f"{aggregate['p10_fastsim_uops_per_second'] / 1e6:.3f}M / "
            f"{aggregate['median_fastsim_uops_per_second'] / 1e6:.3f}M | "
            f"{aggregate['median_fastsim_mips']:.3f} | "
            f"{conservation['uop_mismatches']} | "
            f"{conservation['memory_event_mismatches']} | "
            f"{conservation['memory_partition_mismatches']} |"
        )
    lines.extend(
        [
            "",
            "## CPI by domain",
            "",
            "| Cores | Domain | Workloads | UOP CPI mean / median / max | signed bias |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    for aggregate in domain_aggregates:
        cpi = aggregate["metrics"]["uop_cpi"]
        lines.append(
            f"| {aggregate['cores']} | {aggregate['domain']} | "
            f"{aggregate['workloads']} | "
            f"{percent(cpi['workload_equal_mean_absolute_error'])} / "
            f"{percent(cpi['median_absolute_error'])} / "
            f"{percent(cpi['maximum_absolute_error'])} | "
            f"{percent(cpi['workload_equal_mean_signed_error'])} |"
        )
    lines.extend(
        [
            "",
            "## PMU count error",
            "",
            "Trace-equal MAPE exposes low-count workloads; WAPE is total absolute "
            "count error divided by the total gem5 count.",
            "",
            "| Cores | Metric | trace-equal MAPE | WAPE | pooled signed | worst workload (reference count) |",
            "|---:|---|---:|---:|---:|---|",
        ]
    )
    for aggregate in aggregates:
        for metric_name in (
            "l1d_miss",
            "private_l2_miss",
            "cha_llc_lookup",
            "branch_miss",
            "dtlb_access",
            "dtlb_miss",
            "o3_iq_full",
            "llc_tag_vs_functional_path",
        ):
            metric = aggregate["metrics"][metric_name]
            lines.append(
                f"| {aggregate['cores']} | {metric_name} | "
                f"{percent(metric['workload_equal_mean_absolute_error'])} | "
                f"{percent(metric.get('count_weighted_absolute_error'))} | "
                f"{percent(metric.get('pooled_signed_error'))} | "
                f"{metric['worst_workload']} ({metric['worst_workload_reference']}) |"
            )
    if any(aggregate["interval_audit"]["steps"] for aggregate in aggregates):
        lines.extend(
            [
                "",
                "## Interval/order audit",
                "",
                "Reordered pairs compare lower-bound weave order with the "
                "dependency-feedback order. Same-line pairs are potential "
                "path-changing conflicts and are not certified as exact.",
                "",
                "| Cores | Steps | UOP/step | UOP/active prefix | Max UOP | "
                "Memory events/step | Escape % | In-flight mem UOP | "
                "Horizon failures | State/timing cert failures | "
                "Shared replay | CA candidates/components | CA replay | "
                "CA stable/fallback | Causal stable/fallback/deferred | "
                "Closure components/events/max | Timing replay | "
                "Activity cert seg/UOP/fallback | "
                "Same-line pairs |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for aggregate in aggregates:
            audit = aggregate["interval_audit"]
            lines.append(
                f"| {aggregate['cores']} | {audit['steps']} | "
                f"{audit['mean_uops_per_step']:.2f} | "
                f"{audit['mean_uops_per_active_prefix']:.2f} | "
                f"{audit['maximum_uops_per_step']} | "
                f"{audit['mean_memory_events_per_step']:.2f} | "
                f"{percent(audit['escape_event_fraction'])} | "
                f"{audit['inflight_memory_uops']} | "
                f"{audit['corrected_horizon_violations']} | "
                f"{audit['state_certificate_failures']}/"
                f"{audit['timing_certificate_failures']} | "
                f"{audit['replayed_shared_events']} | "
                f"{audit['corrected_arrival_candidate_epochs']}/"
                f"{audit['corrected_arrival_conflict_components']} | "
                f"{audit['corrected_arrival_replayed_events']} | "
                f"{audit['corrected_arrival_stable_epochs']}/"
                f"{audit['corrected_arrival_fallback_epochs']} | "
                f"{audit['causal_timing_stable_epochs']}/"
                f"{audit['causal_timing_fallback_epochs']}/"
                f"{audit['causal_timing_deferred_epochs']} | "
                f"{audit['causal_closure_components']}/"
                f"{audit['causal_closure_events']}/"
                f"{audit['causal_max_closure_events']} | "
                f"{audit['causal_timing_replayed_events']} | "
                f"{audit['response_activity_certified_segments']}/"
                f"{audit['response_activity_certified_uops']}/"
                f"{audit['response_activity_fallback_segments']} | "
                f"{audit['same_line_reordered_pairs']} |"
            )
    lines.extend(
        [
            "",
            "Per-workload values and signed errors are in `summary.csv` and "
            "`summary.json`. LLC tag miss versus Ruby protocol demand miss is "
            "diagnostic, not an accepted tag-state accuracy metric.",
            "",
        ]
    )
    (output / "summary.md").write_text("\n".join(lines))


def discover_workloads(raw_roots: Iterable[Path]) -> list[str]:
    workload_sets = [
        {path.name for path in root.glob("W_*") if path.is_dir()}
        for root in raw_roots
    ]
    if not workload_sets:
        return []
    return sorted(set.intersection(*workload_sets))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastsim", default="build/fastsim")
    parser.add_argument("--config", default="configs/gem5-v28_1-c04.cfg")
    parser.add_argument(
        "--raw-data-root", default="/data00/yinhaolang/TSim/data"
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--cores", action="append", type=int)
    parser.add_argument("--workload", action="append")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument(
        "--scratch-dir",
        help="temporary trace root (defaults to FastSim/tmp/validation-scratch)",
    )
    parser.add_argument(
        "--interval-reweave-passes",
        type=int,
        help="override sim.interval_reweave_passes for an experiment",
    )
    parser.add_argument(
        "--interval-max-cycles",
        type=int,
        help="override sim.interval_max_cycles for an experiment",
    )
    parser.add_argument(
        "--cpi-attribution",
        choices=("true", "false"),
        help="enable mutually exclusive response critical-cycle audit",
    )
    parser.add_argument(
        "--interval-private-preview",
        choices=("true", "false"),
        help="override sim.interval_private_preview for an experiment",
    )
    parser.add_argument(
        "--interval-parallel-feedback",
        choices=("true", "false"),
        help="override sim.interval_parallel_feedback for an experiment",
    )
    parser.add_argument(
        "--interval-causal-timing",
        choices=("true", "false"),
        help="override sim.interval_causal_timing for an experiment",
    )
    parser.add_argument(
        "--interval-causal-passes",
        type=int,
        help="override sim.interval_causal_passes for an experiment",
    )
    parser.add_argument(
        "--interval-causal-max-closure-events",
        type=int,
        help=(
            "override sim.interval_causal_max_closure_events for an "
            "experiment"
        ),
    )
    parser.add_argument(
        "--domain-workers",
        type=int,
        help="override sim.domain_workers for an experiment",
    )
    parser.add_argument(
        "--response-rob-lsq-feedback",
        choices=("true", "false"),
        help="override core.response_rob_lsq_feedback for an experiment",
    )
    parser.add_argument(
        "--response-sparse-scoreboard",
        choices=("true", "false"),
        help="override core.response_sparse_scoreboard for an experiment",
    )
    parser.add_argument(
        "--response-sparse-resource-repair",
        choices=("true", "false"),
        help=(
            "override core.response_sparse_resource_repair for an "
            "experiment"
        ),
    )
    parser.add_argument(
        "--response-activity-certificate",
        choices=("true", "false"),
        help=(
            "override core.response_activity_certificate for an "
            "equivalence experiment"
        ),
    )
    parser.add_argument(
        "--branch-shadow-rob",
        choices=("true", "false"),
        help="override branch.shadow_rob for an experiment",
    )
    parser.add_argument(
        "--needs-tso",
        choices=("true", "false"),
        help="override core.needs_tso for an experiment",
    )
    parser.add_argument(
        "--response-retire-exposure",
        type=float,
        help="override core.response_retire_exposure for an experiment",
    )
    parser.add_argument(
        "--domain-min-events",
        type=int,
        help="override sim.domain_min_events for an experiment",
    )
    parser.add_argument(
        "--llc-fill-response-latency",
        type=int,
        help="override uncore.llc_fill_response_latency for an experiment",
    )
    parser.add_argument(
        "--dtlb-miss-model",
        choices=("se_atomic", "timing_walk"),
        help="override dtlb.miss_model for an execution-mode experiment",
    )
    parser.add_argument(
        "--dtlb-page-walk-latency",
        type=int,
        help="override dtlb.page_walk_latency for an experiment",
    )
    parser.add_argument(
        "--dram-scheduler",
        choices=("fcfs", "frfcfs"),
        help="override dram.scheduler for an experiment",
    )
    parser.add_argument(
        "--dram-read-buffer-size",
        type=int,
        help="override dram.read_buffer_size for an experiment",
    )
    parser.add_argument(
        "--dram-frfcfs-selection-window",
        type=int,
        help="override dram.frfcfs_selection_window for an experiment",
    )
    parser.add_argument(
        "--dram-frfcfs-topology-scaled-window",
        choices=("true", "false"),
        help=(
            "override dram.frfcfs_topology_scaled_window for an "
            "experiment"
        ),
    )
    parser.add_argument(
        "--dram-frfcfs-full-queue-page-policy",
        choices=("true", "false"),
        help=(
            "override dram.frfcfs_full_queue_page_policy for the "
            "open_adaptive source-alignment experiment"
        ),
    )
    parser.add_argument(
        "--dram-frfcfs-row-cap-single-precharge",
        choices=("true", "false"),
        help=(
            "override dram.frfcfs_row_cap_single_precharge for the "
            "source-alignment experiment"
        ),
    )
    parser.add_argument(
        "--dram-frfcfs-passes",
        type=int,
        help="override dram.frfcfs_passes for an experiment",
    )
    parser.add_argument(
        "--dram-frfcfs-arrival-bucket-cycles",
        type=int,
        help="override dram.frfcfs_arrival_bucket_cycles",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help=(
            "reuse existing per-case outputs; disabled by default so a "
            "changed FastSim binary or configuration cannot silently leave "
            "stale validation results"
        ),
    )
    parser.add_argument(
        "--data-python",
        default=sys.executable,
        help="Python interpreter with numpy/pyarrow for trace conversion and validation",
    )
    args = parser.parse_args()

    project = Path(__file__).resolve().parent.parent
    scratch = (
        Path(args.scratch_dir).resolve()
        if args.scratch_dir
        else project / "tmp" / "validation-scratch"
    )
    scratch.mkdir(parents=True, exist_ok=True)
    fastsim = Path(args.fastsim).resolve()
    config = Path(args.config).resolve()
    raw_data_root = Path(args.raw_data_root).resolve()
    output = Path(args.out_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    core_counts = sorted(set(args.cores or [4, 8]))
    raw_roots = {
        cores: raw_data_root
        / f"raw_v28_1_business_a2_sharedzipf_seed{args.seed}_c{cores:02d}"
        for cores in core_counts
    }
    for root in raw_roots.values():
        if not root.is_dir():
            raise SystemExit(f"raw trace root does not exist: {root}")
    workloads = args.workload or discover_workloads(raw_roots.values())
    if not workloads:
        raise SystemExit("no common W_* workloads found")
    write_run_manifest(
        output, project, fastsim, config, raw_roots, workloads,
        args.reuse_existing,
    )

    rows = []
    total_cases = len(core_counts) * len(workloads)
    completed = 0
    for cores in core_counts:
        for workload in workloads:
            completed += 1
            source = raw_roots[cores] / workload
            aligned = sorted((source / "tao_trace").glob("*.aligned.parquet"))
            if len(aligned) != cores:
                raise SystemExit(
                    f"{source}: expected {cores} aligned traces, found {len(aligned)}"
                )
            gem5_stats = source / "stats.txt"
            if not gem5_stats.is_file():
                raise SystemExit(f"missing gem5 stats: {gem5_stats}")
            case_dir = output / f"c{cores:02d}" / workload
            case_dir.mkdir(parents=True, exist_ok=True)
            validation_path = case_dir / "validation.json"
            fastsim_stats = case_dir / "fastsim-stats.json"
            print(
                f"[{completed}/{total_cases}] cores={cores} workload={workload}",
                flush=True,
            )
            if (
                not args.reuse_existing
                or not validation_path.is_file()
                or not fastsim_stats.is_file()
            ):
                with tempfile.TemporaryDirectory(
                    prefix=f"fastsim-c{cores:02d}-{workload}-",
                    dir=scratch,
                ) as temporary:
                    trace_dir = Path(temporary) / "trace"
                    pattern = str(source / "tao_trace" / "*.aligned.parquet")
                    run(
                        [
                            args.data_python,
                            str(project / "tools" / "convert_aligned_parquet.py"),
                            "--input",
                            pattern,
                            "--out-dir",
                            str(trace_dir),
                        ]
                    )
                    simulate_command = [
                        str(fastsim),
                        "simulate",
                        "--measurement-scope",
                        "user",
                        "--config",
                        str(config),
                        "--cores",
                        str(cores),
                        "--manifest",
                        str(trace_dir / "manifest.txt"),
                        "--output",
                        str(fastsim_stats),
                    ]
                    if args.interval_reweave_passes is not None:
                        simulate_command.extend(
                            [
                                "--interval-reweave-passes",
                                str(args.interval_reweave_passes),
                            ]
                        )
                    if args.interval_private_preview is not None:
                        simulate_command.extend(
                            [
                                "--interval-private-preview",
                                args.interval_private_preview,
                            ]
                        )
                    if args.interval_max_cycles is not None:
                        simulate_command.extend(
                            [
                                "--interval-max-cycles",
                                str(args.interval_max_cycles),
                            ]
                        )
                    if args.cpi_attribution is not None:
                        simulate_command.extend(
                            ["--cpi-attribution", args.cpi_attribution]
                        )
                    if args.interval_parallel_feedback is not None:
                        simulate_command.extend(
                            [
                                "--interval-parallel-feedback",
                                args.interval_parallel_feedback,
                            ]
                        )
                    if args.interval_causal_timing is not None:
                        simulate_command.extend(
                            [
                                "--interval-causal-timing",
                                args.interval_causal_timing,
                            ]
                        )
                    if args.interval_causal_passes is not None:
                        simulate_command.extend(
                            [
                                "--interval-causal-passes",
                                str(args.interval_causal_passes),
                            ]
                        )
                    if args.interval_causal_max_closure_events is not None:
                        simulate_command.extend(
                            [
                                "--interval-causal-max-closure-events",
                                str(args.interval_causal_max_closure_events),
                            ]
                        )
                    if args.domain_workers is not None:
                        simulate_command.extend(
                            ["--domain-workers", str(args.domain_workers)]
                        )
                    if args.response_rob_lsq_feedback is not None:
                        simulate_command.extend(
                            [
                                "--response-rob-lsq-feedback",
                                args.response_rob_lsq_feedback,
                            ]
                        )
                    if args.response_sparse_scoreboard is not None:
                        simulate_command.extend(
                            [
                                "--response-sparse-scoreboard",
                                args.response_sparse_scoreboard,
                            ]
                        )
                        if (
                            args.response_sparse_scoreboard == "false"
                            and args.response_activity_certificate is None
                        ):
                            simulate_command.extend(
                                [
                                    "--response-activity-certificate",
                                    "false",
                                ]
                            )
                    if args.response_sparse_resource_repair is not None:
                        simulate_command.extend(
                            [
                                "--response-sparse-resource-repair",
                                args.response_sparse_resource_repair,
                            ]
                        )
                    if args.response_activity_certificate is not None:
                        simulate_command.extend(
                            [
                                "--response-activity-certificate",
                                args.response_activity_certificate,
                            ]
                        )
                    if args.branch_shadow_rob is not None:
                        simulate_command.extend(
                            ["--branch-shadow-rob", args.branch_shadow_rob]
                        )
                    if args.needs_tso is not None:
                        simulate_command.extend(
                            ["--needs-tso", args.needs_tso]
                        )
                    if args.response_retire_exposure is not None:
                        simulate_command.extend(
                            [
                                "--response-retire-exposure",
                                str(args.response_retire_exposure),
                            ]
                        )
                    if args.domain_min_events is not None:
                        simulate_command.extend(
                            [
                                "--domain-min-events",
                                str(args.domain_min_events),
                            ]
                        )
                    if args.llc_fill_response_latency is not None:
                        simulate_command.extend(
                            [
                                "--llc-fill-response-latency",
                                str(args.llc_fill_response_latency),
                            ]
                        )
                    if args.dtlb_page_walk_latency is not None:
                        simulate_command.extend(
                            [
                                "--dtlb-page-walk-latency",
                                str(args.dtlb_page_walk_latency),
                            ]
                        )
                    if args.dtlb_miss_model is not None:
                        simulate_command.extend(
                            ["--dtlb-miss-model", args.dtlb_miss_model]
                        )
                    if args.dram_scheduler is not None:
                        simulate_command.extend(
                            ["--dram-scheduler", args.dram_scheduler]
                        )
                    if args.dram_read_buffer_size is not None:
                        simulate_command.extend(
                            [
                                "--dram-read-buffer-size",
                                str(args.dram_read_buffer_size),
                            ]
                        )
                    if args.dram_frfcfs_selection_window is not None:
                        simulate_command.extend(
                            [
                                "--dram-frfcfs-selection-window",
                                str(args.dram_frfcfs_selection_window),
                            ]
                        )
                    if (
                        args.dram_frfcfs_topology_scaled_window
                        is not None
                    ):
                        simulate_command.extend(
                            [
                                "--dram-frfcfs-topology-scaled-window",
                                args.dram_frfcfs_topology_scaled_window,
                            ]
                        )
                    if args.dram_frfcfs_full_queue_page_policy is not None:
                        simulate_command.extend(
                            [
                                "--dram-frfcfs-full-queue-page-policy",
                                args.dram_frfcfs_full_queue_page_policy,
                            ]
                        )
                    if args.dram_frfcfs_row_cap_single_precharge is not None:
                        simulate_command.extend(
                            [
                                "--dram-frfcfs-row-cap-single-precharge",
                                args.dram_frfcfs_row_cap_single_precharge,
                            ]
                        )
                    if args.dram_frfcfs_passes is not None:
                        simulate_command.extend(
                            [
                                "--dram-frfcfs-passes",
                                str(args.dram_frfcfs_passes),
                            ]
                        )
                    if args.dram_frfcfs_arrival_bucket_cycles is not None:
                        simulate_command.extend(
                            [
                                "--dram-frfcfs-arrival-bucket-cycles",
                                str(args.dram_frfcfs_arrival_bucket_cycles),
                            ]
                        )
                    run(simulate_command)
                    run(
                        [
                            args.data_python,
                            str(project / "tools" / "validate_gem5_pmu.py"),
                            "--fastsim-stats",
                            str(fastsim_stats),
                            "--gem5-stats",
                            str(gem5_stats),
                            "--aligned",
                            pattern,
                            "--output",
                            str(validation_path),
                        ]
                    )
            rows.append(load_case(case_dir, cores, workload))
            write_outputs(output, rows, sorted({row["cores"] for row in rows}))

    write_outputs(output, rows, core_counts)
    print(f"summary={output / 'summary.json'}", flush=True)


if __name__ == "__main__":
    main()
