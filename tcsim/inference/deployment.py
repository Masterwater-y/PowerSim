"""Free-running deployment inference over fixed-K packed traces.

Unlike the training/eval rollout cache, this module never consumes the
oracle-selected ``rollout.jsonl`` context.  It reads only packed functional
chunks and post-hoc labels, constructs contexts from predicted scheduler
state, and latches each newly loaded chunk prediction exactly once.
"""
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
import glob
import hashlib
import json
import math
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import torch

from ..chunker.functional_features import (
    BRANCH_CONTRACT_VERSION,
    FEATURE_SCHEMA_VERSION,
    FIELD_NAMES,
    PACKED_SCHEMA_VERSION,
    RESOURCE_KEY_NAMES,
    CHUNK_SUMMARY_NAMES,
    feature_contract_metadata,
)
from ..dataset.torch_dataset import context_features
from ..model.tcsim_model import StaticEmbeddingCache, TCSimModel
from ..utils.config import TCSimConfig
from ..utils.io import dump_json, load_json


def _pctl(values: Sequence[float], q: float) -> float:
    clean = sorted(float(x) for x in values if math.isfinite(float(x)))
    if not clean:
        return float("nan")
    position = min(1.0, max(0.0, float(q))) * (len(clean) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return clean[lower]
    fraction = position - lower
    return clean[lower] * (1.0 - fraction) + clean[upper] * fraction


def _mean(values: Sequence[float]) -> float:
    clean = [float(x) for x in values if math.isfinite(float(x))]
    return sum(clean) / len(clean) if clean else float("nan")


def _relerr(pred: float, true: float) -> float:
    return abs(float(pred) - float(true)) / max(1.0, abs(float(true)))


def _branch_opportunities(chunk: Dict[str, Any]) -> int:
    """Return the denominator matching the cache's branch-label contract."""
    return int(chunk.get(
        "n_branch_opportunities",
        chunk.get("n_branch", chunk.get("n_cond_branch", 0)),
    ))


def _torch_load(path: str, map_location: str = "cpu") -> Any:
    try:
        return torch.load(path, map_location=map_location, weights_only=True)
    except TypeError:  # older torch
        return torch.load(path, map_location=map_location)


def _config_from_dict(data: Dict[str, Any]) -> TCSimConfig:
    return TCSimConfig(
        chunk=dict(data.get("chunk", {})),
        scheduler=dict(data.get("scheduler", {})),
        uarch=dict(data.get("uarch", {})),
        model=dict(data.get("model", {})),
        train=dict(data.get("train", {})),
    )


def _build_model(cfg: TCSimConfig, sdpa_backend: Optional[str] = None) -> TCSimModel:
    return TCSimModel(
        d_field=int(cfg.model.get("d_field", 16)),
        d_dynamic_field=int(cfg.model.get("d_dynamic_field", 16)),
        d_static=int(cfg.model.get("d_static", 128)),
        d_dyn=int(cfg.model.get("d_dyn", 128)),
        n_heads=int(cfg.model.get("n_dyn_heads", 4)),
        n_layers=int(cfg.model.get("n_dyn_layers", 1)),
        ffn_dim=(
            int(cfg.model["ffn_dim"])
            if cfg.model.get("ffn_dim") is not None else None
        ),
        cross_target_block=int(cfg.model.get("cross_target_block", 0)),
        sdpa_backend=str(sdpa_backend or cfg.model.get("sdpa_backend", "auto")),
        dropout=float(cfg.model.get("dropout", 0.1)),
        # Training deliberately allocated a small tail margin.  Recreate that
        # exact parameter shape before loading the checkpoint.
        max_K=int(cfg.chunk.get("K", 256)) + 32,
    )


def load_checkpoint_model(
    checkpoint_path: str,
    *,
    device: str = "cuda",
    config_path: Optional[str] = None,
    sdpa_backend: Optional[str] = None,
) -> Tuple[TCSimModel, TCSimConfig, Dict[str, Any]]:
    """Load the exact training architecture and return inference metadata."""
    if not os.path.isfile(checkpoint_path):
        raise FileNotFoundError(checkpoint_path)
    target = torch.device(device)
    if target.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA was requested but torch.cuda.is_available() is false")

    payload = _torch_load(checkpoint_path, map_location="cpu")
    if isinstance(payload, dict) and "model" in payload:
        state_dict = payload["model"]
        cfg_data = payload.get("config")
        step = int(payload.get("step", 0) or 0)
        best_val = float(payload.get("best_val", float("nan")))
        contracts = payload.get("contracts")
    else:
        state_dict = payload
        cfg_data = None
        step = 0
        best_val = float("nan")
        contracts = None

    if not isinstance(contracts, dict):
        raise RuntimeError(
            "checkpoint lacks v28.1 feature contracts; rebuild cache and retrain"
        )
    predictor_hash = str(contracts.get("predictor_hash", ""))
    if not predictor_hash:
        raise RuntimeError("checkpoint lacks branch predictor hash; retrain")
    expected_contracts = feature_contract_metadata(predictor_hash)
    for key in (
        "packed_schema", "model_input_contract", "feature_schema",
        "branch_contract", "predictor_hash", "dimensions",
    ):
        if contracts.get(key) != expected_contracts.get(key):
            raise RuntimeError(
                f"checkpoint contract mismatch for {key}: "
                f"{contracts.get(key)!r} != {expected_contracts.get(key)!r}"
            )

    if config_path:
        cfg = TCSimConfig.load(config_path)
    elif isinstance(cfg_data, dict):
        cfg = _config_from_dict(cfg_data)
    else:
        raise RuntimeError(
            "checkpoint has no embedded config; pass --config to recreate the model"
        )

    model = _build_model(cfg, sdpa_backend=sdpa_backend)
    model.load_state_dict(state_dict, strict=True)
    # Drop optimizer/history tensors from the 1.2 GiB training checkpoint as
    # soon as the model parameters have been copied.
    del payload, state_dict
    model.to(target)
    model.eval()
    if target.type == "cuda":
        torch.set_float32_matmul_precision("high")

    stat = os.stat(checkpoint_path)
    checkpoint_id = f"{os.path.abspath(checkpoint_path)}:{stat.st_size}:{stat.st_mtime_ns}"
    return model, cfg, {
        "checkpoint": os.path.abspath(checkpoint_path),
        "checkpoint_id": checkpoint_id,
        "step": step,
        "best_val": best_val,
        "device": str(target),
        "sdpa_backend": str(sdpa_backend or cfg.model.get("sdpa_backend", "auto")),
        "contracts": contracts,
        "predictor_hash": predictor_hash,
    }


class PackedTrace:
    """Lazy mmap view of one packed functional trace.

    ``rollout.jsonl`` is intentionally ignored.  It was selected using true
    commit ticks and is not a valid deployment-side context source.
    """

    REQUIRED_ARRAYS = (
        "fields", "mask", "summary", "lines", "access", "resource", "scalar",
    )

    def __init__(
        self,
        rollout_dir: str,
        *,
        source: Optional[Dict[str, Any]] = None,
        max_chunks_per_core: int = 0,
    ) -> None:
        self.rollout_dir = os.path.abspath(rollout_dir)
        self.meta = load_json(os.path.join(self.rollout_dir, "meta.json"))
        packed_meta = self.meta.get("packed")
        if not isinstance(packed_meta, dict):
            raise RuntimeError(f"packed cache metadata missing: {self.rollout_dir}")
        if self.meta.get("feature_schema") != FEATURE_SCHEMA_VERSION:
            raise RuntimeError(
                f"stale rollout feature schema in {self.rollout_dir}; rebuild tensor cache"
            )
        if packed_meta.get("schema_version") != PACKED_SCHEMA_VERSION:
            raise RuntimeError(
                f"stale packed schema in {self.rollout_dir}; rebuild tensor cache"
            )
        if packed_meta.get("branch_contract") != BRANCH_CONTRACT_VERSION:
            raise RuntimeError(
                f"branch contract mismatch in {self.rollout_dir}; rebuild tensor cache"
            )
        packed_dir = os.path.join(
            self.rollout_dir, str(packed_meta.get("relative_dir", "packed"))
        )
        self.arrays = {
            name: np.load(os.path.join(packed_dir, f"{name}.npy"), mmap_mode="r")
            for name in self.REQUIRED_ARRAYS
        }
        if self.arrays["scalar"].ndim != 2 or self.arrays["scalar"].shape[1] != 15:
            raise RuntimeError(
                f"stale packed cache without branch labels: {self.rollout_dir}"
            )
        if (
            int(self.arrays["fields"].shape[-1]) != len(FIELD_NAMES)
            or int(self.arrays["summary"].shape[-1]) != len(CHUNK_SUMMARY_NAMES)
            or int(self.arrays["resource"].shape[-1]) != len(RESOURCE_KEY_NAMES)
        ):
            raise RuntimeError(f"packed v28.1 tensor dimensions mismatch: {self.rollout_dir}")
        self.trace_id = str(self.meta["trace_id"])
        self.K = int(packed_meta["K"])
        self.branch_contract = str(
            packed_meta.get("branch_opportunity_kind", "legacy_conditional_branches")
        )
        self.uarch_features = [float(x) for x in self.meta.get("uarch_features", [])]
        self.uarch_hash = str(self.meta.get("uarch_hash", ""))
        self.predictor_hash = str(self.meta.get("predictor_hash", ""))
        if not self.predictor_hash:
            raise RuntimeError(f"predictor hash missing in {self.rollout_dir}")
        self.source = dict(source or {})
        self.workload = str(self.source.get("workload") or os.path.basename(self.rollout_dir))
        self.seed = self.source.get("seed")
        self.core_offsets = {
            int(key): int(value) for key, value in packed_meta["core_offsets"].items()
        }
        raw_counts = {
            int(key): int(value) for key, value in packed_meta["core_counts"].items()
        }
        limit = int(max_chunks_per_core)
        self.core_counts = {
            core: min(count, limit) if limit > 0 else count
            for core, count in raw_counts.items()
        }
        self.core_ids = sorted(self.core_counts)
        if not self.core_ids:
            raise RuntimeError(f"packed trace has no cores: {self.rollout_dir}")
        if int(self.arrays["fields"].shape[1]) != self.K:
            raise RuntimeError(f"packed K mismatch in {self.rollout_dir}")

    @property
    def total_chunks(self) -> int:
        return sum(self.core_counts.values())

    def get_chunk(self, core_id: int, chunk_id: int) -> Dict[str, Any]:
        core = int(core_id)
        chunk = int(chunk_id)
        if core not in self.core_counts or not 0 <= chunk < self.core_counts[core]:
            raise IndexError((core, chunk))
        index = self.core_offsets[core] + chunk
        scalar = self.arrays["scalar"][index]
        n_uops = int(scalar[0])
        mask = self.arrays["mask"][index]
        lines = self.arrays["lines"][index]
        access = self.arrays["access"][index]
        valid = mask.astype(bool, copy=False)
        read_mask = valid & (lines >= 0) & ((access == 1) | (access == 3))
        write_mask = valid & (lines >= 0) & ((access == 2) | (access == 3))
        valid_label = bool(float(scalar[11]) > 0.5 and float(scalar[9]) > 0.0)
        new_branch_contract = len(scalar) >= 15
        branch_opportunities = int(scalar[12])
        n_cond_branch = int(scalar[13]) if new_branch_contract else int(scalar[12])
        n_branch_miss = int(scalar[14]) if new_branch_contract else int(scalar[13])
        return {
            "trace_id": self.trace_id,
            "core_id": core,
            "chunk_id": chunk,
            "n_uops": n_uops,
            "n_load": int(scalar[1]),
            "n_store": int(scalar[2]),
            "n_atomic": int(scalar[3]),
            "n_branch": int(scalar[4]),
            "n_int": int(scalar[5]),
            "n_fp": int(scalar[6]),
            "n_simd": int(scalar[7]),
            "n_serialize": int(scalar[8]),
            "n_branch_opportunities": branch_opportunities,
            "n_cond_branch": n_cond_branch,
            "n_branch_miss": n_branch_miss,
            "has_atomic": int(scalar[3]) > 0,
            "has_serialize": int(scalar[8]) > 0,
            "per_uop_fields": self.arrays["fields"][index],
            "valid_uop_mask": mask,
            "chunk_summary": self.arrays["summary"][index],
            "per_uop_lines": lines,
            "per_uop_access": access,
            "per_uop_resource_keys": self.arrays["resource"][index],
            "read_lines": np.unique(lines[read_mask]).tolist(),
            "write_lines": np.unique(lines[write_mask]).tolist(),
            "uarch_features": self.uarch_features,
            "valid_label": valid_label,
            "true_delta_cycles": float(scalar[9]) if valid_label else None,
            "true_cpi": float(scalar[10]) if valid_label else None,
        }


@dataclass
class ContextPrediction:
    delta_cycles: List[float]
    branch_miss_prob: List[float]


class TruthContextPredictor:
    """Label-only predictor used solely for an oracle scheduler baseline."""

    n_forwards = 0

    def predict(
        self, trace: PackedTrace, chunks: Sequence[Dict[str, Any]]
    ) -> ContextPrediction:
        delta: List[float] = []
        branch: List[float] = []
        for chunk in chunks:
            value = chunk.get("true_delta_cycles")
            delta.append(float(value) if value is not None else float(chunk["n_uops"]))
            opportunities = _branch_opportunities(chunk)
            branch.append(
                float(chunk.get("n_branch_miss", 0)) / opportunities
                if opportunities > 0 else 0.0
            )
        return ContextPrediction(delta, branch)


class ModelContextPredictor:
    """Full-context model adapter with correctness-preserving static caching."""

    def __init__(
        self,
        model: TCSimModel,
        *,
        device: str,
        checkpoint_id: str,
        predictor_hash: str = "",
        amp_dtype: str = "bf16",
        static_cache_entries: int = 256,
    ) -> None:
        self.model = model
        self.device = torch.device(device)
        self.checkpoint_id = str(checkpoint_id)
        self.predictor_hash = str(predictor_hash)
        self.amp_dtype_name = str(amp_dtype).lower()
        self.amp_dtype = self._resolve_amp_dtype(self.amp_dtype_name)
        self.static_cache = StaticEmbeddingCache(max_entries=int(static_cache_entries))
        self.n_forwards = 0
        self.n_context_rows = 0
        self.forward_seconds = 0.0

    def _resolve_amp_dtype(self, value: str) -> Optional[torch.dtype]:
        if self.device.type != "cuda" or value in {"none", "off", "fp32", "float32"}:
            return None
        if value in {"bf16", "bfloat16"}:
            if not torch.cuda.is_bf16_supported():
                raise RuntimeError("bf16 inference requested but this CUDA device lacks support")
            return torch.bfloat16
        if value in {"fp16", "float16", "half"}:
            return torch.float16
        raise ValueError(f"unsupported amp dtype {value!r}")

    def _autocast(self):
        if self.amp_dtype is None:
            return nullcontext()
        return torch.autocast(device_type=self.device.type, dtype=self.amp_dtype)

    def _cache_key(
        self,
        trace: PackedTrace,
        chunk: Dict[str, Any],
    ) -> Tuple[Any, ...]:
        return (
            trace.trace_id,
            int(chunk["core_id"]),
            int(chunk["chunk_id"]),
            trace.uarch_hash,
            self.checkpoint_id,
        )

    def _batch(
        self,
        chunks: Sequence[Dict[str, Any]],
        dynamic_fields: Sequence[Sequence[Sequence[int]]],
        relations: Sequence[Sequence[float]],
    ) -> Dict[str, torch.Tensor]:
        return {
            "valid_uop_mask": torch.as_tensor(
                np.stack([x["valid_uop_mask"] for x in chunks]),
                dtype=torch.bool,
                device=self.device,
            ),
            "dynamic_uop_fields": torch.as_tensor(
                np.asarray(dynamic_fields, dtype=np.int64),
                dtype=torch.long,
                device=self.device,
            ),
            "chunk_summary": torch.as_tensor(
                np.stack([x["chunk_summary"] for x in chunks]),
                dtype=torch.float32,
                device=self.device,
            ),
            "relation_features": torch.as_tensor(
                np.asarray(relations, dtype=np.float32),
                dtype=torch.float32,
                device=self.device,
            ),
            "uarch_features": torch.as_tensor(
                np.asarray([x["uarch_features"] for x in chunks], dtype=np.float32),
                dtype=torch.float32,
                device=self.device,
            ),
            "n_uops": torch.as_tensor(
                [float(x["n_uops"]) for x in chunks],
                dtype=torch.float32,
                device=self.device,
            ),
            # Layout metadata intentionally remains on CPU; see
            # FunctionalInteractionBlock._sample_ranges.
            "sample_ptr": torch.tensor([0, len(chunks)], dtype=torch.long),
        }

    def predict(
        self, trace: PackedTrace, chunks: Sequence[Dict[str, Any]]
    ) -> ContextPrediction:
        if not chunks:
            return ContextPrediction([], [])
        if self.predictor_hash and trace.predictor_hash != self.predictor_hash:
            raise RuntimeError(
                "checkpoint/trace branch predictor mismatch: "
                f"{self.predictor_hash} != {trace.predictor_hash}"
            )
        dynamic_fields, relations = context_features(list(chunks))
        batch = self._batch(chunks, dynamic_fields, relations)
        cached: List[Optional[torch.Tensor]] = []
        keys: List[Tuple[Any, ...]] = []
        miss_indices: List[int] = []
        for index, chunk in enumerate(chunks):
            key = self._cache_key(trace, chunk)
            keys.append(key)
            value = self.static_cache.get(key)
            cached.append(value)
            if value is None:
                miss_indices.append(index)

        started = time.perf_counter()
        with torch.inference_mode(), self._autocast():
            if miss_indices:
                miss_fields = torch.as_tensor(
                    np.asarray([
                        chunks[index]["per_uop_fields"] for index in miss_indices
                    ], dtype=np.int64),
                    dtype=torch.long,
                    device=self.device,
                )
                encoded = self.model.static_enc.encode_tokens(miss_fields)
                for local, index in enumerate(miss_indices):
                    value = encoded[local]
                    self.static_cache.put(keys[index], value)
                    cached[index] = value
            if any(value is None for value in cached):  # defensive type/runtime check
                raise RuntimeError("static cache failed to supply every context row")
            token_static = torch.stack([value for value in cached if value is not None])
            output = self.model.forward_from_static(batch, token_static)
            delta = output["pred_delta_cycles"].float().cpu().tolist()
            branch = output["pred_branch_miss_prob"].float().cpu().tolist()
        self.forward_seconds += time.perf_counter() - started
        self.n_forwards += 1
        self.n_context_rows += len(chunks)

        if len(delta) != len(chunks) or len(branch) != len(chunks):
            raise RuntimeError("model output row count does not match active core context")
        if any(not math.isfinite(float(value)) or float(value) <= 0.0 for value in delta):
            raise RuntimeError(f"non-finite/non-positive model duration: {delta}")
        return ContextPrediction(
            [max(1.0, float(value)) for value in delta],
            [min(1.0, max(0.0, float(value))) for value in branch],
        )


@dataclass
class _CoreState:
    core_id: int
    cursor: int = 0
    T_pred: float = 0.0
    E_pred: float = 0.0
    delta_hat: float = 0.0
    branch_prob_hat: float = 0.0
    branch_miss_hat: float = 0.0
    resident: bool = False
    exposure: int = 0
    force_fast: bool = False
    current: Optional[Dict[str, Any]] = None


@dataclass
class DeploymentRun:
    summary: Dict[str, Any]
    fast_sets: List[List[int]]


class DeploymentRunner:
    """Predicted-state epsilon scheduler with exact-once output latching."""

    def __init__(
        self,
        *,
        epsilon: float,
        max_resident_exposure: int = 0,
        max_steps: int = 0,
        force_sync_fast: bool = True,
        prefix_lens: Sequence[int] = (4, 8, 16, 32),
    ) -> None:
        self.epsilon = float(epsilon)
        if self.epsilon < 0:
            raise ValueError("epsilon must be non-negative")
        self.max_resident_exposure = int(max_resident_exposure)
        self.max_steps = int(max_steps)
        self.force_sync_fast = bool(force_sync_fast)
        self.prefix_lens = [int(value) for value in prefix_lens if int(value) > 0]

    def run(
        self,
        trace: PackedTrace,
        predictor: Any,
        *,
        step_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> DeploymentRun:
        states = {core: _CoreState(core) for core in trace.core_ids}
        committed: set = set()
        latched: set = set()
        fast_sets: List[List[int]] = []
        pred_seq: Dict[int, List[float]] = {core: [] for core in trace.core_ids}
        true_seq: Dict[int, List[float]] = {core: [] for core in trace.core_ids}
        uops_core = {core: 0 for core in trace.core_ids}
        valid_uops_core = {core: 0 for core in trace.core_ids}
        pred_valid_cycles_core = {core: 0.0 for core in trace.core_ids}
        pred_branch_core = {core: 0.0 for core in trace.core_ids}
        true_branch_core = {core: 0.0 for core in trace.core_ids}
        branch_opportunity_core = {core: 0 for core in trace.core_ids}
        invalid_core = {core: 0 for core in trace.core_ids}
        chunk_cpi_ape: List[float] = []
        chunk_cpi_signed_rel: List[float] = []
        window_cpi_ape: List[float] = []
        branch_prob_abs: List[float] = []
        n_resident_events = 0
        n_scheduler_context_rows = 0
        n_new_chunk_rows = 0
        max_exposure = 0
        n_sync_forced = 0
        n_model_forwards_start = int(getattr(predictor, "n_forwards", 0))
        committed_uops = 0
        cumulative_pred_valid_cycles = 0.0
        cumulative_true_cycles = 0.0
        cumulative_valid_uops = 0
        step = 0

        while len(committed) < trace.total_chunks:
            if self.max_steps > 0 and step >= self.max_steps:
                raise RuntimeError(
                    f"deployment rollout exceeded max_steps={self.max_steps}: "
                    f"committed={len(committed)}/{trace.total_chunks}"
                )
            active: List[int] = []
            new_cores: List[int] = []
            for core in trace.core_ids:
                state = states[core]
                if state.current is None and state.cursor < trace.core_counts[core]:
                    state.current = trace.get_chunk(core, state.cursor)
                    state.resident = False
                    state.exposure = 0
                    new_cores.append(core)
                if state.current is not None:
                    active.append(core)
            if not active:
                break
            n_scheduler_context_rows += len(active)
            n_new_chunk_rows += len(new_cores)

            chunks = [states[core].current for core in active]
            if any(chunk is None for chunk in chunks):
                raise RuntimeError("active core without a current chunk")
            typed_chunks = [chunk for chunk in chunks if chunk is not None]

            # Full-QKVR sees every active current chunk.  Only newly loaded
            # chunks latch outputs; resident predictions remain unchanged.
            if new_cores:
                prediction = predictor.predict(trace, typed_chunks)
                if len(prediction.delta_cycles) != len(active):
                    raise RuntimeError("predictor result does not match active cores")
                new_set = set(new_cores)
                for row, core in enumerate(active):
                    if core not in new_set:
                        continue
                    state = states[core]
                    chunk = state.current
                    assert chunk is not None
                    key = (trace.trace_id, core, int(chunk["chunk_id"]))
                    if key in latched:
                        raise RuntimeError(f"prediction latched twice for {key}")
                    latched.add(key)
                    state.delta_hat = max(1.0, float(prediction.delta_cycles[row]))
                    state.branch_prob_hat = min(
                        1.0, max(0.0, float(prediction.branch_miss_prob[row]))
                    )
                    state.branch_miss_hat = (
                        state.branch_prob_hat * _branch_opportunities(chunk)
                    )
                    state.E_pred = state.T_pred + state.delta_hat

            E_min = min(states[core].E_pred for core in active)
            limit = E_min + self.epsilon
            fast = [core for core in active if states[core].E_pred <= limit]
            fast_set = set(fast)
            for core in active:
                state = states[core]
                chunk = state.current
                assert chunk is not None
                forced = state.force_fast
                sync = self.force_sync_fast and bool(
                    chunk.get("has_atomic") or chunk.get("has_serialize")
                )
                if (forced or sync) and core not in fast_set:
                    fast.append(core)
                    fast_set.add(core)
                    if sync:
                        n_sync_forced += 1
            slow = [core for core in active if core not in fast_set]
            if not fast:
                raise RuntimeError("epsilon scheduler made no progress")

            records: List[Dict[str, Any]] = []
            new_set = set(new_cores)
            for core in active:
                state = states[core]
                chunk = state.current
                assert chunk is not None
                records.append({
                    "core_id": core,
                    "chunk_id": int(chunk["chunk_id"]),
                    "newly_loaded": core in new_set,
                    "resident": state.resident,
                    "exposure": state.exposure,
                    "T_pred": state.T_pred,
                    "E_pred": state.E_pred,
                    "delta_hat": state.delta_hat,
                    "branch_miss_hat": state.branch_miss_hat,
                    "commits": core in fast_set,
                })

            step_pred_valid_cycles = 0.0
            step_true_cycles = 0.0
            step_valid_uops = 0
            step_committed_uops = 0
            for core in fast:
                state = states[core]
                chunk = state.current
                assert chunk is not None
                key = (trace.trace_id, core, int(chunk["chunk_id"]))
                if key in committed:
                    raise RuntimeError(f"double commit for {key}")
                if key not in latched:
                    raise RuntimeError(f"commit without a latched prediction for {key}")
                committed.add(key)
                pred_seq[core].append(state.delta_hat)
                n_uops = int(chunk["n_uops"])
                uops_core[core] += n_uops
                committed_uops += n_uops
                step_committed_uops += n_uops
                pred_branch_core[core] += state.branch_miss_hat
                true_branch_core[core] += float(chunk.get("n_branch_miss", 0))
                branch_opportunity_core[core] += _branch_opportunities(chunk)
                true_delta = chunk.get("true_delta_cycles")
                if true_delta is None:
                    invalid_core[core] += 1
                else:
                    true_value = float(true_delta)
                    true_seq[core].append(true_value)
                    valid_uops_core[core] += n_uops
                    pred_valid_cycles_core[core] += state.delta_hat
                    cumulative_pred_valid_cycles += state.delta_hat
                    cumulative_true_cycles += true_value
                    cumulative_valid_uops += n_uops
                    step_pred_valid_cycles += state.delta_hat
                    step_true_cycles += true_value
                    step_valid_uops += n_uops
                    pred_cpi = state.delta_hat / max(1, n_uops)
                    true_cpi = true_value / max(1, n_uops)
                    denom = max(1e-3, true_cpi)
                    chunk_cpi_ape.append(abs(pred_cpi - true_cpi) / denom)
                    chunk_cpi_signed_rel.append((pred_cpi - true_cpi) / denom)
                opportunities = _branch_opportunities(chunk)
                if opportunities > 0:
                    true_prob = float(chunk.get("n_branch_miss", 0)) / opportunities
                    branch_prob_abs.append(abs(state.branch_prob_hat - true_prob))
                state.T_pred = state.E_pred
                state.cursor += 1
                state.current = None
                state.delta_hat = 0.0
                state.branch_prob_hat = 0.0
                state.branch_miss_hat = 0.0
                state.resident = False
                state.exposure = 0
                state.force_fast = False

            if step_valid_uops > 0:
                pred_window_cpi = step_pred_valid_cycles / step_valid_uops
                true_window_cpi = step_true_cycles / step_valid_uops
                window_cpi_ape.append(
                    abs(pred_window_cpi - true_window_cpi)
                    / max(1e-3, true_window_cpi)
                )

            for core in slow:
                state = states[core]
                state.resident = True
                state.exposure += 1
                n_resident_events += 1
                max_exposure = max(max_exposure, state.exposure)
                if 0 < self.max_resident_exposure <= state.exposure:
                    state.force_fast = True

            fast_sets.append(sorted(fast))
            if step_sink is not None:
                step_sink({
                    "trace_id": trace.trace_id,
                    "step": step,
                    "E_min": E_min,
                    "limit": limit,
                    "new_cores": sorted(new_cores),
                    "fast_cores": sorted(fast),
                    "slow_cores": sorted(slow),
                    "active_cores": len(active),
                    "new_chunk_rows": len(new_cores),
                    "committed_this_step": len(fast),
                    "committed_uops_this_step": step_committed_uops,
                    "committed_chunks": len(committed),
                    "total_chunks": trace.total_chunks,
                    "committed_uops": committed_uops,
                    "running_pred_roi_cpi": (
                        cumulative_pred_valid_cycles / cumulative_valid_uops
                        if cumulative_valid_uops > 0 else None
                    ),
                    "running_true_roi_cpi": (
                        cumulative_true_cycles / cumulative_valid_uops
                        if cumulative_valid_uops > 0 else None
                    ),
                    "running_roi_cpi_error": (
                        _relerr(cumulative_pred_valid_cycles, cumulative_true_cycles)
                        if cumulative_valid_uops > 0 else None
                    ),
                    "core_records": records,
                })
            step += 1

        if len(committed) != trace.total_chunks:
            raise RuntimeError(
                f"incomplete deployment rollout: {len(committed)}/{trace.total_chunks}"
            )
        if len(latched) != trace.total_chunks:
            raise RuntimeError(
                f"not every chunk was latched exactly once: {len(latched)}/{trace.total_chunks}"
            )

        endpoint_err: List[float] = []
        core_roi_cpi_err: List[float] = []
        core_roi_cpi_signed_err: List[float] = []
        pred_core_total: Dict[int, float] = {}
        true_core_total: Dict[int, float] = {}
        per_core: List[Dict[str, Any]] = []
        prefix_drift: Dict[int, List[float]] = {length: [] for length in self.prefix_lens}
        for core in trace.core_ids:
            pred_values = pred_seq[core]
            true_values = true_seq[core]
            pred_total = sum(pred_values)
            true_total = sum(true_values)
            pred_core_total[core] = pred_total
            true_core_total[core] = true_total
            core_uops = int(uops_core[core])
            core_valid_uops = int(valid_uops_core[core])
            pred_core_cpi = (
                pred_valid_cycles_core[core] / core_valid_uops
                if core_valid_uops > 0 else float("nan")
            )
            true_core_cpi = (
                true_total / core_valid_uops
                if core_valid_uops > 0 else float("nan")
            )
            core_cpi_error = (
                abs(pred_core_cpi - true_core_cpi) / max(1e-3, abs(true_core_cpi))
                if invalid_core[core] == 0 and core_valid_uops > 0 else float("nan")
            )
            core_cpi_signed = (
                (pred_core_cpi - true_core_cpi) / max(1e-3, abs(true_core_cpi))
                if invalid_core[core] == 0 and core_valid_uops > 0 else float("nan")
            )
            if math.isfinite(core_cpi_error):
                core_roi_cpi_err.append(core_cpi_error)
                core_roi_cpi_signed_err.append(core_cpi_signed)
            opportunities = int(branch_opportunity_core[core])
            pred_branch_rate = pred_branch_core[core] / opportunities if opportunities else 0.0
            true_branch_rate = true_branch_core[core] / opportunities if opportunities else 0.0
            per_core.append({
                "core_id": core,
                "chunks": len(pred_values),
                "uops": core_uops,
                "valid_label_uops": core_valid_uops,
                "pred_cycles": pred_total,
                "true_cycles": true_total,
                "pred_roi_cpi": pred_core_cpi,
                "true_roi_cpi": true_core_cpi,
                "roi_cpi_error": core_cpi_error,
                "roi_cpi_signed_error": core_cpi_signed,
                "retired_branches": opportunities,
                "pred_branch_misses": pred_branch_core[core],
                "true_branch_misses": true_branch_core[core],
                "pred_branch_miss_rate": pred_branch_rate,
                "true_branch_miss_rate": true_branch_rate,
                "branch_miss_rate_abs_error": abs(pred_branch_rate - true_branch_rate),
            })
            if invalid_core[core] == 0 and true_values:
                endpoint_err.append(_relerr(pred_total, true_total))
                for length in self.prefix_lens:
                    if len(true_values) >= length:
                        prefix_drift[length].append(
                            _relerr(sum(pred_values[:length]), sum(true_values[:length]))
                        )

        total_pred = sum(pred_core_total.values())
        total_true = sum(true_core_total.values())
        pred_makespan = max(pred_core_total.values()) if pred_core_total else 0.0
        true_makespan = max(true_core_total.values()) if true_core_total else 0.0
        branch_pred = sum(pred_branch_core.values())
        branch_true = sum(true_branch_core.values())
        total_uops = sum(uops_core.values())
        valid_uops = sum(valid_uops_core.values())
        pred_valid_cycles = sum(pred_valid_cycles_core.values())
        pred_roi_cpi = pred_valid_cycles / valid_uops if valid_uops > 0 else float("nan")
        true_roi_cpi = total_true / valid_uops if valid_uops > 0 else float("nan")
        total_retired_branches = sum(branch_opportunity_core.values())
        pred_branch_rate = branch_pred / total_retired_branches if total_retired_branches else 0.0
        true_branch_rate = branch_true / total_retired_branches if total_retired_branches else 0.0
        branch_relative_error = (
            abs(branch_pred - branch_true) / branch_true
            if branch_true > 0 else float("nan")
        )
        roi_cpi_error = (
            abs(pred_roi_cpi - true_roi_cpi) / max(1e-3, abs(true_roi_cpi))
            if valid_uops > 0 else float("nan")
        )
        summary: Dict[str, Any] = {
            "trace_id": trace.trace_id,
            "workload": trace.workload,
            "seed": trace.seed,
            "n_cores": len(trace.core_ids),
            "K": trace.K,
            "epsilon": self.epsilon,
            "n_chunks": trace.total_chunks,
            "n_valid_cycle_labels": len(chunk_cpi_ape),
            "n_invalid_cycle_labels": sum(invalid_core.values()),
            "roi_uops": total_uops,
            "roi_valid_label_uops": valid_uops,
            "roi_label_coverage": valid_uops / max(1, total_uops),
            "n_steps": step,
            "n_model_forwards": int(getattr(predictor, "n_forwards", 0)) - n_model_forwards_start,
            "n_resident_events": n_resident_events,
            "max_exposure": max_exposure,
            "n_sync_forced": n_sync_forced,
            "exact_once_latched": len(latched),
            "exact_once_committed": len(committed),
            # Explicit granularity.  The cpi_mape_* aliases are retained for
            # compatibility with early smoke reports.
            "chunk_cpi_mape_mean": _mean(chunk_cpi_ape),
            "chunk_cpi_mape_p50": _pctl(chunk_cpi_ape, 0.50),
            "chunk_cpi_mape_p90": _pctl(chunk_cpi_ape, 0.90),
            "chunk_cpi_mape_p99": _pctl(chunk_cpi_ape, 0.99),
            "chunk_cpi_signed_bias": _mean(chunk_cpi_signed_rel),
            "window_cpi_mape_mean": _mean(window_cpi_ape),
            "window_cpi_mape_p50": _pctl(window_cpi_ape, 0.50),
            "window_cpi_mape_p90": _pctl(window_cpi_ape, 0.90),
            "window_cpi_mape_p99": _pctl(window_cpi_ape, 0.99),
            "core_roi_cpi_mape_mean": _mean(core_roi_cpi_err),
            "core_roi_cpi_mape_p50": _pctl(core_roi_cpi_err, 0.50),
            "core_roi_cpi_mape_p90": _pctl(core_roi_cpi_err, 0.90),
            "core_roi_cpi_mape_p99": _pctl(core_roi_cpi_err, 0.99),
            "core_roi_cpi_signed_bias": _mean(core_roi_cpi_signed_err),
            "pred_roi_cpi": pred_roi_cpi,
            "true_roi_cpi": true_roi_cpi,
            "roi_cpi_error": roi_cpi_error,
            "cpi_mape_mean": _mean(chunk_cpi_ape),
            "cpi_mape_p50": _pctl(chunk_cpi_ape, 0.50),
            "cpi_mape_p90": _pctl(chunk_cpi_ape, 0.90),
            "cpi_mape_p99": _pctl(chunk_cpi_ape, 0.99),
            "pred_cycle_sum": total_pred,
            "pred_valid_label_cycle_sum": pred_valid_cycles,
            "true_cycle_sum": total_true,
            "aggregate_cycle_error": roi_cpi_error,
            "pred_makespan": pred_makespan,
            "true_makespan": true_makespan,
            "makespan_error": _relerr(pred_makespan, true_makespan),
            "endpoint_error_p50": _pctl(endpoint_err, 0.50),
            "endpoint_error_p90": _pctl(endpoint_err, 0.90),
            "endpoint_error_p99": _pctl(endpoint_err, 0.99),
            "pred_branch_misses": branch_pred,
            "true_branch_misses": branch_true,
            "retired_branches": total_retired_branches,
            "branch_opportunity_kind": getattr(
                trace, "branch_contract", "all_retired_branches"
            ),
            "pred_branch_miss_rate": pred_branch_rate,
            "true_branch_miss_rate": true_branch_rate,
            "branch_miss_rate_abs_error": abs(pred_branch_rate - true_branch_rate),
            # Canonical branch-relative metric. Count-relative and rate-relative
            # are identical because both rates share total_retired_branches. Keep
            # the two old names as exact aliases for report compatibility.
            "branch_miss_relative_error": branch_relative_error,
            "branch_miss_count_error": branch_relative_error,
            "branch_miss_rate_relative_error": branch_relative_error,
            "branch_miss_probability_mae": _mean(branch_prob_abs),
            "scheduler_context_rows": n_scheduler_context_rows,
            "new_chunk_rows": n_new_chunk_rows,
            "resident_row_fraction": n_resident_events / max(1, n_scheduler_context_rows),
            "per_core": per_core,
        }
        for length, values in prefix_drift.items():
            summary[f"prefix_drift_L{length}_p50"] = _pctl(values, 0.50)
            summary[f"prefix_drift_L{length}_p90"] = _pctl(values, 0.90)
        return DeploymentRun(summary=summary, fast_sets=fast_sets)


def _compare_schedules(predicted: DeploymentRun, oracle: DeploymentRun) -> Dict[str, Any]:
    count = min(len(predicted.fast_sets), len(oracle.fast_sets))
    jaccard: List[float] = []
    exact = 0
    first_divergence: Optional[int] = None
    for step in range(count):
        left = set(predicted.fast_sets[step])
        right = set(oracle.fast_sets[step])
        union = left | right
        jaccard.append(len(left & right) / max(1, len(union)))
        if left == right:
            exact += 1
        elif first_divergence is None:
            first_divergence = step
    return {
        "oracle_n_steps": oracle.summary["n_steps"],
        "oracle_n_resident_events": oracle.summary["n_resident_events"],
        "oracle_max_exposure": oracle.summary["max_exposure"],
        "compared_steps": count,
        "fast_set_exact_rate": exact / max(1, count),
        "fast_set_jaccard_mean": _mean(jaccard),
        "first_fast_set_divergence_step": first_divergence,
    }


def load_manifest_rollouts(path: str, split: str) -> List[Dict[str, Any]]:
    manifest = load_json(path)
    quality = manifest.get("quality", {})
    if quality.get("status") != "pass":
        raise RuntimeError(
            "manifest quality is not pass; deployment evaluation is blocked: "
            + "; ".join(str(x) for x in quality.get("blockers", []))
        )
    base = os.path.dirname(os.path.abspath(path))
    rows: List[Dict[str, Any]] = []
    for raw in manifest.get("splits", {}).get(split, []):
        item = dict(raw) if isinstance(raw, dict) else {"rollout_dir": str(raw)}
        value = item.get("rollout_dir")
        if not value:
            continue
        item["rollout_dir"] = value if os.path.isabs(value) else os.path.join(base, value)
        rows.append(item)
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("n_cores", 0)),
            str(item.get("workload", "")),
            str(item["rollout_dir"]),
        ),
    )


