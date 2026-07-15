# TCSim v28 seed0 部署侧推理评估报告

> 历史口径警告：本报告由旧 cache/checkpoint 生成，branch 指标只统计条件分支。
> 2026-07-15 起的新合同改为“所有退休分支 / 所有预测失败”，见
> [`v28_1_functional_feature_and_trace_contract.md`](v28_1_functional_feature_and_trace_contract.md)。
> 本页已有 branch 数值不能作为新合同的基线，必须重采、重建 cache、重训后重跑。

> 生成日期：2026-07-15  
> 评估范围：seed0，c4/c8/c16/c32，16 个 train/base 负载 + 7 个业务 heldout 负载  
> 主要结论：训练分布上的 ROI UOP-CPI 误差约为 2.77%–3.13%，但 heldout 为 17.51%–18.81%；当前瓶颈是业务分布泛化，而不是核心数扩展。

## 1. 实验配置与完整性

| 项目 | 配置 |
|---|---|
| checkpoint | `/data00/yinhaolang/TCSim/ckpt/tcsim_v28_business_a1_sharedzipf_100m_8gpu_30000/best.infer.pt` |
| checkpoint step | 30000（best.infer.pt） |
| split | `train,test_business` |
| 核心数 | 4、8、16、32 |
| 每个核心数负载 | 23：train/base 16 + heldout 7 |
| trace 总数 | 92 |
| chunk 总数 | 4,477,368 |
| ROI UOP | 1,145,995,154 |
| 有效标签覆盖率 | 99.99999764%（仅 12 个无效 cycle label） |
| 固定窗口 | K=256 UOP/core |
| 调度阈值 | epsilon=2048 cycles |
| 推理精度/后端 | BF16，SDPA=auto |
| 推理设备 | 本机 8 GPU 并行，不同 trace 分配到不同 GPU |

部署侧评估不读取 oracle `rollout.jsonl` 作为模型上下文。每次模型调用使用当前活跃核心的完整 full-QKVR 上下文；输出只在 chunk 首次加载时锁存，resident chunk 后续重复参与上下文但不会重复锁存或累计。真实 cycle 与 branch miss 只用于评估标签。oracle scheduler 仅作为调度集合对照，不参与主预测轨迹。

原始产物：

- [完整 JSON 报告](../logs/v28_seed0_c04_c08_c16_c32_20260715_124419/report.json)
- [TSim 风格文字报告](../logs/v28_seed0_c04_c08_c16_c32_20260715_124419/report.txt)
- [部署推理机制说明](deployment_inference.md)

## 2. 指标口径

| 指标 | 定义 | 主用途 |
|---|---|---|
| ROI UOP CPI | `sum(all-core cycles) / sum(all-core micro-ops)` | 当前主 CPI；先跨核求和再相除 |
| ROI macro-instruction CPI | `sum(all-core cycles) / sum(all-core macro instructions)` | 宏指令口径 CPI；不是跨核/跨负载平均 |
| ROI UOP-CPI error | `abs(pred ROI UOP CPI - true ROI UOP CPI) / true ROI UOP CPI` | 完整 trace 的 CPI 精度 |
| Scheduler-window CPI MAPE | 每个 epsilon scheduler step 中，本次提交 chunks 的聚合 CPI 误差，再对 step 求平均 | 部署调度局部精度 |
| Chunk CPI MAPE | 每个核心、每个固定 256-UOP chunk 的 CPI MAPE；resident 重复出现不重复计数 | 最局部的预测诊断 |
| Branch miss relative error | 完整 workload trace 内先跨核累加 miss，再计算一个相对误差；count-relative 与 rate-relative 完全相同 | 分支预测主误差 |
| Branch abs pp | 预测与真实 branch-miss rate 的绝对百分点差 | 避免低 miss-rate 时相对误差被放大 |

### 2.1 CPI 的跨核汇总

对一个 workload 的完整 trace，当前主 CPI 定义为：

```text
ROI UOP CPI = sum(每个核的 cycles 推进) / sum(每个核的 micro-ops)
```

每核的 `total cycles / total micro-ops` 同时保留为诊断，但不会先算每核 CPI 再平均。宏指令 CPI 使用相同的 cycle 分子，把分母替换为所有核心的宏指令总数。当前 packed field 已用 `macro_position=1(single)` 或 `2(first)` 标记每条宏指令的起点，因此未来可以在提交时直接累计，无需重新采集 raw trace。

本报告现有全量表格使用 UOP CPI；`macro CPI` 专指宏指令 CPI。跨 workload 的统计统一称为 `workload-equal mean`，不再称为 macro CPI。以 `W_v28_bvc_encoder_base c4` 为例，UOP CPI pred/true 为 1.33468/1.39218，宏指令 CPI pred/true 为 1.70896/1.78259；二者相对误差均为 4.13%，因为预测和真实共享同一个指令数分母。

### 2.2 Branch miss 的统一统计

对 chunk `j`，模型输出条件分支 miss probability `p_hat_j`，functional trace 给出条件分支机会数 `B_j`：

```text
pred_miss_j = p_hat_j * B_j
M_pred = sum(所有核心、所有已提交 chunk 的 pred_miss_j)
M_true = sum(所有核心、所有已提交 chunk 的 true_miss_j)
B      = sum(所有核心、所有已提交 chunk 的 B_j)
```

一个 chunk 只在最终提交时累计一次；resident 重复进入上下文不会重复增加 branch miss。统一后的主指标为：

