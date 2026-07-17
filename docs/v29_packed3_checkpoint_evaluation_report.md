# TCSim v29 packed3 checkpoint 全量推理评估结论

> 状态：评估完成，作为 `tcsim_v29_packed3_100m_8gpu_60k/best.pt` 的当前效果结论。
>
> 评估日期：2026-07-17。seed0 与 seed1 的 c4/c8/c16/c32 全量 free-running 推理共 184 条 trace，全部完成，无失败。
>
> 本文所有效果数字均来自聚合结果 `report.json` 中的独立 worker 记录。共享 `trace_logs` 存在 seed0/seed1 同名文件互相覆盖的问题，因此不作为统计来源。

## 1. 结论摘要

1. **checkpoint 的完整 ROI-CPI 主干已经形成可用 baseline。** seed0/seed1 的 workload-macro ROI mean 分别为 **4.849%/4.864%**，train/base 为约 **2.24%**，两个 seed 几乎完全复现。
2. **跨 seed 稳定性相对 v28 明显改善。** 56 对业务 trace 的 seed0/seed1 预测 CPI 差异均值从 v28 的 **2.946%** 降到 v29 的 **0.081%**，p90 从 **6.217%** 降到 **0.201%**，最大值从 GoFeed c32 的 **29.108%** 降到 **0.612%**。seed0/seed1 业务 cache 是不同输入文件，并非报告重复计数。
3. **heldout 平均误差降到约 10.83%，但仍被 Redis 单点主导。** 排除 `redis_heldout` 后，heldout-6 的 ROI mean 仅为 seed0 **5.56%**、seed1 **5.61%**；`redis_heldout` 则稳定地被低估约 **42.33%**，是当前最明确的机制覆盖缺口。
4. **相对 v28，业务 CPI 泛化总体改善。** 两 seed 平均的 business-base ROI 从约 **3.54%** 降到 **2.28%**，heldout 从约 **13.06%** 降到 **10.83%**。Marine、MySQL、BVC、PyTorch 明显改善；Flink、GoFeed 和 Redis 退化。
5. **快慢核问题改善但没有完全解决。** `memory_seq_moderate c32` 的逐核 MAPE 从 v28 两个 seed 约 **12.53%** 降到 **7.40%**，CPI rank Spearman 达 **0.941**、pairwise ordering 达 **91.9%**，最慢四分位召回为 **75%**。但 core 13/9 仍被明显低估，core 8/7 等被高估，最大终点误差仍达到 **1.231M cycles**。
6. **神经 branch head 仍不适合部署。** 全部 trace 的 branch miss count 相对误差约 **30.6%**，heldout 约 **74.6%**、绝对 rate 误差约 **4.38 pp**。方向型 functional gshare replay baseline 明显更好，heldout 为约 **13.5%/0.84 pp**，但它缺少 target component，仍不能视为完整 predictor。
7. **推理机制和覆盖完整性通过。** 184/184 trace、2.293B ROI UOP 全部消费；无 no-progress、无 stride overshoot、无剩余 UOP。160 次 `<4 cycles` 仅是 advisory min-step 事件，占约 800k step 的 0.02%，没有导致停滞。
8. **当前 checkpoint 可作为 v29 timing baseline，但不是最终部署版本。** Redis、神经 branch head、少数慢核和 stepwise drift 审计仍未通过；尤其本轮关闭了 oracle drift diagnostics，不能用本报告证明“同一窗口的 million-cycle 偏移”已经消失。

## 2. 评估合同与产物

