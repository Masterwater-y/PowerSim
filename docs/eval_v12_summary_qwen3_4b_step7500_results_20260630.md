# v12 summary-pack Qwen3-4B step7500 评估结果

日期：2026-06-30

## 实验配置

- 模型：`Qwen/Qwen3-4B`
- checkpoint：`ckpt/v12_summary_qwen3_4b_c01_c04_c08_c16_8000`
- 实际评估权重：`head_best.pt` + `lora_best`，对应 step 7500
- 输入 schema：v12 summary-pack + timing attention/side + split PMU heads
- 训练集：`c01/c04/c08/c16 seedA`，每 workload 约 600 windows
- 推理集：`seedB c08`、`seedB c16`
- 推理上下文：`MAX_LEN=32768`，`TRAIN_MAX_LEN=32768`
- 推理脚本：`scripts/eval_parallel.sh` + `eval/eval_quota_cycles.py`

吞吐口径：

```text
uops/s = workload 进程内已处理的所有 active-core uop 数 / 窗口循环 wall time
```

这个吞吐是每个 workload/GPU 的 aggregate uops/s，不是 per-core uops/s，也不是 transformer token/s。计时不包含模型加载和 parquet 读取，包含切窗、encode、tensor 构造、full-sequence forward、planner update。

## 总览

| eval set | workloads | CPI mean err | CPI median err | CPI max err | avg uops/s/GPU | avg forward/window | avg total/window | forward 占比 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| v12 4B c08 seedB | 17 | 10.10% | 9.46% | 30.33% | 5,879 | 361.8 ms | 391.6 ms | 92.4% |
| v12 4B c16 seedB | 17 | 14.99% | 10.19% | 55.44% | 6,739 | 724.9 ms | 793.6 ms | 91.3% |
| v11 0.6B c08 seedB | 17 | 7.80% | 5.33% | 38.95% | 24,625 | 76.2 ms | 105.1 ms | 72.5% |
| v11 0.6B c16 seedB | 17 | 11.26% | 7.79% | 63.36% | 24,941 | 178.3 ms | 244.5 ms | 72.9% |
| v11 0.6B c32 seedB | 17 | 28.17% | 20.93% | 84.30% | 21,562 | 432.8 ms | 574.4 ms | 75.3% |

结论：

- v12 4B 在 c08/c16 上没有整体优于 v11 0.6B；平均和中位 CPI 误差更高。
- v12 4B 降低了 worst-case：c08 max `38.95% -> 30.33%`，c16 max `63.36% -> 55.44%`。
- `W_phased_mix` 明显改善，但 `W_false_sharing` 和 `W_ads_ranking_proxy` 变差，是当前主要缺陷。
- 4B 推理主要瓶颈是 full-sequence prefill：forward 占总窗口时间超过 91%。
- 4B 单卡吞吐约 5.9K-6.7K aggregate uops/s，明显低于 v11 0.6B 的 24K-25K。

## v12 4B c08 明细

| workload | pred CPI/uop | ROI CPI/uop | err vs ROI | windows | uops/s | forward/window |
|---|---:|---:|---:|---:|---:|---:|
| W_false_sharing | 11.37 | 16.31 | 30.33% | 2748 | 5746 | 349.4 ms |
| W_ads_ranking_proxy | 0.4339 | 0.6052 | 28.30% | 2873 | 6038 | 409.7 ms |
| W_phased_mix | 0.8565 | 0.7150 | 19.79% | 2091 | 5989 | 420.9 ms |
| W_stream | 1.286 | 1.544 | 16.74% | 2675 | 5861 | 360.7 ms |
| W_chase_dram | 2.454 | 2.908 | 15.61% | 3036 | 5819 | 344.0 ms |
| W_ads_ctr | 0.8793 | 0.9999 | 12.07% | 2622 | 5860 | 352.8 ms |
| W_fp_compute_dense | 0.5825 | 0.6537 | 10.90% | 2202 | 5841 | 352.6 ms |
| W_feed_ranking | 0.7466 | 0.8254 | 9.55% | 1956 | 5803 | 359.2 ms |
| W_interest_graph_recall | 0.9316 | 1.029 | 9.46% | 1931 | 5800 | 342.9 ms |
| W_fp_lite | 0.6265 | 0.6675 | 6.13% | 2145 | 5789 | 350.0 ms |
| W_search_index_proxy | 0.4136 | 0.3922 | 5.44% | 2218 | 6186 | 447.1 ms |
| W_indirect | 0.8773 | 0.8966 | 2.15% | 2984 | 5863 | 330.2 ms |
| W_mlp_light | 0.4983 | 0.5080 | 1.90% | 2665 | 5773 | 332.7 ms |
| W_compute_int | 0.4134 | 0.4183 | 1.17% | 2977 | 5821 | 332.7 ms |
| W_branch_storm | 0.4386 | 0.4346 | 0.94% | 2669 | 5825 | 334.1 ms |
| W_graph_recall_proxy | 0.4914 | 0.4949 | 0.70% | 2742 | 6110 | 401.8 ms |
| W_int_div | 0.7381 | 0.7414 | 0.45% | 3549 | 5818 | 330.4 ms |

