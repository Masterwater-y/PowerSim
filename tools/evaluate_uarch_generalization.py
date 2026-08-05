#!/usr/bin/env python3
"""Compare FastSim uarch replays with gem5 OoO CPI/PMU labels."""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as source:
        return json.load(source)


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def distribution(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "mean": statistics.fmean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p90": percentile(values, 90),
        "p99": percentile(values, 99),
        "max": max(values) if values else None,
    }


def relative(predicted: float, reference: float) -> float | None:
    return None if reference == 0 else predicted / reference - 1.0


def cha_sum(stats: dict[str, Any], key: str) -> int:
    return sum(int(item[key]) for item in stats["cha"])


PMUS: dict[str, tuple[Callable[[dict[str, Any]], float], str, str]] = {
    "l1d_misses": (lambda s: float(s["totals"]["l1d_misses"]), "l1d_demand_misses", "strict"),
    "private_l2_misses": (lambda s: float(s["totals"]["l2_misses"]), "private_l2_demand_misses", "strict"),
    "cha_llc_lookups": (
        lambda s: float(sum(int(c["llc_hits"]) + int(c["llc_misses"]) for c in s["cha"])),
        "cha_llc_demand_accesses",
        "strict",
    ),
    "branch_misses": (lambda s: float(s["totals"]["branch_misses"]), "branch_misses", "strict"),
    "l1d_accesses": (lambda s: float(s["totals"]["l1d_accesses"]), "l1d_demand_accesses", "diagnostic"),
    "private_l2_accesses": (lambda s: float(s["totals"]["l2_accesses"]), "private_l2_demand_accesses", "diagnostic"),
    "branch_committed": (lambda s: float(s["totals"]["branches"]), "branch_committed", "diagnostic"),
    "dtlb_accesses": (lambda s: float(s["totals"]["dtlb_accesses"]), "dtlb_accesses", "diagnostic"),
    "dtlb_misses": (lambda s: float(s["totals"]["dtlb_misses"]), "dtlb_misses", "diagnostic"),
    "iq_full_events": (lambda s: float(s["totals"]["o3_iq_full_events"]), "iq_full_events", "diagnostic"),
    "rob_full_events": (lambda s: float(s["totals"]["o3_rob_full_events"]), "rob_full_events", "diagnostic"),
    "lsq_full_events": (
        lambda s: float(int(s["totals"]["o3_lq_full_events"]) + int(s["totals"]["o3_sq_full_events"])),
        "lsq_full_events",
        "diagnostic",
    ),
    "llc_tag_misses_vs_ruby": (lambda s: float(s["totals"]["llc_misses"]), "ruby_llc_demand_misses", "diagnostic"),
    "dram_reads": (lambda s: float(cha_sum(s, "dram_reads")), "dram_read_bursts", "diagnostic"),
    "dram_writes": (lambda s: float(cha_sum(s, "dram_writes")), "dram_write_bursts", "diagnostic"),
}

# Keep additional counters in JSON/CSV for offline diagnosis, but the formal
# human-facing PMU report is intentionally limited to the requested cache,
# branch, and CHA signals.
REPORTED_PMUS = (
    "l1d_misses",
    "private_l2_misses",
    "llc_tag_misses_vs_ruby",
    "branch_misses",
    "cha_llc_lookups",
)
PMU_LABELS = {
    "l1d_misses": "L1D misses",
    "private_l2_misses": "Private-L2 misses",
    "llc_tag_misses_vs_ruby": "LLC misses (FastSim tag vs gem5 Ruby)",
    "branch_misses": "Branch misses",
    "cha_llc_lookups": "CHA LLC lookups",
}


