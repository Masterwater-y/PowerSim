"""Phase 0: fixed-K functional chunk builder.

Reads a gem5-style tao_trace directory (records.micro.jsonl + labels.micro.jsonl
per core) and emits a flat list of fixed-K functional chunks. Chunk boundaries
depend only on functional UOP index; real ticks are used only for post-hoc
labels (see plan §1.2).

Also supports a lightweight synthetic-record source for smoke tests.
"""
from __future__ import annotations

import glob
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..utils.io import iter_jsonl
from .functional_features import (
    CHUNK_SUMMARY_NAMES,
    FIELD_INDEX,
    FIELD_PAD_IDS,
    RESOURCE_KEY_INDEX,
    RESOURCE_KEY_INVALID,
    FunctionalFeatureEncoder,
    chunk_summary,
    functional_line,
    is_mem,
    is_write,
    load_uarch_profile,
    tick_per_cycle_from_profile,
    uarch_hash,
    uarch_vector,
    physical_line,
    predictor_hash,
)

CORE_RE = re.compile(r"(?:cores|switch)(\d*)\.core")
ALIGNED_SUFFIX = ".aligned.parquet"

# Aligned parquet contains additional timing/oracle columns.  Keep the input
# contract explicit: only these functional record fields are reconstructed for
# the feature encoder.  ``fetch_tick``/``commit_tick`` are read separately and
# are used solely to form boundary labels.
ALIGNED_RECORD_COLUMNS = (
    "core_id", "thread_id", "micro_seq", "seq_num",
    "macro_pc", "micro_pc", "vaddr", "paddr",
    "cacheline_addr", "cacheline_paddr", "size",
    "is_load", "is_store", "is_atomic", "is_branch",
    "is_branch_cond", "is_branch_indirect", "is_call", "is_return",
    "branch_taken", "branch_target", "branch_next_pc", "branch_history",
    "is_int", "is_fp", "is_simd", "is_serialize",
    "is_microop", "is_last_microop", "op_class", "n_src", "n_dst",
    "producer_dists", "producer_classes",
)
ALIGNED_TIMING_COLUMNS = ("fetch_tick", "commit_tick")
# Auxiliary labels are deliberately not functional model inputs.  They may be
# absent in old/synthetic traces, in which case the label is masked to zero.
ALIGNED_AUX_LABEL_COLUMNS = ("mispredicted",)

_FUNC_FIELDS = (
    "is_load", "is_store", "is_atomic", "is_branch", "is_branch_cond",
    "is_branch_indirect", "is_call", "is_return", "is_int", "is_fp",
    "is_simd", "is_serialize", "is_microop", "is_last_microop",
    "n_src", "n_dst",
)

_SYNC_FIELDS = ("is_atomic", "is_serialize")


@dataclass
class Chunk:
    trace_id: str
    core_id: int
    chunk_id: int
    uop_start: int
    uop_end: int          # exclusive
    n_uops: int
    # aggregated functional features (all counts within the chunk)
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
    op_class_hist: Dict[int, int]
    # sync markers
    has_atomic: bool
    has_serialize: bool
    # per-uop compact feature matrix (op_class + 8 flags) as list of ints —
    # cheap enough for a K=256 chunk (256 * 9 ints).
    per_uop_op_class: List[int]
    per_uop_flags: List[int]   # bit 0 load, 1 store, 2 atomic, 3 branch, 4 int, 5 fp, 6 simd, 7 serialize
    # rich functional-only model inputs
    per_uop_fields: List[List[int]]
    valid_uop_mask: List[int]
    chunk_summary: List[float]
    read_lines: List[int]
    write_lines: List[int]
    per_uop_lines: List[int]
    per_uop_access: List[int]
    # Non-model-facing exact physical resource keys used only to recompute
    # dynamic active-context equality/fanout features.
    per_uop_resource_keys: List[List[int]]
    # boundary keys for label join (inclusive micro-sequence ids)
    boundary_start_seq: int
    boundary_end_seq: int
    # optional trace-level meta filled later
    workload: str = ""
    n_cores: int = 0
    uarch_hash: str = ""
    uarch_features: List[float] = field(default_factory=list)
    extras: Dict[str, Any] = field(default_factory=dict)


