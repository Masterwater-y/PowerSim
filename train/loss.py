"""loss.py — label 变换 + 多任务回归 loss + 物理 invariance。

回归空间（见 regression_head.KEY_SPACE）：
  logratio : pred 是 log(y) 的线性输出，target = log(y)            -> Huber
  rat01    : pred 已 sigmoid ∈[0,1]，target = y ∈[0,1]              -> Huber
  logcount : pred 是 log1p(count) 的线性输出，target = log1p(count) -> Huber
  direct   : pred 线性，target = y                                  -> Huber

v22 默认使用 fixed weighting：
  L = 1.0 * L_cpi_abs
    + 1.0 * L_cycles_window
    + 0.05 * mean(L_aux_pmu)
    + 0.3 * L_centered_cpi

L_centered_cpi：
  - 所有 active core 数 > 1 的窗口都参与；
  - 对每个窗口去掉 core mean 后监督 per-core residual；
  - 按 label log-CPI std 连续加权，高 spread 窗口权重大；
  - 目标是避免 high-spread 窗口预测被平均化。

legacy uncertainty weighting：每 key 一个可学习 log_var σ_k，
  L = Σ_k exp(-σ_k) L_k + σ_k

per-key huber delta（按各空间的"业务可接受误差"取值）：
  logratio (CPI) : 0.1   ≈ ±10% 相对误差
  rat01          : 0.05  ≈ ±5pp 绝对误差
  logcount       : 0.5   ≈ ±65% count 相对误差
  direct         : 1.0   保留原值

L_cycles_window = Huber(log(sum_i CPI_i_pred·uops_i),
                        log(sum_i CPI_i_label·uops_i), δ=0.1)
  - 显式监督 cycles，方案C / OnlineQuotaPlanner 的 T_end 反推直接相关
  - fixed 模式下使用显式 lambda_cycles；uncertainty 模式下才用 log_var_cycles
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
    def __init__(self, lambda_inv: float = 0.0,
                 lambda_phys: float = 0.0,
                 lambda_rank: float = 0.0,
                 lambda_spread: float = 0.0,
                 loss_weight_mode: str = "fixed",
                 lambda_cpi_abs: float = 1.0,
                 lambda_cycles: float = 1.0,
                 lambda_aux_pmu: float = 0.05,
                 lambda_centered_cpi: float = 0.3,
                 rank_gap: float = 0.10,
                 rank_tau: float = 0.10,
                 spread_min_std: float = 0.03,
                 centered_min_std: float = 0.30,
                 centered_ref_std: float = 0.30,
                 centered_weight_min: float = 0.10,
                 centered_weight_max: float = 3.0,
                 huber_delta: dict | float | None = None,
                 cycles_delta: float = 0.1,
                 centered_delta: float = 0.1):
        super().__init__()
        if loss_weight_mode not in {"fixed", "uncertainty"}:
            raise ValueError(f"unknown loss_weight_mode={loss_weight_mode!r}")
        # Legacy uncertainty state. In fixed mode these parameters are frozen
        # and ignored, but kept for checkpoint compatibility.
        init_lv = torch.zeros(K)
        idx = {k: i for i, k in enumerate(PMU_KEYS)}
        if "cpi_uop" in idx:
            init_lv[idx["cpi_uop"]] = -1.0
        for k, i in idx.items():
            if KEY_SPACE[k] == "logcount":
                init_lv[i] = 1.0
        self.log_var = nn.Parameter(init_lv)
        self.log_var_cycles = nn.Parameter(torch.tensor(-1.0))
        if loss_weight_mode == "fixed":
            self.log_var.requires_grad_(False)
            self.log_var_cycles.requires_grad_(False)
        self.loss_weight_mode = loss_weight_mode
        self.lambda_cpi_abs = float(lambda_cpi_abs)
        self.lambda_cycles = float(lambda_cycles)
        self.lambda_aux_pmu = float(lambda_aux_pmu)
        self.lambda_centered_cpi = float(lambda_centered_cpi)
        self.lambda_inv = float(lambda_inv)
        self.lambda_phys = float(lambda_phys)
        # v22: rank/spread are intentionally removed from the objective.
        # Keep constructor args only so old scripts fail less abruptly.
        self.lambda_rank = 0.0
        self.lambda_spread = 0.0
        self.rank_gap = float(rank_gap)
        self.rank_tau = max(float(rank_tau), 1e-6)
        self.spread_min_std = float(spread_min_std)
        # centered_min_std is kept as a diagnostic high-spread threshold.
        # It is not a hard training gate.
        self.centered_min_std = float(centered_min_std)
        self.centered_ref_std = max(float(centered_ref_std), 1e-6)
        self.centered_weight_max = max(float(centered_weight_max), 1.0)
        self.centered_weight_min = min(
            max(float(centered_weight_min), 0.0),
            self.centered_weight_max,
        )

        if huber_delta is None:
            huber_delta = DEFAULT_HUBER_DELTA
        if isinstance(huber_delta, (int, float)):
            huber_delta = {sp: float(huber_delta)
                           for sp in DEFAULT_HUBER_DELTA}
        deltas = torch.tensor([huber_delta[KEY_SPACE[k]] for k in PMU_KEYS],
                              dtype=torch.float32)
        self.register_buffer("huber_delta_per_k", deltas)
        self.cycles_delta = cycles_delta
        self.centered_delta = centered_delta
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
        cpi_abs = per_k[cpi_idx]
        aux_idx = [i for i in range(K) if i != cpi_idx]
        if aux_idx:
            aux_pmu = per_k[aux_idx].mean()
        else:
            aux_pmu = pred.new_zeros(())

        # L_cycles：真正窗口级 log(sum_i CPI_i * uops_i) Huber。
        l_cyc = pred.new_zeros(())
        if uops is not None:
            uops_t = uops.clamp(min=1.0).to(pred.dtype)
            cm = core_mask.to(pred.dtype)
            pred_cpi = torch.exp(
                pred[..., cpi_idx].clamp(LOG_PRED_MIN, LOG_PRED_MAX))
            label_cpi = label[..., cpi_idx].clamp(min=EPS).to(pred.dtype)
            cyc_pred = (pred_cpi * uops_t * cm).sum(dim=1).clamp(min=EPS)
            cyc_tgt = (label_cpi * uops_t * cm).sum(dim=1).clamp(min=EPS)
            log_cycles_pred = torch.log(cyc_pred)
            log_cycles_tgt = torch.log(cyc_tgt)
            e_cyc = log_cycles_pred - log_cycles_tgt
            ae_cyc = e_cyc.abs()
            d = self.cycles_delta
            per_cyc = torch.where(
                ae_cyc <= d, 0.5 * e_cyc * e_cyc, d * (ae_cyc - 0.5 * d),
            )
            l_cyc = per_cyc.mean()

        centered, centered_weight_mean, high_spread_frac = self._centered_cpi(
            pred, label, core_mask)

        if self.loss_weight_mode == "uncertainty":
            lv = self.log_var.to(per_k.dtype)
            weighted = (torch.exp(-lv) * per_k + lv).sum()
            lvc = self.log_var_cycles.to(l_cyc.dtype)
            weighted = weighted + torch.exp(-lvc) * l_cyc + lvc
        else:
            weighted = (
                self.lambda_cpi_abs * cpi_abs
                + self.lambda_cycles * l_cyc
                + self.lambda_aux_pmu * aux_pmu
                + self.lambda_centered_cpi * centered
            )

        inv = self._invariance(pred, core_mask)
        phys = self._physical_constraints(pred, core_mask, denoms)
        total = (
            weighted
            + self.lambda_inv * inv
            + self.lambda_phys * phys
        )
        logs = {f"L_{k}": per_k[i].detach() for i, k in enumerate(PMU_KEYS)}
        logs["L_cycles"] = l_cyc.detach()
        logs["L_aux_pmu"] = aux_pmu.detach()
        logs["L_centered_cpi"] = centered.detach()
        logs["L_rank"] = pred.new_zeros(()).detach()
        logs["L_spread"] = pred.new_zeros(()).detach()
        logs["centered_weight_mean"] = centered_weight_mean.detach()
        logs["high_spread_frac"] = high_spread_frac.detach()
        logs["pred_log_cpi_std"] = self._masked_std(
            pred[..., cpi_idx], core_mask).mean().detach()
        logs["label_log_cpi_std"] = self._masked_std(
            torch.log(label[..., cpi_idx].clamp(min=EPS).to(pred.dtype)),
            core_mask).mean().detach()
        logs["W_cpi_abs"] = pred.new_tensor(self.lambda_cpi_abs).detach()
        logs["W_cycles"] = pred.new_tensor(self.lambda_cycles).detach()
        logs["W_aux_pmu"] = pred.new_tensor(self.lambda_aux_pmu).detach()
        logs["W_centered_cpi"] = pred.new_tensor(
            self.lambda_centered_cpi).detach()
        logs["W_inv"] = pred.new_tensor(self.lambda_inv).detach()
        logs["W_phys"] = pred.new_tensor(self.lambda_phys).detach()
        logs["L_inv"] = inv.detach()
        logs["L_phys"] = phys.detach()
        logs["loss"] = total.detach()
        return total, logs

    def _masked_mean(self, x: torch.Tensor,
                     core_mask: torch.Tensor) -> torch.Tensor:
        m = core_mask.to(x.dtype)
        return (x * m).sum(dim=1) / m.sum(dim=1).clamp(min=1.0)

    def _masked_std(self, x: torch.Tensor,
                    core_mask: torch.Tensor) -> torch.Tensor:
        m = core_mask.to(x.dtype)
        n = m.sum(dim=1).clamp(min=1.0)
        mean = (x * m).sum(dim=1) / n
        var = (((x - mean.unsqueeze(1)) ** 2) * m).sum(dim=1) / n
        return torch.sqrt(var.clamp(min=0.0))

    def _centered_cpi(self, pred: torch.Tensor, label: torch.Tensor,
                      core_mask: torch.Tensor):
        cpi_idx = self.idx["cpi_uop"]
        p = pred[..., cpi_idx]
        y = torch.log(label[..., cpi_idx].clamp(min=EPS).to(pred.dtype))
        m = core_mask.to(pred.dtype)
        active = m.sum(dim=1)
        valid = active > 1
        if not valid.any():
            z = pred.new_zeros(())
            return z, z, z

        p_mean = self._masked_mean(p, core_mask).unsqueeze(1)
        y_mean = self._masked_mean(y, core_mask).unsqueeze(1)
        p_delta = p - p_mean
        y_delta = y - y_mean
        y_std = self._masked_std(y, core_mask)

        e = p_delta - y_delta
        ae = e.abs()
        d = self.centered_delta
        per_core = torch.where(
            ae <= d, 0.5 * e * e, d * (ae - 0.5 * d),
        )
        per_win = (per_core * m).sum(dim=1) / active.clamp(min=1.0)
        weights = (y_std / self.centered_ref_std).clamp(
            min=self.centered_weight_min, max=self.centered_weight_max)
        weights = weights * valid.to(weights.dtype)
        centered = (per_win * weights).sum() / weights.sum().clamp(min=1.0)
        weight_mean = weights[valid].mean()
        high_spread = valid & (y_std >= self.centered_min_std)
        high_frac = high_spread.to(pred.dtype).mean()
        return centered, weight_mean, high_frac

    def _rank_spread(self, pred: torch.Tensor, label: torch.Tensor,
                     core_mask: torch.Tensor):
        cpi_idx = self.idx["cpi_uop"]
        p = pred[..., cpi_idx]
        y = torch.log(label[..., cpi_idx].clamp(min=EPS).to(pred.dtype))
        m = core_mask.to(torch.bool)

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
        upper = torch.triu(torch.ones_like(pair_mask, dtype=torch.bool), diagonal=1)
        pair_mask = pair_mask & upper
        if pair_mask.any():
            sign = dy.sign()
            rank_loss = F.softplus(-(dp * sign) / self.rank_tau)
            rank = rank_loss[pair_mask].mean()
            order_acc = ((dp * sign) > 0).to(pred.dtype)[pair_mask].mean()
        else:
            rank = pred.new_zeros(())
            order_acc = pred.new_zeros(())

        active_n = m.sum(dim=1)
        valid = active_n > 1
        if valid.any():
            mf = m.to(pred.dtype)
            p_mean = (p * mf).sum(dim=1) / mf.sum(dim=1).clamp(min=1.0)
            y_mean = (y * mf).sum(dim=1) / mf.sum(dim=1).clamp(min=1.0)
            p_var = (((p - p_mean.unsqueeze(1)) ** 2) * mf).sum(dim=1) / (
                mf.sum(dim=1).clamp(min=2.0) - 1.0
            )
            y_var = (((y - y_mean.unsqueeze(1)) ** 2) * mf).sum(dim=1) / (
                mf.sum(dim=1).clamp(min=2.0) - 1.0
            )
            p_std = torch.sqrt(p_var.clamp(min=0.0))
            y_std = torch.sqrt(y_var.clamp(min=0.0))
            spread_mask = valid & (y_std > self.spread_min_std)
            if spread_mask.any():
                spread = F.smooth_l1_loss(
                    torch.log(p_std[spread_mask] + 1e-3),
                    torch.log(y_std[spread_mask] + 1e-3),
                    beta=0.1,
                    reduction="mean",
                )
            else:
                spread = pred.new_zeros(())
        else:
            spread = pred.new_zeros(())

        return rank, spread, order_acc

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
