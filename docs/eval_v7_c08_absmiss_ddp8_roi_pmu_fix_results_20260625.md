# v7_c08_absmiss_ddp8 roi_pmu_fix 推理实验结果汇总

<!-- dtlb_miss errata: see final answer for the discovered ROI aggregation bug. -->

## 实验说明

- 模型：`ckpt/v7_c08_absmiss_ddp8`
- 结果目录：`logs/eval_parallel_v7_c08_absmiss_ddp8_roi_pmu_fix_20260625_172427`
- 统计口径：
  - 直接统计该目录下 17 个 workload 日志
  - 不再混合主跑 / retry 目录
  - cycle-weighted 指标按各 workload 的 `roi_stats_cycles` 加权
- 额外信息：
  - 本次日志内包含 `timing(avg/window)`，下文一并整理平均每窗口推理耗时
  - PMU 指标中的 `pred_vs_roi` / `label_vs_roi` 只在 ROI 值非 0 时定义

最终共统计 17 个 workload。

## 表 1：整体 CPI 误差汇总

| 指标 | 样本数 | mean | median | max |
|---|---:|---:|---:|---:|
| `pred_vs_roi_cpi_uop` | 17 | 3.96% | 2.30% | 12.62% |
| `pred_vs_label_cpi_uop` | 17 | 4.03% | 3.77% | 12.60% |
| `win_mape_cpi_uop` | 17 | 17.29% | 12.97% | 78.63% |
| `pred_vs_roi_cpi_macro` | 17 | 4.09% | 2.31% | 12.66% |
| `pred_vs_label_cpi_macro` | 17 | 4.03% | 3.77% | 12.60% |
| `label_vs_roi_cpi_uop` | 17 | 1.05% | 0.41% | 7.23% |
| `label_vs_roi_cpi_macro` | 17 | 1.15% | 0.59% | 7.17% |

cycle-weighted 口径：

| 指标 | 加权误差 |
|---|---:|
| weighted `pred_vs_roi_cpi_uop` | 2.93% |
| weighted `label_vs_roi_cpi_uop` | 2.18% |
| weighted `pred_vs_roi_cpi_macro` | 3.00% |
| weighted `label_vs_roi_cpi_macro` | 2.22% |

## 表 2：逐 workload CPI 结果

| Workload | Windows | `pred_vs_roi_cpi_uop` | `pred_vs_roi_cpi_macro` | `win_mape_cpi_uop` |
|---|---:|---:|---:|---:|
| `W_ads_ctr` | 1129 | 4.22% | 4.40% | 17.61% |
| `W_ads_ranking_proxy` | 1451 | 2.26% | 2.25% | 78.63% |
| `W_branch_storm` | 1113 | 0.19% | 0.20% | 11.20% |
| `W_chase_dram` | 1334 | 1.49% | 1.70% | 11.86% |
| `W_compute_int` | 1228 | 0.80% | 0.80% | 2.99% |
| `W_false_sharing` | 1104 | 2.30% | 2.31% | 8.80% |
| `W_feed_ranking` | 834 | 0.48% | 1.90% | 8.76% |
| `W_fp_compute_dense` | 923 | 1.00% | 1.27% | 11.11% |
| `W_fp_lite` | 930 | 5.78% | 5.62% | 18.28% |
| `W_graph_recall_proxy` | 1225 | 5.84% | 6.31% | 20.44% |
| `W_indirect` | 1202 | 6.33% | 6.34% | 8.35% |
| `W_int_div` | 1261 | 12.62% | 12.66% | 12.97% |
| `W_interest_graph_recall` | 824 | 0.66% | 0.45% | 15.64% |
| `W_mlp_light` | 1107 | 7.95% | 7.95% | 9.52% |
| `W_phased_mix` | 1071 | 1.00% | 1.10% | 15.17% |
| `W_search_index_proxy` | 1080 | 8.87% | 8.88% | 24.98% |
| `W_stream` | 1121 | 5.53% | 5.47% | 17.64% |

## 表 3：高误差 workload 的 signed bias

说明：

- `signed_pred_minus_label = (pred - label) / label`
- `signed_label_minus_roi = (label - roi) / roi`
- 正值表示偏高估，负值表示偏低估

