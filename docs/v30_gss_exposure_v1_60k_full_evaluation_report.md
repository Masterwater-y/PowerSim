# TCSim v30 GSS Exposure-v1 60k 全量推理评估报告

> 状态：全量评估完成，但当前 checkpoint 未通过替代 v29 baseline 的门槛。
>
> 评估完成时间：2026-07-30 14:44:48（Asia/Shanghai）。
>
> 覆盖：seed0 development heldout 与 seed1 deployment inference，C4/C8/C16/C32 共 120 条 trace，全部完成，无失败。
>
> 公式兼容性：本文件不使用 LaTeX；所有指标定义均使用纯文本，避免 Markdown 渲染器无法解析公式。

## 1. 结论摘要

1. **当前 Exposure-v1 60k 整体不如 v29。** 相同 120 条 trace 的 workload-equal ROI-CPI MAPE 从 v29 的 **4.859%** 上升到 **5.409%**，退化 **0.550 pp**，相对增加约 **11.3%**。
2. **只有 C4 总体改善，C8/C16/C32 均退化。** C4 改善 0.226 pp；C8、C16、C32 分别退化 0.764、1.149、0.513 pp。当前 Exposure/router 没有形成稳定的核心数泛化。
3. **Redis-heldout 没有得到实质解决。** 四种核心数平均误差从 **42.326%** 降到 **41.612%**，仅改善 0.714 pp；C32 只改善 0.112 pp，C16 反而退化 0.759 pp。
4. **结果表现为负载间重新校准，而不是统一收益。** 120 条 trace 中 66 条改善，但 PyTorch、Marine、memory-seq 等少数负载的大幅退化吞掉了 MySQL、Flink、BVC 等负载的收益。
5. **GSS 的 cache-miss 计数总体较准，但计数准确没有转化为 timing 收益。** LLC miss trace MAPE 为 **0.793%**；Redis-heldout 的 LLC miss 为 **0%**，但 CPI 仍低估约 42%。问题已经从“是否识别 miss”收敛为“如何把 miss 映射为可见 commit 停顿”。
6. **正式 Branch PMU 必须使用 configured Tournament replay。** 120 条 trace 的显式 replay MAPE 为 **1.958%**，pooled count error 为 **0.142%**，heldout MAPE 为 **0.318%**。当前推理原始报告中的 neural `BRerr` 无效，禁止用于模型结论。
7. **当前 checkpoint 不是设计文档中的完整 v30。** 它实际是“v29 + GSS + Exposure”：配置没有 `branch_mode=replay_event_history`，checkpoint 仍保留 neural `branch_head`，训练和推理 manifest 也没有接入 branch replay sidecar。
8. **当前版本推理吞吐明显下降。** 相同 120 条 trace 的 pooled 单 trace 吞吐从 v29 的 72.2k UOP/s 降到 45.7k UOP/s，下降约 36.7%。现有 v29 全量报告使用较早 runtime，严格吞吐 A/B 仍需在当前 canonical runtime 上重跑 v29。
9. **当前 checkpoint 不应替代 v29 作为默认模型。** 下一步应先做同 checkpoint 的 GSS-on/gap0 消融，定位退化来自在线 adapter/router 注入还是 60k 联合微调后的共享主干漂移；不应直接增加训练步数。

## 2. 评估对象与产物

| 项目 | 内容 |
|---|---|
| Checkpoint | `ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234/best.pt` |
| Step | 60,000 |
| Checkpoint ID | `6c224229ee4e0929371f742393a78a6824bb1ac822cc8026b709569d2c8face4` |
| 训练配置 | `configs/v30_gss_exposure_v1_60k.yaml` |
| 推理 manifest | `data/v30_exposure_v1_inference_dataset/manifest.json` |
| 测试 split | `development_heldout,deployment_inference` |
| 核心数 | 4、8、16、32 |
| Trace 数 | 每种核心数 30 条，共 120 条 |
| 推理模式 | single-global-time free-running，serial |
| Target stride | 256 UOP |
| Timing | canonical retirement gap，FP64 prefix accumulation |
| GSS 时钟 | commit clock |
| GSS 顺序 | predicted commit cycle，然后 core ID、UOP 顺序 |
| Oracle 使用 | 仅在 rollout 完成后计算指标，不作为模型输入 |
| 完成状态 | 120/120，failure=0 |

