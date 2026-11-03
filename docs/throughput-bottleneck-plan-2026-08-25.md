# FastSim 吞吐瓶颈与无损优化方案（2026-08-25）

## 结论

当前 native-FS 正式入口为 `configs/gem5-fs-native-kernel.cfg`，它固定选择已通过
正式门禁的 v28_6 materialized-UOP kernel。v28.7 preview-bypass 保留为实验 overlay，
但因 C32 单轮 10-workload 门禁收益接近零且有两个明显回退，不作为全局默认。吞吐
主瓶颈不是新加入的 I-cache 路径，而是：

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

## P1.2 首个精确切片：响应 IQ 单调时间轮

2026-08-25 已完成 P1.2 的第一个资源日历切片，并由 v28_5 默认开启。它不是完整的
causal-cone/block transfer，也不会删除或合并模拟 UOP；它只减少逐 UOP feedback 中
维护 IQ capacity 的宿主工作：

- 原路径每接受一个 UOP，都会从 IQ release-cycle 二叉最小堆取根并执行通用向下
  调整。profile 中这一 `replace_min` 代码区曾占 C4 LBM 约 6.1% self cycles；
- IQ 状态转移严格满足“删除当前最小值，并插入一个不早于当前最小值的 release”。
  v28_5 因而使用固定容量 monotone radix calendar，使每次更新按 64 个 value-range
  bucket 摊销 O(1)，checkpoint 边界仍导入/导出原有二叉堆 multiset；
- 只有 `32 <= iq_entries <= 256` 才启用 radix 路径。更小或超出固定容量的 IQ 自动走
  原二叉堆，避免小队列常数开销并保持所有合法配置可运行；
- 新增 `response_iq_radix_checkpoints/updates` 审计计数。C4 LBM 每轮 16,296 个
  checkpoint、40,660,551 次更新，更新数与 `interval_accepted_uops` 完全相等；
- 尝试过的 block-inactive certificate 因真实 LBM 覆盖率太低、sequencer radix 因只有
  16 个槽而变慢，均已完整移除，没有留下默认关闭的死分支。

固定 CPU/NUMA、三轮交错的 C4 LBM A/B 中，measurement user-UOP/s 中位数从
4.771M 提升到 4.938M（+3.51%），外部整进程 wall 中位数从 8.67 秒降到 8.38 秒
（+3.46%），feedback wall 中位数从 4.322 秒降到 4.051 秒（子阶段 +6.69%）。
交叉门禁也全部为正：C4 zstd +6.70%、C4 graph500 +3.91%、C16 LBM +2.67%。
四组运行的 cycles、CPI、PMU、core/thread、CHA、cache/DRAM 和全部非宿主 causal
state 递归 diff 均为零；新 profile 中 `replace_min` 已跌出 0.5% 热点列表。

正式目录 `tmp/p12-monotone-iq-full40-20260825` 为 40/40 passed。CPI 与四项 PMU
误差逐例、聚合统计均与 v28_4 基线完全相同；剔除 wall/throughput、宿主调度字段、
配置开关和两个新审计计数后，40 份完整 JSON 递归 diff 为 0。全矩阵共接受
6,285,757,997 个 UOP，其中 6,285,753,411 个经过 radix 更新，剩余 4,586 个由既有
response-inactive 证书精确跳过，二者之和严格等于 accepted UOP。共享宿主 jobs=4 的
聚合 measurement throughput 从 7.882M 提升至 8.004M user-UOP/s（+1.54%）；该结果
仍只作矩阵周转诊断，固定绑核配对是单引擎性能结论。

这项优化并没有改变约 92% 的 C4 LBM sparse materialized-UOP 比例。要取得数倍提升，
下一步仍需完整的 dependency/IQ/LSQ/dispatch/retire 可组合 transfer，只对证书失败的
causal cone 逐 UOP 展开；不能把本次资源日历优化表述成“减少了模拟 UOP 数”。

### 64→32→16 UOP 精确块传递原型（2026-08-26）

已实现第一版 sequence-aligned 分层块传递，并以
`core.response_causal_block_transfer` 作为显式实验开关。它只接受可以证明 response
反馈没有移动 dispatch、completion 和 ordered-retire 下界的块；L1 请求的 Sequencer、
IQ、LQ/SQ、TSO store 和 lazy ROB 状态仍在私有候选状态中精确推进。任一 serialize、
依赖、容量、带暴露延迟的 I-fetch/data response 或 commit-width 检查失败时，候选状态
不提交，立即回到原标量路径。块宽 64 是首选宿主粒度，32/16 只负责对齐尾部和局部
失败；它不是新增的目标微架构参数。

