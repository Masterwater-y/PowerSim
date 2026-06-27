# v9 c04/c06 核心数泛化推理结果分析

日期：2026-06-27

## 结论摘要

当前 `v9_tq_train600_8gpu_4000_resume1840_fastskip` 在 4 核 seedB 和 6 核 seedC full eval 上整体可用，CPI 主指标没有出现核心数泛化崩溃。

核心结果：

- c04 seedB：mean pVr `4.06%`，median `2.88%`，max `17.56%`
- c06 seedC：mean pVr `4.66%`，median `1.20%`，max `21.13%`
- c08 seedB 参考：mean pVr `5.53%`，median `3.14%`，max `29.88%`
- c06 相比历史 v8 c06：mean pVr `7.82% -> 4.66%`，max pVr `52.16% -> 21.13%`
- 推理循环吞吐保持在 `21K-26K uops/s`，没有因为 c04/c06 验证出现吞吐回退

主要问题仍然集中在两个 workload：

- `W_false_sharing`：模型能学到 core count 增加会放大 CPI，但放大量不足，c04/c06/c08 都低估。
- `W_ads_ranking_proxy`：c04 很准，但 c06/c08 明显低估，说明并发核数带来的额外内存/排队压力仍没有充分建模。

PMU 结论没有变化：`cpi_uop` 可作为主指标；PMU count 头只能做辅助诊断，不适合作当前部署 KPI。

## 实验输入

| 项 | c04 | c06 | c08 参考 |
|---|---|---|---|
| checkpoint | `ckpt/v9_tq_train600_8gpu_4000_resume1840_fastskip` | 同左 | 同左 |
| raw trace | `data/raw_trace_pool/activecore_eval/c04_seedB_infer17` | `data/raw_trace_pool/activecore_eval/c06_seedC_infer17` | `data/raw_trace_pool/activecore_eval/c08_seedB_infer17` |
| stdout | `logs/eval_v9_final_c04_seedB_full.out` | `logs/eval_v9_final_c06_seedC_full.out` | `logs/eval_v9_final_c08_seedB_full.out` |
| logdir | `logs/eval_parallel_v9_final_c04_seedB_full_20260627_232440` | `logs/eval_parallel_v9_final_c06_seedC_full_20260627_233817` | `logs/eval_parallel_v9_final_c08_seedB_full_20260627_213543` |
| workload 数 | 17 | 17 | 17 |
| `MAX_WINDOWS` | 0 full eval | 0 full eval | 0 full eval |

注意：c04/c08 是 seedB，c06 是 seedC。三组 workload 列表一致，但 c06 不是同 seed 的严格逐 trace A/B；它适合看核心数泛化趋势，不适合解释所有 workload 的逐点差异。

## 完整性

c04 seedB raw trace 已重新采集并验证：

- 17/17 workload 完成。
- 每个 workload 都是 4 核。
- 每核 records 均在 `500k-1000k` 接受范围。
- `collect_v7_c08_parallel.sh verify` 结果为 `all workloads passed`。

c04/c06 eval 日志完整性：

- 未发现 `Traceback` / `ERROR` / `Exception` / `Killed` / `OOM`。
- 17/17 workload 都写出 `Summary`。
- c04/c06/c08 的 `label_vs_roi_cpi_uop` 均为 0，说明窗口标签聚合和 ROI stats 对齐正常。
- PMU ROI coverage 均为 100%。

## CPI 汇总

| 指标 | c04 seedB | c06 seedC | c08 seedB |
|---|---:|---:|---:|
| total windows | 49,080 | 45,272 | 44,656 |
| mean pVr | 4.06% | 4.66% | 5.53% |
| median pVr | 2.88% | 1.20% | 3.14% |
| max pVr | 17.56% | 21.13% | 29.88% |
| mean per-window MAPE | 15.52% | 16.12% | 16.10% |
| median per-window MAPE | 12.06% | 13.00% | 11.59% |
| cycle signed error | -6.32% | -9.87% | -16.92% |

