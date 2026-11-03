# 共享服务按实际到达重新求值：实施与验证

日期：2026-09-10。状态：**候选实现已验证，有小幅 CPI 收益，吞吐门禁未通过，默认关闭。**

本文保留上一轮只读候选的实现与数据。后续已接入普通 store 并细分资源组回退，
但关键请求仍未覆盖、CPI/吞吐验收失败，见 [最新验证](critical-service-repair-20260910.md)。
下文“每个核心最多恢复一次”与只读范围仅描述本轮历史版本。

本项承接 [两阶段服务修复设计](two-stage-service-repair-design-20260910.md) 的第二单元。
保留 `interval_weave + time_epoch`、Q=1024、并行 producer 与 materialized feedback。
没有 causal_read、gem5 时序输入、负载/PC 分支、补偿系数、FST 重采或设备扩展。

## 1. 实际修复的机制

原反馈使用 `actual_issue + canonical_latency`。canonical latency 内的排队等待由早期
请求时间和资源占用计算；移动请求后继续平移该等待，会重复收费，也可能漏掉新阻塞。

新增 `core.response_shared_service_constraints`，在原核心反馈访问共享请求的现场，
用实际请求时刻和候选资源日历重新调用既有服务计算。CHA、LLC MSHR、DRAM bank、
bank group、rank 激活历史、command/data bus、已有 write queue 的服务状态均来自
同一候选状态。先更新前驱资源，再计算后继；等待允许增加或减少，保留当前 DRAM
命令参数与服务算法，没有统一延迟或只保留历史 winning blocker。

反馈使用新响应更新 load data-ready、WB、RAW、LQ/ROB 和既有队列状态。成功候选
提交其完整 CHA/channel 状态、fill generation/expiry 与队列统计，后续批次继续使用。
候选不再次访问 cache/directory；功能路径只能在通过以下校验时保留。

## 2. 组件与提交边界

共享批次构建 CHA、DRAM channel 和 LLC replacement set 的连接关系；同 line 的
跨核访问、private eviction 和未覆盖副作用也参与排除。组件必须由一个核心消费其
全部当前批次共享资源，才能在原并行核心反馈中独立求值。不同核心的独立资源仍并行。

**资源分组没有按核心全部捆绑。** 同核心上的共享 store 或取指只有与某个读资源
组件实际相连时，才阻止该组件。各核心把通过静态检查的读资源组成一个候选工作块。
`candidate/committed/fallback_components` 统计这些核心工作块；`guarded_requests`
统计静态未纳入的共享请求。unsupported 原因计数按请求，可重叠，不应相加为总数。

动态校验包括：

- 同 CHA 的请求次序、同 DRAM channel 的到达次序保持合法。
- LLC hit/miss/merge 的 fill generation 和半开可见区间仍成立。
- 最终导出的 fragment issue 与产生候选服务的请求时刻完全相同。
- 已准入请求可以预约未来命令，但不能越过未观察工作量的请求下界。

最后一项使用 **反馈后的出口**：下一批下界包含本批实际退休传播产生的
`interval_extra_q16`。使用反馈前的 fetch floor 会把合法组件错误地排除。
如果某核心回退改变了下界，重新检查其余候选。每次迭代至少移除一个候选，每个失败
核心最多恢复一次，没有固定迭代次数后强行接受。

失败工作块的候选资源状态与核心结果全部放弃，仅从既有 epoch-entry checkpoint
恢复该核心的 accepted prefix。其他核心的成功结果保留。队列计数、ROB/LSQ、依赖
结果、audit rows 均按同一事务恢复，没有部分新响应混入旧日历。

## 3. 覆盖限制

这不是设计第二单元的完整验收：

- 已实现的是独立读资源组件；共享 store、atomic、取指、PTE、远端供应、权限升级、
  carried lookahead 等不受本轮重新求值支持，相关资源组件会被排除。
- 当前复用 canonical FCFS 服务路径。FR-FCFS 仅允许原实现明确旁路为 FCFS 的
  topology-scaled window=1；不把一般 FR-FCFS 的离散选择当作已验证固定关系。
- 仅在 reference/local 时钟一致且未启用 DVFS 的运行阶段接入。
- private pending visibility、变化后的 cache/coherence 路径重分类和一般跨核共享
  资源闭合仍未完成。
