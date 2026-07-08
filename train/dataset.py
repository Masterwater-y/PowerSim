"""V26 structured dataset and tensor cache.

This module is intentionally v26-only.  It does not tokenize windows or load
historical token caches; it only accepts structured UOP rows from windows built
with the v26_14 schema.
"""
from __future__ import annotations

import bisect
import json
import os
from pathlib import Path
from typing import Dict, List

import torch
from torch.utils.data import Dataset

from model import tokenizer as tk
from model.regression_head import PMU_KEYS

MANIFEST_NAME = "manifest.pt"
TENSOR_CACHE_FORMAT = "tensor_v1"
INPUT_MODE_V26_STRUCTURED = "v26_structured"
DENOM_KEYS = [
    "branch_count",
    "loads",
    "stores",
    "atomics",
    "mem_ops",
    "page_touches",
]


def tensor_cache_path(jsonl_path: str, max_len: int) -> str:
    p = Path(jsonl_path)
    return str(p.with_name(f"{p.stem}.maxlen{max_len}.tensor_cache"))


def default_cache_path(jsonl_path: str, max_len: int) -> str:
    return tensor_cache_path(jsonl_path, max_len)


def _normalize_uop_row(row, field_count: int) -> List[int]:
    vals = [int(v) for v in list(row or [])[:field_count]]
    if len(vals) < field_count:
        vals.extend([0] * (field_count - len(vals)))
    return vals


def _remap_label(rec: dict,
                 wanted_keys: List[str] | None = None) -> List[List[float]] | None:
    label_keys = rec.get("label_keys") or PMU_KEYS
    wanted = list(wanted_keys or PMU_KEYS)
    labels = rec.get("label")
    if labels is None:
        return None
    try:
        idx = [label_keys.index(k) for k in wanted]
    except ValueError:
        return None
    return [[float(row[i]) for i in idx] for row in labels]


def _pad_side_feats(raw, n_core: int) -> List[List[float]]:
    width = len(tk.SIDE_FEATURE_KEYS)
    if raw is None:
        return [[0.0] * width for _ in range(n_core)]
    out: List[List[float]] = []
    for ci in range(n_core):
        row = list(raw[ci]) if ci < len(raw) else []
        vals = [float(x) for x in row[:width]]
        if len(vals) < width:
            vals.extend([0.0] * (width - len(vals)))
        out.append(vals)
    return out


def _denom_vecs(raw, n_core: int) -> List[List[float]]:
    out: List[List[float]] = []
    for ci in range(n_core):
        d = raw[ci] if raw is not None and ci < len(raw) else {}
        if isinstance(d, (list, tuple)):
            row = [float(x or 0.0) for x in d[:len(DENOM_KEYS)]]
            if len(row) < len(DENOM_KEYS):
                row.extend([0.0] * (len(DENOM_KEYS) - len(row)))
            out.append(row)
        else:
            out.append([
                float((d or {}).get(k, 0.0) or 0.0) for k in DENOM_KEYS
            ])
    return out


def build_cache_meta(jsonl_path: str, max_len: int,
                     max_cores: int = tk.MAX_CORES,
                     label_keys: List[str] | None = None) -> dict:
    path = os.path.realpath(jsonl_path)
    st = os.stat(path)
    return {
        "jsonl_path": path,
        "jsonl_size": int(st.st_size),
        "jsonl_mtime_ns": int(st.st_mtime_ns),
        "max_len": int(max_len),
        "max_cores": int(max_cores),
        "feat_version": 26,
        "input_mode": INPUT_MODE_V26_STRUCTURED,
        "uop_field_schema": "v26_14",
        "uop_field_count": int(tk.V26_UOP_FIELD_COUNT),
        "pmu_keys": list(label_keys or PMU_KEYS),
        "side_feat_dim": len(tk.SIDE_FEATURE_KEYS),
        "denom_keys": list(DENOM_KEYS),
    }


