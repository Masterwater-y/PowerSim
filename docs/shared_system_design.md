# LLMSim Shared System（多核共享/一致性）建模分析与设计

> 本文档记录"如何让窗口级 LLM 模型学到 shared system（多核共享/MESI 一致性）效应"
> 的完整分析、实测数据与最终设计决策。结论以 `data/analyze_sharing.py` 的真实
> 测量为准。

---

## 0. 问题起源

窗口级方案的输入是「多核 functional trace 窗口」，输出是「每核 PMU 向量」。
其中 `inv_recv`（一致性 remote hit/invalidation 接收）等标签属于 shared system 效应。
连续追问暴露了三层问题：

1. 当前输入能让模型学到 shared system 吗？
2. shared system 的历史状态能被后续窗口的推理感知到吗？
3. MESI 是路径依赖的，"回看足够大可无视更早状态"成立吗？

---

## 1. 当前设计的缺陷（分析结论）

### 1.1 输入端：共享不可见
- `data/build_windows.py` 把多核段**整块串接**（`[core0 全部][core1 全部]...`），
  没有跨核时间交错。
- `model/tokenizer.py` 的地址只编码 **vaddr 的 hash 桶**（`VLINE`），且：
  - 只有 vaddr，没有用 paddr 做跨核行匹配 —— 同一物理共享行在不同核 vaddr 不同，
    模型**看不出它们在共享**。
  - hash 到 1024 桶有碰撞噪声。
- 没有任何「谁、何时、碰了哪条共享行」的可见信号，也没有 `<SYNC>` 锚点。

### 1.2 标签端：有真值但输入接不住
- `aggregate_pmu` 已统计 `inv_recv`（coh_oracle ∈ {REMOTE_HIT_CLEAN, REMOTE_HIT_DIRTY}）。
- 但输入无对应可观测特征 → 模型只能按 workload 类型**猜均值**，学不到因果。

### 1.3 跨窗口：状态断裂
- 每个窗口是独立无状态样本，窗口 `k` 看不到 `k-1` 留下的共享历史。
- coherence 状态衰减慢、跨窗口强相关，导致**窗口开头的一致性事件系统性预测错**
  （冷启动偏差），且 `inv_recv` 稀疏，边界误差占比被放大。

---

## 2. 红线：可见（输入）vs 泄漏（仅标签）

| 信息 | 性质 | 能否作输入 |
|---|---|---|
| µop 访问的 **paddr cacheline** | 架构地址（页表决定） | ✅ |
| 行被**几个核访问 / 谁先写**（程序级统计） | 程序行为 | ✅ |
| 多核访问的**程序序先后** | 程序行为 | ✅（不能用 commit_tick，会泄漏 cycle） |
| `mesi_before / coh_oracle / path_class` | MESI 状态机结果 | ❌ 仅标签 |
| `inv_recv / remote_hit` | 一致性协议结果 | ❌ 仅标签（预测目标） |

**核心思路**：给模型看「谁、在什么相对时刻、碰了哪条共享行」（程序行为），
让它自己推断「会不会产生一致性事件」（微架构结果）——即可学习的 cache-coherence simulator。

---

## 3. 关键理论：MESI 的路径依赖 vs 重写遗忘

### 3.1 路径依赖（质疑成立）
MESI 状态是累积演化的：`state_t = f(state_0, 访存序列[0..t])`。
窗口从 `t` 截断丢掉初值 `state_t`，严格说不能无视更早历史。

### 3.2 重写遗忘（救命性质）
但 MESI 是**有限状态 + 马尔可夫遗忘**：
- 每行状态只有 M/E/S/I + 持有者集合，有限。
- 一次 **写** 把该行之前所有历史抹掉（变 M、其余 invalidate）；一次 **eviction** 清回 I。
- 故一条行的「有效历史」从它**最近一次被写/驱逐**算起，而非程序开头。

### 3.3 "回看足够大"的精确条件
| 行类型 | 回看窗 L 内表现 | 能否重建 |
|---|---|---|
| 活跃行（近期频繁读写） | L 内必出现最近一次写 | ✅ 能（马尔可夫遗忘） |
| 半活跃行 | L 内被读、写在更早 | ⚠️ 部分 |
| 冷驻留行 | L 内完全不出现 | ❌ 不能 |

**关键洞察**：一致性事件**只发生在活跃共享行**上；冷行本窗口大概率不碰、
对 `inv_recv` 贡献≈0。所以"重建不了的恰好是不重要的"——
"足够大回看"在工程上近似成立，条件是 **L 覆盖活跃共享行的写-写间隔**。

---

## 4. 实测数据（data/analyze_sharing.py）

对 3 个 ROI run 统计：共享度、写-写间隔（µop）、coherence 事件冷唤醒比例。

| workload | 共享行占比 | 写-写间隔 p50/p90/p99 (µop) | coh 冷唤醒 (gap>256) |
|---|---|---|---|
| producer_consumer (pc4) | 真共享环形 | 极小 | **0.0%** |
| false_sharing (fs4) | 20.5% | 2 / 6 / 6 | **0.0%** |
| compute_int (_roi_check3) | 24% | 2 / 6 / 50 | **0.0%** |

