"""dataset.py — windows.jsonl / sharded ids cache -> 训练 batch。

长期推荐格式：
  windows.maxlen{N}.ids_cache/
    manifest.pt
    shard-00000.pt
    shard-00001.pt
    ...

训练阶段直接读取 shard cache，而不是再从 windows.jsonl 现算 ids/qpos。
"""
from __future__ import annotations

import bisect
import json
import os
from pathlib import Path
from typing import Dict, List

import torch
from torch.utils.data import Dataset

from model.regression_head import K
from model.regression_head import PMU_KEYS
from model import tokenizer as tk

MANIFEST_NAME = "manifest.pt"
DENOM_KEYS = [
    "branch_count",
    "loads",
    "stores",
    "atomics",
    "mem_ops",
    "page_touches",
]


def default_cache_path(jsonl_path: str, max_len: int) -> str:
    p = Path(jsonl_path)
    return str(p.with_name(f"{p.stem}.maxlen{max_len}.ids_cache"))


def build_cache_meta(jsonl_path: str, hf_tokenizer, max_len: int,
                     max_cores: int) -> dict:
    st = os.stat(jsonl_path)
    return {
        "jsonl_path": os.path.abspath(jsonl_path),
        "jsonl_size": int(st.st_size),
        "jsonl_mtime_ns": int(st.st_mtime_ns),
        "max_len": int(max_len),
        "tokenizer_len": int(len(hf_tokenizer)),
        "unk_token_id": int(
            -1 if hf_tokenizer.unk_token_id is None else hf_tokenizer.unk_token_id
        ),
        "max_cores": int(max_cores),
        "feat_version": 9,  # v9: composite uop + side tensor + L2 PMU schema
        "pmu_keys": list(PMU_KEYS),
        "side_feat_dim": len(tk.SIDE_FEATURE_KEYS),
    }


def _remap_label(rec: dict) -> List[List[float]] | None:
    label_keys = rec.get("label_keys") or PMU_KEYS
    labels = rec.get("label")
    if labels is None:
        return None
    try:
        idx = [label_keys.index(k) for k in PMU_KEYS]
    except ValueError:
        return None
    out = []
    for row in labels:
        out.append([float(row[i]) for i in idx])
    return out


def _pad_side_feats(raw, n_core: int) -> List[List[float]]:
    F = len(tk.SIDE_FEATURE_KEYS)
    if raw is None:
        return [[0.0] * F for _ in range(n_core)]
    out = []
    for ci in range(n_core):
        row = list(raw[ci]) if ci < len(raw) else []
        row = [float(x) for x in row[:F]]
        if len(row) < F:
            row.extend([0.0] * (F - len(row)))
        out.append(row)
    return out


def _denom_vecs(raw, n_core: int) -> List[List[float]]:
    out = []
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