def _core_id_from_path(path: str) -> int:
    m = CORE_RE.search(os.path.basename(path))
    if not m:
        return 0
    return int(m.group(1) or "0")


def iter_core_record_files(trace_dir: str) -> List[Tuple[int, str]]:
    """Return (core_id, records_path) tuples sorted by core_id."""
    pattern = os.path.join(trace_dir, "*records.micro.jsonl*")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"no records.micro jsonl under {trace_dir}")
    return sorted([(_core_id_from_path(f), f) for f in files])


def iter_core_aligned_files(trace_dir: str) -> List[Tuple[int, str]]:
    """Return per-core aligned parquet paths sorted by core id.

    The collector writes one outer-joined row per functional micro-op.  This
    is a storage/input format only; timing columns never become model fields.
    """
    files = sorted(glob.glob(os.path.join(trace_dir, f"*{ALIGNED_SUFFIX}")))
    if not files:
        raise FileNotFoundError(f"no aligned parquet under {trace_dir}")
    return sorted([(_core_id_from_path(f), f) for f in files])


def _load_roi_boundaries(trace_dir: str, core_ids: Sequence[int]) -> Dict[int, Tuple[int, int]]:
    path = os.path.join(trace_dir, "roi_boundaries.jsonl")
    if not os.path.isfile(path):
        raise RuntimeError(
            "raw trace predates per-core ROI contract: roi_boundaries.jsonl missing; "
            "recollect raw data"
        )
    events: Dict[int, List[dict]] = {int(core): [] for core in core_ids}
    depth_by_core: Dict[int, int] = {}
    global_depth = 0
    for row in iter_jsonl(path):
        core = int(row.get("core_id", -1))
        depth_by_core.setdefault(core, 0)
        event = str(row.get("event", ""))
        if event == "begin":
            depth_by_core[core] += 1
            global_depth += 1
        elif event == "end":
            if depth_by_core[core] <= 0 or global_depth <= 0:
                raise RuntimeError(f"unmatched ROI end core={core}")
            depth_by_core[core] -= 1
            global_depth -= 1
        else:
            raise RuntimeError(f"unknown ROI boundary event {event!r}")
        if (
            int(row.get("core_depth", -1)) != depth_by_core[core]
            or int(row.get("global_depth", -1)) != global_depth
        ):
            raise RuntimeError(f"ROI depth accounting mismatch core={core}")
        if core in events:
            events[core].append(row)
    if global_depth != 0 or any(depth_by_core.values()):
        raise RuntimeError("ROI boundary stream ended with nonzero depth")
    out: Dict[int, Tuple[int, int]] = {}
    for core in core_ids:
        rows = events[int(core)]
        begins = [row for row in rows if row.get("event") == "begin"]
        ends = [row for row in rows if row.get("event") == "end"]
        if len(begins) != 1 or len(ends) != 1:
            raise RuntimeError(
                f"ROI boundary contract failed core={core}: "
                f"begin={len(begins)} end={len(ends)}"
            )
        begin = int(begins[0].get("tick", 0) or 0)
        end = int(ends[0].get("tick", 0) or 0)
        if end <= begin or int(ends[0].get("matched", 1) or 0) != 1:
            raise RuntimeError(f"invalid/unmatched ROI boundary core={core}")
        out[int(core)] = (begin, end)
    return out


def _iter_aligned_rows(path: str) -> Iterator[dict]:
    """Stream the minimal functional+boundary-label column set from parquet."""
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:  # pragma: no cover - import depends on runtime env
        raise RuntimeError("pyarrow is required to read aligned parquet") from exc
    required = list(
        ALIGNED_RECORD_COLUMNS + ALIGNED_TIMING_COLUMNS + ALIGNED_AUX_LABEL_COLUMNS
    )
    parquet = pq.ParquetFile(path)
    available = set(parquet.schema_arrow.names)
    missing = set(required) - available
    if missing:
        raise RuntimeError(f"aligned parquet missing required columns {sorted(missing)}: {path}")
    columns = required
    for batch in parquet.iter_batches(batch_size=65536, columns=columns):
        for row in batch.to_pylist():
            yield row


