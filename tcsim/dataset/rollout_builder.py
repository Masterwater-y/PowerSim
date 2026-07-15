"""Phase 2: rollout cache + torch dataset."""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..chunker.fixed_chunk import Chunk, CHUNK_COLS, LABEL_COLS, build_trace, chunk_to_row
from ..scheduler.epsilon_resident import (
    EpsilonResidentScheduler,
    ScheduleSample,
    sample_to_row,
)
from ..utils.io import write_jsonl, iter_jsonl, dump_json
from ..utils.parquet import write_table, read_table

try:
    import numpy as np
except Exception:  # pragma: no cover - synthetic minimal environments
    np = None  # type: ignore


@dataclass
class RolloutArtifacts:
    chunks_path: str
    labels_path: str
    rollout_path: str
    meta_path: str
    n_chunks: int
    n_samples: int
    stats: dict


def _write_packed_cache(
    out_dir: str, chunks: List[Chunk], labels: List[dict], K: int,
) -> dict:
    """Write mmap-friendly per-trace tensors without Python-object expansion."""
    if np is None:
        raise RuntimeError("numpy is required for packed rollout caches")
    ordered = sorted(chunks, key=lambda ch: (ch.core_id, ch.chunk_id))
    packed_dir = os.path.join(out_dir, "packed")
    os.makedirs(packed_dir, exist_ok=True)
    n = len(ordered)
    n_fields = len(ordered[0].per_uop_fields[0])
    n_summary = len(ordered[0].chunk_summary)
    fields = np.lib.format.open_memmap(
        os.path.join(packed_dir, "fields.npy"), mode="w+", dtype=np.uint16,
        shape=(n, K, n_fields),
    )
    mask = np.lib.format.open_memmap(
        os.path.join(packed_dir, "mask.npy"), mode="w+", dtype=np.uint8,
        shape=(n, K),
    )
    summary = np.lib.format.open_memmap(
        os.path.join(packed_dir, "summary.npy"), mode="w+", dtype=np.float32,
        shape=(n, n_summary),
    )
    lines = np.lib.format.open_memmap(
        os.path.join(packed_dir, "lines.npy"), mode="w+", dtype=np.int64,
        shape=(n, K),
    )
    access = np.lib.format.open_memmap(
        os.path.join(packed_dir, "access.npy"), mode="w+", dtype=np.uint8,
        shape=(n, K),
    )
    # n_uops, load, store, atomic, branch, int, fp, simd, serialize,
    # delta_cycles, cpi, valid_label, retired_branch, conditional_branch,
    # all_retired_branch_miss
    scalar = np.lib.format.open_memmap(
        os.path.join(packed_dir, "scalar.npy"), mode="w+", dtype=np.float64,
        shape=(n, 15),
    )
    labels_by_key = {
        (int(row["core_id"]), int(row["chunk_id"])): row for row in labels
    }
    core_offsets: Dict[str, int] = {}
    core_counts: Dict[str, int] = {}
    for index, ch in enumerate(ordered):
        core_key = str(int(ch.core_id))
        core_offsets.setdefault(core_key, index)
        core_counts[core_key] = core_counts.get(core_key, 0) + 1
        fields[index] = np.asarray(ch.per_uop_fields, dtype=np.uint16)
        mask[index] = np.asarray(ch.valid_uop_mask, dtype=np.uint8)
        summary[index] = np.asarray(ch.chunk_summary, dtype=np.float32)
        lines[index] = np.asarray(ch.per_uop_lines, dtype=np.int64)
        access[index] = np.asarray(ch.per_uop_access, dtype=np.uint8)
        label = labels_by_key.get((int(ch.core_id), int(ch.chunk_id)), {})
        valid = bool(label.get("valid_label", label.get("delta_cycles") is not None))
        delta = float(label.get("delta_cycles") or 0.0) if valid else 0.0
        cpi = float(label.get("cpi") or delta / max(1, ch.n_uops)) if valid else 0.0
        scalar[index] = [
            ch.n_uops, ch.n_load, ch.n_store, ch.n_atomic, ch.n_branch,
            ch.n_int, ch.n_fp, ch.n_simd, ch.n_serialize,
            delta, cpi, float(valid), ch.n_branch, ch.n_cond_branch,
            ch.n_branch_miss,
        ]
    for array in (fields, mask, summary, lines, access, scalar):
        array.flush()
    return {
        "schema_version": "functional-v28.1-packed-2",
        "branch_opportunity_kind": "all_retired_branches",
        "relative_dir": "packed",
        "n_chunks": n,
        "K": K,
        "n_fields": n_fields,
        "n_summary": n_summary,
        "core_offsets": core_offsets,
        "core_counts": core_counts,
    }


def _group_chunks_by_core(chunks: List[Chunk]) -> Dict[int, List[Chunk]]:
    out: Dict[int, List[Chunk]] = {}
    for ch in chunks:
        out.setdefault(ch.core_id, []).append(ch)
    for lst in out.values():
        lst.sort(key=lambda x: x.chunk_id)
    return out


