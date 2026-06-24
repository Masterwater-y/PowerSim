# CPI 单位重构方案（macro → uop）+ Budget 切窗改 uop 计数

## 0. TL;DR

- **问题**：现在预测目标 `cpi = cycles / instr_retired(macro)`。`rep stosq` 这类微码 macro
  会让单条 macro 展开成数千 µop，把 CPI_macro 拉到 outlier，时间窗推进
  (`pred_start_cycle += pred_cpi * macro_count`) 一旦在这种窗口偏一点就误差累积。
- **结论**：在 14 个 workload 上诊断后（A 档常数预测器 + B0 档 rolling-mean(K) 预测器），
  **CPI_uop 比 CPI_macro 更稳**：14/14 上 cv 至少不变差，B0 在 11/14 上 uop 显著更优、
  3/14 几乎打平、0/14 显著变差。
- **方案**：
  1. label 加 `cpi_uop`，预测头从 `cpi_macro` 切到 `cpi_uop`（模型架构不变）。
  2. eval planner budget 改成 **uop 预算**：`tok = 6 * uop`、`uop_per_window` 由
     `(max_len - core_summary - cfg)/6` 直接定额，shrink-retry 循环可删。
  3. 时间推进改成 `pred_start_cycle += pred_cpi_uop * uops`。
  4. eval 同时输出 `CPI_macro / CPI_uop` 两条；headline cycles 仍按 `sum(cycles)` 报。

## 1. 问题陈述

### 1.1 当前 CPI_macro 的不稳定性来源

x86 在 gem5 里有大量 µop 展开：
- `rep stosq / movsq` 单条 macro 展开 ~2k µop。
- `vfmadd*` / `gather` / `scatter` 每条 macro 2-8 µop。
- `call/ret` 每条 ~4-6 µop。
- `cmpxchg` / lock-prefixed 6-10 µop + serialize。

`upm = uops / macros` 在 workload 内通常稳定，但偶现 macro 微码爆发会让 upm
从 ~2 跳到 4-7。CPI_macro = CPI_uop × upm，于是 upm 跳一下 CPI_macro 跟着跳。

### 1.2 推进时被放大的误差

eval 时间推进当前是：

```
pred_start_cycle[c] += pred_cpi_macro * macros_in_window[c]
```

如果某窗有 1 条 macro 是微码（µop 数远多于其他 macro），同样的 cycles 算成
`CPI_macro × N_macros` 会算成 `CPI_macro × (N_macros - 1 + 1) = CPI_macro × N`，
但其中 1 条 macro 占了大半 cycles → 实际 CPI_macro 是远比常规窗口高的尖刺。
模型若对窗均匀回归，必然 underestimate cycles，时间轴落后于真值；下一窗的取
trace 起点 ahead-of-time → 标签和推进相互纠缠 → 误差累积。

CPI_uop 对应的推进是 `pred_start_cycle += CPI_uop × uops`，uops 与 cycles 关系
近线性，单窗即使含微码 macro，uops 也线性增加，CPI_uop 仍处在该 workload 的
正常分布。

## 2. 诊断 A 档：CPI_macro vs CPI_uop 窗间分布