def _label_path_for(rec_path: str) -> Optional[str]:
    lbl = rec_path.replace("records.micro.jsonl", "labels.micro.jsonl")
    if lbl == rec_path:
        return None
    return lbl if os.path.exists(lbl) else None


def _flags(rec: dict) -> int:
    return (
        (int(rec.get("is_load", 0)) & 1)
        | ((int(rec.get("is_store", 0)) & 1) << 1)
        | ((int(rec.get("is_atomic", 0)) & 1) << 2)
        | ((int(rec.get("is_branch", 0)) & 1) << 3)
        | ((int(rec.get("is_int", 0)) & 1) << 4)
        | ((int(rec.get("is_fp", 0)) & 1) << 5)
        | ((int(rec.get("is_simd", 0)) & 1) << 6)
        | ((int(rec.get("is_serialize", 0)) & 1) << 7)
    )


def build_chunks_from_records(
    trace_id: str,
    core_id: int,
    records: Iterable[dict],
    K: int,
    pad_opclass: int = 127,
    uarch_profile: Optional[Dict[str, Any]] = None,
    resource_seed: str = "",
) -> List[Chunk]:
    chunks: List[Chunk] = []
    buf: List[dict] = []
    buf_fields: List[List[int]] = []
    buf_resources: List[List[int]] = []
    buf_producer_logs: List[float] = []
    encoder = FunctionalFeatureEncoder(
        uarch_profile=uarch_profile or {}, resource_seed=resource_seed or trace_id,
    )
    chunk_id = 0
    for rec in records:
        fields, producer_log, resource_keys = encoder.encode(rec)
        buf.append(rec)
        buf_fields.append(fields)
        buf_resources.append(resource_keys)
        buf_producer_logs.append(producer_log)
        if len(buf) >= K:
            chunks.append(_pack_chunk(
                trace_id, core_id, chunk_id, buf, buf_fields,
                buf_resources, buf_producer_logs, K, pad_opclass,
            ))
            chunk_id += 1
            buf = []
            buf_fields = []
            buf_resources = []
            buf_producer_logs = []
    if buf:
        chunks.append(_pack_chunk(
            trace_id, core_id, chunk_id, buf, buf_fields,
            buf_resources, buf_producer_logs, K, pad_opclass,
        ))
    return chunks


