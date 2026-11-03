# Stockfish CPI 尾误差：排查路径、证据方法与修复方案

日期：2026-08-21

状态：StoreSet committed-stream 修复已实现，C4/C8 全量门禁已完成。

> 2026-08-25 状态更新：维护中的 native full-system 默认入口已改为
> `configs/gem5-fs-native-kernel.cfg`，当前选择 v28_4 modeled-I-fetch。
> 下文所称“最终生产配置为 v28_2”记录的是 2026-08-21 当时的结论；
> v28_2 继续保留为显式 no-lower-I-fetch 对照，不再是默认入口。

## 1. 执行摘要

本次问题不是由单一的“固定延迟偏小”造成，而是需要区分三个独立组件：

1. native kernel 指令决定 full-system PMU 覆盖范围，并显著改善
   L2/LLC/DTLB 误差，但只解释 Stockfish C4 原始周期缺口的 4.55%；
2. fetch-response/shadow 修复对大部分负载有效，但 Stockfish 的 shadow
   response 全部隐藏在分支恢复窗口内，因此不是 Stockfish 的关键路径；
3. Stockfish 的主要缺失时序是 gem5 O3 `MemDepUnit` 中的 StoreSet/LFST
   预测性内存依赖。旧 FastSim 利用 FST 最终地址进行近似完美的内存消歧，
   错误地让大量年轻 load/store 提前执行。

根因不是 FST 的四个 `producer_dists` 槽位太短。这四个槽位表达显式
寄存器 producer，而 StoreSet 是另一类由内存依赖预测器产生的动态边。
gem5 直接日志中，主导 StoreSet 边与现有四个寄存器 producer 的重复数为
0。

最终生产配置为 `configs/gem5-v28_2-fs-native-kernel.cfg`。相对 v28_1：

| 指标 | v28_1 | v28_2 |
|---|---:|---:|
| mean CPI APE | 8.086% | 6.936% |
| P95 CPI APE | 17.871% | 11.740% |
| maximum / nearest-rank P99 | 20.459% | 12.354% |
| Stockfish C4 | 20.459% | 8.114% |
| Stockfish C8 | 17.871% | 10.097% |

这里的 20 点 P99 使用 nearest-rank 定义，因此等于该集合的最大值。当前
12.354% 尾部已经转移到 Tealeaf C8；本次修复解决了 Stockfish 原有的主要
20% 尾误差，但没有宣称所有负载已经小于 10%。

## 2. 问题、约束与证据标准

### 2.1 问题

基线为 C4/C8、10 个负载组成的 20 点 native-kernel FST 实验。需要回答：

- 包含内核态指令对 CPI 和 PMU 误差的影响；
- P99 约 20% 的核心来源属于哪个组件；
- 在输入必然不含 wrong-path 指令的约束下如何修复；
- 修复是否只对 Stockfish 有效，是否损害其他负载。

### 2.2 证据标准

本次没有用最终 CPI 相关性直接宣布根因，而是按以下证据强度推进：

| 层级 | 方法 | 用途 |
|---|---|---|
| A | gem5 内部动态事件按 `seq_num` join | 直接确认生产者、消费者和唤醒时刻 |
| B | 单组件 ablation | 证明组件对目标误差是否有因果贡献 |
| C | 对齐的短窗口 replay | 检查边数量和周期量级是否同时匹配 |
| D | 20 点全量门禁 | 检查泛化、回退和 PMU 守恒 |

所有范围比较都使用匹配的 measurement scope：user-only FST 对应 gem5
user scope，native FST 对应 gem5 user+kernel scope。oracle label 和 gem5
debug 日志只用于离线诊断，不会成为 FastSim 运行时输入。

## 3. 排查路径

### 3.1 第一阶段：内核态指令 ablation

20 点 C4/C8 对照如下：

