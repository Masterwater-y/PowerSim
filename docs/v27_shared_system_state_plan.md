# TSim v27 Shared-System State 方案

本文整理当前讨论后的 shared-system 方案。目标是在不使用 gem5 label-only
字段作为部署输入的前提下，让模型看到可部署的 cache/coherence/TLB/MSHR
状态，从而减少 per-core CPI 均值塌缩和 pred-driven rollout 偏移。

本文件只定义设计方案，不对应当前代码实现。

## 1. 背景与核心判断

当前 v26 clean14 已经有 PC、cacheline hash、same-core reuse、cross-core
coherence proxy、fanout 和 side/global summary，但这些特征仍是 functional
统计，不维护真实的跨窗口 cache/coherence 状态。

训练集诊断显示，部分负载存在“同样可见输入对应不同 CPI 标签”的情况，典型
原因是窗口前状态不可见：

- cache warm/cold state
- line owner / sharer set / dirty owner
- previous remote writer / invalidation history
- TLB/page-walk/MSHR hotness
- per-core progress lag/lead
- branch/path predictor 历史

`/data00/yinhaolang/LLMSim/shared_system` 已有一个可从 functional mem event
stream 维护状态的 C++ shared-system simulator。它能维护：

- per-core `L1d/L1i/L2/L2_i/TLB/MSHR`
- shared MESI directory
- LLC LRU/set residency
- recent line count
- page walker
- cache/TLB/coherence PMU counters

因此 v27 的主要方向是：

1. 让 shared_system 在部署侧随 rollout 一起更新；
2. 把 shared_system 的可部署状态作为模型输入；
3. 模型继续预测真实 per-core CPI；
4. cache/TLB/coherence PMU 由 shared_system 输出或作为诊断，不再要求模型头
   直接预测；
5. branch miss 仍由模型预测，因为当前 shared_system 不维护 branch predictor。

## 2. Teacher 与 Deployable 的区分

这里有两个容易混淆的“teacher”：

### 2.1 CPI label teacher

训练目标仍使用 gem5/trace 的真实 per-core `cpi_uop` label。这是监督学习必须的
label，不属于部署输入。

### 2.2 shared-state teacher

用真实 `commit_tick` 对全局访存排序，然后跑 shared_system 得到状态。这只能作为
上限诊断或 warm start，不能作为最终部署输入，因为部署时没有真实 `commit_tick`。

最终部署一致的 shared-system 状态必须由模型自己的预测时间推进：

```text
pred_start_cycle + pred_cpi_uop * uops
    -> per-window t_pred_cycle
    -> global mem event order
    -> update shared_system
    -> next window state
```

## 3. 部署侧主流程：Lagged Shared State

首版推荐方案是方案 A：只把窗口开始前的 shared-system 状态输入给当前窗口模型。
当前窗口内部访存 replay 只用于更新下一窗口状态。

```text
initialize shared_system
initialize pred_start_cycle[c] = 0

for window k:
    1. read shared_system state before window k
    2. build current window UOP / side / global / planner features
    3. model predicts per-core cpi_uop and branch_miss
    4. use predicted cpi_uop to assign t_pred_cycle to window-k mem ops
    5. sort window-k mem ops by t_pred_cycle
    6. commit sorted mem ops into shared_system
    7. update pred_start_cycle and enter window k+1
```

这个流程没有 label leakage。窗口 k 的 shared-system 输入只来自窗口 k 之前已经
处理过的历史。

## 4. State 应该进哪里

shared-system 状态分三层输入。首版应同时有 core/global 状态；per-UOP 状态分成
“窗口开始前可读状态”和“当前窗口 replay 状态”，后者暂不作为首版输入。

### 4.1 Global state: window-level

`ss_global_feats[b]` 表示整个 shared system 在窗口开始前的状态摘要，注入
`global_cond`。

建议字段：

