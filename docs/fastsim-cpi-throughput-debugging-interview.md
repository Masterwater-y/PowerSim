# FastSim CPI 精度与吞吐瓶颈：排查方法、优化路线和面试案例

> 状态：2026-08-06。本文使用 FS C4 `lbm` 的真实排查过程作为案例。独立 DRAM
> write-queue 仍是实验候选：它显著修复了 `lbm`，但完整六负载 CPI P99 仍未过 10%，
> 因而不能描述成已经切换的 production baseline。

## 1. 一句话结论

性能模拟器有两个相互独立的坐标：

- **CPI 精度**衡量“模拟出来的目标机时间是否正确”；
- **host throughput** 衡量“宿主机多快算出这个结果”。

本案例中，`lbm` 的 CPI 大误差来自 dirty writeback 过早占用 DRAM calendar，属于目标模型的
service-order 错误；吞吐瓶颈则来自 response timing feedback 对大量 UOP 的重复扫描，属于
host 算法成本。前者通过独立 write queue 把 C4 `lbm` CPI error 从 `+19.711%` 收敛到
`+0.827%`；后者通过避免无意义 worker 唤醒把三轮平均吞吐从 4.233M 提升到 4.341M
UOP/s，CPI 和非 host 因果状态 bit-exact 不变。

这也是面试时最重要的开场：**先把模拟精度与执行速度解耦，再分别建立证据链。**

## 2. 指标定义：先保证比较的是同一件事

### 2.1 FS CPI 口径

gem5 FS 的 `numCycles` 会让每个 clocked core 一直计数到公共 ROI 结束。C4 的 FastSim
必须使用相同 label scope：

```text
gem5_uop_cpi    = sum(gem5_per_core_cycles) / gem5_total_uops
fastsim_uop_cpi = 4 * fastsim_global_makespan_cycles / fastsim_retired_uops
cpi_error       = (fastsim_uop_cpi - gem5_uop_cpi) / gem5_uop_cpi
```

不能把 FastSim 各 core 的 active-stream completion time 之和当成主 CPI；core 提前空闲时，
它与 gem5 的 clocked-core label scope 不同。active-stream CPI 只保留为负载不均衡诊断。

误差符号也要保留：

- 正误差：FastSim 预测周期过多，模型偏慢；
- 负误差：FastSim 预测周期不足，模型偏快；
- aggregate 同时报告 signed mean 与 absolute mean/P90/P99/max，不能只看均值相互抵消。

### 2.2 吞吐口径

two-phase FS 包含 functional warmup 和 measurement ROI，需要分开报告：

```text
ROI throughput = ROI retired UOPs / measurement wall seconds
end-to-end throughput = (warmup UOPs + ROI UOPs) / total wall seconds
```

旧字段曾使用 `ROI UOP / (warmup + ROI wall)`，分子不含 warmup 而分母包含 warmup，既不是
ROI 也不是 end-to-end。该口径曾把 zstd 等负载误判为低吞吐。修正后，C4 六案中真正低于
5M gate 的只有 `lbm`。

### 2.3 两个指标为什么必须独立

| 变化 | CPI | host throughput | 解释 |
|---|---|---|---|
| 修正 DRAM service order | 可以显著变化 | 可能不变 | 修改目标机模型 |
| 删除无效 worker barrier | 必须不变 | 应提高 | 只修改 host 实现 |
| 增大 epoch Q | 可能变化 | 通常提高 | 同时改模型与性能，不能当纯优化 |
| 关闭纯诊断计数 | 必须不变 | 应提高 | production/shadow-audit 分层 |

## 3. CPI 误差排查方法

### 3.1 第一步：先排除输入和 label scope 错误

在分析微架构前，先回答以下问题：

1. gem5 与 FastSim 是否使用同一个 ROI、core 数和公共结束点；
2. trace magic/version/header/record size/source core/file length/hash 是否一致；
3. macro instruction 与 UOP 数是否对齐；
4. warmup 是否重放完整前缀，并在公共 barrier 后只重置 measurement counter；
5. MMIO、pseudo-op、跨页且缺少 virtual token 的访问是否有显式例外，而不是静默丢失。

