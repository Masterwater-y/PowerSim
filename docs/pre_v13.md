# pre-v13 方案设计：语义锚定的结构化输入与尾部误差修正

日期：2026-06-30

状态：设计稿。目标是在 v12 结果基础上，明确下一版 v13 之前最值得验证的改动，避免继续只堆模型或只改数据格式。

## 1. 背景

v12 已经完成了几项关键改造：

- uop 压缩为 `1 uop = 1 position`，由 `UopEncoder(op, rg, mk, rd, st, br)` 生成 embedding。
- global/core attention feature 以 `GF_*` / `CF_*` token 进入序列，并由数值 encoder 覆盖 embedding。
- sharing/timing 等连续特征进入 side tensor，在 query hidden 后通过 `side_proj` 融合。
- 加入固定 summary pack：`<SUMMARY_PACK>` 与 32 个 `<C{i}_SUM>` slot，降低 core block 长度变化带来的 query 位置偏移。
- 输出头拆分为 `cpi_head`、`branch_head`、`cache_miss_head`、`dtlb_head`。

v12 的结果显示：

- easy workload 表现很好，例如 `compute_int`、`branch_storm`、`int_div`。
- `W_false_sharing`、`W_ads_ranking_proxy`、`W_phased_mix` 仍然是主要误差来源。
- high-CPI tail 明显被低估，尤其是共享写、ownership bouncing、phase skew 放大的窗口。
- side sharing 特征与 CPI 高度相关，说明数据里有信号，但当前模型没有稳定把这些信号转成高 CPI 输出。
- 4B 版本相比 0.6B 没有显著提升精度，反而推理吞吐下降很多，说明当前输入形态没有充分利用大模型已有 embedding 与预训练结构。

因此 pre-v13 的核心问题不是简单扩大模型，而是：

1. 让模型更重视 high-CPI tail 和低估风险。
2. 让 side tensor 里的强 sharing 信号更有效影响预测。
3. 让结构化 trace embedding 更接近 Qwen 熟悉的 embedding 空间。
4. 保持 `1 uop = 1 position` 的吞吐优势，不把 trace 展开成自然语言。

## 2. 目标与非目标

目标：

- 改善 `W_false_sharing`、`W_ads_ranking_proxy`、`W_phased_mix` 的 CPI 误差。
- 降低 high-CPI tail 的系统性低估。
- 在不显著增加上下文长度的前提下，让 attention 能看到窗口机制类别。
- 维持当前部署侧切窗和 tensor cache 的高吞吐路径。
- 设计清晰 ablation，判断收益来自 loss、side branch、semantic anchor 还是 context token。

非目标：

- 不把每条 uop 改写成自然语言句子。
- 不依赖 workload name 或人工 workload 标签。
- 不为每个 workload 单独加校正项。
- 不引入无法从 functional trace 推导的特征。
- 不牺牲 easy workload 的精度来强行拉高 tail。

## 3. 总体方案

方案名称：语义锚定的结构化输入方案。

核心形式仍然是结构化序列：

```text
<SYS>
<CFG_*>
<TRACE>

<WORKLOAD_MEMORY_BOUND>
<COHERENCE_BOUND>
<OWNER_BOUNCE_HIGH>
<SHARED_STORE_HIGH>
<NCORE_16>

<C0_BEGIN>
  <SM_*>
  <UOP> <UOP> <UOP> ...
<C0_END>

...

<SUMMARY_PACK>
<C0_SUM> ... <C31_SUM>
<TRACE_END>
<QUERY_Ci>
```

不同位置的 embedding 来源不同：

| 位置类型 | v12 做法 | pre-v13 做法 |
|---|---|---|
| 结构 token | 新增 token embedding | 继续使用 token embedding，可用 Qwen 词向量轻量初始化 |
| semantic context token | 无 | 新增少量窗口机制 token，进入 attention |
| uop token | `UopEncoder(fields)` 覆盖 `<UOP>` | anchor + residual，保持 1 uop 1 position |
| attention feature token | `AttentionFeatureEncoder(feat_id, value)` | feature anchor + value residual |
| side tensor | `query_hidden += side_proj(side_feats)` | gated SideMLP 融合 |
| regression head | 四类 head | 保持四类 head，优先调 CPI loss |