```text
branch_relative_error = abs(M_pred - M_true) / M_true
pred_rate = M_pred / B
true_rate = M_true / B
branch_abs_pp = abs(pred_rate - true_rate) * 100
```

由于 pred/true rate 使用完全相同的 `B`，`miss count relative error` 与 `miss rate relative error` 数学上相同，因此报告只保留一个 `Branch relative error` 作为主相对误差；同时保留 pred/true miss 数量、条件分支数、pred/true rate 和绝对百分点差。若 `M_true=0`，相对误差记为 N/A，只看绝对 miss 数和绝对百分点差。部署代码现在输出规范字段 `branch_miss_relative_error`，两个旧字段仅作为同值兼容别名；本次历史 JSON 生成于规范字段加入前，92 条 trace 上两个旧字段的最大差仅为浮点重算误差 `1.83e-16`。

### 2.3 推荐的汇报层级

1. **主结果：按核心数、workload 等权平均。** 每个负载一票，分别汇报 all-23、train/base-16、heldout-7。
2. **第二层：逐负载结果。** 同时给出 pred/true ROI UOP CPI、window MAPE/P90，以及 branch miss 数量、rate、相对误差和绝对百分点差。
3. **辅助结果：跨全部 trace 的 global-pooled aggregate。** 只用于总量偏差和计数完整性检查，不能作为泛化精度结论。

## 3. 按核心数的主结果（workload-equal mean）

| 核数 | 集合 | n | ROI UOP-CPI err mean | ROI err P50 | ROI err P90 | Window mean | Window P50 | Window P90 | Branch rel mean | Branch rel P50 | Branch rel P90 | Branch abs mean | Chunk MAPE |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | all | 23 | 7.90% | 4.97% | 19.87% | 9.67% | 7.24% | 24.73% | 26.08% | 15.88% | 68.04% | 1.31 pp | 12.07% |
| 4 | train/base | 16 | 3.13% | 2.65% | 5.95% | 4.83% | 5.95% | 9.32% | 15.10% | 13.39% | 26.30% | 0.37 pp | 6.69% |
| 4 | heldout | 7 | 18.81% | 19.60% | 26.48% | 20.75% | 20.27% | 29.09% | 51.17% | 64.87% | 78.07% | 3.46 pp | 24.35% |
| 8 | all | 23 | 7.70% | 4.73% | 20.58% | 9.26% | 6.26% | 22.57% | 25.86% | 16.00% | 69.59% | 1.35 pp | 13.45% |
| 8 | train/base | 16 | 2.90% | 3.03% | 5.50% | 4.49% | 5.76% | 8.75% | 14.70% | 12.79% | 25.14% | 0.41 pp | 8.20% |
| 8 | heldout | 7 | 18.67% | 19.19% | 26.65% | 20.16% | 20.97% | 28.53% | 51.36% | 63.57% | 76.49% | 3.49 pp | 25.43% |
| 16 | all | 23 | 7.62% | 3.56% | 22.16% | 9.41% | 6.38% | 22.64% | 24.77% | 15.21% | 70.65% | 1.33 pp | 16.63% |
| 16 | train/base | 16 | 2.77% | 2.63% | 4.47% | 4.58% | 5.56% | 8.86% | 12.91% | 11.25% | 25.76% | 0.38 pp | 11.70% |
| 16 | heldout | 7 | 18.72% | 17.48% | 27.86% | 20.44% | 20.39% | 28.77% | 51.88% | 64.92% | 76.19% | 3.50 pp | 27.90% |
| 32 | all | 23 | 7.48% | 4.47% | 22.35% | 9.85% | 6.35% | 23.33% | 24.52% | 15.54% | 69.95% | 1.23 pp | 21.08% |
| 32 | train/base | 16 | 3.10% | 2.52% | 5.48% | 5.10% | 5.74% | 9.67% | 13.33% | 11.29% | 26.36% | 0.30 pp | 16.55% |
| 32 | heldout | 7 | 17.51% | 21.54% | 27.66% | 20.71% | 21.17% | 27.97% | 50.08% | 63.05% | 74.46% | 3.36 pp | 31.42% |

这里的 P50/P90 是“负载级指标”的分位数，不是把所有 window 混在一起后的分位数。Window mean 是各负载自身 scheduler-window MAPE mean 的等权平均。

## 4. global-pooled 全量聚合的含义与限制

| 项目 | 预测 | 真实 | pooled 误差 |
|---|---:|---:|---:|
| ROI UOP CPI | 1.753794 | 1.856270 | 5.5205% |
| Branch miss count | 1,541,229.28 | 1,574,038 | 2.0844% |
| Conditional branches | 34,170,974 | 34,170,974 | shared denominator |
| Branch miss rate | 4.51035% | 4.60636% | 2.0844%（0.10 pp） |

该结果先跨 92 条 trace 累加 cycle/UOP/branch miss，再计算一个误差，因此是 **global-pooled aggregate**。它适合检查整批预测是否存在总体系统偏差，以及确认 exact-once 累计/标签覆盖是否正确。

它不能作为主精度结果，原因是：

- 长 trace 和高 branch-count 负载权重更大；
- 不同负载的高估与低估会互相抵消；
- 混合了 4/8/16/32 核；
- 混合了训练分布和 heldout。

最明显的例子是 branch miss：pooled 相对误差只有 **2.08%**，但按负载等权后，各核心数 all-23 为 **24.52%–26.08%**，heldout 为 **50.08%–51.88%**。因此 pooled 数字有总量意义，但没有足够的泛化判别力。JSON 中 `trace_*_mean` 虽然是 trace-equal mean，但仍混合了核心数和数据集角色，也不应替代分核心数报告。