def _pack_chunk(
    trace_id: str,
    core_id: int,
    chunk_id: int,
    recs: List[dict],
    feature_rows: List[List[int]],
    resource_rows: List[List[int]],
    producer_logs: List[float],
    K: int,
    pad_opclass: int,
) -> Chunk:
    n = len(recs)
    op_hist: Dict[int, int] = {}
    per_op: List[int] = []
    per_fl: List[int] = []
    n_load = n_store = n_atomic = n_branch = n_cond_branch = n_branch_miss = 0
    n_int = n_fp = n_simd = n_ser = 0
    for r in recs:
        oc = int(r.get("op_class", 0) or 0)
        op_hist[oc] = op_hist.get(oc, 0) + 1
        per_op.append(oc)
        per_fl.append(_flags(r))
        n_load += int(r.get("is_load", 0) or 0)
        n_store += int(r.get("is_store", 0) or 0)
        n_atomic += int(r.get("is_atomic", 0) or 0)
        n_branch += int(r.get("is_branch", 0) or 0)
        branch = int(r.get("is_branch", 0) or 0)
        cond = int(r.get("is_branch_cond", 0) or 0)
        n_cond_branch += cond
        # v28.1: branch head covers every retired control-flow instruction,
        # including direct/indirect, call and return, not conditional only.
        n_branch_miss += branch * int(bool(r.get("mispredicted", 0)))
        n_int += int(r.get("is_int", 0) or 0)
        n_fp += int(r.get("is_fp", 0) or 0)
        n_simd += int(r.get("is_simd", 0) or 0)
        n_ser += int(r.get("is_serialize", 0) or 0)
    valid_mask = [1] * n
    model_fields = [list(x) for x in feature_rows]
    resource_keys = [list(x) for x in resource_rows]
    for key_name, pressure_name in (
        ("l1_set", "l1_set_pressure"),
        ("l2_set", "l2_set_pressure"),
        ("llc_set", "llc_set_pressure"),
    ):
        key_index = RESOURCE_KEY_INDEX[key_name]
        counts = Counter(
            int(row[key_index]) for row in resource_rows
            if int(row[key_index]) >= 0
        )
        field_index = FIELD_INDEX[pressure_name]
        for fields, keys in zip(model_fields, resource_rows):
            key = int(keys[key_index])
            fields[field_index] = (
                min(9, 1 + int(math.log2(counts[key]))) if key >= 0 else 0
            )
    # pad tail chunk to K so per-uop tensors are rectangular in cache
    while len(per_op) < K:
        per_op.append(pad_opclass)
        per_fl.append(0)
        model_fields.append(list(FIELD_PAD_IDS))
        resource_keys.append([RESOURCE_KEY_INVALID] * len(RESOURCE_KEY_INDEX))
        valid_mask.append(0)
    # Functional indices are independent of trace sequence-number conventions.
    uop_start = chunk_id * K
    uop_end = uop_start + n
    boundary_start_seq = int(recs[0].get("micro_seq", recs[0].get("seq_num", 0)) or 0)
    boundary_end_seq = int(recs[-1].get("micro_seq", recs[-1].get("seq_num", 0)) or 0)
    read_lines = sorted({
        int(physical_line(r)) for r in recs
        if is_mem(r) and not is_write(r) and physical_line(r) is not None
    })
    write_lines = sorted({
        int(physical_line(r)) for r in recs
        if is_write(r) and physical_line(r) is not None
    })
    return Chunk(
        trace_id=trace_id,
        core_id=core_id,
        chunk_id=chunk_id,
        uop_start=uop_start,
        uop_end=uop_start + n,
        n_uops=n,
        n_load=n_load,
        n_store=n_store,
        n_atomic=n_atomic,
        n_branch=n_branch,
        n_cond_branch=n_cond_branch,
        n_branch_miss=n_branch_miss,
        n_int=n_int,
        n_fp=n_fp,
        n_simd=n_simd,
        n_serialize=n_ser,
        op_class_hist=op_hist,
        has_atomic=n_atomic > 0,
        has_serialize=n_ser > 0,
        per_uop_op_class=per_op,
        per_uop_flags=per_fl,
        per_uop_fields=model_fields,
        valid_uop_mask=valid_mask,
        chunk_summary=chunk_summary(recs, producer_logs, model_fields[:n], resource_rows, K),
        read_lines=read_lines,
        write_lines=write_lines,
        per_uop_lines=[
            int(physical_line(r)) if is_mem(r) and physical_line(r) is not None else -1
            for r in recs
        ] + [-1] * (K - n),
        per_uop_access=[
            3 if int(r.get("is_atomic", 0) or 0)
            else 2 if int(r.get("is_store", 0) or 0)
            else 1 if int(r.get("is_load", 0) or 0)
            else 0
            for r in recs
        ] + [0] * (K - n),
        per_uop_resource_keys=resource_keys,
        boundary_start_seq=boundary_start_seq,
        boundary_end_seq=boundary_end_seq,
    )


def load_timing_labels(labels_path: str) -> Dict[int, dict]:
    """Return timing rows keyed by functional micro-sequence id."""
    out: Dict[int, dict] = {}
    if not labels_path or not os.path.exists(labels_path):
        return out
    for row in iter_jsonl(labels_path):
        s = int(row.get("seq_num") or row.get("micro_seq") or 0)
        ct = int(row.get("commit_tick") or 0)
        if s > 0 and ct > 0:
            out[s] = {
                "commit_tick": ct,
                "fetch_tick": int(row.get("fetch_tick") or 0),
                "mispredicted": int(bool(row.get("mispredicted", 0))),
            }
    return out


def load_labels(labels_path: str) -> Dict[int, int]:
    """Backward-compatible ``{seq_num: commit_tick}`` view."""
    return {k: int(v["commit_tick"]) for k, v in load_timing_labels(labels_path).items()}


