"""Versioned configured-replay inputs for the v30 B1--B3 experiments.

The event stream is compact (one row per retired branch).  Prefix history is
materialized once per UOP so a random K=256 training window never cold-starts
the predictor or scans from the beginning of the trace.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence, Tuple

import numpy as np

from ..branch_replay import (
    BranchType,
    ReplayConfig,
    TournamentBPUReplay,
    events_from_cache_arrays,
)


BRANCH_FEATURE_CONTRACT = "v30-configured-branch-input-v1"
BRANCH_FEATURE_IMPLEMENTATION = "tournament-correct-path-prefix-v2"
BRANCH_EVENT_NAMES = (
    "replay_full_miss",
    "replay_direction_miss",
    "replay_target_miss",
    "replay_cold_state",
)
BRANCH_HISTORY_NAMES = (
    "uops_since_previous_replay_miss_bucket",
    "branches_since_previous_replay_miss_bucket",
    "replay_misses_last_16_branches",
    "replay_misses_last_64_branches",
    "previous_replay_miss_kind",
)
BRANCH_EVENT_CARDINALITIES = (2, 2, 2, 2)
BRANCH_HISTORY_CARDINALITIES = (17, 17, 17, 65, 8)
BRANCH_COLD_PREFIX = 4096
BRANCH_DISTANCE_MAX_BUCKET = 16
BRANCH_EVENT_FILE = "event.npy"
BRANCH_HISTORY_FILE = "history.npy"
BRANCH_INDEX_FILE = "branch_index.npy"


_BRANCH_KIND_CODE = {
    BranchType.RETURN.value: 1,
    BranchType.CALL_DIRECT.value: 2,
    BranchType.CALL_INDIRECT.value: 3,
    BranchType.DIRECT_COND.value: 4,
    BranchType.DIRECT_UNCOND.value: 5,
    BranchType.INDIRECT_COND.value: 6,
    BranchType.INDIRECT_UNCOND.value: 7,
}


def contract_metadata(
    replay_config: ReplayConfig,
    *,
    cold_prefix_branches: int = BRANCH_COLD_PREFIX,
) -> dict[str, Any]:
    """Return the trace-independent part of the sidecar contract."""
    replay_config.validate()
    cold = int(cold_prefix_branches)
    if cold < 0:
        raise ValueError("cold_prefix_branches must be non-negative")
    return {
        "branch_feature_contract": BRANCH_FEATURE_CONTRACT,
        "branch_feature_implementation": BRANCH_FEATURE_IMPLEMENTATION,
        "predictor_family": replay_config.direction_family,
        "predictor_config_hash": replay_config.stable_hash(),
        "cold_prefix_branches_per_core": cold,
        "event_names": list(BRANCH_EVENT_NAMES),
        "event_cardinalities": list(BRANCH_EVENT_CARDINALITIES),
        "event_dtype": "uint8",
        "event_storage": "compact-per-branch",
        "event_index_dtype": "uint32",
        "event_index_order": "strictly-increasing-uop-index",
        "history_names": list(BRANCH_HISTORY_NAMES),
        "history_cardinalities": list(BRANCH_HISTORY_CARDINALITIES),
        "history_dtype": "uint8",
        "history_storage": "dense-per-uop-strict-prefix",
        "distance_bucket": {
            "none": 0,
            "positive": "min(16, floor(log2(distance)) + 1)",
        },
        "branch_kind_codes": dict(_BRANCH_KIND_CODE),
        "functional_order": "per-core-committed-uop-order",
        "current_event_order": "predict-before-update",
    }


def _distance_bucket(values: np.ndarray, valid: np.ndarray) -> np.ndarray:
    output = np.zeros(values.shape, dtype=np.uint8)
    if np.any(valid):
        selected = np.maximum(values[valid], 1)
        buckets = np.floor(np.log2(selected.astype(np.float64))).astype(np.int64) + 1
        output[valid] = np.minimum(
            buckets, BRANCH_DISTANCE_MAX_BUCKET,
        ).astype(np.uint8)
    return output


def _build_prefix_history(
    n_uops: int,
    branch_indices: np.ndarray,
    miss_mask: np.ndarray,
    branch_kind: np.ndarray,
    *,
    chunk_size: int = 1 << 20,
) -> np.ndarray:
    """Materialize strict-prefix history without a Python loop over UOPs."""
    n_uops = int(n_uops)
    if n_uops < 0:
        raise ValueError("n_uops must be non-negative")
    history = np.zeros((n_uops, len(BRANCH_HISTORY_NAMES)), dtype=np.uint8)
    miss_ordinals = np.flatnonzero(miss_mask).astype(np.int64, copy=False)
    miss_uops = branch_indices[miss_ordinals]
    miss_kinds = branch_kind[miss_ordinals]
    for begin in range(0, n_uops, max(1, int(chunk_size))):
        end = min(n_uops, begin + max(1, int(chunk_size)))
        uops = np.arange(begin, end, dtype=np.int64)
        # side='left' is the critical strict-prefix rule: the current branch
        # event is not visible in its own history fields.
        branch_prefix = np.searchsorted(branch_indices, uops, side="left")
        miss_prefix = np.searchsorted(miss_uops, uops, side="left")
        has_previous = miss_prefix > 0
        previous_position = np.maximum(miss_prefix - 1, 0)

        uop_distance = np.zeros(len(uops), dtype=np.int64)
        if len(miss_uops) and np.any(has_previous):
            uop_distance[has_previous] = (
                uops[has_previous] - miss_uops[previous_position[has_previous]]
            )
        history[begin:end, 0] = _distance_bucket(uop_distance, has_previous)

        branch_distance = np.zeros(len(uops), dtype=np.int64)
        if len(miss_ordinals) and np.any(has_previous):
            branch_distance[has_previous] = branch_prefix[has_previous] - (
                miss_ordinals[previous_position[has_previous]]
            )
        history[begin:end, 1] = _distance_bucket(
            branch_distance, has_previous & (branch_distance > 0),
        )

        if len(miss_ordinals):
            upper = np.searchsorted(miss_ordinals, branch_prefix, side="left")
            lower16 = np.searchsorted(
                miss_ordinals, np.maximum(branch_prefix - 16, 0), side="left",
            )
            lower64 = np.searchsorted(
                miss_ordinals, np.maximum(branch_prefix - 64, 0), side="left",
            )
            history[begin:end, 2] = (upper - lower16).astype(np.uint8)
            history[begin:end, 3] = (upper - lower64).astype(np.uint8)
            if np.any(has_previous):
                history[begin:end, 4][has_previous] = miss_kinds[
                    previous_position[has_previous]
                ]
    return history


def build_core_features(
    arrays: Mapping[str, Any],
    replay_config: ReplayConfig,
    *,
    n_uops: int,
    cold_prefix_branches: int = BRANCH_COLD_PREFIX,
) -> Tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Replay one core and return compact events plus dense prefix history."""
    branch_indices = np.asarray(
        arrays["replay_branch_index"], dtype=np.int64,
    )
    return build_core_features_from_events(
        branch_indices,
        events_from_cache_arrays(arrays),
        replay_config,
        n_uops=n_uops,
        cold_prefix_branches=cold_prefix_branches,
    )


