# pre-v15 方案设计：v9 基线上的 per-core 快慢核识别

日期：2026-07-01

状态：v15A+B 实施稿。目标是在当前回退到 v9 的稳定语义基础上，专门修复 `W_ads_ranking_proxy` 中 per-core 快慢核识别失败的问题，而不是继续扩大模型或回到 v13/v14 的 tail/gated 路线。

## 1. 版本定位

建议版本号使用 **v15**，但在说明和 checkpoint tag 中明确写成：

```text
v15_v9core
```

原因：

- 它的标签、输入字段和部署 eval 主线都从 v9 延伸。
- 但模型 head、loss、query 位置都会改变，已经不是可直接称为 v9 的小修。
- v10-v14 的效率代码可以继续保留，但不继承 v13 tail loss / gated side / v14 head-only 结论。

推荐命名：

```text
data/windows_v15_v9core_queryseg_all/
ckpt/v15_v9core_delta_rank_queryseg_8gpu_8000/
logs/eval_parallel_v15_v9core_...
```

## 2. 背景结论

当前 v9 在 c08 seedB 的 `W_ads_ranking_proxy` 上明显失败：

```text
full eval:
  pred_cpi_uop = 0.3841
  label/ROI    = 0.6052
  error        = 36.53%
```

对前 1000 个 eval 窗口做逐窗 per-core dump 后得到：

```text
agg_pred_cpi_uop = 0.3948
agg_label_cpi_uop = 0.5993
agg_relerr = 34.1%

pred_core_cv mean  = 0.295
label_core_cv mean = 0.629

pred_core_range mean  = 0.420
label_core_range mean = 1.133

pred_label_corr mean = -0.054
slowest_core_hit_rate = 0.117
fastest_core_hit_rate = 0.175
random_baseline_8core = 0.125
```

判断：

- 模型不是把所有 core 完全预测成一样。
- 但模型显著压扁了核间 CPI 差异。
- 更严重的是，预测快慢核排序接近随机。
- planner 虽然会做不均匀 `planned_counts`，但它使用的是低相关的 per-core CPI 信号，因此无法复现训练 TQ 中真实快慢核时间跨度。

## 3. 目标与非目标

目标：

- 提升 per-core CPI 快慢排序能力。
- 让预测的核间 CPI 离散度接近真实离散度，而不是回归到窗口均值。
- 降低 `W_ads_ranking_proxy` 在 c08/c16 上的系统性低估。
- 保持 v9 的 `1 uop = 1 position`、tensor cache、online eval 路径。
- 不引入 workload name、seed、oracle PMU/timing state 作为输入。

非目标：

- 不为每个 core 单独建固定 head，避免破坏核心数泛化。
- 不回到 v13 tail-aware loss 作为主方案。
- 不用 low-CPI/easy-workload guardrail 做 workload 特化。
- 不依赖 gem5 timing/PMU 才能得到的新特征。

## 4. 改动一：CPI = base + per-core delta

当前 v9 是共享 head 直接输出每核 `log_cpi_i`：

```text
query_hidden_i -> shared PMURegressionHead -> log_cpi_i
```

v15 改成显式分解：

```text
log_cpi_i = base_log_cpi + delta_i
```

其中：

- `base_log_cpi` 表示窗口整体 CPI 水平。
- `delta_i` 表示第 i 个 core 相对窗口平均的快慢偏移。
- `delta_i > 0` 表示该 core 比平均慢。
- `delta_i < 0` 表示该 core 比平均快。

实现建议：

```python
h = query_hidden                         # [B, C, D]
mask = core_mask                         # [B, C]

base_hidden = masked_mean(h, mask)        # [B, D]
base = base_head(base_hidden)             # [B, 1]

delta_raw = delta_head(h).squeeze(-1)      # [B, C]
delta = delta_raw - masked_mean(delta_raw, mask)

pred_log_cpi = base.unsqueeze(1) + delta
```

输出张量仍保持 `[B, C, K]`，只替换 `cpi_uop` 这一维。其它 PMU 维度继续走共享 head，以保持 eval 和 checkpoint 保存逻辑容易兼容。

预期收益：

- base 负责 workload/phase/global CPI。
- delta 专门学习核间快慢差异。
- 后续 rank/spread loss 可以直接作用于 `pred_log_cpi` 或 `delta`。

风险：

- 如果 `base` 太强、`delta` 仍然塌缩，收益有限。
- 如果 `delta` 过强，可能放大噪声窗口的核间差异。

控制方式：

- `delta` 零均值。
- rank/spread loss 小权重起步。
- 固定报告 `spread_ratio = pred_core_cv / label_core_cv`。

## 5. 改动二：轻量 rank + spread loss

普通 per-core Huber loss 会倾向条件均值。当各核输入相似但真实 CPI 受共享状态影响时，模型容易把差异压扁。

v15 增加两个只作用于 `cpi_uop` 的辅助 loss。

