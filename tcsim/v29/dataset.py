"""Torch dataset over v29 common-time trace caches."""
from __future__ import annotations

import hashlib
import math
import os
import time
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..utils.io import load_json
from .contracts import (
    BRANCH_CONTRACT_VERSION,
    CHUNK_SUMMARY_NAMES,
    DATASET_SCHEMA_VERSION,
    DYNAMIC_PAD_IDS,
    DYNAMIC_FIELD_NAMES,
    FEATURE_SCHEMA_VERSION,
    FIELD_INDEX,
    FIELD_NAMES,
    FIELD_PAD_IDS,
    MODEL_INPUT_CONTRACT,
    RELATION_FEATURE_NAMES,
    RESOURCE_KEY_INDEX,
    RESOURCE_KEY_INVALID,
    RESOURCE_KEY_NAMES,
    STATE_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
    normalized_horizons,
)

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


FUNCTIONAL_CORE_ARRAY_NAMES = (
    "fields", "resource", "physical_line", "functional_line",
    "functional_page", "producer_log", "semantic_flags", "access", "macro_pc",
    "macro_end", "branch",
)
ORACLE_CORE_ARRAY_NAMES = ("commit_tick", "branch_miss")
CORE_ARRAY_NAMES = FUNCTIONAL_CORE_ARRAY_NAMES + ORACLE_CORE_ARRAY_NAMES
FUNCTIONAL_CONTAINER_SCHEMA = "tcsim-v29-functional-cache-3"
CONTEXT_TIMING_CONTRACT = "v29-context-phases-v1"
CONTEXT_BUILDER = "numpy-vectorized-v2"
CONTEXT_PHASE_NAMES = (
    "active_core_selection",
    "per_core_window",
    "cross_core_features",
    "state_and_targets",
    "tensor_assembly",
)


def _mixed_radix_codes(values: Any, maxima: Optional[Any] = None) -> Any:
    """Encode non-negative integer rows without collisions when int64 permits.

    The encoding is local to one context and is used only for equality/grouping.
    Returning ``None`` selects the exact multi-column fallback for unusually
    large values that cannot be represented safely in int64.
    """
    rows = np.asarray(values, dtype=np.int64)
    if rows.ndim == 1:
        rows = rows[:, None]
    if rows.ndim != 2:
        raise ValueError("v29 mixed-radix values must be a matrix")
    if not len(rows):
        return np.empty(0, dtype=np.int64)
    if np.any(rows < 0):
        raise ValueError("v29 mixed-radix values must be non-negative")
    row_maxima = (
        np.asarray(maxima, dtype=np.int64)
        if maxima is not None else rows.max(axis=0)
    )
    if row_maxima.shape != (rows.shape[1],):
        raise ValueError("v29 mixed-radix maxima shape mismatch")
    if np.any(rows > row_maxima):
        raise ValueError("v29 mixed-radix value exceeds shared maximum")
    codes = rows[:, 0].copy()
    limit = np.iinfo(np.int64).max
    for column in range(1, rows.shape[1]):
        base = int(row_maxima[column]) + 1
        maximum_code = int(codes.max()) if len(codes) else 0
        maximum_value = int(row_maxima[column])
        if base <= 0 or maximum_code > (limit - maximum_value) // base:
            return None
        codes *= base
        codes += rows[:, column]
    return codes


def _row_histogram_counts(values: Any, *, bounded: bool = False) -> Any:
    """Return non-zero group counts for scalar or row-valued exact keys."""
    rows = np.asarray(values, dtype=np.int64)
    if not rows.size:
        return np.empty(0, dtype=np.int64)
    if rows.ndim == 1:
        if bounded:
            counts = np.bincount(rows)
            return counts[counts > 0].astype(np.int64, copy=False)
        return np.unique(rows, return_counts=True)[1]
    codes = _mixed_radix_codes(rows)
    if codes is not None:
        return np.unique(codes, return_counts=True)[1]
    return np.unique(rows, axis=0, return_counts=True)[1]


def _exact_row_lookup_ids(keys: Any, query: Any) -> Any:
    """Map row-valued queries to exact key IDs without a tuple/list hot path."""
    table = np.asarray(keys, dtype=np.int64)
    values = np.asarray(query, dtype=np.int64)
    if table.ndim == 1:
        table = table[:, None]
    if values.ndim == 1:
        values = values[:, None]
    if table.ndim != 2 or values.ndim != 2 or table.shape[1] != values.shape[1]:
        raise ValueError("v29 exact row lookup shape mismatch")
    output = np.full(len(values), -1, dtype=np.int64)
    if not len(table) or not len(values):
        return output
    selected = np.all(values >= 0, axis=1)
    if not np.any(selected):
        return output
    selected_values = values[selected]
    maxima = np.maximum(table.max(axis=0), selected_values.max(axis=0))
    table_codes = _mixed_radix_codes(table, maxima)
    query_codes = _mixed_radix_codes(selected_values, maxima)
    if table_codes is None or query_codes is None:
        # This exact fallback is intentionally cold: decoded cache resources
        # are small bounded integers, but arbitrary diagnostic inputs remain
        # correct even if a mixed-radix int64 representation would overflow.
        mapping = {
            tuple(map(int, key)): index for index, key in enumerate(table)
        }
        output[selected] = np.fromiter(
            (mapping.get(tuple(map(int, row)), -1) for row in selected_values),
            dtype=np.int64,
            count=len(selected_values),
        )
        return output
    order = np.argsort(table_codes, kind="stable")
    sorted_codes = table_codes[order]
    positions = np.searchsorted(sorted_codes, query_codes)
    clipped = np.minimum(positions, len(sorted_codes) - 1)
    candidate_ids = order[clipped]
    found = positions < len(sorted_codes)
    found &= sorted_codes[clipped] == query_codes
    found &= np.all(table[candidate_ids] == selected_values, axis=1)
    selected_ids = np.full(len(selected_values), -1, dtype=np.int64)
    selected_ids[found] = candidate_ids[found]
    output[selected] = selected_ids
    return output


