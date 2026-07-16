# TCSim v28.1 A2 训练与部署侧推理实验报告

> 实验状态：已完成。训练于 2026-07-16 结束；seed0 与独立 seed1 在 c4/c8/c16/c32 的全量部署侧推理各 92 条 trace、合计 184 条，均已完成。
>
> 本报告使用 workload-equal macro 指标判断精度；pooled 指标仅用于总量偏差和计数完整性检查。

## 1. 结论摘要

1. **训练和部署推理机制通过。** 30,000 step 正常结束，best checkpoint 位于 step 23,000；seed0/seed1 分别完成 4,479,169/4,479,205 chunks，预测锁存和提交均 exact-once，ROI 标签覆盖率分别为 99.999997%/99.999993%。
2. **seed1 复现了 seed0 的总体结论。** workload-macro ROI mean 为 5.97%，与 seed0 的 5.71% 只差 0.26 pp；heldout 为 12.94%，与 seed0 的 13.19% 基本一致。因此主要退化不是偶然 seed 噪声。
3. **机制分布内 CPI 可用，但业务结果不能被混合指标掩盖。** seed1 的 9 类机制负载 ROI mean 为 2.02%，7 类业务 base 为 4.08%，业务 heldout 为 12.94%。报告表中的 `train/base=2.92%` 混合了机制负载和业务 base，不能单独代表业务精度。
4. **业务 heldout 仍未通过泛化验收。** `redis_heldout` 平均 ROI 误差为 35.81%，`mysql_heldout` 为 17.16%，`marine_heldout` 为 16.21%；Flink/PyTorch heldout 分别只有 1.64%/3.77%，说明模型并非整体容量失效，而是存在特定机制覆盖缺口。
5. **branch-miss head 泛化失败且可稳定复现。** seed1 train/base 的 branch rate 相对误差约 10.35%、绝对误差约 0.24 pp；heldout 则约 366.48%、19.14 pp，与 seed0 几乎相同。该输出当前不能用于部署。
6. **存在 seed 敏感的业务 base。** `gofeed_base c32` 的真实 CPI 在 seed0/seed1 中均约 1.61，但预测从 1.455 变为 1.924，误差从 9.67% 变为 19.56%；`mysql_base c32` 误差从 0.58% 变为 9.05%。模型对 seed 改变的功能序列比真实 O3 性能更敏感。
7. **大核数下局部与调度轨迹仍变差。** seed1 c32 train/base 的 ROI 误差为 3.95%，但 chunk MAPE 为 18.11%；c32 heldout 的 fast-set exact 只有 28.27%，虽然 Jaccard 仍有 88.87%。aggregate ROI 较准不等于 closed-loop 调度轨迹可靠。
8. **没有必要把训练上限提高到 40k。** 最佳 `val_total` 已在 23k 出现，之后进入波动平台；seed0/seed1 的稳定 OOD 缺口不能通过延长训练解决。

当前版本可以作为 **训练分布内 CPI baseline**，但不能作为覆盖业务 heldout 和 branch PMU 的部署版本。

## 2. 实验合同

| 项目 | 配置 |
|---|---|
| 数据集 | `v28_1_business_a2_sharedzipf_dataset` |
| 训练源 | 80 traces，244,082 oracle-context samples |
| 验证源 | 64 traces，21,096 disjoint context samples；只含 seed0 train/base workload |
| 业务测试 | 7 个 heldout business workloads |
| 部署测试 | seed0、seed1 各自覆盖 c4/c8/c16/c32，16 个 train/base + 7 个 heldout；每 seed 92 traces，共 184 traces |
| Chunk | 固定 `K=256` UOP |
| Scheduler | `epsilon=2048 cycles`，每核独立 resident chunk |
| 模型 | 114,482,780 参数，full QKVR，8 layers，`d_dyn=960`，15 heads |
| 输入 | base14 + branch5 + resource11 + dynamic8 + summary38 + relation22 |
| 训练精度 | bf16，SDPA auto，8 GPU，global batch 32 |
| Loss | `L_abs_log_cpi + 0.25 L_centered + 0.10 L_branch` |
| Branch 合同 | 所有退休控制流指令中的 predictor failure；分母为所有 retired branches |
| 部署模式 | free-running predicted scheduler；不读取 oracle rollout context；chunk 首次加载锁存，提交时 exact-once 累计 |