## 5. 结果分析

### 5.1 训练分布

- train/base-16 的 ROI-CPI 平均误差在 **2.77%–3.13%**，P90 在 **4.47%–5.95%**，说明模型对训练分布的完整 ROI 周期预测较稳定。
- train/base 的 scheduler-window MAPE 为 **4.49%–5.10%**，明显高于 ROI 误差，符合局部误差在长 ROI 中部分抵消的预期。
- train/base 的 branch relative error 仍为 **12.91%–15.10%**；绝对误差只有 **0.30–0.41 pp**。对低 miss-rate 微负载，应优先结合绝对百分点判断。

### 5.2 heldout 泛化

- heldout-7 的 ROI-CPI 平均误差为 **17.51%–18.81%**，约为 train/base 的 6 倍。
- heldout scheduler-window MAPE 为 **20.16%–20.75%**，说明问题不是只发生在最终累计，而是局部窗口预测已经明显偏离。
- heldout branch relative error 为 **50.08%–51.88%**，绝对误差为 **3.36–3.50 pp**，是当前最弱的输出。
- 因此当前 checkpoint 可以说明模型拟合了训练分布，但不能证明其具备足够的业务泛化能力。

### 5.3 核心数扩展

- all-23 的 ROI-CPI 平均误差从 c4 的 7.90% 轻微下降到 c32 的 7.48%，没有出现随着核心数增长而系统性失效。
- scheduler-window MAPE 在 9.26%–9.85% 之间，也没有明显的 c32 突增。
- 但 Chunk MAPE 从 c4 的 12.07% 增长到 c32 的 21.08%；train/base 也从 6.69% 增长到 16.55%。这说明大核心数下最局部的 per-core chunk 预测更不稳定，只是部分误差在 scheduler window 和完整 ROI 中被抵消。若目标包含精确 closed-loop 调度，该现象需要继续关注。

### 5.4 主要失败模式

- `flink_heldout` 是最稳定、最严重的 CPI 泛化失败：四种核心数 ROI 误差均在 28.56%–34.50%，branch miss 误差约 68.83%–72.08%。
- `mysql_heldout` 的 ROI 误差随核心数从 21.14% 上升到 27.07%，但 branch miss 误差较小，说明其 CPI 误差主要不能简单归因于分支预测头。
- `bvc_encoder_heldout`、`marine_heldout`、`pytorch_heldout` 同时存在明显的窗口或 branch miss 偏差。
- `redis_heldout` 的 ROI 误差相对较低（c32 为 5.54%），但 branch miss 相对误差仍为 63.05%，表明 CPI 总量正确不代表 PMU 输出正确。

## 6. 逐负载详细结果

每行先在该 workload 的完整 trace 内跨核累计。Branch pred/true misses 是数量，Cond branches 是共享分母；Branch relative error 对 count 和 rate 完全相同，Branch abs 是 rate 的绝对百分点差。

### 6.1 c04

