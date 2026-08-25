# FastSim 吞吐瓶颈与无损优化方案（2026-08-25）

## 结论

当前 native-FS 正式入口已切换为
`configs/gem5-fs-native-kernel.cfg`，它固定选择 v28_4 modeled-I-fetch
配置。吞吐主瓶颈不是新加入的 I-cache 路径，而是：

1. producer/`IntervalCoreModel::schedule` 的逐 UOP 状态推进与中间描述符搬运；
2. response timing feedback 的逐 UOP 扫描，以及空 epoch 仍构造完整反馈状态；
3. private/shared weave，特别是高核数下 FR-FCFS 在确定不可重放之前所做的
   哈希、排序和临时对象构造；
4. 正式矩阵用固定 `--jobs` 并发，没有按每个 case 的宿主线程、CPU 和内存压力调度。

优化必须保持目标结果逐项相同。不得用增大 Q、缩放延迟、关闭 TSO/scoreboard、
省略 LRU touch、降低 PMU 精度或按 workload 调参来换吞吐。

## P0 实施状态

2026-08-25 已完成全部 P0，并作为无配置开关的等价实现生效：

| P0 项 | 实现结果 |
|---|---|
| 零进度 epoch | 跳过 batch/preview/feedback/commit 空路径；连续确定为空的固定 Q horizon 合并推进，同时补回原有 step、zero-progress、feedback-call 计数 |
| active-core feedback scratch | `TimingFeedback` 容量跨 epoch 复用；只复制 active core 的 IQ/ROB/LQ/SQ、sequencer、rename 和 sparse 状态；domain worker 使用显式 active-core 映射 |
| 无效逐 UOP 数组 | suffix/resource 功能关闭时不再分配 `rob_head_suffix_uops` 和 `resource_candidate` |
| FR-FCFS fail-fast | 第一次 request 扫描即累计 replayable/atomic 条件；不可重放时在 arrival hash、临时描述符和排序之前保留原计数并返回 |
| producer 热状态 | 当前 ASID 对应的 page-fault state 使用稳定 map-node 指针缓存；distinct/switch 集合只在 ASID 变化时更新；syscall metadata 只在 syscall semantic 实际需要时读取 |
| batch 构建 | k-way merge heap 容量跨 epoch 复用；page-fault-fill 和 regular-store 标志随 merge 累计，只有罕见 preflight trim 后才重扫 retained batch |

验证结果：

- Release 构建、完整 `fastsim_tests`、ASan+UBSan 全部通过；新增 atomic
  FR-FCFS fallback 和连续空 epoch 定向测试；
- 最新 native-FS 40 例为 40/40 passed；CPI/PMU 精度与本文件基线不变；
- 40 例完整 JSON 去除 throughput、wall timer、`frontier_waits` 和
  `max_resident_chunks` 后递归 diff 全部为零。后两项是 producer/consumer
  宿主调度计数，不属于目标机状态；
- 空闲宿主 C16 LBM 的旧二进制为 4.847M user-UOP/s，新二进制两次为
  5.147M/5.162M，即约 +6.2%--+6.5%；C4 LBM 三轮中位数约 -0.8%，落在
  单轮 -2.8%--+3.3% 的噪声/producer-overlap 区间，不能宣称有稳定收益；
- jobs=4 的新 40 例聚合为 7.845M user-UOP/s，相对此前同为最多 4 case
  并发的 7.528M 约 +4.2%。该数字用于矩阵级诊断，不替代固定 affinity、
  `jobs=1` 的配对性能门禁。

## P1.1 实施状态

2026-08-25 已完成 chunk slab/ring，并作为无配置开关的等价实现生效：

- time-epoch 不再把 producer chunk 的 UOP/memory 描述符复制进一个持续增长的
  `CoreChunk`。`ResidentCoreBuffer` 直接持有不可变 producer segment；两个紧凑的
  2 次幂 pointer/index ring 提供 O(1) 逻辑 UOP、memory、`first_memory` 和
  `uop_index` 映射；
- retire 只推进 ring head，并且只在一个 producer segment 被完整消费后释放所有权。
  不再执行描述符全量重编号、前缀 `vector::erase` 或保留后缀搬移；
