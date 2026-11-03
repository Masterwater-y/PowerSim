# 固定读服务关系：首轮三个反例结果

日期：2026-09-24。状态：首轮实现和观察完成，默认关闭。设计见 [实施方案](hybrid-fixed-read-services-round1-20260924.md)。

固定程序序服务关系已能在这些案例中运行，FR-FCFS 继续传播最终响应；结果没有支持“增加 read alias 就能修掉 buffer PMU 主要误差”的预期。LBM C4 的 CPI 绝对误差下降，NAMD 几乎不变，LBM C32 上升；缓存 PMU 基本不变。**5% 继续作为观察目标，不设精度准入门槛，不触发自动回退。**

## 数据、配置与验证

这是针对已知问题选取的三案 pilot，不是随机样本、held-out 泛化结论或全 38 案结果。所有统计均为 `user-plus-kernel`；未运行单独 user pipeline，因此 user-only 精度不可用。源目录为 `tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/`，精确案例 ID 见 [cases.json](../tmp/hybrid-fixed-path-20260924/cases.json)。

三个案例、40 条核 trace 均通过独立 target identity 检查；before/OFF/ON 的维护 native validator 均 discovered/passed/PMU-scored=3，failed/pending=0。逐核 FastSim/trace/gem5 宏指令数一致；参与核与 common-end 元数据已核对。这不代表严格硬件 PMU 认证。metadata 没有单独输出全部 common-start 逐事件身份，因此不把本 pilot 提升为完整 provenance 认证。

缓存 reference 来自 conserved native Ruby/SLICC，branch 来自原始 retired BPred outcome；使用维护 validator 的选源与计分。[事件字典](../configs/pmu-event-dictionary-v1.json) 中 branch/L1D/private-L2 为 proxy，LLC 为 diagnostic；当前没有 strict PMU headline。

配置为 buffer-114、store forwarding=true、prepare lifecycle/residence certificate=true，store-wait-relief=false；旧 read-service/repair/line-coalescing 实验关闭。ON 与 OFF 仅差 `ruby.fixed_read_services`。完整 include chain、文本快照、源码及二进制 SHA256 保存在 [fingerprints.json](../tmp/hybrid-fixed-path-20260924/fingerprints.json) 及 `config-snapshot/`。复用已记录的同一 buffer-114 时延映射，未新增／调节时延常数；原有端到端近似仍是近似，不声称实现严格 zero-load bound。

功能 warmup 使用 `fastsim-binary-warmup-slice`；前缀用于建立状态，测量 barrier 重置计数与时间，保留缓存／coherence 状态。最快核到达 10M user UOP 时统一结束；吞吐分子取实际 user UOP 总数。每核记录边界、宏指令数、结束 tick 和 stop reason 见 [provenance.json](../tmp/hybrid-fixed-path-20260924/provenance.json)。

| 案例 | 核数 | Warmup records | 测量 mixed records | 测量 macro instructions | 测量 user UOP |
|---|---|---|---|---|---|
| NAMD C4 | 4 | 2066842 | 40980800 | 27253641 | 38547500 |
| LBM C4 | 4 | 1519988 | 34154264 | 26805470 | 34052153 |
| LBM C32 | 32 | 10600173 | 293424694 | 226572645 | 292190169 |

`cmake --build build -- -j16` 与 `./build/fastsim_tests` 均通过。机制测试覆盖同行读、FR-FCFS 改序后的父响应、RAW/WB、晚到请求、跨 epoch 前缀不变、generation/过期边界、写／替换失效、preview 路由、DVFS 与不支持组合拒绝。OFF 与修改前二进制在三案的全部 `scope_metrics`（排除吞吐）及 `cores` 逐值一致。

## CPI

单位为 cycles / macro instruction；相对列为绝对相对误差。绝对误差 = |FastSim CPI − gem5 CPI|。

| 案例 | gem5 CPI | OFF CPI | OFF 绝对误差 | OFF 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 |
|---|---|---|---|---|---|---|---|
| NAMD C4 | 0.479341 | 0.389693 | 0.089648 | 18.702% | 0.389917 | 0.089424 | 18.656% |
| LBM C4 | 0.817326 | 0.775128 | 0.042199 | 5.163% | 0.852616 | 0.035290 | 4.318% |
| LBM C32 | 1.502989 | 1.758565 | 0.255576 | 17.005% | 1.797769 | 0.294779 | 19.613% |

| 模式 (3 案) | CPI MAE | MAPE | P50 APE | P90 APE | P99 APE | WAPE | Signed bias |
|---|---|---|---|---|---|---|---|
| OFF | 0.129141 | 13.623% | 17.005% | 18.363% | 18.668% | 16.373% | +14.469% |
| ON | 0.139831 | 14.195% | 18.656% | 19.421% | 19.594% | 18.687% | +17.389% |

