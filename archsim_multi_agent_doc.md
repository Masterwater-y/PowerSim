# 基于多agent协作的多核CPU仿真器开发

> 本文总结一次基于 `botmux` 的多 agent 协作实验：通过两个 AI agent 分工推进 `MineSim / Sniper / perf baseline / CounterPoint` 闭环，用真实物理机器上的 `perf` 结果作为 baseline，持续定位仿真误差、审查修正假设，并把 MineSim 与 CounterPoint 的解释语义同步收敛。

## 1. 背景与目标

本实验的目标不是让 agent 自由写代码，而是把 CPU 仿真器开发拆成一个可审稿、可回退、可验证的闭环。

实验对象包括：

- `MineSim`：当前主线仿真器，用于建模单核 workload 的 cycles、instructions、branch、cache、TLB 等行为。
- `Sniper`：对照仿真器，用于与 MineSim 和物理机 baseline 做三方比较。
- `perf baseline`：物理机器上采集的真实 PMU 计数，作为误差计算基准。
- `CounterPoint`：约束求解和 DAG 诊断工具，用于判断 MineSim 的 counter / timing / decomposition 是否能被模型解释。
- `DynamoRIO drmemtrace`：旧链路用于 trace 采集，当前默认使用 `dynamorio/collect_drmemtrace.sh`，不使用 `dynamorio_release`。

默认实验集合为 5 个 workload：

| workload | 默认规模 | 主要用途 |
| --- | ---: | --- |
| `log_state` | `iter=1` | 混合回归点，观察整体退化 |
| `graph_walk` | `iter=1` | branch + memory + dependency 组合瓶颈 |
| `codec_pipeline` | `iter=1` | backend / dependency / compute-bound 路径 |
| `branch_dense` | `iter=5` | branch predictor / branch recovery 隔离验证 |
| `cache_bench` | `iter=1` | cache hierarchy / writeback / store pressure |

当前隔离工作区为：

```text
/data00/yinhaolang/simulators/archsim
```

最新已验证的默认 5-workload 结果目录为：

```text
/data00/yinhaolang/simulators/archsim/global/out/default_5workload_tripartite_oldtrace_20260527_rerun
```

## 2. 基于 botmux 的多 agent 协作环境

### 2.1 为什么需要两个 agent

早期如果只让一个 agent 同时完成“跑实验、解释误差、改代码、判断是否接受 patch”，容易出现几个问题：

- 看到误差就直接改模型，缺少反证。
- 同一轮同时修改多个语义点，导致收益无法归因。
- 过度相信单 workload 的结果，把局部拟合误认为模型改进。
- MineSim 改了输出语义后，CounterPoint mapping / DAG 没有同步，导致诊断结果失真。

因此本流程把 agent 固定分成两个角色：

| 角色 | botmux `/role` 定位 | 核心职责 | 禁止事项 |
| --- | --- | --- | --- |
| Agent A | 实验执行者 + 实现者 | 编译、跑实验、收集 perf / trace / MineSim / Sniper / CounterPoint、提出最小 patch | 不得一次引入多个独立修改；不得无证据大改模型 |
| Agent B | 误差分析者 + 审稿人 + 反方 | 审查假设、寻找反证、判断 patch 是否接受、要求 CounterPoint 同步 | 不得跟着 A 乐观推进；不得接受无 targeted validation 的 patch |

这套设计的关键是：一个 agent 负责推进，一个 agent 负责反驳。真正提升效率的不是“两个 AI 同时写代码”，而是把实验开发变成一种可审查的科学流程。

### 2.2 botmux 总控 prompt

`botmux` 的总控 prompt 需要明确项目根目录、实验链路、默认 workload、禁止 release 链路、每轮最小修改规则，以及 CounterPoint 同步要求。核心内容如下：

