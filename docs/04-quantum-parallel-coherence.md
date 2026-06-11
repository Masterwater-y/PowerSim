# Quantum-based Parallel Coherence

> 本文档定义 TAO 多核 CPU 仿真系统中 **Quantum-based Parallel Coherence** 的设计、实现与误差控制规范。
>
> 适用范围：阶段 3 推理 driver（`infer/driver/`）。
>
> 目标：把单卡推理吞吐从当前 ~78 IPS 推到 ≥100K IPS，同时把 CPI 误差控制在 <2%。

---

## 1. 问题陈述

当前 driver（`inference_driver.py`）采用 **严格全序事件循环**：

```
heapq 全局取 fetch_clock 最小的 µop
  -> ref_sim
  -> model.forward(B=1)
  -> 更新该核 fetch/ready clock
  -> 重新全局排序
  -> 取下一条
```

实测吞吐 ~78 rows/s（macro instr ~40/s），距离目标 100K IPS 差三个数量级。
进一步分析发现：

- mock 模式 5K rows 仅 1.7s，说明 ref_sim + window features + I/O 不是瓶颈
- ckpt 模式 5K rows 需要 60s+，**>95% 时间在 batch=1 GPU forward**

简单跨事件 batch 不可行，原因是：

> `ref_sim` 的入队顺序由当前 `fetch_clock` 决定，而 `fetch_clock` 又由上一条 µop 的预测结果更新；
> 这是一个 **强耦合闭环**，提前打 batch 等于人为冻结调度顺序，会改变 MESI / LLC / MSHR 演化路径。

因此必须从执行范式层面解耦串行约束与模型并行计算。

---

## 2. 核心思想

> **严格事件驱动**：每条访存事件必须按全局时间戳 1 条 1 条处理。
> **Quantum-based**：**只要在一个足够小的时间窗口 Δt 内，谁先谁后已无法分辨真实硬件行为，就允许并行执行，窗口结束再统一对账。**

形式化：

- 仿真时间被切成等长 quantum：`[T, T+Δt), [T+Δt, T+2Δt), ...`
- 每核维护两套时钟（与现有 `ReferenceClock` 一致，见 [reference_clock.py](../infer/driver/reference_clock.py)）：
  - `fetch_clock`：单调 += `fetch_lat`，**用作 Phase 1 截止条件**
  - `ready_clock`：`max(ready, fetch_clock + exec_lat)`，**用作 Phase 2 事件时间戳**
- **同一 quantum 内**：各核独立向前推进，不互相通信
- **quantum 边界**：把 `pending_events` 按 `fetch_clock + exec_lat` 升序集中重放，让全局状态恢复一致

它本质上是 **PDES（Parallel Discrete Event Simulation）的 conservative 变体**：
用 lookahead Δt 换并行度。

> **时间口径约定（贯穿全文）**
> - "推进基准 / 截止条件" → `fetch_clock`（取指节奏）
> - "MESI 可见时刻 / Phase 2 排序键" → `fetch_clock + exec_lat`（≈ store-to-coherent / load 命中查询时刻）
> - `mispred` 在 L1 不参与时钟推进与排序；仅 L2 rollback 时进入 fetch_clock squash 罚款

---

## 3. 系统结构

把原来的"中心化 ref_sim"拆成两层：

```
┌─────────────────────────────────────────────────┐
│            Coherence Coordinator                │
│  - 全局 MESI directory                          │
│  - 全局 LLC LRU / set residency                 │
│  - 跨核 invalidate / downgrade 队列             │
│  - 每 Δt 触发一次 reconcile                     │
└──────┬──────────┬──────────┬──────────┬─────────┘
       │          │          │          │
   ┌───┴───┐  ┌───┴───┐  ┌───┴───┐  ┌───┴───┐
   │ Core0 │  │ Core1 │  │ Core2 │  │ Core3 │
   │       │  │       │  │       │  │       │
   │ local │  │ local │  │ local │  │ local │
   │ L1/L2 │  │ L1/L2 │  │ L1/L2 │  │ L1/L2 │
   │ MSHR  │  │ MSHR  │  │ MSHR  │  │ MSHR  │
   │ refsim│  │ refsim│  │ refsim│  │ refsim│
   │ model │  │ model │  │ model │  │ model │
   └───────┘  └───────┘  └───────┘  └───────┘
```

