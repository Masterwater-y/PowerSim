# Quantum 并行一致性方案 —— 进度文档

> 本文档记录 driver + mesi_ref_sim 在引入 quantum-based PDES 方案过程中
> 已完成的阶段（A → B → C → D.0~D.4）与未完成的阶段（D.5a / D.5b）。
> 配合权威设计稿 `tao_cpu_sim/docs/04-quantum-parallel-coherence.md` 阅读。

---

## 总体路径

```
A. driver-side 准备           ✅ 完成
B. C++ 拆分过渡（B.1 ~ B.5）  ✅ 完成
C. Python 热点优化            ✅ 完成
   ├─ C.1 windowed_features    ✅
   └─ C.2 phase1c dict 装回    ✅
D. C++ 真私有 + 并发           🚧 进行中
   ├─ D.0 step() 真私有        ✅
   ├─ D.1 WriteSet 协议        ✅
   ├─ D.2 probe overlay-only   ✅
   ├─ D.3 GIL release          ✅
   ├─ D.4 ThreadPool plumbing  ✅
   └─ D.5 真并发解锁           🚧 进行中
       ├─ D.5a 结构改造         ✅
       └─ D.5b 开多线程验收      ⬜
```

吞吐对比基线（5K Δt=1 bit-exact 守门 + 50K/100K Δt=256 K=32 smoke）：

| 阶段 | 50K rows/s | 100K rows/s | 备注 |
|---|---|---|---|
| baseline (A 之前) | 23K | 23K | — |
| C.1 完成 | 34.2K | 34.0K | windowed_features 增量计数 +47% |
| C.2 完成 | 35.1K | 35.4K | dict 装回字面量化 |
| D.0 完成 | 35.1K | 34.9K | step() 拆 CoreLocal/SharedState |
| D.1 完成 | 33.9K | 33.4K | WriteSetOp 镜像记录开销 -3~4% |
| D.2 完成 | 34.5K | 33.2K | overlay-only + reconcile flush |
| D.3 完成 | 34.4K | 34.1K | pybind GIL release（单线程零开销）|
| **D.4 (workers=1)** | 34.5K | 34.1K | 与 D.3 一致 |
| D.4 (workers=4) | 30.7K | 30.4K | mutex contention 占主导（预期）|
| **D.5a (workers=1)** | 34.8K | 34.4K | atomic line + bank lock；删除 overlay/write_set/shared_mu_ |
| E.1 完成 | 32.4K | 32.0K | batch_probe 单次 pybind，单看降速但为 E.2 架构基础 |
| E.2 完成 | 35.1K | 33.2K | WindowedFeatures 下沉 C++（batch_probe 直接返回 12 win 字段 + batch_window_update 单次 pybind） |
| E.3 完成 | 35.7K | 36.4K | 删除 phase1c 每 µop commit_speculative pybind crossing |
| **E.4 完成** | **40.4K** | **40.6K** | orjson + bytearray 批写 jsonl |

5K Δt=1 jsonl 在 D.0 → D.5a 全程严格 bit-exact（diff -q 通过），CPI 全程
等于 0.46874121285968695（Δt=1 5K）/ 0.4600（50K Δt=256）/ 0.4553
（100K Δt=256）。

---

## A 阶段：driver-side 准备 ✅

driver 已具备 quantum loop 框架：phase1a probe / phase1b predict /
phase1c commit / phase2 reconcile / phase3 flush。reference_clock /
windowed_features / 与 mesi_ref_sim 通过 ref_sim_client 解耦。

关键文件：
- [reference_clock.py](infer/driver/reference_clock.py)
- [inference_driver.py](infer/driver/inference_driver.py)
- [ref_sim_client.py](infer/driver/ref_sim_client.py)

---

## B 阶段：C++ 过渡拆分 ✅

引入 `Coordinator` + `LocalRefSim` 双层；初始仅作 façade 包装，行为
等价于原 `Simulator`。pybind11 暴露 `PyCoordinator` / `PyLocalRefSim`，
driver 通过 `LocalPybindBackend` 走新接口。

---

## C 阶段：Python 热点优化 ✅

### C.1 windowed_features
- `mem_count64` / `br_count64` 增量计数器替代 `sum(generator)`
- `int.bit_length() - 1` 替代 `math.log2(int)`
- bit-exact 保留；50K/100K +47%

