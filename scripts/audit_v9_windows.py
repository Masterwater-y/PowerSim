#!/usr/bin/env python3
"""Audit v9 composite-uop windows JSONL.

Checks schema, PMU keys, composite-uop alignment, side tensor shape, and TQ
geometry. This is intentionally lightweight and avoids importing torch.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from statistics import mean
from typing import Iterable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from model.regression_head import PMU_KEYS
from model import tokenizer as tk


def _pct(x: float) -> str:
    return f"{100.0 * x:.2f}%"


def _read_jsonl(path: str, limit: int = 0) -> Iterable[dict]:
    n = 0
    with open(path) as f:
        for line in f:
            s = line.strip()
            if not s or not s.startswith("{"):
                continue
            yield json.loads(s)
            n += 1
            if limit and n >= limit:
                return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--min-uops-per-core", type=int, default=256)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    n = 0
    bad: list[str] = []
    warnings: list[str] = []
    workloads = Counter()
    ncores = Counter()
    token_lens: list[int] = []
    uop_counts: list[int] = []
    min_core: list[int] = []
    max_core: list[int] = []
    variable_core_split = 0
    global_tokens = Counter()
    tq_span_ticks: list[int] = []
    fill_ratios: list[float] = []
    l2_positive = Counter()
    samples_by_workload = defaultdict(int)

    for rec in _read_jsonl(args.data, args.limit):
        n += 1
        wid = rec.get("workload", "<unknown>")
        workloads[wid] += 1
        samples_by_workload[wid] += 1
        nc = int(rec.get("n_core", 0) or 0)
        ncores[nc] += 1

        tokens = rec.get("tokens") or []
        is_uop = rec.get("is_uop")
        uop_fields = rec.get("uop_fields")
        side_feats = rec.get("side_feats")
        core_split = rec.get("core_split") or []
        labels = rec.get("label") or []
        label_keys = rec.get("label_keys")

        token_lens.append(len(tokens))
        if len(tokens) > args.max_len:
            bad.append(f"{rec.get('id')}: len(tokens)={len(tokens)} > {args.max_len}")
        if not isinstance(label_keys, list):
            bad.append(f"{rec.get('id')}: label_keys missing/invalid {label_keys}")
        elif any(k not in label_keys for k in PMU_KEYS):
            bad.append(f"{rec.get('id')}: label_keys missing current keys {label_keys}")
        elif label_keys != PMU_KEYS:
            warnings.append(
                f"{rec.get('id')}: label_keys has extra legacy keys {label_keys}"
            )
        if len(labels) != nc:
            bad.append(f"{rec.get('id')}: labels n_core mismatch")
        if len(core_split) != nc:
            bad.append(f"{rec.get('id')}: core_split n_core mismatch")
        if is_uop is None or len(is_uop) != len(tokens):
            bad.append(f"{rec.get('id')}: is_uop missing/length mismatch")
            is_uop = []
        if uop_fields is None or len(uop_fields) != len(tokens):
            bad.append(f"{rec.get('id')}: uop_fields missing/length mismatch")
            uop_fields = []
        if side_feats is None or len(side_feats) != nc:
            bad.append(f"{rec.get('id')}: side_feats n_core mismatch")
            side_feats = []

        uop_n = int(sum(int(x) for x in is_uop))
        uop_counts.append(uop_n)
        if uop_n != sum(int(x) for x in core_split):
            bad.append(
                f"{rec.get('id')}: uop positions {uop_n} != core_split sum {sum(core_split)}"
            )
        if tokens.count("<UOP>") != uop_n:
            bad.append(f"{rec.get('id')}: <UOP> token count mismatch")
        for i, flag in enumerate(is_uop):
            if int(flag):
                if tokens[i] != "<UOP>":
                    bad.append(f"{rec.get('id')}: is_uop at non-<UOP> token")
                    break
                if len(uop_fields[i]) != 6:
                    bad.append(f"{rec.get('id')}: uop_fields width != 6")
                    break
        for row in side_feats:
            if len(row) != len(tk.SIDE_FEATURE_KEYS):
                bad.append(
                    f"{rec.get('id')}: side_feats width {len(row)} != {len(tk.SIDE_FEATURE_KEYS)}"
                )
                break

        if core_split:
            mn = min(int(x) for x in core_split)
            mx = max(int(x) for x in core_split)
            min_core.append(mn)
            max_core.append(mx)
            if mn < args.min_uops_per_core:
                bad.append(
                    f"{rec.get('id')}: min core_split {mn} < {args.min_uops_per_core}"
                )
            if mx > mn:
                variable_core_split += 1

        for tok in rec.get("global_tokens") or []:
            global_tokens[tok] += 1

        if "t_start_tick" in rec and "t_end_tick" in rec:
            span = int(rec["t_end_tick"]) - int(rec["t_start_tick"])
            tq_span_ticks.append(span)
            if "tq_span_tick" in rec and int(rec["tq_span_tick"]) != span:
                bad.append(f"{rec.get('id')}: tq_span_tick mismatch")
            if span < 0:
                bad.append(f"{rec.get('id')}: negative TQ span")
        else:
            warnings.append(f"{rec.get('id')}: no t_start_tick/t_end_tick metadata")

        if "fill_ratio" in rec:
            try:
                fill_ratios.append(float(rec["fill_ratio"]))
            except Exception:
                pass

        try:
            li = label_keys.index("l2_ld_miss")
            si = label_keys.index("l2_st_miss")
            if any(float(row[li]) > 0 for row in labels):
                l2_positive["l2_ld_miss"] += 1
            if any(float(row[si]) > 0 for row in labels):
                l2_positive["l2_st_miss"] += 1
        except Exception:
            pass

    print(f"file={args.data}")
    print(f"samples={n}")
    print(f"workloads={dict(workloads)}")
    print(f"ncores={dict(ncores)}")
    if token_lens:
        print(
            "tokens min/mean/max="
            f"{min(token_lens)}/{mean(token_lens):.1f}/{max(token_lens)}"
        )
    if uop_counts:
        print(
            "uop_positions min/mean/max="
            f"{min(uop_counts)}/{mean(uop_counts):.1f}/{max(uop_counts)}"
        )
    if min_core:
        print(
            "core_split min_core min/mean/max="
            f"{min(min_core)}/{mean(min_core):.1f}/{max(min_core)}"
        )
        print(
            "core_split max_core min/mean/max="
            f"{min(max_core)}/{mean(max_core):.1f}/{max(max_core)}"
        )
        print(
            f"variable_core_split={variable_core_split}/{len(min_core)} "
            f"({_pct(variable_core_split / max(len(min_core), 1))})"
        )
        if variable_core_split == 0 and max(ncores, default=0) > 1:
            warnings.append("all samples have equal per-core uop counts")
    if fill_ratios:
        print(
            "fill_ratio min/mean/max="
            f"{min(fill_ratios):.4f}/{mean(fill_ratios):.4f}/{max(fill_ratios):.4f}"
        )
    if tq_span_ticks:
        print(
            "tq_span_tick min/mean/max="
            f"{min(tq_span_ticks)}/{mean(tq_span_ticks):.1f}/{max(tq_span_ticks)}"
        )
    print(f"global_tokens={dict(global_tokens)}")
    print(f"l2_positive_samples={dict(l2_positive)}")
    if warnings:
        dedup = list(dict.fromkeys(warnings))
        print(f"warnings={len(dedup)}")
        for w in dedup[:20]:
            print(f"  WARN {w}")
    if bad:
        print(f"FAIL bad={len(bad)}")
        for b in bad[:50]:
            print(f"  BAD {b}")
        raise SystemExit(1)
    print("PASS")


if __name__ == "__main__":
    main()
