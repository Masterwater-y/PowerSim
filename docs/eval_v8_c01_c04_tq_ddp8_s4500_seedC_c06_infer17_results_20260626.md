# v8_c01_c04_tq_ddp8_s4500 seedC c06 infer17 推理实验结果汇总

## 实验说明

- 模型：`ckpt/v8_c01_c04_tq_ddp8_s4500`
- 结果目录：`logs/eval_parallel_v8_c01_c04_tq_ddp8_s4500_seedC_c06_infer17_20260626_154419`
- 数据入口：`data/raw_v7_seedC_c06_infer17`
- 数据形态：6 核部署侧推理 cache，17 个 workload，102 个 `*.aligned.parquet`
- 运行口径：
  - `label` 为方案 C 部署侧动态切窗后的窗口标签聚合
  - `ROI` 为 trace ROI stats
  - `gem5_full` 为 `stats.txt` 全程统计，仅作参考
  - `pred_vs_roi` / `pred_vs_label` 均按绝对相对误差统计
- 推理参数：
  - `max_len=32768`
  - `dt_init=8000.0`
  - `dt_range=[200.0,12000.0]`
  - `target_load=0.95`
  - `seed_n=160`
  - `nmin=8`
  - `use_tstart=False`
- 完整性：
  - 17 个 workload 日志均写出 `Summary`
  - 未发现 `Traceback` / `ERROR` / `Exception` / `FAILED` / `OOM` / `Killed`
  - 17 个 workload 均为 `roi_pmu_coverage=100%`
  - 17 个 workload 均为 `valid_cores=6`

## 结论摘要

1. 6 核部署侧切窗链路本身是干净的。
   - `label_vs_roi_cpi_uop` mean/median/max 均为 0.00%
   - `roi_pmu_coverage` 全部为 100%
   - 说明这批结果的主误差来自模型预测侧，不是窗口标签聚合、ROI 统计或部分 core 提前结束导致的覆盖问题

2. 6 核外推对大多数 workload 可用，但仍被两个 outlier 主导。
   - 17 workload 平均 `pred_vs_roi_cpi_uop = 7.82%`，median `2.61%`
   - `pred_vs_roi_cpi_uop <= 5%`：11 / 17
   - `<= 10%`：14 / 17
   - `<= 15%`：15 / 17
   - 超过 15% 的 workload 只有 `W_false_sharing` 和 `W_ads_ranking_proxy`

3. 全局总周期误差被 `W_false_sharing` 单项主导。
   - 全量全局总周期误差：25.38%
   - `W_false_sharing` 占真实总周期 48.27%，占绝对周期误差 92.07%
   - 去掉 `W_false_sharing` 后，全局总周期误差降到 0.39%，但绝对周期误差仍为 4.19%
   - 再去掉 `W_ads_ranking_proxy` 后，15 个 workload 的绝对周期误差为 2.62%，全局总周期误差为 1.39%

4. 和 seedB 8 核 activecore 结果相比，6 核整体更好，但问题形态一致。
   - mean pVr：9.66% -> 7.82%
   - median pVr：3.84% -> 2.61%
   - max pVr：62.56% -> 52.16%
   - 全局总周期误差：35.47% -> 25.38%
   - `W_false_sharing` 和 `W_ads_ranking_proxy` 仍然是主要失败点

5. PMU count 头目前更适合作诊断，不适合作主 KPI。
   - `mshr_avg` median pVr 为 5.59%，相对可用
   - miss count 类 PMU 的相对误差经常被低计数小分母放大
   - `label_vs_roi` 基本为 0，说明 PMU 口径问题不在标签聚合，而在预测侧

## 表 1：整体 CPI 误差汇总

| 指标 | 样本数 | mean | median | max |
|---|---:|---:|---:|---:|
| `pred_vs_roi_cpi_uop` | 17 | 7.82% | 2.61% | 52.16% |
| `pred_vs_label_cpi_uop` | 17 | 7.82% | 2.61% | 52.16% |
| `win_mape_cpi_uop` | 17 | 17.91% | 13.09% | 55.00% |
| `pred_vs_roi_cpi_macro` | 17 | 7.82% | 2.61% | 52.16% |
| `label_vs_roi_cpi_uop` | 17 | 0.00% | 0.00% | 0.00% |
| `gem5_full_vs_roi_cpi_macro` | 17 | 31.12% | 4.65% | 255.95% |

全局聚合口径：