| 字段 | 含义 |
|---|---|
| `ss_log1p_events_seen` | shared_system 已处理 mem event 数 |
| `ss_l1d_miss_rate_ema` | 历史 L1D miss rate EMA |
| `ss_l2_miss_rate_ema` | 历史 L2 miss rate EMA |
| `ss_llc_miss_rate_ema` | 历史 LLC miss rate EMA |
| `ss_remote_hit_rate_ema` | 历史 remote hit rate EMA |
| `ss_wb_required_rate_ema` | 历史 writeback-required rate EMA |
| `ss_inval_fanout_rate_ema` | 历史 invalidation fanout / mem op |
| `ss_dtlb_miss_rate_ema` | 历史 DTLB miss rate EMA |
| `ss_active_shared_lines_log` | directory 中 shared line 数量桶 |
| `ss_active_dirty_lines_log` | directory 中 dirty/owned line 数量桶 |
| `ss_llc_occupancy_frac` | LLC 近似占用率 |

这些特征服务于全局 memory pressure 和 coherence pressure。

### 4.2 Core state: per-core

`ss_core_feats[b,c]` 表示窗口开始前每个 core 的私有状态摘要，注入
`core_cond`，并参与 per-core pooling/head。

建议字段：

| 字段 | 含义 |
|---|---|
| `ss_core_l1d_miss_rate_ema` | 该核历史 L1D miss rate |
| `ss_core_l2_miss_rate_ema` | 该核历史 L2 miss rate |
| `ss_core_llc_miss_rate_ema` | 该核历史 LLC miss rate |
| `ss_core_remote_hit_rate_ema` | 该核 remote hit rate |
| `ss_core_wb_required_rate_ema` | 该核 writeback-required rate |
| `ss_core_inval_recv_rate_ema` | 该核被 invalidate/remote 相关 proxy |
| `ss_core_dtlb_miss_rate_ema` | 该核 DTLB miss rate |
| `ss_core_mshr_depth_ema` | 该核 MSHR depth proxy |
| `ss_core_l1d_occupancy_frac` | L1D 近似占用率 |
| `ss_core_l2_occupancy_frac` | L2 近似占用率 |
| `ss_core_pred_start_cycle` | 当前 core 预测累计 cycle |
| `ss_core_lag_vs_min` | 相对最快 core 的 predicted lag |
| `ss_core_lag_vs_mean` | 相对平均 progress 的 lag |

core state 是方案 A 的关键。它把跨窗口状态显式提供给每个 core 的 CPI 预测。

### 4.3 Per-UOP state: line-level pre-window peek

per-UOP 状态应该加，但首版只加“窗口开始前可读”的 line state，不加当前窗口
replay 后的 oracle state。

对每条 memory UOP，在不修改 shared_system 状态的前提下，根据该 UOP 的
`cacheline_paddr` 查询窗口开始前状态：

| 字段 | 含义 |
|---|---|
| `ss_line_mesi_before` | 当前 line 对该 core 可见的 MESI 状态桶 |
| `ss_line_owner_dist` | owner 是 self / other / none |
| `ss_line_dirty_owner` | 是否有 remote dirty owner |
| `ss_line_sharer_bucket` | 其他 sharer 数量桶 |
| `ss_line_same_recent` | recent_line_count 桶 |
| `ss_line_l1d_present` | 该 core L1D 是否已有该 line |
| `ss_line_l2_present` | 该 core L2 是否已有该 line |
| `ss_line_llc_present` | LLC 是否已有该 line |
| `ss_line_llc_set_residency` | 访问前 LLC set occupancy |
| `ss_line_llc_lru_pos` | 访问前 LLC LRU position |
| `ss_line_dtlb_present` | DTLB 是否命中该页 |
| `ss_line_bank_id` | L1D bank bucket |

非 memory UOP 的这些字段置为 0/none。

这个设计是可部署的，因为它只读取窗口开始前状态和当前 UOP 的地址，不使用当前窗口
真实执行顺序，也不使用 label。

注意：当前 shared_system `stepImpl` 会修改状态。要实现这一层，需要新增只读
`peek` API，或者在构建阶段复制临时状态后 probe 再丢弃。首选只读 `peek`，避免
状态复制成本。

### 4.4 Per-UOP current-window replay state 暂不进首版

如果先预测一次 CPI，再用当前窗口预测时间 replay，确实可以为每条 UOP 得到更准的
`path_class/coh_oracle/MSHR/TLB` 状态。但这属于 two-pass：

```text
pass1 predict CPI
temporary replay current window
extract per-uop ss_current_* state
pass2 predict CPI
commit with pass2 CPI
```

它更强，但成本和系统复杂度明显更高。首版先不做，避免把问题混成“特征收益”和
“two-pass rollout 收益”。

