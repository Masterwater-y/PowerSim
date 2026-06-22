# MineSim 模拟器精度改进项目一期验收研究报告

> 项目范围：基于旧链路 trace 的默认 5-workload 集合，围绕 `MineSim / perf baseline / Sniper / CounterPoint` 闭环，完成一期误差归因、最小修正、协作框架搭建与阶段性冻结基线。

## 0. 报告规划与验收风格判断

### 0.1 本报告拟包含的章节

为符合“一期验收”场景，本报告按以下章节组织：

1. **项目目标与验收口径**
   - 说明一期究竟要回答什么问题：不是把所有误差归零，而是建立一条可复现、可审稿、可回退的仿真器精度迭代主线。
2. **实验对象、基线与验证链路**
   - 交代 MineSim、Sniper、perf baseline、CounterPoint、drmemtrace 的关系，以及默认 5-workload 的选择依据。
3. **多 Agent 协作框架设计与落地**
   - 单独作为一个章节，描述 role 设计、botmux harness、固定协议、审稿门禁、same-binary / same-trace discipline。
4. **一期关键技术工作与方法论升级**
   - 说明 overlap / resolve 修复、CounterPoint 对齐、cache writeback 路径补模、运行环境固化等工作，不只是罗列 patch，而是讲清“为什么值得修”。
5. **一期结果：误差收敛与阶段性收益**
   - 用 suite 级指标、代表 workload 收益、backlog 收口状态来证明阶段目标已完成。
6. **与 Sniper 的对比：为什么 MineSim 仍是当前主线**
   - 既给出客观数字，也明确说明 Sniper 的参考价值与边界。
7. **当前路线的优点、缺点与风险**
   - 包括强依赖物理机 baseline、trace/ROI 污染、TLB 模型缺口、suite 泛化不足等问题。
8. **一期验收结论与二期建议**
   - 明确一期完成了什么，没有完成什么，以及为什么这些剩余问题更适合进入下一阶段。

### 0.2 这是否符合验收人想看的风格

符合，而且比“按 round 堆实验日志”更适合验收人阅读，原因有三点：

- **先讲目标与闭环，再讲 patch**：验收人更关心“项目是否建立了稳定推进机制”，而不仅仅是某几个局部修正。
- **先讲硬收益，再讲软收益**：既给出 CPI / cycles / branch.misses 的收敛数据，也解释为什么大量伪问题被排除本身就是项目收益。
- **把局限性单列**：一期验收通常不要求“问题全部解决”，但要求团队知道当前路线的边界、风险和二期入口。

因此，本报告采用的不是“实验记录风格”，而是更偏向 **技术验收 / 阶段复盘 / 研究总结** 的写法：

> 先说明项目把什么体系搭起来，再说明这个体系带来了哪些结果，最后说明这条路线的边界在哪里。

---

## 1. 项目目标与一期验收口径

本项目的一期目标，不是把 MineSim 在所有 workload、所有 PMU counter 上都调到与物理机完全一致，而是完成以下四件事：

1. 建立一条 **MineSim / perf / Sniper / CounterPoint** 的稳定验证闭环；
2. 在默认 5-workload 旧链路上形成可复现、可冻结的阶段基线；
3. 将误差改进流程从“看到偏差就改代码”升级为“先归因、再最小修、最后回归”；
4. 明确区分三类问题：
   - MineSim 真语义缺口；
   - trace / ROI 共性问题；
   - CounterPoint mapping / rule / solver artifact。

因此，一期验收的判断标准应当是：

- 是否已经形成了稳定的实验与审稿机制；
- 是否已经在主指标上拿到可验证的收敛收益；
- 是否已经把高风险误判大幅排除；
- 是否已经把剩余开放问题压缩到可清晰描述的二期 backlog。

从这个标准看，本项目一期已经达到了“**可以验收**”的程度：主线闭环已建立，关键误差已经显著下降，workflow 已经由盲修转入收口阶段。

## 2. 实验对象、基线与验证链路

### 2.1 实验对象

一期主线围绕如下组件展开：

