# gem5 stat 与物理机 perf PMU 语义对齐方案

本文整理 `gem5 stats.txt` 中的仿真 PMU/微架构统计与物理机 `perf` 采集 PMU 之间的语义对齐方案。目标是：对同一类 workload/ROI，同时获得 gem5 仿真统计与物理机 PMU 统计，并训练一个机器学习模型完成 `gem5 stat -> real PMU` 的映射。

核心结论：**不要追求完整 workload 的 gem5 仿真**。完整负载在 gem5 O3/Ruby 下通常有数万到数十万倍放大，成本不可接受；更合理的方法是使用可 warm-up、可截断、可重复的 ROI/window 样本。

推荐路线：

```text
物理机完整或长时间运行，用低成本方式发现 phase
    ↓
选取代表性 ROI/window
    ↓
物理机在 ROI 边界 enable/disable perf PMU
    ↓
gem5 在同一 ROI 边界 reset/dump stats
    ↓
生成 window 级训练样本
    ↓
训练 gem5-stat-rate -> perf-PMU-rate 映射模型
```

---

## 1. 为什么不做完整 workload 对齐

完整 workload 的问题有两个：

1. **gem5 detailed 仿真太慢**
   对 O3CPU + Ruby MESI 这类配置，一个完整 benchmark 可能需要数万到数十万倍 wall-time 放大。即使物理机只跑几秒，gem5 也可能需要数小时到数天。

2. **完整 workload 的统计反而不利于语义建模**
   完整程序混合了初始化、内存分配、线程创建、warm-up、稳态计算、phase 切换、cleanup 等多个阶段。把这些阶段混在一个总计数里，会让模型难以学习具体 PMU 语义。

因此，训练映射模型时应将样本粒度从完整 workload 降为 **ROI/window**：

```text
X = gem5 在某个 ROI/window 内的 normalized stats
Y = 物理机 perf 在同一 ROI/window 内的 normalized PMU
```

---

## 2. 极短负载可以用，但不能从 main 开始直接计数

极短负载的主要风险是 **冷启动污染**。如果程序刚启动就开始计数，很多 PMU 事件会被初始化阶段放大：

| 指标类型 | 冷启动影响 |
|---|---|
| I-cache miss | 程序代码刚进入 cache，miss 偏高 |
| D-cache miss | 数据工作集尚未稳定 |
| LLC miss | 初始填充阶段与稳态行为差异很大 |
| TLB miss | 页表和 TLB 未 warm |
| Branch miss | 分支预测器未 warm |
| Page fault | 物理机上 minor fault/首次触页污染明显 |
| Coherence event | 多线程启动、barrier、锁初始化不代表稳态 |

所以不推荐：

```text
main() 开始
    ↓
直接计数
    ↓
程序结束
```

推荐结构是：

```text
init
    ↓
warm-up，不计数
    ↓
ROI begin: reset/enable counters
    ↓
kernel loop，计数
    ↓
ROI end: dump/disable counters
```

---

## 3. 推荐的 workload 结构

每个 workload 应尽量改造成三段式：

```c
int main() {
    init_data();

    for (int i = 0; i < warmup_iters; i++) {
        kernel_step();
    }

    barrier_all_threads();

    roi_begin();

    for (int i = 0; i < roi_iters; i++) {
        kernel_step();
    }
    roi_end();

    cleanup();
}
```

要求：

- ROI 内尽量不包含 `pthread_create`、大量 `malloc/free`、IO、sleep、频繁 syscall。
- 多线程 workload 在 ROI 前做 barrier，保证所有线程同时进入计数区间。
- 尽量固定线程数、core binding、NUMA 策略和输入规模。
- warm-up 和 ROI 最好使用同一个核心 kernel，避免 warm-up 与 ROI 行为不一致。

---

## 4. gem5 侧采集方法

gem5 侧应只统计 ROI，不统计 init/warm-up/cleanup。

典型流程：

```text
程序启动
    ↓
init
    ↓
warm-up
    ↓
m5_reset_stats
    ↓
ROI
    ↓
m5_dump_stats
    ↓
退出
```

程序内可以使用 gem5 m5ops，例如：

```c
m5_reset_stats(0, 0);
m5_work_begin(0, 0);

run_roi();

m5_work_end(0, 0);
m5_dump_stats(0, 0);
```

或者在 gem5 Python/config 侧监听 work begin/end，在事件触发时执行：

```text
ROI begin -> resetStats()
ROI end   -> dumpStats()
```

### 大 benchmark 的加速路径

对于无法快速到达 ROI 的真实 benchmark，建议使用：

