"""loss.py — label 变换 + 多任务回归 loss（uncertainty/fixed weighting）+ 物理 invariance。

回归空间（见 regression_head.KEY_SPACE）：
  logratio : pred 是 log(y) 的线性输出，target = log(y)            -> Huber
  rat01    : pred 已 sigmoid ∈[0,1]，target = y ∈[0,1]              -> Huber
  logcount : pred 是 log1p(count) 的线性输出，target = log1p(count) -> Huber
  direct   : pred 线性，target = y                                  -> Huber

uncertainty weighting：每 key 一个可学习 log_var σ_k，
  L = Σ_k exp(-σ_k) L_k + σ_k

per-key huber delta（按各空间的"业务可接受误差"取值）：
  logratio (CPI) : 0.1   ≈ ±10% 相对误差
  rat01          : 0.05  ≈ ±5pp 绝对误差
  logcount       : 0.5   ≈ ±65% count 相对误差
  direct         : 1.0   保留原值

L_cycles = Huber(log(sum_i CPI_uop_pred_i·uops_i), log(sum_i cycles_label_i), δ=0.1)
  - 显式监督 cycles，方案C / OnlineQuotaPlanner 的 T_end 反推直接相关
  - uncertainty 模式下有单独 log_var σ_cyc；fixed 模式下用显式固定权重
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from model.regression_head import PMU_KEYS, KEY_SPACE, K

EPS = 1e-6
LOG_PRED_MIN = -20.0
LOG_PRED_MAX = 20.0
LOG_COUNT_MAX = 20.0
# Avoid sqrt(0) in spread calibration: forward is finite at zero variance, but
# backward through sqrt can produce inf/NaN gradients when all core predictions
# are identical, which is common early in training.
SPREAD_VAR_EPS = 1e-4
DENOM_KEYS = [
    "branch_count",
    "loads",
    "stores",
    "atomics",
    "mem_ops",
    "page_touches",
]
DENOM_IDX = {k: i for i, k in enumerate(DENOM_KEYS)}

DEFAULT_HUBER_DELTA = {
    "logratio": 0.1,
    "rat01": 0.05,
    "logcount": 0.5,
    "direct": 1.0,
}


def transform_label(label: torch.Tensor) -> torch.Tensor:
    """把原始 PMU 标签变换到各自回归空间。label: [...,K] -> [...,K]。"""
    out = torch.empty_like(label)
    for i, k in enumerate(PMU_KEYS):
        sp = KEY_SPACE[k]
        y = label[..., i]
        if sp == "logratio":
            out[..., i] = torch.log(y.clamp(min=EPS))
        elif sp == "rat01":
            out[..., i] = y.clamp(0.0, 1.0)
        elif sp == "logcount":
            out[..., i] = torch.log1p(y.clamp(min=0.0))
        else:  # direct
            out[..., i] = y
    return out


def invert_pred(pred: torch.Tensor) -> torch.Tensor:
    """把模型 raw 输出逆变换回原始 PMU 量纲（推理用）。"""
    out = torch.empty_like(pred)
    for i, k in enumerate(PMU_KEYS):
        sp = KEY_SPACE[k]
        p = pred[..., i]
        if sp == "logratio":
            out[..., i] = torch.exp(p.clamp(LOG_PRED_MIN, LOG_PRED_MAX))
        elif sp == "rat01":
            out[..., i] = p.clamp(0.0, 1.0)
        elif sp == "logcount":
            out[..., i] = torch.expm1(p.clamp(max=LOG_COUNT_MAX)).clamp(min=0.0)
        else:
            out[..., i] = p
    return out


class PMULoss(nn.Module):
    def __init__(self, lambda_inv: float = 0.1,
                 lambda_phys: float = 0.05,
                 lambda_delta: float = 0.0,
                 lambda_cycles_window: float = 1.0,
                 lambda_rank: float = 0.0,
                 lambda_spread: float = 0.0,
                 lambda_slowest: float = 0.0,
                 lambda_fastest: float = 0.0,
                 rank_gap: float = 0.10,
                 rank_tau: float = 0.10,
                 spread_min_std: float = 0.03,
                 spread_ref: float = 0.10,
                 spread_weight_max: float = 3.0,
                 spread_weight_min: float = 0.25,
                 spread_loss_mode: str = "gated",
                 loss_weight_mode: str = "uncertainty",
                 lambda_cpi_abs: float = 1.0,
                 lambda_aux_pmu: float = 1.0,
                 huber_delta: dict | float | None = None,
                 cycles_delta: float = 0.1):
        super().__init__()
        # Scheme A 初值偏 cpi_uop：log_var=-1 ≈ 权重 e≈2.72x。
        # miss 绝对计数在 log1p 空间回归，初始降权，避免 count 头在早期淹没 CPI。
        init_lv = torch.zeros(K)
        idx = {k: i for i, k in enumerate(PMU_KEYS)}
        if "cpi_uop" in idx:
            init_lv[idx["cpi_uop"]] = -1.0
        for k, i in idx.items():
            if KEY_SPACE[k] == "logcount":
                init_lv[i] = 1.0
        self.log_var = nn.Parameter(init_lv)
        self.log_var_cycles = nn.Parameter(torch.tensor(-1.0))
        self.lambda_inv = lambda_inv
        self.lambda_phys = lambda_phys
        self.lambda_delta = float(lambda_delta)
        self.lambda_cycles_window = float(lambda_cycles_window)
        self.lambda_rank = float(lambda_rank)
        self.lambda_spread = float(lambda_spread)
        self.lambda_slowest = float(lambda_slowest)
        self.lambda_fastest = float(lambda_fastest)
        self.rank_gap = float(rank_gap)
        self.rank_tau = max(float(rank_tau), 1e-6)
        self.spread_min_std = float(spread_min_std)
        self.spread_ref = max(float(spread_ref), 1e-6)
        self.spread_weight_max = max(float(spread_weight_max), 0.0)
        self.spread_weight_min = max(float(spread_weight_min), 0.0)
        if spread_loss_mode not in {"gated", "soft"}:
            raise ValueError(f"unknown spread_loss_mode={spread_loss_mode!r}")
        self.spread_loss_mode = str(spread_loss_mode)
        if loss_weight_mode not in {"uncertainty", "fixed"}:
            raise ValueError(f"unknown loss_weight_mode={loss_weight_mode!r}")
        self.loss_weight_mode = str(loss_weight_mode)
        self.lambda_cpi_abs = float(lambda_cpi_abs)
        self.lambda_aux_pmu = float(lambda_aux_pmu)
        if self.loss_weight_mode == "fixed":
            self.log_var.requires_grad_(False)
            self.log_var_cycles.requires_grad_(False)

        if huber_delta is None:
            huber_delta = DEFAULT_HUBER_DELTA
        if isinstance(huber_delta, (int, float)):
            huber_delta = {sp: float(huber_delta)
                           for sp in DEFAULT_HUBER_DELTA}
        deltas = torch.tensor([huber_delta[KEY_SPACE[k]] for k in PMU_KEYS],
                              dtype=torch.float32)
        self.register_buffer("huber_delta_per_k", deltas)
        self.cycles_delta = cycles_delta
        self.idx = {k: i for i, k in enumerate(PMU_KEYS)}

    def forward(self, pred: torch.Tensor, label: torch.Tensor,
                core_mask: torch.Tensor,
                uops: torch.Tensor | None = None,
                denoms: torch.Tensor | None = None):
        """pred/label: [B,nc,K]，core_mask: [B,nc]，uops: [B,nc]。
        pred 已对 rat01 做 sigmoid。"""
        tgt = transform_label(label)
        m = core_mask.to(pred.dtype).unsqueeze(-1)                   # [B,nc,1]

        # per-key huber loss with per-space delta
        deltas = self.huber_delta_per_k.to(pred.dtype)               # [K]
        e = pred - tgt                                               # [B,nc,K]
        ae = e.abs()
        per_k = torch.where(
            ae <= deltas,
            0.5 * e * e,
            deltas * (ae - 0.5 * deltas),
        )                                                            # [B,nc,K]
        denom = m.sum().clamp(min=1.0)
        per_k = (per_k * m).sum(dim=(0, 1)) / denom                 # [K]
        cpi_idx = self.idx["cpi_uop"]
        aux_idxs = [i for i in range(K) if i != cpi_idx]
        aux_pmu = (
            per_k[aux_idxs].mean() if aux_idxs else pred.new_zeros(())
        )
        if self.loss_weight_mode == "uncertainty":
            lv = self.log_var.to(per_k.dtype)
            weighted = (torch.exp(-lv) * per_k + lv).sum()
        else:
            weighted = (
                self.lambda_cpi_abs * per_k[cpi_idx]
                + self.lambda_aux_pmu * aux_pmu
            )

        # L_cycles：窗口级 log(sum cycles) Huber，独立 log_var。
        l_cyc = pred.new_zeros(())
        if uops is not None:
            cm = core_mask.to(pred.dtype)
            uops_t = uops.clamp(min=0.0).to(pred.dtype) * cm
            pred_cpi = torch.exp(
                pred[..., cpi_idx].clamp(LOG_PRED_MIN, LOG_PRED_MAX)
            )
            cpi_label = label[..., cpi_idx].clamp(min=EPS).to(pred.dtype)
            pred_cycles = (pred_cpi * uops_t).sum(dim=1)
            label_cycles = (cpi_label * uops_t).sum(dim=1)
            valid = (uops_t.sum(dim=1) > 0) & (label_cycles > 0)
            log_cycles_pred = torch.log(pred_cycles[valid].clamp(min=EPS))
            log_cycles_tgt = torch.log(label_cycles[valid].clamp(min=EPS))
            e_cyc = log_cycles_pred - log_cycles_tgt
            ae_cyc = e_cyc.abs()
            d = self.cycles_delta
            per_cyc = torch.where(
                ae_cyc <= d, 0.5 * e_cyc * e_cyc, d * (ae_cyc - 0.5 * d),
            )
            if per_cyc.numel() > 0:
                l_cyc = per_cyc.mean()
                if self.loss_weight_mode == "uncertainty":
                    lvc = self.log_var_cycles.to(l_cyc.dtype)
                    weighted = weighted + self.lambda_cycles_window * (
                        torch.exp(-lvc) * l_cyc + lvc
                    )
                else:
                    weighted = weighted + self.lambda_cycles_window * l_cyc

        z = pred.new_zeros(())
        delta = self._delta_loss(pred, label, core_mask) if self.lambda_delta else z
        rank, spread, order_acc = self._rank_spread(
            pred, label, core_mask, compute_rank=bool(self.lambda_rank))
        if self.lambda_slowest or self.lambda_fastest:
            slowest, fastest, slowest_acc, fastest_acc = self._extreme_core_loss(
                pred, label, core_mask)
        else:
            slowest = fastest = slowest_acc = fastest_acc = z
        inv = self._invariance(pred, core_mask)
        phys = self._physical_constraints(pred, core_mask, denoms)
        total = weighted
        if self.lambda_inv:
            total = total + self.lambda_inv * inv
        if self.lambda_phys:
            total = total + self.lambda_phys * phys
        if self.lambda_delta:
            total = total + self.lambda_delta * delta
        if self.lambda_rank:
            total = total + self.lambda_rank * rank
        if self.lambda_spread:
            total = total + self.lambda_spread * spread
        if self.lambda_slowest:
            total = total + self.lambda_slowest * slowest
        if self.lambda_fastest:
            total = total + self.lambda_fastest * fastest
        logs = {f"L_{k}": per_k[i].detach() for i, k in enumerate(PMU_KEYS)}
        logs["L_cpi_abs"] = per_k[cpi_idx].detach()
        logs["L_aux_pmu"] = aux_pmu.detach()
        logs["L_cycles"] = l_cyc.detach()
        logs["L_cycles_window"] = l_cyc.detach()
        logs["L_delta"] = delta.detach()
        logs["L_spread"] = spread.detach()
        if self.lambda_rank:
            logs["L_rank"] = rank.detach()
            logs["pairwise_order_acc"] = order_acc.detach()
        if self.lambda_slowest:
            logs["L_slowest"] = slowest.detach()
            logs["slowest_acc"] = slowest_acc.detach()
        if self.lambda_fastest:
            logs["L_fastest"] = fastest.detach()
            logs["fastest_acc"] = fastest_acc.detach()
        logs["L_inv"] = inv.detach()
        logs["L_phys"] = phys.detach()
        logs["loss"] = total.detach()
        return total, logs

    def _log_cpi_deltas(self, pred: torch.Tensor, label: torch.Tensor,
                        core_mask: torch.Tensor):
        cpi_idx = self.idx["cpi_uop"]
        p = pred[..., cpi_idx]
        y = torch.log(label[..., cpi_idx].clamp(min=EPS).to(pred.dtype))
        m = core_mask.to(torch.bool)
        mf = m.to(pred.dtype)
        den = mf.sum(dim=1).clamp(min=1.0)
        p_mean = (p * mf).sum(dim=1) / den
        y_mean = (y * mf).sum(dim=1) / den
        p_delta = (p - p_mean.unsqueeze(1)) * mf
        y_delta = (y - y_mean.unsqueeze(1)) * mf
        active_n = m.sum(dim=1)
        y_var = (((y - y_mean.unsqueeze(1)) ** 2) * mf).sum(dim=1) / (
            mf.sum(dim=1).clamp(min=2.0) - 1.0
        )
        y_std = torch.sqrt(y_var.clamp(min=0.0))
        return p, y, p_delta, y_delta, y_std, active_n, m

    def _spread_weights(self, y_std: torch.Tensor) -> torch.Tensor:
        if self.spread_weight_max <= 0:
            return torch.ones_like(y_std)
        if self.spread_loss_mode == "soft":
            return torch.clamp(
                y_std / self.spread_ref,
                min=self.spread_weight_min,
                max=self.spread_weight_max,
            )
        boost = torch.clamp(y_std / self.spread_ref,
                            min=0.0, max=self.spread_weight_max)
        return 1.0 + boost

    def _delta_loss(self, pred: torch.Tensor, label: torch.Tensor,
                    core_mask: torch.Tensor):
        _p, _y, p_delta, y_delta, y_std, _active_n, m = self._log_cpi_deltas(
            pred, label, core_mask)
        if not m.any():
            return pred.new_zeros(())
        e = p_delta - y_delta
        ae = e.abs()
        d = float(self.huber_delta_per_k[self.idx["cpi_uop"]].item())
        per = torch.where(ae <= d, 0.5 * e * e, d * (ae - 0.5 * d))
        mf = m.to(pred.dtype)
        w = self._spread_weights(y_std).unsqueeze(1)
        return (per * mf * w).sum() / (mf * w).sum().clamp(min=1.0)

    def _rank_spread(self, pred: torch.Tensor, label: torch.Tensor,
                     core_mask: torch.Tensor, compute_rank: bool = True):
        p, y, _p_delta, _y_delta, y_std, active_n, m = self._log_cpi_deltas(
            pred, label, core_mask)
        window_weight = self._spread_weights(y_std)

        rank = pred.new_zeros(())
        order_acc = pred.new_zeros(())
        if compute_rank:
            yi = y.unsqueeze(2)
            yj = y.unsqueeze(1)
            pi = p.unsqueeze(2)
            pj = p.unsqueeze(1)
            dy = yi - yj
            dp = pi - pj
            pair_mask = (
                m.unsqueeze(2)
                & m.unsqueeze(1)
                & (dy.abs() > self.rank_gap)
            )
            # Keep one direction per pair to avoid duplicate gradients/statistics.
            upper = torch.triu(
                torch.ones_like(pair_mask, dtype=torch.bool), diagonal=1)
            pair_mask = pair_mask & upper
            if pair_mask.any():
                sign = dy.sign()
                rank_loss = F.softplus(-(dp * sign) / self.rank_tau)
                pair_w = window_weight.view(-1, 1, 1).to(rank_loss.dtype)
                denom = (
                    pair_mask.to(rank_loss.dtype) * pair_w).sum().clamp(min=1.0)
                rank = (
                    rank_loss * pair_mask.to(rank_loss.dtype) * pair_w
                ).sum() / denom
                order_acc = ((dp * sign) > 0).to(pred.dtype)[pair_mask].mean()

        valid = active_n > 1
        if valid.any():
            mf = m.to(pred.dtype)
            p_mean = (p * mf).sum(dim=1) / mf.sum(dim=1).clamp(min=1.0)
            p_var = (((p - p_mean.unsqueeze(1)) ** 2) * mf).sum(dim=1) / (
                mf.sum(dim=1).clamp(min=2.0) - 1.0
            )
            p_std = torch.sqrt(p_var.clamp(min=0.0) + SPREAD_VAR_EPS)
            if self.spread_loss_mode == "soft":
                spread_mask = valid
            else:
                spread_mask = valid & (y_std > self.spread_min_std)
            if spread_mask.any():
                y_std_safe = torch.sqrt(
                    y_std[spread_mask].pow(2) + SPREAD_VAR_EPS)
                per_spread = F.smooth_l1_loss(
                    torch.log(p_std[spread_mask] + 1e-3),
                    torch.log(y_std_safe + 1e-3),
                    beta=0.1,
                    reduction="none",
                )
                w = window_weight[spread_mask].to(per_spread.dtype)
                spread = (per_spread * w).sum() / w.sum().clamp(min=1.0)
            else:
                spread = pred.new_zeros(())
        else:
            spread = pred.new_zeros(())

        return rank, spread, order_acc

    def _extreme_core_loss(self, pred: torch.Tensor, label: torch.Tensor,
                           core_mask: torch.Tensor):
        """Classify the slowest and fastest core on high-spread windows.

        This is deliberately derived from predicted log-CPI rather than a
        separate classifier head, so it directly sharpens the CPI deltas used by
        the online planner. Low-spread windows are ignored to avoid forcing fake
        fast/slow differences when labels are effectively tied.
        """
        p, y, _p_delta, _y_delta, y_std, active_n, m = self._log_cpi_deltas(
            pred, label, core_mask)
        valid = (active_n > 1) & (y_std > self.spread_min_std)
        if not valid.any():
            z = pred.new_zeros(())
            return z, z, z, z

        neg_inf = torch.finfo(y.dtype).min
        pos_inf = torch.finfo(y.dtype).max
        y_slow = y.masked_fill(~m, neg_inf)
        y_fast = y.masked_fill(~m, pos_inf)
        slow_target = y_slow.argmax(dim=1)
        fast_target = y_fast.argmin(dim=1)

        logits_mask = ~m
        slow_logits = p.masked_fill(logits_mask, -1.0e4) / self.rank_tau
        fast_logits = (-p).masked_fill(logits_mask, -1.0e4) / self.rank_tau

        slow_per = F.cross_entropy(
            slow_logits[valid], slow_target[valid], reduction="none")
        fast_per = F.cross_entropy(
            fast_logits[valid], fast_target[valid], reduction="none")
        w = self._spread_weights(y_std)[valid].to(slow_per.dtype)
        slowest = (slow_per * w).sum() / w.sum().clamp(min=1.0)
        fastest = (fast_per * w).sum() / w.sum().clamp(min=1.0)

        slow_acc = (slow_logits.argmax(dim=1)[valid] == slow_target[valid])
        fast_acc = (fast_logits.argmax(dim=1)[valid] == fast_target[valid])
        return (
            slowest,
            fastest,
            slow_acc.to(pred.dtype).mean(),
            fast_acc.to(pred.dtype).mean(),
        )

    def _invariance(self, pred: torch.Tensor, core_mask: torch.Tensor):
        """rat01 类应 ∈[0,1]（sigmoid 已保证），这里约束 CPI>=0.25(IPC<=4)。"""
        m = core_mask
        cpi_log = pred[..., self.idx["cpi_uop"]]
        cpi = torch.exp(cpi_log.clamp(LOG_PRED_MIN, LOG_PRED_MAX))
        viol = F.relu(0.25 - cpi) * m
        return viol.sum() / m.sum().clamp(min=1)

    def _denom(self, denoms: torch.Tensor, key: str, like: torch.Tensor):
        if denoms is None or key not in DENOM_IDX:
            return torch.zeros_like(like)
        return denoms[..., DENOM_IDX[key]].to(like.dtype).clamp(min=0.0)

    def _physical_constraints(self, pred: torch.Tensor,
                              core_mask: torch.Tensor,
                              denoms: torch.Tensor | None):
        """Soft PMU count constraints from functional opportunity counts."""
        raw = invert_pred(pred.float()).to(pred.dtype)
        m = core_mask.to(pred.dtype)
        terms = []

        def add_upper(key: str, bound: torch.Tensor) -> None:
            if key not in self.idx:
                return
            val = raw[..., self.idx[key]]
            b = bound.to(val.dtype).clamp(min=0.0)
            rel = F.relu(val - b) / (b + 1.0)
            terms.append(rel * rel)

        loads = self._denom(denoms, "loads", m)
        stores = self._denom(denoms, "stores", m)
        atomics = self._denom(denoms, "atomics", m)
        mem_ops = self._denom(denoms, "mem_ops", m)
        branch_count = self._denom(denoms, "branch_count", m)
        store_ops = stores + atomics

        add_upper("branch_miss", branch_count)
        add_upper("l1d_ld_miss", loads)
        add_upper("l1d_st_miss", store_ops)
        add_upper("l2_ld_miss", loads)
        add_upper("l2_st_miss", store_ops)
        add_upper("llc_miss", mem_ops)
        add_upper("dtlb_miss", mem_ops)

        if "l2_ld_miss" in self.idx and "l1d_ld_miss" in self.idx:
            add_upper("l2_ld_miss", raw[..., self.idx["l1d_ld_miss"]])
        if "l2_st_miss" in self.idx and "l1d_st_miss" in self.idx:
            add_upper("l2_st_miss", raw[..., self.idx["l1d_st_miss"]])
        if "llc_miss" in self.idx:
            l2_bound = raw.new_zeros(raw.shape[:2])
            if "l2_ld_miss" in self.idx:
                l2_bound = l2_bound + raw[..., self.idx["l2_ld_miss"]]
            if "l2_st_miss" in self.idx:
                l2_bound = l2_bound + raw[..., self.idx["l2_st_miss"]]
            add_upper("llc_miss", l2_bound)

        if not terms:
            return pred.new_zeros(())
        stacked = torch.stack(terms, dim=-1)
        return (stacked * m.unsqueeze(-1)).sum() / (
            m.sum().clamp(min=1) * stacked.shape[-1]
        )
