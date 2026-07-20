# Macro-v29：离线 LLM 语义编码 + 在线 Macro-level Qwen 设计

状态：**384 维逐 macro 跨核 attention 的 vNext、旧 A/B/E 混合核训练，以及
c4/c8/c16/c32 deployment 对照均已完成。下一项主线是第 18 节定义的 B2/E2：以最新
TCSim v29 per-UOP 特征和 full-QKVR 为共同底座，仅比较是否注入 Qwen3-14B + LoRA 的
静态 macro 语义。第 18 节当前是设计合同，尚未实现或开始正式训练。**

日期：2026-07-20。
[256-macro 原生 Token 方案](llmsim_tcsim_v29_macro_native_token_design.md) 仍作为已训练的权威
baseline 保留。当前权威 vNext checkpoint 为
`ckpt/macro_v29_vnext_d384_crossmacro_c8_s1_30k_20260719_025211/trainable_step00030000.pt`；
其 c8 deployment 汇总为 ROI CPI MAPE `6.872%`、makespan MAPE `7.045%`、约
`21075.7 aggregate macro/s`。这些结果证明当前 checkpoint 在总体 23 条 trace 上依赖正确
语义映射、LoRA 和在线 LLM 分支，但 heldout 置信区间尚不足以证明 LLM 语义的因果泛化收益。
本文第 16 节记录旧 vNext 架构，第 17 节记录已经完成的旧 A/B/E 设计与入口，第 18 节是
当前 B2/E2 权威设计。旧 A/B/E 已覆盖混合核训练和 c4/c8/c16/c32 deployment，但仍只有
一个正式训练 seed，且底座未与最新 TCSim v29 完全对齐，因此不能把“当前 checkpoint 依赖
LLM”写成“LLM 语义已被因果证明”或“在线 Qwen 不可替代”。

## 0. 2026-07-19 实现状态

已完成的主线代码：

| 合同 | 实现 |
|---|---|
| frozen Qwen 静态语义构建 | `data/build_macro_v29_semantic_cache.py` |
| 同 BB 前 4 条上下文、身份归一化 prompt | builder 的 `build_static_records()` |
| binary/bytes/context/model/tokenizer/prompt/pooling 敏感 key | `semantic_key_fingerprint()` 与 shard `semantic_key_hashes` |
| versioned manifest、模型 artifact hash、抽样重算 | `macro-v29-semantic-cache-1` |
| cache miss fail closed、`[K,D]` gather/padding | `CachedSemanticSource` |
| online `[R,256,D_qwen]` soft-token Qwen | `MacroV29TimingModel._semantic_macro_hidden()` |
| anchor + gated semantic adapter | `static_anchor + sigmoid(g_sem) * SemanticAdapter(z_static)` |
| 无 teacher 的真实标签训练 | `train/train_macro_v29.py`，run contract v4 |
| cache/checkpoint 强绑定 | checkpoint 中记录 encoder、prompt、pooling、manifest hash 和维度 |
| label-free 部署 rollout 与 stage timing | `eval/rollout_macro_v29_checkpoint.py`、`MacroV29ModelPredictor` |
| native-token A/B | `--semantic-input-mode native_token` 显式保留 |

当前主线 CLI 默认 `cached_macro_soft_token`；native baseline 必须显式选择
`--semantic-input-mode native_token`。semantic mainline 禁止 `--init-trainable`，checkpoint 明确写入：

~~~text
task_init_source: fresh
init_timing_checkpoint: null
supervision_mode: real_labels_only
distillation_enabled: false
online_backbone_input_unit: macro
~~~

一键入口：

~~~bash
# 完整主线：先构建/校验 cache，再启动 c8 8000-step 训练
bash scripts/run_macro_v29_semantic_c8_mainline.sh

# 临时一键入口：构建/复用 cache 后直接启动 c8 8000-step 训练
bash scripts/tmp/run_macro_v29_semantic_c8_8k_all_in_one.sh

# 或分开执行。1. 离线构建全部 c8 train/validation/deployment 静态语义
bash scripts/build_macro_v29_semantic_cache.sh

# 2. 8 GPU、c8、8000 step、无蒸馏监督训练
bash scripts/train_macro_v29_semantic_supervised.sh

# 3. 完成训练后，设置 RUN_DIR/TRACE_ROOT/STATIC_DICT 做部署侧吞吐 smoke
RUN_DIR=... TRACE_ROOT=... STATIC_DICT=... \
  bash scripts/bench_macro_v29_semantic_throughput.sh
~~~

vNext 的一键入口与旧 256-summary 训练入口分开。当前实验决策先固定只训练 c8，使用
`sequence_length=1`、30000 step。脚本内部由 `nohup` 后台启动；它会依次运行架构/梯度
preflight、构建或校验 c8 semantic cache，最后从头启动真实标签训练：

~~~bash
bash scripts/tmp/run_macro_v29_vnext_c8_s1_30k_all_in_one.sh
~~~

命令会立即打印 `pid`、`log` 和 `output`。默认训练配置为每卡 batch 1、8 卡、
`sequence_length=1`、`sequence_stride=1`、`max_steps=30000`、`d_macro=384`；不加载旧 timing checkpoint，
不使用 teacher/student 蒸馏。若输出目录已有 `run.json`，训练会 fail closed，不会静默续训
或覆盖旧模型。

截至本次代码审查，完整测试集 44 个单元/合同测试通过，包括一次 batched 8-core soft-token
backbone call、尾部 mask、cache provenance sensitivity、cache miss fail-closed、单调 commit time、
core permutation equivariance、逐 macro cross output、目标核分块等价性、Qwen 核分批等价性、
checkpoint 重计算的梯度完整性和 exactly-once 既有测试。此后已经在真实 GPU 上完成 c8
soft-macro 8000-step 训练；`>=10K macro/s`、完整 seed1/deployment 精度和 closed-loop drift
仍是待执行项。这里的真实 GPU 8K 结果只对应旧 256-summary checkpoint；384-cross-macro
vNext 尚无正式 GPU 训练结果，不能由单元测试、训练速度或训练侧 validation 替代。

当前权威 soft-macro 训练产物：

~~~text
checkpoint:
  ckpt/macro_v29_semantic_c8_8k_20260719_011546/trainable_step00008000.pt
semantic_input_mode: cached_macro_soft_token
online_backbone_input_unit: macro
K_macro: 256
semantic_dim / anchor_dim: 1536 / 1536
d_model: 256
steps: 8000
elapsed: 1338.83 s
training-side validation commit WAPE: 18.1678%
semantic gate sigmoid: 4.6443%
core gate sigmoid: 13.5690%
deployment validation: pending
~~~

`macro_v29_real_c08_trainval_8gpu_8k_sdpa_safe` 是较早的 native-token 产物，不是本文当前
soft-macro checkpoint；比较或部署时不得混用两者合同。

本次实际 smoke 记录：

| smoke | 结果 | 可证明内容 | 不能证明内容 |
|---|---|---|---|
| 真实 c8 数据 + synthetic cache + tiny backbone，1 train step | PASS；8 rows / 2048 valid macro；loss/backward/validation/checkpoint 全通过 | 数据、训练和 checkpoint 主链路 | LLM 语义、精度、正式吞吐 |
| 上述 checkpoint 的 c8 label-free rollout，3 steps | PASS；无 label key；退休 3051 macro；stage timing 字段完整 | semantic cache 部署接入和 scheduler 推进 | tiny 数字不能作为 10K 结论 |
| 真实冻结 Qwen2.5-Coder-1.5B CPU smoke | PASS；revision `2e1fd397...`；离线得到 5x1536 semantic；在线输入 `1x256x1536`；finite loss | 真实 Qwen 权重、离线编码、anchor、`inputs_embeds` 在线路径 | c8 GPU 延迟、8K 训练精度 |

semantic cache 的 checkpoint identity 使用去除 `build_reports/elapsed` 后的规范化 manifest hash；
parquet SHA256、shard SHA256、PC-set hash、模型 artifact/config/tokenizer/prompt/pooling 任一变化都会
在 forward 前 fail closed。

## 1. 决策摘要

旧 native-token macro-v29 baseline 会把每个 active core 未来 256 条真实 macro 汇编渲染为
原生文本 token，随后把 8 个 core row 作为一个 batch 调用一次 Qwen backbone。它已经是批量
推理，不是 Python 循环逐核执行；瓶颈是每条 macro 平均膨胀为约 10 个文本 token，并且高度
重叠的滚动窗口每一步都重新执行完整 prefill。当前已训练 soft-macro 主线已经将在线输入压缩为
每条 macro 一个连续 position；第 16 节的 vNext 在此基础上进一步修复跨核 summary 瓶颈。

本文采用两级语义模型：

~~~text
离线静态语义塔：
  唯一静态 macro / basic-block context
    -> frozen Qwen2.5-Coder-1.5B
    -> 每条 macro 的静态 LLM 语义向量
    -> 按 binary/instruction/schema 哈希持久化缓存

在线动态上下文塔：
  cached static semantic vector
    -> 每条 macro 一个 soft token
    -> batched macro-level Qwen: [active_core_rows, 256, hidden]
    -> 当前 StructuredMacroEncoder / side features / cross-core mixer
    -> commit-time / progress / branch heads
    -> 当前 label-free macro stride scheduler
~~~

这不是删除 LLM，也不是退回纯手工特征：

- 离线 Qwen 回答“这条汇编指令和局部 basic block 表达什么语义”；
- 在线 Qwen 回答“这 256 条动态 macro 以当前顺序组合后形成什么程序模式”；
- 结构化侧通道回答“本次动态执行的 reuse、stride、dependency、共享和 uarch 状态是什么”；
- cross-core mixer 回答“多个 active core 的状态如何相互影响”。

部署热路径不再重复让 Qwen 阅读相同静态汇编文本，但仍使用 LLM 生成的语义，并可继续让
Qwen backbone 在线建模 macro 序列。

训练主线固定为：**不使用当前 native-token checkpoint 作为 teacher，不做 hidden/output
蒸馏。** 离线和在线 Qwen 都从公开预训练权重初始化；semantic adapter、StructuredMacroEncoder、
cross-core mixer 和 timing/branch heads 使用新随机初始化，只由真实训练标签监督。蒸馏仅可在
无蒸馏主线完成后作为独立消融，不是实现或验收的必要步骤。

## 2. 目标、口径和非目标

### 2.1 主吞吐目标

本文的 `10KIPS` 固定解释为：

~~~text
单条 c8 trace
8 个 active core 合计
>= 10,000 retired macro instructions / wall-clock second
~~~