实现包含三项防误用约束：配置必须同时开启 sparse scoreboard、ROB block summary、
memory descriptor 和 monotone IQ；候选状态到整块通过后才事务式提交；JSON 输出分别
记录 64/32/16 的 candidate、transfer 和 transferred-UOP 数。定向回归覆盖了完整
64-UOP 命中，以及在第 63 个 UOP 注入 serialize 后的深度回退；Release 测试和
ASan/UBSan 测试均通过。

固定 `CPU 0-15 / NUMA node 0` 的 C4 LBM 10M/core 三轮交错 A/B 结论是否定的：

| 指标（中位数） | v28_5 标量 | 开启块传递 | 变化 |
|---|---:|---:|---:|
| measurement wall | 8.249 s | 8.359 s | +1.34% |
| measurement user-UOP/s | 4.849M | 4.785M | -1.32% |
| feedback wall | 4.213 s | 4.344 s | +3.11% |
| 外部整进程 wall | 8.53 s | 8.64 s | +1.29% |

三轮 `totals/cores/threads/CHA/instruction-CHA` 递归 diff 都为 0，因此这是纯宿主负收益，
不是精度变化。40,660,551 个 accepted UOP 中只有 543,472 个通过传递（1.34%）：
64/32/16-UOP transfer 分别为 473/2,015/28,045 次。根因是 C4 LBM 的
37,601,490 个 materialized UOP（92.48%）大多位于 I-fetch response、ROB、LSQ 或
ordered-retire 延迟波内；“只跳过完全不活动块”的证书覆盖率不足以摊销状态复制和
检查。

因此该原型开关保持默认关闭；在该实验时点，`gem5-fs-native-kernel.cfg` 继续指向
v28_5，也没有为这条负收益块路径创建生产 overlay。下一版若继续，必须传递活动 delay frontier
（例如可组合 max-plus/affine block summary），而不是放宽证书或近似跳过 UOP；只有
覆盖率和固定绑核性能同时转正后才进入全 40 例门禁。

### P0：隔离块探测、memory-event feedback 与 scratch 复用（2026-08-26）

默认关闭的块传递不再在逐 UOP 循环中读取配置并做对齐判断。现在每个 active core
checkpoint 只分派一次模板实例；关闭实例通过 `if constexpr` 完全删除块探测，开启
实例仍保留原有的逐块证书与回退语义。

`issue_extra_q16` 的持久结果从 accepted-UOP 索引改为 accepted-memory-event 索引。
它没有改成“每个 event 独立算延迟”：一个 UOP 的最终 issue displacement 仍复制给
该 UOP 的全部 I-fetch/data events，因此 corrected order、suffix carry、TSO store send
和 causal fixed point 的输入与旧实现相同。C4 LBM 中 materialized feedback 槽从
40,660,551 降为 12,454,167，减少 69.37%；accepted memory 起点随 feedback 缓存，
消费端不再为每个 event 重查 resident UOP 索引。

completion、store-set、lazy-ROB retire 和两级 MSHR calendar 改为跨 checkpoint 复用
容量。completion/store/ROB 槽按程序序完全覆盖后再读取，故无需预清零；MSHR calendar
仍在每轮可能成为容量瓶颈时显式清零。scratch 按 core 聚合并以 64B 对齐，避免多个
domain worker 同时修改相邻 `std::vector` 元数据产生 false sharing。第一版按字段组织
的 nested-vector scratch 曾抵消收益，已被这一布局替换。

固定 `CPU 0-15 / NUMA node 0` 的 C4 LBM 10M/core 三轮交错 A/B：

| 指标（中位数） | P0 前 | P0 后 | 变化 |
|---|---:|---:|---:|
| measurement wall | 8.209 s | 8.165 s | -0.53% |
| measurement user-UOP/s | 4.873M | 4.899M | +0.54% |
| timing-feedback wall | 4.169 s | 4.075 s | -2.24% |
| interval-weave wall | 5.954 s | 5.879 s | -1.26% |
| 外部整进程 wall | 8.49 s | 8.44 s | -0.59% |