每核拥有自己的 **私有 ref_sim 实例**（私有 L1/L2/MSHR/walker、本地 fetch_clock、ring buffer 窗口、绑定的 GPU model 句柄）。
只有 **共享 LLC + coherence directory** 由 coordinator 统一维护。

---

## 4. 一个 quantum 内的执行流程

```
Quantum [T, T+Δt):

Phase 1 — 各核独立推进（并行）
  # 截止条件用 fetch_clock：决定本核还能否再发一条 µop 进流水
  for core in cores 并行:
      while core.fetch_clock < T + Δt:
          op   = core.next_op()
          coh  = core.local_ref_sim.on_mem_access(op)   # 用本地 cache + 上一 quantum 末快照
          feat = core.window.derive(op, coh)
          pred = core.model.predict(feat)               # 已批化（见 §5），返回 (fl, el, mp)
          fc, rc = core.clock.step(pred.fl, pred.el)    # fetch_clock += fl; ready = max(ready, fc + el)
          if op 涉及共享 line / LLC:
              # 事件时间戳用 fetch_clock + exec_lat（≈ per-op ready / store-to-coherent 时刻），
              # 不用 fetch_clock；后者只反映取指节奏，与 MESI 可见时刻系统性偏差
              core.pending_events.append(PendingEvent(
                  t          = fc + pred.el,            # 排序键
                  core_id    = core.id,
                  seq        = op.seq,
                  cacheline  = op.cacheline_addr,
                  is_store   = op.is_store,
                  size       = op.size,
              ))

Phase 2 — quantum 边界 reconcile（串行 / 极快）
  # 排序键 = (fetch_clock + exec_lat, core_id, seq)
  # 物理依据：MESI 可见事件发生在 access/commit 段，而非 fetch 段
  events = sort(
      union(c.pending_events for c in cores),
      key=lambda ev: (ev.t, ev.core_id, ev.seq),
  )
  for ev in events:
      coordinator.apply(ev)        # 真正改 MESI directory / LLC LRU
      if ev 与 Phase 1 假设不一致:
          standard: 修正状态，下一 quantum 自动追上
          strict:   触发 rollback，仅回滚相关 core 的相关 µops

Phase 3 — 广播新快照
  snap = coordinator.snapshot()
  for core in cores:
      core.local_ref_sim.update_directory(snap)
```

---

## 5. 与模型 forward 的结合

quantum 化之后，**batch 有了天然来源**：

- Phase 1 内每核大约会推进 `K = Δt / avg_cycle_per_op` 条 µops
- 4 核同步进入 Phase 1，全卡可见 batch ≈ `cores * K`
- 全部送入 `model.predict_batch`
- **不破坏一致性建模**，因为 Phase 1 已经从 Phase 2 拿到上一 quantum 末的 directory 快照

这就解决了之前的根本矛盾：

> "ref_sim 入队顺序依赖 fetch_clock，跨事件 batch 不安全。"

quantum 化的等价转换是：

> 在 Δt 时间窗内，**承认顺序不可分辨**，所以 batch 是**语义合法的**。

误差被限制在 Δt 窗内，可通过缩小 Δt 或加 rollback 控制到任意精度。

---

## 6. Δt 选择

| Δt（cycle） | 含义 | 误差 | 并行度 |
|---|---|---|---|
| 1 | 等价严格全序 | 0 | 无并行 |
| ~10 | 局部强假设 | 极小 | 一般 |
| **~100–1000** | **推荐区间** | **<2% CPI** | **接近 N×核数** |
| ~10K | DRAM 整事务窗 | 5–10% | 最大 |

经验法则：

- Δt ≤ "L2 命中延迟 + DRAM 行预充延迟" 时，跨核冲突基本不漏
- Δt ≈ 几个 cacheline 跨核传输周期（~200–500 cycle）是甜点
- 本系统 fetch latency 量级 ~10–80 cycle，建议 **Δt = 256 或 512 cycle** 起步
- 命令行参数：`--quantum-cycles`