- **MineSim**：当前主线仿真器，用于输出 `core.cycles`、`core.instructions`、`branch.misses`、cache、TLB 等观测；
- **perf baseline**：在物理机 `SPR 8457C` 上采集的真实 PMU，作为误差计算基线；
- **Sniper**：对照仿真器，用于与 MineSim 和 perf 做三方比较；
- **CounterPoint**：约束求解与 DAG 诊断工具，用于判定当前 MineSim 输出能否被模型解释；
- **DynamoRIO drmemtrace**：旧链路 trace 采集工具，一期默认仅允许使用旧链路，不切换到 release 链路。

### 2.2 默认 workload 集合

一期默认 suite 收敛为 5 个 workload：

| workload | 默认规模 | 主要用途 |
| --- | ---: | --- |
| `log_state` | `iter=1` | 混合回归点，检查整体副作用 |
| `graph_walk` | `iter=1` | branch + memory + dependency 组合瓶颈 |
| `codec_pipeline` | `iter=1` | backend / dependency / compute-bound 路径 |
| `branch_dense` | `iter=5` | branch predictor / branch recovery 隔离验证 |
| `cache_bench` | `iter=1` | cache hierarchy / writeback / store pressure |

这套 workload 组合不是随意拼接，而是围绕“**先 isolating，再 full suite**”的归因策略确定的。它的价值在于：先用干净 workload 验证一个局部假设，再决定是否允许回归整个 5-workload 集合。

### 2.3 一期正式结果目录

当前一期正式冻结结果目录为：

```text
/data00/yinhaolang/simulators/archsim/global/out/default_5workload_tripartite_oldtrace_20260527_rerun
```

在这一基线上：

- 默认 5-workload 旧链路整轮 wall time 约 `760.10s`；
- 旧链路已经验证可完整跑通；
- 后续 targeted 诊断与审稿均围绕这套 old-trace 基线开展。

## 3. 多 Agent 协作框架设计与落地

### 3.1 为什么要搭建多 Agent 框架

在 CPU 仿真器这类复杂系统里，如果让一个 agent 同时承担“跑实验、做解释、写 patch、判断 patch 是否有效”四个角色，极容易出现三类问题：

1. 看到误差就直接改模型，缺少反证；
2. 一轮同时引入多个修改，收益无法归因；
3. MineSim 计数语义改了，但 CounterPoint 没同步，导致“数字变近了、解释却变假了”。

因此，一期并没有把 AI 当成“自动写代码工具”，而是把它们组织成一个**可审稿、可回退、可验证**的工程系统。

### 3.2 Role 设计

一期采用固定的双 Agent 角色设计：

| 角色 | 定位 | 核心职责 | 约束 |
| --- | --- | --- | --- |
| Agent A | 执行者 / 实现者 | 编译、跑实验、收集 perf / trace / MineSim / Sniper / CounterPoint、提交最小 patch | 不得同时推进多个主假设；不得无证据大改 |
| Agent B | 审稿人 / 反方 / 归因者 | 审查假设、寻找反证、判断 patch 是否接受、要求 CounterPoint 同步 | 不得跟着 A 乐观推进；不得接受无 targeted validation 的 patch |

这种设计的核心价值不是“两个 agent 并行写更多代码”，而是：

> **一个 agent 负责推进，另一个 agent 负责反驳。**

其直接收益是：可以把“试错式开发”转化成“证据驱动式迭代”。

### 3.3 Harness 设计：botmux + 固定协议

一期的 harness 不是普通聊天，而是基于 `botmux` 的受控协作环境。它做了三件关键事：

1. **固定身份与 mention 路径**
   - 要求使用 `botmux send --mention` 在群里显式拉起对方 agent；
   - 保证 Agent A / Agent B 始终在同一会话线程中协作，而不是各自漂移。

2. **固定输出模板**
   - Agent A 每轮必须给出：`Hypothesis / Evidence / Patch / Changed files / Validation / Results / Question for B`；
   - Agent B 每轮必须给出：`Verdict / Why / What evidence is missing / Best next step / Approval scope`。

