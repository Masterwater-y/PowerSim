# LLMSim 数据采集方案（泛化性导向）

> 目标：采集覆盖**不同微架构特征维度**的多核 functional trace + µarch 标签，
> 使窗口级 PMU 预测模型具备跨 workload 泛化能力，而非过拟合单一负载。

---

## 1. 设计原则

1. **特征维度覆盖**：训练集必须在以下正交维度上都有样本，否则模型学不到对应 PMU 的因果：
   - 计算密集（ALU / FP / SIMD / 长依赖链 / 除法）
   - 访存模式（顺序流 / 随机指针追踪 / 跨步 / 工作集大小）
   - 控制流（分支密集 / 状态机分支 / 间接跳转 / 间接派发）
   - 多核一致性（共享行读写 / 全 coh 路径压力 / false sharing）
2. **训练 / 泛化分离**：留出 holdout workload 做 leave-one-workload-out，验证真泛化。
3. **窗口多样性**：多尺度窗口 + 多核组合 + 排除主线程 core0。
4. **标签纯净**：输入只 functional，µarch 仅作标签（已在 build_windows 保证）。

---

## 2. Workload 特征分类（taogen 现有 18 个）

| 类别 | workload | 主导 PMU 特征 | 用途 |
|---|---|---|---|
| **ALU 计算** | mt_compute_int | 低 CPI、低 miss、高 IPC | train |
| **整数除法** | mt_int_div | 长延迟执行单元、低 IPC | train |
| **FP/SIMD** | mt_simd_fp | FP/SIMD 长依赖链、execution stall | train |
| **DRAM 随机** | mt_chase_dram | 高 LLC miss、高 CPI、指针追踪 | train |
| **顺序流** | mt_stream | 高带宽、prefetch 友好、中等 miss | train |
| **混合流** | mt_stream_mix | 读写混合带宽 | train |
| **跨步预取** | mt_stride_pf | stride prefetch、TLB 压力 | train |
| **2D stencil** | mt_stencil2d | 邻域复用、cache 局部性 | train |
| **图遍历** | mt_graph_walk | 不规则访存、大工作集、W1024 失效 | train |
| **分支风暴** | mt_branch_storm | 高 branch miss、控制流抖动 | train |
| **分支状态机** | mt_branch_state_machine | 规律分支、BTB/TAGE 行为 | train |
| **间接跳转** | mt_indirect_jump | 间接分支 misprediction | train |
| **间接派发** | mt_indirect_dispatch | 虚函数式间接派发 | train |
| **微 coh** | mt_micro_coh | 共享行读写、MESI 转移 | train |
| **coh 压力** | mt_coh_stress | 全 coh 路径、invalidation 风暴 | train |
| **混合服务** | holdout_mixed_service | 真实混合负载 | **holdout** |
| **分片 KV** | holdout_sharded_kv | KV 索引访存 | **holdout** |
| **分析扫描** | holdout_analytics_scan | scan-transform | **holdout** |

→ **15 train + 3 holdout**，覆盖全部 4 个正交特征维度。

---

## 3. 采集参数

### 3.1 单次 run 配置
- `--num-cores 4`（4 核，多核耦合 + 排除 core0 后仍有 3 worker 核）
- `--require-roi`（ROI 闸门，只采稳态、无冷启动）
- 单配置 arch_A（Phase0/1）；Phase2 扩 4 配置
- 每 workload 调参使 ROI 内 µop 数 ≥ 20 万/worker 核（保证足够窗口）

### 3.2 窗口切分（build_windows）
- **排除 core0 主线程**（需给 build_windows 加 `--skip-core0`），用 core1/2/3
- 多尺度：W ∈ {256, 512, 1024}，stride = W/4（75% 重叠增广）
- 预计每 workload 每尺度产出窗口数：
  - 20 万 µop / (W=256, stride=64) ≈ 3000 窗口/核 × 3 核对齐 → 但多核对齐取 min，约 3000 窗口
  - 三尺度合计每 workload ≈ 4500 窗口

### 3.3 数据规模目标

| 阶段 | workload 数 | 配置 | 窗口/workload | 总窗口 |
|---|---|---|---|---|
| Phase0（已完成） | 1 | 1 | ~34 | ~34（管线验证） |
| **Phase1** | 15 train | 1 | ~4500 | **~67K** |
| Phase2 | 15 train | 4 | ~4500 | ~270K |
| holdout | 3 | 1~4 | ~4500 | ~13K~54K |

