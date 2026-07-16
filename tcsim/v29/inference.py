"""v29 oracle one-step evaluation and single-global-time deployment rollout.

The deployment path never selects a context with oracle commit ticks.  It owns
one virtual clock, advances predicted cursors, and consults timing arrays only
after each transition to measure drift.  This separation is deliberately kept
inside one module so it can be tested as an invariant rather than a convention
of a shell script.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import hashlib
import json
import math
import os
import random
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import torch

from ..utils.config import TCSimConfig
from ..utils.io import dump_json, load_json
from .contracts import CHECKPOINT_SCHEMA_VERSION, FIELD_INDEX
from .dataset import V29TraceStore, discover_trace_caches
from .model import TCSimV29Model, build_model


MODEL_TENSOR_KEYS = (
    "per_uop_fields",
    "dynamic_uop_fields",
    "valid_uop_mask",
    "chunk_summary",
    "relation_features",
    "uarch_features",
    "state_features",
)
ORACLE_ONLY_KEYS = (
    "branch_miss_target",
    "commit_time_target",
    "prefix_target",
    "progress_target",
)


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
    commit_probability: np.ndarray
    progress: np.ndarray
    branch_miss_probability: np.ndarray
    valid_uop_mask: np.ndarray


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
    return {key: store.meta[key] for key in keys}


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
        self._static_by_core: Dict[int, Tuple[Tuple[Any, ...], torch.Tensor]] = {}
        self.static_hits = 0
        self.static_misses = 0
        self.static_evictions = 0
        self._active_trace = ""
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

    def _autocast(self):
        if self.amp_dtype is None:
            return nullcontext()
        return torch.autocast(
            device_type=self.device.type, dtype=self.amp_dtype, enabled=True,
        )

    def begin_trace(self, store: V29TraceStore) -> None:
        if _store_contract(store) != self.contract:
            raise RuntimeError(
                f"v29 checkpoint/cache contract mismatch for {store.trace_id}"
            )
        self._active_trace = store.trace_id
        self._static_by_core.clear()
        self.static_hits = 0
        self.static_misses = 0
        self.static_evictions = 0
        if self.device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(self.device)

    def _model_batch(self, context: Mapping[str, Any]) -> Dict[str, torch.Tensor]:
        batch = {
            key: context[key].to(self.device, non_blocking=True)
            for key in MODEL_TENSOR_KEYS
        }
        rows = int(batch["per_uop_fields"].shape[0])
        batch["sample_ptr"] = torch.tensor([0, rows], dtype=torch.long)
        return batch

    def predict(
        self, store: V29TraceStore, context: Mapping[str, Any],
    ) -> V29Prediction:
        if self._active_trace != store.trace_id:
            self.begin_trace(store)
        batch = self._model_batch(context)
        rows = int(batch["per_uop_fields"].shape[0])
        slots = [int(value) for value in context["core_slots"].tolist()]
        cursors = [int(value) for value in context["cursors"].tolist()]
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
            output = self.model.forward_from_static(batch, static_tokens)
        commit_time = output["commit_time"].float().cpu().numpy()
        if np.any(~np.isfinite(commit_time)):
            raise RuntimeError("v29 timing head produced a non-finite commit time")
        valid = context["valid_uop_mask"].cpu().numpy().astype(bool, copy=False)
        differences = np.diff(commit_time, axis=1)
        pair_valid = valid[:, 1:] & valid[:, :-1]
        if np.any(differences[pair_valid] < -1.0e-6):
            raise RuntimeError("v29 timing head violated monotonicity")
        return V29Prediction(
            commit_time=commit_time,
            commit_probability=output["commit_probability"].float().cpu().numpy(),
            progress=output["progress"].float().cpu().numpy(),
            branch_miss_probability=(
                output["branch_miss_probability"].float().cpu().numpy()
            ),
            valid_uop_mask=valid,
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
        }


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


def replay_branch_baseline(store: V29TraceStore, entries: int = 4096) -> Dict[str, Any]:
    """Replay a deterministic functional gshare + last-target baseline.

    This is intentionally not presented as a clone of the configured gem5
    predictor.  It is a deployable reference that consumes only committed
    functional branch outcomes and targets, and makes branch-head regressions
    visible independently of the learned timing head.
    """
    table_entries = 1
    while table_entries < max(16, int(entries)):
        table_entries <<= 1
    mask = table_entries - 1
    true_misses = 0
    branches = 0
    direction_misses = 0
    target_misses = 0
    combined_misses = 0
    per_core = []
    for core_id in store.core_ids:
        arrays = store.cores[core_id]
        fields = arrays["fields"]
        branch_indices = np.flatnonzero(np.asarray(arrays["branch"], dtype=np.uint8))
        branch_labels = np.asarray(arrays["branch_miss"], dtype=np.uint8)
        macro_pc = np.asarray(arrays["macro_pc"], dtype=np.uint64)
        macro_end = np.asarray(arrays["macro_end"], dtype=np.uint8)
        macro_starts = np.concatenate((
            np.asarray([0], dtype=np.int64),
            np.flatnonzero(macro_end[:-1]).astype(np.int64) + 1,
        ))
        next_macro_slot = np.searchsorted(
            macro_starts, branch_indices, side="right",
        )
        actual_targets = np.zeros(len(branch_indices), dtype=np.uint64)
        has_successor = next_macro_slot < len(macro_starts)
        actual_targets[has_successor] = macro_pc[
            macro_starts[next_macro_slot[has_successor]]
        ]
        counters = np.ones(table_entries, dtype=np.uint8)
        history = 0
        history_mask = (1 << min(16, int(math.log2(table_entries)))) - 1
        targets: Dict[int, int] = {}
        core_direction = 0
        core_target = 0
        core_combined = 0
        for branch_ordinal, index in enumerate(branch_indices):
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
            actual_target = int(actual_targets[branch_ordinal])
            predicted_target = targets.get(pc)
            target_miss = bool(
                taken and (
                    predicted_target is None
                    or actual_target == 0
                    or int(predicted_target) != actual_target
                )
            )
            if taken and actual_target:
                targets[pc] = actual_target
            combined = direction_miss or target_miss
            core_direction += int(direction_miss)
            core_target += int(target_miss)
            core_combined += int(combined)
        core_branches = int(len(branch_indices))
        core_true = int(branch_labels[branch_indices].sum())
        branches += core_branches
        true_misses += core_true
        direction_misses += core_direction
        target_misses += core_target
        combined_misses += core_combined
        per_core.append({
            "core_id": int(core_id),
            "branches": core_branches,
            "true_misses": core_true,
            "direction_only_misses": core_direction,
            "last_target_misses": core_target,
            "combined_misses": core_combined,
        })
    return {
        "name": "functional_gshare_last_target_replay",
        "table_entries": table_entries,
        "branches": branches,
        "true_misses": true_misses,
        "true_rate": _event_rate(true_misses, branches),
        "direction_only_misses": direction_misses,
        "direction_only_rate": _event_rate(direction_misses, branches),
        "last_target_misses": target_misses,
        "combined_misses": combined_misses,
        "combined_rate": _event_rate(combined_misses, branches),
        "per_core": per_core,
        "oracle_labels_consumed_as_input": False,
    }


def _synchronize(engine: Any) -> None:
    device = getattr(engine, "device", None)
    if isinstance(device, torch.device) and device.type == "cuda":
        torch.cuda.synchronize(device)


def _engine_begin(engine: Any, store: V29TraceStore) -> None:
    begin = getattr(engine, "begin_trace", None)
    if callable(begin):
        begin(store)


def _engine_stats(engine: Any) -> Dict[str, Any]:
    function = getattr(engine, "stats", None)
    return dict(function()) if callable(function) else {
        "static_cache_hits": 0,
        "static_cache_misses": 0,
        "static_cache_evictions": 0,
        "static_cache_hit_rate": 0.0,
        "gpu_peak_memory_bytes": 0,
    }


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
        **_engine_stats(engine),
    }


def _functional_prediction_report(
    store: Any,
    engine: Any,
    *,
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
    complete: bool,
    elapsed: float,
    target_stride: int,
    min_step_cycles: float,
    max_step_cycles: float,
    advisory_min_step_violations: int,
    stride_overshoots: int,
    total_no_progress: int,
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
        "truth_available": False,
        "metric_scope": "full_functional_trace" if complete else "consumed_functional_prefix",
        "virtual_time_semantics": "one_shared_clock",
        "initialization_contract": "all_functional_core_streams_active_at_T0",
        "model_context_uses_oracle_timing": False,
        "oracle_timing_usage": "none",
        "predicted_context_used_as_training_label": False,
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
    max_steps: int = 0,
    progress: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Run one trace from cursor zero using one virtual time for every core."""
    target_stride = max(1, int(target_stride))
    min_step_cycles = max(0.0, float(min_step_cycles))
    max_step_cycles = float(max_step_cycles)
    if max_step_cycles <= 0:
        raise ValueError("max_step_cycles must be positive")
    max_no_progress_steps = max(1, int(max_no_progress_steps))
    has_oracle_labels = bool(getattr(store, "has_oracle_labels", True))
    _engine_begin(engine, store)
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
    consecutive_no_progress = 0
    total_no_progress = 0
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
        context = store.context_from_cursors(
            cursors,
            state_time_cycles=global_time,
            include_labels=False,
            last_commit_cycles=last_commit_cycles,
        )
        leaked = [key for key in ORACLE_ONLY_KEYS if key in context]
        if leaked:
            raise RuntimeError(
                f"free-running v29 context leaked oracle keys: {leaked}"
            )
        prediction = engine.predict(store, context)
        slots = [int(value) for value in context["core_slots"].tolist()]
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
        for row, (slot, consumed) in enumerate(zip(slots, consumed_by_row)):
            core_id = int(store.core_ids[slot])
            total_uops = int(store.core_meta[core_id]["n_uops"])
            if consumed > 0:
                prefix = slice(0, consumed)
                branch = context["branch_mask"][row, prefix].numpy().astype(bool)
                macro = context["macro_end"][row, prefix].numpy().astype(bool)
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
        if has_oracle_labels:
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
        if progress is not None:
            progress({
                "phase": "free_running",
                "step": steps,
                "global_time": global_time,
                "delta": delta,
                "retired_uops": sum(retired_uops),
                "total_uops": int(store.meta["n_uops"]),
                "active_cores": len(unfinished_slots),
                "no_progress": step_progress == 0,
            })
    _synchronize(engine)
    elapsed = time.perf_counter() - started
    if not has_oracle_labels:
        return _functional_prediction_report(
            store,
            engine,
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
            complete=complete,
            elapsed=elapsed,
            target_stride=target_stride,
            min_step_cycles=min_step_cycles,
            max_step_cycles=max_step_cycles,
            advisory_min_step_violations=advisory_min_step_violations,
            stride_overshoots=stride_overshoots,
            total_no_progress=total_no_progress,
        )
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
    return {
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
        "cross_core_oracle_head_span_cycles": head_spans.summary(),
        "cumulative_progress_error_uops": progress_errors.summary(),
        "per_core": per_core,
        "branch_replay_baseline": replay_branch_baseline(store),
        "elapsed_s": elapsed,
        "steps_per_s": steps / max(elapsed, 1.0e-12),
        "uops_per_s": total_uops / max(elapsed, 1.0e-12),
        "virtual_time_semantics": "one_shared_clock",
        "initialization_contract": "all_functional_core_streams_active_at_T0",
        "model_context_uses_oracle_timing": False,
        "oracle_timing_usage": "post_transition_metrics_only",
        "metric_scope": "full_roi" if complete else "consumed_functional_prefix",
        "predicted_context_used_as_training_label": False,
        **_engine_stats(engine),
    }


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


