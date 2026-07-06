# TSim v26 Query-Centric KVQR 干净方案

状态：最终设计稿。本文只描述当前收敛后的首版方案：保留 `side_feats` 和
`global_feats`，不引入 `planner_feats`、`local_encoder`、core memory 压缩、LLM
special tokens 或默认绝对 core-id embedding。

`docs/2026.7.6LLMSim.md` 只作为 TAO/多核建模参考，不直接照搬。TSim 的真实任务是
多核 dynamic window 级 PMU 预测，不是逐指令 fetch/execute latency 预测。

## 1. 目标

训练一个从零开始、全参数可训练的 Transformer，用多核 functional trace window
预测每核 PMU：

```text
输入: 多核 UOP 字段 + side/global functional summary
输出: 每核 cpi_uop、branch_miss、cache/TLB miss 等 PMU
```

首版要验证三件事：

1. 结构化 core 轴输入是否优于旧的伪文本 token 序列。
2. query-centric KVQR 是否能有效建模跨核干扰。
3. side/global functional summaries 是否能稳定窗口级 count/rate 预测。

首版明确不做：

- 不做逐指令 latency/fetch/execute 标签。
- 不输入 `path_class`、`coh_oracle`、`mshr`、`dtlb_hit`、tick/latency/miss label 等
  微架构 oracle。
- 不使用 Qwen/HF tokenizer、`<C0_BEGIN>`、`<QUERY_C0>`、`<LOCAL_C0>` 等 LLM
  兼容 special tokens。
- 不使用 `planner_feats` 或 planner replay。`true_t_start_rel/true_t_end_rel`
  只保留在 meta 或诊断中，不作为模型输入。
- 不默认加入 `local_encoder`、core memory 压缩、system query、绝对 core-id
  embedding。

## 2. 当前代码事实

当前 TSim v25a 是 8 层、320 宽、约 12M 参数的自训 TinyTransformer，仍沿用
v22/v25a 的数据与 head：

- 数据 cache：`data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache`
- manifest：`max_len=32768`，`max_cores=32`，`side_feat_dim=34`
- 生成侧 PMU：`cpi_uop, branch_miss, l1d_ld_miss, l1d_st_miss,
  l2_ld_miss, l2_st_miss, llc_miss, dtlb_miss`
- 模型侧 PMU：当前只训练 7 维，遗漏 `dtlb_miss`
- 旧输入：`<UOP>` token + 6 个 UOP fields + `<C{i}_BEGIN>/<QUERY_C{i}>` 等
  special tokens

v26a 第一项工程应先统一 PMU schema，至少补回生成侧已有的 `dtlb_miss`。

## 3. 输入张量

一个 sample 是一个多核 dynamic window。输入保持结构化 core 轴：

```text
uop_fields:     [B, C, L, F]
uop_mask:       [B, C, L]
core_mask:      [B, C]
side_feats:     [B, C, S]
global_feats:   [B, G]
uops_per_core:  [B, C]
denoms:         [B, C, Dn]
```

含义：

```text
B  = batch size
C  = max cores, default 32
L  = per-core UOP length after padding
F  = UOP discrete field count
S  = per-core functional summary dim
G  = window/global/config functional summary dim
Dn = opportunity denominator count
```

核心划分由 `C` 轴和 mask 自然给出，不需要 `segment_id`。若后续为了内核实现把
`[B, C, L, D]` flatten 成 `[B, C*L, D]`，segment metadata 只作为实现辅助，
不是模型语义组件。

默认不加入绝对 `core_id` embedding。同构 core 下，`C0/C1/...` 的绝对编号通常无
物理意义，容易破坏 core permutation invariance。只有确认物理 core id、cache slice、
NUMA 或拓扑位置会影响标签时，才加入 topology feature。

## 4. UOP 字段和 Embedding

首版使用 10 个 functional-only UOP 字段：

| 字段 | 来源 | 作用 |
| --- | --- | --- |
| `opclass` | `op_class` / fallback flags | 指令类型 |
| `reg_bucket` | `n_src/n_dst/producer_classes` hash | 架构依赖结构 |
| `memkind` | load/store/atomic/fence/none | 访存机会 |
| `rd_bucket` | same-core bounded reuse distance | cache 局部性 |
| `stride_bucket` | same-core cacheline stride | stream/random 访问 |
| `branch_bucket` | taken/cond/indirect/target-delta | 分支类型 |
| `pc_bucket` | macro/micro PC hash 和低位 bucket | branch/I-cache/循环结构 |
| `branch_hist_bucket` | per-core branch history folded hash | branch miss 预测 |
| `xcore_mem_bucket` | line 共享/多写者 functional bucket | 跨核共享和 false sharing proxy |
| `macro_pos_bucket` | macro head/last microop/index bucket | macro 展开和 fetch group 机会 |

每个字段独立 embedding，然后 concat + MLP：