## 5. 模型输入结构

在 v26 packed QKVR 主干上，v27 输入建议变为：

```text
uop_fields:       [B, C, L, F_uop]       # clean14 + ss_line_pre fields
uop_mask:         [B, C, L]
side_feats:       [B, C, S_old]
ss_core_feats:    [B, C, S_core]
global_feats:     [B, G_old]
ss_global_feats:  [B, G_ss]
planner_feats:    [B, C, P]
```

注入方式：

```text
token_emb = UopEncoder(clean14_fields, ss_line_pre_fields)
core_cond = MLP([side_feats, ss_core_feats, planner_feats,
                 log1p(n_core), log1p(total_uops), core_uop_share])
global_cond = MLP([global_feats, ss_global_feats,
                   log1p(n_core), log1p(total_uops)])

h[token] = token_emb
         + pos_emb
         + core_cond[b,c]
         + global_cond[b]
```

其中：

- line-level state 进入每条 UOP；
- core state 进入每个 core 的所有 UOP，并进入 per-core pooling/head；
- global state 进入所有 token。

## 6. 预测头调整

有 shared_system 之后，模型不再需要直接预测 cache/TLB/coherence PMU count。

### 6.1 保留的模型输出

首版输出：

```text
pred[c] = [
  cpi_uop,
  branch_miss,
]
```

可选额外输出：

```text
branch_miss_rate
cpi_uncertainty
```

但不建议首版增加太多头。

### 6.2 为什么去掉 cache/TLB PMU head

cache/TLB/coherence PMU 在 v27 中有两个来源：

1. shared_system 可以直接输出 PMU snapshot；
2. shared_system 状态已经作为模型输入影响 CPI。

继续让模型预测 `l1d/l2/llc/dtlb` PMU 会引入重复监督：

- 标签噪声和口径差异会继续污染 CPI 学习；
- 模型会被迫同时拟合 cache simulator 已经在做的状态机；
- loss 权重难调，可能再次拉向均值或对 cache PMU 过拟合。

因此模型目标应该收敛到：

- 主目标：真实 per-core `cpi_uop`
- 辅助目标：`branch_miss`

cache/TLB/coherence PMU 保留为 eval/diagnostic，不作为模型主 head。

### 6.3 branch_miss 为什么保留

shared_system 只维护 memory hierarchy，不维护 branch predictor。branch miss 会影响：

- front-end bubble
- wrong-path/speculative warmup 强度
- per-core CPI
- indirect/branch-heavy workload 的相位

因此 `branch_miss` 仍然是有价值的辅助 head。它也可以反过来驱动 shared_system
中的 speculative warm model，例如根据预测 branch miss rate 近似注入 shadow warmup。

## 7. Branch Miss 派生特征

branch miss 不能直接从 shared_system 得到，需要从 functional trace 派生 branch
predictor proxy。红线是：不能把真实 `mispredicted` 当输入；它只能作为 label。

允许使用的输入：

- committed branch PC
- branch type
- target/next PC，如果 trace 中有
- branch direction/taken，如果可从 committed control flow 推出
- call/return/indirect 类型，如果 trace 中有
- per-core 过去 functional branch history

建议新增以下 per-branch/per-UOP 字段。

### 7.1 PC/BHT/BTB proxy

| 字段 | 含义 |
|---|---|
| `br_pc_bucket` | branch PC hash bucket，当前已有 `pc_bucket` 可复用 |
| `br_bht_index_bucket` | `pc >> k` 的 BHT index hash |
| `br_gshare_bucket` | `pc_bucket xor global_history_hash` |
| `br_btb_set_bucket` | target predictor set proxy |
| `br_target_bucket` | target PC hash，非 branch 为 0 |
| `br_target_delta_bucket` | target - pc 的方向/距离桶 |

### 7.2 Local/global history proxy

| 字段 | 含义 |
|---|---|
| `br_local_hist_hash` | 同一 branch PC 最近 K 次 taken/not-taken folded hash |
| `br_local_taken_rate_bucket` | 同一 PC 最近窗口 taken rate |
| `br_global_hist_hash` | per-core 最近 K 条 branch outcome folded hash |
| `br_outcome_flip_bucket` | 当前 PC outcome 是否频繁翻转 |
| `br_streak_len_bucket` | 当前方向连续 streak 长度 |

