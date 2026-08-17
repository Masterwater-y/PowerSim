#!/usr/bin/env python3
"""Validate a classified gem5-FS kernel-events-v2 oracle.

This is intentionally a strict calibration gate.  The legacy
``irq_idle_kernel_cycles`` residual is not accepted: every measured core cycle
must be assigned to one explicit, mutually exclusive source.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path


SCHEMA = "tcsim-gem5-fs-kernel-events-v2"
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
EXACT_PMU_SOURCE = "taotrace-path-class-v2"
EXACT_IDLE_DETECTION = "x86-halt-mwait-or-repeated-f3-90-v2"
POLL_IDLE_PAUSE_THRESHOLD = 128
POLL_IDLE_MAX_GAP_COMMITS = 64


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oracle", type=Path)
    parser.add_argument(
        "--max-unknown-ratio",
        type=float,
        default=0.0,
        help="Maximum unknown_kernel_cycles / measured_cycles (default: 0).",
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


def close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


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
    exact_source = row.get("pmu_source") == "taotrace-path-class-v2"
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


def validate_row(row: dict, where: str, max_unknown_ratio: float) -> None:
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


def validate_document(document: dict, max_unknown_ratio: float) -> dict:
    if document.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA!r}")
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
        validate_row(row, f"per_core[{index}]", max_unknown_ratio)
    if sorted(core_ids) != list(range(len(per_core))):
        raise ValueError("per_core core_id values must be dense from zero")
    validate_row(aggregate, "aggregate", max_unknown_ratio)
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
    return {
        "schema": "fastsim-kernel-events-oracle-validation-v1",
        "oracle_schema": SCHEMA,
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
        "idle_detection": aggregate.get("idle_detection"),
    }


def main() -> int:
    args = parse_args()
    document = json.loads(args.oracle.read_text())
    result = validate_document(document, args.max_unknown_ratio)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
