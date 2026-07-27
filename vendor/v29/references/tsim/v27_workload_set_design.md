# TSim v27 训练负载集设计方案

状态：设计稿。本文档描述 v27 数据集重构的负载集合、gem5 生成策略、抽样规模，
用于替代 v26（`windows_v26_clean14_tail_local_*`）的负载定义与采样机制。

## 1. 背景与动机

### 1.1 v26 数据集诊断结论（已在数据上量化）

- **workload 内部不同构**：`W_chase_dram @ c16` CPI 分布 min=0.35 / p50=2.36 / max=22.7，
  同一 workload 内部横跨 L1-hit / L2-hit / DRAM-spill 三种物理机制。
- **关键窗口漏抽**：`W_chase_dram @ c16` 中 CPI∈[3,10] 的相变段只有 59 行，
  占该 slice 的 2.7%。抽样策略保不住相变窗口。
- **极端 CPI 塌到一个 workload**：CPI≥40 的 744 行 100% 来自 `W_false_sharing @ c16`。
- **c32 完全缺失**：训练集只覆盖 c01/c04/c08/c16，c32 是 OOD 外推。
- **cross-core 不均匀零覆盖**：所有 workload 都是均匀多核负载，真实业务
  常见的 hot/cold core skew 未被采样。
- **单 uarch**：cfg_hash 只有 A0，无法学 uarch 敏感度。
- **workload signature 分离度不足**：z-space L2 输入距离最近的两对
  workload 距离仅 7.88，label 空间有 7 个 workload 塌到 CPI≈0.4-0.7 单点。

### 1.2 v27 设计原则

1. **每个 workload 是"单一物理机制的实验"**：内部同构，稳态或相变本身各作
   为一个 workload。不允许一个 workload 里同时包含 L1-hit 段和 DRAM-spill 段。
2. **相变作为独立 workload 显式训练**：不靠模型对稳态两端外推。非线性排队
   现象（`waiting_time ~ 1/(1-utilization)`）不能由 Transformer 归纳偏置补出，
   必须直接见样本。
3. **四个正交轴定义 workload**：compute mix / memory pattern / coherence / temporal state。
4. **样本预算通过分级 slice cap 显式分配**，不做 loss reweight。
5. **多 uarch 用 pivot 设计**：每个 cfg 只改一个 uarch 参数，保证消融可归因。

## 2. 四轴正交空间

| 轴 | 取值 |
|---|---|
| **compute mix** | int-alu / int-mul / int-div / fp-alu / fp-fma / fp-div-sqrt / simd |
| **memory pattern** | reg-only / seq/L2 / seq/DRAM / rand/LLC / rand/DRAM / chase/LLC / chase/DRAM / mixed |
| **coherence** | none / read-share / write-share / atomic / producer-consumer / migratory |
| **temporal state** | steady / grow / onset / steady-nonuniform |

每个 workload 必须在这四轴上有唯一坐标；不允许一个 workload 内部沿某轴漂移
（除 temporal 是 grow/onset 的 phase workload 之外）。

## 3. 负载集合（20 个训练 + 2 个 held-out）

### 3.1 Compute-only（6 个）

学 ILP 上限和执行单元占用。所有 compute workload 的 memory pattern 锁在
`reg-only`（零 memory access），coherence 锁在 `none`。

| workload | compute | 预期 c01 CPI | 用途 |
|---|---|--:|---|
| **W_int_alu_dense** | 100% int-ALU，无长依赖 | 0.25 | ILP 上限锚 |
| **W_int_mul_dense** | 100% int-mul，独立乘法 | 0.4 | mul pipe |
| **W_int_div_dense** | 100% int-div | 3-5 | div slow path |
| **W_fp_alu_dense** | 100% fp-ALU 加减 | 0.4 | fp add/sub pipe |
| **W_fp_fma_dense** | 100% FMA 独立 | 0.3 | fma pipe |
| **W_simd_dense** | AVX2/AVX512 int/fp mix | 0.3 | simd pipe |