- 核心恢复粒度仍为既有检查点到 prefix 出口，**没有任意中点 checkpoint、反向依赖
  索引和任意局部区段重算能力**。没有无条件第二遍全部 UOP，但失败候选的范围仍偏大。

因此这次没有重新开启上一轮 private-read-services，也没有将全部服务复用宣称为正确。

## 4. 机制验证

构建与 `./build/fastsim_tests` 通过。初始反例在旧代码明确失败：
`moved arrival reused the stale DRAM queue wait`；修复后通过。

新增回归覆盖：依赖 miss 推迟后排队等待缩短并改变最终退休；实际共享请求起点更新；
后继请求反序的整块回退；跨 fill 边界的整块回退；独立通道的串并行一致；共用通道
不能被两个核心独立修改；无关 store 不排除独立读通道；跨 epoch 保留 DRAM 状态，
并与一个连续 controller 请求流比较。generic/materialized 的周期和有关计数一致，
原 RAW、load/WB 与 WB 容量检查继续通过。

## 5. 最终两项完整 ROI

固定相同 FST、配置与统计口径：`user-plus-kernel`，CPI 为 cycles/user-UOP。
每版本两次、NUMA node 0、串行 ABBA。下表只使用资源组件版最终二进制。

| C4 case | gem5 CPI | 原 CPI | 候选 CPI | 原误差 → 候选误差 | 原 → 候选吞吐（M user-UOP/s） | 变化 |
|---|---:|---:|---:|---:|---:|---:|
| TeaLeaf L1D64 | 0.6138814 | 0.50460785 | 0.50535585 | -17.8004% → -17.6786% | 5.9532 → 5.7750 | -2.99% |
| TeaLeaf LLC32 | 0.7427406 | 0.81864287 | 0.81598609 | +10.2192% → +9.8615% | 5.7852 → 5.2977 | -8.43% |

L1D64 总周期 20,184,314 → **20,214,234**，增加 29,920，绝对误差改善 **0.12185 pp**。
LLC32 总周期 32,745,718 → **32,639,447**，减少 106,271，绝对误差改善 **0.35770 pp**。
这是两个定点结果，不能外推为 P99 或一般负载收益。吞吐样本每版本只有两次，但不足以
支持默认开启：本次观察到的下降明显高于这两项的微小精度收益。

输入人口不变：L1D64 为 40,000,000 user UOP + 169,794 native kernel 记录；LLC32
为 40,000,004 + 299,541。没有改动 syscall 或测量边界。

### PMU

L1D64 的 L1D/L2 miss、DRAM read/write、分支和退休计数不变；LLC hit 少 3、remote
supply 多 3。LLC32 的 L2 hit 少 1、miss 多 1；LLC access/hit 多 2；upgrade 多 1、
remote supply 少 1。其余现有 scope PMU 不变。这里是跨批时间变化后后续 canonical
交织的微小路径人口变化，不是将 candidate 内的 cache 计数再加一次。

复用既有同 ROI gem5 数据，L1D64 的三项主要 PMU 误差均未改善：

| 计数 | gem5 | 原/候选 FastSim | 误差 |
|---|---:|---:|---:|
| L1D tag miss | 273,254 | 273,284 | +0.0110% |
| private L2 tag miss | 271,555 | 271,752 | +0.0725% |
| DRAM data read | 186,893 | 187,054 | +0.0861% |

### 激活与额外工作

| 计数 | L1D64 | LLC32 |
|---|---:|---:|
| 观察的共享请求 | 338,012 | 303,331 |
| 静态排除请求 | 336,119 | 299,375 |
| 候选核心工作块 | 840 | 1,609 |
| 成功工作块 | 171 | 328 |
| 跨批边界回退 | 669 | 1,281 |
| 重新求值请求 | 1,893 | 3,956 |
| 提交的新服务请求 | 351 | 815 |
| 其中 DRAM 请求 | 297 | 683 |
| 实际到达移动的提交请求 | 213 | 656 |
| 相对旧平移结果改变响应的请求 | 83 | 282 |
| 响应提前 cycles 合计 | 0 | 1,523 |
| 响应延后 cycles 合计 | 233 | 561 |
| 候选 UOP 范围 | 1,579,018 | 3,230,424 |
| 回退重算 UOP 范围 | 1,429,268 | 2,911,862 |

