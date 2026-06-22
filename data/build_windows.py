"""build_windows.py — gem5 ROI 原始 trace -> 窗口级训练样本 jsonl。

严格遵守 docs/design.md 的修正：
  - 程序序切窗（按 micro_seq / pos_in_thread），不依赖 commit_tick 排序输入。
  - 多核拼接按 core_id 段串接（不按 cycle）。
  - 输入只用 functional 字段（records.micro 的架构态子集）。
  - 标签全部窗口聚合 PMU，主目标用比率（CPI / MPKI / miss-rate）。
  - cycles 用窗口内 commit_tick 端点差（除以 tick_per_cycle）。

输入目录结构（taogen gem5 输出）：
  <raw>/<WNAME>/tao_trace/board.processor.cores<C>.core.tao_trace.tao_trace.records.micro.jsonl
  <raw>/<WNAME>/tao_trace/...labels.micro.jsonl
（--ff-atomic 模式下 SimObject 路径会变成 board.processor.switch<C>.*，CORE_RE 同时兼容两种）

输出：
  <out>/windows.jsonl  每行一个样本（含 tokens / label / 元数据）
"""
from __future__ import annotations

import argparse
import bisect
import concurrent.futures as cf
import glob
import json
import os
import random
import re
import shutil
import sys
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

import pyarrow.parquet as pq

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from model import tokenizer as tk  # noqa: E402

CORE_RE = re.compile(r"(?:cores|switch)(\d+)\.core")
ALIGNED_PARQUET_COLS = [
    "core_id", "thread_id", "micro_seq", "seq_num",
    "macro_pc", "micro_pc", "vaddr", "paddr",
    "cacheline_addr", "cacheline_paddr", "size",
    "is_load", "is_store", "is_atomic",
    "is_branch", "is_branch_cond", "is_branch_indirect",
    "is_call", "is_return", "is_int", "is_fp",
    "is_simd", "is_serialize", "is_microop", "is_last_microop",
    "n_src", "n_dst", "producer_dists", "producer_classes",
    "path_class", "coh_oracle", "i_path_class",
    "d_mshr_depth", "dtlb_hit", "itlb_hit",
    "fetch_tick", "issue_tick", "complete_tick", "commit_tick",
    "ready_tick", "ready_source", "mispredicted",
]

# path_class 阈值（见 config/pmu_keys.yaml）
PC_L2 = 1      # >=1 视为 L1 miss
PC_DRAM = 4    # >=4 视为 LLC miss
# coh_oracle: 2=REMOTE_HIT_CLEAN 3=REMOTE_HIT_DIRTY
COH_REMOTE = {2, 3}

# label 维度顺序（与 pmu_keys.yaml keys 顺序一致）
PMU_KEYS = [
    "cpi", "mpki_br", "mr_l1d_ld", "mr_l1d_st",
    "mr_l1i", "mr_llc", "dtlb_miss", "itlb_miss",
    "inv_recv", "mshr_avg",
]


