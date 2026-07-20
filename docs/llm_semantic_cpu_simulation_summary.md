# LLM 语义多核 CPU 仿真：方案总览与相关工作总结

状态：推荐主线设计，尚未完成数据契约、消融和闭环验收。日期：2026-07-14。

## 1. 一句话结论

主线不是“让 LLM 读一整段 trace 后直接生成 CPI JSON”，而是：

```text
真实静态汇编语义（Coding LLM）
  + 动态功能执行状态（数值编码器）
  + 显式跨核共享状态（状态机/稀疏图）
  -> 连续 cycles / PMU 回归
  -> 固定功能调度器闭环推进
```

LLM 的职责是解释真实指令、寄存器依赖、控制流和 basic block；它不负责凭空猜出
cache hit/miss、coherence 状态或 DRAM queue。地址、reuse、stride、历史和共享 line
关系保持为结构化输入。训练可端到端反传到 LoRA；部署则缓存静态汇编 embedding，
只在线运行动态、共享状态和回归模块。

## 2. 当前实现与目标方案

### 2.1 当前可运行的 v22 基线

```text
functional uop trace + custom uop tokens/uop fields
  -> Qwen3-0.6B + Q/K/V/O LoRA
  -> 每核尾部 query hidden
  + side_feats
  -> 多 PMU 回归 head
```

当前输入中有 per-window 的共享写统计，但没有真正随窗口演化的 per-line 共享状态。
`v24` native macro 路径把 uop 字段渲染成伪汇编，不能替代从 ELF/PC 恢复的真实汇编。
目前训练入口仍对窗口作 `random_split`，重叠窗口可能跨 train/val，因此只能作为链路和
性能基线，不能作为正式泛化结论的依据。

### 2.2 推荐目标架构

```text
静态字典：
  ELF PC -> 真实 x86 instruction/basic block
         -> Coding LLM + LoRA -> E_static

动态流：
  static_instruction_id -> lookup(E_static)
  + branch/address/reuse/stride/dependency/history/uarch
         -> LocalTraceEncoder -> z_core

共享系统：
  {z_core} + same-line/sync/resource edges + shared-state proxy
         -> sparse graph / Set Transformer -> h_core

输出与闭环：
  h_core -> continuous cycles/PMU heads
         -> scheduler 更新预测时间和下一窗口 shared state
```

这是一套混合 surrogate simulator。它在神经模块内部是端到端可训练的，但不要求对长期
离散状态机和硬调度决策反传梯度。

## 3. 静态语义与动态状态

### 3.1 真实静态汇编语义

静态内容随 binary 固定，由 `macro_pc` 映射到真实反汇编：

```text
mnemonic、operand kind、src/dst 寄存器、immediate/displacement
basic-block、branch target、CFG、静态 def-use 关系
```

例如 `mov rax, [rbx + 64]` 的 `mov`、load 形式、`rbx`、`+64` 是静态语义。LLM
只处理这类 native code tokens；同一 loop body 的 embedding 可复用。

### 3.2 动态功能执行状态

动态内容随每次执行变化：

```text
core/thread/program-order、branch taken、effective address、cache line
load/store/atomic、access size、reuse distance、stride、producer distance
branch/memory history、sync event、typed uarch profile
```

对于同一条 `mov rax, [rbx + 64]`，`rbx` 的实际值、访问 line、reuse 和是否有远端
写者都可能不同。这些字段不应全部转为 BPE 字符串，而应由 NumericEncoder 编码。

两条通道会在每个动态 macro 处融合：

```text
E_static[static_instruction_id] + E_dynamic[functional fields]
  -> local execution embedding
```

## 4. 显式跨核共享状态

目标状态只由功能可见信息更新：`core_id`、`cacheline_paddr`、load/store/atomic 和
功能顺序或预测时间顺序。它对每条 line 维护轻量代理：

```text
owner, sharers, last_writer, last_touch_core, last_touch_seq, touch_count
```

在访问前导出 owner relation、sharer count、remote-writer、conflict、distance-since-touch
等 bucket；在访问后更新 owner/sharer，并维护 per-core/global EMA 压力特征。它不是
完整 MESI，也不模拟 cache 容量、替换、MSHR、directory 或 DRAM queue。

