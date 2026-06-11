#!/usr/bin/env python3
"""Grid-search timing-aware functional-refsim parameters across datasets."""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import sys
from pathlib import Path

import _timing_functional_refsim_eval as eval_mod


def _parse_csv_int(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def _parse_csv_float(text: str) -> list[float]:
    return [float(x) for x in text.split(",") if x.strip()]


def _make_eval_args(base: argparse.Namespace, **updates) -> argparse.Namespace:
    ns = copy.copy(base)
    for k, v in updates.items():
        setattr(ns, k, v)
    return ns


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset-dirs", nargs="+", required=True)
    ap.add_argument("--tick-source", default="row",
                    choices=["row", "per_core_row", "micro_seq", "commit_tick", "issue_tick", "complete_tick"])
    ap.add_argument("--snp-tick-source", default="same",
                    choices=["same", "row", "per_core_row", "max_row_per_core", "micro_seq", "commit_tick", "issue_tick", "complete_tick"])
    ap.add_argument("--row-tick-strides", default="128,192,256,384")
    ap.add_argument("--prefetch-degrees", default="0,1,2")
    ap.add_argument("--prefetch-coverages", default="1.0")
    ap.add_argument("--l1d-capacity-factors", default="1.0")
    ap.add_argument("--llc-capacity-factors", default="1.0")
    ap.add_argument("--dram-cycles-list", default="180")
    ap.add_argument("--store-sharing-ttl-list", default="0")
    ap.add_argument("--snp-coverages", default="1.0")
    ap.add_argument("--private-sideband-load-coverages", default="0.0")
    ap.add_argument("--private-sideband-store-coverages", default="0.0")
    ap.add_argument("--l1-load-fold-l2-coverages", default="0.0")
    ap.add_argument("--l1-load-fold-llc-coverages", default="0.0")
    ap.add_argument("--l1-load-fold-min-miss-rate", type=float, default=0.01)
    ap.add_argument("--l1-load-fold-max-miss-rate", type=float, default=0.05)
    ap.add_argument("--l1-load-fold-min-llc-l2-ratio", type=float, default=2.0)
    ap.add_argument("--prefetch-visible-to-stores", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--coherence-actions-affect-coh", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--invalidate-private-on-store", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--summary-out", required=True)

    # Fixed physical defaults.
    ap.add_argument("--l1-hit-cycles", type=int, default=4)
    ap.add_argument("--l2-hit-cycles", type=int, default=12)
    ap.add_argument("--llc-hit-cycles", type=int, default=36)
    ap.add_argument("--store-wb-cycles", type=int, default=80)
    ap.add_argument("--prefetch-latency-cycles", type=int, default=40)
    ap.add_argument("--mshr-entries", type=int, default=16)
    ap.add_argument("--remote-read-fold-to-llc", action=argparse.BooleanOptionalAction, default=True)
    return ap


def main() -> None:
    args = build_argparser().parse_args()
    dataset_dirs = [Path(p).resolve() for p in args.dataset_dirs]
    for d in dataset_dirs:
        if not (d / "functional_parquet").is_dir():
            raise SystemExit(f"missing functional_parquet: {d}")
        if not (d / "all_mem_events.merged.jsonl").is_file():
            raise SystemExit(f"missing all_mem_events.merged.jsonl: {d}")

    rows = []
    grid = itertools.product(
        _parse_csv_int(args.row_tick_strides),
        _parse_csv_int(args.prefetch_degrees),
        _parse_csv_float(args.prefetch_coverages),
        _parse_csv_float(args.l1d_capacity_factors),
        _parse_csv_float(args.llc_capacity_factors),
        _parse_csv_int(args.dram_cycles_list),
        _parse_csv_int(args.store_sharing_ttl_list),
        _parse_csv_float(args.snp_coverages),
        _parse_csv_float(args.private_sideband_load_coverages),
        _parse_csv_float(args.private_sideband_store_coverages),
        _parse_csv_float(args.l1_load_fold_l2_coverages),
        _parse_csv_float(args.l1_load_fold_llc_coverages),
    )
    for (
        row_stride,
        prefetch_degree,
        prefetch_coverage,
        l1d_factor,
        llc_factor,
        dram_cycles,
        ttl,
        snp_coverage,
        private_load_cov,
        private_store_cov,
        l1_fold_l2_cov,
        l1_fold_llc_cov,
    ) in grid:
        per_dataset = []
        total_loss = 0.0
        for d in dataset_dirs:
            ev_args = _make_eval_args(
                args,
                dataset_dir=str(d),
                row_tick_stride=row_stride,
                prefetch_degree=prefetch_degree,
                prefetch_coverage=prefetch_coverage,
                l1d_capacity_factor=l1d_factor,
                llc_capacity_factor=llc_factor,
                dram_cycles=dram_cycles,
                store_sharing_ttl_cycles=ttl,
                snp_coverage=snp_coverage,
                private_sideband_load_coverage=private_load_cov,
                private_sideband_store_coverage=private_store_cov,
                l1_load_fold_l2_coverage=l1_fold_l2_cov,
                l1_load_fold_llc_coverage=l1_fold_llc_cov,
                summary_out=None,
            )
            result = eval_mod.run_once(d, ev_args)
            per_dataset.append({
                "dataset_dir": str(d),
                "loss": result["loss"],
                "functional_pmu": result["pmu"],
                "oracle_pmu": result["oracle_pmu"],
            })
            total_loss += result["loss"]
        avg_loss = total_loss / max(1, len(per_dataset))
        rows.append({
            "avg_loss": avg_loss,
            "params": {
                "tick_source": args.tick_source,
                "snp_tick_source": args.snp_tick_source,
                "row_tick_stride": row_stride,
                "prefetch_degree": prefetch_degree,
                "prefetch_coverage": prefetch_coverage,
                "prefetch_visible_to_stores": args.prefetch_visible_to_stores,
                "l1d_capacity_factor": l1d_factor,
                "llc_capacity_factor": llc_factor,
                "dram_cycles": dram_cycles,
                "store_sharing_ttl_cycles": ttl,
                "coherence_actions_affect_coh": args.coherence_actions_affect_coh,
                "invalidate_private_on_store": args.invalidate_private_on_store,
                "snp_coverage": snp_coverage,
                "private_sideband_load_coverage": private_load_cov,
                "private_sideband_store_coverage": private_store_cov,
                "l1_load_fold_l2_coverage": l1_fold_l2_cov,
                "l1_load_fold_llc_coverage": l1_fold_llc_cov,
                "l1_load_fold_min_miss_rate": args.l1_load_fold_min_miss_rate,
                "l1_load_fold_max_miss_rate": args.l1_load_fold_max_miss_rate,
                "l1_load_fold_min_llc_l2_ratio": args.l1_load_fold_min_llc_l2_ratio,
                "l1_hit_cycles": args.l1_hit_cycles,
                "l2_hit_cycles": args.l2_hit_cycles,
                "llc_hit_cycles": args.llc_hit_cycles,
                "store_wb_cycles": args.store_wb_cycles,
                "prefetch_latency_cycles": args.prefetch_latency_cycles,
                "mshr_entries": args.mshr_entries,
                "remote_read_fold_to_llc": args.remote_read_fold_to_llc,
            },
            "datasets": per_dataset,
        })
        print(
            f"loss={avg_loss:.6f} stride={row_stride} prefetch={prefetch_degree} "
            f"coverage={prefetch_coverage} l1d_factor={l1d_factor} "
            f"llc_factor={llc_factor} dram={dram_cycles} "
            f"ttl={ttl} snp_cov={snp_coverage} private_load={private_load_cov} "
            f"private_store={private_store_cov} l1_fold_l2={l1_fold_l2_cov} "
            f"l1_fold_llc={l1_fold_llc_cov}",
            flush=True,
        )

    rows.sort(key=lambda r: r["avg_loss"])
    out = {
        "dataset_dirs": [str(d) for d in dataset_dirs],
        "top_k": rows[: args.top_k],
        "all_results": rows,
    }
    Path(args.summary_out).write_text(json.dumps(out, indent=2, sort_keys=True))
    print("\n=== TOP ===")
    for r in rows[: args.top_k]:
        print(f"loss={r['avg_loss']:.6f} params={r['params']}")


if __name__ == "__main__":
    main()
