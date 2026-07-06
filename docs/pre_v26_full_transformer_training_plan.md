# TSim v26 全量 Transformer 训练方案

> 注意：本文是讨论过程中形成的历史设计稿，保留用于追溯。当前收敛后的干净方案见
> `docs/v26_query_centric_kvqr_clean_plan.md`，后续实现应以该文件为准。

状态：设计稿。本文以当前 TSim 代码和 v25a 结果为准，`docs/2026.7.6LLMSim.md`
只作为 TAO/多核建模参考，不直接照搬。

## 1. 目标和边界

目标是训练一个从零开始、全参数可训练的 TSim Transformer，用多核 functional
trace 窗口预测每核 PMU，并服务部署侧动态切窗。

核心交付：

- 输入方案：重新定义适合当前多核窗口的结构化序列、UOP field embedding、
  core axis / role / local position 表达、连续 side feature 注入方式。
- 模型方案：不再依赖 Qwen/LoRA，也不保留 152k 自然语言词表；全量训练 compact
  Transformer。
- 输出指标：以 per-core `cpi_uop` 为主目标，同时覆盖 `branch_miss`、D/I cache
  miss、TLB miss、coherence proxy 和 MSHR 类指标。
- 动态切窗：训练分布必须覆盖部署侧 `OnlineQuotaPlanner` 的 soft-nmin tail-aligned
  行为，而不是只训练静态真值 TQ 窗口。
- 实验路线：保持单变量归因，先验证 loss/label，再验证 embedding，再放大 backbone，
  最后加入 planner-in-loop。

非目标：

- 不做逐指令 latency/fetch/execute 标签。当前 TSim 是窗口级 PMU 预测，训练标签来自
  window 聚合，不是 TAO 的逐指令详细追踪投影。
- 不把 `path_class`、`coh_oracle`、`dtlb_hit`、`mshr`、tick/latency 等微架构 oracle
  放进输入。它们只能进入 label 或诊断。
- 不以 branch/cache 的逐指令 BCE 作为主训练口径。TSim 部署输出是窗口计数/比率，
  应按窗口 opportunity denom 建模。

## 2. 当前状态

当前 TSim 已完成 v25a：8 层、320 宽、约 12M 可训练参数的自训 TinyTransformer。
它沿用 v22/v25a 的数据和 head：

- 数据 cache：`data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache`
- cache manifest：`max_len=32768`，`max_cores=32`，`side_feat_dim=34`
- 生成侧 PMU key：`cpi_uop, branch_miss, l1d_ld_miss, l1d_st_miss,
  l2_ld_miss, l2_st_miss, llc_miss, dtlb_miss`
- 模型侧 PMU key：当前 `model/regression_head.py` 只训练 7 维，丢掉 `dtlb_miss`
- 输入编码：1 个 `<UOP>` position + 6 个 functional field embedding
  (`opclass, reg, memkind, reuse-distance, stride, branch`)
- 每核 segment：旧范式用 `<C{i}_BEGIN>/<C{i}_END>/<LOCAL_C{i}>/<QUERY_C{i}>`
  special tokens 标记边界和查询；v26 应优先保留结构化 core 轴
  `[B, n_core, n_elem, d]`，核心划分由张量维度和 mask 自然给出，加显式
  per-core query vector
- 连续特征：34 维 `side_feats` 在 query hidden 后线性注入，当前 v25a 训练脚本默认
  未启用 `t_start_rel`
- loss：per-core log-CPI、window sum cycles、aux PMU logcount、centered CPI；
  rank/spread 参数保留但实际禁用
- 部署 planner：`OnlineQuotaPlanner` 根据上一窗预测 CPI 和预测 start cycle 做
  soft-nmin tail-aligned 配额，预算不足时进入 `catch_up_floor`

v25a seedB full eval 显示：c04/c08 已可用，但 c16 的 `W_phased_mix` 和
`W_ads_ranking_proxy` 是明显 outlier。这说明下一步问题不只是 backbone 参数量，
还包括 phase/dynamic-window 分布、跨核状态表示和辅助 PMU 口径。

