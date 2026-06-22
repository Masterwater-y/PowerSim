# 跨核时间锚点（per-core window T_start）设计

## 1. 背景与动机

当前窗口构造按「固定指令数」切窗：每个核取程序序第 `t..t+W` 条 µop 放入同一个 `seg`。
这对单核 CPI 预测自洽，但对跨核效应（伪共享、一致性失效）存在系统性问题：

> 不同核 CPI 不同 → 同样 W 条指令花费的物理时间（cycle）不同 →
> 同一个 `seg` 索引下，8 个核覆盖的物理时间区间彼此错位，且随程序推进越漂越远。

伪共享这类负载的「谁在物理时间上先写」关系因此被破坏，模型难以学到正确的跨核交互。

### 诊断证据（ckpt: train8_w512_embfix_v2，8 卡全量 eval）

| 负载 | 跨核敏感度 | per-window CPI MAPE | pred vs gem5 |
|---|---|---|---|
| W_compute_int | 无（纯计算） | 2.92% | 5.43% |
| W_indirect | 弱 | 12.39% | 14.86% |
| W_chase_dram | 中 | 12.24% | 4.88% |
| W_false_sharing | 强（伪共享） | **34.36%** | 13.99% |

per-window MAPE 随跨核敏感度单调上升，false_sharing 是 compute_int 的 ~12 倍，
强烈暗示「按指令切导致跨核时间错位」是真实瓶颈（待对照实验确认因果）。

## 2. 方案演化与取舍

讨论中淘汰的中间方案及原因：

1. **固定 T（绝对 cycle 宽度）切窗**：4 核 / 8 核在同样 T 内指令数差异巨大 →
   上下文一边浪费一边超长；且若 T 按平均 CPI 自适应会与核数耦合，损害核数泛化。
2. **时间填充切窗（按 token 预算填满，T_end 自得）**：跨核对齐最干净，但
   **推理时存在自举循环依赖** —— 窗口边界依赖 cycle，cycle 依赖推理，推理依赖窗口。
   纯时间切窗在部署（无 gem5 真值 tick）时无法构造输入。
3. **给每条 µop 喂 commit_tick**：部署时 tick 未知（正是预测目标），同样自举；
   且窗口内匀速近似对 cache miss 密集段不准。

### 最终方案：按指令切窗 + 每核窗口相对 T_start 连续特征

- **窗口边界仍按固定指令数切**（functional 已知）→ 推理可并行、无自举死锁。
- **额外输入**：每个核、每个窗口一个标量 `T_start_core`（该核窗口首条 µop 的 commit_tick），
  相对窗口归一化后作为**连续特征**注入对应核的 query hidden。
- 模型从 8 个核的相对 T_start 直接读出「本窗口内各核物理时间错开多少」，
  即跨核时间对齐的**关键锚点**，而无需窗口内每条指令的 tick。

为什么只取窗口起点：CPI 反推中窗口**端点**的 tick 最准（总 cycle 对即可，不受窗口内
分布影响），匀速近似的误差集中在窗口内部——只取起点恰好规避了最不准的部分。

## 3. 部署推理时如何获得 T_start

T_start 仍是 cycle，部署时未知，但代价远小于反推每条 µop：只需逐窗口传递一个标量。

```
窗口 k 的 T_start_core = 窗口 k-1 该核 T_start + 窗口 k-1 该核预测 cycle
                      = T_start_{k-1} + CPI_pred_{k-1} × instr_{k-1}
```

- 窗口边界按指令切（固定、已知）→ 窗口输入内容一开始就确定，可并行。
- 只有 T_start 这个标量需要递推。
- **Chunk 并行**：按指令序粗切 N 块分到 N 卡，块内逐窗口递推 T_start，块间并行；
  块边界 T_start 用粗估初值，仅影响每块首窗口（N-1 个边界，误差可忽略）。
- 离线评估：直接用 gem5 真值 tick 作为 T_start，完全并行。

## 4. 实现要点

### 4.1 数据构造（build_windows.py）
- 每核窗口额外计算 `t_start_core = win[0]._commit_tick`、`t_end_core = win[-1]._commit_tick`。
- 相对窗口归一化：取本窗口 8 核中最小 t_start 为零点，
  `rel = (t_start_core - min_tstart) / tick_per_cycle`（单位 cycle，跨核可比，数值可控）。
- 样本新增字段 `t_start_rel`: `[n_core]`（float）。

### 4.2 数据加载（dataset.py）
- cache 样本与 collate 透传 `t_start_rel`，batch 输出 `t_start` tensor `[B, n_core]`。
- 旧 cache 无此字段时回退为 0（向后兼容，等价于不启用）。

### 4.3 模型注入（llm_wrapper.py）
- 新增小投影 `tstart_proj: Linear(1 -> d_model)`（可训练，bf16）。
- forward 增参 `t_start`: `[B, n_core]`；对每核 query_hidden 加上
  `tstart_proj(t_start_norm)`，其中 `t_start_norm` 做一次尺度归一（如除以常数 / log1p）。
- 训练入口把 `tstart_proj` 纳入 head 参数组优化。

### 4.4 训练入口（train_lora.py）
- TrainModule.forward 与训练/验证步透传 `t_start`。
- `tstart_proj.parameters()` 加入 head_params 优化分组。

## 5. 对照实验（天花板测量）

