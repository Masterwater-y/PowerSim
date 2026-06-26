#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict


FEATURE_KEYS = [
    "seen_line_rate_8k",
    "seen_line_rate_64k",
    "recent_ws_size_64k",
    "branch_density",
    "indirect_branch_rate",
    "pc_entropy",
    "basic_block_len_mean",
    "branch_target_reuse_rate",
    "short_reg_raw_rate",
    "reg_raw_distance_mean",
    "load_use_short_rate",
    "mem_ratio",
    "load_frac_mem",
    "store_frac_mem",
    "distinct_lines",
]


def quantile(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round(p * (len(ys) - 1)))))
    return ys[i]


def summarize(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    m = sum(xs) / len(xs)
    var = sum((x - m) * (x - m) for x in xs) / len(xs)
    return {
        "n": len(xs),
        "mean": m,
        "std": math.sqrt(var),
        "p05": quantile(xs, 0.05),
        "p50": quantile(xs, 0.50),
        "p95": quantile(xs, 0.95),
        "min": min(xs),
        "max": max(xs),
        "nonzero_rate": sum(1 for x in xs if abs(x) > 1e-12) / len(xs),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/windows_v6_tq32k/windows.jsonl")
    ap.add_argument("--out", default="logs/v6_tq32k_distribution_report.json")
    args = ap.parse_args()

    label_keys = None
    vals = defaultdict(lambda: defaultdict(list))
    counts = defaultdict(int)

    with open(args.data) as f:
        for line in f:
            if not line.lstrip().startswith("{"):
                continue
            rec = json.loads(line)
            wl = rec.get("workload", "unknown")
            counts[wl] += 1
            if label_keys is None:
                label_keys = rec["label_keys"]
            for core_label in rec["label"]:
                for key, value in zip(label_keys, core_label):
                    v = float(value)
                    vals[wl]["label." + key].append(v)
                    vals["__all__"]["label." + key].append(v)
            for summary in rec.get("core_summary") or []:
                for key in FEATURE_KEYS:
                    if key in summary:
                        v = float(summary.get(key) or 0.0)
                        vals[wl]["feat." + key].append(v)
                        vals["__all__"]["feat." + key].append(v)

    report = {
        "data": args.data,
        "samples_by_workload": dict(sorted(counts.items())),
        "label_keys": label_keys,
        "workloads": {},
    }
    for wl in sorted(vals):
        report["workloads"][wl] = {
            key: summarize(values)
            for key, values in sorted(vals[wl].items())
        }

    def mean_value(wl: str, key: str) -> float:
        return report["workloads"][wl][key]["mean"]

    checks = {}
    comparisons = [
        (
            "W_chase_dram",
            "W_branch_storm",
            [
                "feat.seen_line_rate_64k",
                "feat.recent_ws_size_64k",
                "label.mr_llc",
                "label.mshr_avg",
            ],
        ),
        (
            "W_branch_storm",
            "W_compute_int",
            [
                "feat.branch_density",
                "feat.pc_entropy",
                "label.branch_mispred_frac",
            ],
        ),
        (
            "W_int_div",
            "W_compute_int",
            ["label.cpi", "label.mpki_br"],
        ),
        (
            "W_mlp_light",
            "W_compute_int",
            [
                "feat.short_reg_raw_rate",
                "feat.reg_raw_distance_mean",
                "feat.load_use_short_rate",
                "label.cpi",
            ],
        ),
    ]
    for a, b, keys in comparisons:
        if a not in report["workloads"] or b not in report["workloads"]:
            continue
        checks[f"{a}_minus_{b}"] = {
            key: mean_value(a, key) - mean_value(b, key)
            for key in keys
            if key in report["workloads"][a] and key in report["workloads"][b]
        }
    report["discriminative_checks"] = checks

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    print(f"[wrote] {args.out}")
    print(f"[samples] {sum(counts.values())} {dict(sorted(counts.items()))}")
    print(f"[label_keys] {label_keys}")
    shown = [
        "__all__",
        "W_chase_dram",
        "W_branch_storm",
        "W_int_div",
        "W_mlp_light",
        "W_phased_mix",
    ]
    keys = [
        "label.branch_mispred_frac",
        "label.mr_llc",
        "label.mshr_avg",
        "feat.seen_line_rate_64k",
        "feat.recent_ws_size_64k",
        "feat.branch_density",
        "feat.pc_entropy",
        "feat.short_reg_raw_rate",
        "feat.load_use_short_rate",
    ]
    for wl in shown:
        if wl not in report["workloads"]:
            continue
        print(f"\n## {wl}")
        for key in keys:
            if key not in report["workloads"][wl]:
                continue
            s = report["workloads"][wl][key]
            print(
                f"{key:32s} mean={s['mean']:.4g} "
                f"p50={s['p50']:.4g} p95={s['p95']:.4g} "
                f"nz={s['nonzero_rate']:.3f}"
            )
    print("\n[checks]")
    for name, obj in checks.items():
        rounded = {key: round(value, 4) for key, value in obj.items()}
        print(name, rounded)


if __name__ == "__main__":
    main()
