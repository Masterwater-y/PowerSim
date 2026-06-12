# LLMSim 方案设计与审核

> 本文档对"窗口级 functional trace -> 多核 PMU 向量"LLM 方案做完整审核，标出潜在风险与改进项。

---

## 1. 方案速览

```
[functional µop window, multi-core interleaved]   ──>  LLM (LoRA) ──>  [N_core × K_pmu]
```

- 输入：W=1024~2048 条 retired µop，按全局 commit_seq 多核交错
- 输出：窗口内每核 K 维 PMU 聚合
- 约束：输入只含架构态，微架构态全部进 label

## 2. 方案是否合理

### 2.1 合理性论据

1. **物理可学性成立**：在固定 µarch 下，`functional trace -> PMU` 是确定性映射（gem5 确定性模拟可证）。LLM 学的是这个映射的近似。
2. **监督密度合适**：一窗给 N×K ≈ 4×16 = 64 个标量监督，远高于"整 trace -> 整 PMU"的 1 标量监督。
3. **窗口推理一次出**：契合 LLM seq-in / vector-out 接口，部署成本低（一窗一次 forward）。
4. **多核交错保留耦合**：跨核冲突（共享行、bus contention）由相邻位置的 `<CORE i>` token 显式表达，attention 可学。

### 2.2 风险点 / 不充分之处

下面是审核出的**真实存在的设计漏洞**，必须在落地前补齐。

---

## 3. 必须修正的问题

### P0-1. **窗口边界本身是微架构态依赖的**

> 风险：用"W 条 retired µop"切窗，但"哪 W 条按什么顺序退休"取决于 OoO 调度，**这本身已经是微架构标签**。把这个顺序当输入会泄漏。

**修正**：
- 改用**程序序窗口**：每个核取自己 `pos_in_thread ∈ [t, t+W)` 的连续 µop。
- 多核之间不强制对齐 retire 顺序，只对齐**逻辑时间起点**（通过 barrier / 同步原语 / 起始 PC）。
- 每核窗口长度可不同；输入 token 串里仅按"core 内程序序"展开，跨核拼接顺序固定（C0 全部 + C1 全部 + ...）或交错按 program-order 但**不按 commit-tick**。

### P0-2. **多核交错的"全局序"不能是 commit_seq**

`commit_seq` 与 cycle 强相关，本质是 µarch 输出。输入序列里如果 token 顺序是按 commit 排的，那"先后"就泄漏了 cycle 比较关系。

**修正**：
- 跨核拼接采用**程序序 + 显式 sync 锚点**：
  - 每核独立按 `pos_in_thread` 排序
  - 在共享内存访问 / atomic / barrier 处插入 `<SYNC sid>` token，对齐多核同步事件
  - 没有 sync 时，多核 token 简单按 `core_id` 段串接（C0_segment ‖ C1_segment ‖ ...）
- 让 attention 自学多核耦合，不喂进任何 µarch 时间。

### P0-3. **vaddr / paddr 的可见性需谨慎**

- `vaddr` 是架构态、可见，OK。
- `paddr` 由 OS 页表决定，是架构 + OS 映射，**严格说不是 ISA 可见**，但 ISA 可见的 access 会触达 paddr。
- 但是 `paddr` 与 cache 行为强耦合（决定 set index），喂 paddr 等于半泄漏 cache 命中信号。

**修正**：
- 默认**只用 vaddr**（页大小内 line bucket、页 bucket）。
- `paddr` 作为**可选 conditioning**，并在配置里显式标记 `use_paddr: bool`，跑两组实验对比是否破坏泛化。
- 训练集若混合"OS 不同页表分配"的样本，paddr 反而会引入噪声，进一步支持默认关掉。

### P0-4. **Label 中的 cycles 与窗口长度强相关，回归目标病态**

直接回归 `cycles` 会让模型学一个近似线性 `cycles ≈ α · W`，PMU 之间的细节淹没在 W 的规模里。

**修正**：
- 主目标改为 **CPI = cycles / instructions_retired**（回归到 [0.1, 5] 量级）
- `cycles_pred = CPI_pred × W_core`（W_core 由输入直接数出，无监督泄漏）
- `branch_miss / branch_count`、`l1d_miss / loads` 等也改成**比率头**，绝对计数从输入计数 × 比率反算
- 真正用绝对计数监督的，仅保留稀有事件（如 `inv_recv`、`itlb_miss`）

### P0-5. **配置 conditioning 的泄漏面**

把 cache size、MSHR 大小作为 token 喂进去本身没问题，但若数据集只有 1~2 个配置，模型会把"配置 hash"当快捷键，PMU 头退化为查表。

**修正**：
- 至少 4~8 个不同 µarch 配置混训
- 对每条样本随机 mask 部分 cfg token（dropout 50%），逼模型从 trace 学
- 评估必须含 leave-one-config-out

### P0-6. **多核 N 不固定时回归头维度问题**

`[N_core, K_pmu]` 的 N 若变化（2/4/8 核），固定输出头会爆维度。

**修正**：
- 把回归头做成 **per-core query**：序列尾部追加 N 个 `<PMU CORE_i>` token，每个独立过同一 head（共享权重） -> [K]
- 训练时 N 可变，推理时按需追加查询 token

---

## 4. 应当改进的二级问题

### P1-1. **窗口大小的偏差/方差权衡**

- W 太小：PMU 计数稀疏（branch_miss 常 0~1），监督噪声大
- W 太大：上下文超 LLM 限制 + 长程依赖学不动

