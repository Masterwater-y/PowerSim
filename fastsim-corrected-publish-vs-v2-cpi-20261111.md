# 修正后的 publish 与 R0/R1 v2-wait CPI 对比

修正后的 publish 已消除主要雪崩，但五个负载的 CPI 绝对误差仍全部高于此前的 R0/R1 v2-wait。

这里的 **v2** 指改善过 Graph500 的 R0/R1 `v2-wait` 冻结二进制，SHA-256 `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`；**修正后**指隔离副本中的 `v2-publish` store 响应时长传递干预，SHA-256 `b80a8755432bfa197fa849597f2f55d8bc1ac80dc7339cf66bbd8b4093dc0343`。后者尚未集成生产源码。

复用已完成、已通过独立来源审计的同输入测量，无新增仿真或吞吐测量。两组均为五个 c04 FST、4 核、关闭预取、native `sprEarlyStorePrepare=false`，FastSim 保留 `legacy_prepare_timing=true`。两组 source root、gem5 CPI 参考及各负载 task 宏人口完全相同，逐核人口检查已通过。

CPI = workload-task active cycles / retired macro instructions，使用 common-start-v1 / retired-stream-completion-v1。RE = (FastSim CPI / gem5 CPI − 1) × 100%；AE = abs(FastSim CPI − gem5 CPI)，单位 cycles/macro。

| 负载 | gem5 CPI | v2-wait CPI | v2 RE | v2 AE | 修正后 CPI | 修正后 RE | 修正后 AE |
|---|---:|---:|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | −5.28% | 0.063733 | 1.114011 | −7.63% | 0.092038 |
| XZ | 0.575297 | 0.601359 | +4.53% | 0.026062 | 0.648483 | +12.72% | 0.073185 |
| FLAC | 0.284584 | 0.318433 | +11.89% | 0.033849 | 0.325841 | +14.50% | 0.041256 |
| Graph500 | 0.966422 | 0.835896 | −13.51% | 0.130526 | 0.699532 | −27.62% | 0.266891 |
| Marian | 2.364997 | 3.068823 | +29.76% | 0.703826 | 3.743249 | +58.28% | 1.378251 |

| 版本 | CPI MAE (cycles/macro) | MAPE | CPI WAPE |
|---|---:|---:|---:|
| R0/R1 v2-wait | 0.191599 | 12.99% | 17.48% |
| 修正后 publish | 0.370324 | 24.15% | 33.75% |

MAE、MAPE 按五个负载等权。CPI WAPE 为宏指令数加权的绝对周期误差除以同 scope 的 gem5 周期；它与 MAPE 不同。修正后 MAE 增加 0.178725 cycles/macro，MAPE 增加 11.15 个百分点。

| 负载 | 相对 v2 的 AE 增量 (cycles/macro) | APE 增量 (百分点) |
|---|---:|---:|
| Zstd | +0.028306 | +2.35 |
| XZ | +0.047124 | +8.19 |
| FLAC | +0.007408 | +2.60 |
| Graph500 | +0.136365 | +14.11 |
| Marian | +0.674425 | +28.52 |

最大剩余差距在 Marian 和 Graph500。Marian 仍明显高估，Graph500 则比 v2 更明显低估。此前确认的 canonical prepare 提前可见性缺口尚未闭合 Graph500 的真实 ROI 残差，不能把其差距全部归给该机制。

本次修正把原 publish 的五例 MAE/MAPE 从 14.901505 / 681.18% 降到 0.370324 / 24.15%，证明 store 旧时长重复收费是雪崩主因；但对照表也表明，它尚未恢复到 R0/R1 v2-wait 的精度水平。

原始数值和审计证据：

- [R0/R1 v2-wait 完整数据](fastsim-r01-v2-wait-noearly-comparison-20261110.json)
- [修正后 publish 完整数据](fastsim-v2-publish-cpi-collapse-analysis-20261110.json)
- [publish 雪崩因果分析](fastsim-v2-publish-cpi-collapse-analysis-20261110.md)
