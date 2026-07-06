#!/usr/bin/env python3
"""Analyze per-core CPI spread from eval_quota_cycles window dumps."""
from __future__ import annotations

import argparse
import json
import math
import statistics as st
from pathlib import Path
from typing import Iterable


def finite(x: object) -> float | None:
    try:
        v = float(x)
    except Exception:
        return None
    return v if math.isfinite(v) else None


def mean(vals: Iterable[float]) -> float:
    xs = [x for x in vals if math.isfinite(float(x))]
    return st.mean(xs) if xs else float("nan")


def pstdev(vals: Iterable[float]) -> float:
    xs = [x for x in vals if math.isfinite(float(x))]
    return st.pstdev(xs) if xs else float("nan")


def quantile(vals: Iterable[float], q: float) -> float:
    xs = sorted(x for x in vals if math.isfinite(float(x)))
    if not xs:
        return float("nan")
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    return xs[lo] * (hi - pos) + xs[hi] * (pos - lo)


def pearson(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2:
        return float("nan")
    ma = mean(a)
    mb = mean(b)
    da = [x - ma for x in a]
    db = [x - mb for x in b]
    va = sum(x * x for x in da)
    vb = sum(x * x for x in db)
    if va <= 1.0e-20 or vb <= 1.0e-20:
        return float("nan")
    return sum(x * y for x, y in zip(da, db)) / math.sqrt(va * vb)


def rel_range(vals: list[float]) -> float:
    if not vals:
        return float("nan")
    m = mean(vals)
    if abs(m) <= 1.0e-12:
        return float("nan")
    return (max(vals) - min(vals)) / abs(m)


def cv(vals: list[float]) -> float:
    if not vals:
        return float("nan")
    m = mean(vals)
    if abs(m) <= 1.0e-12:
        return float("nan")
    return pstdev(vals) / abs(m)


def ratio(num: float, den: float) -> float:
    if not math.isfinite(num) or not math.isfinite(den) or abs(den) <= 1.0e-12:
        return float("nan")
    return num / den


def discover(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        path = Path(p)
        if path.is_dir():
            out.extend(sorted(path.glob("*.windows.jsonl")))
        elif path.is_file():
            out.append(path)
    return out


def parse_file(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open() as fh:
        for lineno, line in enumerate(fh, 1):
            s = line.strip()
            if not s:
                continue
            obj = json.loads(s)
            cores = obj.get("cores") or obj.get("core_rows") or []
            triples: list[tuple[int, float, float]] = []
            for idx, core in enumerate(cores):
                label = finite((core.get("label") or {}).get("cpi_uop"))
                pred = finite((core.get("pred") or {}).get("cpi_uop"))
                if label is None or pred is None:
                    continue
                triples.append((int(core.get("core_id", idx)), label, pred))
            if len(triples) < 2:
                continue
            core_ids = [x[0] for x in triples]
            labels = [x[1] for x in triples]
            preds = [x[2] for x in triples]
            label_std = pstdev(labels)
            pred_std = pstdev(preds)
            label_range = rel_range(labels)
            pred_range = rel_range(preds)
            label_cv = cv(labels)
            pred_cv = cv(preds)
            label_slowest_i = max(range(len(labels)), key=lambda i: labels[i])
            pred_slowest_i = max(range(len(preds)), key=lambda i: preds[i])
            label_fastest_i = min(range(len(labels)), key=lambda i: labels[i])
            pred_fastest_i = min(range(len(preds)), key=lambda i: preds[i])
            rows.append({
                "source": str(path),
                "lineno": lineno,
                "workload": str(obj.get("workload") or path.name.split(".")[0]),
                "window": int(obj.get("window", len(rows))),
                "n_core": len(triples),
                "label_mean": mean(labels),
                "pred_mean": mean(preds),
                "label_std": label_std,
                "pred_std": pred_std,
                "label_cv": label_cv,
                "pred_cv": pred_cv,
                "label_range_rel": label_range,
                "pred_range_rel": pred_range,
                "std_capture": ratio(pred_std, label_std),
                "cv_capture": ratio(pred_cv, label_cv),
                "range_capture": ratio(pred_range, label_range),
                "corr": pearson(preds, labels),
                "slowest_hit": 1.0 if core_ids[label_slowest_i] == core_ids[pred_slowest_i] else 0.0,
                "fastest_hit": 1.0 if core_ids[label_fastest_i] == core_ids[pred_fastest_i] else 0.0,
                "label_min": min(labels),
                "label_max": max(labels),
                "pred_min": min(preds),
                "pred_max": max(preds),
                "label_min_core": core_ids[label_fastest_i],
                "label_max_core": core_ids[label_slowest_i],
                "pred_min_core": core_ids[pred_fastest_i],
                "pred_max_core": core_ids[pred_slowest_i],
                "window_rel_err": finite(obj.get("cpi_uop_rel_err")),
            })
    return rows


def summarize(rows: list[dict], high_q: float, flat_range: float) -> dict:
    if not rows:
        return {}
    high_thr = quantile([r["label_range_rel"] for r in rows], high_q)
    high_rows = [r for r in rows if r["label_range_rel"] >= high_thr]

    def m(key: str, group: list[dict] = rows) -> float:
        return mean(r.get(key, float("nan")) for r in group)

    def q(key: str, prob: float, group: list[dict] = rows) -> float:
        return quantile((r.get(key, float("nan")) for r in group), prob)

    def rate(pred, group: list[dict] = rows) -> float:
        vals = [1.0 if pred(r) else 0.0 for r in group]
        return mean(vals)

    return {
        "n": len(rows),
        "high_n": len(high_rows),
        "label_range_p50": q("label_range_rel", 0.50),
        "pred_range_p50": q("pred_range_rel", 0.50),
        "range_capture_p50": q("range_capture", 0.50),
        "label_range_p90": q("label_range_rel", 0.90),
        "pred_range_p90": q("pred_range_rel", 0.90),
        "label_cv_mean": m("label_cv"),
        "pred_cv_mean": m("pred_cv"),
        "cv_capture_mean": m("cv_capture"),
        "corr_mean": m("corr"),
        "slowest_hit_rate": m("slowest_hit"),
        "fastest_hit_rate": m("fastest_hit"),
        "flat_pred_rate": rate(lambda r: r["pred_range_rel"] <= flat_range),
        "under_half_rate": rate(lambda r: r["pred_range_rel"] < 0.5 * r["label_range_rel"]),
        "window_rel_err_mean": m("window_rel_err"),
        "high_label_range_mean": m("label_range_rel", high_rows),
        "high_pred_range_mean": m("pred_range_rel", high_rows),
        "high_capture_p50": q("range_capture", 0.50, high_rows),
        "high_corr_mean": m("corr", high_rows),
        "high_slowest_hit_rate": m("slowest_hit", high_rows),
        "high_under_half_rate": rate(
            lambda r: r["pred_range_rel"] < 0.5 * r["label_range_rel"],
            high_rows,
        ),
        "high_flat_pred_rate": rate(
            lambda r: r["pred_range_rel"] <= flat_range,
            high_rows,
        ),
        "high_threshold": high_thr,
    }


def fmt(x: object, pct: bool = False) -> str:
    v = finite(x)
    if v is None:
        return "-"
    if pct:
        return f"{100.0 * v:.1f}%"
    return f"{v:.3f}"


def print_table(title: str, summaries: list[tuple[str, dict]]) -> None:
    print(title)
    header = (
        "group n high label_rng50 pred_rng50 cap50 label_rng90 pred_rng90 "
        "corr slow_hit flat under_half high_cap50 high_slow_hit high_under"
    )
    print(header)
    for name, s in summaries:
        print(
            f"{name} {s['n']} {s['high_n']} "
            f"{fmt(s['label_range_p50'])} {fmt(s['pred_range_p50'])} "
            f"{fmt(s['range_capture_p50'], pct=True)} "
            f"{fmt(s['label_range_p90'])} {fmt(s['pred_range_p90'])} "
            f"{fmt(s['corr_mean'])} {fmt(s['slowest_hit_rate'], pct=True)} "
            f"{fmt(s['flat_pred_rate'], pct=True)} "
            f"{fmt(s['under_half_rate'], pct=True)} "
            f"{fmt(s['high_capture_p50'], pct=True)} "
            f"{fmt(s['high_slowest_hit_rate'], pct=True)} "
            f"{fmt(s['high_under_half_rate'], pct=True)}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="dump dirs or *.windows.jsonl files")
    ap.add_argument("--high-q", type=float, default=0.80)
    ap.add_argument("--flat-range", type=float, default=0.10)
    ap.add_argument("--top-n", type=int, default=12)
    ap.add_argument("--json-out", default="")
    args = ap.parse_args()

    files = discover(args.paths)
    if not files:
        raise SystemExit("[err] no dump files found")

    rows: list[dict] = []
    for fp in files:
        rows.extend(parse_file(fp))
    if not rows:
        raise SystemExit("[err] no valid windows found")

    by_w: dict[str, list[dict]] = {}
    for r in rows:
        by_w.setdefault(r["workload"], []).append(r)

    summaries = [("ALL", summarize(rows, args.high_q, args.flat_range))]
    for w in sorted(by_w):
        summaries.append((w, summarize(by_w[w], args.high_q, args.flat_range)))
    print_table(
        f"core CPI spread summary (high_q={args.high_q}, flat_range={args.flat_range})",
        summaries,
    )

    worst = sorted(
        rows,
        key=lambda r: (
            -float(r["label_range_rel"]),
            float(r["range_capture"]) if math.isfinite(float(r["range_capture"])) else 1e9,
        ),
    )[:args.top_n]
    print()
    print("worst high-label-spread windows")
    print("workload win label_rng pred_rng cap corr label[min,max] pred[min,max] slow_hit")
    for r in worst:
        print(
            f"{r['workload']} {r['window']} "
            f"{fmt(r['label_range_rel'])} {fmt(r['pred_range_rel'])} "
            f"{fmt(r['range_capture'], pct=True)} {fmt(r['corr'])} "
            f"c{r['label_min_core']}:{r['label_min']:.3f},c{r['label_max_core']}:{r['label_max']:.3f} "
            f"c{r['pred_min_core']}:{r['pred_min']:.3f},c{r['pred_max_core']}:{r['pred_max']:.3f} "
            f"{fmt(r['slowest_hit'], pct=True)}"
        )

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({
            "files": [str(f) for f in files],
            "summary": {name: s for name, s in summaries},
            "rows": rows,
        }, indent=2, sort_keys=True))
        print(f"[wrote] {out}")


if __name__ == "__main__":
    main()
