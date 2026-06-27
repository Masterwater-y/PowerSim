# PMU Precision Training Plan

目标：在 **CPI 精度不退化** 的前提下，大幅提升 functional trace -> PMU 的预测精度。本文档只整理当前设计方案，不代表已全部落地到代码。

## 1. 当前模型状态

当前训练不是“只训练预测头”。`LLMSimModel` 冻结 Qwen3 base 主体和 LM head，但训练以下参数：

- LoRA adapter：`q_proj/k_proj/v_proj/o_proj`。
- PMU regression head。
- `tstart_proj`。
- loss 里的可学习 `log_var`。
- 新增 special-token embedding 行；原始 tokenizer embedding 行通过 gradient mask 冻结。

当前 v8 PMU head 曾包含 `l1i_miss` 与 `mshr_avg`。v9 方案先删除
`mshr_avg` 和 i-side 指标，只保留 CPI、branch/data-side miss，并加入 L2
data-side miss。

v9 PMU head 目标输出：

```text
cpi_uop
branch_miss
l1d_ld_miss
l1d_st_miss
l2_ld_miss
l2_st_miss
llc_miss
dtlb_miss
```

当前目标空间：

- `cpi_uop`: `log(cpi_uop)`。
- miss count: `log1p(count)`。
- 训练时还有 `L_cycles = Huber(log(cpi_uop * uops), log(cycles_label))`。

当前窗口 JSONL 已保存 `denoms`，但训练 cache/collate 还没有把这些 denominator 暴露给模型或 loss。

## 2. 指标空间设计

不是所有指标都要取 log。规则如下：

| 类型 | 变换 | 原因 |
|---|---|---|
| 正实数、关心相对误差 | `log(x)` | 例如 CPI，`0.5->0.6` 和 `10->12` 都是 20% 相对误差 |
| 非负 count，可为 0，长尾 | `log1p(x)` | 例如 miss count，兼容 0，并压缩长尾 |
| denominator count | `log1p(x)` | 作为输入归一化，避免窗口规模差异导致数值不稳 |
| rate/probability | 不取 log，走 `[0,1]` | 例如 `miss / denominator`，用 sigmoid/BCE/Huber |
| event flag | BCE/focal | 例如 `miss_count > 0` |

因此推荐：

| 指标 | 推荐空间 |
|---|---|
| `cpi_uop` | `log(cpi_uop)` |
| `branch_miss` | `log1p(count)` |
| `l1d_ld_miss` | `log1p(count)` |
| `l1d_st_miss` | `log1p(count)` |
| `l2_ld_miss` / `l2_st_miss` | `log1p(count)` |
| `llc_miss` | `log1p(count)` |
| `dtlb_miss` | `log1p(count)` |
| `*_miss_rate` | `[0,1]` rate head |
| `has_*_miss` | binary event head |

## 3. PMU 优先级

functional trace 对不同 PMU 的可观测性不同。应把 PMU 分层，而不是所有指标同等优化。

### 3.1 主优化 PMU

这些指标和 committed functional trace 的关系更强，适合作为 PMU 精度提升主目标：

```text
branch_miss
l1d_ld_miss
l1d_st_miss
l2_ld_miss
l2_st_miss
llc_miss
dtlb_miss
```

### 3.2 先删除的 PMU

v9 第一阶段先不训练 `mshr_avg`、`l1i_miss`、`itlb_miss` 和 frontend stall。
其中 i-side/frontend 指标不建议作为 hard gate，原因是输入是 committed
functional trace，天然缺失：

- wrong-path / speculative fetch。
- 真实 frontend fetch block 边界。
- BTB/RAS/predictor 状态。
- decode queue / frontend bubble。
- I-cache prefetch 和 speculative line fill。

当前代码里的 `fetch_groups` 实际是 macro-head proxy，而不是真实 fetch group。`macro head` 和真实取指 fetch group 不是一回事：

- 一个 fetch group 可以包含多个 macro instruction。
- 一个 x86 macro instruction 可能跨 instruction cacheline/fetch block。
- micro-op 是 decode 后的对象，不是 frontend fetch 的对象。

`mshr_avg` 也先删除：它更像队列/并发状态标签，和 committed functional trace
的直接可观测性弱于 data-side miss count。后续如果 CPI 和主要 PMU 已稳定，再作为
单独扩展头重新评估。

## 4. L2 Cache 指标

当前 PMU head 没有 L2 指标，这是实现简化，不是 functional trace 不能支持。

当前 label 构建只用了两个阈值：

```text
path_class >= 1  -> L1D miss
path_class >= 4  -> LLC miss / DRAM
```

但 `path_class` 口径实际可以区分中间层：

```text
0 = L1 hit
1 = L2 hit
2 = LLC hit
3 = NoC / remote
4 = DRAM
```

v9 正式新增 data-side L2 PMU，按 load/store 分开：

```text
l2_ld_miss = is_load             && path_class >= 2
l2_st_miss = (is_store/atomic)   && path_class >= 2
```

注意：L2 的真实 denominator 是 L1D miss 次数，而 L1D miss 是标签，不是 functional input。不能把真实 `l1d_miss` 作为 side tensor 输入。安全做法：

