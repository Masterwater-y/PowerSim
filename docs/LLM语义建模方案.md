# LLM 语义多核 CPU 仿真：历史方案与相关工作总结

状态：**已被 2026-07-17 的 TCSim v29 接口审查取代，不再是实现依据。**
新的权威设计见
[LLMSim × TCSim v29：256-macro 原生 Token 方案](llmsim_tcsim_v29_macro_native_token_design.md)。
本文保留相关工作与早期设计背景；其中标量 CPI、`EpsilonResidentScheduler` 和当前
Phase 0/1 gate 均不得作为正式主线或验收标准。256-macro 的新定义以权威方案为准。

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
  ELF PC(macro) -> 真实 x86 macro instruction/basic block
                -> Coding LLM + LoRA -> E_static

动态流（macro 单位）：
  static_instruction_id -> lookup(E_static)
  + branch/address/reuse/stride/dependency/history/uarch (macro 级聚合)
                -> LocalTraceEncoder -> z_core

共享系统：
  {z_core} + same-line/sync/resource edges + shared-state proxy
                -> sparse graph / Set Transformer -> h_core

输出与闭环（TCSim 对齐）：
  h_core -> cpi_macro/PMU heads -> Δ̂ = pred_cpi_macro * n_macros
        -> tcsim.scheduler.epsilon_resident.EpsilonResidentScheduler
        -> E_pred = T_pred + Δ̂, 更新 resident/exposure, exactly-once commit
```

**切窗单位：macro，不是 uop**。理由：Coding LLM 预训练分布是 macro 级真实
x86 指令；uop 只是 gem5 解码内部产物，缺乏语义样本；且 macro 边界
(`is_last_microop=1`) 才是可加 cycles 标签的天然定义。macro 内的 uop
序列只作为动态 side channel 聚合进 `E_dynamic`，不占 LLM position。

**推理器：直接同步 TCSim** `EpsilonResidentScheduler + fixed_chunk chunker`
(`/data00/yinhaolang/TCSim/tcsim/{chunker,scheduler,inference}`)。训练/推理
使用同一 chunker，边界只依赖功能序 (`is_last_microop=1` 计数)，绝不依赖
真实 `commit_tick`。Predictor 契约：`(core_id, macro_chunk, state, sample_ctx) -> Δ̂ cycles`。

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

两条通道会在每个动态 macro 处融合。首版推荐把融合方式写死为“维度对齐后相加”，
避免只写抽象的 `+` 号而无法落地：

```text
asm_text/static_info
  -> Coding LLM/LoRA -> E_static_raw[static_instruction_id]

E_static = MLP_static(E_static_raw)          # 对齐到模型维度 D
E_dynamic = DynamicEncoder(functional fields) # 也输出 D 维

local_execution_embedding =
  LayerNorm(E_static[static_instruction_id] + E_dynamic)
