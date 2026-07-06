# v25: 自训小 Transformer + Local-core（脱离 LLM）

状态：设计稿。目标是**验证 Qwen backbone 是否值得保留**。从零训 25M 参数的
小 transformer 替换 Qwen-0.6B + LoRA，其他所有 v23 反塌缩改造保留。这是当前
LLMSim 项目**从未做过的对照实验**——之前所有版本对比都是 Qwen 内部的
tokenizer/head/loss 变化，没有跑过"同规模自训 transformer"对照，Qwen 的
实际贡献一直是猜测。

## 1. 出发点

### 1.1 Qwen 当前的实际贡献

v22 的 Qwen3-0.6B-Base backbone：

| 组件 | 参数量 | 训练状态 | 语义激活 |
|---|---:|---|---:|
| 原生 token embedding (152k vocab) | 155M | 冻结 | 0% (输入是 1470 个 special token) |
| 28 层 attention/FFN | 400M | 冻结 + LoRA rank=32 | - |
| LM head | 155M | 冻结 | 未使用 |
| **LoRA 可训** | **7.3M** | 训练 | - |
| **PMU head 可训** | **~1M** | 训练 | - |
| **新 special token embedding** | **1.5M** | 训练 | 从零学 |

**结论**：v22 实际可训参数约 11M，Qwen 提供的 590M 冻结权重的贡献**从未
独立验证过**。历代方案都在 Qwen 内部改动，没有对照。

### 1.2 Qwen 提供的能力

- **代码语义先验**：`0%` 被利用（输入全是 special token）
- **32k 长上下文**：v22 用了（全局模式），但 local-core 下不需要
- **通用 attention pattern**：可能被 LoRA 修正后利用，但不可测
- **多 head 分工**：Qwen 有 16 heads，具体分工没审视

从这些能力看：
- **语义先验**在当前 tokenizer 下不可用（需要 v24 macro 汇编才可用）
- **32k 上下文**在 local-core 下多余
- **attention pattern / head 分工**是唯一可能有用的东西，但从零训 transformer
  也能学出来（如果数据充分）

### 1.3 从零训 25M 是否足够

学界经验：25M 参数 transformer 在 40k 样本 × 20k token/sample = 800M token
数据量下，属于**充分训练规模**。相比自然语言 LLM 预训练（T 级 token）微小，
但对下游 fine-tune 任务是常规规模。

**关键约束不是参数量，是任务的监督密度**：

- Next-token prediction 监督：每个 token 位置一个 loss，训练信号密度极高
- Window-level PMU 回归：一个 sample 只有 4-32 个 loss 值，密度是 next-token
  的 1000-10000 分之一

监督稀疏是从零训 transformer 的**真正难点**，比参数量或数据量更棘手。这个
问题在**任何模型规模**下都存在（Qwen 也一样），但 Qwen 的**预训练权重**是
"从密集监督任务学好的"，作为起点更有利。

### 1.4 v25 想回答的问题

- **问题 1**：Qwen 冻结 backbone 的 590M 权重对 PMU 任务有多少贡献？
- **问题 2**：如果贡献小，切自训 transformer 能否带来 latency 10-20x 改善？
- **问题 3**：从零训 transformer 在稀疏监督下能否稳定训练？

**这三个问题必须实验回答，不能推理出来**。之前一直没做过对照。

## 2. 与 v23 / v24 的关系

三个版本的位置：

| 版本 | Backbone | Tokenizer | Loss/Head | 目标 |
|---|---|---|---|---|
| v22（当前） | Qwen-0.6B + LoRA | Composite uop token | 有塌缩 | Baseline |
| v23 | Qwen-0.6B + LoRA | 同 v22 | **反塌缩改造** | 独立验证塌缩可解 |
| v24 | Qwen2.5-Coder + LoRA | **Macro 汇编** | v23 反塌缩 | 激活 LLM 语义 |
| **v25 (本)** | **自训 25M transformer** | 同 v22 | v23 反塌缩 | 验证是否需要 LLM |

**v25 依赖 v23**：塌缩问题和 backbone 无关，v25 必须包含 v23 的所有 loss/head
改造。不然从零训 transformer 也会塌缩，实验就无意义。

**v25 和 v24 互斥**：v24 假设 LLM 语义有用，v25 假设 LLM 语义没用。二者只能
选一个方向长期演进。**做完 v25 才能给出这个假设的实证答案**。