目的：用 gem5 真值 tick 作为 T_start，测「理想跨核时间锚点」能把 false_sharing 的
per-window MAPE 从 34% 降到多少，回答「时序信息到底值不值钱」。

- 控制变量：唯一变量是「是否注入 T_start」，其余（模型 / LoRA / 步数 / token 预算 / 负载）全固定。
- 负载：W_false_sharing（跨核最敏感）+ W_compute_int（单核对照，保护性，不应退化）。
- 指标：per-window CPI MAPE 为主，pred vs gem5 / pred vs label 为辅。
- 判据：
  - ✅ 值得推广：false_sharing MAPE 显著下降（如 34% → <15%）且 compute_int 不退化。
  - ❌ 不值得：false_sharing 无明显改善 → 瓶颈不在切窗/对齐，省去部署端递推复杂度。

## 6. 这个方案不解决什么

- **核数泛化**：8 核训练→16 核预测仍是分布外，需在数据采集层覆盖多核数，切窗方式无法替代。
- **窗口内暖机态**：窗口起点的 cache/分支预测器状态仍丢失，属另一问题（warm-up 前缀解决）。
- T_start 只给跨核**起点**对齐，窗口内细粒度跨核时序仍是近似。

---

# 7. 对照实验结论（A 基线 vs B 时间锚点，已完成）

数据集 `data/windows_exp_tstart`（W=160, stride=160, 不重叠），MAXLEN=8192，8 卡全量 eval。
A=`ckpt/exp_baseline`（USE_TSTART=0），B=`ckpt/exp_tstart`（USE_TSTART=1，注入相对 T_start）。

| 负载 | per-window MAPE (A) | per-window MAPE (B) | pred vs gem5 (A) | pred vs gem5 (B) |
|---|---|---|---|---|
| W_compute_int | 1.95% | 2.27% | 7.82% | 6.42% |
| W_false_sharing | **28.15%** | **27.36%** | 7.19% | **3.27%** |

**判定：时间锚点（单标量 T_start）未通过验收。**

- 核心判据是 false_sharing 的 per-window MAPE 显著下降（→<15%），实际仅 28.15%→27.36%，几乎不动。
- 唯一改善的是**全局** CPI（pred vs gem5 7.19%→3.27%），但这只是把整体预测偏置往上拉了
  （cpi_pred 20.94→21.82，更接近 label 22.48），属于"平均值蒙得更准"，**逐窗分辨能力没变**。
- 结论：单个起点标量信息量太低，补不回"按指令切窗导致的跨核物理时间错位"这个结构性缺口。

## 7.1 为什么"逐窗不准但整体准"

- per-window MAPE 取**绝对值**累加，正负误差不抵消。
- 全局 CPI = Σ(cpi·macro)/Σmacro，先聚合再相除，各窗口正负偏差在 Σ 中**相互对消**。
- 模型学到了 false_sharing 的**平均 CPI 水平**，但没学会**区分哪个具体窗口该高、哪个该低**。
- 对周期级仿真（要逐窗 CPI 曲线）而言，只有 per-window 精度才有意义；全局准是假象。

## 7.2 标签可学性诊断（scripts/_diag_label_var.py）

| 指标 | compute_int | false_sharing | 含义 |
|---|---|---|---|
| per-core CV（相邻窗口波动） | 0.118 | 0.179 | fs 波动更大 |
| **lag-1 自相关** | -0.030 | **0.482** | fs 序列**有强时间结构，非白噪声** |
| 跨核相对中位数离散 | 0.010 | 0.165 | fs 同窗各核差异 16× |
| CPI 范围 | 0.5–4.4 | 0.78–32.7 | fs 动态范围极大 |

**结论：false_sharing 的逐窗 CPI 是平滑、可学的（自相关 0.48），不是标签噪声。**
→ 28% MAPE 是**模型/对齐能力问题**，不是数据噪声问题。可学信号被"按指令切窗"破坏了。

## 7.3 根因：为什么模型学不到跨核影响

- 8 核 token **本就拼在同一序列**（`<C0_BEGIN>...<C1_BEGIN>...`），模型物理上看得见所有核。
- 但切窗按"程序序第 t..t+W 条指令"对所有核取**同一指令下标区间**（build_windows.py L203）。
- 由于各核 CPI 不同、快慢不同，`<C0>` 段和 `<C1>` 段对应的**物理时间完全错开**。
- 模型看到的跨核 token **在物理时间上并非同时发生**，争用信号（"此刻谁在抢同一 cache line"）被对齐方式破坏。
- 再大的 attention 也无法从错位的时间片里恢复争用 → 这是 28% 的真正来源。

## 7.4 为什么"按物理时间切窗"训练可行但部署不行

- 按物理时间切窗能根上对齐跨核（`<C0><C1>` 装同一时间段指令），训练时可行（gem5 有 commit tick）。
- 但部署存在**自举循环依赖**：
  > 按时间切窗需要每条指令的 tick → tick = 累积 cycle = CPI 的积分 → CPI 正是模型输出。
  > **需要 CPI 来切窗，又需要切窗来预测 CPI。**
- 部署输入只有 functional trace（无时间信息），cycle 是预测目标 → 推理时无 tick 可用来划窗。

---

# 8. DVFS 场景的方案选择（关键约束）

## 8.1 DVFS 必须吃物理时间轴

DVFS = 动态改核频率 = 拉伸/压缩该核的物理时间轴：

