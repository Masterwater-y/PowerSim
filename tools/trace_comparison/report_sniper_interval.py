"""Compare one interval campaign with preserved three-engine measurements."""
import argparse
import csv
import hashlib
import json
import pathlib
import statistics

from e2e_launch import verify_case
from elapsed_metrics import compare_elapsed
from report_e2e import summarize_rates, validate_timing
from run_e2e_matrix import extract

ROOT = pathlib.Path(__file__).resolve().parents[2]


def digest(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--campaign', type=pathlib.Path, required=True)
    p.add_argument('--baseline', type=pathlib.Path,
        default=ROOT / 'fastsim-sniper-zsim-current-20261116.json')
    p.add_argument('--output-prefix', type=pathlib.Path, required=True)
    a = p.parse_args()
    baseline = json.loads(a.baseline.read_text())
    freeze = json.loads((a.campaign / 'freeze.json').read_text())
    receipt = json.loads((a.campaign / 'input-identity.json').read_text())
    receipt_sha = digest(a.campaign / 'input-identity.json')
    new = json.loads((a.campaign / 'summary.json').read_text())
    assert baseline['zsim_preparation_policy'] == 'parallel-files'
    assert freeze['sniper_core_type'] == 'interval'
    assert digest(freeze['binaries']['sniper']) == freeze['binary_sha256']['sniper']
    assert digest(a.campaign / 'sniper.cfg') == freeze['sniper_config_sha256']
    old_sniper = pathlib.Path(baseline['source_campaigns']['sniper'])
    assert digest(a.campaign / 'sniper.cfg') == digest(old_sniper / 'sniper.cfg')
    assert digest(old_sniper / 'input-identity.json') == receipt_sha
    expected = {r['case']: r for r in baseline['cases'] if r['engine'] == 'sniper'}
    cases = {r['case']: r for r in freeze['cases']}
    assert len(new) == 10 and len({r['case'] for r in new}) == 10
    assert set(expected) == set(cases) == {r['case'] for r in new}
    rows = []
    completion = []
    old_accuracy = {(r['case'], r['engine']): r for r in baseline['physical_completion_accuracy']['cases']}
    for r in new:
        assert r['engine'] == 'sniper' and r['sniper_core_type'] == 'interval'
        assert r['mode'] == 'trusted' and not r.get('error') and r['population_match']
        assert r['binary_sha256'] == freeze['binary_sha256']['sniper']
        validate_timing(r['timing'], receipt_sha)
        assert verify_case(receipt, r['case']) == r['timing']['source_identity']
        assert r['user_uops'] == expected[r['case']]['user_uops']
        out = a.campaign / r['case'] / 'sniper-trusted'
        measured = extract('sniper', out, r['cores'], sniper_model='interval')
        for key in ['instructions', 'uops', 'simulation_seconds', 'model_elapsed_seconds']:
            assert measured[key] == r[key], ('changed model output', r['case'], key)
        manifest = pathlib.Path(cases[r['case']]['manifest'])
        entries = sorted((line.split() for line in manifest.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith('#')), key=lambda e: int(e[0]))
        assert [int(e[0]) for e in entries] == list(range(r['cores']))
        assert r['instructions'] == [int(e[5]) for e in entries]
        assert r['uops'] == [int(e[7]) for e in entries]
        audit_path = manifest.parent / 'oracle/thread-state-accounting.json'
        assert digest(audit_path) == old_accuracy[r['case'], 'sniper']['source_audit_sha256']
        audit = json.loads(audit_path.read_text())
        assert audit['valid'] and audit['participants'] == list(range(r['cores']))
        cores = sorted(audit['per_core'], key=lambda c: c['core_id'])
        assert [c['core_id'] for c in cores] == list(range(r['cores']))
        assert [c['retirement_stop']['record_ordinal'] for c in cores] == [int(e[6])+int(e[7]) for e in entries]
        population = sorted(audit['recorded_owner_proxy']['per_core'], key=lambda c: c['core_id'])
        assert [c['measurement_macro_instructions'] for c in population] == r['instructions']
        assert [c['measurement_retired_uops'] for c in population] == r['uops']
        metric = compare_elapsed(
            reference_seconds=(max(c['retirement_stop']['tick'] for c in cores)-audit['common_start_tick'])/audit['ticks_per_second'],
            reference_hz=audit['reference_frequency_hz'], model_seconds=r['model_elapsed_seconds'],
            participants=r['cores'], macros=r['instructions'], uops=r['uops'],
            expected_macros=[int(e[5]) for e in entries], expected_uops=[int(e[7]) for e in entries])
        completion.append(dict(case=r['case'], engine='sniper-interval', cores=r['cores'],
            source_audit=str(audit_path), source_audit_sha256=digest(audit_path),
            reference_freshly_audited=False, **metric))
        rows.append(dict(case=r['case'], engine='sniper-interval', cores=r['cores'],
            user_uops=r['user_uops'], all_measurement_uops=sum(r['uops']),
            all_measurement_macros=sum(r['instructions']),
            e2e_seconds=r['timing']['e2e_seconds'], e2e_user_mups=r['e2e_user_mups'],
            simulation_seconds=r['simulation_seconds'],
            simulation_user_mups=r['user_uops']/r['simulation_seconds']/1e6,
            source_campaign=str(a.campaign.resolve()),
            source_identity=r['timing']['source_identity'],
            binary_sha256=r['binary_sha256'], **metric))
    for r in baseline['cases']:
        row = dict(r)
        row['engine'] = 'sniper-rob' if r['engine'] == 'sniper' else r['engine']
        row.update(old_accuracy[r['case'], r['engine']])
        row['engine'] = 'sniper-rob' if r['engine'] == 'sniper' else r['engine']
        rows.append(row)
    aggregates = []
    for n in [4, 8]:
        for engine in ['fastsim', 'zsim', 'sniper-rob', 'sniper-interval']:
            group = [r for r in rows if r['cores'] == n and r['engine'] == engine]
            assert len(group) == 5
            aggregates.append(dict(cores=n, engine=engine, cases=5,
                **summarize_rates(group),
                batch_simulation_user_mups=sum(r['user_uops'] for r in group)/sum(r['simulation_seconds'] for r in group)/1e6,
                completion_time_mape_percent=statistics.mean(abs(r['relative_error']) for r in group)*100,
                elapsed_macro_cycle_proxy_mae=statistics.mean(r['absolute_error_cycles_per_macro'] for r in group)))
    data = dict(schema='sniper-interval-comparison-v1', repeats_per_case=1,
        formal_validation=False, formal_throughput_acceptance=False,
        policy='preparation-included-extra-validation-excluded-v1',
        interval_campaign=str(a.campaign.resolve()), preserved_baseline=str(a.baseline.resolve()),
        interval_binary_sha256=freeze['binary_sha256']['sniper'],
        validation_receipt_sha256=receipt_sha, cpu_budget='52-63',
        reference_freshly_audited=False, aggregates=aggregates, cases=rows)
    a.output_prefix.with_suffix('.json').write_text(json.dumps(data, indent=2)+'\n')
    fields = ['case','cores','engine','e2e_user_mups','e2e_seconds','simulation_user_mups',
        'simulation_seconds','reference_seconds','model_seconds','relative_error',
        'absolute_error_cycles_per_macro','source_campaign']
    with a.output_prefix.with_suffix('.csv').open('w') as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore'); w.writeheader(); w.writerows(rows)
    lines = ['# Sniper interval / ROB 同输入对照（2026-11-16）', '',
        '新跑 Sniper interval 的完整 C4/C8 各五项 FST；FastSim、并行准备 zsim、Sniper ROB 复用已冻结的原测量。三者旧模型没有在本轮重跑。', '',
        '主吞吐包含读取、解压、转换、初始化、功能 warmup、模拟和必要排空；额外可靠性审计在计时外。单位为百万测量段 user UOP/s。按每组总工作量÷总墙钟汇总；正式模拟速率是辅助项。', '',
        '唯一配置切换为 perf_model/core/type=interval；保留原 SPR FST traits、512 窗口、6-wide dispatch、缓存、分支预测/I-cache 和 100 ns barrier。interval 补齐 FST 显式依赖、尾部片段、末尾窗口排空及 FU 类型接口；使用 Sniper 原生 interval window/critical-path 算法。FU 仍为既有 SPR 池容量近似，EOF 使用 interval 原生 critical-path / memory 完成坐标的隐式 flush 估计，并非 ROB 周期精确退休。该配置不等于论文关闭 I-cache/BPred/同步的 interval 速度配置。', '',
        '各参与核实际 interval dispatch 宏/UOP 计数与完整 FST 保留人口一致，且最后模型完成端点完整。不能以 reader 读入计数冒充模型完成。原 ROB 的退休计数继续保留原义。', '',
        '| 核数 | 引擎 | 端到端 MUPS | 正式模拟 MUPS（辅助） | 完成时间 MAPE | proxy MAE (cycles/macro) |',
        '|---|---|---:|---:|---:|---:|']
    for r in aggregates:
        lines.append(f"| C{r['cores']} | {r['engine']} | {r['batch_e2e_user_mups']:.3f} | {r['batch_simulation_user_mups']:.3f} | {r['completion_time_mape_percent']:.2f}% | {r['elapsed_macro_cycle_proxy_mae']:.4f} |")
    lines += ['', '完成时间诊断参考保存的 gem5 退休终点审计：本次核对其 SHA、参与核、宏/UOP 人口和记录序号终点，未重新生成完整 native 审计。MAPE 按负载等权；proxy = 秒数×参考频率×核数÷全部物理宏指令。它包含其他上下文，不是 workload-task active CPI；native PMU 和硬件等价仍未认证。', '',
        '| Case | ROB MUPS | interval MUPS | interval / ROB 吞吐 | interval 完成时间 APE | proxy 绝对误差 (cycles/macro) |',
        '|---|---:|---:|---:|---:|---:|']
    for case in sorted(expected):
        ni = next(r for r in rows if r['case'] == case and r['engine'] == 'sniper-interval')
        ro = next(r for r in rows if r['case'] == case and r['engine'] == 'sniper-rob')
        lines.append(f"| {case} | {ro['e2e_user_mups']:.3f} | {ni['e2e_user_mups']:.3f} | {ni['e2e_user_mups']/ro['e2e_user_mups']:.2f}× | {abs(ni['relative_error'])*100:.2f}% | {ni['absolute_error_cycles_per_macro']:.4f} |")
    lines += ['', '每项测量一次，CPU 52–63、NUMA 1、已有 performance 策略；未声称共享主机独占、重复测量吞吐验收或模型等价。缺失指令长度继续沿用显式 1-byte fallback，并保留每核 unknown 数。', '',
        f"interval ELF SHA256：`{data['interval_binary_sha256']}`。",
        f"原始批次：`{data['interval_campaign']}`。",
        f"保留基线：`{data['preserved_baseline']}`。", '']
    a.output_prefix.with_suffix('.md').write_text('\n'.join(lines))
    print(json.dumps(aggregates, indent=2))


if __name__ == '__main__':
    main()
