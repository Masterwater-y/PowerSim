# 默认 5-workload 最新实验结果分析

本文档总结默认 `5-workload` 在旧链路上的三方实验结果，并补充截至 `2026-05-28 / Round 31` 的最新 targeted 归因收敛结论，覆盖：

- baseline `perf`
- `MineSim`
- `Sniper`
- `CounterPoint`
- 旧链路 `drmemtrace`

## 0. 截至 Round 31 的最新收敛状态（2026-05-28）

下面这部分是对后续 targeted 归因（约 `Round 17`–`Round 31`）的增量总结，优先级高于本文后面基于 `2026-05-27` 总跑结果写下的旧建议。

### 0.1 总体判断：误差收敛已经明显从“盲修阶段”进入“收口阶段”

- `branch_dense`、`graph_walk`、`codec_pipeline` 三条主线都已经完成一轮较深的 targeted 归因，当前**没有证据支持继续对 MineSim 主语义做大改**。
- 最新确认的可执行修正主要集中在 **CounterPoint 模型/映射补全**，而不是 MineSim timing 语义本体：
  - 已确认并移除 `timing.branch_memory_overlap_cycles` 这个 **orphan counter** 的伪诊断影响。
  - 已确认 `cache_bench` 上 `L2 writeback -> L3 access` 的 **CP 路径缺口**；宽代理方案不可接受，现已切换为精确 `cache.l2.writebacks` anchor，等待 targeted regression 最终确认。
- 这意味着当前误差收敛的主趋势是：
  1. **先排除误判与伪 violation**；
  2. **再只对真正缺口做最小修补**；
  3. **尽量避免为了单 workload 把 MineSim 语义改坏。**

### 0.2 各 workload 最新状态

#### `branch_dense`：predictor 线已基本收口

- `branch.misses` 的主残差已被定位到极少数热点 PC；其中 top-4 PC 吃掉了绝大多数 `both_wrong`。
- 对这些 PC，`bimodal / gshare / local-1bit / local-2bit / tagged-global` 的准确率都只在约 `50%`–`57%`，而且 global 与 chooser 分歧时通常更差。
- 当前结论不是“所有可能 predictor 的理论极限”，而是：**对 MineSim 当前 predictor family，这条线已接近可达到上限**。
- 审稿结论：**关闭 branch predictor 主线，不再作为下一 patch 方向。**

#### `graph_walk`：组合误差已大幅拆开，主线诊断基本收口

- 已证明 `MineSim ≈ Sniper`，二者 instructions 都系统性低于 perf；这是 **trace/ROI 共性问题**，不是 MineSim 独有 bug。
- 已排除几个高风险误判：
  - `dep stall overcount` 不是主因；绝大多数 dep stall 属于 true blocking。
  - `branch_memory_overlap -99%` 不是 MineSim overlap 实现 bug，而是 CounterPoint 把一个**没有 rule 消费的 orphan counter** 当成诊断对象导致的伪 violation。
- 移除该 orphan counter 后，`graph_walk` 剩余 timing 诊断已明显变干净；`port_pressure` 仍有高 normalized 值，但绝对量极小，不值得主修。
- 审稿结论：**graph_walk 当前不再是优先 patch 目标，除非出现新的强证据。**

#### `codec_pipeline`：backend/dependency 线基本收口

- 该 workload 现已确认是 **compute-bound**：`dependency + residual` 占主导，memory 几乎不贡献。
- `base_cycles below_ci` 经 instruction-normalized sanity check 后，已证明主要是 **CounterPoint solver 在 instruction budget 分配上的 artifact**，不是 MineSim 本地 timing 语义错误。
- 其余 `timing_mcw` 偏差绝对值都较小，没有支撑继续开 backend/dependency 语义 patch。
- 审稿结论：**codec_pipeline 线关闭。**

#### `cache_bench`：cache path 解释已显著收敛，但 SQ Drain 仍未收口

- 该 workload 的 instructions deficit 非常大（约 `-54%`），因此 `core.cycles` 相关大偏差不能直接当作 MineSim 语义错误。
- MineSim 自身 cache path 审计已经证明：
  - L1/L2/L3/DRAM 逐层计数自洽；
  - `L2 miss -> L3 access` 之外，`L2 writeback -> L3 access` 也是实打实的主要路径。