### 5.1 Pairwise ranking loss

目标是学会“哪个核慢”。

在 log CPI 空间：

```python
y = log(label_cpi)
p = pred_log_cpi

dy = y_i - y_j
dp = p_i - p_j
sign = sign(dy)
valid_pair = abs(dy) > label_gap

L_rank = softplus(-(dp * sign) / tau)
```

建议初始参数：

```text
label_gap = 0.10   # 约 10% CPI 差异以上才监督排序
tau       = 0.10
lambda_rank = 0.02
```

只对同一窗口内 active core pair 生效。

### 5.2 Spread calibration loss

目标是学会“快慢差距有多大”。

```python
std_pred = std(pred_log_cpi over active cores)
std_label = std(log(label_cpi) over active cores)

L_spread = Huber(log(std_pred + eps), log(std_label + eps))
```

建议初始参数：

```text
lambda_spread = 0.02
spread_eps = 1e-3
```

可选保护：

```text
仅当 std_label > 0.03 时启用 spread loss
```

避免在真实核间差异很小的窗口里强行制造差异。

### 5.3 总 loss 初始形式

保留 v9 主损失，增加轻量辅助项：

```text
L = L_v9_main
  + lambda_rank   * L_rank
  + lambda_spread * L_spread
```

第一版不重新引入 v13 tail loss。若后续发现 high-CPI 全局低估仍明显，再单独做 ablation。

## 6. 改动三：调整 query 位置，增强 core binding

当前 v9 布局：

```text
<C0_BEGIN> summary0 uops0 <C0_END>
<C1_BEGIN> summary1 uops1 <C1_END>
...
<C7_BEGIN> summary7 uops7 <C7_END>
<TRACE_END>
<QUERY_C0> <QUERY_C1> ... <QUERY_C7>
```

问题：

- `<QUERY_C0>` 到 C0 段的距离受 C1-C7 长度影响。
- 不同 core 的 uop 长度变化会改变 query 到目标 core 的 RoPE 距离。
- `<QUERY_C7>` 总是离 C7 近，`<QUERY_C0>` 总是离 C0 远，存在结构性位置偏置。

v15 建议改成段内 query：

```text
<C0_BEGIN> summary0 uops0 <QUERY_C0> <C0_END>
<C1_BEGIN> summary1 uops1 <QUERY_C1> <C1_END>
...
<C7_BEGIN> summary7 uops7 <QUERY_C7> <C7_END>
<TRACE_END>
```

这个方案不能消除“本 core uop 长度不同”的距离变化，但可以消除“其它 core 长度污染本 core query 距离”的问题。

decoder-only 注意点：

- `<QUERY_Ci>` 在段内时只能看见它之前的 token。
- 早序号 core 看不到后续 core 的原始 uop token。
- 但所有 core query 都能看到序列开头的 global tokens，以及通过 `side_feats` 注入的跨核 functional summary。
- 因此 v15B 的假设是：本核 raw uop + 全局/跨核 summary 足够预测 per-core delta；如果后续发现早序号 core 明显偏差，再回退到 tail query 或改成局部 pooling 方案。

实现点：

- 修改 `data/build_windows.py::encode_multicore_sample()`。
- 修改 `eval/eval_quota_cycles.py` 的在线 sample encode。
- 修改 tensor cache 构建时的 `qpos` 计算即可；训练 forward 不需要改。
- 保持 `<QUERY_Ci>` token 族不变，不需要改 tokenizer vocabulary。

数据影响：

- 不需要重新采集 raw trace。
- 不需要重跑 gem5。
- 需要重新生成训练 tensor cache。
- 推荐生成新的 windows/cache 目录，不覆盖 v9 原始数据。

可选实现路径：

1. 最稳：从 aligned parquet 重新 build windows。
2. 更快：写 JSONL relocation 工具，把旧 v9 windows 中尾部 query 移到对应 core 段内，同时同步 `tokens/is_uop/uop_fields/qpos`，再重建 tensor cache。

第一版优先使用最稳路径，除非构建时间成为瓶颈。

## 7. 当前执行方案与 ablation

当前按用户要求，直接同时做 v15A+B：

```text
base+delta CPI head
+ rank/spread loss
+ segment query placement
```

保留 ablation 设计，方便如果结果不佳时定位收益来源。

### v15A：base+delta + rank/spread，旧 query 位置

目的：

- 验证 loss/head 是否能改善 per-core 快慢核识别。
- 复用当前 v9 tensor cache，不需要重建数据。

验收重点：

```text
pred_core_cv / label_core_cv 是否上升并接近 1
pred_label_corr 是否 > 0
slowest_core_hit_rate 是否明显高于 0.125
W_ads c08/c16 CPI error 是否下降
```

### v15B：v15A + 段内 query（当前主线）

目的：

- 验证 query binding 和 RoPE 距离稳定性是否是关键瓶颈。

需要：