原始产物：

- [v30 全量 JSON](../logs/v30_gss_exposure_v1_best60k_seed1_heldout_c32_c04_serial_s256_8gpu/report.json)
- [v30 全量文字报告](../logs/v30_gss_exposure_v1_best60k_seed1_heldout_c32_c04_serial_s256_8gpu/report.txt)
- [v30 controller 日志](../logs/v30_gss_exposure_v1_best60k_seed1_heldout_c32_c04_serial_s256_8gpu/controller.log)
- [v30 训练 metrics](../ckpt/tcsim_v30_gss_exposure_v1_60k_seed1234/metrics.json)
- [v30 训练配置](../configs/v30_gss_exposure_v1_60k.yaml)
- [v29 baseline 全量 JSON](../logs/v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full/report.json)
- [v29 baseline 评估报告](v29_packed3_checkpoint_evaluation_report.md)
- [Branch replay manifest](../data/v30_branch_replay_dataset/manifest.json)
- [seed1 Branch replay 汇总](../logs/branch_replay_c04_c32_20260720_204917/summary.md)
- [v30 总体设计](v30_global_shared_system_design.md)

## 3. 指标口径

本报告的 CPI 主指标使用 workload-equal MAPE：

```text
单 trace CPI error = abs(predicted ROI-CPI - true ROI-CPI) / true ROI-CPI

workload-equal MAPE：
1. 先对同一 workload 的 seed 做等权平均；
2. 再对 workload 做等权平均；
3. 综合结果再对 C4/C8/C16/C32 等权平均。
```

其他定义：

```text
signed bias = (predicted ROI-CPI - true ROI-CPI) / true ROI-CPI

signed bias < 0：模型低估周期。
signed bias > 0：模型高估周期。

PMU trace MAPE：每条 trace 的 count relative error 等权平均。
PMU pooled error：先汇总所有 count，再计算相对误差。
```

禁止用 pooled CPI 代替 workload-equal MAPE，因为大 trace 会掩盖负载间退化。

## 4. 完整性与运行状态

| 指标 | 结果 |
|---|---:|
| 完成 trace | 120/120 |
| C4/C8/C16/C32 | 各 30/30 |
| Worker | 8 个，每个完成 15 条 |
| Failure | 0 |
| 总 ROI UOP | 1,496,359,099 |
| Scheduler steps | 593,625 |
| Model forwards | 593,625 |
| 单 trace elapsed 累计 | 32,742.7 s |
| 8 卡墙钟 | 约 84 分钟 |
| 最终状态 | PASS |

完整性 PASS 只表示 rollout、计数和报告生成成功，不表示模型精度通过。

## 5. v30 与 v29 的总体 CPI 对比

### 5.1 分核心数

| 核数 | v29 ROI-CPI MAPE | v30 ROI-CPI MAPE | 变化 | 判断 |
|---:|---:|---:|---:|---|
| C4 | 5.087% | 4.861% | -0.226 pp | 小幅改善 |
| C8 | 4.609% | 5.374% | +0.764 pp | 退化 |
| C16 | 4.458% | 5.607% | +1.149 pp | 明显退化 |
| C32 | 5.280% | 5.793% | +0.513 pp | 退化 |
| 综合 | 4.859% | 5.409% | +0.550 pp | 整体不通过 |

Signed bias：

| 范围 | v29 | v30 | 变化 |
|---|---:|---:|---:|
| 综合 | -2.744% | -3.292% | 低估加重 0.548 pp |
| Train/base | -0.828% | -1.928% | 低估加重 1.100 pp |
| Heldout | -7.125% | -6.411% | 低估略减 0.714 pp |

120 条配对 trace 中，v30 有 66 条误差小于 v29。这个“胜率”不能推翻总体退化结论：大幅回归集中在 PyTorch、Marine 和 memory-seq，损失的绝对幅度远大于多数小幅收益。

### 5.2 Train/base 与 heldout

