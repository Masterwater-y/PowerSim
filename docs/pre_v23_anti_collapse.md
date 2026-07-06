# v23: Per-core CPI 反塌缩改造（loss + head 双端）

状态：设计稿。目标是在 v22 baseline 上，不换 base model、不改 tokenizer、
不重建数据集，只改 loss 和 head，让每核 CPI 输出从"塌缩到窗口均值"变成
"跟随每核真实 CPI"。这是 v22→v23 的最小可行改造。

## 1. 问题定位

### 1.1 塌缩现象

历史诊断数据：

| 版本 | pred_log_cpi_std / label_log_cpi_std | query_pair_cos | 
|---|---:|---:|
| v9 baseline | ~0.4 | - |
| v18 fastslow | 0.15 | 0.997 |
| v19 local-core | ~0.30 | 0.868 |
| v22 bind-fuse | 类似 v19 | - |

label 侧核间 log-CPI std 平均 ~0.50（快慢核 CPI 比接近 3×），pred 侧只有
0.15-0.30，**pred CV 是 label 的 30-60%**。这是核间 CPI 塌缩到窗口均值的
直接指标。

塌缩的下游后果：

- 部署 planner 用塌缩后的 pred CPI 推进每核 cursor，快核被切少慢核被切多
- 切窗错位导致下一窗输入分布偏离训练分布，CPI 预测更错
- Phased_mix c16、ads_ranking_proxy c16/c32 等高 spread 负载崩溃
- 全局 CPI 靠 uop 加权和时间平均**掩盖**了这个问题，看起来还行

### 1.2 塌缩的三个共同因

从 v17 到 v22 的多次尝试证明**改任何单端都不够**。塌缩是三个层面同时允许的：

1. **表征端**：causal + tail query 的几何几乎让所有 QUERY_Ci hidden 一致
2. **目标端**：Huber δ=0.1 + L_cycles_window + gated L_centered_cpi 允许均值
   预测作为可行解甚至最优解
3. **head 端**：三个 head 共享参数，没有 per-core identity，纯靠 hidden 差异
   区分核

### 1.3 v22 现状：三端都有妥协

`train/loss.py`：

```python
lambda_cpi_abs      = 1.0     # Huber δ=0.1 死区太大
lambda_cycles       = 1.0     # 只监督窗口总和，不监督每核
lambda_aux_pmu      = 0.05    # 太弱，PMU 几乎不训
lambda_centered_cpi = 0.3     # 权重不够
centered_min_std    = 0.30    # gate 阈值高，训练集里大部分窗口不触发
huber_delta         = 0.1     # 相当于 ±10% 相对误差不惩罚
```

`model/regression_head.py`：

```python
cpi_head    : LayerNorm → Linear(d,256) → GELU → Linear(256,1)
branch_head : LayerNorm → Linear(d,256) → GELU → Linear(256,1)
cache_head  : LayerNorm → Linear(d,256) → GELU → Linear(256,5)
```

Head 只看 `query_hidden [B, N_core, D]` 输入，**参数在所有核间共享**，
**没有 core-identity 信号**。

`model/llm_wrapper.py`：

```python
local_proj  : 零初始化，训练早期几乎不激活
side_proj   : 零初始化，训练早期几乎不激活
tstart_proj : 零初始化，训练早期几乎不激活
local_bind_fuse.alpha = 0.10  # 本核局部信号权重很小
```

## 2. 目标端为什么允许塌缩：数学分析

这一节展开塌缩解为什么在 v22 loss 下**是可行的甚至最优的**，不是训练不够。

### 2.1 一个具体高 spread 窗口

拿 phased_mix c04 的一个真实高 spread 窗口，4 核 log CPI 分别：

```
core 0: log(0.82) = -0.20
core 1: log(6.75) =  1.91
core 2: log(0.79) = -0.24
core 3: log(7.12) =  1.96

均值 μ = 0.86
std σ = 1.08
```

### 2.2 塌缩解的 loss

假设模型输出全部预测均值 `pred_i = 0.86`。

**L_cpi_abs**（Huber δ=0.1 逐核）：

