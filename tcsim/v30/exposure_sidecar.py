"""Strict loading for functional-only v30 exposure sidecars."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

import numpy as np

from ..utils.io import load_json
from .exposure import (
    EXPOSURE_CAUSAL_FIELDS,
    EXPOSURE_FIELDS,
    EXPOSURE_MAX_LOOKAHEAD,
    EXPOSURE_MAX_PRODUCERS,
    EXPOSURE_SCHEMA_VERSION,
)


EXPOSURE_SIDECAR_SCHEMA = "tcsim-v30-exposure-sidecar-1"


def load_exposure_sidecar(
    root: Optional[str],
    *,
    base_meta: Mapping[str, Any],
    core_ids: Sequence[int],
    core_meta: Mapping[int, Mapping[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Dict[int, Dict[str, Any]]]:
    if root is None:
        return None, {}
    root = os.path.abspath(str(root))
    metadata = load_json(os.path.join(root, "meta.json"))
    if metadata.get("schema_version") != EXPOSURE_SIDECAR_SCHEMA:
        raise RuntimeError("unsupported v30 exposure sidecar schema")
    if metadata.get("feature_schema") != EXPOSURE_SCHEMA_VERSION:
        raise RuntimeError("v30 exposure feature schema mismatch")
    if str(metadata.get("trace_id")) != str(base_meta["trace_id"]):
        raise RuntimeError("v30 exposure sidecar trace mismatch")
    if bool(metadata.get("uses_timing_or_microarchitecture_oracle")):
        raise RuntimeError("v30 exposure sidecar contains forbidden oracle input")
    if int(metadata.get("max_lookahead", -1)) != EXPOSURE_MAX_LOOKAHEAD:
        raise RuntimeError("v30 exposure lookahead contract mismatch")
    if tuple(metadata.get("causal_fields", ())) != EXPOSURE_CAUSAL_FIELDS:
        raise RuntimeError("v30 exposure causal field mismatch")
    if tuple(metadata.get("output_fields", ())) != EXPOSURE_FIELDS:
        raise RuntimeError("v30 exposure output field mismatch")
    if metadata.get("causal_dtype") != "float16":
        raise RuntimeError("v30 exposure causal dtype mismatch")
    if metadata.get("producer_distance_dtype") != "uint32":
        raise RuntimeError("v30 exposure producer-distance dtype mismatch")
    observed_hash = str(metadata.get("contract_hash", ""))
    payload = dict(metadata)
    payload.pop("contract_hash", None)
    expected_hash = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    if observed_hash != expected_hash:
        raise RuntimeError("v30 exposure sidecar contract hash mismatch")

    listed = {int(row["core_id"]): dict(row) for row in metadata.get("cores", [])}
    if sorted(listed) != sorted(int(value) for value in core_ids):
        raise RuntimeError("v30 exposure sidecar core set mismatch")
    arrays: Dict[int, Dict[str, Any]] = {}
    for core_id_value in core_ids:
        core_id = int(core_id_value)
        n_uops = int(core_meta[core_id]["n_uops"])
        if int(listed[core_id].get("n_uops", -1)) != n_uops:
            raise RuntimeError(f"v30 exposure UOP count mismatch core={core_id}")
        core_dir = os.path.join(root, "cores", str(core_id))
        causal = np.load(os.path.join(core_dir, "causal.npy"), mmap_mode="r")
        producer_distance = np.load(
            os.path.join(core_dir, "producer_distance.npy"), mmap_mode="r",
        )
        if causal.dtype != np.float16 or causal.shape != (
            n_uops, len(EXPOSURE_CAUSAL_FIELDS),
        ):
            raise RuntimeError(f"invalid v30 exposure causal array core={core_id}")
        if producer_distance.dtype != np.uint32 or producer_distance.shape != (
            n_uops, EXPOSURE_MAX_PRODUCERS,
        ):
            raise RuntimeError(
                f"invalid v30 exposure producer-distance array core={core_id}"
            )
        arrays[core_id] = {
            "causal": causal,
            "producer_distance": producer_distance,
        }
    contract = {
        key: metadata[key] for key in (
            "schema_version", "feature_schema", "max_lookahead",
            "causal_fields", "window_fields", "output_fields",
            "causal_dtype", "producer_distance_dtype",
            "consumer_features_are_window_local",
            "uses_timing_or_microarchitecture_oracle",
        )
    }
    return contract, arrays