**推荐顺序**：v23 → v25 → 根据 v25 结果决定 v24 是否值得做

- 如果 v25 精度接近甚至超过 v23 (Qwen 版本)：**Qwen 没有贡献**，v24 不做，
  长期路线是自训 transformer + 数据/结构改进
- 如果 v25 精度明显差 v23（3pp+）：**Qwen 有贡献**，v24 值得做，验证是否
  能通过语义激活拿到更多收益

## 3. 架构设计

### 3.1 Transformer backbone

```text
n_layers    = 12
d_model     = 384
n_heads     = 8
head_dim    = 48
ffn_dim     = 1536     (= 4 × d_model)
dropout     = 0.1
```

**参数量估算**：

- Attention (4 × d²)/layer: 590k
- FFN (2 × d × ffn_dim)/layer: 1.18M
- LayerNorm: negligible
- Per layer: ~1.77M
- 12 layers: **21M**
- + Embedding (1500 × 384): 0.6M
- + Head (借用 v23 head 结构，约 1M): 1M
- + Cross-core adapter (2-layer, d=384): 1.2M
- **总计约 24M 可训参数**

对比 v22 的 11M 可训 + 590M 冻结 = 601M 总量。v25 是 v22 总量的 4%，可训量
的 2.2x。

### 3.2 归一化和残差

- **Pre-norm**（QKV/FFN 之前 LayerNorm，稳定长序列训练）
- **RMSNorm**（比 LayerNorm 快，Llama/Qwen 都用）
- 残差路径无归一化

### 3.3 位置编码

**RoPE (Rotary Position Embedding)**：

- 无学习参数，旋转矩阵由数学公式生成
- `base_theta = 10000`（Qwen/Llama 默认值）
- 应用于 Q 和 K 的每对相邻维度
- Local-core 下每核序列 5-6k token，长距离位置差在最低频周期内可精确编码

**为什么选 RoPE 而不是 ALiBi**：

- **LLMSim 需要长距离 attention**：`<QUERY_Ci>` 在序列末尾，`<CFG>` /
  `<GLOBAL>` 在开头，本核 5k local 序列里也有 3k+ 距离。Query 必须能
  attend 到 cfg 和跨核摘要。
- **ALiBi 的距离衰减 bias（`-m × distance`）是硬先验**，会系统性抑制长
  距离 attention。ALiBi 只有小部分低斜率 head 能看远，剩下 head 被限制在
  局部——与 LLMSim 里"所有 head 都需要看到 cfg 和 global summary"的需求
  冲突。
- **RoPE 无位置衰减先验**：模型自由学 attention pattern，长距离依赖是否
  重要由数据决定。
- **不需要外推能力**：训练和部署长度一致（local-core 下都是 5-6k），ALiBi
  的外推优势对本项目零价值。
- **和 Qwen 一致**：backbone 对比时位置编码保持相同，隔离变量更干净。

从零训 RoPE 有"长距离 attention 学不好"的理论担忧（依赖低频维度信号密度），
但相比 ALiBi 的机制性冲突，RoPE 是中立且更适合的选择。

### 3.4 Local-core 结构

**每核序列**（复用 v22 tokenizer，每 uop 1 composite token）：

```text
[SYS] [cfg] [跨核 features]
<C_self_BEGIN>
  <C_self summary>
  <UOP> <UOP> <UOP> ...   # W=1024 uops per core
  <LOCAL_C_self>
<C_self_END>
```

- 单核序列长度约 **4-5k token**（W=1024 uops + prefix + tail）
- 相比 Qwen 全局模式的 32k，attention 复杂度 5² / 32² ≈ **1/40**

**Forward 路径**：

```python
def forward(local_input_ids, local_query_pos, ...):
    # [B, N_core, L_local]
    B, N, L = local_input_ids.shape
    
    # Flatten 到 batch dim
    flat_ids = local_input_ids.view(B * N, L)
    hidden = self.backbone(flat_ids)              # [B*N, L, D]
    
    # Gather LOCAL_Ci hidden
    query_hidden = gather_at_query_pos(hidden)    # [B*N, D]
    query_hidden = query_hidden.view(B, N, D)      # [B, N, D]
    
    # Cross-core adapter (mask-aware self-attention over cores)
    adapted = self.cross_core_adapter(
        query_hidden, core_mask
    )                                              # [B, N, D]
    
    # PMU head (v23 反塌缩改造版)
    return self.head(adapted, core_mask)          # [B, N, K]
```

