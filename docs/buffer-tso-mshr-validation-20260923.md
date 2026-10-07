# Buffer TSO / MSHR：共享响应、资源归属与局部性首轮验证

日期：2026-09-23。前置讨论：[store timing 文档](buffer-tso-store-timing-20260923.md)。

**结论：服务身份/绝对响应引用的方向有定向机制支持，但现有受限服务引用开关没有足够精度收益，不能作为完整 MSHR 修复推广。**
本轮没有改动模拟器、生产配置或默认开关；复用了现有机制，所有实验代码、二进制和原始输出在
`tmp/buffer-mshr-validation-20260923/`。没有实现完整跨 epoch MSHR 或增量失效求解器，不能宣称已证明其吞吐成本。

## 1. 验证对象和来源门

- 生产基线：冻结本轮 `build/fastsim`，buffer=114，residence certificate / prepare lifecycle 开启，buffer alias 关闭。
- 服务引用对照：同一二进制、同一配置，仅开启已有 `core.response_private_read_services`。它保留两阶段框架；不是新 MSHR 实现。
- 旧接线反例：合成窗口开启已有 `ruby.buffer_line_coalescing`，不进入生产默认配置。
- 完整 ROI：NAMD、Graph500、TeaLeaf、LBM C4 和 LBM C32，共 5 项诊断小集；并非 38-case 矩阵，也没有新的 held-out 划分。
- 数据：`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source`，first-core common-end、record-bounded functional warmup、user-plus-kernel。
- 独立 `validate_fs_oracle_identity.py`：5/5 身份有效。两组 `run_native_kernel_fastsim_validation.py`：各 5/5 passed、5/5 PMU scored，0 failed/pending；每项 CPI instruction-population gate 通过。
- cache 真值只使用维护 validator 选择的 native Ruby/SLICC；没有使用 path-class。L1D/private-L2/branch 映射仍是 proxy，LLC 为 diagnostic，passed 不等于严格硬件 PMU 或精度认证。
- `identity.json`、`baseline/summary.*`、`service/summary.*`、逐 case `validation.json`、`comparison.json`、`fingerprints.json` 保留来源与结果。配置使用已有同次采集 store 参数；未引入拟合时延。

## 2. 先纠正文档里的 Sequencer / MSHR 混用

本地 gem5 `tmp/gem5-tso-buffer-20260917/gem5/src/mem/ruby/system/Sequencer.cc`：

1. `makeRequest` 的普通请求容量门在 `insertRequest` 之前；store prepare 有独立入口，不能机械套入普通 demand 规则。
2. `insertRequest` 将普通 follower 加入请求列表并增加 `m_outstanding_count`，然后返回 `Aliased`。
3. 未合并请求随后才 `issueRequest` 到控制器。Sequencer 行表不是只在 L1 miss 后建立。

因此必须分别建模 **CPU request outstanding、每行等待者、各级 miss/TBE 事务**。
follower 可以不新增下层 miss 事务，但仍占普通 Sequencer 请求容量。

Native NAMD 短采集的四核观测包含 64,019 个可匹配请求、50,034 个 load，其中 14,202 个 native coalesced load。
按同核、同行、相同 callback、单请求未合并父候选配对：10,833 个 follower 的父候选是 native L1 hit，3,349 个为其他父候选，20 个无法如此配对。
这不是全 ROI 分布，也不是新的 PMU 精度评分，但足以反对“只有 L1 miss 才有 Sequencer 合并”的普遍断言。

一个直接见证（native ticks）：core 1、line 5709304，父 `inst_seq_num=701932`，follower `701934`，
两者 admission=23666068284291、callback=23666068284624。父 `l1d.accesses=1,hits=1,tag_misses=0`，follower `native_coalesced=1`。
见 `native-parent-witness.json`；本地源码快照与这一 native 事实交叉支持，不把源码路径本身视为采集二进制身份认证。

**旧文档 8.6 的 miss-only 过滤只能称实验限制。不能把全部 native alias 数都解释为唯一 miss 的合并数，也不能据此断言全部 CPI/PMU 残差有唯一根因。**

## 3. 单变量反例：重复等待发生在准入变化之后

`probe.cpp` 链接当前 `fastsim_lib`，覆盖同线多读、读后写、异线读、RAW 顺序反转、真实跨 epoch 五种形状，
分别运行 baseline / buffer alias / private service，Sequencer 容量分别为 2、16，共 30 个 materialized 路径实验。
合成配置用于关系验证，不代表目标机器的绝对时延或 CPI。

四条独立同线读、容量=2：

