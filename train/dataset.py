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

MANIFEST_NAME = "manifest.pt"


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
        "feat_version": 4,  # v4: Scheme A 5-head labels with RD/stride inputs
    }


def build_cache_samples_from_jsonl(jsonl_path: str, hf_tokenizer,
                                   max_len: int = 8192,
                                   max_cores: int = 8) -> List[dict]:
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
            qpos = []
            for ci in range(rec["n_core"]):
                qt = query_token_ids[ci]
                pos = len(ids) - 1 - ids[::-1].index(qt)
                qpos.append(pos)
            samples.append({
                "ids": ids,
                "qpos": qpos,
                "label": rec["label"],
                "n_core": rec["n_core"],
                "instr_retired": rec["instr_retired"],
                "t_start_rel": rec.get("t_start_rel",
                                       [0.0] * rec["n_core"]),
            })
    return samples


class WindowDataset(Dataset):
    def __init__(self, jsonl_path: str, hf_tokenizer, max_len: int = 8192,
                 max_cores: int = 8, cache_path: str | None = None,
                 require_cache: bool = False):
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

        if not self._try_load_cache():
            if require_cache:
                raise FileNotFoundError(
                    f"dataset cache missing or stale: {self.cache_path}"
                )
            self.samples = build_cache_samples_from_jsonl(
                self.jsonl_path, self.tok, self.max_len, self.max_cores
            )
            self.total_samples = len(self.samples)
            self._save_single_shard_dir()

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
            "t_start_rel": s.get("t_start_rel", [0.0] * s["n_core"]),
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
            "t_start_rel": s.get("t_start_rel", [0.0] * s["n_core"]),
        }


def prepare_dataset_cache(jsonl_path: str, hf_tokenizer, max_len: int = 8192,
                          max_cores: int = 8,
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
        qpos = torch.zeros((B, max_nc), dtype=torch.long)
        label = torch.zeros((B, max_nc, K), dtype=torch.float32)
        core_mask = torch.zeros((B, max_nc), dtype=torch.float32)
        instr = torch.ones((B, max_nc), dtype=torch.float32)
        t_start = torch.zeros((B, max_nc), dtype=torch.float32)
        for bi, b in enumerate(batch):
            L = len(b["ids"])
            input_ids[bi, :L] = torch.tensor(b["ids"], dtype=torch.long)
            attn[bi, :L] = 1
            tsr = b.get("t_start_rel", [0.0] * b["n_core"])
            for ci in range(b["n_core"]):
                qpos[bi, ci] = b["qpos"][ci]
                label[bi, ci] = torch.tensor(b["label"][ci],
                                             dtype=torch.float32)
                core_mask[bi, ci] = 1.0
                instr[bi, ci] = float(b["instr_retired"][ci])
                t_start[bi, ci] = float(tsr[ci])
        return {
            "input_ids": input_ids,
            "attention_mask": attn,
            "query_pos": qpos,
            "label": label,
            "core_mask": core_mask,
            "instr_retired": instr,
            "t_start": t_start,
        }
    return collate
