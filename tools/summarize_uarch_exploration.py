#!/usr/bin/env python3
"""Create a meeting-ready summary for single- and multi-factor uarch ranking."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.3f}%"


def profile_class(profile: dict[str, Any]) -> str:
    explicit = profile.get("design_class")
    if explicit:
        return str(explicit)
    changed = len(profile.get("gem5", {}))
    return "baseline" if changed == 0 else "single" if changed == 1 else "multi"


def add_pair(counter: dict[str, int], concordant: bool, material: bool) -> None:
    counter["raw_pairs"] += 1
    counter["raw_correct"] += int(concordant)
    if material:
        counter["material_pairs"] += 1
        counter["material_correct"] += int(concordant)


def finish(counter: dict[str, int]) -> dict[str, int | float | None]:
    raw_pairs = counter["raw_pairs"]
    material_pairs = counter["material_pairs"]
    return {
        **counter,
        "raw_accuracy": counter["raw_correct"] / raw_pairs if raw_pairs else None,
        "material_accuracy": (
            counter["material_correct"] / material_pairs if material_pairs else None
        ),
    }


def ranking_slices(
    rows: list[dict[str, str]], classes: dict[str, str], threshold: float
) -> dict[str, dict[str, int | float | None]]:
    groups: dict[tuple[str, int], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        groups[(row["workload"], int(row["cores"]))].append(row)
    names = ("all", "ofat_only", "involves_multi", "single_multi", "multi_multi")
    counters = {
        name: {"raw_pairs": 0, "raw_correct": 0, "material_pairs": 0, "material_correct": 0}
        for name in names
    }
    for group in groups.values():
        for left_index, left in enumerate(group):
            for right in group[left_index + 1 :]:
                gem5_left = float(left["gem5_uop_cpi"])
                gem5_right = float(right["gem5_uop_cpi"])
                gem5_delta = gem5_left - gem5_right
                if gem5_delta == 0.0:
                    continue
                fastsim_delta = float(left["fastsim_uop_cpi"]) - float(
                    right["fastsim_uop_cpi"]
                )
                concordant = gem5_delta * fastsim_delta > 0.0
                material = abs(gem5_delta) / min(gem5_left, gem5_right) >= threshold
                left_multi = classes[left["uarch"]] == "multi"
                right_multi = classes[right["uarch"]] == "multi"
                add_pair(counters["all"], concordant, material)
                if not left_multi and not right_multi:
                    add_pair(counters["ofat_only"], concordant, material)
                else:
                    add_pair(counters["involves_multi"], concordant, material)
                if left_multi != right_multi:
                    add_pair(counters["single_multi"], concordant, material)
                elif left_multi and right_multi:
                    add_pair(counters["multi_multi"], concordant, material)
    return {name: finish(counter) for name, counter in counters.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    matrix = load(args.matrix.resolve())
    evaluation = args.evaluation.resolve()
    report = load(evaluation / "generalization-report.json")
    with (evaluation / "cases.csv").open(newline="", encoding="utf-8") as source:
        rows = list(csv.DictReader(source))
    classes = {profile["id"]: profile_class(profile) for profile in matrix["profiles"]}
    class_counts = {
        name: sum(value == name for value in classes.values())
        for name in ("baseline", "single", "multi")
    }
    threshold = float(report["uarch_cpi_ranking"]["materiality_threshold"])
    slices = ranking_slices(rows, classes, threshold)
    result = {
        "schema": "fastsim-uarch-exploration-summary-v1",
        "matrix": str(args.matrix.resolve()),
        "evaluation": str(evaluation),
        "workloads": [item["name"] for item in matrix["workloads"]],
        "core_counts": matrix["core_counts"],
        "profile_counts": class_counts,
        "cases": report["cases"],
        "variant_cases": report["variant_cases"],
        "ranking_slices": slices,
        "headline": {
            "variant_cpi": report["cpi_absolute_error_variants"],
            "variant_speedup": report["speedup_absolute_error_variants"],
            "material_direction_accuracy": report["material_direction_accuracy"],
            "minimum_uops_per_second": report["minimum_uops_per_second"],
            "gates": report["gates"],
        },
        "by_workload": report["uarch_cpi_ranking"]["by_workload"],
    }
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    lines = [
        "# SPEC2026 C4/C8 单变量与多变量微架构排序总结",
        "",
        f"- Workloads: {len(result['workloads'])}",
        f"- Profiles: {sum(class_counts.values())} "
        f"(baseline={class_counts['baseline']}, single={class_counts['single']}, "
        f"multi={class_counts['multi']})",
        f"- Cases: {report['cases']}",
        f"- Materiality threshold: {pct(threshold)}",
        "",
        "## 排序准确率",
        "",
        "| Slice | Material correct/pairs | Material accuracy | Raw correct/pairs | Raw accuracy |",
        "|---|---:|---:|---:|---:|",
    ]
    labels = {
        "all": "全部配置",
        "ofat_only": "baseline + 单变量",
        "involves_multi": "至少一端为多变量",
        "single_multi": "单变量/基线 vs 多变量",
        "multi_multi": "多变量 vs 多变量",
    }
    for name in ("all", "ofat_only", "involves_multi", "single_multi", "multi_multi"):
        item = slices[name]
        lines.append(
            f"| {labels[name]} | {item['material_correct']}/{item['material_pairs']} | "
            f"{pct(item['material_accuracy'])} | {item['raw_correct']}/{item['raw_pairs']} | "
            f"{pct(item['raw_accuracy'])} |"
        )
    cpi = report["cpi_absolute_error_variants"]
    speedup = report["speedup_absolute_error_variants"]
    lines.extend(
        [
            "",
            "## 精度与门禁",
            "",
            f"- Variant CPI APE mean/P90/P99: {pct(cpi['mean'])} / "
            f"{pct(cpi['p90'])} / {pct(cpi['p99'])}",
            f"- Speedup error mean/P90/P99: {pct(speedup['mean'])} / "
            f"{pct(speedup['p90'])} / {pct(speedup['p99'])}",
            f"- Material direction accuracy: "
            f"{pct(report['material_direction_accuracy'])}",
            f"- Minimum throughput: {report['minimum_uops_per_second'] / 1e6:.3f} M UOP/s",
            f"- Overall gate: {'PASS' if report['gates']['overall'] else 'FAIL'}",
            "",
            "## 各 workload/core 排序",
            "",
            "| Workload/core | Correct/pairs | Accuracy |",
            "|---|---:|---:|",
        ]
    )
    for name, item in sorted(report["uarch_cpi_ranking"]["by_workload"].items()):
        lines.append(
            f"| `{name}` | {item['correct']}/{item['pairs']} | {pct(item['accuracy'])} |"
        )
    (args.out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {args.out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
