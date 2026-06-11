# Driver + mesi_ref_sim 性能优化学习案例

> 记录从 baseline 23K rows/s 到 360K rows/s（100K BIN）的全部优化点、瓶颈识别方法、
> 一致性守门策略与经验教训。可以脱离上下文阅读。
>
> 关联设计：[quantum-parallel-progress.md](infer/docs/quantum-parallel-progress.md)
> 关键文件：
> - [inference_driver.py](infer/driver/inference_driver.py)
> - [ref_sim_client.py](infer/driver/ref_sim_client.py)
> - [python_module.cc](infer/mesi_ref_sim/src/python_module.cc)
> - [reference_clock.py](infer/driver/reference_clock.py)
> - [windowed_features.py](infer/driver/windowed_features.py)

---

## 0. 总览

### 性能演进（mock 模式，K=32，Δt=256，单核）

| 阶段 | 100K rows/s | vs baseline | vs 上一阶段 | 主要手段 |
|---|---|---|---|---|
| baseline | 23K | 1.0× | — | Python phase1a/b/c + RefSim 每 µop pybind |
| C.1 | 34.0K | 1.48× | +47% | windowed_features 增量计数器 |
| C.2 | 35.4K | 1.54× | +4% | dict 装回字面量化 |
| D.0 | 34.9K | 1.52× | -1% | step() 拆 CoreLocal/SharedState |
| D.1 | 33.4K | 1.45× | -4% | WriteSet 双写过渡 |
| D.2 | 33.2K | 1.44× | -1% | probe overlay-only |
| D.3 | 34.1K | 1.48× | +3% | pybind GIL release |
| D.4 (w=1) | 34.1K | 1.48× | 0% | ThreadPool plumbing |
| D.5a | 34.4K | 1.50× | +1% | atomic line + bank lock，删 overlay/write_set |
| E.1 | 32.0K | 1.39× | -7% | batch_probe 单次 pybind（架构铺垫，单看降速） |
| E.2 | 33.2K | 1.44× | +4% | WindowedFeatures 下沉 C++ |
| E.3 | 36.4K | 1.58× | +10% | phase1c 删每 µop commit_speculative |
| E.4 | 40.6K | 1.77× | +12% | orjson + bytearray 批写 jsonl |
| E.5 | 122.1K | 5.31× | +200% | functional rows → numpy SoA + lexsort |
| E.6 | 140.8K (BIN) | 6.12× | +15% | phase1c 下沉 C++ commit_quantum + 56B BIN |
| F.1 | 205.7K (BIN) | 8.94× | +46% | batch_probe_pod ndarray 输入 |
| commit_quantum_pod | **360.2K (BIN)** | **15.7×** | +75% | commit_quantum ndarray 输入 |

JSONL 路径同步：270.0K rps（commit_quantum_pod 下）。两条路径全程通过
5K Δt=1 mock JSONL bit-exact + 5K BIN 8 字段 semantic guard。

### 一句话总结

最大的两次飞跃都来自**消除大量小对象 / 小调用**：
- E.5（+200%）：把 functional rows 从 list[dict] 改成 numpy SoA，去掉每行 dict 解析；
- commit_quantum_pod（+75%）：把 phase1c 的 list[tuple] 入参改成 ndarray，去掉每 µop 的 Python tuple 构造。

剩余热点都是"Python 边界开销"，不是"算法太慢"。

---

## 1. 方法论

### 1.1 守门优先：先定 baseline 再优化

每次改动前先固定两条 baseline：

1. **5K Δt=1 mock JSONL bit-exact**：`diff -q /tmp/smoke_coord.jsonl /tmp/smoke_<tag>.jsonl`，
   要求字节完全一致。任何破坏 bit-exact 的改动会被立刻拦截。
2. **5K BIN semantic guard**：56 字节定长 record `<IIQddddd`，按 8 字段
   `(core_id, thread_id, micro_seq, fc, rc, fl, el, mp)` bit-equal 比较。

每完成一个阶段都跑一次 5K 守门、再跑 50K/100K 吞吐。守门 fail 直接回滚。

### 1.2 cProfile + cumtime 定位

100K mock 12s 时的典型 profile（E.4 后）：

```
parquet load    6.0s   ← I/O，下沉到 SoA 后变 0.4s
phase1c         2.6s   ← group_features 0.64 + ReferenceClock.step 0.42 + dict 装回
batch_probe     1.2s   ← pybind crossing
phase1a         0.87s  ← 列表生成 + tuple 构造
```

