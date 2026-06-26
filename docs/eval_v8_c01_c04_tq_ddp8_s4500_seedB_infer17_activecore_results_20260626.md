# v8_c01_c04_tq_ddp8_s4500 seedB infer17 activecore 推理实验结果汇总

## 实验说明

- 模型：`ckpt/v8_c01_c04_tq_ddp8_s4500`
- 结果目录：`logs/eval_parallel_v8_c01_c04_tq_ddp8_s4500_seedB_infer17_activecore_20260626_142203`
- 数据入口：`data/raw_v7_seedB_c08_infer17`
- 统计口径：
  - 共 17 个 workload，17 个日志均写出 `Summary`
  - `label` 为方案 C 切窗后窗口标签聚合
  - `ROI` 为 trace ROI stats
  - `gem5_full` 为 `stats.txt` 全程统计，仅作参考
  - `pred_vs_roi` / `pred_vs_label` 均按绝对相对误差统计
- 运行参数：
  - `max_len=32768`
  - `dt_init=8000.0`
  - `dt_range=[200.0,12000.0]`
  - `target_load=0.95`
  - `seed_n=160`
  - `nmin=8`
  - `use_tstart=False`
- 完整性：
  - 未发现 `ERROR` / `Traceback` / `Exception` / `FAILED` / `OOM` / `Killed`
  - 17 个 workload 的 `roi_pmu_coverage=100%`
  - 17 个 workload 均为 `valid_cores=8`

## 结论摘要

1. 这批 activecore 口径下，`label` 与 `ROI` 已完全对齐。
   - `label_vs_roi_cpi_uop` mean/median/max 均为 0.00%
   - 说明当前主要误差来自模型预测侧，而不是切窗标签聚合或 ROI 统计口径

2. CPI 主指标整体被两个强 outlier 拉坏。
   - 17 workload 平均 `pred_vs_roi_cpi_uop = 9.66%`，median `3.84%`
   - 全局总周期误差为 `35.47%`
   - `W_false_sharing` 单项误差 `62.56%`，且是高周期 workload，主导了全局误差
   - 去掉 `W_false_sharing` 后，全局总周期误差降到 `3.64%`
   - 再去掉 `W_ads_ranking_proxy` 后，15 个 workload 的全局总周期误差降到 `1.41%`

3. 通过率分布：
   - `pred_vs_roi_cpi_uop <= 5%`：10 / 17
   - `<= 10%`：13 / 17
   - `<= 15%`：15 / 17
   - 超过 15% 的 workload 为 `W_false_sharing`、`W_ads_ranking_proxy`

4. 主要风险点：
   - `W_false_sharing`：严重低估，`pred=6.1079` vs `ROI=16.3142`
   - `W_ads_ranking_proxy`：明显低估，`pred=0.3576` vs `ROI=0.6052`
   - `W_phased_mix`：中等低估，`11.73%`
   - `W_search_index_proxy`：中等高估，`10.52%`

## 表 1：整体 CPI 误差汇总

| 指标 | 样本数 | mean | median | max |
|---|---:|---:|---:|---:|
| `pred_vs_roi_cpi_uop` | 17 | 9.66% | 3.84% | 62.56% |
| `pred_vs_label_cpi_uop` | 17 | 9.66% | 3.84% | 62.56% |
| `win_mape_cpi_uop` | 17 | 18.00% | 13.24% | 63.74% |
| `pred_vs_roi_cpi_macro` | 17 | 9.66% | 3.84% | 62.56% |
| `label_vs_roi_cpi_uop` | 17 | 0.00% | 0.00% | 0.00% |
| `gem5_full_vs_roi_cpi_macro` | 17 | 31.52% | 3.67% | 267.22% |

全局聚合口径：

| 口径 | pred CPI | ROI CPI | label CPI | pred vs ROI |
|---|---:|---:|---:|---:|
| `cpi_uop` | 1.1489 | 1.7804 | 1.7804 | 35.47% |
| `cpi_macro` | 2.0905 | 3.2395 | 3.2395 | 35.47% |

