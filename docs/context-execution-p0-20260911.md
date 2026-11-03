# P0：分离计分边界与执行边界

规约变更说明（2026-09-11）：本记录描述已实现的“固定逐核计分＋不计分后缀”历史诊断。
新的 [项目规约 §3.0](project-goal-and-semantic-contract.md) 要求所有核心在共同终点前
持续记录并持续累计指标，最快核到 10M 即共同停止，慢核按实际记录计分。本文的旧计分合同和 CPI 数值
不作为新共同窗口方案；仿真端已有能力不等于采集/oracle 全链路已符合新规约。

日期：2026-09-11。对应 [LBM 机制修复设计](lbm-two-stage-mechanism-repair-design-20260911.md) 的 P0。

## 本次接入

每个核心在原计分记录退休时保存 CPI 终点，继续执行已采集的真实后缀。
后缀照常影响 RAW、ROB/IQ/LSQ、store drain、cache/coherence 和 DRAM。
计分结束不会产生 barrier、排空或重置，也不会令核心提前停止竞争。

实现沿用 `interval_weave + time_epoch` 的两阶段路径。没有新增逐指令事件求解器、
时序 oracle 输入或按负载补偿。FST 热记录仍为 64 字节；本次不修改采集器、不重采 FST。

## 输入与输出合同

新增 manifest 行格式：

```text
<core> fastsim-binary-context-v1 <fst> <source_core> <warmup_macro> <score_macro> <warmup_records> <score_records> <execution_records_after_warmup>
```

最后一个字段包含计分记录和后缀记录，不含 warmup；三个排他边界为：

```text
warmup_end    = warmup_records
score_end     = warmup_records + score_records
execution_end = warmup_records + execution_records_after_warmup
```

要求 `0 < score_records <= execution_records_after_warmup`，宏数与记录数按既有
FST 退休规则一致。带 `kRetires` 的 syscall 功能标记继续计入原有指令分母；
不退休的辅助标记不计入。计分最后一条须为完成宏指令的非 syscall 记录；执行 EOF
不能截断宏指令，完整宏指令后的辅助记录可以保留。短文件、负数、溢出及计数不一致
均报错。已有 RAW 扩展、地址空间和其他源元数据继续转发。

旧 manifest 的语义保持 `execution_end == score_end`，旧格式 JSON 保持原结构。
**旧格式没有声明真实后缀，结果不能据此认定覆盖了其他核心的剩余执行时间。**
新格式允许零长度后缀，但也不自动代表覆盖充分。

新结果中：

- `cores`、`threads`、`scope_metrics` 的指令人口及 CPI 保留原计分范围。
- `context_execution.cores` 分别报告计分和执行的记录数、退休周期及计分关闭状态。
- `scope_metrics.pmu` 与 `context_execution.pmu` 分别报告计分记录和后缀记录的正式 PMU。
  事件按触发记录归属；由后缀触发的共享脏写回也归后缀，并保留正确的受影响 CHA。
- Queue/O3、response、recovery 等诊断仍描述完整执行，不能当成可相加的计分 CPI 分量；
  `diagnostic_counter_scope` 明确各类口径。
- `throughput.processed_*` 包含真实后缀，`scored_*` 只含计分工作；速率使用排除
  warmup 的测量墙钟。合成内核 PMU 不充当实际处理 UOP 数。

`all_execution_covers_last_score` 只检查：在共同参考时间中，各活跃核心的执行退休
终点是否覆盖最后一个计分退休终点。它不是所有计分请求返回窗口的覆盖证明，也不能
区分真实程序结束与人为采集 EOF。当前显式边界会执行到所声明的 EOF；并非所有计分
结束就提前取消后缀请求。请求身份与返回窗口的进一步追踪留给后续 P1。

本次支持 `run()` 的 time_epoch sparse per-UOP feedback，包括 generic、materialized
和 activity-certificate 路径。新格式在 `advance()`、event-only/block-transfer、
异步 line coalescing 或合成 IRQ 时序组合下明确报错；这些路径尚未接入相同的计分
提交合同。没有按负载、核心数或整数/浮点类型作限制。

## 关键路径

| 位置 | 改动及原因 |
|---|---|
| `include/fastsim/trace.hpp`、`src/trace.cpp` | 版本化记录边界；计分 marker 是源状态，执行流继续读取 |
| `produce_thread_chunk()` | 在原 chunk 内记录计分序号及功能计数前缀，不切 chunk，不改变时序调度粒度 |
| `account_producer_chunk()` | 通过消费队列安装不可变计分序号；分别累积计分和后缀人口，避免消费者跨线程读源状态 |
| timing feedback / `commit_timing_feedback()` | 提案携带 marker 的实际退休时间，仅接受提交时冻结；丢弃提案不污染 CPI |
| private preview/replay、`SharedSystem` transaction | 后缀 PMU 随原快照回滚；正式计数在最终报告时拆分，执行中保留完整服务状态 |
| `run()` 最终统计、`src/main.cpp` | 输出原计分 CPI、分离的 PMU、执行覆盖及实际处理吞吐 |

## 验证记录

回归包含：marker 位于 chunk 内、双核竞争、generic/materialized 等价、零后缀、
跨边界 store/SQ、activity-certificate 退休、真实 retiring syscall 计数，以及非法输入。
最终 `cmake --build build -- -j16` 成功，`./build/fastsim_tests` 输出
`all FastSim tests passed`。首次新格式回归在实现前失败；真实 retiring syscall 的
兼容性反例也先复现计数失败，修复后通过。

真实负载只运行两次：新格式 C4 后缀一次、旧格式 C4 前缀对照一次，串行运行以免
彼此污染吞吐量。没有启动 gem5 或 40-case 矩阵。