### C.2 phase1c_commit dict 装回
- `_build_feature_row`: 5x update() → `{**a,**b,**c,**d,**e}` 字面量解包
- `phase1a_probe`: 缓存 row.get / 共享 D_ZERO / cached_i_attrs / 上提方法引用
- `ref_sim_client.py`: 移除冗余 `dict(...)` 包装
- 50K/100K +3~4%

---

## D 阶段：C++ 真私有 + 并发 🚧

### D.0 step() 真私有 ✅

将 step()/stepIFetch() 改写为纯函数 `stepImpl(CoreLocal&, SharedState&, ...)`。

私有/共享拆分：
- `CoreLocal`：L1d / L1i / L2 / L2_i / dtlb / itlb / l1d_mshr / l1i_mshr
- `SharedState`：cfg / lines / i_lines / l3 / l3_i / walker / i_walker /
  recent_line_count

`Simulator` 类保留为 main.cc 用 façade。

### D.1 WriteSet 协议（双写过渡）✅

引入 `WriteSetOp` 结构（cl/core_id/seq/is_store/is_ifetch/line_before/
line_after/owner_before/owner_after/same_line_recent_before）。probe 仍
立即写 SharedState（保 sorted-cid 串行下 cross-core 一致性 bit-exact），
WriteSetOp 是镜像记录，driver 在 quantum 边界统一 drain。

`PyLocalRefSim` 从 fallback `stepForCore` 切换到真持有 `unique_ptr<LocalRefSim>`。
`commit_speculative` / `commit_ifetch_speculative` 真调 C++（不再 no-op）。

### D.2 probe overlay-only ✅

`SharedState` 新增 `lines_overlay` / `i_lines_overlay` /
`recent_line_count_overlay`。新增 lazy copy-on-touch 视图：
- 写视图 `linesView` / `iLinesView` / `rlcView`：命中 overlay 返回，
  否则把 base 项拷一份到 overlay 后返回
- 只读视图 `linesViewConst` / `iLinesViewConst` / `rlcViewRead`：不污染 overlay

`stepImpl` / `stepIFetchImpl` 全部改走视图。`Coordinator::reconcile` 在
quantum 边界把 `*_overlay` move/copy 回 base。

base lines/i_lines/recent_line_count 在 quantum 内不变，所有写沉淀到 overlay。
单 overlay + sorted-cid 串行 phase1a 下，"overlay 视图" ≡ "直接写 base"，
bit-exact 保留。

### D.3 GIL release ✅

`PyLocalRefSim::on_mem_access_speculative` / `on_ifetch_speculative`：
C++ 计算段（`backend_->probe`）放进 `py::gil_scoped_release` 块；dict
构造段保持在 GIL 下。`commit` / `commit_ifetch` / `drain_*` /
`reconcile` 整段释放。`drain_all_write_sets` 走 `backend()` 直接拿 `LocalRefSim*`
绕开嵌套 acquire。

单线程下 GIL release 净开销 ≈ 0；接口契约就位为 D.4 ThreadPool 准备。

### D.4 ThreadPool plumbing ✅

- driver 加 `--phase1a-workers` 参数（默认 1，向后兼容）
- workers > 1 时走 `concurrent.futures.ThreadPoolExecutor` 并发提交各核 probe
- workers = 1 时走原串行路径，零开销
- 预热 `_local()` 避免首次 dict race
- C++ 端引入 `Coordinator::shared_mu_` 全局粗粒度锁包 probe / probeIFetch /
  commit / commitIFetch / drainWriteSet / reconcile，跨核 step 仍按到达
  顺序串行执行，行为 bit-exact

w=4 当前掉速 -11%，**符合预期**：C++ 段全锁串行拿不到并发收益，反而
mutex contention + ThreadPool 调度开销引入；接口/线程框架就位，等 D.5
拆锁解锁真并发。

---

## D.5 真并发解锁 🚧 进行中

### 决策清单（已与用户确认）

| 决策项 | 选定方案 |
|---|---|
| 一致性守门口径 | **宽口径**：CPI ≤ 1% / counter ≤ 2% |
| 拆锁实现路径 | **方案 C**：lock-free atomic line state |
| L3 / L3_i 锁粒度 | **每个 bank 一把 mutex** |
| W11 全量长跑 | **否**，仅 50K/100K smoke |
| WriteSet 去留 | **删除**（瘦身，含 drainWriteSet / drain_all_write_sets）|
| 实施分步 | **分两步**：D.5a 结构改造 → D.5b 开并发 |
| TSan 验证 | **是**，5K Δt=1 workers=4 跑一次 0 race |

