# v25a: 自训 Transformer 第一版（11M 严格对齐）

状态：实施稿。作为 v25 系列的第一步单点实验，验证"Qwen 冻结 backbone 的
590M 权重对 PMU 任务是否有实际贡献"。设计原则是**严格控制变量**，只换
backbone，其他和 v22 完全一样。

## 1. 目标

回答一个之前从未直接验证过的问题：**Qwen3-0.6B backbone 相比同等可训
参数量的从零训 transformer，精度差距有多大**？

历代 v9-v22 都在 Qwen 内部改动（tokenizer、head、loss、切窗），从来没有
把 backbone 换掉过。所有关于"Qwen 值不值得留"的讨论都是猜测。v25a 是
LLMSim 项目**第一次**做这个 backbone 对照。

**注意**：这一版**不能证明** LLM 的"语义先验"是否有用（当前 tokenizer 是
1470 个自定义 special token，Qwen 的自然语言/代码 embedding 无法激活）。
它只能验证 **Qwen 的 attention/FFN 预训练权重是否对 PMU 任务有帮助**。
如果要验证语义先验，是 v24 的目标。

## 2. 与其他版本的关系

| 版本 | Backbone | Loss/Head | Tokenizer | 状态 |
|---|---|---|---|---|
| v22（基线） | Qwen3-0.6B + LoRA rank=32 | v22 loss + 三 head split | 1470 special token | 已有 |
| v23 | Qwen3-0.6B + LoRA rank=32 | 反塌缩 loss + 反塌缩 head | 沿用 v22 | 独立设计稿 |
| **v25a（本文档）** | **自训 12M transformer** | **完全同 v22** | **沿用 v22** | 实施稿 |
| v25 后续 | 自训 + 其他改造 | 待定 | 待定 | 长期规划 |
| v24 | Qwen2.5-Coder + LoRA | 反塌缩 loss + 语义辅助 | Macro 汇编 | 独立设计稿 |

**依赖关系**：
- v25a 不依赖 v23（不需要 v23 完成才能做）
- v25a 结果决定 v24 是否值得做（如果 v25a 精度接近 v22，说明 Qwen 无贡献，
  语义先验可能也无用，v24 优先级降低）
- v25a 完成后可以选择叠加 v23 反塌缩改造（作为 v25b），但**第一版不叠加**，
  保持归因干净

## 3. 严格控制变量清单

**唯一变量**：backbone 从 Qwen3-0.6B + LoRA 换成从零训 12M transformer。

**保持不变**：

- Windows.jsonl 数据（`data/windows_v16_v9core_tail_local_all/windows.jsonl`）
- Tensor cache（`windows.maxlen32768.tensor_cache`）
- Tokenizer（1470 special token，`model/tokenizer.py` 完全不动）
- UopEncoder 结构（`model/llm_wrapper.py:UopEncoder`，6 字段 embedding + MLP）
- Head 结构（`model/regression_head.py`，三个 2 层 MLP：cpi_head、branch_head、
  cache_head）
- Loss（`train/loss.py`，`lambda_cpi_abs=1.0`, `lambda_cycles=1.0`,
  `lambda_aux_pmu=0.05`, `lambda_centered_cpi=0.3`, Huber δ=0.1，都不改）
- 切窗逻辑（部署侧 `eval/eval_quota_cycles.py` 完全不动）
- 训练数据分布（8 workload 训练集）

## 4. Backbone 架构

### 4.1 参数配置

```text
d_model      = 320
n_layers     = 8
n_heads      = 8
head_dim     = 40
ffn_dim      = 1280      (= 4 × d_model)
dropout      = 0.1
max_len      = 32768     (对齐 v22 max_len)
```

**参数量估算**：

- Attention/layer: 4 × 320² = 410k
- FFN/layer: 2 × 320 × 1280 = 819k
- 每层合计: ~1.23M
- 8 层 backbone: **9.8M**
- Token embedding (vocab 152k × 320): 48.6M（**不训所有词，仅新加 1470
  special token 参与训练**，等价于 v22 的处理）