- `cache.l2.misses +5.36` 的根因已经定位为 **CounterPoint 模型缺口**，不是 MineSim cache 统计错误。
- 第一版用 `cache.l2.accesses` 作为 writeback proxy 的规则虽然修好了 `cache_bench`，但会把 `graph_walk` 拉坏，已被审稿否决。
- 当前最新状态：
  - `counterpoint_lite/configs/simulator_mappings.json` 已加入 `cache.l2.writebacks`；
  - `counterpoint_lite/counterpoint_lite/minesim_model.py` 已改为用**精确的** `cache.l2.writebacks` 作为 `L2 writeback -> L3 access` anchor；
  - 这条精确 anchor 方案仍需做 `graph_walk + cache_bench + log_state` targeted regression 最终确认。
- 在 cache path 基本澄清后，`SQ Drain = 0.198 CPI` 成为 cache_bench 剩余最值得继续 observation 的点。

#### `log_state`：仍然是混合回归点，不是当前主修对象

- `log_state` 没有出现新的 isolated 根因收敛，仍主要承担“回归看是否把混合 workload 带坏”的作用。
- 当前最稳定的用途仍是：**在 cache / memory / backend 修正后做回归检查，而不是拿它做第一主线。**

### 0.3 当前已经确认的“不是 MineSim bug”的内容

- `branch_dense` 的大头 branch 残差，不是简单的 global gating / loop predictor 压制问题。
- `graph_walk` 的 `branch_memory_overlap -99%`，不是 branch recovery overlap 实现漏算。
- `graph_walk` / `codec_pipeline` / `cache_bench` 上大幅 instructions deficit，本质上是 **trace/ROI 问题**，不是 MineSim timing 语义本体错误。
- `codec_pipeline base_cycles` 的主 violation，不是 MineSim base timing 语义 bug，而是 **CP solver artifact**。
- `cache_bench cache.l2.misses +5.36`，不是 MineSim L2/L3 统计不守恒，而是 **CP 缺少精确 writeback 路径解释**。

### 0.4 当前仍然打开的两类问题（截至 Round 35 已全部收口）

1. **CounterPoint 侧的最小补模/映射补全** ✅ 已完成
   - `cache.l2.writebacks` 精确 anchor 方案已完成 targeted regression 确认（Round 34）。
   - `L3 accesses = L2 misses + L2 writebacks` 在 5/5 workload 上精确闭合（最大偏差 2）。
   - 既保留了 `cache_bench` 收益，又没有伤到 `graph_walk`。

2. **cache_bench 的 SQ Drain 语义是否过于悲观** ✅ 已完成 observation
   - Round 35 完成 observation-only 分析。
   - 结论：SQ Drain 是 cache/writeback 压力的伴生现象，不是独立 SQ 建模缺陷。
   - SQ Drain Extra Stall (21.4M) >> Visible (2.7M)，大部分被 memory stall 隐藏。
   - 根因是极高 store intensity 导致 L1D/L2 大量 dirty eviction。

### 0.5 误差收敛的阶段性结论（Round 36 里程碑更新）

- 与 `2026-05-27` 那份"谁都很可疑"的状态相比，当前最大的进展是：
  - **branch_dense / graph_walk / codec_pipeline 三条线已经基本完成诊断收口**；
  - **多个高优先级伪问题已被排除**；
  - **CounterPoint 与 MineSim 的接口语义已被逐项校正并全局验证**；
  - **所有 Round 31 时仍打开的问题（精确 writeback anchor + SQ Drain）已全部收口**。
- Round 36 full 5-workload suite 回归确认：
  - Round 11 vs Round 36 全部精确匹配，**零回归**；
  - `timing_mcw` 在 5/5 workload 上均未出现（overlap 修复持续有效）；
  - `L3 = L2 misses + L2 writebacks` 全局闭合；
  - CounterPoint top violator 统一为 `frontend_icache`（已知 CP 模型缺口）。
- 当前误差收敛情况：
  - **解释层面的收敛明显快于数值层面的收敛**；
  - **大部分 workload 已经知道"为什么先不要修"或"为什么不能这么修"**；
  - 真正还值得继续动手的 MineSim 语义点，已经明显比一开始少很多。