如果 UOP 数或 scope 不同，CPI error 还不是微架构误差，调 latency 只会掩盖数据问题。

### 3.2 第二步：看误差“形状”，不要先调参数

建议同时切四个维度：

- workload：是全局偏差还是单个形状异常；
- core：是所有 core 同向，还是长尾 core 放大；
- 正负号：过估和低估通常是不同机制；
- 强度：memory/UOP、write/read、branch、TLB、共享访问、queue occupancy。

如果只有一个 write-heavy workload 大幅正偏，而五个几乎无 DRAM write 的 workload 不变，
优先检查 write service order；不应该先统一缩放 DRAM latency。

### 3.3 第三步：从计数误差走向时序误差

按以下顺序核对守恒和 PMU：

1. `records = retired_uops`、memory event 生成/消费、private/shared/escape partition；
2. L1/L2/LLC access/miss、DTLB、DRAM read/write、branch miss 的 signed error/WAPE；
3. request arrival、queue wait、service、response、dependency wakeup、SQ release、ordered retire；
4. response-critical cycle attribution是否守恒。

判断规则：

- request count 已接近而 CPI 很差，优先查 queue/service order、MLP/slack 和 critical-path
  exposure；
- request count 本身就错，先修 trace decode、cache state、split access 或统计口径；
- PMU 与 CPI 都低估，才考虑缺失 latency/资源；
- PMU 接近但只有 write-heavy case 高估，统一 latency scale 通常是错误方向。

### 3.4 第四步：建立机制假设，并设计单变量消融

一个可验证的假设必须写清：

```text
触发条件 -> 被改变的状态边 -> 预期影响的 PMU/CPI -> 不应变化的状态
```

例如：

```text
dirty LLC victim 持续产生
-> FastSim 立即修改 DRAM bank/data-bus calendar
-> demand read 被不合理串行化，lbm CPI 正误差放大
-> architectural store response、TSO、SQ release 不应在本实验中改变
```

然后只改变 dirty writeback 的 controller service order，保留 architectural store completion
和 TSO 语义，才能把因果归到这一条边。

### 3.5 第五步：验证修复不是 workload 拟合

候选机制应满足：

- 参数来自目标结构，如 queue size、high/low watermark、minimum burst；
- 在线运行不读取 workload ID、gem5 service order、gem5 row-hit label 或参考 CPI；
- 先跑定向测试，再跑触发负载和不触发负载；
- 对不触发 workload，目标状态应 bit-exact；
- 最后跑完整 workload/core-count gate，而不是只展示被修好的一个 case。

## 4. CPI 实例：C4 `lbm` 的 dirty-write service order

### 4.1 现象

初始 C4 六案中：

| Workload | gem5 UOP CPI | FastSim baseline | error |
|---|---:|---:|---:|
| stockfish | 0.316260 | 0.272138 | -13.951% |
| omnetpp | 0.360471 | 0.312356 | -13.348% |
| zstd | 0.539101 | 0.474304 | -12.019% |
| **lbm** | **2.445125** | **2.927075** | **+19.711%** |
| tealeaf | 0.539485 | 0.489558 | -9.255% |
| graph500 | 0.904989 | 0.819791 | -9.414% |

`lbm` 与其他 workload 的显著差异是持续读写流和高 SQ/LSQ 压力。其 cache/DRAM request
count 已较接近 gem5，所以继续修改 miss count 或统一 memory latency 缺少证据。

### 4.2 根因

旧模型在 LLC dirty eviction 时直接调用 DRAM access，使每个 writeback 立即占用 DRAM
calendar。gem5 controller 有独立 read/write queue，低水位时 demand read 可以绕过 buffered
write，高水位后再按 burst 切换方向。

