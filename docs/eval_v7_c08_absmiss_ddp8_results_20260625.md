# v7_c08_absmiss_ddp8 推理实验结果汇总

## 实验说明

- 模型：`ckpt/v7_c08_absmiss_ddp8`
- 主结果目录：`logs/eval_parallel_v7_c08_absmiss_ddp8_20260625_114231`
- 重跑结果目录：`logs/eval_parallel_v7_c08_absmiss_ddp8_retry2_no_macro_align_20260625_134610`
- 合并口径：
  - 默认采用主结果目录中的 15 个成功 workload
  - `W_graph_recall_proxy`、`W_phased_mix` 采用 retry2 结果覆盖主结果中的失败项
- retry2 变更：
  - eval 切窗改为默认**不补齐 macro 边界**
  - 仅重跑之前因上下文超长失败的 2 个 workload

最终合并后共有 17 个 workload。

## 表 1：整体 CPI 误差汇总

| 指标 | 样本数 | mean | median | max |
|---|---:|---:|---:|---:|
| `pred_vs_roi_cpi_uop` | 17 | 3.94% | 2.31% | 12.76% |
| `pred_vs_label_cpi_uop` | 17 | 4.06% | 2.25% | 12.74% |
| `win_mape_cpi_uop` | 17 | 16.24% | 13.25% | 63.58% |
| `pred_vs_roi_cpi_macro` | 17 | 4.08% | 2.32% | 12.79% |
| `pred_vs_label_cpi_macro` | 17 | 4.06% | 2.25% | 12.74% |
| `label_vs_roi_cpi_uop` | 17 | 1.13% | 0.64% | 7.08% |
| `label_vs_roi_cpi_macro` | 17 | 1.21% | 0.80% | 7.02% |

cycle-weighted 口径：

| 指标 | 加权误差 |
|---|---:|
| weighted `pred_vs_roi_cpi_uop` | 1.01% |
| weighted `label_vs_roi_cpi_uop` | 2.00% |
| weighted `pred_vs_roi_cpi_macro` | 1.02% |
| weighted `label_vs_roi_cpi_macro` | 2.00% |

## 表 2：逐 workload CPI 结果

| Workload | 来源 | Windows | `pred_vs_roi_cpi_uop` | `pred_vs_roi_cpi_macro` | `win_mape_cpi_uop` |
|---|---|---:|---:|---:|---:|
| `W_ads_ctr` | main | 1096 | 4.09% | 4.38% | 17.70% |
| `W_ads_ranking_proxy` | main | 1460 | 0.53% | 0.53% | 63.58% |
| `W_branch_storm` | main | 1114 | 0.21% | 0.23% | 11.24% |
| `W_chase_dram` | main | 1335 | 1.41% | 1.59% | 11.60% |
| `W_compute_int` | main | 1227 | 0.81% | 0.81% | 2.88% |
| `W_false_sharing` | main | 1105 | 2.31% | 2.32% | 9.90% |
| `W_feed_ranking` | main | 836 | 0.61% | 1.77% | 8.49% |
| `W_fp_compute_dense` | main | 915 | 1.40% | 1.74% | 9.49% |
| `W_fp_lite` | main | 933 | 6.57% | 6.43% | 16.08% |
| `W_graph_recall_proxy` | retry2 | 1225 | 5.84% | 6.31% | 20.44% |
| `W_indirect` | main | 1202 | 6.28% | 6.29% | 8.49% |
| `W_int_div` | main | 1260 | 12.76% | 12.79% | 13.25% |
| `W_interest_graph_recall` | main | 819 | 0.89% | 0.73% | 17.24% |
| `W_mlp_light` | main | 1107 | 7.94% | 7.93% | 9.74% |
| `W_phased_mix` | retry2 | 1071 | 1.00% | 1.10% | 15.17% |
| `W_search_index_proxy` | main | 1087 | 8.92% | 8.95% | 24.65% |
| `W_stream` | main | 1124 | 5.42% | 5.36% | 16.05% |