三轮去除 wall/throughput 与 worker-wait 后的完整 JSON 递归 diff 均为 0；Release 与
ASan/UBSan 全套测试通过。该 P0 是确定的小幅正收益，主要压缩 feedback 子阶段；它
没有减少必须执行的 sparse dependency/IQ/LSQ/retire UOP，因此不能外推成倍吞吐量。

### P1.2b：materialized-UOP 常用配置精确内核（2026-08-26）

在实现 active retire-wave transfer 前，先对 C4 LBM 的 37,601,490 个 materialized
UOP 做了一次临时 stage-mask 诊断。结果否定了“先只压缩 ordered-retire”的假设：

- completion 被移动 36,403,622 个，dispatch 被移动 36,191,562 个，retire 被移动
  37,500,952 个；这些集合允许重叠；
- 36,158,064 个（占 materialized 的 96.16%）同时移动 dispatch、completion 和
  retire；纯 retire-only 只有 1,182,062 个（3.14%）；
- ROB crossing 达 33,774,543 次。主波是 retire/ROB 容量反压继续移动 dispatch，
  再穿过 issue slack 进入 completion，而不是一条可以独立闭式计算的 commit 尾波。

因此没有把低覆盖率 retire-only block 加入热路径，也没有保留上述逐 UOP 临时诊断
计数。第一步改为给生产中已经验证的 sparse feature 组合生成专用模板实例：

- 每个 active-core checkpoint 只分派一次
  `compute_core_timing_feedback_impl<false, true>`。在该实例内，sparse scoreboard、ROB
  block summary、memory descriptor、monotone IQ、TSO 和 retire exposure=1 成为编译期
  常量；关闭的 attribution、fetch queue、rename、resource repair、legacy ROB/LSQ 和
  suffix replay 分支被编译器完整删除；
- dispatch、dependency、Sequencer/MSHR、completion、ordered retire、ROB/LQ/SQ 和
  store drain 仍按原程序序逐 UOP/逐 event 推进。该切片减少的是每个 materialized UOP
  的宿主控制流，不是近似减少目标 UOP 或 cache 请求；
- 通用标量实例仍是等价参考。配置键
  `core.response_materialized_uop_fast_kernel` 只有在上述完整 feature contract 成立时才
  允许开启，不兼容配置 fail closed；JSON 记录 fast-kernel checkpoint/UOP 数；
- 新增定向测试让 generic 与 specialized 实例处理同一组 Sequencer、memory、ROB 和
  ordered-retire 状态，逐项比较 cycles、PMU、O3、Sequencer 和 sparse 计数。

移除临时诊断开销后的固定 `CPU 0-15 / NUMA node 0` C4 LBM 三轮交错 A/B：

| 指标（中位数） | 通用标量 | 专用内核 | 变化 |
|---|---:|---:|---:|
| measurement wall | 8.160 s | 7.842 s | -3.90% |
| measurement user-UOP/s | 4.902M | 5.101M | +4.06% |
| timing-feedback wall | 4.091 s | 3.679 s | -10.08% |
| interval-weave wall | 5.906 s | 5.479 s | -7.23% |

三轮范围分别为 4.872M--4.910M 与 5.095M--5.114M，没有重叠。交叉 workload/核数的
固定绑核单配对也全部为正：C4 zstd +2.47%、C4 graph500 +1.43%、C16 LBM +3.63%、
C32 LBM +3.04%；四组 timing-feedback 均下降约 9.9%--10.7%。每组完整 JSON 去除
host timer、配置选择键和新增审计计数后递归 diff 为零。

正式 40 例随后直接重放 v28_5 冻结的 40 条 native user+kernel 命令，结果 40/40
target-state exact。专用内核处理 6,285,753,411 个 UOP，既有 inactive certificate
处理 4,586 个，二者之和严格等于 6,285,757,997 个 accepted UOP。Release、完整
`fastsim_tests` 和 ASan+UBSan 均通过。因此新增版本化 overlay
`gem5-v28_6-fs-materialized-kernel.cfg`，维护入口默认选择 v28_6；通用配置默认值仍为
false。

该切片尚未降低 92.48% 的 materialized 比例，也不是最终的 active causal block
transfer。下一步若追求更大收益，仍需把 ROB-lag/dispatch/completion/retire 的 max-plus
frontier 与 memory/store 边界组合成可证 block summary；本次约 10% feedback 降幅是其
无损、低风险的编译期前置优化。

