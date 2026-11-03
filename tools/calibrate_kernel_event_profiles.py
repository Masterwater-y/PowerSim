#!/usr/bin/env python3
"""Fit deployable kernel-event profiles from paired gem5/FastSim data.

The formal oracle supplies mutually exclusive syscall, page-fault and IRQ
counts, exact class PMU, and exact per-sysnum PMU. The FastSim model-off report
supplies first-touch candidates and foreground core cycles. No workload
identifier is used by the generated model: inference consumes only syscall
numbers, virtual-page first touches and foreground cycle progress. A legacy
:uk-:u common-rate fallback exists only behind an explicit diagnostic flag.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np

from validate_kernel_events_oracle import validate_document


ORACLE_SCHEMA = "tcsim-gem5-fs-kernel-events-v3"
LEGACY_ORACLE_SCHEMA = "tcsim-gem5-fs-kernel-events-v2"
FASTSIM_SCHEMA = "fastsim-stats-v5"
EVENT_CLASSES = ("syscall", "page_fault", "irq")
PAGE_FAULT_ALLOCATION_SYSCALLS = (9, 12, 25, 28)
PAGE_FAULT_ALLOCATION_RECENCY_UPPER_BOUNDS = (
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
    18446744073709551615,
)
# Frozen before looking at workload-held-out scores. These act as ridge
# pseudo-candidate counts on syscall and allocation-write deviations; they
# prevent aggregate page-fault labels from assigning a free probability to
# every workload-shaped channel.
PAGE_FAULT_SYSCALL_SHRINKAGE_PSEUDO_CANDIDATES = 65536
PAGE_FAULT_WRITE_SHRINKAGE_PSEUDO_CANDIDATES = 65536
PAGE_FAULT_BACKGROUND_ZERO_PSEUDO_CANDIDATES = 65536
PROFILE_FIELDS = (
    "service_cycles",
    "retired_instructions",
    "retired_uops",
    "memory_uops",
    "line_requests",
    "branches",
    "branch_misses",
    "l1d_accesses",
    "l1d_misses",
    "l2_accesses",
    "l2_misses",
    "llc_accesses",
    "llc_misses",
    "permission_upgrades",
    "remote_supplies",
    "llc_merged_misses",
    "llc_unique_fills",
    "dram_reads",
    "dram_writes",
    "dtlb_accesses",
    "dtlb_misses",
    "blocked_wall_cycles",
)
FIT_FIELDS = (
    "retired_instructions",
    "retired_uops",
    "memory_uops",
    "line_requests",
    "branches",
    "branch_misses",
    "l1d_accesses",
    "l1d_misses",
    "l2_misses",
    "llc_misses",
    "permission_upgrades",
    "remote_supplies",
    "llc_merged_misses",
    "llc_unique_fills",
    "dram_reads",
    "dram_writes",
    "dtlb_accesses",
    "dtlb_misses",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        action="append",
        nargs=2,
        metavar=("ORACLE", "USER_REPORT"),
        required=True,
        help="Repeat once per calibration result.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config-output", type=Path, required=True)
    parser.add_argument(
        "--base-config",
        type=Path,
        help="Prepend a complete FastSim microarchitecture config.",
    )
    parser.add_argument(
        "--allow-legacy-pmu",
        action="store_true",
        help=(
            "Permit diagnostic calibration from a scope-only PMU oracle. "
            "Such output is not formal PMU accuracy evidence."
        ),
    )
    return parser.parse_args()


def load_case(oracle_path: Path, report_path: Path) -> dict:
    oracle = json.loads(oracle_path.read_text())
    report = json.loads(report_path.read_text())
    if oracle.get("schema") not in (ORACLE_SCHEMA, LEGACY_ORACLE_SCHEMA):
        raise ValueError(
            f"{oracle_path}: expected {ORACLE_SCHEMA} or {LEGACY_ORACLE_SCHEMA}"
        )
    try:
        validate_document(oracle, 0.0)
    except ValueError as exc:
        raise ValueError(f"{oracle_path}: invalid oracle: {exc}")
    aggregate = oracle["aggregate"]
    if oracle.get("schema") == LEGACY_ORACLE_SCHEMA:
        # Diagnostic compatibility only. v2 had no cross-line conservation,
        # so its L1D lookup count is the only available proxy for both fields.
        for row in [aggregate, *oracle["per_core"]]:
            scopes = [row.get("pmu_user"), row.get("pmu_user_plus_kernel")]
            scopes.extend(row.get("pmu_kernel_by_class", {}).values())
            scopes.extend(
                item.get("pmu") for item in row.get("syscall_profiles", [])
            )
            for pmu in scopes:
                if isinstance(pmu, dict):
                    pmu.setdefault("memory_uops", pmu.get("l1d_accesses", 0))
                    pmu.setdefault("line_requests", pmu.get("l1d_accesses", 0))
                    for field in (
                        "permission_upgrades",
                        "remote_supplies",
                        "llc_merged_misses",
                        "llc_unique_fills",
                        "dram_reads",
                        "dram_writes",
                    ):
                        pmu.setdefault(field, 0)
    if report.get("schema") != FASTSIM_SCHEMA:
        raise ValueError(f"{report_path}: expected schema {FASTSIM_SCHEMA}")
    if report.get("measurement_scope") != "user":
        raise ValueError(
            f"{report_path}: calibration requires measurement_scope=user"
        )
    metrics = report.get("scope_metrics", {})
    if not isinstance(metrics.get("pmu"), dict):
        raise ValueError(f"{report_path}: missing canonical scope_metrics")
    totals = report["totals"]
    if int(metrics["user_trace_uops"]) != int(aggregate["n_user"]):
        raise ValueError(
            f"{report_path}: functional trace uops do not match oracle"
        )
    if int(metrics["synthetic_kernel_active_cycles"]) != 0:
        raise ValueError(f"{report_path}: user report has kernel cycles")
    if len(report.get("cores", [])) != len(oracle["per_core"]):
        raise ValueError(f"{report_path}: core count does not match oracle")
    case_label = report_path.parent.name
    match = re.match(r"^\d+c-(.+)$", case_label)
    workload_label = match.group(1) if match else case_label
    return {
        "oracle_path": oracle_path,
        "report_path": report_path,
        # Labels are diagnostics and cross-validation partitions only. They
        # are never columns in the fitted or generated inference model.
        "case_label": case_label,
        "workload_label": workload_label,
        "oracle": oracle,
        "report": report,
    }


def nnls_three(matrix: np.ndarray, target: np.ndarray) -> tuple[np.ndarray, float]:
    """Solve a three-column non-negative least-squares problem exactly."""
    best = np.zeros(3, dtype=float)
    best_loss = float(np.dot(target, target))
    for mask in range(1, 1 << 3):
        columns = [index for index in range(3) if mask & (1 << index)]
        # rcond=-1 is supported by the repository's oldest NumPy baseline and
        # makes the rank cutoff explicit across host NumPy versions.
        solution, _, _, _ = np.linalg.lstsq(
            matrix[:, columns], target, rcond=-1
        )
        if np.any(solution < -1e-9):
            continue
        candidate = np.zeros(3, dtype=float)
        candidate[columns] = np.maximum(solution, 0.0)
        residual = matrix.dot(candidate) - target
        loss = float(np.dot(residual, residual))
        if loss < best_loss:
            best = candidate
            best_loss = loss
    return best, best_loss


def rounded(value: float) -> int:
    return max(0, int(math.floor(value + 0.5)))


def fit_semantic_page_fault_fallback(cases: list[dict]) -> dict:
    """Fit one workload-agnostic residual rate after exact mmap semantics.

    The exact selector owns every first touch in a trace-visible non-populated
    mmap.  Only first writes outside those ranges are candidates here; this
    covers startup-created anonymous/COW mappings whose creation predates the
    functional trace.  A single coefficient prevents workload identity from
    entering inference, and leave-one-workload-out diagnostics expose how
    much the residual varies.
    """

    enabled = [
        bool(
            case["report"].get("configuration", {}).get(
                "page_fault_syscall_semantic_model", False
            )
        )
        for case in cases
    ]
    if any(enabled) and not all(enabled):
        raise ValueError(
            "semantic page-fault calibration requires a consistent model "
            "setting across all cases"
        )
    if not any(enabled):
        return {
            "method": "disabled",
            "write_probability_ppm": 0,
            "reference_events": sum(
                int(
                    case["oracle"]["aggregate"]["event_counts"][
                        "page_fault"
                    ]
                )
                for case in cases
            ),
            "exact_semantic_candidates": 0,
            "fallback_write_candidates": 0,
            "residual_reference_events": 0,
            "training_error": None,
            "workload_heldout": {
                "selection_method": "disabled",
                "error": {"wape_percent": None},
                "folds": [],
            },
            "per_case": [],
            "workload_id_is_inference_input": False,
        }

    rows = []
    for case in cases:
        totals = case["report"]["totals"]
        exact = int(
            totals.get("page_fault_syscall_semantic_candidates", 0)
        )
        exact_writes = int(
            totals.get("page_fault_syscall_semantic_write_candidates", 0)
        )
        fallback = int(
            totals.get(
                "page_fault_syscall_semantic_fallback_write_candidates", 0
            )
        )
        first_writes = int(
            totals["page_fault_first_touch_write_candidates"]
        )
        if exact_writes > exact or exact_writes + fallback != first_writes:
            raise ValueError(
                "semantic page-fault write candidates do not conserve "
                "first-touch writes; run calibration with "
                "page_fault.syscall_semantic_model=true"
            )
        core_fallback = [
            int(
                core.get(
                    "page_fault_syscall_semantic_fallback_write_candidates",
                    0,
                )
            )
            for core in case["report"]["cores"]
        ]
        if sum(core_fallback) != fallback:
            raise ValueError(
                "per-core semantic fallback candidates do not conserve total"
            )
        reference = int(
            case["oracle"]["aggregate"]["event_counts"]["page_fault"]
        )
        rows.append(
            {
                "case": case,
                "reference": reference,
                "exact": exact,
                "fallback_candidates": fallback,
                "core_fallback_candidates": core_fallback,
                "residual_reference": max(0, reference - exact),
            }
        )

    def probability(training: list[dict]) -> int:
        candidates = sum(row["fallback_candidates"] for row in training)
        residual = sum(row["residual_reference"] for row in training)
        if candidates == 0:
            return 0
        return min(1_000_000, rounded(residual / candidates * 1_000_000))

    def predict(row: dict, probability_ppm: int) -> int:
        selected = sum(
            candidates * probability_ppm // 1_000_000
            for candidates in row["core_fallback_candidates"]
        )
        return row["exact"] + selected

    def error_summary(predicted: list[int]) -> dict:
        reference = [row["reference"] for row in rows]
        absolute = [
            abs(prediction - truth)
            for prediction, truth in zip(predicted, reference)
        ]
        nonzero_ape = [
            error / truth * 100.0
            for error, truth in zip(absolute, reference)
            if truth
        ]
        return {
            "absolute_error_events": sum(absolute),
            "wape_percent": (
                sum(absolute) / sum(reference) * 100.0
                if sum(reference)
                else None
            ),
            "mape_percent_nonzero_reference": (
                float(np.mean(nonzero_ape)) if nonzero_ape else None
            ),
            "zero_reference_cases": sum(value == 0 for value in reference),
        }

    frozen_probability = probability(rows)
    predicted = [predict(row, frozen_probability) for row in rows]
    groups: dict[str, list[int]] = {}
    for index, row in enumerate(rows):
        groups.setdefault(row["case"]["workload_label"], []).append(index)
    heldout = [0] * len(rows)
    folds = []
    if len(groups) > 1:
        for workload, test_indices in sorted(groups.items()):
            training = [
                row for index, row in enumerate(rows)
                if index not in test_indices
            ]
            fold_probability = probability(training)
            fold_predictions = []
            for index in test_indices:
                value = predict(rows[index], fold_probability)
                heldout[index] = value
                fold_predictions.append(value)
            folds.append(
                {
                    "held_out_workload": workload,
                    "write_probability_ppm": fold_probability,
                    "reference_events": sum(
                        rows[index]["reference"] for index in test_indices
                    ),
                    "predicted_events": sum(fold_predictions),
                }
            )
        selection_method = "leave-one-workload-out"
    else:
        heldout = predicted.copy()
        selection_method = "training-fallback-single-workload"

    return {
        "method": "exact-mmap-plus-shared-preexisting-first-write-v1",
        "write_probability_ppm": frozen_probability,
        "reference_events": sum(row["reference"] for row in rows),
        "exact_semantic_candidates": sum(row["exact"] for row in rows),
        "fallback_write_candidates": sum(
            row["fallback_candidates"] for row in rows
        ),
        "residual_reference_events": sum(
            row["residual_reference"] for row in rows
        ),
        "training_error": error_summary(predicted),
        "workload_heldout": {
            "selection_method": selection_method,
            "error": error_summary(heldout),
            "folds": folds,
        },
        "per_case": [
            {
                "case": row["case"]["case_label"],
                "workload": row["case"]["workload_label"],
                "reference_events": row["reference"],
                "exact_semantic_candidates": row["exact"],
                "fallback_write_candidates": row["fallback_candidates"],
                "predicted_events": predicted[index],
                "heldout_predicted_events": heldout[index],
            }
            for index, row in enumerate(rows)
        ],
        "workload_id_is_inference_input": False,
    }


def fit_pmu(cases: list[dict]) -> tuple[dict, dict]:
    matrix = np.asarray(
        [
            [
                int(case["oracle"]["aggregate"]["event_counts"][name])
                for name in EVENT_CLASSES
            ]
            for case in cases
        ],
        dtype=float,
    )
    per_cycle_rates = {}
    diagnostics = {}
    total_kernel_cycles = sum(
        int(case["oracle"]["aggregate"][field])
        for case in cases
        for field in (
            "syscall_kernel_cycles",
            "page_fault_kernel_cycles",
            "irq_kernel_cycles",
        )
    )
    for field in FIT_FIELDS:
        target = np.asarray(
            [
                int(case["oracle"]["aggregate"]["pmu_user_plus_kernel"][field])
                - int(case["oracle"]["aggregate"]["pmu_user"][field])
                for case in cases
            ],
            dtype=float,
        )
        coefficients, loss = nnls_three(matrix, target)
        fitted = matrix.dot(coefficients)
        per_cycle_rates[field] = (
            float(target.sum()) / total_kernel_cycles
            if total_kernel_cycles
            else 0.0
        )
        diagnostics[field] = {
            "coefficients": {
                name: float(coefficients[index])
                for index, name in enumerate(EVENT_CLASSES)
            },
            "rmse": math.sqrt(loss / len(cases)),
            "reference_total": int(target.sum()),
            "fitted_total": float(fitted.sum()),
        }

    return per_cycle_rates, diagnostics


def profile_for_service(service_cycles: int, rates: dict) -> dict:
    profile = {
        field: rounded(rates[field] * service_cycles)
        for field in FIT_FIELDS
    }
    profile["service_cycles"] = service_cycles
    profile["blocked_wall_cycles"] = 0
    return bound_profile(profile)


def profile_for_event_totals(
    service_cycles: int, pmu_totals: dict, event_count: int
) -> dict:
    denominator = max(1, event_count)
    profile = {
        field: rounded(int(pmu_totals[field]) / denominator)
        for field in FIT_FIELDS
    }
    profile["service_cycles"] = service_cycles
    profile["blocked_wall_cycles"] = 0
    return bound_profile(profile)


def bound_profile(profile: dict) -> dict:
    """Apply deployable architectural bounds and cache-path identities."""
    profile["retired_instructions"] = min(
        profile["retired_instructions"], profile["retired_uops"]
    )
    profile["memory_uops"] = min(
        profile["memory_uops"], profile["retired_uops"]
    )
    profile["line_requests"] = max(
        profile["line_requests"], profile["memory_uops"]
    )
    profile["branches"] = min(
        profile["branches"], profile["retired_instructions"]
    )
    profile["branch_misses"] = min(
        profile["branch_misses"], profile["branches"]
    )
    # Every line request performs one L1D lookup. Keep this identity exact;
    # cross-line UOPs may legitimately make it exceed retired UOP count.
    profile["l1d_accesses"] = profile["line_requests"]
    profile["l1d_misses"] = min(
        profile["l1d_misses"], profile["l1d_accesses"]
    )
    profile["l2_accesses"] = profile["l1d_misses"]
    profile["l2_misses"] = min(
        profile["l2_misses"], profile["l2_accesses"]
    )
    # Shared-cache hit, tag-miss, upgrade, remote-supply, and merge are
    # disjoint controller outcomes.  Preserve the directly fitted access
    # population and project each non-hit outcome into its remaining capacity
    # instead of aliasing LLC accesses to private-L2 tag misses.
    remaining_llc_outcomes = profile["llc_accesses"]
    for field in (
        "llc_misses",
        "permission_upgrades",
        "remote_supplies",
        "llc_merged_misses",
    ):
        profile[field] = min(profile[field], remaining_llc_outcomes)
        remaining_llc_outcomes -= profile[field]
    profile["llc_unique_fills"] = min(
        profile["llc_unique_fills"], profile["llc_misses"]
    )
    profile["dram_reads"] = min(
        profile["dram_reads"], profile["llc_unique_fills"]
    )
    profile["dtlb_accesses"] = min(
        profile["dtlb_accesses"], profile["retired_uops"]
    )
    profile["dtlb_misses"] = min(
        profile["dtlb_misses"], profile["dtlb_accesses"]
    )
    return profile


def has_exact_class_pmu(cases: list[dict]) -> bool:
    return all(
        case["oracle"]["aggregate"].get("pmu_source")
        == "taotrace-path-class-v3"
        and case["oracle"].get("schema") == ORACLE_SCHEMA
        and isinstance(
            case["oracle"]["aggregate"].get("pmu_kernel_by_class"), dict
        )
        and isinstance(
            case["oracle"]["aggregate"].get("syscall_profiles"), list
        )
        for case in cases
    )


def exact_class_pmu_totals(cases: list[dict], name: str) -> dict:
    return {
        field: sum(
            int(
                case["oracle"]["aggregate"]["pmu_kernel_by_class"][name][
                    field
                ]
            )
            for case in cases
        )
        for field in FIT_FIELDS
    }


def exact_syscall_stats(cases: list[dict]) -> dict[int, dict]:
    totals = {}
    for case in cases:
        for row in case["oracle"]["aggregate"]["syscall_profiles"]:
            sysnum = int(row["sysnum"])
            entry = totals.setdefault(
                sysnum,
                {
                    "count": 0,
                    "cycles": 0,
                    "pmu": {field: 0 for field in FIT_FIELDS},
                },
            )
            entry["count"] += int(row["count"])
            entry["cycles"] += int(row["kernel_cycles"])
            for field in FIT_FIELDS:
                entry["pmu"][field] += int(row["pmu"][field])
    return totals


def syscall_service_table(cases: list[dict]) -> dict[int, int]:
    totals: dict[int, dict[str, float]] = {}
    for case in cases:
        oracle_dir = case["oracle_path"].parent
        core_paths = sorted(oracle_dir.glob("cpi-core*.json"))
        if not core_paths:
            raise ValueError(f"{oracle_dir}: missing cpi-core*.json")
        for core_path in core_paths:
            core = json.loads(core_path.read_text())
            for row in core.get("syscalls", []):
                sysnum = int(row["sysnum"])
                entry = totals.setdefault(sysnum, {"count": 0, "cycles": 0.0})
                entry["count"] += int(row["count"])
                entry["cycles"] += float(row["kernel_cycles"])
    return {
        sysnum: rounded(entry["cycles"] / entry["count"])
        for sysnum, entry in sorted(totals.items())
        if entry["count"]
    }


def total_count(cases: list[dict], name: str) -> int:
    return sum(
        int(case["oracle"]["aggregate"]["event_counts"][name])
        for case in cases
    )


def total_cycles(cases: list[dict], field: str) -> int:
    return sum(int(case["oracle"]["aggregate"][field]) for case in cases)


def fit_irq_period(cases: list[dict]) -> tuple[int, int, int]:
    """Fit one deterministic foreground-cycle period to per-core IRQ counts."""
    samples = [
        (int(core["cycles"]), int(reference["event_counts"]["irq"]))
        for case in cases
        for core, reference in zip(
            case["report"]["cores"], case["oracle"]["per_core"]
        )
    ]
    foreground_cycles = sum(cycles for cycles, _ in samples)
    reference_events = sum(events for _, events in samples)
    if not samples:
        return 1, 0, 0
    if reference_events == 0:
        period = max(cycles for cycles, _ in samples) + 1
        return period, foreground_cycles, 0

    def predicted(period: int) -> list[int]:
        return [cycles // period for cycles, _ in samples]

    # Sum(floor(cycles / period)) is monotone. Find the first period whose
    # aggregate prediction does not exceed the reference, then compare the
    # two sides of that discontinuity. The per-core squared error breaks ties.
    low = 1
    high = max(cycles for cycles, _ in samples) + 1
    while low < high:
        middle = (low + high) // 2
        if sum(predicted(middle)) <= reference_events:
            high = middle
        else:
            low = middle + 1
    first_not_above = low
    if sum(predicted(first_not_above)) == reference_events:
        # Choose the center of the exact-count plateau instead of its first
        # cycle. Model-on foreground timing can differ from model-off timing
        # by a few cycles, and a boundary choice would amplify that harmless
        # perturbation into an extra or missing interrupt.
        low = first_not_above
        high = max(cycles for cycles, _ in samples) + 1
        while low < high:
            middle = (low + high) // 2
            if sum(predicted(middle)) < reference_events:
                high = middle
            else:
                low = middle + 1
        last_exact = low - 1
        candidates = {(first_not_above + last_exact) // 2}
    else:
        candidates = {first_not_above, max(1, first_not_above - 1)}

    def score(period: int) -> tuple[int, int, int]:
        values = predicted(period)
        return (
            abs(sum(values) - reference_events),
            sum(
                (value - reference) ** 2
                for value, (_, reference) in zip(values, samples)
            ),
            period,
        )

    period = min(candidates, key=score)
    return period, foreground_cycles, sum(predicted(period))


def fit_page_fault_probabilities(cases: list[dict]) -> dict:
    """Fit one shared, strongly-shrunk page-fault hierarchy.

    The eight coefficients are background read/write, one global allocation
    rate, four syscall deviations, and one shared allocation-write deviation.
    Workload labels are used only to construct held-out folds and never enter
    the feature matrix or generated model.
    """
    target = np.asarray(
        [
            int(case["oracle"]["aggregate"]["event_counts"]["page_fault"])
            for case in cases
        ],
        dtype=float,
    )
    target_cycles = np.asarray(
        [
            int(case["oracle"]["aggregate"]["page_fault_kernel_cycles"])
            for case in cases
        ],
        dtype=float,
    )
    final_service_cycles = rounded(
        float(target_cycles.sum()) / max(1.0, float(target.sum()))
    )

    for case in cases:
        report_bounds = tuple(
            int(value)
            for value in case["report"]["totals"].get(
                "page_fault_allocation_recency_upper_bounds", []
            )
        )
        if report_bounds != PAGE_FAULT_ALLOCATION_RECENCY_UPPER_BOUNDS:
            raise ValueError(
                "FastSim report lacks the expected page-fault allocation "
                "recency histogram"
            )

    def selected_count(values: list[int], window: int) -> int:
        if len(values) != len(PAGE_FAULT_ALLOCATION_RECENCY_UPPER_BOUNDS):
            raise ValueError("invalid page-fault allocation recency histogram")
        if window == 0:
            return sum(values)
        return sum(
            value
            for value, upper in zip(
                values, PAGE_FAULT_ALLOCATION_RECENCY_UPPER_BOUNDS
            )
            if upper <= window
        )

    def core_channels(core: dict, window: int) -> dict:
        entries = core.get("page_fault_allocation_by_syscall")
        if not isinstance(entries, list):
            raise ValueError(
                "FastSim report lacks page_fault_allocation_by_syscall; "
                "regenerate model-off reports with the hierarchical counter"
            )
        by_syscall: dict[int, tuple[int, int]] = {}
        aggregate_hist = [0] * len(
            PAGE_FAULT_ALLOCATION_RECENCY_UPPER_BOUNDS
        )
        aggregate_write_hist = [0] * len(aggregate_hist)
        for entry in entries:
            sysnum = int(entry["sysnum"])
            if sysnum in by_syscall:
                raise ValueError(f"duplicate page-fault syscall {sysnum}")
            recency = [int(value) for value in entry["recency_candidates"]]
            writes = [
                int(value)
                for value in entry["recency_write_candidates"]
            ]
            if len(recency) != len(aggregate_hist) or len(writes) != len(
                aggregate_hist
            ):
                raise ValueError(
                    "invalid per-syscall page-fault recency histogram"
                )
            if any(write > total for total, write in zip(recency, writes)):
                raise ValueError(
                    "page-fault per-syscall writes exceed candidates"
                )
            for index, value in enumerate(recency):
                aggregate_hist[index] += value
                aggregate_write_hist[index] += writes[index]
            total = selected_count(recency, window)
            write = selected_count(writes, window)
            by_syscall[sysnum] = (total - write, write)

        reported_hist = [
            int(value)
            for value in core["page_fault_allocation_recency_candidates"]
        ]
        reported_write_hist = [
            int(value)
            for value in core[
                "page_fault_allocation_recency_write_candidates"
            ]
        ]
        if aggregate_hist != reported_hist or (
            aggregate_write_hist != reported_write_hist
        ):
            raise ValueError(
                "per-syscall page-fault candidates do not conserve aggregate "
                "recency histograms"
            )

        allocation = sum(read + write for read, write in by_syscall.values())
        allocation_writes = sum(write for _, write in by_syscall.values())
        first_touch = int(core["page_fault_first_touch_candidates"])
        first_touch_writes = int(
            core["page_fault_first_touch_write_candidates"]
        )
        background = first_touch - allocation
        background_writes = first_touch_writes - allocation_writes
        background_reads = background - background_writes
        if min(background, background_reads, background_writes) < 0:
            raise ValueError("page-fault channel candidates underflow")
        return {
            "background_reads": background_reads,
            "background_writes": background_writes,
            "allocation": allocation,
            "allocation_writes": allocation_writes,
            "by_syscall": by_syscall,
        }

    def window_data(window: int) -> tuple[list[list[dict]], np.ndarray]:
        per_core = []
        rows = []
        for case in cases:
            case_cores = [
                core_channels(core, window)
                for core in case["report"]["cores"]
            ]
            per_core.append(case_cores)
            rows.append(
                [
                    sum(core["background_reads"] for core in case_cores),
                    sum(core["background_writes"] for core in case_cores),
                    sum(core["allocation"] for core in case_cores),
                    *[
                        sum(
                            sum(core["by_syscall"].get(sysnum, (0, 0)))
                            for core in case_cores
                        )
                        for sysnum in PAGE_FAULT_ALLOCATION_SYSCALLS
                    ],
                    sum(
                        core["allocation_writes"] for core in case_cores
                    ),
                ]
            )
        return per_core, np.asarray(rows, dtype=float)

    def fit_model(matrix: np.ndarray, labels: np.ndarray) -> dict:
        if matrix.shape[1] != 8:
            raise ValueError("page-fault hierarchical matrix must have 8 columns")
        penalties = np.zeros((7, 8), dtype=float)
        penalties[0, 0] = PAGE_FAULT_BACKGROUND_ZERO_PSEUDO_CANDIDATES
        penalties[1, 1] = PAGE_FAULT_BACKGROUND_ZERO_PSEUDO_CANDIDATES
        for offset in range(4):
            penalties[2 + offset, 3 + offset] = (
                PAGE_FAULT_SYSCALL_SHRINKAGE_PSEUDO_CANDIDATES
            )
        penalties[6, 7] = PAGE_FAULT_WRITE_SHRINKAGE_PSEUDO_CANDIDATES
        augmented_matrix = np.vstack((matrix, penalties))
        augmented_target = np.concatenate((labels, np.zeros(7)))
        raw, _, _, _ = np.linalg.lstsq(
            augmented_matrix, augmented_target, rcond=-1
        )
        background_read = float(np.clip(raw[0], 0.0, 1.0))
        background_write = float(np.clip(raw[1], 0.0, 1.0))
        allocation_global = float(np.clip(raw[2], 0.0, 1.0))
        allocation_write = float(
            np.clip(allocation_global + raw[7], 0.0, 1.0)
        )
        table = {}
        for offset, sysnum in enumerate(PAGE_FAULT_ALLOCATION_SYSCALLS):
            read = float(
                np.clip(allocation_global + raw[3 + offset], 0.0, 1.0)
            )
            write = float(np.clip(read + raw[7], 0.0, 1.0))
            table[sysnum] = {
                "read_ppm": rounded(read * 1_000_000),
                "write_ppm": rounded(write * 1_000_000),
            }
        return {
            "background_read_ppm": rounded(background_read * 1_000_000),
            "background_write_ppm": rounded(background_write * 1_000_000),
            "allocation_read_ppm": rounded(allocation_global * 1_000_000),
            "allocation_write_ppm": rounded(allocation_write * 1_000_000),
            "allocation_table": table,
            "raw_coefficients": [float(value) for value in raw],
        }

    def predict_case(case_cores: list[dict], model: dict) -> int:
        predicted = 0
        for core in case_cores:
            predicted += (
                core["background_reads"]
                * model["background_read_ppm"]
                // 1_000_000
            )
            predicted += (
                core["background_writes"]
                * model["background_write_ppm"]
                // 1_000_000
            )
            for sysnum, (reads, writes) in core["by_syscall"].items():
                probability = model["allocation_table"].get(
                    sysnum,
                    {
                        "read_ppm": model["allocation_read_ppm"],
                        "write_ppm": model["allocation_write_ppm"],
                    },
                )
                predicted += reads * probability["read_ppm"] // 1_000_000
                predicted += writes * probability["write_ppm"] // 1_000_000
        return predicted

    def error_summary(reference: np.ndarray, predicted: np.ndarray) -> dict:
        absolute = np.abs(predicted - reference)
        nonzero = reference != 0
        ape = absolute[nonzero] / reference[nonzero] * 100.0
        return {
            "sse_events": int(np.dot(predicted - reference, predicted - reference)),
            "rmse_events_per_case": float(np.sqrt(np.mean((predicted - reference) ** 2))),
            "mae_events_per_case": float(np.mean(absolute)),
            "wape_percent": float(
                absolute.sum() / reference.sum() * 100.0
            )
            if reference.sum()
            else None,
            "mape_percent_nonzero_reference": float(np.mean(ape))
            if len(ape)
            else None,
            "ape_p50_percent_nonzero_reference": float(np.percentile(ape, 50))
            if len(ape)
            else None,
            "ape_p90_percent_nonzero_reference": float(np.percentile(ape, 90))
            if len(ape)
            else None,
            "ape_p99_percent_nonzero_reference": float(np.percentile(ape, 99))
            if len(ape)
            else None,
            "zero_reference_cases": int((~nonzero).sum()),
        }

    fits = []
    windows = (*PAGE_FAULT_ALLOCATION_RECENCY_UPPER_BOUNDS[:-1], 0)
    for window in windows:
        per_core, matrix = window_data(window)
        model = fit_model(matrix, target)
        predicted = np.asarray(
            [predict_case(core_rows, model) for core_rows in per_core],
            dtype=float,
        )
        groups: dict[str, list[int]] = {}
        for index, case in enumerate(cases):
            groups.setdefault(case["workload_label"], []).append(index)
        heldout = np.zeros(len(cases), dtype=float)
        heldout_cycles = np.zeros(len(cases), dtype=float)
        folds = []
        if len(groups) > 1:
            for workload, test_indices in sorted(groups.items()):
                train_indices = [
                    index
                    for index in range(len(cases))
                    if index not in test_indices
                ]
                fold_model = fit_model(
                    matrix[train_indices], target[train_indices]
                )
                fold_service_cycles = rounded(
                    float(target_cycles[train_indices].sum())
                    / max(1.0, float(target[train_indices].sum()))
                )
                fold_predictions = []
                for index in test_indices:
                    value = predict_case(per_core[index], fold_model)
                    heldout[index] = value
                    heldout_cycles[index] = value * fold_service_cycles
                    fold_predictions.append(value)
                folds.append(
                    {
                        "held_out_workload": workload,
                        "held_out_cases": len(test_indices),
                        "reference_events": int(target[test_indices].sum()),
                        "predicted_events": int(sum(fold_predictions)),
                        "service_cycles": fold_service_cycles,
                    }
                )
            selection_predictions = heldout
            selection_method = "leave-one-workload-out"
        else:
            heldout = predicted.copy()
            heldout_cycles = predicted * final_service_cycles
            selection_predictions = predicted
            selection_method = "training-fallback-single-workload"
        heldout_error = selection_predictions - target
        training_error = predicted - target
        fits.append(
            {
                "window": window,
                "matrix": matrix,
                "per_core": per_core,
                "model": model,
                "predicted": predicted,
                "heldout_predicted": heldout,
                "heldout_predicted_cycles": heldout_cycles,
                "folds": folds,
                "selection_method": selection_method,
                "score": (
                    int(np.dot(heldout_error, heldout_error)),
                    abs(int(selection_predictions.sum() - target.sum())),
                    int(np.dot(training_error, training_error)),
                    window == 0,
                    window,
                ),
            }
        )

    best = min(fits, key=lambda fit: fit["score"])
    matrix = best["matrix"]
    model = best["model"]
    predicted_per_case = [int(value) for value in best["predicted"]]
    heldout_per_case = [int(value) for value in best["heldout_predicted"]]
    predicted_cycles_per_case = [
        value * final_service_cycles for value in predicted_per_case
    ]
    heldout_cycles_per_case = [
        int(value) for value in best["heldout_predicted_cycles"]
    ]
    training_summary = error_summary(
        target, np.asarray(predicted_per_case, dtype=float)
    )
    heldout_summary = error_summary(
        target, np.asarray(heldout_per_case, dtype=float)
    )
    allocation_support = {
        str(sysnum): int(matrix[:, 3 + offset].sum())
        for offset, sysnum in enumerate(PAGE_FAULT_ALLOCATION_SYSCALLS)
    }
    return {
        "allocation_window_records": best["window"],
        "background_probability_ppm": model["background_read_ppm"],
        "background_write_probability_ppm": model[
            "background_write_ppm"
        ],
        "allocation_probability_ppm": model["allocation_read_ppm"],
        "allocation_write_probability_ppm": model[
            "allocation_write_ppm"
        ],
        "allocation_probability_table": {
            str(sysnum): probabilities
            for sysnum, probabilities in model["allocation_table"].items()
        },
        "allocation_candidate_support_by_syscall": allocation_support,
        "background_read_candidates": int(matrix[:, 0].sum()),
        "background_write_candidates": int(matrix[:, 1].sum()),
        "background_candidates": int(matrix[:, :2].sum()),
        "allocation_candidates": int(matrix[:, 2].sum()),
        "allocation_write_candidates": int(matrix[:, 7].sum()),
        "first_touch_candidates": int(matrix[:, :3].sum()),
        "reference_events": int(target.sum()),
        "predicted_events": sum(predicted_per_case),
        "training_error": training_summary,
        "active_cycle_training_error": error_summary(
            target_cycles,
            np.asarray(predicted_cycles_per_case, dtype=float),
        ),
        "workload_heldout": {
            "partition_only": True,
            "selection_method": best["selection_method"],
            "error": heldout_summary,
            "active_cycle_error": error_summary(
                target_cycles,
                np.asarray(heldout_cycles_per_case, dtype=float),
            ),
            "wape_worse_than_training": (
                heldout_summary["wape_percent"]
                > training_summary["wape_percent"]
            )
            if heldout_summary["wape_percent"] is not None
            and training_summary["wape_percent"] is not None
            else None,
            "folds": best["folds"],
        },
        "window_selection": [
            {
                "allocation_window_records": fit["window"],
                "heldout_sse_events": fit["score"][0],
                "heldout_aggregate_absolute_error_events": fit["score"][1],
                "training_sse_events": fit["score"][2],
            }
            for fit in fits
        ],
        "regularization": {
            "background_zero_pseudo_candidates": (
                PAGE_FAULT_BACKGROUND_ZERO_PSEUDO_CANDIDATES
            ),
            "syscall_deviation_pseudo_candidates": (
                PAGE_FAULT_SYSCALL_SHRINKAGE_PSEUDO_CANDIDATES
            ),
            "allocation_write_deviation_pseudo_candidates": (
                PAGE_FAULT_WRITE_SHRINKAGE_PSEUDO_CANDIDATES
            ),
            "unseen_or_zero_support_syscall_fallback": "global-allocation",
        },
        "per_case": [
            {
                "case": case["case_label"],
                "workload": case["workload_label"],
                "oracle": str(case["oracle_path"]),
                "background_read_candidates": int(matrix[index, 0]),
                "background_write_candidates": int(matrix[index, 1]),
                "background_candidates": int(matrix[index, :2].sum()),
                "allocation_candidates": int(matrix[index, 2]),
                "reference_events": int(target[index]),
                "predicted_events": predicted_per_case[index],
                "heldout_predicted_events": heldout_per_case[index],
                "reference_active_cycles": int(target_cycles[index]),
                "predicted_active_cycles": predicted_cycles_per_case[index],
                "heldout_predicted_active_cycles": (
                    heldout_cycles_per_case[index]
                ),
                "heldout_ape_percent": (
                    abs(heldout_per_case[index] - int(target[index]))
                    / int(target[index])
                    * 100.0
                    if int(target[index])
                    else None
                ),
            }
            for index, case in enumerate(cases)
        ],
        "method": "strong-shrinkage-syscall-read-write-recency-lstsq-v4",
        "degrees_of_freedom": 8,
        "workload_id_is_inference_input": False,
    }


def profile_string(profile: dict) -> str:
    return ":".join(str(int(profile[field])) for field in PROFILE_FIELDS)


def main() -> int:
    args = parse_args()
    cases = [load_case(Path(a), Path(b)) for a, b in args.case]
    services = {
        "syscall": rounded(
            total_cycles(cases, "syscall_kernel_cycles")
            / max(1, total_count(cases, "syscall"))
        ),
        "page_fault": rounded(
            total_cycles(cases, "page_fault_kernel_cycles")
            / max(1, total_count(cases, "page_fault"))
        ),
        "irq": rounded(
            total_cycles(cases, "irq_kernel_cycles")
            / max(1, total_count(cases, "irq"))
        ),
    }
    exact_pmu = has_exact_class_pmu(cases)
    if not exact_pmu and not args.allow_legacy_pmu:
        raise ValueError(
            "formal calibration requires taotrace-path-class-v3 exactly-once "
            "per-class/per-sysnum PMU; pass --allow-legacy-pmu only for "
            "diagnostics"
        )
    if exact_pmu:
        pmu_profiles = {
            name: profile_for_event_totals(
                services[name],
                exact_class_pmu_totals(cases, name),
                total_count(cases, name),
            )
            for name in EVENT_CLASSES
        }
        syscall_stats = exact_syscall_stats(cases)
        syscall_table = {
            str(sysnum): profile_for_event_totals(
                rounded(values["cycles"] / max(1, values["count"])),
                values["pmu"],
                values["count"],
            )
            for sysnum, values in sorted(syscall_stats.items())
            if values["count"]
        }
        pmu_attribution = {
            "method": "exact-taotrace-kernel-class-v2",
            "classes": list(EVENT_CLASSES),
            "syscall_profiles": "exact-per-sysnum",
            "idle_excluded_from_user_plus_kernel": True,
        }
        regression = None
    else:
        pmu_rates, regression = fit_pmu(cases)
        pmu_profiles = {
            name: profile_for_service(services[name], pmu_rates)
            for name in EVENT_CLASSES
        }
        syscall_services = syscall_service_table(cases)
        syscall_table = {
            str(sysnum): profile_for_service(service, pmu_rates)
            for sysnum, service in syscall_services.items()
        }
        pmu_attribution = {
            "method": "common-active-kernel-cycle-rate-v1",
            "per_cycle_rates": pmu_rates,
            "limitation": (
                "legacy oracle exposes only :u/:uk PMU scopes; per-class "
                "instruction mixes are not identifiable"
            ),
        }
    page_fault_fit = fit_page_fault_probabilities(cases)
    semantic_fallback_fit = fit_semantic_page_fault_fallback(cases)
    irq_events = total_count(cases, "irq")
    (
        irq_period_cycles,
        foreground_cycles,
        predicted_irq_events,
    ) = fit_irq_period(cases)

    payload = {
        "schema": "fastsim-kernel-event-calibration-v1",
        "calibration_cases": [
            {
                "oracle": str(case["oracle_path"].resolve()),
                "user_report": str(case["report_path"].resolve()),
            }
            for case in cases
        ],
        "base_config": (
            str(args.base_config.resolve()) if args.base_config else None
        ),
        "formal_pmu_eligible": exact_pmu,
        "inference_inputs": [
            "syscall_number",
            "syscall_arguments_and_return",
            "token_to_virtual_page_mapping",
            "first_touch_virtual_page_token",
            "first_touch_read_or_write",
            "records_since_allocation_syscall",
            "foreground_core_cycles",
        ],
        "syscall": {
            "default_service_cycles": services["syscall"],
            "profiles": syscall_table,
        },
        "page_fault": {
            **page_fault_fit,
            "syscall_semantic_fallback": semantic_fallback_fit,
            "allocation_syscalls": list(PAGE_FAULT_ALLOCATION_SYSCALLS),
            "profile": pmu_profiles["page_fault"],
        },
        "irq": {
            "period_cycles": irq_period_cycles,
            "period_fit": "per-core-foreground-count-plateau-v1",
            "foreground_cycles": foreground_cycles,
            "reference_events": irq_events,
            "predicted_events": predicted_irq_events,
            "profile": pmu_profiles["irq"],
        },
        "pmu_attribution": pmu_attribution,
        "pmu_nonnegative_regression": regression,
    }

    syscall_entries = [
        f"{sysnum}:{profile_string(profile)}"
        for sysnum, profile in sorted(
            syscall_table.items(), key=lambda item: int(item[0])
        )
    ]
    page_fault_allocation_entries = [
        f"{sysnum}:{probability['read_ppm']}:{probability['write_ppm']}"
        for sysnum, probability in sorted(
            page_fault_fit["allocation_probability_table"].items(),
            key=lambda item: int(item[0]),
        )
    ]
    generated_config_lines = [
            "# Generated by tools/calibrate_kernel_event_profiles.py.",
            "# Calibration consumes oracle data; inference consumes only trace-visible facts.",
            "measurement.scope = user-plus-kernel",
            f"syscall.service_latency = {services['syscall']}",
            "syscall.cost_model = false",
            "syscall.event_model = true",
            "syscall.event_default_profile = "
            + profile_string(pmu_profiles["syscall"]),
            "trace.require_virtual_page_token = true",
            "trace.allow_cross_page_without_virtual_token = true",
            "page_fault.event_model = true",
            "page_fault.syscall_semantic_model = "
            + (
                "true"
                if semantic_fallback_fit["method"] != "disabled"
                else "false"
            ),
            "page_fault.syscall_semantic_fallback_write_probability_ppm = "
            + str(semantic_fallback_fit["write_probability_ppm"]),
            "page_fault.allocation_syscalls = "
            + ",".join(
                str(value) for value in PAGE_FAULT_ALLOCATION_SYSCALLS
            ),
            "page_fault.allocation_window_records = "
            + str(page_fault_fit["allocation_window_records"]),
            "page_fault.probability_ppm = "
            + str(page_fault_fit["background_probability_ppm"]),
            "page_fault.background_write_probability_ppm = "
            + str(page_fault_fit["background_write_probability_ppm"]),
            "page_fault.allocation_probability_ppm = "
            + str(page_fault_fit["allocation_probability_ppm"]),
            "page_fault.allocation_write_probability_ppm = "
            + str(page_fault_fit["allocation_write_probability_ppm"]),
            "page_fault.allocation_probability_table = "
            + ",".join(page_fault_allocation_entries),
            "page_fault.event_profile = "
            + profile_string(pmu_profiles["page_fault"]),
            "irq.event_model = true",
            f"irq.period_cycles = {irq_period_cycles}",
            "irq.event_profile = " + profile_string(pmu_profiles["irq"]),
            "",
        ]
    if syscall_entries:
        generated_config_lines.insert(
            7, "syscall.event_table = " + ",".join(syscall_entries)
        )
    generated_config = "\n".join(generated_config_lines)
    config = generated_config
    if args.base_config is not None:
        config = (
            args.base_config.read_text().rstrip()
            + "\n\n"
            + generated_config
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.config_output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    args.config_output.write_text(config)
    print(
        json.dumps(
            {
                "cases": len(cases),
                "syscall_profiles": len(syscall_table),
                "page_fault_background_probability_ppm": page_fault_fit[
                    "background_probability_ppm"
                ],
                "page_fault_background_write_probability_ppm": page_fault_fit[
                    "background_write_probability_ppm"
                ],
                "page_fault_allocation_probability_ppm": page_fault_fit[
                    "allocation_probability_ppm"
                ],
                "page_fault_allocation_write_probability_ppm": (
                    page_fault_fit["allocation_write_probability_ppm"]
                ),
                "page_fault_workload_heldout_wape_percent": (
                    page_fault_fit["workload_heldout"]["error"][
                        "wape_percent"
                    ]
                ),
                "page_fault_workload_heldout_active_cycle_wape_percent": (
                    page_fault_fit["workload_heldout"][
                        "active_cycle_error"
                    ]["wape_percent"]
                ),
                "page_fault_allocation_window_records": page_fault_fit[
                    "allocation_window_records"
                ],
                "page_fault_syscall_semantic_fallback_write_probability_ppm": (
                    semantic_fallback_fit["write_probability_ppm"]
                ),
                "page_fault_syscall_semantic_fallback_heldout_wape_percent": (
                    semantic_fallback_fit["workload_heldout"]["error"][
                        "wape_percent"
                    ]
                ),
                "irq_period_cycles": irq_period_cycles,
                "output": str(args.output),
                "config_output": str(args.config_output),
                "base_config": (
                    str(args.base_config) if args.base_config else None
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
