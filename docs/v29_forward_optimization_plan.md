# v29/v30 单 trace Forward 优化

状态：2026-07-30 已完成 `flex_shared_kv` 和 eval-only `fused` Q/R/K/V projection
的 H20 c32 门禁；下一步为 300-step 单 trace 端到端 A/B。

## 当前瓶颈

正式模型为 8 层、`d_dyn=960`、15 heads、`ffn_dim=3840`，共 112,206,324 参数，
其中 interaction blocks 为 103,288,320 参数。c32/K=256 每个 forward 输入 8192 token。

单层理论工作量约为：cross attention 249.6 GFLOPs、FFN 120.8 GFLOPs、Q/R/K/V/O
projection 90.6 GFLOPs、local attention 8.1 GFLOPs。8 层合计约 3.75 TFLOPs。

legacy cross path 为每个目标核 gather 其他 31 核的 K/V，c32 中每个 K 或 V 临时 tensor
为 `[32,7936,960]`，BF16 约 488 MB；K+V 每层约 976 MB，8 层累计产生约 7.8 GB
target-wise 临时数据。

## Phase F1：shared-K/V exact attention

实现位置：`tcsim/model/tcsim_model.py`。

`flex_shared_kv` 将每个 scheduler sample 展平为 `[C*K,D]`，FlexAttention block mask
排除 query 所属核对应的 K-token 对角块。每个 query 仍然看到其他所有核的全部有效 token，
K/V 顺序与 legacy 去掉自身 block 后一致；模型参数和 checkpoint state dict 不变。

为避免每个 forward 构造动态 validity block mask：

- 完整窗口使用 shared-K/V；
- partial/tail window 自动回退 legacy；
- 默认部署仍使用 legacy，CUDA 正式门禁后再切换默认值。

非 pytest CPU 验证覆盖 c1/c2/c3/c4、多 sample bucket 和不同 K：最大绝对差
`3.58e-7`，partial fallback 与 legacy 逐位一致。旧 112,206,324 参数 checkpoint 已按
`strict=True` 成功加载，未增加参数。

复现及 CUDA A/B：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  scripts/benchmark_v29_cross_attention.py
```

H20 BF16 c32/K256 实测：legacy median `2.542 ms`，shared-K/V median `2.255 ms`，
加速 `1.127x`；peak temporary allocation 从 `1,088,694,272` 降为 `96,470,528`
bytes，减少约 `992 MB`。输出 max/mean absolute difference 分别为 `2.44e-4` 和
`2.28e-8`。因此该路径通过数值和显存门禁，但没有通过原定 25% latency 门禁：按 8 层
只节省约 `2.30 ms/forward`，定位为显存优化，不单独作为 125K 的主要收益来源。

正式 rollout 的 runner stats 同时记录
`shared_kv_cross_attention_layer_calls` 与 `legacy_cross_attention_layer_calls`，用于确认完整窗口
确实命中新后端，并量化 tail fallback，而不是只根据配置名判断优化已经生效。

## Phase F2：eval-only fused Q/R/K/V projection

每层原本依次执行四个 `[960,960]` Linear。`fused` 后端在首次 eval forward 时按
Q/R/K/V 顺序拼接 checkpoint 权重为 `[3840,960]`，后续执行一次 `F.linear` 并沿输出维
切分。拼接 tensor 不是 parameter/buffer，不改变 state dict；训练态始终回退原四个
module call，避免改变 optimizer/autograd 语义。

CPU 门禁结果为逐位一致，state dict key 与参数数量不变；正式 112,206,324 参数 checkpoint
已用 `strict=True` 加载。H20 BF16 c32/K256 实测 separate median `0.5200 ms`、fused
median `0.4445 ms`，加速 `1.170x`，每层节省 `0.0755 ms`，按 8 层约节省
`0.60 ms/forward`；输出逐位一致，临时 allocation 减少约 `6.0 MB`。复现命令：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  scripts/benchmark_v29_qrkv_projection.py
```

正式 rollout 可与 shared-K/V 叠加：

```bash
CROSS_ATTENTION_BACKEND=flex_shared_kv \
QRKV_PROJECTION_BACKEND=fused \
bash scripts/run_v29_eval_8gpu.sh
```

runner stats 记录 `fused_qrkv_projection_layer_calls` 和
`separate_qrkv_projection_layer_calls`，用于排除配置已打开但实际仍走 fallback 的情况。

两项优化的单 trace 300-step 端到端 A/B：

```bash
bash scripts/benchmark_v29_forward_rollout.sh
```

后台一键运行：

```bash
bash scripts/launch_v29_forward_rollout_benchmark_nohup.sh
```

v30 Exposure-v1 + online GSS（GSS 同时进入 timing adapter 并维护 canonical PMU）的同口径
A/B：

```bash
bash scripts/launch_v30_gss_forward_rollout_benchmark_nohup.sh
```

