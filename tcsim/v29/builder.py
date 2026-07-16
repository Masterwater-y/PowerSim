"""Build mmap-friendly common-oracle-time v29 trace caches."""
from __future__ import annotations

import math
import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ..chunker.fixed_chunk import (
    _iter_aligned_rows,
    _load_roi_boundaries,
    iter_core_aligned_files,
)
from ..chunker.functional_features import functional_line, functional_page, physical_line
from ..utils.io import dump_json
from .contracts import (
    FIELD_NAMES,
    RESOURCE_KEY_NAMES,
    feature_contract_metadata,
    normalized_horizons,
)
from .features import (
    FunctionalFeatureEncoderV29,
    load_trace_profile,
    predictor_hash,
    semantic_flags,
    tick_per_cycle,
    uarch_hash,
    uarch_vector,
)

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore


@dataclass(frozen=True)
class V29TraceArtifacts:
    out_dir: str
    meta_path: str
    trace_id: str
    n_cores: int
    n_uops: int
    n_samples: int


def _require_numpy() -> Any:
    if np is None:
        raise RuntimeError("numpy is required to build v29 caches")
    return np


def _parquet_rows(path: str) -> int:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pyarrow is required to build v29 caches") from exc
    return int(pq.ParquetFile(path).metadata.num_rows)


def _open_array(path: str, dtype: Any, shape: Tuple[int, ...]):
    numpy = _require_numpy()
    return numpy.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)


