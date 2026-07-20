# v26: Native-token Macro Assembly + JSON/GRPO 方案评估

状态：设计稿。目标是回答一个更激进的问题：能否让 LLMSim 真正使用 LLM
预训练语义，把 trace 渲染成 LLM 原生 tokenizer 友好的 macro 汇编/代码文本，
再让模型直接生成每核 PMU JSON，并用强化学习微调。

结论先行：

- **输入侧可行，且值得做**。Macro Assembly 是当前最有机会激活 coding LLM
  语义先验的路径；绝对地址不要直接输入，应该输入功能性访存 pattern 和少量
  窗口内地址别名。
- **直接 JSON 生成可行，但不是最高性价比的一版**。JSON 生成会引入数值 token
  难学、格式错误、输出 token 延迟和 RL 方差；它应该先经过 SFT，再用小步
  GRPO/RLOO 优化最终指标，而不是一上来纯 RL。
- **不建议第一版全量微调 LLM 参数**。先做 LoRA/QLoRA，冻结原生 embedding，
  保留代码语义；全量微调只有在 LoRA 明确受限后再上 FSDP/ZeRO。
- **资源上可做**。Qwen2.5-Coder-0.5B 级别，8 x A100 80GB 上 LoRA SFT
  约 6-12 小时，JSON-RL 约 12-36 小时；全量 0.5B 微调需要 80GB 级显存且
  工程复杂度明显上升。

## 1. 与已有路线的关系

已有路线分工：

| 版本 | 输入 | 输出 | 目的 |
|---|---|---|---|
| v22 | custom special uop token | regression head | 当前 baseline，LLM 语义激活约 0% |
| v23 | 同 v22 | regression head | 修复 per-core CPI 塌缩 |
| v24 | native macro assembly | regression head | 激活 coding LLM 语义，但仍是判别式回归 |
| v25 | custom special uop token | regression head | 自训 Transformer，验证 Qwen 是否有价值 |
| **v26** | **native macro assembly + prompt** | **JSON generation** | 验证生成式结构化输出 + RL 是否有额外价值 |

v26 不能替代 v23。Per-core CPI 塌缩来自目标和 head 结构，换成 JSON 输出后
仍然可能把所有 core 输出成均值。因此 v26 的 reward 必须显式包含 v23 的
rank/spread/cycles 约束。

v26 也不能替代 v24。v24 是更稳的语义激活版本：输入同样 native-token 化，
但输出仍用 differentiable regression head。推荐实验顺序是：

```text
v23 anti-collapse -> v24 native-token regression -> v26 JSON SFT/RL
```

如果 v24 已经显著改善，v26 才有资格验证“生成式 JSON + RL”是否进一步提升。
如果 v24 没改善，v26 的 RL 大概率只是在噪声上花更多算力。

## 2. 模型选择

首选：

```text
Qwen2.5-Coder-0.5B-Instruct
```

理由：

- 需要遵循系统提示并输出 JSON，Instruct 比 Base 更合适。
- 0.5B 规模接近当前 Qwen3-0.6B，资源可控。
- Coder 预训练语料对 `mov/add/ld/st/jne/call/ret` 等汇编/代码 token 更友好。
- Qwen tokenizer 家族切换成本低。

对照：

- `Qwen2.5-Coder-0.5B-Base + SFT`：如果担心 Instruct tuning 扭曲底层代码
  表征，可作为 ablation。
- `Qwen2.5-Coder-1.5B-Instruct`：0.5B 语义不足时再尝试，显存和时间约
  2-3 倍。
- 不建议第一版上 4B 以上模型。当前瓶颈更可能是输入/目标设计，不是模型知识量。

## 3. Prompt 和输入结构

### 3.1 原则

1. 用英文短 prompt。Coding LLM 的代码语义和系统提示遵循主要来自英文语料。
2. 指令文本固定且短，避免每个样本浪费 token。
3. Trace 主体使用 whitespace-separated macro assembly，少用逗号、括号和长字段名。
4. 不新增 `<OP_*>` 这类 custom special token。新增 special token 越多，越偏离
   LLM 原生语义空间。
5. c08/c16/c32 不拼全局巨长 trace。每核 local-core prompt 预测一核，再由
   外部程序合并成整窗 JSON。

### 3.2 系统提示

固定 system prompt 控制在 80 token 内：

```text
You are a CPU performance model. Read the x86-like macro trace and core
summaries. Return only valid compact JSON. Predict PMU for the target core.
PMU order is cpi_m,br,l1ld,l1st,l2ld,l2st,llc. cpi_m is CPI*1000.
```

