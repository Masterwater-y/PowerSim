"""Torch dataset over v29 common-time trace caches."""
from __future__ import annotations

import hashlib
import math
import os
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.io import load_json
from .contracts import (
    BRANCH_CONTRACT_VERSION,
    CHUNK_SUMMARY_NAMES,
    DATASET_SCHEMA_VERSION,
    DYNAMIC_FIELD_NAMES,
    FEATURE_SCHEMA_VERSION,
    FIELD_NAMES,
    FIELD_PAD_IDS,
    MODEL_INPUT_CONTRACT,
    RELATION_FEATURE_NAMES,
    RESOURCE_KEY_INVALID,
    RESOURCE_KEY_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
    normalized_horizons,
)
from .features import apply_window_pressure, context_features, summarize_window

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore

try:
    import torch
    from torch.utils.data import Dataset
except Exception:  # pragma: no cover
    torch = None  # type: ignore
    Dataset = object  # type: ignore


CORE_ARRAY_NAMES = (
    "fields", "resource", "commit_tick", "physical_line", "functional_line",
    "functional_page", "producer_log", "semantic_flags", "access", "macro_pc",
    "macro_end", "branch", "branch_miss",
)


def discover_trace_caches(root: str) -> List[str]:
    out = []
    for current, _dirs, files in os.walk(root):
        if "meta.json" not in files or "sample_cursors.npy" not in files:
            continue
        try:
            meta = load_json(os.path.join(current, "meta.json"))
        except Exception:
            continue
        if meta.get("dataset_schema") == DATASET_SCHEMA_VERSION:
            out.append(current)
    return sorted(out)


def _check_contract(meta: Mapping[str, Any]) -> None:
    expected = {
        "dataset_schema": DATASET_SCHEMA_VERSION,
        "feature_schema": FEATURE_SCHEMA_VERSION,
        "model_input_contract": MODEL_INPUT_CONTRACT,
        "branch_contract": BRANCH_CONTRACT_VERSION,
    }
    for key, value in expected.items():
        if meta.get(key) != value:
            raise RuntimeError(
                f"v29 cache contract mismatch {key}: {meta.get(key)!r} != {value!r}"
            )
    dimensions = dict(meta.get("dimensions", {}) or {})
    required_dims = {
        "static_fields": len(FIELD_NAMES),
        "dynamic_fields": len(DYNAMIC_FIELD_NAMES),
        "resource_keys": len(RESOURCE_KEY_NAMES),
        "state": len(STATE_FEATURE_NAMES),
        "chunk_summary": len(CHUNK_SUMMARY_NAMES),
        "relation": len(RELATION_FEATURE_NAMES),
        "uarch": len(UARCH_FEATURE_NAMES),
    }
    for key, value in required_dims.items():
        if int(dimensions.get(key, -1)) != value:
            raise RuntimeError(f"v29 cache dimension mismatch {key}")