因此旧模型把“LLC 已接收 dirty victim”错误等价为“该 write 已立即消费 bank/data bus”，
把后台 writeback 与 demand read 过度串行化。`lbm` 会持续激励这个错误，其他五案很少或完全
没有 DRAM write，所以问题表现为 workload-specific 大正误差。

### 4.3 单变量修复

实验候选加入 per-channel 128-entry write buffer、85%/50% watermark 和 16-write burst：

1. dirty victim 先进入 write buffer；
2. 未进入 write turn 时 demand read 绕过 buffered write；
3. 超过 high watermark 且 read quota 满足后切换；
4. 每个 write turn drain 16 个 write，再回到有 pending read 的 read turn；
5. architectural store response、SQ release 和 TSO 顺序保持不变。

结果：

- `lbm` CPI 从 2.927075 降至 2.465344；
- 相对 gem5 的 error 从 `+19.711%` 收敛到 `+0.827%`；
- enqueue/drain/final 为 `276,946 / 276,144 / 802`，满足守恒；
- 其他五案 CPI bit-exact 不变，因为没有触发该 controller 路径。

这说明主因是 service order，不是关闭 TSO、修改 request 数或对 `lbm` 单独缩放 latency。

### 4.4 为什么候选仍不能直接上线

六案 absolute CPI P99 仍为 13.921%，误差尾部已转移到 stockfish/omnetpp/zstd 的独立低估；
同时 FastSim `lbm` write row-hit 比 gem5 更乐观，command/data-bus turnaround 也仍需闭合。
因此生产配置继续 default-off，避免把“修好一个 workload”误报成完整 gate 通过。

## 5. host throughput 排查方法

### 5.1 第一步：选择真正的慢 case

使用 ROI measurement throughput，而不是 mixed-scope legacy 字段。当前完整 C4 中只有
`lbm` 低于 5M：优化前约 4.226M，其他五案均高于 6M。

### 5.2 第二步：先做 wall phase decomposition

优化后的 C4 `lbm` measurement wall 为 22.232 秒：

| Phase | wall seconds | measurement share |
|---|---:|---:|
| schedule/batch | 3.316 | 14.9% |
| weave | 14.355 | 64.6% |
| commit/audit | 4.328 | 19.5% |
| timing feedback（weave 子集） | 9.369 | 42.1% |

三大 phase 合计约 99%，计时闭合，可以继续下钻；如果 phase timer 只覆盖一半 wall time，
应先补计时，不能直接根据函数直觉优化。

### 5.3 第三步：用 profiler 找函数，用事件基数解释函数

`perf` 8K cycle samples、0 lost samples 的 self-cycle 热点为：

| Hotspot | self cycles |
|---|---:|
| `compute_core_timing_feedback` | 32.48% |
| `IntervalCoreModel::schedule` | 13.46% |
| `produce_thread_chunk` | 9.36% |
| `count_inversions` | 4.61% |
| `replay_previewed_memory_event` | 4.47% |
| `cycles_to_fixed` | 3.51% |
| `audit_batch_order` | 2.29% |
| malloc | 1.54% |
| `DramModel::drain_write_burst` | 0.60% |

函数占比只能说明“时间花在哪”，事件基数才能解释“为什么”：

- 96.304M ROI UOP、28.522M memory events；
- 57,965 次 timing-feedback 调用，但只有 38,710 个 non-empty core task；
- 只有 7,680 次调用真的包含两个以上 active core；
- 362,921 个 response seed 最终 materialize 87.223M UOP；
- 79.072M 次 ROB crossing 说明延迟通过有序退休/容量状态广泛传播；
- 纯诊断 same-line inversion 累积到 33.516M pairs。

因此结论不是“DRAM queue 慢”，而是：**反馈传播的 host 算法接近全 UOP 扫描，同时任务粒度
和活跃 core 数不足以摊薄 barrier。**

### 5.4 第四步：先做 target-state-exact 的 host 优化

原实现只要 C4 开启 parallel feedback，就为 zero/one-active-core epoch 唤醒完整 worker pool。
优化后：

