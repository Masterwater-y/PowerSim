# 10 - 推理框架吞吐量可扩展性研究（W11 100K × 16c, H20 单卡）

数据快照：`runs/profile_W11_optA2_100k_20260609_173111`

| 指标 | 值 |
|------|-----|
| wall | 215.14s |
| rows | 1,600,000 |
| rows/s | 7437 |
| GPU | 1 × H20，bf16 autocast |
| ckpt | v10_3_fetchdecomp.best.pt |

## 1. 时间分布全景

```
wall = 215.1s
├── quantum_loop = 161.6s (75%)
│   ├── phase1a (speculate)  20.6s (12.8%)   跨 core 串行（共享 MESI）
│   ├── phase1b (predict)   134.1s (83.0%)   单 GPU + 单 Python enc
│   │   ├── enc             64.4s            Python+numpy 单线程
│   │   ├── h2d             12.5s            串行同步
│   │   ├── fwd             52.6s            单 GPU bf16
│   │   └── d2h              0.4s
│   ├── phase1c (commit)     5.5s (3.4%)     单线程 C++（pybind GIL release）
│   ├── phase2 (reconcile)   0.4s (0.2%)
│   └── phase3 (flush)       0.9s (0.6%)
└── IO（load + materialize + write）~53s (25%)  完全在 loop 外
```

参考源：`infer/driver/inference_driver.py:1361-1482` 的 `quantum_loop`。

## 2. 设计上的硬性串行（不可并行）

| 串行点 | 位置 | 为什么必须串行 |
|--------|------|--------------|
| **quantum 间** | `quantum_loop` L1409-1476 | phase1c 写 clock/MESI overlay/win → 下一 quantum 的 phase1a 读 |
| **quantum 内 phase 顺序** | L1431, L1456 | phase1a 产生 probes → phase1b 喂模型 → phase1c 用 preds 提交 |
| **phase1a 跨 core** | L1411-1412 注释 | batch_probe 直写共享 MESI 状态，并行会破坏顺序 |

这三个是**设计强约束**。除非引入"推测执行"+回滚，否则改不动。

## 3. 已实现的并行点

- **phase1b 跨 16 core fresh probes 单次聚合**：`fresh_cap = k_max * num_cores`（L1374-1376），实测 avg_B = 2046（=128 × 16）
- **C++ pybind 调用释放 GIL**：`commit_quantum_pod` 等在 `mlsim/infer/mesi_ref_sim/src/python_module.cc` 用 `py::gil_scoped_release`
- **优化方案 A + 修复**（已落地）：SoA 矩阵化 + `from_numpy(view).long()`，enc 段从 baseline 的 87s 优化到 64s

## 4. 可成倍并行化的维度（按 ROI 排序）

### 维度 A：IO 与 quantum_loop 重叠（**最低风险，+33%**）

- **现状**：`load_parquet_soa` + `_materialize_row_dicts` 在 `_build_cores`(L1503-L1510) 完全串行执行，loop 启动前阻塞 ~30s；`out.jsonl` 写在 phase3_flush (L1328) 内已批写，但仍串行
- **改法**：
  1. 主进程只 `load_parquet_soa` (SoA 数值列，~7s)，立即开始 quantum_loop
  2. 后台 ThreadPool 异步对每个 core 跑 `_materialize_row_dicts`（pyarrow + sort）
  3. `phase1a_probe` 第一次取该 core 的 `row_dicts` 时若未就绪则 `.result()` 等
- **预期收益**：~30s 前置 IO 与前 100 quantums 重叠，wall 215 → ~185s（+16%）；如完全无阻塞 → 215 - 53 = **162s（+33%）**
- **风险**：低；row_dicts 是 list-of-dict，由 phase1b 的 enc 路径消费，多线程 read-only 安全
- **bit-exact**：✅（IO 顺序无变化，只改时序）

### 维度 B：CUDA stream + 双缓冲（+5-10%）