### 7.3 Loop/phase proxy

| 字段 | 含义 |
|---|---|
| `br_is_backward` | target < pc |
| `br_loop_iter_bucket` | backward branch 连续迭代计数桶 |
| `br_loop_exit_pressure` | 当前迭代接近历史常见 trip count |
| `br_path_ngram_hash` | 最近 N 个 branch PC/outcome 的 folded hash |
| `br_basic_block_len_bucket` | 距离上一 branch 的 UOP 数 |

### 7.4 Indirect/return proxy

| 字段 | 含义 |
|---|---|
| `br_indirect_target_entropy` | indirect branch recent target entropy |
| `br_indirect_target_count` | recent distinct target count |
| `br_target_switch_rate` | target 是否频繁切换 |
| `br_return_depth_bucket` | call/return stack depth proxy |
| `br_return_match_proxy` | return target 是否匹配 recent call stack |

这些特征都是 functional/path 特征，不使用真实 misprediction。它们应进入每条 branch
UOP 的 field embedding，同时把 per-core branch summary 加入 `side_feats` 或
`ss_core_feats` 邻近的 `branch_core_feats`。

### 7.5 branch summary core features

per-core summary 建议增加：

| 字段 | 含义 |
|---|---|
| `br_count_log` | branch count |
| `br_cond_count_log` | conditional branch count |
| `br_indirect_count_log` | indirect branch count |
| `br_return_count_log` | return count |
| `br_entropy_mean` | branch outcome entropy mean |
| `br_flip_rate` | outcome flip rate |
| `br_indirect_entropy_mean` | indirect target entropy |
| `br_loop_exit_proxy_rate` | loop exit pressure |
| `br_path_hash_diversity` | path/ngram diversity |

## 8. 训练设计

训练仍使用真实 CPI/branch labels，但 shared-system 输入分阶段构造。

### 8.1 Stage A: teacher-state 上限诊断

用真实 `commit_tick` 排序历史 mem events，维护 shared_system，生成窗口开始前
`ss_*` state。

目标：

- 判断 shared-system state 是否有上限收益；
- 如果 teacher-state 也没有收益，就不应继续投入 deployable rollout state。

限制：

- 该阶段不能作为最终部署模型；
- 只能看作 upper-bound / ablation。

### 8.2 Stage B: deployable-state rollout

用已有模型在训练 raw traces 上做 free-running rollout：

```text
model pred CPI
-> pred_start_cycle
-> t_pred_cycle
-> update shared_system
-> dump ss_* state + true CPI label
```

这会生成真正部署一致的训练样本。

目标：

- 让模型适应自己造成的 shared-system state 分布；
- 减少 teacher forcing 到 pred rollout 的分布偏移。

### 8.3 Stage C: scheduled mix

混合 teacher-state 与 deployable-state：

```text
early: teacher-state ratio high
middle: teacher/deployable mixed
late: deployable-state dominant
```

这类似 scheduled sampling。它能避免训练初期 pred state 太差，同时防止最终模型只适应
真实 commit_tick state。

## 9. Loss 设计

Loss 调整必须和预测头调整一起落地。v27 不再让神经网络直接拟合
cache/TLB/coherence PMU，因此 loss 也要删掉这些 PMU count 项；否则 shared_system
负责的状态机问题又会通过辅助 loss 压回模型。

### 9.1 v27 首版总 loss

首版只监督 `cpi_uop` 和 `branch_miss`：

```text
L_v27 =
  1.0  * L_cpi_abs_log
+ 0.3  * L_pairwise_log_gap
+ 0.2  * L_cycles_sum_log
+ 0.1  * L_branch_miss
```

其中：

- `L_cpi_abs_log`: per-core log CPI Huber 或 L1；
- `L_pairwise_log_gap`: 同窗口核心间 CPI 差异，防止均值塌缩；
- `L_cycles_sum_log`: 窗口总 cycles 约束；
- `L_branch_miss`: branch miss opportunity-bounded 辅助监督。

不再加入：

- `l1d_ld_miss/l1d_st_miss/l2_miss/llc_miss/dtlb_miss` count loss；
- cache/TLB/coherence PMU 的 `L_count_log`；
- 由 cache PMU 派生的 rate loss；
- 为 cache PMU 设计的 top-k/tail loss。