> 改频率 → 该核时间轴拉长/缩短 → 它的访存相对其他核**整体平移** →
> 与其他核的 cache line 碰撞模式变 → 争用变 → CPI 变。

**DVFS 的全部效果都通过"频率→时间错位→争用"这条路径发生。** 模型要预测降频后 CPI，
就必须理解"时间错位变化如何改变争用"——即时间对齐必须是一等输入。

## 8.2 方案 2（跨核特征）在 DVFS 下结构性失效

- 方案 2 的争用特征在"按指令数切的窗口"里算，而指令窗口内容**不随频率变化**
  （Core0 第 1000–1160 条指令，无论 2GHz/3GHz 都是同样指令、同样地址）。
- → 特征对频率不敏感 → 改频率后模型输入不变 → 预测出相同 CPI。
- **方案 2 对频率完全瞎，无法表达 DVFS 敏感性，不可用于 DVFS。**

## 8.3 各方案对 DVFS 的适配

| 方案 | 通用场景 | DVFS 场景 | 理由 |
|---|---|---|---|
| 方案 1 两遍法+时间切窗 | 推荐 | **必需/正解** | 时间轴随频率重算，争用随之变 |
| 方案 2 跨核特征 | 先试 | **不适用** | 频率盲 |
| 方案 3 全局交错序列 | 备选 | 可作方案1增强 | 时序原生编码，但仍需 tick |
| 方案 4 只保全局 CPI | 兜底 | **不行** | DVFS 要时间分辨曲线 |

**DVFS 加分点**：DVFS 决策粒度通常 μs–ms 级（几千~百万 cycle），不需要 160 指令的细窗。
可按 DVFS 决策间隔切大时间窗 → 窗口大、数量少 → 两遍法串行开销与切窗噪声都被摊薄。

---

# 9. 方案 1：迭代两遍法（DVFS 正解，详细设计见 §10）

## 9.1 思路

把"切窗需要时间、时间需要 CPI"的循环用**迭代**解开：

```
Pass 0（粗）: 按指令切粗窗 → 粗预测每核 CPI → 得到每核近似 tick 轴
Pass 1（细）: 用 Pass0 的 tick 轴按物理时间重切窗（<C0><C1> 装同一时间段）
            → 重新预测 CPI → 更准的 tick 轴
(可再迭代 1–2 轮至收敛)
```

第二遍起跨核已按物理时间对齐，争用信号恢复。tick 来自上一遍预测，**不依赖 gem5，部署可行**。

## 9.2 开销不是 2 倍（≈1.1–1.3×）

1. **两遍粒度不同**：Pass 0 只为估时间轴，用粗窗（如每核 1 万指令），窗口数少一个量级，
   算力占细遍的 5–15%。不是"两次完整精细推理"。
2. **Pass 0 可用更便宜模型**：估时间轴只需 CPI 量级（compute_int 段已 1.95% 很准，时间轴主体靠它定），
   可用蒸馏小模型甚至线性回归。
3. **非全量重算**：只有跨核敏感段在 Pass 1 显著变化；compute_int 这类 Pass 0 已准的段可缓存/跳过。

## 9.3 并行性：每遍内部全并行，仅 pass 间串行

| 方案 | 串行深度 | 并行度 |
|---|---|---|
| 逐窗自举（坏） | O(窗口数)，数千次串行 | 无 |
| **两遍法（好）** | O(pass 数)，2–3 次串行 | 每遍内全部窗口 8 卡并行 |

- **pass 间串行**：Pass 1 切窗依赖 Pass 0 输出，无法避免，但只有 2–3 次。
- **pass 内并行**：单遍内窗口边界一次性确定、彼此独立，可任意分片到多卡（即现 eval_cycles 的做法）。
- 两遍法的核心价值：用 2–3 次全局串行，换回窗口级的完全并行，避开"逐窗自举"的 O(窗口数) 串行灾难。

```
总时间 ≈ Pass0(粗 ~0.1×) + Pass1(细 1×) ≈ 1.1× 单遍   （两遍各自内部 8 卡并行）
```

---

# 10. 方案 1 详细设计（两遍法 + 物理时间切窗）

## 10.1 数据基础（已确认）

- 每条 µop 都带 `_commit_tick`（build_windows.py L86，来自 labels.micro.jsonl）。
- gem5 的 commit_tick 是**全局统一时钟**（所有核同一时间轴），跨核可直接比较。
- → 训练时可按物理时间精确切窗；这是两遍法成立的前提。

## 10.2 物理时间切窗（Pass 1 的窗口定义）

与现状（按指令下标 `[t, t+W]`）不同，Pass 1 按**全局时间区间**切：

```
选定窗宽 ΔT（单位 cycle，对应 DVFS 决策粒度，如 1e4~1e5 cycle）
窗口 k 覆盖全局时间区间 [T0 + k·ΔT, T0 + (k+1)·ΔT)
每个核 c：收集 commit_tick 落在该区间内的 µop → <Cc> 段
```

关键性质：
- 所有 `<C0>..<C7>` 段覆盖**同一物理时间区间** → 跨核争用信号天然对齐。
- 每核窗口内**指令数可变**（快核多、慢核少），需 token 预算管理（见 10.4）。
- 标签语义变化：窗口 cycles 固定 = ΔT/tick_per_cycle；**CPI = ΔT_cycles / n_instr_core**。
  即"固定时间内各核退休多少指令"成为被预测量——对 DVFS 恰好是天然目标
  （给定频率，预测单位时间各核吞吐）。