## 4. 当前 v12 的主要限制

### 4.1 大模型原始 embedding 利用不足

v12 虽然使用 Qwen backbone，但多数新增符号都是项目内自定义 token：

- `<UOP>` 位置被 `UopEncoder` 覆盖。
- `GF_*` / `CF_*` 位置被 attention feature encoder 覆盖。
- `<SM_*>`、`<CFG_*>`、`<QUERY_Ci>` 等新增 token 主要是随机或局部初始化后训练。
- 原始 Qwen embedding 大量冻结，只有新增 token row 可训练。

这意味着模型更多是在学习一个新的结构化 embedding 系统，而不是充分利用 Qwen 对自然语言中 "load/store/cache/branch/memory" 等概念的先验。

### 4.2 side tensor 不参与 attention

side tensor 的优点是便宜、稳定、不增加序列长度；缺点是只能在 query hidden 后融合：

```python
query_hidden = query_hidden + side_proj(side_feats)
```

这会带来两个问题：

- sharing 特征无法改变前面 uop/core token 之间的 attention pattern。
- 强 side 特征容易被最终 MLP 当作普通连续特征使用，难以形成“这是 coherence-bound 窗口”的全局上下文。

因此 v12 中 side sharing 特征与 CPI 强相关，但 tail workload 仍然低估。

### 4.3 high-CPI tail 训练目标不足

当前 loss 对主分布和 tail 分布基本同等处理。由于大多数窗口 CPI 不极端，模型会倾向于优化整体均方误差或 log-space 误差，导致：

- high-CPI 窗口低估比高估更常见。
- false sharing 这类倍增效应被预测成平滑均值。
- ads/proxy 这类 phase/timing 敏感 workload 的窗口峰值被压低。

## 5. pre-v13 改动一：Tail-aware CPI Loss

这是优先级最高的改动，因为它不需要重建数据集，也不会影响推理吞吐。

建议 CPI loss 从统一权重改为 tail-aware：

```python
base = smooth_l1(log_pred_cpi, log_label_cpi)
tail_w = 1 + lambda_tail * sigmoid((label_cpi - cpi_tail_mid) / cpi_tail_tau)
under_w = 1 + lambda_under * relu(log_label_cpi - log_pred_cpi)
loss_cpi = mean(base * tail_w * under_w)
```

设计要点：

- `tail_w` 让高 CPI 标签窗口权重更高。
- `under_w` 专门惩罚 high-CPI 低估。
- 使用平滑权重，不按 workload 名称 hardcode。
- tail 阈值可从训练集分位数确定，例如 p80/p90，而不是固定绝对值。

建议初始参数：

```text
lambda_tail  = 1.0 ~ 2.0
lambda_under = 0.5 ~ 1.0
cpi_tail_mid = train CPI p80 或 p85
cpi_tail_tau = 0.25 ~ 0.5 in log-space
```

预期收益：

- 对 `W_false_sharing`、`W_phased_mix` 这类 high-CPI 低估最直接。
- 对 easy workload 影响可控，因为低 CPI 样本权重变化小。

风险：

- 如果 tail 权重过高，可能导致整体预测偏高。
- 需要监控 mean/median error，不能只看 worst-case。

## 6. pre-v13 改动二：Gated SideMLP

v12 的 side 融合是线性投影相加。pre-v13 建议改为 gated side branch：

```python
side_hidden = side_mlp(side_feats)
gate = sigmoid(gate_mlp(side_feats))
query_hidden = query_hidden + gate * side_hidden
```

也可以增加 residual scale：

```python
query_hidden = query_hidden + gamma_side * gate * side_hidden
```

其中 `gamma_side` 可训练，初始较小，例如 0.1 或 0.2。

设计理由：

- sharing/timing/PMU proxy 特征强度差异很大，简单线性相加不够灵活。
- gate 可以让模型在 false sharing 类窗口里更依赖 side，在 compute/branch 类窗口里少用 side。
- 不增加序列长度，不影响 prefill 开销。

建议 SideMLP 输入继续使用 v12 side tensor，但优先关注这些特征：