```

这里的 `E_static_raw` 可能来自不同 Coding LLM，维度不一定等于 TSim 主干的 `D`，因此不能
直接与原有 UOP/trace embedding 相加。必须先通过 `MLP_static` 或 `Linear+Norm` 投影到
统一维度。`E_dynamic` 仍由原来的离散字段 embedding、数值字段编码和 shared-state 预特征
生成；每核 `side_feats` 和全局 `global_feats/uarch_feats` 可继续按 v26/v27 方式经 MLP
升维后加到该 core 的所有 local execution embedding 上。

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

窗口按固定退休 macro 数切分：**首版固定 `K_macro = 256`**，与 TCSim
`configs/mvp_100m.yaml::chunk.K=256` 数值对齐（单位从 uop 换成 macro）。
后续再做 `M ∈ {256, 1024, 4096}` 的多档 ablation。边界只由 `is_last_microop=1`
计数决定，不由真实 `commit_tick` 定义。标签由累计差分得到：

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

### 5.1 v28_1 workload 清单与 split 表

数据源：`/data00/yinhaolang/TSim/data/raw_v28_1_business_a2_sharedzipf_seed{0,1}_c{01,04,08,16,32}`。
共 23 个 workload，按 TCSim `configs/v28_business_workloads.json` 已切成
Train（16）+ Heldout（7）：

- **Train (16)**：`W_v28_int_alu_dense / int_div_serial / fp_alu_dense / simd_sse_dense /
  cache_L1_mixed / cache_L2_mixed / memory_seq_moderate / memory_random_mlp /
  coh_readmostly_sparse / marine_base / gofeed_base / flink_base / mysql_base /
  redis_base / pytorch_base / bvc_encoder_base`。
- **Heldout (7)**：`W_v28_marine_heldout / gofeed_heldout / flink_heldout /
  mysql_heldout / redis_heldout / pytorch_heldout / bvc_encoder_heldout`。

Split 契约（按 `run_id = sha1(workload, binary_hash, seed, cores, uarch_hash)` 打标签）：

| Split | 组合 | 目的 |
|---|---|---|
| `train` | `seed0 × {c01,c04,c08,c16,c32} × Train16` | LoRA 主训练；**c32 seed0 直接进 train，直接监督核数扩展** |
| `family_ood` | `seed0 × {c04,c08,c16,c32} × Heldout7` | 方案 §5 program family OOD |
| `seed_ood` | `seed1 × {c04,c08,c16,c32} × Train16` | seed 泛化 |
| `sealed_joint_ood` | `seed1 × {c04,c08,c16,c32} × Heldout7` | family × seed 双 OOD；消融跑完前不动 |

注：`seed1` 目录缺 `c01`（`raw_v28_1_business_a2_sharedzipf_seed1_c01` 不存在），
seed_ood/sealed_joint_ood 因此不包含 c01。任何 split 都必须在窗口化前按
`run_id` 完成；评测至少有 workload-family OOD、uarch OOD、joint OOD，以及
cold/warm/carried-state 和 closed-loop 两个榜单。

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

### 6.3 推理侧同步 TCSim EpsilonResidentScheduler

首版推理器直接同步 TCSim 方案，避免自研 planner 分布偏移：

- **调度器**：`tcsim.scheduler.epsilon_resident.EpsilonResidentScheduler`
  (`/data00/yinhaolang/TCSim/tcsim/scheduler/epsilon_resident.py:68`)。
- **参数（与 `configs/mvp_100m.yaml` 对齐）**：`epsilon=2048.0 cycle`、
  `max_resident_exposure=256`、`max_forward_budget=0`（0 表示不限）。
- **Chunker**：`tcsim.chunker.fixed_chunk` 的 `build_trace / _aligned_chunks_and_labels /
  _anchor_labels_to_roi` 骨架，唯一改动是分块条件由「uop 累计到 K」改成「macro
  累计到 K_macro=256」。
- **Predictor 契约**：`(core_id, macro_chunk, state, sample_ctx) -> Δ̂ cycles`，
  `Δ̂ = pred_cpi_macro × chunk.n_macros`。首版 `pred_cpi_macro` 由现有
  `cpi_uop` head 通过 `cpi_macro = cpi_uop × uops_per_macro` 换算得到，
  等 static_dict + real asm 通道接上后再上专用 `cpi_macro` head。
- **exactly-once commit**：slow 核 chunk 保持 `resident=True`、`exposure+=1`，
  达到 `max_resident_exposure` 强制 fast；`_committed_keys` 保证每个 chunk
  提交且只提交一次。指标口径复用 `tcsim/inference/deployment.py::_summary`
  (chunk_cpi_mape、prefix/endpoint/makespan cycle error、resident exposure、
  exact_once_committed)。

## 7. RAG 的位置

首版暂不引入 RAG。当前方案还没有明确的可检索语料、字段 schema 和校验器，强行加入
RAG 容易把问题变成“检索了什么、是否泄漏标签、抽取是否可靠”的额外变量。

如果后续补齐资料库和 schema，RAG 也不应在每个窗口检索相似 trace 或相似标签。它只可
作为离线配置抽取工具：从 gem5 config、源码、RTL、手册中抽取并校验 typed uarch
profile：

```text
documents -> retrieval -> schema extraction -> deterministic verifier
          -> canonical text prefix + normalized numeric vector
