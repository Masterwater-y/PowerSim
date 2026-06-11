# 12 - TAO 原文 vs 本框架 吞吐量对比分析

> 编写日期：2026-06-10
> 目的：回答"TAO 论文能做到 ~2 MIPS，我们当前只有 ~10k rows/s，差距 ~190× 的根因是什么、能否追平"
> 数据来源（本框架）：`runs/fastenc_on_121941`、`runs/kmax_512_140628`、`runs/fastenc_prof_*`
> 数据来源（TAO）：arXiv 全文 v2 https://arxiv.org/html/2404.10921v2（对应 ACM DOI 10.1145/3656012, POMACS Vol.8 No.2 Article 28）

---

## 0. TL;DR

- **数字不可直接对比**：TAO 是 **单核、无 cache coherence** 的无状态批推理；本框架是 **多核、带逐 quantum MESI 一致性闭环** 的串行状态机。差距主要由**架构性取舍**而非工程实现造成。
- TAO 论文实测 **1.98 MIPS @ 4×A100-80GB**（注意：不是 1.9 MIPS，"1.9 hours" 是它的训练耗时，易混淆）。
- 本框架当前 **10,473 rows/s @ 1×H20**（FASTENC 开启），已确认到 10k 量级，bit-exact。
- ~190× gap ≈ **GPU 利用率(~1.6×) × 每样本算力(模型/上下文) × 多卡近线性(~4-8×) × 单核 vs 多核一致性开销**。
- 加 batch / quantum / 显存 **无法**缩小 gap——本框架已处在自身吞吐 plateau 上（实测 k_max 翻 4× 仅 +6%）。

---

## 1. 两套系统的本质差异

| 维度 | TAO（论文） | 本框架 |
|---|---|---|
| 仿真对象 | **单核**乱序超标量 | **多核**（实测 16 core） |
| Cache coherence | **不建模**（明确不支持多核，全文无 MESI） | **逐 quantum MESI 一致性闭环** |
| 推理范式 | 无状态批推理：trace 切 subtrace 并行 | 有状态串行：quantum N 的 commit 写 MESI → quantum N+1 的 probe 读 |
| 指令间反馈 | 无（self-attention 自主学习上下文，去除 fetch-cycle 上下文队列） | 有（phase1c 写 clock/MESI/win → phase1a 读） |
| 上下文长度 | N+1 = 129（N=128=ROB） | context_len = 128 |
| 模型 | 两级嵌入 + 多头自注意力（层数/维度未披露） | d_model=256, n_layer=6, n_head=8, d_ff=1024 |
| 预测指标 | latency / branch mispred / D-cache miss（可扩展 I-cache/TLB） | fetch/exec lat / mispred / cache / coherence |
| 硬件 | 4×A100-80GB | 1×H20 |
| 吞吐 | **1.98 MIPS** | **10,473 rows/s** |

> 关键引文（TAO，arXiv v2 §6 Generality）：
> "TAO is designed to simulate **single-core** out-of-order superscalar processors. … Tao **cannot be directly used to simulate multi-core** CPU and GPU architectures."
> 全文未出现 MESI / cache coherence。

---

## 2. 本框架当前吞吐（已确认 10k 量级）

| 指标 | 值 |
|---|---|
| **rows/s** | **10,473.3** |
| wall | 152.8s |
| rows | 1,600,000（16 core × 100k） |
| GPU | 1 × NVIDIA H20 (97GB), bf16 autocast |
| ckpt | v10_3_fetchdecomp.best.pt |
| fetch gate | soft, temp=15 |
| cpi_pred | 1.4185（bit-exact，与 baseline 逐位一致） |
| precision / recall | 0.4750 / 0.0870（bit-exact） |

### 2.1 复现命令

```bash
cd <MTAO>/tao_cpu_sim
TAO_INFER_FASTENC=1 RUN_ROOT=runs/repro_$(date +%H%M%S) \
  bash scripts/07_validate_w11_w15_100k.sh \
    --num-cores 16 --rows-per-core 100000 \
    --fetch-gate-mode soft --fetch-gate-temp 15
# 读 RUN_ROOT/summary.tsv 的 rows_per_s 列
```

### 2.2 已开启的关键参数 / 开关

| 开关 | 值 | 作用 |
|---|---|---|
| `TAO_INFER_FASTENC` | **1** | 向量化预编码 enc（方案 a），关掉即回退 baseline (7437 rows/s) |
| `TAO_INFER_DEVICE` | cuda | GPU 推理（H20） |
| `--fetch-gate-mode` | soft | fetch 软门控 |
| `--fetch-gate-temp` | 15 | 门控温度 |
| `--num-cores` | 16 | 16 核并行 |
| `--rows-per-core` | 100000 | 每核 100k |
| `--k-max`（默认） | 128 | 投机深度（=batch 上限因子） |
| bf16 autocast | 自动开（CUDA 下 `use_cuda_amp=True`） | fwd 已是 bf16+TF32 |

> 从原始 7437 → 10473 rows/s（+40.8%）由 FASTENC（IO build 25× / enc 6×）贡献，全程 bit-exact。

### 2.3 batch 旋钮已榨干（实测证据）

| k_max | rows/s | speedup | cpi_pred | 结论 |
|---|---|---|---|---|
| 128 | 9783.5 | 1.00x | 1.4185 | baseline |
| 256 | 9857.1 | 1.01x | 1.4185 | 几乎无增益 |
| 512 | 10356.7 | **1.06x** | 1.4185 | GPU 已饱和，sublinear 尾部 |

