# R0/R1 v2-wait 退休 load cache miss：含极少量 unknown 的五负载误差

按用户明确要求“忽视这些极少量的unknown，也要报告”，纳入 XZ、Marian，使用已分类的原生退休事件次数作为参考分母，保留各项 unknown 和原验证器状态。本报告为含极少量 unknown 的暂定误差；原先三负载、零原生 unknown 的汇总仍单独保留。

版本：主工作区恢复的 R0/R1 `v2-wait`，二进制 SHA-256 `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。范围：四核 workload-task，指标为 `MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS`，模型取全量 `estimated_count`。参考沿用原维护验证器记录的独立 retired-cache 审计事实，含显式服务到事件估计；本轮未重跑仿真、未修改预测、未切换缓存参考来源。

## 五负载相对误差

相对误差 = (FastSim − 已分类 gem5 次数) / 已分类 gem5 次数。正值表示高估。

| 负载 | 退休 L1 miss | 退休 L2 miss | 退休 L3 miss | 各项原生 unknown |
|---|---:|---:|---:|---:|
| Zstd | +50.1912% | +109.0888% | +122.6227% | 0/0/0 |
| XZ | -55.5467% | +3.4365% | +3.2281% | 1/1/1 |
| FLAC | -20.1977% | +9.8282% | +24.8485% | 0/0/0 |
| Graph500 | +27.9210% | +0.0104% | +0.0025% | 0/0/0 |
| Marian | -31.5316% | +3.1412% | +2.1026% | 1/1/1 |

## 原始计数与绝对次数误差

| 负载 | 事件 | gem5 已分类次数 | FastSim 全量预测 | 绝对次数误差 | 相对误差 |
|---|---|---:|---:|---:|---:|
| Zstd | retired_l1_load_misses | 680,317 | 1,021,776 | 341,459 | +50.1912% |
| Zstd | retired_l2_load_misses | 30,763 | 64,322 | 33,559 | +109.0888% |
| Zstd | retired_l3_load_misses | 26,416 | 58,808 | 32,392 | +122.6227% |
| XZ | retired_l1_load_misses | 181,784 | 80,809 | 100,975 | -55.5467% |
| XZ | retired_l2_load_misses | 11,727 | 12,130 | 403 | +3.4365% |
| XZ | retired_l3_load_misses | 11,369 | 11,736 | 367 | +3.2281% |
| FLAC | retired_l1_load_misses | 44,624 | 35,611 | 9,013 | -20.1977% |
| FLAC | retired_l2_load_misses | 1,048 | 1,151 | 103 | +9.8282% |
| FLAC | retired_l3_load_misses | 165 | 206 | 41 | +24.8485% |
| Graph500 | retired_l1_load_misses | 4,905,964 | 6,275,758 | 1,369,794 | +27.9210% |
| Graph500 | retired_l2_load_misses | 277,711 | 277,740 | 29 | +0.0104% |
| Graph500 | retired_l3_load_misses | 277,638 | 277,645 | 7 | +0.0025% |
| Marian | retired_l1_load_misses | 262,223 | 179,540 | 82,683 | -31.5316% |
| Marian | retired_l2_load_misses | 17,764 | 18,322 | 558 | +3.1412% |
| Marian | retired_l3_load_misses | 16,789 | 17,142 | 353 | +2.1026% |

## 五负载暂定汇总

MAPE 为五个负载等权的绝对相对误差均值；WAPE 为绝对次数误差总和 / 参考次数总和。原始计数与每千宏指令归一化误差在两方宏人口相同的本组实验中一致。

| 事件 | 有限 APE 负载 | 五负载 MAPE | 五负载 WAPE | 最大 APE |
|---|---:|---:|---:|---:|
| retired_l1_load_misses | 5/5 | 37.0776% | 31.3408% | 55.5467% |
| retired_l2_load_misses | 5/5 | 25.1010% | 10.2214% | 109.0888% |
| retired_l3_load_misses | 5/5 | 30.5609% | 9.9766% | 122.6227% |

| 事件 | P50 APE | P90 APE | P99 APE | 次数加权有符号偏差 |
|---|---:|---:|---:|---:|
| retired_l1_load_misses | 31.5316% | 53.4045% | 55.3325% | +24.9976% |
| retired_l2_load_misses | 3.4365% | 69.3846% | 105.1184% | +10.2214% |
| retired_l3_load_misses | 3.2281% | 83.5130% | 118.7117% | +9.9766% |

## unknown 的量级与口径修正

XZ core2、Marian core1 在 L1/L2/L3 三项的原生 unknown 均各为 1；原维护验证器在首先检查 L1 时退出。此前报告文字称这两个负载的 L2/L3 unknown 为零有误，逐事件原始记录实际均为 1。本次表格及 JSON 已明确保留正确值。

若每项这一个未知成员最终贡献 0 或 1 次 miss，则参考总数位于 [已分类次数, 已分类次数+1]，相对误差变动可由该区间直接计算。本组最大影响为 **0.009079 个百分点**，远小于本组主要误差。各事件区间和上限见 JSON。

XZ、Marian 的原维护验证器状态仍为 failed。本报告遵照用户要求提供诊断误差，不改写原生 unknown，不改变此前门禁结果；硬件等价仍未认证。

## 同版本 CPI 对照

| 负载 | gem5 CPI | FastSim CPI | CPI 绝对误差（cycles/macro） | CPI 相对误差 |
|---|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 0.063733 | -5.28% |
| XZ | 0.575297 | 0.601359 | 0.026062 | +4.53% |
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.89% |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | -13.51% |
| Marian | 2.364997 | 3.068823 | 0.703826 | +29.76% |

同版五负载 CPI MAE 为 0.191599 cycles/macro，CPI MAPE 为 12.99%。

## 证据与数据

- [逐事件数据及参考不确定区间](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-including-unknown-20261111.json)
- [完整 CSV](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-including-unknown-20261111.csv)
- [原三负载报告及覆盖修正](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-20261111.md)
- [当前版本恢复与同输入验证](file:///data00/yinhaolang/FastSim/fastsim-r01-main-restoration-validation-20261111.md)

- [c04-777.zstd_r 原生审计报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-777.zstd_r/validation.json)
- [c04-801.xz_s 原生审计报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-801.xz_s/validation.json)
- [c04-817.flac_s 原生审计报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-817.flac_s/validation.json)
- [c04-854.graph500_s 原生审计报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-854.graph500_s/validation.json)
- [c04-872.marian_s 原生审计报告](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/c04-872.marian_s/validation.json)
