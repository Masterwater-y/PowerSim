"""Human-readable reports for deployment-side evaluation results."""
from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _values(rows: Iterable[Mapping[str, Any]], key: str) -> List[float]:
    result: List[float] = []
    for row in rows:
        value = _finite(row.get(key))
        if value is not None:
            result.append(value)
    return result


def _branch_relative(row: Mapping[str, Any]) -> Optional[float]:
    """Read the canonical metric while accepting reports made before it existed."""
    for key in ("branch_miss_relative_error", "branch_miss_rate_relative_error"):
        value = _finite(row.get(key))
        if value is not None:
            return value
    return None


def _branch_relative_values(rows: Iterable[Mapping[str, Any]]) -> List[float]:
    result: List[float] = []
    for row in rows:
        value = _branch_relative(row)
        if value is not None:
            result.append(value)
    return result


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else float("nan")


def _percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _pct(value: Any) -> float:
    number = _finite(value)
    return float("nan") if number is None else 100.0 * number


def _fmt(value: Any, width: int = 8, precision: int = 2) -> str:
    number = _finite(value)
    if number is None:
        return f"{'-':>{width}}"
    return f"{number:>{width}.{precision}f}"


def _set_name(row: Mapping[str, Any]) -> str:
    workload = str(row.get("workload", ""))
    return "heldout" if workload.endswith("_heldout") else "train/base"


def _subset(rows: Sequence[Mapping[str, Any]], set_name: str) -> List[Mapping[str, Any]]:
    if set_name == "all":
        return list(rows)
    return [row for row in rows if _set_name(row) == set_name]


def _macro_row(n_cores: int, set_name: str, rows: Sequence[Mapping[str, Any]]) -> str:
    roi = _values(rows, "roi_cpi_error")
    window = _values(rows, "window_cpi_mape_mean")
    branch_rel = _branch_relative_values(rows)
    branch_pp = _values(rows, "branch_miss_rate_abs_error")
    chunk = _values(rows, "chunk_cpi_mape_mean")
    return (
        f"{n_cores:>5d} {set_name:<10} {len(rows):>3d}  "
        f"{_fmt(100 * _mean(roi))} {_fmt(100 * _percentile(roi, .50))} "
        f"{_fmt(100 * _percentile(roi, .90))}  "
        f"{_fmt(100 * _mean(window))} {_fmt(100 * _percentile(window, .50))} "
        f"{_fmt(100 * _percentile(window, .90))}  "
        f"{_fmt(100 * _mean(branch_rel))} {_fmt(100 * _percentile(branch_rel, .50))} "
        f"{_fmt(100 * _percentile(branch_rel, .90))} {_fmt(100 * _mean(branch_pp))}  "
        f"{_fmt(100 * _mean(chunk))}"
    )


