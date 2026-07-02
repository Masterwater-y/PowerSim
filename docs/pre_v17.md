# pre-v17 方案设计：显式跨核交互与结构化 side encoder

日期：2026-07-02

状态：方案稿。v17 目标是在 v16 `tail_local` 的基础上，补上当前 LLMSim 最缺的两类归纳偏置：

- 读出后的显式 cross-core communication。
- 结构化 side/cfg/summary feature 的字段级建模。

一句话总结：**v17 不是单纯继续改 tokenizer/embedding，而是让 LLM 负责长 UOP 序列表示，让小型结构模块负责 core 间交互和 tabular feature 交互。**

## 1. 当前判断

当前实现已经不再是最早的“6 个普通 token 表示一个 uop”：

```text
<UOP> position + UopEncoder(OP/RG/MK/RD/ST/BR)
+ per-core summary tokens
+ global tokens
+ side_feats
+ t_start_rel
+ v16 LOCAL_Ci
```

模型主路径为：

```text
tokens/uop_fields/side_feats
-> Qwen backbone + LoRA
-> gather hidden(<QUERY_Ci>) 得到 query_hidden [B, C, D]
-> local_proj + side_proj + tstart_proj
-> PMURegressionHead
```

因此当前问题不能简单归因于“tokenizer/embedding 不好”。更准确地说，核心问题是：

```text
输入表示已有改进，但多核结构和结构化特征仍主要靠隐式学习。
```

## 2. 已有证据

### 2.1 v15 的 segment query 暴露了 causal 可见性问题

v15 把 query 放进每个 core 段内：

```text
<C0_BEGIN> C0_uops <QUERY_C0> <C0_END>
<C1_BEGIN> C1_uops <QUERY_C1> <C1_END>
...
```

这能增强本核绑定，但 Qwen 是 causal decoder，早序号 query 看不到后续 core：

```text
QUERY_C0 只能看到 global + C0
QUERY_C1 只能看到 global + C0 + C1
```

结果是 `W_phased_mix` c08 seedB 从 v9_tq 级别退化到 v15 的 `40.10%`，说明只靠 query placement 无法同时满足：

- 本核局部绑定。
- 全局 phase 判断。
- 跨核可见性。

### 2.2 v15 对部分 workload 有改善，但不是根解

v15 c08 seedB step6500 best：

| workload | pred | ROI | err |
|---|---:|---:|---:|
| `W_false_sharing` | 13.0585 | 16.3142 | 19.96% |
| `W_ads_ranking_proxy` | 0.4643 | 0.6052 | 23.28% |
| `W_phased_mix` | 1.0018 | 0.7150 | 40.10% |
| all 17 workloads mean | - | - | 8.64% |

判断：

- segment query + delta/rank 对 high-CPI coherence case 有一定帮助。
- `ads_ranking_proxy` 仍然低估，说明并发随机 gather / LLC / DRAM / MSHR / queue pressure 没有充分表达。
- `phased_mix` 退化说明 causal 可见性损失不可接受。

### 2.3 v16 是当前实现 baseline，但问题已收敛到少数 workload

v16 `tail_local` 结构：

```text
<SYS> cfg <TRACE> global_tokens

<C0_BEGIN> C0_summary C0_uops <LOCAL_C0> <C0_END>
<C1_BEGIN> C1_summary C1_uops <LOCAL_C1> <C1_END>
...

<TRACE_END>
<QUERY_C0> <QUERY_C1> ... <QUERY_CN>
```

语义分工：

- `LOCAL_Ci` 位于本 core 段尾部，给本核局部锚点。
- `QUERY_Ci` 位于序列尾部，能看到所有 core 和所有 local token。
- head 输入使用 `query_hidden + local_proj(local_hidden) + side_proj(side_feats) + tstart_proj(t_start)`。

当前 v16 数据与 cache 已完成：

```text
data/windows_v16_v9core_tail_local_all/windows.jsonl
data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache
samples = 39370
```

step5000 已完成 c04/c08/c16 seedB 端到端评估，详细结果见：