## 3. 参考文档的取舍

| 参考文档内容 | TSim v26 取舍 |
| --- | --- |
| 每条指令输入 opcode/register/access distance/branch history | 保留“functional-only”和“历史/局部性特征”的思想；不使用 TAO 的 N+1 ROB 固定序列，继续按多核 window 建模 |
| M 个核心各自一段序列 | 保留 per-core segment，但窗口长度按 planner/预算动态变化，不固定 N |
| Cross-Core Attention 显式查询其他核 | 采用其核心思想，但按 TSim window 改造成显式 KVQR cross-core block：普通 `Q` 负责同核/全局上下文，独立 `R` 专门查询其他核的 `K/V`，作为跨核资源竞争的归纳偏置 |
| 逐指令 fetch/exec/branch 分类标签 | 不采用。TSim 标签是窗口级 per-core PMU；branch/cache 用 opportunity-bounded count/rate head |
| CPI 由逐指令 fetch+exec 汇总 | 不采用。TSim 主标签是窗口 `cpi_uop = cycles / uops`，部署也用 `cpi_uop * planned_uops` 更新 per-core tail |
| 输入阶段不构造跨核特征，完全交给 attention | 不采用。当前项目已经有 functional cross-core side features；v26 应继续强化这些特征，而不是删除 |

## 4. 标签和预测指标

v26 先统一一个完整 PMU schema，避免现在 `config/pmu_keys.yaml`、`data/build_windows.py`
和 `model/regression_head.py` 三处口径漂移。

推荐 `PMU_KEYS_FULL`：

| key | 空间 | opportunity denom | 说明 |
| --- | --- | --- | --- |
| `cpi_uop` | logratio | `uops` | 主目标；`cycles_pred = cpi_uop_pred * uops` |
| `branch_miss` | count + rate | `branch_count` | 分支 miss 绝对数，rate 头约束到 `[0, branch_count]` |
| `l1d_ld_miss` | count + rate | `loads` | L1D load miss |
| `l1d_st_miss` | count + rate | `stores + atomics` | L1D store/atomic miss |
| `l2_ld_miss` | count + rate | `loads` | L2 load miss |
| `l2_st_miss` | count + rate | `stores + atomics` | L2 store/atomic miss |
| `l1i_miss` | count + rate | `fetch_groups` | I-side miss，当前 aggregate 已能算，但需加入生成侧 PMU_KEYS |
| `llc_miss` | count + rate | `mem_ops` | DRAM/LLC miss |
| `dtlb_miss` | count + rate | `mem_ops` | 当前生成侧有，模型侧需要补回 |
| `itlb_miss` | count + rate | `fetch_groups` | 当前 aggregate 已能算，生成侧需补入 |
| `inv_recv` | count + rate | `mem_ops` | coherence remote proxy，作为 false sharing/多写者诊断目标 |
| `mshr_avg` | direct | `mem_ops` | direct regression，低权重辅助目标 |

`cpi_macro` 不作为训练头，继续由 `cycles / instr_retired` 派生，用于和 gem5/stats
口径对照。

## 5. 特征工程

### 5.1 UOP field embedding v2

当前 6 字段是好的起点，但对 branch miss、I-side miss 和跨核 cache/coherence 不够。
v26 建议扩展为 10 个字段，每个字段独立 embedding 后 concat + MLP：

| 字段 | 来源 | 作用 |
| --- | --- | --- |
| `opclass` | `op_class` / fallback flags | 保留当前指令类型信息 |
| `reg_bucket` | `n_src/n_dst/producer_classes` hash | 保留依赖结构 |
| `memkind` | load/store/atomic/fence/none | 区分访存机会 |
| `rd_bucket` | same-core bounded reuse distance | cache 局部性 |
| `stride_bucket` | same-core cacheline stride | stream/random 访问 |
| `branch_bucket` | taken/cond/indirect/target-delta | 保留当前 branch 类型 |
| `pc_bucket` | macro_pc/micro_pc hash，低位和页内 bucket | branch predictor、I-cache、循环结构；只用 functional PC |
| `branch_hist_bucket` | per-core branch history folded hash | 专门服务 `branch_miss`，无 branch 时为 none |
| `xcore_mem_bucket` | window 内 line 被多少核访问/写、是否 multi-writer | 跨核共享、false sharing、coherence proxy |
| `macro_pos_bucket` | macro head/last microop、micro-op index bucket | macro 展开长度和 fetch group 机会 |