def load_core_files(trace_dir: str) -> Dict[int, dict]:
    """返回 {core_id: {'rec': path, 'lab': path}}。"""
    out: Dict[int, dict] = defaultdict(dict)
    for p in glob.glob(os.path.join(trace_dir, "*.aligned.parquet")):
        m = CORE_RE.search(p)
        if m:
            out[int(m.group(1))]["aligned"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.records.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            out[int(m.group(1))]["rec"] = p
    for p in glob.glob(os.path.join(trace_dir, "*.labels.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            out[int(m.group(1))]["lab"] = p
    keep = {}
    for c, v in out.items():
        if "aligned" in v:
            keep[c] = {"aligned": v["aligned"]}
        elif "rec" in v and "lab" in v:
            keep[c] = {"rec": v["rec"], "lab": v["lab"]}
    return keep


def read_jsonl(path: str) -> List[dict]:
    rows = []
    with open(path) as f:
        for ln in f:
            s = ln.strip()
            if not s.startswith("{"):
                continue
            try:
                rows.append(json.loads(s))
            except Exception:
                pass
    return rows


def read_aligned_parquet(path: str) -> List[dict]:
    rows = []
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(columns=ALIGNED_PARQUET_COLS, batch_size=65536):
        part = batch.to_pylist()
        for row in part:
            row["_commit_tick"] = row.get("commit_tick", 0)
            row["_mispredicted"] = row.get("mispredicted", 0)
        rows.extend(part)
    return rows


def merge_rec_lab(recs: List[dict], labs: List[dict]) -> List[dict]:
    """按 (thread_id, micro_seq) 对齐 records 与 labels。"""
    lab_idx = {(r["thread_id"], r["micro_seq"]): r for r in labs}
    merged = []
    for r in recs:
        key = (r["thread_id"], r["micro_seq"])
        lab = lab_idx.get(key)
        if lab is None:
            continue
        r["_commit_tick"] = lab.get("commit_tick", 0)
        r["_mispredicted"] = lab.get("mispredicted", 0)
        merged.append(r)
    # 程序序：按 (thread_id, micro_seq) 升序，不依赖 commit_tick
    merged.sort(key=lambda x: (x["thread_id"], x["micro_seq"]))
    return merged


def is_macro_head(rec: dict, prev: Optional[dict]) -> bool:
    """动态 macro 首条 µop：上一条 macro 结束或 macro_pc 变化。"""
    if prev is None:
        return True
    prev_ended = (prev.get("is_microop", 0) == 0
                  or prev.get("is_last_microop", 0) == 1)
    return prev_ended or rec.get("macro_pc") != prev.get("macro_pc")


def aggregate_pmu(window: List[dict], tick_per_cycle: int) -> Optional[dict]:
    """对一个核窗口聚合 PMU 标签（绝对计数 + 派生比率分母）。

    若窗口内存在任何 commit_tick<=0 的 µop（outer-join 救回的 lab 缺失项），
    直接返回 None 丢弃整个窗口，避免 cycles 端点差与 PMU 计数口径不一致
    污染训练标签。
    """
    if len(window) < 2:
        return None
    if any(w.get("_commit_tick", 0) <= 0 for w in window):
        return None
    ticks = [w["_commit_tick"] for w in window]
    t_start_tick = min(ticks)
    cycles = (max(ticks) - t_start_tick) / float(tick_per_cycle)
    if cycles <= 0:
        return None

    instr_retired = 0   # macro 指令数
    branch_count = loads = stores = mem_ops = fetch_groups = 0
    branch_miss = l1d_ld_miss = l1d_st_miss = l1i_miss = llc_miss = 0
    dtlb_miss = itlb_miss = inv_recv = 0
    mshr_sum = mshr_n = 0

    prev = None
    for w in window:
        head = is_macro_head(w, prev)
        if head:
            instr_retired += 1
            fetch_groups += 1
            # i-side miss 仅在 fetch-group head 计
            if int(w.get("i_path_class", 0)) >= PC_L2:
                l1i_miss += 1
            if int(w.get("itlb_hit", 1)) == 0:
                itlb_miss += 1
        is_ld = int(w.get("is_load", 0))
        is_st = int(w.get("is_store", 0))
        is_at = int(w.get("is_atomic", 0))
        if int(w.get("is_branch", 0)):
            branch_count += 1
            if int(w.get("_mispredicted", 0)):
                branch_miss += 1
        pc = int(w.get("path_class", 0))
        if is_ld:
            loads += 1
            if pc >= PC_L2:
                l1d_ld_miss += 1
        if is_st:
            stores += 1
            if pc >= PC_L2:
                l1d_st_miss += 1
        if is_ld or is_st or is_at:
            mem_ops += 1
            if pc >= PC_DRAM:
                llc_miss += 1
            if int(w.get("dtlb_hit", 1)) == 0:
                dtlb_miss += 1
            mshr_sum += int(w.get("d_mshr_depth", 0))
            mshr_n += 1
            if int(w.get("coh_oracle", 0)) in COH_REMOTE:
                inv_recv += 1
        prev = w

    if instr_retired == 0:
        return None

    def safe_div(a, b):
        return float(a) / float(b) if b > 0 else 0.0

    # 标签：绝对值 + 分母（分母来自 functional，可在推理时复算）
    return {
        "cycles": cycles,
        "instr_retired": instr_retired,
        "t_start_tick": float(t_start_tick),  # 该核窗口首条 commit_tick（绝对）
        # 比率主目标
        "cpi": safe_div(cycles, instr_retired),
        "mpki_br": safe_div(branch_miss, max(branch_count, 1)),
        "mr_l1d_ld": safe_div(l1d_ld_miss, max(loads, 1)),
        "mr_l1d_st": safe_div(l1d_st_miss, max(stores, 1)),
        "mr_l1i": safe_div(l1i_miss, max(fetch_groups, 1)),
        "mr_llc": safe_div(llc_miss, max(mem_ops, 1)),
        # 计数头（log1p 在 dataset 侧做）
        "dtlb_miss": float(dtlb_miss),
        "itlb_miss": float(itlb_miss),
        "inv_recv": float(inv_recv),
        # direct
        "mshr_avg": safe_div(mshr_sum, max(mshr_n, 1)),
        # 分母（供推理反算绝对值）
        "_denoms": {
            "branch_count": branch_count, "loads": loads,
            "stores": stores, "fetch_groups": fetch_groups,
            "mem_ops": mem_ops,
        },
    }


def build_samples_align(merged_by_core: Dict[int, List[dict]], wname: str,
                        cfg: dict, n_per_core: int,
                        stride_tick: int, target_windows: int = 0) -> List[dict]:
    """方案A：固定每核指令数 N + 近似跨核时间对齐。

    - 全局时间锚点 T_k = t_lo + k*stride_tick（在全局时间轴上推进）。
    - 每个核取 commit_tick >= T_k 的连续前 N 条 µop -> <Cc> 段。
    - 各核段长度恒为 N（上下文均等，解决固定 ΔT 的核间不均）；
      各核都从物理时刻 T_k 附近起步（近似对齐，解决指令切窗的跨核错位）。
    - 标签按该 N 条窗口聚合（CPI=cycles/N，语义与指令切窗一致，稳定）。

    stride_tick==0 且 target_windows>0 时，按本负载时间跨度自适应：
      stride_tick = (t_hi - t_lo) / target_windows
    解决不同负载 CPI 差异巨大导致固定 stride 窗口数失衡的问题。

    依据：验证2 显示跨核争用对时间差 median 仅 9 cycle，N 条覆盖的时间宽度
    虽因核速不同而异，争用双方仍大概率落在各核的同一 T_k 锚定窗内。
    """
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    # 预取每核 (idx -> commit_tick) 升序数组与 µop 序列（已程序序，tick 单调）
    seqs = {}
    ticks = {}
    for c in cores:
        s = [w for w in merged_by_core[c] if w["_commit_tick"] > 0]
        if len(s) < n_per_core + 1:
            return []
        seqs[c] = s
        ticks[c] = [w["_commit_tick"] for w in s]
    t_lo = min(ticks[c][0] for c in cores)
    t_hi = max(ticks[c][-1] for c in cores)

    if stride_tick <= 0:
        if target_windows <= 0:
            target_windows = 3000
        stride_tick = max(1, int((t_hi - t_lo) / target_windows))
    print(f"[align] {wname}: span={t_hi-t_lo} tick, "
          f"stride_tick={stride_tick}, N={n_per_core}", file=sys.stderr)

    import bisect
    samples = []
    seg = 0
    n_dropped = 0
    k = 0
    while True:
        T_k = t_lo + k * stride_tick
        if T_k > t_hi:
            break
        k += 1
        per_core_windows = {}
        ok = True
        for c in cores:
            # 该核第一条 commit_tick >= T_k 的位置
            j = bisect.bisect_left(ticks[c], T_k)
            if j + n_per_core > len(seqs[c]):
                ok = False
                break
            win = seqs[c][j:j + n_per_core]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            n_dropped += 1
            continue
        # 跨核相对起点（cycle），以本窗各核最小 t_start 为零点
        min_ts = min(per_core_windows[c][1]["t_start_tick"] for c in cores)
        t_start_rel = [
            (per_core_windows[c][1]["t_start_tick"] - min_ts) / float(tpc)
            for c in cores
        ]
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[kk] for kk in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},A_N{n_per_core},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": int(n_per_core),
            "n_per_core": int(n_per_core),
            "stride_tick": int(stride_tick),
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"]
                              for c in cores],
            "t_start_rel": t_start_rel,
        })
        seg += 1
    if n_dropped:
        print(f"[align] {wname}: dropped {n_dropped} windows (尾部不足N), "
              f"kept {len(samples)}", file=sys.stderr)
    return samples