```text
docs/eval_v16_tail_local_step5000_results_20260702.md
```

汇总：

| core | mean pVr | median pVr | max pVr | worst workload |
|---:|---:|---:|---:|---|
| c04 | 5.61% | 3.48% | 14.88% | `W_ads_ranking_proxy` |
| c08 | 6.33% | 4.31% | 34.09% | `W_ads_ranking_proxy` |
| c16 | 10.85% | 3.52% | 71.81% | `W_phased_mix` |

判断：

- v16 在 c04/c08 上已经明显优于 v13/v14，c16 median 也明显改善。
- `W_false_sharing` 已从主要瓶颈变成可控项，c04/c08/c16 分别为 `1.66% / 1.99% / 7.02%`。
- `W_ads_ranking_proxy` 仍随核心数增加低估，说明多核随机 gather / LLC / DRAM pressure 仍没有被稳定建模。
- `W_phased_mix` 在 c08 被修复，但 c16 退化到 `71.81%`，说明高核心数 phase / planner 闭环仍可能失稳。
- 因此 v16 可以作为 v17 的实现基线，但不是最终收敛方案。v17 应重点验证 per-core CPI 快慢核识别、oracle label-cut 与 free-running pred-cut 的差距，以及 ads/phased 两个 outlier 的误差来源。

## 3. v17 核心改动

### 3.1 新增 cross-core adapter

当前 `query_hidden` 形状是：

```text
[B, C, D]
```

含义：

- `B`: batch size。
- `C`: active core 数。
- `D`: Qwen hidden size。

当前做法是每核 hidden 直接进共享 head。v17 建议在 PMU head 前插入一个可变长度、mask-aware 的 cross-core adapter：

```text
query_hidden [B, C, D]
+ local/side/tstart
-> CoreAdapter([B, C, D], core_mask)
-> adapted_hidden [B, C, D]
-> shared PMURegressionHead
```

推荐第一版实现：

```text
2-layer TransformerEncoder
hidden size = D
num heads = 8 或 16
FFN dim = 2D 或 4D
dropout = 0.05
src_key_padding_mask = ~core_mask
zero/residual init，保证初始近似旧模型
```

为什么不是固定 MLP：

```text
flatten [C, D] -> [C*D] -> MLP
```

这种会把训练核心数写死，损害 c01/c04/c08/c16 -> c32 泛化。

cross-core adapter 必须满足：

- 参数不依赖 core 数 `C`。
- 支持 padding core mask。
- 尽量 permutation-equivariant，不给固定 core index 过强特权。
- 允许通过 `log1p_active_cores` 等特征表达核心数尺度。

预期收益：

- 显式学习 core 间快慢排序。
- 显式学习共享资源竞争对每核 CPI 的影响。
- 减轻 tail query 自己通过长序列 attention 学跨核关系的压力。
- 对 `ads_ranking_proxy`、`false_sharing`、c32 外推更有帮助。

### 3.2 将 `side_proj` 升级为 FTTransformer-style side encoder

当前 side path 是：

```text
side_feats [B, C, F]
-> Linear(F, D)
-> add to query_hidden
```

这会把 34 个 side feature 直接混成一个向量，字段身份和字段间交互都靠一个线性层表达，结构偏弱。

v17 建议改成：

```text
side_feats [B, C, F]
-> FeatureTokenizer
   每个字段 -> 一个 feature token
-> per-core feature Transformer
-> side_hidden [B, C, D]
-> add to query_hidden
```

FeatureTokenizer 形式：

```text
continuous feature j:
  token_j = field_bias_j + normalized_value_j * field_weight_j

categorical feature j:
  token_j = field_bias_j + embedding_j[value]
```

第一版可以只处理连续 side feature：

```text
side_feats [B, C, F]
-> tokens [B, C, F, d_side]
-> flatten to [B*C, F, d_side]
-> small TransformerEncoder over F feature tokens
-> pool/CLS -> [B*C, D]
-> reshape [B, C, D]
```