- 直接预测 `log1p(l2_ld_miss_count)` 与 `log1p(l2_st_miss_count)`。
- 物理约束先用 `l2_ld_miss <= load_count`、`l2_st_miss <= store_count + atomic_count`。
- 后续加层级约束：`pred_l2_ld_miss <= pred_l1d_ld_miss`、`pred_l2_st_miss <= pred_l1d_st_miss`。

新增 L2 label 通常需要从 raw/aligned trace 重新构建 windows，因为现有 windows JSONL 只保存聚合后的当前 PMU，没有保留每条 uop 的 `path_class`。

## 5. Side Tensor 与 Token

### 5.1 Side Tensor

side tensor 是不进入 token 序列的额外数值输入。模型流程：

```text
tokens -> LLM -> query_hidden
side_tensor -> small MLP -> side_hidden
query_hidden + side_hidden -> PMU head
```

优点：

- 不占上下文长度。
- 不改 tokenizer/vocab。
- 保留连续数值，不需要粗桶化。
- 适合 denominator、窗口规模、时间对齐等数值特征。

缺点：

- 需要改 dataset/collate/model forward。
- 特征只在 query/head 阶段注入，不参与 trace token 的长程 attention。

### 5.2 Token

token 方案是把特征桶化为 special token 放进序列：

```text
<SM_DEN_BR_5>
<SM_DEN_LD_7>
<SM_DEN_MEM_8>
```

优点：

- 和现有 summary token 风格一致。
- 特征可参与 attention。

缺点：

- 占 token budget。
- 需要改 tokenizer/vocab/cache。
- 桶化会损失数值精度。
- checkpoint/cache 兼容更麻烦。

### 5.3 Attention 的分工

attention 负责理解序列模式。适合 token/attention 的特征通常回答：

```text
事件如何排列？
地址/分支/依赖模式如何重复？
不同 core 之间是否有相关模式？
```

side tensor 负责提供精确规模和物理分母。适合 side tensor 的特征通常回答：

```text
有多少个？
窗口规模多大？
PMU opportunity denominator 是多少？
```

## 6. 推荐 Side Tensor 特征

side tensor 应优先放 functional trace 可精确解析、连续数值、主要服务于 head/约束的特征。

第一版最小集合：

```text
log1p(uops)
log1p(instr_retired)
log1p(branch_count)
log1p(loads)
log1p(stores)
log1p(mem_ops)
log1p(distinct_data_lines)
log1p(distinct_data_pages)
log1p(cond_branches)
log1p(indirect_branches)
```

这些特征用途：

| 特征 | 用途 |
|---|---|
| `uops` | CPI cycles、count scale |
| `instr_retired` | macro 规模校准 |
| `branch_count` | `branch_miss` denominator |
| `loads` | `l1d_ld_miss` denominator |
| `stores + atomics` | `l1d_st_miss` denominator |
| `mem_ops` | `l2_ld_miss`、`l2_st_miss`、`llc_miss`、`dtlb_miss` denominator / upper bound |
| `distinct_data_lines` | data locality scale |
| `distinct_data_pages` | DTLB opportunity / working set |
| `cond_branches` | conditional branch opportunity |
| `indirect_branches` | indirect branch opportunity |

第二阶段可考虑：

```text
log1p(atomics)
log1p(data_line_transitions)
window_token_len
core_fill_ratio
t_start_rel
t_end_rel / end_skew
```

不建议走 side tensor、继续放 token/summary 的特征：

```text
op mix ratio
memory locality refinement ratio
dependency chain summary
indirect target entropy
pc entropy
basic block length
stride/reuse-distance per-uop tokens
cross-core locality/coherence proxy
```

这些是模式型特征，适合让 attention 使用。

## 7. Denominator + Count/Rate/Event 设计

分母不需要预测，应直接从输入 functional trace 解析：

| PMU | denominator | functional-only |
|---|---|---|
| `branch_miss` | `branch_count` | 是 |
| `l1d_ld_miss` | `loads` | 是 |
| `l1d_st_miss` | `stores + atomics` | 是 |
| `l2_ld_miss` | 第一版用 `loads` 上界；后续约束 `<= pred_l1d_ld_miss` | 部分 |
| `l2_st_miss` | 第一版用 `stores + atomics` 上界；后续约束 `<= pred_l1d_st_miss` | 部分 |
| `llc_miss` | `mem_ops` | 是 |
| `dtlb_miss` | `mem_ops` 或 page touches | 是 |

推荐不要用 rate 完全替代 count，而是三路监督：

```text
count head: log1p(miss_count)
rate head:  miss_count / denominator，只在 denominator > 0 时训练
event head: miss_count > 0
```

推理第一版可用：

```text
if denominator == 0:
    pred_count = 0
else:
    pred_count = clamp(count_head_pred, 0, denominator)
```

更进一步：

```text
count_from_rate = denominator * rate_head
pred_count = blend(count_head_pred, count_from_rate, event_prob)
```

Loss 设计：

```text
L =
  L_cpi_original
+ L_cycles
+ L_count
+ lambda_rate  * L_rate
+ lambda_event * L_event
+ lambda_phys  * L_physical_constraint
+ lambda_distill * L_cpi_keep
```

