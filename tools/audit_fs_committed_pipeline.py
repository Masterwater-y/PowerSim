#!/usr/bin/env python3
"""Audit FastSim committed-path timing against aligned full-system labels.

The accuracy pipeline already guarantees that every FST measurement slice and
every TaoTrace oracle use the same per-core user-record denominator.  This
tool replays those slices with FastSim's timing-neutral CPI attribution enabled
and joins each FastSim core with the corresponding oracle user cycles and raw
gem5 O3 diagnostics.  Raw O3 counters are deliberately treated as
non-additive diagnostics; they never become FastSim inputs or fitted costs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable


GEM5_COUNTERS = {
    "branch_mispredicts": "commit.branchMispredicts",
    "commit_squashed_insts": "commit.commitSquashedInsts",
    "fetch_squash_cycles": "fetch.status::squashing",
    "fetch_running_cycles": "fetch.status::running",
    "fetch_itlb_wait_cycles": "fetch.status::itlbWait",
    "fetch_icache_wait_response_cycles":
        "fetch.status::icacheWaitResponse",
    "fetch_icache_wait_retry_cycles": "fetch.status::icacheWaitRetry",
    "fetch_misc_stall_cycles": "fetch.miscStallCycles",
    "fetch_cache_lines": "fetch.cacheLines",
    "fetch_predicted_branches": "fetch.predictedBranches",
    "fetch_instructions": "fetchStats0.numInsts",
    "fetch_zero_inst_cycles": "fetch.nisnDist::0",
    "icache_stall_cycles": "fetchStats0.icacheStallCycles",
    "decode_idle_cycles": "decode.status::Idle",
    "decode_blocked_cycles": "decode.status::Blocked",
    "dispatch_blocked_cycles": "iew.dispatchStatus::blocked",
    "iq_full_events": "iew.iqFullEvents",
    "lsq_full_events": "iew.lsqFullEvents",
    "memory_order_violations": "iew.memOrderViolationEvents",
    "rescheduled_loads": "lsq0.rescheduledLoads",
    "rename_blocked_cycles": "rename.status::Blocked",
    "rename_rob_full_events": "rename.ROBFullEvents",
    "rename_iq_full_events": "rename.IQFullEvents",
    "rename_lq_full_events": "rename.LQFullEvents",
    "rename_sq_full_events": "rename.SQFullEvents",
}


FASTSIM_COUNTERS = (
    "branch_misses",
    "branch_penalty_cycles",
    "fetch_buffer_transitions",
    "fetch_buffer_refill_delay_cycles",
    "fetch_block_response_wait_cycles",
    "fetch_block_response_hidden_cycles",
    "fetch_block_response_exposed_cycles",
    "fetch_block_response_to_resume_cycles",
    "fetch_block_request_to_resume_cycles",
    "l1i_miss_stall_cycles",
    "o3_iq_full_events",
    "o3_iq_stall_cycles",
    "o3_rob_full_events",
    "o3_rob_stall_cycles",
    "o3_lq_full_events",
    "o3_lq_stall_cycles",
    "o3_sq_full_events",
    "o3_sq_stall_cycles",
    "o3_tso_store_stall_cycles",
    "response_critical_total_cycles",
    "response_critical_rename_free_list_cycles",
    "response_critical_dispatch_bandwidth_cycles",
    "response_critical_rob_capacity_cycles",
    "response_critical_iq_capacity_cycles",
    "response_critical_lq_capacity_cycles",
    "response_critical_sq_capacity_cycles",
    "response_critical_dependency_cycles",
    "response_critical_sequencer_cycles",
    "response_critical_l1_mshr_cycles",
    "response_critical_l2_mshr_cycles",
    "response_critical_memory_response_cycles",
    "response_critical_commit_bandwidth_cycles",
    "response_critical_tso_store_cycles",
    "response_critical_unattributed_cycles",
    "response_residual_seed_events",
    "response_residual_seed_cycles",
    "response_residual_completion_extended_uops",
    "response_residual_completion_extension_cycles",
    "response_residual_dependency_input_cycles",
    "response_residual_dependency_absorbed_cycles",
    "response_residual_dependency_propagated_cycles",
    "response_residual_retire_input_cycles",
    "response_residual_retire_absorbed_cycles",
    "response_residual_retire_propagated_cycles",
    "response_residual_ordered_retire_moved_uops",
    "response_residual_ordered_retire_moved_cycles",
    "response_residual_dispatch_moved_uops",
    "response_residual_dispatch_moved_cycles",
    "response_residual_memory_issue_moved_events",
    "response_residual_memory_issue_moved_cycles",
)

COMMITTED_AUDIT_COUNTERS = (
    "dispatch_delay_cycles",
    "dispatch_bandwidth_cycles",
    "rob_capacity_cycles",
    "iq_capacity_cycles",
    "lq_capacity_cycles",
    "sq_capacity_cycles",
    "rob_residency_cycles",
    "iq_residency_cycles",
    "memory_iq_post_issue_cycles",
)


def parse_args() -> argparse.Namespace:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pipeline", type=Path, required=True,
        help="pipeline.json from run_kernel_event_accuracy_pipeline.py",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=project / "configs/gem5-v28_1-fs-user.cfg",
        help="Frozen FS user-only profile (default: maintained profile).",
    )
    parser.add_argument("--fastsim", type=Path, default=Path("build/fastsim"))
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--dtlb-miss-model",
        choices=("se_atomic", "timing_walk"),
        default="timing_walk",
        help=(
            "FS translation timing model (default: timing_walk). "
            "se_atomic is retained only as an SE/control audit."
        ),
    )
    parser.add_argument(
        "--dtlb-page-walk-latency",
        type=int,
        default=12,
        help=(
            "Fixed total timing_walk service in cycles (default: 12, the "
            "accepted FS baseline)."
        ),
    )
    parser.add_argument(
        "--fetch-buffer-refill-latency",
        type=int,
        default=1,
        help="Target L0-I resident-hit empty-cycle delay (default: 1).",
    )
    parser.add_argument(
        "--interval-max-cycles", type=int,
        help="Diagnostic Q override; omitted runs the frozen config value.",
    )
    parser.add_argument(
        "--rename-free-list", action="store_true",
        help=(
            "enable exact per-class committed rename free-list timing; "
            "requires complete FST v7 destination metadata"
        ),
    )
    return parser.parse_args()


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    begin = 0
    while begin < len(order):
        end = begin + 1
        while end < len(order) and values[order[end]] == values[order[begin]]:
            end += 1
        rank = (begin + end - 1) / 2.0 + 1.0
        for position in range(begin, end):
            result[order[position]] = rank
        begin = end
    return result


def pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return math.nan
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left, right)
    )
    left_variance = sum((x - left_mean) ** 2 for x in left)
    right_variance = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_variance * right_variance)
    return numerator / denominator if denominator else math.nan


def spearman(rows: list[dict[str, Any]], key: str) -> float:
    return pearson(
        ranks([float(row["cpi_gap"]) for row in rows]),
        ranks([float(row[key]) for row in rows]),
    )


def parse_gem5_stats(path: Path, cores: int) -> list[dict[str, int]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    result = [dict.fromkeys(GEM5_COUNTERS, 0) for _ in range(cores)]
    for output, suffix in GEM5_COUNTERS.items():
        pattern = re.compile(
            rf"^board\.processor\.switch(\d+)\.core\."
            rf"{re.escape(suffix)}\s+([0-9.eE+-]+)",
            re.MULTILINE,
        )
        for core_text, value in pattern.findall(text):
            core = int(core_text)
            if core >= cores:
                raise ValueError(f"{path}: counter for unexpected core {core}")
            result[core][output] = int(float(value))
    return result


def run_case(
    case: dict[str, Any], output: Path, fastsim: Path, config: Path,
    force: bool, interval_max_cycles: int | None, rename_free_list: bool,
    dtlb_miss_model: str, dtlb_page_walk_latency: int,
    fetch_buffer_refill_latency: int,
) -> list[dict[str, Any]]:
    workload = str(case["workload"])
    cores = int(case["cores"])
    audit_variant = "rename-free-list" if rename_free_list else "audit-only"
    dtlb_variant = (
        f"timing-walk-{dtlb_page_walk_latency}"
        if dtlb_miss_model == "timing_walk"
        else "se-atomic"
    )
    variant = (
        f"{audit_variant}-{dtlb_variant}-fetch-"
        f"{fetch_buffer_refill_latency}"
    )
    report = output / "cases" / f"{cores:02d}c-{workload}-{variant}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    if force or not report.is_file():
        command = [
            str(fastsim), "simulate", "--config", str(config),
            "--manifest", str(Path(case["manifest"]).resolve()),
            "--measurement-scope", "user", "--cores", str(cores),
            "--allow-cross-page-without-virtual-token", "true",
            "--fetch-buffer-refill-latency",
            str(fetch_buffer_refill_latency),
            "--dtlb-miss-model", dtlb_miss_model,
            "--allow-mmio-escape", "true",
            "--dram-size", str(3 * 1024**3), "--cpi-attribution", "true",
            "--committed-pipeline-audit", "true",
            "--output", str(report),
        ]
        if dtlb_miss_model == "timing_walk":
            command.extend([
                "--dtlb-page-walk-latency",
                str(dtlb_page_walk_latency),
            ])
        if rename_free_list:
            command.extend(["--rename-free-list", "true"])
        if interval_max_cycles is not None:
            command.extend([
                "--interval-max-cycles", str(interval_max_cycles),
            ])
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"{cores}c/{workload}: exit={completed.returncode}: "
                f"{completed.stdout}"
            )

    stats = load(report)
    if stats.get("measurement_scope") != "user":
        raise ValueError(f"{report}: expected user measurement scope")
    if not bool(stats["totals"]["response_critical_conserved"]):
        raise ValueError(f"{report}: response critical ledger is not conserved")
    if not bool(stats["totals"]["response_residual_dependency_conserved"]):
        raise ValueError(f"{report}: dependency residual is not conserved")
    if not bool(stats["totals"]["response_residual_retire_conserved"]):
        raise ValueError(f"{report}: retirement residual is not conserved")
    if not bool(stats["totals"]["fetch_block_response_conserved"]):
        raise ValueError(f"{report}: fetch-block response ledger is not conserved")

    result_dir = Path(case["result_dir"]).resolve()
    oracle = load(Path(case["oracle"]).resolve())
    oracle_cores = {
        int(row["core_id"]): row for row in oracle.get("per_core", [])
    }
    raw = parse_gem5_stats(result_dir / "stats.txt", cores)
    fastsim_cores = stats.get("cores", [])
    if len(fastsim_cores) != cores or len(oracle_cores) != cores:
        raise ValueError(
            f"{report}: core cardinality mismatch "
            f"FastSim={len(fastsim_cores)} oracle={len(oracle_cores)}"
        )

    rows = []
    for core, fastsim_core in enumerate(fastsim_cores):
        reference = oracle_cores[core]
        if not bool(fastsim_core["fetch_block_response_conserved"]):
            raise ValueError(
                f"{report}: core {core} fetch-block response ledger is not conserved"
            )
        uops = int(reference["n_user"])
        if int(case["measurement_records"]) != sum(
            int(row["n_user"]) for row in oracle_cores.values()
        ):
            raise ValueError(f"{report}: aggregate measurement denominator mismatch")
        fastsim_cycles = int(fastsim_core["cycles"])
        reference_cycles = int(reference["user_cycles"])
        committed = fastsim_core.get("committed_pipeline_audit")
        if not isinstance(committed, dict):
            raise ValueError(f"{report}: core {core} committed audit is missing")
        if (
            int(committed.get("uops", -1)) != uops
            or int(committed.get("destination_class_uops", -1)) != uops
            or not bool(committed.get("destination_conserved"))
            or not bool(committed.get("destination_classes_conserved"))
            or not bool(committed.get("dispatch_conserved"))
        ):
            raise ValueError(
                f"{report}: core {core} destination/dispatch audit is "
                "incomplete or not conserved"
            )
        classes = {
            str(item["class"]): item
            for item in committed.get("destination_classes", [])
        }
        if set(classes) != {"int", "float", "vec", "cc"}:
            raise ValueError(
                f"{report}: core {core} destination classes are incomplete"
            )
        row: dict[str, Any] = {
            "workload": workload,
            "cores": cores,
            "core": core,
            "user_uops": uops,
            "fastsim_cycles": fastsim_cycles,
            "reference_cycles": reference_cycles,
            "fastsim_trace_instructions": int(fastsim_core["instructions"]),
            "fastsim_trace_uops_per_instruction": (
                uops / int(fastsim_core["instructions"])
            ),
            "cpi_gap_cycles": reference_cycles - fastsim_cycles,
            "fastsim_cpi": fastsim_cycles / uops,
            "reference_cpi": reference_cycles / uops,
            "cpi_gap": (reference_cycles - fastsim_cycles) / uops,
            "signed_error_percent":
                (fastsim_cycles / reference_cycles - 1.0) * 100.0,
            "report": str(report),
            "fastsim_committed_destination_tokens": int(
                committed["destination_tokens"]
            ),
            "fastsim_committed_destination_tokens_per_uop": (
                int(committed["destination_tokens"]) / uops
            ),
            "fastsim_committed_max_live_destination_tokens": int(
                committed["max_live_destination_tokens"]
            ),
            "fastsim_committed_rename_free_list_stall_cycles": int(
                committed["rename_free_list_stall_cycles"]
            ),
        }
        for name, item in classes.items():
            row[f"fastsim_committed_{name}_tokens"] = int(item["tokens"])
            row[f"fastsim_committed_{name}_tokens_per_uop"] = (
                int(item["tokens"]) / uops
            )
            row[f"fastsim_committed_{name}_max_live_tokens"] = int(
                item["max_live_tokens"]
            )
        for key in COMMITTED_AUDIT_COUNTERS:
            value = int(committed[key])
            row[f"fastsim_committed_{key}"] = value
            row[f"fastsim_committed_{key}_per_uop"] = value / uops
        for key in FASTSIM_COUNTERS:
            value = int(fastsim_core.get(key, 0))
            row[f"fastsim_{key}"] = value
            row[f"fastsim_{key}_per_uop"] = value / uops
        for key, value in raw[core].items():
            row[f"gem5_{key}"] = value
            row[f"gem5_{key}_per_uop"] = value / uops
        rows.append(row)
    return rows


def aggregate_workloads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((int(row["cores"]), str(row["workload"])), []).append(row)
    result = []
    for (cores, workload), group in sorted(groups.items()):
        uops = sum(int(row["user_uops"]) for row in group)
        fastsim_cycles = sum(int(row["fastsim_cycles"]) for row in group)
        reference_cycles = sum(int(row["reference_cycles"]) for row in group)
        result.append({
            "cores": cores,
            "workload": workload,
            "user_uops": uops,
            "fastsim_cpi": fastsim_cycles / uops,
            "reference_cpi": reference_cycles / uops,
            "cpi_gap": (reference_cycles - fastsim_cycles) / uops,
            "signed_error_percent":
                (fastsim_cycles / reference_cycles - 1.0) * 100.0,
            "fastsim_response_critical_cycles_per_uop": sum(
                int(row["fastsim_response_critical_total_cycles"])
                for row in group
            ) / uops,
            "fastsim_trace_uops_per_instruction": sum(
                int(row["user_uops"]) for row in group
            ) / sum(
                int(row["fastsim_trace_instructions"]) for row in group
            ),
            "fastsim_fetch_buffer_transitions_per_uop": sum(
                int(row["fastsim_fetch_buffer_transitions"])
                for row in group
            ) / uops,
            "fastsim_fetch_buffer_refill_delay_cycles_per_uop": sum(
                int(row["fastsim_fetch_buffer_refill_delay_cycles"])
                for row in group
            ) / uops,
            "fastsim_dependency_critical_cycles_per_uop": sum(
                int(row["fastsim_response_critical_dependency_cycles"])
                for row in group
            ) / uops,
            "fastsim_memory_critical_cycles_per_uop": sum(
                int(row["fastsim_response_critical_memory_response_cycles"])
                for row in group
            ) / uops,
            "gem5_icache_stall_cycles_per_uop": sum(
                int(row["gem5_icache_stall_cycles"]) for row in group
            ) / uops,
            "gem5_fetch_cache_lines_per_uop": sum(
                int(row["gem5_fetch_cache_lines"]) for row in group
            ) / uops,
            "gem5_fetch_zero_inst_cycles_per_uop": sum(
                int(row["gem5_fetch_zero_inst_cycles"]) for row in group
            ) / uops,
            "gem5_commit_squashed_insts_per_uop": sum(
                int(row["gem5_commit_squashed_insts"]) for row in group
            ) / uops,
            "gem5_rename_iq_full_events_per_uop": sum(
                int(row["gem5_rename_iq_full_events"]) for row in group
            ) / uops,
            "fastsim_committed_destination_tokens_per_uop": sum(
                int(row["fastsim_committed_destination_tokens"])
                for row in group
            ) / uops,
            "fastsim_committed_max_live_destination_tokens": max(
                int(row["fastsim_committed_max_live_destination_tokens"])
                for row in group
            ),
            "fastsim_committed_rename_free_list_stall_cycles_per_uop": sum(
                int(row["fastsim_committed_rename_free_list_stall_cycles"])
                for row in group
            ) / uops,
        })
        for name in ("int", "float", "vec", "cc"):
            result[-1][f"fastsim_committed_{name}_tokens_per_uop"] = sum(
                int(row[f"fastsim_committed_{name}_tokens"])
                for row in group
            ) / uops
            result[-1][f"fastsim_committed_{name}_max_live_tokens"] = max(
                int(row[f"fastsim_committed_{name}_max_live_tokens"])
                for row in group
            )
        for key in COMMITTED_AUDIT_COUNTERS:
            result[-1][f"fastsim_committed_{key}_per_uop"] = sum(
                int(row[f"fastsim_committed_{key}"]) for row in group
            ) / uops
        for key in (
            "fetch_block_response_wait_cycles",
            "fetch_block_response_hidden_cycles",
            "fetch_block_response_exposed_cycles",
            "fetch_block_response_to_resume_cycles",
            "fetch_block_request_to_resume_cycles",
        ):
            result[-1][f"fastsim_{key}_per_uop"] = sum(
                int(row[f"fastsim_{key}"]) for row in group
            ) / uops
    return result


def write_outputs(
    rows: list[dict[str, Any]], workloads: list[dict[str, Any]], output: Path,
    pipeline: Path, config: Path, interval_max_cycles: int | None,
    rename_free_list: bool, dtlb_miss_model: str,
    dtlb_page_walk_latency: int, fetch_buffer_refill_latency: int,
) -> None:
    output.mkdir(parents=True, exist_ok=True)
    correlation_keys = (
        "fastsim_trace_uops_per_instruction",
        "fastsim_fetch_buffer_transitions_per_uop",
        "fastsim_fetch_buffer_refill_delay_cycles_per_uop",
        "fastsim_fetch_block_response_wait_cycles_per_uop",
        "fastsim_fetch_block_response_hidden_cycles_per_uop",
        "fastsim_fetch_block_response_exposed_cycles_per_uop",
        "fastsim_fetch_block_response_to_resume_cycles_per_uop",
        "fastsim_fetch_block_request_to_resume_cycles_per_uop",
        "gem5_fetch_cache_lines_per_uop",
        "gem5_fetch_zero_inst_cycles_per_uop",
        "gem5_fetch_icache_wait_response_cycles_per_uop",
        "gem5_fetch_itlb_wait_cycles_per_uop",
        "gem5_decode_idle_cycles_per_uop",
        "gem5_decode_blocked_cycles_per_uop",
        "gem5_dispatch_blocked_cycles_per_uop",
        "gem5_fetch_squash_cycles_per_uop",
        "fastsim_committed_dispatch_delay_cycles_per_uop",
        "fastsim_committed_dispatch_bandwidth_cycles_per_uop",
        "fastsim_committed_rob_capacity_cycles_per_uop",
        "fastsim_committed_iq_capacity_cycles_per_uop",
        "fastsim_committed_lq_capacity_cycles_per_uop",
        "fastsim_committed_sq_capacity_cycles_per_uop",
        "fastsim_committed_rob_residency_cycles_per_uop",
        "fastsim_committed_iq_residency_cycles_per_uop",
        "fastsim_committed_memory_iq_post_issue_cycles_per_uop",
        "gem5_icache_stall_cycles_per_uop",
        "gem5_commit_squashed_insts_per_uop",
        "gem5_branch_mispredicts_per_uop",
        "gem5_rename_blocked_cycles_per_uop",
        "gem5_rename_rob_full_events_per_uop",
        "gem5_rename_iq_full_events_per_uop",
        "gem5_rename_lq_full_events_per_uop",
        "gem5_rename_sq_full_events_per_uop",
        "fastsim_response_critical_total_cycles_per_uop",
        "fastsim_response_critical_dependency_cycles_per_uop",
        "fastsim_response_critical_memory_response_cycles_per_uop",
    )
    correlations = {key: spearman(rows, key) for key in correlation_keys}
    underpredicted = [row for row in rows if float(row["cpi_gap"]) > 0.0]
    underprediction_correlations = {
        key: spearman(underpredicted, key) for key in correlation_keys
    }
    errors = [abs(float(row["signed_error_percent"])) for row in workloads]
    document = {
        "schema": "fastsim-fs-committed-pipeline-audit-v2",
        "pipeline": str(pipeline),
        "config": str(config),
        "config_sha256": sha256(config),
        "interval_max_cycles_override": interval_max_cycles,
        "rename_free_list": rename_free_list,
        "dtlb_miss_model": dtlb_miss_model,
        "dtlb_page_walk_latency": (
            dtlb_page_walk_latency
            if dtlb_miss_model == "timing_walk"
            else None
        ),
        "fetch_buffer_refill_latency": fetch_buffer_refill_latency,
        "cases": len(workloads),
        "core_rows": len(rows),
        "cpi_ape_percent": {
            "mean": sum(errors) / len(errors),
            "p50": percentile(errors, 0.50),
            "p90": percentile(errors, 0.90),
            "p99": percentile(errors, 0.99),
        },
        "correlations": correlations,
        "underprediction_only_correlations": underprediction_correlations,
        "workloads": workloads,
        "rows": rows,
    }
    (output / "summary.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (output / "rows.csv").open("w", newline="", encoding="utf-8") as sink:
        writer = csv.DictWriter(sink, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = [
        "# FS committed-path CPI audit",
        "",
        "Raw gem5 stall counters below are diagnostic and non-additive. They are "
        "not FastSim inputs and are not converted into fitted cycle costs.",
        "",
        f"- cases/core rows: {len(workloads)}/{len(rows)}",
        "- DTLB model: " + (
            f"timing_walk/{dtlb_page_walk_latency} cycles"
            if dtlb_miss_model == "timing_walk"
            else "se_atomic (SE/control only)"
        ),
        f"- fetch-buffer refill latency: {fetch_buffer_refill_latency} cycle(s)",
        f"- FS profile: `{config}` (`sha256={sha256(config)}`)",
        "- CPI APE mean/P50/P90/P99: "
        f"{document['cpi_ape_percent']['mean']:.3f}% / "
        f"{document['cpi_ape_percent']['p50']:.3f}% / "
        f"{document['cpi_ape_percent']['p90']:.3f}% / "
        f"{document['cpi_ape_percent']['p99']:.3f}%",
        "- response critical, dependency, retirement and fetch-response "
        "ledgers: PASS",
        "",
        "## Workload-equal CPI and timing signals",
        "",
        "| workload | FS CPI | gem5 CPI | signed error | CPI gap | "
        "FS response critical/uop | dependency/uop | memory/uop | "
        "trace uop/inst | fetch blocks/uop | "
        "dest/uop | max live dest | free-list stall/uop | "
        "gem5 I-cache stall/uop | squashed/uop | rename IQ-full/uop |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in workloads:
        report.append(
            f"| {row['cores']:02d}c-{row['workload']} | "
            f"{row['fastsim_cpi']:.6f} | {row['reference_cpi']:.6f} | "
            f"{row['signed_error_percent']:+.3f}% | {row['cpi_gap']:+.6f} | "
            f"{row['fastsim_response_critical_cycles_per_uop']:.4f} | "
            f"{row['fastsim_dependency_critical_cycles_per_uop']:.4f} | "
            f"{row['fastsim_memory_critical_cycles_per_uop']:.4f} | "
            f"{row['fastsim_trace_uops_per_instruction']:.4f} | "
            f"{row['fastsim_fetch_buffer_transitions_per_uop']:.4f} | "
            f"{row['fastsim_committed_destination_tokens_per_uop']:.4f} | "
            f"{row['fastsim_committed_max_live_destination_tokens']} | "
            f"{row['fastsim_committed_rename_free_list_stall_cycles_per_uop']:.4f} | "
            f"{row['gem5_icache_stall_cycles_per_uop']:.4f} | "
            f"{row['gem5_commit_squashed_insts_per_uop']:.4f} | "
            f"{row['gem5_rename_iq_full_events_per_uop']:.4f} |"
        )
    report.extend([
        "",
        "## Per-core rank correlations with CPI gap",
        "",
        "These correlations rank candidate mechanism families only; overlapping "
        "gem5 counters are not a cycle decomposition.",
        "",
        "| signal | Spearman rho |",
        "|---|---:|",
    ])
    for key, value in correlations.items():
        report.append(f"| {key} | {value:.3f} |")
    report.extend([
        "",
        "## Per-core rank correlations within underpredicted rows",
        "",
        "This removes already-overpredicted cases where adding any positive "
        "penalty is directionally invalid.",
        "",
        "| signal | Spearman rho |",
        "|---|---:|",
    ])
    for key, value in underprediction_correlations.items():
        report.append(f"| {key} | {value:.3f} |")
    report.append("")
    (output / "summary.md").write_text("\n".join(report), encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.jobs <= 0:
        raise SystemExit("--jobs must be positive")
    if args.dtlb_page_walk_latency <= 0:
        raise SystemExit("--dtlb-page-walk-latency must be positive")
    if args.fetch_buffer_refill_latency <= 0:
        raise SystemExit("--fetch-buffer-refill-latency must be positive")
    pipeline_path = args.pipeline.resolve()
    pipeline = load(pipeline_path)
    cases = pipeline.get("cases", [])
    if not cases:
        raise SystemExit(f"{pipeline_path}: no cases")
    config = args.config.resolve()
    fastsim = args.fastsim.resolve()
    output = args.output.resolve()
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
        futures = {
            executor.submit(
                run_case, case, output, fastsim, config, args.force,
                args.interval_max_cycles, args.rename_free_list,
                args.dtlb_miss_model, args.dtlb_page_walk_latency,
                args.fetch_buffer_refill_latency,
            ): case
            for case in cases
        }
        for future in as_completed(futures):
            case = futures[future]
            try:
                rows.extend(future.result())
                print(
                    f"[done] {int(case['cores']):02d}c/{case['workload']}",
                    flush=True,
                )
            except Exception as error:  # noqa: BLE001 - retain all failures
                failures.append(str(error))
                print(f"[fail] {error}", flush=True)
    if failures:
        raise SystemExit("\n".join(failures[:20]))
    rows.sort(key=lambda row: (row["cores"], row["workload"], row["core"]))
    workloads = aggregate_workloads(rows)
    write_outputs(
        rows, workloads, output, pipeline_path, config,
        args.interval_max_cycles, args.rename_free_list,
        args.dtlb_miss_model, args.dtlb_page_walk_latency,
        args.fetch_buffer_refill_latency,
    )
    print(f"wrote {output / 'summary.md'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