def build_samples_timewin(merged_by_core: Dict[int, List[dict]], wname: str,
                          cfg: dict, dt_tick: int,
                          max_per_core: int) -> List[dict]:
    """方案1 v1：按全局物理时间区间 [T0+k·ΔT, T0+(k+1)·ΔT) 切窗。

    与 build_samples（按指令下标切）不同：同一窗口内各核装的是
    commit_tick 落在同一物理时间区间的 µop，从而跨核物理时间对齐。

    - dt_tick: 时间窗宽 ΔT（单位 tick）。
    - max_per_core: 每核段指令数上限（token 预算），超出截断尾部。
      标签按截断后窗口聚合（保持 input 与 label 一致）。
    """
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    # 各核已按程序序排好（merge_rec_lab 保证），commit_tick 单调
    # 取每核首/尾有效 tick，定全局时间范围
    core_ticks = {}
    for c in cores:
        ts = [w["_commit_tick"] for w in merged_by_core[c]
              if w["_commit_tick"] > 0]
        if len(ts) < 2:
            return []
        core_ticks[c] = ts
    t_lo = min(ct[0] for ct in core_ticks.values())
    t_hi = max(ct[-1] for ct in core_ticks.values())
    n_win = int((t_hi - t_lo) // dt_tick) + 1

    # 每核维护一个游标，顺序扫过 µop 落入时间窗（commit_tick 单调，O(N)）
    cursor = {c: 0 for c in cores}
    samples = []
    seg = 0
    n_dropped = 0
    for k in range(n_win):
        w_lo = t_lo + k * dt_tick
        w_hi = w_lo + dt_tick
        per_core_windows = {}
        ok = True
        for c in cores:
            seq = merged_by_core[c]
            i = cursor[c]
            win = []
            while i < len(seq):
                ct = seq[i]["_commit_tick"]
                if ct <= 0:
                    i += 1
                    continue
                if ct >= w_hi:
                    break
                if ct >= w_lo:
                    win.append(seq[i])
                i += 1
            cursor[c] = i
            # token 预算截断（保留窗口内最早的 max_per_core 条）
            if max_per_core and len(win) > max_per_core:
                win = win[:max_per_core]
            pmu = aggregate_pmu(win, int(cfg.get("tick_per_cycle", 333)))
            if pmu is None:
                ok = False
            per_core_windows[c] = (win, pmu)
        if not ok:
            n_dropped += 1
            continue
        # 时间窗下 cycles 应≈ΔT/tpc（固定）；用 aggregate 的端点差即可
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[kk] for kk in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},DT{dt_tick},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": int(dt_tick),          # 复用字段：此处存 ΔT(tick)
            "dt_tick": int(dt_tick),
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"]
                              for c in cores],
            # 时间窗下各核起点已天然对齐到 w_lo，保留 0 占位以兼容旧 schema
            "t_start_rel": [0.0] * len(cores),
        })
        seg += 1
    if n_dropped:
        print(f"[timewin] {wname}: dropped {n_dropped}/{n_win} windows "
              f"(某核<2指令), kept {len(samples)}", file=sys.stderr)
    return samples


