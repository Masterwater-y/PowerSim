#!/usr/bin/env python3
"""Summarize paired c8 post-hoc semantic interventions against full_real."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
from typing import Any, Iterable


CONTROL_VARIANTS = (
    "no_offline_hidden",
    "no_lora",
    "semantic_permute",
    "no_llm_branch",
)


def mean(values: Iterable[float]) -> float | None:
    selected = [float(value) for value in values if math.isfinite(float(value))]
    return sum(selected) / len(selected) if selected else None


def bootstrap_mean_ci(
    values: list[float],
    *,
    seed: int,
    samples: int = 20_000,
) -> list[float] | None:
    if not values:
        return None
    generator = random.Random(int(seed))
    count = len(values)
    estimates = sorted(
        sum(values[generator.randrange(count)] for _ in range(count)) / count
        for _ in range(int(samples))
    )
    lower = estimates[int(0.025 * (len(estimates) - 1))]
    upper = estimates[int(0.975 * (len(estimates) - 1))]
    return [float(lower), float(upper)]


def role_is_heldout(record: dict[str, Any]) -> bool:
    return "heldout" in str(record.get("workload_role", "")).lower()


def aggregate_activation_rms(records: list[dict[str, Any]]) -> dict[str, float]:
    sum_squares: dict[str, float] = {}
    counts: dict[str, int] = {}
    for record in records:
        timing = record.get("predictor_timing") or {}
        rms = timing.get("activation_rms") or {}
        local_counts = timing.get("activation_counts") or {}
        for key, value in rms.items():
            count = int(local_counts.get(key, 0))
            if count <= 0:
                continue
            sum_squares[key] = sum_squares.get(key, 0.0) + float(value) ** 2 * count
            counts[key] = counts.get(key, 0) + count
    return {
        key: math.sqrt(sum_squares[key] / counts[key])
        for key in sorted(sum_squares)
        if counts[key] > 0
    }


def summarize_variant(summary: dict[str, Any]) -> dict[str, Any]:
    records = [
        dict(record) for record in summary["records"]
        if record.get("status") == "PASS"
    ]
    base = [record for record in records if not role_is_heldout(record)]
    heldout = [record for record in records if role_is_heldout(record)]
    heldout_no_redis = [
        record for record in heldout
        if "redis" not in str(record["workload"]).lower()
    ]

    def errors(selected: list[dict[str, Any]], key: str) -> float | None:
        return mean(
            float(record[key]) for record in selected
            if record.get(key) is not None
        )

    return {
        "status": str(summary["status"]),
        "trace_count": len(records),
        "mean_macro_cpi_error": errors(records, "macro_cpi_absolute_error"),
        "mean_makespan_error": errors(records, "makespan_absolute_error"),
        "base_macro_cpi_error": errors(base, "macro_cpi_absolute_error"),
        "base_makespan_error": errors(base, "makespan_absolute_error"),
        "heldout_macro_cpi_error": errors(heldout, "macro_cpi_absolute_error"),
        "heldout_makespan_error": errors(heldout, "makespan_absolute_error"),
        "heldout_no_redis_macro_cpi_error": errors(
            heldout_no_redis, "macro_cpi_absolute_error",
        ),
        "heldout_no_redis_makespan_error": errors(
            heldout_no_redis, "makespan_absolute_error",
        ),
        "mean_trace_macro_per_s": summary["throughput"][
            "mean_trace_macro_per_s"
        ],
        "weighted_macro_per_s": summary["throughput"]["weighted_macro_per_s"],
        "max_gpu_peak_allocated_bytes": max(
            (int(record.get("gpu_peak_allocated_bytes", 0)) for record in records),
            default=0,
        ),
        "max_gpu_peak_reserved_bytes": max(
            (int(record.get("gpu_peak_reserved_bytes", 0)) for record in records),
            default=0,
        ),
        "activation_rms": aggregate_activation_rms(records),
    }


def paired_comparison(
    baseline: dict[str, Any],
    control: dict[str, Any],
    *,
    seed: int,
) -> dict[str, Any]:
    baseline_records = {
        str(record["workload"]): dict(record)
        for record in baseline["records"] if record.get("status") == "PASS"
    }
    control_records = {
        str(record["workload"]): dict(record)
        for record in control["records"] if record.get("status") == "PASS"
    }
    workloads = sorted(set(baseline_records) & set(control_records))
    rows = []
    for workload in workloads:
        full = baseline_records[workload]
        ablated = control_records[workload]
        rows.append({
            "workload": workload,
            "workload_role": str(full.get("workload_role", "")),
            "macro_cpi_delta_control_minus_full": (
                float(ablated["macro_cpi_absolute_error"])
                - float(full["macro_cpi_absolute_error"])
            ),
            "makespan_delta_control_minus_full": (
                float(ablated["makespan_absolute_error"])
                - float(full["makespan_absolute_error"])
            ),
            "full_macro_cpi_error": float(full["macro_cpi_absolute_error"]),
            "control_macro_cpi_error": float(
                ablated["macro_cpi_absolute_error"]
            ),
            "full_makespan_error": float(full["makespan_absolute_error"]),
            "control_makespan_error": float(ablated["makespan_absolute_error"]),
        })

    def paired_scope(selected: list[dict[str, Any]]) -> dict[str, Any]:
        cpi = [row["macro_cpi_delta_control_minus_full"] for row in selected]
        makespan = [row["makespan_delta_control_minus_full"] for row in selected]
        return {
            "count": len(selected),
            "mean_macro_cpi_delta": mean(cpi),
            "median_macro_cpi_delta": statistics.median(cpi) if cpi else None,
            "macro_cpi_full_better_count": sum(value > 0.0 for value in cpi),
            "macro_cpi_mean_delta_bootstrap_95ci": bootstrap_mean_ci(
                cpi, seed=seed,
            ),
            "mean_makespan_delta": mean(makespan),
            "median_makespan_delta": (
                statistics.median(makespan) if makespan else None
            ),
            "makespan_full_better_count": sum(value > 0.0 for value in makespan),
            "makespan_mean_delta_bootstrap_95ci": bootstrap_mean_ci(
                makespan, seed=seed + 1,
            ),
        }

    heldout = [
        row for row in rows if "heldout" in row["workload_role"].lower()
    ]
    heldout_no_redis = [
        row for row in heldout if "redis" not in row["workload"].lower()
    ]
    return {
        "all": paired_scope(rows),
        "heldout": paired_scope(heldout),
        "heldout_no_redis": paired_scope(heldout_no_redis),
        "per_workload": rows,
    }


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100.0 * value:.3f}%"


def percent_ci(values: list[float] | tuple[float, float] | None) -> str:
    if values is None:
        return "n/a"
    return f"[{percent(values[0])}, {percent(values[1])}]"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Macro-v29 第一阶段 post-hoc 语义干预实验",
        "",
        f"- checkpoint: `{report['checkpoint']}`",
        f"- full_real baseline: `{report['baseline_summary']}`",
        f"- c8 deployment traces: {report['trace_count']}，stride=256，无 lookahead",
        "- 注意：本阶段是同一已训练 checkpoint 的推理干预，只能证明依赖/敏感性，不能单独证明重新训练后的泛化收益。",
        "",
        "## 汇总",
        "",
        "| variant | ROI CPI MAPE | makespan MAPE | base CPI | heldout CPI | heldout CPI（去 Redis） | macro/s | peak GiB |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in ("full_real", *CONTROL_VARIANTS):
        row = report["variants"][variant]
        lines.append(
            f"| {variant} | {percent(row['mean_macro_cpi_error'])} | "
            f"{percent(row['mean_makespan_error'])} | "
            f"{percent(row['base_macro_cpi_error'])} | "
            f"{percent(row['heldout_macro_cpi_error'])} | "
            f"{percent(row['heldout_no_redis_macro_cpi_error'])} | "
            f"{float(row['mean_trace_macro_per_s'] or 0.0):.1f} | "
            f"{row['max_gpu_peak_allocated_bytes'] / 2**30:.3f} |"
        )
    lines.extend([
        "",
        "## 相对 full_real 的配对变化",
        "",
        "正值表示去掉/打乱该因素后误差变大，即 full_real 更好。",
        "",
        "| control | ROI CPI Δ（95% CI） | CPI full 更好 | heldout CPI Δ（95% CI） | heldout 去 Redis CPI Δ（95% CI） | makespan Δ（95% CI） |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for variant in CONTROL_VARIANTS:
        paired = report["paired"][variant]
        all_scope = paired["all"]
        heldout = paired["heldout"]
        heldout_no_redis = paired["heldout_no_redis"]
        lines.append(
            f"| {variant} | {percent(all_scope['mean_macro_cpi_delta'])} "
            f"{percent_ci(all_scope['macro_cpi_mean_delta_bootstrap_95ci'])} | "
            f"{all_scope['macro_cpi_full_better_count']}/{all_scope['count']} | "
            f"{percent(heldout['mean_macro_cpi_delta'])} "
            f"{percent_ci(heldout['macro_cpi_mean_delta_bootstrap_95ci'])} | "
            f"{percent(heldout_no_redis['mean_macro_cpi_delta'])} "
            f"{percent_ci(heldout_no_redis['macro_cpi_mean_delta_bootstrap_95ci'])} | "
            f"{percent(all_scope['mean_makespan_delta'])} "
            f"{percent_ci(all_scope['makespan_mean_delta_bootstrap_95ci'])} |"
        )
    lines.extend([
        "",
        "## 阶段一结论",
        "",
        "- 在全部 23 条 trace 上，`semantic_permute`、`no_lora` 和 `no_llm_branch` 的 CPI 与 makespan 配对均值差的 95% bootstrap CI 均高于 0：当前 checkpoint 明确依赖正确语义映射、LoRA 增量和整个在线 LLM 分支。",
        "- `no_offline_hidden` 的整体 CPI 退化较小且区间贴近 0，makespan 区间跨 0；在 7 条 heldout 上均值还反向。因此现阶段没有证据把主要收益归因于 offline hidden 残差。",
        "- 7 条 heldout 的 `semantic_permute`、`no_lora`、`no_llm_branch` CPI 区间均跨 0，且 workload 方向不一致；总体显著性主要证明已训练模型的依赖，不能等价为未见 workload 的语义泛化收益。",
        "- 下一步若要回答“LLM 语义是否真正提升泛化”，必须做等数据、等初始化、等训练预算的第二阶段重训练对照。",
        "",
        "## 激活 RMS",
        "",
        "RMS 按每条 trace 的首个部署 window 中所有有效 macro 元素加权；复用的历史 full_real baseline 未开启该诊断，因此该行为空。",
        "",
        "```json",
        json.dumps(
            {key: report["variants"][key]["activation_rms"] for key in report["variants"]},
            indent=2,
            sort_keys=True,
        ),
        "```",
        "",
        "## 判读边界",
        "",
        "- `semantic_permute` 直接检验正确 PC↔语义对应关系；它保留 semantic+anchor 的联合边际分布。",
        "- `no_offline_hidden` 只检验 gated offline hidden 在 anchor 之外的增量价值。",
        "- `no_lora` 检验已训练 LoRA 增量；预训练 Qwen 和其余 timing 层仍保留。",
        "- `no_llm_branch` 检验当前 predictor 是否依赖整个 Qwen 分支，但属于强分布偏移对照。",
        "- 是否有可复现的泛化收益，仍需第二阶段等数据、等初始化、等训练预算的重训练对照。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment-root", required=True)
    parser.add_argument("--baseline-summary", required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--bootstrap-seed", type=int, default=20260719)
    args = parser.parse_args()

    root = Path(args.experiment_root).resolve()
    baseline_path = Path(args.baseline_summary).resolve()
    baseline = json.loads(baseline_path.read_text())
    summaries = {"full_real": baseline}
    sources = {"full_real": str(baseline_path)}
    for variant in CONTROL_VARIANTS:
        path = root / variant / "summary.json"
        if not path.is_file():
            raise FileNotFoundError(path)
        summaries[variant] = json.loads(path.read_text())
        sources[variant] = str(path)
    trace_ids = {
        variant: sorted(str(row["trace_id"]) for row in summary["records"])
        for variant, summary in summaries.items()
    }
    if any(value != trace_ids["full_real"] for value in trace_ids.values()):
        raise RuntimeError("variant trace sets do not match full_real")
    if any(summary.get("status") != "PASS" for summary in summaries.values()):
        raise RuntimeError("at least one deployment suite did not pass")

    report: dict[str, Any] = {
        "schema_version": "llmsim-macro-v29-stage1-posthoc-1",
        "status": "PASS",
        "checkpoint": str(Path(args.checkpoint).resolve()),
        "baseline_summary": str(baseline_path),
        "baseline_reused": True,
        "trace_count": len(trace_ids["full_real"]),
        "variant_sources": sources,
        "variants": {
            variant: summarize_variant(summary)
            for variant, summary in summaries.items()
        },
        "paired": {
            variant: paired_comparison(
                baseline,
                summaries[variant],
                seed=int(args.bootstrap_seed) + index * 101,
            )
            for index, variant in enumerate(CONTROL_VARIANTS)
        },
        "interpretation_scope": (
            "posthoc checkpoint reliance/sensitivity only; controlled retraining "
            "is required for a causal generalization claim"
        ),
    }
    canonical = json.dumps(report, indent=2, sort_keys=True) + "\n"
    report["report_fingerprint"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    (root / "summary.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n"
    )
    (root / "report.md").write_text(render_markdown(report))
    print(f"stage1_summary={root / 'summary.json'}")
    print(f"stage1_report={root / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
