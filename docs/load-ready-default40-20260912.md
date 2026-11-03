# 普通 load 就绪配置设为默认：10 负载 × C4–C32（2026-09-12）

已按用户明确要求，将维护默认切换为 `ordinary_load_latency = 3`、
`load_response_to_ready = 1`，并完成 10 个负载在 C4/C8/C16/C32 的 40 项完整输入验证。
40/40 通过运行、共同终点、原生输入与守恒检查，0 项失败；这不是 40 项精度门禁全部通过。
总体 MAPE 6.224% → 5.673%，CPI MAE 0.060409 → 0.056292，但只有 15 项改善、25 项回退。
收益主要来自 Stockfish；其余 36 项整体误差增大，见下表。

本决定替代[此前保留候选、不切换默认的决定](stockfish-load-ready-repair-20260912.md)。
用户的默认选择不等于独立 workload-held-out、参数趋势或 quiet-host 吞吐门禁已经通过。

## 默认配置与实现边界

- [维护别名](../configs/gem5-fs-native-kernel.cfg) 现在包含
  [gem5-v28_9-fs-load-ready.cfg](../configs/gem5-v28_9-fs-load-ready.cfg)，后者继承原 v28_6，
  仅增加普通 load 下界 3 和实际 response 到消费者就绪 1 cycle 两项。
- 旧版本配置不变；[gem5-exp-load-ready.cfg](../configs/gem5-exp-load-ready.cfg) 保留为兼容入口。
  使用维护别名的运行入口自动获得新设置；显式使用历史版本的运行保持历史行为。
- atomic 的旧下界 4、store AGU 时序、same-PC 依赖、两阶段 Q=1024、并行 materialized
  feedback 和既有默认关闭的实验保持不变。没有 PC/workload 特判、oracle timing 输入或补偿。
- 本轮没有改生产 C++/头文件；使用上一轮已经实现并配对验证的普通 load 就绪路径。
  这是已采集 gem5 O3/Ruby CPU 的配置，不是所有 CPU 的通用常数。

## 数据、口径与校验

- 数据集：`tmp/first-core-common-end-20260911/source/<case>/`；40 个配置、600 条逐核 trace。
  十个负载见后表，每个运行 C4/C8/C16/C32。本轮未新跑 gem5、未重采、未截短输入。
- 使用 `first-core-target-common-end-v1`：最快核达到 10M 用户 UOP 时共同关闭；
  其他核保留共同窗口中的实际人口，不补齐到目标。manifest、共同事件、参与核、FST/deps
  头、功能边界、oracle identity 与原生 PMU gate 均已复核，0 项输入拒绝。
- 这是既有矩阵回归，不是新的 workload-held-out；Stockfish 已用于组件归因。
  新值与历史冻结维护默认在同一输入和共同窗口上比较，但不是全 40 项同二进制 A/B。
- CPI 只取 `scope_metrics` 的 `user-plus-kernel` 活跃核心周期 / 各自实际完成宏指令数；
  不混入 idle，不用目标 UOP 数当分母，不注入 gem5 周期或指令数。只验证 combined 管线，
  未另跑 user-only。误差按配置等权，百分位数为 Type 7；绝对 CPI 误差与 MAE 单位均为
  cycles/macroinstruction。
- `fastsim-binary-warmup-slice` 按记录边界完整重放功能预热，在共同屏障重置测量时间和
  计数并保留模型状态。实际测量用户 UOP 为 4,991,090,907，测量混合 UOP 为
  5,256,566,858，另有功能预热混合 UOP 4,896,580,238；预热不进入测量 CPI/吞吐。
- 33 项 gem5/FastSim 宏指令分母完全对齐；另 7 项沿用既有微小差异，详见完整报告。
  新旧 FastSim 每核宏指令数精确一致，差异没有扩大，也没有替换各自分母。
  因此不能把全部 40 项称为严格宏指令人口对齐。

## CPI 结果

“旧”是历史冻结维护默认，“新”是本轮新默认。全 40 项逐项 gem5 CPI、新旧 FastSim CPI、
有符号相对误差和绝对 CPI 误差见[完整报告](../tmp/load-ready-default40-20260912.Ydiy65/report.md)
与[逐项 CSV](../tmp/load-ready-default40-20260912.Ydiy65/cases.csv)。

| 核数 | 项数 | 旧 MAPE | 新 MAPE | 旧 CPI MAE | 新 CPI MAE | 改善 / 回退 |
|---|---:|---:|---:|---:|---:|---:|
| C4 | 10 | 4.893% | 4.244% | 0.048258 | 0.043520 | 4 / 6 |
| C8 | 10 | 6.414% | 6.119% | 0.058445 | 0.057796 | 3 / 7 |
| C16 | 10 | 6.786% | 6.203% | 0.057552 | 0.052704 | 4 / 6 |
| C32 | 10 | 6.804% | 6.125% | 0.077383 | 0.071148 | 4 / 6 |
| 全部 | 40 | 6.224% | 5.673% | 0.060409 | 0.056292 | 15 / 25 |

| 负载（各 4 项） | 旧 MAPE | 新 MAPE | 旧 CPI MAE | 新 CPI MAE |
|---|---:|---:|---:|---:|
| 706.stockfish_r | 11.550% | 0.460% | 0.076914 | 0.003046 |
| 710.omnetpp_r | 5.300% | 7.334% | 0.041283 | 0.056995 |
| 777.zstd_r | 2.403% | 3.803% | 0.023992 | 0.037397 |
| 782.lbm_r | 3.084% | 3.216% | 0.112222 | 0.116875 |
| 803.sph_exa_s | 8.622% | 8.866% | 0.052890 | 0.054427 |
| 811.tealeaf_s | 10.856% | 10.630% | 0.074483 | 0.072550 |
| 816.nab_s | 2.971% | 3.350% | 0.017073 | 0.018996 |
| 854.graph500_s | 4.912% | 4.586% | 0.093642 | 0.085007 |
| 857.namd_s | 8.800% | 11.133% | 0.049044 | 0.061795 |
| 881.neutron_s | 3.744% | 3.348% | 0.062549 | 0.055834 |