## 10.3 两遍流程（训练 + 部署）

### Pass 0（粗估时间轴）
- 输入：按指令切的**粗窗**（如每核 1e4 指令/窗，窗口少）。
- 输出：每核每粗窗 CPI → 累积得到每核近似 tick 轴
  `tick_c(i) ≈ Σ_{j<i} CPI_pred`（µop 级线性内插）。
- 可复用现有按指令切窗模型（compute_int 段已 1.95%，时间轴主体可靠）。

### Pass 1（按时间重切 + 精预测）
- 用 Pass 0 的 tick 轴，把每条 µop 映射到全局时间，按 ΔT 重切窗。
- 跨核已对齐 → 预测精细 CPI / 吞吐。
- 离线评估：直接用 gem5 真值 tick 切窗（上限对照）。
- 部署：用 Pass 0 预测 tick 切窗（真实场景）。

### 自举闭合（可选迭代）
- Pass 1 输出更准 CPI → 回头更新 tick 轴 → 再切一次。迭代 1–2 轮至 tick 轴收敛。

## 10.4 训练/部署一致性（核心设计难点）

**问题**：Pass 1 若只用 gem5 精确 tick 训练，部署时喂的是 Pass 0 的**近似 tick**（有误差），
窗口边界偏移 → train/deploy 分布不一致 → 精度打折。

**三个候选解（需选型）**：

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| A. 噪声注入 | 训练时对 gem5 tick 加扰动模拟 Pass0 误差，按扰动后边界切窗 | 实现简单，无需先训 Pass0 | 噪声分布需贴近真实 Pass0 误差 |
| B. 闭环数据 | 先训 Pass0，用其预测 tick 构造 Pass1 训练数据 | 最忠实部署分布 | 需 Pass0 先就绪，流程串行 |
| C. 边界鲁棒 | 窗口边界软化（重叠/padding），降对精确边界敏感 | 对 tick 误差天然鲁棒 | 改 token 方案，复杂 |

推荐路线：**先 A（快速验证时间切窗上限）→ 上限高再上 B（贴部署）**。

## 10.5 Token 预算管理

时间窗内各核指令数不等，总和可能超 MAXLEN：
- 每核段独立截断到 `MAXLEN/n_core` 上限，超出丢弃尾部（标签同步截断或全段聚合）。
- 或动态 ΔT 按 token 预算反推（但 ΔT 应锚定 DVFS 粒度，优先固定 ΔT + 截断）。
- 需统计真实负载在选定 ΔT 下的每核指令数分布定 MAXLEN（见 10.7）。

## 10.6 改动清单

| 文件 | 改动 |
|---|---|
| data/build_windows.py | 新增 `build_samples_timewin(ΔT, noise)`：按全局时间区间切窗，标签按时间片聚合 |
| train/dataset.py | 兼容变长每核指令数；feat_version=3 |
| eval/eval_cycles.py | 支持时间窗评估；新增 Pass0→Pass1 两遍推理驱动 |
| scripts/ | 新增两遍法数据构造 + 训练 + eval 脚本 |

## 10.7 落地前的零成本验证（建议先做）

写切窗代码前，先用真值 tick 做两个统计验证确认方向：
1. **ΔT 标定**：选候选 ΔT（1e4/3e4/1e5 cycle），统计各负载每核窗口指令数分布
   → 定 MAXLEN 与窗口数量级，估训练成本。
2. **时间切窗上限预演**：用真值 tick 按时间切窗，统计 false_sharing 同窗各核
   共享地址重叠率，对比按指令切窗，确认时间切窗确实恢复了争用信号（数据层面，无需训练）。

通过后再进入 10.6 代码实现与 A 方案训练。

### 10.7.1 零成本验证已完成（W_false_sharing, limit=40000/core）

**验证1（ΔT 标定）**——跨核物理时间错位实测：
- 同样 40000 指令，最慢核(core1)跨 3.41 亿 tick，最快核(core6)跨 3.07 亿 → **慢核物理时间是快核的 1.47×**。
- 直接证实"按指令切窗→跨核物理时间严重错位"。
- 每窗 8 核合计指令数（≈token 预算需求）：

| ΔT | n_win | 每核每窗(med/p90/max) | 8核合计(med/p90/max) |
|---|---|---|---|
| 1e4 tick (30cyc) | ~30000 | 12/14/56 | 13/26/326 |
| 3e4 tick (90cyc) | ~10000 | 12/14/78 | 29/61/465 |
| **1e5 tick (300cyc)** | ~3000 | 14/26/143 | **118/152/982** |
| 3e5 tick (901cyc) | ~1000 | 51/64/185 | 366/418/1280 |

**验证2（争用对齐，决定性证据）**——跨核争用对的物理时间差：
- false_sharing 争用集中在 **2 条热 cacheline**（典型伪共享），49094 个跨核争用对。
- 争用对 Δtick: **median=2997 (9 cycle)**, p90=11322 (34 cycle) → 争用双方在物理时间上几乎同时。
- 同窗捕获率随 ΔT：1e4→88.0%, 3e4→99.2%, **1e5→100.0%**, 3e5→100.0%。

