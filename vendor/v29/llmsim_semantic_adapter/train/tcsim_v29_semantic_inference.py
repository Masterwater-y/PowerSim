"""Deployment inference adapter for controlled TCSim v29 E2/B2 runs."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping

import numpy as np
import torch

from tcsim.utils.config import TCSimConfig
from tcsim.v29.inference import V29ModelRunner

from model.tcsim_v29_semantic import (
    B2_VARIANTS,
    LEGACY_FUSION_ARCHITECTURE,
    SEMANTIC_EXPERIMENT_SCHEMA,
    build_semantic_model,
)
from train.macro_v29_dataset import CachedSemanticSource, MacroContractError
from train.tcsim_v29_semantic_dataset import _load_static_manifest
from train.tcsim_v29_semantic_train import SEMANTIC_CHECKPOINT_SCHEMA
from train.tcsim_v29_semantic_sidecar import load_semantic_id_sidecar


BASE_CONTRACT_KEYS = (
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


def _torch_load(path: str, map_location: Any = "cpu") -> Any:
    try:
        return torch.load(
            path, map_location=map_location, weights_only=False, mmap=True,
        )
    except TypeError:  # pragma: no cover - older torch
        return torch.load(path, map_location=map_location)


def _config_from_mapping(data: Mapping[str, Any]) -> TCSimConfig:
    return TCSimConfig(
        chunk=dict(data.get("chunk", {})),
        scheduler=dict(data.get("scheduler", {})),
        uarch=dict(data.get("uarch", {})),
        model=dict(data.get("model", {})),
        train=dict(data.get("train", {})),
    )


def _file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class FrozenDeploymentSemanticLookup:
    """Resolve one deployment context to a compact frozen semantic table."""

    def __init__(
        self,
        cache_root: str | Path,
        static_manifest: str | Path,
        *,
        semantic_permutation_seed: int | None = None,
        semantic_sidecar_root: str | Path | None = None,
    ):
        self.semantic_permutation_seed = semantic_permutation_seed
        self.cache = CachedSemanticSource(
            cache_root,
            fixed_permutation_seed=semantic_permutation_seed,
        )
        self.static_rows = _load_static_manifest(static_manifest)
        self.semantic_sidecar_root = (
            None if semantic_sidecar_root is None
            else Path(semantic_sidecar_root).resolve()
        )
        self._semantic_rows_by_trace: Dict[str, np.ndarray] = {}

    def _trace_parquet(self, store: Any) -> str:
        workload = str(store.meta.get("workload", ""))
        binary_name = workload.removeprefix("W_")
        row = self.static_rows.get(binary_name)
        if row is None:
            raise MacroContractError(
                f"no static dictionary for deployment workload {workload!r}"
            )
        return str(Path(str(row["parquet"])).resolve())

    def attach(
        self, store: Any, context: Mapping[str, Any],
    ) -> Dict[str, Any]:
        valid_mask = context["valid_uop_mask"].cpu().numpy().astype(
            bool, copy=False,
        )
        parquet = self._trace_parquet(store)
        binary_hash, arrays = self.cache._load_binary(parquet)
        if "macro_id" in context and bool(getattr(store, "has_macro_ids", False)):
            if self.semantic_sidecar_root is None:
                raise MacroContractError(
                    "B2 optimized deployment requires semantic_sidecar_root "
                    "when the TCSim cache exposes macro IDs"
                )
            mapping = self._semantic_rows_by_trace.get(str(store.trace_id))
            if mapping is None:
                mapping = load_semantic_id_sidecar(
                    store,
                    self.cache,
                    parquet,
                    self.semantic_sidecar_root,
                )
                self._semantic_rows_by_trace[str(store.trace_id)] = mapping
            macro_ids = context["macro_id"].cpu().numpy().astype(
                np.int64, copy=False,
            )
            if macro_ids.shape != valid_mask.shape:
                raise MacroContractError("B2 macro-ID window shape mismatch")
            selected_ids = macro_ids[valid_mask]
            if len(selected_ids) == 0 or np.any(selected_ids < 0) or np.any(
                selected_ids >= len(mapping)
            ):
                raise MacroContractError("B2 macro-ID window is invalid")
            unique_ids, inverse = np.unique(
                selected_ids, return_inverse=True,
            )
            cache_indices = np.asarray(mapping[unique_ids], dtype=np.int64)
            if self.semantic_permutation_seed is not None:
                permutation = self.cache._fixed_permutation(
                    binary_hash, arrays["pcs"],
                )
                cache_indices = permutation[cache_indices]
            semantic_index = np.full(valid_mask.shape, -1, dtype=np.int32)
            semantic_index[valid_mask] = inverse.astype(np.int32, copy=False)
            output = dict(context)
            output["semantic_values"] = torch.from_numpy(np.array(
                arrays["semantic"][cache_indices], copy=True,
            ))
            output["semantic_index"] = torch.from_numpy(semantic_index)
            return output

        pc_rows: List[np.ndarray] = []
        all_pcs: List[np.ndarray] = []
        for row, (slot_value, cursor_value) in enumerate(zip(
            context["core_slots"].tolist(), context["cursors"].tolist(),
        )):
            slot = int(slot_value)
            cursor = int(cursor_value)
            core_id = int(store.core_ids[slot])
            n_valid = int(np.count_nonzero(valid_mask[row]))
            pcs = np.asarray(
                store.cores[core_id]["macro_pc"][cursor:cursor + n_valid],
                dtype=np.uint64,
            )
            if len(pcs) != n_valid:
                raise MacroContractError("deployment semantic PC window truncated")
            pc_rows.append(pcs)
            if len(pcs):
                all_pcs.append(pcs)
        if not all_pcs:
            raise MacroContractError("B2 deployment context has no valid macro PCs")

        unique_pcs = np.unique(np.concatenate(all_pcs))
        lookup = self.cache._pc_indices[binary_hash]
        cache_indices = np.empty(len(unique_pcs), dtype=np.int64)
        for local, pc in enumerate(unique_pcs):
            cached = lookup.get(int(pc))
            if cached is None:
                raise MacroContractError(
                    f"B2 deployment cache miss binary={binary_hash} "
                    f"pc=0x{int(pc):x}; online fallback is forbidden"
                )
            cache_indices[local] = cached
        if self.semantic_permutation_seed is not None:
            permutation = self.cache._fixed_permutation(
                binary_hash, arrays["pcs"],
            )
            cache_indices = permutation[cache_indices]

        semantic_index = np.full(valid_mask.shape, -1, dtype=np.int32)
        for row, pcs in enumerate(pc_rows):
            if len(pcs):
                positions = np.searchsorted(unique_pcs, pcs).astype(
                    np.int32, copy=False,
                )
                if np.any(unique_pcs[positions] != pcs):
                    raise MacroContractError("deployment semantic index mismatch")
                semantic_index[row, :len(pcs)] = positions
        if np.any(semantic_index[valid_mask] < 0):
            raise MacroContractError("valid deployment B2 UOP lacks semantics")

        output = dict(context)
        output["semantic_values"] = torch.from_numpy(np.array(
            arrays["semantic"][cache_indices], copy=True,
        ))
        output["semantic_index"] = torch.from_numpy(semantic_index)
        return output


class TCSimV29SemanticRunner(V29ModelRunner):
    """Upstream free-running runner plus frozen semantic lookup."""

    def __init__(
        self,
        *args: Any,
        variant: str,
        semantic_lookup: FrozenDeploymentSemanticLookup | None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.variant = str(variant)
        self.semantic_lookup = semantic_lookup
        self._active_store: Any = None

    def begin_trace(self, store: Any) -> None:
        self._active_store = store
        super().begin_trace(store)

    def _model_batch(
        self, context: Mapping[str, Any],
    ) -> Dict[str, torch.Tensor]:
        adapted: Mapping[str, Any] = context
        if self.variant in B2_VARIANTS:
            if self.semantic_lookup is None or self._active_store is None:
                raise RuntimeError("B2 deployment semantic lookup is unavailable")
            adapted = self.semantic_lookup.attach(self._active_store, context)
        batch = super()._model_batch(adapted)
        if self.variant in B2_VARIANTS:
            batch["semantic_values"] = adapted["semantic_values"].to(
                self.device, non_blocking=True,
            )
            batch["semantic_index"] = adapted["semantic_index"].to(
                self.device, non_blocking=True,
            )
        return batch


def load_semantic_checkpoint_runner(
    checkpoint_path: str,
    *,
    device: str = "cuda",
    amp_dtype: str | None = None,
    sdpa_backend: str | None = None,
    static_cache: bool = True,
    semantic_cache_root: str | Path | None = None,
    static_manifest: str | Path | None = None,
    semantic_sidecar_root: str | Path | None = None,
) -> TCSimV29SemanticRunner:
    """Load E2-null or B2-frozen while preserving v29 rollout semantics."""

    if not os.path.isfile(checkpoint_path):
        raise FileNotFoundError(checkpoint_path)
    payload = _torch_load(checkpoint_path, "cpu")
    if not isinstance(payload, Mapping) or "model" not in payload:
        raise RuntimeError("not a full B2/E2 checkpoint")
    if payload.get("checkpoint_schema") != SEMANTIC_CHECKPOINT_SCHEMA:
        raise RuntimeError("checkpoint schema is not controlled B2/E2 v29")
    contract = payload.get("contract")
    config_data = payload.get("config")
    if not isinstance(contract, Mapping) or not isinstance(config_data, Mapping):
        raise RuntimeError("B2/E2 checkpoint lacks contract or config")
    if contract.get("semantic_experiment_schema") != SEMANTIC_EXPERIMENT_SCHEMA:
        raise RuntimeError("unsupported B2/E2 semantic experiment contract")
    semantic_contract = contract.get("semantic")
    if not isinstance(semantic_contract, Mapping):
        raise RuntimeError("B2/E2 checkpoint lacks semantic contract")
    variant = str(semantic_contract.get("variant", "")).lower()
    if variant not in {"e2-null", *B2_VARIANTS}:
        raise RuntimeError(f"unsupported checkpoint semantic variant {variant!r}")
    semantic_dim = int(semantic_contract.get("semantic_dim", 0))
    fusion_contract = contract.get("semantic_fusion", {})
    if not isinstance(fusion_contract, Mapping):
        raise RuntimeError("invalid B2/E2 semantic fusion contract")
    fusion_architecture = str(fusion_contract.get(
        "architecture", LEGACY_FUSION_ARCHITECTURE,
    ))
    adapter_hidden_dim = int(fusion_contract.get(
        "semantic_adapter_hidden_dim",
        contract.get("lora_v29_deployment_materialization", {}).get(
            "semantic_adapter_hidden_dim", 1024,
        ),
    ))
    semantic_slot_count = int(
        fusion_contract.get("semantic_slot_count", 4) or 4
    )
    semantic_attention_heads = int(
        fusion_contract.get("semantic_attention_heads", 4) or 4
    )
    max_residual_rms_ratio = float(fusion_contract.get(
        "max_residual_rms_ratio", 0.05,
    ) or 0.05)

    config = _config_from_mapping(config_data)
    model_config = dict(config.model)
    if sdpa_backend:
        model_config["sdpa_backend"] = str(sdpa_backend)
        config.model = model_config
    model = build_semantic_model(
        model_config,
        contract["horizons"],
        variant=variant,
        semantic_dim=semantic_dim,
        fusion_architecture=fusion_architecture,
        semantic_adapter_hidden_dim=adapter_hidden_dim,
        semantic_max_residual_rms_ratio=max_residual_rms_ratio,
        semantic_slot_count=semantic_slot_count,
        semantic_attention_heads=semantic_attention_heads,
    )
    model.load_state_dict(payload["model"], strict=True)

    semantic_lookup = None
    if variant in B2_VARIANTS:
        if (
            semantic_cache_root is None
            or static_manifest is None
            or semantic_sidecar_root is None
        ):
            raise RuntimeError(
                "B2 deployment requires semantic cache, static manifest, "
                "and semantic-ID sidecar root"
            )
        intervention = semantic_contract.get("semantic_intervention", {})
        if intervention is None:
            intervention = {}
        if not isinstance(intervention, Mapping):
            raise RuntimeError("invalid B2 semantic intervention contract")
        mode = str(intervention.get("mode", "full_real"))
        if mode not in {"full_real", "fixed_semantic_permute"}:
            raise RuntimeError(f"unsupported B2 semantic intervention {mode!r}")
        permutation_seed = (
            int(intervention["seed"])
            if mode == "fixed_semantic_permute" else None
        )
        semantic_lookup = FrozenDeploymentSemanticLookup(
            semantic_cache_root,
            static_manifest,
            semantic_permutation_seed=permutation_seed,
            semantic_sidecar_root=semantic_sidecar_root,
        )
        for key, value in semantic_lookup.cache.contract.items():
            if semantic_contract.get(key) != value:
                raise RuntimeError(
                    f"B2 checkpoint/cache contract mismatch for {key}"
                )
        static_contract = contract.get("static_manifest", {})
        if not isinstance(static_contract, Mapping):
            raise RuntimeError("B2 checkpoint lacks static manifest contract")
        if _file_sha256(static_manifest) != static_contract.get("sha256"):
            raise RuntimeError("B2 checkpoint/static manifest SHA256 mismatch")
    elif semantic_cache_root is not None or semantic_sidecar_root is not None:
        raise RuntimeError(
            "E2-null deployment must not receive semantic cache/sidecars"
        )

    stat = os.stat(checkpoint_path)
    checkpoint_id = hashlib.sha256(
        (
            f"{os.path.abspath(checkpoint_path)}:{stat.st_size}:"
            f"{stat.st_mtime_ns}"
        ).encode("utf-8")
    ).hexdigest()
    base_contract = {key: contract[key] for key in BASE_CONTRACT_KEYS}
    metadata = {
        "checkpoint": os.path.abspath(checkpoint_path),
        "checkpoint_id": checkpoint_id,
        "step": int(payload.get("step", 0)),
        "best_validation": float(
            payload.get("best_validation", float("nan"))
        ),
        "contract": base_contract,
        "semantic_contract": dict(semantic_contract),
        "sdpa_backend": str(model_config.get("sdpa_backend", "auto")),
    }
    del payload
    return TCSimV29SemanticRunner(
        model,
        config,
        metadata,
        variant=variant,
        semantic_lookup=semantic_lookup,
        device=device,
        amp_dtype=(amp_dtype or str(config.train.get("amp_dtype", "bf16"))),
        static_cache=static_cache,
    )