- 实际参与优化的 embedding: 1470 × 320 = 470k
- 剩余部分（v22 UopEncoder + head + adapter，都同 v22）: ~1.5M
- **backbone + 新 emb 总可训参数 ≈ 11.8M**（对齐 v22 的 11M）

**注意**：v22 中 152k vocab embedding 是冻结的（Qwen 原生 token），只有
新加 1470 special token 行可训。v25a 中我们只关心 special token embedding
（其他 vocab 位置永远不会被激活，因为输入序列不含 Qwen 原生 token）。

### 4.2 Norm 和残差

- **Pre-norm**：QKV/FFN 输入前 RMSNorm
- **RMSNorm**（比 LayerNorm 快约 20%）
- 残差路径无 norm

### 4.3 位置编码

**RoPE (Rotary Position Embedding)**：

- 无学习参数（旋转矩阵由数学公式生成）
- `base_theta = 10000`（Qwen/Llama 默认值）
- 应用于每个 attention head 的 Q 和 K
- 32k 上下文下最低频维度周期 ≈ 62.8k，长距离位置差可精确编码，无需 NTK
  scaling

**为什么选 RoPE 而不是 ALiBi**：

- **LLMSim 需要长距离 attention**：`<QUERY_Ci>` 在序列末尾，`<CFG>` 在开头，
  距离 20k+ 位置。Query 必须能 attend 到 cfg 才能预测 CPI。
- **ALiBi 的距离衰减 bias 是硬先验**（`bias = -m × distance`），会系统性
  抑制长距离 attention。ALiBi 只有小部分 head 是低斜率能看远，剩下 head
  被限制在局部——与 LLMSim 里"所有 head 都需要看到 cfg"的需求冲突。
- **RoPE 无位置衰减先验**：模型自由学 attention pattern，长距离依赖是否重要
  由数据决定，不是由架构预设。
- **不需要外推能力**：训 32k 部署 32k，ALiBi 的外推优势对本项目零价值。
- **和 v22 Qwen 位置编码一致**：backbone 对比时位置编码层保持相同，隔离
  变量更干净。

从零训 RoPE 有"长距离 attention 学不好"的理论担忧（依赖低频维度信号密度），
但相比 ALiBi 的机制性冲突，RoPE 是**中立且更适合的**选择。

### 4.4 Attention

- Multi-head standard attention
- PyTorch `scaled_dot_product_attention` (sdpa)，自动选 FlashAttention 内核
- RoPE 在 QK 计算前应用（旋转 Q 和 K 的每对相邻维度）
- Causal mask（保持和 Qwen 一致的 causal 结构）

### 4.5 输出接口

**必须与 HuggingFace `AutoModel` 完全兼容**：

```python
class TinyTransformer(nn.Module):
    def forward(self, input_ids=None, inputs_embeds=None, 
                attention_mask=None):
        # 返回 {"last_hidden_state": [B, L, D]}
        return TinyOutput(last_hidden_state=hidden)
    
    def get_input_embeddings(self):
        return self.token_emb
    
    def resize_token_embeddings(self, new_size):
        # 兼容 model/llm_wrapper.py 的现有调用
        ...
    
    def enable_input_require_grads(self):
        pass
    
    def gradient_checkpointing_enable(self, kwargs=None):
        # 实现梯度检查点
        ...
```

这样 `LLMSimModel.forward` 的 gather / head / adapter 逻辑一行不用改。

## 5. 数据管道

### 5.1 完全复用 v22

- 数据源：`data/windows_v16_v9core_tail_local_all/windows.jsonl`
- Tensor cache：`windows.maxlen32768.tensor_cache`（v22 已生成）
- Dataset 类：`train/dataset.py:WindowDataset`，不改
- Tokenizer：`model/tokenizer.py`，不改
- Collate：`train/dataset.py:collate_fn`，不改