```text
uop_emb[b,c,t] = MLP(concat(
  Emb_op(opclass),
  Emb_reg(reg_bucket),
  Emb_mem(memkind),
  Emb_rd(rd_bucket),
  Emb_stride(stride_bucket),
  Emb_branch(branch_bucket),
  Emb_pc(pc_bucket),
  Emb_branch_hist(branch_hist_bucket),
  Emb_xcore_mem(xcore_mem_bucket),
  Emb_macro_pos(macro_pos_bucket)
))
```

得到：

```text
E: [B, C, L, D]
```

再加每核内部位置编码：

```text
E = E + role_emb(uop) + local_position_encoding[position_in_core]
```

`position_in_core` 每个 core 内从 0 重置，避免旧全局拼接位置把 core 排列顺序误当成
物理距离。

## 5. Side 和 Global Features

`side_feats/global_feats` 是 functional trace 可直接计算的窗口级统计量，不是 oracle。
它们的作用是把确定的机会数和压力统计显式提供给窗口级 PMU head，避免模型浪费容量
从 UOP 序列里重新数 branch/load/store/mem 机会。

### 5.1 side_feats

首版保留最小 per-core side 集：

```text
log1p_uops_core
log1p_instr_retired
log1p_branch_count
log1p_cond_branch_count
log1p_indirect_branch_count
log1p_load_count
log1p_store_count
log1p_atomic_count
log1p_mem_ops
log1p_distinct_data_lines_core
log1p_distinct_data_pages_core
core_shared_store_rate
core_shared_load_rate
core_multi_writer_store_rate
core_random_load_density
```

### 5.2 global_feats

首版保留最小 global/config 集：

```text
log1p_active_cores
log1p_uops_window_total
log1p_global_distinct_data_lines
log1p_global_distinct_data_pages
shared_store_rate
multi_writer_line_frac
pairwise_writer_pressure
store_owner_switch_rate
inval_fanout_proxy_mean
aggregate_load_density
aggregate_mem_density
global_large_stride_rate
random_access_pressure
cfg/cache/clock buckets or numeric cfg features
```

连续特征需要基于训练集做 mean/std 或 robust scaling，并把统计量写入 cache manifest。
比例类保持 `[0,1]`，count 类使用 `log1p` 后标准化。

投影方式：

```text
side_vec[b,c] = Linear(norm(side_feats[b,c]))
global_vec[b] = Linear(norm(global_feats[b]))
```

首版不构造 side token / side memory。若后续实测 query 只在 head 前看到 side 信息
不足，再作为 ablation 引入 side memory。

## 6. Query-Centric KVQR 模型

每个 core 一个 learned prediction query：

```text
q[b,c] = learned_query_base
       + role_emb(query)
       + side_proj(side_feats[b,c])
       + global_proj(global_feats[b])
```

本核上下文：

```text
local_ctx[b,c] = Attn(
  Q = q[b,c] W_q,
  K = E[b,c,:,:] W_k,
  V = E[b,c,:,:] W_v,
  mask = uop_mask[b,c]
)
```

跨核上下文：

```text
cross_ctx[b,c] = Attn(
  Q = q[b,c] W_r,
  K = E[b,j!=c,:,:] W_k,
  V = E[b,j!=c,:,:] W_v,
  mask = core_mask[b,j!=c] & uop_mask[b,j!=c]
)
```

融合和输出：

```text
h[b,c] = Fuse(q[b,c], local_ctx[b,c], cross_ctx[b,c], side_vec[b,c], global_vec[b])
pred[b,c] = PMUHead(h[b,c])
```

`R` 是跨核交互的核心。普通 `Q` 负责本核 UOP；独立 `R` 专门查询其他核 UOP 的
`K/V`，避免把本核建模和跨核干扰混在一个 query 子空间里。

首版不使用：

```text
local_encoder
core_memory / summary-mediated compression
planner_state_proj
absolute core_id embedding
system query
```

这些都保留为后续 ablation，而不是首版默认组件。

### 6.1 跨核注意力开销

首版使用 query-to-all-UOP cross attention，不是 full UOP-to-UOP cross attention。

```text
query-to-all-UOP:
  每核 1 个 query 看其他核所有 UOP
  score count = C * (C - 1) * L
  complexity ~= O(C * C * L * D)

full UOP-to-UOP:
  每核每条 UOP 看其他核每条 UOP
  score count = C * (C - 1) * L * L
  complexity ~= O(C * C * L * L * D)
```

例子：

```text
C=32, L=256:
query-to-all-UOP scores = 32 * 31 * 256       = 254k
full UOP-to-UOP scores  = 32 * 31 * 256 * 256 = 65M
```

因此首版 query-centric KVQR 的跨核注意力通常可承担。只有 C/L 更大或实测
latency/显存不可接受时，才考虑 core memory 压缩。

## 7. 标签

标签是窗口级 per-core PMU：

```text
label:     [B, C, K]
core_mask: [B, C]
```

v26a 先覆盖当前生成侧已有的 8 维，避免一次混入太多变量：

```text
PMU_KEYS_V26A = [
  cpi_uop,
  branch_miss,
  l1d_ld_miss,
  l1d_st_miss,
  l2_ld_miss,
  l2_st_miss,
  llc_miss,
  dtlb_miss,
]
```

后续 schema 稳定后再扩展：