| Workload | `pred_cpi_uop` | `label_cpi_uop` | `roi_cpi_uop` | `signed_pred_minus_label` | `signed_label_minus_roi` |
|---|---:|---:|---:|---:|---:|
| `W_int_div` | 0.6234 | 0.7133 | 0.7135 | -12.60% | -0.02% |
| `W_search_index_proxy` | 0.4194 | 0.3859 | 0.3852 | 8.67% | 0.19% |
| `W_mlp_light` | 0.5484 | 0.5080 | 0.5081 | 7.96% | -0.01% |
| `W_graph_recall_proxy` | 0.5262 | 0.4878 | 0.4971 | 7.88% | -1.89% |
| `W_indirect` | 0.8400 | 0.8960 | 0.8968 | -6.25% | -0.09% |
| `W_fp_lite` | 0.7059 | 0.6662 | 0.6673 | 5.95% | -0.16% |
| `W_false_sharing` | 17.0619 | 17.9686 | 17.4634 | -5.05% | 2.89% |
| `W_ads_ctr` | 0.9625 | 1.0127 | 1.0050 | -4.96% | 0.77% |

## 表 4：整体 PMU 误差汇总

说明：

- `pVr` = `pred_vs_roi`
- `pVl` = `pred_vs_label`
- `lVr` = `label_vs_roi`
- `n` 按 `pred_vs_roi` 可定义样本数统计
- 对 `roi=0` 或 `label=0` 导致不可定义的项做了跳过，因此不同 PMU 的样本数不同

| PMU | n | `pVr` mean | `pVr` median | `pVl` mean | `pVl` median | `lVr` mean | `lVr` median |
|---|---:|---:|---:|---:|---:|---:|---:|
| `branch_miss` | 17 | 23.78% | 20.90% | 23.88% | 13.84% | 14.03% | 10.33% |
| `l1d_ld_miss` | 16 | 36.72% | 19.88% | 55.36% | 13.79% | 18.94% | 8.12% |
| `l1d_st_miss` | 15 | 521.20% | 10.95% | 2694.61% | 13.12% | 24.86% | 5.10% |
| `l1i_miss` | 17 | 64.99% | 69.59% | 66.16% | 61.26% | 31.56% | 22.71% |
| `llc_miss` | 14 | 642.59% | 8.85% | 768.62% | 9.83% | 24.03% | 2.17% |
| `dtlb_miss` | 0 | - | - | 120.46% | 6.34% | - | - |
| `mshr_avg` | 17 | 6.24% | 2.54% | 6.60% | 2.39% | 5.42% | 0.66% |

## 表 5：PMU 在 ROI 口径下可定义的 workload 数

| PMU | ROI 可定义样本数 |
|---|---:|
| `branch_miss` | 17 |
| `l1d_ld_miss` | 16 |
| `l1d_st_miss` | 15 |
| `l1i_miss` | 17 |
| `llc_miss` | 14 |
| `dtlb_miss` | 0 |
| `mshr_avg` | 17 |

### `dtlb_miss` 勘误

本次日志中 `dtlb_miss` 的 `roi` 全部为 0，原因不是原始 trace 没有 DTLB 信息，也不是模型没有预测该项，而是 `eval/eval_quota_cycles.py` 的 ROI PMU 聚合逻辑把合法的 `dtlb_hit=0` 误当成缺省值处理了。

- 错误逻辑：`w.get("dtlb_hit", 1) or 1`
- 影响：`dtlb_hit=0` 会被改写成 1，因此 ROI 侧 `dtlb_miss` 永远计不到
- 修复：缺字段时默认 hit，但字段明确为 0 时保留为 miss

因此，本次日志生成的 `dtlb_miss pred_vs_roi` / `label_vs_roi` 不应作为有效结论。下面是不重跑模型、只重新扫描 aligned parquet 中 `dtlb_hit` 后得到的 ROI 参考值：

