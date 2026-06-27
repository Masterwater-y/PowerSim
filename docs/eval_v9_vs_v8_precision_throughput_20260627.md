# v9 vs v8 CPI/PMU 精度与推理吞吐对比

## 结论

当前 v9 在推理吞吐大幅上升的同时，**CPI 精度没有下降，反而明显改善**。

同一组 seedB c08 infer17 / 17 workloads 上：

- CPI mean pVr：v8 `9.66%` -> v9 `5.53%`
- CPI median pVr：v8 `3.84%` -> v9 `3.14%`
- CPI max pVr：v8 `62.56%` -> v9 `29.88%`
- 全局 signed cycle error：v8 `35.47%` -> v9 `16.92%`
- 推理循环吞吐：v8 `3.88K uops/s` -> v9 `25.78K uops/s`，约 `6.64x`
- 推理循环 macro 吞吐：v8 `2.13K macro/s` -> v9 `14.17K macro/s`，约 `6.64x`

PMU count 头的结论更谨慎：v9 的 `branch_miss`、`l1d_ld_miss`、`dtlb_miss` 明显改善；`l1d_st_miss` 和 `llc_miss` 的 median 改善，但 mean/max 被极小真值分母 outlier 放大，暂时仍不适合作为部署主 KPI。

## 对比口径

主对比使用同一组 seedB c08 infer17 raw trace：

| 项 | v8 | v9 |
|---|---|---|
| 模型 | `ckpt/v8_c01_c04_tq_ddp8_s4500` | `ckpt/v9_tq_train600_8gpu_4000_resume1840_fastskip` |
| 结果目录 | `logs/eval_parallel_v8_c01_c04_tq_ddp8_s4500_seedB_infer17_activecore_20260626_142203` | `logs/eval_parallel_v9_final_c08_seedB_full_20260627_213543` |
| workload 数 | 17 | 17 |
| ROI uops | 100,728,721 | 100,728,721 |
| ROI macros | 55,359,280 | 55,359,280 |
| 标签对齐 | `label_vs_roi_cpi_uop = 0` | `label_vs_roi_cpi_uop = 0` |

补充参考使用 v8 seedC c06 结果：`logs/eval_parallel_v8_c01_c04_tq_ddp8_s4500_seedC_c06_infer17_20260626_154419`。该组不是 v9 的直接 A/B，因为 v9 c06 full eval 还没有跑。

注意：v9 不是只改了吞吐实现。它同时引入了 composite uop embedding、side tensor、L2 PMU label、mshr/i-side 删除、c08 训练覆盖、以及新的 min-uop tail-aligned 切窗。因此下面结论是“当前 v9 系统 vs v8 系统”的对比，不能单独归因于 uop 压缩。

## CPI 精度

### 汇总

| 指标 | v8 c08 seedB | v9 c08 seedB | 变化 |
|---|---:|---:|---:|
| workloads | 17 | 17 | 0 |
| total windows | 20,356 | 44,656 | +119.4% |
| mean CPI pVr | 9.66% | 5.53% | -4.13 pp |
| median CPI pVr | 3.84% | 3.14% | -0.70 pp |
| max CPI pVr | 62.56% | 29.88% | -32.68 pp |
| mean window MAPE | 18.00% | 16.10% | -1.90 pp |
| median window MAPE | 13.24% | 11.59% | -1.65 pp |
| global signed cycle error | 35.47% | 16.92% | -18.55 pp |

v9 的窗口数翻倍，是因为新切窗每窗证据量更小、更稳定；但总 ROI uops/macros 与 v8 完全一致，所以 CPI 误差是同一批 trace 的可比结果。

### 逐 workload

| Workload | v8 pVr | v9 pVr | 变化 |
|---|---:|---:|---:|
| `W_ads_ctr` | 0.90% | 3.27% | +2.37 pp |
| `W_ads_ranking_proxy` | 40.92% | 23.09% | -17.83 pp |
| `W_branch_storm` | 5.13% | 2.13% | -3.00 pp |
| `W_chase_dram` | 2.12% | 2.08% | -0.04 pp |
| `W_compute_int` | 3.84% | 0.14% | -3.70 pp |
| `W_false_sharing` | 62.56% | 29.88% | -32.68 pp |
| `W_feed_ranking` | 2.08% | 1.91% | -0.17 pp |
| `W_fp_compute_dense` | 5.61% | 4.62% | -0.99 pp |
| `W_fp_lite` | 0.49% | 3.14% | +2.65 pp |
| `W_graph_recall_proxy` | 2.42% | 4.26% | +1.84 pp |
| `W_indirect` | 5.11% | 5.95% | +0.84 pp |
| `W_int_div` | 1.02% | 1.05% | +0.03 pp |
| `W_interest_graph_recall` | 1.90% | 0.95% | -0.95 pp |
| `W_mlp_light` | 4.48% | 1.06% | -3.42 pp |
| `W_phased_mix` | 11.73% | 3.17% | -8.56 pp |
| `W_search_index_proxy` | 10.52% | 5.87% | -4.65 pp |
| `W_stream` | 3.43% | 1.36% | -2.07 pp |

结论：v9 有少数轻微退化项，但主要 outlier 显著改善。尤其 `W_false_sharing` 从 `62.56%` 降到 `29.88%`，`W_ads_ranking_proxy` 从 `40.92%` 降到 `23.09%`。这两个仍是当前主要误差来源，但已经不再像 v8 那样主导到 35% 以上的全局周期误差。

## PMU 精度

PMU schema 有变化：

- v8 有 `l1i_miss`、`mshr_avg`
- v9 删除 `l1i_miss`、`mshr_avg`，新增 `l2_ld_miss`、`l2_st_miss`

