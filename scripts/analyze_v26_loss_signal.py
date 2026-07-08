#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import numpy as np
import torch


def huber_error(e: torch.Tensor, delta: float) -> torch.Tensor:
    ae = e.abs()
    return torch.where(ae <= delta, 0.5 * e * e, delta * (ae - 0.5 * delta))


def qstats(values: Iterable[float]) -> dict:
    arr = np.asarray(list(values), dtype=np.float64)
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "p50": float(np.quantile(arr, 0.50)),
        "p90": float(np.quantile(arr, 0.90)),
        "p95": float(np.quantile(arr, 0.95)),
        "p99": float(np.quantile(arr, 0.99)),
        "max": float(arr.max()),
    }


def make_acc() -> dict:
    return {
        "n": 0,
        "n_multi": 0,
        "n_by_core": defaultdict(int),
        "log_cpi_std": [],
        "log_cpi_range": [],
        "pair_abs_gap": [],
        "pair_huber_if_collapsed": [],
        "abs_huber_if_logmean_collapsed": [],
        "abs_huber_if_cycle_preserving_flat": [],
        "total_uops": [],
    }


def add_values(acc: dict, nc: int, total_uops: float, log_std: float,
               log_range: float, abs_logmean: float, abs_cycleflat: float,
               pair_abs: float | None, pair_huber: float | None) -> None:
    acc["n"] += 1
    acc["n_by_core"][int(nc)] += 1
    acc["total_uops"].append(float(total_uops))
    acc["log_cpi_std"].append(float(log_std))
    acc["log_cpi_range"].append(float(log_range))
    acc["abs_huber_if_logmean_collapsed"].append(float(abs_logmean))
    acc["abs_huber_if_cycle_preserving_flat"].append(float(abs_cycleflat))
    if pair_abs is not None and pair_huber is not None:
        acc["n_multi"] += 1
        acc["pair_abs_gap"].append(float(pair_abs))
        acc["pair_huber_if_collapsed"].append(float(pair_huber))


def summarize_acc(acc: dict) -> dict:
    return {
        "n": int(acc["n"]),
        "n_multi": int(acc["n_multi"]),
        "n_by_core": {str(k): int(v) for k, v in sorted(acc["n_by_core"].items())},
        "total_uops": qstats(acc["total_uops"]),
        "log_cpi_std": qstats(acc["log_cpi_std"]),
        "log_cpi_range": qstats(acc["log_cpi_range"]),
        "pair_abs_log_gap": qstats(acc["pair_abs_gap"]),
        "pair_huber_if_collapsed": qstats(acc["pair_huber_if_collapsed"]),
        "abs_huber_if_logmean_collapsed": qstats(
            acc["abs_huber_if_logmean_collapsed"]
        ),
        "abs_huber_if_cycle_preserving_flat": qstats(
            acc["abs_huber_if_cycle_preserving_flat"]
        ),
    }


