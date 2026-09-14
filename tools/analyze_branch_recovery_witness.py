#!/usr/bin/env python3
"""Analyze response-delayed branch recovery in a bounded FastSim audit.

Contiguous input supports a deliberately narrow counterfactual: it gates only
the first committed UOP after each selected misprediction on the corrected
branch completion, keeps the observed fetch-to-issue and issue-to-completion
service durations, and propagates the four trace register edges plus ordered
commit.  Sparse input may contain branch-miss/successor pairs separated by
gaps; that mode only locates observed necessary-order violations and never
claims a retire effect.  Neither mode replays cache/coherence choices,
StoreSet, FU/IQ/LSQ state, or gem5 timing.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path


SCHEMA = "fastsim-branch-recovery-witness-v1"


def fingerprint(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def validate_samples(samples: list[dict]) -> bool:
    if len(samples) < 2:
        raise ValueError("branch recovery audit requires at least two samples")
    contiguous = True
    for left, right in zip(samples, samples[1:]):
        left_sequence = int(left["sequence"])
        right_sequence = int(right["sequence"])
        if right_sequence <= left_sequence:
            raise ValueError("branch recovery audit must be sequence-ordered")
        contiguous &= right_sequence == left_sequence + 1
    required = {
        "sequence",
        "pc",
        "branch",
        "branch_miss",
        "producer_dists",
        "actual_fetch_cycle",
        "actual_issue_cycle",
        "actual_completion_cycle",
        "actual_retire_cycle",
        "has_load",
        "has_store",
    }
    missing = required - samples[0].keys()
    if missing:
        raise ValueError("audit samples lack fields: " + ", ".join(sorted(missing)))
    return contiguous


def replay(
    samples: list[dict],
    active_branches: set[int],
    commit_width: int,
    execute_to_commit: int,
) -> list[dict[str, int]]:
    """Run the bounded fixed-service counterfactual described above."""

    if commit_width <= 0:
        raise ValueError("commit width must be positive")
    completion: dict[int, int] = {}
    retirement: dict[int, int] = {}
    commit_slots: collections.Counter[int] = collections.Counter()
    result: list[dict[str, int]] = []
    previous_retire = 0
    previous_sample: dict | None = None
    for sample in samples:
        sequence = int(sample["sequence"])
        old_fetch = int(sample["actual_fetch_cycle"])
        old_issue = int(sample["actual_issue_cycle"])
        old_completion = int(sample["actual_completion_cycle"])
        old_retire = int(sample["actual_retire_cycle"])
        if not old_fetch <= old_issue <= old_completion <= old_retire:
            raise ValueError(f"non-monotone stage sample at sequence {sequence}")

        fetch = old_fetch
        issue = old_issue
        recovery_source = 0
        if (
            previous_sample is not None
            and int(previous_sample["sequence"]) in active_branches
        ):
            recovery_source = int(previous_sample["sequence"])
            recovery_floor = completion[recovery_source]
            if recovery_floor > fetch:
                fetch_to_issue = old_issue - old_fetch
                fetch = recovery_floor
                issue = max(issue, recovery_floor + fetch_to_issue)

        for distance in sample["producer_dists"][:4]:
            distance = int(distance)
            producer = sequence - distance
            if distance and producer in completion:
                issue = max(issue, completion[producer])

        current_completion = old_completion + (issue - old_issue)
        candidate_retire = max(
            old_retire,
            current_completion + execute_to_commit,
            previous_retire,
        )
        while commit_slots[candidate_retire] >= commit_width:
            candidate_retire += 1
        commit_slots[candidate_retire] += 1
        completion[sequence] = current_completion
        retirement[sequence] = candidate_retire
        previous_retire = candidate_retire
        result.append(
            {
                "sequence": sequence,
                "fetch": fetch,
                "issue": issue,
                "completion": current_completion,
                "retire": candidate_retire,
                "fetch_delta": fetch - old_fetch,
                "issue_delta": issue - old_issue,
                "completion_delta": current_completion - old_completion,
                "retire_delta": candidate_retire - old_retire,
                "recovery_source_sequence": recovery_source,
            }
        )
        previous_sample = sample
    return result


def direct_consumers(samples: list[dict], producer_sequence: int) -> list[dict]:
    consumers = []
    for sample in samples:
        sequence = int(sample["sequence"])
        if sequence <= producer_sequence:
            continue
        slots = [
            slot
            for slot, distance in enumerate(sample["producer_dists"][:4])
            if int(distance) and sequence - int(distance) == producer_sequence
        ]
        if slots:
            consumers.append(
                {
                    "sequence": sequence,
                    "pc": int(sample["pc"]),
                    "producer_slots": slots,
                    "has_load": int(sample["has_load"]),
                    "has_store": int(sample["has_store"]),
                }
            )
    return consumers


def observed_event(branch: dict, successor: dict) -> dict:
    branch_completion = int(branch["actual_completion_cycle"])
    successor_fetch = int(successor["actual_fetch_cycle"])
    successor_issue = int(successor["actual_issue_cycle"])
    return {
        "branch_sequence": int(branch["sequence"]),
        "branch_pc": int(branch["pc"]),
        "base_completion_cycle": int(branch["base_completion_cycle"]),
        "corrected_completion_cycle": branch_completion,
        "resolution_extension_cycles": branch_completion
        - int(branch["base_completion_cycle"]),
        "correct_path_sequence": int(successor["sequence"]),
        "correct_path_pc": int(successor["pc"]),
        "correct_path_has_load": int(successor["has_load"]),
        "correct_path_has_store": int(successor["has_store"]),
        "correct_path_fetch_cycle": successor_fetch,
        "correct_path_dispatch_cycle": int(successor["actual_dispatch_cycle"]),
        "correct_path_issue_cycle": successor_issue,
        "fetch_before_resolution_cycles": max(
            0, branch_completion - successor_fetch
        ),
        "issue_before_resolution_cycles": max(
            0, branch_completion - successor_issue
        ),
    }


def build_report(document: dict, source: Path, core: int) -> dict:
    cores = document.get("cores", [])
    if core < 0 or core >= len(cores):
        raise ValueError("audit core is outside the stats document")
    samples = cores[core].get("response_frontier_audit", [])
    contiguous = validate_samples(samples)
    config = document["configuration"]
    if int(config["interval_max_cycles"]) != 1024:
        raise ValueError("branch recovery screening keeps Q fixed at 1024")
    commit_width = int(config["commit_width"])
    execute_to_commit = int(config["execute_to_commit"])

    if contiguous:
        identity = replay(samples, set(), commit_width, execute_to_commit)
        if any(
            row[field] != 0
            for row in identity
            for field in (
                "fetch_delta",
                "issue_delta",
                "completion_delta",
                "retire_delta",
            )
        ):
            raise ValueError("zero-change fixed-service replay is not an identity")

    events = []
    selected = set()
    by_sequence = {int(sample["sequence"]): sample for sample in samples}
    unpaired_branch_misses = []
    for index, branch in enumerate(samples):
        if not int(branch["branch_miss"]):
            continue
        branch_sequence = int(branch["sequence"])
        successor = by_sequence.get(branch_sequence + 1)
        if successor is None:
            unpaired_branch_misses.append(branch_sequence)
            continue
        event = observed_event(branch, successor)
        fetch_violation = event["fetch_before_resolution_cycles"]
        if fetch_violation:
            selected.add(branch_sequence)
        if contiguous:
            counterfactual = replay(
                samples, {branch_sequence}, commit_width, execute_to_commit
            )
            successor_cf = counterfactual[index + 1]
            retire_deltas = [
                row["retire_delta"] for row in counterfactual[index:]
            ]
            consumers = direct_consumers(
                samples[index + 1 :], int(successor["sequence"])
            )
            event.update(
                {
                    "counterfactual_correct_path_fetch_cycle": successor_cf[
                        "fetch"
                    ],
                    "counterfactual_correct_path_issue_cycle": successor_cf[
                        "issue"
                    ],
                    "counterfactual_max_retire_delta_cycles": max(
                        retire_deltas
                    ),
                    "counterfactual_final_window_retire_delta_cycles": (
                        retire_deltas[-1]
                    ),
                    "first_direct_consumer": consumers[0]
                    if consumers
                    else None,
                }
            )
        else:
            event.update(
                {
                    "counterfactual_correct_path_fetch_cycle": None,
                    "counterfactual_correct_path_issue_cycle": None,
                    "counterfactual_max_retire_delta_cycles": None,
                    "counterfactual_final_window_retire_delta_cycles": None,
                    "first_direct_consumer": None,
                }
            )
        events.append(event)

    combined = (
        replay(samples, selected, commit_width, execute_to_commit)
        if contiguous
        else None
    )
    violations = [event for event in events if event["fetch_before_resolution_cycles"]]
    report = {
        "schema": SCHEMA,
        "input": fingerprint(source),
        "core": core,
        "q_cycles": int(config["interval_max_cycles"]),
        "audit_layout": "contiguous" if contiguous else "sparse_branch_pairs",
        "sample_begin_sequence": int(samples[0]["sequence"]),
        "sample_end_sequence": int(samples[-1]["sequence"]),
        "sample_uops": len(samples),
        "zero_change_replay_identity": True if contiguous else None,
        "branch_misses": len(events),
        "unpaired_branch_misses": unpaired_branch_misses,
        "necessary_order_violations": len(violations),
        "violations_with_correct_path_load": sum(
            event["correct_path_has_load"] for event in violations
        ),
        "sum_fetch_before_resolution_cycles_diagnostic_only": sum(
            event["fetch_before_resolution_cycles"] for event in violations
        ),
        "maximum_fetch_before_resolution_cycles": max(
            (event["fetch_before_resolution_cycles"] for event in violations),
            default=0,
        ),
        "combined_fixed_service_max_retire_delta_cycles": (
            max(row["retire_delta"] for row in combined)
            if combined is not None
            else None
        ),
        "combined_fixed_service_final_retire_delta_cycles": (
            combined[-1]["retire_delta"] if combined is not None else None
        ),
        "events": events,
        "limitations": [
            "No gem5 stage label is used as an inference input.",
            "The fixed-service replay gates only the first correct-path UOP.",
            "Cache/coherence path choice, StoreSet, FU, IQ, LSQ, and shared-state replay are held fixed.",
            "Summed early cycles are not CPI benefit; only retire deltas expose this bounded critical span.",
        ],
    }
    if not contiguous:
        report["limitations"].append(
            "Sparse branch-pair screening omits intervening UOPs, so no "
            "counterfactual retire delta or direct-consumer claim is made."
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stats", type=Path)
    parser.add_argument("--core", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_report(json.loads(args.stats.read_text()), args.stats, args.core)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(args.output)


if __name__ == "__main__":
    main()