def render_deployment_text_report(
    report: Mapping[str, Any],
    *,
    source: Optional[str] = None,
) -> str:
    """Render TSim-style macro summaries and per-workload detail.

    Macro statistics give every workload one vote. The existing JSON aggregate is
    retained as a micro aggregate, but is deliberately not used as the headline
    accuracy result because signed errors and workload sizes can cancel.
    """
    traces = [row for row in report.get("traces", []) if isinstance(row, Mapping)]
    grouped: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for row in traces:
        grouped[int(row.get("n_cores", 0))].append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: str(row.get("workload", "")))

    run = report.get("run", {}) if isinstance(report.get("run"), Mapping) else {}
    aggregate = (
        report.get("aggregate", {})
        if isinstance(report.get("aggregate"), Mapping)
        else {}
    )
    lines: List[str] = []
    lines.append("TCSim deployment evaluation report")
    lines.append("=" * 152)
    if source:
        lines.append(f"source_json : {os.path.abspath(source)}")
    lines.append(f"checkpoint  : {run.get('checkpoint', '-')}")
    lines.append(f"split       : {run.get('split', '-')}")
    lines.append(
        f"coverage    : traces={len(traces)} chunks={int(aggregate.get('n_chunks', 0))} "
        f"label_uops={int(aggregate.get('roi_valid_label_uops', 0))}/"
        f"{int(aggregate.get('roi_uops', 0))} "
        f"({100.0 * float(aggregate.get('roi_label_coverage', 0.0)):.6f}%)"
    )
    lines.append("")
    lines.append("Metric definitions")
    lines.append("- ROI-CPI error: abs(predicted full-trace ROI CPI - true ROI CPI) / true ROI CPI.")
    lines.append("- window CPI MAPE: mean error over deployment scheduler steps; a step aggregates chunks committed in that epsilon step.")
    lines.append("- branch relative error: relative error of full-ROI branch-miss rate (equivalently miss count because the conditional-branch denominator is shared).")
    lines.append("- branch abs pp: absolute predicted-vs-true branch-miss-rate difference in percentage points.")
    lines.append("- macro: workloads are equally weighted. P50/P90 are across workload-level metrics, not pooled windows.")
    lines.append("- train/base contains all non-heldout workloads; heldout contains names ending in _heldout.")
    lines.append("")

    lines.append("Primary result: workload-macro accuracy by core count")
    lines.append("-" * 152)
    lines.append(
        f"{'cores':>5} {'set':<10} {'n':>3}  "
        f"{'ROImean%':>8} {'ROIp50%':>8} {'ROIp90%':>8}  "
        f"{'WINmean%':>8} {'WINp50%':>8} {'WINp90%':>8}  "
        f"{'BRmean%':>8} {'BRp50%':>8} {'BRp90%':>8} {'BRabsPP':>8}  "
        f"{'CHUNK%':>8}"
    )
    lines.append("-" * 152)
    for n_cores, rows in sorted(grouped.items()):
        for set_name in ("all", "train/base", "heldout"):
            selected = _subset(rows, set_name)
            if selected:
                lines.append(_macro_row(n_cores, set_name, selected))
    lines.append("-" * 152)
    lines.append("WINmean is the macro mean of each workload's window-MAPE mean; CHUNK is diagnostic fixed-256-UOP chunk MAPE.")
    lines.append("")

    lines.append("Secondary result: pooled micro aggregate (not the headline accuracy metric)")
    lines.append("-" * 152)
    lines.append(
        "This first sums cycles/misses over all 92 traces and then computes one error. "
        "It is useful for total-volume bias and accounting sanity, but long traces dominate and over/under-prediction across workloads cancels."
    )
    lines.append(
        "ROI CPI  pred={} true={} error={}%; branch miss rate pred={}% true={}% rel_error={}% abs_error={} pp".format(
            _fmt(aggregate.get("pred_roi_cpi"), 0, 6).strip(),
            _fmt(aggregate.get("true_roi_cpi"), 0, 6).strip(),
            _fmt(_pct(aggregate.get("global_roi_cpi_error")), 0, 4).strip(),
            _fmt(_pct(aggregate.get("pred_branch_miss_rate")), 0, 5).strip(),
            _fmt(_pct(aggregate.get("true_branch_miss_rate")), 0, 5).strip(),
            _fmt(_pct(_branch_relative(aggregate)), 0, 4).strip(),
            _fmt(_pct(aggregate.get("branch_miss_rate_abs_error")), 0, 4).strip(),
        )
    )
    lines.append("")

    detail_header = (
        f"{'workload':<34} {'set':<10} {'steps':>7}  "
        f"{'predROI':>8} {'trueROI':>8} {'ROIerr%':>8}  "
        f"{'WINmean%':>8} {'WINp90%':>8}  "
        f"{'BRpred%':>8} {'BRtrue%':>8} {'BRerr%':>8} {'BRabsPP':>8}  "
        f"{'CHUNK%':>8}"
    )
    for n_cores, rows in sorted(grouped.items()):
        lines.append(f"Per-workload detail: c{n_cores:02d}")
        lines.append("-" * 152)
        lines.append(detail_header)
        lines.append("-" * 152)
        for row in rows:
            lines.append(
                f"{str(row.get('workload', '-')):<34} {_set_name(row):<10} "
                f"{int(row.get('n_steps', 0)):>7d}  "
                f"{_fmt(row.get('pred_roi_cpi'))} {_fmt(row.get('true_roi_cpi'))} "
                f"{_fmt(_pct(row.get('roi_cpi_error')))}  "
                f"{_fmt(_pct(row.get('window_cpi_mape_mean')))} "
                f"{_fmt(_pct(row.get('window_cpi_mape_p90')))}  "
                f"{_fmt(_pct(row.get('pred_branch_miss_rate')))} "
                f"{_fmt(_pct(row.get('true_branch_miss_rate')))} "
                f"{_fmt(_pct(_branch_relative(row)))} "
                f"{_fmt(_pct(row.get('branch_miss_rate_abs_error')))}  "
                f"{_fmt(_pct(row.get('chunk_cpi_mape_mean')))}"
            )
        lines.append("-" * 152)
        lines.append(_macro_row(n_cores, "all", rows))
        lines.append("")

    lines.append("Largest ROI-CPI errors")
    lines.append("-" * 86)
    lines.append(f"{'cores':>5} {'workload':<38} {'set':<10} {'ROIerr%':>9} {'WINmean%':>10} {'BRerr%':>9}")
    lines.append("-" * 86)
    ranked = sorted(
        traces,
        key=lambda row: _finite(row.get("roi_cpi_error")) or -1.0,
        reverse=True,
    )
    for row in ranked[:15]:
        lines.append(
            f"{int(row.get('n_cores', 0)):>5d} {str(row.get('workload', '-')):<38} "
            f"{_set_name(row):<10} {_fmt(_pct(row.get('roi_cpi_error')), 9)} "
            f"{_fmt(_pct(row.get('window_cpi_mape_mean')), 10)} "
            f"{_fmt(_pct(_branch_relative(row)), 9)}"
        )
    lines.append("=" * 152)
    return "\n".join(lines) + "\n"


def write_deployment_text_report(
    path: str,
    report: Mapping[str, Any],
    *,
    source: Optional[str] = None,
) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_deployment_text_report(report, source=source))
