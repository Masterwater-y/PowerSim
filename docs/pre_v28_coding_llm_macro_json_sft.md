# v28: Coding LLM + Macro Assembly + JSON per-core CPI（纯 SFT）

状态：设计稿。目标是给出**真正落地能训**的一版方案，直接回答一个问题：

- functional trace 转成 macro 反汇编 + system prompt 输入。
- Backbone 换成 coding LLM。
- 输出是 JSON，含每核 CPI（第一版只 CPI，后续可扩到 branch/cache miss）。
- 微调策略：**LoRA + head 全量 + Qwen 原生 embedding 冻结 + 新增极少 delimiter token**，不做全量微调，不做 RL。

结论先行：

- **不做全量微调**。40k-70k 窗口对 0.5B 参数不够，会破坏 Qwen-Coder 的原生代码语义（`mov`/`ld`/`hot`/`seq` 这些词的 embedding 位置就是资产），且显存/时间成本 3-4×。
- **也不做纯 LoRA**。需要少量 delimiter/anchor token（`<QUERY_Ci>`、`<TRACE_BEGIN>` 等）来定位 hidden state，这些新增行必须可训；PMU head 是新任务，也必须全量训。
- **正解是分层解冻**：Qwen 原生 embedding 冻结 + LoRA 挂 backbone attention/MLP + 新 token embedding 行全量 + head 全量。这是"用 LoRA 保持语义，用局部全量学新结构"的组合。
- **只做 SFT，不做 RL**。RL 相对 SFT 的唯一优势是目标不可微；PMU 数值预测目标可微，直接 next-token cross entropy on JSON 即可。RL 是过度设计。

## 1. 与 v24 / v26 的关系

| 版本 | 输入 | 输出 | 训练 | 定位 |
|---|---|---|---|---|
| v24 | macro assembly | regression head → PMU tensor | LoRA SFT | 判别式，保留 head |
| v26 | macro assembly | JSON generation | SFT → GRPO/RLOO | 生成式 + RL，过重 |
| **v28** | **macro assembly + system prompt** | **JSON with per-core CPI** | **LoRA SFT only** | **v24 输入 + v26 输出，去掉 RL** |

v28 是最保守的"生成式 + 语义激活"版本，只保留必要复杂度：

- 保留 native macro assembly（v24 的核心）。
- 保留 JSON 输出（v26 的核心，且用户明确要求）。
- 去掉 RL、去掉复杂 reward、去掉 v23 anti-collapse loss 耦合。第一版把归因保持简单。

如果 v28 精度已经 acceptable，就不需要 v26 的 RL 阶段。

## 2. 模型选择

**首选：Qwen2.5-Coder-0.5B-Instruct**

理由：

- 需要遵循 system prompt 并输出严格 JSON schema，Instruct 版格式遵循能力更强。
- 0.5B 规模对齐当前 Qwen3-0.6B-Base，工程改动最小（`hidden_size`、tokenizer 家族兼容）。
- 5.5T 代码 token 预训练，对 x86-style macro 汇编 token 覆盖度高（v24 probe 已验证 UNK 率 0%）。

**备选**：

- **Qwen2.5-Coder-0.5B-Base + SFT**：如果担心 Instruct tuning 扭曲底层代码表征，作为 ablation。Base 版本走 SFT 也能学会 schema，但需要更多样本或更强 prompt。
- **Qwen2.5-Coder-1.5B-Instruct**：0.5B 语义激活不足时再上，显存/时间约 2-3×。
- **不建议 4B 以上**：当前瓶颈是输入表达和目标结构，不是模型知识量。

## 3. 输入设计

### 3.1 System prompt（固定，约 80 token）

```text
You are a CPU performance model. Read the x86-style macro trace and
per-core summaries. Return only a single valid compact JSON object.
For the target core, predict CPI as integer cpi_m = round(CPI * 1000).
Do not output any text outside the JSON.
```

关键约束：

- **英文**：Qwen-Coder 的 instruction following 主要来自英文语料。
- **固定文本**：每条样本 system prompt 一模一样，训练时可复用 KV cache。
- **cpi_m = round(CPI * 1000)**：小整数比浮点 token 稳定得多。Qwen tokenizer 里 `842` 是 1-2 个 token；`0.842` 会被切成 `0`, `.`, `8`, `4`, `2` 五个 token，且每个都可能 decode 错。
- **禁止 JSON 外文本**：给 constrained decoding 和 parser 一个明确契约。

### 3.2 User prompt 模板（每核一条，local-core）

