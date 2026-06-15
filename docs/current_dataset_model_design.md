# LLMSim 当前数据集与模型设计

本文档记录当前已落地并完成 8 卡训练的 LLMSim 实现，而不是早期方案草案。对应代码分支为 `LLMSim`，最新可用 checkpoint 为 `ckpt/train8_w512_embfix_v2`。

## 1. 目标

LLMSim 的目标是用 functional trace 直接预测窗口级每核 PMU，再聚合得到 workload 级性能指标。

```text
functional trace window -> Qwen3-0.6B-Base + LoRA -> [n_core, K_pmu]
```

输入只使用 functional/架构态可见字段；微架构状态、commit tick、latency、cache hit/miss oracle 等不作为模型输入，只作为训练标签的来源。

当前主评估目标是：

```text
pred_cycles = pred_cpi * instr_retired
global_cpi = sum(pred_cycles) / sum(instr_retired)
```

并与窗口标签聚合值以及 gem5 `stats.txt` 的 `sum(numCycles)/sum(commitStats0.numInsts)` 对比。

## 2. 当前数据集

### 2.1 数据来源

当前训练集由 8 个 8-core workload 组成：

| Workload | Raw 来源 |
|---|---|
| `W_branch_storm` | `data/raw_fix3_8c_500k/W_branch_storm` |
| `W_chase_dram` | `data/raw_8w_8c_500k/W_chase_dram` |
| `W_compute_int` | `data/raw_8w_8c_500k/W_compute_int` |
| `W_false_sharing` | `data/raw_8w_8c_500k/W_false_sharing` |
| `W_indirect` | `data/raw_8w_8c_500k/W_indirect` |
| `W_int_div` | `data/raw_fix3_8c_500k/W_int_div` |
| `W_phased_mix` | `data/raw_fix3_8c_500k/W_phased_mix` |
| `W_stream` | `data/raw_8w_8c_500k/W_stream` |

`raw_fix3_8c_500k` 是修复过 ROI 工作量/Makefile target 后重新采集的三类 workload；其余 workload 来自首批 8-core 500k 采集。

### 2.2 窗口构建

窗口构建入口：

```text
scripts/build_windows_train8.sh
```

当前配置：

```text
WINDOW=512
STRIDE=256
MAXLEN=32768
JOBS=8
```

输出：

```text
data/windows_train8_w512/windows.jsonl
data/windows_train8_w512/windows.maxlen32768.ids_cache/
```

样本数：

| Workload | windows |
|---|---:|
| `W_branch_storm` | 2968 |
| `W_chase_dram` | 2304 |
| `W_compute_int` | 2147 |
| `W_false_sharing` | 2183 |
| `W_indirect` | 2234 |
| `W_int_div` | 3251 |
| `W_phased_mix` | 21149 |
| `W_stream` | 2225 |
| **Total** | **38461** |

训练时直接读取落盘的分片 ids cache，不在训练进程内生成 tokenizer cache，避免 rank 间等待和 NCCL timeout。

### 2.3 每条样本结构

`windows.jsonl` 每行是一个窗口样本，关键字段包括：

```text
id
workload
cfg_hash
n_core
w_ops
tokens
core_split
label
label_keys
denoms
instr_retired
```

其中：

- `tokens` 是 functional trace 编码后的 token 序列。
- `label` 是 `[n_core, K]` 的窗口级 PMU 标签。
- `instr_retired` 是每核窗口内 macro 指令数，来自 functional trace，可用于把预测 CPI 反算为 cycles。
- `denoms` 保存部分比率类 PMU 的分母，例如 branch/load/store/mem/fetch 计数。

## 3. Tokenizer 设计

实现文件：

```text
model/tokenizer.py
```

每条 uop 固定编码为 6 个 token：

| Slot | Token 族 | 含义 |
|---|---|---|
| 1 | `<OP_i>` | 指令类别，例如 load/store/branch/int/fp/simd/atomic/fence |
| 2 | `<RG_i>` | `(n_src, n_dst, producer_classes)` 的寄存器依赖 hash 桶 |
| 3 | `<MK_i>` | memory kind：none/load/store/atomic |
| 4 | `<VL_i>` | `vaddr >> 6` 的 cacheline hash 桶 |
| 5 | `<VP_i>` | `vaddr >> 12` 的 page hash 桶 |
| 6 | `<BR_i>` | 静态分支类型组合：cond/indirect/call/return |

控制和配置 token：

```text
<SYS>, <TRACE>, <TRACE_END>, <SYNC>, <PAD_UOP>
<C{i}_BEGIN>, <C{i}_END>, <QUERY_C{i}>
<CFG_L1D_i>, <CFG_L2_i>, <CFG_L3_i>, <CFG_CLK_i>
```

