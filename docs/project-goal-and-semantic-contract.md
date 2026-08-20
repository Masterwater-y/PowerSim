# FastSim 项目目标与 gem5 语义对齐合同

P0 baseline/measurement-contract 的实现状态与激活门禁见
[`p0-baseline-measurement-contract-implementation.md`](p0-baseline-measurement-contract-implementation.md)。

状态：规范性（normative）
生效日期：2026-08-18

本文定义 FastSim 后续采集、实现、实验、报告和默认配置变更必须遵守的最高层
目标。若历史文档、工具字段名或实验习惯与本文冲突，以本文为准；历史结果可以保留，
但必须按本文重新标注其语义和有效边界。

## 1. 项目目标

项目存在两级 baseline 关系：

```text
真实机器 perf 事件语义
          ↓ 定义和验证
gem5：项目唯一微架构精度 baseline
          ↓ 同一参数、同一事件、同一统计口径
FastSim：高吞吐、允许精度损失的近似模拟器
```

### 1.1 gem5 的目标

gem5 输出的 CPI 和 PMU 必须尽可能贴近真实机器 `perf` 的事件语义：

- 事件名称、计数对象、privilege scope、开始/结束边界和分母必须明确；
- architectural event、speculative event、cache-line request、protocol message 和
  unique memory transaction 不得混为一个计数器；
- gem5 内部方便获取的统计量只有在其 increment site 与目标 perf event 等价时，
  才能作为正式 baseline；
- 不等价但有诊断价值的 gem5 统计量必须标记为 `diagnostic`，不得进入正式精度
  headline。

这里的“贴近”首先指语义一致，其次才是数值接近。真实机器和 gem5 的实现机制可以
不同，但不能用同一名称统计不同事件。

### 1.2 FastSim 的目标

FastSim 是以 gem5 为 baseline 的高吞吐模拟器。它允许通过更紧凑的状态和调度框架
牺牲精度，但必须尽可能保持 gem5 的微架构语义：

- 相同配置名必须代表相同的目标资源或状态，例如 ROB 大小、cache 容量、相联度、
  replacement、LLC slice、DRAM channel；
- 相同 PMU 名必须具有相同的事件定义和计数单位；
- 相同测量名必须具有相同的 scope、分子和分母；
- FastSim 可以压缩状态机或使用近似排队模型，但不得把未实现组件伪装成已对齐；
- FastSim 不得使用 workload ID、在线 gem5 timing/path label、逐 case CPI 常数或
  目标答案作为推理输入。

### 1.3 终极目标：绝对精度和参数趋势同时一致

FastSim 的用户可调微架构参数必须对应 gem5 的同一参数。改变参数后，需要同时满足：

1. CPI 和正式 PMU 的绝对值尽可能接近 gem5；
2. 相对 baseline 的变化方向一致；
3. 变化幅度接近；
4. 多个配置的优劣排序接近；
5. FastSim 保持显著高于 gem5 的推理吞吐率。

因此，单一 baseline 配置上 CPI 很准，不足以证明 FastSim 可以用于微架构探索；
只匹配变化趋势但绝对 PMU 口径不同，也不合格。

## 2. 必须对齐、允许近似和明确不支持

### 2.1 必须严格同口径的内容

以下内容是 baseline 身份，不允许以吞吐量为理由改变语义：

- ISA、执行模式、core/thread 拓扑、目标时钟和测量时间域；
- CPI/PMU 的 scope、分子、分母和 reset 边界；
- pipeline width 和用户声称支持的 ROB/IQ/LQ/SQ 容量；
- 用户声称支持的 FU 数量、op latency 和 pipelining 属性；
- cache 层级映射、容量、相联度、line size、indexing、replacement、inclusion；
- LLC slice/home mapping 和用户声称支持的 coherence 行为；
- TLB 容量/replacement 以及用户声称支持的 walker 语义；
- DRAM 容量、channel/rank/bank/row 几何和地址映射；
- 每个正式 PMU 的事件定义，例如 demand access、tag miss、permission upgrade、
  remote supply、merged miss、unique fill、DRAM read；
