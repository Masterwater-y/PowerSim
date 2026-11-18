"""Primary end-to-end throughput, including preparation and excluding extra audits."""

import argparse
import csv
import hashlib
import json
import math
import pathlib
import statistics
from e2e_launch import verify_case
from baseline_policy import require_parallel_zsim


def validate_timing(t, receipt_sha256):
    if not all(
        t[k] for k in ["trusted_input", "preprocessing_included", "warmup_included"]
    ):
        raise ValueError("invalid primary timing scope")
    if t["policy"] != "preparation-included-extra-validation-excluded-v1":
        raise ValueError("invalid timing policy")
    if t["validation_receipt_sha256"] != receipt_sha256:
        raise ValueError("different validation receipt")
    seconds = t["e2e_seconds"]
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("invalid end-to-end duration")
    span = (t["end_ns"] - t["start_ns"]) / 1e9
    if not math.isclose(span, seconds, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError("end-to-end duration not bound to wall timestamps")
    if (
        not math.isfinite(t["process_wall_seconds"])
        or t["process_wall_seconds"] < seconds
    ):
        raise ValueError("model end outside process wall")


def summarize_rates(group):
    if not group:
        raise ValueError("empty batch")
    work = sum(r["user_uops"] for r in group)
    wall = sum(r["e2e_seconds"] for r in group)
    rates = [r["e2e_user_mups"] for r in group]
    return dict(
        batch_e2e_user_mups=work / wall / 1e6,
        total_user_uops=work,
        total_e2e_seconds=wall,
        median_e2e_user_mups=statistics.median(rates),
        min_e2e_user_mups=min(rates),
        max_e2e_user_mups=max(rates),
        median_e2e_seconds=statistics.median(r["e2e_seconds"] for r in group),
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", type=pathlib.Path, required=True)
    p.add_argument("--output-prefix", type=pathlib.Path, required=True)
    p.add_argument(
        "--historical-serial-zsim",
        action="store_true",
        help="explicitly label archived serial preparation results",
    )
    a = p.parse_args()
    batch = json.loads((a.campaign / "freeze.json").read_text())
    results = json.loads((a.campaign / "summary.json").read_text())
    receipt = json.loads((a.campaign / "input-identity.json").read_text())
    receipt_sha = hashlib.sha256(
        (a.campaign / "input-identity.json").read_bytes()
    ).hexdigest()
    for engine, path in batch["binaries"].items():
        if (
            hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()
            != batch["binary_sha256"][engine]
        ):
            raise ValueError("frozen executable changed")
    by_case = {r["case"]: r for r in batch["cases"]}
    expected_users = {
        case: json.loads(
            (
                pathlib.Path(batch["original_validation_campaign"])
                / "fastsim"
                / case
                / "stats.json"
            ).read_text()
        )["scope_metrics"]["user_trace_uops"]
        for case in by_case
    }
    rows = []
    preparation_policies = set()
    expected = {r["case"] for r in batch["cases"]}
    if len(expected) != 10:
        raise ValueError("expected ten retained C4/C8 cases")
    for r in results:
        if r["engine"] == "zsim":
            preparation_policies.add(
                require_parallel_zsim(r["native"], a.historical_serial_zsim)
            )
        if r["mode"] != "trusted":
            raise ValueError("strict pilot cannot enter primary report")
        if r.get("error") or not r.get("population_match"):
            raise ValueError("failed replay/population gate")
        t = r["timing"]
        validate_timing(t, receipt_sha)
        entries = [
            line.split()
            for line in pathlib.Path(by_case[r["case"]]["manifest"])
            .read_text()
            .splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        entries.sort(key=lambda e: int(e[0]))
        if (
            [int(e[0]) for e in entries] != list(range(r["cores"]))
            or r["instructions"] != [int(e[5]) for e in entries]
            or r["uops"] != [int(e[7]) for e in entries]
        ):
            raise ValueError("report per-core population mismatch")
        if r["user_uops"] != expected_users[r["case"]]:
            raise ValueError("incorrect functional user numerator")
        if t["source_identity"] != verify_case(receipt, r["case"]):
            raise ValueError("source identity mismatch")
        if r["binary_sha256"] != batch["binary_sha256"][r["engine"]]:
            raise ValueError("binary identity mismatch")
        rows.append(
            dict(
                case=r["case"],
                engine=r["engine"],
                cores=r["cores"],
                user_uops=r["user_uops"],
                all_measurement_uops=sum(r["uops"]),
                e2e_seconds=t["e2e_seconds"],
                e2e_user_mups=r["user_uops"] / t["e2e_seconds"] / 1e6,
                e2e_all_uops_mups=sum(r["uops"]) / t["e2e_seconds"] / 1e6,
                simulation_seconds=r.get("simulation_seconds"),
                simulation_user_mups=(
                    r["user_uops"] / r["simulation_seconds"] / 1e6
                    if r.get("simulation_seconds")
                    else None
                ),
                process_wall_seconds=t["process_wall_seconds"],
                external_validation_seconds=t["validation_before_seconds"]
                + t["validation_after_seconds"],
                peak_rss_kib=t.get("peak_rss_kib"),
                child_cpu_seconds=t.get("child_cpu_seconds"),
                formal_throughput_acceptance=False,
            )
        )
    if len(rows) != 30 or {(r["case"], r["engine"]) for r in rows} != {
        (c, e) for c in expected for e in ["fastsim", "zsim", "sniper"]
    }:
        raise ValueError("incomplete or duplicated three-engine matrix")
    if len(preparation_policies) != 1:
        raise ValueError("do not aggregate mixed zsim preparation policies")
    preparation_policy = next(iter(preparation_policies))
    for case in expected:
        if len({r["user_uops"] for r in rows if r["case"] == case}) != 1:
            raise ValueError("different FST numerator")
    aggregates = []
    for cores in [4, 8]:
        for engine in ["fastsim", "zsim", "sniper"]:
            group = [r for r in rows if r["cores"] == cores and r["engine"] == engine]
            if len(group) != 5:
                raise ValueError("incomplete C4/C8 group")
            aggregates.append(
                dict(
                    cores=cores,
                    engine=engine,
                    cases=5,
                    **summarize_rates(group),
                )
            )
    data = dict(
        schema="tracebench-e2e-report-v1",
        policy="preparation-included-extra-validation-excluded-v1",
        formal_throughput_acceptance=False,
        repeats_per_case=1,
        cpu_budget="52-63",
        zsim_preparation_policy=preparation_policy,
        binary_sha256=batch["binary_sha256"],
        aggregates=aggregates,
        cases=rows,
    )
    a.output_prefix.with_suffix(".json").write_text(json.dumps(data, indent=2))
    with a.output_prefix.with_suffix(".csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    lines = [
        "# C4/C8 端到端吞吐对比（2026-11-16）",
        "",
        (
            "zsim 主报告基线：文件级并行预处理。"
            if preparation_policy == "parallel-files"
            else "历史串行预处理对照：不作为当前 zsim 主吞吐基线。"
        ),
        "主指标包含输入读取、解压、功能解析/转换、内存准备、初始化、功能 warmup、正式模拟、同步与必要排空。独立内容身份、整流审计和额外投影校验在计时外完成；解析边界检查保留，解压器自然执行的 CRC 工作随读取计时。受信回放不额外扫尾以重复验证完整 gzip CRC/footer，完整性由严格审计和未变的内容收据保证。",
        "每次从启动实际引擎前的全局 monotonic 墙钟开始，到全部参与核的模型完成时间戳结束；不相减线程 CPU 时间，不以纯模拟时间代替端到端耗时。结果格式化及后置审计在完成时间戳之后。",
        "",
        "单位 user MUPS（百万测量段 user UOP/s）。主汇总为每组五个负载的总 user UOP ÷ 总端到端耗时；另列逐项吞吐的中位数和范围。范围是负载间范围，不是重复测量置信区间。",
        "",
        "| 核数 | 后端 | 总量/总耗时 MUPS | MUPS 中位数 | 负载间范围 |",
        "|---|---|---:|---:|---:|",
    ]
    for r in aggregates:
        lines.append(
            f"| C{r['cores']} | {r['engine']} | {r['batch_e2e_user_mups']:.3f} | {r['median_e2e_user_mups']:.3f} | {r['min_e2e_user_mups']:.3f}–{r['max_e2e_user_mups']:.3f} |"
        )
    lines += [
        "",
        "| Case | FastSim MUPS / 秒 | zsim MUPS / 秒 | Sniper MUPS / 秒 |",
        "|---|---:|---:|---:|",
    ]
    for case in sorted(expected):
        group = {r["engine"]: r for r in rows if r["case"] == case}
        cells = [
            f"{group[e]['e2e_user_mups']:.3f} / {group[e]['e2e_seconds']:.2f}"
            for e in ["fastsim", "zsim", "sniper"]
        ]
        lines.append("| " + case + " | " + " | ".join(cells) + " |")
    lines += [
        "",
        "三者逐项使用相同的 FST user UOP 分子，全部物理记录（包括 kernel、其他上下文与尾部片段）都执行。warmup 成本计入分母，warmup UOP 不加入测量段分子；完整人口和 all-UOP 吞吐另存 JSON/CSV。",
        "",
        "本 campaign 每次只运行一个 case/引擎，均限制在 CPU 52–63、NUMA 1，并记录高频策略检查。每项只测一次，不声称多轮配对吞吐验收，也不保证整台共享主机独占。",
        "",
        "原始 FST 与伴随文件在每次引擎启动后重新读取和转换，没有把 DynUop 或功能 preload 的准备成本提前免费复用。校验使用既有严格运行形成的内容收据，并在计时前后核对完整文件清单、inode/mtime/ctime 及配置辅助输入。依赖稳定的本地文件系统路径和时钟，不是对抗恶意存储的证明。",
        "",
        "FastSim、Sniper、zsim 的原生模型及硬件近似仍有差异，尤其 zsim SimpleMemory 与原生固定核心资源。这个表比较当前具体实现的端到端吞吐，不表示同等 SPR 建模精度。纯模拟辅助时间、完整进程墙钟和校验成本见 JSON/CSV。",
        "",
        "实现与复现见 [计时合同](docs/trace-comparison-throughput-contract-20261116.md)。历史纯模拟结果见 [原生后端报告](fastsim-native-trace-comparison-20261116.md)，两种口径分别保留。",
        "",
        f"原始 campaign：`{a.campaign}`。",
    ]
    a.output_prefix.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps(aggregates, indent=2))


if __name__ == "__main__":
    main()
