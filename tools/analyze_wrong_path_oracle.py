#!/usr/bin/env python3
"""Turn a validated TaoTrace wrong-path sidecar into causal diagnostics.

All quantities are offline oracle attribution.  In particular, capacity and
active-window CPI equivalents are bounds/exposure metrics, not production CPI
corrections and not FastSim inputs.
"""

from __future__ import annotations

import argparse
import configparser
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from validate_wrong_path_oracle import validate


DEPTH_FIELDS = (
    "fetched_instructions",
    "renamed_instructions",
    "dispatched_instructions",
    "issued_instructions",
    "completed_instructions",
    "memory_instructions",
    "data_completed_instructions",
)
FLAG_TO_DEPTH = {
    "fetched": "fetched_instructions",
    "renamed": "renamed_instructions",
    "dispatched": "dispatched_instructions",
    "execute_seen": "issued_instructions",
    "to_commit_seen": "completed_instructions",
    "data_complete_seen": "data_completed_instructions",
}
SCOPED_COUNT_FIELDS = (
    "instruction_records",
    *DEPTH_FIELDS,
    "load_instructions",
    "store_instructions",
    "source_registers",
    "destination_registers",
    "renamed_memory_instructions",
    "renamed_source_registers",
    "renamed_destination_registers",
)
CACHE_LINE_BYTES = 64
PAGE_BYTES = 4096
PMU_FIELDS = (
    "retired_instructions",
    "retired_uops",
    "branches",
    "branch_misses",
    "l1d_accesses",
    "l1d_hits",
    "l1d_misses",
    "l2_accesses",
    "l2_hits",
    "l2_misses",
    "llc_accesses",
    "llc_hits",
    "llc_misses",
    "dtlb_accesses",
    "dtlb_hits",
    "dtlb_misses",
)


def quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return float(ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction)


def distribution(values: list[float]) -> dict[str, float]:
    return {
        "mean": sum(values) / len(values) if values else 0.0,
        "p50": quantile(values, 0.50),
        "p90": quantile(values, 0.90),
        "p99": quantile(values, 0.99),
        "max": max(values, default=0.0),
    }


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 2:
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_energy = sum((x - left_mean) ** 2 for x in left)
    right_energy = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_energy * right_energy)
    return numerator / denominator if denominator else None


def merge_intervals(intervals: list[tuple[int, int]]) -> int:
    total = 0
    end = -1
    for start, stop in sorted(intervals):
        if stop <= start:
            continue
        if start > end:
            total += stop - start
            end = stop
        elif stop > end:
            total += stop - end
            end = stop
    return total