它不是：

- 单核 `10K macro/s`；
- `uops/s`；
- 8 张 GPU 同时跑 8 条独立 trace 后的整机合计；
- Transformer tokenizer token/s。

正式报告必须同时给出：

1. per-trace/GPU inference-loop macro/s；
2. per-trace/GPU inference-loop UOP/s；
3. 单步 wall time 和其中 Qwen forward 时间；
4. 每步 exactly-once 退休的 macro/UOP 数；
5. 8-GPU end-to-end aggregate，单列且不与主目标混报。

### 2.2 精度与语义目标

- 保留当前 label-free free-running rollout、全局虚拟时间和 macro cursor；
- 保留 256-macro lookahead、单调 commit-time、prefix/progress/drift/branch 目标；
- 不把 commit tick、branch-miss truth、cache/coherence oracle 或 stall reason 放入语义缓存；
- 使用真实、经 binary bytes 验证并做身份去除的汇编语义；
- 相对当前 native-token 8K baseline 的精度变化必须由同一 deployment split 的闭环结果证明；
- `real semantic` 必须优于 zero/shuffle/random 等语义控制组，否则不能声称 LLM 语义有效。

### 2.3 非目标

- 不要求新的 macro soft token 与当前 native-token hidden 逐位完全相同；
- 不试图对当前窗口相关的 `asm_macro` hidden 做按 PC 的“精确缓存”；
- 不在第一版实现跨步 KV cache；
- 不通过把 8 核原生文本拼成一条 20K token 序列来提速；
- 不把吞吐改善归因于 tokenizer cache。当前 token cache 只省 CPU tokenization，不能省
  backbone 计算。

## 3. 当前证据与瓶颈

### 3.1 历史吞吐

同一类 c08 部署评估的已归档结果：

| 方案 | 表示 | mean total/window | c08 aggregate uops/s | c08 aggregate macro/s |
|---|---|---:|---:|---:|
| v8 | 每 UOP 约 6 个文本 token，TQ 尽量填满 32K | 1275.6 ms | 3,883 | 2,134 |
| v9 | composite UOP，1 UOP = 1 position | 87.9 ms | 25,775 | 14,166 |
| v11 | composite UOP，Qwen3-0.6B | 105.1 ms | 24,624.5 | 13,632.2 |
| v12 | composite UOP，Qwen3-4B | 391.6 ms | 5,879 | 未统一报告 |

来源：

- [v8 与 v9 精度/吞吐对比](eval_v9_vs_v8_precision_throughput_20260627.md)；
- [v11 Qwen3-0.6B 结果](eval_v11_timing_qwen3_0p6b_step8000_results_20260629.md)；
- [v12 Qwen3-4B 结果](eval_v12_summary_qwen3_4b_step7500_results_20260630.md)。

这些报告中的 c08 throughput 都是一个 workload/GPU 内 8 个 active core 的合计，不是
per-core throughput。

### 3.2 当前 macro-v29 诊断量级

2026-07-18 对 real Qwen 8K checkpoint 做的短 rollout 诊断得到以下暂定量级：

~~~text
8 scheduler steps
6631 retired macros
8427 retired UOPs
5.865 s wall

约 1131 aggregate macro/s
约 1437 aggregate UOP/s
约 733 ms/step
约 829 retired macros/step
约 1053 retired UOPs/step
~~~

代表性第一步 8 个 core row 合计约 20,096 个 Qwen token，对应 2,048 条 lookahead macro：

~~~text
约 9.8 input token / lookahead macro
约 24.2 processed token / retired macro
~~~

后一个数字更能解释部署吞吐：由于 stride 和全局最小候选时间，一步只退休 lookahead 的
一部分；下一步窗口与本步高度重叠，但当前实现仍完整重算。

这组数据是短诊断，不替代完整 23-trace 稳态报告。2026-07-18 的完整运行因节点 GPU
设备消失而中断，没有形成最终结果；在新方案验收时必须重跑完整基线。

### 3.3 当前不是“逐核串行跑 8 次 Qwen”

当前 `MacroV29TimingModel._backbone_hidden()` 在 c8 时一次接收：

~~~text
input_ids:      [8, L_max]
attention_mask: [8, L_max]
~~~

并执行一次 batched backbone call。把 batch 拆成两次 `[4, L]` 只会降低峰值显存，通常
不会提高总吞吐；把 8 行拼成 `[1, 8L]` 会使 attention 从 `8 * L^2` 变为 `(8L)^2`，
计算和显存反而增加约 8 倍。

### 3.4 真正瓶颈

当前链路是：

~~~text
8 cores * 256 macro/core
  -> about 20K native text positions
  -> Qwen2.5-Coder-1.5B full prefill
  -> span-pool back to 8 * 256 macro states
~~~

主要浪费发生在“先把 2,048 条 macro 膨胀到约 20K token，再压回 2,048 个 macro state”。
CPU context/token 构造短测约 22 ms/step，而完整 step 约 733 ms，因此只优化 parquet、
tokenizer 或 CPU last-window cache 不可能获得所需的约 9 倍 macro 吞吐提升。

## 4. 总体架构

~~~text
                         OFFLINE / BUILD TIME

validated binary bytes + normalized real asm + local BB/CFG context
                                |
                                v
                   frozen Coding-LLM semantic encoder
                                |
                                v
                 per-static-macro semantic vector z_static
                                |
                                v
          versioned semantic cache keyed by binary/instruction/schema

                         ONLINE / ROLLOUT STEP

predicted macro cursors of all active cores
                |
                +--> gather z_static by static instruction key
                |                |
                |                v
                |      semantic adapter + embedding anchor
                |                |
                |                v
                |      macro soft tokens [R, 256, D_qwen]
                |                |
                |                v
                |        online macro-level Qwen
                |                |
                |                v
                |      contextual semantic states [R, 256, D]
                |
                +--> current ragged UOP fields -> StructuredMacroEncoder
                +--> current state/relation/uarch/chunk-summary features
                                |
                                v
                  semantic + numeric + side late fusion
                                |
                                v
                     permutation-equivariant core mixer
                                |
                                v
          positive gap -> FP64 cumulative commit time -> branch/progress
                                |
                                v
               current macro stride scheduler and exactly-once update
~~~

`R` 是 batch 中串接的 active-core row 数；单个 c8 context 通常 `R=8`，尾部完成的核会退出。

## 5. 离线静态语义编码

### 5.1 编码单位

主单位是“真实静态 macro instruction”，但编码 prompt 应提供短 basic-block context：

~~~text
Architecture: x86-64
Function/BB context (identity-normalized):
  cmp eax, 0
  je .L_exit
  mov edx, dword ptr [rax + rcx*4]
  imul edx, esi

Target instruction:
  mov edx, dword ptr [rax + rcx*4]

Semantic representation:
~~~

目标语义向量可由最后一个 summary position 的 hidden 得到。对 causal Qwen，这个位置能看到
整个前缀。另一候选是目标 instruction token span pooling，两者必须做语义门禁和下游精度
消融，不能在看到 test 结果后选择。

本地上下文第一版建议固定为：

- 同一 basic block 内目标指令之前最多 4 条指令；
- 目标指令；
- 目标 direct branch 的规范化 target/fallthrough 类别；
- 不放绝对 PC、模块路径、workload 名、动态地址或 timing 字段。

若希望包含目标之后的静态指令，不能假设 causal hidden 自动看见未来 token；应把完整 BB
内容放在 prompt 前部，再在末尾重复目标指令并放置 summary marker。

### 5.2 静态语义与动态信息的边界

允许进入离线语义编码器：

- 经 binary bytes 验证的 mnemonic 和 operands；
- operand size、register def/use、addressing mode；
- 有意义的小 immediate/displacement；
- function/basic-block 内局部顺序；
- 规范化 direct-branch target/fallthrough/loop-back 类别；
- decoder 和 CFG 可静态证明的信息。

禁止进入离线语义编码器或 cache key 的可学习内容：

- commit/fetch/issue/complete tick；
- branch miss truth；
- cache hit/miss、MESI/coherence oracle；
- stall reason、MSHR/queue occupancy；
- 动态 effective/physical address；
- workload/seed/split 名称；
- 由上述标签聚合出来的 count、bucket 或 summary。

动态 reuse、stride、producer distance、共享 line、uarch profile 等继续使用当前结构化侧通道。

### 5.3 Cache key

不能只用裸 PC；不同二进制的相同地址可能表示不同指令。建议：

~~~text
semantic_key = sha256(
    binary_build_id,
    module_relative_instruction_offset,
    instruction_bytes,
    normalized_assembly,
    normalized_local_context_hash,
    decoder_name_and_version,
    semantic_encoder_model_revision,
    tokenizer_hash,
    prompt_schema_version,
    pooling_policy,
)
~~~

任何影响语义向量的字段变化都必须生成新 key。cache manifest 必须记录完整 provenance，
加载时不匹配应 fail closed，不能静默复用。

### 5.4 Cache value 和存储预算

第一版保存冻结 Qwen 的原始 BF16 hidden：

~~~text
z_static: [D_sem], BF16
anchor:   [D_qwen], BF16       # 可选，原始 token embedding pooling
~~~

当前 NPZ 实现实际以 `float16` 保存，并在 manifest 锁定 `storage_dtype=float16`；原因是 NumPy
NPZ 没有原生 BF16 dtype。builder 会检查 finite，并对抽样向量重新计算后按 FP16 容差核验，
不能把 FP16/BF16 cache 混用。若全量构建发现 hidden 超出 FP16 范围，应切换为支持 BF16 的
safetensors shard 并升级 cache schema，而不是静默截断。

训练稳定后可导出紧凑部署缓存：

~~~text
z_compact = trained_export_projection(z_static)   # 例如 256/512 维 BF16
~~~

以 100 万条唯一静态 macro 为例：

- 1536 维 BF16 约 3 GB；
- 256 维 BF16 约 512 MB。

动态 trace 中同一 loop/body 的静态 macro 会被执行大量次，因此缓存规模按唯一静态指令数，
不是按动态 macro 总数增长。

### 5.5 冻结和更新策略

离线 semantic Qwen 在第一版必须冻结。若训练中更新它，每次 optimizer step 后缓存都会过期，
失去设计意义。

若后续证明有必要微调离线编码器，应采用独立阶段：

1. 在训练 split 上微调并选定 semantic encoder checkpoint；
2. 冻结 checkpoint；
3. 重建全部静态 semantic cache；
4. 再训练在线 macro model。

