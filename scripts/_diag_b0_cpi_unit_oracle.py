"""B0: oracle 上限对比 — 用 CPI_macro vs CPI_uop 单位推进时间，
谁的累计 drift 更小。

模型实际预测有残差，所以仅用 oracle 完美预测对比是过于乐观的。
B0 同时跑两类预测器，每类都用 macro / uop 两种单位：

  P1 = perfect ：pred_CPI = label_CPI（窗口内真值）
         => Δt_pred = Δt_label，drift = 0（无信息）。只看分布属性。
  P2 = rolling_mean(K=20) ：用最近 K 窗的 mean(CPI) 预测下一窗
         => 与"模型只记住分布均值"等价；这是真实模型常态的 trivial baseline。

把每个 workload 的所有窗口（按 workload 内的"程序序"近似 = 出现顺序）跑两遍：
  - 单位 macro ：dt_pred = pred_cpi_macro * macros_win
  - 单位 uop   ：dt_pred = pred_cpi_uop   * uops_win
计算每窗的 Δt 相对误差 |pred - label|/label，以及把它们顺序累加得到 cumulative drift。

输出每 workload 三个指标：
  meanWAPE_macro / meanWAPE_uop  (per-window 相对误差均值)
  finalDrift_macro / finalDrift_uop (累计 dt_pred / 累计 dt_label - 1)
  p99WAPE_macro / p99WAPE_uop (尾部相对误差)
"""
from __future__ import annotations
import argparse
import json
import math
import os
import statistics as stats
from collections import defaultdict, deque


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


