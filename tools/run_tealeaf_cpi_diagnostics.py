#!/usr/bin/env python3
"""Run the TeaLeaf C4/C8 single-factor CPI and request-lifecycle matrix.

The matrix is diagnostic only.  It leaves the maintained production alias
untouched, disables the materialized host fast path solely so timing-neutral
attribution can run, and compares that generic baseline back to the production
result before interpreting any factor.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


WORKLOAD = "811.tealeaf_s"

COMMON_OVERRIDES: dict[str, Any] = {
    "sim.interval_max_cycles": 1024,
    "sim.cpi_attribution": True,
    "sim.interval_causal_timing": False,
    "core.committed_pipeline_audit": True,
    "core.response_materialized_uop_fast_kernel": False,
    "core.response_sparse_resource_repair": False,
    "core.response_frontier_audit_stride_uops": 65536,
    "core.response_paired_frontier": False,
    "core.store_post_commit_request": False,
    "cache.l1i.speculative_path_state": False,
    "dtlb.speculative_path_state": False,
    "dram.t_ras": 0,
    "dram.t_rtp": 0,
    "dram.t_rrd": 0,
    "dram.t_rrd_l": 0,
    "dram.t_xaw": 0,
    "dram.activation_limit": 0,
    "dram.t_ccd_l": 0,
    "dram.t_cs": 0,
}

VARIANTS: dict[str, dict[str, Any]] = {
    "q0128": {
        "factor": "Q",
        "description": "Q sensitivity at sim.interval_max_cycles=128",
        "overrides": {"sim.interval_max_cycles": 128},
    },
    "q0256": {
        "factor": "Q",
        "description": "Q sensitivity at sim.interval_max_cycles=256",
        "overrides": {"sim.interval_max_cycles": 256},
    },
    "baseline_q1024": {
        "factor": "baseline",
        "description": "v28.6 generic attribution baseline; Q=1024",
        "overrides": {},
    },
    "q0512": {
        "factor": "Q",
        "description": "Q sensitivity at sim.interval_max_cycles=512",
        "overrides": {"sim.interval_max_cycles": 512},
    },
    "q2048": {
        "factor": "Q",
        "description": "Q sensitivity at sim.interval_max_cycles=2048",
        "overrides": {"sim.interval_max_cycles": 2048},
    },
    "legacy_causal_timing_on": {
        "factor": "causal timing",
        "description": (
            "existing timing-only fixed-point replay; not the proposed "
            "incremental P1 causal closure"
        ),
        "overrides": {"sim.interval_causal_timing": True},
    },
    "store_post_commit_on": {
        "factor": "store request edge",
        "description": "move regular store hierarchy requests to post-commit",
        "overrides": {"core.store_post_commit_request": True},
    },
    "sparse_resource_repair_on": {
        "factor": "response resource calendar",
        "description": (
            "reallocate issue/FU/cache-port/writeback slots only inside "
            "response causal cones"
        ),
        "overrides": {"core.response_sparse_resource_repair": True},
    },
    "dtlb_speculative_path_on": {
        "factor": "wrong-path DTLB state",
        "description": (
            "state-only DTLB accesses for tracked memory PCs on the "
            "predicted instruction path; committed L1I state is unchanged"
        ),
        "overrides": {"dtlb.speculative_path_state": True},
    },
    "ddr_act_on": {
        "factor": "DDR ACT calendar",
        "description": "enable only ACT/precharge/rank-activation constraints",
        "overrides": {
            "dram.t_ras": 96,
            "dram.t_rrd": 11,
            "dram.t_rrd_l": 15,
            "dram.t_xaw": 64,
            "dram.activation_limit": 4,
        },
    },
    "ddr_column_rank_on": {
        "factor": "DDR column/rank calendar",
        "description": "enable only RTP, same-group column, and rank-switch spacing",
        "overrides": {
            "dram.t_rtp": 23,
            "dram.t_ccd_l": 16,
            "dram.t_cs": 5,
        },
    },
    "ddr_combined": {
        "factor": "DDR combined calendar",
        "description": "enable ACT and column/rank timing groups together",
        "overrides": {
            "dram.t_ras": 96,
            "dram.t_rtp": 23,
            "dram.t_rrd": 11,
            "dram.t_rrd_l": 15,
            "dram.t_xaw": 64,
            "dram.activation_limit": 4,
            "dram.t_ccd_l": 16,
            "dram.t_cs": 5,
        },
    },
}

FRONTIER_COUNTERS = (
    "causal_timing_candidate_epochs",
    "causal_timing_noop_epochs",
    "causal_timing_stable_epochs",
    "causal_timing_fallback_epochs",
    "causal_timing_deferred_epochs",
    "causal_closure_components",
    "causal_closure_events",
    "causal_timing_passes",
    "causal_timing_replayed_events",
    "store_post_commit_request_events",
    "store_post_commit_request_delay_cycles",
    "store_post_commit_request_candidate_epochs",
    "store_post_commit_request_stable_epochs",
    "store_post_commit_request_fallback_epochs",
    "store_post_commit_request_horizon_fallback_epochs",
    "store_post_commit_request_replayed_events",
    "sparse_resource_candidates",
    "sparse_resource_issue_moves",
    "sparse_resource_issue_collision_cycles",
    "sparse_resource_writeback_moves",
    "sparse_resource_writeback_collision_cycles",
)

BASELINE_Q_SEQUENCE = (
    "q0128",
    "q0256",
    "q0512",
    "baseline_q1024",
    "q2048",
)

def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.3f}%"


def ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def frontier_counters(stats: dict[str, Any]) -> dict[str, int]:
    frontier = stats.get("causal_frontier", {})
    if not isinstance(frontier, dict):
        raise ValueError("causal_frontier must be an object")
    return {key: int(frontier.get(key, 0)) for key in FRONTIER_COUNTERS}


def selected_variants(names: list[str]) -> list[str]:
    if not names:
        return list(VARIANTS)
    unknown = sorted(set(names) - set(VARIANTS))
    if unknown:
        raise ValueError("unknown variants: " + ", ".join(unknown))
    return [name for name in VARIANTS if name in set(names)]


def variant_overrides(name: str) -> dict[str, Any]:
    result = dict(COMMON_OVERRIDES)
    result.update(VARIANTS[name]["overrides"])
    return result


def runner_command(args: argparse.Namespace, name: str) -> list[str]:
    command = [
        str(args.python),
        str(args.runner),
        "--root",
        str(args.root),
        "--out",
        str(args.out / name),
        "--matrix",
        str(args.matrix),
        "--config",
        str(args.config),
        "--fastsim",
        str(args.fastsim),
        "--jobs",
        "1",
        "--timeout",
        str(args.timeout),
        "--uarch",
        "baseline",
        "--workload",
        WORKLOAD,
        "--experiment-id",
        name,
    ]
    if args.force:
        command.append("--force")
    for key, value in sorted(variant_overrides(name).items()):
        if isinstance(value, bool):
            encoded = "true" if value else "false"
        else:
            encoded = str(value)
        command.extend(["--config-override", f"{key}={encoded}"])
    return command


def run_variant(args: argparse.Namespace, name: str) -> tuple[str, str | None]:
    command = runner_command(args, name)
    print(f"[variant start] {name}", flush=True)
    completed = subprocess.run(
        command,
        cwd=args.project,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stdout, end="", flush=True)
        return name, f"runner exit={completed.returncode}"
    final_line = completed.stdout.rstrip().splitlines()[-1]
    print(f"[variant done ] {name}: {final_line}", flush=True)
    return name, None


def cha_breakdown(stats: dict[str, Any]) -> dict[str, int | bool]:
    requests = sum(int(item["requests"]) for item in stats["cha"])
    upgrades = sum(int(item["upgrades"]) for item in stats["cha"])
    if upgrades > requests:
        raise ValueError("CHA permission upgrades exceed requests")
    return {
        "total_requests": requests,
        "demand_lookups": requests - upgrades,
        "permission_upgrades": upgrades,
        "remote_supplies": sum(
            int(item["remote_supplies"]) for item in stats["cha"]
        ),
        "dram_reads": sum(int(item["dram_reads"]) for item in stats["cha"]),
        "outcomes_conserved": all(
            bool(item["llc_outcomes_conserved"]) for item in stats["cha"]
        ),
    }


def result_dir(output: Path, variant: str, cores: int) -> Path:
    return output / variant / "baseline" / f"c{cores:02d}" / f"W_{WORKLOAD}"


def label_path(root: Path, cores: int) -> Path:
    return root / "labels" / "baseline" / f"c{cores:02d}" / f"W_{WORKLOAD}" / "metrics.json"


def flatten_lifecycle(row: dict[str, Any], lifecycle: dict[str, Any]) -> None:
    for key, value in lifecycle.items():
        row[f"lifecycle_{key}"] = value
    requests = int(lifecycle["requests"])
    for key in (
        "candidate_to_corrected_issue_cycles",
        "corrected_issue_to_controller_arrival_cycles",
        "controller_arrival_to_service_cycles",
        "controller_service_to_response_cycles",
        "response_to_retire_cycles",
        "adjacent_backward_cycles",
        "corrected_issue_after_controller_arrival_cycles",
        "unprojected_issue_after_controller_arrival_cycles",
        "shared_stage_projection_forward_cycles",
        "shared_stage_projection_backward_cycles",
        "response_after_retire_cycles",
        "candidate_to_retire_cycles",
    ):
        row[f"lifecycle_{key}_per_request"] = ratio(
            int(lifecycle[key]), requests
        )
    row["lifecycle_corrected_issue_after_controller_arrival_rate"] = ratio(
        int(lifecycle["corrected_issue_after_controller_arrival_events"]),
        requests,
    )
    row["lifecycle_unprojected_issue_after_controller_arrival_rate"] = ratio(
        int(lifecycle["unprojected_issue_after_controller_arrival_events"]),
        requests,
    )
    row["lifecycle_shared_stage_projection_rate"] = ratio(
        int(lifecycle["shared_stage_projection_events"]), requests
    )
    row["lifecycle_response_after_retire_rate"] = ratio(
        int(lifecycle["response_after_retire_events"]), requests
    )


def collect_rows(args: argparse.Namespace, variants: list[str]) -> list[dict[str, Any]]:
    baseline_by_core: dict[int, float] = {}
    production_by_core: dict[int, float] = {}
    for cores in (4, 8):
        production = (
            args.production_root
            / "baseline"
            / f"c{cores:02d}"
            / f"W_{WORKLOAD}"
            / "fastsim-stats.json"
        )
        if production.is_file():
            production_by_core[cores] = float(
                load(production)["scope_metrics"]["cycles_per_user_uop"]
            )
        baseline = result_dir(args.out, "baseline_q1024", cores)
        if baseline.is_dir():
            baseline_by_core[cores] = float(
                load(baseline / "fastsim-stats.json")["scope_metrics"]
                ["cycles_per_user_uop"]
            )

    rows: list[dict[str, Any]] = []
    for variant in variants:
        for cores in (4, 8):
            directory = result_dir(args.out, variant, cores)
            stats_path = directory / "fastsim-stats.json"
            run_path = directory / "run.json"
            if not stats_path.is_file() or not run_path.is_file():
                raise ValueError(f"missing completed result: {directory}")
            stats = load(stats_path)
            run = load(run_path)
            label = load(label_path(args.root, cores))
            config = stats["configuration"]
            dram = config["dram"]
            totals = stats["totals"]
            scope = stats["scope_metrics"]
            lifecycle = totals.get("dram_request_lifecycle")
            if not isinstance(lifecycle, dict):
                raise ValueError(f"{stats_path}: missing DRAM request lifecycle")
            cpi = float(scope["cycles_per_user_uop"])
            sum_core_cycles = int(totals["sum_core_cycles"])
            response_critical_cycles = int(
                totals["response_critical_total_cycles"]
            )
            if response_critical_cycles > sum_core_cycles:
                raise ValueError(
                    f"{stats_path}: response-critical cycles exceed total cycles"
                )
            reference = float(label["aggregate_uop_cpi"])
            baseline = baseline_by_core.get(cores)
            production = production_by_core.get(cores)
            cha = cha_breakdown(stats)
            row: dict[str, Any] = {
                "variant": variant,
                "factor": VARIANTS[variant]["factor"],
                "description": VARIANTS[variant]["description"],
                "cores": cores,
                "gem5_uop_cpi": reference,
                "fastsim_uop_cpi": cpi,
                "sum_core_cycles": sum_core_cycles,
                "response_critical_total_cycles": response_critical_cycles,
                "non_response_lower_bound_cycles": (
                    sum_core_cycles - response_critical_cycles
                ),
                "response_critical_conserved": bool(
                    totals["response_critical_conserved"]
                ),
                "response_frontier_audit_samples": sum(
                    len(core.get("response_frontier_audit", []))
                    for core in stats["cores"]
                ),
                "response_paired_frontier": bool(
                    config.get("response_paired_frontier", False)
                ),
                "response_frontier_checkpoints": int(
                    totals.get("response_frontier_checkpoints", 0)
                ),
                "response_frontier_settled_checkpoints": int(
                    totals.get("response_frontier_settled_checkpoints", 0)
                ),
                "response_frontier_open_checkpoints": int(
                    totals.get("response_frontier_open_checkpoints", 0)
                ),
                "response_frontier_closed_gap_cycles": int(
                    totals.get("response_frontier_closed_gap_cycles", 0)
                ),
                "response_frontier_open_frontier_cycles": int(
                    totals.get(
                        "response_frontier_open_frontier_cycles", 0
                    )
                ),
                "response_frontier_final_open_frontier_cycles": int(
                    totals.get(
                        "response_frontier_final_open_frontier_cycles", 0
                    )
                ),
                "response_frontier_final_tail_cycles": int(
                    totals.get("response_frontier_final_tail_cycles", 0)
                ),
                "response_frontier_total_critical_cycles": int(
                    totals.get(
                        "response_frontier_total_critical_cycles", 0
                    )
                ),
                "response_frontier_conserved": bool(
                    totals.get("response_frontier_conserved", True)
                ),
                "cpi_signed_error": cpi / reference - 1.0,
                "cpi_delta_vs_q1024": (
                    cpi / baseline - 1.0 if baseline else None
                ),
                "production_uop_cpi": production,
                "generic_vs_production_cpi_delta": (
                    cpi / production - 1.0
                    if variant == "baseline_q1024" and production
                    else None
                ),
                "user_uops_per_second": float(
                    scope["throughput"]["user_uops_per_second"]
                ),
                "fastsim_dtlb_misses": int(scope["pmu"]["dtlb_misses"]),
                "gem5_dtlb_misses": int(label["dtlb_misses"]),
                "dtlb_miss_signed_error": (
                    int(scope["pmu"]["dtlb_misses"])
                    / int(label["dtlb_misses"])
                    - 1.0
                    if int(label["dtlb_misses"])
                    else None
                ),
                "speculative_dtlb_accesses": int(
                    totals.get("speculative_dtlb_accesses", 0)
                ),
                "speculative_dtlb_hits": int(
                    totals.get("speculative_dtlb_hits", 0)
                ),
                "speculative_dtlb_misses": int(
                    totals.get("speculative_dtlb_misses", 0)
                ),
                "speculative_dtlb_untracked": int(
                    totals.get("speculative_dtlb_untracked", 0)
                ),
                "wall_time_seconds": float(run["wall_time_seconds"]),
                "interval_max_cycles": int(config["interval_max_cycles"]),
                "interval_causal_timing": bool(config["interval_causal_timing"]),
                "store_post_commit_request": bool(
                    config["store_post_commit_request"]
                ),
                "dram_t_ras": int(dram["t_ras"]),
                "dram_t_rtp": int(dram["t_rtp"]),
                "dram_t_rrd": int(dram["t_rrd"]),
                "dram_t_rrd_l": int(dram["t_rrd_l"]),
                "dram_t_xaw": int(dram["t_xaw"]),
                "dram_activation_limit": int(dram["activation_limit"]),
                "dram_t_ccd_l": int(dram["t_ccd_l"]),
                "dram_t_cs": int(dram["t_cs"]),
                "corrected_horizon_violations": int(
                    totals["committed_epoch_audit"][
                        "corrected_horizon_violations"
                    ]
                ),
                "corrected_issue_beyond_horizon_events": int(
                    totals["committed_epoch_audit"][
                        "corrected_issue_beyond_horizon_events"
                    ]
                ),
                "sparse_cross_epoch_edges": int(
                    totals["committed_epoch_audit"]["sparse_cross_epoch_edges"]
                ),
                "response_stage_conserved": bool(
                    totals["response_residual_stage_conserved"]
                ),
                "epoch_memory_events_conserved": bool(
                    totals["committed_epoch_audit"]["memory_events_conserved"]
                ),
                "fastsim_sha256": run["hashes"]["fastsim_sha256"],
                "effective_configuration_sha256": run["hashes"]
                ["effective_configuration_sha256"],
                "stats": str(stats_path.resolve()),
            }
            row.update(frontier_counters(stats))
            for key, value in cha.items():
                row[f"cha_{key}"] = value
            flatten_lifecycle(row, lifecycle)
            rows.append(row)
    return rows


def adjacent_q_changes(
    rows: list[dict[str, Any]], sequence: tuple[str, ...] = BASELINE_Q_SEQUENCE
) -> list[dict[str, Any]]:
    by_key = {(row["variant"], row["cores"]): row for row in rows}
    result = []
    for coarse, fine in zip(sequence, sequence[1:]):
        for cores in (4, 8):
            left = by_key[(coarse, cores)]
            right = by_key[(fine, cores)]
            sum_core_cycle_delta = (
                int(left["sum_core_cycles"])
                - int(right["sum_core_cycles"])
            )
            response_critical_cycle_delta = (
                int(left["response_critical_total_cycles"])
                - int(right["response_critical_total_cycles"])
            )
            non_response_cycle_delta = (
                int(left["non_response_lower_bound_cycles"])
                - int(right["non_response_lower_bound_cycles"])
            )
            result.append(
                {
                    "coarse_variant": coarse,
                    "fine_variant": fine,
                    "coarse_q": left["interval_max_cycles"],
                    "fine_q": right["interval_max_cycles"],
                    "cores": cores,
                    "absolute_cpi_change": abs(
                        float(left["fastsim_uop_cpi"])
                        / float(right["fastsim_uop_cpi"])
                        - 1.0
                    ),
                    "sum_core_cycle_delta": sum_core_cycle_delta,
                    "response_critical_cycle_delta": (
                        response_critical_cycle_delta
                    ),
                    "non_response_lower_bound_cycle_delta": (
                        non_response_cycle_delta
                    ),
                    "cycle_delta_explained_by_response_critical": (
                        sum_core_cycle_delta == response_critical_cycle_delta
                        and non_response_cycle_delta == 0
                    ),
                }
            )
    return result


FRONTIER_ABSOLUTE_CYCLE_FIELDS = {
    "base_fetch_cycle",
    "actual_fetch_cycle",
    "actual_rename_cycle",
    "actual_dispatch_cycle",
    "base_issue_cycle",
    "actual_issue_cycle",
    "base_completion_cycle",
    "actual_completion_cycle",
    "memory_response_cycle",
    "base_retire_cycle",
    "actual_retire_cycle",
    "commit_cycle",
    "store_drain_ready_cycle",
    "sequencer_min_release_cycle",
    "iq_min_release_cycle",
    "rob_head_retire_cycle",
    "lq_head_release_cycle",
    "sq_head_release_cycle",
    "rob_capacity_predecessor_retire_cycle",
    "prior_commit_cycle",
    "incoming_sq_release_cycle",
    "selected_memory_corrected_issue_cycle",
    "selected_memory_response_cycle",
}

FRONTIER_PARTITION_METADATA_FIELDS = {
    "checkpoint_begin_sequence",
    "checkpoint_end_sequence",
    "checkpoint_uop_offset",
    "checkpoint_uops",
    "memory_events",
}

FRONTIER_CURRENT_TIMING_FIELDS = {
    "actual_fetch_cycle",
    "actual_rename_cycle",
    "actual_dispatch_cycle",
    "actual_issue_cycle",
    "actual_completion_cycle",
    "memory_response_cycle",
    "actual_retire_cycle",
    "commit_cycle",
}

FRONTIER_CAUSE_NAMES = {
    0: "unattributed",
    1: "rename_free_list",
    2: "dispatch_bandwidth",
    3: "rob_capacity",
    4: "iq_capacity",
    5: "lq_capacity",
    6: "sq_capacity",
    7: "dependency",
    8: "sequencer",
    9: "l1_mshr",
    10: "l2_mshr",
    11: "instruction_fetch",
    12: "memory_response",
    13: "commit_bandwidth",
    14: "tso_store",
}


def translation_normalized_frontier_sample(
    sample: dict[str, Any],
) -> dict[str, Any]:
    """Remove the checkpoint's accumulated scalar time translation."""
    origin = int(sample["interval_gap_cycles"])
    normalized = {
        key: value
        for key, value in sample.items()
        if key != "interval_gap_cycles"
        and key not in FRONTIER_PARTITION_METADATA_FIELDS
    }
    for field in FRONTIER_ABSOLUTE_CYCLE_FIELDS:
        if field not in normalized:
            continue
        cycle = int(normalized[field])
        # Zero is the no-response/empty-calendar sentinel, not an absolute
        # timestamp that should become negative after translation.
        normalized[field] = 0 if cycle == 0 else cycle - origin
    return normalized