| 项目 | 配置 |
|---|---|
| Checkpoint | `ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt` |
| Checkpoint step | 59,000 |
| Checkpoint ID | `74e9fcc88db18d7a4e4c94848ba2545821226e433174f7320e33acd494f76dc8` |
| 数据集 | `v29_global_time_dataset`，schema `global-time-v29-packed-3` |
| 输入合同 | `functional_only_v29_global_time_prefix` |
| 测试 split | `seed0_inference,development_heldout,deployment_inference` |
| 覆盖 | seed0/seed1 各 92 条；每 seed 的 c4/c8/c16/c32 各 23 条 |
| Workloads | 9 个 mechanism、7 个 business base、7 个 business heldout |
| 推理模式 | single-global-time free-running；predicted cursor 构建下一步 context |
| Oracle 使用 | 不作为模型输入，不构建 rollout context；只在完成 transition 后计算最终指标 |
| Target stride | 256 UOP |
| Step cycles | advisory minimum 4，maximum 1024 |
| 数值路径 | bf16，SDPA auto，FP64 prefix timing accumulation |
| 快路径 | free fast path 开启，horizon outputs 关闭 |
| Drift diagnostics | 关闭 |

原始产物：

- [全量聚合 JSON](../logs/v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full/report.json)
- [全量文字报告](../logs/v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full/report.txt)
- [启动日志](../logs/v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full/launcher.log)
- [训练 metrics](../ckpt/tcsim_v29_packed3_100m_8gpu_60k/metrics.json)
- [训练配置](../configs/v29_100m.yaml)
- [全量推理启动脚本](../scripts/tmp/launch_v29_packed3_seed0_seed1_c04_c08_c16_c32_free_s256.sh)

指标口径：

- ROI mean、p50、p90 均为 workload/trace 等权 macro，不能用 pooled CPI 替代。
- `signed bias < 0` 表示 CPI 低估，`> 0` 表示高估。
- `core MAPE` 先在每条 trace 内计算逐核相对误差，再做 trace 等权平均。
- Branch relative 是完整 ROI branch-miss count 相对误差；branch abs 是 miss rate 的绝对百分点差。
- Makespan error 是预测共享虚拟时间终点相对真实最晚核心终点的误差。

## 3. checkpoint 选择与训练末期状态

`best.pt` 由 `val_total` 选择，最佳点在 59k；60k 的 last checkpoint 已回退。

| Step | val total | commit time | prefix BCE | progress count | cumulative | branch token/count | progress MAE |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1k | 1.34874 | 0.35942 | 1.84934 | 0.07880 | 0.04903 | 0.12679 / 0.00313 | 23.045 |
| 10k | 0.64242 | 0.14608 | 0.93207 | 0.03603 | 0.02259 | 0.06478 / 0.00168 | 10.713 |
| 30k | 0.55896 | 0.12656 | 0.81262 | 0.03185 | 0.02209 | 0.04523 / 0.00113 | 9.472 |
| 50k | 0.50723 | 0.11939 | 0.72522 | 0.03062 | 0.02254 | 0.04178 / 0.00106 | 9.102 |
| 58k | 0.49931 | 0.11623 | 0.71740 | 0.02973 | 0.01998 | 0.04410 / 0.00108 | 8.912 |
| **59k** | **0.48318** | **0.11288** | **0.69225** | **0.02918** | **0.01885** | 0.04757 / 0.00113 | **8.739** |
| 60k | 0.50551 | 0.11771 | 0.72524 | 0.03053 | 0.02210 | **0.04275** / 0.00110 | 9.089 |

59k 的加权 loss 构成如下：

```text
commit_time          0.112883                         23.36%
0.5 * prefix_bce     0.346126                         71.63%
0.5 * progress_count 0.014591                          3.02%
0.25 * cumulative    0.004711                          0.98%
0.1 * branch_token   0.004757                          0.98%
0.1 * branch_count   0.000113                          0.02%
total                0.483181                        100.00%
```

训练末期仍有波动，但完整 free-running 结果支持选择 59k 而不是 60k。当前证据不支持单纯继续延长训练来解决 Redis 或慢核尾部问题；这些问题更符合机制覆盖和状态表达缺口。

## 4. 完整性和运行性能

| 指标 | seed0 | seed1 |
|---|---:|---:|
| 完成 traces | 92/92 | 92/92 |
| ROI UOP | 1,146,442,446 | 1,146,450,199 |
| Scheduler steps | 400,257 | 400,273 |
| Model forwards | 400,257 | 400,273 |
| 剩余 UOP | 0 | 0 |
| Stride overshoot | 0 | 0 |
| Advisory min-step violations | 78 | 82 |
| No-progress events | 0 | 0 |
| 单 trace elapsed 总和 | 15,357.8 s | 15,342.5 s |
| GPU peak allocated | 1.63 GiB | 1.63 GiB |

