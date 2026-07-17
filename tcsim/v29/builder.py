"""Build mmap-friendly common-oracle-time v29 trace caches."""
from __future__ import annotations

import math
import os
import re
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
from .dataset import FUNCTIONAL_CONTAINER_SCHEMA
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


FUNCTIONAL_ALIGNED_COLUMNS = frozenset({
    "core_id", "macro_pc", "micro_pc", "vaddr", "paddr",
    "cacheline_addr", "cacheline_paddr", "size",
    "is_load", "is_store", "is_atomic", "is_branch",
    "is_branch_cond", "is_branch_indirect", "is_call", "is_return",
    "branch_taken", "branch_target", "branch_next_pc", "branch_history",
    "is_int", "is_fp", "is_simd", "is_serialize", "op_class",
    "is_microop", "is_last_microop", "n_src", "n_dst",
    "producer_dists", "producer_classes",
})
ORACLE_ALIGNED_COLUMNS = frozenset({"commit_tick", "mispredicted"})


def _parquet_rows(path: str, *, include_oracle: bool) -> int:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError("pyarrow is required to build v29 caches") from exc
    parquet = pq.ParquetFile(path)
    names = set(parquet.schema_arrow.names)
    required = set(FUNCTIONAL_ALIGNED_COLUMNS)
    if include_oracle:
        required.update(ORACLE_ALIGNED_COLUMNS)
    missing = sorted(required - names)
    if missing:
        raise RuntimeError(
            f"v29 aligned parquet is missing required columns {missing}: {path}"
        )
    rows = int(parquet.metadata.num_rows)
    if rows <= 0:
        raise RuntimeError(f"v29 aligned parquet is empty: {path}")
    return rows


def _open_array(path: str, dtype: Any, shape: Tuple[int, ...]):
    numpy = _require_numpy()
    return numpy.lib.format.open_memmap(path, mode="w+", dtype=dtype, shape=shape)