| workload | 集合 | steps | Pred ROI UOP CPI | True ROI UOP CPI | ROI error | Window MAPE | Window P90 | Pred misses | True misses | Cond branches | Pred miss rate | True miss rate | Branch relative error | Branch abs | Chunk MAPE |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `W_v28_bvc_encoder_base` | train/base | 3,461 | 1.3347 | 1.3922 | 4.13% | 5.73% | 10.00% | 641.29 | 851 | 39,971 | 1.60% | 2.13% | 24.64% | 0.52 pp | 7.60% |
| `W_v28_bvc_encoder_heldout` | heldout | 3,372 | 1.5972 | 1.3354 | 19.60% | 25.84% | 45.54% | 4,757.04 | 5,345 | 58,929 | 8.07% | 9.07% | 11.00% | 1.00 pp | 32.37% |
| `W_v28_cache_L1_mixed` | train/base | 3,332 | 0.6514 | 0.6424 | 1.40% | 2.05% | 2.81% | 1,269.68 | 1,175 | 131,091 | 0.97% | 0.90% | 8.06% | 0.07 pp | 2.09% |
| `W_v28_cache_L2_mixed` | train/base | 3,333 | 1.0076 | 1.0390 | 3.02% | 1.58% | 2.44% | 1,146.88 | 1,189 | 131,134 | 0.87% | 0.91% | 3.54% | 0.03 pp | 1.56% |
| `W_v28_coh_readmostly_sparse` | train/base | 3,079 | 0.6703 | 0.6596 | 1.62% | 2.37% | 3.14% | 1,132.41 | 1,168 | 131,080 | 0.86% | 0.89% | 3.05% | 0.03 pp | 2.60% |
| `W_v28_flink_base` | train/base | 3,429 | 1.9618 | 2.0452 | 4.08% | 6.18% | 11.49% | 1,505.14 | 1,767 | 55,334 | 2.72% | 3.19% | 14.82% | 0.47 pp | 7.99% |
| `W_v28_flink_heldout` | heldout | 3,455 | 2.0612 | 3.1469 | 34.50% | 33.18% | 44.46% | 1,582.22 | 5,076 | 81,993 | 1.93% | 6.19% | 68.83% | 4.26 pp | 32.13% |
| `W_v28_fp_alu_dense` | train/base | 3,361 | 0.3604 | 0.3578 | 0.73% | 0.74% | 1.09% | 64.96 | 60 | 245,772 | 0.03% | 0.02% | 8.27% | 0.00 pp | 0.75% |
| `W_v28_gofeed_base` | train/base | 3,219 | 2.1535 | 2.3378 | 7.88% | 9.72% | 20.41% | 5,463.29 | 5,906 | 49,204 | 11.10% | 12.00% | 7.50% | 0.90 pp | 16.16% |
| `W_v28_gofeed_heldout` | heldout | 3,245 | 2.2348 | 2.5224 | 11.40% | 15.12% | 27.64% | 2,736.99 | 5,632 | 76,835 | 3.56% | 7.33% | 51.40% | 3.77 pp | 19.36% |
| `W_v28_int_alu_dense` | train/base | 3,601 | 0.7331 | 0.7341 | 0.12% | 0.54% | 1.11% | 47.95 | 64 | 81,928 | 0.06% | 0.08% | 25.08% | 0.02 pp | 0.54% |
| `W_v28_int_div_serial` | train/base | 3,625 | 0.8338 | 0.8306 | 0.39% | 2.34% | 4.74% | 57,318.66 | 49,464 | 532,283 | 10.77% | 9.29% | 15.88% | 1.48 pp | 4.54% |
| `W_v28_marine_base` | train/base | 3,427 | 2.1021 | 2.2276 | 5.63% | 6.40% | 11.35% | 3,894.50 | 4,330 | 33,819 | 11.52% | 12.80% | 10.06% | 1.29 pp | 8.07% |
| `W_v28_marine_heldout` | heldout | 3,240 | 2.4701 | 2.0594 | 19.94% | 26.36% | 49.47% | 13,864.35 | 7,773 | 71,706 | 19.33% | 10.84% | 78.37% | 8.49 pp | 36.91% |
| `W_v28_memory_random_mlp` | train/base | 2,199 | 2.9496 | 3.1093 | 5.14% | 7.26% | 20.75% | 63.77 | 88 | 32,875 | 0.19% | 0.27% | 27.53% | 0.07 pp | 10.33% |
| `W_v28_memory_seq_moderate` | train/base | 2,177 | 4.8752 | 4.7684 | 2.24% | 8.91% | 17.21% | 47.87 | 63 | 32,782 | 0.15% | 0.19% | 24.01% | 0.05 pp | 9.15% |
| `W_v28_mysql_base` | train/base | 3,743 | 3.0291 | 3.2319 | 6.27% | 7.24% | 20.70% | 2,368.26 | 2,690 | 46,109 | 5.14% | 5.83% | 11.96% | 0.70 pp | 9.64% |
| `W_v28_mysql_heldout` | heldout | 3,708 | 2.4738 | 3.1368 | 21.14% | 20.27% | 33.60% | 5,770.37 | 5,451 | 86,043 | 6.71% | 6.34% | 5.86% | 0.37 pp | 19.66% |
| `W_v28_pytorch_base` | train/base | 3,060 | 1.0512 | 1.1061 | 4.97% | 9.88% | 19.41% | 409.65 | 384 | 32,777 | 1.25% | 1.17% | 6.68% | 0.08 pp | 18.02% |
| `W_v28_pytorch_heldout` | heldout | 2,952 | 1.0636 | 1.2338 | 13.79% | 12.88% | 22.16% | 737.51 | 3,333 | 71,711 | 1.03% | 4.65% | 77.87% | 3.62 pp | 16.26% |
| `W_v28_redis_base` | train/base | 3,364 | 1.7172 | 1.7572 | 2.28% | 6.17% | 12.19% | 315.04 | 388 | 32,825 | 0.96% | 1.18% | 18.81% | 0.22 pp | 7.91% |
| `W_v28_redis_heldout` | heldout | 3,463 | 2.3005 | 2.5941 | 11.32% | 11.58% | 21.97% | 834.46 | 2,375 | 57,360 | 1.45% | 4.14% | 64.87% | 2.69 pp | 13.76% |
| `W_v28_simd_sse_dense` | train/base | 3,521 | 0.2730 | 0.2735 | 0.16% | 0.15% | 0.14% | 40.94 | 60 | 163,852 | 0.02% | 0.04% | 31.76% | 0.01 pp | 0.15% |

### 6.2 c08

