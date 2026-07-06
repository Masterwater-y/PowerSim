# v25a seedB c04/c08/c16 部署侧验证总结

## 1. 状态

训练与部署侧验证均已完成。

训练产物：

- `ckpt/v25a_tiny_transformer_8l320_8gpu_8000/`
- `logs/train_v25a.nohup.log`

训练结束状态：

```text
[DONE] steps=8000 run_steps=8000 step_offset=0 world_size=8 best_val_loss=0.0082
[WALL] total_time=16646.5s (2080.8 ms/step)
[THROUGHPUT-avg] 30.8 samp/s, 100654 tok/s (global over 8 GPUs)
```

验证产物：

- c04/c16: `logs/v25a_seedB_c04_c16_full_20260706_234745/`
- c08: `logs/v25a_seedB_c08_full_20260706_232436/`

验证完成状态：

```text
c04+c16: complete done=34 fails=0
c08:     complete done=17 fails=0
```

注：`v25a_seedB_c16_remainder_*` 是重复补跑目录；主验证目录
`logs/v25a_seedB_c04_c16_full_20260706_234745/` 已包含完整 c16 结果。

## 2. 训练收敛

验证集 loss 从 step 500 到 step 8000 持续下降，后期进入平台区：

| step | val_loss |
|---:|---:|
| 500 | 0.0421 |
| 1000 | 0.0188 |
| 1500 | 0.0162 |
| 2000 | 0.0129 |
| 2500 | 0.0129 |
| 3000 | 0.0124 |
| 3500 | 0.0105 |
| 4000 | 0.0106 |
| 4500 | 0.0095 |
| 5000 | 0.0088 |
| 5500 | 0.0088 |
| 6000 | 0.0083 |
| 6500 | 0.0084 |
| 7000 | 0.0082 |
| 7500 | 0.0082 |
| 8000 | 0.0082 |

判断：

- `500 -> 2000` 快速收敛。
- `2000 -> 6000` 缓慢下降。
- `6000 -> 8000` 基本平台。
- `pred_std` 后期多数接近 `label_std`，未见明显核间塌缩。
- `grad_norm` 后期在小范围内波动，未见发散。

## 3. 部署侧核心指标

指标说明：

- `global pVr`: 全 workload 聚合后 `pred_vs_roi_cpi_uop`。
- `mean/median workload pVr`: 先按 workload 算 `pred_vs_roi_cpi_uop`，再做统计。
- `win MAPE`: per-window `cpi_uop` MAPE，容易被小分母窗口放大。

| core | workloads | windows | global pVr | mean workload pVr | median workload pVr | p90 workload pVr | max workload pVr | median win MAPE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| c04 | 17 | 39115 | 5.27% | 5.20% | 5.34% | 7.87% | 10.36% | 13.93% |
| c08 | 17 | 31871 | 1.27% | 7.79% | 5.46% | 14.70% | 31.34% | 15.16% |
| c16 | 17 | 27887 | 9.24% | 11.02% | 6.20% | 18.98% | 65.60% | 14.62% |

总体判断：

- c04 表现稳定，没有特别大的 workload outlier。
- c08 全局聚合 CPI 很准，但存在 `W_phased_mix` 和 `W_ads_ranking_proxy` 两个明显 outlier。
- c16 全局误差偏高，主要由 `W_phased_mix` 和 `W_ads_ranking_proxy` 拉高。

## 4. 性能指标

| core | avg forward/window | avg total/window | avg uops/s |
|---|---:|---:|---:|
| c04 | 19.8 ms | 35.4 ms | 42.6k |
| c08 | 29.7 ms | 78.2 ms | 54.8k |
| c16 | 39.7 ms | 144.7 ms | 63.4k |

说明：

- forward 时间随 core count 增加，但吞吐按 uops/s 仍上升。
- total/window 中包含 trace window 构建、token encode、tensor 准备和 planner 更新，不只是模型 forward。