def address_units(address: int, size: int, unit_bytes: int) -> range:
    """Return every cache line/page touched by one non-zero address range."""
    if address <= 0:
        return range(0)
    last_address = address + max(size, 1) - 1
    return range(address // unit_bytes, last_address // unit_bytes + 1)


def new_memory_footprint() -> dict:
    return {
        "counts": Counter(),
        "all_physical_cache_lines": set(),
        "all_physical_pages": set(),
        "all_virtual_cache_lines": set(),
        "all_virtual_pages": set(),
        "completed_physical_cache_lines": set(),
        "completed_physical_pages": set(),
        "completed_virtual_cache_lines": set(),
        "completed_virtual_pages": set(),
    }


def add_memory_footprint(footprint: dict, row: dict) -> None:
    """Accumulate address coverage for one in-scope wrong-path memory uop."""
    counts = footprint["counts"]
    counts["memory_instruction_records"] += 1
    size = int(row.get("size", 0))
    vaddr = int(row.get("vaddr", 0))
    paddr = int(row.get("paddr", 0))
    completed = bool(int(row.get("data_complete_seen", 0)))
    if completed:
        counts["data_completed_records"] += 1
    elif int(row.get("execute_seen", 0)):
        counts["executed_inflight_at_squash_records"] += 1

    for address_kind, address in (("virtual", vaddr), ("physical", paddr)):
        if address <= 0:
            continue
        counts[f"{address_kind}_address_records"] += 1
        footprint[f"all_{address_kind}_cache_lines"].update(
            address_units(address, size, CACHE_LINE_BYTES)
        )
        footprint[f"all_{address_kind}_pages"].update(
            address_units(address, size, PAGE_BYTES)
        )
        if completed:
            counts[f"data_completed_{address_kind}_address_records"] += 1
            footprint[f"completed_{address_kind}_cache_lines"].update(
                address_units(address, size, CACHE_LINE_BYTES)
            )
            footprint[f"completed_{address_kind}_pages"].update(
                address_units(address, size, PAGE_BYTES)
            )


def merge_memory_footprint(destination: dict, source: dict) -> None:
    destination["counts"].update(source["counts"])
    for field, values in source.items():
        if field != "counts":
            destination[field].update(values)


def summarize_memory_footprint(
    footprint: dict, total_user_records: int
) -> dict:
    counts = footprint["counts"]
    memory_records = int(counts["memory_instruction_records"])
    completed_records = int(counts["data_completed_records"])
    completed_physical_lines = len(footprint["completed_physical_cache_lines"])
    completed_virtual_lines = len(footprint["completed_virtual_cache_lines"])
    result = {
        "cache_line_bytes": CACHE_LINE_BYTES,
        "page_bytes": PAGE_BYTES,
        **{name: int(value) for name, value in sorted(counts.items())},
        "data_completed_percent_of_memory": (
            100.0 * completed_records / memory_records if memory_records else 0.0
        ),
    }
    for field in (
        "memory_instruction_records",
        "data_completed_records",
        "executed_inflight_at_squash_records",
        "physical_address_records",
        "virtual_address_records",
        "data_completed_physical_address_records",
        "data_completed_virtual_address_records",
    ):
        result.setdefault(field, 0)
    for stage in ("all", "completed"):
        for address_kind in ("physical", "virtual"):
            result[f"{stage}_unique_{address_kind}_cache_lines"] = len(
                footprint[f"{stage}_{address_kind}_cache_lines"]
            )
            result[f"{stage}_unique_{address_kind}_pages"] = len(
                footprint[f"{stage}_{address_kind}_pages"]
            )
    result["data_completed_physical_records_per_unique_cache_line"] = (
        int(counts["data_completed_physical_address_records"])
        / completed_physical_lines
        if completed_physical_lines
        else 0.0
    )
    result["data_completed_virtual_records_per_unique_cache_line"] = (
        int(counts["data_completed_virtual_address_records"])
        / completed_virtual_lines
        if completed_virtual_lines
        else 0.0
    )
    normalization = 1000.0 / total_user_records if total_user_records else 0.0
    result["memory_instruction_records_per_1k_user_records"] = (
        memory_records * normalization
    )
    result["data_completed_records_per_1k_user_records"] = (
        completed_records * normalization
    )
    result["executed_inflight_at_squash_records_per_1k_user_records"] = (
        int(counts["executed_inflight_at_squash_records"]) * normalization
    )
    result["completed_unique_physical_cache_lines_per_1k_user_records"] = (
        completed_physical_lines * normalization
    )
    result["completed_unique_physical_pages_per_1k_user_records"] = (
        len(footprint["completed_physical_pages"]) * normalization
    )
    return result


def summarize_memory_phase_transition(before: dict, late: dict) -> dict:
    candidates = int(before["counts"]["executed_inflight_at_squash_records"])
    completions = int(late["counts"]["data_completed_records"])
    result = {
        "executed_inflight_at_squash_records": candidates,
        "observed_late_data_completed_records": completions,
        "observed_late_completion_percent_of_candidates": (
            100.0 * completions / candidates if candidates else 0.0
        ),
    }
    for address_kind in ("physical", "virtual"):
        for unit in ("cache_lines", "pages"):
            before_values = before[f"completed_{address_kind}_{unit}"]
            late_values = late[f"completed_{address_kind}_{unit}"]
            result[f"late_unique_{address_kind}_{unit}"] = len(late_values)
            result[f"late_overlapping_pre_squash_{address_kind}_{unit}"] = len(
                before_values & late_values
            )
            result[f"late_novel_{address_kind}_{unit}"] = len(
                late_values - before_values
            )
    return result


def load_widths(path: Path) -> dict[str, int]:
    document = json.loads(path.read_text())
    found = None
    stack = [document]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if all(name in value for name in ("fetchWidth", "renameWidth", "dispatchWidth", "issueWidth")):
                found = value
                break
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    if found is None:
        raise ValueError(f"{path}: could not find O3 pipeline widths")
    return {
        "fetch": int(found["fetchWidth"]),
        "rename": int(found["renameWidth"]),
        "dispatch": int(found["dispatchWidth"]),
        "issue": int(found["issueWidth"]),
    }


def load_clock_ticks(path: Path) -> float:
    parser = configparser.ConfigParser(strict=False)
    parser.read(path)
    return float(parser["board.clk_domain"]["clock"])


def load_fastsim_user_result(path: Path, expected_user_records: int) -> dict:
    document = json.loads(path.read_text())
    if document.get("schema") != "fastsim-stats-v5":
        raise ValueError(
            f"{path}: formal same-window comparison requires fastsim-stats-v5"
        )
    scope = document["configuration"].get("measurement_scope")
    if scope != "user":
        raise ValueError(
            f"{path}: expected measurement_scope=user, observed {scope!r}"
        )
    metrics = document["scope_metrics"]
    retired_uops = int(metrics["user_trace_uops"])
    if retired_uops != expected_user_records:
        raise ValueError(
            f"{path}: retired_uops={retired_uops} does not match oracle "
            f"n_user={expected_user_records}"
        )
    pmu = {name: int(metrics["pmu"][name]) for name in PMU_FIELDS}
    per_core = {
        int(row["core"]): {
            "user_records": int(row["uops"]),
            "sum_core_cycles": int(row["cycles"]),
            "user_cpi": float(row["cycles"]) / int(row["uops"]),
        }
        for row in document["cores"]
    }
    if sum(row["user_records"] for row in per_core.values()) != retired_uops:
        raise ValueError(f"{path}: per-core user records do not conserve scope total")
    return {
        "path": str(path.resolve()),
        "user_cpi": float(metrics["cpi"]),
        "sum_core_cycles": int(metrics["sum_core_cycles"]),
        "retired_uops": retired_uops,
        "measurement_uops_per_second": float(
            metrics["throughput"]["user_uops_per_second"]
        ),
        "pmu": pmu,
        "per_core": per_core,
    }


def compare_pmu(predicted: dict, reference: dict) -> dict:
    rows = {}
    for name in PMU_FIELDS:
        predicted_value = int(predicted[name])
        reference_value = int(reference[name])
        signed_error = predicted_value - reference_value
        rows[name] = {
            "reference": reference_value,
            "predicted": predicted_value,
            "absolute_error": abs(signed_error),
            "ape_percent": (
                100.0 * abs(signed_error) / reference_value
                if reference_value
                else (0.0 if predicted_value == 0 else None)
            ),
            "signed_error_percent": (
                100.0 * signed_error / reference_value
                if reference_value
                else (0.0 if predicted_value == 0 else None)
            ),
        }
    return rows


def analyze(
    result_dir: Path,
    fastsim_stats_path: Path | None = None,
    analysis_scope: str = "user",
) -> dict:
    sidecar = result_dir / "oracle" / "wrong_path.jsonl"
    stats = result_dir / "stats.txt"
    trace = json.loads((result_dir / "tao_trace" / "trace.json").read_text())
    kernel = json.loads((result_dir / "oracle" / "kernel_events.json").read_text())
    validation = validate(sidecar, stats)
    oracle_schema = validation["metadata"]["schema"]
    if analysis_scope == "user" and oracle_schema not in {
        "taotrace-wrong-path-oracle-v2",
        "taotrace-wrong-path-oracle-v3",
    }:
        raise ValueError(
            f"{sidecar}: user attribution requires v2/v3 decoded-CPL fields; "
            "v1 mixes user and kernel squashes"
        )
    widths = load_widths(result_dir / "config.json")
    ticks_per_cycle = load_clock_ticks(result_dir / "config.ini")

    records_by_core = {
        int(core): int(row["measurement_records"])
        for core, row in trace["functional_boundaries"].items()
    }
    kernel_by_core = {int(row["core_id"]): row for row in kernel["per_core"]}
    episodes: dict[int, dict] = {}
    episode_cpl_counts = Counter()
    depth_values = defaultdict(lambda: defaultdict(list))
    totals_by_core = defaultdict(Counter)
    scoped_by_cause = defaultdict(Counter)
    memory_footprint_by_cause = defaultdict(new_memory_footprint)
    late_memory_footprint_by_cause = defaultdict(new_memory_footprint)
    late_completion_delay_cycles = defaultdict(list)

    with sidecar.open(encoding="utf-8") as handle:
        for text in handle:
            row = json.loads(text)
            kind = row.get("record")
            if kind == "episode":
                episode_id = int(row["episode_id"])
                episode = {
                    "core_id": int(row["core_id"]),
                    "cause": str(row["cause"]),
                    "cause_cpl": int(row.get("cause_cpl", 255)),
                    "included": (
                        analysis_scope == "all" or int(row.get("cause_cpl", 255)) == 3
                    ),
                    "squash_tick": int(row["squash_tick"]),
                    "first_stage_tick": 0,
                    "counts": Counter(),
                }
                episodes[episode_id] = episode
                episode_cpl_counts[str(episode["cause_cpl"])] += 1
            elif kind == "instruction":
                episode = episodes[int(row["episode_id"])]
                instruction_in_scope = episode["included"] and (
                    analysis_scope == "all" or int(row.get("cpl", 255)) == 3
                )
                if not instruction_in_scope:
                    continue
                counts = episode["counts"]
                counts["instruction_records"] += 1
                counts["source_registers"] += int(row.get("n_src", 0))
                counts["destination_registers"] += int(row.get("n_dst", 0))
                if int(row.get("renamed", 0)):
                    counts["renamed_source_registers"] += int(
                        row.get("n_src", 0)
                    )
                    counts["renamed_destination_registers"] += int(
                        row.get("n_dst", 0)
                    )
                for flag, field in FLAG_TO_DEPTH.items():
                    counts[field] += int(row.get(flag, 0))
                is_load = int(row.get("is_load", 0))
                is_store = int(row.get("is_store", 0))
                is_atomic = int(row.get("is_atomic", 0))
                counts["load_instructions"] += is_load
                counts["store_instructions"] += is_store
                counts["memory_instructions"] += int(
                    bool(is_load or is_store or is_atomic)
                )
                if int(row.get("renamed", 0)):
                    counts["renamed_memory_instructions"] += int(
                        bool(is_load or is_store or is_atomic)
                    )
                if is_load or is_store or is_atomic:
                    add_memory_footprint(
                        memory_footprint_by_cause[episode["cause"]], row
                    )
                ticks = [
                    int(row.get(field, 0))
                    for field in (
                        "fetch_tick",
                        "rename_tick",
                        "dispatch_tick",
                        "execute_probe_tick",
                        "to_commit_probe_tick",
                        "data_complete_tick",
                    )
                    if int(row.get(field, 0)) > 0
                ]
                if ticks:
                    first = min(ticks)
                    if not episode["first_stage_tick"] or first < episode["first_stage_tick"]:
                        episode["first_stage_tick"] = first
            elif kind == "late_data_complete":
                episode = episodes[int(row["episode_id"])]
                instruction_in_scope = episode["included"] and (
                    analysis_scope == "all" or int(row.get("cpl", 255)) == 3
                )
                if not instruction_in_scope:
                    continue
                completed_row = dict(row)
                completed_row["data_complete_seen"] = 1
                add_memory_footprint(
                    late_memory_footprint_by_cause[episode["cause"]],
                    completed_row,
                )
                delay = (
                    int(row["data_complete_tick"]) - int(row["squash_tick"])
                ) / ticks_per_cycle
                late_completion_delay_cycles[episode["cause"]].append(delay)

    intervals_by_core = defaultdict(list)
    span_values = defaultdict(list)
    for episode in episodes.values():
        if not episode["included"]:
            continue
        counts = episode["counts"]
        core_counts = totals_by_core[episode["core_id"]]
        core_counts["episodes"] += 1
        core_counts[f"episodes_{episode['cause']}"] += 1
        scoped_by_cause[episode["cause"]]["episodes"] += 1
        for field in SCOPED_COUNT_FIELDS:
            scoped_by_cause[episode["cause"]][field] += int(counts[field])
        for field in DEPTH_FIELDS:
            value = int(counts[field])
            depth_values[episode["cause"]][field].append(float(value))
            core_counts[field] += value
        start = int(episode["first_stage_tick"])
        stop = int(episode["squash_tick"])
        if start and stop >= start:
            intervals_by_core[episode["core_id"]].append((start, stop))
            span_values[episode["cause"]].append((stop - start) / ticks_per_cycle)

    per_core = []
    for core_id in sorted(records_by_core):
        counts = totals_by_core[core_id]
        n_user = records_by_core[core_id]
        union_cycles = merge_intervals(intervals_by_core[core_id]) / ticks_per_cycle
        user_cycles = float(kernel_by_core[core_id]["user_cycles"])
        per_core.append(
            {
                "core_id": core_id,
                "n_user": n_user,
                "episodes": counts["episodes"],
                "branch_episodes": counts["episodes_branch_mispredict"],
                "fetched_instructions": counts["fetched_instructions"],
                "issued_instructions": counts["issued_instructions"],
                "wrong_path_active_union_cycles": union_cycles,
                "active_union_percent_of_user_cycles": 100.0 * union_cycles / user_cycles if user_cycles else 0.0,
                "active_window_cpi_ceiling": union_cycles / n_user if n_user else 0.0,
                "issue_capacity_cpi_equivalent": counts["issued_instructions"] / (widths["issue"] * n_user) if n_user else 0.0,
            }
        )

    total_user_records = sum(records_by_core.values())
    aggregate_active_union_cycles = sum(
        float(row["wrong_path_active_union_cycles"]) for row in per_core
    )
    aggregate_active_window_cpi_ceiling = (
        aggregate_active_union_cycles / total_user_records
        if total_user_records
        else 0.0
    )
    aggregate = Counter()
    for counts in totals_by_core.values():
        for field in DEPTH_FIELDS:
            aggregate[field] += counts[field]
    aggregate_memory_footprint = new_memory_footprint()
    for footprint in memory_footprint_by_cause.values():
        merge_memory_footprint(aggregate_memory_footprint, footprint)
    aggregate_late_memory_footprint = new_memory_footprint()
    for footprint in late_memory_footprint_by_cause.values():
        merge_memory_footprint(aggregate_late_memory_footprint, footprint)
    pmu_user = kernel["aggregate"]["pmu_user"]
    pmu_user_plus_kernel = kernel["aggregate"]["pmu_user_plus_kernel"]
    branch_episodes = sum(
        counts["episodes_branch_mispredict"] for counts in totals_by_core.values()
    )
    capacity = {
        "fetch_cpi_equivalent": aggregate["fetched_instructions"] / (widths["fetch"] * total_user_records),
        "rename_cpi_equivalent": aggregate["renamed_instructions"] / (widths["rename"] * total_user_records),
        "dispatch_cpi_equivalent": aggregate["dispatched_instructions"] / (widths["dispatch"] * total_user_records),
        "issue_cpi_equivalent": aggregate["issued_instructions"] / (widths["issue"] * total_user_records),
    }
    result = {
        "schema": "fastsim-wrong-path-attribution-v3",
        "result_dir": str(result_dir.resolve()),
        "oracle_only": True,
        "fst_input": False,
        "analysis_scope": analysis_scope,
        "episode_cpl_counts_all": dict(sorted(episode_cpl_counts.items())),
        "scoped_by_cause": {
            cause: dict(counts)
            for cause, counts in sorted(scoped_by_cause.items())
        },
        "memory_footprint": {
            "aggregate": summarize_memory_footprint(
                aggregate_memory_footprint, total_user_records
            ),
            "by_cause": {
                cause: summarize_memory_footprint(footprint, total_user_records)
                for cause, footprint in sorted(memory_footprint_by_cause.items())
            },
            "late_after_squash": {
                "aggregate": summarize_memory_footprint(
                    aggregate_late_memory_footprint, total_user_records
                ),
                "by_cause": {
                    cause: summarize_memory_footprint(
                        footprint, total_user_records
                    )
                    for cause, footprint in sorted(
                        late_memory_footprint_by_cause.items()
                    )
                },
                "completion_delay_cycles_by_cause": {
                    cause: distribution(values)
                    for cause, values in sorted(
                        late_completion_delay_cycles.items()
                    )
                },
            },
            "phase_transition": summarize_memory_phase_transition(
                aggregate_memory_footprint,
                aggregate_late_memory_footprint,
            ),
            "note": (
                "Oracle-only unique address coverage. Completed means the "
                "DataAccessComplete probe was observed before squash. Schema "
                "v3 separately reports callbacks observed after squash. "
                "Neither proves cache/TLB pollution or quantifies CPI impact."
            ),
        },
        "validation": validation,
        "ticks_per_cycle": ticks_per_cycle,
        "pipeline_widths": widths,
        "total_user_records": total_user_records,
        "depth_distribution_by_cause": {
            cause: {field: distribution(values) for field, values in fields.items()}
            for cause, fields in sorted(depth_values.items())
        },
        "active_span_cycles_by_cause": {
            cause: distribution(values) for cause, values in sorted(span_values.items())
        },
        "capacity_cpi_equivalent": capacity,
        "active_window_exposure": {
            "union_cycles": aggregate_active_union_cycles,
            "cpi_ceiling": aggregate_active_window_cpi_ceiling,
        },
        "branch_redirect_comparison": {
            "accepted_branch_squash_episodes": branch_episodes,
            "retired_user_branch_misses": int(pmu_user["branch_misses"]),
            "retired_user_plus_kernel_branch_misses": int(pmu_user_plus_kernel["branch_misses"]),
            "accepted_minus_matching_retired_scope": branch_episodes - int(
                pmu_user["branch_misses"]
                if analysis_scope == "user"
                else pmu_user_plus_kernel["branch_misses"]
            ),
            "note": "CPL filtering matches the requested scope. Accepted redirects may still be caused by instructions later squashed by an older redirect; retired PMU misses cannot include those nested wrong-path branches.",
        },
        "per_core": per_core,
        "interpretation": {
            "capacity_cpi_equivalent": "Minimum full-width stage cycles occupied by wrong-path work divided by user records; it is not an additive CPI correction.",
            "active_window_cpi_ceiling": "Union of intervals from the earliest observed wrong-path stage to squash divided by user records; useful older-path work overlaps these intervals, so it is only an interference window ceiling.",
            "memory_footprint": "Unique wrong-path virtual/physical cache lines and pages observed by the oracle. The completed subset reached DataAccessComplete before squash; it is an exposure diagnostic, not a production feature or CPI correction.",
        },
    }
    if fastsim_stats_path is None and analysis_scope == "user":
        candidate = result_dir / "fastsim-baseline.json"
        if candidate.is_file():
            fastsim_stats_path = candidate
    if fastsim_stats_path is not None:
        if analysis_scope != "user":
            raise ValueError("same-window FastSim CPI comparison requires --scope user")
        fastsim = load_fastsim_user_result(
            fastsim_stats_path, total_user_records
        )
        gem5_user_cpi = float(kernel["aggregate"]["cpi_user"])
        gap = gem5_user_cpi - fastsim["user_cpi"]
        diagnostic_cpi = (
            fastsim["user_cpi"] + aggregate_active_window_cpi_ceiling
        )
        result["same_window_cpi"] = {
            "gem5_user_cpi": gem5_user_cpi,
            "fastsim_user_cpi": fastsim["user_cpi"],
            "fastsim_ape_percent": (
                100.0 * abs(gap) / gem5_user_cpi if gem5_user_cpi else 0.0
            ),
            "gem5_minus_fastsim_cpi": gap,
            "active_window_ceiling_fraction_of_gap_percent": (
                100.0 * aggregate_active_window_cpi_ceiling / gap
                if gap > 0.0
                else 0.0
            ),
            "diagnostic_fastsim_plus_active_ceiling_cpi": diagnostic_cpi,
            "diagnostic_fastsim_plus_active_ceiling_ape_percent": (
                100.0 * abs(diagnostic_cpi - gem5_user_cpi) / gem5_user_cpi
                if gem5_user_cpi
                else 0.0
            ),
            "fastsim": fastsim,
            "note": (
                "The plus-ceiling value is a deliberately pessimistic "
                "same-window attribution bound, not a replay result or a "
                "valid CPI correction. It excludes persistent cache/TLB and "
                "memory-system effects after squash."
            ),
        }
        per_core_exposure = {int(row["core_id"]): row for row in per_core}
        per_core_comparison = []
        for core_id in sorted(records_by_core):
            oracle = kernel_by_core[core_id]
            predicted = fastsim["per_core"][core_id]
            exposure = per_core_exposure[core_id]
            reference_cpi = float(oracle["user_cycles"]) / int(oracle["n_user"])
            core_gap = reference_cpi - float(predicted["user_cpi"])
            per_core_comparison.append(
                {
                    "core_id": core_id,
                    "reference_user_cpi": reference_cpi,
                    "predicted_user_cpi": float(predicted["user_cpi"]),
                    "gem5_minus_fastsim_cpi": core_gap,
                    "active_window_cpi_ceiling": float(
                        exposure["active_window_cpi_ceiling"]
                    ),
                    "branch_episodes": int(exposure["branch_episodes"]),
                }
            )
        result["same_window_cpi"]["per_core"] = per_core_comparison
        result["same_window_cpi"][
            "per_core_gap_vs_active_ceiling_pearson"
        ] = pearson(
            [row["gem5_minus_fastsim_cpi"] for row in per_core_comparison],
            [row["active_window_cpi_ceiling"] for row in per_core_comparison],
        )
        result["same_window_pmu"] = {
            "scope": "user",
            "counters": compare_pmu(fastsim["pmu"], pmu_user),
            "note": (
                "Single-case same-window PMU diagnostics from scope_metrics. "
                "Formal MAPE/percentiles/WAPE require a multi-case report."
            ),
        }
    return result


def write_markdown(result: dict, path: Path) -> None:
    branch = result["depth_distribution_by_cause"].get("branch_mispredict", {})
    redirect = result["branch_redirect_comparison"]
    capacity = result["capacity_cpi_equivalent"]
    active = result["active_window_exposure"]
    fetch = branch.get("fetched_instructions", {})
    issue = branch.get("issued_instructions", {})
    footprint = result["memory_footprint"]["aggregate"]
    late_footprint = result["memory_footprint"]["late_after_squash"][
        "aggregate"
    ]
    phase_transition = result["memory_footprint"]["phase_transition"]
    lines = [
        "# Wrong-path attribution",
        "",
        "This report is **oracle-only**. None of these fields are FastSim/FST inputs.",
        f"Analysis scope: **{result['analysis_scope']}**.",
        "",
        "## Aggregate branch episodes",
        "",
        f"- Accepted branch squash episodes: {redirect['accepted_branch_squash_episodes']:,}",
        f"- Retired user branch misses: {redirect['retired_user_branch_misses']:,}",
        f"- Retired user+kernel branch misses: {redirect['retired_user_plus_kernel_branch_misses']:,}",
        f"- Accepted minus matching retired scope: {redirect['accepted_minus_matching_retired_scope']:,}",
        f"- Wrong-path fetch depth mean/P50/P90/P99/max: {fetch.get('mean', 0):.3f} / {fetch.get('p50', 0):.3f} / {fetch.get('p90', 0):.3f} / {fetch.get('p99', 0):.3f} / {fetch.get('max', 0):.3f}",
        f"- Wrong-path issue depth mean/P50/P90/P99/max: {issue.get('mean', 0):.3f} / {issue.get('p50', 0):.3f} / {issue.get('p90', 0):.3f} / {issue.get('p99', 0):.3f} / {issue.get('max', 0):.3f}",
        "",
        "## Capacity exposure",
        "",
        f"- Fetch capacity CPI-equivalent: {capacity['fetch_cpi_equivalent']:.6f}",
        f"- Rename capacity CPI-equivalent: {capacity['rename_cpi_equivalent']:.6f}",
        f"- Dispatch capacity CPI-equivalent: {capacity['dispatch_cpi_equivalent']:.6f}",
        f"- Issue capacity CPI-equivalent: {capacity['issue_cpi_equivalent']:.6f}",
        f"- Aggregate active-window CPI ceiling: {active['cpi_ceiling']:.6f}",
        "",
        "These are exposure bounds, not additive CPI corrections.",
        "",
        "## Wrong-path memory footprint",
        "",
        f"- Memory/data-completed records: {footprint.get('memory_instruction_records', 0):,} / {footprint.get('data_completed_records', 0):,} ({footprint['data_completed_percent_of_memory']:.3f}%)",
        f"- Completed records with physical/virtual address: {footprint.get('data_completed_physical_address_records', 0):,} / {footprint.get('data_completed_virtual_address_records', 0):,}",
        f"- Completed unique physical cache lines/pages: {footprint['completed_unique_physical_cache_lines']:,} / {footprint['completed_unique_physical_pages']:,}",
        f"- Completed unique virtual cache lines/pages: {footprint['completed_unique_virtual_cache_lines']:,} / {footprint['completed_unique_virtual_pages']:,}",
        f"- Completed physical records per unique cache line: {footprint['data_completed_physical_records_per_unique_cache_line']:.3f}",
        f"- Executed memory records still incomplete at squash: {footprint.get('executed_inflight_at_squash_records', 0):,}",
        f"- Late data completions observed after squash: {late_footprint.get('data_completed_records', 0):,}",
        f"- Late-completion unique physical cache lines/pages: {late_footprint['completed_unique_physical_cache_lines']:,} / {late_footprint['completed_unique_physical_pages']:,}",
        f"- Late completion/candidate rate: {phase_transition['observed_late_completion_percent_of_candidates']:.3f}%",
        f"- Late physical cache lines overlapping/novel versus pre-squash completion: {phase_transition['late_overlapping_pre_squash_physical_cache_lines']:,} / {phase_transition['late_novel_physical_cache_lines']:,}",
        "",
        "These address fields are oracle-only. Completion before squash shows exposure, not persistent-state CPI causality.",
    ]
    same_window = result.get("same_window_cpi")
    if same_window:
        lines.extend(
            [
                "",
                "## Same-window CPI context",
                "",
                f"- gem5 user CPI: {same_window['gem5_user_cpi']:.6f}",
                f"- FastSim user CPI: {same_window['fastsim_user_cpi']:.6f}",
                f"- FastSim user CPI APE: {same_window['fastsim_ape_percent']:.3f}%",
                f"- gem5 minus FastSim: {same_window['gem5_minus_fastsim_cpi']:.6f} CPI",
                f"- Active-window ceiling/gap: {same_window['active_window_ceiling_fraction_of_gap_percent']:.3f}%",
                f"- Diagnostic FastSim + ceiling CPI/APE: {same_window['diagnostic_fastsim_plus_active_ceiling_cpi']:.6f} / {same_window['diagnostic_fastsim_plus_active_ceiling_ape_percent']:.3f}%",
                "",
                "The last line is not a replay result or a proposed correction; it is a deliberately pessimistic direct-interference bound.",
            ]
        )
    same_window_pmu = result.get("same_window_pmu")
    if same_window_pmu:
        pmu = same_window_pmu["counters"]
        lines.extend(
            [
                "",
                "## Same-window user PMU context",
                "",
                "| Counter | gem5 | FastSim | signed error | APE |",
                "|---|---:|---:|---:|---:|",
            ]
        )
        for name in (
            "branch_misses",
            "l1d_misses",
            "l2_misses",
            "llc_misses",
            "dtlb_misses",
        ):
            row = pmu[name]
            signed = row["signed_error_percent"]
            ape = row["ape_percent"]
            signed_text = f"{signed:.3f}%" if signed is not None else "undefined"
            ape_text = f"{ape:.3f}%" if ape is not None else "undefined"
            lines.append(
                f"| {name} | {row['reference']:,} | {row['predicted']:,} | "
                f"{signed_text} | {ape_text} |"
            )
        lines.extend(
            [
                "",
                "This is a single-case diagnostic; formal PMU distribution statistics require the full matrix.",
            ]
        )
    lines.extend(
        [
            "",
            "## Per core",
            "",
            "| Core | Episodes | Branch episodes | Wrong fetch | Wrong issue | Active union cycles | Active/user cycles | Active-window CPI ceiling | Issue capacity CPI-eq |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in result["per_core"]:
        lines.append(
            f"| {row['core_id']} | {row['episodes']:,} | {row['branch_episodes']:,} | "
            f"{row['fetched_instructions']:,} | {row['issued_instructions']:,} | "
            f"{row['wrong_path_active_union_cycles']:.3f} | "
            f"{row['active_union_percent_of_user_cycles']:.3f}% | "
            f"{row['active_window_cpi_ceiling']:.6f} | "
            f"{row['issue_capacity_cpi_equivalent']:.6f} |"
        )
    path.write_text("\n".join(lines) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("result_dir", type=Path)
    parser.add_argument(
        "--fastsim-stats",
        type=Path,
        help=(
            "optional same-window user-only FastSim JSON; defaults to "
            "RESULT/fastsim-baseline.json when present"
        ),
    )
    parser.add_argument(
        "--scope",
        choices=("user", "all"),
        default="user",
        help="CPL scope for wrong-path attribution; user requires oracle v2",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()
    result = analyze(args.result_dir, args.fastsim_stats, args.scope)
    encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(encoded)
    else:
        print(encoded, end="")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(result, args.markdown_out)


if __name__ == "__main__":
    main()