当前 TSim 的 `SharedStateFeatureEngine` 已实现这种代理。离线按真实 `commit_tick`
构造的 teacher state 只能作为 upper bound；正式训练/评测应使用 cold/warm 功能 replay，
或使用前一窗口预测 cycles 产生的事件交错重放。原始物理地址只在状态机内部用于判断
同一 line，不直接输入模型。

## 5. 数据集契约

正式数据不能是单一 JSON 窗口文件，而应分五类工件：

| 工件 | 核心字段 | 作用 |
|---|---|---|
| `run_manifest` | binary hash、flags、seed、uarch、core/thread、warm policy、sim commit | 生成不可泄漏的 `run_id` 与 split |
| `static_dictionary` | PC、bytes、真实 asm、operand、basic block、CFG | 供 LLM 编码，按 binary 去重 |
| `dynamic_macro_stream` | core/seq/static id/branch/address/reuse/stride/dependency/sync | 功能输入 |
| `label_stream` | cumulative retirement cycles、PMU cumulative counters、provenance | 通过前缀差分生成标签 |
| `window_manifest` | target/history/interacting-core macro ranges、track、offset | loader 索引与评测定义 |

窗口按固定退休 macro 数切分，例如 `M in {256, 1024, 4096}`；而不是由真实
`commit_tick` 定义边界。标签由累计差分得到：

```text
cycles[b,e) = T_retire[e] - T_retire[b]
PMU[b,e)    = C_pmu[e] - C_pmu[b]
```

以下只可作为标签，禁止进入 prompt 或 numeric side channel：

```text
commit/fetch/issue/complete tick
cache hit/miss、path class、MESI/coherence oracle
MSHR occupancy、stall reason、真实 mispredict 结果
```

split 必须在 window 化前按 `run_id` 完成；评测至少有 workload-family OOD、uarch OOD、
joint OOD，以及 cold/warm/carried-state 和 closed-loop 两个榜单。

## 6. 训练、梯度与部署

### 6.1 梯度路径

LoRA 训练时，对 batch 内去重的静态 block 重新运行 LLM：

```text
loss -> PMU head -> cross-core module -> local encoder
     -> E_static[index] -> LLM hidden -> LoRA A/B
```

同一个 static block 在多个动态位置出现时，autograd 会在 `E_static[index]` 的 gather
位置累加梯度。静态 embedding 在 LoRA 训练中不能使用 detached cache；可缓存的是
反汇编、tokenization、索引。LoRA 收敛并冻结后，才导出 `E_static` 用于部署。

离散 shared-state update 和按预测时间排序默认 stop-gradient。训练先做单窗口监督；
闭环时只展开少量窗口做 truncated BPTT，定期 detach。

### 6.2 LoRA 和输出头

推荐比较而非预设唯一超参数：

1. frozen Coding LLM + trainable pooling/numeric/cross-core/head；
2. attention `Q/K/V/O` LoRA；
3. all-linear LoRA：再加 MLP `gate/up/down`；
4. 只有前三者 group-val 都欠拟合且语义 gate 成立时，解冻最后若干层。

主输出为连续多头：每核 `log cycles-per-macro`、CPI 与 branch/cache/TLB/DRAM/coherence
辅助指标。损失先以 per-core cycles 和可加 window cycles 为主，逐步加入 per-core
relative spread/rank、PMU、rollout 和轻量物理约束。具体权重必须由 group/OOD 消融决定。

## 7. RAG 的位置

RAG 不在每个窗口检索相似 trace 或相似标签。其唯一推荐用途是离线从 gem5 config、
源码、RTL、手册中抽取并校验 typed uarch profile：

```text
documents -> retrieval -> schema extraction -> deterministic verifier
          -> canonical text prefix + normalized numeric vector
```

uarch 文本帮助 LLM 对齐机器语义；数值 profile 通过 FiLM/cross-attention 进入动态和
跨核模块。对已存在 machine-readable YAML/JSON 的配置直接 parse，不需要 RAG。

## 8. 相关论文与可采纳经验