```text
目标：
自动推进 MineSim / Sniper / perf baseline 三方实验，对误差进行归因，并用 CounterPoint 辅助定位问题，逐步迭代 MineSim，使整体误差下降，尤其关注 CPI、core.cycles、branch.misses。

背景：
- 项目根目录：/data00/yinhaolang/simulators/archsim
- 关键组件：minesim, snipersim, counterpoint_lite, dynamorio, workloads, global/scripts
- 当前默认 5-workload 旧链路整轮 wall time：760.10s（约 12 分 40 秒）
- 当前主线只允许使用旧链路 dynamorio/collect_drmemtrace.sh，不要切换到 release 链路。

原则：
- 没有证据，不要改代码。
- 每轮只允许一个主要假设、一个主要 patch。
- 不允许两个 agent 同时修改同一文件。
- 优先复用已有 trace 和已有结果，避免重复昂贵实验。
- 每轮都必须输出：假设、证据、修改点、验证结果、是否接受。
- 只要 MineSim 的语义发生变化，CounterPoint 必须同步更新；该同步修改由 Agent B 发起和审核。
```

### 2.3 `/role` 设计

Agent A 的 `/role` 应强调执行与最小实现：

```text
你是实验执行者和实现者。
你的职责：
1. 读取当前共享状态文件与最新实验结果。
2. 运行 baseline / MineSim / Sniper / CounterPoint。
3. 对 MineSim 做最小必要修改。
4. 每轮输出结构化实验摘要，供另一个 agent 审核。
5. 你只能在证据支持下修改代码，不得一次引入多个独立想法。
```

Agent B 的 `/role` 应强调审稿与反证：

```text
你是 MineSim 误差归因分析者和审稿人。
你的职责：
1. 不盲目改代码，而是审查 Agent A 的实验结果和 patch。
2. 使用 CounterPoint、CPI decomposition、workload 类型和 counters 做归因。
3. 判断当前 patch 是否真正对准根因。
4. 如果 patch 不合理，明确指出为什么，并给出更好的下一步实验建议。
5. 当 MineSim 的语义、计数器导出、timing 组合逻辑发生变化时，你必须主导要求同步修改 CounterPoint 的 observation mapping 与 DAG/rules。
```

### 2.4 固定对话协议

为了避免两个 agent 随意聊天，流程要求每轮都使用固定模板。

Agent A 发给 Agent B：

```text
[Round N]
Hypothesis:
Evidence:
Patch:
Changed files:
Validation:
Results:
Question for B:
```

Agent B 回复 Agent A：

```text
[Round N Review]
Verdict: accept / reject / needs more evidence
Why:
What evidence is missing:
Best next step:
Approval scope:
```

这种协议减少了“长篇讨论但没有结论”的情况，并且方便把每轮结果落盘为 `round_XX_summary.md`、`accepted_changes.md` 和 `rejected_hypotheses.md`。

## 3. 与 MineSim、Sniper、CounterPoint 的结合方式

### 3.1 实验闭环

完整实验链路如下：

1. 编译 workload。
2. 用 `perf stat` 在物理机上采集 baseline。
3. 用旧链路 `dynamorio/collect_drmemtrace.sh` 采集 `.trace.gz`。
4. 运行 `MineSim + CounterPoint`，得到 MineSim 计数器、CounterPoint summary / diagnosis / violations。
5. 运行 `Sniper`，得到对照仿真结果。
6. 生成三方比较表，计算 CPI 与 counter 误差。
7. Agent A 提出最小修正，Agent B 审稿。
8. 先跑 targeted workload，再决定是否跑 full suite。

当前默认 5-workload 旧链路已经验证可完整跑通，整轮 wall time 为 `760.10s`，约 `12 分 40 秒`。因此 agent 不再把“整轮跑不通”当作第一 blocker，而是转向误差归因与 targeted validation。

### 3.2 CounterPoint 在流程中的角色

CounterPoint 不是最终裁决器，而是用于回答一个关键问题：当前 MineSim 输出能否被模型的 timing / cache / memory / branch / dependency DAG 解释。