```cpp
if (config.interval_parallel_feedback && active_tasks > 1) {
    run_parallel_timing_feedback(...);
} else {
    compute_on_coordinator(...);
}
```

三轮隔离 `lbm`：

```text
before: 4.226 / 4.240 M UOP/s，mean 4.233
after : 4.330 / 4.363 / 4.332 M UOP/s，mean 4.341
gain  : +2.56%
```

CPI、totals/cores/threads/CHA 以及除 host 计时/调用计数外的 causal-frontier state 全部
bit-exact。强制全串行反而降低约 3.99%，说明“去掉并行”不是答案；只应在并行工作量不足时
避免 barrier。

### 5.5 第五步：用 A/B 和 target-state diff 验证

吞吐实验至少包含：

1. 相同 binary/config/trace、隔离单变量；
2. 慢 case 多轮运行，使用 mean/median 和波动范围；
3. 比较 CPI 与 `totals/cores/threads/cha`；
4. 比较 non-host causal state，允许 wall timer 和 host-call counter 不同；
5. 跑完整 workload suite，确认没有把成本转移到其他形状；
6. Release、ASan/UBSan 和定向等价性测试。

单次跨进程 wall time 会受 page cache、CPU 调度和频率影响。只看一次 `+3%/-3%` 不能宣布
显著优化或退化。

## 6. 还有哪些点可能大幅提高吞吐

当前 `lbm` 为 4.332M 左右。达到 5M 需要 1.154× 总体加速，即把 22.23 秒降到约
19.26 秒，减少 2.97 秒或 13.3% wall time。下面把“单 case engine throughput”和“整套
cases/hour”分开讨论。

### 6.1 P0：block-summary / incremental timing feedback

第一阶段已经实现为 lazy ROB exit checkpoint，但完整 block transfer 仍是后续方向。

当前 producer 已算出 baseline rename/dispatch/issue/completion/retire，response feedback 又按
UOP 重放 IQ/ROB/LSQ/dependency/ordered-retire 状态。仅 362.9K response seeds 却 materialize
87.2M UOP，说明简单的“没有 seed 就跳过”证书不够；79.1M ROB crossing 会让少量 seed 通过
容量与有序退休传播很远。

可行设计是把固定大小 UOP block（如 32/64 UOP）表示为状态转移摘要：

1. producer 在 baseline schedule 时生成 block 的 dispatch/commit bandwidth、ROB/IQ/LSQ
   exit-state 摘要和 dependency boundary；
2. feedback 从 response seed 开始传播；
3. 若 block-entry delayed-state 满足证书，O(1) 应用预计算 transfer；
4. 证书失败才逐 UOP materialize，并生成新的 exit state；
5. ROB ring 使用 lazy range/update 或 block checkpoint，避免为了推进 cursor 写每个 entry；
6. 任意不确定情况回退当前完整路径，保证 target-state exact。

timing feedback 占 42.1% wall。按 Amdahl 估算：

| feedback path speedup | total speedup（理论） | 预计吞吐 |
|---:|---:|---:|
| 1.5× | 1.163× | 约 5.04M UOP/s |
| 2.0× | 1.267× | 约 5.49M UOP/s |

这是基于 phase wall 的方向性估算，最终仍需实测。验收重点不是 seed 数，而是
`materialized_uops / retired_uops` 能否从当前 90.6% 显著下降，且所有 non-host state exact。

当前实现新增 `core.response_block_summary`：

- checkpoint 入口的 sequence-tagged ROB ring 保持只读；
- 区间内只保留 completion/retire delta；
- checkpoint 退出时只写回最后一个 ROB window；
- `false` 保留原逐 UOP ring write，作为同一 binary 的 equivalence reference。

C4 `lbm` 覆盖 38,710 个 non-empty checkpoint、96.304M UOP，只写回 7.302M ROB entry，
避免 89.002M 次写入（92.4%）。三轮隔离 A/B 中，ROB-sized delta 版本的 off/on 均值分别为
5.745M/5.770M UOP/s，单项收益 `+0.43%`。

