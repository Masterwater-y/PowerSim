# V1 多核 TAO Probe 问题修复记录

记录在 V1 多核扩展（`mt_micro_coh` workload + per-core TaoTrace probe）中遇到的所有问题及修复方法，按发现顺序排列。

---

## 问题 1：编译产物加载报 `bad marshal data`

### 现象
```
terminate called after throwing an instance of 'pybind11::error_already_set'
  what():  ValueError: bad marshal data (unknown type code)
At:
  <string>(93): install
scons: *** [build/X86/params/RandomRP.hh] Error 134
```
出现于 `scons build/X86/gem5.opt` 的 SO Param 阶段，几十个 generated header 全部 134 退出。

### 根因
gem5 的 `build/X86/python/m5/...` 下保留了**之前用其它 Python 版本生成的 `__pycache__` / `*.pyc`**。当本次 build 用的解释器版本与缓存里的 magic 不一致时，pybind11 在 `import` 编译期产物时直接抛 `bad marshal data`。

### 修复
清掉 build tree 里的所有字节码缓存：
```bash
find /data00/yinhaolang/simulators/gem5 -name "__pycache__" -type d -prune -exec rm -rf {} +
find /data00/yinhaolang/simulators/gem5 -name "*.pyc" -delete
```
之后 `scons -j 96` 一次过。

### 教训
切换 Python 解释器（`pyenv shell` / `LD_LIBRARY_PATH` 改 libpythonX.Y）后必须清缓存，不能依赖 SCons 的增量。

---

## 问题 2：gem5 二进制找不到 `libpython3.8.so.1.0`

### 现象
```
gem5.opt: error while loading shared libraries: libpython3.8.so.1.0:
cannot open shared object file: No such file or directory
```

### 根因
gem5 在 build 时 link 到了 pyenv 下的 `/root/.pyenv/versions/3.8.0/lib/libpython3.8.so.1.0`，但运行时该路径不在 `LD_LIBRARY_PATH`。

### 修复
所有 build / 运行命令都要带：
```bash
export LD_LIBRARY_PATH=/root/.pyenv/versions/3.8.0/lib:/opt/gcc-11/lib64:$LD_LIBRARY_PATH
```

---

## 问题 3：probe 字段不全，方案要求的 reg bitmap / branch_history / access_distance / fetch+exec latency 缺失

### 现象
方案文档要求 records / labels 含以下字段，旧版 probe 完全没有：
- records: `reg_read_bitmap`, `reg_write_bitmap`, `branch_history`, `access_distance`
- labels:  `fetch_latency_cyc`, `execution_latency_cyc`, `branch_mispred`

### 修复
在 `MacroAccum` 中新增累积字段，`accumulateMicro` 中按 `IntRegClass` 折叠 src/dest 寄存器到 64 bit bitmap；
`flushMacro` 中从 `inst->issueTick` / `inst->completeTick` 计算
- `exec_t = completeTick − issueTick`
- `fetch_t = macro_t − exec_t`

`access_distance` 桶（0=未见过/1=≤4/2=≤16/3=≤64/4=≤256/5=>256）在 flushMacro 末尾按 (thread, cacheline) 维度算最近一次 macro 间距；
`branch_history` 在分支 commit 后按 16-bit 移位寄存器旋转（taken 近似置 1）。

### 验证（C1.4 / C1.9-C1.11 / C4.1-C4.5）
verifier 加入新断言：
- bitmap ∈ [0, 2^64) 且非零率 ≥ 50% / 30%
- branch_history ∈ [0, 2^16)
- access_distance ∈ {0..5}，mem 行 distance≥1 比例 ≥ 10%
- `fetch_lat_cyc + exec_lat_cyc ≤ macro_cycles`，非零率 ≥ 20%
- `branch_mispred ∈ {0,1}`

100k smoke 全部通过。

---

## 问题 4：SharedAttr 91.8% 标 `UNKNOWN`（核心问题）

### 现象
首次 100k smoke 后：
```
coh_unknown frac : 91.8%
coh_action dist  : {0: 510742, 1: 45393, ...}
sharer_bucket    : {0: 553131, 1: 3104}
```
基本上每条 mem 行都被标成"未知 + 没有跨核共享者"。

