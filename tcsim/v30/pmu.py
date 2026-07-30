"""Post-rollout cache-miss PMU audit for the v30 cache proxy.

The aligned ``path_class`` column is a gem5 oracle label.  This module is kept
outside the model/dataset hot path and must only be called after a rollout has
finished.  It compares the canonical GSS cache-state counters with the PMU
label contract used by TSim; it never constructs a model input.
"""
from __future__ import annotations

import glob
import math
import os
from typing import Any, Dict, Mapping, Sequence

import numpy as np
import pyarrow.parquet as pq


CACHE_MISS_PMU_AUDIT_SCHEMA = "tcsim-v30-cache-miss-pmu-audit-1"
CACHE_MISS_PMU_LABEL_CONTRACT = "tsim-path-class-thresholds-v1"

_LABEL_COLUMNS = (
    "core_id", "is_load", "is_store", "is_atomic", "path_class",
)


def _column(table: Any, name: str, dtype: Any) -> np.ndarray:
    return table[name].combine_chunks().to_numpy(
        zero_copy_only=False,
    ).astype(dtype, copy=False)


def load_cache_miss_pmu_labels(
    trace_dir: str,
    *,
    expected_core_ids: Sequence[int],
) -> Dict[str, int]:
    """Count full-ROI cache-miss labels without exposing them to the model."""
    paths = sorted(glob.glob(os.path.join(os.path.abspath(trace_dir), "*.aligned.parquet")))
    if not paths:
        raise FileNotFoundError(f"no aligned parquet files below {trace_dir}")
    expected = sorted(int(value) for value in expected_core_ids)
    observed = []
    counts = {
        "load_opportunities": 0,
        "store_opportunities": 0,
        "memory_opportunities": 0,
        "l1d_load_misses": 0,
        "l1d_store_misses": 0,
        "l1d_misses": 0,
        "l2_load_misses": 0,
        "l2_store_misses": 0,
        "l2_misses": 0,
        "llc_misses": 0,
    }
    for path in paths:
        table = pq.read_table(path, columns=list(_LABEL_COLUMNS))
        core_values = np.unique(_column(table, "core_id", np.int64))
        if len(core_values) != 1:
            raise RuntimeError(f"PMU parquet contains multiple core IDs: {path}")
        observed.append(int(core_values[0]))
        load = _column(table, "is_load", np.uint8) > 0
        store_like = (
            (_column(table, "is_store", np.uint8) > 0)
            | (_column(table, "is_atomic", np.uint8) > 0)
        )
        memory = load | store_like
        path_class = _column(table, "path_class", np.int16)
        l1_load = load & (path_class >= 1)
        l1_store = store_like & (path_class >= 1)
        l2_load = load & (path_class >= 2)
        l2_store = store_like & (path_class >= 2)
        counts["load_opportunities"] += int(load.sum())
        counts["store_opportunities"] += int(store_like.sum())
        counts["memory_opportunities"] += int(memory.sum())
        counts["l1d_load_misses"] += int(l1_load.sum())
        counts["l1d_store_misses"] += int(l1_store.sum())
        counts["l2_load_misses"] += int(l2_load.sum())
        counts["l2_store_misses"] += int(l2_store.sum())
        counts["llc_misses"] += int((memory & (path_class >= 4)).sum())
    if sorted(observed) != expected:
        raise RuntimeError(
            f"PMU label core set mismatch: observed={sorted(observed)} expected={expected}"
        )
    counts["l1d_misses"] = (
        counts["l1d_load_misses"] + counts["l1d_store_misses"]
    )
    counts["l2_misses"] = (
        counts["l2_load_misses"] + counts["l2_store_misses"]
    )
    return counts


def _metric(predicted: int, truth: int, opportunities: int) -> Dict[str, Any]:
    predicted = int(predicted)
    truth = int(truth)
    opportunities = int(opportunities)
    predicted_rate = (
        predicted / opportunities if opportunities > 0 else float("nan")
    )
    true_rate = truth / opportunities if opportunities > 0 else float("nan")
    return {
        "predicted_count": predicted,
        "true_count": truth,
        "signed_count_error": predicted - truth,
        "absolute_count_error": abs(predicted - truth),
        "absolute_relative_count_error": abs(predicted - truth) / max(1, truth),
        "opportunities": opportunities,
        "predicted_rate": predicted_rate,
        "true_rate": true_rate,
        "rate_abs_error_pp": (
            abs(predicted_rate - true_rate) * 100.0
            if math.isfinite(predicted_rate) and math.isfinite(true_rate)
            else float("nan")
        ),
    }


def cache_miss_pmu_error_report(
    *,
    trace_dir: str | None,
    expected_core_ids: Sequence[int],
    canonical_state: Mapping[str, Any] | None,
    complete: bool,
) -> Dict[str, Any]:
    """Build a qualified audit or an explicit unavailable record."""
    base: Dict[str, Any] = {
        "schema_version": CACHE_MISS_PMU_AUDIT_SCHEMA,
        "qualified": False,
        "oracle_read_phase": "post_rollout_only",
        "oracle_columns_used_as_model_input": False,
        "label_contract": CACHE_MISS_PMU_LABEL_CONTRACT,
        "path_class_thresholds": {"l1d_miss": 1, "l2_miss": 2, "llc_miss": 4},
        "atomic_accounting": "store_like",
    }
    if not complete:
        return {**base, "status": "unavailable", "reason": "rollout_incomplete"}
    if canonical_state is None:
        return {**base, "status": "unavailable", "reason": "no_gss_canonical_state"}
    if not trace_dir:
        return {**base, "status": "unavailable", "reason": "trace_dir_not_recorded"}
    try:
        truth = load_cache_miss_pmu_labels(
            trace_dir, expected_core_ids=expected_core_ids,
        )
    except Exception as error:  # Preserve inference output, but never hide audit failure.
        return {
            **base,
            "status": "unavailable",
            "reason": "pmu_label_read_failed",
            "detail": f"{type(error).__name__}: {error}",
        }

    predicted = {
        name: int(canonical_state.get(name, 0))
        for name in (
            "l1d_load_misses", "l1d_store_misses", "l1d_misses",
            "l2_load_misses", "l2_store_misses", "l2_misses", "llc_misses",
        )
    }
    opportunity = {
        "l1d_load_misses": truth["load_opportunities"],
        "l1d_store_misses": truth["store_opportunities"],
        "l1d_misses": truth["memory_opportunities"],
        "l2_load_misses": truth["load_opportunities"],
        "l2_store_misses": truth["store_opportunities"],
        "l2_misses": truth["memory_opportunities"],
        "llc_misses": truth["memory_opportunities"],
    }
    metrics = {
        name: _metric(predicted[name], truth[name], opportunity[name])
        for name in predicted
    }
    valid_address_events = int(canonical_state.get("events", 0))
    return {
        **base,
        "qualified": True,
        "status": "ok",
        "trace_dir": os.path.abspath(trace_dir),
        "functional_memory_uops": int(truth["memory_opportunities"]),
        "gss_valid_address_events": valid_address_events,
        "gss_invalid_address_uops": max(
            0, int(truth["memory_opportunities"]) - valid_address_events,
        ),
        "labels": truth,
        "predicted": predicted,
        "metrics": metrics,
    }
