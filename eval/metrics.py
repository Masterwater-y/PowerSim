"""metrics.py — PMU 预测评估指标。"""
from __future__ import annotations

import numpy as np

from model.regression_head import PMU_KEYS, KEY_SPACE


def mape(pred: np.ndarray, true: np.ndarray, eps: float = 1e-6) -> float:
    return float(np.mean(np.abs(pred - true) / (np.abs(true) + eps)))


def within_pct(pred: np.ndarray, true: np.ndarray, pct: float,
               eps: float = 1e-6) -> float:
    rel = np.abs(pred - true) / (np.abs(true) + eps)
    return float(np.mean(rel <= pct))


def evaluate(pred_pmu: np.ndarray, true_pmu: np.ndarray) -> dict:
    """pred_pmu / true_pmu: [N, K] 原始量纲。返回 per-key MAPE + cycles 命中率。"""
    res = {}
    for i, k in enumerate(PMU_KEYS):
        res[f"mape_{k}"] = mape(pred_pmu[:, i], true_pmu[:, i])
    ci = PMU_KEYS.index("cpi")
    res["cpi_within_10pct"] = within_pct(pred_pmu[:, ci], true_pmu[:, ci], 0.10)
    res["cpi_within_20pct"] = within_pct(pred_pmu[:, ci], true_pmu[:, ci], 0.20)
    return res