def _timing_value(value: Any, key: str) -> Optional[int]:
    if isinstance(value, dict):
        v = int(value.get(key) or 0)
    elif key == "commit_tick" and value is not None:
        v = int(value)
    else:
        v = 0
    return v if v > 0 else None


def compute_chunk_labels(
    chunks: List[Chunk],
    labels_by_seq: Dict[int, Any],
    tick_per_cycle: float,
) -> List[dict]:
    """Produce additive boundary-to-boundary duration labels.

    Chunk 0 starts at the first UOP's fetch tick when available.  Later chunks
    start at the previous chunk's final commit tick.  This makes labels
    telescope exactly over the trace-visible fetch-to-final-commit span.
    """
    rows: List[dict] = []
    if not chunks:
        return rows
    prev_end_tick: Optional[int] = None
    tpc = float(tick_per_cycle) if tick_per_cycle > 0 else 1.0
    first_seq = chunks[0].boundary_start_seq
    first_timing = labels_by_seq.get(first_seq)
    first_commit = _timing_value(first_timing, "commit_tick")
    first_start = _timing_value(first_timing, "fetch_tick")
    if first_start is None and first_commit is not None:
        # Legacy/synthetic labels may only provide commit ticks.  Infer the
        # first interval from the next distinct commit boundary.
        later = sorted(
            _timing_value(v, "commit_tick") for k, v in labels_by_seq.items()
            if int(k) > first_seq and _timing_value(v, "commit_tick") is not None
        )
        gap = (later[0] - first_commit) if later else int(round(tpc))
        first_start = first_commit - max(1, int(gap))

    for ch in chunks:
        last_seq = ch.boundary_end_seq
        end_tick = _timing_value(labels_by_seq.get(last_seq), "commit_tick")
        if end_tick is None:
            # Fall back only within this chunk's actual sequence boundaries.
            for s in range(last_seq, ch.boundary_start_seq - 1, -1):
                if s in labels_by_seq:
                    end_tick = _timing_value(labels_by_seq[s], "commit_tick")
                    if end_tick is not None:
                        break
        start_tick = prev_end_tick if prev_end_tick is not None else first_start
        valid = end_tick is not None and start_tick is not None and end_tick > start_tick
        if not valid:
            row = {
                "trace_id": ch.trace_id,
                "core_id": ch.core_id,
                "chunk_id": ch.chunk_id,
                "delta_cycles": None,
                "cpi": None,
                "start_tick": start_tick,
                "end_tick": end_tick,
                "delta_ticks": None,
                "n_uops": ch.n_uops,
                "boundary_start_seq": ch.boundary_start_seq,
                "boundary_end_seq": ch.boundary_end_seq,
                "valid_label": False,
                "quality_reason": "missing_or_nonpositive_boundary",
            }
        else:
            delta_ticks = int(end_tick - start_tick)
            dcycles = float(delta_ticks) / tpc
            row = {
                "trace_id": ch.trace_id,
                "core_id": ch.core_id,
                "chunk_id": ch.chunk_id,
                "delta_cycles": dcycles,
                "cpi": dcycles / max(1, ch.n_uops),
                "start_tick": start_tick,
                "end_tick": end_tick,
                "delta_ticks": delta_ticks,
                "n_uops": ch.n_uops,
                "boundary_start_seq": ch.boundary_start_seq,
                "boundary_end_seq": ch.boundary_end_seq,
                "valid_label": True,
                "quality_reason": "ok",
            }
        rows.append(row)
        if end_tick is not None:
            prev_end_tick = end_tick
    return rows