| metric | user-only mean APE | native mean APE | user-only max | native max |
|---|---:|---:|---:|---:|
| CPI | 11.155% | 8.086% | 51.163% | 20.459% |
| retired instructions | 0 | 0.000005% | 0 | 0.000047% |
| retired UOPs | 0 | 0.000003% | 0 | 0.000029% |
| branch misses | 6.206% | 17.293% | 30.262% | 63.823% |
| L1D misses | 5.799% | 5.997% | 33.658% | 33.221% |
| L2 misses | 60.245% | 14.718% | 526.674% | 56.699% |
| LLC misses | 157.273% | 15.437% | 796.591% | 99.545% |
| DTLB misses | 48.659% | 34.822% | 89.762% | 88.815% |

native 输入只在 5/20 点改善 CPI APE，在 15/20 点回退，但少数大幅修正
明显降低了均值和尾部。它必须保留，因为它改善了 full-system memory PMU
合同；branch-miss PMU 变差则是另一个尚未解决的模型问题。

Stockfish C4 的定量分解为：

| scope | gem5 cycles | FastSim cycles |
|---|---:|---:|
| user only | 12,497,452 | 9,978,673 |
| user + kernel | 12,897,339 | 10,258,538 |

native gap 为 2,638,801 cycles，内核输入只解释约 120,022 cycles，即
4.55%。结论是：内核态指令不是 Stockfish 约 20% 尾误差的核心来源。

### 3.2 第二阶段：检查 wrong-path 与 fetch-response

输入没有 wrong-path 指令，因此先使用 gem5 oracle 给出其贡献上界，而不是
直接假定所有缺失周期都来自 wrong-path。精确窗口中，Stockfish core 3
缺少 12,653 cycles，而 wrong-path active-union ceiling 只有 291 cycles。
wrong-path 无法解释该缺口。

随后实现的 fetch-response ledger 对更广泛的负载有效：20 点中 15 点改善、
5 点回退，mean CPI APE 从 8.086% 降到 7.798%。但它对 Stockfish 几乎无效：

| case | baseline APE | fetch APE | 改善 |
|---|---:|---:|---:|
| Stockfish C4 | 20.459% | 20.377% | 0.082 pp |
| Stockfish C8 | 17.871% | 17.852% | 0.020 pp |
| Graph500 C8 | 12.720% | 8.689% | 4.031 pp |

ledger 给出了直接原因：

| case | committed requests | shadow requests | server wait | admission delay |
|---|---:|---:|---:|---:|
| Stockfish C4 | 1,879,932 | 100,259 | 78,682 | 149,714 |
| Graph500 C8 | 4,955,032 | 1,687,732 | 1,473,664 | 5,419,043 |

Stockfish 的一周期匿名 shadow response 全部在两周期 branch recovery 内完成，
`speculative_fetch_shadow_recovery_exposed_cycles` 为 0。fetch 组件确实工作，
但它不在 Stockfish 的 committed critical path 上。

### 3.3 第三阶段：对齐短窗口并拆分 commit gap

建立每核目标 100,000 committed UOP 的对齐 replay。因为停止边界允许单条
记录 overshoot，实际总量为 400,003 UOP。

| model | per-core cycles | aggregate cycles |
|---|---|---:|
| gem5 | 59,992 / 57,515 / 63,914 / 42,800 | 224,221 |
| FastSim v28_1 | 38,688 / 43,864 / 51,711 / 30,147 | 164,410 |

总缺口为 59,811 cycles，其中 zero-commit deficit 为 26,884 cycles，约占
44.95%；productive-cycle density deficit 为 32,927 cycles，约占 55.05%。
这说明问题同时影响 readiness 和提交密度，不像单个固定 cache latency。

### 3.4 第四阶段：排除固定 load latency 与真实地址冲突

两个候选通过定向实验被排除：

- 将 minimum load latency 从 4 增加到 8，只增加 1,503 aggregate cycles，
  远小于 59,811-cycle 缺口；
- 精确窗口中，只有 31 个 load 在 ROB 内存在更早且地址重叠的 store，20 个
  在 store 尚未完成时发射，最终只产生 2 个 ROB-head stall cycles。

因此根因不是统一 load latency，也不是真实地址 store forwarding/hazard。