联合 8-GPU 任务从 16:37:30 运行到约 17:51:20，墙钟约 **73 分 50 秒**。总计 2.293B UOP，对应整机有效吞吐约 **517k UOP/s**。

按 core count 做两个 seed 等权平均：

| Cores | v29 UOP/s | v29 useful UOP/forward | v28 UOP/s | v28 UOP/forward |
|---:|---:|---:|---:|---:|
| 4 | 58,966 | 865.7 | 60,974 | 1,013.1 |
| 8 | 81,439 | 1,639.6 | 79,309 | 2,018.7 |
| 16 | 90,493 | 3,127.3 | 82,217 | 4,019.5 |
| 32 | 80,790 | 5,898.2 | 74,900 | 8,011.3 |

target-stride=256 下每次 forward 消费的 UOP 仍少于 v28 固定 chunk 路径，但优化后的 c8/c16/c32 trace throughput 已高于 v28。c32 比 c16 回落，主要仍是 context 和大窗口模型计算成本，而不是每步只推进 32 UOP。

累计 trace 时间的阶段占比在两个 seed 上几乎一致：

| 阶段 | seed0 | seed1 |
|---|---:|---:|
| Context build | 44.27% | 44.44% |
| Predict total | 54.89% | 54.71% |
| 其中 model forward | 53.79% | 53.62% |
| Scheduler | 0.82% | 0.83% |
| D2H output | 0.16% | 0.16% |
| CPU last-window cache hit | 1.28% | 1.28% |

本次运行在新增 context 子阶段计时前启动，因此只有 context 总时间，没有 select/window/cross-core/state/tensor 的细分；后续运行应按标准框架输出这些子阶段。

## 5. seed0/seed1 分核心数结果

### 5.1 seed0

| Cores | Set | n | ROI mean | ROI p50 | ROI p90 | signed bias | core MAPE | makespan | branch rel. | branch abs | UOP/s |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | all | 23 | 5.07% | 3.19% | 6.80% | -2.84% | 5.21% | 5.30% | 33.19% | 1.71 pp | 58.6k |
| 4 | train/base | 16 | 2.48% | 1.90% | 5.03% | -0.87% | 2.62% | 2.69% | 11.17% | 0.32 pp | 61.5k |
| 4 | heldout | 7 | 11.00% | 5.66% | 23.18% | -7.36% | 11.15% | 11.26% | 83.52% | 4.88 pp | 52.1k |
| 8 | all | 23 | 4.62% | 2.81% | 6.43% | -3.09% | 4.71% | 4.59% | 31.18% | 1.60 pp | 81.4k |
| 8 | train/base | 16 | 2.11% | 1.77% | 4.18% | -1.19% | 2.18% | 2.01% | 11.48% | 0.33 pp | 86.2k |
| 8 | heldout | 7 | 10.37% | 6.37% | 21.55% | -7.43% | 10.47% | 10.47% | 76.20% | 4.50 pp | 70.3k |
| 16 | all | 23 | 4.44% | 1.90% | 6.73% | -2.58% | 4.73% | 4.61% | 29.77% | 1.50 pp | 90.5k |
| 16 | train/base | 16 | 2.00% | 1.28% | 4.64% | -0.71% | 2.40% | 2.12% | 11.41% | 0.32 pp | 96.8k |
| 16 | heldout | 7 | 9.99% | 6.49% | 21.95% | -6.83% | 10.06% | 10.31% | 71.74% | 4.22 pp | 76.1k |
| 32 | all | 23 | 5.27% | 3.06% | 9.49% | -2.45% | 5.43% | 5.11% | 28.13% | 1.42 pp | 80.8k |
| 32 | train/base | 16 | 2.37% | 2.28% | 4.83% | -0.55% | 2.60% | 2.42% | 11.15% | 0.33 pp | 87.4k |
| 32 | heldout | 7 | 11.89% | 6.59% | 24.79% | -6.78% | 11.89% | 11.26% | 66.97% | 3.92 pp | 65.7k |