### 3.5 Cross-core adapter

2 层 transformer encoder over cores：

```text
n_layers    = 2
d_model     = 384    (与 backbone 对齐)
n_heads     = 4
ffn_dim     = 768
```

- Mask-aware self-attention over N_core tokens
- 支持任意 N (c04, c08, c16, c32)
- 参数无 N 依赖
- 输出 gating: `adapted = query_hidden + gate × attn_out(query_hidden)`
- gate 初值 0.5（不是 0，避免训练早期 adapter 死掉）

**为什么 gate=0.5 而不是 zero init**：v22 的 `local_proj`/`side_proj` 零初始化
的教训——早期塌缩 basin 形成后，即使后期 adapter 开始有信号，也已经卡在
塌缩。gate=0.5 让 adapter 从训练第一步就有影响。

### 3.6 Head

完全继承 v23 反塌缩改造：

- 三独立 head（CPI / branch / cache），每个 2 层 MLP
- **Core identity embedding** 注入 head 输入
- **Residual centering**：`h - detach(mean_core(h))` + `mean_core(h)` 显式拼接
- 参数量约 1M

详细见 `docs/pre_v23_anti_collapse.md`。

### 3.7 Tokenizer 选择：沿用 v22

**不切换到 macro 汇编**（v24 思路）。理由：

- 自训 transformer 没有 Qwen 原生词表要激活，macro 汇编的核心优势消失
- v22 composite uop token 每 uop 只占 1 个位置，**信息密度比 macro 汇编高**
- 复用现有 tokenizer 和数据 pipeline，工程量最小
- Special token 从零学 embedding 对自训 transformer 是**同等待遇**（Qwen 是
  混合词表，自训是全 special token）

**新加特殊 token 数**：沿用 v22 的 1470 个。

**Embedding 初始化**：从零训 transformer 的 embedding 从头初始化（Gaussian
std=0.02），不复用 Qwen embedding（那些权重对 special token 无意义）。

## 4. 训练策略

### 4.1 优化器和学习率

```text
optimizer     = AdamW
weight_decay  = 0.05
betas         = (0.9, 0.95)
lr_backbone   = 3e-4
lr_head       = 3e-4  (与 backbone 相同，因为都是从零训)
lr_adapter    = 3e-4
warmup_steps  = 1000
schedule      = cosine to 10% of peak
grad_clip     = 1.0
```

**关键**：从零训比 fine-tune 需要更大 lr。3e-4 是 transformer 从零训的通用值。
v22 的 lr_lora=5e-5 是 fine-tune 值，不适用。

### 4.2 训练步数和 batch

```text
steps         = 12000-16000
batch_size    = 8 per GPU × 8 GPU = 64 global
grad_accum    = 1
```

从零训需要更多步数收敛。相比 v22 的 8000 步，v25 建议 12000+ 步。

### 4.3 数据增广

40k 样本 × 25M 参数容易过拟合。加：

- **Dropout**：backbone 每层 0.1（相比 v22 的 0.0）
- **Attention dropout**：0.1
- **Register bucket shuffle**：训练时对 reg_bucket 做随机 permutation（保持
  依赖结构，破坏具体 reg id 过拟合）
- **Cacheline bucket shuffle**：同上对 line/page bucket

### 4.4 初始化

**权重初始化**：

- Attention/FFN linear: Gaussian, std=0.02 (与 GPT-2 一致)
- Output projection: 缩放到 `std / sqrt(2 × n_layers)` (DeepNorm/GPT-2 做法)
- LayerNorm: 初值 1，bias 初值 0

**Embedding 初始化**：

- Token embedding: Gaussian, std=0.02
- RoPE 无学习参数（旋转矩阵由公式生成）

**Head 初始化**：

- Hidden 层: Gaussian std=0.02
- 输出层: 小值 (std=0.01)，避免早期输出过大

### 4.5 Curriculum

**Warmup 阶段（前 1000 步）**：

- 只训 W_compute_int / W_indirect / W_branch_storm 等**低 spread 简单**workload
- 让模型先学基础 pattern，avoid 塌缩到均值先形成
- 学习率 warmup 到 3e-4

**主训练阶段（1000-10000 步）**：

- 全 workload 混合训练
- **高 spread 窗口过采样**：`label_log_std > 0.3` 的窗口权重 2x

**Cooldown 阶段（10000-12000 步）**：