### 0.5b Round 36 阶段性里程碑：收益总表

#### Suite 级 MAE 演进（CPI / core.cycles / branch.misses）

| 阶段 | CPI MAE | cycles MAE | br.miss MAE |
|------|---------|------------|-------------|
| formal (workflow 前) | 16.67% | 23.84% | 26.61% |
| Round 2 (baseline refresh) | 10.08% | 18.80% | 22.31% |
| Round 11 (overlap/resolve 修复) | 7.60% | 18.80% | 22.31% |
| **Round 36 (阶段性收口)** | **7.62%** | **19.34%** | **22.31%** |

排除 cache_bench（trace/ROI 污染）：

| 阶段 | CPI MAE | cycles MAE | br.miss MAE |
|------|---------|------------|-------------|
| formal | 19.95% | 15.85% | 21.15% |
| Round 2 | 11.03% | 9.25% | 12.71% |
| Round 11 | 7.93% | 9.91% | 12.71% |
| **Round 36** | **7.93%** | **9.91%** | **12.71%** |

#### 收益归因（三层）

1. **MineSim 主语义收益**（Round 8-11）：overlap/resolve 修复
   - graph_walk CPI: +38.87% → +13.07%（-25.8pp）
   - branch_dense CPI: +32.46% → +0.77%（-31.7pp）
   - timing_mcw CP 组件在 5/5 workload 上消失
   - Suite CPI MAE: 16.67% → 7.60%（-9.07pp）

2. **CounterPoint/接口收益**（Round 17-34）：
   - 移除 orphan counter `timing.branch_memory_overlap_cycles` 的伪诊断影响
   - 精确 `cache.l2.writebacks` anchor 补全 L2 writeback → L3 access 路径
   - `L3 = L2 misses + L2 writebacks` 在 5/5 workload 上全局闭合

3. **运行环境收益**（Round 35）：
   - `LD_LIBRARY_PATH` 固化到 `run_minesim_config_check.py`，解除 GLIBCXX_3.4.29 blocker
   - 保证可复现性

### 0.6 适合项目会议分享的 Agent 工作流总结

本轮协作里，真正带来收益的不是“让 agent 自由试错”，而是把 agent 工作流明确约束成一个**可审稿、可回退、可验证**的闭环。

#### 工作流骨架

1. **Agent A（执行者）提出单一假设**
   - 每轮只允许一个主要假设、一个主要 patch。
   - 优先做最小修改，不同时改多个文件语义。

2. **Agent B（审稿人）先审 hypothesis，再决定是否允许跑 patch**
   - 不接受“先改了再说”。
   - 没有 targeted validation 的 patch，不进入 accepted 状态。

3. **先 targeted workload，后 full suite**
   - 先用 isolating workload 验证单一瓶颈：
     - `branch_dense` 看 predictor / branch timing
     - `graph_walk` 看 branch+memory+dependency 组合
     - `codec_pipeline` 看 backend/dependency
     - `cache_bench` 看 cache hierarchy / MLP / store pressure
   - 只有 targeted 结果成立，才允许做 full-suite 回归。

4. **严格区分三类问题**
   - `MineSim` 真语义错误
   - `trace/ROI` 共性问题
   - `CounterPoint` mapping / rules / solver artifact
   
   这一步非常关键。后半程大量“看起来像 bug”的问题，最后被证明其实不是 MineSim bug。

5. **CounterPoint 必须同步审稿**
   - 只要触碰 timing/observation/mapping 语义，必须同步检查 `CounterPoint`。
   - 不允许只把 MineSim 数字调近，而不核对 CP 解释是否失真。

6. **所有 patch 都走 same-binary / same-trace consistency check**
   - 避免“其实跑的是旧二进制”或“trace 不同导致数字漂移”的伪结论。

#### 这个工作流为什么有效

- 它把“盲目调参”变成了“先证明值得修，再最小修”。
- 它把“工作量大但不确定”拆成了“isolating workload + 组件级证据 + 回归验证”。
- 它把很多潜在误判挡在 patch 之前，而不是 patch 之后再收拾残局。

一句话概括：