def take_macro_window_by_budget(seq: List[dict], start: int,
                                budget_tok: int) -> Tuple[int, int]:
    """按程序序累加真实 encode_uop 长度，装到 budget_tok 即收口到上一条
    完整 macro 边界。返回 (end_index, got_macro)。

    与 tpm 反算不同：每条 µop 的 token 长度精确测量，不依赖估计；
    收口对齐 macro head，保证窗口不切半条 macro（与 aggregate_pmu/部署一致）。
    """
    n = len(seq)
    if start >= n:
        return start, 0
    prev = seq[start - 1] if start > 0 else None
    tok = 0
    macro_n = 0
    last_safe_end = start          # 上一条完整 macro 收尾位置（含）
    last_safe_macros = 0           # 到 last_safe_end 时累计的完整 macro 数
    i = start
    while i < n:
        rec = seq[i]
        is_head = is_macro_head(rec, prev)
        if is_head and i > start:
            # 进入新 macro：当前 i 之前的 µop 构成一段完整 macro
            last_safe_end = i
            last_safe_macros = macro_n
        tok_len = len(tk.encode_uop(rec))
        if tok + tok_len > budget_tok and last_safe_end > start:
            # 装不下当前 µop，且已有至少一条完整 macro，停在 last_safe_end
            return last_safe_end, last_safe_macros
        tok += tok_len
        if is_head:
            macro_n += 1
        prev = rec
        i += 1
    # 扫到 trace 末尾：若末尾本身是完整 macro 收尾，直接用 i；否则回退
    if i >= n:
        # 末尾整段已被吃完；要求至少一条完整 macro
        if macro_n >= 1:
            return i, macro_n
        return last_safe_end, last_safe_macros
    return last_safe_end, last_safe_macros


def _prev_macro_end(seq: List[dict], end: int) -> int:
    """Return an exclusive end index that does not cut the tail macro."""
    end = min(end, len(seq))
    while end > 0:
        rec = seq[end - 1]
        if rec.get("is_microop", 0) == 0 or rec.get("is_last_microop", 0) == 1:
            break
        end -= 1
    return end


def _macro_start(seq: List[dict], end: int) -> int:
    """Return the inclusive start index of the macro ending at exclusive end."""
    i = end - 1
    while i > 0 and not is_macro_head(seq[i], seq[i - 1]):
        i -= 1
    return i


def take_macro_window_back_by_budget(seq: List[dict], end: int,
                                     budget_tok: int) -> Tuple[int, int, int]:
    """Backward quota window ending at a macro boundary.

    Returns (start_index, end_index, got_macro). The selected slice
    seq[start:end] is as long as possible under budget_tok and starts/ends on
    dynamic macro boundaries. This keeps the most recent context before a
    tail-time anchor.
    """
    end = _prev_macro_end(seq, end)
    if end <= 0:
        return end, end, 0

    start = end
    tok = 0
    macro_n = 0
    while start > 0:
        m_start = _macro_start(seq, start)
        macro_tok = 0
        for rec in seq[m_start:start]:
            macro_tok += len(tk.encode_uop(rec))
        if tok + macro_tok > budget_tok and macro_n > 0:
            break
        if tok + macro_tok > budget_tok and macro_n == 0:
            return start, end, 0
        tok += macro_tok
        macro_n += 1
        start = m_start
    return start, end, macro_n


def sample_tq_fill(rng: random.Random) -> float:
    """Training fill jitter matched to deployment: mostly 85%+ loaded."""
    u = rng.random()
    if u < 0.70:
        return rng.uniform(0.92, 1.00)
    if u < 0.95:
        return rng.uniform(0.85, 0.92)
    return rng.uniform(0.75, 0.85)


def encode_multicore_sample(tokens: List[str], labels: List[List[float]],
                            per_core_windows: Dict[int, Tuple[List[dict], dict]],
                            cores: List[int], cfg: dict, sample_meta: dict) -> dict:
    """Shared sample serialization for multi-core window builders."""
    out_tokens: List[str] = ["<SYS>"] + tk.cfg_tokens(cfg) + ["<TRACE>"]
    core_split = []
    for ci, c in enumerate(cores):
        win, _pmu = per_core_windows[c]
        out_tokens.append(f"<C{ci}_BEGIN>")
        for w in win:
            out_tokens.extend(tk.encode_uop(w))
        out_tokens.append(f"<C{ci}_END>")
        core_split.append(len(win))
    out_tokens.append("<TRACE_END>")
    for ci in range(len(cores)):
        out_tokens.append(f"<QUERY_C{ci}>")

    sample = dict(sample_meta)
    sample.update({
        "tokens": out_tokens,
        "core_split": core_split,
        "label": labels,
        "label_keys": PMU_KEYS,
        "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
        "instr_retired": [per_core_windows[c][1]["instr_retired"]
                          for c in cores],
    })
    return sample