### 根因（三个独立 bug 叠加）

#### 4.A — `onDataAccessComplete` 与 `accumulateMicro` 时序错位
- `DataAccessComplete` probe 在 IEW writeback 阶段触发；
- `Commit` listener（驱动 `accumulateMicro`）在 commit 阶段触发。

旧实现里 `onDataAccessComplete` 直接尝试写 `current_macro_[tid].shared_attr`：
```cpp
if (it_acc->second.valid && !it_acc->second.shared_attr.valid) {
    it_acc->second.shared_attr = a;   // 几乎永远不命中
}
```
对 load：data 完成时 micro 还在 ROB 等 commit，此刻 acc 要么 valid=false（macro 还没起），要么 first_seq != 当前 inst。
对 store：x86 store 在 commit **之后** 才下发到 mem subsystem，此时 acc 已被 flush 清空。

#### 4.B — `line_states_` per-instance，跨核根本不共享
`run_mt_mvp.py` 给每个 core 挂独立 TaoTrace 实例：
```python
core.tao_trace = TaoTrace(output_dir=...)
core.probeListener = core.tao_trace
```
而 `std::unordered_map<uint64_t, LineState> line_states_` 是 per-instance 普通成员。每核只看到自己的 line state，永远查不到"远端核持有"，所以 `sharer_count`、`REMOTE_HIT_*`、`WB_REQUIRED` 全是 0。

#### 4.C — read 路径 `sharer_count_bucket` 没排除 self
旧代码：
```cpp
a.sharer_count_bucket = bucketCount(ls.sharers.size());
```
load 命中、ls.sharers 仅含自己时显示为 1，让人误以为"有 1 个 sharer"。store 路径已经 `fanout - 1`，但 read 路径漏了。

### 修复

#### Fix A：解耦 SharedAttr 与 acc，按 seqNum 缓存
新增静态映射：
```cpp
static std::unordered_map<uint64_t, SharedAttr> pending_shared_attr_;
```
key = `(tid<<48) | seqNum`。

- `onDataAccessComplete` 不再写 acc，而是写 pending：
  ```cpp
  pending_shared_attr_[(uint64_t(tid)<<48) | inst->seqNum] = a;
  ```
- `accumulateMicro` 处理 mem-touching micro 时查 pending 回填到 acc，并 erase 防泄漏。
- `onSquash` 也 erase 对应 key 防 leak。

#### Fix A2：store fallback — 纯 line-state 推断
即便有 Fix A，x86 store 在 commit **之后** 才下发 DataAccessComplete，accumulateMicro 阶段 pending 表里仍然没有它。
新增：
```cpp
SharedAttr deriveSharedAttrFromLineState(uint64_t vaddr, uint32_t core_id,
                                         bool is_store);
```
不依赖 packet，纯靠 `line_states_[cl]` 推断 mesi_before / coh_action / sharer / dirty_owner / fanout，并按 is_store 同步更新 line state。

`flushMacro` 在写 records 前若 `acc.shared_attr.valid==false` 就调用它兜底。

#### Fix B：line_states_ 改为 process-global static
`tao_trace.hh`:
```cpp
static std::unordered_map<uint64_t, LineState>  line_states_;
static std::unordered_map<uint64_t, uint32_t>   recent_line_count_;
static std::unordered_map<uint64_t, SharedAttr> pending_shared_attr_;
```
`tao_trace.cc` 顶部定义。gem5 是单线程仿真，无需 mutex。

#### Fix C：read 路径排除 self
```cpp
size_t sc = ls.sharers.size();
if (ls.sharers.count(core_id)) sc = (sc > 0) ? sc - 1 : 0;
a.sharer_count_bucket = bucketCount(sc);
```

### 验证（100k smoke, 4 核 × 25k iter, 556k mem rows）

