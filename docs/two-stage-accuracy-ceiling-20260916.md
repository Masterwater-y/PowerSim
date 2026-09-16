# 两阶段（吞吐优先）框架的精度天花板判断

日期：2026-09-16。状态：**诊断/推理综合（diagnostic synthesis），非 formal 精度报告**。

本文件汇总一个结论性判断：在当前"两阶段（阶段1 每核 lower-bound interval + 阶段2
批量 weave、每个 accepted prefix 施加一次 clock-skew 反馈）"框架合同不变的前提下，
全局 CPI 精度已到达实际下界；剩余可优化空间小、方向双向受限、且历史上反复被证明
无法泛化成全局或大单点收益。这**不是调参不到位，而是框架为换取吞吐所固有的结构
代价**。

本文件不修改模拟器、配置默认或运行 formal 矩阵。所引 40 项与十负载数字均为既有
冻结结果的转述；本轮唯一新增的是一组"活跃路径干预"复现（见第 5 节），用于按项目
既有方法论纪律（"因果结论要靠干预、不靠读源码"）现场验证最关键的结构主张。

所有 CPI 绝对误差为 `abs(FastSim CPI − gem5 CPI)`，单位为 cycles per macro
instruction（遵循 [`accuracy-reporting-contract.md`](accuracy-reporting-contract.md)
第 4 节），取自 scope-matched 未取整值。

---

## 1. 当前维护方案

- 入口 `configs/gem5-fs-native-kernel.cfg` → `gem5-v28_9-fs-load-ready.cfg` +
  `core.store_wait_relief = true`（bank/PRE-gated v2，2026-09-16 用户拍板默认开启；
  回退 `--store-wait-relief false` 重跑）。
- 活跃时序核为 `compute_core_timing_feedback_impl<false,true,false>`（materialized
  fast kernel）；x86 `needs_tso` 使 `core.memory_exposure` 被强制为 1.0。
- 框架本质（见 [`architecture.md`](architecture.md)）：阶段1 并行解码 + 每核
  lower-bound OoO interval（依赖/ROB/IQ/FU）；阶段2 把共享访存按批 gather/sort/weave，
  每个 accepted prefix **只施加一次 clock-skew 反馈**。架构文档明确记载：该路径
  **"audits rather than repairs path-changing order conflicts … a staging point,
  not completed Zsim-style reconciliation"**，并保留了 127,426（C4）/ 153,761（C8）
  个未修复的 same-line 乱序对（explicit failed certificates）。

关键结构性质：**一次性、非重构反馈** 是吞吐来源，也是精度上限来源。它用一次
clock-skew 近似替代了逐周期的乱序重构与真实到达闭合。

---

## 2. 当前精度快照

两个口径需分开陈述，不可混为一谈：

**(a) 冻结 40 项基线**（v28.6 materialized kernel 时代，relief 之前的二进制；见
[`fastsim-v28_6-c4-c32-baseline-2026-08-27.md`](fastsim-v28_6-c4-c32-baseline-2026-08-27.md)）：

| 指标 | 值 |
|---|---|
| CPI MAPE（40 项） | 6.857% |
| CPI P99 / max | 13.204% / 13.430% |
| CPI MAE | 0.056292 cyc/inst |
| 吞吐（等权几何均值 12 代表 case） | ~8.032M user-UOP/s |

**(b) v2 默认开启的十负载验证**（严格宏指令人口对齐 33 项；见
[`store-wait-v2-default40-20260916.md`](store-wait-v2-default40-20260916.md)
与 [`optimization-decisions.md`](optimization-decisions.md) 2026-09-16 两条）：

| 口径 | CPI MAE | MAPE | 备注 |
|---|---|---|---|
| off（基线） | 0.055661 | 5.0359% | — |
| on（v2 默认） | 0.054931 | 5.0614% | 7 改善 / 15 回退 / 11 不变 |
| 非 LBM 29 项 off→on | 0.047218→0.048660 | 5.2869%→5.3795% | **非 LBM 平均回退** |

主过估尾（v2 on）：**LBM C32 = +6.4665%，绝对误差 0.252580 cyc/inst**
（relief off 时为 +7.5364% / 0.294373）。主低估尾：TeaLeaf C32 −12.02%（0.060242），
NAMD C32 −10.46%（0.056394）。

**要点：连当前"最好"的默认候选（v2）本身,都是一次"LBM 局部换全局回退"的权衡,
而非跨负载普遍改善。** 这已是本框架内单次机制改动的典型形态。

---

## 3. 为什么这是"结构天花板"而非调参问题

### 支柱 1 —— 主导误差项对结构资源与内存延迟均不敏感

