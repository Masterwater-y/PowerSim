"""A-tier offline diagnostic: cpi_macro vs cpi_uop window-level distribution.

输入：windows.jsonl（默认 data/windows_v7_cpi_uop_mc32_tq32k/windows.jsonl）
对每个 (workload, core, window) 算：
  cpi_uop   = label[c][cpi_uop_idx]
  uops      = uops_per_core[c]
  macros    = instr_retired[c]
  cycles    = cpi_uop * uops
  cpi_macro = cycles / macros
  upm       = uops / macros

按 workload 汇总，对比：
  1) 离散统计：mean / std / p50 / p90 / p99 / max
  2) "若模型只能预测 mean" 的相对误差分布：用 dt_pred = mean·N 推时间误差
     dt_label = cycles
     rel_err_macro_i = |mean(cpi_macro)*M_i - cycles_i| / cycles_i
     rel_err_uop_i   = |mean(cpi_uop)*U_i   - cycles_i| / cycles_i
  3) outlier 窗：upm > 4 的窗占比 + 它们的 cpi_macro/cpi_uop 对比

输出：stdout 表 + JSON。
"""
from __future__ import annotations
import argparse
import json
import math
import os
import statistics as stats
from collections import defaultdict


def percentile(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    return xs[f] * (c - k) + xs[c] * (k - f)


def summarize(xs):
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "mean": sum(xs) / len(xs),
        "std": stats.pstdev(xs) if len(xs) > 1 else 0.0,
        "p50": percentile(xs, 0.50),
        "p90": percentile(xs, 0.90),
        "p99": percentile(xs, 0.99),
        "max": max(xs),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", default="/data00/yinhaolang/LLMSim/data/windows_v7_cpi_uop_mc32_tq32k/windows.jsonl")
    ap.add_argument("--out-json", default="/data00/yinhaolang/LLMSim/out/diag_cpi_uop_vs_macro.json")
    ap.add_argument("--upm-outlier", type=float, default=4.0)
    args = ap.parse_args()

    # 按 workload 收集 per-core 样本
    per_wl_cpi_macro = defaultdict(list)
    per_wl_cpi_uop = defaultdict(list)
    per_wl_upm = defaultdict(list)
    per_wl_cycles = defaultdict(list)
    per_wl_macros = defaultdict(list)
    per_wl_uops = defaultdict(list)
    per_wl_n_windows = defaultdict(int)

    with open(args.windows, "r") as fh:
        for line in fh:
            if not line.strip():
                continue
            o = json.loads(line)
            wl = o["workload"]
            per_wl_n_windows[wl] += 1
            label_keys = o["label_keys"][0]
            if "cpi_uop" not in label_keys:
                raise SystemExit(
                    f"[err] {args.windows} 缺 cpi_uop key（label_keys={label_keys}）；"
                    f"该脚本只支持 v7+ 数据集"
                )
            cpi_uop_idx = label_keys.index("cpi_uop")
            uops_per_core = o.get("uops_per_core") or o.get("core_split")
            instr_retired = o["instr_retired"]
            label = o["label"]
            for c in range(o["n_core"]):
                m = float(instr_retired[c])
                u = float(uops_per_core[c])
                if m <= 0 or u <= 0:
                    continue
                cpi_u = float(label[c][cpi_uop_idx])
                cyc = cpi_u * u
                cpi_m = cyc / m
                per_wl_cpi_macro[wl].append(cpi_m)
                per_wl_cpi_uop[wl].append(cpi_u)
                per_wl_upm[wl].append(u / m)
                per_wl_cycles[wl].append(cyc)
                per_wl_macros[wl].append(m)
                per_wl_uops[wl].append(u)

    report = {}
    workloads = sorted(per_wl_cpi_macro.keys())
    for wl in workloads:
        cpi_m = per_wl_cpi_macro[wl]
        cpi_u = per_wl_cpi_uop[wl]
        upm = per_wl_upm[wl]
        cyc = per_wl_cycles[wl]
        macs = per_wl_macros[wl]
        uops = per_wl_uops[wl]
        n = len(cpi_m)
        if n == 0:
            continue

        s_macro = summarize(cpi_m)
        s_uop = summarize(cpi_u)
        s_upm = summarize(upm)

        # 假设模型只输出 mean ：dt 误差
        mu_macro = s_macro["mean"]
        mu_uop = s_uop["mean"]
        rel_macro = []
        rel_uop = []
        for i in range(n):
            if cyc[i] <= 0:
                continue
            rel_macro.append(abs(mu_macro * macs[i] - cyc[i]) / cyc[i])
            rel_uop.append(abs(mu_uop * uops[i] - cyc[i]) / cyc[i])
        s_re_macro = summarize(rel_macro)
        s_re_uop = summarize(rel_uop)

        # cv = std/mean
        cv_macro = s_macro["std"] / s_macro["mean"] if s_macro["mean"] > 0 else float("nan")
        cv_uop = s_uop["std"] / s_uop["mean"] if s_uop["mean"] > 0 else float("nan")

        # outlier 窗：upm > threshold
        out_mask = [(upm[i] > args.upm_outlier) for i in range(n)]
        n_out = sum(out_mask)
        out_share = n_out / n
        out_cpi_macro = [cpi_m[i] for i in range(n) if out_mask[i]]
        out_cpi_uop = [cpi_u[i] for i in range(n) if out_mask[i]]
        out_upm = [upm[i] for i in range(n) if out_mask[i]]

        report[wl] = {
            "n_windows": per_wl_n_windows[wl],
            "n_cores": n,
            "cpi_macro": s_macro,
            "cpi_uop": s_uop,
            "cv_macro": cv_macro,
            "cv_uop": cv_uop,
            "cv_ratio_macro_over_uop": cv_macro / cv_uop if cv_uop > 0 else float("nan"),
            "upm": s_upm,
            "rel_err_macro_meanonly": s_re_macro,
            "rel_err_uop_meanonly": s_re_uop,
            "outlier": {
                "upm_threshold": args.upm_outlier,
                "n": n_out,
                "share": out_share,
                "cpi_macro": summarize(out_cpi_macro),
                "cpi_uop": summarize(out_cpi_uop),
                "upm": summarize(out_upm),
            },
        }

    os.makedirs(os.path.dirname(args.out_json), exist_ok=True)
    with open(args.out_json, "w") as fh:
        json.dump(report, fh, indent=2)

    # ===== pretty stdout =====
    print("\n========= CPI_macro vs CPI_uop (per-window, per-core) =========\n")
    hdr = (
        f"{'workload':28s}  {'n':>5s}  "
        f"{'cv_M':>6s}  {'cv_U':>6s}  {'M/U':>5s}  "
        f"{'p99_M':>8s}  {'p99_U':>7s}  "
        f"{'reMU%':>6s}  {'reUU%':>6s}  "
        f"{'oShr%':>6s}  {'oP99_M':>8s}  {'oP99_U':>7s}"
    )
    print(hdr)
    print("-" * len(hdr))
    for wl in workloads:
        r = report[wl]
        cv_M = r["cv_macro"]
        cv_U = r["cv_uop"]
        ratio = r["cv_ratio_macro_over_uop"]
        p99_M = r["cpi_macro"]["p99"]
        p99_U = r["cpi_uop"]["p99"]
        re_M = r["rel_err_macro_meanonly"].get("mean", float("nan"))
        re_U = r["rel_err_uop_meanonly"].get("mean", float("nan"))
        oshr = r["outlier"]["share"]
        op99_M = r["outlier"]["cpi_macro"].get("p99", float("nan")) if r["outlier"]["n"] > 0 else float("nan")
        op99_U = r["outlier"]["cpi_uop"].get("p99", float("nan")) if r["outlier"]["n"] > 0 else float("nan")
        print(
            f"{wl[:28]:28s}  {r['n_cores']:5d}  "
            f"{cv_M:6.2f}  {cv_U:6.2f}  {ratio:5.2f}  "
            f"{p99_M:8.2f}  {p99_U:7.2f}  "
            f"{re_M*100:6.1f}  {re_U*100:6.1f}  "
            f"{oshr*100:6.2f}  {op99_M:8.2f}  {op99_U:7.2f}"
        )
    print()
    print("说明：")
    print("  cv_M / cv_U   = std/mean，越小越稳。M=CPI_macro, U=CPI_uop。")
    print("  M/U           = cv_macro / cv_uop，>1 说明 macro 比 uop 抖。")
    print("  p99_M/p99_U   = CPI 的 99 分位，体现尾部 outlier。")
    print("  reMU% / reUU% = 假设模型只能输出 mean 时的 dt 相对误差均值。")
    print("                  越小越好。reMU > reUU 说明 uop 单位时间推进会更准。")
    print(f"  oShr%         = upm > {args.upm_outlier} 的"
          f" outlier 窗口占比（rep stosq 类微码 macro）。")
    print("  oP99_M/oP99_U = outlier 窗里 CPI 的 p99。oP99_M 通常会爆炸。")
    print()
    print(f"[saved] {args.out_json}")


if __name__ == "__main__":
    main()