每次改动后只读 cumtime 前 10 行，不去研究 tottime——cumtime 才告诉你"调用栈整体花了多少时间"。

### 1.3 边界开销 ≠ 算法开销

这是本案中反复出现的教训：

- C++ 算法本身从未变慢（D.5a 之后基本定型）
- 提升来自**减少 Python ↔ C++ crossing 的频次和单次开销**：
  - 频次：每 µop crossing → 每 quantum crossing（E.3、E.6）
  - 单次开销：list[tuple] / dict → ndarray + raw pointer（E.5、F.1、commit_quantum_pod）

经验法则：**单次 pybind crossing 在 GIL 下消耗 ~1µs；构造一个 10 元 tuple ~0.5µs**。
100K rows × K=32 = 3.2M µops，每行少一次 tuple 构造能节省 1.6s，对 12s 的总时间是 13%。

### 1.4 接口下沉的代价

E.1（batch_probe）单看是 -7%，因为多了一层 pybind 包装；但它是 E.2/E.3 的架构基础。
**不要单独评估架构改造的当下收益**，要评估它解锁的下游收益。

---

## 2. 阶段详解

### C.1 windowed_features 增量计数器（+47%）

**瓶颈**：[windowed_features.py](infer/driver/windowed_features.py)
每次 `update()` 都用 `sum(1 for x in deque if ...)` 重算窗口大小，O(N) 重复扫描。

**改动**：维护 `mem_count64`、`br_count64` 等增量计数器。`int.bit_length() - 1` 替代 `math.log2(int)`。

**教训**：`sum(generator)` / `math.log2` 在热路径就是性能毒药。如果一个状态量是
"加进去 / 弹出去"的形式，几乎一定能维护一个 O(1) 计数器代替每次扫描。

### C.2 phase1c dict 装回（+4%）

**瓶颈**：`_build_feature_row` 用 5 次 `dict.update()` 拼最终行；连续 update 的成本
比一次字面量字典构造高 3-4×。

**改动**：`{**a, **b, **c, **d, **e}` 字面量解包；`row.get` 缓存到局部变量；共享 `D_ZERO` 单例。

**教训**：在 1k+ qps 的 hot path 里，"显得啰嗦"的字面量 `{**...}` 反而比"看着干净"的
`update()` 快。

### D.0/D.1/D.2/D.3/D.4 真私有 + 锁拆分（净持平）

**目的**：为多核并发铺路（D.5b），不是直接收益。

**关键设计**：
- D.0 把 `step()` 改写为纯函数 `stepImpl(CoreLocal&, SharedState&, ...)`，
  显式声明私有/共享数据
- D.2 引入 overlay：probe 不动 base，写到 lazy copy-on-touch 的 overlay
- D.3 在 GIL release 包裹 C++ 段；dict 构造仍在 GIL 下
- D.4 引入 ThreadPool（workers≥2 时启用）

**教训**：单线程吞吐**没有损失**就是 D 阶段的胜利。绝大多数"为并发改造"的项目
会在单线程留下 5-10% 的 overhead；本案靠 inline 与 RAII view helper 把它压回 0。

### D.5a atomic line + bank lock（删 overlay/write_set）

**改动**：
- `AtomicLine` 64-bit raw（state 2 bit + owner 8 bit + sharer 4 bit），CAS-loop 状态机
- `LinesShard×64` shared_mutex；`unique_ptr<AtomicLine>` 防 rehash 失效
- `BankLockedLRU` per-bank mutex
- 删 `WriteSetOp` / `lines_overlay` / `recent_line_count_overlay` / 6 个 view helper
- 删 `Coordinator::shared_mu_`

**教训**：在并发铺路阶段引入的"过渡机制"（write_set、overlay）一旦验证可以直接走
atomic + 分片锁，要**主动删除**；否则它们会变成永久 deadweight。本次清理后 simulator
代码量净减少 ~200 行。

### E.1 batch_probe（-7% 单看，架构基础）

**改动**：`PyLocalRefSim::batch_probe(fields)` 单次 pybind 替代 K×(probe + probeIFetch)。
driver phase1a 改 10 元 POD tuple 列表，C++ 内部循环。

**教训**：**接口改造与性能优化要分离评估**。单看 -7% 不能否决，因为后续 E.2/E.3 都
依赖这个接口形态。

### E.2 WindowedFeatures 下沉 C++

