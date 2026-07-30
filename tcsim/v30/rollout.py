"""Deployment-time causal GSS coordination for free-running rollout.

The training sidecar is deliberately not read here.  One canonical cache state
contains only functional memory UOPs accepted by the scheduler.  Each model
window is generated from a disposable touched-set copy-on-write preview, and
the accepted prefix is replayed in predicted commit-cycle order.
"""
from __future__ import annotations

from dataclasses import asdict
import math
import os
import time
from typing import (
    Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence,
    Tuple,
)

import numpy as np
import torch

from ..v29.contracts import RESOURCE_KEY_INDEX
from .gss import (
    GSS_CATEGORICAL_FIELDS,
    GSS_CONTINUOUS_FIELDS,
    GSS_SCHEMA_VERSION,
    GSSFeatureEngine,
    GSSGeometry,
)
from .sidecar import GSS_SIDECAR_SCHEMA
from .native import NativeGSSFeatureEngine, load_native_gss


GSS_SERIAL_ROLLOUT_CONTRACT = "commit-clock-single-qkvr-deadline-v2"
GSS_PREVIEW_ORDER_POLICY = (
    "retained-or-base-predicted-commit-cycle-then-core-id-uop-v2"
)
GSS_PARALLEL_ROLLOUT_CONTRACT = (
    "parallel-relaxed-continuous-shadow-deadline-v2"
)
GSS_PARALLEL_PREVIEW_ORDER_POLICY = (
    "retained-deadline-then-relative-uop-core-id-uop-v2"
)
GSS_COMMIT_ORDER_POLICY = "final-predicted-commit-cycle-then-core-id-uop-v2"


def _tensor_values(value: Any) -> np.ndarray:
    if isinstance(value, torch.Tensor):
        return value.detach().cpu().numpy()
    return np.asarray(value)