**删除说明**：不设 W_int_alu_serial（依赖链信号由 side_feats 里的
`dep_dist_mean_log`、`raw_chain_depth_p95` 承载，无需专门 workload）；
不设 W_fp_divsqrt_dense（生产极少见，与 fp_fma 强相关）。

### 3.2 Memory-only（6 个）

学 access pattern × cache level 的物理响应。memory workload 都用 int-ALU 骨架，
coherence 锁在 `none`（每核 partitioned working set）。

| workload | pattern | 工作集 | 预期 c01 CPI |
|---|---|---|--:|
| **W_stream_seq_L2** | 顺序 load/store | 256 KB (fit L2) | 1.5 |
| **W_stream_seq_DRAM** | 顺序 | 64 MB (spill DRAM) | 12 |
| **W_random_LLC** | uniform random | 4 MB (fit LLC) | 8 |
| **W_random_DRAM** | uniform random | 64 MB | 15 |
| **W_chase_LLC** | pointer chase | 4 MB | 6 |
| **W_chase_DRAM** | pointer chase | 64 MB | 20 |

**关键**：工作集大小是 workload 的一部分，不是内部 phase。用 gem5 fixed
working set generator 保证不漂移。

**删除说明**：不设 W_stream_seq_L1（≈ W_int_alu_dense）、W_stream_seq_LLC
（可由 stream_DRAM 稳态外推）、W_stream_stride_LLC（stride 由 side_feats
里的 `stream_stride_ratio` 承载）、W_random_L2（低压段留给 chase_LLC）。

### 3.3 Coherence（5 个）

学多核干扰。memory pattern 锁在 seq/L1，改 coherence 强度。

| workload | 模式 | 预期 c16 CPI |
|---|---|--:|
| **W_coh_ws** | 所有核写同一 cache line（stride tuned by ncore） | c01=0.6, c04=6, c16=30 |
| **W_coh_read_share** | 所有核只读同一 4KB 区 | 0.6 |
| **W_coh_atomic_cas** | 所有核 CAS 同一 atomic | 20 |
| **W_coh_prod_cons** | 1 core produce, N core consume | 5 |
| **W_coh_migratory** | 每核轮流写同一 line (migratory pattern) | 8 |

**关键设计**：`W_coh_ws` 一个 workload 覆盖多个争用强度——通过 `core_count`
轴自然遍历（c01=无争、c04=4 核 mild、c16=16 核 heavy、c32=32 核 pathological），
不再人工分档为 `W_coh_ws_2core / 8core / 16core`。

### 3.4 Phase transition（2 训练 + 2 held-out）

**训练用**：

| workload | 相变机制 | CPI 轨迹 |
|---|---|---|
| **W_phase_ws_grow** | 工作集从 8KB 每 100k uop 翻倍到 8MB | 0.5 → 2 → 8 单调爬 |
| **W_phase_coh_onset** | 前 200k uop 无争用，后 200k uop 突然 all-core ping-pong | 0.5 → 30 阶跃 |

**Held-out（不进训练）**：

| workload | 相变机制 |
|---|---|
| **W_phase_ws_shrink** | 工作集从 8MB 每 100k uop 减半到 8KB |
| **W_phase_coh_decay** | 前 200k uop ping-pong，后 200k uop 争用解除 |

Held-out 用于测试模型是否学到相变**机制**而非记忆 grow/onset 方向。

### 3.5 Non-uniform（2 个）

学 cross-core 不均匀。v26 完全缺失的场景。

| workload | 特征 | 预期 |
|---|---|---|
| **W_skew_hot_cold** | 25% 核跑 stream-DRAM，75% 核跑 int-ALU | hot core CPI=12, cold core CPI=0.4 |
| **W_skew_producer_amp** | 1 核 heavy compute，其余核 idle 轮询 | 一核 CPI=1, 其余 CPI=0.3 |

### 3.6 汇总