`xcore_mem_bucket` 只能由 functional address stream 构造，例如：

- `shared_core_count`: 1/2/3-4/5-8/9+
- `writer_core_count`: 0/1/2/3+
- access role: load/store/atomic
- line 是否有跨核 store-owner switch
- store byte slot 是否与其他 core overlap/disjoint

这些都不能读取 `path_class` 或 `coh_oracle`。

### 5.2 Compact indexer vs direct embedding

v25a 为了和 Qwen 对照，仍使用 HF tokenizer + 新 special token，并通过 hook 冻结旧
152k embedding 行。v26 不需要 HF 文本 tokenizer，也不应该再保留自然语言词表。

这里要区分两个概念：

- `tokenizer/indexer`：把离散符号映射成整数 id，并定义序列布局、cache manifest
  和兼容检查。文本任务里它会做 BPE/分词；TSim 里它只应是轻量的 schema/indexer。
- `embedding`：模型参数层，把整数 id 或结构化字段映射成向量。

因此 v26 的推荐不是“继续 tokenizer 还是直接 embedding”二选一，而是：

- 不再使用 `<C0_BEGIN>` 这类文本式边界 token。默认保持 `[B, n_core, n_elem, ...]`
  张量形状，core 边界由 core 轴和 padding mask 表达；query/local summary/side
  memory 位置由 role 轴或 `role_id` 表达。
- 配置档位、summary bucket 这类确实离散的符号，用轻量 `FeatureIndexer` 映射 id，
  再走 `nn.Embedding`。这里的 indexer 是 schema 映射，不是 LLM tokenizer。
- UOP 主体不要先拼成字符串 token；直接把 `opclass/reg/memkind/rd/stride/branch/...`
  等字段张量送入多路 `nn.Embedding`，concat 后 MLP 融合。
- 连续 side feature、planner state 直接走标准化 + `nn.Linear`，不离散 token 化。
- Dataset cache 必须重建；manifest 记录 `indexer_type=tsim_compact`、字段 schema、
  `feature_version=v26_full` 和各字段 vocab size。

收益：

- 去掉无用 152k 自然语言 embedding，减少显存和 optimizer 状态。
- embedding 语义完全由 TSim feature 决定，不再受 Qwen 兼容逻辑牵制。
- 后续可稳定加入新字段，不需要处理旧 special token 连续 id 假设。

### 5.3 Core axis、role 和 position

当前全局 RoPE 使用单一序列位置，core 顺序会隐式影响 attention 距离。v26 应优先
用结构化 core 轴建模，而不是依赖绝对 core-id token：

- `role_embedding`: cfg/global/core_summary/uop/local_memory/query/system_query/pad
- `core_axis`: 主张量形状保留 `[B, n_core, n_elem, d]`；same-core/cross-core mask
  直接由 core 轴生成
- `core_slot_embedding`: 默认不加绝对 `C0..C31` embedding，除非物理 core id、cache
  slice、NUMA/拓扑位置确实会影响标签；同构 core 下绝对 id 可能破坏置换等变性
- `core_order_permutation`: 如果最后为了实现效率 flatten 成 `[B, seq, d]`，训练时随机打乱
  core 顺序并同步 label/query，降低对排列顺序的过拟合
- `position_ids`: 每个 core 内局部位置从 0 重置；cfg/global/query 使用独立位置域
- per-core query 不再是 `<QUERY_Ci>` 字符串 token，而是一个 learned query base vector
  加 `role_embedding(query) + planner/state projection`；只有启用拓扑/物理 core 建模时
  再额外加 core/topology embedding