| Workload | `pred` | `label` | corrected `roi` | corrected `pVr` | corrected `lVr` |
|---|---:|---:|---:|---:|---:|
| `W_ads_ctr` | 344378.5 | 339411.0 | 341183.0 | 0.94% | 0.52% |
| `W_ads_ranking_proxy` | 253412.7 | 230945.0 | 240357.0 | 5.43% | 3.92% |
| `W_branch_storm` | 397.1 | 131.0 | 201.0 | 97.57% | 34.83% |
| `W_chase_dram` | 949847.0 | 898111.0 | 902272.0 | 5.27% | 0.46% |
| `W_compute_int` | 126.5 | 50.0 | 102.0 | 24.07% | 50.98% |
| `W_false_sharing` | 1482753.3 | 1357926.0 | 1487535.0 | 0.32% | 8.71% |
| `W_feed_ranking` | 308629.1 | 304034.0 | 307086.0 | 0.50% | 0.99% |
| `W_fp_compute_dense` | 873685.5 | 825889.0 | 868902.0 | 0.55% | 4.95% |
| `W_fp_lite` | 963187.7 | 887208.0 | 906947.0 | 6.20% | 2.18% |
| `W_graph_recall_proxy` | 209956.0 | 238764.0 | 271873.0 | 22.77% | 12.18% |
| `W_indirect` | 27560.4 | 28908.0 | 30381.0 | 9.28% | 4.85% |
| `W_int_div` | 2096.3 | 123.0 | 124.0 | 1590.58% | 0.81% |
| `W_interest_graph_recall` | 196973.4 | 210301.0 | 214015.0 | 7.96% | 1.74% |
| `W_mlp_light` | 359287.2 | 346615.0 | 352128.0 | 2.03% | 1.57% |
| `W_phased_mix` | 170535.8 | 191222.0 | 195991.0 | 12.99% | 2.43% |
| `W_search_index_proxy` | 245463.9 | 257785.0 | 270948.0 | 9.41% | 4.86% |
| `W_stream` | 872189.4 | 846836.0 | 892892.0 | 2.32% | 5.16% |

### `dtlb_miss` 勘误

本次日志中 `dtlb_miss` 的 `roi` 全部为 0，原因不是原始 trace 没有 DTLB 信息，也不是模型没有预测该项，而是 `eval/eval_quota_cycles.py` 的 ROI PMU 聚合逻辑把合法的 `dtlb_hit=0` 误当成缺省值处理了：

- 错误逻辑：`w.get("dtlb_hit", 1) or 1`
- 影响：`dtlb_hit=0` 会被改写成 1，因此 ROI 侧 `dtlb_miss` 永远计不到
- 修复：缺字段时默认 hit，但字段明确为 0 时保留为 miss

因此，本次日志生成的 `dtlb_miss pred_vs_roi` / `label_vs_roi` 不应作为有效结论。下面是不重跑模型、只重新扫描 aligned parquet 中 `dtlb_hit` 后得到的 ROI 参考值：

| Workload | `pred` | `label` | corrected `roi` | corrected `pVr` | corrected `lVr` |
|---|---:|---:|---:|---:|---:|
| `W_ads_ctr` | 344378.5 | 339411.0 | 341183.0 | 0.94% | 0.52% |
| `W_ads_ranking_proxy` | 253412.7 | 230945.0 | 240357.0 | 5.43% | 3.92% |
| `W_branch_storm` | 397.1 | 131.0 | 201.0 | 97.57% | 34.83% |
| `W_chase_dram` | 949847.0 | 898111.0 | 902272.0 | 5.27% | 0.46% |
| `W_compute_int` | 126.5 | 50.0 | 102.0 | 24.07% | 50.98% |
| `W_false_sharing` | 1482753.3 | 1357926.0 | 1487535.0 | 0.32% | 8.71% |
| `W_feed_ranking` | 308629.1 | 304034.0 | 307086.0 | 0.50% | 0.99% |
| `W_fp_compute_dense` | 873685.5 | 825889.0 | 868902.0 | 0.55% | 4.95% |
| `W_fp_lite` | 963187.7 | 887208.0 | 906947.0 | 6.20% | 2.18% |
| `W_graph_recall_proxy` | 209956.0 | 238764.0 | 271873.0 | 22.77% | 12.18% |
| `W_indirect` | 27560.4 | 28908.0 | 30381.0 | 9.28% | 4.85% |
| `W_int_div` | 2096.3 | 123.0 | 124.0 | 1590.58% | 0.81% |
| `W_interest_graph_recall` | 196973.4 | 210301.0 | 214015.0 | 7.96% | 1.74% |
| `W_mlp_light` | 359287.2 | 346615.0 | 352128.0 | 2.03% | 1.57% |
| `W_phased_mix` | 170535.8 | 191222.0 | 195991.0 | 12.99% | 2.43% |
| `W_search_index_proxy` | 245463.9 | 257785.0 | 270948.0 | 9.41% | 4.86% |
| `W_stream` | 872189.4 | 846836.0 | 892892.0 | 2.32% | 5.16% |

## 表 6：逐 workload 关键 PMU 指标

