#!/usr/bin/env python3
"""Summarize a C8 static speculative-path audit against formal/oracle data.

This tool deliberately keeps three domains separate:

* FastSim's architectural user PMU (committed functional records),
* FastSim's state-only speculative diagnostics, and
* gem5's raw timing-DTLB activity (which includes non-retired requests).

It does not fit a timing coefficient or change a simulation result.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any


DTLB_MISS_RE = re.compile(
    r"^board\.processor\.switch\d+\.core\.mmu\.dtb\."
    r"(?:rdMisses|wrMisses)\s+(\d+)\b"
)
GEM5_WRONG_PATH_RE = re.compile(
    r"^board\.processor\.switch\d+\.core\."
    r"(?P<name>commit\.commitSquashedInsts|iew\.iqFullEvents|"
    r"lsq0\.squashedLoads|lsq0\.squashedStores|squashedInstsIssued|"
    r"rename\.undoneMaps)\s+(?P<value>\d+)\b"
)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def percentile_type7(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile of an empty list")
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def raw_gem5_stats(stats_path: Path) -> dict[str, int]:
    totals = {
        "timing_dtlb_misses": 0,
        "commit_squashed_insts": 0,
        "iq_full_events": 0,
        "squashed_loads": 0,
        "squashed_stores": 0,
        "squashed_insts_issued": 0,
        "rename_undone_maps": 0,
    }
    dtlb_matches = 0
    wrong_path_matches: dict[str, int] = {}
    wrong_path_names = {
        "commit.commitSquashedInsts": "commit_squashed_insts",
        "iew.iqFullEvents": "iq_full_events",
        "lsq0.squashedLoads": "squashed_loads",
        "lsq0.squashedStores": "squashed_stores",
        "squashedInstsIssued": "squashed_insts_issued",
        "rename.undoneMaps": "rename_undone_maps",
    }
    with stats_path.open(encoding="utf-8", errors="replace") as source:
        for line in source:
            match = DTLB_MISS_RE.match(line)
            if match is not None:
                totals["timing_dtlb_misses"] += int(match.group(1))
                dtlb_matches += 1
            match = GEM5_WRONG_PATH_RE.match(line)
            if match is not None:
                output_name = wrong_path_names[match.group("name")]
                totals[output_name] += int(match.group("value"))
                wrong_path_matches[output_name] = (
                    wrong_path_matches.get(output_name, 0) + 1
                )
    if dtlb_matches == 0:
        raise ValueError(f"no switch-core timing DTLB miss counters in {stats_path}")
    missing = sorted(set(wrong_path_names.values()) - set(wrong_path_matches))
    if missing:
        raise ValueError(f"missing gem5 wrong-path counters {missing} in {stats_path}")
    return totals


def ratio_percent(numerator: int, denominator: int) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def find_one(paths: list[Path], description: str) -> Path:
    if len(paths) != 1:
        raise ValueError(
            f"expected exactly one {description}, found {len(paths)}: {paths}"
        )
    return paths[0]


def workload_label(case_name: str) -> str:
    return case_name.removeprefix("08c-")


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    audit = args.audit.resolve()
    formal = args.formal.resolve()
    reports = sorted(audit.glob(f"*/{args.report_name}"))
    if not reports:
        raise ValueError(
            f"no */{args.report_name} reports found under {audit}"
        )
    case_prefix = f"{args.cores:02d}c"
    accuracy_split = "calibration-c4" if args.cores == 4 else "held-out-c8"
    source_core_directory = f"{args.cores}c"

    rows: list[dict[str, Any]] = []
    feature_settings: set[tuple[bool, bool, bool]] = set()
    for report_path in reports:
        workload = report_path.parent.name
        report = load_json(report_path)
        counters = report["totals"]
        scope = report["scope_metrics"]
        configuration = report["configuration"]
        feature_settings.add(
            (
                bool(configuration["l1i_enabled"]),
                bool(configuration["l1i_speculative_path_state"]),
                bool(configuration["dtlb"]["speculative_path_state"]),
            )
        )
        accuracy_path = formal / "accuracy" / accuracy_split / "cases" / (
            f"{case_prefix}-{workload}"
        ) / "accuracy.json"
        accuracy = load_json(accuracy_path)
        reference_cycles_per_uop = float(
            accuracy["cycles_per_user_uop"]["user"]["reference"]
        )
        formal_cycles_per_uop = float(
            accuracy["cycles_per_user_uop"]["user"]["predicted"]
        )
        candidate_cycles_per_uop = float(scope["cycles_per_user_uop"])
        n_user = int(accuracy["n_user"])

        stats_path = find_one(
            list(
                (
                    formal
                    / "source"
                    / "sample"
                    / "mesi-three-level-3GiB"
                    / source_core_directory
                    / workload
                ).glob("*/*/stats.txt")
            ),
            f"gem5 stats.txt for {workload}",
        )
        gem5_stats = raw_gem5_stats(stats_path)
        raw_dtlb_misses = gem5_stats["timing_dtlb_misses"]
        retired_dtlb_misses = int(
            accuracy["pmu"]["user"]["dtlb_misses"]["reference"]
        )
        formal_dtlb_misses = int(
            accuracy["pmu"]["user"]["dtlb_misses"]["predicted"]
        )
        path_memory = int(
            counters["l1i_speculative_path_memory_instructions"]
        )
        page_known = int(counters["l1i_speculative_path_memory_page_known"])
        page_unstable = int(
            counters["l1i_speculative_path_memory_page_unstable"]
        )
        transition_samples = int(
            counters["l1i_speculative_path_memory_page_transition_samples"]
        )
        transition_score_ppm = int(
            counters[
                "l1i_speculative_path_memory_page_transition_score_ppm"
            ]
        )
        raw_excess = max(0, raw_dtlb_misses - retired_dtlb_misses)
        profile_instructions = int(
            counters.get("l1i_speculative_path_profiled_instructions", 0)
        )
        profile_uops = int(
            counters.get("l1i_speculative_path_profile_uops_q16", 0)
        ) / 65_536.0
        profile_memory_uops = int(
            counters.get(
                "l1i_speculative_path_profile_memory_uops_q16", 0
            )
        ) / 65_536.0
        profile_rob_capped_uops = int(
            counters.get(
                "l1i_speculative_path_profile_rob_capped_uops_q16", 0
            )
        ) / 65_536.0
        gem5_squashed_memory = (
            gem5_stats["squashed_loads"] + gem5_stats["squashed_stores"]
        )
        operand_instructions = int(
            counters.get("l1i_speculative_path_operand_instructions", 0)
        )
        operand_segments = int(
            counters.get("l1i_speculative_path_operand_segments", 0)
        )
        operand_raw_edges = int(
            counters.get("l1i_speculative_path_raw_edges", 0)
        )
        operand_dependent_instructions = int(
            counters.get(
                "l1i_speculative_path_dependent_instructions", 0
            )
        )
        operand_chain_depth_sum = int(
            counters.get("l1i_speculative_path_chain_depth_sum", 0)
        )
        operand_chain_depth_max = int(
            counters.get("l1i_speculative_path_chain_depth_max", 0)
        )
        static_instructions = int(
            counters["l1i_speculative_path_static_instructions"]
        )

        rows.append(
            {
                "workload": workload,
                "n_user": n_user,
                "reference_cycles_per_user_uop": reference_cycles_per_uop,
                "formal_cycles_per_user_uop": formal_cycles_per_uop,
                "formal_cycles_per_user_uop_ape_percent": abs(
                    formal_cycles_per_uop - reference_cycles_per_uop
                ) / reference_cycles_per_uop * 100.0,
                "candidate_cycles_per_user_uop": candidate_cycles_per_uop,
                "candidate_cycles_per_user_uop_ape_percent": abs(
                    candidate_cycles_per_uop - reference_cycles_per_uop
                ) / reference_cycles_per_uop * 100.0,
                "speculative_path_records": int(
                    counters["l1i_speculative_path_records"]
                ),
                "speculative_path_memory_instructions": path_memory,
                "speculative_path_memory_page_known": page_known,
                "speculative_path_memory_page_known_percent":
                    ratio_percent(page_known, path_memory),
                "speculative_path_memory_page_unstable": page_unstable,
                "speculative_path_memory_page_unstable_percent":
                    ratio_percent(page_unstable, path_memory),
                "speculative_path_memory_page_transition_samples":
                    transition_samples,
                "speculative_path_memory_page_transition_coverage_percent":
                    ratio_percent(transition_samples, path_memory),
                "speculative_path_memory_page_transition_probability_percent":
                    transition_score_ppm / (transition_samples * 10_000.0)
                    if transition_samples else 0.0,
                "speculative_dtlb_misses": int(
                    counters["speculative_dtlb_misses"]
                ),
                "speculative_dtlb_untracked": int(
                    counters["speculative_dtlb_untracked"]
                ),
                "candidate_committed_dtlb_misses": int(
                    counters["dtlb_misses"]
                ),
                "formal_committed_dtlb_misses": formal_dtlb_misses,
                "candidate_committed_dtlb_delta":
                    int(counters["dtlb_misses"]) - formal_dtlb_misses,
                "oracle_retired_dtlb_misses": retired_dtlb_misses,
                "gem5_raw_timing_dtlb_misses": raw_dtlb_misses,
                "gem5_raw_to_retired_dtlb_ratio":
                    raw_dtlb_misses / retired_dtlb_misses
                    if retired_dtlb_misses else None,
                "gem5_raw_nonretired_dtlb_excess": raw_excess,
                "raw_dtlb_excess_per_speculative_memory":
                    raw_excess / path_memory if path_memory else None,
                "speculative_path_profiled_instructions":
                    profile_instructions,
                "speculative_path_operand_instructions":
                    operand_instructions,
                "speculative_path_static_instructions":
                    static_instructions,
                "speculative_path_operand_coverage_percent":
                    ratio_percent(operand_instructions, static_instructions),
                "speculative_path_read_registers": int(
                    counters.get("l1i_speculative_path_read_registers", 0)
                ),
                "speculative_path_write_registers": int(
                    counters.get("l1i_speculative_path_write_registers", 0)
                ),
                "speculative_path_operand_segments": operand_segments,
                "speculative_path_raw_edges": operand_raw_edges,
                "speculative_path_dependent_instructions":
                    operand_dependent_instructions,
                "speculative_path_dependent_instruction_percent":
                    ratio_percent(
                        operand_dependent_instructions,
                        operand_instructions,
                    ),
                "speculative_path_raw_read_percent": ratio_percent(
                    operand_raw_edges,
                    int(counters.get(
                        "l1i_speculative_path_read_registers", 0
                    )),
                ),
                "speculative_path_chain_depth_sum":
                    operand_chain_depth_sum,
                "speculative_path_chain_depth_mean":
                    operand_chain_depth_sum / operand_instructions
                    if operand_instructions else 0.0,
                "speculative_path_chain_depth_max":
                    operand_chain_depth_max,
                "speculative_path_operand_rob_prefix_uops": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_prefix_uops_q16",
                        0,
                    )
                ) / 65_536.0,
                "speculative_path_operand_rob_capped_instructions": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_instructions",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_read_registers": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_read_registers",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_write_registers": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_write_registers",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_memory_instructions": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_memory_instructions",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_memory_instructions_max_per_path": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_memory_instructions_max_per_path",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_write_registers_max_per_path": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_write_registers_max_per_path",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_raw_edges": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_raw_edges",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_dependent_instructions": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_dependent_instructions",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_chain_depth_sum": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_chain_depth_sum",
                        0,
                    )
                ),
                "speculative_path_operand_rob_capped_chain_depth_max": int(
                    counters.get(
                        "l1i_speculative_path_operand_rob_capped_chain_depth_max",
                        0,
                    )
                ),
                "speculative_path_profile_coverage_percent":
                    ratio_percent(
                        profile_instructions,
                        static_instructions,
                    ),
                "speculative_path_profile_uops": profile_uops,
                "speculative_path_profile_uops_per_instruction":
                    profile_uops / profile_instructions
                    if profile_instructions else None,
                "speculative_path_profile_memory_uops": profile_memory_uops,
                "speculative_path_profile_rob_capped_uops":
                    profile_rob_capped_uops,
                "gem5_commit_squashed_insts":
                    gem5_stats["commit_squashed_insts"],
                "gem5_squashed_insts_issued":
                    gem5_stats["squashed_insts_issued"],
                "gem5_squashed_loads": gem5_stats["squashed_loads"],
                "gem5_squashed_stores": gem5_stats["squashed_stores"],
                "gem5_squashed_memory_insts": gem5_squashed_memory,
                "gem5_iq_full_events": gem5_stats["iq_full_events"],
                "gem5_rename_undone_maps": gem5_stats["rename_undone_maps"],
                "profile_uops_per_gem5_commit_squashed":
                    profile_uops / gem5_stats["commit_squashed_insts"]
                    if gem5_stats["commit_squashed_insts"] else None,
                "profile_rob_capped_uops_per_gem5_commit_squashed":
                    profile_rob_capped_uops /
                    gem5_stats["commit_squashed_insts"]
                    if gem5_stats["commit_squashed_insts"] else None,
                "profile_uops_per_gem5_issued_squashed":
                    profile_uops / gem5_stats["squashed_insts_issued"]
                    if gem5_stats["squashed_insts_issued"] else None,
                "profile_memory_uops_per_gem5_squashed_memory":
                    profile_memory_uops / gem5_squashed_memory
                    if gem5_squashed_memory else None,
            }
        )

    apes = [
        row["candidate_cycles_per_user_uop_ape_percent"] for row in rows
    ]
    denominator = sum(
        row["reference_cycles_per_user_uop"] * row["n_user"]
        for row in rows
    )
    absolute_numerator = sum(
        abs(
            row["candidate_cycles_per_user_uop"]
            - row["reference_cycles_per_user_uop"]
        )
        * row["n_user"]
        for row in rows
    )
    signed_numerator = sum(
        (
            row["candidate_cycles_per_user_uop"]
            - row["reference_cycles_per_user_uop"]
        )
        * row["n_user"]
        for row in rows
    )
    aggregate = {
        "cases": len(rows),
        "mean_ape_percent": sum(apes) / len(apes),
        "p50_ape_percent": percentile_type7(apes, 0.50),
        "p90_ape_percent": percentile_type7(apes, 0.90),
        "p99_ape_percent": percentile_type7(apes, 0.99),
        "wape_percent": 100.0 * absolute_numerator / denominator,
        "bias_percent": 100.0 * signed_numerator / denominator,
    }
    static_path_instructions = sum(
        int(row["speculative_path_operand_instructions"])
        for row in rows
    )
    static_path_denominator = sum(
        int(row["speculative_path_static_instructions"])
        for row in rows
    )
    operand_coverage = {
        "operand_instructions": static_path_instructions,
        "static_instructions": static_path_denominator,
        "coverage_percent": ratio_percent(
            static_path_instructions, static_path_denominator
        ),
        "read_registers": sum(
            row["speculative_path_read_registers"] for row in rows
        ),
        "write_registers": sum(
            row["speculative_path_write_registers"] for row in rows
        ),
        "path_segments": sum(
            row["speculative_path_operand_segments"] for row in rows
        ),
        "raw_edges": sum(
            row["speculative_path_raw_edges"] for row in rows
        ),
        "dependent_instructions": sum(
            row["speculative_path_dependent_instructions"] for row in rows
        ),
        "chain_depth_sum": sum(
            row["speculative_path_chain_depth_sum"] for row in rows
        ),
        "chain_depth_max": max(
            row["speculative_path_chain_depth_max"] for row in rows
        ),
        "rob_prefix_uops": sum(
            row["speculative_path_operand_rob_prefix_uops"]
            for row in rows
        ),
        "rob_capped_instructions": sum(
            row["speculative_path_operand_rob_capped_instructions"]
            for row in rows
        ),
        "rob_capped_read_registers": sum(
            row["speculative_path_operand_rob_capped_read_registers"]
            for row in rows
        ),
        "rob_capped_write_registers": sum(
            row["speculative_path_operand_rob_capped_write_registers"]
            for row in rows
        ),
        "rob_capped_memory_instructions": sum(
            row["speculative_path_operand_rob_capped_memory_instructions"]
            for row in rows
        ),
        "rob_capped_memory_instructions_max_per_path": max(
            row[
                "speculative_path_operand_rob_capped_memory_instructions_max_per_path"
            ]
            for row in rows
        ),
        "rob_capped_write_registers_max_per_path": max(
            row[
                "speculative_path_operand_rob_capped_write_registers_max_per_path"
            ]
            for row in rows
        ),
        "rob_capped_raw_edges": sum(
            row["speculative_path_operand_rob_capped_raw_edges"]
            for row in rows
        ),
        "rob_capped_dependent_instructions": sum(
            row[
                "speculative_path_operand_rob_capped_dependent_instructions"
            ]
            for row in rows
        ),
        "rob_capped_chain_depth_sum": sum(
            row["speculative_path_operand_rob_capped_chain_depth_sum"]
            for row in rows
        ),
        "rob_capped_chain_depth_max": max(
            row["speculative_path_operand_rob_capped_chain_depth_max"]
            for row in rows
        ),
        "affects_timing": False,
    }
    operand_coverage["dependent_instruction_percent"] = ratio_percent(
        operand_coverage["dependent_instructions"],
        operand_coverage["operand_instructions"],
    )
    operand_coverage["raw_read_percent"] = ratio_percent(
        operand_coverage["raw_edges"], operand_coverage["read_registers"]
    )
    operand_coverage["chain_depth_mean"] = (
        operand_coverage["chain_depth_sum"] /
        operand_coverage["operand_instructions"]
        if operand_coverage["operand_instructions"] else 0.0
    )
    dtlb_reference = sum(row["oracle_retired_dtlb_misses"] for row in rows)
    dtlb_aggregate = {
        "formal_wape_percent": 100.0 * sum(
            abs(
                row["formal_committed_dtlb_misses"]
                - row["oracle_retired_dtlb_misses"]
            )
            for row in rows
        ) / dtlb_reference,
        "candidate_wape_percent": 100.0 * sum(
            abs(
                row["candidate_committed_dtlb_misses"]
                - row["oracle_retired_dtlb_misses"]
            )
            for row in rows
        ) / dtlb_reference,
        "candidate_bias_percent": 100.0 * sum(
            row["candidate_committed_dtlb_misses"]
            - row["oracle_retired_dtlb_misses"]
            for row in rows
        ) / dtlb_reference,
    }
    if len(feature_settings) != 1:
        raise ValueError(
            f"audit reports use inconsistent feature settings: {feature_settings}"
        )
    l1i_enabled, l1i_path_enabled, dtlb_path_enabled = next(
        iter(feature_settings)
    )
    return {
        "schema": "fastsim-speculative-path-audit-v2",
        "audit": str(audit),
        "formal": str(formal),
        "cores": args.cores,
        "report_name": args.report_name,
        "feature_settings": {
            "l1i_enabled": l1i_enabled,
            "l1i_speculative_path_state": l1i_path_enabled,
            "dtlb_speculative_path_state": dtlb_path_enabled,
        },
        "interpretation": {
            "speculative_counters_are_architectural_pmu": False,
            "speculative_accesses_counted_in_architectural_pmu": False,
            "candidate_mutates_dtlb_state": dtlb_path_enabled,
            "gem5_raw_timing_dtlb_includes_nonretired_requests": True,
        },
        "candidate_cycles_per_user_uop": aggregate,
        "static_operand_observability": operand_coverage,
        "committed_dtlb_misses": dtlb_aggregate,
        "rows": rows,
    }


def write_outputs(summary: dict[str, Any], prefix: Path) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    with prefix.with_suffix(".json").open("w", encoding="utf-8") as output:
        json.dump(summary, output, indent=2, sort_keys=True)
        output.write("\n")

    rows = summary["rows"]
    with prefix.with_suffix(".csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    aggregate = summary["candidate_cycles_per_user_uop"]
    with prefix.with_suffix(".md").open("w", encoding="utf-8") as output:
        output.write(f"# C{summary['cores']} speculative-path audit\n\n")
        output.write(
            "The candidate uses a pre-repair predictor snapshot, complete static "
            "instruction maps, and state-only speculative DTLB replay. Speculative "
            "counters are diagnostic and are not architectural PMU.\n\n"
        )
        output.write(
            "Candidate cycles/user-UOP APE: "
            f"mean {aggregate['mean_ape_percent']:.3f}%, "
            f"P50 {aggregate['p50_ape_percent']:.3f}%, "
            f"P90 {aggregate['p90_ape_percent']:.3f}%, "
            f"P99 {aggregate['p99_ape_percent']:.3f}%, "
            f"WAPE {aggregate['wape_percent']:.3f}%, "
            f"bias {aggregate['bias_percent']:.3f}%.\n\n"
        )
        dtlb = summary["committed_dtlb_misses"]
        output.write(
            "Committed user DTLB-miss WAPE changes from "
            f"{dtlb['formal_wape_percent']:.3f}% to "
            f"{dtlb['candidate_wape_percent']:.3f}% "
            f"(candidate bias {dtlb['candidate_bias_percent']:.3f}%).\n\n"
        )
        operands = summary["static_operand_observability"]
        output.write(
            "Static operand coverage on traversed speculative instructions: "
            f"{operands['coverage_percent']:.2f}% "
            f"({operands['operand_instructions']}/"
            f"{operands['static_instructions']}). These counters are "
            "audit-only and add no cycles.\n\n"
        )
        output.write(
            "Within complete-operand path segments, "
            f"{operands['dependent_instruction_percent']:.2f}% of static "
            "instructions read a value written earlier on the same wrong "
            f"path; mean/max dependency depth is "
            f"{operands['chain_depth_mean']:.2f}/"
            f"{operands['chain_depth_max']}. The complete-macro ROB prefix "
            f"contains {operands['rob_capped_instructions']} instructions, "
            f"{operands['rob_capped_write_registers']} architectural "
            "destination lower-bound allocations, "
            f"{operands['rob_capped_memory_instructions']} memory macros, "
            "and a maximum of "
            f"{operands['rob_capped_write_registers_max_per_path']} in one "
            "path. These remain diagnostic, not IQ/ROB timing events.\n\n"
        )
        output.write(
            "| Workload | Formal cycles/user-UOP APE | Candidate cycles/user-UOP APE | Path records "
            "(M) | Path memory (M) | Known page | Unstable page | Spec DTLB "
            "misses | Page transition coverage/rate | Raw/retired DTLB | "
            "Raw excess/path memory |\n"
        )
        output.write(
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        )
        for row in rows:
            raw_ratio = row["gem5_raw_to_retired_dtlb_ratio"]
            excess_ratio = row["raw_dtlb_excess_per_speculative_memory"]
            raw_ratio_text = (
                f"{raw_ratio:.3f}" if raw_ratio is not None else "n/a"
            )
            excess_ratio_text = (
                f"{excess_ratio:.3f}"
                if excess_ratio is not None else "n/a"
            )
            output.write(
                f"| {row['workload']} | "
                f"{row['formal_cycles_per_user_uop_ape_percent']:.3f}% | "
                f"{row['candidate_cycles_per_user_uop_ape_percent']:.3f}% | "
                f"{row['speculative_path_records'] / 1e6:.3f} | "
                f"{row['speculative_path_memory_instructions'] / 1e6:.3f} | "
                f"{row['speculative_path_memory_page_known_percent']:.2f}% | "
                f"{row['speculative_path_memory_page_unstable_percent']:.2f}% | "
                f"{row['speculative_dtlb_misses']} | "
                f"{row['speculative_path_memory_page_transition_coverage_percent']:.2f}%/"
                f"{row['speculative_path_memory_page_transition_probability_percent']:.2f}% | "
                f"{raw_ratio_text} | {excess_ratio_text} |\n"
            )
        if any(row["speculative_path_profiled_instructions"] for row in rows):
            output.write("\n## Causal committed-UOP profile diagnostic\n\n")
            output.write(
                "These are state-free diagnostics learned only from earlier "
                "committed macro PCs. They do not allocate target resources or "
                "enter PMU.\n\n"
            )
            output.write(
                "| Workload | Profile coverage | Estimated UOPs (M) | gem5 "
                "ROB-capped (M) | gem5 commit-squashed (M) | ROB-capped/"
                "commit | gem5 issued-squashed (M) | Estimated/issued | "
                "Estimated memory UOPs (M) | gem5 squashed memory (M) | "
                "IQ-full events (M) |\n"
            )
            output.write(
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n"
            )
            for row in rows:
                issued_ratio = row["profile_uops_per_gem5_issued_squashed"]
                issued_text = (
                    f"{issued_ratio:.2f}x"
                    if issued_ratio is not None else "n/a"
                )
                capped_ratio = row[
                    "profile_rob_capped_uops_per_gem5_commit_squashed"
                ]
                capped_text = (
                    f"{capped_ratio:.2f}x"
                    if capped_ratio is not None else "n/a"
                )
                output.write(
                    f"| {row['workload']} | "
                    f"{row['speculative_path_profile_coverage_percent']:.2f}% | "
                    f"{row['speculative_path_profile_uops'] / 1e6:.3f} | "
                    f"{row['speculative_path_profile_rob_capped_uops'] / 1e6:.3f} | "
                    f"{row['gem5_commit_squashed_insts'] / 1e6:.3f} | "
                    f"{capped_text} | "
                    f"{row['gem5_squashed_insts_issued'] / 1e6:.3f} | "
                    f"{issued_text} | "
                    f"{row['speculative_path_profile_memory_uops'] / 1e6:.3f} | "
                    f"{row['gem5_squashed_memory_insts'] / 1e6:.3f} | "
                    f"{row['gem5_iq_full_events'] / 1e6:.3f} |\n"
                )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--formal", type=Path, required=True)
    parser.add_argument("--cores", type=int, choices=(4, 8), default=8)
    parser.add_argument("--report-name", default="fastsim.json")
    parser.add_argument("--output-prefix", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    write_outputs(summarize(args), args.output_prefix.resolve())


if __name__ == "__main__":
    main()