初始建议：

```text
lambda_rate = 0.2
lambda_event = 0.2
lambda_phys = 0.05
lambda_distill = 0.5
```

## 8. 数据集设计

PMU 训练集应保留自然分布，同时做 PMU-aware 重采样。不要只筛 `miss > 0`，否则推理会系统性高估 miss。

推荐每个 workload 内部分层：

```text
50% natural windows
25% opportunity-but-zero windows
25% event-positive windows
```

其中：

- `natural`: 原始自然抽样，保持真实 zero 分布。
- `opportunity-but-zero`: 有 denominator，但对应 miss count 为 0，例如 `branch_count > 0 && branch_miss == 0`。
- `event-positive`: 目标 PMU count > 0。

采样应按 workload 限额，并按 per-key round-robin，而不是 union event：

```text
per_workload: 600-900 windows
event_frac: 0.25
opportunity_zero_frac: 0.25
natural_frac: 0.50
```

PMU 主训练建议优先用 32k 窗口。8k/16k 可以做 CPI fast mode，但不适合作为 PMU 主训练窗口。

## 9. 训练与重建边界

### 9.1 不需要重新采集 gem5 的情况

以下改动不需要重新采集 raw trace：

- 基于现有 32k windows JSONL 做 PMU-balanced 重采样。
- 使用 JSONL 中已有 `denoms / instr_retired / uops_per_core` 做 side tensor。
- 从现有 CPI checkpoint 初始化后继续训练。

但仍需要：

- 重新 build training cache，或者扩展 cache 以保存 side tensor。
- 继续微调新模型，不能直接用旧模型推理。

### 9.2 加 side tensor 后是否要重新训练

需要训练，但不需要从零训练。

新模型结构变为：

```text
旧: tokens -> LLM -> query_hidden -> PMU head
新: tokens -> LLM -> query_hidden
    side_tensor -> side MLP -> side_hidden
    query_hidden + side_hidden -> PMU head
```

新增 side MLP 必须训练。推荐初始化：

```text
side_proj.weight = 0
side_proj.bias = 0
```

这样训练起点和旧模型等价，不会一开始破坏 CPI。

推荐阶段：

1. 从 CPI 最佳 checkpoint 初始化。
2. 第一阶段冻结 LoRA，只训 side MLP + PMU head + auxiliary heads + log_var。
3. 第二阶段可选小 LR 解冻 LoRA：

```text
lr_lora = 1e-5 ~ 3e-5
lr_head = 5e-4 ~ 1e-3
```

### 9.3 需要重新构建 windows 的情况

以下情况需要重新从 raw/aligned trace 构建 windows，但不一定需要重新采集 gem5：

- 新增 L2 label：需要 per-uop `path_class`。
- 新增当前 JSONL 不包含的聚合标签。
- 修改 token schema 且无法从现有 JSONL 补齐。

## 10. CPI 不退化策略

PMU 专训必须保护 CPI：

- 保留 `L_cpi_uop`。
- 保留 `L_cycles`。
- 从 CPI 最佳 checkpoint 初始化。
- side MLP 零初始化。
- 第一阶段冻结 LoRA。
- 加 CPI distillation：

```text
L_cpi_keep = Huber(log(cpi_pred_new), log(cpi_pred_baseline))
```

CPI gate：

```text
CPI workload MAPE <= baseline + 0.5pp
高 CPI 误差负载不能恶化
```

## 11. 评估指标

PMU 不能只看平均 loss。需要按 key 输出：

- `count WAPE`
- `log1p MAE`
- positive recall / precision / F1
- zero false positive rate
- rate MAE，只在 denominator > 0 的窗口上算
- workload-level aggregate error
- 分 workload 的 PMU 表，避免某些 workload 被平均值掩盖

主评估优先看 data-side/branch-side：

```text
branch_miss
l1d_ld_miss
l1d_st_miss
l2_ld_miss
l2_st_miss
llc_miss
dtlb_miss
```

`mshr_avg`、`l1i_miss`、`itlb_miss` 第一阶段不进入训练和 gate。

## 12. 推荐实验顺序

1. 固定模型结构，只做 PMU-balanced 32k 数据续训，观察 PMU 是否提升、CPI 是否回退。
2. 加 side tensor denominator，side MLP 零初始化；冻结 LoRA，只训 head/side/aux。
3. 加 physical hard mask 和 soft constraint。
4. 加 rate/event auxiliary heads。
5. 使用重建后的 L2 label 验证 data-side 层级 PMU。
6. 用 6-thread 推理验证集检查泛化。

优先级：先验证 data-side PMU 和 branch PMU；i-side 与 `mshr_avg` 第一阶段删除。

## 13. v9 综合方案：uop 压缩、跨核特征与 PMU 精度

本节整合 `v8_c01_c04_tq_ddp8_s4500` 在 c06/c08 activecore 推理中的问题：

- `W_false_sharing` 在 c08 低估 62.56%，在 c06 低估 52.16%。
- `W_ads_ranking_proxy` 在 c08 低估 40.92%，在 c06 低估 33.08%。
- 两者的预测几乎不随 6/8 核变化，但 ROI CPI 明显随核心数和共享资源压力变化。

