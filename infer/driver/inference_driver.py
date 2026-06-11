#!/usr/bin/env python3
"""Multi-core deploy-side inference driver.

Inputs are strict functional traces (A-subset only). The driver runs the
quantum-based parallel coherence loop (see
``tao_cpu_sim/docs/04-quantum-parallel-coherence.md``):

    Phase 1a  per core, speculatively probe up to K_max upcoming µops
              (oracle + window features) without advancing fetch_clock.
    Phase 1b  merge all fresh probes into a single model forward.
    Phase 1c  per core, walk the predictions and commit until either the
              quantum deadline (fetch_clock_base + Δt) is crossed (after
              committing >= 1) or feat_buf is exhausted; uncommitted probes
              survive as ``unconsumed`` and are re-forwarded next quantum.
    Phase 2   coordinator reconcile (Phase A: stub, just sort by
              (t, core_id, seq) so jsonl output is deterministic).
    Phase 3   batch-flush jsonl + advance per-core fetch_clock_base.

Δt == 1 also goes through this loop (each quantum at most one commit per
core), so the legacy global-heapq path is no longer instantiated.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pyarrow.parquet as pq

# E.4: orjson 比 stdlib json 在我们的 payload 上快约 4-5x，且字节序与
# json.dumps(separators=(",", ":")) 完全一致（已校验）。orjson.dumps 直接
# 返回 bytes，配合 bytearray 批写一次 syscall flush，避免 200K 次 fout.write。
try:
    import orjson  # type: ignore
    _ORJSON = True
except ImportError:  # pragma: no cover
    orjson = None
    _ORJSON = False

THIS_DIR = Path(__file__).resolve().parent
ROOT = THIS_DIR.parent
sys.path.insert(0, str(ROOT))

from driver.ref_sim_client import (  # noqa: E402
    LocalPybindBackend,
    PybindBackend,
    make_timing_functional_backend,
)
from driver.reference_clock import ReferenceClock  # noqa: E402
from driver.windowed_features import OnlineWindowFeatures  # noqa: E402


D_ZERO = {
    "mesi_before": 0, "coh_oracle": 0, "sharer_bucket": 0, "owner_dist": 0,
    "dirty_owner": 0, "path_class": 0, "inval_fanout": 0,
    "same_line_recent": 0, "oracle_source": 1,
    "d_mshr_depth": 0, "dtlb_hit": 0, "d_walker_levels": 0,
    "d_walker_dram_misses": 0, "d_bank_id": 0,
    "d_llc_set_residency": 0, "d_llc_set_lru_pos": 0,
}


def hash_addr_bucket_scalar(x: int, n_bucket: int = 16) -> int:
    a = (int(x) & ((1 << 64) - 1)) >> 6
    a ^= a >> 30
    a = (a * 0xbf58476d1ce4e5b9) & ((1 << 64) - 1)
    a ^= a >> 27
    a = (a * 0x94d049bb133111eb) & ((1 << 64) - 1)
    a ^= a >> 31
    return int(a % n_bucket)


def load_jsonl(path: str) -> List[Dict]:
    rows = []
    with open(path) as f:
        for line in f:
            if line.startswith("{"):
                rows.append(json.loads(line))
    rows.sort(key=lambda r: (int(r["thread_id"]), int(r["micro_seq"])))
    return rows


def load_parquet(path: str) -> List[Dict]:
    rows = pq.read_table(path).to_pylist()
    rows.sort(key=lambda r: (int(r["thread_id"]), int(r["micro_seq"])))
    return rows


def load_rows(path: str) -> List[Dict]:
    if path.endswith(".parquet"):
        return load_parquet(path)
    if path.endswith(".jsonl"):
        return load_jsonl(path)
    raise SystemExit(f"unsupported trace format: {path}")


# ---------------------------------------------------------------------------
# E.5: Functional rows 改 SoA（structure-of-arrays）。
#
# 改造前：load 把 parquet 物化为 List[Dict]，phase1a/1c 每行 r["macro_pc"] /
#   r.get("paddr", 0) 都是 Python hash + 类型转换，热路径上每行 ≥10 次。
# 改造后：load 一次性把所需 14 列保留为 numpy ndarray（zero-copy），phase1a 用
#   切片+ndarray 直接构造 batch_probe 的输入；phase1c 通过整数 idx 索引列。
# 仍保留稀疏字段（producer_dists/producer_classes 等模型侧用的）的 List[Dict]
# 视图作为 row_dicts，仅在 ckpt 模式 to_model_row / label_prediction 路径用到，
# mock/label-driven 主路径完全不再 touch dict。
#
# RowsSoA 字段命名与 parquet 列一致；数据类型尽量压缩（uint8 flags / uint16
# size），让 cache footprint 紧凑。
@dataclass
class RowsSoA:
    n: int
    # 必需热字段（phase1a / phase1c 都会读）：
    core_id: np.ndarray        # int32
    thread_id: np.ndarray      # int32
    micro_seq: np.ndarray      # int64
    macro_pc: np.ndarray       # uint64
    paddr: np.ndarray          # uint64
    cacheline_paddr: np.ndarray  # uint64
    size: np.ndarray           # uint16
    is_load: np.ndarray        # uint8 (0/1)
    is_store: np.ndarray       # uint8
    is_atomic: np.ndarray      # uint8
    is_branch: np.ndarray      # uint8
    is_microop: np.ndarray     # uint8
    is_last_microop: np.ndarray  # uint8
    # 仅 ckpt 路径用到，保留 List[Dict] 视图（mock/label 主路径不 touch）。
    row_dicts: List[Dict] = field(default_factory=list)


def _empty_uint(n: int, dtype) -> np.ndarray:
    return np.zeros(n, dtype=dtype)


def _table_col(table, name, dtype):
    """从 pyarrow.Table 拿一列；缺失列返回零数组。"""
    if name in table.column_names:
        return table.column(name).to_numpy(zero_copy_only=False).astype(dtype, copy=False)
    return _empty_uint(table.num_rows, dtype)


def load_parquet_soa(path: str) -> RowsSoA:
    """E.5: parquet → SoA，零 dict 物化（除 row_dicts 备份）。

    排序保持与 [load_parquet](file:./inference_driver.py#L86)
    完全一致：(thread_id, micro_seq)。先用 numpy.lexsort 算出顺序，然后所有
    列一起 reorder，避免对每行做 Python 端排序。
    """
    table = pq.read_table(path)
    n = table.num_rows
    thread_id = _table_col(table, "thread_id", np.int32)
    micro_seq = _table_col(table, "micro_seq", np.int64)
    # numpy.lexsort 的 keys 顺序：最后一个是主 key。
    order = np.lexsort((micro_seq, thread_id))

    def col(name, dtype):
        return _table_col(table, name, dtype)[order]

    soa = RowsSoA(
        n=n,
        core_id=col("core_id", np.int32),
        thread_id=thread_id[order],
        micro_seq=micro_seq[order],
        macro_pc=col("macro_pc", np.uint64),
        paddr=col("paddr", np.uint64),
        cacheline_paddr=col("cacheline_paddr", np.uint64),
        size=col("size", np.uint16),
        is_load=col("is_load", np.uint8),
        is_store=col("is_store", np.uint8),
        is_atomic=col("is_atomic", np.uint8),
        is_branch=col("is_branch", np.uint8),
        is_microop=col("is_microop", np.uint8),
        is_last_microop=col("is_last_microop", np.uint8),
        row_dicts=[],  # 懒加载：仅 ckpt 路径触发 _materialize_row_dicts
    )
    return soa


def _materialize_row_dicts(soa: RowsSoA, table_path: str) -> None:
    """ckpt 模式才需要的稀疏字段（producer_dists 等）。Mock/label 路径不调。"""
    if soa.row_dicts:
        return
    rows = pq.read_table(table_path).to_pylist()
    rows.sort(key=lambda r: (int(r["thread_id"]), int(r["micro_seq"])))
    soa.row_dicts = rows


# ---------------------------------------------------------------------------
# FASTENC（方案 a）：向量化 enc。把每行最终送入模型的 61 个特征列预先编码成
# per-core int64 矩阵 [N, 61]，phase1b predict 只做窗口切片 + view，绕过
#   _materialize_row_dicts / to_model_row / _HistSoA / feature_row dict。
#
# 列布局：前 29 列为「静态列」（load 期一次性从 parquet 向量化算好，含 bool /
# n_src/n_dst/size / d0..d3 桶化 / pc0..pc3 clip / 4 个地址 hash bucket）；
# 后 32 列为「动态列」（oracle 16 + win 12 + group 4），由 phase1a 逐行写入。
# 动态列连续放在末尾，phase1a 用一次 block 行赋值，避免逐 cell setitem。
#
# 全部通过 env TAO_INFER_FASTENC 灰度；未开启时该路径完全不触发，回退 baseline。
# ---------------------------------------------------------------------------
def _fastenc_layout():
    """返回 FASTENC 的列布局元数据（与 _build_batch_soa 输出键完全等价）。"""
    from ml.dataset import (SCALAR_BOOL, SCALAR_SMALL_INT, SCALAR_P1C,
                            SCALAR_V10_3_B, SCALAR_V10_3_C)
    bool_keys = list(SCALAR_BOOL)                       # 14，静态
    si = list(SCALAR_SMALL_INT)
    static_si = si[:3]                                  # n_src, n_dst, size
    oracle_keys = si[3:]                                # 16，动态
    win_keys = list(SCALAR_P1C) + list(SCALAR_V10_3_B) + list(SCALAR_V10_3_C)  # 12，动态
    group_keys = ['is_macro_head', 'uop_pos_in_macro',
                  'i_group_head', 'i_group_pos']        # 4，动态（顺序对应 group_features_idx 返回）
    d_keys = ['d0', 'd1', 'd2', 'd3']
    pc_keys = ['pc0', 'pc1', 'pc2', 'pc3']
    addr_bucket_keys = ['vaddr_bucket', 'paddr_bucket',
                        'cline_bucket', 'cline_p_bucket']
    static_keys = (bool_keys + static_si + d_keys + pc_keys + addr_bucket_keys)
    dynamic_keys = oracle_keys + win_keys + group_keys
    keys = static_keys + dynamic_keys
    return {
        "bool_keys": bool_keys,
        "static_si": static_si,
        "oracle_keys": oracle_keys,
        "win_keys": win_keys,
        "group_keys": group_keys,
        "d_keys": d_keys,
        "pc_keys": pc_keys,
        "addr_bucket_keys": addr_bucket_keys,
        "static_keys": static_keys,
        "dynamic_keys": dynamic_keys,
        "keys": keys,
        "dyn_lo": len(static_keys),
        "dyn_hi": len(keys),
    }


class _CoreEnc:
    """per-core 预编码矩阵。mat[idx] 与 RowsSoA 行 idx 一一对齐（同 lexsort 序）。"""
    __slots__ = ("mat", "layout", "dyn_lo", "dyn_hi", "n")

    def __init__(self, mat, layout):
        self.mat = mat
        self.layout = layout
        self.dyn_lo = layout["dyn_lo"]
        self.dyn_hi = layout["dyn_hi"]
        self.n = mat.shape[0]


def build_core_enc(table_path: str, layout: Dict) -> _CoreEnc:
    """从 functional parquet 向量化构建静态列；动态列留 0，由 phase1a 写。

    排序口径与 load_parquet_soa 完全一致：lexsort((micro_seq, thread_id))，
    保证 mat[idx] 与 soa 行 idx 对齐。
    """
    from ml.dataset import hash_addr_bucket, bucketize_dist
    table = pq.read_table(table_path)
    n = table.num_rows
    thread_id = _table_col(table, "thread_id", np.int32)
    micro_seq = _table_col(table, "micro_seq", np.int64)
    order = np.lexsort((micro_seq, thread_id))

    nfeat = len(layout["keys"])
    mat = np.zeros((n, nfeat), dtype=np.int64)
    col = {k: i for i, k in enumerate(layout["keys"])}

    def scol(name, dtype):
        return _table_col(table, name, dtype)[order]

    # bool 14 + n_src/n_dst/size
    for name in layout["bool_keys"]:
        mat[:, col[name]] = scol(name, np.int64)
    for name in layout["static_si"]:
        mat[:, col[name]] = scol(name, np.int64)

    # producer_dists / producer_classes（fixed_size_list[4]）-> d0..d3 / pc0..pc3
    def _list_col(name):
        c = table.column(name).combine_chunks()
        arr = np.asarray(c.values.to_numpy(zero_copy_only=False)).reshape(n, 4)
        return arr[order]

    pds = _list_col("producer_dists").astype(np.int64)      # to_model_row: d{i}=int(pds[i])
    pcs = _list_col("producer_classes").astype(np.int64)    # pc{i}=int(pcs[i])
    d_bucketed = bucketize_dist(pds)                          # [n,4]
    for i, name in enumerate(layout["d_keys"]):
        mat[:, col[name]] = d_bucketed[:, i]
    pc_clipped = np.clip(np.where(pcs == 255, 7, pcs), 0, 7)
    for i, name in enumerate(layout["pc_keys"]):
        mat[:, col[name]] = pc_clipped[:, i]

    # 地址 hash bucket。to_model_row: cline=cacheline_addr, cline_p=cacheline_paddr。
    addr_src = {"vaddr_bucket": "vaddr", "paddr_bucket": "paddr",
                "cline_bucket": "cacheline_addr", "cline_p_bucket": "cacheline_paddr"}
    for name in layout["addr_bucket_keys"]:
        a = scol(addr_src[name], np.uint64)
        mat[:, col[name]] = hash_addr_bucket(a).astype(np.int64)

    return _CoreEnc(mat, layout)


def load_rows_soa(path: str) -> RowsSoA:
    if path.endswith(".parquet"):
        return load_parquet_soa(path)
    if path.endswith(".jsonl"):
        rows = load_jsonl(path)
        return _rows_to_soa(rows)
    raise SystemExit(f"unsupported trace format: {path}")


def _rows_to_soa(rows: List[Dict]) -> RowsSoA:
    """jsonl 兜底路径：List[Dict] → SoA。"""
    n = len(rows)
    out = RowsSoA(
        n=n,
        core_id=np.zeros(n, np.int32),
        thread_id=np.zeros(n, np.int32),
        micro_seq=np.zeros(n, np.int64),
        macro_pc=np.zeros(n, np.uint64),
        paddr=np.zeros(n, np.uint64),
        cacheline_paddr=np.zeros(n, np.uint64),
        size=np.zeros(n, np.uint16),
        is_load=np.zeros(n, np.uint8),
        is_store=np.zeros(n, np.uint8),
        is_atomic=np.zeros(n, np.uint8),
        is_branch=np.zeros(n, np.uint8),
        is_microop=np.zeros(n, np.uint8),
        is_last_microop=np.zeros(n, np.uint8),
        row_dicts=rows,
    )
    for i, r in enumerate(rows):
        out.core_id[i] = int(r.get("core_id", 0))
        out.thread_id[i] = int(r.get("thread_id", 0))
        out.micro_seq[i] = int(r.get("micro_seq", 0))
        out.macro_pc[i] = int(r.get("macro_pc", 0))
        out.paddr[i] = int(r.get("paddr", 0))
        out.cacheline_paddr[i] = int(r.get("cacheline_paddr", 0))
        out.size[i] = int(r.get("size", 0))
        out.is_load[i] = int(r.get("is_load", 0))
        out.is_store[i] = int(r.get("is_store", 0))
        out.is_atomic[i] = int(r.get("is_atomic", 0))
        out.is_branch[i] = int(r.get("is_branch", 0))
        out.is_microop[i] = int(r.get("is_microop", 0))
        out.is_last_microop[i] = int(r.get("is_last_microop", 0))
    return out


def load_functional_dir_soa(path: str) -> Dict[int, Tuple[RowsSoA, str]]:
    out: Dict[int, Tuple[RowsSoA, str]] = {}
    for fp in sorted(glob.glob(os.path.join(path, "functional.core*.*"))):
        m = re.search(r"core(\d+)", os.path.basename(fp))
        if not m:
            continue
        cid = int(m.group(1))
        out[cid] = (load_rows_soa(fp), fp)
    if not out:
        raise SystemExit(f"no functional.core* files under {path}")
    return out



def load_functional_dir(path: str) -> Dict[int, List[Dict]]:
    out = {}
    for fp in sorted(glob.glob(os.path.join(path, "functional.core*.*"))):
        m = re.search(r"core(\d+)", os.path.basename(fp))
        if not m:
            continue
        cid = int(m.group(1))
        out[cid] = load_rows(fp)
    if not out:
        raise SystemExit(f"no functional.core* files under {path}")
    return out


def load_labels(label_dir: str) -> Dict[Tuple[int, int, int], Dict]:
    labels = {}
    for fp in sorted(glob.glob(os.path.join(label_dir, "labels.core*.*"))):
        for r in load_rows(fp):
            key = (int(r["core_id"]), int(r["thread_id"]),
                   int(r["micro_seq"]))
            labels[key] = r
    return labels


def parse_stats_num_insts(stats_path: Optional[str]) -> Dict[int, int]:
    if not stats_path:
        return {}
    pat = re.compile(r"board\.processor\.cores(\d+)\.core\.commitStats0\.numInsts\s+(\d+)")
    out = {}
    with open(stats_path) as f:
        for line in f:
            m = pat.match(line)
            if m:
                out[int(m.group(1))] = int(m.group(2))  # final dump wins
    return out


def is_macro_counted(row: Dict) -> bool:
    return int(row.get("is_microop", 0)) == 0 or int(row.get("is_last_microop", 0)) == 1


def is_macro_counted_idx(soa: RowsSoA, i: int) -> bool:
    """E.5: SoA-aware 版本，避开 dict.get + int() 双重 Python 调用。"""
    return soa.is_microop[i] == 0 or soa.is_last_microop[i] == 1


@dataclass
class PendingProbe:
    """One speculatively-probed µop awaiting Phase 1c commit.

    E.5: row dict 退场，改成 (soa, idx) 引用 + fields_tuple（已含 phase1a 用到
    的 10 字段 POD，phase1c 复用追加 d_bank_id 形成 11 元 tuple）。feature_row
    仅在 ckpt 路径懒构造。
    """
    cid: int
    idx: int                         # SoA 行号，phase1c 通过这个索引列
    d_attrs: Dict                    # batch_probe 返回的合并 dict（含 16 oracle + 12 win + 8 i_*）
    d_bank_id: int = 0               # F.1: POD probe 只需要这个 scalar
    fields_tuple: Tuple = ()
    preds: Optional[Tuple[float, float, float]] = None
    # ckpt 模式才物化的 dict（mock/label 路径恒为 None，节省 600K 次 dict 分配）。
    feature_row: Optional[Dict] = None


@dataclass
class PendingEvent:
    """Phase 2 reconcile element. payload is json dict or 56B bin record."""
    t: float
    core_id: int
    seq: int
    payload: Any


@dataclass
class CoreState:
    soa: RowsSoA                # E.5: 改 SoA；旧 rows: List[Dict] 退场
    soa_path: str = ""          # ckpt 模式按需 _materialize_row_dicts 用
    idx: int = 0                # next µop awaiting Phase 1c commit
    next_probe_idx: int = 0     # next µop awaiting Phase 1a probe (>= idx)
    clock: ReferenceClock = field(default_factory=ReferenceClock)
    win: OnlineWindowFeatures = field(default_factory=OnlineWindowFeatures)
    prev_i_cl: Optional[int] = None
    i_group_pos: int = 0
    prev_macro_pc: Optional[int] = None
    prev_is_micro: int = 0
    prev_is_last_micro: int = 0
    uop_pos_in_macro: int = 0
    cached_i_attrs: Dict = field(default_factory=dict)
    enc_history: List[Dict] = field(default_factory=list)
    # FASTENC (方案 a)：per-core 预编码矩阵。默认 None（走 baseline dict 路径）。
    enc_fast: Optional[Any] = None
    macro_count: int = 0
    uop_count: int = 0
    first_fetch_tick: Optional[int] = None
    prev_fetch_tick: Optional[int] = None
    max_abs_fetch_diff: float = 0.0
    max_truth_ready_rel: float = 0.0
    # Quantum state
    feat_buf: List[PendingProbe] = field(default_factory=list)
    unconsumed: List[PendingProbe] = field(default_factory=list)


def group_features_idx(soa: RowsSoA, i: int, st: CoreState) -> Tuple[int, int, int, int]:
    """SCHEMA.md V10.3-ma16 口径：返回 (is_macro_head, uop_pos_in_macro,
    i_group_head, i_group_pos)。i_group_bkt 已剔除。

    is_macro_head 与 train derive_sequence_features 等价：
        prev_macro_pc is None
        or prev_is_micro == 0
        or prev_is_last_micro == 1
        or cur_macro_pc != prev_macro_pc
    """
    macro_pc = int(soa.macro_pc[i])
    is_micro = int(soa.is_microop[i])
    is_last_micro = int(soa.is_last_microop[i])
    macro_head = 1 if (st.prev_macro_pc is None
                       or st.prev_is_micro == 0
                       or st.prev_is_last_micro == 1
                       or macro_pc != st.prev_macro_pc) else 0
    if macro_head:
        st.uop_pos_in_macro = 0
    else:
        st.uop_pos_in_macro = min(st.uop_pos_in_macro + 1, 15)

    i_cl = macro_pc >> 6
    i_group_head = 1 if (st.prev_i_cl is None or st.prev_i_cl != i_cl) else 0
    if i_group_head:
        st.i_group_pos = 0
    else:
        st.i_group_pos = min(st.i_group_pos + 1, 15)

    st.prev_macro_pc = macro_pc
    st.prev_is_micro = is_micro
    st.prev_is_last_micro = is_last_micro
    st.prev_i_cl = i_cl
    return macro_head, st.uop_pos_in_macro, i_group_head, st.i_group_pos


def group_features(row: Dict, st: CoreState) -> Dict:
    macro_pc = int(row["macro_pc"])
    is_micro = int(row.get("is_microop", 0))
    is_last_micro = int(row.get("is_last_microop", 0))
    macro_head = 1 if (st.prev_macro_pc is None
                       or st.prev_is_micro == 0
                       or st.prev_is_last_micro == 1
                       or macro_pc != st.prev_macro_pc) else 0
    if macro_head:
        st.uop_pos_in_macro = 0
    else:
        st.uop_pos_in_macro = min(st.uop_pos_in_macro + 1, 15)
    i_cl = macro_pc >> 6
    i_group_head = 1 if (st.prev_i_cl is None or st.prev_i_cl != i_cl) else 0
    if i_group_head:
        st.i_group_pos = 0
    else:
        st.i_group_pos = min(st.i_group_pos + 1, 15)
    st.prev_macro_pc = macro_pc
    st.prev_is_micro = is_micro
    st.prev_is_last_micro = is_last_micro
    st.prev_i_cl = i_cl
    return {
        "is_macro_head": macro_head,
        "uop_pos_in_macro": st.uop_pos_in_macro,
        "i_group_head": i_group_head,
        "i_group_pos": st.i_group_pos,
    }


def label_prediction_idx(soa: RowsSoA, i: int, labels: Dict, st: CoreState) -> Tuple[float, float, float]:
    key = (int(soa.core_id[i]), int(soa.thread_id[i]), int(soa.micro_seq[i]))
    lab = labels[key]
    ft = int(lab["fetch_tick"])
    rt = int(lab["ready_tick"])
    if st.first_fetch_tick is None:
        st.first_fetch_tick = ft
        st.prev_fetch_tick = ft
    st.max_truth_ready_rel = max(st.max_truth_ready_rel,
                                 float(rt - int(st.first_fetch_tick)))
    fl = ft - int(st.prev_fetch_tick)
    el = rt - ft
    st.prev_fetch_tick = ft
    return float(max(fl, 0)), float(max(el, 0)), float(lab.get("mispredicted", 0))


def label_prediction(row: Dict, labels: Dict, st: CoreState) -> Tuple[float, float, float]:
    key = (int(row["core_id"]), int(row["thread_id"]), int(row["micro_seq"]))
    lab = labels[key]
    ft = int(lab["fetch_tick"])
    rt = int(lab["ready_tick"])
    if st.first_fetch_tick is None:
        st.first_fetch_tick = ft
        st.prev_fetch_tick = ft
    st.max_truth_ready_rel = max(st.max_truth_ready_rel,
                                 float(rt - int(st.first_fetch_tick)))
    fl = ft - int(st.prev_fetch_tick)
    el = rt - ft
    st.prev_fetch_tick = ft
    return float(max(fl, 0)), float(max(el, 0)), float(lab.get("mispredicted", 0))


def mock_prediction(_: Dict) -> Tuple[float, float, float]:
    return 1.0, 1.0, 0.0


def to_model_row(r: Dict) -> Dict:
    out = dict(r)
    pds = r.get("producer_dists", [0, 0, 0, 0])
    pcs = r.get("producer_classes", [255, 255, 255, 255])
    for i in range(4):
        out[f"d{i}"] = int(pds[i]) if i < len(pds) else 0
        out[f"pc{i}"] = int(pcs[i]) if i < len(pcs) else 255
    out["cline"] = int(r.get("cacheline_addr", 0))
    out["cline_p"] = int(r.get("cacheline_paddr", 0))
    return out




class _HistSoA:
    """G.1 per-history SoA mirror。每行 dict 的字段同步落到字段级 ndarray，
    capacity 按 1.5x 扩容；feats_to_window 的 ctx_len 切片直接 numpy slice。

    A 优化：i32/d/pc/addr 改为 [cap, n_keys] 矩阵存储，让 _build_batch_soa
    g2 段从"B × n_keys 次 Python slice"降到"B × 4 次 slice"。"""
    __slots__ = ("n", "cap",
                 "i32_mat", "d_mat", "pc_mat", "addr_mat",
                 "_i32_keys", "_d_keys", "_pc_keys", "_addr_keys")

    def __init__(self, i32_keys, d_keys, pc_keys, addr_keys, cap=256):
        import numpy as np
        self._i32_keys = list(i32_keys)
        self._d_keys = list(d_keys)
        self._pc_keys = list(pc_keys)
        self._addr_keys = list(addr_keys)
        self.n = 0
        self.cap = cap
        self.i32_mat = np.zeros((cap, len(self._i32_keys)), dtype=np.int32)
        self.d_mat = np.zeros((cap, len(self._d_keys)), dtype=np.int64)
        self.pc_mat = np.full((cap, len(self._pc_keys)), 255, dtype=np.int32)
        self.addr_mat = np.zeros((cap, len(self._addr_keys)), dtype=np.uint64)

    def _grow(self, need):
        import numpy as np
        new_cap = max(self.cap * 2, need)
        n = self.n
        new_i32 = np.zeros((new_cap, len(self._i32_keys)), dtype=np.int32)
        new_i32[:n] = self.i32_mat[:n]
        self.i32_mat = new_i32
        new_d = np.zeros((new_cap, len(self._d_keys)), dtype=np.int64)
        new_d[:n] = self.d_mat[:n]
        self.d_mat = new_d
        new_pc = np.full((new_cap, len(self._pc_keys)), 255, dtype=np.int32)
        new_pc[:n] = self.pc_mat[:n]
        self.pc_mat = new_pc
        new_addr = np.zeros((new_cap, len(self._addr_keys)), dtype=np.uint64)
        new_addr[:n] = self.addr_mat[:n]
        self.addr_mat = new_addr
        self.cap = new_cap

    def append(self, enc: Dict):
        if self.n + 1 > self.cap:
            self._grow(self.n + 1)
        i = self.n
        # 整行 list 赋值：4 次 numpy bulk setitem，比逐 cell view setitem 快 ~40%
        self.i32_mat[i, :]  = [int(enc.get(k, 0))   for k in self._i32_keys]
        self.d_mat[i, :]    = [int(enc.get(k, 0))   for k in self._d_keys]
        self.pc_mat[i, :]   = [int(enc.get(k, 255)) for k in self._pc_keys]
        mask64 = (1 << 64) - 1
        self.addr_mat[i, :] = [int(enc.get(k, 0)) & mask64 for k in self._addr_keys]
        self.n += 1


class ModelPredictor:
    def __init__(self, ckpt_path: str,
                 fetch_gate_mode: str = "hard",
                 fetch_gate_temp: float = 1.0):
        import numpy as np
        import torch
        from ml.model import TaoConfig, TaoCoreTransformer

        self.np = np
        self.torch = torch
        self.ckpt_path = os.path.abspath(ckpt_path)
        self.fetch_gate_mode = "hard"
        self.fetch_gate_temp = 1.0
        self.set_fetch_gate(fetch_gate_mode, fetch_gate_temp)
        self._collate_batch = None
        self._feats_to_window = None
        device_kind = os.environ.get("TAO_INFER_DEVICE", "cpu").strip().lower()
        self.device = torch.device("cpu")
        self.use_cuda_amp = False
        self.device_ids: List[int] = []
        if device_kind == "cuda":
            if not torch.cuda.is_available():
                raise SystemExit("TAO_INFER_DEVICE=cuda but CUDA is not available")
            raw_ids = os.environ.get("TAO_INFER_CUDA_DEVICES", "").strip()
            if raw_ids:
                self.device_ids = [int(x) for x in raw_ids.split(",") if x.strip()]
            else:
                self.device_ids = list(range(torch.cuda.device_count()))
            if not self.device_ids:
                raise SystemExit("no CUDA devices selected for inference")
            self.device = torch.device(f"cuda:{self.device_ids[0]}")
            self.use_cuda_amp = True
            if hasattr(torch, "set_float32_matmul_precision"):
                torch.set_float32_matmul_precision("high")
        ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
        cfg_dict = ck["cfg"]
        cfg = TaoConfig(**{k: v for k, v in cfg_dict.items()
                           if k in TaoConfig.__dataclass_fields__})
        self.cfg = cfg
        self.model = TaoCoreTransformer(cfg)
        self.model.load_state_dict(ck["model"])
        self.model.to(self.device)
        # 维度 C：手动 stream 分片
        # TAO_INFER_GPU_SHARDS=N（N>=2）打开多卡分片：N 个 model 副本 + N 个 stream
        # 与 DataParallel 互斥（设了 SHARDS 则不再走 DataParallel 路径）
        self.gpu_shards = 0
        self.shard_models: List = []
        self.shard_streams: List = []
        self.shard_devices: List = []
        try:
            n_shards = int(os.environ.get("TAO_INFER_GPU_SHARDS", "0") or 0)
        except Exception:
            n_shards = 0
        if n_shards >= 2 and self.device.type == "cuda":
            avail = list(self.device_ids)
            if len(avail) < n_shards:
                # 不够的话从 cuda.device_count() 补
                full = list(range(torch.cuda.device_count()))
                for d in full:
                    if d not in avail:
                        avail.append(d)
                    if len(avail) >= n_shards:
                        break
            if len(avail) < n_shards:
                raise SystemExit(
                    f"TAO_INFER_GPU_SHARDS={n_shards} but only {len(avail)} CUDA devices available")
            self.shard_devices = [torch.device(f"cuda:{avail[i]}") for i in range(n_shards)]
            # 第 0 个直接复用主 model（已 to(self.device)）
            if self.shard_devices[0] != self.device:
                self.model = self.model.to(self.shard_devices[0])
                self.device = self.shard_devices[0]
            self.shard_models.append(self.model)
            for i in range(1, n_shards):
                m = TaoCoreTransformer(cfg)
                m.load_state_dict(ck["model"])
                m.to(self.shard_devices[i])
                m.eval()
                self.shard_models.append(m)
            self.shard_streams = [torch.cuda.Stream(device=d) for d in self.shard_devices]
            self.gpu_shards = n_shards
        elif len(self.device_ids) > 1:
            self.model = torch.nn.DataParallel(
                self.model, device_ids=self.device_ids, output_device=self.device_ids[0]
            )
        self.model.eval()

    def set_fetch_gate(self, mode: str, temp: float = 1.0) -> None:
        mode = str(mode or "hard").strip().lower()
        if mode not in {"hard", "soft", "direct"}:
            raise SystemExit(f"invalid fetch gate mode: {mode}")
        temp = float(temp)
        if temp <= 0.0:
            raise SystemExit("--fetch-gate-temp must be > 0")
        self.fetch_gate_mode = mode
        self.fetch_gate_temp = temp
        self._prof_acc = self._new_prof_acc()
        # G.1: SoA per-history cache。key=id(history)，value=_HistSoA。
        # 当 driver enc_history 列表的 id 第一次被见到时，预先把整段重编码；
        # 之后 predict_batch 内只对每行 incremental encode。
        self._hist_cache: Dict[int, "_HistSoA"] = {}
        self._init_field_specs()

    @staticmethod
    def _new_prof_acc() -> Dict[str, float]:
        return {"n": 0, "B": 0, "enc": 0.0, "coll": 0.0,
                "h2d": 0.0, "fwd": 0.0, "d2h": 0.0,
                "enc_g1": 0.0, "enc_g2": 0.0, "enc_g3": 0.0, "enc_g4": 0.0}

    def reset_runtime_state(self) -> None:
        """清空与单次 job 绑定的缓存/统计，保留已加载模型。"""
        self._hist_cache.clear()
        self._prof_acc = self._new_prof_acc()

    def _init_field_specs(self):
        from ml.dataset import (SCALAR_BOOL, SCALAR_SMALL_INT, SCALAR_P1C,
                                SCALAR_V10_3_B, SCALAR_V10_3_C)
        self._bool_keys = list(SCALAR_BOOL)
        self._si_keys = list(SCALAR_SMALL_INT) + [
            'i_group_head', 'i_group_pos', 'uop_pos_in_macro']
        self._ctx_keys = list(SCALAR_P1C) + list(SCALAR_V10_3_B) + list(SCALAR_V10_3_C)
        # i32 keys = bool + si + ctx + is_macro_head
        self._i32_keys = self._bool_keys + self._si_keys + self._ctx_keys + ['is_macro_head']
        self._d_keys = ['d0', 'd1', 'd2', 'd3']
        self._pc_keys = ['pc0', 'pc1', 'pc2', 'pc3']
        self._addr_keys = ['vaddr', 'paddr', 'cline', 'cline_p']
        # SCHEMA.md / ml.infer.feats_to_window 中：addr 用 hash bucket 后存为 cline_p_bucket 等
        self._addr_bucket_keys = {
            'vaddr': 'vaddr_bucket', 'paddr': 'paddr_bucket',
            'cline': 'cline_bucket', 'cline_p': 'cline_p_bucket'}

    def _build_batch_soa(self, items):
        """G.1 SoA-batched feats_to_window：将 B 个 (history, row) 编码并切窗口
        合并成一次 numpy 矩阵化操作。等价于循环 feats_to_window+collate_batch,
        但跳过 list-of-dict comprehension 与单样本 stack。"""
        np = self.np
        import torch
        from ml.dataset import hash_addr_bucket, bucketize_dist
        ctx_len = self.cfg.context_len
        B = len(items)
        rows_for_valid = []
        prof_enc = bool(int(os.environ.get("TAO_INFER_PROFILE", "0")))
        if prof_enc:
            import time as _t
            tg0 = _t.perf_counter()
        # G.1.1：先把每个 history 推入新 row、并对 history 同步 SoA。
        # 关键：同一个 enc_history 在 items 中可能出现多次（K-max 一次 probe 256 个),
        # soa 对象是共享的，所以必须**append 时立刻记下当前 anchor n**,
        # 否则后面切窗口时所有 b 看到的都是最终 soa.n（= legacy path 的 bug）。
        soas: List["_HistSoA"] = []
        anchors: List[int] = []
        for history, row in items:
            enc = to_model_row(row)
            history.append(enc)
            hid = id(history)
            soa = self._hist_cache.get(hid)
            if soa is None or soa.n != len(history) - 1:
                # 首次遇到、或 history 被外部 mutate；从头重建。
                soa = _HistSoA(self._i32_keys, self._d_keys, self._pc_keys,
                               self._addr_keys)
                for prev in history[:-1]:
                    soa.append(prev)
                self._hist_cache[hid] = soa
            soa.append(enc)
            soas.append(soa)
            anchors.append(soa.n)  # 立刻快照 anchor，对应 legacy 的 len(history)
            rows_for_valid.append(row)
        if prof_enc:
            tg1 = _t.perf_counter()

        # G.1.2：构造 [B, ctx_len, n_keys] 矩阵化切窗口（左 pad）。
        # A 优化：原来 B × ~29 次 numpy slice = ~60K 次 Python loop，
        # 改为按 4 类 group 各做一次 advanced indexing。
        n_i32 = len(self._i32_keys)
        n_d = len(self._d_keys)
        n_pc = len(self._pc_keys)
        n_addr = len(self._addr_keys)
        i32_buf = np.zeros((B, ctx_len, n_i32), dtype=np.int32)
        d_buf = np.zeros((B, ctx_len, n_d), dtype=np.int64)
        pc_buf = np.zeros((B, ctx_len, n_pc), dtype=np.int32)
        addr_buf = np.zeros((B, ctx_len, n_addr), dtype=np.uint64)
        attn_mask = np.zeros((B, ctx_len), dtype=np.int8)

        for b, soa in enumerate(soas):
            n = anchors[b]
            real_len = min(n, ctx_len)
            start = n - real_len
            pad = ctx_len - real_len
            # 一次切走整个 group：[real_len, n_keys] -> [pad:, :]
            i32_buf[b, pad:] = soa.i32_mat[start:n]
            d_buf[b, pad:] = soa.d_mat[start:n]
            pc_buf[b, pad:] = soa.pc_mat[start:n]
            addr_buf[b, pad:] = soa.addr_mat[start:n]
            attn_mask[b, pad:] = 1
        if prof_enc:
            tg2 = _t.perf_counter()

        # G.1.3：bucketize_dist (d0..d3) / clip pc / hash addr。
        # A 优化：bucketize/hash 都是 element-wise，可对 [B, ctx_len, n_keys]
        # 整体执行后再按 key 切片，避免每个 key 单独算一次。
        feat = {}
        # i32 直接按 key 切片，不需要变换
        for j, k in enumerate(self._i32_keys):
            feat[k] = i32_buf[:, :, j]
        # d_keys -> bucketize_dist
        d_bucketed = bucketize_dist(d_buf)  # [B, ctx_len, n_d]
        for j, k in enumerate(self._d_keys):
            feat[k] = d_bucketed[:, :, j]
        # pc_keys -> where(==255, 7) + clip(0, 7)
        pc_clipped = np.clip(np.where(pc_buf == 255, 7, pc_buf), 0, 7)
        for j, k in enumerate(self._pc_keys):
            feat[k] = pc_clipped[:, :, j]
        # addr_keys -> hash_addr_bucket
        addr_hashed = hash_addr_bucket(addr_buf)  # [B, ctx_len, n_addr]
        for j, k in enumerate(self._addr_keys):
            feat[self._addr_bucket_keys[k]] = addr_hashed[:, :, j]
        if prof_enc:
            tg3 = _t.perf_counter()

        # G.1.4：转 tensor。feat 中 value 来自 [B,ctx_len,n_keys] 的 axis=2 切片
        # （stride view），torch.from_numpy 接受 strided ndarray 共享内存；
        # .long() 做 int32→int64 cast 时一次性产出 contiguous int64 tensor，
        # 等价于先 ascontiguousarray 再 long()，但只复制一次而非两次。
        out = {}
        for k, v in feat.items():
            out[k] = torch.from_numpy(v).long()
        am = torch.from_numpy(attn_mask).bool()
        if prof_enc:
            tg4 = _t.perf_counter()
            self._prof_acc["enc_g1"] += (tg1 - tg0)
            self._prof_acc["enc_g2"] += (tg2 - tg1)
            self._prof_acc["enc_g3"] += (tg3 - tg2)
            self._prof_acc["enc_g4"] += (tg4 - tg3)
        return out, am, rows_for_valid

    def _soa_equivalence_check_against(self, items, soa_feat, soa_attn,
                                       legacy_feat, legacy_attn):
        np = self.np
        diffs = []
        for k in legacy_feat:
            if k not in soa_feat:
                diffs.append(f"missing_in_soa:{k}")
                continue
            a = legacy_feat[k].cpu().numpy()
            b = soa_feat[k].cpu().numpy()
            if a.shape != b.shape:
                diffs.append(f"shape:{k} legacy={a.shape} soa={b.shape}")
            elif not np.array_equal(a, b):
                diffs.append(f"{k}:{int((a!=b).sum())}/{a.size}")
        for k in soa_feat:
            if k not in legacy_feat:
                diffs.append(f"missing_in_legacy:{k}")
        if not np.array_equal(legacy_attn.cpu().numpy(), soa_attn.cpu().numpy()):
            diffs.append("attn_mask")
        if diffs:
            print(f"[soa-check] DIFF B={len(items)} -> {diffs[:10]}", file=sys.stderr)
            raise SystemExit("SoA equivalence check failed")

    def _soa_equivalence_check(self, items, soa_feat, soa_attn):
        """G.1 自检：用同一份 items 走 legacy feats_to_window+collate，与 SoA 输出
        做 dict-level numpy diff。注意：_build_batch_soa 已经把每个 history append
        过了，所以这里 legacy 路径不能再 append；改用倒数第二行作 anchor 重新切窗口。
        实际上更简洁：legacy 直接对 history[-1] 做 feats_to_window，因为 SoA
        也是 enc 已被 append 后取 soa[start:n] 即 history[start:len]。"""
        np = self.np
        import torch
        ctx_len = self.cfg.context_len
        feats_pairs = []
        for history, _row in items:
            f, am = self._feats_to_window(history, len(history) - 1, ctx_len)
            feats_pairs.append((f, am))
        legacy_feat, legacy_attn = self._collate_batch(feats_pairs)
        diffs = []
        for k in legacy_feat:
            if k not in soa_feat:
                diffs.append(f"missing_in_soa:{k}")
                continue
            a = legacy_feat[k].cpu().numpy()
            b = soa_feat[k].cpu().numpy()
            if a.shape != b.shape:
                diffs.append(f"shape_mismatch:{k} legacy={a.shape} soa={b.shape}")
            elif not np.array_equal(a, b):
                n_diff = int((a != b).sum())
                diffs.append(f"{k}:{n_diff}/{a.size}")
        for k in soa_feat:
            if k not in legacy_feat:
                diffs.append(f"missing_in_legacy:{k}")
        am_a = legacy_attn.cpu().numpy()
        am_b = soa_attn.cpu().numpy()
        if not np.array_equal(am_a, am_b):
            diffs.append(f"attn_mask:{int((am_a!=am_b).sum())}/{am_a.size}")
        if diffs:
            print(f"[soa-check] DIFF B={len(items)} -> {diffs[:10]}",
                  file=sys.stderr)
            raise SystemExit("SoA equivalence check failed")
        # 进一步比 forward 输出（同一份 input，分别走 SoA tensor / legacy tensor 各一次）
        if bool(int(os.environ.get("TAO_INFER_SOA_CHECK_FWD", "0"))):
            torch_dev = self.device
            def to_dev(d): return {k: v.to(torch_dev, non_blocking=True) for k, v in d.items()}
            with torch.inference_mode():
                amp_ctx = (torch.autocast(device_type="cuda", dtype=torch.bfloat16)
                           if self.use_cuda_amp else torch.autocast(device_type="cpu", enabled=False))
                with amp_ctx:
                    out_soa = self.model({"feat": to_dev(soa_feat),
                                          "attn_mask": soa_attn.to(torch_dev, non_blocking=True)})
                    out_leg = self.model({"feat": to_dev(legacy_feat),
                                          "attn_mask": legacy_attn.to(torch_dev, non_blocking=True)})
            for k in ("fetch_lat", "exec_lat", "head_logit", "mispred_logit"):
                a = out_soa[k].detach().float().cpu().numpy()
                b = out_leg[k].detach().float().cpu().numpy()
                max_abs = float(np.max(np.abs(a - b)))
                print(f"[soa-check-fwd] {k} max|d|={max_abs:.6e}", file=sys.stderr)

    def dump_prof(self):
        a = self._prof_acc
        if a["n"] == 0:
            return
        n = a["n"]
        print(f"[infer-prof] calls={n} avg_B={a['B']/n:.1f} "
              f"enc={a['enc']:.2f}s coll={a['coll']:.2f}s "
              f"h2d={a['h2d']:.2f}s fwd={a['fwd']:.2f}s d2h={a['d2h']:.2f}s "
              f"sum={(a['enc']+a['coll']+a['h2d']+a['fwd']+a['d2h']):.2f}s",
              file=sys.stderr)
        if a.get("enc_g1", 0.0) or a.get("enc_g2", 0.0):
            print(f"[infer-prof-enc] g1_append={a['enc_g1']:.2f}s "
                  f"g2_window={a['enc_g2']:.2f}s "
                  f"g3_bucketize={a['enc_g3']:.2f}s "
                  f"g4_to_tensor={a['enc_g4']:.2f}s",
                  file=sys.stderr)

    def predict_batch(self, items: List[Tuple[List[Dict], Dict]]) -> List[Tuple[float, float, float]]:
        import torch
        if self._collate_batch is None or self._feats_to_window is None:
            from ml.infer import collate_batch, feats_to_window
            self._collate_batch = collate_batch
            self._feats_to_window = feats_to_window

        prof = bool(int(os.environ.get("TAO_INFER_PROFILE", "0")))
        if prof:
            import time
            t0 = time.perf_counter()

        use_soa = bool(int(os.environ.get("TAO_INFER_SOA", "1")))
        if use_soa:
            if bool(int(os.environ.get("TAO_INFER_SOA_CHECK", "0"))):
                # G.1 等价性自检：先用 legacy 路径采样（append + 切窗）做基准，
                # 再走 SoA path 并 diff。两条 path 都会 history.append → 自检结束后
                # 我们必须把多 append 的尾巴撤回。
                ctx_len = self.cfg.context_len
                feats_pairs0 = []
                appended = []
                for hh, _r in items:
                    enc = to_model_row(_r)
                    hh.append(enc)
                    appended.append(hh)
                    f, am = self._feats_to_window(hh, len(hh) - 1, ctx_len)
                    feats_pairs0.append((f, am))
                legacy_feat_chk, legacy_attn_chk = self._collate_batch(feats_pairs0)
                # 撤回 append（SoA path 自己会再 append 一遍）
                for hh in appended:
                    hh.pop()
                batch_feat, batch_attn, rows_for_valid = self._build_batch_soa(items)
                self._soa_equivalence_check_against(items, batch_feat, batch_attn,
                                                    legacy_feat_chk, legacy_attn_chk)
            else:
                batch_feat, batch_attn, rows_for_valid = self._build_batch_soa(items)
        else:
            # legacy 路径：逐项 feats_to_window + collate_batch
            np = self.np
            ctx_len = self.cfg.context_len
            feats_pairs = []
            rows_for_valid = []
            for history, row in items:
                enc = to_model_row(row)
                history.append(enc)
                f, am = self._feats_to_window(history, len(history) - 1, ctx_len)
                feats_pairs.append((f, am))
                rows_for_valid.append(row)
            batch_feat, batch_attn = self._collate_batch(feats_pairs)
        if int(os.environ.get("TAO_INFER_DUMP_FIRST", "0")):
            if not getattr(self, "_dumped_first", False):
                import hashlib
                h = hashlib.md5()
                for k in sorted(batch_feat.keys()):
                    h.update(k.encode())
                    h.update(batch_feat[k].cpu().numpy().tobytes())
                h.update(batch_attn.cpu().numpy().tobytes())
                # 同时 dump 第一个 item 的 history 长度 + row keys+ row content hash
                import json as _json
                hist0, row0 = items[0]
                rh = hashlib.md5()
                rh.update(_json.dumps(row0, sort_keys=True, default=str).encode())
                # 同进程内同时跑另一条 path（SoA 跑后 history 已被 append，
                # legacy path 应该用 history[-1] 重新切窗口而不再 append。）
                ctx_len = self.cfg.context_len
                if use_soa:
                    feats_pairs2 = []
                    for hh, _r in items:
                        f, am = self._feats_to_window(hh, len(hh) - 1, ctx_len)
                        feats_pairs2.append((f, am))
                    bf2, ba2 = self._collate_batch(feats_pairs2)
                    h2 = hashlib.md5()
                    for k in sorted(bf2.keys()):
                        h2.update(k.encode())
                        h2.update(bf2[k].cpu().numpy().tobytes())
                    h2.update(ba2.cpu().numpy().tobytes())
                    rid = (row0.get('core_id'), row0.get('thread_id'), row0.get('micro_seq'))
                    print(f"[first-batch] use_soa=True B={len(items)} soa_md5={h.hexdigest()} "
                          f"legacy_md5={h2.hexdigest()} hist0_len={len(hist0)} row0={rid}",
                          file=sys.stderr)
                else:
                    rid = (row0.get('core_id'), row0.get('thread_id'), row0.get('micro_seq'))
                    print(f"[first-batch] use_soa=False B={len(items)} legacy_md5={h.hexdigest()} "
                          f"hist0_len={len(hist0)} row0={rid}",
                          file=sys.stderr)
                self._dumped_first = True
        if prof:
            t1 = time.perf_counter()
        valid_list = []
        for row in rows_for_valid:
            valid_list.append(
                int(row.get("is_branch", 0)) > 0
                and (int(row.get("is_last_microop", 0)) > 0
                     or int(row.get("is_microop", 0)) == 0))
        enc_dt = (t1 - t0) if prof else 0.0
        return self._forward_decode(batch_feat, batch_attn, valid_list, enc_dt)

    def _forward_decode(self, batch_feat, batch_attn, valid_list, enc_dt=0.0):
        """共享的 forward + 后处理路径。baseline 与 FASTENC 都走这里，
        保证两条 enc 路径在模型 forward / gate / 反变换上字节一致。"""
        import torch
        prof = bool(int(os.environ.get("TAO_INFER_PROFILE", "0")))
        if prof:
            import time
            t2 = time.perf_counter()
        # 维度 C：多 GPU shard 分片路径
        if self.gpu_shards >= 2:
            N = self.gpu_shards
            B = batch_attn.shape[0]
            # pad 到 N 的整数倍
            pad = (N - (B % N)) % N
            if pad:
                pad_attn = torch.zeros((pad,) + tuple(batch_attn.shape[1:]),
                                       dtype=batch_attn.dtype)
                batch_attn_p = torch.cat([batch_attn, pad_attn], dim=0)
                batch_feat_p = {}
                for k, v in batch_feat.items():
                    pv = torch.zeros((pad,) + tuple(v.shape[1:]), dtype=v.dtype)
                    batch_feat_p[k] = torch.cat([v, pv], dim=0)
            else:
                batch_attn_p = batch_attn
                batch_feat_p = batch_feat
            chunk = batch_attn_p.shape[0] // N
            shard_outs = [None] * N
            # 在每个 stream 上 enqueue h2d → forward → d2h
            for i in range(N):
                dev = self.shard_devices[i]
                stream = self.shard_streams[i]
                lo = i * chunk
                hi = lo + chunk
                with torch.cuda.device(dev):
                    with torch.cuda.stream(stream):
                        feat_i = {k: v[lo:hi].to(dev, non_blocking=True)
                                  for k, v in batch_feat_p.items()}
                        attn_i = batch_attn_p[lo:hi].to(dev, non_blocking=True)
                        with torch.inference_mode():
                            amp_ctx = (torch.autocast(device_type="cuda", dtype=torch.bfloat16)
                                       if self.use_cuda_amp
                                       else torch.autocast(device_type="cpu", enabled=False))
                            with amp_ctx:
                                out_i = self.shard_models[i]({"feat": feat_i, "attn_mask": attn_i})
                        fl = out_i["fetch_lat"].detach().float().cpu()
                        el = out_i["exec_lat"].detach().float().cpu()
                        mp = torch.sigmoid(out_i["mispred_logit"]).detach().float().cpu()
                        hl = out_i["head_logit"].detach().float().cpu()
                        shard_outs[i] = (fl, el, mp, hl)
            # 同步所有 device 上的 stream
            for i in range(N):
                self.shard_streams[i].synchronize()
            # gather
            fl_all = torch.cat([x[0] for x in shard_outs], dim=0)
            el_all = torch.cat([x[1] for x in shard_outs], dim=0)
            mp_all = torch.cat([x[2] for x in shard_outs], dim=0)
            hl_all = torch.cat([x[3] for x in shard_outs], dim=0)
            if pad:
                fl_all = fl_all[:B]
                el_all = el_all[:B]
                mp_all = mp_all[:B]
                hl_all = hl_all[:B]
            fetch_lat = fl_all.numpy()
            exec_lat = el_all.numpy()
            mispred = mp_all.numpy()
            head_logit = hl_all.numpy()
            if prof:
                # shard 路径下 enc/coll/h2d/fwd/d2h 不再独立可测，
                # 把全部 GPU 段时间记到 fwd 桶（h2d/d2h=0），便于 prof 报表区分
                t3 = time.perf_counter()
                t4 = t3
                # 走到下方 t5 时 d2h=t5-t4=0
        else:
            if self.device.type == "cuda":
                batch_feat = {k: v.to(self.device, non_blocking=True)
                              for k, v in batch_feat.items()}
                batch_attn = batch_attn.to(self.device, non_blocking=True)
            if prof:
                if self.device.type == "cuda":
                    torch.cuda.synchronize()
                t3 = time.perf_counter()
            with torch.inference_mode():
                amp_ctx = (torch.autocast(device_type="cuda", dtype=torch.bfloat16)
                           if self.use_cuda_amp else torch.autocast(device_type="cpu", enabled=False))
                with amp_ctx:
                    out = self.model({"feat": batch_feat, "attn_mask": batch_attn})
            if prof:
                if self.device.type == "cuda":
                    torch.cuda.synchronize()
                t4 = time.perf_counter()
            fetch_lat = out["fetch_lat"].detach().float().cpu().numpy()
            exec_lat = out["exec_lat"].detach().float().cpu().numpy()
            mispred = torch.sigmoid(out["mispred_logit"]).detach().float().cpu().numpy()
            head_logit = out["head_logit"].detach().float().cpu().numpy()
        fl_pos = self.np.expm1(self.np.maximum(fetch_lat, 0.0))
        if self.fetch_gate_mode == "direct":
            fl_cyc = fl_pos
        else:
            z = head_logit / float(self.fetch_gate_temp)
            head_prob = 1.0 / (1.0 + self.np.exp(-z))
            if self.fetch_gate_mode == "soft":
                fl_cyc = fl_pos * head_prob
            else:
                head_hard = (head_prob > 0.5).astype(self.np.int32)
                fl_cyc = fl_pos * head_hard
        el_cyc = self.np.expm1(self.np.maximum(exec_lat, 0.0))
        if prof:
            t5 = time.perf_counter()
            self._prof_acc["n"] += 1
            self._prof_acc["B"] += len(valid_list)
            self._prof_acc["enc"] += enc_dt
            self._prof_acc["coll"] += 0.0
            self._prof_acc["h2d"] += t3 - t2
            self._prof_acc["fwd"] += t4 - t3
            self._prof_acc["d2h"] += t5 - t4
        preds = []
        for i in range(len(valid_list)):
            mp = float(mispred[i]) if valid_list[i] else 0.0
            preds.append((float(fl_cyc[i]), float(el_cyc[i]), mp))
        return preds

    def predict(self, history: List[Dict], row: Dict) -> Tuple[float, float, float]:
        return self.predict_batch([(history, row)])[0]

    def predict_batch_fast(self, items_fast):
        """FASTENC：items_fast = list of (enc_fast: _CoreEnc, idx, valid_bool)。
        直接从 per-core 预编码矩阵切窗口，向量化构造 batch tensor，绕过
        to_model_row / _HistSoA / feature_row dict。forward 走与 baseline
        相同的 _forward_decode，保证 bit-exact。"""
        import torch
        prof = bool(int(os.environ.get("TAO_INFER_PROFILE", "0")))
        if prof:
            import time
            t0 = time.perf_counter()
        np = self.np
        ctx_len = self.cfg.context_len
        B = len(items_fast)
        layout = items_fast[0][0].layout
        keys = layout["keys"]
        nfeat = len(keys)
        # [B, ctx_len, nfeat] 左 pad 窗口切片
        buf = np.zeros((B, ctx_len, nfeat), dtype=np.int64)
        attn = np.zeros((B, ctx_len), dtype=np.int8)
        for b, (enc_fast, idx, _valid) in enumerate(items_fast):
            n = idx + 1                       # enc_history append 后 anchor = idx+1
            real_len = min(n, ctx_len)
            start = n - real_len
            pad = ctx_len - real_len
            buf[b, pad:] = enc_fast.mat[start:n]
            attn[b, pad:] = 1
        feat = {}
        for j, k in enumerate(keys):
            feat[k] = torch.from_numpy(buf[:, :, j]).long()
        batch_attn = torch.from_numpy(attn).bool()
        valid_list = [iv[2] for iv in items_fast]
        enc_dt = (time.perf_counter() - t0) if prof else 0.0
        return self._forward_decode(feat, batch_attn, valid_list, enc_dt)


# ---------------------------------------------------------------------------
# Quantum loop phases
# ---------------------------------------------------------------------------

def _build_feature_row(row: Dict, d_attrs: Dict, i_attrs: Dict,
                       win_attrs: Dict, grp_attrs: Dict) -> Dict:
    # C.2: dict 字面量合并比 update() 链快；CPython 3.11 BUILD_MAP_UNPACK
    # 走 fast-path 一次性分配 hash table，避免 5 次 rehash。
    return {**row, **d_attrs, **i_attrs, **win_attrs, **grp_attrs}


def phase1a_probe(cid: int, st: CoreState, sim, k_max: int,
                  need_feature_row: bool = False) -> None:
    """Speculative probe: peek up to K_max upcoming µops, gather features.

    E.1: Single ``batch_probe`` pybind call per core per quantum, replacing
    the K * (ifetch + mem_access) round-trips. The C++ side caches the last
    seen i_cl per core so counters are bit-equivalent to the previous path.

    E.5: Functional rows 改 SoA。fields_list 直接从 numpy 列切片 + zip 构造，
    不再走 row dict。feature_row 物化推迟到 ckpt 路径（need_feature_row=True）。
    """
    st.feat_buf = list(st.unconsumed)
    st.unconsumed = []
    soa = st.soa
    n_rows = soa.n
    budget = k_max - len(st.feat_buf)
    if budget <= 0 or st.next_probe_idx >= n_rows:
        return

    idx_lo = st.next_probe_idx
    idx_hi = min(idx_lo + budget, n_rows)

    # F.1: mock/label 热路径使用 ndarray POD 接口，C++ 只返回 d_bank_id。
    # ckpt 模式仍走完整 batch_probe，保留 oracle/win dict 用于 feature_row。
    if (not need_feature_row and hasattr(sim, "batch_probe_pod")
            and hasattr(sim, "commit_quantum_pod")):
        d_banks = sim.batch_probe_pod(
            cid,
            soa.macro_pc[idx_lo:idx_hi],
            soa.paddr[idx_lo:idx_hi],
            soa.cacheline_paddr[idx_lo:idx_hi],
            soa.is_load[idx_lo:idx_hi],
            soa.is_store[idx_lo:idx_hi],
            soa.is_atomic[idx_lo:idx_hi],
            soa.is_branch[idx_lo:idx_hi],
            soa.size[idx_lo:idx_hi],
            soa.micro_seq[idx_lo:idx_hi],
            soa.thread_id[idx_lo:idx_hi],
        )
        for k, d_bank_id in enumerate(d_banks):
            i = idx_lo + k
            st.feat_buf.append(PendingProbe(
                cid=cid, idx=i, d_attrs={}, d_bank_id=int(d_bank_id),
            ))
            st.next_probe_idx += 1
        return

    # E.5: 从 SoA 列切片直接 zip，避免 dict.get 路径。zip + tolist 保证
    # 元素是 Python int/bool（pybind 无需 numpy scalar 转换）。
    fields_list = list(zip(
        soa.macro_pc[idx_lo:idx_hi].tolist(),
        soa.paddr[idx_lo:idx_hi].tolist(),
        soa.cacheline_paddr[idx_lo:idx_hi].tolist(),
        soa.is_load[idx_lo:idx_hi].astype(bool).tolist(),
        soa.is_store[idx_lo:idx_hi].astype(bool).tolist(),
        soa.is_atomic[idx_lo:idx_hi].astype(bool).tolist(),
        soa.is_branch[idx_lo:idx_hi].astype(bool).tolist(),
        soa.size[idx_lo:idx_hi].tolist(),
        soa.micro_seq[idx_lo:idx_hi].tolist(),
        soa.thread_id[idx_lo:idx_hi].tolist(),
    ))
    oracles = sim.batch_probe(cid, fields_list)

    row_dicts = soa.row_dicts if need_feature_row else None
    enc_fast = st.enc_fast  # FASTENC：非 None 即启用向量化 enc 路径
    fe_layout = enc_fast.layout if enc_fast is not None else None
    for k, (oracle, fields) in enumerate(zip(oracles, fields_list)):
        i = idx_lo + k
        # group_features_idx 是 stateful（更新 prev_macro_pc/prev_i_cl 等），
        # 必须每行调用一次，结果在 ckpt 模式拼回 feature_row。
        macro_head, uop_pos, i_group_head, i_group_pos = group_features_idx(
            soa, i, st)
        feature_row: Optional[Dict] = None
        if enc_fast is not None:
            # FASTENC：动态列（oracle 16 + win 12 + group 4）一次性 block 写入
            # 预编码矩阵 mat[i]，绕过 feature_row / to_model_row / _HistSoA。
            d_bank_id = int(oracle.get("d_bank_id", 0))
            d_attrs_for_win = {"d_bank_id": d_bank_id}
            r_win = {
                "is_load": int(soa.is_load[i]), "is_store": int(soa.is_store[i]),
                "is_atomic": int(soa.is_atomic[i]), "is_branch": int(soa.is_branch[i]),
                "macro_pc": int(soa.macro_pc[i]), "paddr": int(soa.paddr[i]),
                "cacheline_paddr": int(soa.cacheline_paddr[i]),
            }
            win_attrs = st.win.derive_before_update(r_win, d_attrs_for_win)
            st.win.update(r_win, d_attrs_for_win)
            dyn = [oracle.get(kk, 0) for kk in fe_layout["oracle_keys"]]
            dyn += [win_attrs[kk] for kk in fe_layout["win_keys"]]
            dyn += [macro_head, uop_pos, i_group_head, i_group_pos]
            enc_fast.mat[i, enc_fast.dyn_lo:enc_fast.dyn_hi] = dyn
        elif row_dicts is not None:
            r = row_dicts[i]
            d_bank_id = int(oracle.get("d_bank_id", 0))
            d_attrs_for_win = {"d_bank_id": d_bank_id}
            # P1C / V10.3 win features：strictly causal，按 train derive_sequence_features
            # 顺序——先 derive_before_update（用 update 之前的窗口状态），再 update。
            win_attrs = st.win.derive_before_update(r, d_attrs_for_win)
            st.win.update(r, d_attrs_for_win)
            feature_row = {**r, **oracle, **win_attrs,
                           "is_macro_head": macro_head,
                           "uop_pos_in_macro": uop_pos,
                           "i_group_head": i_group_head,
                           "i_group_pos": i_group_pos}
        st.feat_buf.append(PendingProbe(
            cid=cid, idx=i, d_attrs=oracle,
            d_bank_id=int(oracle.get("d_bank_id", 0)),
            fields_tuple=fields, feature_row=feature_row,
        ))
        st.next_probe_idx += 1


def phase1b_predict(cores: Dict[int, CoreState],
                    predictor: Optional[ModelPredictor],
                    label_driven: bool, labels: Dict,
                    mock_model: bool, model_batch_size: int) -> None:
    """Run model forward exactly once per quantum, on fresh probes only.

    Unconsumed probes already carry cached preds from the previous quantum;
    re-forward would simply reproduce identical numbers (the model is
    deterministic given the cached feature_row), so we skip them. This also
    avoids double-mutating ``enc_history`` and (for label_driven) double-
    advancing ``prev_fetch_tick``.
    """
    fresh: List[Tuple[CoreState, PendingProbe]] = []
    for cid in sorted(cores.keys()):
        st = cores[cid]
        for probe in st.feat_buf:
            if probe.preds is None:
                fresh.append((st, probe))
    if not fresh:
        return

    if label_driven:
        for st, probe in fresh:
            probe.preds = label_prediction_idx(st.soa, probe.idx, labels, st)
        return

    if predictor is None:
        for _, probe in fresh:
            probe.preds = mock_prediction(probe.feature_row)
        return

    bs = max(1, int(model_batch_size))
    # FASTENC：若 fresh 的 core 都带 enc_fast，则走向量化 predict（绕过 feature_row）。
    use_fast = all(st.enc_fast is not None for st, _ in fresh)
    if use_fast:
        for i in range(0, len(fresh), bs):
            chunk = fresh[i:i + bs]
            items_fast = []
            for st, probe in chunk:
                soa = st.soa
                j = probe.idx
                valid = bool(soa.is_branch[j]) and (
                    bool(soa.is_last_microop[j]) or not bool(soa.is_microop[j]))
                items_fast.append((st.enc_fast, j, valid))
            preds = predictor.predict_batch_fast(items_fast)
            for (st, probe), p in zip(chunk, preds):
                probe.preds = p
        return

    for i in range(0, len(fresh), bs):
        chunk = fresh[i:i + bs]
        items = [(st.enc_history, probe.feature_row) for st, probe in chunk]
        preds = predictor.predict_batch(items)
        for (st, probe), p in zip(chunk, preds):
            probe.preds = p


def phase1c_commit(st: CoreState, sim, pending: List[PendingEvent],
                   delta_t: int, label_driven: bool, labels: Dict,
                   emit_bin: bool = False) -> None:
    """Walk feat_buf in order, commit until quantum deadline crossed.

    Break condition follows §9.3: only stop once we've already committed at
    least one µop AND fetch_clock has crossed (base + Δt). The remaining
    probes survive as ``unconsumed`` for the next quantum.

    E.2: 在 commit 累积阶段同时构造 11 元 tuple（fields_tuple + d_bank_id），
    末尾对 sim 调用一次 batch_window_update 把 win 状态前推。这样
    每 quantum 每 core win 累积只产生一次 pybind crossing。
    """
    soa = st.soa

    # E.6 fast path：把 deadline walk + ReferenceClock + win.update 下沉到 C++。
    # Python 仍负责跨核 Phase 2 sort；JSONL 下 payload dict 继续交给 orjson，
    # BIN 下 payload 是 56B 定长 record。
    if not st.feat_buf:
        st.clock.advance_base(delta_t)
        return
    if hasattr(sim, "commit_quantum_pod"):
        n_probe = len(st.feat_buf)
        idx_lo = int(st.feat_buf[0].idx)
        idx_hi = idx_lo + n_probe
        if idx_hi > soa.n or st.feat_buf[-1].idx != idx_hi - 1:
            raise RuntimeError("commit_quantum_pod requires contiguous feat_buf indices")
        d_banks = np.empty(n_probe, dtype=np.uint32)
        fetch_lats = np.empty(n_probe, dtype=np.float64)
        exec_lats = np.empty(n_probe, dtype=np.float64)
        mispreds = np.empty(n_probe, dtype=np.float64)
        label_fetch_ticks = (np.empty(n_probe, dtype=np.float64)
                             if label_driven else np.zeros(0, dtype=np.float64))
        for k, probe in enumerate(st.feat_buf):
            assert probe.preds is not None, "phase1b must have populated preds"
            idx = probe.idx
            fl, el, mp = probe.preds
            valid = bool(soa.is_branch[idx]) and (
                bool(soa.is_last_microop[idx]) or not bool(soa.is_microop[idx]))
            d_banks[k] = int(probe.d_bank_id)
            fetch_lats[k] = float(fl)
            exec_lats[k] = float(el)
            mispreds[k] = float(mp) if valid else 0.0
            if label_driven:
                lab = labels[(int(soa.core_id[idx]), int(soa.thread_id[idx]),
                              int(soa.micro_seq[idx]))]
                label_fetch_ticks[k] = float(lab["fetch_tick"])
        result = sim.commit_quantum_pod(
            st.feat_buf[0].cid,
            soa.macro_pc[idx_lo:idx_hi],
            soa.paddr[idx_lo:idx_hi],
            soa.cacheline_paddr[idx_lo:idx_hi],
            soa.is_load[idx_lo:idx_hi],
            soa.is_store[idx_lo:idx_hi],
            soa.is_atomic[idx_lo:idx_hi],
            soa.is_branch[idx_lo:idx_hi],
            soa.micro_seq[idx_lo:idx_hi],
            soa.thread_id[idx_lo:idx_hi],
            d_banks, fetch_lats, exec_lats, mispreds,
            soa.is_microop[idx_lo:idx_hi],
            soa.is_last_microop[idx_lo:idx_hi],
            label_fetch_ticks,
            float(st.first_fetch_tick or 0),
            st.clock.fetch_clock, st.clock.ready_clock,
            st.clock.fetch_clock_base, st.clock.committed_this_quantum,
            int(delta_t), bool(label_driven), bool(emit_bin))
        consumed = int(result["consumed"])
        st.clock.fetch_clock = float(result["fetch_clock"])
        st.clock.ready_clock = float(result["ready_clock"])
        st.clock.fetch_clock_base = float(result["fetch_clock_base"])
        st.clock.committed_this_quantum = int(result["committed_this_quantum"])
        st.macro_count += int(result["macro_inc"])
        st.uop_count += int(result["uop_inc"])
        if label_driven:
            st.max_abs_fetch_diff = max(st.max_abs_fetch_diff,
                                        float(result["max_abs_fetch_diff"]))
        for t, core_id, seq, payload in result["events"]:
            pending.append(PendingEvent(float(t), int(core_id), int(seq), payload))
        st.idx += consumed
        st.unconsumed = list(st.feat_buf[consumed:])
        st.feat_buf = []
        return
    if hasattr(sim, "commit_quantum"):
        fields11: List[Tuple] = []
        fetch_lats: List[float] = []
        exec_lats: List[float] = []
        mispreds: List[float] = []
        is_microop: List[bool] = []
        is_last_microop: List[bool] = []
        label_fetch_ticks: List[float] = []
        for probe in st.feat_buf:
            assert probe.preds is not None, "phase1b must have populated preds"
            idx = probe.idx
            fl, el, mp = probe.preds
            valid = bool(soa.is_branch[idx]) and (
                bool(soa.is_last_microop[idx]) or not bool(soa.is_microop[idx]))
            mp = float(mp) if valid else 0.0
            fields11.append(probe.fields_tuple + (int(probe.d_bank_id),))
            fetch_lats.append(float(fl))
            exec_lats.append(float(el))
            mispreds.append(float(mp))
            is_microop.append(bool(soa.is_microop[idx]))
            is_last_microop.append(bool(soa.is_last_microop[idx]))
            if label_driven:
                lab = labels[(int(soa.core_id[idx]), int(soa.thread_id[idx]),
                              int(soa.micro_seq[idx]))]
                label_fetch_ticks.append(float(lab["fetch_tick"]))
        result = sim.commit_quantum(
            st.feat_buf[0].cid,
            fields11, fetch_lats, exec_lats, mispreds,
            is_microop, is_last_microop, label_fetch_ticks,
            float(st.first_fetch_tick or 0),
            st.clock.fetch_clock, st.clock.ready_clock,
            st.clock.fetch_clock_base, st.clock.committed_this_quantum,
            int(delta_t), bool(label_driven), bool(emit_bin))
        consumed = int(result["consumed"])
        st.clock.fetch_clock = float(result["fetch_clock"])
        st.clock.ready_clock = float(result["ready_clock"])
        st.clock.fetch_clock_base = float(result["fetch_clock_base"])
        st.clock.committed_this_quantum = int(result["committed_this_quantum"])
        st.macro_count += int(result["macro_inc"])
        st.uop_count += int(result["uop_inc"])
        if label_driven:
            st.max_abs_fetch_diff = max(st.max_abs_fetch_diff,
                                        float(result["max_abs_fetch_diff"]))
        for t, core_id, seq, payload in result["events"]:
            pending.append(PendingEvent(float(t), int(core_id), int(seq), payload))
        st.idx += consumed
        st.unconsumed = list(st.feat_buf[consumed:])
        st.feat_buf = []
        return

    deadline = st.clock.fetch_clock_base + float(delta_t)
    consumed = 0
    win_fields: List[Tuple] = []
    win_mask: List[bool] = []
    win_cid: Optional[int] = None
    for probe in st.feat_buf:
        assert probe.preds is not None, "phase1b must have populated preds"
        fl, el, mp = probe.preds
        valid = bool(soa.is_branch[probe.idx]) and (
            bool(soa.is_last_microop[probe.idx]) or not bool(soa.is_microop[probe.idx]))
        mp = float(mp) if valid else 0.0
        if (st.clock.fetch_clock >= deadline
                and st.clock.committed_this_quantum >= 1):
            break

        fc, rc = st.clock.step(fl, el)
        i = probe.idx
        thread_id = int(soa.thread_id[i])
        micro_seq = int(soa.micro_seq[i])

        if label_driven:
            lab = labels[(int(soa.core_id[i]), thread_id, micro_seq)]
            abs_fc = fc + float(st.first_fetch_tick or 0)
            st.max_abs_fetch_diff = max(st.max_abs_fetch_diff,
                                        abs(abs_fc - float(lab["fetch_tick"])))

        if is_macro_counted_idx(soa, i):
            st.macro_count += 1
        st.uop_count += 1

        payload = {
            "core_id": probe.cid,
            "thread_id": thread_id,
            "micro_seq": micro_seq,
            "fetch_clock": fc,
            "ready_clock": rc,
            "fetch_lat": fl,
            "exec_lat": el,
            "mispred": mp,
        }
        pending.append(PendingEvent(
            t=fc + max(0.0, float(el)),
            core_id=probe.cid,
            seq=micro_seq,
            payload=payload,
        ))

        # E.2: 累积 (fields_tuple + d_bank_id) 给 batch_window_update。
        if win_cid is None:
            win_cid = int(probe.cid)
        win_fields.append(probe.fields_tuple + (int(probe.d_bank_id),))
        win_mask.append(True)
        # E.3: D.5a 后 backend->commit() 仅生成未使用的 LineDelta，phase1c
        # 不再需要每 µop pybind crossing；状态在 probe() 阶段已写。win 累积
        # 由 batch_window_update 一次性下推。
        st.idx += 1
        consumed += 1

    # E.2: 单次 pybind 把已 commit 的 win 累积下推到 C++ WindowState。
    if win_fields and win_cid is not None:
        sim.batch_window_update(win_cid, win_fields, win_mask)
    st.unconsumed = list(st.feat_buf[consumed:])
    st.feat_buf = []
    st.clock.advance_base(delta_t)


def phase2_reconcile_stub(pending: List[PendingEvent]) -> None:
    """Phase A coordinator: just sort jsonl events; no shared-side oracle
    backfill yet (that lands in Phase B with PyCoordinator)."""
    pending.sort(key=lambda e: (e.t, e.core_id, e.seq))


def phase2_reconcile(pending: List[PendingEvent], sim) -> None:
    """Phase B coordinator: deterministic ordering + shared-side reconcile.

    D.5a mutates shared state directly through atomic / bank-locked
    structures, so ``Coordinator::reconcile`` is a no-op stub. Skip the
    pybind crossing entirely unless the backend opts in via
    ``_reconcile_is_active`` (kept as a forward-compatibility hook for when
    a real cross-core reconcile lands).
    """
    pending.sort(key=lambda e: (e.t, e.core_id, e.seq))
    if hasattr(sim, "reconcile") and getattr(sim, "_reconcile_is_active", False):
        sim.reconcile([], [])


def phase3_flush(pending: List[PendingEvent], fout, out_format: str = "jsonl") -> None:
    if out_format == "bin":
        buf = bytearray()
        for ev in pending:
            buf += ev.payload
        fout.write(buf)
        return

    # E.4: 用 orjson + bytearray 批写。fout 在 main() 中已以 buffering=1MB
    # 打开；这里再聚合一次到 bytearray，把 N 次 write() syscall 降到 1 次。
    if _ORJSON:
        buf = bytearray()
        for ev in pending:
            buf += orjson.dumps(ev.payload)
            buf += b"\n"
        # fout 是文本模式时（mock_model fallback），用 bytes.decode；
        # 但 main() 在 E.4 后默认以二进制模式打开 jsonl。
        if "b" in getattr(fout, "mode", ""):
            fout.write(buf)
        else:
            fout.write(buf.decode("utf-8"))
        return
    is_binary = "b" in getattr(fout, "mode", "")
    for ev in pending:
        line = json.dumps(ev.payload, separators=(",", ":"))
        if is_binary:
            fout.write(line.encode("utf-8"))
            fout.write(b"\n")
        else:
            fout.write(line)
            fout.write("\n")


def quantum_loop(cores: Dict[int, CoreState], sim,
                 predictor: Optional[ModelPredictor],
                 args, labels: Dict, fout,
                 profile_out: Optional[Dict[str, float]] = None) -> int:
    delta_t = max(1, int(args.quantum_cycles))
    k_max = max(1, int(args.k_max))
    model_batch_size = max(1, int(args.model_batch_size))
    # B: 每 quantum 最多产生 k_max * num_cores 个 fresh probe；若用户给的
    # model_batch_size 小于该值，phase1b 会拆成多次 GPU forward，白白浪费
    # 多核扩展带来的并行 probe。这里把 batch 上限自动顶到 fresh 上限，避免
    # 多次 forward；下沿仍由用户控制。一致性：模型本身无 batch 维状态，
    # bf16 AMP 已允许批维数值差，TAO_INFER_DUMP_FIRST 的 md5 校验是按 chunk
    # 计算的，不跨 batch_size 比对。
    fresh_cap = k_max * max(1, len(cores))
    if model_batch_size < fresh_cap:
        model_batch_size = fresh_cap
    n_total = 0
    # E.5: 仅 ckpt 模式才需要稀疏字段（producer_dists 等）合成 feature_row。
    # mock/label-driven 路径恒为 False，省 200K+ 次 dict 物化。
    need_feature_row = bool(predictor is not None) and not args.mock_model

    if hasattr(sim, "_local"):
        for cid in cores:
            sim._local(cid)

    cid_order = sorted(cores.keys())
    phase1a_workers = max(1, int(getattr(args, "phase1a_workers", 1)))
    # 回退方案 A 的 driver 自动 fan-out：实测 16c 上 ThreadPool overhead 与
    # phase1c C++ release 段同量级，净收益为 0 甚至略负。默认串行；用户显式
    # `--phase1c-workers N` (N>1) 才启用并行（_phase1c_parallel_safe 仍是闸门）。
    phase1c_arg = int(getattr(args, "phase1c_workers", 0))
    phase1c_auto_safe = bool(getattr(sim, "_phase1c_parallel_safe", False))
    phase1c_workers = max(1, phase1c_arg) if phase1c_auto_safe else 1
    if phase1c_workers > len(cid_order):
        phase1c_workers = len(cid_order)
    phase1a_pool = (ThreadPoolExecutor(max_workers=min(phase1a_workers, len(cid_order)))
                    if phase1a_workers > 1 and len(cid_order) > 1 else None)
    phase1c_pool = (ThreadPoolExecutor(max_workers=phase1c_workers)
                    if phase1c_workers > 1 and len(cid_order) > 1 else None)

    def all_done() -> bool:
        for st in cores.values():
            if st.idx < st.soa.n:
                return False
            if st.unconsumed or st.feat_buf:
                return False
        return True

    try:
        while not all_done():
            # Phase 1a: 默认串行，保证 D.5a 中 probe 直写 shared MESI 状态时的
            # 跨核更新顺序稳定。--phase1a-workers > 1 作为显式实验开关保留。
            if profile_out is not None:
                t0 = time.perf_counter()
            if phase1a_pool is not None:
                futures = [
                    phase1a_pool.submit(phase1a_probe, cid, cores[cid], sim, k_max,
                                        need_feature_row)
                    for cid in cid_order
                ]
                for fut in futures:
                    fut.result()
            else:
                for cid in cid_order:
                    phase1a_probe(cid, cores[cid], sim, k_max,
                                  need_feature_row=need_feature_row)
            if profile_out is not None:
                t1 = time.perf_counter()
                profile_out["phase1a"] += t1 - t0
            # Phase 1b
            phase1b_predict(cores, predictor, args.label_driven, labels,
                            args.mock_model, model_batch_size)
            if profile_out is not None:
                t2 = time.perf_counter()
                profile_out["phase1b"] += t2 - t1
            # Phase 1c (and per-core advance_base). 每核只写自己的 CoreState /
            # LocalRefSim::WindowState，事件最后仍在 phase2 统一排序，因此可并行。
            pending: List[PendingEvent] = []
            if phase1c_pool is not None:
                per_core_pending: Dict[int, List[PendingEvent]] = {
                    cid: [] for cid in cid_order
                }
                futures = [
                    phase1c_pool.submit(phase1c_commit, cores[cid], sim,
                                        per_core_pending[cid], delta_t,
                                        args.label_driven, labels,
                                        args.out_format == "bin")
                    for cid in cid_order
                ]
                for fut in futures:
                    fut.result()
                for cid in cid_order:
                    pending.extend(per_core_pending[cid])
            else:
                for cid in cid_order:
                    phase1c_commit(cores[cid], sim, pending, delta_t,
                                   args.label_driven, labels,
                                   emit_bin=(args.out_format == "bin"))
            if profile_out is not None:
                t3 = time.perf_counter()
                profile_out["phase1c"] += t3 - t2
            if not pending and all_done():
                break  # nothing committed and done — exit cleanly
            # Phase 2 (reconcile barrier) + Phase 3 (batch flush at quantum boundary)
            phase2_reconcile(pending, sim)
            if profile_out is not None:
                t4 = time.perf_counter()
                profile_out["phase2"] += t4 - t3
            phase3_flush(pending, fout, args.out_format)
            if profile_out is not None:
                t5 = time.perf_counter()
                profile_out["phase3"] += t5 - t4
                profile_out["quanta"] += 1
            n_total += len(pending)
            if not pending:
                break
    finally:
        if phase1a_pool is not None:
            phase1a_pool.shutdown(wait=True)
        if phase1c_pool is not None:
            phase1c_pool.shutdown(wait=True)
    return n_total


def _validate_args(args) -> None:
    required = [
        "functional_dir",
        "uarch_profile",
        "out_jsonl",
        "report_json",
    ]
    if getattr(args, "ref_sim_backend", "timing-functional") != "timing-functional":
        required.append("ref_sim_module_dir")
    missing = [name for name in required if not getattr(args, name, None)]
    if missing:
        raise SystemExit("missing required args: " + ", ".join(f"--{x.replace('_', '-')}" for x in missing))
    if args.label_driven and not args.labels_dir:
        raise SystemExit("--label-driven requires --labels-dir")
    if not args.label_driven and not args.mock_model and not args.ckpt:
        raise SystemExit("need one of --label-driven, --mock-model or --ckpt")


def _build_cores(args, predictor: Optional[ModelPredictor]) -> Dict[int, CoreState]:
    cores: Dict[int, CoreState] = {}
    for cid, (soa, soa_path) in load_functional_dir_soa(args.functional_dir).items():
        cores[cid] = CoreState(soa=soa, soa_path=soa_path)
    if predictor is not None:
        # FASTENC（方案 a）：env 灰度，**默认开启**（实测 1.50× 提速、bit-exact PASS）。
        # 显式 `TAO_INFER_FASTENC=0` 可回退到 _materialize_row_dicts baseline。
        use_fastenc = bool(int(os.environ.get("TAO_INFER_FASTENC", "1")))
        if use_fastenc:
            layout = _fastenc_layout()
            for st in cores.values():
                st.enc_fast = build_core_enc(st.soa_path, layout)
        else:
            for st in cores.values():
                _materialize_row_dicts(st.soa, st.soa_path)
    return cores


def run_job(args, predictor: Optional[ModelPredictor] = None) -> Dict[str, Any]:
    _validate_args(args)

    need_predictor = not args.label_driven and not args.mock_model
    own_predictor = False
    predictor_obj = predictor
    if need_predictor:
        if predictor_obj is None:
            predictor_obj = ModelPredictor(
                args.ckpt, args.fetch_gate_mode, args.fetch_gate_temp)
            own_predictor = True
        elif os.path.abspath(args.ckpt) != predictor_obj.ckpt_path:
            raise SystemExit(
                f"persistent predictor ckpt mismatch: loaded={predictor_obj.ckpt_path} "
                f"requested={os.path.abspath(args.ckpt)}"
            )
        else:
            predictor_obj.set_fetch_gate(args.fetch_gate_mode, args.fetch_gate_temp)
        predictor_obj.reset_runtime_state()
    else:
        predictor_obj = None

    cores = _build_cores(args, predictor_obj)
    labels = load_labels(args.labels_dir) if args.label_driven else {}
    stats_insts = parse_stats_num_insts(args.stats)
    if args.ref_sim_backend == "timing-functional":
        sim = make_timing_functional_backend(args.uarch_profile, args)
    elif args.ref_sim_backend == "coordinator":
        sim = LocalPybindBackend(args.uarch_profile, args.ref_sim_module_dir)
    else:
        sim = PybindBackend(args.uarch_profile, args.ref_sim_module_dir)

    os.makedirs(os.path.dirname(os.path.abspath(args.out_jsonl)), exist_ok=True)
    profile_out: Optional[Dict[str, float]] = None
    if os.environ.get("TAO_INFER_PROFILE", "0") not in ("", "0", "false", "False"):
        profile_out = {"phase1a": 0.0, "phase1b": 0.0, "phase1c": 0.0,
                       "phase2": 0.0, "phase3": 0.0, "quanta": 0.0}
    quantum_t0 = time.perf_counter() if profile_out is not None else 0.0
    with open(args.out_jsonl, "wb", buffering=1 << 20) as fout:
        n = quantum_loop(cores, sim, predictor_obj, args, labels, fout,
                         profile_out=profile_out)
    if profile_out is not None:
        profile_out["quantum_loop_total"] = time.perf_counter() - quantum_t0

    infer_macro = {str(cid): st.macro_count for cid, st in sorted(cores.items())}
    infer_uop = {str(cid): st.uop_count for cid, st in sorted(cores.items())}
    infer_cycle = {str(cid): st.clock.ready_clock for cid, st in sorted(cores.items())}
    total_cycle_wall = max((st.clock.ready_clock for st in cores.values()), default=0.0)
    total_cycle_sum = sum(st.clock.ready_clock for st in cores.values())
    total_macro = sum(st.macro_count for st in cores.values())
    report = {
        "rows": n,
        "infer_macro_count": infer_macro,
        "infer_uop_count": infer_uop,
        "infer_cycle_count": infer_cycle,
        "stats_numInsts": {str(k): v for k, v in sorted(stats_insts.items())},
        "macro_diff_infer_minus_stats": ({
            str(k): infer_macro.get(str(k), 0) - stats_insts.get(k, 0)
            for k in sorted(set(stats_insts) | {int(x) for x in infer_macro})
        } if stats_insts else {}),
        "total_cycle": total_cycle_sum,
        "total_cycle_sum": total_cycle_sum,
        "total_cycle_wall": total_cycle_wall,
        "total_macro": total_macro,
        "cpi_macro": (total_cycle_sum / total_macro) if total_macro else None,
        "cpi_sumsum": (total_cycle_sum / total_macro) if total_macro else None,
        "cpi_wall": (total_cycle_wall / total_macro) if total_macro else None,
        "quantum_cycles": int(args.quantum_cycles),
        "k_max": int(args.k_max),
        "fetch_gate_mode": getattr(args, "fetch_gate_mode", "hard"),
        "fetch_gate_temp": float(getattr(args, "fetch_gate_temp", 1.0)),
        "refsim_warmup_records_per_core": int(
            getattr(args, "refsim_warmup_records_per_core", 0)
        ),
        "out_format": args.out_format,
        "label_driven_max_abs_fetch_diff": {
            str(cid): st.max_abs_fetch_diff for cid, st in sorted(cores.items())
        } if args.label_driven else {},
        "label_driven_ready_end_diff": {
            str(cid): st.clock.ready_clock - st.max_truth_ready_rel
            for cid, st in sorted(cores.items())
        } if args.label_driven else {},
    }
    if hasattr(sim, "drain_counters"):
        report["coord_counters"] = sim.drain_counters()
    if profile_out is not None:
        report["profile"] = profile_out
        total = profile_out.get("quantum_loop_total", 0.0) or 0.0
        def _pct(name: str) -> str:
            v = profile_out.get(name, 0.0)
            return f"{name}={v:.3f}s ({(100.0*v/total) if total else 0.0:.1f}%)"
        print("[driver-profile] "
              + " ".join(_pct(p) for p in ("phase1a", "phase1b", "phase1c",
                                            "phase2", "phase3"))
              + f" total={total:.3f}s quanta={int(profile_out.get('quanta', 0))}",
              file=sys.stderr)
    os.makedirs(os.path.dirname(os.path.abspath(args.report_json)), exist_ok=True)
    with open(args.report_json, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
    print(f"[driver] rows={n} total_macro={total_macro} cpi={report['cpi_macro']}", file=sys.stderr)
    if predictor_obj is not None and hasattr(predictor_obj, "dump_prof"):
        predictor_obj.dump_prof()
    if own_predictor:
        predictor_obj.reset_runtime_state()
    return report


def _merge_job_args(base_args, overrides: Dict[str, Any]):
    merged = dict(vars(base_args))
    merged.update(overrides)
    merged["persistent_session"] = False
    return argparse.Namespace(**merged)


def run_persistent_session(args) -> None:
    base_need_predictor = not args.label_driven and not args.mock_model
    if base_need_predictor and not args.ckpt:
        raise SystemExit("--persistent-session requires a fixed --ckpt at startup")

    predictor = None if not base_need_predictor else ModelPredictor(
        args.ckpt, args.fetch_gate_mode, args.fetch_gate_temp)
    if predictor is not None:
        print(f"[driver] persistent predictor ready ckpt={predictor.ckpt_path}",
              file=sys.stderr)

    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        if line in {"quit", "exit"}:
            break
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise SystemExit("persistent job payload must be a JSON object")
            if "ckpt" in payload and predictor is not None:
                req_ckpt = os.path.abspath(str(payload["ckpt"]))
                if req_ckpt != predictor.ckpt_path:
                    raise SystemExit(
                        f"persistent predictor ckpt mismatch: loaded={predictor.ckpt_path} "
                        f"requested={req_ckpt}"
                    )
            if "label_driven" in payload and bool(payload["label_driven"]) != bool(args.label_driven):
                raise SystemExit("persistent session does not allow changing --label-driven per job")
            if "mock_model" in payload and bool(payload["mock_model"]) != bool(args.mock_model):
                raise SystemExit("persistent session does not allow changing --mock-model per job")

            job_args = _merge_job_args(args, payload)
            report = run_job(job_args, predictor=predictor)
            reply = {
                "ok": True,
                "out_jsonl": job_args.out_jsonl,
                "report_json": job_args.report_json,
                "rows": report["rows"],
                "total_macro": report["total_macro"],
                "cpi_macro": report["cpi_macro"],
            }
        except SystemExit as exc:
            reply = {"ok": False, "error": str(exc)}
        except Exception as exc:  # pragma: no cover
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(reply, separators=(",", ":")), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--functional-dir")
    ap.add_argument("--uarch-profile")
    ap.add_argument("--ref-sim-module-dir",
                    help="Directory containing ref_sim_py*.so")
    ap.add_argument("--out-jsonl")
    ap.add_argument("--out-format", choices=["jsonl", "bin"], default="jsonl",
                    help="Output format. jsonl keeps bit-exact guard; bin writes fixed 56B records.")
    ap.add_argument("--report-json")
    ap.add_argument("--label-driven", action="store_true")
    ap.add_argument("--labels-dir",
                    help="Directory containing labels.core*.parquet/jsonl")
    ap.add_argument("--stats", help="gem5 stats.txt for macro-count baseline")
    ap.add_argument("--mock-model", action="store_true",
                    help="Use constant latency prediction; useful for plumbing")
    ap.add_argument("--ckpt", help="Strict no-PC model checkpoint")
    ap.add_argument("--model-batch-size", type=int, default=8,
                    help="Max number of fresh probes per model forward call")
    ap.add_argument("--fetch-gate-mode", choices=["hard", "soft", "direct"],
                    default="hard",
                    help="Fetch zero-inflated gate at inference time: hard keeps "
                         "legacy thresholding, soft uses sigmoid(head/temp) as "
                         "expected value, direct ignores the learned head.")
    ap.add_argument("--fetch-gate-temp", type=float, default=1.0,
                    help="Temperature for soft fetch gate; also affects hard "
                         "threshold probability monotonically but not the 0.5 boundary.")
    ap.add_argument("--quantum-cycles", type=int, default=256,
                    help="Quantum size Δt in cycles (default 256: amortizes "
                         "model forward / collate over many µops; use 1 for "
                         "legacy heap baseline)")
    ap.add_argument("--k-max", type=int, default=32,
                    help="Per-core, per-quantum optimistic probe depth")
    ap.add_argument("--ref-sim-backend", choices=["legacy", "coordinator", "timing-functional"],
                    default="timing-functional",
                    help="timing-functional=functional-trace-only Python backend (default), coordinator=LocalPybindBackend, legacy=PybindBackend")
    ap.add_argument("--tf-row-tick-stride", type=int, default=256)
    ap.add_argument("--tf-prefetch-degree", type=int, default=1)
    ap.add_argument("--tf-prefetch-coverage", type=float, default=0.25)
    ap.add_argument("--tf-snp-coverage", type=float, default=0.45)
    ap.add_argument("--tf-private-sideband-load-coverage", type=float, default=0.018)
    ap.add_argument("--tf-private-sideband-store-coverage", type=float, default=0.040)
    ap.add_argument("--tf-l1-load-fold-l2-coverage", type=float, default=0.41)
    ap.add_argument("--tf-l1-load-fold-llc-coverage", type=float, default=0.14)
    ap.add_argument("--tf-l1-load-fold-min-miss-rate", type=float, default=0.01)
    ap.add_argument("--tf-l1-load-fold-max-miss-rate", type=float, default=0.05)
    ap.add_argument("--tf-l1-load-fold-min-llc-l2-ratio", type=float, default=2.0)
    ap.add_argument("--refsim-warmup-records-per-core", type=int, default=0,
                    help="For timing-functional backend, replay the first N "
                         "rows per core to warm cache/directory state without "
                         "counting PMU counters.")
    ap.add_argument("--phase1a-workers", type=int, default=1,
                    help="ThreadPool workers for phase1a probe (D.5a default "
                         "is 1 for bit-exact gating; D.5b will default to 4)")
    ap.add_argument("--phase1c-workers", type=int, default=0,
                    help="ThreadPool workers for phase1c commit; 0 uses one "
                         "worker per active core")
    ap.add_argument("--persistent-session", action="store_true",
                    help="Keep the process alive, preload the model once, "
                         "then read one JSON job per stdin line")
    args = ap.parse_args()
    if args.persistent_session:
        run_persistent_session(args)
        return
    run_job(args)


if __name__ == "__main__":
    main()