### CPI 与计分人口

原分母固定为 **31,766,542 宏指令**；gem5 CPI 为 **3.660593**。
CPI 单位及绝对误差单位均为 cycles/macroinstruction。

| 同一 LBM C4 计分范围 | FastSim CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|
| 当前 binary，旧格式前缀 | 3.427716 | −6.3617% | 0.232877 |
| 当前 binary，P0 真实后缀 | 3.623955 | −1.0009% | 0.036639 |

计分周期从 108,886,699 增至 115,120,505，新增 6,233,806 cycles，复现此前真实
后缀诊断的全部计分终点。相对 gem5 的净缺口减少 **84.27%**；逐核绝对周期误差
合计减少 **56.94%**，不能把有正负抵消的总体误差视为每个核心均达到 1%。

| 核心 | gem5 CPI | P0 CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|
| 0 | 6.300994 | 6.420844 | +1.9021% | 0.119850 |
| 1 | 2.770238 | 2.675696 | −3.4128% | 0.094542 |
| 2 | 2.636775 | 2.476874 | −6.0643% | 0.159901 |
| 3 | 2.710919 | 2.685637 | −0.9326% | 0.025283 |

### PMU 与旧格式兼容性

后缀接入比较 **140 项全部通过**，包括各核原计分人口、计分退休、完整执行退休，
以及每个正式 PMU 字段的 `score + context == 既有完整执行结果`。46,072,637 条
额外记录没有混入原计分分母。正式 PMU 的 UOP 字段含既有合成内核 footprint，
不同于 `processed_uops` 的真实记录人口，比较没有混用两者。

部分计分范围 PMU 如下。它们允许随恢复的竞争而改变；这张表用于观察机制影响，
不是与 gem5 同口径的 PMU 精度评分。

| PMU | 旧前缀 | P0 计分 | P0 后缀 |
|---|---:|---:|---:|
| L1D misses | 1,158,779 | 1,158,779 | 955,399 |
| L2 misses | 708,479 | 708,484 | 580,730 |
| DRAM reads | 701,786 | 701,786 | 575,519 |
| DRAM writes | 0 | 41,757 | 123,324 |

旧格式对照的配置、CPI、cores/threads、PMU 和目标时序诊断与历史结果一致，仍不
生成 `context_execution` 字段。严格初检只有 `causal_frontier.frontier_waits` 从
1,176 变为 1,228：源码在主机 producer 队列为空时、条件变量等待前递增它，不是
目标处理器的阻塞计数。将其与墙钟/速率一起显式列入主机开销排除表后，模型字段
差异为零；原始严格比较报告一并保留，没有为此重跑模拟。

### 吞吐与覆盖边界

| 实测口径 | 旧前缀 | P0 后缀 |
|---|---:|---:|
| 实际执行记录/UOP | 40,660,551 | 86,733,188 |
| 原计分记录/UOP | 40,660,551 | 40,660,551 |
| 测量墙钟，不含 warmup | 10.557 s | 21.757 s |
| 全运行墙钟 | 10.672 s | 21.871 s |
| 实际处理吞吐 | 3.851 MUOP/s | 3.987 MUOP/s |
| 原计分吞吐 | 3.851 MUOP/s | 1.869 MUOP/s |

P0 的实际宏指令处理吞吐为 **3.091 MIPS**，用户 UOP 处理吞吐为 **3.938 MUOP/s**。
实际工作量是原来的 2.133 倍，全运行墙钟为 2.049 倍。本次单次运行没有观察到
处理吞吐明显下降，但不是成对重复的性能回归实验；不据此宣称提速或零开销。
以后做正式效率比较必须同时报告额外上下文工作量和原计分有效吞吐。

`all_execution_covers_last_score=false`：三个 worker 的执行退休终点分别仍比
最后计分退休早 **346,010 / 2,069,047 / 2,499,902 cycles**。本次通过证明 P0
边界与统计接入正确，尚不能声称后缀覆盖充分或 LBM 全部机制误差已修复。

物理文件消费补充核对：本次声明的执行范围已全部完成；三个 worker 的扩展 FST
记录全部执行完。core0 为保持原指令范围的隔离对照，执行到原计分边界，文件头共
10,291,996 条记录，实际含预热执行 10,291,859 条，末尾 137 条未纳入。
因此“跑完验证范围”不等于本例四份物理 FST 均逐条执行到底，更不等于整个程序运行到退出。

复现命令、JSON、源码前后指纹和差异保存在
[`tmp/context-execution-p0-20260911/`](../tmp/context-execution-p0-20260911/)，主要证据为
`final-build.log`、`final-tests.log`、`context-comparison.json`、
`prefix-control-comparison.json`、`prefix-control-strict-comparison.json`。

最小真实输入验证复用 `tmp/lbm-full-roi-error-20260911/c4-suffix/fixed-static-map/`。
这组语料使用冻结的静态指令 map 隔离后缀影响；该处理仍只是已有实验夹具，不是通用
采集策略。结果不能代表已完成生产采集器的共同终点、RAW 连续性及静态元数据稳定性接入。

本次不涉及 P1–P3 的显式混合 RD/WB 服务、跨 Q 队列、store 实际准入及局部恢复。

后续 [C32 十负载等量回归](context-p0-c32-regression-20260911.md) 已完成：新边界开启、
零后缀时，10/10 case 的 CPI、正式 PMU 和目标时序诊断与修复前一致；单次配对聚合
吞吐变化 −0.641%，最大观测降幅为 Neutron 的 −3.102%。该验证不包含额外真实后缀的工作成本。