该阶段在单测、真实 `lbm` 和 C4 六案上 target state exact，但没有降低 90.6% 的
`materialized_uops`，因此不能宣称已完成 1.5× feedback path。下一阶段仍需 producer
生成 dependency/IQ/LSQ/dispatch/commit transfer summary，并只 materialize 违反入口证书的
causal cone。

后续对真实 `lbm` ROI 做了 32-UOP block 活跃度量化：

- 3.028M 个 block 中只有 8.67% 完全 response-inactive；
- 91.27% retire-active，89.65% completion-active；
- uniform dispatch/completion/retire block 仅 8.82%；
- completion/retire delay 平均分别有 12.09/8.16 个 run，`<=4` run 覆盖仅
  14.31%/22.09%；
- 32-UOP memory-free block 只有约 0.03%--0.06%。

因此不能用“无 seed block”或统一平移冒充完整 block transfer；它们覆盖率不足，且会把
certificate 检查成本留在 90% 以上的 fallback block 上。真正的 Phase 2 仍需
memory-aware max-plus/dependency transfer 或 causal graph，而不是继续堆低命中快路径。

2026-08-25 的最新实现先取了一个可严格证明等价的资源日历切片：IQ capacity 的状态
转移总是“删除当前最小 release，再插入一个不早于它的新 release”，因此可把逐 UOP 的
二叉堆向下调整换成 monotone radix calendar。C4 LBM 三轮固定绑核
measurement throughput 中位数由 4.771M 提升至 4.938M user-UOP/s（+3.51%），
feedback 子阶段约 +6.69%；cycles、CPI、PMU 及非宿主状态逐项相同。它不减少
materialized UOP，故只是完整 block transfer 的基础设施和局部加速，不能宣称已获得
O(1) block 级重放。

### 6.2 P0：把 same-line inversion audit 移出 production hot path

`interval_full_order_audit=false` 仍保留 exact same-line conflict audit：每个 batch 构建
hash group、排序，再用 Fenwick tree 计 inversion。`count_inversions + audit_batch_order`
合计约 6.90% self cycles，但只更新诊断计数，不参与 CPI 或状态转移。

现已新增独立的 `sim.interval_same_line_order_audit` 开关：

- production 默认关闭；
- CI/accuracy audit 或采样 epoch 开启；
- shadow run 定期核对完整计数；
- 关闭前后 target state 必须 bit-exact，只允许诊断 counter 不同。

真实三轮隔离 `lbm` A/B（block summary 固定开启）为：

```text
audit on : 4.715 / 4.606 / 4.605 M UOP/s，mean 4.642
audit off: 5.719 / 5.749 / 5.617 M UOP/s，mean 5.695
gain     : +22.69%
```

收益高于 perf self-cycle 的 6.9% 粗估，因为该 audit 位于串行 commit critical path，
`commit/audit` wall 从约 3.887 秒降到 0.396 秒。开关前后除诊断 counter、配置和 host
timer 外全状态 bit-exact。该低风险优化已经单项跨过 5M gate。

### 6.3 P1：producer/feedback 融合与 compact SoA descriptor

`IntervalCoreModel::schedule + produce_thread_chunk` 合计 22.82% self cycles。当前 producer
生成 baseline timing 后，feedback 仍重复做 fixed-point conversion、load/store 分类、部分
dependency 和 queue 输入整理。

可做的 exact 优化：

- 在 `ChunkUopBound` 直接保存 feedback 所需的整数 cycle 和 compact flag；
- 直接使用已生成的 `memory_read/memory_write`，避免反馈阶段再次扫描 memory events；
- 把 producer 输出改为 SoA，减少 feedback 顺序扫描时的 cache footprint；
- 为 32/64-UOP block 同时生成第 6.1 节的 transfer summary；
- chunk 与 scratch arena 循环复用，降低 malloc 和 vector initialization。

