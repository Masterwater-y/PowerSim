"""Functional-only exposure features for the v30 GSS strength router.

The sidecar stores strict-prefix dependency summaries plus the exact producer
distances already present in the functional trace.  Consumer-facing features
are derived when a K-window is sliced, so they never reveal instructions past
the model's configured functional lookahead.

No timing, cache outcome, MSHR, TLB, coherence, workload, trace, or core ID is
read by this module.
"""
from __future__ import annotations

import math
from typing import Any, Tuple

import numpy as np


EXPOSURE_SCHEMA_VERSION = "tcsim-v30-exposure-functional-1"
EXPOSURE_MAX_LOOKAHEAD = 256
EXPOSURE_MAX_PRODUCERS = 4

EXPOSURE_CAUSAL_FIELDS = (
    "nearest_memory_ancestor_distance_log",
    "dependency_depth_log",
    "memory_chain_depth_log",
    "memory_ancestors_32_log",
    "memory_ancestors_128_log",
    "independent_memory_32_log",
    "independent_memory_128_log",
)

EXPOSURE_WINDOW_FIELDS = (
    "first_consumer_distance_log",
    "direct_consumer_fanout_log",
    "downstream_dependency_depth_log",
    "dependent_span_log",
    "next_serialize_distance_log",
)

EXPOSURE_FIELDS = EXPOSURE_CAUSAL_FIELDS + EXPOSURE_WINDOW_FIELDS


def _log_normalize(value: int, maximum: int) -> float:
    return min(
        1.0,
        math.log1p(max(0, int(value))) / math.log1p(max(1, int(maximum))),
    )


def build_causal_exposure(
    producer_distances: Any,
    memory_mask: Any,
    *,
    max_lookahead: int = EXPOSURE_MAX_LOOKAHEAD,
) -> np.ndarray:
    """Build strict-prefix exposure summaries for one program-order stream."""
    distances = np.asarray(producer_distances, dtype=np.uint32)
    memory = np.asarray(memory_mask, dtype=np.bool_)
    if distances.ndim != 2 or distances.shape[1] != EXPOSURE_MAX_PRODUCERS:
        raise ValueError("exposure producer distances must have shape [N,4]")
    if memory.shape != (len(distances),):
        raise ValueError("exposure memory mask length mismatch")
    horizon = int(max_lookahead)
    if horizon <= 0 or horizon > 256:
        raise ValueError("exposure max lookahead must be in [1,256]")

    count = len(distances)
    output = np.zeros((count, len(EXPOSURE_CAUSAL_FIELDS)), dtype=np.float32)
    depth = np.zeros(count, dtype=np.uint16)
    memory_depth = np.zeros(count, dtype=np.uint16)
    nearest_memory = np.full(count, -1, dtype=np.int64)
    ancestor_masks = [0] * count
    mask_limit = (1 << horizon) - 1
    recent_memory_mask = 0

    for index in range(count):
        if index > 0:
            recent_memory_mask = (
                (recent_memory_mask << 1) | int(memory[index - 1])
            ) & mask_limit
        parent_indices = []
        seen_parents = set()
        ancestor_mask = 0
        for raw_distance in distances[index]:
            distance = int(raw_distance)
            if distance <= 0:
                continue
            if distance > index:
                raise RuntimeError(
                    "functional producer distance points before trace start: "
                    f"uop={index} distance={distance}"
                )
            parent = index - distance
            if parent in seen_parents:
                continue
            seen_parents.add(parent)
            parent_indices.append(parent)
            if distance <= horizon:
                ancestor_mask |= 1 << (distance - 1)
                ancestor_mask |= ancestor_masks[parent] << distance
        ancestor_mask &= mask_limit
        ancestor_masks[index] = ancestor_mask

        if parent_indices:
            dependency_depth = 1 + max(int(depth[p]) for p in parent_indices)
            prior_memory_depth = max(
                int(memory_depth[p]) for p in parent_indices
            )
            candidates = [
                p if memory[p] else int(nearest_memory[p])
                for p in parent_indices
            ]
            nearest = max(candidates)
        else:
            dependency_depth = 0
            prior_memory_depth = 0
            nearest = -1
        depth[index] = min(horizon, dependency_depth)
        memory_depth[index] = min(
            horizon, prior_memory_depth + int(memory[index])
        )
        nearest_memory[index] = nearest

        memory_ancestors = ancestor_mask & recent_memory_mask
        recent_32 = recent_memory_mask & ((1 << min(32, horizon)) - 1)
        ancestors_32 = memory_ancestors & ((1 << min(32, horizon)) - 1)
        recent_128 = recent_memory_mask & ((1 << min(128, horizon)) - 1)
        ancestors_128 = memory_ancestors & ((1 << min(128, horizon)) - 1)
        ancestor_count_32 = ancestors_32.bit_count()
        ancestor_count_128 = ancestors_128.bit_count()
        independent_count_32 = max(
            0, recent_32.bit_count() - ancestor_count_32
        )
        independent_count_128 = max(
            0, recent_128.bit_count() - ancestor_count_128
        )
        nearest_distance = index - nearest if nearest >= 0 else 0
        output[index] = (
            _log_normalize(nearest_distance, horizon),
            _log_normalize(int(depth[index]), horizon),
            _log_normalize(int(memory_depth[index]), horizon),
            _log_normalize(ancestor_count_32, 32),
            _log_normalize(ancestor_count_128, 128),
            _log_normalize(independent_count_32, 32),
            _log_normalize(independent_count_128, 128),
        )
    return output


