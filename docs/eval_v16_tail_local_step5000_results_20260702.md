# v16 tail-local step5000 seedB 评估结果

日期：2026-07-02

## 1. 实验配置

| 项 | 值 |
|---|---|
| checkpoint | `ckpt/v16_v9core_tail_local_delta_rank_8gpu_8000/step_005000` |
| 训练数据 | `data/windows_v16_v9core_tail_local_all/windows.jsonl` |
| tensor cache | `data/windows_v16_v9core_tail_local_all/windows.maxlen32768.tensor_cache` |
| query placement | `tail_local` |
| 评估集 | `data/raw_trace_pool/activecore_eval/c{04,08,16}_seedB_infer17` |
| max len | `32768` |
| workloads | 17 |

日志目录：

```text
c04: logs/eval_parallel_v16_tail_local_step5000_c04_seedB_full_20260702_155505
c08: logs/eval_parallel_v16_tail_local_step5000_c08_seedB_full_20260702_153626
c16: logs/eval_parallel_v16_tail_local_step5000_c16_seedB_full_20260702_160450
```

说明：这是 step5000 快照，不是 8000 step 最终模型。训练日志显示 step5000 之后 validation loss 仍在下降，因此本结果用于判断 v16 结构方向，不作为最终精度上限。

## 2. 汇总结果

| core | mean pVr | median pVr | max pVr | worst workload |
|---:|---:|---:|---:|---|
| c04 | 5.61% | 3.48% | 14.88% | `W_ads_ranking_proxy` |
| c08 | 6.33% | 4.31% | 34.09% | `W_ads_ranking_proxy` |
| c16 | 10.85% | 3.52% | 71.81% | `W_phased_mix` |

整体判断：

- c04/c08 整体可用，mean/median 都明显低于 v13/v14。
- c16 median 仍低，说明大多数 workload 没崩；mean 被少数 outlier 拉高。
- `W_false_sharing` 已明显修复，c04/c08/c16 分别为 `1.66% / 1.99% / 7.02%`。
- `W_ads_ranking_proxy` 仍随核心数增加低估，c04/c08/c16 分别为 `14.88% / 34.09% / 36.68%`。
- `W_phased_mix` 在 c08 修复，但 c16 严重退化到 `71.81%`，说明 tail-local 还没有解决高核心数 phase / planner 闭环问题。

## 3. 重点 workload

| workload | c04 pVr | c08 pVr | c16 pVr | 结论 |
|---|---:|---:|---:|---|
| `W_ads_ranking_proxy` | 14.88% | 34.09% | 36.68% | 核心数越高越低估，仍未解决 |
| `W_false_sharing` | 1.66% | 1.99% | 7.02% | v16 明显有效 |
| `W_phased_mix` | 4.71% | 4.31% | 71.81% | c04/c08 修复，c16 崩坏 |
| `W_chase_dram` | 12.56% | 11.45% | 5.55% | 低/中核偏低估，c16 反而改善 |
| `W_feed_ranking` | 10.02% | 10.98% | 3.52% | c04/c08 中等低估，c16 改善 |
| `W_stream` | 11.97% | 10.04% | 1.95% | c04/c08 中等低估，c16 改善 |
| `W_compute_int` | 10.32% | 7.60% | 21.36% | c16 异常低估，需要复查 |

## 4. 与已有版本对比

### c08 对比

| 版本 | mean | median | max | worst |
|---|---:|---:|---:|---|
| v9 old c08 | 5.53% | 3.14% | 29.88% | `W_false_sharing` |
| v9 current c08 | 7.80% | 7.90% | 36.53% | `W_ads_ranking_proxy` |
| v15 c08 | 8.64% | 5.00% | 40.10% | `W_phased_mix` |
| v16 c08 | 6.33% | 4.31% | 34.09% | `W_ads_ranking_proxy` |

v16 c08 相比 v15：

| workload | v15 pVr | v16 pVr | 变化 |
|---|---:|---:|---:|
| `W_phased_mix` | 40.10% | 4.31% | -35.80 pp |
| `W_false_sharing` | 19.96% | 1.99% | -17.96 pp |
| `W_ads_ranking_proxy` | 23.28% | 34.09% | +10.80 pp |
| `W_chase_dram` | 3.09% | 11.45% | +8.36 pp |
| `W_compute_int` | 1.26% | 7.60% | +6.34 pp |

结论：

- `tail_local` 修复了 v15 query-segment 导致的 `W_phased_mix` 全局可见性问题。
- `W_false_sharing` 也显著改善，说明 high-CPI coherence 类并非无法学习。
- `W_ads_ranking_proxy` 从 v15 的改善状态退回，说明 local summary token 尚未替代 segment query 对本核快慢识别的帮助。

### 与 v13/v14 量级对比

已有文档中的 c08：