| workload | 集合 | steps | Pred ROI UOP CPI | True ROI UOP CPI | ROI error | Window MAPE | Window P90 | Pred misses | True misses | Cond branches | Pred miss rate | True miss rate | Branch relative error | Branch abs | Chunk MAPE |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `W_v28_bvc_encoder_base` | train/base | 3,461 | 1.3602 | 1.4115 | 3.64% | 5.47% | 12.09% | 1,303.63 | 1,756 | 79,956 | 1.63% | 2.20% | 25.76% | 0.57 pp | 8.10% |
| `W_v28_bvc_encoder_heldout` | heldout | 3,495 | 1.6445 | 1.3599 | 20.93% | 25.15% | 40.42% | 8,546.27 | 10,686 | 117,816 | 7.25% | 9.07% | 20.02% | 1.82 pp | 34.18% |
| `W_v28_cache_L1_mixed` | train/base | 3,332 | 0.6494 | 0.6425 | 1.07% | 1.52% | 2.20% | 2,521.18 | 2,350 | 262,182 | 0.96% | 0.90% | 7.28% | 0.07 pp | 1.58% |
| `W_v28_cache_L2_mixed` | train/base | 3,333 | 1.0151 | 1.0405 | 2.44% | 1.30% | 2.33% | 2,299.37 | 2,385 | 262,286 | 0.88% | 0.91% | 3.59% | 0.03 pp | 1.37% |
| `W_v28_coh_readmostly_sparse` | train/base | 3,077 | 0.6668 | 0.6587 | 1.23% | 2.04% | 3.03% | 2,630.33 | 2,336 | 262,160 | 1.00% | 0.89% | 12.60% | 0.11 pp | 2.23% |
| `W_v28_flink_base` | train/base | 3,439 | 2.0020 | 2.0795 | 3.73% | 6.04% | 17.13% | 3,004.57 | 3,577 | 110,729 | 2.71% | 3.23% | 16.00% | 0.52 pp | 9.64% |
| `W_v28_flink_heldout` | heldout | 3,466 | 2.0883 | 3.1853 | 34.44% | 33.60% | 41.77% | 2,917.52 | 10,094 | 163,983 | 1.78% | 6.16% | 71.10% | 4.38 pp | 32.15% |
| `W_v28_fp_alu_dense` | train/base | 3,361 | 0.3605 | 0.3579 | 0.73% | 0.75% | 1.09% | 128.21 | 120 | 491,544 | 0.03% | 0.02% | 6.84% | 0.00 pp | 0.75% |
| `W_v28_gofeed_base` | train/base | 3,216 | 2.0967 | 2.2130 | 5.26% | 8.98% | 20.28% | 10,796.93 | 12,181 | 98,446 | 10.97% | 12.37% | 11.36% | 1.41 pp | 23.42% |
| `W_v28_gofeed_heldout` | heldout | 3,227 | 2.2623 | 2.4972 | 9.41% | 13.85% | 27.41% | 5,428.12 | 11,088 | 153,737 | 3.53% | 7.21% | 51.05% | 3.68 pp | 21.78% |
| `W_v28_int_alu_dense` | train/base | 3,601 | 0.7359 | 0.7341 | 0.25% | 0.62% | 1.20% | 111.54 | 128 | 163,856 | 0.07% | 0.08% | 12.86% | 0.01 pp | 0.62% |
| `W_v28_int_div_serial` | train/base | 3,626 | 0.8343 | 0.8305 | 0.46% | 1.68% | 3.44% | 112,911.97 | 98,855 | 1,064,579 | 10.61% | 9.29% | 14.22% | 1.32 pp | 4.53% |
| `W_v28_marine_base` | train/base | 3,434 | 2.1500 | 2.2808 | 5.73% | 6.26% | 15.96% | 7,749.70 | 8,634 | 67,654 | 11.45% | 12.76% | 10.24% | 1.31 pp | 9.22% |
| `W_v28_marine_heldout` | heldout | 3,262 | 2.5164 | 2.1112 | 19.19% | 22.97% | 43.27% | 27,310.11 | 15,469 | 143,451 | 19.04% | 10.78% | 76.55% | 8.25 pp | 36.95% |
| `W_v28_memory_random_mlp` | train/base | 2,214 | 3.1519 | 3.3086 | 4.73% | 6.11% | 15.71% | 140.74 | 176 | 65,718 | 0.21% | 0.27% | 20.03% | 0.05 pp | 10.16% |
| `W_v28_memory_seq_moderate` | train/base | 2,183 | 6.2060 | 6.4241 | 3.40% | 8.52% | 17.94% | 133.62 | 177 | 65,776 | 0.20% | 0.27% | 24.51% | 0.07 pp | 14.42% |
| `W_v28_mysql_base` | train/base | 3,743 | 3.0571 | 3.2287 | 5.31% | 6.98% | 16.21% | 4,748.60 | 5,441 | 92,292 | 5.15% | 5.90% | 12.73% | 0.75 pp | 12.96% |
| `W_v28_mysql_heldout` | heldout | 3,713 | 2.5077 | 3.1928 | 21.46% | 20.97% | 32.75% | 11,070.10 | 11,155 | 172,151 | 6.43% | 6.48% | 0.76% | 0.05 pp | 20.52% |
| `W_v28_pytorch_base` | train/base | 3,060 | 1.0689 | 1.1333 | 5.68% | 9.14% | 18.41% | 820.21 | 782 | 65,585 | 1.25% | 1.19% | 4.89% | 0.06 pp | 22.58% |
| `W_v28_pytorch_heldout` | heldout | 2,965 | 1.0934 | 1.2842 | 14.86% | 13.68% | 24.86% | 1,589.22 | 6,748 | 143,441 | 1.11% | 4.70% | 76.45% | 3.60 pp | 16.87% |
| `W_v28_redis_base` | train/base | 3,365 | 1.7505 | 1.7984 | 2.66% | 6.20% | 14.25% | 622.32 | 783 | 65,640 | 0.95% | 1.19% | 20.52% | 0.24 pp | 9.46% |
| `W_v28_redis_heldout` | heldout | 3,470 | 2.3787 | 2.6544 | 10.38% | 10.94% | 21.13% | 1,751.35 | 4,808 | 114,812 | 1.53% | 4.19% | 63.57% | 2.66 pp | 15.58% |
| `W_v28_simd_sse_dense` | train/base | 3,521 | 0.2731 | 0.2735 | 0.14% | 0.24% | 0.65% | 81.89 | 120 | 327,704 | 0.02% | 0.04% | 31.76% | 0.01 pp | 0.24% |

### 6.3 c16