当前新增 token 数为 1470，注入 HuggingFace tokenizer 的 `additional_special_tokens`，随后 `resize_token_embeddings`。

## 4. 标签和目标空间

PMU key 定义在：

```text
model/regression_head.py
```

当前 10 个预测目标：

```text
cpi
mpki_br
mr_l1d_ld
mr_l1d_st
mr_l1i
mr_llc
dtlb_miss
itlb_miss
inv_recv
mshr_avg
```

各目标的回归空间：

| Key | 空间 | Target transform | Pred inverse |
|---|---|---|---|
| `cpi` | `logratio` | `log(y)` | `exp(pred)` |
| `mpki_br` | `rat01` | clamp 到 `[0,1]` | clamp 到 `[0,1]` |
| `mr_l1d_ld` | `rat01` | clamp 到 `[0,1]` | clamp 到 `[0,1]` |
| `mr_l1d_st` | `rat01` | clamp 到 `[0,1]` | clamp 到 `[0,1]` |
| `mr_l1i` | `rat01` | clamp 到 `[0,1]` | clamp 到 `[0,1]` |
| `mr_llc` | `rat01` | clamp 到 `[0,1]` | clamp 到 `[0,1]` |
| `dtlb_miss` | `logcount` | `log1p(y)` | `expm1(pred)` |
| `itlb_miss` | `logcount` | `log1p(y)` | `expm1(pred)` |
| `inv_recv` | `logcount` | `log1p(y)` | `expm1(pred)` |
| `mshr_avg` | `direct` | `y` | `pred` |

cycles 不直接作为模型输出；它由 CPI 和 macro 指令数反算：

```text
cycles_pred = cpi_pred * instr_retired
```

## 5. 模型设计

实现文件：

```text
model/llm_wrapper.py
model/regression_head.py
```

### 5.1 Backbone

当前 backbone：

```text
Qwen/Qwen3-0.6B-Base
```

加载配置：

```text
torch_dtype = bf16
attn_implementation = sdpa
use_cache = False
gradient_checkpointing = enabled, use_reentrant=False
```

`sdpa` 和 gradient checkpointing 是长序列训练的必要配置。当前序列长度上限为 32768，普通 eager attention 和不做 checkpointing 会触发激活 OOM。

### 5.2 LoRA

LoRA 配置：

```text
r = 32
alpha = 64
dropout = 0.05
target_modules = q_proj, k_proj, v_proj, o_proj
bias = none
task_type = FEATURE_EXTRACTION
```

主干权重冻结，只训练 LoRA、回归头、loss 的 `log_var`，以及新增 token embedding 行。

### 5.3 新 token embedding

新增 token 位于词表末尾连续区间：

```text
new_token_start = 151669
n_new_tokens = 1470
embedding_dim = 1024
```

embedding 参数本身是一整张矩阵，不能只对部分行设置 `requires_grad`。当前实现使用 backward hook：

```text
grad[:new_token_start] = 0
```

这样优化器虽然持有完整 embedding 参数，但只有新增 token 行真正更新；原始 Qwen token embedding 不被修改。

### 5.4 Per-core query head

输入 token 序列末尾追加：

```text
<QUERY_C0> ... <QUERY_C{n-1}>
```

forward 时从 `last_hidden_state` 按 `query_pos` gather 每核 query hidden，再经过共享的 `PMURegressionHead`：

```text
hidden[QUERY_Ci] -> LayerNorm -> Linear -> GELU -> Linear -> [K]
```

输出形状：

```text
[batch, n_core, 10]
```

回归头对所有核共享参数，因此支持可变 core 数。

## 6. Loss 设计

实现文件：

```text
train/loss.py
```

当前损失为：

```text
total_loss = Σ_k [ exp(-σ_k) * Huber(pred_k, target_k) + σ_k ]
             + 0.1 * L_inv
```

其中：

- `k` 是 10 个 PMU key。
- `σ_k = log_var[k]` 是可学习 uncertainty 参数。
- `Huber delta = 1.0`。
- `core_mask` 用于只统计有效 core。

`L_inv` 是 CPI 物理约束：

```text
cpi = exp(pred_log_cpi)
L_inv = mean(relu(0.25 - cpi))
```

它惩罚 `CPI < 0.25`，等价于 `IPC > 4` 的不合理预测。日志中的 `L_inv=0` 表示没有违反该约束。

由于 uncertainty weighting 中有 `+ σ_k`，总 loss 可以为负；判断效果时应看分项 loss、验证集趋势，以及反解到物理量纲后的 CPI/cycles 误差。