```text
KVM/Atomic/SimpleCPU fast-forward
    ↓
checkpoint
    ↓
restore 到 O3CPU + Ruby
    ↓
detailed warm-up，不计数
    ↓
reset stats
    ↓
detailed ROI
    ↓
dump stats
```

注意：checkpoint 恢复后的 cache/Ruby/coherence 状态不一定等价于真实长期运行后的状态，所以恢复后建议增加一段 **detailed warm-up**，再 reset stats 进入正式 ROI。

---

## 5. 物理机 perf 侧采集方法

物理机侧也必须只统计 ROI，不能直接用全程：

```bash
perf stat ./workload
```

否则会把 init、warm-up、线程创建、page fault 和 cleanup 全部算进去。

### 5.1 首选：程序内 perf_event_open enable/disable

最推荐在 workload 内部使用 `perf_event_open`，在 ROI 边界控制 PMU：

```c
setup_perf_counters();

init_data();
warmup();
barrier_all_threads();

ioctl(fd, PERF_EVENT_IOC_RESET, 0);
ioctl(fd, PERF_EVENT_IOC_ENABLE, 0);

run_roi();

ioctl(fd, PERF_EVENT_IOC_DISABLE, 0);
read_perf_counters();
```

优点：

- ROI 边界最准确。
- 可以和 gem5 的 `m5_reset_stats/m5_dump_stats` 对齐。
- 适合自动批量生成训练样本。
- 可以做 per-thread、per-core 或全局聚合。

注意事项：

- 多线程时要明确选择 per-thread 计数还是 per-core 计数。
- 如果要与 gem5 per-core 统计对齐，物理机线程需要固定到指定 core。
- 需要记录 `time_enabled/time_running`，避免 PMU multiplexing 导致计数缩放误差。

### 5.2 备选：perf stat --control

较新的 `perf stat` 支持 control pipe，可以由外部脚本控制 enable/disable。其思路是：

```text
workload 启动
    ↓
warm-up 完成后通知控制脚本
    ↓
控制脚本 enable perf
    ↓
ROI 结束后通知控制脚本
    ↓
控制脚本 disable perf
```

优点是对 workload 侵入较小；缺点是同步边界精度和自动化复杂度不如程序内 `perf_event_open`。

### 5.3 不优先推荐：按时间戳截断

ROI 前后输出 timestamp，然后对 `perf record` 结果进行截断也是可行的，但更适合采样型分析，不适合严格 PMU count 对齐。

原因：

- `perf stat` 是计数型结果，不天然支持后处理截断。
- timestamp 对齐存在误差。
- 采样 skid、调度噪声、NTP/TSC 差异会影响边界。
- 很难得到严格的窗口事件总数。

因此 timestamp 截断可作为辅助调试手段，不建议作为主采集路径。

---

## 6. ROI/window 长度建议

ROI 不宜按 wall-time 控制，最好按 committed instructions 或固定 iteration 控制。

建议量级：

| ROI 动态指令数 | 适用性 |
|---:|---|
| 1e5 | 太短，只适合 smoke test |
| 1e6 | 可 debug，但 PMU 噪声偏大 |
| 1e7 | 比较实用的最小训练窗口 |
| 5e7–1e8 | 更稳定，接近 TAO 风格采样窗口 |
| >1e8 | 统计更稳，但 gem5 成本显著上升 |

建议初始配置：

```text
每个 ROI: 5M–20M committed instructions
每类 workload: 多个 ROI/window
物理机每个 ROI: 重复 5–20 次，取 median
gem5 每个 ROI: 1 次或少量重复
```

如果某个 PMU 事件在 ROI 内计数太低，应过滤或降权。例如：

```text
event_count < 1000 的样本，不用于该事件训练，或降低 loss 权重
```

---

## 7. 多窗口样本优于单个完整样本

机器学习模型需要大量样本。与其为一个 workload 生成一条完整统计样本，不如生成多个 window 级样本：

```text
window_0: warm-up 后第 0 个 ROI
window_1: warm-up 后第 1 个 ROI
window_2: warm-up 后第 2 个 ROI
...
```

每个 window 生成一条训练样本：

```json
{
  "workload_id": "...",
  "phase_id": "...",
  "nthreads": 4,
  "roi_insts": 10000000,
  "gem5_stats": { "...": "..." },
  "perf_stats": { "...": "..." }
}
```

优点：

- 一个 workload 可以产生多条训练样本。
- 可以覆盖不同 phase。
- 不需要完整 gem5 仿真。
- 可以过滤不稳定 window。
- 可做 leave-one-workload-out / leave-one-phase-out 评估。

