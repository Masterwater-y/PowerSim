# 13 - 多卡可扩展性再分析：cores 是免费 batch 放大器，k_max 是效率/精度旋钮

> 编写日期：2026-06-10
> 缘起：纠正 docs/12 的一处混淆——"做大 batch" 被错误等同于"加深投机、牺牲 coherence 精度"。
> 实测验证：quantum 数与 cores **无关**；`batch = k_max × cores` 中 cores 是**零精度代价**的并行度。
> 数据来源：`runs/fastenc_on_121941`、`runs/kmax_{128,256,512}_*`、`src/cpu/o3/probe/tao_trace.hh`、各 4c/16c dataset 的 cut_baseline.json

---

## 0. TL;DR（修正版结论）

1. **quantum 数 = `rows_per_core / k_max`，与 cores 无关**（实测 16c×100k → quanta=782 = 100000/128）。串行 barrier 次数**不随 cores 增长**。
2. **`batch = k_max × cores`**。其中：
   - **cores**：决定 batch，但**不改 quanta、不改单核精度的计算方式**（cores 是"要仿真几核的芯片"，是仿真对象 / 工作负载规模，不是自由调的旋钮）。
   - **k_max**：决定 batch **也**决定 quanta，且改变投机深度 → 影响精度。这才是"效率/精度权衡旋钮"。
3. **此前对多卡的否定结论需要限定条件**：在"固定 workload、固定 cores、固定 k_max"下扩 GPU 确实受 Amdahl 限制（CPU 段串行）。但若**工作负载本身就是大 cores（如 64/128 核芯片仿真）**，batch 天然很大，多卡 GPU 是有意义的——前提仍是先解决 CPU 段串行。
4. **可交付的"一个效率/性能权衡参数" = k_max**：实测 k_max=512 仍 bit-exact，提供 +6% 吞吐；它是面向用户的精度/速度档位旋钮。

---

## 1. 两个被验证的地基事实

### 事实 1：quantum 数与 cores 无关

```
quanta ≈ rows_per_core / k_max
实测：16c × 100k, k_max=128 → quanta=782 = ceil(100000/128)   ✓
      kmax=512               → quanta≈196 = ceil(100000/512)  ✓
```

公式中**没有 cores 项**。含义：

- 增大 cores → 每 quantum 的 batch（`k_max×cores`）变大，但**串行迭代次数不变**。
- 这推翻了 docs/12 "串行 quantum barrier 随规模增长拖累多卡"的隐含说法：barrier 次数只由 `单核指令数 / k_max` 决定。

### 事实 2：cores 通过 shared MESI 耦合 → cores 是仿真对象，不是自由旋钮

`src/cpu/o3/probe/tao_trace.hh` L126：

```cpp
struct LineState {
    uint8_t mesi = 0;                      // I/S/E/M
    int32_t owner_core = -1;               // 最近写者
    std::unordered_set<uint32_t> sharers;  // 最近读者集合
};
```

所有核共享同一 coherence 目录。实测不同 cores 精度不同（同 workload W11，来自 cut_baseline.json）：

| 配置 | approx_cpi_macro |
|---|---|
| W11 4c×100k | 1.0557 |
| W11 16c×100k | 1.2263 |

含义：cores 数决定了"仿真的是几核芯片"，核间一致性交互真实存在 → **cores 由建模目标决定，不能为了凑 batch 随意改**。但在给定 cores 下，`k_max × cores` 这个 batch 是**天然存在、跨核并行、零额外精度代价**的。

---

## 2. 修正：两个旋钮的本质区别（docs/12 的混淆点）

| 旋钮 | batch | quanta | 单核精度 | 可否自由调 | 角色 |
|---|---|---|---|---|---|
| **k_max** | ↑ ∝ k_max | ↓ 反比 | ⚠️ 受影响（投机越深，probe 读越陈旧的 MESI） | 是，但有精度上限 | **效率/精度权衡旋钮** |
| **cores** | ↑ ∝ cores | 不变 | 不改计算方式（改的是被仿真硬件） | 否，由建模目标定 | **工作负载规模 / 免费 batch 放大器** |

> docs/12 §3 把"做大 batch = 减少 coherence 更新 = 牺牲精度"写绝对了。**只有通过 k_max 做大 batch 才牺牲精度；通过 cores 做大 batch 不牺牲**（quanta 不变，coherence 更新频率不变）。

---

## 3. 重估多卡：什么时候值得

多卡加速 fwd 段，受 Amdahl 限制，关键看 **fwd 占比** 与 **batch 是否够大维持每卡 GEMM 饱和**。

### 3.1 当前实测段占比（16c, k_max=128, total 121s）

```
CPU 串行（phase1a+enc+phase1c） 37.4s (30.8%)   ← GIL 锁住，多卡无关
h2d                            22.2s (18.3%)   ← PCIe
fwd                            52.7s (43.5%)   ← 多卡可分
```

### 3.2 多卡有效的三个前提（现状对照）

| 前提 | 当前状态 | 如何满足 |
|---|---|---|
| fwd 占比高（≥50%） | ❌ 43.5%，CPU 段压着 | 先解决 CPU 串行（§4） |
| batch 够大维持每卡饱和 | ⚠️ B=2048 切 N 卡后每卡变小，GEMM 利用率掉 | 用 **大 cores** 或 k_max↑ 把总 batch 撑到 `2048×N` |
| CPU 段不在关键路径 | ❌ | §4 GIL 下沉 |

