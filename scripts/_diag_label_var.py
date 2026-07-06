"""_diag_label_var.py — 诊断固定指令切窗下 label cpi_uop 的可学习性。

对比 false_sharing（跨核敏感）vs compute_int（不敏感）：
  1. per-core 相邻窗口 cpi_uop 序列的波动（变异系数 CV）
  2. 相邻窗口 cpi_uop 的 lag-1 自相关（是否平滑/有结构 vs 白噪声）
  3. per-core-window cpi_uop 相对全核中位数的离散度
  4. 误差符号（系统性偏置 vs 随机）—— 需要 baseline 预测，这里只看 label 结构

判读：
  CV 大 + 自相关 ≈ 0  -> 白噪声，标签本身在固定指令切窗下不可学
  CV 中 + 自相关高     -> 有结构，模型应能学，当前没学好
"""
import json
import sys
from collections import defaultdict

import numpy as np

PATH = sys.argv[1] if len(sys.argv) > 1 else \
    "data/windows_v7_cpi_uop_mc32_tq32k/windows.jsonl"

CPI_KEY = "cpi_uop"


def lag1_autocorr(x):
    x = np.asarray(x, dtype=float)
    if len(x) < 3:
        return float("nan")
    x = x - x.mean()
    denom = (x * x).sum()
    if denom == 0:
        return float("nan")
    return float((x[:-1] * x[1:]).sum() / denom)


def main():
    # 按 workload -> core -> 有序 CPI 序列
    rows = [json.loads(l) for l in open(PATH)]
    if not rows:
        print("empty"); return
    cpi_idx = rows[0]["label_keys"].index(CPI_KEY)

    series = defaultdict(lambda: defaultdict(list))  # wl -> core -> [cpi...]
    for r in rows:
        wl = r["workload"]
        nc = r["n_core"]
        for c in range(nc):
            series[wl][c].append(r["label"][c][cpi_idx])

    for wl in sorted(series):
        cores = series[wl]
        cvs, acs, n_win = [], [], None
        all_cpi = []
        for c in sorted(cores):
            seq = np.asarray(cores[c], dtype=float)
            n_win = len(seq)
            all_cpi.append(seq)
            mu = seq.mean()
            sd = seq.std()
            cvs.append(sd / mu if mu != 0 else float("nan"))
            acs.append(lag1_autocorr(seq))
        all_cpi = np.stack(all_cpi)  # [n_core, n_win]
        # 跨核离散：每个窗口内 8 核 CPI 相对中位数的 MAD
        med = np.median(all_cpi, axis=0)  # [n_win]
        cross_core_disp = np.mean(
            np.abs(all_cpi - med[None, :]) / np.maximum(med[None, :], 1e-9))

        print(f"\n=== {wl}  (n_win/core={n_win}, n_core={len(cores)}) ===")
        print(f"  per-core CPI 变异系数 CV   : "
              f"mean={np.nanmean(cvs):.3f}  "
              f"(越大=相邻窗口波动越剧烈)")
        print(f"  per-core lag-1 自相关       : "
              f"mean={np.nanmean(acs):.3f}  "
              f"(接近0=白噪声不可学, 接近1=平滑有结构)")
        print(f"  跨核 CPI 相对中位数离散     : "
              f"{cross_core_disp:.3f}  "
              f"(越大=同窗各核越不齐)")
        print(f"  CPI 范围: min={all_cpi.min():.3f} "
              f"med={np.median(all_cpi):.3f} max={all_cpi.max():.3f}")


if __name__ == "__main__":
    main()
