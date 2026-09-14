#!/usr/bin/env python3
"""Locate the first semantic divergence and observed CPI sign reversal.

The input is one or more baseline/candidate FastSim stats pairs produced with
``core.response_frontier_audit_stride_uops``.  The report keeps raw absolute
stage deltas for end-to-end timing, and separately removes each sample's
accumulated interval-gap translation before comparing frontier semantics.
It reports observed gates and owners; it does not infer that a sampled UOP is
the original cause when an older live frontier already differs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


LOCAL_ABSOLUTE_CYCLE_FIELDS = {
    "base_fetch_cycle",
    "actual_fetch_cycle",
    "actual_rename_cycle",
    "actual_dispatch_cycle",
    "base_issue_cycle",
    "actual_issue_cycle",
    "base_completion_cycle",
    "actual_completion_cycle",
    "memory_response_cycle",
    "base_retire_cycle",
    "actual_retire_cycle",
    "commit_cycle",
    "store_drain_ready_cycle",
    "sequencer_min_release_cycle",
    "iq_min_release_cycle",
    "rob_head_retire_cycle",
    "lq_head_release_cycle",
    "sq_head_release_cycle",
    "rob_capacity_predecessor_retire_cycle",
    "prior_commit_cycle",
    "incoming_sq_release_cycle",
    "selected_memory_corrected_issue_cycle",
    "selected_memory_response_cycle",
}

PARTITION_METADATA_FIELDS = {
    "checkpoint_begin_sequence",
    "checkpoint_end_sequence",
    "checkpoint_uop_offset",
    "checkpoint_uops",
    "memory_events",
}

STAGE_FIELDS = (
    "actual_fetch_cycle",
    "actual_rename_cycle",
    "actual_dispatch_cycle",
    "actual_issue_cycle",
    "actual_completion_cycle",
    "memory_response_cycle",
    "actual_retire_cycle",
    "commit_cycle",
)

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


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: top-level JSON must be an object")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Remove only the sample's accumulated local scalar translation."""
    origin = int(sample["interval_gap_cycles"])
    result = {
        key: value
        for key, value in sample.items()
        if key != "interval_gap_cycles" and key not in PARTITION_METADATA_FIELDS
    }
    for field in LOCAL_ABSOLUTE_CYCLE_FIELDS:
        if field not in result:
            continue
        cycle = int(result[field])
        result[field] = 0 if cycle == 0 else cycle - origin
    return result


def functional_signature(sample: dict[str, Any]) -> tuple[Any, ...]:
    events = tuple(
        (
            event.get("ordinal"),
            event.get("line"),
            event.get("write"),
            event.get("instruction_fetch"),
        )
        for event in sample.get("memory_events", [])
    )
    return (
        sample.get("sequence"),
        sample.get("pc"),
        sample.get("has_load"),
        sample.get("has_store"),
        sample.get("branch"),
        sample.get("branch_miss"),
        tuple(sample.get("producer_dists", [])),
        events,
    )


def sample_index(
    stats: dict[str, Any], core: int, source: Path
) -> dict[int, dict[str, Any]]:
    try:
        samples = stats["cores"][core]["response_frontier_audit"]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError(f"{source}: missing core {core} frontier audit") from error
    indexed = {int(sample["sequence"]): sample for sample in samples}
    if len(indexed) != len(samples):
        raise ValueError(f"{source}: duplicate sequence in core {core} audit")
    return indexed


def cause(value: Any) -> str:
    return CAUSE_NAMES.get(int(value or 0), f"unknown_{value}")