因此严格可比的是公共 PMU：`cpi_uop`、`branch_miss`、`l1d_ld_miss`、`l1d_st_miss`、`llc_miss`、`dtlb_miss`。

| PMU | v8 mean | v8 median | v8 max | v9 mean | v9 median | v9 max | 结论 |
|---|---:|---:|---:|---:|---:|---:|---|
| `cpi_uop` | 9.66% | 3.84% | 62.56% | 5.53% | 3.14% | 29.88% | 改善 |
| `branch_miss` | 498.19% | 81.72% | 7157.36% | 54.89% | 30.19% | 317.65% | 明显改善 |
| `l1d_ld_miss` | 398.95% | 77.05% | 3150.89% | 28.73% | 25.87% | 54.39% | 明显改善 |
| `l1d_st_miss` | 499.80% | 107.92% | 3057.09% | 1816.00% | 19.37% | 13410.57% | median 改善，mean/max 被 outlier 放大 |
| `llc_miss` | 1551.83% | 107.62% | 10170.20% | 3024.12% | 11.78% | 44721.70% | median 改善，mean/max 被 outlier 放大 |
| `dtlb_miss` | 207.98% | 101.02% | 1624.27% | 78.23% | 18.28% | 766.31% | 改善 |

v9 新增 L2 指标当前表现：

| PMU | mean pVr | median pVr | max pVr |
|---|---:|---:|---:|
| `l2_ld_miss` | 51.77% | 40.76% | 253.15% |
| `l2_st_miss` | 1501.17% | 23.44% | 11486.04% |

解释：

- `l1d_st_miss`、`l2_st_miss`、`llc_miss` 的 mean/max 很差，主要来自低计数或接近 0 的真值分母。例如 `W_false_sharing` 的 `llc_miss` label 只有 7，但预测为 3137，单项相对误差会被放大到 44721%。
- 从 median 看，v9 的大多数 PMU 分布并没有整体退化；但 count 头仍明显不稳定。
- 当前 PMU 更适合作 auxiliary/diagnostic，不适合作部署主指标。部署主 KPI 仍应看 `cpi_uop` 和聚合 cycle error。

## 推理吞吐

吞吐有两个口径：

- 推理循环口径：`sum_uops / Σ(windows × final timing(avg/window).total)`，基本排除模型加载和任务调度。
- 整批 end-to-end 口径：从 logdir 时间戳到最后日志写完，包含模型加载、并行调度和 straggler。

| 指标 | v8 c08 seedB | v9 c08 seedB | speedup |
|---|---:|---:|---:|
| total windows | 20,356 | 44,656 | 2.19x |
| mean total ms/window | 1275.6 ms | 87.9 ms | 14.51x faster |
| mean forward ms/window | 1200.8 ms | 62.5 ms | 19.21x faster |
| mean encode ms/window | 63.1 ms | 19.9 ms | 3.17x faster |
| loop uops/s | 3,883 | 25,775 | 6.64x |
| loop macro/s | 2,134 | 14,166 | 6.64x |
| batch wall uops/s | 23,843 | 108,535 | 4.55x |
| batch wall macro/s | 13,104 | 59,649 | 4.55x |

补充参考：v8 seedC c06 的推理循环吞吐为 `3,904 uops/s`、`2,147 macro/s`，与 v8 c08 seedB 基本一致，说明 v8 的瓶颈主要是每窗 transformer forward，而不是具体 core 数。

吞吐大幅上升的直接原因：

1. v8 每个 uop 约 6 个 token；v9 composite uop 每个 uop 只占 1 个 transformer position。
2. v9 每窗 total latency 从约 `1.28s` 降到 `88ms`。
3. 即使 v9 窗口数从 20,356 增加到 44,656，总推理循环时间仍从约 `25,941s` 降到 `3,908s`。

## 是否因为吞吐提升牺牲了精度

当前证据不支持“吞吐提升导致精度下降”。

更准确的说法是：

1. CPI 主指标没有下降，而是改善。
   - mean pVr 降低 4.13 pp
   - max pVr 降低 32.68 pp
   - 全局 signed cycle error 降低 18.55 pp

2. v9 的 PMU count 头并非全面改善。
   - `branch_miss`、`l1d_ld_miss`、`dtlb_miss` 改善明显。
   - store miss 和 LLC miss 的 mean/max 仍很差，且部分 outlier 比 v8 更大。
   - 但这些 PMU 的 median 反而明显改善，说明主要问题是稀疏计数 outlier，而不是所有窗口都变差。

3. 不能把改善全部归因于 uop 压缩。
   - v9 同时加入 c08 训练覆盖、跨核 side features、L2 标签、新切窗机制。
   - 因此结论应表述为：当前 v9 方案在大幅提升吞吐的同时，CPI 精度也提升；PMU count 头仍需继续校准。

## 当前风险和下一步

1. `W_false_sharing` 仍然低估：
   - v8: `62.56%`
   - v9: `29.88%`
   - 已改善，但仍是最大 CPI outlier。

2. `W_ads_ranking_proxy` 仍然低估：
   - v8: `40.92%`
   - v9: `23.09%`
   - 说明 proxy 类负载的并发/缓存压力仍未完全学到。

3. 需要补 v9 c06 seedC full eval。
   - 目前 v9 只有 c08 seedB full eval。
   - v8 c06 可作参考，但不能替代 v9 c06 泛化验证。

4. PMU count 头需要重新定义验收标准。
   - 建议主验收仍使用 CPI/cycle。
   - PMU 对比使用 global count 的同时，应加入绝对误差、非零样本过滤、低分母保护，避免被 0/个位数 label 放大。