Agent B 必须检查：

- `observation.minesim.json` 是否导入新增 counters。
- `model.from_minesim_config.json` 是否包含对应 DAG counters / rules。
- `summary.json`、`diagnosis.json`、`violations.csv` 是否支持当前假设。
- MineSim 修改是否只是“把数字调近”，还是确实改善了解释路径。

硬约束是：只要 MineSim 的语义发生变化，CounterPoint 必须同步更新。没有完成 CounterPoint 同步的 MineSim patch，不允许进入 accepted 状态。

### 3.3 workload 的归因分工

多 agent 流程不直接从 full suite 排名开始修，而是先用 isolating workload 做归因。

| workload | 归因方向 | 在 agent 流程中的用途 |
| --- | --- | --- |
| `branch_dense` | branch predictor / branch recovery | 验证分支方向预测、resolve cycle、branch cost 是否合理 |
| `cache_bench` | L1/L2/L3/DRAM、writeback、store queue | 验证 cache hierarchy 与 store pressure |
| `graph_walk` | branch + memory + dependency overlap | 验证组合型瓶颈，不适合直接盲修 |
| `codec_pipeline` | backend / dependency / compute-bound | 验证 dependency、issue、forwarding、base cycles |
| `log_state` | 混合回归 | 检查其他修正是否引入副作用 |

这一步是流程改进的核心：先隔离，再回归，而不是看到 full-suite 某个 workload 误差大就直接改模型。

## 4. Agent 协作前后的指标效果

### 4.1 当前 5-workload 总体结果

基于最新默认 5-workload 旧链路结果，全部 `25` 个对比点的平均绝对相对误差为：

| 统计口径 | MineSim | Sniper |
| --- | ---: | ---: |
| 全部 25 个 counter 点 | `50.138%` | `387.685%` |
| 去掉 `dtlb_load_misses` | `39.678%` | `50.355%` |
| 仅 `core.cycles + core.instructions + branch.misses` | `22.754%` | `26.878%` |
| 仅 `core.cycles + core.instructions` | `20.827%` | `24.040%` |

`Sniper` 的全部 counter 平均误差被 `dtlb_load_misses` 极端异常显著放大，因此更合理的比较口径是去掉该项后再看核心 counters。此时 MineSim 在主指标上整体略优于 Sniper。

### 4.2 CPI 对比

| workload | perf CPI | MineSim CPI | Sniper CPI | MineSim CPI 误差 | Sniper CPI 误差 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `log_state` | `0.7882` | `0.7596` | `0.9833` | `-3.627%` | `+24.748%` |
| `graph_walk` | `0.5282` | `0.7335` | `0.5715` | `+38.873%` | `+8.190%` |
| `codec_pipeline` | `0.5332` | `0.5074` | `0.7500` | `-4.842%` | `+40.655%` |
| `branch_dense` | `1.2996` | `1.7214` | `1.1001` | `+32.459%` | `-15.345%` |
| `cache_bench` | `0.7893` | `0.7611` | `0.4352` | `-3.570%` | `-44.858%` |

从 CPI 看，MineSim 在 `log_state`、`codec_pipeline`、`cache_bench` 上已经比较接近 baseline；`graph_walk` 与 `branch_dense` 仍是主要问题点。

### 4.3 工作流前后的硬收益

以 agent 工作流启动前的 formal 结果，对比工作流启动后的首轮正式 5-workload baseline refresh 和后续被接受的 overlap / resolve 修正，可以看到 suite 级主指标误差下降。

| 指标 | workflow 前 formal | workflow 后 Round 2 baseline refresh | 收益 |
| --- | ---: | ---: | ---: |
| CPI | `16.67%` | `10.08%` | `-6.59pp` |
| `core.cycles` | `23.84%` | `18.80%` | `-5.04pp` |
| `branch.misses` | `26.61%` | `22.31%` | `-4.30pp` |

