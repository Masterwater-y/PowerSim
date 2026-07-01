# v13 tail-side Qwen3-0.6B step8000 已完成评估结果

日期：2026-07-01

## 实验配置

| 项 | 值 |
|---|---|
| base model | `Qwen/Qwen3-0.6B-Base` |
| checkpoint | `ckpt/v13_tail_side_qwen3_0p6b_c01_c04_c08_c16_8000` |
| 权重 | `head_best.pt` + `lora_best` |
| 主要改动 | Tail-aware CPI loss + Gated SideMLP |
| 训练数据 | `data/windows_v12_summary_tq_train600_seedA_c01_c04_c08_c16/windows.jsonl` |
| train/eval max len | `32768 / 32768` |
| eval raw | `data/raw_trace_pool/activecore_eval/c{04,08,16,32}_seedB_infer17` |
| eval 脚本 | `scripts/run_v13_eval_sweep.sh` -> `scripts/eval_parallel.sh` -> `eval/eval_quota_cycles.py` |

训练已正常完成：

| 指标 | 值 |
|---|---:|
| steps | `8000` |
| best step | `8000` |
| best val loss | `-31.2978` |
| total train time | `11971.7s` |
| avg step time | `1496.5 ms/step` |
| avg throughput | `5.3 samp/s`, `25390 tok/s` |

截至本文档生成时，`c04/c08/c16` 已完成，`c32` 正在运行，未纳入正式统计。

| 核数 | eval logdir | 状态 |
|---:|---|---|
| c04 | `logs/eval_parallel_v13_qwen3_0p6b_step8000_c04_seedB_full_ctx32768_20260630_231645` | 完成 |
| c08 | `logs/eval_parallel_v13_qwen3_0p6b_step8000_c08_seedB_full_ctx32768_20260630_232931` | 完成 |
| c16 | `logs/eval_parallel_v13_qwen3_0p6b_step8000_c16_seedB_full_ctx32768_20260630_234841` | 完成 |
| c32 | `logs/eval_parallel_v13_qwen3_0p6b_step8000_c32_seedB_full_ctx32768_20260701_002301` | 运行中 |

注意：日志中的 `dt=8000cyc` 是兼容旧参数的显示字段。当前部署侧 planner 实际走 min-uop tail-align；本轮默认 `seed_n=256`、`nmin=256`、`nmin_floor_min=128`。

## 结论摘要

v13 在 c16 上相比 v11/v12 有改善，但 c04/c08 没有整体变好。尤其 c08 的 `W_false_sharing` 被明显拉低，是当前已完成结果里最大的异常点。

| eval set | workloads | CPI mean err | CPI median err | CPI max err | max workload | cycle signed err | avg uops/s/GPU | avg forward/window | avg total/window |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|
| v13 0.6B c04 seedB | 17 | 7.87% | 5.57% | 28.73% | W_false_sharing | -14.26% | 14,545 | 60.4 ms | 76.1 ms |
| v13 0.6B c08 seedB | 17 | 11.00% | 6.52% | 64.94% | W_false_sharing | -38.55% | 18,475 | 97.1 ms | 128.3 ms |
| v13 0.6B c16 seedB | 17 | 10.92% | 7.55% | 38.34% | W_ads_ranking_proxy | -24.02% | 20,199 | 209.4 ms | 278.0 ms |

与已有 v11/v12 对比：

| eval set | v11 0.6B mean/median/max | v12 4B mean/median/max | v13 0.6B mean/median/max | 判断 |
|---|---:|---:|---:|---|
| c04 seedB | 5.71 / 4.13 / 26.47 | - | 7.87 / 5.57 / 28.73 | v13 退化 |
| c08 seedB | 7.80 / 5.33 / 38.95 | 10.10 / 9.46 / 30.33 | 11.00 / 6.52 / 64.94 | mean/max 退化，median 好于 v12 |
| c16 seedB | 11.26 / 7.79 / 63.36 | 14.99 / 10.19 / 55.44 | 10.92 / 7.55 / 38.34 | v13 明显优于 v12，略优于 v11 |

主要观察：