| workload | 集合 | steps | Pred ROI UOP CPI | True ROI UOP CPI | ROI error | Window MAPE | Window P90 | Pred misses | True misses | Cond branches | Pred miss rate | True miss rate | Branch relative error | Branch abs | Chunk MAPE |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `W_v28_bvc_encoder_base` | train/base | 3,462 | 1.3684 | 1.4085 | 2.85% | 5.75% | 13.46% | 2,582.09 | 3,608 | 160,040 | 1.61% | 2.25% | 28.43% | 0.64 pp | 12.65% |
| `W_v28_bvc_encoder_heldout` | heldout | 3,500 | 1.7238 | 1.3807 | 24.84% | 26.85% | 41.40% | 18,308.84 | 21,593 | 235,689 | 7.77% | 9.16% | 15.21% | 1.39 pp | 39.40% |
| `W_v28_cache_L1_mixed` | train/base | 3,332 | 0.6563 | 0.6426 | 2.13% | 2.04% | 2.58% | 4,829.58 | 4,686 | 524,335 | 0.92% | 0.89% | 3.06% | 0.03 pp | 2.06% |
| `W_v28_cache_L2_mixed` | train/base | 3,332 | 1.0688 | 1.0435 | 2.42% | 2.33% | 5.20% | 4,378.34 | 4,697 | 524,358 | 0.83% | 0.90% | 6.78% | 0.06 pp | 2.56% |
| `W_v28_coh_readmostly_sparse` | train/base | 3,081 | 0.6707 | 0.6583 | 1.89% | 2.73% | 3.63% | 4,675.37 | 4,651 | 524,320 | 0.89% | 0.89% | 0.52% | 0.00 pp | 2.93% |
| `W_v28_flink_base` | train/base | 3,450 | 2.0121 | 2.0741 | 2.99% | 5.90% | 14.87% | 5,941.32 | 7,165 | 221,426 | 2.68% | 3.24% | 17.08% | 0.55 pp | 14.72% |
| `W_v28_flink_heldout` | heldout | 3,483 | 2.1435 | 3.1696 | 32.37% | 31.64% | 38.90% | 5,639.76 | 20,202 | 327,977 | 1.72% | 6.16% | 72.08% | 4.44 pp | 30.78% |
| `W_v28_fp_alu_dense` | train/base | 3,361 | 0.3622 | 0.3579 | 1.20% | 1.21% | 1.89% | 252.18 | 240 | 983,088 | 0.03% | 0.02% | 5.08% | 0.00 pp | 1.21% |
| `W_v28_gofeed_base` | train/base | 3,226 | 1.8771 | 1.9586 | 4.16% | 9.34% | 20.61% | 21,882.67 | 24,429 | 196,889 | 11.11% | 12.41% | 10.42% | 1.29 pp | 33.01% |
| `W_v28_gofeed_heldout` | heldout | 3,228 | 2.2221 | 2.3917 | 7.09% | 14.73% | 27.33% | 10,405.03 | 22,118 | 307,491 | 3.38% | 7.19% | 52.96% | 3.81 pp | 26.32% |
| `W_v28_int_alu_dense` | train/base | 3,601 | 0.7374 | 0.7341 | 0.46% | 0.68% | 1.35% | 260.67 | 256 | 327,712 | 0.08% | 0.08% | 1.82% | 0.00 pp | 0.68% |
| `W_v28_int_div_serial` | train/base | 3,627 | 0.8338 | 0.8304 | 0.41% | 1.24% | 2.43% | 221,295.10 | 197,457 | 2,129,716 | 10.39% | 9.27% | 12.07% | 1.12 pp | 4.51% |
| `W_v28_marine_base` | train/base | 3,445 | 2.2049 | 2.3104 | 4.57% | 5.69% | 13.63% | 15,619.89 | 17,273 | 135,455 | 11.53% | 12.75% | 9.57% | 1.22 pp | 12.13% |
| `W_v28_marine_heldout` | heldout | 3,257 | 2.5352 | 2.1646 | 17.12% | 20.39% | 37.84% | 54,064.18 | 30,512 | 286,961 | 18.84% | 10.63% | 77.19% | 8.21 pp | 36.97% |
| `W_v28_memory_random_mlp` | train/base | 2,234 | 3.4758 | 3.5952 | 3.32% | 5.43% | 13.47% | 274.59 | 357 | 131,404 | 0.21% | 0.27% | 23.09% | 0.06 pp | 11.57% |
| `W_v28_memory_seq_moderate` | train/base | 2,206 | 7.1452 | 6.8998 | 3.56% | 8.38% | 15.84% | 299.45 | 381 | 131,763 | 0.23% | 0.29% | 21.40% | 0.06 pp | 23.20% |
| `W_v28_mysql_base` | train/base | 3,761 | 2.9925 | 3.1290 | 4.36% | 6.38% | 14.67% | 9,548.77 | 10,936 | 184,622 | 5.17% | 5.92% | 12.68% | 0.75 pp | 17.83% |
| `W_v28_mysql_heldout` | heldout | 3,708 | 2.4530 | 3.1996 | 23.33% | 23.20% | 35.57% | 21,098.79 | 22,280 | 344,301 | 6.13% | 6.47% | 5.30% | 0.34 pp | 23.16% |
| `W_v28_pytorch_base` | train/base | 3,062 | 1.0943 | 1.1807 | 7.32% | 9.49% | 19.37% | 1,601.76 | 1,587 | 131,215 | 1.22% | 1.21% | 0.93% | 0.01 pp | 32.90% |
| `W_v28_pytorch_heldout` | heldout | 2,971 | 1.1243 | 1.3625 | 17.48% | 16.58% | 28.54% | 3,302.99 | 13,492 | 287,011 | 1.15% | 4.70% | 75.52% | 3.55 pp | 19.64% |
| `W_v28_redis_base` | train/base | 3,370 | 1.7673 | 1.8111 | 2.42% | 6.47% | 14.16% | 1,248.61 | 1,584 | 131,348 | 0.95% | 1.21% | 21.17% | 0.26 pp | 14.98% |
| `W_v28_redis_heldout` | heldout | 3,475 | 2.4425 | 2.6785 | 8.81% | 9.66% | 18.15% | 3,394.74 | 9,677 | 229,724 | 1.48% | 4.21% | 64.92% | 2.73 pp | 19.01% |
| `W_v28_simd_sse_dense` | train/base | 3,521 | 0.2742 | 0.2737 | 0.18% | 0.25% | 0.64% | 162.34 | 240 | 655,409 | 0.02% | 0.04% | 32.36% | 0.01 pp | 0.26% |

