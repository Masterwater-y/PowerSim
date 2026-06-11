# 11 - 推理吞吐量优化阶段性总结（截至 2026-06-09）

数据快照：`runs/profile_W11_optA2_100k_20260609_173111`（baseline）

## 1. 当前性能

| 指标 | 值 |
|------|-----|
| wall | 215.14s |
| rows | 1,600,000 |
| **rows/s** | **7437** |
| 数据 | W11_stream_mix, NUM_CORES=16, ROWS_PER_CORE=100K |
| GPU | 1 × H20，bf16 autocast |
| ckpt | v10_3_fetchdecomp.best.pt |

精度指标全部 bit-exact 通过：cpi_pred=1.4115653351985364, precision/recall 与原 baseline 一致。

## 2. 已落地的优化（bit-exact）

| 修复 | 改动点 | 收益 |
|---|---|---|
| **A2.g1**：`_HistSoA.append` 整行 list 赋值 | inference_driver.py L513-L523 | g1_append -8.55s |
| **A2.g4**：去掉 `np.ascontiguousarray`，让 `from_numpy(view).long()` 一次完成 stride+dtype 转换 | inference_driver.py L713-L720 | g4_to_tensor -35.21s |
| 合计 | | **wall 253→215s, rows/s 6326→7437 (+17.8%)** |

## 3. 已尝试但回退的优化

| 维度 | 尝试 | 结果 | 根因 |
|---|---|---|---|
| **A.1** | `_materialize_row_dicts` 用 ThreadPool 并行 16 core | 负优化 -7.6%（rows/s 7437→6875） | `pq.read_table().to_pylist()` + `rows.sort` 都是 GIL-bound，Threading 触发 GIL 争用 + threadpool oversubscription |
| **C** | 维度 C 多 GPU 数据分片（4 卡，dim=0 切 batch，每卡独立 stream） | bit-exact ✅ 但 wall 215→237s 负优化 -10% | fwd 仅占 wall 24%，多卡分片节省的 fwd 时间被 h2d 4× / launch overhead / 小 batch GEMM 利用率↓ 全部吃掉 |

## 4. 多卡分片为什么没效果（核心结论）

### 4.1 时间分布（baseline 215s）

```
phase1a       20.6s ( 9.6%)  CPU only
phase1b enc   64.4s (30.0%)  CPU + numpy
phase1b h2d   12.5s ( 5.8%)  PCIe
phase1b fwd   52.6s (24.5%)  GPU
phase1b d2h    0.4s ( 0.2%)
phase1c        5.5s ( 2.6%)  C++ ref_sim
其他          59.0s (27.4%)  Python loop / IO
```

### 4.2 Amdahl 上限

只有 fwd 是真正可并行的部分（h2d 在 PCIe 串行；其他都是 CPU 单线程）。

```
P (可并行占比) = 52.6 / 215.14 ≈ 0.24
4 卡理论加速比上限 = 1 / ((1 - 0.24) + 0.24/4) = 1.21
即 wall 215s → 178s 是 4 卡上限
```

实测 wall 237s 反而比单卡慢，是因为：
- h2d 4× 串行（4 个 device.to() 顺序提交，PCIe 共享）
- 4 次 stream.synchronize() barrier
- 切片后 batch B=2046→511，cuBLAS GEMM SM 利用率从 60% 降到 30%
- 4 套 cuBLAS workspace 初始化

### 4.3 多卡有效的前提（**当前 4 个全不满足**）

| 前提 | 当前状态 |
|---|---|
| fwd 占 wall ≥ 50% | ❌ 24% |
| h2d 数据量小 | ❌ ~100MB / batch |
| Python loop overhead 不在关键路径 | ❌ enc/phase1a 是关键路径 |
| 单卡 GPU saturate | ❌ 利用率 < 50% |

**结论：必须先把 CPU 段砍到 < 50s，GPU 才会变成瓶颈，那时多卡才能贡献 ≥ 20%。**

## 5. CPU 段开销结构

CPU 段总 ~135s = wall 的 63%，按性质拆解：

```
├── pyarrow → python 反序列化           ≈ 45s  (33%)
│   └── pq.read_table.to_pylist()      45s
│
├── Python 解释器 overhead              ≈ 55s  (40%)
│   ├── _materialize_row_dicts dict 创建   45s 内的 dict 化部分
│   ├── g1_append (dict.get 海)          17s 中的 ~10s
│   ├── win.derive/update                12s 中的 ~8s
│   └── 杂项 dict 合并                    ~10s
│
├── numpy element-wise                  ≈ 35s  (26%)
│   ├── g3_bucketize                    20.7s
│   ├── g2_window slice copy            12.2s
│   └── tolist() 装箱                    ~3s
│
└── 其他 (to_model_row / phase1c)        ≈ 7s   (5%)
```

### 5.1 三个核心根因