说明：`gem5_full` 在 `W_phased_mix`、`W_search_index_proxy`、`W_graph_recall_proxy` 等 workload 上明显偏离 ROI，因此本报告仍以 trace ROI 为主 baseline。

## 表 2：outlier 敏感性

| 口径 | workload 数 | mean pVr | median pVr | max pVr | 全局总周期误差 |
|---|---:|---:|---:|---:|---:|
| 全量 | 17 | 9.66% | 3.84% | 62.56% | 35.47% |
| 去掉 `W_false_sharing` | 16 | 6.36% | 3.63% | 40.92% | 3.64% |
| 去掉 `W_false_sharing` + `W_ads_ranking_proxy` | 15 | 4.05% | 3.43% | 11.73% | 1.41% |

这个表说明当前全局误差不是均匀退化，而是少数 workload 的系统性低估造成。

## 表 3：逐 workload CPI 结果

| Workload | Windows | `pred_cpi_uop` | `roi_cpi_uop` | `pred_vs_roi` | `win_mape` | signed bias |
|---|---:|---:|---:|---:|---:|---:|
| `W_ads_ctr` | 1174 | 0.9910 | 0.9999 | 0.90% | 18.34% | -0.90% |
| `W_ads_ranking_proxy` | 1537 | 0.3576 | 0.6052 | 40.92% | 31.03% | -40.92% |
| `W_branch_storm` | 1136 | 0.4569 | 0.4346 | 5.13% | 13.12% | +5.13% |
| `W_chase_dram` | 1346 | 2.8460 | 2.9076 | 2.12% | 13.24% | -2.12% |
| `W_compute_int` | 1249 | 0.4344 | 0.4183 | 3.84% | 5.25% | +3.84% |
| `W_false_sharing` | 1209 | 6.1079 | 16.3142 | 62.56% | 63.74% | -62.56% |
| `W_feed_ranking` | 859 | 0.8082 | 0.8254 | 2.08% | 11.82% | -2.08% |
| `W_fp_compute_dense` | 972 | 0.6171 | 0.6537 | 5.61% | 11.77% | -5.61% |
| `W_fp_lite` | 958 | 0.6642 | 0.6675 | 0.49% | 8.41% | -0.49% |
| `W_graph_recall_proxy` | 1480 | 0.4829 | 0.4949 | 2.42% | 30.11% | -2.42% |
| `W_indirect` | 1261 | 0.9424 | 0.8966 | 5.11% | 8.47% | +5.11% |
| `W_int_div` | 1494 | 0.7338 | 0.7414 | 1.02% | 5.53% | -1.02% |
| `W_interest_graph_recall` | 845 | 1.0095 | 1.0290 | 1.90% | 14.62% | -1.90% |
| `W_mlp_light` | 1128 | 0.4852 | 0.5080 | 4.48% | 6.73% | -4.48% |
| `W_phased_mix` | 1134 | 0.6312 | 0.7150 | 11.73% | 19.29% | -11.73% |
| `W_search_index_proxy` | 1335 | 0.4335 | 0.3922 | 10.52% | 29.42% | +10.52% |
| `W_stream` | 1239 | 1.4913 | 1.5442 | 3.43% | 15.12% | -3.43% |

## 表 4：高误差 workload 的 signed bias

`signed bias = (pred - ROI) / ROI`，正值表示高估，负值表示低估。

| Workload | `pred_cpi_uop` | `roi_cpi_uop` | `pred_vs_roi` | signed bias | `win_mape` |
|---|---:|---:|---:|---:|---:|
| `W_false_sharing` | 6.1079 | 16.3142 | 62.56% | -62.56% | 63.74% |
| `W_ads_ranking_proxy` | 0.3576 | 0.6052 | 40.92% | -40.92% | 31.03% |
| `W_phased_mix` | 0.6312 | 0.7150 | 11.73% | -11.73% | 19.29% |
| `W_search_index_proxy` | 0.4335 | 0.3922 | 10.52% | +10.52% | 29.42% |
| `W_fp_compute_dense` | 0.6171 | 0.6537 | 5.61% | -5.61% | 11.77% |
| `W_branch_storm` | 0.4569 | 0.4346 | 5.13% | +5.13% | 13.12% |
| `W_indirect` | 0.9424 | 0.8966 | 5.11% | +5.11% | 8.47% |
| `W_mlp_light` | 0.4852 | 0.5080 | 4.48% | -4.48% | 6.73% |

