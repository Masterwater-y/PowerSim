"""Report source-bound physical completion estimates; never imply active CPI.

Native semantic checksums and per-core retired populations must agree before
any errors are calculated. Native oracle audits remain saved, provisional data.
"""

import argparse
import csv
import hashlib
import json
import pathlib
import statistics
from elapsed_metrics import compare_elapsed
from source_identity import stat_identity, check_inventory


def read(path):
    return json.loads(pathlib.Path(path).read_text())


def evaluate(case, frozen, results, input_receipt):
    engines = ["fastsim", "zsim", "sniper"]
    reference_case = frozen["fastsim"][case]
    manifest = pathlib.Path(reference_case["manifest"])
    manifest_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    entries = [
        line.split()
        for line in manifest.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    n = len(entries)
    if sorted(int(e[0]) for e in entries) != list(range(n)):
        raise ValueError("invalid manifest participants")
    entries.sort(key=lambda e: int(e[0]))
    macros, uops = [int(e[5]) for e in entries], [int(e[7]) for e in entries]
    for engine in engines:
        r = results[engine][case]
        if r.get("error") or r.get("returncode") != 0 or not r["population_match"]:
            raise ValueError(f"{engine} replay/population gate failed")
        if r["manifest_sha256"] != manifest_hash:
            raise ValueError("manifest changed since replay")
        if r["instructions"] != macros or r["uops"] != uops:
            raise ValueError(f"{engine} per-core population mismatch")
        if (
            pathlib.Path(frozen[engine][case]["manifest"]).resolve()
            != manifest.resolve()
        ):
            raise ValueError("different source manifests")
        if (
            frozen[engine][case]["effective_values"]
            != reference_case["effective_values"]
        ):
            raise ValueError("different source profiles")
    z = results["zsim"][case]["statistics"]
    s = results["sniper"][case]
    for field in [
        "semantic_hashes",
        "unknown_code_records",
        "bound_eof_cycles",
        "bound_retirement_tail",
        "native_cycles",
    ]:
        if len(z[field]) != n:
            raise ValueError("missing native zsim timing/semantic participant")
    participants = {str(c) for c in range(n)}
    if any(
        set(s[field]) != participants
        for field in ["begin", "end", "semantic_hashes", "unknown_code_records"]
    ):
        raise ValueError("missing or unexpected Sniper endpoint participant")
    if any(
        s["end"][str(c)]["stop"] != 0 or s["end"][str(c)]["macros"] != macros[c]
        for c in range(n)
    ):
        raise ValueError("Sniper endpoint stopped or incomplete")
    if z["semantic_hashes"] != [s["semantic_hashes"].get(str(c)) for c in range(n)]:
        raise ValueError("native adapters disagree on functional semantics")
    if z["unknown_code_records"] != [
        s["unknown_code_records"].get(str(c)) for c in range(n)
    ]:
        raise ValueError("native adapters disagree on code coverage")
    if z["weave_phases"] <= 0 or any(
        a < b for a, b in zip(z["bound_eof_cycles"], z["bound_retirement_tail"])
    ):
        raise ValueError("native zsim did not weave/drain")
    audit_path = manifest.parent / "oracle/thread-state-accounting.json"
    audit = read(audit_path)
    if not audit["valid"] or audit["participants"] != list(range(n)):
        raise ValueError("invalid saved native time audit")
    by_core = {row["core_id"]: row for row in audit["per_core"]}
    stops = []
    for c, e in enumerate(entries):
        stop = by_core[c]["retirement_stop"]
        if stop["record_ordinal"] != int(e[6]) + int(e[7]):
            raise ValueError(
                "native retirement stop does not bind full retained stream"
            )
        stops.append(stop["tick"])
    reference_seconds = (max(stops) - audit["common_start_tick"]) / audit[
        "ticks_per_second"
    ]
    fs = read(results["fastsim"][case]["statistics_file"])
    consumed = fs["functional_source_identity"]
    if (
        consumed["status"] != "complete"
        or consumed["source_identity"] != input_receipt["source_identity"]
    ):
        raise ValueError("FastSim consumed source identity mismatch")
    expected_artifacts = {
        str(pathlib.Path(art["path"])): art["sha256"]
        for source in consumed["sources"]
        for art in source["artifacts"]
    }
    if expected_artifacts != {
        art["path"]: art["sha256"] for art in input_receipt["artifacts"]
    }:
        raise ValueError("incomplete cross-engine source content receipt")
    if any(
        stat_identity(art["path"]) != art["stat"] for art in input_receipt["artifacts"]
    ):
        raise ValueError("source changed after cross-engine content verification")
    if check_inventory(consumed) != input_receipt["directories"]:
        raise ValueError("source directory or companion inventory changed")
    if (
        fs["time_accounting"]["elapsed_source"]
        != "target-common-origin-to-last-completion"
    ):
        raise ValueError("unsupported FastSim elapsed origin")
    durations = {
        "fastsim": fs["time_accounting"]["elapsed_time_seconds"],
        "zsim": max(z["native_cycles"]) / z["frequency_hz"],
        "sniper": max(
            s["end"][str(c)]["model_fs"] - s["begin"][str(c)]["model_fs"]
            for c in range(n)
        )
        / 1e15,
    }
    ss_seconds = (
        max(v["host_ns"] for v in s["end"].values())
        - min(v["host_ns"] for v in s["begin"].values())
    ) / 1e9
    user_uops = fs["scope_metrics"]["user_trace_uops"]
    rows = []
    for engine in engines:
        metric = compare_elapsed(
            reference_seconds=reference_seconds,
            reference_hz=audit["reference_frequency_hz"],
            model_seconds=durations[engine],
            participants=n,
            macros=results[engine][case]["instructions"],
            uops=results[engine][case]["uops"],
            expected_macros=macros,
            expected_uops=uops,
        )
        rate = (
            fs["scope_metrics"]["throughput"]["user_uops_per_second"]
            if engine == "fastsim"
            else user_uops
            / (z["measurement_seconds"] if engine == "zsim" else ss_seconds)
        )
        rows.append(
            dict(
                case=case,
                engine=engine,
                cores=n,
                **metric,
                measurement_user_mups=rate / 1e6,
                process_wall_seconds=results[engine][case]["process_wall_seconds"],
                source_audit_sha256=hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                unknown_code_records=sum(z["unknown_code_records"]),
                per_core_population_match=True,
                native_semantic_hashes_match=True,
                cross_engine_source_content_match=True,
                reference_freshly_audited=False,
            )
        )
    return rows


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign", type=pathlib.Path, required=True)
    p.add_argument("--sniper-subdir", default="sniper-repaired")
    p.add_argument("--output-prefix", type=pathlib.Path, required=True)
    a = p.parse_args()
    folders = {
        "fastsim": a.campaign / "fastsim",
        "zsim": a.campaign / "zsim",
        "sniper": a.campaign / a.sniper_subdir,
    }
    batches = {k: read(v / "freeze.json") for k, v in folders.items()}
    frozen = {k: {r["case"]: r for r in v["cases"]} for k, v in batches.items()}
    results = {
        k: {r["case"]: r for r in read(v / "summary.json")} for k, v in folders.items()
    }
    cases = sorted(frozen["fastsim"])
    identity = read(a.campaign / "input-identity.json")
    cutoff = min((v / "freeze.json").stat().st_mtime_ns for v in folders.values())
    if (
        identity["policy"] != "local-sha256-and-prelaunch-ctime-v1"
        or identity["cutoff_ns"] > cutoff
    ):
        raise ValueError("invalid cross-engine prelaunch identity boundary")
    if any(
        max(art["stat"]["mtime_ns"], art["stat"]["ctime_ns"]) >= identity["cutoff_ns"]
        for row in identity["cases"].values()
        for art in row["artifacts"]
    ):
        raise ValueError("source content receipt includes post-launch changes")
    if len(cases) != 10 or any(set(v) != set(cases) for v in results.values()):
        raise ValueError("require the complete ten-case three-engine matrix")
    rows, failures = [], []
    for case in cases:
        try:
            rows.extend(evaluate(case, frozen, results, identity["cases"][case]))
        except (ValueError, KeyError) as error:
            failures.append(dict(case=case, error=str(error)))
    aggregates = []
    for cores in [4, 8]:
        for engine in folders:
            group = [r for r in rows if r["cores"] == cores and r["engine"] == engine]
            if group:
                aggregates.append(
                    dict(
                        cores=cores,
                        engine=engine,
                        cases=len(group),
                        completion_time_mape_percent=100
                        * statistics.mean(abs(r["relative_error"]) for r in group),
                        elapsed_macro_proxy_mae=statistics.mean(
                            r["absolute_error_cycles_per_macro"] for r in group
                        ),
                        diagnostic_median_user_mups=statistics.median(
                            r["measurement_user_mups"] for r in group
                        ),
                    )
                )
    data = dict(
        metric="retained-physical-completion-time-v1",
        formal_validation=False,
        reference_freshly_audited=False,
        parallel_host_timing_diagnostic_only=True,
        binaries={k: v["binary_sha256"] for k, v in batches.items()},
        cases=rows,
        aggregates=aggregates,
        failures=failures,
    )
    active = []
    for cores in [4, 8]:
        path = a.campaign / f"validation-c{cores}" / "summary.json"
        if path.exists():
            summary = read(path)
            if (
                summary["validation_mode"] != "quick"
                or summary["cpi_scored_cases"] != 5
            ):
                raise ValueError("unexpected FastSim active CPI diagnostic scope")
            active.append(
                dict(
                    cores=cores,
                    cpi_mae=summary["cpi_mae"],
                    cpi_mape_percent=summary["cpi_mape_percent"],
                    cases=[
                        dict(case=r["case_id"], **r["cpi"]) for r in summary["cases"]
                    ],
                )
            )
    data["fastsim_active_cpi_diagnostics"] = active
    a.output_prefix.with_suffix(".json").write_text(json.dumps(data, indent=2))
    if rows:
        with a.output_prefix.with_suffix(".csv").open("w") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    lines = [
        "# C4/C8 原生后端 trace 对比（2026-11-16）",
        "",
        "三者消费相同保留 FST。以下比较的是整段物理流完成时间，包含其他上下文；不是 workload-task active CPI。",
        "参考取保存的 gem5 逐核 retirement-stop 最大值与共同起点之差，尚未重新做内容级审计；所有误差均为暂定估计。",
        "原生后端配置尚未完全对齐 SPR，指令类别翻译也有近似，不能据此认定某个模拟器普遍更准确。",
        "输入内容另通过 SHA256、完整伴随文件清单和 prelaunch ctime 门核对，原生适配器的逐核功能语义校验和也必须一致。",
        "",
        "| 核数 | 后端 | 样本 | 完成时间 MAPE | 归一化绝对误差 MAE（cycles/macro） |",
        "|---|---|---:|---:|---:|",
    ]
    for r in aggregates:
        lines.append(
            f"| C{r['cores']} | {r['engine']} | {r['cases']} | {r['completion_time_mape_percent']:.2f}% | {r['elapsed_macro_proxy_mae']:.4f} |"
        )
    lines += [
        "",
        "归一化量 = 完成秒数 × 参考时钟 × 核数 / 物理流宏指令数。它仅为 elapsed macro cycle proxy，不是 active CPI。",
        "",
        "| Case | gem5 ms | FastSim ms / 相对误差 / 绝对误差 | zsim ms / 相对误差 / 绝对误差 | Sniper ms / 相对误差 / 绝对误差 |",
        "|---|---:|---:|---:|---:|",
    ]
    for case in cases:
        g = {r["engine"]: r for r in rows if r["case"] == case}
        if len(g) == 3:
            cells = [
                f"{g[k]['model_seconds'] * 1000:.3f} / {g[k]['relative_error'] * 100:+.2f}% / {g[k]['absolute_error_cycles_per_macro']:.4f}"
                for k in folders
            ]
            lines.append(
                f"| {case} | {g['fastsim']['reference_seconds'] * 1000:.3f} | "
                + " | ".join(cells)
                + " |"
            )
    lines += [
        "",
        "各行绝对误差单位均为 cycles/macro。完整逐核人口、校验和、输入缺项和二进制身份见同名 JSON/CSV 及原始 campaign。",
        "",
        "| 核数 | 后端 | 测量阶段 user MUPS 中位数（并发诊断） |",
        "|---|---|---:|",
    ]
    for r in aggregates:
        lines.append(
            f"| C{r['cores']} | {r['engine']} | {r['diagnostic_median_user_mups']:.3f} |"
        )
    lines += [
        "",
        "MUPS 排除了预处理、校验与功能 warmup；完整进程墙钟另存于 JSON。多后端共享 CPU 52–63 并行运行，不能作为受控吞吐验收或加速比。",
        "",
        "zsim 使用原生 OOOCore、TimingCache、MESI、EventRecorder 和 ContentionSim；核心资源仍为原生模型，DRAM 为 SimpleMemory。Sniper 使用原生 ROB/缓存与 SPR 操作类别表，DRAM 常数/带宽仍为既有近似配置。",
        "",
        "部分记录缺少指令长度，本轮显式启用 1-byte fallback，并逐核记录 unknown；未据此宣称正式精度通过。详情和复现方法见 [实现说明](docs/native-trace-comparison-20261116.md)。",
        "",
        f"共同人口与原生语义门：{len(rows)//3}/10 cases；拒绝项：{len(failures)}。",
    ]
    if active:
        lines += [
            "",
            "FastSim 单独的 workload-task active CPI（维护入口 quick diagnostics，保存的 gem5 审计）：",
            "",
            "| 核数 | CPI MAPE | CPI MAE（cycles/macro） |",
            "|---|---:|---:|",
        ]
        for r in active:
            lines.append(
                f"| C{r['cores']} | {r['cpi_mape_percent']:.2f}% | {r['cpi_mae']:.4f} |"
            )
        lines += [
            "",
            "该 active CPI 表与上面的物理完成时间表口径不同；Sniper/zsim 未提供匹配的任务时钟，不能纳入这张 CPI 排名。逐项 CPI 与绝对误差保留在 JSON 及 validation-c4/c8/summary.md。",
        ]
    lines += ["", "冻结程序 SHA256：", ""]
    lines += [f"- {k}: `{v['binary_sha256']}`" for k, v in batches.items()]
    for failure in failures:
        lines.append(f"- {failure['case']}: {failure['error']}")
    a.output_prefix.with_suffix(".md").write_text("\n".join(lines) + "\n")
    print(
        json.dumps(
            dict(
                compared_cases=len(rows) // 3, failures=failures, aggregates=aggregates
            ),
            indent=2,
        )
    )
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