- 新 windows/cache。
- eval 在线 encode 同步使用段内 query。

### v15C：若仍失败，再补 deployment-style 短窗口训练数据

当前已知 train/eval 窗口分布仍有偏移：

```text
train W_ads/c08 mean ≈ 858.7 uops/core
diag first 1000 eval mean ≈ 492.8 uops/core
full eval mean ≈ 304.4 uops/core
```

如果 v15B 仍然低估，下一步应补：

```text
256 / 320 / 384 / 512 uops_per_core
```

的 deployment-style sequential windows。

## 8. 验收指标

v15 不只看最终 CPI MAPE，必须固定报告 per-core 诊断指标。

每个重点 workload 至少报告：

```text
pred_core_cv
label_core_cv
spread_ratio = pred_core_cv / label_core_cv
pred_core_range
label_core_range
pred_label_corr
slowest_core_hit_rate
fastest_core_hit_rate
pairwise_order_acc
planned_counts_cv
actual_uops_mean_per_core
```

主要验收门槛：

```text
W_ads_ranking_proxy c08:
  CPI error 从 36.5% 明显下降
  pred_label_corr 从 -0.054 提升到 > 0.20
  slowest_core_hit_rate 从 0.117 提升到 > 0.25
  spread_ratio 接近 1，至少从 0.47 提升到 > 0.70
```

同时监控非目标退化：

```text
c04/c08/c16 全 workload mean/median error 不应明显差于 v9
compute_int / int_div / search_index_proxy 不应因 spread loss 被拉坏
false_sharing 单独作为 stress 分榜，不作为唯一优化目标
```

## 9. 实施清单

代码改动：

1. `model/regression_head.py`
   - 增加 `base + per-core delta` CPI 路径。
   - 保持输出 `[B, C, K]` 和 `PMU_KEYS` 不变。

2. `train/loss.py`
   - 增加 `L_rank`、`L_spread`。
   - 默认小权重：`lambda_rank=0.02`、`lambda_spread=0.02`。
   - 日志输出 `L_rank`、`L_spread`、`pairwise_order_acc`。

3. `data/build_windows.py`
   - 增加 query placement 选项，支持 `tail` 与 `segment`。
   - v15 默认 `segment`。

4. `eval/eval_quota_cycles.py`
   - 同步 query placement。
   - dump 中继续保留 per-core pred/label/planned counts。

5. `scripts/diag_v9_cpi_spread.sh`
   - 可泛化命名为 v15 诊断脚本，或继续复用，只要参数指向 v15 ckpt/log。

6. `scripts/build_v15_v9core_queryseg_train600.sh`
   - 删除旧 v15 windows/cache。
   - 从 c01/c04/c08/c16 seedA raw 重新构建 segment-query windows。
   - 合并成 `data/windows_v15_v9core_queryseg_all/windows.jsonl`。
   - 构建 tensor cache。

7. `scripts/run_v15_v9core_delta_rank_queryseg_qwen3_0p6b.sh`
   - 8 卡训练 v15A+B。
   - 默认 8000 step、eval every 500。

8. `scripts/run_v15_v9core_eval_sweep.sh`
   - 默认对 c04/c08/c16 seedB 做并行 eval。
   - 自动设置 `QUERY_PLACEMENT=segment`。

数据/训练：

1. 当前直接做 v15A+B，不覆盖 v9 原始 windows/cache。
2. 训练步数沿用 8000 step，eval every 500。
3. eval 至少跑 c04/c08/c16 seedB。
4. 对 `W_ads_ranking_proxy` 固定跑 1000-window diag 和 full eval。

一键构建：

```bash
nohup env CLEAN=1 JOBS=17 bash scripts/build_v15_v9core_queryseg_train600.sh > logs/build_v15_v9core_queryseg_train600.nohup.log 2>&1 &
```

训练：

```bash
nohup bash scripts/run_v15_v9core_delta_rank_queryseg_qwen3_0p6b.sh > logs/train_v15_v9core_delta_rank_queryseg_8gpu_8000.log 2>&1 &
```

评估：

```bash
nohup bash scripts/run_v15_v9core_eval_sweep.sh > logs/eval_v15_v9core_delta_rank_queryseg_sweep.log 2>&1 &
```

## 10. 风险与回滚

风险：

- rank loss 可能在信息不足时逼模型猜排序，导致 easy workload 退化。
- spread loss 可能过度放大核间噪声。
- 段内 query 改变序列几何，可能影响已经学到的 v9 表示。
- 如果主因仍是 train/eval 短窗口分布偏移，v15A/B 可能只能改善 per-core 指标，不能完全修复 CPI。

回滚策略：

- 保留 v9 cache 和 v9 eval 脚本不覆盖。
- v15 head/loss 用独立 checkpoint。
- query placement 保留 `tail` 兼容路径。
- 若 v15B 退化，保留 v15A 结果，并优先进入 v15C 短窗口数据补齐。