配置与原始产物：

- [训练配置](../configs/mvp_100m.yaml)
- [训练 metrics](../ckpt/tcsim_v281_a2_100m_8gpu_30k/metrics.json)
- [seed0 部署聚合 JSON](../logs/v281_a2_seed0_c04_c08_c16_c32_full/report.json)
- [seed0 部署文字报告](../logs/v281_a2_seed0_c04_c08_c16_c32_full/report.txt)
- [seed1 部署聚合 JSON](../logs/v281_a2_seed1_c04_c08_c16_c32_full/report.json)
- [seed1 部署文字报告](../logs/v281_a2_seed1_c04_c08_c16_c32_full/report.txt)
- [历史 A1 报告](v28_seed0_deployment_evaluation_report.md)

## 3. 训练收敛

| Step | val total | val log-CPI | val centered | val branch NLL | per-core MAPE |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 0.03588 | 0.01956 | 0.01637 | 0.12218 | 17.29% |
| 10,000 | 0.02740 | 0.01497 | 0.01526 | 0.08617 | 13.04% |
| 13,000 | 0.02678 | 0.01476 | 0.01511 | 0.08241 | 12.58% |
| **23,000** | **0.02635** | **0.01449** | **0.01497** | **0.08123** | 12.72% |
| 29,000 | 0.02664 | 0.01481 | 0.01493 | 0.08097 | **12.42%** |
| 30,000 | 0.02647 | 0.01464 | 0.01491 | 0.08107 | 13.02% |

`best.pt` 按 `val_total` 选择，因此部署推理使用 step 23,000。该点的加权 loss 构成为：

```text
L_abs      = 0.014490                         54.98%
0.25*L_ctr = 0.25 * 0.014965 = 0.003741     14.20%
0.10*L_brm = 0.10 * 0.081226 = 0.008123     30.82%
total      = 0.026354
```

训练没有出现继续下降的明确信号：23k 后的 `val_total` 只在 0.02643--0.02825 间波动。29k 的 per-core MAPE 略低，但 joint loss 没有超过 23k。当前实验支持保留 30k 上限，同时增加 `best_cpi.pt` 与 `best_joint.pt` 双 checkpoint，而不支持直接增加训练步数。

## 4. 推理完整性与吞吐量

### 4.1 seed0

| 指标 | 结果 |
|---|---:|
| 完成 traces | 92/92 |
| 总 chunks | 4,479,169 |
| 总 ROI UOP | 1,146,442,446 |
| 有效标签 UOP | 1,146,442,408 |
| 标签覆盖率 | 99.999997% |
| Scheduler steps | 311,436 |
| Model forwards | 311,426 |
| Resident events | 124,861 |
| Exact-once latched | 4,479,169/4,479,169 |
| Exact-once committed | 4,479,169/4,479,169 |
| 平均单 trace throughput | 74.2k committed UOP/s |
| GPU 峰值 allocated/reserved | 1.74/2.14 GiB |
| 8 GPU 实际全量推理墙钟时间 | 约 34.7 分钟 |

全量 pooled CPI 为 `pred=1.7257`、`true=1.8195`，整体低估 5.15%。该数字受长 trace 权重和不同 workload 误差抵消影响，不能代替下面的 workload-macro 指标。

### 4.2 seed1

| 指标 | 结果 |
|---|---:|
| 完成 traces | 92/92 |
| 总 chunks | 4,479,205 |
| 总 ROI UOP | 1,146,450,199 |
| 有效标签 UOP | 1,146,450,119 |
| 标签覆盖率 | 99.999993% |
| Scheduler steps | 309,085 |
| Model forwards | 309,078 |
| Resident events | 118,330 |
| Exact-once latched | 4,479,205/4,479,205 |
| Exact-once committed | 4,479,205/4,479,205 |
| 平均单 trace throughput | 74.6k committed UOP/s |
| GPU 峰值 allocated/reserved | 1.74/2.12 GiB |
| 8 GPU 全量推理墙钟时间 | 约 31--33 分钟 |

