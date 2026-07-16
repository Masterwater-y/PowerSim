#!/usr/bin/env python3
"""Real-trace v29 build -> train step -> checkpoint -> deployment smoke."""
from __future__ import annotations

import argparse
import os
import shutil
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

import torch

from tcsim.utils.config import TCSimConfig
from tcsim.utils.io import dump_json
from tcsim.v29.builder import build_trace_cache
from tcsim.v29.dataset import V29GlobalTimeDataset, V29TraceStore, collate_v29_sequences
from tcsim.v29.inference import (
    evaluate_oracle_one_step,
    load_checkpoint_runner,
    run_free_running,
)
from tcsim.v29.losses import compute_v29_losses
from tcsim.v29.model import build_model
from tcsim.v29.train import train_one_run


CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


def _config(store: V29TraceStore) -> TCSimConfig:
    return TCSimConfig(
        chunk={
            "K": 256,
            "horizons": list(store.horizons),
            "sample_period_cycles": store.sample_period_cycles,
            "sequence_length": 2,
            "sequence_stride": 2,
        },
        scheduler={
            "target_stride": 4,
            "min_step_cycles": 1.0,
            "max_step_cycles": 64.0,
            "max_no_progress_steps": 8,
        },
        model={
            "d_field": 8,
            "d_dynamic_field": 4,
            "d_static": 32,
            "d_dyn": 64,
            "n_dyn_heads": 4,
            "n_dyn_layers": 1,
            "ffn_dim": 128,
            "dropout": 0.0,
            "sdpa_backend": "math",
            "commit_temperature": 4.0,
            "gap_softplus_beta": 4.0,
        },
        train={
            "amp_dtype": "fp32",
            "batch_samples": 1,
            "num_workers": 0,
            "trace_balanced_sampling": True,
            "epochs": 2,
            "log_every": 1,
            "eval_every": 0,
            "save_every": 1,
            "gradient_clip": 5.0,
            "time_huber_beta": 0.2,
            "progress_count_beta": 8.0,
            "branch_count_beta": 1.0,
            "loss_weights": {
                "commit_time": 1.0,
                "prefix_bce": 0.5,
                "progress_count": 0.5,
                "cumulative": 0.25,
                "branch_token": 0.1,
                "branch_count": 0.1,
            },
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--trace-dir",
        default=(
            "/data00/yinhaolang/TSim/data/"
            "raw_v28_1_business_a2_sharedzipf_seed0_c01/"
            "W_v28_int_alu_dense/tao_trace"
        ),
    )
    parser.add_argument("--out", default="/tmp/tcsim_v29_e2e_smoke")
    parser.add_argument("--max-samples", type=int, default=16)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--reuse-cache", action="store_true")
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    cache_dir = os.path.join(out, "cache")
    checkpoint_dir = os.path.join(out, "checkpoint")
    checkpoint_path = os.path.join(checkpoint_dir, "best.pt")
    os.makedirs(out, exist_ok=True)
    if not args.reuse_cache:
        shutil.rmtree(cache_dir, ignore_errors=True)
        print(f"[v29 smoke] building real trace cache: {args.trace_dir}", flush=True)
        build_trace_cache(
            args.trace_dir,
            cache_dir,
            max_samples=args.max_samples,
            overwrite=True,
        )
    store = V29TraceStore(cache_dir)
    if len(store) < 2:
        raise RuntimeError("v29 smoke cache needs at least two common-time samples")
    config = _config(store)
    dataset = V29GlobalTimeDataset(
        [cache_dir], sequence_length=2, sequence_stride=2,
    )
    batch = collate_v29_sequences([dataset[0]])
    device = torch.device(args.device)
    batch = {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }
    model = build_model(config.model, store.horizons).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=3.0e-4)
    optimizer.zero_grad(set_to_none=True)
    predictions = model(batch)
    losses = compute_v29_losses(
        predictions,
        batch,
        weights=config.train["loss_weights"],
        time_beta=config.train["time_huber_beta"],
        progress_count_beta=config.train["progress_count_beta"],
        branch_count_beta=config.train["branch_count_beta"],
    )
    losses.total.backward()
    optimizer.step()
    if losses.monotonic_violations:
        raise RuntimeError("v29 smoke model violated monotonicity")
    shutil.rmtree(checkpoint_dir, ignore_errors=True)
    training = train_one_run(
        [cache_dir], [], checkpoint_dir, config,
        device=args.device, max_steps=1,
    )
    runner = load_checkpoint_runner(
        checkpoint_path,
        device=args.device,
        amp_dtype="fp32",
        sdpa_backend="math",
    )
    oracle = evaluate_oracle_one_step(store, runner, max_samples=2)
    free = run_free_running(
        store,
        runner,
        source={"workload": store.meta.get("workload"), "seed": 0},
        target_stride=4,
        min_step_cycles=1.0,
        max_step_cycles=64.0,
        max_no_progress_steps=8,
        max_steps=2,
    )
    result = {
        "cache": cache_dir,
        "checkpoint": checkpoint_path,
        "n_uops": int(store.meta["n_uops"]),
        "n_samples": len(store),
        "train_loss": float(losses.total.detach()),
        "training_entry": training,
        "oracle": oracle,
        "free_running": free,
        "pass": bool(
            oracle["prefix_monotonic_token_violations"] == 0
            and oracle["prefix_monotonic_horizon_violations"] == 0
            and free["steps"] == 2
            and free["model_context_uses_oracle_timing"] is False
        ),
    }
    dump_json(os.path.join(out, "smoke_report.json"), result)
    print(
        f"[v29 smoke] pass={result['pass']} loss={result['train_loss']:.5f} "
        f"oracle_rows={oracle['active_core_rows']} free_steps={free['steps']} "
        f"report={os.path.join(out, 'smoke_report.json')}",
        flush=True,
    )
    return 0 if result["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