---

## 7. 误差来源与量级

误差只可能来自 Phase 1 的 "独立假设"。三类：

1. **共享 line 的 invalidate 窗口错位**
   - Core0 在 `T+50` 写 line X；Core1 在 `T+80` 读 line X
   - 严格情况：Core1 应看到 invalid
   - quantum 内：Core1 可能仍看到旧 S 状态
   - 影响字段：`mesi_before`, `coh_oracle`
   - 概率：跨核共享访问 <5% 总访问，且 Δt 越小越少

2. **LLC LRU 顺序错位**
   - 多核同 quantum 都打同 set 时排序错乱
   - 影响字段：`d_llc_set_residency`, `d_llc_set_lru_pos`
   - 概率：仅当多核打同 set 时

3. **MSHR 占用估计偏低**
   - 各核独立累计自己的 MSHR
   - 影响字段：`d_mshr_depth` / `i_mshr_depth`
   - 缓解：MSHR 本身核私有，几乎无误差

### 7.1 误差性质订正：聚合无偏 ≠ 单 µop 无偏

> 重要订正：原文档曾用"下一 quantum 自动追上"描述 L1 模式的误差，这只对 IPC/CPI 这类**聚合量**成立，对单 µop 的字段值不成立。

- **单 µop 视角**：Phase 1 用旧 snapshot 算出的 oracle 字段（`mesi_before / sharer_bucket / inval_fanout / coh_oracle / d_llc_set_residency` 等）一旦写入 jsonl，**就不会被回改**——这条 µop 的 fetch_clock 也不会回退。所以受影响的 µop 上字段是"持续错"的。
- **聚合视角**：错误的 oracle 字段不会反馈影响后续核内事件触发顺序（因为 fetch_lat / exec_lat 由模型给出，不依赖 mesi_before），所以错误**不会跨 µop 累积放大**；统计平均下"错向上"和"错向下"的概率近似相等，IPC/CPI 是统计无偏的。
- **结论**：L1 quantum 的承诺只是 **CPI 误差 < 2%**，**不**承诺单条 µop 的 oracle 字段精度。**任何依赖单 µop oracle 字段精度的下游用途**（如 L1miss 数、CHA 流量、per-line 一致性事件统计）必须看 §7A。

### 7.2 文献实测

- Δt = 200–1000 cycle：CPI 误差 **0.5–2%**
- Δt = 100 cycle：CPI 误差 **<0.5%**
- 加 rollback：可压到 **<0.1%**

---

## 7A. 结构化属性（L1miss / CHA / per-line MESI 事件等）的精度保障

> 用户场景：除了 CPI，还要输出 **L1 miss 数、L2/L3 miss 数、CHA 流量、coherence event 计数**等结构化属性。这些属性是 ref_sim oracle 的**直接计数**，不能容忍 §7 列出的"持续错"。

### 7A.1 误差源分类（按是否影响结构化属性）

| 字段类 | 来源 | quantum 误差 | 结构化属性是否受影响 |
|---|---|---|---|
| 私有 L1/L2 hit/miss、私有 TLB、walker level、私有 MSHR | LocalRefSim 内部状态 | **零** | **否**（核私有，与 quantum 无关） |
| 共享 line MESI 状态、sharer 集合、inval_fanout | 上一 quantum 末 snapshot | 边界窗错位 | **是** |
| LLC residency / LRU pos / LLC hit/miss | 上一 quantum 末 LLC 视图 | 多核同 set 排序错乱 | **是** |
| CHA / directory 流量计数 | 跨核 invalidate / downgrade 触发次数 | Phase 1 估算 vs Phase 2 权威 | **是** |

L1 miss / private L2 miss / private MSHR 这类**完全核私有**的字段，**quantum 引入零误差**——它们只看本核的 L1/L2 cache 状态，而 LocalRefSim 把这部分完全私有化了。所以"L1 miss 准不准"实际上**不是 quantum 的问题，是 ref_sim 本身的问题**。

### 7A.2 共享侧字段的双轨制：Phase 1 估算 + Phase 2 权威回填