```
per_core residual = |pred_i - label_i|
core 0: |0.86 - (-0.20)| = 1.06  → Huber = 0.1 × (1.06 - 0.05) = 0.101
core 1: |0.86 - 1.91|    = 1.05  → Huber = 0.1 × (1.05 - 0.05) = 0.100
core 2: |0.86 - (-0.24)| = 1.10  → Huber = 0.1 × (1.10 - 0.05) = 0.105
core 3: |0.86 - 1.96|    = 1.10  → Huber = 0.1 × (1.10 - 0.05) = 0.105
mean = 0.103
```

Huber δ=0.1 在残差 >0.1 的线性段，梯度是常数 0.1（不随残差增大而增大）。
这意味着**塌缩解的 loss 是有界的**，梯度也是有界的。

**L_cycles_window**（窗口总 cycles 匹配）：

```
sum(exp(pred_i) × uops_i) = exp(0.86) × Σ uops_i
sum(exp(label_i) × uops_i) = exp(-0.20)×u0 + exp(1.91)×u1 + exp(-0.24)×u2 + exp(1.96)×u3
```

如果 uops 每核均等（W=1024 macros × 2 uops/macro，每核 ~2000），
label 侧总 cycles ≈ (0.82 + 6.75 + 0.79 + 7.12) × 2000 = **31k cycles**。
塌缩 pred 侧 cycles ≈ 0.86 × 8000 = **6.9k cycles**... 差 4.5×。

**等等**——这里塌缩解在 L_cycles_window 上是不通的。让我们更仔细看。

pred 是 log CPI，`exp(0.86) = 2.36`，pred CPI = 2.36。四核塌缩到 CPI=2.36 时
总 cycles = 2.36 × 8000 = **18.9k cycles**。label 总 cycles = **31k cycles**。
差 **1.64×**，log 空间差 0.5 nat。Huber δ=0.1，进入线性段，
`L_cycles = 0.1 × (0.5 - 0.05) = 0.045`。

也就是说，**如果所有核塌缩到 log 均值**，`L_cycles_window` 会告诉模型
"总 cycles 少算了"，梯度会推 pred 上升。但如果所有核塌缩到**加权后 log 均值**
（即 `pred = log(label CPI 加权均值)`）：

```
label CPI 加权均值 = 31k / 8k = 3.87
log(3.87) = 1.35

塌缩到 pred = 1.35：
L_cycles = 0，完美匹配窗口总 cycles
L_cpi_abs residuals：
  core 0: |1.35 - (-0.20)| = 1.55 → Huber = 0.1 × (1.55 - 0.05) = 0.150
  core 1: |1.35 - 1.91|    = 0.56 → Huber = 0.1 × (0.56 - 0.05) = 0.051
  core 2: |1.35 - (-0.24)| = 1.59 → Huber = 0.1 × (1.59 - 0.05) = 0.154
  core 3: |1.35 - 1.96|    = 0.61 → Huber = 0.1 × (0.61 - 0.05) = 0.056
  mean = 0.103
```

**关键发现**：塌缩到 `log(CPI 加权均值)` 时：
- `L_cycles_window = 0`（完美）
- `L_cpi_abs = 0.103`（有界，但不是零）

**如果预测真值**：
- `L_cycles_window = 0`
- `L_cpi_abs = 0`

真值预测的 loss 严格更低。**塌缩解不是全局最优，但比真值解容易得多**。

### 2.3 为什么塌缩是"更容易的解"

进入 causal-tail 结构 + 共享 head 的组合：

- 模型输出核间差异的**代价**：backbone 必须让 QUERY_Ci hidden 分开、head 必须
  从微小差异里放大信号
- 模型输出核间均值的**代价**：head 只需要输出 pooled hidden 的一个映射

Loss surface 上的两个 basin：

```
"塌缩到 log CPI 加权均值"：L_total ≈ 0.103 + 0.05 × aux + 0.3 × centered
"真值预测"：L_total ≈ 0
```

真值 basin 更深，但**塌缩 basin 更宽更平**（Huber δ=0.1 死区让"接近均值"的
预测有大量等价点）。SGD 沿梯度最陡方向走，先找到塌缩 basin 就不容易出来。

### 2.4 L_centered_cpi 为什么救不了

v22 加了 `L_centered_cpi`，公式：

```python
p_delta = p - mean_core(p)
y_delta = y - mean_core(y)
L_centered = Huber(p_delta - y_delta, δ=0.1)
gated on: label_log_std > 0.30
weight   ∈ [0.10, 3.0]
lambda_centered_cpi = 0.3
```