seed1 pooled CPI 为 `pred=1.7487`、`true=1.8192`、误差 3.88%。该误差低于 seed0 的 5.15%，但 workload-macro 反而从 5.71% 微升到 5.97%，说明 pooled 改善主要来自不同 workload 高估/低估的抵消，不能解释为泛化精度提升。

## 5. 分核心数主要结果

### 5.1 seed0

| Cores | Set | ROI mean | Window MAPE | Chunk MAPE | Branch rel. | Branch abs | Fast-set exact | Fast-set Jaccard |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 4 | train/base | 2.40% | 5.23% | 6.90% | 8.79% | 0.20 pp | 97.37% | 99.18% |
| 4 | heldout | 14.76% | 19.22% | 22.57% | 371.19% | 19.33 pp | 88.79% | 96.87% |
| 8 | train/base | 2.13% | 4.99% | 8.35% | 10.17% | 0.23 pp | 89.19% | 97.19% |
| 8 | heldout | 13.54% | 17.22% | 21.94% | 360.41% | 18.96 pp | 65.68% | 93.27% |
| 16 | train/base | 2.32% | 4.89% | 11.41% | 10.73% | 0.24 pp | 81.75% | 95.69% |
| 16 | heldout | 12.09% | 16.00% | 23.19% | 364.24% | 19.13 pp | 51.10% | 92.75% |
| 32 | train/base | 2.89% | 5.22% | 16.04% | 9.94% | 0.19 pp | 69.79% | 94.31% |
| 32 | heldout | 12.37% | 16.34% | 26.27% | 359.16% | 18.85 pp | 24.52% | 88.43% |

主要现象：

- ROI 与 window 误差没有随核心数单调恶化，说明 full-QKVR 和 free-running scheduler 在总量上可扩展到 c32。
- Chunk MAPE 随核心数上升，train/base 从 c4 的 6.90% 增到 c32 的 16.04%；部分正负误差在 window 和完整 ROI 中抵消。
- fast-set exact rate 明显下降，但 Jaccard 仍较高，说明预测 active set 通常只差少量核心；对于 aggregate CPI 影响有限，但会使严格 closed-loop 轨迹很早分叉。

### 5.2 seed1

| Cores | Set | ROI mean | Window MAPE | Chunk MAPE | Branch rel. | Branch abs | Fast-set exact | Fast-set Jaccard |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 4 | train/base | 2.83% | 5.28% | 7.00% | 9.42% | 0.24 pp | 97.11% | 99.09% |
| 4 | heldout | 14.29% | 18.08% | 21.31% | 373.17% | 19.49 pp | 89.38% | 96.94% |
| 8 | train/base | 2.38% | 5.00% | 8.52% | 10.55% | 0.24 pp | 89.42% | 97.32% |
| 8 | heldout | 13.65% | 17.40% | 22.15% | 366.72% | 19.09 pp | 71.97% | 94.47% |
| 16 | train/base | 2.53% | 5.21% | 12.04% | 11.22% | 0.26 pp | 80.38% | 95.36% |
| 16 | heldout | 12.55% | 16.44% | 23.89% | 363.70% | 18.93 pp | 53.60% | 92.98% |
| 32 | train/base | 3.95% | 6.56% | 18.11% | 10.22% | 0.20 pp | 68.28% | 94.16% |
| 32 | heldout | 11.26% | 15.11% | 25.87% | 362.33% | 19.06 pp | 28.27% | 88.87% |

seed1 的真实 CPI 分布符合业务目标：7 类业务 proxy 的真实 ROI CPI 为 1.23--3.22，整个 23-workload 集合为 0.144--7.09，没有重新引入 CPI 数十或上百的极端 DRAM chase 分布。