RoPE 继续保留，但使用显式 `position_ids`，不再在 attention 内直接 `arange(seq_len)`。

### 5.4 Side/global/planner features

这三类特征不能混为一谈：

- `side_feats`: 每核 window 内 functional summary，部署时可由 trace 直接计算。
- `global_feats`: 整个 window / 配置级 functional summary，例如 n_core、cfg bucket、
  全局 distinct lines/pages、共享写压力。
- `planner_feats`: 动态切窗器的运行状态，只在 free-running/replay 阶段由上一窗预测产生。

`side_feats/global_feats` 不是强行移植。它们是低成本 functional 聚合，解决 query attention
难以稳定统计 count/rate 的问题。例如 branch/load/store opportunity、shared-store rate
这类量对 `branch_miss/cache_miss/CPI` head 都是直接有用的。当前 34 维 `side_feats`
已覆盖很多关键多核 functional proxy：

- active cores、uops/instr count
- branch/load/store/mem opportunity count
- global/core distinct lines/pages
- shared store/load、multi-writer、owner switch、pairwise pressure
- random/large-stride pressure

v26 不应删除这些特征，但应分层使用：

1. 默认输入保留最小 side/global 集：`uops_core`、branch/load/store/mem/fetch
   opportunity、active cores、global/core distinct lines/pages、shared/multi-writer pressure。
2. `side_proj_query`: 加到每核 query hidden，保证 head 能直接使用这些统计量。
3. `side_memory`: 可选。若实测 query 只在 head 前看到 side 信息不足，再构造连续
   memory vector 供 attention 使用。

连续特征需要 train-set mean/std 或 robust scale，写入 cache manifest。比例类保持
`[0,1]`，count 类使用 `log1p` 后标准化。

### 5.5 Query-centric core representation

v26 不应默认引入独立 `local_encoder`。更干净的首版是 query-centric KVQR：保留
每核 UOP element embeddings，直接让每核 learned query 读取本核 UOP 和其他核 UOP/summary。
core memory 可以作为低成本优化，但不是必需前置层。

```text
UOP element embeddings: [B, C, L, D]   # 每核一段程序序 UOP 表示
per-core query vectors: [B, C, D]      # 每核预测 query
optional core memory:   [B, C, M, D]   # 可选的 summary/memory 表示
```

默认路径：

```text
uop_emb_i,t = MLP(concat(
  Emb(opclass), Emb(reg_bucket), Emb(memkind), Emb(rd_bucket),
  Emb(stride_bucket), Emb(branch_bucket), Emb(pc_bucket),
  Emb(branch_hist_bucket), Emb(xcore_mem_bucket), Emb(macro_pos_bucket)
))

E_i = uop_emb_i + role_emb(uop) + local_position_encoding

q_i = learned_query_base
    + role_emb(query)
    + side_proj(side_feats_i)
    + global_proj(global_feats)
    # planner_state_proj(planner_state_i) only in replay/fine-tune stage

local_ctx_i = Attn(q_i W_q, E_i W_k, E_i W_v)
cross_ctx_i = Attn(q_i W_r, E_{j!=i} W_k, E_{j!=i} W_v)
head_input_i = Fuse(q_i, local_ctx_i, cross_ctx_i)
```

这个版本没有单独的 local encoder；本核顺序信息由 `local_position_encoding` 和
query-to-UOP attention 学习。只有当 query-centric 版本对局部依赖、branch pattern 或
load-use chain 明显欠拟合时，再把 local encoder 作为 v26b/c 的 ablation。

可选 core memory 路径：

```text
core_memory_i = AttnPool(E_i) + side_proj(side_feats_i) + summary_bucket_proj(summary_i)
```

它只用于把 `cross_ctx_i` 的 K/V 从全 UOP 缩成少量 memory slots，例如 c32 长窗显存
或 latency 不可接受时使用。首版可以先不用，直接让 `R` 查询其他核 UOP elements。

