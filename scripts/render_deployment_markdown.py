#!/usr/bin/env python3
"""Render a Chinese Markdown deployment-evaluation report from report.json."""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence


Q = chr(96)


def finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def values(rows: Iterable[Mapping[str, Any]], key: str) -> List[float]:
    result = []
    for row in rows:
        value = finite(row.get(key))
        if value is not None:
            result.append(value)
    return result


def branch_relative(row: Mapping[str, Any]) -> float | None:
    """Read the canonical metric, with compatibility for historical JSON."""
    for key in ("branch_miss_relative_error", "branch_miss_rate_relative_error"):
        value = finite(row.get(key))
        if value is not None:
            return value
    return None


def branch_relative_values(rows: Iterable[Mapping[str, Any]]) -> List[float]:
    result: List[float] = []
    for row in rows:
        value = branch_relative(row)
        if value is not None:
            result.append(value)
    return result


def mean(numbers: Sequence[float]) -> float:
    return sum(numbers) / len(numbers) if numbers else float("nan")


def percentile(numbers: Sequence[float], quantile: float) -> float:
    ordered = sorted(numbers)
    if not ordered:
        return float("nan")
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return (
        ordered[lower] * (upper - position)
        + ordered[upper] * (position - lower)
    )


def percent(value: Any, precision: int = 2) -> str:
    number = finite(value)
    return "-" if number is None else f"{100.0 * number:.{precision}f}%"


def number(value: Any, precision: int = 4) -> str:
    parsed = finite(value)
    return "-" if parsed is None else f"{parsed:.{precision}f}"


def count_number(value: Any, precision: int = 2) -> str:
    parsed = finite(value)
    return "-" if parsed is None else f"{parsed:,.{precision}f}"


def percentage_points(value: Any) -> str:
    parsed = finite(value)
    return "-" if parsed is None else f"{100.0 * parsed:.2f} pp"


def set_name(row: Mapping[str, Any]) -> str:
    return (
        "heldout"
        if str(row.get("workload", "")).endswith("_heldout")
        else "train/base"
    )


def select_set(
    rows: Sequence[Mapping[str, Any]], name: str
) -> List[Mapping[str, Any]]:
    if name == "all":
        return list(rows)
    return [row for row in rows if set_name(row) == name]


def relative_link(target: str, output: str) -> str:
    return os.path.relpath(os.path.abspath(target), os.path.dirname(os.path.abspath(output)))


