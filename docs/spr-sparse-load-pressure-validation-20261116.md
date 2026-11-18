# 失败版本回退与稀疏 load 批次下界：实现和验证

2026-11-16。旧 P0/P1/P2 已精确回退。新候选已实现并完成 Graph500 C4/C8 验证；**低成本目标在本轮小样本中未见明显退化，CPI 仍未改善，故保持默认关闭，安装 M4 不变。** 本文不将机制守恒或构建通过写成精度通过。

## 回退与新实现

- [回退收据](../tmp/sparse-load-pressure-20261116/rollback-receipt.json)：九个失败版本文件先按冻结 SHA 校验并备份，六个恢复到失败前源码哈希，三个失败专属头文件/测试/候选配置从工作代码移除。旧测试 ELF 另有归档收据。没有使用 git restore/reset 覆盖其他未提交工作。
- 删除每 UOP 的 FunctionalIssue 描述、全量 FU 日历、失败预约/唤醒实现及旧开关；归档源码、实验和负面报告保留。
- 新 `core.sparse_load_pressure` / `--sparse-load-pressure` 默认 false，候选 overlay 为 [spr-sparse-load-pressure-candidate.cfg](../configs/spr-sparse-load-pressure-candidate.cfg)。
- 新 helper 为 [sparse_load_pressure.hpp](../include/fastsim/sparse_load_pressure.hpp)，仅维护每核常数状态；没有 map/vector、动态分配、逐周期扫描、非内存 UOP 再预约或重发倍数。

## 近似规则

只处理已经在同一次最早 Sequencer callback R 前就绪、被成员槽阻塞的普通单片段 load。目标每周期可服务 L 个 load，则该批第 n 个成员的乐观成功需求下界为：

```text
bound(n) = R + floor((n - 1) / L)
new_admission = max(existing_admission, bound(n))
```

只把超过已有接收等待的部分送入原来的响应/依赖反馈。callback 改变时开始新批次。额度释放来自当前模型，不使用 gem5 grant、重试或 stall 时序；端口容量沿用来源绑定的目标配置，不修改固定延迟拟合 CPI。

同线成员额度与下层服务仍由原模型管理；转发、特殊访问、软件预取、atomic、PTE、多片段和不具备普通访问功能资格的成员不进入新下界。状态随通常 proposal/commit 和跨 chunk/warmup 时间轴保留。新模型仅是成功 load 需求这一资源组的首个切片，不是所有 FU 资源的解析替代。

共享 cache/DRAM 继续按原有一次提交顺序处理，局部延迟不会触发全窗口收敛；共享到达顺序的残余近似仍存在。累计修正周期可重叠，不能当作 CPI 贡献。

独立审阅发现三类不支持组合：`response_shared_service_constraints` 的回退状态迁移，以及 `dtlb.hierarchy_walk` / `software_prefetch_early_ack` 的 source-order 路径。现已明确拒绝这些组合，避免状态遗漏或静默绕过机制。

## 测试与模型身份

- 聚合下界测试先 RED（机制缺失）后 GREEN：带宽边界、已有等待吸收、复制隔离、callback 更新、超大时间和溢出。
- 集成测试覆盖跨 chunk、功能人口/cache 事务保持、OFF 中性和正向资源修正；构造用例 435 个受阻成员、1 次正修正，计数及修正周期均断言非零。
- 集成 RED 发现普通 load 资格收集未包含新开关，修正后通过；审阅配置组合回归同样经历 RED→GREEN。
- `cmake --build build -- -j16` 完成；确认 `-flto=16`；最终 focused 测试和重新链接的 `fastsim_tests` 均通过。最终收据见 [final-test-receipts.json](../tmp/sparse-load-pressure-20261116/final-test-receipts.json)。
- 初轮严格 reader 在审阅兼容性收紧前执行；最终计时 reader 的全部逐核 cores、configuration、time_accounting 和除宿主 throughput 外的 scope_metrics 均与相应严格输出一致。最终 ELF 另行回放并输出完整来源身份，维护 quick 评分采用这些独立输出；计时路径按合同关闭额外来源哈希，直接评分曾因身份缺失被拒绝，该失败记录保留在 final-score-off.log 的前置归档中。
- M4、候选严格 ELF、初轮严格 ELF 和最终计时 ELF 均单独保留身份；详见 [freeze.json](../tmp/sparse-load-pressure-20261116/freeze.json)。

## CPI：无改善，候选不推进默认