这里选两个更稳定、且本次 ROI 口径可定义性更好的 PMU：

- `branch_miss pred_vs_roi`
- `mshr_avg pred_vs_roi`

| Workload | `pred_vs_roi_cpi_uop` | `branch_miss pVr` | `mshr_avg pVr` |
|---|---:|---:|---:|
| `W_ads_ctr` | 4.22% | 8.07% | 1.23% |
| `W_ads_ranking_proxy` | 2.26% | 12.70% | 1.01% |
| `W_branch_storm` | 0.19% | 28.58% | 3.56% |
| `W_chase_dram` | 1.49% | 3.56% | 3.70% |
| `W_compute_int` | 0.80% | 6.11% | 4.66% |
| `W_false_sharing` | 2.30% | 38.30% | 3.20% |
| `W_feed_ranking` | 0.48% | 55.06% | 44.54% |
| `W_fp_compute_dense` | 1.00% | 49.35% | 4.44% |
| `W_fp_lite` | 5.78% | 46.30% | 2.22% |
| `W_graph_recall_proxy` | 5.84% | 12.16% | 0.97% |
| `W_indirect` | 6.33% | 3.09% | 0.29% |
| `W_int_div` | 12.62% | 16.41% | 0.79% |
| `W_interest_graph_recall` | 0.66% | 20.90% | 18.40% |
| `W_mlp_light` | 7.95% | 25.02% | 2.54% |
| `W_phased_mix` | 1.00% | 42.40% | 10.67% |
| `W_search_index_proxy` | 8.87% | 6.71% | 1.39% |
| `W_stream` | 5.53% | 29.62% | 2.51% |

## 表 7：逐 workload PMU 误差明细 A（`pred_vs_roi`）

| Workload | `branch_miss` | `l1d_ld_miss` | `l1d_st_miss` |
|---|---:|---:|---:|
| `W_ads_ctr` | 8.07% | 8.34% | 2.58% |
| `W_ads_ranking_proxy` | 12.70% | 4.89% | 3.53% |
| `W_branch_storm` | 28.58% | 43.66% | 6571.86% |
| `W_chase_dram` | 3.56% | 20.24% | 13.01% |
| `W_compute_int` | 6.11% | 15.48% | 574.08% |
| `W_false_sharing` | 38.30% | 13.89% | 3.97% |
| `W_feed_ranking` | 55.06% | 45.96% | 5.61% |
| `W_fp_compute_dense` | 49.35% | 26.89% | 5.54% |
| `W_fp_lite` | 46.30% | 4.24% | 2.02% |
| `W_graph_recall_proxy` | 12.16% | 37.00% | 47.47% |
| `W_indirect` | 3.09% | 56.71% | 487.24% |
| `W_int_div` | 16.41% | 242.36% | - |
| `W_interest_graph_recall` | 20.90% | 2.80% | 6.14% |
| `W_mlp_light` | 25.02% | - | - |
| `W_phased_mix` | 42.40% | 9.51% | 68.28% |
| `W_search_index_proxy` | 6.71% | 36.10% | 15.80% |
| `W_stream` | 29.62% | 19.52% | 10.95% |

## 表 8：逐 workload PMU 误差明细 B（`pred_vs_roi`）

| Workload | `l1i_miss` | `llc_miss` | `dtlb_miss` | `mshr_avg` |
|---|---:|---:|---:|---:|
| `W_ads_ctr` | 63.42% | 1.82% | - | 1.23% |
| `W_ads_ranking_proxy` | 92.36% | 5.77% | - | 1.01% |
| `W_branch_storm` | 80.14% | 3520.51% | - | 3.56% |
| `W_chase_dram` | 60.40% | 5.71% | - | 3.70% |
| `W_compute_int` | 43.78% | 351.91% | - | 4.66% |
| `W_false_sharing` | 17.12% | 4246.92% | - | 3.20% |
| `W_feed_ranking` | 63.17% | 10.17% | - | 44.54% |
| `W_fp_compute_dense` | 69.59% | 9.16% | - | 4.44% |
| `W_fp_lite` | 70.21% | 8.54% | - | 2.22% |
| `W_graph_recall_proxy` | 81.40% | 807.55% | - | 0.97% |
| `W_indirect` | 85.31% | 15.90% | - | 0.29% |
| `W_int_div` | 74.27% | - | - | 0.79% |
| `W_interest_graph_recall` | 55.48% | 3.62% | - | 18.40% |
| `W_mlp_light` | 41.19% | - | - | 2.54% |
| `W_phased_mix` | 72.85% | 4.08% | - | 10.67% |
| `W_search_index_proxy` | 71.13% | - | - | 1.39% |
| `W_stream` | 62.97% | 4.64% | - | 2.51% |