def case_rows(
    root: Path,
    fastsim_root: Path | None = None,
    workloads: set[str] | None = None,
) -> list[dict[str, Any]]:
    replay_root = fastsim_root or (root / "fastsim")
    rows: list[dict[str, Any]] = []
    for label_path in sorted(root.glob("labels/*/c*/W_*/metrics.json")):
        label = load(label_path)
        uarch = str(label["uarch"])
        workload = str(label["workload"])
        if workloads is not None and workload not in workloads:
            continue
        cores = int(label["cores"])
        replay_dir = replay_root / uarch / f"c{cores:02d}" / f"W_{workload}"
        stats_path = replay_dir / "fastsim-stats.json"
        if not stats_path.is_file():
            raise ValueError(f"missing FastSim result: {stats_path}")
        stats = load(stats_path)
        cycles = sum(int(core["cycles"]) for core in stats["cores"])
        uops = int(stats["totals"]["retired_uops"])
        instructions = int(stats["totals"]["retired_instructions"])
        fastsim_uop_cpi = cycles / uops
        fastsim_macro_cpi = cycles / instructions
        row: dict[str, Any] = {
            "uarch": uarch,
            "workload": workload,
            "domain": label["domain"],
            "cores": cores,
            "gem5_uop_cpi": float(label["aggregate_uop_cpi"]),
            "fastsim_uop_cpi": fastsim_uop_cpi,
            "uop_cpi_signed_error": fastsim_uop_cpi / float(label["aggregate_uop_cpi"]) - 1.0,
            "uop_cpi_absolute_error": abs(fastsim_uop_cpi / float(label["aggregate_uop_cpi"]) - 1.0),
            "gem5_macro_cpi": float(label["aggregate_macro_cpi"]),
            "fastsim_macro_cpi": fastsim_macro_cpi,
            "macro_cpi_signed_error": fastsim_macro_cpi / float(label["aggregate_macro_cpi"]) - 1.0,
            "macro_cpi_absolute_error": abs(fastsim_macro_cpi / float(label["aggregate_macro_cpi"]) - 1.0),
            "gem5_retired_uops": int(label["retired_uops"]),
            "fastsim_retired_uops": uops,
            "uop_delta": uops - int(label["retired_uops"]),
            "uops_per_second": float(stats["throughput"]["uops_per_second"]),
            "fastsim_stats": str(stats_path.resolve()),
            "gem5_metrics": str(label_path.resolve()),
        }
        for name, (predict, reference_key, scope) in PMUS.items():
            predicted = predict(stats)
            reference = float(label[reference_key])
            row[f"{name}_fastsim"] = predicted
            row[f"{name}_gem5"] = reference
            row[f"{name}_signed_error"] = relative(predicted, reference)
            row[f"{name}_scope"] = scope
        rows.append(row)
    baselines = {
        (row["workload"], row["cores"]): row
        for row in rows if row["uarch"] == "baseline"
    }
    for row in rows:
        baseline = baselines[(row["workload"], row["cores"])]
        row["gem5_speedup"] = baseline["gem5_uop_cpi"] / row["gem5_uop_cpi"]
        row["fastsim_speedup"] = baseline["fastsim_uop_cpi"] / row["fastsim_uop_cpi"]
        row["speedup_signed_error"] = row["fastsim_speedup"] / row["gem5_speedup"] - 1.0
        row["speedup_absolute_error"] = abs(row["speedup_signed_error"])
        if row["uarch"] == "baseline" or abs(row["gem5_speedup"] - 1.0) < 1e-12:
            row["direction_correct"] = None
        else:
            row["direction_correct"] = (
                (row["gem5_speedup"] - 1.0) * (row["fastsim_speedup"] - 1.0) > 0
            )
    return rows


def pmu_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, (_, _, scope) in PMUS.items():
        predicted = sum(float(row[f"{name}_fastsim"]) for row in rows)
        reference = sum(float(row[f"{name}_gem5"]) for row in rows)
        absolute = sum(
            abs(float(row[f"{name}_fastsim"]) - float(row[f"{name}_gem5"]))
            for row in rows
        )
        per_case = [
            abs(float(value))
            for row in rows
            if (value := row[f"{name}_signed_error"]) is not None
        ]
        result[name] = {
            "scope": scope,
            "gate_eligible": scope == "strict" and reference >= 10_000,
            "predicted_total": predicted,
            "reference_total": reference,
            "wape": absolute / reference if reference else None,
            "pooled_signed_error": predicted / reference - 1.0 if reference else None,
            "case_absolute_error": distribution(per_case),
        }
    return result