- 学习率 cosine 衰减到 3e-5
- 只在 fine-tune 微调，不改变 pattern

### 4.6 塌缩早停诊断

前面 v23 提到的机制在 v25 里更关键：

```python
if step > 2000 and pred_cv_ratio < 0.3:
    # 塌缩再次发生，说明架构本身不足以避免
    raise CollapseDetected
```

从零训比 fine-tune 更容易塌缩（缺 anchor），必须持续监控。

## 5. 数据管道

### 5.1 复用 v22 数据

**不重建数据集**。使用现有：

```text
data/windows_v16_v9core_tail_local_all/windows.jsonl
data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache
```

Tokenizer 一样，token id 一样，label 一样。

### 5.2 Local-core 转换

需要生成 local-core 版本的 cache（v19 已实现代码，路径`_build_local_core_sequences`
在 `train/dataset.py:161`）：

```bash
python scripts/prepare_dataset_cache.py \
  --data data/windows_v16_v9core_tail_local_all/windows.jsonl \
  --max-len 8192 \
  --format tensor \
  --input-mode local_core \
  --cache-out data/windows_v16_v9core_tail_local_all/windows.maxlen8192.local_tensor_cache \
  --jobs 8
```

**max-len 从 32768 降到 8192**：local-core 单核序列约 5k token，8k 足够，
省训练显存。

## 6. 工程组织

### 6.1 新增 model class

新增文件 `model/tiny_transformer.py`：

```python
class TinyTransformer(nn.Module):
    """From-scratch transformer for LLMSim, replaces Qwen backbone."""
    
    def __init__(self, cfg: TinyTransformerConfig, tokenizer):
        # Embedding + 12-layer transformer + RoPE
        ...
    
    def forward(self, input_ids, attention_mask=None):
        # 返回 hidden states, 接口和 HF backbone 兼容
        ...
```

**关键**：接口和 `AutoModel` 输出兼容（返回 `last_hidden_state`），这样
`LLMSimModel.forward` 里的 gather/head 逻辑不用改。

### 6.2 修改 LLMSimModel

`model/llm_wrapper.py` 加分支：

```python
class WrapperConfig:
    base_model: str = "Qwen/Qwen3-0.6B-Base"  # 或 "tiny_transformer"
    ...

class LLMSimModel(nn.Module):
    def __init__(self, cfg, tokenizer):
        if cfg.base_model == "tiny_transformer":
            from model.tiny_transformer import TinyTransformer
            self.backbone = TinyTransformer(cfg.tiny_cfg, tokenizer)
            # 不加 LoRA，直接全量训
        else:
            # 原 Qwen + LoRA 路径
            ...
```

### 6.3 训练脚本调整

`scripts/run_v25_tiny_transformer_qwen0p6_replacement.sh`（新增）：

```bash
BASE_MODEL=tiny_transformer \
CACHE_PATH=data/windows_v16_v9core_tail_local_all/windows.maxlen8192.local_tensor_cache \
MODEL_INPUT_MODE=local_core \
LOSS_WEIGHT_MODE=fixed \
LAMBDA_CPI_ABS=1.0 LAMBDA_CENTERED_CPI=1.0 LAMBDA_RANK=0.5 \
HUBER_DELTA=0.02 \
STEPS=12000 WARMUP_STEPS=1000 \
LR_BACKBONE=3e-4 LR_HEAD=3e-4 \
BATCH_PER_GPU=8 GPUS=8 \
OUT=ckpt/v25_tiny_transformer_local_core_12000 \
./scripts/train_current6_ddp8.sh
```

### 6.4 Eval pipeline

`eval/eval_quota_cycles.py` 需要检查 checkpoint 加载路径：

- 读 checkpoint meta 里的 `base_model` 字段
- 如果是 `tiny_transformer`，构建 TinyTransformer 而不是 AutoModel
- Forward 路径完全一致（因为接口兼容）

**工作量**：eval 侧改动 <100 行。

## 7. Two-stage cross-core attention（P1 扩展，不在 default）

**默认 v25 不包含 two-stage**。作为可选扩展，只在下面条件下启用：

- v25 default 训练完成，全 workload eval 已跑
- 发现 W_false_sharing / W_ads_ranking_proxy 类跨核依赖 workload 精度明显
  低于 v22（差 5pp+）
- 说明 local-core basic 的跨核信号损失确实存在

**Two-stage 结构**：