### 5.6 Planner-state feature

`t_start_rel` 不能继续直接使用真值 commit start 作为训练输入，否则和部署侧 predicted
state 不一致。v26 分两阶段处理：

- 静态 TQ 预训练：不输入 `t_start_rel`，或只把 true start/end 放在 label/meta 供
  tail loss 和诊断使用。
- planner-in-loop 微调：输入由 planner replay 产生的 `pred_start_rel`、`planned_uops`,
  `nmin_eff`, `planner_mode`, `tail_skew_pred`。这些来自上一窗预测和 functional cursor，
  部署时真实可得。

## 6. 模型结构

### 6.1 默认 v26c-full32M

第一版全量模型建议用 30M 级，而不是直接跳到很大：

```text
d_model      = 512
n_layers     = 10
n_heads      = 8
head_dim     = 64
ffn_dim      = 2048
dropout      = 0.10
attn_dropout = 0.05
max_len      = 32768
norm         = pre-RMSNorm
ffn_act      = GELU
position     = RoPE with explicit per-element position_ids
attention    = bidirectional encoder attention
```

选择 bidirectional encoder attention 的理由：窗口内所有 functional UOP 在预测前已经
可见，TSim 不是语言生成任务。causal mask 只是 Qwen 兼容遗留约束，会让 segment 中
早期 UOP 的 hidden 看不到后续 UOP。v26 可以保留一个 causal ablation，但默认应使用
non-causal padding mask。

参数量粗估：每层 attention 约 `4*d^2 = 1.05M`，FFN 约 `2*d*4d = 2.10M`，
10 层约 31.5M；加 compact embedding、UOP encoder 和 head 后约 35M。

### 6.2 KVQR cross-core block

需要显式保留 KVQR 里的 `R`。原因是普通 self-attention 只有一个 query 投影 `Q`，
它同时承担“看本核局部上下文、看 cfg/global、看其他核竞争状态”三件事；对多核 PMU
来说，这会把跨核干扰学习成一个弱归纳偏置。`R` 是专门面向跨核交互的 query 投影，
让模型用另一套子空间去问“其他核有没有会影响本核 CPI/miss 的行为”。

每层建议拆成两路：

```text
Q_i = H_i W_q       # 普通上下文 query
R_i = H_i W_r       # cross-core query，独立参数
K_i = H_i W_k
V_i = H_i W_v

self_i  = Attn(Q_i, K_{same-core + cfg + global}, V_{same-core + cfg + global})
cross_i = Attn(R_i, K_{other-core + global},      V_{other-core + global})
out_i   = self_i + gate_i * cross_i
```

实现上有三个粒度，默认选 query-centric：

- query-centric cross：每核 1 个或少量 prediction query 用 `R` 查询其他 core 的
  UOP/summary/side elements。首版默认采用。
- full element-level cross：每个 UOP element 都用 `R` 查询其他 core 的 UOP elements。
  表达力最强，但 c32/32k 成本高。
- summary-mediated cross：每个 core 先汇聚少量 `CORE_SUM/LOCAL/SIDE` memory vectors，
  `R` 主要查询其他 core 的这些 memory vectors。它是 latency/显存优化路径，不是首版必需。

这里要区分两种成本：

```text
query-to-all-UOP:
  每核只有 1 个或少量 query 去看所有 UOP
  复杂度约 O(C * C * L * D)

full UOP-to-UOP cross:
  每核每个 UOP 都去看其他核每个 UOP
  复杂度约 O(C * C * L * L * D)
```

如果 `C=32, L=256, D=512`：

```text
query-to-all-UOP attention scores 约 32 * 31 * 256 = 254k
full UOP-to-UOP attention scores 约 32 * 31 * 256 * 256 = 65M
```

前者通常可以承担；后者每层每头都要产生大 attention 矩阵，训练显存和 latency 都会明显上升。
因此 v26 首版应优先做 query-centric KVQR：每核 query 的 `R` 直接查询其他核 UOP
elements。只有当 C/L 更大或实测延迟不可接受时，再引入 core memory 压缩。