- `W_ads_ranking_proxy` 仍然是稳定低估，且随核心数增加低估更明显：c04 `22.59%`，c08 `34.23%`，c16 `38.34%`。
- `W_false_sharing` 在 c08 严重退化：pred `5.7202` vs ROI `16.3142`，误差 `64.94%`。c16 反而回到 `29.46%`，说明这不是简单的核心数单调问题。
- `W_phased_mix` 在 c04/c08 很准，但 c16 仍低估 `32.00%`。c16 的 per-window MAPE 极高，主要受阶段切换和低分母窗口影响。
- `W_chase_dram`、`W_stream` 在 c04/c08/c16 已完成范围内相对稳定，没有出现 v11 c32 那种外推崩溃。
- v13 吞吐低于 v11 0.6B 旧结果，当前每 workload/GPU 平均约 `14.5K-20.2K uops/s`。

## CPI 明细

### c04 seedB

| workload | windows | pred CPI/uop | ROI CPI/uop | err vs ROI | win MAPE | uops/s | forward/window |
|---|---:|---:|---:|---:|---:|---:|---:|
| W_false_sharing | 2839 | 5.1520 | 7.2284 | 28.73% | 55.79% | 13481 | 63.7 ms |
| W_ads_ranking_proxy | 3647 | 0.3305 | 0.4270 | 22.59% | 14.36% | 14475 | 57.6 ms |
| W_stream | 2864 | 1.3326 | 1.5234 | 12.52% | 15.86% | 14243 | 57.7 ms |
| W_fp_compute_dense | 2149 | 0.5665 | 0.6228 | 9.04% | 7.13% | 14263 | 62.7 ms |
| W_feed_ranking | 3396 | 0.7221 | 0.7889 | 8.47% | 9.71% | 14860 | 57.1 ms |
| W_interest_graph_recall | 3150 | 0.9207 | 0.9906 | 7.06% | 10.18% | 14775 | 57.7 ms |
| W_chase_dram | 3124 | 2.6739 | 2.8709 | 6.86% | 7.59% | 14801 | 56.0 ms |
| W_graph_recall_proxy | 2354 | 0.4517 | 0.4849 | 6.84% | 15.85% | 13962 | 83.3 ms |
| W_fp_lite | 2729 | 0.5823 | 0.6167 | 5.57% | 4.64% | 14012 | 59.9 ms |
| W_int_div | 2750 | 0.7027 | 0.7432 | 5.46% | 7.64% | 14876 | 54.7 ms |
| W_ads_ctr | 1852 | 0.8663 | 0.9138 | 5.21% | 15.10% | 14382 | 63.7 ms |
| W_branch_storm | 3544 | 0.4199 | 0.4394 | 4.44% | 13.69% | 14697 | 55.6 ms |
| W_compute_int | 2964 | 0.4043 | 0.4183 | 3.34% | 2.92% | 15089 | 54.2 ms |
| W_search_index_proxy | 2485 | 0.3914 | 0.4041 | 3.16% | 26.77% | 14834 | 70.1 ms |
| W_phased_mix | 2947 | 0.7069 | 0.6947 | 1.76% | 22.70% | 14563 | 63.8 ms |
| W_mlp_light | 3102 | 0.4993 | 0.5080 | 1.71% | 1.58% | 15026 | 53.8 ms |
| W_indirect | 3005 | 0.8870 | 0.8965 | 1.06% | 8.32% | 14928 | 55.0 ms |

### c08 seedB