1. **`row_dicts` 化是设计原罪**：1.6M 行 × 30+ 字段全 dict 化只为读 producer_dists/producer_classes 两个 list-of-int 字段
2. **`win.derive_before_update` stateful Python**：P1C/V10_3_B/V10_3_C 三组特征是按 row 累积的 window 状态，全 Python bytecode + dict 操作
3. **g1_append dict→numpy 冗余**：phase1a 已经写入 SoA，predict_batch 又把 dict 反向 unpack 回 history SoA

## 6. 候选优化方向（穷尽列表 + ROI 排序）

### 6.1 推荐做的（bit-exact）

| # | 改动 | 砍掉 | 预期 wall | rows/s | 工程量 | 风险 |
|---|---|---|---|---|---|---|
| **#1** | 消除 `_materialize_row_dicts`：在 `load_parquet_soa` 把 producer_dists/producer_classes 直读为 `np.ndarray[N, 4]`，phase1a 和 g1_append 改数组索引 | -45s | 170s | 9400 (+27%) | 1-2 天 | 中 |
| **#2** | `win.derive_before_update` 改 numba @njit + numpy buffer | -10s | 160s | 10000 (+34%) | 1 天 | 中（numba 对 dict/deque 不友好，需重构数据结构）|
| **#3** | g1_append 完全跳过：phase1a 直接写 history SoA，避免 dict 化 | -7s | 153s | 10500 (+41%) | 1 天 | 中 |

### 6.2 可选小优化

| # | 改动 | 预期 | 工程量 | 风险 |
|---|---|---|---|---|
| #4 | A1 torch.compile inductor | 0~+10% | 0.5h 尝试 | 中（bf16 + 首次编译慢，可能 segfault）|
| #5 | A2 CUDA Graph 替代 fwd | +5~7% | 4h | 中（batch shape 已固定，可行）|
| #6 | A6 / P2 pre-bucketize 写进 dataset | +10% | 4h | 低（dataset schema 改 + bit-exact 闸门兜底）|

### 6.3 高风险或工程量过大（不推荐当前阶段）

| # | 改动 | 预期 | 风险 |
|---|---|---|---|
| #7 | enc 整段写 C++ extension | +25% | 高，3-5 天 |
| #8 | enc 整段 numba @njit | +18% | 中-高，1-2 天，dict 不友好 |
| #9 | fp8 inference (用户允许统计无偏差) | +10% | 高（需 calibrate） |
| #10 | TensorRT 编译 model | +12% | 高（onnx 导出兼容性） |
| #11 | multiprocessing 拆 16 core | 理论 +N×（GIL 隔离） | 极高（ckpt 副本 GPU 占用、跨进程同步） |

## 7. 路线图建议

| 阶段 | 目标 rows/s | 工程量 | 风险 | 备注 |
|---|---|---|---|---|
| **当前** | 7437 | - | - | A2 修复已落地 |
| 0-2 周 | 9400 (+27%) | 1-2 天 | 中 | 做 #1 |
| 2-4 周 | 10000 (+34%) | +1 天 | 中 | 做 #2 |
| 4-8 周 | 10500 (+41%) | +1 天 | 中 | 做 #3 |
| 长期 | 12000+ | 高 | 高 | #7-#11，需重构 |

**最稳妥下一步**：做 #1（消除 _materialize_row_dicts）。这是单一最大块（45s），且工作集中在 dataset 加载层，不动 hot loop 主结构。

## 8. 经验教训

1. **A.1 ThreadPool 失败的教训**：必须先确认任务的本质是 IO bound 还是 GIL bound。Python 的 dict 创建、list.sort、json 解析都是 GIL-bound，ThreadPool 不会加速。
2. **维度 C 多卡分片失败的教训**：必须先用 Amdahl 算可并行占比 P，然后估上限 = 1/((1-P)+P/N)。当 P < 0.3 时多卡几乎没用。
3. **改动必须先做副作用分析**：每次改动前列清楚影响面、回退开关、bit-exact 闸门。维度 A.1 / 维度 C 都是因为评估收益时低估了同步/IO 副作用导致负优化回退。
4. **dataset 阶段一次性预算 > hot loop 优化**：能离线算的 row-local 纯函数全部挪到 dataset 生成阶段（如 #6 P2 pre-bucketize）。

## 9. 一致性闸门（任何改动都必须通过）

- `TAO_INFER_SOA_CHECK=1`：逐 cell 比对 SoA path 与 row_dicts path 的 enc 输出
- `TAO_INFER_DUMP_FIRST=1`：dump first batch tensor，比 md5
- 100K × 16c 完整跑：cpi_pred / precision / recall 与 baseline 完全一致

## 10. 当前可回退状态

所有未完成的优化均通过 env var 灰度，**不设即回退到当前 baseline (7437 rows/s)**：

- `TAO_INFER_GPU_SHARDS=N`：维度 C（已实现，未启用）
- 未来 `TAO_INFER_PRESOA_PROD=1`：#1（待实施）
- 未来 `TAO_INFER_BUCKET_PRECOMPUTE=1`：#6 P2（待实施）

代码主路径仍是 baseline 路径，bit-exact 保证。