## 5. Workload 明细

### c04

| workload | pred | roi | pVr | win MAPE |
|---|---:|---:|---:|---:|
| W_branch_storm | 0.3939 | 0.4394 | 10.36% | 24.48% |
| W_stream | 1.3842 | 1.5234 | 9.14% | 18.61% |
| W_feed_ranking | 0.7335 | 0.7889 | 7.03% | 4.20% |
| W_phased_mix | 0.6466 | 0.6947 | 6.92% | 11.00% |
| W_chase_dram | 2.6737 | 2.8709 | 6.87% | 9.43% |
| W_indirect | 0.8362 | 0.8965 | 6.73% | 15.00% |
| W_fp_compute_dense | 0.5860 | 0.6228 | 5.90% | 7.97% |
| W_ads_ctr | 0.8627 | 0.9138 | 5.60% | 15.29% |
| W_int_div | 0.7035 | 0.7432 | 5.34% | 12.82% |
| W_interest_graph_recall | 0.9408 | 0.9906 | 5.03% | 13.93% |
| W_false_sharing | 6.8907 | 7.2284 | 4.67% | 15.14% |
| W_search_index_proxy | 0.4223 | 0.4041 | 4.50% | 42.13% |
| W_fp_lite | 0.5934 | 0.6167 | 3.78% | 10.11% |
| W_mlp_light | 0.4947 | 0.5080 | 2.62% | 3.49% |
| W_compute_int | 0.4107 | 0.4183 | 1.81% | 1.74% |
| W_ads_ranking_proxy | 0.4323 | 0.4270 | 1.24% | 44.45% |
| W_graph_recall_proxy | 0.4810 | 0.4849 | 0.81% | 24.28% |

### c08

| workload | pred | roi | pVr | win MAPE |
|---|---:|---:|---:|---:|
| W_phased_mix | 0.9391 | 0.7150 | 31.34% | 38.00% |
| W_ads_ranking_proxy | 0.4869 | 0.6052 | 19.54% | 276.62% |
| W_branch_storm | 0.3847 | 0.4346 | 11.48% | 24.71% |
| W_indirect | 0.8196 | 0.8966 | 8.58% | 15.16% |
| W_feed_ranking | 0.7645 | 0.8254 | 7.38% | 4.66% |
| W_fp_compute_dense | 0.6060 | 0.6537 | 7.30% | 7.96% |
| W_stream | 1.4321 | 1.5442 | 7.26% | 16.92% |
| W_interest_graph_recall | 0.9621 | 1.0290 | 6.50% | 16.33% |
| W_chase_dram | 2.7488 | 2.9076 | 5.46% | 10.97% |
| W_fp_lite | 0.6313 | 0.6675 | 5.42% | 11.10% |
| W_ads_ctr | 0.9494 | 0.9999 | 5.06% | 15.40% |
| W_int_div | 0.7068 | 0.7414 | 4.67% | 13.11% |
| W_search_index_proxy | 0.4065 | 0.3922 | 3.63% | 42.29% |
| W_mlp_light | 0.4897 | 0.5080 | 3.60% | 4.49% |
| W_compute_int | 0.4093 | 0.4183 | 2.15% | 1.93% |
| W_graph_recall_proxy | 0.4874 | 0.4949 | 1.52% | 24.12% |
| W_false_sharing | 16.5607 | 16.3142 | 1.51% | 10.14% |

### c16