def compare_frontier_samples(
    samples_by_variant: dict[str, list[dict[str, Any]]],
    sequence: tuple[str, ...] = BASELINE_Q_SEQUENCE,
) -> dict[str, Any]:
    """Compare semantic frontier snapshots only at shared UOP milestones."""
    variants = [name for name in sequence if name in samples_by_variant]
    indexed: dict[str, dict[int, dict[str, Any]]] = {}
    for variant in variants:
        samples = samples_by_variant[variant]
        by_sequence = {int(sample["sequence"]): sample for sample in samples}
        if len(by_sequence) != len(samples):
            raise ValueError(f"{variant}: duplicate response-frontier sequence")
        indexed[variant] = by_sequence

    common_sequences: list[int] = []
    if variants:
        common = set(indexed[variants[0]])
        for variant in variants[1:]:
            common.intersection_update(indexed[variant])
        common_sequences = sorted(common)

    earliest_mismatch: dict[str, Any] | None = None
    for milestone in common_sequences:
        samples = {
            variant: indexed[variant][milestone] for variant in variants
        }
        fields = sorted(
            set().union(*(sample.keys() for sample in samples.values()))
            - {"sequence"}
        )
        mismatched_fields = [
            field
            for field in fields
            if len(
                {
                    json.dumps(sample.get(field), sort_keys=True)
                    for sample in samples.values()
                }
            )
            != 1
        ]
        if mismatched_fields:
            earliest_mismatch = {
                "sequence": milestone,
                "mismatched_fields": mismatched_fields,
                "values": {
                    variant: {
                        field: samples[variant].get(field)
                        for field in mismatched_fields
                    }
                    for variant in variants
                },
            }
            break

    complete = len(variants) == len(sequence)
    return {
        "variants": variants,
        "complete_variant_set": complete,
        "sample_counts": {
            variant: len(samples_by_variant[variant])
            for variant in variants
        },
        "common_sample_count": len(common_sequences),
        "first_common_sequence": (
            common_sequences[0] if common_sequences else None
        ),
        "last_common_sequence": (
            common_sequences[-1] if common_sequences else None
        ),
        "all_common_samples_equal": (
            complete and bool(common_sequences) and earliest_mismatch is None
        ),
        "earliest_mismatch": earliest_mismatch,
    }