针对 LLC / MESI / CHA 这类共享侧字段，方案采用**双值并存**：

- **Phase 1 估算值 `*_est`**：用 quantum 末 snapshot 算出，**仅供模型 forward 使用**（特征对齐训练分布）
- **Phase 2 权威值 `*_oracle`**：coordinator 按 `(t, core_id, seq)` 重放后的真实状态，**写入最终 jsonl**

实现要点：

1. Phase 1 写 jsonl 时**只占位**共享侧字段（或写 `_est` 后缀），不要落最终值
2. Phase 2 `coordinator.apply(ev)` 时，每应用一个事件就**重算**该 µop 的共享侧 oracle，回填到 `oracle_ref` 指向的输出行
3. coordinator 内部维护权威 `lines_ / l3_ / l3_i_ / recent_line_count_`，全部计数器（CHA flits、invalidation count、sharer transitions）也在此处累加
4. 模型预测的 fetch_lat / exec_lat **不回算**，因为 Phase 1 forward 已经完成；这部分误差只反映在 CPI（仍受 §7 误差控制），不污染结构化属性

```
Phase 2 — reconcile + 权威回填
  events = sort(...)
  for ev in events:
      oracle = coordinator.apply(ev)        # 真正改 directory / LLC / 计数器
      jsonl[ev.oracle_ref].update({
          'mesi_before':         oracle.mesi_before,
          'coh_oracle':          oracle.coh_oracle,
          'sharer_bucket':       oracle.sharer_bucket,
          'inval_fanout':        oracle.inval_fanout,
          'dirty_owner':         oracle.dirty_owner,
          'd_llc_set_residency': oracle.llc_resid,
          'd_llc_set_lru_pos':   oracle.llc_lru,
      })
      coordinator.cha.add(oracle.cha_flits)  # CHA 流量
```

### 7A.3 这等价于把跨核侧"降级到严格事件驱动"

- 私有侧：quantum 并行 → 0 误差
- 跨核侧：quantum 内并行预算特征 → quantum 末按 `(t, core_id, seq)` **严格全序重放** → 0 误差
- 唯一的代价是**模型 forward 输入的 mesi_before 等是估算值**，会让 fetch_lat/exec_lat 预测带轻微偏置——但这正是 §7 的 CPI 误差，已被 Δt 控制

→ **结论：L1miss / CHA / per-line MESI 事件计数的精度等价于严格全序仿真，与 Δt 无关。**

### 7A.4 与原 §7 对照

| 字段 | 原 §7 误差 | §7A 双轨后误差 |
|---|---|---|
| `private_l1_miss / l2_miss / mshr` | 0 | 0 |
| `mesi_before / coh_oracle / sharer_bucket / inval_fanout` | quantum 错位 | **0**（Phase 2 回填） |
| `d_llc_set_residency / lru_pos` | quantum 错位 | **0**（Phase 2 回填） |
| CHA flits / invalidate count | 不可计 | **0**（Phase 2 计数） |
| `fetch_lat / exec_lat` 预测 | quantum 估算 | <2% CPI 误差（不变） |

### 7A.5 落地接口要求

`Coordinator.apply` 必须返回**完整的 oracle 包**（不仅是 directory 副作用），并且要保留计数器：

```cpp
struct OracleResult {
    int mesi_before;
    int coh_oracle;
    int sharer_bucket;
    int inval_fanout;
    int dirty_owner;
    int llc_resid;
    int llc_lru;
    int cha_flits;        // 本事件触发的 CHA 流量
    int inval_count;      // 本事件触发的远端 invalidate 次数
};
OracleResult Coordinator::apply(const PendingEvent& ev);

struct CounterSnapshot {
    uint64_t cha_total;
    uint64_t inval_total;
    uint64_t llc_miss_total;
    // ... per-core / per-line 细化按需添加
};
CounterSnapshot Coordinator::counters() const;
```

driver Phase 2 完成后即可用 `coordinator.counters()` 输出 PMU 风格汇总。

---

## 8. 实现等级