部署进程不应为了 cache miss 在热路径加载第二份 1.5B 模型。生产模式应在运行前扫描并补齐
缓存；未知/JIT 指令由独立冷路径生成，或严格报错。

## 6. 在线 Macro-level Qwen

### 6.1 输入契约

新数据入口建议为：

~~~text
static_semantic     [R, M, D_sem]
static_anchor       [R, M, D_qwen]     # optional
valid_macro_mask    [R, M]
uop_fields          [R, U_batch, 26]
uop_valid_mask      [R, U_batch]
uop_to_macro        [R, U_batch]
dynamic_uop_fields  [R, U_batch, 8]
chunk_summary       [R, 38]
relation_features   [R, 22]
state_features      [R, 5]
uarch_features      [R, 29]
sample_ptr          [n_context + 1]

M = 256
~~~

在线热路径不再需要 `input_ids`、`token_to_macro`、`macro_token_start/end` 和 span pooling。
为了与当前 native-token baseline 做严格 A/B，训练数据可在过渡期同时保留 native-token 与
semantic-cache 两套入口，但新主线不读取 native checkpoint 的 hidden 或预测；checkpoint
contract 必须声明使用哪一种输入。

### 6.2 Macro soft token

不能把任意投影向量直接作为 Qwen `inputs_embeds`，否则可能偏离预训练 token embedding 的
数值分布。建议使用带词嵌入锚点的门控适配器：

~~~text
E_anchor[m] = Mean(QwenInputEmbedding(native_token_ids_of_macro_m))
E_sem[m]    = SemanticAdapter(z_static[m])

X_macro[m] = LayerNorm(
    E_anchor[m] + sigmoid(g_sem) * E_sem[m]
)
~~~

- `E_anchor` 很便宜，可以与 semantic cache 一起离线保存；
- `SemanticAdapter` 和 `g_sem` 可训练；
- `g_sem` 从较小值初始化，让输入从 Qwen 熟悉的 embedding 分布平滑过渡；
- 不增加 tokenizer vocabulary，也不伪造 token ID。

### 6.3 在线 Qwen 调用

~~~python
soft_macro = semantic_adapter(static_semantic, static_anchor)

online = macro_qwen(
    inputs_embeds=soft_macro,          # [R, 256, D_qwen]
    attention_mask=valid_macro_mask,   # [R, 256]
    output_hidden_states=False,
    use_cache=False,
)

semantic_macro = online.last_hidden_state
~~~

c8 仍然是一次 batched call：

~~~text
macro_qwen(inputs_embeds=[8, 256, D_qwen])
~~~

不是逐核调用 8 次。Qwen 的 RoPE/position IDs 按每个 core row 的 macro 顺序生成；不同 core
在 local Qwen 中互不可见，跨核信息仍由后续 permutation-equivariant core mixer 处理。

### 6.4 第一版融合位置

第一版保持与当前实现最接近的 late fusion：

~~~text
H_sem[m] = OnlineMacroQwen(X_macro)[m]
H_num[m] = StructuredMacroEncoder(current UOP/dynamic fields)[m]
H_side   = SideProjection(summary, relation, state, uarch)

H_macro[m] = LayerNorm(
    P_sem(H_sem[m])
    + H_num[m]
    + position_embedding[m]
    + H_side
)
~~~

随后原样保留：

~~~text
core summary
  -> permutation-equivariant core mixer
  -> gated broadcast to per-macro states
  -> positive retirement gap
  -> FP64 cumulative commit time
  -> prefix/progress/branch outputs
~~~

第二阶段才消融把动态特征门控注入 Qwen 输入：

~~~text
X_macro += sigmoid(g_dyn) * DynamicAdapter(H_num)
~~~

`g_dyn` 应接近 0 初始化。不能在第一轮同时改变表示、动态注入、backbone 大小和 scheduler，
否则无法判断吞吐或精度变化来自哪里。

### 6.5 Backbone 选择顺序

为隔离变量，按以下顺序实验：

1. **P0：同一个 Qwen2.5-Coder-1.5B**，只把 native text positions 改成 macro soft tokens；
2. **P1：同尺寸 backbone + LoRA/compile 优化**；
3. **P2：0.5B/0.6B macro-level Qwen**，使用其预训练权重并由真实标签从头训练新任务模块；
4. **P3：专用小型 Transformer**，只作为吞吐/精度下界，不可与“在线 Qwen”主结果混称。

P0 先证明“表示压缩”本身能否达标；只有 P0 精度通过后才缩小 backbone。

## 7. 主线训练：真实标签监督，不使用 Teacher

### 7.1 “从头训练”的准确含义

主线不加载当前 `trainable_step00008000.pt`，也不生成或读取 teacher hidden、teacher
commit-time、teacher progress 等软目标。新架构直接针对真实训练标签优化。

“从头训练”不等于随机初始化 Qwen，否则会丢掉本方案要求保留的预训练汇编语义。初始化
合同固定为：

| 模块 | 初始化 | 主线是否训练 |
|---|---|---|
| 离线 semantic Qwen | 预训练 Qwen2.5-Coder 权重 | 冻结，不训练 |
| 在线 macro-level Qwen | 预训练 Qwen 权重 | backbone 冻结，LoRA 从头训练 |
| semantic adapter / anchor gate | 新随机初始化 | 训练 |
| StructuredMacroEncoder | 新随机初始化 | 训练 |
| side projection / core mixer | 新随机初始化 | 训练 |
| timing / progress / branch heads | 新随机初始化 | 训练 |

因此它是“保留 foundation-model 预训练、任务模型从头监督训练”，而不是沿用当前 timing
checkpoint 的 warm start。

### 7.2 训练阶段

#### Phase A：静态 cache 构建

- 冻结离线 Coding LLM；
- 对所有训练二进制的唯一静态 macro/BB 编码；
- 对 validation/deployment 二进制也可在评测前编码，因为只读取静态汇编，不读取 label；
- manifest 审计不得包含 split/workload identity 可学习字段；
- cache lookup 和 binary/static join 必须 100% 或 fail closed。

#### Phase B：新任务模型初始化

- 加载冻结的离线 semantic cache；
- 在线 macro-level Qwen 只加载对应 base model 的公开预训练权重；
- 新建 LoRA、semantic adapter、numeric encoder、side projection、core mixer 和各输出头；
- 所有新任务参数使用固定 seed 随机初始化；
- 不从当前 macro-v29 8K checkpoint 复制任何 task parameter；
- 将初始化策略、base model revision、随机 seed 和 cache manifest 写入 run contract。

#### Phase C：只用真实标签监督训练

保留当前 macro-v29 损失：

- log1p commit-time smooth-L1；
- horizon prefix BCE；
- horizon progress；
- sequence cumulative drift；
- branch token 和 branch count。

~~~text
L_total = L_v29_real_labels
~~~

主线损失中不得出现当前 checkpoint 的 hidden 或预测。第一版冻结离线 semantic encoder，
训练：

- semantic adapter / anchor gate；
- online Qwen LoRA；
- current StructuredMacroEncoder；
- side projection、core mixer 和 timing/branch heads。

#### Phase D：closed-loop fine-tuning（可选）

只有 Phase C 的离线 validation 和自由 rollout 都稳定后，才考虑基于预测 cursor 的短序列
fine-tuning。不能在尚未证明基础语义/吞吐时引入 RL 或长闭环训练。

### 7.3 蒸馏只作为后续可选消融

无蒸馏主线是唯一必做结果。只有同时满足以下条件，才允许追加蒸馏实验：

1. 当前 native-token baseline 已完成同一 deployment split 的完整精度验证；
2. 无蒸馏新模型已完成训练和闭环评测，但精度仍明显不足；
3. native-token baseline 在目标指标上确实优于无蒸馏模型；
4. 蒸馏只使用 train split，不读取 validation/deployment teacher 输出；
5. 蒸馏结果作为 `supervised + optional distillation` 独立行报告，不能替代无蒸馏主线。

若做该消融，优先只蒸馏最终 commit-time/progress 软输出，不默认对齐 hidden。两种架构的
hidden 坐标没有唯一对应关系，hidden cosine/MSE 可能错误限制 macro-soft-token 模型。任何
蒸馏权重都必须预注册，并同时证明 validation 和 closed-loop 改善。

### 7.4 防止“用了 cache 就不再是 LLM 语义”

正式语义 gate 至少包含：

| 变体 | 定义 |
|---|---|
| `real_semantic` | 真实汇编离线 Qwen vector + online macro Qwen |
| `zero_semantic` | 静态语义向量置零，容量和其他输入不变 |
| `mnemonic_shuffle` | 保持频率但确定性打乱 mnemonic/semantic mapping |
| `random_encoder` | 同形状固定随机向量，其他训练完全相同 |
| `anchor_only` | 只有 pooled native token embedding，无离线 full-Qwen hidden |
| `register_rename` | 一致寄存器重命名，检查语义稳定性 |

`real_semantic` 必须在配对 heldout workload 上优于 zero/shuffle/random，并证明相对
`anchor_only` 的增益，才能说明收益来自完整离线 LLM 语义而不只是词嵌入。

## 8. 部署流程与缓存层次

### 8.1 部署前

1. 验证 binary build-id、module map、decoder 和 static dictionary；
2. 枚举所有唯一静态 macro；
3. 根据 semantic cache manifest 查缺补漏；
4. 将目标二进制的紧凑 semantic table 映射到连续 static IDs；
5. 可将完整 table 一次搬到 GPU，或建立 pinned-CPU + GPU hot cache；
6. 加载 online macro-Qwen checkpoint，并验证 semantic schema/hash 契约。

部署热路径不加载离线 semantic encoder。

### 8.2 每个 rollout step

1. 从每个 active core 的 predicted macro cursor 取未来最多 256 条 macro；
2. 根据 static ID 批量 gather `z_static` 和 `anchor`；
3. 构造当前 ragged UOP、dynamic、summary、relation、state 和 uarch tensor；
4. 生成 `[R,256,D_qwen]` macro soft tokens；
5. 一次 batched online Qwen forward；
6. late-fuse numeric/side features；
7. cross-core mixer 和 timing/branch heads；
8. scheduler 选择全局 `delta`，推进各核 whole-macro cursor；
9. 更新功能状态并进入下一步。

### 8.3 Cache 层次

~~~text
L0 persistent semantic cache:
  binary/instruction/schema hash -> frozen Qwen semantic vector

L1 run-local static table:
  dense static_id -> compact semantic vector

L2 optional GPU hot table:
  active binary's frequent static_ids -> device tensor
~~~