回退范围分别占输入 **3.56% / 7.23%**。现有 materialized UOP 统计只记录被接受的
结果，仍为 40,169,794 / 40,299,545；不能据此宣称没有额外工作。

L1D64 338,012 个共享请求中仅 351 个提交了新服务（约 0.104%）。103,124 个为
共享 write，3,086 个为共享 instruction 请求；其他排除原因还包括权限/远端路径和
资源连接后的跨核竞争。不能将这些全部称为错误请求或潜在收益。

## 6. 全 ROI 核心阶段对齐

使用隔离诊断源码导出全部阶段，生产源码及二进制不受插桩影响。输出在核心事务被
接受后写出，失败候选的 stage rows 也恢复，未混入被放弃的试算。

四核 **40,169,786 条硬件记录**的 sequence、PC 与原 FST 完整一致，8 条 syscall
辅助事实沿用原规则单列，实际内核指令全部保留。generic 诊断的 scope（除宿主吞吐）
及全部新服务计数与最终 materialized 候选精确一致。

| 核心 | 原周期 | 候选周期 | 变化 |
|---|---:|---:|---:|
| 0 | 4,905,800 | 4,900,778 | -5,022 |
| 1 | 5,417,429 | 5,423,358 | +5,929 |
| 2 | 3,646,192 | 3,654,499 | +8,307 |
| 3 | 6,214,893 | 6,235,599 | +20,706 |

互斥阶段分解的候选减原值：not fetched −363，fetched not issued −20，issued-load-head
**+33,341**，issued store −9，issued other −17，有退休周期 −3,012；合计 **+29,920**。
这次响应变化传到了退休路径，但各核方向并不一致。全 ROI load-head 缺口仅从
3,487,465 缩小到 **3,454,124** elapsed cycles，主要问题仍在。

约 3,111 万条退休时间改变主要包含后续累计时间位移，不能当作直接修复的请求数量。
同样，233 个逐请求延后 cycles 不能相加或外推为总周期贡献；服务日历、后续请求到达
及跨批交织也随核心出口改变。

复查原 20 个关键 DRAM 见证：7 个请求的服务时间改变，新范围 161–312 cycles，
中位数仍 **161**；gem5 非刷新样本中位数约 **315**。仅 1 个见证的 shared origin
等于最终实际 issue。此处没有给每个见证记录独立 candidate 标记，不能把所有时间
变化归因于该请求直接使用了新路径。主要 owner 服务关系仍未覆盖。

## 7. 决定与后续

保留实现、反例、配置和证据，默认关闭，不修改维护配置。**这次是局部机制修复及
定点验证，不是整体精度/吞吐验收。**

下一步应解决两个已量化的问题：恢复范围过大，以及跨核相连资源导致覆盖过低。
应先在已有核心反馈内部增加可恢复的较近检查点与一致的出口状态，使失败组件只恢复
受影响区段；再接通跨核资源依赖与服务选择变化的局部闭合。不能通过放松边界检查、
增加等待、调 Q 或直接启用旧完整 retime 来扩大表面收益。

## 8. 文件与复现

- `include/fastsim/config.hpp`、`src/config.cpp`：候选配置与范围校验。
- `src/simulator.cpp`：资源组件、原反馈内重新求值、事务恢复、跨批日历提交。
- `include/fastsim/types.hpp`、`src/main.cpp`：激活、排除、回退和工作量统计。
- `tests/test_response_completion.cpp`：机制回归。
- `configs/gem5-exp-shared-service-constraints.cfg`：显式候选入口。

实验目录：`tmp/shared-service-arrival-20260910/`。`inventory.json` 和 `inputs/` 固定
输入；`before-sha256.json` / `before/` 保存修改前快照；`component-summary.json`
给出最终 CPI/PMU/吞吐，`component-validation.log` 及 `runs/*/component-*` 保存 ABBA
命令和二进制 SHA。`full-stage-comparison.json`、`full-stage-partition.json` 与
`critical-owner-comparison.json` 保存完整阶段及关键请求证据。

初版按核心捆绑资源的 8 次 ABBA 仅作覆盖诊断，不混入最终结果；最终版 8 次 ABBA
覆盖这两个 case，另做一次完整阶段诊断和一次最终关闭开关的兼容性确认。一次与
测试链接交叠的基线及其被中止的候选任务保存在 `interrupted-overlap/`，排除性能结论。
没有新 gem5 或完整负载矩阵。
