"""loss.py — label 变换 + 多任务回归 loss（uncertainty weighting）+ 物理 invariance。

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

L_cycles(per_core) = Huber(log(CPI_uop_pred·uops), log(cycles_label), δ=0.1)
  - 显式监督 cycles，方案C / OnlineQuotaPlanner 的 T_end 反推直接相关
  - 单独 log_var σ_cyc，与 L_cpi_uop 解耦，给"周期级精度"独立学习权重

L_cycles(window) = Huber(log(sum_i CPI_pred_i·uops_i),
                         log(sum_i CPI_label_i·uops_i), δ=0.1)
  - 真正约束窗口级总周期，避免 per-core cycles 在 log-space 退化成 CPI loss。
"""
from __future__ import annotations

from typing import Sequence

import torch
import torch.nn as nn
import torch.nn.functional as F

from model.regression_head import PMU_KEYS, KEY_SPACE, K

EPS = 1e-6
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
            out[..., i] = torch.exp(p)
        elif sp == "rat01":
            out[..., i] = p.clamp(0.0, 1.0)
        elif sp == "logcount":
            out[..., i] = torch.expm1(p).clamp(min=0.0)
        else:
            out[..., i] = p
    return out


def _huber(e: torch.Tensor, delta: float | torch.Tensor) -> torch.Tensor:
    ae = e.abs()
    return torch.where(
        ae <= delta,
        0.5 * e * e,
        delta * (ae - 0.5 * delta),
    )


