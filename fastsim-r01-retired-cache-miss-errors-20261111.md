# R0/R1 v2-wait 退休 load cache miss 误差（2026-11-11）

当前 `fastsim-spr` 主工作区已恢复 R0/R1 冻结实现，主程序 SHA-256 为 `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`，与冻结二进制完全相同。五负载同输入模型结果已逐项复现，本报告使用维护验证器已有的独立 retired-cache 比较；未修改模型或重新选择参考。[恢复验证](file:///data00/yinhaolang/FastSim/fastsim-r01-main-restoration-validation-20261111.md)

范围为四核 workload-task（含其用户与内核活动），单位为退休宏指令事件次数。目标为 `MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS`（D1/08、D1/10、D1/20）。使用 `spr-retired-load-miss-v2` 全量预测 `estimated_count`，不使用原生 tag miss 或 TaoTrace path-class 计数替代退休事件。[来源与覆盖合同](file:///data00/yinhaolang/FastSim/docs/accuracy-reporting-contract.md)

三个负载的原生参考在三个主事件上 unknown=0，但服务到事件的部分成员仍有显式估计，ISA/硬件等价及错误路径、预取对缓存状态的间接影响未完全认证。下列误差沿用维护入口的 `incomplete-evidence` 状态，属于暂定误差，不能称为严格 SPR 硬件 perf 精度。

## 逐负载相对误差

相对误差 = (FastSim − gem5) / gem5；正值表示高估。N/A 不参加聚合。

| 负载 | 退休 L1 miss | 退休 L2 miss | 退休 L3 miss |
|---|---:|---:|---:|
| Zstd | +50.1912% | +109.0888% | +122.6227% |
| XZ | N/A | N/A | N/A |
| FLAC | -20.1977% | +9.8282% | +24.8485% |
| Graph500 | +27.9210% | +0.0104% | +0.0025% |
| Marian | N/A | N/A | N/A |

## 参考分类完整的三负载：原始计数与误差

| 负载 | 事件 | gem5 | FastSim | 绝对次数误差 | 相对误差 |
|---|---|---:|---:|---:|---:|
| Zstd | retired_l1_load_misses | 680,317 | 1,021,776 | 341,459 | +50.1912% |
| Zstd | retired_l2_load_misses | 30,763 | 64,322 | 33,559 | +109.0888% |
| Zstd | retired_l3_load_misses | 26,416 | 58,808 | 32,392 | +122.6227% |
| FLAC | retired_l1_load_misses | 44,624 | 35,611 | 9,013 | -20.1977% |
| FLAC | retired_l2_load_misses | 1,048 | 1,151 | 103 | +9.8282% |
| FLAC | retired_l3_load_misses | 165 | 206 | 41 | +24.8485% |
| Graph500 | retired_l1_load_misses | 4,905,964 | 6,275,758 | 1,369,794 | +27.9210% |
| Graph500 | retired_l2_load_misses | 277,711 | 277,740 | 29 | +0.0104% |
| Graph500 | retired_l3_load_misses | 277,638 | 277,645 | 7 | +0.0025% |

## 暂定汇总：仅 Zstd、FLAC、Graph500

MAPE 为三个负载等权的绝对相对误差均值；WAPE 为绝对次数误差之和 / gem5 次数之和。两者分母不同，不能互换；不代表完整五负载精度。

| 事件 | 有限 APE 负载 | 暂定 MAPE | 暂定 WAPE | 最大 APE |
|---|---:|---:|---:|---:|
| retired_l1_load_misses | 3/5 | 32.7699% | 30.5504% | 50.1912% |
| retired_l2_load_misses | 3/5 | 39.6425% | 10.8848% | 109.0888% |
| retired_l3_load_misses | 3/5 | 49.1579% | 10.6634% | 122.6227% |

| 事件 | P50 APE | P90 APE | P99 APE | 次数加权有符号偏差 |
|---|---:|---:|---:|---:|
| retired_l1_load_misses | 27.9210% | 45.7371% | 49.7458% | +30.2303% |
| retired_l2_load_misses | 9.8282% | 89.2367% | 107.1036% | +10.8848% |
| retired_l3_load_misses | 24.8485% | 103.0678% | 120.6672% | +10.6634% |

## 未评分负载与预测覆盖

XZ 的 core2、Marian 的 core1 在 workload_task 退休 L1/L2/L3 miss 三项中，原生资格 unknown 均各为 1。维护验证器首先在 L1 检查时退出，因此在 case 层拒绝三个主事件计分。原文称 L2/L3 unknown 为零有误，本次按逐事件原始审计记录修正。两负载的全量模型预测保留如下。

用户随后明确要求忽略这些极少量 unknown 并报告误差；五负载暂定误差另见[含 unknown 的完整报告](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-including-unknown-20261111.md)。本页三负载汇总与原始评分状态保持原口径；修正前文本留存在[原报告备份](file:///data00/yinhaolang/FastSim/tmp/restore-r01-main-20261111/retired-miss-report-before-unknown-clarification.md)。

| 负载 | 全量 L1 miss 预测 | 全量 L2 miss 预测 | 全量 L3 miss 预测 |
|---|---:|---:|---:|
| XZ | 80,809 | 12,130 | 11,736 |
| Marian | 179,540 | 18,322 | 17,142 |

模型旧资格字段仍有 Zstd 8858、Marian 1 个 unknown；三个主事件的全量点预测 `prediction_unknown` 均为零。旧资格 unknown 与全量预测覆盖不同，未删除这些诊断字段，也未将原生 unknown 当零。

Zstd 的 L2/L3 次数分别多 33,559 / 32,392；FLAC 的 L3 只多 41 次，因参考仅 165 次而呈现 24.85% 相对误差。Graph500 的 L2/L3 分别只多 29 / 7 次，但 L1 多 1,369,794 次。上述是偏差分布，尚不构成根因归因。

## 同版本 CPI 对照

| 负载 | gem5 CPI | FastSim CPI | CPI 绝对误差（cycles/macro） | CPI 相对误差 |
|---|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 0.063733 | -5.28% |
| XZ | 0.575297 | 0.601359 | 0.026062 | +4.53% |
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.89% |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | -13.51% |
| Marian | 2.364997 | 3.068823 | 0.703826 | +29.76% |

同版本五负载 CPI MAE 为 0.191599 cycles/macro，MAPE 为 12.99%；该五负载 CPI 汇总与三负载 PMU 暂定汇总的样本集合不同。

## 证据

[逐事件字段、覆盖与来源哈希](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-20261111.json)；[逐事件 CSV](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-20261111.csv)

- [c04-777.zstd_r 维护原生验证报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-777.zstd_r/validation.json)
- [c04-801.xz_s 维护原生验证报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-801.xz_s/validation.json)
- [c04-817.flac_s 维护原生验证报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-817.flac_s/validation.json)
- [c04-854.graph500_s 维护原生验证报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-854.graph500_s/validation.json)
- [c04-872.marian_s 维护原生验证报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-872.marian_s/validation.json)
