# v14 head-only Qwen3-0.6B step7000 评估结果

日期：2026-07-01

## 实验配置

| 项 | 值 |
|---|---|
| base model | `Qwen/Qwen3-0.6B-Base` |
| checkpoint | `ckpt/v14_headonly_qwen3_0p6b_c01_c04_c08_c16_8000` |
| 权重 | `head_best.pt` + `lora_best` |
| best step | `7000` |
| best val loss | `-10.6938` |
| latest step | `7500`，val loss `-10.4545` |
| 训练状态 | 训练在 step `7520` 被 `SIGHUP` 中断，best 已保存 |
| active loss | `cpi_uop,branch_miss` |
| cycles loss | `window` |
| frozen heads | `cache_miss_head, dtlb_head` |
| 训练数据 | `data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl` |
| tensor cache | `data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.maxlen32768.tensor_cache` |
| train/eval max len | `32768 / 32768` |
| eval raw | `data/raw_trace_pool/activecore_eval/c{04,08,16}_seedB_infer17` |
| eval 脚本 | `scripts/run_v13_eval_sweep.sh` -> `scripts/eval_parallel.sh` -> `eval/eval_quota_cycles.py` |

注意：`dt=8000cyc` 仍只是兼容日志字段。当前部署侧 planner 是 min-uop tail-align，默认 `seed_n=256`、`nmin=256`、`nmin_floor_min=128`。

## 结果摘要

| eval set | workloads | CPI mean err | CPI median err | CPI max err | max workload | mean signed err |
|---|---:|---:|---:|---:|---|---:|
| v14 c04 seedB | 17 | 12.41% | 7.95% | 81.88% | `W_false_sharing` | -12.31% |
| v14 c08 seedB | 17 | 10.52% | 10.26% | 36.55% | `W_ads_ranking_proxy` | -9.99% |
| v14 c16 seedB | 17 | 15.18% | 11.25% | 51.36% | `W_phased_mix` | -14.12% |

与 v13 对比：

| eval set | v13 mean/median/max | v14 mean/median/max | 判断 |
|---|---:|---:|---|
| c04 seedB | 7.87 / 5.57 / 28.73 | 12.41 / 7.95 / 81.88 | 明显退化，主要被 `W_false_sharing` 拉高 |
| c08 seedB | 11.00 / 6.52 / 64.94 | 10.52 / 10.26 / 36.55 | mean/max 改善，median 退化 |
| c16 seedB | 10.92 / 7.55 / 38.34 | 15.18 / 11.25 / 51.36 | 明显退化，`W_ads_ranking_proxy` 和 `W_phased_mix` 更差 |

## 主要观察

1. 只保留 CPI/branch/cycles loss 并冻结 cache/dtlb 头，没有带来稳定收益。
2. `W_false_sharing` 在 c08/c16 比 v13 明显改善，但 c04 严重崩坏：pred `1.3096` vs ROI `7.2284`，误差 `81.88%`。
3. `W_ads_ranking_proxy` 仍然随核心数增加持续低估：c04 `20.78%`，c08 `36.55%`，c16 `44.93%`。v14 在 c16 上比 v13 更差。
4. `W_phased_mix` 在 c04/c08 仍好，但 c16 严重低估：pred `3.2853` vs ROI `6.7535`，误差 `51.36%`。
5. 当前偏差总体仍是低估，mean signed err 为负。

## 重点 workload

### W_ads_ranking_proxy

| 核数 | windows | pred CPI/uop | ROI CPI/uop | err vs ROI | win MAPE |
|---:|---:|---:|---:|---:|---:|
| c04 | 3654 | 0.3382 | 0.4270 | 20.78% | 13.96% |
| c08 | 3580 | 0.3839 | 0.6052 | 36.55% | 28.88% |
| c16 | 2774 | 0.4751 | 0.8627 | 44.93% | 44.56% |

判断：仍是稳定低估。ROI 从 c04 到 c16 放大 `2.02x`，预测只放大 `1.40x`。这说明模型没有学到并发随机 gather 的共享系统压力放大。