## 表 9：平均每窗口推理时延汇总

| 阶段 | simple mean | window-weighted mean |
|---|---:|---:|
| `build` | 3.8 ms | 3.8 ms |
| `encode` | 51.8 ms | 51.7 ms |
| `tensor` | 1.7 ms | 1.7 ms |
| `forward` | 1208.4 ms | 1207.9 ms |
| `update` | 5.0 ms | 5.0 ms |
| `total` | 1271.2 ms | 1270.6 ms |

## 表 10：逐 workload 平均每窗口推理时延

| Workload | Windows | `build` | `encode` | `tensor` | `forward` | `update` | `total` |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W_search_index_proxy` | 1080 | 4.1 ms | 55.6 ms | 1.9 ms | 1264.5 ms | 5.3 ms | 1332.0 ms |
| `W_feed_ranking` | 834 | 3.8 ms | 55.2 ms | 2.1 ms | 1263.8 ms | 4.9 ms | 1330.7 ms |
| `W_ads_ranking_proxy` | 1451 | 3.9 ms | 55.4 ms | 1.8 ms | 1260.6 ms | 5.0 ms | 1327.4 ms |
| `W_fp_compute_dense` | 923 | 3.6 ms | 52.9 ms | 1.7 ms | 1239.6 ms | 5.3 ms | 1303.6 ms |
| `W_ads_ctr` | 1129 | 3.7 ms | 53.7 ms | 1.9 ms | 1219.4 ms | 4.9 ms | 1284.4 ms |
| `W_phased_mix` | 1071 | 3.6 ms | 52.3 ms | 1.9 ms | 1219.0 ms | 4.8 ms | 1282.3 ms |
| `W_graph_recall_proxy` | 1225 | 3.5 ms | 52.6 ms | 1.8 ms | 1210.7 ms | 4.8 ms | 1274.1 ms |
| `W_stream` | 1121 | 3.4 ms | 52.1 ms | 1.7 ms | 1194.6 ms | 4.9 ms | 1257.2 ms |
| `W_fp_lite` | 930 | 3.6 ms | 52.2 ms | 1.7 ms | 1190.7 ms | 5.5 ms | 1254.2 ms |
| `W_interest_graph_recall` | 824 | 3.6 ms | 53.2 ms | 2.0 ms | 1189.5 ms | 4.6 ms | 1253.8 ms |
| `W_chase_dram` | 1334 | 3.5 ms | 52.7 ms | 1.7 ms | 1188.1 ms | 4.8 ms | 1251.4 ms |
| `W_compute_int` | 1228 | 3.8 ms | 48.2 ms | 1.5 ms | 1188.6 ms | 5.0 ms | 1247.3 ms |
| `W_mlp_light` | 1107 | 3.9 ms | 50.0 ms | 1.6 ms | 1186.0 ms | 5.3 ms | 1246.9 ms |
| `W_int_div` | 1261 | 4.2 ms | 48.5 ms | 1.6 ms | 1184.4 ms | 4.7 ms | 1243.5 ms |
| `W_branch_storm` | 1113 | 4.1 ms | 48.1 ms | 1.5 ms | 1182.6 ms | 4.9 ms | 1241.4 ms |
| `W_indirect` | 1202 | 4.0 ms | 48.4 ms | 1.5 ms | 1181.5 ms | 5.0 ms | 1240.6 ms |
| `W_false_sharing` | 1104 | 4.1 ms | 49.5 ms | 1.5 ms | 1179.0 ms | 5.2 ms | 1239.5 ms |

## 结果解读

1. CPI 主指标整体仍然稳定。
   - 17 个 workload 合并后，`pred_vs_roi_cpi_uop` mean 3.96%，median 2.30%，max 12.62%
   - `label_vs_roi_cpi_uop` mean 1.05%，仍显著低于预测误差，说明主误差来源依旧在模型预测侧

2. 高 CPI 误差 workload 集中区没有变。
   - Top 5 仍是 `W_int_div`、`W_search_index_proxy`、`W_mlp_light`、`W_indirect`、`W_graph_recall_proxy` / `W_fp_lite`
   - signed bias 看，`W_int_div`、`W_indirect` 仍偏低估，`W_search_index_proxy`、`W_mlp_light`、`W_fp_lite` 仍偏高估

3. workload 级误差和窗口级误差仍然可能明显分离。
   - `W_ads_ranking_proxy` 的 workload CPI 误差仅 2.26%，但 `win_mape_cpi_uop` 达到 78.63%
   - 这说明窗口级波动很大，但聚合后有较强抵消

4. PMU 结果相比上一版汇总更稳定；`dtlb_miss` 需要按上面的勘误看。
   - 对比 `docs/eval_v7_c08_absmiss_ddp8_results_20260625.md`，`branch_miss pVr mean` 从 120.47% 降到 23.78%，`l1d_ld_miss pVr mean` 从 98.35% 降到 36.72%，`mshr_avg pVr mean` 从 9.15% 降到 6.24%
   - 本次日志中 `dtlb_miss pred_vs_roi` 在 17 个 workload 上全部不可定义，是 eval 聚合 bug 导致 `roi=0`，不是有效实验结论

5. 推理耗时由 `forward` 主导。
   - window-weighted 平均每窗口总耗时 1270.6 ms，其中 `forward` 1207.9 ms，占绝对大头
   - `encode` 次之，为 51.7 ms；`build` / `tensor` / `update` 都明显更小
   - 单窗口总耗时最高的是 `W_search_index_proxy`，达到 1332.0 ms

## `dtlb_miss` 勘误

本次日志中 `dtlb_miss` 的 `roi` 全部为 0，原因不是原始 trace 没有 DTLB 信息，也不是模型没有预测该项，而是 `eval/eval_quota_cycles.py` 的 ROI PMU 聚合逻辑把合法的 `dtlb_hit=0` 误当成缺省值处理了。

- 错误逻辑：`w.get("dtlb_hit", 1) or 1`
- 影响：`dtlb_hit=0` 会被改写成 1，因此 ROI 侧 `dtlb_miss` 永远计不到
- 修复：缺字段时默认 hit，但字段明确为 0 时保留为 miss

因此，本次日志生成的 `dtlb_miss pred_vs_roi` / `label_vs_roi` 不应作为有效结论。下面是不重跑模型、只重新扫描 aligned parquet 中 `dtlb_hit` 后得到的 ROI 参考值：

| Workload | `pred` | `label` | corrected `roi` | corrected `pVr` | corrected `lVr` |
|---|---:|---:|---:|---:|---:|
| `W_ads_ctr` | 344378.5 | 339411.0 | 341183.0 | 0.94% | 0.52% |
| `W_ads_ranking_proxy` | 253412.7 | 230945.0 | 240357.0 | 5.43% | 3.92% |
| `W_branch_storm` | 397.1 | 131.0 | 201.0 | 97.57% | 34.83% |
| `W_chase_dram` | 949847.0 | 898111.0 | 902272.0 | 5.27% | 0.46% |
| `W_compute_int` | 126.5 | 50.0 | 102.0 | 24.07% | 50.98% |
| `W_false_sharing` | 1482753.3 | 1357926.0 | 1487535.0 | 0.32% | 8.71% |
| `W_feed_ranking` | 308629.1 | 304034.0 | 307086.0 | 0.50% | 0.99% |
| `W_fp_compute_dense` | 873685.5 | 825889.0 | 868902.0 | 0.55% | 4.95% |
| `W_fp_lite` | 963187.7 | 887208.0 | 906947.0 | 6.20% | 2.18% |
| `W_graph_recall_proxy` | 209956.0 | 238764.0 | 271873.0 | 22.77% | 12.18% |
| `W_indirect` | 27560.4 | 28908.0 | 30381.0 | 9.28% | 4.85% |
| `W_int_div` | 2096.3 | 123.0 | 124.0 | 1590.58% | 0.81% |
| `W_interest_graph_recall` | 196973.4 | 210301.0 | 214015.0 | 7.96% | 1.74% |
| `W_mlp_light` | 359287.2 | 346615.0 | 352128.0 | 2.03% | 1.57% |
| `W_phased_mix` | 170535.8 | 191222.0 | 195991.0 | 12.99% | 2.43% |
| `W_search_index_proxy` | 245463.9 | 257785.0 | 270948.0 | 9.41% | 4.86% |
| `W_stream` | 872189.4 | 846836.0 | 892892.0 | 2.32% | 5.16% |