`R` 不应该替代当前 functional cross-core side features。side features 是显式统计先验，
`R` 是学习到的跨核注意力通道；两者互补。对于 `false_sharing`、`phased_mix`、c16/c32
尾部漂移这类问题，`R` 应作为默认架构组件，而不是后续可选项。

### 6.3 兼容调试配置

为了归因干净，保留 12M 配置作为 debug/backbone-control：

```text
d_model=320, n_layers=8, n_heads=8, ffn_dim=1280
```

实验顺序中先在 12M 上验证 label/loss 和 embedding，再放大到 32M。

### 6.4 Head 设计

保持“per-core query hidden -> shared head”的基本形态，但扩展为 typed heads：

- `cpi_head`: 输出 `log(cpi_uop)`。
- `rate_heads`: 对有 denom 的 count 指标输出 miss/event rate logit。
- `count_calibration_heads`: 输出 `log1p(count)` residual 或 calibration，用于小 denom
  和稀有事件。
- `direct_head`: 输出 `mshr_avg` 等 direct 指标。
- 可选 `system_query_head`: 追加 learned system query vector 预测聚合 CPI/total cycles，作为辅助
  校准，不替代 per-core head。

所有 heads 在 core 之间共享参数。核心状态主要由 core 轴上的 UOP 序列、side memory、
planner state 和 KVQR cross-core context 区分；默认不依赖绝对 core-id embedding。

## 7. Loss

推荐 v26 loss：

```text
L =
  1.00 * L_cpi_abs
+ 0.50 * L_cpi_centered
+ 0.10 * L_cpi_rank
+ 0.50 * L_cycles_sum
+ 0.50 * L_tail_time
+ 0.05 * L_count_log
+ 0.05 * L_rate
+ 0.02 * L_physical_bounds
+ 0.02 * L_system_query
```

说明：

- `L_cpi_abs`: per-core Huber on `log(cpi_uop)`，`delta=0.05` 起步。v22 的
  `delta=0.1` 对主目标偏宽。
- `L_cpi_centered`: 对同窗 active core 去均值后的 log-CPI residual 监督，保留
  v22 经验，但权重可提高到 0.5。
- `L_cpi_rank`: 只在 label core gap 足够大时启用，pairwise sign/rank loss，防止
  high-spread 窗口向均值收缩。
- `L_cycles_sum`: 当前 `sum_i cpi_i * uops_i` 的 aggregate cycles 监督继续保留，但
  不能单独依赖它，因为 core 间误差会抵消。
- `L_tail_time`: 用 label/meta 中 true `t_start_rel/t_end_rel` 监督
  `t_start_rel + cpi_pred * uops` 的尾部时间，重点服务 dynamic planner。静态阶段
  若不输入 start，可用 true start 只参与 loss；planner-in-loop 阶段用 predicted
  start 输入和对应 true tail 监督。
- `L_count_log`: count 指标的 `Huber(log1p(pred_count), log1p(label_count))`。
- `L_rate`: 对有 opportunity denom 的指标，监督 `label_count / denom`。denom=0 的
  样本 mask 掉 rate loss；count loss 仍要求接近 0。
- `L_physical_bounds`: 惩罚 `pred_count < 0`、`pred_count > denom`、rate 越界等。
- `L_system_query`: 若启用 system query，监督全窗口 aggregate CPI/cycles。

count/rate 预测建议：

```text
rate = sigmoid(rate_logit)
bounded_count = rate * denom
pred_count = bounded_count + softplus(count_residual) * small_scale
```

第一版可先不用 residual，直接 `pred_count=rate*denom`，把绝对 count 校准留给
下一版。这样 branch/cache/TLB 的物理边界最清楚。

## 8. 动态切窗训练

### 8.1 静态 TQ 预训练

继续使用 TQ tail-aligned 训练窗口，但做三项改动：