这与当前 token cache 不同：token cache 保存 tokenizer 结果；semantic cache 保存 frozen Qwen
已经提取的语义。动态状态、online Qwen contextual hidden 和 timing 输出不能放入永久静态缓存。

### 8.4 Cache miss、JIT 和自修改代码

- 普通静态二进制：部署前补齐，热路径 miss 视为合同错误；
- JIT：以 code-object/bytes hash 为 key，由独立冷路径编码，再原子发布到 run-local table；
- 自修改代码：instruction bytes 变化必须使 key 失效；
- 不允许 miss 时退回 UNKNOWN 向量并继续计入正式精度/吞吐报告，除非该行为是预注册消融。

## 9. 性能预算

### 9.1 Position 数和理论方向

当前代表性 c8 step：

~~~text
about 20,096 native text positions
~~~

新方案：

~~~text
8 active cores * 256 macro positions = 2,048 positions
~~~

线性 token/MLP 工作量约减少 `20,096 / 2,048 = 9.8x`。若每核长度近似相等，local
attention 元素数约从：

~~~text
8 * 2500^2 ~= 50,000,000
~~~

下降到：

~~~text
8 * 256^2 = 524,288
~~~

约 96 倍。端到端不会获得 96 倍，因为仍有 Qwen MLP、projection、numeric encoder、
context builder、cross-core mixer、heads 和调度器。

### 9.2 达到 10K macro/s 的必要条件

~~~text
throughput = retired_macros_per_step / wall_seconds_per_step
~~~

若保持当前短测约 829 macro/step：

~~~text
829 / 10,000 = 82.9 ms/step
~~~

若 stride/预测使每步达到约 1,200 macro：

~~~text
1,200 / 10,000 = 120 ms/step
~~~

因此首轮工程 gate：

| Gate | 要求 |
|---|---:|
| P0 表示压缩速度 gate | c8 real checkpoint 单步 p50 `<= 150 ms` |
| 最终吞吐 gate A | `>= 829 macro/step` 且 p50 `<= 82.9 ms` |
| 最终吞吐 gate B | `>= 1200 macro/step` 且 p50 `<= 120 ms` |
| 完整主指标 | 23 条 c8 deployment trace mean `>= 10K macro/s` |

`70-150 ms/step` 只是需要验证的设计区间，不是已测承诺。若 P0 同尺寸 1.5B 仍无法进入
该区间，再比较使用预训练权重、由真实标签监督训练的 0.5B/0.6B online Qwen；不能用
理论 attention 降幅替代真实测量。

### 9.3 Scheduler 优化顺序

表示压缩先固定 `K_macro=256`、`target_stride_macro=128`，与当前基线严格 A/B。吞吐和
闭环精度通过后，依次测试 stride 160、192、224。

提高 stride 可能增加每步退休量，但会改变预测误差传播和全局时间推进，必须同时报告：

- macro/s 和 UOP/s；
- step latency；
- retired macro/step 分布；
- capped steps / zero-core rows；
- ROI elapsed-cycle error 和长期 drift。

不能只通过增大 stride 达到吞吐数字而不检查闭环精度。

## 10. 与当前代码的对应关系

当前实现：

| 责任 | 当前文件 | 新方案 |
|---|---|---|
| 静态汇编/token cache | `data/build_macro_v29_token_cache.py` | 保留为 native baseline/anchor；新增 semantic cache builder |
| macro/UOP context | `train/macro_v29_dataset.py` | 保留动态/side 构造；新增 static semantic gather |
| native Qwen + span pool | `model/macro_v29_model.py` | 新增 macro-soft-token backbone path |
| loss | `model/macro_v29_model.py` | 主线只保留真实标签 v29 loss；蒸馏不得进入默认配置 |
| train | `train/train_macro_v29.py` | 新增 input mode、cache provenance 和 fresh task initialization |
| free rollout | `eval/macro_v29_rollout.py` | scheduler/label isolation不变；predictor改 semantic input |
| scheduler | `eval/macro_v29_scheduler.py` | 不变，stride实验显式写 manifest |

本次已经新增：

~~~text
data/build_macro_v29_semantic_cache.py
model/macro_v29_model.py                   # 显式 semantic_input_mode
scripts/build_macro_v29_semantic_cache.sh
scripts/run_macro_v29_semantic_c8_mainline.sh
scripts/bench_macro_v29_semantic_p0.py
scripts/bench_macro_v29_semantic_throughput.sh
scripts/train_macro_v29_semantic_supervised.sh
tests/test_macro_v29_semantic_cache.py
tests/test_macro_v29_semantic_model.py
~~~

不建议直接删除当前 native-token path。两条路径至少保留到同一数据、同一 scheduler、同一
checkpoint budget 下完成精度/吞吐 A/B。

### 10.1 Checkpoint contract 新字段

~~~text
semantic_input_mode: native_token | cached_macro_soft_token
semantic_encoder_model
semantic_encoder_revision/hash
semantic_prompt_schema_version
semantic_pooling_policy
semantic_cache_manifest_hash
semantic_dim
anchor_policy
online_backbone_model/revision
online_backbone_input_unit: macro
task_init_source: fresh
init_timing_checkpoint: null
supervision_mode: real_labels_only
distillation_enabled: false
K_macro
target_stride_macro
~~~

checkpoint、run manifest 与 semantic cache 任一不匹配都必须在模型 forward 前失败。

## 11. 验证与验收

### 11.1 数据与缓存正确性

- binary/static instruction join 100%；
- instruction bytes、decoder length 和 normalized asm 一致；
- cache key 对 binary/model/tokenizer/prompt/pooling 版本敏感；
- 随机抽样重算 semantic vector，与缓存误差满足 BF16/导出精度容差；
- cache 中不存在 label/oracle/workload/seed/split identity；
- train/validation/deployment 的 label 使用边界不变。

### 11.2 模型合同

- 输入严格为 `[R,256,D]`，尾部 mask 正确；
- 一次 batched online Qwen，不得隐式逐核循环；
- commit time 单调、有效 gap 为正；
- core permutation equivariance 通过；
- macro/UOP exactly-once 通过；
- cache miss fail-closed 行为通过；
- 新主线未加载当前 native timing checkpoint，run contract 明确为
  `distillation_enabled=false`，且不存在 teacher 模型/输出/损失字段；
- native baseline 与新方案的 source/window/split 配对完全一致。

### 11.3 语义 gate

- `real_semantic` 配对优于 zero/shuffle/random；
- `real_semantic` 优于 `anchor_only`，证明 full-Qwen cached hidden 的附加价值；
- register rename 退化受控；
- compute/branch/memory/stride/reuse 代表性 workload 都有覆盖；
- 不因 static ID、binary identity 或绝对 PC 形成 shortcut。

### 11.4 精度 gate

至少报告：

- validation commit-time WAPE；
- prefix/progress/branch 指标；
- deployment free-running ROI elapsed-cycle error；
- workload mean/median/max；
- 长期 signed drift；
- heldout business workloads；
- 相对当前 native-token 8K checkpoint 和 v9/v11 历史基线。

吞吐达标但闭环误差明显退化时，不能替换当前方案；应优先修复表示、训练和动态特征。
蒸馏只能按 7.3 节作为额外消融，不能成为掩盖无蒸馏主线退化的默认补丁。

### 11.5 吞吐 gate

- 首先用同一条代表性 c8 trace 做 100+ step 稳态基准；
- 计时前 warmup，CUDA synchronize，排除模型加载；
- 分解 semantic gather、context、online Qwen、numeric/heads、scheduler；
- 再跑完整 23 条 deployment trace；
- 主结论使用 per-trace/GPU 8-core aggregate macro/s；
- 8-GPU aggregate 单列；
- 同时记录 GPU utilization、peak allocated/reserved memory 和功耗。

## 12. 风险与缓解

| 风险 | 原因 | 缓解 |
|---|---|---|
| macro soft token 偏离 Qwen embedding 分布 | cached hidden 不是词表 embedding | anchor residual、LayerNorm、small gate、LoRA、真实标签训练 |
| 丢失 token-level 邻接细节 | 一条 macro 压成一个 position | 离线 BB context、在线 macro Qwen、语义控制组与闭环验证 |
| 静态向量缺少动态行为 | 静态语义本来不含 reuse/cache状态 | 保留 StructuredMacroEncoder 和 side channel |
| 当前 checkpoint 不能直接复用 | 输入单位和分布改变 | 不复用 timing checkpoint；新任务模块从头监督训练 |
| cache 身份泄漏 | 绝对 PC、路径或 workload 名进入 key/vector | binary验证但对模型隐藏身份；规范化 prompt；leakage probes |
| cache 过大 | 保存完整 BF16 hidden | 训练后导出 256/512维 compact table，按 binary 分片 |
| P0 仍达不到 10K | 1.5B MLP 成本仍高 | 先测 stride，再用预训练 0.5B/0.6B Qwen 从头训练新任务模块 |
| stride 增大破坏闭环精度 | 每步跨度和误差传播改变 | 分档审计 128/160/192/224，精度与吞吐同榜 |
| cache miss 导致长尾 | 热路径临时加载离线模型 | 部署前预扫描；独立冷路径；正式运行 fail closed |

## 13. 实施顺序

### Phase 0：不训练的速度上限

1. 在当前模型增加仅用于 benchmark 的“跳过 native Qwen”路径；
2. 用假 macro embeddings 测 `[8,256,D]` 同尺寸 Qwen forward；
3. 测 semantic gather + current numeric/core/head 的非 Qwen 开销；
4. 若 P0 速度上限明显低于 10K 所需预算，先处理在线 backbone/stride，不构建全量 cache。

该 benchmark 不能用于精度结论，也不能把 `side_only` 当前实现当作跳过 Qwen；当前
`side_only` 为容量匹配仍执行 backbone 后再乘零。

已实现入口：

~~~bash
/data00/yinhaolang/infer/.venv/bin/python \
  scripts/bench_macro_v29_semantic_p0.py \
  --device cuda:0 --rows 8 --macros 256 --warmup 10 --iterations 50
~~~

脚本在 CUDA 不可用时拒绝输出 gate 结论，避免把 CPU 延迟误当作 GPU P0 结果。

### Phase 1：最小真实 semantic cache

1. 选一个 base workload + 一个 heldout workload；
2. 完成 static prompt、cache key、frozen Qwen encoding 和重算校验；
3. 训练新初始化的 semantic adapter + timing heads，online Qwen backbone 冻结、只开新 LoRA；
4. 通过 real/zero/shuffle/anchor-only 小规模语义门禁。

### Phase 2：c8 无蒸馏监督训练