分布看起来比 mean 更健康：

| 阈值 | c04 seedB | c06 seedC | c08 seedB |
|---|---:|---:|---:|
| pVr <= 5% | 12 / 17 | 11 / 17 | 13 / 17 |
| pVr <= 10% | 16 / 17 | 15 / 17 | 15 / 17 |
| pVr > 15% | 1 / 17 | 2 / 17 | 2 / 17 |

解释：

- c04 的 worst case 只有 `W_false_sharing`。
- c06/c08 的 worst cases 是 `W_false_sharing` 和 `W_ads_ranking_proxy`。
- 大多数普通 workload 在 4/6/8 核下都保持在 10% 以内。
- cycle signed error 比 mean pVr 更差，因为 `W_false_sharing` 周期占比高，且模型系统性低估它。

## Top CPI 误差

### c04 seedB

| workload | windows | pred | ROI | pVr | win MAPE |
|---|---:|---:|---:|---:|---:|
| `W_false_sharing` | 2,668 | 5.9591 | 7.2284 | 17.56% | 47.28% |
| `W_indirect` | 2,961 | 0.9631 | 0.8965 | 7.43% | 10.83% |
| `W_graph_recall_proxy` | 2,513 | 0.5201 | 0.4849 | 7.26% | 19.66% |
| `W_search_index_proxy` | 2,645 | 0.4332 | 0.4041 | 7.19% | 35.10% |
| `W_fp_compute_dense` | 2,260 | 0.5868 | 0.6228 | 5.78% | 8.57% |

### c06 seedC

| workload | windows | pred | ROI | pVr | win MAPE |
|---|---:|---:|---:|---:|---:|
| `W_false_sharing` | 2,650 | 9.9043 | 12.5582 | 21.13% | 28.10% |
| `W_ads_ranking_proxy` | 3,225 | 0.4402 | 0.5371 | 18.05% | 32.84% |
| `W_search_index_proxy` | 2,643 | 0.4245 | 0.3890 | 9.10% | 44.89% |
| `W_graph_recall_proxy` | 2,749 | 0.5211 | 0.4865 | 7.11% | 22.01% |
| `W_indirect` | 2,937 | 0.9534 | 0.8941 | 6.64% | 10.43% |

### c08 seedB 参考

| workload | windows | pred | ROI | pVr | win MAPE |
|---|---:|---:|---:|---:|---:|
| `W_false_sharing` | 2,642 | 11.4392 | 16.3142 | 29.88% | 29.75% |
| `W_ads_ranking_proxy` | 2,773 | 0.4654 | 0.6052 | 23.09% | 37.88% |
| `W_indirect` | 2,938 | 0.9499 | 0.8966 | 5.95% | 9.95% |
| `W_search_index_proxy` | 2,460 | 0.4153 | 0.3922 | 5.87% | 43.10% |
| `W_fp_compute_dense` | 2,205 | 0.6235 | 0.6537 | 4.62% | 10.14% |

## 核心数缩放分析

### `W_false_sharing`

| 核数 | pred CPI | ROI CPI | pVr |
|---:|---:|---:|---:|
| 4 | 5.9591 | 7.2284 | 17.56% |
| 6 | 9.9043 | 12.5582 | 21.13% |
| 8 | 11.4392 | 16.3142 | 29.88% |

缩放比例：

| 区间 | ROI CPI scale | pred CPI scale |
|---|---:|---:|
| c04 -> c06 | 1.74x | 1.66x |
| c04 -> c08 | 2.26x | 1.92x |
| c06 -> c08 | 1.30x | 1.15x |

判断：模型确实学到了 false sharing 随核心数上升而变慢，但高核数放大被压扁。c08 最大误差来自这里，且它在总周期中权重最高。

cycle 贡献：

| 核数 | pred cycles | label cycles | signed error |
|---:|---:|---:|---:|
| 4 | 17.99M | 21.82M | -17.56% |
| 6 | 44.10M | 55.91M | -21.13% |
| 8 | 67.93M | 96.88M | -29.88% |