**改动**：新增 `WindowState` C++ struct，按位对齐
[windowed_features.py](infer/driver/windowed_features.py)
的 win64 / win256_cl / win1024_cl / win256_dram + 7 张 unordered_map。
`batch_probe` 同一段 GIL release 内同时返回 16 oracle 字段 + 12 win 字段。

**教训**：C++ 端复刻 Python 数据结构时，**同名同序**是关键——这样 5K bit-exact
守门可以直接复用，不需要重写比较逻辑。

### E.3 删 phase1c 每 µop commit_speculative（+10%）

**瓶颈**：D.5a 后 `LocalRefSim::commit()` 仅生成未使用的 LineDelta，
phase1c 每 µop 调 `sim.commit_speculative` 是纯 pybind crossing。

**改动**：直接删除调用。50K 节省 200K 次 crossing；100K 节省 400K 次。

**教训**：**重构后要主动检查"留下的接口还做不做事"**。本次发现的接口 100% 是 no-op，
但因为没人去看就一直被调用。

### E.4 jsonl 写盘加速（+12%）

**改动**：
- `import orjson`（已校验与 `json.dumps(separators=(',',':'))` 字节相同）
- `phase3_flush` 用 `bytearray` 聚合，一次 `fout.write` 批量 syscall
- jsonl 文件改二进制 + 1MB buffer

**教训**：syscall 不便宜。100K 行 / 一行一次 write = 100K syscalls，改成
buffered bytearray flush 后变成 ~10 次 syscall。

### E.5 functional rows → numpy SoA（+200%，最大单次飞跃）

**瓶颈**：parquet load 6.0s / 12s = 50% 总时间；driver 每行都通过
`row.get("paddr")` 访问 Python dict。

**改动**：
- 新增 `FunctionalSoA(macro_pc, paddr, cl, is_load, ...)` 即 numpy 列数组
- `load_functional_dir_soa` 用 pyarrow 一次性 `column.to_numpy()`
- `numpy.lexsort((micro_seq, macro_idx, ...))` 替代 Python sorted
- driver phase1a 直接 slice ndarray `soa.macro_pc[idx_lo:idx_hi]`，零拷贝
- ckpt 路径保留 `_materialize_row_dicts(soa, soa_path)` 兜底

**教训**：
1. **dict-of-anything 是 Python 性能终极反模式**。任何能改成 SoA 的数据结构都应该改。
2. **lexsort** 比 Python sorted 快 50-100×（就 stable sort 而言），因为完全在 C 层。
3. 只在真正需要稀疏字段（producer_dists / cacheline_addr）的 ckpt 模式才物化 dict，
   mock/label 路径完全跳过。

### E.6 phase1c 下沉 C++ commit_quantum + 56B BIN（+15%）

**瓶颈**：phase1c 还在 Python 跑 deadline walk + ReferenceClock + win.update。

**改动**：
- C++ 新增 `commit_quantum(core_id, fields11, fetch_lats, exec_lats, mispreds, ...)`
  消化 list[tuple]，做 deadline walk + clock + win.update
- 新增 `--out-format {jsonl,bin}`：BIN 路径让 C++ 直接产 56 字节定长 record
  `<IIQddddd` (core_id u32 / thread_id u32 / micro_seq u64 / fc / rc / fl / el / mp doubles)
- BIN 守门 = 8 字段 `struct.unpack_from` bit-equal

**教训：为什么不让 C++ 直接产 JSONL？**
- `std::to_chars(double)` 与 `orjson.dumps(double)` 在边角浮点上字节不同
  （IEEE 754 → string 是有"shortest round-trip"算法分歧的）
- 一旦 C++ 产 JSONL，5K bit-exact 守门会立即崩溃，且很难诊断哪一位浮点出问题
- 选择**定长 BIN**：浮点直接 memcpy 8 字节，无序列化问题；JSONL 路径仍由 Python 产，守门保留

### F.0/F.1/commit_quantum_pod ndarray 化（+46% / +75%）

**瓶颈**：cProfile 显示 phase1c_commit cumtime 1.4s 中，~0.6s 是
`list((cid, idx, ...))` 与 `list(fetch_lats)` 这种 small list 构造与 pybind 转 vector。

**改动**：
- `batch_probe_pod`：phase1a 用 ndarray slice 直接传 C++，
  C++ `py::array_t<T, c_style|forcecast>::request().data()` 取 raw pointer