- **现状**：predict_batch 内 enc → h2d → fwd → d2h 完全串行（L912/L920 显式 cuda.synchronize）
- **改法**：主 stream 跑 fwd 时，h2d_stream 预拷贝**下一 batch**到 GPU
- **预期收益**：h2d 12.5s + d2h 0.4s 大部分重叠到 fwd 内，phase1b 134 → 121s
- **bit-exact**：✅（仅改变拷贝时机）

### 维度 C：多 GPU 数据分片（+25-30%，bit-exact 可证）

- **现状**：1 GPU，fwd 52.6s；机器有 8 张 H20 全空闲
- **架构**：每卡 load 同一 ckpt，把 batch pad 到 N 整除后切 N 等份 → 每卡独立 fwd → CPU gather
- **bit-exact 论证**：模型只有 LayerNorm/Attention/Linear，全是 batch-independent 算子；同 GPU 型号 + 同 cuBLAS kernel + 整除分片 → 单 row 输出 bit-exact 等于单卡
- **预期（4 卡）**：fwd 52.6 → ~16s，phase1b 134 → 98s，wall → ~179s（+20%）
- **预期（8 卡）**：fwd 52.6 → ~10s；此时 enc=64s 已成新瓶颈
- **风险**：中，需要 CUDA stream 并发以避免 Python 串行 launch 抵消并行性

### 维度 D：enc 多进程 / Cython（+30-60%）

- **现状**：enc 64.4s 单 Python 线程（GIL 限制）
- **改法 1（multiprocessing）**：把 16 core 的 enc 分给 16 个 worker 进程并行
- **改法 2（cython/numba）**：单线程提速 3×
- **改法 3（GPU 端 enc）**：把 bucketize/hash_addr 从 numpy 搬到 GPU
- **bit-exact**：✅

### 维度 E：phase1c ThreadPool（已就绪，但只 -5s，ROI 极低）

- 代码已写（L1438-1453），缺 `LocalPybindBackend._phase1c_parallel_safe=True`
- phase1c 仅占 3.4%

### 维度 F：fp8 / TRT / 模型蒸馏（+30-50%，非 bit-exact）

- 仅在 H20 + Hopper 架构有 fp8 路径；TRT 编译可再 -20%

## 5. 组合上限

| 段 | 现在 | 全优化 | 说明 |
|----|------|----------|------|
| IO | 53s | 0 | 与 loop 重叠 |
| phase1a | 20.6 | 20.6 | 不可动 |
| enc | 64.4 | ~12 | multiprocessing |
| h2d | 12.5 | ~2 | overlap |
| fwd | 52.6 | ~10 | 8 卡分片 |
| d2h | 0.4 | 0 | overlap |
| phase1c | 5.5 | 5.5 | 不动 |
| phase2/3 | 1.3 | 1.3 | 不动 |
| **wall** | **215** | **~52** | 全做 |
| **rows/s** | **7437** | **~30000** | **+300%** |

**架构性硬下界**：phase1a 20.6 + phase1c 5.5 + phase2/3 1.3 + fwd_min ~5-10 ≈ **35s** → rows/s 上限 ~45000。

## 6. 推荐路线图

| 阶段 | 维度 | 工作量 | 收益 | bit-exact |
|------|------|--------|------|-----------|
| ✅ 已完成 | enc 优化（SoA 矩阵化 + from_numpy view） | — | +17.8% | ✅ |
| **当前 stage** | **A: IO overlap** | 0.5-1 天 | **+30%** | ✅ |
| 下一站 | C: 4 卡数据分片 | 3-5 天 | +20% | ✅ |
| 下一站 | B: cuda stream 双缓冲 | 1-2 天 | +5-10% | ✅ |
| 后续 | D: enc multiprocessing | 1-2 周 | +30% | ✅ |
| 长期 | F: fp8 / TRT | 1-2 月 | +30-50% | ❌ |

## 7. 设计上是否"不可能成倍增长"

**不是不可能**。结论：

- **0-2 周工作量**可把 rows/s 从 7437 → ~22000（**~3×**），bit-exact 全部可保
- **再往上**需要 fp8（数值偏差）或重写 sim 引入推测执行（极大代价）
- 设计的"必须串行"部分（phase1a 跨 core + quantum 间）决定了 ~30000-45000 rows/s 是软上限