| 范围 | Trace | Workload | v29 | v30 | 变化 |
|---|---:|---:|---:|---:|---:|
| Train/base | 64 | 16 | 2.245% | 2.706% | +0.461 pp |
| Business heldout | 56 | 7 | 10.833% | 11.586% | +0.753 pp |

训练分布和 heldout 同时退化，因此不能简单解释为“只有 heldout 数据覆盖不足”。当前联合微调同时改变了已知负载和未见负载的标定。

## 6. 分负载 CPI 对比

四种核心数合并；heldout 同时平均 seed0/seed1。

| Workload | n | v29 | v30 | 变化 |
|---|---:|---:|---:|---:|
| `bvc_encoder_base` | 4 | 2.807% | 2.516% | -0.291 pp |
| `bvc_encoder_heldout` | 8 | 3.363% | 1.663% | -1.701 pp |
| `cache_L1_mixed` | 4 | 1.377% | 1.236% | -0.141 pp |
| `cache_L2_mixed` | 4 | 2.399% | 3.759% | +1.360 pp |
| `coh_readmostly_sparse` | 4 | 1.013% | 3.935% | +2.921 pp |
| `flink_base` | 4 | 1.213% | 0.642% | -0.570 pp |
| `flink_heldout` | 8 | 7.325% | 3.857% | -3.468 pp |
| `fp_alu_dense` | 4 | 3.379% | 1.827% | -1.552 pp |
| `gofeed_base` | 4 | 3.255% | 1.512% | -1.743 pp |
| `gofeed_heldout` | 8 | 9.615% | 8.110% | -1.505 pp |
| `int_alu_dense` | 4 | 0.432% | 0.304% | -0.128 pp |
| `int_div_serial` | 4 | 1.811% | 0.614% | -1.198 pp |
| `marine_base` | 4 | 1.826% | 1.929% | +0.103 pp |
| `marine_heldout` | 8 | 5.691% | 9.418% | +3.727 pp |
| `memory_random_mlp` | 4 | 1.377% | 2.245% | +0.868 pp |
| `memory_seq_moderate` | 4 | 2.871% | 8.078% | +5.207 pp |
| `mysql_base` | 4 | 3.151% | 2.619% | -0.532 pp |
| `mysql_heldout` | 8 | 6.185% | 1.781% | -4.404 pp |
| `pytorch_base` | 4 | 1.251% | 7.179% | +5.928 pp |
| `pytorch_heldout` | 8 | 1.328% | 14.663% | +13.335 pp |
| `redis_base` | 4 | 2.409% | 1.544% | -0.865 pp |
| `redis_heldout` | 8 | 42.326% | 41.612% | -0.714 pp |
| `simd_sse_dense` | 4 | 5.344% | 3.354% | -1.990 pp |

主要正收益：MySQL-heldout、Flink-heldout、BVC-heldout、Gofeed-heldout。

主要负收益：PyTorch-heldout、PyTorch-base、memory-seq、Marine-heldout、coherence 和 L2 mixed。

这不是统一的“内存负载改善”或“业务负载改善”，而是负载相关的重新校准。

## 7. Heldout 核心数泛化

### 7.1 Redis-heldout

| 核数 | v29 | v30 | 变化 |
|---:|---:|---:|---:|
| C4 | 45.440% | 42.403% | -3.037 pp |
| C8 | 43.043% | 42.578% | -0.465 pp |
| C16 | 41.102% | 41.861% | +0.759 pp |
| C32 | 39.719% | 39.607% | -0.112 pp |

Redis 的收益几乎全部来自 C4。高核心数没有形成稳定改善，说明 GSS/Exposure 尚未捕获 Redis 的长期 memory-service 与可见停顿机制。

### 7.2 PyTorch-heldout

| 核数 | v29 | v30 | 变化 |
|---:|---:|---:|---:|
| C4 | 0.062% | 11.736% | +11.674 pp |
| C8 | 0.395% | 12.364% | +11.969 pp |
| C16 | 1.023% | 15.039% | +14.017 pp |
| C32 | 3.830% | 19.512% | +15.682 pp |

退化随核心数单调扩大，是当前最明确的 exposure/core-scale 泛化失败。

### 7.3 Marine-heldout