1. 为正式 c8 train/validation 静态字典建 cache；
2. 在线 Qwen 从公开预训练权重初始化，新任务模块使用固定 seed 随机初始化；
3. 只使用真实 commit/prefix/progress/drift/branch 标签训练；
4. 对同一 validation source 比较当前 native baseline 与新监督模型；
5. 跑短 free rollout，检查 drift、exactly-once 和速度。

### Phase 3：完整部署验证

1. 构建 seed1/deployment 静态 cache，只读取静态汇编；
2. 跑 23 条 c8 完整 free-running trace；
3. 报告 per-trace/GPU macro/s、UOP/s、ROI error 和 stage timing；
4. 在精度通过后测试 stride 160/192/224；
5. 若同尺寸 1.5B 仍不足，再使用预训练 0.5B/0.6B online Qwen 从头训练其新任务模块。

## 14. 最终判定标准

该方案只有同时满足以下条件，才能替代当前 native-token 部署路径：

1. 静态语义确实来自有 provenance 的 Coding LLM，而不是随机/纯结构化向量；
2. 在线仍有 macro-level Qwen 或明确标注的小模型消融；
3. label isolation、core equivariance、macro/UOP exactly-once 全部通过；
4. 同一 c8 deployment 口径达到 `>= 10K aggregate macro/s`；
5. closed-loop ROI error 和长期 drift 达到预注册精度门槛；
6. semantic cache 的存储、构建时间、命中率和冷启动策略被完整报告；
7. 当前 native-token baseline、v9/v11 历史基线和新方案同口径对比；无蒸馏新方案必须单列。

一句话概括：

> 用 frozen Coding LLM 把重复的静态汇编阅读从 rollout 热路径移到离线缓存，再让在线
> Qwen 以“一条 macro 一个语义 position”的形式处理动态序列；保留 LLM 语义，同时把
> c8 每步的 Qwen position 数从约 20K 降到 2,048。

## 15. 当前实现验收审计

| 检查项 | 状态 | 证据/下一步 |
|---|---|---|
| static join、bytes/decoder 校验 | PASS（代码和真实小样本） | resolver fail-closed；真实 Qwen smoke 使用实际 Parquet |
| key 对 binary/PC/bytes/context/model/tokenizer/prompt/pooling 敏感 | PASS | `test_semantic_key_is_provenance_sensitive` |
| parquet/shard/PC-set 被修改后失效 | PASS | `test_static_and_shard_mutation_invalidates_cache` |
| cache miss 禁止 UNKNOWN fallback | PASS | `CachedSemanticSource.gather_window` 合同测试 |
| 输入为 `[R,256,D]`、一次 batched Qwen | PASS | 8-row counting-backbone 测试；真实 Qwen `1x256x1536` smoke |
| 尾部 mask、commit 单调、core equivariance | PASS | 单元测试 |
| 真实标签 loss/backward/checkpoint | PASS（真实 Qwen c8 8K） | run contract v4；最终 checkpoint step 8000 |
| teacher / distillation 隔离 | PASS | semantic 模式禁止 `--init-trainable`；contract 显式 false |
| label-free rollout / scheduler / stage timing | PASS（tiny 合同 smoke） | 3-step c8 rollout，无 label key |
| 完整 c8 real semantic cache | PASS | manifest hash `815b8daa...9072673c`，23 binaries |
| CUDA P0 `p50 <= 150 ms` | 待按最终 checkpoint 重测 | 运行 `bench_macro_v29_semantic_p0.py`，不能以训练 step/s 代替 |
| c8 8000-step 无蒸馏训练 | PASS | `macro_v29_semantic_c8_8k_20260719_011546`，训练侧 WAPE 18.1678% |
| real/zero/shuffle/random/anchor-only 语义 gate | 待训练 | 不以 tiny/CPU smoke 代替 |
| 23 条 c8 deployment 精度与 `>=10K macro/s` | 待训练后运行 | 使用 throughput shell 和完整 rollout 清单 |

因此当前结论是“soft-macro 实现、合同 smoke 和 c8 8K 训练完成”，不是“部署模型效果和
10K 指标已验收”。

## 16. vNext：384 维 Macro State + 逐 Macro 跨核 Attention

### 16.1 状态边界与决策

本节描述的是已经落地到代码、但尚未进入正式 vNext checkpoint 的改进主线。当前已训练
模型仍为：

~~~text
soft macro -> online per-core Qwen -> macro_state[256]
           -> mean over 256 macros -> one core_summary
           -> summary-level core mixer -> broadcast to every macro
~~~

vNext 固定为：

1. 保留离线 frozen-Qwen semantic cache 和在线 macro-level Qwen；
2. `macro_state` 从 256 提高到 384；
3. 删除“每核平均成一个向量再广播”的跨核主路径；
4. 使用 TCSim full-QKVR 思路，让每条 macro 分别读取其他核的 macro state；
5. c8 先完成精度和部署吞吐 A/B，c32 只在分块显存门禁通过后训练；
6. 继续只用真实标签，从公开预训练 Qwen 和全新任务模块初始化，不引入 teacher/student
   蒸馏或旧 timing checkpoint。

实现与证据边界如下：

| 项目 | 当前状态 | 代码/证据 |
|---|---|---|
| 384 维 macro state | 已实现 | `MacroV29Config.d_model=384` |
| 逐 macro 跨核 attention | 已实现 | `MacroCrossCoreMixer`，输出保持 `[R,256,384]` |
| 排除自身核心、scheduler context 隔离和 valid mask | 已实现并测试 | `tests/test_macro_v29_model.py` |
| 目标核分块 | 已实现并通过分块/不分块等价测试 | `cross_target_block` |
| Qwen core-row 分批 | 已实现并通过数值等价测试 | `backbone_core_chunk_size` |
| 精确 LoRA 梯度重计算 | 已实现并通过梯度测试 | non-reentrant activation checkpoint |
| 峰值显存记录 | 已实现 | step log 与 final report 的 peak allocated GiB |
| 旧 256-summary checkpoint 兼容 | 已实现并测试 | 旧 `run.json` 无新 schema 时自动选择 legacy summary mixer |
| 完整单元/合同测试 | PASS | 44 tests passed |
| 真实 c8 20--100 step GPU 显存门禁 | 待执行 | 不能用 CPU/tiny 测试替代 |
| 正式 c8 s1 30000-step 训练 | 待执行 | 使用本节的一键脚本，从 step 0 开始 |
| c8 deployment 精度与吞吐 | 待执行 | 训练完成后单独做 label-free rollout |
| c32 训练 | 本轮不执行 | 仅在 c8 精度、吞吐和显存均通过后评估 |

因此，“实现完成”只表示模型、训练合同和显存控制路径已经落地并通过确定性测试，不表示新
模型已经达到精度或 `>=10K aggregate macro/s`。

### 16.2 vNext 总体架构

~~~text
normalized static assembly / local BB context
  -> offline frozen Qwen
  -> static_semantic[1536] + anchor[1536]
  -> gated semantic adapter
  -> one soft macro position per instruction

per-core soft macro sequence [C, 256, 1536]
  -> one batched online Qwen+LoRA call; cores remain independent batch rows
  -> online hidden [C, 256, 1536]
  -> asm projection [C, 256, 384]

structured UOP / dynamic / position / side features
  -> projections to 384
  -> local macro_state [C, 256, 384]

local macro_state
  -> MacroCrossCoreMixer: each macro queries all valid macros on other cores
  -> cross-enhanced macro_state [C, 256, 384]
  -> gap / branch / progress / cumulative heads
~~~

Qwen 的核间隔离保持不变。c8 和 c32 的在线 Qwen 输入分别是
`[8,256,1536]` 和 `[32,256,1536]`，Qwen attention 复杂度仍为
`C * 256^2`，不是 `(C * 256)^2`。只有 Qwen 之后的 384 维跨核 mixer
执行逐 macro 跨核 attention。

### 16.3 Macro State 维度

第一版 vNext 使用：

~~~text
d_macro = 384
n_heads = 8                 # head_dim = 48
cross_layers = 1
cross_ffn_dim = 1536
cross_gate_init = -2.0      # sigmoid 约 0.119，避免初始跨核残差过强
~~~

384 相对 256 提高 50% 表征容量，同时避免 512 维带来的二次投影/FFN增长。需要同步修改：

- `asm_projection`；
- `StructuredMacroEncoder` 输出；
- `side_projection`；
- `position_embedding`；
- fusion norm；
- gap、branch 和 progress heads；
- checkpoint contract 中的 `d_macro`、mixer schema 和分块策略。

soft macro 本身仍为 Qwen hidden size 1536；384 只是 Qwen 输出与结构化特征融合后的
`macro_state`，不能混淆这两个维度。

### 16.4 TCSim 式逐 Macro 跨核 Mixer

对每个 scheduler context 和目标核 `i`：

~~~text
Q_i = RProj(macro_state[i, :, :])               # [256, 384]
K_i = KProj(macro_state[all other cores, :, :]) # [(C-1)*256, 384]
V_i = VProj(macro_state[all other cores, :, :]) # [(C-1)*256, 384]

cross_i = SDPA(Q_i, K_i, V_i, valid_macro_mask)
gate_i  = sigmoid(CrossGate(relation_i, state_i))

macro_state_i = macro_state_i
              + gate_i * OProj(cross_i)
              + FFN(Norm(...))
~~~

每条 macro 因 query 不同而获得不同的跨核上下文，解决当前同一 `core_context` 广播给本核
全部 256 条 macro 的信息瓶颈。单核内部序列关系已经由 28 层在线 Qwen 建模，因此 vNext
mixer 第一版只做跨核 attention，不再重复一套 384 维 local self-attention。

必须保持：

- core permutation equivariance；
- 尾部/失活核心 mask；
- 不允许同一目标核进入自己的 cross K/V；
- 不允许跨不同 scheduler context 或不同 sequence step 互相 attention；
- relation/state 只作为动态 gate/bias，不把标签或 oracle 字段引入输入。

### 16.5 复杂度与性能预算

令 `C` 为活跃核数，`K=256`。跨核 attention 的逻辑 token-pair 数为：

~~~text
N_cross = C * (C - 1) * K^2
~~~

| core count | cross token-pairs / layer | 相对 c8 |
|---:|---:|---:|
| 8 | 3,670,016 | 1.00x |
| 32 | 65,011,712 | 17.71x |

c8 一层 384 维 mixer 的新增部署延迟必须实测，不能从 FLOPs 直接宣称达到 10KIPS。预期
主要瓶颈仍是 Qwen-1.5B，而不是这一层 mixer；但 c32 的跨核计算已经足够大，必须采用下面的
分块实现。