3. **固定控制规则**
   - 每轮只允许一个主要假设、一个主要 patch；
   - 不允许两个 agent 同时修改同一文件；
   - 必须优先复用已有 trace 和已有结果；
   - 只要 MineSim 语义变化，CounterPoint 必须同步修改。

这套 harness 的意义在于把 agent 从“自由发挥”约束成“有门禁的实验参与者”。

### 3.4 审稿门禁与 same-binary / same-trace discipline

一期工作流最关键的工程纪律有两条：

1. **没有 targeted validation，不允许 accepted**；
2. **所有 patch 都必须经过 same-binary / same-trace consistency check**。

第二条尤其重要。因为仿真器项目中最容易出现的伪结论是：

- 其实跑的是旧二进制；
- 其实换了 trace；
- 其实统计窗口不一致；
- 局部 workload 看起来变好，但 full suite 只是漂移。

通过 same-binary / same-trace discipline，一期把大量“实验噪声”挡在了 patch 评审之前。

### 3.5 CounterPoint 同步审稿机制

一期还有一条与普通代码项目不同的硬约束：

> **MineSim 的语义、counter 导出、timing 组合逻辑发生变化时，CounterPoint mapping 与 DAG / rules 必须同步修改。**

这条规则非常关键，因为一期后半程已经多次证明：

- 有些“严重 violation”其实不是 MineSim bug，而是 CounterPoint 没有跟上 MineSim 的解释口径；
- 如果只调 MineSim 数字而不校正 CP 解释路径，最终会得到“指标近了，但模型更假”的错误收敛。

因此，Agent B 在一期里不仅是“代码审稿人”，也是 **MineSim–CounterPoint 语义一致性守门人**。

## 4. 一期关键技术工作与方法论升级

### 4.1 从 patch 驱动转向证据驱动

一期真正的转折点，不是某一个 patch，而是方法论发生了变化：

- 从“直接看 full suite 排名”改成“先 isolating workload，再回归”；
- 从“看到 violation 就想修”改成“先判断它属于 MineSim / trace / CounterPoint 哪一类”；
- 从“凭一次结果乐观推进”改成“same-binary / same-trace / targeted validation”；
- 从“只修 MineSim”改成“MineSim 与 CounterPoint 双边共同收敛”。

这使得一期后半程的收益，不再只是把某个数字调近，而是把大量高风险误判排除掉。

### 4.2 MineSim 主语义修正：overlap / resolve

一期中被正式接受、且对主指标收益最大的 MineSim 本体修正，集中在 Round 8–11 的 overlap / resolve 语义修复：

- `resolve_cycle` 改为 branch-uop-specific 的 `last_branch_complete_cycle_`；
- branch recovery overlap 不再错误地长期为 `0`；
- `timing_mcw` 相关 violation 在关键 workload 上消失。

这不是简单的参数微调，而是修正了 branch / memory overlap 的核心时序解释路径。其直接收益包括：

- `graph_walk` CPI 误差：`+38.87% -> +13.07%`；
- `branch_dense` CPI 误差：`+32.46% -> +0.77%`；
- `timing_mcw` CounterPoint 组件在 `graph_walk`、`branch_dense` 上消失，并最终在 5/5 workload 上不再出现。

### 4.3 CounterPoint / 接口侧补全

一期另一个重要结论是：后半程很多高优先级问题并不是 MineSim 本体 bug，而是 CP 侧缺口。

典型例子有两个：

1. **orphan counter**
   - `timing.branch_memory_overlap_cycles` 没有 rule 消费，却被当成诊断对象，造成 `graph_walk` 上的 `-99%` 伪 violation；
   - 修复方向不是再改 MineSim，而是把这个 orphan counter 从 CP 模型侧清掉。

2. **精确 writeback anchor**
   - `cache_bench` 上 `cache.l2.misses +5.36` 的根因并不是 MineSim L2/L3 统计错误，而是 CP 缺少 `L2 writeback -> L3 access` 的精确解释路径；
   - 第一版宽代理规则虽然局部修好了 `cache_bench`，但会伤到 `graph_walk`，因此被审稿否决；
   - 最终接受的是以 `cache.l2.writebacks` 为 anchor 的精确补模方案。

