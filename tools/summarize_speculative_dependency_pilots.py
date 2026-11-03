#!/usr/bin/env python3
"""Summarize audit-only speculative dependency pilots against CPL-safe oracles."""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any


GEM5_COUNTER = re.compile(
    r"^board\.processor\.switch\d+\.core\."
    r"(?P<name>commit\.commitSquashedInsts|iew\.iqFullEvents|"
    r"lsq0\.squashedLoads|lsq0\.squashedStores|squashedInstsIssued|"
    r"rename\.undoneMaps)\s+(?P<value>\d+)\b"
)
GEM5_NAMES = {
    "commit.commitSquashedInsts": "gem5_commit_squashed_instructions",
    "iew.iqFullEvents": "gem5_iq_full_events",
    "lsq0.squashedLoads": "gem5_squashed_loads",
    "lsq0.squashedStores": "gem5_squashed_stores",
    "squashedInstsIssued": "gem5_squashed_instructions_issued",
    "rename.undoneMaps": "gem5_rename_undone_maps",
}


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def gem5_counters(path: Path) -> dict[str, int]:
    totals = {output: 0 for output in GEM5_NAMES.values()}
    matches = {output: 0 for output in GEM5_NAMES.values()}
    with path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            match = GEM5_COUNTER.match(line)
            if match is None:
                continue
            output = GEM5_NAMES[match.group("name")]
            totals[output] += int(match.group("value"))
            matches[output] += 1
    missing = sorted(name for name, count in matches.items() if count == 0)
    if missing:
        raise ValueError(f"{path}: missing gem5 counters {missing}")
    totals["gem5_squashed_memory_instructions"] = (
        totals["gem5_squashed_loads"] + totals["gem5_squashed_stores"]
    )
    return totals


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def require_counter(counters: dict[str, Any], name: str) -> int:
    if name not in counters:
        raise ValueError(f"FastSim report lacks required counter {name}")
    return int(counters[name])


def oracle_scope(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "oracle": None,
            "oracle_user_branch_misses": None,
            "oracle_kernel_branch_misses": None,
            "oracle_kernel_retired_uops": None,
            "oracle_kernel_event_counts": None,
            "resource_scope_comparable": False,
        }
    oracle = load_json(path)
    aggregate = oracle.get("aggregate")
    if not isinstance(aggregate, dict):
        raise ValueError(f"{path}: missing aggregate oracle scope")
    user_pmu = aggregate.get("pmu_user")
    combined_pmu = aggregate.get("pmu_user_plus_kernel")
    if not isinstance(user_pmu, dict) or not isinstance(combined_pmu, dict):
        raise ValueError(f"{path}: missing dual-scope PMU oracle")
    user_misses = int(user_pmu["branch_misses"])
    combined_misses = int(combined_pmu["branch_misses"])
    if combined_misses < user_misses:
        raise ValueError(f"{path}: kernel branch-miss count is negative")
    kernel_misses = combined_misses - user_misses
    user_uops = int(user_pmu["retired_uops"])
    combined_uops = int(combined_pmu["retired_uops"])
    if combined_uops < user_uops:
        raise ValueError(f"{path}: kernel retired-UOP count is negative")
    kernel_uops = combined_uops - user_uops
    return {
        "oracle": str(path.resolve()),
        "oracle_user_branch_misses": user_misses,
        "oracle_kernel_branch_misses": kernel_misses,
        "oracle_kernel_retired_uops": kernel_uops,
        "oracle_kernel_event_counts": aggregate.get("event_counts"),
        # gem5's squash/rename/IQ stats are not CPL-filtered.  They are a
        # user-wrong-path reference only when the CPL-class oracle proves the
        # measured interval retired no kernel work at all.
        "resource_scope_comparable": kernel_uops == 0,
    }


