#!/usr/bin/env python3
"""Compare FastSim cache PMU counters with gem5 Ruby and trace-side labels."""

from __future__ import annotations

import argparse
import glob
import json
import re
from pathlib import Path
from typing import Dict, List, Optional


def indexed_stats(text: str, pattern: str) -> Dict[int, int]:
    result: Dict[int, int] = {}
    for match in re.finditer(pattern, text):
        result[int(match.group(1))] = int(match.group(2))
    return result


def relative_error(predicted: float, reference: float):
    if reference == 0:
        return None
    return (predicted - reference) / reference


def aggregate_row(name: str, predicted: float, reference: float) -> dict:
    signed_error = relative_error(predicted, reference)
    return {
        "name": name,
        "fastsim": predicted,
        "reference": reference,
        "signed_relative_error": signed_error,
        "absolute_relative_error": (
            abs(signed_error) if signed_error is not None else None
        ),
    }


def indexed_mape(
    fastsim: List[dict],
    reference: Dict[int, int],
    key: str,
    index_key: str = "core",
) -> Optional[float]:
    errors = []
    for item in fastsim:
        core_id = int(item[index_key])
        truth = reference.get(core_id)
        if truth:
            errors.append(abs(int(item[key]) - truth) / truth)
    return sum(errors) / len(errors) if errors else None


def indexed_cpi_mape(
    fastsim: List[dict],
    reference_cycles: Dict[int, int],
    reference_denominators: Dict[int, int],
    fastsim_denominator_key: str,
) -> Optional[float]:
    errors = []
    for item in fastsim:
        core_id = int(item["core"])
        truth_cycles = reference_cycles.get(core_id)
        truth_denominator = reference_denominators.get(core_id)
        predicted_denominator = int(item[fastsim_denominator_key])
        if not truth_cycles or not truth_denominator or not predicted_denominator:
            continue
        predicted_cpi = int(item["cycles"]) / predicted_denominator
        reference_cpi = truth_cycles / truth_denominator
        errors.append(abs(predicted_cpi - reference_cpi) / reference_cpi)
    return sum(errors) / len(errors) if errors else None


