# TSim v26 Full Q/K/V/R 方案

状态：当前实现方案。本文替代早期 query-centric KVQR 首版设想，按
`docs/2026.7.6LLMSim.md` 的多核 Cross-Core Attention 思想落地，但仍保持 TSim
任务定义：预测窗口级 per-core `cpi_uop` 和 PMU count/rate，不预测逐指令
fetch/execute cycles。

## 1. 目标

当前 v26 需要验证的问题是：在结构化 `[core, uop]` 输入上，让每个 UOP 先经过
本核 self-attention 和跨核 R-attention 上下文化，是否能修复 query-readout 版本
对普通 workload 的系统性低估。

模型输入：

```text
uop_fields:   [B, C, L, F]
uop_mask:     [B, C, L]
core_mask:    [B, C]
side_feats:   [B, C, S]   # per-core summary / pressure features
global_feats: [B, G]      # window-level aggregate features
```

输出：

```text
pred: [B, C, K]
K = 8 = cpi_uop + branch/cache/TLB PMU
```

其中 `C` 是 batch 内 padding 后 core 数，`L` 是 batch 内 padding 后每核 UOP 长度。
真实有效 core/UOP 由 mask 决定。

## 2. UOP Embedding

当前实现使用 clean v26_14 UOP schema。前 6 个字段沿用 v9/v25a：

```text
opclass, reg_bucket, memkind, rd_bucket, stride_bucket, branch_bucket
```

新增 8 个 functional-state proxy 字段：

```text
pc_bucket
macro_pos_bucket
line_hash_bucket
line_role_bucket
same_core_hist_bucket
xcore_mem_bucket
coherence_bucket
fanout_bucket
```

这些字段全部来自 functional trace 可在线重放的信息，不使用 gem5 cache miss、
commit tick、最终 CPI 或其它标签侧信息：

```text
pc_bucket:
  macro_pc/micro_pc 的 hash bucket，用于区分静态指令/代码路径。

macro_pos_bucket:
  single/first/middle/last，表示 macro 指令内 micro-op 位置。

line_hash_bucket:
  虚拟 cacheline hash，用于让模型区分同一窗口内的 line 身份。

line_role_bucket:
  nonmem/private/shared/multiwriter/hot/remote 等窗口前缀内 line 角色。

same_core_hist_bucket:
  同核 reuse-distance/近期见过该 line 的离散桶。

xcore_mem_bucket:
  last-writer/reader 是否来自其他 core，是否 remote-store 后 load/store。

coherence_bucket:
  private/shared load、store invalidating readers、owner transfer、ping-pong store
  等 proxy coherence 状态。

fanout_bucket:
  同一 line 上其它 core 读写参与度的 log bucket。
```

每个字段独立 embedding，concat 后经 MLP 得到：

```text
E: [B, C, L, D]
```

然后加入每核内部位置编码：

```text
H0[b,c,t] = UopEncoder(fields[b,c,t])
          + local_position_embedding[t]
          + role_uop
```

这些字段被嵌入在每个 UOP 上，而不是只作为 core-level 特征。原因是 cacheline、
PC、reuse/coherence proxy 都是 UOP 局部属性；如果只压成 core-level summary，
模型仍然不知道“哪个 load/store”处在 remote/shared/multiwriter 状态。

core/window 级别的 side/global 特征仍保留，用于给 head 和全局条件提供：

```text
n_core, total_uops, per-core uops share,
shared_store_rate, multi_writer_line_frac, pairwise_writer_pressure,
store_owner_switch_rate, aggregate_mem_density, random_access_pressure, ...
```

### 2.1 数据重建要求

v26_14 不能从旧 v16/v25a tensor cache 直接恢复。旧 cache 已经把
`uop_fields_flat` 固定保存为 6 列，新增 8 个字段的信息已经丢失。因此必须从
functional trace 重新 build windows，并生成新的 tensor cache：

```bash
cd /data00/yinhaolang/TSim
bash scripts/build_v26_clean14_tail_local_train600.sh
```

该脚本沿用 v16 tail-local train600 的 workload、core set 和窗口参数；关键新增
参数是 `data/build_windows.py --uop-field-schema v26_14`，windows 阶段显式写入
8-key label schema：