| 指标 | 修复前 | 一次修复后 (A+B+C) | 完整修复后 (A+A2+B+C) |
|---|---|---|---|
| coh_unknown | **91.82%** | 65.82% | **0.00%** |
| store unknown | 100% | 100% | 0.00% |
| coh 分布 | UNKNOWN 主导 | LOCAL_HIT 主导 | LOCAL 79.6% / REMOTE_DIRTY 4.3% / LLC 0.2% / WB_REQ 15.9% |
| sharer_bucket≥2 | 0% | 17.95% | 17.96% |
| mesi_before E/M | 0 | E:0.6% M:0% | E:1.8% / M:55% |
| dirty_owner | 0 | 0.03% | 15.95% |
| inval_fanout 非零 | 0 | 9 | 88526 |
| verifier C2.1–C2.5 | FAIL | FAIL | **PASS** |

---

## 问题 5：`DataAccessComplete` 对 store 在 commit 后才触发

### 现象（4.A 的延伸）
即便加了 Fix A 的 pending 表，store 100% 仍是 UNKNOWN。

### 根因
gem5 O3 中 store 的内存系统 packet 在 **commit 时**（甚至 commit 后）才发出（store buffer drain），probe 的 `DataAccessComplete` 因此晚于 `accumulateMicro` 处理同一 micro 的时刻。pending 表此时还没条目可查。

### 修复
即问题 4 的 Fix A2：用 `deriveSharedAttrFromLineState` 在 flushMacro 时兜底。
不依赖 packet 信号，仅基于 probe 自身维护的 MESI proxy 状态机推断；对 store 同样会更新 line state（M、清空 sharers），保证后续访问能看到正确的跨核视图。

---

## 关键设计原则（沉淀）

1. **probe 内任何"跨核可见"的状态必须是 static 跨实例**。 gem5 默认每个 SimObject 一个 listener 实例；想做共享 cache view 就必须 static 或者挂在更高层 SimObject 上。
2. **probe hook 触发时机不一定与 commit 对齐**，特别是 store。任何依赖"现在能看到 acc"的写法都不可靠。正确做法是按 seqNum/tid 把 hook 数据缓存进侧表，由 commit 阶段主动消费 + 兜底。
3. **跨实例统计要排除 self**：sharers 集合维护"曾经访问过 line 的所有核"时，自家的 core_id 只在做"远端可见数"时才能算。
4. **bad marshal data → 清 build/X86 下的 *.pyc / __pycache__**，等同于 SCons "clean python codegen"。
5. **`LD_LIBRARY_PATH` 配置一次性给 build + run 同时用**，不要只在 build 时加。

---

## 文件改动一览

- `gem5/src/cpu/o3/probe/tao_trace.hh`
  - `MacroAccum` 加 `reg_read_bitmap, reg_write_bitmap, access_distance_bucket, last_issue_tick_delta, last_complete_tick_delta, last_mispredicted`
  - `line_states_, recent_line_count_, pending_shared_attr_` 改为 `static`
  - 新增 `deriveSharedAttrFromLineState` 声明
  - `writeRecordsLine` / `writeLabelsLine` 签名扩展

- `gem5/src/cpu/o3/probe/tao_trace.cc`
  - 顶部定义三个 static 表
  - `accumulateMicro`：累积 reg bitmap / 缓存 issue/complete tick / 回填 pending SharedAttr
  - `flushMacro`：fetch/exec latency 拆分 + access_distance + branch_history rotate + Fix A2 兜底
  - `onDataAccessComplete` 改为只写 pending
  - `onSquash` erase pending 防泄漏
  - 新增 `deriveSharedAttrFromLineState`
  - read/store 路径 sharer_count 排除 self
  - `writeRecordsLine` / `writeRecordsSyscallLine` / `writeLabelsLine` 函数体输出新字段

- `single_core_mvp/scripts/verify_trace_coh.py`
  - `REQUIRED_RECORDS_KEYS` / `REQUIRED_LABELS_KEYS` 增加新字段
  - 新增 C1.9 / C1.10 / C1.11 / C4.1-C4.5 断言

- `single_core_mvp/scripts/build_mt_window_dataset.py`（新增）
  - V1 多核窗口数据集构建器（128-macro window，target+history+labels+sched_state）