### 数据结构（最终设计）

```cpp
// 4-bit sharer bitmap（限定 num_cores ≤ 4）
struct AtomicLine {
    std::atomic<uint64_t> raw{0};
    // [0:1]=state(I=0/S=1/E=2/M=3) [2:9]=owner_i8 [10:13]=sharer_bits
};

constexpr size_t SHARD_N = 64;
struct LinesShard {
    mutable std::shared_mutex mu;
    // unique_ptr 防 rehash 失效（atomic 必须地址稳定）
    std::unordered_map<uint64_t, std::unique_ptr<AtomicLine>> map;
};
struct RlcShard {
    mutable std::shared_mutex mu;
    std::unordered_map<uint64_t, std::unique_ptr<std::atomic<uint32_t>>> map;
};
struct BankLockedLRU {
    BankedSetAssocLRU lru;
    std::vector<std::mutex> bank_mu;
    bool touch(uint64_t cl);
    void peekSetState(uint64_t cl, uint32_t* res, uint32_t* pos) const;
    void invalidate(uint64_t cl);
};

struct SharedState {
    UarchProfile cfg;
    std::array<LinesShard, SHARD_N> lines, i_lines;
    std::array<RlcShard,   SHARD_N> rlc;
    BankLockedLRU l3, l3_i;
    PageWalkSim walker, i_walker;     // 通过 walkWithL3 调 BankLockedLRU
    // overlay/write_set 全部删除
};
```

### stepImpl 写路径（CAS-loop）

```cpp
auto& shard = lines.shard(cl);
AtomicLine* al;
{ shared_lock lk(shard.mu);
  auto it = shard.map.find(cl);
  if (it != end) al = it->second.get();
  else al = nullptr;
}
if (!al) { unique_lock lk(shard.mu); al = ensure(shard, cl); }

uint64_t v = al->raw.load(std::memory_order_acquire);
do {
    new_v = transition(v, ev);   // pure function
} while (!al->raw.compare_exchange_weak(
    v, new_v, std::memory_order_release, std::memory_order_acquire));
```

### 删除清单（瘦身）

- `WriteSetOp` 结构 + `LocalRefSim::write_set_` / `drainWriteSet` /
  `writeSetSize`
- `PyLocalRefSim::drain_write_set_count` / `PyCoordinator::drain_all_write_sets`
- `LocalPybindBackend.drain_all_write_sets` 及 driver `phase2_reconcile` 调用
- `Coordinator::shared_mu_`（D.4 引入）
- `lines_overlay` / `i_lines_overlay` / `recent_line_count_overlay`（D.2 引入）
  + 6 个 view helper（linesView / iLinesView / rlcView / linesViewConst /
  iLinesViewConst / rlcViewRead）
- `Coordinator::reconcile` 退回 stub（保留 API 兼容）

### 实施分步

#### D.5a — 结构改造（单线程 bit-exact）✅

1. 新增 `AtomicLine` / `LinesShard` / `RlcShard` / `BankLockedLRU` 结构
2. 重写 `stepImpl` / `stepIFetchImpl` 走 atomic line + sharded map + bank lock
3. 删除 overlay / write_set / shared_mu_ / reconcile flush 逻辑
4. driver 保持 `--phase1a-workers 1`
5. 守门已过：`diff -q /tmp/smoke_coord.jsonl /tmp/smoke_d5a.jsonl` BIT-EXACT
6. 50K/100K workers=1：34.8K / 34.4K rows/s，CPI 0.4600 / 0.4553
7. 5K Δt=1 workers=4 smoke 可运行，CPI 0.46874121285968695（非 D.5b 验收）

#### D.5b — 开并发

1. driver 默认 `--phase1a-workers 4`
2. 守门：50K/100K workers=1 vs workers=4 CPI ≤ 1% / counter ≤ 2%
3. 重跑 3 次 workers=4，CPI stddev < 0.1%
4. TSan build：`cmake -DCMAKE_CXX_FLAGS="-fsanitize=thread -g"` →
   5K Δt=1 workers=4 跑一次，确认 0 race

### 风险点 & 缓解

