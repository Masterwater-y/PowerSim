# 同线读服务引用：第一项受限实现与完整 ROI 验证

日期：2026-09-10。对应 [两阶段修复设计](two-stage-service-repair-design-20260910.md)
的第一项。本次已实现并验证批内稳定组件的服务引用；**没有 CPI 收益，不进入默认
配置**。它不是整个 private cache / shared service / corrected-arrival 闭合的完成。

## 1. 实际改动

在 `interval_weave + time_epoch` 的原有反馈中，同一物理 line 的在途读请求引用
本批父事务的实际响应。父事务返回前，tag 已分配不能再作为数据已可见的理由。
响应传入现有 load data-ready、WB、RAW 消费者、IQ/退休与队列处理，未在最终 CPI
或周期总数上加补偿。

- 新增 [PrivateReadServices](../include/fastsim/private_read_services.hpp)：按共同
  L1D/L2 set 索引保存有界组件状态。本批首个 miss 是服务 owner，后续读引用该服务；
  没有逐请求过期堆、历史 line 哈希表或全 ROI 指令图。
- 在 [Simulator](../src/simulator.cpp) 中接入组件检查、服务发布与响应引用，generic
  和 materialized 共用此路径；并行 producer、并行 core feedback 和 Q=1024 保持。
- 保留逐 CPU request 的 Sequencer 容量。follower 不另分配 miss MSHR，但仍占一个
  CPU request 位置；通过校验的请求在 callback 释放该位置，CPU response 是另一个边。
- callback 同时刻不再附着旧事务。引用返回具体父响应，不是
  `max(旧响应,父响应)`；接口也支持原服务等待变短。
- 当前普通请求的起点合同保持原样，没有顺便开启 source-load-admission、改 raw
  issue/envelope、修改 DRAM 控制器或引入 causal_read。

入口是 `core.response_private_read_services`，默认 `false`。
[实验配置](../configs/gem5-exp-private-read-services.cfg) 显式开启它。配置校验拒绝
尚未接通的 source-admission、hierarchy walk、post-commit store、event-only、
issue-resource 等实验组合，避免把不同响应合同混用。

## 2. 哪些组件可进入

当前证明只针对现有服务模型中的局部复用，不能称为整个硬件时序的等价证书：

1. 当前接受批次中，同核的共同 L1D/L2 replacement component 只访问一条 line。
   首访是 miss，后续均为 L1 hit，全部是普通单片段读。
2. 没有相关 write、atomic、PTE、I-side、跨核同线或 functional-carried 效果；
   inclusive LLC 不进入该路径。整个批次存在 atomic 时不做此项修复。
3. 父请求的实际到达不晚于所有 follower 的原始请求下界。这在发布之前校验，
   不能在发现后来请求更早时继续将它挂到未来的父事务上。
4. 父事务开始前，前批未追踪的事务已经越过保守完成上界，包含可能晚于功能响应的
   store SQ release；其 callback 也必须不晚于
   未检查后续指令的最早 fetch 下界。第一版因此不携带未完成 owner 跨批复用。

第三、四项失败时整个组件保留旧路径，并单列拒绝计数；这些上界**只决定是否采用
新机制，不会延迟请求**。跨批完成上界在原反馈的访存处理处累计，包含 I-side，
随接受的 timing state 提交；被放弃的 proposal 不更新持久状态。

这里只有同一 line 的重复触碰，且检查排除了可能改变它的替换/失效干扰，因此没有
重新执行功能缓存，也没有添加 cache snapshot/replay。对一般 set 冲突、权限变化和
跨批事务，仍需要设计中后续的事务提交与增量恢复。

现有 cache PMU 仍是功能预览计数，本轮没有将其改定义为 Ruby 实际 tag-probe 计数。
新增 follower 统计描述服务分类；PMU 不变不代表每一层协议路径已经与 gem5 等价。

## 3. 必要机制验证

先在旧实现添加端到端反例：一个 cold line 后跟两条独立同线 load。旧版本明确失败：
`same-line reads observed data before the unique fill response`。接入后通过。

[响应测试](../tests/test_response_completion.cpp) 还覆盖：