这说明一期后半程最大的工程收益之一，是把“该修 MineSim 的”和“该修 CP 的”分开了。

### 4.4 运行环境与可复现性固化

一期还完成了一类容易被忽视、但对验收很重要的工作：运行环境稳定化。

例如：

- 在 `run_minesim_config_check.py` 中固化 `LD_LIBRARY_PATH`，解除 `GLIBCXX_3.4.29` blocker；
- 明确禁止 release trace 链路，把旧链路作为唯一默认主线；
- 控制整轮 full-suite 频次，优先复用已有 trace 和已有结果。

这些工作本身不一定直接降低 MAE，但它们显著提升了**复现性与工程稳定性**，这同样是一阶段验收的重要组成部分。

## 5. 一期结果：误差收敛与阶段性收益

### 5.1 Suite 级主指标演进

一期最重要的硬结果，是默认 5-workload 套件的主指标误差显著下降：

| 阶段 | CPI MAE | core.cycles MAE | branch.misses MAE |
| --- | ---: | ---: | ---: |
| workflow 前 formal | `16.67%` | `23.84%` | `26.61%` |
| Round 2 baseline refresh | `10.08%` | `18.80%` | `22.31%` |
| Round 11 overlap/resolve 修复 | `7.60%` | `18.80%` | `22.31%` |
| Round 36 阶段性收口 | `7.62%` | `19.34%` | `22.31%` |

如果排除 trace / ROI 污染最强的 `cache_bench`，结果更能反映 MineSim 本体收益：

| 阶段 | CPI MAE | core.cycles MAE | branch.misses MAE |
| --- | ---: | ---: | ---: |
| formal | `19.95%` | `15.85%` | `21.15%` |
| Round 2 | `11.03%` | `9.25%` | `12.71%` |
| Round 11 | `7.93%` | `9.91%` | `12.71%` |
| Round 36 | `7.93%` | `9.91%` | `12.71%` |

这组数据表明：

- MineSim 在一期内已经从“主指标明显偏离”收敛到“CPI 接近可冻结”；
- 主收益集中在 CPI 与 cycles 上；
- branch.misses 仍然是最 stubborn 的残差线，但其性质已经被更清晰地界定。

### 5.2 代表 workload 的收益

一期最具代表性的两个 workload 收益如下：

1. **branch_dense**
   - CPI 误差：`+32.46% -> +3.88% -> +0.77%`
   - 说明前期对 branch / memory overlap、resolve cycle 的修正确实打中了 branch 主线根因。

2. **graph_walk**
   - CPI 误差：`+38.87% -> +29.89% -> +13.07%`
   - 说明“先 targeted 验证，再回归 full suite”的策略在组合型 workload 上也有效。

此外，`timing_mcw` 从关键 workload 上消失，并在 Round 36 达到 5/5 workload 全消失，这进一步证明一期的收益不是“仅把数值调近”，而是 **解释路径真的被校正了**。

### 5.3 收口状态与冻结基线

到 Round 36 为止，一期已经达到以下状态：

- `branch_dense`、`graph_walk`、`codec_pipeline` 三条主线已基本完成诊断收口；
- `cache_bench` 的精确 writeback anchor 与 SQ Drain observation 已完成；
- Round 11 与 Round 36 全套 5-workload 结果精确匹配，确认 **零回归**；
- `L3 = L2 misses + L2 writebacks` 在 5/5 workload 上全局闭合；
- `timing_mcw` 在 5/5 workload 上不再出现。

这意味着一期已经形成了一个可冻结、可复用、可作为二期出发点的稳定基线。

## 6. 与 Sniper 的对比：为什么 MineSim 仍是当前主线

### 6.1 不能只看“点数胜负”

如果只看 25 个 counter 点里“谁离 perf 更近”的逐点胜负，Sniper 会赢更多点；但这不是最合理的比较口径。原因是：

- Sniper 在若干非主线计数器上只是“更不差”；
- `dtlb_load_misses` 的极端异常会严重放大 Sniper 的整体平均误差；
- 验收更关注的是主指标：`core.cycles`、`core.instructions`、`branch.misses` 与 CPI。