| 风险 | 缓解 |
|---|---|
| LinesMap rehash 让 atomic 地址失效 | `unique_ptr<AtomicLine>` 稳定地址 |
| sharer_bitmap 仅 4-bit | `static_assert(num_cores ≤ 4)` at Coordinator ctor |
| same_line_recent / inval_fanout 因 race 偏差 | 宽口径已允许 |
| bank_mu 在 num_banks=1 退化 | 仍保留单 mutex；只影响性能下界 |
| atomic CAS 在高竞争 cl 下 livelock | 失败超阈值 → 短暂 backoff（如需）|
| TSan 报 unordered_map find/emplace race | shared/unique mutex 已分流 |

### 改动文件清单（预估）

- [simulator.hpp](infer/mesi_ref_sim/include/simulator.hpp)
  - 新增 AtomicLine / LinesShard / RlcShard / BankLockedLRU
  - 删除 LineMesi（改成纯解码 helper）+ overlay 三件套 + 6 个 view helper
  - 新 SharedState：lines/i_lines = `array<LinesShard, 64>`；rlc 同理；
    l3/l3_i 改 BankLockedLRU
  - 重写 stepImpl / stepIFetchImpl
- [quantum.hpp](infer/mesi_ref_sim/include/quantum.hpp) /
  [quantum.cc](infer/mesi_ref_sim/src/quantum.cc)
  - 删 shared_mu_、WriteSetOp、write_set_、drainWriteSet
  - probe / probeIFetch 移除 lock_guard
  - reconcile 退回 stub
- [python_module.cc](infer/mesi_ref_sim/src/python_module.cc)
  - 删 drain_all_write_sets / drain_write_set_count
- [inference_driver.py](infer/driver/inference_driver.py)
  - phase2_reconcile 不再调 drain_all_write_sets
  - `--phase1a-workers` 默认从 1 改 4
- [ref_sim_client.py](infer/driver/ref_sim_client.py)
  - 删 drain_all_write_sets

---

## 守门基准文件

- `/tmp/smoke_coord.jsonl` —— 5K Δt=1 K=32 coordinator backend 输出
  （D.0 ~ D.4 全程 bit-exact）
- `/tmp/smoke_func_5k`、`/tmp/smoke_func_50k`、`/tmp/smoke_func_100k`
  —— smoke 数据集

复现命令：

```bash
# 5K Δt=1 bit-exact 守门
/root/miniconda3/envs/yinhaolang/bin/python -m driver.inference_driver \
  --functional-dir /tmp/smoke_func_5k \
  --uarch-profile data/W11_stream_mix/uarch_profile.json \
  --ref-sim-module-dir mesi_ref_sim/build \
  --out-jsonl /tmp/smoke_<tag>.jsonl \
  --report-json /tmp/smoke_<tag>.report.json \
  --mock-model --quantum-cycles 1 --k-max 32 \
  --ref-sim-backend coordinator --phase1a-workers 1
diff -q /tmp/smoke_coord.jsonl /tmp/smoke_<tag>.jsonl  # 期望无差异

# 50K/100K Δt=256 吞吐
for sz in 50k 100k; do
  for w in 1 4; do
    t0=$(date +%s.%N)
    /root/miniconda3/envs/yinhaolang/bin/python -m driver.inference_driver \
      --functional-dir /tmp/smoke_func_$sz \
      --uarch-profile data/W11_stream_mix/uarch_profile.json \
      --ref-sim-module-dir mesi_ref_sim/build \
      --out-jsonl /tmp/smoke_${sz}_<tag>_w${w}.jsonl \
      --report-json /tmp/smoke_${sz}_<tag>_w${w}.report.json \
      --mock-model --quantum-cycles 256 --k-max 32 \
      --ref-sim-backend coordinator --phase1a-workers $w
    t1=$(date +%s.%N)
    echo "$sz w=$w elapsed=$(echo "$t1-$t0"|bc) rows_per_sec=..."
  done
done
```

---

## E 阶段：CPU driver 端到端冲 ≥250K rows/s ✅ (E.1–E.4 段一)

> 目标背景：原 §11 性能预算建立在 GPU forward 是瓶颈的假设上；实测纯 CPU
> driver 在 D.5a 已达 34K rows/s 上限，加推理模型后端到端被 CPU 路径封顶。
> 为了支撑加模型后 ≥100K MIPS 的总目标，CPU 框架必须先冲到 ≥250K rows/s
> （留 1.5–2× headroom 给模型）。E 段聚焦 pybind crossing / Python heat 的
> 系统性下沉。**workers 通路实测降速，已正式放弃**（E_clean）。