## 表 5：PMU 全局误差概览

说明：

- `pVr` = `pred_vs_roi`
- `lVr` = `label_vs_roi`
- 下表是 workload 维度的相对误差统计
- 低计数 miss 项的相对误差容易被小分母放大，因此 PMU 头更适合作为诊断信号，不宜直接当主 KPI

| PMU | n | `pVr` mean | `pVr` median | `pVr` max | `lVr` mean |
|---|---:|---:|---:|---:|---:|
| `branch_miss` | 17 | 498.19% | 81.72% | 7157.36% | 0.00% |
| `l1d_ld_miss` | 16 | 398.95% | 77.05% | 3150.89% | 0.00% |
| `l1d_st_miss` | 16 | 499.80% | 107.92% | 3057.09% | 0.00% |
| `l1i_miss` | 17 | 77.21% | 30.79% | 748.35% | 0.00% |
| `llc_miss` | 16 | 1551.83% | 107.62% | 10170.20% | 0.00% |
| `dtlb_miss` | 17 | 207.98% | 101.02% | 1624.27% | 0.00% |
| `mshr_avg` | 17 | 31.07% | 6.35% | 296.34% | 0.00% |

PMU 的 `label_vs_roi` 基本为 0，说明当前 ROI PMU 聚合口径已经和窗口标签一致；PMU 误差主要也来自预测侧。

## 表 6：关键 PMU 指标逐 workload

| Workload | CPI pVr | `branch_miss` pVr | `dtlb_miss` pVr | `mshr_avg` pVr | `l1i_miss` pVr |
|---|---:|---:|---:|---:|---:|
| `W_ads_ctr` | 0.90% | 50.81% | 98.48% | 1.51% | 9.67% |
| `W_ads_ranking_proxy` | 40.92% | 59.37% | 40.88% | 11.01% | 20.37% |
| `W_branch_storm` | 5.13% | 112.40% | 163.21% | 58.77% | 73.80% |
| `W_chase_dram` | 2.12% | 95.55% | 106.90% | 0.61% | 5.67% |
| `W_compute_int` | 3.84% | 123.31% | 547.60% | 53.78% | 80.67% |
| `W_false_sharing` | 62.56% | 55.08% | 77.02% | 13.05% | 2.29% |
| `W_feed_ranking` | 2.08% | 81.72% | 109.68% | 44.77% | 2.11% |
| `W_fp_compute_dense` | 5.61% | 26.77% | 101.02% | 0.49% | 0.65% |
| `W_fp_lite` | 0.49% | 29.48% | 108.39% | 0.11% | 1.44% |
| `W_graph_recall_proxy` | 2.42% | 127.67% | 63.90% | 6.35% | 56.71% |
| `W_indirect` | 5.11% | 85.75% | 52.41% | 2.82% | 85.32% |
| `W_int_div` | 1.02% | 79.71% | 1624.27% | 1.27% | 81.16% |
| `W_interest_graph_recall` | 1.90% | 62.12% | 136.67% | 21.79% | 11.02% |
| `W_mlp_light` | 4.48% | 7157.36% | 47.83% | 0.70% | 748.35% |
| `W_phased_mix` | 11.73% | 157.03% | 80.00% | 296.34% | 55.22% |
| `W_search_index_proxy` | 10.52% | 133.93% | 51.54% | 10.94% | 47.37% |
| `W_stream` | 3.43% | 31.21% | 125.86% | 3.96% | 30.79% |

## 推理性能

按日志中的 `timing(avg/window)` 聚合：