该路径单独做到 1.5×，按 cycle sample 粗估只有约 8% 总加速；它更适合作为 block-summary
方案的一部分，而不是孤立重写。

已完成其中第一项可验证融合：`core.response_memory_descriptor=true` 让 producer 在真实
in-range memory event 物化时记录 dispatch load/store class，feedback 不再逐 UOP 重扫
memory events。descriptor 与 resource-port flag 分离，atomic 仍只进入 store queue，MMIO
escape 不会被错误计入。三类 workload 的同 binary A/B 为：

| Workload | reference | descriptor | 总吞吐收益 | feedback 子项 |
|---|---:|---:|---:|---:|
| `lbm` | 5.671M | 5.751M | +1.40% | +2.47% |
| `stockfish` | 11.357M | 11.701M | +3.03% | +2.74% |
| `zstd` | 9.286M | 9.497M | +2.27% | +6.07% |

C4 六案递归 target-state diff 为零，正式最低 ROI throughput 更新为 5.832M UOP/s。

第二项可验证融合：`core.response_batch_timing_encode=true` 把 feedback 里逐 UOP 的
Q16 定点 encode 移到 producer 物化 block 时批量完成，consumer 侧复用已编码结果。这是
本 session 中第一个**在全部六个 workload 上都不回退**的微优化（此前 heap 变体、layout/scratch
改写、bulk append 都会牺牲至少一个 workload）。同 binary 双次配对 A/B（measurement UOP/s）：

| Workload | reference | batch-encode | 收益 |
|---|---:|---:|---:|
| `lbm` | 5.515M | 5.689M | +3.14% |
| `stockfish` | 11.603M | 11.730M | +1.09% |
| `zstd` | 8.112M | 8.396M | +3.49% |
| `omnetpp` | 8.766M | 9.055M | +3.29% |
| `tealeaf` | 9.910M | 9.997M | +0.88% |
| `graph500` | 8.298M | 8.460M | +1.95% |
| **等权均值** | | | **+2.31%** |

六案递归 target-state diff：除 `frontier_waits`（`simulator.cpp:3257` 的宿主
producer/consumer 队列等待计数，doc 已列为非确定性、不入 hash）外全部为零；且 off↔off
两次运行的 `frontier_waits` 本身就抖动（如 zstd 23↔22、lbm 20↔25），证明它是宿主线程唤醒
次序噪声而非 flag 引起。ASan/UBSan 下 `test_parallel_feedback_equivalence` 的
batch-encode 分支逐字段校验（cycles/retired/memory/O3/sequencer/interval_steps/
materialized_uops）通过。两项融合叠加后生产配置默认开启。

### 6.4 P1：按独立状态组件并行 shared replay

扣除 timing feedback 后，weave 仍约 4.99 秒，占 measurement wall 的 22.4%。可探索把同一
epoch 的 shared events 按 LLC slice/directory component/DRAM channel 分片，在证明组件间无
coherence、eviction、command/data-bus 交叉状态后并行 replay，最后按确定顺序合并 counter。

关键约束：地址落在不同 DRAM channel 不代表一定独立；directory sharer、LLC eviction 和
NoC/共享 bus 可能把组件重新连接。必须用 state-component certificate，失败即回退串行。
即使该剩余 weave 路径整体 2×，理论总加速约 1.126×，预计约 4.88M，通常需要与 audit
拆分或 feedback 优化组合。

### 6.5 P2：scratch/state 复用和关闭功能的零成本化

这是必要的清理，但不是“大幅”杠杆：

- `TimingFeedback` 每次调用会 resize/copy 多组 per-core vector；
- `response_sparse_resource_repair=false` 时仍构造 `resource_candidate`；
- `interval_rob_head_suffix_replay=false` 时仍初始化 suffix bitmap；
- malloc 本身约 1.54% self cycles。