#### C32 固定绑核配对复验（2026-08-26）

先前 v28.5/v28.6 两次独立的 `jobs=4` 全 40 例批跑中，C32 聚合吞吐曾显示
`7.944M -> 7.425M user-UOP/s`（-6.53%）。这不是有效的性能 A/B：单个 C32 进程已经
包含 32 个 core producer 和 8 个 domain worker，多个 C32 case 并发时会共同争用宿主
CPU、NUMA 内存与频率预算，而且两个批次中同一 case 的并发邻居和完成时刻不同。

为隔离这一问题，正式复验只运行 10 个 C32 case：使用同一个当前 Release 二进制，
基线显式选择 v28.5，候选显式选择 v28.6；`jobs=1`，绑定 node0 的 0--47 号物理核并
`membind=0`。每个 workload 连续运行三组相邻 A/B，跨轮反转 AB/BA 次序并重新打乱
workload 次序。每一组都对去除 host timer、throughput、配置选择键和新增审计计数后的
完整 JSON 做递归 exact diff。

| C32 workload | 三轮配对吞吐变化中位数 | feedback 时间变化中位数 |
|---|---:|---:|
| stockfish | +1.85% | -11.94% |
| omnetpp | +0.73% | -9.80% |
| zstd | +2.03% | -10.61% |
| lbm | +1.23% | -9.73% |
| sph_exa | +1.15% | -11.66% |
| tealeaf | +2.29% | -15.11% |
| nab | +2.89% | -13.44% |
| graph500 | +1.97% | -11.57% |
| namd | +1.95% | -12.89% |
| neutron | +0.90% | -9.53% |

10/10 workload 的三轮中位数为正。按 workload 等权的几何平均吞吐提升为 +1.70%，
对十个 workload 中位数做 bootstrap 的 95% 区间为 +1.30%--+2.10%；按全部模拟工作量
和 measurement wall 聚合为 `9.071M -> 9.229M user-UOP/s`（+1.75%）。AB 与 BA
子集的几何平均分别为 +1.54% 和 +2.16%，方向一致；30/30 配对 target-state exact。

阶段累计进一步解释了旧批跑的假回退：固定实验中 feedback 为 -11.27%、weave 为
-3.95%，未修改的 schedule/batch 和 commit 只有 +0.27%/+0.09%；旧共享批跑的 C32
schedule/batch 却膨胀了 +12.57%。因此 -6.53% 来自宿主并发污染，不能归因于
materialized-UOP 专用内核。完整可复验结果保存在
`tmp/materialized-fast-kernel-c32-paired-20260826/summary.json`。

#### v28.7 private-preview 旁路（2026-08-27，C16/C32 单轮门禁）

当前 10 个 C32 workload 三轮基线中，private-preview state certificate 累计占
measurement wall 的 10.82%，但 96.02% 的 memory event 仍要经过证书构造、batch
重复扫描和 domain barrier。v28.6 的 chunk/ring 与 materialized-UOP 专用内核降低了
canonical 路径的其他成本后，这条旧 host 加速路径已不能稳定摊销自身开销。

新增版本化 overlay `gem5-v28_7-fs-preview-bypass.cfg`：继承 v28.6，仅设置
`sim.interval_private_preview=false`。初步固定绑核 C32 结果为：

- LBM 两轮反向顺序中，preview-off 相对 preview-on 分别为 `+6.12%`、`+5.13%`，
  几何平均 `+5.62%`；
- Stockfish 单轮为 `+4.51%`；
- 新维护入口的 LBM 简单复验为 `6.082M user-UOP/s`，配置输出确认 preview=false，
  certificate 时间为 0；相对相邻 v28.6 preview-on 为 `+4.82%`；
- 剔除 preview/domain/host timer 等实现路径计数后，新入口与 v28.6 preview-on、显式
  CLI preview-off 的完整目标状态均递归 exact；Release 构建和 `fastsim_tests` 通过。

随后按要求完成 C16/C32 各 10 workload、每例一对的固定绑核 AB/BA 门禁。20/20
配对的完整目标状态递归 exact；吞吐结果如下：

