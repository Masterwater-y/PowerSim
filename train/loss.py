"""loss.py — label 变换 + 多任务回归 loss（uncertainty weighting）+ 物理 invariance。

回归空间（见 regression_head.KEY_SPACE）：
  logratio : pred 是 log(y) 的线性输出，target = log(y)            -> Huber
  rat01    : pred 已 sigmoid ∈[0,1]，target = y ∈[0,1]              -> Huber
  logcount : pred 是 log1p(count) 的线性输出，target = log1p(count) -> Huber
  direct   : pred 线性，target = y                                  -> Huber

uncertainty weighting：每 key 一个可学习 log_var σ_k，
  L = Σ_k exp(-σ_k) L_k + σ_k
"""
from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from model.regression_head import PMU_KEYS, KEY_SPACE, K

EPS = 1e-6


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
    def __init__(self, lambda_inv: float = 0.1, huber_delta: float = 1.0):
        super().__init__()
        self.log_var = nn.Parameter(torch.zeros(K))
        self.lambda_inv = lambda_inv
        self.huber_delta = huber_delta
        # rat01 维度索引（用于 invariance）
        self.idx = {k: i for i, k in enumerate(PMU_KEYS)}

    def forward(self, pred: torch.Tensor, label: torch.Tensor,
                core_mask: torch.Tensor):
        """pred/label: [B,nc,K]，core_mask: [B,nc]。pred 已对 rat01 做 sigmoid。"""
        tgt = transform_label(label)
        m = core_mask.unsqueeze(-1)                    # [B,nc,1]
        per_k = F.huber_loss(pred, tgt, reduction="none",
                            delta=self.huber_delta)    # [B,nc,K]
        per_k = (per_k * m).sum(dim=(0, 1)) / m.sum().clamp(min=1)  # [K]
        # uncertainty weighting
        lv = self.log_var.to(per_k.dtype)
        weighted = (torch.exp(-lv) * per_k + lv).sum()

        # 物理 invariance（在原始量纲 / [0,1] 空间）
        inv = self._invariance(pred, core_mask)
        total = weighted + self.lambda_inv * inv
        logs = {f"L_{k}": per_k[i].detach() for i, k in enumerate(PMU_KEYS)}
        logs["L_inv"] = inv.detach()
        logs["loss"] = total.detach()
        return total, logs

    def _invariance(self, pred: torch.Tensor, core_mask: torch.Tensor):
        """rat01 类应 ∈[0,1]（sigmoid 已保证），这里约束 CPI>=0.25(IPC<=4)。"""
        m = core_mask
        cpi_log = pred[..., self.idx["cpi"]]           # log(cpi)
        cpi = torch.exp(cpi_log)
        # CPI < 0.25 惩罚
        viol = F.relu(0.25 - cpi) * m
        return viol.sum() / m.sum().clamp(min=1)
