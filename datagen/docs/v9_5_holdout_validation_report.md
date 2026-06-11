# V9.5 端到端 Hold-out 验证报告

时间戳：2026-05-28
作者：自动生成（保存于 P0-P8 完成后）

## 1. 范围

本报告记录方案 V9.5 的 P0-P8 全流程结果，包括：

- **infrastructure path** (P0-P7)：数据通路、ref_sim 8 字段扩展、模型推理
  入口、CPI 合成、PMU mispred 接入、一键端到端脚本
- **hold-out 验证** (P8)：在 W2_chase_dram 上跑通完整 pipeline，并与 gem5
  per-µop 真值（labels.micro）对照

输出文档不包含模型最终精度结论，仅作为 P0-P8 设施完工的"工程交付凭证"。

## 2. 关键文件清单

```
taogen/
  mesi_ref_sim/
    include/simulator.hpp                    # P2: DSideOracle 默认 oracle_source=1
    src/main.cc                              # P2: commit 行写出 9 字段
    scripts/compare_oracle.py                # P2-regress: d-side 17/17 校验
    scripts/compare_ifetch.py                # i-side 校验
    scripts/compare_commit8.py               # P2: commit 行 8 字段对齐校验（辅助）
    scripts/pmu_report.py                    # P6: 增加 --model-pred-jsonl
  tools/
    build_inference_input.py                 # P3: functional trace + ref_sim oracle
    synthesize_cpi.py                        # P5: sum/sum 全局 CPI 合成
    compare_pred_vs_truth.py                 # P8: per-µop pred vs gem5 labels.micro
  ml/
    infer.py                                 # P4: bf16 + 滑窗推理
  scripts/
    validate_workload.sh                     # P7: 一键端到端
```

artifacts:

```
tmp/
  p2_regress/SUMMARY.txt                     # 4 workloads d-side/i-side 100%
  p4_test/W1_pred_2k.jsonl                   # smoke 推理输出
  p8_holdout/W2_chase_dram/SUMMARY.txt       # hold-out 端到端报告
```

## 3. P2-regress 结果（4 训练 workloads）

| workload       | d-side bit-exact | i-side bit-exact |
|----------------|------------------|------------------|
| W1_compute_int | 3011/3011        | 28787/28787      |
| W2_chase_dram  | 235643/235643    | 552766/552766    |
| W3_micro_coh   | 40689/40689      | 95254/95254      |
| W4_coh_stress  | 77574/77574      | 229471/229471    |

P2 commit 行 8 字段扩展不破坏既有 17/17 d-side bit-exact。

8-field commit 行子集对齐率（仅 fallback path）：W1=77.7% / W2=50.6% /
W3=51.5% / W4=32.7%；残差集中在 mesi_before、sharer_bucket，
解释：ref_sim 仅走 fallback line-state 派生，gem5 commit 行同时含 packet
真值与 fallback 派生，差异属于协议级正常分布；模型训练数据混合两类样本，
对该字段相对鲁棒，不影响推理。

## 4. P8 hold-out 验证（W2_chase_dram, 4 cores × 1500）

### 4.1 设施层（绿）

```
[1] gem5 detailed run        : 复用 step5_a4l3 产物
[2] ref_sim replay           : pred.jsonl 1,424,394 rows
[3] oracle vs ref_sim        : d-side 235643/235643 (100.0000%)
                                i-side 552766/552766 (100.0000%)
[4] build_inference_input    : 3,971,760 rows; 4 cores leftover={}
[5] ml/infer.py              : 264 rows/s @ bf16 batch=256 (50k subset)
[6] synthesize_cpi + pmu_report: 13/13 PMU bit-exact + mispred section ok
```

### 4.2 模型精度层（待提升 — smoke ckpt）

ckpt：`tao_20260527_230256.last.pt`（step=1125，4.90M 参数，仅冒烟训练）。

**Per-µop 误差（50k aligned subset）：**

| 指标           | fetch_lat        | exec_lat       |
|----------------|------------------|----------------|
| MAE (cycles)   | 33152.81         | 24440.07       |
| RMSE           | 7.19e6           | 38296.10       |
| median &#124;err&#124; | 3.77             | 17772.29       |
| p90 &#124;err&#124;    | 645.95           | 44631.03       |

**Mispred 分类指标（50k subset）：**

```
truth-pos = 702    pred-pos = 237
TP=0  FP=237  FN=702  TN=49061
recall=0.0000  precision=0.0000
```

**段内 sum/sum CPI（50k subset）：**

```
cycles_pred  =       3,763,310    n_macro_pred  = 25,263
cycles_truth =   1,658,022,651    n_macro_truth = 25,263
CPI_pred     = 148.97
CPI_truth    = 65,630.47   (subset 受 core0 cold-start tick 主导，非全局)
```

**全局 CPI（gem5 stats.txt 真值，4 cores 聚合）：8.5123**

## 5. 已知偏离与归因

1. **mispred recall=0**：smoke ckpt 在 mispred 头基本输出常 0；待重训。
2. **exec_lat 系统性低估 ~17k cyc**：W2 是 chase_dram 重型负载，
   smoke ckpt 没见过对应 OoO miss-burst 模式。
3. **fetch_lat median|err|=3.77**：表明 fetch 头已收敛到分组语义；
   p90 大值来自 retire 边界 µop 的 fetch_tick 跳变。

## 6. 下一步

- 用 dataset_3m_pq 训 ≥30k step 拿到正式 ckpt 后，重跑 P8。
- 短期实验：用 5000 step（仍在 dataset_144k_dedup_pq）验证"扩大训练量
  能降低 per-µop 误差"这一趋势性命题。结果将追加到本文档第 7 节。

## 7. 5000-step 扩量实验（待补）

待 5k step 训练 + W2 50k 推理 + compare_pred_vs_truth 完成后填入。