**结论：方案 1 通过验证。** ΔT=1e5 tick（300 cycle）能 100% 捕获跨核争用且 token 预算友好
（每窗合计 med 118），选为第一版 ΔT。争用对 median 仅 9 cycle 说明"近似对齐"即可捕获，
为后续思路 A 提供依据。

---

# 11. 上下文长度问题与切窗方案演进（v2 设计储备）

固定 ΔT 时间切窗有两个**已识别但 v1 暂不解决**的结构性缺陷（v2 再处理）：

## 11.1 缺陷1：窗口变短伤害长历史标签

- 时间切窗把每核每窗指令从 ~1280（指令切窗）压到 ~118（ΔT=1e5）。
- 依赖**长 reuse distance** 的标签会受损：`mr_llc`(LLC miss)、`mr_l1d_*`、`dtlb/itlb_miss`、`mpki_br`
  ——它们的"因"（上次访问同行/同页）可能落在短窗之外，模型看不到。
- 而跨核标签（`inv_recv`、false_sharing 的 CPI 增量）反而受益于短时间窗的对齐。
- **粒度冲突**：跨核争用要细时间窗对齐；cache/TLB/分支要长指令历史暖机。同一窗难两全。

## 11.2 缺陷2：固定 ΔT 导致上下文利用率不均

窗内指令数 = ΔT / CPI，导致：
- **核间不均**：快核(CPI≈1)塞 ~300 条、极慢核(CPI≈20)只塞 ~15 条，慢核 token 槽位大量闲置。
- **负载间不均**：chase_dram(高CPI)每窗指令极少→上下文浪费；compute_int(低CPI)填得满。
- **讽刺**：越是高 CPI、越需要长访存历史的负载，固定 ΔT 给它的指令上下文越短（需求与供给反向）。

## 11.3 候选切窗方案对比（v2 选型）

| 方案 | 核间上下文 | 跨核对齐 | 标签语义 | DVFS | 复杂度 |
|---|---|---|---|---|---|
| **固定 ΔT（v1）** | 不均 | 精确 | 稳定(固定时间预测指令数) | 需加频率特征 | 低 |
| A. 固定指令数+近似时间对齐 | **均等** | 近似(各核 N 条跨时间宽度不同) | 稳定(固定指令预测cycle) | 需加频率特征 | 中 |
| B. 动态 ΔT 撑满预算+注入 ΔT | 仍不均(核间) | 精确 | 随窗变(须注入 ΔT) | **天然支持** | 高 |
| D. 长前缀+固定时间目标段 | 不均 | 精确 | 稳定 | 需加频率特征 | 中 |

**关键洞察**：验证2 证明争用对时间差 median 仅 9 cycle，故"近似对齐"（思路 A）几乎必能把
争用装进同窗——A 的精确对齐损失可忽略，却换来核间均等（同时缓解 11.1/11.2 两个缺陷）。
故 **A 很可能优于 B**（B 的精确对齐对 9-cycle 争用是过度对齐，代价是核间不均未解）。

## 11.4 ΔT 动态化（推理时调整 ΔT）

- ❌ 训练固定 ΔT、推理换不同固定 ΔT → 分布外，不行。
- ✅ 训练时混多尺度 ΔT 且把 ΔT 注入为输入特征 → 推理可在覆盖范围内自由调。
- DVFS 改频率 = 拉伸时间轴 = 改变有效时间尺度 → **多尺度 ΔT 训练 + ΔT 注入是 DVFS 频率泛化的必需品**，非可选。

## 11.5 v1 → v2 推进逻辑

```
v1: 固定 ΔT=1e5（最简）→ 唯一目的：回答"时间对齐能否降低 false_sharing 的 28% MAPE"
    ├─ 若有效 → v2: 思路 A（均等上下文+近似对齐）+ ΔT/频率注入（DVFS 就绪）
    │           并视 11.1 诊断决定是否加长历史前缀
    └─ 若无效 → 瓶颈另有其因，放弃时间切窗方向，省下 A/B 全部投入
```

**先跑 v1 固定 ΔT 证伪/证实，不让 v2 方案选型阻塞 v1 验证。**

---

# 12. 方案A（N=384, 8负载）实验结果与采样偏差发现

## 12.1 实验结果（ckpt/exp_align_n384_8w, MAXLEN=20480, 8卡全量eval）

数据集 `data/windows_align_n384_8w`（每核固定 N=384 指令 + 全局时间锚点近似对齐，
8 负载共 23078 窗口）。训练 2000 步，best_val_loss=-18.23。

| 负载 | per-window MAPE | pred vs label | pred vs gem5 | label vs gem5 |
|---|---|---|---|---|
| W_compute_int | 1.54% | 1.24% | 9.23% | 8.09% |
| W_false_sharing | **6.14%** | 6.06% | 1.79% | 8.35% |
| W_branch_storm | 12.21% | 0.40% | 4.66% | 4.28% |
| W_indirect | 7.39% | 2.84% | 0.02% | 2.79% |
| W_int_div | 10.02% | 6.18% | 3.20% | 2.81% |
| W_chase_dram | 7.94% | 2.91% | **140.87%** | 148.10% |
| W_phased_mix | 9.49% | 3.68% | **230.73%** | 243.36% |
| W_stream | 10.34% | 2.21% | **516.29%** | 530.23% |