- 每 core 增加 `CoreChunk` 回收池。producer 复用对象以及 `uops`/`memory` vector
  capacity，consumer 批量归还完整 segment；classic、interval 和 time-epoch 三条路径
  共用相同生命周期；
- 新增跨 chunk 映射、逻辑 rebase、raw descriptor 不变、ring wrap 和完整回收测试。
  测试跨 34 个 chunk，覆盖 ring 扩容后再次绕回；
- 逐项 diff 还暴露出一个既有并行 DRAM 数据竞争：`vector<bool>` 把不同 channel 的
  `write_mode` 压在同一个宿主字内。现已改为每 channel 独立的 `uint8_t`，并增加
  8-channel、每 channel 预置 dirty write 的串/并行逐字段等价测试，连续重复 16 次。

当前二进制的验证结果：

- Release `fastsim_tests`、ASan+UBSan 全部通过；
- 新目录 `tmp/p1-ring-current-full40-20260825` 的正式 native-FS 结果为 40/40
  passed；CPI、branch/L1D/L2/LLC 精度与 P0 完全相同；
- 40 例完整 JSON 去除 wall/throughput、`frontier_waits`、`max_resident_chunks` 和
  明确的 host-only phase timer 后，与 P0 递归比较为 0 个目标态差异。C32 zstd 的
  `dram_frfcfs_passes` 在修复后也稳定回到基线 3610；
- 固定 CPU/NUMA 的 C4 LBM 五轮配对中，P0/P1 中位数为 4.657M/4.809M
  user-UOP/s，即 +3.27%，两组范围分别为 4.626M--4.678M 和
  4.779M--4.825M，没有重叠；固定条件下的一轮 C16 LBM 为
  5.310M -> 5.475M，即 +3.12%；
- `jobs=4` 正式 40 例的聚合 measurement throughput 为
  7.845M -> 7.882M user-UOP/s，即 +0.48%。该共享宿主结果受 C32 进程间 CPU/NUMA
  争用影响，只作为全矩阵周转诊断；固定 affinity 的配对结果才是本优化的性能门禁；
- 40 例 commit/audit 累计时间从 41.952 秒降至 10.665 秒（-74.6%）。新的 C4 LBM
  profile 中不再出现 resident 查找或 descriptor 搬移热点；主要 self cycles 已转为
  timing feedback 23.0%、`IntervalCoreModel::schedule` 17.7%、producer 13.8% 和
  trace decode/wrapper 7.6%。

因此 P1.1 的收益不是被语义修复抵消，而是它只移除了原本约 5%--6% 的串行
commit/container 成本；省下的时间随后受 producer、schedule 和 feedback 主路径以及
高核数宿主争用限制。下一项应进入 P1.2 causal-cone/block transfer，而不是继续微调
ring。

## 基线口径

最新 seed-1 40 例全部通过 replay/source/conservation gate。精度保护基线为：

| 指标 | 当前值 |
|---|---:|
| CPI MAPE / P99 | 6.857% / 13.204% |
| branch-miss MAPE / P99 | 2.630% / 11.609% |
| L1D-miss MAPE / P99 | 2.369% / 8.402% |
| private-L2-miss MAPE / P99 | 3.579% / 18.707% |
| LLC-miss MAPE / P99 | 5.553% / 21.532% |

正式 throughput 是 measurement user-UOP/s；分子不含内核 UOP，但 measurement
wall time 包含处理 native kernel trace 的成本。40 例共 6,000,002,863 user UOP、
797.019 秒 measurement wall，聚合为 7.528M user-UOP/s。等权 P50 为
7.967M，最慢五例为：

| case | measurement user-UOP/s |
|---|---:|
| C4 graph500 | 3.898M |
| C16 lbm | 4.187M |
| C4 zstd | 4.202M |
| C4 lbm | 4.411M |
| C32 lbm | 4.607M |

这些 wall time 来自共享宿主，只适合定位慢例，不适合作为跨配置 A/B。候选收集的
峰值并发只有 4 个 case，而控制组达到 8 个；两批的 wall span 分别为 428.1 秒和
264.3 秒，因此不能把二者差值归因于 modeled I-fetch。

