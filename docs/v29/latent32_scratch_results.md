# v29 hierarchical-latent32：实现与最终结果

更新时间：2026-08-02

## 1. 结论

`hierarchical_latent` 将每核 256 个 UOP 压缩成 32 个 latent，在 latent 间完成跨核
attention 后再广播回 UOP。它显著降低了 c32 跨核 attention 的计算量和临时显存，最终
使 seed0/base c32 吞吐量从 canonical v29 的约 87.4 K UOP/s 提高到 187.2 K UOP/s，
但 workload-macro ROI-CPI mean 从约 2.37% 退化到 4.57%。完整 heldout 的 c32
ROI-CPI mean 为 16.59%，说明该结构不应作为最终精度方案。

这次实验验证了两件事：

1. 跨核 attention 是 c32 forward 的主要可优化项，压缩跨核信息可以超过 125 K UOP/s。
2. 先把 source 和 target 都压成 latent，再广播回 UOP，会丢失稀疏 coherence、地址相位、
   dependency 和程序位置关系。下一版应保留每个 target UOP 的 Query，只压缩远端 K/V。

## 2. 模型与训练合同

最终训练配置：`configs/v29_latent32_scratch_60k.yaml`。

| 项目 | 设置 |
|---|---:|
| 动态宽度 | 960 |
| Attention heads | 15 |
| 层数 | 8 |
| FFN | 3840 |
| 每核 UOP | 256 |
| 每核 latent | 32 |
| 参数量 | 112,513,524 |
| 初始化 | 全参数随机初始化 |
| 训练方式 | 从头、端到端，无 teacher，无冻结 |
| 训练精度 | BF16 主路径；三段 latent attention 使用 FP32 |
| 总 step | 60,000 |
| 固定里程碑 | `step_30000.pt` |
| 最优 checkpoint | step 55,000 |

训练完成 60,000 step，用时 25,039.65 秒，即 6 小时 57 分 20 秒，平均 2.396 step/s。
训练全程通过 forward、DDP backward/all-reduce、optimizer 和 checkpoint finite guard，未再
产生 NaN checkpoint。

最优 validation：

| 指标 | step 55,000 |
|---|---:|
| total | 0.461152 |
| commit log MAE | 0.161766 |
| progress MAE | 8.2161 UOP |

注意：这组 sequence validation 来自已有 base trace，不能代表 workload-domain heldout
泛化能力。最终 checkpoint 选择仍需增加未见合成变体的 full-trace rollout 门禁。

## 3. Forward 微基准

测试硬件为 NVIDIA H20，形状为 c32、K=256、D=960、15 heads、BF16。

| 优化 | 基线 | 优化后 | 加速 | 其他收益 |
|---|---:|---:|---:|---:|
| shared-K/V FlexAttention | 2.5418 ms | 2.2546 ms | 1.127x | 临时显存减少 992,223,744 B |
| fused Q/R/K/V projection | 0.5200 ms | 0.4445 ms | 1.170x | state dict 与参数量不变 |
| hierarchical-latent32 cross | 约 2.52 ms | 约 0.56 ms | 约 4.5x | 避免完整跨核 score matrix |

其中 fused projection 是数学等价的 eval-only 优化；latent32 是改变信息拓扑的近似模型，
必须用匹配配置重新训练。

可复现命令：

```bash
/data00/yinhaolang/infer/.venv/bin/python scripts/benchmark_v29_cross_attention.py
/data00/yinhaolang/infer/.venv/bin/python scripts/benchmark_v29_qrkv_projection.py
/data00/yinhaolang/infer/.venv/bin/python scripts/benchmark_v29_latent_attention.py
```

## 4. Seed0/base 64-trace free rollout

Checkpoint：
`ckpt/tcsim_v29_latent32_scratch_100m_8gpu_60k/best.pt`（step 55,000）。
推理使用 native C++ context、BF16、fused QRKV、hierarchical-latent32；v29 负责 timing，
canonical commit-clock GSS 只生成 PMU，不向 timing model 注入 GSS 特征。

| cores | ROI-CPI mean | p50 | p90 | UOP/s |
|---:|---:|---:|---:|---:|
| 4 | 3.10% | 2.23% | 5.19% | 70,872 |
| 8 | 4.30% | 2.45% | 7.40% | 121,487 |
| 16 | 5.20% | 3.17% | 7.93% | 167,413 |
| 32 | 4.57% | 2.24% | 7.41% | 187,165 |

c32 已明显超过 125 K UOP/s，但 16 个 workload 中的
`coh_readmostly_sparse` CPI 误差随核心数从 14.54% 上升到 28.14%，成为最明确的结构性
回归。排除该 workload 后 c32 base mean 约为 3.00%，仍高于 canonical v29。

完整报告：
`logs/v29_latent32_best55k_gss_c04_c32_full/report.txt`。

## 5. Seed1 与 heldout 120-trace free rollout

补充评估覆盖 120/120 traces、1,496,359,099 ROI UOP，失败数为 0：

- seed0 `development_heldout`：7 workloads × 4 core counts = 28 traces；
- seed1 `deployment_inference`：23 workloads × 4 core counts = 92 traces。

| cores | 全部 ROI mean | seed1 base | heldout | 全部 UOP/s | heldout UOP/s |
|---:|---:|---:|---:|---:|---:|
| 4 | 6.89% | 3.10% | 15.55% | 66,995 | 56,781 |
| 8 | 7.62% | 4.27% | 15.27% | 111,781 | 88,662 |
| 16 | 8.34% | 5.20% | 15.51% | 151,672 | 115,325 |
| 32 | 8.23% | 4.57% | 16.59% | 167,602 | 124,194 |

seed1 base 与 seed0 base 基本一致，说明问题不是随机 seed shift。heldout 的大误差稳定出现
在两个 seed，因此根因是 workload/domain 泛化以及 latent 信息压缩：

| heldout workload | c4 | c8 | c16 | c32 |
|---|---:|---:|---:|---:|
| Redis | 37.94% | 36.83% | 35.79% | 34.92% |
| BVC encoder | 21.19% | 22.10% | 22.83% | 17.62% |
| PyTorch | 13.33% | 14.39% | 16.49% | 20.38% |
| Marine | 29.23% | 21.03% | 10.53% | 3.29% |
| MySQL | 3.77% | 7.00% | 14.04% | 21.96% |
| Flink | 2.93% | 4.51% | 7.98% | 13.15% |
| Gofeed | 0.48% | 1.00% | 0.93% | 4.84% |

完整报告：
`logs/v29_latent32_best55k_gss_seed1_plus_heldout120/report.txt`。

## 6. 失败原因与后续约束

当前实现的三段路径是：

```text
learned Query: 256 UOP/core -> 32 latent/core
latent Query: local latent -> other-core latent
UOP Query: 256 UOP/core -> 32 updated local latent
```

它的复杂度低，但第二段跨核交互只在 32 个共享 latent Query 上发生。目标 UOP 在第三段
只能读取已经按 target-core latent 聚合过的远端信息，难以恢复某条 load 与远端某条
store/load 的精确短尺度关系。增加到 64 latent 只能减轻容量压力，不能消除这一拓扑瓶颈。

下一版必须满足：

1. 保留全部 target UOP Query，只压缩 source/remote K/V；
2. anchor 显式保留位置和稀疏语义，不能只用 learned seeds；
3. local 层与 cross 层解耦，优先验证 8 local + 2 cross；
4. 正式 heldout 不进入训练；另建 domain-randomized train/validation cache；
5. full-trace CPI、最坏 workload 和实际 UOP/s 共同选择 checkpoint，不能只看 sequence MAE。
