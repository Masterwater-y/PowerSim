"""build_v28_1_macro_chunks.py — Phase 0 macro-boundary chunker for v28_1.

This module forks TCSim's fixed-uop chunker (``tcsim.chunker.fixed_chunk``)
and changes exactly one axis: the chunk boundary is now driven by
``is_last_microop=1`` count reaching ``K_macro`` (default 256), instead of raw
uop count. Everything else — ROI boundary handling, telescoping labels from
``commit_tick``, strict additive assert — is preserved as it is in TCSim.

Contract (per run_id = one raw workload directory):

  chunks.parquet columns:
    trace_id, run_id, core_id, chunk_id, uop_start, uop_end,
    macro_start, macro_end, n_uops, n_macros,
    n_load, n_store, n_atomic, n_branch, n_cond_branch, n_branch_miss,
    n_int, n_fp, n_simd, n_serialize,
    per_macro_static_pc:list<int64>       # module_pc of each retiring macro
    per_macro_uop_count:list<int32>       # #uops inside that macro
    per_macro_op_class:list<int32>        # op_class of macro's last uop
    per_macro_flags:list<int32>           # bit-packed macro-level flags
    per_uop_lines:list<int64>             # cache-line addr per uop (or -1)
    per_uop_access:list<int32>            # 0/1/2/3 = none/load/store/atomic
    boundary_start_seq, boundary_end_seq,
    workload, n_cores, uarch_hash, uarch_features:list<float>,
    binary_hash

  labels.parquet columns:
    trace_id, run_id, core_id, chunk_id,
    delta_cycles, cpi_uop, cpi_macro,
    start_tick, end_tick, delta_ticks,
    n_uops, n_macros,
    boundary_start_seq, boundary_end_seq,
    valid_label, quality_reason

Model-facing input allowlist: ``per_macro_*``, ``per_uop_lines`` (only used to
build shared-line relations at load time), ``per_uop_access``, ``n_*``,
``uarch_features``. Timing/oracle fields never appear.

Usage:
  python -m data.build_v28_1_macro_chunks \\
    --raw-dir /data00/yinhaolang/TSim/data/raw_v28_1_business_a2_sharedzipf_seed0_c04/W_v28_int_alu_dense \\
    --out /data00/yinhaolang/LLMSim/data/v28_1/chunks/<run_id> \\
    --k-macro 256

Or with a manifest of (run_id, raw_dir) pairs for batch mode via ``--manifest``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc

# Reuse TCSim helpers when available; otherwise inline minimal fallbacks.
_TCSIM_PATH = "/data00/yinhaolang/TCSim"
if _TCSIM_PATH not in sys.path:
    sys.path.insert(0, _TCSIM_PATH)
try:
    from tcsim.chunker.fixed_chunk import (  # type: ignore
        _load_roi_boundaries,
        _iter_aligned_rows,
        iter_core_aligned_files,
        CORE_RE,
        ALIGNED_RECORD_COLUMNS,
        ALIGNED_TIMING_COLUMNS,
    )
    from tcsim.chunker.functional_features import (  # type: ignore
        load_uarch_profile,
        tick_per_cycle_from_profile,
        uarch_hash,
        uarch_vector,
    )
    _TCSIM_AVAILABLE = True
except Exception:  # pragma: no cover
    _TCSIM_AVAILABLE = False
    raise RuntimeError(
        "TCSim must be importable from /data00/yinhaolang/TCSim to reuse its "
        "aligned-parquet and ROI helpers"
    )


# ---------------------------------------------------------------------------
# Access-type packing (allowlisted; no timing/oracle fields)
# ---------------------------------------------------------------------------

# Bit-packed macro flags (macro-level aggregation of uop flags).
_MACRO_FLAG_BITS = {
    "has_load": 0,
    "has_store": 1,
    "has_atomic": 2,
    "has_branch": 3,
    "has_int": 4,
    "has_fp": 5,
    "has_simd": 6,
    "has_serialize": 7,
    "branch_taken": 8,
    "is_call": 9,
    "is_return": 10,
    "is_cond_branch": 11,
}


def _access_kind(rec: dict) -> int:
    if int(rec.get("is_atomic", 0) or 0):
        return 3
    if int(rec.get("is_store", 0) or 0):
        return 2
    if int(rec.get("is_load", 0) or 0):
        return 1
    return 0


def _cacheline(rec: dict) -> int:
    v = int(rec.get("cacheline_paddr", 0) or 0)
    if v:
        return v
    v = int(rec.get("cacheline_addr", 0) or 0)
    if v:
        return v
    # fall back to vaddr aligned to 64B when phys is absent
    va = int(rec.get("vaddr", 0) or 0)
    if va:
        return va & ~63
    return -1


def _macro_flags(uops: Sequence[dict]) -> int:
    """OR-reduce uop-level flags into one macro-level bit field."""
    if not uops:
        return 0
    has_load = any(int(u.get("is_load", 0) or 0) for u in uops)
    has_store = any(int(u.get("is_store", 0) or 0) for u in uops)
    has_atomic = any(int(u.get("is_atomic", 0) or 0) for u in uops)
    has_branch = any(int(u.get("is_branch", 0) or 0) for u in uops)
    has_int = any(int(u.get("is_int", 0) or 0) for u in uops)
    has_fp = any(int(u.get("is_fp", 0) or 0) for u in uops)
    has_simd = any(int(u.get("is_simd", 0) or 0) for u in uops)
    has_ser = any(int(u.get("is_serialize", 0) or 0) for u in uops)
    br_taken = any(int(u.get("branch_taken", 0) or 0) for u in uops)
    is_call = any(int(u.get("is_call", 0) or 0) for u in uops)
    is_ret = any(int(u.get("is_return", 0) or 0) for u in uops)
    is_cond = any(int(u.get("is_branch_cond", 0) or 0) for u in uops)
    v = 0
    if has_load: v |= 1 << _MACRO_FLAG_BITS["has_load"]
    if has_store: v |= 1 << _MACRO_FLAG_BITS["has_store"]
    if has_atomic: v |= 1 << _MACRO_FLAG_BITS["has_atomic"]
    if has_branch: v |= 1 << _MACRO_FLAG_BITS["has_branch"]
    if has_int: v |= 1 << _MACRO_FLAG_BITS["has_int"]
    if has_fp: v |= 1 << _MACRO_FLAG_BITS["has_fp"]
    if has_simd: v |= 1 << _MACRO_FLAG_BITS["has_simd"]
    if has_ser: v |= 1 << _MACRO_FLAG_BITS["has_serialize"]
    if br_taken: v |= 1 << _MACRO_FLAG_BITS["branch_taken"]
    if is_call: v |= 1 << _MACRO_FLAG_BITS["is_call"]
    if is_ret: v |= 1 << _MACRO_FLAG_BITS["is_return"]
    if is_cond: v |= 1 << _MACRO_FLAG_BITS["is_cond_branch"]
    return int(v)


# ---------------------------------------------------------------------------
# Chunk dataclass and macro-boundary packer
# ---------------------------------------------------------------------------

@dataclass
class MacroChunk:
    trace_id: str
    run_id: str
    core_id: int
    chunk_id: int
    uop_start: int
    uop_end: int
    macro_start: int
    macro_end: int
    n_uops: int
    n_macros: int
    n_load: int
    n_store: int
    n_atomic: int
    n_branch: int
    n_cond_branch: int
    n_branch_miss: int
    n_int: int
    n_fp: int
    n_simd: int
    n_serialize: int
    per_macro_static_pc: List[int]
    per_macro_uop_count: List[int]
    per_macro_op_class: List[int]
    per_macro_flags: List[int]
    per_uop_lines: List[int]
    per_uop_access: List[int]
    boundary_start_seq: int
    boundary_end_seq: int
    workload: str
    n_cores: int
    uarch_hash: str
    uarch_features: List[float]
    binary_hash: str


def build_macro_chunks_from_records(
    trace_id: str,
    run_id: str,
    core_id: int,
    records: Iterable[dict],
    k_macro: int,
    workload: str,
    n_cores: int,
    uarch_hash_str: str,
    uarch_feats: Sequence[float],
    binary_hash: str,
) -> Tuple[List[MacroChunk], List[int]]:
    """Return (chunks, end_ticks_per_chunk).

    Each chunk contains exactly ``k_macro`` retired macros (final chunk may be
    smaller). Boundary is decided by ``is_last_microop=1`` count. Uses ``fetch``
    and ``commit_tick`` only to derive telescoping label boundaries; those fields
    are consumed here and never surface in the chunk payload.
    """
    chunks: List[MacroChunk] = []
    end_ticks: List[int] = []
    buf_uops: List[dict] = []
    macro_buf: List[dict] = []           # uops of currently building macro
    completed_macros: List[List[dict]] = []
    macros_done_in_chunk = 0
    uop_start = 0
    macro_start = 0
    chunk_id = 0
    current_last_commit = 0
    for rec in records:
        buf_uops.append(rec)
        macro_buf.append(rec)
        commit = int(rec.get("commit_tick") or 0)
        if commit > 0:
            current_last_commit = commit
        if int(rec.get("is_last_microop", 0) or 0):
            completed_macros.append(macro_buf)
            macro_buf = []
            macros_done_in_chunk += 1
            if macros_done_in_chunk >= k_macro:
                chunks.append(_pack_macro_chunk(
                    trace_id=trace_id, run_id=run_id, core_id=core_id,
                    chunk_id=chunk_id, uops=buf_uops, macros=completed_macros,
                    uop_start=uop_start, macro_start=macro_start,
                    workload=workload, n_cores=n_cores,
                    uarch_hash_str=uarch_hash_str, uarch_feats=uarch_feats,
                    binary_hash=binary_hash,
                ))
                end_ticks.append(current_last_commit if current_last_commit > 0 else 0)
                chunk_id += 1
                uop_start += len(buf_uops)
                macro_start += macros_done_in_chunk
                buf_uops = []
                completed_macros = []
                macros_done_in_chunk = 0
                current_last_commit = 0
    # Flush trailing partial chunk if any *retired* macros exist.
    # Uops belonging to an incomplete macro at trace end are discarded because
    # their retirement is not observed (would break additivity).
    if completed_macros:
        chunks.append(_pack_macro_chunk(
            trace_id=trace_id, run_id=run_id, core_id=core_id,
            chunk_id=chunk_id, uops=buf_uops[: sum(len(m) for m in completed_macros)],
            macros=completed_macros, uop_start=uop_start, macro_start=macro_start,
            workload=workload, n_cores=n_cores,
            uarch_hash_str=uarch_hash_str, uarch_feats=uarch_feats,
            binary_hash=binary_hash,
        ))
        end_ticks.append(current_last_commit if current_last_commit > 0 else 0)
    return chunks, end_ticks


def _pack_macro_chunk(*, trace_id: str, run_id: str, core_id: int, chunk_id: int,
                     uops: Sequence[dict], macros: Sequence[Sequence[dict]],
                     uop_start: int, macro_start: int, workload: str, n_cores: int,
                     uarch_hash_str: str, uarch_feats: Sequence[float],
                     binary_hash: str) -> MacroChunk:
    n_uops = len(uops)
    n_macros = len(macros)
    n_load = n_store = n_atomic = n_branch = n_cond = n_bmiss = 0
    n_int = n_fp = n_simd = n_ser = 0
    per_macro_pc: List[int] = []
    per_macro_uc: List[int] = []
    per_macro_oc: List[int] = []
    per_macro_fl: List[int] = []
    per_uop_lines: List[int] = []
    per_uop_access: List[int] = []
    for uops_of_macro in macros:
        head = uops_of_macro[0]
        last = uops_of_macro[-1]
        per_macro_pc.append(int(head.get("macro_pc") or 0))
        per_macro_uc.append(int(len(uops_of_macro)))
        per_macro_oc.append(int(last.get("op_class") or 0))
        per_macro_fl.append(_macro_flags(uops_of_macro))
        for u in uops_of_macro:
            per_uop_lines.append(int(_cacheline(u)))
            per_uop_access.append(int(_access_kind(u)))
            n_load += int(u.get("is_load", 0) or 0)
            n_store += int(u.get("is_store", 0) or 0)
            n_atomic += int(u.get("is_atomic", 0) or 0)
            b = int(u.get("is_branch", 0) or 0)
            n_branch += b
            n_cond += int(u.get("is_branch_cond", 0) or 0)
            n_bmiss += b * int(bool(u.get("mispredicted", 0)))
            n_int += int(u.get("is_int", 0) or 0)
            n_fp += int(u.get("is_fp", 0) or 0)
            n_simd += int(u.get("is_simd", 0) or 0)
            n_ser += int(u.get("is_serialize", 0) or 0)
    head = macros[0][0]
    tail = macros[-1][-1]
    return MacroChunk(
        trace_id=trace_id,
        run_id=run_id,
        core_id=int(core_id),
        chunk_id=int(chunk_id),
        uop_start=int(uop_start),
        uop_end=int(uop_start + n_uops),
        macro_start=int(macro_start),
        macro_end=int(macro_start + n_macros),
        n_uops=int(n_uops),
        n_macros=int(n_macros),
        n_load=int(n_load),
        n_store=int(n_store),
        n_atomic=int(n_atomic),
        n_branch=int(n_branch),
        n_cond_branch=int(n_cond),
        n_branch_miss=int(n_bmiss),
        n_int=int(n_int),
        n_fp=int(n_fp),
        n_simd=int(n_simd),
        n_serialize=int(n_ser),
        per_macro_static_pc=per_macro_pc,
        per_macro_uop_count=per_macro_uc,
        per_macro_op_class=per_macro_oc,
        per_macro_flags=per_macro_fl,
        per_uop_lines=per_uop_lines,
        per_uop_access=per_uop_access,
        boundary_start_seq=int(head.get("micro_seq") or head.get("seq_num") or 0),
        boundary_end_seq=int(tail.get("micro_seq") or tail.get("seq_num") or 0),
        workload=str(workload),
        n_cores=int(n_cores),
        uarch_hash=str(uarch_hash_str),
        uarch_features=list(uarch_feats),
        binary_hash=str(binary_hash),
    )


# ---------------------------------------------------------------------------
# Label build (mirrors TCSim ROI-anchored telescoping additivity)
# ---------------------------------------------------------------------------

def _macro_chunk_labels(chunks: Sequence[MacroChunk], end_ticks: Sequence[int],
                        first_fetch: Optional[int], roi_begin: int,
                        tick_per_cycle: float) -> List[dict]:
    tpc = float(tick_per_cycle) if tick_per_cycle > 0 else 1.0
    labels: List[dict] = []
    prev_end: Optional[int] = None
    # Anchor first chunk to per-core roi_begin (WORKBEGIN tick).
    for i, (ch, end_tick) in enumerate(zip(chunks, end_ticks)):
        start_tick: Optional[int]
        if prev_end is not None:
            start_tick = prev_end
        else:
            start_tick = int(roi_begin) if roi_begin > 0 else first_fetch
        valid = (
            end_tick is not None and end_tick > 0
            and start_tick is not None and int(end_tick) > int(start_tick)
        )
        if valid:
            delta_ticks = int(end_tick) - int(start_tick)
            dcycles = float(delta_ticks) / tpc
            row = {
                "trace_id": ch.trace_id,
                "run_id": ch.run_id,
                "core_id": ch.core_id,
                "chunk_id": ch.chunk_id,
                "delta_cycles": dcycles,
                "cpi_uop": dcycles / max(1, ch.n_uops),
                "cpi_macro": dcycles / max(1, ch.n_macros),
                "start_tick": int(start_tick),
                "end_tick": int(end_tick),
                "delta_ticks": delta_ticks,
                "n_uops": ch.n_uops,
                "n_macros": ch.n_macros,
                "boundary_start_seq": ch.boundary_start_seq,
                "boundary_end_seq": ch.boundary_end_seq,
                "valid_label": True,
                "quality_reason": "ok" if prev_end is not None else "ok_roi_anchored",
            }
            prev_end = int(end_tick)
        else:
            row = {
                "trace_id": ch.trace_id,
                "run_id": ch.run_id,
                "core_id": ch.core_id,
                "chunk_id": ch.chunk_id,
                "delta_cycles": None,
                "cpi_uop": None,
                "cpi_macro": None,
                "start_tick": int(start_tick) if start_tick is not None else None,
                "end_tick": int(end_tick) if end_tick else None,
                "delta_ticks": None,
                "n_uops": ch.n_uops,
                "n_macros": ch.n_macros,
                "boundary_start_seq": ch.boundary_start_seq,
                "boundary_end_seq": ch.boundary_end_seq,
                "valid_label": False,
                "quality_reason": "missing_or_nonpositive_boundary",
            }
        labels.append(row)
    return labels


def _assert_additive(labels: Sequence[dict], roi_begin: int, roi_end: int,
                     tol_frac: float = 0.005) -> Tuple[bool, float]:
    valid = [r for r in labels if r.get("valid_label")]
    if not valid:
        return False, float("inf")
    if len(valid) != len(labels):
        # some invalid rows exist; we still check the contiguous valid prefix
        pass
    summed = sum(int(r["delta_ticks"]) for r in valid)
    endpoint = int(valid[-1]["end_tick"]) - int(valid[0]["start_tick"])
    if endpoint <= 0:
        return False, float("inf")
    err = abs(summed - endpoint) / max(1, abs(endpoint))
    return err <= tol_frac, err


# ---------------------------------------------------------------------------
# End-to-end builder
# ---------------------------------------------------------------------------

def build_trace_macro(
    raw_dir: str,
    out_dir: str,
    k_macro: int,
    run_id: Optional[str] = None,
    binary_hash: Optional[str] = None,
    tol_frac: float = 0.005,
) -> dict:
    # Skip if already built (idempotent resume).
    chunks_path = os.path.join(out_dir, "chunks.parquet")
    labels_path = os.path.join(out_dir, "labels.parquet")
    meta_path = os.path.join(out_dir, "meta.json")
    if (os.path.isfile(chunks_path) and os.path.isfile(labels_path)
            and os.path.isfile(meta_path)):
        with open(meta_path, "r") as fh:
            return json.load(fh)
    trace_dir = os.path.join(raw_dir, "tao_trace")
    if not os.path.isdir(trace_dir):
        raise FileNotFoundError(f"missing tao_trace dir: {trace_dir}")
    profile = load_uarch_profile(trace_dir)
    profile_hash = uarch_hash(profile)
    uarch_feats = list(uarch_vector(profile))
    tpc = tick_per_cycle_from_profile(profile)
    workload = os.path.basename(raw_dir.rstrip("/"))
    n_cores_dir = int(profile.get("core", {}).get("num_cores") or 0)
    aligned_files = iter_core_aligned_files(trace_dir)
    core_ids = [int(c) for c, _ in aligned_files]
    roi = _load_roi_boundaries(trace_dir, core_ids)
    n_cores = len(aligned_files)
    if n_cores_dir and n_cores_dir != n_cores:
        # trust the count of core files (matches the actual dataset)
        pass
    trace_id_v = run_id or f"{os.path.basename(os.path.dirname(raw_dir.rstrip('/')))}/{workload}/{profile_hash[:12]}"
    bhash = str(binary_hash or "")
    os.makedirs(out_dir, exist_ok=True)
    all_chunks: List[MacroChunk] = []
    all_labels: List[dict] = []
    core_summary: List[dict] = []
    max_add_err = 0.0
    for core_id, aligned_path in aligned_files:
        core_id = int(core_id)
        first_fetch: Optional[int] = None
        def rec_iter() -> Iterator[dict]:
            nonlocal first_fetch
            first = True
            for row in _iter_aligned_rows(aligned_path):
                if first:
                    v = int(row.get("fetch_tick") or 0)
                    first_fetch = v if v > 0 else None
                    first = False
                # Only pass the functional record columns to the chunker; the
                # timing columns are consumed inside via commit_tick lookup.
                out = {k: row.get(k) for k in ALIGNED_RECORD_COLUMNS}
                out["commit_tick"] = row.get("commit_tick")
                out["fetch_tick"] = row.get("fetch_tick")
                out["mispredicted"] = row.get("mispredicted", 0)
                yield out
        chunks, end_ticks = build_macro_chunks_from_records(
            trace_id=trace_id_v, run_id=str(run_id or trace_id_v),
            core_id=core_id, records=rec_iter(), k_macro=int(k_macro),
            workload=workload, n_cores=n_cores, uarch_hash_str=profile_hash,
            uarch_feats=uarch_feats, binary_hash=bhash,
        )
        labels = _macro_chunk_labels(
            chunks, end_ticks,
            first_fetch=first_fetch, roi_begin=int(roi[core_id][0]),
            tick_per_cycle=float(tpc),
        )
        ok, err = _assert_additive(labels, int(roi[core_id][0]), int(roi[core_id][1]),
                                    tol_frac=tol_frac)
        max_add_err = max(max_add_err, err if math.isfinite(err) else 0.0)
        if not ok:
            raise RuntimeError(
                f"additivity failed core={core_id} rel_err={err:.6f} > tol={tol_frac} "
                f"raw={raw_dir}"
            )
        all_chunks.extend(chunks)
        all_labels.extend(labels)
        core_summary.append({
            "core_id": core_id,
            "n_chunks": len(chunks),
            "n_macros": sum(c.n_macros for c in chunks),
            "n_uops": sum(c.n_uops for c in chunks),
            "additivity_rel_err": err,
        })

    chunks_path = os.path.join(out_dir, "chunks.parquet")
    labels_path = os.path.join(out_dir, "labels.parquet")
    _write_chunks(all_chunks, chunks_path)
    _write_labels(all_labels, labels_path)
    meta = {
        "run_id": str(run_id or trace_id_v),
        "trace_id": trace_id_v,
        "workload": workload,
        "raw_dir": raw_dir,
        "uarch_hash": profile_hash,
        "tick_per_cycle": float(tpc),
        "k_macro": int(k_macro),
        "binary_hash": bhash,
        "cores": core_summary,
        "n_chunks": len(all_chunks),
        "additivity_max_rel_err": float(max_add_err),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def _write_chunks(rows: Sequence[MacroChunk], out_path: str) -> None:
    cols = {k: [] for k in [
        "trace_id", "run_id", "core_id", "chunk_id",
        "uop_start", "uop_end", "macro_start", "macro_end",
        "n_uops", "n_macros",
        "n_load", "n_store", "n_atomic", "n_branch",
        "n_cond_branch", "n_branch_miss",
        "n_int", "n_fp", "n_simd", "n_serialize",
        "per_macro_static_pc", "per_macro_uop_count",
        "per_macro_op_class", "per_macro_flags",
        "per_uop_lines", "per_uop_access",
        "boundary_start_seq", "boundary_end_seq",
        "workload", "n_cores", "uarch_hash", "uarch_features",
        "binary_hash",
    ]}
    for c in rows:
        cols["trace_id"].append(c.trace_id)
        cols["run_id"].append(c.run_id)
        cols["core_id"].append(int(c.core_id))
        cols["chunk_id"].append(int(c.chunk_id))
        cols["uop_start"].append(int(c.uop_start))
        cols["uop_end"].append(int(c.uop_end))
        cols["macro_start"].append(int(c.macro_start))
        cols["macro_end"].append(int(c.macro_end))
        cols["n_uops"].append(int(c.n_uops))
        cols["n_macros"].append(int(c.n_macros))
        cols["n_load"].append(int(c.n_load))
        cols["n_store"].append(int(c.n_store))
        cols["n_atomic"].append(int(c.n_atomic))
        cols["n_branch"].append(int(c.n_branch))
        cols["n_cond_branch"].append(int(c.n_cond_branch))
        cols["n_branch_miss"].append(int(c.n_branch_miss))
        cols["n_int"].append(int(c.n_int))
        cols["n_fp"].append(int(c.n_fp))
        cols["n_simd"].append(int(c.n_simd))
        cols["n_serialize"].append(int(c.n_serialize))
        cols["per_macro_static_pc"].append(list(c.per_macro_static_pc))
        cols["per_macro_uop_count"].append(list(c.per_macro_uop_count))
        cols["per_macro_op_class"].append(list(c.per_macro_op_class))
        cols["per_macro_flags"].append(list(c.per_macro_flags))
        cols["per_uop_lines"].append(list(c.per_uop_lines))
        cols["per_uop_access"].append(list(c.per_uop_access))
        cols["boundary_start_seq"].append(int(c.boundary_start_seq))
        cols["boundary_end_seq"].append(int(c.boundary_end_seq))
        cols["workload"].append(c.workload)
        cols["n_cores"].append(int(c.n_cores))
        cols["uarch_hash"].append(c.uarch_hash)
        cols["uarch_features"].append(list(c.uarch_features))
        cols["binary_hash"].append(c.binary_hash)
    tbl = pa.table(cols)
    tmp_path = out_path + ".tmp"
    pq.write_table(tbl, tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)


def _write_labels(rows: Sequence[dict], out_path: str) -> None:
    keys = [
        "trace_id", "run_id", "core_id", "chunk_id",
        "delta_cycles", "cpi_uop", "cpi_macro",
        "start_tick", "end_tick", "delta_ticks",
        "n_uops", "n_macros",
        "boundary_start_seq", "boundary_end_seq",
        "valid_label", "quality_reason",
    ]
    cols = {k: [r.get(k) for r in rows] for k in keys}
    tbl = pa.table(cols)
    tmp_path = out_path + ".tmp"
    pq.write_table(tbl, tmp_path, compression="zstd")
    os.replace(tmp_path, out_path)


# ---------------------------------------------------------------------------
# CLI / batch driver
# ---------------------------------------------------------------------------

def _iter_raw_dirs(raw_root: str, prefix: str, seeds: Sequence[int],
                   cores: Sequence[str]) -> Iterator[Tuple[str, str]]:
    for seed in seeds:
        for core_str in cores:
            root = os.path.join(raw_root, f"{prefix}_seed{seed}_c{core_str}")
            if not os.path.isdir(root):
                continue
            for name in sorted(os.listdir(root)):
                if not name.startswith("W_v28_"):
                    continue
                p = os.path.join(root, name)
                if not os.path.isdir(os.path.join(p, "tao_trace")):
                    continue
                run_id = f"v28_1_a2_sharedzipf_seed{seed}_c{core_str}_{name}"
                yield run_id, p


def _resolve_binary_hash(static_dict_manifest: Optional[str],
                         workload_name: str) -> str:
    """Look up ``binary_hash`` for a workload by stripping the ``W_`` prefix and
    matching the ``binary_name`` field of the static_dict manifest."""
    if not static_dict_manifest or not os.path.isfile(static_dict_manifest):
        return ""
    target = workload_name.removeprefix("W_")
    with open(static_dict_manifest, "r") as fh:
        for line in fh:
            row = json.loads(line)
            if str(row.get("binary_name")) == target:
                return str(row.get("binary_hash") or "")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-dir", default=None,
                    help="single raw workload dir (contains tao_trace/)")
    ap.add_argument("--raw-root", default="/data00/yinhaolang/TSim/data")
    ap.add_argument("--raw-prefix", default="raw_v28_1_business_a2_sharedzipf")
    ap.add_argument("--seeds", default="0,1")
    ap.add_argument("--cores", default="01,04,08,16,32")
    ap.add_argument("--out-root", default="/data00/yinhaolang/LLMSim/data/v28_1/chunks")
    ap.add_argument("--k-macro", type=int, default=256)
    ap.add_argument("--static-dict-manifest",
                    default="/data00/yinhaolang/LLMSim/data/v28_1/static_dict/manifest.jsonl")
    ap.add_argument("--tol-frac", type=float, default=0.005)
    ap.add_argument("--limit", type=int, default=0, help="max workloads to build (0=all)")
    ap.add_argument("--jobs", type=int, default=1,
                    help="parallel worker processes; 1=serial. Each worker is CPU+I/O "
                         "heavy so keep <= min(nproc, len(targets))")
    args = ap.parse_args()

    targets: List[Tuple[str, str]] = []
    if args.raw_dir:
        run_id = os.path.basename(args.raw_dir.rstrip("/"))
        targets = [(run_id, args.raw_dir)]
    else:
        seeds = [int(s) for s in args.seeds.split(",") if s]
        cores = [c.strip() for c in args.cores.split(",") if c.strip()]
        targets = list(_iter_raw_dirs(args.raw_root, args.raw_prefix, seeds, cores))
    if args.limit > 0:
        targets = targets[: args.limit]
    if not targets:
        print("[build_v28_1_macro_chunks] no targets", flush=True)
        return 1
    print(f"[build_v28_1_macro_chunks] {len(targets)} runs, K_macro={args.k_macro}, jobs={args.jobs}",
          flush=True)
    max_add_err = 0.0
    if args.jobs <= 1:
        for run_id, raw_dir in targets:
            wname = os.path.basename(raw_dir.rstrip("/"))
            bhash = _resolve_binary_hash(args.static_dict_manifest, wname)
            out_dir = os.path.join(args.out_root, run_id)
            os.makedirs(out_dir, exist_ok=True)
            try:
                t0 = time.time()
                meta = build_trace_macro(
                    raw_dir=raw_dir, out_dir=out_dir, k_macro=int(args.k_macro),
                    run_id=run_id, binary_hash=bhash, tol_frac=float(args.tol_frac),
                )
                dt = time.time() - t0
                max_add_err = max(max_add_err, float(meta.get("additivity_max_rel_err", 0.0)))
                print(f"  {run_id}  chunks={meta['n_chunks']}  add_err={meta['additivity_max_rel_err']:.2e}  dt={dt:.1f}s",
                      flush=True)
            except Exception as exc:
                print(f"[FAIL] {run_id}: {exc}", flush=True)
                raise
    else:
        # Parallel: one worker process per workload run. Each is independent.
        import concurrent.futures as cf
        specs: List[Tuple[str, str, str, str]] = []
        for run_id, raw_dir in targets:
            wname = os.path.basename(raw_dir.rstrip("/"))
            bhash = _resolve_binary_hash(args.static_dict_manifest, wname)
            out_dir = os.path.join(args.out_root, run_id)
            os.makedirs(out_dir, exist_ok=True)
            specs.append((run_id, raw_dir, bhash, out_dir))
        t_start = time.time()
        n_done = 0
        with cf.ProcessPoolExecutor(max_workers=int(args.jobs)) as pool:
            futures = {
                pool.submit(_build_one, s, int(args.k_macro), float(args.tol_frac)): s
                for s in specs
            }
            for fut in cf.as_completed(futures):
                spec = futures[fut]
                run_id = spec[0]
                try:
                    meta = fut.result()
                except Exception as exc:
                    print(f"[FAIL] {run_id}: {exc}", flush=True)
                    # Shut down remaining workers early on any failure.
                    for f in futures:
                        f.cancel()
                    raise
                max_add_err = max(max_add_err,
                                  float(meta.get("additivity_max_rel_err", 0.0)))
                n_done += 1
                dt_total = time.time() - t_start
                print(f"  [{n_done}/{len(specs)}] {run_id}  chunks={meta['n_chunks']}  "
                      f"add_err={meta['additivity_max_rel_err']:.2e}  wall={dt_total:.1f}s",
                      flush=True)
    print(f"[gate macro_chunks] additivity max_rel_err={max_add_err:.4%} tol={args.tol_frac:.1%}",
          flush=True)
    if max_add_err > float(args.tol_frac):
        print("[gate macro_chunks] FAIL", flush=True)
        return 2
    print("[gate macro_chunks] PASS", flush=True)
    return 0


def _build_one(spec: Tuple[str, str, str, str],
               k_macro: int, tol_frac: float) -> dict:
    """Worker entrypoint for ProcessPoolExecutor. Must be top-level for pickling."""
    run_id, raw_dir, bhash, out_dir = spec
    return build_trace_macro(
        raw_dir=raw_dir, out_dir=out_dir, k_macro=int(k_macro),
        run_id=run_id, binary_hash=bhash, tol_frac=float(tol_frac),
    )


if __name__ == "__main__":
    sys.exit(main())