### 6.4 c32

| workload | 集合 | steps | Pred ROI UOP CPI | True ROI UOP CPI | ROI error | Window MAPE | Window P90 | Pred misses | True misses | Cond branches | Pred miss rate | True miss rate | Branch relative error | Branch abs | Chunk MAPE |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `W_v28_bvc_encoder_base` | train/base | 3,479 | 1.3447 | 1.3686 | 1.75% | 6.35% | 14.31% | 5,178.73 | 7,220 | 319,907 | 1.62% | 2.26% | 28.27% | 0.64 pp | 23.72% |
| `W_v28_bvc_encoder_heldout` | heldout | 3,492 | 1.7234 | 1.4064 | 22.55% | 23.87% | 40.31% | 38,305.51 | 43,042 | 471,600 | 8.12% | 9.13% | 11.00% | 1.00 pp | 41.12% |
| `W_v28_cache_L1_mixed` | train/base | 3,332 | 0.6721 | 0.6433 | 4.47% | 2.58% | 2.86% | 9,363.72 | 9,374 | 1,048,696 | 0.89% | 0.89% | 0.11% | 0.00 pp | 2.61% |
| `W_v28_cache_L2_mixed` | train/base | 3,332 | 1.1962 | 1.2095 | 1.10% | 1.82% | 2.93% | 8,543.73 | 9,521 | 1,048,946 | 0.81% | 0.91% | 10.26% | 0.09 pp | 2.14% |
| `W_v28_coh_readmostly_sparse` | train/base | 3,079 | 0.6737 | 0.6579 | 2.41% | 3.29% | 4.10% | 9,216.74 | 9,277 | 1,048,641 | 0.88% | 0.88% | 0.65% | 0.01 pp | 3.48% |
| `W_v28_flink_base` | train/base | 3,469 | 1.9345 | 1.9878 | 2.68% | 6.19% | 14.69% | 12,059.59 | 14,279 | 442,974 | 2.72% | 3.22% | 15.54% | 0.50 pp | 24.47% |
| `W_v28_flink_heldout` | heldout | 3,516 | 2.1916 | 3.0677 | 28.56% | 28.20% | 35.72% | 11,457.79 | 40,453 | 656,147 | 1.75% | 6.17% | 71.68% | 4.42 pp | 29.50% |
| `W_v28_fp_alu_dense` | train/base | 3,361 | 0.3637 | 0.3578 | 1.62% | 1.55% | 1.89% | 473.80 | 480 | 1,966,176 | 0.02% | 0.02% | 1.29% | 0.00 pp | 1.56% |
| `W_v28_gofeed_base` | train/base | 3,250 | 1.5096 | 1.6134 | 6.44% | 12.76% | 26.85% | 46,020.77 | 48,965 | 393,888 | 11.68% | 12.43% | 6.01% | 0.75 pp | 36.99% |
| `W_v28_gofeed_heldout` | heldout | 3,196 | 2.0832 | 2.1884 | 4.81% | 16.39% | 29.55% | 21,000.12 | 44,405 | 615,015 | 3.41% | 7.22% | 52.71% | 3.81 pp | 33.42% |
| `W_v28_int_alu_dense` | train/base | 3,601 | 0.7385 | 0.7341 | 0.60% | 0.79% | 1.39% | 624.61 | 512 | 655,424 | 0.10% | 0.08% | 21.99% | 0.02 pp | 0.79% |
| `W_v28_int_div_serial` | train/base | 3,628 | 0.8332 | 0.8309 | 0.27% | 0.89% | 1.77% | 430,228.14 | 395,430 | 4,259,290 | 10.10% | 9.28% | 8.80% | 0.82 pp | 4.52% |
| `W_v28_marine_base` | train/base | 3,457 | 2.1910 | 2.2881 | 4.24% | 6.13% | 14.21% | 32,322.42 | 34,841 | 271,007 | 11.93% | 12.86% | 7.23% | 0.93 pp | 20.32% |
| `W_v28_marine_heldout` | heldout | 3,260 | 2.5472 | 2.2646 | 12.48% | 19.95% | 35.95% | 106,818.55 | 61,023 | 574,059 | 18.61% | 10.63% | 75.05% | 7.98 pp | 37.01% |
| `W_v28_memory_random_mlp` | train/base | 2,262 | 3.9328 | 4.1188 | 4.52% | 5.35% | 12.00% | 577.11 | 743 | 263,147 | 0.22% | 0.28% | 22.33% | 0.06 pp | 12.98% |
| `W_v28_memory_seq_moderate` | train/base | 2,427 | 7.1141 | 6.9336 | 2.60% | 8.63% | 18.90% | 554.61 | 734 | 263,174 | 0.21% | 0.28% | 24.44% | 0.07 pp | 28.64% |
| `W_v28_mysql_base` | train/base | 3,767 | 2.8386 | 2.9096 | 2.44% | 6.22% | 13.38% | 19,395.81 | 22,121 | 369,240 | 5.25% | 5.99% | 12.32% | 0.74 pp | 24.81% |
| `W_v28_mysql_heldout` | heldout | 3,720 | 2.2934 | 3.1445 | 27.07% | 27.81% | 46.45% | 43,010.26 | 44,343 | 688,856 | 6.24% | 6.44% | 3.01% | 0.19 pp | 28.18% |
| `W_v28_pytorch_base` | train/base | 3,088 | 1.1225 | 1.2383 | 9.34% | 10.72% | 22.97% | 3,219.84 | 3,253 | 262,824 | 1.23% | 1.24% | 1.02% | 0.01 pp | 49.85% |
| `W_v28_pytorch_heldout` | heldout | 2,974 | 1.1711 | 1.4926 | 21.54% | 21.17% | 32.93% | 6,984.78 | 26,932 | 573,918 | 1.22% | 4.69% | 74.07% | 3.48 pp | 26.32% |
| `W_v28_redis_base` | train/base | 3,397 | 1.6823 | 1.7613 | 4.49% | 7.73% | 16.60% | 2,644.55 | 3,171 | 262,699 | 1.01% | 1.21% | 16.60% | 0.20 pp | 27.41% |
| `W_v28_redis_heldout` | heldout | 3,577 | 2.4903 | 2.6363 | 5.54% | 7.57% | 15.31% | 7,208.72 | 19,511 | 459,338 | 1.57% | 4.25% | 63.05% | 2.68 pp | 24.37% |
| `W_v28_simd_sse_dense` | train/base | 3,521 | 0.2755 | 0.2739 | 0.58% | 0.55% | 0.76% | 321.62 | 506 | 1,310,843 | 0.02% | 0.04% | 36.44% | 0.01 pp | 0.56% |