```text
l1i_miss
itlb_miss
inv_recv
mshr_avg
```

标签口径：

```text
cpi_uop = cycles / uops
branch_miss = count
l1d_ld_miss = count
l1d_st_miss = count
l2_ld_miss = count
l2_st_miss = count
llc_miss = count
dtlb_miss = count
```

count 类指标配 functional opportunity denom：

```text
branch_miss -> branch_count
l1d_ld_miss -> loads
l1d_st_miss -> stores + atomics
l2_ld_miss  -> loads
l2_st_miss  -> stores + atomics
llc_miss    -> mem_ops
dtlb_miss   -> mem_ops
```

## 8. Head 和 Loss

CPI head：

```text
pred_log_cpi = cpi_head(h)
pred_cpi = exp(pred_log_cpi)
```

count/rate heads 首版用 bounded rate：

```text
pred_rate_k = sigmoid(rate_head_k(h))
pred_count_k = pred_rate_k * denom_k
```

这样天然满足：

```text
0 <= pred_count_k <= denom_k
```

首版不加 count residual，保持物理边界干净。若后续绝对 count 校准不足，再单独实验：

```text
pred_count = pred_rate * denom + residual
```

推荐 loss：

```text
L =
  1.0  * L_cpi_abs
+ 0.5  * L_cpi_centered
+ 0.1  * L_cpi_rank
+ 0.5  * L_cycles_sum
+ 0.05 * L_count_log
+ 0.05 * L_rate
+ 0.02 * L_physical_bounds
```

定义：

```text
L_cpi_abs = Huber(pred_log_cpi, log(label_cpi_uop))

cycles_pred_core  = pred_cpi_core * uops_per_core
cycles_label_core = label_cpi_uop * uops_per_core
L_cycles_sum = Huber(log(sum_core cycles_pred_core),
                     log(sum_core cycles_label_core))

L_cpi_centered = Huber(pred_log_cpi - mean_core(pred_log_cpi),
                       log(label_cpi) - mean_core(log(label_cpi)))

L_rate = Huber(pred_rate, label_count / denom)  # denom > 0 only
L_count_log = Huber(log1p(pred_count), log1p(label_count))
```

`L_cpi_rank` 只在 core pair 的 label log-CPI gap 足够大时启用，约束预测排序方向，
避免 high-spread 窗口坍缩到均值。

denom=0 时 mask 掉 rate loss；count label 应为 0，count log loss 可保留低权重约束。

## 9. 训练数据和评估

训练窗口继续使用 TQ tail-aligned 方案，保持和部署动态切窗接近：

```text
每核至少 nmin UOP
尾部时间尽量对齐
max_len 预算约束
c04/c08/c16/c32 混合
```

首版不输入 planner state，也不启用 `L_tail_time`，避免真值 start/tail 泄漏争议。
`true_t_start_rel/true_t_end_rel` 只保留在 meta 或诊断中。

评估必须包含：

```text
c04/c08/c16/c32 seedB full eval
global / mean / median / p90 workload pred_vs_roi_cpi_uop
per-core CPI log-MAE / rank accuracy / pred-vs-label std
branch/cache/TLB aggregate count relative error
rate calibration
physical bound violation rate
```

## 10. 实验顺序

为保持归因干净：

| 版本 | 改动 | 目的 |
| --- | --- | --- |
| v25a-repro | 当前 12M + 7 维 PMU + v22 loss | 锚定基线 |
| v26a-schema | 补回 `dtlb_miss`，统一 data/model/loss PMU schema | 消除口径漂移 |
| v26b-input | 结构化 UOP fields + side/global + query-centric KVQR，12M | 验证输入和 R |
| v26c-full | 放大到 32M | 验证全量 backbone |
| v26d-replay | 后续再加 planner replay/planner_feats | 验证动态闭环 |

本文件定义的是 v26b/v26c 的干净首版方案：只保留 side/global，不引入 planner。

## 11. 需要修改的模块

- `model/tokenizer.py` 可拆成 `model/features.py`：定义 UOP field buckets、feature
  schema、compact indexer。
- `data/build_windows.py`：输出 v26 UOP fields、side/global feats、denoms，并统一
  `PMU_KEYS_V26A`。
- `train/dataset.py`：加载结构化 `[C,L,F]` 张量、mask、side/global、denoms。
- `model/tiny_transformer.py` 或新模型文件：实现 query-centric KVQR。
- `model/regression_head.py`：实现 CPI head + bounded rate heads。
- `train/loss.py`：实现 v26 CPI/count/rate/rank/cycles loss。
- `eval/eval_quota_cycles.py`：加载新 schema 的 checkpoint 并构造相同结构化输入。

## 12. 结论

v26 首版的核心是：

```text
structured core-axis input
+ direct UOP field embeddings
+ side/global functional summaries
+ per-core learned query
+ KVQR query-to-all-UOP cross attention
+ bounded count/rate PMU heads
```

这版刻意不加入 local encoder、core memory、planner_feats、LLM special tokens 和
绝对 core-id embedding。只有当干净首版暴露出明确瓶颈时，再通过 ablation 增加对应组件。
