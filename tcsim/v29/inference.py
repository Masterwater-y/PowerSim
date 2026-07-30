"""v29 oracle one-step evaluation and single-global-time deployment rollout.

The deployment path never selects a context with oracle commit ticks.  It owns
one virtual clock, advances predicted cursors, and consults timing arrays only
after each transition to measure drift.  This separation is deliberately kept
inside one module so it can be tested as an invariant rather than a convention
of a shell script.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import math
import multiprocessing as mp
import os
import random
import time
from typing import (
    Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional,
    Sequence, Tuple,
)

import numpy as np
import torch

from ..branch_replay import ReplayConfig, events_from_cache_arrays, replay_core_streams
from ..utils.config import TCSimConfig
from ..utils.io import dump_json, load_json
from .contracts import CHECKPOINT_SCHEMA_VERSION, FIELD_INDEX
from .dataset import (
    CONTEXT_PHASE_NAMES,
    V29FunctionalStore,
    V29TraceStore,
    discover_trace_caches,
)
from .model import TCSimV29Model, build_model
from .model import (
    BRANCH_MODE_REPLAY_EVENT,
    BRANCH_MODE_REPLAY_EVENT_HISTORY,
)
from ..v30.rollout import GSSSerialRollout
from ..v30.pmu import cache_miss_pmu_error_report


MODEL_TENSOR_KEYS = (
    "per_uop_fields",
    "dynamic_uop_fields",
    "valid_uop_mask",
    "chunk_summary",
    "relation_features",
    "uarch_features",
    "state_features",
    "branch_mask",
)
ORACLE_ONLY_KEYS = (
    "branch_miss_target",
    "commit_time_target",
    "prefix_target",
    "progress_target",
)
CONTEXT_REPORT_PHASE_NAMES = CONTEXT_PHASE_NAMES + ("call_overhead",)
FREE_TIMING_RECONSTRUCTION_CONTRACT = "canonical-retirement-gap-fp64-v1"
MIN_RETIREMENT_GAP_CYCLES = 1.0e-6
FREE_DEADLINE_CONTRACT = "persistent-absolute-uop-deadline-v1"
PER_CORE_STARVATION_GUARD = "consecutive-zero-retirement-per-core-v1"


def _mean(values: Sequence[float]) -> float:
    clean = [float(value) for value in values if math.isfinite(float(value))]
    return sum(clean) / len(clean) if clean else float("nan")


def _pctl(values: Sequence[float], percentile: float) -> float:
    clean = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not clean:
        return float("nan")
    position = max(0.0, min(100.0, float(percentile))) / 100.0 * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    weight = position - lower
    return clean[lower] * (1.0 - weight) + clean[upper] * weight


def _relative_error(predicted: float, truth: float) -> float:
    return abs(float(predicted) - float(truth)) / max(1.0e-12, abs(float(truth)))


def _event_rate(count: float, opportunities: int) -> float:
    return (
        float(count) / int(opportunities)
        if int(opportunities) > 0 else float("nan")
    )


def _attach_cache_miss_pmu_error(
    report: MutableMapping[str, Any],
    *,
    source: Optional[Mapping[str, Any]],
    store: Any,
    gss_rollout: Optional[GSSSerialRollout],
    complete: bool,
) -> None:
    """Attach the post-rollout PMU audit without contaminating model context."""
    canonical_state = (
        dict(gss_rollout.canonical.state_summary())
        if gss_rollout is not None else None
    )
    report["cache_miss_pmu_error"] = cache_miss_pmu_error_report(
        trace_dir=(str(source.get("trace_dir")) if source and source.get("trace_dir") else None),
        expected_core_ids=store.core_ids,
        canonical_state=canonical_state,
        complete=bool(complete),
    )


def _linear_slope(xs: Sequence[float], ys: Sequence[float]) -> float:
    pairs = [
        (float(x), float(y)) for x, y in zip(xs, ys)
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]
    if len(pairs) < 2:
        return float("nan")
    mean_x = sum(item[0] for item in pairs) / len(pairs)
    mean_y = sum(item[1] for item in pairs) / len(pairs)
    denominator = sum((item[0] - mean_x) ** 2 for item in pairs)
    if denominator <= 0:
        return 0.0
    return sum(
        (item[0] - mean_x) * (item[1] - mean_y) for item in pairs
    ) / denominator


class Reservoir:
    """Deterministic bounded reservoir for percentile reporting."""

    def __init__(self, capacity: int = 200000, seed: int = 29) -> None:
        self.capacity = max(1, int(capacity))
        self.values: List[float] = []
        self.seen = 0
        self._random = random.Random(int(seed))

    def add(self, values: Iterable[float]) -> None:
        for raw in values:
            value = float(raw)
            if not math.isfinite(value):
                continue
            self.seen += 1
            if len(self.values) < self.capacity:
                self.values.append(value)
                continue
            replacement = self._random.randrange(self.seen)
            if replacement < self.capacity:
                self.values[replacement] = value

    def summary(self, *, absolute: bool = False) -> Dict[str, Any]:
        values = [abs(value) for value in self.values] if absolute else self.values
        return {
            "count": int(self.seen),
            "reservoir_count": len(values),
            "mean": _mean(values),
            "p50": _pctl(values, 50),
            "p90": _pctl(values, 90),
            "p99": _pctl(values, 99),
            "max": max(values) if values else float("nan"),
        }


class ScalarErrors:
    def __init__(self, capacity: int = 200000, seed: int = 29) -> None:
        self.count = 0
        self.total = 0.0
        self.absolute_total = 0.0
        self.reservoir = Reservoir(capacity=capacity, seed=seed)

    def add(self, values: Iterable[float]) -> None:
        materialized = [float(value) for value in values if math.isfinite(float(value))]
        self.count += len(materialized)
        self.total += sum(materialized)
        self.absolute_total += sum(abs(value) for value in materialized)
        self.reservoir.add(materialized)

    def summary(self) -> Dict[str, Any]:
        absolute = self.reservoir.summary(absolute=True)
        return {
            "count": self.count,
            "signed_mean": self.total / self.count if self.count else float("nan"),
            "mae": self.absolute_total / self.count if self.count else float("nan"),
            "p50_abs": absolute["p50"],
            "p90_abs": absolute["p90"],
            "p99_abs": absolute["p99"],
            "max_abs": absolute["max"],
        }


class BinaryHistogram:
    """Streaming binary metrics without retaining every branch token."""

    def __init__(self, bins: int = 1000, calibration_bins: int = 10) -> None:
        self.bins = max(10, int(bins))
        self.calibration_bins = max(2, int(calibration_bins))
        self.positive = np.zeros(self.bins, dtype=np.int64)
        self.negative = np.zeros(self.bins, dtype=np.int64)
        self.cal_count = np.zeros(self.calibration_bins, dtype=np.int64)
        self.cal_prob = np.zeros(self.calibration_bins, dtype=np.float64)
        self.cal_label = np.zeros(self.calibration_bins, dtype=np.float64)
        self.bce_sum = 0.0
        self.brier_sum = 0.0
        self.count = 0

    def add(self, probabilities: np.ndarray, labels: np.ndarray) -> None:
        probability = np.asarray(probabilities, dtype=np.float64).reshape(-1)
        label = np.asarray(labels, dtype=np.float64).reshape(-1)
        if probability.shape != label.shape:
            raise ValueError("binary metric shape mismatch")
        if probability.size == 0:
            return
        probability = np.clip(probability, 1.0e-7, 1.0 - 1.0e-7)
        label = (label > 0.5).astype(np.float64)
        bins = np.minimum((probability * self.bins).astype(np.int64), self.bins - 1)
        calibration = np.minimum(
            (probability * self.calibration_bins).astype(np.int64),
            self.calibration_bins - 1,
        )
        np.add.at(self.positive, bins[label > 0.5], 1)
        np.add.at(self.negative, bins[label <= 0.5], 1)
        np.add.at(self.cal_count, calibration, 1)
        np.add.at(self.cal_prob, calibration, probability)
        np.add.at(self.cal_label, calibration, label)
        self.bce_sum += float(np.sum(
            -(label * np.log(probability) + (1.0 - label) * np.log(1.0 - probability))
        ))
        self.brier_sum += float(np.sum((probability - label) ** 2))
        self.count += int(probability.size)

    def summary(self) -> Dict[str, Any]:
        positives = int(self.positive.sum())
        negatives = int(self.negative.sum())
        cumulative_negative = 0
        concordant = 0.0
        for positive, negative in zip(self.positive, self.negative):
            concordant += float(positive) * (
                float(cumulative_negative) + 0.5 * float(negative)
            )
            cumulative_negative += int(negative)
        auc = (
            concordant / (positives * negatives)
            if positives > 0 and negatives > 0 else float("nan")
        )
        calibration = []
        expected_calibration_error = 0.0
        for index in range(self.calibration_bins):
            count = int(self.cal_count[index])
            if count <= 0:
                continue
            predicted = float(self.cal_prob[index]) / count
            observed = float(self.cal_label[index]) / count
            expected_calibration_error += (
                count / max(1, self.count) * abs(predicted - observed)
            )
            calibration.append({
                "bin": index,
                "count": count,
                "predicted": predicted,
                "observed": observed,
            })
        return {
            "count": self.count,
            "positives": positives,
            "negatives": negatives,
            "bce": self.bce_sum / self.count if self.count else float("nan"),
            "brier": self.brier_sum / self.count if self.count else float("nan"),
            "auc_histogram": auc,
            "ece": expected_calibration_error,
            "calibration": calibration,
        }


@dataclass
class V29Prediction:
    commit_time: np.ndarray
    commit_probability: Optional[np.ndarray]
    progress: Optional[np.ndarray]
    branch_miss_probability: np.ndarray
    valid_uop_mask: np.ndarray
    retirement_gap: Optional[np.ndarray] = None


@dataclass
class _DeadlineWindow:
    start_uop: int
    absolute_cycles: np.ndarray


class _AbsoluteDeadlineLedger:
    """Keep already predicted UOP deadlines stable across overlapping windows."""

    def __init__(self) -> None:
        self._windows: Dict[int, _DeadlineWindow] = {}
        self.reconcile_calls = 0
        self.retained_uops = 0
        self.new_uops = 0
        self.max_retained_prefix = 0

    def lookup(self, core_id: int, absolute_uop: int) -> Optional[float]:
        window = self._windows.get(int(core_id))
        if window is None:
            return None
        index = int(absolute_uop) - int(window.start_uop)
        if not 0 <= index < len(window.absolute_cycles):
            return None
        return float(window.absolute_cycles[index])

    def reconcile(
        self,
        store: V29TraceStore,
        slots: Sequence[int],
        cursors: Sequence[int],
        prediction: V29Prediction,
        *,
        now_cycles: float,
    ) -> None:
        """Replace overlapping re-predictions with their absolute deadlines.

        Only the newly exposed tail may receive new model gaps.  Its first gap
        is chained after the last retained UOP, so a stalled head cannot be
        repeatedly recharged by rebuilding the same lookahead window.
        """
        now = float(now_cycles)
        source_commit_time = np.asarray(
            prediction.commit_time, dtype=np.float64,
        ).copy()
        source_retirement_gap = (
            None
            if prediction.retirement_gap is None
            else np.asarray(
                prediction.retirement_gap, dtype=np.float64,
            ).copy()
        )
        prediction.commit_time = source_commit_time.copy()
        canonical_gap_output = np.zeros_like(
            source_commit_time, dtype=np.float64,
        )
        for row, (slot_value, cursor_value) in enumerate(zip(slots, cursors)):
            slot = int(slot_value)
            cursor = int(cursor_value)
            core_id = int(store.core_ids[slot])
            count = int(prediction.valid_uop_mask[row].sum())
            if count <= 0:
                continue
            relative = np.asarray(
                source_commit_time[row, :count], dtype=np.float64,
            )
            if np.any(~np.isfinite(relative)) or np.any(relative <= 0.0):
                raise RuntimeError("deadline ledger received invalid commit cycles")
            absolute = now + relative
            retained = 0
            previous = self._windows.get(core_id)
            if previous is not None:
                previous_end = previous.start_uop + len(previous.absolute_cycles)
                if cursor < previous.start_uop:
                    raise RuntimeError("deadline cursor moved backwards")
                retained = max(0, min(count, previous_end - cursor))
                if retained:
                    begin = cursor - previous.start_uop
                    stable = np.asarray(
                        previous.absolute_cycles[begin:begin + retained],
                        dtype=np.float64,
                    )
                    if float(stable[0]) < now - 1.0e-6:
                        raise RuntimeError(
                            "unretired UOP deadline is already behind virtual time: "
                            f"core={core_id} uop={cursor} deadline={stable[0]:.9g} "
                            f"now={now:.9g}"
                        )
                    absolute[:retained] = stable
            if retained < count and retained > 0:
                if source_retirement_gap is not None:
                    gaps = source_retirement_gap[row, :count]
                else:
                    gaps = np.diff(np.concatenate((
                        [0.0], source_commit_time[row, :count],
                    )))
                if np.any(~np.isfinite(gaps)) or np.any(gaps < -1.0e-6):
                    raise RuntimeError("deadline ledger received invalid gaps")
                tail_gaps = np.maximum(
                    gaps[retained:], MIN_RETIREMENT_GAP_CYCLES,
                )
                absolute[retained:] = absolute[retained - 1] + np.cumsum(
                    tail_gaps, dtype=np.float64,
                )
            if count > 1 and np.any(np.diff(absolute) < -1.0e-6):
                raise RuntimeError("deadline ledger produced a non-monotonic window")
            relative = absolute - now
            prediction.commit_time[row, :count] = relative
            canonical_gap = np.diff(np.concatenate(([0.0], relative)))
            canonical_gap[0] = max(0.0, canonical_gap[0])
            canonical_gap_output[row, :count] = canonical_gap
            self._windows[core_id] = _DeadlineWindow(
                start_uop=cursor,
                absolute_cycles=np.asarray(absolute, dtype=np.float64).copy(),
            )
            self.retained_uops += retained
            self.new_uops += count - retained
            self.max_retained_prefix = max(self.max_retained_prefix, retained)
        prediction.retirement_gap = canonical_gap_output
        self.reconcile_calls += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "deadline_contract": FREE_DEADLINE_CONTRACT,
            "deadline_reconcile_calls": int(self.reconcile_calls),
            "deadline_retained_uops": int(self.retained_uops),
            "deadline_new_uops": int(self.new_uops),
            "deadline_max_retained_prefix": int(self.max_retained_prefix),
        }


@dataclass
class V29ParallelWindow:
    depth: int
    start_cursors: Tuple[int, ...]
    context: Mapping[str, Any]
    prediction: V29Prediction


@dataclass
class V29ParallelWave:
    anchor_cursors: Tuple[int, ...]
    windows: List[V29ParallelWindow]
    unconditional_gap: Optional[np.ndarray] = None
    unconditional_branch_probability: Optional[np.ndarray] = None
    unconditional_valid_count: Optional[np.ndarray] = None
    current_window: int = 0
    scheduler_steps: int = 0
    full_chain_counted: bool = False


@dataclass
class V29WindowStep:
    slots: List[int]
    commit_time: np.ndarray
    branch_miss_probability: np.ndarray
    valid_uop_mask: np.ndarray


_PROCESS_CONTEXT_STORE: Optional[V29TraceStore] = None
_PROCESS_CONTEXT_STORE_KEY: Optional[Tuple[Any, ...]] = None
_PROCESS_CONTEXT_TASK_SECONDS = 0.0


def _build_context_in_process(
    request: Tuple[Any, ...],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build one context in a spawn worker without touching CUDA."""
    global _PROCESS_CONTEXT_STORE
    global _PROCESS_CONTEXT_STORE_KEY
    global _PROCESS_CONTEXT_TASK_SECONDS

    (
        cache_dir,
        functional,
        context_epoch,
        long_history_dir,
        branch_replay_dir,
        gss_sidecar_dir,
        exposure_sidecar_dir,
        allow_ready_clock_gss_sidecar,
        cursors,
        state_time_cycles,
        last_commit_cycles,
    ) = request
    sidecar_dirs = tuple(
        os.path.abspath(str(value)) if value else None
        for value in (
            long_history_dir,
            branch_replay_dir,
            gss_sidecar_dir,
            exposure_sidecar_dir,
        )
    )
    key = (
        os.path.abspath(cache_dir),
        bool(functional),
        int(context_epoch),
        *sidecar_dirs,
        bool(allow_ready_clock_gss_sidecar),
    )
    task_started = time.perf_counter()
    if _PROCESS_CONTEXT_STORE is None or _PROCESS_CONTEXT_STORE_KEY != key:
        sidecar_kwargs = {
            "long_history_dir": sidecar_dirs[0],
            "branch_replay_dir": sidecar_dirs[1],
            "gss_sidecar_dir": sidecar_dirs[2],
            "exposure_sidecar_dir": sidecar_dirs[3],
        }
        if functional:
            _PROCESS_CONTEXT_STORE = V29FunctionalStore(
                key[0], **sidecar_kwargs,
            )
        else:
            _PROCESS_CONTEXT_STORE = V29TraceStore(
                key[0],
                allow_ready_clock_gss_sidecar=bool(
                    allow_ready_clock_gss_sidecar
                ),
                **sidecar_kwargs,
            )
        _PROCESS_CONTEXT_STORE_KEY = key
        _PROCESS_CONTEXT_TASK_SECONDS = 0.0
    context = _PROCESS_CONTEXT_STORE.context_from_cursors(
        cursors,
        state_time_cycles=float(state_time_cycles),
        include_labels=False,
        last_commit_cycles=last_commit_cycles,
    )
    _PROCESS_CONTEXT_TASK_SECONDS += time.perf_counter() - task_started
    serialized = {
        key_name: (
            value.detach().cpu().numpy()
            if isinstance(value, torch.Tensor) else value
        )
        for key_name, value in context.items()
    }
    stats = _PROCESS_CONTEXT_STORE.runtime_stats()
    stats["context_task_seconds"] = _PROCESS_CONTEXT_TASK_SECONDS
    return serialized, stats


def _restore_process_context(context: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        key: torch.from_numpy(value) if isinstance(value, np.ndarray) else value
        for key, value in context.items()
    }