```text
cpi_uop, branch_miss, l1d_ld_miss, l1d_st_miss,
l2_ld_miss, l2_st_miss, llc_miss, dtlb_miss
```

构建后旧的
`windows.maxlen32768.tensor_cache` 不能复用。v26 cache 应使用 direct structured
模式重建，不再经过 HF tokenizer 或 `<C*_BEGIN>/<QUERY_C*>` special token：

```bash
/data00/yinhaolang/infer/.venv/bin/python scripts/prepare_dataset_cache.py \
  --data data/windows_v26_clean14_tail_local_all/windows.jsonl \
  --cache-out data/windows_v26_clean14_tail_local_all/windows.maxlen32768.tensor_cache \
  --max-len 32768 \
  --label-keys cpi_uop,branch_miss,l1d_ld_miss,l1d_st_miss,l2_ld_miss,l2_st_miss,llc_miss,dtlb_miss
```

direct cache 只依赖 `uop_fields/core_split/side_feats/denoms/label`。
`scripts/prepare_dataset_cache.py` 现在是 v26-only：只构建 structured tensor cache，
不再接受历史 token cache 相关参数。

## 3. Full Q/K/V/R Block

每一层对每个 UOP 位置都计算四组投影：

```text
Q = H Wq     # 本核 self-attention query
K = H Wk     # 本核和跨核共享 key
V = H Wv     # 本核和跨核共享 value
R = H Wr     # 跨核 attention query
```

### 3.1 本核 Self-Attention

每个 core 内独立做 UOP-UOP attention：

```text
SelfAttn[b,c] =
  softmax(Q[b,c] K[b,c]^T / sqrt(dk)) V[b,c]
```

复杂度：

```text
O(C * L^2)
```

这一步负责让每个 UOP 表示包含本核局部上下文，例如 stride phase、load burst、
branch pattern 和同核 reuse distance 组合。

### 3.2 跨核 R-Attention

对目标 core `c`，每个 UOP 用 `R[b,c]` 查询其他 core 的 UOP `K/V`：

```text
K_other = concat(K[b,j]) for j != c
V_other = concat(V[b,j]) for j != c

CrossAttn[b,c] =
  softmax(R[b,c] K_other^T / sqrt(dk)) V_other
```

复杂度：

```text
O(C * (C - 1) * L^2)
```

`R` 与 `Q` 独立，目的是把“本核局部模式读取”和“跨核干扰读取”分到不同子空间，
避免共享一个 query projection。

### 3.3 融合和 FFN

本层输出：

```text
H = H + Wo(SelfAttn + CrossAttn)
H = H + FFN(LayerNorm(H))
```

多层堆叠：

```text
H_L = QKVRBlock(...QKVRBlock(H0)...)
```

当前默认使用 8 层同宽 v26 transformer profile：

```text
D = 320
heads = 8
layers = 8
ffn_dim = 1280
attention_impl = ragged_sdpa
amp_dtype = bf16
sdpa_backend = no_flash
```

这个 profile 不再为了维持 8 层 profile 的整体参数量而加宽 `D`。它使用
`D=320, ffn_dim=1280` 的旧 v26 单层规模，并恢复 8 层深度，优先补回
UOP 间多层 contextualization；不采用 4 层加宽方案。

v26 QKVR block 参数量：

```text
P_block = 5 * D^2 + 2 * D * ffn_dim + ffn_dim + 5 * D
        = 1.334080M  # D=320, ffn_dim=1280
```

v26 8 层 blocks 总参数：

```text
P_blocks = 8 * 1.334080M = 10.672640M
```

注意：`pos_emb = max_uops_per_core * D = 32768 * 320 = 10.49M`。在当前模型中，
位置表仍是参数量的主要来源之一，但不是主要激活显存来源。

在该 profile 下，v26 主要参数量约为：

```text
QKVR blocks     10.67M
pos_emb         10.49M  # 32768 * 320
uop_encoder      0.67M
core/global MLP  0.32M
PMU head         0.20M
```

