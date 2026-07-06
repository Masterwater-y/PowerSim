#!/usr/bin/env python3
"""训练数据集一次性体检：

A) 冗余检测：feature + label 联合 z-score 下 1-NN 距离 < (eps_feat, eps_lab) 视为冗余
B) 难样本（特征不足）：feature kNN 邻域内 label 标准差 / 全局 label 标准差
   ratio 越大 → 同一 feature 邻域映射到差异很大的 label → 特征不足
C) 分布覆盖：per-workload feature / label 范围、跨 workload NN、effective_n、
   PCA top-3 密度直方

用法：
  python scripts/dataset_audit.py --data data/windows_v6.2_tq32k/windows.jsonl \
     --out logs/dataset_audit_v6.2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from ood_holdout_scan import FEATURE_SCALAR_KEYS, extract_sample_vec

PMU_KEYS = [
    "cpi_uop", "branch_miss", "l1d_ld_miss",
    "l1d_st_miss", "l1i_miss", "llc_miss",
    "dtlb_miss", "mshr_avg",
]


def load_dataset(path: Path):
    workloads: list[str] = []
    feats: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    dim_feat = None
    with path.open() as f:
        for ln in f:
            if not ln.startswith("{"):
                continue
            r = json.loads(ln)
            v = extract_sample_vec(r)
            if v.size == 0:
                continue
            if dim_feat is None:
                dim_feat = v.size
            elif v.size != dim_feat:
                continue
            label_keys = r.get("label_keys") or PMU_KEYS
            try:
                L_mat = np.asarray(r["label"], dtype=np.float32)
            except Exception:
                continue
            if L_mat.ndim != 2 or L_mat.shape[1] != len(label_keys):
                continue
            # 多核 label 取 mean -> 每窗口 8 维 PMU 向量
            L = L_mat.mean(axis=0)
            # 按 label_keys 顺序重排成统一 PMU_KEYS 顺序
            idx_map = [label_keys.index(k) for k in PMU_KEYS if k in label_keys]
            if len(idx_map) != len(PMU_KEYS):
                continue
            L = L[idx_map]
            workloads.append(r["workload"])
            feats.append(v)
            labels.append(L)
    if not feats:
        raise SystemExit(f"empty dataset: {path}")
    return (
        np.asarray(workloads),
        np.stack(feats, axis=0).astype(np.float32),
        np.stack(labels, axis=0).astype(np.float32),
    )


def z_score(X: np.ndarray):
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd_safe = np.where(sd < 1e-6, 1.0, sd)
    return (X - mu) / sd_safe, mu, sd_safe


def pairwise_min_nn(Z: np.ndarray, batch: int = 512):
    """返回每个样本到其他所有样本的 1-NN index 和 L2 距离。"""
    n = Z.shape[0]
    znorm = (Z * Z).sum(axis=1)
    nn_idx = np.empty(n, dtype=np.int32)
    nn_d = np.empty(n, dtype=np.float32)
    for i in range(0, n, batch):
        chunk = Z[i:i + batch]
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + znorm[None, :] \
            - 2.0 * chunk @ Z.T
        d2 = np.maximum(d2, 0.0)
        for j in range(chunk.shape[0]):
            d2[j, i + j] = np.inf
        idx = d2.argmin(axis=1)
        nn_idx[i:i + chunk.shape[0]] = idx
        nn_d[i:i + chunk.shape[0]] = np.sqrt(d2[np.arange(chunk.shape[0]), idx])
    return nn_idx, nn_d


def knn_indices(Z: np.ndarray, k: int, batch: int = 512):
    n = Z.shape[0]
    znorm = (Z * Z).sum(axis=1)
    idx_out = np.empty((n, k), dtype=np.int32)
    for i in range(0, n, batch):
        chunk = Z[i:i + batch]
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + znorm[None, :] \
            - 2.0 * chunk @ Z.T
        d2 = np.maximum(d2, 0.0)
        for j in range(chunk.shape[0]):
            d2[j, i + j] = np.inf
        part = np.argpartition(d2, kth=k, axis=1)[:, :k]
        idx_out[i:i + chunk.shape[0]] = part
    return idx_out


def percentile(arr, p):
    return float(np.percentile(arr, p))


def pca_top(Z: np.ndarray, k: int = 3):
    cov = np.cov(Z, rowvar=False)
    vals, vecs = np.linalg.eigh(cov)
    order = np.argsort(-vals)[:k]
    return Z @ vecs[:, order], vals[order]


def feature_names():
    return list(FEATURE_SCALAR_KEYS)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", default="logs/dataset_audit.json")
    ap.add_argument("--eps-feat", type=float, default=0.10,
                    help="冗余判定的 feature L2 阈值（全集 z-score 空间）")
    ap.add_argument("--eps-label", type=float, default=0.10,
                    help="冗余判定的 label L2 阈值（全集 z-score 空间）")
    ap.add_argument("--knn", type=int, default=5,
                    help="难样本检测的 feature kNN 大小")
    ap.add_argument("--ambig-ratio", type=float, default=0.5,
                    help="难样本判定阈值：邻域 label std / 全局 label std")
    args = ap.parse_args()

    print(f"[audit] load {args.data}")
    wl, X, Y = load_dataset(Path(args.data))
    n, df = X.shape
    _, dl = Y.shape
    print(f"[audit] n={n}  feat_dim={df}  label_dim={dl}")

    Zf, _, _ = z_score(X)
    Zl, _, _ = z_score(Y)
    feat_names = feature_names()

    # ---------- A) 冗余检测 ----------
    print("\n[A] redundancy: feature dist < %.3f AND label dist < %.3f"
          % (args.eps_feat, args.eps_label))
    nn_idx_f, nn_d_f = pairwise_min_nn(Zf)
    # 用 same nn_idx 计算 label 距离
    nn_label_d = np.linalg.norm(Zl - Zl[nn_idx_f], axis=1)
    redundant_mask = (nn_d_f < args.eps_feat) & (nn_label_d < args.eps_label)

    print(f"  global: {redundant_mask.sum():>5}/{n} = "
          f"{redundant_mask.mean()*100:.1f}% redundant")
    print(f"  {'workload':<28} {'n':>5} {'redundant':>10} {'rate':>7} "
          f"{'feat_p50':>9} {'label_p50':>10}")
    per_wl_redundant = {}
    for w in sorted(set(wl)):
        m = wl == w
        rd = redundant_mask & m
        per_wl_redundant[w] = {
            "n": int(m.sum()),
            "n_redundant": int(rd.sum()),
            "rate": float(rd.mean() if m.sum() > 0 else 0.0)
                * (m.sum() / max(1, m.sum())),
            "nn_feat_p50": percentile(nn_d_f[m], 50),
            "nn_label_p50": percentile(nn_label_d[m], 50),
        }
        per_wl_redundant[w]["rate"] = (
            int(rd.sum()) / max(1, int(m.sum())))
        print(f"  {w:<28} {int(m.sum()):>5} {int(rd.sum()):>10} "
              f"{per_wl_redundant[w]['rate']*100:>6.1f}% "
              f"{per_wl_redundant[w]['nn_feat_p50']:>9.3f} "
              f"{per_wl_redundant[w]['nn_label_p50']:>10.3f}")

    # ---------- B) 难样本：特征不足 ----------
    print(f"\n[B] hard samples: kNN={args.knn} feature-only neighborhood, "
          f"label std / global label std > {args.ambig_ratio}")
    knn = knn_indices(Zf, args.knn)
    global_label_std = Zl.std(axis=0)
    global_label_std_safe = np.where(global_label_std < 1e-6,
                                     1.0, global_label_std)
    # 邻域 label std（per-dim）
    nbr_labels = Zl[knn]  # n x k x dl
    nbr_std = nbr_labels.std(axis=1)  # n x dl
    # ratio per dim per sample
    ratio_per_dim = nbr_std / global_label_std_safe[None, :]
    # 取 max over dim → 是否有"任一 label 维度"的邻域差异显著
    ratio_max = ratio_per_dim.max(axis=1)
    # 也存 dim_argmax 表示是哪一维 PMU 最难
    dim_argmax = ratio_per_dim.argmax(axis=1)

    hard_mask = ratio_max > args.ambig_ratio
    hard_global = float(hard_mask.mean())
    print(f"  global hard rate: {hard_mask.sum()}/{n} = {hard_global*100:.1f}%")

    print(f"  {'workload':<28} {'n':>5} {'hard':>6} {'rate':>7} "
          f"{'top_pmu_dim':<22}")
    per_wl_hard = {}
    for w in sorted(set(wl)):
        m = wl == w
        h = hard_mask & m
        if h.sum() > 0:
            top_dim_cnt = np.bincount(dim_argmax[h], minlength=dl)
            top_dim_idx = int(top_dim_cnt.argmax())
            top_pmu = PMU_KEYS[top_dim_idx]
        else:
            top_pmu = "-"
        per_wl_hard[w] = {
            "n": int(m.sum()),
            "n_hard": int(h.sum()),
            "rate": int(h.sum()) / max(1, int(m.sum())),
            "top_pmu_dim": top_pmu,
        }
        print(f"  {w:<28} {int(m.sum()):>5} {int(h.sum()):>6} "
              f"{per_wl_hard[w]['rate']*100:>6.1f}% {top_pmu:<22}")

    # 哪个 feature 维度的"邻域内方差大"分布最密集 → 提示该维度信号弱
    print("\n  top-5 hard-sample-dominant PMU label dims (which label is "
          "least separable by current v8 features):")
    pmu_hard_count = np.bincount(dim_argmax[hard_mask], minlength=dl)
    for i in np.argsort(-pmu_hard_count)[:5]:
        print(f"    {PMU_KEYS[i]:<22} {int(pmu_hard_count[i])} samples")

    # ---------- C) 分布覆盖 ----------
    print("\n[C] coverage")
    # C1 effective_n per workload (NN > 0.10 in feature-only)
    effective_n = {}
    for w in sorted(set(wl)):
        m = wl == w
        eff = int(((nn_d_f[m] > 0.10)).sum())
        effective_n[w] = {"n": int(m.sum()), "effective_n": eff}
    # C2 cross-workload nearest neighbor
    wl_arr = wl
    cross_nn_d = np.empty(n, dtype=np.float32)
    cross_nn_wl = np.empty(n, dtype=object)
    znorm = (Zf * Zf).sum(axis=1)
    BATCH = 512
    for i in range(0, n, BATCH):
        chunk = Zf[i:i + BATCH]
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + znorm[None, :] \
            - 2.0 * chunk @ Zf.T
        d2 = np.maximum(d2, 0.0)
        chunk_wl = wl_arr[i:i + BATCH]
        for j in range(chunk.shape[0]):
            d2[j, i + j] = np.inf
            same = (wl_arr == chunk_wl[j])
            d2_cross = np.where(same, np.inf, d2[j])
            mind = d2_cross.min()
            cross_nn_d[i + j] = (np.sqrt(mind)
                                 if np.isfinite(mind) else np.inf)
            cross_nn_wl[i + j] = (wl_arr[d2_cross.argmin()]
                                  if np.isfinite(mind) else "")
    print(f"  {'workload':<28} {'n':>5} {'eff_n':>6} {'cross_p50':>10} "
          f"{'cross_p95':>10} {'top_neighbor':<22}")
    per_wl_cov = {}
    for w in sorted(set(wl)):
        m = wl == w
        cd = cross_nn_d[m]
        votes = defaultdict(int)
        for cw in cross_nn_wl[m]:
            if cw:
                votes[cw] += 1
        top = max(votes.items(), key=lambda x: x[1]) if votes else ("", 0)
        per_wl_cov[w] = {
            "n": int(m.sum()),
            "effective_n_gt_0.10": effective_n[w]["effective_n"],
            "cross_p50": percentile(cd, 50),
            "cross_p95": percentile(cd, 95),
            "top_cross_neighbor": top[0],
            "top_cross_neighbor_votes": int(top[1]),
        }
        print(f"  {w:<28} {int(m.sum()):>5} "
              f"{effective_n[w]['effective_n']:>6} "
              f"{per_wl_cov[w]['cross_p50']:>10.3f} "
              f"{per_wl_cov[w]['cross_p95']:>10.3f} "
              f"{top[0]:<18}({top[1]})")

    # C3 label 分布偏斜（log10 cpi 直方）
    cpi_idx = PMU_KEYS.index("cpi_uop")
    cpi = Y[:, cpi_idx]
    log_cpi = np.log10(np.maximum(cpi, 1e-3))
    bins = np.linspace(-3, 2, 11)
    hist, edges = np.histogram(log_cpi, bins=bins)
    print("\n  cpi distribution (log10 bins):")
    for i, c in enumerate(hist):
        bar = "#" * int(40 * c / max(hist.max(), 1))
        print(f"    [{edges[i]:+.1f}, {edges[i+1]:+.1f})  "
              f"{c:>5}  {bar}")

    # C4 全局 feature 主轴覆盖
    proj, _ = pca_top(Zf, k=3)
    pc_range = {
        f"pc{i+1}": {"p05": percentile(proj[:, i], 5),
                     "p50": percentile(proj[:, i], 50),
                     "p95": percentile(proj[:, i], 95),
                     "std": float(proj[:, i].std())}
        for i in range(3)
    }
    print("\n  feature PCA top-3 ranges (z-scored space):")
    for k, v in pc_range.items():
        print(f"    {k}  p05={v['p05']:+.2f} p50={v['p50']:+.2f} "
              f"p95={v['p95']:+.2f} std={v['std']:.2f}")

    # ---------- Output JSON ----------
    out = {
        "dataset": args.data,
        "n_samples": int(n),
        "feat_dim": int(df),
        "label_dim": int(dl),
        "redundancy": {
            "eps_feat": args.eps_feat,
            "eps_label": args.eps_label,
            "global_rate": float(redundant_mask.mean()),
            "per_workload": per_wl_redundant,
        },
        "hard_samples": {
            "knn": args.knn,
            "ambig_ratio": args.ambig_ratio,
            "global_rate": float(hard_mask.mean()),
            "per_workload": per_wl_hard,
            "pmu_hard_count": {PMU_KEYS[i]: int(pmu_hard_count[i])
                               for i in range(dl)},
        },
        "coverage": {
            "per_workload": per_wl_cov,
            "cpi_log10_hist": {
                "bin_edges": [float(x) for x in edges.tolist()],
                "counts": [int(x) for x in hist.tolist()],
            },
            "pca_top3_range": pc_range,
        },
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n[audit] report -> {out_path}")

    print("\n[audit] verdict:")
    if hard_global > 0.30:
        print(f"  WARN  global hard rate {hard_global*100:.1f}% > 30% "
              "→ feature space likely insufficient. "
              "Add features that target dims listed in [B].")
    if redundant_mask.mean() > 0.50:
        print(f"  WARN  redundancy {redundant_mask.mean()*100:.1f}% > 50% "
              "→ heavy sample duplication. "
              "Add WeightedSampler or run dedup.")
    isolated = [w for w, info in per_wl_cov.items()
                if info["cross_p50"] > 3.0]
    if isolated:
        print(f"  WARN  isolated workloads (cross_p50>3.0): {isolated} "
              "→ no neighbor support, won't generalize from train.")


if __name__ == "__main__":
    main()