def _build_core(
    *,
    core_id: int,
    aligned_path: str,
    core_dir: str,
    profile: Dict[str, Any],
    decoder,
    roi_begin_tick: int,
    roi_end_tick: int,
) -> Dict[str, Any]:
    numpy = _require_numpy()
    os.makedirs(core_dir, exist_ok=True)
    count = _parquet_rows(aligned_path)
    if count <= 0:
        raise RuntimeError(f"empty aligned trace {aligned_path}")
    arrays = {
        "fields": _open_array(
            os.path.join(core_dir, "fields.npy"), numpy.uint16,
            (count, len(FIELD_NAMES)),
        ),
        "resource": _open_array(
            os.path.join(core_dir, "resource.npy"), numpy.int64,
            (count, len(RESOURCE_KEY_NAMES)),
        ),
        "commit_tick": _open_array(
            os.path.join(core_dir, "commit_tick.npy"), numpy.int64, (count,),
        ),
        "physical_line": _open_array(
            os.path.join(core_dir, "physical_line.npy"), numpy.int64, (count,),
        ),
        "functional_line": _open_array(
            os.path.join(core_dir, "functional_line.npy"), numpy.int64, (count,),
        ),
        "functional_page": _open_array(
            os.path.join(core_dir, "functional_page.npy"), numpy.int64, (count,),
        ),
        "producer_log": _open_array(
            os.path.join(core_dir, "producer_log.npy"), numpy.float32, (count,),
        ),
        "semantic_flags": _open_array(
            os.path.join(core_dir, "semantic_flags.npy"), numpy.uint8, (count,),
        ),
        "access": _open_array(
            os.path.join(core_dir, "access.npy"), numpy.uint8, (count,),
        ),
        "macro_pc": _open_array(
            os.path.join(core_dir, "macro_pc.npy"), numpy.uint64, (count,),
        ),
        "macro_end": _open_array(
            os.path.join(core_dir, "macro_end.npy"), numpy.uint8, (count,),
        ),
        "branch": _open_array(
            os.path.join(core_dir, "branch.npy"), numpy.uint8, (count,),
        ),
        "branch_miss": _open_array(
            os.path.join(core_dir, "branch_miss.npy"), numpy.uint8, (count,),
        ),
    }
    encoder = FunctionalFeatureEncoderV29(profile, decoder)
    previous_commit = -1
    branch_count = 0
    branch_misses = 0
    macro_count = 0
    paddr_valid = 0
    atomic_count = 0
    index = 0
    for row in _iter_aligned_rows(aligned_path):
        if index >= count:
            raise RuntimeError(f"parquet row count changed while reading {aligned_path}")
        observed_core = int(row.get("core_id", core_id) or core_id)
        if observed_core != int(core_id):
            raise RuntimeError(
                f"core mismatch path={core_id} row={observed_core} at {index}"
            )
        commit = int(row.get("commit_tick", 0) or 0)
        if commit <= 0:
            raise RuntimeError(f"missing commit_tick core={core_id} row={index}")
        if commit < previous_commit:
            raise RuntimeError(
                f"non-monotonic commit tick core={core_id} row={index}: "
                f"{commit} < {previous_commit}"
            )
        if commit < int(roi_begin_tick) or commit > int(roi_end_tick):
            raise RuntimeError(
                f"commit outside per-core ROI core={core_id} row={index}: "
                f"{commit} not in [{roi_begin_tick},{roi_end_tick}]"
            )
        fields, producer_log, resource = encoder.encode(row)
        arrays["fields"][index] = numpy.asarray(fields, dtype=numpy.uint16)
        arrays["resource"][index] = numpy.asarray(resource, dtype=numpy.int64)
        arrays["commit_tick"][index] = commit
        p_line = physical_line(row)
        f_line = functional_line(row)
        f_page = functional_page(row)
        arrays["physical_line"][index] = -1 if p_line is None else int(p_line)
        arrays["functional_line"][index] = -1 if f_line is None else int(f_line)
        arrays["functional_page"][index] = -1 if f_page is None else int(f_page)
        arrays["producer_log"][index] = float(producer_log)
        flags = semantic_flags(row)
        arrays["semantic_flags"][index] = flags
        if int(row.get("is_atomic", 0) or 0):
            access = 3
            atomic_count += 1
        elif int(row.get("is_store", 0) or 0):
            access = 2
        elif int(row.get("is_load", 0) or 0):
            access = 1
        else:
            access = 0
        arrays["access"][index] = access
        arrays["macro_pc"][index] = int(
            row.get("macro_pc", row.get("micro_pc", 0)) or 0
        )
        is_micro = bool(int(row.get("is_microop", 0) or 0))
        macro_end = (not is_micro) or bool(int(row.get("is_last_microop", 0) or 0))
        arrays["macro_end"][index] = int(macro_end)
        macro_count += int(macro_end)
        branch = int(bool(row.get("is_branch", 0)))
        miss = branch * int(bool(row.get("mispredicted", 0)))
        arrays["branch"][index] = branch
        arrays["branch_miss"][index] = miss
        branch_count += branch
        branch_misses += miss
        paddr_valid += int(p_line is not None)
        previous_commit = commit
        index += 1
    if index != count:
        raise RuntimeError(
            f"parquet row count mismatch expected={count} observed={index}: {aligned_path}"
        )
    for array in arrays.values():
        array.flush()
    first_commit = int(arrays["commit_tick"][0])
    last_commit = int(arrays["commit_tick"][-1])
    del arrays
    return {
        "core_id": int(core_id),
        "relative_dir": os.path.relpath(core_dir, os.path.dirname(core_dir)),
        "n_uops": count,
        "n_macros": macro_count,
        "n_branches": branch_count,
        "n_branch_misses": branch_misses,
        "n_atomics": atomic_count,
        "paddr_valid_fraction": paddr_valid / max(1, count),
        "roi_begin_tick": int(roi_begin_tick),
        "roi_end_tick": int(roi_end_tick),
        "first_commit_tick": first_commit,
        "last_commit_tick": last_commit,
    }


