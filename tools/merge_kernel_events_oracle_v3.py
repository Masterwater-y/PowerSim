#!/usr/bin/env python3
"""Merge TaoTrace per-core P0 PMU rows into one fail-closed v3 oracle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from validate_kernel_events_oracle import (
    ACTIVE_KERNEL_PMU_CLASSES,
    BRANCH_MISS_SOURCE,
    CYCLE_FIELDS,
    EXACT_IDLE_DETECTION,
    EXACT_PMU_SOURCE,
    FRONTEND_BOOLEAN_FIELDS,
    FRONTEND_SCHEMA,
    FRONTEND_SCOPE,
    KERNEL_PMU_CLASSES,
    PMU_CONTRACT_ID,
    POLL_IDLE_MAX_GAP_COMMITS,
    POLL_IDLE_PAUSE_THRESHOLD,
    SCHEMA,
    validate_document,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oracle_dir", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def require_uniform(rows: list[dict], field: str, expected=None):
    values = {row.get(field) for row in rows}
    if len(values) != 1:
        raise ValueError(f"per-core {field} is inconsistent: {values}")
    value = values.pop()
    if expected is not None and value != expected:
        raise ValueError(f"per-core {field}={value!r}, expected {expected!r}")
    return value


def sum_scope(rows: list[dict], scope: str) -> dict:
    fields = set(rows[0].get(scope, {}))
    if not fields or any(set(row.get(scope, {})) != fields for row in rows):
        raise ValueError(f"per-core {scope} fields are inconsistent")
    return {
        field: sum(int(row[scope][field]) for row in rows)
        for field in sorted(fields)
    }


def merge(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda row: int(row["core_id"]))
    core_ids = [int(row["core_id"]) for row in rows]
    if core_ids != list(range(len(rows))):
        raise ValueError(f"core IDs must be dense from zero, found {core_ids}")
    require_uniform(rows, "pmu_source", EXACT_PMU_SOURCE)
    require_uniform(rows, "pmu_contract_id", PMU_CONTRACT_ID)
    require_uniform(rows, "branch_miss_source", BRANCH_MISS_SOURCE)
    require_uniform(rows, "idle_detection", EXACT_IDLE_DETECTION)
    require_uniform(
        rows, "poll_idle_pause_threshold", POLL_IDLE_PAUSE_THRESHOLD
    )
    require_uniform(
        rows, "poll_idle_max_gap_commits", POLL_IDLE_MAX_GAP_COMMITS
    )

    scalar_fields = (
        "measured_cycles",
        "n_user",
        *CYCLE_FIELDS,
        "blocked_wall_cycles",
        "user_retired_instructions",
        "user_plus_kernel_retired_instructions",
    )
    aggregate = {
        field: sum(int(row[field]) for row in rows) for field in scalar_fields
    }
    n_user = aggregate["n_user"]
    active_cycles = sum(
        aggregate[field]
        for field in CYCLE_FIELDS
        if field != "idle_cycles"
    )
    user_cycles = aggregate["user_cycles"]
    user_instructions = aggregate["user_retired_instructions"]
    combined_instructions = aggregate["user_plus_kernel_retired_instructions"]
    aggregate.update(
        {
            "cpi_user": user_cycles / n_user if n_user else 0.0,
            "cpi_user_plus_kernel": active_cycles / n_user if n_user else 0.0,
            "cycles_per_user_uop_user": (
                user_cycles / n_user if n_user else 0.0
            ),
            "cycles_per_user_uop_user_plus_kernel": (
                active_cycles / n_user if n_user else 0.0
            ),
            "perf_like_cpi_user": (
                user_cycles / user_instructions if user_instructions else 0.0
            ),
            "perf_like_cpi_user_plus_kernel": (
                active_cycles / combined_instructions
                if combined_instructions
                else 0.0
            ),
            "pmu_source": EXACT_PMU_SOURCE,
            "pmu_contract_id": PMU_CONTRACT_ID,
            "branch_miss_source": BRANCH_MISS_SOURCE,
            "idle_detection": EXACT_IDLE_DETECTION,
            "poll_idle_pause_threshold": POLL_IDLE_PAUSE_THRESHOLD,
            "poll_idle_max_gap_commits": POLL_IDLE_MAX_GAP_COMMITS,
        }
    )

    aggregate["pmu_user"] = sum_scope(rows, "pmu_user")
    aggregate["pmu_user_plus_kernel"] = sum_scope(
        rows, "pmu_user_plus_kernel"
    )
    pmu_fields = set(aggregate["pmu_user"])
    for row in rows:
        by_class = row.get("pmu_kernel_by_class")
        if not isinstance(by_class, dict) or set(by_class) != set(
            KERNEL_PMU_CLASSES
        ):
            raise ValueError("per-core PMU classes are missing or inconsistent")
        if any(set(by_class[name]) != pmu_fields for name in KERNEL_PMU_CLASSES):
            raise ValueError("per-core PMU class fields are inconsistent")
    aggregate["pmu_kernel_by_class"] = {
        name: {
            field: sum(
                int(row["pmu_kernel_by_class"][name][field]) for row in rows
            )
            for field in sorted(pmu_fields)
        }
        for name in KERNEL_PMU_CLASSES
    }

    profiles: dict[int, dict] = {}
    for row in rows:
        seen: set[int] = set()
        for profile in row.get("syscall_profiles", []):
            sysnum = int(profile["sysnum"])
            if sysnum in seen:
                raise ValueError(f"core {row['core_id']} repeats syscall {sysnum}")
            seen.add(sysnum)
            if set(profile.get("pmu", {})) != pmu_fields:
                raise ValueError(f"syscall {sysnum} PMU fields are inconsistent")
            target = profiles.setdefault(
                sysnum,
                {
                    "sysnum": sysnum,
                    "count": 0,
                    "kernel_cycles": 0,
                    "pmu": {field: 0 for field in sorted(pmu_fields)},
                },
            )
            target["count"] += int(profile["count"])
            target["kernel_cycles"] += int(profile["kernel_cycles"])
            for field in pmu_fields:
                target["pmu"][field] += int(profile["pmu"][field])
    aggregate["syscall_profiles"] = [profiles[key] for key in sorted(profiles)]

    event_fields = set(rows[0].get("event_counts", {}))
    if any(set(row.get("event_counts", {})) != event_fields for row in rows):
        raise ValueError("per-core event-count fields are inconsistent")
    aggregate["event_counts"] = {
        field: sum(int(row["event_counts"][field]) for row in rows)
        for field in sorted(event_fields)
    }
    accounting_fields = set(rows[0].get("memory_accounting", {}))
    if not accounting_fields or any(
        set(row.get("memory_accounting", {})) != accounting_fields for row in rows
    ):
        raise ValueError("per-core memory-accounting fields are inconsistent")
    aggregate["memory_accounting"] = {
        field: sum(int(row["memory_accounting"][field]) for row in rows)
        for field in sorted(accounting_fields)
    }

    frontend_rows = [row.get("frontend_accounting") for row in rows]
    if any(item is not None for item in frontend_rows):
        if any(not isinstance(item, dict) for item in frontend_rows):
            raise ValueError("per-core frontend accounting is incomplete")
        if {item.get("schema") for item in frontend_rows} != {
            FRONTEND_SCHEMA
        }:
            raise ValueError("per-core frontend schemas are inconsistent")
        if {item.get("scope") for item in frontend_rows} != {FRONTEND_SCOPE}:
            raise ValueError("per-core frontend scopes are inconsistent")
        numeric_fields = {
            field
            for field, value in frontend_rows[0].items()
            if type(value) is int
        }
        if any(
            {
                field
                for field, value in item.items()
                if type(value) is int
            }
            != numeric_fields
            for item in frontend_rows
        ):
            raise ValueError("per-core frontend fields are inconsistent")
        frontend_aggregate = {
            field: sum(int(item[field]) for item in frontend_rows)
            for field in sorted(numeric_fields)
        }
        frontend_aggregate.update(
            {"schema": FRONTEND_SCHEMA, "scope": FRONTEND_SCOPE}
        )
        frontend_aggregate.update(
            {field: True for field in FRONTEND_BOOLEAN_FIELDS}
        )
        frontend_aggregate["status_sample_minus_measured_cycles"] = (
            frontend_aggregate["status_cycle_samples"]
            - aggregate["measured_cycles"]
        )
        aggregate["frontend_accounting"] = frontend_aggregate

    irq_vectors: dict[str, int] = {}
    for row in rows:
        for vector, count in row.get("irq_vectors", {}).items():
            irq_vectors[str(vector)] = irq_vectors.get(str(vector), 0) + int(count)
    aggregate["irq_vectors"] = dict(
        sorted(irq_vectors.items(), key=lambda item: int(item[0]))
    )
    unknown_sources: dict[str, int] = {}
    for row in rows:
        for source in row.get("unknown_kernel_sources", []):
            name = str(source["source"])
            unknown_sources[name] = unknown_sources.get(name, 0) + int(
                source["count"]
            )
    aggregate["unknown_kernel_sources"] = [
        {"source": name, "count": unknown_sources[name]}
        for name in sorted(unknown_sources)
    ]

    # Redundant conservation here produces a local error before the full
    # document validator and makes merger failures easier to diagnose.
    for field in pmu_fields:
        expected = aggregate["pmu_user"][field] + sum(
            aggregate["pmu_kernel_by_class"][name][field]
            for name in ACTIVE_KERNEL_PMU_CLASSES
        )
        if aggregate["pmu_user_plus_kernel"][field] != expected:
            raise ValueError(f"aggregate PMU does not conserve {field}")

    document = {"schema": SCHEMA, "per_core": rows, "aggregate": aggregate}
    validate_document(document, 0.0)
    return document


def main() -> int:
    args = parse_args()
    core_paths = sorted(args.oracle_dir.glob("kernel-events-core*.json"))
    if not core_paths:
        raise SystemExit(f"{args.oracle_dir}: no kernel-events-core*.json files")
    try:
        document = merge(
            [json.loads(path.read_text(encoding="utf-8")) for path in core_paths]
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"cannot merge P0 oracle: {exc}") from exc
    output = args.output or args.oracle_dir / "kernel_events.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