### E.1 Phase 1a 批量 probe ✅
- C++ 端 `PyLocalRefSim::batch_probe(fields)` 单次 pybind 替代 K×(probe + probeIFetch)
- driver phase1a 改 10 元 POD tuple 列表
- 单看吞吐 32.4K/32.0K（-7%/-9%），是 E.2/E.3 的架构基础

### E.2 WindowedFeatures 下沉 C++ ✅
- 新增 `WindowState` struct，按位对齐 [windowed_features.py](infer/driver/windowed_features.py)
  （win64 / win256_cl / win1024_cl / win256_dram + 7 张 unordered_map）
- `batch_probe` 在同一段 GIL release 内同时返回 16 个 oracle 字段 + 12 个
  win 字段
- 新增 `batch_window_update(fields, committed_mask)`：phase1c 末尾一次 pybind
  把 K 条 win.update 批量推下去，C++ 内 GIL release
- driver phase1c 累积 `(fields_tuple + d_bank_id, True)` 数组；删除
  `OnlineWindowFeatures.update` 调用
- 吞吐 35.1K/33.2K，5K Δt=1 bit-exact

### E.3 Phase 1c 批量 commit ✅
- D.5a 后 `LocalRefSim::commit()` 仅生成未使用的 LineDelta，phase1c 每 µop
  调 sim.commit_speculative 是纯 pybind crossing
- 删除 driver 端 200K (50K) / 400K (100K) 次每 µop crossing
- 吞吐 35.7K/36.4K（+1.7% / +9.8% vs E.2）

### E.4 Phase 3 jsonl 加速 ✅
- `import orjson`（已校验与 `json.dumps(separators=(',', ':'))` 字节相同）
- `phase3_flush` 用 `bytearray` 聚合，一次 fout.write 批量 syscall
- jsonl 文件改二进制 + 1MB buffer，避免 utf-8 编码再次拷贝
- 吞吐 **40.4K / 40.6K**（+16% vs D.5a baseline）

### E_clean Phase 1a 串行化 ✅
- `--phase1a-workers` 仅作回归参数保留；quantum_loop 强制串行 phase1a
- 删除 `concurrent.futures` 导入与 ThreadPool plumbing

### 关键文件改动
- [python_module.cc](infer/mesi_ref_sim/src/python_module.cc)
  - 新增 `WindowState` struct + `PyLocalRefSim::batch_probe` 扩展返回 win
    字段 + `PyLocalRefSim::batch_window_update`
- [ref_sim_client.py](infer/driver/ref_sim_client.py)
  - 新增 `LocalPybindBackend.batch_window_update(cid, fields, committed_mask)`
- [inference_driver.py](infer/driver/inference_driver.py)
  - phase1a 改 10 元 tuple；删 `win.derive_before_update`/`win.update`/
    `commit_speculative` 调用；phase1c 末尾 `sim.batch_window_update`
  - phase3_flush 改 orjson + bytearray
  - quantum_loop 强制串行 phase1a；删 ThreadPool

### E 段守门记录
| 阶段 | 5K Δt=1 bit-exact | 50K rps | 100K rps | CPI 50K / 100K |
|---|---|---|---|---|
| baseline (D.5a) | OK | 34.8K | 34.4K | 0.4600 / 0.4553 |
| E.1 | OK | 32.4K | 32.0K | 0.4600 / 0.4553 |
| E.2 | OK | 35.1K | 33.2K | 0.4600 / 0.4553 |
| E.3 | OK | 35.7K | 36.4K | 0.4600 / 0.4553 |
| **E.4** | **OK** | **40.4K** | **40.6K** | 0.4600 / 0.4553 |

### E 段下一步（E.5+ 待启动）
- E.4 后剩余 cumtime（100K 12.0s 中）：parquet load 6.0s、phase1c 2.6s（其中
  group_features 0.64s + ReferenceClock.step 0.42s）、batch_probe pybind
  1.2s、phase1a 列表生成 0.87s
- 候选：(1) parquet load 改 mmap 或一次 columnar；(2) ReferenceClock.step
  下沉 C++；(3) phase1a fields 元组构造改 numpy struct；(4) 多核多进程切片
  （单核纯 CPU 已 40K，4 核多进程理论可上 150K+）

---


