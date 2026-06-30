# v11 timing Qwen3-0.6B step8000 评估结果

日期：2026-06-29

## 结论摘要

这版 `v11_timing_qwen3_0p6b_c01_c04_c08_c16_8000` 在已覆盖训练核心数的 seedB 验证上整体可用，但精度没有超过 v9。c04/c08/c16 的 CPI mean pVr 分别为 `5.71%`、`7.80%`、`11.26%`，median 分别为 `4.13%`、`5.33%`、`7.79%`。

c32 是未参与训练核心数的部署侧外推验证，当前明显不稳：CPI mean/median/max pVr 为 `28.17% / 20.93% / 84.30%`。主要崩在 `W_chase_dram`、`W_phased_mix`、`W_stream`、`W_false_sharing`、`W_graph_recall_proxy`，说明 32 核下的内存系统压力、共享放大和 phase/tail 行为还没有被这版模型可靠外推。

推理吞吐保持较高：c04/c08/c16/c32 的 per-workload mean throughput 分别约 `20.2K / 24.6K / 24.9K / 21.6K uops/s`。按 8 GPU 同时跑最优 8 个 workload 的粗略容量估计，c04/c08/c16/c32 分别约 `175K / 210K / 214K / 187K uops/s`。

PMU count 头仍不适合作主验收指标。`cpi_uop` 可用；`branch_miss`、L1/L2/LLC count 的 mean/max 经常被低计数分母放大。PMU 当前应作为辅助诊断和 regularizer，主 KPI 仍看 CPI/cycle。

## 实验配置

| 项 | 值 |
|---|---|
| checkpoint | `ckpt/v11_timing_qwen3_0p6b_c01_c04_c08_c16_8000/head_best.pt` |
| base model | `Qwen/Qwen3-0.6B-Base` |
| label/schema | `v11_timing_attn_side_l2_no_mshr_no_iside` |
| 训练窗口 | `data/windows_v11_timing_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl` |
| tensor cache | `data/windows_v11_timing_tq_train600_seedA_c01_c04_c08_c16/windows.maxlen32768.tensor_cache` |
| 训练样本数 | `40566` |
| 训练核心数分布 | c01 `10187` / c04 `10183` / c08 `10152` / c16 `10044` |
| max train len | `32768` |
| eval max len | `40960` |
| side feat dim | `47` |
| attn feat dim | `20` |
| PMU labels | `cpi_uop`, `branch_miss`, `l1d_ld_miss`, `l1d_st_miss`, `l2_ld_miss`, `l2_st_miss`, `llc_miss`, `dtlb_miss` |

训练正常结束：

| 指标 | 值 |
|---|---:|
| steps | `8000` |
| best step | `8000` |
| best val loss | `-32.3105` |
| total train time | `8940.9s` |
| avg step time | `1117.6 ms/step` |
| avg throughput | `7.2 samp/s`, `23094 tok/s` |

## 评估输入

| 核数 | raw trace | eval logdir |
|---:|---|---|
| c04 | `data/raw_trace_pool/activecore_eval/c04_seedB_infer17` | `logs/eval_parallel_v11_qwen3_0p6b_step8000_c04_seedB_full_ctx40960_20260629_190612` |
| c08 | `data/raw_trace_pool/activecore_eval/c08_seedB_infer17` | `logs/eval_parallel_v11_qwen3_0p6b_step8000_c08_seedB_full_ctx40960_20260629_200127` |
| c16 | `data/raw_trace_pool/activecore_eval/c16_seedB_infer17` | `logs/eval_parallel_v11_qwen3_0p6b_step8000_c16_seedB_full_ctx40960_20260629_210903` |
| c32 | `data/raw_trace_pool/activecore_eval/c32_seedB_infer17` | `logs/eval_parallel_v11_qwen3_0p6b_step8000_c32_seedB_full_ctx40960_20260629_221403` |

完整性：

- 4 组 eval 都完成 `17/17` workloads，共 `68` 个 workload log。
- `label_vs_roi_cpi_uop` 最大值为 `0.00%`，窗口标签聚合与 ROI stats 对齐正常。
- c04/c08/c16 属于训练核心数覆盖范围内的 seedB 验证；c32 是部署侧核心数外推验证，不应和训练覆盖核心数混作同一结论。
- 本轮没有 c06 eval。

## CPI 精度汇总

| 核数 | workloads | mean pVr | median pVr | max pVr | mean win MAPE | median win MAPE |
|---:|---:|---:|---:|---:|---:|---:|
| c04 | 17 | 5.71% | 4.13% | 26.47% | 16.19% | 10.77% |
| c08 | 17 | 7.80% | 5.33% | 38.95% | 17.93% | 12.17% |
| c16 | 17 | 11.26% | 7.79% | 63.36% | 29.71% | 14.94% |
| c32 | 17 | 28.17% | 20.93% | 84.30% | 55.59% | 26.94% |