### 5.3 seed0 与 seed1 稳定性

| 指标 | seed0 | seed1 | 判断 |
|---|---:|---:|---|
| 全部 workload-macro ROI mean | 5.71% | 5.97% | 基本稳定，+0.26 pp |
| 全部 ROI p50 | 2.61% | 2.80% | 基本稳定 |
| 全部 ROI p90 | 16.74% | 15.26% | seed1 略好 |
| 机制负载 ROI mean | 1.99% | 2.02% | 完全稳定 |
| 业务 base ROI mean | 3.00% | 4.08% | seed1 变差 1.08 pp |
| 业务 heldout ROI mean | 13.19% | 12.94% | 完全稳定 |
| Window MAPE mean | 8.77% | 8.94% | 基本稳定 |
| Chunk MAPE mean | 14.58% | 15.03% | 基本稳定 |
| pooled branch rate error | 102.90% | 103.26% | 完全稳定且均不通过 |

两个 seed 的 pooled true ROI CPI 从 1.81949 变为 1.81921，仅变化约 0.015%；workload 实际执行分布稳定。与此同时，部分模型输出明显变化，最典型的是：

| Trace | seed0 true/pred/error | seed1 true/pred/error |
|---|---|---|
| `gofeed_base c32` | 1.611/1.455/9.67% | 1.609/1.924/19.56% |
| `mysql_base c32` | 2.907/2.890/0.58% | 2.905/3.168/9.05% |

这说明 seed1 的价值不只是重复测试：它暴露了模型对 branch history、地址/集合映射、局部访问顺序等 seed-dependent 功能特征的过度敏感。具体责任字段仍需通过特征消融或跨 seed 输入分布比较确定，不能仅凭最终误差归因到单一字段。

### 5.4 seed1 逐业务族结果

下表对每个业务族的 c4/c8/c16/c32 做等权平均；signed bias 为正表示 CPI 高估，为负表示低估。

| 业务族 | Base ROI error | Heldout ROI error | Heldout signed bias | Heldout branch pred/true | Heldout branch abs |
|---|---:|---:|---:|---:|---:|
| BVC encoder | 2.36% | 8.73% | -8.73% | 16.25% / 4.66% | 11.59 pp |
| Flink | 2.41% | 1.64% | -0.17% | 18.27% / 5.81% | 12.46 pp |
| GoFeed | 8.20% | 7.26% | -6.97% | 20.74% / 6.87% | 13.86 pp |
| Marine | 3.65% | 16.21% | +16.21% | 26.39% / 9.72% | 16.67 pp |
| MySQL | 4.75% | 17.16% | -17.16% | 23.43% / 6.11% | 17.32 pp |
| PyTorch | 4.65% | 3.77% | +0.59% | 44.96% / 4.50% | 40.47 pp |
| Redis | 2.56% | 35.81% | -35.81% | 25.59% / 3.97% | 21.63 pp |

Flink/PyTorch 说明 CPI 主干可以迁移到部分业务变体；Redis/MySQL/Marine 则分别暴露 dependent lookup/working-set、tail/write cadence 和 branch-path mixture 缺口。Branch 列在全部七个 heldout 上都严重过预测，即使 Flink/PyTorch 的 CPI 很准也不例外，因此 branch 是独立的系统性失败，不应被 CPI 指标掩盖。

## 6. A2 相对 A1 的变化

> A1 与 A2 的 workload 实现、ROI/branch 合同和功能特征不同，因此下面只能作为方向性比较，不是严格单变量 ablation。尤其 A1 branch 只统计条件分支，不能与 A2 的 all-retired-branch 指标比较。为保持原实验口径，本节 A2 数字仍使用 seed0；seed1 的独立复现见 5.3 节。

四种核心数 workload-macro mean：

