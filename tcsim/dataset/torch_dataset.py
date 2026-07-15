"""Torch datasets for oracle-context, functional-only fixed-chunk training."""
from __future__ import annotations

import json
import hashlib
import math
import os
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ..chunker.functional_features import (
    CHUNK_SUMMARY_NAMES,
    FIELD_INDEX,
    RELATION_FEATURE_NAMES,
    UARCH_FEATURE_NAMES,
)
from ..utils.io import load_json
from ..utils.parquet import read_table

try:
    import numpy as np
except Exception:
    np = None  # type: ignore

try:
    import torch
    from torch.utils.data import Dataset
    _HAS_TORCH = True
except Exception:
    torch = None  # type: ignore
    Dataset = object  # type: ignore
    _HAS_TORCH = False


def _resolve_path(base: str) -> Optional[str]:
    if os.path.exists(base):
        return base
    if os.path.exists(base + ".jsonl"):
        return base + ".jsonl"
    return None


def discover_rollout_dirs(root: str) -> List[str]:
    """Recursively find complete per-trace rollout directories."""
    out: List[str] = []
    for current, _dirs, files in os.walk(root):
        if "meta.json" in files and "rollout.jsonl" in files:
            out.append(current)
    return sorted(out)


def _keep_sample_in_partition(trace_id: str, step: int, policy: Optional[Dict[str, Any]]) -> bool:
    """Return whether a complete oracle-context sample belongs to a split.

    A row in ``rollout.jsonl`` is the indivisible cross-core model sample.  A
    deterministic hash lets the seed0 cache provide a development split while
    keeping seed1 completely outside training-time model selection.
    """
    if not policy:
        return True
    partition = str(policy.get("partition", "")).lower()
    if partition not in {"train", "validation"}:
        raise ValueError(f"unsupported sample partition {partition!r}")
    percent = int(policy.get("validation_percent", 10))
    if not 0 < percent < 100:
        raise ValueError("sample validation_percent must be in (0, 100)")
    seed = int(policy.get("seed", 20260714))
    key = f"{seed}:{trace_id}:{int(step)}".encode("utf-8")
    bucket = int.from_bytes(hashlib.sha256(key).digest()[:8], "big") % 100
    is_validation = bucket < percent
    return is_validation if partition == "validation" else not is_validation


def context_features(chunks: List[dict]) -> Tuple[List[List[List[int]]], List[List[float]]]:
    """Decorate chunks with current-context functional relations.

    Raw line keys are used only to test equality.  They are never returned to
    the model.  This preserves same-line coherence signal without exposing an
    absolute-address identity shortcut.
    """
    reads = [set(int(x) for x in ch.get("read_lines", [])) for ch in chunks]
    writes = [set(int(x) for x in ch.get("write_lines", [])) for ch in chunks]
    accesses = [reads[i] | writes[i] for i in range(len(chunks))]
    n_active = len(chunks)
    global_lines = set().union(*accesses) if accesses else set()
    total_uops = sum(int(ch.get("n_uops", 0) or 0) for ch in chunks)
    total_mem = sum(
        int(ch.get("n_load", 0) or 0)
        + int(ch.get("n_store", 0) or 0)
        + int(ch.get("n_atomic", 0) or 0)
        for ch in chunks
    )
    line_readers: Dict[int, set] = {}
    line_writers: Dict[int, set] = {}
    line_accessors: Dict[int, set] = {}
    for i in range(n_active):
        for line in reads[i]:
            line_readers.setdefault(line, set()).add(i)
            line_accessors.setdefault(line, set()).add(i)
        for line in writes[i]:
            line_writers.setdefault(line, set()).add(i)
            line_accessors.setdefault(line, set()).add(i)

    decorated: List[List[List[int]]] = []
    relations: List[List[float]] = []
    for i in range(n_active):
        other_access = set().union(*(accesses[j] for j in range(n_active) if j != i))
        other_writes = set().union(*(writes[j] for j in range(n_active) if j != i))
        denom_access = max(1, len(accesses[i]))
        denom_read = max(1, len(reads[i]))
        denom_write = max(1, len(writes[i]))
        own_shared_reads = [line for line in reads[i] if len(line_accessors.get(line, ())) > 1]
        own_shared_writes = [line for line in writes[i] if len(line_accessors.get(line, ())) > 1]
        reader_fanout = [len(line_readers.get(line, set()) - {i}) for line in accesses[i]]
        writer_fanout = [len(line_writers.get(line, set()) - {i}) for line in accesses[i]]
        accessor_fanout = [len(line_accessors.get(line, set()) - {i}) for line in accesses[i]]
        max_writer_cores = max(
            [len(line_writers.get(line, set())) for line in writes[i]] or [0]
        )
        fanout_den = max(1, n_active - 1)
        relations.append([
            math.log1p(n_active) / 4.0,
            len(accesses[i] & other_access) / denom_access,
            len(reads[i] & other_writes) / denom_read,
            len(writes[i] & other_access) / denom_write,
            len(writes[i] & other_writes) / denom_write,
            len(own_shared_reads) / denom_read,
            len(own_shared_writes) / denom_write,
            (sum(reader_fanout) / max(1, len(reader_fanout))) / fanout_den,
            (sum(writer_fanout) / max(1, len(writer_fanout))) / fanout_den,
            max(accessor_fanout or [0]) / fanout_den,
            max_writer_cores / max(1, n_active),
            math.log1p(1000.0 * len(global_lines) / max(1, total_uops)) / 8.0,
            len(accesses[i]) / max(1, len(global_lines)),
            total_mem / max(1, total_uops),
        ])

        fields = [list(x) for x in chunks[i].get("per_uop_fields", [])]
        lines = [int(x) for x in chunks[i].get("per_uop_lines", [])]
        access_kinds = [int(x) for x in chunks[i].get("per_uop_access", [])]
        if not fields or len(lines) != len(fields) or len(access_kinds) != len(fields):
            raise RuntimeError(
                "stale chunk cache without per-UOP functional line/access data; rebuild rollout"
            )
        for row, line, access_kind in zip(fields, lines, access_kinds):
            if line < 0 or access_kind <= 0:
                continue
            other_readers = line_readers.get(line, set()) - {i}
            other_writers = line_writers.get(line, set()) - {i}
            other_cores = line_accessors.get(line, set()) - {i}
            if access_kind == 1:
                role = 4 if other_writers else 2 if other_readers else 1
            else:
                role = 6 if other_writers else 5 if other_readers else 3
            fanout = 1 + min(6, int(math.ceil(math.log2(len(other_cores) + 1))))
            row[FIELD_INDEX["xcore_role"]] = role
            row[FIELD_INDEX["xcore_fanout"]] = fanout
        decorated.append(fields)
    return decorated, relations