| 阶段 | mean | median | min | max |
|---|---:|---:|---:|---:|
| total | 1289.4 ms | 1282.1 ms | 1265.6 ms | 1338.4 ms |
| forward | 1214.6 ms | 1205.8 ms | 1195.4 ms | 1259.1 ms |
| encode | 63.2 ms | 63.9 ms | 57.5 ms | 70.0 ms |
| build | 3.9 ms | 3.7 ms | 3.3 ms | 4.8 ms |
| update | 5.3 ms | 5.3 ms | 5.0 ms | 6.0 ms |

GPU forward 约占单窗口总耗时的 94%，输入构造和状态更新不是瓶颈。

## 历史参考：v7 seedA ROI/PMU fix

参考目录：`logs/eval_parallel_v7_c08_absmiss_ddp8_roi_pmu_fix_20260625_172427`。

注意：该参考批次使用 `data/raw_v7_seedA_c08`，当前批次使用 `data/raw_v7_seedB_c08_infer17`，因此这不是严格 A/B，只能作为历史量级参考。

| 指标 | v7 seedA | v8 seedB activecore |
|---|---:|---:|
| workload 数 | 17 | 17 |
| `pred_vs_roi_cpi_uop` mean | 3.96% | 9.66% |
| `pred_vs_roi_cpi_uop` median | 2.30% | 3.84% |
| `pred_vs_roi_cpi_uop` max | 12.62% | 62.56% |
| 全局总周期误差 | 7.33% | 35.47% |
| `label_vs_roi_cpi_uop` mean | 1.05% | 0.00% |

相较历史参考，当前批次的标签/ROI 口径更干净，但预测误差分布明显更尖，主要集中在 `W_false_sharing` 与 `W_ads_ranking_proxy`。

## 深入分析：`W_false_sharing` 与 `W_ads_ranking_proxy`

### 日志里的早期误差形态

当前日志没有逐窗口 dump，第一条进度日志是前 20 个窗口的累计结果。即便如此，两个 workload 在第一条累计点就已经进入稳定低估状态，说明问题不是尾部少数窗口拉偏。

| Workload | 首条进度 | `pred_cpi_uop` | `label_cpi_uop` | `ROI cpi_uop` | signed bias |
|---|---:|---:|---:|---:|---:|
| `W_false_sharing` | 20 / 1209 windows, 1.7% | 4.9020 | 18.1488 | 16.3142 | -73.0% vs label |
| `W_ads_ranking_proxy` | 20 / 1537 windows, 1.3% | 0.4536 | 0.9570 | 0.6052 | -52.6% vs label |

后续误差不是被修正，而是维持同一方向：

| Workload | 早期累计 | 中后期累计 | 最终 Summary |
|---|---:|---:|---:|
| `W_false_sharing` | 20 windows: `4.9020 / 18.1488` | 1200 windows: `6.1396 / 16.3581` | `6.1079 / 16.3142`, 低估 62.56% |
| `W_ads_ranking_proxy` | 20 windows: `0.4536 / 0.9570` | 1520 windows: `0.3573 / 0.6016` | `0.3576 / 0.6052`, 低估 40.92% |

这里的 `label` 与 `ROI` 最终完全一致，因此可以排除 ROI 聚合、窗口标签对齐造成的主误差。问题集中在模型预测侧。

### 训练集与推理集的分布错位

`v8_c01_c04_tq_ddp8_s4500` 的训练数据来自：

- `data/raw_v8_train_c01_seedA`
- `data/raw_v8_train_c04_seedA`
- 合并窗口：`data/windows_v8_train_c01_c04_tq/windows.jsonl`
- 训练样本数：40014，其中 train 36013、val 4001
- 训练参数：`USE_TSTART=0`，推理也为 `use_tstart=False`

当前评估数据为 `data/raw_v7_seedB_c08_infer17`，有效 core 数为 8。也就是说，模型只见过 1 核和 4 核训练窗口，却被用于 8 核 activecore 推理。

更关键的是，训练和推理都受 `max_len=32768` 约束，窗口总 token/uop 规模接近固定。到 8 核 activecore 后，每个 core 分到的窗口上下文约为 4 核训练的一半：

