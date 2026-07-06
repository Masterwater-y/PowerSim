#!/usr/bin/env python3
"""Probe whether per-core hidden states contain CPI identity signal.

This freezes the trained model and fits cheap ridge probes from hidden states
to the within-window target:

    y_i = log(CPI_i) - mean_core log(CPI)

The result answers whether query/pre-adapter/post-adapter hidden vectors contain
enough information to separate slow/fast cores, independent of the trained CPI
head.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import torch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import analyze_core_hidden_similarity as hidden_diag  # noqa: E402


def safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if a.size < 2 or np.std(a) < 1.0e-12 or np.std(b) < 1.0e-12:
        return float("nan")
    return float(np.corrcoef(a, b)[0, 1])


def summarize(x: np.ndarray) -> dict[str, float | int]:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    if x.size == 0:
        return {"n": 0}
    return {
        "n": int(x.size),
        "mean": float(np.mean(x)),
        "p50": float(np.percentile(x, 50)),
        "p90": float(np.percentile(x, 90)),
        "p95": float(np.percentile(x, 95)),
        "min": float(np.min(x)),
        "max": float(np.max(x)),
    }


def fit_ridge_probe(
    X: np.ndarray,
    y: np.ndarray,
    train_mask: np.ndarray,
    window_ids: np.ndarray,
    core_ids: np.ndarray,
    workloads: np.ndarray,
    lam: float,
    name: str,
) -> dict:
    X = np.asarray(X, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    train_mask = np.asarray(train_mask, dtype=bool)
    test_mask = ~train_mask

    mu = X[train_mask].mean(axis=0)
    sd = X[train_mask].std(axis=0)
    keep = sd > 1.0e-8
    if not np.any(keep):
        raise ValueError(f"{name}: no non-constant probe features")
    Z = (X[:, keep] - mu[keep]) / sd[keep]

    A = Z[train_mask]
    b = y[train_mask]
    At = Z[test_mask]
    yt = y[test_mask]

    gram = A.T @ A + float(lam) * np.eye(A.shape[1], dtype=np.float64)
    rhs = A.T @ b
    try:
        coef = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:
        coef = np.linalg.lstsq(gram, rhs, rcond=None)[0]
    pred = At @ coef

    mse = float(np.mean((pred - yt) ** 2))
    base_mse = float(np.mean(yt ** 2))
    out = {
        "name": name,
        "feature_dim": int(X.shape[1]),
        "kept_dim": int(np.sum(keep)),
        "test_core_samples": int(np.sum(test_mask)),
        "corr": safe_corr(pred, yt),
        "r2_vs_collapse": float(1.0 - mse / (base_mse + 1.0e-12)),
        "mae": float(np.mean(np.abs(pred - yt))),
        "base_mae": float(np.mean(np.abs(yt))),
    }

    by_window: dict[int, list[tuple[int, float, float, str]]] = defaultdict(list)
    test_indices = np.nonzero(test_mask)[0]
    for idx, p, yy in zip(test_indices, pred, yt):
        by_window[int(window_ids[idx])].append(
            (int(core_ids[idx]), float(p), float(yy), str(workloads[idx]))
        )

    corrs: list[float] = []
    slow_hit = 0
    fast_hit = 0
    nwin = 0
    per_wl: dict[str, list] = defaultdict(lambda: [0, 0, 0, []])
    for vals in by_window.values():
        if len(vals) < 2:
            continue
        vals = sorted(vals)
        pv = np.asarray([v[1] for v in vals], dtype=np.float64)
        tv = np.asarray([v[2] for v in vals], dtype=np.float64)
        c = safe_corr(pv, tv)
        if math.isfinite(c):
            corrs.append(c)
        sh = int(int(np.argmax(pv)) == int(np.argmax(tv)))
        fh = int(int(np.argmin(pv)) == int(np.argmin(tv)))
        wl = vals[0][3]
        nwin += 1
        slow_hit += sh
        fast_hit += fh
        per_wl[wl][0] += 1
        per_wl[wl][1] += sh
        per_wl[wl][2] += fh
        if math.isfinite(c):
            per_wl[wl][3].append(c)

    out.update({
        "test_windows": int(nwin),
        "window_corr_mean": float(np.mean(corrs)) if corrs else float("nan"),
        "window_corr_p50": float(np.percentile(corrs, 50)) if corrs else float("nan"),
        "slowest_hit_rate": float(slow_hit / max(1, nwin)),
        "fastest_hit_rate": float(fast_hit / max(1, nwin)),
        "per_workload": {
            wl: {
                "n_windows": int(v[0]),
                "slowest_hit_rate": float(v[1] / max(1, v[0])),
                "fastest_hit_rate": float(v[2] / max(1, v[0])),
                "window_corr_mean": float(np.mean(v[3])) if v[3] else float("nan"),
            }
            for wl, v in sorted(per_wl.items())
        },
    })
    return out


def core_id_prior(
    y: np.ndarray,
    train_mask: np.ndarray,
    window_ids: np.ndarray,
    core_ids: np.ndarray,
    workloads: np.ndarray,
    n_core: int,
) -> dict:
    means = np.zeros((n_core,), dtype=np.float64)
    counts = np.zeros((n_core,), dtype=np.float64)
    for c, yy in zip(core_ids[train_mask], y[train_mask]):
        cc = min(max(int(c), 0), n_core - 1)
        means[cc] += yy
        counts[cc] += 1.0
    means = means / np.maximum(counts, 1.0)

    X = means[np.minimum(np.maximum(core_ids, 0), n_core - 1)].reshape(-1, 1)
    return fit_ridge_probe(
        X, y, train_mask, window_ids, core_ids, workloads,
        lam=0.0, name="core_id_prior_only",
    )


def collect_hidden(args: argparse.Namespace) -> dict:
    device = args.device or ("cuda" if torch.cuda.is_available() else "cpu")
    model_args = argparse.Namespace(ckpt=args.ckpt, max_len=args.max_len)
    model, tok, use_tstart = hidden_diag.load_model_and_tokenizer(
        model_args, device
    )
    input_mode = getattr(model.cfg, "model_input_mode", "global")
    collate = hidden_diag.make_collate(tok.pad_token_id)

    stage_rows: dict[str, list[np.ndarray]] = defaultdict(list)
    side_rows: list[np.ndarray] = []
    y_rows: list[np.ndarray] = []
    window_rows: list[np.ndarray] = []
    core_rows: list[np.ndarray] = []
    workload_rows: list[np.ndarray] = []
    label_spread: list[float] = []
    label_cv: list[float] = []

    batch_samples = []
    sample_idx = 0
    with torch.no_grad():
        for sample in hidden_diag.iter_samples(args, tok, input_mode):
            batch_samples.append(sample)
            if len(batch_samples) < args.batch_size:
                continue
            sample_idx = process_batch(
                model, collate, batch_samples, use_tstart, device,
                sample_idx, stage_rows, side_rows, y_rows, window_rows,
                core_rows, workload_rows, label_spread, label_cv,
            )
            batch_samples = []
        if batch_samples:
            sample_idx = process_batch(
                model, collate, batch_samples, use_tstart, device,
                sample_idx, stage_rows, side_rows, y_rows, window_rows,
                core_rows, workload_rows, label_spread, label_cv,
            )

    if sample_idx <= 1:
        raise SystemExit("[err] need at least two multi-core samples")

    return {
        "device": str(device),
        "use_tstart": bool(use_tstart),
        "model_input_mode": str(input_mode),
        "stage_X": {
            k: np.concatenate(v, axis=0)
            for k, v in stage_rows.items()
        },
        "side_X": np.concatenate(side_rows, axis=0),
        "y": np.concatenate(y_rows, axis=0),
        "window_ids": np.concatenate(window_rows, axis=0),
        "core_ids": np.concatenate(core_rows, axis=0),
        "workloads": np.concatenate(workload_rows, axis=0),
        "label_log_residual_std": np.asarray(label_spread, dtype=np.float64),
        "label_cpi_cv": np.asarray(label_cv, dtype=np.float64),
        "samples": int(sample_idx),
    }


def process_batch(
    model,
    collate,
    batch_samples: list[dict],
    use_tstart: bool,
    device: str,
    sample_idx: int,
    stage_rows: dict[str, list[np.ndarray]],
    side_rows: list[np.ndarray],
    y_rows: list[np.ndarray],
    window_rows: list[np.ndarray],
    core_rows: list[np.ndarray],
    workload_rows: list[np.ndarray],
    label_spread: list[float],
    label_cv: list[float],
) -> int:
    batch = collate(batch_samples)
    stages, _ = hidden_diag.gather_query_hidden(model, batch, use_tstart, device)
    labels = batch["label"].float()
    side = batch["side_feats"].float()
    core_mask = batch["core_mask"].bool()

    for bi, sample in enumerate(batch_samples):
        active = core_mask[bi]
        cpi = labels[bi, active, hidden_diag.CPI_IDX].cpu().numpy().astype(np.float64)
        if cpi.size < 2 or not np.all(np.isfinite(cpi)) or np.any(cpi <= 0):
            continue
        logc = np.log(cpi)
        y = logc - np.mean(logc)
        label_spread.append(float(np.std(y)))
        label_cv.append(float(np.std(cpi) / (np.mean(cpi) + 1.0e-12)))

        nc = int(active.sum().item())
        for name, h in stages.items():
            hv = h[bi, active].float().cpu().numpy().astype(np.float64)
            # Probe within-window information, not shared window difficulty.
            hv = hv - hv.mean(axis=0, keepdims=True)
            stage_rows[name].append(hv)
        sv = side[bi, active].cpu().numpy().astype(np.float64)
        sv = sv - sv.mean(axis=0, keepdims=True)
        side_rows.append(sv)
        y_rows.append(y)
        window_rows.append(np.full((nc,), sample_idx, dtype=np.int64))
        core_rows.append(np.arange(nc, dtype=np.int64))
        workload = str(sample.get("meta", {}).get("workload", ""))
        workload_rows.append(np.asarray([workload] * nc, dtype=object))
        sample_idx += 1
    return sample_idx


def make_split(window_ids: np.ndarray, val_frac: float, seed: int) -> np.ndarray:
    unique = np.unique(window_ids)
    rng = np.random.RandomState(seed)
    shuffled = unique.copy()
    rng.shuffle(shuffled)
    n_val = max(1, int(round(len(shuffled) * val_frac)))
    n_val = min(n_val, max(1, len(shuffled) - 1))
    val_windows = set(int(x) for x in shuffled[:n_val])
    return np.asarray([int(w) not in val_windows for w in window_ids], dtype=bool)


def quality_label(r2: float) -> str:
    if not math.isfinite(r2):
        return "nan"
    if r2 <= 0.05:
        return "empty"
    if r2 < 0.30:
        return "weak"
    if r2 < 0.60:
        return "usable"
    return "strong"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--workload", default="W_ads_ranking_proxy")
    ap.add_argument("--n-core", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--max-cores", type=int, default=32)
    ap.add_argument("--max-samples", type=int, default=128)
    ap.add_argument("--skip", type=int, default=0)
    ap.add_argument("--batch-size", type=int, default=1)
    ap.add_argument("--device", default=None)
    ap.add_argument("--val-frac", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=20260704)
    ap.add_argument("--ridge", type=float, default=10.0)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    data = collect_hidden(args)
    train_mask = make_split(data["window_ids"], args.val_frac, args.seed)

    probes = []
    probes.append(core_id_prior(
        data["y"], train_mask, data["window_ids"], data["core_ids"],
        data["workloads"], args.n_core,
    ))
    probes.append(fit_ridge_probe(
        data["side_X"], data["y"], train_mask, data["window_ids"],
        data["core_ids"], data["workloads"], args.ridge, "side_feats",
    ))
    for name in ("query", "pre_adapter", "post_adapter"):
        if name in data["stage_X"]:
            probes.append(fit_ridge_probe(
                data["stage_X"][name], data["y"], train_mask,
                data["window_ids"], data["core_ids"], data["workloads"],
                args.ridge, f"hidden_{name}",
            ))

    result = {
        "ckpt": args.ckpt,
        "data": args.data,
        "workload": args.workload,
        "n_core": int(args.n_core),
        "max_samples": int(args.max_samples),
        "samples": int(data["samples"]),
        "core_samples": int(data["y"].size),
        "device": data["device"],
        "use_tstart": data["use_tstart"],
        "model_input_mode": data["model_input_mode"],
        "target": "log(CPI_i) - mean_core log(CPI)",
        "label_log_residual_std": summarize(data["label_log_residual_std"]),
        "label_cpi_cv": summarize(data["label_cpi_cv"]),
        "probes": probes,
    }

    print(f"[hidden-probe] ckpt={args.ckpt}")
    print(f"[hidden-probe] workload={args.workload} samples={data['samples']} target={result['target']}")
    print(f"[label] log_residual_std={result['label_log_residual_std']}")
    print("[probes]")
    for p in probes:
        print(
            f"  {p['name']:<24} "
            f"R2={p['r2_vs_collapse']:+.4f}({quality_label(float(p['r2_vs_collapse']))}) "
            f"corr={p['corr']:+.4f} "
            f"MAE={p['mae']:.4f}/{p['base_mae']:.4f} "
            f"win_corr={p.get('window_corr_mean', float('nan')):+.4f} "
            f"slow={p.get('slowest_hit_rate', float('nan')):.4f} "
            f"fast={p.get('fastest_hit_rate', float('nan')):.4f}"
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True))
        print(f"[wrote] {out}")


if __name__ == "__main__":
    main()