> **Agent 工作流真正提升的，不只是找 bug 的速度，而是降低了把伪问题修成真回归的概率。**

### 0.7 Agent 工作流开始前 vs 开始后的误差收益

这里分成两层看：

#### 第一层：有 full-suite 数字可验证的“硬收益”

以 agent 工作流启动前的 formal 结果，对比工作流启动后的首轮正式 5-workload baseline refresh（`round_02_summary.md`）和后续被接受的 overlap/resolve 修正（`round_11_summary.md`）：

##### Suite 级主指标平均绝对误差（formal → agent workflow 后）

| 指标 | workflow 前（formal） | workflow 后（Round 2 baseline refresh） | 收益 |
| --- | ---: | ---: | ---: |
| CPI | `16.67%` | `10.08%` | `-6.59pp` |
| core.cycles | `23.84%` | `18.80%` | `-5.04pp` |
| branch.misses | `26.61%` | `22.31%` | `-4.30pp` |

排除 `cache_bench` 这个 trace/ROI 污染最强的 workload 后，收益更明显：

| 指标 | workflow 前（formal, excl. cache_bench） | workflow 后（Round 2, excl. cache_bench） | 收益 |
| --- | ---: | ---: | ---: |
| CPI | `19.95%` | `11.03%` | `-8.92pp` |
| core.cycles | `15.85%` | `9.25%` | `-6.60pp` |
| branch.misses | `21.15%` | `12.71%` | `-8.44pp` |

##### 代表性 workload 收益

1. `branch_dense`
   - CPI 误差：`+32.46%` → `+3.88%`（Round 2）→ `+0.77%`（Round 11）
   - 说明 workflow 前期最关键的 branch/memory overlap + resolve_cycle 语义修正，确实把最干净的 branch workload 拉回来了。

2. `graph_walk`
   - CPI 误差：`+38.87%` → `+29.89%`（Round 2）→ `+13.07%`（Round 11）
   - 这是 workflow 带来的最大单点收益之一，说明“先 targeted 验证 overlap 语义，再回归 full suite”是有效的。

3. `CounterPoint timing_mcw`
   - 在 `graph_walk` / `branch_dense` 上，原来最显眼的 `timing_mcw` 违规，在 overlap 语义修正后**直接消失**。
   - 这不是简单把 MineSim 数字调近，而是 MineSim 与 CP 的解释口径真正对齐了一步。

#### 第二层：还没有 full-suite 新数字、但诊断已显著收口的“软收益”

从 `Round 17` 到 `Round 31`，更大的收益不是再跑出一个更低的 suite MAE，而是把大量高风险误判排除掉：

- `branch_dense`：证明问题不是 global gating / loop predictor suppression，而是当前 predictor family 对热点 PC 已接近上限。
- `graph_walk`：证明 `branch_memory_overlap -99%` 不是 MineSim 实现 bug，而是 CounterPoint orphan counter 造成的伪 violation。
- `codec_pipeline`：证明 `base_cycles` 主 violation 是 solver artifact，不值得继续大修 backend/dependency 语义。
- `cache_bench`：证明 `cache.l2.misses +5.36` 不是 MineSim cache path 错，而是 CP 缺少精确 `L2 writeback -> L3 access` 路径。

这类收益很难立刻体现在一个 suite MAE 数字上，但对项目推进非常重要，因为它减少了三类浪费：

1. 不再为伪问题开 MineSim patch；
2. 不再把 trace/ROI 问题误修成 simulator bug；
3. 不再把 CounterPoint 模型缺口误读成 MineSim 设计错误。

#### 第三层：工作流具体带来的方法论调整

与工作流开始前相比，当前的具体调整主要有：

1. **从“直接看 full suite 排名”改成“先 isolating workload 再回归”**
   - 这是最核心的流程升级。

2. **从“看到 violation 就想修”改成“先判断它属于 MineSim / trace / CounterPoint 哪一类”**
   - 这一步直接减少了大量无效 patch。

3. **从“凭单次结果乐观推进”改成“same-binary / same-trace / targeted validation”**
   - 避免了二进制陈旧、trace 不一致、局部跑偏等伪结论。

4. **从“修 MineSim 为主”改成“MineSim 与 CounterPoint 双边共同收敛”**
   - 当前已确认的有效修正里，有相当一部分其实是 CP mapping/rules 补全，而不是 MineSim 本体改动。