```
Layers 0-5:      每核独立 forward (stage 1)
Cross-core mem attention layer (stage 2):
  收集每核 mem uop 位置的 hidden
  跨核 sparse attention，只在 (不同核 + 相同 cacheline bucket) 的 pair
  scatter 回原位置
Layers 6-11:     每核继续 forward (stage 3)
```

**变长 mem uop 处理**：

- Flatten 所有 sample × core × mem_uop 到一维序列 [Total_mem, D]
- 用 `(sample_id, core_id, bucket_id)` 三元 mask 控制 attention
- Block-sparse attention 实现（FlashAttention-2 varlen 模式）

**时序对齐**：

- 每个 mem uop 附加 "本核内 relative uop position" (0-1 归一化)
- 作为 attention 的 pair-wise bias 输入
- 不追求精确 wall-clock 对齐，只做**结构层面**的跨核连接

**代价**：

- 新增 2 层跨核 attention 参数 (~3M)
- 训练每 step 多 30% 时间（stage 2 是稀疏但序列长）
- 显存增加约 20%

**收益**（预估，需实验验证）：

- W_false_sharing 类 pVr 从 basic 的 8-15% 降到 3-5%
- W_ads_ranking_proxy 从 24-34% 降到 15-20%
- 其他 workload 无明显变化

这一步只有在**basic 版本明显不够**时才做。

## 8. 主要风险

**风险 1：从零训 attention pattern 学不出来**

从零训 transformer 在 5k 上下文 + 稀疏监督下，attention 可能学不到有效
long-range pattern，退化成局部 window。

**表现**：训练 loss 下降但 val 精度不升，或核间预测极不稳定。

**缓解**：
- 用 RoPE 保持位置编码中立（不像 learned position 需要额外训练，也不像
  ALiBi 引入距离衰减硬先验）
- Warmup 用简单 workload 打基础
- 监控 attention entropy（塌到均匀是 fail signal）
- 如果确实学不出来，回退：加 pretraining stage（用 next-token prediction on
  trace token sequences 做辅助监督）

**风险 2：训练不稳定**

从零训 + 稀疏监督 + 深度 12 层，梯度可能爆炸或消失。

**缓解**：
- Pre-norm + RMSNorm
- DeepNorm 缩放 output projection
- Grad clip 1.0
- Warmup 学习率 1000 步

**风险 3：塌缩重现**

尽管有 v23 loss/head 改造，从零训的 head 也可能重新塌缩。

**缓解**：
- 严格遵守 v23 的所有反塌缩机制（Huber δ=0.02, rank loss, core_id, centering）
- 塌缩早停诊断（step > 2000, pred_cv_ratio < 0.3 就停）

**风险 4：精度显著差 v23 (Qwen 版本)**

如果 v25 精度比 v23 差 3pp+，说明 Qwen 的 590M 冻结权重确实提供了自训
transformer 学不出来的能力。

**这是 valid 的实验结果，不是失败**：告诉我们 Qwen 值得保留，然后走 v24
路径。

**风险 5：数据不够**

40k 样本对 25M 参数可能不够，导致过拟合。

**缓解**：
- Dropout 0.1
- Data augmentation (register/cacheline bucket shuffle)
- Weight decay 0.05
- 如果过拟合明显，减模型到 12M

**风险 6：Adapter 学不好**

Cross-core adapter 从零学，需要有效梯度。gate=0.5 初值，adapter 一开始就
参与前向，希望有梯度信号。

**缓解**：
- 训练监控 adapter output norm 和梯度 norm
- 如果 adapter 死掉（output ≈ 0），提高 gate 初值到 1.0 + zero init 输出层

## 9. 部署侧影响

### 9.1 Latency

**估算**：

- 25M 参数 transformer, 5k context, sdpa, bf16, A100:
  - 单次 forward: ~4-6ms
- Local-core c08: 8 次 forward × 5ms = **40ms/窗**
- 相比 v22 Qwen c08: 76ms/窗，**改善 ~2x**
- 相比 v22 Qwen c32: 432ms/窗（如果能装下）→ v25 c32 32 × 5ms = **160ms/窗**，
  **改善 2.7x**

**Latency 改善不是 10-20x 那么大**——local-core 下每窗要 forward N_core 次，
增益被 forward 次数抵消一部分。但仍然显著。

### 9.2 显存