quick 使用保存的独立 BaseCPU task-clock 证据，两组各 2/2 CPI scored、failed/pending=0。终点为 `retired-stream-completion-v1`，逐核 task macro 人口相同。原生 PMU 和真实 SPR 硬件等价性未重新认证。

| 来源 | gem5 CPI | M4/OFF CPI | 新候选 CPI | RE OFF→ON | AE OFF→ON |
|---|---:|---:|---:|---:|---:|
| c04-854.graph500_s | 0.966422 | 0.839507 | 0.836803 | -13.132% → -13.412% | 0.126915 → 0.129620 |
| c08-854.graph500_s | 0.969791 | 0.851812 | 0.851502 | -12.165% → -12.197% | 0.117979 → 0.118290 |

RE=(FastSim−gem5)/gem5；AE=abs(FastSim−gem5)，单位 cycles/macro。

| 等权两例 | CPI MAE | CPI MAPE |
|---|---:|---:|
| off | 0.122447 | 12.649% |
| on | 0.123955 | 12.805% |

| 来源 | 受阻成员 | 批次 | 实际提高接收下界次数 | 增量周期累计 | 最大批次 |
|---|---:|---:|---:|---:|---:|
| c04-854.graph500_s | 6,073,235 | 1,788,756 | 2,491,962 | 4,139,567 | 16 |
| c08-854.graph500_s | 11,392,476 | 3,351,513 | 4,669,566 | 7,748,779 | 16 |

新增下界产生了真实反馈，但并未改善最终 CPI，不能把累计修正量视为已补回的目标周期。在现有一次共享提交及响应反馈中，接收顺序、MLP 和等待重叠仍会改变净效果。此结果不支持把这部分成功需求/重试压力作为 Graph500 主要残差的已闭合解释，也不支持继续增加固定罚时。

## 触发不等于精度收益：后续澄清

候选并非没有触发：C4/C8 分别提高 2,491,962/4,669,566 个成员的接收下界。最终 active cycles 总和却分别减少 84,130/19,316；基线本来低估 CPI，因此误差反而扩大。累计新增接收等待 4,139,567/7,748,779 周期有大量重叠，不能当作最终损失。

该候选近似的是单个 callback 批次的成功 load 带宽，并非原生失败尝试、重发执行资源和 IQ 持有的完整机制。原先“原生发射很多→遗漏重试关键路径成本”的假设没有得到本轮闭环证明。它作为低成本资源下界有明确含义，但作为 Graph500 精度修复的依据仍不足。

跨批次也只是乐观近似。当前 helper 的独立边界演示：3 个端口，callback=100 的 6 个成员得到下界 100×3、101×3；callback=101 的新批次再有 3 个成员，下界又是 101×3。两个批次在 101 的下界合计为 6，并不是满足 3 端口容量的完整排程。代码遇到新 callback 会重置批次，而不会维护前一批次的剩余端口预约。这是抽象范围的限制，不能据此断言 Graph500 中这一重叠实际发生或已解释其残差。演示见 [cohort-overlap-diagnostic.cpp](../tmp/sparse-load-pressure-20261116/cohort-overlap-diagnostic.cpp)。

已有 ON/OFF 统计还显示接收累计等待增加、parent follower/合并和队列等待改变；这些重叠计数不能逐项相减分解 CPI。尚未完成逐成员关键路径归因，因此不能把最终周期减少全部归给 MLP 改善或共享到达近似。后续优先验证关键因果链，再决定是否增加新的低成本机制；不继续用触发次数或固定罚时替代精度证据。

## 吞吐：最终版本每例三组 OFF/ON

共 12 次串行回放，固定 CPU 52–63、NUMA1、既有 performance 策略；pair 0/2 顺序 OFF→ON，pair 1 为 ON→OFF。记录工作频率，不修改 governor，不清系统缓存。共享宿主上的小样本配对诊断，不是同输入 30 对正式验收。

主分母按 [现行 E2E 合同](trace-comparison-throughput-contract-20261116.md) 包含输入读取/预处理、初始化、warmup、模拟及必要排空，额外可靠性审计另计；user UOP 为测量段分子。纯模拟速率是辅助项。汇总使用总 UOP/总 wall，未平均不同负载速率。

| 范围 | OFF E2E Muser-UOP/s | ON E2E Muser-UOP/s | E2E 速率变化 | 纯模拟速率变化 |
|---|---:|---:|---:|---:|
| c04-854.graph500_s | 1.547 | 1.548 | +0.065% | +0.201% |
| c08-854.graph500_s | 1.631 | 1.633 | +0.133% | +0.202% |
| both | 1.602 | 1.603 | +0.109% | +0.202% |