| workload | windows | pred CPI/uop | ROI CPI/uop | err vs ROI | win MAPE | uops/s | forward/window |
|---|---:|---:|---:|---:|---:|---:|---:|
| W_false_sharing | 2701 | 5.7202 | 16.3142 | 64.94% | 51.43% | 15848 | 108.8 ms |
| W_ads_ranking_proxy | 2481 | 0.3980 | 0.6052 | 34.23% | 32.08% | 18168 | 131.1 ms |
| W_interest_graph_recall | 1893 | 0.9222 | 1.0290 | 10.38% | 11.52% | 18590 | 89.1 ms |
| W_feed_ranking | 1856 | 0.7413 | 0.8254 | 10.19% | 16.08% | 18768 | 96.8 ms |
| W_stream | 2816 | 1.3946 | 1.5442 | 9.69% | 14.45% | 17968 | 92.1 ms |
| W_graph_recall_proxy | 2544 | 0.4479 | 0.4949 | 9.49% | 23.32% | 18043 | 121.7 ms |
| W_search_index_proxy | 1966 | 0.3601 | 0.3922 | 8.20% | 37.63% | 17815 | 144.3 ms |
| W_fp_compute_dense | 1976 | 0.6006 | 0.6537 | 8.13% | 11.73% | 18212 | 102.4 ms |
| W_fp_lite | 2096 | 0.6239 | 0.6675 | 6.52% | 7.42% | 18057 | 93.9 ms |
| W_chase_dram | 3037 | 2.7393 | 2.9076 | 5.79% | 10.77% | 19052 | 85.7 ms |
| W_compute_int | 2957 | 0.3952 | 0.4183 | 5.53% | 5.19% | 19630 | 80.6 ms |
| W_ads_ctr | 2660 | 0.9659 | 0.9999 | 3.40% | 17.11% | 18645 | 89.2 ms |
| W_int_div | 3525 | 0.7190 | 0.7414 | 3.03% | 7.05% | 19219 | 81.0 ms |
| W_branch_storm | 2684 | 0.4235 | 0.4346 | 2.55% | 14.81% | 19267 | 82.3 ms |
| W_indirect | 2990 | 0.8800 | 0.8966 | 1.84% | 9.27% | 19001 | 82.2 ms |
| W_phased_mix | 2526 | 0.7027 | 0.7150 | 1.72% | 20.83% | 18373 | 90.9 ms |
| W_mlp_light | 2696 | 0.5010 | 0.5080 | 1.37% | 1.77% | 19415 | 79.3 ms |

### c16 seedB

| workload | windows | pred CPI/uop | ROI CPI/uop | err vs ROI | win MAPE | uops/s | forward/window |
|---|---:|---:|---:|---:|---:|---:|---:|
| W_ads_ranking_proxy | 1573 | 0.5319 | 0.8627 | 38.34% | 57.81% | 20139 | 373.1 ms |
| W_phased_mix | 1068 | 4.5923 | 6.7535 | 32.00% | 1276.67% | 18431 | 456.2 ms |
| W_false_sharing | 2463 | 20.9677 | 29.7250 | 29.46% | 34.43% | 19700 | 179.8 ms |
| W_graph_recall_proxy | 2551 | 0.4982 | 0.5989 | 16.81% | 44.55% | 19732 | 221.6 ms |
| W_stream | 2819 | 1.5739 | 1.7419 | 9.65% | 17.01% | 19827 | 163.0 ms |
| W_search_index_proxy | 1223 | 0.3507 | 0.3870 | 9.38% | 49.25% | 19492 | 385.0 ms |
| W_interest_graph_recall | 1852 | 0.9911 | 1.0913 | 9.18% | 13.38% | 20135 | 162.3 ms |
| W_fp_compute_dense | 2132 | 0.6632 | 0.7215 | 8.09% | 189.66% | 16804 | 215.9 ms |
| W_chase_dram | 2904 | 3.0398 | 3.2879 | 7.55% | 13.17% | 20773 | 159.6 ms |
| W_feed_ranking | 1775 | 0.8493 | 0.9026 | 5.91% | 29.03% | 20605 | 182.1 ms |
| W_ads_ctr | 2592 | 1.0348 | 1.0956 | 5.54% | 24.58% | 19092 | 176.2 ms |
| W_fp_lite | 2035 | 0.6647 | 0.6988 | 4.88% | 8.26% | 20228 | 167.3 ms |
| W_compute_int | 2922 | 0.3988 | 0.4186 | 4.73% | 4.39% | 22346 | 142.6 ms |
| W_branch_storm | 2641 | 0.4267 | 0.4344 | 1.77% | 15.44% | 21473 | 145.4 ms |
| W_mlp_light | 2683 | 0.5010 | 0.5081 | 1.40% | 2.05% | 21115 | 139.1 ms |
| W_indirect | 2944 | 0.8901 | 0.8960 | 0.66% | 8.80% | 21698 | 147.1 ms |
| W_int_div | 2923 | 0.7434 | 0.7407 | 0.37% | 7.15% | 21789 | 144.0 ms |