```text
cfg cores=8 clk=3G l1=32K l2=512K l3=16M rob=192 iq=64 mshr=16

cores
c0 mem=hi br=lo hot=A B
c1 mem=md br=hi hot=C
c2 mem=hi br=lo hot=A B D
c3 mem=lo br=lo hot=E
c4 mem=md br=md hot=F
c5 mem=hi br=hi hot=A G
c6 mem=lo br=md hot=H
c7 mem=md br=lo hot=B

target c2 uops=1030 macros=512 mem=hi br=lo ld=260 st=72 atom=0

asm
add rax rbx
mov rax rbx seq hot A
mov rcx rax seq warm A
imul rdx rcx
mov r8 rdx rnd cold
jne L
ret
```

原则（都是为了让 LLM 用原生词表）：

1. **cfg / cores / target / asm 都是 Qwen 原生 token**，不新增 special token。
2. **不用绝对地址**：`0x7fffff10` 这类数字 token 成本高、跨 run 不迁移。
3. **地址信号靠 pattern tag 和窗口内 alias**：
   - `stride`：`same` `seq` `str` `far` `rnd`
   - `reuse`：`hot` `warm` `mid` `cool` `cold`
   - `alias`：`A`..`Z`, `a`..`z`（每窗口 top-64 hot/shared cacheline 分配短别名，跨核 sharing 靠相同字母表达）
4. **不把微架构 oracle 放进输入**：L1/L2/LLC hit、path_class、mispredict 属于标签，不能进 prompt。
5. **c08/c16/c32 走 local-core**：每核一条 prompt 独立 forward，避免拼 160k 全局 trace。

### 3.3 Delimiter / anchor token

第一版**尽量少加新 token**。只加真正需要的：

| Token | 用途 | 是否必要 |
|---|---|---|
| `<QUERY_C{i}>`（可选） | Head gather 位置 | 生成式方案里可省——用固定 `out\n` 后的位置替代 |
| 其他 | 一律不加 | 是 |

**关键**：v28 是纯生成式，训练目标是让模型在 `out\n` 后 decode JSON，不需要像 v22-v24 那样从中间 hidden gather 出向量。所以 `<QUERY_Ci>` 都可以省。

**结果**：新增 special token = 0 或极少（≤5）。语义激活率 ≈ 100%。

### 3.4 Prompt 拼装（Qwen chat template）

```text
<|im_start|>system
{system_prompt}<|im_end|>
<|im_start|>user
{user_prompt}<|im_end|>
<|im_start|>assistant
```

模型的 completion 部分开始于 `<|im_start|>assistant\n`，训练只对这段计 loss。

## 4. 输出设计

### 4.1 Compact JSON schema

**第一版只输出 CPI**（目标最简，先验证生成式可行）：

```json
{"c0":842,"c1":915,"c2":812,"c3":1200,"c4":730,"c5":905,"c6":880,"c7":760}
```

- key 为 `c{i}`（一个 token）。
- value 为 `cpi_m = round(cpi * 1000)`，整数，范围 100-10000。
- 无空格、无换行，缩短 decode 长度。

**c32 也放同一个 JSON**（32 个 key），预计输出 200-300 token。

### 4.2 输出扩展（后续版本，先不做）

```json
{"c0":{"cpi":842,"br":3,"l1ld":16,"l1st":2,"l2ld":1,"l2st":0,"llc":0},...}
```

先跑通只有 `cpi_m` 的版本，稳定后再扩 branch/cache miss。避免第一版就把 schema 撑爆。

### 4.3 Local-core vs 全窗

两种输出策略：

**A. 全窗输出（推荐第一版）**：

- 一个 prompt 输入所有核的 asm summary（cores 段）+ 一段代表性 asm（比如目标核，或者 pack 所有核）。
- 输出一次 JSON，含所有核 CPI。
- 优点：window 级 rank/spread 天然被目标 sequence 建模；输出量小。
- 缺点：输入长度随核数增长。

**B. 单核输出（v26 风格）**：

- 每核一个 prompt，输出 `{"core":2,"cpi":842}`。
- 外部程序聚合成全窗 JSON。
- 优点：输入短且可并行。
- 缺点：core 间的 rank/cycles 一致性需要额外后处理。

**v28 第一版用 A**，因为要"每核 CPI"就意味着核间关系是评估目标，A 让模型能一次性看到窗口 context 再一次性输出所有核。

## 5. 训练：SFT

### 5.1 训练目标

标准 next-token cross entropy，**只对 completion 部分计 loss**：

```python
loss = CE(logits[assistant_start:], target_ids[assistant_start:])
```

