#!/usr/bin/env python3
"""Diagnose CPI residual outliers from eval_quota_cycles window dumps.

This script intentionally separates:
  - deployment-visible functional features from summary_avg
  - diagnostic-only hidden labels from hidden_avg

It prints decile residual tables, hidden-field correlations, and a lightweight
OOD kNN check against training windows.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from pathlib import Path
from typing import Dict, Iterable, List, Tuple


FEATURE_KEYS = [
    "mem_ratio",
    "load_frac_mem",
    "store_frac_mem",
    "distinct_lines",
    "distinct_pages",
]

HIDDEN_KEYS = [
    "path_l1_miss_frac_mem",
    "path_llc_miss_frac_mem",
    "path_l1_miss_frac_load",
    "path_llc_miss_frac_load",
    "d_mshr_depth_avg",
    "commit_issue_gap_avg_tick",
    "complete_issue_gap_avg_tick",
    "ready_issue_gap_avg_tick",
    "producer_dist_avg",
    "producer_dist_max",
    "branch_mispred_frac",
]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-dir", required=True,
                    help="Directory containing *.windows.jsonl from eval_quota_cycles")
    ap.add_argument("--train-windows",
                    default="/data00/yinhaolang/LLMSim/data/windows_v5_tq32k/windows.jsonl",
                    help="Training windows JSONL used for OOD nearest-neighbor baseline")
    ap.add_argument("--workload", action="append", default=[],
                    help="Only analyze these workloads; default: all dump files")
    ap.add_argument("--train-max", type=int, default=20000,
                    help="Max train windows for OOD baseline; 0 means all")
    ap.add_argument("--knn", type=int, default=5)
    ap.add_argument("--top-bad", type=int, default=12)
    ap.add_argument("--out-json", default="",
                    help="Optional path for machine-readable report")
    return ap.parse_args()


def load_jsonl(path: str) -> Iterable[dict]:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def median(xs: List[float]) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    mid = len(ys) // 2
    if len(ys) % 2:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def pearson(xs: List[float], ys: List[float]) -> float:
    pairs = [
        (float(x), float(y)) for x, y in zip(xs, ys)
        if math.isfinite(float(x)) and math.isfinite(float(y))
    ]
    if len(pairs) < 3:
        return float("nan")
    xbar = mean([p[0] for p in pairs])
    ybar = mean([p[1] for p in pairs])
    num = sum((x - xbar) * (y - ybar) for x, y in pairs)
    dx = math.sqrt(sum((x - xbar) ** 2 for x, _ in pairs))
    dy = math.sqrt(sum((y - ybar) ** 2 for _, y in pairs))
    if dx == 0.0 or dy == 0.0:
        return float("nan")
    return num / (dx * dy)


def avg_core_summaries(summaries: List[dict]) -> dict:
    if not summaries:
        return {}
    out = {}
    for k in FEATURE_KEYS:
        out[k] = mean([float(s.get(k, 0.0) or 0.0) for s in summaries])
    n_rd = max((len(s.get("rd_hist") or []) for s in summaries), default=0)
    n_st = max((len(s.get("stride_hist") or []) for s in summaries), default=0)
    out["rd_hist"] = [
        mean([float(((s.get("rd_hist") or []) + [0.0] * n_rd)[i]) for s in summaries])
        for i in range(n_rd)
    ]
    out["stride_hist"] = [
        mean([float(((s.get("stride_hist") or []) + [0.0] * n_st)[i]) for s in summaries])
        for i in range(n_st)
    ]
    return out


def feature_vector(summary: dict) -> List[float]:
    v = [float(summary.get(k, 0.0) or 0.0) for k in FEATURE_KEYS]
    rd = summary.get("rd_hist") or []
    st = summary.get("stride_hist") or []
    v.extend(float(x or 0.0) for x in rd)
    v.extend(float(x or 0.0) for x in st)
    return v


def pad_vectors(vs: List[List[float]], width: int | None = None) -> List[List[float]]:
    if width is None:
        width = max((len(v) for v in vs), default=0)
    return [(v + [0.0] * width)[:width] for v in vs]


def standardize_fit(vs: List[List[float]]) -> Tuple[List[float], List[float]]:
    if not vs:
        return [], []
    width = len(vs[0])
    mu = []
    sig = []
    for i in range(width):
        col = [v[i] for v in vs]
        m = mean(col)
        var = mean([(x - m) ** 2 for x in col])
        s = math.sqrt(var)
        mu.append(m)
        sig.append(s if s > 1e-9 else 1.0)
    return mu, sig


def standardize(v: List[float], mu: List[float], sig: List[float]) -> List[float]:
    return [(x - m) / s for x, m, s in zip(v, mu, sig)]


def l2(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def load_train_vectors(path: str, max_rows: int) -> Tuple[List[List[float]], List[str]]:
    vectors = []
    workloads = []
    for i, obj in enumerate(load_jsonl(path)):
        if max_rows and i >= max_rows:
            break
        summary = avg_core_summaries(obj.get("core_summary") or [])
        if not summary:
            continue
        vectors.append(feature_vector(summary))
        workloads.append(str(obj.get("workload", "")))
    return vectors, workloads


def load_dump_windows(dump_dir: str, workloads: List[str]) -> Dict[str, List[dict]]:
    wanted = set(workloads)
    out: Dict[str, List[dict]] = {}
    for path in sorted(glob.glob(os.path.join(dump_dir, "*.windows.jsonl"))):
        wl = Path(path).name.replace(".windows.jsonl", "")
        if wanted and wl not in wanted:
            continue
        rows = list(load_jsonl(path))
        if rows:
            out[wl] = rows
    return out


def decile_report(rows: List[dict]) -> List[dict]:
    if not rows:
        return []
    ordered = sorted(rows, key=lambda r: float(r.get("progress_after", 0.0) or 0.0))
    out = []
    n = len(ordered)
    for d in range(10):
        lo = int(n * d / 10)
        hi = int(n * (d + 1) / 10)
        chunk = ordered[lo:hi]
        if not chunk:
            continue
        residuals = [float(r.get("cpi_residual", 0.0) or 0.0) for r in chunk]
        rels = [float(r.get("cpi_rel_err", 0.0) or 0.0) for r in chunk]
        out.append({
            "decile": d,
            "n": len(chunk),
            "progress_lo": float(chunk[0].get("progress_after", 0.0) or 0.0),
            "progress_hi": float(chunk[-1].get("progress_after", 0.0) or 0.0),
            "pred_cpi_mean": mean([float(r.get("pred_cpi", 0.0) or 0.0) for r in chunk]),
            "label_cpi_mean": mean([float(r.get("label_cpi", 0.0) or 0.0) for r in chunk]),
            "residual_mean": mean(residuals),
            "abs_rel_err_mean": mean(rels),
            "mem_ratio_mean": mean([
                float((r.get("summary_avg") or {}).get("mem_ratio", 0.0) or 0.0)
                for r in chunk
            ]),
            "llc_miss_mem_mean": mean([
                float((r.get("hidden_avg") or {}).get("path_llc_miss_frac_mem", 0.0) or 0.0)
                for r in chunk
            ]),
            "mshr_avg_mean": mean([
                float((r.get("hidden_avg") or {}).get("d_mshr_depth_avg", 0.0) or 0.0)
                for r in chunk
            ]),
        })
    return out


def correlation_report(rows: List[dict]) -> List[dict]:
    residual = [float(r.get("cpi_residual", 0.0) or 0.0) for r in rows]
    candidates = []
    for k in FEATURE_KEYS:
        candidates.append(("summary." + k, [
            float((r.get("summary_avg") or {}).get(k, 0.0) or 0.0)
            for r in rows
        ]))
    for i in range(9):
        candidates.append((f"summary.rd_hist[{i}]", [
            float((((r.get("summary_avg") or {}).get("rd_hist") or []) + [0.0] * 9)[i])
            for r in rows
        ]))
    for i in range(10):
        candidates.append((f"summary.stride_hist[{i}]", [
            float((((r.get("summary_avg") or {}).get("stride_hist") or []) + [0.0] * 10)[i])
            for r in rows
        ]))
    for k in HIDDEN_KEYS:
        candidates.append(("hidden." + k, [
            float((r.get("hidden_avg") or {}).get(k, 0.0) or 0.0)
            for r in rows
        ]))
    out = []
    for name, vals in candidates:
        c = pearson(residual, vals)
        if math.isfinite(c):
            out.append({"field": name, "corr": c})
    out.sort(key=lambda x: abs(x["corr"]), reverse=True)
    return out


def ood_report(rows: List[dict], train_z: List[List[float]],
               train_wl: List[str], mu: List[float], sig: List[float],
               knn: int) -> dict:
    if not train_z:
        return {}
    width = len(mu)
    distances = []
    nearest_wl_count: Dict[str, int] = {}
    for r in rows:
        v = pad_vectors([feature_vector(r.get("summary_avg") or {})], width)[0]
        z = standardize(v, mu, sig)
        ds = sorted((l2(z, tv), i) for i, tv in enumerate(train_z))[:knn]
        dmean = mean([d for d, _ in ds])
        distances.append(dmean)
        for _, idx in ds:
            wl = train_wl[idx]
            nearest_wl_count[wl] = nearest_wl_count.get(wl, 0) + 1
    top = sorted(nearest_wl_count.items(), key=lambda kv: kv[1], reverse=True)[:8]
    return {
        "knn": knn,
        "distance_mean": mean(distances),
        "distance_p50": median(distances),
        "distance_p90": sorted(distances)[int(0.9 * (len(distances) - 1))] if distances else float("nan"),
        "nearest_workloads": [{"workload": k, "hits": v} for k, v in top],
    }


def workload_summary(rows: List[dict], train_z: List[List[float]],
                     train_wl: List[str], mu: List[float], sig: List[float],
                     knn: int, top_bad: int) -> dict:
    rels = [float(r.get("cpi_rel_err", 0.0) or 0.0) for r in rows]
    residuals = [float(r.get("cpi_residual", 0.0) or 0.0) for r in rows]
    bad = sorted(rows, key=lambda r: float(r.get("cpi_rel_err", 0.0) or 0.0),
                 reverse=True)[:top_bad]
    return {
        "windows": len(rows),
        "pred_cpi_mean": mean([float(r.get("pred_cpi", 0.0) or 0.0) for r in rows]),
        "label_cpi_mean": mean([float(r.get("label_cpi", 0.0) or 0.0) for r in rows]),
        "residual_mean": mean(residuals),
        "residual_p50": median(residuals),
        "abs_rel_err_mean": mean(rels),
        "abs_rel_err_p50": median(rels),
        "deciles": decile_report(rows),
        "top_correlations": correlation_report(rows)[:15],
        "ood": ood_report(rows, train_z, train_wl, mu, sig, knn),
        "top_bad_windows": [
            {
                "window": int(r.get("window", -1)),
                "progress_after": float(r.get("progress_after", 0.0) or 0.0),
                "pred_cpi": float(r.get("pred_cpi", 0.0) or 0.0),
                "label_cpi": float(r.get("label_cpi", 0.0) or 0.0),
                "residual": float(r.get("cpi_residual", 0.0) or 0.0),
                "rel_err": float(r.get("cpi_rel_err", 0.0) or 0.0),
                "summary_avg": r.get("summary_avg") or {},
                "hidden_avg": r.get("hidden_avg") or {},
            }
            for r in bad
        ],
    }


def print_report(report: dict) -> None:
    for wl, r in report["workloads"].items():
        print(f"\n## {wl}")
        print(
            f"windows={r['windows']} pred={r['pred_cpi_mean']:.4f} "
            f"label={r['label_cpi_mean']:.4f} residual={r['residual_mean']:.4f} "
            f"rel_err_mean={r['abs_rel_err_mean'] * 100:.2f}% "
            f"rel_err_p50={r['abs_rel_err_p50'] * 100:.2f}%"
        )
        ood = r.get("ood") or {}
        if ood:
            nearest = ", ".join(
                f"{x['workload']}:{x['hits']}" for x in ood.get("nearest_workloads", [])
            )
            print(
                f"OOD knn{ood['knn']}: dist_mean={ood['distance_mean']:.3f} "
                f"p50={ood['distance_p50']:.3f} p90={ood['distance_p90']:.3f} "
                f"nearest=[{nearest}]"
            )
        print("Deciles:")
        print("  d  prog%        pred   label  resid  rel%   mem%  llc%  mshr")
        for d in r["deciles"]:
            print(
                f"  {d['decile']:1d}  "
                f"{d['progress_lo'] * 100:5.1f}-{d['progress_hi'] * 100:5.1f} "
                f"{d['pred_cpi_mean']:7.3f} {d['label_cpi_mean']:7.3f} "
                f"{d['residual_mean']:7.3f} {d['abs_rel_err_mean'] * 100:6.1f} "
                f"{d['mem_ratio_mean'] * 100:5.1f} "
                f"{d['llc_miss_mem_mean'] * 100:5.1f} "
                f"{d['mshr_avg_mean']:5.2f}"
            )
        print("Top residual correlations:")
        for c in r["top_correlations"][:8]:
            print(f"  {c['field']:<36} corr={c['corr']:+.3f}")
        print("Worst windows:")
        for w in r["top_bad_windows"][:5]:
            print(
                f"  win={w['window']} prog={w['progress_after'] * 100:.1f}% "
                f"pred={w['pred_cpi']:.3f} label={w['label_cpi']:.3f} "
                f"resid={w['residual']:+.3f} rel={w['rel_err'] * 100:.1f}%"
            )


def main() -> None:
    args = parse_args()
    dump = load_dump_windows(args.dump_dir, args.workload)
    if not dump:
        raise SystemExit(f"no *.windows.jsonl found in {args.dump_dir}")

    train_vecs, train_wl = load_train_vectors(args.train_windows, args.train_max)
    dump_vecs = [
        feature_vector(r.get("summary_avg") or {})
        for rows in dump.values() for r in rows
    ]
    width = max([len(v) for v in train_vecs + dump_vecs] or [0])
    train_vecs = pad_vectors(train_vecs, width)
    mu, sig = standardize_fit(train_vecs)
    train_z = [standardize(v, mu, sig) for v in train_vecs]

    report = {
        "dump_dir": args.dump_dir,
        "train_windows": args.train_windows,
        "train_vectors": len(train_z),
        "workloads": {},
    }
    for wl, rows in dump.items():
        report["workloads"][wl] = workload_summary(
            rows, train_z, train_wl, mu, sig, args.knn, args.top_bad)

    print_report(report)
    if args.out_json:
        os.makedirs(os.path.dirname(args.out_json) or ".", exist_ok=True)
        with open(args.out_json, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\n[wrote] {args.out_json}")


if __name__ == "__main__":
    main()
