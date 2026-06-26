#!/usr/bin/env python3
"""v6.1 内部分布诊断：

1) 14 个负载的 self-NN 距离（在 v6.1 全集 z-score 空间里）
   - 每个负载内部 leave-one-out NN（同 workload）距离 → 内聚度
   - 每个负载跨 workload NN（不同 workload）距离 → 临近哪个 train 负载
2) 三个新加负载（ads_ctr / feed_ranking / interest_graph_recall）
   在 v6 train 分布（11 个负载）下的 OOD 分维度分解：
   - z-score 各维 |delta| 排序，定位"OOD 最严重的特征维度"
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
    FEATURE_SCALAR_KEYS,
    extract_sample_vec,
    load_windows,
    percentile,
)


def feature_names() -> list[str]:
    rd = [f"rd_{i}" for i in range(9)]
    st = [f"st_{i}" for i in range(10)]
    return FEATURE_SCALAR_KEYS + rd + st


def per_workload_nn(tr_Z: np.ndarray, tr_wl: list[str], BATCH: int = 512):
    n = tr_Z.shape[0]
    wl_arr = np.asarray(tr_wl)
    tr_norm = (tr_Z * tr_Z).sum(axis=1)
    same_d = np.empty(n, dtype=np.float32)
    cross_d = np.empty(n, dtype=np.float32)
    cross_wl = ["" for _ in range(n)]
    for i in range(0, n, BATCH):
        chunk = tr_Z[i:i + BATCH]
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + tr_norm[None, :] \
            - 2.0 * chunk @ tr_Z.T
        d2 = np.maximum(d2, 0.0)
        chunk_wl = wl_arr[i:i + BATCH]
        for j in range(chunk.shape[0]):
            d2[j, i + j] = np.inf
            same_mask = (wl_arr == chunk_wl[j])
            d2_same = np.where(same_mask, d2[j], np.inf)
            d2_cross = np.where(same_mask, np.inf, d2[j])
            same_d[i + j] = np.sqrt(d2_same.min())
            mind = d2_cross.min()
            cross_d[i + j] = np.sqrt(mind) if np.isfinite(mind) else np.inf
            if np.isfinite(mind):
                cross_wl[i + j] = wl_arr[d2_cross.argmin()]
    return same_d, cross_d, cross_wl


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v6-train", required=True,
                    help="windows_v6_tq32k/windows.jsonl (11 负载)")
    ap.add_argument("--v6-holdout", required=True,
                    help="windows_v6_holdout_tq32k/windows.jsonl (3 负载)")
    ap.add_argument("--v61", required=True,
                    help="windows_v6.1_tq32k/windows.jsonl (14 负载)")
    ap.add_argument("--out", default="logs/v6.1_distribution_diag.json")
    args = ap.parse_args()

    print(f"[diag] load v6 train")
    v6_wl, v6_X = load_windows(Path(args.v6_train))
    print(f"[diag] load v6 holdout")
    ho_wl, ho_X = load_windows(Path(args.v6_holdout))
    print(f"[diag] load v6.1 all")
    v61_wl, v61_X = load_windows(Path(args.v61))
    print(f"v6 train n={v6_X.shape[0]} | v6 holdout n={ho_X.shape[0]} | "
          f"v6.1 n={v61_X.shape[0]}")

    feat = feature_names()
    assert len(feat) == v6_X.shape[1] == v61_X.shape[1]

    # ---- A) v6 train vs holdout: per-feature mean shift (z-scored on v6 train) ----
    mu = v6_X.mean(axis=0)
    sd = v6_X.std(axis=0)
    sd_safe = np.where(sd < 1e-6, 1.0, sd)
    print()
    print("[A] v6 train -> 3 holdout: per-feature mean shift (z-scored on train)")
    out_A = {}
    ho_wl_arr = np.asarray(ho_wl)
    for wl in sorted(set(ho_wl)):
        ho_sub = ho_X[ho_wl_arr == wl]
        delta_mean = (ho_sub.mean(axis=0) - mu) / sd_safe
        delta_p95 = (np.percentile(ho_sub, 95, axis=0) - mu) / sd_safe
        rank = np.argsort(-np.abs(delta_mean))
        print(f"  ---- {wl} (n={ho_sub.shape[0]}) "
              f"top |z-shift| dims: ----")
        per_wl_feats = []
        for r in rank[:10]:
            name = feat[r]
            row = {
                "feature": name,
                "z_shift_mean": float(delta_mean[r]),
                "z_shift_p95": float(delta_p95[r]),
                "train_mean": float(mu[r]),
                "train_std": float(sd[r]),
                "holdout_mean": float(ho_sub.mean(axis=0)[r]),
            }
            per_wl_feats.append(row)
            print(f"    {name:<28} z_mean={row['z_shift_mean']:+8.2f}  "
                  f"z_p95={row['z_shift_p95']:+8.2f}  "
                  f"train_mean={row['train_mean']:8.4f}  "
                  f"ho_mean={row['holdout_mean']:8.4f}")
        out_A[wl] = per_wl_feats

    # ---- B) v6 holdout -> nearest train workload ----
    print()
    print("[B] v6 holdout each sample -> nearest train workload")
    tr_Z = (v6_X - mu) / sd_safe
    ho_Z = (ho_X - mu) / sd_safe
    tr_norm = (tr_Z * tr_Z).sum(axis=1)
    nearest_wl = ["" for _ in range(ho_Z.shape[0])]
    nearest_d = np.empty(ho_Z.shape[0], dtype=np.float32)
    BATCH = 512
    v6_wl_arr = np.asarray(v6_wl)
    for i in range(0, ho_Z.shape[0], BATCH):
        chunk = ho_Z[i:i + BATCH]
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + tr_norm[None, :] \
            - 2.0 * chunk @ tr_Z.T
        d2 = np.maximum(d2, 0.0)
        idx_min = d2.argmin(axis=1)
        for j in range(chunk.shape[0]):
            nearest_wl[i + j] = v6_wl_arr[idx_min[j]]
            nearest_d[i + j] = float(np.sqrt(d2[j, idx_min[j]]))
    out_B = {}
    for wl in sorted(set(ho_wl)):
        mask = ho_wl_arr == wl
        votes = defaultdict(int)
        for nw in np.asarray(nearest_wl)[mask]:
            votes[nw] += 1
        votes_sorted = sorted(votes.items(), key=lambda x: -x[1])
        d_sub = nearest_d[mask]
        print(f"  {wl} (n={mask.sum()}): mean NN dist {d_sub.mean():.3f}")
        for nw, cnt in votes_sorted:
            print(f"    -> {nw:<28} {cnt:>5} ({cnt/mask.sum()*100:>5.1f}%)")
        out_B[wl] = {
            "mean_nn_dist": float(d_sub.mean()),
            "p95_nn_dist": percentile(d_sub, 95),
            "vote_train_workload": dict(votes_sorted),
        }

    # ---- C) v6.1 self-NN: same-workload tight vs cross-workload reach ----
    print()
    print("[C] v6.1 self-NN per-workload (same vs cross workload)")
    mu61 = v61_X.mean(axis=0)
    sd61 = v61_X.std(axis=0)
    sd61_safe = np.where(sd61 < 1e-6, 1.0, sd61)
    v61_Z = (v61_X - mu61) / sd61_safe
    same_d, cross_d, cross_wl = per_workload_nn(v61_Z, v61_wl)
    out_C = {}
    v61_wl_arr = np.asarray(v61_wl)
    print(f"  {'workload':<28} {'n':>5} {'same_p50':>9} {'same_p95':>9} "
          f"{'cross_p50':>10} {'cross_p95':>10} {'top_neighbor':<20}")
    for wl in sorted(set(v61_wl)):
        mask = v61_wl_arr == wl
        sd_sub = same_d[mask]
        cd_sub = cross_d[mask]
        cw_arr = np.asarray(cross_wl)[mask]
        votes = defaultdict(int)
        for cw in cw_arr:
            if cw:
                votes[cw] += 1
        top_nb = max(votes.items(), key=lambda x: x[1]) if votes else ("", 0)
        same_p50 = percentile(sd_sub, 50)
        same_p95 = percentile(sd_sub, 95)
        cross_p50 = percentile(cd_sub, 50)
        cross_p95 = percentile(cd_sub, 95)
        print(f"  {wl:<28} {mask.sum():>5} "
              f"{same_p50:>9.3f} {same_p95:>9.3f} "
              f"{cross_p50:>10.3f} {cross_p95:>10.3f} "
              f"{top_nb[0]:<20}({top_nb[1]})")
        out_C[wl] = {
            "n": int(mask.sum()),
            "same_workload_nn_p50": same_p50,
            "same_workload_nn_p95": same_p95,
            "cross_workload_nn_p50": cross_p50,
            "cross_workload_nn_p95": cross_p95,
            "top_cross_neighbor": top_nb[0],
            "top_cross_neighbor_votes": top_nb[1],
        }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "feature_shift_v6_holdout": out_A,
        "nearest_train_workload": out_B,
        "v6.1_self_nn": out_C,
    }, indent=2))
    print(f"\n[diag] report -> {out_path}")


if __name__ == "__main__":
    main()