def _build_samples(
    *,
    tmp_dir: str,
    core_meta: Sequence[Dict[str, Any]],
    sample_period_cycles: float,
    block_cycles: float,
    tpc: float,
    horizons: Sequence[float],
    K: int,
    max_samples: Optional[int],
) -> Tuple[int, Dict[str, Any]]:
    numpy = _require_numpy()
    period_ticks = int(round(float(sample_period_cycles) * float(tpc)))
    block_ticks = int(round(float(block_cycles) * float(tpc)))
    if period_ticks <= 0 or block_ticks <= period_ticks:
        raise ValueError("sample period/block size must be positive and block > period")
    start_tick = min(int(meta["roi_begin_tick"]) for meta in core_meta)
    stop_tick = max(int(meta["last_commit_tick"]) for meta in core_meta)
    sample_ticks = numpy.arange(start_tick, stop_tick, period_ticks, dtype=numpy.int64)
    if max_samples is not None and int(max_samples) > 0 and len(sample_ticks) > int(max_samples):
        positions = numpy.linspace(0, len(sample_ticks) - 1, int(max_samples), dtype=numpy.int64)
        sample_ticks = sample_ticks[positions]
    cursors = numpy.full((len(sample_ticks), len(core_meta)), -1, dtype=numpy.int32)
    commits_by_core = []
    for column, meta in enumerate(core_meta):
        core_dir = os.path.join(tmp_dir, "cores", str(int(meta["core_id"])))
        commits = numpy.load(os.path.join(core_dir, "commit_tick.npy"), mmap_mode="r")
        commits_by_core.append(commits)
        positions = numpy.searchsorted(commits, sample_ticks, side="right")
        active = (
            (sample_ticks >= int(meta["roi_begin_tick"]))
            & (sample_ticks < int(meta["last_commit_tick"]))
            & (positions < int(meta["n_uops"]))
        )
        cursors[active, column] = positions[active].astype(numpy.int32)
    keep = (cursors >= 0).any(axis=1)
    sample_ticks = sample_ticks[keep]
    cursors = cursors[keep]
    block_ids = ((sample_ticks - start_tick) // block_ticks).astype(numpy.int32)
    numpy.save(os.path.join(tmp_dir, "sample_ticks.npy"), sample_ticks)
    numpy.save(os.path.join(tmp_dir, "sample_cursors.npy"), cursors)
    numpy.save(os.path.join(tmp_dir, "sample_block_ids.npy"), block_ids)

    audit_indices = numpy.linspace(
        0, max(0, len(sample_ticks) - 1), min(10000, len(sample_ticks)),
        dtype=numpy.int64,
    ) if len(sample_ticks) else numpy.asarray([], dtype=numpy.int64)
    no_commit = 0
    active_rows = 0
    horizon_counts = {
        float(horizon): {"rows": 0, "zero": 0, "full": 0, "sum": 0.0}
        for horizon in horizons
    }
    residuals: List[float] = []
    for sample_index in audit_indices:
        tick = int(sample_ticks[sample_index])
        for column, cursor in enumerate(cursors[sample_index]):
            if int(cursor) < 0:
                continue
            active_rows += 1
            commits = commits_by_core[column]
            next_tick = int(commits[int(cursor)])
            no_commit += int(next_tick - tick > period_ticks)
            residuals.append((next_tick - tick) / float(tpc))
            for horizon in horizons:
                end = int(numpy.searchsorted(
                    commits,
                    tick + int(round(float(horizon) * float(tpc))),
                    side="right",
                ))
                progress = min(int(K), max(0, end - int(cursor)))
                stats = horizon_counts[float(horizon)]
                stats["rows"] += 1
                stats["zero"] += int(progress == 0)
                stats["full"] += int(progress == int(K))
                stats["sum"] += progress
    horizon_audit = {}
    for horizon, stats in horizon_counts.items():
        rows = max(1, int(stats["rows"]))
        horizon_audit[str(horizon)] = {
            "active_rows": int(stats["rows"]),
            "zero_fraction": int(stats["zero"]) / rows,
            "full_fraction": int(stats["full"]) / rows,
            "mean_progress": float(stats["sum"]) / rows,
        }
    return len(sample_ticks), {
        "start_tick": start_tick,
        "stop_tick": stop_tick,
        "sample_period_ticks": period_ticks,
        "sample_period_cycles": float(sample_period_cycles),
        "block_ticks": block_ticks,
        "block_cycles": float(block_cycles),
        "n_blocks": int(block_ids.max()) + 1 if len(block_ids) else 0,
        "no_commit_fraction_at_sample_period": no_commit / max(1, active_rows),
        "audit_active_rows": active_rows,
        "horizon_audit": horizon_audit,
        "head_residual_cycles": {
            "p50": float(numpy.percentile(residuals, 50)) if residuals else 0.0,
            "p90": float(numpy.percentile(residuals, 90)) if residuals else 0.0,
            "p99": float(numpy.percentile(residuals, 99)) if residuals else 0.0,
        },
    }


def build_trace_cache(
    trace_dir: str,
    out_dir: str,
    *,
    K: int = 256,
    horizons: Iterable[float] = (16, 32, 64, 128, 256, 512, 1024),
    sample_period_cycles: float = 64.0,
    block_cycles: float = 65536.0,
    trace_id: Optional[str] = None,
    max_samples: Optional[int] = None,
    overwrite: bool = False,
) -> V29TraceArtifacts:
    _require_numpy()
    if int(K) != 256:
        raise ValueError("v29 contract fixes functional lookahead K=256")
    horizon_values = normalized_horizons(horizons)
    profile, decoder = load_trace_profile(trace_dir)
    tpc = tick_per_cycle(profile)
    aligned = iter_core_aligned_files(trace_dir)
    core_ids = [int(core) for core, _ in aligned]
    boundaries = _load_roi_boundaries(trace_dir, core_ids)
    workload = os.path.basename(os.path.dirname(trace_dir.rstrip("/")))
    raw_root = os.path.basename(os.path.dirname(os.path.dirname(trace_dir.rstrip("/"))))
    profile_hash = uarch_hash(profile)
    trace_id = trace_id or f"{raw_root}/{workload}/{profile_hash[:12]}"
    predictor = predictor_hash(profile)
    decoder_hash = decoder.provenance_hash()

    out_dir = os.path.abspath(out_dir)
    tmp_dir = out_dir + f".tmp-{os.getpid()}"
    if os.path.exists(out_dir):
        if not overwrite:
            raise FileExistsError(out_dir)
        shutil.rmtree(out_dir)
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(os.path.join(tmp_dir, "cores"), exist_ok=True)
    try:
        core_meta = []
        for core_id, path in aligned:
            begin, end = boundaries[int(core_id)]
            core_meta.append(_build_core(
                core_id=int(core_id),
                aligned_path=path,
                core_dir=os.path.join(tmp_dir, "cores", str(int(core_id))),
                profile=profile,
                decoder=decoder,
                roi_begin_tick=begin,
                roi_end_tick=end,
            ))
        core_meta.sort(key=lambda item: int(item["core_id"]))
        n_samples, sample_meta = _build_samples(
            tmp_dir=tmp_dir,
            core_meta=core_meta,
            sample_period_cycles=sample_period_cycles,
            block_cycles=block_cycles,
            tpc=tpc,
            horizons=horizon_values,
            K=int(K),
            max_samples=max_samples,
        )
        contracts = feature_contract_metadata(
            predictor_hash=predictor,
            resource_decoder_hash=decoder_hash,
            horizons=horizon_values,
            sample_period_cycles=sample_period_cycles,
        )
        metadata = {
            **contracts,
            "trace_id": trace_id,
            "workload": workload,
            "raw_root": raw_root,
            "trace_dir": os.path.abspath(trace_dir),
            "K": int(K),
            "tick_per_cycle": float(tpc),
            "n_cores": len(core_meta),
            "core_ids": [int(item["core_id"]) for item in core_meta],
            "n_uops": sum(int(item["n_uops"]) for item in core_meta),
            "n_samples": n_samples,
            "cores": core_meta,
            "sample_grid": sample_meta,
            "uarch_hash": profile_hash,
            "uarch_features": uarch_vector(profile),
            "uarch_profile": profile,
            "resource_decoder": decoder.metadata(),
            "quality": {
                "status": "pass",
                "commit_ticks_monotonic": True,
                "common_time_grid": True,
                "contains_no_commit_states": (
                    sample_meta["no_commit_fraction_at_sample_period"] > 0
                ),
                "atomic_uops": sum(int(item["n_atomics"]) for item in core_meta),
            },
        }
        dump_json(os.path.join(tmp_dir, "meta.json"), metadata)
        os.replace(tmp_dir, out_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return V29TraceArtifacts(
        out_dir=out_dir,
        meta_path=os.path.join(out_dir, "meta.json"),
        trace_id=trace_id,
        n_cores=len(core_meta),
        n_uops=sum(int(item["n_uops"]) for item in core_meta),
        n_samples=n_samples,
    )