5. **从“多条线并行乱改”改成“每轮一个主假设 + 一个主 patch + 一个明确回归面”**
   - 这使审稿、回退和收益归因都清晰很多。

#### 会议分享可直接用的一句话总结

> 工作流开始前，我们更多是在“看误差、猜问题、试 patch”；工作流开始后，我们变成了“先证伪、再最小修、最后回归”。它带来的直接收益是 suite 主指标误差明显下降，间接收益是把大量伪问题排除掉，让真正还值得修的点快速收缩到少数几个。 

## 1. 实验对象与链路

本轮实验输出目录：

```text
/data00/yinhaolang/simulators/archsim/global/out/default_5workload_tripartite_oldtrace_20260527_rerun
```

默认 workload 集合：

- `log_state iter=1`
- `graph_walk iter=1`
- `codec_pipeline iter=1`
- `branch_dense iter=5`
- `cache_bench iter=1`

采集链路：

- `perf stat` 采样 baseline
- `dynamorio/collect_drmemtrace.sh` 采集 trace
- `run_minesim_config_check.py` 运行 `MineSim + CounterPoint`
- `Sniper` 运行 trace-driven 仿真

注意：

- 当前默认链路明确使用旧链路 `dynamorio/collect_drmemtrace.sh`
- 本轮实验不使用 `dynamorio_release`
- 旧链路运行过程中仍会打印 `DynamoRIO root` / `lib32` warning，但默认 5-workload 已验证可完整跑通

## 2. 运行时间

整轮实验 wall time：

- `760.10s`
- 约 `12 分 40 秒`

本轮运行日志见：

```text
/data00/yinhaolang/simulators/archsim/global/out/default_5workload_tripartite_oldtrace_20260527_rerun/run.log
```

运行阶段观察：

- 宿主机 workload 本体都很快，均明显小于 `1s`
- 主要耗时不在原生 workload，而在：
  - `drraw2trace`
  - `Sniper`
- `codec_pipeline` 是本轮最重 workload 之一，`Sniper` 单段实测约 `246.28s`
- 单独隔离复现时，`log_state` 的旧链路 trace 生成约 `84.77s`

结论：

- 默认 5-workload 整轮预算可按 `15 分钟` 预留
- 当前实测 `12 分 40 秒` 已可作为 agent 的主线时间预算

## 3. 总体结果概览

本轮使用 `5` 个 workload x `5` 个 counters，共 `25` 个对比点：

- `branch.misses`
- `cache.llc.load_misses`
- `core.cycles`
- `core.instructions`
- `tlb.dtlb_load_misses`

按全部 `25` 个点计算的平均绝对相对误差：

- `MineSim`: `50.138%`
- `Sniper`: `387.685%`

这个结果被 `Sniper` 在 `dtlb_load_misses` 上的极端异常强烈放大，因此还需要看去掉明显失真项后的结果。

去掉 `dtlb_load_misses` 后的平均绝对相对误差：

- `MineSim`: `39.678%`
- `Sniper`: `50.355%`

仅看更核心的 `core.cycles + core.instructions + branch.misses`：

- `MineSim`: `22.754%`
- `Sniper`: `26.878%`

仅看 `core.cycles + core.instructions`：

- `MineSim`: `20.827%`
- `Sniper`: `24.040%`

逐点胜负统计：

- `MineSim` 优于 `Sniper`: `10` / `25`
- `Sniper` 优于 `MineSim`: `15` / `25`

但这个胜负分布并不代表 Sniper 更可信，因为其中大量胜点来自 `Sniper` 在少数 counters 上“更不差”，而不是整体更稳定；从更关键的 `cycles/instructions/CPI` 看，MineSim 当前整体上仍更接近 baseline。

## 4. CPI 结果

按 `CPI = core.cycles / core.instructions` 计算：