注意：FTTransformer 不替代 Qwen。它只处理 structured side/cfg/summary scalar，不处理长 UOP 序列。

分工应保持为：

```text
Qwen:
  长 UOP 序列、局部依赖、程序序、访存模式、phase 表示。

FTTransformer side encoder:
  structured feature 的字段身份、连续值、字段组合关系。

Cross-core adapter:
  core-core communication、快慢核排序、共享资源竞争。
```

预期收益：

- 比单层 `side_proj` 更好表达字段身份。
- 更适合表达 gather/queue pressure 这类组合特征。
- 降低“新增 side feature 后只是线性相加，模型难以使用”的风险。

### 3.3 补 functional gather / queue pressure side features

`W_ads_ranking_proxy` 的主要问题不像 false sharing 那样是共享写 coherence storm，而是多核并发随机 gather 导致的共享 LLC/DRAM/MSHR/queue pressure。

建议新增字段，全部从 functional trace 和 cfg 得到，不引入 timing/PMU oracle：

| feature | 作用 |
|---|---|
| `cold_loads_per_kuop` | 冷/远复用 load 强度 |
| `indep_random_loads_per_kuop` | 独立随机 gather 强度 |
| `addrdep_random_loads_per_kuop` | pointer chasing 强度 |
| `random_unique_lines_per_kuop` | 随机 load footprint |
| `random_unique_pages_per_kuop` | TLB/page footprint |
| `random_line_entropy_norm` | 地址随机性 |
| `random_page_entropy_norm` | page 分散度 |
| `load_pc_top_frac` | 少数 load PC 是否主导 |
| `load_pc_entropy_norm` | load site 多样性 |
| `llc_footprint_pressure` | 全局 footprint 相对 LLC 容量 |
| `l2_footprint_pressure` | 每核 footprint 相对 L2 容量 |
| `gather_pressure_ncore` | 独立随机 gather * footprint * 核数 |
| `queue_pressure_proxy` | load density * gather pressure * footprint |

推荐公式草案：

```text
random_load =
  is_load and (
    rd_bucket in {RD_COLD, RD_FAR}
    or stride_bucket in {ST_P9_64, ST_M9_64, ST_LARGE}
  )

indep_random_load =
  random_load and not addr_dep_load

addrdep_random_load =
  random_load and addr_dep_load

llc_footprint_pressure =
  squash(unique_random_lines / llc_lines)

l2_footprint_pressure =
  squash(unique_core_lines / l2_lines)

gather_pressure_ncore =
  indep_random_load_density
  * llc_footprint_pressure
  * log1p(n_core)

queue_pressure_proxy =
  aggregate_load_density
  * gather_pressure_ncore
  * squash(lines_per_kuop_global)
```

预期收益：

- 针对 `ads_ranking_proxy` 的 c08/c16/c32 低估。
- 区分普通大 working set、pointer chasing、独立 embedding gather。
- 给 cross-core adapter 明确的共享内存压力信号。

### 3.4 加强 per-core CPI / delta 监督，避免均值化

当前训练并不是没有 per-core CPI loss。`train/loss.py` 的主 CPI loss 是：

```text
target_i = log(label_cpi_i)

L_cpi =
  mean_{batch, active_core} Huber(
    pred_log_cpi_i - target_i,
    delta = 0.1
  )
```

也就是说，每个 active core 的 `log(cpi_uop)` 都被监督。但这仍不足以保证 online planner 需要的快慢核识别，因为：

- per-core loss 是所有窗口/核心平均，easy workload 会稀释 high-spread 窗口。
- 该 loss 惩罚绝对 CPI，不专门惩罚核间相对快慢排序。
- 当预测都靠近窗口均值时，平均 Huber loss 可能还可以，但 planner 会拿到错误的每核 CPI，导致下一窗切分偏移。
- 当前 `cpi_head_mode=delta` 只是结构上采用 `base + delta`，还没有单独监督 `delta_i`。

当前 v16 head 结构为：

```text
pred_log_cpi_i = base_log_cpi + pred_delta_i
mean_i(pred_delta_i) = 0
```