def slice_exposure_window(
    sidecar: Any,
    access: Any,
    semantic_flags: Any,
    cursor: int,
    end: int,
    K: int,
) -> np.ndarray:
    """Materialize causal and window-local exposure features for one K-window."""
    cursor = int(cursor)
    end = int(end)
    K = int(K)
    count = max(0, end - cursor)
    if K != EXPOSURE_MAX_LOOKAHEAD:
        raise ValueError(
            f"exposure-v1 requires K={EXPOSURE_MAX_LOOKAHEAD}, got {K}"
        )
    output = np.zeros((K, len(EXPOSURE_FIELDS)), dtype=np.float32)
    if count == 0:
        return output
    causal = np.asarray(sidecar["causal"][cursor:end], dtype=np.float32)
    distances = np.asarray(
        sidecar["producer_distance"][cursor:end], dtype=np.uint32,
    )
    memory = np.asarray(access[cursor:end], dtype=np.uint8) > 0
    serialize = (
        np.asarray(semantic_flags[cursor:end], dtype=np.uint8) & (1 << 7)
    ) != 0
    if causal.shape != (count, len(EXPOSURE_CAUSAL_FIELDS)):
        raise RuntimeError("exposure causal window shape mismatch")
    output[:count, :len(EXPOSURE_CAUSAL_FIELDS)] = causal

    parents: list[list[int]] = [[] for _ in range(count)]
    first_consumer = np.zeros(count, dtype=np.uint16)
    fanout = np.zeros(count, dtype=np.uint16)
    for consumer in range(count):
        seen_producers = set()
        for raw_distance in distances[consumer]:
            distance = int(raw_distance)
            if distance <= 0 or distance > consumer:
                continue
            producer = consumer - distance
            if producer in seen_producers:
                continue
            seen_producers.add(producer)
            parents[consumer].append(producer)
            fanout[producer] = min(65535, int(fanout[producer]) + 1)
            if first_consumer[producer] == 0:
                first_consumer[producer] = distance

    downstream_depth = np.zeros(count, dtype=np.uint16)
    furthest_descendant = np.arange(count, dtype=np.int64)
    for consumer in range(count - 1, -1, -1):
        for producer in parents[consumer]:
            downstream_depth[producer] = min(
                K,
                max(
                    int(downstream_depth[producer]),
                    1 + int(downstream_depth[consumer]),
                ),
            )
            furthest_descendant[producer] = max(
                int(furthest_descendant[producer]),
                int(furthest_descendant[consumer]),
            )

    next_serialize = np.zeros(count, dtype=np.uint16)
    next_position = -1
    for index in range(count - 1, -1, -1):
        if next_position >= 0:
            next_serialize[index] = min(K, next_position - index)
        if serialize[index]:
            next_position = index

    offset = len(EXPOSURE_CAUSAL_FIELDS)
    for index in np.flatnonzero(memory):
        span = max(0, int(furthest_descendant[index]) - int(index))
        output[index, offset:] = (
            _log_normalize(int(first_consumer[index]), K),
            _log_normalize(int(fanout[index]), K),
            _log_normalize(int(downstream_depth[index]), K),
            _log_normalize(span, K),
            _log_normalize(int(next_serialize[index]), K),
        )
    return output


def exposure_distribution_summary(values: np.ndarray) -> Tuple[float, float, float]:
    """Return compact finite/nonzero/max diagnostics for build metadata."""
    array = np.asarray(values, dtype=np.float32)
    return (
        float(np.isfinite(array).mean()) if array.size else 1.0,
        float(np.count_nonzero(array) / array.size) if array.size else 0.0,
        float(np.max(array)) if array.size else 0.0,
    )