## 重点 workload 缩放

### W_ads_ranking_proxy

| 核数 | pred | ROI | err |
|---:|---:|---:|---:|
| c04 | 0.3305 | 0.4270 | 22.59% |
| c08 | 0.3980 | 0.6052 | 34.23% |
| c16 | 0.5319 | 0.8627 | 38.34% |

判断：仍是稳定低估。真实 ROI 从 c04 到 c16 放大约 `2.02x`，模型只放大约 `1.61x`。这说明 v13 仍没有充分建模并发随机 gather 对共享 LLC/DRAM/MSHR/队列压力的非线性放大。

### W_false_sharing

| 核数 | pred | ROI | err |
|---:|---:|---:|---:|
| c04 | 5.1520 | 7.2284 | 28.73% |
| c08 | 5.7202 | 16.3142 | 64.94% |
| c16 | 20.9677 | 29.7250 | 29.46% |

判断：c08 异常低估最严重。c16 相对 c08 反而恢复，说明当前模型不是简单缺少 high-CPI 标签，而是某些核心数/窗口分布下的 side/tail 信号映射不稳定。

### W_phased_mix

| 核数 | pred | ROI | err |
|---:|---:|---:|---:|
| c04 | 0.7069 | 0.6947 | 1.76% |
| c08 | 0.7027 | 0.7150 | 1.72% |
| c16 | 4.5923 | 6.7535 | 32.00% |

判断：c04/c08 很好，但 c16 仍低估。per-window MAPE 在 c16 极高，不宜单独作为结论，aggregate CPI 更可靠。

## PMU median 误差

下表为 aggregate pred-vs-ROI 相对误差的 median。PMU count 的 mean 仍容易被低 label workload 拉爆，因此这里不列 mean。

| 核数 | branch_miss | l1d_ld_miss | l1d_st_miss | l2_ld_miss | l2_st_miss | llc_miss | dtlb_miss |
|---:|---:|---:|---:|---:|---:|---:|---:|
| c04 | 14.48% | 21.92% | 25.94% | 40.52% | 37.39% | 17.31% | 7.24% |
| c08 | 15.75% | 22.06% | 33.95% | 36.80% | 37.65% | 33.64% | 7.32% |
| c16 | 24.44% | 36.52% | 33.08% | 59.76% | 41.89% | 25.95% | 8.06% |

观察：

- `dtlb_miss` median 仍相对稳定，约 `7-8%`。
- L2/L1 miss 头仍不足以作为主验收指标，尤其 c16 的 `l2_ld_miss` median 达到 `59.76%`。
- PMU 目前更适合辅助诊断，不应替代 CPI/cycle 主指标。

## 当前判断

1. v13 的 `Tail-aware CPI loss + Gated SideMLP` 对 c16 有正向收益，但没有形成稳定的 c04/c08/c16 全局提升。
2. `W_ads_ranking_proxy` 没有被修好，根因更像共享内存系统排队压力缺少显式可观测特征，而不是 seed 或训练步数。
3. `W_false_sharing` 在 c08 出现严重低估，需要优先复查 side gate 与 tail loss 是否在中高 CPI 样本上产生了压低 bias。
4. 继续只加 step 不太可能解决 `ads_ranking_proxy` 和 `false_sharing`，下一步更应做 targeted ablation：
   - 固定 v13 ckpt，比较 `seed_n/nmin=128/200/256` 对 `W_ads_ranking_proxy`、`W_false_sharing` 的影响。
   - 对比关闭 tail-aware loss 或关闭 Gated SideMLP 的小训练，确认是哪一项引入 c04/c08 退化。
   - 增加 explicit memory contention proxy，例如 `ncore * random_load_rate`、`ncore * working_set_lines/pages`、估计 outstanding miss/MSHR pressure。
5. 等 `c32` 完成后，需要追加 c32 到本文档，并单独判断未训练核心数外推是否改善。