### 3.5 第五阶段：直接审计 gem5 MemDepUnit/StoreSet

重新运行 gem5，开启 `MemDepUnit,StoreSet` debug。离线工具
`tools/audit_gem5_store_set_debug.py` 执行以下 join：

1. 解析 `Waking up a dependent inst [sn:consumer]`；
2. 关联紧随其后的 completed memory producer；
3. 通过动态 `seq_num` 将 producer/consumer join 到 FST record；
4. 再 join stage label，检查 issue、complete 和 ready-list 时刻；
5. 比较地址范围、PC 和四个寄存器 `producer_dists`。

结果如下：

| core | gem5 same-PC edges | 重构 edges | gem5 critical wakeups | 重构 readiness extensions |
|---:|---:|---:|---:|---:|
| 0 | 8,237 | 8,184 | 4,150 | 4,091 |
| 1 | 8,262 | 8,182 | 4,030 | 4,090 |
| 2 | 8,166 | 8,184 | 4,028 | 4,089 |
| 3 | 13,474 | 13,481 | 6,660 | 6,739 |

直接事实为：

- 所有主导 same-PC 边的 producer/consumer 地址均不重叠；
- 与 FST 四个寄存器 producer 距离重复的边数为 0；
- 超过 99.98% 的 wakeup tick 等于 producer store 的地址生成完成时刻；
- 同一 SSID/LFST producer 后既有 load consumer，也有 store consumer；
- 重构 edge 和关键唤醒数量与 gem5 相差约 1% 或更小。

这些事实排除了真实地址冲突和寄存器依赖，直接指向 PC-indexed StoreSet
false dependency。

## 4. StoreSet 在仿真什么

乱序 CPU 遇到一个年轻 load 时，较老 store 的地址可能尚未计算完成。处理器
可以保守地让 load 等待所有 store，也可以激进地让它直接执行；前者损失
并行度，后者可能产生 memory-order violation 和 squash/replay。

StoreSet 是两者之间的动态内存依赖预测器：

```text
SSIT: instruction PC -> StoreSet ID
LFST: StoreSet ID -> youngest in-flight store
```

预测无依赖的 load 可以越过旧 store；预测相关的 load/store 只等待 LFST
指出的特定 store。这个等待是预测性时序依赖，不是架构寄存器依赖，也不
要求最终地址真的重叠。

旧 FastSim 没有让所有 load 等待 store，行为恰好相反。FST 已提供最终物理
地址，因此旧模型近似使用“完美内存消歧”：只要已知地址不重叠，就允许年轻
load/store 提前执行。这样会漏掉真实 O3 CPU 为避免未来 violation 而施加的
预测性 false dependency，导致 issue 过早、ROB 更快排空、zero-commit 周期
不足和 CPI 偏低。

StoreSet 唤醒也不等同于等待 store commit、cache writeback 或 Ruby response。
gem5 直接日志表明，本次主导依赖在 producer store 地址生成/执行完成时唤醒。

## 5. 为什么不是 `producer_dists[4]` 太短

FST record 的四个 `producer_dists` 表达最多四个显式寄存器 source 的生产者
距离：

```text
consumer
  +- register source 0 -> producer distance
  +- register source 1 -> producer distance
  +- register source 2 -> producer distance
  +- register source 3 -> producer distance
```

StoreSet edge 是第五种语义，不是“第五个寄存器 source”。如果问题只是数组
长度不足，应观察到 StoreSet producer 被第五个寄存器 operand 截断，或者
StoreSet distance 与现有 producer 重合；实际直接审计的重复数为 0。

本次没有修改 FST 文件格式。FastSim 内部将 interval descriptor 从四个寄存器
依赖扩展为四个寄存器槽加一个专用 StoreSet 槽；序列化的 FST
`producer_dists` 仍保持四个。

超过四个显式寄存器 source 是否会影响其他稀有指令，可以作为独立审计项，
但现有证据排除它是 Stockfish 20% 尾误差的核心原因。

## 6. 修复设计