- `commit_quantum_pod`：phase1c 用 `np.fromiter` 把 PendingProbe 的 d_bank_id /
  preds_fl / preds_el / preds_mp / label_fetch_ticks 收成 5 个 ndarray，连同
  SoA 切片一起一次性传 C++
- C++ 端**释放 GIL** 后用 raw pointer 跑全循环，事件回 GIL 时构造 56B bytes 或 jsonl dict

**教训**：
1. **小 list / small dict 转 pybind 远比想象的贵**。10 元 tuple 列表 100K 个，
   单次 pybind cast 就是 0.3-0.5s 的纯 marshalling 开销。
2. **ndarray 切片几乎免费**：`soa.paddr[idx_lo:idx_hi]` 不复制，C++ 通过
   `py::array_t::data() + offset` 直接读。
3. **GIL release 区只用 POD/raw pointer**，不要持有 `py::object`。事件构造
   （bytes / dict）回到 GIL 后再做。
4. **保持 idx 连续假设**：commit_quantum_pod 假设 `feat_buf` 中 idx 是连续的
   `[idx_lo, idx_lo+n)`，runtime check `np.array_equal(idx_arr, np.arange(...))`
   不连续就退回旧路径。

---

## 3. 一致性守门完整方案

### 3.1 双守门设计

| 模式 | 校验工具 | 通过条件 |
|---|---|---|
| JSONL | `diff -q /tmp/smoke_coord.jsonl /tmp/smoke_<tag>.jsonl` | 字节完全一致 |
| BIN | `python -c 'import struct; ...'` 8 字段比较 | bit-equal `(core_id, thread_id, micro_seq, fc, rc, fl, el, mp)` |

### 3.2 守门用例

```bash
PYTHON=/root/miniconda3/envs/yinhaolang/bin/python

# JSONL 5K Δt=1 bit-exact
$PYTHON -m driver.inference_driver \
  --functional-dir /tmp/smoke_func_5k \
  --uarch-profile data/W11_stream_mix/uarch_profile.json \
  --ref-sim-module-dir mesi_ref_sim/build \
  --out-jsonl /tmp/smoke_<tag>.jsonl \
  --report-json /tmp/smoke_<tag>.report.json \
  --mock-model --quantum-cycles 1 --k-max 32 \
  --ref-sim-backend coordinator
diff -q /tmp/smoke_coord.jsonl /tmp/smoke_<tag>.jsonl  # 应无差异

# BIN 5K Δt=1 semantic guard
$PYTHON -m driver.inference_driver ... --out-format bin --out-jsonl /tmp/smoke_<tag>.bin
$PYTHON tools/bin_guard.py /tmp/smoke_<tag>.bin /tmp/smoke_coord.jsonl
```

### 3.3 哪些改动会破坏 bit-exact

不破坏（已验证）：
- 算法逻辑下沉 C++（顺序、累加顺序保持）
- ndarray slice 替换 list/dict 输入
- GIL release / 锁结构改造
- 接口形状改变（list → ndarray）

会破坏（必须避开）：
- 浮点累加顺序变化（reduce 顺序、SIMD 向量化）
- C++ 端直接产 JSONL（`std::to_chars` vs `orjson.dumps` 浮点 shortest 算法不同）
- float32 BIN（精度损失）
- 跨核浮点合并的并行执行（多核走 E.7 时需换 baseline）

---

## 4. 总结：优化优先级矩阵

| 优先级 | 类型 | 例子 | 收益 | 风险 |
|---|---|---|---|---|
| P0 | 数据布局：list[dict] → SoA | E.5 | 200%+ | 中（需重写 access） |
| P0 | 接口形状：list[tuple] → ndarray | F.1 / commit_quantum_pod | 50-75% | 低 |
| P1 | 减少 pybind crossing 频次 | E.3 / E.6 | 10-15% | 低 |
| P1 | C++ 算法下沉 | E.2 / commit_quantum | 5-15% | 中（需复刻 Python 数据结构） |
| P2 | I/O 缓冲 / orjson | E.4 | 10-15% | 低 |
| P2 | 增量计数器替代 sum/log | C.1 | 40-50% | 低（但案例特殊） |
| P3 | 字面量解包替代 update | C.2 | 3-4% | 低 |
| 防守 | GIL release / atomic line | D.3 / D.5a | 0%（解锁多核） | 高 |
| 谨慎 | 多核并行 | E.7（未做） | 2-3× | 高（需换 baseline） |

**首要原则**：**先做 P0**。本案 D 阶段花了大量精力做并发铺路（净收益 0%），
如果先做 E.5（200%）再回头做 D，整体节奏会更合理。这是事后总结的最大教训。