| 工作 | 如何使用 LLM | 如何回归 | 对 LLMSim 的结论 |
|---|---|---|---|
| RLM for Code | 冻结 T5Gemma encoder 读代码/ONNX | 科学计数法数字 decoder、约束采样、聚合 | 建立生成式 `RLM-CPU` 对照；不替代动态状态模型 |
| Omniwise | LoRA LLaMA 读 HIP kernel、GPU/flags | JSON performance counters | 采纳代码 x 配置数据扩增和多指标规范化；其静态 kernel 问题较简单 |
| Time-LLM | 数值 patch 经 reprogrammer 对齐到冻结 LLM | projection 连续 forecast | 数值应经 adapter 而非全文本化；做为 dynamic-projector 对照 |
| TP-BERTa | 字段名语义与 magnitude embedding 解耦 | `[CLS]` + MSE | 采纳字段内融合、单位/type/bucket；禁用全数据 target-aware 分桶 |
| TabuLa | row 文本化、row-causal mask、全量微调 | 分类/分桶生成 | 采纳数据清洗、分组和 contamination 控制；不照搬其数据规模假设 |
| Nova | 真 x86、instruction summary、层级 attention、对比学习 | 汇编理解/检索 | 静态语义主线最直接的参考；先做真实汇编与 semantic gate |
| ASMA-Tune | asm encoder -> projector -> LLM | 文本生成 | 独立结构 encoder/projector 是可选备案，不是 timing 解法本身 |
| GenRe2 | 编码器 + 数字 decoder | CE 后用数值序列 reward RL | 若 RLM-CPU 有收益，再比较 sequence-level reward |

### RLM 与 LLM 的关系

LLM 是通用语言/代码 backbone；RLM（Regression Language Model）是将它训练为
`text/code -> number` 的任务范式。RLM for Code 以 frozen encoder 和数字 decoder
预测 memory/latency；它不是一个能自动模拟多核动态状态的新模型类别。

### 为什么生成式 RLM 只是对照

生成数字可以表达多指标间相关性和不确定性，但 token CE 不直接对应连续数值误差，
还引入 decode/sample/parse 成本。GenRe2 用 sequence-level RL 缓解此问题，说明该
路线值得严格比较；但现有论文没有证明它可稳定处理多核共享状态的长期闭环。

## 9. 实验门槛和执行顺序

1. **数据门槛**：完成真实反汇编、固定 macro chunk、可加标签、run-level split 和
   metadata/leakage probes。
2. **语义门槛**：在单核上，`real asm + pretrained Coding LLM` 必须优于 pseudo asm、
   mnemonic shuffle、random-init 和 side-only；否则不扩大 LLM。
3. **回归门槛**：同输入、同 split 比较 continuous head 与 RLM-CPU；生成式路线必须
   报告 parse rate、sample 数、decode latency、MIPS 和 rollout drift。
4. **多核门槛**：比较 local-only、cross-core graph、显式 shared-state hybrid；要求
   per-core cycles、window additivity、spread/rank、tail、OOD、rollout 与 MIPS 同时过关。
5. **部署门槛**：静态 embedding cache 或蒸馏后的吞吐不能低于仿真器目标；否则 LLM
   只保留为离线 teacher/语义分析器。

## 10. 当前决策

- **主线**：真实汇编 Coding LLM 语义编码 + numeric dynamic encoder + explicit shared
  state/cross-core module + continuous cycles/PMU head。
- **保留基线**：当前 v22 custom-token Qwen、TSim structured model、deterministic
  shared-system hybrid、side-only/tabular model。
- **对照路线**：RLM-CPU 数字生成；仅当其严格 OOD/rollout/MIPS 表现全面胜出时改主线。
- **尚未冻结**：具体 Coding LLM、LoRA target/rank、loss 权重、cross-core 图结构和
  state engine 精度，均以无泄漏数据上的消融结果决定。

## 11. 详细文档

- [完整多核方案与数据审计](llm_multicore_cpu_simulation_blueprint.md)
- [LLM 领域回归论文逐篇审计](llm_domain_regression_literature_review.md)
- [当前 v22 已落地数据与模型设计](current_dataset_model_design.md)
- [当前共享系统设计与实验记录](shared_system_design.md)

### 论文链接

- [RLM for Code](https://arxiv.org/html/2509.26476)
- [Omniwise](https://arxiv.org/html/2506.20886)
- [Time-LLM](https://arxiv.org/abs/2310.01728)
- [TP-BERTa](https://arxiv.org/html/2403.01841)
- [TabuLa](https://arxiv.org/html/2406.12031)
- [Nova](https://arxiv.org/html/2311.13721)
- [ASMA-Tune](https://arxiv.org/html/2503.11617)
- [GenRe2](https://arxiv.org/html/2512.06533)