**关键正向信号**：false_sharing per-window MAPE 从基线 28.15% → **6.14%**（降 4.6×），
近似时间对齐似乎真有效。⚠️ 但同时变了多个变量（切窗方式 + N 160→384 + 2负载→8负载
+ step 1000→2000），不能 100% 归因于对齐；需补 N=160/2负载/USE_TSTART=0 干净对照。
⚠️ 且仍在训练集上评估，有标签泄漏，绝对数字偏乐观。

## 12.2 采样偏差发现（chase_dram/phased_mix/stream 的 pred vs gem5 爆炸）

现象：这三个负载 pred vs gem5 达 140%~516%，但 **pred vs label 都很小（2-4%）**，
说明模型预测窗口标签很准——问题在 **label vs gem5 本身就爆炸**（窗口集 CPI 不代表全程）。

**根因：方案A 按全局时间轴等距撒锚点 = 按 wall-clock 时间采样窗口，对 CPI 时变负载系统性高估平均 CPI。**

### 数学本质
- 按**时间**均匀采样 = 给每段按其**占用时间**加权。
- 真实 CPI = 按每段**占用指令数**加权（Σcycle/Σinstr）。
- 高 CPI 段"指令少但耗时长"→ 时间采样放大其权重 → 高估平均 CPI。

### 数值举例（单核，1000 指令）
| 阶段 | 指令 | 每条CPI | 耗时(cyc) |
|---|---|---|---|
| A 计算段 | 900 | 1 | 900 |
| B 访存段 | 100 | 100 | 10000 |

真值 CPI = 10900/1000 = **10.9**。
按时间撒 100 锚点：A 段占时间 8.3%→8 锚点，B 段 91.7%→92 锚点。
窗口集均值 ≈ (8×1+92×100)/100 = **92** → 高估 8×。

### 实测验证（stream core0）
- stride=269 cyc，但每窗 384µop 实际覆盖 4557 cyc → 窗口重叠 ~17×。
- 窗口集 label CPI=16.8 vs gem5 全程=2.67（高估 6×），与举例机制一致。

### 为何 compute_int/false_sharing 无此问题
- compute_int：CPI 恒定，两种加权相同 → 无偏。
- false_sharing：CPI 高但均匀 → 几乎无偏。
- stream/chase_dram/phased_mix：CPI 在计算/DRAM 段剧烈跳变 → 严重高估。

### 根本张力
```
按指令撒锚点 → 无采样偏差，但跨核时间错位（原始问题）
按时间撒锚点 → 跨核对齐好，但 CPI 时变负载有采样偏差（新问题）
```
对齐需时间轴，无偏需指令轴——方案A（时间轴）二者不可兼得。

---

# 13. 方案C：CPI 配额自举切窗（无需 commit_tick，并行可行）

突破"对齐 vs 无偏不可兼得"的核心思路：**用上一窗口的 CPI 把指令换算成时间，
给各核动态分配指令配额，使各核覆盖相同物理时间 ΔT，且按指令序连续推进。**

## 13.1 机制

```
假设：窗口 k+1 的 CPI ≈ 窗口 k 的 CPI（局部平滑）
窗口 k 推理完 → 得各核 CPI_c(k)
构建窗口 k+1：
  设共同目标时间宽度 ΔT (cycle)
  每核分配指令数 N_c = ΔT / CPI_c(k)
    CPI 高(慢)核 → N_c 小；CPI 低(快)核 → N_c 大
  各核取接下来的 N_c 条指令（紧接上一窗尾部，连续推进）
结果：各核指令数不同，但都覆盖约 ΔT 物理时间 → 跨核对齐
```

**不需要 commit_tick**：CPI 充当"指令→时间"换算率，由模型自己预测的上一窗 CPI 提供。

## 13.2 同时解决两个问题

- **跨核对齐**：各核覆盖相同 ΔT → `<C0><C1>` 装同一时间段指令 → 争用对齐（保住 false_sharing 收益）。
- **无采样偏差**：按指令序**连续、无重叠、无遗漏**推进（窗口 k+1 紧接窗口 k 尾部），
  每条指令恰进一个窗口 → Σ所有窗口 = 全程 → 全程 CPI 还原无偏（修复 stream 高估）。

方案A在时间轴撒点（重叠+偏向高CPI段）；方案C沿指令轴连续推进（无偏）。

## 13.3 训练 vs 部署

- **训练**：有 gem5 真值 CPI，直接用真值 commit_tick 反推配额（各核累积指令直到耗时≈ΔT），
  一次性切好所有窗口，可并行训练。
- **部署**：无 tick，用**上一窗预测 CPI** 递推配额 → 窗口间串行自举。
- train/deploy gap：训练用真值时间切、部署用预测CPI递推。比方案A的gap小（机制一致），
  仍需关注（可用噪声注入/闭环数据缓解，见 §10.4）。

## 13.4 假设失效与冷启动

- **CPI 突变负载**（stream 计算段→DRAM段）：上一窗配额会让本窗暂时超/欠覆盖 ΔT，
  相变点附近对齐暂时崩；自举几窗后自我修正，误差有界。
- **窗口0冷启动**：无"上一窗CPI"，需初值（假设CPI=1 或全局均值 或固定小配额预热一窗）。
- **配额上下限**：CPI 极高→N_c→0，CPI 极低→N_c 爆 token，需设 N_c ∈ [N_min, N_max]。

## 13.5 chunk 级并行（部署提速）