| Set/Metric | A1 | A2 | 变化 |
|---|---:|---:|---:|
| train/base ROI | 2.97% | 2.43% | -0.54 pp |
| heldout ROI | 18.43% | 13.19% | **-5.24 pp（相对改善 28.4%）** |
| train/base window | 4.75% | 5.08% | +0.33 pp |
| heldout window | 20.51% | 17.20% | -3.32 pp |
| train/base chunk | 10.79% | 10.68% | -0.11 pp |
| heldout chunk | 27.27% | 23.49% | -3.78 pp |

逐业务 heldout 的四核心数平均 ROI：

| Heldout workload | A1 ROI | A2 ROI | 变化 | A2 window | A2 branch abs |
|---|---:|---:|---:|---:|---:|
| BVC encoder | 21.98% | 7.30% | **-14.68 pp** | 10.12% | 11.44 pp |
| Flink | 32.47% | 1.65% | **-30.82 pp** | 8.23% | 12.63 pp |
| GoFeed | 8.18% | 8.14% | -0.04 pp | 15.64% | 14.02 pp |
| Marine | 17.18% | 17.48% | +0.30 pp | 21.52% | 16.28 pp |
| MySQL | 23.25% | 16.63% | -6.62 pp | 16.96% | 17.53 pp |
| PyTorch | 16.92% | 4.14% | **-12.78 pp** | 11.54% | 40.15 pp |
| Redis | 9.01% | 37.01% | **+28.00 pp** | 36.36% | 21.41 pp |

A2 的改进不是平均平移：Flink、BVC、PyTorch 显著改善，MySQL 中等改善，GoFeed/Marine 基本不变，Redis 则大幅退化。若排除 Redis，heldout-6 的平均 ROI 从 A1 的约 20.00% 降至 A2 的 9.22%，说明新增 resource/dynamic context 确实产生了泛化收益，但当前负载覆盖仍存在一个严重机制空洞。

## 7. 误差诊断

### 7.1 CPI：不是整体容量不足，而是特定 heldout 机制缺口

训练分布内 ROI 只有约 2.4%，简单 compute/cache micro 和多数 base proxy 都较准确；这不符合“114M 模型整体容量不足”或“full-QKVR 根本学不会”的表现。主要误差集中在：

- `redis_heldout`：37.01% 平均 ROI 误差，模型系统性低估；
- `marine_heldout`：17.48%；
- `mysql_heldout`：16.63%；
- c32 `gofeed_base`：9.67%，提示高核数下该 mechanism 仍有局部缺口。

Redis base 的真实 ROI CPI 约为 1.76--1.81，heldout 为 2.59--2.68；模型对 heldout 预测约 1.55--1.73，基本停留在 base 区间。源码中 heldout 同时引入：

- shared model 从 16 MiB 增到 32 MiB；
- private state 从 64 KiB 增到 128 KiB；
- Zipf 主体上加入 1/16 uniform tail；
- 每轮对 chosen entry 增加第三次 dependent shared read。

因此下一轮不应直接把 `redis_heldout` 偷渡进训练集，而应新增通用 mechanism train probes：不同 dependent lookup depth、16/32/64 MiB shared Zipf table、Zipf+uniform-tail 混合比例和 64/128 KiB private state。这样可以验证模型究竟缺少 working-set、依赖链还是 tail-locality 信息。

`memory_seq_moderate` 的 c16/c32 真实 ROI CPI 分别约 7.09/6.93，模型 ROI 误差只有 1.24%/2.87%。这说明适度高 CPI/DRAM 压力本身没有拉坏模型；Redis 问题是业务机制组合的分布外缺口，不是所有 memory workload 都失败。
seed1 进一步确认该判断：`redis_heldout` 四种核心数误差为 38.14%/38.20%/36.24%/30.63%，平均 35.81%，仍然系统性低估；`mysql_heldout` 平均 17.16%，`marine_heldout` 平均 16.21%。相反，Flink/PyTorch heldout 平均只有 1.64%/3.77%。如果是全局模型容量、epsilon 或 loss 设计整体失效，不会出现这种长期稳定、按业务机制集中的误差结构。