## 阶段与函数证据

40 例 measurement 阶段计时闭合如下。feedback、domain 和 FR-FCFS 是 weave 的
子集，不能与三大阶段重复相加。

| 阶段 | 累计秒数 | measurement 占比 |
|---|---:|---:|
| schedule / batch | 359.088 | 45.1% |
| weave | 386.338 | 48.5% |
| commit / audit | 44.709 | 5.6% |
| timing feedback（weave 子集） | 168.102 | 21.1% |
| domain phase（weave 子集） | 170.769 | 21.4% |
| FR-FCFS 计时区间（weave 子集、下界） | 68.954 | 8.7% |

当前 Release/native/IPO 二进制在空闲宿主上的两个 `perf` profile 均为零 lost
sample：

| self-cycle 热点 | C4 lbm | C4 namd |
|---|---:|---:|
| `compute_core_timing_feedback` | 24.4% | 20.8% |
| `IntervalCoreModel::schedule` | 19.6% | 17.5% |
| `produce_thread_chunk` | 13.3% | 19.4% |
| trace wrapper + binary decode | 8.0% | 8.3% |
| `SetAssociativeCache::access_indexed` + `touch` | 2.3% | 1.4% |

所以 modeled I-fetch 不是主机端主瓶颈。它在 40 例中生成 452,770,644 次 L1I
访问，只相当于全部 measured UOP 的 7.2%；真正进入下层的请求只有 3,582,405 次。
C4 lbm 的三轮空闲宿主顺序 A/B 中，v28.2 control 中位数为 4.476M，默认
modeled-I 中位数为 4.534M user-UOP/s；逐轮相对波动约为 -4.9% 到 +4.4%，没有
可分辨的 modeled-I 吞吐退化。默认别名与直接 v28.4 运行在去除 wall/throughput 和
宿主等待字段后递归 diff 为零。

## 根因分解

### 1. 空 epoch 和全量 feedback 快照

40 例共有 453,540 个 epoch，其中 150,589 个（33.2%）没有接受 UOP。慢 LBM
的空 epoch 比例尤其高：C4 71%、C8 60%、C16 63%、C32 52%。C4 lbm 每个
feedback 调用平均只有 0.35 个 active core。

`compute_timing_feedback()` 在统计 active core 之前创建 `TimingFeedback`，并复制
所有 core 的 IQ/ROB/LQ/SQ、sequencer 和 sparse-ROB 状态。即使 active core 为零，
这些分配和深拷贝仍然发生；active core 为一时也会复制其他 idle core 的状态。当前
`response_sparse_resource_repair=false` 和
`interval_rob_head_suffix_replay=false`，但每个非空 core 仍分别分配
`resource_candidate` 与 `rob_head_suffix_uops` 数组。

40 例 sparse feedback 实际 materialize 3.169B UOP，占 measured user+kernel UOP
的 50.4%；LBM 单例达到 92%--94%。现有 block checkpoint 已避免 91.5% 的 ROB
ring 写回，所以继续只优化 ring 写回收益很小；后续算法收益必须来自减少 materialized
causal cone。

### 2. producer 描述符、容器和重复查询

`schedule + produce_thread_chunk` 在两个 profile 中合计占 32.9%--36.9%
self cycles。每 4096 UOP 新建一个 `CoreChunk`，40 例 measurement 共消费
1,534,922 个 chunk。time-epoch resident buffer 随后再次复制并重编号 UOP/memory
描述符，周期性 `vector::erase` 又会移动保留后缀。

producer 的每条记录还会：

- 对同一 ASID 重复执行 `observed_address_spaces.insert()`；
- 对 `page_fault_state[address_space_id]` 做树查找，即使地址空间没有变化；
- 无条件读取 syscall metadata，尽管只有 syscall 且启用相关语义时才消费它；
- 把约百字节的 AoS `ChunkUopBound` 写入并再次搬运。

这些操作不改变目标模型，却放大了内存带宽和 cache footprint。C4 lbm 的 `perf stat`
显示平均使用 2.42 个宿主 CPU、IPC 2.06，hardware cache-reference miss 比例 44.2%；
瓶颈是并行度有限下的状态扫描和数据搬运，不是算术吞吐不足。