建议持久化 per-core scratch、按 active core 初始化，并让 disabled feature 不分配任何 per-UOP
数组。预期更接近 1%–3%，适合与 P0/P1 一起实施，不应包装成 15% 优化。

### 6.6 如果目标是 cases/hour：优先做进程级并行

不同 workload/case 没有目标状态依赖，整套 regression 的总吞吐可以用进程级并行提升，直到
host core、内存带宽或 page cache 饱和。这可能接近线性提高 cases/hour，但不会提高单个
`lbm` 的 4.33M UOP/s，也不能帮助 per-case 5M gate。面试时要先问清“吞吐量”指单模拟器
UOP/s，还是整个平台每天完成多少实验。

### 6.7 不推荐作为第一选择的方向

- **直接增大 epoch Q**：能减少 barrier，但 Q=1024 是当前 accuracy contract，修改会改变
  CPI 近似，不能称为 host-only optimization；
- **继续增加 worker**：平均每次 feedback 不到一个 active core，更多线程没有可并行任务；
- **统一缩放 latency**：会混淆 CPI 修复与 throughput 优化，并可能过拟合 workload；
- **只调编译参数**：当前已经是 Release `-O3`、native 和 LTO/IPO 路径，热点是算法扫描；
- **只优化 DRAM write queue**：其 drain 只有约 0.60% self cycles，不是 host 主瓶颈；
- **关闭 TSO/scoreboard 换速度**：会改变目标语义，除非明确作为精度消融，不能算无损优化。

## 7. 推荐实施顺序和验收门槛

| 顺序 | 工作 | 结果/预期 | 风险 | 验收 |
|---|---|---:|---:|---|
| 1 | production/shadow audit 分层 | 已实现，`lbm` +22.69% | 低 | 六案仅 audit counter 可变 |
| 2 | disabled-feature 零分配、scratch 复用 | 约 1%–3% | 低 | 全 target state exact |
| 3 | lazy ROB block checkpoint | 已实现，`lbm` +0.43% | 中 | 六案全状态 exact |
| 4 | 完整 block transfer / causal cone | 约 15%–27% | 高 | materialized ratio 下降、反馈 ≥1.5× |
| 5 | producer descriptor 与 block summary 融合 | 约 5%–10% 组合收益 | 中 | CPU/cache footprint 下降 |
| 6 | component-sharded shared replay | 约 5%–12% | 高 | certificate fallback + exact merge |

每一步都按同一 gate：

1. 定向单元测试与 parallel/serial equivalence；
2. Release、ASan/UBSan；
3. `lbm` 至少三轮 A/B；
4. FS C4 六案 target-state diff；
5. 再跑完整 core-count/workload suite；
6. 单 case 最低 ROI 和 end-to-end throughput 均 ≥5M；
7. CPI P99 ≤10%，所有 UOP/memory/partition/response ledger 守恒。

## 8. 面试表达模板

### 8.1 90 秒版本

> 我先把问题拆成模拟精度和宿主机吞吐两条线。精度上先对齐 FS 的公共结束点、trace、
> UOP 数和 warmup，再按 workload、core、PMU 和因果 ledger 找误差形状。`lbm` 是唯一持续
> 激励 DRAM write queue 的负载，request count 已接近 gem5，但 CPI 高估 19.7%。单变量实验
> 证明 FastSim 把 dirty eviction 立即写入 DRAM calendar，错误阻塞 demand read；加入独立
> write buffer 后误差降到 0.83%，其余五案 bit-exact。
>
> 性能上我先修正 ROI throughput 口径，再用 phase timer 和 perf 定位。42% wall time 在
> timing feedback，57,965 次调用只有 38,710 个有效 core task，所以先让 zero/one-task 在
> coordinator 执行，三轮均值提升 2.56%。随后把只更新 33.5M inversion 诊断计数的
> same-line audit 移出 production critical path，`lbm` 三轮均值从 4.642M 提升到 5.695M；
> 再用 lazy ROB exit checkpoint 避免 89.0M 次 ring 写入。完整六案最低吞吐达到 5.667M，
> CPI、PMU 和所有 non-host causal state 保持 bit-exact。