**不做**：

- 不重建 windows.jsonl
- 不生成新的 tensor cache（v22 的 32k 全局 cache 直接用）
- 不切 local-core（保持 v22 的全局模式）

### 5.2 输入格式

单个 sample 的输入 tensor（和 v22 完全一致）：

```
input_ids       [L]           # L ≤ 32768
attention_mask  [L]           # padding mask
query_pos       [n_core]      # <QUERY_Ci> 位置
local_pos       [n_core]      # <LOCAL_Ci> 位置
side_feats      [n_core, F]   # F=47 side feature dim
t_start_rel     [n_core]      # 每核相对起始时间(cycle)
core_mask       [n_core]      # active core mask
is_uop          [L]           # <UOP> 位置 mask
uop_fields      [L, 6]        # 每 uop 的 6 字段
label           [n_core, K]   # K=7 PMU labels
uops_per_core   [n_core]
denoms          [n_core, 6]
```

## 6. 训练策略

### 6.1 优化器和学习率

**关键差异 vs v22**：

```text
optimizer     = AdamW
weight_decay  = 0.05
betas         = (0.9, 0.95)

lr_backbone   = 3e-4      (v22 lr_lora=5e-5 太小，从零训需大 lr)
lr_head       = 3e-4      (对齐 backbone；v22 lr_head=1e-4)
lr_new_emb    = 3e-4      (新 special token embedding，从零学)
lr_uop_encoder= 3e-4

warmup_steps  = 1000      (v22 warmup=500)
schedule      = cosine to 3e-5 (10% of peak)
grad_clip     = 1.0
```

从零训需要更大 lr。3e-4 是 transformer 从零训的常用值。fine-tune 用的
5e-5 学不动。

### 6.2 训练步数

**默认 8000 步**：对齐 v22。第一版严格控制变量，包括训练预算。

**如果 8000 步 val loss 还在下降**：训到 16000 步作为 upper bound reference。
但主结论以 8000 步对比为准。

### 6.3 Batch 和 grad accumulation

同 v22：`batch_size=8 per GPU × 8 GPU = 64 global`。不用 grad accum。

**显存注意**：自训 12M 需要每层反传，比 v22 的 LoRA 反传显存高。32k 上下文
下可能吃紧：

- v22（LoRA）：反传主要在 LoRA 层，少数几百万参数
- v25a（自训）：反传全网，1230 万参数每层

如果 A100 80GB 显存不够：
- 减 batch 到 4 per GPU + grad_accum=2
- 保持 global batch=64 不变

### 6.4 初始化

**关键，不能用 default**：

```python
# Attention/FFN linear
nn.init.normal_(m.weight, std=0.02)

# Output projection (attention.o_proj 和 FFN 最后 linear)
# DeepNorm-style scaling 防止残差和爆炸
nn.init.normal_(m.weight, std=0.02 / (2 * n_layers) ** 0.5)

# Token embedding
nn.init.normal_(token_emb.weight, std=0.02)

# LayerNorm/RMSNorm
gain = 1.0
```

### 6.5 Regularization

40k 训练样本 × 12M 参数容易过拟合，加：

- **Dropout 0.1**（backbone 每层）
- **Attention dropout 0.1**（sdpa 里）
- **Weight decay 0.05**（AdamW）
- **Grad clip 1.0**

v22 因为 LoRA 只训低秩修正，过拟合风险低，dropout 和 weight decay 都是 0。
v25a 必须加。

### 6.6 Loss

**完全同 v22，一个字都不改**：

```text
lambda_cpi_abs      = 1.0
lambda_cycles       = 1.0
lambda_aux_pmu      = 0.05
lambda_centered_cpi = 0.3
huber_delta         = 0.1     (per-key，logratio 空间)
cycles_delta        = 0.1
centered_delta      = 0.1
centered_min_std    = 0.30
loss_weight_mode    = fixed
```