### 9.2 CPI loss

`L_cpi_abs_log` 是主监督：

```text
log_pred_cpi  = log(clamp(pred_cpi_uop, eps))
log_label_cpi = log(clamp(label_cpi_uop, eps))
L_cpi_abs_log = mean_active_core(Huber(log_pred_cpi - log_label_cpi))
```

建议初始 `Huber delta = 0.2 ~ 0.3`。如果仍出现明显均值塌缩，可以切到 L1 或减小
delta；不建议用过大的 delta 把高误差样本全部变成近似 L2。

### 9.3 Pairwise gap loss

pairwise 只服务一个目标：让同窗口不同 core 的快慢关系和幅度不要被抹平。

```text
gap_pred(i,j)  = log_pred_cpi[i]  - log_pred_cpi[j]
gap_label(i,j) = log_label_cpi[i] - log_label_cpi[j]

L_pairwise_log_gap =
    mean_active_pairs(Huber(gap_pred(i,j) - gap_label(i,j)))
```

它不是排序 loss。它监督的是 log-CPI gap 的幅度，因此比纯 rank/pairwise-sign 更适合
“不要预测成单窗均值”的问题。

首版不默认加入 top-k tail loss。top-k 会人为强调少数慢核，容易让训练目标变得不稳。
如果 eval 仍显示 slowest-core top1/top2 命中率很差，再作为 ablation 加入。

### 9.4 Window cycles loss

`L_cycles_sum_log` 继续保留，因为部署端最终用 CPI 推进每核时间。

```text
pred_window_cycles  = sum_c(pred_cpi_uop[c]  * uops[c])
label_window_cycles = sum_c(label_cpi_uop[c] * uops[c])

L_cycles_sum_log =
    Huber(log1p(pred_window_cycles) - log1p(label_window_cycles))
```

这个项约束全局能量，避免 pairwise 修正后整体 CPI scale 漂移。

### 9.5 Branch miss loss

branch miss 不能用 unbounded count head。推荐模型输出 branch miss rate logit：

```text
p_branch_miss[c] = sigmoid(branch_logit[c])
pred_branch_miss[c] = p_branch_miss[c] * branch_count[c]
```

当 `branch_count[c] == 0` 时跳过该 core 的 branch loss。

branch loss 建议由 rate 和 count 两部分组成：

```text
label_rate[c] = clamp(label_branch_miss[c] / branch_count[c], 0, 1)

L_branch_rate =
    BCEWithLogits(branch_logit[c], label_rate[c])

L_branch_count_log =
    Huber(log1p(pred_branch_miss[c]) - log1p(label_branch_miss[c]))

L_branch_miss =
    0.5 * L_branch_rate + 0.5 * L_branch_count_log
```

rate 项让模型学习“给定机会数下的错分概率”，count-log 项让绝对 miss 数不漂。

如果 branch miss 标签很稀疏，可以按 `log1p(branch_count)` 给 branch loss 加权，但不要
让大 branch_count 窗口完全主导 CPI loss。

### 9.6 Teacher-state 与 deployable-state 的 loss 一致

teacher-state、deployable-state 和 scheduled-mix 三个阶段使用同一套 loss。变化的只是
输入状态分布，不改变 label 或 loss 定义：

```text
input state: teacher / pred-driven / mixed
label:       true gem5 cpi_uop + true branch_miss
loss:        L_v27
```

这样可以直接比较不同 state 来源对 CPI 学习的影响。

## 10. 推理吞吐与实现成本

已有实测说明 shared_system C++ 本身不慢：

- 约 `0.5M - 0.7M mem events/s`
- 打开 per-op JSON 输出约 `0.4M mem events/s`
- Python JSONL 构造/排序通常比 C++ replay 更慢

首版方案 A 的部署开销：

```text
每窗口 1 次模型 forward
+ 每窗口 O(mem_ops) shared_system replay
+ 每窗口 O(mem_ops) pre-window line peek
```

对当前 v26 这类 GPU 模型，C++ replay 预计不是主瓶颈。需要避免的是：

- 每窗口同步 pipe 小批量 flush；
- 大量 JSON encode/decode；
- 为 per-UOP peek 复制完整 shared_system state。