| workload | pred | roi | pVr | win MAPE |
|---|---:|---:|---:|---:|
| W_phased_mix | 2.3234 | 6.7535 | 65.60% | 182.10% |
| W_ads_ranking_proxy | 0.5994 | 0.8627 | 30.51% | 438.30% |
| W_branch_storm | 0.3853 | 0.4344 | 11.29% | 23.56% |
| W_ads_ctr | 0.9946 | 1.0956 | 9.22% | 16.37% |
| W_stream | 1.5944 | 1.7419 | 8.47% | 18.20% |
| W_interest_graph_recall | 1.0057 | 1.0913 | 7.84% | 17.82% |
| W_indirect | 0.8285 | 0.8960 | 7.53% | 14.62% |
| W_search_index_proxy | 0.4150 | 0.3870 | 7.23% | 45.86% |
| W_feed_ranking | 0.8467 | 0.9026 | 6.20% | 4.88% |
| W_graph_recall_proxy | 0.5636 | 0.5989 | 5.90% | 56.55% |
| W_chase_dram | 3.0963 | 3.2879 | 5.82% | 12.92% |
| W_fp_compute_dense | 0.6801 | 0.7215 | 5.74% | 9.10% |
| W_fp_lite | 0.6628 | 0.6988 | 5.15% | 11.73% |
| W_mlp_light | 0.4873 | 0.5081 | 4.10% | 5.19% |
| W_false_sharing | 30.4256 | 29.7250 | 2.36% | 13.71% |
| W_int_div | 0.7242 | 0.7407 | 2.22% | 12.94% |
| W_compute_int | 0.4099 | 0.4186 | 2.08% | 2.83% |

## 6. 主要发现

### 6.1 自训 tiny Transformer 在 c04/c08 上有效

c04 和 c08 的全局 CPI 误差分别为 5.27% 和 1.27%，说明 v25a 从零训练 backbone
不是只在训练 loss 上有效，部署侧也能形成可用预测。

### 6.2 c16 存在明显 workload 风险

c16 全局 pVr 为 9.24%，主要被两个 workload 拉高：

- `W_phased_mix`: 65.60%
- `W_ads_ranking_proxy`: 30.51%

其中 `W_phased_mix` 在 c08 已经是最大 outlier，到了 c16 进一步恶化。这提示
v25a 对 phase 切换、长时序状态或跨核动态负载变化的泛化不足。

### 6.3 逐窗口误差仍明显高于全局聚合误差

各 core 的 median per-window MAPE 大约在 14%-15%。因此当前模型更适合用作
ROI/window 聚合级预测；如果后续要依赖逐窗口精确控制，还需要继续改善 planner
和 per-window 稳定性。

### 6.4 辅助 PMU 不是当前结论主依据

miss count 的 per-window MAPE 经常被零/小分母窗口放大，当前总结以 CPI 为主。
辅助 PMU 可用于诊断，但不建议作为 v25a 第一版路线判断的主指标。

## 7. 和 c08 v22 参考的初步对比

用现有同口径 c08 `v22_fixed_latest_c08_hidden_diag_20260706_112349/label/run_logs`
作为参考，v25a 在 c08 的全局指标明显更好：

| model | global pVr | mean workload pVr | median workload pVr | max workload pVr |
|---|---:|---:|---:|---:|
| v25a tiny | 1.27% | 7.79% | 5.46% | 31.34% |
| v22 label reference | 16.54% | 14.02% | 10.69% | 30.17% |

这不是最终全局结论，因为 c04/c16 还需要补齐严格同口径 v22 reference；但 c08
至少说明 v25a 自训 backbone 有竞争力，不是明显弱于 Qwen+LoRA。

## 8. 下一步建议

1. 对 `W_phased_mix` 做专项诊断，优先检查 c08/c16 下预测窗口演化、phase 边界、
   `planner_state_source=pred` 与 `label` 的差异。
2. 对 `W_ads_ranking_proxy` 做窗口级误差拆分。该 workload 的全局 pVr 和
   win MAPE 都高，可能存在少量窗口主导的偏差。
3. 补跑 c04/c16 的 v22 同口径 reference，完成 v25a vs v22 的完整决策表。
4. 若重点追求 c16 稳定性，优先考虑 v25b：保持自训 backbone，加入针对 phase
   和 high-spread 窗口的训练/损失改造，而不是只延长当前 8k 训练。