| 分组 | 等 workload 几何平均 | 工作量加权 | 正收益 | workload bootstrap 95% 区间 |
|---|---:|---:|---:|---:|
| C16 | +1.322% | +1.545% | 7/10 | +0.044%--+2.621% |
| C32 | +0.169% | +0.324% | 7/10 | -1.681%--+1.805% |
| 合并 | +0.744% | +0.701% | 14/20 | -0.414%--+1.815% |

单轮不能估计同 workload 的 run-to-run 方差；bootstrap 区间只反映 workload 采样。
但 C32 `graph500` 和 `neutron` 已分别出现 -4.463% 和 -5.193% 回退，且不是目标
状态差异：preview-off 的 weave wall 分别增加 9.562% 和 5.855%。private preview
不仅构造证书，也用永久 worker 并行执行 private-cache replay；全局关闭后，某些高核
负载省下的证书/barrier 成本不足以抵消退回 canonical 串行 replay 的成本。相反，
C32 LBM 的 weave 缩短 6.528%，吞吐提升 4.327%，证明 crossover 明显依赖 workload。

因此 v28.7 未通过“收益显著且无明显 workload 回退”的全局默认门禁；实验 overlay
保留，维护入口恢复到 v28.6。下一步若继续该方向，应做 epoch-local 自适应选择，而
不是按 workload 配置或全局关闭。完整单轮结果位于
`tmp/preview-bypass-v28_7-c16-c32-r1-20260827/summary.json`；早期简单复验位于
`tmp/preview-bypass-v28_7-simple-20260827/lbm.json`。

#### P0：causal-event 零复制与 preview scratch 复用（2026-08-27）

保持 v28.6 private preview 开启，只删除三个纯宿主结构成本：repair/retime 使用指向
`batch` 或 `materialized` 的只读 view，不再逐 epoch 深拷贝到第三个
`causal_events` vector；`materialized` 与 `PrivatePreviewPlan::component_rank_limit`
跨 epoch 复用最大容量；certificate 的 `active/safe/unsafe core` 临时 vector 改为
与 256-core 配置上限等价的 4×64-bit mask。所有 view 都在 suffix repair 可能修改
`batch` 前消费完，不改变事件顺序或生命周期。

C16/C32 单轮数据中，`materialized_escape_events + private_preview_bypass_events`
至少为 3.253 亿；按 32 B `BatchPending` 计算，旧 `causal_events` 路径至少复制
10.4 GB 描述符。preview plan 另在 11.3 万个 preview epoch 中反复创建 core-vector。

冻结修改前后的独立 Release/IPO 二进制后，对四个 C32 敏感 workload 做三轮交错
AB/BA：

| workload | 吞吐中位数 | weave 中位数 | feedback 中位数 | certificate 中位数 |
|---|---:|---:|---:|---:|
| zstd | +0.570% | -1.297% | -0.349% | -3.482% |
| lbm | +0.351% | -0.425% | -0.537% | -7.415% |
| graph500 | -0.486% | -0.154% | -0.068% | -20.445% |
| neutron | +0.350% | -1.091% | -0.344% | -15.664% |

12/12 配对在只排除 wall/throughput timer 后完整递归 exact，连 preview
safe/unsafe、materialized、domain 调用和 fast-kernel 计数也逐项相同；Release 与
ASan+UBSan 全套测试通过。四 workload 等权几何平均吞吐为 +0.195%，工作量加权为
+0.040%，bootstrap 区间跨零。certificate 12/12 缩短、weave 11/12 缩短，但总吞吐
仍被未修改的 schedule/batch 计时漂移覆盖。因此保留该无配置开关的低风险结构 P0，
但不把它表述成已证明的总体吞吐提升，也不为它创建新版本 profile。完整结果位于
`tmp/p0-epoch-container-20260827/pilot-c32-sensitive4-r1/summary.json`。

#### schedule/batch 热路径 P0 门禁（2026-08-26，未合入）

随后对 schedule/batch 及其相邻 materialized-feedback 热路径做了独立二进制 A/B。
基线二进制在修改前冻结；候选与基线使用相同 C32 trace、绑核和 NUMA 设置，三轮按
AB/BA 反转顺序运行，并递归比较剔除宿主计时字段后的完整目标状态。