| 核数 | v29 | v30 | 变化 |
|---:|---:|---:|---:|
| C4 | 8.232% | 7.191% | -1.042 pp |
| C8 | 6.134% | 9.731% | +3.597 pp |
| C16 | 3.437% | 11.866% | +8.429 pp |
| C32 | 4.963% | 8.885% | +3.923 pp |

Marine 从 v29 的低估转成 v30 的高估，四种核心数平均 signed bias 从 -5.69% 变成 +9.42%，说明不是随机误差，而是系统性过度修正。

### 7.4 稳定改善的 heldout

| Workload | C4 变化 | C8 变化 | C16 变化 | C32 变化 |
|---|---:|---:|---:|---:|
| BVC-heldout | -3.071 pp | -2.667 pp | +0.024 pp | -1.090 pp |
| Flink-heldout | -3.898 pp | -3.782 pp | -2.197 pp | -3.993 pp |
| MySQL-heldout | -4.132 pp | -4.753 pp | -4.733 pp | -3.997 pp |
| Gofeed-heldout | +2.051 pp | +0.394 pp | -2.508 pp | -5.957 pp |

Flink 和 MySQL 是当前最稳定的收益负载；Gofeed 的收益只在高核心数出现。

## 8. Branch-miss PMU：必须使用显式 replay

### 8.1 当前原始报告的错误路径

当前 Exposure-v1 配置没有声明 `branch_mode`，因此使用默认 `neural_head`。checkpoint 仍包含 `branch_head.*` 参数，推理 manifest 的 379 条记录中没有任何 `branch_replay_dir`。推理代码看到 neural head 输出后，会优先累计 neural probability。

因此原始 `report.txt` 中的以下字段无效：

```text
BRerr
predicted_branch_misses
predicted_branch_miss_rate
branch_miss_count_abs_relative_error
branch_miss_rate_abs_error_pp
```

这些字段不能用于 v30 的正式 Branch PMU 结论，也不能用来解释 CPI。

### 8.2 正式显式 replay 结果

正式 Branch PMU 使用 configured Tournament correct-path replay sidecar。它只消费功能信息，不读取 gem5 `mispredicted` 标签。

| 范围 | Trace | Replay/True miss | Trace MAPE | Pooled error | Rate delta |
|---|---:|---:|---:|---:|---:|
| 全部 | 120 | 2,094,754 / 2,091,782 | 1.958% | 0.142% | 0.00655 pp |
| C4 | 30 | 139,710 / 139,545 | 1.961% | 0.118% | 0.00546 pp |
| C8 | 30 | 279,491 / 279,119 | 1.960% | 0.133% | 0.00615 pp |
| C16 | 30 | 558,221 / 557,430 | 1.974% | 0.142% | 0.00654 pp |
| C32 | 30 | 1,117,332 / 1,115,688 | 1.939% | 0.147% | 0.00679 pp |
| Train/base | 64 | 1,046,964 / 1,047,554 | 3.393% | 0.056% | 0.00212 pp |
| Heldout | 56 | 1,047,790 / 1,044,228 | 0.318% | 0.341% | 0.02028 pp |

Train/base 的 trace MAPE 较高主要由少数合成负载的极小 miss 分母造成；其 pooled error 只有 0.056%，应同时查看绝对 rate delta。

Redis-heldout：

| Seed | 核数 | Replay | gem5 | 误差 |
|---:|---:|---:|---:|---:|
| 0 | 4 | 2,372 | 2,371 | 0.042% |
| 0 | 8 | 4,772 | 4,766 | 0.126% |
| 0 | 16 | 9,577 | 9,566 | 0.115% |
| 0 | 32 | 19,329 | 19,309 | 0.104% |
| 1 | 4 | 2,342 | 2,338 | 0.171% |
| 1 | 8 | 4,647 | 4,637 | 0.216% |
| 1 | 16 | 9,416 | 9,396 | 0.213% |
| 1 | 32 | 19,057 | 19,017 | 0.210% |

Branch replay 与 timing checkpoint 无关，因此 v29/v30 的正式 Branch PMU 应使用同一份显式 replay，不再比较两个 neural head。

## 9. Cache-miss PMU：在线 GSS 状态机

### 9.1 运行语义

