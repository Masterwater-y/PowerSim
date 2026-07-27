"""Event- and window-level audits for functional branch replay.

The replay consumes only the functional aligned stream.  Gem5 labels are read
from the separately built v29 cache only after ``process()`` returns for the
matching retired control UOP.
"""
from __future__ import annotations

import math
import os
from typing import Any, Iterable, Mapping, Sequence

from .config import ReplayConfig
from .io import discover_aligned_files, iter_aligned_events
from .replay import BranchEvent, BranchPrediction, TournamentBPUReplay


BINARY_COUNT_KEYS = ("tp", "fp", "fn", "tn")
WINDOW_SUM_KEYS = (
    "windows_total",
    "branch_windows",
    "true_miss_windows",
    "branches",
    "predicted_misses",
    "true_misses",
    "abs_error_sum",
    "signed_error_sum",
    "exact_windows",
    "within_one_windows",
    "rate_abs_error_pp_sum",
    "sum_pred",
    "sum_true",
    "sum_pred_sq",
    "sum_true_sq",
    "sum_pred_true",
)


def empty_binary_counts() -> dict[str, int]:
    return {key: 0 for key in BINARY_COUNT_KEYS}


def add_binary_observation(
    counts: dict[str, int], predicted: bool, actual: bool
) -> None:
    if predicted and actual:
        counts["tp"] += 1
    elif predicted:
        counts["fp"] += 1
    elif actual:
        counts["fn"] += 1
    else:
        counts["tn"] += 1


