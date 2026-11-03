#!/usr/bin/env python3
"""Audit TaoTrace committed PMU populations against raw gem5 stats.

This tool is intentionally diagnostic.  Raw O3/Ruby/MemCtrl statistics do not
share TaoTrace's per-core frozen window or committed-demand population, so it
must never turn the raw differences below into APE/WAPE accuracy numbers.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable

from validate_kernel_events_oracle import validate_document


SCHEMA = "fastsim-p1-native-pmu-population-audit-v1"
FIELDS = (
    "retired_uops",
    "retired_instructions",
    "memory_uops",
    "line_requests",
    "l1d_tag_accesses",
    "l1d_tag_misses",
    "private_l2_tag_accesses",
    "private_l2_tag_misses",
    "llc_tag_accesses",
    "llc_tag_misses",
    "dtlb_accesses",
    "dtlb_misses",
)
MEMORY_ACCOUNTING_FIELDS = (
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
COMPARABILITY_BLOCKERS = (
    {
        "id": "start-boundary",
        "taotrace": "functional serial marker after source warmup",
        "native": "global gem5 stats reset at architectural WORKBEGIN",
    },
    {
        "id": "end-boundary",
        "taotrace": "each core freezes at its own functional-record target",
        "native": "all cores and shared controllers run until the last core exits",
    },
    {
        "id": "request-population",
        "taotrace": "committed memory UOPs expanded to touched cache lines",
        "native": "issued Ruby/TLB requests include squashed, replayed, and merged traffic",
    },
    {
        "id": "hierarchy-event",
        "taotrace": "independent path-class cache tag proxy",
        "native": "Ruby m_demand_misses is a protocol/request event, not an LLC tag miss",
    },
    {
        "id": "memory-traffic",
        "taotrace": "DRAM transaction fields are unavailable",
        "native": "MemCtrl bursts include page walks, writebacks, and protocol traffic",
    },
)


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def indexed_int(text: str, pattern: str) -> dict[int, int]:
    result: dict[int, int] = {}
    for match in re.finditer(pattern, text, flags=re.MULTILINE):
        result[int(match.group(1))] = int(float(match.group(2)))
    return result


def require_indices(values: dict[int, int], cores: int, field: str) -> None:
    expected = set(range(cores))
    actual = set(values)
    if actual != expected:
        raise ValueError(
            f"native stats {field} indices {sorted(actual)} != {sorted(expected)}"
        )


def parse_native_stats(
    path: Path, cores: int, llc_banks: int, dram_channels: int
) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")

    def core(pattern: str, field: str) -> dict[int, int]:
        values = indexed_int(text, pattern)
        require_indices(values, cores, field)
        return values

    cycles = core(
        r"board\.processor\.switch(\d+)\.core\.numCycles\s+([0-9.eE+-]+)",
        "cycles",
    )
    retired_uops = core(
        r"board\.processor\.switch(\d+)\.core\.commitStats0\.numOps\s+([0-9.eE+-]+)",
        "retired_uops",
    )
    retired_instructions = core(
        r"board\.processor\.switch(\d+)\.core\.commitStats0\.numInsts\s+([0-9.eE+-]+)",
        "retired_instructions",
    )
    dtlb_rd_accesses = core(
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.rdAccesses\s+([0-9.eE+-]+)",
        "dtlb_rd_accesses",
    )
    dtlb_wr_accesses = core(
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.wrAccesses\s+([0-9.eE+-]+)",
        "dtlb_wr_accesses",
    )
    dtlb_rd_misses = core(
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.rdMisses\s+([0-9.eE+-]+)",
        "dtlb_rd_misses",
    )
    dtlb_wr_misses = core(
        r"board\.processor\.switch(\d+)\.core\.mmu\.dtb\.wrMisses\s+([0-9.eE+-]+)",
        "dtlb_wr_misses",
    )

    def ruby(controller: str, cache: str, stat: str, expected: int) -> dict[int, int]:
        values = indexed_int(
            text,
            rf"ruby_system\.{controller}(\d+)\.{cache}\.m_demand_{stat}"
            rf"\s+([0-9.eE+-]+)",
        )
        require_indices(values, expected, f"{controller}.{cache}.{stat}")
        return values

    l1_accesses = ruby("l1_controllers", "Dcache", "accesses", cores)
    l1_misses = ruby("l1_controllers", "Dcache", "misses", cores)
    l2_accesses = ruby("l2_controllers", "cache", "accesses", cores)
    l2_misses = ruby("l2_controllers", "cache", "misses", cores)
    llc_accesses = ruby("l3_controllers", "L2cache", "accesses", llc_banks)
    llc_misses = ruby("l3_controllers", "L2cache", "misses", llc_banks)
    dram_reads = indexed_int(
        text,
        r"board\.memory\.mem_ctrl(\d+)\.dram\.readBursts\s+([0-9.eE+-]+)",
    )
    dram_writes = indexed_int(
        text,
        r"board\.memory\.mem_ctrl(\d+)\.dram\.writeBursts\s+([0-9.eE+-]+)",
    )
    require_indices(dram_reads, dram_channels, "dram.readBursts")
    require_indices(dram_writes, dram_channels, "dram.writeBursts")

    per_core = []
    for core_id in range(cores):
        per_core.append(
            {
                "core_id": core_id,
                "cycles": cycles[core_id],
                "retired_uops": retired_uops[core_id],
                "retired_instructions": retired_instructions[core_id],
                "dtlb_accesses": dtlb_rd_accesses[core_id]
                + dtlb_wr_accesses[core_id],
                "dtlb_misses": dtlb_rd_misses[core_id]
                + dtlb_wr_misses[core_id],
                "l1d_demand_accesses": l1_accesses[core_id],
                "l1d_demand_misses": l1_misses[core_id],
                "private_l2_demand_accesses": l2_accesses[core_id],
                "private_l2_demand_misses": l2_misses[core_id],
            }
        )

    def total(name: str) -> int:
        return sum(int(row[name]) for row in per_core)

    return {
        "window": "global-workbegin-to-last-core-exit",
        "population": "native-issued-and-committed-mixed-stats",
        "per_core": per_core,
        "aggregate": {
            "cycles": total("cycles"),
            "retired_uops": total("retired_uops"),
            "retired_instructions": total("retired_instructions"),
            "dtlb_accesses": total("dtlb_accesses"),
            "dtlb_misses": total("dtlb_misses"),
            "l1d_demand_accesses": total("l1d_demand_accesses"),
            "l1d_demand_misses": total("l1d_demand_misses"),
            "private_l2_demand_accesses": total("private_l2_demand_accesses"),
            "private_l2_demand_misses": total("private_l2_demand_misses"),
            "ruby_llc_demand_accesses": sum(llc_accesses.values()),
            "ruby_llc_demand_misses": sum(llc_misses.values()),
            "dram_read_bursts": sum(dram_reads.values()),
            "dram_write_bursts": sum(dram_writes.values()),
        },
    }


def add_pmu(left: dict[str, int], right: dict[str, Any]) -> None:
    for field in FIELDS:
        left[field] += int(right[field])


def oracle_all_classified(document: dict[str, Any]) -> dict[str, Any]:
    try:
        validation = validate_document(document, max_unknown_ratio=0.0)
    except ValueError as error:
        raise ValueError(
            f"P1 population audit requires a formal v3 oracle: {error}"
        ) from error
    if not validation["formal_pmu_eligible"]:
        raise ValueError("P1 population audit requires a formal v3 oracle")

    per_core = []
    for row in document["per_core"]:
        pmu = {field: 0 for field in FIELDS}
        add_pmu(pmu, row["pmu_user"])
        for kernel in row["pmu_kernel_by_class"].values():
            add_pmu(pmu, kernel)
        pmu["cycles"] = int(row["measured_cycles"])
        accounting = {
            field: int(row["memory_accounting"][field])
            for field in MEMORY_ACCOUNTING_FIELDS
        }
        per_core.append(
            {
                "core_id": int(row["core_id"]),
                **pmu,
                "memory_accounting": accounting,
            }
        )

    aggregate = {field: sum(int(row[field]) for row in per_core) for field in FIELDS}
    aggregate["cycles"] = sum(int(row["cycles"]) for row in per_core)
    aggregate["memory_accounting"] = {
        field: sum(int(row["memory_accounting"][field]) for row in per_core)
        for field in MEMORY_ACCOUNTING_FIELDS
    }
    return {
        "window": "serial-marker-to-per-core-functional-target",
        "population": "all-CPL-classified-committed-demand",
        "pmu_source": document["aggregate"]["pmu_source"],
        "pmu_contract_id": document["aggregate"]["pmu_contract_id"],
        "per_core": per_core,
        "aggregate": aggregate,
    }


def ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def raw_population_ratios(oracle: dict[str, Any], native: dict[str, Any]) -> dict[str, Any]:
    o = oracle["aggregate"]
    n = native["aggregate"]
    pairs = {
        "retired_uops": (n["retired_uops"], o["retired_uops"]),
        "retired_instructions": (n["retired_instructions"], o["retired_instructions"]),
        "dtlb_accesses": (n["dtlb_accesses"], o["dtlb_accesses"]),
        "dtlb_misses": (n["dtlb_misses"], o["dtlb_misses"]),
        "l1d_accesses": (n["l1d_demand_accesses"], o["l1d_tag_accesses"]),
        "l1d_misses": (n["l1d_demand_misses"], o["l1d_tag_misses"]),
        "private_l2_accesses": (
            n["private_l2_demand_accesses"],
            o["private_l2_tag_accesses"],
        ),
        "private_l2_misses": (
            n["private_l2_demand_misses"],
            o["private_l2_tag_misses"],
        ),
        "llc_accesses": (n["ruby_llc_demand_accesses"], o["llc_tag_accesses"]),
        "llc_misses": (n["ruby_llc_demand_misses"], o["llc_tag_misses"]),
    }
    return {
        name: {
            "native": int(values[0]),
            "taotrace": int(values[1]),
            "native_over_taotrace": ratio(int(values[0]), int(values[1])),
            "accuracy_metric_allowed": False,
        }
        for name, values in pairs.items()
    }


def workload_from_result(result: Path) -> str:
    request = load(result / "request.json")
    selection = request.get("workload_selection", {})
    return str(selection.get("workload") or result.parents[2].name)


def audit_result(result: Path) -> dict[str, Any]:
    result = result.resolve()
    request = load(result / "request.json")
    oracle_document = load(result / "oracle" / "kernel_events.json")
    oracle = oracle_all_classified(oracle_document)
    cores = len(oracle["per_core"])
    target = load(result / "effective-target.json")
    target_cores = int(target["core"]["count"])
    if target_cores != cores:
        raise ValueError(f"effective target cores {target_cores} != oracle cores {cores}")
    native = parse_native_stats(
        result / "stats.txt",
        cores,
        int(target["cache"]["l3"]["num_banks"]),
        int(target["dram"]["num_channels"]),
    )

    window_rows = []
    for oracle_row, native_row in zip(oracle["per_core"], native["per_core"]):
        if oracle_row["core_id"] != native_row["core_id"]:
            raise ValueError("per-core oracle/native ordering mismatch")
        window_rows.append(
            {
                "core_id": oracle_row["core_id"],
                "taotrace_cycles": oracle_row["cycles"],
                "native_global_cycles": native_row["cycles"],
                "taotrace_retired_uops": oracle_row["retired_uops"],
                "native_retired_uops": native_row["retired_uops"],
                "native_minus_taotrace_retired_uops": (
                    native_row["retired_uops"] - oracle_row["retired_uops"]
                ),
            }
        )

    gem5 = request.get("gem5", {})
    accounting = oracle["aggregate"]["memory_accounting"]
    return {
        "workload": workload_from_result(result),
        "cores": cores,
        "result_dir": str(result),
        "gem5_binary_sha256": gem5.get("binary_sha256"),
        "formal_comparable": False,
        "accuracy_metrics_emitted": False,
        "comparability_blockers": list(COMPARABILITY_BLOCKERS),
        "window_evidence": window_rows,
        "path_attribution_timing_evidence": {
            "fallback_attributed_uops": accounting["fallback_attributed_uops"],
            "late_packets_after_fallback": accounting[
                "late_packets_after_fallback"
            ],
            "late_packets_over_fallback": ratio(
                accounting["late_packets_after_fallback"],
                accounting["fallback_attributed_uops"],
            ),
            "interpretation": (
                "A late callback is not double-counted. A high ratio proves that "
                "the fallback cache path was frozen before a later packet outcome "
                "became observable; it is timing evidence, not an accuracy metric."
            ),
            "accuracy_metric_allowed": False,
        },
        "taotrace": oracle,
        "native": native,
        "raw_diagnostic_ratios": raw_population_ratios(oracle, native),
    }


def matrix_results(matrix: Path) -> Iterable[Path]:
    status = load(matrix / "status.json")
    for key, task in sorted(status.get("tasks", {}).items()):
        sample = task.get("sample", {})
        if sample.get("status") != "completed" or sample.get("return_code") != 0:
            raise ValueError(f"matrix task {key} is not a successful completed sample")
        result = sample.get("result_dir")
        if not result:
            raise ValueError(f"matrix task {key} lacks result_dir")
        yield Path(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, action="append", default=[])
    parser.add_argument("--matrix", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    results = list(args.result)
    for matrix in args.matrix:
        results.extend(matrix_results(matrix))
    if not results:
        raise SystemExit("at least one --result or --matrix is required")

    cases = [audit_result(path) for path in results]
    document = {
        "schema": SCHEMA,
        "contract": "diagnostic-only-no-ape-wape",
        "formal_comparable_cases": 0,
        "cases": cases,
    }
    rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