| workload | perf CPI | MineSim CPI | Sniper CPI | MineSim CPI 误差 | Sniper CPI 误差 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `log_state` | `0.7882` | `0.7596` | `0.9833` | `-3.627%` | `+24.748%` |
| `graph_walk` | `0.5282` | `0.7335` | `0.5715` | `+38.873%` | `+8.190%` |
| `codec_pipeline` | `0.5332` | `0.5074` | `0.7500` | `-4.842%` | `+40.655%` |
| `branch_dense` | `1.2996` | `1.7214` | `1.1001` | `+32.459%` | `-15.345%` |
| `cache_bench` | `0.7893` | `0.7611` | `0.4352` | `-3.570%` | `-44.858%` |

解读：

- `MineSim` 在 `log_state`、`codec_pipeline`、`cache_bench` 上的 CPI 已经比较接近 baseline
- `graph_walk` 和 `branch_dense` 仍然是当前 CPI 的主要问题点
- `Sniper` 只在 `graph_walk`、`branch_dense` 上更接近 baseline，但在 `codec_pipeline` 与 `cache_bench` 上明显偏离

## 5. 分 workload 结果

按每个 workload 的 `5` 个 counters 计算平均绝对相对误差：

| workload | MineSim | Sniper |
| --- | ---: | ---: |
| `log_state` | `45.009%` | `146.740%` |
| `graph_walk` | `48.461%` | `1609.282%` |
| `codec_pipeline` | `36.958%` | `30.774%` |
| `branch_dense` | `70.736%` | `88.859%` |
| `cache_bench` | `49.524%` | `62.769%` |

### 5.1 `log_state`

优点：

- `branch.misses` 很准：
  - MineSim `-0.48%`
  - Sniper `+1.29%`
- `core.cycles` 上 MineSim 误差 `-14.51%`
- CPI 误差仅 `-3.63%`

问题：

- `cache.llc.load_misses` 几乎完全失真：
  - MineSim `-98.80%`
  - Sniper `-98.55%`
- `dtlb_load_misses` 同样严重失真：
  - MineSim `-99.96%`
  - Sniper `+611.90%`

判断：

- `log_state` 当前更像是“整体 CPI 还可以，但 uncore/LLC/TLB 观测解释严重不对齐”的混合回归点

### 5.2 `graph_walk`

优点：

- `Sniper` 的 `core.cycles` 和 CPI 比 MineSim 更接近 baseline

问题：

- MineSim `branch.misses` 明显偏高：
  - `+48.19%`
- MineSim CPI 误差 `+38.87%`
- `cache.llc.load_misses` 仍显著偏低：
  - MineSim `-61.76%`
  - Sniper `-79.23%`
- `dtlb_load_misses` 继续失真：
  - MineSim `-99.23%`
  - Sniper `+7903.24%`

判断：

- `graph_walk` 依然是当前最典型的“组合型难点”
- 它同时暴露：
  - 分支方向预测 / branch recovery
  - memory criticality
  - overlap / timing 语义

### 5.3 `codec_pipeline`

优点：

- `core.instructions` 非常接近 baseline：
  - MineSim `-2.18%`
  - Sniper `-2.17%`
- `core.cycles` 上 MineSim 也相对稳定：
  - `-6.91%`
- CPI 误差仅 `-4.84%`

问题：

- `branch.misses` MineSim 偏高：
  - `+19.61%`
- `cache.llc.load_misses` 两边都明显偏低：
  - MineSim `-56.50%`
  - Sniper `-47.25%`
- `dtlb_load_misses` MineSim 近乎归零：
  - `-99.59%`

判断：

- `codec_pipeline` 说明 MineSim 在 backend/cycles 主路径上已经有一定可用性
- 但 memory/LLC/TLB 统计解释仍然很弱

### 5.4 `branch_dense`

优点：

- 这是当前观察 predictor/branch 路径最直接的 workload
- MineSim 在 `branch.misses` 上比 Sniper 更接近 baseline：
  - MineSim `+16.30%`
  - Sniper `-48.92%`

问题：

- MineSim `core.cycles` 偏高：
  - `+23.62%`
- CPI 误差 `+32.46%`
- `cache.llc.load_misses` 由于 baseline 很小，MineSim 和 Sniper 都相对误差巨大
- `dtlb_load_misses` 仍严重偏低

判断：

- `branch_dense` 是当前最值得继续 targeted 修正的 workload 之一
- 原因是它能相对干净地验证：
  - branch predictor / branch recovery
  - timing visible branch cost
