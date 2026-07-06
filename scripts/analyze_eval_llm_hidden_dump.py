#!/usr/bin/env python3
"""Summarize LLM hidden-state diagnostics from eval window dumps."""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Iterable


PREFERRED_PRINT_KEYS = [
    "label_log_cpi_std",
    "pred_log_cpi_std",
    "pred_label_log_cpi_corr",
    "head_input_pair_cos_mean",
    "head_input_center_rel_norm",
    "head_input_effective_rank",
    "head_input_hidden_dist_label_loggap_corr",
    "query_pair_cos_mean",
    "local_fused_pair_cos_mean",
]


def finite(xs: Iterable[float]) -> list[float]:
    out = []
    for x in xs:
        try:
            v = float(x)
        except Exception:
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def quantile(xs: list[float], q: float) -> float:
    vals = sorted(finite(xs))
    if not vals:
        return float("nan")
    if len(vals) == 1:
        return vals[0]
    pos = q * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] * (hi - pos) + vals[hi] * (pos - lo)


def summarize(xs: Iterable[float]) -> dict:
    vals = finite(xs)
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "p50": quantile(vals, 0.50),
        "p90": quantile(vals, 0.90),
        "min": min(vals),
        "max": max(vals),
    }


def discover(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            out.extend(sorted(p.glob("*.windows.jsonl")))
        else:
            out.append(p)
    return out


def flatten_hidden(obj: dict) -> dict[str, float]:
    h = obj.get("llm_hidden")
    if not isinstance(h, dict):
        return {}
    flat: dict[str, float] = {}
    for k, v in h.items():
        if k == "stages":
            continue
        if isinstance(v, (int, float)):
            flat[k] = float(v)
    stages = h.get("stages")
    if isinstance(stages, dict):
        for stage_name, stage in stages.items():
            if not isinstance(stage, dict):
                continue
            for k, v in stage.items():
                if isinstance(v, (int, float)):
                    flat[f"{stage_name}_{k}"] = float(v)
    return flat


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+",
                    help="dump dirs or *.windows.jsonl files with llm_hidden")
    ap.add_argument("--out", default="",
                    help="optional JSON summary path")
    ap.add_argument("--high-label-std", type=float, default=0.30,
                    help="log-CPI std threshold for high-spread windows")
    ap.add_argument("--low-label-std", type=float, default=0.10,
                    help="log-CPI std threshold for low-spread windows")
    ap.add_argument("--high-cos", type=float, default=0.97,
                    help="head-input cosine threshold considered too similar")
    ap.add_argument("--low-center-rel", type=float, default=0.15,
                    help="head-input center_rel threshold considered weakly separated")
    ap.add_argument("--top-k", type=int, default=8,
                    help="print this many high-spread example windows")
    args = ap.parse_args()

    by_workload: dict[str, list[dict[str, float]]] = defaultdict(list)
    records: list[dict] = []
    missing = 0
    total = 0
    for path in discover(args.paths):
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open() as fh:
            for line in fh:
                if not line.startswith("{"):
                    continue
                total += 1
                obj = json.loads(line)
                flat = flatten_hidden(obj)
                if not flat:
                    missing += 1
                    continue
                wl = str(obj.get("workload") or path.name.split(".")[0])
                by_workload[wl].append(flat)
                records.append({
                    "workload": wl,
                    "window": int(obj.get("window", -1)),
                    "path": str(path),
                    "metrics": flat,
                })

    if not by_workload:
        raise SystemExit(
            f"[err] no llm_hidden records found "
            f"(windows={total}, missing={missing})"
        )

    def summarize_rows(rows: list[dict[str, float]]) -> dict:
        keys = sorted({k for row in rows for k in row})
        return {
            "windows": len(rows),
            "metrics": {
                k: summarize(row.get(k, float("nan")) for row in rows)
                for k in keys
            },
        }

    all_rows = [r["metrics"] for r in records]

    def in_high_spread(row: dict[str, float]) -> bool:
        return float(row.get("label_log_cpi_std", float("nan"))) >= args.high_label_std

    def in_low_spread(row: dict[str, float]) -> bool:
        v = float(row.get("label_log_cpi_std", float("nan")))
        return math.isfinite(v) and v <= args.low_label_std

    def in_hidden_too_similar(row: dict[str, float]) -> bool:
        cos = float(row.get("head_input_pair_cos_mean", float("nan")))
        center = float(row.get("head_input_center_rel_norm", float("nan")))
        return (
            (math.isfinite(cos) and cos >= args.high_cos)
            or (math.isfinite(center) and center <= args.low_center_rel)
        )

    bin_rows = {
        "high_label_spread": [
            r["metrics"] for r in records if in_high_spread(r["metrics"])
        ],
        "low_label_spread": [
            r["metrics"] for r in records if in_low_spread(r["metrics"])
        ],
        "high_label_spread_hidden_too_similar": [
            r["metrics"] for r in records
            if in_high_spread(r["metrics"])
            and in_hidden_too_similar(r["metrics"])
        ],
    }
    high_examples = [
        r for r in records if in_high_spread(r["metrics"])
    ]
    high_examples.sort(
        key=lambda r: (
            float(r["metrics"].get("label_log_cpi_std", float("-inf"))),
            float(r["metrics"].get("head_input_pair_cos_mean", float("-inf"))),
        ),
        reverse=True,
    )

    result = {
        "total_windows": total,
        "hidden_windows": len(all_rows),
        "missing_hidden_windows": missing,
        "thresholds": {
            "high_label_std": args.high_label_std,
            "low_label_std": args.low_label_std,
            "high_cos": args.high_cos,
            "low_center_rel": args.low_center_rel,
        },
        "by_workload": {
            wl: summarize_rows(rows)
            for wl, rows in sorted(by_workload.items())
        },
        "bins": {
            name: summarize_rows(rows) if rows else {"windows": 0, "metrics": {}}
            for name, rows in bin_rows.items()
        },
        "top_high_spread_windows": high_examples[:max(0, args.top_k)],
        "all": summarize_rows(all_rows),
    }

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, sort_keys=True))
        print(f"[wrote] {out}")

    print(
        "\t".join(["workload", "windows"] + PREFERRED_PRINT_KEYS)
    )
    for wl, summary in [
        ("ALL", result["all"]),
        *sorted(result["by_workload"].items()),
    ]:
        metrics = summary["metrics"]
        row = [wl, str(summary["windows"])]
        for key in PREFERRED_PRINT_KEYS:
            val = (metrics.get(key) or {}).get("mean", float("nan"))
            row.append("nan" if not math.isfinite(float(val)) else f"{val:.6g}")
        print("\t".join(row))

    print()
    print("\t".join(["bin", "windows"] + PREFERRED_PRINT_KEYS))
    for name, summary in result["bins"].items():
        metrics = summary["metrics"]
        row = [name, str(summary["windows"])]
        for key in PREFERRED_PRINT_KEYS:
            val = (metrics.get(key) or {}).get("mean", float("nan"))
            row.append("nan" if not math.isfinite(float(val)) else f"{val:.6g}")
        print("\t".join(row))

    if args.top_k > 0:
        print()
        example_keys = [
            "label_log_cpi_std",
            "pred_log_cpi_std",
            "pred_label_log_cpi_corr",
            "head_input_pair_cos_mean",
            "head_input_center_rel_norm",
            "head_input_effective_rank",
            "head_input_hidden_dist_label_loggap_corr",
        ]
        print("\t".join(["example", "workload", "window"] + example_keys))
        for i, rec in enumerate(high_examples[:args.top_k], start=1):
            m = rec["metrics"]
            row = [str(i), rec["workload"], str(rec["window"])]
            for key in example_keys:
                val = float(m.get(key, float("nan")))
                row.append(
                    "nan" if not math.isfinite(val) else f"{val:.6g}"
                )
            print("\t".join(row))


if __name__ == "__main__":
    main()