def build_core_features_from_events(
    branch_indices: Sequence[int],
    events: Iterable[Any],
    replay_config: ReplayConfig,
    *,
    n_uops: int,
    cold_prefix_branches: int = BRANCH_COLD_PREFIX,
) -> Tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Replay already aligned functional events for a legacy base cache."""
    replay_config.validate()
    branch_indices = np.asarray(branch_indices, dtype=np.int64)
    events = list(events)
    if len(events) != len(branch_indices):
        raise RuntimeError("branch feature replay event/index count mismatch")
    if len(branch_indices):
        if int(branch_indices[0]) < 0 or int(branch_indices[-1]) >= int(n_uops):
            raise RuntimeError("branch feature index is outside the UOP stream")
        if len(branch_indices) > 1 and np.any(
            branch_indices[1:] <= branch_indices[:-1]
        ):
            raise RuntimeError("branch feature indices are not strictly increasing")

    replay = TournamentBPUReplay(replay_config)
    event_features = np.zeros(
        (len(events), len(BRANCH_EVENT_NAMES)), dtype=np.uint8,
    )
    branch_kind = np.zeros(len(events), dtype=np.uint8)
    cold = int(cold_prefix_branches)
    for ordinal, event in enumerate(events):
        prediction = replay.process(event)
        event_features[ordinal] = (
            int(prediction.full_miss),
            int(prediction.direction_miss),
            int(prediction.target_miss),
            int(ordinal < cold),
        )
        branch_kind[ordinal] = _BRANCH_KIND_CODE[prediction.branch_type]

    history = _build_prefix_history(
        int(n_uops),
        branch_indices,
        event_features[:, 0].astype(np.bool_, copy=False),
        branch_kind,
    )
    report = replay.report()
    if int(report["branches"]) != len(events):
        raise RuntimeError("branch feature replay report count mismatch")
    return event_features, history, {
        "branches": len(events),
        "replayed_misses": int(event_features[:, 0].sum()),
        "functional_history_checks": int(report["functional_history_checks"]),
        "functional_history_mismatches": int(
            report["functional_history_mismatches"]
        ),
    }


def validate_sidecar_metadata(
    metadata: Mapping[str, Any],
    *,
    base_meta: Mapping[str, Any],
    core_ids: Sequence[int],
    core_uops: Mapping[int, int],
) -> dict[str, Any]:
    """Validate and return the trace-independent checkpoint contract."""
    replay_config = ReplayConfig.from_mapping(base_meta)
    expected = contract_metadata(
        replay_config,
        cold_prefix_branches=int(
            metadata.get("cold_prefix_branches_per_core", BRANCH_COLD_PREFIX)
        ),
    )
    for key, value in expected.items():
        if metadata.get(key) != value:
            raise RuntimeError(
                f"branch feature sidecar contract mismatch {key}: "
                f"{metadata.get(key)!r} != {value!r}"
            )
    if str(metadata.get("trace_id")) != str(base_meta["trace_id"]):
        raise RuntimeError("branch feature sidecar trace_id mismatch")
    if [int(value) for value in metadata.get("core_ids", [])] != [
        int(value) for value in core_ids
    ]:
        raise RuntimeError("branch feature sidecar core_ids mismatch")
    expected_uops = {str(int(key)): int(value) for key, value in core_uops.items()}
    if dict(metadata.get("core_uops", {})) != expected_uops:
        raise RuntimeError("branch feature sidecar core_uops mismatch")
    base_contract = {
        key: base_meta.get(key)
        for key in (
            "raw_trace_schema",
            "dataset_schema",
            "feature_schema",
            "model_input_contract",
            "predictor_hash",
            "resource_decoder_hash",
            "functional_branch_replay_contract",
        )
    }
    if dict(metadata.get("base_contract", {})) != base_contract:
        raise RuntimeError("branch feature sidecar/base cache contract mismatch")
    quality = dict(metadata.get("quality", {}) or {})
    if quality.get("status") != "pass" or not bool(quality.get("strict_prefix")):
        raise RuntimeError("branch feature sidecar quality is not pass")
    return expected