这个 loss 直接监督**每核相对均值的偏差**。理论上应该能防止塌缩。

问题是三个：

**问题 1：gate 阈值 0.30 太高**。训练集里大部分窗口 label_log_std < 0.30，
gate 不触发。上面 phased_mix 例子 std=1.08 触发了，但训练时这类窗口比例
估计 10-20%。

**问题 2：权重 0.3 太小**。就算触发，加权后是 0.3 × Huber(δ=0.1)，塌缩解
下 y_delta 大而 p_delta ≈ 0，Huber 是线性段：

```
core 0 delta: y_delta = -0.20 - 0.86 = -1.06, p_delta = 0
  |p_delta - y_delta| = 1.06 → Huber = 0.101
core 1: y_delta = 1.05, p_delta = 0 → Huber = 0.100
...
L_centered ≈ 0.1
乘 weight (取 1.08/0.30 = 3.6，clamp 到 3.0) = 0.3
乘 lambda_centered_cpi 0.3 = 0.09
```

**问题 3：仍然是 Huber**。塌缩解下 `p_delta = 0`，损失是有界线性梯度，和
`L_cpi_abs` 是同一类信号，不产生额外的"塌缩不可行"约束。

### 2.5 关键机制：塌缩需要"不可行"，不是"更贵"

Huber-type loss 让塌缩解**贵一点**，但仍然可行——梯度有界，模型可以选择
"接受这个额外 loss，换取 head 输出简单"。

要真正打破塌缩，loss 必须提供一个**在塌缩解处非零下界**的项，且下界随核间
label std 增大**放大**而不是有界。有两类 loss 满足这个：

1. **Rank / pairwise loss**：塌缩解下所有 pair sign 都错，惩罚随 pair 数量
   平方增长
2. **Contrastive-style loss**：塌缩解下"任意两核 hidden 相同" → 对比 loss
   给出常数正惩罚

## 3. Head 端为什么允许塌缩：结构分析

### 3.1 当前 head 的信息路径

```
query_hidden [B, N_core, D]
→ LayerNorm
→ Linear(D → 256)
→ GELU
→ Linear(256 → 1)  # cpi_uop
```

同一个 head 处理所有核。核间区分**完全依赖 hidden 差异**：如果
`hidden[c_i] ≈ hidden[c_j]`，则 `pred[c_i] ≈ pred[c_j]`。这是数学上的
Lipschitz 连续性——head 是有界梯度的连续函数。

v18 诊断显示 `query_pair_cos = 0.997`。四核 hidden 几乎在同一点，head 无论
怎么设计都输出接近相同的值。

### 3.2 v22 引入 `local_bind_fuse` 后

Head 输入变成：

```
h_i = query_hidden_i
    + alpha × LN(local_hidden_i)   # alpha=0.10, 训练时可学
    + MLP([q, l, q-l, q*l])         # MLP 末层零初始化
    + side_proj(side_feats_i)      # 零初始化
    + tstart_proj(t_start_i)       # 零初始化
```

理论上 `local_hidden_i` 是本核 segment 尾部的 hidden，每核不同，应该能打破
均值。

实际问题：

- `alpha` 初值 0.10，训练早期本核信号只贡献 10%
- MLP 末层零初始化，训练早期贡献 0
- side_proj 零初始化，早期贡献 0
- 训练前几百步 head 输入几乎等于 `query_hidden`，塌缩局部最优先形成

**这些零初始化机制的初衷是"稳定训练"，避免额外模块一上来就扰乱主路径**。
但代价是训练早期塌缩解先形成，之后即使 `local_proj` 开始有信号，模型也
已经卡在塌缩 basin 里。

### 3.3 参数共享的隐藏成本

Head 参数在所有核间共享意味着：**head 无法学到"我在处理第几个核"**。它
只能从 hidden 里读出每核的独立信息。如果 hidden 差异不足以携带 identity，
head 输出必然趋同。

对比：如果每核有自己的 head 参数（`head_c0`, `head_c1`, ...），即使
`hidden[c_i] = hidden[c_j]`，两个 head 也能输出不同的值。但这样引入
n_core 依赖，破坏跨核数泛化。

**折中方案**：共享 head 参数 + 每核一个 identity embedding 加到 head 输入。
既保持 n_core 无关，又打破共享 head 的塌缩倾向。

## 4. 反塌缩设计（v23）

三端同时改，且保持工程量最小。