Cache PMU 不是对静态地址序列一次性计算，而是由 free-running 推理进度驱动：

```text
当前 canonical cache state
  -> 对候选窗口建立 transactional shadow
  -> 按 provisional/predicted commit 顺序 preview
  -> 单次 QKVR forward 确定本步退休前缀
  -> 只把实际退休的访存提交到 canonical state
  -> 实时累计 L1D/L2/LLC miss
  -> 下一步继续使用更新后的 canonical state
```

最终 PMU count 来自 rollout 完成时的 canonical state。gem5 `path_class` 只在完整 rollout 结束后读取并计算误差，既不是模型输入，也不参与状态转移。

Branch replay 不需要预测时间或跨核顺序。它可以按每核 committed functional branch 顺序离线预计算逐事件 miss，再随推理 cursor 消费；这与推理时逐分支更新 BPU 状态等价。

### 9.2 全部 120 条 trace

| PMU | Trace MAPE | Median | Pooled signed error | Predicted/True |
|---|---:|---:|---:|---:|
| L1D miss | 4.459% | 2.063% | +1.261% | 20,647,299 / 20,390,213 |
| L2 miss | 5.031% | 3.326% | -2.898% | 16,410,016 / 16,899,799 |
| LLC miss | 0.793% | 0.0067% | +0.960% | 14,001,745 / 13,868,591 |

### 9.3 分核心数 Trace MAPE

| 核数 | L1D | L2 | LLC |
|---:|---:|---:|---:|
| C4 | 4.240% | 4.780% | 0.801% |
| C8 | 4.610% | 5.148% | 0.812% |
| C16 | 4.557% | 5.115% | 0.810% |
| C32 | 4.427% | 5.079% | 0.750% |

PMU 误差随核心数基本稳定，没有出现 C32 单独爆炸。

### 9.4 Redis-heldout

| 核数 | L1D error | L2 error | LLC error |
|---:|---:|---:|---:|
| C4 | 0.022% | 9.093% | 0% |
| C8 | 0.023% | 9.053% | 0% |
| C16 | 0.026% | 9.071% | 0% |
| C32 | 0.029% | 9.066% | 0% |

Redis 的 LLC 0% 不是 cycle-exact cache 建模的证据。以 seed0 C32 为例：

- 访存事件：1,253,196；
- 唯一物理 cache line：244,447；
- gem5 LLC miss：244,447；
- 每条唯一 cache line 恰好只产生一次 LLC miss；
- 没有同一 cache line 的第二次 LLC miss；
- 约 14.92 MiB 的唯一行工作集小于当前 64 MiB LLC。

因此这个 trace 的 LLC count 退化为“每条唯一物理行一次 compulsory miss”。它不能验证容量淘汰、冲突淘汰、coherence transient 或复杂跨核次序。

### 9.5 Cache count 与 CPI 的关系

| Workload | 相对 v29 CPI 变化 | L1 error | L2 error | LLC error |
|---|---:|---:|---:|---:|
| Redis-heldout | -0.714 pp | 0.02% | 9.07% | 0.00% |
| PyTorch-heldout | +13.335 pp | 0.47% | 0.17% | 0.03% |
| Marine-heldout | +3.727 pp | 6.85% | 2.20% | 5.56% |
| MySQL-heldout | -4.404 pp | 3.65% | 7.11% | 2.32% |
| Flink-heldout | -3.468 pp | 2.07% | 4.27% | 0.00% |
| memory-seq | +5.207 pp | 0.00% | 0.00% | 0.00% |
| coherence sparse | +2.921 pp | 0.00% | 0.04% | 0.00% |

LLC count error 与“v30 相对 v29 的 CPI 变化”相关系数约为 -0.01，基本没有相关性。这说明当前主要瓶颈不是 cache miss 事件识别，而是：

- miss service latency；
- MSHR 占用和阻塞；
- LLC/DRAM bank 排队；
- 带宽饱和；
- MLP 能隐藏的延迟；
- miss 是否位于 commit critical path；
- exposure/router 如何把访存影响传播给后续依赖与非访存 UOP。

## 10. 推理吞吐与开销

相同 120 条 trace 的 pooled 单 trace吞吐：