- 配置的 runtime activation。配置文件里有一个数值但运行时组件关闭或全部 bypass，
  不算对齐。

例如，FastSim 和 gem5 不能都报告 `LLC misses`，却分别统计 LLC tag miss 和 Ruby
protocol/demand miss；也不能把 FastSim ROB 配成 192、gem5 实际使用另一个值后仍称为
同一 baseline。

### 2.2 允许的机制近似

FastSim 不要求复制 gem5 的代码结构或逐周期状态机。满足输入、资源约束、输出事件和
参数敏感性合同后，可以采用：

- interval、calendar、ledger 或有界队列代替逐周期流水线；
- compact directory 代替完整 Ruby controller；
- 聚合 network/DRAM 排队模型代替逐 flit/逐 command 对象；
- 隐式 resource lifetime 代替显式 stage object；
- 整数或有理数目标 cycle 近似 gem5 tick；
- 统计近似处理 functional trace 无法观察的状态，但必须有 held-out 验证、置信边界
  和明确开关。

允许近似不等于允许统计口径变化。模型内部可以不同，外部配置和输出语义必须稳定。

### 2.3 明确不支持优于伪对齐

不是每个 gem5 参数都必须立即在 FastSim 中实现。以 rename 为例：FastSim 可以没有
显式 rename map/free-list 状态机。如果物理寄存器参数没有可靠模型，应将其标记为
`unsupported`，不进入已验证的用户可调参数集合；不得仅复制数值后声称对齐。

同理，未实现 I-side、ITLB、StoreSet、完整 Ruby transient 或 DRAM refresh 时，应明确
报告缺失。只有当某个参数进入“支持的配置表面”后，才要求其绝对值和变化趋势通过
gem5 对照门禁。

## 3. CPI 和 PMU 的规范性语义

### 3.1 CPI

真实 `perf stat -e cycles,instructions` 的 CPI 是：

```text
perf_like_CPI(scope) = cycles(scope) / retired_instructions(scope)
```

项目正式报告必须区分：

- macro-instruction CPI：perf-like headline；
- UOP CPI：内部微架构分析指标；
- active cycles per user UOP：把 kernel 服务开销归一到 user work 的系统开销指标。

当前 FS 合同把 user 和 user+kernel cycles 都除以同一个 `N_user`/user UOP 数。该指标
适合比较“每单位 user work 的总开销”，但不等价于真实 perf CPI。迁移完成前可以保留
兼容字段，正式文档必须称为 `cycles_per_user_uop` 或 `user-work-normalized CPI`，不能
只写 `CPI` 后暗示 perf 语义。

只有用户态 functional trace 时，需要按 scope 区分可计算性：

- **user perf-like CPI 可以计算。** FST v7 的 `kMicroOp`/`kLastMicroOp` 标志保留宏指令
  退休边界，FastSim 已据此统计 `retired_instructions`。在边界守恒门禁通过时，可以计算
  `user_cycles / user_retired_instructions`；
- **user+kernel perf-like CPI 不能从用户 trace 精确计算。** 输入没有 kernel instruction
  stream，因而没有精确的 kernel retired-instruction 分母。若冻结的 syscall/page-fault/
  IRQ profile 同时给出 kernel retired instructions，FastSim 可以输出
  `modeled_user_plus_kernel_perf_like_CPI`，但它是 `proxy`，不是 input-exact 指标；
- gem5 应保存精确的 user 和 user+kernel perf-like CPI，作为辅助检验。同时保留两种
  scope 的 `cycles_per_user_uop`，用于与 FastSim 的可部署主指标直接比较；
- 禁止在 FastSim 推理时读取 gem5 的 combined retired-instruction 数作为分母。它只能
  用于离线诊断，否则会把 baseline 答案带入预测。

