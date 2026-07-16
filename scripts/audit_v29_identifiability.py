#!/usr/bin/env python3
"""Run v29 visible-signature variance and single-sample overfit gates."""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from tcsim.utils.config import TCSimConfig
from tcsim.utils.io import dump_json
from tcsim.v29.diagnostics import single_sample_overfit, visible_signature_audit
from tcsim.v29.inference import discover_sources, load_manifest_sources


def main() -> int:
    parser = argparse.ArgumentParser()
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest")
    source.add_argument("--cache-root")
    parser.add_argument("--split", default="train")
    parser.add_argument("--out", required=True)
    parser.add_argument(
        "--config", default=os.path.join(ROOT, "configs", "v29_100m.yaml"),
    )
    parser.add_argument("--max-traces", type=int, default=0)
    parser.add_argument("--max-samples-per-trace", type=int, default=2000)
    parser.add_argument("--near-prefix-tokens", type=int, default=32)
    parser.add_argument("--near-float-step", type=float, default=0.05)
    parser.add_argument("--skip-visible-signature", action="store_true")
    parser.add_argument("--run-overfit", action="store_true")
    parser.add_argument("--overfit-source-index", type=int, default=0)
    parser.add_argument("--overfit-sequence-index", type=int, default=0)
    parser.add_argument("--overfit-steps", type=int, default=300)
    parser.add_argument("--overfit-lr", type=float, default=3.0e-4)
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--tiny-model", action="store_true")
    args = parser.parse_args()

    sources = (
        load_manifest_sources(args.manifest, [args.split])
        if args.manifest else discover_sources(args.cache_root)
    )
    if args.max_traces > 0:
        sources = sources[:args.max_traces]
    if not sources:
        raise SystemExit("no v29 sources selected")
    report = {
        "schema_version": "tcsim-v29-identifiability-gates-1",
        "sources": len(sources),
        "split": args.split,
    }
    if not args.skip_visible_signature:
        print(
            f"[v29 identifiability] visible signatures traces={len(sources)}",
            flush=True,
        )
        report["visible_signature"] = visible_signature_audit(
            sources,
            max_samples_per_trace=args.max_samples_per_trace,
            near_prefix_tokens=args.near_prefix_tokens,
            near_float_step=args.near_float_step,
        )
    if args.run_overfit:
        source_index = int(args.overfit_source_index) % len(sources)
        print(
            f"[v29 identifiability] single-sample overfit source={source_index} "
            f"steps={args.overfit_steps}",
            flush=True,
        )
        report["single_sample_overfit"] = single_sample_overfit(
            sources[source_index],
            TCSimConfig.load(args.config),
            sample_index=args.overfit_sequence_index,
            steps=args.overfit_steps,
            learning_rate=args.overfit_lr,
            device=args.device,
            tiny_model=args.tiny_model,
        )
    blockers = []
    if (
        "single_sample_overfit" in report
        and not bool(report["single_sample_overfit"].get("pass"))
    ):
        blockers.append("single-sample overfit gate failed")
    report["quality"] = {
        "status": "pass" if not blockers else "fail",
        "blockers": blockers,
        "visible_signature_is_diagnostic_not_a_fixed_threshold_gate": True,
    }
    dump_json(args.out, report)
    visible = report.get("visible_signature", {})
    if visible:
        exact = visible["exact"]
        near = visible["near"]
        print(
            "[v29 identifiability] "
            f"exact coverage={exact['duplicate_row_coverage']:.4f} "
            f"head-rmse={exact['head_log_time_irreducible_rmse']} "
            f"near coverage={near['duplicate_row_coverage']:.4f} "
            f"head-rmse={near['head_log_time_irreducible_rmse']}",
            flush=True,
        )
    if "single_sample_overfit" in report:
        overfit = report["single_sample_overfit"]
        print(
            f"[v29 overfit] pass={overfit['pass']} "
            f"ratio={overfit['final_to_initial_ratio']:.5f} "
            f"log-mae={overfit['final_commit_log_mae']:.5f}",
            flush=True,
        )
    print(f"[v29 identifiability] report={os.path.abspath(args.out)}", flush=True)
    return 0 if not blockers else 2


if __name__ == "__main__":
    raise SystemExit(main())