v17 应把这个结构变成显式训练目标：

```text
label_log_cpi_i = log(label_cpi_i)
label_base = mean_active(label_log_cpi_i)
label_delta_i = label_log_cpi_i - label_base

pred_base = mean_active(pred_log_cpi_i)
pred_delta_i = pred_log_cpi_i - pred_base

L_delta =
  mean_active Huber(pred_delta_i - label_delta_i, delta=0.1)
```

`L_delta` 的目的不是替代 `L_cpi`，而是防止模型只学窗口平均 CPI。它直接约束：

```text
哪个 core 更慢
慢多少
核间 CPI spread 是否被压扁
```

同时保留并加强 rank/spread：

```text
L_rank =
  pairwise logistic loss on sign(label_delta_i - label_delta_j)
  only if |label_delta_i - label_delta_j| > rank_gap

L_spread =
  Huber(log(std(pred_delta)+eps), log(std(label_delta)+eps))
  only if std(label_delta) > spread_min_std
```

建议增加 high-spread window weighting：

```text
spread_w =
  1 + alpha * clamp(std(label_delta) / spread_ref, 0, w_max)

L_cpi_core = spread_w * L_cpi
L_delta    = spread_w * L_delta
L_rank     = spread_w * L_rank
L_spread   = spread_w * L_spread
```

第一版参数建议：

```text
lambda_delta = 0.5 ~ 1.0
lambda_rank = 0.02
lambda_spread = 0.02
spread_ref = 0.10
w_max = 3.0
```

如果 rank/spread 在低差异窗口放大噪声，则只在 label spread 足够大时启用：

```text
enable_relative_losses = std(label_delta) > spread_min_std
```

可选再加 slowest/fastest core loss：

```text
slowest_label = argmax_i(label_delta_i)
slowest_logits = pred_delta_i / tau
L_slowest = CE(slowest_logits, slowest_label)

fastest_label = argmin_i(label_delta_i)
fastest_logits = -pred_delta_i / tau
L_fastest = CE(fastest_logits, fastest_label)
```

该项只应在 high-spread 窗口开启，避免正常低差异 workload 被迫制造假差异。

预期收益：

- 提升 `pred_label_corr`。
- 提升 `slowest_core_hit_rate` / `fastest_core_hit_rate`。
- 让 `pred_core_cv` 接近 `label_core_cv`，减少 CPI spread 被压扁。
- 让 planner 使用的每核 CPI 更可靠，降低 free-running 切窗累计偏移。

### 3.5 改 window-level cycles loss

当前 cycles loss 近似为：

```text
log_cycles_pred_i = pred_log_cpi_i + log(uops_i)
log_cycles_label_i = log(label_cpi_i) + log(uops_i)
```

因为同一个 `log(uops_i)` 出现在两边，约束仍接近 per-core CPI loss。

v17 建议新增真正窗口级 cycles loss：

```text
pred_cycles_window =
  sum_i exp(pred_log_cpi_i) * uops_i

label_cycles_window =
  sum_i label_cpi_i * uops_i

L_cycles_window =
  Huber(log(pred_cycles_window), log(label_cycles_window))
```

它与当前 per-core CPI loss 并存：

```text
L = L_pmu_core
  + lambda_delta * L_delta
  + lambda_cycles_window * L_cycles_window
  + lambda_rank * L_rank
  + lambda_spread * L_spread
  + lambda_slowest * L_slowest
  + lambda_phys * L_phys
```

第一版参数：

```text
lambda_cycles_window = 1.0
lambda_rank = 0.01 ~ 0.02
lambda_spread = 0.01 ~ 0.02
```

预期收益：

- 直接约束 planner 关心的窗口总周期。
- 降低单核误差互相抵消或放大的不可控性。
- 减少 online free-running planner 中的闭环偏移。

### 3.6 PMU 多任务降权或分阶段训练

当前 8 个输出指标包括：

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

PMU count 诊断有价值，但部分 miss count 的 per-window MAPE 很大，容易干扰 CPI 表示。

