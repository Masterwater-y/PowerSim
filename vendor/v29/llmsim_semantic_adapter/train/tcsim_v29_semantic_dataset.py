"""Compact frozen-semantic adapter for the current TCSim v29 dataset."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np
import torch

from tcsim.v29.dataset import V29GlobalTimeDataset, collate_v29_sequences

from model.tcsim_v29_semantic import B2_VARIANTS, SUPPORTED_VARIANTS
from train.macro_v29_dataset import CachedSemanticSource, MacroContractError
from train.tcsim_v29_semantic_sidecar import load_semantic_id_sidecar


def _load_static_manifest(path: str | Path) -> Dict[str, Dict[str, Any]]:
    manifest_path = Path(path)
    if not manifest_path.is_file():
        raise MacroContractError(f"static manifest not found: {manifest_path}")
    rows: Dict[str, Dict[str, Any]] = {}
    for line in manifest_path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        name = str(row.get("binary_name", ""))
        parquet = str(row.get("parquet", ""))
        if not name or not parquet:
            raise MacroContractError("invalid static manifest row")
        if name in rows:
            raise MacroContractError(f"duplicate static binary {name}")
        rows[name] = dict(row)
    if not rows:
        raise MacroContractError("empty static manifest")
    return rows


class TCSimV29SemanticDataset(V29GlobalTimeDataset):
    """Add per-UOP indices into a per-sequence unique semantic table.

    The compact representation is important at C32: a dense
    ``[sequence, core, 256, 5120]`` tensor would duplicate the same macro
    vector for every UOP occurrence.  This adapter transfers each unique
    cached macro vector once and a small int32 index for each UOP.
    """

    def __init__(
        self,
        sources: Sequence[Any],
        *,
        variant: str,
        semantic_cache_root: str | Path | None,
        static_manifest: str | Path | None,
        semantic_dim: int,
        semantic_permutation_seed: int | None = None,
        semantic_sidecar_root: str | Path | None = None,
        include_semantic_prompts: bool = False,
        sequence_length: int = 4,
        sequence_stride: int | None = None,
    ) -> None:
        self.variant = str(variant).lower()
        if self.variant not in SUPPORTED_VARIANTS:
            raise ValueError(f"unsupported semantic variant {variant!r}")
        self.semantic_dim = int(semantic_dim)
        self.semantic_permutation_seed = (
            None
            if semantic_permutation_seed is None
            else int(semantic_permutation_seed)
        )
        if self.semantic_dim <= 0:
            raise ValueError("semantic_dim must be positive")
        self.semantic_cache: CachedSemanticSource | None = None
        self.static_rows: Dict[str, Dict[str, Any]] = {}
        self.semantic_sidecar_root = (
            None if semantic_sidecar_root is None
            else Path(semantic_sidecar_root).resolve()
        )
        self.include_semantic_prompts = bool(include_semantic_prompts)
        self._prompt_tables: Dict[str, tuple[str, ...]] = {}
        self._semantic_rows_by_trace: Dict[str, np.ndarray] = {}
        if self.variant in B2_VARIANTS:
            if semantic_cache_root is None or static_manifest is None:
                raise ValueError(
                    "B2-frozen requires semantic_cache_root and static_manifest"
                )
            self.semantic_cache = CachedSemanticSource(
                semantic_cache_root,
                fixed_permutation_seed=self.semantic_permutation_seed,
            )
            if self.semantic_cache.semantic_dim != self.semantic_dim:
                raise MacroContractError(
                    f"cache semantic_dim={self.semantic_cache.semantic_dim} "
                    f"!= requested {self.semantic_dim}"
                )
            self.static_rows = _load_static_manifest(static_manifest)
        elif semantic_cache_root is not None:
            raise ValueError("E2-null must not receive a semantic cache")
        elif self.semantic_permutation_seed is not None:
            raise ValueError("E2-null cannot use semantic permutation")
        if self.include_semantic_prompts and self.variant not in B2_VARIANTS:
            raise ValueError("online semantic prompts are B2-only")
        if self.include_semantic_prompts and self.semantic_permutation_seed is not None:
            raise ValueError("online LoRA prompts cannot use shuffled semantics")
        super().__init__(
            sources,
            sequence_length=sequence_length,
            sequence_stride=sequence_stride,
        )
        if self.variant in B2_VARIANTS:
            if self.semantic_sidecar_root is None:
                raise MacroContractError(
                    "B2-frozen requires the versioned semantic-ID sidecar root"
                )
            missing_ids = [
                store.trace_id for store in self.stores
                if not bool(getattr(store, "has_macro_ids", False))
            ]
            if missing_ids:
                raise MacroContractError(
                    "B2-frozen requires TCSim macro-ID sidecars for every trace: "
                    + ", ".join(missing_ids[:8])
                )

    @property
    def semantic_contract(self) -> Dict[str, Any]:
        if self.semantic_cache is None:
            return {
                "variant": self.variant,
                "semantic_dim": self.semantic_dim,
                "semantic_source": "shared_trainable_NO_SEM",
                "offline_encoder_frozen": True,
            }
        contract = {
            "variant": self.variant,
            "semantic_source": (
                "frozen_lora_static_macro_cache"
                if self.variant == "b2-lora"
                else "frozen_static_macro_cache"
            ),
            "offline_encoder_frozen": True,
            **self.semantic_cache.contract,
        }
        contract["semantic_intervention"] = {
            key: value
            for key, value in self.semantic_cache.intervention_report.items()
            if key != "binaries"
        }
        return contract

    def _trace_parquet(self, store: Any) -> str:
        workload = str(store.meta.get("workload", ""))
        binary_name = workload.removeprefix("W_")
        row = self.static_rows.get(binary_name)
        if row is None:
            raise MacroContractError(
                f"no static dictionary for trace workload {workload!r}"
            )
        return str(Path(str(row["parquet"])).resolve())

    def _attach_b2_semantics(
        self,
        item: Dict[str, Any],
        store: Any,
    ) -> None:
        if self.semantic_cache is None:
            raise RuntimeError("B2 semantic cache was not initialized")
        parquet = self._trace_parquet(store)
        binary_hash, arrays = self.semantic_cache._load_binary(parquet)
        if all("macro_id" in context for context in item["contexts"]):
            mapping = self._semantic_rows_by_trace.get(str(store.trace_id))
            if mapping is None:
                mapping = load_semantic_id_sidecar(
                    store,
                    self.semantic_cache,
                    parquet,
                    self.semantic_sidecar_root,
                )
                self._semantic_rows_by_trace[str(store.trace_id)] = mapping
            valid_ids: List[np.ndarray] = []
            for context in item["contexts"]:
                valid = context["valid_uop_mask"].numpy().astype(bool, copy=False)
                macro_ids = context["macro_id"].numpy().astype(
                    np.int64, copy=False,
                )
                if macro_ids.shape != valid.shape:
                    raise MacroContractError("B2 training macro-ID shape mismatch")
                valid_ids.append(macro_ids[valid])
            unique_ids = np.unique(np.concatenate(valid_ids))
            if np.any(unique_ids < 0) or np.any(unique_ids >= len(mapping)):
                raise MacroContractError("B2 training macro-ID is out of range")
            cache_indices = np.asarray(mapping[unique_ids], dtype=np.int64)
            if self.semantic_permutation_seed is not None:
                permutation = self.semantic_cache._fixed_permutation(
                    binary_hash, arrays["pcs"],
                )
                cache_indices = permutation[cache_indices]
            item["semantic_values"] = torch.from_numpy(np.array(
                arrays["semantic"][cache_indices], copy=True,
            ))
            if self.include_semantic_prompts:
                table = self._prompt_table(
                    binary_hash=binary_hash,
                    parquet=parquet,
                    pcs=arrays["pcs"],
                )
                cache_batch = int(
                    self.semantic_cache.manifest["semantic_encoder_batch_size"]
                )
                if cache_batch <= 0:
                    raise MacroContractError("invalid semantic cache batch size")
                group_starts = np.unique(
                    (cache_indices // cache_batch) * cache_batch
                )
                group_indices = np.concatenate([
                    np.arange(
                        int(start),
                        min(int(start) + cache_batch, len(table)),
                        dtype=np.int64,
                    )
                    for start in group_starts
                ])
                positions = np.searchsorted(group_indices, cache_indices)
                if np.any(group_indices[positions] != cache_indices):
                    raise MacroContractError(
                        "canonical semantic cache group lookup mismatch"
                    )
                item["semantic_prompts"] = [
                    table[int(index)] for index in group_indices
                ]
                item["semantic_prompt_select"] = torch.from_numpy(
                    positions.astype(np.int64, copy=False)
                )
            for context in item["contexts"]:
                valid = context["valid_uop_mask"].numpy().astype(bool, copy=False)
                macro_ids = context["macro_id"].numpy().astype(
                    np.int64, copy=False,
                )
                indices = np.full(valid.shape, -1, dtype=np.int32)
                positions = np.searchsorted(unique_ids, macro_ids[valid])
                if np.any(unique_ids[positions] != macro_ids[valid]):
                    raise MacroContractError("B2 training macro-ID lookup mismatch")
                indices[valid] = positions.astype(np.int32, copy=False)
                context["semantic_index"] = torch.from_numpy(indices)
            return
        raise MacroContractError(
            "B2-frozen context is missing required TCSim macro IDs"
        )

    def _prompt_table(
        self,
        *,
        binary_hash: str,
        parquet: str,
        pcs: np.ndarray,
    ) -> tuple[str, ...]:
        cached = self._prompt_tables.get(str(binary_hash))
        if cached is not None:
            return cached
        if self.semantic_cache is None:
            raise RuntimeError("semantic cache was not initialized")
        # Lazy imports avoid a module cycle: the cache builder itself imports
        # CachedSemanticSource from train.macro_v29_dataset.
        from data.build_macro_v29_semantic_cache import (
            build_static_prompt_records,
        )
        from train.macro_v29_dataset import ParquetInstructionResolver

        schema = str(
            self.semantic_cache.manifest["semantic_prompt_schema_version"]
        )
        records = build_static_prompt_records(
            ParquetInstructionResolver(parquet),
            (int(value) for value in pcs),
            prompt_schema_version=schema,
            previous_instructions=4,
        )
        observed_pcs = np.asarray(
            [int(row["pc"]) for row in records], dtype=np.uint64,
        )
        if not np.array_equal(observed_pcs, np.asarray(pcs, dtype=np.uint64)):
            raise MacroContractError("online prompt/cache PC order mismatch")
        table = tuple(str(row["prompt"]) for row in records)
        self._prompt_tables[str(binary_hash)] = table
        return table

    def __getitem__(self, index: int) -> Dict[str, Any]:
        store_index, _sample_indices = self.sequences[int(index)]
        item = super().__getitem__(index)
        if self.variant in B2_VARIANTS:
            self._attach_b2_semantics(item, self.stores[store_index])
        return item


def collate_tcsim_v29_semantic(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    result = collate_v29_sequences(items)
    has_semantics = ["semantic_values" in item for item in items]
    if not any(has_semantics):
        return result
    if not all(has_semantics):
        raise MacroContractError("cannot mix B2-frozen and E2-null items")
    semantic_values: List[torch.Tensor] = []
    semantic_prompts: List[str] = []
    semantic_prompt_select: List[torch.Tensor] = []
    semantic_indices: List[torch.Tensor] = []
    offset = 0
    prompt_offset = 0
    for item in items:
        values = item["semantic_values"]
        semantic_values.append(values)
        prompts = item.get("semantic_prompts")
        if prompts is not None:
            selection = item.get("semantic_prompt_select")
            if selection is None or tuple(selection.shape) != (
                int(values.shape[0]),
            ):
                raise MacroContractError("semantic prompt selection mismatch")
            if torch.any(selection < 0) or torch.any(selection >= len(prompts)):
                raise MacroContractError("semantic prompt selection out of range")
            semantic_prompts.extend(str(value) for value in prompts)
            semantic_prompt_select.append(selection + int(prompt_offset))
            prompt_offset += len(prompts)
        for context in item["contexts"]:
            indices = context["semantic_index"]
            semantic_indices.append(torch.where(
                indices >= 0, indices + int(offset), indices,
            ))
        offset += int(values.shape[0])
    result["semantic_values"] = torch.cat(semantic_values, dim=0)
    if semantic_prompts:
        result["semantic_prompts"] = semantic_prompts
        result["semantic_prompt_select"] = torch.cat(
            semantic_prompt_select, dim=0,
        )
        if tuple(result["semantic_prompt_select"].shape) != (
            int(result["semantic_values"].shape[0]),
        ):
            raise MacroContractError("collated semantic prompt selection mismatch")
    result["semantic_index"] = torch.cat(semantic_indices, dim=0)
    if tuple(result["semantic_index"].shape) != tuple(
        result["valid_uop_mask"].shape
    ):
        raise MacroContractError("collated B2 semantic index shape mismatch")
    return result
