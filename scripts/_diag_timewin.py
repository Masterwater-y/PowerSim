"""_diag_timewin.py — 方案1零成本验证（doc §10.7）。

不训练，仅用 gem5 真值 commit_tick 做两件事：
  验证1 ΔT 标定：对候选 ΔT，统计各核每个时间窗的指令数分布 -> 定 MAXLEN/窗口数。
  验证2 时间切窗 vs 指令切窗的跨核争用对齐度：
        统计「同一窗口内不同核访问同一 cacheline」的对数（争用对齐信号），
        对比两种切窗方式，确认时间切窗确实把争用拉回同窗。

用法：
  python scripts/_diag_timewin.py \
    --trace data/raw_8w_8c_500k/W_false_sharing/tao_trace \
    --limit 60000 --dt 10000 30000 100000 --tpc 333 --w-instr 160
"""
import argparse
import glob
import json
import os
import re
from collections import defaultdict

import numpy as np

CORE_RE = re.compile(r"(?:cores|switch)(\d+)\.core")


def load_core(trace_dir, core, limit):
    """读单核 records+labels 前 limit 行，按 (thread,micro_seq) 合并。

    返回按程序序排好的 list[ (micro_seq, commit_tick, cacheline_paddr,
                              is_load, is_store) ]。
    """
    recp = (glob.glob(os.path.join(
        trace_dir, f"*cores{core}.core*.records.micro.jsonl"))
        or glob.glob(os.path.join(
            trace_dir, f"*switch{core}.core*.records.micro.jsonl")))
    labp = (glob.glob(os.path.join(
        trace_dir, f"*cores{core}.core*.labels.micro.jsonl"))
        or glob.glob(os.path.join(
            trace_dir, f"*switch{core}.core*.labels.micro.jsonl")))
    if not recp or not labp:
        return []
    labs = {}
    with open(labp[0]) as f:
        for i, ln in enumerate(f):
            if i >= limit:
                break
            r = json.loads(ln)
            labs[(r["thread_id"], r["micro_seq"])] = r["commit_tick"]
    out = []
    with open(recp[0]) as f:
        for i, ln in enumerate(f):
            if i >= limit:
                break
            r = json.loads(ln)
            ct = labs.get((r["thread_id"], r["micro_seq"]))
            if ct is None or ct <= 0:
                continue
            out.append((r["micro_seq"], ct, r.get("cacheline_paddr", -1),
                        r.get("is_load", 0), r.get("is_store", 0)))
    out.sort(key=lambda x: x[0])  # 程序序
    return out