```


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

1. **数据门槛（Phase 0，本次交付）**：完成真实反汇编、固定 macro chunk、可加标签、
   run-level split 和 metadata/leakage probes。具体脚本：
   - `data/build_static_dict.py`：`workloads/bin/v28_*` → `PC → real asm`，
     随机 1000 macro 校验 objdump 一致率 = 100%。
   - `data/build_v28_1_macro_chunks.py`：fork TCSim `chunker/fixed_chunk.py`，
     以 `is_last_microop=1` 计数到 `K_macro=256` 分块；strict `sum(delta_ticks)==endpoint`。
   - `data/build_v28_1_manifest.py`：按前述 split 表打 `train/family_ood/seed_ood/sealed_joint_ood`。
   - `data/leakage_probes.py`：input allowlist、metadata-only GBDT R² < 0.30、target-shuffle 退化。
   - `eval/eval_fixed_chunk.py`：直接调用 `tcsim.scheduler.epsilon_resident.EpsilonResidentScheduler`
     替代 `OnlineQuotaPlanner`；`OnlineQuotaPlanner` 保留作为回归对照。
   - 顶层 one-shot：`scripts/run_v29_phase0_data_contract.sh`。
2. **语义门槛**：在单核上，`real asm + pretrained Coding LLM` 必须优于 pseudo asm、
   mnemonic shuffle、random-init 和 side-only；**同时增加 `LLM-only` 消融，只输入
   `MLP_static(E_static_raw)` 后的静态语义向量，不加入动态字段、side/global 和 shared-state
   特征，用来判断 LLM embedding 本身是否含有可预测 cycles/PMU 的信息。若 `LLM-only`
   不优于随机静态向量或 PC/hash baseline，则不扩大 LLM。**
3. **回归门槛**：同输入、同 split 比较 continuous head 与 RLM-CPU；生成式路线必须
   报告 parse rate、sample 数、decode latency、MIPS 和 rollout drift。
4. **多核门槛**：比较 local-only、cross-core graph、显式 shared-state hybrid；要求
   per-core cycles、window additivity、spread/rank、tail、OOD、rollout 与 MIPS 同时过关。
5. **部署门槛**：静态 embedding cache 或蒸馏后的吞吐不能低于仿真器目标；否则 LLM
   只保留为离线 teacher/语义分析器。

## 10. 当前决策

- **主线**：真实汇编 Coding LLM 语义编码 + numeric dynamic encoder + explicit shared
  state/cross-core module + continuous cycles/PMU head。
- **首版融合**：`E_static_raw` 经 `MLP_static` 对齐到模型维度，和 `DynamicEncoder`
  输出的 `E_dynamic` 相加并归一化，形成每个动态 macro 的 local execution embedding。
- **RAG 决策**：首版不做 RAG；先使用已有结构化 uarch/config 字段。只有补齐可检索语料、
  schema、校验器并证明跨配置收益后，再作为离线 typed uarch profile 抽取工具加入。
- **保留基线**：当前 v22 custom-token Qwen、TSim structured model、deterministic
  shared-system hybrid、side-only/tabular model。
- **对照路线**：RLM-CPU 数字生成；仅当其严格 OOD/rollout/MIPS 表现全面胜出时改主线。
- **尚未冻结**：具体 Coding LLM、LoRA target/rank、loss 权重、cross-core 图结构和
  state engine 精度，均以无泄漏数据上的消融结果决定。

## 11. 详细文档

- [当前权威方案：LLMSim × TCSim v29 256-macro 原生 Token](llmsim_tcsim_v29_macro_native_token_design.md)
- [256-macro 实现审查与初步验证](macro_v29_implementation_audit_20260717.md)
- [历史方案：LLMSim × TCSim v29 256-UOP 静态语义适配](llmsim_tcsim_v29_semantic_adapter_design.md)
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