| 等级 | 名称 | 简述 | 误差 | 复杂度 |
|---|---|---|---|---|
| L1 | Conservative quantum | 边界 reconcile，不 rollback | 0.5–2% | 低 |
| L2 | Optimistic + rollback | 一致性失败时回放 | <0.1% | 中 |
| L3 | Causal slicing | Δt 动态切片 | <0.5% | 高 |

建议路线：先 L1 跑通并测吞吐，再判断是否升 L2。

---

## 9. 落地到当前代码的最小改造

### 9.1 拆分 ref_sim

当前 `mesi_ref_sim` 是中心化的，内部既管私有 L1/L2 又管共享 LLC + directory。改成：

- `LocalRefSim`（per-core）：私有 L1/L2/MSHR/walker
- `CoherenceCoordinator`（全局）：directory + LLC LRU + 计数器
- 两者通过 **快照接口** 通信，不每条都同步

涉及文件：

- `infer/mesi_ref_sim/include/simulator.hpp`
- `infer/mesi_ref_sim/src/`（新增 `coordinator.cpp` / `local.cpp`）
- pybind 绑定面新增：
  - `LocalRefSim.on_ifetch / on_mem_access / on_commit`
  - `Coordinator.apply / snapshot / counters`

#### 9.1.1 Snapshot 增量协议

`Coordinator.snapshot()` 不导出全量 `lines_`（避免 5K rows 量级线表造成 quantum 边界开销），改为**版本号 + 增量**：

```cpp
struct LineDelta {
    uint64_t cacheline_addr;
    uint8_t  state;       // M/E/S/I
    uint32_t owner_core;
    uint64_t sharer_mask; // bitmask over cores
};

struct LlcSetDelta {
    uint32_t set_id;
    uint16_t residency;
    uint16_t lru_top_k[K];   // K = associativity
};

struct Snapshot {
    uint64_t version;                     // 单调递增，每个 quantum +1
    std::vector<LineDelta>   lines_dirty; // 自上次 snapshot 后被改动过的 line
    std::vector<LlcSetDelta> llc_dirty;   // 自上次 snapshot 后被改动过的 set
};
```

- **Coordinator** 维护 `dirty_lines / dirty_sets` 两个集合，`apply(ev)` 时往里加；`snapshot()` 弹出并清空，version++
- **LocalRefSim.absorb(snap)**：仅 patch `snap.lines_dirty` 与 `snap.llc_dirty` 提到的条目；其他保留旧值
- 每核记录 `last_seen_version`；coordinator 据此判断是否需要全量重发（首次 / 落后过多）
- 序列化：直接 pybind11 zero-copy 暴露 `numpy.ndarray`，避免 Python 字典开销

预算：典型 Δt=256、4 核场景，单 quantum dirty_lines ≤ 数百条、dirty_sets ≤ 几十，每 quantum 同步开销 < 100 µs。

### 9.2 driver 主循环

`infer/driver/inference_driver.py`：

```python
DELTA_T = args.quantum_cycles  # default 256

if DELTA_T <= 1:
    legacy_heap_loop()         # 严格全序 baseline，沿用现有 heapq[fetch_clock] 路径
    return

while not all_done:
    # Phase 1: 三段式（特征收集 → 合并 forward → 时钟回填 + 入队），见 §9.3
    phase1(cores, DELTA_T)

    # Phase 2: 串行 reconcile，按 (t, core_id, seq) 排序，并回填共享侧 oracle（见 §7A）
    coordinator.reconcile(merge_pending(cores))

    # Phase 3: 广播增量 snapshot（version-based，见 §9.1.1）
    snap = coordinator.snapshot()
    for core in cores:
        core.absorb(snap)
        # core.fetch_clock_base 已在 phase1 内部按 deadline+slack 推进
```

要点：

- **不再有全局 heapq**，跨核入队顺序由"每核独立 advance_until"取代
- 每核 `fetch_clock_base` 是该核 quantum 边界基准，每 quantum 推进 `Δt + slack`（见 §4 伪代码）
- 同核内部 µop 仍按 `micro_seq` 顺序（`st.idx += 1`），quantum 不引入核内重排
- `Δt == 1` 走 legacy 路径以保留 ground-truth baseline