排除 `cache_bench` 这个 trace / ROI 污染最强的 workload 后，收益更明显：

| 指标 | workflow 前 formal, excl. cache_bench | workflow 后 Round 2, excl. cache_bench | 收益 |
| --- | ---: | ---: | ---: |
| CPI | `19.95%` | `11.03%` | `-8.92pp` |
| `core.cycles` | `15.85%` | `9.25%` | `-6.60pp` |
| `branch.misses` | `21.15%` | `12.71%` | `-8.44pp` |

代表性 workload 的收益包括：

| workload | 指标变化 | 解释 |
| --- | --- | --- |
| `branch_dense` | CPI 误差 `+32.46%` → `+3.88%` → `+0.77%` | branch / memory overlap 与 resolve_cycle 语义修正显著改善了干净 branch workload |
| `graph_walk` | CPI 误差 `+38.87%` → `+29.89%` → `+13.07%` | targeted 验证 overlap 语义后再回归 full suite，带来最大单点收益之一 |
| CounterPoint `timing_mcw` | `graph_walk` / `branch_dense` 的显著违规消失 | MineSim 数字变近的同时，CounterPoint 解释口径也对齐了一步 |

### 4.4 工作流带来的软收益

后续 Round 17 到 Round 31 的更大收益，不是立刻跑出一个更低的 suite MAE，而是把高风险误判逐步排除。

已收敛的判断包括：

- `branch_dense`：大头 branch 残差不是简单 global gating / loop predictor suppression，而是当前 predictor family 对热点 PC 已接近上限。
- `graph_walk`：`branch_memory_overlap -99%` 不是 MineSim overlap 实现 bug，而是 CounterPoint orphan counter 造成的伪 violation。
- `codec_pipeline`：`base_cycles` 主 violation 主要是 solver artifact，不值得继续大修 backend / dependency 语义。
- `cache_bench`：`cache.l2.misses +5.36` 不是 MineSim cache path 错，而是 CounterPoint 缺少精确 `L2 writeback -> L3 access` 路径。

这类收益不一定马上体现在 MAE 数字上，但它显著减少了三类浪费：

1. 不再为伪问题开 MineSim patch。
2. 不再把 trace / ROI 问题误修成 simulator bug。
3. 不再把 CounterPoint 模型缺口误读成 MineSim 设计错误。

## 5. Agent 协作真正改进了什么

与工作流开始前相比，改进主要体现在方法论和工程纪律上。

| 改进点 | 协作前 | 协作后 |
| --- | --- | --- |
| 修复入口 | 直接看 full suite 排名，容易盲修最差 workload | 先 isolating workload，再 full-suite 回归 |
| 问题分类 | 看到 violation 就倾向于改 MineSim | 先判断属于 MineSim / trace ROI / CounterPoint 哪一类 |
| patch 粒度 | 容易一轮改多个方向 | 每轮一个主假设、一个 patch、一个明确回归面 |
| 验证方式 | 单次结果容易被误读 | same-binary / same-trace / targeted validation |
| CounterPoint | 容易滞后于 MineSim 修改 | Agent B 强制检查 mapping / DAG / rules 同步 |
| 风险控制 | 局部收益可能带来全局回归 | Agent B 反方审稿，未通过 targeted validation 不接受 |

一句话概括：

> 工作流开始前，我们更多是在“看误差、猜问题、试 patch”；工作流开始后，我们变成了“先证伪、再最小修、最后回归”。它带来的直接收益是 suite 主指标误差下降，间接收益是把大量伪问题排除掉，让真正还值得修的点快速收缩到少数几个。

## 6. 当前仍然打开的问题

截至最新实验总结，当前真正还打开的问题已经收缩到少数方向。