因此当前 common-denominator 指标继续作为 user/user+kernel 主比较口径；user
perf-like CPI可作为严格辅助指标，combined perf-like CPI在 FastSim 侧只能缺省为
`unavailable` 或明确标记为 profile-derived `proxy`。

### 3.2 PMU event dictionary

每个正式 PMU 必须有一条版本化字典记录：

| 字段 | 必须内容 |
|---|---|
| perf event | 通用名或 raw encoding、目标 CPU/PMU、`:u/:k` scope |
| count unit | instruction、UOP、memory operation、cache line、request 或 transaction |
| speculative policy | issue、completion、retirement或 squash 后是否保留 |
| gem5 source | 精确 stat/probe/increment site 和 reset boundary |
| FastSim source | 精确 counter/increment site |
| mapping status | strict、proxy 或 diagnostic |
| conservation | 与上/下游计数器的守恒关系 |

没有这条字典的 `cache-misses`、`LLC-misses`、`DTLB-accesses` 等泛化名称不得作为
正式 PMU 精度结果。

### 3.3 cache 事件必须拆分

至少区分：

- committed demand memory UOP；
- 每 cache-line demand request；
- L1D tag access/hit/miss；
- private-L2 tag access/hit/miss；
- permission upgrade；
- remote clean/dirty supply；
- shared-LLC/CHA lookup；
- LLC tag hit/miss；
- same-line secondary/merged miss；
- unique LLC fill；
- DRAM read/write transaction。

`L2 accesses = L1D misses` 或 `LLC accesses = L2 misses` 只有在事件合同明确保证时才能
作为守恒式，不能由报告工具无条件假设。

## 4. 当前代码和方案审计

判定分为：`已对齐`、`允许近似`、`阻塞性不一致`、`明确不支持`。

| 组件 | 当前状态 | 合同判定 |
|---|---|---|
| core count、8-wide、ROB/IQ/LQ/SQ=192/64/32/32 | effective report 中数值正确且状态已激活 | 已对齐 |
| FU pool | 数量、主要 latency/pipelining 对齐 | 已对齐/条件一致 |
| rename physical registers | 候选存在但正式关闭；无 wrong-path allocation | 明确不支持，当前允许；不得声称该参数可用于 DSE |
| branch predictor | table/BTB/RAS 几何对齐；speculative history、squash occupancy 不对齐 | 阻塞趋势精度 |
| L1I/ITLB | L1I 几何存在但关闭；ITLB 未建模 | 明确不支持，且是部分 CPI 尾差来源 |
| DTLB | 64-entry LRU 对齐；fixed 12-cycle walker 不等价于 gem5 page walk | 允许近似但尚未通过参数趋势合同 |
| cache geometry | L1D/private-L2/shared-LLC 几何和 FastSim replacement 对齐 | 已对齐 |
| cache request/fill lifetime | private cache miss 时立即插入 tag；缺少 response-time private transient | 阻塞性不一致 |
| PMU oracle completeness | native lifecycle/drain 已守恒；v6 修复部分边界在飞缺口，仍有 55/1,839,588（0.00299%）hierarchy 请求缺少 demand L1D outcome | 接受为显式报告的 baseline 容差；不再阻塞采集或精度优化 |
| LLC PMU | v5/v6 已拆 tag miss、upgrade、remote、TBE merge、unique fill；旧 path proxy 仅保留诊断 | gem5 baseline 已拆分，FastSim 尚未对齐 |
| TaoTrace uarch sidecar | 从最终 `config.ini` 生成，L2/L3 TreePLRU 与有效 queue 字段已显式记录 | 已对齐 |
| Ruby/coherence | compact owner/sharer/upgrade/remote/merge；无完整 transient/retry/ack | 允许近似，但 PMU 必须拆分且趋势未验证 |
| interconnect | gem5 为 SimpleNetwork；FastSim 是固定 5-cycle lower bound，旧注释误称 Garnet | 阻塞 contention 参数趋势 |
| DRAM geometry | 3 GiB runtime、8ch、rank/bank/row/address map 对齐 | 已对齐，但依赖隐藏 override |
| MemCtrl/FR-FCFS | queue 数值存在；C4/C8 effective window=1，正式 demand repair 全部 bypass | 阻塞性不一致 |
| DDR command/refresh | 多数 direct 参数关闭，refresh 未实现 | 明确不支持/部分趋势阻塞 |
| warmup | committed-visible state 保留 | 已对齐于可观测子集；不等价完整 ROI state |
| user+kernel | synthetic event/time model，不执行 gem5 kernel instruction stream | proxy，不得称 perf-like kernel microarchitecture baseline |
| effective configuration | runner override 仍存在，但单一 effective-target manifest 从最终 `config.ini` 固化其有效值 | 已对齐/可复现 |