### 9.3 模型 batching：Phase 1 三段式

> **关键设计**：Phase 1 内每核**不**单独跑 GPU，而是先攒 feature 再合并 forward，否则失去 batch 收益。

```python
def phase1(cores, DELTA_T):
    # === Phase 1a: 各核独立攒 feature（CPU 并行，可多线程） ===
    # 注意：此阶段还不知道 fl/el，所以无法用 fetch_clock 作截止；
    # 改用乐观上界 K_max = 经验值（如 32）作每核探测深度
    for core in cores 并行:
        deadline = core.fetch_clock_base + DELTA_T
        K = min(K_max, len(remaining_ops(core)))
        for k in range(K):
            op   = core.peek_op(k)                                  # 不消费 idx
            coh  = core.local_ref_sim.on_mem_access_speculative(op) # 不改本地 cache
            feat = core.window.derive_speculative(op, coh)          # 不改 ring buffer
            core.feat_buf.append((op, coh, feat))

    # === Phase 1b: 全核合并 forward（GPU 仅一次） ===
    all_feats = concat(c.feat_buf.feats for c in cores)
    preds     = model.predict_batch(all_feats)        # KV cache + bf16 + CUDA Graph
    split_back_to_cores(preds, cores)

    # === Phase 1c: 各核回填 + 推时钟 + 入 pending（CPU 并行） ===
    for core in cores 并行:
        deadline = core.fetch_clock_base + DELTA_T
        for (op, coh, feat), (fl, el, mp) in zip(core.feat_buf, core.preds):
            if core.fetch_clock >= deadline and core.committed_this_quantum >= 1:
                # 已发过至少 1 条且越过 deadline → 截止，剩余探测的留下 quantum
                core.unconsumed.extend(rest)         # feature 已算好，下个 quantum 直接复用
                break
            fc, rc = core.clock.step(fl, el)         # 真推 fetch_clock / ready_clock
            core.local_ref_sim.commit_speculative(op)# 真改本地 L1/L2/MSHR/window
            core.win.update(op, coh)
            if op 涉及共享 line / LLC:
                core.pending_events.append(PendingEvent(
                    t=fc+el, core_id=core.id, seq=op.seq,
                    cacheline=op.cacheline_addr, is_store=op.is_store,
                    size=op.size, oracle_ref=jsonl_row_id))
            core.idx += 1
            core.committed_this_quantum += 1
        # slack 推进
        slack = max(0, core.fetch_clock - deadline)
        core.fetch_clock_base = deadline + slack
        core.committed_this_quantum = 0
```

要点：

1. **`on_mem_access_speculative` 不改本地 cache**：拿 oracle 但不更新 LRU/MSHR；commit 时再真改。这是因为 1a 阶段同时探测 K 条 µop 的特征。
2. **`derive_speculative` 不改窗口 ring buffer**：1c 阶段才真正调 `win.update`。
3. **K_max 上限**：每核每 quantum 至多发 K_max 条，避免 fl 过小时 1a 探测过深；典型 K_max=32，全卡 batch = cores * K_max = 128（4 核）。
4. **fetch_clock 截止条件在 1c 检查**：1a 不知道 fl 不能截止，所以乐观探测 K_max 条，1c 拿到 fl 后逐条判定是否真的越过 deadline；越过的部分留作 `unconsumed`，下个 quantum 复用（feature 已算好，省一次 forward）。
5. **GPU forward 唯一一次**：是整套优化的吞吐瓶颈所在，必须合并；多核合并后 batch ≈ 128–256。

### 9.4 ring buffer + 增量窗口

为消除 batch=1 forward 之外的次要开销，同步把 `feats_to_window` 改为 ring-buffer 增量：

- 每核维护 `torch.zeros(N, F, pin_memory=True)` 固定窗口
- 每条 commit 后只 `buf[head] = new_row; head = (head+1) % N`

### 9.5 验证步骤

1. 跑严格串行（`--quantum-cycles 1`）作 ground truth
2. 跑 `Δt = 256 / 512 / 1024`，对比：
   - per-core CPI 偏差
   - fetch_lat / exec_lat 直方图
   - mispred 率