class V29TraceStore:
    def __init__(self, cache_dir: str) -> None:
        if np is None:
            raise RuntimeError("numpy is required to load v29 caches")
        self.cache_dir = os.path.abspath(cache_dir)
        self.meta = load_json(os.path.join(self.cache_dir, "meta.json"))
        _check_contract(self.meta)
        if self.meta.get("quality", {}).get("status") != "pass":
            raise RuntimeError(f"v29 cache quality is not pass: {self.cache_dir}")
        self.trace_id = str(self.meta["trace_id"])
        self.K = int(self.meta["K"])
        if self.K != 256:
            raise RuntimeError("v29 requires K=256")
        self.horizons = normalized_horizons(self.meta["horizons"])
        self.tpc = float(self.meta["tick_per_cycle"])
        self.sample_period_cycles = float(self.meta["sample_period_cycles"])
        self.core_ids = [int(value) for value in self.meta["core_ids"]]
        self.core_meta = {
            int(item["core_id"]): dict(item) for item in self.meta["cores"]
        }
        self.uarch_features = [float(value) for value in self.meta["uarch_features"]]
        self.sample_ticks = np.load(
            os.path.join(self.cache_dir, "sample_ticks.npy"), mmap_mode="r"
        )
        self.sample_cursors = np.load(
            os.path.join(self.cache_dir, "sample_cursors.npy"), mmap_mode="r"
        )
        self.sample_block_ids = np.load(
            os.path.join(self.cache_dir, "sample_block_ids.npy"), mmap_mode="r"
        )
        if self.sample_cursors.shape != (len(self.sample_ticks), len(self.core_ids)):
            raise RuntimeError("v29 sample cursor shape mismatch")
        self.cores: Dict[int, Dict[str, Any]] = {}
        for core_id in self.core_ids:
            core_dir = os.path.join(self.cache_dir, "cores", str(core_id))
            arrays = {
                name: np.load(os.path.join(core_dir, name + ".npy"), mmap_mode="r")
                for name in CORE_ARRAY_NAMES
            }
            n_uops = int(self.core_meta[core_id]["n_uops"])
            if any(int(array.shape[0]) != n_uops for array in arrays.values()):
                raise RuntimeError(f"v29 core array length mismatch core={core_id}")
            if int(arrays["fields"].shape[1]) != len(FIELD_NAMES):
                raise RuntimeError("v29 static field dimension mismatch")
            if int(arrays["resource"].shape[1]) != len(RESOURCE_KEY_NAMES):
                raise RuntimeError("v29 resource key dimension mismatch")
            self.cores[core_id] = arrays

    def __len__(self) -> int:
        return len(self.sample_ticks)

    @staticmethod
    def _pad_1d(values: Sequence[Any], K: int, pad: Any) -> List[Any]:
        out = list(values[:K])
        out.extend([pad] * (K - len(out)))
        return out

    def window(self, core_id: int, cursor: int) -> Dict[str, Any]:
        arrays = self.cores[int(core_id)]
        cursor = int(cursor)
        count = int(arrays["commit_tick"].shape[0])
        if not 0 <= cursor < count:
            raise IndexError(f"cursor {cursor} outside core {core_id} length {count}")
        end = min(count, cursor + self.K)
        n_valid = end - cursor
        valid = [1] * n_valid + [0] * (self.K - n_valid)
        resource = [list(map(int, row)) for row in arrays["resource"][cursor:end]]
        resource.extend([
            [RESOURCE_KEY_INVALID] * len(RESOURCE_KEY_NAMES)
            for _ in range(self.K - n_valid)
        ])
        raw_fields = [list(map(int, row)) for row in arrays["fields"][cursor:end]]
        raw_fields.extend([list(FIELD_PAD_IDS) for _ in range(self.K - n_valid)])
        fields = apply_window_pressure(raw_fields, resource, valid)
        physical_lines = self._pad_1d(
            [int(value) for value in arrays["physical_line"][cursor:end]],
            self.K, -1,
        )
        access = self._pad_1d(
            [int(value) for value in arrays["access"][cursor:end]], self.K, 0,
        )
        semantic = self._pad_1d(
            [int(value) for value in arrays["semantic_flags"][cursor:end]],
            self.K, 0,
        )
        functional_lines = self._pad_1d(
            [int(value) for value in arrays["functional_line"][cursor:end]],
            self.K, -1,
        )
        functional_pages = self._pad_1d(
            [int(value) for value in arrays["functional_page"][cursor:end]],
            self.K, -1,
        )
        producer_logs = self._pad_1d(
            [float(value) for value in arrays["producer_log"][cursor:end]],
            self.K, 0.0,
        )
        macro_pcs = self._pad_1d(
            [int(value) for value in arrays["macro_pc"][cursor:end]], self.K, 0,
        )
        macro_end = self._pad_1d(
            [int(value) for value in arrays["macro_end"][cursor:end]], self.K, 0,
        )
        branch = self._pad_1d(
            [int(value) for value in arrays["branch"][cursor:end]], self.K, 0,
        )
        branch_miss = self._pad_1d(
            [int(value) for value in arrays["branch_miss"][cursor:end]], self.K, 0,
        )
        commit_ticks = self._pad_1d(
            [int(value) for value in arrays["commit_tick"][cursor:end]], self.K, 0,
        )
        read_lines = sorted({
            line for line, kind, is_valid in zip(physical_lines, access, valid)
            if is_valid and line >= 0 and kind in (1, 3)
        })
        write_lines = sorted({
            line for line, kind, is_valid in zip(physical_lines, access, valid)
            if is_valid and line >= 0 and kind in (2, 3)
        })
        chunk: Dict[str, Any] = {
            "core_id": int(core_id),
            "cursor": cursor,
            "n_uops": n_valid,
            "per_uop_fields": fields,
            "per_uop_resource_keys": resource,
            "per_uop_lines": physical_lines,
            "per_uop_access": access,
            "valid_uop_mask": valid,
            "semantic_flags": semantic,
            "functional_lines": functional_lines,
            "functional_pages": functional_pages,
            "producer_logs": producer_logs,
            "macro_pcs": macro_pcs,
            "macro_end": macro_end,
            "branch": branch,
            "branch_miss": branch_miss,
            "commit_ticks": commit_ticks,
            "read_lines": read_lines,
            "write_lines": write_lines,
        }
        chunk["chunk_summary"] = summarize_window(chunk, self.K)
        return chunk

    def context_from_cursors(
        self,
        cursors: Sequence[int],
        *,
        state_time_tick: Optional[int] = None,
        state_time_cycles: Optional[float] = None,
        include_labels: bool,
        last_commit_cycles: Optional[Mapping[int, float]] = None,
    ) -> Dict[str, Any]:
        if len(cursors) != len(self.core_ids):
            raise ValueError("cursor vector/core count mismatch")
        if include_labels and state_time_tick is None:
            raise ValueError("oracle labels require state_time_tick")
        if state_time_cycles is None:
            if state_time_tick is None:
                raise ValueError("state time is required")
            origin = min(int(meta["roi_begin_tick"]) for meta in self.core_meta.values())
            state_time_cycles = (int(state_time_tick) - origin) / self.tpc
        entries = [
            (slot, core_id, int(cursors[slot]))
            for slot, core_id in enumerate(self.core_ids)
            if int(cursors[slot]) >= 0
            and int(cursors[slot]) < int(self.core_meta[core_id]["n_uops"])
        ]
        if not entries:
            raise RuntimeError("empty v29 active context")
        chunks = [self.window(core_id, cursor) for slot, core_id, cursor in entries]
        dynamic, relations = context_features(chunks)
        active_fraction = len(entries) / max(1, len(self.core_ids))
        state_features = []
        commit_targets = []
        prefix_targets = []
        progress_targets = []
        for (_slot, core_id, cursor), chunk in zip(entries, chunks):
            core_meta = self.core_meta[core_id]
            if last_commit_cycles is not None:
                previous_cycles = float(last_commit_cycles.get(core_id, 0.0))
                elapsed = max(0.0, float(state_time_cycles) - previous_cycles)
                roi_begin_cycles = 0.0
            else:
                tick = int(state_time_tick) if state_time_tick is not None else 0
                previous_tick = (
                    int(self.cores[core_id]["commit_tick"][cursor - 1])
                    if cursor > 0 else int(core_meta["roi_begin_tick"])
                )
                elapsed = max(0.0, (tick - previous_tick) / self.tpc)
                origin = min(int(meta["roi_begin_tick"]) for meta in self.core_meta.values())
                roi_begin_cycles = (int(core_meta["roi_begin_tick"]) - origin) / self.tpc
            roi_age = max(0.0, float(state_time_cycles) - roi_begin_cycles)
            state_features.append([
                math.log1p(elapsed) / 8.0,
                math.log1p(elapsed) / 8.0,
                math.log1p(roi_age) / 16.0,
                float(cursor == 0),
                active_fraction,
            ])
            if include_labels:
                tick = int(state_time_tick)
                tau = [
                    (int(commit) - tick) / self.tpc if valid else 0.0
                    for commit, valid in zip(
                        chunk["commit_ticks"], chunk["valid_uop_mask"],
                    )
                ]
                if any(
                    value <= 0 for value, valid in zip(tau, chunk["valid_uop_mask"])
                    if valid
                ):
                    raise RuntimeError(
                        f"oracle cursor is not the first unretired UOP core={core_id}"
                    )
                prefix = [
                    [float(valid and value <= horizon) for horizon in self.horizons]
                    for value, valid in zip(tau, chunk["valid_uop_mask"])
                ]
                commit_targets.append(tau)
                prefix_targets.append(prefix)
                progress_targets.append([
                    sum(row[horizon_index] for row in prefix)
                    for horizon_index in range(len(self.horizons))
                ])
        if torch is None:
            raise RuntimeError("torch is required for v29 dataset contexts")
        t = torch
        result: Dict[str, Any] = {
            "per_uop_fields": t.tensor(
                [chunk["per_uop_fields"] for chunk in chunks], dtype=t.long,
            ),
            "dynamic_uop_fields": t.tensor(dynamic, dtype=t.long),
            "valid_uop_mask": t.tensor(
                [chunk["valid_uop_mask"] for chunk in chunks], dtype=t.bool,
            ),
            "chunk_summary": t.tensor(
                [chunk["chunk_summary"] for chunk in chunks], dtype=t.float32,
            ),
            "relation_features": t.tensor(relations, dtype=t.float32),
            "uarch_features": t.tensor(
                [self.uarch_features for _ in chunks], dtype=t.float32,
            ),
            "state_features": t.tensor(state_features, dtype=t.float32),
            "branch_mask": t.tensor(
                [chunk["branch"] for chunk in chunks], dtype=t.bool,
            ),
            "branch_miss_target": t.tensor(
                [chunk["branch_miss"] for chunk in chunks], dtype=t.float32,
            ),
            "macro_end": t.tensor(
                [chunk["macro_end"] for chunk in chunks], dtype=t.bool,
            ),
            "core_slots": t.tensor([slot for slot, _, _ in entries], dtype=t.long),
            "cursors": t.tensor([cursor for _, _, cursor in entries], dtype=t.long),
            "trace_id": self.trace_id,
            "state_time_cycles": float(state_time_cycles),
        }
        if include_labels:
            result.update({
                "commit_time_target": t.tensor(commit_targets, dtype=t.float32),
                "prefix_target": t.tensor(prefix_targets, dtype=t.float32),
                "progress_target": t.tensor(progress_targets, dtype=t.float32),
            })
        return result

    def context_at(self, sample_index: int) -> Dict[str, Any]:
        sample_index = int(sample_index)
        return self.context_from_cursors(
            self.sample_cursors[sample_index],
            state_time_tick=int(self.sample_ticks[sample_index]),
            include_labels=True,
        )


