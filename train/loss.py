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

L_cycles = Huber(log(CPI_uop_pred·uops), log(cycles_label), δ=0.1)
  - 显式监督 cycles，方案C / OnlineQuotaPlanner 的 T_end 反推直接相关
  - 单独 log_var σ_cyc，与 L_cpi_uop 解耦，给"周期级精度"独立学习权重
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from model.regression_head import PMU_KEYS, KEY_SPACE, K

EPS = 1e-6

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


class PMULoss(nn.Module):
    def __init__(self, lambda_inv: float = 0.1,
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
                uops: torch.Tensor | None = None):
        """pred/label: [B,nc,K]，core_mask: [B,nc]，uops: [B,nc]。
        pred 已对 rat01 做 sigmoid。"""
        tgt = transform_label(label)
        m = core_mask.unsqueeze(-1)                                  # [B,nc,1]

        # per-key huber loss with per-space delta
        deltas = self.huber_delta_per_k.to(pred.dtype)               # [K]
        e = pred - tgt                                               # [B,nc,K]
        ae = e.abs()
        per_k = torch.where(
            ae <= deltas,
            0.5 * e * e,
            deltas * (ae - 0.5 * deltas),
        )                                                            # [B,nc,K]
        per_k = (per_k * m).sum(dim=(0, 1)) / m.sum().clamp(min=1)   # [K]
        lv = self.log_var.to(per_k.dtype)
        weighted = (torch.exp(-lv) * per_k + lv).sum()

        # L_cycles：log(cycles) Huber，独立 log_var
        l_cyc = pred.new_zeros(())
        if uops is not None:
            cpi_idx = self.idx["cpi_uop"]
            uops_t = uops.clamp(min=1.0).to(pred.dtype)
            log_uops = torch.log(uops_t)
            log_cycles_pred = pred[..., cpi_idx] + log_uops
            cpi_label = label[..., cpi_idx].clamp(min=EPS).to(pred.dtype)
            log_cycles_tgt = torch.log(cpi_label) + log_uops
            e_cyc = log_cycles_pred - log_cycles_tgt
            ae_cyc = e_cyc.abs()
            d = self.cycles_delta
            per_cyc = torch.where(
                ae_cyc <= d, 0.5 * e_cyc * e_cyc, d * (ae_cyc - 0.5 * d),
            )
            l_cyc = (per_cyc * core_mask).sum() / core_mask.sum().clamp(min=1)
            lvc = self.log_var_cycles.to(l_cyc.dtype)
            weighted = weighted + torch.exp(-lvc) * l_cyc + lvc

        inv = self._invariance(pred, core_mask)
        total = weighted + self.lambda_inv * inv
        logs = {f"L_{k}": per_k[i].detach() for i, k in enumerate(PMU_KEYS)}
        logs["L_cycles"] = l_cyc.detach()
        logs["L_inv"] = inv.detach()
        logs["loss"] = total.detach()
        return total, logs

    def _invariance(self, pred: torch.Tensor, core_mask: torch.Tensor):
        """rat01 类应 ∈[0,1]（sigmoid 已保证），这里约束 CPI>=0.25(IPC<=4)。"""
        m = core_mask
        cpi_log = pred[..., self.idx["cpi_uop"]]
        cpi = torch.exp(cpi_log)
        viol = F.relu(0.25 - cpi) * m
        return viol.sum() / m.sum().clamp(min=1)
