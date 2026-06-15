"""analyze_sharing.py — 纯离线分析：量化跨核共享行为，回答两个问题：
  Q1. warm-up 回看 L 该多大？ -> 看共享行的「写-写间隔」分布（按全局程序序）。
       L 取该分布的高分位数（如 p90），即可让多数活跃共享行在窗内见到最近一次写。
  Q2. 解法 B（循环状态）值不值得上？ -> 看「冷行唤醒」比例：
       一致性事件中有多少发生在「距上次访问极远（> 候选 L）」的行上。
       若该比例高，说明有限回看漏掉的长尾多，B 才值得；否则 A+C 足够。

定义（全部基于程序行为，不读 MESI 状态）：
  - 全局程序序：把各核 records 按 (core 内 micro_seq) 归并；跨核用轮转近似全局序
    （无 cycle 泄漏，仅用于"间隔"的粗略尺度）。这里用更稳妥的口径：
    对每条 cacheline_paddr，记录其被访问的「全局事件下标」序列。
  - 写-写间隔：同一行相邻两次 store 之间隔了多少个「该行的全局访问事件」以及
    多少个「全局 µop」。后者才是 warm-up L 的单位（µop 数）。
  - 共享行：被 >=2 个不同核访问过的行。
  - 冷行唤醒：一次访问距同一行上次访问的「全局 µop 间隔」super 大。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
from collections import defaultdict

import numpy as np

CORE_RE = re.compile(r"cores(\d+)\.core")


def load_core_streams(trace_dir):
    """返回 {core_id: [(micro_seq, is_store, cl_paddr), ...]}（按 micro_seq 升序）。"""
    out = {}
    for p in glob.glob(os.path.join(trace_dir, "*.records.micro.jsonl")):
        m = CORE_RE.search(p)
        if not m:
            continue
        c = int(m.group(1))
        rows = []
        with open(p) as f:
            for ln in f:
                s = ln.strip()
                if not s.startswith("{"):
                    continue
                try:
                    r = json.loads(s)
                except Exception:
                    continue
                cl = int(r.get("cacheline_paddr", 0))
                if cl == 0:
                    continue
                is_st = int(r.get("is_load", 0) or r.get("is_store", 0)
                            or r.get("is_atomic", 0))
                if not is_st:
                    continue
                rows.append((int(r["micro_seq"]), int(r.get("is_store", 0)
                             or r.get("is_atomic", 0)), cl))
        rows.sort()
        out[c] = rows
    return out


def build_global_order(streams):
    """把多核访存事件按「程序序轮转归并」成一个全局序列。
    口径：用每核 micro_seq 做归并键（不同核 micro_seq 同尺度，近似并发推进）。
    返回 [(global_idx, core, is_store, cl)]。"""
    merged = []
    for c, rows in streams.items():
        for (ms, st, cl) in rows:
            merged.append((ms, c, st, cl))
    merged.sort(key=lambda x: (x[0], x[1]))   # 按 micro_seq 再按 core
    return [(i, c, st, cl) for i, (ms, c, st, cl) in enumerate(merged)]


def analyze(trace_dir, name, cand_L=(256, 512, 1024, 2048, 4096, 8192)):
    streams = load_core_streams(trace_dir)
    if len(streams) < 2:
        print(f"[{name}] <2 cores, skip")
        return
    glob_ev = build_global_order(streams)
    N = len(glob_ev)

    cores_of_line = defaultdict(set)
    last_access_gidx = {}
    last_write_gidx = {}
    ww_gap = []                 # 写-写 间隔（全局 µop 单位）
    access_gap_all = []         # 任意访问间隔
    sharers_hist = defaultdict(int)
    # 一致性"必要事件"：对共享行的访问，且上次访问来自其它核（潜在 coherence 事件）
    coh_event_gap = []          # 这些事件距上次同行访问的间隔（看冷唤醒）
    last_access_core = {}

    for (g, c, st, cl) in glob_ev:
        cores_of_line[cl].add(c)
        if cl in last_access_gidx:
            gap = g - last_access_gidx[cl]
            access_gap_all.append(gap)
            # 共享 coherence 必要事件：上次访问来自别的核
            if last_access_core.get(cl) is not None and last_access_core[cl] != c:
                coh_event_gap.append(gap)
        if st:
            if cl in last_write_gidx:
                ww_gap.append(g - last_write_gidx[cl])
            last_write_gidx[cl] = g
        last_access_gidx[cl] = g
        last_access_core[cl] = c

    # 共享度统计
    n_lines = len(cores_of_line)
    n_shared = sum(1 for s in cores_of_line.values() if len(s) >= 2)
    for s in cores_of_line.values():
        sharers_hist[min(len(s), 4)] += 1

    def pct(arr, ps=(50, 75, 90, 95, 99)):
        if not arr:
            return {p: None for p in ps}
        a = np.array(arr)
        return {p: int(np.percentile(a, p)) for p in ps}

    print(f"\n========== [{name}] ==========")
    print(f"total mem events (global)  : {N}")
    print(f"distinct cachelines        : {n_lines}")
    print(f"shared lines (>=2 cores)   : {n_shared} ({100*n_shared/max(n_lines,1):.1f}%)")
    print(f"sharers hist (1/2/3/4+)    : "
          f"{sharers_hist[1]}/{sharers_hist[2]}/{sharers_hist[3]}/{sharers_hist[4]}")
    print(f"write-write gap (µop) pct  : {pct(ww_gap)}   n={len(ww_gap)}")
    print(f"coh-event gap (µop)  pct   : {pct(coh_event_gap)}   n={len(coh_event_gap)}")

    # Q1: warm-up L 覆盖率 —— L 覆盖多少比例的写-写间隔
    print("  -- Q1: warm-up L 对 write-write gap 的覆盖率 --")
    ww = np.array(ww_gap) if ww_gap else np.array([0])
    for L in cand_L:
        cov = float((ww <= L).mean()) if ww_gap else 1.0
        print(f"     L={L:5d}: 覆盖 {cov*100:5.1f}% 的写-写间隔")

    # Q2: 冷唤醒 —— coherence 必要事件中, 距上次访问 > L 的比例（B 才能救）
    print("  -- Q2: coherence 事件的「冷唤醒」比例（gap > L，warm-up 救不到）--")
    ce = np.array(coh_event_gap) if coh_event_gap else np.array([0])
    for L in cand_L:
        miss = float((ce > L).mean()) if coh_event_gap else 0.0
        print(f"     L={L:5d}: {miss*100:5.1f}% 的 coh 事件落在回看窗外（需解法B兜底）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="/data00/yinhaolang/LLMSim/data/raw")
    ap.add_argument("--runs", nargs="*",
                    default=["pc4", "fs4", "_roi_check3"])
    args = ap.parse_args()
    for r in args.runs:
        td = os.path.join(args.raw, r, "tao_trace")
        if os.path.isdir(td):
            analyze(td, r)
        else:
            print(f"[skip] {r}: no tao_trace")


if __name__ == "__main__":
    main()