def build_v26_structured_samples_from_jsonl(
    jsonl_path: str,
    max_len: int = 32768,
    label_keys: List[str] | None = None,
    field_count: int = tk.V26_UOP_FIELD_COUNT,
) -> List[dict]:
    samples: List[dict] = []
    with open(jsonl_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            label = _remap_label(rec, label_keys)
            if label is None:
                continue
            nc = int(rec.get("n_core", 0) or 0)
            if nc <= 0:
                continue
            split_src = rec.get(
                "core_split",
                rec.get("uops_per_core", rec.get("instr_retired", [])),
            )
            core_split = [
                int(round(float(x))) for x in list(split_src)[:nc]
            ]
            if len(core_split) != nc or any(x < 0 for x in core_split):
                continue
            if sum(core_split) > max_len:
                continue

            raw_fields = rec.get("uop_fields")
            if raw_fields is None:
                continue
            raw_is_uop = rec.get("is_uop")
            if raw_is_uop is None:
                raw_is_uop = [
                    1 if t == "<UOP>" else 0 for t in rec.get("tokens", [])
                ]
            if len(raw_is_uop) != len(raw_fields):
                continue
            uop_fields = []
            short_row = False
            for row, flag in zip(raw_fields, raw_is_uop):
                if not bool(flag):
                    continue
                if len(row or []) < field_count:
                    short_row = True
                    break
                uop_fields.append(_normalize_uop_row(row, field_count))
            if short_row:
                continue
            if len(uop_fields) != sum(core_split):
                continue

            samples.append({
                "label": label,
                "n_core": nc,
                "core_split": core_split,
                "instr_retired": rec["instr_retired"],
                "uops": rec.get("uops_per_core", core_split),
                "t_start_rel": rec.get("t_start_rel", [0.0] * nc),
                "uop_fields": uop_fields,
                "side_feats": _pad_side_feats(rec.get("side_feats"), nc),
                "denoms": _denom_vecs(rec.get("denoms"), nc),
                "meta": {
                    "id": rec.get("id", ""),
                    "workload": rec.get("workload", ""),
                    "cfg_hash": rec.get("cfg_hash", ""),
                    "mode": rec.get("mode", ""),
                    "w_ops": rec.get("w_ops", 0),
                    "target_fill": rec.get("target_fill", 0.0),
                    "fill_ratio": rec.get("fill_ratio", 0.0),
                    "t_start_tick": rec.get("t_start_tick", 0),
                    "t_end_tick": rec.get("t_end_tick", 0),
                    "tq_span_tick": rec.get("tq_span_tick", 0),
                    "stride_tick": rec.get("stride_tick", 0),
                    "end_skew_cycle": rec.get("end_skew_cycle", 0.0),
                },
            })
    return samples


def build_cache_samples_from_jsonl(
    jsonl_path: str,
    max_len: int = 32768,
    label_keys: List[str] | None = None,
) -> List[dict]:
    return build_v26_structured_samples_from_jsonl(
        jsonl_path, max_len=max_len, label_keys=label_keys)


def build_tensor_cache_shard(samples: List[dict]) -> dict:
    n = len(samples)
    if n == 0:
        return {
            "format": TENSOR_CACHE_FORMAT,
            "count": 0,
            "max_n_core": 0,
            "uop_field_count": int(tk.V26_UOP_FIELD_COUNT),
        }

    side_dim = len(tk.SIDE_FEATURE_KEYS)
    denom_dim = len(DENOM_KEYS)
    field_count = int(tk.V26_UOP_FIELD_COUNT)
    max_nc = max(int(s["n_core"]) for s in samples)

    uop_offsets = [0]
    uop_fields_flat: list[list[int]] = []
    n_core = torch.zeros((n,), dtype=torch.int16)
    label_dim = max(
        len(row)
        for s in samples
        for row in s.get("label", [])
    )
    label = torch.zeros((n, max_nc, label_dim), dtype=torch.float32)
    instr = torch.ones((n, max_nc), dtype=torch.float32)
    uops = torch.ones((n, max_nc), dtype=torch.float32)
    core_split = torch.zeros((n, max_nc), dtype=torch.int32)
    t_start = torch.zeros((n, max_nc), dtype=torch.float32)
    side = torch.zeros((n, max_nc, side_dim), dtype=torch.float32)
    denoms = torch.zeros((n, max_nc, denom_dim), dtype=torch.float32)
    meta_workload: list[str] = []
    meta_id: list[str] = []
    meta_cfg_hash: list[str] = []
    meta_mode: list[str] = []
    meta_w_ops = torch.zeros((n,), dtype=torch.int32)
    meta_target_fill = torch.zeros((n,), dtype=torch.float32)
    meta_fill_ratio = torch.zeros((n,), dtype=torch.float32)
    meta_t_start_tick = torch.zeros((n,), dtype=torch.int64)
    meta_t_end_tick = torch.zeros((n,), dtype=torch.int64)
    meta_tq_span_tick = torch.zeros((n,), dtype=torch.int64)
    meta_stride_tick = torch.zeros((n,), dtype=torch.int64)
    meta_end_skew_cycle = torch.zeros((n,), dtype=torch.float32)

    for si, sample in enumerate(samples):
        nc = int(sample["n_core"])
        meta = sample.get("meta") or {}
        rows = [
            _normalize_uop_row(row, field_count)
            for row in sample.get("uop_fields", [])
        ]
        n_core[si] = nc
        uop_fields_flat.extend(rows)
        uop_offsets.append(len(uop_fields_flat))
        label[si, :nc] = torch.as_tensor(
            sample["label"][:nc], dtype=torch.float32)
        instr[si, :nc] = torch.as_tensor(
            sample["instr_retired"][:nc], dtype=torch.float32)
        uops[si, :nc] = torch.as_tensor(
            sample.get("uops", sample["instr_retired"])[:nc],
            dtype=torch.float32,
        )
        split = [
            int(round(float(x)))
            for x in sample["core_split"][:nc]
        ]
        core_split[si, :nc] = torch.as_tensor(split, dtype=torch.int32)
        t_start[si, :nc] = torch.as_tensor(
            sample.get("t_start_rel", [0.0] * nc)[:nc], dtype=torch.float32)
        side[si, :nc] = torch.as_tensor(
            _pad_side_feats(sample.get("side_feats"), nc), dtype=torch.float32)
        denoms[si, :nc] = torch.as_tensor(
            _denom_vecs(sample.get("denoms"), nc), dtype=torch.float32)

        meta_workload.append(str(meta.get("workload", "")))
        meta_id.append(str(meta.get("id", "")))
        meta_cfg_hash.append(str(meta.get("cfg_hash", "")))
        meta_mode.append(str(meta.get("mode", "")))
        meta_w_ops[si] = int(meta.get("w_ops", 0) or 0)
        meta_target_fill[si] = float(meta.get("target_fill", 0.0) or 0.0)
        meta_fill_ratio[si] = float(meta.get("fill_ratio", 0.0) or 0.0)
        meta_t_start_tick[si] = int(meta.get("t_start_tick", 0) or 0)
        meta_t_end_tick[si] = int(meta.get("t_end_tick", 0) or 0)
        meta_tq_span_tick[si] = int(meta.get("tq_span_tick", 0) or 0)
        meta_stride_tick[si] = int(meta.get("stride_tick", 0) or 0)
        meta_end_skew_cycle[si] = float(
            meta.get("end_skew_cycle", 0.0) or 0.0
        )

    uop_fields = torch.as_tensor(uop_fields_flat, dtype=torch.int16)
    return {
        "format": TENSOR_CACHE_FORMAT,
        "count": n,
        "max_n_core": max_nc,
        "uop_field_count": field_count,
        "uop_offsets": torch.as_tensor(uop_offsets, dtype=torch.int64),
        "uop_fields_flat": uop_fields,
        "n_core": n_core,
        "label": label,
        "instr_retired": instr,
        "uops": uops,
        "core_split": core_split,
        "t_start_rel": t_start,
        "side_feats": side,
        "denoms": denoms,
        "workload": meta_workload,
        "sample_id": meta_id,
        "cfg_hash": meta_cfg_hash,
        "mode": meta_mode,
        "w_ops": meta_w_ops,
        "target_fill": meta_target_fill,
        "fill_ratio": meta_fill_ratio,
        "t_start_tick": meta_t_start_tick,
        "t_end_tick": meta_t_end_tick,
        "tq_span_tick": meta_tq_span_tick,
        "stride_tick": meta_stride_tick,
        "end_skew_cycle": meta_end_skew_cycle,
    }


class WindowDataset(Dataset):
    def __init__(self, jsonl_path: str, max_len: int = 32768,
                 max_cores: int = tk.MAX_CORES,
                 cache_path: str | None = None,
                 require_cache: bool = True,
                 label_keys: List[str] | None = None):
        self.max_len = int(max_len)
        self.max_cores = int(max_cores)
        self.label_keys = list(label_keys or PMU_KEYS)
        self.jsonl_path = os.path.realpath(jsonl_path)
        self.cache_path = cache_path or default_cache_path(
            self.jsonl_path, self.max_len)
        self.shards: List[dict] = []
        self._cum_counts: List[int] = []
        self._loaded_shard_idx: int | None = None
        self._loaded_tensor_shard: dict | None = None
        self._cache_label_idx: List[int] | None = None
        self.total_samples = 0

        if not self._try_load_cache():
            if require_cache:
                raise FileNotFoundError(
                    f"v26 structured tensor cache missing or stale: "
                    f"{self.cache_path}"
                )
            samples = build_v26_structured_samples_from_jsonl(
                self.jsonl_path,
                max_len=self.max_len,
                label_keys=self.label_keys,
            )
            self._save_tensor_cache(samples)
            if not self._try_load_cache():
                raise RuntimeError(f"failed to load cache: {self.cache_path}")

    @staticmethod
    def default_cache_path(jsonl_path: str, max_len: int) -> str:
        return default_cache_path(jsonl_path, max_len)

    @staticmethod
    def tensor_cache_path(jsonl_path: str, max_len: int) -> str:
        return tensor_cache_path(jsonl_path, max_len)

    def _cache_meta(self) -> dict:
        return build_cache_meta(
            self.jsonl_path,
            self.max_len,
            max_cores=self.max_cores,
            label_keys=self.label_keys,
        )

    def _cache_meta_matches(self, cached_meta: dict | None):
        if not isinstance(cached_meta, dict):
            return False, None
        cur = self._cache_meta()
        keys = [
            "jsonl_path",
            "jsonl_size",
            "jsonl_mtime_ns",
            "max_len",
            "max_cores",
            "side_feat_dim",
            "input_mode",
            "uop_field_schema",
            "uop_field_count",
        ]
        for key in keys:
            if cached_meta.get(key) != cur.get(key):
                return False, None
        wanted = list(self.label_keys)
        cached = list(cached_meta.get("pmu_keys") or PMU_KEYS)
        if cached == wanted:
            return True, None
        if all(k in cached for k in wanted):
            return True, [cached.index(k) for k in wanted]
        return False, None

    def _try_load_cache(self) -> bool:
        cache_dir = Path(self.cache_path)
        manifest_path = cache_dir / MANIFEST_NAME
        if not manifest_path.exists():
            return False
        try:
            manifest = torch.load(manifest_path, map_location="cpu")
        except Exception:
            return False
        if not isinstance(manifest, dict):
            return False
        if manifest.get("format") != TENSOR_CACHE_FORMAT:
            return False
        ok, label_idx = self._cache_meta_matches(manifest.get("meta"))
        if not ok:
            return False
        shards = manifest.get("shards", [])
        if not shards:
            return False
        self._cache_label_idx = label_idx
        self.shards = []
        self._cum_counts = []
        total = 0
        for shard in shards:
            path = cache_dir / shard["file"]
            count = int(shard["count"])
            if not path.exists():
                return False
            total += count
            self._cum_counts.append(total)
            self.shards.append({"path": str(path), "count": count})
        self.total_samples = total
        return total > 0

    def _save_tensor_cache(self, samples: List[dict]) -> None:
        cache_dir = Path(self.cache_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        shard_name = "shard-00000.pt"
        _atomic_torch_save(
            build_tensor_cache_shard(samples),
            cache_dir / shard_name,
        )
        _atomic_torch_save({
            "format": TENSOR_CACHE_FORMAT,
            "meta": self._cache_meta(),
            "total_samples": len(samples),
            "shards": [{"file": shard_name, "count": len(samples)}],
        }, cache_dir / MANIFEST_NAME)

    def _ensure_shard_loaded(self, shard_idx: int) -> None:
        if self._loaded_shard_idx == shard_idx:
            return
        self._loaded_tensor_shard = torch.load(
            self.shards[shard_idx]["path"], map_location="cpu")
        self._loaded_shard_idx = shard_idx

    def _select_label_columns(self, label):
        if self._cache_label_idx is None:
            return label
        idx = self._cache_label_idx
        if torch.is_tensor(label):
            return label[:, idx]
        return [[row[i] for i in idx] for row in label]

    def _tensor_sample(self, shard: dict, local_idx: int) -> dict:
        off_key = "uop_offsets"
        if off_key not in shard:
            raise RuntimeError(
                "stale tensor cache: missing uop_offsets. Rebuild with the "
                "current scripts/prepare_dataset_cache.py."
            )
        u0 = int(shard[off_key][local_idx])
        u1 = int(shard[off_key][local_idx + 1])
        nc = int(shard["n_core"][local_idx])
        return {
            "label": self._select_label_columns(
                shard["label"][local_idx, :nc]),
            "n_core": nc,
            "instr_retired": shard["instr_retired"][local_idx, :nc],
            "uops": shard["uops"][local_idx, :nc],
            "core_split": shard["core_split"][local_idx, :nc],
            "t_start_rel": shard["t_start_rel"][local_idx, :nc],
            "uop_fields": shard["uop_fields_flat"][u0:u1],
            "side_feats": shard["side_feats"][local_idx, :nc],
            "denoms": shard["denoms"][local_idx, :nc],
        }

    def __len__(self) -> int:
        return self.total_samples

    def __getitem__(self, i: int) -> dict:
        shard_idx = bisect.bisect_right(self._cum_counts, i)
        shard_start = 0 if shard_idx == 0 else self._cum_counts[shard_idx - 1]
        self._ensure_shard_loaded(shard_idx)
        assert self._loaded_tensor_shard is not None
        return self._tensor_sample(self._loaded_tensor_shard, i - shard_start)


def prepare_dataset_cache(jsonl_path: str, max_len: int = 32768,
                          max_cores: int = tk.MAX_CORES,
                          cache_path: str | None = None,
                          label_keys: List[str] | None = None) -> str:
    ds = WindowDataset(
        jsonl_path,
        max_len=max_len,
        max_cores=max_cores,
        cache_path=cache_path,
        require_cache=False,
        label_keys=label_keys,
    )
    return ds.cache_path


def _global_feats_from_side(side_feats: torch.Tensor,
                            core_mask: torch.Tensor) -> torch.Tensor:
    keys = {k: i for i, k in enumerate(tk.SIDE_FEATURE_KEYS)}

    def get(name: str) -> torch.Tensor:
        idx = keys.get(name)
        if idx is None:
            return side_feats.new_zeros(side_feats.shape[:2])
        return side_feats[..., idx]

    cm = core_mask.to(side_feats.dtype)
    active = cm.sum(dim=1).clamp(min=1.0)

    def masked_mean(name: str) -> torch.Tensor:
        x = get(name)
        return (x * cm).sum(dim=1) / active

    def masked_max(name: str) -> torch.Tensor:
        x = get(name).masked_fill(~core_mask.to(torch.bool), 0.0)
        return x.max(dim=1).values

    return torch.stack([
        masked_max("log1p_active_cores"),
        masked_max("log1p_uops_window_total"),
        masked_max("log1p_global_distinct_data_lines"),
        masked_max("log1p_global_distinct_data_pages"),
        masked_mean("shared_store_rate"),
        masked_mean("multi_writer_line_frac"),
        masked_mean("pairwise_writer_pressure"),
        masked_mean("store_owner_switch_rate"),
        masked_mean("inval_fanout_proxy_mean"),
        masked_mean("aggregate_load_density"),
        masked_mean("aggregate_mem_density"),
        masked_mean("global_large_stride_rate"),
        masked_mean("random_access_pressure"),
    ], dim=-1)


def make_collate_v26_structured(
    field_count: int = tk.V26_UOP_FIELD_COUNT,
):
    field_count = int(field_count)

    def require_row(row) -> List[int]:
        if len(row or []) < field_count:
            raise ValueError(
                f"v26 collate requires {field_count}-field UOP rows, "
                f"got {len(row or [])}. Rebuild windows/cache."
            )
        return _normalize_uop_row(row, field_count)

    def core_lengths(item: dict) -> list[int]:
        nc = int(item["n_core"])
        raw = item.get("core_split", [])
        if torch.is_tensor(raw):
            raw = raw.detach().cpu().tolist()
        out = [
            int(round(float(x)))
            for x in list(raw)[:nc]
        ]
        if len(out) != nc:
            raise ValueError(f"expected {nc} core_split entries, got {len(out)}")
        return out

    def uop_rows(item: dict) -> torch.Tensor:
        rows = item.get("uop_fields", [])
        if torch.is_tensor(rows):
            if rows.shape[-1] < field_count:
                raise ValueError(
                    f"v26 collate requires {field_count}-field UOP rows, "
                    f"got {rows.shape[-1]}. Rebuild windows/cache."
                )
            return rows[:, :field_count].to(dtype=torch.int16)
        return torch.as_tensor(
            [require_row(row) for row in rows],
            dtype=torch.int16,
        )

    def label_dim_of(item: dict) -> int:
        label = item["label"]
        if torch.is_tensor(label):
            return int(label.shape[-1])
        return max(len(row) for row in label)

    def collate(batch: List[dict]) -> Dict[str, torch.Tensor]:
        B = len(batch)
        max_nc = max(int(b["n_core"]) for b in batch)
        label_dim = max(label_dim_of(b) for b in batch)
        per_core_fields: list[list[torch.Tensor]] = []
        max_uops = 1

        for item in batch:
            lengths = core_lengths(item)
            rows = uop_rows(item)
            if sum(lengths) != int(rows.shape[0]):
                raise ValueError(
                    "cannot split UOP rows: "
                    f"sum(core_split)={sum(lengths)} rows={int(rows.shape[0])}"
                )
            cursor = 0
            core_rows = []
            for n in lengths:
                part = rows[cursor:cursor + n]
                cursor += n
                if int(part.shape[0]) == 0:
                    part = torch.zeros((1, field_count), dtype=torch.int16)
                max_uops = max(max_uops, int(part.shape[0]))
                core_rows.append(part)
            per_core_fields.append(core_rows)

        uop_fields = torch.zeros(
            (B, max_nc, max_uops, field_count), dtype=torch.int16)
        uop_mask = torch.zeros((B, max_nc, max_uops), dtype=torch.bool)
        label = torch.zeros((B, max_nc, label_dim), dtype=torch.float32)
        core_mask = torch.zeros((B, max_nc), dtype=torch.float32)
        uops = torch.ones((B, max_nc), dtype=torch.float32)
        instr = torch.ones((B, max_nc), dtype=torch.float32)
        side = torch.zeros(
            (B, max_nc, len(tk.SIDE_FEATURE_KEYS)), dtype=torch.float32)
        denoms = torch.zeros((B, max_nc, len(DENOM_KEYS)), dtype=torch.float32)

        for bi, item in enumerate(batch):
            nc = int(item["n_core"])
            for ci in range(nc):
                rows = per_core_fields[bi][ci]
                n = int(rows.shape[0])
                uop_fields[bi, ci, :n] = rows
                uop_mask[bi, ci, :n] = True
                label[bi, ci] = torch.as_tensor(
                    item["label"][ci], dtype=torch.float32)
                core_mask[bi, ci] = 1.0
                uops[bi, ci] = float(item["uops"][ci])
                instr[bi, ci] = float(item["instr_retired"][ci])
                side[bi, ci] = torch.as_tensor(
                    item["side_feats"][ci], dtype=torch.float32)
                denoms[bi, ci] = torch.as_tensor(
                    item["denoms"][ci], dtype=torch.float32)

        return {
            "uop_fields": uop_fields,
            "uop_mask": uop_mask,
            "core_mask": core_mask,
            "side_feats": side,
            "global_feats": _global_feats_from_side(side, core_mask),
            "label": label,
            "uops": uops,
            "instr_retired": instr,
            "denoms": denoms,
        }

    return collate


def _atomic_torch_save(obj: object, path: Path) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    torch.save(obj, tmp)
    os.replace(tmp, path)
