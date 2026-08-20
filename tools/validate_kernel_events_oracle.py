#!/usr/bin/env python3
"""Validate a classified gem5-FS kernel-events v3 (or diagnostic v2) oracle.

This is intentionally a strict calibration gate.  The legacy
``irq_idle_kernel_cycles`` residual is not accepted: every measured core cycle
must be assigned to one explicit, mutually exclusive source.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


LEGACY_SCHEMA = "tcsim-gem5-fs-kernel-events-v2"
SCHEMA = "tcsim-gem5-fs-kernel-events-v3"
CYCLE_FIELDS = (
    "user_cycles",
    "syscall_kernel_cycles",
    "page_fault_kernel_cycles",
    "irq_kernel_cycles",
    "scheduler_kernel_cycles",
    "idle_cycles",
    "unknown_kernel_cycles",
)
SUMMED_FIELDS = ("measured_cycles", "n_user", *CYCLE_FIELDS)
P0_SUMMED_FIELDS = (
    "user_retired_instructions",
    "user_plus_kernel_retired_instructions",
)
KERNEL_PMU_CLASSES = (
    "syscall",
    "page_fault",
    "irq",
    "scheduler",
    "idle",
    "unknown_kernel",
)
ACTIVE_KERNEL_PMU_CLASSES = tuple(
    name for name in KERNEL_PMU_CLASSES if name != "idle"
)
LEGACY_PMU_SOURCE = "taotrace-path-class-v2"
EXACT_PMU_SOURCE = "taotrace-path-class-v3"
PMU_CONTRACT_ID = "perf-gem5-fastsim-x86-fs-v1"
P0_PMU_FIELDS = {
    "retired_instructions",
    "retired_uops",
    "memory_uops",
    "line_requests",
    "branches",
    "branch_misses",
    "l1d_accesses",
    "l1d_hits",
    "l1d_misses",
    "l1d_tag_accesses",
    "l1d_tag_hits",
    "l1d_tag_misses",
    "l2_accesses",
    "l2_hits",
    "l2_misses",
    "private_l2_tag_accesses",
    "private_l2_tag_hits",
    "private_l2_tag_misses",
    "llc_accesses",
    "llc_hits",
    "llc_misses",
    "llc_tag_accesses",
    "llc_tag_hits",
    "llc_tag_misses",
    "permission_upgrades",
    "remote_supplies",
    "llc_merged_misses",
    "llc_unique_fills",
    "dram_reads",
    "dram_writes",
    "dtlb_accesses",
    "dtlb_hits",
    "dtlb_misses",
}
EXACT_IDLE_DETECTION = "x86-halt-mwait-or-repeated-f3-90-v2"
POLL_IDLE_PAUSE_THRESHOLD = 128
POLL_IDLE_MAX_GAP_COMMITS = 64
FRONTEND_SCHEMA = "taotrace-scoped-frontend-v1"
FRONTEND_SCOPE = "exact-cpl-first-event-to-functional-target-window"
FRONTEND_TERMINAL_FIELDS = (
    "translation_squashes",
    "translation_faults",
    "no_good_address_terminals",
    "retry_discards",
    "icache_responses",
    "icache_squashed_responses",
)
FRONTEND_STATUS_FIELDS = (
    "running_cycles",
    "idle_cycles",
    "squashing_cycles",
    "blocked_cycles",
    "fetching_cycles",
    "trap_pending_cycles",
    "quiesce_pending_cycles",
    "itlb_wait_cycles",
    "icache_wait_response_cycles",
    "icache_wait_retry_cycles",
    "icache_access_complete_cycles",
    "ftq_wait_cycles",
    "no_good_addr_cycles",
)
FRONTEND_BOOLEAN_FIELDS = (
    "request_population_conserved",
    "request_mode_conserved",
    "request_reason_conserved",
    "send_accounting_conserved",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oracle", type=Path)
    parser.add_argument(
        "--max-unknown-ratio",
        type=float,
        default=0.0,
        help="Maximum unknown_kernel_cycles / measured_cycles (default: 0).",
    )
    parser.add_argument(
        "--allow-legacy-pmu",
        action="store_true",
        help=(
            "Validate the old v2 shape for diagnostics. It remains ineligible "
            "for formal cache-PMU accuracy because it lacks coverage accounting."
        ),
    )
    args = parser.parse_args()
    if not 0.0 <= args.max_unknown_ratio <= 1.0:
        parser.error("--max-unknown-ratio must be in [0, 1]")
    return args


def nonnegative_integer(row: dict, field: str, where: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{where}.{field} must be a non-negative integer")
    return value


def validate_frontend_accounting(row: dict, where: str) -> None:
    frontend = row.get("frontend_accounting")
    if frontend is None:
        return
    if not isinstance(frontend, dict):
        raise ValueError(f"{where}.frontend_accounting must be an object")
    if frontend.get("schema") != FRONTEND_SCHEMA:
        raise ValueError(f"{where} has unsupported frontend schema")
    if frontend.get("scope") != FRONTEND_SCOPE:
        raise ValueError(f"{where} has unsupported frontend scope")
    numeric_fields = (
        "inflight_at_start",
        "requests_started",
        "user_mode_requests_started",
        "kernel_mode_requests_started",
        "invalid_same_block_refetches",
        "invalid_new_block_requests",
        "valid_block_changes",
        "translations_completed",
        "icache_send_attempts",
        "icache_requests_sent",
        "icache_send_rejects",
        *FRONTEND_TERMINAL_FIELDS,
        "inflight_at_end",
        "squash_events",
        "squash_events_with_outstanding",
        "status_cycle_samples",
        *FRONTEND_STATUS_FIELDS,
        "request_to_response_ticks",
        "request_to_response_cycles",
        "request_to_response_samples",
    )
    values = {
        field: nonnegative_integer(frontend, field, f"{where}.frontend_accounting")
        for field in numeric_fields
    }
    expected = {
        "request_population_conserved": (
            values["inflight_at_start"] + values["requests_started"]
            == sum(values[field] for field in FRONTEND_TERMINAL_FIELDS)
            + values["inflight_at_end"]
        ),
        "request_mode_conserved": (
            values["requests_started"]
            == values["user_mode_requests_started"]
            + values["kernel_mode_requests_started"]
        ),
        "request_reason_conserved": (
            values["requests_started"]
            == values["invalid_same_block_refetches"]
            + values["invalid_new_block_requests"]
            + values["valid_block_changes"]
        ),
        "send_accounting_conserved": (
            values["icache_send_attempts"]
            == values["icache_requests_sent"]
            + values["icache_send_rejects"]
        ),
    }
    if values["status_cycle_samples"] != sum(
        values[field] for field in FRONTEND_STATUS_FIELDS
    ):
        raise ValueError(f"{where} frontend status cycles do not conserve")
    measured_cycles = int(row["measured_cycles"])
    # Fetch.tick precedes Commit.tick, so a target commit can contribute one
    # final sample.  Anything larger is an actual marker-to-CPL window leak.
    # A negative delta remains legal while a quiesced O3 CPU has no Fetch.tick.
    if values["status_cycle_samples"] > measured_cycles + 1:
        raise ValueError(f"{where} frontend samples extend past CPL window")
    for field, conserved in expected.items():
        if frontend.get(field) is not True or not conserved:
            raise ValueError(f"{where} frontend does not conserve {field}")
    sample_delta = frontend.get("status_sample_minus_measured_cycles")
    if sample_delta is not None:
        if isinstance(sample_delta, bool) or not isinstance(sample_delta, int):
            raise ValueError(
                f"{where}.frontend_accounting.status_sample_minus_measured_cycles "
                "must be an integer"
            )
        if sample_delta != values["status_cycle_samples"] - measured_cycles:
            raise ValueError(f"{where} frontend/measured cycle delta is stale")


def close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def validate_memory_accounting(row: dict, where: str) -> None:
    accounting = row.get("memory_accounting")
    if not isinstance(accounting, dict):
        raise ValueError(f"{where}.memory_accounting must be an object")
    fields = (
        "committed_memory_uops",
        "packet_attributed_uops",
        "fallback_attributed_uops",
        "explicitly_rejected_uops",
        "line_requests",
        "unaccounted_uops",
        "duplicate_accounting_uops",
        "dtlb_unknown_uops",
        "late_packets_after_fallback",
    )
    values = {
        field: nonnegative_integer(accounting, field, f"{where}.memory_accounting")
        for field in fields
    }
    expected = (
        values["packet_attributed_uops"]
        + values["fallback_attributed_uops"]
        + values["explicitly_rejected_uops"]
    )
    if values["committed_memory_uops"] != expected:
        raise ValueError(
            f"{where} memory-UOP coverage failed: committed="
            f"{values['committed_memory_uops']}, attributed/rejected={expected}"
        )
    if values["unaccounted_uops"] != 0:
        raise ValueError(f"{where} has unaccounted committed memory UOPs")
    if values["duplicate_accounting_uops"] != 0:
        raise ValueError(f"{where} has duplicate memory PMU accounting")
    if values["explicitly_rejected_uops"] != 0:
        raise ValueError(f"{where} has explicitly rejected committed memory UOPs")
    if values["dtlb_unknown_uops"] != 0:
        raise ValueError(f"{where} has unknown committed dTLB outcomes")
    if values["line_requests"] < values["committed_memory_uops"]:
        raise ValueError(f"{where} line requests are below committed memory UOPs")

    by_class = row.get("pmu_kernel_by_class")
    user = row.get("pmu_user")
    if not isinstance(by_class, dict) or not isinstance(user, dict):
        raise ValueError(f"{where} cannot prove scope-level memory conservation")
    all_class_memory_uops = int(user.get("memory_uops", -1)) + sum(
        int(by_class[name].get("memory_uops", -1)) for name in KERNEL_PMU_CLASSES
    )
    all_class_line_requests = int(user.get("line_requests", -1)) + sum(
        int(by_class[name].get("line_requests", -1)) for name in KERNEL_PMU_CLASSES
    )
    if all_class_memory_uops != values["committed_memory_uops"]:
        raise ValueError(
            f"{where} scope memory-UOP conservation failed: classes="
            f"{all_class_memory_uops}, committed={values['committed_memory_uops']}"
        )
    if all_class_line_requests != values["line_requests"]:
        raise ValueError(
            f"{where} scope line-request conservation failed: classes="
            f"{all_class_line_requests}, requests={values['line_requests']}"
        )


def validate_p0_pmu_semantics(pmu: dict, where: str) -> None:
    missing = P0_PMU_FIELDS - set(pmu)
    if missing:
        raise ValueError(f"{where} lacks P0 PMU fields: {sorted(missing)}")
    for field in P0_PMU_FIELDS:
        nonnegative_integer(pmu, field, where)
    for legacy, canonical in (
        ("l1d_accesses", "l1d_tag_accesses"),
        ("l1d_hits", "l1d_tag_hits"),
        ("l1d_misses", "l1d_tag_misses"),
        ("l2_accesses", "private_l2_tag_accesses"),
        ("l2_hits", "private_l2_tag_hits"),
        ("l2_misses", "private_l2_tag_misses"),
        ("llc_accesses", "llc_tag_accesses"),
        ("llc_hits", "llc_tag_hits"),
        ("llc_misses", "llc_tag_misses"),
    ):
        if pmu[legacy] != pmu[canonical]:
            raise ValueError(
                f"{where} alias mismatch: {legacy}={pmu[legacy]}, "
                f"{canonical}={pmu[canonical]}"
            )
    if pmu["memory_uops"] > pmu["line_requests"]:
        raise ValueError(f"{where} line requests are below memory UOPs")
    if pmu["line_requests"] != pmu["l1d_tag_accesses"]:
        raise ValueError(f"{where} line requests do not conserve L1D lookups")
    if pmu["l1d_tag_misses"] != pmu["private_l2_tag_accesses"]:
        raise ValueError(f"{where} L1D misses do not conserve L2 lookups")
    if pmu["private_l2_tag_misses"] != pmu["llc_tag_accesses"]:
        raise ValueError(f"{where} L2 misses do not conserve LLC lookups")
    if pmu["dtlb_accesses"] != pmu["memory_uops"]:
        raise ValueError(f"{where} dTLB lookups do not conserve memory UOPs")
    if pmu["dtlb_accesses"] != pmu["dtlb_hits"] + pmu["dtlb_misses"]:
        raise ValueError(f"{where} dTLB hit/miss outcomes do not conserve lookups")
    if (
        pmu["permission_upgrades"] > pmu["line_requests"]
        or pmu["remote_supplies"] > pmu["line_requests"]
        or pmu["llc_merged_misses"] > pmu["llc_tag_misses"]
        or pmu["llc_unique_fills"] > pmu["llc_tag_misses"]
        or pmu["dram_reads"] > pmu["llc_unique_fills"]
    ):
        raise ValueError(f"{where} hierarchy subpopulation exceeds its parent")


def validate_pmu_scopes(row: dict, where: str) -> None:
    user = row.get("pmu_user")
    combined = row.get("pmu_user_plus_kernel")
    if (user is None) != (combined is None):
        raise ValueError(
            f"{where} must provide both pmu_user and pmu_user_plus_kernel"
        )
    if user is None:
        return
    if not isinstance(user, dict) or not isinstance(combined, dict):
        raise ValueError(f"{where} PMU scopes must be objects")
    if set(user) != set(combined) or not user:
        raise ValueError(f"{where} PMU scopes must have identical non-empty fields")
    for field in user:
        user_value = nonnegative_integer(user, field, f"{where}.pmu_user")
        combined_value = nonnegative_integer(
            combined, field, f"{where}.pmu_user_plus_kernel"
        )
        if combined_value < user_value:
            raise ValueError(
                f"{where} combined PMU field {field} is below user-only count"
            )

    by_class = row.get("pmu_kernel_by_class")
    exact_source = row.get("pmu_source") in (LEGACY_PMU_SOURCE, EXACT_PMU_SOURCE)
    if by_class is None:
        if exact_source:
            raise ValueError(f"{where} exact PMU source lacks per-class data")
        return
    if not isinstance(by_class, dict) or set(by_class) != set(
        KERNEL_PMU_CLASSES
    ):
        raise ValueError(
            f"{where}.pmu_kernel_by_class must contain exactly "
            f"{KERNEL_PMU_CLASSES}"
        )
    for name in KERNEL_PMU_CLASSES:
        pmu = by_class[name]
        if not isinstance(pmu, dict) or set(pmu) != set(user):
            raise ValueError(
                f"{where}.pmu_kernel_by_class.{name} has inconsistent fields"
            )
        for field in user:
            nonnegative_integer(
                pmu, field, f"{where}.pmu_kernel_by_class.{name}"
            )
    for field in user:
        expected = user[field] + sum(
            by_class[name][field] for name in ACTIVE_KERNEL_PMU_CLASSES
        )
        if combined[field] != expected:
            raise ValueError(
                f"{where} PMU conservation failed for {field}: "
                f"combined={combined[field]}, expected={expected}"
            )

    profiles = row.get("syscall_profiles")
    if not isinstance(profiles, list):
        raise ValueError(f"{where}.syscall_profiles must be an array")
    seen = set()
    profile_count = 0
    profile_cycles = 0
    profile_pmu = {field: 0 for field in user}
    for index, profile in enumerate(profiles):
        profile_where = f"{where}.syscall_profiles[{index}]"
        if not isinstance(profile, dict):
            raise ValueError(f"{profile_where} must be an object")
        sysnum = nonnegative_integer(profile, "sysnum", profile_where)
        if sysnum in seen:
            raise ValueError(f"{where} repeats syscall profile {sysnum}")
        seen.add(sysnum)
        profile_count += nonnegative_integer(profile, "count", profile_where)
        profile_cycles += nonnegative_integer(
            profile, "kernel_cycles", profile_where
        )
        pmu = profile.get("pmu")
        if not isinstance(pmu, dict) or set(pmu) != set(user):
            raise ValueError(f"{profile_where}.pmu has inconsistent fields")
        for field in user:
            profile_pmu[field] += nonnegative_integer(
                pmu, field, f"{profile_where}.pmu"
            )
    event_counts = row.get("event_counts")
    if not isinstance(event_counts, dict):
        raise ValueError(f"{where}.event_counts must be an object")
    if profile_count != nonnegative_integer(event_counts, "syscall", where):
        raise ValueError(f"{where} syscall profile counts do not conserve")
    if profile_cycles != row["syscall_kernel_cycles"]:
        raise ValueError(f"{where} syscall profile cycles do not conserve")
    for field in user:
        if profile_pmu[field] != by_class["syscall"][field]:
            raise ValueError(
                f"{where} syscall PMU profiles do not conserve {field}"
            )


def validate_row(
    row: dict, where: str, max_unknown_ratio: float, strict_p0: bool
) -> None:
    measured = nonnegative_integer(row, "measured_cycles", where)
    n_user = nonnegative_integer(row, "n_user", where)
    classified = sum(nonnegative_integer(row, field, where) for field in CYCLE_FIELDS)
    if classified != measured:
        raise ValueError(
            f"{where} cycle conservation failed: classified={classified}, "
            f"measured={measured}"
        )
    unknown = row["unknown_kernel_cycles"]
    unknown_ratio = unknown / measured if measured else 0.0
    if unknown_ratio > max_unknown_ratio:
        raise ValueError(
            f"{where} unknown cycle ratio {unknown_ratio:.9g} exceeds "
            f"{max_unknown_ratio:.9g}"
        )
    expected_user = row["user_cycles"] / n_user if n_user else 0.0
    expected_combined = (
        row["user_cycles"]
        + row["syscall_kernel_cycles"]
        + row["page_fault_kernel_cycles"]
        + row["irq_kernel_cycles"]
        + row["scheduler_kernel_cycles"]
        + row["unknown_kernel_cycles"]
    ) / n_user if n_user else 0.0
    for field, expected in (
        ("cpi_user", expected_user),
        ("cpi_user_plus_kernel", expected_combined),
    ):
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{where}.{field} must be numeric")
        if not math.isfinite(value) or not close(float(value), expected):
            raise ValueError(
                f"{where}.{field}={value!r}, expected {expected:.17g}"
            )
    if strict_p0:
        for field, expected in (
            ("cycles_per_user_uop_user", expected_user),
            ("cycles_per_user_uop_user_plus_kernel", expected_combined),
        ):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{where}.{field} must be numeric")
            if not math.isfinite(value) or not close(float(value), expected):
                raise ValueError(
                    f"{where}.{field}={value!r}, expected {expected:.17g}"
                )
        user_instructions = nonnegative_integer(
            row, "user_retired_instructions", where
        )
        combined_instructions = nonnegative_integer(
            row, "user_plus_kernel_retired_instructions", where
        )
        if combined_instructions < user_instructions:
            raise ValueError(
                f"{where} combined retired instructions are below user scope"
            )
        expected_user_perf = (
            row["user_cycles"] / user_instructions if user_instructions else 0.0
        )
        expected_combined_perf = (
            (
                row["user_cycles"]
                + row["syscall_kernel_cycles"]
                + row["page_fault_kernel_cycles"]
                + row["irq_kernel_cycles"]
                + row["scheduler_kernel_cycles"]
                + row["unknown_kernel_cycles"]
            )
            / combined_instructions
            if combined_instructions
            else 0.0
        )
        for field, expected in (
            ("perf_like_cpi_user", expected_user_perf),
            ("perf_like_cpi_user_plus_kernel", expected_combined_perf),
        ):
            value = row.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{where}.{field} must be numeric")
            if not math.isfinite(value) or not close(float(value), expected):
                raise ValueError(
                    f"{where}.{field}={value!r}, expected {expected:.17g}"
                )
    blocked = row.get("blocked_wall_cycles", 0)
    if isinstance(blocked, bool) or not isinstance(blocked, int) or blocked < 0:
        raise ValueError(f"{where}.blocked_wall_cycles must be non-negative")
    if row.get("pmu_source") == EXACT_PMU_SOURCE:
        expected_idle_metadata = {
            "idle_detection": EXACT_IDLE_DETECTION,
            "poll_idle_pause_threshold": POLL_IDLE_PAUSE_THRESHOLD,
            "poll_idle_max_gap_commits": POLL_IDLE_MAX_GAP_COMMITS,
        }
        for field, expected in expected_idle_metadata.items():
            if row.get(field) != expected:
                raise ValueError(
                    f"{where}.{field}={row.get(field)!r}, expected "
                    f"{expected!r}"
                )
    validate_pmu_scopes(row, where)
    validate_frontend_accounting(row, where)
    if strict_p0:
        if row.get("pmu_source") != EXACT_PMU_SOURCE:
            raise ValueError(
                f"{where}.pmu_source must be {EXACT_PMU_SOURCE!r}"
            )
        if row.get("pmu_contract_id") != PMU_CONTRACT_ID:
            raise ValueError(
                f"{where}.pmu_contract_id must be {PMU_CONTRACT_ID!r}"
            )
        validate_p0_pmu_semantics(row["pmu_user"], f"{where}.pmu_user")
        validate_p0_pmu_semantics(
            row["pmu_user_plus_kernel"],
            f"{where}.pmu_user_plus_kernel",
        )
        for name in KERNEL_PMU_CLASSES:
            validate_p0_pmu_semantics(
                row["pmu_kernel_by_class"][name],
                f"{where}.pmu_kernel_by_class.{name}",
            )
        for index, profile in enumerate(row["syscall_profiles"]):
            validate_p0_pmu_semantics(
                profile["pmu"], f"{where}.syscall_profiles[{index}].pmu"
            )
        validate_memory_accounting(row, where)


def validate_document(document: dict, max_unknown_ratio: float) -> dict:
    oracle_schema = document.get("schema")
    if oracle_schema not in (SCHEMA, LEGACY_SCHEMA):
        raise ValueError(f"schema must be {SCHEMA!r} or {LEGACY_SCHEMA!r}")
    strict_p0 = oracle_schema == SCHEMA
    per_core = document.get("per_core")
    aggregate = document.get("aggregate")
    if not isinstance(per_core, list) or not per_core:
        raise ValueError("per_core must be a non-empty array")
    if not isinstance(aggregate, dict):
        raise ValueError("aggregate must be an object")
    core_ids = []
    for index, row in enumerate(per_core):
        if not isinstance(row, dict):
            raise ValueError(f"per_core[{index}] must be an object")
        core_id = nonnegative_integer(row, "core_id", f"per_core[{index}]")
        core_ids.append(core_id)
        validate_row(row, f"per_core[{index}]", max_unknown_ratio, strict_p0)
    if sorted(core_ids) != list(range(len(per_core))):
        raise ValueError("per_core core_id values must be dense from zero")
    validate_row(aggregate, "aggregate", max_unknown_ratio, strict_p0)
    for field in SUMMED_FIELDS:
        expected = sum(row[field] for row in per_core)
        if aggregate[field] != expected:
            raise ValueError(
                f"aggregate.{field}={aggregate[field]}, per-core sum={expected}"
            )
    aggregate_has_pmu = "pmu_user" in aggregate
    if any(("pmu_user" in row) != aggregate_has_pmu for row in per_core):
        raise ValueError("aggregate and every per-core row must agree on PMU scopes")
    if aggregate_has_pmu:
        for scope in ("pmu_user", "pmu_user_plus_kernel"):
            for field, value in aggregate[scope].items():
                expected = sum(row[scope][field] for row in per_core)
                if value != expected:
                    raise ValueError(
                        f"aggregate.{scope}.{field}={value}, "
                        f"per-core sum={expected}"
                    )
        aggregate_has_class_pmu = "pmu_kernel_by_class" in aggregate
        if any(
            ("pmu_kernel_by_class" in row) != aggregate_has_class_pmu
            for row in per_core
        ):
            raise ValueError(
                "aggregate and per-core rows disagree on per-class PMU"
            )
        if aggregate_has_class_pmu:
            for name in KERNEL_PMU_CLASSES:
                for field, value in aggregate["pmu_kernel_by_class"][
                    name
                ].items():
                    expected = sum(
                        row["pmu_kernel_by_class"][name][field]
                        for row in per_core
                    )
                    if value != expected:
                        raise ValueError(
                            f"aggregate per-class PMU mismatch for "
                            f"{name}.{field}: {value} != {expected}"
                        )
    if strict_p0:
        for field in P0_SUMMED_FIELDS:
            expected = sum(row[field] for row in per_core)
            if aggregate[field] != expected:
                raise ValueError(
                    f"aggregate.{field}={aggregate[field]}, "
                    f"per-core sum={expected}"
                )
        for field, value in aggregate["memory_accounting"].items():
            expected = sum(row["memory_accounting"][field] for row in per_core)
            if value != expected:
                raise ValueError(
                    f"aggregate.memory_accounting.{field}={value}, "
                    f"per-core sum={expected}"
                )
    aggregate_has_frontend = "frontend_accounting" in aggregate
    if any(
        ("frontend_accounting" in row) != aggregate_has_frontend
        for row in per_core
    ):
        raise ValueError(
            "aggregate and every per-core row must agree on frontend accounting"
        )
    if aggregate_has_frontend:
        aggregate_frontend = aggregate["frontend_accounting"]
        for field, value in aggregate_frontend.items():
            if type(value) is not int or field == (
                "status_sample_minus_measured_cycles"
            ):
                continue
            expected = sum(row["frontend_accounting"][field] for row in per_core)
            if value != expected:
                raise ValueError(
                    f"aggregate.frontend_accounting.{field}={value}, "
                    f"per-core sum={expected}"
                )
    return {
        "schema": "fastsim-kernel-events-oracle-validation-v1",
        "oracle_schema": oracle_schema,
        "cores": len(per_core),
        "measured_cycles": aggregate["measured_cycles"],
        "unknown_kernel_cycles": aggregate["unknown_kernel_cycles"],
        "unknown_ratio": (
            aggregate["unknown_kernel_cycles"] / aggregate["measured_cycles"]
            if aggregate["measured_cycles"]
            else 0.0
        ),
        "cycle_conservation": True,
        "dual_cpi_conservation": True,
        "pmu_scopes_present": "pmu_user" in aggregate,
        "pmu_class_conservation": "pmu_kernel_by_class" in aggregate,
        "pmu_contract_id": aggregate.get("pmu_contract_id"),
        "memory_coverage_conservation": strict_p0,
        "formal_pmu_eligible": strict_p0,
        "idle_detection": aggregate.get("idle_detection"),
        "frontend_accounting_present": aggregate_has_frontend,
    }


def main() -> int:
    args = parse_args()
    document = json.loads(args.oracle.read_text())
    result = validate_document(document, args.max_unknown_ratio)
    if not result["formal_pmu_eligible"] and not args.allow_legacy_pmu:
        raise SystemExit(
            "legacy kernel-events-v2 PMU lacks P0 memory coverage; "
            "use --allow-legacy-pmu only for diagnostic validation"
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