def analyze_shard(shard: dict, args, overall: dict, by_workload: dict) -> None:
    n = int(shard.get("count", len(shard["n_core"])))
    n_core = shard["n_core"][:n].long()
    label = shard["label"][:n].float()
    uops = shard["uops"][:n].float()
    workloads = shard.get("workload") or [""] * n

    max_nc = int(label.shape[1])
    active = torch.arange(max_nc)[None, :] < n_core[:, None]
    active_f = active.float()
    active_count = active_f.sum(dim=1).clamp(min=1.0)
    active_uops = torch.where(active, uops, torch.zeros_like(uops))
    total_uops = active_uops.sum(dim=1)
    max_core_uops = active_uops.amax(dim=1)

    keep = torch.ones((n,), dtype=torch.bool)
    if args.train_max_total_uops > 0:
        keep &= total_uops <= float(args.train_max_total_uops)
    if args.train_max_uops_per_core > 0:
        keep &= max_core_uops <= float(args.train_max_uops_per_core)
    if args.workloads:
        wanted = set(args.workloads)
        keep &= torch.tensor([w in wanted for w in workloads], dtype=torch.bool)
    idx = keep.nonzero(as_tuple=False).flatten()
    if idx.numel() == 0:
        return

    label = label[idx]
    uops = uops[idx]
    n_core = n_core[idx]
    workloads = [workloads[int(i)] for i in idx.tolist()]
    active = active[idx]
    active_f = active.float()
    active_count = active_f.sum(dim=1).clamp(min=1.0)
    active_uops = torch.where(active, uops, torch.zeros_like(uops))
    total_uops = active_uops.sum(dim=1)

    cpi = label[..., 0].clamp(min=1.0e-6)
    log_cpi = torch.log(cpi)
    mean_log = (log_cpi * active_f).sum(dim=1) / active_count
    centered = (log_cpi - mean_log[:, None]) * active_f
    log_std = torch.sqrt((centered * centered).sum(dim=1) / active_count)
    masked_hi = log_cpi.masked_fill(~active, -float("inf")).amax(dim=1)
    masked_lo = log_cpi.masked_fill(~active, float("inf")).amin(dim=1)
    log_range = masked_hi - masked_lo

    abs_logmean = (
        huber_error(log_cpi - mean_log[:, None], 0.1) * active_f
    ).sum(dim=1) / active_count

    true_cycles = (cpi * active_uops).sum(dim=1).clamp(min=1.0e-6)
    flat_cpi = true_cycles / total_uops.clamp(min=1.0)
    abs_cycleflat = (
        huber_error(torch.log(flat_cpi)[:, None] - log_cpi, 0.1) * active_f
    ).sum(dim=1) / active_count

    C = int(label.shape[1])
    upper = torch.arange(C)[:, None] < torch.arange(C)[None, :]
    pair_mask = active[:, :, None] & active[:, None, :] & upper[None, :, :]
    pair_mask_f = pair_mask.float()
    pair_count = pair_mask_f.sum(dim=(1, 2))
    diff = log_cpi[:, :, None] - log_cpi[:, None, :]
    pair_abs = (diff.abs() * pair_mask_f).sum(dim=(1, 2)) / pair_count.clamp(min=1.0)
    pair_huber = (
        huber_error(diff, 0.1) * pair_mask_f
    ).sum(dim=(1, 2)) / pair_count.clamp(min=1.0)

    for i in range(int(label.shape[0])):
        pair_abs_i = None
        pair_huber_i = None
        if float(pair_count[i]) > 0:
            pair_abs_i = float(pair_abs[i])
            pair_huber_i = float(pair_huber[i])
        vals = (
            int(n_core[i]),
            float(total_uops[i]),
            float(log_std[i]),
            float(log_range[i]),
            float(abs_logmean[i]),
            float(abs_cycleflat[i]),
            pair_abs_i,
            pair_huber_i,
        )
        add_values(overall, *vals)
        add_values(by_workload[workloads[i]], *vals)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True,
                    help="Path to windows.maxlen*.tensor_cache")
    ap.add_argument("--train-max-total-uops", type=int, default=0)
    ap.add_argument("--train-max-uops-per-core", type=int, default=0)
    ap.add_argument("--max-shards", type=int, default=0)
    ap.add_argument("--workloads", nargs="*", default=None)
    ap.add_argument("--top-workloads", type=int, default=16)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    cache = Path(args.cache)
    manifest = torch.load(cache / "manifest.pt", map_location="cpu")
    shards = list(manifest.get("shards") or [])
    if args.max_shards > 0:
        shards = shards[:args.max_shards]

    overall = make_acc()
    by_workload = defaultdict(make_acc)
    for si, info in enumerate(shards, start=1):
        shard = torch.load(cache / info["file"], map_location="cpu")
        analyze_shard(shard, args, overall, by_workload)
        if si % 50 == 0:
            print(f"[progress] shards={si}/{len(shards)} samples={overall['n']}",
                  flush=True)

    summary = {
        "cache": str(cache),
        "total_manifest_samples": int(manifest.get("total_samples", 0)),
        "analyzed_samples": int(overall["n"]),
        "filters": {
            "train_max_total_uops": int(args.train_max_total_uops),
            "train_max_uops_per_core": int(args.train_max_uops_per_core),
            "workloads": args.workloads or [],
            "max_shards": int(args.max_shards),
        },
        "overall": summarize_acc(overall),
        "by_workload": {
            k: summarize_acc(v)
            for k, v in sorted(
                by_workload.items(),
                key=lambda kv: kv[1]["n"],
                reverse=True,
            )[:max(0, int(args.top_workloads))]
        },
        "interpretation": {
            "pair_huber_if_collapsed": (
                "Huber loss that pairwise_log_gap would assign to a model "
                "that predicts the same CPI for every active core in a window."
            ),
            "abs_huber_if_cycle_preserving_flat": (
                "Per-core CPI loss of a flat-CPI predictor that exactly "
                "matches the window sum cycles; its cycles_sum loss is zero "
                "by construction."
            ),
        },
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n")


if __name__ == "__main__":
    main()