这说明当前 side features/训练分布已经能给出方向，但对 ownership bouncing 的非线性放大仍不足。

### `W_ads_ranking_proxy`

| 核数 | pred CPI | ROI CPI | pVr |
|---:|---:|---:|---:|
| 4 | 0.4105 | 0.4270 | 3.86% |
| 6 | 0.4402 | 0.5371 | 18.05% |
| 8 | 0.4654 | 0.6052 | 23.09% |

缩放比例：

| 区间 | ROI CPI scale | pred CPI scale |
|---|---:|---:|
| c04 -> c06 | 1.26x | 1.07x |
| c04 -> c08 | 1.42x | 1.13x |

判断：c04 已经很准，但 c06/c08 明显低估。这不是基础功能特征不够的问题，而是模型没有充分把 active core 增加映射到 ranking proxy 的额外内存/排队/共享系统压力。它不像 `W_false_sharing` 那样 CPI 绝对值极高，但在核心数泛化上是第二个主要缺陷。

### 稳定 workload 对照

| workload | c04 pVr | c06 pVr | c08 pVr | 观察 |
|---|---:|---:|---:|---|
| `W_chase_dram` | 3.94% | 0.50% | 2.08% | DRAM 型负载泛化稳定 |
| `W_stream` | 2.69% | 2.21% | 1.36% | 带宽/流式访问泛化稳定 |
| `W_compute_int` | 2.88% | 0.89% | 0.14% | 计算型负载非常稳定 |
| `W_fp_lite` | 1.92% | 1.14% | 3.14% | FP 轻量负载稳定 |
| `W_int_div` | 0.66% | 1.15% | 1.05% | divider 型负载稳定 |

这说明 v9 的核心数泛化问题不是全局性的，而是集中在“跨核共享/排队放大”的 workload。

## PMU 结果

PMU mean pVr：

| PMU | c04 mean | c06 mean | c08 mean |
|---|---:|---:|---:|
| `branch_miss` | 50.18% | 50.58% | 54.89% |
| `cpi_uop` | 4.06% | 4.66% | 5.53% |
| `dtlb_miss` | 150.27% | 66.90% | 78.23% |
| `l1d_ld_miss` | 82.55% | 31.45% | 28.73% |
| `l1d_st_miss` | 1530.65% | 2543.06% | 1816.00% |
| `l2_ld_miss` | 98.68% | 253.79% | 51.77% |
| `l2_st_miss` | 1140.40% | 2022.96% | 1501.17% |
| `llc_miss` | 3952.88% | 3400.97% | 3024.12% |

PMU median pVr：

| PMU | c04 median | c06 median | c08 median |
|---|---:|---:|---:|
| `branch_miss` | 26.45% | 28.37% | 30.19% |
| `dtlb_miss` | 18.05% | 17.60% | 18.28% |
| `l1d_ld_miss` | 33.41% | 35.17% | 25.87% |
| `l1d_st_miss` | 18.94% | 20.80% | 19.37% |
| `l2_ld_miss` | 37.87% | 44.63% | 40.76% |
| `l2_st_miss` | 25.11% | 19.09% | 23.44% |
| `llc_miss` | 9.86% | 20.92% | 11.78% |

解释：

- `cpi_uop` 作为 PMU 标签本身表现稳定。
- count 类 PMU 的 mean 容易被小分母放大，尤其 `l1d_st_miss`、`l2_st_miss`、`llc_miss`。
- median 看起来没有 mean 那么差，但仍不足以作为主 KPI。
- `llc_miss` 的极端误差典型来自真值接近 0。例如 `W_false_sharing` 的 LLC miss label 只有个位数，但预测为数千，相对误差会被放大到数万百分比。

因此当前 PMU 结论仍是：保留 PMU count 作为辅助诊断和多任务 regularizer，但验收主指标应使用 `cpi_uop`、cycle signed error、以及少数关键 workload 的 targeted error。

## 推理吞吐

