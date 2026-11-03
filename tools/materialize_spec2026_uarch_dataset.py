#!/usr/bin/env python3
"""Link collected SPEC2026 FS cases into the uarch replay/evaluation layout."""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATRIX = ROOT / "configs/spec2026-uarch-exploration-v1.json"
DEFAULT_RUN_ROOT = ROOT / "tmp/spec2026-uarch-exploration-v1-native-v28_2"
ACTIVE_SCOPES = (
    "user", "syscall", "page_fault", "irq", "scheduler", "unknown_kernel",
)
ACTIVE_CYCLE_FIELDS = (
    "user_cycles", "syscall_kernel_cycles", "page_fault_kernel_cycles",
    "irq_kernel_cycles", "scheduler_kernel_cycles", "unknown_kernel_cycles",
)


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected JSON object")
    return value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def indexed_sum(text: str, expression: str) -> int:
    return sum(int(float(match.group(1))) for match in re.finditer(expression, text, re.MULTILINE))


def native_combined_pmu(result: Path, cores: int) -> dict[str, int]:
    levels = {
        "l1d": {"accesses": 0, "tag_misses": 0},
        "l2": {"accesses": 0, "tag_misses": 0},
        "llc": {"accesses": 0, "tag_misses": 0},
    }
    for core in range(cores):
        summary = load(result / "oracle" / f"native-summary-core{core}.json")
        by_scope = summary["native_ruby_pmu_by_scope"]
        for scope in ACTIVE_SCOPES:
            source_scope = by_scope[scope]
            if int(source_scope["hierarchy_incomplete_uops"]) != 0:
                raise ValueError(
                    f"{result}: core{core}/{scope} has incomplete native hierarchy"
                )
            for level in levels:
                source = source_scope["hierarchy"][level]
                levels[level]["accesses"] += int(source["accesses"])
                levels[level]["tag_misses"] += int(source["tag_misses"])
    return {
        "l1d_demand_accesses": levels["l1d"]["accesses"],
        "l1d_demand_misses": levels["l1d"]["tag_misses"],
        "private_l2_demand_accesses": levels["l2"]["accesses"],
        "private_l2_demand_misses": levels["l2"]["tag_misses"],
        "cha_llc_demand_accesses": levels["llc"]["accesses"],
        "ruby_llc_demand_misses": levels["llc"]["tag_misses"],
    }


def write_trace_view(result: Path, output: Path, cores: int) -> dict[str, Any]:
    source_dir = result / "tao_trace"
    trace = load(source_dir / "trace.json")
    source_manifest = (source_dir / "manifest.txt").read_text(encoding="utf-8").splitlines()
    manifest_lines = []
    for line in source_manifest:
        parts = line.split()
        if len(parts) != 8:
            raise ValueError(f"{source_dir}/manifest.txt: unsupported row {line!r}")
        parts[2] = str((source_dir / parts[2]).resolve())
        manifest_lines.append(" ".join(parts))
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest.txt").write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    per_core = trace.get("per_core", {})
    expected = {str(core) for core in range(cores)}
    if set(per_core) != expected:
        raise ValueError(f"{result}: trace core set mismatch")
    records = {
        core: int(row["measurement_records"])
        for core, row in per_core.items()
    }
    user_records = {
        core: int(row.get("measurement_user_records", row["measurement_records"]))
        for core, row in per_core.items()
    }
    feature_flags = {}
    for core, row in per_core.items():
        with Path(row["fst"]).open("rb") as source:
            header = source.read(40)
        if len(header) != 40:
            raise ValueError(f"{row['fst']}: short FST header")
        feature_flags[core] = struct.unpack_from("<Q", header, 32)[0]
    metadata = {
        "schema": "fastsim-spec2026-uarch-trace-view-v1",
        "source_result": str(result),
        "records_per_core": records,
        "user_records_per_core": user_records,
        "instructions_per_core": {
            core: int(row["measurement_instructions"])
            for core, row in per_core.items()
        },
        "user_instructions_per_core": {
            core: int(
                row.get(
                    "measurement_user_instructions", row["measurement_instructions"]
                )
            )
            for core, row in per_core.items()
        },
        "fst_versions": {core: int(row["fst_version"]) for core, row in per_core.items()},
        "fst_feature_flags": feature_flags,
        "trace_scope": trace.get("trace_scope"),
        "source_trace": str((source_dir / "trace.json").resolve()),
    }
    atomic_json(output / "trace.json", metadata)
    return metadata