---

## 5. ckpt 推理路径复盘（V10.3-ma16，2026-06）

> 这一节记录从“mock 路径已达 360K rps”切换到“真实 ckpt 推理”后，围绕
> schema 对齐、吞吐优化、CPI/单位对账、ref_sim oracle 准确性排查的完整结论。
> 目标不是展示最终性能，而是保留一套可复用的问题定位范式。

### 5.1 本轮目标

1. 用 `MTAO/ckpt/tao_v10_3_ma16.best.pt` 验证当前 infer 框架
2. 以 `MTAO/SCHEMA.md` 为全局统一标准，对齐 train / infer / driver
3. 把 5K ckpt smoke 吞吐从 CPU 慢路径提升到可用的 GPU 路径
4. 搞清楚 `CPI` 为什么和 gem5 baseline 差异巨大
5. 判断问题来自 driver 公式、oracle 质量，还是模型本身

### 5.2 本轮关键结论

#### 结论 A：schema 不一致是真问题，但不是 CPI 偏差的唯一根因

- infer 侧 `ml/{dataset.py,model.py,infer.py}` 一度落后于 train 侧，已直接同步到
  [dataset.py](infer/ml/dataset.py)、
  [model.py](infer/ml/model.py)、
  [infer.py](infer/ml/infer.py)
- driver 侧 group features 改为 SCHEMA.md 的 V10.3-ma16 口径：
  `is_macro_head`、`uop_pos_in_macro`、`i_group_head`、`i_group_pos`
- `i_group_bkt` 不再作为模型输入；`i_oracle_source` 仍保留在数据里但不喂模型

#### 结论 B：ckpt 路径最初的 CPI 异常，先是 feature 漏接，再是单位误读

- [inference_driver.py](infer/driver/inference_driver.py)
  里 `_build_feature_row` 曾定义但未实际接入 phase1a，导致 win features 没进入 feature row
- 修复方式：在 `phase1a_probe` 显式调用
  `st.win.derive_before_update(...)` 和 `st.win.update(...)`
