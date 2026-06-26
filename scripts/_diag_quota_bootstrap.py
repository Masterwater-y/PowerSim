"""_diag_quota_bootstrap.py — 验证 CPI 配额自举切窗的跨核对齐效果。

对比两种切窗在【跨核时间对齐】上的表现，全部用 gem5 真值 commit_tick：
  基线: 每核每窗固定 N 条指令（原始指令切窗）
  方案C: 窗0用种子 N0，之后 N_c(k+1) = ΔT_target / CPI_c(k)（用上一窗CPI配额）

度量每个窗口的跨核对齐度：
  各核窗口 k 覆盖物理时间区间 [start_c, end_c]
  - 重叠率 = |∩ 各核区间| / |∪ 各核区间|  (1=完美对齐, 0=完全错开)
  - start 漂移 = max(start_c) - min(start_c)  (cycle)
关键看：漂移随窗口序号 k 是【增长】(基线) 还是【有界】(方案C)。

用法:
  python scripts/_diag_quota_bootstrap.py \
    --trace data/raw_8w_8c_500k/W_false_sharing/tao_trace \
    --limit 200000 --dt-target 500 --seed-n 160 --tpc 333 \
    --nmin 8 --nmax 512
"""
import argparse
import glob
import json
import re

import numpy as np

CORE_RE = re.compile(r"(?:cores|switch)(\d+)\.core")


def load_core_ticks(trace_dir, core, limit):
    recp = (glob.glob(trace_dir + f"/*cores{core}.core*.records.micro.jsonl")
            or glob.glob(trace_dir + f"/*switch{core}.core*.records.micro.jsonl"))
    labp = (glob.glob(trace_dir + f"/*cores{core}.core*.labels.micro.jsonl")
            or glob.glob(trace_dir + f"/*switch{core}.core*.labels.micro.jsonl"))
    if not recp or not labp:
        return None
    labs = {}
    with open(labp[0]) as f:
        for i, ln in enumerate(f):
            if i >= limit:
                break
            r = json.loads(ln)
            labs[(r["thread_id"], r["micro_seq"])] = r["commit_tick"]
    seq = []
    with open(recp[0]) as f:
        for i, ln in enumerate(f):
            if i >= limit:
                break
            r = json.loads(ln)
            ct = labs.get((r["thread_id"], r["micro_seq"]))
            if ct and ct > 0:
                seq.append((r["micro_seq"], ct))
    seq.sort(key=lambda x: x[0])
    return np.array([t for _, t in seq], dtype=np.float64)


def interval_overlap_ratio(starts, ends):
    """8 核区间的交/并比。"""
    lo = max(starts)
    hi = min(ends)
    inter = max(0.0, hi - lo)
    union = max(ends) - min(starts)
    return inter / union if union > 0 else 0.0


