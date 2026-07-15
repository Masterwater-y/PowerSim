"""Per-core identifiable losses for oracle-context fixed-chunk training."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import torch


def huber(x: torch.Tensor, delta: float) -> torch.Tensor:
    abs_x = x.abs()
    d = torch.as_tensor(delta, device=x.device, dtype=x.dtype)
    quadratic = torch.minimum(abs_x, d)
    linear = abs_x - quadratic
    return 0.5 * quadratic ** 2 + d * linear


@dataclass
class LossOutputs:
    total: torch.Tensor
    log_cpi: torch.Tensor
    centered: torch.Tensor
    branch_miss: torch.Tensor
    prefix: torch.Tensor
    endpoint: torch.Tensor
    n_committed: int
    n_spread_samples: int
    true_spread: torch.Tensor
    pred_spread: torch.Tensor


def _sample_ranges(sample_ptr: torch.Tensor) -> List[Tuple[int, int]]:
    return [
        (int(sample_ptr[i]), int(sample_ptr[i + 1]))
        for i in range(int(sample_ptr.shape[0]) - 1)
    ]


def _absolute_loss(
    pred: torch.Tensor,
    true: torch.Tensor,
    mask: torch.Tensor,
    group_id: torch.Tensor,
    sample_ptr: torch.Tensor,
    beta: float,
) -> torch.Tensor:
    """Macro-average samples after collapsing indistinguishable cores.

    Cores in one functional equivalence group receive the group's mean target;
    group size remains the weight so the absolute objective still represents a
    core average.  This removes contradictory gradients caused by random
    timing symmetry breaking without hiding hot/cold population ratios.
    """
    losses: List[torch.Tensor] = []
    for start, end in _sample_ranges(sample_ptr):
        m = mask[start:end].bool()
        if m.any():
            p = pred[start:end][m]
            y = true[start:end][m]
            gid = group_id[start:end][m]
            group_loss = p.new_zeros(())
            group_count = p.new_zeros(())
            for value in torch.unique(gid):
                gm = gid == value
                count = gm.sum().to(p.dtype)
                group_loss = group_loss + count * huber(
                    p[gm].mean() - y[gm].mean(), beta,
                )
                group_count = group_count + count
            losses.append(group_loss / group_count.clamp(min=1.0))
    return torch.stack(losses).mean() if losses else pred.new_zeros(())


def _centered_loss(
    pred: torch.Tensor,
    true: torch.Tensor,
    mask: torch.Tensor,
    exposure_weight: torch.Tensor,
    group_id: torch.Tensor,
    sample_ptr: torch.Tensor,
    *,
    beta: float,
    spread_threshold: float,
) -> Tuple[torch.Tensor, int, torch.Tensor, torch.Tensor]:
    centered_num = pred.new_zeros(())
    total_weight = pred.new_zeros(())
    true_spreads: List[torch.Tensor] = []
    pred_spreads: List[torch.Tensor] = []
    n_valid = 0

    for start, end in _sample_ranges(sample_ptr):
        m = mask[start:end].bool()
        if int(m.sum()) < 2:
            continue
        p_core = pred[start:end][m]
        y_core = true[start:end][m]
        raw_w = exposure_weight[start:end][m].clamp(min=0.0)
        gid = group_id[start:end][m]
        if float(raw_w.sum().detach()) <= 0:
            raw_w = torch.ones_like(raw_w)
        unique_groups = torch.unique(gid)
        if int(unique_groups.numel()) < 2:
            # A permutation-equivariant model must emit the same value for an
            # exactly symmetric context.  Do not ask the centered loss to
            # invent a core identity that is absent from functional input.
            continue
        group_pred: List[torch.Tensor] = []
        group_true: List[torch.Tensor] = []
        group_weight: List[torch.Tensor] = []
        for value in unique_groups:
            gm = gid == value
            gw = raw_w[gm]
            denom = gw.sum().clamp(min=1e-8)
            group_pred.append((p_core[gm] * gw).sum() / denom)
            group_true.append((y_core[gm] * gw).sum() / denom)
            group_weight.append(denom)
        p = torch.stack(group_pred)
        y = torch.stack(group_true)
        raw_group_w = torch.stack(group_weight)
        w = raw_group_w / raw_group_w.sum().clamp(min=1e-8)
        p_mean = (p * w).sum()
        y_mean = (y * w).sum()
        p_center = p - p_mean
        y_center = y - y_mean
        true_std = torch.sqrt((w * y_center.square()).sum().clamp(min=0.0))
        if float(true_std.detach()) < float(spread_threshold):
            continue
        pred_std = torch.sqrt((w * p_center.square()).sum().clamp(min=0.0))

        # Continuous gate: high-spread contexts receive more signal but are
        # capped so a few pathological samples cannot dominate.
        gate = (true_std.detach() / max(1e-8, spread_threshold)).clamp(max=3.0)
        # The trace-balanced sampler already equalizes trace mass.  Do not let
        # a 32-core context gain an additional implicit core-count multiplier.
        sample_weight = gate
        centered_sample = (w * huber(p_center - y_center, beta)).sum()

        centered_num = centered_num + sample_weight * centered_sample
        total_weight = total_weight + sample_weight
        true_spreads.append(true_std.detach())
        pred_spreads.append(pred_std.detach())
        n_valid += 1

    if n_valid == 0:
        zero = pred.new_zeros(())
        return zero, 0, zero, zero
    denom = total_weight.clamp(min=1e-8)
    return (
        centered_num / denom,
        n_valid,
        torch.stack(true_spreads).mean(),
        torch.stack(pred_spreads).mean(),
    )


def _branch_miss_loss(
    logits: torch.Tensor,
    opportunities: torch.Tensor,
    misses: torch.Tensor,
    mask: torch.Tensor,
    sample_ptr: torch.Tensor,
) -> torch.Tensor:
    """Macro-average binomial NLL for conditional-branch misses.

    ``opportunities`` is known from the functional chunk.  Miss outcomes are
    targets only, never model inputs.  Per-sample normalization prevents a
    branch-dense workload from dominating the trace-balanced CPI objective.
    """
    losses: List[torch.Tensor] = []
    for start, end in _sample_ranges(sample_ptr):
        m = mask[start:end].bool() & (opportunities[start:end] > 0)
        if not m.any():
            continue
        opp = opportunities[start:end][m].clamp(min=1.0)
        rate = (misses[start:end][m] / opp).clamp(min=0.0, max=1.0)
        nll = torch.nn.functional.binary_cross_entropy_with_logits(
            logits[start:end][m], rate, reduction="none",
        )
        losses.append((nll * opp).sum() / opp.sum().clamp(min=1.0))
    return torch.stack(losses).mean() if losses else logits.new_zeros(())


def compute_losses(
    preds: Dict[str, torch.Tensor],
    batch: Dict[str, torch.Tensor],
    *,
    weights: Dict[str, float],
    huber_delta_log: float,
    prefix_lens: List[int],
    centered_spread_threshold: float = 0.10,
    **_unused,
) -> LossOutputs:
    """Compute v27.4 absolute/centered CPI plus branch-miss losses.

    Prefix/endpoint are deliberately zero until a contiguous sequence batcher
    supplies an explicit ``sequence_ptr`` contract.  This is safer than the old
    implementation, which assembled fake trajectories from shuffled samples.
    """
    pred = preds["log_cpi"]
    true = batch["log_cpi"]
    abs_mask = batch["label_mask"].bool()
    context_mask = batch["context_label_mask"].bool()
    exposure_weight = batch["context_weight"]
    sample_ptr = batch["sample_ptr"]
    group_id = batch.get("functional_group_id")
    if group_id is None:
        # Backward-compatible unit-test/legacy path: each row is identifiable.
        group_id = torch.arange(pred.shape[0], device=pred.device, dtype=torch.long)
    n_committed = int(abs_mask.sum().item())
    if n_committed == 0:
        zero = pred.new_zeros(())
        return LossOutputs(zero, zero, zero, zero, zero, zero, 0, 0, zero, zero)

    l_abs = _absolute_loss(
        pred, true, abs_mask, group_id, sample_ptr, huber_delta_log,
    )
    l_center, n_spread, true_spread, pred_spread = _centered_loss(
        pred,
        true,
        context_mask,
        exposure_weight,
        group_id,
        sample_ptr,
        beta=huber_delta_log,
        spread_threshold=centered_spread_threshold,
    )
    zero = pred.new_zeros(())
    branch_logits = preds.get("branch_miss_logit")
    if branch_logits is None:
        l_branch = zero
    else:
        l_branch = _branch_miss_loss(
            branch_logits,
            batch.get("branch_opportunities", torch.zeros_like(pred)),
            batch.get("branch_misses", torch.zeros_like(pred)),
            batch.get("branch_label_mask", abs_mask).bool(),
            sample_ptr,
        )
    l_prefix = zero
    l_endpoint = zero
    total = (
        float(weights.get("abs_log_cpi", weights.get("log_cpi", 1.0))) * l_abs
        + float(weights.get("centered", 0.5)) * l_center
        + float(weights.get("branch_miss", 0.10)) * l_branch
        + float(weights.get("prefix", 0.0)) * l_prefix
        + float(weights.get("endpoint", 0.0)) * l_endpoint
    )
    return LossOutputs(
        total=total,
        log_cpi=l_abs.detach(),
        centered=l_center.detach(),
        branch_miss=l_branch.detach(),
        prefix=l_prefix.detach(),
        endpoint=l_endpoint.detach(),
        n_committed=n_committed,
        n_spread_samples=n_spread,
        true_spread=true_spread,
        pred_spread=pred_spread,
    )