MAPE/分位数对案例等权，分位数使用 Type 7；CPI WAPE/bias 使用各案共同宏指令人口加权。三案的尾分位数只描述该小样本，不能外推全矩阵。

## PMU 观察

以下均为 APE，OFF → ON；完整 reference/prediction 原始计数见维护 validator summary。

| 案例 | Branch | L1D tag miss | Private L2 tag miss | LLC tag miss |
|---|---|---|---|---|
| NAMD C4 | 0.623% → 0.623% | 11.398% → 11.417% | 383.313% → 383.889% | 760.696% → 760.696% |
| LBM C4 | 10.879% → 10.879% | 178.449% → 178.449% | 296.261% → 296.261% | 298.196% → 298.196% |
| LBM C32 | 12.312% → 12.312% | 233.385% → 233.384% | 296.049% → 296.047% | 298.494% → 298.479% |

| 事件 | 模式 | 映射 | finite APE | MAPE | P50 | P90 | P99 | WAPE | Signed bias |
|---|---|---|---|---|---|---|---|---|---|
| Branch | OFF | proxy | 3/3 | 7.938% | 10.879% | 12.025% | 12.283% | 3.844% | 2.944% |
| Branch | ON | proxy | 3/3 | 7.938% | 10.879% | 12.025% | 12.283% | 3.844% | 2.944% |
| L1D tag miss | OFF | proxy | 3/3 | 141.078% | 178.449% | 222.398% | 232.286% | 196.795% | 196.795% |
| L1D tag miss | ON | proxy | 3/3 | 141.084% | 178.449% | 222.397% | 232.285% | 196.797% | 196.797% |
| Private L2 tag miss | OFF | proxy | 3/3 | 325.208% | 296.261% | 365.903% | 381.572% | 296.405% | 296.405% |
| Private L2 tag miss | ON | proxy | 3/3 | 325.399% | 296.261% | 366.363% | 382.136% | 296.406% | 296.406% |
| LLC tag miss | OFF | diagnostic | 3/3 | 452.462% | 298.494% | 668.255% | 751.452% | 299.341% | 299.341% |
| LLC tag miss | ON | diagnostic | 3/3 | 452.457% | 298.479% | 668.252% | 751.451% | 299.328% | 299.328% |

缓存 reference source 分别为 `native-ruby-slicc:l1d.tag_misses`、`l2.tag_misses`、`llc.tag_misses`；branch 为 `kernel_events-v3:taotrace-retired-bpred-v1`。每事件有限 APE 覆盖 3/3。当前缓存误差离 5% 很远；不能用 WAPE 替换 MAPE 报成达标。

## 路径诊断与近似覆盖

先列测量范围 `scope_metrics.pmu` 中的净计数变化；accesses 只用于模型内部机制诊断，没有在此把它增加为硬件 PMU accuracy 项。

| 案例 | Δ L1D tag accesses | Δ L1D tag misses | Δ private L2 tag misses | Δ LLC tag misses |
|---|---|---|---|---|
| NAMD C4 | -2071435 | 65 | 24 | 0 |
| LBM C4 | -1452593 | 0 | 0 | 0 |
| LBM C32 | -11484774 | -26 | -17 | -144 |

LBM C4 的 L1 查找量明显下降，而三层 miss 数不变；净变化落在模型的 hit 计数。因此“每个被合并 follower 原本都会再产生一次 miss”的假设不成立。其他两案 miss 的净变化也很小。父服务属于 escape 路径，不等于其 follower 在 OFF 下会 miss。这仍不是超额 miss 的根因定位，需按读／store demand／permission／generation 提取事件证据。

下表来自 `causal_frontier` 机制诊断；`prepare_measurement_phase()` 中的 `initialize_stats()` 会在 warmup barrier 清零这些计数，因此不含 warmup。它们统计 owner/follower 分类与时间，不等同于缓存 miss 事件，不能直接当作 PMU 误差修正量。late 表示最终到达已不早于父 callback；reversed 表示最终到达早于父 admission。它们保留固定关系并记录近似暴露量，不触发按时间或精度回退。

| 案例 | committed epochs | owners | followers | incoming | late | reversed | fallback |
|---|---|---|---|---|---|---|---|
| NAMD C4 | 2682 | 3022256 | 2069117 | 8 | 104309 | 36983 | 0 |
| LBM C4 | 5615 | 731142 | 1416048 | 50 | 824852 | 9621 | 0 |
| LBM C32 | 15336 | 6177589 | 11398626 | 402 | 7431694 | 99161 | 0 |

late/follower 比例：NAMD C4 5.04%；LBM C4 58.25%；LBM C32 65.20%。这是固定分类近似的暴露量，尚不能直接归因为 CPI 误差占比。

## 宿主吞吐