```text
coherence_pressure_ncore
store_owner_switch_rate
shared_store_rate
core_multi_writer_store_rate
core_writer_role
core_shared_store_rate
multi_writer_line_frac
disjoint_store_slot_pair_rate
cross_core_line_overlap
pairwise_writer_pressure
inval_fanout_proxy_mean
log1p_t_start_rel
log1p_t_start_skew
log1p_t_lag_to_leader
```

预期收益：

- 让强相关 side 特征真正影响 CPI。
- 对吞吐基本无影响。

风险：

- side branch 过强时可能绕过 backbone，模型退化为 tabular regressor。
- 可通过 `gamma_side` 小初始化、dropout、ablation 控制。

## 7. pre-v13 改动三：UOP Multi-anchor + Residual

旧方案：

```python
uop_emb = UopEncoder(op, rg, mk, rd, st, br)
```

pre-v13 建议：

```python
anchor = (
    E_op_anchor[op_group]
  + E_mem_anchor[mem_group]
  + E_loc_anchor[locality_group]
  + E_share_anchor[share_group]
)
residual = UopResidualMLP(op, rg, mk, rd, st, br, optional_extra_fields)
uop_emb = LayerNorm(anchor + alpha * residual)
```

这里不建议只用单一 anchor。单一 anchor 例如 `<MEM_RANDOM_LOAD>` 会把多个机制压成一个离散类，表达力不足。multi-anchor 可以组合：

```text
load + random + shared-line + high-owner-switch
store + shared-line + multi-writer
branch + hot-loop
int + private
```

### 7.1 Anchor 类别

第一版可以分四组。

op anchor：

```text
<OP_LOAD>
<OP_STORE>
<OP_ATOMIC>
<OP_BRANCH>
<OP_FP>
<OP_INT>
<OP_OTHER>
```

memory/locality anchor：

```text
<MEM_NONE>
<MEM_STREAM>
<MEM_RANDOM>
<MEM_HOT_LINE>
<MEM_COLD>
```

sharing anchor：

```text
<SHARE_UNKNOWN>
<PRIVATE_LINE>
<READ_SHARED_LINE>
<WRITE_SHARED_LINE>
<MULTI_WRITER_LINE>
<OWNER_BOUNCE_LINE>
```

role anchor：

```text
<ROLE_NORMAL>
<ROLE_WRITER>
<ROLE_READER>
<ROLE_OWNER_SWITCH_SOURCE>
<ROLE_OWNER_SWITCH_TARGET>
```

### 7.2 哪些可以直接从现有 functional trace 推出

| anchor | 可行性 | 说明 |
|---|---|---|
| op anchor | 可直接推出 | 来自 op 字段 |
| load/store/branch/fp/int | 可直接推出 | 现有 uop fields 已有基础类别 |
| memory stream/random/hot/cold | 部分可推出 | 依赖 line 地址序列、reuse、stride、distinct line |
| private/read-shared/write-shared | 可从窗口内 line/core 访问集合推出 | 需要按 cacheline 聚合读写 core set |
| multi-writer line | 可推出 | 同一 line 出现多个 writer core |
| owner-bounce line | 可近似推出 | 同一 line 的 writer core 在时间顺序上切换 |
| source/target role | 可近似推出 | 根据本 core 写入前后 owner 变化标注 |

如果只使用现有 6 个 uop fields，则无需重建 windows/cache；如果要给每条 uop 增加 sharing/locality anchor，就需要在 window builder 中生成 per-uop anchor id，并重建 windows/cache。

### 7.3 Semantic 初始化

anchor embedding 不建议完全随机初始化。建议使用 Qwen 原生词向量均值初始化：

```text
<OP_LOAD>      = mean("load", "memory", "read")
<OP_STORE>     = mean("store", "memory", "write")
<OP_BRANCH>    = mean("branch", "jump", "condition")
<OP_ATOMIC>    = mean("atomic", "synchronize", "memory")
<MEM_RANDOM>   = mean("random", "memory", "access")
<MEM_STREAM>   = mean("sequential", "stream", "memory")
<MULTI_WRITER_LINE> = mean("shared", "write", "memory")
<OWNER_BOUNCE_LINE> = mean("owner", "switch", "cache")
```

更稳的实现方式：

```python
anchor_emb = frozen_qwen_phrase_mean + trainable_delta
```

