# Graph500 C8：load 返回约束为何造成 CPI 从负偏差变为正偏差

日期：2026-09-09。结论：**主要回归来自反馈请求起点混用，导致已被等待吸收的
程序序 clamp 再次计入请求时刻；新增 load data-ready 下界把这项误差传播到写回、
RAW 和退休。** WB 端口容量不是此次大幅回归的主因。

本轮完成原因分析与隔离实验，生产源码和二进制未改变。诊断副本中的绝对时刻修正
减少了 27,520,407 core cycles，占此前增加 31,775,960 cycles 的 86.61%；这说明
该计算错误影响显著，不等于完成了通用修复或解释了剩余全部 gem5 误差。

## 1. 口径与修复隔离

沿用 [旧 P99 尾部验证](two-stage-tail-validation-20260909.md) 的 Graph500 C8：
`interval_weave/time_epoch`、Q=1024、同一旧 FST、同一 functional warmup 和配置。
用户 UOP 80,000,010，native kernel UOP 10,859,931，宏指令分母 48,782,979。
冻结 gem5 CPI 为 1.8777655419526553。所有对照的人口、分母均相同。

| 版本／诊断消融 | 合计 core cycles | 宏 CPI | 相对 gem5 有符号误差 |
|---|---:|---:|---:|
| 三项修复前 | 79,019,770 | 1.619823 | −13.7367% |
| 仅完整依赖修复后 | 79,019,770 | 1.619823 | −13.7367% |
| load/WB 修复后、最后服务校验前 | 110,795,730 | 2.271196 | +20.9521% |
| 当前生产模型 | 110,795,730 | 2.271196 | +20.9521% |
| 当前模型仅关闭新增 load 下界 | 79,838,960 | 1.636615 | −12.8424% |
| 当前模型仅关闭新增 WB 容量 | 110,820,502 | 2.271704 | +20.9791% |
| 保留 load/WB，仅统一普通数据请求的反馈起点 | 83,275,323 | 1.707057 | −9.0910% |

依赖快照的 totals/cores/CHA 与旧版完全一致；服务校验前后的同类输出也完全一致。
Graph500 C8 的服务校验次数为 0。关闭 load 下界只是定位手段，会重新允许提前
写回，不能作为修复。WB 开关造成的小幅非单调差异包含批次、访存交错和队列反馈
变化，不能把两个消融的周期差相加成独立硬件开销。

附加诊断把 `memory_producer_issue_q16` 全部改为 raw issue，仍保持 canonical
功能遍历顺序，得到 98,491,572 cycles、+7.5200%。这个改法同时移动服务计算起点，
没有统一所有时间语义，也不是可采用的修复；“把一个 helper 改成 raw issue”并不充分。

## 2. 确定的计算错误：两个起点之间重复加延迟

`src/simulator.cpp` 中三个位置形成问题：

1. producer 在 `last_bound_memory_issue_q16` 处对访存事件做程序序 clamp，保留
   `producer_issue_q16`，但将事件的 `delta_q16` 推迟。普通请求的
   `memory_producer_issue_q16` 默认返回后者。
2. feedback 根据 **UOP 原始 issue** 计算 `dependency_extra`。它除了 RAW/StoreSet，
   还包含 dispatch/ROB/IQ/serialize 等使 issue 推迟的下界，名称不能按字面只理解为 RAW。
3. memory loop 把这个 extra 加到 **已经 clamp 的事件起点** 上，形成
   `corrected_event_issue_cycle`，再加 `feedback.latency_cycles` 得到 response。
   新修复要求 load completion 不早于该 response。

将 interval gap 统一加入各时刻后，令：

- `I`：UOP 基础 issue；`M=I+c`：clamp 后的请求起点；
- `R=I+d`：当前反馈要求的 issue 就绪时刻；`L`：本次服务延迟。

当前在进一步竞争 sequencer/MSHR 之前，相当于计算 `request=M+d`。
如果暂时仍保留 `M` 作为下界，两个独立就绪条件应该合并为
`request=max(M,R)`，或者 `event_extra=max(0,R-M)`。

在 `c,d≥0` 时，两式之差为 `min(c,d)`。已经被其他等待覆盖的 clamp，又被加了一次。
这不依赖某个 workload、PC、gem5 timing label 或拟合系数，是时间起点不一致造成的
通用计算错误。诊断只在普通数据 memory loop 内做上述归一化，然后继续执行原有
sequencer/MSHR、load data-ready 和 WB 约束；未修改配置延迟、带宽或曝光系数。

更深一层，`M` 原本服务于缺少完整访存消歧信息时的功能回放排序。它也不自动等价于
真实硬件发射下界。原 producer 注释明确说明了该近似。此次归一化仍保留 `M`，
所以没有解决功能顺序与硬件 timing 顺序的全部差异。

## 3. 真实输入上的指令见证

core 0，sequence 8,940,543，PC `0x4093bb`，普通用户态 load，模型路径为 L1 hit，
latency=2、exposed=0。数据来自保留完整 warmup/ROI 的定点审计：

| 时刻 | cycle |
|---|---:|
| UOP 基础 issue `I` | 6,848,953 |
| clamp 后请求起点 `M` | 6,849,085 |
| 反馈要求的实际 issue `R`，即 `I+539` | 6,849,492 |
| 当前请求，`M+539` | 6,849,624 |
| 当前 response／最终 completion | 6,849,626 |
| 合并下界后的请求，`max(M,R)` | 6,849,492 |
| 固定该 L1 服务时的 response | 6,849,494 |
| 同时保留原 4-cycle 执行下界后的完成下界 | 6,849,496 |

