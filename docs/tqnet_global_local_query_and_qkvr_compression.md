# TQNet 对 TCSim 的设计启发：Global-local Query 与 full QKVR 压缩方案

> 状态：保守的 Query-preserving remote-K/V16 方案已于 2026-08-02 实施并通过
> CPU/CUDA/1-step DDP 门禁；等待正式 60K 训练。实现与命令见
> `docs/v29/query_preserving_kv_design_and_training.md`。Global-local Query 本身仍为后续
> 消融项，未混入首版。
>
> 日期：2026-07-22。
>
> 适用范围：TCSim v29 global-time、monotonic-prefix timing 模型；重点讨论
> `FunctionalInteractionBlock` 的 cross-core `R/K/V` 路径，不改变数据标签、单调
> retirement head、single-global-time scheduler 和 functional-only 部署合同。
>
> 参考论文：Shengsheng Lin et al., *Temporal Query Network for Efficient
> Multivariate Time Series Forecasting*, ICML 2025，
> [arXiv:2505.12917](https://arxiv.org/abs/2505.12917)，
> [官方实现](https://github.com/ACAT-SCUT/TQNet)。

## 0. 结论

TQNet 对 TCSim 有中等偏高的设计参考价值，但不适合直接移植。

值得借鉴的核心是：

1. Attention 的 Query 不必完全来自当前、可能已受噪声或 cursor 偏移影响的样本；
   可以混入训练集级稳定先验。
2. Query 表达足够强时，跨实体交互不一定需要在深层网络的每一层重复执行。
3. 跨实体关系建模和实体内部时序建模可以解耦，从而获得更好的归纳偏置和效率。

不能直接移植的部分是：

1. TQNet 为固定通道身份学习 `cycle_len × channel_count` Query；TCSim 的 core 必须
   permutation-equivariant，禁止固定 core embedding。
2. TQNet 依赖明确的日/周等稳定周期；TCSim 的 `sample_period_cycles=64` 只是采样间隔，
   不是可复用的业务周期。
3. TQNet 是 open-loop 连续时间序列预测；TCSim 是 predicted cursor 驱动的
   free-running 闭环，错误会反馈到下一步输入。
4. TQNet 的 instance normalization 不适合直接应用于 TCSim 的 categorical token、
   equality relation 和具有绝对物理意义的 retirement time。

因此，推荐方案不是复制 Temporal Query，而是引入无 core identity、无绝对周期的
**Semantic Global-local Query**，并按以下顺序压缩 full QKVR：

1. 保留 8 层 local-QKV，先把 full cross-core-RKV 从 8 层减为 2 层；
2. 将每个其他核心的 256 个 UOP 压为 8～16 个 semantic/positional anchors；
3. 让目标 UOP 只对其他核心的 anchors 做 cross attention；
4. 在压缩后的 cross block 中加入 Global-local Query；
5. 证明精度与闭环稳定性后，再把 local tower 从 8 层减为 4～6 层。

该顺序可以独立归因每项改动，并避免同时改变深度、attention 长度和 Query 语义。

## 1. TQNet 的核心方法

### 1.1 Attention 轴

设多变量时间序列输入为：

```text
X_t ∈ ℝ^(L × C)
```

其中：

- `L` 是历史窗口长度；
- `C` 是变量/通道数量，例如电表、路段或传感器数量。

TQNet 将输入转置为：

```text
X_t^T ∈ ℝ^(C × L)
```

因此 TQNet 的 Attention token 是通道，时间窗口是每个通道 token 的 embedding。
它不是沿时间点执行标准 self-attention。

### 1.2 Periodically shifted learnable Query

TQNet 维护一个全局可学习参数表：

```text
P ∈ ℝ^(W × C)
```

`W` 是数据集的稳定周期，例如一天或一周。给定当前样本周期位置，循环取出长度为
`L` 的片段并转置：

```text
Q_t = ([P_((t+i) mod W, :)] for i = 0, ..., L-1)^T ∈ ℝ^(C × L)
```

Attention 使用：

```text
Q = Q_t,    K = X_t^T,    V = X_t^T
```

```text
A = softmax((Q K^T) / sqrt(d)),    Z = A V
```

其中：

- Query 是跨整个训练集学习的、周期相位相关的稳定模式；
- Key/Value 是当前样本的局部观测；
- 相隔 `W` 个采样点的样本共享同一组 Query，使 Query 的梯度聚合多个周期样本，
  从而弱化异常值、缺失和局部扰动。

### 1.3 论文对 TCSim 真正有价值的归纳偏置

TQNet 的有效性不应被理解为“周期表比输入特征更重要”，而应理解为：

```text
稳定、跨样本复用的 Query
        ×
当前样本提供的 Key/Value
        =
全局相关性先验与局部状态的融合
```

论文消融同时表明这种设计并非总能改善：在若干 PEMS 高维数据集上收益显著，但在
Traffic 的 MSE 上反而退化。论文也明确指出，无清晰周期、跨变量相关性弱或 look-back
过长时，强制全局多变量建模可能无效甚至有害。

## 2. TCSim v29 当前 full QKVR

### 2.1 输入与输出

每个共同全局时间点，模型对每个 active core 读取未来 `K=256` 条 functional UOP：

```text
H_0 ∈ ℝ^(C × K × D)
```

输入融合：

- static per-UOP categorical fields；
- dynamic per-UOP equality/relation fields；
- chunk summary；
- cross-core relation；
- uarch features；
- deployment-maintainable state。

模型输出每条 UOP 的非负 retirement gap：

```text
d_hat[c,j] = softplus(f(H[c,j]))
```

并累计为单调 retirement time：

```text
tau_hat[c,j] = sum(i=1..j, d_hat[c,i])
```

该 per-token monotonic head 是 v29 的核心语义合同，本方案不修改。

### 2.2 当前单层 QKVR

每个 `FunctionalInteractionBlock` 计算：

```text
Q = W_Q H,    R = W_R H,    K = W_K H,    V = W_V H
```

其中：

- `Q`：同一核心内部的 local query；
- `R`：访问其他核心 UOP 的 cross-core query；
- `K/V`：当前 token 内容。

核内路径：

```text
Z_local[c] = Attn(Q[c], K[c], V[c])
```

跨核路径：

```text
Z_cross[c] = Attn(R[c], K[other cores], V[other cores])
```

最后执行 cross gate、残差和 FFN：

```text
H' = H + Z_local + g_cross ⊙ Z_cross + FFN(H)
```

当前正式配置为：

```text
K              = 256
d_dyn          = 960
n_heads        = 15
n_layers       = 8
ffn_dim        = 3840
```

八层中的每一层都执行 local attention 和 full cross-core attention。

### 2.3 计算量

若有 `C` 个 active core，每个核心 `K` 个 UOP，则每个目标核心有 `K` 个 Query，每个
Query 访问其他 `(C-1)K` 个 Key。单层 cross attention 的主要 QK/V 计算近似为：

```text
O(C(C-1) K² D)
```

8 层为：

```text
O(8 C(C-1) K² D)
```

c32、K=256 时，每个目标 UOP 访问：

```text
31 × 256 = 7936
```

个其他核心 UOP。该路径保留了最大的表达能力，但重复交换跨核信息的必要性尚未被独立
验证。

## 3. Global-local Query 定义

### 3.1 设计目标

Global-local Query 只首先作用于 cross-core `R`，不替换核内 local `Q`。

目标是同时保留：

1. 当前 target UOP 和 target core 的局部、条件化查询能力；
2. 训练集上跨 trace 学到的稳定机制先验；
3. core-slot permutation equivariance；
4. 当前样本 K/V 所携带的实际 functional context。

它不能使用：

- raw core ID/core slot；
- workload/trace ID；
- nominal channel/bank/row ID；
- `global_time % W`；
- oracle timing/cache/queue outcome。

### 3.2 Local Query

现有 cross-core Query 为：

```text
r_local[c,j] = W_R h[c,j]
```

它是 target-specific 的，可以表达“当前这个核心的这条 UOP 需要从其他核心读取什么”。
但当 predicted cursor、当前 relation 或递归状态已经偏移时，Query 也可能随之失稳。

### 3.3 Global semantic prototypes

新增所有 trace、所有 core 共享的 Query prototype bank：

```text
E = {e_1, e_2, ..., e_M},    e_m ∈ ℝ^D
```

这些 prototype 不绑定具体核心。训练后它们可能对应 compute、branch、independent-load、
dependent-memory、same-row-support、different-row-conflict、high-service-debt 等机制，
但不要求人工硬编码含义。

为每个目标 UOP 构造 permutation-safe 语义输入 `s`：

```text
s[c,j] = SemanticFeatures(c, j)
```

候选输入包括：

- op/memory/control 类型；
- lookahead 相对位置；
- dependency frontier/criticality；
- memory burst position/length；
- same-bank/different-row role；
- same-row support 和 conflict pressure；
- head residency；
- virtual queue rank、bypass age、service-debt rank；
- active-core-relative progress rank。

Router 输出 prototype 权重：

```text
pi[c,j] = softmax(Router(s[c,j]))
```

全局 Query 为：

```text
r_global[c,j] = sum(m=1..M, pi[c,j,m] e_m) + p[j]
```

其中 `p_j` 是 0～255 lookahead 相对位置编码，具有稳定程序顺序语义。

### 3.4 推荐融合：单 Attention Query fusion

推荐只融合 Query，不重复运行第二套 cross attention：

```text
alpha[c,j] = sigmoid(Gate(h[c,j], s[c,j]))
```

```text
r_mix[c,j] = r_local[c,j] + alpha[c,j] r_global[c,j]
```

```text
Z_cross[c,j] = Attn(r_mix[c,j], K[other cores], V[other cores])
```

解释为：

- local Query 决定当前 UOP 具体想问什么；
- global Query 提供同类机制在训练集上的稳定提问方向；
- 当前样本 K/V 提供其他核心此刻实际存在的 functional 内容。

应将 global gate 最后一层初始化为使 `alpha≈0`，确保新模型初始行为退化为现有 local
Query，便于从当前 checkpoint 平滑迁移。

### 3.5 不推荐作为最终实现的双 Attention 分支

研究阶段也可使用：

```text
Z_local_query = Attn(r_local, K, V)
```

```text
Z_global_query = Attn(r_global, K, V)
```

```text
Z = Z_local_query + beta Z_global_query
```

该版本更容易解释 global/local 各自贡献，但 cross attention 计算接近翻倍，不符合压缩
目标。它只适合作为小规模消融，不建议进入最终部署模型。

## 4. 压缩 8 层 full QKVR

压缩包含三个相互独立的维度：

```text
Query 压缩：稳定“问什么”
KV/anchor 压缩：减少“向多少 token 问”
cross-depth 压缩：减少“重复问多少轮”
```

三项必须分阶段验证，不能一次合并后只看最终 CPI。

## 5. Phase A：8 层 local，2 层 full cross

### 5.1 拆分 LocalBlock 与 CrossBlock

将当前混合 block 拆为：

```text
LocalBlock
  LayerNorm
  same-core Q/K/V attention
  residual
  FFN

CrossBlock
  LayerNorm
  cross-core R/K/V attention
  relation/state cross gate
  residual
```

第一阶段仍保留 8 个 local block，只执行 2 次 cross block：

```text
Input feature fusion
        │
     Local 0
        │
     Local 1
        │
     Cross A
        │
     Local 2
        │
     Local 3
        │
     Local 4
        │
     Cross B
        │
     Local 5
        │
     Local 6
        │
     Local 7
        │
Monotonic retirement head
```

第一次 cross 前保留 1～2 层 local，使 raw token 先形成 dependency/position 语义；第二次
cross 放在后半段，刷新深层跨核 context；最后保留 local refinement，生成 per-token
retirement state。

### 5.2 收益与边界

full cross 次数从 8 降至 2，cross QK/V 理论计算约减少 4 倍。local attention、FFN 和
projection 仍保留，因此模型 forward 的实际墙钟收益会小于 4 倍。

该阶段最重要的实验问题是：

```text
TCSim 是否真的需要在每一层重新读取其他核心全部 UOP？
```

如果 2-cross 与 8-cross 精度接近，说明当前 full QKVR 的主要冗余在 cross depth，而不是
模型宽度或 per-token head。

## 6. Phase B：每核 256 UOP 压为 anchors

### 6.1 Anchor 表示

对每个核心独立地将 `K=256` 个 token 压缩为 `M=8～16` 个 anchor：

```text
A[c] = Pool(H[c]) ∈ ℝ^(M × D),    M ≪ K
```

pooling 参数必须在所有核心间共享，以保持 core permutation equivariance。

可选实现：

1. **Learned latent anchors**：用 `M` 个共享 seed queries 对每核 256 token 做 attention
   pooling。
2. **Positional patch anchors**：按程序顺序分为若干连续 patch，例如 8 个 × 32 UOP。
3. **Semantic anchors**：按 load/store/branch/critical-dependency/row-conflict/
   row-support/head/tail 等可部署语义聚合。
4. **Hybrid anchors**：一半 positional、一半 semantic。

首版推荐 `M=16` 的 hybrid 方案：

```text
8 positional anchors
  覆盖 8 × 32-UOP 的程序顺序片段

8 semantic anchors
  head / branch / load / store / dependency-critical /
  same-row-support / different-row-conflict / tail
```

语义组为空时必须携带 anchor mask，不能让空组产生伪 token。

### 6.2 保守方案：target UOP 查询 other-core anchors

每个目标 UOP 仍保留独立 Query，只把其他核心的 K/V 压缩：

```text
Z_cross[c,j] = Attn(r_mix[c,j], K(A[other cores]), V(A[other cores]))
```

c32、`M=16` 时，每个目标 UOP 的 Key 数从：

```text
31 × 256 = 7936
```

降为：

```text
31 × 16 = 496
```

单层 cross-attention 的 QK 长度约减少 16 倍。结合 8-cross 降至 2-cross，总 cross QK
理论缩减为：

```text
(8 × 256) / (2 × 16) = 64
```

倍。

复杂度从：

```text
O(C(C-1) K² D)
```

降为：

```text
O(C(C-1) K M D)
```

该方案仍为每个目标 UOP 生成不同 cross context，能较好保留 prefix 头尾、dependency 和
资源竞争位置语义，是首选 anchor 压缩方案。

### 6.3 激进方案：anchor-to-anchor，再广播到 UOP

进一步只在 anchors 间执行跨核 Attention：

```text
A'[c] = CrossAttn(A[c], A[other cores], A[other cores])
```

然后每个 UOP 从本核更新后的 cross anchors 读取信息：

```text
B[c] = Attn(H[c], A'[c], A'[c])
```

```text
H'[c] = H[c] + Gate(H[c]) ⊙ B[c]
```

复杂度近似：

```text
O(C(C-1) M² D) + O(C K M D)
```

该方案效率更高，但更容易丢失某条目标 load 与其他核心某条具体 store/load 的短尺度
关系，以及跨核地址相位在 prefix 中的具体位置。因此只能在 token-to-anchor 方案通过后
再验证。

## 7. Phase C：加入 Global-local Query

Global-local Query 应在 cross depth 和 KV 长度已经独立消融后加入。推荐放在压缩后的
CrossBlock 中：

```text
Target UOP hidden
   ├── local R projection ────────────────┐
   │                                      │
   └── semantic router → global prototype ├── query fusion
                                          │
Other-core anchors → K/V projections ─────┘
                    │
             one cross attention
                    │
             relation/state gate
                    │
             per-token residual
```

global prototype 数量建议从 `8/16` 开始。过大的 prototype bank 可能退化为机制或 trace
记忆表，应通过新 seed/binary、router entropy 和 permutation tests 约束。

## 8. Phase D：压缩 local tower

只有在以下两项均通过后才减少 local 层数：

1. 2-cross 相对 8-cross 没有明显闭环退化；
2. token-to-anchor 相对 full-token cross 没有丢失慢核和 OOD 机制。

候选 student：

```text
Feature fusion
  → Local × 2
  → Anchor pool
  → Global-local Cross × 1
  → Local × 2
  → optional second Cross
  → Local × 1～2
  → Monotonic head
```

最终建议搜索：

- local layers：4、6、8；
- cross layers：1、2、4；
- anchors/core：8、16、32；
- global prototypes：0、8、16。

减少 local 层会影响核内 dependency、branch 和程序顺序建模，风险高于只减少 cross
层，因此必须最后执行。

## 9. Checkpoint 迁移

### 9.1 8-local/2-cross 迁移

从现有 8 层 checkpoint 拆分时：

- 每个 `LocalBlock` 复制对应原层的 `q_proj/k_proj/v_proj/local_o_proj/FFN/LayerNorm`；
- 两个 `CrossBlock` 可复制原第 1、5 层或第 2、6 层的
  `r_proj/k_proj/v_proj/cross_o_proj`；
- 保留现有 cross gate；
- static/dynamic/side projections 完整继承；
- timing gap head 和 branch head 完整继承。

由于原实现 local/cross 共享 `K/V` projection，拆分后应显式记录初始化来源；后续允许
local 和 cross 的 K/V 独立更新。

### 9.2 新模块初始化

- anchor seed/projection：小方差初始化；
- global prototypes：小方差或零均值初始化；
- router：普通小方差初始化；
- global gate：初始化为 `alpha≈0`；
- anchor broadcast/cross residual gate：初始化为接近当前 full-cross 输出尺度；
- 新 checkpoint metadata 必须记录 `interaction_schema`、anchor 数、prototype 数、
  cross-layer placement 和 pooling contract。

### 9.3 训练顺序

1. 冻结 static encoder、local tower 和 timing head；
2. 只训练 anchor、compressed cross、router 和 global Query；
3. 验证 one-step 没有明显退化；
4. 解冻 interaction tower，使用较低学习率 fine-tune；
5. 最后才允许 timing head 小学习率更新；
6. 必须运行 free-running，不能用 oracle one-step 代替最终判断。

## 10. Local tower 蒸馏

若将 local layer 从 8 减为 4～6，建议以当前 8 层模型为 teacher。除真实监督外加入：

```text
L_hidden = ||H_student - stop_gradient(H_teacher)||²
```

```text
L_tau = |log(1 + tau_student) - stop_gradient(log(1 + tau_teacher))|
```

```text
L_progress = ||N_student(h) - N_teacher(h)||_1
```

总 loss：

```text
L = L_v29-supervised
  + lambda_h   L_hidden
  + lambda_tau L_tau
  + lambda_p   L_progress
```

teacher 只作为训练正则，真实 commit-time/prefix target 仍是最终监督，避免 student 继承
teacher 的 Redis 和慢核系统偏差。

## 11. 消融矩阵

必须按以下顺序独立实验：

| 实验 | Local 层 | Cross 层 | Cross KV | Query | 目的 |
|---|---:|---:|---|---|---|
| A | 8 | 8 | 256 tokens/core | local | 当前基线 |
| B | 8 | 2 | 256 tokens/core | local | 验证 cross depth 冗余 |
| C | 8 | 2 | 16 anchors/core | local | 验证 KV 压缩 |
| D | 8 | 2 | 16 anchors/core | global-local | 验证 Query 先验增量 |
| E | 6 | 2 | 16 anchors/core | global-local | 压 local 深度 |
| F | 4 | 1/2 | 8/16 anchors/core | global-local | 激进 student |

额外 anchor 消融：

| Anchor 方案 | M | 风险 |
|---|---:|---|
| learned latent | 8/16 | 可能缺少明确位置/资源语义 |
| positional patches | 8/16 | 可能混合不同 memory mechanism |
| semantic groups | 8/16 | 空组和人工分组偏置 |
| hybrid | 16 | 实现略复杂，首版推荐 |

Global Query 消融：

1. 无 global Query；
2. 单个共享 global vector；
3. position-only global Query；
4. semantic prototype router；
5. 双 Attention global/local，仅作研究上界；
6. 打乱 semantic router 输入，确认收益不是额外参数导致。

## 12. 评估指标

### 12.1 Oracle one-step

- per-UOP `log1p(commit_time)` MAE/p50/p90/p99；
- 各 horizon progress count MAE 和 signed bias；
- prefix BCE；
- monotonic violation 必须为 0；
- memory token、branch token、head/tail position 分桶误差；
- 每核 `tau@stride` signed error；
- slow/fast core pairwise ordering。

### 12.2 Free-running

- workload-macro ROI-CPI；
- per-core MAPE 和 signed endpoint error；
- makespan error；
- cursor-interval offset p50/p90/p99/max；
- drift slope 和 cross-core oracle-head span；
- no-progress、overshoot、剩余 UOP；
- 每 workload、每 core count 独立报告；
- 不能用 pooled CPI 的正负抵消作为通过依据。

### 12.3 重点 workload

必须至少覆盖：

1. `memory_seq_moderate c16/c32`：检查离散慢核、FR-FCFS 相位和逐核 endpoint；
2. `redis_heldout`：检查 global prior 是否进一步把 OOD 样本拉回训练均值；
3. compute/private workload：检查弱相关情况下 cross/global 分支是否造成退化；
4. mixed business base/heldout：检查总体泛化；
5. c4/c8/c16/c32：检查可变 core count 和 active-core tail。

### 12.4 不变性

必须通过：

- core-slot permutation equivariance；
- nominal channel/bank label permutation；
- address relocation 下 equality/reuse 语义不变；
- 不同 active-core 数量和完成顺序；
- global prototype/router 不读取 core/trace/workload ID。

### 12.5 性能

记录：

- model forward latency p50/p95；
- context/model/scheduler 阶段时间；
- UOP/s 和 useful UOP/forward；
- GPU peak allocated/reserved；
- SDPA kernel/backend；
- anchor pooling、cross attention、broadcast 的独立 profile；
- c4/c8/c16/c32 scaling。

cross QK 理论倍数只能用于解释结构，不能替代端到端实测。当前 context build 已占总墙钟
较大比例，即使模型计算大幅下降，端到端收益仍受 Amdahl 定律限制。

## 13. 准入门槛

进入完整重训前建议满足：

1. B 相对 A：2-cross 的 oracle one-step 和 free-running 不显著退化；
2. C 相对 B：anchor 压缩保持主要精度，同时 model forward 明显下降；
3. D 相对 C：global-local Query 在新 seed/phase split 上有稳定增量，而不是只改善训练集；
4. memory-seq 慢核召回、per-core signed endpoint 和 drift 同方向改善；
5. Redis heldout 不因全局平均而进一步恶化；
6. 所有 permutation tests 通过；
7. 无新增 no-progress、overshoot 或闭环正反馈；
8. 只有 D 通过后才启动 E/F 的 local-depth compression。

可采用现有 feature/state 方案中的严格门槛作为参考：在 leave-core/phase/seed-out 上提供
稳定的 residual 改善，并要求 oracle one-step 与 free-running 同方向。具体百分比应根据
重复 seed 的置信区间校准，不能在看到 heldout 后反复调整。

## 14. 主要风险与缓解

### 14.1 Global Query 把罕见慢状态拉回均值

Redis OOD 和 memory-seq 离散慢核都可能被全局先验过度平滑。

缓解：

- 保留 local Query 主路径；
- global gate 零初始化；
- gate 输入加入 uncertainty、service debt 和 OOD-sensitive locality state；
- 对 Redis 和 per-core signed tail 设置独立回归门禁。

### 14.2 Anchors 丢失精确 token-to-token 竞争

特别是 same-bank/different-row 的短尺度相位可能被 mean pooling 稀释。

缓解：

- 首先使用 target-UOP-to-anchor，而不是 anchor-to-anchor；
- 使用 hybrid positional/semantic anchors；
- 保留现有 per-token dynamic equality/relation；
- 增加 head、critical-memory、row-conflict 专用 anchors；
- 消融 M=8/16/32。

### 14.3 Prototype 退化为身份记忆

Router 可能通过不稳定 categorical ID 暗中识别 trace 或 core。

缓解：

- 严格限制 router 输入；
- 做 core/resource relabel；
- 使用新 seed/new binary；
- 审计 prototype usage、router entropy 和 workload mutual information。

### 14.4 Oracle one-step 改善但闭环恶化

Query 或压缩表示可能降低单步误差，却产生持续同号 progress bias。

缓解：

- 每个阶段都运行 free-running；
- 报告 cursor interval drift 和 per-core cumulative signed error；
- global gate 和 compressed-cross residual 使用保守初始化；
- 必要时先训练小 correction head，再全模型 fine-tune。

## 15. 推荐实施顺序

### P0：先解决输入可辨识性

Global-local Query 不能替代以下功能状态：

- 长期 line/page locality；
- ordered memory phase；
- dependency-ready frontier；
- virtual open-row/queue rank；
- bypass age 和 service debt；
- 独立的 head residency。

这些状态缺失时，Global Query 只能学习条件均值。应优先按
`v29_functional_feature_and_virtual_state_update_plan.md` 完成 probe 和最小 state 验证。

### P1：cross-depth 消融

实现 LocalBlock/CrossBlock 拆分，测试 8-local/2-cross。该阶段不引入 anchors 和 global
Query，确保可以单独判断 cross depth。

### P2：token-to-anchor cross

实现 `M=16` hybrid anchors，将 cross KV 从 256 tokens/core 压为 16 anchors/core；保持
target UOP Query 和 per-token输出。

### P3：Semantic Global-local Query

在 compressed CrossBlock 中加入 prototype/router/query fusion；先冻结 backbone 训练，再
低学习率全模型 fine-tune。

### P4：local-depth student

在 P1～P3 通过后，通过蒸馏测试 6-layer 和 4-layer local tower。不得因模型尺寸目标跳过
闭环和 OOD 验收。

## 16. 最终推荐结构

保守目标结构：

```text
Static/dynamic/side feature fusion
        │
Local token blocks × 2
        │
Hybrid anchor pooling, M=16/core
        │
Global-local Query CrossBlock
target UOP queries → other-core anchors
        │
Local token blocks × 2～3
        │
Optional second compressed CrossBlock
        │
Local refinement × 1
        │
Per-token softplus gap
        │
FP64 monotonic prefix accumulation
        │
Commit-time / progress outputs
```

该结构保留 TCSim 最关键的 per-token、target-specific、closed-loop 语义，同时把 full
cross 的三个主要成本分别压缩：

1. cross layers：8 → 1～2；
2. other-core K/V：256 tokens/core → 8～16 anchors/core；
3. Query：纯当前样本 → local Query + gated global semantic prior。

最重要的判断标准不是参数量或单步 loss，而是：在 memory-seq、Redis 和新 untouched
seed/binary 上，per-core endpoint、cursor drift、workload-macro CPI 与推理吞吐能否同时
改善。
