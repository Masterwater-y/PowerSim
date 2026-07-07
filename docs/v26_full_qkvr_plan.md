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
side_feats:   [B, C, S]   # 当前 full QKVR 兼容路径暂不使用
global_feats: [B, G]      # 当前 full QKVR 兼容路径暂不使用
```

输出：

```text
pred: [B, C, K]
K = 8 = cpi_uop + branch/cache/TLB PMU
```

其中 `C` 是 batch 内 padding 后 core 数，`L` 是 batch 内 padding 后每核 UOP 长度。
真实有效 core/UOP 由 mask 决定。

## 2. UOP Embedding

兼容实现继续复用现有 v16/v25a tensor cache，只能恢复 6 个 UOP 字段：

```text
opclass, reg_bucket, memkind, rd_bucket, stride_bucket, branch_bucket
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

严格 clean 10-field 方案仍需要重建数据集，补齐：

```text
pc_bucket, branch_hist_bucket, xcore_mem_bucket, macro_pos_bucket
```

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

当前默认恢复为之前的 4 层 v26 transformer profile：

```text
D = 320
heads = 8
layers = 4
ffn_dim = 1280
attention_impl = ragged_sdpa
amp_dtype = bf16
sdpa_backend = auto
```

这个 profile 不再为了维持 8 层 profile 的整体参数量而加宽 `D`。它保留 4
层深度和 `D=320, ffn_dim=1280` 的旧 v26 规模，用于降低显存、DDP all-reduce
和每层 projection/FFN 开销。

v26 QKVR block 参数量：

```text
P_block = 5 * D^2 + 2 * D * ffn_dim + ffn_dim + 5 * D
        = 1.334080M  # D=320, ffn_dim=1280
```

v26 4 层 blocks 总参数：

```text
P_blocks = 4 * 1.334080M = 5.336320M
```

注意：由于 `pos_emb = max_uops_per_core * D`，恢复 `D=320` 后位置表参数也会
从 12.58M 降回 10.49M。

在该 profile 下，v26 整体参数量会低于 8 层同宽 profile：

```text
QKVR blocks      5.34M
pos_emb         10.49M  # 32768 * 320
uop_encoder      0.67M
core/global MLP  0.32M
PMU head         0.20M
```

实现上使用 packed 主干：模型入口仍接收 compat collate 产生的 dense
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

当前代码采用简化后的 CPI 主任务 loss：

```text
L =
  1.0  * L_cpi_abs(delta=0.1)
+ 1.0  * L_cycles_sum(delta=0.1)
+ 0.05 * L_count_log
```

其中：

```text
L_cpi_abs:
  per-core log(CPI) Huber，按 active core 平均。

L_cycles_sum:
  log(sum_core CPI_core * uops_core) Huber，约束窗口总周期标定。

L_count_log:
  对 branch/cache/TLB PMU count 做 log1p(pred_count) vs log1p(label_count)
  Huber，作为结构性辅助项。
```

暂不加入 `centered/rate/physical/rank/tail_time`。原因是当前首要目标是验证 full
Q/K/V/R 是否能恢复 CPI 绝对标定和窗口总量；过早强化核间相对形状或 PMU rate
约束，可能再次放大系统性低估。

checkpoint/训练日志中记录：

```text
loss_schema = cpi_abs0.1_cycles0.1_countlog0.05_v1
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

compat6 collate 仍会临时构造：

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

现有训练集和 cache 在字段结构上可以支持当前兼容实现：

```text
data/windows_v16_v9core_tail_local_all/windows.jsonl
data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache
```

满足：

```text
能恢复 [B, C, L, 6] UOP fields
有 uop_mask/core_mask
有 per-core PMU label，包括 dtlb_miss
有 uops_per_core 和 denoms，可计算 CPI/cycles/count/rate loss
```

但它不能直接作为 full Q/K/V/R 的无过滤训练集。2026-07-07 smoke 统计：

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
train_max_total_uops    = 8192   # 训练 compute cap
train_max_uops_per_core = 0      # 允许大核使用其他核额度
filter_long_uops        = on
```

处理方式是按 `sum(uops_per_core)` 过滤超过 compute cap 的样本，不截断 UOP
序列。选择过滤而不是截断，是为了避免“只给模型看部分 UOP、却仍用整窗 CPI label”
的标签不一致。

不满足严格 clean 方案：

```text
缺少 10-field 中的 pc/branch_hist/xcore_mem/macro_pos
global_feats 仍从 side_feats 派生
没有按 C/L bucket 的采样器
packed v1 已显式注入 n_core/total_uops/core_uops/share；clean 数据集仍应提供
更完整的 global_feats。
```

因此当前数据集可以用于 compat6 smoke 和受限 cap 的初步 ablation；若 full Q/K/V/R
在 c08 上有效，再重建 10-field clean 数据集，并加入 C/L bucket sampler 或跨核
summary attention。

## 8. 实现对应

当前实现文件：

```text
model/v26_kvqr.py              # Packed full Q/K/V/R model
train/train_v26_kvqr.py        # 训练入口
train/dataset.py               # compat6 collate
eval/eval_quota_cycles.py      # v26 doc_qkvr checkpoint 加载和部署 eval
scripts/run_v26_kvqr_compat_*  # smoke/DDP 启动脚本
```

checkpoint schema：

```text
v26a_8key_compat6field_doc_qkvr_packed_v1
```

2026-07-07 packed smoke：

```text
BS=8, world=8, TRAIN_MAX_TOTAL_UOPS=32768, layers=4
20 steps completed without OOM
elapsed_s=17.3 at step 20
throughput ~= 74 samples/s
```