实现上使用 packed 主干：模型入口接收 v26 direct structured collate 产生的 dense
`[B,C,Lmax,F]` 输入，但 `V26KVQRModel.forward` 会立刻根据
`uop_mask/core_mask` pack 成真实 UOP token 列表 `[N_real,D]`。之后
UOP encoder、position embedding、Q/R/K/V projection、attention、FFN 和 pooling
都只对真实 token 计算。

每个 active core 对应一个 segment：

```text
(sample_id, core_id, start, end)
```

position 使用 per-core 局部位置 `pos_in_core`，不会因为其他 core 的长度不同产生
flatten 后的位置偏移。每层只对真实 segment 调用
`torch.nn.functional.scaled_dot_product_attention`：

```text
local core i:
  Q_i[0:L_i] attend K_i/V_i[0:L_i]

cross target core i:
  R_i[0:L_i] attend concat(K_j/V_j[0:L_j]) for j != i
```

这些 SDPA 调用不带 padding mask。默认 `sdpa_backend=auto`，CUDA 上由 PyTorch
按 dtype/head_dim/shape 自动选择 cuDNN Flash、FlashAttention、
memory-efficient 或 math SDPA。由于当前 full QKVR 是 ragged 分段 attention，
大量小/中等 segment 可能让 flash kernel 的启动和调度开销抵消收益，因此实现
提供 `sdpa_backend=no_flash` 用于保留 bf16 autocast、同时排除
`CUDNN_ATTENTION/FLASH_ATTENTION`，只允许 efficient/math SDPA 做对照实验。

相比 dense mask attention，packed/ragged 主干不会让短 core 被长 core 的
`Lmax` 拖进 QK 计算，也不会让 projection/FFN/activation 按 `[B,C,Lmax,D]`
分配。

### 3.1 Core/Global Conditioning

packed v1 已接入 per-core 和 global 特征：

```text
core_cond = MLP([side_feats,
                 log1p(n_core),
                 log1p(total_uops),
                 log1p(core_uops),
                 core_uops / total_uops])

global_cond = MLP([global_feats,
                   log1p(n_core),
                   log1p(total_uops)])
```

`core_cond` 和 `global_cond` 会加到每个真实 UOP token 上，并在 per-core pooling
之后再次加到 pooled core representation 上。这样模型可以显式感知可变核心数、
窗口总 UOP 数、每核负载占比以及现有 side/global pressure features。

## 4. Per-Core Readout

模型仍预测窗口级 per-core PMU，不做逐指令 latency head。每个 core 的 packed UOP
states 经过 segment mean pooling：

```text
h_core[b,c] = mean(H_L[start:end])
```

然后进入 PMU head：

```text
pred_log_cpi = cpi_head(h_core)
pred_rate    = sigmoid(rate_head(h_core))
```

推理时：

```text
pred_cpi   = exp(pred_log_cpi)
pred_count = pred_rate * denom
```

## 5. Loss 现状

当前代码采用 CPI 绝对标定 + hard-core tail + 加权核间 gap 监督的 loss：

```text
L =
  1.0  * L_cpi_abs_log(delta=0.3)
+ 0.5  * L_cpi_topk_log(delta=0.3)
+ 0.4  * L_pairwise_log_gap_weighted(delta=0.5)
+ 0.2  * L_cycles_sum(delta=0.1)
+ 0.05 * L_count_log(delta=0.5)
```

其中：

```text
L_cpi_abs_log:
  per-core log(CPI) Huber，按 active core 平均。
  delta 从 0.1 放宽到 0.3，避免大误差样本过早进入近似 L1 区间后梯度过小。

L_cpi_topk_log:
  使用同一个 per-core log(CPI) Huber loss，在本 rank 当前 batch 的 active core
  中取 top 20%（至少 1 个）求均值。
  该项不是排序 loss；它直接提高最差核心的标定权重，减少“多数普通核心把平均
  loss 拉低，少数慢核/快核长期学不好”的问题。

L_pairwise_log_gap_weighted:
  对同一样本内 active core 两两约束 log(CPI_i)-log(CPI_j) 的数值 gap。
  该项不是排序 loss；即使快慢顺序正确，只要快慢幅度不对仍会被惩罚。
  权重为 clamp(abs(label_log_gap) / 0.3, 0.5, 3.0)，真实差异越大的 core pair
  权重越高，用于直接反制“所有 core 预测成窗口均值”的塌缩解。

L_cycles_sum:
  log(sum_core CPI_core * uops_core) Huber，约束窗口总周期标定。
  该项只看窗口总量，不能单独约束 cycles 分配到哪个 core，因此权重从 1.0
  降到 0.2。

L_count_log:
  对 branch/cache/TLB PMU count 做 log1p(pred_count) vs log1p(label_count)
  Huber，作为结构性辅助项。
```