而不是让整行 embedding 完全自由训练。这样可以保留语义锚点，同时允许模型适配微架构含义。

`alpha` 建议为可训练标量或分组标量，初始值较小：

```text
alpha_init = 0.1 或 0.2
```

这样训练初期 embedding 主要靠 anchor，residual 逐渐学习字段细节，避免 residual MLP 一开始把语义 anchor 淹没。

预期收益：

- 让 uop embedding 更接近 Qwen 熟悉的向量空间。
- 对 4B 是否真正优于 0.6B 是关键验证点。
- 对 load/store/shared-write 组合关系更友好。

风险：

- 自然语言词向量里的 "owner/cache/coherence" 不等价于微架构因果知识。
- 如果 residual 太强，anchor 失效；如果 residual 太弱，字段细节不足。
- per-uop sharing anchor 会增加 builder 复杂度，并要求重建数据。

## 8. pre-v13 改动四：Semantic Context Tokens

在窗口前部加入少量机制 token，让 attention 从一开始知道这个窗口属于哪类机制：

```text
<WORKLOAD_MEMORY_BOUND>
<COHERENCE_BOUND>
<OWNER_BOUNCE_HIGH>
<SHARED_STORE_HIGH>
<RANDOM_LOAD_HIGH>
<PHASE_SKEW_HIGH>
<NCORE_16>
```

这些 token 不是自然语言句子，也不是 workload name，而是从 functional trace 和窗口统计推导出的离散 bucket。

建议分工：

```text
semantic context token：告诉模型“这是什么机制”
side tensor：告诉模型“机制强度是多少”
uop residual：告诉模型“局部字段细节是什么”
```

### 8.1 候选 token

核心数：

```text
<NCORE_1>
<NCORE_4>
<NCORE_8>
<NCORE_16>
<NCORE_32>
```

memory 机制：

```text
<MEMORY_BOUND_LOW>
<MEMORY_BOUND_MED>
<MEMORY_BOUND_HIGH>
<RANDOM_LOAD_LOW>
<RANDOM_LOAD_HIGH>
<STREAM_ACCESS_HIGH>
```

sharing 机制：

```text
<COHERENCE_BOUND_LOW>
<COHERENCE_BOUND_HIGH>
<OWNER_BOUNCE_LOW>
<OWNER_BOUNCE_HIGH>
<SHARED_STORE_LOW>
<SHARED_STORE_HIGH>
<MULTI_WRITER_LOW>
<MULTI_WRITER_HIGH>
```

timing/phase 机制：

```text
<PHASE_SKEW_LOW>
<PHASE_SKEW_HIGH>
<LAG_TO_LEADER_HIGH>
```

### 8.2 Bucket 设计

不要为每个连续值生成 token。建议只做低/中/高或是否高：

```text
HIGH = 当前特征超过训练集 p75 或 p80
LOW  = 低于 p50 或 p25
```

特征强度本身仍放 side tensor。这样 attention token 表达机制类别，side tensor 表达连续强度。

预期收益：

- sharing/timing 机制可以参与 attention，而不是只在 query 处融合。
- 对 false sharing 和 phase skew 的上下文建模更直接。

风险：

- 机制 token 可能成为 workload shortcut。
- bucket 阈值如果从全训练集固定，跨 core 数可能偏移。
- token 数量过多会污染序列，第一版应控制在 8-16 个 context token 以内。

## 9. pre-v13 改动五：Attention Feature Anchor + Value Residual

v12 的 attention feature 已经进入序列，但其 embedding 完全由 feature encoder 生成：

```python
attn_feat_emb = AttentionFeatureEncoder(feat_id, feat_value)
```

pre-v13 建议改为：

```python
feat_anchor = E_feat_anchor[feat_id]
value_residual = ValueMLP(feat_value)
attn_feat_emb = LayerNorm(feat_anchor + beta * value_residual)
```

feature anchor 表示“这个特征是什么”，value residual 表示“数值是多少”。

候选 feature anchor：