def wrong_path_scope(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    attribution = load_json(path)
    if (attribution.get("oracle_only") is not True or
            attribution.get("fst_input") is not False or
            attribution.get("analysis_scope") != "user"):
        raise ValueError(
            f"{path}: expected oracle-only, user-scope wrong-path attribution"
        )
    branch = attribution.get("scoped_by_cause", {}).get(
        "branch_mispredict", {}
    )
    required = (
        "episodes", "renamed_instructions", "renamed_memory_instructions",
        "renamed_destination_registers",
    )
    missing = [name for name in required if name not in branch]
    if missing:
        raise ValueError(f"{path}: missing branch attribution fields {missing}")
    return {
        "wrong_path_attribution": str(path.resolve()),
        "wrong_path_attribution_scope": "user-branch-mispredict",
        "oracle_user_branch_episodes": int(branch["episodes"]),
        "oracle_user_wrong_path_renamed_uops": int(
            branch["renamed_instructions"]
        ),
        "oracle_user_wrong_path_renamed_memory_uops": int(
            branch["renamed_memory_instructions"]
        ),
        "oracle_user_wrong_path_renamed_destination_operands": int(
            branch["renamed_destination_registers"]
        ),
    }


def summarize_case(
    label: str, report_path: Path, stats_path: Path,
    oracle_path: Path | None, wrong_path_path: Path | None,
) -> dict[str, Any]:
    report = load_json(report_path)
    counters = report["totals"]
    scope = report["scope_metrics"]
    gem5 = gem5_counters(stats_path)
    oracle = oracle_scope(oracle_path)
    exact_wrong_path = wrong_path_scope(wrong_path_path)
    operand_instructions = require_counter(
        counters, "l1i_speculative_path_operand_instructions"
    )
    reads = require_counter(counters, "l1i_speculative_path_read_registers")
    writes = require_counter(counters, "l1i_speculative_path_write_registers")
    segments = require_counter(counters, "l1i_speculative_path_operand_segments")
    raw_edges = require_counter(counters, "l1i_speculative_path_raw_edges")
    dependent = require_counter(
        counters, "l1i_speculative_path_dependent_instructions"
    )
    depth_sum = require_counter(
        counters, "l1i_speculative_path_chain_depth_sum"
    )
    depth_max = require_counter(
        counters, "l1i_speculative_path_chain_depth_max"
    )
    rob_instructions = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_instructions",
    )
    rob_reads = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_read_registers",
    )
    rob_writes = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_write_registers",
    )
    rob_memory = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_memory_instructions",
    )
    rob_raw_edges = require_counter(
        counters, "l1i_speculative_path_operand_rob_capped_raw_edges"
    )
    rob_dependent = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_dependent_instructions",
    )
    rob_depth_sum = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_chain_depth_sum",
    )
    rob_depth_max = require_counter(
        counters,
        "l1i_speculative_path_operand_rob_capped_chain_depth_max",
    )
    branch_misses = int(scope["pmu"]["branch_misses"])
    static_memory = require_counter(
        counters, "l1i_speculative_path_memory_instructions"
    )
    invariants = {
        "segments_not_above_branch_misses": segments <= branch_misses,
        "raw_edges_not_above_reads": raw_edges <= reads,
        "dependent_not_above_instructions": dependent <= operand_instructions,
        "depth_conserved": depth_sum >= operand_instructions,
        "rob_instructions_not_above_all": rob_instructions <= operand_instructions,
        "rob_reads_not_above_all": rob_reads <= reads,
        "rob_writes_not_above_all": rob_writes <= writes,
        "rob_memory_not_above_all": rob_memory <= static_memory,
        "rob_raw_edges_not_above_all": rob_raw_edges <= raw_edges,
        "rob_dependent_not_above_all": rob_dependent <= dependent,
        "rob_depth_conserved": rob_depth_sum >= rob_instructions,
        "rob_depth_max_not_above_all": rob_depth_max <= depth_max,
    }
    if not all(invariants.values()):
        failed = sorted(name for name, valid in invariants.items() if not valid)
        raise ValueError(f"{report_path}: dependency conservation failed {failed}")

    row: dict[str, Any] = {
        "label": label,
        "fastsim_report": str(report_path.resolve()),
        "gem5_stats": str(stats_path.resolve()),
        "user_uops": int(scope["user_trace_uops"]),
        "fastsim_user_cpi": float(scope["cpi"]),
        "fastsim_branch_misses": branch_misses,
        "gem5_counter_scope": "all-cpl-measurement",
        "operand_segments": segments,
        "operand_segment_branch_miss_coverage": ratio(segments, branch_misses),
        "operand_instructions": operand_instructions,
        "operand_reads": reads,
        "operand_writes": writes,
        "raw_edges": raw_edges,
        "dependent_instructions": dependent,
        "dependent_instruction_fraction": ratio(dependent, operand_instructions),
        "raw_read_fraction": ratio(raw_edges, reads),
        "chain_depth_mean": ratio(depth_sum, operand_instructions),
        "chain_depth_max": depth_max,
        "rob_prefix_uops": require_counter(
            counters, "l1i_speculative_path_operand_rob_prefix_uops_q16"
        ) / 65_536.0,
        "rob_capped_instructions": rob_instructions,
        "rob_capped_reads": rob_reads,
        "rob_capped_writes": rob_writes,
        "rob_capped_writes_max_per_path": require_counter(
            counters,
            "l1i_speculative_path_operand_rob_capped_write_registers_max_per_path",
        ),
        "rob_capped_memory_instructions": rob_memory,
        "rob_capped_memory_max_per_path": require_counter(
            counters,
            "l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path",
        ),
        "rob_capped_raw_edges": rob_raw_edges,
        "rob_capped_dependent_instructions": rob_dependent,
        "rob_capped_chain_depth_mean": ratio(rob_depth_sum, rob_instructions),
        "rob_capped_chain_depth_max": rob_depth_max,
        "invariants": invariants,
    }
    row.update(oracle)
    row.update(exact_wrong_path or {
        "wrong_path_attribution": None,
        "wrong_path_attribution_scope": None,
        "oracle_user_branch_episodes": None,
        "oracle_user_wrong_path_renamed_uops": None,
        "oracle_user_wrong_path_renamed_memory_uops": None,
        "oracle_user_wrong_path_renamed_destination_operands": None,
    })
    oracle_user_misses = row["oracle_user_branch_misses"]
    row["fastsim_branch_miss_signed_error"] = (
        branch_misses - oracle_user_misses
        if oracle_user_misses is not None else None
    )
    row["fastsim_branch_miss_ape_percent"] = (
        100.0 * abs(branch_misses - oracle_user_misses) /
        oracle_user_misses
        if oracle_user_misses else None
    )
    row["operand_segment_oracle_user_branch_miss_ratio"] = (
        ratio(segments, oracle_user_misses)
        if oracle_user_misses is not None else None
    )
    row.update(gem5)
    row["macro_to_commit_squashed_ratio"] = ratio(
        rob_instructions, gem5["gem5_commit_squashed_instructions"]
    )
    row["memory_to_gem5_squashed_ratio"] = ratio(
        rob_memory, gem5["gem5_squashed_memory_instructions"]
    )
    row["destination_to_gem5_undone_map_ratio"] = ratio(
        rob_writes, gem5["gem5_rename_undone_maps"]
    )
    row["commit_squashed_scale_required"] = ratio(
        gem5["gem5_commit_squashed_instructions"], rob_instructions
    )
    row["squashed_memory_scale_required"] = ratio(
        gem5["gem5_squashed_memory_instructions"], rob_memory
    )
    row["undone_map_scale_required"] = ratio(
        gem5["gem5_rename_undone_maps"], rob_writes
    )
    scales = [
        row["commit_squashed_scale_required"],
        row["squashed_memory_scale_required"],
        row["undone_map_scale_required"],
    ]
    finite_scales = [value for value in scales if value is not None]
    row["within_case_resource_scale_spread"] = (
        max(finite_scales) / min(finite_scales)
        if finite_scales and min(finite_scales) > 0 else None
    )
    if exact_wrong_path is not None:
        reference_renamed = row["oracle_user_wrong_path_renamed_uops"]
        reference_memory = row["oracle_user_wrong_path_renamed_memory_uops"]
        reference_destinations = row[
            "oracle_user_wrong_path_renamed_destination_operands"
        ]
        modeled_renamed = row["rob_prefix_uops"]
        row["resource_reference_source"] = (
            "cpl3-wrong-path-oracle-branch-mispredict"
        )
        row["resource_scope_comparable"] = True
    else:
        reference_renamed = gem5["gem5_commit_squashed_instructions"]
        reference_memory = gem5["gem5_squashed_memory_instructions"]
        reference_destinations = gem5["gem5_rename_undone_maps"]
        modeled_renamed = rob_instructions
        row["resource_reference_source"] = "all-cpl-gem5-stats"
    row["modeled_to_reference_renamed_ratio"] = ratio(
        modeled_renamed, reference_renamed
    )
    row["modeled_to_reference_memory_ratio"] = ratio(
        rob_memory, reference_memory
    )
    row["modeled_to_reference_destination_ratio"] = ratio(
        rob_writes, reference_destinations
    )
    row["reference_renamed_scale_required"] = ratio(
        reference_renamed, modeled_renamed
    )
    row["reference_memory_scale_required"] = ratio(
        reference_memory, rob_memory
    )
    row["reference_destination_scale_required"] = ratio(
        reference_destinations, rob_writes
    )
    exact_scales = [
        row["reference_renamed_scale_required"],
        row["reference_memory_scale_required"],
        row["reference_destination_scale_required"],
    ]
    finite_exact_scales = [value for value in exact_scales if value is not None]
    row["reference_resource_scale_spread"] = (
        max(finite_exact_scales) / min(finite_exact_scales)
        if finite_exact_scales and min(finite_exact_scales) > 0 else None
    )
    return row