暂不加入 `centered/rate/physical/rank/tail_time`。`pairwise_log_gap_weighted`
覆盖了 centered 的核心目的；`topk` 只作为 hard-core 标定项，不替代 CPI 绝对值
监督，也不只给排序信号。

checkpoint/训练日志中记录：

```text
loss_schema = cpi_abs0.3_topk0.5_pairgapw0.4_cycles0.2_countlog0.05_v3
```

## 6. 复杂度和扩展性

Dense full Q/K/V/R 的 attention score 数约为：

```text
local: C * L * L
cross: C * (C - 1) * L * L
total: C^2 * L^2
```

ragged 版本按真实 core 长度计算：

```text
local: sum_i L_i^2
cross: sum_i L_i * sum_{j!=i} L_j
total: (sum_i L_i)^2
```

因此当前训练 compute budget 更接近 v25a 的 flat token budget：关键不是“每核
最多多少”，而是“每个样本总 UOP 数 `sum_i L_i` 多少”。

直观例子，假设 `heads=8`：

```text
C=8,  L=256  -> scores ~= 29M
C=16, L=512  -> scores ~= 503M
C=32, L=512  -> scores ~= 2.1B
```

因此该方案适合先在 c08/small batch 验证建模有效性。若要稳定扩展 c16/c32，
后续应考虑：

```text
本核 full self-attention: O(C * L^2)
跨核 summary/memory attention: O(C^2 * L * K)
```

其中 `K << L`。

### 6.1 Padding 和预算优化

clean14 direct collate 仍会临时构造入口 tensor：

```text
[B, Cmax, Lmax, F]
```

但当前模型主干会立即 pack 到真实 token：

```text
N_real = sum_{b,c} L_{b,c}
hidden/proj/ffn activations: [N_real, D]
```

因此 padding 只影响入口 batch tensor 和 collate，不再主导 Transformer 主干显存。
仍存在两类入口 padding：

```text
batch 内样本 padding:
  不同样本的 C/L 不同，collate 会 pad 到 batch 最大 Cmax/Lmax。

样本内 core padding:
  同一样本里不同 core 的 UOP 数不同，也会 pad 到该样本最大 Lmax。
```

ragged attention 已经消除 attention 计算中的样本内 core padding；剩余 dense
embedding/MLP 上仍会有 padding，但主要瓶颈 QK 不再按 `C * Lmax` 放大。

当前训练入口做三件事：

```text
1. 模型位置容量:
   max_uops_per_core = 32768

2. 训练 compute cap:
   train_max_total_uops = 8192
   train_max_uops_per_core = 0  # 默认不按单核过滤

3. 默认启用 shape bucket sampler：
   bucket key = (n_core, ceil(total_uops / length_bucket_size))
   length_bucket_size = 512
```

这可以减少 batch 内 padding，尤其在 `BS>1` 或 DDP 每个 step 同时处理多个 rank
时，让各 rank 的样本总 UOP 规模接近。因为 ragged attention 的主复杂度接近
`total_uops^2`，bucket 也按 total UOP 分组。

如果还要继续降复杂度，下一步不再是 padding，而是把跨核 full attention 改成
memory/summary 形式：

```text
local self-attn: 按每个 core 的真实 L_i 单独计算
cross attention: 每核压缩成 K 个 memory slots，O(C^2 * L * K)
```

## 7. 当前训练集满足度

旧训练集和 cache：

```text
data/windows_v16_v9core_tail_local_all/windows.jsonl
data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache
```

只能提供：

```text
能恢复 [B, C, L, 6] UOP fields
有 uop_mask/core_mask
有 per-core PMU label，包括 dtlb_miss
有 uops_per_core 和 denoms，可计算 CPI/cycles/count/rate loss
```