- 默认保留每核 `local_memory` anchor，但不再通过 `<LOCAL_Ci>/<QUERY_Ci>` token
  位置表达，而是在结构化输入中为每个 core 构造 local summary/side memory 和
  learned query vector。
- `tq_min_uops_per_core` 做 jitter：`128/256/384/512` 混合，覆盖部署侧不同证据量。
- 训练数据混合 c04/c08/c16/c32，并对 `W_phased_mix`、`W_ads_ranking_proxy`、
  `W_branch_storm`、`W_false_sharing` 做有限 oversampling。

静态 TQ 的 window 边界仍由真值 commit tick 构造，这是训练标签生成阶段允许的；
但不要把 true `t_start_rel` 当作输入。

### 8.2 Planner replay 微调

v25a 的部署误差来自 free-running planner 和模型误差互相放大，尤其是 phase 切换。
因此 v26 必须增加 planner-in-loop 数据：

1. 用 v26 静态模型在训练 raw traces 上跑 `eval_quota_cycles.py`，`planner_state_source=pred`。
2. dump 每个实际 free-running window 的 structured sequence、functional features、planned counts、
   predicted start state、planner stats 和真实 PMU label。
3. 用这些 replay windows 微调模型，并启用 planner-state feature。
4. 每轮只刷新一次 replay cache，避免训练过程过重；推荐 `static -> replay1 -> replay2`
   两轮。

同时保留 `planner_state_source=label` 和 `tq_forward` eval 作为诊断，不作为最终部署
指标替代。

### 8.3 Fit retry 和 c32

部署侧在 max_len 不足时会降低 `nmin_eff` 或重试缩窗。训练必须见过这些模式：

- 在 replay cache 记录 `planner_mode=aligned_floor/catch_up_floor/cold_start`。
- 对 `catch_up_floor` 窗口提高采样权重，因为它们最容易造成尾部漂移。
- c32 若 32768 放不下默认 nmin，则训练中显式加入低 nmin/floor 的 c32 窗口，不要
  只靠 eval 临时缩窗。

## 9. 训练配置

默认 full32M：

```text
optimizer      = AdamW
betas          = (0.9, 0.95)
weight_decay   = 0.05
lr_backbone    = 2e-4
lr_embedding   = 2e-4
lr_head        = 3e-4
warmup_steps   = 1000
schedule       = cosine, min_lr_ratio=0.1
grad_clip      = 1.0
precision      = bf16
max_len        = 32768
dropout        = 0.10
steps_static   = 12000
steps_replay   = 4000 per replay round
```

batch 建议：

- 先用 12M/debug：global batch 64，对齐 v25a。
- full32M：从 `bs=2~4 per GPU` 起步，用 grad accumulation 保持 global batch
  32 或 64。
- 若 non-causal 32k attention 显存过高，优先降 batch，不先降 max_len；dynamic
  planner 的训练/部署上下文必须一致。

## 10. 实验顺序

为了避免把 backbone、loss、feature 和 planner 改动混在一起，建议按下面顺序：

| 版本 | 改动 | 目的 |
| --- | --- | --- |
| v25a-repro | 现有 12M + 7 维 PMU + v22 loss | 锚定当前结果 |
| v26a-label-loss | 12M，补全 PMU schema，换 count/rate + tail/rank loss，embedding 不变 | 验证目标函数和 label 口径 |
| v26b-embed | 12M，compact indexer + UOP field v2 + side memory + core axis / role / local position | 验证特征工程 |
| v26c-full32M | 放大到 32M，non-causal full-window encoder | 验证全量 backbone |
| v26d-replay | 在 v26c 基础上 planner replay 微调，启用 planner-state feature | 验证动态切窗闭环 |

每一版都跑同一套 seedB c04/c08/c16/c32 full eval。若某版失败，停止叠加下一项，
先做诊断。

## 11. 评估和验收

主指标：