### 16.6 显存优化：参考 TCSim，但不复制其模型边界

#### A. 融合 SDPA

- 使用 PyTorch SDPA 的 Flash/Efficient/cuDNN 可用后端；
- 禁止 eager 路径持久化完整 attention score；
- backend 必须通过 finite forward/backward 门禁，不能为了速度重新引入已观察到的 BF16 NaN；
- 训练、部署分别记录实际选中的 kernel 和峰值显存。

#### B. 按目标核分块

逻辑数学保持完整 cross attention，物理执行按目标核分批：

~~~text
c32 initial cross_target_block = 4 or 8
~~~

当前 c8 一键训练也使用 `cross_target_block=8`，用于统一训练/后续扩核代码路径；这不会把
attention 近似化或截断。它只改变目标 query 核的物理分批方式，每个目标 macro 仍读取当前
scheduler context 内全部其他活跃核的有效 macro。

每批只构造该组目标核对应的 Q 和其他核 K/V，完成后立即释放临时 workspace。分块结果必须
与未分块小规模参考在容差内一致。实现应按 active-core count 分桶，避免 Python 逐核 SDPA
launch。

#### C. Qwen 核分批与重计算

Qwen 的每核序列互相独立，因此 c32 可按 4/8 个 core row 分批：

~~~text
soft_macro core chunk
  -> checkpointed online Qwen
  -> immediately project 1536 -> 384
  -> retain only compact macro_state for global mixer
~~~

当前 c8 一键配置为 `backbone_core_chunk_size=8`，并打开外层精确 chunk activation
checkpoint。c8 时一个 scheduler context 通常正好是一组；当 batch 中 row 更多或未来扩到
c32 时才会切成多组。内部 Qwen gradient checkpoint 默认关闭，避免与外层重计算重复；它仍
可通过环境变量显式打开做显存 A/B。

训练时禁止简单 `detach` 或在独立 chunk 上提前 optimizer step，否则 LoRA 得不到正确的全局
跨核梯度。可接受的精确实现是 core-chunk activation checkpoint，或两遍重算：先得到紧凑
macro states 并反传 mixer 对它们的梯度，再逐 chunk 重算 Qwen 并执行 vector-Jacobian
backward。部署前向可直接分批，不需要保留反向图。

#### D. Sequence step 隔离

当前第一轮正式训练配置改为：

~~~text
batch_size = 1 per GPU
sequence_length = 1
sequence_stride = 1
max_steps = 30000
~~~

每个 step 只执行一个时间点的 c8 跨核 attention，不会把多个时间点拼成一个 attention
序列。该设置与已训练的 s1 基线更容易做同口径比较，也显著降低单步激活；代价是需要至少
两个连续窗口才生效的 cumulative-drift loss 在本轮为 0。因此 s1 30K 只能验证单窗口精度、
逐 macro 跨核建模和部署吞吐，不能替代后续 s4 长期 drift 实验。c32 的初始配置仍必须同时
打开 Qwen core chunk、cross target block 和 activation checkpoint。

#### E. Cache 边界

部署时可以缓存：

- `static_semantic`、`static_anchor`；
- 训练完成后固定 adapter 生成的 soft macro；
- 静态解码和结构化字段。

不能按静态指令直接缓存在线 Qwen 最终 hidden 或跨核 attention 输出，因为它们依赖当前
256-macro 序列、动态侧信息和其他核心状态。TCSim 的 cache 思路只用于可证明静态的边界。

### 16.7 实施与验收顺序

1. **已完成**：实现 `d_macro=384` 和单层 `MacroCrossCoreMixer`，增加未分块小张量参考测试；
2. **待执行**：c8、`sequence_length=1` 做前 20--100 step forward/backward、梯度和峰值显存门禁；
3. c8、`sequence_length=1` 从头进行 30000-step 真实标签训练；
4. 与当前 256-summary soft-macro checkpoint 做同数据、同 seed、同训练预算 A/B；
5. 跑完整 c8 deployment traces，报告精度、drift、macro/s、stage timing 和峰值显存；
6. 只有 c8 同时通过精度与 `>=10K aggregate macro/s`，才进入 c32；
7. c32 先做单 batch 分块前向/反向和显存 sweep，再决定正式训练，不通过时不得用 OOM
   重启循环替代设计修复。

正式 c8 一键命令：

~~~bash
bash scripts/tmp/run_macro_v29_vnext_c8_s1_30k_all_in_one.sh
~~~

默认展开后的关键合同：

~~~text
cores=8
max_steps=30000
batch_size_per_gpu=1
sequence_length=1
sequence_stride=1
d_macro=384
n_heads=8
cross_layers=1
cross_target_block=8
backbone_core_chunk_size=8
backbone_chunk_checkpoint=true
supervision_mode=real_labels_only
distillation_enabled=false
init_timing_checkpoint=null
architecture_schema=macro-v29-soft-cross-core-1
~~~

脚本自身包含 `nohup`，因此不需要在外层再写 `nohup ... &`。训练日志位于
`logs/tmp/<RUN_NAME>/train_30000.log`，PID 位于
`logs/tmp/<RUN_NAME>/launcher.pid`，checkpoint 位于 `ckpt/<RUN_NAME>/`。multiprocessing
临时目录固定使用项目内短路径 `tmp/mv29`，避免长 `RUN_NAME` 使 AF_UNIX socket 路径超过
系统限制；不使用系统 `/tmp`。

vNext 除继承第 11、14 节全部合同外，新增以下硬门槛：

| gate | 要求 |
|---|---|
| macro-specific cross context | 同核不同 macro 的 cross output 不得退化为完全相同广播向量 |
| permutation equivariance | 任意重排 core rows 后，输出按相同置换变化 |
| block equivalence | 分块与未分块 attention 在注册容差内一致 |
| gradient integrity | semantic adapter、online LoRA、cross mixer、timing heads 均收到 finite 非零梯度 |
| memory report | c8/c32 分别报告 forward、backward 和部署峰值，不使用估算替代 |
| deployment throughput | c8 完整口径 `>=10K aggregate macro/s` |
| no distillation | `distillation_enabled=false`，`init_timing_checkpoint=null` |

一句话概括 vNext：

> 保留离线和在线 Qwen 的 LLM 语义，将 Qwen 后的 macro state 扩展为 384 维，并用可分块的
> TCSim 式逐 macro 跨核 attention 代替单一 core summary；通过融合 SDPA、目标核分块和
> Qwen 核分批重计算，让 c32 的扩展主要转化为可控制的计算开销，而不是不可控的激活或
> attention-score 显存。

### 16.8 部署调度改为无尾部 lookahead

当前部署主线固定：

~~~text
K_macro = 256
target_stride_macro = 256
tail_lookahead_macro = 0
~~~

每个活跃核每次模型前向仍预测 256 个 macro，scheduler 每步允许消费的上限也为 256。
实际消费量仍由所有活跃核候选完成时间的全局最小值和 `max_step_cycles` 决定，因此“256”是
单核单步上限，不要求所有核每步恰好同时推进 256。

同一真实 commit tick 的 macro group 若跨过窗口边界，下一窗口允许用 0-cycle step 继续消费，
保证 macro/UOP exactly-once；不再以 tail tie guard 拒绝 `stride == K`。这一变更不改变训练
输入窗口和逐 256-macro 监督，只改变部署 closed-loop 的重规划间隔。已有 checkpoint 内记录的
旧 `target_stride_macro=128` 保留为训练 provenance，部署命令显式使用 256；后续新训练默认
记录 256。

## 17. A/B/E：LLM Backbone 与语义贡献的重训练验证

### 17.1 要回答的问题与当前证据边界

该实验必须分开回答三个问题：

1. 当前 checkpoint 是否使用 LLM 分支；
2. 离线 LLM 静态语义是否提供了不可由结构化输入替代的信息；
3. 在已有离线语义的前提下，在线 Qwen-1.5B 是否比容量匹配的普通 Transformer 更有价值。

第一阶段已在同一个 30K checkpoint 上完成 23 条 c8、`stride=256`、无 lookahead 的
post-hoc 干预：

| variant | ROI CPI MAPE | makespan MAPE | 相对 full_real 的含义 |
|---|---:|---:|---|
| `full_real` | 6.872% | 7.045% | 当前完整模型 |
| `semantic_permute` | 13.735% | 13.894% | 正确 PC 与语义映射被破坏 |
| `no_lora` | 15.719% | 15.869% | 训练完成后关闭 LoRA |
| `no_llm_branch` | 19.235% | 19.944% | Qwen 输出到融合层前被清零 |

完整报告位于
[`eval_results/macro_v29_stage1_posthoc_c8_20260719_165753/report.md`](../eval_results/macro_v29_stage1_posthoc_c8_20260719_165753/report.md)。
总体 23 条 trace 上的结果证明当前 checkpoint 使用且依赖正确语义映射、LoRA 增量和整个
LLM 分支；但这是训练后干预，存在强分布偏移。7 条 heldout workload 上的主要区间仍跨 0，
因此不能据此声称在线 Qwen 不可替代，也不能把 checkpoint 共适应等同于 LLM 语义泛化。

`no_offline_hidden` 也不是“无语义”控制：它只去掉 gated semantic residual，仍保留由汇编
token embedding 得到的 `static_anchor`。真正的无 LLM 控制不能继续读取
`static_semantic` 或 `static_anchor`。

### 17.2 A/B/E 三个正式变体

三个变体均从头训练任务参数，保留相同的 c8 数据、结构化 UOP 分支、side 分支、位置分支、
逐 macro cross-core mixer、输出头、损失和部署 scheduler。

| ID | 静态/语义输入 | 每核在线序列 backbone | 实验目的 |
|---|---|---|---|
| **A: Full-Qwen-Real** | 正确的 `static_semantic + static_anchor` | 预训练 Qwen2.5-Coder-1.5B + LoRA | 当前完整主线 |
| **B: Transformer-Real** | 与 A 完全相同的正确 soft-macro 输入 | 容量匹配、从头训练的普通 causal Transformer | 判断在线 Qwen 是否必要 |
| **E: Transformer-Structured-Only** | 不读取 semantic cache；使用可训练的同尺度 `NO_SEM` 输入，真实功能信息只来自现有 numeric/side 分支 | 与 B 完全相同的普通 causal Transformer | 判断离线 LLM 语义是否必要 |