def _read_key_value_file(path: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not os.path.isfile(path):
        return values
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _collection_provenance(trace_dir: str) -> Dict[str, Any]:
    """Verify the pre-ROI Atomic -> detailed O3/Ruby collection transition.

    ``is_atomic`` in the trace describes an ISA UOP and is intentionally zero
    for the initial workload contract.  It is unrelated to gem5's
    ``AtomicSimpleCPU`` fast-forward mode, so both facts are recorded under
    unambiguous names.
    """
    workload_dir = os.path.dirname(os.path.abspath(trace_dir.rstrip("/")))
    collect_path = os.path.join(workload_dir, "collect.meta")
    gem5_log_path = os.path.join(workload_dir, "gem5.log")
    collect = _read_key_value_file(collect_path)
    command_has_ff_atomic = False
    command_has_roi_gate = False
    observed_o3_ruby_switch = False
    if os.path.isfile(gem5_log_path):
        with open(gem5_log_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "command line:" in line:
                    command_has_ff_atomic = command_has_ff_atomic or bool(
                        re.search(r"(?:^|\s)--ff-atomic(?:\s|$)", line)
                    )
                    command_has_roi_gate = command_has_roi_gate or bool(
                        re.search(r"(?:^|\s)--require-roi(?:\s|$)", line)
                    )
                if "first WORKBEGIN -> switch Atomic -> O3+Ruby" in line:
                    observed_o3_ruby_switch = True
    metadata_ff_atomic = collect.get("ff_atomic") == "1"
    verified = bool(
        metadata_ff_atomic
        and command_has_ff_atomic
        and command_has_roi_gate
        and observed_o3_ruby_switch
    )
    return {
        "collect_meta_path": collect_path,
        "gem5_log_path": gem5_log_path,
        "collect_meta_present": os.path.isfile(collect_path),
        "gem5_log_present": os.path.isfile(gem5_log_path),
        "collect_meta_ff_atomic": metadata_ff_atomic,
        "command_has_ff_atomic": command_has_ff_atomic,
        "command_has_require_roi": command_has_roi_gate,
        "observed_atomic_to_o3_ruby_switch": observed_o3_ruby_switch,
        "ff_atomic_verified": verified,
        "pre_roi_cpu": "AtomicSimpleCPU" if verified else "unknown",
        "roi_cpu": "O3+Ruby" if verified else "unknown",
        "ruby_cache_at_roi_entry": "cold" if verified else "unknown",
        "declared_num_cores": (
            int(collect["num_cores"])
            if str(collect.get("num_cores", "")).isdigit() else None
        ),
    }


def _build_core(
    *,
    core_id: int,
    aligned_path: str,
    core_dir: str,
    profile: Dict[str, Any],
    decoder,
    roi_begin_tick: Optional[int],
    roi_end_tick: Optional[int],
    include_oracle: bool = True,
) -> Dict[str, Any]:
    numpy = _require_numpy()
    os.makedirs(core_dir, exist_ok=True)
    count = _parquet_rows(aligned_path, include_oracle=include_oracle)
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
    }
    if include_oracle:
        arrays.update({
            "commit_tick": _open_array(
                os.path.join(core_dir, "commit_tick.npy"), numpy.int64, (count,),
            ),
            "branch_miss": _open_array(
                os.path.join(core_dir, "branch_miss.npy"), numpy.uint8, (count,),
            ),
        })
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
        if include_oracle:
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
        if include_oracle:
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
        raw_miss = int(bool(row.get("mispredicted", 0))) if include_oracle else 0
        if raw_miss and not branch:
            raise RuntimeError(
                f"branch miss label is not attached to a retired control UOP "
                f"core={core_id} row={index}"
            )
        miss = branch * raw_miss
        arrays["branch"][index] = branch
        if include_oracle:
            arrays["branch_miss"][index] = miss
        branch_count += branch
        branch_misses += miss
        paddr_valid += int(p_line is not None)
        if include_oracle:
            previous_commit = commit
        index += 1
    if index != count:
        raise RuntimeError(
            f"parquet row count mismatch expected={count} observed={index}: {aligned_path}"
        )
    for array in arrays.values():
        array.flush()
    first_commit = int(arrays["commit_tick"][0]) if include_oracle else None
    last_commit = int(arrays["commit_tick"][-1]) if include_oracle else None
    del arrays
    result = {
        "core_id": int(core_id),
        "relative_dir": os.path.relpath(core_dir, os.path.dirname(core_dir)),
        "n_uops": count,
        "n_macros": macro_count,
        "n_branches": branch_count,
        "n_atomics": atomic_count,
        "paddr_valid_fraction": paddr_valid / max(1, count),
    }
    if include_oracle:
        result.update({
            "n_branch_misses": branch_misses,
            "roi_begin_tick": int(roi_begin_tick),
            "roi_end_tick": int(roi_end_tick),
            "first_commit_tick": int(first_commit),
            "last_commit_tick": int(last_commit),
        })
    return result


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
    full_grid_samples = len(sample_ticks)
    selected_grid_offset = 0
    if max_samples is not None and int(max_samples) > 0 and len(sample_ticks) > int(max_samples):
        # A sequence loss assumes adjacent cache indices are exactly one sample
        # period apart.  Never downsample with linspace: that creates sparse
        # points while metadata still claims a dense common-time grid.  A
        # centered contiguous slice keeps smoke/audit caches semantically valid.
        selected_grid_offset = (len(sample_ticks) - int(max_samples)) // 2
        sample_ticks = sample_ticks[
            selected_grid_offset:selected_grid_offset + int(max_samples)
        ]
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
        "full_grid_samples": int(full_grid_samples),
        "selected_grid_offset": int(selected_grid_offset),
        "max_samples_contiguous": (
            int(max_samples) if max_samples is not None and int(max_samples) > 0 else None
        ),
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
    min_uops_per_core: int = 500000,
    max_uops_per_core: int = 1000000,
    max_full_uop_cpi: float = 10.0,
    require_ff_atomic: bool = True,
    overwrite: bool = False,
) -> V29TraceArtifacts:
    _require_numpy()
    if int(K) != 256:
        raise ValueError("v29 contract fixes functional lookahead K=256")
    horizon_values = normalized_horizons(horizons)
    if not any(abs(value - float(sample_period_cycles)) <= 1.0e-6 for value in horizon_values):
        raise ValueError("v29 sample_period_cycles must be present in horizons")
    if int(min_uops_per_core) <= 0 or int(max_uops_per_core) < int(min_uops_per_core):
        raise ValueError("invalid v29 per-core UOP bounds")
    if float(max_full_uop_cpi) <= 0:
        raise ValueError("max_full_uop_cpi must be positive")
    collection = _collection_provenance(trace_dir)
    if require_ff_atomic and not collection["ff_atomic_verified"]:
        raise RuntimeError(
            "v29 training trace lacks verified --ff-atomic -> O3/Ruby ROI "
            f"provenance: {collection}"
        )
    profile, decoder = load_trace_profile(trace_dir)
    tpc = tick_per_cycle(profile)
    aligned = iter_core_aligned_files(trace_dir)
    core_ids = [int(core) for core, _ in aligned]
    declared_cores = collection.get("declared_num_cores")
    if declared_cores is not None and int(declared_cores) != len(core_ids):
        raise RuntimeError(
            f"collection/core stream mismatch declared={declared_cores} "
            f"observed={len(core_ids)}"
        )
    boundaries = _load_roi_boundaries(trace_dir, core_ids)
    roi_begin_ticks = {int(boundaries[core_id][0]) for core_id in core_ids}
    if len(roi_begin_ticks) != 1:
        raise RuntimeError(
            "v29 deployment contract requires every core active at the same T0; "
            f"ROI begin ticks={sorted(roi_begin_ticks)}"
        )
    workload = os.path.basename(os.path.dirname(trace_dir.rstrip("/")))
    raw_root = os.path.basename(os.path.dirname(os.path.dirname(trace_dir.rstrip("/"))))
    profile_hash = uarch_hash(profile)
    trace_id = trace_id or f"{raw_root}/{workload}/{profile_hash[:12]}"
    predictor = predictor_hash(profile)
    decoder_hash = decoder.provenance_hash()

    out_dir = os.path.abspath(out_dir)
    tmp_dir = out_dir + f".tmp-{os.getpid()}"
    backup_dir = out_dir + f".old-{os.getpid()}"
    if os.path.exists(out_dir):
        if not overwrite:
            raise FileExistsError(out_dir)
    if os.path.exists(tmp_dir):
        shutil.rmtree(tmp_dir)
    if os.path.exists(backup_dir):
        shutil.rmtree(backup_dir)
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
        out_of_range_uops = [
            (int(item["core_id"]), int(item["n_uops"]))
            for item in core_meta
            if not int(min_uops_per_core) <= int(item["n_uops"]) <= int(max_uops_per_core)
        ]
        if out_of_range_uops:
            raise RuntimeError(
                "v29 per-core UOP count outside "
                f"[{int(min_uops_per_core)},{int(max_uops_per_core)}]: "
                f"{out_of_range_uops}"
            )
        for item in core_meta:
            duration_cycles = (
                int(item["last_commit_tick"]) - int(item["roi_begin_tick"])
            ) / tpc
            item["full_uop_cpi"] = duration_cycles / max(1, int(item["n_uops"]))
            item["full_macro_cpi"] = duration_cycles / max(1, int(item["n_macros"]))
        excessive_cpi = [
            (int(item["core_id"]), float(item["full_uop_cpi"]))
            for item in core_meta
            if float(item["full_uop_cpi"]) > float(max_full_uop_cpi)
        ]
        if excessive_cpi:
            raise RuntimeError(
                f"v29 full UOP CPI exceeds {max_full_uop_cpi}: {excessive_cpi}"
            )
        atomic_uops = sum(int(item["n_atomics"]) for item in core_meta)
        if atomic_uops:
            raise RuntimeError(
                "v29 ROI ISA-atomic UOP contract requires zero "
                f"(independent of FFATOMIC fast-forward), got {atomic_uops}"
            )
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
            "min_uops_per_core_contract": int(min_uops_per_core),
            "max_uops_per_core_contract": int(max_uops_per_core),
            "max_full_uop_cpi_contract": float(max_full_uop_cpi),
            "collection_provenance": collection,
            "uarch_hash": profile_hash,
            "uarch_features": uarch_vector(profile),
            "uarch_profile": profile,
            "resource_decoder": decoder.metadata(),
            "quality": {
                "status": "pass",
                "commit_ticks_monotonic": True,
                "common_time_grid": True,
                "synchronous_roi_start": True,
                "contains_no_commit_states": (
                    sample_meta["no_commit_fraction_at_sample_period"] > 0
                ),
                "roi_atomic_uops": atomic_uops,
                "atomic_uops": atomic_uops,
                "ff_atomic_verified": bool(collection["ff_atomic_verified"]),
            },
        }
        dump_json(os.path.join(tmp_dir, "meta.json"), metadata)
        if os.path.exists(out_dir):
            os.replace(out_dir, backup_dir)
        try:
            os.replace(tmp_dir, out_dir)
        except Exception:
            if os.path.exists(backup_dir) and not os.path.exists(out_dir):
                os.replace(backup_dir, out_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if os.path.exists(backup_dir) and not os.path.exists(out_dir):
            os.replace(backup_dir, out_dir)
        raise
    return V29TraceArtifacts(
        out_dir=out_dir,
        meta_path=os.path.join(out_dir, "meta.json"),
        trace_id=trace_id,
        n_cores=len(core_meta),
        n_uops=sum(int(item["n_uops"]) for item in core_meta),
        n_samples=n_samples,
    )


def build_functional_trace_cache(
    trace_dir: str,
    out_dir: str,
    *,
    K: int = 256,
    horizons: Iterable[float] = (16, 32, 64, 128, 256, 512, 1024),
    sample_period_cycles: float = 64.0,
    trace_id: Optional[str] = None,
    overwrite: bool = False,
) -> V29TraceArtifacts:
    """Encode a deployment cache without reading timing or PMU labels.

    Input files use the same per-core aligned-parquet naming and functional
    columns as training traces, but ``commit_tick`` and ``mispredicted`` may be
    absent.  A matching config.ini is still required because physical resource
    relations must use the exact target uarch decoder.
    """
    _require_numpy()
    if int(K) != 256:
        raise ValueError("v29 contract fixes functional lookahead K=256")
    horizon_values = normalized_horizons(horizons)
    if not any(abs(value - float(sample_period_cycles)) <= 1.0e-6 for value in horizon_values):
        raise ValueError("v29 sample_period_cycles must be present in horizons")
    profile, decoder = load_trace_profile(trace_dir)
    aligned = iter_core_aligned_files(trace_dir)
    workload = os.path.basename(os.path.dirname(trace_dir.rstrip("/")))
    raw_root = os.path.basename(os.path.dirname(os.path.dirname(trace_dir.rstrip("/"))))
    profile_hash = uarch_hash(profile)
    trace_id = trace_id or f"functional/{raw_root}/{workload}/{profile_hash[:12]}"
    predictor = predictor_hash(profile)
    decoder_hash = decoder.provenance_hash()
    out_dir = os.path.abspath(out_dir)
    tmp_dir = out_dir + f".tmp-{os.getpid()}"
    backup_dir = out_dir + f".old-{os.getpid()}"
    if os.path.exists(out_dir) and not overwrite:
        raise FileExistsError(out_dir)
    shutil.rmtree(tmp_dir, ignore_errors=True)
    shutil.rmtree(backup_dir, ignore_errors=True)
    os.makedirs(os.path.join(tmp_dir, "cores"), exist_ok=True)
    try:
        core_meta = []
        for core_id, path in aligned:
            core_meta.append(_build_core(
                core_id=int(core_id),
                aligned_path=path,
                core_dir=os.path.join(tmp_dir, "cores", str(int(core_id))),
                profile=profile,
                decoder=decoder,
                roi_begin_tick=None,
                roi_end_tick=None,
                include_oracle=False,
            ))
        core_meta.sort(key=lambda item: int(item["core_id"]))
        atomic_uops = sum(int(item["n_atomics"]) for item in core_meta)
        if atomic_uops:
            raise RuntimeError(
                "v29 functional ROI ISA-atomic UOP contract requires zero "
                f"(independent of FFATOMIC fast-forward), got {atomic_uops}"
            )
        contracts = feature_contract_metadata(
            predictor_hash=predictor,
            resource_decoder_hash=decoder_hash,
            horizons=horizon_values,
            sample_period_cycles=sample_period_cycles,
        )
        metadata = {
            **contracts,
            "container_schema": FUNCTIONAL_CONTAINER_SCHEMA,
            "trace_id": trace_id,
            "workload": workload,
            "raw_root": raw_root,
            "trace_dir": os.path.abspath(trace_dir),
            "functional_source_schema": "v29-functional-aligned-parquet-1",
            "K": int(K),
            "tick_per_cycle": float(tick_per_cycle(profile)),
            "n_cores": len(core_meta),
            "core_ids": [int(item["core_id"]) for item in core_meta],
            "n_uops": sum(int(item["n_uops"]) for item in core_meta),
            "n_samples": 0,
            "cores": core_meta,
            "uarch_hash": profile_hash,
            "uarch_features": uarch_vector(profile),
            "uarch_profile": profile,
            "resource_decoder": decoder.metadata(),
            "quality": {
                "status": "pass",
                "functional_only": True,
                "contains_commit_tick": False,
                "contains_branch_miss_label": False,
                "roi_atomic_uops": atomic_uops,
                "atomic_uops": atomic_uops,
            },
        }
        dump_json(os.path.join(tmp_dir, "meta.json"), metadata)
        if os.path.exists(out_dir):
            os.replace(out_dir, backup_dir)
        try:
            os.replace(tmp_dir, out_dir)
        except Exception:
            if os.path.exists(backup_dir) and not os.path.exists(out_dir):
                os.replace(backup_dir, out_dir)
            raise
        shutil.rmtree(backup_dir, ignore_errors=True)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if os.path.exists(backup_dir) and not os.path.exists(out_dir):
            os.replace(backup_dir, out_dir)
        raise
    return V29TraceArtifacts(
        out_dir=out_dir,
        meta_path=os.path.join(out_dir, "meta.json"),
        trace_id=trace_id,
        n_cores=len(core_meta),
        n_uops=sum(int(item["n_uops"]) for item in core_meta),
        n_samples=0,
    )