```text
v13 c08: mean 11.00%, median 6.52%, max 64.94%  worst=W_false_sharing
v14 c08: mean 10.52%, median 10.26%, max 36.55% worst=W_ads_ranking_proxy
v16 c08: mean  6.33%, median 4.31%, max 34.09%  worst=W_ads_ranking_proxy
```

v16 在 c08 上明显优于 v13/v14。

c16 上：

```text
v13 c16: mean 10.92%, median 7.55%, max 38.34% worst=W_ads_ranking_proxy
v14 c16: mean 15.18%, median 11.25%, max 51.36% worst=W_phased_mix
v16 c16: mean 10.85%, median 3.52%, max 71.81% worst=W_phased_mix
```

v16 c16 的 median 明显更好，但 max 更差。也就是说，v16 不是整体退化，而是 `W_phased_mix` 这个 outlier 把 max 和 mean 拉高。

## 5. 全 workload 明细

### c04

| workload | windows | pred CPI | ROI CPI | pVr | win MAPE |
|---|---:|---:|---:|---:|---:|
| `W_ads_ranking_proxy` | 3589 | 0.3635 | 0.4270 | 14.88% | 17.85% |
| `W_chase_dram` | 2979 | 2.5103 | 2.8709 | 12.56% | 15.92% |
| `W_stream` | 2737 | 1.3410 | 1.5234 | 11.97% | 13.67% |
| `W_compute_int` | 2554 | 0.3751 | 0.4183 | 10.32% | 16.07% |
| `W_feed_ranking` | 3353 | 0.7099 | 0.7889 | 10.02% | 5.50% |
| `W_fp_compute_dense` | 2201 | 0.5808 | 0.6228 | 6.74% | 8.37% |
| `W_phased_mix` | 2567 | 0.7274 | 0.6947 | 4.71% | 17.59% |
| `W_branch_storm` | 3311 | 0.4229 | 0.4394 | 3.75% | 24.58% |
| `W_ads_ctr` | 1772 | 0.8820 | 0.9138 | 3.48% | 16.35% |
| `W_interest_graph_recall` | 3246 | 0.9586 | 0.9906 | 3.24% | 17.79% |
| `W_indirect` | 3026 | 0.8683 | 0.8965 | 3.15% | 17.22% |
| `W_int_div` | 2715 | 0.7210 | 0.7432 | 2.99% | 15.21% |
| `W_search_index_proxy` | 2296 | 0.4139 | 0.4041 | 2.42% | 29.79% |
| `W_false_sharing` | 2907 | 7.1086 | 7.2284 | 1.66% | 48.43% |
| `W_mlp_light` | 3103 | 0.5159 | 0.5080 | 1.57% | 2.29% |
| `W_graph_recall_proxy` | 2578 | 0.4925 | 0.4849 | 1.56% | 18.65% |
| `W_fp_lite` | 2671 | 0.6140 | 0.6167 | 0.44% | 19.14% |

### c08

| workload | windows | pred CPI | ROI CPI | pVr | win MAPE |
|---|---:|---:|---:|---:|---:|
| `W_ads_ranking_proxy` | 3459 | 0.3989 | 0.6052 | 34.09% | 30.36% |
| `W_chase_dram` | 2837 | 2.5747 | 2.9076 | 11.45% | 16.58% |
| `W_feed_ranking` | 1931 | 0.7348 | 0.8254 | 10.98% | 5.95% |
| `W_stream` | 2391 | 1.3892 | 1.5442 | 10.04% | 16.91% |
| `W_compute_int` | 2963 | 0.3865 | 0.4183 | 7.60% | 10.76% |
| `W_ads_ctr` | 2446 | 0.9420 | 0.9999 | 5.79% | 15.62% |
| `W_interest_graph_recall` | 1832 | 0.9834 | 1.0290 | 4.43% | 17.59% |
| `W_fp_compute_dense` | 1971 | 0.6251 | 0.6537 | 4.38% | 13.96% |
| `W_phased_mix` | 1960 | 0.7458 | 0.7150 | 4.31% | 17.96% |
| `W_indirect` | 2683 | 0.8594 | 0.8966 | 4.15% | 13.47% |
| `W_branch_storm` | 2511 | 0.4207 | 0.4346 | 3.18% | 24.64% |
| `W_search_index_proxy` | 1996 | 0.4015 | 0.3922 | 2.37% | 30.91% |
| `W_false_sharing` | 2793 | 15.9888 | 16.3142 | 1.99% | 14.80% |
| `W_int_div` | 3401 | 0.7313 | 0.7414 | 1.36% | 11.21% |
| `W_mlp_light` | 2687 | 0.5136 | 0.5080 | 1.10% | 2.63% |
| `W_fp_lite` | 1992 | 0.6657 | 0.6675 | 0.26% | 20.33% |
| `W_graph_recall_proxy` | 2457 | 0.4945 | 0.4949 | 0.07% | 19.42% |