### 4.1 Loss 改造

**改动 1：Huber δ 缩小**

```python
# v22
huber_delta = {
    "logratio": 0.1,   # ±10% 相对误差死区
    "logcount": 0.5,
    ...
}
cycles_delta = 0.1

# v23
huber_delta = {
    "logratio": 0.02,  # ±2% 相对误差死区
    "logcount": 0.1,
    ...
}
cycles_delta = 0.02
```

Huber δ 缩小让塌缩解的 loss 从 0.10 → 0.5+，且线性段梯度 0.02 而不是 0.1。
这不是彻底解决，但让塌缩 basin 变浅。

**改动 2：删除 L_cycles_window，改为逐核 cycles**

```python
# v22
L_cycles = Huber(log(sum_i CPI_i × uops_i), log(sum_i label_i × uops_i))
# 只监督总和，允许核间抵消

# v23
L_cycles_per_core = mean_i Huber(pred_log_cpi_i + log(uops_i),
                                  label_log_cpi_i + log(uops_i), δ=0.02)
# 逐核监督 log cycles，不允许抵消
```

这是把 `L_cycles` 变成 per-core 版本。数学上等价于 `L_cpi_abs` 的 uops 加权
版本，但对高 uops 的核给更多权重（因为它们贡献 wall-clock 更多）。

**改动 3：L_centered_cpi 去 gate，权重提高**

```python
# v22
centered_min_std = 0.30
lambda_centered_cpi = 0.3
centered_delta = 0.1

# v23
centered_min_std = 0.0            # 所有 active_core > 1 窗口都参与
lambda_centered_cpi = 1.0         # 和 L_cpi_abs 同权
centered_delta = 0.02             # 更严格的死区
centered_weight_max = 5.0         # 高 spread 窗口权重上限提高
```

**改动 4：新增 L_rank（关键）**

Pairwise rank loss，塌缩解下给恒定非零下界：

```python
def rank_loss(pred_log_cpi, label_log_cpi, core_mask):
    # pred/label: [B, N_core]
    # 对每个窗口 B，任意两核 i<j
    dp = pred[:, :, None] - pred[:, None, :]    # [B, N, N]
    dy = label[:, :, None] - label[:, None, :]  # [B, N, N]
    
    # 只保留 label 差异大于 margin 的 pair（避免同 CPI 核的噪声）
    gap = 0.05  # log space, 相当于 5% CPI 差异
    pair_mask = (dy.abs() > gap) & upper_triangular & both_active
    
    # sign(dy) 是真实顺序，dp × sign(dy) > 0 表示预测顺序对
    tau = 0.1
    loss = F.softplus(-(dp * dy.sign()) / tau)
    return loss[pair_mask].mean()

lambda_rank = 0.5
```

**为什么这个 loss 打破塌缩**：塌缩解下 `dp ≈ 0`，`softplus(0) = ln(2) ≈ 0.69`
每 pair。窗口 4 核有 6 pairs，其中若 3 pairs 满足 label gap 条件，
`L_rank ≈ 0.69 × 3 = 2.07`。远超塌缩解下 `L_cpi_abs = 0.10`。

**改动 5：可选加 L_shape（分离到二阶）**

```python
def shape_loss(pred_log_cpi, label_log_cpi, core_mask):
    p_std = masked_std(pred, mask, dim=core)
    y_std = masked_std(label, mask, dim=core)
    return F.smooth_l1_loss(torch.log(p_std + 1e-3),
                            torch.log(y_std + 1e-3),
                            beta=0.05)

lambda_shape = 0.3
```

监督 log 空间的 std。塌缩解下 `p_std → 0`，`log(p_std)` → -∞，loss 大且梯度大。

### 4.2 Head 改造

**改动 1：核 identity embedding 注入 head 输入**

```python
class PMURegressionHead:
    def __init__(self, d_model, max_cores=64, ...):
        ...
        self.core_id_emb = nn.Embedding(max_cores, d_model)
        nn.init.normal_(self.core_id_emb.weight, std=0.02)
    
    def forward(self, query_hidden, core_mask):
        # query_hidden: [B, N_core, D]
        B, N, D = query_hidden.shape
        core_ids = torch.arange(N, device=query_hidden.device)
        id_emb = self.core_id_emb(core_ids)  # [N, D]
        h = query_hidden + id_emb.unsqueeze(0)  # [B, N, D]
        ...
```