核心判断：

1. 仅靠 c01+c04 训练集无法可靠外推到 c06/c08。
2. 仅靠 per-core functional summary 无法表达跨核共享行、写者数量、并发随机访存压力。
3. 6 token/uop 在多核长窗口下浪费上下文，导致 c06/c08 每核可见 uop 过短。
4. PMU 精度训练应和 CPI 修复统一设计，避免只优化 miss count 后 CPI 退化。

### 13.1 输入形态

v9 推荐使用混合输入：

```text
<SYS> <CFG_*> <TRACE>
<G_*>                       # 少量桶化 global token，可选
<C0_BEGIN>
  <SM_*>                    # per-core summary token
  uop_embed_0               # composite uop position
  uop_embed_1
<C0_END>
...
<TRACE_END> <QUERY_C0> ...
```

普通控制、配置、summary、global、query token 仍走原 tokenizer embedding。只有 uop 位置从 6 个 token 压缩成 1 个 continuous embedding。

数据结构：

```text
input_ids:      [B, L]       # 普通 token id；uop 位置填 <UOP>
is_uop:         [B, L]       # bool
uop_fields:     [B, L, 6]    # op, rg, mk, rd, st, br；非 uop 位置填 0 或 -1
attention_mask: [B, L]
query_pos:      [B, C]
side_feats:     [B, C, F]    # denominator + sharing + memory pressure
label:          [B, C, K]
denoms:         [B, C, D]
```

模型前向：

```text
token embedding(input_ids) -> tok_emb
UopEncoder(uop_fields)     -> uop_emb
inputs_embeds = where(is_uop, uop_emb, tok_emb)
LLM(inputs_embeds, attention_mask) -> query_hidden
SideMLP(side_feats) -> side_hidden
query_hidden + side_hidden -> PMU heads
```

`SideMLP` 建议零初始化最后一层，保证从旧结构迁移时起点近似不变。

### 13.2 UopEncoder 设计

每个 uop 原先 6 槽：

```text
OP, RG, MK, RD, ST, BR
```

改为：

```text
x = concat(E_op, E_rg, E_mk, E_rd, E_st, E_br)
uop_embed = W_base(x) + MLP(LayerNorm(x))
```

建议：

- field embedding 维度先取 96 或 128。
- `W_base` 提供稳定线性组合。
- MLP 末层零初始化或小初始化，减少训练早期震荡。
- 输出维度等于 backbone hidden size。
- 保留 `<C*_BEGIN>`、`<SM_*>`、`<TRACE_END>`、`<QUERY_C*>` 等结构 token，避免 core 边界和 query 语义丢失。

预期收益：

- uop 主体长度约缩短 6 倍。
- c06/c08 每核可见上下文显著增加。
- forward 显存和耗时下降，或可在相同 `max_len` 下扩大窗口。

风险：

- 与 v8 checkpoint/token cache 不兼容，需要重建 cache 并重新训练或蒸馏。
- attention 不再直接看到 OP/RD/ST 等字段 token，只能看到 composite embedding。
- 需要重新标定窗口采样策略，否则新 `max_len` 下训练/推理窗口几何可能再次错位。

### 13.3 Global Token 与 Side Tensor 分工

原则：

- 桶化、结构型、低精度足够的信息可放 token。
- 连续数值、分母、核心数放大项、PMU opportunity 应放 side tensor。
- 跨核 sharing/memory-pressure 由 functional trace 的 `core_id`、`micro_seq`、`vaddr/cacheline_addr`、`size`、`is_load/is_store/is_atomic` 推出，不能使用 `path_class`、`coh_oracle`、`mesi_before`、tick 或 latency。

#### 13.3.1 少量 global token

global token 只保留离散结构信息：

```text
<G_NCORE_*>
<G_WINDOW_GEOM_*>
<G_HAS_SHARED_WRITE_*>
<G_HAS_HIGH_RANDOM_LOAD_*>
```

用途：

- 让 attention 在编码阶段知道当前是 1/4/6/8 核结构。
- 提供粗粒度任务上下文。
- token 数控制在 4-8 个/window，不造成上下文压力。

#### 13.3.2 Side tensor 第一版特征

每个窗口全局计算后 broadcast 到每个 query core；per-core 可得的分母则保留 per-core 版本。

基础规模与 denominator：

```text
log1p(active_cores)
log1p(uops_core)
log1p(uops_window_total)
log1p(instr_retired)
log1p(branch_count)
log1p(cond_branch_count)
log1p(indirect_branch_count)
log1p(load_count)
log1p(store_count)
log1p(atomic_count)
log1p(mem_ops)
log1p(distinct_data_lines_core)
log1p(distinct_data_pages_core)
log1p(global_distinct_data_lines)
log1p(global_distinct_data_pages)
```

跨核 false-sharing/coherence proxy：

```text
shared_line_access_rate
shared_store_rate
multi_writer_line_frac
multi_writer_store_rate
max_writer_cores_per_line_log
writer_core_coverage
pairwise_writer_pressure
store_owner_switch_rate
inval_fanout_proxy_mean
inval_fanout_proxy_max_log
disjoint_store_slot_pair_rate
overlap_store_slot_pair_rate
```