v17 第一版建议：

- CPI/branch/cycles/rank/spread 作为主训练信号。
- cache/dtlb count 头保留输出，但 loss 降权或后期开启。
- report 中继续保留 PMU median error，不作为主验收门槛。

可选训练策略：

```text
phase A:
  train CPI + branch + cycles_window + rank/spread

phase B:
  小权重加入 cache/dtlb PMU loss
```

## 4. 预期数据流

```text
raw trace
-> data/build_windows.py
   - RD/stride/proxy 标注
   - TQ tail-aligned slicing
   - labels / denoms / side_feats / uop_fields

windows.jsonl / tensor_cache
-> train/dataset.py
   input_ids      [B, L]
   is_uop         [B, L]
   uop_fields     [B, L, 6]
   query_pos      [B, C]
   local_pos      [B, C]
   side_feats     [B, C, F]
   t_start        [B, C]
   label          [B, C, K]
   core_mask      [B, C]

model/llm_wrapper.py
-> token embedding + UopEncoder replacement
-> Qwen backbone + LoRA
-> last_hidden_state [B, L, D]
-> gather QUERY/LOCAL:
   query_hidden [B, C, D]
   local_hidden [B, C, D]

side_feats
-> FTTransformer side encoder
-> side_hidden [B, C, D]

fused_hidden =
  query_hidden
  + local_proj(local_hidden)
  + side_hidden
  + tstart_proj(t_start)

fused_hidden
-> CoreAdapter [B, C, D]
-> PMURegressionHead
-> pred [B, C, K]
-> loss
```

这样仍然能吃上 LLM 大参数：

- 所有长 trace token 仍经过 Qwen backbone。
- Qwen 负责重表示。
- adapter 和 FTTransformer 是小模块，只补结构归纳偏置。

## 5. 实施顺序

### 5.0 当前主线口径：排除 `W_phased_mix`

`W_phased_mix` 同步版先从主训练集和主验证集排除，只保留为
stress-only / diagnostic workload。原因是它的单阶段特征已被
`compute_int`、`branch_storm`、`chase_dram` 等负载覆盖，而 c16 异常主要来自
所有核同步进入随机访存相触发的 synthetic LLC/DRAM cliff。

主线版本验收先聚焦：

- `W_ads_ranking_proxy` 的快慢核识别和 free-running 切窗误差。
- `W_false_sharing` 不因修正快慢核而明显退化。
- c04/c08/c16/c32 的普通 workload mean/median/max CPI pVr。

`W_phased_mix` 后续只作为 planner stress test 单独报告，不混入 overall mean。

### v17A: cross-core adapter only

目标：最小验证显式 core communication 是否有收益。

改动：

- 新增 `CoreAdapter`。
- 插在 `local/side/tstart` 融合之后、PMU head 之前。
- 保持 v16 dataset、side features、loss 不变。

验收：

- v16 baseline vs v17A 同 checkpoint/eval 口径。
- c04/c08/c16/c32 主线 16 workload。
- 重点看 `W_ads_ranking_proxy`、`W_false_sharing`。

### v17B: per-core delta / rank / spread anti-mean-collapse loss

目标：防止模型把每个 core 的 CPI 都预测成窗口平均值，提升 online planner 需要的快慢核识别。

改动：

- 在 `train/loss.py` 增加 `L_delta`。
- 让 `L_rank` / `L_spread` 按 label spread gated 或加权。
- 可选增加 `L_slowest` / `L_fastest`。
- 训练参数显式记录 `lambda_delta`、`spread_ref`、`spread_weight_max`。

验收：

- `W_ads_ranking_proxy` 的 `pred_label_corr` 上升。
- `slowest_core_hit_rate` 明显高于随机基线。
- `pred_core_cv / label_core_cv` 更接近 1。
- free-running 的 `pred_true_start_err_mean_abs` 下降。
- `planner-state-source=label` oracle 切窗下的 CPI 误差和 free-running 差距缩小。
- 不因 rank/spread 过强导致 low-spread workload 退化。