# Backward-compatible alias.  The deployment runner imports the public helper
# so training and free-running inference share exactly one relation builder.
_context_features = context_features


def _functional_group_ids(
    fields: List[List[List[int]]],
    masks: List[List[int]],
    summaries: List[List[float]],
    relations: List[List[float]],
    n_uops: List[float],
) -> List[int]:
    """Group cores that are not causally identifiable from model inputs.

    Local PC/line ordinals are deliberately ignored here: they are useful for
    repeated-pattern encoding but must not authorize a loss to assign random
    slow/fast identities to otherwise symmetric cores.
    """
    ignored = {FIELD_INDEX["local_pc_id"], FIELD_INDEX["local_line_id"]}
    group_of: Dict[str, int] = {}
    out: List[int] = []
    for per_uop, mask, summary, relation, nu in zip(
        fields, masks, summaries, relations, n_uops,
    ):
        visible = [
            tuple(value for j, value in enumerate(row) if j not in ignored)
            for row, valid in zip(per_uop, mask) if valid
        ]
        payload = repr((
            visible,
            tuple(round(float(x), 5) for x in summary),
            tuple(round(float(x), 5) for x in relation),
            int(nu),
        )).encode("utf-8")
        signature = hashlib.sha1(payload).hexdigest()
        if signature not in group_of:
            group_of[signature] = len(group_of)
        out.append(group_of[signature])
    return out


