# TCSim MVP 首轮结果分析与 Loss 重设计

> **已被 v27.2 澄清方案取代。** 本文第 4--6 节关于“逐元素 mean 会让正负误差抵消”、
> “per-core normalize 可以杜绝 mean collapse”以及首轮 model-generated rollout 的结论不再作为实现依据。
> 当前规范见 [`v27_2_oracle_first_functional_training_and_per_core_loss.md`](./v27_2_oracle_first_functional_training_and_per_core_loss.md)。
> 首版现在采用：真实 timing 生成 label 和 oracle context、模型只读 functional-safe 特征、
> `T_pred/E_pred/delta_hat` 留在外部 scheduler、首模型训练完成后才做 predicted-rollout 闭环评测。

**日期**：2026-07-10
**范围**：`/data00/yinhaolang/TCSim` 第一版 (Phase 0–3) 运行结果、chase workload 物理解释、异常样本处理策略、以及针对 mean collapse 的 loss 重设计。
**上游文档**：`/data00/yinhaolang/TSim/docs/v27_fixed_chunk_resident_mvp_plan.md`。

---

## 目录

- [1. 当前结果分析](#1-当前结果分析)
- [2. Chase workload 的 chunk delta 为什么是 5 万–数十万 cycles](#2-chase-workload-的-chunk-delta-为什么是-5-万数十万-cycles)
- [3. 异常样本要不要剔除](#3-异常样本要不要剔除)
- [4. 当前 loss 是均值：per-core 信号被稀释 + mean collapse 风险](#4-当前-loss-是均值per-core-信号被稀释--mean-collapse-风险)
- [5. Loss 重设计（含微架构泛化）](#5-loss-重设计含微架构泛化)
- [6. 后续 action items](#6-后续-action-items)

---

## 1. 当前结果分析

### 1.1 调度器语义已生效

| trace | chunks | samples | commits | resident_events | resident/commit |
|---|---:|---:|---:|---:|---:|
| `W_chase_DRAM@c04`（完整）        | 9223 | 4721 | 9223 | 9661 | 1.05 |
| `W_chase_DRAM@c04`（budget=512）  | 9223 |  512 | 1235 |  813 | 0.66 |
| `W_stream_seq_DRAM@c04`（budget=512） | 9220 |  512 | 2012 |   36 | 0.018 |

- chase 高延迟 → 大量 resident（≈66% commit 都伴随 slow 保持）。
- stream 均匀 → 几乎无 resident（1.8%）。
- 这正是 plan §2 期望的 workload-differentiated 行为，说明 `E_min + ε` 判据可用。
- `max_exposure=2`，ε=500 与当前 chunk duration 分布匹配良好，未触及 plan §3.2 建议的 soft cap 16/32。

### 1.2 Fast/slow 分布

- 真机 c04（chase+stream 各 512 samples）：`fast=3247, slow=849 → slow_ratio ≈ 20.7%`。
- 合成 (fast_slow+mixed)：`fast=8, slow=1 → 11%`。

两个数量级都在健康区间；plan §8.2 要求报的 metric 全部上齐（fast/slow ratio、resident_exposure bucket、per-core MAPE 分位）。

### 1.3 30 步真机 warm-up + 2 epoch 合成 loss

真机 30 步：
```
loss=95.74  log_cpi=0.0065  delta=175.68  prefix=11.17  endpoint=9.25
cpi_mape p50/p90/p99 = 5.22 / 9.89 / 9.93   (mean 5.07)
endpoint_err p50/p90 = 7.33 / 7.34
prefix_drift  L4/L8/L16/L32 p50 = 6.17 / 7.01 / 7.23 / 7.16
```

- `log_cpi` 已经进入 0.006 量级，`delta_cycle` 项仍在 175，两者相差 4 个数量级。
- prefix drift 随 L 单调微涨、未发散，符合 plan §8.3 “不发散”通过条件的结构要求，数值层面待正式训练。

### 1.4 与 plan §8.3 通过标准的对照

| 通过条件 | 当前 | 状态 |
|---|---|---|
| 误差不随 rollout 单调发散 | prefix L4→L32 p50 6.17→7.16 | 结构 OK；数值待训练 |
| c16 chase p90 outlier ≪ 171% | 未做 c16；c04 未训练 | 待完整训练 |
| c32 p90 workload error < 41.55% | 未跑 c32 | 待完整训练 |
| 不用 true tick 决定 chunk/context | `commit_tick` 只在 label 里出现 | 结构 OK |
| `sum(delta_cycles)` 与 endpoint 守恒 | evaluator 已聚合 | 结构 OK；数值待训练 |
| static cache 重复编码数下降 | `StaticEmbeddingCache` 类已建；`encode_static` 未接线 | **未接线** |
| c32 吞吐 ≥ 0.5x baseline | 未测 | 待测 |

### 1.5 已识别短板

1. **Loss 尺度不均**（详见 §4）。
2. **static cache 未接线**：`TCSimModel.encode_static` 忽略 `cache` 参数；resident chunk `exposure≥1` 时应命中已缓存的 `h_static`，当前每次都重算。
3. **未做 ε sweep**：plan §2.6 要求 `{250, 500, 1000}` 都跑一次。

---

## 2. Chase workload 的 chunk delta 为什么是 5 万–数十万 cycles

先按 K=256 UOP/chunk 拆物理量级：

| workload 类型 | 平均 CPI | K=256 chunk delta |
|---|---:|---:|
| int_alu dense | 0.5 | 128 |
| stream L2 hit | 2   | 512 |
| stream DRAM（sequential，HW prefetcher 生效） | 4 | ≈ 1,000 |
| chase DRAM（pointer chase，prefetcher 打不到） | 40–200 | **10k – 50k** |
| chase 跨 socket / atomic dirty owner        | 300–800 | **75k – 200k** |

**结论：5 万–数十万 cycles/chunk 是 chase 的物理正常值，不是 bug，也不是“一次 DRAM = 数十万 cycles”。**

### 2.1 单次 DRAM 访问的实际成本

一次 LLC miss → memory controller → DRAM row activate + column read + fill LLC/L2/L1 → 唤醒 load。

- 端到端 **≈ 200–400 cycles**（server class，DDR4/DDR5，3–4 GHz）。
- 有 MC queueing、bank conflict、跨 socket / NUMA remote hit、migratory MESI transfer 时可以到 500–1500 cycles。

### 2.2 为什么 chase 一个 chunk 会累到 5–20 万 cycles

关键在 **MLP（memory-level parallelism）退化到 1**：

- OoO 核有 LSQ + MSHR（典型 10–16 个），stream / random DRAM 可以同时飞几十个 miss，把延迟 overlap 掉，摊到每 UOP 才便宜。
- Pointer chase 语义 `p = *p`：下一次访存地址依赖上一次 load 返回值，RAW 依赖串成长链，硬件既不能 speculate，也不能 prefetch → **实际同时在飞的 miss ≈ 1**。
- 每一条 chase load 都要**串行**吃满一次 DRAM 延迟：

  ```
  单次 chase load ≈ 300 (DRAM) + 20–80 (ROB/replay/TLB) = 350–500 cycles
  ```

- K=256 UOP 里通常每 3–5 条 UOP 就有一条 chase load，其它是 arith/branch/addr calc：

  ```
  chunk delta ≈ 60 loads × 400 cycles ≈ 24,000 cycles
  CPI ≈ 24000 / 256 ≈ 94
  ```

- chase_extreme / dirty-owner / walker DRAM miss 叠加时，单 load 上到 800–1200 cycles，chunk delta 到 6–15 万，CPI 到 200–600 之间。这也是 memory 里 `W_chase_dram@c16` 曾经触发 171.99% 灾难性误差的物理来源。

### 2.3 与 raw trace 对得上

`records.micro.jsonl` 单行例子里 `path_class=4`（≥LLC miss）、`i_walker_dram_misses=4`（iTLB walk 也打到 DRAM），单 uop 增量约 140 cycles，256 条平均起来 chunk delta 上 3–5 万完全合理。

### 2.4 对 loss / 泛化的启示

- chunk delta 分布右尾极重，跨 workload 跨越 5 个数量级 → **additive cycle loss 必须在 log 域**，线性 Huber 会被 chase tail 完全 dominate。
- CPI 在 log 域范围窄（chase 200 → log 5.3；alu 0.5 → log −0.7；跨度 6）→ 最适合做**主监督**。
- **MLP 是 chase vs stream 的核心可辨识特征**：模型能否学会 chase，本质是能否从 functional 输入恢复 load-to-load RAW 依赖链。当前 tokenizer 已带 `n_src / n_dst / producer_dists / producer_classes`，信号存在；如果模型学不出，第一件事就是确认这几个字段没有在 pipeline 中被丢掉。

### 2.5 自检 heuristic

用 evaluator 输出的 chunk delta 分布：

- 合理：chase workload p50 落在 20k–60k、p99 落在 100k–200k；stream p50 500–2000、p99 5k–10k。
- 异常：chase p99 到 100 万甚至 1000 万 → 可能 `tick_per_cycle` 配错（例如 gem5 用了 500 ps/tick 的 8 GHz clock）→ 回去核对 `uarch_profile.json`。

---

## 3. 异常样本要不要剔除

**结论：不能一刀切剔掉 CPI>10 的样本；但要区分层次、区分训练/评测。**

### 3.1 业务阈值 ≠ 训练分布

- 线上把 `CPI > 10` 视作性能告警是对的（说明真实服务撞上 chase / false sharing / hot line）。
- 训练目标不是“预测线上健康区”，而是“给定 functional trace + uarch，预测这段代码在这套微架构上会跑成什么样”。
- chase / hot atomic / false sharing 是**合法 workload family**（`W_chase_DRAM`、`W_false_sharing`、`W_coh_atomic_cas`），它们的 CPI 就是几十到几百。
- 若全剔除：训练分布只剩 alu / stream / L2-hit (CPI ∈ [0.5, 5])，模型完全没见过“陡峭 phase” → 部署遇 chase → 灾难性外推。**这正是 memory 记忆里 `W_chase_dram@c16 → pVr 171.99%` 的一个成因**。

### 3.2 分层过滤 + 分层加权

区分“合法 tail”与“真采集异常”：

- 合法 tail：chase / false sharing / atomic 反复 CAS，chunk CPI 稳定落在 [30, 300]。
- 真异常：ROI 端点错、`commit_tick` 回退、`n_uops` 掉到 1 但 delta_cycles 上万、单 chunk CPI ≈ 1e5（几乎必然是 boundary/drain 越界）。

具体落到 `tcsim/chunker/fixed_chunk.py:compute_chunk_labels`：

```python
def _quality_flag(row, n_uops):
    dc = row.get("delta_cycles")
    if dc is None or dc <= 0:
        return "invalid"
    cpi = dc / max(1, n_uops)
    if cpi > 1000:
        return "invalid"                # 物理不可能
    if cpi > 30:
        return "extreme_tail"           # chase_extreme / atomic hot line
    if cpi > 10:
        return "heavy"                  # chase / false sharing 主体
    return "normal"
```

- `invalid`：**训练硬剔**（`label_mask=0`，仍保 Exactly once cursor/commit 语义）。
- `extreme_tail`：训练降权 0.3；**eval 全权重**。
- `heavy`：正常权重（模型必须学会的 phase）。
- `normal`：正常权重。

**关键红线：train loss 可以 reweight，eval 报表不能 reweight。**Plan §8.2 要求报 p50/p90/p99，eval 降权等于自欺欺人。

---

## 4. 【历史归档，结论已废弃】当前 loss 是均值：per-core 信号被稀释 + mean collapse 风险

> 本节保留用于追踪首轮分析过程，不可作为实现依据。逐元素 `mean(Huber(error))` 的正负误差不会抵消；
> per-core normalization 也不能从相同输入创造核间差异。正确分析和新 loss 见 v27.2 规范第 5--7 节。

### 4.1 当前 loss 组成

`tcsim/train/losses.py:44-79` 的总 loss：

```
L = 1.00 * L_log_cpi          # log 域 Huber，δ=0.3
  + 0.50 * L_delta_cycle      # 线性 cycle 域 Huber / 1000
  + 0.50 * L_prefix           # Σ delta 前缀 Huber / (1000 * L)
  + 0.25 * L_endpoint         # Σ delta 端点 Huber / (1000 * N)
```

| 项 | 输入量 | 单位 | Huber δ | 归一化 |
|---|---|---|---:|---|
| `L_log_cpi` | `log(pred_cpi) - log(true_cpi)` | log-CPI | 0.3 | 无 |
| `L_delta_cycle` | `pred_delta - true_delta` | cycles | 1000 | `/1000` |
| `L_prefix` | `Σpred - Σtrue`（前缀 L 个 chunk） | cycles | 1000 | `/(1000*L)` |
| `L_endpoint` | `Σpred - Σtrue`（整核） | cycles | 1000 | `/(1000*N)` |

### 4.2 尺度不均：Huber 线性分支主导

- chase chunk `true_delta ≈ 50,000 cycles` 时，Huber `|x| > δ` 变线性：`huber(diff=25000, δ=1000) ≈ 1000 * 25000 = 2.5e7`。
- 除 1000 得到 2.5e4；一个 batch 里几十个这样的 chunk 平均出 `L_delta ≈ 100–200`。
- 相同相对误差（30%）在两个域产生 **300× 的绝对 loss 差**：log 域 ≈ 0.05，cycle 域 ≈ 15。
- 结论：**当前实际优化的几乎全是 cycle 项**，`log_cpi=0.006` 只是被动跟随，不代表 log 目标真的被学到。

### 4.3 mean 归约：per-core 与 per-chunk 信号都被稀释

看 `losses.py:44-50`：

```python
m = mask.bool()
delta_diff = (pred_delta - delta_true)[m]
l_delta = huber(delta_diff, 1000).mean() / 1000
```

`m` 是 batch 里所有核所有 committed chunk 的展平集合。后果：

**(a) 大 delta 核 dominate**：chase core 单 chunk 贡献 `≈ 1000 * 30000 = 3e7`；alu core 单 chunk 贡献 `≈ 0.5 * 10² = 50`。mean 里 chase 核占 99.99%，alu 核几乎无梯度。

**(b) mean collapse 会被主动奖励**：真实 `[core0=1000, core1=1010, core2=990, core3=1005]`，模型预测 `[1002, 1002, 1002, 1002]`（全预测均值），mean loss 只有 5，看起来很好；但 per-core spread=0，aggregate 完美，per-core p90 灾难。**这就是 memory 里 v26 结构性 mean collapse 的 loss-side 成因。**

**(c) 核内 chunk 之间同样被平均**：核 0 的 100 个 chunk 拉平取 mean，模型可以第 5 号严重错、第 95 号相反方向严重错，mean 抵消 → loss 小。

### 4.4 微架构泛化：CPI 还是 cycle 做主监督

Plan §5.4 的核心分解：
```
delta_cycles = intrinsic(uarch, functional chunk)
             + bounded interaction residual(shared/resource state)
```

两者都不天然架构无关，但训练目标选择上：

| 维度 | log-CPI | cycle (additive) |
|---|---|---|
| 数量级稳定 | ✅ 大多在 [0, 4] | ❌ 与 K 成正比、跨 workload 4 个数量级 |
| 尾 chunk / K 变化 | ✅ 天然不变 | ❌ K 改就重训 |
| 前缀 / makespan 可加 | ❌ 需换算 | ✅ 直接加 |
| 调度器直接可用（更新 T_pred） | ❌ | ✅ |
| 跨 uarch 迁移 | ✅ log 吸收 uarch scale bias | ❌ 绝对量随 uarch 线性缩放 |
| 与 CPI baseline 报表接口一致 | ✅ | ❌ |

**结论：主监督用 log-CPI，additive 约束在 log-cycle 域，cycle head 是派生量**。这样：

- 单点监督 → 跨 uarch 稳；
- prefix / endpoint 在 `log(Σ exp(log_cpi) * n_uops)` 与 `log(Σ true_delta)` 上做 → 保 additive 语义、量纲仍在 log 域；
- 报表用 exp 出来的 cycle 与 CPI 都能出。

当前代码的 `pred_delta = exp(log_cpi) * n_uops` 已经是派生结构，只需把 loss 域换掉即可。

---

## 5. 【历史归档，方案已废弃】Loss 重设计（含微架构泛化）

> 本节提出的“只改 loss、加 spread/per-core normalize”方案已被替代。当前方案使用 unique-chunk
> `L_abs`、high-spread gated `L_centered`、信息性 slow-core listwise loss，以及真正连续的 prefix loss；
> 首轮不使用 spread loss，不使用线性 raw-cycle Huber。

### 5.1 五条最小改动（全部落在 `tcsim/train/losses.py`）

**改动 1 — 单位统一到 log 域**：

```python
log_pred_delta = torch.log(pred_delta.clamp(min=1.0))
log_true_delta = torch.log(delta_true.clamp(min=1.0))
```
4 项 loss 全部 log 域，Huber δ 统一 0.3。

**改动 2 — per-core 归一化，杜绝 mean collapse**：

```python
def per_core_mean(vals, core_ids):
    buckets = {}
    for v, c in zip(vals, core_ids):
        buckets.setdefault(int(c), []).append(v)
    per_core = [torch.stack(g).mean() for g in buckets.values()]
    return torch.stack(per_core).mean()
```

每个核在 loss 里权重固定 1/N_core，与它跑得快慢无关。chase 核与 alu 核梯度可比，模型必须**同时**把两种 phase 学好。

**改动 3 — 加 per-sample cross-core spread 项**（直击 mean collapse）：

对每个 scheduler sample 的 active cores：

```python
true_spread = log_true_cpi - log_true_cpi.mean()   # 只保留“谁快谁慢”
pred_spread = log_pred_cpi - log_pred_cpi.mean()
L_spread    = huber(pred_spread - true_spread, 0.3).mean()
```

`L_spread` 强制模型学到**核间相对差异**，与 aggregate 层面正交，直接量化并惩罚 mean collapse。权重建议 0.5。

**改动 4 — long-tail 用样本加权，不用 loss 变形**：

```python
sample_weight = {"invalid": 0.0, "extreme_tail": 0.3, "heavy": 1.0, "normal": 1.0}
w = weights_from_flag(batch["quality_flag"])
l_log = (huber(log_diff, 0.3) * w).sum() / w.sum().clamp(min=1e-6)
```

- 不改 Huber δ、不改归一化域，只在样本级 reweight。
- 同一个 `w` 用于 log_cpi / delta / prefix / endpoint 四项，保证四个 loss 的样本分布一致。
- eval 端不加 `w`、不 per-core normalize，直接报 raw p50/p90/p99。

**改动 5 —（可选，Phase 4）per-core rank loss**：

```python
# 对同一 sample 的 core pair (i, j)：
L_rank += softplus( -sign(true_i - true_j) * (pred_i - pred_j) )
```

排序 loss 消掉 uarch scale bias，天然跨微架构友好。第一版可以先不加；等 mean collapse 缓解后作为 Phase 4 补丁。

### 5.2 最终 loss 建议

```
L = 1.0 * L_log_cpi_per_core_normalized     # 主监督：单点，按核归一
  + 0.3 * L_log_delta_per_core_normalized   # 冗余项，保 additive 语义
  + 0.5 * L_prefix_log_per_core             # 前缀 L∈{4,8,16,32}，log 域，per-core mean
  + 0.3 * L_endpoint_log_per_core           # 整核 makespan，log 域
  + 0.5 * L_cross_core_spread_per_sample    # 消 mean collapse
  (+ 0.3 * L_rank_pairwise                  # Phase 4：跨 uarch 增强)
```

- 所有 Huber δ = 0.3。
- 样本级 quality-flag 加权 `w`：`invalid=0`、`extreme_tail=0.3`、其余 1。
- eval 端**不加 `w`、不 per-core normalize**，直接报 raw p50/p90/p99。
- `pred_delta` 保持派生量 `exp(log_cpi) * n_uops`，不引入独立 delta head。

### 5.3 微架构泛化的落点

- 主监督选择 **log-CPI**：跨 uarch 相对差稳定、量纲不随 issue width / cache / clock 变。
- uarch 参数继续作为**显式输入**（plan §5.4 要求）。
- prefix / endpoint 在 log-cycle 域做 additive，报表用 exp 反算 cycle 与 CPI。
- 跨 uarch split 必须保留**至少一组完全 unseen 的 uarch/config**（plan §4.3）。

---

## 6. 【历史归档，优先级已废弃】后续 action items

优先级从高到低（预估 ROI 排序）：

1. **Loss 重设计**：按 §5.1 五条改 `tcsim/train/losses.py`；同步在 `tcsim/chunker/fixed_chunk.py` 加 `quality_flag`，在 `torch_dataset` 里透传。
2. **static cache 接线**：`TCSimModel.encode_static` 消费 `StaticEmbeddingCache`；训练时对 `exposure≥1` 的 resident chunk 命中缓存，只重算 dynamic interaction。上报 `cache_hit_rate` 到 metrics。
3. **ε sweep**：`{250, 500, 1000}` 各跑一次 rollout + 训练，报 fast/slow ratio、per-core p90、endpoint。
4. **正式训练**：≥ 5k step，四核 c04 起步，再扩到 c08/c16/c32；按 workload/trace/uarch group split。
5. **quality distribution 自检**：evaluator 输出 chunk delta 与 CPI 分布直方图，确认 chase p50/p99、stream p50/p99 落在 §2.5 的物理区间。
6. **Phase 4 增强**：per-core rank loss、resident K/V cache、bounded fast-chunk microbatch。