### 4.1 当前 CPI 名称与 perf 语义不一致

`tools/compare_kernel_event_accuracy.py` 和现有报告使用：

```text
predicted cycles / n_user
```

两个 scope 的 `n_user` 都是 user trace UOP 数。这保证了应用开销比较的共同分母，但不是
`perf cycles / instructions`。这是在新项目目标下必须修复的命名和输出合同，不代表已有
数值计算错误。

### 4.2 当前 PMU headline 首先暴露 oracle 缺陷

最新 C4/C8/C16 正式数据中，LBM 对总体绝对误差的贡献为：

| 核数 | L1D | L2 | LLC |
|---:|---:|---:|---:|
| C4 | 64% | 74% | 79% |
| C8 | 60% | 69% | 89% |
| C16 | 57% | 65% | 90% |

C4 LBM 的直接守恒检查：

| 数量 | 值 |
|---|---:|
| FST committed memory UOP | 12,306,888 |
| FastSim cache-line touches | 12,306,923 |
| 跨 line 额外 touches | 35 |
| oracle `pmu_user.l1d_accesses` | 10,495,114 |
| 未被 oracle 计入的 committed memory UOP | 1,811,774 |

TaoTrace 的 PMU 更新依赖 `DataAccessComplete`/pending attribute；store/慢路径可以在
commit 时用 line-state fallback 生成 FST，但该 fallback 没有同等 PMU accounting。
因此当前 `taotrace-path-class-v2` 的 class conservation 只能证明各 PMU scope 相加守恒，
不能证明它覆盖了全部 FST memory UOP。

LBM 的 LLC 条件 miss rate 反而接近：

| 核数 | oracle | FastSim |
|---:|---:|---:|
| C4 | 99.994% | 99.922% |
| C8 | 99.996% | 99.916% |
| C16 | 99.998% | 99.910% |

所以当前 118%--132% LLC WAPE 主要是 L1D/L2 入口数量和分类差异级联，不是“LLC
条件 miss rate 错了一倍”。在 oracle 修复前，不允许据此调 LLC 容量、latency 或统一
miss scale。

### 4.3 当前 formal eligibility 判定过宽

以下描述针对 P0 修复前的 v2 工具链。P0 的 v3 validator 已改为 fail-closed；
外部补丁未应用前，现有数据仍只能按本节所述降级处理。

现有工具只要看到 `pmu_source=taotrace-path-class-v2`、class 表和 syscall profile 就把
PMU 标为 formal eligible。它没有检查：

- FST committed memory count 与 PMU access count 的覆盖守恒；
- memory-UOP 和 cache-line request 的计数单位；
- packet/fallback/no-callback 来源覆盖；
- remote/upgrade/tag/merged/unique-fill 的语义分离；
- sidecar replacement/queue/network identity。

因此当前 60/60 推理成功和 30/30 oracle 结构校验有效，但不等价于 cache PMU 语义已经
通过。该批 cache PMU 应降级为 diagnostic，CPI 结果仍可保留。