| 口径 | pred CPI | ROI CPI | label CPI | pred vs ROI |
|---|---:|---:|---:|---:|
| `cpi_uop` | 1.1477 | 1.5381 | 1.5381 | 25.38% |

补充统计：

| 指标 | 数值 |
|---|---:|
| total windows | 15305 |
| total uops | 75307839 |
| cycle-weighted absolute error | 27.35% |
| mean per-window MAPE | 17.91% |
| median per-window MAPE | 13.09% |

`gem5_full` 在 `W_phased_mix`、`W_search_index_proxy`、`W_graph_recall_proxy` 等 workload 上明显偏离 ROI，因此本报告仍以 trace ROI 为主要 baseline。

## 表 2：outlier 敏感性

| 口径 | workload 数 | mean pVr | median pVr | max pVr | 全局总周期误差 | cycle-weighted abs err |
|---|---:|---:|---:|---:|---:|---:|
| 全量 | 17 | 7.82% | 2.61% | 52.16% | 25.38% | 27.35% |
| 去掉 `W_false_sharing` | 16 | 5.05% | 2.24% | 33.08% | 0.39% | 4.19% |
| 去掉 `W_false_sharing` + `W_ads_ranking_proxy` | 15 | 3.18% | 1.86% | 11.11% | 1.39% | 2.62% |
| 再去掉 `W_search_index_proxy` | 14 | 2.61% | 1.70% | 7.00% | 1.05% | 2.32% |

这个表说明：整体误差不是均匀退化，而是少数 workload 的系统性偏差造成。

## 表 3：逐 workload CPI 结果

`signed bias = (pred - ROI) / ROI`。正值表示高估，负值表示低估。

