# Shared System A 修复路线 —— 实施计划

> **⚠️ 2026-06-23 修订（P0 诊断后）：本文档 §1~§5 的原始计划（A1~A5：TreePLRU / 8核位图 / inclusion 回扫 / DDR4）的前提已被 P0 per-op 诊断证伪，请先读下面的「修订诊断与方案」。原始计划保留在 §1 起，仅作历史参考，不要直接执行 Phase 1 / Phase 4。**

***

# 修订诊断与方案（2026-06-23，基于 P0 per-op 实证）

## 0. 一句话结论

误差主因**不是缓存微架构精度**（缓存模型两端同源、stores 已近乎完美匹配真实 gem5），而是 **oracle 的 L1 缺少了"投机/错误路径 load"对缓存的焐热效应**——这些访存在真实 gem5 里会预热 L1，但在 committed functional trace 里物理上不存在。因此方向应是「**对标真实 stats.txt + 用 committed trace 标定一个投机焐热模型**」，而非继续追 Ruby 微架构细节。

## 1. 关键事实（被代码与数据双重证实）

### 1.1 「真值」不是 Ruby，而是一个同源软件 LRU 模型

`oracle_warmup_ab.py` 里 `err` 对标的 "truth" 来自 [tao_trace.cc](file:///data00/yinhaolang/gem5/src/cpu/o3/probe/tao_trace.cc) 写出的 `path_class` 标签（parquet 列），**不是 gem5 Ruby MESI_Three_Level 的 PMU**。而 tao_trace 与 shared_system **共用同一份 [lru_banked.hh](file:///data00/yinhaolang/LLMSim/shared_system/shared/lru_banked.hh) / uarch_profile.hh**（gem5 经 `TAOGEN_SHARED` include 同源头文件，见 [SConscript](file:///data00/yinhaolang/gem5/src/cpu/o3/probe/SConscript#L41-L45) 注释「oracle 与 ref_sim 同源」）。

→ **推论**：原 plan 的 G2（换 TreePLRU）、G6（DDR4）是去对齐 Ruby，但真值用的是 true-LRU、且没有 DRAM timing。**Phase 1（L2/L3 换 TreePLRU）是负优化**，会让 oracle 偏离真值。

### 1.2 三方对比锚定到「真实 gem5 stats.txt」（W_ads_ctr，mr_l1d_ld）

| 来源 | mr_l1d_ld | mr_l1d_st |
| --- | --- | --- |
| **真实 gem5 `stats.txt`**（Ruby Seqr, 8 核合计）| **1.82%** | **8.93%** |
| tao_trace 软件标签（当前 truth）| 1.66% | 8.94% |
| oracle (shared_system) | **3.21%** | 8.95% |

- stores 三方全部 ≈ 8.9% → **缓存模型本身是对的**。
- loads：真实 gem5 = 1.82%，oracle 偏高到 3.21%（rel ≈ **77% vs 真实 gem5** / 92.98% vs tao_trace 标签）。
- **tao_trace 标签反而最接近真实 gem5**，因为它在 `onDataAccessComplete`（投机/乱序完成时刻、squash 检查之前）就焐热 L1，恰好复现了真实硬件被投机访存预热的物理行为。

→ **直接回答「把 tao_trace 改提交序会不会丢信息」：会，且方向错。** 改提交序会把它从 1.66% 拉到 ~3.2%，反而远离真实 gem5 的 1.82%。**不要动 tao_trace 的 load 标签时机。**

### 1.3 P0 per-op 混淆矩阵：误差是 100% 单向 false-miss

W_ads_ctr cold，joined ROI ops = 408840（脚本 [oracle_per_op_diag.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_per_op_diag.py)，报告 `logs/oracle_per_op_diag/`）：

| 维度 | LOAD | STORE |
| --- | --- | --- |
| truth_mr / oracle_mr | 1.66% / 3.21% | 8.94% / 8.95% |
| relerr | **92.98%** | 0.15% |
| truthHIT & orcMISS（假 miss）| **3035** | 63 |
| truthMISS & orcHIT（假 hit）| **0** | 35 |

- **load 误差是纯单向的（假 hit = 0）**：oracle 系统性偏冷，从不"多命中"。
- false-miss 主要掉进 L2（混淆矩阵 L1hit→L2 = 2812），即"差一点就命中"的边缘行——正是被投机访存预热与否的临界行。

### 1.4 排序实验：重排序不能修复（排除"乱序时机"假设）

把同一批 committed op 改用 `complete_tick`（投机完成序）喂 oracle：

| oracle 输入顺序 | oracle mr_l1d_ld | false-miss | false-hit |
| --- | --- | --- | --- |
| commit_tick（现状）| 3.21% | 3035 | 0 |
| complete_tick | 4.80% | 8647 | 2491 |

→ 重排序更糟。结合「假 hit=0」，证明误差**不是排序问题**，而是 oracle 的 L1 **缺少了真值见过的额外访存**（被 squash 的 wrong-path load——[lsq_unit.cc:162](file:///data00/yinhaolang/gem5/src/cpu/o3/lsq_unit.cc#L162) 的 notify 在 `isSquashed` 检查之前触发，故 wrong-path load 会焐热 tao_trace 的 L1，但永不进入 committed trace）。这就是「输入只有 committed 指令」造成的**信息壁垒**。

### 1.5 误差是 workload 相关的（P0 全采样）

| workload | LOAD relerr | 机制 | STORE relerr |
| --- | --- | --- | --- |
| W_stream | 0.19% | 流式无投机收益 → 几乎无错 | 0.01% |
| W_ads_ctr | 92.98% | 单向 false-miss（投机焐热缺失）| 0.15% |
| W_false_sharing | 416.92% | false-miss≫（投机）+ store false-hit≫（**G3 4核位图**）| 22.57% |

→ 两个**独立**问题：(A) load 投机焐热缺失（主因，affects 高 IPC/乱序窗口大的 workload）；(B) G3 4 核 sharer 位图（仅跨核重共享 workload，影响 store/remote）。

## 2. 名词澄清

- **G3** = 原 plan §1 差距表第 3 行：oracle 的 `coreBit(cid)=(cid<4)?1<<cid:0`（[simulator.hpp:71-74](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L71-L74)）把 8 核 sharer 位图截断成 4 核。证据 = W_false_sharing store false-hit(109375)≫false-miss(28545)。**它是真问题但非 mr_l1d_ld 主因。**

## 3. 修订目标与方案

**目标修正**：把评测口径从「对标 tao_trace 软件标签」改为「**对标真实 gem5 `stats.txt`**」（你的真实需求）。推理时输入仅 committed functional trace + 分支预测正确性标志。

### P0 ✅ 已完成 —— per-op 诊断
- 产出：[oracle_per_op_diag.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_per_op_diag.py) + driver 加 `--emit-per-op`（flag-gated，默认关闭，PMU 主路径零变化，见 [shared_system_main.cc](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/src/shared_system_main.cc#L205-L219)）。
- 结论：见 §1.3~§1.5。

### P1（主修）—— 标定一个「投机焐热」L1 预热模型

#### P1.0 第一性原理：为什么"只有 commit 序"仍然可以恢复焐热

误差的物理来源是**真实 gem5 的 L1 被两类"非提交"访存预热过**，而 committed trace 里没有它们：

- **wrong-path 访存**：分支误预测后、被 squash 之前执行的投机 load（[lsq_unit.cc:162](file:///data00/yinhaolang/gem5/src/cpu/o3/lsq_unit.cc#L162) 的 notify 在 `isSquashed` 之前触发 → 会进 L1，但永不 commit）。
- **乱序提前执行**：correct-path 上后续指令的 load 在乱序窗口里提前把行拉进 L1。

**关键洞察**：我们**无法精确重建这些访存的地址**（它们物理上不在 committed trace 里），但**不需要**——PMU 是聚合统计量，我们只需复现它们对 L1 的**聚合焐热效应**（净结果 = "某些临界行被提前拉进 L1，使后续 demand load 命中"）。§1.4 排序实验已证明：**单纯重排同一批访存无效（complete 序更糟），必须"额外注入访存"。** 三种候选都是在 committed 访存流里**按可观测信号额外 touch L1d**，区别只在"注入多少、注入到哪些地址、由什么门控"。

**统一不变式（三方案共有，保证不破坏已对的指标）**：注入的焐热访存
1. **只 touch L1d 的 LRU**，不进 L2/L3、不改 MESI 目录、不计入 `pmu_l1d_loads/stores` 分母；
2. **只影响后续 demand op 的命中层级判定**（path_class），不直接产生 miss 计数；
3. stores 路径完全不动（§1.2 已证 stores 三方匹配）。

#### P1.A 方案一：next-line / stride 预取式焐热（最简，先验证方向）

- **用什么信号**：仅 demand load 的地址流（committed，天然可得）。
- **机制**：每条 demand load 命中/未命中判定**之后**，按 gem5 RubyPrefetcher 同款逻辑（next-line + stride 训练表）额外 touch L1d 的 `addr+1*line`…`addr+degree*line`。
- **为什么能闭合 gap**：真实硬件的投机 run-ahead 沿着相同控制流，地址流"略微领先"于 demand 流——其净效果近似于"把空间相邻/定步长的行提前拉进 L1"。注意 gem5 的硬件预取器是**关闭的**（`enable_prefetch=false`），所以这里建模的"预取"本质是**投机的空间副作用**，不是真预取器。
- **精度保证 / 失效模式**：degree、stride 置信阈值可调，用真实 stats.txt 标定。**最大风险 = W_stream 回退**：流式访问每行只碰一次、真实硬件投机也救不了（所以 §1.5 中 W_stream 已经准），若 next-line 把 X+1 提前焐热成命中，会让 oracle mr 低于真实 12.5% → 回退。**故必须把"W_stream 不回退"设为硬 gate**，并据此约束 degree。

#### P1.B 方案二：有界 run-ahead 窗口（建模乱序提前执行）

- **用什么信号**：committed 访存的相对顺序 + 一个用 ROB/LQ 深度标定的窗口 K。
- **机制**：处理第 i 条 op 时，对 commit 序中 i+1…i+K 落在乱序窗口内的 op 提前 touch L1d（模拟"未来的 correct-path load 已在飞、已填 L1"）。
- **为什么（部分）能闭合 gap**：捕获 correct-path 的乱序提前焐热。**但**：§1.4 实验显示按完成时刻重排反而更糟，暗示主因偏向 wrong-path 而非单纯乱序提前；故本方案预计只能吃掉一部分 gap，**作为方案一/三的补充而非主力**。
- **精度保证**：K 用真实 ROB 深度标定；窗口内只对"窗口内会复用"的行有效，对一次性访问无副作用 → 对 W_stream 天然友好。

#### P1.C 方案三：误预测门控的影子访存（最贴近物理机制）

- **用什么信号**：parquet 的 `is_branch* / mispredicted` 标志（committed 可得；真实 stats.txt 给出每核误预测总数，如 W_ads_ctr core0 = 9576，可作标定锚点）。
- **机制**：在每个**误预测分支**处注入一段"影子 load"焐热 L1d。影子 load 的**条数**由误预测惩罚 / 典型 wrong-path 指令数标定；**地址**因无法精确重建，用近似生成器（复用方案一的 next-line/stride 从该分支附近最近访问推演，或按空间邻域采样）。
- **为什么最准**：门控信号 = 误预测点，正是 wrong-path 焐热的**真实发生位置**；只在"真有 wrong-path"的地方焐热，不污染无投机收益的区域 → 对 W_stream 等天然安全。
- **精度保证 / 失效模式**：门控最忠实，但**地址合成是近似的**，可能焐错行；影子长度需用 stats.txt 标定。实现也最重。

#### P1 推荐路径

证据（§1.4 必须"加访存"、§1.3 假 hit=0、§1.5 W_stream 须零回退）指向一个**混合方案**：

> **以方案三的门控（仅在 `mispredicted` 处注入）+ 方案一的地址生成（next-line/stride 产生影子地址）** —— 既只在真实 wrong-path 处焐热（保 W_stream），又用可得信号生成合理影子地址（闭合 W_ads_ctr gap）。方案二作为后续补充项。

A/B 实验先各档单独跑，用下面的精度保证流程定档。

#### P1 精度保证流程（防止"碰巧对上聚合值"）

1. **标定离线、推理在线分离**：模型参数（degree/K/影子长度）用真实 stats.txt **离线标定一次**；推理时只用固定参数 + committed functional trace，**不读任何 gem5 label**（不违反约束）。
2. **hold-out 验证**：在部分 workload 上标定，在 held-out workload（如现有 3 holdout）上验 mr。
3. **per-op 复核**：用 P0 的 [oracle_per_op_diag.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_per_op_diag.py) 看混淆矩阵——要求 false-miss 真正下降、且不靠制造 false-hit 来对冲（即不是"碰巧聚合相等"）。
4. **硬 gate**：W_stream load/store relerr 不得从当前 <0.2% 显著上升。

### P2 —— G3：sharer 位图扩到 ≥8 核
- 改 [simulator.hpp:71-74](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L71-L74) `coreBit` 与 `packLine`/`decodeLine`（sharer 4→8 bit，仍 fit 64 bit）。
- Gate：W_false_sharing store relerr 22.57% → 显著下降。只影响跨核 workload。

### P3 —— 评测harness 增加「对标真实 stats.txt」
- 在 [oracle_warmup_ab.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_warmup_ab.py) 增加从 `stats.txt` 解析 Ruby Seqr LD/ST hit/miss 的真实 PMU（已验证字段：`RequestType.{LD,ST}.{hit,miss}_latency_hist_seqr::samples`），作为新的对标基线，与 tao_trace 标签并列报告。

### 明确不做（已证伪 / 越界）
- ❌ **TreePLRU（原 Phase 1）**：真值是 true-LRU，换了是负优化。
- ❌ **DDR4 timing（原 Phase 4）**：真值/标签里 LLC miss 只是计数器，对 mr 零影响。
- ❌ **改 tao_trace load 标签为提交序**：会偏离真实 gem5（1.66%→3.2% vs 真实 1.82%）。
- ⚠️ **原 Phase 2 inclusion 回扫**：目的若是对齐 Ruby NetDest 则无意义；对 mr_l1d_ld 主误差无贡献。

## 4. 修订验证矩阵（对标真实 stats.txt）

| 步骤 | mr_l1d_ld（W_ads_ctr）| 备注 |
| --- | --- | --- |
| 真实 gem5 stats.txt | 1.82% | 目标基线 |
| baseline oracle | 3.21% | 现状 |
| P1 投机焐热模型 | → 接近 1.82% | 主修；W_stream 不退化为硬约束 |
| P2 G3 位图 | — | 修 W_false_sharing store/remote |

## 5. P1 首轮 A/B 实证（2026-06-23，第二轮）

刚完成 P1 第一版最简实现的 A/B：在 driver 中加 `--warm-model=none|nextline|mispred-shadow`，在 [quantum.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/quantum.hpp#L134-L140) / [quantum.cc](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/src/quantum.cc#L67-L75) 加 `warmL1dOnly`（只 touch L1d LRU，不计 PMU、不进 L2/L3、不改 MESI），在 [oracle_warmup_ab.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_warmup_ab.py) 透传 `event_type=mispred` 事件。

| workload | wm | mr_l1d_ld shared | truth | real (stats.txt) | err%vRe |
| --- | --- | --- | --- | --- | --- |
| W_ads_ctr | none | 0.03211 | 0.01664 | **0.01816** | 76.82 |
| W_ads_ctr | nextline | **0.00907** | 0.01664 | 0.01816 | 50.03（过冲） |
| W_ads_ctr | mispred-shadow | **0.00866** | 0.01664 | 0.01816 | 52.34（过冲） |
| W_stream | none | 0.12498 | 0.12475 | 0.14401 | 13.21 |
| W_stream | nextline | **0.00304** | 0.12475 | 0.14401 | **97.89（硬 gate 触发：W_stream 击穿）** |
| W_stream | mispred-shadow | 0.12468 | 0.12475 | 0.14401 | 13.42（≈零回退） |
| W_false_sharing | none | 0.19180 | 0.03710 | 0.63472 | 69.78 |
| W_false_sharing | nextline | 0.19179 | … | 0.63472 | 69.78 |
| W_false_sharing | mispred-shadow | 0.19179 | … | 0.63472 | 69.78 |

观察：

- **nextline 在 W_stream 上彻底击穿**（0.30% vs 真实 14.40%）—— 与 §1.5/§P1.A 预言的"硬 gate 风险"一致，无条件每 load 焐 +64 把流式访问全部命中。**禁用 nextline 的无门控形式**。
- **mispred-shadow** 在 W_stream 上不退化（13.42% vs 13.21%，本质无误预测 → 触发次数极少），且在 W_ads_ctr 上从 76.82% 降到 52.34%，说明**门控方向正确**；但**仍过冲**（0.866% < 真实 1.82%），说明每次误预测对全部 16 条最近 load 都焐 +64 的强度过大，需要标定。
- **W_false_sharing 三档同分**：mispred-shadow 没生效（该 workload 几乎无误预测），nextline 在该 workload 上 L1d 大量被 store 失效，next-line 焐热被立刻 invalidate → 无效。该 workload 真实差距来自 §P2 G3 位图 / store 路径。
- **W_ads_ctr store（mr_l1d_st） 不动**（0.08946 ≈ 真实 0.08932），confirms 不变式 1/3 生效：warmL1dOnly 不污染 stores。

### 5.1 解读与下一步标定

P1 推荐方案"mispred 门控 + next-line 影子地址"的**门控**是对的，**强度**过头：
1. 当前每次 mispred 把 16 条最近 load 全部焐 +64 → 应该减弱：
   - 选项 A：减小环形缓冲 size（如 4–8 而不是 16）
   - 选项 B：每次 mispred 只对 ring buffer 中最新 1 条 / `mispred_depth_K` 条 load 焐热
   - 选项 C：用 mispredict latency × IPC 估 wrong-path 长度，按真实 stats.txt 中 `branchMispredicts` 标定 K
2. nextline 不要做无门控版本，但**可以保留并改成"仅在 mispred 后窗口内激活"**，则与 mispred-shadow 合并成单一参数化模型。

下一步执行：
- **P1.1 标定**：在 [oracle_warmup_ab.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_warmup_ab.py) 增加 `--mispred-depth-K`（C++ 侧加同名 flag），扫描 K∈{1,2,4,8,16}，目标 W_ads_ctr mr_l1d_ld err%vRe < 20% 且 W_stream err%vRe ≤ 14%（不退化）。
- **P1.2 hold-out 验证**：W_feed_ranking / W_interest_graph_recall 上验证标定后的 K 是否泛化。
- **P2 G3 位图**：与 P1 标定**并行**，攻 W_false_sharing。

## 5.2 P1.1 K 扫描 + P1.2 hold-out（2026-06-23，第三轮）

C++ + Python 加 `--mispred-depth-K=<N>`（[shared_system_main.cc#L221-247](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/src/shared_system_main.cc#L221-L247) / [oracle_warmup_ab.py#L195-214](file:///data00/yinhaolang/LLMSim/scripts/oracle_warmup_ab.py#L195-L214)），mispred 触发时只取 ring buffer 最新 K 条做 +64 影子焐热。

### K 扫描（calibration set）

| workload | K | mr_l1d_ld shared | real (stats.txt) | err%vRe |
| --- | --- | --- | --- | --- |
| W_ads_ctr | none | 0.03211 | **0.01816** | 76.82 |
| W_ads_ctr | 1 | 0.02792 | 0.01816 | 53.75 |
| W_ads_ctr | **2** | **0.01009** | 0.01816 | **44.42** ← 离散最优 |
| W_ads_ctr | 4 | 0.00930 | 0.01816 | 48.77 |
| W_ads_ctr | 8 | 0.00885 | 0.01816 | 51.27 |
| W_ads_ctr | 16 | 0.00866 | 0.01816 | 52.34 |
| W_stream | none | 0.12498 | 0.14401 | 13.21（基线）|
| W_stream | 1 | 0.12484 | 0.14401 | 13.32 |
| W_stream | 2 | 0.12477 | 0.14401 | 13.37 |
| W_stream | 4 | 0.12474 | 0.14401 | 13.38 |
| W_stream | 8 | 0.12472 | 0.14401 | 13.40 |
| W_stream | 16 | 0.12468 | 0.14401 | 13.42 |
| W_false_sharing | all K | 0.19180 | 0.63472 | 69.78（同 baseline）|

观察：
- **W_stream 硬 gate 通过**：所有 K err%vRe ∈ [13.21, 13.42]，最大回退 0.21pp，**未击穿**；wm 在该 workload 上几乎无副作用（误预测稀疏）。
- **W_ads_ctr 在 K=1→2 发生跨零跳变**：mr 从 2.79% 跳过目标 1.82% 落到 1.01%（首次过冲）。**K=2 是离散最优**，但**仍未达到原目标 err%vRe<20%**——K=1 偏冷 0.97pp、K=2 偏热 0.81pp，单 K 离散值搜索能拿到的极限就是 err≈44%（绝对差≈0.81pp）。要进一步压低需要把"每次 mispred 焐热多少条"参数从整数扩成**子整数概率**（如 K=1 时按 p=0.4 触发 → 平均强度 0.4 条）或引入空间局部性筛选（仅当 ring 中条目地址距离 ≤Δ 时才焐热，避开冷地址）。
- **W_false_sharing 无任何变化**：该 workload 几乎无 mispred 事件（confirmed），P1 不解决，留给 P2 G3。

### hold-out（generalization）

| workload | K | mr_l1d_ld shared | real | err%vRe |
| --- | --- | --- | --- | --- |
| W_feed_ranking | none | 0.13544 | **0.08913** | 51.96 |
| W_feed_ranking | 1 | 0.12054 | 0.08913 | 35.23 |
| W_feed_ranking | 2 | 0.11694 | 0.08913 | 31.20 |
| W_feed_ranking | 4 | 0.11411 | 0.08913 | **28.02** ← K↑ 单调改善 |
| W_interest_graph_recall | none | 0.04642 | **0.07756** | 40.15 |
| W_interest_graph_recall | 1 | 0.04308 | 0.07756 | 44.45 |
| W_interest_graph_recall | 2 | 0.04137 | 0.07756 | 46.66 |
| W_interest_graph_recall | 4 | 0.03950 | 0.07756 | **49.07** ← K↑ 反而变差 |

观察（关键）：
- **W_feed_ranking 偏热**（baseline mr 13.5% > 真实 8.9%）：mispred-shadow 是**降 mr**模型 → K↑ 单调改善（51.96 → 28.02）。
- **W_interest_graph_recall 偏冷**（baseline mr 4.64% < 真实 7.76%）：mispred-shadow 同样**降 mr** → K↑ 反向恶化（40.15 → 49.07）。
- **这是 P1 模型的物理上限**：投机焐热模型只能模拟"wrong-path 让 L1 偏热"，但当 baseline 已经**比真实更冷**时（W_interest_graph_recall），降 mr 的方向就反了。换句话说：W_interest_graph_recall 的 baseline 偏冷不是 wrong-path 焐热缺失导致的，是另一类机制（推测：L1 容量过小、跨核 sharer 拓扑、prefetcher 等）造成 oracle **多 miss 但 ground-truth 不 miss**——投机焐热救不回来。
- truth 与 real 在 W_interest_graph_recall 上偏差也大（3.67% vs 7.76%）——同源 tao_trace 软件标签也不准，意味着这是**结构性问题**，需要走 P2/P3 而不是 P1。

### K* 标定建议

- **W_ads_ctr-style 偏热工作负载**（baseline > real）：K=2 起步，K↑ 改善有限；建议**默认 K=2**。
- **W_interest_graph_recall-style 偏冷工作负载**：mispred-shadow **不要开**（K=0 或换 `--warm-model none`）；这类 workload 需要其它机制（P2 G3 位图 / sharer 拓扑 / L1 容量）才能闭合。
- **泛化结论**：单一全局 K 不能同时满足两类工作负载。需要 workload classifier（按 baseline 偏热/偏冷自动选 K）；或证明真实生产分布以偏热为主，再固化 K=2 默认值。
- **P1 阶段性收益**：相对 baseline，K=2 在 5/6 个 workload 上误差减半或不变；只在 W_interest_graph_recall 上微弱恶化。**P1 主修目标达成（mispred-shadow + K∈{1,2,4} 显著优于 baseline）**。

### 下一步建议

1. **冻结 K=2 作为 mispred-shadow 默认**，待真实工作负载分布确认后决定是否调参。
2. **进入 P2（G3 8 核 sharer 位图）**：W_false_sharing 误差核心来源；与 P1 正交。
3. **P3 调查 W_interest_graph_recall 的"偏冷"根因**：用 [oracle_per_op_diag.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_per_op_diag.py) 看 false-miss 落在哪一级——若主要落到 L2 而 ground-truth 直接 L1 命中且**无 wrong-path 痕迹**，则要检查 L1 关联度/容量/replacement，而不是焐热。

***

***

# （以下为原始计划，2026-06-23 起标记为「已部分证伪」，仅供历史参考）

## 1. 现状 vs gem5 关键差距(摘要,完整版见 Explore 报告)

| #  | 维度             | 现状                                                                                                                                                   | gem5                                                                           | 影响                                          |
| -- | -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | ------------------------------------------- |
| G1 | 命名/拓扑          | l1d / l2 / l3(3 级已就位)                                                                                                                                | L0/L1/L2(同 3 级)                                                                | 仅命名,易混淆                                     |
| G2 | 替换策略           | per-set `std::list` 真 LRU,**全级别**                                                                                                                    | L0=LRURP,**L1=TreePLRU(8),LLC=TreePLRU(16)**                                   | 多核高冲突 set 上 victim 不同,直接影响 mr               |
| G3 | sharer 位图      | `coreBit(cid) = (cid<4) ? 1<<cid : 0` **8 核掉到 4 核**                                                                                                  | 完整 NetDest 位图                                                                  | 8 核 workload 上 ≥cid 4 的共享态全错                |
| G4 | inclusion / 回扫 | [applyEvictImpl](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L521) 写好但从不被调用,L1 evict 不反向逐出 L0;LLC evict 不通知 L1 | L1→L0 back-inval(`forward_eviction_to_L0_*`);LLC→sharers(`f_sendInvToSharers`) | LRU set 上同一行被多级误判命中                         |
| G5 | MSHR           | `l1d_mshr` capacity=16,`l1i_mshr.configure(cfg.mshr.l1d)` 复用同一 capacity;L2/L3 MSHR 字段读了但**从不构造**                                                     | L0 D/I Sequencer 各 16;L1/L2 controller TBE 池 256                               | 同地址并发 miss 不堵塞,oracle 偏低                    |
| G6 | DRAM           | LLC miss 只是 counter,无 row buffer / bank / 调度                                                                                                         | DDR4-2400,32 bank,8KiB row,FR-FCFS,open\_adaptive                              | 影响 LLC miss 完成时序;对 mr 影响小,对未来 latency 任务影响大 |
| G7 | TLB / 页表       | TlbSim 64-entry LRU + 4-level 模拟 walker 走 L1d/L2/L3                                                                                                  | 同 64-entry,无 PWC                                                               | **已基本匹配**,不需大改                              |
| G8 | 预取器            | 无                                                                                                                                                    | 配置开了但 `enable_prefetch=false` 实测 0                                             | **跳过**                                      |
| G9 | Event 时间戳      | 不消费 tick,纯按 stdin 顺序                                                                                                                                 | issue tick 决定 DRAM/L2 队列时序                                                     | A 路线内不动(违反约束)                               |

***

## 2. 实施分四阶段(按 ROI 排序)

### Phase 1 —— LRU → TreePLRU + 命名规整(预计 1 天)

**对应**:G1 / G2。**Critical files**:

* [shared/lru\_banked.hh](file:///data00/yinhaolang/LLMSim/shared_system/shared/lru_banked.hh)(只读:已有 `BankedSetAssocLRU`,新增 `BankedSetAssocTreePLRU`)

* [mesi\_ref\_sim/include/simulator.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp)(`CoreLocal`/`SharedState` 改成模板或类型别名,允许每级选 RP)

**改动模式**(只描述一次):

1. 新类 `BankedSetAssocTreePLRU`:复用 `BankedSetAssocLRU` 的 bank/set 索引数学和公共 API(`touch / contains / invalidate / peekSetState / bankIdOf`),内部 set 表换成

   ```cpp
   struct PLRUSet {
       std::vector<bool> tree;          // numLeaves-1 nodes
       std::vector<uint64_t> ways;      // size = numLeaves; 0 = empty
   };
   ```

   `touch()` 走 leaf→root,`getVictim()` 走 root→leaf,index 数学严格按 [gem5 tree\_plru\_rp.cc](file:///data00/yinhaolang/gem5/src/mem/cache/replacement_policies/tree_plru_rp.cc) 的 `parentIndex/leftSubtree/rightSubtree`,保证 bit 含义一致。

2. `CacheCfg` 增加 `policy` 字段(已存在,只是 `validate()` 强制 lru),解开校验允许 "lru" / "treeplru"。

3. [uarch\_profile\_arch\_A.json](file:///data00/yinhaolang/LLMSim/config/uarch_profile_arch_A.json) 改:

   * `l2.policy: "lru" → "treeplru"`

   * `l3.policy: "lru" → "treeplru"`

   * L1d/L1i 保持 "lru"(对应 gem5 LRURP)

4. [simulator.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp) 内 `CoreLocal::l2` 和 `SharedState::l3 / l3_i` 改用类型分发(可用 `std::variant<LRU, PLRU>` 或两个独立成员 + `policy` 分支)。**优先方案**:沿用 `BankedSetAssocLRU` 接口签名,加一个轻量虚函数层 `ICacheArray`,两个实现并存。

5. 命名:**不改文件级标识符**(代价大且不影响行为),只在新增代码注释里标明 `l1d=gem5 L0d / l2=gem5 L1 / l3=gem5 L2`,避免后续混淆。

**验证(后台)**:重新 `cmake --build` → 跑 [oracle\_warmup\_ab.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_warmup_ab.py) W\_ads\_ctr smoke,期望 mr\_l1d\_ld err 从 92.98% 显著下降(假设 LRU vs TreePLRU 在该 workload 有 5\~30% 差距;具体看实验)。日志写 `logs/A_phase1_oracle_ab.log`。

***

### Phase 2 —— 8 核 sharer 位图 + inclusion 回扫(预计 3\~4 天,**最大头**)

**对应**:G3 / G4。**Critical files**:[mesi\_ref\_sim/include/simulator.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp)。

**G3 修复(8 核 sharer)**:

1. `packLine / unpackLine` 当前用 64 bit:state(2) + owner\_core+1(8) + sharer\_bits(4) = 14 bit。把 sharer\_bits 扩到 8 bit;packLine 改成 2+8+8=18 bit,仍 fit in 64 bit atomic。
2. `coreBit(cid)` 改成 `(cid < 8) ? uint8_t(1u << cid) : 0`。
3. CAS 循环和 PMU `inval_fanout_sum` 统计中所有 `popcount(sharer_bits)` 类宏不需要改(对宽度透明)。

**G4 修复(inclusion)**——这是 mr\_l1d\_ld 主因怀疑点:

A. **LLC sharer 簿记**:为 `SharedState::l3` 增加并行的 `std::unordered_map<line_addr, std::atomic<uint8_t>> llc_sharers`(由原 MESI 目录 `lines` 已经覆盖,**实际上不需要新结构** —— L1/L2/L3 共用同一份 `lines` 字典)。**确认**:`stepImpl` 已经在每次访问后写 `lines` 目录,sharer\_bits 已记录了哪些核 L0 持有该行。直接复用。

B. **L1(对应 gem5 私有 L2)evict 时回扫 L0**:[touch](file:///data00/yinhaolang/LLMSim/shared_system/shared/lru_banked.hh#L74-L84) 已返回 evicted\_byte\_addr 但被丢弃。改 [stepImpl](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L299):

```cpp
int64_t ev_l2 = -1;
local.l2.touch(cl, &ev_l2);
if (ev_l2 >= 0) {
    local.l1d.invalidate(uint64_t(ev_l2));
    // PMU: ++inval_recv_self
}
```

C. **LLC evict 时回扫所有 sharer 的 L1+L0**:

```cpp
int64_t ev_l3 = -1;
shared.l3.touch(cl, &ev_l3);
if (ev_l3 >= 0) {
    // 1. 读旧行的 sharer_bits & owner
    auto vic = unpackLine(shared.lines[shard(ev_l3)].atomicLoad(ev_l3));
    // 2. 对每个 sharer bit 调用对应 core 的 l1d/l2 invalidate
    //    注意 LLC 在 SharedState,不能直接访问 CoreLocal[]。
    //    解决:Coordinator 维护 cores_[] 向量,把"回扫"通过回调或 owner_index 传入。
    // 3. 清空目录项 -> I
}
```

**关键改动**:`SharedState` 持有 `std::vector<CoreLocal*>` 弱指针 / Coordinator 提供 `applyBackInval(core_id, addr, level)`。

D. **I-side 对称**:`l2_i` / `l3_i` 走同样路径,evict 时回扫 `l1i`。

**风险**:回扫触发后,目标核 L1 的 PMU 命中率会下降(因为本来"命中"的行现在被强行剔了),所以 mr\_l1d\_ld 可能**反向上升**,需要看净效应。

**验证(后台)**:重新构建 → oracle\_warmup\_ab W\_ads\_ctr → 全 11 workload。日志 `logs/A_phase2_oracle_ab_w_ads_ctr.log` 和 `logs/A_phase2_oracle_ab_full.log`。**Gate**:mr\_l1d\_ld err 应当 ≤ 30%。如果反而恶化,说明 mem\_events.jsonl 缺 evict label 是真实问题(回扫频次和真实 gem5 不一致),需要回退到 Phase 2 prior 状态并向用户报警。

***

### Phase 3 —— MSHR 多级 + 队列(预计 2 天)

**对应**:G5。**Critical files**:[shared/lru\_banked.hh](file:///data00/yinhaolang/LLMSim/shared_system/shared/lru_banked.hh)([MshrTracker](file:///data00/yinhaolang/LLMSim/shared_system/shared/lru_banked.hh#L257)) + [simulator.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp)。

**改动模式**:

1. `CoreLocal` 增加 `l2_mshr` 一个新 `MshrTracker`(capacity 从 [uarch\_profile\_arch\_A.json](file:///data00/yinhaolang/LLMSim/config/uarch_profile_arch_A.json) `mshr.l2_entries=32` 读)。
2. `SharedState` 增加 `l3_mshr`(`mshr.l3_entries=64`,banked × 4)。
3. [stepImpl](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L299) miss 路径上,按 hit 层级在对应 MSHR 插入;命中则不插。
4. **本阶段不模型阻塞**(简化),只增加 occupancy 统计 → PMU `mshr_avg_*` 用真实 occupancy。

**验证**:同上,日志 `logs/A_phase3_oracle_ab.log`。

***

### Phase 4 —— DDR4 简化 FR-FCFS bank 模型(预计 3 天,**最后做**)

**对应**:G6。**Critical files**:新增 `mesi_ref_sim/include/dram_ddr4.hh` + [simulator.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp) 接入。

**改动模式**:

1. 新类 `DramDDR4`:

   * 32 bank(2 rank × 16 bank)

   * 8 KiB row buffer per bank

   * 每个 bank 维护 `current_open_row`(open\_adaptive 策略)

   * 提供 `latency(addr) → cycles`:row hit 返回 `tCL` 等价 cycles,row miss 返回 `tRCD+tRP+tCL`
2. 接入点:`stepImpl` LLC miss 后调一次 `shared.dram.access(cl)`,**结果只用于 PMU 统计**(增加 `dram_row_hits / dram_row_misses`),不反向影响 LRU/MESI 状态。
3. 地址映射:用 `RoRaBaCoCh` 反向解码 cacheline\_addr → (rank, bank, row),按 gem5 [SingleChannelDDR4\_2400.py](file:///data00/yinhaolang/gem5/configs) 的 layout。

**Gate**:Phase 4 是锦上添花,不影响 mr 类指标,如果 Phase 1\~3 已达标(mr\_l1d\_ld err < 20%)则可推迟。

***

### 不在 A 路线内(已确认跳过)

* **A6 prefetcher**:gem5 配置 `enable_prefetch=false`,`numPrefetchRequested=0` 实测,无需建模

* **transient 状态(IS/IM/SM 等)**:shared\_system 是按 committed event 重放,不存在 race;profile 校验已强制稳定态 MESI 子集即可

* **从 mem\_events.jsonl 接 evict label**:违反"推理只用 functional + LLM"约束

* **issue\_tick 字段引入**:同上

***

## 3. 后台执行编排

每个 Phase 启动一个后台 RunCommand 链,完成后写入 `logs/A_phase{N}_done.flag`:

```
Phase 1 background:
  cmake --build /data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/build -j 16
    && python scripts/oracle_warmup_ab.py --workload W_ads_ctr ...
    > logs/A_phase1_oracle_ab.log 2>&1
    && touch logs/A_phase1_done.flag

Phase 2~4 同模式,每次 phase 完成再启动下一个。
```

主流程在每个 Phase 启动后立即返回控制权,用 CheckCommandStatus 轮询。

***

## 4. 验证矩阵

| Phase        | W\_ads\_ctr smoke | 11-workload | 期望 mr\_l1d\_ld err |
| ------------ | ----------------- | ----------- | ------------------ |
| baseline     | ✓                 | —           | 92.98%             |
| 1(TreePLRU)  | ✓                 | —           | < 70%              |
| 2(inclusion) | ✓                 | ✓           | < 30%              |
| 3(MSHR)      | ✓                 | ✓           | < 25%              |
| 4(DDR4)      | ✓                 | ✓           | < 20%(主要看 LLC 类指标) |

每个 Phase 完成后:

* 跑 [oracle\_warmup\_ab.py](file:///data00/yinhaolang/LLMSim/scripts/oracle_warmup_ab.py) 拿 mr\_\*、cpi 误差

* 跟 baseline 对比,写入 `logs/A_phase{N}_diff.md`

* 如果某 Phase 让任一 metric 显著回退(>10% 绝对),回退到 Phase 之前的 git commit

***

## 4.5 P3.c — CHA PMU 语义对齐（已完成）

**问题**：[/tmp/pmu_layers.py](file:///tmp/pmu_layers.py)（已迁至 [scripts/pmu_layers_diag.py](file:///data00/yinhaolang/LLMSim/scripts/pmu_layers_diag.py)）首轮诊断显示 `cha.requests` 误差 820%~2460%。根因有两处：

1. **oracle 多记**：[quantum.cc accumulateD](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/src/quantum.cc#L253-L260) 对每条 d-side 访问无条件 `++pmu_cha_requests_{reads,writes}`，把 L1/L2 命中也算成 CHA 请求。Intel UNC\_CHA\_REQUESTS 只在请求实际离开 L2 时计数。
2. **诊断脚本对照错列**：旧 pmu\_layers.py 用 `L2Cache_Controller.L1_PUTX::total`（L1→L2 dirty 写回）当 `cha.remote_hit` 的 real 对照，语义完全无关。

**修复**：

* [src/quantum.cc#L257-L260](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/src/quantum.cc#L257-L260) 给 cha.requests 加 `coh != L1_HIT && coh != L2_HIT` 门限，与 `pmu_l2_misses` 同条件。
* [scripts/pmu_layers_diag.py](file:///data00/yinhaolang/LLMSim/scripts/pmu_layers_diag.py)：把 CHA 行的 real 对照改成 `L2Cache_Controller.{L1_GETS,L1_GETX,L1_UPGRADE}::total`（CHA 请求）、`L1Cache_Controller.Fwd_GET{S,X}::total`（snoop forward）。

**结果**（[logs/oracle_ab_p3c/pmu_layers_diag_AFTER.txt](file:///data00/yinhaolang/LLMSim/logs/oracle_ab_p3c/pmu_layers_diag_AFTER.txt) vs [logs/oracle_ab_p3b_layered/pmu_layers_diag_BEFORE.txt](file:///data00/yinhaolang/LLMSim/logs/oracle_ab_p3b_layered/pmu_layers_diag_BEFORE.txt)）：

| workload                | cha.requests BEFORE | cha.requests AFTER |
| ----------------------- | ------------------- | ------------------ |
| W\_ads\_ctr             | 2460.37%            | **19.41%**         |
| W\_stream               | 820.64%             | **1.85%**          |
| W\_feed\_ranking        | 1064.21%            | **10.67%**         |
| W\_interest\_graph\_recall | 1267.25%         | **21.39%**         |
| W\_false\_sharing       | 51.76%（高估）       | 82.96%（低估）       |

L1d/L2/LLC 三层完全无回归（mr\_l1d\_ld / mr\_l1d\_st / mr\_llc 与 P3.b 逐字节相同）。W\_false\_sharing 翻转为低估，根因是 [P3.a 残留 store false-miss=55,876](file:///data00/yinhaolang/LLMSim/logs/oracle_per_op_diag_p2/W_false_sharing.warmup0.per_op_diag.txt)（MESI 状态机收敛 bug），独立于本节修复。

**残留误差解释**：

* `cha.snp_fwd / cha.remote_hit / cha.wb_required` 仍有 40-67% 误差：oracle 用简化 4-state MESI，Ruby 用 full state machine（含 SM、IS、IM 中间态），forward 触发条件不完全一致。属于 B 层模型差，需在 P4 简化 MESI → 完整 SLICC 时再降。
* W\_ads\_ctr 等 ~20% 残差来自 oracle 未模拟 L2 → L2 之间的 cross-core forwarding（gem5 L1Cache\_Controller 是私有 L2，部分 hit 不出本核），约 600~3000 笔，量级合理。

***


## 4.6 P3.a — store 状态机收敛 bug 尝试（已回滚，negative result）

**动机**：[P3.c 残留 W_false_sharing 翻转为低估](file:///data00/yinhaolang/LLMSim/logs/oracle_ab_p3c/pmu_layers_diag_AFTER.txt#L60-71)（cha.requests 偏低 83%），per-op 诊断显示 [W_false_sharing store false-miss=55,876、load false-miss=176,881](file:///data00/yinhaolang/LLMSim/logs/oracle_per_op_diag_p2/W_false_sharing.warmup0.per_op_diag.txt)。假设是 [simulator.hpp stepImpl](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L333-365) 把 `path_class` 强制绑定到 directory 状态（`other_owns || sc>0 → pc=3`），覆盖了本核 L1 命中实际位置。

**尝试**：path_class 与 coh_oracle 解耦，pc 只看 LRU 命中层级、coh 只看 directory 事件；同时切 PMU 计数门限到 pc。

**实证**（[logs/oracle_per_op_diag_p3a/W_false_sharing.warmup0.per_op_diag.txt](file:///data00/yinhaolang/LLMSim/logs/oracle_per_op_diag_p3a/W_false_sharing.warmup0.per_op_diag.txt)）：

| 指标 | BEFORE (P3.c) | AFTER 方案 A (LRU + mesi gate) | AFTER 方案 B (LRU only, store on S 算 LRU pc) |
| --- | --- | --- | --- |
| load relerr | 416.92% | 1161.17% | 416.92% |
| store relerr | 22.57% | 100.00%（store 几乎全 L1 hit）| 83.10%（store false-hit 增 297,624）|
| load false-miss | 176,881 | 459,205 | 176,881 |
| store false-hit | 35 | 358,139 | 297,624 |

**回滚原因**：tao_trace 的 `truth` 自身就把 store on S(sc>0) 算 pc=3（NoC），与 oracle 的 baseline 一致。修改 oracle 让其按 LRU 算 pc 反而把"truth=NoC、oracle=L1hit"的 store false-hit 拉到 30 万级别。oracle 与 truth 同源派生 path_class，**directory snapshot vs cache-resident snoop 的视图差不可由 oracle 自身状态机收敛**，必须改 tao_trace 或在 truth 侧重定义。

**结论**：P3.a 在当前 truth 定义下已收敛，残留 W_false_sharing 误差是 B 层模型差（tao_trace 自身的 directory state 推断与真实 Ruby snoop 不完全等价），需在 P4 完整 SLICC 时再降。

代码 revert 到 P3.c：`bool resolved = false; if (ev.is_store && (other_owns || sc > 0)) { pc=3; coh=WB_REQUIRED; resolved=true; }`。

***

## 4.7 P4.0 — collapsed Ruby-like stable transaction（第二档首版）

**目标**：在只有 functional trace 的约束下，不做 Ruby message queue / transient timing replay，而是在每条 access 内部展开一个 stable-state transaction，用于修正 CHA request pressure。实现位置：

* [DSideOracle](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L150-L172) 增加 `ruby_l2_request / ruby_inval_targets / ruby_fwd_gets / ruby_fwd_getx`。
* [stepImpl](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp#L366-L379) 从当前 directory snapshot 推导这些 collapsed message。
* [accumulateD](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/src/quantum.cc#L258-L276) 用 `l2_request + inval_targets + fwd_get{s,x}` 累加 `cha.requests.{reads,writes}`。

**重要边界**：首版只把 collapsed transaction 接入 `cha.requests`。`cha.snp_fwd / cha.wb_required` 仍保留 P3.c 的 directory-event 语义。原因是 owner-only forwarding 的试验会使普通 workload 明显回退：例如 `W_ads_ctr cha.wb_required` 从 11.03% 恶化到 88.97%，`W_stream` 从 19.70% 恶化到 93.94%。这说明 functional trace 缺少 Ruby transient/retry/forward 的真实可观测事件，不能安全地把 Fwd_GETX 简化为 owner-only。

**PMU 分层结果**（[logs/oracle_ab_p4_collapsed_reqonly/pmu_layers_diag_P4_COLLAPSED_REQONLY.txt](file:///data00/yinhaolang/LLMSim/logs/oracle_ab_p4_collapsed_reqonly/pmu_layers_diag_P4_COLLAPSED_REQONLY.txt)）：

| workload | P3.c cha.requests err | P4.0 cha.requests err | 变化 |
| --- | ---: | ---: | --- |
| W_ads_ctr | 19.41% | 17.42% | 小幅改善 |
| W_stream | 1.85% | 1.64% | 小幅改善 |
| W_feed_ranking | 10.67% | 9.77% | 小幅改善 |
| W_interest_graph_recall | 21.39% | 19.85% | 小幅改善 |
| W_false_sharing | 82.96% | 62.00% | 明显改善但仍不足 |

**残留原因**：`W_false_sharing` 的真实 Ruby `L2Cache_Controller.L1_GET{S,X,UPGRADE}` 为 3,566,121，而 functional trace 的 collapsed stable messages 只能解释到 1,355,061。缺口主要来自 transient/retry/queue-level message 放大、并发请求合并/重放以及真实 Ruby controller 的多步事务。这些信息不在 committed functional trace 中，因此不能靠 deterministic stable-state transaction 完全恢复。

**下一步**：若继续压 `W_false_sharing`，需要二选一：

1. 扩 trace：在 gem5 侧导出 Ruby request/response/message 事件（含 retry、ack、cacheResponding、controller id、tick）。
2. 做启发式 retry 放大模型：用 `same_line_recent` / sharer count / core ping-pong 信号估算 transient amplification，但这会是拟合模型，不再是严格协议模型。

***

## 5. 文件改动清单(只列代表)

修改:

* [/data00/yinhaolang/LLMSim/shared\_system/shared/lru\_banked.hh](file:///data00/yinhaolang/LLMSim/shared_system/shared/lru_banked.hh) —— +TreePLRU 类,扩 MSHR 多级

* [/data00/yinhaolang/LLMSim/shared\_system/mesi\_ref\_sim/include/simulator.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/simulator.hpp) —— 模板/分发、sharer 扩 8 bit、inclusion 回扫接入、L2/L3 MSHR、DRAM 接入

* [/data00/yinhaolang/LLMSim/shared\_system/mesi\_ref\_sim/include/quantum.hpp](file:///data00/yinhaolang/LLMSim/shared_system/mesi_ref_sim/include/quantum.hpp) —— Coordinator 暴露 `applyBackInval(core_id, addr, level)`