## 5. 下一步优化方案

### P0：修复 baseline 和测量合同

这是继续调 FastSim 前的前置条件。

FastSim 仓库内实现与跨仓补丁已经完成，并已在实际 `gem5-fs`/`TCSim`/`taogen`
运行路径激活。重编后的 gem5 通过四 workload、C4、100K records/core 的短门禁；
生产者/runtime 修改见 `patches/p0-external-baseline-contract.patch`，遗漏的 v3 usergate
与 summarizer 消费者修复见
`patches/p0-external-baseline-contract-consumers.patch`，真实 O3 timing-translation dTLB
归因修复见 `patches/p0-external-dtlb-attribution.patch`，跨核 completion-to-commit
key 隔离见 `patches/p0-external-per-core-pending-attribution.patch`。逐项状态、最终二进制哈希和
superseding C4 门禁证据见本文开头链接的 P0 记录。

1. 建立版本化 perf/gem5/FastSim event dictionary。
2. gem5 在全局 warmup→measurement 边界 reset/snapshot 目标 stats，只保留 ROI delta；
   cache state 不重置。
3. TaoTrace 增加以下只读审计计数：
   - committed load/store/atomic；
   - packet-attributed；
   - fallback-attributed；
   - completion-before/after-commit；
   - unaccounted-at-end。
4. fallback memory path执行一次且仅一次 scope-locked PMU accounting。
5. 明确 memory-UOP 与 line-request 两套计数，跨 line 展开分别守恒。
6. LLC 拆成 upgrade、remote supply、CHA lookup、tag miss、merged miss、unique fill 和
   DRAM transaction；旧 `llc_misses` 不再进入 strict gate。
7. 从最终 `config.ini` 自动生成 `uarch_profile.json`，扩展 fail-closed identity gate。
8. 生成单一 effective target manifest，消除 3 GiB、core count、FS DTLB 的隐藏 override。
9. 输出严格的 user perf-like macro CPI；gem5 保存精确的 user/user+kernel perf-like
   CPI。FastSim combined perf-like CPI 仅在 kernel retired-instruction profile 可用时
   作为 `proxy` 输出；现有 common-user-UOP 主指标显式改名并保留。

P0 验收：

```text
FST memory UOP = packet + fallback + explicit rejection
line requests = per-UOP touched-line expansion
unaccounted = 0
duplicate accounting = 0
dtlb unknown = 0
dtlb accesses = dtlb hits + dtlb misses = memory UOPs per PMU scope
```

### P1：闭合 committed-functional 可观察状态

P0 完成后才修改 FastSim cache/timing：

1. private L1D/L2 增加 request-time transient 和 response-time fill visibility；
2. secondary request 计 demand miss，但与 parent 共享一个 unique fill；
3. replacement 只在与 gem5 等价的 lookup/fill 时机更新；
4. 建立 `request→private lookup→coherence/CHA→LLC→DRAM→fill→waiter` 守恒 ledger；
5. 使用相同输入顺序输出逐级 confusion matrix，不使用 oracle label驱动推理；
6. 修正 SimpleNetwork provenance，并实现最小 per-link/per-vnet bandwidth calendar；
7. 认证 MemCtrl arrival 后再启用 FR-FCFS、read/write turnaround、page policy 和 refresh。

第一批负载必须包含：

- LBM：store/streaming/下游级联；
- zstd：跨 line 和 first-touch/cache state；
- Graph500：L2/NoC/共享访问；
- NAMD：稀疏 L2/LLC 和 combined scope；
- Stockfish：frontend/branch 主尾差，作为 cache 修复非回归对照。

### P2：处理 committed trace 不可观察状态

wrong path、完整 I-side/ITLB、page-table physical request 和 ROI in-flight state 不能从
committed functional trace 精确恢复。处理原则是：