### 5.2 seed1

| Cores | Set | n | ROI mean | ROI p50 | ROI p90 | signed bias | core MAPE | makespan | branch rel. | branch abs | UOP/s |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | all | 23 | 5.09% | 3.28% | 6.99% | -2.87% | 5.23% | 5.32% | 33.46% | 1.72 pp | 59.3k |
| 4 | train/base | 16 | 2.49% | 1.87% | 4.96% | -0.87% | 2.63% | 2.67% | 11.46% | 0.34 pp | 61.9k |
| 4 | heldout | 7 | 11.04% | 5.76% | 23.05% | -7.43% | 11.19% | 11.36% | 83.74% | 4.87 pp | 53.4k |
| 8 | all | 23 | 4.62% | 2.81% | 6.83% | -3.11% | 4.70% | 4.62% | 31.17% | 1.61 pp | 81.5k |
| 8 | train/base | 16 | 2.07% | 1.77% | 4.01% | -1.18% | 2.14% | 1.97% | 11.41% | 0.34 pp | 86.5k |
| 8 | heldout | 7 | 10.46% | 6.37% | 21.55% | -7.53% | 10.55% | 10.70% | 76.34% | 4.50 pp | 70.1k |
| 16 | all | 23 | 4.47% | 1.92% | 6.83% | -2.56% | 4.76% | 4.61% | 29.50% | 1.51 pp | 90.5k |
| 16 | train/base | 16 | 2.03% | 1.30% | 4.60% | -0.72% | 2.42% | 2.10% | 11.11% | 0.33 pp | 96.9k |
| 16 | heldout | 7 | 10.04% | 6.42% | 22.10% | -6.78% | 10.11% | 10.33% | 71.53% | 4.21 pp | 75.8k |
| 32 | all | 23 | 5.28% | 3.14% | 9.66% | -2.47% | 5.43% | 5.10% | 28.04% | 1.42 pp | 80.8k |
| 32 | train/base | 16 | 2.39% | 2.28% | 4.80% | -0.55% | 2.62% | 2.44% | 11.25% | 0.34 pp | 87.3k |
| 32 | heldout | 7 | 11.87% | 6.62% | 24.68% | -6.85% | 11.87% | 11.18% | 66.43% | 3.90 pp | 65.8k |

两个 seed 的各行差异通常只有 0.01--0.1 pp，说明结果不是某个 seed 的偶然好点。c32 没有出现 train/base ROI 爆炸，但 all/heldout 比 c16 回升，主要由 Redis 与 GoFeed/Flink heldout 的高核数误差造成。

## 6. 机制、业务 base 与 heldout

四种 core count 合并、trace 等权：

| Seed | Domain | n | ROI mean | p50 | p90 | signed bias | core MAPE | makespan |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | mechanism | 36 | 2.21% | 1.79% | 5.16% | -0.28% | 2.57% | 2.43% |
| 0 | business base | 28 | 2.28% | 2.01% | 4.30% | -1.53% | 2.30% | 2.15% |
| 0 | heldout | 28 | 10.82% | 6.41% | 40.15% | -7.10% | 10.89% | 10.82% |
| 1 | mechanism | 36 | 2.22% | 1.79% | 5.16% | -0.30% | 2.58% | 2.41% |
| 1 | business base | 28 | 2.27% | 2.00% | 4.18% | -1.50% | 2.28% | 2.15% |
| 1 | heldout | 28 | 10.85% | 6.39% | 40.12% | -7.15% | 10.93% | 10.89% |

Pooled CPI 仅作守恒检查：

| Seed | Pred pooled CPI | True pooled CPI | 相对误差 |
|---:|---:|---:|---:|
| 0 | 1.7553 | 1.8195 | 3.53% |
| 1 | 1.7548 | 1.8192 | 3.54% |