使用 `cpi_m = round(cpi * 1000)`，而不是让 LLM 直接输出浮点数。原因：

- 小整数比浮点 token 更稳定。
- JSON parse 后可无损转回三位小数 CPI。
- RL reward 可以在 decode 后计算真实 CPI 误差。

### 3.3 User prompt 模板

Local-core 单核预测模板：

```text
cfg cores=8 clk=3G l1=32K l2=512K l3=16M rob=192 iq=64 mshr=16
win uops=8192 macros=4096 shared=12 hotshared=5
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
out
```

输出：

```json
{"core":2,"pmu":[842,3,16,2,1,0,0]}
```

外部 wrapper 把每核输出合并成整窗：

```json
{"keys":["cpi_m","br","l1ld","l1st","l2ld","l2st","llc"],"cores":[[0,...],[1,...]]}
```

这样能避免 c32 的 160k token 全局上下文问题，同时最终产物仍是“包含每核 CPI、
branch miss 等”的 JSON。

## 4. Macro 指令压缩

### 4.1 Macro 边界

从 aligned parquet 抽取 macro：

```text
macro boundary = is_last_microop == 1 or macro_pc changes
```

每个 macro 只渲染一行，用 head uop 的功能字段加少量聚合 tag。复杂 macro 的
展开数只在异常时标注：

```text
x4, x8, x16
```

`xN` 仅在 `n_uops >= 4` 时出现，避免普通指令浪费 token。

### 4.2 指令行格式

统一格式：

```text
<op> <dst> <src> [stride] [reuse] [addr_alias] [extra]
```

不要用：

```text
mov rax, [rbx+0x7fffff10] ; L1 hit
```

应该用：

```text
mov rax rbx seq hot A
```

原因：

- 逗号、括号、十六进制大数会显著增加 token。
- L1/L2/LLC/DRAM 是 gem5 微架构结果，不能进入部署侧输入。
- `seq hot A` 保留了访存行为中真正可泛化的部分。

### 4.3 Op 词表

使用 LLM 原生代码 token，不用自定义枚举 token：

| 功能 | 渲染 |
|---|---|
| integer ALU | `add` / `sub` / `xor` |
| multiply/divide | `imul` / `idiv` |
| FP scalar | `addsd` / `mulsd` / `divsd` |
| SIMD | `padd` / `addps` |
| load | `mov <dst> <base>` |
| store | `mov <base> <src>` |
| atomic | `lock add` |
| conditional branch | `jne L` |
| indirect branch | `jmp R` |
| call/return | `call` / `ret` |

寄存器固定映射到常见 x86 名字：

```text
rax rbx rcx rdx rsi rdi rbp rsp r8 r9 r10 r11 r12 r13 r14 r15
```

这些名字对 Coder LLM 比 `RG_7` 或 `src_bucket_3` 更自然。

### 4.4 Token 预算

目标预算：

| 指令类型 | 示例 | 目标 token 数 |
|---|---|---:|
| ALU | `add rax rbx` | 3-4 |
| branch | `jne L` | 2-3 |
| load/store no alias | `mov rax rbx seq hot` | 5-6 |
| load/store with alias | `mov rax rbx seq hot A` | 6-7 |
| complex macro | `rep mov x8 seq warm A` | 6-8 |

已有 macro token probe 显示，compact assembly 在 Qwen tokenizer 下约
3.1-6.4 token/macro，UNK 率 0%。v26 的 gate 是：

```text
avg_tokens_per_macro <= 5.0
p95_tokens_per_macro <= 8.0
native-token coverage >= 95%
```

超过这个 gate，说明渲染仍然太啰嗦，应先压缩输入而不是训练模型。

## 5. 访存地址编码

### 5.1 不输入绝对地址

不把以下字段直接放进 prompt：

```text
vaddr, paddr, cacheline_addr, 0x7ffff...
```

原因：

- 绝对地址跨 workload 和进程运行不稳定。
- LLM 对长十六进制数字没有“同 page / 同 cacheline / stride 64B”的天然理解。
- 数字 token 成本高，容易诱导模型记住无泛化能力的地址模式。
- 部署侧需要的是访问 pattern，不是某次 gem5 run 的地址常量。

### 5.2 默认地址信息：pattern tag

每条 memory macro 最多携带两类功能 tag：

