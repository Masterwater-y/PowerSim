"""Pre-training identifiability and memorization gates for v29."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import torch

from ..utils.config import TCSimConfig
from .dataset import V29GlobalTimeDataset, V29TraceStore, collate_v29_sequences
from .losses import compute_v29_losses
from .model import build_model


VISIBLE_CATEGORICAL_KEYS = (
    "per_uop_fields", "dynamic_uop_fields", "valid_uop_mask",
)
VISIBLE_FLOAT_KEYS = (
    "chunk_summary", "relation_features", "uarch_features", "state_features",
)


@dataclass
class _Moments:
    count: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, value: float) -> None:
        value = float(value)
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)

    @property
    def variance(self) -> float:
        return self.m2 / self.count if self.count else float("nan")


class _SignatureGroups:
    def __init__(self, horizons: Sequence[float]) -> None:
        self.horizons = tuple(float(value) for value in horizons)
        self.groups: Dict[bytes, Dict[str, Any]] = {}
        self.rows = 0

    def add(self, signature: bytes, head_log: float, mean_log: float, progress: Sequence[float]) -> None:
        group = self.groups.get(signature)
        if group is None:
            group = {
                "head_log": _Moments(),
                "mean_log": _Moments(),
                "progress": [_Moments() for _ in self.horizons],
            }
            self.groups[signature] = group
        group["head_log"].add(head_log)
        group["mean_log"].add(mean_log)
        for moment, value in zip(group["progress"], progress):
            moment.add(float(value))
        self.rows += 1

    def summary(self) -> Dict[str, Any]:
        duplicate = [
            value for value in self.groups.values()
            if value["head_log"].count >= 2
        ]
        duplicate_rows = sum(value["head_log"].count for value in duplicate)

        def weighted_variance(name: str) -> float:
            denominator = sum(value[name].count for value in duplicate)
            return (
                sum(value[name].variance * value[name].count for value in duplicate)
                / denominator if denominator else float("nan")
            )

        progress = {}
        for index, horizon in enumerate(self.horizons):
            denominator = sum(value["progress"][index].count for value in duplicate)
            variance = (
                sum(
                    value["progress"][index].variance
                    * value["progress"][index].count
                    for value in duplicate
                ) / denominator if denominator else float("nan")
            )
            progress[str(horizon)] = {
                "within_signature_variance": variance,
                "irreducible_rmse": math.sqrt(max(0.0, variance))
                if math.isfinite(variance) else float("nan"),
            }
        head_variance = weighted_variance("head_log")
        mean_variance = weighted_variance("mean_log")
        return {
            "rows": self.rows,
            "unique_signatures": len(self.groups),
            "duplicate_groups": len(duplicate),
            "duplicate_rows": duplicate_rows,
            "duplicate_row_coverage": duplicate_rows / max(1, self.rows),
            "head_log_time_within_signature_variance": head_variance,
            "head_log_time_irreducible_rmse": (
                math.sqrt(max(0.0, head_variance))
                if math.isfinite(head_variance) else float("nan")
            ),
            "mean_log_time_within_signature_variance": mean_variance,
            "mean_log_time_irreducible_rmse": (
                math.sqrt(max(0.0, mean_variance))
                if math.isfinite(mean_variance) else float("nan")
            ),
            "progress": progress,
        }


def _update_hash(digest: Any, array: np.ndarray) -> None:
    contiguous = np.ascontiguousarray(array)
    digest.update(str(contiguous.dtype).encode("ascii"))
    digest.update(np.asarray(contiguous.shape, dtype=np.int64).tobytes())
    digest.update(contiguous.tobytes())


def _row_digest(
    context: Mapping[str, Any], row: int, *, near: bool,
    near_prefix_tokens: int, near_float_step: float,
) -> bytes:
    digest = hashlib.sha256()
    for key in VISIBLE_CATEGORICAL_KEYS:
        array = context[key][row].numpy()
        if near and array.ndim >= 1:
            array = array[:near_prefix_tokens]
        _update_hash(digest, array)
    for key in VISIBLE_FLOAT_KEYS:
        array = context[key][row].numpy().astype(np.float32, copy=False)
        if near:
            array = np.rint(array / near_float_step).astype(np.int32)
        _update_hash(digest, array)
    return digest.digest()


def _target_signature(row_digests: Sequence[bytes], target: int) -> bytes:
    digest = hashlib.sha256()
    digest.update(row_digests[target])
    digest.update(len(row_digests).to_bytes(4, "little"))
    for value in sorted(row_digests):
        digest.update(value)
    return digest.digest()


def visible_signature_audit(
    sources: Sequence[Any],
    *,
    max_samples_per_trace: int = 2000,
    near_prefix_tokens: int = 32,
    near_float_step: float = 0.05,
) -> Dict[str, Any]:
    """Measure label variance for exact and explicitly defined near signatures."""
    if near_prefix_tokens <= 0 or near_float_step <= 0:
        raise ValueError("near signature parameters must be positive")
    exact_groups: Optional[_SignatureGroups] = None
    near_groups: Optional[_SignatureGroups] = None
    trace_reports = []
    started = time.perf_counter()
    for source in sources:
        cache_dir = str(source if isinstance(source, str) else source["cache_dir"])
        store = V29TraceStore(cache_dir)
        if exact_groups is None:
            exact_groups = _SignatureGroups(store.horizons)
            near_groups = _SignatureGroups(store.horizons)
        elif tuple(store.horizons) != exact_groups.horizons:
            raise RuntimeError("visible-signature audit requires identical horizons")
        maximum = int(max_samples_per_trace)
        if maximum <= 0 or len(store) <= maximum:
            indices = list(range(len(store)))
        else:
            indices = sorted({
                int(round(index * (len(store) - 1) / max(1, maximum - 1)))
                for index in range(maximum)
            })
        rows = 0
        for sample_index in indices:
            context = store.context_at(sample_index)
            exact_digests = [
                _row_digest(
                    context, row, near=False,
                    near_prefix_tokens=near_prefix_tokens,
                    near_float_step=near_float_step,
                )
                for row in range(int(context["core_slots"].shape[0]))
            ]
            near_digests = [
                _row_digest(
                    context, row, near=True,
                    near_prefix_tokens=near_prefix_tokens,
                    near_float_step=near_float_step,
                )
                for row in range(int(context["core_slots"].shape[0]))
            ]
            target_time = context["commit_time_target"].numpy()
            valid = context["valid_uop_mask"].numpy().astype(bool)
            progress = context["progress_target"].numpy()
            for row in range(len(exact_digests)):
                values = np.log1p(target_time[row, valid[row]])
                head_log = float(values[0])
                mean_log = float(values.mean())
                exact_groups.add(
                    _target_signature(exact_digests, row),
                    head_log, mean_log, progress[row],
                )
                near_groups.add(
                    _target_signature(near_digests, row),
                    head_log, mean_log, progress[row],
                )
                rows += 1
        trace_reports.append({
            "trace_id": store.trace_id,
            "cache_dir": store.cache_dir,
            "samples": len(indices),
            "active_core_rows": rows,
        })
    if exact_groups is None or near_groups is None:
        raise RuntimeError("visible-signature audit received no sources")
    return {
        "schema_version": "tcsim-v29-visible-signature-audit-1",
        "exact": exact_groups.summary(),
        "near": near_groups.summary(),
        "near_signature_contract": {
            "categorical_prefix_tokens": int(near_prefix_tokens),
            "floating_quantization_step": float(near_float_step),
            "target_row_plus_sorted_peer_multiset": True,
            "core_placement_order_invariant": True,
        },
        "traces": trace_reports,
        "elapsed_s": time.perf_counter() - started,
    }


CONTROL_KEYS = {
    "sample_ptr", "sequence_ptr", "row_sequence", "row_sequence_step",
    "core_slots", "cursors", "sample_indices", "horizons",
}


def _batch_to_device(batch: Mapping[str, Any], device: torch.device) -> Dict[str, Any]:
    return {
        key: (
            value if key in CONTROL_KEYS else value.to(device)
        ) if isinstance(value, torch.Tensor) else value
        for key, value in batch.items()
    }


def single_sample_overfit(
    source: Any,
    config: TCSimConfig,
    *,
    sample_index: int = 0,
    steps: int = 300,
    learning_rate: float = 3.0e-4,
    device: str = "cuda",
    tiny_model: bool = False,
) -> Dict[str, Any]:
    """Try to memorize one oracle-aligned sequence as an architecture gate."""
    dataset = V29GlobalTimeDataset(
        [source],
        sequence_length=int(config.chunk.get("sequence_length", 4)),
        sequence_stride=int(config.chunk.get("sequence_length", 4)),
    )
    item = dataset[int(sample_index) % len(dataset)]
    batch = collate_v29_sequences([item])
    target = torch.device(device)
    if target.type == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested for single-sample overfit but is unavailable")
    model_config = dict(config.model)
    model_config["dropout"] = 0.0
    if tiny_model:
        model_config.update({
            "d_field": 8,
            "d_dynamic_field": 4,
            "d_static": 32,
            "d_dyn": 64,
            "n_dyn_heads": 4,
            "n_dyn_layers": 2,
            "ffn_dim": 128,
            "sdpa_backend": "math",
        })
    model = build_model(model_config, dataset.stores[0].horizons).to(target)
    batch = _batch_to_device(batch, target)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=float(learning_rate), weight_decay=0.0,
    )
    weights = dict(config.train.get("loss_weights", {}))
    history = []
    started = time.perf_counter()
    initial = None
    best = float("inf")
    final_losses = None
    model.train()
    for step in range(max(1, int(steps))):
        optimizer.zero_grad(set_to_none=True)
        predictions = model(batch)
        losses = compute_v29_losses(
            predictions,
            batch,
            weights=weights,
            time_beta=float(config.train.get("time_huber_beta", 0.2)),
            progress_count_beta=float(config.train.get("progress_count_beta", 8.0)),
            branch_count_beta=float(config.train.get("branch_count_beta", 1.0)),
        )
        losses.total.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 10.0)
        optimizer.step()
        value = float(losses.total.detach())
        if initial is None:
            initial = value
        best = min(best, value)
        final_losses = losses
        if step == 0 or (step + 1) % max(1, int(steps) // 20) == 0:
            history.append({
                "step": step + 1,
                "total": value,
                "commit_log_mae": float(losses.commit_log_mae.detach()),
                "progress_mae": float(losses.progress_mae.detach()),
                "branch_brier": float(losses.branch_brier.detach()),
            })
    assert initial is not None and final_losses is not None
    final = float(final_losses.total.detach())
    ratio = final / max(initial, 1.0e-12)
    commit_log_mae = float(final_losses.commit_log_mae.detach())
    progress_mae = float(final_losses.progress_mae.detach())
    passed = bool(ratio <= 0.10 and commit_log_mae <= 0.10 and progress_mae <= 2.0)
    return {
        "schema_version": "tcsim-v29-single-sample-overfit-1",
        "trace_id": dataset.stores[0].trace_id,
        "sequence_index": int(sample_index) % len(dataset),
        "sequence_length": dataset.sequence_length,
        "active_core_rows": int(batch["valid_uop_mask"].shape[0]),
        "valid_uops": int(batch["valid_uop_mask"].sum().item()),
        "branch_tokens": int((batch["branch_mask"] & batch["valid_uop_mask"]).sum().item()),
        "steps": max(1, int(steps)),
        "tiny_model": bool(tiny_model),
        "initial_loss": initial,
        "best_loss": best,
        "final_loss": final,
        "final_to_initial_ratio": ratio,
        "final_commit_log_mae": commit_log_mae,
        "final_progress_mae": progress_mae,
        "final_branch_brier": float(final_losses.branch_brier.detach()),
        "pass": passed,
        "gate": {
            "final_to_initial_ratio_max": 0.10,
            "commit_log_mae_max": 0.10,
            "progress_mae_max": 2.0,
        },
        "history": history,
        "elapsed_s": time.perf_counter() - started,
    }