def _anchor_labels_to_roi(
    labels: List[dict], roi_begin: int, tick_per_cycle: float,
) -> None:
    """Make the first chunk include the complete cold-start ROI prefix.

    Initial ROI UOPs can be fetched before WORKBEGIN retires.  Fetch time is
    therefore not a legal full-ROI origin; the per-core WORKBEGIN tick is.
    Later chunks already chain from the preceding commit endpoint.
    """
    if not labels or not labels[0].get("valid_label"):
        return
    end_tick = int(labels[0].get("end_tick") or 0)
    if end_tick <= int(roi_begin):
        labels[0].update({
            "delta_cycles": None,
            "cpi": None,
            "start_tick": int(roi_begin),
            "delta_ticks": None,
            "valid_label": False,
            "quality_reason": "first_boundary_not_after_roi_begin",
        })
        return
    delta_ticks = end_tick - int(roi_begin)
    labels[0].update({
        "start_tick": int(roi_begin),
        "delta_ticks": delta_ticks,
        "delta_cycles": float(delta_ticks) / max(1e-12, float(tick_per_cycle)),
        "cpi": (
            float(delta_ticks) / max(1e-12, float(tick_per_cycle))
            / max(1, int(labels[0].get("n_uops") or 0))
        ),
        "quality_reason": "ok_roi_anchored",
    })


def _aligned_chunks_and_labels(
    trace_id: str,
    core_id: int,
    aligned_path: str,
    K: int,
    pad_opclass: int,
    tick_per_cycle: float,
    uarch_profile: Dict[str, Any],
    resource_seed: str,
) -> Tuple[List[Chunk], List[dict]]:
    """Build fixed chunks and timing labels in one aligned-parquet pass.

    Keeping only the last valid commit boundary of each K-UOP block avoids a
    per-UOP Python timing dictionary for multi-million-UOP traces.  It has the
    same boundary semantics as :func:`compute_chunk_labels`: an absent commit
    at the exact end boundary falls back to the latest valid commit inside the
    current block.
    """
    first_fetch: Optional[int] = None
    first_commits: List[int] = []
    end_ticks: List[Optional[int]] = []
    current_last_commit: Optional[int] = None
    emitted = 0

    def records() -> Iterator[dict]:
        nonlocal first_fetch, current_last_commit, emitted
        for row in _iter_aligned_rows(aligned_path):
            if emitted == 0:
                value = int(row.get("fetch_tick") or 0)
                first_fetch = value if value > 0 else None
            commit = int(row.get("commit_tick") or 0)
            if commit > 0:
                current_last_commit = commit
                if len(first_commits) < 2:
                    first_commits.append(commit)
            record = {key: row.get(key) for key in ALIGNED_RECORD_COLUMNS}
            # Auxiliary supervision is carried through chunk aggregation but
            # is never passed to FunctionalFeatureEncoder/model inputs.
            for key in ALIGNED_AUX_LABEL_COLUMNS:
                record[key] = row.get(key, 0)
            yield record
            emitted += 1
            if emitted % K == 0:
                end_ticks.append(current_last_commit)
                current_last_commit = None
        if emitted % K:
            end_ticks.append(current_last_commit)

    chunks = build_chunks_from_records(
        trace_id, core_id, records(), K, pad_opclass,
        uarch_profile=uarch_profile, resource_seed=resource_seed,
    )
    if len(chunks) != len(end_ticks):
        raise RuntimeError(
            f"aligned chunk/boundary mismatch core={core_id} chunks={len(chunks)} "
            f"boundaries={len(end_ticks)} path={aligned_path}"
        )

    tpc = float(tick_per_cycle) if tick_per_cycle > 0 else 1.0
    first_start = first_fetch
    if first_start is None and first_commits:
        gap = (
            first_commits[1] - first_commits[0]
            if len(first_commits) > 1 else int(round(tpc))
        )
        first_start = first_commits[0] - max(1, int(gap))

    labels: List[dict] = []
    prev_end_tick: Optional[int] = None
    for ch, end_tick in zip(chunks, end_ticks):
        start_tick = prev_end_tick if prev_end_tick is not None else first_start
        valid = end_tick is not None and start_tick is not None and end_tick > start_tick
        if valid:
            delta_ticks = int(end_tick - start_tick)
            dcycles = float(delta_ticks) / tpc
            row = {
                "trace_id": ch.trace_id,
                "core_id": ch.core_id,
                "chunk_id": ch.chunk_id,
                "delta_cycles": dcycles,
                "cpi": dcycles / max(1, ch.n_uops),
                "start_tick": start_tick,
                "end_tick": end_tick,
                "delta_ticks": delta_ticks,
                "n_uops": ch.n_uops,
                "boundary_start_seq": ch.boundary_start_seq,
                "boundary_end_seq": ch.boundary_end_seq,
                "valid_label": True,
                "quality_reason": "ok",
            }
        else:
            row = {
                "trace_id": ch.trace_id,
                "core_id": ch.core_id,
                "chunk_id": ch.chunk_id,
                "delta_cycles": None,
                "cpi": None,
                "start_tick": start_tick,
                "end_tick": end_tick,
                "delta_ticks": None,
                "n_uops": ch.n_uops,
                "boundary_start_seq": ch.boundary_start_seq,
                "boundary_end_seq": ch.boundary_end_seq,
                "valid_label": False,
                "quality_reason": "missing_or_nonpositive_boundary",
            }
        labels.append(row)
        if end_tick is not None:
            prev_end_tick = end_tick
    return chunks, labels