### W_false_sharing

| 核数 | windows | pred CPI/uop | ROI CPI/uop | err vs ROI | win MAPE |
|---:|---:|---:|---:|---:|---:|
| c04 | 2929 | 1.3096 | 7.2284 | 81.88% | 68.80% |
| c08 | 2636 | 12.1991 | 16.3142 | 25.22% | 23.17% |
| c16 | 2643 | 22.2006 | 29.7250 | 25.31% | 27.63% |

判断：c08/c16 比 v13 好，但 c04 被压得过低。说明 v14 的 head-only loss 不是简单改善 high-CPI tail，而是在不同核心数上产生不稳定映射。

### W_phased_mix

| 核数 | windows | pred CPI/uop | ROI CPI/uop | err vs ROI | win MAPE |
|---:|---:|---:|---:|---:|---:|
| c04 | 3269 | 0.6976 | 0.6947 | 0.42% | 19.73% |
| c08 | 2654 | 0.7153 | 0.7150 | 0.04% | 23.56% |
| c16 | 1932 | 3.2853 | 6.7535 | 51.36% | 422.92% |

判断：c16 出现新的大幅低估，说明阶段性 workload 对当前 free-running planner 和窗口分布更敏感。

## 全 workload CPI 明细

### c04 seedB

| workload | windows | pred | ROI | err | win MAPE |
|---|---:|---:|---:|---:|---:|
| W_ads_ctr | 1900 | 0.7084 | 0.9138 | 22.48% | 12.73% |
| W_ads_ranking_proxy | 3654 | 0.3382 | 0.4270 | 20.78% | 13.96% |
| W_branch_storm | 3536 | 0.4399 | 0.4394 | 0.10% | 13.98% |
| W_chase_dram | 3161 | 2.6427 | 2.8709 | 7.95% | 9.22% |
| W_compute_int | 2984 | 0.4034 | 0.4183 | 3.54% | 3.06% |
| W_false_sharing | 2929 | 1.3096 | 7.2284 | 81.88% | 68.80% |
| W_feed_ranking | 3450 | 0.7341 | 0.7889 | 6.96% | 3.11% |
| W_fp_compute_dense | 2311 | 0.5624 | 0.6228 | 9.70% | 4.81% |
| W_fp_lite | 2769 | 0.5532 | 0.6167 | 10.29% | 7.15% |
| W_graph_recall_proxy | 2886 | 0.4288 | 0.4849 | 11.58% | 19.77% |
| W_indirect | 3020 | 0.8800 | 0.8965 | 1.84% | 8.37% |
| W_int_div | 2784 | 0.7047 | 0.7432 | 5.18% | 7.59% |
| W_interest_graph_recall | 3263 | 0.9087 | 0.9906 | 8.27% | 9.66% |
| W_mlp_light | 3122 | 0.4754 | 0.5080 | 6.42% | 6.12% |
| W_phased_mix | 3269 | 0.6976 | 0.6947 | 0.42% | 19.73% |
| W_search_index_proxy | 2753 | 0.4054 | 0.4041 | 0.32% | 23.82% |
| W_stream | 2864 | 1.3204 | 1.5234 | 13.32% | 17.55% |

### c08 seedB