## 表 3：整体 PMU 误差汇总

说明：

- `pVr` = `pred_vs_roi`
- `pVl` = `pred_vs_label`
- `lVr` = `label_vs_roi`
- 对 `roi=0` 或 `label=0` 导致不可定义的项做了跳过，因此不同 PMU 的样本数不同

| PMU | n | `pVr` mean | `pVr` median | `pVl` mean | `pVl` median | `lVr` mean | `lVr` median |
|---|---:|---:|---:|---:|---:|---:|---:|
| `branch_miss` | 17 | 120.47% | 30.30% | 24.99% | 15.55% | 80.00% | 13.73% |
| `l1d_ld_miss` | 16 | 98.35% | 24.21% | 58.69% | 14.35% | 85.60% | 17.27% |
| `l1d_st_miss` | 15 | 622.67% | 11.09% | 2705.13% | 12.98% | 65.80% | 7.21% |
| `l1i_miss` | 17 | 73.92% | 66.04% | 64.81% | 56.28% | 110.08% | 27.94% |
| `llc_miss` | 14 | 677.86% | 8.96% | 771.65% | 8.65% | 50.10% | 3.21% |
| `dtlb_miss` | 17 | 450.62% | 8.53% | 119.97% | 8.86% | 81.56% | 5.06% |
| `mshr_avg` | 17 | 9.15% | 3.24% | 6.11% | 2.31% | 9.78% | 0.74% |

## 表 4：逐 workload 关键 PMU 指标

这里列出每个 workload 最有代表性的两个 PMU 对齐指标：

- `dtlb_miss pred_vs_roi`
- `mshr_avg pred_vs_roi`

| Workload | 来源 | `pred_vs_roi_cpi_uop` | `dtlb_miss pVr` | `mshr_avg pVr` |
|---|---|---:|---:|---:|
| `W_ads_ctr` | main | 4.09% | 0.35% | 5.11% |
| `W_ads_ranking_proxy` | main | 0.53% | 8.53% | 1.08% |
| `W_branch_storm` | main | 0.21% | 98.82% | 3.51% |
| `W_chase_dram` | main | 1.41% | 5.31% | 3.68% |
| `W_compute_int` | main | 0.81% | 25.31% | 4.38% |
| `W_false_sharing` | main | 2.31% | 0.32% | 3.24% |
| `W_feed_ranking` | main | 0.61% | 0.46% | 43.02% |
| `W_fp_compute_dense` | main | 1.40% | 0.46% | 4.42% |
| `W_fp_lite` | main | 6.57% | 6.85% | 2.24% |
| `W_graph_recall_proxy` | retry2 | 5.84% | 22.77% | 0.97% |
| `W_indirect` | main | 6.28% | 9.43% | 0.28% |
| `W_int_div` | main | 12.76% | 6591.49% | 1.61% |
| `W_interest_graph_recall` | main | 0.89% | 11.10% | 16.25% |
| `W_mlp_light` | main | 7.94% | 2.18% | 2.52% |
| `W_phased_mix` | retry2 | 1.00% | 233.93% | 59.59% |
| `W_search_index_proxy` | main | 8.92% | 640.62% | 1.16% |
| `W_stream` | main | 5.42% | 2.67% | 2.43% |

## 表 5：逐 workload PMU 误差明细 A（`pred_vs_roi`）

