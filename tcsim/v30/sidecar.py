"""Strict loading and slicing for v30 GSS teacher sidecars."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..utils.io import load_json
from .gss import (
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSS_SCHEMA_VERSION,
)


GSS_SIDECAR_SCHEMA = "tcsim-v30-gss-teacher-sidecar-1"


def load_gss_sidecar(
    root: Optional[str],
    *,
    base_meta: Mapping[str, Any],
    core_ids: Sequence[int],
    core_meta: Mapping[int, Mapping[str, Any]],
    allow_ready_clock: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    if root is None:
        return None, {}
    root = os.path.abspath(str(root))
    metadata = load_json(os.path.join(root, "meta.json"))
    if metadata.get("schema_version") != GSS_SIDECAR_SCHEMA:
        raise RuntimeError("unsupported v30 GSS sidecar schema")
    if metadata.get("engine_schema") != GSS_SCHEMA_VERSION:
        raise RuntimeError("v30 GSS engine schema mismatch")
    if str(metadata.get("trace_id")) != str(base_meta["trace_id"]):
        raise RuntimeError("v30 GSS sidecar trace mismatch")
    clock_source = str(metadata.get("clock_source"))
    order_policy = str(metadata.get("order_policy"))
    formal_commit = (
        clock_source == "commit"
        and order_policy == "commit_tick_then_core_then_uop_v1"
    )
    diagnostic_ready = (
        bool(allow_ready_clock)
        and clock_source == "ready"
        and order_policy == "ready_tick_then_core_then_uop_v1"
    )
    if not formal_commit and not diagnostic_ready:
        raise RuntimeError(
            "formal v30 requires commit-clock GSS sidecars; ready-clock "
            "sidecars are accepted only by an explicit oracle diagnostic"
        )
    if not bool(metadata.get("features_are_pre_access")):
        raise RuntimeError("v30 GSS sidecar is not pre-access causal")
    if bool(metadata.get("timestamp_is_model_visible")):
        raise RuntimeError("v30 GSS sidecar exposes teacher timestamps")
    if tuple(metadata.get("categorical_fields", ())) != GSS_CATEGORICAL_FIELDS:
        raise RuntimeError("v30 GSS categorical fields mismatch")
    if tuple(metadata.get("continuous_fields", ())) != GSS_CONTINUOUS_FIELDS:
        raise RuntimeError("v30 GSS continuous fields mismatch")
    if metadata.get("categorical_dtype") != "uint8":
        raise RuntimeError("v30 GSS categorical dtype mismatch")
    if metadata.get("continuous_dtype") != "float16":
        raise RuntimeError("v30 GSS continuous dtype mismatch")
    if float(metadata.get("physical_address_coverage_memory_uops", 0.0)) != 1.0:
        raise RuntimeError("v30 GSS memory physical-address coverage is incomplete")
    observed_hash = str(metadata.get("contract_hash", ""))
    hash_payload = dict(metadata)
    hash_payload.pop("contract_hash", None)
    expected_hash = hashlib.sha256(json.dumps(
        hash_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    if observed_hash != expected_hash:
        raise RuntimeError("v30 GSS sidecar contract hash mismatch")
    listed_cores = {
        int(row["core_id"]): dict(row) for row in metadata.get("cores", [])
    }
    if sorted(listed_cores) != sorted(int(value) for value in core_ids):
        raise RuntimeError("v30 GSS sidecar core set mismatch")
    arrays: Dict[int, Dict[str, Any]] = {}
    for core_id_value in core_ids:
        core_id = int(core_id_value)
        expected_uops = int(core_meta[core_id]["n_uops"])
        declared = listed_cores[core_id]
        if int(declared.get("n_uops", -1)) != expected_uops:
            raise RuntimeError(f"v30 GSS UOP count mismatch core={core_id}")
        core_dir = os.path.join(root, "cores", str(core_id))
        index = np.load(os.path.join(core_dir, "index.npy"), mmap_mode="r")
        categorical = np.load(
            os.path.join(core_dir, "categorical.npy"), mmap_mode="r",
        )
        continuous = np.load(
            os.path.join(core_dir, "continuous.npy"), mmap_mode="r",
        )
        expected_memory = int(declared.get("n_memory_uops", -1))
        if index.dtype != np.uint32 or index.shape != (expected_memory,):
            raise RuntimeError(f"v30 GSS index shape/dtype mismatch core={core_id}")
        if categorical.dtype != np.uint8 or categorical.shape != (
            expected_memory, len(GSS_CATEGORICAL_FIELDS),
        ):
            raise RuntimeError(
                f"v30 GSS categorical shape/dtype mismatch core={core_id}"
            )
        if continuous.dtype != np.float16 or continuous.shape != (
            expected_memory, len(GSS_CONTINUOUS_FIELDS),
        ):
            raise RuntimeError(
                f"v30 GSS continuous shape/dtype mismatch core={core_id}"
            )
        if len(index) > 1 and np.any(index[1:] <= index[:-1]):
            raise RuntimeError(f"v30 GSS indices are not increasing core={core_id}")
        if len(index) and int(index[-1]) >= expected_uops:
            raise RuntimeError(f"v30 GSS index outside UOP stream core={core_id}")
        arrays[core_id] = {
            "index": index,
            "categorical": categorical,
            "continuous": continuous,
        }
    contract = {
        key: metadata[key] for key in (
            "schema_version", "engine_schema", "clock_source", "order_policy",
            "features_are_pre_access", "timestamp_is_model_visible",
            "replacement", "geometry", "categorical_fields",
            "continuous_fields", "categorical_dtype", "continuous_dtype",
        )
    }
    return contract, arrays


def slice_gss_window(
    sidecar: Mapping[str, Any],
    cursor: int,
    end: int,
    K: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    categorical = np.zeros(
        (int(K), len(GSS_CATEGORICAL_FIELDS)), dtype=np.uint8,
    )
    continuous = np.zeros(
        (int(K), len(GSS_CONTINUOUS_FIELDS)), dtype=np.float32,
    )
    memory_mask = np.zeros(int(K), dtype=np.bool_)
    indices = sidecar["index"]
    left = int(np.searchsorted(indices, int(cursor), side="left"))
    right = int(np.searchsorted(indices, int(end), side="left"))
    if right > left:
        positions = np.asarray(indices[left:right], dtype=np.int64) - int(cursor)
        categorical[positions] = np.asarray(
            sidecar["categorical"][left:right], dtype=np.uint8,
        )
        continuous[positions] = np.asarray(
            sidecar["continuous"][left:right], dtype=np.float32,
        )
        memory_mask[positions] = True
    return categorical, continuous, memory_mask