def group_distribution(
    rows: list[dict[str, Any]], key: str, metric: str
) -> dict[str, Any]:
    groups: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        groups[str(row[key])].append(float(row[metric]))
    return {group: distribution(values) for group, values in sorted(groups.items())}


def sensitivity_by_uarch(
    rows: list[dict[str, Any]], cpi_threshold: float, pmu_threshold: float
) -> dict[str, Any]:
    baseline = {
        (row["workload"], row["cores"]): row
        for row in rows if row["uarch"] == "baseline"
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["uarch"] == "baseline":
            continue
        groups[str(row["uarch"])].append(row)
    result: dict[str, Any] = {}
    for uarch, items in sorted(groups.items()):
        if uarch.startswith("dtlb"):
            metric = "dtlb_misses"
        elif uarch.startswith("l1d"):
            metric = "l1d_misses"
        elif uarch.startswith("l2_"):
            metric = "private_l2_misses"
        elif uarch.startswith("llc"):
            metric = "llc_tag_misses_vs_ruby"
        else:
            metric = "cpi_speedup"
        threshold = cpi_threshold if metric == "cpi_speedup" else pmu_threshold
        deltas: list[tuple[dict[str, Any], float, float]] = []
        for row in items:
            if metric == "cpi_speedup":
                gem5_delta = float(row["gem5_speedup"]) - 1.0
                fastsim_delta = float(row["fastsim_speedup"]) - 1.0
            else:
                base = baseline[(row["workload"], row["cores"])]
                gem5_base = float(base[f"{metric}_gem5"])
                fastsim_base = float(base[f"{metric}_fastsim"])
                if gem5_base == 0 or fastsim_base == 0:
                    continue
                gem5_delta = float(row[f"{metric}_gem5"]) / gem5_base - 1.0
                fastsim_delta = float(row[f"{metric}_fastsim"]) / fastsim_base - 1.0
            deltas.append((row, gem5_delta, fastsim_delta))
        material = [item for item in deltas if abs(item[1]) >= threshold]
        correct = sum(gem5 * fastsim > 0 for _, gem5, fastsim in material)
        result[uarch] = {
            "cases": len(items),
            "coverage_metric": metric,
            "coverage_threshold": threshold,
            "material_cases": len(material),
            "material_direction_correct": correct,
            "material_direction_accuracy": correct / len(material) if material else None,
            "max_abs_gem5_speedup_delta": max(
                (abs(gem5) for _, gem5, _ in deltas), default=0.0),
            "max_abs_fastsim_speedup_delta": max(
                (abs(fastsim) for _, _, fastsim in deltas), default=0.0),
        }
    return result


def uarch_cpi_ranking(
    rows: list[dict[str, Any]], materiality_threshold: float
) -> dict[str, Any]:
    """Score pairwise uarch CPI ordering within each workload/core group."""
    groups: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(str(row["workload"]), int(row["cores"]))].append(row)

    raw_pairs = raw_correct = 0
    material_pairs = material_correct = 0
    by_workload: dict[str, dict[str, int | float | None]] = {}
    for (workload, cores), group in sorted(groups.items()):
        group_pairs = group_correct = 0
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                gem5_delta = float(left["gem5_uop_cpi"]) - float(
                    right["gem5_uop_cpi"]
                )
                if gem5_delta == 0.0:
                    continue
                fastsim_delta = float(left["fastsim_uop_cpi"]) - float(
                    right["fastsim_uop_cpi"]
                )
                concordant = gem5_delta * fastsim_delta > 0.0
                raw_pairs += 1
                raw_correct += int(concordant)
                relative_delta = abs(gem5_delta) / min(
                    float(left["gem5_uop_cpi"]),
                    float(right["gem5_uop_cpi"]),
                )
                if relative_delta < materiality_threshold:
                    continue
                material_pairs += 1
                material_correct += int(concordant)
                group_pairs += 1
                group_correct += int(concordant)
        by_workload[f"{workload}/c{cores:02d}"] = {
            "pairs": group_pairs,
            "correct": group_correct,
            "accuracy": group_correct / group_pairs if group_pairs else None,
        }
    return {
        "materiality_threshold": materiality_threshold,
        "raw_pairs": raw_pairs,
        "raw_correct": raw_correct,
        "raw_accuracy": raw_correct / raw_pairs if raw_pairs else None,
        "material_pairs": material_pairs,
        "material_correct": material_correct,
        "material_accuracy": (
            material_correct / material_pairs if material_pairs else None
        ),
        "by_workload": by_workload,
    }


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.3f}%"