def build_samples_tq(merged_by_core: Dict[int, List[dict]], wname: str,
                     cfg: dict, max_len: int,
                     target_windows: int = 1200,
                     stride_tick: int = 0,
                     ratio_lo: float = 0.5,
                     ratio_hi: float = 2.0,
                     min_fill: float = 0.70,
                     max_end_skew_cycle: float = 0.0,
                     overhead: int = 64,
                     budget_frac: float = 0.95,
                     rng_seed: int = 0) -> List[dict]:
    """方案TQ：tail-aligned quota，最终默认切窗策略。

    - 以全局 T_end 为锚点，每核取 commit_tick <= T_end 的最后完整 macro
      作为窗口尾部，使窗口尾部在物理时间上尽可能对齐。
    - 从尾部向前按 token budget 回溯，保留最近上下文并尽量填满 max_len。
    - target_fill 抖动匹配部署侧 85%+ 的常见装载率，避免训练只见满窗。
    """
    rng = random.Random(rng_seed)
    tpc = int(cfg.get("tick_per_cycle", 333))
    cores = sorted(merged_by_core.keys())
    n_core = len(cores)
    if n_core < 2:
        return []

    seqs: Dict[int, List[dict]] = {}
    ticks: Dict[int, List[int]] = {}
    for c in cores:
        seq = [w for w in merged_by_core[c] if w.get("_commit_tick", 0) > 0]
        if len(seq) < 2:
            return []
        seqs[c] = seq
        ticks[c] = [int(w["_commit_tick"]) for w in seq]

    t_lo = max(ticks[c][0] for c in cores)
    t_hi = min(ticks[c][-1] for c in cores)
    if t_hi <= t_lo:
        return []
    if stride_tick <= 0:
        target_windows = max(1, int(target_windows or 1200))
        stride_tick = max(1, int((t_hi - t_lo) / target_windows))

    base_budget = int((max_len - overhead) * budget_frac)
    print(f"[tq] {wname}: max_len={max_len} base_budget={base_budget} "
          f"stride_tick={stride_tick} target_windows={target_windows} "
          f"ratio=[{ratio_lo:.2f},{ratio_hi:.2f}]",
          file=sys.stderr)

    samples: List[dict] = []
    dropped_low_fill = 0
    dropped_skew = 0
    dropped_bad = 0
    seg = 0
    k = 0
    while True:
        T_end = t_lo + k * stride_tick
        if T_end > t_hi:
            break
        k += 1

        target_fill = sample_tq_fill(rng)
        total_budget = max(1, int(base_budget * target_fill))
        ratios = [rng.uniform(ratio_lo, ratio_hi) for _ in cores]
        ratio_sum = sum(ratios)
        budgets = {
            c: max(1, int(round(total_budget * ratios[ci] / ratio_sum)))
            for ci, c in enumerate(cores)
        }

        per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
        ok = True
        for c in cores:
            end = bisect.bisect_right(ticks[c], T_end)
            start, end, got = take_macro_window_back_by_budget(
                seqs[c], end, budgets[c])
            if got < 1 or end <= start:
                ok = False
                break
            win = seqs[c][start:end]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            dropped_bad += 1
            continue

        t_starts = [per_core_windows[c][1]["t_start_tick"] for c in cores]
        t_ends = [
            max(int(w["_commit_tick"]) for w in per_core_windows[c][0])
            for c in cores
        ]
        end_skew_cycle = (max(t_ends) - min(t_ends)) / float(tpc)
        if max_end_skew_cycle > 0 and end_skew_cycle > max_end_skew_cycle:
            dropped_skew += 1
            continue

        labels = [
            [per_core_windows[c][1][kk] for kk in PMU_KEYS]
            for c in cores
        ]
        min_tstart = min(t_starts)
        min_tend = min(t_ends)
        sample = encode_multicore_sample(
            tokens=[],
            labels=labels,
            per_core_windows=per_core_windows,
            cores=cores,
            cfg=cfg,
            sample_meta={
                "id": f"{wname},TQ,seg{seg:05d}",
                "workload": wname,
                "cfg_hash": cfg.get("cfg_hash", "A0"),
                "n_core": n_core,
                "w_ops": int(sum(len(per_core_windows[c][0])
                                 for c in cores) / n_core),
                "max_len": int(max_len),
                "mode": "tq",
                "target_fill": float(target_fill),
                "t_end_tick": int(T_end),
                "stride_tick": int(stride_tick),
                "t_start_rel": [
                    (ts - min_tstart) / float(tpc) for ts in t_starts
                ],
                "t_end_rel": [
                    (te - min_tend) / float(tpc) for te in t_ends
                ],
                "end_skew_cycle": float(end_skew_cycle),
            },
        )
        sample["fill_ratio"] = len(sample["tokens"]) / float(max_len)
        if len(sample["tokens"]) > max_len:
            dropped_bad += 1
            continue
        if sample["fill_ratio"] < min_fill:
            dropped_low_fill += 1
            continue
        samples.append(sample)
        seg += 1

    print(f"[tq] {wname}: produced {len(samples)} windows "
          f"(drop_bad={dropped_bad}, drop_low_fill={dropped_low_fill}, "
          f"drop_skew={dropped_skew})", file=sys.stderr)
    return samples