并发内存压力 proxy：

```text
aggregate_load_density
aggregate_store_density
aggregate_mem_density
global_large_stride_rate
global_stream_stride_rate
global_reuse_hot_rate
global_reuse_cold_rate
random_access_pressure
pages_per_kuop_global
lines_per_kuop_global
```

窗口几何：

```text
core_fill_ratio
window_token_len
t_start_rel_log1p          # 若 USE_TSTART 开启
end_skew_log1p             # 可选
```

最小可跑版本可以先取 24-32 维，不必一次放满所有特征。

### 13.4 核心特征定义

#### 13.4.1 核心数放大

```text
writer_cores[line] = set(core_id for store/atomic on line)
active_cores = number of active core blocks

max_writer_cores_per_line = max(len(writer_cores[line]))
writer_core_coverage = max_writer_cores_per_line / active_cores
pairwise_writer_pressure =
    sum(C(len(writer_cores[line]), 2) for line) / max(C(active_cores, 2), 1)
```

解释：

- raw count 表达规模：2/4/6/8 核。
- coverage 表达是否全员参与。
- pairwise pressure 表达潜在互扰 core-pair 数。

#### 13.4.2 程序序 bouncing

使用 `(micro_seq, core_id)` 归并多核访存事件：

```text
store_owner_switch_rate =
    count(store to same line and previous store core != current core) / store_count
```

`inval_fanout_proxy` 使用 functional-only recent sharer set：

```text
on load:
    recent_cores[line].add(core)
on store:
    fanout += len(recent_cores[line] - {core})
    recent_cores[line] = {core}
```

#### 13.4.3 False sharing slot

用 `vaddr & 63` 与 `size` 计算 cacheline 内 word/byte mask：

```text
disjoint_store_slot_pair_rate =
    cross-core same-line store pairs with non-overlap masks / all such pairs
```

`W_false_sharing` 典型表现是同 line、多 core 写、slot 多数不重叠。

### 13.5 PMU 标签与预测头

PMU 输出分三层：主 CPI、PMU count、辅助 rate/event。

主输出：

```text
cpi_uop
branch_miss_count
l1d_ld_miss_count
l1d_st_miss_count
l2_ld_miss_count      # v9 正式新增，需从 raw/aligned trace 重建
l2_st_miss_count      # v9 正式新增，需从 raw/aligned trace 重建
llc_miss_count
dtlb_miss_count
```

第一阶段不训练 `mshr_avg`、`l1i_miss`、`itlb_miss`。后续若要恢复，
应作为独立扩展实验，不进入 v9 主 gate。

每个 count PMU 同时训练：

```text
log1p(count)
rate = count / denominator
event = count > 0
```

推荐 denominator：

| PMU | denominator |
|---|---|
| `branch_miss` | `branch_count` 或 `cond_branch_count + indirect_branch_count` |
| `l1d_ld_miss` | `load_count` |
| `l1d_st_miss` | `store_count + atomic_count` |
| `l2_ld_miss` | 第一版用 `load_count` 上界；后续可约束 `<= pred_l1d_ld_miss` |
| `l2_st_miss` | 第一版用 `store_count + atomic_count` 上界 |
| `llc_miss` | `mem_ops` |
| `dtlb_miss` | `mem_ops` 或 `distinct page touches` |

推理时：

```text
if denominator == 0:
    pred_count = 0
else:
    pred_count = clamp(exp(log_count_pred)-1, 0, denominator)
    pred_rate_count = denominator * sigmoid(rate_logit)
    pred_count = blend(pred_count, pred_rate_count, event_prob)
```

第一版可以先只用 count head 输出作为主值，rate/event 仅辅助训练和评估。

### 13.6 Loss 设计

总 loss：

```text
L =
  L_cpi
+ L_cycles
+ L_pmu_count
+ lambda_rate  * L_pmu_rate
+ lambda_event * L_pmu_event
+ lambda_phys  * L_physical
+ lambda_distill * L_cpi_keep
```

建议初值：

```text
lambda_rate = 0.2
lambda_event = 0.1-0.2
lambda_phys = 0.05
lambda_distill = 0.3-0.5
```

物理约束：

```text
pred_branch_miss <= branch_count
pred_l1d_ld_miss <= load_count
pred_l1d_st_miss <= store_count + atomic_count
pred_l2_ld_miss <= pred_l1d_ld_miss
pred_l2_st_miss <= pred_l1d_st_miss
pred_llc_miss <= pred_l2_ld_miss + pred_l2_st_miss 或 mem_ops
pred_dtlb_miss <= mem_ops
```

`L_cpi_keep` 使用旧 CPI checkpoint 作为 teacher，限制 PMU 专训造成 CPI 退化：

```text
Huber(log(cpi_new), log(cpi_teacher))
```

### 13.7 数据集设计

训练集必须补核心数维度：

```text
c01, c02, c04, c06, c08
```

最低可行版本：

```text
c01 + c04 + c06 + c08
```

重点补齐 workload：

```text
W_false_sharing
W_ads_ranking_proxy
W_phased_mix
W_search_index_proxy
W_indirect
W_stream
W_chase_dram
```

采样原则：