**故意不上 v23 反塌缩**。理由：控制变量。v25a 的比较对象是 v22，两者 loss
必须一样，不然结果无法归因。

如果想验证"反塌缩改造在自训 backbone 上是否有效"，是 v25b 的事，第一版
不做。

## 7. 工程实现

### 7.1 新增文件

**`model/tiny_transformer.py`**（新增，约 200 行）：

- `TinyTransformerConfig` dataclass
- `RMSNorm` 类
- `RoPE` 位置编码工具（Q/K 旋转，无参数）
- `TinyBlock` transformer 层
- `TinyTransformer` 主类，接口兼容 HF `AutoModel`

**关键点**：
- Forward 返回对象要有 `.last_hidden_state` 属性
- 支持 `input_ids` 和 `inputs_embeds` 两种输入路径（因为 `LLMSimModel` 里
  会用 `inputs_embeds` 注入 UopEncoder 输出）
- 实现 `resize_token_embeddings` 兼容现有调用
- 实现基础 gradient checkpointing（用 `torch.utils.checkpoint.checkpoint`
  包每个 block）

### 7.2 修改 `model/llm_wrapper.py`

**新增 config 字段**：

```python
@dataclass
class WrapperConfig:
    base_model: str = "Qwen/Qwen3-0.6B-Base"
    ...
    # 新增
    tiny_transformer: bool = False
    tiny_d_model: int = 320
    tiny_n_layers: int = 8
    tiny_n_heads: int = 8
    tiny_ffn_dim: int = 1280
```

**修改 LLMSimModel `__init__`**：

```python
if cfg.tiny_transformer:
    from model.tiny_transformer import TinyTransformer, TinyTransformerConfig
    tcfg = TinyTransformerConfig(
        vocab_size=len(hf_tokenizer),
        d_model=cfg.tiny_d_model,
        n_layers=cfg.tiny_n_layers,
        n_heads=cfg.tiny_n_heads,
        ffn_dim=cfg.tiny_ffn_dim,
        max_len=cfg.max_len,
        dropout=0.1,
    )
    self.backbone = TinyTransformer(tcfg).to(torch.bfloat16)
    self.backbone.gradient_checkpointing_enable()
    d_model = cfg.tiny_d_model
    # 不加 LoRA，全量训 backbone
    self.new_token_start = 0  # 自训所有 token 都可训
    self.n_new_tokens = len(hf_tokenizer)
else:
    # 原 Qwen + LoRA 路径
    ...
```

**修改 `_unfreeze_new_embeddings`**：

自训模式下所有 embedding 都可训，不需要 mask。

### 7.3 训练脚本参数扩展

`train/train_lora.py` 加 CLI 参数：

```python
ap.add_argument("--tiny-transformer", action="store_true")
ap.add_argument("--tiny-d-model", type=int, default=320)
ap.add_argument("--tiny-n-layers", type=int, default=8)
ap.add_argument("--tiny-n-heads", type=int, default=8)
ap.add_argument("--tiny-ffn-dim", type=int, default=1280)
```

在构建 `WrapperConfig` 时传入。

### 7.4 训练启动脚本

新增 `scripts/run_v25a_tiny_transformer_11m.sh`：