pooled 数字低于 workload-macro mean，是因为不同 workload 的 UOP 数和正负误差发生抵消。checkpoint 验收应继续使用 macro ROI mean。

### 6.1 Heldout 逐业务结果

下表按两个 seed、四种 core count 等权。signed bias 为 v29 的方向；v28 只用于同一完整 ROI 合同的方向性对照。

| Heldout workload | v28 ROI | v29 seed0 | v29 seed1 | v29 signed bias | v29 branch rel. | v29 branch abs |
|---|---:|---:|---:|---:|---:|---:|
| BVC encoder | 8.01% | 3.42% | 3.30% | +3.36% | 37.15% | 1.72 pp |
| Flink | 1.64% | 7.14% | 7.51% | -7.32% | 51.50% | 3.00 pp |
| GoFeed | 7.70% | 9.57% | 9.66% | +9.62% | 119.53% | 8.22 pp |
| Marine | 16.84% | 5.71% | 5.67% | -5.69% | 44.62% | 4.36 pp |
| MySQL | 16.89% | 6.20% | 6.17% | -6.19% | 79.28% | 4.84 pp |
| PyTorch | 3.96% | 1.33% | 1.33% | -1.33% | 182.30% | 8.20 pp |
| Redis | 36.41% | 42.33% | 42.33% | -42.33% | 7.53% | 0.30 pp |

关键判断：

- BVC、Marine、MySQL、PyTorch 均明显改善，说明 monotonic prefix/global-time 主干不是整体失效。
- Flink 从约 1.64% 退化到 7.32%，GoFeed 从约 7.70% 退化到 9.62%；它们不是灾难性失败，但说明 v29 的改善不是全 workload 单调。
- Redis 的真实 CPI 为约 2.59--2.68，而模型仍预测约 1.42--1.59，基本停在 base 区间。该误差跨 core count 从约 45.4% 缓慢降到 39.7%，跨 seed 完全复现，不能归因于随机 seed。
- 去除 Redis 后，heldout-6 ROI mean 为 seed0 5.56%、seed1 5.61%，p90 约 8.93%/9.04%。当前 heldout headline 被单个 mechanism hole 显著拉高。

### 6.2 Train/base 尾部

train/base 的主要高误差项也稳定复现：

- `simd_sse_dense`：约 5.34%，系统性低估；
- `fp_alu_dense`：约 3.38%，系统性低估；
- `gofeed_base`：约 3.3%；
- `mysql_base`：约 3.2%；
- `memory_seq_moderate`：aggregate ROI 约 2.87%，但逐核 MAPE 约 5.98%，见下一节。

除上述尾部外，大多数 train/base workload 的完整 ROI 误差在 0.4%--3% 范围。业务 base 在两个 seed 上均约 2.28%，说明 v28 的 seed-dependent GoFeed/MySQL c32 问题已经显著收敛。

## 7. 跨 seed 稳定性

seed0 与 seed1 的总体指标：

| 指标 | seed0 | seed1 | 差值 |
|---|---:|---:|---:|
| 全部 ROI mean | 4.849% | 4.864% | +0.015 pp |
| 全部 ROI p50 | 2.810% | 2.865% | +0.055 pp |
| 全部 ROI p90 | 7.049% | 7.188% | +0.139 pp |
| Mechanism ROI | 2.206% | 2.223% | +0.017 pp |
| Business-base ROI | 2.282% | 2.273% | -0.009 pp |
| Heldout ROI | 10.815% | 10.851% | +0.036 pp |
| 全部 core MAPE | 5.018% | 5.031% | +0.013 pp |
| 全部 makespan | 4.900% | 4.913% | +0.013 pp |

56 对业务 trace 的配对稳定性：

| 指标 | Mean | p90 | Max |
|---|---:|---:|---:|
| seed0/seed1 真实 CPI 差异 | 0.080% | 0.158% | 0.305% |
| seed0/seed1 预测 CPI 差异 | 0.081% | 0.201% | 0.612% |
| seed0/seed1 ROI error 差值 | 0.112 pp | 0.265 pp | 0.673 pp |