3. 选最小 Δt 满足 <2% CPI 误差

---

## 10. 安全性论证

quantum-based 的最终结果**等价于**一个 "Δt 时间窗内乱序、窗外有序" 的离散事件仿真。

- 真实硬件本身**不存在** "全局 1 cycle 一刀切" 的全序：
  - 不同核的访存请求在 NoC 上的到达顺序本来就有 ~tens-of-cycles 抖动
  - DRAM controller 的 bank scheduler 也会重排
- Δt 选在 ≤ L2 命中延迟 + DRAM 行预充延迟时，
  quantum 内的乱序范围 ≤ 真实硬件的天然抖动范围
- 因此 quantum-based 的"偏差"在物理上**不可观测**，只是与"严格 cycle-accurate baseline"对比时表现为统计误差

加上 L2 rollback 后可消除任何与 baseline 的统计偏差。

---

## 11. 性能预算

| 阶段 | 关键开关 | 单卡 IPS（rows/s） |
|---|---|---|
| 当前 | 严格全序 + B=1 | ~78 |
| L0 优化 | ring buffer + bf16 + torch.compile + cuda graph + KV cache | ~3K–5K |
| L1 quantum=256 | 多核 fetch-group batch | ~30K–60K |
| L2 quantum + rollback + bf16 | 几乎无损 | ~50K–100K |
| 多卡 sharded cores | 进程级，每卡若干核 | **~150K–300K** |

100K IPS 目标在 **L1 + 单卡 H 系列** 已经基本可达，2 卡 sharding 稳超目标。

---

## 12. 与论文的关系

TAO 论文（POMACS 2024 §7-8）公开报告 50–100K IPS，正是采用：

- fetch-group 粒度预测（不是 µop 粒度）
- per-core lookahead + 投机执行
- 多核 quantum-based coherence
- 中心化 directory shadow + 各核私有 cache
- KV cache + bf16 + CUDA Graph

本方案是这套思想在本仓库的落地实现，命名、接口、Δt 选择与本仓库现有 `mesi_ref_sim` / `driver` 体系兼容。

---

## 13. 待办

### 13.1 已对齐的口径（落地必须遵守）

- [ ] 拆分 `mesi_ref_sim` 为 `LocalRefSim` + `Coordinator`（§9.1）
- [ ] 实现 Snapshot 增量协议（version + dirty_lines + dirty_sets，§9.1.1）
- [ ] driver 主循环改为 quantum loop 三段式：1a 攒 feature → 1b 合并 forward → 1c 回填推时钟（§9.3）
- [ ] `LocalRefSim.on_mem_access_speculative / commit_speculative` 接口（§9.3）
- [ ] `PendingEvent.t = fc + exec_lat`，Phase 2 排序键 `(t, core_id, seq)`（§4）
- [ ] `fetch_clock_base += Δt + slack`，避免下一 quantum deadline 倒退（§4）
- [ ] `Coordinator.apply` 返回完整 `OracleResult`，driver 在 Phase 2 回填共享侧 oracle 字段（§7A）
- [ ] `Coordinator.counters()` 输出 CHA / inval / LLC miss 等结构化属性（§7A.5）
- [ ] 保留 `--quantum-cycles 1` legacy heapq 路径作 ground-truth baseline（§9.2）

### 13.2 验证与配套

- [ ] ring buffer 增量窗口（§9.4）
- [ ] Δt 误差扫描脚本（`scripts/05_quantum_sweep.sh`）：CPI 误差 + 共享侧 oracle bit-exact + 计数器 bit-exact 三项对比
- [ ] L2 rollback 实现（可选，§8）
- [ ] 多卡 sharded cores 启动脚本

### 13.3 待研究

- [ ] load 类事件是否改用 `t = fc + L1_lookup_cycles` 而非 `fc + exec_lat`（§B.3 工程问题）
- [ ] K_max 自适应：根据各核近 N quantum 的 IPC 动态调（避免负载不均导致 batch 抖动）
- [ ] Phase 1a/1c 共用 op 列表的零拷贝实现（避免 feat_buf 序列化）
