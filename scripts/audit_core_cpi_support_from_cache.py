#!/usr/bin/env python3
"""Pre-train audit for per-core CPI separability from tensor cache.

This script does not train the LLM.  It asks whether cheap features already
stored in the local-core tensor cache contain enough signal to distinguish
which core has higher/lower CPI within the same window.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import torch


PMU_KEYS = [
    "cpi_uop", "branch_miss", "l1d_ld_miss",
    "l1d_st_miss", "l1i_miss", "llc_miss",
    "dtlb_miss", "mshr_avg",
]

SIDE_KEYS = [
    "log1p_active_cores",
    "log1p_uops_core",
    "log1p_uops_window_total",
    "log1p_instr_retired",
    "core_fill_ratio",
    "log1p_branch_count",
    "log1p_cond_branch_count",
    "log1p_indirect_branch_count",
    "log1p_load_count",
    "log1p_store_count",
    "log1p_atomic_count",
    "log1p_mem_ops",
    "log1p_distinct_data_lines_core",
    "log1p_distinct_data_pages_core",
    "log1p_global_distinct_data_lines",
    "log1p_global_distinct_data_pages",
    "shared_store_rate",
    "multi_writer_line_frac",
    "max_writer_cores_per_line_log",
    "writer_core_coverage",
    "pairwise_writer_pressure",
    "store_owner_switch_rate",
    "inval_fanout_proxy_mean",
    "disjoint_store_slot_pair_rate",
    "aggregate_load_density",
    "aggregate_mem_density",
    "global_large_stride_rate",
    "random_access_pressure",
    "lines_per_kuop_global",
    "pages_per_kuop_global",
    "core_shared_store_rate",
    "core_shared_load_rate",
    "core_multi_writer_store_rate",
    "core_random_load_density",
]

DENOM_KEYS = [
    "branch_count",
    "loads",
    "stores",
    "atomics",
    "mem_ops",
    "page_touches",
]


def _as_np(x) -> np.ndarray:
    if isinstance(x, torch.Tensor):
        return x.cpu().numpy()
    return np.asarray(x)


def _percentiles(x: np.ndarray) -> dict[str, float]:
    if x.size == 0:
        return {"mean": float("nan"), "p50": float("nan"), "p90": float("nan")}
    return {
        "mean": float(np.mean(x)),
        "p50": float(np.percentile(x, 50)),
        "p90": float(np.percentile(x, 90)),
    }


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def _ridge_fit_eval(
    A: np.ndarray,
    y: np.ndarray,
    train_mask: np.ndarray,
    window_ids: np.ndarray,
    core_ids: np.ndarray,
    workloads: np.ndarray,
    name: str,
    lam: float,
) -> dict:
    tr = train_mask
    te = ~train_mask
    atr = A[tr]
    ate = A[te]
    ytr = y[tr]
    yte = y[te]
    gram = atr.T @ atr + lam * np.eye(atr.shape[1], dtype=np.float64)
    rhs = atr.T @ ytr
    coef = np.linalg.solve(gram, rhs)
    pred = ate @ coef

    mse = float(np.mean((pred - yte) ** 2))
    base_mse = float(np.mean(yte ** 2))
    out = {
        "name": name,
        "core_residual_corr": _safe_corr(pred, yte),
        "r2_vs_window_mean_collapse": float(1.0 - mse / (base_mse + 1e-12)),
        "mae_log_residual": float(np.mean(np.abs(pred - yte))),
        "base_mae_log_residual": float(np.mean(np.abs(yte))),
    }

    by_window: dict[int, list[tuple[int, float, float, str]]] = defaultdict(list)
    te_indices = np.nonzero(te)[0]
    for idx, p, t in zip(te_indices, pred, yte):
        by_window[int(window_ids[idx])].append(
            (int(core_ids[idx]), float(p), float(t), str(workloads[idx]))
        )

    corrs: list[float] = []
    slow_hit = 0
    fast_hit = 0
    nwin = 0
    by_wl: dict[str, list] = defaultdict(lambda: [0, 0, 0, []])
    for vals in by_window.values():
        if len(vals) < 2:
            continue
        vals = sorted(vals)
        pred_v = np.asarray([v[1] for v in vals], dtype=np.float64)
        true_v = np.asarray([v[2] for v in vals], dtype=np.float64)
        c = _safe_corr(pred_v, true_v)
        if math.isfinite(c):
            corrs.append(c)
        ps = int(np.argmax(pred_v))
        ts = int(np.argmax(true_v))
        pf = int(np.argmin(pred_v))
        tf = int(np.argmin(true_v))
        sh = int(ps == ts)
        fh = int(pf == tf)
        wl = vals[0][3]
        nwin += 1
        slow_hit += sh
        fast_hit += fh
        by_wl[wl][0] += 1
        by_wl[wl][1] += sh
        by_wl[wl][2] += fh
        if math.isfinite(c):
            by_wl[wl][3].append(c)

    out.update({
        "window_corr_mean": float(np.mean(corrs)) if corrs else float("nan"),
        "window_corr_p50": float(np.percentile(corrs, 50)) if corrs else float("nan"),
        "slowest_hit_rate": float(slow_hit / max(1, nwin)),
        "fastest_hit_rate": float(fast_hit / max(1, nwin)),
        "test_windows": int(nwin),
        "per_workload": {
            wl: {
                "n_windows": int(v[0]),
                "slowest_hit_rate": float(v[1] / max(1, v[0])),
                "fastest_hit_rate": float(v[2] / max(1, v[0])),
                "window_corr_mean": float(np.mean(v[3])) if v[3] else float("nan"),
            }
            for wl, v in sorted(by_wl.items())
        },
    })
    return out


def _top_feature_corrs(
    X: np.ndarray,
    y: np.ndarray,
    names: list[str],
    train_mask: np.ndarray,
    k: int = 16,
) -> list[dict[str, float | str]]:
    Xtr = X[train_mask]
    ytr = y[train_mask]
    out = []
    for i, name in enumerate(names):
        c = _safe_corr(Xtr[:, i], ytr)
        if math.isfinite(c):
            out.append({"feature": name, "corr": float(c), "abs_corr": float(abs(c))})
    out.sort(key=lambda d: -float(d["abs_corr"]))
    return out[:k]


def _is_size_feature(name: str) -> bool:
    needles = [
        "uops",
        "instr",
        "token_len",
        "uop_tokens",
        "core_fill_ratio",
        "denom:log1p_",
        "log1p_branch_count",
        "log1p_cond_branch_count",
        "log1p_indirect_branch_count",
        "log1p_load_count",
        "log1p_store_count",
        "log1p_atomic_count",
        "log1p_mem_ops",
        "local:uop_field_sum_",
    ]
    return any(s in name for s in needles)


def load_rows(cache_dir: Path, n_core_filter: int | None, max_windows: int):
    manifest = torch.load(cache_dir / "manifest.pt", map_location="cpu")
    label_keys = list((manifest.get("meta") or {}).get("pmu_keys") or PMU_KEYS)
    if "cpi_uop" not in label_keys:
        raise SystemExit(f"cpi_uop missing from cache pmu_keys: {label_keys}")
    cpi_idx = label_keys.index("cpi_uop")

    rows_x: list[np.ndarray] = []
    rows_y: list[float] = []
    rows_wid: list[int] = []
    rows_core: list[int] = []
    rows_wl: list[str] = []
    label_spread: list[float] = []
    label_cv: list[float] = []
    slow_counts: Counter[int] = Counter()
    fast_counts: Counter[int] = Counter()
    wl_counts: Counter[str] = Counter()
    window_id = 0
    feature_names = (
        [f"side:{k}" for k in SIDE_KEYS]
        + [f"denom:log1p_{k}" for k in DENOM_KEYS]
        + ["log1p_instr_retired", "log1p_uops", "t_start_rel"]
        + ["local:log1p_token_len", "local:log1p_uop_tokens"]
        + [f"local:uop_field_sum_{i}" for i in range(6)]
        + [f"local:uop_field_mean_{i}" for i in range(6)]
    )

    for shard_meta in manifest.get("shards", []):
        shard = torch.load(cache_dir / shard_meta["file"], map_location="cpu")
        n_core = _as_np(shard["n_core"]).astype(np.int64)
        label = _as_np(shard["label"]).astype(np.float64)
        side = _as_np(shard["side_feats"]).astype(np.float64)
        den = _as_np(shard["denoms"]).astype(np.float64)
        instr = _as_np(shard["instr_retired"]).astype(np.float64)
        uops = _as_np(shard["uops"]).astype(np.float64)
        t_start = _as_np(shard["t_start_rel"]).astype(np.float64)
        workloads = list(shard.get("workload") or [""] * len(n_core))

        has_local = (
            "local_core_offsets" in shard
            and "local_ids_offsets" in shard
            and "local_uop_fields_flat" in shard
            and "local_is_uop_flat" in shard
        )
        if has_local:
            local_core_offsets = _as_np(shard["local_core_offsets"]).astype(np.int64)
            local_ids_offsets = _as_np(shard["local_ids_offsets"]).astype(np.int64)
            local_is_uop = _as_np(shard["local_is_uop_flat"]).astype(bool)
            local_uop_fields = _as_np(shard["local_uop_fields_flat"]).astype(np.float64)

        for i, nc_raw in enumerate(n_core):
            nc = int(nc_raw)
            if nc <= 1:
                continue
            if n_core_filter is not None and nc != n_core_filter:
                continue
            if max_windows > 0 and window_id >= max_windows:
                break
            cpi = label[i, :nc, cpi_idx]
            if not np.all(np.isfinite(cpi)) or np.any(cpi <= 0):
                continue
            logc = np.log(cpi)
            y = logc - float(np.mean(logc))
            label_spread.append(float(np.std(y)))
            label_cv.append(float(np.std(cpi) / (float(np.mean(cpi)) + 1e-12)))
            slow_counts[int(np.argmax(cpi))] += 1
            fast_counts[int(np.argmin(cpi))] += 1
            wl = str(workloads[i])
            wl_counts[wl] += 1

            feats = []
            for ci in range(nc):
                vec: list[float] = []
                vec.extend(float(v) for v in side[i, ci, :len(SIDE_KEYS)])
                vec.extend(math.log1p(max(0.0, float(v))) for v in den[i, ci, :len(DENOM_KEYS)])
                vec.append(math.log1p(max(0.0, float(instr[i, ci]))))
                vec.append(math.log1p(max(0.0, float(uops[i, ci]))))
                vec.append(float(t_start[i, ci]))

                if has_local:
                    core_base = int(local_core_offsets[i])
                    local_idx = core_base + ci
                    b = int(local_ids_offsets[local_idx])
                    e = int(local_ids_offsets[local_idx + 1])
                    is_u = local_is_uop[b:e]
                    uf = local_uop_fields[b:e]
                    ucnt = int(is_u.sum())
                    vec.append(math.log1p(max(0, e - b)))
                    vec.append(math.log1p(max(0, ucnt)))
                    if ucnt > 0:
                        uuf = uf[is_u]
                        sums = uuf.sum(axis=0)
                        means = uuf.mean(axis=0)
                    else:
                        sums = np.zeros((6,), dtype=np.float64)
                        means = np.zeros((6,), dtype=np.float64)
                    vec.extend(float(v) for v in sums)
                    vec.extend(float(v) for v in means)
                else:
                    vec.extend([0.0] * 14)
                feats.append(vec)

            X = np.asarray(feats, dtype=np.float64)
            X = X - X.mean(axis=0, keepdims=True)
            for ci in range(nc):
                rows_x.append(X[ci])
                rows_y.append(float(y[ci]))
                rows_wid.append(window_id)
                rows_core.append(ci)
                rows_wl.append(wl)
            window_id += 1
        if max_windows > 0 and window_id >= max_windows:
            break

    if not rows_x:
        raise SystemExit("no multi-core windows matched the requested filter")
    return {
        "X": np.stack(rows_x, axis=0),
        "y": np.asarray(rows_y, dtype=np.float64),
        "window_ids": np.asarray(rows_wid, dtype=np.int64),
        "core_ids": np.asarray(rows_core, dtype=np.int64),
        "workloads": np.asarray(rows_wl, dtype=object),
        "feature_names": feature_names,
        "label_spread": np.asarray(label_spread, dtype=np.float64),
        "label_cv": np.asarray(label_cv, dtype=np.float64),
        "slow_counts": slow_counts,
        "fast_counts": fast_counts,
        "wl_counts": wl_counts,
        "n_windows": window_id,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--n-core", type=int, default=8)
    ap.add_argument("--max-windows", type=int, default=0)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=20260704)
    ap.add_argument("--ridge", type=float, default=10.0)
    ap.add_argument("--drop-size-features", action="store_true",
                    help="Drop uops/token length/count features; keeps ratio/content/shared features.")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cache_dir = Path(args.cache)
    data = load_rows(cache_dir, args.n_core, args.max_windows)
    X = data["X"]
    y = data["y"]
    window_ids = data["window_ids"]
    core_ids = data["core_ids"]
    workloads = data["workloads"]

    unique_windows = np.unique(window_ids)
    rng = np.random.RandomState(args.seed)
    shuffled = unique_windows.copy()
    rng.shuffle(shuffled)
    n_val = max(1, int(round(len(shuffled) * args.val_frac)))
    val_windows = set(int(x) for x in shuffled[:n_val])
    train_mask = np.asarray([int(w) not in val_windows for w in window_ids], dtype=bool)

    mu = X[train_mask].mean(axis=0)
    sd = X[train_mask].std(axis=0)
    keep = sd > 1e-8
    if args.drop_size_features:
        keep = np.asarray([
            bool(k) and not _is_size_feature(n)
            for n, k in zip(data["feature_names"], keep)
        ], dtype=bool)
        if not np.any(keep):
            raise SystemExit("--drop-size-features removed every usable feature")
    Z = (X[:, keep] - mu[keep]) / sd[keep]
    kept_names = [n for n, k in zip(data["feature_names"], keep) if k]

    core_oh = np.zeros((len(core_ids), args.n_core), dtype=np.float64)
    clipped_core = np.minimum(core_ids, args.n_core - 1)
    core_oh[np.arange(len(core_ids)), clipped_core] = 1.0
    core_oh = core_oh - core_oh[train_mask].mean(axis=0, keepdims=True)

    reports = [
        _ridge_fit_eval(
            Z, y, train_mask, window_ids, core_ids, workloads,
            "feature_residuals_only", args.ridge,
        ),
        _ridge_fit_eval(
            np.concatenate([Z, core_oh], axis=1), y, train_mask,
            window_ids, core_ids, workloads,
            "feature_residuals_plus_core_id", args.ridge,
        ),
    ]

    means = np.zeros((args.n_core,), dtype=np.float64)
    counts = np.zeros((args.n_core,), dtype=np.float64)
    for ci, yy in zip(core_ids[train_mask], y[train_mask]):
        c = min(int(ci), args.n_core - 1)
        means[c] += yy
        counts[c] += 1
    means = means / np.maximum(counts, 1.0)
    core_pred = means[np.minimum(core_ids[~train_mask], args.n_core - 1)]
    y_val = y[~train_mask]
    core_prior = {
        "name": "core_id_prior_only",
        "core_means": [float(v) for v in means],
        "core_residual_corr": _safe_corr(core_pred, y_val),
        "r2_vs_window_mean_collapse": float(
            1.0 - np.mean((core_pred - y_val) ** 2) / (np.mean(y_val ** 2) + 1e-12)
        ),
        "mae_log_residual": float(np.mean(np.abs(core_pred - y_val))),
        "base_mae_log_residual": float(np.mean(np.abs(y_val))),
    }

    result = {
        "cache": str(cache_dir),
        "n_core_filter": int(args.n_core),
        "max_windows": int(args.max_windows),
        "n_windows": int(data["n_windows"]),
        "n_core_samples": int(len(y)),
        "workload_counts": dict(data["wl_counts"].most_common()),
        "label_log_residual_std": _percentiles(data["label_spread"]),
        "label_cpi_cv": _percentiles(data["label_cv"]),
        "slowest_core_counts": {str(k): int(v) for k, v in sorted(data["slow_counts"].items())},
        "fastest_core_counts": {str(k): int(v) for k, v in sorted(data["fast_counts"].items())},
        "top_feature_corrs": _top_feature_corrs(Z, y, kept_names, train_mask),
        "baselines": [core_prior] + reports,
    }

    print(f"[audit] cache={cache_dir}")
    print(f"[audit] n_core={args.n_core} windows={result['n_windows']} core_samples={result['n_core_samples']}")
    print(f"[label] log_residual_std={result['label_log_residual_std']}")
    print(f"[label] cpi_cv={result['label_cpi_cv']}")
    print(f"[label] slowest_core_counts={result['slowest_core_counts']}")
    print(f"[label] fastest_core_counts={result['fastest_core_counts']}")
    print("\n[top feature corr | target=log(CPI_i)-window_mean_logCPI]")
    for item in result["top_feature_corrs"][:12]:
        print(f"  {item['corr']:+.4f}  {item['feature']}")
    print("\n[baselines]")
    for b in result["baselines"]:
        print(
            f"  {b['name']}: corr={b['core_residual_corr']:.4f} "
            f"R2_vs_collapse={b['r2_vs_window_mean_collapse']:.4f} "
            f"MAE={b['mae_log_residual']:.4f} base_MAE={b['base_mae_log_residual']:.4f}"
        )
        if "slowest_hit_rate" in b:
            print(
                f"    window_corr_mean={b['window_corr_mean']:.4f} "
                f"slowest_hit={b['slowest_hit_rate']:.4f} "
                f"fastest_hit={b['fastest_hit_rate']:.4f} "
                f"test_windows={b['test_windows']}"
            )
            for wl, wlr in sorted(
                b["per_workload"].items(),
                key=lambda kv: -kv[1]["n_windows"],
            )[:12]:
                print(
                    f"    {wl:<28} n={wlr['n_windows']:4d} "
                    f"slow={wlr['slowest_hit_rate']:.3f} "
                    f"fast={wlr['fastest_hit_rate']:.3f} "
                    f"corr={wlr['window_corr_mean']:.3f}"
                )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True))
        print(f"\n[audit] wrote {out}")


if __name__ == "__main__":
    main()