```bash
#!/bin/bash
set -e

: ${DATA:=data/windows_v16_v9core_tail_local_all/windows.jsonl}
: ${CACHE_PATH:=data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache}
: ${OUT:=ckpt/v25a_tiny_transformer_8l320_8gpu_8000}
: ${STEPS:=8000}
: ${WARMUP:=1000}
: ${LR:=3e-4}
: ${BATCH_PER_GPU:=8}
: ${GPUS:=8}

/data00/yinhaolang/infer/.venv/bin/torchrun \
  --nproc_per_node ${GPUS} \
  train/train_lora.py \
  --data ${DATA} \
  --cache-path ${CACHE_PATH} \
  --out ${OUT} \
  --steps ${STEPS} \
  --warmup-steps ${WARMUP} \
  --tiny-transformer \
  --tiny-d-model 320 \
  --tiny-n-layers 8 \
  --tiny-n-heads 8 \
  --tiny-ffn-dim 1280 \
  --max-len 32768 \
  --lr-lora ${LR} \
  --lr-head ${LR} \
  --lr-emb ${LR} \
  --batch-size ${BATCH_PER_GPU} \
  --lambda-cpi-abs 1.0 \
  --lambda-cycles 1.0 \
  --lambda-aux-pmu 0.05 \
  --lambda-centered-cpi 0.3 \
  --loss-weight-mode fixed \
  --dropout 0.1 \
  --weight-decay 0.05 \
  --grad-clip 1.0 \
  2>&1 | tee logs/train_v25a.log
```

### 7.5 Eval 脚本兼容

`eval/eval_quota_cycles.py` 加载 checkpoint 时读 meta：

```python
if ckpt_meta.get("tiny_transformer", False):
    cfg.tiny_transformer = True
    cfg.tiny_d_model = ckpt_meta.get("tiny_d_model", 320)
    cfg.tiny_n_layers = ckpt_meta.get("tiny_n_layers", 8)
    cfg.tiny_n_heads = ckpt_meta.get("tiny_n_heads", 8)
    cfg.tiny_ffn_dim = ckpt_meta.get("tiny_ffn_dim", 1280)
```

约 20 行修改。切窗、planner、PMU 聚合逻辑完全不动。

### 7.6 Checkpoint 保存

`train/train_lora.py` 保存 checkpoint 时把 tiny 相关参数写进 meta：

```python
meta = {
    ...
    "tiny_transformer": cfg.tiny_transformer,
    "tiny_d_model": cfg.tiny_d_model,
    "tiny_n_layers": cfg.tiny_n_layers,
    "tiny_n_heads": cfg.tiny_n_heads,
    "tiny_ffn_dim": cfg.tiny_ffn_dim,
    ...
}
```

## 8. 训练监控指标

**每 100 步打印**（在现有 v22 打印基础上）：

```text
train_loss              # 总 loss
L_cpi_abs               # CPI 主 loss
L_cycles                # 窗口 cycles loss
L_aux_pmu               # 辅助 PMU loss
L_centered_cpi          # 中心化 CPI loss
pred_log_cpi_std        # 塌缩指标 - pred 侧核间 std
label_log_cpi_std       # label 侧核间 std
pred_cv_ratio           # pred_std / label_std
grad_norm               # 梯度范数（不能爆炸或消失）
attention_entropy       # attention 分布熵（可选，塌到均匀是 bad signal）
```

**塌缩早停**：

```python
if step > 2000 and pred_cv_ratio < 0.15:
    # 塌缩发生，且比 v22 更严重
    log.warning("Collapse detected, exiting training")
    save_final_ckpt()
    exit()
```

**训练稳定性早停**：

```python
if step > 500 and grad_norm > 100:
    log.warning("Gradient explosion, exiting")
    exit()

if step > 1000 and val_loss > val_loss_at_step_500 * 1.5:
    log.warning("Val loss diverging, exiting")
    exit()
```

## 9. Eval 和对比

### 9.1 需要的对照

**必要**：

- **v22 baseline (Qwen + LoRA + v22 loss + 8000 步)**：如果没有最新 seedB
  full eval，重跑一次
- **v25a (自训 12M + v22 loss + 8000 步)**

**可选**（时间允许）：

- **v25a (16000 步)**：查看自训模型的 upper bound

### 9.2 Eval 集

seedB full eval：
- c04：`data/raw_trace_pool/activecore_eval/c04_seedB_infer17`
- c08：`data/raw_trace_pool/activecore_eval/c08_seedB_infer17`
- c16：`data/raw_trace_pool/activecore_eval/c16_seedB_infer17`
- c32：`data/raw_trace_pool/activecore_eval/c32_seedB_infer17`