### 3. batch merge 与 preview

主 memory batch 已经从全局 `O(E log E)` sort 改为按 core 有序流的
`O(E log C)` k-way merge，这是正确方向。但每个 epoch 仍重新创建
`std::priority_queue`，每个 memory event 都做通用 heap push/pop。构建 batch 后又有
多次全量扫描，用于 page-fault-fill 探测、preview 计划、materialization 和统计。

40 例 host ns/UOP 与 `epochs/MUOP` 的 Pearson 相关系数为 0.610，与
preview-bypass/UOP 为 0.604，与 memory/UOP 为 0.525。这说明 epoch 固定成本和
低收益 preview 路径比 L1I 密度更能解释尾部吞吐。

### 4. FR-FCFS 的晚回退

40 例有 137,828 个 FR-FCFS candidate epoch，其中 66,043 个（47.9%）回退。
C16/C32 lbm 的回退率分别为 74% 和 90%。不可重放的主要结构条件是 epoch 中出现
atomic 或 canonical replay 产生 dirty DRAM write。

当前实现先为全部 shared event 构造多个 `unordered_map`、生成两个临时数组并做两次
`stable_sort`，之后才检查 `replayable`。而 `dram_frfcfs_wall_ns` 的计时起点还在这些
预处理之后，所以 8.7% 只是已进入 fixed-point 调度部分的下界，没有包含大量晚回退
成本。

### 5. trace 和矩阵调度

FST v7 每条记录为 64 B，reader 使用每 core 4096-record 缓冲。trace decode 栈约占
8% self cycles，属于次级但稳定的带宽成本。

矩阵 runner 只按固定 `--jobs` 启动进程。一个 C32 case 内含 32 个 trace producer、
最多 8 个 domain worker 和 coordinator；8 个 C32 同时运行会对应 256 个 target-core
producer，再叠加 domain worker，超过当前 192 个逻辑 CPU，并加剧双 NUMA socket 的
内存流量。固定 job 数不能表达这种资源差异。

functional warmup 另占 487.786 秒，即 40 例端到端 wall 的 38.0%。measurement
throughput 不含它，但 cases/hour 必须包含。全套 warmup + measurement 共处理
11.182B user+kernel UOP，端到端为 8.704M UOP/s；如果目标是回归周转时间，必须单独
优化或复用 warmup，而不能只看 measurement user-UOP/s。

## 实施顺序

### P0：低风险、目标状态逐项相同（已完成）

1. **零进度 epoch 快路径**：当 `accepted_uops==0` 时，不构造 batch、preview、
   FR-FCFS 和 `TimingFeedback`；直接执行与旧路径相同的 global-time、epoch 和统计
   更新。进一步可证明连续空 epoch 后一次跳转，并按跳过数量补齐确定性计数。
2. **active-core feedback scratch**：先统计 active core，只复制这些 core 的状态；
   持久化并复用 `TimingFeedback` scratch capacity。关闭 resource/suffix 功能时不分配
   对应 per-UOP 数组。
3. **FR-FCFS fail-fast**：在第一次统计 memory request 的扫描中同时计算
   `replayable`/atomic 条件；不可重放时在创建哈希表和排序前返回，并保持所有既有
   candidate/fallback/request counter 相同。
4. **producer 当前-ASID缓存**：仅在 ASID 变化时更新 distinct/switch 集合并查找
   page-fault state；syscall metadata 延迟到实际 syscall 语义需要时读取。行为和异常
   检查保持不变。
5. **复用 k-way heap 和 batch 元数据**：为最多 C 个 stream 使用预留容量的小型 heap，
   在建 batch 时顺带累计 `has_page_fault_fill` 等布尔量，删除后续重复扫描。

这些改动应分别提交和 A/B，不能把预期百分比相加。按当前 phase/Amdahl 上界，若将
全部 timing-feedback 成本减半，40 例总吞吐理论提升约 11.8%；C4 lbm 因 feedback
占 48%，理论上界约 31.6%。P0 的目标是先消除空调用与无效预处理，不声称已经达到该
上界。