def discover_packed_rollouts(root: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for meta_path in glob.glob(os.path.join(os.path.abspath(root), "**", "meta.json"), recursive=True):
        out_dir = os.path.dirname(meta_path)
        meta = load_json(meta_path)
        packed = meta.get("packed")
        if not isinstance(packed, dict):
            continue
        packed_dir = os.path.join(out_dir, str(packed.get("relative_dir", "packed")))
        if all(os.path.isfile(os.path.join(packed_dir, f"{name}.npy")) for name in PackedTrace.REQUIRED_ARRAYS):
            rows.append({"rollout_dir": out_dir, "workload": os.path.basename(out_dir)})
    return sorted(rows, key=lambda item: str(item["rollout_dir"]))


def aggregate_trace_reports(
    traces: Sequence[Dict[str, Any]],
    *,
    run: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    rows = list(traces)
    total_pred = sum(float(row.get("pred_cycle_sum", 0.0)) for row in rows)
    total_pred_valid = sum(
        float(row.get("pred_valid_label_cycle_sum", row.get("pred_cycle_sum", 0.0)))
        for row in rows
    )
    total_true = sum(float(row.get("true_cycle_sum", 0.0)) for row in rows)
    total_uops = sum(int(row.get("roi_uops", 0)) for row in rows)
    total_valid_uops = sum(int(row.get("roi_valid_label_uops", 0)) for row in rows)
    total_branch_pred = sum(float(row.get("pred_branch_misses", 0.0)) for row in rows)
    total_branch_true = sum(float(row.get("true_branch_misses", 0.0)) for row in rows)
    total_retired_branches = sum(int(
        row.get("retired_branches", row.get("conditional_branches", 0))
    ) for row in rows)
    total_wall = sum(float(row.get("wall_seconds", 0.0)) for row in rows)
    pred_roi_cpi = total_pred_valid / total_valid_uops if total_valid_uops else float("nan")
    true_roi_cpi = total_true / total_valid_uops if total_valid_uops else float("nan")
    pred_branch_rate = total_branch_pred / total_retired_branches if total_retired_branches else 0.0
    true_branch_rate = total_branch_true / total_retired_branches if total_retired_branches else 0.0
    branch_relative_error = (
        abs(total_branch_pred - total_branch_true) / total_branch_true
        if total_branch_true > 0 else float("nan")
    )
    aggregate: Dict[str, Any] = {
        "n_traces": len(rows),
        "n_chunks": sum(int(row.get("n_chunks", 0)) for row in rows),
        "n_steps": sum(int(row.get("n_steps", 0)) for row in rows),
        "n_model_forwards": sum(int(row.get("n_model_forwards", 0)) for row in rows),
        "n_resident_events": sum(int(row.get("n_resident_events", 0)) for row in rows),
        "n_invalid_cycle_labels": sum(int(row.get("n_invalid_cycle_labels", 0)) for row in rows),
        "roi_uops": total_uops,
        "roi_valid_label_uops": total_valid_uops,
        "roi_label_coverage": total_valid_uops / max(1, total_uops),
        "pred_cycle_sum": total_pred,
        "pred_valid_label_cycle_sum": total_pred_valid,
        "true_cycle_sum": total_true,
        "pred_roi_cpi": pred_roi_cpi,
        "true_roi_cpi": true_roi_cpi,
        "global_roi_cpi_error": (
            abs(pred_roi_cpi - true_roi_cpi) / max(1e-3, abs(true_roi_cpi))
            if total_valid_uops else float("nan")
        ),
        "global_aggregate_cycle_error": _relerr(total_pred_valid, total_true),
        "retired_branches": total_retired_branches,
        "pred_branch_misses": total_branch_pred,
        "true_branch_misses": total_branch_true,
        "pred_branch_miss_rate": pred_branch_rate,
        "true_branch_miss_rate": true_branch_rate,
        "branch_miss_rate_abs_error": abs(pred_branch_rate - true_branch_rate),
        "branch_miss_relative_error": branch_relative_error,
        "branch_miss_rate_relative_error": branch_relative_error,
        "global_branch_miss_count_error": branch_relative_error,
        "summed_trace_wall_seconds": total_wall,
        "aggregate_committed_uops_per_second": total_uops / max(1e-9, total_wall),
        "aggregate_committed_chunks_per_second": (
            sum(int(row.get("n_chunks", 0)) for row in rows) / max(1e-9, total_wall)
        ),
    }
    metric_names = (
        "chunk_cpi_mape_mean", "chunk_cpi_mape_p90", "window_cpi_mape_mean",
        "window_cpi_mape_p90", "core_roi_cpi_mape_mean",
        "core_roi_cpi_mape_p90", "roi_cpi_error", "makespan_error",
        "endpoint_error_p50", "endpoint_error_p90",
        "branch_miss_probability_mae", "branch_miss_rate_abs_error",
        "fast_set_exact_rate", "fast_set_jaccard_mean",
    )
    for name in metric_names:
        values = [float(row[name]) for row in rows if row.get(name) is not None]
        aggregate[f"trace_{name}_mean"] = _mean(values)
        aggregate[f"trace_{name}_p50"] = _pctl(values, 0.50)
        aggregate[f"trace_{name}_p90"] = _pctl(values, 0.90)
    return {"schema_version": "tcsim-deployment-eval-1", "run": run or {}, "aggregate": aggregate, "traces": rows}


def evaluate_packed_rollouts(
    rollout_sources: Sequence[Dict[str, Any]],
    predictor: ModelContextPredictor,
    *,
    epsilon: float,
    max_resident_exposure: int,
    max_steps: int = 0,
    max_chunks_per_core: int = 0,
    oracle_schedule: bool = True,
    force_sync_fast: bool = True,
    prefix_lens: Sequence[int] = (4, 8, 16, 32),
    step_dump_dir: Optional[str] = None,
    trace_start: Optional[Callable[[int, int, PackedTrace], None]] = None,
    step_progress: Optional[
        Callable[[int, int, PackedTrace, Dict[str, Any]], None]
    ] = None,
    progress: Optional[Callable[[int, int, Dict[str, Any]], None]] = None,
) -> List[Dict[str, Any]]:
    runner = DeploymentRunner(
        epsilon=epsilon,
        max_resident_exposure=max_resident_exposure,
        max_steps=max_steps,
        force_sync_fast=force_sync_fast,
        prefix_lens=prefix_lens,
    )
    reports: List[Dict[str, Any]] = []
    for index, source in enumerate(rollout_sources, 1):
        trace = PackedTrace(
            str(source["rollout_dir"]),
            source=source,
            max_chunks_per_core=max_chunks_per_core,
        )
        if trace.K > predictor.model.static_enc.max_K:
            raise RuntimeError(
                f"trace K={trace.K} exceeds checkpoint max_K={predictor.model.static_enc.max_K}"
            )
        if trace_start:
            trace_start(index, len(rollout_sources), trace)
        wall_started = time.perf_counter()
        sink = None
        step_file = None
        if step_dump_dir:
            os.makedirs(step_dump_dir, exist_ok=True)
            safe = hashlib.sha1(trace.trace_id.encode("utf-8")).hexdigest()[:12]
            step_file = open(os.path.join(step_dump_dir, f"{safe}.jsonl"), "w", encoding="utf-8")

        if step_file is not None or step_progress is not None:
            def sink(row: Dict[str, Any], handle=step_file) -> None:
                row["wall_seconds"] = time.perf_counter() - wall_started
                row["model_forwards"] = predictor.n_forwards - before_forwards
                row["model_forward_seconds"] = predictor.forward_seconds - before_seconds
                row["model_context_rows"] = predictor.n_context_rows - before_context_rows
                if handle is not None:
                    handle.write(json.dumps(json_safe(row), sort_keys=True) + "\n")
                if step_progress is not None:
                    step_progress(index, len(rollout_sources), trace, row)

        before_hits = predictor.static_cache.hits
        before_misses = predictor.static_cache.misses
        before_evictions = predictor.static_cache.evictions
        before_seconds = predictor.forward_seconds
        before_forwards = predictor.n_forwards
        before_context_rows = predictor.n_context_rows
        if predictor.device.type == "cuda":
            torch.cuda.reset_peak_memory_stats(predictor.device)
        try:
            predicted = runner.run(trace, predictor, step_sink=sink)
        finally:
            if step_file is not None:
                step_file.close()
        row = dict(predicted.summary)
        row["wall_seconds"] = time.perf_counter() - wall_started
        row["model_forward_seconds"] = predictor.forward_seconds - before_seconds
        row["non_model_seconds"] = max(
            0.0, row["wall_seconds"] - row["model_forward_seconds"]
        )
        row["model_context_rows"] = predictor.n_context_rows - before_context_rows
        row["static_cache_hits"] = predictor.static_cache.hits - before_hits
        row["static_cache_misses"] = predictor.static_cache.misses - before_misses
        row["static_cache_evictions"] = predictor.static_cache.evictions - before_evictions
        row["static_cache_hit_rate"] = row["static_cache_hits"] / max(
            1, row["static_cache_hits"] + row["static_cache_misses"]
        )
        wall = max(1e-9, float(row["wall_seconds"]))
        forward_time = max(1e-9, float(row["model_forward_seconds"]))
        row["throughput"] = {
            "committed_uops_per_second": float(row["roi_uops"]) / wall,
            "committed_chunks_per_second": float(row["n_chunks"]) / wall,
            "scheduler_steps_per_second": float(row["n_steps"]) / wall,
            "model_forwards_per_second": float(row["n_model_forwards"]) / wall,
            "model_context_rows_per_second": float(row["model_context_rows"]) / wall,
            "avg_model_forward_ms": (
                1000.0 * forward_time / max(1, int(row["n_model_forwards"]))
            ),
            "avg_scheduler_step_ms": 1000.0 * wall / max(1, int(row["n_steps"])),
            "avg_active_cores_per_model_forward": (
                float(row["model_context_rows"]) / max(1, int(row["n_model_forwards"]))
            ),
        }
        if predictor.device.type == "cuda":
            row["gpu_peak_allocated_gib"] = (
                torch.cuda.max_memory_allocated(predictor.device) / (1024.0 ** 3)
            )
            row["gpu_peak_reserved_gib"] = (
                torch.cuda.max_memory_reserved(predictor.device) / (1024.0 ** 3)
            )
        if oracle_schedule:
            oracle = runner.run(trace, TruthContextPredictor())
            row.update(_compare_schedules(predicted, oracle))
        reports.append(row)
        if progress:
            progress(index, len(rollout_sources), row)
    return reports


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    return value


def append_trace_jsonl(path: str, row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(json_safe(row), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_report(path: str, report: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    dump_json(path, json_safe(report))