def _oracle_predictor_factory(
    labels_by_key: Dict[Tuple[str, int, int], Optional[float]],
) -> Callable:
    """Oracle duration policy used only to select first-stage context chunks.

    Scheduler timing produced by this policy is serialized under ``audit_*``
    keys and is deliberately excluded by :class:`TCSimSampleDataset`.
    """
    def predict(core_id: int, chunk: Chunk, _state: dict, _ctx: List[dict]) -> float:
        v = labels_by_key.get((chunk.trace_id, chunk.core_id, chunk.chunk_id))
        if v is None or v <= 0:
            return float(chunk.n_uops)  # CPI=1 fallback
        return float(v)
    return predict


def build_and_dump_trace(
    trace_dir: str,
    out_dir: str,
    K: int,
    epsilon: float,
    tick_per_cycle: Optional[float],
    max_forward_budget: Optional[int],
    trace_id: Optional[str] = None,
    max_resident_exposure: int = 0,
    cache_format: str = "both",
    input_format: str = "auto",
) -> RolloutArtifacts:
    chunks, labels = build_trace(
        trace_dir,
        K=K,
        trace_id=trace_id,
        tick_per_cycle=tick_per_cycle,
        input_format=input_format,
    )
    if not chunks:
        raise RuntimeError(f"no chunks built from {trace_dir}")
    trace_id = chunks[0].trace_id
    labels_by_key: Dict[Tuple[str, int, int], Optional[float]] = {
        (r["trace_id"], r["core_id"], r["chunk_id"]): r.get("delta_cycles") for r in labels
    }
    chunks_by_core = _group_chunks_by_core(chunks)
    predictor = _oracle_predictor_factory(labels_by_key)
    sched = EpsilonResidentScheduler(
        chunks_by_core,
        predictor,
        epsilon=epsilon,
        max_forward_budget=max_forward_budget,
        max_resident_exposure=max_resident_exposure,
        trace_id=trace_id,
    )
    samples = sched.run()
    if sched.stats.n_commits != len(chunks):
        raise RuntimeError(
            "incomplete rollout: "
            f"commits={sched.stats.n_commits} chunks={len(chunks)} "
            f"budget={max_forward_budget}"
        )

    os.makedirs(out_dir, exist_ok=True)
    chunks_path = os.path.join(out_dir, "chunks.parquet")
    labels_path = os.path.join(out_dir, "labels.parquet")
    rollout_path = os.path.join(out_dir, "rollout.jsonl")
    meta_path = os.path.join(out_dir, "meta.json")

    cache_format = str(cache_format).lower()
    if cache_format not in {"parquet", "packed", "both"}:
        raise ValueError("cache_format must be parquet, packed, or both")
    if cache_format in {"parquet", "both"}:
        write_table(chunks_path, [chunk_to_row(c) for c in chunks], schema_cols=CHUNK_COLS)
        write_table(labels_path, labels, schema_cols=LABEL_COLS)
    packed_meta = (
        _write_packed_cache(out_dir, chunks, labels, K)
        if cache_format in {"packed", "both"} else None
    )
    write_jsonl(rollout_path, [sample_to_row(s) for s in samples])
    dump_json(meta_path, {
        "trace_id": trace_id,
        "K": K,
        "epsilon": epsilon,
        "tick_per_cycle": chunks[0].extras.get("tick_per_cycle"),
        "rollout_mode": "oracle_context",
        "model_input_contract": "functional_only_v27_3_branch_aux",
        "feature_schema": "functional17_summary27_relation14",
        "cache_format": cache_format,
        "input_format": input_format,
        "packed": packed_meta,
        "uarch_hash": chunks[0].uarch_hash,
        "uarch_features": list(chunks[0].uarch_features),
        "auxiliary_label_contract": {
            "branch_opportunities": "all retired control-flow instructions per chunk",
            "branch_misses": "prediction failures among all retired branches per chunk",
            "contract": "all_retired_branches_v28.1",
            "model_input": False,
        },
        "n_chunks": len(chunks),
        "n_samples": len(samples),
        "n_cores": len(chunks_by_core),
        "scheduler_stats": {
            "n_samples": sched.stats.n_samples,
            "n_commits": sched.stats.n_commits,
            "n_resident_events": sched.stats.n_resident_events,
            "n_unique_chunk_encodes": sched.stats.n_unique_chunk_encodes,
            "max_exposure": sched.stats.max_exposure,
        },
    })
    return RolloutArtifacts(
        chunks_path=chunks_path,
        labels_path=labels_path,
        rollout_path=rollout_path,
        meta_path=meta_path,
        n_chunks=len(chunks),
        n_samples=len(samples),
        stats={
            "n_commits": sched.stats.n_commits,
            "n_resident_events": sched.stats.n_resident_events,
            "n_unique_chunk_encodes": sched.stats.n_unique_chunk_encodes,
            "max_exposure": sched.stats.max_exposure,
        },
    )


def find_trace_dirs(root: str, workloads: Optional[Sequence[str]] = None) -> List[str]:
    """Discover <root>/<workload>/tao_trace directories.

    If `workloads` is None, all subdirectories are considered.
    """
    if not os.path.isdir(root):
        raise FileNotFoundError(root)
    if workloads is None:
        candidates = sorted([d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))])
    else:
        candidates = list(workloads)
    out: List[str] = []
    for wl in candidates:
        tao = os.path.join(root, wl, "tao_trace")
        if os.path.isdir(tao):
            out.append(tao)
    return out
