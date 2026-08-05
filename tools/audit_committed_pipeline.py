#!/usr/bin/env python3
"""Audit committed-path rename/window pressure against existing gem5 labels."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable


RAW_GEM5_STATS = {
    "commit_branch_mispredicts": "commit.branchMispredicts",
    "fetch_squash_cycles": "fetch.status::squashing",
    "icache_stall_cycles": "fetchStats0.icacheStallCycles",
    "squashed_insts_examined": "squashedInstsExamined",
    "rename_register_full_events": "rename.fullRegistersEvents",
    "rename_blocked_cycles": "rename.status::Blocked",
    "rename_unblocking_cycles": "rename.status::Unblocking",
    "dispatch_blocked_cycles": "iew.dispatchStatus::blocked",
    "mem_order_violation_events": "iew.memOrderViolationEvents",
    "rescheduled_loads": "lsq0.rescheduledLoads",
}


def load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def raw_gem5_counters(path: Path) -> dict[str, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    counters: dict[str, int] = {}
    for output, suffix in RAW_GEM5_STATS.items():
        values = re.findall(
            rf"board\.processor\.switch\d+\.core\."
            rf"{re.escape(suffix)}\s+([0-9.eE+-]+)",
            text,
        )
        counters[output] = sum(int(float(value)) for value in values)
    return counters


def normalized_deterministic_stats(stats: dict[str, Any]) -> dict[str, Any]:
    """Drop audit/host-rate fields; everything remaining must be bit-exact."""
    totals = dict(stats["totals"])
    totals.pop("committed_pipeline_audit", None)
    cores = []
    for source in stats["cores"]:
        core = dict(source)
        core.pop("committed_pipeline_audit", None)
        cores.append(core)
    return {
        "totals": totals,
        "cores": cores,
        "threads": stats["threads"],
        "cha": stats["cha"],
    }


def ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    begin = 0
    while begin < len(order):
        end = begin + 1
        while end < len(order) and values[order[end]] == values[order[begin]]:
            end += 1
        rank = (begin + end - 1) / 2.0 + 1.0
        for position in range(begin, end):
            result[order[position]] = rank
        begin = end
    return result


def pearson(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        return math.nan
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean)
        for x, y in zip(left, right)
    )
    left_variance = sum((x - left_mean) ** 2 for x in left)
    right_variance = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_variance * right_variance)
    return numerator / denominator if denominator else math.nan


def spearman(rows: list[dict[str, Any]], left: str, right: str) -> float:
    return pearson(
        ranks([float(row[left]) for row in rows]),
        ranks([float(row[right]) for row in rows]),
    )


def percentile(values: Iterable[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def audit_rows(
    fastsim_root: Path,
    gem5_root: Path,
    reference_root: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for stats_path in sorted(fastsim_root.glob("*/c*/W_*/fastsim-stats.json")):
        case_dir = stats_path.parent
        workload = case_dir.name[2:] if case_dir.name.startswith("W_") else case_dir.name
        core_text = case_dir.parent.name
        cores = int(core_text[1:] if core_text.startswith("c") else core_text)
        uarch = case_dir.parent.parent.name
        gem5_dir = gem5_root / uarch / f"c{cores:02d}" / f"W_{workload}"
        reference_path = (
            reference_root
            / uarch
            / f"c{cores:02d}"
            / f"W_{workload}"
            / "fastsim-stats.json"
        )
        metrics_path = gem5_dir / "metrics.json"
        raw_stats_path = gem5_dir / "stats.txt"
        for required in (metrics_path, raw_stats_path, reference_path):
            if not required.is_file():
                raise FileNotFoundError(f"missing case input: {required}")

        stats = load(stats_path)
        metrics = load(metrics_path)
        reference = load(reference_path)
        audit = stats["totals"]["committed_pipeline_audit"]
        total = stats["totals"]
        uops = int(total["retired_uops"])
        gem5_uops = int(metrics["retired_uops"])
        raw = raw_gem5_counters(raw_stats_path)
        thresholds = {
            int(entry["tokens"]): entry
            for entry in audit["destination_thresholds"]
        }
        fastsim_cpi = float(total["sum_core_cycles"]) / uops
        gem5_cpi = float(metrics["aggregate_uop_cpi"])
        row: dict[str, Any] = {
            "uarch": uarch,
            "workload": workload,
            "cores": cores,
            "q_cycles": int(stats["configuration"]["interval_max_cycles"]),
            "fastsim_uop_cpi": fastsim_cpi,
            "gem5_uop_cpi": gem5_cpi,
            "cpi_gap": gem5_cpi - fastsim_cpi,
            "absolute_cpi_error_percent":
                abs(fastsim_cpi - gem5_cpi) / gem5_cpi * 100.0,
            "fastsim_uops": uops,
            "gem5_uops": gem5_uops,
            "timing_pmu_bit_exact":
                normalized_deterministic_stats(stats)
                == normalized_deterministic_stats(reference),
            "dispatch_conserved": bool(audit["dispatch_conserved"]),
            "destination_conserved": bool(audit["destination_conserved"]),
            "max_live_destination_tokens": int(
                audit["max_live_destination_tokens"]
            ),
            "destination_average_lifetime_cycles":
                float(audit["destination_lifetime_token_cycles"])
                / max(1, int(audit["destination_tokens"])),
            "destination_64_event_rate":
                int(thresholds[64]["events"]) / uops,
            "destination_96_event_rate":
                int(thresholds[96]["events"]) / uops,
            "destination_128_event_rate":
                int(thresholds[128]["events"]) / uops,
            "destination_192_event_rate":
                int(thresholds[192]["events"]) / uops,
            "destination_256_event_rate":
                int(thresholds[256]["events"]) / uops,
            "fastsim_dispatch_bandwidth_event_rate":
                int(audit["dispatch_bandwidth_events"]) / uops,
            "fastsim_rob_capacity_event_rate":
                int(audit["rob_capacity_events"]) / uops,
            "fastsim_iq_capacity_event_rate":
                int(audit["iq_capacity_events"]) / uops,
            "fastsim_lq_capacity_event_rate":
                int(audit["lq_capacity_events"]) / uops,
            "fastsim_sq_capacity_event_rate":
                int(audit["sq_capacity_events"]) / uops,
            "fastsim_memory_iq_post_issue_cycles_per_uop":
                int(audit["memory_iq_post_issue_cycles"]) / uops,
            "gem5_rob_full_events_per_uop":
                int(metrics["rob_full_events"]) / gem5_uops,
            "gem5_iq_full_events_per_uop":
                int(metrics["iq_full_events"]) / gem5_uops,
            "gem5_register_full_events_per_uop":
                raw["rename_register_full_events"] / gem5_uops,
            "gem5_rename_blocked_cycles_per_uop":
                raw["rename_blocked_cycles"] / gem5_uops,
            "gem5_dispatch_blocked_cycles_per_uop":
                raw["dispatch_blocked_cycles"] / gem5_uops,
            **raw,
        }
        rows.append(row)
    if not rows:
        raise ValueError(f"no FastSim cases found below {fastsim_root}")
    return rows


def write_report(rows: list[dict[str, Any]], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "rows.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    with (output / "rows.csv").open("w", newline="", encoding="utf-8") as sink:
        writer = csv.DictWriter(sink, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    all_neutral = all(row["timing_pmu_bit_exact"] for row in rows)
    all_dispatch = all(row["dispatch_conserved"] for row in rows)
    all_destination = all(row["destination_conserved"] for row in rows)
    all_q = all(row["q_cycles"] == 1024 for row in rows)
    correlations = {
        key: spearman(rows, "cpi_gap", key)
        for key in (
            "gem5_register_full_events_per_uop",
            "gem5_rename_blocked_cycles_per_uop",
            "gem5_iq_full_events_per_uop",
            "gem5_rob_full_events_per_uop",
            "destination_96_event_rate",
            "destination_192_event_rate",
            "destination_average_lifetime_cycles",
            "fastsim_iq_capacity_event_rate",
            "fastsim_rob_capacity_event_rate",
        )
    }
    baselines = {
        row["workload"]: row for row in rows if row["uarch"] == "baseline"
    }

    report = [
        "# Committed-path pipeline audit",
        "",
        "本报告只审计 functional trace 可观测的 committed path；不生成、估计或回放 "
        "speculative/wrong-path UOP。",
        "",
        "## Gate",
        "",
        f"- cases: {len(rows)}",
        f"- Q 固定为 1024: {'PASS' if all_q else 'FAIL'}",
        f"- 审计前后 CPI/PMU bit-exact: {'PASS' if all_neutral else 'FAIL'}",
        f"- dispatch 最终门控守恒: {'PASS' if all_dispatch else 'FAIL'}",
        f"- destination token 生命周期守恒: {'PASS' if all_destination else 'FAIL'}",
        f"- CPI absolute error mean/P90: "
        f"{sum(float(row['absolute_cpi_error_percent']) for row in rows) / len(rows):.3f}% / "
        f"{percentile((float(row['absolute_cpi_error_percent']) for row in rows), 0.9):.3f}%",
        "",
        "## Baseline evidence",
        "",
        "| workload | FastSim CPI | gem5 CPI | gap | gem5 rename blocked cycles | "
        "gem5 register-full events | committed destination max | >96 event rate | >192 event rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for workload in sorted(baselines):
        row = baselines[workload]
        report.append(
            f"| {workload} | {row['fastsim_uop_cpi']:.5f} | "
            f"{row['gem5_uop_cpi']:.5f} | {row['cpi_gap']:.5f} | "
            f"{row['rename_blocked_cycles']:,} | "
            f"{row['rename_register_full_events']:,} | "
            f"{row['max_live_destination_tokens']:,} | "
            f"{100 * row['destination_96_event_rate']:.2f}% | "
            f"{100 * row['destination_192_event_rate']:.2f}% |"
        )

    report.extend(
        [
            "",
            "## Twelve-case component ledger",
            "",
            "| uarch | workload | CPI gap | FS BW gate/uop | FS ROB gate/uop | "
            "FS IQ gate/uop | gem5 ROB full/uop | gem5 IQ full/uop | gem5 reg full/uop |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(rows, key=lambda value: (value["workload"], value["uarch"])):
        report.append(
            f"| {row['uarch']} | {row['workload']} | {row['cpi_gap']:.4f} | "
            f"{row['fastsim_dispatch_bandwidth_event_rate']:.3f} | "
            f"{row['fastsim_rob_capacity_event_rate']:.3f} | "
            f"{row['fastsim_iq_capacity_event_rate']:.3f} | "
            f"{row['gem5_rob_full_events_per_uop']:.3f} | "
            f"{row['gem5_iq_full_events_per_uop']:.3f} | "
            f"{row['gem5_register_full_events_per_uop']:.3f} |"
        )

    report.extend(
        [
            "",
            "## Rank correlations (diagnostic only)",
            "",
            "12 cases 跨两个 workload，相关性可能被 workload identity 混杂，不能当作 "
            "cycle 分解或因果证明。",
            "",
            "| signal vs CPI gap | Spearman rho |",
            "|---|---:|",
        ]
    )
    for key, value in correlations.items():
        report.append(f"| {key} | {value:.3f} |")

    report.extend(
        [
            "",
            "## Profile response relative to baseline",
            "",
            "| workload | uarch | gem5 ΔCPI | FastSim ΔCPI | gap Δ | "
            "gem5 rename-blocked/uop Δ |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in sorted(rows, key=lambda value: (value["workload"], value["uarch"])):
        if row["uarch"] == "baseline":
            continue
        base = baselines[row["workload"]]
        report.append(
            f"| {row['workload']} | {row['uarch']} | "
            f"{row['gem5_uop_cpi'] - base['gem5_uop_cpi']:+.5f} | "
            f"{row['fastsim_uop_cpi'] - base['fastsim_uop_cpi']:+.5f} | "
            f"{row['cpi_gap'] - base['cpi_gap']:+.5f} | "
            f"{row['gem5_rename_blocked_cycles_per_uop'] - base['gem5_rename_blocked_cycles_per_uop']:+.3f} |"
        )

    report.extend(
        [
            "",
            "## Decision",
            "",
            "- 现有证据不支持继续做 speculative-path 模型；该信息不在 functional trace "
            "合同内，不能成为可部署修复。",
            "- cache/branch/CHA 事件已在既有 gate 对齐，而本轮显示 gem5 rename 阻塞与 "
            "register-full 压力很大；FastSim 却没有物理寄存器 free-list，并且 ROB/IQ/LSQ "
            "容量只在 dispatch lower bound 处门控。当前应优先审计/修复 committed rename "
            "admission 与资源释放语义。",
            "- v5 trace 只有 n_dst，没有 destination register class/identity。当前 pooled token "
            "只可定位压力，不能直接作为 timing 容量，否则会把 Int/Float/Vec/CC 的独立 "
            "free-list 错并成一个参数。",
            "- 下一安全实现单元是给 functional trace 增加 destination class counts，按 gem5 "
            "配置的 per-class physical register 数在 rename 分配、ordered retirement 释放；"
            "随后按 12-case → 业务48 → 阈值48 gate，Q 始终为1024。",
            "",
        ]
    )
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fastsim-root", type=Path, required=True)
    parser.add_argument("--gem5-root", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    output = args.out or args.fastsim_root
    rows = audit_rows(
        args.fastsim_root.resolve(),
        args.gem5_root.resolve(),
        args.reference_root.resolve(),
    )
    write_report(rows, output.resolve())
    failed = [
        row
        for row in rows
        if row["q_cycles"] != 1024
        or not row["timing_pmu_bit_exact"]
        or not row["dispatch_conserved"]
        or not row["destination_conserved"]
    ]
    print(
        f"committed pipeline audit cases={len(rows)} "
        f"gate={'PASS' if not failed else 'FAIL'} "
        f"report={(output.resolve() / 'report.md')}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