def build_samples_quota(merged_by_core: Dict[int, List[dict]], wname: str,
                        cfg: dict, max_len: int,
                        ratio_lo: float, ratio_hi: float,
                        overhead: int = 64, budget_frac: float = 0.95,
                        rng_seed: int = 0) -> List[dict]:
    """方案Q（B 简化版）：token-budget 全核归一化分配 + macro 抖动 + 真值 t_start_rel。

    与"等分 budget/N × ratio"路径区别：
      ratio_c ~ U(lo, hi)  每核独立采样
      budget_c = total_budget · ratio_c / Σ ratio_c   归一化保证总预算不超
    单核可拿到接近 ratio_hi/(N·mean) × total_budget 的 token（远超 budget/N），
    覆盖部署 OnlineQuotaPlanner 跨核借调时落后核拿到的大窗样本，避免 OOD。

    每核段独立切（不强求跨核 cycle 对齐），t_start_rel 仍用真值 commit_tick。
    贪心装窗：累加真实 encode_uop 长度直到 budget_c，收口到上一条完整 macro 边界。
    """
    rng = random.Random(rng_seed)
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    n_core = len(cores)
    if n_core < 2:
        return []

    total_budget = int((max_len - overhead) * budget_frac)
    print(f"[quota] {wname}: total_budget={total_budget} "
          f"ratio=[{ratio_lo:.2f},{ratio_hi:.2f}] cores={n_core}",
          file=sys.stderr)

    cursor = {c: 0 for c in cores}
    samples: List[dict] = []
    seg = 0
    while True:
        per_core_windows: Dict[int, Tuple[List[dict], dict]] = {}
        ok = True
        ratios = [rng.uniform(ratio_lo, ratio_hi) for _ in cores]
        ratio_sum = sum(ratios)
        for ci, c in enumerate(cores):
            budget_c = max(1, int(round(total_budget * ratios[ci] / ratio_sum)))
            seq = merged_by_core[c]
            end, got = take_macro_window_by_budget(seq, cursor[c], budget_c)
            if got < 1 or end <= cursor[c]:
                ok = False
                break
            win = seq[cursor[c]:end]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
            cursor[c] = end
        if not ok:
            break

        # 真值跨核相对起点（cycle）
        min_ts = min(per_core_windows[c][1]["t_start_tick"] for c in cores)
        t_start_rel = [
            (per_core_windows[c][1]["t_start_tick"] - min_ts) / float(tpc)
            for c in cores
        ]

        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[kk] for kk in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(n_core):
            tokens.append(f"<QUERY_C{ci}>")

        # 安全网：理论上 budget_per_core·ratio_hi·N + overhead ≤ max_len
        # 仍 assert 一道避免 overhead 估错时静默 OOC。
        if len(tokens) > max_len:
            seg += 1
            continue

        samples.append({
            "id": f"{wname},Q,seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": n_core,
            "w_ops": int(sum(core_split) / n_core),
            "max_len": int(max_len),
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"]
                              for c in cores],
            "t_start_rel": t_start_rel,
        })
        seg += 1
    print(f"[quota] {wname}: produced {len(samples)} windows", file=sys.stderr)
    return samples


def build_samples(merged_by_core: Dict[int, List[dict]], wname: str,
                  cfg: dict, W: int, stride: int) -> List[dict]:
    """对齐多核程序序起点切窗，每窗一个样本。"""
    tpc = int(cfg.get("tick_per_cycle", 333))
    cfg_tok = tk.cfg_tokens(cfg)
    cores = sorted(merged_by_core.keys())
    min_len = min(len(merged_by_core[c]) for c in cores)
    samples = []
    seg = 0
    for t in range(0, min_len - W + 1, stride):
        tokens: List[str] = ["<SYS>"] + cfg_tok + ["<TRACE>"]
        labels = []
        core_split = []
        ok = True
        per_core_windows = {}
        for c in cores:
            win = merged_by_core[c][t:t + W]
            pmu = aggregate_pmu(win, tpc)
            if pmu is None:
                ok = False
                break
            per_core_windows[c] = (win, pmu)
        if not ok:
            continue
        # 跨核相对起始时间：以本窗口 8 核最小 t_start 为零点，单位 cycle
        min_tstart = min(per_core_windows[c][1]["t_start_tick"] for c in cores)
        t_start_rel = [
            (per_core_windows[c][1]["t_start_tick"] - min_tstart) / float(tpc)
            for c in cores
        ]
        for ci, c in enumerate(cores):
            win, pmu = per_core_windows[c]
            tokens.append(f"<C{ci}_BEGIN>")
            for w in win:
                tokens.extend(tk.encode_uop(w))
            tokens.append(f"<C{ci}_END>")
            core_split.append(len(win))
            labels.append([pmu[k] for k in PMU_KEYS])
        tokens.append("<TRACE_END>")
        for ci in range(len(cores)):
            tokens.append(f"<QUERY_C{ci}>")
        samples.append({
            "id": f"{wname},W{W},seg{seg:05d}",
            "workload": wname,
            "cfg_hash": cfg.get("cfg_hash", "A0"),
            "n_core": len(cores),
            "w_ops": W,
            "tokens": tokens,
            "core_split": core_split,
            "label": labels,                 # [n_core, K]
            "label_keys": PMU_KEYS,
            "denoms": [per_core_windows[c][1]["_denoms"] for c in cores],
            "instr_retired": [per_core_windows[c][1]["instr_retired"] for c in cores],
            "t_start_rel": t_start_rel,      # [n_core] 跨核相对起始时间(cycle)
        })
        seg += 1
    return samples


