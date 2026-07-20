"""dataset.py — windows.jsonl / sharded ids cache -> 训练 batch。

长期推荐格式：
  windows.maxlen{N}.tensor_cache/
    manifest.pt
    shard-00000.pt
    shard-00001.pt
    ...

训练阶段直接读取 shard cache，而不是再从 windows.jsonl 现算 ids/qpos。
旧的 windows.maxlen{N}.ids_cache/ 仍保留为兼容回退。
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
TENSOR_CACHE_FORMAT = "tensor_v1"
INPUT_MODE_GLOBAL = "global"
INPUT_MODE_LOCAL_CORE = "local_core"

# v24 native-macro rewriter: keep only these structural tokens; drop everything
# else and re-encode each <UOP> position as a Qwen-native macro-asm string.
_NATIVE_STRUCT_PREFIXES = ("<SYS", "<TRACE", "<C", "<QUERY_C", "<LOCAL_C",
                           "<PAD_UOP", "<PAD")


def _is_native_struct(tok: str) -> bool:
    if not tok.startswith("<"):
        return False
    if tok.startswith("<CFG_") or tok.startswith("<SM_") or tok.startswith("<G_"):
        return False
    if tok in ("<UOP>", "<SYNC>"):
        return False
    if tok.startswith("<OP_") or tok.startswith("<RG_") or tok.startswith("<MK_"):
        return False
    if tok.startswith("<RD_") or tok.startswith("<ST_") or tok.startswith("<BR_"):
        return False
    return True


def rewrite_tokens_native_macro(rec: dict, hf_tokenizer):
    """v24 pipeline: transform a v22-format record into native macro-asm ids.

    Returns a NEW dict with fields (tokens/is_uop/uop_fields not used further):
      ids            : List[int]        (already-tokenized subword ids)
      is_uop_flags   : List[int]        (all zeros)
      uop_fields_zero: List[List[int]]  (all zero rows)
      query_pos      : List[int]        (per-core <QUERY_Ci> id positions)
      local_pos      : List[int]        (per-core <LOCAL_Ci> id positions)

    Every <UOP> position expands to 3-6 subword ids of native macro assembly.
    Every non-structural v22 special token is dropped.
    """
    tokens = rec["tokens"]
    is_uop = rec.get("is_uop") or [1 if t == "<UOP>" else 0 for t in tokens]
    uop_fields = rec.get("uop_fields") or [[0] * 6 for _ in tokens]

    struct_ids = {}
    n_core = int(rec.get("n_core", 0))
    for ci in range(max(n_core, 32)):
        for name in (f"<C{ci}_BEGIN>", f"<C{ci}_END>",
                     f"<QUERY_C{ci}>", f"<LOCAL_C{ci}>"):
            tid = hf_tokenizer.convert_tokens_to_ids(name)
            if tid is not None and tid != hf_tokenizer.unk_token_id:
                struct_ids[name] = tid
    for name in ("<SYS>", "<TRACE>", "<TRACE_END>", "<PAD_UOP>"):
        tid = hf_tokenizer.convert_tokens_to_ids(name)
        if tid is not None and tid != hf_tokenizer.unk_token_id:
            struct_ids[name] = tid

    out_ids: List[int] = []
    q_pos: Dict[int, int] = {}
    l_pos: Dict[int, int] = {}

    for tok, uop_flag, fields in zip(tokens, is_uop, uop_fields):
        if uop_flag:
            line = tk.render_uop_field_row_as_macro(fields)
            sub_ids = hf_tokenizer.encode(" " + line + "\n",
                                          add_special_tokens=False)
            out_ids.extend(sub_ids)
            continue
        if not _is_native_struct(tok):
            continue
        tid = struct_ids.get(tok)
        if tid is None:
            continue
        # record positions for query/local anchors
        if tok.startswith("<QUERY_C"):
            ci = int(tok[len("<QUERY_C"):-1])
            q_pos[ci] = len(out_ids)
        elif tok.startswith("<LOCAL_C"):
            ci = int(tok[len("<LOCAL_C"):-1])
            l_pos[ci] = len(out_ids)
        out_ids.append(tid)

    if len(q_pos) != n_core:
        return None
    query_pos = [q_pos[ci] for ci in range(n_core)]
    local_pos = [l_pos.get(ci, q_pos[ci]) for ci in range(n_core)]

    return {
        "ids": out_ids,
        "is_uop_flags": [0] * len(out_ids),
        "uop_fields_zero": [[0, 0, 0, 0, 0, 0] for _ in out_ids],
        "query_pos": query_pos,
        "local_pos": local_pos,
    }



DENOM_KEYS = [
    "branch_count",
    "loads",
    "stores",
    "atomics",
    "mem_ops",
    "page_touches",
]


def ids_cache_path(jsonl_path: str, max_len: int,
                   input_mode: str = INPUT_MODE_GLOBAL) -> str:
    p = Path(jsonl_path)
    if input_mode == INPUT_MODE_LOCAL_CORE:
        return str(p.with_name(f"{p.stem}.maxlen{max_len}.local_ids_cache"))
    return str(p.with_name(f"{p.stem}.maxlen{max_len}.ids_cache"))


def tensor_cache_path(jsonl_path: str, max_len: int,
                      input_mode: str = INPUT_MODE_GLOBAL) -> str:
    p = Path(jsonl_path)
    if input_mode == INPUT_MODE_LOCAL_CORE:
        return str(p.with_name(f"{p.stem}.maxlen{max_len}.local_tensor_cache"))
    return str(p.with_name(f"{p.stem}.maxlen{max_len}.tensor_cache"))


def default_cache_path(jsonl_path: str, max_len: int,
                       input_mode: str = INPUT_MODE_GLOBAL) -> str:
    tensor_path = tensor_cache_path(jsonl_path, max_len, input_mode=input_mode)
    if Path(tensor_path).exists():
        return tensor_path
    return ids_cache_path(jsonl_path, max_len, input_mode=input_mode)


def build_cache_meta(jsonl_path: str, hf_tokenizer, max_len: int,
                     max_cores: int,
                     input_mode: str = INPUT_MODE_GLOBAL) -> dict:
    st = os.stat(jsonl_path)
    meta = {
        "jsonl_path": os.path.abspath(jsonl_path),
        "jsonl_size": int(st.st_size),
        "jsonl_mtime_ns": int(st.st_mtime_ns),
        "max_len": int(max_len),
        "tokenizer_len": int(len(hf_tokenizer)),
        "unk_token_id": int(
            -1 if hf_tokenizer.unk_token_id is None else hf_tokenizer.unk_token_id
        ),
        "max_cores": int(max_cores),
        "feat_version": 16,
        "pmu_keys": list(PMU_KEYS),
        "side_feat_dim": len(tk.SIDE_FEATURE_KEYS),
    }
    if input_mode == INPUT_MODE_LOCAL_CORE:
        meta["input_mode"] = INPUT_MODE_LOCAL_CORE
        meta["feat_version"] = 19
    return meta


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


def _find_token(tokens: List[str], target: str, start: int = 0) -> int:
    try:
        return tokens.index(target, start)
    except ValueError:
        return -1


def _build_local_core_sequences(rec: dict, ids: List[int],
                                max_len: int) -> dict | None:
    """Build one short local input sequence per core.

    Each local sequence is prefix + this core's segment.  This makes LOCAL_Ci
    causal-visible only to the global-lite prefix and its own core segment.
    """
    tokens = rec["tokens"]
    n_core = int(rec["n_core"])
    is_uop = rec.get("is_uop")
    if is_uop is None:
        is_uop = [1 if t == "<UOP>" else 0 for t in tokens]
    uop_fields = rec.get("uop_fields")
    if uop_fields is None:
        uop_fields = [[0, 0, 0, 0, 0, 0] for _ in tokens]
    if len(is_uop) != len(tokens) or len(uop_fields) != len(tokens):
        return None

    first_begin = min(
        (idx for idx in (
            _find_token(tokens, f"<C{ci}_BEGIN>") for ci in range(n_core)
        ) if idx >= 0),
        default=-1,
    )
    if first_begin < 0:
        return None
    prefix_ids = ids[:first_begin]
    prefix_is_uop = list(is_uop[:first_begin])
    prefix_uop_fields = list(uop_fields[:first_begin])

    local_input_ids: List[List[int]] = []
    local_is_uop: List[List[int]] = []
    local_uop_fields: List[List[List[int]]] = []
    local_query_pos: List[int] = []
    for ci in range(n_core):
        begin = _find_token(tokens, f"<C{ci}_BEGIN>")
        end = _find_token(tokens, f"<C{ci}_END>", begin + 1)
        loc = _find_token(tokens, f"<LOCAL_C{ci}>", begin + 1)
        if begin < 0 or end < 0 or loc < 0 or loc > end:
            return None
        seg_slice = slice(begin, end + 1)
        seq_ids = prefix_ids + ids[seg_slice]
        seq_is_uop = prefix_is_uop + list(is_uop[seg_slice])
        seq_uop_fields = prefix_uop_fields + list(uop_fields[seg_slice])
        if len(seq_ids) > max_len:
            return None
        local_input_ids.append(seq_ids)
        local_is_uop.append(seq_is_uop)
        local_uop_fields.append(seq_uop_fields)
        local_query_pos.append(len(prefix_ids) + (loc - begin))

    return {
        "local_input_ids": local_input_ids,
        "local_is_uop": local_is_uop,
        "local_uop_fields": local_uop_fields,
        "local_query_pos": local_query_pos,
    }


def build_cache_samples_from_jsonl(jsonl_path: str, hf_tokenizer,
                                   max_len: int = 8192,
                                   max_cores: int = tk.MAX_CORES,
                                   input_mode: str = INPUT_MODE_GLOBAL) -> List[dict]:
    samples: List[dict] = []
    # auto-detect native-macro mode: v22 injects <OP_0>, native does not
    op0 = hf_tokenizer.convert_tokens_to_ids("<OP_0>")
    unk = hf_tokenizer.unk_token_id
    native_macro = (op0 is None) or (op0 == unk)
    query_token_ids = {
        ci: hf_tokenizer.convert_tokens_to_ids(f"<QUERY_C{ci}>")
        for ci in range(max_cores)
    }
    local_token_ids = {
        ci: hf_tokenizer.convert_tokens_to_ids(f"<LOCAL_C{ci}>")
        for ci in range(max_cores)
    }
    with open(jsonl_path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            rec = json.loads(s)
            if native_macro:
                nm = rewrite_tokens_native_macro(rec, hf_tokenizer)
                if nm is None:
                    continue
                ids = nm["ids"]
                if input_mode != INPUT_MODE_LOCAL_CORE and len(ids) > max_len:
                    continue
                label = _remap_label(rec)
                if label is None:
                    continue
                qpos = nm["query_pos"]
                lpos = nm["local_pos"]
                is_uop = nm["is_uop_flags"]
                uop_fields = nm["uop_fields_zero"]
            else:
                ids = hf_tokenizer.convert_tokens_to_ids(rec["tokens"])
                if any(i is None or i == hf_tokenizer.unk_token_id for i in ids):
                    continue
                if input_mode != INPUT_MODE_LOCAL_CORE and len(ids) > max_len:
                    continue
                label = _remap_label(rec)
                if label is None:
                    continue
                qpos = []
                lpos = []
                for ci in range(rec["n_core"]):
                    qt = query_token_ids[ci]
                    pos = len(ids) - 1 - ids[::-1].index(qt)
                    qpos.append(pos)
                    lt = local_token_ids[ci]
                    lpos.append(ids.index(lt) if lt in ids else pos)
                is_uop = rec.get("is_uop")
                if is_uop is None:
                    is_uop = [1 if t == "<UOP>" else 0 for t in rec["tokens"]]
                uop_fields = rec.get("uop_fields")
                if uop_fields is None:
                    uop_fields = [[0, 0, 0, 0, 0, 0] for _ in ids]
            if len(is_uop) != len(ids) or len(uop_fields) != len(ids):
                continue
            if input_mode == INPUT_MODE_LOCAL_CORE:
                local = _build_local_core_sequences(rec, ids, max_len)
                if local is None:
                    continue
                store_ids = ids[:1]
                store_is_uop = [0]
                store_uop_fields = [[0, 0, 0, 0, 0, 0]]
                store_qpos = [0] * int(rec["n_core"])
                store_lpos = [0] * int(rec["n_core"])
            else:
                local = None
                store_ids = ids
                store_is_uop = is_uop
                store_uop_fields = uop_fields
                store_qpos = qpos
                store_lpos = lpos
            sample = {
                "ids": store_ids,
                "qpos": store_qpos,
                "local_pos": store_lpos,
                "label": label,
                "n_core": rec["n_core"],
                "instr_retired": rec["instr_retired"],
                "uops": rec.get("uops_per_core", rec["instr_retired"]),
                "t_start_rel": rec.get("t_start_rel",
                                       [0.0] * rec["n_core"]),
                "is_uop": store_is_uop,
                "uop_fields": store_uop_fields,
                "side_feats": _pad_side_feats(rec.get("side_feats"),
                                              rec["n_core"]),
                "denoms": _denom_vecs(rec.get("denoms"), rec["n_core"]),
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
                    "legacy_token_len": rec.get("legacy_token_len", 0),
                },
            }
            if local is not None:
                sample.update(local)
            samples.append(sample)
    return samples


def build_tensor_cache_shard(samples: List[dict]) -> dict:
    """Pack v9 samples into tensor-only shard storage.

    This keeps the v9 label/model schema unchanged while avoiding repeated
    unpickling of deeply nested Python lists in DataLoader workers.
    """
    n = len(samples)
    if n == 0:
        return {
            "format": TENSOR_CACHE_FORMAT,
            "count": 0,
            "max_n_core": 0,
        }
    side_dim = len(tk.SIDE_FEATURE_KEYS)
    denom_dim = len(DENOM_KEYS)
    max_nc = max(int(s["n_core"]) for s in samples)

    ids_offsets = [0]
    ids_flat: list[int] = []
    is_uop_flat: list[bool] = []
    uop_fields_flat: list[list[int]] = []
    has_local_core = any("local_input_ids" in s for s in samples)
    local_core_offsets = [0]
    local_ids_offsets = [0]
    local_ids_flat: list[int] = []
    local_is_uop_flat: list[bool] = []
    local_uop_fields_flat: list[list[int]] = []

    n_core = torch.zeros((n,), dtype=torch.int16)
    qpos = torch.zeros((n, max_nc), dtype=torch.int32)
    local_pos = torch.zeros((n, max_nc), dtype=torch.int32)
    local_query_pos = torch.zeros((n, max_nc), dtype=torch.int32)
    label = torch.zeros((n, max_nc, K), dtype=torch.float32)
    instr = torch.ones((n, max_nc), dtype=torch.float32)
    uops = torch.ones((n, max_nc), dtype=torch.float32)
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
    meta_legacy_token_len = torch.zeros((n,), dtype=torch.int32)

    for si, s in enumerate(samples):
        ids = list(s["ids"])
        L = len(ids)
        nc = int(s["n_core"])
        meta = s.get("meta") or {}
        n_core[si] = nc
        ids_flat.extend(int(x) for x in ids)
        ids_offsets.append(len(ids_flat))
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
        meta_legacy_token_len[si] = int(
            meta.get("legacy_token_len", 0) or 0
        )

        iu = s.get("is_uop", [0] * L)
        uf = s.get("uop_fields", [[0, 0, 0, 0, 0, 0] for _ in range(L)])
        is_uop_flat.extend(bool(x) for x in iu[:L])
        for row in uf[:L]:
            out_row = [int(v) for v in row[:6]]
            if len(out_row) < 6:
                out_row.extend([0] * (6 - len(out_row)))
            uop_fields_flat.append(out_row)

        qpos[si, :nc] = torch.as_tensor(s["qpos"][:nc], dtype=torch.int32)
        local_pos[si, :nc] = torch.as_tensor(
            s.get("local_pos", s["qpos"])[:nc], dtype=torch.int32)
        if has_local_core:
            local_query_pos[si, :nc] = torch.as_tensor(
                s["local_query_pos"][:nc], dtype=torch.int32)
            for ci in range(nc):
                lids = list(s["local_input_ids"][ci])
                liu = list(s["local_is_uop"][ci])
                luf = list(s["local_uop_fields"][ci])
                local_ids_flat.extend(int(x) for x in lids)
                local_is_uop_flat.extend(bool(x) for x in liu[:len(lids)])
                for row in luf[:len(lids)]:
                    out_row = [int(v) for v in row[:6]]
                    if len(out_row) < 6:
                        out_row.extend([0] * (6 - len(out_row)))
                    local_uop_fields_flat.append(out_row)
                local_ids_offsets.append(len(local_ids_flat))
            local_core_offsets.append(local_core_offsets[-1] + nc)
        label[si, :nc] = torch.as_tensor(s["label"][:nc], dtype=torch.float32)
        instr[si, :nc] = torch.as_tensor(
            s["instr_retired"][:nc], dtype=torch.float32)
        uops[si, :nc] = torch.as_tensor(
            s.get("uops", s["instr_retired"])[:nc], dtype=torch.float32)
        t_start[si, :nc] = torch.as_tensor(
            s.get("t_start_rel", [0.0] * nc)[:nc], dtype=torch.float32)
        side[si, :nc] = torch.as_tensor(
            _pad_side_feats(s.get("side_feats"), nc), dtype=torch.float32)
        denoms[si, :nc] = torch.as_tensor(
            _denom_vecs(s.get("denoms"), nc), dtype=torch.float32)

    if not uop_fields_flat:
        uop_fields = torch.zeros((0, 6), dtype=torch.int16)
    else:
        uop_fields = torch.as_tensor(uop_fields_flat, dtype=torch.int16)
    if not local_uop_fields_flat:
        local_uop_fields = torch.zeros((0, 6), dtype=torch.int16)
    else:
        local_uop_fields = torch.as_tensor(
            local_uop_fields_flat, dtype=torch.int16)

    out = {
        "format": TENSOR_CACHE_FORMAT,
        "count": n,
        "max_n_core": max_nc,
        "ids_offsets": torch.as_tensor(ids_offsets, dtype=torch.int64),
        "ids_flat": torch.as_tensor(ids_flat, dtype=torch.int32),
        "is_uop_flat": torch.as_tensor(is_uop_flat, dtype=torch.bool),
        "uop_fields_flat": uop_fields,
        "n_core": n_core,
        "qpos": qpos,
        "local_pos": local_pos,
        "label": label,
        "instr_retired": instr,
        "uops": uops,
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
        "legacy_token_len": meta_legacy_token_len,
    }
    if has_local_core:
        out.update({
            "local_core_offsets": torch.as_tensor(
                local_core_offsets, dtype=torch.int64),
            "local_ids_offsets": torch.as_tensor(
                local_ids_offsets, dtype=torch.int64),
            "local_ids_flat": torch.as_tensor(
                local_ids_flat, dtype=torch.int32),
            "local_is_uop_flat": torch.as_tensor(
                local_is_uop_flat, dtype=torch.bool),
            "local_uop_fields_flat": local_uop_fields,
            "local_query_pos": local_query_pos,
        })
    return out


class WindowDataset(Dataset):
    def __init__(self, jsonl_path: str, hf_tokenizer, max_len: int = 8192,
                 max_cores: int = tk.MAX_CORES, cache_path: str | None = None,
                 require_cache: bool = False, use_cache: bool = True,
                 input_mode: str = INPUT_MODE_GLOBAL):
        self.tok = hf_tokenizer
        self.max_len = max_len
        self.max_cores = max_cores
        self.input_mode = str(input_mode)
        self.jsonl_path = os.path.abspath(jsonl_path)
        self._explicit_cache_path = cache_path is not None
        self.cache_path = cache_path or default_cache_path(
            self.jsonl_path, max_len, input_mode=self.input_mode)
        self.mode = "eager"
        self.samples: List[dict] = []
        self.total_samples = 0
        self.shards: List[dict] = []
        self._cum_counts: List[int] = []
        self._loaded_shard_idx: int | None = None
        self._loaded_samples: List[dict] = []
        self._loaded_tensor_shard: dict | None = None
        self._cache_label_idx: List[int] | None = None

        if require_cache and not use_cache:
            raise ValueError("require_cache=True conflicts with use_cache=False")

        if use_cache and self._try_load_cache():
            return

        if require_cache:
            raise FileNotFoundError(
                f"dataset cache missing or stale: {self.cache_path}"
            )

        self.samples = build_cache_samples_from_jsonl(
            self.jsonl_path, self.tok, self.max_len, self.max_cores,
            input_mode=self.input_mode,
        )
        self.total_samples = len(self.samples)
        self.mode = "eager"
        if use_cache:
            self._save_single_shard_dir()
        else:
            self.mode = "eager_no_cache"

    @staticmethod
    def default_cache_path(jsonl_path: str, max_len: int,
                           input_mode: str = INPUT_MODE_GLOBAL) -> str:
        return default_cache_path(jsonl_path, max_len, input_mode=input_mode)

    @staticmethod
    def ids_cache_path(jsonl_path: str, max_len: int,
                       input_mode: str = INPUT_MODE_GLOBAL) -> str:
        return ids_cache_path(jsonl_path, max_len, input_mode=input_mode)

    @staticmethod
    def tensor_cache_path(jsonl_path: str, max_len: int,
                          input_mode: str = INPUT_MODE_GLOBAL) -> str:
        return tensor_cache_path(jsonl_path, max_len, input_mode=input_mode)

    def _cache_meta(self) -> dict:
        return build_cache_meta(
            self.jsonl_path, self.tok, self.max_len, self.max_cores,
            input_mode=self.input_mode,
        )

    def _try_load_cache(self) -> bool:
        candidates = [Path(self.cache_path)]
        if not self._explicit_cache_path:
            fallback = Path(ids_cache_path(
                self.jsonl_path, self.max_len, input_mode=self.input_mode))
            if fallback not in candidates:
                candidates.append(fallback)
        for cp in candidates:
            ok = False
            if cp.is_dir():
                ok = self._try_load_sharded_cache(cp)
            elif cp.is_file():
                ok = self._try_load_legacy_cache(cp)
            if ok:
                self.cache_path = str(cp)
                return True
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
        ok, label_idx = self._cache_meta_compatible(manifest.get("meta"))
        if not ok:
            return False
        self._cache_label_idx = label_idx
        if manifest.get("format") == TENSOR_CACHE_FORMAT:
            return self._try_load_tensor_cache(cache_dir, manifest)
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

    def _try_load_tensor_cache(self, cache_dir: Path, manifest: dict) -> bool:
        shards = manifest.get("shards", [])
        if not shards:
            return False
        self.mode = "tensor_sharded"
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
        ok, label_idx = self._cache_meta_compatible(blob.get("meta"))
        if not ok:
            return False
        self._cache_label_idx = label_idx
        legacy_samples = blob.get("samples", [])
        if not legacy_samples:
            return False
        self.mode = "eager"
        out_samples = []
        for s in legacy_samples:
            item = {
            "ids": s.get("_ids", s["ids"]),
            "qpos": s.get("_qpos", s["qpos"]),
            "local_pos": s.get("local_pos", s.get("_qpos", s["qpos"])),
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
            }
            if "local_input_ids" in s:
                item.update({
                    "local_input_ids": s["local_input_ids"],
                    "local_is_uop": s["local_is_uop"],
                    "local_uop_fields": s["local_uop_fields"],
                    "local_query_pos": s["local_query_pos"],
                })
            out_samples.append(item)
        self.samples = out_samples
        self.total_samples = len(self.samples)
        return True

    def _cache_meta_compatible(self, cached_meta: dict | None):
        if not isinstance(cached_meta, dict):
            return False, None
        cur = self._cache_meta()
        for key in (
            "jsonl_path",
            "jsonl_size",
            "jsonl_mtime_ns",
            "max_len",
            "tokenizer_len",
            "unk_token_id",
            "max_cores",
            "side_feat_dim",
        ):
            if cached_meta.get(key) != cur.get(key):
                return False, None
        if cached_meta.get("input_mode") != cur.get("input_mode"):
            return False, None
        cached_keys = list(cached_meta.get("pmu_keys") or PMU_KEYS)
        if cached_keys == list(PMU_KEYS):
            return True, None
        if all(k in cached_keys for k in PMU_KEYS):
            return True, [cached_keys.index(k) for k in PMU_KEYS]
        return False, None

    def _select_label_columns(self, label):
        if self._cache_label_idx is None:
            return label
        idx = self._cache_label_idx
        return [[row[i] for i in idx] for row in label]

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
        if self.mode == "tensor_sharded":
            self._loaded_tensor_shard = blob
            self._loaded_samples = []
        else:
            self._loaded_samples = blob["samples"]
            self._loaded_tensor_shard = None
        self._loaded_shard_idx = shard_idx

    def _tensor_sample(self, shard: dict, local_idx: int) -> dict:
        ids0 = int(shard["ids_offsets"][local_idx])
        ids1 = int(shard["ids_offsets"][local_idx + 1])
        nc = int(shard["n_core"][local_idx])
        item = {
            "ids": shard["ids_flat"][ids0:ids1].tolist(),
            "qpos": shard["qpos"][local_idx, :nc].tolist(),
            "local_pos": shard.get(
                "local_pos", shard["qpos"])[local_idx, :nc].tolist(),
            "label": shard["label"][local_idx, :nc].tolist(),
            "n_core": nc,
            "instr_retired": shard["instr_retired"][local_idx, :nc].tolist(),
            "uops": shard["uops"][local_idx, :nc].tolist(),
            "t_start_rel": shard["t_start_rel"][local_idx, :nc].tolist(),
            "is_uop": shard["is_uop_flat"][ids0:ids1].tolist(),
            "uop_fields": shard["uop_fields_flat"][ids0:ids1].tolist(),
            "side_feats": shard["side_feats"][local_idx, :nc].tolist(),
            "denoms": shard["denoms"][local_idx, :nc].tolist(),
        }
        if "local_core_offsets" in shard:
            core0 = int(shard["local_core_offsets"][local_idx])
            local_input_ids = []
            local_is_uop = []
            local_uop_fields = []
            for ci in range(nc):
                seq_idx = core0 + ci
                li0 = int(shard["local_ids_offsets"][seq_idx])
                li1 = int(shard["local_ids_offsets"][seq_idx + 1])
                local_input_ids.append(
                    shard["local_ids_flat"][li0:li1].tolist())
                local_is_uop.append(
                    shard["local_is_uop_flat"][li0:li1].tolist())
                local_uop_fields.append(
                    shard["local_uop_fields_flat"][li0:li1].tolist())
            item.update({
                "local_input_ids": local_input_ids,
                "local_is_uop": local_is_uop,
                "local_uop_fields": local_uop_fields,
                "local_query_pos": (
                    shard["local_query_pos"][local_idx, :nc].tolist()
                ),
            })
        return item

    def __len__(self):
        return self.total_samples

    def __getitem__(self, i):
        if self.mode in {"sharded", "tensor_sharded"}:
            shard_idx = bisect.bisect_right(self._cum_counts, i)
            shard_start = 0 if shard_idx == 0 else self._cum_counts[shard_idx - 1]
            self._ensure_shard_loaded(shard_idx)
            local_idx = i - shard_start
            if self.mode == "tensor_sharded":
                assert self._loaded_tensor_shard is not None
                s = self._tensor_sample(self._loaded_tensor_shard, local_idx)
            else:
                s = self._loaded_samples[local_idx]
        else:
            s = self.samples[i]
        item = {
            "ids": s["ids"],
            "qpos": s["qpos"],
            "local_pos": s.get("local_pos", s["qpos"]),
            "label": self._select_label_columns(s["label"]),  # [n_core,K]
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
        if "local_input_ids" in s:
            item.update({
                "local_input_ids": s["local_input_ids"],
                "local_is_uop": s["local_is_uop"],
                "local_uop_fields": s["local_uop_fields"],
                "local_query_pos": s["local_query_pos"],
            })
        return item


def prepare_dataset_cache(jsonl_path: str, hf_tokenizer, max_len: int = 8192,
                          max_cores: int = tk.MAX_CORES,
                          cache_path: str | None = None,
                          input_mode: str = INPUT_MODE_GLOBAL) -> str:
    ds = WindowDataset(
        jsonl_path,
        hf_tokenizer,
        max_len=max_len,
        max_cores=max_cores,
        cache_path=cache_path,
        require_cache=False,
        input_mode=input_mode,
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
        local_pos = torch.zeros((B, max_nc), dtype=torch.long)
        label = torch.zeros((B, max_nc, K), dtype=torch.float32)
        core_mask = torch.zeros((B, max_nc), dtype=torch.float32)
        instr = torch.ones((B, max_nc), dtype=torch.float32)
        uops = torch.ones((B, max_nc), dtype=torch.float32)
        t_start = torch.zeros((B, max_nc), dtype=torch.float32)
        side = torch.zeros((B, max_nc, len(tk.SIDE_FEATURE_KEYS)),
                           dtype=torch.float32)
        denoms = torch.zeros((B, max_nc, len(DENOM_KEYS)),
                             dtype=torch.float32)
        has_local = all("local_input_ids" in b for b in batch)
        if has_local:
            max_local_L = max(
                len(seq) for b in batch for seq in b["local_input_ids"])
            local_input_ids = torch.full(
                (B, max_nc, max_local_L), pad_id, dtype=torch.long)
            local_attn = torch.zeros(
                (B, max_nc, max_local_L), dtype=torch.long)
            local_is_uop = torch.zeros(
                (B, max_nc, max_local_L), dtype=torch.bool)
            local_uop_fields = torch.zeros(
                (B, max_nc, max_local_L, 6), dtype=torch.long)
            local_query_pos = torch.zeros((B, max_nc), dtype=torch.long)
        else:
            local_input_ids = None
            local_attn = None
            local_is_uop = None
            local_uop_fields = None
            local_query_pos = None
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
                local_pos[bi, ci] = b.get("local_pos", b["qpos"])[ci]
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
                if has_local:
                    l_ids = b["local_input_ids"][ci]
                    l_len = len(l_ids)
                    assert local_input_ids is not None
                    assert local_attn is not None
                    assert local_is_uop is not None
                    assert local_uop_fields is not None
                    assert local_query_pos is not None
                    local_input_ids[bi, ci, :l_len] = torch.tensor(
                        l_ids, dtype=torch.long)
                    local_attn[bi, ci, :l_len] = 1
                    local_is_uop[bi, ci, :l_len] = torch.tensor(
                        b["local_is_uop"][ci], dtype=torch.bool)
                    local_uop_fields[bi, ci, :l_len] = torch.tensor(
                        b["local_uop_fields"][ci], dtype=torch.long)
                    local_query_pos[bi, ci] = int(b["local_query_pos"][ci])
        out = {
            "input_ids": input_ids,
            "attention_mask": attn,
            "is_uop": is_uop,
            "uop_fields": uop_fields,
            "query_pos": qpos,
            "local_pos": local_pos,
            "label": label,
            "core_mask": core_mask,
            "instr_retired": instr,
            "uops": uops,
            "t_start": t_start,
            "side_feats": side,
            "denoms": denoms,
        }
        if has_local:
            out.update({
                "local_input_ids": local_input_ids,
                "local_attention_mask": local_attn,
                "local_is_uop": local_is_uop,
                "local_uop_fields": local_uop_fields,
                "local_query_pos": local_query_pos,
            })
        return out
    return collate
