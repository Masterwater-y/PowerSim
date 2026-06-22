"""regression_head.py — per-core PMU 回归头。

输入：每核 <QUERY_C{i}> token 的 hidden state [B, n_core, d_model]
输出：[B, n_core, K]，前若干维是 ratio（sigmoid 限幅前的 raw），后若干是 count/direct。

头权重在所有核之间共享 -> 支持任意 n_core。
"""
from __future__ import annotations

import torch
import torch.nn as nn

# 与 data/build_windows.py PMU_KEYS 顺序一致
PMU_KEYS = [
    "cpi",
    "mpki_br",
    "mr_l1d_ld",
    "mr_l1d_st",
    "dtlb_miss",
]
# 每个 key 的回归空间：
#   logratio : 目标 = log(y)（CPI 这类正实数比率，无界）
#   rat01    : 目标 ∈ [0,1]，用 sigmoid
#   logcount : 目标 = log1p(count)，无界正
#   direct   : 直接线性
KEY_SPACE = {
    "cpi": "logratio",
    "mpki_br": "rat01",
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


class PMURegressionHead(nn.Module):
    def __init__(self, d_model: int, hidden: int = 256):
        super().__init__()
        self.ln = nn.LayerNorm(d_model)
        self.mlp = nn.Sequential(
            nn.Linear(d_model, hidden),
            nn.GELU(),
            nn.Linear(hidden, K),
        )
        # rat01 维度的索引，forward 后做 sigmoid
        self.sig_idx = [i for i, k in enumerate(PMU_KEYS)
                        if KEY_SPACE[k] == "rat01"]

    def forward(self, query_hidden: torch.Tensor) -> torch.Tensor:
        """query_hidden: [B, n_core, d_model] -> raw_out [B, n_core, K]。
        raw_out 已对 rat01 维度做 sigmoid，其余维度保持线性（回归 log 空间）。"""
        x = self.ln(query_hidden)
        out = self.mlp(x)
        if self.sig_idx:
            idx = torch.tensor(self.sig_idx, device=out.device)
            sig = torch.sigmoid(out.index_select(-1, idx))
            out = out.index_copy(-1, idx, sig)
        return out