观察：

- c04/c08/c16 随核心数升高误差逐步扩大，说明 timing attention/side 特征能用，但高核放大仍偏弱。
- c16 的 worst case 来自 `W_phased_mix`，预测 `2.4744`，ROI `6.7535`，低估 `63.36%`。
- c32 外推很差，不只是 false sharing；DRAM chase、stream、phase mix、graph proxy 都出现大幅低估。

## Top CPI 误差

### c04 seedB

| workload | windows | pred | ROI | pVr | win MAPE | uops/s | macro/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W_false_sharing` | 2583 | 9.1414 | 7.2284 | 26.47% | 66.46% | 18507 | 14242 |
| `W_phased_mix` | 2097 | 0.8120 | 0.6947 | 16.88% | 41.80% | 17683 | 10815 |
| `W_ads_ctr` | 1862 | 0.8281 | 0.9138 | 9.38% | 13.33% | 18826 | 10145 |
| `W_ads_ranking_proxy` | 2810 | 0.3978 | 0.4270 | 6.83% | 22.85% | 17780 | 8507 |
| `W_int_div` | 2774 | 0.6935 | 0.7432 | 6.68% | 8.11% | 21349 | 6478 |

### c08 seedB

| workload | windows | pred | ROI | pVr | win MAPE | uops/s | macro/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W_phased_mix` | 1129 | 0.9935 | 0.7150 | 38.95% | 88.79% | 19981 | 12238 |
| `W_ads_ranking_proxy` | 2417 | 0.4509 | 0.6052 | 25.48% | 28.64% | 23553 | 11318 |
| `W_interest_graph_recall` | 1894 | 0.9312 | 1.0290 | 9.51% | 9.69% | 25129 | 6949 |
| `W_false_sharing` | 2781 | 17.6935 | 16.3142 | 8.46% | 18.58% | 23467 | 18042 |
| `W_ads_ctr` | 2538 | 0.9230 | 0.9999 | 7.70% | 17.60% | 23755 | 12817 |

### c16 seedB