LBM C32 的 `exposed_memory_penalty_cycles` 占 941,155,750 / 1,008,609,550 =
**93.3% 的全部模拟周期**，但：

- halving 全部 DRAM latency 只移动 CPI **−0.73%**（历史 latency-scale 探针）；
- 放大全部执行资源（见第 5 节本轮实测）只移动 **约 −0.01 cyc/inst**（噪声级）。

源码证实该桶被贴错标签：它其实是 interval-extra 的退休尾闭合残差
（`checkpoint_closed_gap_cycles` / 活跃 fast-kernel 上的
`max(0, sparse_last_retire_cycle − base_end_cycle)`），是**通用调度残差，不是 DRAM
延迟**。一个占 93% 周期、却既不随内存延迟也不随资源变化的桶，说明误差不落在任何
单一结构旋钮上。这也解释了为何历次"内存侧"候选全部只产生噪声级移动：它们作用在
标签上，而非真正主导的项上。

### 支柱 2 —— 残差根因是"重构问题"，不是"参数问题"

逐层定位（见 [`lbm-error-mechanism-20260914.md`](lbm-error-mechanism-20260914.md)）：
C16→C32 尾增长 0.689445 cyc/inst → 其中 SQ-admission 占 **72.03%** → 序列化退休 →
再收敛到 DRAM store-service 的 bank/command 等待。最深一层是**时间压缩**：阶段1 用
lower-bound 排程算到达，把 105 个 store 的到达投影进 **706 周期**的窗口，而 native
Ruby admission 实际跨 **33,553 周期**，最终 FastSim send 跨 71,261 周期。这个压缩是
"在还不知道共享反馈时用 lower-bound 排程算到达"的**内生产物**。要真正闭合它，必须做
跨 Q 的真实到达闭合 —— 而那正是已被否决的路线（见支柱 3）。

### 支柱 3 —— 每次"更精确地闭合"都撞墙，方向固定

| 候选 | 结果 | 出处 |
|---|---|---|
| canonical store-response projection | LBM C32 CPI 4.200→**88.15（+2156%）**，MAE 0.117→36.2 | 2026-09-15 repair experiment |
| projected DRAM feedback | 热点 C13 服务改善，整核 +11.99%→**+40.66%**，端到端失败 | 2026-09-14 pilot |
| ACT ownership 扩展 | 四项 LBM 绝对误差**全部恶化** | 2026-09-15 further optimization |
| 全核心 continuation / causal_read | 吞吐掉 **13–43%**，且从未通过 gem5 等价 | 多条历史决定 |
| response-critical bank gate（v3） | LBM 四项 MAE 0.100→**0.107**，正窗口误差 +24%→+96% | 2026-09-16 v3 |

一致模式：任何试图在阶段2 引入"真实到达 / 未来响应可见性"的机制，要么精度爆炸，
要么吞吐崩塌。这与框架"一次性非重构反馈"的定义直接冲突。

### 支柱 4 —— 误差是"分布式 + 补偿式"的，天然抗单点修复

- C16 整体误差近 0，是 core0（+1.068）与其余 15 核负误差**相互抵消**的结果；
  C32 残差的 93.35% 分布在 core1–31，并非某个异常核。
- guardrail（`configs/cpi-guardrail-baseline-v1.json`）冻结 LBM（上界）+ TeaLeaf/NAMD
  （下界）正是为抓这种交易：**任何全局降低过估的机制都会把已经低估的 tealeaf/namd
  推得更低**（tealeaf 在同一 relief 下移动 −10.58%）。
- 结论：不能在运行时区分"高 MLP 过估区"与"低估区"的机制，必然自我抵消。这是十负载
  验证里非 LBM 平均误差回退的根本原因。

---

## 4. 本框架内的可优化空间（及为何未兑现）

理论上仅剩一条**默认关闭、局部门控**的路：仅在"本地可观测的高 MLP / 有并发独立在飞
请求"条件下，在 settlement 点减少闭合残差，并强制通过双向 guardrail。文档已把它
scope 好（见 mechanism doc 第 4 节 candidate/acceptance）。但需如实说明：

1. **期望收益低**：30+ 候选经验 + 上述结构论证表明，凡不能运行时区分过估/低估的机制
   都会抵消。
2. **不可能是大幅收益**：它只能回收"有并发独立 miss、确实被过度串行化"的那部分残差，
   而其中一部分是 gem5 也要付的真实串行代价（latency 外推到零仍 +6%）。
3. **上界是算术上界，不是可达界**：全部 70.5M 过估周期（7.52%）仅在"每个被串行化的
   残差都完美可重叠"时才可回收，而这与"部分是真实串行"矛盾。

---

## 5. 本轮活跃路径干预复现（新增证据）