| Workload | `branch_miss` | `l1d_ld_miss` | `l1d_st_miss` |
|---|---:|---:|---:|
| `W_ads_ctr` | 14.53% | 16.88% | 1.10% |
| `W_ads_ranking_proxy` | 10.43% | 6.22% | 7.58% |
| `W_branch_storm` | 28.55% | 45.70% | 6558.23% |
| `W_chase_dram` | 5.65% | 19.79% | 12.87% |
| `W_compute_int` | 1.57% | 18.26% | 599.56% |
| `W_false_sharing` | 37.17% | 13.95% | 3.94% |
| `W_feed_ranking` | 52.60% | 47.70% | 5.69% |
| `W_fp_compute_dense` | 49.87% | 28.62% | 4.95% |
| `W_fp_lite` | 47.02% | 3.23% | 1.42% |
| `W_graph_recall_proxy` | 12.16% | 37.00% | 47.47% |
| `W_indirect` | 3.09% | 54.90% | 488.12% |
| `W_int_div` | 230.41% | 594.32% | - |
| `W_interest_graph_recall` | 22.73% | 5.03% | 3.25% |
| `W_mlp_light` | 32.05% | - | - |
| `W_phased_mix` | 687.86% | 264.85% | 1077.94% |
| `W_search_index_proxy` | 782.09% | 397.70% | 516.82% |
| `W_stream` | 30.30% | 19.42% | 11.09% |

## 表 6：逐 workload PMU 误差明细 B（`pred_vs_roi`）

| Workload | `l1i_miss` | `llc_miss` | `dtlb_miss` | `mshr_avg` |
|---|---:|---:|---:|---:|
| `W_ads_ctr` | 65.85% | 3.37% | 0.35% | 5.11% |
| `W_ads_ranking_proxy` | 92.44% | 4.50% | 8.53% | 1.08% |
| `W_branch_storm` | 79.42% | 3549.36% | 98.82% | 3.51% |
| `W_chase_dram` | 61.42% | 6.32% | 5.31% | 3.68% |
| `W_compute_int` | 44.01% | 347.25% | 25.31% | 4.38% |
| `W_false_sharing` | 16.43% | 4356.52% | 0.32% | 3.24% |
| `W_feed_ranking` | 64.74% | 9.89% | 0.46% | 43.02% |
| `W_fp_compute_dense` | 66.26% | 8.03% | 0.46% | 4.42% |
| `W_fp_lite` | 71.57% | 7.32% | 6.85% | 2.24% |
| `W_graph_recall_proxy` | 81.40% | 807.55% | 22.77% | 0.97% |
| `W_indirect` | 84.92% | 19.87% | 9.43% | 0.28% |
| `W_int_div` | 13.14% | - | 6591.49% | 1.61% |
| `W_interest_graph_recall` | 55.83% | 0.90% | 11.10% | 16.25% |
| `W_mlp_light` | 45.41% | - | 2.18% | 2.52% |
| `W_phased_mix` | 99.33% | 364.51% | 233.93% | 59.59% |
| `W_search_index_proxy` | 248.45% | - | 640.62% | 1.16% |
| `W_stream` | 66.04% | 4.67% | 2.67% | 2.43% |

## 结果解读

1. CPI 主指标整体稳定。
   - 17 个 workload 合并后，`pred_vs_roi_cpi_uop` mean 3.94%，median 2.31%
   - cycle-weighted 口径只有 1.01%，说明模型对总周期预算的拟合相对稳

2. retry2 修复了上下文爆炸问题。
   - `W_graph_recall_proxy` 与 `W_phased_mix` 在主跑中失败
   - 改成默认不补齐 macro 边界后，二者均可完整完成

3. CPI 误差较大的 workload 仍集中在：
   - `W_int_div`：12.76%
   - `W_search_index_proxy`：8.92%
   - `W_mlp_light`：7.94%
   - `W_fp_lite`：6.57%
   - `W_indirect`：6.28%

4. PMU 误差显著高于 CPI，尤其是低计数 miss 项。
   - `mshr_avg` 是最稳定的 PMU 头之一
   - `dtlb_miss` / `llc_miss` / `l1d_st_miss` 在若干 workload 上会出现非常大的相对误差
   - 原因主要是 ROI 分母很小或接近 0 时，相对误差被放大；因此 PMU 头更适合作为机制诊断，而非主 KPI

5. `label_vs_roi` 整体较小。
   - 说明 quota eval 的窗口标签聚合与 ROI 全局统计基本对齐
   - 当前主要误差来源仍在模型预测侧，而不是标签/ROI 口径不一致