| 类别 | 数量 | 训练 |
|---|--:|:-:|
| Compute-only | 6 | ✓ |
| Memory-only | 6 | ✓ |
| Coherence | 5 | ✓ |
| Phase (进训练) | 2 | ✓ |
| Non-uniform | 2 | ✓ |
| **训练合计** | **21** | |
| Held-out validation | 2 | — |

## 4. gem5 硬件配置矩阵

### 4.1 uarch cfg（5 个）

pivot 设计，每个 cfg 只改一个变量：

| cfg | 改动 vs A0 | 目的 |
|---|---|---|
| **A0** | baseline (现 v26 使用) | 主训练配置 |
| **A1** | L2 latency 12→24 cycle | memory sensitivity |
| **A2** | ROB 128→64 | backend sensitivity |
| **A3** | LLC 8MB→4MB | cache sizing sensitivity |
| **A4** | DRAM latency 100→200 cycle | 极端 memory pressure |

需要在 `config/uarch_configs.yaml` 补齐 A1-A4。

### 4.2 core count

`{c01, c04, c08, c16, c32}` 全覆盖。c32 从 v26 的 OOD 变为 in-distribution。

### 4.3 立方体规模

- **v27.0**：A0 × 5 core × 20 workload = **100 gem5 run**（作为首轮）
- **v27.1**：+A1 pivot = 200 run
- **v27.2**：全 uarch (A0-A4) = 500 run

## 5. 样本预算

### 5.1 分级 slice cap

按 workload signature 复杂度分档：

| 类别 | 每 slice 目标窗口 | c01 加权（×1.2） |
|---|--:|--:|
| Compute-only | 1500 | 1800 |
| Memory-only | 2500 | 3000 |
| Coherence | 3000 | 3600 |
| Phase | 4000 | 4800 |
| Non-uniform | 2500 | 3000 |

**c01 加权 1.2×**：c01 是"无多核干扰"基线锚点，多采一点有助于模型学到
core count → CPI 的曲线起点。

### 5.2 总窗口数

单 uarch (v27.0)：

| 类别 | workload 数 | 每 wl 五个 core 合计 | 单类总量 |
|---|--:|--:|--:|
| Compute | 6 | 7800 | 46,800 |
| Memory | 6 | 13,000 | 78,000 |
| Coherence | 5 | 15,600 | 78,000 |
| Phase | 2 | 20,800 | 41,600 |
| Non-uniform | 2 | 13,000 | 26,000 |
| **v27.0 总计** | | | **~270k** |

多 uarch 阶段：

| Stage | 覆盖范围 | 总窗口 |
|---|---|--:|
| **v27.0** | A0 × 5 core × 20 workload | 270k |
| **v27.1** | +A1 | 540k |
| **v27.2** | 全 uarch A0-A4 | 1.35M |

### 5.3 与 v26 对比

| 维度 | v26 | v27.0 | 备注 |
|---|--:|--:|---|
| workload 数 | 16 | 20 | +25% |
| core count 数 | 4 | 5 | +c32 |
| uarch 数 | 1 | 1 (v27.0) → 5 (v27.2) | pivot 消融 |
| 总窗口 (单 uarch) | 220k | 270k | +23% |
| 每 slice 平均 | 3438 但严重不均 | 2700 且均衡 | 稀有 slice 修复 |
| 稀有 slice 最少样本 | 584 (c01 W_compute_int) | ≥1500 | 2.6× |
| CPI 3-10 中段样本 | 59 行 (2.7%) | ~20k 行 (~7%) | 340× |
| CPI≥40 来源 workload 数 | 1 | 5 | 分散化 |

## 6. 抽样策略

沿用 v26 现有 `build_windows.py`，只需调参：