class V29ParallelContextPool:
    """Lane-local context workspaces over one shared read-only trace."""

    def __init__(
        self,
        store: V29TraceStore,
        depth: int,
        *,
        backend: str,
    ) -> None:
        self.workspaces = [
            store.fork_context_workspace() for _ in range(max(1, int(depth)))
        ]
        self.backend = str(backend)
        self.wall_seconds = 0.0
        self.external_lane_stats: Optional[List[Optional[Mapping[str, Any]]]] = None

    def add_wall_seconds(self, value: float) -> None:
        self.wall_seconds += max(0.0, float(value))

    def update_lane_stats(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if self.external_lane_stats is None:
            self.external_lane_stats = [None for _ in self.workspaces]
        for lane, row in enumerate(rows):
            self.external_lane_stats[lane] = dict(row)

    def runtime_stats(self) -> Dict[str, Any]:
        rows = (
            [
                row if row is not None else self.workspaces[lane].runtime_stats()
                for lane, row in enumerate(self.external_lane_stats)
            ]
            if self.external_lane_stats is not None
            else [workspace.runtime_stats() for workspace in self.workspaces]
        )
        first = dict(rows[0])
        phase_work = {
            name: sum(
                float(row.get("context_phase_seconds", {}).get(name, 0.0))
                for row in rows
            )
            for name in CONTEXT_PHASE_NAMES
        }
        worker_seconds = sum(
            float(row.get(
                "context_task_seconds",
                sum(float(value) for value in (
                    row.get("context_phase_seconds", {}) or {}
                ).values()),
            ))
            for row in rows
        )
        # Existing reports require phase buckets to reconcile to context wall
        # time.  Preserve raw CPU work separately and scale the displayed
        # phase attribution when lanes overlap.
        scale = (
            min(1.0, self.wall_seconds / worker_seconds)
            if worker_seconds > 0.0 else 0.0
        )
        first.update({
            "context_builder": (
                str(first.get("context_builder", ""))
                + f"+parallel-window-{self.backend}"
            ),
            "cpu_window_cache_policy": "last-window-per-core-per-lane",
            "cpu_window_cache_hits": sum(
                int(row.get("cpu_window_cache_hits", 0)) for row in rows
            ),
            "cpu_window_cache_misses": sum(
                int(row.get("cpu_window_cache_misses", 0)) for row in rows
            ),
            "cpu_window_cache_entries": sum(
                int(row.get("cpu_window_cache_entries", 0)) for row in rows
            ),
            "context_calls": sum(
                int(row.get("context_calls", 0)) for row in rows
            ),
            "context_phase_seconds": {
                name: phase_work[name] * scale
                for name in CONTEXT_PHASE_NAMES
            },
            "context_phase_worker_seconds": phase_work,
            "context_build_worker_seconds": worker_seconds,
            "context_build_parallel_wall_seconds": self.wall_seconds,
            "context_parallel_workers": len(rows),
            "context_parallel_backend": self.backend,
            "context_effective_parallelism": (
                worker_seconds / self.wall_seconds
                if self.wall_seconds > 0.0 else 0.0
            ),
        })
        total = (
            int(first["cpu_window_cache_hits"])
            + int(first["cpu_window_cache_misses"])
        )
        first["cpu_window_cache_hit_rate"] = (
            int(first["cpu_window_cache_hits"]) / max(1, total)
        )
        return first


def _torch_load(path: str, map_location: Any = "cpu") -> Any:
    try:
        return torch.load(path, map_location=map_location, weights_only=False)
    except TypeError:  # pragma: no cover - old torch
        return torch.load(path, map_location=map_location)


def _config_from_mapping(data: Mapping[str, Any]) -> TCSimConfig:
    return TCSimConfig(
        chunk=dict(data.get("chunk", {})),
        scheduler=dict(data.get("scheduler", {})),
        uarch=dict(data.get("uarch", {})),
        model=dict(data.get("model", {})),
        train=dict(data.get("train", {})),
    )


def _store_contract(store: V29TraceStore) -> Dict[str, Any]:
    keys = (
        "raw_trace_schema",
        "dataset_schema",
        "model_input_contract",
        "feature_schema",
        "branch_contract",
        "resource_decoder_schema",
        "resource_decoder_hash",
        "predictor_hash",
        "horizons",
        "sample_period_cycles",
        "dimensions",
    )
    contract = {key: store.meta[key] for key in keys}
    if store.long_history_contract is not None:
        contract["long_history"] = dict(store.long_history_contract)
    if store.branch_feature_contract is not None:
        contract["branch_features"] = dict(store.branch_feature_contract)
    if store.exposure_contract is not None:
        contract["exposure"] = dict(store.exposure_contract)
    return contract


class V29ModelRunner:
    """Strict checkpoint runner with a per-core last-window static cache."""

    def __init__(
        self,
        model: TCSimV29Model,
        config: TCSimConfig,
        checkpoint_meta: Mapping[str, Any],
        *,
        device: str = "cuda",
        amp_dtype: str = "bf16",
        static_cache: bool = True,
    ) -> None:
        self.device = torch.device(device)
        if self.device.type == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA requested for v29 inference but is unavailable")
        self.model = model.to(self.device).eval()
        self.config = config
        self.checkpoint_meta = dict(checkpoint_meta)
        self.contract = dict(self.checkpoint_meta["contract"])
        self.static_cache_enabled = bool(static_cache)
        self.gss_ablation_mode = "predicted-order"
        self._static_by_core: Dict[int, Tuple[Tuple[Any, ...], torch.Tensor]] = {}
        self.static_hits = 0
        self.static_misses = 0
        self.static_evictions = 0
        self._active_trace = ""
        self._model_start_event: Optional[torch.cuda.Event] = None
        self._model_end_event: Optional[torch.cuda.Event] = None
        value = str(amp_dtype).lower()
        if self.device.type != "cuda" or value in {"fp32", "float32", "none", "off"}:
            self.amp_dtype = None
        elif value in {"bf16", "bfloat16"}:
            self.amp_dtype = torch.bfloat16
        elif value in {"fp16", "float16"}:
            self.amp_dtype = torch.float16
        else:
            raise ValueError(f"unsupported v29 inference amp dtype {amp_dtype!r}")
        if self.device.type == "cuda":
            torch.set_float32_matmul_precision("high")
            self._model_start_event = torch.cuda.Event(enable_timing=True)
            self._model_end_event = torch.cuda.Event(enable_timing=True)
        self._reset_timings()

    def set_gss_ablation_mode(self, mode: str) -> None:
        value = str(mode).strip().lower()
        allowed = {
            "gap0", "state-disabled", "predicted-order", "teacher-order",
        }
        if value not in allowed:
            raise ValueError(
                f"unsupported GSS ablation mode {mode!r}; "
                f"expected one of {sorted(allowed)}"
            )
        if self.model.gss_adapter is None and value != "predicted-order":
            raise ValueError(
                "GSS ablation modes require a checkpoint with a GSS adapter"
            )
        self.gss_ablation_mode = value

    def _reset_timings(self) -> None:
        self.predict_calls = 0
        self.free_fast_path_calls = 0
        self.retirement_gap_floor_count = 0
        self.batch_transfer_seconds = 0.0
        self.model_forward_seconds = 0.0
        self.output_transfer_seconds = 0.0
        self.prediction_validation_seconds = 0.0
        self.predict_wall_seconds = 0.0

    def _autocast(self):
        if self.amp_dtype is None:
            return nullcontext()
        return torch.autocast(
            device_type=self.device.type, dtype=self.amp_dtype, enabled=True,
        )

    def begin_trace(self, store: V29TraceStore) -> None:
        expected_contract = dict(self.contract)
        expected_gss = expected_contract.pop("gss", None)
        if _store_contract(store) != expected_contract:
            raise RuntimeError(
                f"v29 checkpoint/cache contract mismatch for {store.trace_id}"
            )
        store_gss = getattr(store, "gss_contract", None)
        if expected_gss is None and store_gss is not None:
            raise RuntimeError(
                f"non-GSS checkpoint received a GSS sidecar for {store.trace_id}"
            )
        if (
            expected_gss is not None
            and store_gss is not None
            and dict(store_gss) != dict(expected_gss)
        ):
            raise RuntimeError(
                f"v30 GSS checkpoint/sidecar contract mismatch for {store.trace_id}"
            )
        self._active_trace = store.trace_id
        self._static_by_core.clear()
        self.static_hits = 0
        self.static_misses = 0
        self.static_evictions = 0
        self._reset_timings()
        if self.device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(self.device)

    def _model_batch(
        self,
        context: Mapping[str, Any],
        *,
        include_gss: bool = True,
    ) -> Dict[str, torch.Tensor]:
        batch = {
            key: context[key].to(self.device, non_blocking=True)
            for key in MODEL_TENSOR_KEYS
        }
        if self.model.long_history_dim:
            if "long_history_features" not in context:
                raise RuntimeError(
                    "v29 long-history checkpoint requires a sidecar context"
                )
            batch["long_history_features"] = context[
                "long_history_features"
            ].to(self.device, non_blocking=True)
        if self.model.branch_mode in {
            BRANCH_MODE_REPLAY_EVENT,
            BRANCH_MODE_REPLAY_EVENT_HISTORY,
        }:
            if "branch_replay_event" not in context:
                raise RuntimeError(
                    f"v29 branch_mode={self.model.branch_mode} requires replay sidecar"
                )
            batch["branch_replay_event"] = context[
                "branch_replay_event"
            ].to(self.device, non_blocking=True)
        if self.model.branch_mode == BRANCH_MODE_REPLAY_EVENT_HISTORY:
            if "branch_replay_history" not in context:
                raise RuntimeError(
                    "v29 replay_event_history checkpoint requires history sidecar"
                )
            batch["branch_replay_history"] = context[
                "branch_replay_history"
            ].to(self.device, non_blocking=True)
        if self.model.gss_exposure_dim:
            if "exposure_features" not in context:
                raise RuntimeError(
                    "v30 Exposure-v1 checkpoint requires an exposure sidecar"
                )
            batch["exposure_features"] = context[
                "exposure_features"
            ].to(self.device, non_blocking=True)
        if self.model.gss_adapter is not None and include_gss:
            self._attach_gss_batch(batch, context)
        rows = int(batch["per_uop_fields"].shape[0])
        batch["sample_ptr"] = torch.tensor([0, rows], dtype=torch.long)
        return batch

    def _attach_gss_batch(
        self,
        batch: Dict[str, torch.Tensor],
        context: Mapping[str, Any],
    ) -> None:
        if self.model.gss_adapter is None:
            raise RuntimeError("cannot attach GSS tensors to a non-GSS model")
        missing = [
            key for key in (
                "gss_uop_categorical", "gss_uop_continuous",
                "gss_memory_mask",
            ) if key not in context
        ]
        if missing:
            raise RuntimeError(
                "v30 GSS checkpoint requires online rollout or a teacher "
                f"sidecar context; missing={missing}"
            )
        for key in (
            "gss_uop_categorical", "gss_uop_continuous",
            "gss_memory_mask",
        ):
            batch[key] = context[key].to(self.device, non_blocking=True)
        compact_keys = (
            "gss_event_categorical", "gss_event_continuous",
            "gss_event_positions", "gss_event_valid",
            "gss_event_is_memory",
        )
        compact_present = [key in context for key in compact_keys]
        if any(compact_present) and not all(compact_present):
            raise RuntimeError("v30 compact GSS context is incomplete")
        if all(compact_present):
            for key in compact_keys:
                batch[key] = context[key].to(
                    self.device, non_blocking=True,
                )

    def _predict(
        self,
        store: V29TraceStore,
        context: Mapping[str, Any],
        *,
        include_horizon_outputs: bool,
        gss_rollout: Optional[GSSSerialRollout] = None,
        step_start_cycles: float = 0.0,
        deadline_lookup: Optional[Callable[[int, int], Optional[float]]] = None,
    ) -> V29Prediction:
        predict_started = time.perf_counter()
        if self._active_trace != store.trace_id:
            self.begin_trace(store)
        batch_started = time.perf_counter()
        staged_gss = gss_rollout is not None
        if staged_gss and include_horizon_outputs:
            raise RuntimeError("online staged GSS is only valid for free rollout")
        if staged_gss and self.model.gss_adapter is None:
            raise RuntimeError("online GSS rollout requires a GSS model")
        gap0 = self.gss_ablation_mode == "gap0"
        batch = self._model_batch(
            context, include_gss=not staged_gss and not gap0,
        )
        self.batch_transfer_seconds += time.perf_counter() - batch_started
        rows = int(batch["per_uop_fields"].shape[0])
        slots = [int(value) for value in context["core_slots"].tolist()]
        cursors = [int(value) for value in context["cursors"].tolist()]
        cpu_model_started = time.perf_counter()
        if self._model_start_event is not None:
            self._model_start_event.record()
        with torch.inference_mode(), self._autocast():
            if not self.static_cache_enabled:
                self.static_misses += rows
                static_tokens = self.model.static_encoder(batch["per_uop_fields"])
            else:
                cached: List[Optional[torch.Tensor]] = [None] * rows
                missing_indices: List[int] = []
                missing_keys: List[Tuple[int, Tuple[Any, ...]]] = []
                for row, (slot, cursor) in enumerate(zip(slots, cursors)):
                    core_id = int(store.core_ids[slot])
                    key = (
                        store.trace_id,
                        core_id,
                        cursor,
                        str(store.meta.get("uarch_hash", "")),
                        str(self.checkpoint_meta.get("checkpoint_id", "")),
                    )
                    current = self._static_by_core.get(core_id)
                    if current is not None and current[0] == key:
                        self.static_hits += 1
                        cached[row] = current[1]
                    else:
                        self.static_misses += 1
                        missing_indices.append(row)
                        missing_keys.append((core_id, key))
                if missing_indices:
                    index = torch.tensor(
                        missing_indices, dtype=torch.long, device=self.device,
                    )
                    encoded = self.model.static_encoder(
                        batch["per_uop_fields"].index_select(0, index)
                    )
                    for local, row in enumerate(missing_indices):
                        core_id, key = missing_keys[local]
                        if core_id in self._static_by_core:
                            self.static_evictions += 1
                        token = encoded[local].detach()
                        self._static_by_core[core_id] = (key, token)
                        cached[row] = token
                if any(value is None for value in cached):
                    raise RuntimeError("v29 static cache failed to populate every row")
                static_tokens = torch.stack([
                    value for value in cached if value is not None
                ], dim=0)
            if staged_gss:
                base_token, core = self.model.interaction(static_tokens, batch)
                provisional = self.model.provisional_timing_from_base(
                    base_token, batch["valid_uop_mask"],
                )
                provisional_commit = (
                    provisional["commit_time"].float().cpu().numpy()
                )
                assert gss_rollout is not None
                if not isinstance(context, MutableMapping):
                    raise RuntimeError("online GSS context must be mutable")
                gss_rollout.augment_context(
                    context,
                    predicted_commit_time=provisional_commit,
                    step_start_cycles=float(step_start_cycles),
                    deadline_lookup=deadline_lookup,
                )
                self._attach_gss_batch(batch, context)
                output = self.model.forward_from_base(
                    batch,
                    base_token,
                    core,
                    include_horizon_outputs=False,
                    provisional_raw_gap=provisional["raw_gap"],
                )
            elif gap0 and self.model.gss_adapter is not None:
                base_token, core = self.model.interaction(static_tokens, batch)
                output = self.model.forward_from_base(
                    batch,
                    base_token,
                    core,
                    include_horizon_outputs=include_horizon_outputs,
                    gss_ablation_mode="gap0",
                )
            else:
                output = self.model.forward_from_static(
                    batch,
                    static_tokens,
                    include_horizon_outputs=include_horizon_outputs,
                )
        if self._model_end_event is not None:
            self._model_end_event.record()
            self._model_end_event.synchronize()
            assert self._model_start_event is not None
            self.model_forward_seconds += (
                self._model_start_event.elapsed_time(self._model_end_event) / 1000.0
            )
        else:
            self.model_forward_seconds += time.perf_counter() - cpu_model_started
        transfer_started = time.perf_counter()
        retirement_gap = output["retirement_gap"].float().cpu().numpy()
        if "branch_miss_probability" in output:
            branch_probability = (
                output["branch_miss_probability"].float().cpu().numpy()
            )
        elif "branch_replay_event" in context:
            # B1--B3 remove the neural PMU head.  Deployment counts come from
            # the same deterministic replay sidecar used by B2/B3 timing.
            branch_probability = context[
                "branch_replay_event"
            ][..., 0].float().cpu().numpy()
        else:
            raise RuntimeError(
                "headless branch checkpoint requires configured replay sidecar "
                "for deployment branch PMU"
            )
        commit_probability = (
            output["commit_probability"].float().cpu().numpy()
            if include_horizon_outputs else None
        )
        progress = (
            output["progress"].float().cpu().numpy()
            if include_horizon_outputs else None
        )
        self.output_transfer_seconds += time.perf_counter() - transfer_started
        validation_started = time.perf_counter()
        valid = context["valid_uop_mask"].cpu().numpy().astype(bool, copy=False)
        if np.any(~np.isfinite(retirement_gap[valid])):
            raise RuntimeError("v29 timing head produced a non-finite retirement gap")
        if np.any(retirement_gap[valid] < 0.0):
            raise RuntimeError("v29 timing head produced a negative retirement gap")
        if include_horizon_outputs:
            # Preserve the checkpoint's one-step/oracle output contract.  The
            # canonical gap reconstruction below is a free-running contract.
            commit_time = output["commit_time"].float().cpu().numpy()
            canonical_gap = np.asarray(retirement_gap, dtype=np.float64)
        else:
            canonical_gap = np.zeros_like(retirement_gap, dtype=np.float64)
            valid_gap = np.asarray(retirement_gap[valid], dtype=np.float64)
            floor_mask = valid_gap < MIN_RETIREMENT_GAP_CYCLES
            self.retirement_gap_floor_count += int(np.count_nonzero(floor_mask))
            canonical_gap[valid] = np.maximum(
                valid_gap, MIN_RETIREMENT_GAP_CYCLES,
            )
            commit_time = np.cumsum(canonical_gap, axis=1, dtype=np.float64)
        if np.any(~np.isfinite(commit_time)):
            raise RuntimeError("v29 timing head produced a non-finite commit time")
        differences = np.diff(commit_time, axis=1)
        pair_valid = valid[:, 1:] & valid[:, :-1]
        violation = pair_valid & (differences < -1.0e-6)
        if np.any(violation):
            row, left = np.argwhere(violation)[0].tolist()
            right = left + 1
            gap = output["retirement_gap"].float().cpu().numpy()
            raise RuntimeError(
                "v29 timing head violated monotonicity: "
                f"row={row} pair={left}->{right} "
                f"tau={commit_time[row, left]:.9g}->{commit_time[row, right]:.9g} "
                f"diff={differences[row, left]:.9g} "
                f"predicted_gap_right={gap[row, right]:.9g} "
                f"row_max_tau={commit_time[row, valid[row]].max():.9g}"
            )
        self.prediction_validation_seconds += (
            time.perf_counter() - validation_started
        )
        self.predict_calls += 1
        self.free_fast_path_calls += int(not include_horizon_outputs)
        prediction = V29Prediction(
            commit_time=commit_time,
            commit_probability=commit_probability,
            progress=progress,
            branch_miss_probability=branch_probability,
            valid_uop_mask=valid,
            retirement_gap=canonical_gap,
        )
        self.predict_wall_seconds += time.perf_counter() - predict_started
        return prediction

    def predict(
        self, store: V29TraceStore, context: Mapping[str, Any],
    ) -> V29Prediction:
        """Return the complete training/one-step evaluation output contract."""
        return self._predict(store, context, include_horizon_outputs=True)

    def predict_free(
        self, store: V29TraceStore, context: Mapping[str, Any],
    ) -> V29Prediction:
        """Return only outputs consumed by the free-running scheduler."""
        return self._predict(store, context, include_horizon_outputs=False)

    def predict_free_gss(
        self,
        store: V29TraceStore,
        context: MutableMapping[str, Any],
        gss_rollout: GSSSerialRollout,
        *,
        step_start_cycles: float,
        deadline_lookup: Optional[Callable[[int, int], Optional[float]]] = None,
    ) -> V29Prediction:
        """Run one QKVR pass with a commit-clock GSS staging boundary."""
        return self._predict(
            store,
            context,
            include_horizon_outputs=False,
            gss_rollout=gss_rollout,
            step_start_cycles=step_start_cycles,
            deadline_lookup=deadline_lookup,
        )

    def stats(self) -> Dict[str, Any]:
        total = self.static_hits + self.static_misses
        peak = (
            int(torch.cuda.max_memory_allocated(self.device))
            if self.device.type == "cuda" else 0
        )
        return {
            "static_cache_hits": self.static_hits,
            "static_cache_misses": self.static_misses,
            "static_cache_evictions": self.static_evictions,
            "static_cache_hit_rate": self.static_hits / max(1, total),
            "gpu_peak_memory_bytes": peak,
            "predict_calls": self.predict_calls,
            "free_fast_path_calls": self.free_fast_path_calls,
            "retirement_gap_floor_count": self.retirement_gap_floor_count,
            "batch_transfer_seconds": self.batch_transfer_seconds,
            "model_forward_seconds": self.model_forward_seconds,
            "output_transfer_seconds": self.output_transfer_seconds,
            "prediction_validation_seconds": self.prediction_validation_seconds,
            "predict_wall_seconds": self.predict_wall_seconds,
        }


class V29ParallelModelRunner:
    """Replicate one checkpoint across devices for one-trace window parallelism."""

    def __init__(
        self,
        runners: Sequence[V29ModelRunner],
        *,
        context_backend: str = "thread",
    ) -> None:
        self.runners = list(runners)
        if not self.runners:
            raise ValueError("v29 parallel runner requires at least one device")
        first = self.runners[0]
        if any(
            runner.checkpoint_meta["checkpoint_id"]
            != first.checkpoint_meta["checkpoint_id"]
            for runner in self.runners[1:]
        ):
            raise ValueError("v29 parallel runners do not share one checkpoint")
        self.config = first.config
        self.checkpoint_meta = first.checkpoint_meta
        self.amp_dtype = first.amp_dtype
        self.device = tuple(runner.device for runner in self.runners)
        self.context_backend = str(context_backend).strip().lower()
        if self.context_backend not in {"serial", "thread", "process"}:
            raise ValueError(
                "parallel context backend must be serial, thread, or process"
            )
        self._executor = ThreadPoolExecutor(
            max_workers=len(self.runners),
            thread_name_prefix="v29-window-gpu",
        )
        self._context_process_executors: List[ProcessPoolExecutor] = []
        if self.context_backend == "process":
            spawn_context = mp.get_context("spawn")
            self._context_process_executors = [
                ProcessPoolExecutor(max_workers=1, mp_context=spawn_context)
                for _ in self.runners
            ]
        self._context_lane_stats: List[Mapping[str, Any]] = []
        self._context_epoch = 0

    @property
    def parallel_depth(self) -> int:
        return len(self.runners)

    def begin_trace(self, store: V29TraceStore) -> None:
        self._context_epoch += 1
        self._context_lane_stats = []
        for runner in self.runners:
            runner.begin_trace(store)

    def synchronize(self) -> None:
        for runner in self.runners:
            if runner.device.type == "cuda":
                torch.cuda.synchronize(runner.device)

    def set_gss_ablation_mode(self, mode: str) -> None:
        for runner in self.runners:
            runner.set_gss_ablation_mode(mode)

    def predict(
        self, store: V29TraceStore, context: Mapping[str, Any],
    ) -> V29Prediction:
        return self.runners[0].predict(store, context)

    def predict_free(
        self, store: V29TraceStore, context: Mapping[str, Any],
    ) -> V29Prediction:
        return self.runners[0].predict_free(store, context)

    def predict_free_gss(
        self,
        store: V29TraceStore,
        context: MutableMapping[str, Any],
        gss_rollout: GSSSerialRollout,
        *,
        step_start_cycles: float,
        deadline_lookup: Optional[Callable[[int, int], Optional[float]]] = None,
    ) -> V29Prediction:
        # Serial temporal rollout owns one device even when the runner was
        # instantiated with multiple devices for a different parallel mode.
        return self.runners[0].predict_free_gss(
            store,
            context,
            gss_rollout,
            step_start_cycles=step_start_cycles,
            deadline_lookup=deadline_lookup,
        )

    def predict_free_many(
        self,
        store: V29TraceStore,
        contexts: Sequence[Mapping[str, Any]],
    ) -> List[V29Prediction]:
        if len(contexts) > len(self.runners):
            raise ValueError(
                f"window count {len(contexts)} exceeds parallel depth "
                f"{len(self.runners)}"
            )
        futures = [
            self._executor.submit(runner.predict_free, store, context)
            for runner, context in zip(self.runners, contexts)
        ]
        return [future.result() for future in futures]

    def build_context_many(
        self,
        workspaces: Sequence[V29TraceStore],
        cursor_vectors: Sequence[Sequence[int]],
        *,
        state_time_cycles: float,
        last_commit_cycles: Mapping[int, float],
    ) -> List[Mapping[str, Any]]:
        if len(workspaces) != len(cursor_vectors):
            raise ValueError("parallel context workspace/request count mismatch")
        if len(workspaces) > len(self.runners):
            raise ValueError(
                f"context count {len(workspaces)} exceeds parallel depth "
                f"{len(self.runners)}"
            )
        if self.context_backend == "process":
            requests = [
                (
                    workspace.cache_dir,
                    not bool(getattr(workspace, "has_oracle_labels", True)),
                    self._context_epoch,
                    getattr(workspace, "long_history_dir", None),
                    getattr(workspace, "branch_replay_dir", None),
                    getattr(workspace, "gss_sidecar_dir", None),
                    getattr(workspace, "exposure_sidecar_dir", None),
                    bool(getattr(
                        workspace, "allow_ready_clock_gss_sidecar", False,
                    )),
                    tuple(int(value) for value in cursors),
                    float(state_time_cycles),
                    {
                        int(key): float(value)
                        for key, value in last_commit_cycles.items()
                    },
                )
                for workspace, cursors in zip(workspaces, cursor_vectors)
            ]
            futures = [
                executor.submit(_build_context_in_process, request)
                for executor, request in zip(
                    self._context_process_executors, requests,
                )
            ]
            rows = [future.result() for future in futures]
            self._context_lane_stats = [row[1] for row in rows]
            return [_restore_process_context(row[0]) for row in rows]
        if self.context_backend == "serial":
            contexts = [
                workspace.context_from_cursors(
                    tuple(int(value) for value in cursors),
                    state_time_cycles=float(state_time_cycles),
                    include_labels=False,
                    last_commit_cycles=dict(last_commit_cycles),
                )
                for workspace, cursors in zip(workspaces, cursor_vectors)
            ]
        else:
            futures = [
                self._executor.submit(
                    workspace.context_from_cursors,
                    tuple(int(value) for value in cursors),
                    state_time_cycles=float(state_time_cycles),
                    include_labels=False,
                    last_commit_cycles=dict(last_commit_cycles),
                )
                for workspace, cursors in zip(workspaces, cursor_vectors)
            ]
            contexts = [future.result() for future in futures]
        self._context_lane_stats = [
            workspace.runtime_stats() for workspace in workspaces
        ]
        return contexts

    def context_lane_stats(self) -> List[Mapping[str, Any]]:
        return list(self._context_lane_stats)

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        for executor in self._context_process_executors:
            executor.shutdown(wait=True)

    def stats(self) -> Dict[str, Any]:
        rows = [runner.stats() for runner in self.runners]
        hits = sum(int(row.get("static_cache_hits", 0)) for row in rows)
        misses = sum(int(row.get("static_cache_misses", 0)) for row in rows)
        summed_keys = (
            "static_cache_evictions",
            "predict_calls",
            "free_fast_path_calls",
            "retirement_gap_floor_count",
        )
        device_time_keys = (
            "batch_transfer_seconds",
            "model_forward_seconds",
            "output_transfer_seconds",
            "prediction_validation_seconds",
            "predict_wall_seconds",
        )
        report: Dict[str, Any] = {
            "static_cache_hits": hits,
            "static_cache_misses": misses,
            "static_cache_hit_rate": hits / max(1, hits + misses),
            "gpu_peak_memory_bytes": max(
                (int(row.get("gpu_peak_memory_bytes", 0)) for row in rows),
                default=0,
            ),
            "gpu_peak_memory_bytes_sum": sum(
                int(row.get("gpu_peak_memory_bytes", 0)) for row in rows
            ),
            "parallel_devices": [str(runner.device) for runner in self.runners],
            "parallel_depth": len(self.runners),
            "window_context_backend": self.context_backend,
        }
        for key in summed_keys:
            report[key] = sum(int(row.get(key, 0)) for row in rows)
        for key in device_time_keys:
            values = [float(row.get(key, 0.0)) for row in rows]
            report[key] = max(values, default=0.0)
            report[key + "_device_sum"] = sum(values)
        return report


def load_checkpoint_runner(
    checkpoint_path: str,
    *,
    device: str = "cuda",
    amp_dtype: Optional[str] = None,
    sdpa_backend: Optional[str] = None,
    static_cache: bool = True,
) -> V29ModelRunner:
    if not os.path.isfile(checkpoint_path):
        raise FileNotFoundError(checkpoint_path)
    payload = _torch_load(checkpoint_path, "cpu")
    if not isinstance(payload, Mapping) or "model" not in payload:
        raise RuntimeError("not a full v29 checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("checkpoint schema is not v29")
    contract = payload.get("contract")
    config_data = payload.get("config")
    if not isinstance(contract, Mapping) or not isinstance(config_data, Mapping):
        raise RuntimeError("v29 checkpoint lacks contract or embedded config")
    config = _config_from_mapping(config_data)
    model_config = dict(config.model)
    if sdpa_backend:
        model_config["sdpa_backend"] = str(sdpa_backend)
        config.model = model_config
    model = build_model(model_config, contract["horizons"])
    model.load_state_dict(payload["model"], strict=True)
    stat = os.stat(checkpoint_path)
    checkpoint_id = hashlib.sha256(
        (
            f"{os.path.abspath(checkpoint_path)}:{stat.st_size}:"
            f"{stat.st_mtime_ns}"
        ).encode("utf-8")
    ).hexdigest()
    metadata = {
        "checkpoint": os.path.abspath(checkpoint_path),
        "checkpoint_id": checkpoint_id,
        "step": int(payload.get("step", 0)),
        "best_validation": float(payload.get("best_validation", float("nan"))),
        "contract": dict(contract),
        "sdpa_backend": str(model_config.get("sdpa_backend", "auto")),
    }
    del payload
    return V29ModelRunner(
        model,
        config,
        metadata,
        device=device,
        amp_dtype=(amp_dtype or str(config.train.get("amp_dtype", "bf16"))),
        static_cache=static_cache,
    )


def load_checkpoint_parallel_runner(
    checkpoint_path: str,
    *,
    devices: Sequence[str],
    amp_dtype: Optional[str] = None,
    sdpa_backend: Optional[str] = None,
    static_cache: bool = True,
    context_backend: str = "process",
) -> V29ParallelModelRunner:
    """Load one CPU checkpoint payload and materialize one model per device."""
    device_names = [str(value) for value in devices]
    if not device_names:
        raise ValueError("parallel checkpoint runner requires devices")
    if not os.path.isfile(checkpoint_path):
        raise FileNotFoundError(checkpoint_path)
    payload = _torch_load(checkpoint_path, "cpu")
    if not isinstance(payload, Mapping) or "model" not in payload:
        raise RuntimeError("not a full v29 checkpoint")
    if payload.get("checkpoint_schema") != CHECKPOINT_SCHEMA_VERSION:
        raise RuntimeError("checkpoint schema is not v29")
    contract = payload.get("contract")
    config_data = payload.get("config")
    if not isinstance(contract, Mapping) or not isinstance(config_data, Mapping):
        raise RuntimeError("v29 checkpoint lacks contract or embedded config")
    config = _config_from_mapping(config_data)
    model_config = dict(config.model)
    if sdpa_backend:
        model_config["sdpa_backend"] = str(sdpa_backend)
        config.model = model_config
    stat = os.stat(checkpoint_path)
    checkpoint_id = hashlib.sha256(
        (
            f"{os.path.abspath(checkpoint_path)}:{stat.st_size}:"
            f"{stat.st_mtime_ns}"
        ).encode("utf-8")
    ).hexdigest()
    metadata = {
        "checkpoint": os.path.abspath(checkpoint_path),
        "checkpoint_id": checkpoint_id,
        "step": int(payload.get("step", 0)),
        "best_validation": float(payload.get("best_validation", float("nan"))),
        "contract": dict(contract),
        "sdpa_backend": str(model_config.get("sdpa_backend", "auto")),
    }
    runners = []
    for device_name in device_names:
        model = build_model(model_config, contract["horizons"])
        model.load_state_dict(payload["model"], strict=True)
        runners.append(V29ModelRunner(
            model,
            config,
            metadata,
            device=device_name,
            amp_dtype=(amp_dtype or str(config.train.get("amp_dtype", "bf16"))),
            static_cache=static_cache,
        ))
    del payload
    return V29ParallelModelRunner(
        runners,
        context_backend=context_backend,
    )


def replay_branch_baseline(store: V29TraceStore, entries: int = 4096) -> Dict[str, Any]:
    """Replay a deterministic functional direction-only gshare baseline.

    This is intentionally not presented as a clone of the configured gem5
    predictor.  It consumes only retired functional control-UOP outcomes and
    makes branch-head regressions visible independently of the learned timing
    head.  Exact targets are not stored in the packed cache, so this baseline
    must not infer them from the next architectural macro: microcoded control
    UOPs can branch inside one macro.
    """
    table_entries = 1
    while table_entries < max(16, int(entries)):
        table_entries <<= 1
    mask = table_entries - 1
    true_misses = 0
    branches = 0
    direction_misses = 0
    per_core = []
    for core_id in store.core_ids:
        arrays = store.cores[core_id]
        fields = arrays["fields"]
        branch_indices = np.flatnonzero(np.asarray(arrays["branch"], dtype=np.uint8))
        branch_labels = np.asarray(arrays["branch_miss"], dtype=np.uint8)
        macro_pc = np.asarray(arrays["macro_pc"], dtype=np.uint64)
        counters = np.ones(table_entries, dtype=np.uint8)
        history = 0
        history_mask = (1 << min(16, int(math.log2(table_entries)))) - 1
        core_direction = 0
        for index in branch_indices:
            index = int(index)
            pc = int(macro_pc[index])
            kind = int(fields[index, FIELD_INDEX["branch_kind"]])
            conditional = bool(kind & 0x2)
            taken = int(fields[index, FIELD_INDEX["branch_taken"]]) == 2
            if conditional:
                predictor_index = ((pc >> 2) ^ history) & mask
                predicted_taken = int(counters[predictor_index]) >= 2
                direction_miss = predicted_taken != taken
                if taken:
                    counters[predictor_index] = min(
                        3, int(counters[predictor_index]) + 1,
                    )
                else:
                    counters[predictor_index] = max(
                        0, int(counters[predictor_index]) - 1,
                    )
                history = ((history << 1) | int(taken)) & history_mask
            else:
                predicted_taken = True
                direction_miss = bool(taken != predicted_taken)
            core_direction += int(direction_miss)
        core_branches = int(len(branch_indices))
        core_true = int(branch_labels[branch_indices].sum())
        branches += core_branches
        true_misses += core_true
        direction_misses += core_direction
        per_core.append({
            "core_id": int(core_id),
            "branches": core_branches,
            "true_misses": core_true,
            "direction_only_misses": core_direction,
            "predicted_misses": core_direction,
            "predicted_rate": _event_rate(core_direction, core_branches),
        })
    return {
        "name": "functional_gshare_direction_only_replay",
        "table_entries": table_entries,
        "branches": branches,
        "true_misses": true_misses,
        "true_rate": _event_rate(true_misses, branches),
        "direction_only_misses": direction_misses,
        "direction_only_rate": _event_rate(direction_misses, branches),
        "predicted_misses": direction_misses,
        "predicted_rate": _event_rate(direction_misses, branches),
        "target_component_available": False,
        "target_component_reason": (
            "packed cache intentionally omits exact branch targets; the next "
            "architectural macro is not a valid target for microcoded branches"
        ),
        "per_core": per_core,
        "oracle_labels_consumed_as_input": False,
    }


def replay_configured_branch_predictor(store: V29TraceStore) -> Dict[str, Any]:
    """Replay the configured full Tournament BPU from exact functional facts.

    Miss labels are joined only after replay has completed, for evaluation.
    They are never visible to the replay state transition.
    """
    compact_replay = bool(getattr(store, "has_branch_replay_inputs", False))
    sidecar_replay = bool(
        getattr(store, "branch_feature_contract", None) is not None
    )
    if not compact_replay and not sidecar_replay:
        return {
            "name": "standalone_tournament_full_bpu_replay",
            "status": "unavailable",
            "reason": (
                "cache predates functional-branch-replay-v1 compact exact "
                "target arrays; rebuild it from current aligned functional trace"
            ),
            "target_component_available": False,
            "oracle_labels_consumed_as_input": False,
        }
    if compact_replay:
        config = ReplayConfig.from_mapping(store.meta)
        streams = [
            (int(core_id), events_from_cache_arrays(store.cores[core_id]))
            for core_id in store.core_ids
        ]
        report = replay_core_streams(streams, config)
        report["source"] = "online-functional-replay"
    else:
        per_core_reports = []
        for core_id in store.core_ids:
            sidecar = store.branch_feature_arrays[int(core_id)]
            event = np.asarray(sidecar["event"], dtype=np.uint8)
            indices = np.asarray(sidecar["index"], dtype=np.int64)
            fields = store.cores[int(core_id)]["fields"]
            conditional = int(np.count_nonzero(
                np.asarray(
                    fields[indices, FIELD_INDEX["branch_kind"]],
                    dtype=np.uint8,
                ) & 0x2
            ))
            direction_misses = int(event[:, 1].sum(dtype=np.int64))
            target_misses = int(event[:, 2].sum(dtype=np.int64))
            full_misses = int(event[:, 0].sum(dtype=np.int64))
            branches = int(len(indices))
            per_core_reports.append({
                "core_id": int(core_id),
                "branches": branches,
                "conditional_branches": conditional,
                "conditional_direction_misses": direction_misses,
                "conditional_direction_miss_rate": _event_rate(
                    direction_misses, conditional,
                ),
                "target_misses": target_misses,
                "full_misses": full_misses,
                "full_miss_rate": _event_rate(full_misses, branches),
                "cold_prefix_branches": int(event[:, 3].sum(dtype=np.int64)),
            })
        branches = sum(int(item["branches"]) for item in per_core_reports)
        conditional = sum(
            int(item["conditional_branches"]) for item in per_core_reports
        )
        direction_misses = sum(
            int(item["conditional_direction_misses"])
            for item in per_core_reports
        )
        target_misses = sum(
            int(item["target_misses"]) for item in per_core_reports
        )
        full_misses = sum(
            int(item["full_misses"]) for item in per_core_reports
        )
        report = {
            "name": "materialized_configured_branch_replay",
            "source": "v30-branch-replay-sidecar",
            "predictor_config_hash": store.branch_feature_contract[
                "predictor_config_hash"
            ],
            "branches": branches,
            "conditional_branches": conditional,
            "conditional_direction_misses": direction_misses,
            "conditional_direction_miss_rate": _event_rate(
                direction_misses, conditional,
            ),
            "target_misses": target_misses,
            "full_misses": full_misses,
            "full_miss_rate": _event_rate(full_misses, branches),
            "cold_prefix_branches": sum(
                int(item["cold_prefix_branches"])
                for item in per_core_reports
            ),
            "per_core": per_core_reports,
            "functional_history_mismatches": 0,
        }
    report["status"] = "ok"
    report["target_component_available"] = True
    if not bool(getattr(store, "has_oracle_labels", False)):
        return report

    true_misses = 0
    true_branches = 0
    per_core = {int(item["core_id"]): item for item in report["per_core"]}
    for core_id in store.core_ids:
        arrays = store.cores[core_id]
        indices = np.asarray(
            arrays["replay_branch_index"]
            if compact_replay
            else store.branch_feature_arrays[int(core_id)]["index"],
            dtype=np.int64,
        )
        labels = np.asarray(arrays["branch_miss"], dtype=np.uint8)
        core_true = int(labels[indices].sum())
        core_branches = int(len(indices))
        item = per_core[int(core_id)]
        item["true_misses"] = core_true
        item["true_rate"] = _event_rate(core_true, core_branches)
        item["miss_count_abs_error"] = abs(int(item["full_misses"]) - core_true)
        item["miss_count_abs_relative_error"] = (
            item["miss_count_abs_error"] / max(1, core_true)
        )
        item["miss_rate_abs_error_pp"] = abs(
            float(item["full_miss_rate"]) - float(item["true_rate"])
        ) * 100.0
        true_misses += core_true
        true_branches += core_branches
    true_rate = _event_rate(true_misses, true_branches)
    report.update({
        "true_misses": true_misses,
        "true_rate": true_rate,
        "predicted_misses": int(report["full_misses"]),
        "predicted_rate": float(report["full_miss_rate"]),
        "miss_count_abs_error": abs(int(report["full_misses"]) - true_misses),
        "miss_count_abs_relative_error": (
            abs(int(report["full_misses"]) - true_misses) / max(1, true_misses)
        ),
        "miss_rate_abs_error_pp": abs(
            float(report["full_miss_rate"]) - true_rate
        ) * 100.0,
        "oracle_labels_consumed_as_input": False,
        "oracle_labels_used_post_replay_for_evaluation": True,
    })
    return report


def _synchronize(engine: Any) -> None:
    synchronize = getattr(engine, "synchronize", None)
    if callable(synchronize):
        synchronize()
        return
    device = getattr(engine, "device", None)
    if isinstance(device, torch.device) and device.type == "cuda":
        torch.cuda.synchronize(device)


def _engine_begin(engine: Any, store: V29TraceStore) -> None:
    begin = getattr(engine, "begin_trace", None)
    if callable(begin):
        begin(store)


def _engine_predict_free(
    engine: Any,
    store: V29TraceStore,
    context: MutableMapping[str, Any],
    *,
    gss_rollout: Optional[GSSSerialRollout] = None,
    step_start_cycles: float = 0.0,
    deadline_lookup: Optional[Callable[[int, int], Optional[float]]] = None,
) -> V29Prediction:
    if gss_rollout is not None:
        staged = getattr(engine, "predict_free_gss", None)
        if not callable(staged):
            raise RuntimeError(
                "GSS free rollout requires the single-QKVR staged inference API"
            )
        return staged(
            store,
            context,
            gss_rollout,
            step_start_cycles=float(step_start_cycles),
            deadline_lookup=deadline_lookup,
        )
    predict_free = getattr(engine, "predict_free", None)
    if callable(predict_free):
        return predict_free(store, context)
    return engine.predict(store, context)


def _engine_predict_free_many(
    engine: Any,
    store: V29TraceStore,
    contexts: Sequence[Mapping[str, Any]],
) -> List[V29Prediction]:
    predict_many = getattr(engine, "predict_free_many", None)
    if callable(predict_many):
        return list(predict_many(store, contexts))
    return [
        _engine_predict_free(engine, store, context) for context in contexts
    ]


def _engine_stats(engine: Any) -> Dict[str, Any]:
    function = getattr(engine, "stats", None)
    return dict(function()) if callable(function) else {
        "static_cache_hits": 0,
        "static_cache_misses": 0,
        "static_cache_evictions": 0,
        "static_cache_hit_rate": 0.0,
        "gpu_peak_memory_bytes": 0,
    }


def _engine_gss_contract(engine: Any) -> Optional[Dict[str, Any]]:
    metadata = getattr(engine, "checkpoint_meta", None)
    if not isinstance(metadata, Mapping):
        runners = getattr(engine, "runners", None)
        if runners:
            metadata = getattr(runners[0], "checkpoint_meta", None)
    if not isinstance(metadata, Mapping):
        return None
    contract = metadata.get("contract")
    if not isinstance(contract, Mapping):
        return None
    gss = contract.get("gss")
    return dict(gss) if isinstance(gss, Mapping) else None


def _store_begin(store: Any) -> None:
    reset = getattr(store, "reset_runtime_stats", None)
    if callable(reset):
        reset(clear_cache=True)


def _store_stats(store: Any) -> Dict[str, Any]:
    function = getattr(store, "runtime_stats", None)
    return dict(function()) if callable(function) else {
        "context_builder": "legacy-python",
        "cpu_window_cache_policy": "none",
        "cpu_window_cache_hits": 0,
        "cpu_window_cache_misses": 0,
        "cpu_window_cache_hit_rate": 0.0,
        "cpu_window_cache_entries": 0,
        "context_calls": 0,
        "context_phase_seconds": {
            name: 0.0 for name in CONTEXT_PHASE_NAMES
        },
    }


def _context_phase_report(
    store_stats: Mapping[str, Any], external_seconds: float,
) -> Dict[str, float]:
    raw = store_stats.get("context_phase_seconds", {})
    raw = raw if isinstance(raw, Mapping) else {}
    phases = {
        name: max(0.0, float(raw.get(name, 0.0)))
        for name in CONTEXT_PHASE_NAMES
    }
    # The outer timer includes Python dispatch/return and instrumentation that
    # sits just outside V29TraceStore.context_from_cursors().  Preserve it as a
    # separate reconciliation bucket so the displayed phases sum to the
    # existing context_build_seconds contract.
    phases["call_overhead"] = max(
        0.0, float(external_seconds) - sum(phases.values()),
    )
    return phases


def _free_timing_breakdown(
    store_stats: Mapping[str, Any],
    *,
    elapsed: float,
    context_build_seconds: float,
    predict_seconds: float,
    scheduler_seconds: float,
    oracle_drift_seconds: float,
    progress_seconds: float,
) -> Dict[str, Any]:
    measured_seconds = (
        context_build_seconds
        + predict_seconds
        + scheduler_seconds
        + oracle_drift_seconds
        + progress_seconds
    )
    return {
        "context_build_seconds": context_build_seconds,
        "context_phase_seconds": _context_phase_report(
            store_stats, context_build_seconds,
        ),
        "predict_seconds": predict_seconds,
        "scheduler_seconds": scheduler_seconds,
        "oracle_drift_seconds": oracle_drift_seconds,
        "progress_logging_seconds": progress_seconds,
        "unattributed_seconds": max(0.0, elapsed - measured_seconds),
    }


def _prediction_gaps(prediction: V29Prediction, row: int, count: int) -> np.ndarray:
    if prediction.retirement_gap is not None:
        gaps = np.asarray(
            prediction.retirement_gap[row, :count], dtype=np.float64,
        )
        if np.any(~np.isfinite(gaps)) or np.any(gaps < 0.0):
            raise RuntimeError(
                "parallel v29 window produced invalid direct retirement gaps"
            )
        return gaps
    tau = np.asarray(prediction.commit_time[row, :count], dtype=np.float64)
    if count <= 0:
        return np.zeros(0, dtype=np.float64)
    gaps = np.diff(np.concatenate(([0.0], tau)))
    if np.any(~np.isfinite(gaps)) or np.any(gaps < -1.0e-6):
        raise RuntimeError("parallel v29 window produced invalid retirement gaps")
    return np.maximum(gaps, 0.0)


def _build_parallel_wave(
    store: V29TraceStore,
    engine: Any,
    *,
    context_pool: Optional[V29ParallelContextPool],
    anchor_cursors: Sequence[int],
    global_time: float,
    last_commit_cycles: Mapping[int, float],
    depth: int,
    shift: int,
    mode: str,
    gss_rollout: Optional[GSSSerialRollout] = None,
    deadline_lookup: Optional[
        Callable[[int, int], Optional[float]]
    ] = None,
) -> Tuple[V29ParallelWave, float, float]:
    context_started = time.perf_counter()
    starts_by_depth: List[Tuple[int, ...]] = []
    for window_depth in range(max(1, int(depth))):
        starts = tuple(
            min(
                int(store.core_meta[core_id]["n_uops"]),
                int(anchor_cursors[slot]) + window_depth * int(shift),
            )
            for slot, core_id in enumerate(store.core_ids)
        )
        if all(
            starts[slot] >= int(store.core_meta[core_id]["n_uops"])
            for slot, core_id in enumerate(store.core_ids)
        ):
            break
        starts_by_depth.append(starts)
    if not starts_by_depth:
        raise RuntimeError("parallel v29 wave has no active context")
    build_context_many = getattr(engine, "build_context_many", None)
    if context_pool is not None and callable(build_context_many):
        contexts = list(build_context_many(
            context_pool.workspaces[:len(starts_by_depth)],
            starts_by_depth,
            state_time_cycles=float(global_time),
            last_commit_cycles=last_commit_cycles,
        ))
    else:
        contexts = [
            store.context_from_cursors(
                starts,
                state_time_cycles=float(global_time),
                include_labels=False,
                last_commit_cycles=last_commit_cycles,
            )
            for starts in starts_by_depth
        ]
    if len(contexts) != len(starts_by_depth):
        raise RuntimeError("parallel v29 context builder returned wrong window count")
    if gss_rollout is not None:
        gss_rollout.augment_contexts(
            contexts,
            anchor_cursors=anchor_cursors,
            deadline_lookup=deadline_lookup,
        )
    if context_pool is not None:
        context_lane_stats = getattr(engine, "context_lane_stats", None)
        if callable(context_lane_stats):
            context_pool.update_lane_stats(context_lane_stats())
    for context in contexts:
        leaked = [key for key in ORACLE_ONLY_KEYS if key in context]
        if leaked:
            raise RuntimeError(
                f"parallel free-running v29 context leaked oracle keys: {leaked}"
            )
    context_seconds = time.perf_counter() - context_started
    if context_pool is not None:
        context_pool.add_wall_seconds(context_seconds)
    predict_started = time.perf_counter()
    predictions = _engine_predict_free_many(engine, store, contexts)
    predict_seconds = time.perf_counter() - predict_started
    if len(predictions) != len(contexts):
        raise RuntimeError("parallel v29 engine returned the wrong window count")
    windows = [
        V29ParallelWindow(
            depth=window_depth,
            start_cursors=starts,
            context=context,
            prediction=prediction,
        )
        for window_depth, (starts, context, prediction) in enumerate(zip(
            starts_by_depth, contexts, predictions,
        ))
    ]
    wave = V29ParallelWave(
        anchor_cursors=tuple(int(value) for value in anchor_cursors),
        windows=windows,
    )
    if mode == "unconditional":
        _populate_unconditional_lattice(store, wave, shift=int(shift))
    return wave, context_seconds, predict_seconds


def _populate_unconditional_lattice(
    store: V29TraceStore,
    wave: V29ParallelWave,
    *,
    shift: int,
) -> None:
    if not wave.windows:
        raise RuntimeError("cannot build an unconditional lattice without windows")
    horizon = (len(wave.windows) - 1) * int(shift) + int(store.K)
    core_count = len(store.core_ids)
    gaps = np.zeros((core_count, horizon), dtype=np.float64)
    branch_probability = np.zeros((core_count, horizon), dtype=np.float64)
    valid = np.zeros((core_count, horizon), dtype=np.bool_)
    for window_index, window in enumerate(wave.windows):
        slots = [int(value) for value in window.context["core_slots"].tolist()]
        row_by_slot = {slot: row for row, slot in enumerate(slots)}
        ownership = int(store.K) if window_index == len(wave.windows) - 1 else int(shift)
        output_start = window_index * int(shift)
        for slot, row in row_by_slot.items():
            valid_count = int(window.prediction.valid_uop_mask[row].sum())
            take = min(ownership, valid_count)
            if take <= 0:
                continue
            output_end = output_start + take
            gaps[slot, output_start:output_end] = _prediction_gaps(
                window.prediction, row, valid_count,
            )[:take]
            branch_probability[slot, output_start:output_end] = np.asarray(
                window.prediction.branch_miss_probability[row, :take],
                dtype=np.float64,
            )
            valid[slot, output_start:output_end] = True
    valid_count = np.zeros(core_count, dtype=np.int64)
    for slot in range(core_count):
        false = np.flatnonzero(~valid[slot])
        count = int(false[0]) if len(false) else int(horizon)
        if np.any(valid[slot, count:]):
            raise RuntimeError("unconditional v29 lattice contains an ownership gap")
        valid_count[slot] = count
    wave.unconditional_gap = gaps
    wave.unconditional_branch_probability = branch_probability
    wave.unconditional_valid_count = valid_count


def _unconditional_wave_step(
    store: V29TraceStore,
    wave: V29ParallelWave,
    cursors: Sequence[int],
    *,
    target_stride: int,
) -> Optional[V29WindowStep]:
    gaps = wave.unconditional_gap
    branch_probability = wave.unconditional_branch_probability
    valid_count = wave.unconditional_valid_count
    if gaps is None or branch_probability is None or valid_count is None:
        raise RuntimeError("unconditional v29 wave has no gap lattice")
    slots = [
        slot for slot, core_id in enumerate(store.core_ids)
        if int(cursors[slot]) < int(store.core_meta[core_id]["n_uops"])
    ]
    tau = np.zeros((len(slots), store.K), dtype=np.float64)
    branch = np.zeros((len(slots), store.K), dtype=np.float64)
    valid = np.zeros((len(slots), store.K), dtype=np.bool_)
    for row, slot in enumerate(slots):
        relative = int(cursors[slot]) - int(wave.anchor_cursors[slot])
        remaining = int(valid_count[slot]) - relative
        trace_remaining = (
            int(store.core_meta[store.core_ids[slot]]["n_uops"])
            - int(cursors[slot])
        )
        if remaining <= 0:
            return None
        take = min(int(store.K), remaining, trace_remaining)
        chosen = gaps[slot, relative:relative + take]
        tau[row, :take] = np.cumsum(chosen, dtype=np.float64)
        branch[row, :take] = branch_probability[
            slot, relative:relative + take
        ]
        valid[row, :take] = True
    return V29WindowStep(
        slots=slots,
        commit_time=tau,
        branch_miss_probability=branch,
        valid_uop_mask=valid,
    )


def _speculative_window_step(
    store: V29TraceStore,
    window: V29ParallelWindow,
    cursors: Sequence[int],
    *,
    target_stride: int,
) -> Tuple[Optional[V29WindowStep], str]:
    active_slots = [
        slot for slot, core_id in enumerate(store.core_ids)
        if int(cursors[slot]) < int(store.core_meta[core_id]["n_uops"])
    ]
    window_slots = [
        int(value) for value in window.context["core_slots"].tolist()
    ]
    row_by_slot = {slot: row for row, slot in enumerate(window_slots)}
    aligned: List[Tuple[int, int, int, int, int]] = []
    for slot in active_slots:
        offset = int(cursors[slot]) - int(window.start_cursors[slot])
        if offset < 0:
            return None, "start_not_covered"
        row = row_by_slot.get(slot)
        if row is None:
            return None, "active_core_missing"
        valid_count = int(window.prediction.valid_uop_mask[row].sum())
        if offset >= valid_count:
            return None, "window_exhausted"
        remaining = valid_count - offset
        trace_remaining = (
            int(store.core_meta[store.core_ids[slot]]["n_uops"])
            - int(cursors[slot])
        )
        aligned.append((
            slot, row, offset,
            min(store.K, remaining, trace_remaining), valid_count,
        ))
    tau = np.zeros((len(aligned), store.K), dtype=np.float64)
    branch = np.zeros((len(aligned), store.K), dtype=np.float64)
    valid = np.zeros((len(aligned), store.K), dtype=np.bool_)
    for output_row, (
        _slot, input_row, offset, take, input_valid_count,
    ) in enumerate(aligned):
        rebased = np.cumsum(
            _prediction_gaps(
                window.prediction, input_row, input_valid_count,
            )[offset:offset + take],
            dtype=np.float64,
        )
        if np.any(~np.isfinite(rebased)) or np.any(rebased <= 0):
            return None, "invalid_rebased_time"
        tau[output_row, :take] = rebased
        branch[output_row, :take] = np.asarray(
            window.prediction.branch_miss_probability[
                input_row, offset:offset + take
            ],
            dtype=np.float64,
        )
        valid[output_row, :take] = True
    return V29WindowStep(
        slots=[item[0] for item in aligned],
        commit_time=tau,
        branch_miss_probability=branch,
        valid_uop_mask=valid,
    ), ""


def _uniform_indices(length: int, maximum: int) -> List[int]:
    if maximum <= 0 or length <= maximum:
        return list(range(length))
    return sorted({
        int(round(index * (length - 1) / max(1, maximum - 1)))
        for index in range(maximum)
    })


def evaluate_oracle_one_step(
    store: V29TraceStore,
    engine: Any,
    *,
    max_samples: int = 0,
    progress: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Evaluate predictions on true common-time contexts without rollout state."""
    _store_begin(store)
    _engine_begin(engine, store)
    sample_indices = _uniform_indices(len(store), int(max_samples))
    commit_log_error = ScalarErrors(seed=31)
    commit_cycle_error = ScalarErrors(seed=37)
    horizon_progress = {
        float(horizon): ScalarErrors(seed=41 + index)
        for index, horizon in enumerate(store.horizons)
    }
    branch_count = {
        float(horizon): ScalarErrors(seed=61 + index)
        for index, horizon in enumerate(store.horizons)
    }
    true_zero = {float(horizon): 0 for horizon in store.horizons}
    true_full = {float(horizon): 0 for horizon in store.horizons}
    predicted_zero = {float(horizon): 0 for horizon in store.horizons}
    predicted_full = {float(horizon): 0 for horizon in store.horizons}
    horizon_rows = {float(horizon): 0 for horizon in store.horizons}
    branch_histogram = BinaryHistogram()
    predicted_horizon_misses = {float(horizon): 0.0 for horizon in store.horizons}
    true_horizon_misses = {float(horizon): 0.0 for horizon in store.horizons}
    true_horizon_opportunities = {float(horizon): 0 for horizon in store.horizons}
    monotonic_token = 0
    monotonic_horizon = 0
    rows_seen = 0
    tokens_seen = 0
    _synchronize(engine)
    started = time.perf_counter()
    for ordinal, sample_index in enumerate(sample_indices):
        context = store.context_at(sample_index)
        prediction = engine.predict(store, context)
        if prediction.commit_probability is None or prediction.progress is None:
            raise RuntimeError(
                "oracle one-step evaluation requires v29 horizon outputs"
            )
        valid = prediction.valid_uop_mask
        target_time = context["commit_time_target"].numpy()
        target_prefix = context["prefix_target"].numpy()
        target_progress = context["progress_target"].numpy()
        rows_seen += int(valid.shape[0])
        tokens_seen += int(valid.sum())
        signed_log = np.log1p(prediction.commit_time[valid]) - np.log1p(
            target_time[valid]
        )
        signed_cycles = prediction.commit_time[valid] - target_time[valid]
        commit_log_error.add(signed_log.tolist())
        commit_cycle_error.add(signed_cycles.tolist())
        pair_valid = valid[:, 1:] & valid[:, :-1]
        monotonic_token += int(np.count_nonzero(
            np.diff(prediction.commit_probability, axis=1)[pair_valid] > 1.0e-6
        ))
        monotonic_horizon += int(np.count_nonzero(
            np.diff(prediction.commit_probability, axis=2) < -1.0e-6
        ))
        branch_mask = context["branch_mask"].numpy().astype(bool) & valid
        branch_labels = context["branch_miss_target"].numpy()
        branch_histogram.add(
            prediction.branch_miss_probability[branch_mask],
            branch_labels[branch_mask],
        )
        for horizon_index, horizon_value in enumerate(store.horizons):
            horizon = float(horizon_value)
            signed_progress = (
                prediction.progress[:, horizon_index]
                - target_progress[:, horizon_index]
            )
            horizon_progress[horizon].add(signed_progress.tolist())
            hard_progress = np.sum(
                (prediction.commit_time <= horizon) & valid, axis=1,
            )
            valid_count = valid.sum(axis=1)
            truth = target_progress[:, horizon_index]
            horizon_rows[horizon] += int(len(truth))
            true_zero[horizon] += int(np.count_nonzero(truth == 0))
            true_full[horizon] += int(np.count_nonzero(truth == valid_count))
            predicted_zero[horizon] += int(np.count_nonzero(hard_progress == 0))
            predicted_full[horizon] += int(np.count_nonzero(hard_progress == valid_count))
            soft_gate = prediction.commit_probability[:, :, horizon_index]
            predicted_count = np.sum(
                soft_gate * branch_mask * prediction.branch_miss_probability,
                axis=1,
            )
            true_count = np.sum(
                target_prefix[:, :, horizon_index]
                * branch_mask * branch_labels,
                axis=1,
            )
            opportunities = np.sum(
                target_prefix[:, :, horizon_index] * branch_mask, axis=1,
            )
            branch_count[horizon].add((predicted_count - true_count).tolist())
            predicted_horizon_misses[horizon] += float(predicted_count.sum())
            true_horizon_misses[horizon] += float(true_count.sum())
            true_horizon_opportunities[horizon] += int(opportunities.sum())
        if progress is not None:
            progress({
                "phase": "oracle_one_step",
                "sample": ordinal + 1,
                "samples": len(sample_indices),
                "rows": rows_seen,
                "tokens": tokens_seen,
            })
    _synchronize(engine)
    elapsed = time.perf_counter() - started
    horizon_report = {}
    for horizon_value in store.horizons:
        horizon = float(horizon_value)
        rows = max(1, horizon_rows[horizon])
        opportunities = true_horizon_opportunities[horizon]
        predicted_rate = _event_rate(predicted_horizon_misses[horizon], opportunities)
        true_rate = _event_rate(true_horizon_misses[horizon], opportunities)
        horizon_report[str(horizon)] = {
            "progress": horizon_progress[horizon].summary(),
            "branch_count": branch_count[horizon].summary(),
            "true_zero_fraction": true_zero[horizon] / rows,
            "true_full_fraction": true_full[horizon] / rows,
            "predicted_zero_fraction": predicted_zero[horizon] / rows,
            "predicted_full_fraction": predicted_full[horizon] / rows,
            "branch_opportunities": opportunities,
            "predicted_branch_misses": predicted_horizon_misses[horizon],
            "true_branch_misses": true_horizon_misses[horizon],
            "predicted_branch_rate": predicted_rate,
            "true_branch_rate": true_rate,
            "branch_rate_abs_error_pp": abs(predicted_rate - true_rate) * 100.0,
        }
    return {
        "mode": "oracle_one_step",
        "samples": len(sample_indices),
        "active_core_rows": rows_seen,
        "valid_uops": tokens_seen,
        "commit_time_log_error": commit_log_error.summary(),
        "commit_time_cycle_error": commit_cycle_error.summary(),
        "horizons": horizon_report,
        "branch_token": branch_histogram.summary(),
        "prefix_monotonic_token_violations": monotonic_token,
        "prefix_monotonic_horizon_violations": monotonic_horizon,
        "elapsed_s": elapsed,
        "core_rows_per_s": rows_seen / max(elapsed, 1.0e-12),
        "uops_per_s": tokens_seen / max(elapsed, 1.0e-12),
        **_store_stats(store),
        **_engine_stats(engine),
    }


def _functional_prediction_report(
    store: Any,
    engine: Any,
    *,
    context_stats_source: Any,
    source: Optional[Mapping[str, Any]],
    cursors: Sequence[int],
    active_cycles: Sequence[float],
    retired_uops: Sequence[int],
    retired_macros: Sequence[int],
    branch_opportunities: Sequence[int],
    predicted_branch_misses: Sequence[float],
    last_commit_cycles: Mapping[int, float],
    predicted_endpoints: Sequence[float],
    global_time: float,
    steps: int,
    model_forwards: int,
    complete: bool,
    elapsed: float,
    target_stride: int,
    min_step_cycles: float,
    max_step_cycles: float,
    advisory_min_step_violations: int,
    stride_overshoots: int,
    total_no_progress: int,
    context_build_seconds: float,
    predict_seconds: float,
    scheduler_seconds: float,
    oracle_drift_seconds: float,
    progress_seconds: float,
    window_parallel_report: Mapping[str, Any],
) -> Dict[str, Any]:
    per_core = []
    predicted_cycles = []
    functional_macros = 0
    functional_branches = 0
    for slot, core_id in enumerate(store.core_ids):
        cursor = int(cursors[slot])
        arrays = store.cores[core_id]
        macros = int(np.asarray(
            arrays["macro_end"][:cursor], dtype=np.int64,
        ).sum())
        branches = int(np.asarray(
            arrays["branch"][:cursor], dtype=np.int64,
        ).sum())
        endpoint = (
            float(predicted_endpoints[slot])
            if math.isfinite(float(predicted_endpoints[slot]))
            else float(last_commit_cycles[int(core_id)])
        )
        if complete and not math.isclose(
            float(active_cycles[slot]), endpoint, rel_tol=1.0e-7, abs_tol=1.0e-5,
        ):
            raise RuntimeError(
                f"functional v29 active-cycle integration mismatch core={core_id}: "
                f"{active_cycles[slot]} != {endpoint}"
            )
        predicted_cycles.append(endpoint)
        functional_macros += macros
        functional_branches += branches
        per_core.append({
            "core_id": int(core_id),
            "complete": cursor == int(store.core_meta[core_id]["n_uops"]),
            "retired_uops": int(retired_uops[slot]),
            "functional_uops": cursor,
            "full_functional_uops": int(store.core_meta[core_id]["n_uops"]),
            "retired_macros": int(retired_macros[slot]),
            "functional_macros": macros,
            "branch_opportunities": int(branch_opportunities[slot]),
            "functional_branches": branches,
            "predicted_cycles": endpoint,
            "integrated_active_cycles": float(active_cycles[slot]),
            "predicted_branch_misses": float(predicted_branch_misses[slot]),
            "predicted_branch_miss_rate": (
                _event_rate(float(predicted_branch_misses[slot]), branches)
            ),
        })
    total_uops = sum(int(value) for value in retired_uops)
    total_macros = sum(int(value) for value in retired_macros)
    total_branches = sum(int(value) for value in branch_opportunities)
    if total_uops != sum(int(value) for value in cursors):
        raise RuntimeError("functional v29 UOP accounting is not exact-once")
    if total_macros != functional_macros:
        raise RuntimeError("functional v29 macro accounting is not exact-once")
    if total_branches != functional_branches:
        raise RuntimeError("functional v29 branch accounting is not exact-once")
    cycle_sum = sum(predicted_cycles)
    branch_misses = sum(float(value) for value in predicted_branch_misses)
    source_row = dict(source or {})
    store_stats = _store_stats(context_stats_source)
    return {
        "mode": "single_global_time_functional_rollout",
        "trace_id": store.trace_id,
        "workload": str(source_row.get("workload", store.meta.get("workload", ""))),
        "seed": source_row.get("seed"),
        "n_cores": len(store.core_ids),
        "complete": bool(complete),
        "steps": int(steps),
        "global_time_cycles": float(global_time),
        "target_stride": int(target_stride),
        "min_step_cycles_advisory": float(min_step_cycles),
        "max_step_cycles": float(max_step_cycles),
        "advisory_min_step_violations": int(advisory_min_step_violations),
        "stride_overshoot_rows": int(stride_overshoots),
        "no_progress_steps": int(total_no_progress),
        "predicted_cycles_sum": cycle_sum,
        "integrated_active_cycles_sum": sum(float(value) for value in active_cycles),
        "retired_uops": total_uops,
        "retired_macros": total_macros,
        "branch_opportunities": total_branches,
        "predicted_micro_cpi": cycle_sum / max(1, total_uops),
        "predicted_macro_cpi": cycle_sum / max(1, total_macros),
        "predicted_makespan": max(predicted_cycles),
        "predicted_branch_misses": branch_misses,
        "predicted_branch_miss_rate": _event_rate(branch_misses, total_branches),
        "per_core": per_core,
        "elapsed_s": float(elapsed),
        "steps_per_s": steps / max(elapsed, 1.0e-12),
        "uops_per_s": total_uops / max(elapsed, 1.0e-12),
        "model_forwards": int(model_forwards),
        "retired_uops_per_model_forward": (
            total_uops / max(1, int(model_forwards))
        ),
        "timing_breakdown": _free_timing_breakdown(
            store_stats,
            elapsed=elapsed,
            context_build_seconds=context_build_seconds,
            predict_seconds=predict_seconds,
            scheduler_seconds=scheduler_seconds,
            oracle_drift_seconds=oracle_drift_seconds,
            progress_seconds=progress_seconds,
        ),
        "truth_available": False,
        "metric_scope": "full_functional_trace" if complete else "consumed_functional_prefix",
        "virtual_time_semantics": "one_shared_clock",
        "initialization_contract": "all_functional_core_streams_active_at_T0",
        "model_context_uses_oracle_timing": (
            gss_ablation_mode == "teacher-order"
        ),
        "oracle_timing_usage": (
            "teacher-ready-order-gss-input"
            if gss_ablation_mode == "teacher-order" else "none"
        ),
        "gss_ablation_mode": gss_ablation_mode,
        "predicted_context_used_as_training_label": False,
        "free_timing_reconstruction": FREE_TIMING_RECONSTRUCTION_CONTRACT,
        "min_retirement_gap_cycles": MIN_RETIREMENT_GAP_CYCLES,
        **dict(window_parallel_report),
        **store_stats,
        **_engine_stats(engine),
    }


def run_free_running(
    store: V29TraceStore,
    engine: Any,
    *,
    source: Optional[Mapping[str, Any]] = None,
    target_stride: int = 32,
    min_step_cycles: float = 4.0,
    max_step_cycles: float = 1024.0,
    max_no_progress_steps: int = 64,
    max_core_stall_steps: int = 256,
    max_steps: int = 0,
    collect_oracle_drift: bool = True,
    progress_interval: int = 1,
    progress: Optional[Callable[[Mapping[str, Any]], None]] = None,
    window_parallel_mode: str = "serial",
    window_parallel_shift: int = 64,
    window_parallel_depth: int = 0,
    allow_ready_clock_gss_compat: bool = False,
    gss_ablation_mode: str = "predicted-order",
) -> Dict[str, Any]:
    """Run one trace from cursor zero using one virtual time for every core."""
    target_stride = max(1, int(target_stride))
    min_step_cycles = max(0.0, float(min_step_cycles))
    max_step_cycles = float(max_step_cycles)
    if max_step_cycles <= 0:
        raise ValueError("max_step_cycles must be positive")
    max_no_progress_steps = max(1, int(max_no_progress_steps))
    max_core_stall_steps = max(1, int(max_core_stall_steps))
    progress_interval = int(progress_interval)
    window_parallel_mode = str(window_parallel_mode).strip().lower()
    gss_ablation_mode = str(gss_ablation_mode).strip().lower()
    allowed_gss_ablation_modes = {
        "gap0", "state-disabled", "predicted-order", "teacher-order",
    }
    if gss_ablation_mode not in allowed_gss_ablation_modes:
        raise ValueError(
            "unsupported GSS ablation mode; expected one of "
            f"{sorted(allowed_gss_ablation_modes)}"
        )
    if window_parallel_mode not in {"serial", "unconditional", "speculative"}:
        raise ValueError(
            "window_parallel_mode must be serial, unconditional, or speculative"
        )
    if window_parallel_mode == "serial":
        parallel_depth = 1
    else:
        configured_depth = int(window_parallel_depth)
        engine_depth = int(getattr(engine, "parallel_depth", 0))
        parallel_depth = configured_depth if configured_depth > 0 else engine_depth
        if parallel_depth < 2:
            raise ValueError("parallel window modes require depth >= 2")
        if engine_depth > 0 and parallel_depth > engine_depth:
            raise ValueError(
                f"requested depth {parallel_depth} exceeds engine depth {engine_depth}"
            )
        window_parallel_shift = int(window_parallel_shift)
        if not 1 <= window_parallel_shift <= int(store.K):
            raise ValueError("window_parallel_shift must satisfy 1 <= shift <= K")
    has_oracle_labels = bool(getattr(store, "has_oracle_labels", True))
    oracle_drift_enabled = has_oracle_labels and bool(collect_oracle_drift)
    _store_begin(store)
    _engine_begin(engine, store)
    set_gss_ablation = getattr(engine, "set_gss_ablation_mode", None)
    if callable(set_gss_ablation):
        set_gss_ablation(gss_ablation_mode)
    gss_contract = _engine_gss_contract(engine)
    if gss_contract is None and gss_ablation_mode != "predicted-order":
        raise ValueError(
            "non-default GSS ablation mode requires a GSS checkpoint"
        )
    online_gss = gss_ablation_mode in {
        "predicted-order", "state-disabled",
    }
    gss_rollout = (
        GSSSerialRollout(
            store,
            gss_contract,
            rollout_mode=window_parallel_mode,
            allow_ready_clock_compat=allow_ready_clock_gss_compat,
            feature_mode=(
                "state-disabled"
                if gss_ablation_mode == "state-disabled" else "full"
            ),
        )
        if gss_contract is not None and online_gss else None
    )
    parallel_context_pool = (
        V29ParallelContextPool(
            store,
            parallel_depth,
            backend=str(getattr(engine, "context_backend", "thread")),
        )
        if (
            window_parallel_mode != "serial"
            and callable(getattr(engine, "build_context_many", None))
        )
        else None
    )
    context_stats_source: Any = parallel_context_pool or store
    core_count = len(store.core_ids)
    cursors = [0 for _ in store.core_ids]
    active_cycles = [0.0 for _ in store.core_ids]
    retired_uops = [0 for _ in store.core_ids]
    retired_macros = [0 for _ in store.core_ids]
    branch_opportunities = [0 for _ in store.core_ids]
    predicted_branch_misses = [0.0 for _ in store.core_ids]
    last_commit_cycles = {int(core_id): 0.0 for core_id in store.core_ids}
    predicted_endpoints = [float("nan") for _ in store.core_ids]
    global_time = 0.0
    steps = 0
    model_forwards = 0
    parallel_waves = 0
    speculative_windows_issued = 0
    speculative_windows_accepted = 0
    speculative_waves_with_future = 0
    speculative_full_chain_hits = 0
    speculative_failure_depths: Dict[str, int] = {}
    speculative_failure_reasons: Dict[str, int] = {}
    parallel_wave: Optional[V29ParallelWave] = None
    scheduler_window_cpi_errors = ScalarErrors(seed=137)
    scheduler_window_uop_weighted_ape_sum = 0.0
    scheduler_window_evaluated_uops = 0
    scheduler_window_predicted_cycles_sum = 0.0
    scheduler_window_true_cycles_sum = 0.0
    scheduler_window_absolute_cycle_error_sum = 0.0
    consecutive_no_progress = 0
    total_no_progress = 0
    core_stall_steps = [0 for _ in store.core_ids]
    max_observed_core_stall_steps = [0 for _ in store.core_ids]
    core_starvation_guard_fires = 0
    deadline_ledger = _AbsoluteDeadlineLedger()
    advisory_min_step_violations = 0
    stride_overshoots = 0
    head_residual_values = Reservoir(seed=101)
    absolute_head_residual_values = Reservoir(seed=103)
    interval_offset_values = Reservoir(seed=127)
    absolute_interval_offset_values = Reservoir(seed=131)
    interval_offset_times: List[float] = []
    signed_interval_offset_for_slope: List[float] = []
    absolute_interval_offset_for_slope: List[float] = []
    head_spans = Reservoir(seed=107)
    progress_errors = Reservoir(seed=109)
    per_core_progress_error: Dict[int, Reservoir] = {
        int(core_id): Reservoir(capacity=50000, seed=113 + slot)
        for slot, core_id in enumerate(store.core_ids)
    }
    oracle_origin_tick = (
        min(
            int(store.core_meta[core_id]["roi_begin_tick"])
            for core_id in store.core_ids
        ) if has_oracle_labels else 0
    )
    _synchronize(engine)
    started = time.perf_counter()
    context_build_seconds = 0.0
    predict_seconds = 0.0
    scheduler_seconds = 0.0
    oracle_drift_seconds = 0.0
    progress_seconds = 0.0
    complete = False
    while True:
        if all(
            cursors[slot] >= int(store.core_meta[core_id]["n_uops"])
            for slot, core_id in enumerate(store.core_ids)
        ):
            complete = True
            break
        if max_steps > 0 and steps >= int(max_steps):
            break
        if window_parallel_mode == "serial":
            context_started = time.perf_counter()
            context = store.context_from_cursors(
                cursors,
                state_time_cycles=global_time,
                include_labels=False,
                last_commit_cycles=last_commit_cycles,
            )
            context_build_seconds += time.perf_counter() - context_started
            leaked = [key for key in ORACLE_ONLY_KEYS if key in context]
            if leaked:
                raise RuntimeError(
                    f"free-running v29 context leaked oracle keys: {leaked}"
                )
            predict_started = time.perf_counter()
            prediction = _engine_predict_free(
                engine,
                store,
                context,
                gss_rollout=gss_rollout,
                step_start_cycles=global_time,
                deadline_lookup=deadline_ledger.lookup,
            )
            predict_seconds += time.perf_counter() - predict_started
            model_forwards += 1
            slots = [int(value) for value in context["core_slots"].tolist()]
        else:
            if parallel_wave is None:
                parallel_wave, context_seconds, prediction_seconds = (
                    _build_parallel_wave(
                        store,
                        engine,
                        context_pool=parallel_context_pool,
                        anchor_cursors=cursors,
                        global_time=global_time,
                        last_commit_cycles=last_commit_cycles,
                        depth=parallel_depth,
                        shift=window_parallel_shift,
                        mode=window_parallel_mode,
                        gss_rollout=gss_rollout,
                        deadline_lookup=deadline_ledger.lookup,
                    )
                )
                context_build_seconds += context_seconds
                predict_seconds += prediction_seconds
                parallel_waves += 1
                model_forwards += len(parallel_wave.windows)
                if window_parallel_mode == "speculative":
                    future_windows = max(0, len(parallel_wave.windows) - 1)
                    speculative_windows_issued += future_windows
                    speculative_waves_with_future += int(future_windows > 0)
            if window_parallel_mode == "speculative":
                # Keep using the current owner window across as many scheduler
                # transitions as necessary.  Move to the next speculative
                # window only after every still-active core has covered that
                # window's functional start.  Multiple levels may become
                # eligible after one large transition.
                while (
                    parallel_wave.current_window + 1
                    < len(parallel_wave.windows)
                ):
                    next_index = parallel_wave.current_window + 1
                    next_window = parallel_wave.windows[next_index]
                    next_view, next_reason = _speculative_window_step(
                        store,
                        next_window,
                        cursors,
                        target_stride=target_stride,
                    )
                    if next_view is None:
                        if next_reason == "start_not_covered":
                            break
                        depth_key = str(next_window.depth)
                        speculative_failure_depths[depth_key] = (
                            speculative_failure_depths.get(depth_key, 0) + 1
                        )
                        speculative_failure_reasons[next_reason] = (
                            speculative_failure_reasons.get(next_reason, 0) + 1
                        )
                        parallel_wave = None
                        break
                    parallel_wave.current_window = next_index
                    speculative_windows_accepted += 1
                    if (
                        next_index == len(parallel_wave.windows) - 1
                        and not parallel_wave.full_chain_counted
                    ):
                        speculative_full_chain_hits += 1
                        parallel_wave.full_chain_counted = True
                if parallel_wave is None:
                    continue
                window_index = int(parallel_wave.current_window)
                window = parallel_wave.windows[window_index]
                step_view, failure_reason = _speculative_window_step(
                    store,
                    window,
                    cursors,
                    target_stride=target_stride,
                )
                if step_view is None:
                    if (
                        failure_reason == "window_exhausted"
                        and window_index + 1 >= len(parallel_wave.windows)
                    ):
                        # The final owner was consumed normally.  There is no
                        # deeper speculative window to reject; simply re-anchor.
                        parallel_wave = None
                        continue
                    # If the current owner is exhausted before the next
                    # speculative start is covered by every active core, the
                    # first missing depth and every deeper window are invalid.
                    failure_window = window
                    if (
                        failure_reason == "window_exhausted"
                        and window_index + 1 < len(parallel_wave.windows)
                    ):
                        failure_window = parallel_wave.windows[window_index + 1]
                        failure_reason = "start_not_covered"
                    depth_key = str(failure_window.depth)
                    speculative_failure_depths[depth_key] = (
                        speculative_failure_depths.get(depth_key, 0) + 1
                    )
                    speculative_failure_reasons[failure_reason] = (
                        speculative_failure_reasons.get(failure_reason, 0) + 1
                    )
                    parallel_wave = None
                    continue
            else:
                step_view = _unconditional_wave_step(
                    store,
                    parallel_wave,
                    cursors,
                    target_stride=target_stride,
                )
                if step_view is None:
                    parallel_wave = None
                    continue
            parallel_wave.scheduler_steps += 1
            slots = list(step_view.slots)
            prediction = V29Prediction(
                commit_time=step_view.commit_time,
                commit_probability=None,
                progress=None,
                branch_miss_probability=step_view.branch_miss_probability,
                valid_uop_mask=step_view.valid_uop_mask,
            )
        deadline_ledger.reconcile(
            store,
            slots,
            [int(cursors[slot]) for slot in slots],
            prediction,
            now_cycles=global_time,
        )
        scheduler_started = time.perf_counter()
        candidates = []
        for row in range(len(slots)):
            valid_count = int(prediction.valid_uop_mask[row].sum())
            if valid_count <= 0:
                raise RuntimeError("active v29 core has an empty lookahead")
            index = min(target_stride, valid_count) - 1
            candidate = float(prediction.commit_time[row, index])
            if not math.isfinite(candidate) or candidate <= 0:
                raise RuntimeError("v29 scheduler received an invalid target commit time")
            candidates.append(candidate)
        unconstrained_delta = min(candidates)
        # Never clamp upward: doing so would silently consume events beyond the
        # chosen prefix.  The configured minimum is an efficiency advisory and
        # is reported when the model predicts a smaller semantic transition.
        advisory_min_step_violations += int(unconstrained_delta < min_step_cycles)
        delta = min(unconstrained_delta, max_step_cycles)
        delta = max(delta, 1.0e-6)
        consumed_by_row = []
        for row in range(len(slots)):
            consumed = int(np.count_nonzero(
                prediction.valid_uop_mask[row]
                & (prediction.commit_time[row] <= delta + 1.0e-6)
            ))
            consumed_by_row.append(consumed)
            stride_overshoots += int(consumed > target_stride)
            slot = int(slots[row])
            if consumed > 0:
                core_stall_steps[slot] = 0
            else:
                core_stall_steps[slot] += 1
                max_observed_core_stall_steps[slot] = max(
                    max_observed_core_stall_steps[slot],
                    core_stall_steps[slot],
                )
                if core_stall_steps[slot] > max_core_stall_steps:
                    core_starvation_guard_fires += 1
                    core_id = int(store.core_ids[slot])
                    head_deadline = deadline_ledger.lookup(
                        core_id, int(cursors[slot]),
                    )
                    raise RuntimeError(
                        "v29 per-core starvation guard fired "
                        f"trace={store.trace_id} core={core_id} "
                        f"cursor={cursors[slot]} time={global_time:.9g} "
                        f"head_deadline={head_deadline} "
                        f"steps={core_stall_steps[slot]}"
                    )
        if gss_rollout is not None:
            if window_parallel_mode == "serial":
                gss_rollout.commit_context(
                    context,
                    prediction,
                    consumed_by_row,
                    step_start_cycles=global_time,
                )
            else:
                gss_rollout.commit_step(
                    slots,
                    [int(cursors[slot]) for slot in slots],
                    prediction,
                    consumed_by_row,
                    step_start_cycles=global_time,
                )
        step_progress = sum(consumed_by_row)
        if step_progress == 0:
            consecutive_no_progress += 1
            total_no_progress += 1
            if consecutive_no_progress > max_no_progress_steps:
                raise RuntimeError(
                    f"v29 no-progress guard fired trace={store.trace_id} "
                    f"time={global_time:.3f} steps={consecutive_no_progress}"
                )
        else:
            consecutive_no_progress = 0
        step_start = global_time
        step_window_predicted_cycles = 0.0
        step_window_true_cycles = 0.0
        step_window_uops = 0
        for row, (slot, consumed) in enumerate(zip(slots, consumed_by_row)):
            core_id = int(store.core_ids[slot])
            total_uops = int(store.core_meta[core_id]["n_uops"])
            if consumed > 0:
                prefix = slice(0, consumed)
                cursor_start = int(cursors[slot])
                cursor_end = cursor_start + int(consumed)
                previous_predicted_commit = float(last_commit_cycles[core_id])
                branch = np.asarray(
                    store.cores[core_id]["branch"][cursor_start:cursor_end],
                    dtype=np.bool_,
                )
                macro = np.asarray(
                    store.cores[core_id]["macro_end"][cursor_start:cursor_end],
                    dtype=np.bool_,
                )
                branch_opportunities[slot] += int(branch.sum())
                predicted_branch_misses[slot] += float(
                    prediction.branch_miss_probability[row, prefix][branch].sum()
                )
                retired_macros[slot] += int(macro.sum())
                retired_uops[slot] += consumed
                cursors[slot] += consumed
                last_time = step_start + float(
                    prediction.commit_time[row, consumed - 1]
                )
                last_commit_cycles[core_id] = last_time
                if has_oracle_labels:
                    commit_ticks = store.cores[core_id]["commit_tick"]
                    previous_true_tick = (
                        int(commit_ticks[cursor_start - 1])
                        if cursor_start > 0
                        else int(store.core_meta[core_id]["roi_begin_tick"])
                    )
                    current_true_tick = int(commit_ticks[cursor_end - 1])
                    predicted_interval = (
                        last_time - previous_predicted_commit
                    )
                    true_interval = (
                        current_true_tick - previous_true_tick
                    ) / store.tpc
                    if predicted_interval < -1.0e-6 or true_interval < 0.0:
                        raise RuntimeError(
                            "v29 scheduler-window interval is not monotonic"
                        )
                    step_window_predicted_cycles += max(
                        0.0, predicted_interval,
                    )
                    step_window_true_cycles += true_interval
                    step_window_uops += int(consumed)
            finished = cursors[slot] >= total_uops
            if finished:
                finish_delta = (
                    float(prediction.commit_time[row, consumed - 1])
                    if consumed > 0 else 0.0
                )
                active_cycles[slot] += finish_delta
                predicted_endpoints[slot] = step_start + finish_delta
            else:
                active_cycles[slot] += delta
        if has_oracle_labels and step_window_uops > 0:
            predicted_window_cpi = (
                step_window_predicted_cycles / step_window_uops
            )
            true_window_cpi = step_window_true_cycles / step_window_uops
            signed_relative_error = (
                predicted_window_cpi - true_window_cpi
            ) / max(1.0e-3, abs(true_window_cpi))
            absolute_relative_error = abs(signed_relative_error)
            scheduler_window_cpi_errors.add([signed_relative_error])
            scheduler_window_uop_weighted_ape_sum += (
                step_window_uops * absolute_relative_error
            )
            scheduler_window_evaluated_uops += step_window_uops
            scheduler_window_predicted_cycles_sum += (
                step_window_predicted_cycles
            )
            scheduler_window_true_cycles_sum += step_window_true_cycles
            scheduler_window_absolute_cycle_error_sum += abs(
                step_window_predicted_cycles - step_window_true_cycles
            )
        steps += 1
        unfinished_slots = [
            slot for slot, core_id in enumerate(store.core_ids)
            if cursors[slot] < int(store.core_meta[core_id]["n_uops"])
        ]
        if unfinished_slots:
            global_time = step_start + delta
        else:
            global_time = max(
                value for value in predicted_endpoints if math.isfinite(value)
            )
        scheduler_seconds += time.perf_counter() - scheduler_started
        if oracle_drift_enabled:
            oracle_drift_started = time.perf_counter()
            true_head_times = []
            # Include predicted-finished cores in the interval metric.  A
            # cursor at n_uops owns the terminal oracle interval
            # [last_commit, +inf).  Omitting it used to hide precisely the
            # case where a core was predicted to finish much too early.
            for slot, core_id_value in enumerate(store.core_ids):
                core_id = int(core_id_value)
                commits = store.cores[core_id]["commit_tick"]
                cursor = int(cursors[slot])
                if cursor < len(commits):
                    true_head = (
                        int(commits[cursor]) - oracle_origin_tick
                    ) / store.tpc
                    previous_tick = (
                        int(commits[cursor - 1])
                        if cursor > 0
                        else int(store.core_meta[core_id]["roi_begin_tick"])
                    )
                    true_interval_start = (
                        previous_tick - oracle_origin_tick
                    ) / store.tpc
                    head_residual = true_head - global_time
                    if global_time < true_interval_start:
                        # Predicted cursor is ahead of the oracle interval.
                        interval_offset = true_interval_start - global_time
                    elif global_time >= true_head:
                        # Predicted cursor is behind the oracle interval.
                        interval_offset = true_head - global_time
                    else:
                        interval_offset = 0.0
                    head_residual_values.add([head_residual])
                    absolute_head_residual_values.add([abs(head_residual)])
                    true_head_times.append(true_head)
                else:
                    terminal_start = (
                        int(commits[-1]) - oracle_origin_tick
                    ) / store.tpc
                    interval_offset = max(0.0, terminal_start - global_time)
                interval_offset_values.add([interval_offset])
                absolute_interval_offset_values.add([abs(interval_offset)])
                interval_offset_times.append(global_time)
                signed_interval_offset_for_slope.append(interval_offset)
                absolute_interval_offset_for_slope.append(abs(interval_offset))
            if true_head_times:
                head_spans.add([max(true_head_times) - min(true_head_times)])
            oracle_tick = oracle_origin_tick + int(round(global_time * store.tpc))
            for slot, core_id in enumerate(store.core_ids):
                commits = store.cores[core_id]["commit_tick"]
                oracle_cursor = int(np.searchsorted(commits, oracle_tick, side="right"))
                signed_progress = int(cursors[slot]) - oracle_cursor
                progress_errors.add([signed_progress])
                per_core_progress_error[int(core_id)].add([signed_progress])
            oracle_drift_seconds += time.perf_counter() - oracle_drift_started
        should_report_progress = (
            progress is not None
            and progress_interval > 0
            and steps % progress_interval == 0
        )
        if should_report_progress:
            progress_started = time.perf_counter()
            progress_event = {
                "phase": "free_running",
                "_pre_throttled": True,
                "step": steps,
                "global_time": global_time,
                "delta": delta,
                "retired_uops": sum(retired_uops),
                "total_uops": int(store.meta["n_uops"]),
                "active_cores": len(unfinished_slots),
                "no_progress": step_progress == 0,
                "model_forwards": model_forwards,
                "parallel_waves": parallel_waves,
                "window_parallel_mode": window_parallel_mode,
                "speculative_windows_issued": speculative_windows_issued,
                "speculative_windows_accepted": speculative_windows_accepted,
                "speculative_window_hit_rate": (
                    speculative_windows_accepted
                    / max(1, speculative_windows_issued)
                    if window_parallel_mode == "speculative" else None
                ),
            }
            progress_store_stats = _store_stats(context_stats_source)
            progress_context_phases = _context_phase_report(
                progress_store_stats, context_build_seconds,
            )
            progress_event.update({
                "context_total_avg_ms": (
                    1000.0 * context_build_seconds / max(1, model_forwards)
                ),
                "context_parallel_workers": int(
                    progress_store_stats.get("context_parallel_workers", 1)
                ),
                "context_effective_parallelism": float(
                    progress_store_stats.get(
                        "context_effective_parallelism", 1.0,
                    )
                ),
                "context_phase_avg_ms": {
                    name: (
                        1000.0 * progress_context_phases[name]
                        / max(1, model_forwards)
                    )
                    for name in CONTEXT_REPORT_PHASE_NAMES
                },
            })
            if has_oracle_labels:
                running_true_cycles = 0.0
                for slot, core_id in enumerate(store.core_ids):
                    cursor = int(cursors[slot])
                    if cursor <= 0:
                        continue
                    running_true_cycles += (
                        int(store.cores[core_id]["commit_tick"][cursor - 1])
                        - int(store.core_meta[core_id]["roi_begin_tick"])
                    ) / store.tpc
                running_uops = sum(retired_uops)
                running_predicted_cycles = sum(last_commit_cycles.values())
                running_predicted_cpi = (
                    running_predicted_cycles / running_uops
                    if running_uops > 0 else float("nan")
                )
                running_true_cpi = (
                    running_true_cycles / running_uops
                    if running_uops > 0 else float("nan")
                )
                progress_event.update({
                    "running_predicted_roi_uop_cpi": running_predicted_cpi,
                    "running_true_roi_uop_cpi": running_true_cpi,
                    "running_roi_uop_cpi_abs_relative_error": _relative_error(
                        running_predicted_cpi, running_true_cpi,
                    ),
                    "retired_uops_per_step": running_uops / max(1, steps),
                })
            progress(progress_event)
            progress_seconds += time.perf_counter() - progress_started
    _synchronize(engine)
    elapsed = time.perf_counter() - started
    speculative_windows_rejected = max(
        0, speculative_windows_issued - speculative_windows_accepted,
    )
    window_parallel_report = {
        "window_parallel_mode": window_parallel_mode,
        "window_parallel_depth": int(parallel_depth),
        "window_parallel_shift": (
            int(window_parallel_shift)
            if window_parallel_mode != "serial" else 0
        ),
        "parallel_waves": int(parallel_waves),
        "speculative_windows_issued": int(speculative_windows_issued),
        "speculative_windows_accepted": int(speculative_windows_accepted),
        "speculative_windows_rejected": int(speculative_windows_rejected),
        "speculative_window_hit_rate": (
            speculative_windows_accepted / max(1, speculative_windows_issued)
            if window_parallel_mode == "speculative" else None
        ),
        "speculative_full_chain_hits": int(speculative_full_chain_hits),
        "speculative_waves_with_future": int(speculative_waves_with_future),
        "speculative_full_chain_hit_rate": (
            speculative_full_chain_hits / max(1, speculative_waves_with_future)
            if window_parallel_mode == "speculative" else None
        ),
        "speculative_first_failure_depth": dict(speculative_failure_depths),
        "speculative_failure_reasons": dict(speculative_failure_reasons),
    }
    if not has_oracle_labels:
        functional_report = _functional_prediction_report(
            store,
            engine,
            context_stats_source=context_stats_source,
            source=source,
            cursors=cursors,
            active_cycles=active_cycles,
            retired_uops=retired_uops,
            retired_macros=retired_macros,
            branch_opportunities=branch_opportunities,
            predicted_branch_misses=predicted_branch_misses,
            last_commit_cycles=last_commit_cycles,
            predicted_endpoints=predicted_endpoints,
            global_time=global_time,
            steps=steps,
            model_forwards=model_forwards,
            complete=complete,
            elapsed=elapsed,
            target_stride=target_stride,
            min_step_cycles=min_step_cycles,
            max_step_cycles=max_step_cycles,
            advisory_min_step_violations=advisory_min_step_violations,
            stride_overshoots=stride_overshoots,
            total_no_progress=total_no_progress,
            context_build_seconds=context_build_seconds,
            predict_seconds=predict_seconds,
            scheduler_seconds=scheduler_seconds,
            oracle_drift_seconds=oracle_drift_seconds,
            progress_seconds=progress_seconds,
            window_parallel_report=window_parallel_report,
        )
        if gss_rollout is not None:
            functional_report.update(gss_rollout.stats())
        functional_report.update(deadline_ledger.stats())
        functional_report.update({
            "per_core_starvation_guard": PER_CORE_STARVATION_GUARD,
            "max_core_stall_steps": int(max_core_stall_steps),
            "max_observed_core_stall_steps": {
                str(int(core_id)): int(max_observed_core_stall_steps[slot])
                for slot, core_id in enumerate(store.core_ids)
            },
            "core_starvation_guard_fires": int(core_starvation_guard_fires),
        })
        _attach_cache_miss_pmu_error(
            functional_report,
            source=source,
            store=store,
            gss_rollout=gss_rollout,
            complete=complete,
        )
        return functional_report
    true_core_cycles = []
    true_global_endpoints = []
    full_true_core_cycles = []
    full_true_global_endpoints = []
    per_core = []
    total_true_branch_misses = 0
    total_true_branches = 0
    evaluated_true_macros = 0
    full_true_macros = 0
    full_true_branches = 0
    full_true_branch_misses = 0
    evaluated_predicted_cycles = []
    evaluated_predicted_endpoints = []
    core_roi_cpi_errors = []
    core_roi_cpi_signed_errors = []
    for slot, core_id in enumerate(store.core_ids):
        meta = store.core_meta[core_id]
        full_cycles = (
            int(meta["last_commit_tick"]) - int(meta["roi_begin_tick"])
        ) / store.tpc
        full_endpoint = (
            int(meta["last_commit_tick"]) - oracle_origin_tick
        ) / store.tpc
        evaluated_uops = int(cursors[slot])
        arrays = store.cores[core_id]
        if evaluated_uops > 0:
            last_tick = int(arrays["commit_tick"][evaluated_uops - 1])
            true_cycles = (
                last_tick - int(meta["roi_begin_tick"])
            ) / store.tpc
            true_endpoint = (last_tick - oracle_origin_tick) / store.tpc
        else:
            true_cycles = 0.0
            true_endpoint = 0.0
        true_macros = int(np.asarray(
            arrays["macro_end"][:evaluated_uops], dtype=np.int64,
        ).sum())
        true_branches = int(np.asarray(
            arrays["branch"][:evaluated_uops], dtype=np.int64,
        ).sum())
        true_misses = int(np.asarray(
            arrays["branch_miss"][:evaluated_uops], dtype=np.int64,
        ).sum())
        true_core_cycles.append(true_cycles)
        true_global_endpoints.append(true_endpoint)
        full_true_core_cycles.append(full_cycles)
        full_true_global_endpoints.append(full_endpoint)
        evaluated_true_macros += true_macros
        total_true_branches += true_branches
        total_true_branch_misses += true_misses
        full_true_macros += int(meta["n_macros"])
        full_true_branches += int(meta["n_branches"])
        full_true_branch_misses += int(meta["n_branch_misses"])
        predicted_prefix_endpoint = (
            predicted_endpoints[slot]
            if math.isfinite(predicted_endpoints[slot])
            else float(last_commit_cycles[int(core_id)])
        )
        predicted_prefix_cycles = predicted_prefix_endpoint
        if complete and not math.isclose(
            float(active_cycles[slot]), float(predicted_prefix_cycles),
            rel_tol=1.0e-7, abs_tol=1.0e-5,
        ):
            raise RuntimeError(
                f"v29 active-cycle integration mismatch core={core_id}: "
                f"{active_cycles[slot]} != {predicted_prefix_cycles}"
            )
        evaluated_predicted_endpoints.append(predicted_prefix_endpoint)
        evaluated_predicted_cycles.append(predicted_prefix_cycles)
        final_oracle_tick = oracle_origin_tick + int(round(global_time * store.tpc))
        final_oracle_cursor = min(
            int(meta["n_uops"]),
            int(np.searchsorted(arrays["commit_tick"], final_oracle_tick, side="right")),
        )
        final_oracle_progress_error = cursors[slot] - final_oracle_cursor
        predicted_core_cpi = (
            predicted_prefix_cycles / evaluated_uops
            if evaluated_uops > 0 else float("nan")
        )
        true_core_cpi = (
            true_cycles / evaluated_uops
            if evaluated_uops > 0 else float("nan")
        )
        core_cpi_abs_error = (
            _relative_error(predicted_core_cpi, true_core_cpi)
            if evaluated_uops > 0 else float("nan")
        )
        core_cpi_signed_error = (
            (predicted_core_cpi - true_core_cpi)
            / max(1.0e-12, abs(true_core_cpi))
            if evaluated_uops > 0 else float("nan")
        )
        if math.isfinite(core_cpi_abs_error):
            core_roi_cpi_errors.append(core_cpi_abs_error)
            core_roi_cpi_signed_errors.append(core_cpi_signed_error)
        per_core.append({
            "core_id": int(core_id),
            "complete": cursors[slot] == int(meta["n_uops"]),
            "retired_uops": retired_uops[slot],
            "evaluated_true_uops": evaluated_uops,
            "full_true_uops": int(meta["n_uops"]),
            "retired_macros": retired_macros[slot],
            "evaluated_true_macros": true_macros,
            "full_true_macros": int(meta["n_macros"]),
            "predicted_cycles": predicted_prefix_cycles,
            "integrated_active_cycles": active_cycles[slot],
            "true_cycles": true_cycles,
            "full_true_cycles": full_cycles,
            "cycle_signed_error": predicted_prefix_cycles - true_cycles,
            "cycle_abs_relative_error": _relative_error(
                predicted_prefix_cycles, true_cycles,
            ),
            "pred_roi_cpi": predicted_core_cpi,
            "true_roi_cpi": true_core_cpi,
            "roi_cpi_error": core_cpi_abs_error,
            "roi_cpi_signed_error": core_cpi_signed_error,
            "predicted_global_endpoint": predicted_prefix_endpoint,
            "true_global_endpoint": true_endpoint,
            "full_true_global_endpoint": full_endpoint,
            "endpoint_signed_error": (
                predicted_prefix_endpoint - true_endpoint
            ),
            "branch_opportunities": branch_opportunities[slot],
            "evaluated_true_branches": true_branches,
            "full_true_branches": int(meta["n_branches"]),
            "predicted_branch_misses": predicted_branch_misses[slot],
            "evaluated_true_branch_misses": true_misses,
            "full_true_branch_misses": int(meta["n_branch_misses"]),
            "predicted_branch_miss_rate": (
                _event_rate(predicted_branch_misses[slot], branch_opportunities[slot])
            ),
            "true_branch_miss_rate": _event_rate(true_misses, true_branches),
            "branch_miss_count_abs_error": abs(
                predicted_branch_misses[slot] - true_misses
            ),
            "branch_miss_rate_abs_error_pp": abs(
                _event_rate(predicted_branch_misses[slot], branch_opportunities[slot])
                - _event_rate(true_misses, true_branches)
            ) * 100.0,
            "remaining_uops": int(meta["n_uops"]) - cursors[slot],
            "final_oracle_progress_error_uops": final_oracle_progress_error,
            "progress_error": per_core_progress_error[int(core_id)].summary(),
        })
    total_uops = sum(retired_uops)
    total_macros = sum(retired_macros)
    true_uops = sum(cursors)
    true_macros = evaluated_true_macros
    full_true_uops = sum(
        int(store.core_meta[core_id]["n_uops"]) for core_id in store.core_ids
    )
    predicted_cycles_sum = sum(evaluated_predicted_cycles)
    integrated_active_cycles_sum = sum(active_cycles)
    true_cycles_sum = sum(true_core_cycles)
    if not math.isclose(
        scheduler_window_predicted_cycles_sum,
        predicted_cycles_sum,
        rel_tol=1.0e-9,
        abs_tol=1.0e-5,
    ):
        raise RuntimeError(
            "v29 scheduler-window predicted cycles do not reconcile with ROI"
        )
    if not math.isclose(
        scheduler_window_true_cycles_sum,
        true_cycles_sum,
        rel_tol=1.0e-9,
        abs_tol=1.0e-5,
    ):
        raise RuntimeError(
            "v29 scheduler-window true cycles do not reconcile with ROI"
        )
    if scheduler_window_evaluated_uops != total_uops:
        raise RuntimeError(
            "v29 scheduler-window UOPs do not reconcile with retired UOPs"
        )
    scheduler_window_summary = scheduler_window_cpi_errors.summary()
    predicted_micro_cpi = predicted_cycles_sum / max(1, total_uops)
    true_micro_cpi = true_cycles_sum / max(1, true_uops)
    predicted_macro_cpi = predicted_cycles_sum / max(1, total_macros)
    true_macro_cpi = true_cycles_sum / max(1, true_macros)
    predicted_branch_count = sum(predicted_branch_misses)
    predicted_branch_rate = _event_rate(
        predicted_branch_count, sum(branch_opportunities),
    )
    true_branch_rate = _event_rate(total_true_branch_misses, total_true_branches)
    if total_uops != true_uops:
        raise RuntimeError("v29 rollout did not retire the evaluated UOP prefix exactly once")
    if total_macros != true_macros:
        raise RuntimeError("v29 rollout macro accounting is not exact-once")
    if sum(branch_opportunities) != total_true_branches:
        raise RuntimeError("v29 rollout branch accounting is not exact-once")
    if complete and total_uops != full_true_uops:
        raise RuntimeError("complete v29 rollout did not reach the full ROI")
    source_row = dict(source or {})
    head_residuals = head_residual_values.summary()
    absolute_head_residuals = absolute_head_residual_values.summary()
    interval_offsets = interval_offset_values.summary()
    absolute_interval_offsets = absolute_interval_offset_values.summary()
    store_stats = _store_stats(context_stats_source)
    configured_branch_replay = replay_configured_branch_predictor(store)
    timing_breakdown = _free_timing_breakdown(
        store_stats,
        elapsed=elapsed,
        context_build_seconds=context_build_seconds,
        predict_seconds=predict_seconds,
        scheduler_seconds=scheduler_seconds,
        oracle_drift_seconds=oracle_drift_seconds,
        progress_seconds=progress_seconds,
    )
    report = {
        "mode": "single_global_time_free_running",
        "trace_id": store.trace_id,
        "workload": str(source_row.get("workload", store.meta.get("workload", ""))),
        "seed": source_row.get("seed"),
        "n_cores": core_count,
        "complete": complete,
        "steps": steps,
        "global_time_cycles": global_time,
        "target_stride": target_stride,
        "min_step_cycles_advisory": min_step_cycles,
        "max_step_cycles": max_step_cycles,
        "advisory_min_step_violations": advisory_min_step_violations,
        "stride_overshoot_rows": stride_overshoots,
        "no_progress_steps": total_no_progress,
        "predicted_cycles_sum": predicted_cycles_sum,
        "integrated_active_cycles_sum": integrated_active_cycles_sum,
        "true_cycles_sum": true_cycles_sum,
        "retired_uops": total_uops,
        "true_uops": true_uops,
        "evaluated_true_uops": true_uops,
        "full_true_uops": full_true_uops,
        "retired_macros": total_macros,
        "true_macros": true_macros,
        "evaluated_true_macros": true_macros,
        "full_true_macros": full_true_macros,
        # v28-compatible names.  For a complete rollout, v29 micro-CPI is
        # exactly the all-core ROI UOP CPI: sum(core cycles) / sum(ROI UOPs).
        "roi_uops": full_true_uops,
        "evaluated_roi_uops": total_uops,
        "roi_completion_fraction": total_uops / max(1, full_true_uops),
        "roi_label_coverage": 1.0,
        "pred_roi_cpi": predicted_micro_cpi,
        "true_roi_cpi": true_micro_cpi,
        "roi_cpi_error": _relative_error(predicted_micro_cpi, true_micro_cpi),
        "core_roi_cpi_mape_mean": _mean(core_roi_cpi_errors),
        "core_roi_cpi_mape_p50": _pctl(core_roi_cpi_errors, 50),
        "core_roi_cpi_mape_p90": _pctl(core_roi_cpi_errors, 90),
        "core_roi_cpi_mape_p99": _pctl(core_roi_cpi_errors, 99),
        "core_roi_cpi_signed_bias": _mean(core_roi_cpi_signed_errors),
        "scheduler_window_count": scheduler_window_summary["count"],
        "scheduler_window_cpi_mape_mean": scheduler_window_summary["mae"],
        "scheduler_window_cpi_mape_p50": scheduler_window_summary["p50_abs"],
        "scheduler_window_cpi_mape_p90": scheduler_window_summary["p90_abs"],
        "scheduler_window_cpi_mape_p99": scheduler_window_summary["p99_abs"],
        "scheduler_window_cpi_signed_bias": scheduler_window_summary[
            "signed_mean"
        ],
        "scheduler_window_cpi_uop_weighted_mape": (
            scheduler_window_uop_weighted_ape_sum
            / max(1, scheduler_window_evaluated_uops)
        ),
        "scheduler_window_cpi_cycle_wape": (
            scheduler_window_absolute_cycle_error_sum
            / max(1.0e-12, scheduler_window_true_cycles_sum)
        ),
        "scheduler_window_evaluated_uops": scheduler_window_evaluated_uops,
        "scheduler_window_predicted_cycles_sum": (
            scheduler_window_predicted_cycles_sum
        ),
        "scheduler_window_true_cycles_sum": scheduler_window_true_cycles_sum,
        # Compatible aliases for reporting consumers.  Unlike fixed functional
        # chunks, these windows are the actual variable-prefix scheduler steps.
        "window_cpi_mape_mean": scheduler_window_summary["mae"],
        "window_cpi_mape_p50": scheduler_window_summary["p50_abs"],
        "window_cpi_mape_p90": scheduler_window_summary["p90_abs"],
        "window_cpi_mape_p99": scheduler_window_summary["p99_abs"],
        "predicted_micro_cpi": predicted_micro_cpi,
        "true_micro_cpi": true_micro_cpi,
        "micro_cpi_abs_relative_error": _relative_error(
            predicted_micro_cpi, true_micro_cpi,
        ),
        "predicted_macro_cpi": predicted_macro_cpi,
        "true_macro_cpi": true_macro_cpi,
        "macro_cpi_abs_relative_error": _relative_error(
            predicted_macro_cpi, true_macro_cpi,
        ),
        "predicted_makespan": max(evaluated_predicted_endpoints),
        "true_makespan": max(true_global_endpoints),
        "full_true_makespan": max(full_true_global_endpoints),
        "makespan_abs_relative_error": _relative_error(
            max(evaluated_predicted_endpoints),
            max(true_global_endpoints),
        ),
        "predicted_branch_misses": predicted_branch_count,
        "true_branch_misses": total_true_branch_misses,
        "evaluated_true_branch_misses": total_true_branch_misses,
        "full_true_branch_misses": full_true_branch_misses,
        "branch_miss_count_abs_relative_error": abs(
            predicted_branch_count - total_true_branch_misses
        ) / max(1.0, float(total_true_branch_misses)),
        "branch_miss_count_abs_error": abs(
            predicted_branch_count - total_true_branch_misses
        ),
        "branch_opportunities": sum(branch_opportunities),
        "evaluated_true_branches": total_true_branches,
        "full_true_branches": full_true_branches,
        "predicted_branch_miss_rate": predicted_branch_rate,
        "true_branch_miss_rate": true_branch_rate,
        "branch_miss_rate_abs_error_pp": abs(
            predicted_branch_rate - true_branch_rate
        ) * 100.0,
        "oracle_head_residual_cycles": head_residuals,
        "oracle_head_abs_residual_cycles": absolute_head_residuals,
        "oracle_cursor_interval_offset_cycles": interval_offsets,
        "oracle_cursor_interval_abs_offset_cycles": absolute_interval_offsets,
        "oracle_cursor_interval_signed_slope_cycles_per_cycle": _linear_slope(
            interval_offset_times, signed_interval_offset_for_slope,
        ),
        "oracle_cursor_interval_abs_slope_cycles_per_cycle": _linear_slope(
            interval_offset_times, absolute_interval_offset_for_slope,
        ),
        "oracle_drift_diagnostics_enabled": oracle_drift_enabled,
        "cross_core_oracle_head_span_cycles": head_spans.summary(),
        "cumulative_progress_error_uops": progress_errors.summary(),
        "per_core": per_core,
        "branch_replay_baseline": configured_branch_replay,
        "legacy_gshare_direction_only_replay": replay_branch_baseline(store),
        "elapsed_s": elapsed,
        "steps_per_s": steps / max(elapsed, 1.0e-12),
        "uops_per_s": total_uops / max(elapsed, 1.0e-12),
        "model_forwards": model_forwards,
        "retired_uops_per_model_forward": total_uops / max(1, model_forwards),
        "timing_breakdown": timing_breakdown,
        "virtual_time_semantics": "one_shared_clock",
        "initialization_contract": "all_functional_core_streams_active_at_T0",
        "model_context_uses_oracle_timing": (
            gss_ablation_mode == "teacher-order"
        ),
        "oracle_timing_usage": (
            "teacher-ready-order-gss-input-and-final-metrics"
            if gss_ablation_mode == "teacher-order" else (
                "post_transition_drift_and_final_metrics"
                if oracle_drift_enabled else "final_metrics_only"
            )
        ),
        "gss_ablation_mode": gss_ablation_mode,
        "metric_scope": "full_roi" if complete else "consumed_functional_prefix",
        "predicted_context_used_as_training_label": False,
        "free_timing_reconstruction": FREE_TIMING_RECONSTRUCTION_CONTRACT,
        "min_retirement_gap_cycles": MIN_RETIREMENT_GAP_CYCLES,
        "per_core_starvation_guard": PER_CORE_STARVATION_GUARD,
        "max_core_stall_steps": int(max_core_stall_steps),
        "max_observed_core_stall_steps": {
            str(int(core_id)): int(max_observed_core_stall_steps[slot])
            for slot, core_id in enumerate(store.core_ids)
        },
        "core_starvation_guard_fires": int(core_starvation_guard_fires),
        **window_parallel_report,
        **deadline_ledger.stats(),
        **store_stats,
        **_engine_stats(engine),
        **(gss_rollout.stats() if gss_rollout is not None else {}),
    }
    _attach_cache_miss_pmu_error(
        report,
        source=source,
        store=store,
        gss_rollout=gss_rollout,
        complete=complete,
    )
    return report


def load_manifest_sources(
    manifest_path: str, split_names: Sequence[str],
) -> List[Dict[str, Any]]:
    manifest = load_json(manifest_path)
    if manifest.get("quality", {}).get("status") != "pass":
        raise RuntimeError(
            "v29 manifest quality is not pass: "
            + "; ".join(map(str, manifest.get("quality", {}).get("blockers", [])))
        )
    base = os.path.dirname(os.path.abspath(manifest_path))
    unique: Dict[str, Dict[str, Any]] = {}
    for split in split_names:
        for item in manifest.get("splits", {}).get(str(split), []):
            source = dict(item) if isinstance(item, Mapping) else {"cache_dir": str(item)}
            path = str(source.get("cache_dir", ""))
            if not path:
                continue
            path = path if os.path.isabs(path) else os.path.join(base, path)
            source["cache_dir"] = os.path.abspath(path)
            for sidecar_key in (
                "long_history_dir", "branch_replay_dir", "gss_sidecar_dir",
                "exposure_sidecar_dir",
            ):
                sidecar = source.get(sidecar_key)
                if sidecar:
                    sidecar_path = str(sidecar)
                    source[sidecar_key] = os.path.abspath(
                        sidecar_path
                        if os.path.isabs(sidecar_path)
                        else os.path.join(base, sidecar_path)
                    )
            source["split"] = str(split)
            source["source_splits"] = [str(split)]
            existing = unique.get(source["cache_dir"])
            if existing is None:
                unique[source["cache_dir"]] = source
            else:
                memberships = list(existing.get("source_splits", []))
                if str(split) not in memberships:
                    memberships.append(str(split))
                existing["source_splits"] = memberships
                if str(split) == "development_heldout":
                    existing["split"] = str(split)
    return sorted(
        unique.values(),
        key=lambda row: (
            int(row.get("n_cores", 0)),
            str(row.get("workload", "")),
            int(row.get("seed", 0)),
            str(row["cache_dir"]),
        ),
    )


def discover_sources(cache_root: str) -> List[Dict[str, Any]]:
    return [
        {"cache_dir": path}
        for path in discover_trace_caches(cache_root)
    ]


def _average_metric(rows: Sequence[Mapping[str, Any]], path: Sequence[str]) -> float:
    values = []
    for row in rows:
        value: Any = row
        try:
            for key in path:
                value = value[key]
            value = float(value)
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return _mean(values)


def _group_summary(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    free = [row for row in rows if isinstance(row.get("free_running"), Mapping)]
    complete_free = [
        row for row in free if bool(row["free_running"].get("complete"))
    ]
    oracle = [row for row in rows if isinstance(row.get("oracle_one_step"), Mapping)]
    speculative_issued = sum(
        int(row["free_running"].get("speculative_windows_issued", 0))
        for row in free
    )
    speculative_accepted = sum(
        int(row["free_running"].get("speculative_windows_accepted", 0))
        for row in free
    )
    speculative_full_chain_hits = sum(
        int(row["free_running"].get("speculative_full_chain_hits", 0))
        for row in free
    )
    parallel_waves = sum(
        int(row["free_running"].get("parallel_waves", 0))
        for row in free
    )
    speculative_waves_with_future = sum(
        int(row["free_running"].get("speculative_waves_with_future", 0))
        for row in free
    )
    return {
        "traces": len(rows),
        "complete_free_running": len(complete_free),
        "incomplete_free_running": len(free) - len(complete_free),
        "micro_cpi_mape": _average_metric(
            complete_free, ("free_running", "micro_cpi_abs_relative_error"),
        ),
        "macro_cpi_mape": _average_metric(
            complete_free, ("free_running", "macro_cpi_abs_relative_error"),
        ),
        "makespan_mape": _average_metric(
            complete_free, ("free_running", "makespan_abs_relative_error"),
        ),
        "scheduler_window_cpi_mape_mean": _average_metric(
            complete_free,
            ("free_running", "scheduler_window_cpi_mape_mean"),
        ),
        "scheduler_window_cpi_mape_p90": _average_metric(
            complete_free,
            ("free_running", "scheduler_window_cpi_mape_p90"),
        ),
        "scheduler_window_cpi_uop_weighted_mape": _average_metric(
            complete_free,
            ("free_running", "scheduler_window_cpi_uop_weighted_mape"),
        ),
        "branch_count_mape": _average_metric(
            complete_free, ("free_running", "branch_miss_count_abs_relative_error"),
        ),
        "branch_rate_abs_error_pp": _average_metric(
            complete_free, ("free_running", "branch_miss_rate_abs_error_pp"),
        ),
        "oracle_cursor_interval_abs_offset_p50_cycles": _average_metric(
            free,
            ("free_running", "oracle_cursor_interval_abs_offset_cycles", "p50"),
        ),
        "oracle_cursor_interval_abs_offset_p90_cycles": _average_metric(
            free,
            ("free_running", "oracle_cursor_interval_abs_offset_cycles", "p90"),
        ),
        "oracle_cursor_interval_abs_offset_p99_cycles": _average_metric(
            free,
            ("free_running", "oracle_cursor_interval_abs_offset_cycles", "p99"),
        ),
        "oracle_cursor_interval_abs_slope": _average_metric(
            free,
            ("free_running", "oracle_cursor_interval_abs_slope_cycles_per_cycle"),
        ),
        "oracle_head_abs_residual_p99_cycles": _average_metric(
            free,
            ("free_running", "oracle_head_abs_residual_cycles", "p99"),
        ),
        "steps_per_s": _average_metric(
            free, ("free_running", "steps_per_s"),
        ),
        "uops_per_s": _average_metric(
            free, ("free_running", "uops_per_s"),
        ),
        "static_cache_hit_rate": _average_metric(
            free, ("free_running", "static_cache_hit_rate"),
        ),
        "speculative_windows_issued": speculative_issued,
        "speculative_windows_accepted": speculative_accepted,
        "speculative_window_hit_rate": (
            speculative_accepted / speculative_issued
            if speculative_issued > 0 else float("nan")
        ),
        "speculative_full_chain_hits": speculative_full_chain_hits,
        "parallel_waves": parallel_waves,
        "speculative_waves_with_future": speculative_waves_with_future,
        "speculative_full_chain_hit_rate": (
            speculative_full_chain_hits / speculative_waves_with_future
            if speculative_waves_with_future > 0 else float("nan")
        ),
        "oracle_commit_log_mae": _average_metric(
            oracle,
            ("oracle_one_step", "commit_time_log_error", "mae"),
        ),
        "oracle_commit_cycle_mae": _average_metric(
            oracle,
            ("oracle_one_step", "commit_time_cycle_error", "mae"),
        ),
        "oracle_branch_brier": _average_metric(
            oracle, ("oracle_one_step", "branch_token", "brier"),
        ),
        "oracle_branch_auc": _average_metric(
            oracle, ("oracle_one_step", "branch_token", "auc_histogram"),
        ),
    }


def _report_category(row: Mapping[str, Any]) -> str:
    role = str(row.get("workload_role", ""))
    workload = str(row.get("workload", ""))
    if role == "business_heldout" or workload.endswith("_heldout"):
        return "business_heldout"
    if role in {"train", "train_base", "base"}:
        return "base"
    memberships = set(str(value) for value in row.get("source_splits", []))
    split = str(row.get("source_split", ""))
    memberships.add(split)
    if "development_heldout" in memberships:
        return "business_heldout"
    if "deployment_inference" in memberships:
        return "deployment_seed"
    return "base"


def aggregate_trace_reports(
    trace_reports: Sequence[Mapping[str, Any]],
    *,
    run: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    rows = [dict(row) for row in trace_reports]
    by_core: Dict[int, List[Dict[str, Any]]] = {}
    by_workload: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    by_category: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    by_seed: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for row in rows:
        cores = int(row.get("n_cores", 0))
        workload = str(row.get("workload", ""))
        by_core.setdefault(cores, []).append(row)
        by_workload.setdefault((cores, workload), []).append(row)
        by_category.setdefault((cores, _report_category(row)), []).append(row)
        by_seed.setdefault((cores, str(row.get("seed", "unknown"))), []).append(row)
    core_reports = []
    for cores in sorted(by_core):
        workload_rows = [
            {
                "workload": workload,
                **_group_summary(group),
            }
            for (group_cores, workload), group in sorted(by_workload.items())
            if group_cores == cores
        ]
        # The top-level core-count row is workload-equal.  A long trace or a
        # workload with more seeds cannot dominate the headline metric.
        workload_wrappers = [
            {
                "n_cores": cores,
                "workload": item["workload"],
                "free_running": {
                    "micro_cpi_abs_relative_error": item["micro_cpi_mape"],
                    "macro_cpi_abs_relative_error": item["macro_cpi_mape"],
                    "makespan_abs_relative_error": item["makespan_mape"],
                    "scheduler_window_cpi_mape_mean": item[
                        "scheduler_window_cpi_mape_mean"
                    ],
                    "scheduler_window_cpi_mape_p90": item[
                        "scheduler_window_cpi_mape_p90"
                    ],
                    "scheduler_window_cpi_uop_weighted_mape": item[
                        "scheduler_window_cpi_uop_weighted_mape"
                    ],
                    "branch_miss_count_abs_relative_error": item["branch_count_mape"],
                    "branch_miss_rate_abs_error_pp": item["branch_rate_abs_error_pp"],
                    "oracle_cursor_interval_abs_offset_cycles": {
                        "p50": item["oracle_cursor_interval_abs_offset_p50_cycles"],
                        "p90": item["oracle_cursor_interval_abs_offset_p90_cycles"],
                        "p99": item["oracle_cursor_interval_abs_offset_p99_cycles"],
                    },
                    "oracle_cursor_interval_abs_slope_cycles_per_cycle": item[
                        "oracle_cursor_interval_abs_slope"
                    ],
                    "oracle_head_abs_residual_cycles": {
                        "p99": item["oracle_head_abs_residual_p99_cycles"],
                    },
                    "steps_per_s": item["steps_per_s"],
                    "uops_per_s": item["uops_per_s"],
                    "static_cache_hit_rate": item["static_cache_hit_rate"],
                    "speculative_windows_issued": item[
                        "speculative_windows_issued"
                    ],
                    "speculative_windows_accepted": item[
                        "speculative_windows_accepted"
                    ],
                    "speculative_full_chain_hits": item[
                        "speculative_full_chain_hits"
                    ],
                    "parallel_waves": item["parallel_waves"],
                    "speculative_waves_with_future": item[
                        "speculative_waves_with_future"
                    ],
                    "complete": item["complete_free_running"] == item["traces"],
                },
                "oracle_one_step": {
                    "commit_time_log_error": {"mae": item["oracle_commit_log_mae"]},
                    "commit_time_cycle_error": {"mae": item["oracle_commit_cycle_mae"]},
                    "branch_token": {
                        "brier": item["oracle_branch_brier"],
                        "auc_histogram": item["oracle_branch_auc"],
                    },
                },
            }
            for item in workload_rows
        ]
        summary = _group_summary(workload_wrappers)
        summary["traces"] = len(by_core[cores])
        summary["workloads"] = len(workload_rows)
        summary["complete_free_running"] = sum(
            bool(row.get("free_running", {}).get("complete"))
            for row in by_core[cores]
        )
        summary["complete_free_running_workloads"] = sum(
            int(item["complete_free_running"] == item["traces"])
            for item in workload_rows
        )
        core_reports.append({
            "n_cores": cores,
            **summary,
            "by_category": [
                {"category": category, **_group_summary(group)}
                for (group_cores, category), group in sorted(by_category.items())
                if group_cores == cores
            ],
            "by_seed": [
                {"seed": seed, **_group_summary(group)}
                for (group_cores, seed), group in sorted(by_seed.items())
                if group_cores == cores
            ],
            "by_workload": workload_rows,
        })
    return {
        "schema_version": "tcsim-v29-evaluation-report-1",
        "run": dict(run or {}),
        "aggregation_contract": {
            "core_count_headlines": "workload_equal_mean",
            "workload_rows": "trace_equal_mean",
            "cpi": "sum_core_cycles_div_sum_retired_uops_or_macros",
            "branch": "sum_miss_count_div_sum_branch_opportunities",
            "global_pooled_headline_forbidden": True,
        },
        "trace_count": len(rows),
        "by_core_count": core_reports,
        "traces": rows,
    }


def _format_percent(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if not math.isfinite(number) else f"{number * 100.0:.2f}%"


def _format_number(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "n/a"
    return "n/a" if not math.isfinite(number) else f"{number:.{digits}f}"


def render_text_report(
    report: Mapping[str, Any], *, source_json: Optional[str] = None,
) -> str:
    """Render the v29 report in the established v28 deployment layout."""
    separator = "=" * 164
    rule = "-" * 164

    def value(row: Mapping[str, Any], *path: str) -> float:
        current: Any = row
        try:
            for key in path:
                current = current[key]
            number = float(current)
        except (KeyError, TypeError, ValueError):
            return float("nan")
        return number if math.isfinite(number) else float("nan")

    def percent(number: float) -> str:
        return "n/a" if not math.isfinite(number) else f"{100.0 * number:.2f}"

    traces = [dict(row) for row in report.get("traces", [])]
    workload_groups: Dict[Tuple[int, str], List[Dict[str, Any]]] = {}
    for row in traces:
        workload_groups.setdefault(
            (int(row.get("n_cores", 0)), str(row.get("workload", ""))), [],
        ).append(row)

    workload_rows = []
    for (cores, workload), rows in sorted(workload_groups.items()):
        complete = [
            row for row in rows
            if bool(row.get("free_running", {}).get("complete"))
        ]
        free_rows = complete or [
            row for row in rows if isinstance(row.get("free_running"), Mapping)
        ]

        def average(*path: str) -> float:
            return _mean([value(row, *path) for row in free_rows])

        categories = [_report_category(row) for row in rows]
        category = (
            "heldout" if "business_heldout" in categories else "train/base"
        )
        speculative_issued = sum(
            int(row.get("free_running", {}).get("speculative_windows_issued", 0))
            for row in free_rows
        )
        speculative_accepted = sum(
            int(row.get("free_running", {}).get("speculative_windows_accepted", 0))
            for row in free_rows
        )
        pmu_qualified = [
            row for row in free_rows
            if bool(
                row.get("free_running", {})
                .get("cache_miss_pmu_error", {})
                .get("qualified")
            )
        ]
        workload_rows.append({
            "cores": cores,
            "workload": workload,
            "category": category,
            "traces": len(rows),
            "complete": len(complete) == len(rows),
            "steps": average("free_running", "steps"),
            "pred_roi": average("free_running", "pred_roi_cpi"),
            "true_roi": average("free_running", "true_roi_cpi"),
            "roi_error": average("free_running", "roi_cpi_error"),
            "window_mape": average(
                "free_running", "scheduler_window_cpi_mape_mean",
            ),
            "window_p90": average(
                "free_running", "scheduler_window_cpi_mape_p90",
            ),
            "branch_pred": average("free_running", "predicted_branch_miss_rate"),
            "branch_true": average("free_running", "true_branch_miss_rate"),
            "branch_error": average(
                "free_running", "branch_miss_count_abs_relative_error",
            ),
            "branch_abs_pp": average(
                "free_running", "branch_miss_rate_abs_error_pp",
            ),
            "drift_p99": average(
                "free_running", "oracle_cursor_interval_abs_offset_cycles", "p99",
            ),
            "uops_per_s": average("free_running", "uops_per_s"),
            "uops_per_forward": average(
                "free_running", "retired_uops_per_model_forward",
            ),
            "speculative_hit_rate": (
                speculative_accepted / speculative_issued
                if speculative_issued > 0 else float("nan")
            ),
            "cache_pmu_qualified": len(pmu_qualified),
            "l1d_miss_error": average(
                "free_running", "cache_miss_pmu_error", "metrics",
                "l1d_misses", "absolute_relative_count_error",
            ),
            "l1d_miss_abs_pp": average(
                "free_running", "cache_miss_pmu_error", "metrics",
                "l1d_misses", "rate_abs_error_pp",
            ),
            "l2_miss_error": average(
                "free_running", "cache_miss_pmu_error", "metrics",
                "l2_misses", "absolute_relative_count_error",
            ),
            "l2_miss_abs_pp": average(
                "free_running", "cache_miss_pmu_error", "metrics",
                "l2_misses", "rate_abs_error_pp",
            ),
            "llc_miss_error": average(
                "free_running", "cache_miss_pmu_error", "metrics",
                "llc_misses", "absolute_relative_count_error",
            ),
            "llc_miss_abs_pp": average(
                "free_running", "cache_miss_pmu_error", "metrics",
                "llc_misses", "rate_abs_error_pp",
            ),
        })

    evaluated_uops = sum(
        int(row.get("free_running", {}).get("evaluated_roi_uops", 0))
        for row in traces
    )
    full_uops = sum(
        int(row.get("free_running", {}).get("roi_uops", 0))
        for row in traces
    )
    run = report.get("run", {})
    lines = [
        "TCSim v29 deployment evaluation report",
        separator,
        f"source_json : {source_json or 'worker/in-memory report'}",
        f"checkpoint  : {run.get('checkpoint', '')}",
        f"split       : {','.join(map(str, run.get('splits', [])))}",
        f"coverage    : traces={len(traces)} ROI_uops={evaluated_uops}/{full_uops} "
        f"({_format_percent(evaluated_uops / max(1, full_uops))})",
        "",
        "Metric definitions",
        "- ROI-CPI error: abs(predicted full-trace ROI UOP CPI - true ROI UOP CPI) / true ROI UOP CPI.",
        "- branch relative error: relative error of full-ROI branch-miss count; branch abs pp is rate difference.",
        "- drift p99: p99 absolute distance from predicted cursor time to its oracle commit interval.",
        "- speculative hit: accepted future windows / all issued future windows; anchor window 0 is excluded.",
        "- macro rows weight workloads equally within each core count; incomplete rollouts are excluded from ROI headlines.",
        "- scheduler-window CPI MAPE: local CPI error over each actual variable-prefix scheduler transition.",
        "- scheduler-window partitions are mode-dependent; use this metric for deployed-path accuracy, not fixed-window model isolation.",
        "- fixed-256-UOP chunk MAPE is not reported and needs a separate compatible audit.",
        "- cache-miss PMU error compares post-rollout canonical GSS proxy counts with gem5 path_class labels; path_class is never a model input.",
        "",
        "Primary result: workload-macro accuracy by core count",
        rule,
        "cores set          n  ROImean%  ROIp50%  ROIp90%  WINmean%   BRmean%   BRp50%   BRp90%  BRabsPP  driftP99  specHit    uops/s  uops/fwd",
        rule,
    ]
    core_counts = sorted({int(row["cores"]) for row in workload_rows})
    for cores in core_counts:
        for category in ("all", "train/base", "heldout"):
            selected = [
                row for row in workload_rows
                if row["cores"] == cores
                and row["complete"]
                and (category == "all" or row["category"] == category)
            ]
            if not selected:
                continue
            roi = [float(row["roi_error"]) for row in selected]
            window = [float(row["window_mape"]) for row in selected]
            branch = [float(row["branch_error"]) for row in selected]
            lines.append(
                f"{cores:5d} {category:<10} {len(selected):3d} "
                f"{percent(_mean(roi)):>9} {percent(_pctl(roi, 50)):>8} "
                f"{percent(_pctl(roi, 90)):>8} "
                f"{percent(_mean(window)):>9} "
                f"{percent(_mean(branch)):>9} {percent(_pctl(branch, 50)):>8} "
                f"{percent(_pctl(branch, 90)):>8} "
                f"{_format_number(_mean([row['branch_abs_pp'] for row in selected])):>8} "
                f"{_format_number(_mean([row['drift_p99'] for row in selected])):>9} "
                f"{percent(_mean([row['speculative_hit_rate'] for row in selected])):>8} "
                f"{_format_number(_mean([row['uops_per_s'] for row in selected]), 0):>9} "
                f"{_format_number(_mean([row['uops_per_forward'] for row in selected]), 1):>9}"
            )
    lines.extend([rule, ""])

    for cores in core_counts:
        selected = [row for row in workload_rows if row["cores"] == cores]
        lines.extend([
            f"Per-workload detail: c{cores:02d}",
            rule,
            "workload                           set          steps   predROI  trueROI  ROIerr%  WINmean%   WINp90%   BRpred%  BRtrue%   BRerr%  BRabsPP  driftP99  specHit    uops/s  uops/fwd",
            rule,
        ])
        for row in selected:
            lines.append(
                f"{str(row['workload']):<34} {str(row['category']):<10} "
                f"{_format_number(row['steps'], 0):>7} "
                f"{_format_number(row['pred_roi'], 4):>9} "
                f"{_format_number(row['true_roi'], 4):>8} "
                f"{percent(row['roi_error']):>8} "
                f"{percent(row['window_mape']):>9} "
                f"{percent(row['window_p90']):>9} "
                f"{percent(row['branch_pred']):>9} "
                f"{percent(row['branch_true']):>8} "
                f"{percent(row['branch_error']):>8} "
                f"{_format_number(row['branch_abs_pp']):>8} "
                f"{_format_number(row['drift_p99']):>9} "
                f"{percent(row['speculative_hit_rate']):>8} "
                f"{_format_number(row['uops_per_s'], 0):>9} "
                f"{_format_number(row['uops_per_forward'], 1):>9}"
            )
        lines.extend([rule, ""])

    if any(int(row["cache_pmu_qualified"]) > 0 for row in workload_rows):
        lines.extend([
            "Cache-miss PMU audit: canonical GSS proxy versus gem5 labels",
            rule,
            "cores workload                               set          n  L1Derr%  L1DabsPP   L2err%  L2absPP  LLCerr%  LLCabsPP",
            rule,
        ])
        for row in workload_rows:
            if int(row["cache_pmu_qualified"]) <= 0:
                continue
            lines.append(
                f"{int(row['cores']):5d} {str(row['workload']):<38} "
                f"{str(row['category']):<10} {int(row['cache_pmu_qualified']):2d} "
                f"{percent(row['l1d_miss_error']):>9} "
                f"{_format_number(row['l1d_miss_abs_pp']):>9} "
                f"{percent(row['l2_miss_error']):>8} "
                f"{_format_number(row['l2_miss_abs_pp']):>8} "
                f"{percent(row['llc_miss_error']):>8} "
                f"{_format_number(row['llc_miss_abs_pp']):>8}"
            )
        lines.extend([rule, ""])

    largest = sorted(
        [row for row in workload_rows if math.isfinite(float(row["roi_error"]))],
        key=lambda row: float(row["roi_error"]), reverse=True,
    )[:15]
    lines.extend([
        "Largest ROI-CPI errors",
        rule,
        "cores workload                               set          ROIerr%   BRerr%  driftP99",
        rule,
    ])
    for row in largest:
        lines.append(
            f"{int(row['cores']):5d} {str(row['workload']):<38} "
            f"{str(row['category']):<10} {percent(row['roi_error']):>9} "
            f"{percent(row['branch_error']):>8} "
            f"{_format_number(row['drift_p99']):>9}"
        )
    lines.extend([
        separator,
        "Semantics",
        "- one shared virtual clock for every active core",
        "- predicted cursors construct deployment contexts",
        "- oracle commit ticks are used only after transitions for metrics",
        "- branch and macro events are accumulated exactly once from consumed prefixes",
        "- cache PMU oracle labels are loaded only after a complete rollout and are reporting-only",
    ])
    return "\n".join(lines) + "\n"


def write_evaluation_report(out_dir: str, report: Mapping[str, Any]) -> Dict[str, str]:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    text_path = os.path.join(out_dir, "report.txt")
    dump_json(json_path, report)
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(render_text_report(report, source_json=json_path))
    return {"json": json_path, "text": text_path}