- callback 边界、直接父响应引用及响应缩短；
- request capacity=2 时第三个同线请求不能占用一个不存在的空槽；
- RAW 延迟导致候选 parent 晚于独立 child 时，整个组件回退且周期不变；
- incoming / outgoing 批次边界、同 set 异线、write、重置后的旧服务失效；
- load/WB/RAW 不变量，以及 generic/materialized 一致。

最终 `cmake --build build -- -j16` 和 `./build/fastsim_tests` 均通过。
构建日志只有既有的 serial LTRANS 提示，没有编译错误。

## 4. 两个完整 ROI 的 CPI、PMU 与吞吐

固定配置、FST、manifest，均使用原 materialized 内核。初版每个 case 在 NUMA node 0
串行 ABBA：旧二进制两次、开启候选两次；共 8 次精度兼性能运行。收尾审查补上了
incoming SQ release 上界，最终版再各跑 1 次完整 ROI，确认两项 CPI、PMU 仍不变。
没有新 gem5 采集，没有全矩阵。

| Case | 旧 CPI → 最终候选 CPI | 旧误差 → 最终候选误差 | 现有 scope PMU |
|---|---:|---:|---|
| TeaLeaf L1D64 C4 | 0.50460785 → 0.50460785 | −17.8004% → −17.8004% | 全部相同 |
| TeaLeaf LLC32 C4 | 0.8186428681 → 0.8186428681 | +10.2192% → +10.2192% | 全部相同 |

参考 CPI 分别为 0.6138814、0.7427406257259375。L1D64 的当前旧模型精确复现
20,184,314 cycles，LLC32 为 32,745,718 cycles。两个 case 的全部现有 scope PMU
字段与基线相同；用户/内核输入人口也相同。

保留初版 ABBA 的性能记录如下，**不冒充补齐 SQ 边界后最终版的吞吐结论**：

| Case | 旧 → 初版候选吞吐均值（M user-UOP/s） | 均值变化 |
|---|---:|---:|
| TeaLeaf L1D64 C4 | 6.0255 → 5.9785 | −0.78% |
| TeaLeaf LLC32 C4 | 5.7983 → 5.7312 | −1.16% |

初版每版本两次的目标 cycles 和 PMU 精确一致；性能均值不外推为其他负载的稳定
百分比。最终版定点运行与隔离诊断编译并行，只用于精度；因 CPI 仍无收益，没有
重复 ABBA 来追求一个更好看的吞吐数字，也不将插桩运行用于性能结论。

### 实际激活情况

| 计数 | L1D64 C4 | LLC32 C4 |
|---|---:|---:|
| 检查的 batch memory events | 5,649,768 | 6,277,526 |
| 通过静态组件检查的候选 load | 105,314 | 3,740 |
| 发布的 owner | 7,870 | 5 |
| 使用父响应的 follower | **20,972** | **0** |
| 父 callback 后的完成态命中 | 33,936 | 17 |
| 请求先后关系不能证明 | 20,488 | 39 |
| incoming 边界不能证明 | 20,967 | 3,679 |
| outgoing 边界不能证明 | 1,081 | 0 |
| 累计响应延后 cycles | 210,636 | 0 |

候选计数等于 owner、follower、完成态命中及三类动态拒绝之和。其余静态不适用事件
单列为 guarded events，包括本来就没有新 owner 的稳定命中；不能把全部 guarded
events 称为错误访问或潜在修复收益。

L1D64 的 feedback 仍为 6,070 次、13,535 个 core task、40,169,794 个 materialized
UOP；LLC32 对应 8,296 / 12,109 / 40,299,545。新旧精确相同，没有增加完整反馈
扫描或 UOP 重放轮数。额外成本来自内存组件检查、服务引用及它们触发的原有反馈工作。

## 5. 为什么实际生效却没有 CPI 改善

由于总周期不变但内部计数变化，初版与补齐 SQ 上界的最终版各做 **1 次** L1D64
全 ROI 阶段导出。以下以最终版为准。使用隔离
诊断源码副本，复用前次六字段导出与完整基线，没有修改生产二进制。该 generic
诊断的 CPI、PMU、输入人口与服务计数均与当前 materialized 候选精确一致。