def functional_memory_paths(patterns: List[str]) -> dict:
    try:
        import numpy as np
        import pyarrow.parquet as pq
    except ImportError as error:
        raise SystemExit(
            "pyarrow and numpy are required when --aligned is used"
        ) from error

    files = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    if not files:
        raise SystemExit("--aligned patterns matched no files")
    path_counts: Dict[int, int] = {}
    memory_records = 0
    for path in sorted(set(files)):
        parquet = pq.ParquetFile(path)
        required = {"is_load", "is_store", "is_atomic", "path_class"}
        missing = required - set(parquet.schema_arrow.names)
        if missing:
            raise SystemExit(f"{path}: missing columns {sorted(missing)}")
        for batch in parquet.iter_batches(
            batch_size=262_144,
            columns=["is_load", "is_store", "is_atomic", "path_class"],
        ):
            memory = (
                batch.column(0).to_numpy(zero_copy_only=False)
                | batch.column(1).to_numpy(zero_copy_only=False)
                | batch.column(2).to_numpy(zero_copy_only=False)
            ).astype(bool)
            paths = batch.column(3).to_numpy(zero_copy_only=False)[memory]
            memory_records += int(memory.sum())
            values, counts = np.unique(paths, return_counts=True)
            for value, count in zip(values, counts):
                path_counts[int(value)] = (
                    path_counts.get(int(value), 0) + int(count)
                )
    return {
        "memory_records": memory_records,
        "path_class_counts": path_counts,
        "memory_or_beyond": path_counts.get(4, 0),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fastsim-stats", required=True)
    parser.add_argument("--gem5-stats", required=True)
    parser.add_argument(
        "--aligned",
        action="append",
        default=[],
        help="Optional aligned Parquet path/glob; repeat as needed",
    )
    parser.add_argument("--output")
    args = parser.parse_args()

    fastsim = json.loads(Path(args.fastsim_stats).read_text())
    gem5_text = Path(args.gem5_stats).read_text()
    l1_misses = indexed_stats(
        gem5_text,
        r"l1_controllers(\d+)\.Dcache\.m_demand_misses\s+(\d+)",
    )
    l1_accesses = indexed_stats(
        gem5_text,
        r"l1_controllers(\d+)\.Dcache\.m_demand_accesses\s+(\d+)",
    )
    l2_misses = indexed_stats(
        gem5_text,
        r"l2_controllers(\d+)\.cache\.m_demand_misses\s+(\d+)",
    )
    l2_accesses = indexed_stats(
        gem5_text,
        r"l2_controllers(\d+)\.cache\.m_demand_accesses\s+(\d+)",
    )
    llc_misses = indexed_stats(
        gem5_text,
        r"l3_controllers(\d+)\.L2cache\.m_demand_misses\s+(\d+)",
    )
    llc_accesses = indexed_stats(
        gem5_text,
        r"l3_controllers(\d+)\.L2cache\.m_demand_accesses\s+(\d+)",
    )
    core_cycles = indexed_stats(
        gem5_text,
        r"board\.processor\.switch(\d+)\.core\.numCycles\s+(\d+)",
    )
    core_instructions = indexed_stats(
        gem5_text,
        (
            r"board\.processor\.switch(\d+)\.core\.commitStats0"
            r"\.numInsts\s+(\d+)"
        ),
    )
    core_uops = indexed_stats(
        gem5_text,
        (
            r"board\.processor\.switch(\d+)\.core\.commitStats0"
            r"\.numOps\s+(\d+)"
        ),
    )
    branch_misses = indexed_stats(
        gem5_text,
        (
            r"board\.processor\.switch(\d+)\.core\.branchPred"
            r"\.mispredicted_0::total\s+(\d+)"
        ),
    )
    committed_branches = indexed_stats(
        gem5_text,
        (
            r"board\.processor\.switch(\d+)\.core\.branchPred"
            r"\.committed_0::total\s+(\d+)"
        ),
    )
    rename_iq_full_events = indexed_stats(
        gem5_text,
        (
            r"board\.processor\.switch(\d+)\.core\.rename"
            r"\.IQFullEvents\s+(\d+)"
        ),
    )
    dtlb_read_accesses = indexed_stats(
        gem5_text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb"
        r"\.rdAccesses\s+(\d+)",
    )
    dtlb_write_accesses = indexed_stats(
        gem5_text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb"
        r"\.wrAccesses\s+(\d+)",
    )
    dtlb_read_misses = indexed_stats(
        gem5_text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb"
        r"\.rdMisses\s+(\d+)",
    )
    dtlb_write_misses = indexed_stats(
        gem5_text,
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb"
        r"\.wrMisses\s+(\d+)",
    )
    dtlb_accesses = {
        core: dtlb_read_accesses.get(core, 0)
        + dtlb_write_accesses.get(core, 0)
        for core in set(dtlb_read_accesses) | set(dtlb_write_accesses)
    }
    dtlb_misses = {
        core: dtlb_read_misses.get(core, 0)
        + dtlb_write_misses.get(core, 0)
        for core in set(dtlb_read_misses) | set(dtlb_write_misses)
    }
    if not l1_misses or not l2_misses or not llc_misses:
        raise SystemExit("failed to locate expected MESI_Three_Level stats")

    totals = fastsim["totals"]
    fastsim_cycles = sum(int(core["cycles"]) for core in fastsim["cores"])
    fastsim_instructions = int(totals["retired_instructions"])
    fastsim_uops = int(totals["retired_uops"])
    gem5_cycles = sum(core_cycles.values())
    gem5_instructions = sum(core_instructions.values())
    gem5_uops = sum(core_uops.values())
    comparison = [
        aggregate_row(
            "retired_instructions",
            fastsim_instructions,
            gem5_instructions,
        ),
        aggregate_row(
            "retired_uops",
            fastsim_uops,
            gem5_uops,
        ),
        aggregate_row(
            "aggregate_macro_cpi",
            fastsim_cycles / fastsim_instructions,
            gem5_cycles / gem5_instructions,
        ),
        aggregate_row(
            "aggregate_uop_cpi",
            fastsim_cycles / fastsim_uops,
            gem5_cycles / gem5_uops,
        ),
        aggregate_row(
            "l1d_demand_accesses",
            totals["l1d_accesses"],
            sum(l1_accesses.values()),
        ),
        aggregate_row(
            "l1d_demand_misses",
            totals["l1d_misses"],
            sum(l1_misses.values()),
        ),
        aggregate_row(
            "private_l2_demand_accesses",
            totals["l2_accesses"],
            sum(l2_accesses.values()),
        ),
        aggregate_row(
            "private_l2_demand_misses",
            totals["l2_misses"],
            sum(l2_misses.values()),
        ),
        aggregate_row(
            "shared_llc_tag_accesses_vs_ruby_demand_accesses",
            totals["llc_accesses"],
            sum(llc_accesses.values()),
        ),
        aggregate_row(
            "shared_llc_tag_misses_vs_ruby_demand_misses",
            totals["llc_misses"],
            sum(llc_misses.values()),
        ),
    ]
    if core_cycles:
        comparison.append(
            aggregate_row(
                "sum_of_per_core_cycles",
                fastsim_cycles,
                gem5_cycles,
            )
        )
    if dtlb_accesses and "dtlb_accesses" in totals:
        comparison.extend(
            [
                aggregate_row(
                    "dtlb_accesses",
                    int(totals["dtlb_accesses"]),
                    sum(dtlb_accesses.values()),
                ),
                aggregate_row(
                    "dtlb_misses",
                    int(totals["dtlb_misses"]),
                    sum(dtlb_misses.values()),
                ),
            ]
        )
    if rename_iq_full_events and "o3_iq_full_events" in totals:
        comparison.append(
            aggregate_row(
                "o3_rename_iq_full_events",
                int(totals["o3_iq_full_events"]),
                sum(rename_iq_full_events.values()),
            )
        )
    fastsim_cha = []
    for cha in fastsim["cha"]:
        enriched = dict(cha)
        enriched["llc_lookups"] = (
            int(cha["llc_hits"]) + int(cha["llc_misses"])
        )
        fastsim_cha.append(enriched)
    fastsim_cha_lookups = sum(
        int(cha["llc_lookups"]) for cha in fastsim_cha
    )
    comparison.append(
        aggregate_row(
            "cha_llc_lookups_vs_shared_llc_demand_accesses",
            fastsim_cha_lookups,
            sum(llc_accesses.values()),
        )
    )
    branch_records = (
        int(totals["branches"]) +
        int(totals["branches_without_outcome"])
    )
    branch_outcome_coverage = (
        int(totals["branches"]) / branch_records
        if branch_records
        else 1.0
    )
    if branch_misses and branch_outcome_coverage == 1.0:
        comparison.extend(
            [
                aggregate_row(
                    "committed_branches",
                    int(totals["branches"]),
                    sum(committed_branches.values()),
                ),
                aggregate_row(
                    "branch_misses",
                    int(totals["branch_misses"]),
                    sum(branch_misses.values()),
                ),
            ]
        )
    report = {
        "schema": "fastsim-gem5-pmu-validation-v3",
        "fastsim_stats": str(Path(args.fastsim_stats).resolve()),
        "gem5_stats": str(Path(args.gem5_stats).resolve()),
        "gem5": {
            "l1d_accesses": sum(l1_accesses.values()),
            "l1d_misses": sum(l1_misses.values()),
            "private_l2_accesses": sum(l2_accesses.values()),
            "private_l2_misses": sum(l2_misses.values()),
            "shared_llc_accesses": sum(llc_accesses.values()),
            "shared_llc_demand_misses": sum(llc_misses.values()),
            "retired_instructions": gem5_instructions,
            "retired_uops": gem5_uops,
            "sum_of_per_core_cycles": gem5_cycles,
            "aggregate_macro_cpi": gem5_cycles / gem5_instructions,
            "aggregate_uop_cpi": gem5_cycles / gem5_uops,
            "committed_branches": sum(committed_branches.values()),
            "branch_misses": sum(branch_misses.values()),
            "dtlb_accesses": sum(dtlb_accesses.values()),
            "dtlb_misses": sum(dtlb_misses.values()),
            "o3_rename_iq_full_events": sum(
                rename_iq_full_events.values()
            ),
        },
        "input_coverage": {
            "branch_outcome_coverage": branch_outcome_coverage,
            "branches_without_outcome": int(
                totals["branches_without_outcome"]
            ),
        },
        "comparison": comparison,
        "per_core": {
            "l1d_miss_mape": indexed_mape(
                fastsim["cores"], l1_misses, "l1d_misses"
            ),
            "private_l2_miss_mape": indexed_mape(
                fastsim["cores"], l2_misses, "l2_misses"
            ),
            "cycle_mape": indexed_mape(
                fastsim["cores"], core_cycles, "cycles"
            ),
            "macro_cpi_mape": indexed_cpi_mape(
                fastsim["cores"], core_cycles, core_instructions,
                "instructions"
            ),
            "uop_cpi_mape": indexed_cpi_mape(
                fastsim["cores"], core_cycles, core_uops, "uops"
            ),
            "branch_miss_mape": (
                indexed_mape(
                    fastsim["cores"], branch_misses, "branch_misses"
                )
                if branch_outcome_coverage == 1.0
                else None
            ),
            "dtlb_access_mape": (
                indexed_mape(
                    fastsim["cores"], dtlb_accesses, "dtlb_accesses"
                )
                if dtlb_accesses and "dtlb_accesses" in totals
                else None
            ),
            "dtlb_miss_mape": (
                indexed_mape(
                    fastsim["cores"], dtlb_misses, "dtlb_misses"
                )
                if dtlb_misses and "dtlb_misses" in totals
                else None
            ),
            "o3_rename_iq_full_mape": (
                indexed_mape(
                    fastsim["cores"], rename_iq_full_events,
                    "o3_iq_full_events"
                )
                if rename_iq_full_events and
                "o3_iq_full_events" in totals
                else None
            ),
            "cha_llc_lookup_mape": indexed_mape(
                fastsim_cha, llc_accesses, "llc_lookups", "cha"
            ),
        },
        "scope": {
            "l1d_and_private_l2": (
                "miss counters are direct aggregate comparisons to gem5 "
                "Ruby demand misses; access counts are diagnostic because "
                "functional operation and Ruby request scopes can differ"
            ),
            "llc": (
                "FastSim reports functional tag misses; Ruby demand misses "
                "also reflect protocol/permission behavior, so the direct "
                "difference is diagnostic rather than an accuracy score"
            ),
            "branch": (
                "direct replay comparison to gem5 committed branch "
                "predictor counters"
                if branch_outcome_coverage == 1.0
                else "unavailable because the functional trace lacks "
                "committed branch outcomes"
            ),
            "cha": (
                "aggregate and per-slice LLC lookup comparison to gem5 "
                "shared LLC demand accesses; total CHA requests additionally "
                "include permission upgrades and are not equated with this "
                "Ruby demand counter"
            ),
            "cycles": (
                "diagnostic comparison of non-cycle-accurate FastSim "
                "penalties to gem5 O3 per-core cycles"
            ),
            "dtlb": (
                "direct comparison of functional virtual-page replay to "
                "gem5 x86 DTLB read/write access and miss counters; page "
                "walk latency remains a calibrated timing approximation"
            ),
            "o3_iq": (
                "FastSim counts modeled UOP dispatch admissions delayed by "
                "a full IQ. gem5 rename.IQFullEvents counts rename calls "
                "that block or make partial progress because IQ entries are "
                "insufficient. The comparison is a timing-pressure proxy, "
                "not identical event scope"
            ),
        },
    }
    if args.aligned:
        paths = functional_memory_paths(args.aligned)
        report["functional_trace"] = paths
        report["comparison"].append(
            aggregate_row(
                "llc_tag_misses_vs_functional_memory_path",
                totals["llc_misses"],
                paths["memory_or_beyond"],
            )
        )

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(encoded)
    else:
        print(encoded, end="")


if __name__ == "__main__":
    main()