def _apply_window_pressure_numpy(
    fields: Any,
    resource_keys: Any,
    valid_mask: Any,
    *,
    copy: bool = True,
) -> Any:
    """Vectorized equivalent of ``apply_window_pressure`` for one K-window."""
    out = np.array(fields, dtype=np.int64, copy=copy)
    if not out.flags.writeable:
        out = out.copy()
    resources = np.asarray(resource_keys, dtype=np.int64)
    valid = np.asarray(valid_mask, dtype=np.bool_)
    bank_index = RESOURCE_KEY_INDEX["llc_bank"]
    for key_name, field_name in (
        ("l1_set", "l1_set_pressure"),
        ("l2_set", "l2_set_pressure"),
        ("llc_set", "llc_set_pressure"),
    ):
        field_index = FIELD_INDEX[field_name]
        key_index = RESOURCE_KEY_INDEX[key_name]
        out[:, field_index] = 0
        if key_name == "llc_set":
            selected = valid & (resources[:, bank_index] >= 0) & (
                resources[:, key_index] >= 0
            )
            keys = resources[selected][:, [bank_index, key_index]]
            if keys.size == 0:
                continue
        else:
            selected = valid & (resources[:, key_index] >= 0)
            keys = resources[selected, key_index]
            if keys.size == 0:
                continue
            if key_name in {"l1_set", "l2_set"}:
                counts = np.bincount(keys)
                token_counts = counts[keys]
            else:  # pragma: no cover - kept for future scalar pressure fields.
                _unique, inverse, counts = np.unique(
                    keys, return_inverse=True, return_counts=True,
                )
                token_counts = counts[inverse]
        if key_name == "llc_set":
            codes = _mixed_radix_codes(keys)
            if codes is None:
                _unique, inverse, counts = np.unique(
                    keys, axis=0, return_inverse=True, return_counts=True,
                )
            else:
                _unique, inverse, counts = np.unique(
                    codes, return_inverse=True, return_counts=True,
                )
            token_counts = counts[inverse]
        buckets = 1 + np.floor(np.log2(token_counts)).astype(np.int64)
        out[selected, field_index] = np.minimum(9, buckets)
    return out


def _summarize_window_numpy(
    fields: Any,
    resources: Any,
    valid_mask: Any,
    semantic_flags: Any,
    functional_lines: Any,
    functional_pages: Any,
    producer_logs: Any,
    macro_pcs: Any,
    macro_end: Any,
    K: int,
) -> Any:
    """Vectorized equivalent of ``summarize_window`` for one K-window."""
    valid = np.asarray(valid_mask, dtype=np.bool_)
    field_rows = np.asarray(fields, dtype=np.int64)[valid]
    resource_rows = np.asarray(resources, dtype=np.int64)[valid]
    semantic = np.asarray(semantic_flags, dtype=np.uint8)[valid]
    lines_all = np.asarray(functional_lines, dtype=np.int64)[valid]
    pages_all = np.asarray(functional_pages, dtype=np.int64)[valid]
    producer = np.asarray(producer_logs, dtype=np.float32)[valid]
    pcs = np.asarray(macro_pcs, dtype=np.uint64)[valid]
    macro_flags = np.asarray(macro_end, dtype=np.uint8)[valid]
    n_valid = int(valid.sum())
    n = max(1, n_valid)
    mem_mask = (semantic & 0x7) != 0
    mem_den = max(1, int(mem_mask.sum()))
    mem_resources = resource_rows[mem_mask]

    semantic_counts = np.count_nonzero(
        semantic[:, None] & (1 << np.arange(8, dtype=np.uint8)), axis=0,
    )

    resource_values: Dict[str, Any] = {}
    for name in (
        "physical_line", "l1_set", "l2_set", "llc_bank", "dram_channel",
    ):
        if mem_resources.size:
            values = mem_resources[:, RESOURCE_KEY_INDEX[name]]
            resource_values[name] = values[values >= 0]
        else:
            resource_values[name] = np.empty(0, dtype=np.int64)

    def hhi(counts: Any) -> float:
        if not len(counts):
            return 0.0
        probabilities = np.asarray(counts, dtype=np.float64) / float(counts.sum())
        return float(np.dot(probabilities, probabilities))

    opclasses = field_rows[:, FIELD_INDEX["op_class"]]
    reuse = field_rows[:, FIELD_INDEX["reuse_distance"]]
    strides = field_rows[:, FIELD_INDEX["stride"]]
    producer_distance = field_rows[:, FIELD_INDEX["producer_distance"]]
    branch_mask = (semantic & (1 << 3)) != 0
    branch_taken = (
        field_rows[branch_mask, FIELD_INDEX["branch_taken"]] == 2
    )
    branch_switches = int(np.count_nonzero(branch_taken[1:] != branch_taken[:-1]))
    branch_kind = field_rows[:, FIELD_INDEX["branch_kind"]]
    l1_sets = resource_values["l1_set"]
    l2_sets = resource_values["l2_set"]
    llc_banks = resource_values["llc_bank"]
    channels = resource_values["dram_channel"]

    if mem_resources.size:
        llc_pair_columns = [
            RESOURCE_KEY_INDEX["llc_bank"], RESOURCE_KEY_INDEX["llc_set"],
        ]
        llc_pairs = mem_resources[:, llc_pair_columns]
        llc_pairs = llc_pairs[np.all(llc_pairs >= 0, axis=1)]
        dram_bank_columns = [
            RESOURCE_KEY_INDEX[name]
            for name in ("dram_channel", "dram_rank", "dram_bank")
        ]
        dram_banks = mem_resources[:, dram_bank_columns]
        dram_banks = dram_banks[np.all(dram_banks >= 0, axis=1)]
        dram_row_columns = dram_bank_columns + [RESOURCE_KEY_INDEX["dram_row"]]
        dram_rows = mem_resources[:, dram_row_columns]
        dram_rows = dram_rows[np.all(dram_rows >= 0, axis=1)]
    else:
        llc_pairs = np.empty((0, 2), dtype=np.int64)
        dram_banks = np.empty((0, 3), dtype=np.int64)
        dram_rows = np.empty((0, 4), dtype=np.int64)

    pc_counts = _row_histogram_counts(pcs)
    pc_entropy = 0.0
    if len(pc_counts) > 1:
        probabilities = pc_counts.astype(np.float64) / float(n)
        pc_entropy = -float(np.sum(
            probabilities * np.log(np.maximum(probabilities, 1.0e-12)),
        ))
        pc_entropy /= max(math.log(len(pc_counts)), 1.0e-12)

    ends = np.flatnonzero(macro_flags) + 1
    if not len(ends) or int(ends[-1]) != n_valid:
        ends = np.append(ends, n_valid)
    macro_lengths = np.diff(np.concatenate((np.asarray([0]), ends)))
    mean_macro_log = math.log1p(
        float(macro_lengths.sum()) / max(1, len(macro_lengths))
    ) / 8.0
    valid_lines = lines_all[mem_mask]
    valid_lines = valid_lines[valid_lines >= 0]
    valid_pages = pages_all[mem_mask]
    valid_pages = valid_pages[valid_pages >= 0]
    mem_reuse = reuse[mem_mask]
    mem_strides = strides[mem_mask]
    opclass_counts = np.bincount(opclasses, minlength=30)
    line_counts = _row_histogram_counts(valid_lines)
    page_counts = _row_histogram_counts(valid_pages)
    l1_counts = _row_histogram_counts(l1_sets, bounded=True)
    l2_counts = _row_histogram_counts(l2_sets, bounded=True)
    llc_pair_counts = _row_histogram_counts(llc_pairs)
    llc_bank_counts = _row_histogram_counts(llc_banks, bounded=True)
    channel_counts = _row_histogram_counts(channels, bounded=True)
    dram_bank_counts = _row_histogram_counts(dram_banks)
    dram_row_counts = _row_histogram_counts(dram_rows)
    result = [
        *(float(value) / n for value in semantic_counts),
        int(opclass_counts[2]) / n,
        int(opclass_counts[3]) / n,
        int(opclass_counts[[4, 5, 6, 10]].sum()) / n,
        int(opclass_counts[[7, 8]].sum()) / n,
        int(opclass_counts[[9, 11, 23, 24, 29]].sum()) / n,
        int(np.count_nonzero((branch_kind & 0x2) != 0)) / n,
        int(np.count_nonzero((branch_kind & 0x4) != 0)) / n,
        len(line_counts) / n,
        len(page_counts) / n,
        float(producer.sum(dtype=np.float64)) / n,
        (float(producer.max()) if len(producer) else 0.0) / 16.0,
        int(np.count_nonzero(np.isin(mem_reuse, (2, 3)))) / mem_den,
        int(np.count_nonzero(np.isin(mem_reuse, (1, 8)))) / mem_den,
        int(np.count_nonzero(np.isin(mem_strides, (3, 4, 5, 6)))) / mem_den,
        int(np.count_nonzero(np.isin(mem_strides, (7, 8, 9)))) / mem_den,
        int(np.count_nonzero((producer_distance > 0) & (producer_distance <= 4))) / n,
        pc_entropy,
        mean_macro_log,
        n_valid / max(1, int(K)),
        int(np.count_nonzero(branch_taken)) / max(1, len(branch_taken)),
        branch_switches / max(1, len(branch_taken) - 1),
        len(resource_values["physical_line"]) / mem_den,
        len(l1_counts) / mem_den,
        len(l2_counts) / mem_den,
        len(llc_pair_counts) / mem_den,
        (len(llc_pairs) - len(llc_pair_counts)) / mem_den,
        hhi(llc_bank_counts),
        hhi(channel_counts),
        hhi(dram_bank_counts),
        (len(dram_rows) - len(dram_row_counts)) / mem_den,
    ]
    if len(result) != len(CHUNK_SUMMARY_NAMES):
        raise RuntimeError("v29 vectorized summary dimension mismatch")
    return np.asarray(result, dtype=np.float64)


