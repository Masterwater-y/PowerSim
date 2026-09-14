#!/usr/bin/env python3
"""Audit load-only line-generation closure from gem5 native response ledgers.

This is an evidence tool, not a FastSim timing model.  It joins native
Sequencer admission/callback timestamps to the physical data line in the Tao
trace, reconstructs half-open active intervals, and reports which observed
components are clean under the current conservative load-only contract.
Instruction-side requests and state preceding the observed ledger are not
available, so a clean component is not a production integration certificate.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import heapq
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA = "fastsim-line-generation-component-audit-v1"
SEQ_RE = re.compile(rb'"seq_num":([0-9]+)')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Event:
    core: int
    sequence: int
    line: int
    admission: int
    callback: int
    kind: str
    native_coalesced: bool
    shared_touch: bool
    l1_set: int
    l2_set: int
    llc_set: int
    scope: str = "unknown"
    reasons: set[str] = field(default_factory=set)
    inferred_follower: bool = False


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def join(self, first: int, second: int) -> None:
        first = self.find(first)
        second = self.find(second)
        if first == second:
            return
        if self.rank[first] < self.rank[second]:
            first, second = second, first
        self.parent[second] = first
        if self.rank[first] == self.rank[second]:
            self.rank[first] += 1


def distribution(values: Iterable[int]) -> dict[str, float | int | None]:
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


def connect_active_domain(
    events: list[Event],
    order: list[int],
    union: UnionFind,
    key: Callable[[Event], Any | None],
    conflict_reason: str | None = None,
    conflict: Callable[[Event, Event], bool] | None = None,
) -> None:
    active: dict[Any, list[tuple[int, int]]] = defaultdict(list)
    for index in order:
        event = events[index]
        domain = key(event)
        if domain is None:
            continue
        heap = active[domain]
        while heap and heap[0][0] <= event.admission:
            heapq.heappop(heap)
        if heap:
            prior = heap[0][1]
            union.join(index, prior)
            if conflict_reason is not None and conflict is not None and \
                    conflict(event, events[prior]):
                event.reasons.add(conflict_reason)
        if event.callback > event.admission:
            heapq.heappush(heap, (event.callback, index))


def analyze_events(events: list[Event]) -> dict[str, Any]:
    order = sorted(range(len(events)), key=lambda index: (
        events[index].admission, events[index].callback,
        events[index].core, events[index].sequence))
    union = UnionFind(len(events))

    connect_active_domain(
        events, order, union, lambda event: (event.core, event.line))
    connect_active_domain(
        events, order, union, lambda event: (event.core, event.l1_set),
        "private_l1_set_different_line", lambda a, b: a.line != b.line)
    connect_active_domain(
        events, order, union, lambda event: (event.core, event.l2_set),
        "private_l2_set_different_line", lambda a, b: a.line != b.line)
    connect_active_domain(
        events, order, union,
        lambda event: event.llc_set if event.shared_touch else None,
        "shared_llc_set_different_line", lambda a, b: a.line != b.line)
    connect_active_domain(
        events, order, union, lambda event: event.line,
        "cross_core_same_line", lambda a, b: a.core != b.core)

    # Infer the exact same-core, same-line attachment opportunity separately
    # from component closure so it can be compared with gem5's coalesced bit.
    active_lines: dict[tuple[int, int], list[tuple[int, int]]] = defaultdict(list)
    for index in order:
        event = events[index]
        heap = active_lines[(event.core, event.line)]
        while heap and heap[0][0] <= event.admission:
            heapq.heappop(heap)
        event.inferred_follower = event.kind == "load" and bool(heap)
        if event.kind == "load" and \
                event.inferred_follower != event.native_coalesced:
            # The native ledger contains committed identities only.  A
            # mismatch can therefore expose an unobserved speculative parent
            # as well as a request-type/lifecycle distinction.  Either way,
            # this component is not safe for the current load-only contract.
            event.reasons.add("follower_oracle_mismatch")
        if event.callback > event.admission:
            heapq.heappush(heap, (event.callback, index))

    members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(events)):
        members[union.find(index)].append(index)

    component_reason_counts: Counter[str] = Counter()
    event_primary_counts: Counter[str] = Counter()
    component_sizes: list[int] = []
    clean_component_sizes: list[int] = []
    clean_events = 0
    clean_followers = 0
    clean_native_coalesced = 0
    clean_indices: set[int] = set()
    reason_priority = (
        "unsupported_atomic", "unsupported_store", "unsupported_kind",
        "multi_line_request", "page_walk_dependency",
        "follower_oracle_mismatch",
        "cross_core_same_line", "private_l1_set_different_line",
        "private_l2_set_different_line", "shared_llc_set_different_line",
    )
    for indices in members.values():
        component_sizes.append(len(indices))
        reasons = set().union(*(events[index].reasons for index in indices))
        if reasons:
            for reason in reasons:
                component_reason_counts[reason] += 1
            primary = next(reason for reason in reason_priority if reason in reasons)
        else:
            primary = "observed_clean"
            clean_component_sizes.append(len(indices))
            clean_indices.update(indices)
            clean_events += len(indices)
            clean_followers += sum(events[index].inferred_follower for index in indices)
            clean_native_coalesced += sum(
                events[index].native_coalesced for index in indices)
        event_primary_counts[primary] += len(indices)

    follower_confusion = Counter()
    clean_follower_confusion = Counter()
    for index, event in enumerate(events):
        if event.kind != "load":
            continue
        key = (event.inferred_follower, event.native_coalesced)
        follower_confusion[key] += 1
        if index in clean_indices:
            clean_follower_confusion[key] += 1

    total = len(events)
    return {
        "events": total,
        "components": len(members),
        "observed_clean_events": clean_events,
        "observed_clean_event_fraction": clean_events / total if total else None,
        "observed_clean_components": len(clean_component_sizes),
        "observed_clean_component_fraction": (
            len(clean_component_sizes) / len(members) if members else None),
        "observed_clean_inferred_followers": clean_followers,
        "observed_clean_native_coalesced": clean_native_coalesced,
        "component_sizes": distribution(component_sizes),
        "observed_clean_component_sizes": distribution(clean_component_sizes),
        "component_reason_counts_nonexclusive": dict(sorted(component_reason_counts.items())),
        "event_primary_reason_counts": dict(sorted(event_primary_counts.items())),
        "event_primary_reason_conserved": sum(event_primary_counts.values()) == total,
        "follower_confusion": {
            "inferred_and_native": follower_confusion[(True, True)],
            "inferred_only": follower_confusion[(True, False)],
            "native_only": follower_confusion[(False, True)],
            "neither": follower_confusion[(False, False)],
        },
        "observed_clean_follower_confusion": {
            "inferred_and_native": clean_follower_confusion[(True, True)],
            "inferred_only": clean_follower_confusion[(True, False)],
            "native_only": clean_follower_confusion[(False, True)],
            "neither": clean_follower_confusion[(False, False)],
        },
    }


def cache_sets(configuration: dict[str, Any], name: str) -> int:
    cache = configuration[name]
    denominator = int(cache["associativity"]) * int(cache["line_size"])
    sets = int(cache["size_bytes"]) // denominator
    if sets <= 0 or sets & (sets - 1):
        raise ValueError(f"{name}: set count must be a positive power of two")
    return sets


def native_commits(trace_dir: Path) -> tuple[
    dict[int, dict[tuple[int, int], dict[str, Any]]], list[Path], Counter[str]
]:
    by_core: dict[int, dict[tuple[int, int], dict[str, Any]]] = {}
    paths = sorted((trace_dir / "oracle").glob("native-response-core*.jsonl"))
    exclusions: Counter[str] = Counter()
    for path in paths:
        match = re.search(r"core([0-9]+)$", path.stem)
        if match is None:
            raise ValueError(f"cannot parse core from {path}")
        core = int(match.group(1))
        commits: dict[tuple[int, int], dict[str, Any]] = {}
        pending: dict[tuple[int, int], dict[str, Any]] = {}

        def resolved(row: dict[str, Any]) -> bool:
            return (
                int(row.get("native_response_count", 0)) ==
                    int(row.get("native_admission_count", 0)) and
                (bool(row.get("native_issuance_closed", False)) or
                 bool(row.get("native_terminal_no_ruby", False)))
            )

        def retain(key: tuple[int, int], row: dict[str, Any]) -> None:
            if int(row.get("native_admission_count", 0)) == 0:
                exclusions["no_native_admission"] += 1
                return
            if int(row.get("native_response_count", 0)) == 0:
                exclusions["no_native_response"] += 1
                return
            admission = int(row.get("native_first_admission_tick", 0))
            callback = int(row.get("native_last_response_tick", 0))
            if admission == 0 or callback < admission:
                exclusions["invalid_native_interval"] += 1
                return
            if not bool(row.get("native_issuance_closed", False)):
                exclusions["issuance_not_closed"] += 1
                return
            if key in commits:
                raise ValueError(f"{path}: duplicate final lifecycle {key}")
            commits[key] = row

        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                row = json.loads(line)
                record = row.get("record")
                if record not in {"commit", "resolution"}:
                    continue
                key = (int(row["thread_id"]), int(row["inst_seq_num"]))
                if record == "commit":
                    if int(row.get("line_requests", 0)) == 0:
                        continue
                    if key in pending or key in commits:
                        raise ValueError(f"{path}:{line_number}: duplicate commit {key}")
                    if resolved(row):
                        retain(key, row)
                    else:
                        pending[key] = row
                    continue
                commit = pending.pop(key, None)
                if commit is None:
                    raise ValueError(f"{path}:{line_number}: resolution without commit {key}")
                if not resolved(row):
                    exclusions["unresolved_resolution"] += 1
                    continue
                retain(key, row)
        exclusions["unresolved_lifecycle"] += len(pending)
        by_core[core] = commits
    return by_core, paths, exclusions


def trace_records(
    trace_dir: Path, core: int, wanted: set[tuple[int, int]]
) -> tuple[dict[tuple[int, int], dict[str, Any]], Path]:
    matches = sorted(trace_dir.glob(
        f"board.processor.switch{core}.core.tao_trace.tao_trace.records.micro.jsonl"))
    if len(matches) != 1:
        raise ValueError(f"core {core}: expected one trace record file, found {len(matches)}")
    path = matches[0]
    found: dict[tuple[int, int], dict[str, Any]] = {}
    if not wanted:
        return found, path
    wanted_sequences = {sequence for _, sequence in wanted}
    maximum = max(wanted_sequences)
    with path.open("rb") as stream:
        for raw in stream:
            match = SEQ_RE.search(raw)
            if match is None:
                continue
            sequence = int(match.group(1))
            if sequence in wanted_sequences:
                row = json.loads(raw)
                key = (int(row["thread_id"]), sequence)
                if key in wanted:
                    found[key] = row
                    if len(found) == len(wanted):
                        break
            elif sequence > maximum and found:
                break
    return found, path


def build_events(trace_dir: Path, stats_path: Path) -> tuple[list[Event], dict[str, Any]]:
    configuration = json.loads(stats_path.read_text(encoding="utf-8"))["configuration"]
    line_size = int(configuration["l1d"]["line_size"])
    if int(configuration["l2"]["line_size"]) != line_size or \
            int(configuration["llc"]["line_size"]) != line_size:
        raise ValueError("cache line sizes differ")
    l1_sets = cache_sets(configuration, "l1d")
    l2_sets = cache_sets(configuration, "l2")
    llc_sets = cache_sets(configuration, "llc")
    commits_by_core, native_paths, exclusions = native_commits(trace_dir)
    events: list[Event] = []
    trace_paths: list[Path] = []
    missing_trace_records = 0
    for core, commits in sorted(commits_by_core.items()):
        records, path = trace_records(trace_dir, core, set(commits))
        trace_paths.append(path)
        missing_trace_records += len(commits) - len(records)
        for key, native in commits.items():
            record = records.get(key)
            if record is None:
                continue
            _, sequence = key
            physical = int(record.get("cacheline_paddr", 0))
            if physical == 0 or physical % line_size:
                exclusions["invalid_physical_line"] += 1
                continue
            line = physical // line_size
            atomic = bool(record.get("is_atomic"))
            store = bool(record.get("is_store"))
            load = bool(record.get("is_load"))
            if atomic:
                kind = "atomic"
            elif store:
                kind = "store"
            elif load:
                kind = "load"
            else:
                kind = "other"
            hierarchy = native.get("native_hierarchy", {})
            llc = hierarchy.get("llc", {})
            shared_touch = any(int(llc.get(key, 0)) for key in (
                "accesses", "tag_misses", "permission_upgrades",
                "merged_misses", "remote_supplies")) or any(
                    int(hierarchy.get(key, 0)) for key in (
                        "unique_fills", "ruby_memory_fetches",
                        "memory_read_transactions"))
            event = Event(
                core=core,
                sequence=sequence,
                line=line,
                admission=int(native["native_first_admission_tick"]),
                callback=int(native["native_last_response_tick"]),
                kind=kind,
                native_coalesced=int(native.get("native_coalesced", 0)) > 0,
                shared_touch=shared_touch,
                l1_set=line & (l1_sets - 1),
                l2_set=line & (l2_sets - 1),
                llc_set=line & (llc_sets - 1),
                scope=str(native.get("scope", "unknown")),
            )
            if kind == "store":
                event.reasons.add("unsupported_store")
            elif kind == "atomic":
                event.reasons.add("unsupported_atomic")
            elif kind != "load":
                event.reasons.add("unsupported_kind")
            if int(native.get("line_requests", 0)) != 1:
                event.reasons.add("multi_line_request")
            if not bool(record.get("dtlb_hit", 0)) or int(record.get("d_walker_levels", 0)) > 0:
                event.reasons.add("page_walk_dependency")
            events.append(event)
    provenance_paths = native_paths + trace_paths + [stats_path]
    metadata = {
        "native_candidate_commits": sum(len(value) for value in commits_by_core.values()),
        "missing_trace_records": missing_trace_records,
        "input_exclusions": dict(sorted(exclusions.items())),
        "cache_sets": {"l1d": l1_sets, "l2": l2_sets, "llc": llc_sets},
        "inputs": [
            {"path": str(path), "sha256": sha256(path)}
            for path in provenance_paths
        ],
    }
    return events, metadata


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--stats", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    events, metadata = build_events(args.trace_dir, args.stats)
    result = {
        "schema": SCHEMA,
        "case": args.case,
        "scope": "observed native data-request intervals",
        "certification_status": "upper-bound-only",
        "limitations": [
            "instruction-fetch requests are absent from the native data-response ledger",
            "uncommitted or wrong-path request parents are absent; follower mismatches fail closed",
            "active generations preceding the observed ledger are unavailable",
            "clean components certify event closure only; they do not predict CPI contribution",
        ],
        "metadata": metadata,
        "analysis": analyze_events(events),
        "population": {
            "loads": sum(event.kind == "load" for event in events),
            "stores": sum(event.kind == "store" for event in events),
            "atomics": sum(event.kind == "atomic" for event in events),
            "other": sum(event.kind == "other" for event in events),
            "user": sum(event.scope == "user" for event in events),
            "non_user": sum(event.scope != "user" for event in events),
            "by_scope": dict(sorted(Counter(event.scope for event in events).items())),
            "native_coalesced": sum(event.native_coalesced for event in events),
            "load_native_coalesced": sum(
                event.native_coalesced for event in events if event.kind == "load"),
            "shared_touch": sum(event.shared_touch for event in events),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