def frontier_audit_findings(
    rows: list[dict[str, Any]],
    sequence: tuple[str, ...] = BASELINE_Q_SEQUENCE,
) -> dict[str, Any]:
    """Load each Q run and identify the earliest shared frontier mismatch."""
    by_key = {(row["variant"], row["cores"]): row for row in rows}
    topologies: dict[str, Any] = {}
    for cores in (4, 8):
        if any((variant, cores) not in by_key for variant in sequence):
            continue
        stats_by_variant = {
            variant: load(Path(by_key[(variant, cores)]["stats"]))
            for variant in sequence
        }
        per_core: dict[str, Any] = {}
        earliest: dict[str, Any] | None = None
        earliest_timing: dict[str, Any] | None = None
        for core_index in range(cores):
            samples_by_variant = {
                variant: stats_by_variant[variant]["cores"][core_index].get(
                    "response_frontier_audit", []
                )
                for variant in sequence
            }
            comparison = compare_frontier_samples(
                samples_by_variant,
                sequence,
            )
            normalized_samples = {
                variant: [
                    translation_normalized_frontier_sample(sample)
                    for sample in samples
                ]
                for variant, samples in samples_by_variant.items()
            }
            normalized = compare_frontier_samples(
                normalized_samples,
                sequence,
            )
            current_timing = compare_frontier_samples(
                {
                    variant: [
                        {
                            "sequence": sample["sequence"],
                            **{
                                field: sample[field]
                                for field in FRONTIER_CURRENT_TIMING_FIELDS
                                if field in sample
                            },
                        }
                        for sample in samples
                    ]
                    for variant, samples in normalized_samples.items()
                },
                sequence,
            )
            timing_mismatch = current_timing["earliest_mismatch"]
            if timing_mismatch is not None:
                milestone = int(timing_mismatch["sequence"])
                context_fields = (
                    "sequencer_min_release_cycle",
                    "iq_min_release_cycle",
                    "rob_head_retire_cycle",
                    "lq_head_release_cycle",
                    "sq_head_release_cycle",
                    "store_drain_ready_cycle",
                    "dispatch_used",
                    "commit_used",
                    "dispatch_cause",
                    "completion_cause",
                    "retire_cause",
                    "response_seed",
                    "has_load",
                    "has_store",
                )
                normalized_index = {
                    variant: {
                        int(sample["sequence"]): sample
                        for sample in samples
                    }
                    for variant, samples in normalized_samples.items()
                }
                timing_context: dict[str, Any] = {}
                for variant in sequence:
                    sample = normalized_index[variant][milestone]
                    timing_context[variant] = {
                        field: (
                            FRONTIER_CAUSE_NAMES.get(
                                int(sample[field]), "unknown"
                            )
                            if field.endswith("_cause")
                            else sample[field]
                        )
                        for field in context_fields
                        if field in sample
                    }
                timing_mismatch["context"] = timing_context
            comparison["translation_normalized"] = normalized
            comparison["current_uop_timing"] = current_timing
            per_core[str(core_index)] = comparison
            mismatch = normalized["earliest_mismatch"]
            if mismatch is not None and (
                earliest is None
                or (int(mismatch["sequence"]), core_index)
                < (int(earliest["sequence"]), int(earliest["core"]))
            ):
                earliest = {"core": core_index, **mismatch}
            if timing_mismatch is not None and (
                earliest_timing is None
                or (int(timing_mismatch["sequence"]), core_index)
                < (int(earliest_timing["sequence"]),
                   int(earliest_timing["core"]))
            ):
                earliest_timing = {
                    "core": core_index,
                    **timing_mismatch,
                }

        topologies[f"c{cores}"] = {
            "per_core": per_core,
            "common_sample_count": sum(
                int(item["common_sample_count"])
                for item in per_core.values()
            ),
            "all_common_samples_equal": all(
                bool(item["all_common_samples_equal"])
                for item in per_core.values()
            ),
            "all_translation_normalized_samples_equal": all(
                bool(
                    item["translation_normalized"][
                        "all_common_samples_equal"
                    ]
                )
                for item in per_core.values()
            ),
            "all_current_uop_timing_samples_equal": all(
                bool(
                    item["current_uop_timing"][
                        "all_common_samples_equal"
                    ]
                )
                for item in per_core.values()
            ),
            "earliest_translation_normalized_mismatch": earliest,
            "earliest_current_uop_timing_mismatch": earliest_timing,
        }

    return {
        "stride_uops": int(
            COMMON_OVERRIDES["core.response_frontier_audit_stride_uops"]
        ),
        "topologies": topologies,
        "all_common_samples_equal": bool(topologies)
        and all(
            bool(item["all_common_samples_equal"])
            for item in topologies.values()
        ),
        "all_translation_normalized_samples_equal": bool(topologies)
        and all(
            bool(item["all_translation_normalized_samples_equal"])
            for item in topologies.values()
        ),
        "all_current_uop_timing_samples_equal": bool(topologies)
        and all(
            bool(item["all_current_uop_timing_samples_equal"])
            for item in topologies.values()
        ),
    }