def contention_count(core_lines_by_window):
    """给定 {core: set(被写的 cacheline)} 与 {core: set(被访问)}，
    统计跨核争用：某核写的 line 被另一核在同窗访问（读或写）。
    返回该窗口的争用 line 计数。
    """
    write_sets = {}
    access_sets = {}
    for c, (acc, wr) in core_lines_by_window.items():
        write_sets[c] = wr
        access_sets[c] = acc
    cont_lines = set()
    cores = list(core_lines_by_window.keys())
    for ci in cores:
        for cj in cores:
            if ci == cj:
                continue
            # ci 写的 line 被 cj 访问 -> 争用
            cont_lines |= (write_sets[ci] & access_sets[cj])
    return len(cont_lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--limit", type=int, default=60000)
    ap.add_argument("--dt", type=float, nargs="+",
                    default=[10000, 30000, 100000], help="候选 ΔT(tick)")
    ap.add_argument("--tpc", type=int, default=333, help="tick_per_cycle")
    ap.add_argument("--w-instr", type=int, default=160,
                    help="指令切窗的 W（对照）")
    args = ap.parse_args()

    cores = []
    for p in glob.glob(os.path.join(args.trace, "*.records.micro.jsonl")):
        m = CORE_RE.search(p)
        if m:
            cores.append(int(m.group(1)))
    cores = sorted(set(cores))
    print(f"[load] cores={cores} limit={args.limit}/core ...")

    data = {}
    for c in cores:
        data[c] = load_core(args.trace, c, args.limit)
        print(f"  core{c}: {len(data[c])} µops, "
              f"tick range [{data[c][0][1]}, {data[c][-1][1]}]")

    # 全局时间范围
    t_lo = min(d[0][1] for d in data.values())
    t_hi = max(d[-1][1] for d in data.values())
    print(f"[global] tick range [{t_lo}, {t_hi}] "
          f"span={t_hi-t_lo} tick = {(t_hi-t_lo)/args.tpc:.0f} cycle\n")

    # ---------- 验证1: ΔT 标定 ----------
    print("=" * 70)
    print("验证1  ΔT 标定（每核每窗指令数分布）")
    print("=" * 70)
    for dt in args.dt:
        n_win = int((t_hi - t_lo) / dt) + 1
        # 每核每窗指令数
        counts = []  # 所有 (核,窗) 的指令数
        per_win_total = defaultdict(int)  # 窗 -> 8核总指令数
        for c in cores:
            for (_, ct, *_rest) in data[c]:
                wi = int((ct - t_lo) / dt)
                per_win_total[(c, wi)] += 1
        for (c, wi), n in per_win_total.items():
            counts.append(n)
        counts = np.array(counts)
        win_tot = defaultdict(int)
        for (c, wi), n in per_win_total.items():
            win_tot[wi] += n
        tot_arr = np.array(list(win_tot.values()))
        print(f"\nΔT={dt:.0f}tick ({dt/args.tpc:.0f}cyc)  n_win≈{n_win}")
        print(f"  每核每窗指令数: med={np.median(counts):.0f} "
              f"p90={np.percentile(counts,90):.0f} "
              f"max={counts.max():.0f}")
        print(f"  每窗8核合计指令: med={np.median(tot_arr):.0f} "
              f"p90={np.percentile(tot_arr,90):.0f} "
              f"max={tot_arr.max():.0f}  (≈token预算需求)")

    # ---------- 验证2: 跨核争用对的物理时间差分布 ----------
    print("\n" + "=" * 70)
    print("验证2  跨核争用对的物理时间差分布")
    print("（争用对 = 核i写line L @ tick_w，核j(≠i)访问同line @ tick_a）")
    print("（Δtick = |tick_w - tick_a|，回答：争用双方在物理时间上有多近）")
    print("=" * 70)

    # 收集每条 line 的事件: line -> list[(tick, core, is_store)]
    line_events = defaultdict(list)
    for c in cores:
        for (_, ct, cl, isld, isst) in data[c]:
            if cl < 0:
                continue
            line_events[cl].append((ct, c, isst))

    # 对每条被 >=2 核访问的 line，找跨核 (store, access) 对的最近 Δtick
    deltas = []
    n_contended_lines = 0
    for cl, evs in line_events.items():
        cset = set(e[1] for e in evs)
        if len(cset) < 2:
            continue
        stores = [(t, c) for (t, c, s) in evs if s]
        if not stores:
            continue
        n_contended_lines += 1
        evs_sorted = sorted(evs, key=lambda x: x[0])
        # 对每个 store，找其他核最近的一次访问
        for (tw, cw) in stores:
            best = None
            for (ta, ca, _s) in evs_sorted:
                if ca == cw:
                    continue
                d = abs(ta - tw)
                if best is None or d < best:
                    best = d
            if best is not None:
                deltas.append(best)

    deltas = np.array(deltas) if deltas else np.array([0])
    print(f"\n  被跨核共享且有写的 line 数 = {n_contended_lines}")
    print(f"  跨核争用对数 = {len(deltas)}")
    print(f"  Δtick 分布: med={np.median(deltas):.0f} "
          f"p25={np.percentile(deltas,25):.0f} "
          f"p75={np.percentile(deltas,75):.0f} "
          f"p90={np.percentile(deltas,90):.0f}")
    print(f"  Δcycle(÷{args.tpc}): med={np.median(deltas)/args.tpc:.1f} "
          f"p90={np.percentile(deltas,90)/args.tpc:.1f}")
    print("\n  对每个候选 ΔT，争用对落在「同一时间窗内」(Δtick<ΔT) 的比例：")
    for dt in args.dt:
        frac = float((deltas < dt).mean())
        print(f"    ΔT={dt:.0f}tick ({dt/args.tpc:.0f}cyc): "
              f"{frac*100:.1f}% 的争用对可被同窗捕获")
    print("\n  判读：比例高 -> 时间切窗能把争用双方放进同窗，信号对齐，方案1有效；")
    print("       比例低 -> 争用跨越多窗，需更大 ΔT 或别的机制。")


if __name__ == "__main__":
    main()