口径：

- loop throughput：`sum_uops / Σ(windows × final timing(avg/window).total)`，排除模型加载和调度。
- batch wall throughput：`sum_uops / wall_time`，包含 8 卡并行调度、模型加载和 straggler。

| 指标 | c04 seedB | c06 seedC | c08 seedB |
|---|---:|---:|---:|
| total windows | 49,080 | 45,272 | 44,656 |
| sum uops | 53.47M | 75.31M | 100.73M |
| sum macro | 29.50M | 41.41M | 55.36M |
| mean total ms/window | 52.0 ms | 66.8 ms | 87.9 ms |
| mean forward ms/window | 37.0 ms | 49.1 ms | 62.5 ms |
| mean encode ms/window | 11.0 ms | 13.6 ms | 19.9 ms |
| loop uops/s | 21,122 | 24,977 | 25,775 |
| loop macro/s | 11,653 | 13,734 | 14,166 |
| batch wall uops/s | 72,259 | 108,201 | 107,731 |
| batch wall macro/s | 39,865 | 59,495 | 59,208 |

观察：

- 随核心数增加，单窗长度和 forward 成本增加，mean total ms/window 从 c04 `52.0ms` 增至 c08 `87.9ms`。
- loop uops/s 没有随核心数下降，原因是每窗包含的 uops 也随核心数增加。
- c04 的 batch wall throughput 低于 c06/c08，主要因为总 uops 少、固定开销和 straggler 占比更高，不代表模型单窗效率更差。
- 与历史 v8 c06 的 `3,904 uops/s` 相比，v9 c06 loop throughput 为 `24,977 uops/s`，约 `6.4x`。

## 与历史 v8 c06 对比

历史 v8 c06：`docs/eval_v8_c01_c04_tq_ddp8_s4500_seedC_c06_infer17_results_20260626.md`

| 指标 | v8 c06 seedC | v9 c06 seedC | 变化 |
|---|---:|---:|---:|
| mean pVr | 7.82% | 4.66% | -3.16 pp |
| median pVr | 2.61% | 1.20% | -1.41 pp |
| max pVr | 52.16% | 21.13% | -31.03 pp |
| cycle signed error | 25.38% | -9.87% | 绝对值下降 15.51 pp |
| loop uops/s | 3,904 | 24,977 | 6.40x |
| loop macro/s | 2,147 | 13,734 | 6.40x |

判断：v9 在 c06 上不是只提升吞吐，CPI 精度也明显改善。主要残留问题从 v8 的“高核数外推大面积偏低”收敛为 v9 的“少数共享/排队 workload 缩放不足”。

## 风险和下一步

1. 针对 `W_false_sharing` 做缩放校准。
   - 当前模型学到方向，但 c06/c08 放大不足。
   - 需要让 side tensor 或 global feature 更直接表达 ownership bouncing 的强度，并在 loss 上提高该类高周期窗口权重。

2. 针对 `W_ads_ranking_proxy` 补并发压力特征或训练覆盖。
   - c04 准，c06/c08 低估，说明单核 functional summary 不够，模型需要更强的 active-core pressure / shared-system proxy。
   - 可以优先检查它的 side feature 随 core count 的变化是否足够单调。

3. CPI 验收建议同时看 mean pVr 和 cycle signed error。
   - c04/c06 mean pVr 都很好，但 cycle signed error 仍为负，说明高周期 workload 的低估会被 simple mean 掩盖。

4. PMU count 头暂不作为主 KPI。
   - 对低计数 PMU 应改用 log1p/count-rate/absolute-error 辅助指标，或者按 label magnitude 做 mask/weight。
   - 当前相对误差表适合定位异常，不适合直接判定部署质量。

5. 后续需要补一组同 seed 的 c04/c06/c08。
   - 现在 c06 是 seedC，c04/c08 是 seedB。
   - 若要严格验证核心数曲线，最好补齐 `c04_seedB`、`c06_seedB`、`c08_seedB` 或者统一使用 seedC。
