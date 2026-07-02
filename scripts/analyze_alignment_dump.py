#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import statistics as st
from pathlib import Path


def finite(xs):
    out = []
    for x in xs:
        try:
            v = float(x)
        except Exception:
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def mean(xs):
    xs = finite(xs)
    return st.mean(xs) if xs else float("nan")


def quantile(xs, q):
    xs = sorted(finite(xs))
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


def cv(xs):
    xs = finite(xs)
    if not xs:
        return float("nan")
    m = st.mean(xs)
    return st.pstdev(xs) / abs(m) if abs(m) > 1e-12 else float("nan")


def corr(a, b):
    a = finite(a)
    b = finite(b)
    if len(a) != len(b) or len(a) < 2:
        return float("nan")
    ma = st.mean(a)
    mb = st.mean(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 1e-24 or vb <= 1e-24:
        return float("nan")
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / math.sqrt(va * vb)


def summarize(name, xs):
    xs = finite(xs)
    if not xs:
        return f"{name}: n=0"
    return (
        f"{name}: n={len(xs)} mean={mean(xs):.6g} "
        f"p50={quantile(xs, 0.50):.6g} p90={quantile(xs, 0.90):.6g} "
        f"p95={quantile(xs, 0.95):.6g} min={min(xs):.6g} max={max(xs):.6g}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("dump", help="*.windows.jsonl from --dump-window-jsonl-dir")
    ap.add_argument("--skip-first", type=int, default=1,
                    help="skip cold-start windows in aggregate stats")
    args = ap.parse_args()

    dump = Path(args.dump)
    pred_cv = []
    label_cv = []
    pred_label_corr = []
    slow_hit = 0
    fast_hit = 0
    rank_n = 0
    agg_pred_cyc = 0.0
    agg_label_cyc = 0.0
    agg_uops = 0.0
    true_start_skew = []
    true_end_skew = []
    pred_start_skew = []
    pred_end_skew = []
    start_err = []
    end_err = []
    planner_tail_skew = []
    rows = 0

    with dump.open() as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            if int(r.get("window", 0)) < args.skip_first:
                continue
            cores = r.get("cores") or []
            if not cores:
                continue
            pred = [float(c["pred"]["cpi_uop"]) for c in cores]
            label = [float(c["label"].get("cpi_uop", float("nan"))) for c in cores]
            uops = [float(c.get("uops", 0.0)) for c in cores]
            if any(not math.isfinite(x) for x in pred + label):
                continue
            rows += 1
            pred_cv.append(cv(pred))
            label_cv.append(cv(label))
            pred_label_corr.append(corr(pred, label))
            slow_hit += int(pred.index(max(pred)) == label.index(max(label)))
            fast_hit += int(pred.index(min(pred)) == label.index(min(label)))
            rank_n += 1
            agg_pred_cyc += sum(p * u for p, u in zip(pred, uops))
            agg_label_cyc += sum(y * u for y, u in zip(label, uops))
            agg_uops += sum(uops)

            a = r.get("alignment") or {}
            true_start_skew.append(a.get("true_start_skew_cycle"))
            true_end_skew.append(a.get("true_end_skew_cycle"))
            pred_start_skew.append(a.get("pred_start_skew_cycle"))
            pred_end_skew.append(a.get("pred_end_skew_cycle"))
            start_err.append(a.get("pred_true_start_err_mean_abs"))
            end_err.append(a.get("pred_true_end_err_mean_abs"))
            planner_tail_skew.append((r.get("planner") or {}).get("tail_skew"))

    agg_pred = agg_pred_cyc / agg_uops if agg_uops > 0 else float("nan")
    agg_label = agg_label_cyc / agg_uops if agg_uops > 0 else float("nan")
    agg_rel = (
        abs(agg_pred - agg_label) / abs(agg_label)
        if math.isfinite(agg_label) and abs(agg_label) > 1e-12 else float("nan")
    )

    print(f"dump={dump}")
    print(f"windows_used={rows} skip_first={args.skip_first}")
    print(f"agg_pred_cpi_uop={agg_pred:.6g}")
    print(f"agg_label_cpi_uop={agg_label:.6g}")
    print(f"agg_relerr={agg_rel:.6g}")
    print("")
    print(summarize("pred_core_cv", pred_cv))
    print(summarize("label_core_cv", label_cv))
    print(summarize("pred_label_corr", pred_label_corr))
    if rank_n:
        print(f"slowest_core_hit_rate={slow_hit / rank_n:.6g}")
        print(f"fastest_core_hit_rate={fast_hit / rank_n:.6g}")
        print("random_baseline_8core=0.125")
    print("")
    print(summarize("pred_start_skew_cycle", pred_start_skew))
    print(summarize("pred_end_skew_cycle", pred_end_skew))
    print(summarize("true_start_skew_cycle", true_start_skew))
    print(summarize("true_end_skew_cycle", true_end_skew))
    print(summarize("pred_true_start_err_mean_abs", start_err))
    print(summarize("pred_true_end_err_mean_abs", end_err))
    print(summarize("planner_tail_skew", planner_tail_skew))


if __name__ == "__main__":
    main()