1. gem5-only label 只用于离线归因和评分，不进入 FST 推理输入；
2. gem5 functional trace 和 drmemtrace 必须能生成同一可部署输入合同；
3. 能由 committed PC/branch/address stream 推导的状态，使用确定性模型；
4. 不能推导的状态只能采用无 workload ID、冻结参数、held-out 验证的统计近似；
5. 近似不通过趋势 gate 时保持关闭，并在支持矩阵中标记 unsupported。

### P3：建立微架构参数趋势验证

每个声称支持的参数至少选择 baseline 两侧的值：

| 参数族 | 首批建议值 |
|---|---|
| fetch/issue/commit width | 4 / 8 |
| ROB | 128 / 192 / 256 |
| IQ | 48 / 64 / 96 |
| LQ/SQ | 16 / 32 / 64 |
| L1D | 16/32/64 KiB，匹配相联度实验 |
| private L2 | 512 KiB / 1 MiB / 2 MiB |
| LLC | 32/64/128 MiB，4/8 slices |
| DTLB | 32/64/128 entries |
| DRAM channels | 4 / 8 |
| network bandwidth | baseline 的 0.5x / 1x / 2x |

对每个 workload/core count 报告：

```text
absolute error
delta_gem5 = metric(gem5_variant) / metric(gem5_base) - 1
delta_fastsim = metric(fastsim_variant) / metric(fastsim_base) - 1
direction match
abs(delta_fastsim - delta_gem5)
cross-configuration ranking
```

推荐正式门禁：

- CPI MAPE ≤ 6%，P90 ≤ 10%；
- strict high-volume PMU WAPE 先 ≤ 5%，最终目标 ≤ 2%；
- material parameter direction accuracy ≥ 90%；
- CPI/PMU delta error P90 ≤ 5 percentage points；
- ranking accuracy ≥ 90%；
- quiet-host sequential minimum throughput ≥ 5M user UOP/s。

阈值可以由正式项目决策调整，但同一次候选验证不得为通过 gate 临时更改。

## 6. 实验和默认配置准入规则

后续每个实验必须回答：

1. 它修复哪一条 perf→gem5 或 gem5→FastSim 语义差异？
2. 对应事件和参数的 increment/activation site 在哪里？
3. 输入是否同时可由 gem5 functional trace 和 drmemtrace/FST 合同提供？
4. calibration 和 held-out 轴是什么？
5. 绝对 CPI/PMU、参数 delta、ranking 和吞吐是否同时报告？
6. 是否存在 opposite-signed workload 回退？
7. 状态机实际触发多少次，是否全部 bypass？

候选只有在以下条件全部满足后才能默认开启：

- baseline identity 和 oracle conservation 通过；
- 不使用 workload-specific 参数或在线答案；
- held-out 绝对精度和趋势精度改善或在门槛内不回退；
- historical regression suite 不回退；
- minimum throughput 过门禁；
- 文档、effective config、报告 metadata 和测试同时更新。

## 7. 当前结论和实施顺序

当前 FastSim 仍是 `gem5-derived committed-functional approximation`，不是完整
`gem5-semantic-equivalent simulator`。已有 core/cache/DRAM 几何对齐具有价值；此前的
cache PMU coverage、CPI perf 命名、LLC 事件合同、dTLB 归因和 sidecar identity P0
阻塞项已经闭合。P1 已证明 committed cache path proxy 与 Ruby controller population
不同，并已建立不进入 FST 的 native observer。v5 严格 gate 发现少量测量起点在飞请求
缺失 pre-marker SLICC outcome，v6 以有限 preboundary ledger 修复其中 13 个。v6 最终只剩
55/1,839,588（0.00299%）hierarchy 请求缺少 demand L1D outcome，最坏 NAMD 为
0.01603%；该量级不是当前 CPI/PMU 精度瓶颈。项目决定将其作为显式报告的 baseline 容差，
不再实施 request-type v7，也不再阻塞新标签采集。当前任务转为：用同窗口 native 标签
量化 FastSim lookup/merge/fill 状态机差异，并只用 functional 输入修正 FastSim。