def selected_memory(sample: dict[str, Any]) -> dict[str, Any] | None:
    if not sample.get("selected_memory_valid"):
        return None
    names = (
        "ordinal",
        "line",
        "path",
        "write",
        "unique_dram_request",
        "corrected_issue_cycle",
        "shared_stage_issue_cycle",
        "canonical_dram_arrival_cycle",
        "canonical_dram_bank_command_cycle",
        "canonical_dram_command_cycle",
        "canonical_dram_completion_cycle",
        "canonical_fill_cycle",
        "shared_response_cycle",
        "response_cycle",
        "latency_cycles",
        "exposed_cycles",
        "blocks_retirement",
        "canonical_dram_command_blocker_core",
        "canonical_dram_command_blocker_sequence",
        "canonical_dram_command_blocker_ordinal",
        "canonical_dram_command_blocker_line",
        "canonical_dram_command_blocker_root_core",
        "canonical_dram_command_blocker_root_sequence",
        "canonical_dram_command_blocker_root_ordinal",
        "canonical_dram_command_blocker_root_line",
    )
    return {
        name: sample.get(f"selected_memory_{name}")
        for name in names
    }


def sq_owner(sample: dict[str, Any]) -> dict[str, Any] | None:
    if not sample.get("incoming_sq_release_valid"):
        return None
    return {
        "sequence": sample.get("incoming_sq_release_sequence"),
        "release_cycle": sample.get("incoming_sq_release_cycle"),
        "displacement_cycles": sample.get(
            "incoming_sq_release_displacement_cycles"
        ),
    }


def observed_components(
    baseline: dict[str, Any], candidate: dict[str, Any], fields: list[str]
) -> list[str]:
    changed = set(fields)
    components: list[str] = []
    path_changed = bool(changed & {
        "selected_memory_valid",
        "selected_memory_path",
        "selected_memory_unique_dram_request",
        "selected_memory_uncore_request",
        "selected_memory_descriptor_line",
        "selected_memory_descriptor_memory_line",
    })
    if path_changed:
        components.append("memory_path_or_visibility")
    if not path_changed and any(
        field.startswith("selected_memory_canonical_dram_")
        for field in changed
    ):
        components.append("dram_calendar_or_blocker")
    causes = {
        cause(baseline.get("dispatch_cause")),
        cause(candidate.get("dispatch_cause")),
        cause(baseline.get("completion_cause")),
        cause(candidate.get("completion_cause")),
        cause(baseline.get("retire_cause")),
        cause(candidate.get("retire_cause")),
    }
    for name in (
        "sq_capacity",
        "lq_capacity",
        "rob_capacity",
        "iq_capacity",
        "sequencer",
        "dependency",
        "memory_response",
        "tso_store",
        "instruction_fetch",
        "branch_recovery",
    ):
        if name in causes:
            components.append(name)
    if changed & {
        "incoming_sq_release_sequence",
        "incoming_sq_release_cycle",
        "sq_head_release_cycle",
        "sq_digest",
        "store_drain_ready_cycle",
    } and "sq_capacity" not in components:
        components.append("sq_lifecycle")
    return components or ["pipeline_frontier"]


def primary_observed_gate(
    baseline: dict[str, Any], candidate: dict[str, Any], fields: list[str]
) -> str:
    changed = set(fields)
    if changed & {
        "selected_memory_valid",
        "selected_memory_path",
        "selected_memory_unique_dram_request",
        "selected_memory_uncore_request",
        "selected_memory_descriptor_line",
        "selected_memory_descriptor_memory_line",
    }:
        return "memory_path_or_visibility"
    if any(
        field.startswith("selected_memory_canonical_dram_")
        for field in changed
    ):
        return "dram_calendar_or_blocker"
    fetch_delta = int(candidate.get("actual_fetch_cycle", 0)) - int(
        baseline.get("actual_fetch_cycle", 0)
    )
    dispatch_delta = int(candidate.get("actual_dispatch_cycle", 0)) - int(
        baseline.get("actual_dispatch_cycle", 0)
    )
    dispatch_gate = cause(candidate.get("dispatch_cause"))
    if dispatch_delta != fetch_delta and dispatch_gate != "unattributed":
        return dispatch_gate
    completion_gate = cause(candidate.get("completion_cause"))
    if completion_gate != "unattributed":
        return completion_gate
    return cause(candidate.get("retire_cause"))


