# pre-v19 方案设计：local core backbone + cross-core adapter

日期：2026-07-03

状态：实施稿。v19a 先解决 v18 暴露的表示问题：tail query 能看全局，但每个 `<QUERY_Ci>` 的 hidden 过于相似；online 里再被 per-core 预测时钟闭环放大。v19a 不重采 gem5，不改 label 口径，先把训练输入从“一条多核长序列”改为“每核一条局部短序列”，再用小型 cross-core adapter 学跨核影响。

## 1. 背景结论

v18 hidden 诊断：

```text
query_pair_cos_mean      = 0.9965
post_adapter_pair_cos    = 0.9185
post_adapter_eff_rank    = 1.66
```

这说明 tail query 原始表示几乎是同一个全局窗口向量；`local/side/tstart` 后处理能拉开一点差异，但核间表示仍低维。online pred-cut 里 planner 在自己的预测时间轴上对齐，真实时间已经漂移：

```text
planner_tail_skew        ~= 0.4 cycles
true start/end offset    ~= 94k cycles
```

因此 v19a 的目标不是继续增强 tail query，而是改变 per-core representation 的读出方式。

## 2. v19a 架构

### 2.1 Local core backbone encoder

旧路径：

```text
[C0 segment][C1 segment]...[TRACE_END][QUERY_C0..QUERY_CN]
-> Qwen backbone
-> hidden(QUERY_Ci)
```

v19a 路径：

```text
for each core i:
  [SYS/cfg/TRACE/global-lite][Ci segment][LOCAL_Ci]
  -> same Qwen backbone
  -> h_local_i = hidden(LOCAL_Ci)

h_local: [B, C, D]
```

实现上不是逐核循环，而是把 `B*C` 条短序列一次性 batch 到单卡/多卡：

```text
local_input_ids:      [B, C, Lc]
flatten ->            [B*C_active, Lc]
backbone forward
gather LOCAL hidden
reshape ->            [B, C, D]
```

这样 `LOCAL_Ci` 天然只能看到本核 segment 和短 prefix，不需要 32k dense attention mask。

### 2.2 Cross-core adapter

局部表示之后再做跨核交互：

```text
z_i = h_local_i + local_proj(h_local_i)
z_i += side_proj(side_feats_i)
z_i += tstart_proj(t_start_i)   # v19a 可开关，默认沿用训练脚本 --use-tstart

h_ctx = CoreAdapter(z, core_mask)
```

adapter 是 mask-aware self-attention over cores，只跑 `C` 个 token，成本低，参数不绑定核心数。

### 2.3 Residual CPI head

v19a 继续使用已有 `cpi_head_mode=delta`：

```text
base = global_head(masked_mean(h_ctx))
delta_i = delta_head(h_ctx_i) - mean(delta)
log_cpi_i = base + delta_i
```

含义：

- `base` 管窗口整体 CPI scale。
- `delta_i` 管每核相对快慢，zero-mean，避免每核独立 scale 乱漂。
- branch/cache head 暂时沿用 split head 直接从 `h_ctx_i` 输出。

## 3. 数据与 cache

不需要重新采 gem5。v19a 只从现有 `windows.jsonl` 派生新的 local tensor cache。

已有字段足够：

```text
tokens
is_uop
uop_fields
<Ci_BEGIN> ... <LOCAL_Ci> ... <Ci_END>
side_feats
label
uops_per_core
```

新增 cache 内容：

```text
local_input_ids        # 每个 core 一条短序列
local_attention_mask
local_is_uop
local_uop_fields
local_query_pos        # LOCAL_Ci 在短序列中的位置
```

cache 路径使用独立后缀，避免和 v17/v18 旧全局 cache 混用：

```text
windows.maxlen32768.local_tensor_cache/
```

## 4. v19a 训练路线

先只跑 TQ/oracle windows：

```text
data/windows_v17_bc_split_heads_nophase_all/windows.jsonl
-> windows.maxlen32768.local_tensor_cache
-> train_lora.py --model-input-mode local_core
```

推荐默认：

```text
cpi_head_mode=delta
core_adapter_layers=2
lambda_delta=2.0
lambda_rank=0.15
lambda_spread=0.10
lambda_slowest=0.05
lambda_fastest=0.05
```

v19a 先不承诺 online 改善；它的验收目标是表示和训练式快慢核：

```text
post_hidden_pair_cos_mean < v18 的 0.918
effective_rank > v18 的 1.66
pred_cpi_cv 接近 label_cpi_cv
slowest/fastest hit 提升
ads proxy c8/c16 pred_vs_label 不退化
```

### 4.1 构建 local cache

全量训练 cache：

```bash
/data00/yinhaolang/infer/.venv/bin/python scripts/prepare_dataset_cache.py \
  --data data/windows_v17_bc_split_heads_nophase_all/windows.jsonl \
  --max-len 32768 \
  --format tensor \
  --input-mode local_core \
  --cache-out data/windows_v17_bc_split_heads_nophase_all/windows.maxlen32768.local_tensor_cache \
  --jobs 8 \
  --lines-per-shard 512
```

c08 smoke cache：

```bash
/data00/yinhaolang/infer/.venv/bin/python scripts/prepare_dataset_cache.py \
  --data data/windows_v17_bc_split_heads_nophase_c08/windows.jsonl \
  --max-len 32768 \
  --format tensor \
  --input-mode local_core \
  --cache-out tmp/v19_c08_smoke.local_tensor_cache \
  --jobs 2 \
  --lines-per-shard 64
```

### 4.2 训练命令

全量 v19a：

```bash
./scripts/run_v19_local_core_qwen3_0p6b.sh
```

单卡 smoke：

```bash
CUDA_VISIBLE_DEVICES=0 \
NPROC=1 GPUS=0 STEPS=1 EVAL_EVERY=1 SAVE_EVERY=0 EVAL_BATCHES=1 \
DATA=data/windows_v17_bc_split_heads_nophase_c08/windows.jsonl \
CACHE_PATH=tmp/v19_c08_smoke.local_tensor_cache \
OUT=ckpt/smoke_v19_local_core_c08 \
./scripts/run_v19_local_core_qwen3_0p6b.sh
```

## 5. 后续阶段

v19b：

- 用 online pred planner 切出 rollout windows。
- 从 raw labels 聚合这些 rollout windows 的标签。
- 混合训练 `TQ oracle + online rollout`。

v19c：

- online planner 从 per-core independent clock 改为 `global_cycle + bounded_relative_skew`。
- bootstrap 前几窗 equal-uop，不更新 per-core skew。
- planner count 与 equal-count 混合，逐步打开。

## 6. 主要风险

- 跨核交互不再经过 full-seq Qwen 大 attention，而是交给小 adapter；若 adapter 不够，可加 global-tail residual path。
- 每核 local 序列重复 prefix/cfg，吞吐需 microbenchmark。
- v19a checkpoint 不能直接走旧 online eval；部署侧需要补 local `encode_sample()` 路径。