def markdown(report: dict[str, Any]) -> str:
    cpi = report["cpi_absolute_error_variants"]
    speedup = report["speedup_absolute_error_variants"]
    gates = report["gates"]
    ranking = report["uarch_cpi_ranking"]
    lines = [
        "# FastSim C4 微架构泛化验证结果",
        "",
        f"Cases: {report['cases']}（variant cases: {report['variant_cases']}）。",
        "",
        "## 总结",
        "",
        "| 指标 | 结果 | Gate |",
        "|---|---:|---:|",
        f"| Variant CPI mean | {pct(cpi['mean'])} | — |",
        f"| Variant CPI P90 | {pct(cpi['p90'])} | — |",
        f"| Variant CPI P99 | {pct(cpi['p99'])} | ≤ 10% |",
        f"| Uarch speedup error P90 | {pct(speedup['p90'])} | ≤ 10% |",
        f"| Uarch speedup error P99 / max | {pct(speedup['p99'])} / {pct(speedup['max'])} | diagnostic |",
        f"| 原始方向准确率 | {pct(report['raw_direction_accuracy'])} | diagnostic |",
        f"| 有效变化方向准确率（|gem5 speedup−1| ≥ {pct(report['direction_materiality_threshold'])}） | "
        f"{pct(report['material_direction_accuracy'])} | ≥ 90% |",
        f"| 微架构 CPI 排序准确率（pairwise，gem5 CPI 差异 ≥ "
        f"{pct(ranking['materiality_threshold'])}） | "
        f"{pct(ranking['material_accuracy'])} "
        f"({ranking['material_correct']}/{ranking['material_pairs']}) | ≥ 90% |",
        f"| 原始微架构 CPI 排序准确率 | {pct(ranking['raw_accuracy'])} "
        f"({ranking['raw_correct']}/{ranking['raw_pairs']}) | diagnostic |",
        f"| 参数激励覆盖 | {len(report['underexcited_uarches'])} 个 uarch 未达标 | "
        f"每个 uarch ≥ {report['minimum_material_cases_per_uarch']} 个有效 cases |",
        f"| 最低吞吐量 | {report['minimum_uops_per_second'] / 1e6:.3f} M UOP/s | ≥ 5 M |",
        f"| 总体 gate | {'PASS' if gates['overall'] else 'FAIL'} | — |",
        "",
        "## PMU count error（variant WAPE）",
        "",
        "| PMU | Scope | WAPE | pooled signed | Gate |",
        "|---|---|---:|---:|---:|",
    ]
    for name in REPORTED_PMUS:
        item = report["pmu_variants"][name]
        if item["scope"] != "strict":
            gate = "diagnostic"
        elif item["gate_eligible"]:
            gate = "≤ 2%"
        else:
            gate = "n/a (<10k refs)"
        lines.append(
            f"| {PMU_LABELS[name]} | {item['scope']} | {pct(item['wape'])} | "
            f"{pct(item['pooled_signed_error'])} | {gate} |"
        )
    lines.extend(
        [
            "",
            "## 各 uarch CPI absolute error",
            "",
            "| Uarch | mean | P90 | max |",
            "|---|---:|---:|---:|",
        ]
    )
    for uarch, item in report["cpi_by_uarch"].items():
        lines.append(
            f"| `{uarch}` | {pct(item['mean'])} | {pct(item['p90'])} | {pct(item['max'])} |"
        )
    lines.extend(
        [
            "",
            "## 各 uarch speedup absolute error",
            "",
            "| Uarch | mean | P90 | max |",
            "|---|---:|---:|---:|",
        ]
    )
    for uarch, item in report["speedup_error_by_uarch"].items():
        lines.append(
            f"| `{uarch}` | {pct(item['mean'])} | {pct(item['p90'])} | {pct(item['max'])} |"
        )
    lines.extend(
        [
            "",
            "## 参数激励覆盖",
            "",
            "| Uarch | 覆盖指标 | gem5 有效变化 cases | 方向正确 | max gem5 delta | max FastSim delta |",
            "|---|---|---:|---:|---:|---:|",
        ]
    )
    for uarch, item in report["sensitivity_by_uarch"].items():
        lines.append(
            f"| `{uarch}` | `{item['coverage_metric']}` | "
            f"{item['material_cases']}/{item['cases']} | "
            f"{item['material_direction_correct']}/{item['material_cases']} | "
            f"{pct(item['max_abs_gem5_speedup_delta'])} | "
            f"{pct(item['max_abs_fastsim_speedup_delta'])} |"
        )
    lines.extend(
        [
            "",
            "激励覆盖 gate 会阻止少量偶然命中的 case 被误报为泛化 PASS。未达标："
            + (", ".join(f"`{name}`" for name in report["underexcited_uarches"])
               if report["underexcited_uarches"] else "无")
            + "。",
            "",
            "说明：strict PMU 可按相近计数语义验收；diagnostic 项受退休态 trace 缺少 "
            "wrong-path、Ruby 协议事件或 LLC/DRAM 统计口径差异影响，不进入总体 gate。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("tmp/uarch-c4-first-batch"))
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--fastsim-root",
        type=Path,
        help="alternate FastSim replay directory (default: ROOT/fastsim)",
    )
    parser.add_argument(
        "--workload",
        action="append",
        help=(
            "evaluate only this workload (repeatable); useful for targeted "
            "partial replay directories"
        ),
    )
    parser.add_argument(
        "--direction-materiality-threshold",
        type=float,
        default=0.005,
        help="minimum absolute gem5 speedup delta for direction scoring (default: 0.005)",
    )
    parser.add_argument(
        "--min-material-cases-per-uarch",
        type=int,
        default=2,
        help=(
            "minimum number of gem5-material cases required for every "
            "non-baseline profile (default: 2)"
        ),
    )
    parser.add_argument(
        "--pmu-materiality-threshold",
        type=float,
        default=0.05,
        help="minimum PMU count-rate delta for cache/TLB excitation (default: 0.05)",
    )
    parser.add_argument(
        "--ranking-materiality-threshold",
        type=float,
        default=0.005,
        help=(
            "minimum pairwise gem5 CPI delta for uarch ranking scoring "
            "(default: 0.005)"
        ),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    out = (args.out or (root / "evaluation")).resolve()
    fastsim_root = args.fastsim_root.resolve() if args.fastsim_root else None
    rows = case_rows(
        root, fastsim_root,
        set(args.workload) if args.workload else None,
    )
    variants = [row for row in rows if row["uarch"] != "baseline"]
    directions = [row["direction_correct"] for row in variants if row["direction_correct"] is not None]
    material_directions = [
        row["direction_correct"]
        for row in variants
        if row["direction_correct"] is not None
        and abs(float(row["gem5_speedup"]) - 1.0)
        >= args.direction_materiality_threshold
    ]
    cpi_variants = distribution([float(row["uop_cpi_absolute_error"]) for row in variants])
    speedup_variants = distribution([float(row["speedup_absolute_error"]) for row in variants])
    raw_direction_accuracy = (
        sum(bool(value) for value in directions) / len(directions)
        if directions else None
    )
    material_direction_accuracy = (
        sum(bool(value) for value in material_directions) / len(material_directions)
        if material_directions else None
    )
    pmu_variants = pmu_summary(variants)
    minimum_throughput = min(float(row["uops_per_second"]) for row in rows)
    sensitivity = sensitivity_by_uarch(
        rows, args.direction_materiality_threshold,
        args.pmu_materiality_threshold,
    )
    ranking = uarch_cpi_ranking(rows, args.ranking_materiality_threshold)
    underexcited = sorted(
        uarch for uarch, item in sensitivity.items()
        if int(item["material_cases"]) < args.min_material_cases_per_uarch
    )
    gates = {
        "cpi_p99_le_10pct": bool(cpi_variants["p99"] is not None and cpi_variants["p99"] <= 0.10),
        "speedup_p90_le_10pct": bool(speedup_variants["p90"] is not None and speedup_variants["p90"] <= 0.10),
        "material_direction_accuracy_ge_90pct": bool(
            material_direction_accuracy is not None
            and material_direction_accuracy >= 0.90
        ),
        "uarch_cpi_ranking_accuracy_ge_90pct": bool(
            ranking["material_accuracy"] is not None
            and float(ranking["material_accuracy"]) >= 0.90
        ),
        "minimum_material_cases_per_uarch": not underexcited,
        "minimum_throughput_ge_5m_uops": minimum_throughput >= 5_000_000,
    }
    for name in ("l1d_misses", "private_l2_misses", "cha_llc_lookups", "branch_misses"):
        if pmu_variants[name]["gate_eligible"]:
            gates[f"{name}_wape_le_2pct"] = bool(
                pmu_variants[name]["wape"] is not None
                and pmu_variants[name]["wape"] <= 0.02
            )
    gates["overall"] = all(gates.values())
    report = {
        "schema": "fastsim-uarch-generalization-evaluation-v2",
        "root": str(root),
        "fastsim_root": str(fastsim_root or (root / "fastsim")),
        "cases": len(rows),
        "variant_cases": len(variants),
        "cpi_absolute_error_all": distribution([float(row["uop_cpi_absolute_error"]) for row in rows]),
        "cpi_absolute_error_variants": cpi_variants,
        "speedup_absolute_error_variants": speedup_variants,
        "raw_direction_accuracy": raw_direction_accuracy,
        "raw_direction_cases": len(directions),
        "direction_materiality_threshold": args.direction_materiality_threshold,
        "material_direction_accuracy": material_direction_accuracy,
        "material_direction_cases": len(material_directions),
        "minimum_material_cases_per_uarch": args.min_material_cases_per_uarch,
        "pmu_materiality_threshold": args.pmu_materiality_threshold,
        "underexcited_uarches": underexcited,
        "minimum_uops_per_second": minimum_throughput,
        "uarch_cpi_ranking": ranking,
        "pmu_all": pmu_summary(rows),
        "pmu_variants": pmu_variants,
        "cpi_by_uarch": group_distribution(rows, "uarch", "uop_cpi_absolute_error"),
        "cpi_by_domain": group_distribution(rows, "domain", "uop_cpi_absolute_error"),
        "speedup_error_by_uarch": group_distribution(
            variants, "uarch", "speedup_absolute_error"
        ),
        "speedup_error_by_domain": group_distribution(
            variants, "domain", "speedup_absolute_error"
        ),
        "sensitivity_by_uarch": sensitivity,
        "gates": gates,
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "generalization-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    fields = list(rows[0])
    with (out / "cases.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (out / "generalization-report.md").write_text(markdown(report), encoding="utf-8")
    print(
        f"evaluated cases={len(rows)} variants={len(variants)} "
        f"overall={'PASS' if gates['overall'] else 'FAIL'} out={out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