业务 seed cache 具有不同 inode、不同 UOP 数，且功能字段 checksum 不同。例如 GoFeed c32 core0 的 seed0/seed1 `fields.npy` checksum 不同。因此上述稳定性不是同一行结果复制。另一方面，部分 synthetic mechanism workload 本身跨 seed 确定性相同；这些完全相同的 trace 不应被当作独立 seed 泛化证据，所以本文单独报告了业务配对结果。

相比 v28，业务跨 seed 预测差异如下：

| 版本 | Mean | p90 | Max |
|---|---:|---:|---:|
| v28.1 A2 | 2.946% | 6.217% | 29.108% |
| v29 packed3 | **0.081%** | **0.201%** | **0.612%** |

这说明 v29 的 functional prefix/state 路径消除了 v28 最明显的 seed 过敏，但不能证明对全新 seed/binary 完成最终泛化，因为 seed1 已经被用于本轮诊断。

## 8. 快慢核与逐核误差

### 8.1 Memory-seq 排序能力

slow-set 使用每条 trace 中真实 CPI 最高的四分之一核心；pairwise 是所有可比较核心对的 CPI 顺序准确率。该 workload 的 seed0/seed1 functional trace 完全相同，因此两 seed 数字相同。

| Cores | Aggregate ROI | core MAPE | core p90 | Rank Spearman | Pairwise order | Slow-quartile recall |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 5.71% | 7.93% | 9.21% | -0.800 | 16.7% | 0/1 |
| 8 | 0.89% | 1.68% | 4.60% | 0.595 | 71.4% | 0/2 |
| 16 | 0.75% | 6.89% | 13.80% | 0.709 | 77.5% | 1/4 |
| 32 | 4.13% | 7.40% | 19.02% | **0.941** | **91.9%** | **6/8** |

c4/c8 的 top-k 集合很小，且核心 CPI 接近时 rank 指标容易跳变；但 c16 的 1/4 与 c32 的 6/8 仍说明“能识别多数快慢核”不等于逐核误差已经消失。

v28 到 v29 的 memory-seq core MAPE 对照：

| Cores | v28 seed0/seed1 | v29 seed0/seed1 |
|---:|---:|---:|
| 4 | 5.04% / 4.98% | 7.93% / 7.93% |
| 8 | 1.47% / 5.10% | 1.68% / 1.68% |
| 16 | 9.30% / 12.51% | 6.89% / 6.89% |
| 32 | 12.68% / 12.38% | **7.40% / 7.40%** |

v29 在 c16/c32 和跨 seed 稳定性上明显改善，但 c4 退化，c8 的 top-slow set 仍不可靠。

### 8.2 c32 的异常核心

`memory_seq_moderate c32` 误差最大的核心如下：

| Core | Pred CPI | True CPI | Signed error | Endpoint error |
|---:|---:|---:|---:|---:|
| 8 | 8.524 | 6.824 | +24.90% | +946,744 cycles |
| 13 | 6.955 | 9.165 | -24.11% | **-1,230,857 cycles** |
| 9 | 7.329 | 9.153 | -19.92% | -1,015,732 cycles |
| 7 | 8.973 | 7.526 | +19.23% | +806,097 cycles |
| 3 | 9.796 | 8.362 | +17.15% | +798,751 cycles |
| 4 | 9.060 | 7.816 | +15.90% | +692,509 cycles |

模型已经把大多数高 CPI 核排到慢核区域，但：

- core 13/9 是漏识别的慢核；
- core 8/7/3/4 是被过度判慢的核心；
- aggregate ROI 的 4.13% 远小于这些 15%--25% 的逐核误差，因为正负误差发生抵消；
- core 13/9 的最终 endpoint 已经出现超过 1M cycles 的负偏移，说明此前关注的“大偏移”没有从最终逐核时间上消失。

所有 trace 的 per-core 最终 endpoint 绝对误差分布：