### P1：中风险结构优化

1. **chunk slab/ring 与对象回收（已完成）**：producer/consumer 复用 `CoreChunk` 和 vector
   capacity；resident buffer 使用单调 head/ring，避免 append copy、全量重编号和
   `erase` 搬移。
2. **反馈 causal-cone/block transfer**：producer 生成 dependency、IQ/LSQ、dispatch、
   commit 的可组合状态转移；证书成立时 O(1) 应用 block，失败才逐 UOP materialize。
   目标是降低当前 50.4% 的全套 materialized ratio，而不是继续优化已经避免 91.5%
   的 ROB 写回。
3. **FR-FCFS dense descriptor**：用与 `materialized/events` 对齐的 dense ordinal
   取代 `batch_event_key -> unordered_map`；按 channel/bucket 做确定性 merge，保留完全
   相同的 `(arrival, fair_ordinal)` 顺序。
4. **hot/cold SoA 描述符**：把 feedback 必需字段与 audit/罕见字段分开，flags 打包；
   对时间 delta 使用窄表示时必须有溢出检测和 64-bit fallback。
5. **trace mmap/紧凑静态查找**：只读 mmap 或更大的顺序 buffer，静态 PC map 使用排序
   数组加小型 lookup cache。所有边界、sidecar 和异常检查保持不变。

### P2：整套 cases/hour

1. validator 增加 resource-aware 调度：按 target cores、domain workers、实测 CPU 和
   RSS 分配 host slots，largest-first 消除 C32 长尾；不再把 C4 与 C32 都算一个 job。
2. 每个进程绑定 NUMA CPU/memory node，避免跨 socket 数据迁移；绑定只改变宿主调度。
3. 对完全相同 fingerprint 继续复用整例结果；如要跨实验复用 warmup state，checkpoint
   必须覆盖 predictor、O3、cache/directory、DRAM、ASID/DTLB、trace cursor 和全部
   retained frontier，并把 binary、完整 include chain、trace hashes 和所有影响 warmup
   的配置纳入 key。不满足完整状态合同就不得复用。

## 明确不采用

- 改 `sim.interval_max_cycles=1024` 或增大 chunk/Q；
- 减少 branch/cache/DRAM 状态更新，或跳过 L1I hit 的 LRU touch；
- 关闭 TSO、response scoreboard、FR-FCFS 或 native kernel trace；
- 调整目标 latency/penalty 来换 host 速度；
- 盲目增加 worker。LBM C4/C8/C16 每次 feedback 平均 active core 仅
  0.35/0.64/0.91，更多线程主要增加 barrier；
- 用共享宿主上不同并发度的两批结果宣布 1%--5% 优化。

## 一致性和性能验收

每个优化必须满足：

1. `cmake --build build -- -j16`、`./build/fastsim_tests`，并增加与改动对应的
   serial/parallel、empty-epoch、dirty-eviction fallback 定向测试；
2. ASan/UBSan 通过；
3. 同一 binary 的 off/on 运行对 `scope_metrics`、`totals`、每 core/thread、CHA、
   instruction-CHA、cache/DRAM、O3/sequencer、interval step 和 conservation ledger
   做递归逐项 diff；只能排除 wall/throughput、`frontier_waits`、
   `max_resident_chunks` 和明确列出的宿主计时；
4. C4 lbm、C4 graph500、C4 zstd、C16/C32 lbm 至少五轮交错 A/B，再跑正式 40 例；
5. 固定 `--jobs 1`、CPU/NUMA affinity、相同 page-cache 条件，同时报告 median、范围、
   measurement 与 end-to-end throughput；
6. worker 数 1/2/4/8 结果一致，且 CPI/PMU 必须与本文件基线逐项相同，不接受“误差仍在
   容差内”代替 bit-exact。

P0 与 P1.1 已完成。下一步若继续吞吐优化，应做 P1.2
feedback causal-cone/block transfer；当前约一半 UOP 仍进入完整 feedback
materialization，且 profile 的第一热点仍是 `compute_core_timing_feedback`。P1 后续仍须
沿用本节的逐项 diff 门禁。