| 维度 | tag | 来源 |
|---|---|---|
| stride | `same`, `seq`, `str`, `far`, `rnd` | cacheline delta bucket |
| reuse | `hot`, `warm`, `mid`, `cool`, `cold` | bounded reuse distance |

示例：

```text
mov rax rbx seq hot
mov rax rbx rnd cold
mov rbx rax same warm
```

这些 token 同时满足两个条件：部署侧可算、LLM 语义上可读。

### 5.3 可选地址 identity：窗口内别名

当需要表达“两个 core 访问同一热点 line”时，只给 top-K 热点/共享 cacheline
分配短别名：

```text
A B C ... Z a b c ... z
```

分配规则：

```text
1. 对当前 window 内所有 core 的 cacheline 做频次和跨核共享统计。
2. 只保留 top 32 或 top 64 的 hot/shared line。
3. 按稳定排序映射到 A..Z,a..z,0..9。
4. 指令行只有命中这些 line 时才追加 alias。
5. 其他地址只保留 stride/reuse tag，不给 alias。
```

示例：

```text
c0 hot=A B
c2 hot=A B D
mov rax rbx seq hot A
mov rcx rdx rnd cold
```

这里 `A` 表示窗口内同一个 hot/shared line，不表示绝对地址。这样模型能看到
跨核共享关系，又不会记住不可迁移的地址常量。

### 5.4 Page / region 信息

如果 TLB 或 page locality 是重点，可以加第二层粗粒度 alias，但只放在摘要里，
不要每条指令都带：

```text
target c2 pages=p0 p3 p7 page_span=md
```

默认不在指令行加 page id。否则 token 会膨胀，且 page alias 的收益需要单独
ablation 验证。

## 6. JSON 输出设计

不要让模型每次输出冗长字段名：

```json
{"core":2,"cpi":0.842,"branch_miss":3,"l1d_ld_miss":16}
```

推荐模型输出紧凑 schema：

```json
{"core":2,"pmu":[842,3,16,2,1,0,0]}
```

系统提示中固定 PMU 顺序：

```text
pmu = [cpi_m, br, l1ld, l1st, l2ld, l2st, llc]
cpi = cpi_m / 1000
```

优点：

- 输出短，c32 聚合后也可控。
- 小整数比浮点数更容易生成。
- schema 简单，reward parse 稳定。
- 仍能完整表达每核 CPI 和 branch/cache miss。

推理时使用 constrained decoding 或 JSON schema checker。训练/评估时任何
invalid JSON 都给强负 reward，不能静默修复后再算好分。

## 7. 训练方案

### 7.1 阶段 0：语义 gate

投入训练前必须过两个 gate：

1. Tokenizer gate：compact macro 的 native-token 覆盖和 token/macro 达标。
2. Zero-shot gate：给 Qwen-Coder-Instruct 一段 pointer-chase / branch-heavy
   macro trace，让它解释瓶颈。至少要能说出 memory-bound、pointer chasing、
   branch-heavy 这类概念。

不过 gate 就不要做 v26。否则 RL 只是让模型背标签，不是在利用预训练语义。

### 7.2 阶段 1：SFT

先做 supervised fine-tuning：

```text
input:  system + cfg + cross-core summary + target-core macro asm
target: compact JSON
loss:   next-token cross entropy on JSON only
```

训练标签来自现有 windows：

```text
cpi_m = round(cpi_uop * 1000)
br    = branch_miss
l1ld  = l1d_ld_miss
l1st  = l1d_st_miss
l2ld  = l2_ld_miss
l2st  = l2_st_miss
llc   = llc_miss
```

先 SFT 的原因：

- 让模型学会稳定 schema。
- 给 RL 一个合理初始策略，避免早期全是 invalid JSON。
- 数值任务有监督标签，纯 RL 样本效率太低。

### 7.3 阶段 2：GRPO/RLOO

不建议第一版用完整 PPO + value model。这里的 reward 可直接从 label 计算，
用 GRPO 或 RLOO 更简单。

每个 prompt 采样 K 个 JSON：

```text
K = 4 first run
K = 8 if reward variance is high
temperature = 0.7 -> 0.3 anneal
max_new_tokens = 80 for single-core JSON
```

Reward：

```text
R = R_schema
  + R_numeric
  + R_cycles
  + R_rank
  + R_phys
  + R_len
```

定义：