核 id embedding 让 head 知道"我在处理第几个核"。即使 hidden 完全相同，
每核输入加上不同的 id embedding 后 head 输出可以不同。

**参数量**：`max_cores=64, D=1024`，64 × 1024 = 65k 参数，可忽略。

**跨核数泛化风险**：如果只训练 c04/c08/c16，`core_id_emb[16..31]` 从没训过，
部署 c32 时用未训 embedding。缓解：

- 训练时随机 shuffle core order，让每个 core_id_emb slot 见到各种角色
- 或者用**相对 core rank**：把 core 按某种 functional feature (如 uops)
  排序，位置编码用 rank，不用绝对 id

**改动 2：Residual centering 输入变换**

```python
def forward(self, query_hidden, core_mask):
    # detach mean 让 head 只看每核 offset，不影响 backbone 学 mean
    mean = masked_mean(query_hidden, core_mask, dim=1, keepdim=True).detach()
    centered = query_hidden - mean
    h = torch.cat([mean.expand(-1, N, -1), centered], dim=-1)
    # 或者简单 concat
    return self.head(h)
```

让 head 显式看到两个信号：窗口 mean（决定全局 scale）和 per-core offset
（决定核间差异）。这防止 head 学"忽略 offset 输出 mean"的 shortcut。

**改动 3：Local binding 提前激活**

```python
# v22
local_bind_fuse.alpha = 0.10  # 训练早期本核信号弱
MLP 末层零初始化

# v23
local_bind_fuse.alpha = 1.0   # 训练开始就有满量本核信号
MLP 末层小方差初始化（std=0.01）而不是零
```

代价是训练早期不稳定，需要 warmup 学习率保护。

**改动 4：per-core CPI head 加 SiLU/tanh scale**

当前 head 最后一层是无激活的 Linear。这允许输出任意大值，但也允许"输出接近
mean"。可以加一个 learnable per-core scale：

```python
class PMURegressionHead:
    def forward(self, query_hidden, core_mask):
        h = ...  # head main path
        cpi_out = self.cpi_head(h)  # [B, N, 1]
        # 学一个 per-core CPI residual scale
        cpi_scale = torch.sigmoid(self.cpi_scale_gate(h)) * 2.0 + 0.5
        # scale ∈ [0.5, 2.5]，让 head 自己学核间放大系数
        return cpi_out * cpi_scale
```

这是可选，风险中等。如果 Learn 得当能强化核间差异，学不好会引入不稳定。

### 4.3 训练课程改造

**改动 1：Warmup 只用高 spread 样本**

前 500-1000 step 只训 `label_log_std > 0.30` 的窗口。让模型先看到核间差异
明显的例子，学习"预测差异是必要的"。之后按 workload 分布正常训练。

**改动 2：塌缩早停诊断**

训练 log 里已经有 `pred_log_cpi_std / label_log_cpi_std` 指标。加一个早停
规则：

```python
if step > 2000 and pred_cv_ratio < 0.4:
    # 塌缩，重启训练或降低 learning rate
    raise CollapseDetected
```

避免训到 8000 step 才发现塌缩了。

## 5. 分步实施

### 5.1 阶段 A：最小 loss 改动（1 天）

只做改动 1-3：Huber δ 缩小、L_cycles per-core、L_centered_cpi 去 gate。

从 v22 ckpt fine-tune 1000-2000 步。看 `pred_cv/label_cv` 和
`W_ads_ranking_proxy` c08 pVr。

**决策点**：
- CV ratio 从 0.30 升到 >0.5：塌缩确实是 loss 问题，进入阶段 B 优化
- CV 上升不明显：塌缩根源在表征或 head，进入阶段 C
- CV 显著下降：改动破坏了训练，回滚

### 5.2 阶段 B：加 L_rank（1 天）

在阶段 A 基础上加 pairwise rank loss。同样 1000-2000 步 fine-tune。

**期望**：CV ratio 到 0.7+，`W_ads_ranking_proxy` c08 pVr 从 34% 降到 20% 以下。

**如果没到期望**：进入阶段 C。

### 5.3 阶段 C：Head 改造（3 天）

加 core_id embedding + residual centering + local_bind_fuse.alpha=1.0。

因为改了 head 结构，不能从 v22 ckpt fine-tune，需要从头训 6000-8000 步。

