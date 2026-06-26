#!/usr/bin/env python3
"""OOD scan: 对每个 holdout 窗口在训练集中找 k-NN，输出 distance 分布。

输入特征向量从每个 sample 的 core_summary 抽取（per-window 取 8 核 mean），
v8 共 36 维：op mix / memory refinement / dependency chain /
indirect target behavior / retained structural-locality signals。

每个特征用训练集的 (mean, std) 做 z-score，distance 用 L2。
NN 搜索：暴力 cdist (sklearn 不可依赖，避免装包) — 训练集 13k 样本数量不大。

报告：
  - 训练集自身（leave-one-out 取最近邻）的 distance 分布作为 baseline
  - 每个 holdout workload 的 distance 分布
  - 覆盖率：holdout 中 distance <= train p95 的样本占比
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from model import tokenizer as tk  # noqa: E402

FEATURE_SCALAR_KEYS = list(tk.SUMMARY_FEATURE_KEYS)


def extract_sample_vec(sample: dict) -> np.ndarray:
    cs_list = sample.get("core_summary") or []
    if not cs_list:
        return np.zeros(0, dtype=np.float32)
    scalars: list[list[float]] = [[] for _ in FEATURE_SCALAR_KEYS]
    for cs in cs_list:
        if not isinstance(cs, dict):
            continue
        for i, k in enumerate(FEATURE_SCALAR_KEYS):
            v = cs.get(k, 0.0)
            try:
                scalars[i].append(float(v))
            except Exception:
                scalars[i].append(0.0)
    if not any(scalars):
        return np.zeros(0, dtype=np.float32)
    return np.array([np.mean(xs) if xs else 0.0 for xs in scalars],
                    dtype=np.float32)


def load_windows(path: Path) -> tuple[list[str], np.ndarray]:
    workloads: list[str] = []
    vecs: list[np.ndarray] = []
    dim = None
    with path.open() as f:
        for ln in f:
            if not ln.startswith("{"):
                continue
            r = json.loads(ln)
            v = extract_sample_vec(r)
            if v.size == 0:
                continue
            if dim is None:
                dim = v.size
            elif v.size != dim:
                continue
            workloads.append(r["workload"])
            vecs.append(v)
    if not vecs:
        raise SystemExit(f"empty feature set: {path}")
    return workloads, np.stack(vecs, axis=0)


def percentile(arr: np.ndarray, p: float) -> float:
    return float(np.percentile(arr, p))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", required=True)
    ap.add_argument("--holdout", required=True)
    ap.add_argument("--out", default="logs/ood_holdout_scan.json")
    ap.add_argument("--k", type=int, default=1)
    args = ap.parse_args()

    train_path = Path(args.train)
    holdout_path = Path(args.holdout)
    print(f"[ood] load train {train_path}")
    tr_workloads, tr_X = load_windows(train_path)
    print(f"[ood] load holdout {holdout_path}")
    ho_workloads, ho_X = load_windows(holdout_path)
    print(f"[ood] train n={tr_X.shape[0]} dim={tr_X.shape[1]} | "
          f"holdout n={ho_X.shape[0]} dim={ho_X.shape[1]}")

    mean = tr_X.mean(axis=0)
    std = tr_X.std(axis=0)
    std = np.where(std < 1e-6, 1.0, std)

    tr_Z = (tr_X - mean) / std
    ho_Z = (ho_X - mean) / std

    # train self-NN baseline: leave-one-out via masking diagonal
    tr_norm = (tr_Z * tr_Z).sum(axis=1)
    print("[ood] computing train self-NN baseline ...")
    tr_dists_min = np.empty(tr_Z.shape[0], dtype=np.float32)
    BATCH = 512
    for i in range(0, tr_Z.shape[0], BATCH):
        chunk = tr_Z[i:i + BATCH]
        # ||a-b||^2 = ||a||^2 + ||b||^2 - 2 a.b
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + tr_norm[None, :] \
            - 2.0 * chunk @ tr_Z.T
        # mask self
        for j in range(chunk.shape[0]):
            d2[j, i + j] = np.inf
        tr_dists_min[i:i + BATCH] = np.sqrt(np.maximum(d2.min(axis=1), 0.0))

    print("[ood] computing holdout to-train NN ...")
    ho_dists_min = np.empty(ho_Z.shape[0], dtype=np.float32)
    for i in range(0, ho_Z.shape[0], BATCH):
        chunk = ho_Z[i:i + BATCH]
        d2 = (chunk * chunk).sum(axis=1, keepdims=True) + tr_norm[None, :] \
            - 2.0 * chunk @ tr_Z.T
        ho_dists_min[i:i + BATCH] = np.sqrt(np.maximum(d2.min(axis=1), 0.0))

    tr_p50 = percentile(tr_dists_min, 50)
    tr_p95 = percentile(tr_dists_min, 95)
    tr_p99 = percentile(tr_dists_min, 99)
    tr_max = float(tr_dists_min.max())
    print(f"[train-baseline] NN distance: "
          f"p50={tr_p50:.3f} p95={tr_p95:.3f} p99={tr_p99:.3f} max={tr_max:.3f}")

    # break down by train workload too
    train_by_wl: dict[str, list[float]] = defaultdict(list)
    for w, d in zip(tr_workloads, tr_dists_min):
        train_by_wl[w].append(float(d))

    report = {
        "train_self_nn": {
            "n": int(tr_X.shape[0]),
            "dim": int(tr_X.shape[1]),
            "p50": tr_p50,
            "p95": tr_p95,
            "p99": tr_p99,
            "max": tr_max,
            "per_workload": {
                w: {
                    "n": len(ds),
                    "p50": percentile(np.asarray(ds), 50),
                    "p95": percentile(np.asarray(ds), 95),
                    "max": float(np.max(ds)),
                }
                for w, ds in sorted(train_by_wl.items())
            },
        },
        "holdout": {},
    }

    ho_by_wl: dict[str, list[float]] = defaultdict(list)
    for w, d in zip(ho_workloads, ho_dists_min):
        ho_by_wl[w].append(float(d))

    print()
    print("[holdout] per-workload OOD distance:")
    print(f"  {'workload':<28} {'n':>5} {'p50':>8} {'p95':>8} {'max':>8} "
          f"{'cov@p95':>8} {'cov@p99':>8} {'cov@max':>8}")
    for w, ds_list in sorted(ho_by_wl.items()):
        ds = np.asarray(ds_list)
        p50 = percentile(ds, 50)
        p95 = percentile(ds, 95)
        mx = float(ds.max())
        cov_p95 = float((ds <= tr_p95).mean())
        cov_p99 = float((ds <= tr_p99).mean())
        cov_max = float((ds <= tr_max).mean())
        print(f"  {w:<28} {len(ds):>5} {p50:>8.3f} {p95:>8.3f} {mx:>8.3f} "
              f"{cov_p95:>8.2%} {cov_p99:>8.2%} {cov_max:>8.2%}")
        report["holdout"][w] = {
            "n": len(ds),
            "p50": p50,
            "p95": p95,
            "max": mx,
            "coverage_at_train_p95": cov_p95,
            "coverage_at_train_p99": cov_p99,
            "coverage_at_train_max": cov_max,
        }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    print(f"\n[ood] report -> {out_path}")


if __name__ == "__main__":
    main()