```text
R_schema = +0.2 if valid JSON and exact schema else -1.0

R_numeric = - mean_k huber(
    normalize(pred_k - label_k), delta
)

R_cycles = - huber(
    log(sum_i pred_cpi_i * uops_i) -
    log(sum_i label_cpi_i * uops_i)
)

R_rank = - mean_{i,j} softplus(
    -(pred_cpi_i - pred_cpi_j) * sign(label_cpi_i - label_cpi_j) / tau
)

R_phys = penalty for negative counts, impossible miss counts,
         cpi outside configured range, NaN/Inf

R_len = -0.001 * generated_tokens
```

如果按单核 prompt 训练，`R_rank` 和 `R_cycles` 在同一 window 的 core 输出收齐后
再计算，或者先只用单核 `R_numeric/R_phys`，再做一个 window-level RL finetune。

不要把 gem5 放进 RL inner loop。gem5 太慢，而且会把训练变成在线仿真实验。
第一版 RL 只用离线 label reward；gem5 只做 checkpoint 级别验证。

### 7.4 参数更新策略

第一版：

| 参数组 | 策略 |
|---|---|
| 原生 token embedding | 冻结 |
| LoRA on attention q/k/v/o | rank 32 或 64 |
| LoRA on MLP | rank 16 或 32，可选 |
| LM head | 冻结或只训 LoRA，不建议全量 |
| 新增 delimiter token | 第一版尽量不用；如果用了，只训新增行 |

学习率：

```text
SFT LoRA lr   = 1e-4
SFT head lr   = 5e-5 if LM head unfrozen
RL LoRA lr    = 1e-5 to 3e-5
warmup        = 3-5% steps
grad clip     = 1.0
```

不建议第一版全量微调：

- 40k 量级窗口不足以安全全量改写 0.5B+ 代码模型。
- 全量微调容易破坏正想利用的代码 embedding 和格式遵循能力。
- 显存、通信和 checkpoint 成本都会显著增加。

## 8. 显存估算

估算前提：

```text
model: Qwen2.5-Coder 0.5B / 1.5B
sequence: 4k-6k input tokens per core prompt
output: 40-80 tokens per core
precision: bf16, gradient checkpointing on
hardware baseline: A100 80GB
```

当前工作环境 `nvidia-smi` 无法连接 GPU driver，因此下面是工程估算，不是本机
实测。

| 配置 | 每 GPU 可行性 | 估计峰值显存 | 备注 |
|---|---:|---:|---|
| 0.5B LoRA r32, micro-batch 4-8 core prompts | A100 40GB 可试，80GB 稳 | 24-45GB | 推荐第一版 |
| 0.5B QLoRA r32 | 24GB-40GB 可试 | 18-32GB | 训练慢一些，适合显存紧张 |
| 0.5B full bf16 finetune | 80GB 边界或需 FSDP | 70-100GB | 不建议第一版 |
| 1.5B LoRA r32/r64 | 80GB 级别 | 50-75GB | 作为第二档 |
| 1.5B QLoRA | 40GB-80GB | 35-55GB | 可行但吞吐下降 |
| 1.5B full bf16 finetune | 需要 FSDP/ZeRO | 160GB+ aggregate | 工程成本高 |

显存主要由 activation 决定，不是 LoRA 参数。Local-core 的 5k 序列比全局 32k
安全得多；c32 通过“32 个 core prompt 分批跑”解决，不把 32 核 trace 拼成
一个 160k prompt。

## 9. 训练时间估算

以 8 x A100 80GB 为参考：

| 阶段 | 0.5B LoRA | 1.5B LoRA | 说明 |
|---|---:|---:|---|
| tokenizer probe + zero-shot gate | 0.5 天 | 0.5 天 | 含人工检查样本 |
| macro window/cache 构建 | 0.5-1 天 | 同左 | 取决于数据量和并行度 |
| SFT 1 个有效 epoch | 2-4 小时 | 5-10 小时 | core prompt 数约为 window 数 x core 数 |
| SFT 3 epoch / 8k-12k steps | 6-12 小时 | 18-30 小时 | 第一版主训练 |
| GRPO/RLOO K=4, 1k-3k updates | 12-36 小时 | 1.5-4 天 | 受采样吞吐影响大 |
| 全评估 c04/c08/c16/c32 | 2-6 小时 | 4-10 小时 | 取决于 window 数和生成设置 |

如果只有 4 x A100 80GB，时间大约乘 1.8-2.3。如果是 H100 80GB，时间大约乘
0.5-0.7。QLoRA 显存更低，但通常慢 20-50%。

