#!/usr/bin/env python3
"""Schemas for deploy-side functional trace and verification labels.

`functional.core*` is the strict A-subset of records.micro.  It contains only
fields that can be produced by atomic/functional execution: uop shape,
addresses and register-dependency summaries.  Microarchitecture oracle columns
in records.micro must not be consumed by the inference driver.

`labels.core*` is the detailed-only verification truth used by label-driven
checks.  It is not part of the real deploy input boundary.
"""
from __future__ import annotations

from typing import Dict, Iterable, List

import pyarrow as pa


ID_COLS: List[str] = [
    "core_id", "thread_id", "micro_seq", "seq_num",
]

STATIC_COLS: List[str] = [
    "macro_pc", "micro_pc",
    "vaddr", "paddr", "cacheline_addr", "cacheline_paddr",
    "size",
    "is_load", "is_store", "is_atomic",
    "is_branch", "is_branch_cond", "is_branch_indirect",
    "is_call", "is_return",
    "is_int", "is_fp", "is_simd", "is_serialize",
    "is_microop", "is_last_microop",
    "n_src", "n_dst",
    "producer_dists", "producer_classes",
]

FUNCTIONAL_TRACE_COLS: List[str] = ID_COLS + STATIC_COLS

LABEL_COLS: List[str] = [
    "core_id", "thread_id", "micro_seq",
    "fetch_tick", "issue_tick", "complete_tick", "commit_tick",
    "ready_tick", "ready_source", "mispredicted",
]

FUNCTIONAL_TRACE_SCHEMA = pa.schema([
    pa.field("core_id", pa.int32()),
    pa.field("thread_id", pa.int32()),
    pa.field("micro_seq", pa.int64()),
    pa.field("seq_num", pa.int64()),
    pa.field("macro_pc", pa.uint64()),
    pa.field("micro_pc", pa.uint32()),
    pa.field("vaddr", pa.uint64()),
    pa.field("paddr", pa.uint64()),
    pa.field("cacheline_addr", pa.uint64()),
    pa.field("cacheline_paddr", pa.uint64()),
    pa.field("size", pa.uint16()),
    pa.field("is_load", pa.uint8()),
    pa.field("is_store", pa.uint8()),
    pa.field("is_atomic", pa.uint8()),
    pa.field("is_branch", pa.uint8()),
    pa.field("is_branch_cond", pa.uint8()),
    pa.field("is_branch_indirect", pa.uint8()),
    pa.field("is_call", pa.uint8()),
    pa.field("is_return", pa.uint8()),
    pa.field("is_int", pa.uint8()),
    pa.field("is_fp", pa.uint8()),
    pa.field("is_simd", pa.uint8()),
    pa.field("is_serialize", pa.uint8()),
    pa.field("is_microop", pa.uint8()),
    pa.field("is_last_microop", pa.uint8()),
    pa.field("n_src", pa.uint8()),
    pa.field("n_dst", pa.uint8()),
    pa.field("producer_dists", pa.list_(pa.uint32(), 4)),
    pa.field("producer_classes", pa.list_(pa.uint8(), 4)),
])

LABEL_SCHEMA = pa.schema([
    pa.field("core_id", pa.int32()),
    pa.field("thread_id", pa.int32()),
    pa.field("micro_seq", pa.int64()),
    pa.field("fetch_tick", pa.int64()),
    pa.field("issue_tick", pa.int64()),
    pa.field("complete_tick", pa.int64()),
    pa.field("commit_tick", pa.int64()),
    pa.field("ready_tick", pa.int64()),
    pa.field("ready_source", pa.int32()),
    pa.field("mispredicted", pa.int32()),
])


def require_functional_row(row: Dict) -> None:
    missing = [c for c in FUNCTIONAL_TRACE_COLS if c not in row]
    if missing:
        raise KeyError(f"functional trace row missing columns: {missing}")
    for key in ("producer_dists", "producer_classes"):
        val = row[key]
        if not isinstance(val, list) or len(val) != 4:
            raise ValueError(f"{key} must be a 4-element list, got {val!r}")


def project_record_row(row: Dict) -> Dict:
    """Return the strict functional subset from a records.micro row."""
    require_functional_row(row)
    return {k: row[k] for k in FUNCTIONAL_TRACE_COLS}


def require_label_row(row: Dict) -> None:
    missing = [c for c in LABEL_COLS if c not in row]
    if missing:
        raise KeyError(f"label row missing columns: {missing}")


def project_label_row(row: Dict) -> Dict:
    require_label_row(row)
    return {k: row[k] for k in LABEL_COLS}


def iter_projected(rows: Iterable[Dict]):
    for row in rows:
        yield project_record_row(row)