---

## 8. Phase selection / SimPoint 风格采样

如果 workload 较复杂，建议先在物理机完整运行或长时间运行，用低成本方式采样性能时间序列：

```text
每 10ms / 100ms / N million instructions 采样：
- IPC
- branch miss rate
- LLC miss rate
- L1D miss rate
- DTLB miss rate
- instructions
- cycles
```

然后聚类得到代表性 phase：

```text
phase A: compute-bound
phase B: LLC-miss-heavy
phase C: branch-heavy
phase D: coherence-heavy
phase E: mixed
```

每个 phase 只选择少量 ROI 进入 gem5 detailed 仿真：

```text
phase A: A1, A2, A3
phase B: B1, B2, B3
...
```

这比随机短片段更稳，也比完整仿真现实。

---

## 9. 训练目标：优先使用 rate/ratio，而不是绝对 count

gem5 与物理机的频率、周期定义、cache 层级和 PMU 事件语义不同。直接用绝对计数训练：

```text
real_event_count = f(gem5_event_count)
```

模型容易被 ROI 长度、指令数和线程数支配。更推荐训练 normalized rates：

```text
X = gem5 stat rates
Y = physical perf PMU rates
```

常见归一化形式：

```text
cycles_per_inst
l1d_miss_per_kinst
llc_miss_per_kinst
branch_miss_per_kbranch
dtlb_miss_per_kinst
loads_per_inst
stores_per_inst
coh_event_per_store
remote_hit_per_mem_access
```

样例映射：

```text
gem5_L1D_miss_per_kinst  -> perf_L1D_miss_per_kinst
gem5_LLC_miss_per_kinst  -> perf_LLC_miss_per_kinst
gem5_branch_miss_rate    -> perf_branch_miss_rate
gem5_cycles_per_inst     -> perf_cycles_per_inst
```

对于无法一一对应的事件，应使用多输入特征建模。例如真实 LLC miss 可能不仅依赖 gem5 LLC miss，还依赖：

```text
gem5 L1/L2/LLC miss
gem5 MSHR occupancy
gem5 coherence events
load/store mix
sharing pattern
prefetch 是否缺失
core/thread 数
```

---

## 10. PMU 语义不等价，需要模型而不是常数比例

很多 gem5 stat 和 perf PMU 并不是简单一一对应。

以 LLC miss 为例，物理机 `LLC-load-misses` 可能受到以下因素影响：

- inclusive / non-inclusive LLC 设计；
- prefetch 行为；
- snoop / HITM / remote hit；
- core PMU 与 uncore PMU 的区别；
- Intel/AMD 具体事件定义；
- 内存层级和目录协议差异。

而 gem5 Ruby 里的 LLC miss 更像仿真器内部 cache/directory lookup 的统计。二者不应假设为：

```text
perf_LLC_miss ~= gem5_LLC_miss * constant
```

更合理的是：

```text
perf_LLC_miss_rate = f(
    gem5_L1_miss_rate,
    gem5_L2_miss_rate,
    gem5_LLC_miss_rate,
    gem5_MSHR_stats,
    gem5_coh_event_stats,
    load_store_mix,
    sharing_pattern,
    nthreads
)
```

---

## 11. 物理机采集环境控制

物理机 perf 数据的噪声可能比模型误差还大，因此必须控制实验环境：

```text
固定 CPU frequency
关闭 turbo
固定 core binding: taskset / pthread_setaffinity_np
固定 NUMA: numactl --cpunodebind/--membind
关闭或控制 SMT sibling
尽量使用 isolated CPU
避免后台任务干扰
预触页，减少 minor page fault
必要时 mlock 内存
记录 context switches / migrations / page faults
记录 time_enabled/time_running
每个样本重复 5–20 次，取 median
过滤 variance/CV 过大的样本
```

多线程 workload 必须固定：

```text
线程数
线程到 core 的绑定
NUMA node
内存分配策略
barrier 进入 ROI
```

---

## 12. 模型训练建议

不要一开始就使用复杂深度模型，先建立简单 baseline：

```text
Linear Regression / Ridge
RandomForest
XGBoost / LightGBM
MLP
```

建议评估方式：

```text
leave-one-workload-out
leave-one-pattern-out
leave-one-thread-count-out
leave-one-phase-out
```

不要只随机切分 window，因为同一 workload 的相邻 window 高度相关，随机切分会导致数据泄漏，使指标虚高。

训练目标可以按事件拆分：