- v22 Qwen c08: 32k context, ~15GB per sample
- v25 c08: 5k context × 8 forward (batch), ~4GB per sample
- **改善 ~4x**

### 9.3 训练时间

- v22 训练 8000 step: ~4 小时（8 A100）
- v25 训练 12000 step:
  - 每 step forward 快（5k vs 32k, 40x attention 复杂度差）
  - 但 forward 次数 N_core 倍
  - c04/c08 混合训练，平均 ~6 次 forward per sample
  - 总时间估计: v22 的 40% × 12000/8000 = **~2.5 小时**

## 10. 分阶段实施

### 阶段 A：架构和 pipeline（3-4 天）

- 写 `model/tiny_transformer.py`
- 修改 `LLMSimModel` 支持 backbone switch
- 生成 local-core cache
- 修改 train/eval 脚本
- Smoke train 100 step 验证 forward/backward 正常

### 阶段 B：Warmup 训练验证（2 天）

- 训 1000 步 warmup（低 spread workload）
- 检查 loss 下降、attention pattern、gradient norm
- 检查塌缩早停诊断

### 阶段 C：完整训练（1-2 天）

- 12000 步完整训练
- 8 GPU DDP
- 期望 wall time ~3 小时

### 阶段 D：全 eval + 对比（1 天）

- c04/c08/c16/c32 seedB full eval
- 对比 v23 Qwen 版本相同 seedB 结果
- 报告：全局 CPI 精度、per-core CPI CV、workload-level breakdown

### 阶段 E：决策

**如果 v25 精度接近 v23**（mean pVr 差 < 1pp）：
- Qwen 无贡献，切自训 transformer 是长期方向
- 后续投入方向：更大数据集、更好架构（two-stage、更深层）、更好训练策略
- v24 不做（不需要 LLM 语义先验）

**如果 v25 精度明显差 v23**（差 3pp+）：
- Qwen 有贡献，backbone 保留
- 分析 Qwen 到底提供了什么（attention pattern? 通用序列建模?）
- v24 值得做，尝试进一步激活语义

**如果 v25 精度好于 v23**：
- 说明 Qwen backbone 是**负作用**（自然语言 attention pattern 不适合此任务）
- 完全切自训，v24 也不做

## 11. 不做的事

- **不改 tokenizer**（沿用 v22）：换 base model 已经是大变量，同时改 tokenizer
  无法归因
- **不重建数据集**：现有 windows.jsonl 和 tensor cache 沿用
- **不改 planner**：部署侧切窗逻辑保留
- **不做 two-stage cross-core**（除非 basic 明显不够）：控制变量
- **不改 loss 结构**：完全继承 v23 反塌缩
- **不改数据集覆盖**：训练用同一组 8-17 workload

## 12. 需要的对照

v25 的价值来自对比。跑完 v25 需要有：

**必需对照**：
- v23 c04/c08/c16/c32 seedB full eval（Qwen backbone + 反塌缩）
- v25 c04/c08/c16/c32 seedB full eval（自训 transformer + 反塌缩）

**期望对照**：
- v22 c04/c08/c16 seedB full eval（Qwen backbone + no 反塌缩，历史 baseline）
- v25 c04/c08/c16 前置 no-adapter 变体（了解 adapter 的独立贡献）

**指标**：

```text
mean_pred_vs_roi_cpi_uop
median_pred_vs_roi_cpi_uop
max_pred_vs_roi_cpi_uop
per-workload pred_vs_roi_cpi_uop
per-core CPI CV ratio (pred_cv / label_cv)
worst-core pVr per workload
```

## 13. 一个更大的观察

v9-v22 的所有版本演进都是在 Qwen backbone 上做变化。**LLMSim 项目从来没有
质疑过 Qwen 是否合适**。文档 `docs/design.md` 第 6 节明确说 "LLM 的价值非
充分"，但项目实际路径把这个警告忽略了。

v25 是**第一次**做这个对照实验。无论结果如何，它给出的答案是有价值的：

- 如果 Qwen 没贡献：省下大量 latency 和显存，长期投资方向明确
- 如果 Qwen 有贡献：知道 Qwen 具体贡献什么，v24 有明确目标
- 如果 Qwen 是负作用：撤退到自训是长期正确

**这个实验的信息价值高于任何单一精度改进**。之前的 v17-v22 迭代结果显示，
"改一个东西看是否提升"这条路已经边际收益递减。v25 是**方向性验证**，不是
增量改进。
