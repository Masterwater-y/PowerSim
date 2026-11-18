"""Compare frozen serial/parallel preparation using identical functional inputs."""

import argparse
import csv
import hashlib
import json
import pathlib
import statistics
from e2e_launch import verify_case
from report_e2e import validate_timing


def read(path):
    return json.loads(pathlib.Path(path).read_text())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--serial", type=pathlib.Path, required=True)
    p.add_argument("--parallel", type=pathlib.Path, required=True)
    p.add_argument("--output-prefix", type=pathlib.Path, required=True)
    a = p.parse_args()
    old = {
        r["case"]: r for r in read(a.serial / "summary.json") if r["engine"] == "zsim"
    }
    new = {
        r["case"]: r for r in read(a.parallel / "summary.json") if r["engine"] == "zsim"
    }
    if len(old) != 10 or set(old) != set(new):
        raise ValueError("require identical ten-case matrices")
    receipts = [read(d / "input-identity.json") for d in [a.serial, a.parallel]]
    hashes = [
        hashlib.sha256((d / "input-identity.json").read_bytes()).hexdigest()
        for d in [a.serial, a.parallel]
    ]
    configs = [
        {r["case"]: r["e2e_values"] for r in read(d / "freeze.json")["cases"]}
        for d in [a.serial, a.parallel]
    ]
    b = a.serial.parent
    serial_build = read(b / "zsim/native-build-receipt.json")
    parallel_build = read(b / "zsim-parallel/native-build-receipt.json")
    if (
        serial_build["functional_reader_archive_sha256"]
        != parallel_build["functional_reader_archive_sha256"]
        or serial_build["compiled_sources"] != parallel_build["compiled_sources"]
        or serial_build["adapter_sources"]["fst_memory.cpp"]
        != parallel_build["adapter_sources"]["fst_memory.cpp"]
    ):
        raise ValueError("native backend or functional reader changed")
    rows = []
    for case in sorted(old):
        s, n = old[case], new[case]
        for index, r in enumerate([s, n]):
            if r.get("error") or not r.get("population_match"):
                raise ValueError("failed replay")
            validate_timing(r["timing"], hashes[index])
            verify_case(receipts[index], case)
        if configs[0][case] != configs[1][case]:
            raise ValueError("changed target parameters")
        if any(s[k] != n[k] for k in ["instructions", "uops", "user_uops"]):
            raise ValueError("changed population")
        if s["native"]["prepared_hashes"] != n["native"]["prepared_hashes"]:
            raise ValueError("changed prepared inputs")
        delta = max(n["native"]["native_cycles"]) - max(s["native"]["native_cycles"])
        rows.append(
            dict(
                case=case,
                cores=n["cores"],
                workers=n["native"]["preparation_workers"],
                user_uops=n["user_uops"],
                serial_preparation_seconds=s["native"]["preparation_seconds"],
                parallel_preparation_seconds=n["native"]["preparation_seconds"],
                serial_e2e_seconds=s["timing"]["e2e_seconds"],
                parallel_e2e_seconds=n["timing"]["e2e_seconds"],
                serial_e2e_mups=s["e2e_user_mups"],
                parallel_e2e_mups=n["e2e_user_mups"],
                serial_simulation_seconds=s["simulation_seconds"],
                parallel_simulation_seconds=n["simulation_seconds"],
                serial_peak_rss_gib=s["timing"]["peak_rss_kib"] / 1024**2,
                parallel_peak_rss_gib=n["timing"]["peak_rss_kib"] / 1024**2,
                prepared_inputs_identical=True,
                common_completion_cycle_delta=delta,
                common_completion_cycle_change_percent=100
                * delta
                / max(s["native"]["native_cycles"]),
                per_core_cycle_delta=[
                    y - x
                    for x, y in zip(
                        s["native"]["native_cycles"], n["native"]["native_cycles"]
                    )
                ],
            )
        )
    groups = []
    for cores in [4, 8]:
        g = [r for r in rows if r["cores"] == cores]
        work = sum(r["user_uops"] for r in g)
        groups.append(
            dict(
                cores=cores,
                serial_mean_preparation_seconds=statistics.mean(
                    r["serial_preparation_seconds"] for r in g
                ),
                parallel_mean_preparation_seconds=statistics.mean(
                    r["parallel_preparation_seconds"] for r in g
                ),
                preparation_speedup=sum(r["serial_preparation_seconds"] for r in g)
                / sum(r["parallel_preparation_seconds"] for r in g),
                serial_e2e_mups=work / sum(r["serial_e2e_seconds"] for r in g) / 1e6,
                parallel_e2e_mups=work
                / sum(r["parallel_e2e_seconds"] for r in g)
                / 1e6,
                serial_max_rss_gib=max(r["serial_peak_rss_gib"] for r in g),
                parallel_max_rss_gib=max(r["parallel_peak_rss_gib"] for r in g),
            )
        )
    data = dict(
        schema="zsim-parallel-preparation-v1",
        serial_campaign=str(a.serial),
        parallel_campaign=str(a.parallel),
        native_backend_sources_unchanged=True,
        functional_reader_archive_unchanged=True,
        all_prepared_inputs_identical=True,
        formal_throughput_acceptance=False,
        repeats_per_case=1,
        max_abs_common_cycle_change_percent=max(
            abs(r["common_completion_cycle_change_percent"]) for r in rows
        ),
        aggregates=groups,
        cases=rows,
    )
    control_path = a.parallel.parent / "zsim-parallel-repeat-control/summary.json"
    if control_path.exists():
        repeat = read(control_path)[0]
        first = new[repeat["case"]]
        data["repeat_control"] = dict(
            case=repeat["case"],
            included_in_primary_summary=False,
            prepared_inputs_identical=repeat["native"]["prepared_hashes"]
            == first["native"]["prepared_hashes"],
            common_completion_cycle_delta=max(repeat["native"]["native_cycles"])
            - max(first["native"]["native_cycles"]),
        )
    a.output_prefix.with_suffix(".json").write_text(json.dumps(data, indent=2))
    with a.output_prefix.with_suffix(".csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    lines = [
        "# zsim 文件级并行准备（2026-11-16）",
        "",
        "串行来自本地适配器逐核 preflight 的实现，并非原生 zsim 要求。现已改为独立 reader/解压流/输出槽位的受限线程池：默认 min(文件数, CPU affinity)，当前 C4=4、C8=8，均在 CPU 52–63 内。输入准备仍完整计入端到端墙钟。",
        "",
        "| 核数 | 平均准备秒数：串行 → 并行 | 准备加速 | 端到端 MUPS：串行 → 并行 |",
        "|---|---:|---:|---:|",
    ]
    for g in groups:
        lines.append(
            f"| C{g['cores']} | {g['serial_mean_preparation_seconds']:.2f} → {g['parallel_mean_preparation_seconds']:.2f} | {g['preparation_speedup']:.2f}× | {g['serial_e2e_mups']:.3f} → {g['parallel_e2e_mups']:.3f} |"
        )
    lines += [
        "",
        "准备秒数取每组五个负载的平均；端到端吞吐用总 user UOP / 总端到端耗时。warmup、初始化、正式模拟和排空计入端到端，额外校验独立。",
        "",
        "| Case | 准备秒数：串行 → 并行 | 端到端 MUPS：串行 → 并行 |",
        "|---|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['case']} | {r['serial_preparation_seconds']:.2f} → {r['parallel_preparation_seconds']:.2f} | {r['serial_e2e_mups']:.3f} → {r['parallel_e2e_mups']:.3f} |"
        )
    lines += [
        "",
        "10/10 逐核准备输入摘要、宏指令/UOP 数量一致。原生核心/缓存/争用源码、功能读取库和目标参数保持一致。配置先加载，准备线程全部 join 后才计算寄存器容量并初始化后端；原有顺序功能 warmup 保持不变。线程异常也必须全部 join 并在模拟前报错。",
        "",
        f"原生模拟输出未宣称逐 bit 相等：本轮共同完成周期最大绝对差异为 {data['max_abs_common_cycle_change_percent']:.5f}%，逐核/逐案差值保留在 JSON/CSV；不把它当作精度改善。正式模拟宿主耗时也会变化，记录了前后完整分解，未做多轮配对来分离频率、调度或分配器影响。",
        "",
        "每项仅一轮观测，未作重复吞吐验收；文件大小不均衡及存储/内存带宽限制使实际收益低于按核心数的等比例加速。内存峰值也保留在 JSON/CSV。",
        "",
        "回退串行：`TRACEBENCH_PREPARE_JOBS=1`；默认并行：`TRACEBENCH_PREPARE_JOBS=auto`。",
        "",
        "复现：先用 `build_native_zsim.py --functional-root tmp/e2e-throughput-20261116/fastsim-source --output tmp/e2e-throughput-20261116/zsim-parallel` 构建，然后运行：",
        "",
        "```bash",
        "source scripts/project_tmp.sh",
        "python3 tools/trace_comparison/run_e2e_matrix.py --engines zsim " + chr(92),
        "  --zsim-binary tmp/e2e-throughput-20261116/zsim-parallel/native-zsim "
        + chr(92),
        "  --zsim-prepare-jobs auto --output tmp/e2e-throughput-20261116/campaign-zsim-parallel",
        "```",
        "",
        "原始串行结果及 FastSim/Sniper 样本保持原样，见 [原端到端报告](fastsim-sniper-zsim-e2e-throughput-20261116.md)。",
    ]
    if data.get("repeat_control"):
        control = data["repeat_control"]
        lines += [
            "",
            f"额外同版本/同模式重复对照：{control['case']} 的准备输入摘要仍一致，共同完成周期也出现 {control['common_completion_cycle_delta']:+d} 的差值。该对照单独保留，未并入主吞吐统计；当前原生回放未呈现逐 bit 稳定性。",
        ]
    a.output_prefix.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(json.dumps(groups, indent=2))


if __name__ == "__main__":
    main()