**期望**：至少 c04/c08/c16 完成。c32 如果自训 12M 全局 32k 装不下，允许失败。

### 9.3 主要指标

**Primary**：

- `mean pred_vs_roi_cpi_uop` per core count（4 个数字）
- `median pred_vs_roi_cpi_uop`
- `max pred_vs_roi_cpi_uop`

**Secondary**：

- Per-workload pVr（找到 outlier 是否重叠 v22）
- `pred_cv / label_cv`（塌缩指标，两个方案对比）
- Worst-core pVr per workload

**部署侧**：

- `avg forward/window` (ms)
- `avg total/window` (ms)
- `uops/s throughput`

期望 v25a latency 显著低于 v22（估计 2-4x 快，因为 backbone 参数少 50x
但每层都要 forward）。

### 9.4 决策阈值

**结果 A：v25a mean pVr 差 v22 > 3pp**
→ 结论：Qwen 的 590M 冻结权重有实质贡献
→ 后续路线：走 v24（Qwen-Coder + macro 汇编），进一步激活语义先验
→ 承认自训方向（v25b/c/d）优先级低

**结果 B：v25a mean pVr 差 v22 < 1pp**
→ 结论：Qwen 的 590M 冻结权重贡献很小
→ 后续路线：切自训方向（v26/v27），投资 tabular 输入优化 + 训练策略
→ v24 优先级降到最低（LLM 语义可能也无用）

**结果 C：v25a mean pVr 好于 v22**
→ 结论：Qwen 是负迁移（自然语言 attention pattern 不适合此任务）
→ 后续路线：完全切自训，v22-v24 路线都废弃

**结果 D：v25a 训不动**（loss 不降/塌缩/发散）
→ 结论：自训 12M 在 32k 上下文 + 稀疏监督下真的学不出来
→ 后续路线：
  - 尝试 v25a-25M（放大参数量）
  - 或加 local-core（避开长上下文）
  - 或加 v23 反塌缩（打破塌缩早期形成）
→ Qwen 的独立价值 = 稳定训练能力（这也是一种价值）

## 10. 训练时间和资源

### 10.1 训练时间估算

**v25a (12M, 32k context, 8000 步, 8 GPU)**：

- 每步 forward: ~800ms（相比 v22 的 ~1000ms 略快）
  - Backbone forward 快（12M vs 600M）
  - 但 attention 复杂度不变（32k²）
  - 每层反传比 LoRA 慢
- 8000 步 wall time: ~2 小时
- 加 eval 和 checkpoint 保存: ~2.5 小时

**v22 baseline 补跑**（如果需要）：约 4 小时。

**总实验时间**：v22 补跑 + v25a 训练 + 全 eval = 约 1 天。

### 10.2 显存

**v25a per GPU**：
- Model: ~48MB (bf16 12M 参数)
- Optimizer states (AdamW): ~200MB
- Activations (with checkpoint): ~30GB per sample
- Batch=8: ~40-60GB per GPU

**A100 80GB 应该够**，如果吃紧减 batch 到 4 per GPU + grad_accum=2。

## 11. 主要风险

**风险 1：训不动**

从零训 32k 上下文 + 稀疏监督，attention 可能学不出有效 pattern。

**表现**：train_loss 缓慢下降或不下降，val loss 平台早，pred_cv_ratio 低。

**缓解**：
- 早停诊断（前面第 8 节）
- 如果 8000 步没进展，尝试 lr 从 3e-4 降到 1e-4 重训
- 如果仍然不行，进入结果 D 分支

**风险 2：过拟合**

40k 训练样本对 12M 参数容易过拟合。

**表现**：train_loss 下降但 val loss 反弹。

**缓解**：
- Dropout 0.1 + weight decay 0.05 已经加了
- 如果仍过拟合，增加 dropout 到 0.2
- 或减小 model size 到 8M