| 问题 | 当前状态 | 下一步 |
| --- | --- | --- |
| `cache.l2.writebacks` 精确 anchor | 已在 CounterPoint mapping / model 中加入精确 writeback anchor | 做 `graph_walk + cache_bench + log_state` targeted regression |
| `cache_bench SQ Drain = 0.198 CPI` | 仍可能代表 store queue / drain 语义偏悲观 | 先 observation-only，统计 queue depth、drain 次数、持续周期 |
| `log_state` | 混合回归点，不是主修对象 | 用于检查 cache / memory / backend 修正是否引入副作用 |
| `branch_dense / graph_walk / codec_pipeline` | 诊断基本收口 | 除非出现新强证据，否则不再优先开 patch |

## 7. 局限与风险

### 7.1 agent 协作层面的局限

多 agent 流程降低了盲修概率，但并不能完全消除 agent 行为问题。

主要局限包括：

- Agent 经常忘记 mention 对方，导致 `botmux` 对话回合没有真正交接。
- Agent 可能脱离 `/role` 约束，例如执行者开始做审稿，审稿人开始主动改代码。
- Agent 容易搞错当前重点，例如已经明确禁用 release 链路后，仍可能回到 `dynamorio_release` 调试。
- Agent 容易被局部数值改善吸引，而忽略 full-suite 回归和 CounterPoint 解释是否同步改善。
- 长轮次后上下文会膨胀，必须依赖落盘的 `current_status.md`、`round_XX_summary.md`、`accepted_changes.md` 和 `rejected_hypotheses.md` 才能维持一致性。

因此，多 agent 并不是“放任两个机器人自己讨论”，而是必须配合强模板、硬约束和审稿门禁。

### 7.2 实验平台层面的局限

这套流程依赖物理机器上的 `perf baseline`，因此存在一个根本限制：它无法直接做微架构泛化。

原因是：

- baseline 来自当前物理机器，PMU 计数、cache hierarchy、branch predictor、TLB、uncore 行为都绑定到这台机器。
- MineSim 的修正目标是贴近这台机器的观测，而不是自动学习任意 CPU 微架构。
- 如果换到另一代 CPU，`perf` 事件语义、cache/uncore PMU、branch predictor 行为、内存系统参数都可能变化。
- CounterPoint 的 mapping / DAG 也会随目标机器变化，需要重新校准。

因此，这套流程更适合做“面向某个目标机器的仿真器校准与误差归因”，而不是直接产出一个跨微架构泛化的 CPU 模型。

### 7.3 工程链路层面的局限

当前实验链路还存在工程成本：

- 旧 `DynamoRIO` 链路会打印 root / lib32 warning，但默认 5-workload 已验证可跑通。
- `dynamorio_release` 当前明确禁用，不作为主线入口。
- `drraw2trace` 与 `Sniper` 是主要耗时来源，默认 full suite 需要按约 15 分钟预算预留。
- `dep_chain` 与 `mlp_stream` 当前不进入默认 suite，因为最小规模下 trace 成本仍过高。
- `trace / ROI` 问题会污染部分 workload 的 instructions 与 PMU 解释，不能简单把所有误差归因给 MineSim。

## 8. 结论

本实验说明，多 agent 协作在 CPU 仿真器开发中的价值不只是“自动跑更多命令”，而是把模型迭代过程变成了一个受约束的验证闭环。

关键经验如下：

1. 角色必须固定：Agent A 执行，Agent B 审稿。
2. 每轮必须最小化：一个假设、一个 patch、一个 targeted validation。
3. CounterPoint 必须与 MineSim 同步，否则诊断会变成伪证据。
4. 先用 isolating workload 做归因，再用 full suite 做回归。
5. 指标收益不仅体现在 MAE 下降，也体现在排除伪问题、减少无效 patch、缩小开放问题集合。
6. 由于 baseline 依赖物理机器，这套流程目前不具备微架构泛化能力，更适合作为单目标机器的仿真器校准方法。

最终，这套流程把 MineSim 的开发从“看误差后凭经验修”推进到“证据驱动、审稿约束、可回退验证”的工程化迭代模式。