用 HuggingFace `DataCollatorForCompletionOnlyLM` 或自己 mask instruction 部分。

### 5.2 数值 loss 加权（可选，第一版可以先不加）

如果发现纯 CE 训不出稳定 CPI 精度，可以加：

```text
L = L_CE_json
  + λ_num · L_numeric_after_parse
```

其中 `L_numeric` 是 parse 出 CPI 后与 label 的 Huber loss。但这需要**可微 JSON parser + 数值可微 decoding**，工程复杂。**推荐第一版只做 pure CE**，先看单一目标能不能收敛。

### 5.3 微调策略（用户直接问的问题）

**不做全量微调**，**不做纯 LoRA**，**做分层解冻**：

| 参数组 | 策略 | 学习率 | 理由 |
|---|---|---:|---|
| **Qwen 原生 token embedding**（152k 词表） | **冻结** | — | 语义资产，动了就废 |
| Backbone attention `q_proj/k_proj/v_proj/o_proj` | **LoRA rank 32** | 1e-4 | 学"往 PMU 方向偏"的低秩修正 |
| Backbone MLP `gate_proj/up_proj/down_proj` | **LoRA rank 16**（可选） | 5e-5 | 提升容量，第一版可省 |
| **LM head** | **冻结** | — | Qwen 的 tokenizer 输出层，不动 |
| RMSNorm/LayerNorm | 冻结 | — | 常规 LoRA 不训 norm |
| 新增 delimiter token（如果有） | 全量训 | 1e-3 | 从零学 |

**为什么不全量微调**：

1. **数据量不够**：LLMSim 现有 40k 窗口，扩到 c04-c32 也就 70-100k 样本。0.5B 参数全量训在这种量级上必然过拟合。
2. **破坏语义**：全量微调会把 `mov`、`ld`、`hot`、`seq` 这些原生 embedding 拽到 PMU 任务方向，v24 花力气激活的语义就白激活了。
3. **灾难遗忘**：Qwen-Coder 的 JSON 格式遵循能力来自 5.5T 代码语料，全量训一遍会破坏这个能力，反而更容易输出 invalid JSON。
4. **显存 3-4×**：0.5B 全量 bf16 + optimizer state ≈ 70-100GB，需要 FSDP；LoRA r32 只加约 6M 可训参数，40GB 就够。
5. **无收益证据**：v24 分析已论证"LoRA 容量对这个任务足够"——PMU 预测本质是把已有代码理解能力路由到数值输出，是典型 LoRA 场景。

**为什么不纯 LoRA**：

如果用完全纯粹的 LoRA（PEFT 默认），backbone 只挂 adapter，embedding 和 head 都冻结。问题：

- 如果加了新 delimiter token（哪怕就 5 个），embedding 层这几行必须可训，否则新 token 是随机向量。
- LM head 冻结在生成任务里是 OK 的（用 tied embedding），但要保证 tokenizer 词表没扩展。

**结论**：v28 第一版**不加新 token**（第 3.3 节），所以理论上可以做"纯 LoRA + 完全冻结 embedding/head"。但工程上 head/LM head 保持冻结、只挂 LoRA 就够了。

### 5.4 学习率和调度

```text
optimizer     = AdamW
weight_decay  = 0.01
betas         = (0.9, 0.95)
lr_lora       = 1e-4
warmup_ratio  = 0.03
schedule      = cosine to 10% of peak
grad_clip     = 1.0
```

### 5.5 Batch 和步数

```text
global batch  = 32-64 core prompts
grad accum    = 1 或 2
steps         = 8000-12000 (对应 3-5 epoch)
max_input_len = 6000 (local-core 单核 asm 序列 + prompt overhead)
max_new_len   = 300 (c32 JSON 输出上限)
```

### 5.6 数据切分

保持 v22/v23 的原则：

- 90/10 workload 内 train/val。
- Leave-one-workload-out 作为泛化评估。
- c04/c08/c16 训练，c32 作为 OOD core-count 泛化测试。

## 6. 显存和时间估算

**估算前提**：Qwen2.5-Coder-0.5B + LoRA r32、bf16、gradient checkpointing、local-core 5k prompt。

| 配置 | 每 GPU 显存 | 备注 |
|---|---:|---|
| 0.5B LoRA r32, micro-batch 4 | 24-35 GB | A100 40GB 可试，80GB 稳 |
| 0.5B LoRA r32 + MLP LoRA, micro-batch 4 | 30-45 GB | 加 MLP LoRA 后 |
| 0.5B QLoRA r32 | 18-28 GB | 显存紧张时选 |
| 0.5B **full bf16** | **70-100 GB** | 不推荐；需 FSDP |