def process_workload(wd: str, raw_root: str, out_dir: str,
                     cfg: dict, window: int, stride: int,
                     dt_tick: int = 0, max_per_core: int = 0,
                     align_n: int = 0, stride_tick: int = 0,
                     target_windows: int = 0,
                     quota_max_len: int = 0,
                     quota_ratio_lo: float = 0.5,
                     quota_ratio_hi: float = 1.0,
                     quota_seed: int = 0,
                     tq_max_len: int = 0,
                     tq_target_windows: int = 1200,
                     tq_stride_tick: int = 0,
                     tq_ratio_lo: float = 0.5,
                     tq_ratio_hi: float = 2.0,
                     tq_min_fill: float = 0.70,
                     tq_max_end_skew_cycle: float = 0.0,
                     tq_seed: int = 0) -> tuple:
    """单个 workload 构建 shard，返回 (wd, ok, samples, shard_path, message)。

    模式优先级：quota_max_len>0 走旧方案Q；否则 align_n>0 走方案A；
    否则 dt_tick>0 走时间切窗；否则 tq_max_len>0 走默认最终方案TQ；
    否则指令切窗。
    """
    trace_dir = os.path.join(raw_root, wd, "tao_trace")
    if not os.path.isdir(trace_dir):
        return wd, False, 0, "", f"[skip] {wd}: no tao_trace"

    files = load_core_files(trace_dir)
    if len(files) < 2:
        return wd, False, 0, "", f"[skip] {wd}: <2 cores"

    t0 = time.time()
    print(f"[start] {wd}: reading {len(files)} cores ...", file=sys.stderr)
    merged_by_core = {}
    for ci, (c, fp) in enumerate(sorted(files.items())):
        if "aligned" in fp:
            merged_by_core[c] = read_aligned_parquet(fp["aligned"])
        else:
            recs = read_jsonl(fp["rec"])
            labs = read_jsonl(fp["lab"])
            merged_by_core[c] = merge_rec_lab(recs, labs)
        print(f"[read] {wd}: core {ci+1}/{len(files)} "
              f"({len(merged_by_core[c])} µops, {time.time()-t0:.0f}s)",
              file=sys.stderr)

    print(f"[build] {wd}: read done in {time.time()-t0:.0f}s, slicing ...",
          file=sys.stderr)

    if quota_max_len > 0:
        samples = build_samples_quota(
            merged_by_core, wd, cfg, quota_max_len,
            quota_ratio_lo, quota_ratio_hi, rng_seed=quota_seed,
        )
    elif align_n > 0:
        samples = build_samples_align(merged_by_core, wd, cfg,
                                      align_n, stride_tick, target_windows)
    elif dt_tick > 0:
        samples = build_samples_timewin(merged_by_core, wd, cfg,
                                        dt_tick, max_per_core)
    elif tq_max_len > 0:
        samples = build_samples_tq(
            merged_by_core, wd, cfg, tq_max_len,
            target_windows=tq_target_windows,
            stride_tick=tq_stride_tick,
            ratio_lo=tq_ratio_lo,
            ratio_hi=tq_ratio_hi,
            min_fill=tq_min_fill,
            max_end_skew_cycle=tq_max_end_skew_cycle,
            rng_seed=tq_seed,
        )
    else:
        samples = build_samples(merged_by_core, wd, cfg, window, stride)
    shard_path = os.path.join(out_dir, f"{wd}.jsonl")
    with open(shard_path, "w") as fout:
        for s in samples:
            fout.write(json.dumps(s, separators=(",", ":")) + "\n")
    return wd, True, len(samples), shard_path, f"[ok] {wd}: cores={len(files)} samples={len(samples)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/data00/yinhaolang/LLMSim/data/raw")
    ap.add_argument("--out", default="/data00/yinhaolang/LLMSim/data/windows")
    ap.add_argument("--window", type=int, default=1024)
    ap.add_argument("--stride", type=int, default=512)
    ap.add_argument("--dt-tick", type=int, default=0,
                    help=">0 启用时间切窗，ΔT 单位 tick（如 100000）")
    ap.add_argument("--max-per-core", type=int, default=0,
                    help="时间切窗每核段指令上限（token 预算），0=不截断")
    ap.add_argument("--align-n", type=int, default=0,
                    help=">0 启用方案A：固定每核指令数 N + 近似时间对齐")
    ap.add_argument("--stride-tick", type=int, default=0,
                    help="方案A 全局时间锚点步长（tick），0=按 target-windows 自适应")
    ap.add_argument("--target-windows", type=int, default=3000,
                    help="方案A 每负载目标窗口数（stride-tick=0 时生效）")
    ap.add_argument("--quota-max-len", type=int, default=0,
                    help=">0 启用方案Q：按 token-budget 装满 + macro 抖动")
    ap.add_argument("--quota-ratio-lo", type=float, default=0.3,
                    help="方案Q 每核归一化前的 ratio 下界（U(lo, hi)）")
    ap.add_argument("--quota-ratio-hi", type=float, default=1.7,
                    help="方案Q 每核归一化前的 ratio 上界")
    ap.add_argument("--quota-seed", type=int, default=0,
                    help="方案Q 抖动随机种子")
    ap.add_argument("--tq-max-len", type=int, default=32768,
                    help="默认最终方案TQ：tail-aligned quota 的 max-len；0=关闭")
    ap.add_argument("--tq-target-windows", type=int, default=1200,
                    help="方案TQ 每负载目标窗口数（tq-stride-tick=0 时生效）")
    ap.add_argument("--tq-stride-tick", type=int, default=0,
                    help="方案TQ 全局尾部时间锚点步长，0=按 tq-target-windows 自适应")
    ap.add_argument("--tq-ratio-lo", type=float, default=0.5,
                    help="方案TQ 每核 token budget ratio 下界")
    ap.add_argument("--tq-ratio-hi", type=float, default=2.0,
                    help="方案TQ 每核 token budget ratio 上界")
    ap.add_argument("--tq-min-fill", type=float, default=0.70,
                    help="方案TQ 低于该 fill_ratio 的训练窗丢弃")
    ap.add_argument("--tq-max-end-skew-cycle", type=float, default=0.0,
                    help="方案TQ 尾部 commit 时间最大偏斜；0=只记录不丢弃")
    ap.add_argument("--tq-seed", type=int, default=0,
                    help="方案TQ fill/budget 抖动随机种子")
    ap.add_argument("--no-cache", action="store_true",
                    help="跳过自动生成 ids cache（仅产 jsonl）")
    ap.add_argument("--cache-max-len", type=int, default=0,
                    help="ids cache 的 max-len；默认取 quota-max-len 或 tq-max-len")
    ap.add_argument("--workloads", nargs="*", default=None)
    ap.add_argument("--uarch-config", default="arch_A")
    ap.add_argument("--jobs", type=int, default=max(1, min(os.cpu_count() or 1, 8)))
    args = ap.parse_args()

    import yaml
    cfg_path = "/data00/yinhaolang/LLMSim/config/uarch_configs.yaml"
    with open(cfg_path) as f:
        all_cfg = yaml.safe_load(f)
    cfg = all_cfg["configs"][args.uarch_config]

    os.makedirs(args.out, exist_ok=True)
    out_path = os.path.join(args.out, "windows.jsonl")
    shard_dir = os.path.join(args.out, ".shards")
    wdirs = sorted([d for d in os.listdir(args.raw)
                    if os.path.isdir(os.path.join(args.raw, d))
                    and d.startswith("W") and not d.startswith("probe_")])
    if args.workloads:
        wdirs = [d for d in wdirs if d in args.workloads]

    if not wdirs:
        with open(out_path, "w"):
            pass
        print(f"[done] total samples=0 -> {out_path}")
        return

    if os.path.isdir(shard_dir):
        shutil.rmtree(shard_dir)
    os.makedirs(shard_dir, exist_ok=True)

    total = 0
    results = {}
    jobs = max(1, min(args.jobs, len(wdirs)))
    with cf.ProcessPoolExecutor(max_workers=jobs) as ex:
        future_map = {
            ex.submit(process_workload, wd, args.raw, shard_dir,
                      cfg, args.window, args.stride,
                      args.dt_tick, args.max_per_core,
                      args.align_n, args.stride_tick,
                      args.target_windows,
                      args.quota_max_len,
                      args.quota_ratio_lo, args.quota_ratio_hi,
                      args.quota_seed,
                      args.tq_max_len,
                      args.tq_target_windows,
                      args.tq_stride_tick,
                      args.tq_ratio_lo,
                      args.tq_ratio_hi,
                      args.tq_min_fill,
                      args.tq_max_end_skew_cycle,
                      args.tq_seed): wd
            for wd in wdirs
        }
        for fut in cf.as_completed(future_map):
            wd, ok, nsamp, shard_path, msg = fut.result()
            results[wd] = (ok, nsamp, shard_path, msg)
            stream = sys.stdout if ok else sys.stderr
            print(msg, file=stream)

    with open(out_path, "w") as fout:
        for wd in wdirs:
            ok, nsamp, shard_path, _ = results[wd]
            if not ok:
                continue
            with open(shard_path) as fin:
                shutil.copyfileobj(fin, fout)
            total += nsamp

    shutil.rmtree(shard_dir, ignore_errors=True)
    print(f"[done] total samples={total} -> {out_path}")

    if total > 0 and not args.no_cache:
        cache_max_len = args.cache_max_len or args.quota_max_len or args.tq_max_len or 0
        if cache_max_len <= 0:
            print("[cache] skip: 无法推断 max-len；指定 --cache-max-len、--quota-max-len 或 --tq-max-len",
                  file=sys.stderr)
        else:
            import subprocess
            repo_root = "/data00/yinhaolang/LLMSim"
            cache_dir = f"{out_path[:-6]}.maxlen{cache_max_len}.ids_cache"
            print(f"[cache] preparing ids cache @ max-len={cache_max_len} -> {cache_dir}",
                  flush=True)
            cmd = [
                sys.executable,
                f"{repo_root}/scripts/prepare_dataset_cache.py",
                "--data", out_path,
                "--max-len", str(cache_max_len),
            ]
            rc = subprocess.run(cmd, cwd=repo_root).returncode
            if rc != 0:
                print(f"[cache] FAILED rc={rc}", file=sys.stderr)
                sys.exit(rc)
            print(f"[cache] done -> {cache_dir}")


if __name__ == "__main__":
    main()
