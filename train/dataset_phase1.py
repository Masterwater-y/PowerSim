"""dataset_phase1.py — Phase 1 macro-chunk dataset for the semantic gate.

Loads:
  - data/v28_1/manifest.parquet           # split labels
  - data/v28_1/chunks/<run_id>/*.parquet  # macro chunks + labels
  - data/v28_1/prompts/<binary_hash>/<variant>/  # static-block prompt text

Emits one sample per (run_id, core_id, chunk_id) with:

  input_ids:        Long[T]      tokenized concatenation of unique BB prompts
  attention_mask:   Long[T]
  bb_boundary_pos:  Long[B, 2]   (start, end_inclusive) token positions per
                                  unique basic block encountered in the chunk
  macro_bb_idx:     Long[M]      per-retiring-macro index into bb_boundary_pos
  macro_uop_count:  Long[M]
  macro_op_class:   Long[M]
  macro_flags:      Long[M]
  n_macros:         Long
  n_uops:           Long
  cpi_macro:        Float        label
  valid:            Long         1 if label is valid

Constraints from docs/LLM语义建模方案.md §5:
  - only functional fields in the payload (no oracle/timing/coherence)
  - split labels come from manifest.parquet
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import torch
from torch.utils.data import Dataset

try:
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc


VARIANTS = ("real", "pseudo", "shuffle", "register_rename", "side_only")


@dataclass
class Phase1Config:
    manifest_path: str
    chunks_root: str
    prompts_root: str
    split: str = "train"           # train | family_ood | seed_ood | sealed_joint_ood
    cores: Sequence[int] = (1,)    # limit to specific core counts
    variant: str = "real"
    max_tokens: int = 4096
    max_macros_per_chunk: int = 256
    tokenizer_name: str = "Qwen/Qwen2.5-Coder-1.5B-Instruct"
    seed: int = 0


def _load_manifest(path: str) -> List[dict]:
    tbl = pq.read_table(path).to_pydict()
    n = len(tbl["run_id"])
    return [{k: tbl[k][i] for k in tbl} for i in range(n)]


def _index_prompt_dir(prompts_root: str, binary_hash: str,
                      variant: str) -> Dict[int, str]:
    """Return bb_id -> prompt_path for a (binary, variant)."""
    idx_path = os.path.join(prompts_root, binary_hash, variant, "index.parquet")
    if not os.path.isfile(idx_path):
        raise FileNotFoundError(idx_path)
    tbl = pq.read_table(idx_path).to_pydict()
    return {int(tbl["bb_id"][i]): str(tbl["prompt_path"][i]) for i in range(len(tbl["bb_id"]))}


class Phase1MacroChunkDataset(Dataset):
    """One example per valid macro chunk.

    Heavy work (chunk parquet -> in-memory rows, tokenization) is done lazily
    on __getitem__ so many workers can process disjoint index ranges.  We keep
    a tiny per-run "row-header" list in memory (~n_chunks * ~64 bytes).
    """

    def __init__(self, cfg: Phase1Config, tokenizer: Any):
        self.cfg = cfg
        self.tok = tokenizer
        if cfg.variant not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        self.side_only = cfg.variant == "side_only"
        manifest = _load_manifest(cfg.manifest_path)
        cores_set = set(int(c) for c in cfg.cores)
        rows: List[dict] = []
        for r in manifest:
            if str(r["split"]) != cfg.split:
                continue
            if int(r["cores"]) not in cores_set:
                continue
            if int(r.get("n_chunks", 0)) <= 0:
                continue
            rows.append(r)
        self.runs = rows
        # For each run, load the chunks parquet header (core_id, chunk_id,
        # n_uops, n_macros) and remember row offsets so per-example lookups
        # can seek directly.
        self._chunk_cache: Dict[str, dict] = {}
        self.index: List[Tuple[int, int]] = []  # (run_idx, local_chunk_row_idx)
        for run_idx, r in enumerate(self.runs):
            chunks_path = os.path.join(r["chunks_dir"], "chunks.parquet") \
                if r.get("chunks_dir") else os.path.join(
                    cfg.chunks_root, str(r["run_id"]), "chunks.parquet")
            labels_path = chunks_path.replace("chunks.parquet", "labels.parquet")
            if not (os.path.isfile(chunks_path) and os.path.isfile(labels_path)):
                continue
            # only pull the label validity vector; we lazy-load the full row
            lb = pq.read_table(labels_path, columns=["core_id", "chunk_id", "valid_label", "cpi_macro"]).to_pydict()
            self._chunk_cache[str(r["run_id"])] = {
                "chunks_path": chunks_path,
                "labels_path": labels_path,
                "core_id": lb["core_id"],
                "chunk_id": lb["chunk_id"],
                "valid": lb["valid_label"],
                "cpi_macro": lb["cpi_macro"],
            }
            for i, v in enumerate(lb["valid_label"]):
                if v and lb["cpi_macro"][i] is not None:
                    self.index.append((run_idx, i))
        # Load prompt index for each unique binary_hash.
        self._prompt_index: Dict[str, Dict[int, str]] = {}
        for r in self.runs:
            bh = str(r.get("binary_hash") or "")
            if not bh or self.side_only:
                continue
            if bh in self._prompt_index:
                continue
            self._prompt_index[bh] = _index_prompt_dir(cfg.prompts_root, bh, cfg.variant)

    def __len__(self) -> int:
        return len(self.index)

    def _load_chunk_row(self, run_idx: int, local_idx: int) -> dict:
        r = self.runs[run_idx]
        cache = self._chunk_cache[str(r["run_id"])]
        # Read only the target row using row-group skipping when possible;
        # for a smoke run we just read the whole table once and cache by index.
        if "_rows" not in cache:
            tbl = pq.read_table(cache["chunks_path"]).to_pydict()
            cache["_rows"] = tbl
        tbl = cache["_rows"]
        return {k: tbl[k][local_idx] for k in tbl}

    def _pc_to_bb_id(self, binary_hash: str, module_pc: int) -> Optional[int]:
        """Map a macro's static PC to a basic-block id via the prompt index.

        The prompt index rows carry ``module_pc_start`` (block entry).  Blocks
        that never start at ``module_pc`` (mid-block macros) are folded into
        the block whose start pc is the largest one <= module_pc.  This maps
        every retired macro to the block that contains it.
        """
        # Cheap linear cache; the number of BBs per binary (~29k) makes a bisect
        # useful only for very large chunks.  A per-binary sorted list is built
        # once on first use.
        cache_key = (binary_hash, "_sorted_pcs")
        if cache_key not in self.__dict__:
            idx = self._prompt_index.get(binary_hash, {})
            if not idx:
                self.__dict__[cache_key] = ([], {})
            else:
                # read the module_pc_start column from the parquet index once
                idx_path = os.path.join(self.cfg.prompts_root, binary_hash,
                                        self.cfg.variant, "index.parquet")
                tbl = pq.read_table(idx_path).to_pydict()
                pairs = sorted(
                    zip(tbl["module_pc_start"], tbl["bb_id"]),
                    key=lambda x: int(x[0]),
                )
                self.__dict__[cache_key] = (
                    [int(p) for p, _ in pairs],
                    {int(p): int(b) for p, b in pairs},
                )
        pcs, pc_to_bb = self.__dict__[cache_key]
        if not pcs:
            return None
        # binary search for the largest pc <= module_pc
        import bisect
        i = bisect.bisect_right(pcs, int(module_pc)) - 1
        if i < 0:
            return None
        return int(pc_to_bb[pcs[i]])

    def _read_prompt(self, binary_hash: str, bb_id: int) -> str:
        idx = self._prompt_index.get(binary_hash, {})
        p = idx.get(int(bb_id))
        if not p or not os.path.isfile(p):
            return ""
        with open(p, "r") as fh:
            return fh.read()

    def _cached_tokens(self, binary_hash: str, bb_id: int) -> List[int]:
        """Return tokenized prompt for (binary_hash, bb_id); cached per-worker.

        Basic-block token IDs are pure functions of (binary, bb_id, variant,
        tokenizer). We stash them on ``self`` so subsequent chunks sampling
        the same block skip both file I/O and BPE work.
        """
        cache = self.__dict__.setdefault("_tok_cache", {})
        key = (binary_hash, int(bb_id))
        hit = cache.get(key)
        if hit is not None:
            return hit
        text = self._read_prompt(binary_hash, bb_id) if bb_id >= 0 else "B?:\n"
        ids = self.tok(text, add_special_tokens=False)["input_ids"]
        if not ids:
            ids = [self.tok.pad_token_id or 0]
        # cap cache size so a workload sweep doesn't OOM the dataloader worker
        if len(cache) > 65536:
            cache.clear()
        cache[key] = ids
        return ids

    def __getitem__(self, i: int) -> Dict[str, torch.Tensor]:
        run_idx, local_idx = self.index[i]
        row = self._load_chunk_row(run_idx, local_idx)
        run = self.runs[run_idx]
        cache = self._chunk_cache[str(run["run_id"])]
        cpi_macro = float(cache["cpi_macro"][local_idx])
        macro_pcs = list(row["per_macro_static_pc"])
        macro_ucs = list(row["per_macro_uop_count"])
        macro_ocs = list(row["per_macro_op_class"])
        macro_flags = list(row["per_macro_flags"])
        n_macros = len(macro_pcs)
        n_uops = int(row["n_uops"])
        if n_macros > self.cfg.max_macros_per_chunk:
            macro_pcs = macro_pcs[: self.cfg.max_macros_per_chunk]
            macro_ucs = macro_ucs[: self.cfg.max_macros_per_chunk]
            macro_ocs = macro_ocs[: self.cfg.max_macros_per_chunk]
            macro_flags = macro_flags[: self.cfg.max_macros_per_chunk]
            n_macros = len(macro_pcs)

        # Map each macro's static PC to its BB id.
        bhash = str(run["binary_hash"])
        macro_bb: List[int] = []
        unique_bb: List[int] = []
        bb_to_local: Dict[int, int] = {}
        for pc in macro_pcs:
            bb_id = self._pc_to_bb_id(bhash, int(pc))
            if bb_id is None:
                bb_id = -1
            if bb_id not in bb_to_local:
                bb_to_local[bb_id] = len(unique_bb)
                unique_bb.append(bb_id)
            macro_bb.append(bb_to_local[bb_id])

        if self.side_only or not unique_bb:
            input_ids = torch.zeros(1, dtype=torch.long)
            attn_mask = torch.zeros(1, dtype=torch.long)
            bb_pos = torch.zeros((max(1, len(unique_bb)), 2), dtype=torch.long)
        else:
            # Concatenate unique-bb prompt texts and remember the token span
            # for each unique block (used to pool E_static per BB).
            token_lists: List[List[int]] = []
            spans: List[Tuple[int, int]] = []
            offset = 0
            for bb_id in unique_bb:
                ids = self._cached_tokens(bhash, int(bb_id))
                token_lists.append(ids)
                spans.append((offset, offset + len(ids) - 1))
                offset += len(ids)
                if offset >= self.cfg.max_tokens:
                    break
            flat = [t for lst in token_lists for t in lst][: self.cfg.max_tokens]
            input_ids = torch.tensor(flat, dtype=torch.long)
            attn_mask = torch.ones_like(input_ids)
            bb_pos = torch.tensor([(s, min(e, self.cfg.max_tokens - 1))
                                    for s, e in spans[: len(token_lists)]],
                                   dtype=torch.long)

        return {
            "input_ids": input_ids,
            "attention_mask": attn_mask,
            "bb_boundary_pos": bb_pos,
            "macro_bb_idx": torch.tensor(macro_bb, dtype=torch.long),
            "macro_uop_count": torch.tensor(macro_ucs, dtype=torch.long),
            "macro_op_class": torch.tensor(macro_ocs, dtype=torch.long),
            "macro_flags": torch.tensor(macro_flags, dtype=torch.long),
            "n_macros": torch.tensor(n_macros, dtype=torch.long),
            "n_uops": torch.tensor(n_uops, dtype=torch.long),
            "cpi_macro": torch.tensor(cpi_macro, dtype=torch.float),
            "valid": torch.tensor(1, dtype=torch.long),
            "run_id": str(run["run_id"]),
            "core_id": int(row["core_id"]),
            "chunk_id": int(row["chunk_id"]),
        }


def collate_phase1(batch: List[Dict[str, Any]], pad_id: int = 0) -> Dict[str, Any]:
    """Right-pad variable-length input_ids, bb_boundary_pos and macro fields."""
    T = max(x["input_ids"].shape[0] for x in batch)
    B = max(x["bb_boundary_pos"].shape[0] for x in batch)
    M = max(x["macro_bb_idx"].shape[0] for x in batch)
    def _pad_1d(t: torch.Tensor, size: int, val: int = 0) -> torch.Tensor:
        if t.shape[0] == size:
            return t
        pad = torch.full((size - t.shape[0],), val, dtype=t.dtype)
        return torch.cat([t, pad], dim=0)
    def _pad_2d(t: torch.Tensor, size: int, cols: int, val: int = 0) -> torch.Tensor:
        if t.shape[0] == size:
            return t
        pad = torch.full((size - t.shape[0], cols), val, dtype=t.dtype)
        return torch.cat([t, pad], dim=0)
    out = {
        "input_ids": torch.stack([_pad_1d(x["input_ids"], T, pad_id) for x in batch]),
        "attention_mask": torch.stack([_pad_1d(x["attention_mask"], T, 0) for x in batch]),
        "bb_boundary_pos": torch.stack([_pad_2d(x["bb_boundary_pos"], B, 2, 0) for x in batch]),
        "bb_valid_mask": torch.stack([
            torch.cat([torch.ones(x["bb_boundary_pos"].shape[0], dtype=torch.long),
                       torch.zeros(B - x["bb_boundary_pos"].shape[0], dtype=torch.long)])
            for x in batch]),
        "macro_bb_idx": torch.stack([_pad_1d(x["macro_bb_idx"], M, 0) for x in batch]),
        "macro_uop_count": torch.stack([_pad_1d(x["macro_uop_count"], M, 0) for x in batch]),
        "macro_op_class": torch.stack([_pad_1d(x["macro_op_class"], M, 0) for x in batch]),
        "macro_flags": torch.stack([_pad_1d(x["macro_flags"], M, 0) for x in batch]),
        "macro_valid_mask": torch.stack([
            torch.cat([torch.ones(x["macro_bb_idx"].shape[0], dtype=torch.long),
                       torch.zeros(M - x["macro_bb_idx"].shape[0], dtype=torch.long)])
            for x in batch]),
        "n_macros": torch.stack([x["n_macros"] for x in batch]),
        "n_uops": torch.stack([x["n_uops"] for x in batch]),
        "cpi_macro": torch.stack([x["cpi_macro"] for x in batch]),
        "valid": torch.stack([x["valid"] for x in batch]),
        "meta": [
            {"run_id": x["run_id"], "core_id": x["core_id"], "chunk_id": x["chunk_id"]}
            for x in batch
        ],
    }
    return out