| 解释性子集（不是 held-out） | 项数 | 旧 MAPE | 新 MAPE | 旧 CPI MAE | 新 CPI MAE |
|---|---:|---:|---:|---:|---:|
| Stockfish | 4 | 11.550% | 0.460% | 0.076914 | 0.003046 |
| 其余负载 | 36 | 5.632% | 6.252% | 0.058575 | 0.062208 |
| 宏指令分母完全对齐子集 | 33 | 5.840% | 5.036% | 0.061028 | 0.055661 |

结论是总体均值改善，但非 Stockfish 残差整体回退；不能据总体均值声称广泛改善。
NAMD、Omnet++、zstd 的后续问题应通过真实事件和阶段配对定位，不能仅凭 CPI
方向否认已验证的 load 就绪边，或新增按负载补偿。33 项子集也不替代全 40 项结果。

全 40 项 APE 的 P99 从 13.381% 降至 12.971%（对应整体 CPI MAE
0.060409 → 0.056292）。新默认的主要尾部包括：

| case | gem5 CPI | 新 FastSim CPI | 新有符号相对误差 | 新绝对 CPI 误差 |
|---|---:|---:|---:|---:|
| TeaLeaf C16 | 0.646776 | 0.560665 | −13.314% | 0.086111 |
| NAMD C4 | 0.629211 | 0.550976 | −12.434% | 0.078236 |
| TeaLeaf C32 | 0.501256 | 0.441014 | −12.018% | 0.060242 |
| TeaLeaf C8 | 0.724112 | 0.637337 | −11.984% | 0.086775 |

## PMU 与吞吐解释

使用冻结的 [pmu-event-dictionary-v1.json](../configs/pmu-event-dictionary-v1.json)。
以下为 gem5 原生计数的诊断对照，不是硬件 strict PMU 认证；各核数组及各事件的
有效样本数、MAPE/P50/P90/P99、WAPE 和有符号偏差全部保留在完整报告。

| 计数 | 等级 | 旧 pooled WAPE | 新 pooled WAPE |
|---|---|---:|---:|
| branch_misses | proxy | 1.705% | 1.705% |
| l1d_tag_misses | proxy | 2.034% | 2.028% |
| private_l2_tag_misses | proxy | 2.165% | 2.160% |
| llc_tag_misses | diagnostic | 1.966% | 1.969% |

本轮最多 4 项同时运行，未绑核，非 quiet-host 测试；起始主机负载与 affinity 保存在
inventory。整批运行及校验耗时 386.582 秒。测量段吞吐按配置等权的均值为
6.288 Muser-UOP/s，P50/P90/P99 为 6.508/8.088/8.953，最低 3.379。
历史基线运行并发条件不同，因此不报告加速比，也不将这些观测值作为吞吐准入证据。

## 二进制身份、测试与产物

- 本轮构建和冻结运行二进制 SHA-256：
  `f704f0bf52f95ade02a6d9eb5f988de93b290f2ca6f109b9425d2265db822e73`。
- 历史 C4/C8/C16 与 C32 基线 inventory 中的二进制 SHA-256 均为：
  `6d5b1a9775845f0768a1bb2b8489b6a83662a3bca35884ad76dfcc24aa6be14d`。
  两者不同；上一轮 C16 旧设置逐核/scope 精确复现只能作为支持证据，不能替代
  未执行的全 40 项同二进制开关 A/B。本轮各项原有有效配置字段与历史基线精确一致，
  新增两项就绪参数在 40 项输出中均为 3/1。
- 默认入口行为回归先在旧默认下观察到 resident load consumer 为 +4 的真实失败；
  切换后为 +3。补充了反馈前 interval core 普通 load 下界恰为 3 的调度行为断言，
  避免下界 2 被 response+1 路径掩盖；atomic/store 与迟到响应的既有测试一并运行。
- 最终执行 `cmake --build build -- -j16` 和 `./build/fastsim_tests`，均通过。
  整批终态须包含 40 个唯一成功 case，且冻结二进制 hash 匹配，汇总才宣布完成；
  完成标记缺失、计数错误、失败、未结束、重复及缺失 case 的负例检查均通过。
- [最终验证](../tmp/load-ready-default40-20260912.Ydiy65/verification.json) 的 13 项检查均通过，
  包括构建/测试、40 项结果、600 条 trace、配置链和二进制一致、旧分母差异不变、
  全部 native validator checks，以及本轮生产 C++/头文件未改变。

产物目录：`tmp/load-ready-default40-20260912.Ydiy65/`。
[完整报告](../tmp/load-ready-default40-20260912.Ydiy65/report.md)、
[CSV](../tmp/load-ready-default40-20260912.Ydiy65/cases.csv)、
[JSON 汇总](../tmp/load-ready-default40-20260912.Ydiy65/summary.json)、
[输入与配置清单](../tmp/load-ready-default40-20260912.Ydiy65/inventory.json)、
[终态](../tmp/load-ready-default40-20260912.Ydiy65/finished.json) 均已保留。
逐项输出、原生验证和日志位于 `cases/<case>/`。本轮没有 commit 或 push。