## 7. 训练流程

训练入口：

```text
train/train_lora.py
scripts/launch_ddp8.sh
```

当前 8 卡训练配置：

```text
DATA=data/windows_train8_w512/windows.jsonl
OUT=ckpt/train8_w512_embfix_v2
MAXLEN=32768
BS=1
GRAD_ACCUM=2
STEPS=2000
EVAL_EVERY=100
EVAL_BATCHES=40
LOG_EVERY=20
```

等效全局 batch：

```text
bs * world_size * grad_accum = 1 * 8 * 2 = 16
```

DDP 关键实现：

- `TrainModule` 把 model 和 loss 合为一个 module 后再交给 DDP，确保 LoRA、head、`log_var` 都参与同步。
- 训练和验证都走 DDP wrapper。
- 验证使用 `DistributedSampler`，所有 rank 协同验证后 `all_reduce` 求平均。
- 梯度累积期间，非最后一个 micro step 使用 `no_sync()` 跳过梯度 allreduce。
- `find_unused_parameters=False`，避免额外反向图遍历。

## 8. 最新 checkpoint

最新可用 checkpoint：

```text
ckpt/train8_w512_embfix_v2/
```

内容：

```text
head_best.pt
lora_best/adapter_config.json
lora_best/adapter_model.safetensors
lora_best/README.md
```

大小：

```text
39M  ckpt/train8_w512_embfix_v2
3.4M head_best.pt
36M  lora_best/adapter_model.safetensors
```

`head_best.pt` 包含：

```text
head
log_var
new_token_start = 151669
n_new_tokens = 1470
new_token_embedding shape = [1470, 1024]
step = 2000
val_loss = -18.6613
```

`new_token_embedding` 必须和 LoRA/head 一起保存和加载。旧 checkpoint `ckpt/train8_w512_embfix` 缺失该部分，不能用于有效推理验证。

## 9. 推理和验证

通用 per-window 指标脚本：

```text
eval/eval.py
```

全局 CPI 验证脚本：

```text
eval/eval_cycles.py
```

推荐验证命令：

```bash
cd /data00/yinhaolang/LLMSim
CUDA_VISIBLE_DEVICES=0 HF_HUB_OFFLINE=1 /data00/yinhaolang/infer/.venv/bin/python eval/eval_cycles.py \
  --data data/windows_train8_w512/windows.jsonl \
  --ckpt ckpt/train8_w512_embfix_v2 \
  --max-len 32768 --bs 1 --max-windows 80 \
  --workload W_compute_int:data/raw_8w_8c_500k/W_compute_int/stats.txt \
  --workload W_chase_dram:data/raw_8w_8c_500k/W_chase_dram/stats.txt \
  --workload W_branch_storm:data/raw_fix3_8c_500k/W_branch_storm/stats.txt
```

脚本会报告：

- `pred`：模型预测的 `Σ(cpi_pred * macro) / Σmacro`。
- `label`：窗口标签真值的 `Σ(cpi_label * macro) / Σmacro`。
- `gem5`：`stats.txt` 全程 `ΣnumCycles / ΣnumInsts`。
- `pred vs label`：纯模型预测误差。
- `pred vs gem5`：端到端误差，包含窗口采样和全程 stats 之间的偏差。
- `label vs gem5`：窗口采样本身相对 gem5 全程的偏差。

## 10. 当前限制

当前版本仍有明确边界：

- 只训练了一个 uarch config，泛化到其他配置尚未验证。
- 8 个 workload 中 `W_phased_mix` 样本占比较高，数据分布不均衡。
- 输入目前未显式编码跨核共享 cacheline 的程序级特征，因此 `inv_recv` 等 coherence 目标可能仍主要学 workload 均值。
- 没有使用 sample packing，长序列 padding 和单卡推理成本较高。
- 当前评估应优先看反解后的 CPI/cycles 误差，不能只看训练 loss。

## 11. 关键文件索引

```text
data/build_windows.py              raw trace -> windows.jsonl
scripts/build_windows_train8.sh    8 workload windows + cache 构建
scripts/prepare_dataset_cache.py   并行分片 tokenizer cache
train/dataset.py                   sharded ids cache dataset
model/tokenizer.py                 6-token/uop functional tokenizer
model/llm_wrapper.py               Qwen3 + LoRA + per-core query wrapper
model/regression_head.py           PMU head 和 key/space 定义
train/loss.py                      PMU loss
train/train_lora.py                DDP 训练入口
scripts/launch_ddp8.sh             8 卡启动脚本
eval/eval.py                       per-window PMU eval
eval/eval_cycles.py                global CPI/cycles eval
```