def event_summary(
    core: int,
    sequence: int,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base_normalized = normalize_sample(baseline)
    candidate_normalized = normalize_sample(candidate)
    fields = sorted(
        key
        for key in set(base_normalized) | set(candidate_normalized)
        if base_normalized.get(key) != candidate_normalized.get(key)
    )
    result = {
        "core": core,
        "sequence": sequence,
        "pc": int(baseline.get("pc", 0)),
        "pc_hex": hex(int(baseline.get("pc", 0))),
        "has_load": bool(baseline.get("has_load")),
        "has_store": bool(baseline.get("has_store")),
        "raw_stage_delta_cycles": {
            field: int(candidate.get(field, 0)) - int(baseline.get(field, 0))
            for field in STAGE_FIELDS
        },
        "normalized_stage_delta_cycles": {
            field: int(candidate_normalized.get(field, 0))
            - int(base_normalized.get(field, 0))
            for field in STAGE_FIELDS
        },
        "interval_gap_delta_cycles": int(candidate["interval_gap_cycles"])
        - int(baseline["interval_gap_cycles"]),
        "baseline_actual_retire_cycle": int(
            baseline["actual_retire_cycle"]
        ),
        "candidate_actual_retire_cycle": int(
            candidate["actual_retire_cycle"]
        ),
        "causes": {
            side: {
                name: cause(sample.get(f"{name}_cause"))
                for name in ("dispatch", "completion", "retire")
            }
            for side, sample in (
                ("baseline", baseline),
                ("candidate", candidate),
            )
        },
        "observed_components": observed_components(
            baseline, candidate, fields
        ),
        "primary_observed_gate": primary_observed_gate(
            baseline, candidate, fields
        ),
        "mismatched_normalized_fields": fields,
        "baseline": {
            "selected_memory": selected_memory(baseline),
            "incoming_sq_owner": sq_owner(baseline),
            "sq_head_release_cycle": baseline.get("sq_head_release_cycle"),
            "store_drain_ready_cycle": baseline.get(
                "store_drain_ready_cycle"
            ),
        },
        "candidate": {
            "selected_memory": selected_memory(candidate),
            "incoming_sq_owner": sq_owner(candidate),
            "sq_head_release_cycle": candidate.get("sq_head_release_cycle"),
            "store_drain_ready_cycle": candidate.get(
                "store_drain_ready_cycle"
            ),
        },
    }
    if previous is not None:
        result["previous_observed_sequence"] = previous["sequence"]
        result["previous_raw_retire_delta_cycles"] = previous["delta"]
    return result


def analyze_core(
    core: int,
    baseline: dict[int, dict[str, Any]],
    candidate: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    common = sorted(set(baseline) & set(candidate))
    if not common:
        return {"core": core, "common_sample_count": 0}
    for sequence in common:
        if functional_signature(baseline[sequence]) != functional_signature(
            candidate[sequence]
        ):
            raise ValueError(
                f"core {core} sequence {sequence}: functional identity differs"
            )

    first_mismatch = None
    last_equal = None
    first_lead = None
    first_lag = None
    lead_to_lag = None
    previous_nonzero_sign = 0
    previous_observation = None
    for sequence in common:
        left = baseline[sequence]
        right = candidate[sequence]
        normalized_equal = normalize_sample(left) == normalize_sample(right)
        if first_mismatch is None:
            if normalized_equal:
                last_equal = sequence
            else:
                first_mismatch = event_summary(
                    core, sequence, left, right, previous_observation
                )
                first_mismatch["last_equal_sequence"] = last_equal

        delta = int(right["actual_retire_cycle"]) - int(
            left["actual_retire_cycle"]
        )
        observation = {"sequence": sequence, "delta": delta}
        if delta < 0 and first_lead is None:
            first_lead = event_summary(
                core, sequence, left, right, previous_observation
            )
        if delta > 0 and first_lag is None:
            first_lag = event_summary(
                core, sequence, left, right, previous_observation
            )
        sign = (delta > 0) - (delta < 0)
        if previous_nonzero_sign < 0 and sign > 0 and lead_to_lag is None:
            lead_to_lag = event_summary(
                core, sequence, left, right, previous_observation
            )
        if sign:
            previous_nonzero_sign = sign
        previous_observation = observation

    return {
        "core": core,
        "common_sample_count": len(common),
        "sample_begin_sequence": common[0],
        "sample_end_sequence": common[-1],
        "first_semantic_mismatch": first_mismatch,
        "first_raw_retire_lead": first_lead,
        "first_raw_retire_lag": first_lag,
        "first_observed_lead_to_lag": lead_to_lag,
    }


def analyze_pair(label: str, baseline_path: Path, candidate_path: Path) -> dict[str, Any]:
    baseline_stats = read_json(baseline_path)
    candidate_stats = read_json(candidate_path)
    baseline_cores = baseline_stats.get("cores", [])
    candidate_cores = candidate_stats.get("cores", [])
    if len(baseline_cores) != len(candidate_cores):
        raise ValueError(f"{label}: baseline/candidate core counts differ")
    cores = []
    for core in range(len(baseline_cores)):
        left = sample_index(baseline_stats, core, baseline_path)
        right = sample_index(candidate_stats, core, candidate_path)
        if bool(left) != bool(right):
            raise ValueError(
                f"{label}: core {core} audit exists on only one side"
            )
        if left or right:
            cores.append(analyze_core(core, left, right))
    return {
        "label": label,
        "baseline": {
            "path": str(baseline_path.resolve()),
            "sha256": sha256(baseline_path),
        },
        "candidate": {
            "path": str(candidate_path.resolve()),
            "sha256": sha256(candidate_path),
        },
        "cores": cores,
    }


def earliest(
    pairs: list[dict[str, Any]], field: str
) -> dict[str, Any] | None:
    events = []
    for pair in pairs:
        for core in pair["cores"]:
            event = core.get(field)
            if event is None:
                continue
            events.append(
                {
                    "pair": pair["label"],
                    **event,
                }
            )
    if not events:
        return None
    return min(
        events,
        key=lambda event: min(
            event["baseline_actual_retire_cycle"],
            event["candidate_actual_retire_cycle"],
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pair",
        nargs=3,
        action="append",
        metavar=("LABEL", "BASELINE", "CANDIDATE"),
        required=True,
        help="named baseline/candidate stats pair; repeat for more windows",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pairs = [
        analyze_pair(label, Path(baseline), Path(candidate))
        for label, baseline, candidate in args.pair
    ]
    report = {
        "schema": "fastsim-paired-frontier-divergence-v1",
        "semantics": {
            "semantic_comparison": (
                "local stage cycles minus each sample interval_gap; shared "
                "canonical memory cycles remain absolute"
            ),
            "raw_retire_delta": "candidate minus baseline; negative is lead",
            "causality_limit": (
                "component labels identify the observed gate at a sampled "
                "UOP; an older live frontier can be the original cause"
            ),
        },
        "pairs": pairs,
        "earliest_reported_semantic_mismatch": earliest(
            pairs, "first_semantic_mismatch"
        ),
        "earliest_reported_raw_retire_lag": earliest(
            pairs, "first_raw_retire_lag"
        ),
        "earliest_reported_lead_to_lag": earliest(
            pairs, "first_observed_lead_to_lag"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "pair_count": len(pairs),
                "earliest_reported_semantic_mismatch": report[
                    "earliest_reported_semantic_mismatch"
                ],
                "earliest_reported_lead_to_lag": report[
                    "earliest_reported_lead_to_lag"
                ],
                "output": str(args.output.resolve()),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