但它不满足当前 clean14 方案，因为新增字段必须在 build windows 阶段从
functional trace 的跨核内存访问序列重放得到，旧 tensor cache 中不存在这些列。
当前训练入口会检查真实 UOP row 宽度；低于 14 会直接报错。

旧训练集的长度分布仍可作为新数据集的 compute cap 参考。2026-07-07 smoke 统计：

```text
dataset_len = 39370
C_unique = [1, 4, 8, 16]
per-core L = max(uops_per_core)
L p50/p90/p95/p99/max = 297 / 858 / 1445 / 4510 / 25676
L > 512  : 6747 samples
L > 1024 : 3406 samples
L > 2048 : 1265 samples
L > 4096 : 432 samples

total_uops = sum(uops_per_core)
total_uops p50/p90/p99/max = 2091 / 5704 / 20332 / 32088
total_uops <= 8192 keeps 36950 / 39370 samples
```

因为 packed attention 计算接近 `O(B * heads * total_uops^2)`，这些长尾样本
仍可能导致前向极慢或 cross-attention 临时 K/V 压力过大。当前训练入口默认：

```text
max_uops_per_core       = 32768  # 模型位置容量
train_max_total_uops    = 32768  # 训练 compute cap
train_max_uops_per_core = 0      # 允许大核使用其他核额度
filter_long_uops        = on
```

处理方式是按 `sum(uops_per_core)` 过滤超过 compute cap 的样本，不截断 UOP
序列。选择过滤而不是截断，是为了避免“只给模型看部分 UOP、却仍用整窗 CPI label”
的标签不一致。

clean14 训练集应满足：

```text
每个 UOP row 长度 = 14
sample.uop_field_schema = v26_14
sample.uop_field_count = 14
有 per-core PMU label / uops_per_core / denoms
有 side_feats；global_feats 当前仍可由 side_feats 派生
按 n_core 和 total_uops bucket 采样
训练入口不依赖 HF tokenizer 或 special token 边界
```

因此下一轮训练必须重建 windows/cache。旧 v16/v25a cache 只能用于历史结果对比，
不能用于当前 clean14 训练。当前 `train/train_v26_kvqr.py` 以
`WindowDataset(..., require_cache=True)` 只读 v26 structured tensor cache；
`make_collate_v26_structured` 按 `core_split/uops` 切分 UOP rows。

## 8. 实现对应

当前实现文件：

```text
model/v26_kvqr.py              # Packed full Q/K/V/R model
train/train_v26_kvqr.py        # 训练入口
train/dataset.py               # v26-only structured collate / tensor cache
eval/eval_quota_cycles.py      # v26 direct structured 部署 eval
scripts/run_v26_kvqr_*         # smoke/DDP 启动脚本，默认 clean14 数据路径
```

部署 eval 的 v26 分支也已经 reset：`predict_window_v26()` 直接从
`per_core_wins` 构造 `[1,C,L,F]`、`side_feats`、`denoms` 和 label。当前 v26
eval checkpoint 加载只接受 `schema.startswith("v26")` 的 full QKVR checkpoint。

部署 eval 现在除全局 CPI/PMU 外，还输出：

```text
per-core CPI MAPE mean/p50/p90/p99
per-core signed bias
窗口内 core CPI pearson/spearman
最慢核 top1/top2 命中率
pred/label core CV 及比例
pred-start / pred-end 相对真实 trace 时间的 cycle 误差
```

这些指标用于区分“单窗 per-core CPI 学不好”和“pred-driven 切窗误差累积”。

checkpoint schema：

```text
v26b_8key_clean14field_doc_qkvr_packed_v1
```

2026-07-08 当前默认训练 profile：

```text
BS=8, world=8, TRAIN_MAX_TOTAL_UOPS=32768, layers=8
D=320, heads=8, ffn_dim=1280
AMP_DTYPE=bf16, SDPA_BACKEND=no_flash, REQUIRE_FLASH_ATTN=0
loss_schema=cpi_abs0.3_topk0.5_pairgapw0.4_cycles0.2_countlog0.05_v3
```