class PMULoss(nn.Module):
    def __init__(self, lambda_inv: float = 0.1,
                 lambda_phys: float = 0.05,
                 huber_delta: dict | float | None = None,
                 cycles_delta: float = 0.1,
                 cycles_loss_mode: str = "per_core",
                 loss_keys: str | Sequence[str] | None = None,
                 tail_base_lambda: float = 0.0,
                 tail_under_lambda: float = 0.0,
                 tail_low_over_lambda: float = 0.0,
                 tail_tau: float = 0.4,
                 tail_low_over_margin: float = 0.0,
                 tail_log_mid: float | None = None):
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
        self.active_loss_keys = self._parse_loss_keys(loss_keys)
        active_mask = torch.tensor(
            [1.0 if k in self.active_loss_keys else 0.0 for k in PMU_KEYS],
            dtype=torch.float32,
        )
        self.register_buffer("active_loss_mask", active_mask)
        cycles_loss_mode = str(cycles_loss_mode)
        if cycles_loss_mode not in {"per_core", "window", "off"}:
            raise ValueError(
                "cycles_loss_mode must be one of: per_core, window, off"
            )
        self.cycles_loss_mode = cycles_loss_mode
        self.tail_base_lambda = float(tail_base_lambda)
        self.tail_under_lambda = float(tail_under_lambda)
        self.tail_low_over_lambda = float(tail_low_over_lambda)
        self.tail_tau = max(float(tail_tau), EPS)
        self.tail_low_over_margin = float(tail_low_over_margin)

        if huber_delta is None:
            huber_delta = DEFAULT_HUBER_DELTA
        if isinstance(huber_delta, (int, float)):
            huber_delta = {sp: float(huber_delta)
                           for sp in DEFAULT_HUBER_DELTA}
        deltas = torch.tensor([huber_delta[KEY_SPACE[k]] for k in PMU_KEYS],
                              dtype=torch.float32)
        self.register_buffer("huber_delta_per_k", deltas)
        init_tail = 0.0 if tail_log_mid is None else float(tail_log_mid)
        self.register_buffer("tail_log_mid", torch.tensor(init_tail))
        self.tail_enabled = tail_log_mid is not None
        self.cycles_delta = cycles_delta
        self.idx = {k: i for i, k in enumerate(PMU_KEYS)}

    @staticmethod
    def _parse_loss_keys(loss_keys: str | Sequence[str] | None) -> tuple[str, ...]:
        if loss_keys is None:
            keys = list(PMU_KEYS)
        elif isinstance(loss_keys, str):
            raw = loss_keys.strip()
            keys = list(PMU_KEYS) if not raw else [
                x.strip() for x in raw.split(",") if x.strip()
            ]
        else:
            keys = [str(x).strip() for x in loss_keys if str(x).strip()]
            if not keys:
                keys = list(PMU_KEYS)
        unknown = [k for k in keys if k not in PMU_KEYS]
        if unknown:
            raise ValueError(f"unknown loss_keys={unknown}; valid={PMU_KEYS}")
        return tuple(dict.fromkeys(keys))

    def set_tail_log_mid(self, value: float) -> None:
        with torch.no_grad():
            self.tail_log_mid.fill_(float(value))
        self.tail_enabled = True

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
        per_k = _huber(e, deltas)                                    # [B,nc,K]
        denom = m.sum().clamp(min=1.0)
        per_k = (per_k * m).sum(dim=(0, 1)) / denom                 # [K]
        lv = self.log_var.to(per_k.dtype)
        active = self.active_loss_mask.to(per_k.dtype)
        # Multiplying by active keeps the graph connected for all dimensions
        # while giving inactive heads exactly zero loss/gradient contribution.
        weighted = ((torch.exp(-lv) * per_k + lv) * active).sum()

        l_tail_base = pred.new_zeros(())
        l_tail_under = pred.new_zeros(())
        l_tail_low_over = pred.new_zeros(())
        if self.tail_enabled and (
            self.tail_base_lambda > 0.0
            or self.tail_under_lambda > 0.0
            or self.tail_low_over_lambda > 0.0
        ):
            cpi_idx = self.idx["cpi_uop"]
            log_pred = pred[..., cpi_idx]
            log_label = tgt[..., cpi_idx]
            cm = core_mask.to(pred.dtype)
            tail_mid = self.tail_log_mid.to(pred.dtype)
            w_tail = torch.sigmoid((log_label - tail_mid) / self.tail_tau)
            w_low = 1.0 - w_tail
            cpi_delta = self.huber_delta_per_k[cpi_idx].to(pred.dtype)
            cpi_base = _huber(log_pred - log_label, cpi_delta)
            under = F.relu(log_label - log_pred)
            low_over = F.relu(
                log_pred - log_label - self.tail_low_over_margin
            )
            norm = cm.sum().clamp(min=1.0)
            if self.tail_base_lambda > 0.0:
                l_tail_base = (cpi_base * w_tail * cm).sum() / norm
                weighted = weighted + self.tail_base_lambda * l_tail_base
            if self.tail_under_lambda > 0.0:
                l_tail_under = (_huber(under, cpi_delta) * w_tail * cm).sum() / norm
                weighted = weighted + self.tail_under_lambda * l_tail_under
            if self.tail_low_over_lambda > 0.0:
                l_tail_low_over = (
                    _huber(low_over, cpi_delta) * w_low * cm
                ).sum() / norm
                weighted = weighted + (
                    self.tail_low_over_lambda * l_tail_low_over
                )

        # L_cycles：log(cycles) Huber，独立 log_var
        l_cyc = pred.new_zeros(())
        if uops is not None and self.cycles_loss_mode != "off":
            cpi_idx = self.idx["cpi_uop"]
            uops_t = uops.clamp(min=1.0).to(pred.dtype)
            cpi_label = label[..., cpi_idx].clamp(min=EPS).to(pred.dtype)
            cm = core_mask.to(pred.dtype)
            if self.cycles_loss_mode == "window":
                pred_cpi = torch.exp(pred[..., cpi_idx]).to(pred.dtype)
                pred_cycles = (pred_cpi * uops_t * cm).sum(dim=1)
                label_cycles = (cpi_label * uops_t * cm).sum(dim=1)
                valid = cm.sum(dim=1) > 0
                e_cyc = (
                    torch.log(pred_cycles.clamp(min=EPS))
                    - torch.log(label_cycles.clamp(min=EPS))
                )
                e_cyc = e_cyc[valid]
            else:
                log_uops = torch.log(uops_t)
                log_cycles_pred = pred[..., cpi_idx] + log_uops
                log_cycles_tgt = torch.log(cpi_label) + log_uops
                e_cyc = log_cycles_pred - log_cycles_tgt
            d = self.cycles_delta
            per_cyc = _huber(e_cyc, d)
            if self.cycles_loss_mode == "window":
                l_cyc = per_cyc.mean() if per_cyc.numel() else pred.new_zeros(())
            else:
                cm = core_mask.to(per_cyc.dtype)
                l_cyc = (per_cyc * cm).sum() / cm.sum().clamp(min=1.0)
            lvc = self.log_var_cycles.to(l_cyc.dtype)
            weighted = weighted + torch.exp(-lvc) * l_cyc + lvc

        inv = self._invariance(pred, core_mask)
        phys = self._physical_constraints(pred, core_mask, denoms)
        total = weighted + self.lambda_inv * inv + self.lambda_phys * phys
        logs = {f"L_{k}": per_k[i].detach() for i, k in enumerate(PMU_KEYS)}
        logs["L_cycles"] = l_cyc.detach()
        logs["L_tail_base"] = l_tail_base.detach()
        logs["L_tail_under"] = l_tail_under.detach()
        logs["L_tail_low_over"] = l_tail_low_over.detach()
        logs["L_inv"] = inv.detach()
        logs["L_phys"] = phys.detach()
        logs["loss"] = total.detach()
        return total, logs

    def _invariance(self, pred: torch.Tensor, core_mask: torch.Tensor):
        """rat01 类应 ∈[0,1]（sigmoid 已保证），这里约束 CPI>=0.25(IPC<=4)。"""
        m = core_mask
        cpi_log = pred[..., self.idx["cpi_uop"]]
        cpi = torch.exp(cpi_log)
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
            if key not in self.active_loss_keys:
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