def scale_range(rows: list[dict[str, Any]], field: str) -> dict[str, float] | None:
    values = [
        float(row[field]) for row in rows
        if row[field] is not None and row["resource_scope_comparable"]
    ]
    if not values:
        return None
    return {
        "minimum": min(values),
        "maximum": max(values),
        "max_to_min": max(values) / min(values) if min(values) > 0 else 0.0,
    }


def write_outputs(summary: dict[str, Any], prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    csv_rows = []
    for row in summary["rows"]:
        csv_rows.append({key: value for key, value in row.items() if key != "invariants"})
    with prefix.with_suffix(".csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(csv_rows[0]))
        writer.writeheader()
        writer.writerows(csv_rows)
    with prefix.with_suffix(".md").open("w", encoding="utf-8") as output:
        output.write("# Speculative dependency pilot audit\n\n")
        output.write(
            "All FastSim fields below are audit-only. The ratios compare "
            "related but not identical definitions and must not be used as "
            "cycle-correction coefficients.\n\n"
        )
        output.write(
            "gem5 squash/rename/IQ counters cover all CPLs. Resource ratios "
            "are suppressed unless either a CPL3 wrong-path attribution is "
            "provided or the CPL-class oracle reports zero retired kernel "
            "UOPs in the measured interval.\n\n"
        )
        output.write(
            "| Case | FastSim/oracle user branch misses | Operand path/"
            "FastSim miss | Kernel branch misses | Dependency % | "
            "Mean/max depth | Resource comparison |\n"
        )
        output.write("|---|---:|---:|---:|---:|---:|---|\n")
        for row in summary["rows"]:
            oracle_misses = row["oracle_user_branch_misses"]
            branch_pair = (
                f"{row['fastsim_branch_misses']}/{oracle_misses}"
                if oracle_misses is not None else
                f"{row['fastsim_branch_misses']}/unknown"
            )
            kernel_misses = row["oracle_kernel_branch_misses"]
            kernel_text = str(kernel_misses) if kernel_misses is not None else "unknown"
            if row["resource_scope_comparable"]:
                resource = (
                    f"rename {row['modeled_to_reference_renamed_ratio']:.3f}x; "
                    f"mem {row['modeled_to_reference_memory_ratio']:.3f}x; "
                    f"dst {row['modeled_to_reference_destination_ratio']:.3f}x; "
                    f"spread {row['reference_resource_scale_spread']:.3f}x "
                    f"({row['resource_reference_source']})"
                )
            else:
                resource = "scope mismatch (all-CPL gem5)"
            output.write(
                f"| {row['label']} | "
                f"{branch_pair} | "
                f"{100.0 * row['operand_segment_branch_miss_coverage']:.2f}% | "
                f"{kernel_text} | "
                f"{100.0 * row['dependent_instruction_fraction']:.2f}% | "
                f"{row['chain_depth_mean']:.2f}/{row['chain_depth_max']} | "
                f"{resource} |\n"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", action="append", nargs="+", metavar="FIELD", required=True,
        help=("LABEL FASTSIM_JSON GEM5_STATS [KERNEL_EVENTS_ORACLE]; the "
              "optional fifth field is USER_WRONG_PATH_ATTRIBUTION_JSON"),
    )
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()
    rows = []
    for fields in args.case:
        if len(fields) not in (3, 4, 5):
            parser.error("each --case needs 3, 4, or 5 fields")
        label, report, stats = fields[:3]
        oracle = Path(fields[3]) if len(fields) == 4 else None
        if len(fields) == 5:
            oracle = Path(fields[3])
        wrong_path = Path(fields[4]) if len(fields) == 5 else None
        rows.append(summarize_case(
            label, Path(report), Path(stats), oracle, wrong_path
        ))
    summary = {
        "schema": "fastsim-speculative-dependency-pilots-v2",
        "timing_effect": False,
        "gem5_counter_scope": "all-cpl-measurement",
        "resource_scale_ranges_include_only_cpl_safe_cases": True,
        "case_count": len(rows),
        "required_scale_ranges": {
            "renamed": scale_range(
                rows, "reference_renamed_scale_required"
            ),
            "memory": scale_range(
                rows, "reference_memory_scale_required"
            ),
            "destination_operands": scale_range(
                rows, "reference_destination_scale_required"
            ),
        },
        "rows": rows,
    }
    write_outputs(summary, args.output_prefix.resolve())


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise SystemExit(f"error: {error}") from error
