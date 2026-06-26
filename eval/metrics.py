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


def mape_nonzero(pred: np.ndarray, true: np.ndarray,
                 eps: float = 1e-6) -> float:
    mask = np.abs(true) > eps
    if not np.any(mask):
        return float("nan")
    return mape(pred[mask], true[mask], eps=eps)


def rmse(pred: np.ndarray, true: np.ndarray) -> float:
    return float(np.sqrt(np.mean((pred - true) ** 2)))


def evaluate(pred_pmu: np.ndarray, true_pmu: np.ndarray) -> dict:
    """pred_pmu / true_pmu: [N, K] 原始量纲。

    v7 的 branch/cache/TLB 目标是绝对 miss count，很多窗口真值为 0；
    因此同时报告 MAE/RMSE 与 nonzero MAPE，避免零真值把普通 MAPE 放大到
    不可解释的数量级。
    """
    res = {}
    for i, k in enumerate(PMU_KEYS):
        pred = pred_pmu[:, i]
        true = true_pmu[:, i]
        res[f"mae_{k}"] = float(np.mean(np.abs(pred - true)))
        res[f"rmse_{k}"] = rmse(pred, true)
        res[f"mape_{k}"] = mape(pred, true)
        if KEY_SPACE[k] == "logcount":
            res[f"mape_nonzero_{k}"] = mape_nonzero(pred, true)
    ci = PMU_KEYS.index("cpi_uop")
    res["cpi_within_10pct"] = within_pct(pred_pmu[:, ci], true_pmu[:, ci], 0.10)
    res["cpi_within_20pct"] = within_pct(pred_pmu[:, ci], true_pmu[:, ci], 0.20)
    return res