**验收**：c04/c08/c16/c32 全 workload eval。

### 5.4 阶段 D（如需要）：训练课程

加 warmup 高 spread 采样、塌缩早停。这些是稳定性辅助，只在前三阶段结果
不稳时加。

## 6. 需要看的指标

不能只看 val loss。训练时定期打印：

```text
pred_log_cpi_std       # 塌缩指标：pred 侧核间 CPI std
label_log_cpi_std      # label 侧核间 CPI std
pred_cv_ratio          # pred_std / label_std，塌缩时 <0.5
L_rank                 # 排序 loss，塌缩时接近 log(2)
L_centered_cpi         # 去 gate 后应该稳定 >0.05
order_acc              # pair 排序准确率，塌缩时接近 0.5
```

Eval 时按 workload 分层报：

```text
pred_vs_roi_cpi_uop    # 全局 CPI 精度（旧主 KPI）
pred_cv_ratio_per_wl   # 每个 workload 的塌缩程度
worst_core_pVr         # 每个 workload 最差核的 CPI 误差（新 KPI）
```

## 7. 与 v24（思路 2）的关系

- **v23（本文档）**：只改 loss + head，不换 base model，不改 tokenizer
- **v24**：换 Qwen-Coder + macro 汇编，同时叠加 v23 反塌缩改造

v23 优先做的理由：

- 工程代价小（几百行代码）
- 风险低（不动数据集）
- 结果能独立验证塌缩是否可解
- 如果 v23 成功，v24 更有意义（在稳定 baseline 上验证语义先验的独立收益）
- 如果 v23 失败，v24 也一定失败——塌缩根源比 loss/head 更深，需要重设计

## 8. 不做的事

- **不换 base model**（v22 = Qwen3-0.6B-Base 保留）
- **不改 tokenizer**（不加 macro 汇编）
- **不重建数据集**（v22 的 windows.jsonl 和 tensor cache 沿用）
- **不动 backbone LoRA**（分层解冻是 v24 的事）
- **不动 planner**（部署侧 pred/label/tq_forward 切窗逻辑保留）

## 9. 主要风险

**风险 1：塌缩根源在表征端而不在 loss/head**

v18 数据显示 `query_pair_cos = 0.997`，说明 tail query hidden 几乎无差异。
即使 head 有 core_id + centering、loss 有 rank，也可能因为 hidden 差异
太小，head 只能靠 core_id embedding 强行"造"差异，出现"预测差异存在但
不跟随实际"。

**缓解**：阶段 C 加 head 改造时，同时监控 `hidden_diff` 和 `pred_diff` 的
相关性。如果相关性弱说明 hidden 是死信息，需要走 v24 或 local-core。

**风险 2：Rank loss 让训练不稳定**

Rank loss 对高 spread 窗口权重大，可能让模型过度关注 outlier 忽略常规
窗口。

**缓解**：`lambda_rank=0.5` 起步，观察 val loss；如不稳定降到 0.2。

**风险 3：Huber δ=0.02 训练早期梯度过大**

塌缩发生前的初始 loss surface 会更陡，可能发散。

**缓解**：warmup 期用 δ=0.05，warmup 结束后降到 0.02。

**风险 4：全 workload 精度略降换 outlier 改善**

反塌缩后可能出现"以前很准的低 spread workload 变差 1-2pp，以前很差的高
spread workload 从 30% 降到 15%"这种权衡。

**缓解**：接受这个权衡。目标是每核 CPI 单独准，不是全局 CPI 数字好看。

## 10. 一个更根本的观察

v17-v22 每次都在动一个端（v17/v18 loss，v19 表征，v22 表征），结果都是
其他端拉回。**反塌缩必须同时动 loss + head**。

这不是说必须一次改所有东西——阶段 A/B/C 分步是让每步都能诊断——但**最后
状态必须是三端一致**。

v22 现有代码已经有 rank_gap、rank_tau、spread_min_std 等参数（`train/loss.py`
构造函数保留），但 `self.lambda_rank = 0.0` 硬编码为不激活。v23 相当于把
这些参数打开并调整默认值，不是引入全新机制。

从代码工作量看，v23 主要是**参数调整 + 补几个 loss 项 + head 加一个
embedding**。核心机制在 v17/v18 就实验过，只是当时权重设置不对导致机制被
淹没。v23 是"把已有机制真正激活"，不是发明新东西。