def simulate(ticks, mode, tpc, dt_target, seed_n, nmin, nmax):
    """返回每窗 (overlap_ratio, start_drift_cycle)。

    mode:
      baseline : 每核固定 seed_n 条指令（无对齐）
      quota    : N_c = ΔT / 上一窗CPI（只控宽度，不控起点）
      align    : 终点对齐反馈——已知各核起始cycle与预期CPI，
                 设共同目标 T_end = max(start_c)+ΔT，
                 N_c = (T_end - start_c) / CPI_c，使各核预期结束cycle对齐。
    """
    cores = sorted(ticks.keys())
    cursor = {c: 0 for c in cores}
    N = {c: seed_n for c in cores}
    # 各核"预测累积起始 cycle"（部署时来自递推；这里也跟踪真值起点做对照）
    pred_start = {c: ticks[c][0] / tpc for c in cores}
    prev_cpi = {c: 1.0 for c in cores}
    out = []
    first = True
    while True:
        # 决定本窗各核指令数
        if mode == "align" and not first:
            T_end = max(pred_start.values()) + dt_target
            for c in cores:
                nc = int(round((T_end - pred_start[c]) / max(prev_cpi[c], 1e-6)))
                N[c] = max(nmin, min(nmax, nc))
        starts, ends = [], []
        ok = True
        win_info = {}
        for c in cores:
            i = cursor[c]
            n = N[c]
            if i + n + 1 > len(ticks[c]):
                ok = False
                break
            st = ticks[c][i]
            en = ticks[c][i + n - 1]
            starts.append(st)
            ends.append(en)
            cyc = (en - st) / tpc
            cpi = cyc / max(n, 1)
            win_info[c] = (i + n, cpi, en / tpc)
        if not ok:
            break
        ov = interval_overlap_ratio(starts, ends)
        drift = (max(starts) - min(starts)) / tpc
        total_instr = sum(N[c] for c in cores)
        out.append((ov, drift, total_instr))
        for c in cores:
            cursor[c], cpi, end_cyc = win_info[c]
            prev_cpi[c] = cpi
            pred_start[c] = end_cyc  # 下一窗起点 = 本窗真实结束cycle
            if mode == "baseline":
                N[c] = seed_n
            elif mode == "quota":
                nc = int(round(dt_target / max(cpi, 1e-6)))
                N[c] = max(nmin, min(nmax, nc))
        first = False
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--limit", type=int, default=200000)
    ap.add_argument("--dt-target", type=float, default=500.0,
                    help="方案C 目标时间宽度 (cycle)")
    ap.add_argument("--seed-n", type=int, default=160,
                    help="窗0种子指令数 / 基线固定指令数")
    ap.add_argument("--tpc", type=int, default=333)
    ap.add_argument("--nmin", type=int, default=8)
    ap.add_argument("--nmax", type=int, default=512)
    args = ap.parse_args()

    cores = []
    for p in glob.glob(args.trace + "/*.records.micro.jsonl"):
        m = CORE_RE.search(p)
        if m:
            cores.append(int(m.group(1)))
    cores = sorted(set(cores))
    print(f"[load] cores={cores} limit={args.limit} ...")
    ticks = {}
    for c in cores:
        ticks[c] = load_core_ticks(args.trace, c, args.limit)
        print(f"  core{c}: {len(ticks[c])} µops")

    for mode in ("align",):
        res = simulate(ticks, mode, args.tpc, args.dt_target,
                       args.seed_n, args.nmin, args.nmax)
        if not res:
            print(f"\n[{mode}] no windows"); continue
        ov = np.array([r[0] for r in res])
        dr = np.array([r[1] for r in res])
        ti = np.array([r[2] for r in res])
        nwin = len(res)
        q = max(1, nwin // 10)
        TOK_PER_UOP = 6.0  # 实测 token/µop
        STRUCT = 8 * 2 + 12  # 结构token近似
        med_instr = np.median(ti)
        med_tok = med_instr * TOK_PER_UOP + STRUCT
        p90_tok = np.percentile(ti, 90) * TOK_PER_UOP + STRUCT
        print(f"\n=== {mode}  ΔT={args.dt_target}cyc  (windows={nwin}) ===")
        print(f"  跨核重叠率: mean={ov.mean():.3f} "
              f"(前{q}->后{q}: {ov[:q].mean():.3f}->{ov[-q:].mean():.3f})")
        print(f"  start漂移(cyc): mean={dr.mean():.1f} max={dr.max():.1f} "
              f"({'有界' if dr[-q:].mean() < dr[:q].mean()*1.5 else '发散'})")
        print(f"  每窗8核合计指令: med={med_instr:.0f} "
              f"p90={np.percentile(ti,90):.0f} max={ti.max():.0f}")
        print(f"  估算token: med={med_tok:.0f} p90={p90_tok:.0f}  "
              f"(token/uop={TOK_PER_UOP})")
        for cap in (8192, 16384, 20480):
            print(f"    占 MAXLEN={cap}: med={med_tok/cap*100:.0f}% "
                  f"p90={p90_tok/cap*100:.0f}%")


if __name__ == "__main__":
    main()