默认仍为 seed0 c32 `W_v28_int_alu_dense`、stride256、300 step；checkpoint 为正式
commit-clock `tcsim_v30_gss_exposure_v1_60k_seed1234/best.pt`。它与 PMU-only v29 脚本是
两个独立实验，不能混用吞吐结论。

脚本默认固定使用 seed0 c32 `W_v28_int_alu_dense`、`target_stride=256`，并启用
PMU-only canonical GSS；依次运行
`legacy+separate` 与
`flex_shared_kv+fused`，输出 UOP/s、完整 model ms/forward、context ms/forward、GPU peak
以及四类逐层后端命中次数到 `comparison.json`。

PMU-only GSS 保持 v29 为唯一 timing/CPI 模型。GSS 不执行 preview、不向模型 batch 注入
teacher feature，只按 v29 scheduler 实际接受的 commit prefix 更新 canonical L1D/L2/LLC
状态并输出 miss PMU。报告中的 `gss_preview_calls` 必须为 0，`gss_commit_calls` 应等于
scheduler step 数；完整 rollout 后才会将 canonical PMU 与 gem5 label 做 qualified audit。

## 后续顺序

1. 用两项优化叠加跑短 rollout，测完整 model forward 与 UOP/s；
2. 对固定 c32 shape 编译/capture backbone；
3. 若 model 仍高于 35 ms，再训练 6 层学生模型或仅在 4/8 层执行 cross attention。

目标预算是 context 约 4.6 ms、scheduler/其余约 1 ms、model 不超过 34–35 ms，从而使
总 step 接近 39.7 ms，对应约 125K UOP/s。

## Phase F3：hierarchical cross-core latent attention

`hierarchical_latent` 是需要重新训练的结构后端，不是可直接覆盖旧 checkpoint 的等价推理
后端。每个 interaction layer 依次执行：

1. `M` 个 learned query 将每核 `K` 个 UOP 压缩为 `M` 个 latent；
2. 每核 latent 只和其他核的 latent 做跨核 attention；
3. 聚合后的远端 latent 广播回该核的 `K` 个 UOP query。

因此跨核 score 元素数从
`C*K*(C-1)*K` 变为
`C*M*K + C*M*(C-1)*M + C*K*M`。正式候选取 `C=32,K=256,M=32`，
理论 score 元素从 `65,011,712` 降为 `1,540,096`，减少 `97.63%`。

正式方案全部随机初始化，不导入旧 v29/latent 权重，也不使用任何外部模型监督。所有
参数从 step 1 端到端更新。每层使用 `[32,960]` learned latent query 和四个独立
LayerNorm；不存在额外 latent residual gate，跨核残差只由原有 relation/state
`cross_gate` 控制。

训练期三段 latent attention 强制 FP32，外围 backbone 使用 BF16。AdamW 对矩阵参数
使用 `0.05` weight decay，bias、LayerNorm 和 latent query 不做 decay。学习率在前
2K step 从 0 warmup 到 `1e-4`，随后 cosine decay，90K 时达到 `1e-5`。

复现结构基准：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  scripts/benchmark_v29_latent_attention.py
```

H20 BF16 c32/K256/M32 单层 cross 分支实测：legacy `2.519 ms`、hierarchical latent
`0.555 ms`，加速 `4.537x`；临时显存从 `1,090,791,424` 降至 `117,924,864`
bytes。该结果只证明结构性能，不代表未训练 latent 的 CPI 精度或完整 rollout 吞吐。

正式 8 GPU 训练一键启动：

```bash
bash scripts/launch_v29_latent32_train_nohup.sh
```

默认配置为 `configs/v29_latent32_scratch_100m.yaml`，数据为
`data/v29_global_time_dataset/manifest.json`。训练 90,000 step，输出到
`ckpt/tcsim_v29_latent32_scratch_100m_8gpu_90k`。step 60,000 会额外保存永久里程碑
`step_60000.pt`，随后继续训练且不会覆盖该文件。覆盖 GPU 或步数示例：

```bash
GPUS=0,1,2,3,4,5,6,7 STEPS=90000 \
  bash scripts/launch_v29_latent32_train_nohup.sh
```

先进行 5K 稳定性 pilot：

```bash
STEPS=5000 bash scripts/launch_v29_latent32_train_nohup.sh
```

确认 pilot 后在同一输出目录续跑至 90K：

```bash
RESUME_CKPT=ckpt/tcsim_v29_latent32_scratch_100m_8gpu_90k/last.pt \
STEPS=90000 bash scripts/launch_v29_latent32_train_nohup.sh
```

训练器在 forward、backward/all-reduce、optimizer 三处全 rank fail-fast，并在保存前
检查模型和 optimizer state。训练完成后仍需用 workload-equal CPI、no-progress 和完整
单 trace rollout 做精度/吞吐门禁。