> 说明：batch 上限 = `k_max × num_cores`，实测 `avg_B=2046≈2048` 已打满；`--model-batch-size` 被自动顶到 fresh_cap（inference_driver.py L1668），调大无效；显存填满无效（batch 不由显存定）。**本框架已在自身吞吐 plateau 上。**

---

## 3. 为什么加 batch 不能追平：throughput plateau 原理

GPU 饱和后，单次 forward 时间随 batch **线性增长**，于是：

```
吞吐 = batch / forward_time(batch) ≈ batch / (k × batch) = 1/k = 常数（plateau）
```

→ 加 batch 把你推到**自己的 plateau**（已到），但**不抬高 plateau**。TAO 同样在 plateau 上，差别是**它的 plateau 更高**。抬高 plateau 只有三条路：① 降每样本算力 ② 提 GPU 利用率 ③ 多卡近线性——恰好都是 TAO 因"不做 coherence 闭环"而免费拥有的。

---

## 4. ~190× gap 因子分解

| 因子 | TAO 优势来源 | 估计倍数 | 性质 |
|---|---|---|---|
| GPU 利用率 | 无 CPU 串行段打断；本框架实测 fwd 43% + h2d 18% + CPU 串行 39%，GPU ~40% 时间在等 CPU 写 MESI | ~1.6× | 架构性 |
| 每样本算力 | self-attention O(L²)，L≈128 两边接近；但 TAO 模型层数/维度未披露，可能更轻 | 未知（保守 1~5×） | 模型规模 |
| 多卡近线性 | 无状态批推理可 trace 切片 N 卡近线性；本框架 quantum barrier 使多卡仅 +20%（已实测负优化回退） | ~4-8×（卡数） | 架构性 |
| 单核 vs 多核一致性 | TAO 单核无 MESI；本框架每 quantum 跨核写共享目录 | 并入"利用率/串行" | 架构性 |

> 注：因子相乘的量级（1.6 × ~5 × ~4-8 ≈ 30~60×/卡数差），加上 4×A100 vs 1×H20 的卡数与显存差，可定性解释 1.98M vs 10k 的 ~190× 量级。**模型规模因子因论文未披露层数/d_model，无法精确量化，文中标注为估计。**

---

## 5. 本框架的串行依赖（gap 的核心，无法靠调参消除）

来自 `docs/10` 与 `inference_driver.py`：

```
fwd(N) ─在─ phase1b(N) ──► phase1c(N) 写 shared MESI/directory/clock/win
                                  └──► phase1a(N+1) 读+写 同一 shared 状态
```

- **quantum 间串行**：N+1 的 probe 必须等 N 的 commit 写完 MESI（commit_quantum_pod）。
- **phase1a 跨核串行**：probe 直写 shared MESI，跨核顺序必须稳定（L1704-1705）。
- 这两条决定了 docs/10 给出的**软上限 ~30k-45k rows/s**（单卡），以及多卡近线性不可行。
- 已验证否决项：**跨 quantum GPU/CPU overlap 会让 probe 读到未提交 coherence 状态 → cpi 漂移 → 破坏 bit-exact**，不实施。

---

## 6. 现实可达路线（bit-exact 安全）

| 项 | 段 | 预期 | 风险 | 状态 |
|---|---|---|---|---|
| FASTENC（已落地） | IO+enc | 7437→10473 (+40.8%) | 零 | ✅ 默认可开 |
| k_max=512 | batch | +6% | 零（实测 cpi 不动） | 可设默认 |
| pinned h2d | h2d 18% | +5~10% | 零 | 待实施 |
| CUDA Graph | fwd 43% | +5~7% | 中（shape 固定） | 待评估 |
| 多卡数据分片 | fwd | docs 估 +20%（需先把 CPU 段砍到 <50s） | 中 | 前提未满足 |

> 综合 bit-exact 安全项，单卡现实上限约 **15k-22k rows/s**，仍距 TAO 量级有数量级差距——因为差距是架构性的（多核一致性 vs 单核无状态）。

---

## 7. 结论

1. **当前已确认到 10k 量级**：10,473 rows/s @ 1×H20，FASTENC 开启，bit-exact，复现命令见 §2.1。
2. **batch/quantum/显存 已榨干**：实测处于 plateau，k_max 翻 4× 仅 +6%。
3. **gap 是架构性的，不是调参可跨越的**：TAO 单核无 MESI 的无状态批推理 vs 本框架多核逐 quantum 一致性闭环。追平 TAO 量级需放弃 coherence 闭环（丢失跨核低层指标精度），这是产品/精度取舍，非纯工程问题。
4. **下一步建议**：落 k_max=512 + pinned h2d（零风险 +11~16%），可达 ~12k；是否追求更高需先决策 coherence 闭环精度取舍。

---

## 附：数据出处

- 本框架吞吐：`runs/fastenc_on_121941/summary.json`（10473 rows/s, cpi 1.4185）
- k_max 扫描：`runs/kmax_{128,256,512}_*/summary.json`
- phase 级 profile：`runs/fastenc_prof_*/W11_stream_mix/stderr.log`（phase1a 14.4% / phase1b 81% / fwd 43.5% / h2d 18.3%）
- 模型配置：`infer/ml/model.py` L34-40
- TAO 论文：arXiv v2 https://arxiv.org/html/2404.10921v2 ；ACM DOI https://doi.org/10.1145/3656012