```text
模型 A: cycles/CPI 映射
模型 B: branch miss 映射
模型 C: L1/L2/LLC miss 映射
模型 D: DTLB/iTLB 映射
模型 E: coherence/uncore 事件映射
```

也可以使用 multi-output 模型，但需要对不同事件做尺度归一化和缺失/低计数样本处理。

---

## 13. 推荐的端到端 pipeline

### Step 1: 构造/选择 workload

优先选择：

```text
可重复
可参数化
有明确 ROI
少 syscall
少 IO
少 malloc/free
可控制线程数和 core 绑定
```

覆盖压力面：

```text
compute-bound
branch-heavy
L1/L2 miss
LLC miss
DTLB miss
false sharing
producer-consumer sharing
atomic-heavy
streaming load/store
pointer chasing
```

### Step 2: 三段式执行

```text
init
warm-up
ROI
```

### Step 3: 物理机采集 ROI perf

```text
perf_event_open reset/enable
ROI
perf_event_open disable/read
重复 5–20 次
取 median，并记录 variance
```

### Step 4: gem5 采集 ROI stat

```text
fast-forward/init
warm-up
m5_reset_stats
ROI
m5_dump_stats
```

大 benchmark 使用：

```text
KVM/Atomic fast-forward
checkpoint
restore O3 + Ruby
detailed warm-up
reset
ROI
dump
```

### Step 5: 生成训练样本

每条样本包含：

```text
workload_id
phase_id
nthreads
core_binding
roi_insts
gem5_stats_normalized
perf_stats_normalized
repeat_variance
environment_metadata
```

### Step 6: 训练与验证

```text
先训练 Ridge/RandomForest/LightGBM baseline
再尝试 MLP 或 multi-task 模型
使用 leave-one-workload-out 验证泛化
按事件类型报告 MAPE / SMAPE / R2 / rank correlation
```

---

## 14. 对几种候选方法的判断

| 方法 | 是否推荐 | 说明 |
|---|---|---|
| 完整 workload gem5 仿真 | 不推荐 | 成本过高，phase 混杂 |
| 极短完整负载从 main 开始计数 | 不推荐 | 冷启动污染严重 |
| 长 warm-up + 短 ROI | 强烈推荐 | 成本低，统计更接近稳态 |
| ROI 前后 timestamp 截断 | 备选 | 适合 perf record，不适合精确 perf stat count |
| perf_event_open 控制 ROI | 强烈推荐 | 边界精确，可自动化 |
| KVM/Atomic fast-forward + checkpoint | 推荐 | 适合真实大 benchmark |
| phase clustering 后选 ROI | 推荐 | 提高样本代表性 |
| 直接训练绝对 count | 不推荐 | 容易被 ROI 长度支配 |
| 训练 normalized rate/ratio | 推荐 | 更符合语义映射目标 |

---

## 15. 当前建议的最小可行实验

先不要直接上完整 benchmark。建议做一个最小闭环：

```text
workload 数量: 5–10 个 micro / mini workload
线程数: 1, 2, 4, 8
每个 workload: warm-up + 3–5 个 ROI
每个 ROI: 5M–20M instructions
物理机 repeat: 10 次，取 median
gem5: 每个 ROI 跑一次 O3 + Ruby
模型: Ridge + LightGBM baseline
验证: leave-one-workload-out
```

目标不是一开始覆盖所有 PMU，而是先验证：

```text
gem5 stat rates 是否能稳定映射到 perf PMU rates
不同 workload hold-out 是否有泛化能力
哪些 PMU 事件可映射，哪些事件语义偏差太大
ROI 长度和 warm-up 长度对映射稳定性的影响
```

---

## 16. 最终结论

该任务的关键不是让 gem5 更快地跑完整 workload，而是 **改变样本定义**：

```text
从完整程序级样本
    ↓
变成经过 warm-up 的 ROI/window 级样本
```

推荐最终方案：

```text
1. workload 显式划分 init / warm-up / ROI
2. 物理机用 perf_event_open 在 ROI 边界 reset/enable/disable/read
3. gem5 用 m5_reset_stats / m5_dump_stats 只输出 ROI stats
4. 大 benchmark 用 fast-forward + checkpoint + detailed warm-up + ROI
5. 用物理机低成本采样或 SimPoint 风格方法选择代表性 phase
6. 用 normalized rate/ratio 训练 gem5 stat -> perf PMU 的语义映射模型
```

这样可以同时缓解：

- gem5 完整运行时间过长；
- 极短负载冷启动污染；
- perf 和 gem5 统计区间不一致；
- PMU 语义非一一对应；
- 训练样本数量不足；
- 完整 workload phase 混杂。