### c16

| workload | windows | pred CPI | ROI CPI | pVr | win MAPE |
|---|---:|---:|---:|---:|---:|
| `W_phased_mix` | 1181 | 1.9036 | 6.7535 | 71.81% | 145.57% |
| `W_ads_ranking_proxy` | 1662 | 0.5462 | 0.8627 | 36.68% | 49.07% |
| `W_compute_int` | 2748 | 0.3292 | 0.4186 | 21.36% | 34.40% |
| `W_graph_recall_proxy` | 2116 | 0.5397 | 0.5989 | 9.89% | 29.20% |
| `W_mlp_light` | 2623 | 0.5499 | 0.5081 | 8.22% | 29.81% |
| `W_fp_lite` | 1580 | 0.7491 | 0.6988 | 7.20% | 32.62% |
| `W_false_sharing` | 2891 | 27.6373 | 29.7250 | 7.02% | 26.20% |
| `W_chase_dram` | 2709 | 3.1053 | 3.2879 | 5.55% | 19.80% |
| `W_feed_ranking` | 1717 | 0.8708 | 0.9026 | 3.52% | 9.29% |
| `W_search_index_proxy` | 1437 | 0.3999 | 0.3870 | 3.33% | 36.67% |
| `W_indirect` | 2684 | 0.8708 | 0.8960 | 2.82% | 11.00% |
| `W_stream` | 1606 | 1.7080 | 1.7419 | 1.95% | 38.25% |
| `W_branch_storm` | 2253 | 0.4268 | 0.4344 | 1.74% | 21.96% |
| `W_ads_ctr` | 1895 | 1.0806 | 1.0956 | 1.36% | 25.92% |
| `W_fp_compute_dense` | 1765 | 0.7291 | 0.7215 | 1.06% | 18.99% |
| `W_interest_graph_recall` | 1781 | 1.0983 | 1.0913 | 0.64% | 20.71% |
| `W_int_div` | 2760 | 0.7425 | 0.7407 | 0.25% | 10.73% |

## 6. 误差来源判断

### 6.1 `W_ads_ranking_proxy`: 特征不足 + per-core 快慢核识别仍不够

`W_ads_ranking_proxy` 的误差随核心数上升：

```text
c04 14.88%
c08 34.09%
c16 36.68%
```

这说明当前模型并没有稳定学到多核随机 gather 带来的共享 LLC/DRAM pressure。v15 的 segment query 在 c08 上把 ads 误差压到 `23.28%`，但牺牲了 `phased_mix`。v16 保留 tail query 后修复全局可见性，却没完全继承 segment query 的本核绑定收益。

下一步需要同时处理：

- functional gather / queue pressure side features。
- cross-core adapter。
- per-core delta/rank/spread anti-mean-collapse loss。
- oracle/label 切窗对照，判断 ads 的误差中有多少来自 free-running planner 偏移。

### 6.2 `W_false_sharing`: v16 方向有效

`W_false_sharing` 在三档核心数都可控：

```text
c04 1.66%
c08 1.99%
c16 7.02%
```

这说明 v16 的 tail-local 结构没有破坏 high-CPI coherence 类负载，反而比 v13/v14/v15 更稳。`W_false_sharing` 不再是主要瓶颈。

### 6.3 `W_phased_mix`: c16 仍有严重闭环/phase 问题

c04/c08 的 `W_phased_mix` 已经修复：

```text
c04 4.71%
c08 4.31%
```

但 c16 变成最大 outlier：

```text
c16 pred=1.9036
c16 ROI =6.7535
c16 err =71.81%
```

这说明 `tail_local` 解决了 c08 上 v15 的 causal 可见性问题，但在 c16 高核心数下仍可能出现：

- phase 进入/退出时窗口边界漂移。
- per-core CPI 快慢程度预测不准，导致 planner 累积错位。
- c16 训练/验证分布下该 workload 的高 CPI phase 不够稳定。

应优先对 c16 `W_phased_mix` 跑 `--dump-window-jsonl-dir`，比较 `planner-state-source=pred` 和 `planner-state-source=label`。

## 7. 下一步建议

优先级：

1. 用 `planner-state-source=label` 跑 `W_ads_ranking_proxy` 和 c16 `W_phased_mix`，区分模型窗口内 CPI 错误与 free-running 切窗累积错误。
2. 实现 v17B：per-core `L_delta` + gated rank/spread，先不重建数据。
3. 实现 v17A：cross-core adapter，验证显式 core communication 是否提升 ads 的快慢核排序。
4. 若 ads 仍 30%+，实现 v17E：gather/queue pressure functional features，并重建 cache。
5. c16 `W_phased_mix` 若 oracle 切窗后明显改善，应优先优化 planner-facing per-core CPI；若 oracle 下仍差，则需要补 phase/working-set 特征或训练分布。