### 8.2 STAR 版本

**Situation**：FS C4 `lbm` CPI error `+19.711%`，ROI throughput 约 4.23M，且原吞吐字段
混入 warmup，无法判断真正瓶颈。

**Task**：在不读取 gem5 在线标签、不按 workload 调参的前提下定位 CPI 根因；同时提高
host throughput，并证明性能优化不改变模拟结果。

**Action**：

1. 对齐 trace/hash/ROI/UOP 和 FS common makespan；
2. 用 PMU 与 workload shape 排除 request-count 和统一 latency 假设；
3. 用 controller ledger 和 queue-off/on 单变量实验定位 dirty-write service order；
4. 分离 measurement/end-to-end throughput，增加 schedule/weave/commit/feedback timer；
5. 用 perf 加事件基数发现 worker barrier 与 active-task cardinality 不匹配；
6. 实施 active-task-aware feedback、production/shadow audit 分层和 lazy ROB block
   checkpoint，并做同 binary A/B、递归 state diff 和完整六案回归。

**Result**：`lbm` CPI error 收敛到 `+0.827%`；same-line audit 拆分三轮平均
`+22.69%`，lazy ROB checkpoint 单项 `+0.43%`；C4 六案最低 ROI throughput 从 4.334M
提升到 5.667M，递归 target-state diff 为零。完整 CPI P99 仍为 13.921%，所以 write-queue
候选仍保持 default-off；下一步是降低 materialized ratio 的完整 block transfer，不把
lazy ROB 写回优化冒充 1.5× feedback 算法。

### 8.3 常见追问

**为什么不直接加线程？**

因为反馈调用平均不到一个 active core，更多 worker 没有任务，反而增加 barrier；强制串行
又慢 3.99%。正确做法是按任务基数选择执行方式，并优化单 core 的增量算法。

**为什么不增大 epoch？**

epoch Q 同时决定近似边界和 host 任务粒度。Q=1024 是当前精度合同，改 Q 后 CPI 可能变化，
必须作为模型实验重新过 accuracy gate，不能当成无损性能优化。

**如何证明没有用性能换精度？**

不仅比较最终 CPI，还比较 totals、per-core/thread、CHA、cache/DRAM 计数和 non-host causal
frontier。性能优化只允许 wall timer、worker-call 和纯 host 诊断计数变化。

**如何证明不是为 `lbm` 过拟合？**

机制参数来自目标 controller，在线不读取 workload ID 或 gem5 service order；不触发路径的
workload bit-exact；最后仍以完整 P99 和多 core suite 决定是否上线。

**为什么修好 `lbm` 仍不打开候选？**

因为 P99 gate 看完整分布。`lbm` 收敛后，尾部转移到其他 workload 的负误差，且 write
row-hit/turnaround 仍有未闭合语义。工程上应报告局部机制修复，同时保持 production gate。

## 9. 证据与代码位置

- 完整 FS C4 优化后报告：`tmp/fs-c4-throughput-production-v1/summary.md`
- `lbm` 三轮中的最终报告：`tmp/fs-c4-lbm-feedback-hybrid-final-v1/summary.md`
- active-task-aware feedback：`src/simulator.cpp` 的 `compute_timing_feedback()`
- same-line audit 分层：`src/simulator.cpp` 的 `audit_batch_order()`
- lazy ROB block checkpoint：`src/simulator.cpp` 的 `compute_core_timing_feedback()`
- dirty write queue：`src/simulator.cpp` 的 `DramModel::enqueue_write()` /
  `drain_write_burst()`
- FS CPI scope 与 gate：`tools/validate_fs_c8.py` 的 `analyze_case()` / `summarize()`
- 吞吐统计口径：`src/main.cpp` 的 `stats_json()`
- 完整实验日志：`docs/gem5-source-aligned-p99-plan.md` 第 25 节