**风险 3：塌缩**

自训 backbone 加 v22 弱塌缩机制的 loss，塌缩问题会重现。这是**符合预期**的：

- 塌缩问题和 backbone 无关（前面 v23 分析过）
- v22 loss 就有塌缩，v25a 也会有
- 这不是 v25a 的失败，是 v22 loss 的固有问题

**处理**：不解决塌缩，接受它是"和 v22 一致的问题"。塌缩指标（pred_cv_ratio）
作为对比数据一起报。

**风险 4：显存爆炸**

32k 上下文 + 每层反传 + batch=8。

**缓解**：
- Gradient checkpointing 已启用
- 如果 OOM，减 batch 到 4 + grad_accum=2

**风险 5：结果不明确**

差距在 1-3pp 之间，既不能说 Qwen 有用也不能说无用。

**缓解**：
- 补跑 v25a-16000 步作为 upper bound
- 后续做 v25a-25M（放大参数量）看曲线
- 单点结果不明确时，多点数据能给出趋势

## 12. 分阶段实施

**Day 1**：
- 写 `model/tiny_transformer.py`
- 修改 `model/llm_wrapper.py` 加 tiny 分支
- 修改 `train/train_lora.py` 加 CLI 参数
- Smoke test：单 GPU 100 步验证 forward/backward 正常

**Day 2**：
- 8 GPU DDP smoke test 500 步
- 检查 loss 下降、显存占用、gradient norm
- 确认 checkpoint 保存 meta 正确

**Day 3**：
- 完整训练 v25a 8000 步
- 同时 v22 baseline 8000 步（如果没最新的）
- Eval c04/c08/c16 seedB full

**Day 4**：
- 尝试 c32 eval（自训 32k 上下文可能装不下）
- 如果 v25a 有明显趋势，写结果报告
- 如果结果模糊，跑 v25a-16000 步

## 13. 一些不做的事

- **不换 tokenizer**：这一版严格控制变量
- **不改 loss/head**：这一版是"backbone 换掉，其他一样"
- **不上 v23 反塌缩**：那是 v25b 或独立线的事
- **不走 local-core**：那是 v25b 的事
- **不改数据集**：完全复用 v22 tensor cache
- **不做 two-stage cross-core attention**：v25 系列后期扩展
- **不做多规模 sweep**：本文档单点 12M；如果时间允许可以后续加 25M/50M
  作为 v25a-scale 补充实验

## 14. 一句话说明

**v25a = "只换 backbone 的 v22"**：从零训 12M transformer 替代 Qwen+LoRA，
其他所有设计保持完全一致。目标不是做出更好模型，是给出 LLMSim 项目历史上
第一个"backbone 层面的对照数据"。差距 <1pp 说明 Qwen 无贡献，差距 >3pp
说明 Qwen 有实质贡献；两个结果都是有价值的方向性判断。

## 15. 交付物

**代码**：
- `model/tiny_transformer.py`（新增）
- `model/llm_wrapper.py`（加 tiny 分支）
- `train/train_lora.py`（加 CLI）
- `eval/eval_quota_cycles.py`（加 checkpoint meta 加载）
- `scripts/run_v25a_tiny_transformer_11m.sh`（新增训练脚本）

**训练产物**：
- `ckpt/v25a_tiny_transformer_8l320_8gpu_8000/`
- `logs/train_v25a.log`

**Eval 产物**：
- `logs/eval_parallel_v25a_c04_seedB_full_...`
- `logs/eval_parallel_v25a_c08_seedB_full_...`
- `logs/eval_parallel_v25a_c16_seedB_full_...`
- `logs/eval_parallel_v25a_c32_seedB_full_...`（如能装下）

**结果报告**：
- `docs/eval_v25a_vs_v22_comparison.md`：v25a vs v22 全 workload 对比数据 +
  决策依据
