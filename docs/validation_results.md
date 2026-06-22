# 模型验证结果

本文档沉淀 LLMSim 部署侧推理验证结果，作为后续持续追加的统一入口。

## 口径

- 主 baseline：`trace ROI stats`
  - `ROI instr`：按 rec 的 macro head 计数
  - `ROI cycles`：每核 `max(commit_tick) - min(commit_tick)` 后求和
  - `ROI CPI = ROI cycles / ROI instr`
- `label`：方案 C 连续切窗后，窗口标签聚合得到的全局 CPI
- `pred`：模型部署侧推理得到的全局 CPI
- `gem5_full`：`stats.txt` 全程 `ΣnumCycles / ΣcommitStats0.numInsts`
  - 仅作参考，不作为严格 baseline
  - 原因：可能包含 trace ROI 外的 setup/drain/idle

## 汇总表

| workload | ckpt | max_len | windows | pred CPI | label CPI | ROI CPI | pred vs ROI | label vs ROI | gem5_full vs ROI | macro/s | 日志 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| W_compute_int | `ckpt/quota_32k_balanced_v1` | 32768 | 831 | 0.5501 | 0.5272 | 0.5275 | 4.28% | 0.07% | 8.38% | ~2113 | [eval_quotaC_full_compute_int_roi.log](file:///data00/yinhaolang/LLMSim/logs/eval_quotaC_full_compute_int_roi.log) |
| W_false_sharing | `ckpt/quota_32k_balanced_v1` | 32768 | 1017 | 22.5160 | 23.9495 | 22.6575 | 0.62% | 5.70% | 0.43% | ~2615 | [eval_quotaC_full_false_sharing_roi.log](file:///data00/yinhaolang/LLMSim/logs/eval_quotaC_full_false_sharing_roi.log) |

## 结果详情

### 2026-06-18 11 负载并行验证：quota_32k_balanced_v1

- ckpt：`ckpt/quota_32k_balanced_v1`
- raw 入口：`data/raw_eval11_8c`
- 运行日志：
  - 控制台总日志：[logs/eval_parallel_quota32kv1_console_rerun.log](file:///data00/yinhaolang/LLMSim/logs/eval_parallel_quota32kv1_console_rerun.log)
  - 子日志目录：[logs/eval_parallel_quota_32k_balanced_v1_20260618_124659](file:///data00/yinhaolang/LLMSim/logs/eval_parallel_quota_32k_balanced_v1_20260618_124659)
- 参数口径：
  - `max_len = 32768`
  - `dt_target = 8000`
  - `dt_max = 12000`
  - `uarch_config = arch_A`
  - 8 GPU 并行，每个 workload 独立进程
- 数据口径：
  - 旧 7 个训练负载来自 `data/raw_train8`
  - `W_phased_mix` 使用轻量重采版本 `data/raw_phased_mix_eval2k_8c/W_phased_mix`
  - infer3 三个负载来自 `data/raw_infer3_8c_500k_fullfit`

| workload | windows | pred CPI | label CPI | ROI CPI | gem5 CPI | pred vs label | pred vs ROI | label vs ROI | gem5 vs ROI | win MAPE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| W_ads_ctr | 696 | 2.2534 | 1.7283 | 1.6833 | 1.7274 | 30.38% | 33.87% | 2.67% | 2.62% | 123.45% |
| W_branch_storm | 1160 | 0.8656 | 0.8436 | 0.8455 | 0.8781 | 2.61% | 2.38% | 0.22% | 3.86% | 40.03% |
| W_chase_dram | 876 | 7.0490 | 6.9406 | 6.7042 | 6.7035 | 1.56% | 5.14% | 3.53% | 0.01% | 74.69% |
| W_compute_int | 830 | 0.5498 | 0.5272 | 0.5275 | 0.5717 | 4.30% | 4.22% | 0.07% | 8.38% | 22.75% |
| W_false_sharing | 751 | 22.9468 | 23.9907 | 22.6575 | 22.5608 | 4.35% | 1.28% | 5.88% | 0.43% | 17.28% |
| W_feed_ranking | 809 | 1.6979 | 1.5266 | 1.5550 | 1.6052 | 11.22% | 9.19% | 1.83% | 3.22% | 84.37% |
| W_indirect | 860 | 1.6789 | 1.5944 | 1.5965 | 1.6360 | 5.30% | 5.16% | 0.13% | 2.47% | 34.62% |
| W_int_div | 1274 | 2.1127 | 2.0192 | 2.0200 | 2.0638 | 4.63% | 4.59% | 0.04% | 2.17% | 60.48% |
| W_interest_graph_recall | 715 | 3.7090 | 3.9115 | 3.7679 | 3.8156 | 5.18% | 1.56% | 3.81% | 1.26% | 143.52% |
| W_phased_mix | 1571 | 7.3288 | 7.1446 | 6.8517 | - | 2.58% | 6.96% | 4.28% | - | 63.35% |
| W_stream | 797 | 2.8765 | 2.8603 | 2.6392 | 2.6708 | 0.57% | 8.99% | 8.38% | 1.20% | 68.11% |

聚合结果：

- 11 个 workload 的平均 `pred vs ROI = 7.58%`
- 除 `W_ads_ctr` 外，10 个 workload 的平均 `pred vs ROI = 4.95%`
- 旧训练负载中 `W_false_sharing / W_branch_storm / W_compute_int / W_int_div` 表现较稳，`pred vs ROI` 约 1.28% 到 4.59%
- infer3 三件套里：
  - `W_ads_ctr` 仍是最大异常点：`pred vs ROI = 33.87%`
  - `W_feed_ranking` 中等偏高：`pred vs ROI = 9.19%`
  - `W_interest_graph_recall` 表现较好：`pred vs ROI = 1.56%`
- `W_stream / W_chase_dram / W_phased_mix` 的全局误差处于 5% 到 9% 区间，但 per-window MAPE 仍高，说明局部窗口误差较大、全局聚合后被抵消。
- `W_phased_mix` 的 `gem5 CPI` 为 `-`，原因是轻量重采版本的 `stats.txt` 解析出的 `gem5_full` 为 `nan`；该项不影响 `pred/label/ROI` 主口径。

结论：

- 当前最优 ckpt `quota_32k_balanced_v1` 在旧训练负载整体可用，主要风险仍集中在 `W_ads_ctr`。
- `W_ads_ctr` 的 `label vs ROI = 2.67%`，说明切窗标签本身与 ROI 基线基本对齐；主要误差来自模型预测偏高，而不是 ROI/label 对齐问题。
- 后续续训验证应以本表作为 baseline，对比 resume 后 ckpt 在 11 个负载上的 `pred vs ROI` 和 `W_ads_ctr` 改善幅度，同时确保旧负载不明显退化。

### W_compute_int

- 配置：
  - `ckpt = ckpt/quota_32k_balanced_v1`
  - `max_len = 32768`
  - `workload = W_compute_int`
  - `baseline = trace ROI stats`
- 正式日志：
  - [logs/eval_quotaC_full_compute_int_roi.log](file:///data00/yinhaolang/LLMSim/logs/eval_quotaC_full_compute_int_roi.log)
- 结果：
  - `pred_cpi = 0.5501361249`
  - `label_cpi = 0.5271652471`
  - `roi_stats_cpi = 0.5275439901`
  - `gem5_full_cpi = 0.5717260319`
  - `pred_vs_label = 4.36%`
  - `pred_vs_roi = 4.28%`
  - `label_vs_roi = 0.07%`
  - `gem5_full_vs_roi = 8.38%`
  - `per-window CPI MAPE = 22.01%`
  - `windows = 831`
  - `ROI instr/cycles = 3344559 / 1764402.0`
  - `avg_instr_per_core = 488.9`
  - `steady macro/s ≈ 2113`

结论：

- 方案 C 的切窗/标签聚合已经和 ROI baseline 对齐：`label vs ROI = 0.07%`
- 当前模型在 `W_compute_int` 上仍有稳定正偏：`pred vs ROI = 4.28%`
- `gem5_full` 显著高于 ROI，说明 full stats 与 trace ROI 口径不同，不应用作主 baseline

### W_false_sharing

- 配置：
  - `ckpt = ckpt/quota_32k_balanced_v1`
  - `max_len = 32768`
  - `workload = W_false_sharing`
  - `baseline = trace ROI stats`
- 正式日志：
  - [logs/eval_quotaC_full_false_sharing_roi.log](file:///data00/yinhaolang/LLMSim/logs/eval_quotaC_full_false_sharing_roi.log)
- 结果：
  - `pred_cpi = 22.5159810786`
  - `label_cpi = 23.9494829262`
  - `roi_stats_cpi = 22.6575439202`
  - `gem5_full_cpi = 22.5608169206`
  - `pred_vs_label = 5.99%`
  - `pred_vs_roi = 0.62%`
  - `label_vs_roi = 5.70%`
  - `gem5_full_vs_roi = 0.43%`
  - `per-window CPI MAPE = 10.32%`
  - `windows = 1017`
  - `ROI instr/cycles = 3441069 / 77966172.0`
  - `avg_instr_per_core = 364.8`
  - `steady macro/s ≈ 2615`
  - `timing(avg/window) ≈ build 3.3ms + encode 35.5ms + tensor 1.0ms + forward 1075ms + update 1.0ms`

结论：

- 当前模型在 `W_false_sharing` 上与 ROI baseline 很接近：`pred vs ROI = 0.62%`
- 但窗口标签聚合显著高于 ROI：`label vs ROI = 5.70%`，说明该负载下方案 C 切窗本身仍有系统性偏高
- `gem5_full` 与 ROI 很接近：`gem5_full vs ROI = 0.43%`，这个 workload 的 full stats 口径偏差很小
- 推理吞吐的主瓶颈是 GPU forward，而不是输入构造

## ROI baseline 文件

- 全量 workload ROI stats：
  - [data/raw_train8/roi_stats.json](file:///data00/yinhaolang/LLMSim/data/raw_train8/roi_stats.json)

## 后续追加格式

新增 workload 验证结果时，按下面模板补一节，并更新汇总表：

```md
### W_xxx

- 配置：
  - `ckpt = ...`
  - `max_len = ...`
  - `workload = ...`
  - `baseline = trace ROI stats`
- 正式日志：
  - `logs/...`
- 结果：
  - `pred_cpi = ...`
  - `label_cpi = ...`
  - `roi_stats_cpi = ...`
  - `gem5_full_cpi = ...`
  - `pred_vs_roi = ...`
  - `label_vs_roi = ...`
  - `gem5_full_vs_roi = ...`
  - `windows = ...`
  - `avg_instr_per_core = ...`
  - `steady macro/s = ...`
```