建议实现时优先：

1. 离线预处理训练集；
2. eval 可先用 JSONL 验证；
3. 部署/大规模评测再改 in-process 或二进制 batch API。

## 11. 必要的 shared_system API

当前 shared_system 主路径能 replay event 并输出 snapshot/per-op 简单字段。v27 需要
新增或扩展以下接口：

### 11.1 Read-only peek

```text
peek_dside(core_id, paddr/cacheline, is_store, size)
    -> DSidePreState
```

只读返回窗口开始前状态，不修改 LRU/MESI/TLB/MSHR。

### 11.2 Snapshot summary

```text
snapshot_global_summary()
snapshot_core_summary(core_id)
```

返回 `ss_global_feats` 和 `ss_core_feats`。

### 11.3 Replay event

沿用当前 `step/probe` 语义，按部署预测顺序更新 shared_system。

### 11.4 Optional per-op sink 扩展

将当前 `--emit-per-op` 从：

```text
core_id, micro_seq, path_class, coh, cl
```

扩展为完整 D-side fields：

```text
mesi_before, sharer_bucket, owner_dist, dirty_owner,
path_class, coh_oracle, inval_fanout, same_line_recent,
d_mshr_depth, dtlb_hit, d_walker_levels, d_walker_dram_misses,
d_bank_id, d_llc_set_residency, d_llc_set_lru_pos,
ruby_l2_request, ruby_inval_targets, ruby_fwd_gets, ruby_fwd_getx
```

该接口主要用于 teacher 上限诊断和离线数据构造，不一定用于在线部署。

## 12. 评估矩阵

至少跑以下对照：

| 实验 | shared state 来源 | 目的 |
|---|---|---|
| v26 baseline | none | 当前基线 |
| v27 teacher-state | true commit_tick history | shared state 上限 |
| v27 deploy-lagged | pred-driven history | 部署一致收益 |
| v27 functional-order | round-robin/progress order | 不依赖 CPI 的排序下限 |
| v27 no-per-uop-ss | only core/global ss | 判断 per-UOP state 收益 |
| v27 no-core-ss | only per-UOP ss | 判断 core state 收益 |
| v27 branch-feats-only | no ss, add branch features | branch feature 单独收益 |

重点看：

- per-core CPI MAPE / p90
- pred/label core CV ratio
- per-window CPI correlation
- slowest-core top1/top2 命中率
- pred-driven rollout global CPI
- `W_false_sharing`, `W_ads_ranking_proxy`, `W_feed_ranking`,
  `W_fp_compute_dense`, `W_branch_storm`, `W_indirect`

## 13. 风险与红线

### 13.1 不要把真实 current-window oracle state 当输入

不能用真实 `commit_tick` replay 当前窗口，再把 per-op `path_class/coh_oracle` 喂给模型
作为部署输入。这会造成 label leakage。

### 13.2 不要让 cache PMU loss 继续主导训练

cache/TLB/coherence PMU 应交给 shared_system。模型继续预测这些 PMU 会把状态机问题
又塞回神经网络，并增加 loss 冲突。

### 13.3 shared_system 不是 gem5 真值

shared_system 是 deployable proxy。历史诊断显示它对部分 workload 会受到 committed
trace 缺少 speculative/wrong-path load 的影响。因此：

- `ss_*` 字段应命名为 proxy state；
- teacher-state 也不是完美 oracle；
- 最终判断仍以真实 CPI rollout eval 为准。

## 14. 当前结论

下一版优先采用：

1. lagged shared-system state；
2. `ss_core_feats + ss_global_feats` 必做；
3. per-memory-UOP `ss_line_pre_*` 建议做，但只读窗口开始前状态；
4. 模型 head 缩减为 `cpi_uop + branch_miss`；
5. cache/TLB/coherence PMU 退出模型训练目标；
6. branch miss 增加 functional branch predictor proxy；
7. 先跑 teacher-state 上限，再跑 deployable pred-driven state。

如果 teacher-state 明显改善 per-core CPI，而 deployable-state 改善有限，问题主要在
pred-driven 全局访存排序和 rollout state 分布；如果 teacher-state 也无改善，则说明
主要瓶颈不在 shared-system 状态，而在模型结构、CPI label 或切窗目标本身。