- `pred_vs_roi_cpi_uop`: global、mean/median/p90/max workload pVr
- per-core CPI：log-MAE、Pearson、pred/label std ratio、high-spread 窗口 rank accuracy
- dynamic planner：tail skew、`nmin_eff` 分布、`catch_up_floor` 占比、free-running
  drift、`pred` vs `label` planner gap
- branch/cache/TLB：aggregate count relative error、rate calibration、non-zero window recall
- physical bounds：count 越界率必须接近 0

最低验收门槛：

- c04/c08 不退化超过 v25a 1pp mean workload pVr。
- c16 `W_phased_mix`、`W_ads_ranking_proxy` 相比 v25a 明显下降；否则说明 planner
  replay/phase 特征没有解决核心风险。
- c32 能完成 full eval，且不依赖临时改 max_len 或关闭 workload。
- 辅助 PMU 在 aggregate 口径有可解释相关性，不能只靠 CPI 过关。

诊断必须保留：

- `planner_state_source=pred/label/tq_forward` 三种对照。
- hidden geometry：query/local/head_input 的 core 间 cosine、std、CPI correlation。
- 数据口径 audit：manifest 中 PMU keys、side feature dim、feature version、indexer
  type 必须和代码一致。

## 12. 需要修改的模块

核心改动：

- `model/tokenizer.py`: 可改名为 `model/features.py`；负责 compact indexer、UOP field v2 bucket、role/local-position schema 定义。
- `data/build_windows.py`: `PMU_KEYS_FULL`、新增 functional branch history/xcore mem
  annotation、side feature 标准化统计、TQ jitter。
- `train/dataset.py`: manifest schema 升级，选择 full PMU columns，加载 side normalization，
  支持 planner replay fields。
- `model/tiny_transformer.py`: 支持 non-causal attention、explicit `position_ids`、compact
  embedding 保存/加载。
- `model/llm_wrapper.py`: UopEncoder v2、side memory 注入、planner-state feature 注入、
  expanded heads。
- `model/regression_head.py`: full PMU schema、rate/count/direct typed heads。
- `train/loss.py`: v26 loss，尤其是 rate/count、tail time、rank 和 physical bounds。
- `eval/eval_quota_cycles.py`: dump planner replay cache；加载 compact indexer/schema checkpoint。
- `scripts/run_v26*_*.sh`: 分阶段训练和 seedB full eval 脚本。

## 13. 风险和对策

- 显存风险：32k non-causal attention 是主要瓶颈。先保 max_len，降 batch/加
  grad accumulation；必要时再设计 local-core + cross-core summary，而不是直接缩短
  eval context。
- 标签口径风险：当前代码三处 PMU key 不一致。v26 第一项工作必须是 schema audit，
  否则训练结果不可解释。
- planner-state 泄漏风险：true `t_start_rel` 只能用于 loss/meta，不能作为静态训练
  输入。输入必须来自 replay 的 predicted planner state。
- 辅助 PMU 噪声：count 指标小分母窗口多，不能只看 per-window MAPE；以 aggregate
  relative error 和 rate calibration 为主。
- 归因污染：不要在同一实验里同时换 loss、embedding、backbone size 和 planner replay。
  按 v26a/v26b/v26c/v26d 顺序推进。

## 14. 结论

v26 不应直接复刻 TAO 的“每核 N+1 指令 + 显式 cross-core attention + 逐指令标签”。
TSim 的真实任务是：给定动态切出来的多核 functional window，预测窗口级 per-core
CPI 和 PMU，并让下一窗 planner 稳定推进。

因此，全量 Transformer 的关键不是只把 v25a 放大，而是同时完成四件事：

1. 统一 full PMU schema，补齐模型侧遗漏指标。
2. 把多核 functional 特征做成 first-class embedding，而不是只在 head 前加 side
   projection。
3. 用适合窗口回归的 non-causal compact Transformer 全参数训练。
4. 用 planner replay 覆盖部署侧 free-running 动态切窗分布。

按上述顺序做，才能判断“全量 Transformer”本身的收益，而不是把动态切窗、标签口径
和特征缺口混成一个不可解释的大改动。