def render_text_report(report: Mapping[str, Any]) -> str:
    lines = [
        "TCSim v29 common-time / monotonic-prefix evaluation",
        "====================================================",
        f"traces: {int(report.get('trace_count', 0))}",
        "headline aggregation: workload-equal within each core count",
        "",
    ]
    for core in report.get("by_core_count", []):
        lines.extend([
            f"c{int(core['n_cores'])}: traces={int(core['traces'])} "
            f"workloads={int(core['workloads'])} "
            f"complete={int(core.get('complete_free_running', 0))}/{int(core['traces'])}",
            "  free-running: "
            f"micro-CPI MAPE={_format_percent(core.get('micro_cpi_mape'))} "
            f"macro-CPI MAPE={_format_percent(core.get('macro_cpi_mape'))} "
            f"makespan MAPE={_format_percent(core.get('makespan_mape'))}",
            "  branch: "
            f"count MAPE={_format_percent(core.get('branch_count_mape'))} "
            f"rate abs={_format_number(core.get('branch_rate_abs_error_pp'))} pp",
            "  oracle cursor-interval |offset| cycles: "
            f"p50={_format_number(core.get('oracle_cursor_interval_abs_offset_p50_cycles'))} "
            f"p90={_format_number(core.get('oracle_cursor_interval_abs_offset_p90_cycles'))} "
            f"p99={_format_number(core.get('oracle_cursor_interval_abs_offset_p99_cycles'))} "
            f"slope={_format_number(core.get('oracle_cursor_interval_abs_slope'), 6)} "
            f"head-residual-p99={_format_number(core.get('oracle_head_abs_residual_p99_cycles'))}",
            "  oracle one-step: "
            f"commit log-MAE={_format_number(core.get('oracle_commit_log_mae'), 5)} "
            f"cycle-MAE={_format_number(core.get('oracle_commit_cycle_mae'))} "
            f"branch Brier={_format_number(core.get('oracle_branch_brier'), 5)} "
            f"AUC={_format_number(core.get('oracle_branch_auc'), 4)}",
            "  categories: " + ", ".join(
                f"{item['category']} micro={_format_percent(item.get('micro_cpi_mape'))}"
                for item in core.get("by_category", [])
            ),
            "  workloads:",
        ])
        for workload in core.get("by_workload", []):
            lines.append(
                f"    {workload['workload']}: "
                f"micro={_format_percent(workload.get('micro_cpi_mape'))} "
                f"macro={_format_percent(workload.get('macro_cpi_mape'))} "
                f"branch={_format_number(workload.get('branch_rate_abs_error_pp'))} pp "
                "interval-offset-p99="
                f"{_format_number(workload.get('oracle_cursor_interval_abs_offset_p99_cycles'))}"
            )
        lines.append("")
    lines.extend([
        "Semantics",
        "---------",
        "- one shared virtual clock for every active core",
        "- predicted cursors construct deployment contexts",
        "- oracle commit ticks are used only after transitions for metrics",
        "- branch and macro events are accumulated exactly once from consumed prefixes",
    ])
    return "\n".join(lines) + "\n"


def write_evaluation_report(out_dir: str, report: Mapping[str, Any]) -> Dict[str, str]:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "report.json")
    text_path = os.path.join(out_dir, "report.txt")
    dump_json(json_path, report)
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(render_text_report(report))
    return {"json": json_path, "text": text_path}
