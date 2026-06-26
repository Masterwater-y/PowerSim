#!/usr/bin/env python3
"""Export trace-derived ROI stats for LLMSim workloads.

The baseline is computed from trace/parquet rows rather than full gem5
stats.txt, because stats.txt may include setup/drain cycles outside the traced
ROI. Output is JSON and includes full gem5 stats only as a reference.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Dict, List, Tuple

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.roi_stats import compute_workload_roi_stats  # noqa: E402


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-root", required=True,
                    help="Root containing W*/tao_trace and stats.txt")
    ap.add_argument("--workloads", nargs="*", default=None,
                    help="Optional workload names; default scans W* dirs")
    ap.add_argument("--uarch-config", default="arch_A")
    ap.add_argument("--config-file",
                    default="/data00/yinhaolang/LLMSim/config/uarch_configs.yaml")
    ap.add_argument("--out", default=None,
                    help="Output JSON path; default: <raw-root>/roi_stats.json")
    return ap.parse_args()


def load_tick_per_cycle(config_file: str, name: str) -> int:
    with open(config_file) as f:
        all_cfg = yaml.safe_load(f)
    cfg = all_cfg["configs"][name]
    return int(cfg.get("tick_per_cycle", 333))


def resolve_workloads(raw_root: str,
                      wanted: List[str] | None) -> List[Tuple[str, str, str]]:
    if wanted:
        names = wanted
    else:
        names = sorted(
            d for d in os.listdir(raw_root)
            if d.startswith("W") and os.path.isdir(os.path.join(raw_root, d))
        )
    out = []
    for name in names:
        trace_dir = os.path.join(raw_root, name, "tao_trace")
        stats_path = os.path.join(raw_root, name, "stats.txt")
        if not os.path.isdir(trace_dir):
            print(f"[skip] {name}: missing {trace_dir}", file=sys.stderr)
            continue
        out.append((name, trace_dir, stats_path))
    return out


def main() -> None:
    args = parse_args()
    tick_per_cycle = load_tick_per_cycle(args.config_file, args.uarch_config)
    out_path = args.out or os.path.join(args.raw_root, "roi_stats.json")
    workloads = resolve_workloads(args.raw_root, args.workloads)
    if not workloads:
        raise SystemExit("[err] no workloads found")

    result: Dict[str, dict] = {}
    for name, trace_dir, stats_path in workloads:
        roi = compute_workload_roi_stats(
            trace_dir=trace_dir,
            tick_per_cycle=tick_per_cycle,
            stats_path=stats_path if os.path.isfile(stats_path) else None,
        )
        result[name] = roi
        gem5 = roi.get("gem5_full", {})
        gem5_cpi = gem5.get("cpi", float("nan"))
        rel = gem5.get("cpi_relerr_vs_roi", float("nan"))
        print(
            f"[roi] {name}: "
            f"cpi_uop={roi['cpi_uop']:.6f} cpi_macro={roi['cpi_macro']:.6f} "
            f"uops={roi['uops']:.0f} instr={roi['instr']:.0f} "
            f"cycles={roi['cycles']:.1f} "
            f"missing_label_uops={roi['missing_label_uops']} "
            f"gem5_full_cpi_macro={gem5_cpi:.6f} gem5_vs_roi={rel * 100:.2f}%",
            flush=True,
        )

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, sort_keys=True)
    print(f"[done] wrote {out_path}", flush=True)


if __name__ == "__main__":
    main()
