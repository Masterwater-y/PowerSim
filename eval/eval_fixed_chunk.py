"""eval_fixed_chunk.py — LLMSim inference over TCSim EpsilonResidentScheduler.

Replaces the v22 ``OnlineQuotaPlanner`` tail-align path with the TCSim
fixed-chunk resident scheduler. Consumes chunks/labels produced by
``data/build_v28_1_macro_chunks.py`` and evaluates one workload run at a time.

Reuses:
  - ``tcsim.scheduler.epsilon_resident.EpsilonResidentScheduler`` for the
    exactly-once, epsilon-tail-aligned resident scheduler (verbatim; no fork).
  - Existing LLMSim ``LLMSimModel + PMURegressionHead``: the predictor takes
    ``pred_pmu[..., CPI_UOP_IDX]`` and returns ``Δ̂ = pred_cpi_uop * n_uops``.
    Once a ``cpi_macro`` head lands, swap to ``Δ̂ = pred_cpi_macro * n_macros``.

This module is intentionally minimal: it computes chunk-level CPI MAPE,
per-core ROI CPI error, endpoint/makespan error, and exact-once counters,
which cover the TCSim ``deployment.py::_summary`` core metrics. Full parity
with TCSim's report block can be added when we have a native ``cpi_macro`` head.

Usage:
  /data00/yinhaolang/infer/.venv/bin/python eval/eval_fixed_chunk.py \\
      --chunks-root data/v28_1/chunks \\
      --run-id v28_1_a2_sharedzipf_seed0_c04_W_v28_int_alu_dense \\
      --ckpt ckpt/v22_fixed_step14000_0706 \\
      --epsilon 2048.0 --max-resident-exposure 256 \\
      --output logs/eval_fixed_chunk_smoke.json

If ``--dry-predictor`` is set, the model is skipped and the label CPI is used
as the predictor. This is a pure scheduler/pipeline smoke test.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "pyarrow is required; run with /data00/yinhaolang/infer/.venv/bin/python"
    ) from exc

_TCSIM_PATH = "/data00/yinhaolang/TCSim"
if _TCSIM_PATH not in sys.path:
    sys.path.insert(0, _TCSIM_PATH)
from tcsim.scheduler.epsilon_resident import (  # type: ignore  # noqa: E402
    EpsilonResidentScheduler, Chunk as _TCSimChunk,
)

# LLMSim imports are optional (only when --dry-predictor is not set)
_LLMSIM_PATH = "/data00/yinhaolang/LLMSim"
if _LLMSIM_PATH not in sys.path:
    sys.path.insert(0, _LLMSIM_PATH)


# ---------------------------------------------------------------------------
# Chunk adaptor — the scheduler only requires a minimal duck-typed object.
# ---------------------------------------------------------------------------

@dataclass
class MacroChunkView:
    """Minimal shape the EpsilonResidentScheduler needs from a chunk."""
    trace_id: str
    core_id: int
    chunk_id: int
    n_uops: int
    n_macros: int
    has_atomic: bool
    has_serialize: bool
    # Extra payload the predictor uses (not consumed by the scheduler):
    per_macro_static_pc: List[int]
    per_macro_uop_count: List[int]
    per_macro_op_class: List[int]
    per_macro_flags: List[int]


def _load_chunks_and_labels(chunks_dir: str) -> Tuple[Dict[int, List[MacroChunkView]],
                                                       Dict[Tuple[int, int], dict]]:
    ch_path = os.path.join(chunks_dir, "chunks.parquet")
    lb_path = os.path.join(chunks_dir, "labels.parquet")
    if not (os.path.isfile(ch_path) and os.path.isfile(lb_path)):
        raise FileNotFoundError(f"expected chunks/labels parquet under {chunks_dir}")
    ch = pq.read_table(ch_path).to_pydict()
    lb = pq.read_table(lb_path).to_pydict()
    per_core: Dict[int, List[MacroChunkView]] = {}
    trace_id = str(ch["trace_id"][0]) if ch["trace_id"] else "trace"
    n = len(ch["core_id"])
    for i in range(n):
        core_id = int(ch["core_id"][i])
        flags_list = list(ch["per_macro_flags"][i])
        # Detect atomic/serialize flags via macro-flag bit 2/7 (see build_v28_1_macro_chunks._MACRO_FLAG_BITS).
        has_atomic = any((int(f) >> 2) & 1 for f in flags_list)
        has_ser = any((int(f) >> 7) & 1 for f in flags_list)
        view = MacroChunkView(
            trace_id=str(ch["trace_id"][i]),
            core_id=core_id,
            chunk_id=int(ch["chunk_id"][i]),
            n_uops=int(ch["n_uops"][i]),
            n_macros=int(ch["n_macros"][i]),
            has_atomic=bool(has_atomic),
            has_serialize=bool(has_ser),
            per_macro_static_pc=list(ch["per_macro_static_pc"][i]),
            per_macro_uop_count=list(ch["per_macro_uop_count"][i]),
            per_macro_op_class=list(ch["per_macro_op_class"][i]),
            per_macro_flags=flags_list,
        )
        per_core.setdefault(core_id, []).append(view)
    # sort by chunk_id
    for c in per_core:
        per_core[c].sort(key=lambda v: v.chunk_id)
    labels: Dict[Tuple[int, int], dict] = {}
    for i in range(len(lb["core_id"])):
        labels[(int(lb["core_id"][i]), int(lb["chunk_id"][i]))] = {
            "delta_cycles": lb["delta_cycles"][i],
            "cpi_uop": lb["cpi_uop"][i],
            "cpi_macro": lb["cpi_macro"][i],
            "start_tick": lb["start_tick"][i],
            "end_tick": lb["end_tick"][i],
            "valid_label": bool(lb["valid_label"][i]),
        }
    return per_core, labels


# ---------------------------------------------------------------------------
# Predictors
# ---------------------------------------------------------------------------

class LabelOraclePredictor:
    """Uses the label ``delta_cycles`` as Δ̂. Only for smoke-testing the pipeline."""

    def __init__(self, labels: Dict[Tuple[int, int], dict]):
        self.labels = labels
        self.fallback = float("nan")

    def __call__(self, core_id: int, chunk, state: dict, ctx: List[dict]) -> float:
        row = self.labels.get((int(core_id), int(chunk.chunk_id)))
        if not row or not row.get("valid_label"):
            # Fall back to a mid-range CPI if label is missing.
            return float(max(1.0, 1.0 * getattr(chunk, "n_uops", 1)))
        return float(row["delta_cycles"])


class ModelPredictor:
    """Wraps ``LLMSimModel`` + ``PMURegressionHead``.

    NOTE: v22 head only produces ``cpi_uop``; we compute
        Δ̂ = pred_cpi_uop * chunk.n_uops
    which is dimensionally identical to ``pred_cpi_macro * chunk.n_macros``
    when the per-macro uop count is stable. Once a ``cpi_macro`` head is
    trained we should swap this over.
    """

    def __init__(self, ckpt_path: str, device: str = "cuda") -> None:
        import torch  # local import so --dry-predictor works without torch
        from model.llm_wrapper import LLMSimModel, WrapperConfig, build_tokenizer  # noqa: E402
        from model.regression_head import PMU_KEYS  # noqa: E402
        self.torch = torch
        self.PMU_KEYS = PMU_KEYS
        self.CPI_IDX = PMU_KEYS.index("cpi_uop")
        # Best-effort ckpt load: we support the v22 checkpoint schema.
        if not os.path.isdir(ckpt_path) and not os.path.isfile(ckpt_path):
            raise FileNotFoundError(ckpt_path)
        # We defer full model wiring to a Phase 1 follow-up: predictor
        # currently returns a constant CPI computed from chunk statistics
        # as a stub, unless a full ModelPredictor is patched in.
        raise NotImplementedError(
            "ModelPredictor wiring for v22 checkpoints is Phase 1 work; "
            "use --dry-predictor for the Phase 0 pipeline gate."
        )


# ---------------------------------------------------------------------------
# Metric collection
# ---------------------------------------------------------------------------

def _mape(pred: float, label: float) -> float:
    return abs(pred - label) / max(1.0, abs(label))


def _summarize(samples, per_core_chunks: Dict[int, List[MacroChunkView]],
               labels: Dict[Tuple[int, int], dict],
               scheduler: EpsilonResidentScheduler) -> Dict[str, Any]:
    committed = 0
    chunk_ape: List[float] = []
    per_core_pred_cycles: Dict[int, float] = {c: 0.0 for c in per_core_chunks}
    per_core_true_cycles: Dict[int, float] = {c: 0.0 for c in per_core_chunks}
    endpoint_pred: Dict[int, float] = {}
    endpoint_true: Dict[int, float] = {}
    # Reconstruct predicted per-core cycles from scheduler samples.
    # Each sample.core_records has audit_E_pred == T_pred + delta_hat for that
    # newly loaded chunk. When the same core is resident, its delta_hat is
    # committed exactly once at the fast step.
    committed_key_delta: Dict[Tuple[int, int], float] = {}
    for s in samples:
        for r in s.core_records:
            if r["first_exposure"]:
                committed_key_delta[(int(r["core_id"]), int(r["chunk_id"]))] = float(
                    r["audit_delta_hat"]
                )
    # We treat fast_cores at each step as the commit event.
    for s in samples:
        for c in s.fast_cores:
            core_id = int(c)
            # figure out committed chunk_id from core_records
            rec = next((r for r in s.core_records if r["core_id"] == c), None)
            if rec is None:
                continue
            chunk_id = int(rec["chunk_id"])
            pred_delta = float(committed_key_delta.get((core_id, chunk_id),
                                                        rec["audit_delta_hat"]))
            per_core_pred_cycles[core_id] += pred_delta
            row = labels.get((core_id, chunk_id))
            if row and row.get("valid_label"):
                true_delta = float(row["delta_cycles"])
                per_core_true_cycles[core_id] += true_delta
                chunk_ape.append(_mape(pred_delta, true_delta))
                endpoint_true[core_id] = float(row["end_tick"])
                endpoint_pred[core_id] = float(row["start_tick"]) + pred_delta * scheduler_tpc_hint()
            committed += 1
    # Aggregate
    per_core_cpi_err: List[float] = []
    for c in per_core_chunks:
        if per_core_true_cycles.get(c, 0.0) > 0:
            per_core_cpi_err.append(
                _mape(per_core_pred_cycles[c], per_core_true_cycles[c])
            )
    pred_makespan = max(per_core_pred_cycles.values()) if per_core_pred_cycles else 0.0
    true_makespan = max(per_core_true_cycles.values()) if per_core_true_cycles else 0.0
    return {
        "n_samples": len(samples),
        "n_committed": committed,
        "n_resident_events": scheduler.stats.n_resident_events,
        "max_exposure": scheduler.stats.max_exposure,
        "n_unique_chunk_encodes": scheduler.stats.n_unique_chunk_encodes,
        "chunk_cpi_mape_mean": (sum(chunk_ape) / len(chunk_ape)) if chunk_ape else None,
        "chunk_cpi_mape_p50": _percentile(chunk_ape, 0.5),
        "chunk_cpi_mape_p90": _percentile(chunk_ape, 0.9),
        "per_core_cpi_err_mean": (sum(per_core_cpi_err) / len(per_core_cpi_err))
            if per_core_cpi_err else None,
        "pred_makespan": pred_makespan,
        "true_makespan": true_makespan,
        "makespan_error": (
            abs(pred_makespan - true_makespan) / max(1.0, abs(true_makespan))
            if true_makespan > 0 else None
        ),
    }


def scheduler_tpc_hint() -> float:
    return 1.0  # scheduler uses cycles directly (Δ̂ already in cycles)


def _percentile(vals: Sequence[float], q: float) -> Optional[float]:
    if not vals:
        return None
    xs = sorted(float(v) for v in vals if math.isfinite(float(v)))
    if not xs:
        return None
    pos = q * (len(xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks-root", default="/data00/yinhaolang/LLMSim/data/v28_1/chunks")
    ap.add_argument("--run-id", required=True,
                    help="basename directory under chunks-root (one workload run)")
    ap.add_argument("--ckpt", default="",
                    help="model checkpoint dir (for model predictor)")
    ap.add_argument("--dry-predictor", action="store_true",
                    help="use label oracle instead of the model (smoke test only)")
    ap.add_argument("--epsilon", type=float, default=2048.0)
    ap.add_argument("--max-resident-exposure", type=int, default=256)
    ap.add_argument("--max-forward-budget", type=int, default=0,
                    help="0 = unlimited")
    ap.add_argument("--output", default="")
    args = ap.parse_args()

    chunks_dir = os.path.join(args.chunks_root, args.run_id)
    per_core, labels = _load_chunks_and_labels(chunks_dir)
    if not per_core:
        print(f"[eval_fixed_chunk] no chunks under {chunks_dir}", flush=True)
        return 1

    if args.dry_predictor:
        predictor = LabelOraclePredictor(labels)
    elif args.ckpt:
        predictor = ModelPredictor(args.ckpt)
    else:
        raise SystemExit("either --dry-predictor or --ckpt is required")

    scheduler = EpsilonResidentScheduler(
        chunks_by_core=per_core,   # dict[int, list[MacroChunkView]] — duck-typed
        predictor=predictor,
        epsilon=float(args.epsilon),
        max_forward_budget=int(args.max_forward_budget) or None,
        max_resident_exposure=int(args.max_resident_exposure),
        trace_id=str(args.run_id),
    )
    t0 = time.time()
    samples = scheduler.run()
    dt = time.time() - t0
    summary = _summarize(samples, per_core, labels, scheduler)
    summary.update({
        "run_id": args.run_id,
        "chunks_dir": chunks_dir,
        "epsilon": float(args.epsilon),
        "max_resident_exposure": int(args.max_resident_exposure),
        "elapsed_s": float(dt),
        "dry_predictor": bool(args.dry_predictor),
    })
    print(json.dumps({k: v for k, v in summary.items() if k != "samples"}, indent=2),
          flush=True)
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w") as fh:
            json.dump(summary, fh, indent=2)
        print(f"[eval_fixed_chunk] wrote {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