## 7. 最大 ROI-CPI 误差条目

| 排名 | 核数 | workload | 集合 | ROI error | Window MAPE | Branch error |
|---:|---:|---|---|---:|---:|---:|
| 1 | 4 | `W_v28_flink_heldout` | heldout | 34.50% | 33.18% | 68.83% |
| 2 | 8 | `W_v28_flink_heldout` | heldout | 34.44% | 33.60% | 71.10% |
| 3 | 16 | `W_v28_flink_heldout` | heldout | 32.37% | 31.64% | 72.08% |
| 4 | 32 | `W_v28_flink_heldout` | heldout | 28.56% | 28.20% | 71.68% |
| 5 | 32 | `W_v28_mysql_heldout` | heldout | 27.07% | 27.81% | 3.01% |
| 6 | 16 | `W_v28_bvc_encoder_heldout` | heldout | 24.84% | 26.85% | 15.21% |
| 7 | 16 | `W_v28_mysql_heldout` | heldout | 23.33% | 23.20% | 5.30% |
| 8 | 32 | `W_v28_bvc_encoder_heldout` | heldout | 22.55% | 23.87% | 11.00% |
| 9 | 32 | `W_v28_pytorch_heldout` | heldout | 21.54% | 21.17% | 74.07% |
| 10 | 8 | `W_v28_mysql_heldout` | heldout | 21.46% | 20.97% | 0.76% |
| 11 | 4 | `W_v28_mysql_heldout` | heldout | 21.14% | 20.27% | 5.86% |
| 12 | 8 | `W_v28_bvc_encoder_heldout` | heldout | 20.93% | 25.15% | 20.02% |
| 13 | 4 | `W_v28_marine_heldout` | heldout | 19.94% | 26.36% | 78.37% |
| 14 | 4 | `W_v28_bvc_encoder_heldout` | heldout | 19.60% | 25.84% | 11.00% |
| 15 | 8 | `W_v28_marine_heldout` | heldout | 19.19% | 22.97% | 76.55% |

## 8. 当前结论与后续判断标准

1. **部署推理与计数机制已通过完整性验证。** 92/92 traces 完成，label coverage 为 99.99999764%，chunk 的预测与 branch miss 均按首次加载锁存、提交时 exact-once 累计。
2. **训练分布结果较好。** 如果只评价 train/base 的完整 ROI UOP-CPI，当前 workload-equal mean 误差为 2.77%–3.13%。
3. **业务泛化尚不合格。** heldout ROI 约 18%、window 约 20%、branch 约 50%，不能被 pooled 5.52% ROI 或 2.08% branch 误差掩盖。
4. **核心数本身不是首要问题。** c32 的 ROI/window 没有整体恶化，但 chunk 误差增大，说明大上下文下局部预测仍有改进空间。
5. **后续模型或数据调整应以 heldout-7 的分核心数、workload-equal mean 为验收主线。** 至少同时跟踪 ROI UOP-CPI error mean/P90、scheduler-window MAPE、branch relative error 与 branch abs pp，避免只优化单一 global-pooled 指标。

## 9. 相关实现

- 文本报告聚合与逐负载表格：[`tcsim/inference/reporting.py`](../tcsim/inference/reporting.py)
- shard 合并并自动生成 `report.txt`：[`scripts/merge_deployment_reports.py`](../scripts/merge_deployment_reports.py)
- 独立重渲染文字报告：[`scripts/render_deployment_report.py`](../scripts/render_deployment_report.py)
- 本 Markdown 渲染器：[`scripts/render_deployment_markdown.py`](../scripts/render_deployment_markdown.py)
- 部署推理入口：[`scripts/infer_deployment.py`](../scripts/infer_deployment.py)
- 部署调度与 exact-once 逻辑：[`tcsim/inference/deployment.py`](../tcsim/inference/deployment.py)