- 每 (workload, core, cfg) 生成 raw window 上限：5000 → **8000**（更充分池）
- `--per-workload-cap`：按类别分档设定（见 5.1）
- **稳态类 workload**：dedup 阈值 0.03（更严格去重，因为本来就同质）
- **Phase workload**：**dedup 关闭**，保留全部 raw 窗口（相变每个都独特）
- **Coherence workload**：dedup 阈值 0.05（现状）
- `query_placement`：保持 `tail_local`

## 7. 训练侧配合

### 7.1 Workload embedding

20 个 workload 各自一个 embedding + 加一个 5 类的 workload class embedding
（compute/memory/coherence/phase/non-uniform）。让模型学 family 共性，避免
纯 workload_id shortcut。

### 7.2 Loss balancing

**不加 slice reweight**：预分配的 slice cap 已使样本预算和 signature 复杂度
匹配，让 natural loss surface 反映真实物理。

### 7.3 Held-out validation

`W_phase_ws_shrink` 和 `W_phase_coh_decay` 不进训练集，独立跑 gem5 用于
测试相变机制泛化。关键指标：predicted CPI trajectory 是否呈现方向翻转的
物理正确形态。

### 7.4 uarch 泛化（v27.1+）

一旦 A1 数据到位，启用 paired-counterfactual loss（对齐 LLMSim v27 delta-uarch
objective）：

```
L_delta = loss(pred(A0) - pred(A1), label(A0) - label(A1))
```

同一 workload × 同一 core count × 两个 cfg 作为一组 pair，学 uarch 参数敏感度。

## 8. 与 v26 的能力对照

| 能力 | v26 | v27 |
|---|:-:|:-:|
| 稳态 workload 内部同构 | ✗ | ✓ |
| CPI 中段 (3-10) 有充分样本 | ✗ | ✓ |
| c32 in-distribution | ✗ | ✓ |
| cross-core 不均匀 | ✗ | ✓ |
| 相变作为显式 workload | ✗ | ✓ |
| held-out workload 泛化验证 | ✗ | ✓ |
| 多 uarch 训练 | ✗ | ✓ (v27.1+) |
| delta-uarch paired loss | ✗ | ✓ (v27.1+) |
| 极端 CPI 分散到多 workload | ✗ | ✓ |

## 9. 落地时间线

| Stage | 内容 | gem5 wall-clock | 产物 |
|---|---|---|---|
| **P0** | 写 20 个 workload C 代码 + 补 A1-A4 uarch cfg + 修改 collect script | 2 天 | 可跑 |
| **P1** | 跑 v27.0 (A0 × 100 run) | 3-5 天 | 270k 窗口 |
| **P2** | build_windows 生成窗口 + 训练首轮验证 | 1 天 | v27.0 训练完成 |
| **P3** | 跑 A1 pivot (100 run) | 3-5 天 | v27.1 数据 |
| **P4** | delta-uarch paired 训练 | 1 周 | v27.1 训练完成 |
| **P5** | 补 A2-A4 (300 run) | 10-15 天 | v27.2 全量 |

## 10. 验证清单

数据集生成后必须过这三关：

1. **签名分离度**：所有 workload pair 的 z-space L2 输入距离 ≥ 10（v26 最小 7.88）
2. **CPI 分布合理**：
   - 每个 workload 内部 CPI IQR ≤ 3（除 phase workload）
   - CPI 3-10 段样本 ≥ 5% 总量
   - CPI≥40 样本来源 workload 数 ≥ 3
3. **loss 均衡度**：任一 (workload, core) slice 的 MAE 占比 ≤ 该 slice 样本占比 × 3

## 11. 备注

- 本文档只覆盖训练数据集设计，不涉及模型架构改动（继续用 v26 KVQR
  或 v27 shared system state）。
- Held-out workload 的 gem5 run 也需要跑，只是不进 build_windows 的 train
  集，独立生成 `windows_v27_heldout_*` 目录。
- 若 gem5 预算紧张，v27.0 可先跑 A0 × 20 workload × c01/c04/c16 三档（40 run）
  作为 dry-run 验证 pipeline，通过后再扩展到 5 档。