| 来源/pair | E2E ON/OFF | 纯模拟 ON/OFF |
|---|---:|---:|
| c04-854.graph500_s/0 | 1.006691 | 1.009924 |
| c04-854.graph500_s/1 | 0.998106 | 0.994389 |
| c04-854.graph500_s/2 | 0.997139 | 1.001697 |
| c08-854.graph500_s/0 | 1.009893 | 1.014163 |
| c08-854.graph500_s/1 | 0.992080 | 0.988752 |
| c08-854.graph500_s/2 | 1.002083 | 1.003229 |

| 来源/pair/模式 | 准备秒 | warmup秒 | 模拟秒 | E2E秒 | 工作频率均值GHz |
|---|---:|---:|---:|---:|---:|
| c04-854.graph500_s/0/off | 9.530 | 1.477 | 15.000 | 26.024 | 3.213 |
| c04-854.graph500_s/0/on | 9.512 | 1.470 | 14.852 | 25.851 | 3.288 |
| c04-854.graph500_s/1/on | 9.531 | 1.463 | 14.807 | 25.818 | 3.245 |
| c04-854.graph500_s/1/off | 9.555 | 1.474 | 14.724 | 25.769 | 3.226 |
| c04-854.graph500_s/2/off | 9.467 | 1.460 | 14.776 | 25.719 | 3.243 |
| c04-854.graph500_s/2/on | 9.568 | 1.457 | 14.751 | 25.793 | 3.269 |
| c08-854.graph500_s/0/off | 18.471 | 2.581 | 28.143 | 49.216 | 3.354 |
| c08-854.graph500_s/0/on | 18.384 | 2.581 | 27.750 | 48.734 | 3.336 |
| c08-854.graph500_s/1/on | 18.511 | 2.613 | 27.953 | 49.096 | 3.317 |
| c08-854.graph500_s/1/off | 18.489 | 2.561 | 27.639 | 48.707 | 3.382 |
| c08-854.graph500_s/2/off | 18.495 | 2.548 | 28.004 | 49.066 | 3.363 |
| c08-854.graph500_s/2/on | 18.451 | 2.580 | 27.913 | 48.964 | 3.368 |

此前建议的 E2E≤3%、纯模拟≤5%损失为开发目标；本轮只提供小样本观测，保留全部配对和范围，不据此宣布正式吞吐通过。即使吞吐成本很小，精度无收益仍不足以推进默认。

审阅前另有六次完整 C4 测量，因配置兼容性修正和最终 ELF 变化，单独保留在 [throughput-pre-review](../tmp/sparse-load-pressure-20261116/throughput-pre-review/)。它们没有混入当前 12 次主结果；原路径和迁移记录均保留。首次计时脚本在定位旧 C8 command 时失败，未启动该 C8 进程，已改为使用原收据 artifact_dir。没有按快慢删选样本或补跑直到过门槛。

## 使用、状态与后续

```bash
source scripts/project_tmp.sh
LD_LIBRARY_PATH=/opt/gcc-11/lib64 python3 scripts/run_high_frequency.py -- \
  tmp/sparse-load-pressure-20261116/fastsim-candidate-strict simulate \
  --config SOURCE_BOUND_M4.cfg --manifest ORIGINAL_MANIFEST \
  --sparse-load-pressure true
```

关闭新机制为 `core.sparse_load_pressure=false`；当前默认和安装 M4 均未变。旧 P0/P1/P2 开关不再是工作代码接口。新源码保留默认关闭候选，以便复核，不把负面结果改写为修复成功。后续应回到同起点服务时间、在途持有与关键依赖的离线定位，而非继续以原生发射倍数拟合。

主要收据：[回退](../tmp/sparse-load-pressure-20261116/rollback-receipt.json)、[旧测试 ELF 归档](../tmp/sparse-load-pressure-20261116/retired-test-binary.json)、[执行清单](superpowers/plans/2026-11-16-sparse-load-pressure.md)、[执行记录](../tmp/sparse-load-pressure-20261116/progress.md)、[全部新结果](../tmp/sparse-load-pressure-20261116/results.json)、[12 次吞吐收据](../tmp/sparse-load-pressure-20261116/throughput/runs.json)、[最终 OFF quick 评分](../tmp/sparse-load-pressure-20261116/final-scoring/off/summary.json)、[最终 ON quick 评分](../tmp/sparse-load-pressure-20261116/final-scoring/on/summary.json)。