- 使用 activecore eval 同款窗口几何生成训练窗口。
- composite uop 后重新定义 token budget，不要沿用 v8 的 uop/window 分布。
- 保留自然分布，同时做 PMU-aware 重采样。
- 对 sharing/memory-pressure 负载做 core-count stratified sampling。

建议每个 workload/core-count：

```text
50% natural windows
20% PMU event-positive windows
20% opportunity-but-zero windows
10% high-sharing/high-memory-pressure windows
```

`W_false_sharing` 必须包含 4/6/8 核样本；只靠 c01/c04 仍然难以学出 c08 CPI。

### 13.8 训练阶段

推荐四阶段：

1. **结构冷启动**
   - 新 tokenizer/cache。
   - composite uop + side tensor。
   - 从 base model 初始化，或从旧模型仅迁移非 uop special-token embedding/LoRA 后小心微调。

2. **CPI 主训**
   - 训练 LoRA + UopEncoder + side MLP + PMU head。
   - 目标是先恢复或超过 v8 CPI。
   - PMU count 低权重参与。

3. **PMU 精修**
   - PMU-balanced 数据。
   - CPI loss 和 distillation 保持开启。
   - rate/event/physical loss 开启。

4. **targeted finetune**
   - 对 `W_false_sharing`、`W_ads_ranking_proxy`、`W_search_index_proxy`、`W_phased_mix` 做小比例重采样。
   - 控制 workload 权重，避免过拟合这几个负载导致整体退化。

### 13.9 评估设计

必须同时评估 c01/c04 in-distribution 和 c06/c08 extrapolation/interpolation。

核心 CPI gate：

```text
median pred_vs_roi_cpi_uop 不退化
mean pred_vs_roi_cpi_uop <= v8
global total-cycle error <= v8
W_false_sharing c06/c08 误差显著下降
W_ads_ranking_proxy c06/c08 误差显著下降
```

建议目标：

| 指标 | v8 现状 | v9 目标 |
|---|---:|---:|
| c08 global cycle error | 35.47% | < 8% |
| c06 global cycle error | 25.38% | < 6% |
| c08 `W_false_sharing` | 62.56% | < 20%，理想 < 10% |
| c06 `W_false_sharing` | 52.16% | < 15%，理想 < 10% |
| c08 `W_ads_ranking_proxy` | 40.92% | < 15%，理想 < 10% |
| c06 `W_ads_ranking_proxy` | 33.08% | < 12%，理想 < 8% |

PMU gate：

```text
count WAPE
log1p MAE
positive recall / precision / F1
zero false positive rate
rate MAE on denominator > 0
workload-level aggregate error
```

PMU 主 gate 用：

```text
branch_miss
l1d_ld_miss
l1d_st_miss
l2_ld_miss
l2_st_miss
llc_miss
dtlb_miss
```

`mshr_avg`、`l1i_miss`、`itlb_miss` 不进入第一阶段评估 gate。

### 13.10 风险评估

| 风险 | 影响 | 缓解 |
|---|---|---|
| composite uop 与旧 checkpoint 不兼容 | 需要重训，短期成本高 | 只迁移 special-token embedding/LoRA；用 teacher distillation |
| uop 字段不可被 attention 单独读取 | 可能损失部分模式识别 | UopEncoder 使用 residual MLP；保留 summary/global token |
| side tensor 只在 query 注入 | 不能改变早期 token 表示 | 第一版接受；若不足，再做 continuous virtual tokens |
| sharing 特征实现误用 oracle 字段 | 造成 label leakage | 只允许 core_id/micro_seq/vaddr/cacheline_addr/size/load/store/atomic |
| PMU-balanced 采样高估 miss | 推理 false positive 变多 | 保留 50% natural + opportunity-zero |
| L2 label 需要重建 windows | 增加数据处理成本 | v9 rebuild 必须加入 L2 label；否则 PMU 方案不完整 |
| targeted finetune 过拟合 outlier | 其他 workload 退化 | 设置 workload 权重上限，使用 CPI gate |

### 13.11 推荐落地顺序

1. 实现 composite uop 数据结构和 `UopEncoder`，先不加 PMU 新 head。
2. 生成 c01/c04/c06/c08 activecore 几何训练集。
3. 加基础 side tensor：denominator + active_cores + uops/window。
4. 加 sharing/memory-pressure side tensor。
5. 训练 CPI+cycles 主模型，验证 c06/c08 CPI outlier 是否修复。
6. 加包含 L2 的 PMU count/rate/event heads 和 physical constraints。
7. 做 PMU-balanced 精修和 targeted finetune。

如果资源有限，最小闭环是：

```text
composite uop
+ c06/c08 数据
+ active_cores/uops/denominator side tensor
+ sharing side tensor
+ 原 PMU head
```

这个最小闭环已经能验证 `W_false_sharing` 与 `W_ads_ranking_proxy` 的主要 CPI 缺陷是否来自输入表征与核心数训练覆盖不足。

## 14. 第一版落地方案

第一版目标不是一次性完成全部 v9 设想，而是做一个可训练、可评估、能直接验证当前 CPI 缺陷根因的最小闭环。

优先修复：

```text
W_false_sharing
W_ads_ranking_proxy
W_phased_mix
W_search_index_proxy
```