def build_trace(
    trace_dir: str,
    K: int,
    trace_id: Optional[str] = None,
    tick_per_cycle: Optional[float] = None,
    pad_opclass: int = 127,
    input_format: str = "auto",
) -> Tuple[List[Chunk], List[dict]]:
    """Build chunks + labels for a single tao_trace directory.

    Returns (chunks, label_rows).
    """
    workload = os.path.basename(os.path.dirname(trace_dir.rstrip("/")))
    raw_root = os.path.basename(os.path.dirname(os.path.dirname(trace_dir.rstrip("/"))))
    profile = load_uarch_profile(trace_dir)
    profile_hash = uarch_hash(profile)
    trace_id = trace_id or f"{raw_root}/{workload}/{profile_hash[:12]}"
    branch_predictor_hash = predictor_hash(profile)
    tpc = float(tick_per_cycle) if tick_per_cycle is not None else tick_per_cycle_from_profile(profile)
    uarch_feats = uarch_vector(profile)
    input_format = str(input_format).lower()
    if input_format not in {"auto", "raw", "aligned"}:
        raise ValueError("input_format must be auto, raw, or aligned")
    try:
        raw_files = iter_core_record_files(trace_dir) if input_format != "aligned" else []
    except FileNotFoundError:
        raw_files = []
    try:
        aligned_files = iter_core_aligned_files(trace_dir) if input_format != "raw" else []
    except FileNotFoundError:
        aligned_files = []
    if input_format == "raw":
        core_files = raw_files
        source = "raw"
    elif input_format == "aligned":
        if not aligned_files:
            raise FileNotFoundError(f"no aligned parquet under {trace_dir}")
        core_files = aligned_files
        source = "aligned"
    elif aligned_files and (not raw_files or len(aligned_files) == len(raw_files)):
        core_files = aligned_files
        source = "aligned"
    else:
        core_files = raw_files
        source = "raw"
    if not core_files:
        raise FileNotFoundError(f"no {source} core traces under {trace_dir}")
    roi_boundaries = _load_roi_boundaries(
        trace_dir, [int(core_id) for core_id, _ in core_files],
    )
    all_chunks: List[Chunk] = []
    all_labels: List[dict] = []
    for core_id, rec_path in core_files:
        if source == "aligned":
            chunks, label_rows = _aligned_chunks_and_labels(
                trace_id, core_id, rec_path, K, pad_opclass, tpc,
                profile, trace_id,
            )
        else:
            lbl_path = _label_path_for(rec_path)
            lbls = load_timing_labels(lbl_path) if lbl_path else {}
            def raw_records() -> Iterator[dict]:
                for record in iter_jsonl(rec_path):
                    seq = int(record.get("micro_seq", record.get("seq_num", 0)) or 0)
                    record["mispredicted"] = int(lbls.get(seq, {}).get("mispredicted", 0))
                    yield record
            chunks = build_chunks_from_records(
                trace_id, core_id, raw_records(), K, pad_opclass,
                uarch_profile=profile, resource_seed=trace_id,
            )
            label_rows = compute_chunk_labels(chunks, lbls, tpc)
        roi_begin, roi_end = roi_boundaries[int(core_id)]
        _anchor_labels_to_roi(label_rows, roi_begin, tpc)
        for ch in chunks:
            ch.n_cores = len(core_files)
            ch.workload = workload
            ch.uarch_hash = profile_hash
            ch.uarch_features = list(uarch_feats)
            ch.extras["tick_per_cycle"] = tpc
            ch.extras["predictor_hash"] = branch_predictor_hash
        valid_rows = [r for r in label_rows if r.get("valid_label")]
        outside = [
            row for row in valid_rows
            if int(row.get("start_tick") or 0) < roi_begin
            or int(row.get("end_tick") or 0) > roi_end
        ]
        if outside:
            raise RuntimeError(
                f"timing label lies outside per-core ROI core={core_id}; recollect raw data"
            )
        if valid_rows and len(valid_rows) == len(label_rows):
            summed = sum(int(r["delta_ticks"]) for r in valid_rows)
            endpoint = int(valid_rows[-1]["end_tick"]) - int(valid_rows[0]["start_tick"])
            if summed != endpoint:
                raise RuntimeError(
                    f"non-additive labels trace={trace_id} core={core_id}: "
                    f"sum={summed} endpoint={endpoint}"
                )
        all_chunks.extend(chunks)
        all_labels.extend(label_rows)
    return all_chunks, all_labels


