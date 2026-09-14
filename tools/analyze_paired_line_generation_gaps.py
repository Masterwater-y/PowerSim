#!/usr/bin/env python3
"""Decompose paired line-generation disagreements into spacing and lifetime.

The input is a FastSim response-frontier audit and its paired gem5 native
response ledger.  Events are matched by committed record identity, then each
side independently reconstructs half-open same-core/same-line generations.
For a disagreement whose parent is present, the tool asks whether replacing
only issue spacing or only the parent's callback lifetime would change the
classification.  These local counterfactuals diagnose the admission/response
boundary; they are not additive CPI estimates.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA = "fastsim-paired-line-generation-gaps-v1"
ISSUE_GATES = (
    "none", "dispatch_admission", "serialize_after_carry",
    "serialize_before", "memory_barrier_carry", "memory_barrier_head",
    "register_producer", "store_set_producer", "issue_resource",
    "dtlb_pending_fill", "dtlb_walker", "page_walk_dependency",
    "sequencer", "l1_mshr", "l2_mshr")


@dataclass
class Event:
    core: int
    sequence: int
    line: int
    fastsim_admission: int
    fastsim_response: int
    native_admission_tick: int
    native_response_tick: int
    native_coalesced: bool
    fastsim_issue_gate: int = 0
    fastsim_issue_gate_owner_sequence: int | None = None
    fastsim_producer_dists: tuple[int, ...] = field(default_factory=tuple)
    native_producer_dists: tuple[int, ...] = field(default_factory=tuple)
    fastsim_parent: int | None = None
    native_parent: int | None = None


def distribution(values: Iterable[float | int]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "minimum": None, "p50": None,
                "p90": None, "maximum": None, "mean": None}

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    return {
        "count": len(ordered),
        "minimum": ordered[0],
        "p50": percentile(0.5),
        "p90": percentile(0.9),
        "maximum": ordered[-1],
        "mean": sum(ordered) / len(ordered),
    }


def infer_parents(
    events: list[Event], admission: Callable[[Event], int],
    response: Callable[[Event], int], attribute: str,
) -> None:
    active: dict[tuple[int, int], int] = {}
    order = sorted(range(len(events)), key=lambda index: (
        admission(events[index]), events[index].core, events[index].sequence))
    for index in order:
        event = events[index]
        key = (event.core, event.line)
        parent = active.get(key)
        if parent is not None and response(events[parent]) <= admission(event):
            del active[key]
            parent = None
        setattr(event, attribute, parent)
        if parent is None and response(event) > admission(event):
            active[key] = index


def load_events(stats_path: Path, pairs_path: Path) -> tuple[list[Event], Counter[str]]:
    stats = json.loads(stats_path.read_text(encoding="utf-8"))
    pairs = json.loads(pairs_path.read_text(encoding="utf-8"))
    samples: dict[tuple[int, int], dict[str, Any]] = {}
    exclusions: Counter[str] = Counter()
    for core in stats.get("cores", []):
        core_id = int(core["core"])
        for sample in core.get("response_frontier_audit", []):
            key = (core_id, int(sample["sequence"]))
            if key in samples:
                raise ValueError(f"duplicate FastSim audit identity {key}")
            samples[key] = sample

    events: list[Event] = []
    for core in pairs.get("cores", []):
        core_id = int(core["core"])
        for pair in core.get("pairs", []):
            if not bool(pair.get("is_load")) or bool(pair.get("is_store")) or \
                    bool(pair.get("is_atomic")):
                exclusions["not_plain_load"] += 1
                continue
            native = pair.get("gem5_native")
            if not isinstance(native, dict):
                exclusions["no_native_lifecycle"] += 1
                continue
            if native.get("attribution_source") != "packet" or \
                    int(native.get("line_requests", 0)) != 1 or \
                    not bool(native.get("response_timestamps_available")):
                exclusions["native_not_single_packet"] += 1
                continue
            key = (core_id, int(pair["record_ordinal"]))
            sample = samples.get(key)
            if sample is None:
                exclusions["outside_fastsim_audit"] += 1
                continue
            memory = sample.get("memory_events", [])
            if len(memory) != 1:
                exclusions["fastsim_not_single_event"] += 1
                continue
            event = memory[0]
            if bool(event.get("write")) or bool(event.get("instruction_fetch")):
                exclusions["fastsim_not_plain_load"] += 1
                continue
            fastsim_admission = int(event["corrected_issue_cycle"]) + 1
            fastsim_response = int(event["response_cycle"])
            native_admission = int(native.get("native_first_admission_tick", 0))
            native_response = int(native.get("native_last_response_tick", 0))
            if fastsim_response < fastsim_admission:
                exclusions["invalid_fastsim_interval"] += 1
                continue
            if native_admission == 0 or native_response < native_admission:
                exclusions["invalid_native_interval"] += 1
                continue
            events.append(Event(
                core=core_id,
                sequence=key[1],
                line=int(event["line"]),
                fastsim_admission=fastsim_admission,
                fastsim_response=fastsim_response,
                native_admission_tick=native_admission,
                native_response_tick=native_response,
                native_coalesced=bool(native.get("coalesced", 0)),
                fastsim_issue_gate=int(sample.get("issue_gate_kind", 0)),
                fastsim_issue_gate_owner_sequence=(
                    int(sample.get("issue_gate_owner_sequence", 0))
                    if bool(sample.get("issue_gate_owner_valid")) else None),
                fastsim_producer_dists=tuple(
                    int(value) for value in pair.get("producer_dists", [])
                    if int(value) != 0),
                native_producer_dists=tuple(
                    int(value) for value in pair.get(
                        "gem5_producer_dists", []) if int(value) != 0),
            ))
    return events, exclusions


def analyze(events: list[Event], clock_period_ticks: int) -> dict[str, Any]:
    if clock_period_ticks <= 0:
        raise ValueError("clock period ticks must be positive")
    infer_parents(events, lambda event: event.fastsim_admission,
                  lambda event: event.fastsim_response, "fastsim_parent")
    infer_parents(events, lambda event: event.native_admission_tick,
                  lambda event: event.native_response_tick, "native_parent")

    inferred_confusion: Counter[tuple[bool, bool]] = Counter()
    oracle_confusion: Counter[tuple[bool, bool]] = Counter()
    oracle_mismatches = 0
    disagreement_causes: dict[str, Counter[str]] = {
        "native_only": Counter(), "fastsim_only": Counter()}
    disagreement_issue_gates: dict[str, Counter[str]] = {
        "native_only": Counter(), "fastsim_only": Counter()}
    disagreement_parent_edges: dict[str, Counter[str]] = {
        "native_only": Counter(), "fastsim_only": Counter()}
    metrics: dict[str, dict[str, list[float]]] = {
        direction: {
            "fastsim_issue_gap_cycles": [],
            "native_issue_gap_cycles": [],
            "issue_gap_fastsim_minus_native_cycles": [],
            "fastsim_parent_lifetime_cycles": [],
            "native_parent_lifetime_cycles": [],
            "parent_lifetime_fastsim_minus_native_cycles": [],
        } for direction in disagreement_causes
    }

    def record_axes(index: int, direction: str, parent_index: int) -> None:
        event = events[index]
        parent = events[parent_index]
        gate = event.fastsim_issue_gate
        gate_name = ISSUE_GATES[gate] if 0 <= gate < len(ISSUE_GATES) \
            else f"unknown_{gate}"
        disagreement_issue_gates[direction][gate_name] += 1
        edge_counts = disagreement_parent_edges[direction]
        edge_counts["evaluated"] += 1
        direct_edge = False
        distance = event.sequence - parent.sequence
        if distance <= 0:
            edge_counts["parent_not_older_in_fastsim_order"] += 1
        else:
            if distance in event.fastsim_producer_dists:
                edge_counts["parent_in_fastsim_producer_dists"] += 1
                direct_edge = True
            if distance in event.native_producer_dists:
                edge_counts["parent_in_native_producer_dists"] += 1
                direct_edge = True
        if event.fastsim_issue_gate_owner_sequence == parent.sequence:
            edge_counts["parent_is_fastsim_winning_issue_owner"] += 1
            direct_edge = True
        if not direct_edge:
            edge_counts["no_recorded_direct_parent_edge"] += 1
        fastsim_gap = event.fastsim_admission - parent.fastsim_admission
        native_gap_ticks = (
            event.native_admission_tick - parent.native_admission_tick)
        fastsim_lifetime = (
            parent.fastsim_response - parent.fastsim_admission)
        native_lifetime_ticks = (
            parent.native_response_tick - parent.native_admission_tick)
        native_gap = native_gap_ticks / clock_period_ticks
        native_lifetime = native_lifetime_ticks / clock_period_ticks
        values = metrics[direction]
        values["fastsim_issue_gap_cycles"].append(fastsim_gap)
        values["native_issue_gap_cycles"].append(native_gap)
        values["issue_gap_fastsim_minus_native_cycles"].append(
            fastsim_gap - native_gap)
        values["fastsim_parent_lifetime_cycles"].append(fastsim_lifetime)
        values["native_parent_lifetime_cycles"].append(native_lifetime)
        values["parent_lifetime_fastsim_minus_native_cycles"].append(
            fastsim_lifetime - native_lifetime)

        causes = disagreement_causes[direction]
        if fastsim_gap < 0 or native_gap_ticks < 0:
            causes["parent_order_reversed"] += 1
            return
        if direction == "native_only":
            spacing_suffices = native_gap_ticks < (
                fastsim_lifetime * clock_period_ticks)
            lifetime_suffices = (fastsim_gap * clock_period_ticks) < \
                native_lifetime_ticks
        else:
            spacing_suffices = (fastsim_gap * clock_period_ticks) < \
                native_lifetime_ticks
            lifetime_suffices = native_gap_ticks < (
                fastsim_lifetime * clock_period_ticks)
        if spacing_suffices and lifetime_suffices:
            causes["either_axis_suffices"] += 1
        elif spacing_suffices:
            causes["issue_spacing_only_suffices"] += 1
        elif lifetime_suffices:
            causes["parent_lifetime_only_suffices"] += 1
        else:
            causes["both_axes_required"] += 1

    for index, event in enumerate(events):
        fastsim_follower = event.fastsim_parent is not None
        native_follower = event.native_parent is not None
        inferred_confusion[(fastsim_follower, native_follower)] += 1
        oracle_confusion[(fastsim_follower, event.native_coalesced)] += 1
        if native_follower != event.native_coalesced:
            oracle_mismatches += 1
            continue
        reference_parent = event.native_parent if native_follower \
            else event.fastsim_parent
        if reference_parent is not None:
            parent = events[reference_parent]
            if (parent.native_parent is not None) != parent.native_coalesced:
                disagreement_causes[
                    "native_only" if native_follower else "fastsim_only"
                ]["parent_oracle_mismatch"] += 1
                continue
        if native_follower and not fastsim_follower:
            record_axes(index, "native_only", event.native_parent)
        elif fastsim_follower and not native_follower:
            record_axes(index, "fastsim_only", event.fastsim_parent)

    return {
        "events": len(events),
        "clock_period_ticks": clock_period_ticks,
        "native_oracle_mismatches": oracle_mismatches,
        "fastsim_vs_native_oracle": {
            "both": oracle_confusion[(True, True)],
            "fastsim_only": oracle_confusion[(True, False)],
            "native_only": oracle_confusion[(False, True)],
            "neither": oracle_confusion[(False, False)],
        },
        "inferred_follower_confusion": {
            "both": inferred_confusion[(True, True)],
            "fastsim_only": inferred_confusion[(True, False)],
            "native_only": inferred_confusion[(False, True)],
            "neither": inferred_confusion[(False, False)],
        },
        "disagreement_axis_classification": {
            direction: dict(sorted(counts.items()))
            for direction, counts in disagreement_causes.items()
        },
        "disagreement_issue_gates": {
            direction: dict(sorted(counts.items()))
            for direction, counts in disagreement_issue_gates.items()
        },
        "disagreement_parent_edges": {
            direction: dict(sorted(counts.items()))
            for direction, counts in disagreement_parent_edges.items()
        },
        "disagreement_distributions": {
            direction: {
                name: distribution(values)
                for name, values in groups.items()
            } for direction, groups in metrics.items()
        },
        "interpretation": (
            "Axis classifications are local parent counterfactuals. They do "
            "not estimate CPI or certify that either timestamp source is "
            "globally correct."),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", action="append", nargs=3,
        metavar=("NAME", "FASTSIM_JSON", "PAIRS_JSON"), required=True)
    parser.add_argument("--clock-period-ticks", type=int, default=333)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    cases: list[dict[str, Any]] = []
    for name, stats_name, pairs_name in args.case:
        stats_path = Path(stats_name)
        pairs_path = Path(pairs_name)
        events, exclusions = load_events(stats_path, pairs_path)
        cases.append({
            "name": name,
            "inputs": {
                "fastsim": str(stats_path), "pairs": str(pairs_path)},
            "exclusions": dict(sorted(exclusions.items())),
            "analysis": analyze(events, args.clock_period_ticks),
        })
    result = {
        "schema": SCHEMA,
        "scope": "paired committed single-load dense audit windows",
        "cases": cases,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


if __name__ == "__main__":
    main()
