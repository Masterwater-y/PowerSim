"""Eval driver: run a trained model on a rollout directory and report metrics.

Report the required plan §8.2 metrics as far as they can be computed from a
teacher-conditioned rollout:

  - aggregate cycle / CPI error
  - per-core CPI MAPE p50 / p90 / p99
  - per-core prefix drift
  - endpoint / makespan error
  - fast/slow ratio, resident exposure buckets
  - unique chunk encode count, cache hit rate
"""
from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from typing import Dict, List, Optional

import torch
from torch.utils.data import DataLoader

from ..dataset.torch_dataset import TCSimSampleDataset, collate_variable_active
from ..model.tcsim_model import TCSimModel, StaticEmbeddingCache
from ..utils.config import TCSimConfig
from ..utils.io import load_json, dump_json


def _pctl(vals: List[float], q: float) -> float:
    if not vals:
        return float("nan")
    vs = sorted(vals)
    k = min(len(vs) - 1, max(0, int(round(q * (len(vs) - 1)))))
    return float(vs[k])


def evaluate_dir(
    ckpt_path: str,
    rollout_dirs: List[str],
    cfg: TCSimConfig,
    device: str = "cpu",
    out_path: Optional[str] = None,
) -> Dict[str, float]:
    ds = TCSimSampleDataset(rollout_dirs)
    loader = DataLoader(
        ds,
        batch_size=int(cfg.train.get("batch_samples", 8)),
        shuffle=False,
        collate_fn=collate_variable_active,
    )

    model = TCSimModel(
        d_field=int(cfg.model.get("d_field", 16)),
        d_static=int(cfg.model.get("d_static", 128)),
        d_dyn=int(cfg.model.get("d_dyn", 128)),
        n_heads=int(cfg.model.get("n_dyn_heads", 4)),
        n_layers=int(cfg.model.get("n_dyn_layers", 1)),
        ffn_dim=(
            int(cfg.model["ffn_dim"])
            if cfg.model.get("ffn_dim") is not None else None
        ),
        cross_target_block=int(cfg.model.get("cross_target_block", 0)),
        sdpa_backend=str(cfg.model.get("sdpa_backend", "auto")),
        dropout=float(cfg.model.get("dropout", 0.1)),
        max_K=int(cfg.chunk.get("K", 256)) + 32,
    ).to(device)
    state = torch.load(ckpt_path, map_location=device)
    if isinstance(state, dict) and "model" in state:
        state = state["model"]
    model.load_state_dict(state)
    model.eval()

    per_core_series: Dict[tuple, Dict[int, tuple]] = defaultdict(dict)
    resident_bucket: Dict[int, int] = defaultdict(int)
    fast_slow: Dict[str, int] = {"fast": 0, "slow": 0}
    core_cpi_ape: List[float] = []
    identifiable_centered_abs: List[float] = []
    identifiable_slow_correct: List[float] = []
    identifiable_spread_ratio: List[float] = []
    total_pred_cycles = 0.0
    total_true_cycles = 0.0

    with torch.no_grad():
        for batch in loader:
            b = {
                k: (
                    v if k == "sample_ptr" else v.to(device)
                ) if isinstance(v, torch.Tensor) else v
                for k, v in batch.items()
            }
            preds = model(b)
            sptr = b["sample_ptr"].cpu().tolist()
            group_ids = b["functional_group_id"]
            context_mask = b["context_label_mask"].bool()
            for sample_index in range(len(sptr) - 1):
                start, end = sptr[sample_index], sptr[sample_index + 1]
                cm = context_mask[start:end]
                if int(cm.sum()) < 2:
                    continue
                p_core = preds["log_cpi"][start:end][cm]
                y_core = b["log_cpi"][start:end][cm]
                gid = group_ids[start:end][cm]
                unique = torch.unique(gid)
                if int(unique.numel()) < 2:
                    continue
                p_group = torch.stack([p_core[gid == value].mean() for value in unique])
                y_group = torch.stack([y_core[gid == value].mean() for value in unique])
                y_center = y_group - y_group.mean()
                if float(y_center.std(unbiased=False)) < float(
                    cfg.train.get("centered_spread_threshold", 0.10)
                ):
                    continue
                p_center = p_group - p_group.mean()
                identifiable_centered_abs.append(float((p_center - y_center).abs().mean()))
                identifiable_slow_correct.append(float(
                    int(torch.argmax(p_group)) == int(torch.argmax(y_group))
                ))
                identifiable_spread_ratio.append(float(
                    p_center.std(unbiased=False)
                    / y_center.std(unbiased=False).clamp(min=1e-8)
                ))
            mask = b["label_mask"].bool()
            expo = b["exposure"].cpu().tolist()
            ctx = b["context_only"].cpu().tolist()
            for e, is_ctx in zip(expo, ctx):
                bucket = int(min(e, 32))
                resident_bucket[bucket] += 1
                if is_ctx > 0:
                    fast_slow["slow"] += 1
                else:
                    fast_slow["fast"] += 1
            if mask.sum().item() == 0:
                continue
            pdc = preds["pred_delta_cycles"][mask].cpu().tolist()
            tdc = b["delta_cycles"][mask].cpu().tolist()
            core_ids = b["core_ids"][mask].cpu().tolist()
            commit_idx = b["commit_index"][mask].cpu().tolist()
            trace_ids = b["trace_id"]
            step = b["step"].cpu().tolist()
            # sample_ptr slices core rows back into per-sample groups
            # Build a flat row index -> sample_index mapping so we can look up trace_id
            row2sample = []
            for si in range(len(sptr) - 1):
                row2sample.extend([si] * (sptr[si + 1] - sptr[si]))
            # Now walk only masked rows in original ordering:
            all_idx = torch.nonzero(mask, as_tuple=False).squeeze(-1).cpu().tolist()
            n_uops_masked = b["n_uops"][mask].cpu().tolist()
            for local, orig in enumerate(all_idx):
                tid = trace_ids[row2sample[orig]] if isinstance(trace_ids, list) else trace_ids
                key = (tid, int(core_ids[local]))
                per_core_series[key][int(commit_idx[local])] = (
                    float(pdc[local]), float(tdc[local]), float(n_uops_masked[local]),
                )
                total_pred_cycles += float(pdc[local])
                total_true_cycles += float(tdc[local])
                ape = abs(pdc[local] - tdc[local]) / max(1.0, tdc[local])
                cpi_p = pdc[local] / max(1, int(n_uops_masked[local]))
                cpi_t = tdc[local] / max(1, int(n_uops_masked[local]))
                core_cpi_ape.append(abs(cpi_p - cpi_t) / max(1e-3, cpi_t))

    prefix_lens = list(cfg.train.get("prefix_lens", [4, 8, 16, 32]))
    prefix_drift: Dict[int, List[float]] = defaultdict(list)
    endpoint_err: List[float] = []
    trace_core_totals: Dict[str, List[tuple]] = defaultdict(list)
    for key, series in per_core_series.items():
        if not series:
            continue
        ordered = [series[k] for k in sorted(series.keys())]
        pred_seq = [x[0] for x in ordered]
        true_seq = [x[1] for x in ordered]
        for L in prefix_lens:
            if len(pred_seq) >= L:
                p = sum(pred_seq[:L])
                t = sum(true_seq[:L])
                prefix_drift[L].append(abs(p - t) / max(1.0, t))
        p_total = sum(pred_seq)
        t_total = sum(true_seq)
        endpoint_err.append(abs(p_total - t_total) / max(1.0, t_total))
        trace_core_totals[str(key[0])].append((p_total, t_total))

    trace_aggregate_err: List[float] = []
    makespan_err: List[float] = []
    for values in trace_core_totals.values():
        pred_sum = sum(value[0] for value in values)
        true_sum = sum(value[1] for value in values)
        trace_aggregate_err.append(abs(pred_sum - true_sum) / max(1.0, true_sum))
        pred_makespan = max(value[0] for value in values)
        true_makespan = max(value[1] for value in values)
        makespan_err.append(
            abs(pred_makespan - true_makespan) / max(1.0, true_makespan)
        )

    report: Dict[str, float] = {
        "n_samples": len(ds),
        "n_committed_cores": sum(1 for s in per_core_series.values() if s),
        "cpi_mape_p50": _pctl(core_cpi_ape, 0.50),
        "cpi_mape_p90": _pctl(core_cpi_ape, 0.90),
        "cpi_mape_p99": _pctl(core_cpi_ape, 0.99),
        "cpi_mape_mean": sum(core_cpi_ape) / max(1, len(core_cpi_ape)),
        "global_aggregate_cycle_error": abs(total_pred_cycles - total_true_cycles) / max(1.0, total_true_cycles),
        "trace_aggregate_cycle_err_p50": _pctl(trace_aggregate_err, 0.50),
        "trace_aggregate_cycle_err_p90": _pctl(trace_aggregate_err, 0.90),
        "makespan_err_p50": _pctl(makespan_err, 0.50),
        "makespan_err_p90": _pctl(makespan_err, 0.90),
        "endpoint_err_p50": _pctl(endpoint_err, 0.50),
        "endpoint_err_p90": _pctl(endpoint_err, 0.90),
        "endpoint_err_p99": _pctl(endpoint_err, 0.99),
        "fast_samples": fast_slow["fast"],
        "slow_samples": fast_slow["slow"],
        "identifiable_centered_log_mae": (
            sum(identifiable_centered_abs) / max(1, len(identifiable_centered_abs))
        ),
        "identifiable_slow_group_top1_accuracy": (
            sum(identifiable_slow_correct) / max(1, len(identifiable_slow_correct))
        ),
        "identifiable_spread_ratio_p50": _pctl(identifiable_spread_ratio, 0.50),
        "n_identifiable_contexts": len(identifiable_centered_abs),
    }
    for L, series in prefix_drift.items():
        report[f"prefix_drift_L{L}_p50"] = _pctl(series, 0.50)
        report[f"prefix_drift_L{L}_p90"] = _pctl(series, 0.90)
    report["resident_exposure_bucket"] = dict(resident_bucket)

    if out_path:
        dump_json(out_path, report)
    return report
