"""regression_head.py — per-core PMU 回归头。

输入：每核 <QUERY_C{i}> token 的 hidden state [B, n_core, d_model]
输出：[B, n_core, K]，CPI 用 logratio，miss 事件用 logcount，direct 指标直接回归。

头权重在所有核之间共享 -> 支持任意 n_core。v22 起 CPI、branch miss、
cache miss 使用三个独立 MLP，避免 cache/miss 辅助目标直接挤占 CPI head。
"""
from __future__ import annotations

import torch
import torch.nn as nn

# 与 data/build_windows.py PMU_KEYS 顺序一致
PMU_KEYS = [
    "cpi_uop",
    "branch_miss",
    "l1d_ld_miss",
    "l1d_st_miss",
    "l2_ld_miss",
    "l2_st_miss",
    "llc_miss",
]
CPI_KEYS = ["cpi_uop"]
BRANCH_KEYS = ["branch_miss"]
CACHE_KEYS = [
    "l1d_ld_miss",
    "l1d_st_miss",
    "l2_ld_miss",
    "l2_st_miss",
    "llc_miss",
]
# 每个 key 的回归空间：
#   logratio : 目标 = log(y)（CPI 这类正实数比率，无界）
#   rat01    : 目标 ∈ [0,1]，用 sigmoid
#   logcount : 目标 = log1p(count)，无界正
#   direct   : 直接线性
KEY_SPACE = {
    "cpi_uop": "logratio",
    "branch_miss": "logcount",
    "l1d_ld_miss": "logcount",
    "l1d_st_miss": "logcount",
    "l2_ld_miss": "logcount",
    "l2_st_miss": "logcount",
    "llc_miss": "logcount",
    # 兼容旧 ratio 数据/诊断脚本；新 PMU_KEYS 不再使用这些 key。
    "mpki_br": "rat01",
    "branch_mispred_frac": "rat01",
    "mr_l1d_ld": "rat01",
    "mr_l1d_st": "rat01",
    "mr_l1i": "rat01",
    "mr_llc": "rat01",
    "dtlb_miss": "logcount",
    "itlb_miss": "logcount",
    "inv_recv": "logcount",
    "mshr_avg": "direct",
}
K = len(PMU_KEYS)


def _masked_mean(x: torch.Tensor, mask: torch.Tensor | None,
                 dim: int = 1, keepdim: bool = False) -> torch.Tensor:
    if mask is None:
        return x.mean(dim=dim, keepdim=keepdim)
    m = mask.to(x.dtype)
    while m.dim() < x.dim():
        m = m.unsqueeze(-1)
    num = (x * m).sum(dim=dim, keepdim=keepdim)
    den = m.sum(dim=dim, keepdim=keepdim).clamp(min=1.0)
    return num / den


class PMURegressionHead(nn.Module):
    def __init__(self, d_model: int, hidden: int = 256,
                 cpi_head_mode: str = "direct"):
        super().__init__()
        if cpi_head_mode != "direct":
            raise ValueError(
                "v22 split head only supports cpi_head_mode='direct'"
            )
        self.cpi_head_mode = cpi_head_mode
        self.cpi_head = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, hidden),
            nn.GELU(),
            nn.Linear(hidden, len(CPI_KEYS)),
        )
        self.branch_head = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, hidden),
            nn.GELU(),
            nn.Linear(hidden, len(BRANCH_KEYS)),
        )
        self.cache_head = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, hidden),
            nn.GELU(),
            nn.Linear(hidden, len(CACHE_KEYS)),
        )
        # rat01 维度的索引，forward 后做 sigmoid；当前主标签没有 rat01，
        # 保留逻辑给旧配置/诊断兼容。
        self.sig_idx = [i for i, k in enumerate(PMU_KEYS)
                        if KEY_SPACE[k] == "rat01"]

    def forward(self, query_hidden: torch.Tensor,
                core_mask: torch.Tensor | None = None) -> torch.Tensor:
        """query_hidden: [B, n_core, d_model] -> raw_out [B, n_core, K]。
        raw_out 已对 rat01 维度做 sigmoid，其余维度保持线性（回归 log 空间）。"""
        out = torch.cat([
            self.cpi_head(query_hidden),
            self.branch_head(query_hidden),
            self.cache_head(query_hidden),
        ], dim=-1)
        if self.sig_idx:
            idx = torch.tensor(self.sig_idx, device=out.device)
            sig = torch.sigmoid(out.index_select(-1, idx))
            out = out.index_copy(-1, idx, sig)
        return out