def _block_partition(trace_id: str, block_id: int, policy: Mapping[str, Any]) -> str:
    percent = int(policy.get("validation_percent", 10))
    if not 0 < percent < 100:
        raise ValueError("validation_percent must be in (0,100)")
    seed = int(policy.get("seed", 20260716))
    payload = f"{seed}:{trace_id}:block:{int(block_id)}".encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big") % 100
    return "validation" if bucket < percent else "train"


def _eligible_indices(
    store: V29TraceStore, policy: Optional[Mapping[str, Any]],
) -> List[int]:
    if not policy:
        return list(range(len(store)))
    partition = str(policy.get("partition", "")).lower()
    if partition not in {"train", "validation"}:
        raise ValueError(f"unsupported v29 partition {partition!r}")
    guard_cycles = float(policy.get("guard_cycles", max(store.horizons)))
    block_cycles = float(store.meta["sample_grid"]["block_cycles"])
    start_tick = int(store.meta["sample_grid"]["start_tick"])
    out = []
    for index, (tick, block_id) in enumerate(zip(store.sample_ticks, store.sample_block_ids)):
        if _block_partition(store.trace_id, int(block_id), policy) != partition:
            continue
        position_cycles = (
            (int(tick) - start_tick) / store.tpc - int(block_id) * block_cycles
        )
        if position_cycles < guard_cycles or position_cycles >= block_cycles - guard_cycles:
            continue
        out.append(index)
    return out