同一优化构建二进制，每案按 OFF/ON/ON/OFF 顺序串行重跑，两次重复／模式。单位为百万 measured user UOP/s，取 `scope_metrics.throughput.user_uops_per_second`。每次 replay 的非吞吐 scope 输出均与对应验证运行一致。宿主未独占、未固定 CPU placement，记录了 affinity 和 loadavg；这是带环境噪声的 pilot 成本观察，不作小幅提速的确定结论。

本轮 ABBA 量化的是新二进制的开关增量成本；OFF 对旧二进制验证了目标统计等价，未单独做旧／新二进制的 ABBA。因此不声称新增 descriptor 字段在 OFF 下完全没有宿主成本。

| 案例 | OFF 两次 Muser-UOP/s | ON 两次 Muser-UOP/s | 均值相对变化 |
|---|---|---|---|
| NAMD C4 | 4.987 / 4.857 | 3.881 / 3.842 | -21.54% |
| LBM C4 | 2.869 / 2.877 | 2.445 / 2.461 | -14.61% |
| LBM C32 | 2.807 / 2.811 | 2.175 / 2.190 | -22.29% |

| 模式 (先逐案取两次均值) | Mean | P50 | P90 | P99 | Min |
|---|---|---|---|---|---|
| OFF | 3.534 | 2.873 | 4.512 | 4.881 | 2.809 |
| ON | 2.832 | 2.453 | 3.580 | 3.833 | 2.183 |

| 案例 | 模式 | 测量 wall 秒 | 含 warmup simulator wall 秒 | 完整 subprocess wall 秒 |
|---|---|---|---|---|
| NAMD C4 | OFF | 7.833 | 8.303 | 8.478 |
| NAMD C4 | ON | 9.983 | 10.541 | 10.716 |
| LBM C4 | OFF | 11.854 | 12.364 | 12.566 |
| LBM C4 | ON | 13.882 | 14.477 | 14.686 |
| LBM C32 | OFF | 104.023 | 107.784 | 108.925 |
| LBM C32 | ON | 133.866 | 138.559 | 139.700 |

采样的宿主 1 分钟 loadavg 范围 4.53–6.93；CPU 总数 192、允许 CPU 数 192，replay 并发为 1。端到端 user-UOP/s 因 native warmup 未按特权级分区，原输出为 unavailable，不自行补造该速率。

## 限制、回退与下一轮

候选默认 `ruby.fixed_read_services=false`；显式设为 false 即回到既有 buffer 路径。旧 serial/buffer 和 repair/local/no-fallback 选项保留。permission 失败会恢复层次、核心／Sequencer 计数与身份表再重放旧路径；本次三案 fallback=0，实际 simulator permission-retry 分支仅完成代码审查和组件恢复测试，没有端到端触发验证，不能声称已覆盖。

首轮限制为同频 interval_weave/time_epoch sparse-response；paired-frontier 组合尚未独立验证，late sparse-resource retiming 会移动 FU issue，均明确拒绝组合。合成 load/store/load 在 OFF 也触发既有 residual attribution 异常；写冲突测试使用现有 fast kernel，未放松 residual 检查或把该问题误归因于新候选。当前只固定普通读关系，并未完整实现所有共享系统的 bound 接口；weave 保留不等于整体精度有保证。

下一轮优先取得超额 miss 的首个分歧证据：在三个相同源上按请求身份核对普通读、buffer store prepare/head demand、permission 与替换后的 generation，区分额外请求、提前分配／回填与真实 residency 变化；同时针对 LBM C32 的 late-reference 路径做受控时间诊断。补上权限回退端到端用例，再决定模型改动与全矩阵安排。这些是基于本轮现象的后续方向，不把尚未定位的机制当成根因，也不增加 PMU 精度准入门槛。

## 可复核产物

- [身份检查](../tmp/hybrid-fixed-path-20260924/identity.json)

- [OFF 维护报告](../tmp/hybrid-fixed-path-20260924/off/summary.md)

- [ON 维护报告](../tmp/hybrid-fixed-path-20260924/on/summary.md)

- [OFF JSON](../tmp/hybrid-fixed-path-20260924/off/summary.json)

- [ON JSON](../tmp/hybrid-fixed-path-20260924/on/summary.json)

- [OFF CSV](../tmp/hybrid-fixed-path-20260924/off/summary.csv)

- [ON CSV](../tmp/hybrid-fixed-path-20260924/on/summary.csv)

- [修改前一致性](../tmp/hybrid-fixed-path-20260924/off-equivalence.json)

- [ABBA 原始记录](../tmp/hybrid-fixed-path-20260924/bench/runs.json)

- [构建日志](../tmp/hybrid-fixed-path-20260924/verified-build.log)

- [完整测试日志](../tmp/hybrid-fixed-path-20260924/verified-tests.log)

- [运行脚本](../tmp/hybrid-fixed-path-20260924/evaluate.py)

每案 `off/cases/<case_id>/`、`on/cases/<case_id>/` 中保存 `validation.json`、`fastsim.json` 和完整命令日志；配置快照与 before 二进制保存在相同实验根目录。