其中 B **仍然使用离线 Qwen 语义**，只替换在线 backbone；所以 `A ≈ B` 只能说明在线
Qwen 可被小 Transformer 替代，不能说明 LLM 语义无用。E 才是不读取离线或在线 LLM 表示的
端到端无 LLM 基线。E 的 Transformer 保留完整可训练容量，但输入只包含一个可训练的
`NO_SEM` macro token 和内部位置编码；整体模型仍通过未改动的 numeric/side 分支获得真实
结构化 UOP、动态关系和微架构信息。这样不会通过新增 raw PC/hash 字段引入静态身份 shortcut。

三个核心比较的含义固定为：

~~~text
A vs B：在线预训练 Qwen backbone 的增量价值
B vs E：离线 LLM 静态语义的增量价值
A vs E：整个离线 + 在线 LLM 方案的总价值
~~~

### 17.3 B/E 普通 Transformer 合同

B 与 E 使用完全相同的普通 backbone：

~~~text
per-core input [R,256,1536]
  -> LayerNorm
  -> Linear(1536 -> 384)
  -> 5 x causal Transformer block
       d_model = 384
       n_heads = 8
       FFN = 1536
       pre-norm + residual
  -> per-macro output [R,256,384]
~~~

它继续把每个核作为独立 batch row，不把多个核拼成一条序列；核间信息仍只由下游
`MacroCrossCoreMixer` 建模。5 层 384 维 backbone 连同输入投影为 9,556,992 个可训练参数，
对齐 A 中 8,716,288 个 LoRA 参数 + 593,280 个 Qwen 输出投影参数，共 9,309,568；差异为
2.66%。允许误差不超过 5%，并在 run contract 中同时记录：

- backbone 总参数和可训练参数；
- 单 forward FLOPs 或可复现的 stage timing；
- 峰值 allocated/reserved memory；
- 训练和部署的真实 macro/s。

参数量匹配用于比较相同可训练容量下的表示能力；另行报告相同 wall-clock 预算下的结果，用于
判断工程上是否值得保留在线 Qwen。不得只用“相同 step”掩盖两者训练/部署成本差异。

### 17.4 公平训练合同

第一轮筛选使用与当前 A 相同的 seed `1234` 和固定样本顺序；正式结论至少使用 3 个训练
seed。每个变体必须满足：

- c8、`K_macro=256`、`d_macro=384`、单层逐 macro cross-core mixer；
- 每卡 batch 1、8 卡、`sequence_length=1`、30000 optimizer steps；
- 相同 train/validation/deployment source、split、窗口和顺序；
- 相同下游模块初始张量、loss 权重、warmup、weight decay 和 gradient clip；
- A 不加载旧 timing checkpoint；B/E 不从 A 蒸馏或复制 hidden/output；
- 以固定 30000-step checkpoint 为主，不能为单个变体事后挑选最佳 step；
- 若需要学习率 sweep，A/B/E 获得相同数量、预先登记的候选，且只由 validation 选择；
- 每个 run 写入 backbone 类型、语义输入类型、参数量、数据顺序 hash 和完整 checkpoint
  provenance。

当前 A 的 seed-1234 30K checkpoint 可以作为第一轮配对基线；若进入正式结论阶段，必须补齐
A/B/E 其余 seed，不能用“一个 A seed 对多个 B/E seed”计算显著性。

### 17.5 评测与统计口径

训练 loss 和 teacher-forced validation 只用于健康检查。主结论来自完全 label-free 的 c8
deployment rollout，固定：

~~~text
K_macro = 256
target_stride_macro = 256
tail_lookahead_macro = 0
max_steps = 0  # 完整 ROI
~~~

至少报告：

- ROI macro-CPI absolute relative error；
- makespan absolute relative error；
- per-core cycle error 和长期 signed drift；
- branch miss count/rate error；
- aggregate macro/s、model/predict/context/collate/scheduler stage timing；
- GPU 峰值显存；
- 16 条 base、7 条 heldout、heldout 去 Redis 和每 workload 明细。

所有差值使用同 trace、同训练 seed 配对；统计单元是 workload/trace，不把相关的 scheduler
window 当成独立样本。正式结果对 training seed 和 workload 做分层配对 bootstrap，并报告
95% CI。若继续使用当前 7 个 heldout workload，结论必须标注样本量限制；最终泛化结论还应
补充未见 binary family 和独立动态 seed，并审计静态 PC/basic-block 重叠。

在线 Qwen“有增量价值”的预注册 gate 为：A 相对 B 在 heldout ROI CPI 和 makespan 上的配对
误差降低至少 5%，且 95% CI 下界大于 0。判断 `A ≈ B` 时采用非劣界：B 相对 A 的误差增加
95% CI 上界不超过 1.0 个绝对百分点；不能仅凭均值接近宣称等价。B 相对 E 使用相同规则判断
离线语义价值。

### 17.6 结果判定

| 观察结果 | 允许得出的结论 | 主线决策 |
|---|---|---|
| `A > B > E`，且 heldout gate 通过 | 在线 Qwen 和离线 LLM 语义均有独立价值 | 保留当前完整 LLM 主线 |
| `A ≈ B > E` | 离线 LLM 语义重要，在线 1.5B Qwen 不必要 | 改为“离线 Qwen + 在线小 Transformer” |
| `A > B ≈ E` | 优势主要来自在线预训练 Qwen；当前离线 cache 增量不足 | 继续检查 soft-token/anchor 接口，不宣称离线语义有效 |
| `A ≈ B ≈ E` | 当前性能主要来自 numeric/side/cross-mixer/timing head | 删除在线 Qwen，并重新评估是否保留离线语义 |
| A 只在 base 更好，heldout 不通过 | 训练 workload/PC 记忆或模型共适应，未证明语义泛化 | 不扩到 c32，先修复泛化与数据划分 |

“依赖 LLM 分支”的准确表述只用于现有 checkpoint：把 A 已训练好的 LLM 输出清零后性能下降。
只有 A/B/E 从头重训练后仍在未见 binary 的完整 deployment rollout 上满足上述 gate，才允许将
结论升级为“LLM 语义带来可复现的泛化收益”或“在线 Qwen 不可由容量匹配普通 Transformer
替代”。

### 17.7 执行顺序

1. 实现 B/E backbone 抽象和参数量/因果 mask/尾部 mask/单核批处理合同测试；
2. 用固定真实 c8 batch 验证 A/B/E 输出形状、单调时间、梯度和 checkpoint round-trip；
3. 完成 seed-1234 的 B、E 各 30000-step 训练，与现有 A 做第一轮配对 deployment；
4. 若 `A ≈ B`，优先转向“离线语义 + 小 Transformer”，不继续为在线 Qwen 做 c32 优化；
5. 若 A 显著优于 B，再补齐至少 3 seed，并增加从训练开始固定的 paired semantic permutation
   对照，排除静态身份记忆；
6. A/B/E 正式 heldout 结论完成后，再决定是否进入 `sequence_length>1`、多层 core mixer 或
   c32 正式训练，避免多个架构变量同时变化而失去可归因性。

### 17.8 当前实现与一键入口（2026-07-19）

第 17.7 节的第 1 项已经实现并通过单元测试：

- `online_backbone_type=causal_transformer`：每个 core row 独立执行 5 层 causal Transformer；
- B 只接受 `cached_macro_soft_token + real_cache`；
- E 只接受 `learned_null_macro_token + learned_null`，模型输入 allowlist 不含
  `static_semantic`、`static_anchor`、`input_ids` 或 token cache；
- B/E 公共参数使用相同 seed 逐元素同初始化，E 新增的 `NO_SEM` 参数不消耗公共初始化 RNG；
- 普通 backbone 使用独立 optimizer group 和 `lr_online_backbone`；
- checkpoint/run contract 显式记录实验 ID、backbone 类型、语义来源、层数、head、FFN 和参数组；
- deployment loader 支持 E 的无 cache rollout，A/B 原 cache 合同保持严格检查。

正式 seed-1234 B/E 训练的一键命令为：

~~~bash
bash scripts/tmp/run_macro_v29_abe_be_c8_s1_30k_all_in_one.sh
~~~

脚本内部自带 `nohup`，默认依次训练 B、E，各 c8、S=1、30000 step，不重新训练已有 A，也不
默认启动部署验证。启动后会输出 launcher PID、主日志和两个 checkpoint 目录；状态文件为
`logs/tmp/<EXPERIMENT_NAME>/status.txt`。全部 multiprocessing 临时文件固定在项目目录下的
`tmp/mv29abe/<短时间戳>`，避免 PyTorch multiprocessing 追加 listener 后超过 AF_UNIX 的
108-byte 地址上限。

若希望 B/E 训练完成后自动运行两组 23-trace deployment rollout，并与已有 A 汇总比较，可在
启动时显式打开：

~~~bash
RUN_DEPLOYMENT=1 bash scripts/tmp/run_macro_v29_abe_be_c8_s1_30k_all_in_one.sh
~~~

汇总器固定计算 `A vs B`、`B vs E`、`A vs E` 的同 workload 配对差值及 bootstrap 95% CI；
正差值定义为后者误差减前者误差，即正值表示比较名称中的前者更好。

## 18. B2/E2：Qwen3-14B Macro 语义与 TCSim-like UOP 特征融合

### 18.1 实验目标与边界

旧 A/B/E 已经回答了当前 macro-v29 checkpoint 是否依赖既有语义分支，但其结构化输入、
在线 backbone 和推理粒度均未与最新 TCSim v29 完全对齐。新的 B2/E2 不继续扩展旧
macro-level backbone，而是在同一套 TCSim-like per-UOP 模型上只改变 LLM 静态语义输入：

| 变体 | 结构化输入与动态模型 | LLM 输入 | 实验含义 |
|---|---|---|---|
| **B2: TCSim-LLM-Macro** | 完整 TCSim v29 特征、相同 full-QKVR 和输出头 | Qwen3-14B + LoRA 编码的真实 macro 语义 | 测试 LLM 语义在强结构化基线之上的增量 |
| **E2: TCSim-Structured-Only** | 与 B2 完全相同 | 可训练的同尺度 `NO_SEM`，不读取 tokenizer、LLM hidden 或语义 cache | 无 LLM 的 TCSim-like 对照 |

B2/E2 的第一结论只回答“完整 LLM 语义分支是否值得保留”。若 B2 端到端训练 LoRA，
B2 相对 E2 的变化同时包含预训练先验、LoRA 容量和语义输入，不能仅凭这一对实验声称
“收益全部来自 Qwen 预训练语义”。若需要分解该因果来源，后续再增加 frozen/randomized
encoder 对照；它不是第一轮 B2/E2 的前置条件。

### 18.2 LLM 编码粒度