第一版 PMU 输出：

```text
cpi_uop
branch_miss
l1d_ld_miss
l1d_st_miss
l2_ld_miss
l2_st_miss
llc_miss
dtlb_miss
```

第一版明确删除：

```text
mshr_avg
l1i_miss
itlb_miss
frontend stall
```

### 14.1 输入结构

第一版采用混合输入：

```text
<SYS> <CFG_*> <TRACE>
<G_NCORE_*>
<G_SHARED_WRITE_*>
<G_RANDOM_LOAD_*>
<C0_BEGIN>
  <SM_*> ...
  uop_embed_0
  uop_embed_1
<C0_END>
...
<TRACE_END> <QUERY_C0> ...
```

普通 token 继续使用 tokenizer embedding：

```text
<SYS>
<CFG_*>
<TRACE>
<G_*>
<C*_BEGIN>
<SM_*>
<C*_END>
<TRACE_END>
<QUERY_C*>
```

uop 从原来的 6 token：

```text
OP RG MK RD ST BR
```

压缩成 1 个 composite embedding：

```text
x = concat(E_op, E_rg, E_mk, E_rd, E_st, E_br)
uop_embed = W_base(x) + residual_mlp(LayerNorm(x))
```

batch 字段：

```text
input_ids:      [B, L]      # uop 位填 <UOP>
is_uop:         [B, L]
uop_fields:     [B, L, 6]
attention_mask: [B, L]
query_pos:      [B, C]
side_feats:     [B, C, F]
denoms:         [B, C, D]
labels:         [B, C, K]
```

### 14.2 Global Token

第一版只放少量 coarse global token，让 attention 在编码阶段知道当前窗口的并发/共享场景：

```text
<G_NCORE_1/2/4/6/8/OTHER>
<G_SHARED_WRITE_LOW/MID/HIGH>
<G_PAIRWISE_PRESSURE_LOW/MID/HIGH>
<G_RANDOM_LOAD_LOW/MID/HIGH>
```

global token 控制在 4-8 个/window，不承载连续精确数值。

### 14.3 Side Tensor

side tensor 放连续数值。global 特征 broadcast 到每个 core，per-core 特征按 core 给。

基础规模：

```text
log1p(active_cores)
log1p(uops_core)
log1p(uops_window_total)
log1p(instr_retired)
core_fill_ratio
```

PMU denominator：

```text
log1p(branch_count)
log1p(cond_branch_count)
log1p(indirect_branch_count)
log1p(load_count)
log1p(store_count)
log1p(atomic_count)
log1p(mem_ops)
log1p(distinct_data_lines_core)
log1p(distinct_data_pages_core)
log1p(global_distinct_data_lines)
log1p(global_distinct_data_pages)
```

false-sharing/coherence proxy：

```text
shared_store_rate
multi_writer_line_frac
max_writer_cores_per_line_log
writer_core_coverage
pairwise_writer_pressure
store_owner_switch_rate
inval_fanout_proxy_mean
disjoint_store_slot_pair_rate
```

memory-pressure proxy：

```text
aggregate_load_density
aggregate_mem_density
global_large_stride_rate
random_access_pressure
lines_per_kuop_global
pages_per_kuop_global
```

per-core 版本至少包含：

```text
core_shared_store_rate
core_shared_load_rate
core_multi_writer_store_rate
core_random_load_density
```

第一版 `side_feats` 建议控制在 32-48 维。

### 14.4 模型改动

`LLMSimModel.forward` 支持 `inputs_embeds` 路径：

```text
tok_emb = embedding(input_ids)
safe_fields = clamp(uop_fields, min=0)
uop_emb = UopEncoder(safe_fields)

inputs_embeds = tok_emb.clone()
inputs_embeds[is_uop] = uop_emb[is_uop]

out = backbone(inputs_embeds=inputs_embeds, attention_mask=attention_mask)
query_hidden = gather(out.last_hidden_state, query_pos)
query_hidden = query_hidden + side_proj(side_feats)
pred = head(query_hidden)
```

`side_proj` 最后一层零初始化，降低新增 side tensor 对 CPI 主路径的初始扰动。

### 14.5 PMU 标签

count 主空间：

```text
cpi_uop: log(cpi_uop)
miss count: log1p(count)
```

辅助监督：

```text
rate = count / denominator
event = count > 0
```

denominator：

```text
branch_miss: branch_count
l1d_ld_miss: load_count
l1d_st_miss: store_count + atomic_count
l2_ld_miss: load_count first; constraint <= pred_l1d_ld_miss
l2_st_miss: store_count + atomic_count first; constraint <= pred_l1d_st_miss
llc_miss: mem_ops
dtlb_miss: mem_ops or page touches
```

L2 label 需要从 raw/aligned trace 重建：

```text
l1d_st_miss = (is_store || is_atomic) && path_class >= 1
l2_ld_miss = is_load && path_class >= 2
l2_st_miss = (is_store || is_atomic) && path_class >= 2
```

### 14.6 Loss

第一版 loss：

```text
L =
  L_cpi
+ L_cycles
+ L_count
+ 0.2 * L_rate
+ 0.1~0.2 * L_event
+ 0.05 * L_physical
+ 0.3~0.5 * L_cpi_distill
```