- 此后确认 driver 用的 `fetch_lat` / `exec_lat` 是 **`expm1` 后的绝对值，但单位仍是 tick**
- 训练标签来自 `log1p(fetch_latency)` / `log1p(execution_latency)`，见
  [dataset.py](infer/ml/dataset.py#L368-L373)
- 推理端在 [inference_driver.py](infer/driver/inference_driver.py#L852-L861)
  用 `np.expm1(...)` 还原，因此 driver 当前 `total_cycle` / `cpi_macro` 实际是
  `tick` 口径，不是 `cycle`

#### 结论 C：driver OoO 公式是对的

- 用 label-driven 5K 对账时，`label_driven_ready_end_diff` 四核全为 0
- 说明 `ReferenceClock` / `ready_clock=max(prev_ready, fetch_clock+exec_lat)` 这条
  时序公式本身没有问题
- 真值切片（5K）下，label-driven CPI 约为 `1014.57 ticks/macro`
  `= 3.05 cycles/macro`（按 333 ticks/cycle 换算）

#### 结论 D：模型当前是显著低估 latency，不是高估

- 修正单位后，5K ckpt 当前结果约为 `31.47 ticks/macro`
  `= 0.094 cycles/macro`
- 相比 label-driven truth `3.05 cycles/macro`，模型约低估 `32x`
- 这与“模型预测尾延迟 outlier 不足”一致，不是 driver 公式导致

### 5.3 吞吐优化链路（ckpt 路径）

#### 阶段 1：放大量子 + 放大 batch，先把 GPU 吃满

在不改模型结构的前提下，先做两个最直接的调度优化：

- `--quantum-cycles` 从 `1` 提到 `256`，再进一步提到 `4096` / `16384`
- `--model-batch-size` 从小 batch 提到 `1024`

观察到：

| 配置 | avg_B | fwd | 现象 |
|---|---|---|---|
| Δt=256，小 batch | 很低 | 很高 | GPU 明显吃不满 |
| Δt=4096，bs=1024 | 约 345 | 7s 级 | 明显改善 |
| Δt=16384，bs=1024 | 约 952 | 4.6s 级 | 基本到达当前 `k_max=256 × 4 cores` 饱和点 |

经验：

- `quantum` 变大确实能提升并行度，但只能提升到单 quantum 可产出的 probe 上限
- 当前 driver 的有效 batch 上限受 `k_max × n_cores` 约束，继续无限增大 `quantum`
  不会线性继续变快

#### 阶段 2：SoA-history 改造，解决 enc 死成本

profiling 发现 ckpt 路径不是一开始就卡在 forward，而是先卡在
`feats_to_window` 的 Python dict/list 组装：

- 旧路径：每条 µop 对 50+ 特征做 `ctx_len=128` 的 Python 层切片和 dict 查找
- 表现：`enc` 阶段约 `15.5s`

改造：

- 在 [inference_driver.py](infer/driver/inference_driver.py)
  中新增 `_HistSoA`
- 把 per-history history 改成 SoA mirror
- 用 `_build_batch_soa(items)` 直接一次性构造 `[B, ctx_len]` numpy 矩阵

结果：

| 指标 | 改造前 | 改造后 |
|---|---|---|
| enc | 15.5s | 1.34s |
| wall（5K） | 54s 级 | 29s 级 |
| 5K 吞吐 | 约 370 rps | 约 690 rps |

结论：

- SoA-history 是这一轮 ckpt 路径中收益最大的工程优化
- 做完以后，瓶颈从 `enc` 转移到 `fwd`

### 5.4 ref_sim oracle 对账结论

#### d-side：有一个真实实现 bug，已修

排查 [simulator.hpp](infer/mesi_ref_sim/include/simulator.hpp)
发现 d-side MSHR 之前是：

```cpp
local.l1d_mshr.insert(cl, ev.seq);
local.l1d_mshr.retire(cl);
size_t md = local.l1d_mshr.size();
```

这会导致 `d_mshr_depth` 基本恒为 0。已改为：

- 先读 `size`
- 再 `insert`
- 不在 probe 阶段立即 `retire`

修复后对账变化：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| sim `d_mshr_depth` non-zero | 0.00% | 24.64% |
| mean | 0.000 | 3.625 |
| pearson vs truth | nan | 0.729 |

5K ckpt smoke 的 CPI 也有小幅改善：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| cpi_macro | 33.14 ticks/macro | 31.47 ticks/macro |

说明：

- `d_mshr_depth` 确实是有效信号
- 但它不是当前模型偏差的主因，只能带来小幅改善

#### sharer / owner / dirty / inval：暂不修，主要是 warmup 问题

字段：

- `sharer_bucket`
- `owner_dist`
- `dirty_owner`
- `inval_fanout`

对账发现：

- 5K 冷启动时 ref_sim 几乎全 0
- 即使按 `fetch_tick` 跨核交错重放，仍显著低于 gem5 truth
- 增加 `50K warmup + 5K evaluate` 后，相关性有所改善，但仍明显低估

结论：

- 这几项不是当前 ref_sim 的明显实现 bug
- 更主要原因是 gem5 truth 依赖长历史预热，而当前 smoke / 推理只看到很短的前缀
- 后续如果要修，应优先考虑 driver 增加 warmup 流程，而不是先改 coherence 状态机

### 5.5 当前主要问题清单

#### 问题 1：模型输出仍是 tick 域，driver/报告字段名却写成 cycle

- 现状会误导 CPI 解读
- `cpi_macro`、`total_cycle` 当前都应理解为 tick 口径
- 后续训练侧改成 cycle 标签前，建议在报告字段或文档上显式标注单位

#### 问题 2：模型严重低估尾延迟

- 即使修复 d-side MSHR，5K ckpt 仍只有 `31.47 ticks/macro`
- 相比 truth `1014.57 ticks/macro` 低约 `32x`
- 这说明当前模型对大尾部 latency 分布学习不足

#### 问题 3：forward 已成为新的主瓶颈

- SoA-history 完成后，`enc` 已压到 `1.34s`
- 当前 5K / Δt=16384 / bs=1024 下，`fwd` 约 `4.66s`
- 这意味着继续优化 driver Python 侧收益会越来越小，后续需要转向
  kernel launch、embedding gather、固定 batch CUDA graph 等方向

#### 问题 4：部分 oracle 字段与训练真值仍不一致

- d-side：`d_mshr_depth` 已修，但 sharer/owner 类字段仍明显偏低
- i-side：多项字段和 gem5 truth 一致率偏低，目前未作为本轮优先级处理

### 5.6 当前可复现结果

#### label-driven 5K（真值校验）

- driver 公式对账：`ready_end_diff = 0`
- `cpi_macro = 1014.57 ticks/macro = 3.05 cycles/macro`

#### ckpt 5K（当前最新）

- 命令参数：`Δt=16384`、`k_max=256`、`model_batch_size=1024`
- 当前结果：
  - `rows = 20000`
  - `total_macro = 10669`
  - `cpi_macro = 31.46539225691383`（tick/macro）
  - `total_cycle = 335704.2699890137`（实际是 tick）
  - `[infer-prof] calls=21 avg_B=952.4 enc=1.34s h2d=0.18s fwd=4.66s d2h=0.01s`
  - wall `35.1s`

### 5.7 这轮工作的经验教训

1. **先统一单位，再谈 CPI 对错**
   同一个 `31.47`，如果误以为是 cycle，会得出完全错误的结论。

2. **先证伪 driver，再怀疑模型**
   label-driven `ready_end_diff=0` 这一条非常关键，先把 driver 公式从嫌疑名单里划掉。

3. **oracle 对账要分“实现 bug”和“预热不足”**
   `d_mshr_depth` 属于实现 bug；`sharer_bucket` 这类更像 warmup / 历史窗口问题。

4. **真实 ckpt 路径与 mock 路径是两套瓶颈**
   mock 路径的胜利手段是“减少 pybind / dict / tuple”；ckpt 路径继续往前走，瓶颈已经转到
   `history encoding + GPU forward + 模型质量`。

### 5.8 下一步建议

按优先级排序：

1. **统一 latency 单位**
   训练链路把 label 从 tick 改成 cycle；infer/report 同步改字段名和注释。

2. **跑 pred-vs-truth 分布对账**
   重点看 fetch / exec 的 quantile，确认模型是整体缩放错、还是尾部单独学不到。

3. **继续做 fwd 优化**
   候选：固定 batch + CUDA Graph、embedding gather 合并、`torch.compile`

4. **如需提升 oracle 质量，再补 warmup**
   在 driver 增加 `warmup_records`，优先观察 sharer/owner 字段是否明显接近 truth。

### 5.9 长尾特征对齐矩阵

> 这一节专门回答：对于 execution latency 长尾建模中最敏感的一批特征，
> 当前 infer / ref_sim 路径到底能不能和 gem5 / 训练数据对齐。
>
> 需要先区分两类“对齐”：
>
> 1. **与 gem5 原生 oracle 逐行对齐**
>    - 指字段由 gem5 probe 直接在 `records.micro.jsonl` 中输出，ref_sim 可以直接拿
>      同名字段逐行比较。
> 2. **与训练数据最终喂模特征对齐**
>    - 指字段不是 gem5 原生 probe 列，而是后处理脚本根据同一条 trace 严格因果滑窗
>      派生出来；infer 侧只能按同一公式重算对齐，不能直接和 gem5 原生 probe 逐行比较。

| 特征 | 类型 | 当前是否可对齐 | 当前可信度 | 主要偏差源 | 结论 |
|---|---|---|---|---|---|
| `mem_density_W64` | 后处理滑窗派生 | 可与训练特征对齐 | 高 | 切片时 warmup 不足会偏低 | 可放心使用 |
| `bank_conflict_W64` | 后处理滑窗派生 | 公式上可对齐，但当前 W11 下退化 | 低 | `l1d.num_banks=1`，`d_bank_id` 无熵 | 当前不可靠 |
| `unique_cl_W256` | 后处理滑窗派生 | 可与训练特征对齐 | 高 | 切片 / warmup 不足 | 可放心使用 |
| `unique_cl_W1024` | 后处理滑窗派生 | 可与训练特征对齐 | 高 | 切片 / warmup 不足 | 可放心使用 |
| `d_llc_set_residency` | gem5 原生 oracle | 可直接逐行对齐 | 中 | ref_sim 冷启动、shared state 演化仍有偏差 | 可用但非 bit-exact |
| `d_mshr_depth` | gem5 原生 oracle | 可直接逐行对齐 | 中 | ref_sim MSHR 生命周期仍是近似 | 可用但需继续修 |
| `cl_reuse_dist_log` | 后处理滑窗派生 | 可与训练特征对齐 | 高 | 切片时丢失历史 | 可放心使用 |
| `dram_bank_freq_W256` | 后处理滑窗派生 | 可与训练特征对齐 | 中高 | 仅对齐简化 bank 映射，不等于物理 DRAM 真值 | 适合做特征，不适合做物理解释 |

#### 5.9.1 哪些字段是 gem5 原生 oracle

在这批长尾敏感特征里，真正由 gem5 probe 原生输出、可以直接和 ref_sim 做同名逐行
对账的只有两项：

- `d_llc_set_residency`
- `d_mshr_depth`

证据：

- [tao_trace.hh](datagen/gem5_patches/src/cpu/o3/probe/tao_trace.hh#L90-L101)
  明确把这两项定义在 d-side oracle 结构中
- [tao_trace.cc](datagen/gem5_patches/src/cpu/o3/probe/tao_trace.cc#L1756-L1782)
  给出了它们在 gem5 侧的采样时机和计算口径

当前状态：

- `d_llc_set_residency`：方向正确，相关性较高，但 warmup 不足导致并非 bit-exact
- `d_mshr_depth`：修复 insert/retire bug 后显著改善，但仍不是完整 gem5 生命周期

因此，这两项的正确问题表述是：

- “能不能和 gem5 原生 oracle 输出逐行对齐？”
- 答案：**能，但目前还是近似对齐，不是完全对齐**

#### 5.9.2 哪些字段是训练后处理派生列

以下字段并不是 gem5 probe 原生直接吐出的列，而是数据打包时基于同一条 trace 做严格因果
滑窗派生得到：

- `mem_density_W64`
- `bank_conflict_W64`
- `unique_cl_W256`
- `unique_cl_W1024`
- `cl_reuse_dist_log`
- `dram_bank_freq_W256`

证据：

- [pack_to_parquet.py](datagen/tools/pack_to_parquet.py#L401-L425)
  定义了 `mem_density_W64`、`bank_conflict_W64`、`cl_reuse_dist_log`
- [pack_to_parquet.py](datagen/tools/pack_to_parquet.py#L696-L700)
  明确说明这些列依赖排序后的历史窗口，且 `d_bank_id` 缺失时 `bank_conflict_W64`
  会退化

因此，这些字段的正确问题表述不是：

- “能不能和 gem5 原生 oracle 输出逐行对齐？”

而应该是：

- “infer 侧能不能按同一条 trace、同一因果顺序、同一窗口公式把训练特征重算出来？”

对这类字段，只要满足以下条件，就能高精度对齐训练特征：

1. 行顺序一致
2. thread/core 边界一致
3. warmup 历史一致
4. 底层依赖列一致（例如 `bank_conflict_W64` 依赖 `d_bank_id`）

#### 5.9.3 `bank_conflict_W64` 为什么当前最不可靠

`bank_conflict_W64` 在线下分析里是最强 tail 信号之一，但在当前 W11 infer 配置下，
它恰恰是最不应盲信的字段。

原因：

1. 当前 [uarch_profile.json](infer/data/W11_stream_mix/uarch_profile.json)
   中 `l1d.num_banks = 1`
2. 这意味着 `d_bank_id` 基本没有熵，`bank_conflict_W64` 会退化成“窗口内 mem-touching
   次数”的一个影子量
3. 训练打包脚本自己也承认了这种退化情况，见
   [pack_to_parquet.py](datagen/tools/pack_to_parquet.py#L696-L700)

所以：

- 如果把 `bank_conflict_W64` 当作“当前模型里的一个数值特征”，它当然还存在
- 但如果把它解释成“真实 bank conflict 强弱”，在当前 infer 配置下就是不准确的

#### 5.9.4 实践建议

从长尾建模收益与当前可用性综合看，建议分三档使用：

**第一档：高可信，可优先依赖**

- `mem_density_W64`
- `unique_cl_W256`
- `unique_cl_W1024`
- `cl_reuse_dist_log`

这些特征主要依赖严格因果窗口本身，不太依赖 ref_sim 的复杂共享状态；只要顺序与 warmup
处理正确，就能稳定对齐训练特征。

**第二档：中可信，可用但要保留误差意识**

- `d_llc_set_residency`
- `d_mshr_depth`
- `dram_bank_freq_W256`

它们有明显信号价值，但要么依赖 ref_sim 共享状态近似，要么依赖简化 bank 映射，不应过度
物理解释。

**第三档：当前配置下不建议作为关键判断依据**

- `bank_conflict_W64`

如果后续要恢复它在线下分析中展示出的强 tail 信号，需要先解决：

1. infer 侧 `d_bank_id` 是否真的有足够熵
2. 当前 profile / ref_sim 是否具备多 bank 模型
3. 训练数据中的该字段是否来自更丰富的 bank 配置，而非当前 W11 的退化口径
