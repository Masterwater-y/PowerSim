#!/usr/bin/env python3
"""Compare functional warm-state proxies from eval window dumps.

The dump contains cursor_start/cursor_end per core, but not addresses. This
script uses those cursors to slice the original functional trace and computes
history-only warm-state proxies:

  seen_line_rate_K = fraction of current-window memory references whose
                     cache line appeared in the previous K memory references
                     on the same core, including earlier refs in this window
  new_line_rate_K  = 1 - seen_line_rate_K

No gem5 hit/miss label or shared_system output is used.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, deque
from pathlib import Path
from typing import Dict, Iterable, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.roi_stats import load_workload_rows  # noqa: E402


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-dir", required=True,
                    help="Directory containing <workload>.windows.jsonl")
    ap.add_argument("--raw-root", required=True,
                    help="Raw root containing <workload>/tao_trace")
    ap.add_argument("--workload", action="append", required=True,
                    help="Workload to analyze; pass twice to compare")
    ap.add_argument("--history-k", default="8192,65536,524288",
                    help="Comma-separated per-core memory-reference history sizes")
    ap.add_argument("--out-json", default="",
                    help="Optional machine-readable report path")
    return ap.parse_args()


def load_jsonl(path: str) -> Iterable[dict]:
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def mean(xs: List[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def quantile(xs: List[float], q: float) -> float:
    if not xs:
        return float("nan")
    ys = sorted(xs)
    idx = min(len(ys) - 1, max(0, int(round(q * (len(ys) - 1)))))
    return ys[idx]


def cacheline(rec: dict) -> int | None:
    for key in ("cacheline_paddr", "cacheline_addr"):
        v = int(rec.get(key, 0) or 0)
        if v:
            return v
    paddr = int(rec.get("paddr", 0) or 0)
    if paddr:
        return paddr & ~63
    vaddr = int(rec.get("vaddr", 0) or 0)
    if vaddr:
        return vaddr & ~63
    return None


def is_mem(rec: dict) -> bool:
    return bool(int(rec.get("is_load", 0) or 0)
                or int(rec.get("is_store", 0) or 0)
                or int(rec.get("is_atomic", 0) or 0))


class RecentLineTracker:
    def __init__(self, capacity: int):
        self.capacity = int(capacity)
        self.q: deque[int] = deque()
        self.counts: Counter[int] = Counter()

    def seen(self, line: int) -> bool:
        return self.counts.get(line, 0) > 0

    def add(self, line: int) -> None:
        if self.capacity <= 0:
            return
        self.q.append(line)
        self.counts[line] += 1
        while len(self.q) > self.capacity:
            old = self.q.popleft()
            self.counts[old] -= 1
            if self.counts[old] <= 0:
                del self.counts[old]

    def working_set_size(self) -> int:
        return len(self.counts)


def analyze_workload(raw_root: str, dump_dir: str, workload: str,
                     hist_ks: List[int]) -> dict:
    dump_path = os.path.join(dump_dir, f"{workload}.windows.jsonl")
    if not os.path.isfile(dump_path):
        raise FileNotFoundError(dump_path)
    trace_dir = os.path.join(raw_root, workload, "tao_trace")
    if not os.path.isdir(trace_dir):
        raise FileNotFoundError(trace_dir)

    merged = load_workload_rows(trace_dir)
    trackers = {
        int(c): {k: RecentLineTracker(k) for k in hist_ks}
        for c in merged.keys()
    }
    cumulative_seen = {int(c): set() for c in merged.keys()}

    windows = []
    for obj in load_jsonl(dump_path):
        per_k_seen = {k: 0 for k in hist_ks}
        per_k_ws = {k: [] for k in hist_ks}
        cumulative_seen_count = 0
        mem_ops = 0
        load_ops = 0
        store_ops = 0

        for core_obj in obj.get("cores", []):
            c = int(core_obj["core_id"])
            start = int(core_obj["cursor_start"])
            end = int(core_obj["cursor_end"])
            seq = merged[c]
            if start < 0 or end > len(seq) or start > end:
                raise ValueError(
                    f"{workload} window={obj.get('window')} core={c}: "
                    f"bad cursor range {start}:{end} len={len(seq)}")
            for rec in seq[start:end]:
                if not is_mem(rec):
                    continue
                line = cacheline(rec)
                if line is None:
                    continue
                mem_ops += 1
                load_ops += int(rec.get("is_load", 0) or 0)
                store_ops += int(rec.get("is_store", 0) or 0)
                if line in cumulative_seen[c]:
                    cumulative_seen_count += 1
                for k in hist_ks:
                    if trackers[c][k].seen(line):
                        per_k_seen[k] += 1
                cumulative_seen[c].add(line)
                for k in hist_ks:
                    trackers[c][k].add(line)
            for k in hist_ks:
                per_k_ws[k].append(trackers[c][k].working_set_size())

        row = {
            "workload": workload,
            "window": int(obj.get("window", len(windows))),
            "progress_after": float(obj.get("progress_after", 0.0) or 0.0),
            "pred_cpi": float(obj.get("pred_cpi", float("nan"))),
            "label_cpi": float(obj.get("label_cpi", float("nan"))),
            "cpi_residual": float(obj.get("cpi_residual", float("nan"))),
            "mem_ops": mem_ops,
            "load_frac_mem": load_ops / max(mem_ops, 1),
            "store_frac_mem": store_ops / max(mem_ops, 1),
            "cumulative_seen_line_rate": cumulative_seen_count / max(mem_ops, 1),
        }
        for k in hist_ks:
            seen = per_k_seen[k] / max(mem_ops, 1)
            row[f"seen_line_rate_{k}"] = seen
            row[f"new_line_rate_{k}"] = 1.0 - seen
            row[f"recent_ws_size_{k}"] = mean(per_k_ws[k])
        windows.append(row)

    return {"workload": workload, "dump_path": dump_path, "windows": windows}


def deciles(rows: List[dict], hist_ks: List[int]) -> List[dict]:
    if not rows:
        return []
    rows = sorted(rows, key=lambda r: r["progress_after"])
    out = []
    n = len(rows)
    for d in range(10):
        chunk = rows[int(n * d / 10):int(n * (d + 1) / 10)]
        if not chunk:
            continue
        item = {
            "decile": d,
            "n": len(chunk),
            "progress_lo": chunk[0]["progress_after"],
            "progress_hi": chunk[-1]["progress_after"],
            "mem_ops_mean": mean([r["mem_ops"] for r in chunk]),
            "label_cpi_mean": mean([r["label_cpi"] for r in chunk]),
            "pred_cpi_mean": mean([r["pred_cpi"] for r in chunk]),
            "residual_mean": mean([r["cpi_residual"] for r in chunk]),
            "load_frac_mem_mean": mean([r["load_frac_mem"] for r in chunk]),
            "cumulative_seen_line_rate_mean": mean([
                r["cumulative_seen_line_rate"] for r in chunk
            ]),
        }
        for k in hist_ks:
            item[f"seen_line_rate_{k}_mean"] = mean([
                r[f"seen_line_rate_{k}"] for r in chunk
            ])
            item[f"new_line_rate_{k}_mean"] = mean([
                r[f"new_line_rate_{k}"] for r in chunk
            ])
            item[f"recent_ws_size_{k}_mean"] = mean([
                r[f"recent_ws_size_{k}"] for r in chunk
            ])
        out.append(item)
    return out


def summary(rows: List[dict], hist_ks: List[int]) -> dict:
    out = {
        "windows": len(rows),
        "label_cpi_mean": mean([r["label_cpi"] for r in rows]),
        "pred_cpi_mean": mean([r["pred_cpi"] for r in rows]),
        "residual_mean": mean([r["cpi_residual"] for r in rows]),
        "mem_ops_mean": mean([r["mem_ops"] for r in rows]),
        "load_frac_mem_mean": mean([r["load_frac_mem"] for r in rows]),
        "cumulative_seen_line_rate_mean": mean([
            r["cumulative_seen_line_rate"] for r in rows
        ]),
    }
    for k in hist_ks:
        vals = [r[f"seen_line_rate_{k}"] for r in rows]
        out[f"seen_line_rate_{k}_mean"] = mean(vals)
        out[f"seen_line_rate_{k}_p50"] = quantile(vals, 0.5)
        out[f"seen_line_rate_{k}_p90"] = quantile(vals, 0.9)
        out[f"new_line_rate_{k}_mean"] = mean([
            r[f"new_line_rate_{k}"] for r in rows
        ])
        out[f"recent_ws_size_{k}_mean"] = mean([
            r[f"recent_ws_size_{k}"] for r in rows
        ])
    return out


def print_workload_report(rep: dict, hist_ks: List[int]) -> None:
    wl = rep["workload"]
    rows = rep["windows"]
    sm = summary(rows, hist_ks)
    print(f"\n## {wl}")
    print(
        f"windows={sm['windows']} pred={sm['pred_cpi_mean']:.4f} "
        f"label={sm['label_cpi_mean']:.4f} residual={sm['residual_mean']:+.4f} "
        f"mem_ops/window={sm['mem_ops_mean']:.1f} "
        f"load_frac={sm['load_frac_mem_mean'] * 100:.1f}% "
        f"cum_seen={sm['cumulative_seen_line_rate_mean'] * 100:.1f}%"
    )
    for k in hist_ks:
        print(
            f"  K={k:<7} seen_mean={sm[f'seen_line_rate_{k}_mean'] * 100:6.1f}% "
            f"seen_p50={sm[f'seen_line_rate_{k}_p50'] * 100:6.1f}% "
            f"seen_p90={sm[f'seen_line_rate_{k}_p90'] * 100:6.1f}% "
            f"new_mean={sm[f'new_line_rate_{k}_mean'] * 100:6.1f}% "
            f"ws={sm[f'recent_ws_size_{k}_mean']:.1f}"
        )

    main_k = hist_ks[min(1, len(hist_ks) - 1)]
    print("Deciles:")
    print(
        f"  d  progress%     pred  label  resid  mem_ops load% "
        f"seen{main_k//1024}k% new{main_k//1024}k% cum_seen%"
    )
    for d in deciles(rows, hist_ks):
        print(
            f"  {d['decile']:1d}  "
            f"{d['progress_lo'] * 100:5.1f}-{d['progress_hi'] * 100:5.1f} "
            f"{d['pred_cpi_mean']:6.3f} {d['label_cpi_mean']:6.3f} "
            f"{d['residual_mean']:+6.3f} {d['mem_ops_mean']:8.1f} "
            f"{d['load_frac_mem_mean'] * 100:5.1f} "
            f"{d[f'seen_line_rate_{main_k}_mean'] * 100:7.1f} "
            f"{d[f'new_line_rate_{main_k}_mean'] * 100:7.1f} "
            f"{d['cumulative_seen_line_rate_mean'] * 100:8.1f}"
        )


def print_comparison(reports: List[dict], hist_ks: List[int]) -> None:
    if len(reports) < 2:
        return
    a, b = reports[0], reports[1]
    sa = summary(a["windows"], hist_ks)
    sb = summary(b["windows"], hist_ks)
    print(f"\n## Δ {a['workload']} - {b['workload']}")
    print(
        f"label_cpi Δ={sa['label_cpi_mean'] - sb['label_cpi_mean']:+.4f} "
        f"residual Δ={sa['residual_mean'] - sb['residual_mean']:+.4f} "
        f"load_frac Δ={(sa['load_frac_mem_mean'] - sb['load_frac_mem_mean']) * 100:+.1f}% "
        f"cum_seen Δ={(sa['cumulative_seen_line_rate_mean'] - sb['cumulative_seen_line_rate_mean']) * 100:+.1f}%"
    )
    for k in hist_ks:
        print(
            f"  K={k:<7} seen Δ="
            f"{(sa[f'seen_line_rate_{k}_mean'] - sb[f'seen_line_rate_{k}_mean']) * 100:+.1f}% "
            f"new Δ="
            f"{(sa[f'new_line_rate_{k}_mean'] - sb[f'new_line_rate_{k}_mean']) * 100:+.1f}% "
            f"ws Δ="
            f"{sa[f'recent_ws_size_{k}_mean'] - sb[f'recent_ws_size_{k}_mean']:+.1f}"
        )


def main() -> None:
    args = parse_args()
    hist_ks = [int(x) for x in args.history_k.split(",") if x.strip()]
    reports = []
    missing = []
    for wl in args.workload:
        try:
            rep = analyze_workload(args.raw_root, args.dump_dir, wl, hist_ks)
        except FileNotFoundError as e:
            missing.append(str(e))
            continue
        reports.append(rep)

    for rep in reports:
        print_workload_report(rep, hist_ks)
    print_comparison(reports, hist_ks)

    if missing:
        print("\n[missing]")
        for m in missing:
            print(f"  {m}")
        print("Generate the missing dump with eval_quota_cycles.py "
              "--dump-window-jsonl-dir before comparing.")

    if args.out_json:
        os.makedirs(os.path.dirname(args.out_json) or ".", exist_ok=True)
        out = {
            "history_k": hist_ks,
            "reports": [
                {
                    "workload": r["workload"],
                    "dump_path": r["dump_path"],
                    "summary": summary(r["windows"], hist_ks),
                    "deciles": deciles(r["windows"], hist_ks),
                }
                for r in reports
            ],
            "missing": missing,
        }
        with open(args.out_json, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n[wrote] {args.out_json}")


if __name__ == "__main__":
    main()