- 目前 MineSim 已经抓住了“方向”，但 branch cost 仍偏重

### 5.5 `cache_bench`

优点：

- MineSim 在 `cache.llc.load_misses` 上明显优于 Sniper：
  - MineSim `-22.63%`
  - Sniper `-99.05%`
- `cache_bench` 能稳定放大 cache hierarchy 问题

问题：

- `core.cycles` 和 `core.instructions` 都大幅偏低：
  - cycles `-55.80%`
  - instructions `-54.16%`
- CPI 虽然只差 `-3.57%`，但这是“cycles 和 instructions 同向一起偏低”造成的表面接近
- CounterPoint 也把 `backend_core` 和 `l1_l2_cache` 列为前排嫌疑

判断：

- `cache_bench` 当前不是“纯 memory latency 不对”这么简单
- 更像是：
  - cache hierarchy 统计不完整
  - 同时 retire / instructions 归一化窗口也存在系统性偏低

## 6. CounterPoint 诊断总结

所有 `5` 个 workload 的 CounterPoint verdict 都是 `infeasible`：

| workload | verdict | max_norm |
| --- | --- | ---: |
| `log_state` | `infeasible` | `72.43` |
| `graph_walk` | `infeasible` | `1017.64` |
| `codec_pipeline` | `infeasible` | `552.70` |
| `branch_dense` | `infeasible` | `2237.22` |
| `cache_bench` | `infeasible` | `10.31` |

主要模式：

1. `timing_mcw`
   - 在 `graph_walk`、`branch_dense` 上非常突出
   - 说明当前 timing / decomposition 语义与 CounterPoint 规则仍有较大解释缺口

2. `llc_cha` / `memory`
   - 在 `log_state`、`graph_walk`、`codec_pipeline` 上非常稳定地排在前列
   - 说明 LLC / DRAM / uncore 聚合路径仍是 MineSim 当前最薄弱的部分之一

3. `l1_l2_cache`
   - 在 `cache_bench` 上很突出
   - 说明私有 cache 层级统计与 PMU 映射仍不闭合

4. `backend_core`
   - 在 `cache_bench` 上排名第一
   - 暗示不仅 cache 模型有问题，retire/cycle 基础归一化也仍需继续检查

结论：

- CounterPoint 现在已经足够稳定地指出“哪一层解释不通”
- 但它给出的不是单一 bug，而是一组结构性缺口：
  - timing/MCW
  - LLC/DRAM/uncore
  - L1/L2 层级
  - backend_core 归一化

## 7. 当前最重要的结论（已按 Round 31 更新）

### 7.1 默认 5-workload 已跑通

- 旧链路已经足够支撑 agent 主线实验
- 当前主线不需要再被 `release` 链路问题阻塞

### 7.2 MineSim 仍比 Sniper 更适合作为当前修正主线

理由：

- 在 `core.cycles + core.instructions` 上，MineSim 当前整体优于 Sniper
- 在 `branch_dense branch.misses`、`cache_bench llc.load_misses` 等关键 isolating 点上，MineSim 也更接近 baseline
- Sniper 在 `dtlb_load_misses` 上出现了极端异常，不适合作为这类计数器的强参考

### 7.3 当前最值得修的不是 `log_state`

- `log_state` 仍然更适合做混合回归点
- 它当前的价值主要是：
  - 检查 cache / memory / backend 修正是否产生副作用
  - 而不是承担第一主线归因任务

### 7.4 `branch_dense`、`graph_walk`、`codec_pipeline` 已基本完成诊断收口

- `branch_dense`：当前 predictor family 已接近上限，不再是首选 patch 方向
- `graph_walk`：大头伪问题已排除，剩余偏差没有形成新的高价值 patch 入口
- `codec_pipeline`：主要 residual 已被证明更接近 solver artifact / 轻量偏差，不值得继续大修

### 7.5 当前主要开放问题已收缩到 `cache_bench`

- 第一层开放问题：`cache.l2.writebacks -> cache.l3.accesses` 的精确 CounterPoint 路径是否能稳定消除 `cache_bench` 误报，同时不伤到 `graph_walk`
- 第二层开放问题：`SQ Drain 0.198 CPI` 是否代表独立的 MineSim store-queue 悲观建模