## v12 4B c16 明细

| workload | pred CPI/uop | ROI CPI/uop | err vs ROI | windows | uops/s | forward/window |
|---|---:|---:|---:|---:|---:|---:|
| W_phased_mix | 3.010 | 6.754 | 55.44% | 825 | 6420 | 1991.2 ms |
| W_ads_ranking_proxy | 0.5040 | 0.8627 | 41.58% | 2093 | 7155 | 937.0 ms |
| W_false_sharing | 17.82 | 29.72 | 40.07% | 2785 | 6607 | 585.8 ms |
| W_chase_dram | 2.683 | 3.288 | 18.39% | 3079 | 6703 | 585.0 ms |
| W_stream | 1.424 | 1.742 | 18.27% | 2702 | 6714 | 613.3 ms |
| W_graph_recall_proxy | 0.5032 | 0.5989 | 15.99% | 2576 | 6913 | 765.1 ms |
| W_ads_ctr | 0.9277 | 1.096 | 15.33% | 2603 | 6715 | 614.8 ms |
| W_fp_compute_dense | 0.6292 | 0.7215 | 12.79% | 2138 | 6637 | 632.3 ms |
| W_interest_graph_recall | 0.9801 | 1.091 | 10.19% | 1911 | 6678 | 596.3 ms |
| W_feed_ranking | 0.8269 | 0.9026 | 8.39% | 1952 | 6702 | 615.3 ms |
| W_search_index_proxy | 0.4108 | 0.3870 | 6.17% | 1720 | 6890 | 910.8 ms |
| W_fp_lite | 0.6567 | 0.6988 | 6.02% | 2085 | 6697 | 612.4 ms |
| W_mlp_light | 0.4946 | 0.5081 | 2.65% | 2611 | 6690 | 579.3 ms |
| W_indirect | 0.8776 | 0.8960 | 2.06% | 2945 | 6781 | 569.8 ms |
| W_int_div | 0.7486 | 0.7407 | 1.07% | 2956 | 6746 | 566.2 ms |
| W_branch_storm | 0.4353 | 0.4344 | 0.23% | 2616 | 6814 | 578.0 ms |
| W_compute_int | 0.4177 | 0.4186 | 0.22% | 2945 | 6703 | 570.1 ms |

## PMU 标签与 PMU 预测

ROI label 和 window label 对 CPI 基本一致，说明部署侧切窗的标签聚合没有明显偏移。PMU count 头仍不稳定，尤其是稀疏事件和低基数事件。下面是 aggregate pred-vs-ROI 相对误差的中位数；mean 会被少数低 label workload 拉得很大。

| PMU key | v12 c08 median err | v12 c16 median err |
|---|---:|---:|
| branch_miss | 15.97% | 11.94% |
| l1d_ld_miss | 32.56% | 30.95% |
| l1d_st_miss | 31.61% | 32.34% |
| l2_ld_miss | 37.11% | 58.08% |
| l2_st_miss | 84.80% | 48.68% |
| llc_miss | 47.50% | 31.11% |
| dtlb_miss | 13.67% | 17.85% |

当前 PMU 的主要问题不是标签对齐，而是 count 头对稀疏事件的尺度校准不足。PMU window MAPE 不适合作为主指标，因为很多窗口 label 为 0 或接近 0，会造成极端比例误差。

## 吞吐分析

v12 4B c08:

```text
avg forward/window = 361.8 ms
avg total/window   = 391.6 ms
forward share      = 92.4%
avg uops/s/GPU     = 5,879
```

v12 4B c16:

```text
avg forward/window = 724.9 ms
avg total/window   = 793.6 ms
forward share      = 91.3%
avg uops/s/GPU     = 6,739
```

因此当前部署侧瓶颈是 full-sequence prefill，而不是 parquet cache、切窗、side tensor、PMU head 或 uop encoder。

和 v11 0.6B 对比：

- c08：`24.6K -> 5.9K uops/s/GPU`，约慢 `4.2x`
- c16：`24.9K -> 6.7K uops/s/GPU`，约慢 `3.7x`

v11 在核心数上升时 aggregate uops/s 接近，是因为每窗总 uop 数随核心数增加，抵消了 forward/window 增长。这个吞吐不是 per-core 吞吐。

## 结论与下一步

1. v12 4B 不是当前最合适的部署模型。它的 prefill 成本太高，吞吐明显低于 0.6B。
2. 4B 可以继续作为 teacher 或上限模型，但部署侧更适合 0.6B/1.7B，并用 4B 蒸馏。
3. `W_false_sharing` 和 `W_ads_ranking_proxy` 仍是需要优先修复的 workload。v12 attention/side timing 与 summary-pack 没有解决这两个负载的 CPI 偏低问题。
4. `W_phased_mix` 在 v12 4B 上相对 v11 有改善，说明 timing/summary 对阶段混合负载有效。
5. 还需要补 c04 和 c32 的 v12 4B 完整推理，才能判断核心数泛化曲线。