| Seed/Cores | p50 | p90 | p99 | Max | 最大来源 |
|---|---:|---:|---:|---:|---|
| seed0 c4 | 37k | 156k | 1,041k | 1,042k | Redis heldout |
| seed0 c8 | 26k | 146k | 1,014k | 1,014k | Redis heldout |
| seed0 c16 | 23k | 191k | 985k | 1,018k | memory-seq core9 |
| seed0 c32 | 36k | 245k | 934k | **1,231k** | memory-seq core13 |
| seed1 c4 | 37k | 155k | 1,043k | 1,044k | Redis heldout |
| seed1 c8 | 24k | 146k | 1,012k | 1,013k | Redis heldout |
| seed1 c16 | 22k | 187k | 984k | 1,018k | memory-seq core9 |
| seed1 c32 | 36k | 244k | 932k | **1,231k** | memory-seq core13 |

本表是完整 ROI 的最终 endpoint 误差，不是同一 oracle 窗口的逐步 cursor-offset。由于本轮 `oracle_drift_diagnostics=false`，`oracle_cursor_interval_offset`、slope、cross-core oracle head span 均为 N/A。要判断“同一窗口偏移 1M cycles”是否在运行中持续存在，必须对 memory-seq c16/c32 开启 targeted drift audit；本次全量报告不能替代该审计。

## 9. Branch 结果

### 9.1 神经 head 与 functional replay

| Set | Neural branch rel. | Neural abs | Direction-only replay rel. | Replay abs |
|---|---:|---:|---:|---:|
| All | 30.56% | 1.56 pp | **7.92%** | **0.36 pp** |
| Train/base | 11.30% | 0.33 pp | **5.51%** | **0.15 pp** |
| Heldout | 74.56% | 4.38 pp | **13.50%** | **0.84 pp** |

Neural branch 相对 v28 已大幅改善：heldout relative 从约 365% 降到约 75%，绝对 rate 误差从约 19.1 pp 降到约 4.38 pp。但它仍然在 GoFeed、MySQL、PyTorch heldout 上严重过预测：

- GoFeed：119.5%，8.22 pp；
- MySQL：79.3%，4.84 pp；
- PyTorch：182.3%，8.20 pp。

Redis 的 branch 只有约 7.5%/0.30 pp，而 CPI 误差为 42.3%。这再次说明 Redis CPI 失败不是 branch count 误差造成的。

当前 `functional_gshare_direction_only_replay` 不读取 oracle label，且在本测试集显著优于神经 head。但 packed cache 故意不保存精确 branch target，所以 replay 只覆盖 direction component，无法完整重放 BTB、indirect target、RAS 等 target miss。建议：

1. 部署报告同时保留 neural 与 replay 两套指标；
2. 在完整 target 功能状态可重放前，不把 replay 宣称为完整 branch predictor；
3. 当前 checkpoint 的 neural branch 输出不作为部署 PMU 结论。

## 10. 相对 v28.1 A2 的变化

> v28 与 v29 的训练目标、scheduler 和上下文构造不同，因此不是严格单变量 ablation。完整 ROI CPI、逐核 ROI、makespan 和同一 all-retired-control branch 合同可以做方向性比较；chunk/window/fast-set 指标不可直接互换。

两个 seed 等权平均：

| 指标 | v28.1 A2 | v29 packed3 | 变化 |
|---|---:|---:|---:|
| 全部 ROI mean | 5.84% | **4.86%** | -0.98 pp |
| 全部 core MAPE | 6.21% | **5.02%** | -1.18 pp |
| Mechanism ROI | **2.01%** | 2.21% | +0.21 pp |
| Business-base ROI | 3.54% | **2.28%** | -1.27 pp |
| Heldout ROI | 13.06% | **10.83%** | -2.23 pp |
| 全部 makespan | 6.10% | **4.91%** | -1.19 pp |
| Heldout branch rel. | 365.1% | **74.6%** | -290.5 pp |
| Heldout branch abs | 19.11 pp | **4.38 pp** | -14.73 pp |
| Memory-seq c32 core MAPE | 12.53% | **7.40%** | -5.13 pp |
| 业务跨 seed pred delta mean | 2.946% | **0.081%** | -2.865 pp |

改善最明显的是：