def chunk_to_row(ch: Chunk) -> dict:
    """Flatten a Chunk into a parquet-friendly row (lists of ints kept intact)."""
    return {
        "trace_id": ch.trace_id,
        "core_id": ch.core_id,
        "chunk_id": ch.chunk_id,
        "uop_start": ch.uop_start,
        "uop_end": ch.uop_end,
        "n_uops": ch.n_uops,
        "n_load": ch.n_load,
        "n_store": ch.n_store,
        "n_atomic": ch.n_atomic,
        "n_branch": ch.n_branch,
        "n_branch_opportunities": ch.n_branch,
        "n_cond_branch": ch.n_cond_branch,
        "n_branch_miss": ch.n_branch_miss,
        "n_int": ch.n_int,
        "n_fp": ch.n_fp,
        "n_simd": ch.n_simd,
        "n_serialize": ch.n_serialize,
        "has_atomic": bool(ch.has_atomic),
        "has_serialize": bool(ch.has_serialize),
        "per_uop_op_class": list(ch.per_uop_op_class),
        "per_uop_flags": list(ch.per_uop_flags),
        "per_uop_fields": [list(x) for x in ch.per_uop_fields],
        "valid_uop_mask": list(ch.valid_uop_mask),
        "chunk_summary": list(ch.chunk_summary),
        "read_lines": list(ch.read_lines),
        "write_lines": list(ch.write_lines),
        "per_uop_lines": list(ch.per_uop_lines),
        "per_uop_access": list(ch.per_uop_access),
        "per_uop_resource_keys": [list(x) for x in ch.per_uop_resource_keys],
        "workload": ch.workload,
        "n_cores": ch.n_cores,
        "uarch_hash": ch.uarch_hash,
        "uarch_features": list(ch.uarch_features),
        "boundary_start_seq": ch.boundary_start_seq,
        "boundary_end_seq": ch.boundary_end_seq,
    }


CHUNK_COLS = [
    "trace_id", "core_id", "chunk_id", "uop_start", "uop_end", "n_uops",
    "n_load", "n_store", "n_atomic", "n_branch", "n_branch_opportunities",
    "n_cond_branch", "n_branch_miss",
    "n_int", "n_fp", "n_simd",
    "n_serialize", "has_atomic", "has_serialize",
    "per_uop_op_class", "per_uop_flags",
    "per_uop_fields", "valid_uop_mask", "chunk_summary",
    "read_lines", "write_lines", "per_uop_lines", "per_uop_access",
    "per_uop_resource_keys",
    "workload", "n_cores", "uarch_hash", "uarch_features",
    "boundary_start_seq", "boundary_end_seq",
]

LABEL_COLS = [
    "trace_id", "core_id", "chunk_id",
    "delta_cycles", "cpi", "start_tick", "end_tick", "delta_ticks", "n_uops",
    "boundary_start_seq", "boundary_end_seq", "valid_label", "quality_reason",
]