| Workload | Windows | `pred_cpi_uop` | `roi_cpi_uop` | `pred_vs_roi` | signed bias | `win_mape` | avg instr/core/window |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W_ads_ctr` | 892 | 0.9878 | 0.9805 | 0.74% | +0.74% | 15.99% | 444.7 |
| `W_ads_ranking_proxy` | 1172 | 0.3594 | 0.5371 | 33.08% | -33.08% | 28.64% | 403.2 |
| `W_branch_storm` | 852 | 0.4573 | 0.4345 | 5.25% | +5.25% | 12.40% | 541.1 |
| `W_chase_dram` | 1009 | 2.8734 | 2.8332 | 1.42% | +1.42% | 12.92% | 353.2 |
| `W_compute_int` | 937 | 0.4345 | 0.4183 | 3.86% | +3.86% | 5.58% | 652.9 |
| `W_false_sharing` | 908 | 6.0076 | 12.5582 | 52.16% | -52.16% | 55.00% | 699.8 |
| `W_feed_ranking` | 671 | 0.8080 | 0.7932 | 1.86% | +1.86% | 13.09% | 437.8 |
| `W_fp_compute_dense` | 730 | 0.6141 | 0.6148 | 0.11% | -0.11% | 10.46% | 477.1 |
| `W_fp_lite` | 718 | 0.6597 | 0.6567 | 0.46% | +0.46% | 12.39% | 549.0 |
| `W_graph_recall_proxy` | 1072 | 0.4791 | 0.4865 | 1.53% | -1.53% | 27.25% | 363.7 |
| `W_indirect` | 946 | 0.9478 | 0.8941 | 6.00% | +6.00% | 9.59% | 496.1 |
| `W_int_div` | 1120 | 0.7381 | 0.7417 | 0.49% | -0.49% | 5.80% | 245.1 |
| `W_interest_graph_recall` | 633 | 1.0140 | 1.0005 | 1.35% | +1.35% | 17.59% | 228.2 |
| `W_mlp_light` | 846 | 0.4880 | 0.5080 | 3.93% | -3.93% | 5.65% | 536.7 |
| `W_phased_mix` | 863 | 0.6365 | 0.6844 | 7.00% | -7.00% | 23.60% | 512.1 |
| `W_search_index_proxy` | 1007 | 0.4323 | 0.3890 | 11.11% | +11.11% | 31.25% | 453.8 |
| `W_stream` | 929 | 1.5003 | 1.4622 | 2.61% | +2.61% | 17.23% | 491.4 |

## 表 4：误差贡献

| Workload | 真实周期占比 | 绝对误差贡献 | signed cycle error |
|---|---:|---:|---:|
| `W_false_sharing` | 48.27% | 92.07% | -29164328 |
| `W_ads_ranking_proxy` | 2.68% | 3.24% | -1025329 |
| `W_indirect` | 3.59% | 0.79% | +249481 |
| `W_search_index_proxy` | 1.66% | 0.67% | +213530 |
| `W_phased_mix` | 2.51% | 0.64% | -203183 |
| `W_chase_dram` | 12.13% | 0.63% | +199462 |
| `W_stream` | 5.77% | 0.55% | +174234 |

`W_false_sharing` 的真实周期占比接近一半，而且预测低估超过 52%，所以它几乎单独决定全局误差。排除该项后，全局 signed error 被其它 workload 的正负偏差互相抵消到 0.39%，但绝对误差仍有 4.19%，说明 `W_ads_ranking_proxy`、`W_search_index_proxy`、`W_phased_mix` 仍需关注。

## 表 5：PMU 全局误差概览

说明：

- `pVr` = `pred_vs_roi`
- 下表是 workload 维度的相对误差统计
- 低计数 miss 项的相对误差容易被小分母放大，因此 PMU 头更适合作诊断信号，不宜直接当主 KPI

| PMU | `pVr` mean | `pVr` median | `pVr` max | max workload |
|---|---:|---:|---:|---|
| `cpi_uop` | 7.82% | 2.61% | 52.16% | `W_false_sharing` |
| `branch_miss` | 66.54% | 54.46% | 275.98% | `W_mlp_light` |
| `l1d_ld_miss` | 321.96% | 58.39% | 2811.99% | `W_indirect` |
| `l1d_st_miss` | 344.53% | 76.43% | 2357.21% | `W_branch_storm` |
| `l1i_miss` | 40.49% | 30.45% | 80.12% | `W_indirect` |
| `llc_miss` | 1116.31% | 72.78% | 4298.36% | `W_graph_recall_proxy` |
| `dtlb_miss` | 161.13% | 74.75% | 1117.50% | `W_int_div` |
| `mshr_avg` | 12.66% | 5.59% | 45.90% | `W_branch_storm` |

PMU 的 `label_vs_roi` 基本为 0，说明当前 ROI PMU 聚合口径已经和窗口标签一致；PMU 误差主要来自预测侧。

## 表 6：与 seedB 8 核 activecore 的对比

参考文档：`docs/eval_v8_c01_c04_tq_ddp8_s4500_seedB_infer17_activecore_results_20260626.md`

| 指标 | seedB c08 activecore | seedC c06 | 变化 |
|---|---:|---:|---:|
| workload 数 | 17 | 17 | 0 |
| total windows | 20356 | 15305 | -5051 |
| total uops | 100728721 | 75307839 | -25420882 |
| mean `pred_vs_roi_cpi_uop` | 9.66% | 7.82% | -1.84 pp |
| median `pred_vs_roi_cpi_uop` | 3.84% | 2.61% | -1.23 pp |
| max `pred_vs_roi_cpi_uop` | 62.56% | 52.16% | -10.40 pp |
| 全局总周期误差 | 35.47% | 25.38% | -10.09 pp |
| cycle-weighted abs err | 36.34% | 27.35% | -8.99 pp |
| mean `win_mape_cpi_uop` | 18.00% | 17.91% | -0.09 pp |

逐 workload 关键变化：

| Workload | c08 pVr | c06 pVr | c08 ROI CPI | c06 ROI CPI | 结论 |
|---|---:|---:|---:|---:|---|
| `W_false_sharing` | 62.56% | 52.16% | 16.3142 | 12.5582 | 6 核真实 CPI 低于 8 核，但模型仍低估 |
| `W_ads_ranking_proxy` | 40.92% | 33.08% | 0.6052 | 0.5371 | 6 核略好，仍系统性低估 |
| `W_phased_mix` | 11.73% | 7.00% | 0.7150 | 0.6844 | 6 核改善明显 |
| `W_search_index_proxy` | 10.52% | 11.11% | 0.3922 | 0.3890 | 6 核略差，仍高估 |
| `W_fp_compute_dense` | 5.61% | 0.11% | 0.6537 | 0.6148 | 6 核显著改善 |

整体看，6 核比 8 核更接近训练分布，所以误差下降。但 outlier 类型没有变：跨核 coherence 型的 `false_sharing` 和 proxy 型的 `ads_ranking_proxy` 仍是模型外推的主要短板。

## 深入分析

### `W_false_sharing`

`W_false_sharing` 是当前最关键失败点：

- `pred_cpi_uop=6.0076`
- `ROI cpi_uop=12.5582`
- 低估 52.16%
- 真实周期占全量 48.27%
- 贡献 92.07% 的绝对周期误差

和 8 核 seedB 对比：

| 核数 | `pred_cpi_uop` | `ROI cpi_uop` | pVr |
|---:|---:|---:|---:|
| 6 | 6.0076 | 12.5582 | 52.16% |
| 8 | 6.1079 | 16.3142 | 62.56% |

模型对 6 核和 8 核给出的预测几乎都在 6 左右，说明它没有学到 false sharing 成本随 active core 数继续上升的规律。考虑到该模型训练集主要来自 1 核和 4 核，这个结果符合预期：4 核训练已能让模型看到 coherence penalty，但不足以支撑 6/8 核的 ownership bouncing 外推。

这也说明，仅靠每核局部 trace token 很难表达“多个 core 正在竞争同一个 cache line 写权限”。后续需要显式增强跨核共享/失效相关特征，或者补充 6/8 核训练样本。

### `W_ads_ranking_proxy`

`W_ads_ranking_proxy` 是第二大失败点：

- `pred_cpi_uop=0.3594`
- `ROI cpi_uop=0.5371`
- 低估 33.08%
- 贡献 3.24% 的绝对周期误差

和 8 核 seedB 对比：

| 核数 | `pred_cpi_uop` | `ROI cpi_uop` | pVr |
|---:|---:|---:|---:|
| 6 | 0.3594 | 0.5371 | 33.08% |
| 8 | 0.3576 | 0.6052 | 40.92% |

模型预测也几乎固定在 0.36 附近，但真实 CPI 从 6 核到 8 核继续上升。这说明 proxy 类 workload 的在线路径中存在模型没有捕捉到的规模/并发敏感项，可能来自多表 embedding gather、候选排序和 cache/TLB 压力组合。单靠当前 token 聚合后，模型更像是在复用低核或短上下文下的 pattern。

### `W_search_index_proxy`

`W_search_index_proxy` 是主要高估项：

- `pred_cpi_uop=0.4323`
- `ROI cpi_uop=0.3890`
- 高估 11.11%
- `win_mape=31.25%`

这里全局 CPI 误差不算灾难，但 per-window MAPE 高，说明窗口级波动没有稳定预测。对部署侧控制而言，这会影响下一窗配额的稳定性；建议后续用 window dump 检查它的窗口级 residual 是否和相位切换、倒排列表长度或 miss burst 有关。

## 推理性能

按每个 workload 日志最后一条 `timing(avg/window)` 聚合：

| 阶段 | mean | median | min | max |
|---|---:|---:|---:|---:|
| total | 1260.8 ms | 1259.9 ms | 1243.8 ms | 1300.2 ms |
| forward | 1188.6 ms | 1185.1 ms | 1176.2 ms | 1226.8 ms |
| encode | 61.2 ms | 61.8 ms | 56.7 ms | 66.3 ms |
| build | 3.9 ms | 3.8 ms | 3.3 ms | 4.7 ms |
| update | 5.0 ms | 4.9 ms | 4.6 ms | 5.7 ms |

GPU forward 仍占单窗口总耗时约 94%，输入构造和状态更新不是主要瓶颈。

## 建议

1. 优先补齐 `W_false_sharing` 的多核训练覆盖。
   - 当前 1 核 + 4 核训练不足以外推到 6/8 核
   - 至少应加入 6 核或 8 核 false sharing 训练窗口
   - 更好的方案是加入显式跨核共享行、store ownership、invalidations 或 remote HITM 类 proxy feature

2. 单独分析 `W_ads_ranking_proxy`。
   - 6 核和 8 核都低估，且预测值几乎不随核数变化
   - 建议打开窗口 dump，对比 sparse gather / MLP / heap 阶段的 residual
   - 如果 proxy 类 workload 作为重要部署目标，应补充 6 核或 8 核 proxy 训练样本

3. 对 `W_search_index_proxy` 做窗口级 residual 分析。
   - 全局误差 11.11%，但 per-window MAPE 31.25%
   - 这类高 MAPE 会影响动态配额稳定性，即使最终全局 CPI 误差可接受

4. PMU count 头暂不作为主 KPI。
   - `mshr_avg` 可作为较稳定诊断项
   - miss count 类指标应结合绝对计数和分母过滤，否则相对误差容易被小分母放大

5. 当前 6 核外推结论：
   - 对大部分普通 compute/memory/control workload，1 核 + 4 核训练已经能较好外推到 6 核
   - 对跨核 coherence storm 和复杂 proxy workload，仍需要显式多核训练或跨核特征