```text
<FEAT_CORE_COUNT>
<FEAT_TIME_START_REL>
<FEAT_TIME_LAG_TO_LEADER>
<FEAT_TIME_START_SKEW>
<FEAT_OWNER_SWITCH>
<FEAT_REMOTE_INVALIDATION>
<FEAT_COHERENCE_PRESSURE>
<FEAT_SHARED_STORE>
<FEAT_MULTI_WRITER_LINE>
<FEAT_REUSE_DISTANCE>
```

如果只是改变初始化方式，不改变 feature 列表，则不需要重建数据；如果新增 feature id 或调整 attention feature 列表，则需要重建 windows/cache。

预期收益：

- 让 `GF_*` / `CF_*` 的“特征身份”更稳定。
- 便于大模型把 feature token 当作有语义的上下文，而不是纯数值向量。

风险：

- 相比 SideMLP 和 tail loss，短期收益可能较小。
- 当前 attention feature encoder 已经具备 feat_id embedding，因此需要 ablation 验证 semantic 初始化是否真的有增益。

## 10. 输出头与 PMU 标签

v12 已拆成四类 head：

```text
cpi_head
branch_head
cache_miss_head
dtlb_head
```

pre-v13 建议保持拆分，不再回到所有输出共用一个 MLP。

原因：

- CPI 是最终主目标，和 PMU proxy 的尺度、稀疏性、噪声都不同。
- branch、cache、dtlb 的标签分布不同，共用 head 容易互相干扰。
- 当前主要问题是 CPI tail 低估，不应让 PMU loss 主导 trunk 更新。

PMU 标签继续使用 v12 的八类：

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

建议 loss 权重：

```text
CPI: 主损失，加入 tail-aware / underprediction penalty
branch: 中等权重
L1/L2/LLC: 中等或偏低权重，避免 miss proxy 噪声拉偏 CPI
DTLB: 中等权重
```

PMU 评估仍保留，但 pre-v13 的首要验收标准是 CPI，尤其是 high-CPI tail。

## 11. 数据与 Cache 影响

| 改动 | 是否需要重建 windows | 是否需要重建 tensor cache | 说明 |
|---|---:|---:|---|
| Tail-aware CPI loss | 否 | 否 | 只改训练 loss |
| Gated SideMLP | 否 | 否 | 只改模型融合 |
| UOP anchor from existing 6 fields | 否 | 可能否 | 可在模型内从字段映射 anchor |
| UOP per-uop sharing/locality anchor | 是 | 是 | 需要 builder 输出 per-uop anchor id |
| Semantic context tokens | 是 | 是 | 序列 token 变化 |
| Attention feature semantic init | 否 | 否 | 只改初始化 |
| 新增 attention feature | 是 | 是 | feature id 与序列变化 |
| 结构 token semantic init | 否 | 否 | 只改 embedding 初始化 |

建议第一轮不要一次性重建所有内容。更稳的顺序是：

1. 不重建数据，先验证 tail-aware loss + gated SideMLP。
2. 不重建数据，加入基于现有字段的 uop op/mem anchor。
3. 若有收益，再重建数据加入 semantic context tokens。
4. 最后考虑 per-uop sharing anchor。

## 12. 训练与评估计划

建议 ablation：

| 实验 | 改动 | 数据是否重建 | 目的 |
|---|---|---:|---|
| baseline | v12 当前方案 | 否 | 对照 |
| A | tail-aware CPI loss | 否 | 验证 tail 低估是否由 loss 主导 |
| B | A + gated SideMLP | 否 | 验证 side sharing 信号利用 |
| C | B + uop multi-anchor，随机初始化 | 否/可选 | 验证结构 anchor 本身 |
| D | B + uop multi-anchor，Qwen semantic 初始化 | 否/可选 | 验证语义锚定收益 |
| E | D + semantic context tokens | 是 | 验证机制 token 进入 attention 的收益 |
| F | E + feature anchor/value residual | 视情况 | 验证 attention feature 语义初始化 |

每个实验至少记录：

```text
overall CPI mean / median / max error
high-CPI tail error
W_false_sharing error
W_ads_ranking_proxy error
W_phased_mix error
easy workload error
PMU median error
uops/s/GPU
forward ms/window
```

需要同时比较 0.6B 和 4B：

- 如果 4B 在 semantic anchor 后明显优于 0.6B，说明大模型语义空间开始发挥作用。
- 如果 4B 仍不优于 0.6B，说明瓶颈主要在标签、特征或 loss，而不是模型容量。