时间（8 × A100 80GB）：

| 阶段 | 时间 | 备注 |
|---|---:|---|
| Zero-shot gate + tokenizer probe | 0.5 天 | 人工检查 |
| macro window 构建 + tensor cache | 0.5-1 天 | 复用 v24 pipeline |
| SFT 8k-12k steps | 6-12 h | 主训练 |
| Eval c04/c08/c16/c32 全套 | 2-4 h | 生成式推理 |
| **总计** | **2-3 天** | 第一版可完成 |

## 7. 实施顺序

### Phase 0：Gates（1 天）

必须过两个 gate 才继续：

1. **Tokenizer gate**：
   - 跑 `scripts/tmp_macro_tokenize_probe.py` 验证 macro assembly 在 Qwen-Coder tokenizer 下平均 tok/macro ≤ 5，UNK 率 = 0%，单 token 覆盖 ≥ 85%。

2. **Zero-shot semantic gate**：
   - 用 Qwen2.5-Coder-0.5B-Instruct 拿手写 macro trace 问 "What's the dominant bottleneck?"
   - 至少能识别 memory-bound / branch-heavy / compute-bound 这类概念。
   - 不过就换 1.5B 或 DeepSeek-Coder；再不过就放弃 LLM 路线。

### Phase 1：数据管道（3-5 天）

- `data/build_windows.py` 增加 `emit_macro_assembly_prompt` 输出路径。
- 生成 c04 小样本，人工检查 prompt 质量（可读性、token 数、alias 一致性）。
- 全量重建 windows.jsonl（c04/c08/c16/c32 各 workload）。
- 生成 SFT 训练数据：`(system_prompt, user_prompt, assistant_json)` 三元组。

### Phase 2：模型和训练（3-5 天）

- 换 base_model 到 `Qwen/Qwen2.5-Coder-0.5B-Instruct`。
- LoRA 配置：`target_modules=["q_proj","k_proj","v_proj","o_proj"]`, `r=32`, `alpha=64`。
- 使用 `trl.SFTTrainer` 或自定义 CE loop。
- Smoke 训练 500 步验证 loss 下降合理，JSON parse rate ≥ 80%。
- 全量 SFT 8000 步。

### Phase 3：评估（2 天）

评估指标：

```text
valid_json_rate        (>=99% target)
per-core cpi MAE       (in log space)
window cycles WAPE     (aggregated from per-core cpi × uops)
pred_std / label_std   (>=0.7, 不塌缩)
rank accuracy          (核间相对顺序对不对)
leave-one-workload MAPE
c32 OOD MAPE
```

**验收 gate**：

- `valid_json_rate ≥ 99%`：schema 稳定。
- Per-core CPI MAE 不劣于 v22 baseline。
- `pred_std / label_std ≥ 0.7`：没塌缩。

只要过 gate，v28 就算落地成功。不过 gate 再考虑加辅助 loss、扩到 1.5B、或引入 RL。

## 8. 风险和 fallback

| 风险 | 缓解 |
|---|---|
| Zero-shot gate 不过 | 换 Qwen-Coder-1.5B 或 DeepSeek-Coder-1.3B |
| JSON parse rate < 90% | 加 constrained decoding（Outlines / jsonformer） |
| CPI 塌缩到窗口均值 | 引入 v23 的 `L_rank` 作为辅助 loss（parse 后可微） |
| 数值精度不够 | 加数值辅助 loss，或改回 v24 hidden + regression head 路线 |
| c32 OOD MAPE 太高 | 训练加入 c32 数据；或走 v27 uarch conditioning |
| 显存不够 | 换 QLoRA（4bit backbone + LoRA r32） |

## 9. 不做的事

- 不做 RL。目标可微，用 SFT 足够。
- 不做全量微调。破坏语义 + 过拟合 + 显存 3-4×，无收益。
- 不加大量 special token。第一版不加或加 ≤5 个。
- 不把 gem5 oracle 放入 prompt。
- 不第一版就扩到 branch/cache miss，先跑通 CPI-only。
- 不跳过 zero-shot gate 直接训。

## 10. 一句话总结

**v28 = v24 的 native macro assembly 输入 + v26 的 JSON 输出，用 LoRA + head 全量 + 原生 embedding 冻结做纯 SFT。不做 RL，不做全量微调。第一版只输出每核 CPI（cpi_m = round(cpi × 1000)），过 gate 后再扩 miss 类指标。**