def build_cache_samples_from_jsonl(jsonl_path: str, hf_tokenizer,
                                   max_len: int = 8192,
                                   max_cores: int = tk.MAX_CORES) -> List[dict]:
    samples: List[dict] = []
    query_token_ids = {
        ci: hf_tokenizer.convert_tokens_to_ids(f"<QUERY_C{ci}>")
        for ci in range(max_cores)
    }
    with open(jsonl_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            ids = hf_tokenizer.convert_tokens_to_ids(rec["tokens"])
            if any(i is None or i == hf_tokenizer.unk_token_id for i in ids):
                continue
            if len(ids) > max_len:
                continue
            label = _remap_label(rec)
            if label is None:
                continue
            qpos = []
            for ci in range(rec["n_core"]):
                qt = query_token_ids[ci]
                pos = len(ids) - 1 - ids[::-1].index(qt)
                qpos.append(pos)
            is_uop = rec.get("is_uop")
            if is_uop is None:
                is_uop = [1 if t == "<UOP>" else 0 for t in rec["tokens"]]
            uop_fields = rec.get("uop_fields")
            if uop_fields is None:
                uop_fields = [[0, 0, 0, 0, 0, 0] for _ in ids]
            if len(is_uop) != len(ids) or len(uop_fields) != len(ids):
                continue
            samples.append({
                "ids": ids,
                "qpos": qpos,
                "label": label,
                "n_core": rec["n_core"],
                "instr_retired": rec["instr_retired"],
                "uops": rec.get("uops_per_core", rec["instr_retired"]),
                "t_start_rel": rec.get("t_start_rel",
                                       [0.0] * rec["n_core"]),
                "is_uop": is_uop,
                "uop_fields": uop_fields,
                "side_feats": _pad_side_feats(rec.get("side_feats"),
                                              rec["n_core"]),
                "denoms": _denom_vecs(rec.get("denoms"), rec["n_core"]),
            })
    return samples


class WindowDataset(Dataset):
    def __init__(self, jsonl_path: str, hf_tokenizer, max_len: int = 8192,
                 max_cores: int = tk.MAX_CORES, cache_path: str | None = None,
                 require_cache: bool = False, use_cache: bool = True):
        self.tok = hf_tokenizer
        self.max_len = max_len
        self.max_cores = max_cores
        self.jsonl_path = os.path.abspath(jsonl_path)
        self.cache_path = cache_path or default_cache_path(self.jsonl_path, max_len)
        self.mode = "eager"
        self.samples: List[dict] = []
        self.total_samples = 0
        self.shards: List[dict] = []
        self._cum_counts: List[int] = []
        self._loaded_shard_idx: int | None = None
        self._loaded_samples: List[dict] = []

        if require_cache and not use_cache:
            raise ValueError("require_cache=True conflicts with use_cache=False")

        if use_cache and self._try_load_cache():
            return

        if require_cache:
            raise FileNotFoundError(
                f"dataset cache missing or stale: {self.cache_path}"
            )

        self.samples = build_cache_samples_from_jsonl(
            self.jsonl_path, self.tok, self.max_len, self.max_cores
        )
        self.total_samples = len(self.samples)
        self.mode = "eager"
        if use_cache:
            self._save_single_shard_dir()
        else:
            self.mode = "eager_no_cache"

    @staticmethod
    def default_cache_path(jsonl_path: str, max_len: int) -> str:
        return default_cache_path(jsonl_path, max_len)

    def _cache_meta(self) -> dict:
        return build_cache_meta(
            self.jsonl_path, self.tok, self.max_len, self.max_cores
        )

    def _try_load_cache(self) -> bool:
        cp = Path(self.cache_path)
        if cp.is_dir():
            return self._try_load_sharded_cache(cp)
        if cp.is_file():
            return self._try_load_legacy_cache(cp)
        return False

    def _try_load_sharded_cache(self, cache_dir: Path) -> bool:
        manifest_path = cache_dir / MANIFEST_NAME
        if not manifest_path.exists():
            return False
        try:
            manifest = torch.load(manifest_path, map_location="cpu")
        except Exception:
            return False
        if not isinstance(manifest, dict):
            return False
        if manifest.get("meta") != self._cache_meta():
            return False
        shards = manifest.get("shards", [])
        if not shards:
            return False
        self.mode = "sharded"
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
            self.shards.append({
                "path": str(path),
                "count": count,
            })
        self.total_samples = total
        return total > 0

    def _try_load_legacy_cache(self, cache_file: Path) -> bool:
        try:
            blob = torch.load(cache_file, map_location="cpu")
        except Exception:
            return False
        if not isinstance(blob, dict):
            return False
        if blob.get("meta") != self._cache_meta():
            return False
        legacy_samples = blob.get("samples", [])
        if not legacy_samples:
            return False
        self.mode = "eager"
        self.samples = [{
            "ids": s.get("_ids", s["ids"]),
            "qpos": s.get("_qpos", s["qpos"]),
            "label": s["label"],
            "n_core": s["n_core"],
            "instr_retired": s["instr_retired"],
            "uops": s.get("uops", s["instr_retired"]),
            "t_start_rel": s.get("t_start_rel", [0.0] * s["n_core"]),
            "is_uop": s.get("is_uop", [0] * len(s.get("_ids", s["ids"]))),
            "uop_fields": s.get(
                "uop_fields",
                [[0, 0, 0, 0, 0, 0] for _ in s.get("_ids", s["ids"])]
            ),
            "side_feats": _pad_side_feats(s.get("side_feats"), s["n_core"]),
            "denoms": _denom_vecs(s.get("denoms"), s["n_core"]),
        } for s in legacy_samples]
        self.total_samples = len(self.samples)
        return True

    def _save_single_shard_dir(self) -> None:
        cache_dir = Path(self.cache_path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        shard_name = "shard-00000.pt"
        shard_tmp = cache_dir / f"{shard_name}.tmp"
        shard_path = cache_dir / shard_name
        torch.save({"samples": self.samples}, shard_tmp)
        os.replace(shard_tmp, shard_path)
        manifest = {
            "meta": self._cache_meta(),
            "total_samples": len(self.samples),
            "shards": [{"file": shard_name, "count": len(self.samples)}],
        }
        manifest_tmp = cache_dir / f"{MANIFEST_NAME}.tmp"
        torch.save(manifest, manifest_tmp)
        os.replace(manifest_tmp, cache_dir / MANIFEST_NAME)

    def _ensure_shard_loaded(self, shard_idx: int) -> None:
        if self._loaded_shard_idx == shard_idx:
            return
        blob = torch.load(self.shards[shard_idx]["path"], map_location="cpu")
        self._loaded_samples = blob["samples"]
        self._loaded_shard_idx = shard_idx

    def __len__(self):
        return self.total_samples

    def __getitem__(self, i):
        if self.mode == "sharded":
            shard_idx = bisect.bisect_right(self._cum_counts, i)
            shard_start = 0 if shard_idx == 0 else self._cum_counts[shard_idx - 1]
            self._ensure_shard_loaded(shard_idx)
            s = self._loaded_samples[i - shard_start]
        else:
            s = self.samples[i]
        return {
            "ids": s["ids"],
            "qpos": s["qpos"],
            "label": s["label"],            # [n_core, K]
            "n_core": s["n_core"],
            "instr_retired": s["instr_retired"],
            "uops": s.get("uops", s["instr_retired"]),
            "t_start_rel": s.get("t_start_rel", [0.0] * s["n_core"]),
            "is_uop": s.get("is_uop", [0] * len(s["ids"])),
            "uop_fields": s.get(
                "uop_fields",
                [[0, 0, 0, 0, 0, 0] for _ in s["ids"]]
            ),
            "side_feats": _pad_side_feats(s.get("side_feats"), s["n_core"]),
            "denoms": _denom_vecs(s.get("denoms"), s["n_core"]),
        }


def prepare_dataset_cache(jsonl_path: str, hf_tokenizer, max_len: int = 8192,
                          max_cores: int = tk.MAX_CORES,
                          cache_path: str | None = None) -> str:
    ds = WindowDataset(
        jsonl_path,
        hf_tokenizer,
        max_len=max_len,
        max_cores=max_cores,
        cache_path=cache_path,
        require_cache=False,
    )
    return ds.cache_path


def make_collate(pad_id: int):
    def collate(batch: List[dict]) -> Dict[str, torch.Tensor]:
        B = len(batch)
        maxL = max(len(b["ids"]) for b in batch)
        max_nc = max(b["n_core"] for b in batch)
        input_ids = torch.full((B, maxL), pad_id, dtype=torch.long)
        attn = torch.zeros((B, maxL), dtype=torch.long)
        is_uop = torch.zeros((B, maxL), dtype=torch.bool)
        uop_fields = torch.zeros((B, maxL, 6), dtype=torch.long)
        qpos = torch.zeros((B, max_nc), dtype=torch.long)
        label = torch.zeros((B, max_nc, K), dtype=torch.float32)
        core_mask = torch.zeros((B, max_nc), dtype=torch.float32)
        instr = torch.ones((B, max_nc), dtype=torch.float32)
        uops = torch.ones((B, max_nc), dtype=torch.float32)
        t_start = torch.zeros((B, max_nc), dtype=torch.float32)
        side = torch.zeros((B, max_nc, len(tk.SIDE_FEATURE_KEYS)),
                           dtype=torch.float32)
        denoms = torch.zeros((B, max_nc, len(DENOM_KEYS)),
                             dtype=torch.float32)
        for bi, b in enumerate(batch):
            L = len(b["ids"])
            input_ids[bi, :L] = torch.tensor(b["ids"], dtype=torch.long)
            attn[bi, :L] = 1
            is_uop[bi, :L] = torch.tensor(b.get("is_uop", [0] * L),
                                          dtype=torch.bool)
            uop_fields[bi, :L] = torch.tensor(
                b.get("uop_fields", [[0, 0, 0, 0, 0, 0] for _ in range(L)]),
                dtype=torch.long,
            )
            tsr = b.get("t_start_rel", [0.0] * b["n_core"])
            uops_b = b.get("uops", b["instr_retired"])
            for ci in range(b["n_core"]):
                qpos[bi, ci] = b["qpos"][ci]
                label[bi, ci] = torch.tensor(b["label"][ci],
                                             dtype=torch.float32)
                core_mask[bi, ci] = 1.0
                instr[bi, ci] = float(b["instr_retired"][ci])
                uops[bi, ci] = float(uops_b[ci])
                t_start[bi, ci] = float(tsr[ci])
                side[bi, ci] = torch.tensor(b["side_feats"][ci],
                                            dtype=torch.float32)
                denoms[bi, ci] = torch.tensor(b["denoms"][ci],
                                              dtype=torch.float32)
        return {
            "input_ids": input_ids,
            "attention_mask": attn,
            "is_uop": is_uop,
            "uop_fields": uop_fields,
            "query_pos": qpos,
            "label": label,
            "core_mask": core_mask,
            "instr_retired": instr,
            "uops": uops,
            "t_start": t_start,
            "side_feats": side,
            "denoms": denoms,
        }
    return collate