def render(report: Mapping[str, Any], source: str, output: str) -> str:
    rows = [
        row for row in report.get("traces", [])
        if isinstance(row, Mapping)
    ]
    grouped: Dict[int, List[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[int(row.get("n_cores", 0))].append(row)
    for core_rows in grouped.values():
        core_rows.sort(key=lambda row: str(row.get("workload", "")))

    run = report.get("run", {})
    aggregate = report.get("aggregate", {})
    source_link = relative_link(source, output)
    text_path = os.path.join(os.path.dirname(os.path.abspath(source)), "report.txt")
    text_link = relative_link(text_path, output)
    lines: List[str] = []
    add = lines.append

    add("# TCSim v28 seed0 部署侧推理评估报告")
    add("")
    add("> 生成日期：2026-07-15  ")
    add("> 评估范围：seed0，c4/c8/c16/c32，16 个 train/base 负载 + 7 个业务 heldout 负载  ")
    add("> 主要结论：训练分布上的 ROI UOP-CPI 误差约为 2.77%–3.13%，但 heldout 为 17.51%–18.81%；当前瓶颈是业务分布泛化，而不是核心数扩展。")
    add("")
    add("## 1. 实验配置与完整性")
    add("")
    add("| 项目 | 配置 |")
    add("|---|---|")
    add(f"| checkpoint | {Q}{run.get('checkpoint', '-')}{Q} |")
    add("| checkpoint step | 30000（best.infer.pt） |")
    add(f"| split | {Q}{run.get('split', '-')}{Q} |")
    add("| 核心数 | 4、8、16、32 |")
    add("| 每个核心数负载 | 23：train/base 16 + heldout 7 |")
    add(f"| trace 总数 | {len(rows)} |")
    add(f"| chunk 总数 | {int(aggregate.get('n_chunks', 0)):,} |")
    add(f"| ROI UOP | {int(aggregate.get('roi_uops', 0)):,} |")
    add(
        "| 有效标签覆盖率 | "
        f"{100.0 * float(aggregate.get('roi_label_coverage', 0.0)):.8f}%"
        f"（仅 {int(aggregate.get('n_invalid_cycle_labels', 0))} 个无效 cycle label） |"
    )
    add("| 固定窗口 | K=256 UOP/core |")
    add("| 调度阈值 | epsilon=2048 cycles |")
    add("| 推理精度/后端 | BF16，SDPA=auto |")
    add("| 推理设备 | 本机 8 GPU 并行，不同 trace 分配到不同 GPU |")
    add("")
    add(
        f"部署侧评估不读取 oracle {Q}rollout.jsonl{Q} 作为模型上下文。"
        "每次模型调用使用当前活跃核心的完整 full-QKVR 上下文；输出只在 chunk "
        "首次加载时锁存，resident chunk 后续重复参与上下文但不会重复锁存或累计。"
        "真实 cycle 与 branch miss 只用于评估标签。oracle scheduler 仅作为调度集合"
        "对照，不参与主预测轨迹。"
    )
    add("")
    add("原始产物：")
    add("")
    add(f"- [完整 JSON 报告]({source_link})")
    add(f"- [TSim 风格文字报告]({text_link})")
    add("- [部署推理机制说明](deployment_inference.md)")
    add("")

    add("## 2. 指标口径")
    add("")
    add("| 指标 | 定义 | 主用途 |")
    add("|---|---|---|")
    add(f"| ROI UOP CPI | {Q}sum(all-core cycles) / sum(all-core micro-ops){Q} | 当前主 CPI；先跨核求和再相除 |")
    add(f"| ROI macro-instruction CPI | {Q}sum(all-core cycles) / sum(all-core macro instructions){Q} | 宏指令口径 CPI；不是跨核/跨负载平均 |")
    add(f"| ROI UOP-CPI error | {Q}abs(pred ROI UOP CPI - true ROI UOP CPI) / true ROI UOP CPI{Q} | 完整 trace 的 CPI 精度 |")
    add("| Scheduler-window CPI MAPE | 每个 epsilon scheduler step 中，本次提交 chunks 的聚合 CPI 误差，再对 step 求平均 | 部署调度局部精度 |")
    add("| Chunk CPI MAPE | 每个核心、每个固定 256-UOP chunk 的 CPI MAPE；resident 重复出现不重复计数 | 最局部的预测诊断 |")
    add("| Branch miss relative error | 完整 workload trace 内先跨核累加 miss，再计算一个相对误差；count-relative 与 rate-relative 完全相同 | 分支预测主误差 |")
    add("| Branch abs pp | 预测与真实 branch-miss rate 的绝对百分点差 | 避免低 miss-rate 时相对误差被放大 |")
    add("")
    add("### 2.1 CPI 的跨核汇总")
    add("")
    add("对一个 workload 的完整 trace，当前主 CPI 定义为：")
    add("")
    add(f"{Q}{Q}{Q}text")
    add("ROI UOP CPI = sum(每个核的 cycles 推进) / sum(每个核的 micro-ops)")
    add(f"{Q}{Q}{Q}")
    add("")
    add("每核的 `total cycles / total micro-ops` 同时保留为诊断，但不会先算每核 CPI 再平均。宏指令 CPI 使用相同的 cycle 分子，把分母替换为所有核心的宏指令总数。当前 packed field 已用 `macro_position=1(single)` 或 `2(first)` 标记每条宏指令的起点，因此未来可以在提交时直接累计，无需重新采集 raw trace。")
    add("")
    add("本报告现有全量表格使用 UOP CPI；`macro CPI` 专指宏指令 CPI。跨 workload 的统计统一称为 `workload-equal mean`，不再称为 macro CPI。以 `W_v28_bvc_encoder_base c4` 为例，UOP CPI pred/true 为 1.33468/1.39218，宏指令 CPI pred/true 为 1.70896/1.78259；二者相对误差均为 4.13%，因为预测和真实共享同一个指令数分母。")
    add("")
    add("### 2.2 Branch miss 的统一统计")
    add("")
    add("对 chunk `j`，模型输出退休分支 miss probability `p_hat_j`，functional trace 给出全部退休分支机会数 `B_j`：")
    add("")
    add(f"{Q}{Q}{Q}text")
    add("pred_miss_j = p_hat_j * B_j")
    add("M_pred = sum(所有核心、所有已提交 chunk 的 pred_miss_j)")
    add("M_true = sum(所有核心、所有已提交 chunk 的 true_miss_j)")
    add("B      = sum(所有核心、所有已提交 chunk 的 B_j)")
    add(f"{Q}{Q}{Q}")
    add("")
    add("一个 chunk 只在最终提交时累计一次；resident 重复进入上下文不会重复增加 branch miss。统一后的主指标为：")
    add("")
    add(f"{Q}{Q}{Q}text")
    add("branch_relative_error = abs(M_pred - M_true) / M_true")
    add("pred_rate = M_pred / B")
    add("true_rate = M_true / B")
    add("branch_abs_pp = abs(pred_rate - true_rate) * 100")
    add(f"{Q}{Q}{Q}")
    add("")
    add("由于 pred/true rate 使用完全相同的 `B`，`miss count relative error` 与 `miss rate relative error` 数学上相同，因此报告只保留一个 `Branch relative error` 作为主相对误差；同时保留 pred/true miss 数量、退休分支数、pred/true rate 和绝对百分点差。若 `M_true=0`，相对误差记为 N/A，只看绝对 miss 数和绝对百分点差。")
    add("")
    add("### 2.3 推荐的汇报层级")
    add("")
    add("1. **主结果：按核心数、workload 等权平均。** 每个负载一票，分别汇报 all-23、train/base-16、heldout-7。")
    add("2. **第二层：逐负载结果。** 同时给出 pred/true ROI UOP CPI、window MAPE/P90，以及 branch miss 数量、rate、相对误差和绝对百分点差。")
    add("3. **辅助结果：跨全部 trace 的 global-pooled aggregate。** 只用于总量偏差和计数完整性检查，不能作为泛化精度结论。")
    add("")

    add("## 3. 按核心数的主结果（workload-equal mean）")
    add("")
    add("| 核数 | 集合 | n | ROI UOP-CPI err mean | ROI err P50 | ROI err P90 | Window mean | Window P50 | Window P90 | Branch rel mean | Branch rel P50 | Branch rel P90 | Branch abs mean | Chunk MAPE |")
    add("|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for n_cores, core_rows in sorted(grouped.items()):
        for split_name in ("all", "train/base", "heldout"):
            selected = select_set(core_rows, split_name)
            roi = values(selected, "roi_cpi_error")
            window = values(selected, "window_cpi_mape_mean")
            branch = branch_relative_values(selected)
            branch_abs = values(selected, "branch_miss_rate_abs_error")
            chunk = values(selected, "chunk_cpi_mape_mean")
            add(
                f"| {n_cores} | {split_name} | {len(selected)} | "
                f"{100 * mean(roi):.2f}% | {100 * percentile(roi, .5):.2f}% | "
                f"{100 * percentile(roi, .9):.2f}% | {100 * mean(window):.2f}% | "
                f"{100 * percentile(window, .5):.2f}% | "
                f"{100 * percentile(window, .9):.2f}% | "
                f"{100 * mean(branch):.2f}% | {100 * percentile(branch, .5):.2f}% | "
                f"{100 * percentile(branch, .9):.2f}% | "
                f"{100 * mean(branch_abs):.2f} pp | {100 * mean(chunk):.2f}% |"
            )
    add("")
    add(
        "这里的 P50/P90 是“负载级指标”的分位数，不是把所有 window 混在一起后的"
        "分位数。Window mean 是各负载自身 scheduler-window MAPE mean 的等权平均。"
    )
    add("")

    add("## 4. global-pooled 全量聚合的含义与限制")
    add("")
    add("| 项目 | 预测 | 真实 | pooled 误差 |")
    add("|---|---:|---:|---:|")
    add(
        f"| ROI UOP CPI | {float(aggregate.get('pred_roi_cpi', 0.0)):.6f} | "
        f"{float(aggregate.get('true_roi_cpi', 0.0)):.6f} | "
        f"{percent(aggregate.get('global_roi_cpi_error'), 4)} |"
    )
    add(
        f"| Branch miss count | {count_number(aggregate.get('pred_branch_misses'))} | "
        f"{count_number(aggregate.get('true_branch_misses'), 0)} | "
        f"{percent(branch_relative(aggregate), 4)} |"
    )
    add(
        f"| Retired branches | {int(aggregate.get('retired_branches', 0)):,} | "
        f"{int(aggregate.get('retired_branches', 0)):,} | shared denominator |"
    )
    add(
        f"| Branch miss rate | {percent(aggregate.get('pred_branch_miss_rate'), 5)} | "
        f"{percent(aggregate.get('true_branch_miss_rate'), 5)} | "
        f"{percent(branch_relative(aggregate), 4)}"
        f"（{percentage_points(aggregate.get('branch_miss_rate_abs_error'))}） |"
    )
    add("")
    add(
        "该结果先跨 92 条 trace 累加 cycle/UOP/branch miss，再计算一个误差，因此是 "
        "**global-pooled aggregate**。它适合检查整批预测是否存在总体系统偏差，以及确认 "
        "exact-once 累计/标签覆盖是否正确。"
    )
    add("")
    add("它不能作为主精度结果，原因是：")
    add("")
    add("- 长 trace 和高 branch-count 负载权重更大；")
    add("- 不同负载的高估与低估会互相抵消；")
    add("- 混合了 4/8/16/32 核；")
    add("- 混合了训练分布和 heldout。")
    add("")
    add(
        "最明显的例子是 branch miss：pooled 相对误差只有 **2.08%**，但按负载"
        "等权后，各核心数 all-23 为 **24.52%–26.08%**，heldout 为 "
        "**50.08%–51.88%**。因此 pooled 数字有总量意义，但没有足够的泛化"
        f"判别力。JSON 中 {Q}trace_*_mean{Q} 虽然是 trace-equal mean，但仍混合了"
        "核心数和数据集角色，也不应替代分核心数报告。"
    )
    add("")

    add("## 5. 结果分析")
    add("")
    add("### 5.1 训练分布")
    add("")
    add("- train/base-16 的 ROI-CPI 平均误差在 **2.77%–3.13%**，P90 在 **4.47%–5.95%**，说明模型对训练分布的完整 ROI 周期预测较稳定。")
    add("- train/base 的 scheduler-window MAPE 为 **4.49%–5.10%**，明显高于 ROI 误差，符合局部误差在长 ROI 中部分抵消的预期。")
    add("- train/base 的 branch relative error 仍为 **12.91%–15.10%**；绝对误差只有 **0.30–0.41 pp**。对低 miss-rate 微负载，应优先结合绝对百分点判断。")
    add("")
    add("### 5.2 heldout 泛化")
    add("")
    add("- heldout-7 的 ROI-CPI 平均误差为 **17.51%–18.81%**，约为 train/base 的 6 倍。")
    add("- heldout scheduler-window MAPE 为 **20.16%–20.75%**，说明问题不是只发生在最终累计，而是局部窗口预测已经明显偏离。")
    add("- heldout branch relative error 为 **50.08%–51.88%**，绝对误差为 **3.36–3.50 pp**，是当前最弱的输出。")
    add("- 因此当前 checkpoint 可以说明模型拟合了训练分布，但不能证明其具备足够的业务泛化能力。")
    add("")
    add("### 5.3 核心数扩展")
    add("")
    add("- all-23 的 ROI-CPI 平均误差从 c4 的 7.90% 轻微下降到 c32 的 7.48%，没有出现随着核心数增长而系统性失效。")
    add("- scheduler-window MAPE 在 9.26%–9.85% 之间，也没有明显的 c32 突增。")
    add("- 但 Chunk MAPE 从 c4 的 12.07% 增长到 c32 的 21.08%；train/base 也从 6.69% 增长到 16.55%。这说明大核心数下最局部的 per-core chunk 预测更不稳定，只是部分误差在 scheduler window 和完整 ROI 中被抵消。若目标包含精确 closed-loop 调度，该现象需要继续关注。")
    add("")
    add("### 5.4 主要失败模式")
    add("")
    add(f"- {Q}flink_heldout{Q} 是最稳定、最严重的 CPI 泛化失败：四种核心数 ROI 误差均在 28.56%–34.50%，branch miss 误差约 68.83%–72.08%。")
    add(f"- {Q}mysql_heldout{Q} 的 ROI 误差随核心数从 21.14% 上升到 27.07%，但 branch miss 误差较小，说明其 CPI 误差主要不能简单归因于分支预测头。")
    add(f"- {Q}bvc_encoder_heldout{Q}、{Q}marine_heldout{Q}、{Q}pytorch_heldout{Q} 同时存在明显的窗口或 branch miss 偏差。")
    add(f"- {Q}redis_heldout{Q} 的 ROI 误差相对较低（c32 为 5.54%），但 branch miss 相对误差仍为 63.05%，表明 CPI 总量正确不代表 PMU 输出正确。")
    add("")

    add("## 6. 逐负载详细结果")
    add("")
    add(
        "每行先在该 workload 的完整 trace 内跨核累计。Branch pred/true misses 是数量，"
        "Cond branches 是共享分母；Branch relative error 对 count 和 rate 完全相同，"
        "Branch abs 是 rate 的绝对百分点差。"
    )
    add("")
    section = 1
    for n_cores, core_rows in sorted(grouped.items()):
        add(f"### 6.{section} c{n_cores:02d}")
        section += 1
        add("")
        add("| workload | 集合 | steps | Pred ROI UOP CPI | True ROI UOP CPI | ROI error | Window MAPE | Window P90 | Pred misses | True misses | Retired branches | Pred miss rate | True miss rate | Branch relative error | Branch abs | Chunk MAPE |")
        add("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
        for row in core_rows:
            add(
                f"| {Q}{row.get('workload', '-')}{Q} | {set_name(row)} | "
                f"{int(row.get('n_steps', 0)):,} | "
                f"{number(row.get('pred_roi_cpi'))} | {number(row.get('true_roi_cpi'))} | "
                f"{percent(row.get('roi_cpi_error'))} | "
                f"{percent(row.get('window_cpi_mape_mean'))} | "
                f"{percent(row.get('window_cpi_mape_p90'))} | "
                f"{count_number(row.get('pred_branch_misses'))} | "
                f"{count_number(row.get('true_branch_misses'), 0)} | "
                f"{int(row.get('retired_branches', 0)):,} | "
                f"{percent(row.get('pred_branch_miss_rate'))} | "
                f"{percent(row.get('true_branch_miss_rate'))} | "
                f"{percent(branch_relative(row))} | "
                f"{percentage_points(row.get('branch_miss_rate_abs_error'))} | "
                f"{percent(row.get('chunk_cpi_mape_mean'))} |"
            )
        add("")

    add("## 7. 最大 ROI-CPI 误差条目")
    add("")
    add("| 排名 | 核数 | workload | 集合 | ROI error | Window MAPE | Branch error |")
    add("|---:|---:|---|---|---:|---:|---:|")
    ranked = sorted(
        rows,
        key=lambda row: float(row.get("roi_cpi_error", 0.0)),
        reverse=True,
    )
    for rank, row in enumerate(ranked[:15], 1):
        add(
            f"| {rank} | {int(row.get('n_cores', 0))} | "
            f"{Q}{row.get('workload', '-')}{Q} | {set_name(row)} | "
            f"{percent(row.get('roi_cpi_error'))} | "
            f"{percent(row.get('window_cpi_mape_mean'))} | "
            f"{percent(branch_relative(row))} |"
        )
    add("")

    add("## 8. 当前结论与后续判断标准")
    add("")
    add("1. **部署推理与计数机制已通过完整性验证。** 92/92 traces 完成，label coverage 为 99.99999764%，chunk 的预测与 branch miss 均按首次加载锁存、提交时 exact-once 累计。")
    add("2. **训练分布结果较好。** 如果只评价 train/base 的完整 ROI UOP-CPI，当前 workload-equal mean 误差为 2.77%–3.13%。")
    add("3. **业务泛化尚不合格。** heldout ROI 约 18%、window 约 20%、branch 约 50%，不能被 pooled 5.52% ROI 或 2.08% branch 误差掩盖。")
    add("4. **核心数本身不是首要问题。** c32 的 ROI/window 没有整体恶化，但 chunk 误差增大，说明大上下文下局部预测仍有改进空间。")
    add("5. **后续模型或数据调整应以 heldout-7 的分核心数、workload-equal mean 为验收主线。** 至少同时跟踪 ROI UOP-CPI error mean/P90、scheduler-window MAPE、branch relative error 与 branch abs pp，避免只优化单一 global-pooled 指标。")
    add("")

    add("## 9. 相关实现")
    add("")
    add(f"- 文本报告聚合与逐负载表格：[{Q}tcsim/inference/reporting.py{Q}](../tcsim/inference/reporting.py)")
    add(f"- shard 合并并自动生成 {Q}report.txt{Q}：[{Q}scripts/merge_deployment_reports.py{Q}](../scripts/merge_deployment_reports.py)")
    add(f"- 独立重渲染文字报告：[{Q}scripts/render_deployment_report.py{Q}](../scripts/render_deployment_report.py)")
    add(f"- 本 Markdown 渲染器：[{Q}scripts/render_deployment_markdown.py{Q}](../scripts/render_deployment_markdown.py)")
    add(f"- 部署推理入口：[{Q}scripts/infer_deployment.py{Q}](../scripts/infer_deployment.py)")
    add(f"- 部署调度与 exact-once 逻辑：[{Q}tcsim/inference/deployment.py{Q}](../tcsim/inference/deployment.py)")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="merged deployment report.json")
    parser.add_argument("--out", required=True, help="output Markdown document")
    args = parser.parse_args()
    with open(args.input, "r", encoding="utf-8") as handle:
        report = json.load(handle)
    content = render(report, args.input, args.out)
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    print(f"[markdown-report] traces={len(report.get('traces', []))} out={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