训练时间的主要不确定性是 RL：每个 prompt 要采样 K 个输出，且 decode 不能像
teacher-forcing SFT 一样完全并行。因此 v26 不应该跳过 SFT 直接 RL。

## 10. 可行性判断

### 10.1 技术可行

v26 在工程上可行：

- 输入可以完全落在 LLM 原生 tokenizer 上。
- Macro Assembly 已经有 token probe 支撑。
- JSON 输出可以通过 fixed schema 和 constrained decoding 控制。
- 离线 label reward 足够支持 GRPO/RLOO。
- 0.5B LoRA 的显存和训练时间在 8 x A100 80GB 范围内。

### 10.2 科学风险中等偏高

最大风险不是能否训练，而是收益是否值得：

- PMU 数值预测是低熵回归任务，生成式 JSON 未必比 regression head 更好。
- RL reward 来自同一批 label，可能只是优化指标外壳，不增加泛化。
- 输出 JSON 的数值 token 误差可能掩盖 input semantic gain。
- 如果 per-core 塌缩没有被 rank/cycles reward 明确压住，生成式模型也会输出
  平滑均值。

因此 v26 必须和 v24 做严格对照：

```text
same native macro input
v24: hidden -> regression head
v26: hidden/LM -> JSON generation + SFT/RL
```

只有 v26 在 per-core CPI spread、rank accuracy、cycles WAPE 和 OOD core-count
上超过 v24，JSON/RL 才算证明价值。

## 11. 推荐实施顺序

### Phase A：最低成本验证，1-2 天

1. 写 `render_macro_native.py`，生成 100 个窗口的 prompt 样本。
2. 跑 tokenizer probe，检查 token/macro 和 native coverage。
3. 用 Qwen2.5-Coder-Instruct 做 zero-shot bottleneck 问答。
4. 人工检查地址 alias 是否真的表达 sharing/reuse。

Gate：

```text
avg tok/macro <= 5.0
valid JSON zero-shot examples can follow schema
manual bottleneck answer is semantically plausible
```

### Phase B：JSON SFT，3-5 天

1. 构建单核 prompt 数据集。
2. 训练 0.5B LoRA SFT。
3. 评估 JSON parse rate、per-core PMU MAE、cycles WAPE。
4. 对照 v24 regression head。

Gate：

```text
valid_json_rate >= 99%
per-core cpi error not worse than v24 by > 3pp
rank accuracy better than v22/v23 collapse baseline
```

### Phase C：小步 GRPO/RLOO，2-4 天

1. 从 SFT checkpoint 初始化。
2. K=4 采样，1k updates。
3. 只优化 LoRA，低学习率。
4. 如果 val reward 和 heldout workload 同时改善，再扩到 3k updates。

Gate：

```text
heldout workload cycles WAPE improves
per-core spread does not shrink
JSON parse rate remains >= 99%
```

### Phase D：是否扩模型

只有 0.5B v26 明确输在语义推理上，而不是输在 schema/数值/训练不稳上，才试
1.5B。否则扩模型只会放大成本。

## 12. 不做的事

- 不把绝对地址、cache hit/miss、path_class、mispredicted 等 gem5 oracle 放进输入。
- 不把 c32 的所有 core trace 拼成一个超长 prompt。
- 不从零开始纯 RL。
- 不第一版全量微调 0.5B/1.5B。
- 不用长字段 JSON 作为训练目标。
- 不用 v26 结果替代 v24/v25 对照；它只能作为额外路线验证。

## 13. 最小实验矩阵

为了保持因果归因清晰，只做单变量 ablation：

| 实验 | 输入 | 输出/训练 | 目的 |
|---|---|---|---|
| E0 | v22 custom uop | current regression | baseline |
| E1 | native macro, no alias | regression head | v24 主线 |
| E2 | native macro + pattern alias | regression head | 地址 alias 是否有效 |
| E3 | native macro + alias | JSON SFT | 生成式是否不输 regression |
| E4 | 同 E3 | JSON SFT + GRPO K=4 | RL 是否带来真实增益 |
| E5 | 同 E4 | 1.5B LoRA | 规模是否必要 |

判定指标：

```text
per-core log-CPI MAE
per-window rank accuracy
pred_log_cpi_std / label_log_cpi_std
cycles WAPE after aggregation
branch/cache miss count MAE
valid JSON rate
tokens/window and latency/window
```

如果 E3 已经输给 E2，先不要做 E4；说明问题在 JSON 生成，不是缺 RL。