def summarize_binary_counts(counts: Mapping[str, Any]) -> dict[str, Any]:
    raw = {key: int(counts.get(key, 0)) for key in BINARY_COUNT_KEYS}
    tp, fp, fn, tn = (raw[key] for key in BINARY_COUNT_KEYS)
    total = tp + fp + fn + tn
    predicted = tp + fp
    actual = tp + fn
    precision = tp / predicted if predicted else float("nan")
    recall = tp / actual if actual else float("nan")
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if precision + recall > 0
        else float("nan")
    )
    mcc_denominator = math.sqrt(
        max(0, (tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    )
    return {
        **raw,
        "events": total,
        "predicted_misses": predicted,
        "true_misses": actual,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "event_mismatch_rate": (fp + fn) / total if total else float("nan"),
        "false_positives_per_1k_branches": 1000.0 * fp / total if total else float("nan"),
        "false_negatives_per_1k_branches": 1000.0 * fn / total if total else float("nan"),
        "count_signed_error": predicted - actual,
        "count_abs_relative_error": abs(predicted - actual) / max(1, actual),
        "matthews_correlation": (
            (tp * tn - fp * fn) / mcc_denominator
            if mcc_denominator else float("nan")
        ),
    }


def merge_binary_reports(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    combined = empty_binary_counts()
    for report in reports:
        for key in BINARY_COUNT_KEYS:
            combined[key] += int(report.get(key, 0))
    return summarize_binary_counts(combined)


class FixedUopWindowAudit:
    def __init__(self, n_uops: int, window_size: int) -> None:
        try:
            import numpy as np
        except Exception as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("numpy is required for branch window audit") from exc
        self.window_size = int(window_size)
        self.n_uops = int(n_uops)
        if self.window_size <= 0 or self.n_uops <= 0:
            raise ValueError("window_size and n_uops must be positive")
        count = (self.n_uops + self.window_size - 1) // self.window_size
        self.branch_counts = np.zeros(count, dtype=np.uint32)
        self.predicted_counts = np.zeros(count, dtype=np.uint32)
        self.true_counts = np.zeros(count, dtype=np.uint32)

    def add(self, uop_index: int, predicted: bool, actual: bool) -> None:
        index = int(uop_index)
        if not 0 <= index < self.n_uops:
            raise IndexError(f"UOP index {index} outside [0,{self.n_uops})")
        window = index // self.window_size
        self.branch_counts[window] += 1
        self.predicted_counts[window] += int(predicted)
        self.true_counts[window] += int(actual)

    def report(self) -> dict[str, Any]:
        import numpy as np

        mask = self.branch_counts > 0
        branches = self.branch_counts[mask].astype(np.float64)
        predicted = self.predicted_counts[mask].astype(np.float64)
        true = self.true_counts[mask].astype(np.float64)
        difference = predicted - true
        absolute = np.abs(difference)
        branch_windows = int(mask.sum())
        return summarize_window_sums({
            "window_size_uops": self.window_size,
            "windows_total": len(self.branch_counts),
            "branch_windows": branch_windows,
            "true_miss_windows": int(np.count_nonzero(true > 0)),
            "branches": int(branches.sum()),
            "predicted_misses": int(predicted.sum()),
            "true_misses": int(true.sum()),
            "abs_error_sum": float(absolute.sum()),
            "signed_error_sum": float(difference.sum()),
            "exact_windows": int(np.count_nonzero(difference == 0)),
            "within_one_windows": int(np.count_nonzero(absolute <= 1)),
            "rate_abs_error_pp_sum": float(
                (100.0 * absolute / branches).sum()
            ) if branch_windows else 0.0,
            "sum_pred": float(predicted.sum()),
            "sum_true": float(true.sum()),
            "sum_pred_sq": float((predicted * predicted).sum()),
            "sum_true_sq": float((true * true).sum()),
            "sum_pred_true": float((predicted * true).sum()),
        })


def summarize_window_sums(values: Mapping[str, Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    for key in WINDOW_SUM_KEYS:
        value = values.get(key, 0)
        raw[key] = float(value) if key.startswith("sum_") or key.endswith("_sum") else int(value)
    branch_windows = int(raw["branch_windows"])
    true_misses = int(raw["true_misses"])
    n = branch_windows
    numerator = n * float(raw["sum_pred_true"]) - float(raw["sum_pred"]) * float(raw["sum_true"])
    pred_variance = n * float(raw["sum_pred_sq"]) - float(raw["sum_pred"]) ** 2
    true_variance = n * float(raw["sum_true_sq"]) - float(raw["sum_true"]) ** 2
    denominator = math.sqrt(max(0.0, pred_variance * true_variance))
    return {
        "window_size_uops": int(values.get("window_size_uops", 0)),
        **raw,
        "count_mae_per_branch_window": (
            float(raw["abs_error_sum"]) / branch_windows
            if branch_windows else float("nan")
        ),
        "normalized_count_l1": float(raw["abs_error_sum"]) / max(1, true_misses),
        "signed_count_bias_per_branch_window": (
            float(raw["signed_error_sum"]) / branch_windows
            if branch_windows else float("nan")
        ),
        "exact_match_fraction": (
            int(raw["exact_windows"]) / branch_windows
            if branch_windows else float("nan")
        ),
        "within_one_fraction": (
            int(raw["within_one_windows"]) / branch_windows
            if branch_windows else float("nan")
        ),
        "rate_mae_pp": (
            float(raw["rate_abs_error_pp_sum"]) / branch_windows
            if branch_windows else float("nan")
        ),
        "count_pearson": numerator / denominator if denominator else float("nan"),
    }


def merge_window_reports(reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    reports = list(reports)
    if not reports:
        return summarize_window_sums({})
    sizes = {int(report.get("window_size_uops", 0)) for report in reports}
    if len(sizes) != 1:
        raise ValueError(f"cannot merge different window sizes: {sorted(sizes)}")
    combined: dict[str, Any] = {key: 0 for key in WINDOW_SUM_KEYS}
    for report in reports:
        for key in WINDOW_SUM_KEYS:
            combined[key] += report.get(key, 0)
    combined["window_size_uops"] = sizes.pop()
    return summarize_window_sums(combined)


def _event_kind(event: BranchEvent) -> int:
    return (
        1
        | (int(event.conditional) << 1)
        | (int(event.indirect) << 2)
        | (int(event.call) << 3)
        | (int(event.return_) << 4)
    )


def _prediction_reason(prediction: BranchPrediction) -> str:
    reasons = []
    if prediction.direction_miss:
        reasons.append("direction")
    elif prediction.final_direction_miss:
        reasons.append("final_direction")
    if prediction.target_unavailable_miss:
        reasons.append("target_unavailable")
    elif prediction.target_miss:
        reasons.append("wrong_target")
    return "+".join(reasons) if reasons else "predicted_correct"


def _group_add(
    groups: dict[str, dict[str, int]],
    name: str,
    predicted: bool,
    actual: bool,
) -> None:
    counts = groups.setdefault(str(name), empty_binary_counts())
    add_binary_observation(counts, predicted, actual)


def audit_core_stream(
    events: Iterable[BranchEvent],
    arrays: Mapping[str, Any],
    config: ReplayConfig,
    *,
    core_id: int,
    window_sizes: Sequence[int] = (256, 1024),
    cold_branches: int = 4096,
) -> dict[str, Any]:
    """Audit one core while keeping gem5 labels out of replay transitions."""
    import numpy as np
    from tcsim.v29.contracts import FIELD_INDEX

    branches = np.asarray(arrays["branch"], dtype=np.uint8)
    branch_indices = np.flatnonzero(branches)
    n_uops = len(branches)
    labels = arrays["branch_miss"]
    macro_pc = arrays["macro_pc"]
    fields = arrays["fields"]
    if len(labels) != n_uops or len(macro_pc) != n_uops or len(fields) != n_uops:
        raise RuntimeError(f"core {core_id} cache array length mismatch")

    replay = TournamentBPUReplay(config)
    event_counts = empty_binary_counts()
    by_type: dict[str, dict[str, int]] = {}
    by_provider: dict[str, dict[str, int]] = {}
    by_reason: dict[str, dict[str, int]] = {}
    by_segment: dict[str, dict[str, int]] = {}
    windows = {
        int(size): FixedUopWindowAudit(n_uops, int(size))
        for size in window_sizes
    }
    pc_mismatches = 0
    kind_mismatches = 0
    taken_mismatches = 0
    raw_branches = 0
    for ordinal, event in enumerate(events):
        raw_branches += 1
        if ordinal >= len(branch_indices):
            raise RuntimeError(
                f"core {core_id} raw branch count exceeds cache at ordinal {ordinal}"
            )
        uop_index = int(branch_indices[ordinal])
        pc_mismatches += int(int(macro_pc[uop_index]) != int(event.pc))
        cached_kind = int(fields[uop_index, FIELD_INDEX["branch_kind"]])
        kind_mismatches += int(cached_kind != _event_kind(event))
        cached_taken = int(fields[uop_index, FIELD_INDEX["branch_taken"]]) == 2
        taken_mismatches += int(cached_taken != bool(event.taken))

        # The oracle label is deliberately not read until replay has completed
        # the prediction and all state transitions for this branch.
        prediction = replay.process(event)
        actual_miss = bool(int(labels[uop_index]))
        predicted_miss = bool(prediction.full_miss)
        add_binary_observation(event_counts, predicted_miss, actual_miss)
        _group_add(
            by_type, prediction.branch_type, predicted_miss, actual_miss
        )
        _group_add(
            by_provider, prediction.target_provider, predicted_miss, actual_miss
        )
        _group_add(
            by_reason, _prediction_reason(prediction), predicted_miss, actual_miss
        )
        _group_add(
            by_segment,
            "cold" if ordinal < int(cold_branches) else "steady",
            predicted_miss,
            actual_miss,
        )
        for window in windows.values():
            window.add(uop_index, predicted_miss, actual_miss)

    if raw_branches != len(branch_indices):
        raise RuntimeError(
            f"core {core_id} branch count mismatch raw={raw_branches} "
            f"cache={len(branch_indices)}"
        )
    replay_report = replay.report()
    alignment = {
        "raw_branches": raw_branches,
        "cache_branches": int(len(branch_indices)),
        "branch_count_mismatches": int(raw_branches != len(branch_indices)),
        "pc_mismatches": pc_mismatches,
        "kind_mismatches": kind_mismatches,
        "taken_mismatches": taken_mismatches,
        "functional_history_checks": int(replay_report["functional_history_checks"]),
        "functional_history_mismatches": int(
            replay_report["functional_history_mismatches"]
        ),
    }
    alignment["passed"] = not any(
        int(alignment[key])
        for key in (
            "branch_count_mismatches", "pc_mismatches", "kind_mismatches",
            "taken_mismatches", "functional_history_mismatches",
        )
    )
    return {
        "core_id": int(core_id),
        "n_uops": n_uops,
        "alignment": alignment,
        "event": summarize_binary_counts(event_counts),
        "windows": {str(size): window.report() for size, window in windows.items()},
        "by_branch_type": {
            name: summarize_binary_counts(counts)
            for name, counts in sorted(by_type.items())
        },
        "by_provider": {
            name: summarize_binary_counts(counts)
            for name, counts in sorted(by_provider.items())
        },
        "by_replay_reason": {
            name: summarize_binary_counts(counts)
            for name, counts in sorted(by_reason.items())
        },
        "by_segment": {
            name: summarize_binary_counts(counts)
            for name, counts in sorted(by_segment.items())
        },
        "replay_components": replay.stats.report(),
        "oracle_labels_consumed_as_input": False,
    }


def _merge_named_binary(
    reports: Iterable[Mapping[str, Mapping[str, Any]]]
) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for report in reports:
        for name, values in report.items():
            groups.setdefault(str(name), []).append(values)
    return {
        name: merge_binary_reports(values)
        for name, values in sorted(groups.items())
    }


def aggregate_audit_reports(reports: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    alignment_keys = (
        "raw_branches", "cache_branches", "branch_count_mismatches",
        "pc_mismatches", "kind_mismatches", "taken_mismatches",
        "functional_history_checks", "functional_history_mismatches",
    )
    alignment = {
        key: sum(int(report["alignment"].get(key, 0)) for report in reports)
        for key in alignment_keys
    }
    alignment["cores"] = sum(
        int(report["alignment"].get("cores", 1)) for report in reports
    )
    alignment["passed_cores"] = sum(
        int(report["alignment"].get(
            "passed_cores", int(bool(report["alignment"].get("passed")))
        ))
        for report in reports
    )
    alignment["passed"] = alignment["passed_cores"] == alignment["cores"] and not any(
        int(alignment[key])
        for key in (
            "branch_count_mismatches", "pc_mismatches", "kind_mismatches",
            "taken_mismatches", "functional_history_mismatches",
        )
    )
    window_sizes = sorted({
        name for report in reports for name in report.get("windows", {})
    }, key=int)
    return {
        "alignment": alignment,
        "event": merge_binary_reports(report["event"] for report in reports),
        "windows": {
            size: merge_window_reports(
                report["windows"][size]
                for report in reports if size in report.get("windows", {})
            )
            for size in window_sizes
        },
        "by_branch_type": _merge_named_binary(
            report.get("by_branch_type", {}) for report in reports
        ),
        "by_provider": _merge_named_binary(
            report.get("by_provider", {}) for report in reports
        ),
        "by_replay_reason": _merge_named_binary(
            report.get("by_replay_reason", {}) for report in reports
        ),
        "by_segment": _merge_named_binary(
            report.get("by_segment", {}) for report in reports
        ),
        "oracle_labels_consumed_as_input": False,
    }


def audit_trace(
    trace_dir: str,
    cache_dir: str,
    config: ReplayConfig,
    *,
    window_sizes: Sequence[int] = (256, 1024),
    cold_branches: int = 4096,
) -> dict[str, Any]:
    """Audit all physical cores in one trace using the labeled v29 cache."""
    import numpy as np

    aligned = dict(discover_aligned_files(trace_dir))
    core_root = os.path.join(os.path.abspath(cache_dir), "cores")
    cache_core_ids = sorted(
        int(name) for name in os.listdir(core_root)
        if name.isdigit() and os.path.isdir(os.path.join(core_root, name))
    )
    if sorted(aligned) != cache_core_ids:
        raise RuntimeError(
            f"raw/cache core mismatch raw={sorted(aligned)} cache={cache_core_ids}"
        )
    per_core = []
    for core_id in cache_core_ids:
        core_dir = os.path.join(core_root, str(core_id))
        arrays = {
            name: np.load(os.path.join(core_dir, name + ".npy"), mmap_mode="r")
            for name in ("branch", "branch_miss", "macro_pc", "fields")
        }
        per_core.append(audit_core_stream(
            iter_aligned_events(aligned[core_id]),
            arrays,
            config,
            core_id=core_id,
            window_sizes=window_sizes,
            cold_branches=cold_branches,
        ))
    return {
        **aggregate_audit_reports(per_core),
        "per_core": per_core,
        "window_sizes_uops": [int(value) for value in window_sizes],
        "cold_prefix_branches_per_core": int(cold_branches),
        "config_hash": config.stable_hash(),
        "oracle_labels_consumed_as_input": False,
    }