| Workload | 训练 c01 `core_uops_mean` p50 | 训练 c04 `core_uops_mean` p50 | c08 推理平均 uops/core/window |
|---|---:|---:|---:|
| `W_false_sharing` | 4838 | 1209 | 约 614 |
| `W_ads_ranking_proxy` | 4838 | 1209 | 约 626 |

因此当前输入同时存在两个 OOD 条件：active core 数从 4 外推到 8；单 core 上下文长度从约 1200 uops 下降到约 600 uops。

### `W_false_sharing`：8 核 coherence storm 超出训练支撑

`workloads/src/bench_false_sharing.c` 的核心行为是多个线程写同一个 cacheline 的不同 word，并读取邻近 slot。这个 workload 的真实瓶颈是跨核 cacheline ownership/invalidation 往返，成本随参与 core 数明显上升。

训练集分布显示，`W_false_sharing` 在 1 核和 4 核之间 CPI 已经发生数量级变化：

| Split | 样本数 | CPI p5 | CPI p50 | CPI p95 | CPI max |
|---|---:|---:|---:|---:|---:|
| train c01 | 1194 | 0.6151 | 0.6919 | 0.6924 | 0.7205 |
| train c04 | 1199 | 6.2444 | 7.6375 | 10.5243 | 10.5607 |
| eval c08 | 1209 windows | - | 16.3142 ROI | - | - |

8 核 ROI CPI 16.3142 已经比训练 c04 max 10.5607 高约 54.5%。模型最终预测 6.1079，基本落在 c04 训练分布低端附近，而没有外推到 8 核 coherence 成本。

从 functional summary 看，c01 和 c04 的单核行为非常相似：

| 指标 | train c01 p50 | train c04 p50 |
|---|---:|---:|
| `op_load_ratio` | 0.2307 | 0.2306 |
| `op_store_ratio` | 0.1538 | 0.1540 |
| `store_rd_hot_ratio` | 1.0000 | 1.0000 |
| `seen_line_rate_64k` | 1.0000 | 1.0000 |
| `distinct_lines` | 1 | 1 |
| `distinct_pages` | 1 | 1 |

这解释了为什么模型难以判断 8 核会更慢：单 core trace 看起来只是“反复访问一个很热的 cacheline”，而不是“8 个 core 正在竞争同一个 cacheline 的写权限”。当前 token/schema 没有显式跨 core 共享行、sharer_count、ownership bouncing 或 invalidation rate 之类特征；active core 数也没有作为显式 token 输入，只能从 core block 数量间接学习。训练又没有 c08 样本，所以外推失败。

结论：`W_false_sharing` 的大误差主要是训练支撑不足 + 缺少跨核 coherence 表征，不是冷启动偶然误差。第一条累计日志已经 -73%，最终仍 -62.56%，符合系统性低估。

### `W_ads_ranking_proxy`：独立线程的并发内存压力没有被充分建模

`workloads/src/bench_ads_ranking_proxy.c` 是多表 embedding gather、feature crossing、MLP 和 topK heap 的组合。每个 worker 分配自己的 `Tu/Ti/Tc/req/cand/...` 等数组，ROI 内没有显式共享锁或 atomic。它不像 `W_false_sharing` 那样有单 cacheline coherence storm，但 8 个 core 同时做随机 gather 会放大共享 LLC/DRAM/队列压力。

训练集 CPI 分布如下：

| Split | 样本数 | CPI p5 | CPI p50 | CPI p95 | CPI max |
|---|---:|---:|---:|---:|---:|
| train c01 | 1192 | 0.3264 | 0.3441 | 0.3745 | 0.5772 |
| train c04 | 1195 | 0.3270 | 0.4266 | 0.8462 | 5.0858 |
| eval c08 | 1537 windows | - | 0.6052 ROI | - | - |

不同于 `W_false_sharing`，c08 ROI=0.6052 仍在 c04 训练分布内，但模型预测 0.3576，几乎贴近 c01/全量训练中位数，而不是贴近 c04/c08 并发压力。这说明模型没有可靠地把“更多 active cores + 同时随机访存”映射到更高 CPI。