### v17C: window-level cycles loss

目标：验证真正窗口级 cycles 约束对 online planner 是否有帮助。

改动：

- 在 `train/loss.py` 增加真正窗口级 `L_cycles_window`。
- 保留旧 per-core CPI loss。
- 训练参数显式记录 `lambda_cycles_window`。

验收：

- aggregate CPI pVr。
- free-running planner 的 `pred_start_cycle_error` / `pred_start_skew_cycle`。
- `W_ads_ranking_proxy` free-running 与 oracle/tq-forward 差距是否缩小。

### v17D: FTTransformer side encoder

目标：替换单层 `side_proj`，让 structured side feature 更可用。

改动：

- 新增 `SideFeatureTokenizer` 和 `SideFeatureTransformer`。
- 初始只吃当前 34 维 `SIDE_FEATURE_KEYS`。
- 输出维度对齐 Qwen `D`。

验收：

- side encoder 是否优于线性 `side_proj`。
- `ads_ranking_proxy` 是否改善。
- 是否引入训练不稳定。

### v17E: gather/queue pressure features

目标：针对 `ads_ranking_proxy` 和 memory queue 类低估。

改动：

- 扩展 `SIDE_FEATURE_KEYS`。
- 修改 `build_cross_core_features()`。
- 同步 eval online path。
- bump cache `feat_version`。

验收：

- `W_ads_ranking_proxy` c08/c16/c32 pVr。
- `W_graph_recall_proxy`、`W_interest_graph_recall` 是否同步改善或退化。
- non-memory workload 不应明显退化。

## 6. 风险与控制

### 风险 1: adapter 破坏核心数泛化

控制：

- 不使用 flatten 固定 C 的 MLP。
- 使用 mask-aware Transformer/SetTransformer/DeepSets。
- 训练覆盖 c01/c04/c08/c16，评估 c32。

### 风险 2: side encoder 参数太多，过拟合 train workloads

控制：

- 先用小 `d_side`。
- dropout 0.05。
- 与 v17A 分开 ablation。
- 不引入 workload name 或 seed。

### 风险 3: 新 gather features 变成 workload 特化

控制：

- 所有 feature 从 functional trace 和 cfg 计算。
- 公式保持物理含义。
- 同时检查 graph/search/interest/stream/chase 等邻近 workload。

### 风险 4: rank/spread 放大噪声

控制：

- 保持小权重。
- 只在 label spread 足够大时启用。
- 后续可改为连续 gating。

## 7. 最小成功标准

v17 不应只看 overall mean。建议固定报告：

```text
all 17 workloads:
  mean / median / max CPI pVr

重点 workload:
  W_ads_ranking_proxy c08/c16/c32
  W_false_sharing c04/c08/c16/c32

per-core diagnostics:
  pred_label_corr
  slowest_core_hit_rate
  fastest_core_hit_rate
  pred_core_cv / label_core_cv
  pred_core_range / label_core_range
  pred_start_cycle_error
  planner-state-source=label oracle 切窗 vs pred free-running 差距

PMU diagnostics:
  branch_miss median error
  dtlb_miss median error
  cache miss count 仅作辅助
```

最低预期：

- `W_ads_ranking_proxy` 相比 v16/v9 baseline 有稳定下降。
- `W_false_sharing` 不因修 ads 而明显退化。
- c32 外推不出现新的最大误差项。

## 8. 暂不优先做的方向

### RQ-VAE tokenizer

RQ-VAE 需要可靠的已有连续 embedding 空间。当前更大的问题是结构归纳偏置不足，而不是码本压缩本身。

建议等 v17A-C 稳定后再评估：

```text
TAO / trace embedding
-> RQ-VAE
-> discrete semantic codebook
```

### 用 FTTransformer 替代 Qwen

不建议。FTTransformer 适合 structured feature，不适合直接替代超长 UOP 程序序列建模。

推荐定位：

```text
Qwen: 长序列表示
FTTransformer: structured side feature 表示
CoreAdapter: core 间交互
```