Phase1 的 67K 窗口足以让 0.6B + LoRA 学到跨 workload 的 PMU 因果。

---

## 4. 采集执行（防中断）

关键教训：之前采集被终端复用中断。正式采集必须用 `setsid` 完全脱离 + 串行单进程跑，
避免多 gem5 并发抢占被 OOM/调度 kill。

```bash
# scripts/collect_full.sh 串行跑 15+3 workload，每个独立 outdir
setsid bash scripts/collect_full.sh > data/raw/collect_full.log 2>&1 < /dev/null &
disown
```

### 4.1 单 workload 耗时（实测 W1：4 核 800 iter）
- compute_int 4 核约 **8~12 分钟**产出 ~10 万 µop/核
- 访存密集型（chase_dram / graph_walk）更慢，约 **15~25 分钟**
- 估算单 workload 平均 **15 分钟**

### 4.2 全量采集时间（串行）
- 18 workload × 15 min ≈ **4.5 小时**（单配置 Phase1）
- 若并行 4 个 gem5（机器核多）→ 约 **1.5 小时**，但需监控内存

---

## 5. 预期训练时间

基于已实测的 8 卡吞吐：**8.3 samp/s @ 12307 token/样本（W=512×4核）**。

换算到不同规模：

### 5.1 吞吐基准（实测外推）
- 8 卡全局 102K tok/s
- W=512 样本 12307 token → 8.3 samp/s
- W=256 样本 ~6200 token → ~16 samp/s（序列减半，吞吐近翻倍）
- W=1024 样本 ~24600 token → ~4 samp/s

### 5.2 Phase1 训练时间估算（67K 窗口，混合尺度）

| 设置 | 全局 batch | 样本/s | 1 epoch 时间 | 推荐 epoch | 总时间 |
|---|---|---|---|---|---|
| W=256 主力（~45K 窗口） | 64 | ~16 | ~47 min | 8 | **~6.3 h** |
| W=512（~15K 窗口） | 32 | ~8 | ~31 min | 8 | ~4.1 h |
| W=1024（~7K 窗口） | 16 | ~4 | ~29 min | 8 | ~3.9 h |

**实际建议**：Phase1 先用单尺度 W=512、67K 窗口、bf16、8 卡：
- 67000 / 8.3 ≈ 8072 s/epoch ≈ **2.24 h/epoch**
- 收敛通常需 6~10 epoch → **预计 13~22 小时**

若用 W=256 为主力（吞吐翻倍）：
- 67000 / 16 ≈ 4187 s/epoch ≈ **1.16 h/epoch**
- 6~10 epoch → **预计 7~12 小时**

### 5.3 加速手段（可把训练压到 1/2~1/3）
1. **flash-attention-2**：长序列 attention 提速 2~3×（需装 flash-attn）
2. **冻结 embedding 大表**：只训新增 1470 token 的 embedding 行，trainable 从 166M 降到 ~5M，显存与反向更快
3. **梯度累积 + 更大 micro-batch**：H20 96GB 可塞 bs=4~8（W=256）
4. **packing**：把短样本拼接到固定长度，减少 pad 浪费

采用 (1)+(2) 后，Phase1 预计可压到 **4~8 小时**。

---

## 6. 落地步骤

1. 给 [build_windows.py](file:///data00/yinhaolang/LLMSim/data/build_windows.py) 加 `--skip-core0` 与多尺度批量切窗
2. 写 `scripts/collect_full.sh`（串行 18 workload，参数见 §2）
3. `setsid` 后台采集（~4.5h 单配置）
4. 切窗生成 ~67K 窗口 jsonl
5. 8 卡训练（W=512，~13~22h；或 W=256 + 优化 ~4~8h）
6. holdout 3 workload 做 leave-one-workload-out 评估 CPI MAPE

---

## 7. 泛化性验证口径

- **同分布 val**：train workload 内 90/10 切，CPI MAPE 目标 < 15%
- **leave-one-workload-out**：3 个 holdout 完全不参与训练，CPI MAPE 目标 < 30%
- **per-PMU**：branch_miss / llc_miss 等分别报 MAPE，确认不是只学会 CPI 均值