def rolling_pred(seq_actual, K):
    """For each i, pred[i] = mean(actual[max(0,i-K):i]); pred[0]=actual[0]."""
    out = []
    buf = deque(maxlen=K)
    for i, v in enumerate(seq_actual):
        if not buf:
            out.append(v)
        else:
            out.append(sum(buf) / len(buf))
        buf.append(v)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows",
                    default="/data00/yinhaolang/LLMSim/data/windows_v7_cpi_uop_mc32_tq32k/windows.jsonl")
    ap.add_argument("--out-json",
                    default="/data00/yinhaolang/LLMSim/out/b0_oracle_cpi_unit_compare.json")
    ap.add_argument("--K", type=int, default=20,
                    help="rolling-mean predictor window")
    args = ap.parse_args()

    # 收集 per-(workload, core) 序列：按出现顺序
    seqs_macro = defaultdict(list)  # (wl, c) -> [cpi_macro]
    seqs_uop = defaultdict(list)    # (wl, c) -> [cpi_uop]
    seqs_M = defaultdict(list)      # (wl, c) -> [macros_count]
    seqs_U = defaultdict(list)      # (wl, c) -> [uops_count]
    seqs_cyc = defaultdict(list)    # (wl, c) -> [cycles_label]

    with open(args.windows, "r") as fh:
        for line in fh:
            if not line.strip():
                continue
            o = json.loads(line)
            wl = o["workload"]
            label_keys = o["label_keys"][0]
            if "cpi_uop" not in label_keys:
                raise SystemExit(
                    f"[err] {args.windows} 缺 cpi_uop key（label_keys={label_keys}）；"
                    f"该脚本只支持 v7+ 数据集"
                )
            cpi_uop_idx = label_keys.index("cpi_uop")
            uops_per_core = o.get("uops_per_core") or o.get("core_split")
            for c in range(o["n_core"]):
                m = float(o["instr_retired"][c])
                u = float(uops_per_core[c])
                if m <= 0 or u <= 0:
                    continue
                cpi_u = float(o["label"][c][cpi_uop_idx])
                cyc = cpi_u * u
                cpi_m = cyc / m
                key = (wl, c)
                seqs_macro[key].append(cpi_m)
                seqs_uop[key].append(cpi_u)
                seqs_M[key].append(m)
                seqs_U[key].append(u)
                seqs_cyc[key].append(cyc)

    # 按 workload 聚合
    per_wl = defaultdict(lambda: {
        "wape_macro": [], "wape_uop": [],
        "drift_macro_num": 0.0, "drift_macro_den": 0.0,
        "drift_uop_num": 0.0, "drift_uop_den": 0.0,
        "n_cores": set(), "n_windows_total": 0,
    })

    for (wl, c), cpi_m_seq in seqs_macro.items():
        cpi_u_seq = seqs_uop[(wl, c)]
        M = seqs_M[(wl, c)]
        U = seqs_U[(wl, c)]
        CYC = seqs_cyc[(wl, c)]

        pred_cpi_m = rolling_pred(cpi_m_seq, args.K)
        pred_cpi_u = rolling_pred(cpi_u_seq, args.K)

        rec = per_wl[wl]
        rec["n_cores"].add(c)
        rec["n_windows_total"] += len(cpi_m_seq)
        for i in range(len(cpi_m_seq)):
            cyc_label = CYC[i]
            if cyc_label <= 0:
                continue
            dt_pred_M = pred_cpi_m[i] * M[i]
            dt_pred_U = pred_cpi_u[i] * U[i]
            rec["wape_macro"].append(abs(dt_pred_M - cyc_label) / cyc_label)
            rec["wape_uop"].append(abs(dt_pred_U - cyc_label) / cyc_label)
            rec["drift_macro_num"] += dt_pred_M
            rec["drift_uop_num"] += dt_pred_U
            rec["drift_macro_den"] += cyc_label
            rec["drift_uop_den"] += cyc_label

    report = {}
    workloads = sorted(per_wl.keys())
    for wl in workloads:
        r = per_wl[wl]
        wm = r["wape_macro"]; wu = r["wape_uop"]
        n = len(wm)
        if n == 0:
            continue
        mean_wm = sum(wm) / n
        mean_wu = sum(wu) / n
        p99_wm = percentile(wm, 0.99)
        p99_wu = percentile(wu, 0.99)
        drift_m = r["drift_macro_num"] / max(r["drift_macro_den"], 1e-9) - 1.0
        drift_u = r["drift_uop_num"] / max(r["drift_uop_den"], 1e-9) - 1.0
        report[wl] = {
            "n_cores": len(r["n_cores"]),
            "n_windows_total": r["n_windows_total"],
            "n_samples": n,
            "mean_wape_macro": mean_wm,
            "mean_wape_uop": mean_wu,
            "p99_wape_macro": p99_wm,
            "p99_wape_uop": p99_wu,
            "final_drift_macro": drift_m,
            "final_drift_uop": drift_u,
            "wape_macro_over_uop": mean_wm / max(mean_wu, 1e-12),
        }

    os.makedirs(os.path.dirname(args.out_json), exist_ok=True)
    with open(args.out_json, "w") as fh:
        json.dump(report, fh, indent=2)

    print(f"\n========= B0: rolling-mean(K={args.K}) predictor — "
          f"CPI_macro vs CPI_uop unit =========\n")
    hdr = (
        f"{'workload':28s}  "
        f"{'meanWAPE_M%':>11s}  {'meanWAPE_U%':>11s}  {'M/U':>5s}  "
        f"{'p99WAPE_M%':>10s}  {'p99WAPE_U%':>10s}  "
        f"{'drift_M%':>9s}  {'drift_U%':>9s}"
    )
    print(hdr)
    print("-" * len(hdr))
    winners_m = 0; winners_u = 0
    for wl in workloads:
        r = report[wl]
        ratio = r["wape_macro_over_uop"]
        if ratio > 1.0:
            winners_u += 1
        else:
            winners_m += 1
        print(
            f"{wl[:28]:28s}  "
            f"{r['mean_wape_macro']*100:11.2f}  {r['mean_wape_uop']*100:11.2f}  {ratio:5.2f}  "
            f"{r['p99_wape_macro']*100:10.2f}  {r['p99_wape_uop']*100:10.2f}  "
            f"{r['final_drift_macro']*100:9.3f}  {r['final_drift_uop']*100:9.3f}"
        )
    print()
    print("说明：")
    print("  meanWAPE_M / meanWAPE_U = 每窗 |pred-label|/label 的均值，越小越好。")
    print("  M/U                     = meanWAPE_macro / meanWAPE_uop，>1 → uop 更稳。")
    print("  p99WAPE                 = 尾部窗口的预测误差，体现 outlier 鲁棒性。")
    print("  drift                   = 累计 dt_pred / 累计 dt_label - 1，整体偏移。")
    print(f"\nverdict: uop wins on {winners_u}/{len(workloads)} workloads, "
          f"macro wins on {winners_m}/{len(workloads)}.")
    print(f"[saved] {args.out_json}")


if __name__ == "__main__":
    main()