因此，一期采用更合理的几种口径比较 MineSim 与 Sniper：

| 统计口径 | MineSim | Sniper |
| --- | ---: | ---: |
| 全部 25 个 counter 点 | `50.138%` | `387.685%` |
| 去掉 `dtlb_load_misses` | `39.678%` | `50.355%` |
| 仅 `core.cycles + core.instructions + branch.misses` | `22.754%` | `26.878%` |
| 仅 `core.cycles + core.instructions` | `20.827%` | `24.040%` |

结论是：

> **在一期更重要的主指标上，MineSim 当前整体优于 Sniper。**

### 6.2 CPI 维度的对比

按 workload 看 CPI：

| workload | perf CPI | MineSim CPI | Sniper CPI | MineSim 误差 | Sniper 误差 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `log_state` | `0.7882` | `0.7596` | `0.9833` | `-3.627%` | `+24.748%` |
| `graph_walk` | `0.5282` | `0.7335` | `0.5715` | `+38.873%` | `+8.190%` |
| `codec_pipeline` | `0.5332` | `0.5074` | `0.7500` | `-4.842%` | `+40.655%` |
| `branch_dense` | `1.2996` | `1.7214` | `1.1001` | `+32.459%` | `-15.345%` |
| `cache_bench` | `0.7893` | `0.7611` | `0.4352` | `-3.570%` | `-44.858%` |

从 CPI 看：

- Sniper 在 `graph_walk`、`branch_dense` 这两个前端 / branch 主问题 workload 上更接近 perf；
- MineSim 在 `log_state`、`codec_pipeline`、`cache_bench` 上更稳定，也更适合作为当前主线迭代对象。

### 6.3 MineSim 相对 Sniper 的优势

一期可以明确给出 MineSim 相对 Sniper 的三点优势：

1. **在主指标上整体更接近验收口径**
   - 尤其是 `core.cycles + core.instructions` 这组最重要指标上，MineSim 优于 Sniper。

2. **更适合做可解释的误差归因**
   - MineSim 当前已经接入 CPI decomposition、MCW 可见/隐藏分解、per-PC branch 统计、visible memory breakdown；
   - 这使其更适合做“为什么错”的分析，而不仅仅是“离得近不近”的比较。

3. **与 CounterPoint 的双向收敛更成熟**
   - 一期已经证明 MineSim 与 CounterPoint 可以共同演进；
   - Sniper 则更像一个对照系，而不是当前主线上的可解释建模平台。

因此，一期的结论不是“Sniper 没有价值”，而是：

> **Sniper 适合作为局部参考物，但 MineSim 才是当前更值得继续投资的主线平台。**

## 7. 当前路线的优点、缺点与风险

### 7.1 当前路线的优点

一期已经证明这条路线至少有四个优点：

1. **可复现**：默认 5-workload 旧链路、正式结果目录、运行环境固化都已经建立；
2. **可审稿**：通过 Agent A / Agent B 分工，把 patch 接受条件制度化；
3. **可归因**：通过 targeted workload、CPI decomposition、CounterPoint，把误差拆成可讨论的路径；
4. **可冻结**：Round 36 已形成零回归阶段基线，可作为二期出发点。

### 7.2 当前路线的缺点

但一期也很清楚地暴露了当前路线的边界。

#### 缺点 1：强依赖物理机器作为 baseline，难以泛化

当前误差定义高度依赖真实物理机上的 `perf stat` 采样。这带来的问题是：

- baseline 与特定机器型号、微码、内核、系统状态绑定；
- 需要 CPU pinning、ASLR 关闭、本地 NUMA 分配等额外稳定化措施；
- 换机器后，基线口径与可比性都可能变化。

这意味着一期方法虽然适合“针对已知机器做高保真逼近”，但还不适合直接外推成“可跨平台泛化的自动校准框架”。

#### 缺点 2：强依赖 trace，且 trace/ROI 污染会直接影响判断

一期后半程已经多次证明：