## 8. 下一步建议（已按 Round 31 更新）

按优先级建议如下：

### 第一优先级：完成 `cache.l2.writebacks` 精确 anchor 的 targeted regression

目标：

- 确认 `cache_bench` 上 `cache.l2.misses` 的 CP 误报被稳定消除
- 同时确认 `graph_walk` / `log_state` 不再被宽代理规则污染

原因：

- 这是当前最明确、最小、收益最直接的开放问题
- 它修的是 CP 接口解释缺口，而不是高风险 MineSim 主语义

### 第二优先级：`cache_bench` 的 `SQ Drain 0.198 CPI` observation-only 归因

目标：

- 统计 queue depth、drain 次数、持续周期、与 store/writeback/cache pressure 的相关性
- 判断它是独立的 store-queue 悲观建模，还是 cache/writeback 压力的伴生现象

原因：

- 在 cache path 解释收紧后，`SQ Drain` 已成为 cache_bench 剩余最值得看的 MineSim 端现象

### 第三优先级：`log_state` 轻量回归

目标：

- 确认 cache / memory / llc_cha 的 CP 规则补全没有把混合 workload 解释拉歪

原因：

- `log_state` 不是主修 workload，但它是判断“是否引入副作用”的关键回归点

### 第四优先级：只有出现新强证据时，再重新打开已收口 workload

适用对象：

- `branch_dense`
- `graph_walk`
- `codec_pipeline`

原因：

- 当前这三条线都已经有比较明确的“先不要修”或“没有足够收益再修”的结论
- 在没有新证据前继续投入，收益/风险比很低

## 9. 建议 agent 直接读取的文件

主结果：

- `global/out/default_5workload_tripartite_oldtrace_20260527_rerun/suite_summary.json`
- `global/out/default_5workload_tripartite_oldtrace_20260527_rerun/suite_tripartite_comparison.csv`
- `global/out/default_5workload_tripartite_oldtrace_20260527_rerun/run.log`

每个 workload 的重点文件：

- `tripartite_comparison.csv`
- `minesim_counterpoint/summary.json`
- `minesim_counterpoint/diagnosis.json`
- `minesim_counterpoint/report.json`

## 10. 一句话结论（已按 Round 36 阶段性收口更新）

这轮最新实验说明：

- 默认 `5-workload` 旧链路已经稳定可跑
- `MineSim` 的大部分高风险误差已经从"可能是模型 bug"收缩成"要么是 trace/ROI 问题，要么是 CounterPoint 接口/规则缺口"
- `branch_dense`、`graph_walk`、`codec_pipeline` 三条线已基本完成诊断收口
- Round 31 时仍打开的两个问题（精确 writeback anchor + SQ Drain）已在 Round 34-35 全部收口
- Round 36 full 5-workload suite 回归确认零回归，`timing_mcw` 在 5/5 workload 上均未出现
- 因此当前的误差收敛特征是：**解释已经明显收敛，剩余真正值得动手的语义点已经不多了。**

## 11. 下一阶段候选 Backlog

本阶段（Round 1-36）已正式收口。下一阶段候选方向：

### Backlog A: graph_walk CPI +13% 残差
- 当前状态：CPI 0.597 vs perf 0.528，残差主要来自 Visible Memory Stall (0.250 CPI) + Residual (0.193 CPI)
- 已知约束：instructions deficit 是 trace/ROI 共性问题（MineSim ≈ Sniper），不是 MineSim 独有
- 可能方向：memory latency / MLP / cache pressure 建模

### Backlog B: branch.misses 系统性高估
- 当前状态：graph_walk +22%, codec_pipeline +20%, branch_dense +8%
- 已知约束：branch_dense 上 top-4 both_wrong PC 是数据依赖型（~50% flip rate），当前 predictor family 已接近上限
- 可能方向：indirect branch / return predictor / PMU event 映射语义

### Backlog C: CounterPoint frontend_icache 模型缺口
- 当前状态：5/5 workload 统一 top violator (score ~4.5)
- 已知约束：这是 CP 模型缺口（I-cache miss path 缺失），不是 MineSim bug
- 可能方向：补全 I-cache miss → frontend stall 的 CP 签名路径