- v28 对 seed-dependent functional sequence 的过敏基本消失；
- Marine/MySQL/BVC/PyTorch heldout CPI 显著下降；
- branch head 虽未通过，但已从完全不可用降到可诊断范围；
- memory-seq c16/c32 的逐核识别明显改善。

退化项是：

- Redis heldout 从约 36.4% 进一步恶化到约 42.3%；
- Flink heldout 从 1.64% 退化到 7.32%；
- GoFeed heldout 从 7.70% 退化到 9.62%；
- 简单 mechanism 的平均 ROI 从 2.01% 小幅升到 2.21%；
- memory-seq c4 的逐核 MAPE 从约 5% 升到 7.93%。

因此 v29 不是“所有 workload 都更准”，而是以更稳定的 common-time/prefix 机制换来了业务整体和大核数逐核能力的改善，同时暴露出 Redis 与少数核心状态表达不足。

## 11. 验收结论

| 验收项 | 结论 | 依据 |
|---|---|---|
| Checkpoint/数据合同 | 通过 | step 59k、checkpoint ID 和全部 trace 合同一致 |
| Free-running 完整性 | 通过 | 184/184，2.293B UOP，0 remaining/no-progress/overshoot |
| Seed0/seed1 可复现性 | 通过 | ROI mean 只差 0.015 pp；业务 pred delta mean 0.081% |
| Train/base ROI CPI | 通过 | 两 seed 均约 2.24%；business base 约 2.28% |
| Heldout ROI CPI | 部分通过 | 平均 10.83%；去 Redis 后约 5.58%，Redis 42.33% |
| c32 aggregate CPI | 通过 | all 约 5.27%，train/base 约 2.38% |
| 快慢核/逐核 CPI | 部分通过 | c32 memory-seq rank 0.941、core MAPE 7.40%，但个别核 20%+ |
| Million-cycle endpoint | 不通过 | memory-seq core13 仍有 -1.231M cycles |
| Stepwise drift | 未验收 | 本轮 oracle drift diagnostics 关闭 |
| Neural branch PMU | 不通过 | heldout 74.6%/4.38 pp |
| Direction replay baseline | 部分通过 | 13.5%/0.84 pp，但缺 target component |
| 推理吞吐 | 通过当前 baseline | c8/c16/c32 高于 v28；context 仍占约 44% |
| 日志可追溯性 | 不通过 | seed0/seed1 共享 trace log 文件名，发生覆盖 |
| 最终部署就绪性 | **不通过** | Redis、branch、少数慢核、drift audit 尚未闭环 |

当前推荐定位：

```text
v29 packed3 step59k = 当前 common-time/prefix timing baseline
                      + 可用于业务 CPI 对比和下一轮消融
                      - 不作为最终 Redis/branch/逐核时间部署版本
```

## 12. 下一步最小闭环顺序

1. **Redis mechanism P0。** 构造 dependent lookup depth、shared table size、Zipf/uniform tail、private-state size 的机制矩阵；先验证现有模型 residual 是否与这些维度相关，再决定补数据还是补状态。
2. **Memory-seq core 13/9/8 targeted audit。** 在 c16/c32 开启 oracle drift diagnostics，报告 cursor interval offset、slope、cross-core head span，并对 core 13/9 的 functional phase/LLC-bank/virtual-state 与普通慢核做差分。
3. **慢核排序与 calibration 分开优化。** c32 rank 已较好，当前更需要修正 core13/9 的低估和 core8/7 的高估；不要只增加全局 aggregate loss。
4. **Branch 采用双路径验收。** neural head 继续报告，deployment 默认同时给 direction replay；补齐 target-component 可辨识信息后再决定是否保留 neural branch head。
5. **修复 seed 日志目录。** 改为 `trace_logs/seed0/c32/...`、`trace_logs/seed1/c32/...`，进度行和文件名同时包含 seed；当前报告 JSON 无需重跑，但若需要完整逐任务日志则必须修复后重跑。
6. **新的 untouched final seed。** seed1 已用于本轮诊断，下一版定型后应使用未参与设计的新 seed/binary 做一次性最终验收。