## 13. 成功标准

pre-v13 的目标不是单点压低某个 workload，而是改善 tail 的同时不破坏主分布。

建议验收标准：

- `W_false_sharing` c08/c16 CPI error 明显低于 v12。
- `W_ads_ranking_proxy` c08/c16 CPI error 明显低于 v12。
- `W_phased_mix` c16 tail error 明显下降。
- easy workload 误差不显著变差，例如 `compute_int`、`branch_storm`、`int_div`、`indirect` 不恶化超过 1-2 个百分点。
- overall mean/median CPI 不劣于 v12，最好接近或超过 v11 0.6B。
- PMU 不作为第一目标，但不能出现大面积崩坏。
- 推理吞吐不因 token 数增加明显下降；semantic context token 额外开销应控制在 16 positions 以内。

## 14. 主要风险

### 14.1 Semantic anchor 不等价于微架构知识

Qwen 词向量里的 "cache"、"owner"、"coherence" 只是语言语义，不代表它理解 MESI ownership bouncing。因此 semantic init 只能作为 embedding 空间的初始锚点，不能替代 functional trace 特征。

缓解：

- 必须做 random anchor vs semantic anchor ablation。
- anchor 后保留 residual MLP。
- anchor delta 可训练，但初始化小。

### 14.2 Context token 可能变成 workload shortcut

如果 `<COHERENCE_BOUND_HIGH>` 基本只出现在某些 workload，模型可能学到 workload-level bias，而不是真正的窗口机制。

缓解：

- token 必须由窗口统计推导，不使用 workload name。
- bucket 阈值按训练集统计固定，评估集使用同一规则。
- 检查每个 token 覆盖的 workload/core 分布。

### 14.3 Side branch 过强导致 backbone 无效

如果 gated side branch 太强，模型可能退化成 side feature MLP。

缓解：

- `gamma_side` 小初始化。
- side dropout。
- 对比无 uop 输入或打乱 uop 的 sanity check。

### 14.4 Tail loss 导致整体偏高

过强 underprediction penalty 会让模型保守高估。

缓解：

- 同时看 signed error。
- 监控 easy workload 和低 CPI 分布。
- 使用平滑权重，不使用 hard oversampling。

## 15. 推荐实施顺序

第一阶段，不重建数据：

1. 加入 tail-aware CPI loss。
2. 将 `side_proj` 升级为 gated SideMLP。
3. 加入基于现有 fields 的 uop op/mem multi-anchor。
4. 加入 feature/structure token 的 Qwen semantic 初始化。

第二阶段，小规模重建数据：

1. 增加 semantic context tokens。
2. 构建一份小规模 train/eval cache 做 smoke。
3. 对比 baseline/A/B/C/D/E。

第三阶段，完整训练：

1. 用 c01/c04/c08/c16 seedA 训练。
2. 用 c04/c08/c16 seedB 和 c32 seedB 做推理验证。
3. 同时跑 0.6B 与 4B，判断 semantic anchor 是否让 4B 有真实收益。

第四阶段，如果仍存在 false sharing tail 低估：

1. 在 window builder 中生成 per-uop sharing/locality anchor。
2. 将 owner switch、multi-writer、shared store 从 window/core 统计下钻到 uop-level anchor。
3. 重建 windows/cache，再做 tail workload ablation。

## 16. 建议的 pre-v13 最小闭环

最小闭环不要一口气实现所有特性。建议第一版只做：

```text
tail-aware CPI loss
gated SideMLP
uop op/mem anchor + residual
Qwen semantic 初始化
```

如果这一步能降低 false sharing / ads / phased_mix 的误差，说明当前主要问题是 loss 与 embedding 利用。如果没有明显改善，再进入第二版：

```text
semantic context tokens
feature anchor + value residual
per-uop sharing anchor
```

最终目标是一句话：

```text
不要把 trace 自然语言化；用少量语义 token 给窗口定性，用 anchor 给 uop 定类，
用 residual 和 side tensor 表达细节与连续强度，让结构化输入更接近 Qwen 的语义空间，
同时用 tail-aware loss 修正 high-CPI 低估。
```