### 6.1 模型边界

当前修复是 evidence-derived StoreSet 行为重构，不是按最终 CPI 补固定周期，
也不是 gem5 完整 StoreSet 数据结构的逐项复刻。

FastSim 运行时不会读取 gem5 CPI、stage label 或 debug 日志。它只使用 FST：

1. 从 committed 同一 macro 中地址重叠的 load/store 分解识别 x86 RMW PC；
2. 训练该 PC，并跟踪最近仍在 ROB 内的 same-PC store producer；
3. 为后续同 PC load/store 生成 LFST 类似的动态依赖距离；
4. 在 producer store 地址生成完成时释放 consumer。

这种方案不需要 FST 包含 wrong-path 指令，而且重构边数量由独立 gem5 内部
事件验证，不依赖 CPI 拟合。

当前尚未完整仿真的 StoreSet 结构包括：

- 有限容量 SSIT 的 index/tag 和 aliasing；
- SSID 分配、合并与周期性清空；
- 实体 LFST 表冲突；
- violation detection 驱动的 predictor 更新；
- squash/replay 和 wrong-path 对 predictor 状态的影响。

因此当前方案是针对已证明主导模式的最小结构化修复，不能替代未来完整的
committed-stream StoreSet predictor。

### 6.2 代码修改

`include/fastsim/config.hpp` 和 `src/config.cpp`：

- 增加 `core.store_set_same_pc_feedback`；
- 对 scalar core 等不支持组合执行配置校验。

`include/fastsim/interval_core.hpp` 和 `src/interval_core.cpp`：

- 增加 RMW macro 观察和 PC training；
- 跟踪 same-PC live store producer；
- 为 load 和 store consumer 生成 `store_set_dependency_distance`；
- 排除已有寄存器依赖，输出候选边和 readiness-extension audit。

`src/simulator.cpp`：

- interval 内部依赖槽从四个寄存器边扩展为额外一个 StoreSet 边；
- 增加独立的 `store_set_completion_cycle`；
- 传播 store address-generation completion，而不是 Ruby response completion；
- 在 shared-memory weave 后通过 response scoreboard 修正 readiness。

post-weave 很重要：早期版本直接改变 interval lower bound，虽然能补足
Stockfish 周期，但会改变跨核共享内存请求顺序并放大 Graph500 回退。最终
版本先保持既有 memory replay order，再在 response scoreboard 上施加局部
StoreSet 依赖。

`include/fastsim/types.hpp` 和 `src/main.cpp`：

- 增加 StoreSet candidate、edge、non-overlap 和 extension cycle 计数；
- 将配置与审计结果写入 stats JSON。

`tests/test_main.cpp`：

- 增加配置校验；
- 增加 RMW training、load/store edge、非地址别名 false dependency 等定向
  测试。

`tools/audit_gem5_store_set_debug.py`：

- 提供 gem5 MemDep debug 与 FST/label 的独立动态序号审计。

生产配置 `configs/gem5-v28_2-fs-native-kernel.cfg` 同时启用独立验证过的
fetch-response ledger 与 StoreSet 修复，不注入架构 wrong-path 指令，也不把
wrong-path 事件计入 functional PMU。

## 7. 修复结果

### 7.1 精确短窗口

| model | aggregate cycles | error vs gem5 |
|---|---:|---:|
| gem5 | 224,221 | -- |
| v28_1 baseline | 164,410 | -26.68% |
| StoreSet response-scoreboard repair | 222,775 | -0.65% |

重构 edge 和 critical wakeup 数量约 1% 的一致性，构成独立于 CPI 的组件
验证。aggregate 周期接近不代表每核完全一致；短窗口仍存在 per-core 正负
偏差，因此最终结论还依赖后续 10M 全量门禁，而不是只看 aggregate 抵消。

### 7.2 20 点全量门禁