这里 539-cycle 就绪等待已经超过 clamp 的 132 cycles，当前却在其后再次收取132。
考虑 4-cycle 基础执行下界后，当前 completion 比上述局部合法下界还晚130 cycles。
6,849,494/496 是固定本条上游状态和服务的局部推导，**不是 gem5 实测时刻，也不
声称最终修复后全程重算仍得到相同绝对值**。

窗口内 sequence 8,940,526 的基础 memory issue 为 3,810,466（未加 interval gap）。
后面的 8,940,541/542/543 的基础 UOP issue 为 3,810,335/335/334，却都被功能
访存顺序推到 3,810,466；由此直接看到 clamp 来自更早访存的较晚基础发射。
它们随后又带上 539-cycle feedback。该现象不是请求真的访问了更慢的 cache 层。

审计保留了 PC 身份。当前本地同名 Graph500 ELF 的 SHA 与采集 request.json 中的
SHA 不同，因此没有用它的反汇编给这些 PC 安排函数名或汇编语义。

## 4. 为何 Graph500 放大如此明显

| 测量段诊断 | Graph500 C8 | TeaLeaf L1D64 C4（此前 DSE 尾部） |
|---|---:|---:|
| producer memory-order clamp 次数 | 4,641,918 | 1,376,207 |
| clamp 位移合计 | 367,796,391 | 6,278,201 |
| 当前 load 下界修正 UOP | 4,192,017 | 720,143 |
| load 下界修正周期合计 | 354,132,692 | 4,143,106 |
| 每次 load 下界修正平均 cycles | 84.48 | 5.75 |

Graph500 的 clamp 次数与位移在旧版和当前完全相同。新增 load 下界使这些已存在的
大位移进入 producer-ready，之后经 RAW、IQ 驻留、有序 commit 和 ROB 反压传播。
它不只是增加个别 miss 的服务时间，L1 hit 同样会被放大。

归一化诊断将 load 下界修正合计从 354,132,692 降至 120,222,363 cycles；实际
sum-core cycles 减少27,520,407。两者并非线性关系，因为大部分等待相互重叠。
FastSim 的互斥关键路径分类中，这27,520,407的差额表现为 dependency −18,367,229、
direct memory response −9,349,332、SQ capacity +196,051、IFetch +103 cycles。
这是模型内部差分，不能解释为已测得的 gem5 误差分解。

旧版→当前的 ROB stall 累计为18,818,930,035→29,517,543,920，IQ stall 累计为
11,526,842→45,897,504；归一化后分别为21,212,429,617和15,491,712。
这些是按受影响 UOP 累加的等待，彼此重叠，不是 wall cycles，也不是可与 gem5
同名 PMU 不加合同对齐就比较的数字。

## 5. 缓存 miss 数没有解释这次跳变

| PMU | 旧版 | 当前 |
|---|---:|---:|
| branch misses | 213,027 | 213,027 |
| L1D misses | 3,974,177 | 3,974,122 |
| private L2 misses | 1,994,075 | 1,993,829 |
| LLC misses／DRAM reads | 283,862 | 283,861 |
| DTLB misses | 3,590,635 | 3,590,635 |

周期增加40.21%，但 miss 基本不动。不能据此推断全部服务延迟都正确，不过上述
消融和逐指令算术已能把大部分新增周期定位到请求时间投影。它也解释了为什么旧版
仍为负误差：旧的较早 completion 掩盖了被推迟的请求 response；将 completion
强制对齐一个偏晚的 response，并不自动提升相对 gem5 的正确性。

## 6. 下一步修复边界

优先在原两阶段反馈中统一 absolute issue-ready/request/response 的语义，对 RAW、
dispatch、MSHR/sequencer 使用明确的同一时间起点再取最大值；load fragments 的
返回下界、单次写回和跨 Q 的 WB 占用继续保留。功能排序时刻与硬件请求时刻应显式
区分，服务 descriptor 也须清楚标注 latency 的起点，避免只改一个 helper 后把
排序等待转移到 service latency。可在既有遍历中做这些常数次运算，不需要逐周期
重放、增加全量反馈轮次或切到 causal_read。

当前 −9.09% 是单 case 的诊断结果，仍有负误差；未完成其他负载、配置、跨 Q、
多 fragment、store/atomic 和快速路径的修复验收，也未测吞吐。后续采用机制断言
和少量必要控制负载验收，不能按 Graph500 剩余残差补偿，也不能宣称新 P99 已改善。

## 7. 证据与运行范围

产物：[tmp/graph500-regression-analysis-20260909](../tmp/graph500-regression-analysis-20260909/)。
`summary.json/csv` 保存逐版本指标、哈希、原始指令见证及局部算术；`summarize.py`
验证人口、Q、快照等价和审计不改变目标结果。`diagnostic/` 是复制的源码及一次
无 IPO 构建，环境开关只存在于该临时副本，没有加入生产 API。

本轮只运行 Graph500 C8，共9次：依赖快照1次；当前模型早期、抽样、用户窗口审计
3次；诊断副本 control、no-floor、no-WB、normalized、raw-origin 共5次。无改动
诊断 control 和另外3个审计的 per-core cycles、scope PMU、CHA 均与生产输出一致。
沿用先前已完成的构建／整套测试，没有重复生产测试、全矩阵或 FST/gem5 采集。
两个诊断进程可分别固定 NUMA 0/1 并行，只用于准确率归因，不发布吞吐数字。

沿用冻结 gem5 CPI 参考；采集 request.json 的 `native_response_jsonl=false`，本轮
没有匹配的逐请求 gem5 动态时刻。因此结论区分源码计算错误、FastSim 局部见证、
真实负载消融和 gem5 总 CPI 对照，没有把局部推导伪装成逐指令 gem5 比对。