seed1 还暴露了另一类问题：业务 base 的跨 seed 稳健性。尤其 c32 GoFeed/MySQL 的真实 CPI 几乎不变而预测显著变化，说明训练需要加入多 seed 的分布等价样本或一致性约束；单 seed 的同 trace sample validation 无法发现该问题。

### 7.2 Branch：验证集收敛不代表业务可泛化

Branch head 在 train/base 上表现正常，但在全部 heldout 上系统性过预测，且 seed1 完整复现 seed0：

```text
seed0 train/base: relative error ~= 9.9%,  absolute error ~= 0.21 pp
seed1 train/base: relative error ~= 10.4%, absolute error ~= 0.24 pp
seed0 heldout:    relative error ~= 363.8%, absolute error ~= 19.1 pp
seed1 heldout:    relative error ~= 366.5%, absolute error ~= 19.1 pp
seed1 pooled:     pred rate 8.72%, true rate 4.29%, relative error 103.3%
```

最严重的是 `pytorch_heldout`：seed1 四核心数平均真实 miss rate 约 4.50%，模型预测约 44.96%，绝对偏差约 40.47 pp；其 CPI ROI 平均误差却只有 3.77%。这说明 CPI head 与 branch head 已经明显解耦，不能把 branch 失败简单归因于 CPI timing 失败。

当前 CPI 与 branch 使用独立最终线性 head，但共享同一个 `h_dyn`，而 `h_dyn` 是对全部有效 UOP 的平均池化。Branch token 在 branch-sparse chunk 中会被大量非分支 UOP 稀释。因此除补齐 predictor-index/alias 特征和训练分布外，下一版还应比较 branch-token masked pooling + 独立 branch MLP/encoder；仅增加 `L_branch` 权重不足以解决表示和 OOD 问题。

当前 branch 输入含 branch 类型、actual taken、successor delta、committed direction history 和 first-touch local PC ID，但没有固定 predictor 的索引/别名和动态状态。对于启用 speculative history update 的 gem5 predictor，仅靠退休分支序列不能唯一确定：

- BTB/direction table 的 index/tag 与跨 branch alias；
- predictor counter/history state；
- RAS/indirect-target 工作集；
- wrong-path fetch 和 squash 造成的 speculative state。

此外 `local_pc_id` 是每条 trace 内按首次出现顺序编号，编译成不同 heldout binary 后 ID 语义不保证稳定，可能促使 branch head 记住 base 的局部编号而无法迁移。

因此 branch 的 P0 方向应是二选一并做对照：

1. 从 functional PC/outcome/target 与 predictor profile 派生 BTB/table index、alias pressure、RAS depth、indirect-target reuse 等非 oracle 特征；或
2. 实现固定 predictor 的 deterministic/replay baseline，能直接重放的部分不再交给共享神经 head 猜测。

提高 `L_branch` 权重只会强化当前错误映射；在补齐可辨识信息前不建议从 0.10 上调。A2 原始 `report.txt` 中“conditional-branch denominator”是历史措辞，JSON 与本报告的实际 A2 合同均为 **all retired branches**。

### 7.3 快慢核与 scheduler 轨迹

seed0 c32 heldout 的 fast-set exact rate 从 c4 的 88.79% 降到 24.52%，seed1 则从 89.38% 降到 28.27%；对应 c32 Jaccard 为 88.43%/88.87%。这表示：

- exact active-core 集合经常不完全相同；
- 多数时刻只错少数核心，而不是上下文整体崩溃；
- ROI 误差没有随 c32 爆炸，但 chunk/window 的局部轨迹精度仍不足。

因此不能用 aggregate ROI 误差证明 closed-loop trajectory 正确，也不应仅通过增大 epsilon 掩盖 fast-set 分叉。下一轮应继续按 workload/core-count 同时报告 ROI、window、chunk、fast-set exact/Jaccard 和 resident fraction。