把"全程数千窗口的串行自举链"砍成 N 段并行：

```
按指令序粗切成 N 个大 chunk（N = 卡数 = 8），每 chunk 一张卡
chunk 之间：并行（8卡同时）
chunk 内部：仍按 CPI 配额自举，顺序切小窗
```

- **牺牲的精度 = chunk 边界冷启动**：每 chunk 首窗无"上一窗CPI"，用初值 → 头几窗配额不准。
- **损失可量化**：受损窗占比 = (N_chunk × m冷启动窗) / 总窗 = m/W_chunk。
  每chunk 500窗、冷启动影响5窗 → 损失 ~1%，换 ~8× 吞吐。
- **chunk 重叠预热（可消除边界损失）**：每 chunk 往前多取一小段（如50窗）只用来暖CPI估计、
  不计入输出。代价是重叠区多算几%。

## 13.6 与既有方案关系

| 方案 | 切窗依据 | 跨核对齐 | 采样偏差 | 需tick | 并行 |
|---|---|---|---|---|---|
| 指令切窗(原始) | 指令数 | 错位 | 无 | 否 | 完全 |
| 方案A(时间锚点) | 全局时间撒点 | 近似 | **有(时变负载)** | 训练需 | 完全(离线) |
| **方案C(CPI配额自举)** | CPI换算指令配额 | 近似 | **无** | 否(部署) | chunk级(~8×) |

方案C ≈ 方案B 的"无tick自举版"，且额外修复采样偏差，**可能是当前最优**。

## 13.7 落地前零成本验证（建议先做）

用 gem5 真值数据，按"固定 ΔT、各核按真值 CPI 分配指令数"切窗，验证三件事：
1. **全程 CPI 还原无偏**：窗口集聚合 CPI vs gem5，确认修复 stream 高估（应从 530% 降到个位数）。
2. **跨核争用对齐**：争用对是否落同窗，确认保持 false_sharing 收益。
3. **自举假设误差**：用真值模拟"上一窗CPI预测下一窗配额"，量化配额偏差；
   并量化 chunk 冷启动边界误差。

通过后再改 build_windows 做正式数据构造与训练。

---

# 14. 方案 C 落地：在线配额规划器 + token-budget 训练切窗（v3）

## 14.1 部署侧（OnlineQuotaPlanner）— 已落地于 [eval_quota_cycles.py](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py)

终点对齐反馈替代固定 ΔT 宽度模式：

```
T_end = max(pred_start_c) + dt_target
n_ideal_c = round((T_end - pred_start_c) / CPI_pred_c)   # 落后核多吃、超前核少吃
T_c = n_ideal_c · (EWMA_tpm_c + λ · σ_tpm_c)             # UCB margin
若 Σ T_c > budget：按超前程度 (pred_start - min) 反向加权 water-filling 削减
n_c = floor(T_c / EWMA_tpm_c)，下界 nmin
```

**关键设计选择**：
- **允许跨核借调**：不限制单核 T_c ≤ budget/N，落后核可"借用"超前核让出的 token，
  跨核同步更敏捷；
- **EWMA + UCB 在线估计 tpm_c**：每窗实测 tokens_used_c / macro_used_c 反馈，
  典型 5–10 窗收敛；
- **slack carry-over**：本窗未用尽的 budget 按 0.5 衰减留给下窗，平滑抖动；
- **冷启动**：第 0 窗各核等分 seed_n，受 budget_per_core / tpm_init 夹钳。

口径统一：`take_macro_window` 按 macro 边界切，`aggregate_pmu` 用 macro CPI，
`pred_start_cycle += pred_cpi · macro_used`。评估 baseline 使用 trace ROI stats，
不再用 `stats.txt` 全程 CPI 作为严格对照。

## 14.2 训练侧（build_samples_quota）— 待落地

**核心原则**：训练分布必须**覆盖**部署 planner 的输出分布——尤其要覆盖借调场景下
某核 token 数显著超过 budget/N 的样本。否则部署给落后核分配大窗时模型会 OOD。

### 14.2.1 切窗算法（B 简化版：独立抖动 + 归一化）

```
total_budget = (max_len - overhead) · 0.95
对每窗：
  ratio_c ~ U(ratio_lo, ratio_hi)    每核独立采样, ratio_lo=0.3, ratio_hi=1.7
  budget_c = total_budget · (ratio_c / Σ ratio_c)    归一化保证总预算不超
  贪心装窗：累加 len(encode_uop) 直到 budget_c，收口到上一条完整 macro 边界
  各核段独立切（不强求跨核 cycle 对齐）
  t_start_rel 用真值 commit_tick 算（oracle 红利）
```

**取值依据**：
- `ratio_hi = 1.7`：覆盖 ~5× 跨核失衡场景（极端 ratio=[1.7, 0.3, ..., 0.3]
  时最大核拿 ~1.7 / (7×0.3 + 1.7) × total_budget ≈ 13800 token，对应 ~445 macro）；
- `ratio_lo = 0.3`：覆盖部署 water-filling 削减后的最小核（ratio→floor 时仍不小于 nmin·tpm）；
- 归一化保证 `Σ T_c = total_budget`，**样本利用率 100%**（无丢弃）。

### 14.2.2 与"贪心装满"路径的取舍