class V29GlobalTimeDataset(Dataset):
    """Dataset items are short contiguous oracle-time sequences."""

    def __init__(
        self,
        sources: Sequence[Any],
        *,
        sequence_length: int = 4,
        sequence_stride: Optional[int] = None,
    ) -> None:
        if torch is None or np is None:
            raise RuntimeError("torch and numpy are required for v29 datasets")
        self.sequence_length = max(1, int(sequence_length))
        self.sequence_stride = max(1, int(
            sequence_stride if sequence_stride is not None else self.sequence_length
        ))
        self.stores: List[V29TraceStore] = []
        self.sequences: List[Tuple[int, Tuple[int, ...]]] = []
        self.sample_trace_ids: List[str] = []
        contracts = set()
        for source in sources:
            if isinstance(source, str):
                cache_dir = source
                policy = None
            elif isinstance(source, Mapping):
                cache_dir = str(
                    source.get("cache_dir", source.get("rollout_dir", ""))
                )
                policy = source.get("sample_split")
            else:
                raise TypeError(f"unsupported v29 source {type(source)!r}")
            store = V29TraceStore(cache_dir)
            store_index = len(self.stores)
            self.stores.append(store)
            contracts.add((
                store.horizons,
                store.sample_period_cycles,
                str(store.meta.get("predictor_hash", "")),
                str(store.meta.get("resource_decoder_hash", "")),
            ))
            eligible = _eligible_indices(store, policy)
            runs: List[List[int]] = []
            current: List[int] = []
            for index in eligible:
                if (
                    current
                    and (
                        index != current[-1] + 1
                        or int(store.sample_block_ids[index])
                        != int(store.sample_block_ids[current[-1]])
                    )
                ):
                    runs.append(current)
                    current = []
                current.append(index)
            if current:
                runs.append(current)
            for run in runs:
                if len(run) < self.sequence_length:
                    continue
                for offset in range(
                    0, len(run) - self.sequence_length + 1, self.sequence_stride,
                ):
                    indices = tuple(run[offset:offset + self.sequence_length])
                    self.sequences.append((store_index, indices))
                    self.sample_trace_ids.append(store.trace_id)
        if len(contracts) != 1:
            raise RuntimeError(
                "one v29 training run requires identical horizon/period/predictor/decoder "
                f"contracts, got {len(contracts)}"
            )
        if not self.sequences:
            raise RuntimeError("v29 dataset has no eligible sequences")
        self.contract = next(iter(contracts))
        self.trace_sample_counts = Counter(self.sample_trace_ids)

    def __len__(self) -> int:
        return len(self.sequences)

    def __getitem__(self, index: int) -> Dict[str, Any]:
        store_index, sample_indices = self.sequences[int(index)]
        store = self.stores[store_index]
        return {
            "contexts": [store.context_at(sample) for sample in sample_indices],
            "trace_id": store.trace_id,
            "sample_indices": sample_indices,
            "sample_period_cycles": store.sample_period_cycles,
            "horizons": store.horizons,
        }


