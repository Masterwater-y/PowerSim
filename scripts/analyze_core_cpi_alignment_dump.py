#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def isfinite(x) -> bool:
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def finite(xs):
    out = []
    for x in xs:
        if isfinite(x):
            out.append(float(x))
    return out


def mean(xs):
    xs = finite(xs)
    return sum(xs) / len(xs) if xs else float("nan")


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


def corr(a, b):
    pairs = [
        (float(x), float(y)) for x, y in zip(a, b)
        if isfinite(x) and isfinite(y)
    ]
    if len(pairs) < 2:
        return float("nan")
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-24 or vy <= 1e-24:
        return float("nan")
    return sum((x - mx) * (y - my) for x, y in pairs) / math.sqrt(vx * vy)


def fmt(v, width=9, prec=3):
    if not isfinite(v):
        return " " * (width - 1) + "-"
    return f"{float(v):>{width}.{prec}f}"


def pct(v, width=8, prec=2):
    if not isfinite(v):
        return " " * (width - 1) + "-"
    return f"{float(v) * 100:>{width}.{prec}f}"


def rel(pred, label):
    if not isfinite(pred) or not isfinite(label) or abs(float(label)) <= 1e-12:
        return float("nan")
    return float(pred) / float(label) - 1.0


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
    ap.add_argument("dump", help="*.windows.jsonl from eval_quota_cycles")
    ap.add_argument("--skip-first", type=int, default=1)
    ap.add_argument("--out-json", default="")
    args = ap.parse_args()

    dump = Path(args.dump)
    by_core = defaultdict(lambda: {
        "n": 0,
        "uops": 0.0,
        "pred_cycles": 0.0,
        "label_cycles": 0.0,
        "pred": [],
        "label": [],
        "rel": [],
        "abs_rel": [],
        "abs_err": [],
        "start_err": [],
        "end_err": [],
        "start_abs": [],
        "end_abs": [],
        "true_span": [],
        "pred_span": [],
    })

    win_pred = []
    win_label = []
    win_rel = []
    win_abs_rel = []
    pred_cv = []
    label_cv = []
    pred_label_corr = []
    slow_hit = 0
    fast_hit = 0
    rank_n = 0
    total_uops = 0.0
    total_pred_cycles = 0.0
    total_label_cycles = 0.0
    rows = 0
    source = None
    workload = None
    align = {
        "pred_start_skew_cycle": [],
        "pred_end_skew_cycle": [],
        "true_start_skew_cycle": [],
        "true_end_skew_cycle": [],
        "pred_true_start_err_mean_abs": [],
        "pred_true_end_err_mean_abs": [],
        "planner_tail_skew": [],
    }

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
            rows += 1
            workload = workload or r.get("workload")
            source = source or r.get("planner_state_source")

            a = r.get("alignment") or {}
            for k in list(align):
                if k == "planner_tail_skew":
                    align[k].append((r.get("planner") or {}).get("tail_skew"))
                else:
                    align[k].append(a.get(k))

            core_pred = []
            core_label = []
            for c in cores:
                cid = int(c["core_id"])
                uops = float(c.get("uops", 0.0) or 0.0)
                p = float((c.get("pred") or {}).get("cpi_uop", float("nan")))
                y = float((c.get("label") or {}).get("cpi_uop", float("nan")))
                if not isfinite(p) or not isfinite(y):
                    continue
                b = by_core[cid]
                b["n"] += 1
                b["uops"] += uops
                b["pred_cycles"] += p * uops
                b["label_cycles"] += y * uops
                b["pred"].append(p)
                b["label"].append(y)
                sr = rel(p, y)
                b["rel"].append(sr)
                b["abs_rel"].append(abs(sr) if isfinite(sr) else float("nan"))
                b["abs_err"].append(abs(p - y))
                b["start_err"].append(c.get("pred_true_start_cycle_err"))
                b["end_err"].append(c.get("pred_true_end_cycle_err"))
                b["start_abs"].append(
                    abs(float(c.get("pred_true_start_cycle_err")))
                    if isfinite(c.get("pred_true_start_cycle_err")) else float("nan")
                )
                b["end_abs"].append(
                    abs(float(c.get("pred_true_end_cycle_err")))
                    if isfinite(c.get("pred_true_end_cycle_err")) else float("nan")
                )
                b["true_span"].append(c.get("true_cycle_span"))
                b["pred_span"].append(c.get("pred_cycle_delta"))
                core_pred.append(p)
                core_label.append(y)
                total_uops += uops
                total_pred_cycles += p * uops
                total_label_cycles += y * uops

            if core_pred and core_label:
                wp = float(r.get("pred_cpi_uop", float("nan")))
                wy = float(r.get("label_cpi_uop", float("nan")))
                win_pred.append(wp)
                win_label.append(wy)
                rr = rel(wp, wy)
                win_rel.append(rr)
                win_abs_rel.append(abs(rr) if isfinite(rr) else float("nan"))
                mp = sum(core_pred) / len(core_pred)
                my = sum(core_label) / len(core_label)
                pred_cv.append(
                    math.sqrt(sum((x - mp) ** 2 for x in core_pred) / len(core_pred)) / abs(mp)
                    if abs(mp) > 1e-12 else float("nan")
                )
                label_cv.append(
                    math.sqrt(sum((x - my) ** 2 for x in core_label) / len(core_label)) / abs(my)
                    if abs(my) > 1e-12 else float("nan")
                )
                pred_label_corr.append(corr(core_pred, core_label))
                slow_hit += int(core_pred.index(max(core_pred)) == core_label.index(max(core_label)))
                fast_hit += int(core_pred.index(min(core_pred)) == core_label.index(min(core_label)))
                rank_n += 1

    agg_pred = total_pred_cycles / total_uops if total_uops > 0 else float("nan")
    agg_label = total_label_cycles / total_uops if total_uops > 0 else float("nan")
    agg_rel = rel(agg_pred, agg_label)

    core_rows = []
    for cid in sorted(by_core):
        b = by_core[cid]
        cp = b["pred_cycles"] / b["uops"] if b["uops"] > 0 else float("nan")
        cy = b["label_cycles"] / b["uops"] if b["uops"] > 0 else float("nan")
        core_rows.append({
            "core": cid,
            "windows": b["n"],
            "uops": b["uops"],
            "pred_cpi": cp,
            "label_cpi": cy,
            "signed_rel": rel(cp, cy),
            "mape": mean(b["abs_rel"]),
            "mean_signed_rel": mean(b["rel"]),
            "mae": mean(b["abs_err"]),
            "corr": corr(b["pred"], b["label"]),
            "start_abs_mean": mean(b["start_abs"]),
            "start_abs_p90": quantile(b["start_abs"], 0.90),
            "end_abs_mean": mean(b["end_abs"]),
            "end_abs_p90": quantile(b["end_abs"], 0.90),
            "span_rel_bias": rel(mean(b["pred_span"]), mean(b["true_span"])),
        })

    result = {
        "dump": str(dump),
        "workload": workload,
        "planner_state_source": source,
        "windows_used": rows,
        "skip_first": args.skip_first,
        "agg_pred_cpi_uop": agg_pred,
        "agg_label_cpi_uop": agg_label,
        "agg_signed_rel": agg_rel,
        "window_mape": mean(win_abs_rel),
        "window_signed_rel_mean": mean(win_rel),
        "pred_core_cv_mean": mean(pred_cv),
        "label_core_cv_mean": mean(label_cv),
        "pred_label_corr_mean": mean(pred_label_corr),
        "slowest_hit_rate": slow_hit / rank_n if rank_n else float("nan"),
        "fastest_hit_rate": fast_hit / rank_n if rank_n else float("nan"),
        "alignment": {k: {
            "mean": mean(v),
            "p50": quantile(v, 0.50),
            "p90": quantile(v, 0.90),
            "p95": quantile(v, 0.95),
            "max": max(finite(v)) if finite(v) else float("nan"),
        } for k, v in align.items()},
        "cores": core_rows,
    }

    print(f"dump={dump}")
    print(f"workload={workload} planner_state_source={source}")
    print(f"windows_used={rows} skip_first={args.skip_first}")
    print(
        "aggregate_cpi_uop "
        f"pred={agg_pred:.6g} label={agg_label:.6g} "
        f"signed_rel={agg_rel * 100:.2f}% "
        f"window_mape={mean(win_abs_rel) * 100:.2f}%"
    )
    print(
        "within_window_core "
        f"pred_cv={mean(pred_cv):.6g} label_cv={mean(label_cv):.6g} "
        f"pred_label_corr={mean(pred_label_corr):.6g} "
        f"slow_hit={result['slowest_hit_rate']:.6g} "
        f"fast_hit={result['fastest_hit_rate']:.6g}"
    )
    print()
    print(summarize("pred_start_skew_cycle", align["pred_start_skew_cycle"]))
    print(summarize("pred_end_skew_cycle", align["pred_end_skew_cycle"]))
    print(summarize("true_start_skew_cycle", align["true_start_skew_cycle"]))
    print(summarize("true_end_skew_cycle", align["true_end_skew_cycle"]))
    print(summarize("pred_true_start_err_mean_abs", align["pred_true_start_err_mean_abs"]))
    print(summarize("pred_true_end_err_mean_abs", align["pred_true_end_err_mean_abs"]))
    print(summarize("planner_tail_skew", align["planner_tail_skew"]))
    print()
    print(
        f"{'core':>4} {'win':>5} {'uops':>10} {'pred':>9} {'label':>9} "
        f"{'bias%':>8} {'MAPE%':>8} {'corr':>7} {'MAE':>9} "
        f"{'startAbs':>10} {'endAbs':>10} {'spanBias%':>10}"
    )
    print("-" * 116)
    for r in core_rows:
        print(
            f"{r['core']:>4d} {r['windows']:>5d} {int(r['uops']):>10d} "
            f"{fmt(r['pred_cpi'])} {fmt(r['label_cpi'])} "
            f"{pct(r['signed_rel'])} {pct(r['mape'])} {fmt(r['corr'], 7, 3)} "
            f"{fmt(r['mae'])} {fmt(r['start_abs_mean'], 10, 1)} "
            f"{fmt(r['end_abs_mean'], 10, 1)} {pct(r['span_rel_bias'], 10, 2)}"
        )

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