逐条匹配四核 **40,169,786 条硬件记录**的 sequence、PC 和 FST 身份；8 条无 O3
stage 的 syscall 辅助事实单列过滤。结果如下：

| 核心 | issue 改变的指令 | completion 改变的指令 | retire 改变的指令 |
|---|---:|---:|---:|
| 0 | 0 | 0 | 0 |
| 1 | 25 | 25 | 0 |
| 2 | 9 | 12 | 0 |
| 3 | 62,925 | 91,349 | 7 |
| 合计 | **62,959** | **91,386** | **7** |

其中 21,193 条 load 的 completion 改变，fetch 全部不变。这排除了“只改统计字段，
没有接到核心状态”的解释。

7 条退休变化均在 core3，只有 1 cycle，并在随后指令处追平。例如 sequence
8,618,094–8,618,095 延后 1 cycle；下一条 load 8,618,096 的
issue/completion/retire 新旧都为 6,163,365 / 6,163,369 / 6,163,369。
这里 sequence/PC 只用于定位证据。

初版曾出现另一个 168 条指令、最多 35-cycle 的退休位移片段。补上 incoming SQ
release 上界后，这个未充分证明安全的组件被拒绝，位移消失；没有将它算作修复收益。

四核首尾退休时刻均不变。复用全流互斥阶段分解后，仅 core3 的 issued-load-head
等待少 1 cycle、有退休周期多 1 cycle，其余阶段完全相同。因此原有约 437 万 active
周期缺口没有缩小；不能把 210,636 个重叠的逐请求延后 cycles 累加为 CPI 收益。

更直接的限制是覆盖：大部分完成变化在 core3，原关键见证所在的 core1 只有 25 条。
这次可以证明安全的局部子集，没有覆盖决定当前误差的主要请求/退休路径。parent
自己的共享服务时间仍来自原模型，这一项并未修复其 DRAM 到达和排队关系。

## 6. 决定与下一步

**保留代码及反例，默认关闭；不宣称第一项完整机制或 CPI 精度已经验收。** 相比旧
pending-fill，本次消除了该子集内未来 parent 误关联、callback 边界及 request/line
容量混淆，并避免全量历史 map/expiry/快照成本；但收益覆盖仍不够。

下一步应直接推进设计第二项：让 corrected arrival 与共享服务约束重新求值，并在
实际受影响的核心部分传播；针对当前主导路径处理 owner 自己的服务和跨批身份。
不放松组件检查追求更多 follower，不把本次局部等待翻倍，不重跑旧 pending-fill
组合或全矩阵。DRAM 控制器替换仍需以到达/服务合同接通为前提。

后续进入更广范围前，仍需补齐一般 cache mutation/permission 事务、跨批 owner
生命周期及真正的核心局部恢复。这些能力没有被本次静态组件检查替代。

## 7. 复现与证据

目录：`tmp/private-read-services-20260910/`。

- `before-sha256.json`、`after-sha256.json`：源码、测试和生产二进制版本。
- `inventory.json`、`inputs/`：两项固定配置、manifest 和参考 CPI。
- `red-tests.log`、`sq-boundary-build.log`、`sq-boundary-tests.log`：先失败再通过及最终整套检查。
- `validate.py`、`validation.log`、`runs/`：ABBA 命令、8 次完整 stats 和分项汇总。
- `sq-boundary-validation.log`、`runs/*/sq-boundary-final/`：最终版两项完整 ROI。
- `final-stage-stats.json`、`final-stage-comparison.json`：最终隔离导出与全流阶段变化。
- `final-stage-absorption.json`：最终互斥阶段差和局部退休变化片段的追平位置。

本轮共 12 次 FastSim 负载运行（8 次初版 ABBA、2 次最终精度确认、2 次全量阶段诊断），0 次新 gem5，
未修改 FST、生产默认配置、Q 或工作负载参数。只开启的候选开关由单一配置控制，
没有负载/PC 特例或 CPI 补偿系数。