| 核数 | v29 UOP/s | v30 UOP/s | 下降 |
|---:|---:|---:|---:|
| C4 | 55.5k | 42.3k | 23.7% |
| C8 | 74.6k | 53.0k | 29.0% |
| C16 | 81.1k | 48.3k | 40.4% |
| C32 | 70.4k | 43.5k | 38.3% |
| 综合 | 72.2k | 45.7k | 36.7% |

累计计时：

| 阶段 | v29 | v30 | 增长 |
|---|---:|---:|---:|
| Trace elapsed | 20,730.7 s | 32,742.7 s | +58.0% |
| Context build | 9,275.9 s | 14,245.4 s | +53.6% |
| Predict | 11,280.5 s | 17,671.6 s | +56.7% |
| Model forward | 11,055.6 s | 17,314.1 s | +56.6% |
| Scheduler | 170.3 s | 395.0 s | +131.9% |

v30 还比 v29 多执行约 10.9% 的 forward：

```text
v29 forwards = 535,324
v30 forwards = 593,625
```

每次 forward 的平均总时间也从 38.73 ms 上升到 55.16 ms。总退化由“forward 次数增加”和“每次 forward 变慢”共同造成。

GSS 在线计时：

```text
GSS preview 累计 = 2,811.8 s
GSS canonical commit 累计 = 183.4 s
```

preview 是显著开销，但不是全部退化；context build 与 model forward 本身也明显变慢。部分 GSS 子计时嵌套在 predict/context 总计时内，不能把各行直接相加。

公平性限制：v29 全量结果来自较早 runtime，缺少当前 canonical retirement-gap contract 字段。已核对的一条同 trace BVC C4 在新旧 runtime 下 CPI error 只差约 0.01 pp，说明精度主结论大概率稳定；但正式吞吐结论仍应使用当前 runtime 重跑 v29。

## 11. 为什么验证 loss 下降但全量推理变差

训练 validation 的 `val_total` 从 step0 的 0.45835 降到 step60k 的 0.35991，60k 是该训练记录中的最佳点。但完整 free-running workload-equal MAPE 仍比 v29 差。

这说明当前 validation 目标与部署指标不一致：

- validation 是有限 sequence 的 teacher-conditioned/window-level 指标；
- 部署是 predicted-cursor 驱动的完整 free-running；
- 训练 loss 对负载做了采样，但最终指标要求 workload 与核心数等权；
- 窗口误差可在闭环中改变 cursor、GSS 状态和后续 context；
- 少数负载的大幅漂移不会被平均 validation loss 充分惩罚。

因此不能用“val loss 持续下降”证明继续训练会修复 PyTorch、Marine 或 Redis。

## 12. 当前结构与既定 v30 的偏差

既定正式 v30 branch 合同是：

```text
删除 neural branch_head
configured Tournament replay 产生逐分支 miss
replay event/history 在第一个 full-QKVR block 前注入
部署 Branch PMU 直接累计 replay event
```

当前 Exposure-v1 实际情况：

```text
model.branch_mode 未配置，回退为 neural_head
checkpoint 仍含 branch_head 参数
branch loss 权重为 0
Exposure manifest 没有 branch_replay_dir
推理报告使用 neural branch probability
```

因此当前 checkpoint 的准确名称应是：

```text
v29 Full-QKVR + online GSS + Exposure-v1 router
```

而不是完整的：

```text
v29 Full-QKVR + branch replay input + online GSS + Exposure-v1 router
```

Branch neural head 不进入 timing gap，因此把 Branch PMU 改成显式 replay 不会改变本次 CPI 数字；但缺少 replay event/history 输入意味着本轮也没有验证完整 v30 的 branch timing 特征收益。

## 13. 机制判断

### 13.1 已经验证成立

- commit-clock serial GSS 能按预测进度维护 canonical cache state；
- transactional shadow 不会把未退休访存写入 canonical state；
- L1/L2/LLC count 可以在多数 trace 上接近 gem5 aggregate count；
- configured Tournament replay 可以提供高精度 Branch PMU；
- GSS/Exposure 对 MySQL、Flink、BVC 等负载包含有效信号。

### 13.2 尚未验证或已经失败