- `graph_walk`、`codec_pipeline`、`cache_bench` 上的 instructions deficit 并非 MineSim 独有，而是 trace / ROI 共性问题；
- 一旦 trace 采样窗口与 perf 统计窗口不完全一致，CPI 和 cycles 的解读就会变复杂。

因此，当前路线虽然建立了 old-trace 主线，但它对 trace 质量和 ROI 一致性的依赖依然很强。

#### 缺点 3：默认 suite 规模仍有限，尚不足以证明广泛泛化

一期默认 suite 只覆盖 5 个 workload，这是基于 runtime、trace 成本和 isolating 价值做出的工程取舍。

优点是它足够稳，缺点是：

- `dep_chain`、`mlp_stream` 等更重 workload 尚未进入主线验收口径；
- 当前结论更适合描述“在已知 5-workload 集合上已经收口”，而不是“对所有单核 workload 都有效”。

#### 缺点 4：TLB / frontend_icache 等模型缺口仍未完成主线修复

一期后半程已经把主线问题压缩到较少数，但并不代表没有剩余技术债。

当前仍然存在：

- `mmu_tlb` 相关 gap；
- `frontend_icache` 成为 CounterPoint 统一 top violator；
- branch.misses 仍存在系统性高估残差。

这些问题已经被清楚定位，但尚不属于一期必须收完的范围。

### 7.3 当前最大的风险是什么

如果要用一句话概括当前路线的最大风险，那就是：

> **数值层面的拟合收益，仍然可能被 trace / ROI 与平台耦合性部分掩盖。**

换句话说，一期已经把“本地闭环”做得比较扎实，但离“低成本跨平台泛化”还有明显距离。

## 8. 一期验收结论与二期建议

### 8.1 一期验收结论

综合来看，一期可以给出如下结论：

1. **多 Agent 协作框架已经搭建完成并被证明有效**
   - role 设计、harness 设计、固定协议、审稿门禁、CounterPoint 同步机制均已落地。

2. **默认 5-workload old-trace 基线已经形成并冻结**
   - full suite 可稳定复现，Round 36 验证零回归。

3. **主指标已获得实质性收敛**
   - Suite CPI MAE：`16.67% -> 7.62%`；
   - `graph_walk` 与 `branch_dense` 等关键 workload 获得显著改进。

4. **项目价值不只在于“数字变近”**
   - 更重要的成果是：大量伪问题被排除，MineSim / trace / CounterPoint 三类问题被分离，后续工作不再需要盲修。

5. **MineSim 当前仍优于 Sniper，适合作为后续主线平台**
   - 尤其在 `core.cycles + core.instructions` 等更关键的验收指标上整体更优。

因此，本项目一期建议结论为：

> **建议通过验收。**

它已经完成了“一条稳定可审稿的仿真器精度迭代主线”的搭建，并在此基础上获得了可验证的主指标收益。

### 8.2 二期建议

二期更适合围绕以下方向继续推进：

1. **TLB / page-walk 语义补模**
   - 把 `mmu_tlb` 从已知 gap 推进到可验证建模项。

2. **frontend_icache / CP 模型缺口清理**
   - 将当前统一 top violator 从“已知问题”转成“已解释问题”。

3. **扩大 workload 覆盖面，验证泛化能力**
   - 逐步纳入更重 workload，或通过缩小常量后重新接入 `dep_chain`、`mlp_stream`。

4. **弱化对单台物理机器 baseline 的强依赖**
   - 引入更系统的跨机型验证、trace 对齐方法和稳定性评估指标。

5. **继续坚持多 Agent 审稿式迭代**
   - 一期已经证明 workflow 是有效收益来源，二期不应回到“单 agent 盲修”模式。

## 9. 附：一句话总结

如果要用一句话概括一期成果，可以表述为：

> 一期最大的成果，不是某一个 patch 把 MineSim 一次性修准，而是把仿真器精度改进从“看误差、猜问题、试 patch”，升级成了“先证伪、再最小修、最后回归”的多 Agent 审稿闭环，并在这条闭环上拿到了可验证的主指标收敛结果。
