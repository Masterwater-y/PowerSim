#!/usr/bin/env python3
"""Validate and summarize the oracle-only Ruby response sideband.

The sideband joins committed TaoTrace memory UOPs to facts observed by gem5's
Ruby Sequencer.  It is diagnostic until controller-specific event mappings and
the target-stop drain contract are closed; this tool therefore never emits
APE/WAPE or marks the cases formally comparable.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import os
from pathlib import Path
from typing import Any, Iterable

from validate_kernel_events_oracle import (
    ACTIVE_KERNEL_PMU_CLASSES,
    validate_document,
)


SCHEMA = "fastsim-p1-native-response-sideband-audit-v1"
SIDEBAND_SCHEMA_V1 = "taotrace-native-response-v1"
SIDEBAND_SCHEMA_V2 = "taotrace-native-response-v2"
SIDEBAND_SCHEMA_V3 = "taotrace-native-response-v3"
SIDEBAND_SCHEMA_V4 = "taotrace-native-response-v4"
SIDEBAND_SCHEMA_V5 = "taotrace-native-response-v5"
SIDEBAND_SCHEMA_V6 = "taotrace-native-response-v6"
NATIVE_SUMMARY_SCHEMA_V1 = "taotrace-native-summary-v1"
SCOPES = {
    "user",
    "syscall",
    "page_fault",
    "irq",
    "scheduler",
    "idle",
    "unknown_kernel",
}
PATH_NAMES = {
    0: "proxy_l1",
    1: "proxy_l2",
    2: "proxy_llc",
    3: "proxy_remote",
    4: "proxy_dram",
}
HIERARCHY_LEVELS = ("l1d", "l2", "llc")
HIERARCHY_OUTCOMES = (
    "hits",
    "tag_misses",
    "permission_upgrades",
    "merged_misses",
    "remote_supplies",
)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def nonnegative(row: dict[str, Any], field: str, where: str) -> int:
    value = row.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{where}.{field} must be a nonnegative integer")
    return value


def event_key(row: dict[str, Any], where: str) -> tuple[int, int, int]:
    return (
        nonnegative(row, "core_id", where),
        nonnegative(row, "thread_id", where),
        nonnegative(row, "inst_seq_num", where),
    )


def validate_native_fact(row: dict[str, Any], where: str) -> None:
    responses = nonnegative(row, "native_response_count", where)
    hits = nonnegative(row, "native_external_hits", where)
    misses = nonnegative(row, "native_external_misses", where)
    coalesced = nonnegative(row, "native_coalesced", where)
    nonnegative(row, "native_responder_machine_mask", where)
    nonnegative(row, "native_responder_machine_unknown", where)
    if responses != hits + misses:
        raise ValueError(
            f"{where}: response count {responses} != hits+misses {hits + misses}"
        )
    if coalesced > responses:
        raise ValueError(f"{where}: coalesced {coalesced} > responses {responses}")


def empty_hierarchy() -> dict[str, Any]:
    result: dict[str, Any] = {
        level: {"accesses": 0, **{field: 0 for field in HIERARCHY_OUTCOMES}}
        for level in HIERARCHY_LEVELS
    }
    result["unique_fills"] = 0
    result["ruby_memory_fetches"] = 0
    result["memory_read_transactions"] = 0
    return result


def validate_hierarchy(row: dict[str, Any], where: str) -> dict[str, Any]:
    hierarchy = row.get("native_hierarchy")
    if not isinstance(hierarchy, dict):
        raise ValueError(f"{where}.native_hierarchy must be an object")
    normalized = empty_hierarchy()
    for level in HIERARCHY_LEVELS:
        values = hierarchy.get(level)
        if not isinstance(values, dict):
            raise ValueError(f"{where}.native_hierarchy.{level} must be an object")
        accesses = nonnegative(values, "accesses", f"{where}.native_hierarchy.{level}")
        outcome_total = 0
        for field in HIERARCHY_OUTCOMES:
            value = nonnegative(
                values, field, f"{where}.native_hierarchy.{level}"
            )
            normalized[level][field] = value
            outcome_total += value
        normalized[level]["accesses"] = accesses
        if accesses != outcome_total:
            raise ValueError(
                f"{where}.native_hierarchy.{level}: accesses={accesses}, "
                f"outcomes={outcome_total}"
            )
    for field in ("unique_fills", "ruby_memory_fetches"):
        normalized[field] = nonnegative(
            hierarchy, field, f"{where}.native_hierarchy"
        )
    if "memory_read_transactions" in hierarchy:
        normalized["memory_read_transactions"] = nonnegative(
            hierarchy, "memory_read_transactions", f"{where}.native_hierarchy"
        )
    if row["native_response_count"] == 0:
        population = sum(
            int(normalized[level]["accesses"]) for level in HIERARCHY_LEVELS
        ) + sum(
            int(normalized[field])
            for field in (
                "unique_fills",
                "ruby_memory_fetches",
                "memory_read_transactions",
            )
        )
        if population != 0:
            raise ValueError(f"{where}: no-Ruby terminal has hierarchy events")
    return normalized


def add_hierarchy(target: dict[str, Any], source: dict[str, Any]) -> None:
    for level in HIERARCHY_LEVELS:
        for field in ("accesses", *HIERARCHY_OUTCOMES):
            target[level][field] += int(source[level][field])
    target["unique_fills"] += int(source["unique_fills"])
    target["ruby_memory_fetches"] += int(source["ruby_memory_fetches"])
    target["memory_read_transactions"] += int(source["memory_read_transactions"])


def empty_native_population() -> dict[str, Any]:
    return {
        "memory_uops": 0,
        "architectural_line_requests": 0,
        "ruby_admission_fragments": 0,
        "ruby_hierarchy_request_fragments": 0,
        "ruby_response_fragments": 0,
        "sequencer_coalesced_fragments": 0,
        "no_ruby_uops": 0,
        "hierarchy_complete_uops": 0,
        "hierarchy_incomplete_uops": 0,
        "hierarchy": empty_hierarchy(),
    }


NATIVE_POPULATION_SCALAR_FIELDS = (
    "memory_uops",
    "architectural_line_requests",
    "ruby_admission_fragments",
    "ruby_hierarchy_request_fragments",
    "ruby_response_fragments",
    "sequencer_coalesced_fragments",
    "no_ruby_uops",
    "hierarchy_complete_uops",
    "hierarchy_incomplete_uops",
)


def add_native_population(
    target: dict[str, Any], source: dict[str, Any]
) -> None:
    for field in NATIVE_POPULATION_SCALAR_FIELDS:
        target[field] += int(source[field])
    add_hierarchy(target["hierarchy"], source["hierarchy"])


def sum_native_scopes(
    populations: dict[str, dict[str, Any]], scopes: Iterable[str]
) -> dict[str, Any]:
    result = empty_native_population()
    for scope in scopes:
        add_native_population(result, populations[scope])
    return result


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {error}") from error
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{line_number}: row must be an object")
        rows.append(row)
    return rows


def iter_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    """Yield JSONL rows without materializing multi-gigabyte sidebands."""
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON: {error}"
                ) from error
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: row must be an object")
            yield line_number, row


def empty_confusion() -> dict[str, dict[str, int]]:
    return {
        name: {"native_hit_fragments": 0, "native_miss_fragments": 0}
        for name in PATH_NAMES.values()
    }


def audit_core_v1(
    path: Path, core_id: int, accounting: dict[str, Any]
) -> dict[str, Any]:
    rows = read_jsonl(path)
    if len(rows) < 2:
        raise ValueError(f"{path}: missing metadata or summary")
    metadata = rows[0]
    if metadata.get("record") != "metadata":
        raise ValueError(f"{path}: first row is not metadata")
    if metadata.get("schema") != SIDEBAND_SCHEMA_V1:
        raise ValueError(f"{path}: unsupported schema {metadata.get('schema')!r}")
    if metadata.get("oracle_only") is not True or metadata.get("fst_input") is not False:
        raise ValueError(f"{path}: sideband must be oracle-only and excluded from FST")
    summary = rows[-1]
    if summary.get("record") != "summary":
        raise ValueError(f"{path}: final row is not summary")

    commits: dict[tuple[int, int, int], dict[str, Any]] = {}
    responses: dict[tuple[int, int, int], dict[str, Any]] = {}
    for index, row in enumerate(rows[1:-1], start=2):
        where = f"{path}:{index}"
        record = row.get("record")
        if record not in {"commit", "response"}:
            raise ValueError(f"{where}: unsupported record {record!r}")
        key = event_key(row, where)
        if key[0] != core_id:
            raise ValueError(f"{where}: core {key[0]} != filename core {core_id}")
        if row.get("scope") not in SCOPES:
            raise ValueError(f"{where}: invalid scope {row.get('scope')!r}")
        source = row.get("attribution_source")
        if source not in {"packet", "fallback"}:
            raise ValueError(f"{where}: invalid attribution source {source!r}")
        path_class = nonnegative(row, "proxy_path_class", where)
        if path_class not in PATH_NAMES:
            raise ValueError(f"{where}: invalid proxy path {path_class}")
        nonnegative(row, "line_requests", where)
        validate_native_fact(row, where)
        target = commits if record == "commit" else responses
        if key in target:
            raise ValueError(f"{where}: duplicate {record} identity {key}")
        target[key] = row

    for key, response in responses.items():
        commit = commits.get(key)
        if commit is None:
            raise ValueError(f"{path}: response without committed identity {key}")
        if commit["attribution_source"] != "fallback":
            raise ValueError(f"{path}: late response for non-fallback identity {key}")
        if commit["native_response_count"] != 0:
            raise ValueError(f"{path}: identity {key} has native facts twice")
        for field in ("scope", "proxy_path_class", "line_requests"):
            if response[field] != commit[field]:
                raise ValueError(f"{path}: response/commit {field} mismatch for {key}")

    packet = sum(row["attribution_source"] == "packet" for row in commits.values())
    fallback = len(commits) - packet
    response_at_commit = sum(
        row["native_response_count"] != 0 for row in commits.values()
    )
    late = len(responses)
    pending = sum(
        row["attribution_source"] == "fallback"
        and row["native_response_count"] == 0
        and key not in responses
        for key, row in commits.items()
    )
    response_without_fact = sum(
        row["attribution_source"] == "packet"
        and row["native_response_count"] == 0
        for row in commits.values()
    ) + sum(row["native_response_count"] == 0 for row in responses.values())

    expected_summary = {
        "committed_memory_uops": len(commits),
        "packet_source_uops": packet,
        "fallback_source_uops": fallback,
        "response_at_commit_uops": response_at_commit,
        "late_response_uops": late,
        "response_without_native_fact_uops": response_without_fact,
        "pending_without_response_uops": pending,
    }
    for field, expected in expected_summary.items():
        actual = nonnegative(summary, field, f"{path}:summary")
        if actual != expected:
            raise ValueError(f"{path}: summary {field}={actual}, expected {expected}")

    expected_accounting = {
        "committed_memory_uops": len(commits),
        "packet_attributed_uops": packet,
        "fallback_attributed_uops": fallback,
    }
    for field, actual in expected_accounting.items():
        expected = int(accounting[field])
        if actual != expected:
            raise ValueError(
                f"{path}: sideband {field}={actual}, P0 ledger={expected}"
            )

    confusion = empty_confusion()
    native_outcome_uops = 0
    native_fragments = 0
    native_hits = 0
    native_misses = 0
    native_coalesced = 0
    machine_unknown = 0
    for key, commit in commits.items():
        fact = commit if commit["native_response_count"] else responses.get(key)
        if fact is None or fact["native_response_count"] == 0:
            continue
        native_outcome_uops += 1
        native_fragments += fact["native_response_count"]
        native_hits += fact["native_external_hits"]
        native_misses += fact["native_external_misses"]
        native_coalesced += fact["native_coalesced"]
        machine_unknown += fact["native_responder_machine_unknown"]
        cell = confusion[PATH_NAMES[int(commit["proxy_path_class"])]]
        cell["native_hit_fragments"] += fact["native_external_hits"]
        cell["native_miss_fragments"] += fact["native_external_misses"]

    return {
        "sideband_schema": SIDEBAND_SCHEMA_V1,
        "core_id": core_id,
        "committed_memory_uops": len(commits),
        "packet_source_uops": packet,
        "fallback_source_uops": fallback,
        "response_at_commit_uops": response_at_commit,
        "terminal_at_commit_uops": 0,
        "late_response_uops": late,
        "late_terminal_uops": 0,
        "pending_without_response_uops": pending,
        "response_without_native_fact_uops": response_without_fact,
        "native_outcome_uops": native_outcome_uops,
        "native_no_ruby_uops": 0,
        "native_admission_fragments": native_fragments,
        "native_hierarchy_request_fragments": 0,
        "native_response_fragments": native_fragments,
        "native_external_hit_fragments": native_hits,
        "native_external_miss_fragments": native_misses,
        "native_coalesced_fragments": native_coalesced,
        "native_responder_machine_unknown": machine_unknown,
        "native_hierarchy_complete_uops": 0,
        "native_hierarchy_incomplete_uops": 0,
        "native_outcome_coverage": (
            native_outcome_uops / len(commits) if commits else 0.0
        ),
        "terminal_reason_counts": {},
        "target_drain_polls": 0,
        "proxy_native_confusion": confusion,
    }


def boolean(row: dict[str, Any], field: str, where: str) -> bool:
    value = row.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{where}.{field} must be boolean")
    return value


def validate_lifecycle(row: dict[str, Any], where: str) -> None:
    admissions = nonnegative(row, "native_admission_count", where)
    aliased = nonnegative(row, "native_aliased_admissions", where)
    responses = nonnegative(row, "native_response_count", where)
    closed = boolean(row, "native_issuance_closed", where)
    terminal = boolean(row, "native_terminal_no_ruby", where)
    nonnegative(row, "native_terminal_reason_mask", where)
    validate_native_fact(row, where)
    if aliased > admissions:
        raise ValueError(f"{where}: aliased admissions exceed admissions")
    if responses > admissions:
        raise ValueError(f"{where}: responses exceed admissions")
    if (closed or terminal) and responses > admissions:
        raise ValueError(f"{where}: resolved lifecycle over-responded")


def lifecycle_resolved(row: dict[str, Any]) -> bool:
    return (
        row["native_response_count"] == row["native_admission_count"]
        and (row["native_issuance_closed"] or row["native_terminal_no_ruby"])
    )


def audit_core_lifecycle(
    path: Path, core_id: int, accounting: dict[str, Any], schema: str
) -> dict[str, Any]:
    hierarchy_schema = schema in {
        SIDEBAND_SCHEMA_V3,
        SIDEBAND_SCHEMA_V4,
        SIDEBAND_SCHEMA_V5,
        SIDEBAND_SCHEMA_V6,
    }
    commits = 0
    packet = 0
    response_at_commit = 0
    terminal_at_commit = 0
    late_response = 0
    late_terminal = 0
    response_without_fact = 0
    admissions = 0
    hierarchy_requests = 0
    responses = 0
    native_uops = 0
    no_ruby_uops = 0
    native_hits = 0
    native_misses = 0
    native_coalesced = 0
    machine_unknown = 0
    hierarchy_complete_uops = 0
    hierarchy_incomplete_uops = 0
    hierarchy_mismatch_examples: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    confusion = empty_confusion()
    native_by_scope = {scope: empty_native_population() for scope in sorted(SCOPES)}
    pending: dict[tuple[int, int, int], dict[str, Any]] = {}
    last_commit_seq: dict[tuple[int, int], int] = {}
    metadata: dict[str, Any] | None = None
    summary: dict[str, Any] | None = None

    def aggregate_final(commit: dict[str, Any], fact: dict[str, Any], where: str) -> None:
        nonlocal admissions, hierarchy_requests, responses
        nonlocal native_uops, no_ruby_uops
        nonlocal native_hits, native_misses, native_coalesced, machine_unknown
        nonlocal hierarchy_complete_uops, hierarchy_incomplete_uops
        admissions += int(fact["native_admission_count"])
        responses += int(fact["native_response_count"])
        native_uops += int(fact["native_response_count"] > 0)
        no_ruby_uops += int(fact["native_response_count"] == 0)
        native_hits += int(fact["native_external_hits"])
        native_misses += int(fact["native_external_misses"])
        native_coalesced += int(fact["native_coalesced"])
        machine_unknown += int(fact["native_responder_machine_unknown"])
        mask = int(fact["native_terminal_reason_mask"])
        for bit, name in {
            0: "predicated_off", 1: "no_request", 2: "store_forward",
            3: "local_access", 4: "failed_store_conditional",
            5: "zero_size", 6: "prefetch_skipped",
        }.items():
            if mask & (1 << bit):
                reason_counts[name] = reason_counts.get(name, 0) + 1
        cell = confusion[PATH_NAMES[int(commit["proxy_path_class"])]]
        cell["native_hit_fragments"] += int(fact["native_external_hits"])
        cell["native_miss_fragments"] += int(fact["native_external_misses"])
        if not hierarchy_schema:
            return
        hierarchy = validate_hierarchy(fact, where)
        primary_admissions = (
            int(fact["native_hierarchy_request_count"])
            if schema in {SIDEBAND_SCHEMA_V5, SIDEBAND_SCHEMA_V6}
            else int(fact["native_admission_count"])
            - int(fact["native_aliased_admissions"])
        )
        hierarchy_requests += primary_admissions
        hierarchy_complete = (
            fact["native_response_count"] == 0
            or int(hierarchy["l1d"]["accesses"]) == primary_admissions
        )
        hierarchy_complete_uops += int(hierarchy_complete)
        hierarchy_incomplete_uops += int(not hierarchy_complete)
        if not hierarchy_complete and len(hierarchy_mismatch_examples) < 32:
            hierarchy_mismatch_examples.append(
                {
                    "core_id": core_id,
                    "thread_id": int(commit["thread_id"]),
                    "inst_seq_num": int(commit["inst_seq_num"]),
                    "scope": str(commit["scope"]),
                    "proxy_path_class": int(commit["proxy_path_class"]),
                    "native_admission_count": int(
                        fact["native_admission_count"]
                    ),
                    "native_aliased_admissions": int(
                        fact["native_aliased_admissions"]
                    ),
                    "native_hierarchy_request_count": primary_admissions,
                    "native_response_count": int(
                        fact["native_response_count"]
                    ),
                    "l1d_accesses": int(hierarchy["l1d"]["accesses"]),
                    "source": where,
                }
            )
        population = native_by_scope[str(commit["scope"])]
        population["memory_uops"] += 1
        population["architectural_line_requests"] += int(commit["line_requests"])
        population["ruby_admission_fragments"] += int(
            fact["native_admission_count"]
        )
        population["ruby_hierarchy_request_fragments"] += primary_admissions
        population["ruby_response_fragments"] += int(
            fact["native_response_count"]
        )
        population["sequencer_coalesced_fragments"] += int(
            fact["native_coalesced"]
        )
        population["no_ruby_uops"] += int(fact["native_response_count"] == 0)
        population["hierarchy_complete_uops"] += int(hierarchy_complete)
        population["hierarchy_incomplete_uops"] += int(not hierarchy_complete)
        add_hierarchy(population["hierarchy"], hierarchy)

    for line_number, row in iter_jsonl(path):
        where = f"{path}:{line_number}"
        record = row.get("record")
        if metadata is None:
            metadata = row
            if record != "metadata" or row.get("schema") != schema:
                raise ValueError(f"{path}: invalid {schema} metadata")
            if row.get("oracle_only") is not True or row.get("fst_input") is not False:
                raise ValueError(
                    f"{path}: sideband must be oracle-only and excluded from FST"
                )
            if row.get("target_stop") != "committed-native-drain":
                raise ValueError(f"{path}: target stop is not committed-native-drain")
            if hierarchy_schema:
                if row.get("hierarchy_source") != "ruby-slicc-controller-actions":
                    raise ValueError(
                        f"{path}: hierarchy is not sourced from Ruby SLICC"
                    )
                if row.get("ruby_memory_fetch_semantics") != (
                    "l2-to-directory-fetch-not-dram-transaction"
                ):
                    raise ValueError(f"{path}: ambiguous Ruby memory-fetch semantics")
            if schema in {
                SIDEBAND_SCHEMA_V4,
                SIDEBAND_SCHEMA_V5,
                SIDEBAND_SCHEMA_V6,
            } and row.get(
                "memory_read_transaction_semantics"
            ) != "accepted-ruby-memory-port-read-packet":
                raise ValueError(
                    f"{path}: ambiguous memory-read transaction semantics"
                )
            if schema in {
                SIDEBAND_SCHEMA_V4,
                SIDEBAND_SCHEMA_V5,
                SIDEBAND_SCHEMA_V6,
            } and row.get(
                "hierarchy_identity_transport"
            ) != "context-id-inst-seq-num-no-request-retention":
                raise ValueError(f"{path}: unsafe hierarchy identity transport")
            if schema in {SIDEBAND_SCHEMA_V5, SIDEBAND_SCHEMA_V6} and row.get(
                "hierarchy_request_semantics"
            ) != "sequencer-mandatory-queue-enqueue":
                raise ValueError(f"{path}: ambiguous hierarchy request semantics")
            if schema == SIDEBAND_SCHEMA_V6 and row.get(
                "measurement_boundary_semantics"
            ) != "preboundary-inflight-ledger-retire-cleanup":
                raise ValueError(
                    f"{path}: ambiguous measurement-boundary semantics"
                )
            continue
        if summary is not None:
            raise ValueError(f"{where}: row follows summary")
        if record == "summary":
            summary = row
            continue
        if record not in {"commit", "resolution"}:
            raise ValueError(f"{where}: unsupported record {record!r}")
        key = event_key(row, where)
        if key[0] != core_id:
            raise ValueError(f"{where}: core {key[0]} != filename core {core_id}")
        if row.get("scope") not in SCOPES:
            raise ValueError(f"{where}: invalid scope {row.get('scope')!r}")
        if row.get("attribution_source") not in {"packet", "fallback"}:
            raise ValueError(f"{where}: invalid attribution source")
        path_class = nonnegative(row, "proxy_path_class", where)
        if path_class not in PATH_NAMES:
            raise ValueError(f"{where}: invalid proxy path {path_class}")
        nonnegative(row, "line_requests", where)
        validate_lifecycle(row, where)
        if hierarchy_schema:
            validate_hierarchy(row, where)
            if schema in {
                SIDEBAND_SCHEMA_V4,
                SIDEBAND_SCHEMA_V5,
                SIDEBAND_SCHEMA_V6,
            } and (
                "memory_read_transactions" not in row["native_hierarchy"]
            ):
                raise ValueError(f"{where}: sideband lacks memory-read transactions")
        if schema in {SIDEBAND_SCHEMA_V5, SIDEBAND_SCHEMA_V6}:
            nonnegative(row, "native_hierarchy_request_count", where)
        response_without_fact += int(
            row["native_response_count"]
            != row["native_external_hits"] + row["native_external_misses"]
        )
        if record == "commit":
            thread_key = (key[0], key[1])
            previous = last_commit_seq.get(thread_key)
            if previous is not None and key[2] <= previous:
                raise ValueError(
                    f"{where}: non-increasing or duplicate commit identity {key}"
                )
            last_commit_seq[thread_key] = key[2]
            commits += 1
            packet += int(row["attribution_source"] == "packet")
            if lifecycle_resolved(row):
                if row["native_response_count"] > 0:
                    response_at_commit += 1
                else:
                    terminal_at_commit += 1
                aggregate_final(row, row, f"{where}:final")
            else:
                pending[key] = row
            continue
        commit = pending.pop(key, None)
        if commit is None:
            raise ValueError(f"{where}: resolution without unresolved commit {key}")
        if not lifecycle_resolved(row):
            raise ValueError(f"{where}: resolution {key} is not terminal")
        expected_kind = (
            "ruby_response" if row["native_response_count"] else "no_ruby_terminal"
        )
        if row.get("resolution_kind") != expected_kind:
            raise ValueError(f"{where}: resolution kind mismatch for {key}")
        for field in ("scope", "attribution_source", "proxy_path_class", "line_requests"):
            if row[field] != commit[field]:
                raise ValueError(
                    f"{where}: resolution/commit {field} mismatch for {key}"
                )
        if row["native_response_count"] > 0:
            late_response += 1
        else:
            late_terminal += 1
        aggregate_final(commit, row, f"{where}:final")

    if metadata is None or summary is None:
        raise ValueError(f"{path}: missing metadata or summary")
    fallback = commits - packet
    unresolved_keys = set(pending)
    expected_summary = {
        "committed_memory_uops": commits,
        "packet_source_uops": packet,
        "fallback_source_uops": fallback,
        "response_at_commit_uops": response_at_commit,
        "terminal_at_commit_uops": terminal_at_commit,
        "late_response_uops": late_response,
        "late_terminal_uops": late_terminal,
        "response_without_native_fact_uops": response_without_fact,
        "unresolved_lifecycle_uops": len(unresolved_keys),
    }
    for field, expected in expected_summary.items():
        actual = nonnegative(summary, field, f"{path}:summary")
        if actual != expected:
            raise ValueError(f"{path}: summary {field}={actual}, expected {expected}")

    for field, actual in {
        "committed_memory_uops": commits,
        "packet_attributed_uops": packet,
        "fallback_attributed_uops": fallback,
    }.items():
        expected = int(accounting[field])
        if actual != expected:
            raise ValueError(f"{path}: sideband {field}={actual}, P0 ledger={expected}")

    return {
        "sideband_schema": schema,
        "core_id": core_id,
        "committed_memory_uops": commits,
        "packet_source_uops": packet,
        "fallback_source_uops": fallback,
        "response_at_commit_uops": response_at_commit,
        "terminal_at_commit_uops": terminal_at_commit,
        "late_response_uops": late_response,
        "late_terminal_uops": late_terminal,
        "pending_without_response_uops": len(unresolved_keys),
        "response_without_native_fact_uops": response_without_fact,
        "native_outcome_uops": native_uops,
        "native_no_ruby_uops": no_ruby_uops,
        "native_admission_fragments": admissions,
        "native_hierarchy_request_fragments": hierarchy_requests,
        "native_response_fragments": responses,
        "native_external_hit_fragments": native_hits,
        "native_external_miss_fragments": native_misses,
        "native_coalesced_fragments": native_coalesced,
        "native_responder_machine_unknown": machine_unknown,
        "native_hierarchy_complete_uops": hierarchy_complete_uops,
        "native_hierarchy_incomplete_uops": hierarchy_incomplete_uops,
        "native_hierarchy_mismatch_examples": hierarchy_mismatch_examples,
        "native_outcome_coverage": native_uops / commits if commits else 0.0,
        "terminal_reason_counts": reason_counts,
        "target_drain_polls": nonnegative(summary, "target_drain_polls", f"{path}:summary"),
        "proxy_native_confusion": confusion,
        "native_ruby_pmu_by_scope": native_by_scope if hierarchy_schema else {},
    }


def audit_core(path: Path, core_id: int, accounting: dict[str, Any]) -> dict[str, Any]:
    first = next(iter(iter_jsonl(path)), None)
    if first is None:
        raise ValueError(f"{path}: empty sideband")
    schema = first[1].get("schema")
    if schema == SIDEBAND_SCHEMA_V1:
        return audit_core_v1(path, core_id, accounting)
    if schema == SIDEBAND_SCHEMA_V2:
        return audit_core_lifecycle(path, core_id, accounting, schema)
    if schema == SIDEBAND_SCHEMA_V3:
        return audit_core_lifecycle(path, core_id, accounting, schema)
    if schema == SIDEBAND_SCHEMA_V4:
        return audit_core_lifecycle(path, core_id, accounting, schema)
    if schema == SIDEBAND_SCHEMA_V5:
        return audit_core_lifecycle(path, core_id, accounting, schema)
    if schema == SIDEBAND_SCHEMA_V6:
        return audit_core_lifecycle(path, core_id, accounting, schema)
    raise ValueError(f"{path}: unsupported schema {schema!r}")


def validate_summary_hierarchy(
    hierarchy: Any, where: str
) -> dict[str, Any]:
    if not isinstance(hierarchy, dict):
        raise ValueError(f"{where} must be an object")
    normalized = empty_hierarchy()
    for level in HIERARCHY_LEVELS:
        values = hierarchy.get(level)
        if not isinstance(values, dict):
            raise ValueError(f"{where}.{level} must be an object")
        accesses = nonnegative(values, "accesses", f"{where}.{level}")
        outcomes = 0
        for field in HIERARCHY_OUTCOMES:
            value = nonnegative(values, field, f"{where}.{level}")
            normalized[level][field] = value
            outcomes += value
        normalized[level]["accesses"] = accesses
        if accesses != outcomes:
            raise ValueError(
                f"{where}.{level}: accesses={accesses}, outcomes={outcomes}"
            )
    for field in (
        "unique_fills",
        "ruby_memory_fetches",
        "memory_read_transactions",
    ):
        normalized[field] = nonnegative(hierarchy, field, where)
    return normalized


def validate_summary_population(row: Any, where: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"{where} must be an object")
    normalized = empty_native_population()
    for field in NATIVE_POPULATION_SCALAR_FIELDS:
        normalized[field] = nonnegative(row, field, where)
    aliased = nonnegative(row, "ruby_aliased_admission_fragments", where)
    if aliased > normalized["ruby_admission_fragments"]:
        raise ValueError(f"{where}: aliased admissions exceed admissions")
    normalized["hierarchy"] = validate_summary_hierarchy(
        row.get("hierarchy"), f"{where}.hierarchy"
    )
    if (
        normalized["hierarchy_complete_uops"]
        + normalized["hierarchy_incomplete_uops"]
        != normalized["memory_uops"]
    ):
        raise ValueError(f"{where}: hierarchy completion does not conserve UOPs")
    return normalized


def audit_core_summary(
    path: Path, core_id: int, accounting: dict[str, Any]
) -> dict[str, Any]:
    row = load(path)
    where = str(path)
    if not isinstance(row, dict):
        raise ValueError(f"{path}: native summary must be an object")
    if row.get("schema") != NATIVE_SUMMARY_SCHEMA_V1:
        raise ValueError(f"{path}: unsupported summary schema")
    if row.get("record") != "summary":
        raise ValueError(f"{path}: native summary record is not summary")
    if row.get("oracle_only") is not True or row.get("fst_input") is not False:
        raise ValueError(f"{path}: native summary must be oracle-only")
    if nonnegative(row, "core_id", where) != core_id:
        raise ValueError(f"{path}: core ID does not match filename")
    expected_contract = {
        "source_sideband_schema": SIDEBAND_SCHEMA_V6,
        "lifecycle_join": "context-id-inst-seq-num",
        "target_stop": "committed-native-drain",
        "hierarchy_source": "ruby-slicc-controller-actions",
        "hierarchy_identity_transport": (
            "context-id-inst-seq-num-no-request-retention"
        ),
        "hierarchy_request_semantics": "sequencer-mandatory-queue-enqueue",
        "measurement_boundary_semantics": (
            "preboundary-inflight-ledger-retire-cleanup"
        ),
        "ruby_memory_fetch_semantics": (
            "l2-to-directory-fetch-not-dram-transaction"
        ),
        "memory_read_transaction_semantics": (
            "accepted-ruby-memory-port-read-packet"
        ),
    }
    for field, expected in expected_contract.items():
        if row.get(field) != expected:
            raise ValueError(
                f"{path}: {field}={row.get(field)!r}, expected {expected!r}"
            )
    boolean(row, "full_jsonl_enabled", where)

    scalar_fields = (
        "committed_memory_uops",
        "packet_source_uops",
        "fallback_source_uops",
        "response_at_commit_uops",
        "terminal_at_commit_uops",
        "late_response_uops",
        "late_terminal_uops",
        "pending_without_response_uops",
        "response_without_native_fact_uops",
        "native_outcome_uops",
        "native_no_ruby_uops",
        "native_admission_fragments",
        "native_hierarchy_request_fragments",
        "native_response_fragments",
        "native_external_hit_fragments",
        "native_external_miss_fragments",
        "native_coalesced_fragments",
        "native_responder_machine_unknown",
        "native_hierarchy_complete_uops",
        "native_hierarchy_incomplete_uops",
        "target_drain_polls",
    )
    values = {field: nonnegative(row, field, where) for field in scalar_fields}
    committed = values["committed_memory_uops"]
    if values["packet_source_uops"] + values["fallback_source_uops"] != committed:
        raise ValueError(f"{path}: packet/fallback sources do not conserve UOPs")
    if (
        values["response_at_commit_uops"]
        + values["terminal_at_commit_uops"]
        + values["late_response_uops"]
        + values["late_terminal_uops"]
        + values["pending_without_response_uops"]
        != committed
    ):
        raise ValueError(f"{path}: lifecycle resolution does not conserve UOPs")
    if values["native_admission_fragments"] != values["native_response_fragments"]:
        raise ValueError(f"{path}: Ruby admission/response fragments differ")
    if (
        values["native_external_hit_fragments"]
        + values["native_external_miss_fragments"]
        != values["native_response_fragments"]
    ):
        raise ValueError(f"{path}: native hit/miss fragments do not conserve")
    if values["native_outcome_uops"] + values["native_no_ruby_uops"] != committed:
        raise ValueError(f"{path}: Ruby/no-Ruby populations do not conserve UOPs")
    if (
        values["native_hierarchy_complete_uops"]
        + values["native_hierarchy_incomplete_uops"]
        != committed
    ):
        raise ValueError(f"{path}: hierarchy-completion populations do not conserve")

    expected_accounting = {
        "committed_memory_uops": committed,
        "packet_attributed_uops": values["packet_source_uops"],
        "fallback_attributed_uops": values["fallback_source_uops"],
    }
    for field, actual in expected_accounting.items():
        if actual != int(accounting[field]):
            raise ValueError(
                f"{path}: native summary {field}={actual}, "
                f"P0 ledger={accounting[field]}"
            )

    populations = row.get("native_ruby_pmu_by_scope")
    if not isinstance(populations, dict) or set(populations) != SCOPES:
        raise ValueError(f"{path}: native summary must contain every exact scope")
    normalized_populations = {
        scope: validate_summary_population(
            populations[scope], f"{path}.native_ruby_pmu_by_scope.{scope}"
        )
        for scope in sorted(SCOPES)
    }
    population_total = sum_native_scopes(normalized_populations, SCOPES)
    population_checks = {
        "memory_uops": committed,
        "ruby_admission_fragments": values["native_admission_fragments"],
        "ruby_hierarchy_request_fragments": values[
            "native_hierarchy_request_fragments"
        ],
        "ruby_response_fragments": values["native_response_fragments"],
        "sequencer_coalesced_fragments": values["native_coalesced_fragments"],
        "no_ruby_uops": values["native_no_ruby_uops"],
        "hierarchy_complete_uops": values["native_hierarchy_complete_uops"],
        "hierarchy_incomplete_uops": values[
            "native_hierarchy_incomplete_uops"
        ],
    }
    for field, expected in population_checks.items():
        if int(population_total[field]) != expected:
            raise ValueError(
                f"{path}: scope population {field}={population_total[field]}, "
                f"expected {expected}"
            )

    confusion = row.get("proxy_native_confusion")
    if not isinstance(confusion, dict) or set(confusion) != set(PATH_NAMES.values()):
        raise ValueError(f"{path}: invalid proxy/native confusion matrix")
    normalized_confusion = empty_confusion()
    for name in normalized_confusion:
        cell = confusion[name]
        if not isinstance(cell, dict):
            raise ValueError(f"{path}: invalid confusion cell {name}")
        for field in ("native_hit_fragments", "native_miss_fragments"):
            normalized_confusion[name][field] = nonnegative(
                cell, field, f"{path}.proxy_native_confusion.{name}"
            )
    if sum(
        cell["native_hit_fragments"] for cell in normalized_confusion.values()
    ) != values["native_external_hit_fragments"]:
        raise ValueError(f"{path}: confusion native hits do not conserve")
    if sum(
        cell["native_miss_fragments"] for cell in normalized_confusion.values()
    ) != values["native_external_miss_fragments"]:
        raise ValueError(f"{path}: confusion native misses do not conserve")

    reasons = row.get("terminal_reason_counts")
    if not isinstance(reasons, dict):
        raise ValueError(f"{path}: terminal_reason_counts must be an object")
    normalized_reasons = {
        str(reason): nonnegative(reasons, reason, f"{path}.terminal_reason_counts")
        for reason in reasons
    }
    anomalies = row.get("anomalies")
    if not isinstance(anomalies, dict):
        raise ValueError(f"{path}: anomalies must be an object")
    anomaly_limit = nonnegative(anomalies, "limit", f"{path}.anomalies")
    retained = nonnegative(anomalies, "retained", f"{path}.anomalies")
    nonnegative(anomalies, "dropped", f"{path}.anomalies")
    samples = anomalies.get("samples")
    if not isinstance(samples, list) or len(samples) != retained:
        raise ValueError(f"{path}: retained anomaly count does not match samples")
    if retained > anomaly_limit:
        raise ValueError(f"{path}: anomaly sample exceeds configured limit")
    mismatch_examples = [
        sample
        for sample in samples
        if isinstance(sample, dict)
        and sample.get("kind") == "hierarchy_request_l1d_mismatch"
    ]

    coverage = values["native_outcome_uops"] / committed if committed else 0.0
    return {
        "sideband_schema": NATIVE_SUMMARY_SCHEMA_V1,
        "core_id": core_id,
        **values,
        "native_hierarchy_mismatch_examples": mismatch_examples,
        "native_outcome_coverage": coverage,
        "terminal_reason_counts": normalized_reasons,
        "proxy_native_confusion": normalized_confusion,
        "native_ruby_pmu_by_scope": normalized_populations,
        "summary_only": not bool(row["full_jsonl_enabled"]),
        "bounded_anomalies": {
            "limit": anomaly_limit,
            "retained": retained,
            "dropped": int(anomalies["dropped"]),
        },
    }


def merge_confusion(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    result = empty_confusion()
    for row in rows:
        for path, values in row["proxy_native_confusion"].items():
            for field, value in values.items():
                result[path][field] += int(value)
    return result


def merge_native_populations(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result = {scope: empty_native_population() for scope in sorted(SCOPES)}
    for row in rows:
        for scope, source in row["native_ruby_pmu_by_scope"].items():
            add_native_population(result[scope], source)
    return result


def audit_result(result: Path) -> dict[str, Any]:
    result = result.resolve()
    document = load(result / "oracle" / "kernel_events.json")
    try:
        validation = validate_document(document, max_unknown_ratio=0.0)
    except ValueError as error:
        raise ValueError(f"native sideband requires formal P0 v3: {error}") from error
    if not validation["formal_pmu_eligible"]:
        raise ValueError("native sideband requires formal P0 v3")
    per_core_oracle = {
        int(row["core_id"]): row for row in document["per_core"]
    }
    summary_paths = sorted((result / "oracle").glob("native-summary-core*.json"))
    paths = summary_paths or sorted(
        (result / "oracle").glob("native-response-core*.jsonl")
    )
    if len(paths) != len(per_core_oracle):
        raise ValueError(
            f"{result}: native oracle files {len(paths)} != "
            f"cores {len(per_core_oracle)}"
        )
    per_core = []
    for path in paths:
        stem = path.stem
        try:
            prefix = (
                "native-summary-core"
                if path.suffix == ".json"
                else "native-response-core"
            )
            core_id = int(stem.removeprefix(prefix))
        except ValueError as error:
            raise ValueError(f"{path}: invalid core suffix") from error
        if core_id not in per_core_oracle:
            raise ValueError(f"{path}: unexpected core {core_id}")
        accounting = per_core_oracle[core_id]["memory_accounting"]
        per_core.append(
            audit_core_summary(path, core_id, accounting)
            if path.suffix == ".json"
            else audit_core(path, core_id, accounting)
        )
    per_core.sort(key=lambda row: row["core_id"])

    sum_fields = (
        "committed_memory_uops",
        "packet_source_uops",
        "fallback_source_uops",
        "response_at_commit_uops",
        "terminal_at_commit_uops",
        "late_response_uops",
        "late_terminal_uops",
        "pending_without_response_uops",
        "response_without_native_fact_uops",
        "native_outcome_uops",
        "native_no_ruby_uops",
        "native_admission_fragments",
        "native_hierarchy_request_fragments",
        "native_response_fragments",
        "native_external_hit_fragments",
        "native_external_miss_fragments",
        "native_coalesced_fragments",
        "native_responder_machine_unknown",
        "native_hierarchy_complete_uops",
        "native_hierarchy_incomplete_uops",
    )
    aggregate = {
        field: sum(int(row[field]) for row in per_core) for field in sum_fields
    }
    committed = aggregate["committed_memory_uops"]
    aggregate["native_outcome_coverage"] = (
        aggregate["native_outcome_uops"] / committed if committed else 0.0
    )
    aggregate["proxy_native_confusion"] = merge_confusion(per_core)
    aggregate["terminal_reason_counts"] = {}
    for row in per_core:
        for reason, count in row["terminal_reason_counts"].items():
            aggregate["terminal_reason_counts"][reason] = (
                aggregate["terminal_reason_counts"].get(reason, 0) + int(count)
            )
    aggregate["target_drain_polls_max"] = max(
        (int(row["target_drain_polls"]) for row in per_core), default=0
    )
    aggregate["native_hierarchy_mismatch_examples"] = [
        example
        for row in per_core
        for example in row.get("native_hierarchy_mismatch_examples", [])
    ][:64]
    schemas = {row["sideband_schema"] for row in per_core}
    if len(schemas) != 1:
        raise ValueError(f"{result}: mixed sideband schemas {sorted(schemas)}")
    sideband_schema = next(iter(schemas))
    lifecycle_schema = sideband_schema in {
        SIDEBAND_SCHEMA_V2,
        SIDEBAND_SCHEMA_V3,
        SIDEBAND_SCHEMA_V4,
        SIDEBAND_SCHEMA_V5,
        SIDEBAND_SCHEMA_V6,
        NATIVE_SUMMARY_SCHEMA_V1,
    }
    structural_conservation = True
    if lifecycle_schema:
        structural_conservation = (
            aggregate["pending_without_response_uops"] == 0
            and aggregate["native_admission_fragments"]
            == aggregate["native_response_fragments"]
            and aggregate["native_outcome_uops"]
            + aggregate["native_no_ruby_uops"]
            == aggregate["committed_memory_uops"]
        )
    native_ruby_pmu_by_scope = {}
    if sideband_schema in {
        SIDEBAND_SCHEMA_V3,
        SIDEBAND_SCHEMA_V4,
        SIDEBAND_SCHEMA_V5,
        SIDEBAND_SCHEMA_V6,
        NATIVE_SUMMARY_SCHEMA_V1,
    }:
        native_ruby_pmu_by_scope = merge_native_populations(per_core)
        population_uops = sum(
            int(row["memory_uops"])
            for row in native_ruby_pmu_by_scope.values()
        )
        population_admissions = sum(
            int(row["ruby_admission_fragments"])
            for row in native_ruby_pmu_by_scope.values()
        )
        population_hierarchy_requests = sum(
            int(row["ruby_hierarchy_request_fragments"])
            for row in native_ruby_pmu_by_scope.values()
        )
        population_responses = sum(
            int(row["ruby_response_fragments"])
            for row in native_ruby_pmu_by_scope.values()
        )
        structural_conservation = structural_conservation and (
            population_uops == aggregate["committed_memory_uops"]
            and population_admissions == aggregate["native_admission_fragments"]
            and population_hierarchy_requests
            == aggregate["native_hierarchy_request_fragments"]
            and population_responses == aggregate["native_response_fragments"]
        )
    structural_conservation_without_hierarchy_completion = structural_conservation
    if sideband_schema in {
        SIDEBAND_SCHEMA_V5,
        SIDEBAND_SCHEMA_V6,
        NATIVE_SUMMARY_SCHEMA_V1,
    }:
        structural_conservation = structural_conservation and (
            aggregate["native_hierarchy_incomplete_uops"] == 0
        )
    hierarchy_requests = aggregate["native_hierarchy_request_fragments"]
    hierarchy_incomplete = aggregate["native_hierarchy_incomplete_uops"]
    hierarchy_gap_ratio = (
        hierarchy_incomplete / hierarchy_requests
        if hierarchy_requests
        else (0.0 if hierarchy_incomplete == 0 else None)
    )
    native_scope_metrics = {}
    if native_ruby_pmu_by_scope:
        native_scope_metrics = {
            "user": native_ruby_pmu_by_scope["user"],
            "user_plus_kernel": sum_native_scopes(
                native_ruby_pmu_by_scope,
                ("user", *ACTIVE_KERNEL_PMU_CLASSES),
            ),
        }
    oracle_aggregate = document["aggregate"]
    scope_metrics = {
        "user": {
            "cycles_per_user_uop": oracle_aggregate[
                "cycles_per_user_uop_user"
            ],
            "perf_like_cpi": oracle_aggregate["perf_like_cpi_user"],
            "p0_pmu": oracle_aggregate["pmu_user"],
            "native_ruby_pmu": native_scope_metrics.get("user", {}),
        },
        "user_plus_kernel": {
            "cycles_per_user_uop": oracle_aggregate[
                "cycles_per_user_uop_user_plus_kernel"
            ],
            "perf_like_cpi": oracle_aggregate[
                "perf_like_cpi_user_plus_kernel"
            ],
            "p0_pmu": oracle_aggregate["pmu_user_plus_kernel"],
            "native_ruby_pmu": native_scope_metrics.get(
                "user_plus_kernel", {}
            ),
        },
    }
    request = load(result / "request.json")
    selection = request.get("workload_selection", {})
    return {
        "workload": str(selection.get("workload") or result.parents[2].name),
        "cores": len(per_core),
        "result_dir": str(result),
        "gem5_binary_sha256": request.get("gem5", {}).get("binary_sha256"),
        "sideband_schema": sideband_schema,
        "formal_comparable": False,
        "accuracy_metrics_emitted": False,
        "structural_conservation": structural_conservation,
        "structural_conservation_without_hierarchy_completion": (
            structural_conservation_without_hierarchy_completion
        ),
        "hierarchy_gap_ratio": hierarchy_gap_ratio,
        "target_drain_complete": (
            aggregate["pending_without_response_uops"] == 0
            and aggregate["response_without_native_fact_uops"] == 0
        ),
        "per_core": per_core,
        "aggregate": aggregate,
        "native_ruby_pmu_by_scope": native_ruby_pmu_by_scope,
        "scope_metrics": scope_metrics,
    }


def apply_collection_tolerance(
    case: dict[str, Any], max_hierarchy_gap_ratio: float
) -> bool:
    if not 0.0 <= max_hierarchy_gap_ratio <= 1.0:
        raise ValueError("max hierarchy gap ratio must be in [0, 1]")
    ratio = case["hierarchy_gap_ratio"]
    hierarchy_within_tolerance = (
        ratio is not None and ratio <= max_hierarchy_gap_ratio
    )
    eligible = bool(
        case["target_drain_complete"]
        and case["structural_conservation_without_hierarchy_completion"]
        and hierarchy_within_tolerance
    )
    case["collection_tolerance"] = {
        "policy": "reported-hierarchy-gap-only",
        "max_hierarchy_gap_ratio": max_hierarchy_gap_ratio,
        "observed_hierarchy_gap_ratio": ratio,
        "hierarchy_gap_within_tolerance": hierarchy_within_tolerance,
        "all_other_structural_conservation": case[
            "structural_conservation_without_hierarchy_completion"
        ],
        "collection_eligible": eligible,
    }
    return eligible


def matrix_results(matrix: Path) -> Iterable[Path]:
    status = load(matrix / "status.json")
    for key, task in sorted(status.get("tasks", {}).items()):
        sample = task.get("sample", {})
        if sample.get("status") != "completed" or sample.get("return_code") != 0:
            raise ValueError(f"matrix task {key} is not a successful sample")
        result = sample.get("result_dir")
        if not result:
            raise ValueError(f"matrix task {key} lacks result_dir")
        yield Path(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, action="append", default=[])
    parser.add_argument("--matrix", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--max-hierarchy-gap-ratio",
        type=float,
        default=0.0,
        help=(
            "accept only the hierarchy-completion residual up to this fixed "
            "per-case ratio; all lifecycle, response, and other population "
            "checks remain strict"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=min(8, os.cpu_count() or 1),
        help="number of independent result directories to audit in parallel",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = list(args.result)
    for matrix in args.matrix:
        results.extend(matrix_results(matrix))
    if not results:
        raise SystemExit("at least one --result or --matrix is required")
    if args.jobs <= 0:
        raise SystemExit("--jobs must be positive")
    if args.jobs == 1 or len(results) == 1:
        cases = [audit_result(path) for path in results]
    else:
        with ProcessPoolExecutor(
            max_workers=min(args.jobs, len(results))
        ) as executor:
            cases = list(executor.map(audit_result, results))
    collection_eligible = [
        apply_collection_tolerance(case, args.max_hierarchy_gap_ratio)
        for case in cases
    ]
    document = {
        "schema": SCHEMA,
        "contract": "oracle-only-diagnostic-no-ape-wape",
        "formal_comparable_cases": 0,
        "all_structurally_conserved": all(
            case["structural_conservation"] for case in cases
        ),
        "all_target_drains_complete": all(
            case["target_drain_complete"] for case in cases
        ),
        "max_hierarchy_gap_ratio": args.max_hierarchy_gap_ratio,
        "all_collection_eligible": all(collection_eligible),
        "cases": cases,
    }
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if document["all_collection_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
