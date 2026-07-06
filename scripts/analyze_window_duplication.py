#!/usr/bin/env python3
"""Window 重复率分析。

对每个 workload：
  - 同一窗口的 v8 36-d feature vector 用 z-score 归一化
  - 以 0.05 / 0.10 / 0.20 的 L2 距离阈值算"近邻簇"占比
  - 同时给出 per-workload feature std（标准差越小 → 越同质）
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from ood_holdout_scan import (
    extract_sample_vec,
    load_windows,
    percentile,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="logs/window_duplication.json")
    args = ap.parse_args()

    print(f"[dup] load {args.data}")
    wl, X = load_windows(Path(args.data))
    print(f"[dup] n={X.shape[0]} dim={X.shape[1]}")

    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd_safe = np.where(sd < 1e-6, 1.0, sd)
    Z = (X - mu) / sd_safe

    out: dict = {}
    wl_arr = np.asarray(wl)
    BATCH = 256
    THRESH = [0.05, 0.10, 0.20, 0.50]

    print(f"\n  {'workload':<28} {'n':>5} {'avg_intra_std':>12} "
          f"{'<0.05':>7} {'<0.10':>7} {'<0.20':>7} {'<0.50':>7} "
          f"{'effective_n':>12}")
    for w in sorted(set(wl)):
        mask = wl_arr == w
        sub = Z[mask]
        n = sub.shape[0]
        # average per-feature std within this workload (smaller = more
        # homogeneous, i.e. windows look the same)
        intra_std = float(sub.std(axis=0).mean())
        # pairwise distance: NN distance histogram
        sub_norm = (sub * sub).sum(axis=1)
        dup_counts = {t: 0 for t in THRESH}
        nn_dists = np.empty(n, dtype=np.float32)
        for i in range(0, n, BATCH):
            chunk = sub[i:i + BATCH]
            d2 = (chunk * chunk).sum(axis=1, keepdims=True) + sub_norm[None, :] \
                - 2.0 * chunk @ sub.T
            d2 = np.maximum(d2, 0.0)
            for j in range(chunk.shape[0]):
                d2[j, i + j] = np.inf
            d = np.sqrt(d2)
            nn = d.min(axis=1)
            nn_dists[i:i + chunk.shape[0]] = nn
            for t in THRESH:
                # fraction of windows whose NN is closer than t (i.e. near
                # duplicate of at least one other window in same workload)
                dup_counts[t] += int((nn <= t).sum())
        dup_frac = {f"frac_nn<{t:.2f}": dup_counts[t] / float(n)
                    for t in THRESH}
        # "effective n" = how many windows are >= 0.10 apart from all others
        unique_mask = nn_dists > 0.10
        eff_n = int(unique_mask.sum())
        nn_p50 = percentile(nn_dists, 50)
        nn_p95 = percentile(nn_dists, 95)
        out[w] = {
            "n": n,
            "avg_intra_feature_std": intra_std,
            "nn_p50": nn_p50,
            "nn_p95": nn_p95,
            "effective_n_gt_0.10": eff_n,
            **dup_frac,
        }
        print(f"  {w:<28} {n:>5} {intra_std:>12.4f} "
              f"{dup_frac['frac_nn<0.05']:>7.2%} "
              f"{dup_frac['frac_nn<0.10']:>7.2%} "
              f"{dup_frac['frac_nn<0.20']:>7.2%} "
              f"{dup_frac['frac_nn<0.50']:>7.2%} "
              f"{eff_n:>12}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n[dup] -> {out_path}")


if __name__ == "__main__":
    main()