| statistic | v28_1 baseline | v28_2 combined |
|---|---:|---:|
| mean CPI APE | 8.086% | 6.936% |
| median CPI APE | 8.997% | 8.493% |
| P95 CPI APE | 17.871% | 11.740% |
| maximum / nearest-rank P99 | 20.459% | 12.354% |
| improved / regressed | -- | 14 / 6 |
| worst per-case regression | -- | 0.347 pp |

关键点：

- Stockfish C4：20.459% -> 8.114%；
- Stockfish C8：17.871% -> 10.097%；
- 新尾部为 Tealeaf C8 12.354%，其次为 Graph500 C8 11.740%；
- 最坏回退为 Neutron C8 的 0.347 pp。

### 7.3 对其他负载的影响

需要区分 fetch 与 StoreSet 两个组件：

- fetch-response 是广泛收益组件：非 Stockfish mean APE 从 6.854% 降到
  6.541%；
- 最终组合在非 Stockfish 上为 6.695%，相对 baseline 仍改善 0.160 pp，
  18 个非 Stockfish 点中 12 个改善、6 个回退；
- StoreSet 本身高度 Stockfish-selective，不应宣传为全负载优化；
- Graph500 C8 的 fetch-only 为 8.689%，组合后为 11.740%，仍优于
  12.720% baseline，但说明两个时序组件存在共享内存顺序交互。

### 7.4 PMU 稳定性

最终时序修复基本不改变 functional PMU population：

| PMU mean APE | baseline | final |
|---|---:|---:|
| L1D misses | 5.9971% | 5.9980% |
| L2 misses | 14.7180% | 14.7403% |
| LLC misses | 15.4369% | 15.4124% |

retired instruction/UOP 和 branch 数量保持原有守恒。branch-miss 与 DTLB PMU
误差没有被此次 StoreSet 时序修复解决。

## 8. 结论与后续边界

证据支持的根因链为：

```text
FST只含committed stream
        +
旧FastSim利用最终地址进行近似完美消歧
        |
        v
漏掉gem5 StoreSet/LFST false dependency
        |
        v
Stockfish同PC RMW load/store过早发射
        |
        v
zero-commit周期与productive-cycle密度同时被低估
        |
        v
CPI低估约20%
```

本次已经证明并修复主导 StoreSet 时序，不需要向输入添加 wrong-path。当前
实现是可审计的 committed-stream 行为重构，而不是完整 SSIT/LFST predictor。
如果后续目标是跨微架构、跨负载泛化，应继续实现有限表容量、set merge、
violation training 和 predictor clear，并使用非 Stockfish 负载做独立门禁。

当前剩余工作应聚焦新的尾部 Tealeaf C8、Graph500 C8，以及独立的
branch-miss/DTLB PMU 问题，而不是继续把 Stockfish 缺口归因于 kernel、
wrong-path 数量或统一 load latency。

## 9. 证据与复现入口

主要实验产物：

- kernel 输入 ablation：`tmp/kernel-input-ablation-current-20260821/`；
- fetch-response 全量门禁：
  `tmp/fetch-response-ledger-full20-20260821/candidate/summary.json`；
- Stockfish 对齐窗口：`tmp/stockfish-timing-oracle-20260821/`；
- gem5 StoreSet debug：
  `tmp/stockfish-store-set-oracle-20260821/result/memdep.log`；
- 最终 20 点门禁：
  `tmp/stockfish-root-cause-20260821/`
  `store-set-plus-fetch-ledger-scoreboard-v3-full20/summary.json`。

StoreSet debug 审计示例：

```bash
python3 tools/audit_gem5_store_set_debug.py \
  --debug tmp/stockfish-store-set-oracle-20260821/result/memdep.log \
  --records tmp/stockfish-store-set-oracle-20260821/trace/board.processor.switch0.core.tao_trace.tao_trace.records.micro.jsonl \
  --labels tmp/stockfish-store-set-oracle-20260821/trace/board.processor.switch0.core.tao_trace.tao_trace.labels.micro.jsonl \
  --skip 1641844 \
  --take 100001 \
  --core 0
```

默认构建与测试：

```bash
cmake --build build -- -j16
./build/fastsim_tests
```