def make_metrics(
    result: Path, profile: dict[str, Any], workload: dict[str, Any], cores: int,
    seed: int, wall: float,
) -> dict[str, Any]:
    trace = load(result / "tao_trace/trace.json")
    kernel = load(result / "oracle/kernel_events.json")
    aggregate = kernel["aggregate"]
    total_user_uops = int(aggregate["n_user"])
    total_cycles = sum(int(aggregate[field]) for field in ACTIVE_CYCLE_FIELDS)
    total_instructions = int(aggregate["user_plus_kernel_retired_instructions"])
    total_trace_uops = sum(
        int(row["measurement_records"]) for row in trace["per_core"].values()
    )
    pmu = aggregate["pmu_user_plus_kernel"]
    native = native_combined_pmu(result, cores)
    stats_text = (result / "stats.txt").read_text(encoding="utf-8")
    rob_full = indexed_sum(
        stats_text,
        r"^board\.processor\.switch\d+\.core\.rename\.ROBFullEvents\s+([0-9.eE+-]+)",
    )
    iq_full = indexed_sum(
        stats_text,
        r"^board\.processor\.switch\d+\.core\.rename\.IQFullEvents\s+([0-9.eE+-]+)",
    )
    lsq_full = indexed_sum(
        stats_text,
        r"^board\.processor\.switch\d+\.core\.iew\.lsqFullEvents\s+([0-9.eE+-]+)",
    )
    dram_reads = indexed_sum(
        stats_text,
        r"^board\.memory\.mem_ctrl\d+\.dram\.readBursts\s+([0-9.eE+-]+)",
    )
    dram_writes = indexed_sum(
        stats_text,
        r"^board\.memory\.mem_ctrl\d+\.dram\.writeBursts\s+([0-9.eE+-]+)",
    )
    per_core = []
    trace_rows = trace["per_core"]
    for row in kernel["per_core"]:
        core = int(row["core_id"])
        user_uops = int(row["n_user"])
        instructions = int(row["user_plus_kernel_retired_instructions"])
        trace_uops = int(trace_rows[str(core)]["measurement_records"])
        cycles = sum(int(row[field]) for field in ACTIVE_CYCLE_FIELDS)
        per_core.append({
            "core": core, "cycles": cycles, "uops": user_uops,
            "trace_uops": trace_uops, "instructions": instructions,
            "uop_cpi": cycles / user_uops,
        })
    result_metrics = {
        "schema": "fastsim-gem5-uarch-metrics-v1",
        "uarch": profile["id"],
        "uarch_description": profile["description"],
        "workload": workload["name"],
        "domain": profile.get("domain", workload["domain"]),
        "workload_domain": workload["domain"],
        "cores": cores,
        "seed": seed,
        "scale": 1,
        "wall_time_seconds": wall,
        "aggregate_uop_cpi": float(
            aggregate["cycles_per_user_uop_user_plus_kernel"]
        ),
        "aggregate_macro_cpi": float(aggregate["perf_like_cpi_user_plus_kernel"]),
        "sum_core_cycles": total_cycles,
        "retired_uops": total_user_uops,
        "trace_retired_uops": total_trace_uops,
        "retired_instructions": total_instructions,
        "branch_committed": int(pmu["branches"]),
        "branch_misses": int(pmu["branch_misses"]),
        **native,
        "dtlb_accesses": int(pmu["dtlb_accesses"]),
        "dtlb_misses": int(pmu["dtlb_misses"]),
        "rob_full_events": rob_full,
        "iq_full_events": iq_full,
        "lsq_full_events": lsq_full,
        "dram_read_bursts": dram_reads,
        "dram_write_bursts": dram_writes,
        "dram_total_access_latency_ticks": 0,
        "dram_read_row_hit_rate_per_channel": {},
        "per_core": per_core,
        "source_result": str(result),
        "measurement_scope": "user-plus-kernel",
        "native_kernel_trace": True,
        "pmu_reference_scope": "user-plus-active-kernel",
        "pmu_cache_reference": "taotrace-native-summary-v1 ruby-slicc-controller-actions",
    }
    if not math.isfinite(result_metrics["aggregate_uop_cpi"]):
        raise ValueError(f"{result}: non-finite CPI")
    return result_metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, default=DEFAULT_RUN_ROOT)
    parser.add_argument("--matrix", type=Path, default=DEFAULT_MATRIX)
    args = parser.parse_args()
    run_root = args.run_root.resolve()
    matrix = load(args.matrix.resolve())
    seed = int(matrix.get("seed", 0))
    cases = 0
    for profile in matrix["profiles"]:
        for cores in matrix["core_counts"]:
            for workload in matrix["workloads"]:
                case_root = run_root / "cases" / profile["id"] / f"c{cores:02d}" / workload["name"]
                record_path = case_root / "case.json"
                if not record_path.is_file():
                    continue
                record = load(record_path)
                if record.get("status") != "complete":
                    continue
                result = Path(record["result_dir"]).resolve()
                trace_dir = run_root / "traces" / profile["id"] / f"seed{seed}" / f"c{cores:02d}" / f"W_{workload['name']}"
                # run_uarch_fastsim expects one trace location per label. Keep
                # the profile in the path because the user requested a fresh
                # FST capture for every microarchitecture combination.
                trace_meta = write_trace_view(result, trace_dir, int(cores))
                legacy_trace_dir = run_root / "traces" / f"seed{seed}" / f"c{cores:02d}" / f"W_{workload['name']}"
                label_dir = run_root / "labels" / profile["id"] / f"c{cores:02d}" / f"W_{workload['name']}"
                metrics = make_metrics(
                    result, profile, workload, int(cores), seed,
                    float(record.get("wall_time_seconds", 0.0)),
                )
                metrics["trace_manifest"] = str((trace_dir / "manifest.txt").resolve())
                metrics["trace_metadata"] = str((trace_dir / "trace.json").resolve())
                metrics["profile_trace_layout"] = True
                atomic_json(label_dir / "metrics.json", metrics)
                atomic_json(label_dir / "complete.json", {
                    "status": "complete", "source_result": str(result),
                    "trace_records": sum(trace_meta["records_per_core"].values()),
                })
                cases += 1
    atomic_json(run_root / "dataset-index.json", {
        "schema": "fastsim-spec2026-uarch-dataset-v1",
        "cases": cases,
        "profile_specific_traces": True,
        "matrix": str(args.matrix.resolve()),
    })
    print(f"materialized cases={cases} root={run_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