def _context_features_numpy(
    chunks: Sequence[Mapping[str, Any]],
) -> Tuple[Any, Any]:
    """Vectorized equivalent of cross-core ``context_features``.

    Every relation is derived from distinct-core presence counts.  This keeps
    the equality-only feature contract while avoiding a Python loop over all
    active cores times all K UOPs.
    """
    n_active = len(chunks)
    if n_active <= 0:
        raise ValueError("v29 context requires at least one active core")
    resources = np.stack([
        chunk["_numpy"]["resource"] for chunk in chunks
    ]).astype(np.int64, copy=False)
    lines = np.stack([
        chunk["_numpy"]["physical_line"] for chunk in chunks
    ]).astype(np.int64, copy=False)
    kinds = np.stack([
        chunk["_numpy"]["access"] for chunk in chunks
    ]).astype(np.uint8, copy=False)
    valid = np.stack([
        chunk["_numpy"]["valid_uop_mask"] for chunk in chunks
    ]).astype(np.bool_, copy=False)
    if resources.shape[:2] != valid.shape or lines.shape != valid.shape:
        raise ValueError("v29 vectorized context shape mismatch")
    if resources.shape[2] != len(RESOURCE_KEY_NAMES):
        raise ValueError("v29 vectorized resource-key dimension mismatch")
    core_grid = np.broadcast_to(
        np.arange(n_active, dtype=np.int64)[:, None], valid.shape,
    )

    def presence(
        values: Any, mask: Any, *, bounded_scalar: bool = False,
    ) -> Dict[str, Any]:
        data = np.asarray(values, dtype=np.int64)
        if data.ndim == 2:
            data = data[:, :, None]
        selected = np.asarray(mask, dtype=np.bool_)
        key_width = int(data.shape[2])
        token_ids = np.full(valid.shape, -1, dtype=np.int64)
        if not np.any(selected):
            return {
                "keys": np.empty((0, key_width), dtype=np.int64),
                "token_ids": token_ids,
                "owners": np.zeros((n_active, 0), dtype=np.bool_),
                "counts": np.empty(0, dtype=np.int64),
            }
        selected_data = data[selected]
        if bounded_scalar and key_width == 1:
            scalar_values = selected_data[:, 0]
            dense_counts = np.bincount(scalar_values)
            unique_values = np.flatnonzero(dense_counts)
            dense_ids = np.full(len(dense_counts), -1, dtype=np.int64)
            dense_ids[unique_values] = np.arange(
                len(unique_values), dtype=np.int64,
            )
            keys = unique_values[:, None]
            inverse = dense_ids[scalar_values]
        else:
            codes = _mixed_radix_codes(selected_data)
            if codes is None:
                keys, inverse = np.unique(
                    selected_data, axis=0, return_inverse=True,
                )
            else:
                _unique_codes, first, inverse = np.unique(
                    codes, return_index=True, return_inverse=True,
                )
                keys = selected_data[first]
        token_ids[selected] = inverse
        owners = np.zeros((n_active, len(keys)), dtype=np.bool_)
        owners[core_grid[selected], inverse] = True
        return {
            "keys": keys,
            "token_ids": token_ids,
            "owners": owners,
            "counts": owners.sum(axis=0, dtype=np.int64),
        }

    def line_lookup(table: Mapping[str, Any], values: Any) -> Tuple[Any, Any]:
        query = np.asarray(values, dtype=np.int64)
        keys = table["keys"][:, 0]
        if len(keys) == 0:
            return (
                np.zeros(query.shape, dtype=np.int64),
                np.full(query.shape, -1, dtype=np.int64),
            )
        positions = np.searchsorted(keys, query)
        clipped = np.minimum(positions, len(keys) - 1)
        found = (positions < len(keys)) & (keys[clipped] == query)
        ids = np.where(found, clipped, -1)
        counts = np.zeros(query.shape, dtype=np.int64)
        counts[found] = table["counts"][ids[found]]
        return counts, ids

    def other_token_counts(table: Mapping[str, Any]) -> Any:
        ids = table["token_ids"]
        output = np.zeros(valid.shape, dtype=np.int64)
        selected = ids >= 0
        output[selected] = table["counts"][ids[selected]] - 1
        return output

    read_mask = valid & (lines >= 0) & ((kinds == 1) | (kinds == 3))
    write_mask = valid & (lines >= 0) & ((kinds == 2) | (kinds == 3))
    access_mask = read_mask | write_mask
    reads = presence(lines, read_mask)
    writes = presence(lines, write_mask)
    accesses = presence(lines, access_mask)
    mem_mask = valid & (kinds > 0)

    def resource_presence(
        names: Sequence[str], require_row: bool = False,
        *, bounded_scalar: bool = False,
    ) -> Any:
        indices = [RESOURCE_KEY_INDEX[name] for name in names]
        values = resources[:, :, indices]
        selected = mem_mask & np.all(values >= 0, axis=2)
        if require_row:
            selected &= resources[:, :, RESOURCE_KEY_INDEX["dram_row"]] >= 0
        return presence(values, selected, bounded_scalar=bounded_scalar)

    llc_sets = resource_presence(("llc_bank", "llc_set"))
    llc_banks = resource_presence(("llc_bank",), bounded_scalar=True)
    channels = resource_presence(("dram_channel",), bounded_scalar=True)
    dram_banks = resource_presence(
        ("dram_channel", "dram_rank", "dram_bank"), require_row=True,
    )
    dram_rows = resource_presence(
        ("dram_channel", "dram_rank", "dram_bank", "dram_row"),
    )

    # A different-row conflict exists for another core when that core owns
    # the bank and is not exclusively accessing the same row.
    row_to_bank = _exact_row_lookup_ids(
        dram_banks["keys"], dram_rows["keys"][:, :3],
    )
    if np.any(row_to_bank < 0):
        raise RuntimeError("v29 DRAM row key has no matching bank key")
    exclusive = np.zeros_like(dram_rows["owners"])
    for core in range(n_active):
        owned_rows = np.flatnonzero(dram_rows["owners"][core])
        if not len(owned_rows):
            continue
        bank_row_counts = np.bincount(
            row_to_bank[owned_rows], minlength=len(dram_banks["keys"]),
        )
        exclusive[core, owned_rows] = (
            bank_row_counts[row_to_bank[owned_rows]] == 1
        )
    exclusive_counts = exclusive.sum(axis=0, dtype=np.int64)
    row_conflicts = np.zeros_like(dram_rows["owners"], dtype=np.int64)
    for core in range(n_active):
        owned_rows = np.flatnonzero(dram_rows["owners"][core])
        if not len(owned_rows):
            continue
        bank_ids = row_to_bank[owned_rows]
        other_bank_cores = dram_banks["counts"][bank_ids] - 1
        same_row_only_other = (
            exclusive_counts[owned_rows] - exclusive[core, owned_rows]
        )
        row_conflicts[core, owned_rows] = (
            other_bank_cores - same_row_only_other
        )

    total_uops = int(valid.sum())
    total_mem = int(mem_mask.sum())
    global_lines = len(accesses["keys"])
    fanout_den = max(1, n_active - 1)
    relations = np.zeros(
        (n_active, len(RELATION_FEATURE_NAMES)), dtype=np.float64,
    )
    for core in range(n_active):
        own_access = np.flatnonzero(accesses["owners"][core])
        own_read = np.flatnonzero(reads["owners"][core])
        own_write = np.flatnonzero(writes["owners"][core])
        access_lines = accesses["keys"][own_access, 0]
        read_lines = reads["keys"][own_read, 0]
        write_lines = writes["keys"][own_write, 0]
        read_counts_for_access, read_ids_for_access = line_lookup(
            reads, access_lines,
        )
        write_counts_for_access, write_ids_for_access = line_lookup(
            writes, access_lines,
        )
        write_counts_for_read, write_ids_for_read = line_lookup(
            writes, read_lines,
        )
        access_counts_for_write, _ = line_lookup(accesses, write_lines)
        access_counts_for_read, _ = line_lookup(accesses, read_lines)
        own_read_for_access = np.zeros(len(own_access), dtype=np.int64)
        selected = read_ids_for_access >= 0
        own_read_for_access[selected] = reads["owners"][
            core, read_ids_for_access[selected]
        ]
        own_write_for_access = np.zeros(len(own_access), dtype=np.int64)
        selected = write_ids_for_access >= 0
        own_write_for_access[selected] = writes["owners"][
            core, write_ids_for_access[selected]
        ]
        own_write_for_read = np.zeros(len(own_read), dtype=np.int64)
        selected = write_ids_for_read >= 0
        own_write_for_read[selected] = writes["owners"][
            core, write_ids_for_read[selected]
        ]
        other_readers = read_counts_for_access - own_read_for_access
        other_writers = write_counts_for_access - own_write_for_access
        other_writers_for_read = write_counts_for_read - own_write_for_read
        own_llc_sets = np.flatnonzero(llc_sets["owners"][core])
        own_llc_banks = np.flatnonzero(llc_banks["owners"][core])
        own_channels = np.flatnonzero(channels["owners"][core])
        own_dram_banks = np.flatnonzero(dram_banks["owners"][core])
        own_dram_rows = np.flatnonzero(dram_rows["owners"][core])
        denom_access = max(1, len(own_access))
        denom_read = max(1, len(own_read))
        denom_write = max(1, len(own_write))
        relations[core] = [
            math.log1p(n_active) / 4.0,
            int(np.count_nonzero(accesses["counts"][own_access] > 1)) / denom_access,
            int(np.count_nonzero(other_writers_for_read > 0)) / denom_read,
            int(np.count_nonzero(access_counts_for_write > 1)) / denom_write,
            int(np.count_nonzero(writes["counts"][own_write] > 1)) / denom_write,
            int(np.count_nonzero(access_counts_for_read > 1)) / denom_read,
            int(np.count_nonzero(access_counts_for_write > 1)) / denom_write,
            float(other_readers.sum()) / max(1, len(other_readers)) / fanout_den,
            float(other_writers.sum()) / max(1, len(other_writers)) / fanout_den,
            int(np.max(accesses["counts"][own_access] - 1)) / fanout_den
            if len(own_access) else 0.0,
            int(np.max(writes["counts"][own_write])) / max(1, n_active)
            if len(own_write) else 0.0,
            math.log1p(1000.0 * global_lines / max(1, total_uops)) / 8.0,
            len(own_access) / max(1, global_lines),
            total_mem / max(1, total_uops),
            int(np.count_nonzero(llc_sets["counts"][own_llc_sets] > 1))
            / max(1, len(own_llc_sets)),
            int(np.count_nonzero(llc_banks["counts"][own_llc_banks] > 1))
            / max(1, len(own_llc_banks)),
            int(np.count_nonzero(channels["counts"][own_channels] > 1))
            / max(1, len(own_channels)),
            int(np.count_nonzero(dram_banks["counts"][own_dram_banks] > 1))
            / max(1, len(own_dram_banks)),
            int(np.count_nonzero(dram_rows["counts"][own_dram_rows] > 1))
            / max(1, len(own_dram_rows)),
            int(np.count_nonzero(row_conflicts[core, own_dram_rows] > 0))
            / max(1, len(own_dram_rows)),
            float((llc_sets["counts"][own_llc_sets] - 1).sum())
            / max(1, len(own_llc_sets)) / fanout_den,
            float((dram_banks["counts"][own_dram_banks] - 1).sum())
            / max(1, len(own_dram_banks)) / fanout_den,
        ]

    dynamic = np.zeros(
        valid.shape + (len(DYNAMIC_FIELD_NAMES),), dtype=np.int64,
    )
    dynamic[~valid] = np.asarray(DYNAMIC_PAD_IDS, dtype=np.int64)
    selected = access_mask
    if np.any(selected):
        line_values = lines[selected]
        selected_cores = core_grid[selected]
        read_counts, read_ids = line_lookup(reads, line_values)
        write_counts, write_ids = line_lookup(writes, line_values)
        own_read = np.zeros(len(line_values), dtype=np.int64)
        found = read_ids >= 0
        own_read[found] = reads["owners"][selected_cores[found], read_ids[found]]
        own_write = np.zeros(len(line_values), dtype=np.int64)
        found = write_ids >= 0
        own_write[found] = writes["owners"][selected_cores[found], write_ids[found]]
        other_read = read_counts - own_read
        other_write = write_counts - own_write
        roles = np.where(
            kinds[selected] == 1,
            np.where(other_write > 0, 4, np.where(other_read > 0, 2, 1)),
            np.where(other_write > 0, 6, np.where(other_read > 0, 5, 3)),
        )
        fanout_bucket = np.asarray(
            [
                1 if value <= 0 else 1 + min(
                    6, int(math.ceil(math.log2(value + 1))),
                )
                for value in range(n_active + 1)
            ],
            dtype=np.int64,
        )
        feature_values = [roles]
        dynamic_tables = (accesses, llc_sets, llc_banks, channels)
        token_other = {
            id(table): other_token_counts(table) for table in dynamic_tables
        }
        for table in dynamic_tables:
            feature_values.append(
                fanout_bucket[np.minimum(token_other[id(table)][selected], n_active)]
            )
        bank_columns = [
            RESOURCE_KEY_INDEX[name]
            for name in ("dram_channel", "dram_rank", "dram_bank")
        ]
        selected_bank_ids = _exact_row_lookup_ids(
            dram_banks["keys"], resources[selected][:, bank_columns],
        )
        selected_bank_other = np.zeros(len(line_values), dtype=np.int64)
        found = selected_bank_ids >= 0
        selected_bank_other[found] = (
            dram_banks["counts"][selected_bank_ids[found]]
            - dram_banks["owners"][
                selected_cores[found], selected_bank_ids[found]
            ]
        )
        feature_values.append(
            fanout_bucket[np.minimum(selected_bank_other, n_active)]
        )
        selected_row_ids = dram_rows["token_ids"][selected]
        row_other = other_token_counts(dram_rows)[selected]
        feature_values.append(
            fanout_bucket[np.minimum(row_other, n_active)]
        )
        selected_conflicts = selected_bank_other.copy()
        found = selected_row_ids >= 0
        selected_conflicts[found] -= (
            exclusive_counts[selected_row_ids[found]]
            - exclusive[
                selected_cores[found], selected_row_ids[found]
            ]
        )
        feature_values.append(
            fanout_bucket[np.minimum(selected_conflicts, n_active)]
        )
        dynamic[selected] = np.stack(feature_values, axis=1)
    return dynamic, relations


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
        self.has_oracle_labels = True
        self.meta = load_json(os.path.join(self.cache_dir, "meta.json"))
        _check_contract(self.meta)
        quality = dict(self.meta.get("quality", {}) or {})
        if quality.get("status") != "pass":
            raise RuntimeError(f"v29 cache quality is not pass: {self.cache_dir}")
        if not bool(quality.get("synchronous_roi_start")):
            raise RuntimeError("v29 labeled cache does not have one shared ROI T0")
        if not bool(self.meta.get("collection_provenance", {}).get("ff_atomic_verified")):
            raise RuntimeError("v29 labeled cache lacks verified FFATOMIC -> O3/Ruby provenance")
        roi_atomic_uops = int(
            quality.get("roi_atomic_uops", quality.get("atomic_uops", -1))
        )
        if roi_atomic_uops != 0:
            raise RuntimeError(
                f"v29 labeled cache ROI atomic UOP contract mismatch: {roi_atomic_uops}"
            )
        minimum_uops = int(self.meta.get("min_uops_per_core_contract", -1))
        maximum_uops = int(self.meta.get("max_uops_per_core_contract", -1))
        maximum_cpi = float(self.meta.get("max_full_uop_cpi_contract", -1.0))
        if minimum_uops <= 0 or maximum_uops < minimum_uops or maximum_cpi <= 0:
            raise RuntimeError("v29 labeled cache lacks strict data acceptance metadata")
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
        self.roi_origin_tick = min(
            int(item["roi_begin_tick"]) for item in self.core_meta.values()
        )
        for item in self.core_meta.values():
            n_uops = int(item.get("n_uops", -1))
            full_cpi = float(item.get("full_uop_cpi", float("inf")))
            if not minimum_uops <= n_uops <= maximum_uops or full_cpi > maximum_cpi:
                raise RuntimeError(
                    "v29 labeled cache core violates data acceptance: "
                    f"core={item.get('core_id')} uops={n_uops} cpi={full_cpi}"
                )
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
        # One exact window per core is enough to eliminate repeated work after
        # a no-progress step without retaining an unbounded sliding-window
        # cache.  Normal advancing rollouts intentionally replace this entry.
        self._window_cache: Dict[
            int, Tuple[int, bool, bool, Dict[str, Any]]
        ] = {}
        self.reset_runtime_stats()

    def __len__(self) -> int:
        return len(self.sample_ticks)

    def reset_runtime_stats(self, *, clear_cache: bool = True) -> None:
        if clear_cache:
            self._window_cache.clear()
        self.window_cache_hits = 0
        self.window_cache_misses = 0
        self.context_calls = 0
        self.context_phase_seconds = {
            name: 0.0 for name in CONTEXT_PHASE_NAMES
        }

    def runtime_stats(self) -> Dict[str, Any]:
        total = self.window_cache_hits + self.window_cache_misses
        return {
            "context_builder": CONTEXT_BUILDER,
            "cpu_window_cache_policy": "last-window-per-core",
            "cpu_window_cache_hits": self.window_cache_hits,
            "cpu_window_cache_misses": self.window_cache_misses,
            "cpu_window_cache_hit_rate": self.window_cache_hits / max(1, total),
            "cpu_window_cache_entries": len(self._window_cache),
            "context_timing_contract": CONTEXT_TIMING_CONTRACT,
            "context_calls": self.context_calls,
            "context_phase_seconds": dict(self.context_phase_seconds),
        }

    @staticmethod
    def _pad_1d(values: Sequence[Any], K: int, pad: Any) -> List[Any]:
        out = list(values[:K])
        out.extend([pad] * (K - len(out)))
        return out

    def window(
        self, core_id: int, cursor: int, *, include_oracle: bool,
    ) -> Dict[str, Any]:
        """Return the compatibility window with public Python-list fields."""
        return self._window(
            core_id, cursor, include_oracle=include_oracle, numpy_only=False,
        )

    def _window(
        self,
        core_id: int,
        cursor: int,
        *,
        include_oracle: bool,
        numpy_only: bool,
    ) -> Dict[str, Any]:
        """Materialize one window, avoiding compatibility lists in hot paths."""
        core_id = int(core_id)
        cursor = int(cursor)
        cached = self._window_cache.get(core_id)
        if (
            cached is not None
            and cached[0] == cursor
            and cached[1] == include_oracle
            and cached[2] == numpy_only
        ):
            self.window_cache_hits += 1
            return cached[3]
        self.window_cache_misses += 1
        arrays = self.cores[core_id]
        count = int(arrays["fields"].shape[0])
        if not 0 <= cursor < count:
            raise IndexError(f"cursor {cursor} outside core {core_id} length {count}")
        end = min(count, cursor + self.K)
        n_valid = end - cursor
        valid_array = np.zeros(self.K, dtype=np.uint8)
        valid_array[:n_valid] = 1
        resource_array = np.full(
            (self.K, len(RESOURCE_KEY_NAMES)),
            RESOURCE_KEY_INVALID,
            dtype=np.int64,
        )
        resource_array[:n_valid] = np.asarray(
            arrays["resource"][cursor:end], dtype=np.int64,
        )
        fields_array = np.broadcast_to(
            np.asarray(FIELD_PAD_IDS, dtype=np.int64),
            (self.K, len(FIELD_NAMES)),
        ).copy()
        fields_array[:n_valid] = np.asarray(
            arrays["fields"][cursor:end], dtype=np.int64,
        )
        _apply_window_pressure_numpy(
            fields_array, resource_array, valid_array, copy=False,
        )

        def padded(name: str, dtype: Any, pad: Any) -> Any:
            output = np.full(self.K, pad, dtype=dtype)
            output[:n_valid] = np.asarray(arrays[name][cursor:end], dtype=dtype)
            return output

        physical_lines_array = padded("physical_line", np.int64, -1)
        access_array = padded("access", np.uint8, 0)
        semantic_array = padded("semantic_flags", np.uint8, 0)
        functional_lines_array = padded("functional_line", np.int64, -1)
        functional_pages_array = padded("functional_page", np.int64, -1)
        producer_logs_array = padded("producer_log", np.float32, 0.0)
        macro_pcs_array = padded("macro_pc", np.uint64, 0)
        macro_end_array = padded("macro_end", np.uint8, 0)
        branch_array = padded("branch", np.uint8, 0)
        if include_oracle:
            if "branch_miss" not in arrays or "commit_tick" not in arrays:
                raise RuntimeError("v29 oracle window requested from label-free cache")
            branch_miss_array = padded("branch_miss", np.uint8, 0)
            commit_ticks_array = padded("commit_tick", np.int64, 0)
        valid_bool = valid_array.astype(np.bool_, copy=False)
        chunk_summary = _summarize_window_numpy(
            fields_array,
            resource_array,
            valid_array,
            semantic_array,
            functional_lines_array,
            functional_pages_array,
            producer_logs_array,
            macro_pcs_array,
            macro_end_array,
            self.K,
        )
        chunk: Dict[str, Any] = {
            "core_id": core_id,
            "cursor": cursor,
            "n_uops": n_valid,
            "chunk_summary": chunk_summary,
            "_numpy": {
                "per_uop_fields": fields_array,
                "resource": resource_array,
                "physical_line": physical_lines_array,
                "access": access_array,
                "valid_uop_mask": valid_bool,
                "branch": branch_array.astype(np.bool_, copy=False),
                "macro_end": macro_end_array.astype(np.bool_, copy=False),
            },
        }
        if include_oracle:
            chunk["_numpy"]["branch_miss"] = branch_miss_array
            chunk["_numpy"]["commit_tick"] = commit_ticks_array
        if not numpy_only:
            read_mask = (
                valid_bool
                & (physical_lines_array >= 0)
                & ((access_array == 1) | (access_array == 3))
            )
            write_mask = (
                valid_bool
                & (physical_lines_array >= 0)
                & ((access_array == 2) | (access_array == 3))
            )
            chunk.update({
                "per_uop_fields": fields_array.tolist(),
                "per_uop_resource_keys": resource_array.tolist(),
                "per_uop_lines": physical_lines_array.tolist(),
                "per_uop_access": access_array.tolist(),
                "valid_uop_mask": valid_array.tolist(),
                "semantic_flags": semantic_array.tolist(),
                "functional_lines": functional_lines_array.tolist(),
                "functional_pages": functional_pages_array.tolist(),
                "producer_logs": producer_logs_array.tolist(),
                "macro_pcs": macro_pcs_array.tolist(),
                "macro_end": macro_end_array.tolist(),
                "branch": branch_array.tolist(),
                "read_lines": np.unique(
                    physical_lines_array[read_mask],
                ).tolist(),
                "write_lines": np.unique(
                    physical_lines_array[write_mask],
                ).tolist(),
            })
            if include_oracle:
                chunk["branch_miss"] = branch_miss_array.tolist()
                chunk["commit_ticks"] = commit_ticks_array.tolist()
        self._window_cache[core_id] = (
            cursor, include_oracle, numpy_only, chunk,
        )
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
        context_started = time.perf_counter()
        if len(cursors) != len(self.core_ids):
            raise ValueError("cursor vector/core count mismatch")
        if include_labels and not self.has_oracle_labels:
            raise RuntimeError("label-free v29 functional cache has no oracle targets")
        if include_labels and state_time_tick is None:
            raise ValueError("oracle labels require state_time_tick")
        if state_time_cycles is None:
            if state_time_tick is None:
                raise ValueError("state time is required")
            state_time_cycles = (int(state_time_tick) - self.roi_origin_tick) / self.tpc
        entries = [
            (slot, core_id, int(cursors[slot]))
            for slot, core_id in enumerate(self.core_ids)
            if int(cursors[slot]) >= 0
            and int(cursors[slot]) < int(self.core_meta[core_id]["n_uops"])
        ]
        if not entries:
            raise RuntimeError("empty v29 active context")
        selection_done = time.perf_counter()
        chunks = [
            self._window(
                core_id,
                cursor,
                include_oracle=include_labels,
                numpy_only=True,
            )
            for slot, core_id, cursor in entries
        ]
        windows_done = time.perf_counter()
        dynamic, relations = _context_features_numpy(chunks)
        cross_core_done = time.perf_counter()
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
                if not self.has_oracle_labels:
                    raise ValueError(
                        "label-free v29 context requires deployment last_commit_cycles"
                    )
                tick = int(state_time_tick) if state_time_tick is not None else 0
                previous_tick = (
                    int(self.cores[core_id]["commit_tick"][cursor - 1])
                    if cursor > 0 else int(core_meta["roi_begin_tick"])
                )
                elapsed = max(0.0, (tick - previous_tick) / self.tpc)
                roi_begin_cycles = (
                    int(core_meta["roi_begin_tick"]) - self.roi_origin_tick
                ) / self.tpc
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
                valid = chunk["_numpy"]["valid_uop_mask"]
                commits = chunk["_numpy"]["commit_tick"]
                tau = np.zeros(self.K, dtype=np.float64)
                tau[valid] = (commits[valid] - tick) / self.tpc
                if np.any(tau[valid] <= 0):
                    raise RuntimeError(
                        f"oracle cursor is not the first unretired UOP core={core_id}"
                    )
                prefix = (
                    valid[:, None]
                    & (tau[:, None] <= np.asarray(self.horizons)[None, :])
                ).astype(np.float64)
                commit_targets.append(tau)
                prefix_targets.append(prefix)
                progress_targets.append(prefix.sum(axis=0))
        state_done = time.perf_counter()
        if torch is None:
            raise RuntimeError("torch is required for v29 dataset contexts")
        t = torch

        def stacked_window(name: str, dtype: Any) -> Any:
            values = np.stack([
                chunk["_numpy"][name] for chunk in chunks
            ]).astype(dtype, copy=False)
            return t.from_numpy(values)

        def array_tensor(values: Any, dtype: Any) -> Any:
            return t.from_numpy(np.asarray(values, dtype=dtype))

        result: Dict[str, Any] = {
            "per_uop_fields": stacked_window("per_uop_fields", np.int64),
            "dynamic_uop_fields": array_tensor(dynamic, np.int64),
            "valid_uop_mask": stacked_window("valid_uop_mask", np.bool_),
            "chunk_summary": array_tensor(np.stack([
                chunk["chunk_summary"] for chunk in chunks
            ]), np.float32),
            "relation_features": array_tensor(relations, np.float32),
            "uarch_features": array_tensor(
                [self.uarch_features for _ in chunks], np.float32,
            ),
            "state_features": array_tensor(state_features, np.float32),
            "branch_mask": stacked_window("branch", np.bool_),
            "macro_end": stacked_window("macro_end", np.bool_),
            "core_slots": array_tensor(
                [slot for slot, _, _ in entries], np.int64,
            ),
            "cursors": array_tensor(
                [cursor for _, _, cursor in entries], np.int64,
            ),
            "trace_id": self.trace_id,
            "state_time_cycles": float(state_time_cycles),
        }
        if include_labels:
            result.update({
                "branch_miss_target": stacked_window(
                    "branch_miss", np.float32,
                ),
                "commit_time_target": array_tensor(commit_targets, np.float32),
                "prefix_target": array_tensor(prefix_targets, np.float32),
                "progress_target": array_tensor(progress_targets, np.float32),
            })
        tensors_done = time.perf_counter()
        boundaries = (
            context_started,
            selection_done,
            windows_done,
            cross_core_done,
            state_done,
            tensors_done,
        )
        self.context_calls += 1
        for name, start, end in zip(
            CONTEXT_PHASE_NAMES, boundaries[:-1], boundaries[1:],
        ):
            self.context_phase_seconds[name] += end - start
        return result

    def context_at(self, sample_index: int) -> Dict[str, Any]:
        sample_index = int(sample_index)
        return self.context_from_cursors(
            self.sample_cursors[sample_index],
            state_time_tick=int(self.sample_ticks[sample_index]),
            include_labels=True,
        )