首个组合候选把 retired-prefix 的逐 UOP 扫描改为单调边界二分，并跳过确定为空的
branch/store/attribution 聚合。正式 10 workload × 3 轮结果为 30/30 target-state
exact，但等权吞吐几何平均仅 `+0.739%`，bootstrap 95% 区间为
`[-0.176%, +1.695%]`，只有 6/10 workload 的中位数为正；按总工作量聚合为
`+0.424%`。这不满足“收益显著且无 workload 回退”的默认开启门禁。

拆分实验确认它不是一个可通过简单缩窄范围挽救的候选：

- 只保留 sparse-counter 空值保护，在四个敏感 workload 的 12/12 配对中目标态 exact，
  但等权几何平均为 `-0.301%`，95% 区间为 `[-1.722%, +1.141%]`；
- 只保护 branch-miss 聚合，在同一组 12/12 exact 配对中为 `-0.173%`，95% 区间为
  `[-1.107%, +0.515%]`；
- 把冷 timing 字段移出每 UOP 热结果，把初始化写量从 1040 B 降到 432 B，2 workload
  三轮仍只有 `+0.234%` 等权收益，按工作量聚合反而 `-0.125%`；
- retired-prefix galloping search 和冷路径 outline 的小样本方向不稳定，未进入正式门禁。

这些候选通常能缩短局部 feedback 计时，却会改变大函数代码布局、分支和 cache
行为，收益被 schedule/batch/producer 的宿主侧波动抵消，并在部分 workload 上形成
回退。因此所有生产代码候选均已撤回；重建后的 `fastsim` 与实验前冻结二进制
SHA256 均为 `6fdab6ef80265524fbc777986cbcc5c516ba0d62fbdaff26be23b74482e1fa82`，
字节级相同。完整正式结果位于
`tmp/schedule-batch-p0-20260826/p0-retire-sparse-c32-formal-r3/summary.json`，拆分结果位于
同目录的 `p0-sparse-only-negative4-r3`、`pilot-branch-only-sensitive4-r1` 和
`pilot-cold-sidecar-2case-r1`。配对脚本现支持 `--baseline-binary`、
`--candidate-binary` 与 `--cases`，可在不覆盖当前构建产物的情况下复验独立候选。

结论是本轮 P0 完成了实现和严格否决，但没有可安全默认启用的生产改动。下一步不应
继续堆叠纳秒级分支保护，而应选择能降低 per-epoch 容器/merge 工作量、且可按候选
二进制独立验证的结构性切片。

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
wall time 包含处理 native kernel trace 的成本。2026-08-27 当前 exact 二进制在固定
`CPU 0-47 / NUMA node 0`、严格串行的一轮代表性采样中得到：

| cores | stockfish | lbm | graph500 | 等 case 几何平均 |
|---:|---:|---:|---:|---:|
| C4 | 7.668M | 5.125M | 5.082M | 5.845M |
| C8 | 13.005M | 5.888M | 10.664M | 9.347M |
| C16 | 13.669M | 5.776M | 8.539M | 8.769M |
| C32 | 12.089M | 5.817M | 9.325M | 8.688M |

12 例等 case 几何平均为 8.032M，按实际模拟工作量加权为 8.105M user-UOP/s。
这是一轮共享宿主上的绝对速率快照，不能为小幅收益提供置信区间。跨配置结论仍以
固定绑核、串行、相邻且反转顺序的多轮 A/B 为准；v28_5/v28_6 C32 十 workload 三轮
正式门禁为 +1.697%，30/30 target-state exact。完整当前基线见
`docs/fastsim-v28_6-c4-c32-baseline-2026-08-27.md`。

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
2. **反馈 causal-cone/block transfer（首个 IQ 日历切片已完成）**：producer 生成
   dependency、IQ/LSQ、dispatch、commit 的可组合状态转移；证书成立时 O(1) 应用
   block，失败才逐 UOP materialize。v28_5 已把 IQ minimum/replace 变成精确单调
   radix calendar，但没有降低 materialized ratio；完整 transfer 仍待实现。
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

P0、P1.1、P1.2 IQ 日历和 P1.2b 常用配置专用内核已完成。下一步若继续吞吐优化，应完成
dependency/IQ/LSQ/dispatch/retire causal-cone block transfer；当前大量 UOP 仍进入完整
feedback materialization，且 profile 的第一热点仍是
`compute_core_timing_feedback`。P1 后续仍须沿用本节的逐项 diff 门禁。
