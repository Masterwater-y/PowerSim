# PMU Precision Training Plan

目标：在 **CPI 精度不退化** 的前提下，大幅提升 functional trace -> PMU 的预测精度。本文档只整理当前设计方案，不代表已全部落地到代码。

## 1. 当前模型状态

当前训练不是“只训练预测头”。`LLMSimModel` 冻结 Qwen3 base 主体和 LM head，但训练以下参数：

- LoRA adapter：`q_proj/k_proj/v_proj/o_proj`。
- PMU regression head。
- `tstart_proj`。
- loss 里的可学习 `log_var`。
- 新增 special-token embedding 行；原始 tokenizer embedding 行通过 gradient mask 冻结。

当前 PMU head 输出：

```text
cpi_uop
branch_miss
l1d_ld_miss
l1d_st_miss
l1i_miss
llc_miss
dtlb_miss
mshr_avg
```

当前目标空间：

- `cpi_uop`: `log(cpi_uop)`。
- miss count: `log1p(count)`。
- `mshr_avg`: direct regression。
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
| `l2_miss` / `l2_ld_miss` / `l2_st_miss` | `log1p(count)` |
| `llc_miss` | `log1p(count)` |
| `dtlb_miss` | `log1p(count)` |
| `mshr_avg` | direct 或标准化 direct |
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
l2_miss / l2_ld_miss / l2_st_miss
llc_miss
dtlb_miss
mshr_avg
```

### 3.2 降权或诊断 PMU

`l1i_miss` / `itlb_miss` / frontend stall 不建议作为 hard gate。原因是输入是 committed functional trace，天然缺失：

- wrong-path / speculative fetch。
- 真实 frontend fetch block 边界。
- BTB/RAS/predictor 状态。
- decode queue / frontend bubble。
- I-cache prefetch 和 speculative line fill。

当前代码里的 `fetch_groups` 实际是 macro-head proxy，而不是真实 fetch group。`macro head` 和真实取指 fetch group 不是一回事：

- 一个 fetch group 可以包含多个 macro instruction。
- 一个 x86 macro instruction 可能跨 instruction cacheline/fetch block。
- micro-op 是 decode 后的对象，不是 frontend fetch 的对象。

因此如果保留 `l1i_miss`，应明确标注为 functional-visible i-side proxy prediction，并降低训练/模型选择权重。

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

建议新增 data-side L2 PMU：

```text
l2_ld_miss = is_load  && path_class >= 2
l2_st_miss = is_store && path_class >= 2
```

或先用合并版：

```text
l2_miss = (load/store/atomic) && path_class >= 2
```

注意：L2 的真实 denominator 是 L1D miss 次数，而 L1D miss 是标签，不是 functional input。不能把真实 `l1d_miss` 作为 side tensor 输入。安全做法：

- 第一版直接预测 `log1p(l2_miss_count)`。
- 物理约束只用 `l2_miss <= mem_ops`。
- 后续可做层级约束：`pred_l2_miss <= pred_l1d_miss`。

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
| `stores` | `l1d_st_miss` denominator |
| `mem_ops` | `llc_miss`、`dtlb_miss`、`mshr_avg` denominator |
| `distinct_data_lines` | data locality scale |
| `distinct_data_pages` | DTLB opportunity / working set |
| `cond_branches` | conditional branch opportunity |
| `indirect_branches` | indirect branch opportunity |

第二阶段可考虑：

```text
log1p(atomics)
log1p(data_line_transitions)
log1p(pc_macro_heads)
log1p(distinct_pc_lines)
log1p(pc_line_transitions)
log1p(distinct_pc_pages)
window_token_len
core_fill_ratio
t_start_rel
t_end_rel / end_skew
```

其中 i-side PC 特征仅作为诊断/辅助，不建议作为主优化 gate。

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
| `l1d_st_miss` | `stores` | 是 |
| `llc_miss` | `mem_ops` | 是 |
| `dtlb_miss` | `mem_ops` 或 page touches | 是 |
| `mshr_avg` | `mem_ops` | 是 |
| `l2_miss` | 理想是 L1D miss，不可直接输入；第一版用 `mem_ops` 上界 | 部分 |
| `l1i_miss` | 当前 `macro_heads` proxy 可得，真实 fetch group 不可得 | proxy |

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
l2_miss
llc_miss
dtlb_miss
mshr_avg
```

`l1i_miss` / `itlb_miss` 单独列为 diagnostic。

## 12. 推荐实验顺序

1. 固定模型结构，只做 PMU-balanced 32k 数据续训，观察 PMU 是否提升、CPI 是否回退。
2. 加 side tensor denominator，side MLP 零初始化；冻结 LoRA，只训 head/side/aux。
3. 加 physical hard mask 和 soft constraint。
4. 加 rate/event auxiliary heads。
5. 新增 L2 label，重新构建 windows。
6. 用 6-thread 推理验证集检查泛化。

优先级：先验证 data-side PMU 和 branch PMU；i-side 不作为第一阶段 hard gate。