class V29FunctionalStore(V29TraceStore):
    """Label-free deployment store; no commit tick or miss label is loaded."""

    def __init__(self, cache_dir: str) -> None:
        if np is None:
            raise RuntimeError("numpy is required to load v29 functional caches")
        self.cache_dir = os.path.abspath(cache_dir)
        self.has_oracle_labels = False
        self.meta = load_json(os.path.join(self.cache_dir, "meta.json"))
        _check_contract(self.meta)
        if self.meta.get("container_schema") != FUNCTIONAL_CONTAINER_SCHEMA:
            raise RuntimeError("not a label-free v29 functional cache")
        if self.meta.get("quality", {}).get("status") != "pass":
            raise RuntimeError(f"v29 functional cache quality is not pass: {cache_dir}")
        self.trace_id = str(self.meta["trace_id"])
        self.K = int(self.meta["K"])
        if self.K != 256:
            raise RuntimeError("v29 requires K=256")
        self.horizons = normalized_horizons(self.meta["horizons"])
        self.tpc = float(self.meta.get("tick_per_cycle", 1.0))
        self.sample_period_cycles = float(self.meta["sample_period_cycles"])
        self.core_ids = [int(value) for value in self.meta["core_ids"]]
        self.core_meta = {
            int(item["core_id"]): dict(item) for item in self.meta["cores"]
        }
        self.roi_origin_tick = min(
            int(item.get("roi_begin_tick", 0)) for item in self.core_meta.values()
        )
        self.uarch_features = [float(value) for value in self.meta["uarch_features"]]
        self.cores = {}
        for core_id in self.core_ids:
            core_dir = os.path.join(self.cache_dir, "cores", str(core_id))
            arrays = {
                name: np.load(os.path.join(core_dir, name + ".npy"), mmap_mode="r")
                for name in FUNCTIONAL_CORE_ARRAY_NAMES
            }
            n_uops = int(self.core_meta[core_id]["n_uops"])
            if any(int(array.shape[0]) != n_uops for array in arrays.values()):
                raise RuntimeError(f"v29 functional array length mismatch core={core_id}")
            if int(arrays["fields"].shape[1]) != len(FIELD_NAMES):
                raise RuntimeError("v29 functional static field dimension mismatch")
            if int(arrays["resource"].shape[1]) != len(RESOURCE_KEY_NAMES):
                raise RuntimeError("v29 functional resource dimension mismatch")
            self.cores[core_id] = arrays
        self._window_cache = {}
        self.reset_runtime_stats()

    def __len__(self) -> int:
        return 0

    def context_at(self, sample_index: int) -> Dict[str, Any]:
        raise RuntimeError(
            f"label-free functional cache has no oracle sample index {sample_index}"
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
    block_ticks = int(round(block_cycles * store.tpc))
    out = []
    for index, (tick, block_id) in enumerate(zip(store.sample_ticks, store.sample_block_ids)):
        if _block_partition(store.trace_id, int(block_id), policy) != partition:
            continue
        position_cycles = (
            (int(tick) - start_tick) / store.tpc - int(block_id) * block_cycles
        )
        if position_cycles < guard_cycles or position_cycles >= block_cycles - guard_cycles:
            continue
        block_start_tick = start_tick + int(block_id) * block_ticks
        block_end_tick = block_start_tick + block_ticks
        contained = True
        for column, cursor_value in enumerate(store.sample_cursors[index]):
            cursor = int(cursor_value)
            if cursor < 0:
                continue
            core_id = int(store.core_ids[column])
            commits = store.cores[core_id]["commit_tick"]
            # Padded tail windows are valid for deployment, but must not enter
            # train/validation because their target no longer represents a
            # complete fixed-K functional lookahead.
            if cursor + store.K > len(commits):
                contained = False
                break
            previous_tick = (
                int(commits[cursor - 1])
                if cursor > 0 else int(store.core_meta[core_id]["roi_begin_tick"])
            )
            if previous_tick < block_start_tick:
                contained = False
                break
            lookahead_end = min(len(commits) - 1, cursor + store.K - 1)
            if int(commits[lookahead_end]) >= block_end_tick:
                contained = False
                break
        if not contained:
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
