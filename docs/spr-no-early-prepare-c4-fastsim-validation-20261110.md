# 新 C4 FST 的 FastSim 关闭预取回放

用户2026-11-10要求忽略少量unknown，使用FastSim关闭预取验证新五案FST。
本批目录为`tmp/spr-no-prefetch-no-early-prepare-c4-fastsim-20261110/`。

## 输入和模型控制

输入只来自`tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/`，
包含zstd、XZ、Graph500、FLAC、Marian C4。五案采集均返回0，硬件预取及
early store prepare关闭，20个详细核的投机prepare发出计数均为0。
Marian和XZ的每个退休miss事件各有1个native unknown，其余三案为0。

沿用上一批关闭预取验证的冻结FastSim及冻结基础配置链，二进制SHA256为
`c3f3b5009e6dfaeaec89a094881743c20af6b15b5138b166e08a5623923cfb0c`。
当前`build/fastsim`与此哈希一致。按每案实际config.ini重新映射硬件参数、
split-range 32GiB内存、32控制器与IQ分区；基配置从上一轮冻结快照复制，
不会将正在开发的候选模型混入本轮控制实验。

FastSim预取相关bool全部显式false，包括软件预取提前应答选项；保留真实
ISA软件预取指令及正常需求流量。回放完成后检查配置导出值均关闭，并要求
三个硬件预取计数组内的数值全部为0。

五案并行，各用不重叠的36个逻辑CPU，避开已有实验使用的128–139号核。
并发wall时间不作为正式吞吐测量。

## unknown例外与验证边界

用户本轮指令明确覆盖原有零unknown门禁，但不改写原始参考、人口或unknown。
继续执行维护版`validate_fs_oracle_identity.py`和
`run_native_kernel_fastsim_validation.py --source-root ... --stats-dir ...`，
后者验证native来源、同窗、CPI及基础PMU。独立保留原始退休miss来源审计。

退休miss比较先用维护版`validate_report`、`validate_miss_estimates`核验原语，
逐核逐scope核对宏指令、UOP和eligible-load人口，以及完整输入身份、task集合、
预测/参考策略；这些条件不因忽略unknown而放宽。参考无unknown的case还调用
原维护版`compare_retired_cache`进行严格契约比较。

对于少量unknown的case，使用保留的native已知positive count与FastSim全量
估计计算raw诊断差值，逐事件保存原始`reference_unknown`；不把unknown改为
hit/miss，不修改生产验证器的默认规则。标记为
`user-authorized-unknown-tolerant-diagnostic`。这些结果不升级为硬件PMU认证。

CPI使用维护版来源和时间核算，保存相对误差及cycles/macro绝对误差；
汇总同时输出CPI MAPE和MAE。

## 状态和结果

- `status.json`：当前五案阶段、进程和失败信息。
- `identity.json`：独立目标身份；`freeze.json`：模型、配置和参考审计哈希。
- `stats/<case>.json`：原始FastSim输出。
- `logs/<case>/prefetch-off-audit.json`：实际关闭机制核对。
- `per-case/<case>/validation/`：维护验证器结果。
- `comparison/<case>.json`：逐案CPI和退休L1/L2/L3 miss诊断误差。
- `summary.json`、`completion.json`：五案结束后生成。

回放已提交；完成与否以状态和结果文件为准，不因已提交命令就声称验证通过。

## 完成结果

五案回放、维护版来源验证、逐核人口和预取关闭检查均已通过；硬件预取计数
全部为0。汇总脚本最初遗漏维护比较入口要求的顶层functional input identity
绑定，导致`KeyError('input_identity')`。修订后使用维护版
`collect_source_identity` / `validate_identity`重核完整来源，再重算已有输出；
未重跑模拟、未改变预测值。原失败状态保留为`*.before-report-repair.json`，
修订收据为`report-repair-receipt.json`。

| 负载 | gem5 CPI | FastSim CPI | signed CPI误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| zstd | 1.206049 | 1.136342 | -5.78% | 0.069708 |
| XZ | 0.575297 | 0.598726 | +4.07% | 0.023429 |
| Graph500 | 0.966422 | 0.754675 | -21.91% | 0.211747 |
| FLAC | 0.284584 | 0.319077 | +12.12% | 0.034493 |
| Marian | 2.364997 | 3.040902 | +28.58% | 0.675905 |

五案CPI MAPE为14.492523%，MAE为0.203056 cycles/macro。

退休miss为用户授权的raw诊断比较，保留Marian/XZ每事件各1个unknown，
不作为完整硬件PMU精度。正值为FastSim多报：

| 负载 | L1 miss signed误差 | L2 miss signed误差 | L3 miss signed误差 |
|---|---:|---:|---:|
| zstd | +52.08% | +109.07% | +122.62% |
| XZ | -55.31% | +3.43% | +3.23% |
| Graph500 | +7.84% | +0.0101% | +0.0025% |
| FLAC | -20.29% | +10.02% | +24.85% |
| Marian | -31.10% | +3.06% | +2.10% |

每事件原始计数、绝对计数误差、unknown及来源身份见本批`summary.json`。
本批FST与原开启early prepare的FST不是同一动态人口，不能将跨批误差变化
直接解释为FastSim模型修复收益或某机制的反事实贡献。