物理约束：

```text
branch_miss <= branch_count
l1d_ld_miss <= load_count
l1d_st_miss <= store_count + atomic_count
l2_ld_miss <= l1d_ld_miss
l2_st_miss <= l1d_st_miss
llc_miss <= l2_ld_miss + l2_st_miss 或 mem_ops
dtlb_miss <= mem_ops
```

### 14.7 数据集

第一版必须补核心数梯度：

```text
c01
c04
c06
c08
```

资源允许时再加：

```text
c02
```

优先 workload：

```text
W_false_sharing
W_ads_ranking_proxy
W_phased_mix
W_search_index_proxy
W_indirect
W_stream
W_chase_dram
```

采样比例：

```text
50% natural
20% PMU event-positive
20% opportunity-but-zero
10% high-sharing/high-memory-pressure
```

关键要求：

- 训练窗口几何必须匹配 activecore eval。
- composite uop 后重新标定 `max_len` / `uops_per_window`。
- 第一版默认每核最小上下文为 `256 uops/core`，训练 TQ 和部署侧推理都使用这个 floor。
- TQ 仍然是时间对齐窗口：对每个尾部锚点 `T_end`，向前选择公共 `T_start`，直到窗口内最少的 core 也达到 256 uops；其他 core 可以更多。
- 不再把“尽量填满上下文”作为窗口目标；`max_len` 只作为安全上界。
- 不能沿用 v8 的 per-core uop 分布假设。

### 14.8 训练顺序

1. 实现 composite uop cache 和模型 forward。
2. 重建 c01/c04/c06/c08 activecore 几何 windows，带 L2 label、side_feats、denoms。
3. 先训 CPI 主模型：

```text
LoRA + UopEncoder + side_proj + head
PMU loss 低权重
```

4. 加 PMU count/rate/event 和 physical loss。
5. targeted finetune：

```text
W_false_sharing
W_ads_ranking_proxy
W_search_index_proxy
W_phased_mix
```

6. 跑 c06/c08 eval 做 gate。

### 14.9 验收指标

CPI gate：

```text
c08 global cycle error: 35.47% -> < 8%
c06 global cycle error: 25.38% -> < 6%

c08 W_false_sharing: 62.56% -> < 20%
c06 W_false_sharing: 52.16% -> < 15%

c08 W_ads_ranking_proxy: 40.92% -> < 15%
c06 W_ads_ranking_proxy: 33.08% -> < 12%
```

PMU gate：

```text
count WAPE
log1p MAE
positive precision/recall/F1
zero false positive rate
rate MAE on denominator > 0
workload-level aggregate error
```

### 14.10 风险判断

第一版会破坏 v8 checkpoint/cache 兼容性，需要重建 cache 和重新训练。这是合理代价：当前主要缺陷来自输入结构和核心数覆盖不足，不是简单 loss 调参能解决。

最小可行闭环：

```text
composite uop
+ c06/c08 数据
+ coarse global token
+ cross-core / memory-pressure side tensor
+ L2 PMU labels
```

### 14.11 当前代码落地状态

当前第一版代码已经落地以下部分：

- `model/tokenizer.py` 增加 `<UOP>`、coarse global token、34 维 `SIDE_FEATURE_KEYS`，并提供 `encode_uop_fields()`。
- `model/llm_wrapper.py` 支持混合输入：普通 token 走原 embedding，uop 位置用 `UopEncoder(op, rg, mk, rd, st, br)` 替换 embedding；`side_feats` 通过零初始化 `side_proj` 注入 query hidden。
- `data/build_windows.py` 的 TQ 主训练路径输出 composite uop、`is_uop`、`uop_fields`、`side_feats`、`denoms`，并把 TQ budget 改为 v9 的 `1 uop = 1 position` 口径；默认用时间对齐跨度保证每核至少 `256 uops`，不再尽量填满上下文。
- `eval/eval_quota_cycles.py` 推理编码路径同步切到 composite uop，并加载 `uop_encoder` / `side_proj`；online planner 也改为 composite uop 长度口径，默认每核至少 `256 uops`，只为尾部对齐给部分核增加 quota。
- `train/dataset.py` cache schema 升到 `feat_version=9`，collate 输出 `is_uop/uop_fields/side_feats/denoms`。
- `train/loss.py` 增加 denominator-based physical constraints。
- `model/regression_head.py` / `data/build_windows.py` / eval 聚合统一使用第一版 PMU key：

```text
cpi_uop
branch_miss
l1d_ld_miss
l1d_st_miss
l2_ld_miss
l2_st_miss
llc_miss
dtlb_miss
```

当前仍保留的边界：

- 旧的 align / timewin / quota 数据构建路径仍是 legacy 6-token uop，第一版训练应使用 TQ 路径，后续再统一迁移其他 builder。
- 当前环境缺少 `torch`，只能完成 schema smoke 和 Python 语法检查；完整 tensor forward / 1-step train smoke 需要在训练环境中跑。
- 尚未进行完整数据采集；下一步应先用 1-2 个小 workload 生成 v9 TQ windows，重建 cache，并跑极小步数训练 / eval smoke。