| 维度 | 等分 budget/N + ratio∈[0.5,1.0]（旧） | 归一化 ratio∈[0.3,1.7]（新） |
|---|---|---|
| 单核 token 上限 | budget/N（≈3884） | ~1.7/(7·0.3+1.7) · total_budget（≈13800） |
| 跨核相关性 | 独立 i.i.d. | 反相关（归一化导致） |
| 覆盖部署借调 | **否（OOD）** | **是** |
| 训练总预算利用率 | 0.5–1.0 × N → 50–100% | 100% |

新方案在保留贪心装满精度（直接累加 encode_uop 长度，不依赖 tpm 估计）的同时，
让单核 token 上限随机抖动到 1.7×budget/N，匹配部署借调场景。

### 14.2.3 为什么不上 C（planner-replay）

C 把训练数据切窗也走一遍 [OnlineQuotaPlanner](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py#L206-L298)，
用 oracle CPI 替代 pred CPI。看起来"训练 = 部署 oracle 版"最干净，但：

- **当前模型架构每窗独立 forward**，跨窗状态在 dataloader shuffle 时被打散，
  C 提供的"漂移修正时间相关性"信号会被浪费；
- C 的工程债务大（planner 抽到 common/、训练数据绑定一个 dt_target、调试复杂度 ×2）；
- B 的"独立抖动 + 归一化"已经让训练分布覆盖部署 borrow 场景的 macro 数范围。

C 留作"B 实测 imbalanced workload 仍误差大且 planner 频繁触发 water-filling"时的兜底方案。

## 14.3 落地清单

| 文件 | 改动 |
|---|---|
| data/build_windows.py | `build_samples_quota`：把 `budget_c = ratio · budget_per_core`<br>改为 `budget_c = total_budget · ratio_c / Σ ratio_c`；ratio 范围 0.3–1.7 |
| eval/eval_quota_cycles.py | 不改（保留借调） |
| docs/cross_core_time_anchor.md | 本节 |

## 14.4 验证流程

1. 重新生成训练数据（`--quota-max-len 32768 --quota-ratio-lo 0.3 --quota-ratio-hi 1.7`），
   sanity check `len(tokens)` 直方图（max ≤ 32768，p99 接近 32k）；
2. sanity check 单核 macro 分布：取所有窗口 `core_split` 的 max，应在 ~445 附近
   （而非旧版 ~388）；
3. 32k 重训 ckpt（bs=1 + grad_accum=2）；
4. 部署 [eval_quota_cycles](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py)
   不改任何参数跑全 8 workload，主指标看 pred_vs_ROI，`stats.txt` 全程 gem5 CPI
   仅作为参考口径差异。

## 14.5 ROI stats baseline（当前评估口径）

当前 raw trace 只覆盖 ROI 段，`stats.txt` 的 `numCycles/numInsts` 可能包含
trace 开始前的 setup、启动同步、trace 结束后的 drain/idle 等区间。典型例子是
`W_compute_int`：core0 的第一条 trace commit 已在约 119k cycles 后，但
`stats.txt` 的 core0 `numCycles` 包含这段前缀，因此 full gem5 CPI 比 trace ROI
CPI 高约 8%。

后续评估统一使用 **trace-derived ROI stats** 作为 baseline：

- `ROI instr`：从 rec 字段按 macro head 计数，独立于 lab/commit_tick；
- `ROI cycles`：每核 `max(commit_tick) - min(commit_tick)`，再跨核求和；
- `ROI CPI = Σ ROI cycles / Σ ROI instr`；
- `commit_tick<=0` 只计入 `missing_label_uops` 诊断，不参与 cycles 端点。

代码入口：

- [data/roi_stats.py](file:///data00/yinhaolang/LLMSim/data/roi_stats.py)：共享 ROI stats 计算逻辑；
- [scripts/export_roi_stats.py](file:///data00/yinhaolang/LLMSim/scripts/export_roi_stats.py)：批量导出 workload ROI stats；
- [eval/eval_quota_cycles.py](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py)：进度和最终汇报以 ROI stats 为 baseline，`gem5_full` 仅参考。

导出命令：

```bash
cd /data00/yinhaolang/LLMSim
/root/miniconda3/envs/yinhaolang/bin/python scripts/export_roi_stats.py \
  --raw-root data/raw_train8 \
  --out data/raw_train8/roi_stats.json
```

当前 `data/raw_train8` 的 ROI baseline：

| workload | ROI CPI | ROI instr | ROI cycles | gem5 full CPI | full vs ROI |
|---|---:|---:|---:|---:|---:|
| W_branch_storm | 0.845473 | 3,757,020 | 3,176,460 | 0.878122 | 3.86% |
| W_chase_dram | 6.704204 | 2,025,823 | 13,581,531 | 6.703456 | 0.01% |
| W_compute_int | 0.527544 | 3,344,559 | 1,764,402 | 0.571726 | 8.38% |
| W_false_sharing | 22.657544 | 3,441,069 | 77,966,172 | 22.560817 | 0.43% |
| W_indirect | 1.596529 | 2,743,712 | 4,380,415 | 1.635964 | 2.47% |
| W_int_div | 2.020047 | 2,305,139 | 4,656,488 | 2.063788 | 2.17% |
| W_phased_mix | 4.042254 | 15,511,145 | 62,699,992 | 4.047288 | 0.12% |
| W_stream | 2.639208 | 2,663,553 | 7,029,671 | 2.670768 | 1.20% |