按项目方法论纪律，现场实跑冻结 LBM C32 输入
（`tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r`，
`--measurement-scope user-plus-kernel --cores 32`，`store_wait_relief=false` 以匹配
冻结 BASE_CPI），验证支柱 1 的决定性主张：

| 配置 | CPI | Δ vs baseline | signed vs gem5(3.906) | 绝对误差(cyc/inst) |
|---|---:|---:|---:|---:|
| baseline（维护宽度） | 4.2003734 | 0（精确复现冻结值） | +7.536% | 0.294373 |
| 无限机器（exec=32, ROB=1024, IQ=512, fetch=8） | 4.1902814 | −0.010092 | +7.278% | 0.284281 |

**解读**：把执行资源放大 4–8 倍，CPI 只移动 0.0101 cyc/inst（约为 0.294 误差的 3.4%），
仍 +7.28% over gem5。本轮方向（略降）与历史探针记录的方向（略升 +0.0153）相反，但
**两者一致地落在噪声级**——这本身印证了 mechanism doc 的既有 caveat：该移动来自
request ordering / overlap / checkpoint 人口的反馈扰动，而非资源效应。误差不在结构
资源上，这一结论稳健。

> 说明：本轮为两点手动干预（baseline + 无限机器），足以验证最关键的反单调/不敏感
> 主张；完整六旋钮 sweep（`tools/probe_lbm_base_schedule.py`）只增加每旋钮粒度，
> 不改变结论。原样本 sweep 的历史逐项表见 mechanism doc Finding 1–3。

---

## 6. 打破天花板的唯一出路及其代价

当前框架处于一个 **Pareto 点**：要显著更准，只能离开"两阶段换吞吐"的合同 ——

1. **加 reconciliation（rollback + reweave 真实到达闭合）**：已被反复量化为要么精度
   爆炸（+2156%），要么吞吐掉 13–43%；
2. **换成学习式时序（TCSim 路线）**：精度 ~5%，但吞吐从 ~8M 掉到 tens of thousands
   UOP/s（约 1000×），并丢失确定性与几何泛化（geometry 变化免重训）。

两者都属于"换框架"，不属于"优化框架"。

---

## 7. 结论与停止条件

- **判断**：当前 ~6.9% MAPE / 0.294 cyc-inst（LBM C32，relief off）不是"还没调到位"，
  而是"单遍反馈只审计不重构" + "双向补偿式误差"两条结构性质共同决定的**实际下界**。
  全局大收益或单点大收益在本合同内基本不可能再取得。
- **仅剩空间**：局部门控、双向受限、历史上从未泛化成功的小空间；任何候选必须默认关闭、
  仅依赖 functional trace + modeled state、无 workload/PC/误差符号选择、并同时通过
  12 项 guardrail 与吞吐门（≥5M user-UOP/s）。
- **不要重复的路径**：重开 projected-DRAM / full cross-Q continuation（已否决）；按
  `memory_penalty` 桶直接减周期（掩盖来源、破坏状态一致性）；用局部窗口收益或 MAPE
  掩盖 P99/max 或低估尾回退；把服务 MAE 当作 CPI MAE。
- **重新开启大改的条件**：显式接受"离开吞吐优先合同"的产品决策，并对 reconciliation
  或学习式时序的吞吐/泛化代价做量化验收。

---

## 8. 证据与出处

- 本轮复现产物：baseline 与无限机器两次 LBM C32 运行，`scope_metrics.perf_like_cpi`
  分别为 4.2003734 / 4.1902814（临时配置与输出已清理，可用第 5 节命令复现）。
- 主要出处文件：
  - [`lbm-error-mechanism-20260914.md`](lbm-error-mechanism-20260914.md)（支柱 1–4 的
    干预、账本与配对证据）；
  - [`optimization-decisions.md`](optimization-decisions.md)（v2 默认、v3、ACT、
    projected-DRAM 等决定与停止条件）；
  - [`fastsim-v28_6-c4-c32-baseline-2026-08-27.md`](fastsim-v28_6-c4-c32-baseline-2026-08-27.md)
    （40 项基线与吞吐合同）；
  - [`architecture.md`](architecture.md)（两阶段结构、未修复乱序对、与 Zsim/TCSim 对比）；
  - [`store-wait-v2-default40-20260916.md`](store-wait-v2-default40-20260916.md)
    （十负载验证）。
- 复现工具：`tools/probe_lbm_base_schedule.py`（六旋钮 sweep）、
  `tools/run_cpi_guardrail.py`（12 项冻结 signed error ±1pp 兼容带）。

本文件为诊断/推理综合，不构成 formal 精度报告；不改变任何模拟器/配置默认，
未运行 formal 矩阵，未提交或推送。