| 路径 | 第 1/2 条响应 | 第 3/4 条 issue | 第 3/4 条响应 | 请求数 | 结果 |
|---|---:|---:|---:|---:|---|
| baseline | 113 / 9 | 9 / 13 | 13 / 17 | 4 | follower 提前看到数据 |
| buffer alias | 113 / 113 | 113 / 113 | 221 / 221 | 4 | 准入已移出父生命周期，仍重复套原等待 |
| private service | 113 / 113 | 112 / 112 | 116 / 116 | 4 | callback=112 释放槽；后两条不再合并，走本地服务 |

旧 alias 第三条保留 canonical response=113、latency=108，却在修正后的 issue=113 上重新相加，得到 221。
这比“所有 follower 等待逐条裸加”的笼统解释更精确：容量=16 时旧 alias 四条均在 113 返回，并没有必然逐条串行。
失败触发点是**准入后移之后，服务身份与合并有效区间没有同步重判**。

服务引用在容量=16 的同线读窗口产生 1 owner / 3 followers，所有读共享 response=113；容量=2 只有 1 follower。
异线控制完全不变；读写混合和 RAW 反转被受限服务证明拒绝，保留旧结果。

128 条同线读的跨 epoch 窗口确实进入 3–4 个 checkpoint：服务引用因 outgoing 边界拒绝 48 个候选，0 follower，结果与 baseline 相同。
它证明当前实现不会伪造跨批引用，也证明**只打开现有开关不能覆盖持久 MSHR 生命周期**。

`check_probe.py` 的 108 项诊断断言通过。另运行 generic + CPI attribution 的相同 30 项：26 项完成且输出与 materialized 相同；
读写混合的 baseline/service × 两容量共 4 项触发现有 `response residual stage accounting is not conserved`。
baseline 已能触发，故不归因为新方案；本轮未修复它，也不宣称 generic 全组合通过。见 `probe-generic-checks.json`。

## 4. 局部性：小同行分量不等于小失效闭包

复用维护 `audit_line_generation_components.py`，将 native admission/callback 与 trace PA 配对，
然后在离线诊断中额外连接同核存活区间重叠的请求，观察潜在 request-capacity 耦合。
后一种连接是观察范围内的保守可能竞争，**不是已经发生的容量阻塞，也不是实际增量重算次数**。

| Native 观测 | 请求数 | 同核同行最大分量 | 加 cache set 最大分量 | 再加同核请求潜在竞争 |
|---|---:|---:|---:|---:|
| NAMD C4 短采集 | 64,019 | 8 | 11 | 97 |
| TeaLeaf C4 短采集 | 37,877 | 12 | 41 | 111 |

Graph500 有 35,349 个 native 身份未匹配到该工具选取的 trace 记录；保留原始输出，但在 `coverage-review.json` 明确排除其范围结论。
不能把过滤后的小分量冒充完整覆盖。

仍缺 speculative/I-side、未记录的 prepare、完整 RAW、bank/channel 和跨窗口边。即使 NAMD/TeaLeaf 身份匹配无缺失，
上述分量也不是生产级闭包证书。**文档原有 +15%–45% 局部重算开销估算仍未经验证，不能用这里的 97/111 个请求直接换算吞吐。**

## 5. 完整 ROI：现有服务引用覆盖不足，收益接近零

CPI 绝对误差、MAE 单位均为 cycles/macro instruction；相对误差带符号，MAPE 配置等权。
下表来自维护 validator 的未舍入输出，无自行选择参考。

| Case | gem5 CPI | 基线 CPI | 基线相对误差 | 基线绝对误差 | 服务引用 CPI | 引用相对误差 | 引用绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c04-lbm | 0.817326 | 0.776815 | -4.9565% | 0.040511 | 0.775949 | -5.0626% | 0.041378 |
| c04-tealeaf | 1.004183 | 0.860056 | -14.3527% | 0.144128 | 0.860056 | -14.3527% | 0.144128 |
| c04-graph500 | 1.305529 | 1.179437 | -9.6583% | 0.126092 | 1.181457 | -9.5036% | 0.124072 |
| c04-namd | 0.479341 | 0.389645 | -18.7123% | 0.089696 | 0.389435 | -18.7562% | 0.089906 |
| c32-lbm | 1.502989 | 1.756115 | +16.8415% | 0.253126 | 1.756125 | +16.8421% | 0.253136 |

| 5-case aggregate | CPI MAPE | CPI MAE |
|---|---:|---:|
| baseline | 12.904265% | 0.130710 |
| service | 12.903444% | 0.130524 |

