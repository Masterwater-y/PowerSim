# 性能优化路线

> 把 driver 推理吞吐从当前 ~78 IPS 拉到 ≥100K IPS 的分阶段路线。
> 详细 Quantum 方案见 [04-quantum-parallel-coherence.md](./04-quantum-parallel-coherence.md)。

---

## 1. 当前吞吐

| 模式 | 5K rows 耗时 | rows/s | 主要瓶颈 |
|---|---|---|---|
| mock-model | 1.7s | ~2900 | I/O + ref_sim + window features |
| ckpt（CPU） | 69.7s | ~72 | model.forward(B=1) |
| ckpt（GPU 单卡） | 63.6s | ~78 | model.forward(B=1) + Python 拼装 |
| ckpt（GPU 4 卡 DP） | 比单卡慢 | — | scatter/gather 开销 > 收益 |

**结论**：>95% 时间花在 batch=1 GPU forward。  
任何不打破 batch=1 的优化天花板都很低。

---

## 2. 失败方案 · 简单跨事件 batch（方案 A）

试图保持严格全序，仅把"frontier 上的多个核"合批 forward。

**实测**：`bs=8` vs `bs=1` 仅快 0.84%（57.17s → 56.69s）。  
**根因**：

- 当前 trace 通常只有 1–4 core，frontier 实际 batch ≤ 4
- 同时引入 correctness 回归（`cpi=0.0`）
- 因 `ref_sim` 入队顺序依赖 `fetch_clock`、`fetch_clock` 又来自上一条预测，
  跨事件提前打 batch 等于人为冻结调度顺序，**会破坏 MESI / LLC / MSHR 演化**

**判定**：方案 A 在严格语义下天花板低，且容易引入语义偏差，**放弃**。

---

## 3. 分阶段路线

### L0 · 严格语义内的微观优化（无损，5–80×）

不改调度模型，只优化"单条事件内部"：

| 项 | 预期收益 | 文件 |
|---|---|---|
| Ring buffer 增量窗口 | 5–10× | `infer/driver/windowed_features.py`、`infer/ml/infer.py` |
| 预分配 tensor + pinned memory | 2× | `infer/ml/infer.py` |
| bf16 推理 | 1.5–2× | `infer/ml/model.py` 加 `to(bfloat16)` |
| `torch.compile(mode='reduce-overhead')` | 1.5–2× | infer 入口 |
| CUDA Graph（B 形状固定后） | 2–3× | infer 入口 |
| KV cache（causal self-attn） | 2–4× | `infer/ml/model.py _MHA` |

**叠加预期**：单卡 **3K–8K IPS**（从 78 → 30–100×）。  
**不达 100K IPS**，因为 batch 仍是 1。

### L1 · Quantum-based parallel coherence（核心，0.5–2% CPI 偏差）

详见 [04-quantum-parallel-coherence.md](./04-quantum-parallel-coherence.md)。

**关键操作**：
1. 拆分 `mesi_ref_sim`：`LocalRefSim`（per-core） + `CoherenceCoordinator`（全局）
2. driver 主循环改为 quantum loop：
   ```
   while not done:
       Phase 1 (parallel): 每核独立推进 Δt=256 cycle
       Phase 2 (serial):   reconcile pending events
       Phase 3 (broadcast): 广播新 directory snapshot
   ```
3. Phase 1 内每核攒 mini-batch，整卡 batch 64–256
4. 加 `--quantum-cycles` CLI 参数；`Δt=1` 等价严格 baseline

**预期**：单卡 **30K–60K IPS**。

### L2 · Quantum + rollback（无损）

L1 的 Phase 2 不仅 reconcile，还检查"Phase 1 假设是否成立"。
不一致时只回滚相关 core 的相关 µops。

**预期**：等价严格语义，单卡 **50K–100K IPS**。

### L3 · 多卡 sharded cores（多卡）

不同核分到不同 GPU 进程，每卡跑一组核。
中心化 directory 跨进程同步。

**预期**：**150K–300K IPS**。

---

## 4. 推荐落地顺序

```
L0  (1 周内可完成)  →  78 IPS → 3K–5K IPS
L1  (2–3 周)        →  30K–60K IPS  ← 100K 目标已在视线内
L2  (1 周；可选)    →  50K–100K IPS
L3  (2 周；多卡)    →  150K–300K IPS
```

每个阶段都有独立验证：

- L0：与 baseline 比对 jsonl 字段，必须 bit-exact
- L1：`scripts/05_quantum_sweep.sh` 扫 Δt = {1, 128, 256, 512, 1024}，
  CPI 偏差 < 2% 视为通过
- L2：与 Δt=1 baseline 完全一致（除浮点容差）
- L3：每张卡内部仍走 L1/L2，跨卡只比较聚合 CPI

---

## 5. 风险与回退

| 风险 | 缓解 |
|---|---|
| L1 拆分 ref_sim 引入语义偏差 | 保留 `--quantum-cycles 1` 等价 baseline，所有 PR 必须 bit-exact 通过该模式 |
| L2 rollback 实现复杂 | 先做 L1 + small Δt，rollback 可作为后续可选项 |
| `torch.compile` 不稳 | 提供 `--no-compile` 开关 |
| CUDA Graph 形状变化触发重抓 | 固定 batch shape（`--model-batch-size 固定`） |
| Multi-core directory 同步 | 起步用单进程多线程；多卡 sharding 延后 |

---

## 6. 不在路线内的（明确拒绝）

- ❌ DataParallel（实测越多卡越慢）
- ❌ "把 ref_sim 也 batch 起来"——会打破 coherence 串行约束
- ❌ "把模型砍小到 batch=1 仍能 100K IPS"——精度损失不可控
- ❌ Online distillation 或学生模型——脱离当前 V10.3 strict 契约
