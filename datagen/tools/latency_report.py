#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from typing import Dict, Iterable, Mapping

import numpy as np


DEFAULT_PROBS = (0.5, 0.9, 0.95, 0.99, 0.999)


def _quantiles(arr: np.ndarray, probs: Iterable[float]) -> Dict[str, float]:
    arr = np.asarray(arr, dtype=np.float64)
    if arr.size == 0:
        return {}
    vals = np.quantile(arr, list(probs))
    return {str(p): float(v) for p, v in zip(probs, vals)}


def build_latency_report(by_workload: Mapping[str, Mapping[str, np.ndarray]],
                         probs: Iterable[float] = DEFAULT_PROBS) -> Dict[str, object]:
    probs = tuple(probs)
    report = {
        'probs': list(probs),
        'global': {},
        'by_workload': {},
    }
    fetch_all = []
    exec_all = []
    for workload, cols in by_workload.items():
        fetch = np.asarray(cols['fetch_latency'], dtype=np.float64)
        exe = np.asarray(cols['execution_latency'], dtype=np.float64)
        fetch_all.append(fetch)
        exec_all.append(exe)
        report['by_workload'][workload] = {
            'rows': int(fetch.size),
            'fetch_latency': _quantiles(fetch, probs),
            'execution_latency': _quantiles(exe, probs),
            'fetch_max': float(fetch.max()) if fetch.size else 0.0,
            'execution_max': float(exe.max()) if exe.size else 0.0,
        }
    if fetch_all:
        report['global'] = {
            'rows': int(sum(arr.size for arr in fetch_all)),
            'fetch_latency': _quantiles(np.concatenate(fetch_all), probs),
            'execution_latency': _quantiles(np.concatenate(exec_all), probs),
            'fetch_max': float(max(arr.max() for arr in fetch_all if arr.size)),
            'execution_max': float(max(arr.max() for arr in exec_all if arr.size)),
        }
    return report


def print_latency_report(report: Dict[str, object], file=None) -> None:
    if file is None:
        file = sys.stderr
    probs = report.get('probs', [])
    prob_labels = ', '.join(f'p{int(float(p) * 1000) / 10:g}' for p in probs)
    print(f'[latency-report] probs={prob_labels}', file=file)
    global_stats = report.get('global', {})
    if global_stats:
        print('[latency-report] global '
              f"rows={global_stats.get('rows', 0):,} "
              f"fetch={json.dumps(global_stats.get('fetch_latency', {}), ensure_ascii=False)} "
              f"exec={json.dumps(global_stats.get('execution_latency', {}), ensure_ascii=False)}",
              file=file)
    for workload, stats in sorted((report.get('by_workload') or {}).items()):
        print('[latency-report] '
              f'{workload:20s} rows={stats.get("rows", 0):>10,} '
              f'fetch={json.dumps(stats.get("fetch_latency", {}), ensure_ascii=False)} '
              f'exec={json.dumps(stats.get("execution_latency", {}), ensure_ascii=False)}',
              file=file)