PMU 归一化后也能看到这个 workload 的迷惑性：c08 推理的 label 并没有在若干单核可见指标上显得极端。

| 指标 | train c01 p50 | train c04 p50 | eval c08 label |
|---|---:|---:|---:|
| `branch_miss / kuop` | 2.81 | 2.89 | 2.91 |
| `dtlb_miss / kuop` | 28.28 | 38.00 | 30.94 |
| `llc_miss / kuop` | 0.00 | 0.45 | 0.97 |
| `mshr_avg` | 10.22 | 9.12 | 8.74 |

这些指标不足以直接提示模型“CPI 应该到 0.60”。真实差异更可能来自多 core 并发下共享缓存、内存带宽、miss queue、MSHR/DRAM 排队等资源竞争；当前输入主要是 per-core functional trace 和 per-core summary，缺少跨 core aggregate memory intensity、同时 outstanding miss 压力、全局 distinct line/page、带宽压力等显式上下文。

结论：`W_ads_ranking_proxy` 的误差不是因为 c08 CPI 完全超出训练 CPI 范围，而是模型把它判成了更接近 c01/低并发状态的样本。第一条累计日志 -52.6%，最终 -40.92%，也是从开头就存在的并发压力低估。

### 优先验证项

1. 对这两个 workload 单独开启窗口 dump，抓取前 50 个窗口的输入 token、per-core summary、label 和 prediction。
   - 目标是确认第一窗口是否已经和前 20 个窗口累计一致，以及 8 个 core 的窗口是否都只有约 600 uops/core。

2. 用同 checkpoint 跑 c04 activecore 对照。
   - 如果 `W_false_sharing` 在 c04 上预测接近 6-8、真实也接近训练 c04 分布，则可以直接证明当前 c08 问题是 core-count 外推。

3. 补 c08 训练窗口或 finetune 小集，优先包含 `W_false_sharing`、`W_ads_ranking_proxy`、`W_stream`、`W_phased_mix` 这类共享资源敏感 workload。
   - 对 `W_false_sharing`，必须让模型见到 6/8 核 coherence 成本，否则仅靠 c01/c04 很难学出 16+ CPI。

4. 在 schema 中加入显式并发/共享资源特征。
   - 最低成本：`active_cores`、`uops_per_core_window`、`total_window_uops`。
   - 对 coherence：跨 core 同 cacheline overlap、写共享 line 数、最大 sharer count、store-to-shared-line 比例。
   - 对 ranking/gather 类：全局 distinct lines/pages、跨 core aggregate load/store intensity、估计带宽压力、并发 random gather 密度。

## 建议

1. 不建议直接把 `ckpt/v8_c01_c04_tq_ddp8_s4500` 作为当前主推 checkpoint。
   - 即使多数 workload 可接受，`W_false_sharing` 会把全局总周期误差拉到 35.47%

2. 优先定位 `W_false_sharing` 的预测侧退化。
   - 它是高周期 workload，`ROI cycles=96877562`
   - 当前预测 CPI 只有 ROI 的 37.44%
   - 建议对比训练集 `data/raw_v8_train_c01_seedA`、`data/raw_v8_train_c04_seedA` 中同类 workload 的 CPI/PMU/active-core 特征分布

3. 第二优先级处理 `W_ads_ranking_proxy`。
   - 当前低估 40.92%
   - 去掉 `W_false_sharing` 后，它成为剩余 workload 中最大误差点

4. 对 PMU 头单独设诊断阈值。
   - `mshr_avg` 相对最稳定，但在 `W_phased_mix` 仍有 296.34% outlier
   - miss 类计数建议同时看绝对误差和分母规模，避免只用相对误差判断模型质量

5. 后续如果需要证明 activecore 方案本身收益，需要补一批同数据、同 GPU、同 checkpoint、仅关闭 activecore 的完整对照。
   - 目录 `logs/eval_parallel_v8_c01_c04_tq_ddp8_s4500_seedB_infer17_20260626_140903` 只有 8 个 CPU 初始化日志，未写出 Summary，不能作为有效对照。
