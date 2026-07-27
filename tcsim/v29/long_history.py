"""Long-horizon, prefix-only memory features for v29.

The expensive rolling statistics are materialized as a sidecar at a fixed UOP
stride.  Runtime lookup always selects the latest checkpoint not newer than the
requested cursor, so both oracle training and free rollout obey the same
no-future-information contract.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, Mapping, Sequence, Tuple

import numpy as np


LONG_HISTORY_CONTRACT = "v29-long-history-prefix-sidecar-v1"
LONG_HISTORY_CHECKPOINT_STRIDE = 256
LONG_HISTORY_SCALES = (1024, 4096, 16384, 65536)

_SCALE_FEATURES = (
    "line_unique_frac",
    "page_unique_frac",
    "rare_line_ref_frac",
    "first_touch_line_frac",
    "far_reuse_frac",
    "mem_density",
)
LONG_HISTORY_BASE_FEATURE_NAMES = tuple(
    f"{name}_{scale}"
    for scale in LONG_HISTORY_SCALES
    for name in _SCALE_FEATURES
) + (
    "prefix_mem_refs_log",
    "prefix_unique_lines_log",
    "prefix_unique_pages_log",
    "reuse_gap_p50_log",
    "reuse_gap_p90_log",
    "reuse_gap_p99_log",
    "dtlb_pressure_4096",
    "dtlb_pressure_65536",
)

_AGGREGATE_BASE_NAMES = (
    "line_unique_frac_65536",
    "page_unique_frac_65536",
    "rare_line_ref_frac_65536",
    "mem_density_65536",
)
LONG_HISTORY_AGGREGATE_FEATURE_NAMES = tuple(
    f"active_mean_{name}" for name in _AGGREGATE_BASE_NAMES
) + tuple(
    f"own_to_active_mean_{name}" for name in _AGGREGATE_BASE_NAMES
)
LONG_HISTORY_FEATURE_NAMES = (
    LONG_HISTORY_BASE_FEATURE_NAMES + LONG_HISTORY_AGGREGATE_FEATURE_NAMES
)
LONG_HISTORY_BASE_INDEX = {
    name: index for index, name in enumerate(LONG_HISTORY_BASE_FEATURE_NAMES)
}


def contract_metadata(
    *,
    checkpoint_stride: int = LONG_HISTORY_CHECKPOINT_STRIDE,
) -> Dict[str, Any]:
    return {
        "contract": LONG_HISTORY_CONTRACT,
        "checkpoint_stride_uops": int(checkpoint_stride),
        "scales_memory_refs": list(LONG_HISTORY_SCALES),
        "base_feature_names": list(LONG_HISTORY_BASE_FEATURE_NAMES),
        "feature_names": list(LONG_HISTORY_FEATURE_NAMES),
        "base_dim": len(LONG_HISTORY_BASE_FEATURE_NAMES),
        "output_dim": len(LONG_HISTORY_FEATURE_NAMES),
        "lookup": "latest_checkpoint_not_after_cursor",
        "storage_dtype": "float16",
    }


def _previous_occurrence(values: np.ndarray) -> np.ndarray:
    previous = np.full(len(values), -1, dtype=np.int64)
    latest: Dict[int, int] = {}
    for index, raw in enumerate(values):
        value = int(raw)
        previous[index] = latest.get(value, -1)
        latest[value] = index
    return previous


def _update_rare_reference_count(
    rare_refs: int,
    old_count: int,
    new_count: int,
) -> int:
    if 0 < old_count <= 2:
        rare_refs -= old_count
    if 0 < new_count <= 2:
        rare_refs += new_count
    return rare_refs


def _reuse_quantile(histogram: np.ndarray, quantile: float) -> float:
    total = int(histogram.sum())
    if total <= 0:
        return 0.0
    target = max(1, int(math.ceil(float(quantile) * total)))
    bucket = int(np.searchsorted(np.cumsum(histogram), target, side="left"))
    # The histogram bucket is floor(log2(gap + 1)); normalize consistently
    # with the other logarithmic counters.
    return min(bucket, 31) / 31.0


def build_core_features(
    functional_line: np.ndarray,
    functional_page: np.ndarray,
    *,
    n_uops: int,
    dtlb_entries: int,
    checkpoint_stride: int = LONG_HISTORY_CHECKPOINT_STRIDE,
) -> Tuple[np.ndarray, np.ndarray]:
    """Build strict-prefix features for one core.

    Only UOPs with a valid functional line are memory references.  A checkpoint
    at cursor ``c`` summarizes indices ``[0, c)`` and never consumes UOP ``c``.
    """
    n_uops = int(n_uops)
    checkpoint_stride = int(checkpoint_stride)
    if n_uops < 0 or checkpoint_stride <= 0:
        raise ValueError("invalid long-history UOP extent or checkpoint stride")
    lines_all = np.asarray(functional_line, dtype=np.int64)
    pages_all = np.asarray(functional_page, dtype=np.int64)
    if lines_all.shape != (n_uops,) or pages_all.shape != (n_uops,):
        raise ValueError("long-history source arrays must be one value per UOP")

    mem_uops = np.flatnonzero(lines_all >= 0).astype(np.int64, copy=False)
    lines = lines_all[mem_uops]
    pages = pages_all[mem_uops]
    # A missing page is made line-derived.  This retains stable equality
    # semantics without inventing a globally meaningful virtual address.
    pages = np.where(pages >= 0, pages, lines >> 6).astype(np.int64, copy=False)
    previous_line = _previous_occurrence(lines)
    first_line_prefix = np.concatenate((
        np.zeros(1, dtype=np.int64),
        np.cumsum(previous_line < 0, dtype=np.int64),
    ))
    previous_page = _previous_occurrence(pages)
    first_page_prefix = np.concatenate((
        np.zeros(1, dtype=np.int64),
        np.cumsum(previous_page < 0, dtype=np.int64),
    ))
    del previous_page

    checkpoints = np.arange(
        0, n_uops + 1, checkpoint_stride, dtype=np.int64,
    )
    if not len(checkpoints) or int(checkpoints[-1]) != n_uops:
        checkpoints = np.append(checkpoints, np.int64(n_uops))
    mem_ends = np.searchsorted(mem_uops, checkpoints, side="left")
    features = np.zeros(
        (len(checkpoints), len(LONG_HISTORY_BASE_FEATURE_NAMES)),
        dtype=np.float32,
    )

    reuse_gap = np.where(
        previous_line >= 0,
        np.arange(len(lines), dtype=np.int64) - previous_line,
        np.iinfo(np.int32).max,
    )
    quantile_rows: Dict[int, Tuple[float, float, float]] = {}
    distinct_pages_by_scale: Dict[int, np.ndarray] = {}

    for scale_index, scale in enumerate(LONG_HISTORY_SCALES):
        far_reuse_prefix = np.concatenate((
            np.zeros(1, dtype=np.int64),
            np.cumsum(reuse_gap >= scale, dtype=np.int64),
        ))
        line_counts: Dict[int, int] = defaultdict(int)
        page_counts: Dict[int, int] = defaultdict(int)
        rare_refs = 0
        left = 0
        right = 0
        reuse_hist = np.zeros(32, dtype=np.int64)
        page_distinct = np.zeros(len(checkpoints), dtype=np.float32)
        for row, end_raw in enumerate(mem_ends):
            end = int(end_raw)
            start = max(0, end - int(scale))
            while right < end:
                line = int(lines[right])
                old = int(line_counts[line])
                line_counts[line] = old + 1
                rare_refs = _update_rare_reference_count(
                    rare_refs, old, old + 1,
                )
                page = int(pages[right])
                page_counts[page] += 1
                if scale == 4096:
                    gap = int(reuse_gap[right])
                    bucket = min(31, int(math.log2(gap + 1)))
                    reuse_hist[bucket] += 1
                right += 1
            while left < start:
                line = int(lines[left])
                old = int(line_counts[line])
                rare_refs = _update_rare_reference_count(
                    rare_refs, old, old - 1,
                )
                if old == 1:
                    del line_counts[line]
                else:
                    line_counts[line] = old - 1
                page = int(pages[left])
                page_old = int(page_counts[page])
                if page_old == 1:
                    del page_counts[page]
                else:
                    page_counts[page] = page_old - 1
                if scale == 4096:
                    gap = int(reuse_gap[left])
                    bucket = min(31, int(math.log2(gap + 1)))
                    reuse_hist[bucket] -= 1
                left += 1

            count = end - start
            denominator = max(1, count)
            base = scale_index * len(_SCALE_FEATURES)
            features[row, base + 0] = len(line_counts) / denominator
            features[row, base + 1] = len(page_counts) / denominator
            features[row, base + 2] = rare_refs / denominator
            features[row, base + 3] = (
                int(first_line_prefix[end]) - int(first_line_prefix[start])
            ) / denominator
            features[row, base + 4] = (
                int(far_reuse_prefix[end]) - int(far_reuse_prefix[start])
            ) / denominator
            if count:
                uop_start = int(mem_uops[start])
                uop_end = int(checkpoints[row])
                features[row, base + 5] = count / max(1, uop_end - uop_start)
            page_distinct[row] = float(len(page_counts))
            if scale == 4096:
                quantile_rows[row] = (
                    _reuse_quantile(reuse_hist, 0.50),
                    _reuse_quantile(reuse_hist, 0.90),
                    _reuse_quantile(reuse_hist, 0.99),
                )
        distinct_pages_by_scale[scale] = page_distinct

    extra = len(LONG_HISTORY_SCALES) * len(_SCALE_FEATURES)
    features[:, extra + 0] = np.log1p(mem_ends) / 16.0
    features[:, extra + 1] = np.log1p(first_line_prefix[mem_ends]) / 16.0
    features[:, extra + 2] = np.log1p(first_page_prefix[mem_ends]) / 16.0
    for row in range(len(checkpoints)):
        q50, q90, q99 = quantile_rows.get(row, (0.0, 0.0, 0.0))
        features[row, extra + 3:extra + 6] = (q50, q90, q99)
    capacity = max(1, int(dtlb_entries))
    for offset, scale in enumerate((4096, 65536)):
        pressure = distinct_pages_by_scale[scale] / capacity
        features[:, extra + 6 + offset] = np.log1p(pressure) / 4.0

    if not np.all(np.isfinite(features)):
        raise RuntimeError("non-finite v29 long-history feature")
    return checkpoints, features.astype(np.float16)


def assemble_context_features(base_features: np.ndarray) -> np.ndarray:
    """Append permutation-invariant active-core aggregates."""
    base = np.asarray(base_features, dtype=np.float32)
    expected = len(LONG_HISTORY_BASE_FEATURE_NAMES)
    if base.ndim != 2 or int(base.shape[1]) != expected:
        raise ValueError(
            f"long-history base feature shape {base.shape} != [C,{expected}]"
        )
    selected = np.asarray([
        LONG_HISTORY_BASE_INDEX[name] for name in _AGGREGATE_BASE_NAMES
    ], dtype=np.int64)
    values = base[:, selected]
    means = values.mean(axis=0, keepdims=True)
    repeated_means = np.broadcast_to(means, values.shape)
    ratios = np.clip(values / np.maximum(means, 1.0e-4), 0.0, 8.0) / 8.0
    return np.concatenate((base, repeated_means, ratios), axis=1).astype(
        np.float32, copy=False,
    )


def validate_sidecar_metadata(
    metadata: Mapping[str, Any],
    *,
    trace_id: str,
    core_ids: Sequence[int],
    core_uops: Mapping[int, int],
) -> Dict[str, Any]:
    expected = contract_metadata(
        checkpoint_stride=int(metadata.get("checkpoint_stride_uops", -1)),
    )
    for key in (
        "contract",
        "checkpoint_stride_uops",
        "scales_memory_refs",
        "base_feature_names",
        "feature_names",
        "base_dim",
        "output_dim",
        "lookup",
        "storage_dtype",
    ):
        if metadata.get(key) != expected[key]:
            raise RuntimeError(f"long-history sidecar contract mismatch: {key}")
    if str(metadata.get("trace_id")) != str(trace_id):
        raise RuntimeError("long-history sidecar trace_id mismatch")
    observed_ids = [int(value) for value in metadata.get("core_ids", [])]
    if observed_ids != [int(value) for value in core_ids]:
        raise RuntimeError("long-history sidecar core ordering mismatch")
    observed_uops = {
        int(key): int(value)
        for key, value in dict(metadata.get("core_uops", {})).items()
    }
    if observed_uops != {
        int(key): int(value) for key, value in core_uops.items()
    }:
        raise RuntimeError("long-history sidecar UOP extent mismatch")
    return expected


def lookup_core_features(
    checkpoints: np.ndarray,
    features: np.ndarray,
    cursor: int,
) -> np.ndarray:
    cursor = int(cursor)
    row = int(np.searchsorted(checkpoints, cursor, side="right") - 1)
    if row < 0:
        raise IndexError(f"long-history cursor {cursor} precedes checkpoint zero")
    return np.asarray(features[row], dtype=np.float32)