**建议**：
- 主实验 W=1024
- 多尺度训练：同时构造 W∈{256, 1024, 4096} 三档样本，模型学不变性
- 推理时按部署需要选档，或拼接多窗预测求和

### P1-2. **Tokenizer 必须自定义，BPE 会切碎数字**

如果直接用 base LLM 的 tokenizer 处理 hex 地址 / 数字桶，单条 µop 可能被切成 20+ token，序列长度爆炸。

**建议**：
- 自定义 vocab：每条 µop 严格 8 token
- 通过 `add_special_tokens` 把这些 token 注入 base LLM tokenizer
- embedding 层 resize；新加 token 的 embedding 用小高斯初始化

### P1-3. **回归头与 LM head 的关系**

LLM 的 LM head 训练用不到，浪费参数；但完全丢弃会破坏预训练知识迁移。

**建议**：
- 默认冻结 LM head，只训 LoRA + regression head
- 可选 multi-task：保留少量 next-token CE 在 trace 段做"自我预测"（auxiliary），帮助 attention 收敛

### P1-4. **Loss 加权与稀有 PMU**

`branch_miss` / `inv_send` 在常规 workload 中均值很低，log1p 后仍有数量级差。

**建议**：
- per-PMU 自动学 log_var：`L = Σ exp(-log_var_k) · L_k + log_var_k`（uncertainty weighting）
- 或按训练集统计的 std 做静态归一化

### P1-5. **数据增广**

- 对寄存器号做 random permutation（保持依赖结构不变，破坏绝对寄存器 id 的过拟合）
- 对 vaddr 做 page-level random remap（页内偏移保持，页号 hash 重排）—— 强迫模型学相对模式
- 窗口 50% 重叠采样

### P1-6. **物理 invariance 校验**

模型预测的 PMU 必须满足若干硬约束，应作为 sanity loss：
- `branch_miss ≤ branch_count`
- `l1d_miss ≤ loads + stores`
- `cycles ≥ instructions_retired / max_IPC`

可加 hinge penalty 项。

### P1-7. **跨 workload / 跨 ISA 的可迁移性**

只在 4 个 microbench 上训，模型几乎肯定不可泛化到 SPEC、AI workload。

**建议**：
- 训练集必须含 ≥ 16 类 workload（覆盖 compute-bound / memory-bound / branch-heavy / coh-heavy）
- 留 4 类做 leave-one-workload-out 验证

### P1-8. **基线对比**

LLM 不一定比小模型好。**必须**有一个非 LLM 基线：
- Baseline-A: 直接对窗口做手工特征（指令类型直方图 + 地址熵 + 分支密度）+ XGBoost
- Baseline-B: 1D-CNN + Transformer encoder（小模型，从 scratch 训）
- Baseline-C: 本方案 LLM+LoRA

只有 C 显著优于 A/B 才证明 LLM 路径有价值。

---

## 5. 落地阶段建议

### Phase 0：可行性验证（最小集）
- 1 个配置、4 个 microbench、单核
- W=1024, K=4 (cycles, brmiss, l1d_miss, llc_miss)
- 不用 LLM，先用 Transformer-from-scratch（6 层 256d）
- 目标：cycles MAPE < 20%

### Phase 1：LLM 上线
- 同样数据，换 Qwen2.5-1.5B + LoRA
- 自定义 tokenizer
- 看 LLM 是否带来明显提升

### Phase 2：多核 + 多配置
- N=4, 4 配置混训
- 引入 per-core query token
- 评估 leave-one-config-out

### Phase 3：泛化扩展
- 16 workload
- 多尺度 W
- 物理 invariance loss

### Phase 4：部署
- ONNX/INT8 量化
- 与 cycle-accurate sim 做端到端 Pareto 对比

---

## 6. 是否需要 LLM 这把"重锤"？

**坦诚结论**：本任务（窗口 PMU 回归）的 LLM 价值**非充分**。

- LLM 的核心增益是 NL 先验、长程语义、零样本任务切换
- 而 µop trace 是合成域、与自然语言无关，LLM 预训练知识迁移有限
- 真正决定精度的是：tokenizer 设计 + 物理约束 + 数据多样性
- 一个 8~12 层、d_model=512 的从零训 Transformer 多半就够

**给上层的真实建议**：
1. Phase 0 先做小 Transformer，作为可信基线
2. LLM 路径作为对比，不是默认方案
3. 若坚持 LLM，最大价值不在精度而在"统一接口可对话"——那应该把 task 设计成"问答式"：
   `Q: predict cycles for this trace under config X. A: 1832`
   这样才能复用 LLM 的指令跟随能力

---

## 7. 改进项清单（可执行 todo）

- [ ] P0-1: 窗口切法改"程序序窗口"，弃用 commit_seq
- [ ] P0-2: 多核拼接序去 µarch 时间
- [ ] P0-3: 默认关 paddr，加开关
- [ ] P0-4: 主目标改 CPI / ratio
- [ ] P0-5: ≥4 配置混训 + cfg token dropout
- [ ] P0-6: per-core query token 支持变 N
- [ ] P1-2: 自定义 tokenizer
- [ ] P1-4: uncertainty weighting
- [ ] P1-5: 数据增广（reg perm + page remap）
- [ ] P1-6: 物理 invariance loss
- [ ] P1-7: ≥16 workload，含 leave-one-out
- [ ] P1-8: 必须有 XGBoost / small-Transformer 基线