| workload | windows | pred | ROI | err | win MAPE |
|---|---:|---:|---:|---:|---:|
| W_ads_ctr | 2714 | 0.8785 | 0.9999 | 12.15% | 12.83% |
| W_ads_ranking_proxy | 3580 | 0.3839 | 0.6052 | 36.55% | 28.88% |
| W_branch_storm | 2701 | 0.4428 | 0.4346 | 1.91% | 15.54% |
| W_chase_dram | 3147 | 2.6534 | 2.9076 | 8.74% | 11.21% |
| W_compute_int | 2978 | 0.4064 | 0.4183 | 2.86% | 2.52% |
| W_false_sharing | 2636 | 12.1991 | 16.3142 | 25.22% | 23.17% |
| W_feed_ranking | 2028 | 0.7407 | 0.8254 | 10.26% | 4.53% |
| W_fp_compute_dense | 2291 | 0.5841 | 0.6537 | 10.65% | 7.11% |
| W_fp_lite | 2222 | 0.5892 | 0.6675 | 11.73% | 8.03% |
| W_graph_recall_proxy | 2726 | 0.4356 | 0.4949 | 11.98% | 22.72% |
| W_indirect | 3019 | 0.8798 | 0.8966 | 1.87% | 8.47% |
| W_int_div | 3577 | 0.7250 | 0.7414 | 2.21% | 7.01% |
| W_interest_graph_recall | 1933 | 0.8275 | 1.0290 | 19.58% | 9.92% |
| W_mlp_light | 2704 | 0.4686 | 0.5080 | 7.75% | 7.51% |
| W_phased_mix | 2654 | 0.7153 | 0.7150 | 0.04% | 23.56% |
| W_search_index_proxy | 2683 | 0.4024 | 0.3922 | 2.58% | 26.65% |
| W_stream | 2858 | 1.3476 | 1.5442 | 12.74% | 14.10% |

### c16 seedB

| workload | windows | pred | ROI | err | win MAPE |
|---|---:|---:|---:|---:|---:|
| W_ads_ctr | 2702 | 0.9204 | 1.0956 | 15.99% | 13.36% |
| W_ads_ranking_proxy | 2774 | 0.4751 | 0.8627 | 44.93% | 44.56% |
| W_branch_storm | 2690 | 0.4377 | 0.4344 | 0.78% | 15.03% |
| W_chase_dram | 3102 | 2.9408 | 3.2879 | 10.56% | 14.39% |
| W_compute_int | 2951 | 0.4055 | 0.4186 | 3.12% | 3.18% |
| W_false_sharing | 2643 | 22.2006 | 29.7250 | 25.31% | 27.63% |
| W_feed_ranking | 2035 | 0.8191 | 0.9026 | 9.25% | 10.69% |
| W_fp_compute_dense | 2304 | 0.6404 | 0.7215 | 11.25% | 10.62% |
| W_fp_lite | 2195 | 0.6042 | 0.6988 | 13.54% | 10.15% |
| W_graph_recall_proxy | 1962 | 0.4756 | 0.5989 | 20.59% | 31.40% |
| W_indirect | 3012 | 0.8730 | 0.8960 | 2.56% | 8.56% |
| W_int_div | 2988 | 0.7311 | 0.7407 | 1.29% | 6.98% |
| W_interest_graph_recall | 1949 | 0.9125 | 1.0913 | 16.38% | 11.12% |
| W_mlp_light | 2689 | 0.4660 | 0.5081 | 8.29% | 8.80% |
| W_phased_mix | 1932 | 3.2853 | 6.7535 | 51.36% | 422.92% |
| W_search_index_proxy | 1984 | 0.4191 | 0.3870 | 8.29% | 28.10% |
| W_stream | 2832 | 1.4868 | 1.7419 | 14.65% | 17.00% |

## Planner 诊断假设

当前 `W_ads_ranking_proxy` 的 eval 窗口满足每核 `nmin=256` 左右，但和训练 TQ 的真实时间窗分布不一致：

| 核数 | eval total uops/window | eval per-core uops/window |
|---:|---:|---:|
| c04 | 1051.2 | 262.8 |
| c08 | 2148.2 | 268.5 |
| c16 | 5547.9 | 346.7 |

训练 TQ 不是固定每核 256 uops，而是用真实 commit-time 的 `[T_start,T_end]` 时间窗；慢核决定时间跨度，快核会在同一时间跨度内贡献更多 uops。eval planner 则用模型预测 CPI 递推 `pred_start_cycle`，并按 `nmin=256` 附近做 tail-align。若模型对某 workload 低估 CPI，planner 会维持较短窗口，形成 free-running 闭环偏差。

下一步诊断：新增 `--planner-state-source pred|label`。`label` 模式在验证集用 label CPI 更新 planner 时间线，用来判断误差主要来自 planner 状态累积，还是模型本体/特征已经无法预测 CPI。