def collate_v29_sequences(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if torch is None:
        raise RuntimeError("torch is required")
    tensor_keys = (
        "per_uop_fields", "dynamic_uop_fields", "valid_uop_mask",
        "chunk_summary", "relation_features", "uarch_features", "state_features",
        "branch_mask", "branch_miss_target", "macro_end", "core_slots", "cursors",
        "commit_time_target", "prefix_target", "progress_target",
    )
    values: Dict[str, List[Any]] = {key: [] for key in tensor_keys}
    sample_ptr = [0]
    sequence_ptr = [0]
    row_sequence: List[Any] = []
    row_sequence_step: List[Any] = []
    trace_ids = []
    sample_indices: List[int] = []
    context_count = 0
    for sequence_index, item in enumerate(items):
        trace_ids.append(str(item["trace_id"]))
        for step, context in enumerate(item["contexts"]):
            rows = int(context["core_slots"].shape[0])
            for key in tensor_keys:
                values[key].append(context[key])
            sample_ptr.append(sample_ptr[-1] + rows)
            row_sequence.append(torch.full((rows,), sequence_index, dtype=torch.long))
            row_sequence_step.append(torch.full((rows,), step, dtype=torch.long))
            sample_indices.append(int(item["sample_indices"][step]))
            context_count += 1
        sequence_ptr.append(context_count)
    result = {
        key: torch.cat(parts, dim=0) for key, parts in values.items()
    }
    result.update({
        "sample_ptr": torch.tensor(sample_ptr, dtype=torch.long),
        "sequence_ptr": torch.tensor(sequence_ptr, dtype=torch.long),
        "row_sequence": torch.cat(row_sequence, dim=0),
        "row_sequence_step": torch.cat(row_sequence_step, dim=0),
        "sample_indices": torch.tensor(sample_indices, dtype=torch.long),
        "trace_id": trace_ids,
        "sample_period_cycles": float(items[0]["sample_period_cycles"]),
        "horizons": torch.tensor(items[0]["horizons"], dtype=torch.float32),
    })
    return result