### 4.1 Q1 答案：warm-up L 该多大
- 共享行写-写间隔 p99 ≤ 50 µop（高频重写 = 活跃行）。
- **L=256 即 100% 覆盖所有写-写间隔。** 无需大 warm-up。

### 4.2 Q2 答案：解法 B（循环状态）值不值得上
- 所有 workload 的 coherence 事件冷唤醒比例 = **0.0%**（即便 L 只有 256）。
- 没有任何一致性事件落在"距上次访问 > 256 µop"的冷行上。
- **解法 B 不值得上**——有限回看已覆盖 100% coherence 因果，长尾不存在。

### 4.3 重要保留
以上为 microbench（共享模式简单）。真实负载（LAMMPS / 数据库）共享行可能有大 R
长尾，**届时必须重跑此分析**确认 L 与 B 的必要性。

---

## 5. 最终设计（三支柱 + 历史处理）

### 支柱 1：跨核共享标记（最关键，性价比最高）
build_windows 离线对窗口（+warm-up 前缀）内所有核 µop 一起扫 `cacheline_paddr`，
派生**程序级**共享特征（非 MESI 结果，不泄漏）：

| 字段 | 含义 |
|---|---|
| `share_degree` | 该行被多少不同核访问（0/1/2/3/4+ 桶） |
| `is_shared` | share_degree≥2 |
| `other_wrote_before` | 本 µop 前是否有**其它核写过**该行（潜在 invalidation 源） |
| `writer_core_dist` | 上一写者核与当前核的距离 |
| `reuse_gap_bucket` | 距上次任意核访问该行的 µop 间隔（log 桶） |

> 用 paddr 行做跨核匹配，但**不喂 paddr 数值**（避免 set-index 泄漏），只喂派生标记。

### 支柱 2：全局交错 + 相对时间桶
- 各核 µop 按**程序序归并**交错（用 micro_seq，**禁用 commit_tick**）。
- 在 atomic/lock/barrier/同共享行访问点插 `<SYNC sid>` 锚点。
- 加粗粒度 `epoch_id`（窗口分 8 段），给"并发大致同时段"信号，不泄漏精确 cycle。

### 支柱 3：per-token core_id
交错后段边界消失，每 µop 须自带来源核。
- **推荐 additive embedding**（不加 token）：tokenizer 输出 token 同时输出等长 core_id
  数组，模型 forward 把 core_id embedding 加到 input_embeds。省上下文预算。

### 历史处理：解法 A + C（B 砍掉）
- **A 入口共享摘要**：look-back（L=256~512）统计进入窗口时的活跃共享行/上一写者，
  编码成 `<CTX_*>` token 放在 `<TRACE>` 前。
- **C overlap warm-up**：窗口前带 L=512 条 warm-up µop（只作上下文，**不计入标签聚合**），
  用于重建活跃共享行状态。
- **B 循环状态：不做**（实测冷唤醒 0%，收益不存在，且破坏并行训练）。

### 标签补充（让监督更密）
- `inv_send`、`remote_hit_clean / remote_hit_dirty` 分项。
- `coh_miss_rate = coherence_miss / shared_accesses`（比率头，分母 `is_shared` 计数可见）。

---

## 6. token 预算影响

每 µop 当前 6 token。加共享标记若用 token 化会涨：

| 方案 | token/µop | cores×W 上限(16K档) |
|---|---|---|
| 当前 | 6 | 2730 |
| +2 共享 token | 8 | 2048 |
| +共享+core 前缀 | 9 | 1820 |
| **+共享 token，core_id 走 additive(支柱3-B)** | 8 | 2048 |

→ 共享标记用 token、core_id 用 additive embedding；warm-up 段计入 token 预算。
模型上限 32768（Qwen3-0.6B `max_position_embeddings`），推荐工作点 ≤16K。

---

## 7. 落地优先级

| 步骤 | 改动 | 收益 | 数据支持 |
|---|---|---|---|
| **P1（先做）** | build_windows 加支柱1 共享标记 + C overlap warm-up(L=512)；tokenizer 加共享 token；模型加 SharedCoh 族 | `inv_recv` 从猜均值→可学因果 | §4 实测 L=512 足够 |
| P2 | 支柱3-B：per-token core_id additive embedding | 交错后保留来源核 | — |
| P3 | 支柱2：全局交错 + SYNC + epoch_id；A 入口摘要 token | 跨核时序因果完整 | — |
| P4 | 标签补 inv_send / remote_hit 分项 + coh 比率头 | 监督更密、评估更细 | — |
| ~~B~~ | ~~循环状态 / autoregressive~~ | **砍掉** | §4.2 冷唤醒 0% |

---

## 8. 结论一句话

shared system 可学，但当前输入接不住。修法是**在输入暴露程序级跨核共享行为
（基于 cacheline_paddr 派生标记，不喂 paddr 原值）+ 全局交错 + per-token core_id**。
跨窗口历史用 **warm-up L=512 + 入口摘要** 即可（实测共享行高频重写、coherence 事件
冷唤醒 0%，马尔可夫遗忘快），**无需循环状态（解法 B）**。真实负载需重跑
`analyze_sharing.py` 复核 L 与 B 的必要性。