| Case | 候选 load | owner | follower | arrival 拒绝 | incoming 拒绝 | outgoing 拒绝 |
|---|---:|---:|---:|---:|---:|---:|
| c04-lbm | 395,091 | 78 | 377 | 69,613 | 322,942 | 1,522 |
| c04-tealeaf | 2,860 | 29 | 34 | 427 | 1,601 | 20 |
| c04-graph500 | 424,146 | 40,864 | 38,151 | 182,031 | 68,318 | 27,184 |
| c04-namd | 350,596 | 27,931 | 24,568 | 98,416 | 62,015 | 1,849 |
| c32-lbm | 3,706,446 | 13 | 76 | 63,611 | 3,642,535 | 95 |

其余候选包含已完成后的 hit，不能把候选数减 follower 数全部称为拒绝。
尤其 LBM C32 的 incoming 完成上界排除了大多数候选：若需要实用覆盖，必须保留跨 epoch 的明确服务身份，不能只依赖“前批所有请求都已结束”的全局证明。

本轮未修复 PMU 请求合并定义。两组 native PMU 分布与来源详见 `baseline/summary.md`、`service/summary.md` 和 JSON 中的
`pmu_accuracy`（finite count、MAPE、P50/P90/P99、WAPE、signed bias）。少数计数随 timing/TSO 路径变化；不把这种小变化当作完整 alias PMU 修复。

## 6. 吞吐对照的范围

使用相同冻结二进制和完整 ROI，NAMD C4、Graph500 C4 分别串行 ABBA（baseline/service/service/baseline）。
测量对象仅是**现有受限服务引用开关**，不是尚未实现的完整 MSHR/增量求解器。
每次都核对目标 scope 指标与对应维护 validator 已验输出一致；记录实际 user UOP 吞吐、end-to-end wall、host load 和 CPU affinity。
数据见 `bench/runs.json` 和 `bench/summary.json`。共 8 次 replay，测量段 user-UOP 吞吐如下（M user-UOP/s，非目标 IPC，非宏指令 MIPS）：

| Case / arm | 均值 | P50 | P90 | P99 | 最低 | end-to-end wall 均值(s) |
|---|---:|---:|---:|---:|---:|---:|
| NAMD baseline | 2.809445 | 2.809445 | 2.812902 | 2.813680 | 2.805124 | 14.411636 |
| NAMD service | 2.759673 | 2.759673 | 2.761486 | 2.761894 | 2.757408 | 14.671452 |
| Graph500 baseline | 2.302138 | 2.302138 | 2.308268 | 2.309648 | 2.294475 | 31.614012 |
| Graph500 service | 2.271164 | 2.271164 | 2.280371 | 2.282442 | 2.259655 | 32.176336 |

吞吐均值分别下降 **1.77% / 1.35%**。每组只有两个样本，分位数使用 Type-7，不能据此作稳定 tail 性能结论。
host 1-minute load 的各次起止值保存在 runs.json，开始值约 3.92–4.82；实验 replay 并发=1，未与本轮 native 审计/编译并行，未独占整机。
输出保留每次 affinity/可见 CPU 数；不能把低 load 当作无干扰证明。这里只测 user-plus-kernel replay，没有新的独立 user-only 性能结果。
测量段排除 functional warmup；end-to-end wall 包含 warmup。实际 user/mixed UOP 人口及测量段 wall 在 summary.json 中逐 arm 保存；
native warmup 缺少特权人口拆分，end-to-end user-UOP rate 明确 unavailable，不用测量段人口冒充 end-to-end rate。

这个结果支持“现有受限引用的成本小”，但它的完整 ROI 收益接近零；绝不能外推为“完整 MSHR 只需约 2% 开销”。

## 7. 本轮决策与下一项最小实验

1. 不推广 `ruby.buffer_line_coalescing`，也不推广受限 `response_private_read_services` 为完整 MSHR 修复。
2. 撤回“只对 L1 miss 建 Sequencer 表”的普遍语义；将 CPU 请求槽、line 服务和各级 miss 资源分别记录。
3. 下一项有价值的最小实现是**跨 epoch 的只读服务身份及准入有效性检查**：先覆盖容量=2 导致 follower 越过 callback、RAW 导致父子顺序反转、incoming 服务仍在飞三个反例。
4. 数据就绪引用具体父响应；容量阻塞后重新判断 alias，不能沿用旧延迟。读后写分阶段，MSHR、Sequencer、WB、SQ 各按自己的事件释放。
5. 必须同时输出“可直接复用、关系失效、失效传播节点数、跨批服务数、回退数”。未实现并测量真实传播前，不承诺单趟、常数开销或局部收敛吞吐。

构建 `cmake --build build -- -j16`、`./build/fastsim_tests` 通过；维护分量审计 7 项 Python 测试通过。
完整 ROI 10 次 replay、合成 materialized 30 次和 generic 30 次均已执行；generic 的 4 项既有失败如第 3 节所列。
实验脚本为诊断产物，不是新的生产求解器或精度评分入口。本轮未提交或推送。