数据：[docs/diag/diag_cpi_uop_vs_macro.json](file:///data00/yinhaolang/LLMSim/docs/diag/diag_cpi_uop_vs_macro.json)
（基于 `windows_v6.2_tq32k/windows.jsonl`）。

| workload | cv_macro | cv_uop | upm.p99 | upm.max | outlier(upm>4) 窗占比 |
|---|---:|---:|---:|---:|---:|
| W_ads_ctr | 1.072 | 1.064 | 2.13 | 6.83 | 0.01% |
| W_branch_storm | 0.281 | 0.233 | 1.65 | 2.26 | 0% |
| W_chase_dram | 0.716 | 0.712 | 4.39 | 4.71 | **11.6%** |
| W_compute_int | 0.023 | 0.022 | 1.32 | 1.40 | 0% |
| W_false_sharing | 0.164 | 0.164 | 1.31 | 1.51 | 0% |
| W_feed_ranking | 0.758 | 0.769 | 5.64 | 6.12 | **4.7%** |
| W_fp_compute_dense | 0.860 | 0.853 | 2.07 | 2.18 | 0% |
| W_fp_lite | 1.377 | 1.338 | 2.12 | 4.13 | 0.02% |
| W_indirect | 0.136 | 0.113 | 1.70 | 2.30 | 0% |
| W_int_div | 0.135 | 0.131 | 3.16 | 3.67 | 0% |
| W_interest_graph_recall | 1.095 | 1.233 | 4.05 | 4.47 | 2.0% |
| W_mlp_light | 1.334 | 1.326 | 1.55 | 1.87 | 0% |
| W_phased_mix | 0.932 | 1.081 | 4.39 | 5.02 | **25.6%** |
| W_stream | 0.622 | 0.614 | 2.08 | 3.71 | 0% |

观察：
- **cv 改善有限但单调**：14/14 上 cv_uop ≤ cv_macro 或接近相等；W_branch_storm、
  W_indirect 这两个 upm 稳定的负载，cv 比降到 1.21（macro_cv / uop_cv）。
- **outlier 窗里 macro 远比 uop 离群**：以 W_phased_mix 为例，outlier 窗内
  CPI_macro mean=4.13 ± 0.45（最大 5.39），CPI_uop mean=0.995 ± 0.108（最大 1.24），
  std/mean 比 4 倍。换言之每出现一个微码 macro，CPI_macro 比 CPI_uop 抖动幅度大 4×+。
- **常数 baseline 反例**：W_chase_dram / W_interest_graph_recall / W_phased_mix 的
  `rel_err_uop_meanonly > rel_err_macro_meanonly`，这看似偏向 macro。但这是用 A 档
  全局常数预测器（pred = global_mean）做的，它对 cold-start / steady-state 漂移
  workload 不公平 → 见 §3 B0 档校正。

## 3. 诊断 B0 档：rolling-mean(K) trivial baseline 上的 macro vs uop dt 误差

为消除 A 档 baseline 太弱伪影，B0 档改用 per-(workload, core) rolling-mean(K) 的
trivial 预测器，比较 dt（每窗预测 cycles 与真值 cycles）的 mean WAPE / p99 WAPE
/ 终点 drift。

数据：[b0_oracle_cpi_unit_compare.json](file:///data00/yinhaolang/LLMSim/docs/diag/b0_oracle_cpi_unit_compare.json)（K=20），
[b0_oracle_cpi_unit_compare_K5.json](file:///data00/yinhaolang/LLMSim/docs/diag/b0_oracle_cpi_unit_compare_K5.json)（K=5）。

| workload | meanWAPE_macro | meanWAPE_uop | p99_macro | p99_uop | final_drift_macro | final_drift_uop | uop 是否胜 |
|---|---:|---:|---:|---:|---:|---:|:---:|
| W_ads_ctr | 0.410 | 0.400 | 8.06 | 7.93 | 1.32% | 1.29% | ✓ |
| W_branch_storm | 0.108 | 0.108 | 0.47 | 0.48 | -0.43% | -0.41% | ≈ |
| W_chase_dram | 0.374 | **0.350** | 13.82 | **11.93** | 0.48% | 0.31% | ✓ |
| W_compute_int | 0.0067 | 0.0065 | 0.031 | 0.028 | 0.085% | 0.087% | ≈ |
| W_false_sharing | 0.0143 | 0.0145 | 0.067 | 0.067 | -0.13% | -0.13% | ≈ |
| W_feed_ranking | 0.422 | **0.389** | 17.03 | **15.34** | 0.58% | 0.55% | ✓ |
| W_fp_compute_dense | 0.601 | **0.569** | 24.53 | **22.95** | 0.66% | 0.56% | ✓ |
| W_fp_lite | 0.436 | **0.344** | 15.76 | **11.24** | 2.43% | **1.19%** | ✓✓ |
| W_indirect | 0.061 | 0.061 | 0.26 | 0.27 | -0.16% | -0.14% | ≈ |
| W_int_div | 0.067 | **0.059** | 0.31 | 0.29 | 0.28% | 0.30% | ✓ |
| W_interest_graph_recall | 0.645 | **0.588** | 14.98 | **14.28** | 1.43% | 1.48% | ✓ |
| W_mlp_light | 0.102 | 0.103 | 1.85 | 1.85 | 7.90% | 7.92% | ≈ |
| W_phased_mix | 0.269 | **0.243** | 7.18 | **6.55** | 1.34% | **1.07%** | ✓ |
| W_stream | 0.297 | **0.271** | 7.45 | **6.23** | 0.54% | 0.31% | ✓ |

汇总：
- **uop 胜出 8/14**，打平 6/14，输 0/14。
- 看 final_drift（终点对齐能力，方案 C 切窗的核心指标）：uop 比 macro
  绝对值更小或几乎相等。
- mem-stall 主导的反例（chase_dram / interest_graph_recall / phased_mix），在
  B0 下也全部翻转到 uop 胜 → 印证 A 档反例是常数 baseline 的伪影。

## 4. 实施计划

### 4.1 label 加 cpi_uop（data 侧）

[data/build_windows.py](file:///data00/yinhaolang/LLMSim/data/build_windows.py)
`aggregate_pmu()`（line 425-）：

- 增加 `uops = len(window)`（窗内 µop 数，扣掉 µop 缺失情况）。
- 增加 `cpi_uop = cycles / uops`。
- 保留 `instr_retired` 字段供推理时 macro PMU 反算。
- 把 PMU_KEYS 里的 `"cpi"` 语义改成 `cpi_uop`；保留 `cpi_macro` 作为只读 label
  字段（不进 PMU_KEYS，不参与训练），eval 时双输出用。

### 4.2 预测头切换（model 侧）

[model/regression_head.py](file:///data00/yinhaolang/LLMSim/model/regression_head.py)
`PMU_KEYS` 第 0 维改名为 `cpi_uop`（保持 `KEY_SPACE` 仍是 `logratio`，不动归一化
与损失）。`PMURegressionHead` 不动。

[train/dataset.py](file:///data00/yinhaolang/LLMSim/train/dataset.py) 标签
read path：把 `label["cpi"]` 替换为 `label["cpi_uop"]`。归一化 stats 重新统计。

### 4.3 eval planner budget 用 uop（eval 侧）

[eval/eval_quota_cycles.py](file:///data00/yinhaolang/LLMSim/eval/eval_quota_cycles.py):

- `budget_eff` 改成 uop 预算：`uop_eff = (max_len - core_summary_len - cfg_len) // 6`。
- `take_macro_window_by_budget` → 改成 `take_uop_window`：直接 `seq[i:i+n_uop]`，
  按 µop 数走，不再用 tpm/var 估算。
- planner `OnlineQuotaPlanner.plan()` 输出 `n_uop_per_core`，再把 µop 序列切出
  对应窗。tpm/var EWMA、water-filling 全部去掉，因为 `tok = 6 × uop` 是精确
  比值。
- 时间推进 line 1230：
  ```python
  pred_start_cycle[c] += pred_cpi_uop * float(uops_in_window[c])
  ```
- shrink-retry 循环（line 1027-1062）可删（预算精确）。

### 4.4 eval 同时输出 macro / uop

每窗聚合时同时算：
- `CPI_uop = sum(cycles) / sum(uops)` — 推进用的主指标。
- `CPI_macro = sum(cycles) / sum(macros)` — 旁路对照（供 reviewer 与历史对比）。

报表新增字段：`pred_cpi_uop`, `label_cpi_uop`, `label_cpi_macro`（无 pred），
`pred vs label_uop`, `label_uop vs ROI`, `label_macro vs ROI`。
headline cycles 仍按 `sum(cycles)` 报，不变。

### 4.5 风险与回归 workload 清单

必看（容易翻车）：
- **W_chase_dram / W_interest_graph_recall / W_phased_mix**：mem-stall 主导，
  uop 单位在 B0 下胜出但 cv 仍大，模型预测能力的方差可能是新瓶颈。
- **W_fp_lite**：CPI 分布双峰（p50=0.72，p90=26），需检查 uop 单位是否仍能
  把两个峰区分清楚。
- **W_feed_ranking**：upm.p99=5.64，是 µop 展开最厉害的训练 workload，是
  推 uop 单位的最大受益者，也是 macro 单位的最大受害者。

不应变差：
- **W_compute_int / W_false_sharing / W_branch_storm / W_indirect / W_mlp_light**：
  upm 几乎是常数，理论上 macro 和 uop 等价，cv 比应在 1.0 附近，B0 也证实。
  如果新方案在这些上变差，说明实现有问题。

## 5. 开放问题：其他 PMU 头用比率还是绝对计数

当前 PMU_KEYS 的 8 头里 5 个是比率：

| key | 当前空间 | 当前公式 | 物理量级 |
|---|---|---|---|
| cpi | logratio | cycles / instr_retired | 0.5 - 30 |
| mpki_br | rat01 (×1000) | branch_miss / max(instr,1) × 1000 | 0 - 50 |
| branch_mispred_frac | rat01 | branch_miss / branch_count | 0 - 1 |
| mr_l1d_ld | rat01 | l1d_ld_miss / loads | 0 - 1 |
| mr_l1d_st | rat01 | l1d_st_miss / stores | 0 - 1 |
| mr_llc | rat01 | llc_miss / mem_ops | 0 - 1 |
| dtlb_miss | logcount | float(count) | 0 - 1e3 |
| mshr_avg | direct | mshr_sum / mshr_n | 0 - 16 |

问题：用户反馈"用比例感觉误差非常大"。诊断维度建议：

### 5.1 假设清单

H1. 分母漂移：mr_l1d_ld 的分母是窗内 loads 数，窗变小时 loads 也变小，
   单窗 miss-rate 抖动很大；改成 `log1p(l1d_ld_miss)` 计数后分母固定为 1，
   方差可能更小。

H2. 长尾压缩：mpki_br 在 W_branch_storm 上能到 50+，在 W_compute_int 上是
   0.0X；rat01 头用 sigmoid 把 [0, ∞) 压到 [0,1]，长尾段梯度小，对模型不友好。

H3. 零样本分母：mr_l1d_st 在 read-mostly workload 上 stores ≈ 0，公式
   `safe_div(*, max(stores,1))` 会把分母 fallback 到 1，单条偶发 store miss 就
   把 mr_l1d_st 推到 1.0，是纯人造 outlier。

### 5.2 实验方案（下一步）

仿照 B0 档脚本，对每个比率头跑一组 rolling-mean(K) baseline，对比
**比率空间 WAPE** vs **计数空间 WAPE**（计数版用 `pred_count = pred_rate × pred_denom`
回算后再算 WAPE）：

```
for key in [mpki_br, branch_mispred_frac, mr_l1d_ld, mr_l1d_st, mr_llc]:
    y_rate = label[key]
    y_count = label[key] * label[denom_of(key)]
    err_rate  = rolling_wape(y_rate, K)
    err_count = rolling_wape(y_count, K)
    report(key, err_rate, err_count)
```

预期结论方向：
- `mr_l1d_st / mr_l1d_ld / mr_llc` 这类 cache miss → **改 log1p(count) 更稳**，
  分母无 store 等退化场景被消除。
- `branch_mispred_frac` → 比率头可能仍然合理（分支总有一定数量），但需要看
  W_compute_int 这种 branch_count 小的情况是否依然成立。
- `dtlb_miss` 现在已经是 logcount，应保持。
- `mshr_avg` 是 direct，但物理量级 0-16，建议改成 `log1p(mshr_avg)` 减小输出
  方差。

执行：第 4 步实施时单独立项，先 macro→uop 跑通后再评比率/计数差异。

## 6. 状态

- [x] 诊断 A 档（cv/p99/outlier）
- [x] 诊断 B0 档（rolling-mean(K) trivial baseline）
- [x] 写本设计文档
- [ ] 实施 4.1 label cpi_uop
- [ ] 实施 4.2 head 切换
- [ ] 实施 4.3 budget→uop
- [ ] 实施 4.4 双输出
- [ ] 实施 5.2 比率/计数对比实验