主线固定为 **LLM 只编码 macro，不单独为每个 gem5 UOP 再运行一次 LLM**：

~~~text
同 BB 前 2～4 条真实汇编 + 目标 macro
  -> Qwen3-14B + LoRA
  -> 一个 macro_semantic 向量
  -> 根据 macro-to-UOP 映射广播给该 macro 的全部 UOP
~~~

原因是 Qwen 的预训练语义主要对应真实汇编和代码，而 `IntAlu`、`IntDiv`、`MemRead` 等
gem5 OpClass 是模拟器内部类别。裸 UOP 文本容易退化为昂贵的类别 embedding；若每条 UOP
又重复父 macro 和 basic-block 上下文，则会同时引入冗余 token 和不可归因的双语义分支。

macro prompt 的合同固定为：

- 当前真实 x86-64 macro instruction；
- 同一 basic block 内之前最多 2～4 条静态指令；
- 必要的静态控制流类别；
- 目标平均 48～80 token，硬上限 128 token；
- 不包含 core/workload/seed、绝对 PC、动态地址、时间、cache/coherence oracle 或标签；
- 训练与最终语义 cache 构建使用完全相同的 tokenizer、prompt schema 和 pooling。

若后续证明 macro-only 无法区分复杂指令内部阶段，只允许增加“父 macro + 同 macro 全部
UOP 的单次联合短 prompt”作为消融；不采用 macro 和每个 UOP 分别执行多次 Qwen 的路径。

### 18.3 共同的 TCSim v29 特征合同

B2/E2 都以 `/data00/yinhaolang/TCSim` 当前 v29 合同为准。一个 attention token 是一条
退休 UOP，每核前视窗口 `K=256 UOP`。共同输入为：

| 分支 | 维度/字段数 | 内容 | cache 边界 |
|---|---:|---|---|
| base | 12 categorical | OpClass、寄存器/producer 依赖、mem kind、reuse、stride、macro position、局部历史等 | 静态/同核功能历史 |
| branch | 9 categorical | 分支类型、真实方向/后继距离、历史、PC/target reuse、predictor alias、RAS depth | 功能可见，不含 mispredict oracle |
| resource | 5 categorical | paddr 有效性、DRAM row reuse、L1/L2/LLC set pressure | 固定 trace/chunk/uarch 可缓存 |
| dynamic | 8 categorical | 跨核 line/set/bank/channel fanout、row support/conflict | 每个 scheduler context 重算 |
| chunk summary | 38 continuous | 指令构成、依赖、局部性、分支和资源统计 | 每 chunk |
| relation | 22 continuous | shared line、跨核读写、资源竞争与 active-core 关系 | 每个 scheduler context 重算 |
| state | 5 continuous | head age、距上次 commit、ROI age、cold start、active-core fraction | 每步重算 |
| uarch | 29 continuous | pipeline/cache/TLB/DRAM 的目标配置 | 每 uarch |

第一版对齐 TCSim `v29_100m` 的 `d_static=768`、`d_dyn=960`、15 heads、8 层
full local-QKV/cross-core-R attention。B2 不扩大 attention 宽度，E2 不删减任何
TCSim-like 特征。raw resource equality key 只用于构造 permutation-invariant relation，
不能作为 ID embedding 送进模型。

### 18.4 Macro 语义到 UOP token 的融合

设第 `i` 条 UOP 属于 macro `m(i)`。Qwen 先得到一个 macro 语义向量：

~~~text
s_m = Qwen3LoRA(macro_prompt_m)                     # [d_llm]
h_i = StaticTokenEncoderV29(per_uop_fields_i)       # [768]
p_i = SemanticProjection(RMSNorm(s_{m(i)}))         # [768]
g_i = sigmoid(SemanticGate(LN(h_i), p_i))           # [768]
h_static_i = LN(h_i + g_i * p_i)                    # [768]
~~~

随后完全复用 TCSim 动态融合和 full-QKVR：

~~~text
x_i = W_static(h_static_i)
    + W_dynamic(dynamic_uop_fields_i)
    + W_side(summary, relation, state, uarch)

x_i -> 8 x local-QKV / cross-core-R -> monotonic commit-time + branch heads
~~~

门控依赖当前 UOP 的结构状态，因此同一个 macro 产生的多个 UOP 虽然共享 `s_m`，仍会因
OpClass、macro position、依赖和资源字段不同而获得不同的最终 token state。语义必须在
full-QKVR 之前注入，使其能够影响同核 UOP 关系和跨核交互；禁止只在 chunk pooling 或
输出 head 后拼接一个 macro summary。

首版 `SemanticGate` 的最终 bias 建议初始化为 `-2`，即初始门约为 `0.12`。这样模型从接近
TCSim 稳定路径开始，但语义投影和 LoRA 仍能获得非零梯度。B2/E2 使用完全相同的
`SemanticProjection`、`SemanticGate`、LayerNorm 和初始化；区别仅是输入为真实 `s_m`
还是 `NO_SEM`。

### 18.5 数据、索引与 cache 合同

在现有 per-UOP packed 数据旁新增：

~~~text
semantic_id[N, K]: int32
~~~

`semantic_id` 只用于把 UOP 映射到静态 macro 语义表：

~~~text
(binary_hash, module_relative_macro_pc, prompt_schema,
 tokenizer_hash, base_model_revision, lora_checkpoint_hash)
  -> semantic cache row
~~~

它不是模型 categorical feature，不能让网络直接看到 PC、hash 或 cache row ID。同一
macro 的所有 UOP 共享 semantic row，但仍保留各自的 TCSim 结构字段。数据构建必须验证：

- 每条有效 UOP 恰好映射到一个经过 bytes/反汇编验证的 macro；
- padding UOP 使用专用无效 ID，且始终被 `valid_uop_mask` 屏蔽；
- B2 cache miss、版本不一致或 mapping 越界时 fail closed；
- E2 loader 不打开 semantic cache，也不把任何 LLM provenance 放入模型输入；
- train/validation/deployment 使用同一映射算法和 feature schema。

LoRA 训练期间 hidden state 会随每步参数变化，不能使用 detached semantic cache。可永久
缓存 tokenizer 输出，并在每个 batch 内按 semantic key 去重：

~~~text
unique semantic IDs -> batched independent short prompts -> Qwen3-14B + LoRA
                    -> scatter back to all dynamic UOP occurrences
~~~

各 prompt 是独立短序列，attention 开销为 `sum(L_i^2)`，不能把多核全部 macro 拼成一条
长序列。训练完成后冻结 Qwen 和 LoRA，重新构建最终 hidden-state cache；部署侧只执行
semantic lookup、门控融合和 TCSim模型，不再运行 Qwen。

### 18.6 B2/E2 公平训练合同

第一轮 B2/E2 必须满足：

- 使用相同的 c1/c4/c8/c16/c32 训练划分、相同 validation 和 heldout binary；
- 使用同一套 TCSim v29 packed functional features、`K=256 UOP` 和 scheduler；
- 公共 TCSim 参数逐张量同初始化，训练 seed、样本顺序、optimizer、loss、step 数一致；
- 都从头训练任务模型，不从 TCSim checkpoint 或旧 LLMSim checkpoint 蒸馏或复制 hidden；
- B2 保留全部 TCSim 字段，第一轮不得删除 OpClass、dependency、branch 或 resource 特征；
- E2 使用同尺度可训练 `NO_SEM`，保留相同 semantic bridge 和门控参数；
- checkpoint 记录 base model revision、tokenizer/prompt/pooling、LoRA 配置、feature contract、
  semantic mapping/cache hash、数据顺序 hash 和全部参数量；
- 分别报告相同 optimizer step 与相同 wall-clock 预算，不能只比较其中一个口径。

Qwen3-14B + LoRA 只存在于 B2，因此两组总训练参数量和计算量不会严格相等。这正是“完整
LLM 分支是否值得”的工程处理差异，必须单独报告；给 E2 添加不参与 forward 的 dummy 参数
不能构成有效的容量匹配。

### 18.7 评测与结论口径

训练 loss 和 teacher-forced validation 只用于健康检查。主结论来自与最新 TCSim 对齐的
label-free closed-loop deployment，至少覆盖 c4/c8/c16/c32，并保持 B2/E2 完全相同的
窗口、scheduler、stride、停止条件和推理优化。必须报告：

- 逐 trace ROI micro-CPI absolute relative error；
- per-core CPI MAPE、makespan absolute relative error和长期 signed drift；
- per-UOP commit-time、prefix/progress 与 branch count/rate 指标；
- base、heldout、heldout-no-Redis 和每 workload 明细；
- aggregate UOP/s、useful UOP/forward、context/model/cache 各阶段耗时；
- 训练峰值显存、部署峰值显存和 semantic cache 命中率。

允许的第一轮结论：

| 结果 | 结论 |
|---|---|
| B2 在 heldout binary 和多个 core count 稳定优于 E2 | LLM macro 分支在完整 TCSim 特征之外提供增量，值得继续验证多 seed |
| B2 只在 train/base 更好 | 可能是静态 PC/basic-block 记忆，不能声称语义泛化 |
| B2 与 E2 接近 | 当前 TCSim 特征已经覆盖 LLM 编码能提供的有效信息，优先保留 E2 |
| B2 更差 | 检查 prompt、macro-to-UOP mapping、门控和 LoRA 优化；不能通过删除 E2 特征挽救比较 |

只有 B2 显著优于 E2 后，才进入第二阶段“删除部分 TCSim 静态字段”的替换消融，验证 LLM
能否取代 OpClass 或其他手工特征。第一轮 B2/E2 的目标是验证**增量价值**，不是同时验证
增量、替代和模型规模三个问题。

### 18.8 当前下一步

第 18 节尚未落地。实施顺序固定为：

1. 为现有 TCSim v29 packed 数据构建只用于 lookup 的 `semantic_id` sidecar，并完成覆盖率、
   padding、split 和 provenance 审计；
2. 实现共同的 `SemanticProjection + SemanticGate`，用 real/null 输入验证 B2/E2 公共参数
   同初始化和 E2 无 semantic-cache 访问；
3. 接入 Qwen3-14B tokenizer、macro prompt 和 batch 内 unique-key LoRA 前向，先做真实
   c8 小 batch 的输出、梯度、显存和 checkpoint round-trip smoke；
4. 冻结 smoke checkpoint 重建语义 cache，验证训练态实时编码与冻结后 cache 重算在注册
   容差内一致；
5. 通过上述门禁后再生成正式 B2/E2 一键训练脚本；当前不存在可宣称正式可用的 B2/E2
   训练命令或 checkpoint。
