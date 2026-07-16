"""Losses for monotonic common-time prefix progress."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Tuple

import torch
import torch.nn.functional as F


def _masked_mean(values: torch.Tensor, mask: torch.Tensor) -> torch.Tensor:
    weight = mask.to(values.dtype)
    return (values * weight).sum() / weight.sum().clamp(min=1.0)


def _zero(predictions: Mapping[str, torch.Tensor]) -> torch.Tensor:
    return predictions["commit_time"].sum() * 0.0


@dataclass
class V29LossOutputs:
    total: torch.Tensor
    commit_time: torch.Tensor
    prefix_bce: torch.Tensor
    progress_count: torch.Tensor
    cumulative: torch.Tensor
    branch_token: torch.Tensor
    branch_count: torch.Tensor
    commit_log_mae: torch.Tensor
    progress_mae: torch.Tensor
    progress_signed_bias: torch.Tensor
    branch_brier: torch.Tensor
    n_valid_uops: int
    n_branch_tokens: int
    monotonic_violations: int


def _cumulative_loss(
    predictions: Mapping[str, torch.Tensor],
    batch: Mapping[str, torch.Tensor],
) -> torch.Tensor:
    """Penalize same-sign progress drift over contiguous oracle sequences."""
    sample_period = float(batch["sample_period_cycles"])
    horizons = batch["horizons"].to(predictions["progress"].device)
    horizon_index = int(torch.argmin((horizons - sample_period).abs()).item())
    if abs(float(horizons[horizon_index]) - sample_period) > 1e-4:
        raise RuntimeError(
            "v29 cumulative loss requires sample_period_cycles in horizon set"
        )
    pred = predictions["progress"][:, horizon_index]
    target = batch["progress_target"][:, horizon_index]
    sequence = batch["row_sequence"]
    core_slot = batch["core_slots"]
    losses = []
    keys = torch.stack([sequence, core_slot], dim=1)
    # Sequence/core metadata is control-only.  The number of rows is at most
    # batch_sequences * sequence_length * 32, so a short Python grouping loop
    # is cheaper and clearer than a sparse scatter structure.
    groups: Dict[Tuple[int, int], list] = {}
    for row, pair in enumerate(keys.detach().cpu().tolist()):
        groups.setdefault((int(pair[0]), int(pair[1])), []).append(row)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        index = torch.tensor(rows, dtype=torch.long, device=pred.device)
        signed = pred.index_select(0, index).sum() - target.index_select(0, index).sum()
        denom = max(1.0, 256.0 * len(rows))
        losses.append(F.smooth_l1_loss(
            signed / denom,
            torch.zeros_like(signed),
            beta=0.02,
            reduction="mean",
        ))
    return torch.stack(losses).mean() if losses else pred.sum() * 0.0


def compute_v29_losses(
    predictions: Mapping[str, torch.Tensor],
    batch: Mapping[str, torch.Tensor],
    *,
    weights: Mapping[str, float],
    time_beta: float = 0.2,
    progress_count_beta: float = 8.0,
    branch_count_beta: float = 1.0,
) -> V29LossOutputs:
    valid = batch["valid_uop_mask"].bool()
    true_time = batch["commit_time_target"].clamp(min=0.0)
    pred_time = predictions["commit_time"].clamp(min=0.0)
    time_element = F.smooth_l1_loss(
        torch.log1p(pred_time),
        torch.log1p(true_time),
        beta=float(time_beta),
        reduction="none",
    )
    time_loss = _masked_mean(time_element, valid)
    time_log_mae = _masked_mean(
        (torch.log1p(pred_time) - torch.log1p(true_time)).abs(), valid,
    )

    prefix_mask = valid.unsqueeze(-1).expand_as(batch["prefix_target"])
    prefix_element = F.binary_cross_entropy_with_logits(
        predictions["commit_logits"],
        batch["prefix_target"],
        reduction="none",
    )
    prefix_bce = _masked_mean(prefix_element, prefix_mask)
    progress_error = predictions["progress"] - batch["progress_target"]
    progress_count = F.smooth_l1_loss(
        predictions["progress"],
        batch["progress_target"],
        beta=float(progress_count_beta),
        reduction="mean",
    ) / 256.0
    progress_mae = progress_error.abs().mean()
    progress_signed_bias = progress_error.mean()

    cumulative = _cumulative_loss(predictions, batch)

    branch_mask = batch["branch_mask"].bool() & valid
    if branch_mask.any():
        branch_element = F.binary_cross_entropy_with_logits(
            predictions["branch_miss_logit"],
            batch["branch_miss_target"],
            reduction="none",
        )
        branch_token = _masked_mean(branch_element, branch_mask)
        branch_brier = _masked_mean(
            (
                predictions["branch_miss_probability"]
                - batch["branch_miss_target"]
            ).square(),
            branch_mask,
        )
    else:
        branch_token = _zero(predictions)
        branch_brier = _zero(predictions)

    branch_weight = batch["branch_mask"].to(
        predictions["commit_probability"].dtype
    ).unsqueeze(-1)
    predicted_misses = (
        predictions["commit_probability"]
        * predictions["branch_miss_probability"].unsqueeze(-1)
        * branch_weight
    ).sum(dim=1)
    true_misses = (
        batch["prefix_target"]
        * batch["branch_miss_target"].unsqueeze(-1)
        * branch_weight
    ).sum(dim=1)
    branch_count = F.smooth_l1_loss(
        predicted_misses,
        true_misses,
        beta=float(branch_count_beta),
        reduction="mean",
    ) / 32.0

    total = (
        float(weights.get("commit_time", 1.0)) * time_loss
        + float(weights.get("prefix_bce", 0.5)) * prefix_bce
        + float(weights.get("progress_count", 0.5)) * progress_count
        + float(weights.get("cumulative", 0.25)) * cumulative
        + float(weights.get("branch_token", 0.1)) * branch_token
        + float(weights.get("branch_count", 0.1)) * branch_count
    )
    tau = predictions["commit_time"]
    violations = int(((tau[:, 1:] < tau[:, :-1]) & valid[:, 1:]).sum().item())
    return V29LossOutputs(
        total=total,
        commit_time=time_loss,
        prefix_bce=prefix_bce,
        progress_count=progress_count,
        cumulative=cumulative,
        branch_token=branch_token,
        branch_count=branch_count,
        commit_log_mae=time_log_mae,
        progress_mae=progress_mae,
        progress_signed_bias=progress_signed_bias,
        branch_brier=branch_brier,
        n_valid_uops=int(valid.sum().item()),
        n_branch_tokens=int(branch_mask.sum().item()),
        monotonic_violations=violations,
    )