class GSSSerialRollout:
    """Own one canonical state for serial or window-parallel evaluation."""

    def __init__(
        self,
        store: Any,
        checkpoint_contract: Mapping[str, Any],
        *,
        rollout_mode: str = "serial",
        allow_ready_clock_compat: bool = False,
        feature_mode: str = "full",
    ) -> None:
        self.store = store
        self.contract = dict(checkpoint_contract)
        self.rollout_mode = str(rollout_mode).strip().lower()
        self.allow_ready_clock_compat = bool(allow_ready_clock_compat)
        self.feature_mode = str(feature_mode).strip().lower()
        if self.feature_mode not in {"full", "state-disabled"}:
            raise ValueError(
                "GSS rollout feature_mode must be full or state-disabled"
            )
        self.ready_clock_compat = False
        if self.rollout_mode not in {"serial", "unconditional", "speculative"}:
            raise ValueError("unsupported GSS rollout mode")
        self.geometry = GSSGeometry.from_trace_meta(store.meta)
        self._validate_contract()
        requested_backend = os.environ.get("TCSIM_GSS_BACKEND", "auto").lower()
        if requested_backend not in {"auto", "native", "python"}:
            raise ValueError("TCSIM_GSS_BACKEND must be auto, native, or python")
        native_available = load_native_gss() is not None
        if requested_backend == "native" and not native_available:
            raise RuntimeError(
                "TCSIM_GSS_BACKEND=native but the extension is unavailable; "
                "run scripts/build_v30_gss_native.py"
            )
        if requested_backend != "python" and native_available:
            self.canonical = NativeGSSFeatureEngine(self.geometry)
            self.backend = self.canonical.backend_name
        else:
            self.canonical = GSSFeatureEngine(self.geometry)
            self.backend = "python-reference-batch-v1"
        indexing_started = time.perf_counter()
        self._memory_positions: Dict[int, np.ndarray] = {}
        self._memory_events: Dict[int, np.ndarray] = {}
        resource_columns = [
            RESOURCE_KEY_INDEX[name] for name in (
                "physical_line", "l1_set", "l2_set", "llc_set", "llc_bank",
            )
        ]
        for core_id in store.core_ids:
            core = int(core_id)
            arrays = store.cores[core]
            access = np.asarray(arrays["access"], dtype=np.uint8)
            positions = np.flatnonzero(access > 0).astype(np.int64, copy=False)
            descriptors = np.empty((len(positions), 7), dtype=np.int64)
            descriptors[:, 0] = core
            if len(positions):
                resource = np.asarray(arrays["resource"])
                descriptors[:, 1:6] = resource[np.ix_(positions, resource_columns)]
                descriptors[:, 6] = access[positions]
            self._memory_positions[core] = positions
            self._memory_events[core] = descriptors
        self.index_build_seconds = time.perf_counter() - indexing_started
        self.index_memory_bytes = int(sum(
            value.nbytes for value in self._memory_positions.values()
        ) + sum(value.nbytes for value in self._memory_events.values()))
        self._allocate_buffers(len(store.core_ids), int(store.K))
        self.committed_cursors = {
            int(core_id): 0 for core_id in store.core_ids
        }
        self.preview_calls = 0
        self.preview_memory_uops = 0
        self.preview_invalid_paddr = 0
        self.preview_retained_deadlines = 0
        self.preview_provisional_deadlines = 0
        self.preview_seconds = 0.0
        self.parallel_preview_waves = 0
        self.parallel_preview_lanes = 0
        self.parallel_lane_memory_uops = 0
        self.parallel_retained_deadline_uops = 0
        self.parallel_fallback_order_uops = 0
        self.commit_calls = 0
        self.committed_memory_uops = 0
        self.committed_invalid_paddr = 0
        self.commit_seconds = 0.0
        self._last_committed_event_cycle = -math.inf

    @staticmethod
    def _cpu_buffer(shape: Tuple[int, ...], dtype: torch.dtype) -> torch.Tensor:
        try:
            return torch.empty(
                shape, dtype=dtype, pin_memory=torch.cuda.is_available(),
            )
        except RuntimeError:
            return torch.empty(shape, dtype=dtype)

    def _allocate_buffers(self, rows: int, length: int) -> None:
        self._categorical_buffer = self._cpu_buffer(
            (rows, length, len(GSS_CATEGORICAL_FIELDS)), torch.int64,
        )
        self._continuous_buffer = self._cpu_buffer(
            (rows, length, len(GSS_CONTINUOUS_FIELDS)), torch.float32,
        )
        self._memory_mask_buffer = self._cpu_buffer(
            (rows, length), torch.bool,
        )
        # One extra column covers a zero sentinel when position zero is not a
        # real memory event.  Only the used prefix is transferred to the GPU.
        compact = length + 1
        self._event_categorical_buffer = self._cpu_buffer(
            (rows, compact, len(GSS_CATEGORICAL_FIELDS)), torch.int64,
        )
        self._event_continuous_buffer = self._cpu_buffer(
            (rows, compact, len(GSS_CONTINUOUS_FIELDS)), torch.float32,
        )
        self._event_position_buffer = self._cpu_buffer(
            (rows, compact), torch.int64,
        )
        self._event_valid_buffer = self._cpu_buffer(
            (rows, compact), torch.bool,
        )
        self._event_memory_buffer = self._cpu_buffer(
            (rows, compact), torch.bool,
        )

    def _validate_contract(self) -> None:
        contract = self.contract
        if contract.get("schema_version") != GSS_SIDECAR_SCHEMA:
            raise RuntimeError("checkpoint has an unsupported v30 GSS schema")
        if contract.get("engine_schema") != GSS_SCHEMA_VERSION:
            raise RuntimeError("checkpoint/runtime GSS engine schema mismatch")
        if not bool(contract.get("features_are_pre_access")):
            raise RuntimeError("checkpoint GSS features are not pre-access")
        if bool(contract.get("timestamp_is_model_visible")):
            raise RuntimeError("checkpoint GSS contract exposes teacher time")
        clock_source = str(contract.get("clock_source", ""))
        order_policy = str(contract.get("order_policy", ""))
        if clock_source == "commit":
            if order_policy != "commit_tick_then_core_then_uop_v1":
                raise RuntimeError(
                    "checkpoint/runtime GSS event-order contract mismatch"
                )
        elif clock_source == "ready" and self.allow_ready_clock_compat:
            if order_policy != "ready_tick_then_core_then_uop_v1":
                raise RuntimeError(
                    "ready-clock GSS compatibility order-policy mismatch"
                )
            self.ready_clock_compat = True
        else:
            raise RuntimeError(
                "online v30 requires commit-clock GSS training data; a legacy "
                "ready-clock checkpoint is allowed only through the explicit "
                "throughput/exploratory compatibility option"
            )
        if tuple(contract.get("categorical_fields", ())) != GSS_CATEGORICAL_FIELDS:
            raise RuntimeError("checkpoint/runtime GSS categorical fields mismatch")
        if tuple(contract.get("continuous_fields", ())) != GSS_CONTINUOUS_FIELDS:
            raise RuntimeError("checkpoint/runtime GSS continuous fields mismatch")
        if contract.get("categorical_dtype") != "uint8":
            raise RuntimeError("checkpoint GSS categorical dtype mismatch")
        if contract.get("continuous_dtype") != "float16":
            raise RuntimeError("checkpoint GSS continuous dtype mismatch")
        if dict(contract.get("geometry", {})) != asdict(self.geometry):
            raise RuntimeError(
                f"checkpoint/trace GSS geometry mismatch for {self.store.trace_id}"
            )
        expected_replacement = {
            "l1d": "lru", "l2": "tree_plru", "llc": "tree_plru",
        }
        if dict(contract.get("replacement", {})) != expected_replacement:
            raise RuntimeError("checkpoint/runtime GSS replacement policy mismatch")

    def _event_slice(
        self, core_id: int, cursor: int, count: int,
    ) -> Tuple[np.ndarray, np.ndarray]:
        positions = self._memory_positions[int(core_id)]
        begin = int(np.searchsorted(positions, int(cursor), side="left"))
        end = int(np.searchsorted(positions, int(cursor + count), side="left"))
        return positions[begin:end], self._memory_events[int(core_id)][begin:end]

    def augment_context(
        self,
        context: MutableMapping[str, Any],
        *,
        predicted_commit_time: np.ndarray,
        step_start_cycles: float,
        deadline_lookup: Optional[Callable[[int, int], Optional[float]]] = None,
    ) -> MutableMapping[str, Any]:
        """Build pre-access features in the model's predicted commit order.

        Existing absolute deadlines are authoritative for overlapping UOPs.
        Newly exposed tail UOPs use the no-GSS commit-cycle projection from the
        same QKVR state.  Thus the cache transition and scheduler both operate
        in commit cycles without a second interaction forward.
        """
        started = time.perf_counter()
        valid = _tensor_values(context["valid_uop_mask"]).astype(
            np.bool_, copy=False,
        )
        slots = [int(value) for value in _tensor_values(context["core_slots"])]
        cursors = [int(value) for value in _tensor_values(context["cursors"])]
        rows, length = valid.shape
        predicted = np.asarray(predicted_commit_time, dtype=np.float64)
        if predicted.shape != (rows, length):
            raise RuntimeError(
                "online GSS provisional commit-time shape mismatch: "
                f"{predicted.shape} != {(rows, length)}"
            )
        categorical_tensor = self._categorical_buffer[:rows, :length]
        continuous_tensor = self._continuous_buffer[:rows, :length]
        memory_mask_tensor = self._memory_mask_buffer[:rows, :length]
        categorical_tensor.zero_()
        continuous_tensor.zero_()
        memory_mask_tensor.zero_()
        categorical = categorical_tensor.numpy()
        continuous = continuous_tensor.numpy()
        memory_mask = memory_mask_tensor.numpy()
        event_chunks: List[np.ndarray] = []
        row_chunks: List[np.ndarray] = []
        local_chunks: List[np.ndarray] = []
        core_chunks: List[np.ndarray] = []
        absolute_chunks: List[np.ndarray] = []
        cycle_chunks: List[np.ndarray] = []
        retained_count = 0
        for row, (slot, cursor) in enumerate(zip(slots, cursors)):
            core_id = int(self.store.core_ids[slot])
            expected_cursor = int(self.committed_cursors[core_id])
            if cursor != expected_cursor:
                raise RuntimeError(
                    "online GSS/context cursor mismatch: "
                    f"core={core_id} context={cursor} canonical={expected_cursor}"
                )
            valid_count = int(valid[row].sum())
            absolute, descriptors = self._event_slice(
                core_id, cursor, valid_count,
            )
            if not len(absolute):
                continue
            local = absolute - cursor
            event_chunks.append(descriptors)
            row_chunks.append(np.full(len(local), row, dtype=np.int64))
            local_chunks.append(local)
            core_chunks.append(np.full(len(local), core_id, dtype=np.int64))
            absolute_chunks.append(absolute)
            row_cycles = (
                float(step_start_cycles)
                + np.asarray(predicted[row, :valid_count], dtype=np.float64)
            )
            retained_prefix = 0
            if deadline_lookup is not None:
                row_cycles = row_cycles.copy()
                for relative_uop in range(valid_count):
                    retained = deadline_lookup(
                        core_id, cursor + relative_uop,
                    )
                    if retained is None:
                        break
                    row_cycles[relative_uop] = float(retained)
                    retained_prefix += 1
                if 0 < retained_prefix < valid_count:
                    provisional_gap = np.diff(np.concatenate((
                        [0.0], predicted[row, :valid_count],
                    )))
                    row_cycles[retained_prefix:] = (
                        row_cycles[retained_prefix - 1]
                        + np.cumsum(np.maximum(
                            provisional_gap[retained_prefix:], 1.0e-6,
                        ))
                    )
            cycles = row_cycles[local]
            retained_count += int(np.count_nonzero(local < retained_prefix))
            if not np.isfinite(cycles).all():
                raise RuntimeError("online GSS received a non-finite preview clock")
            if len(row_cycles) > 1 and np.any(np.diff(row_cycles) < -1.0e-6):
                raise RuntimeError("online GSS preview clock is non-monotonic")
            cycle_chunks.append(np.asarray(cycles, dtype=np.float64))
            memory_mask[row, local] = True
        if event_chunks:
            event_values = np.concatenate(event_chunks)
            event_rows = np.concatenate(row_chunks)
            event_local = np.concatenate(local_chunks)
            event_cores = np.concatenate(core_chunks)
            event_absolute = np.concatenate(absolute_chunks)
            event_cycles = np.concatenate(cycle_chunks)
            order = np.lexsort((event_absolute, event_cores, event_cycles))
            event_values = np.ascontiguousarray(event_values[order])
            event_rows = event_rows[order]
            event_local = event_local[order]
        else:
            event_values = np.empty((0, 7), dtype=np.int64)
            event_rows = np.empty(0, dtype=np.int64)
            event_local = np.empty(0, dtype=np.int64)
        event_categorical, event_continuous = self.canonical.preview_batch(
            event_values,
        )
        if len(event_values):
            categorical[event_rows, event_local] = event_categorical
            continuous[event_rows, event_local] = event_continuous

        per_row_indices = [np.flatnonzero(event_rows == row) for row in range(rows)]
        compact_counts = [
            len(indices) + int(
                len(indices) == 0 or int(event_local[indices[0]]) != 0
            )
            for indices in per_row_indices
        ]
        compact_width = max(1, max(compact_counts, default=1))
        compact_categorical = self._event_categorical_buffer[
            :rows, :compact_width
        ]
        compact_continuous = self._event_continuous_buffer[
            :rows, :compact_width
        ]
        compact_position = self._event_position_buffer[:rows, :compact_width]
        compact_valid = self._event_valid_buffer[:rows, :compact_width]
        compact_memory = self._event_memory_buffer[:rows, :compact_width]
        for value in (
            compact_categorical, compact_continuous, compact_position,
            compact_valid, compact_memory,
        ):
            value.zero_()
        compact_categorical_np = compact_categorical.numpy()
        compact_continuous_np = compact_continuous.numpy()
        compact_position_np = compact_position.numpy()
        compact_valid_np = compact_valid.numpy()
        compact_memory_np = compact_memory.numpy()
        for row, indices in enumerate(per_row_indices):
            sentinel = int(
                len(indices) == 0 or int(event_local[indices[0]]) != 0
            )
            compact_valid_np[row, 0] = True
            if not len(indices):
                continue
            destination = np.arange(len(indices), dtype=np.int64) + sentinel
            compact_categorical_np[row, destination] = event_categorical[indices]
            compact_continuous_np[row, destination] = event_continuous[indices]
            compact_position_np[row, destination] = event_local[indices]
            compact_valid_np[row, destination] = True
            compact_memory_np[row, destination] = True

        context["gss_uop_categorical"] = categorical_tensor
        context["gss_uop_continuous"] = continuous_tensor
        context["gss_memory_mask"] = memory_mask_tensor
        context["gss_event_categorical"] = compact_categorical
        context["gss_event_continuous"] = compact_continuous
        context["gss_event_positions"] = compact_position
        context["gss_event_valid"] = compact_valid
        context["gss_event_is_memory"] = compact_memory
        if self.feature_mode == "state-disabled":
            # Preserve the memory-event geometry, compact attention layout,
            # adapter/router execution and canonical-state cost while removing
            # every cache-state value.  This isolates state content from the
            # memory-mask/event-position shortcut in one checkpoint.
            categorical_tensor.zero_()
            continuous_tensor.zero_()
            compact_categorical.zero_()
            compact_continuous.zero_()
        self.preview_calls += 1
        self.preview_memory_uops += len(event_values)
        self.preview_invalid_paddr += int(np.count_nonzero(event_values[:, 1] < 0))
        self.preview_retained_deadlines += int(retained_count)
        self.preview_provisional_deadlines += int(len(event_values) - retained_count)
        self.preview_seconds += time.perf_counter() - started
        return context

    def augment_contexts(
        self,
        contexts: Sequence[MutableMapping[str, Any]],
        *,
        anchor_cursors: Sequence[int],
        deadline_lookup: Optional[
            Callable[[int, int], Optional[float]]
        ] = None,
    ) -> Sequence[MutableMapping[str, Any]]:
        """Attach lane tensors from one continuous transactional preview.

        The union starts at the authoritative cursor and extends through the
        deepest lane.  Overlapping UOPs are evaluated once, then sliced into
        stable per-lane buffers that remain alive for the whole parallel wave.
        """
        started = time.perf_counter()
        anchors = [int(value) for value in anchor_cursors]
        if len(anchors) != len(self.store.core_ids):
            raise RuntimeError("parallel GSS anchor cursor count mismatch")
        for slot, core_id_value in enumerate(self.store.core_ids):
            core_id = int(core_id_value)
            if anchors[slot] != int(self.committed_cursors[core_id]):
                raise RuntimeError(
                    "parallel GSS anchor/canonical cursor mismatch: "
                    f"core={core_id} anchor={anchors[slot]} "
                    f"canonical={self.committed_cursors[core_id]}"
                )
        ends = list(anchors)
        lane_memory_uops = 0
        for context in contexts:
            valid = _tensor_values(context["valid_uop_mask"]).astype(
                np.bool_, copy=False,
            )
            slots = [int(value) for value in _tensor_values(context["core_slots"])]
            cursors = [int(value) for value in _tensor_values(context["cursors"])]
            for row, (slot, cursor) in enumerate(zip(slots, cursors)):
                valid_count = int(valid[row].sum())
                ends[slot] = max(ends[slot], cursor + valid_count)
                absolute, _ = self._event_slice(
                    int(self.store.core_ids[slot]), cursor, valid_count,
                )
                lane_memory_uops += len(absolute)

        event_chunks: List[np.ndarray] = []
        core_chunks: List[np.ndarray] = []
        absolute_chunks: List[np.ndarray] = []
        deadline_class_chunks: List[np.ndarray] = []
        order_value_chunks: List[np.ndarray] = []
        retained_deadline_uops = 0
        fallback_order_uops = 0
        for slot, core_id_value in enumerate(self.store.core_ids):
            core_id = int(core_id_value)
            count = max(0, int(ends[slot]) - anchors[slot])
            absolute, descriptors = self._event_slice(
                core_id, anchors[slot], count,
            )
            if not len(absolute):
                continue
            event_chunks.append(descriptors)
            core_chunks.append(
                np.full(len(absolute), core_id, dtype=np.int64)
            )
            absolute_chunks.append(absolute)
            relative = absolute - anchors[slot]
            deadline_class = np.ones(len(absolute), dtype=np.uint8)
            order_value = relative.astype(np.float64, copy=True)
            if deadline_lookup is not None:
                for event_index, absolute_uop in enumerate(absolute):
                    deadline = deadline_lookup(core_id, int(absolute_uop))
                    if deadline is None:
                        continue
                    if not math.isfinite(float(deadline)):
                        raise RuntimeError(
                            "parallel GSS received a non-finite retained deadline"
                        )
                    deadline_class[event_index] = 0
                    order_value[event_index] = float(deadline)
            retained_deadline_uops += int(np.count_nonzero(
                deadline_class == 0,
            ))
            fallback_order_uops += int(np.count_nonzero(
                deadline_class != 0,
            ))
            deadline_class_chunks.append(deadline_class)
            order_value_chunks.append(order_value)
        if event_chunks:
            event_values = np.concatenate(event_chunks)
            event_cores = np.concatenate(core_chunks)
            event_absolute = np.concatenate(absolute_chunks)
            event_deadline_class = np.concatenate(deadline_class_chunks)
            event_order_value = np.concatenate(order_value_chunks)
            # Retained absolute deadlines are authoritative.  A newly exposed
            # tail has no final clock before its lane forward, so all such UOPs
            # follow the retained prefix and use a stable approximation:
            # relative UOP ordinal, architectural core ID, absolute ordinal.
            order = np.lexsort((
                event_absolute,
                event_cores,
                event_order_value,
                event_deadline_class,
            ))
            event_values = np.ascontiguousarray(event_values[order])
            event_absolute = event_absolute[order]
        else:
            event_values = np.empty((0, 7), dtype=np.int64)
            event_absolute = np.empty(0, dtype=np.int64)
        event_categorical, event_continuous = self.canonical.preview_batch(
            event_values,
        )
        lookup = {
            (int(event_values[index, 0]), int(event_absolute[index])): index
            for index in range(len(event_values))
        }
        for context in contexts:
            self._attach_parallel_context(
                context, lookup, event_categorical, event_continuous,
            )
        self.preview_calls += 1
        self.parallel_preview_waves += 1
        self.parallel_preview_lanes += len(contexts)
        self.parallel_lane_memory_uops += int(lane_memory_uops)
        self.parallel_retained_deadline_uops += retained_deadline_uops
        self.parallel_fallback_order_uops += fallback_order_uops
        self.preview_memory_uops += len(event_values)
        self.preview_invalid_paddr += int(
            np.count_nonzero(event_values[:, 1] < 0)
        )
        self.preview_seconds += time.perf_counter() - started
        return contexts

    def _attach_parallel_context(
        self,
        context: MutableMapping[str, Any],
        lookup: Mapping[Tuple[int, int], int],
        union_categorical: np.ndarray,
        union_continuous: np.ndarray,
    ) -> None:
        valid = _tensor_values(context["valid_uop_mask"]).astype(
            np.bool_, copy=False,
        )
        slots = [int(value) for value in _tensor_values(context["core_slots"])]
        cursors = [int(value) for value in _tensor_values(context["cursors"])]
        rows, length = valid.shape
        categorical_tensor = self._cpu_buffer(
            (rows, length, len(GSS_CATEGORICAL_FIELDS)), torch.int64,
        )
        continuous_tensor = self._cpu_buffer(
            (rows, length, len(GSS_CONTINUOUS_FIELDS)), torch.float32,
        )
        memory_mask_tensor = self._cpu_buffer((rows, length), torch.bool)
        categorical_tensor.zero_()
        continuous_tensor.zero_()
        memory_mask_tensor.zero_()
        categorical = categorical_tensor.numpy()
        continuous = continuous_tensor.numpy()
        memory_mask = memory_mask_tensor.numpy()
        row_positions: List[np.ndarray] = []
        row_feature_indices: List[np.ndarray] = []
        for row, (slot, cursor) in enumerate(zip(slots, cursors)):
            core_id = int(self.store.core_ids[slot])
            valid_count = int(valid[row].sum())
            absolute, _ = self._event_slice(core_id, cursor, valid_count)
            local = absolute - cursor
            indices = np.fromiter(
                (lookup[(core_id, int(value))] for value in absolute),
                dtype=np.int64, count=len(absolute),
            )
            if len(local):
                memory_mask[row, local] = True
                categorical[row, local] = union_categorical[indices]
                continuous[row, local] = union_continuous[indices]
            row_positions.append(local)
            row_feature_indices.append(indices)

        compact_counts = [
            len(local) + int(len(local) == 0 or int(local[0]) != 0)
            for local in row_positions
        ]
        compact_width = max(1, max(compact_counts, default=1))
        compact_categorical = self._cpu_buffer(
            (rows, compact_width, len(GSS_CATEGORICAL_FIELDS)), torch.int64,
        )
        compact_continuous = self._cpu_buffer(
            (rows, compact_width, len(GSS_CONTINUOUS_FIELDS)), torch.float32,
        )
        compact_position = self._cpu_buffer(
            (rows, compact_width), torch.int64,
        )
        compact_valid = self._cpu_buffer((rows, compact_width), torch.bool)
        compact_memory = self._cpu_buffer((rows, compact_width), torch.bool)
        for value in (
            compact_categorical, compact_continuous, compact_position,
            compact_valid, compact_memory,
        ):
            value.zero_()
        compact_categorical_np = compact_categorical.numpy()
        compact_continuous_np = compact_continuous.numpy()
        compact_position_np = compact_position.numpy()
        compact_valid_np = compact_valid.numpy()
        compact_memory_np = compact_memory.numpy()
        for row, (local, indices) in enumerate(zip(
            row_positions, row_feature_indices,
        )):
            sentinel = int(len(local) == 0 or int(local[0]) != 0)
            compact_valid_np[row, 0] = True
            if not len(local):
                continue
            destination = np.arange(len(local), dtype=np.int64) + sentinel
            compact_categorical_np[row, destination] = union_categorical[indices]
            compact_continuous_np[row, destination] = union_continuous[indices]
            compact_position_np[row, destination] = local
            compact_valid_np[row, destination] = True
            compact_memory_np[row, destination] = True
        context.update({
            "gss_uop_categorical": categorical_tensor,
            "gss_uop_continuous": continuous_tensor,
            "gss_memory_mask": memory_mask_tensor,
            "gss_event_categorical": compact_categorical,
            "gss_event_continuous": compact_continuous,
            "gss_event_positions": compact_position,
            "gss_event_valid": compact_valid,
            "gss_event_is_memory": compact_memory,
        })

    def commit_context(
        self,
        context: Mapping[str, Any],
        prediction: Any,
        consumed_by_row: Sequence[int],
        *,
        step_start_cycles: float,
    ) -> None:
        """Replay one serial accepted prefix."""
        slots = [int(value) for value in _tensor_values(context["core_slots"])]
        cursors = [int(value) for value in _tensor_values(context["cursors"])]
        self.commit_step(
            slots, cursors, prediction, consumed_by_row,
            step_start_cycles=step_start_cycles,
        )

    def commit_step(
        self,
        slots: Sequence[int],
        cursors: Sequence[int],
        prediction: Any,
        consumed_by_row: Sequence[int],
        *,
        step_start_cycles: float,
    ) -> None:
        """Commit only UOPs actually consumed by any scheduler mode."""
        started = time.perf_counter()
        slots = [int(value) for value in slots]
        cursors = [int(value) for value in cursors]
        if len(consumed_by_row) != len(slots):
            raise RuntimeError("online GSS accepted-prefix row count mismatch")
        event_chunks: List[np.ndarray] = []
        cycle_chunks: List[np.ndarray] = []
        core_chunks: List[np.ndarray] = []
        absolute_chunks: List[np.ndarray] = []
        advances = []
        for row, (slot, cursor, consumed_value) in enumerate(zip(
            slots, cursors, consumed_by_row,
        )):
            consumed = int(consumed_value)
            core_id = int(self.store.core_ids[slot])
            if cursor != int(self.committed_cursors[core_id]):
                raise RuntimeError(
                    f"online GSS commit cursor mismatch core={core_id}"
                )
            if consumed < 0 or consumed > int(prediction.valid_uop_mask[row].sum()):
                raise RuntimeError("online GSS received an invalid accepted prefix")
            absolute, descriptors = self._event_slice(core_id, cursor, consumed)
            if len(absolute):
                local = absolute - cursor
                cycles = float(step_start_cycles) + np.asarray(
                    prediction.commit_time[row, local], dtype=np.float64,
                )
                if not np.isfinite(cycles).all():
                    raise RuntimeError("online GSS received a non-finite event cycle")
                event_chunks.append(descriptors)
                cycle_chunks.append(cycles)
                core_chunks.append(np.full(len(local), core_id, dtype=np.int64))
                absolute_chunks.append(absolute)
            advances.append((core_id, cursor + consumed))
        if event_chunks:
            event_values = np.concatenate(event_chunks)
            event_cycles = np.concatenate(cycle_chunks)
            event_cores = np.concatenate(core_chunks)
            event_absolute = np.concatenate(absolute_chunks)
            order = np.lexsort((event_absolute, event_cores, event_cycles))
            event_values = np.ascontiguousarray(event_values[order])
            event_cycles = event_cycles[order]
            if float(event_cycles[0]) + 1.0e-6 < self._last_committed_event_cycle:
                raise RuntimeError("online GSS predicted event order moved backwards")
            self.canonical.commit_batch(event_values)
            self._last_committed_event_cycle = max(
                self._last_committed_event_cycle, float(event_cycles[-1]),
            )
        else:
            event_values = np.empty((0, 7), dtype=np.int64)
        for core_id, cursor in advances:
            self.committed_cursors[core_id] = int(cursor)
        self.commit_calls += 1
        self.committed_memory_uops += len(event_values)
        self.committed_invalid_paddr += int(
            np.count_nonzero(event_values[:, 1] < 0)
        )
        self.commit_seconds += time.perf_counter() - started

    def stats(self) -> Dict[str, Any]:
        parallel_relaxed = self.rollout_mode != "serial"
        if self.ready_clock_compat:
            accuracy_mode = (
                "parallel-relaxed-ready-clock-compat"
                if parallel_relaxed else "serial-ready-clock-compat"
            )
            accuracy_qualification = "exploratory-approximate"
        else:
            accuracy_mode = (
                "parallel-relaxed" if parallel_relaxed else "serial-exact"
            )
            accuracy_qualification = accuracy_mode
        return {
            "gss_rollout_contract": (
                GSS_PARALLEL_ROLLOUT_CONTRACT
                if parallel_relaxed else GSS_SERIAL_ROLLOUT_CONTRACT
            ),
            "gss_rollout_mode": self.rollout_mode,
            "gss_feature_mode": self.feature_mode,
            "gss_accuracy_mode": accuracy_mode,
            "gss_accuracy_qualification": accuracy_qualification,
            "gss_clock_contract_exact": not self.ready_clock_compat,
            "gss_formal_accuracy_valid": not self.ready_clock_compat,
            "gss_throughput_valid": True,
            "gss_mid_forward_global_sync": False,
            "gss_training_clock_source": self.contract.get("clock_source"),
            "gss_training_order_policy": self.contract.get("order_policy"),
            "gss_preview_order_policy": (
                GSS_PARALLEL_PREVIEW_ORDER_POLICY
                if parallel_relaxed else GSS_PREVIEW_ORDER_POLICY
            ),
            "gss_commit_order_policy": GSS_COMMIT_ORDER_POLICY,
            "gss_oracle_sidecar_consumed": False,
            "gss_hot_path_backend": self.backend,
            "gss_event_index_build_seconds": float(self.index_build_seconds),
            "gss_event_index_bytes": int(self.index_memory_bytes),
            "gss_preview_calls": int(self.preview_calls),
            "gss_preview_memory_uops": int(self.preview_memory_uops),
            "gss_preview_invalid_paddr": int(self.preview_invalid_paddr),
            "gss_preview_retained_deadlines": int(
                self.preview_retained_deadlines
            ),
            "gss_preview_provisional_deadlines": int(
                self.preview_provisional_deadlines
            ),
            "gss_preview_seconds": float(self.preview_seconds),
            "gss_parallel_preview_waves": int(self.parallel_preview_waves),
            "gss_parallel_preview_lanes": int(self.parallel_preview_lanes),
            "gss_parallel_lane_memory_uops": int(
                self.parallel_lane_memory_uops
            ),
            "gss_parallel_retained_deadline_uops": int(
                self.parallel_retained_deadline_uops
            ),
            "gss_parallel_fallback_order_uops": int(
                self.parallel_fallback_order_uops
            ),
            "gss_commit_calls": int(self.commit_calls),
            "gss_committed_memory_uops": int(self.committed_memory_uops),
            "gss_committed_invalid_paddr": int(self.committed_invalid_paddr),
            "gss_commit_seconds": float(self.commit_seconds),
            "gss_canonical_state": dict(self.canonical.state_summary()),
        }