- 没有证明 GSS 能改善 Redis-heldout；
- 没有证明 cache count 准确会改善 timing；
- 没有形成核心数单调或稳定泛化；
- PyTorch 和 Marine 出现明显 workload shortcut/过度修正；
- 当前训练验证指标不能可靠选择完整 free-running 最优 checkpoint；
- 当前训练和推理没有接入既定 Branch replay input；
- 当前吞吐不足以替代 v29。

### 13.3 最可能的误差来源

1. **Adapter/router 注入强度或归因位置错误。** Cache 事件本身正确，但 residual 被错误传播或过度调制。
2. **共享主干漂移。** 8k 后 QKVR 与 static encoder 解冻，在没有 per-workload regret 保护的情况下改变了 v29 的原有标定。
3. **Exposure 特征缺乏尺度不变性。** PyTorch 退化随核心数增长，说明某些汇总量或归一化可能把“更多核心”解释成“更强可见停顿”。
4. **Redis 缺少 service/exposure 机制。** LLC miss count 正确，但真实周期仍少预测约 40%；仅有 tag/replacement 状态不足以恢复 MSHR、DRAM 排队、MLP 和 critical-path stall。
5. **离线 validation 与闭环 deployment 不一致。** sequence loss 的改善不能约束某个 workload 在 free-running 中被持续拉偏。

## 14. 下一步实验顺序

### P0：同 checkpoint timing 消融

使用当前 60k checkpoint，不重新训练，对以下负载运行 GSS-on 与 gap0：

```text
PyTorch base/heldout
Marine heldout
memory_seq_moderate
coh_readmostly_sparse
Redis heldout
MySQL heldout
Flink heldout
```

判断规则：

- gap0 明显恢复 v29：主要问题在 adapter/router 在线注入；
- gap0 仍明显差于 v29：主要问题在 60k 联合微调后的共享主干漂移；
- Redis on/gap0 几乎相同：现有 GSS 特征或曝光映射没有提供有效 timing 信号。

### P1：修复正式 Branch 合同

- 合并 branch replay、GSS 与 Exposure sidecar manifest；
- 设置 `branch_mode=replay_event_history`；
- 删除 neural `branch_head`；
- 推理启动器强制校验 branch contract 和 `branch_replay_dir`；
- 报告 Branch PMU 只允许读取 replay event；
- 初始化 canonical v29 时显式丢弃旧 branch head 参数。

只修复 Branch PMU 报告不需要重训；让 replay event/history 进入 timing 主干需要重新训练。

### P2：改进训练保护和验证

- 加入 workload/core-group regret，而不是仅优化 sequence 平均 loss；
- validation 同时报告每个 workload/core 的 signed bias；
- 增加 PyTorch、Marine、Redis 的小规模 free-running checkpoint gate；
- checkpoint 选择要求“不显著退化 v29”，但不恢复昂贵的 frozen-teacher 双前向；
- 优先使用离线周期性 free-running gate，而不是每个训练 step 做 teacher forward。

### P3：补足 Redis service/exposure

在 P0 证明现有 GSS 注入位置合理之后，再考虑加入：

- outstanding miss / MSHR pressure；
- bank/channel pressure；
- recent service latency summary；
- MLP overlap 与 oldest-dependent-miss age；
- load-to-use dependency criticality；
- bandwidth saturation 和 queue occupancy proxy。

这些特征必须保持训练和推理同一构造机制，并进行跨核心数归一化。

### P4：严格 v29 A/B

用当前 canonical runtime、相同 120 条 manifest、相同 serial 配置重跑 v29，以消除旧 runtime 对吞吐和极少量 timing reconstruction 的影响。

## 15. 最终决策

当前证据支持以下决策：

- 保留 v29 作为默认 timing baseline；
- 保留 GSS 状态机作为有价值的机制基础设施；
- 不接受当前 Exposure-v1 60k checkpoint 为正式 v30；
- Branch PMU 正式切换到 configured replay，禁止继续使用 neural `BRerr`；
- 在任何新 60k 训练前，先完成 P0 同 checkpoint 消融；
- Redis 下一步重点从 cache miss count 转向 memory service 与 critical exposure，而不是继续增加静态 tag/replacement 特征。