正式采集不再默认序列化逐 UOP native JSONL。gem5 保留同一 identity registry，并在
committed UOP 的 Ruby lifecycle 终结后直接聚合 `taotrace-native-summary-v1`；每核只写
一个汇总 JSON，异常样本有界，完整 JSONL 仅是显式 debug 模式。这是输出表示优化，
没有改变 PMU population、scope、target drain、FST 或 FastSim 在线输入合同。

C4/C8 首轮正式采集暴露的永久 pending identity 已闭合。根因不是 warmup 长度，也不是
Ruby 漏 response，而是两处 native identity 合同错误：fallback 路径把可跨同一 x86 宏
指令内多个 memory UOP 复用的 proxy `SharedAttr` 当成当前 UOP 的 native Ruby lifecycle；
同时 response-complete identity 在 SLICC hierarchy fact 到齐前被提前删除。修复后 native
事实只来自精确 Request extension/registry，lifecycle 与 hierarchy completion 分别守恒，
split request 聚合保留 main request 的 issuance closure。Omnet C4 100K 严格复验达到
100,045/100,045 committed/accounted、88,846/88,846 admission/response、
83,340/83,340 hierarchy/L1D，pending、hierarchy gap 和 anomaly 均为 0。正式数据仍须按
单一最终 gem5 binary identity 重跑全部 20 个 C4/C8 case，不能混用旧 15 个结果。

首轮 P1 审计证明旧 raw gem5 stats 不能直接算 APE/WAPE。随后 lifecycle drain、
controller/TBE probe、value-only identity 和 measurement-boundary inflight 修复闭合了
response outcome；v5 又以 mandatory-queue enqueue 取代错误的 alias 推断，v6 保留只属于
边界在飞请求的 warmup 期 controller outcome。当前证据更符合“全部 Ruby request 与
demand L1D population 混用”的小缺口，不能再归因于 warmup 长度，也没有 FS 路径落入
SE atomic 的证据。证据和事件
定义见 [`p2-native-ruby-pmu-contract-2026-08-19.md`](p2-native-ruby-pmu-contract-2026-08-19.md)。

实施顺序固定为：

```text
P0 event/oracle/effective-config contract
→ P1 committed cache/transient/network/memory semantics
→ P2 unobservable-state approximations
→ P3 complete absolute + parameter-trend gate
```

禁止用调 LLC latency、miss multiplier 或 workload-specific residual 代替 P1
population 对齐；也禁止让未通过 P0 gate 的数据进入正式精度结论。否则仍会把 FastSim
拟合到一个语义不完整的 baseline。

## 8. 相关文档

- 报告统计格式：[accuracy-reporting-contract.md](accuracy-reporting-contract.md)
- 当前全组件审计：[fs-gem5-uarch-semantic-alignment-audit-2026-08-18.md](fs-gem5-uarch-semantic-alignment-audit-2026-08-18.md)
- P1 原生 PMU population 与 response-boundary 审计：[p1-native-pmu-population-audit-2026-08-19.md](p1-native-pmu-population-audit-2026-08-19.md)
- P2 native Ruby PMU 合同与严格 gate：[p2-native-ruby-pmu-contract-2026-08-19.md](p2-native-ruby-pmu-contract-2026-08-19.md)
- 参数覆盖：[gem5-parameter-coverage.md](gem5-parameter-coverage.md)
- trace 可观察性：[gem5-trace-contract.md](gem5-trace-contract.md)
- FST/drmemtrace 输入合同：[fst-v7-drmemtrace-conversion-contract.md](fst-v7-drmemtrace-conversion-contract.md)
- 当前 FS CPI 方案：[fs-cpi-current-status-and-plan-2026-08-17.md](fs-cpi-current-status-and-plan-2026-08-17.md)