class TCSimSampleDataset(Dataset):
    """One item is one oracle-selected cross-core functional context.

    The serialized ``audit_T_pred/audit_E_pred/audit_delta_hat`` fields are
    intentionally ignored.  Absolute labels are enabled only on a chunk's
    first exposure; context labels carry inverse-exposure weights for centered
    and listwise losses.
    """

    def __init__(self, out_dirs: List[Any]):
        if not _HAS_TORCH:
            raise RuntimeError("torch is required to use TCSimSampleDataset")
        self.chunks_by_key: Dict[Tuple[str, int, int], dict] = {}
        self.labels_by_key: Dict[Tuple[str, int, int], dict] = {}
        self.trace_meta: Dict[str, dict] = {}
        self.packed_by_trace: Dict[str, dict] = {}
        self.commit_index_of: Dict[Tuple[str, int, int], int] = {}
        self._flat: List[dict] = []
        for source in out_dirs:
            if isinstance(source, str):
                out_dir, sample_split = source, None
            elif isinstance(source, dict):
                out_dir = str(source.get("rollout_dir", ""))
                sample_split = source.get("sample_split")
                if not out_dir:
                    raise ValueError("rollout source is missing rollout_dir")
            else:
                raise TypeError(f"unsupported rollout source {type(source)!r}")
            self._load_one(out_dir, sample_split=sample_split)
        self.sample_trace_ids = [str(row["trace_id"]) for row in self._flat]
        self.trace_sample_counts: Counter = Counter(self.sample_trace_ids)
        self.occurrence_count: Counter = Counter()
        for row in self._flat:
            tid = row["trace_id"]
            for rec in row["core_records"]:
                self.occurrence_count[(tid, int(rec["core_id"]), int(rec["chunk_id"]))] += 1

    def _load_one(self, out_dir: str, sample_split: Optional[Dict[str, Any]] = None) -> None:
        meta_path = os.path.join(out_dir, "meta.json")
        if not os.path.exists(meta_path):
            raise FileNotFoundError(meta_path)
        meta = load_json(meta_path)
        trace_id = meta["trace_id"]
        if trace_id in self.trace_meta:
            raise ValueError(f"duplicate trace_id across rollout dirs: {trace_id}")
        self.trace_meta[trace_id] = meta
        packed_meta = meta.get("packed")
        if packed_meta:
            if np is None:
                raise RuntimeError("numpy is required to read packed rollout caches")
            packed_dir = os.path.join(out_dir, packed_meta.get("relative_dir", "packed"))
            self.packed_by_trace[trace_id] = {
                "meta": packed_meta,
                "fields": np.load(os.path.join(packed_dir, "fields.npy"), mmap_mode="r"),
                "mask": np.load(os.path.join(packed_dir, "mask.npy"), mmap_mode="r"),
                "summary": np.load(os.path.join(packed_dir, "summary.npy"), mmap_mode="r"),
                "lines": np.load(os.path.join(packed_dir, "lines.npy"), mmap_mode="r"),
                "access": np.load(os.path.join(packed_dir, "access.npy"), mmap_mode="r"),
                "scalar": np.load(os.path.join(packed_dir, "scalar.npy"), mmap_mode="r"),
            }
            if int(self.packed_by_trace[trace_id]["scalar"].shape[1]) < 14:
                raise RuntimeError(
                    "stale packed cache without branch-miss auxiliary labels; rebuild rollout"
                )
        else:
            chunks_path = _resolve_path(os.path.join(out_dir, "chunks.parquet"))
            labels_path = _resolve_path(os.path.join(out_dir, "labels.parquet"))
            if chunks_path is None or labels_path is None:
                raise FileNotFoundError(f"missing chunks/labels under {out_dir}")
            for row in read_table(chunks_path):
                key = (row["trace_id"], int(row["core_id"]), int(row["chunk_id"]))
                if key in self.chunks_by_key:
                    raise ValueError(f"duplicate chunk key: {key}")
                self.chunks_by_key[key] = row
            for row in read_table(labels_path):
                key = (row["trace_id"], int(row["core_id"]), int(row["chunk_id"]))
                self.labels_by_key[key] = row
        rollout_path = os.path.join(out_dir, "rollout.jsonl")

        per_core_ci: Dict[int, int] = {}
        with open(rollout_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row["trace_id"] != trace_id:
                    raise ValueError(f"rollout/meta trace mismatch in {out_dir}")
                if not _keep_sample_in_partition(trace_id, int(row["step"]), sample_split):
                    continue
                fast = set(int(x) for x in row["fast_cores"])
                for rec in row["core_records"]:
                    key = (trace_id, int(rec["core_id"]), int(rec["chunk_id"]))
                    self.commit_index_of.setdefault(
                        key, per_core_ci.get(int(rec["core_id"]), 0)
                    )
                for rec in row["core_records"]:
                    core = int(rec["core_id"])
                    if core in fast:
                        per_core_ci[core] = per_core_ci.get(core, 0) + 1
                self._flat.append(row)

    def _chunk_and_label(self, key: Tuple[str, int, int]) -> Tuple[Optional[dict], Optional[dict]]:
        trace_id, core_id, chunk_id = key
        packed = self.packed_by_trace.get(trace_id)
        if packed is None:
            return self.chunks_by_key.get(key), self.labels_by_key.get(key)
        pmeta = packed["meta"]
        core_key = str(int(core_id))
        if core_key not in pmeta["core_offsets"]:
            return None, None
        if not 0 <= int(chunk_id) < int(pmeta["core_counts"][core_key]):
            return None, None
        index = int(pmeta["core_offsets"][core_key]) + int(chunk_id)
        scalar = packed["scalar"][index]
        new_branch_contract = len(scalar) >= 15
        branch_opportunities = int(scalar[12])
        n_cond_branch = int(scalar[13]) if new_branch_contract else int(scalar[12])
        n_branch_miss = int(scalar[14]) if new_branch_contract else int(scalar[13])
        n_uops = int(scalar[0])
        valid_mask = packed["mask"][index].astype("uint8", copy=False).tolist()
        per_lines = packed["lines"][index].astype("int64", copy=False).tolist()
        per_access = packed["access"][index].astype("uint8", copy=False).tolist()
        read_lines = sorted({
            int(line) for line, kind, valid in zip(per_lines, per_access, valid_mask)
            if valid and line >= 0 and kind in (1, 3)
        })
        write_lines = sorted({
            int(line) for line, kind, valid in zip(per_lines, per_access, valid_mask)
            if valid and line >= 0 and kind in (2, 3)
        })
        trace_meta = self.trace_meta[trace_id]
        chunk = {
            "trace_id": trace_id,
            "core_id": int(core_id),
            "chunk_id": int(chunk_id),
            "n_uops": n_uops,
            "n_load": int(scalar[1]),
            "n_store": int(scalar[2]),
            "n_atomic": int(scalar[3]),
            "n_branch": int(scalar[4]),
            "n_branch_opportunities": branch_opportunities,
            "n_cond_branch": n_cond_branch,
            "n_branch_miss": n_branch_miss,
            "n_int": int(scalar[5]),
            "n_fp": int(scalar[6]),
            "n_simd": int(scalar[7]),
            "n_serialize": int(scalar[8]),
            "per_uop_fields": packed["fields"][index].astype("int64", copy=False).tolist(),
            "valid_uop_mask": valid_mask,
            "chunk_summary": packed["summary"][index].astype("float32", copy=False).tolist(),
            "per_uop_lines": per_lines,
            "per_uop_access": per_access,
            "read_lines": read_lines,
            "write_lines": write_lines,
            "uarch_features": list(trace_meta.get("uarch_features", [])),
        }
        valid_label = bool(scalar[11] > 0.5)
        label = {
            "delta_cycles": float(scalar[9]) if valid_label else None,
            "cpi": float(scalar[10]) if valid_label else None,
            "valid_label": valid_label,
        }
        return chunk, label

    def __len__(self) -> int:
        return len(self._flat)

    def __getitem__(self, idx: int) -> Dict[str, Any]:
        row = self._flat[idx]
        trace_id = row["trace_id"]
        entries: List[Tuple[dict, dict, Optional[dict], tuple]] = []
        for rec in row["core_records"]:
            key = (trace_id, int(rec["core_id"]), int(rec["chunk_id"]))
            ch, label = self._chunk_and_label(key)
            if ch is not None:
                entries.append((rec, ch, label, key))
        if not entries:
            raise RuntimeError("empty oracle-context sample")

        context_chunks = [x[1] for x in entries]
        per_fields, relation = _context_features(context_chunks)
        valid_masks: List[List[int]] = []
        summaries: List[List[float]] = []
        uarch_features: List[List[float]] = []
        core_ids: List[int] = []
        n_uops: List[float] = []
        resident: List[float] = []
        context_only: List[float] = []
        exposure: List[float] = []
        delta_true: List[float] = []
        log_cpi: List[float] = []
        abs_mask: List[float] = []
        context_mask: List[float] = []
        context_weight: List[float] = []
        branch_opportunities: List[float] = []
        branch_misses: List[float] = []
        branch_label_mask: List[float] = []
        chunk_ids: List[int] = []
        commit_indices: List[int] = []

        for entry_idx, (rec, ch, label, key) in enumerate(entries):
            fields = per_fields[entry_idx]
            valid = ch.get("valid_uop_mask")
            if not fields or not valid:
                raise RuntimeError(
                    "stale v27.1 chunk cache without functional fields; rebuild rollout"
                )
            valid_masks.append([int(x) for x in valid])
            summaries.append([float(x) for x in ch.get("chunk_summary", [])])
            uarch_features.append([float(x) for x in ch.get("uarch_features", [])])
            core_ids.append(int(rec["core_id"]))
            n_uops.append(float(ch["n_uops"]))
            resident.append(float(bool(rec.get("resident", False))))
            context_only.append(float(bool(rec.get("context_only", False))))
            exposure.append(float(rec.get("exposure", 0)))
            chunk_ids.append(int(rec["chunk_id"]))
            commit_indices.append(int(self.commit_index_of.get(key, 0)))
            opportunities = int(ch.get(
                "n_branch_opportunities",
                ch.get("n_branch", ch.get("n_cond_branch", 0)),
            ))
            branch_opportunities.append(float(opportunities))
            branch_misses.append(float(ch.get("n_branch_miss", 0)))

            valid_label = bool(
                label
                and label.get("delta_cycles") is not None
                and label.get("valid_label", True)
            )
            if valid_label:
                dc = float(label["delta_cycles"])
                cpi = float(label.get("cpi") or dc / max(1.0, float(ch["n_uops"])))
                delta_true.append(dc)
                log_cpi.append(math.log(max(1e-8, cpi)))
                first = bool(rec.get("first_exposure", int(rec.get("exposure", 0)) == 0))
                abs_mask.append(float(first))
                context_mask.append(1.0)
                context_weight.append(1.0 / max(1, int(self.occurrence_count[key])))
            else:
                delta_true.append(0.0)
                log_cpi.append(0.0)
                abs_mask.append(0.0)
                context_mask.append(0.0)
                context_weight.append(0.0)
            branch_label_mask.append(float(
                valid_label
                and bool(rec.get("first_exposure", int(rec.get("exposure", 0)) == 0))
                and opportunities > 0
            ))

        functional_group_ids = _functional_group_ids(
            per_fields, valid_masks, summaries, relation, n_uops,
        )
        t = torch
        return {
            "per_uop_fields": t.tensor(per_fields, dtype=t.long),
            "valid_uop_mask": t.tensor(valid_masks, dtype=t.bool),
            "chunk_summary": t.tensor(summaries, dtype=t.float32),
            "relation_features": t.tensor(relation, dtype=t.float32),
            "uarch_features": t.tensor(uarch_features, dtype=t.float32),
            "core_ids": t.tensor(core_ids, dtype=t.long),
            "n_uops": t.tensor(n_uops, dtype=t.float32),
            # Scheduler metadata is returned for diagnostics only; the model
            # forward signature does not consume it.
            "resident": t.tensor(resident, dtype=t.float32),
            "context_only": t.tensor(context_only, dtype=t.float32),
            "exposure": t.tensor(exposure, dtype=t.float32),
            "delta_cycles": t.tensor(delta_true, dtype=t.float32),
            "log_cpi": t.tensor(log_cpi, dtype=t.float32),
            "label_mask": t.tensor(abs_mask, dtype=t.float32),
            "context_label_mask": t.tensor(context_mask, dtype=t.float32),
            "context_weight": t.tensor(context_weight, dtype=t.float32),
            "branch_opportunities": t.tensor(branch_opportunities, dtype=t.float32),
            "branch_misses": t.tensor(branch_misses, dtype=t.float32),
            "branch_label_mask": t.tensor(branch_label_mask, dtype=t.float32),
            "functional_group_id": t.tensor(functional_group_ids, dtype=t.long),
            "chunk_ids": t.tensor(chunk_ids, dtype=t.long),
            "commit_index": t.tensor(commit_indices, dtype=t.long),
            "trace_id": trace_id,
            "step": int(row["step"]),
        }


def collate_variable_active(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    if torch is None:
        raise RuntimeError("torch is required")
    tensor_keys = [
        "per_uop_fields", "valid_uop_mask", "chunk_summary",
        "relation_features", "uarch_features", "core_ids", "n_uops",
        "resident", "context_only", "exposure", "delta_cycles", "log_cpi",
        "label_mask", "context_label_mask", "context_weight",
        "branch_opportunities", "branch_misses", "branch_label_mask",
        "functional_group_id", "chunk_ids", "commit_index",
    ]
    out: Dict[str, List[Any]] = {k: [] for k in tensor_keys}
    ptr = [0]
    trace_ids: List[str] = []
    steps: List[int] = []
    for sample in samples:
        for key in tensor_keys:
            out[key].append(sample[key])
        ptr.append(ptr[-1] + int(sample["core_ids"].shape[0]))
        trace_ids.append(sample["trace_id"])
        steps.append(int(sample["step"]))
    result: Dict[str, Any] = {
        key: torch.cat(values, dim=0) for key, values in out.items()
    }
    result["sample_ptr"] = torch.tensor(ptr, dtype=torch.long)
    result["trace_id"] = trace_ids
    result["step"] = torch.tensor(steps, dtype=torch.long)
    return result