| workload | windows | pred | ROI | pVr | win MAPE | uops/s | macro/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W_phased_mix` | 660 | 2.4744 | 6.7535 | 63.36% | 231.05% | 17219 | 10549 |
| `W_ads_ranking_proxy` | 1663 | 0.5235 | 0.8627 | 39.31% | 45.27% | 23995 | 11484 |
| `W_graph_recall_proxy` | 2730 | 0.5407 | 0.5989 | 9.72% | 31.58% | 24356 | 10556 |
| `W_ads_ctr` | 2500 | 0.9906 | 1.0956 | 9.58% | 26.73% | 23449 | 12652 |
| `W_interest_graph_recall` | 1895 | 0.9890 | 1.0913 | 9.37% | 11.25% | 26408 | 7296 |

### c32 seedB

| workload | windows | pred | ROI | pVr | win MAPE | uops/s | macro/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| `W_chase_dram` | 2534 | 3.3372 | 21.2528 | 84.30% | 38.51% | 22308 | 8839 |
| `W_phased_mix` | 1038 | 3.7016 | 15.5661 | 76.22% | 274.40% | 17426 | 10671 |
| `W_stream` | 2803 | 1.7415 | 5.3362 | 67.37% | 63.36% | 20792 | 12070 |
| `W_false_sharing` | 2664 | 36.0098 | 63.0922 | 42.93% | 41.45% | 22428 | 17253 |
| `W_graph_recall_proxy` | 2487 | 0.6539 | 1.1187 | 41.55% | 61.33% | 22291 | 9623 |

## 与 v9 已知结果对照

这里只对同 seedB 且已有 v9 文档的 c04/c08 做参考，不能作为严格消融，因为 v11 同时改变了 base model、timing schema、attention features、side features 和训练数据。

| 核数 | v9 mean pVr | v11 mean pVr | v9 median pVr | v11 median pVr | 判断 |
|---:|---:|---:|---:|---:|---|
| c04 seedB | 4.06% | 5.71% | 2.88% | 4.13% | v11 轻度退化 |
| c08 seedB | 5.53% | 7.80% | 3.14% | 5.33% | v11 退化 |

主要差异：

- v11 0.6B 的吞吐和训练成本更低，但精度没有超过 v9。
- v11 在 c08 的 `W_false_sharing` 不再是最大问题，pVr 为 `8.46%`；但 `W_phased_mix` 和 `W_ads_ranking_proxy` 成为主要误差。
- c16/c32 显示高核数下的 phase/memory pressure 外推仍不足，尤其 c32 不能视作部署可接受结果。

## PMU 结果

下表为各 PMU 的 pred_vs_roi pVr，单位为百分比。

| 核数 | PMU | mean | median | max |
|---:|---|---:|---:|---:|
| c04 | `cpi_uop` | 5.71 | 4.13 | 26.47 |
| c04 | `branch_miss` | 93.05 | 32.93 | 871.84 |
| c04 | `l1d_ld_miss` | 2598.44 | 27.38 | 38618.46 |
| c04 | `l1d_st_miss` | 288.67 | 20.00 | 3563.60 |
| c04 | `l2_ld_miss` | 402.58 | 44.68 | 3186.19 |
| c04 | `l2_st_miss` | 395.37 | 27.88 | 4431.21 |
| c04 | `llc_miss` | 136.43 | 36.09 | 911.97 |
| c04 | `dtlb_miss` | 14.23 | 13.08 | 30.00 |
| c08 | `cpi_uop` | 7.80 | 5.33 | 38.95 |
| c08 | `branch_miss` | 69.91 | 13.66 | 380.37 |
| c08 | `l1d_ld_miss` | 207.34 | 16.58 | 2465.57 |
| c08 | `l1d_st_miss` | 263.69 | 14.00 | 3755.17 |
| c08 | `l2_ld_miss` | 174.98 | 30.70 | 1255.12 |
| c08 | `l2_st_miss` | 524.43 | 51.33 | 5288.19 |
| c08 | `llc_miss` | 746.41 | 39.01 | 9878.57 |
| c08 | `dtlb_miss` | 26.35 | 12.82 | 225.92 |
| c16 | `cpi_uop` | 11.26 | 7.79 | 63.36 |
| c16 | `branch_miss` | 73.57 | 17.41 | 419.83 |
| c16 | `l1d_ld_miss` | 9100.83 | 17.48 | 142367.60 |
| c16 | `l1d_st_miss` | 264.49 | 25.66 | 3560.66 |
| c16 | `l2_ld_miss` | 148.95 | 19.14 | 930.76 |
| c16 | `l2_st_miss` | 658.29 | 48.38 | 6158.55 |
| c16 | `llc_miss` | 1895.64 | 40.07 | 25883.54 |
| c16 | `dtlb_miss` | 17.15 | 16.77 | 63.87 |
| c32 | `cpi_uop` | 28.17 | 20.93 | 84.30 |
| c32 | `branch_miss` | 130.84 | 30.37 | 963.84 |
| c32 | `l1d_ld_miss` | 4630.80 | 29.41 | 65281.05 |
| c32 | `l1d_st_miss` | 31.59 | 18.66 | 117.97 |
| c32 | `l2_ld_miss` | 1631.57 | 61.49 | 11995.95 |
| c32 | `l2_st_miss` | 2837.54 | 49.12 | 36867.56 |
| c32 | `llc_miss` | 990.13 | 47.90 | 11892.71 |
| c32 | `dtlb_miss` | 22.11 | 16.87 | 98.21 |

解释：

- `cpi_uop` 与 CPI 汇总一致，是当前唯一可作为主指标的 PMU label。
- `dtlb_miss` 的 median 相对稳定，c04/c08/c16/c32 都在 `12.82% - 16.87%`。
- L1/L2/LLC count 的 mean/max 经常极端，主要是低计数分母和窗口级 MAPE 被放大；这些指标要继续保留绝对误差、global count error 或低分母过滤视角。
- PMU count 头暂时不应决定模型是否上线。

## 推理吞吐

| 核数 | mean uops/s | median uops/s | mean macro/s | median macro/s | 8-GPU top8 uops/s | 8-GPU top8 macro/s |
|---:|---:|---:|---:|---:|---:|---:|
| c04 | 20244.5 | 19808.0 | 11243.9 | 11173.8 | 175049.0 | 111516.2 |
| c08 | 24624.5 | 24680.0 | 13632.2 | 13476.1 | 209885.0 | 133895.1 |
| c16 | 24940.9 | 25411.0 | 13796.6 | 13404.6 | 214172.0 | 137418.2 |
| c32 | 21562.5 | 22301.0 | 11915.8 | 11756.3 | 186678.0 | 120511.3 |

该表是 per-workload 推理循环吞吐统计和 8 GPU 粗略并行容量估计，不等价于严格 end-to-end wall throughput。end-to-end 还会包含模型加载、cache 读取、进程调度和 straggler。

## 当前判断

1. 这版 ckpt 可作为 v11 timing schema 的第一版完整结果，但不是当前最佳精度 ckpt。
2. c04/c08/c16 的 CPI 主指标可用，但相比 v9 已知 c04/c08 有退化。
3. c32 外推不可接受，不能用这版直接支撑 32 核部署精度结论。
4. 下一轮优先级应是：
   - 补 c32 训练覆盖或至少加入 c32 seedA 训练窗口；
   - 专门处理 `W_phased_mix` 的时间切窗/phase label；
   - 继续增强 `W_ads_ranking_proxy` 的跨核 memory pressure 表征；
   - 对 PMU count 头加入更合理的 loss weighting、低分母处理和 global-count 验收口径。