### 3.3 修正后的多卡结论

- **小 cores（如 4/16）+ 固定 k_max**：batch 不够喂多卡，多卡 ROI 低（与 docs/11 实测 4 卡负优化一致）。
- **大 cores（仿真 64/128 核芯片）**：batch = `k_max × cores` 天然大，切 N 卡后每卡仍饱和 → **多卡此时线性度好**，前提是 CPU 段已下沉。
- 即"多卡是否有用"**取决于仿真目标的 cores 规模**，不是一刀切。docs/12 的否定只对"小 cores"成立。

---

## 4. 修改计划

目标：让 batch（无论来自大 cores 还是 k_max）能真正喂饱多卡，必须先拆掉 CPU 串行段。分 4 个 phase，每个都带 env 灰度 + bit-exact 闸门，可独立回退。

### Phase 0（已完成）：基线确立
- ✅ FASTENC：7437→10473 rows/s，bit-exact。`TAO_INFER_FASTENC=1`。

### Phase 1（低风险，先做）：k_max 作为正式"效率/精度旋钮"
- **改动**：无需改代码（`--k-max` 已存在）。补一份不同 k_max 的精度/吞吐曲线文档，确立"推荐档位"。
- **交付**：用户可见的权衡参数：k_max=128（最准）/ 256 / 512（+6%，W11 实测仍 bit-exact）。
- **风险**：零（仅文档 + 默认值建议）。
- **验证**：扫 k_max ∈ {128,256,512,1024}，记录每档 cpi_err / precision / rows/s，找到 bit-exact 不破的最大 k_max。

### Phase 2（中风险，核心）：CPU 串行段下沉，解锁多核 CPU 并行
- **根因**：`OnlineWindowFeatures.derive_before_update/update`（infer/driver/windowed_features.py）是纯 Python 逐行有状态，持 GIL，16 核无法并行。
- **改动**：把窗口状态机重写为**释放 GIL** 的实现，二选一：
  - 2a. numba `@njit(nogil=True)`：deque/defaultdict → 定长环形 buffer + 开放寻址哈希。
  - 2b. C++ pybind（`gil_scoped_release`）：与 batch_probe 同款。
- **并行**：下沉后 phase1a 用 ThreadPool 跨 core 真并行（`--phase1a-workers` 开关已存在）。
- **灰度**：`TAO_INFER_WIN_NATIVE=1`，关掉回退 Python。
- **bit-exact 闸门**：先加细粒度计时确认 `win.*` 在 phase1a 的占比（决定 ROI），再决定 2a/2b；改完跑 `TAO_INFER_DUMP_FIRST` md5 + cpi 对齐。
- **预期**：phase1a 17.5s → ~3-5s（16 核并行）。

### Phase 3（低风险）：pinned memory + h2d/fwd overlap
- **改动**：batch tensor 用 `pin_memory()`，h2d 走独立 stream，与上一 quantum 的 fwd 尾部 overlap（注意：**不跨 quantum**，只在 phase1b 内部 enc→h2d→fwd 流水）。
- **灰度**：`TAO_INFER_PIN=1`。
- **预期**：h2d 22.2s 大部分藏进 fwd，+5~10%。

### Phase 4（条件触发）：多卡数据分片
- **前提**：Phase 2/3 完成后 fwd 占比升到 ≥60%，**且** 目标 workload 是大 cores（batch ≥ 2048×N）。
- **改动**：复用已有 `TAO_INFER_GPU_SHARDS`（_forward_decode 已实现 shard 路径），按 batch 维 pad+切分。
- **bit-exact**：模型全是 batch-independent 算子，整除分片逐 row 等价（docs/10 §C 已论证）。
- **预期**：大 cores 场景 fwd ~N× 加速；小 cores 不启用。

### 路线收益估算（保 bit-exact）

| 阶段 | 关键改动 | 预期 rows/s（16c） | 累计 |
|---|---|---|---|
| Phase 0 | FASTENC（已落地） | 10473 | — |
| Phase 1 | k_max=512 默认 | ~11100 | +6% |
| Phase 2 | win 下沉 + phase1a 多核 | ~13000 | +24% |
| Phase 3 | pinned h2d overlap | ~14500 | +38% |
| Phase 4 | 多卡（仅大 cores 场景） | 大 cores 下可超线性扩展 fwd | 取决于 cores |

> 16c 场景软上限仍约 docs/10 给的 ~30-45k（受 quanta×CPU 段地板限制）。要突破需 Phase 4 配合大 cores，或放宽 k_max（精度换吞吐）。

---

## 5. 待验证项（动手前必须先测）

1. **k_max 精度上限**：扫 k_max 到 1024/2048，找 bit-exact / cpi_err<0.5% 的最大值。
2. **win.* 占 phase1a 比例**：加计时探针，决定 Phase 2 的 ROI 与 2a/2b 选型。
3. **大 cores batch 实测**：用 64c dataset（若有）验证 batch 是否随 cores 线性增大、GEMM 是否维持饱和。

---

## 6. 与 docs/12 的关系

- docs/12 的"plateau / Amdahl / coherence 闭环是架构性约束"**仍成立**。
- 本文**修正** docs/12 的两点：
  1. quantum barrier 次数与 cores 无关（不随规模恶化）；
  2. cores 放大 batch 是零精度代价的，多卡在大 cores 场景有效——docs/12 的多卡否定只对小 cores 成立。
- 面向用户的"效率/精度权衡参数"明确为 **k_max**。