def factor_findings(
    rows: list[dict[str, Any]], q_changes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Turn the directed measurements into explicit go/no-go evidence."""
    by_key = {(row["variant"], row["cores"]): row for row in rows}

    def effect(variant: str, cores: int) -> dict[str, Any] | None:
        row = by_key.get((variant, cores))
        if row is None:
            return None
        return {
            "cpi_delta_vs_q1024": row["cpi_delta_vs_q1024"],
            "gem5_cpi_signed_error": row["cpi_signed_error"],
            "effective_issue_after_controller_arrival_rate": row[
                "lifecycle_corrected_issue_after_controller_arrival_rate"
            ],
            "unprojected_issue_after_controller_arrival_rate": row[
                "lifecycle_unprojected_issue_after_controller_arrival_rate"
            ],
        }

    findings: dict[str, Any] = {}
    if q_changes:
        convergence_limit = 0.02
        findings["q_convergence"] = {
            "limit": convergence_limit,
            "passed": all(
                float(item["absolute_cpi_change"]) <= convergence_limit
                for item in q_changes
            ),
            "max_adjacent_cpi_change": max(
                float(item["absolute_cpi_change"]) for item in q_changes
            ),
            "all_cycle_deltas_explained_by_response_critical": all(
                bool(item["cycle_delta_explained_by_response_critical"])
                for item in q_changes
            ),
            "max_absolute_non_response_lower_bound_cycle_delta": max(
                abs(int(item["non_response_lower_bound_cycle_delta"]))
                for item in q_changes
            ),
            "interpretation": (
                "Q is a numerical partition and must not be selected by "
                "minimizing gem5 error. The adjacent cycle ledger separates "
                "checkpoint-tail response attribution from the fixed lower "
                "bound."
            ),
        }

    if all(("legacy_causal_timing_on", cores) in by_key for cores in (4, 8)):
        causal_rows = [
            by_key[("legacy_causal_timing_on", cores)] for cores in (4, 8)
        ]
        findings["legacy_causal_timing"] = {
            "effects": {str(cores): effect("legacy_causal_timing_on", cores)
                        for cores in (4, 8)},
            "candidate_epochs": sum(
                int(row["causal_timing_candidate_epochs"])
                for row in causal_rows
            ),
            "stable_epochs": sum(
                int(row["causal_timing_stable_epochs"])
                for row in causal_rows
            ),
            "fallback_epochs": sum(
                int(row["causal_timing_fallback_epochs"])
                for row in causal_rows
            ),
            "closure_events": sum(
                int(row["causal_closure_events"]) for row in causal_rows
            ),
            "replayed_events": sum(
                int(row["causal_timing_replayed_events"])
                for row in causal_rows
            ),
            "meets_baseline_cpi_gate": all(
                abs(float(row["cpi_signed_error"])) < 0.06
                for row in causal_rows
            ),
            "interpretation": (
                "The existing timing-only fixed point is active but is not "
                "the proposed incremental request-order closure."
            ),
        }

    if all(("store_post_commit_on", cores) in by_key for cores in (4, 8)):
        store_rows = [
            by_key[("store_post_commit_on", cores)] for cores in (4, 8)
        ]
        findings["store_post_commit"] = {
            "effects": {str(cores): effect("store_post_commit_on", cores)
                        for cores in (4, 8)},
            "request_events": sum(
                int(row["store_post_commit_request_events"])
                for row in store_rows
            ),
            "request_delay_cycles": sum(
                int(row["store_post_commit_request_delay_cycles"])
                for row in store_rows
            ),
            "horizon_fallback_epochs": sum(
                int(row["store_post_commit_request_horizon_fallback_epochs"])
                for row in store_rows
            ),
            "cpi_neutral_within_0_5_percent": all(
                abs(float(row["cpi_delta_vs_q1024"])) <= 0.005
                for row in store_rows
            ),
        }

    if all(("sparse_resource_repair_on", cores) in by_key for cores in (4, 8)):
        resource_rows = [
            by_key[("sparse_resource_repair_on", cores)]
            for cores in (4, 8)
        ]
        findings["sparse_resource_repair"] = {
            "effects": {
                str(cores): effect("sparse_resource_repair_on", cores)
                for cores in (4, 8)
            },
            "candidates": sum(
                int(row["sparse_resource_candidates"])
                for row in resource_rows
            ),
            "issue_moves": sum(
                int(row["sparse_resource_issue_moves"])
                for row in resource_rows
            ),
            "writeback_moves": sum(
                int(row["sparse_resource_writeback_moves"])
                for row in resource_rows
            ),
            "meets_baseline_cpi_gate": all(
                abs(float(row["cpi_signed_error"])) < 0.06
                for row in resource_rows
            ),
        }

    if all(("dtlb_speculative_path_on", cores) in by_key
           for cores in (4, 8)):
        speculative_effects: dict[str, Any] = {}
        for cores in (4, 8):
            dtlb = by_key[("dtlb_speculative_path_on", cores)]
            speculative_effects[str(cores)] = {
                "dtlb_cpi_delta_vs_baseline": dtlb["cpi_delta_vs_q1024"],
                "committed_dtlb_misses": dtlb["fastsim_dtlb_misses"],
                "gem5_dtlb_misses": dtlb["gem5_dtlb_misses"],
                "committed_dtlb_miss_signed_error": (
                    dtlb["dtlb_miss_signed_error"]
                ),
                "speculative_dtlb_accesses": dtlb[
                    "speculative_dtlb_accesses"
                ],
                "speculative_dtlb_misses": dtlb[
                    "speculative_dtlb_misses"
                ],
                "speculative_dtlb_untracked": dtlb[
                    "speculative_dtlb_untracked"
                ],
            }
        findings["speculative_dtlb_state"] = {
            "effects": speculative_effects,
            "interpretation": (
                "The path reconstruction mutates timing DTLB state only; "
                "committed L1I state and architectural PMU counts are fixed."
            ),
        }

    ddr_variants = ("ddr_act_on", "ddr_column_rank_on", "ddr_combined")
    if all((variant, cores) in by_key
           for variant in ddr_variants for cores in (4, 8)):
        ddr_effects: dict[str, Any] = {}
        for cores in (4, 8):
            act = float(by_key[("ddr_act_on", cores)]["cpi_delta_vs_q1024"])
            column = float(
                by_key[("ddr_column_rank_on", cores)]["cpi_delta_vs_q1024"]
            )
            combined = float(
                by_key[("ddr_combined", cores)]["cpi_delta_vs_q1024"]
            )
            ddr_effects[str(cores)] = {
                "act_cpi_delta": act,
                "column_rank_cpi_delta": column,
                "combined_cpi_delta": combined,
                "combined_interaction": combined - act - column,
                "combined_gem5_cpi_signed_error": by_key[
                    ("ddr_combined", cores)
                ]["cpi_signed_error"],
            }
        findings["ddr_calendar"] = {
            "effects": ddr_effects,
            "meets_baseline_cpi_gate": all(
                abs(float(by_key[("ddr_combined", cores)]["cpi_signed_error"]))
                < 0.06
                for cores in (4, 8)
            ),
            "interpretation": (
                "Both timing groups contribute, but their combination is a "
                "partial correction on the current request stream."
            ),
        }
    return findings


def write_outputs(
    args: argparse.Namespace, variants: list[str], rows: list[dict[str, Any]]
) -> None:
    args.out.mkdir(parents=True, exist_ok=True)
    baseline_rows = [row for row in rows if row["variant"] == "baseline_q1024"]
    baseline_equivalence_checked = len(baseline_rows) == 2
    baseline_equivalent = (
        all(
            row["production_uop_cpi"] is not None
            and row["generic_vs_production_cpi_delta"] is not None
            and abs(float(row["generic_vs_production_cpi_delta"])) <= 1e-12
            for row in baseline_rows
        )
        if baseline_equivalence_checked
        else None
    )
    ledgers_conserved = all(
        bool(row["lifecycle_population_conserved"])
        and bool(row["lifecycle_timing_conserved"])
        and bool(row["response_stage_conserved"])
        and bool(row["epoch_memory_events_conserved"])
        and bool(row["response_frontier_conserved"])
        for row in rows
    )
    q_changes = (
        adjacent_q_changes(rows)
        if set(BASELINE_Q_SEQUENCE).issubset(variants)
        else []
    )
    frontier_audit = (
        frontier_audit_findings(rows)
        if set(BASELINE_Q_SEQUENCE).issubset(variants)
        else {}
    )
    findings = factor_findings(rows, q_changes)
    manifest = {
        "schema": "fastsim-tealeaf-cpi-directed-v1",
        "workload": WORKLOAD,
        "cores": [4, 8],
        "production_alias_unchanged": str(args.config),
        "common_overrides": COMMON_OVERRIDES,
        "variants": {
            name: {
                **VARIANTS[name],
                "effective_overrides": variant_overrides(name),
            }
            for name in variants
        },
        "hashes": {
            "fastsim_sha256": sha256(args.fastsim),
            "base_config_sha256": sha256(args.config),
            "matrix_sha256": sha256(args.matrix),
        },
        "checks": {
            "generic_attribution_matches_production_cpi": baseline_equivalent,
            "all_population_and_timing_ledgers_conserved": ledgers_conserved,
            "q_frontier_common_milestones_equal": (
                frontier_audit.get("all_common_samples_equal")
                if frontier_audit
                else None
            ),
            "q_frontier_translation_normalized_milestones_equal": (
                frontier_audit.get(
                    "all_translation_normalized_samples_equal"
                )
                if frontier_audit
                else None
            ),
            "q_frontier_current_uop_timing_milestones_equal": (
                frontier_audit.get(
                    "all_current_uop_timing_samples_equal"
                )
                if frontier_audit
                else None
            ),
        },
        "adjacent_q_changes": q_changes,
        "frontier_audit": frontier_audit,
        "factor_findings": findings,
        "rows": rows,
    }
    (args.out / "report.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if rows:
        with (args.out / "cases.csv").open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    lines = [
        "# TeaLeaf C4/C8 directed CPI diagnostics",
        "",
        "This is an attribution/ablation matrix; it does not change "
        "`configs/gem5-fs-native-kernel.cfg` or promote any candidate.",
        "",
        "| Variant | Cores | FastSim CPI | gem5 error | Δ vs Q1024 | "
        "UOP/s | unprojected issue/arrival mismatch | "
        "effective issue after projected arrival | Ledgers |",
        "|---|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in rows:
        ledgers = (
            bool(row["lifecycle_population_conserved"])
            and bool(row["lifecycle_timing_conserved"])
            and bool(row["response_stage_conserved"])
            and bool(row["epoch_memory_events_conserved"])
        )
        lines.append(
            f"| `{row['variant']}` | {row['cores']} | "
            f"{row['fastsim_uop_cpi']:.6f} | {pct(row['cpi_signed_error'])} | "
            f"{pct(row['cpi_delta_vs_q1024'])} | "
            f"{row['user_uops_per_second'] / 1e6:.3f}M | "
            f"{pct(row['lifecycle_unprojected_issue_after_controller_arrival_rate'])} | "
            f"{pct(row['lifecycle_corrected_issue_after_controller_arrival_rate'])} | "
            f"{'PASS' if ledgers else 'FAIL'} |"
        )
    lines.extend(
        [
            "",
            "The lifecycle population is restricted to committed data events "
            "that create a unique DRAM read. `candidate -> corrected issue -> "
            "controller arrival -> controller service -> response -> retire` "
            "is counted once per request. The hierarchy's absolute stages are "
            "projected from the issue time at which they were generated to "
            "the corrected issue time, matching the relative latency consumed "
            "by the core. The unprojected mismatch is reported separately. "
            "Effective backward edges are not clamped, and the signed "
            "adjacent-stage identity must still conserve.",
            "",
            "`legacy_causal_timing_on` exercises the existing timing-only "
            "fixed-point path. It is not an implementation of the proposed "
            "incremental checkpoint/undo causal closure.",
            "",
            "## Checks",
            "",
            f"- Generic attribution baseline equals materialized production "
            "CPI: "
            f"{('PASS' if baseline_equivalent else 'FAIL') if baseline_equivalence_checked else 'NOT RUN'}.",
            f"- All request/response/epoch ledgers conserve: "
            f"{'PASS' if ledgers_conserved else 'FAIL'}.",
        ]
    )
    if findings:
        lines.extend(["", "## Interpretation", ""])
        q_finding = findings.get("q_convergence")
        if q_finding:
            lines.append(
                "- Q convergence: "
                f"{'PASS' if q_finding['passed'] else 'FAIL'} at the "
                f"{pct(q_finding['limit'])} limit; maximum adjacent-Q CPI "
                f"change is {pct(q_finding['max_adjacent_cpi_change'])}. "
                "All adjacent total-cycle deltas are "
                f"{'exactly' if q_finding['all_cycle_deltas_explained_by_response_critical'] else 'not fully'} "
                "explained by response-critical cycles; maximum lower-bound "
                "delta is "
                f"{q_finding['max_absolute_non_response_lower_bound_cycle_delta']:,} cycles. "
                "Q=512 must not be selected merely because its CPI is closer "
                "to gem5."
            )
        causal = findings.get("legacy_causal_timing")
        if causal:
            effects = causal["effects"]
            lines.append(
                "- Existing causal timing: C4/C8 CPI moves by "
                f"{pct(effects['4']['cpi_delta_vs_q1024'])}/"
                f"{pct(effects['8']['cpi_delta_vs_q1024'])}, but residual "
                "gem5 error remains "
                f"{pct(effects['4']['gem5_cpi_signed_error'])}/"
                f"{pct(effects['8']['gem5_cpi_signed_error'])}; "
                f"{causal['stable_epochs']} of "
                f"{causal['candidate_epochs']} candidate epochs stabilize "
                f"and {causal['fallback_epochs']} fall back. This does not "
                "meet the 6% baseline gate."
            )
        store = findings.get("store_post_commit")
        if store:
            effects = store["effects"]
            lines.append(
                "- Store post-commit edge: it handles "
                f"{store['request_events']:,} request events, but C4/C8 CPI "
                f"moves by only {pct(effects['4']['cpi_delta_vs_q1024'])}/"
                f"{pct(effects['8']['cpi_delta_vs_q1024'])}. It is not the "
                "primary TeaLeaf CPI repair."
            )
        resource = findings.get("sparse_resource_repair")
        if resource:
            effects = resource["effects"]
            lines.append(
                "- Response resource calendar: it identifies "
                f"{resource['candidates']:,} causal-cone candidates and "
                f"moves {resource['issue_moves']:,}/"
                f"{resource['writeback_moves']:,} issue/writeback slots; "
                "C4/C8 CPI moves by "
                f"{pct(effects['4']['cpi_delta_vs_q1024'])}/"
                f"{pct(effects['8']['cpi_delta_vs_q1024'])}."
            )
        speculative = findings.get("speculative_dtlb_state")
        if speculative:
            effects = speculative["effects"]
            lines.append(
                "- Predicted-path DTLB timing state: C4/C8 CPI moves by "
                f"{pct(effects['4']['dtlb_cpi_delta_vs_baseline'])}/"
                f"{pct(effects['8']['dtlb_cpi_delta_vs_baseline'])}; "
                "committed DTLB miss error becomes "
                f"{pct(effects['4']['committed_dtlb_miss_signed_error'])}/"
                f"{pct(effects['8']['committed_dtlb_miss_signed_error'])}. "
                "Speculative requests remain diagnostic and are not added "
                "to architectural PMU counts."
            )
        ddr = findings.get("ddr_calendar")
        if ddr:
            effects = ddr["effects"]
            lines.append(
                "- DDR calendar: ACT, column/rank, and combined CPI effects "
                "are respectively "
                f"{pct(effects['4']['act_cpi_delta'])}, "
                f"{pct(effects['4']['column_rank_cpi_delta'])}, and "
                f"{pct(effects['4']['combined_cpi_delta'])} on C4; "
                f"{pct(effects['8']['act_cpi_delta'])}, "
                f"{pct(effects['8']['column_rank_cpi_delta'])}, and "
                f"{pct(effects['8']['combined_cpi_delta'])} on C8. The "
                "combined residual errors still exceed 6%, so this is a "
                "real but partial contribution."
            )

    if baseline_rows:
        lines.extend(
            [
                "",
                "## Baseline request lifecycle ledger",
                "",
                "| Cores | Requests | Candidate | Corrected issue | "
                "Controller arrival | Service | Response | Retire | "
                "Response after retire |",
                "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in baseline_rows:
            lines.append(
                f"| {row['cores']} | {row['lifecycle_requests']:,} | "
                f"{row['lifecycle_candidate_creates']:,} | "
                f"{row['lifecycle_corrected_issues']:,} | "
                f"{row['lifecycle_controller_arrivals']:,} | "
                f"{row['lifecycle_controller_services']:,} | "
                f"{row['lifecycle_responses']:,} | "
                f"{row['lifecycle_retires']:,} | "
                f"{pct(row['lifecycle_response_after_retire_rate'])} |"
            )
    if q_changes:
        lines.extend(
            [
                "",
                "## Adjacent-Q sensitivity",
                "",
                "| Coarse Q | Fine Q | Cores | |Δ CPI| | Δ total cycles | "
                "Δ response-critical | Δ lower bound | Identity |",
                "|---:|---:|---:|---:|---:|---:|---:|:---:|",
            ]
        )
        for item in q_changes:
            lines.append(
                f"| {item['coarse_q']} | {item['fine_q']} | "
                f"{item['cores']} | {pct(item['absolute_cpi_change'])} | "
                f"{item['sum_core_cycle_delta']:,} | "
                f"{item['response_critical_cycle_delta']:,} | "
                f"{item['non_response_lower_bound_cycle_delta']:,} | "
                f"{'PASS' if item['cycle_delta_explained_by_response_critical'] else 'FAIL'} |"
            )
    if frontier_audit:
        lines.extend(
            [
                "",
                "## Fixed-sequence response frontier audit",
                "",
                "Samples are taken at each core's measurement entry and "
                f"every {frontier_audit['stride_uops']:,} committed UOP "
                "sequence positions. A comparison uses only "
                "milestones present in every Q run for the same core. Stage "
                "cycles and frontier digests are translated by the current "
                "interval gap so a harmless global time-origin shift does "
                "not count as a structural mismatch.",
                "",
                "| Topology | Common samples | First normalized frontier "
                "mismatch | First current-UOP timing mismatch |",
                "|:---:|---:|---|---|",
            ]
        )
        for topology, audit in frontier_audit["topologies"].items():
            mismatch = audit[
                "earliest_translation_normalized_mismatch"
            ]
            timing_mismatch = audit[
                "earliest_current_uop_timing_mismatch"
            ]
            if mismatch is None:
                frontier_summary = "none"
            else:
                fields = ", ".join(mismatch["mismatched_fields"])
                frontier_summary = (
                    f"core {mismatch['core']} @ "
                    f"{int(mismatch['sequence']):,}: {fields}"
                )
            if timing_mismatch is None:
                timing_summary = "none"
            else:
                timing_fields = ", ".join(
                    timing_mismatch["mismatched_fields"]
                )
                timing_summary = (
                    f"core {timing_mismatch['core']} @ "
                    f"{int(timing_mismatch['sequence']):,}: "
                    f"{timing_fields}"
                )
            lines.append(
                f"| {topology.upper()} | "
                f"{audit['common_sample_count']:,} | {frontier_summary} | "
                f"{timing_summary} |"
            )
        lines.extend(
            [
                "",
                "The complete per-Q values for the first mismatch on every "
                "core are retained in `report.json` under `frontier_audit`.",
            ]
        )
    lines.append("")
    (args.out / "report.md").write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=project / "tmp/spec2026-uarch-exploration-v1-native-v28_2",
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--production-root",
        type=Path,
        help="existing maintained v28.6 replay root used for equivalence checks",
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=project / "configs/spec2026-uarch-exploration-v1.json",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=project / "configs/gem5-fs-native-kernel.cfg",
    )
    parser.add_argument(
        "--fastsim", type=Path, default=project / "build/fastsim"
    )
    parser.add_argument(
        "--runner",
        type=Path,
        default=project / "tools/run_uarch_fastsim.py",
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--jobs", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--variant", action="append", default=[])
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    args.project = project
    args.root = args.root.resolve()
    args.out = (args.out or args.root / "diagnostics/tealeaf-cpi-directed-v1").resolve()
    args.production_root = (
        args.production_root.resolve()
        if args.production_root
        else args.root / "diagnostics/fastsim-v28_6-maintained"
    )
    args.matrix = args.matrix.resolve()
    args.config = args.config.resolve()
    args.fastsim = args.fastsim.resolve()
    args.runner = args.runner.resolve()
    args.python = args.python.resolve()
    if args.jobs < 0 or args.timeout <= 0:
        parser.error("--jobs must be nonnegative and --timeout must be positive")
    for path in (args.root, args.matrix, args.config, args.fastsim, args.runner,
                 args.python):
        if not path.exists():
            parser.error(f"missing required path: {path}")
    return args


def main() -> int:
    args = parse_args()
    try:
        variants = selected_variants(args.variant)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if args.dry_run:
        for name in variants:
            print(" ".join(runner_command(args, name)))
        return 0
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.summarize_only:
        cpu_count = os.cpu_count() or 1
        jobs = args.jobs or max(1, min(len(variants), cpu_count // 8))
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            futures = {
                executor.submit(run_variant, args, name): name
                for name in variants
            }
            for future in as_completed(futures):
                name, error = future.result()
                if error:
                    failures.append(f"{name}: {error}")
        if failures:
            for failure in failures:
                print(f"[error] {failure}")
            return 1
    rows = collect_rows(args, variants)
    write_outputs(args, variants, rows)
    print(f"report={args.out / 'report.json'} cases={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
