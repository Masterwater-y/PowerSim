#!/usr/bin/env python3
"""Build a cross-workload component matrix from paired gem5/FastSim events.

The matrix deliberately separates exact local stage residuals from FastSim's
observed pipeline gates.  A residual conditioned on a gate is a prioritization
signal, not a causal attribution: an older frontier may own the real delay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


CAUSE_NAMES = {
    0: "unattributed",
    1: "rename_free_list",
    2: "dispatch_bandwidth",
    3: "rob_capacity",
    4: "iq_capacity",
    5: "lq_capacity",
    6: "sq_capacity",
    7: "dependency",
    8: "sequencer",
    9: "l1_mshr",
    10: "l2_mshr",
    11: "instruction_fetch",
    12: "memory_response",
    13: "commit_bandwidth",
    14: "tso_store",
    15: "branch_recovery",
}

ISSUE_GATE_NAMES = {
    0: "none",
    1: "dispatch_admission",
    2: "serialize_after_carry",
    3: "serialize_before",
    4: "memory_barrier_carry",
    5: "memory_barrier_head",
    6: "register_producer",
    7: "store_set_producer",
    8: "issue_resource",
    9: "dtlb_pending_fill",
    10: "dtlb_walker",
    11: "page_walk_dependency",
    12: "sequencer",
    13: "l1_mshr",
    14: "l2_mshr",
}

FASTSIM_SHARED_PATH_NAMES = {
    0: "local_private_cache",
    1: "permission_upgrade",
    2: "remote_supply",
    3: "llc_hit",
    4: "merged_memory",
    5: "memory",
}

COMPONENTS = (
    "frontend_branch",
    "execution_dependency",
    "core_capacity",
    "load_store_ordering",
    "memory_admission",
    "cache_coherence_response",
    "dram_service",
    "retire_commit",
    "unattributed",
)

CRITICAL_CAUSES = tuple(
    name for _, name in sorted(CAUSE_NAMES.items())
)
ANCHOR_THRESHOLDS = (100, 1000, 10000)


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level JSON must be an object")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def distribution(values: Iterable[float]) -> dict[str, Any]:
    data = list(values)
    if not data:
        return {
            "count": 0,
            "sum": 0,
            "mean": None,
            "minimum": None,
            "p50": None,
            "p90": None,
            "maximum": None,
            "negative": 0,
            "zero": 0,
            "positive": 0,
        }
    return {
        "count": len(data),
        "sum": sum(data),
        "mean": sum(data) / len(data),
        "minimum": min(data),
        "p50": percentile(data, 0.5),
        "p90": percentile(data, 0.9),
        "maximum": max(data),
        "negative": sum(value < 0 for value in data),
        "zero": sum(value == 0 for value in data),
        "positive": sum(value > 0 for value in data),
    }


def bound_class(lower: float, upper: float) -> str:
    if lower > upper:
        raise ValueError("invalid residual bound")
    if lower > 0:
        return "proven_positive"
    if upper < 0:
        return "proven_negative"
    return "ambiguous"


def cause_name(value: Any) -> str:
    return CAUSE_NAMES.get(int(value or 0), f"unknown_{value}")


def issue_gate_name(value: Any) -> str:
    return ISSUE_GATE_NAMES.get(int(value or 0), f"unknown_{value}")


def fastsim_shared_path_name(value: Any) -> str:
    return FASTSIM_SHARED_PATH_NAMES.get(
        int(value or 0), f"unknown_{value}")


def hierarchy_path_semantically_matches(
    fastsim_path: str, gem5_outcome: str
) -> bool:
    compatible = {
        "local_private_cache": {"l1d_hit", "private_l2_hit"},
        "permission_upgrade": {
            "l1d_permission_upgrade",
            "private_l2_permission_upgrade",
            "llc_permission_upgrade",
        },
        "remote_supply": {"llc_remote_supply"},
        "llc_hit": {"llc_hit"},
        "merged_memory": {"sequencer_coalesced"},
        "memory": {"ruby_memory_read"},
    }
    return gem5_outcome in compatible.get(fastsim_path, set())


def native_hierarchy_comparable(
    sample: dict[str, Any], pair: dict[str, Any], native: dict[str, Any] | None
) -> bool:
    data_events = [
        event for event in sample.get("memory_events", [])
        if not bool(event.get("instruction_fetch"))
    ]
    return bool(
        native is not None and
        pair.get("memory_event_scope") == "data-hierarchy" and
        sample.get("selected_memory_valid") and
        not sample.get("selected_memory_instruction_fetch") and
        int(native.get("line_requests", 0)) == 1 and
        len(data_events) == 1
    )


def native_comparison_bucket() -> dict[str, Any]:
    return {
        "samples": 0,
        "semantic_matches": 0,
        "paired_local_tail_residual_cycles": [],
        "gem5_issue_to_first_admission_cycles": [],
        "gem5_first_admission_to_last_response_cycles": [],
        "gem5_last_response_to_commit_cycles": [],
        "fastsim_selected_memory_latency_cycles": [],
        "fastsim_latency_minus_gem5_response_cycles": [],
    }


def add_native_comparison_sample(
    bucket: dict[str, Any],
    *,
    semantic_match: bool,
    tail: float,
    native: dict[str, Any],
    fastsim_latency: float,
) -> bool:
    """Accumulate one exact hierarchy comparison; return timing availability."""
    bucket["samples"] += 1
    bucket["semantic_matches"] += int(semantic_match)
    bucket["paired_local_tail_residual_cycles"].append(tail)
    timing = native.get("timing_cycles")
    if not native.get("response_timestamps_available") or not timing:
        return False
    gem5_service = float(timing["first_admission_to_last_response"])
    bucket["gem5_issue_to_first_admission_cycles"].append(
        float(timing["issue_to_first_admission"])
    )
    bucket["gem5_first_admission_to_last_response_cycles"].append(
        gem5_service
    )
    bucket["gem5_last_response_to_commit_cycles"].append(
        float(timing["last_response_to_commit"])
    )
    bucket["fastsim_selected_memory_latency_cycles"].append(
        fastsim_latency
    )
    bucket["fastsim_latency_minus_gem5_response_cycles"].append(
        fastsim_latency - gem5_service
    )
    return True


def render_native_comparison_rows(
    buckets: dict[tuple[str, str], dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {
            "fastsim_path": fastsim_path,
            "gem5_outcome": gem5_outcome,
            "samples": bucket["samples"],
            "semantic_matches": bucket["semantic_matches"],
            "semantic_mismatches":
                bucket["samples"] - bucket["semantic_matches"],
            "paired_local_tail_residual_cycles": distribution(
                bucket["paired_local_tail_residual_cycles"]
            ),
            "gem5_issue_to_first_admission_cycles": distribution(
                bucket["gem5_issue_to_first_admission_cycles"]
            ),
            "gem5_first_admission_to_last_response_cycles": distribution(
                bucket["gem5_first_admission_to_last_response_cycles"]
            ),
            "gem5_last_response_to_commit_cycles": distribution(
                bucket["gem5_last_response_to_commit_cycles"]
            ),
            "fastsim_selected_memory_latency_cycles": distribution(
                bucket["fastsim_selected_memory_latency_cycles"]
            ),
            "fastsim_latency_minus_gem5_response_cycles": distribution(
                bucket["fastsim_latency_minus_gem5_response_cycles"]
            ),
            "timing_attribution": False,
        }
        for (fastsim_path, gem5_outcome), bucket in sorted(buckets.items())
    ]


def issue_owner_chain(
    sequence: int,
    samples: dict[int, dict[str, Any]],
    maximum_depth: int = 64,
) -> dict[str, Any]:
    """Follow exact FastSim producer edges while dense samples are present."""
    nodes = []
    visited = set()
    current = sequence
    stop = "maximum_depth"
    for _ in range(maximum_depth):
        if current in visited:
            stop = "cycle"
            break
        visited.add(current)
        sample = samples.get(current)
        if sample is None:
            stop = "owner_unsampled"
            nodes.append({"sequence": current, "sampled": False})
            break
        node = {
            "sequence": current,
            "sampled": True,
            "pc": sample.get("pc"),
            "issue_gate": issue_gate_name(sample.get("issue_gate_kind")),
            "issue_gate_extra_cycles":
                int(sample.get("issue_gate_extra_cycles", 0)),
            "completion_cause": cause_name(sample.get("completion_cause")),
            "retire_cause": cause_name(sample.get("retire_cause")),
            "has_load": bool(sample.get("has_load")),
            "has_store": bool(sample.get("has_store")),
            "selected_memory_path": (
                int(sample.get("selected_memory_path", 0))
                if sample.get("selected_memory_valid") else None
            ),
            "selected_memory_latency_cycles": (
                int(sample.get("selected_memory_latency_cycles", 0))
                if sample.get("selected_memory_valid") else None
            ),
            "selected_memory_exposed_cycles": (
                int(sample.get("selected_memory_exposed_cycles", 0))
                if sample.get("selected_memory_valid") else None
            ),
        }
        nodes.append(node)
        if not sample.get("issue_gate_owner_valid"):
            stop = "no_issue_owner"
            break
        current = int(sample["issue_gate_owner_sequence"])
    return {"nodes": nodes, "stop": stop}


def component_for_cause(cause: str, sample: dict[str, Any]) -> str:
    if cause in {
        "rename_free_list",
        "dispatch_bandwidth",
        "instruction_fetch",
        "branch_recovery",
    }:
        return "frontend_branch"
    if cause == "dependency":
        return "execution_dependency"
    if cause in {"rob_capacity", "iq_capacity"}:
        return "core_capacity"
    if cause in {"lq_capacity", "sq_capacity", "tso_store"}:
        return "load_store_ordering"
    if cause in {"sequencer", "l1_mshr", "l2_mshr"}:
        return "memory_admission"
    if cause == "memory_response":
        if (sample.get("selected_memory_valid") and
                sample.get("selected_memory_unique_dram_request") and
                int(sample.get("selected_memory_path", -1)) == 5):
            return "dram_service"
        return "cache_coherence_response"
    if cause == "commit_bandwidth":
        return "retire_commit"
    return "unattributed"


def event_class(pair: dict[str, Any], sample: dict[str, Any]) -> str:
    if pair.get("is_atomic"):
        return "atomic"
    if pair.get("is_store"):
        return "store"
    if pair.get("is_load"):
        return "load"
    if sample.get("branch_miss"):
        return "branch_miss"
    if sample.get("branch"):
        return "branch"
    return "other"


def validate_identity(
    case: str, core: int, sample: dict[str, Any], pair: dict[str, Any]
) -> bool:
    sequence = int(pair["record_ordinal"])
    if int(sample["sequence"]) != sequence:
        raise ValueError(
            f"{case}: FastSim/gem5 identity mismatch at core {core} "
            f"sequence {sequence}"
        )
    direct_pc_check = "pc" in sample
    if direct_pc_check and int(sample["pc"]) != int(pair["pc"]):
        raise ValueError(
            f"{case}: FastSim/gem5 PC mismatch at core {core} "
            f"sequence {sequence}"
        )
    mmio = pair.get("memory_event_scope") == "mmio-escape-no-data-event"
    if not mmio and (
        bool(sample.get("has_load")) != bool(pair.get("is_load")) or
        bool(sample.get("has_store")) != bool(pair.get("is_store"))
    ):
        raise ValueError(
            f"{case}: load/store identity mismatch at core {core} "
            f"sequence {sequence}"
        )
    if tuple(sample.get("producer_dists", [])[:4]) != tuple(
        pair.get("producer_dists", [])[:4]
    ):
        raise ValueError(
            f"{case}: dependency identity mismatch at core {core} "
            f"sequence {sequence}"
        )
    return direct_pc_check


def analyze_case(
    label: str,
    signed_residual_percent: float,
    stats_path: Path,
    pairs_path: Path,
) -> dict[str, Any]:
    stats = read_object(stats_path)
    pairs = read_object(pairs_path)
    if pairs.get("schema") != "fastsim-tail-timing-pairs-v1":
        raise ValueError(f"{pairs_path}: unsupported paired-event schema")
    if not pairs.get("instrumentation_target_metrics_equal"):
        raise ValueError(f"{label}: audit/control target metrics differ")
    recorded_hash = pairs.get("fastsim_audit_sha256")
    actual_hash = sha256(stats_path)
    if recorded_hash != actual_hash:
        raise ValueError(f"{label}: paired event audit hash mismatch")

    stats_cores = stats.get("cores", [])
    audit_by_core: dict[int, dict[int, dict[str, Any]]] = {}
    sampled_ranges = []
    for core, core_stats in enumerate(stats_cores):
        samples = core_stats.get("response_frontier_audit", [])
        indexed = {int(sample["sequence"]): sample for sample in samples}
        if len(indexed) != len(samples):
            raise ValueError(f"{label}: duplicate FastSim sample on core {core}")
        audit_by_core[core] = indexed
        if samples:
            sampled_ranges.append({
                "core": core,
                "samples": len(samples),
                "begin_sequence": min(indexed),
                "end_sequence": max(indexed),
            })

    observed_gate_counts = {name: 0 for name in COMPONENTS}
    retire_gate_counts = {name: 0 for name in COMPONENTS}
    retire_extensions = {name: [] for name in COMPONENTS}
    conditioned_tail = {name: [] for name in COMPONENTS}
    conditioned_frontend = {name: [] for name in COMPONENTS}
    event_tail: dict[str, list[float]] = {}
    event_frontend: dict[str, list[float]] = {}
    memory_paths: dict[str, dict[str, Any]] = {}
    native_hierarchy_comparison: dict[tuple[str, str], dict[str, Any]] = {}
    native_hierarchy_by_event: dict[
        str, dict[tuple[str, str], dict[str, Any]]
    ] = {}
    native_timing_samples = 0
    issue_gates: dict[str, dict[str, Any]] = {}
    checkpoint_displacements: dict[str, dict[str, Any]] = {}
    seen_checkpoints: dict[tuple[int, int, int], tuple[int, str]] = {}
    cumulative_classes = {"proven_positive": 0, "ambiguous": 0,
                          "proven_negative": 0}
    all_frontend: list[float] = []
    all_tail: list[float] = []
    fetch_position_raw: list[float] = []
    fetch_position_exact_active: list[float] = []
    retire_progress_raw: list[float] = []
    retire_progress_exact_active: list[float] = []
    transition_steps: list[dict[str, Any]] = []
    fetch_position_classes = {"proven_positive": 0, "ambiguous": 0,
                              "proven_negative": 0}
    joined = 0
    direct_pc_identity_checks = 0
    per_core = []

    critical_cycles = {}
    critical_total = 0
    for cause in CRITICAL_CAUSES:
        field = f"response_critical_{cause}_cycles"
        value = sum(int(core.get(field, 0)) for core in stats_cores)
        critical_cycles[cause] = value
        critical_total += value
    reported_critical_total = sum(
        int(core.get("response_critical_total_cycles", 0))
        for core in stats_cores
    )
    if critical_total != reported_critical_total:
        raise ValueError(f"{label}: response critical-cycle ledger is not conserved")

    for core_result in pairs.get("cores", []):
        core = int(core_result["core"])
        if core not in audit_by_core:
            raise ValueError(f"{label}: paired core {core} absent from stats")
        indexed = audit_by_core[core]
        core_joined = 0
        idle_budget = float(core_result.get("idle_cycle_budget") or 0)
        previous_pair = None
        anchors = {
            f"first_proven_positive_{threshold}": None
            for threshold in ANCHOR_THRESHOLDS
        }
        anchors.update({
            f"first_proven_negative_{threshold}": None
            for threshold in ANCHOR_THRESHOLDS
        })
        for pair in core_result.get("pairs", []):
            sequence = int(pair["record_ordinal"])
            sample = indexed.get(sequence)
            if sample is None:
                raise ValueError(
                    f"{label}: paired sequence {sequence} absent from core {core} audit"
                )
            direct_pc_identity_checks += int(
                validate_identity(label, core, sample, pair)
            )
            core_joined += 1
            joined += 1

            frontend = float(pair["fastsim_fetch_to_issue"]) - float(
                pair["gem5_fetch_to_issue"]
            )
            tail = float(pair["fastsim_issue_to_retire"]) - float(
                pair["gem5_issue_to_commit"]
            )
            all_frontend.append(frontend)
            all_tail.append(tail)

            gem5_fetch_from_roi_elapsed = (
                float(pair["gem5_elapsed_from_roi"]) -
                float(pair["gem5_fetch_to_issue"]) -
                float(pair["gem5_issue_to_commit"])
            )
            fastsim_fetch_from_roi = (
                float(sample["actual_fetch_cycle"]) -
                float(stats["totals"]["functional_warmup_barrier_cycles"])
            )
            raw_fetch_position = (
                fastsim_fetch_from_roi - gem5_fetch_from_roi_elapsed
            )
            fetch_position_raw.append(raw_fetch_position)
            fetch_position_classes[
                bound_class(raw_fetch_position,
                            raw_fetch_position + idle_budget)
            ] += 1
            if idle_budget == 0:
                fetch_position_exact_active.append(raw_fetch_position)

            progress_residual = None
            fastsim_progress = None
            gem5_progress = None
            if previous_pair is not None:
                fastsim_progress = (
                    float(pair["fastsim_retire_from_roi"]) -
                    float(previous_pair["fastsim_retire_from_roi"])
                )
                gem5_progress = (
                    float(pair["gem5_elapsed_from_roi"]) -
                    float(previous_pair["gem5_elapsed_from_roi"])
                )
                progress_residual = fastsim_progress - gem5_progress
                retire_progress_raw.append(progress_residual)
                if idle_budget == 0:
                    retire_progress_exact_active.append(progress_residual)
            previous_pair = pair
            kind = event_class(pair, sample)
            event_frontend.setdefault(kind, []).append(frontend)
            event_tail.setdefault(kind, []).append(tail)

            gate = issue_gate_name(sample.get("issue_gate_kind"))
            gate_bucket = issue_gates.setdefault(gate, {
                "samples": 0,
                "owner_samples": 0,
                "cross_checkpoint_owner_samples": 0,
                "owner_completion_causes": {},
                "extra_cycles": [],
                "paired_local_frontend_residual_cycles": [],
                "paired_local_tail_residual_cycles": [],
                "cross_milestone_progress_residual_cycles": [],
                "cross_milestone_exact_active_residual_cycles": [],
            })
            gate_bucket["samples"] += 1
            gate_bucket["extra_cycles"].append(float(
                sample.get("issue_gate_extra_cycles", 0)
            ))
            gate_bucket["paired_local_frontend_residual_cycles"].append(
                frontend
            )
            gate_bucket["paired_local_tail_residual_cycles"].append(tail)
            if progress_residual is not None:
                gate_bucket[
                    "cross_milestone_progress_residual_cycles"
                ].append(progress_residual)
                if idle_budget == 0:
                    gate_bucket[
                        "cross_milestone_exact_active_residual_cycles"
                    ].append(progress_residual)
                transition_steps.append({
                    "core": core,
                    "record_ordinal": sequence,
                    "roi_record_offset": int(pair["roi_record_offset"]),
                    "pc": int(pair["pc"]),
                    "cpl": int(pair.get("cpl", 0)),
                    "event_class": kind,
                    "fastsim_progress_cycles": fastsim_progress,
                    "gem5_elapsed_progress_cycles": gem5_progress,
                    "fastsim_minus_gem5_progress_cycles":
                        progress_residual,
                    "exact_active_cycle_comparison": idle_budget == 0,
                    "issue_gate": gate,
                    "issue_gate_extra_cycles": int(
                        sample.get("issue_gate_extra_cycles", 0)
                    ),
                    "checkpoint_extra_cycles": int(
                        sample.get("checkpoint_extra_cycles", 0)
                    ),
                    "checkpoint_critical_cause": cause_name(
                        sample.get("checkpoint_critical_cause")
                    ),
                    "gem5_fetch_to_issue_cycles":
                        float(pair["gem5_fetch_to_issue"]),
                    "gem5_issue_to_commit_cycles":
                        float(pair["gem5_issue_to_commit"]),
                    "fastsim_fetch_to_issue_cycles":
                        float(pair["fastsim_fetch_to_issue"]),
                    "fastsim_issue_to_retire_cycles":
                        float(pair["fastsim_issue_to_retire"]),
                    "gem5_native_hierarchy_outcome": (
                        pair.get("gem5_native") or {}
                    ).get("outcome"),
                    "issue_owner_chain": issue_owner_chain(
                        sequence, indexed
                    ),
                })
            if sample.get("issue_gate_owner_valid"):
                gate_bucket["owner_samples"] += 1
                gate_bucket["cross_checkpoint_owner_samples"] += int(bool(
                    sample.get("issue_gate_cross_checkpoint")
                ))
                root = cause_name(
                    sample.get("issue_gate_owner_completion_cause")
                )
                gate_bucket["owner_completion_causes"][root] = (
                    gate_bucket["owner_completion_causes"].get(root, 0) + 1
                )

            checkpoint_key = (
                core,
                int(sample.get("checkpoint_begin_sequence", sequence)),
                int(sample.get("checkpoint_end_sequence", sequence)),
            )
            checkpoint_value = int(sample.get("checkpoint_extra_cycles", 0))
            checkpoint_cause = cause_name(
                sample.get("checkpoint_critical_cause")
            )
            prior_checkpoint = seen_checkpoints.get(checkpoint_key)
            if prior_checkpoint is not None and prior_checkpoint != (
                checkpoint_value, checkpoint_cause
            ):
                raise ValueError(
                    f"{label}: inconsistent checkpoint displacement for "
                    f"core {core} range {checkpoint_key[1]}-{checkpoint_key[2]}"
                )
            if prior_checkpoint is None:
                seen_checkpoints[checkpoint_key] = (
                    checkpoint_value, checkpoint_cause
                )
                checkpoint_bucket = checkpoint_displacements.setdefault(
                    checkpoint_cause, {"checkpoints": 0, "extra_cycles": []}
                )
                checkpoint_bucket["checkpoints"] += 1
                checkpoint_bucket["extra_cycles"].append(
                    float(checkpoint_value)
                )

            gate_components = {
                component_for_cause(cause_name(sample.get(field)), sample)
                for field in ("dispatch_cause", "completion_cause", "retire_cause")
            }
            for component in gate_components:
                observed_gate_counts[component] += 1

            retire_component = component_for_cause(
                cause_name(sample.get("retire_cause")), sample
            )
            retire_gate_counts[retire_component] += 1
            conditioned_tail[retire_component].append(tail)
            conditioned_frontend[retire_component].append(frontend)
            retire_extensions[retire_component].append(
                int(sample["actual_retire_cycle"]) -
                int(sample["base_retire_cycle"])
            )

            if sample.get("selected_memory_valid"):
                path = str(int(sample.get("selected_memory_path", -1)))
                bucket = memory_paths.setdefault(path, {
                    "samples": 0,
                    "loads": 0,
                    "stores": 0,
                    "unique_dram_requests": 0,
                    "blocks_retirement": 0,
                    "latency_cycles": [],
                    "exposed_cycles": [],
                    "local_tail_residual_cycles": [],
                })
                bucket["samples"] += 1
                bucket["loads"] += int(bool(pair.get("is_load")))
                bucket["stores"] += int(bool(pair.get("is_store")))
                bucket["unique_dram_requests"] += int(bool(
                    sample.get("selected_memory_unique_dram_request")
                ))
                bucket["blocks_retirement"] += int(bool(
                    sample.get("selected_memory_blocks_retirement")
                ))
                bucket["latency_cycles"].append(float(
                    sample.get("selected_memory_latency_cycles", 0)
                ))
                bucket["exposed_cycles"].append(float(
                    sample.get("selected_memory_exposed_cycles", 0)
                ))
                bucket["local_tail_residual_cycles"].append(tail)

            native = pair.get("gem5_native")
            comparable_native = native_hierarchy_comparable(
                sample, pair, native)
            if comparable_native:
                fastsim_path = fastsim_shared_path_name(
                    sample.get("selected_memory_path"))
                gem5_outcome = str(native.get("outcome"))
                semantic_match = hierarchy_path_semantically_matches(
                    fastsim_path, gem5_outcome)
                fastsim_latency = float(
                    sample.get("selected_memory_latency_cycles", 0))
                comparison = native_hierarchy_comparison.setdefault(
                    (fastsim_path, gem5_outcome),
                    native_comparison_bucket())
                event_comparison = native_hierarchy_by_event.setdefault(
                    kind, {}).setdefault(
                        (fastsim_path, gem5_outcome),
                        native_comparison_bucket())
                has_timing = add_native_comparison_sample(
                    comparison,
                    semantic_match=semantic_match,
                    tail=tail,
                    native=native,
                    fastsim_latency=fastsim_latency,
                )
                add_native_comparison_sample(
                    event_comparison,
                    semantic_match=semantic_match,
                    tail=tail,
                    native=native,
                    fastsim_latency=fastsim_latency,
                )
                if has_timing:
                    native_timing_samples += 1

            lower = float(pair["fastsim_minus_gem5_active_lower"])
            upper = float(pair["fastsim_minus_gem5_active_upper"])
            cumulative_classes[bound_class(lower, upper)] += 1
            anchor = {
                "record_ordinal": sequence,
                "roi_record_offset": int(pair["roi_record_offset"]),
                "pc": int(pair["pc"]),
                "event_class": kind,
                "lower_residual_cycles": lower,
                "upper_residual_cycles": upper,
            }
            for threshold in ANCHOR_THRESHOLDS:
                positive = f"first_proven_positive_{threshold}"
                negative = f"first_proven_negative_{threshold}"
                if anchors[positive] is None and lower >= threshold:
                    anchors[positive] = anchor
                if anchors[negative] is None and upper <= -threshold:
                    anchors[negative] = anchor

        if core_joined != int(core_result.get("paired_milestones", core_joined)):
            raise ValueError(f"{label}: paired milestone count mismatch on core {core}")
        per_core.append({
            "core": core,
            "paired_samples": core_joined,
            "idle_cycle_budget": core_result.get("idle_cycle_budget"),
            "residual_anchors": anchors,
        })

    component_rows = {}
    for component in COMPONENTS:
        component_rows[component] = {
            "observed_gate_samples": observed_gate_counts[component],
            "retire_gate_samples": retire_gate_counts[component],
            "fastsim_internal_retire_extension_cycles": distribution(
                retire_extensions[component]
            ),
            "paired_local_frontend_residual_cycles": distribution(
                conditioned_frontend[component]
            ),
            "paired_local_tail_residual_cycles": distribution(
                conditioned_tail[component]
            ),
            "causal_attribution": False,
        }

    memory_rows = {}
    for path, bucket in sorted(memory_paths.items(), key=lambda item: int(item[0])):
        memory_rows[path] = {
            key: value for key, value in bucket.items()
            if not isinstance(value, list)
        }
        for field in (
            "latency_cycles", "exposed_cycles", "local_tail_residual_cycles"
        ):
            memory_rows[path][field] = distribution(bucket[field])

    issue_gate_rows = {}
    for gate, bucket in sorted(issue_gates.items()):
        issue_gate_rows[gate] = {
            "samples": bucket["samples"],
            "owner_samples": bucket["owner_samples"],
            "cross_checkpoint_owner_samples":
                bucket["cross_checkpoint_owner_samples"],
            "owner_completion_causes":
                bucket["owner_completion_causes"],
            "fastsim_internal_extra_cycles": distribution(
                bucket["extra_cycles"]
            ),
            "paired_local_frontend_residual_cycles": distribution(
                bucket["paired_local_frontend_residual_cycles"]
            ),
            "paired_local_tail_residual_cycles": distribution(
                bucket["paired_local_tail_residual_cycles"]
            ),
            "cross_milestone_progress_residual_cycles": distribution(
                bucket["cross_milestone_progress_residual_cycles"]
            ),
            "cross_milestone_exact_active_residual_cycles": distribution(
                bucket["cross_milestone_exact_active_residual_cycles"]
            ),
            "reference_error_attribution": False,
        }

    checkpoint_rows = {
        cause: {
            "checkpoints": bucket["checkpoints"],
            "fastsim_internal_extra_cycles": distribution(
                bucket["extra_cycles"]
            ),
            "reference_error_attribution": False,
        }
        for cause, bucket in sorted(checkpoint_displacements.items())
    }
    native_rows = render_native_comparison_rows(native_hierarchy_comparison)
    native_event_rows = {
        kind: render_native_comparison_rows(buckets)
        for kind, buckets in sorted(native_hierarchy_by_event.items())
    }

    scope = stats.get("scope_metrics", {})
    return {
        "case": label,
        "source_case": pairs.get("case"),
        "signed_full_roi_residual_percent": signed_residual_percent,
        "fastsim_metric": scope.get("cycles_per_user_uop", scope.get("cpi")),
        "evidence": {
            "paired_stage_identity": True,
            "direct_fastsim_pc_identity_checks": direct_pc_identity_checks,
            "identity_checks_inherited_from_paired_input":
                joined - direct_pc_identity_checks,
            "instrumentation_target_metrics_equal": True,
            "causal_root_established": False,
            "pair_count": joined,
            "full_roi_reference_collection": bool(pairs.get("full_roi")),
            "sampling": sampled_ranges,
            "per_core": per_core,
        },
        "paired_local_stage_residual_cycles": {
            "fetch_to_issue": distribution(all_frontend),
            "issue_to_retire_minus_gem5_issue_to_commit": distribution(all_tail),
        },
        "fastsim_internal_critical_cycle_ledger": {
            "cycles_by_reported_cause": critical_cycles,
            "reported_total_cycles": reported_critical_total,
            "conserved": True,
            "per_user_trace_uop": (
                reported_critical_total / scope["user_trace_uops"]
                if scope.get("user_trace_uops") else None
            ),
            "reference_error_attribution": False,
        },
        "fastsim_issue_gate_owners": issue_gate_rows,
        "sampled_checkpoint_interval_displacements": {
            "unique_checkpoints": len(seen_checkpoints),
            "by_reported_cause": checkpoint_rows,
            "reference_error_attribution": False,
        },
        "cross_milestone_progress": {
            "fetch_position_minus_gem5_elapsed_fetch":
                distribution(fetch_position_raw),
            "fetch_position_active_cycle_bound_class":
                fetch_position_classes,
            "fetch_position_exact_active_zero_idle_cores":
                distribution(fetch_position_exact_active),
            "retire_progress_minus_gem5_elapsed_progress":
                distribution(retire_progress_raw),
            "retire_progress_exact_active_zero_idle_cores":
                distribution(retire_progress_exact_active),
            "largest_negative_steps": sorted(
                (step for step in transition_steps
                 if step["fastsim_minus_gem5_progress_cycles"] < 0),
                key=lambda step:
                    step["fastsim_minus_gem5_progress_cycles"],
            )[:12],
            "largest_positive_steps": sorted(
                (step for step in transition_steps
                 if step["fastsim_minus_gem5_progress_cycles"] > 0),
                key=lambda step:
                    step["fastsim_minus_gem5_progress_cycles"],
                reverse=True,
            )[:12],
        },
        "cumulative_retire_residual_bound_class": cumulative_classes,
        "event_classes": {
            kind: {
                "paired_samples": len(event_tail[kind]),
                "frontend_residual_cycles": distribution(event_frontend[kind]),
                "tail_residual_cycles": distribution(event_tail[kind]),
            }
            for kind in sorted(event_tail)
        },
        "components": component_rows,
        "selected_memory_paths": memory_rows,
        "native_hierarchy_path_comparison": {
            "semantically_comparable_samples": sum(
                row["samples"] for row in native_rows
            ),
            "semantic_matches": sum(
                row["semantic_matches"] for row in native_rows
            ),
            "semantic_mismatches": sum(
                row["semantic_mismatches"] for row in native_rows
            ),
            "rows": native_rows,
            "by_event_class": native_event_rows,
            "response_timestamps_available": native_timing_samples > 0,
            "response_timestamp_samples": native_timing_samples,
            "reference_timing_attribution": False,
        },
        "inputs": {
            "fastsim_stats": str(stats_path.resolve()),
            "fastsim_stats_sha256": actual_hash,
            "gem5_pairs": str(pairs_path.resolve()),
            "gem5_pairs_sha256": sha256(pairs_path),
        },
    }


def cross_case_summary(cases: list[dict[str, Any]]) -> dict[str, Any]:
    components = {}
    for component in COMPONENTS:
        rows = []
        for case in cases:
            value = case["components"][component]
            rows.append({
                "case": case["case"],
                "full_roi_signed_residual_percent":
                    case["signed_full_roi_residual_percent"],
                "observed_gate_samples": value["observed_gate_samples"],
                "retire_gate_samples": value["retire_gate_samples"],
                "conditioned_local_tail_mean_cycles":
                    value["paired_local_tail_residual_cycles"]["mean"],
            })
        components[component] = {
            "cases_with_observed_gate": sum(
                row["observed_gate_samples"] > 0 for row in rows
            ),
            "cases_with_retire_gate": sum(
                row["retire_gate_samples"] > 0 for row in rows
            ),
            "cases": rows,
            "causal_priority_rank": None,
        }
    issue_gates = {}
    all_issue_gates = sorted({
        gate
        for case in cases
        for gate in case["fastsim_issue_gate_owners"]
    })
    for gate in all_issue_gates:
        rows = []
        for case in cases:
            value = case["fastsim_issue_gate_owners"].get(gate, {})
            rows.append({
                "case": case["case"],
                "full_roi_signed_residual_percent":
                    case["signed_full_roi_residual_percent"],
                "samples": value.get("samples", 0),
                "owner_samples": value.get("owner_samples", 0),
                "cross_checkpoint_owner_samples":
                    value.get("cross_checkpoint_owner_samples", 0),
                "owner_completion_causes":
                    value.get("owner_completion_causes", {}),
                "conditioned_local_tail_mean_cycles": value.get(
                    "paired_local_tail_residual_cycles", {}
                ).get("mean"),
            })
        issue_gates[gate] = {
            "cases_with_samples": sum(row["samples"] > 0 for row in rows),
            "cases": rows,
            "causal_priority_rank": None,
        }
    return {
        "case_count": len(cases),
        "positive_full_roi_residual_cases": sum(
            case["signed_full_roi_residual_percent"] > 0 for case in cases
        ),
        "negative_full_roi_residual_cases": sum(
            case["signed_full_roi_residual_percent"] < 0 for case in cases
        ),
        "components": components,
        "issue_gates": issue_gates,
        "automatic_component_ranking": "disabled-until-semantic-witnesses",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case", nargs=4, action="append", required=True,
        metavar=("LABEL", "SIGNED_RESIDUAL_PERCENT", "FASTSIM_STATS", "GEM5_PAIRS"),
        help="repeat for each cross-workload anchor",
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    cases = [
        analyze_case(label, float(residual), Path(stats), Path(pairs))
        for label, residual, stats, pairs in args.case
    ]
    report = {
        "schema": "fastsim-cross-workload-component-matrix-v2",
        "semantics": {
            "local_frontend_residual":
                "FastSim fetch-to-issue minus gem5 fetch-to-issue",
            "local_tail_residual":
                "FastSim issue-to-retire minus gem5 issue-to-commit",
            "conditioned_component_residual":
                "local residual grouped by FastSim observed gate; not causal attribution",
            "cumulative_residual":
                "ROI-to-milestone active-cycle bound; never assigned to the current gate",
            "cross_milestone_progress":
                "paired position/progress comparison; raw elapsed includes unknown gem5 idle",
            "issue_gate_owner":
                "direct FastSim issue constraint and producer identity; grouped residual remains non-causal",
            "checkpoint_interval_displacement":
                "deduplicated FastSim interval-gap increment for each sampled checkpoint",
        },
        "cases": cases,
        "cross_case": cross_case_summary(cases),
        "limitations": [
            "issue owners establish FastSim's direct local edge, not why gem5 differs",
            "gem5 issue-to-commit does not identify cache callback or SQ release by itself",
            "different sampling windows are reported explicitly and are not population-weighted",
            "full-ROI signed residual is context only and is not allocated over sampled events",
            "native hierarchy comparison requires exactly one FastSim data event and one gem5 line request",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "schema": report["schema"],
        "case_count": len(cases),
        "paired_samples": {
            case["case"]: case["evidence"]["pair_count"] for case in cases
        },
        "output": str(args.output.resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