seed1 上 ROI 误差与 window MAPE 的相关系数约为 0.92，而与 fast-set exact/Jaccard 的相关系数仅约为 -0.17/-0.06。当前 full-trace ROI 失败的首要来源仍是窗口 CPI 输出本身；scheduler 分叉是需要修复的局部轨迹问题，但不是 Redis/MySQL 系统性偏差的主因。因此不应把降低 epsilon 作为下一轮第一动作。

## 8. 模型与 Loss 判断

| 部分 | 本轮判断 |
|---|---|
| Full QKVR 主干 | 保留；c32 ROI 没有系统性失效 |
| Resource/dynamic context | 有效；Flink/BVC/PyTorch heldout 显著改善 |
| `L_abs_log_cpi` | 保留；训练分布内 CPI 良好，log-Huber 对高 CPI tail 稳健 |
| `L_centered=0.25` | 暂时保留；不能仅因 c32 exact-set 下降就盲目加权，需先补 mechanism 覆盖并做 spread ablation |
| `L_branch=0.10` | 不上调；当前是 OOD 可辨识性问题，不是 loss 不够大 |
| 30k steps | 足够；best 在 23k，继续训练收益不足 |
| 模型容量 | 没有证据要求继续增大；分布内准确、分布外集中失败更符合数据/特征缺口 |
| Checkpoint 选择 | 建议同时保存 best-joint 与 best-CPI；当前 best-total=23k、best-per-core-MAPE=29k 不一致 |
| 多 seed 稳健性 | 不通过；总体稳定，但 c32 GoFeed/MySQL 对 seed-dependent 输入过敏 |

## 9. 验收结论

| 验收项 | 结论 | 说明 |
|---|---|---|
| 数据/标签完整性 | 通过 | seed0/seed1 coverage 为 99.999997%/99.999993% |
| Exact-once 部署累计 | 通过 | 两个 seed 的 latched/committed 均等于总 chunk 数 |
| 8 GPU 推理可运行性 | 通过 | 每 seed 92 traces 约半小时，峰值显存约 2.14 GiB reserved |
| Train/base ROI CPI | 通过 | seed1 四核数 2.38%--3.95%；其中业务 base 平均 4.08% |
| Heldout business CPI | 部分通过 | seed1 平均 12.94%，Redis/Marine/MySQL 未通过 |
| Branch PMU | 不通过 | heldout 绝对误差约 19 pp，系统性过预测 |
| c32 closed-loop trajectory | 不通过 | seed1 heldout fast-set exact 28.27%，chunk MAPE 25.87% |
| 跨 seed 稳健性 | 部分通过 | aggregate 可复现，但 c32 GoFeed/MySQL 单负载预测不稳定 |
| 当前部署就绪性 | 不通过 | 可作为 CPI baseline，不能声称业务和 PMU 泛化完成 |

## 10. 下一轮最小化实验顺序

1. **Redis mechanism cube**：只补 dependent lookup depth、shared table size、uniform-tail ratio、private-state size 四个维度；先验证 CPI 缺口，不改模型。
2. **Branch 可辨识性审计**：在训练前比较新增 predictor-index/state 特征对“相同当前输入、不同 branch miss rate”的条件方差降低量；没有统计收益的特征不进入模型。
3. **Branch replay baseline 与 neural head 对照**：分别报告 all-retired branch count/rate 的绝对 pp 和相对误差。
4. **建立 business development validation。** 当前 heldout 与 seed1 均已用于方案诊断，统计意义上已成为 development evidence；下一版最终泛化必须另留未参与调参的新 seed 和新 binary variant，不能再把 seed1 宣称为 untouched final test。
5. **增加多 seed 一致性实验。** 可用新 seed2/seed3 构造训练等价样本，检查相同 workload/core 的真实 CPI 近似不变时预测方差是否同步降低；在确定最终方案后，再保留新的未见 seed 做一次性测试。
6. **双 checkpoint。** 同时导出 best-joint 和 best-CPI，并在少量固定 c8/c32 trace 上先做部署侧筛选，再运行 92-trace 全量评估。
7. **完成上述诊断前不增大模型、不提高 branch/center loss 权重、不延长到 40k。**
