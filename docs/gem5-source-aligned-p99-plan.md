# FastSim 设计演进、gem5 源码对齐与 CPI P99 ≤ 10% 实施方案

首次整理：2026-08-02  
最后更新：2026-08-04

本文档是本项目关于 functional-trace 多核 timing simulator 的唯一权威设计与
决策记录。其他 architecture、parameter-coverage 和旧 validation 文档只保留
局部接口说明或历史快照；若与本文冲突，以本文的“当前状态”和最新检查点为准。

## 0. 方案演进和决策日志

项目约束从一开始就固定为：运行时输入只有 functional trace，CPI/PMU 以指定
gem5 配置为 baseline，验证覆盖 C4/C8/C16/C32；最终要求每个核数组内的
workload-level CPI absolute-error P99 不超过 10%，且每一个 case 的仿真吞吐量
不低于 5M UOP/s。方案变化来自实测反例和 gem5/ZSim 源码证据，不按单个
workload 调常数。

| 检查点 | 当时方案 | 触发更新的证据 | 决策和动机 | 状态 |
|---|---|---|---|---|
| 初始模型 | UOP 解码、计数和简化延迟；共享访存近似逐事件处理 | 没有 response-driven ROB/IQ/LSQ，无法解释 gem5 rename/IEW stall；多核事件顺序受每核局部时间影响 | 放弃“计数器加固定 miss penalty”作为最终核心模型 | 已淘汰 |
| Interval v1 | 每核独立生成一段 UOP，再按全局 lower-bound issue time 编织访存 | 宿主同步不是瓶颈；关键是不同核心推进后访存次序和响应会互相改变 | 使用共同 simulated-time epoch，producer chunk 只作为解码微批，不作为同步边界 | 已实现 |
| Causal-frontier v2 | Q=2048 fixed epoch、共享事件一次 weave、TBE-like capacity | 原始 C4/C8/C16/C32 P99 为 65.780%/61.975%/50.670%/43.034%；PyTorch 严重低估 | 需要 memory response 回到核心资源生命周期，而不只增加 interval tail | 已替换 |
| gem5 source alignment | 对齐 O3、x86 walker、Ruby Sequencer/TBE 和 DRAM 源码 | gem5 是验收 baseline；仅匹配缓存容量/latency 不能匹配 stall 语义 | 组件参数以“分配、释放、重试、响应回调”语义映射，禁止按名称机械复制 | 持续执行 |
| 检查点 A | 单 walker、无同页 coalescing；Sequencer=16；response-driven IQ；Q=1024 | Q=2048 P99 仍为 24.447%/32.449%/35.490%/54.478%；Q=1024 同时改善四组 P99 | 保留 Q=1024；建立跨 interval IQ/Sequencer calendar 和同语义 PMU | 已实现并完成 92-case 验证 |
| 更小固定 Q | Q=512 | C32 random 误差从 +41.00% 降到 +33.17%，但吞吐量只有 4.701M | 证明 Q 会影响当前模型的精度和吞吐；Q=512 只保留为敏感性证据，后续组件调优固定 Q=1024 | 已否决 |
| Q 实验纪律 | 曾计划把跨 Q target-state hash 作为近期 gate | 当前 response/order closure 不完整，Q 会改变边界截断、可见 MLP 和 CPI；它现阶段就是模型超参数 | 当前 CPI/PMU 优化统一固定 `sim.interval_max_cycles=1024`；不混用其他 Q 选组件或参数，跨 Q 鲁棒性推迟到主目标完成后单独研究 | 2026-08-04 已冻结 |
| 并行 private preview | 每核并行预演 private path，再物化 shared escape | 数值与 canonical path 等价，但 C4/C32 random 仅 4.877M/4.864M | 保留证书和测试，生产关闭；不能用额外 phase barrier 换取表面并行 | 已否决为生产默认 |
| Hot-path A1 | 稀疏 interval 不初始化两组 256-entry MSHR heap | C32 random 是性能最慢 case，只有很小余量 | 数学证明不可能满时跳过 heap；CPI/PMU 不变，吞吐量 5.165M→5.418M | 已实现 |
| ZSim 对照 | 固定 phase bound-weave、预先固定 memory path、事件 DAG 重算 timing、末尾反馈 skew | ZSim 已解决“每核推进不同”和逐事件全局同步，但假设短 phase 内 path-altering interference 很少 | 不把“两阶段推进”本身当创新；新方案必须自动检测并修复 path-altering 次序 | 2026-08-03 完成源码审查 |
| 检查点 B0 | corrected-arrival、跨核同 line 冲突识别和可回退整 epoch replay | C4/C32 pilot 证明回退正确，但 C32 random 误差变为 118.132%，C4/C32 PyTorch 吞吐量降至 4.193M/2.902M | 保留 transaction、PMU 和测试作为机制底座；不启用为默认，禁止把整批 corrected time 当成精度修复 | 已实现，pilot 未通过 |
| 检查点 B1a | private-set certificate + sparse suffix repair | B0 的实际 replay/component 放大为 16.5--31.3倍；粗粒度 per-core preview 在真实 trace 上慢 8%--25% | 按 `(core,private-set component)` 证明私有状态分量，只把受 invalidation 影响的 suffix 留在 canonical 路径；小于收益门槛的 boundary 直接 bypass | 已实现；92-case exact-equivalence 与吞吐 gate 通过，time-epoch profile 已启用 |
| 检查点 B1b | corrected-arrival causal closure | B1a 已解决 private path 的全 epoch fallback，但尚未解决 response 改变 shared-event arrival/order | 固定 Q=1024，只重放可能改变 shared path 的 line/set 因果闭包，排队资源只重算 timing；超预算时在同一 Q 内回退 canonical | 待后续 shared-path repair 阶段 |
| DRAM 源码对齐 | RoRaBaCoCh、rank/channel/bank、command/data bus 和 topology-scaled FR-FCFS timing repair | FCFS 无法解释 C16/C32 random/seq 的核数相关误差；完整 64-entry controller lookahead 又超过 functional trace 能证明的顺序 | 只在静态 producer topology 可证明的窗口内重排，C4/C8/C16/C32 使用 1/1/3/7；固定点失败显式回退 | 已实现并启用 |
| 检查点 C0 | 64B fetch buffer、taken-branch fetch-group 终止、2-cycle redirect | `int_div_serial` 固定 +22.6%，`simd_sse_dense` 固定 -16.8%；gem5 源码和 PC 序列表明两者分别来自错误的 16-cycle branch 常数和缺失的 fetch-block refill | 用 functional PC 对齐 gem5 fetch-buffer 语义；16-cycle 常数不再替代缺失的 OoO stall | 已实现；两类误差均降到 0.4% 内 |
| 检查点 C1 | producer-consumer causal slack | feedback 把 producer 的全部 `completion_extra` 复制给 consumer，即使 lower-bound schedule 已有 FU/queue slack | 传播绝对完成约束 `max(0, producer_actual_complete - consumer_base_issue)`，禁止重复收费 | 已实现；92-case mean 改善，P99 仍失败 |
| B3 dense ROB/LQ/SQ | 每 UOP 持久 calendar + ordered commit | C4 PyTorch 从 -24.1% 翻到 +23.8%，C32 random 降至 3.68M UOP/s；ROBFullEvents 甚至接近每 UOP 触发 | 保留代码为实验开关，生产关闭；不能把 dense calendar 或 exposure 常数当最终方案 | 未通过 gate |
| 检查点 C2 | 固定 ROB completion ring、ordered commit、LQ/SQ/TSO response lifetime，并只传播越过 lower-bound slack 的 residual | 92-case P99 从 24.985%/27.193%/30.151%/33.161% 降至 13.843%/15.315%/12.303%/14.182%；但 C32 最慢单次为 4.991M，四组 P99 仍高于 10% | 它验证闭环方向，但仍是逐 UOP feedback pass；后续必须缩小 causal cone 并修复 shared-path 次序 | 已实现；作为 C3 无损优化的 target reference 启用 |
| 检查点 C3-A | scratch reuse、horizon upper-bound、确定性 C-way merge、DRAM channel domain parallel、IPO/LTO | C32 random 的 schedule/batch 曾占主要宿主时间，且 C2 最慢 case 低于 5M | 只改变宿主算法；用完整 target-state hash 验收，不改 Q 和微架构参数 | 已实现；C3-A 92-case 最低吞吐均超过 5M |
| 检查点 C3-B1 | K=4096 transport + checkpoint response activity entry/exit certificate | K=4096 在 C32 random 比 K=2048 快 2.98%；长纯计算 segment 仍为每 UOP 执行完整 response loop | tentative 重建 IQ/ROB/dispatch/commit 出口；任一容量、依赖或 serialize 边失败即从未修改入口走完整路径 | 已实现；92-case target state 零差异，认证 13.824% UOP |
| 全 epoch timing fixed point | B3 corrected time 固定 canonical path，反复重算整 epoch queue timing | C4 CPI 变为 +141%--+191%、吞吐量 1.6--2.0M；全局 `interval_gap` 被再次注入每个响应，形成正反馈 | 原型代码已撤回，仅保留反例数据；下一步只能修复局部 producer/ROB causal cone | 已否决 |
| 输入与跨 ISA 收敛 | gem5 functional trace 作为当前唯一精度输入；FastSim 运行时不做 ISA decode | gem5 已输出 UOP 边界、OpClass、依赖、分支和访存功能事实；DR 原始 trace 缺少 gem5 UOP 降级和通常缺少物理地址 | 把 canonical FST v5 定义为运行时 functional IR；未来 drmemtrace 只经离线 adapter 转成相同 IR，不把 decoder 放入仿真 hot path | 2026-08-04 已决策；DR adapter 延后 |

### 0.1 不变的验收纪律

1. CPI gate 按 C4/C8/C16/C32 分别计算 type-7 P99；不能用 pooled P99、mean
   或 nearest-rank P90 替代。
2. 性能 gate 使用每组最慢 case，而不是平均吞吐量；任一 case 低于 5M 即失败。
3. 所有精度提交必须同时报告逐 workload signed error 和 gem5 PMU；只改善 CPI
   而恶化机制 PMU 的修改不得进入默认配置。
4. gem5 timing/path label 只用于诊断和验收，不能进入运行时 trace。允许扩展的
   只有寄存器、地址、分支、同步等 functional facts。
5. 结构错误没有闭环前，不用 `memory_exposure`、固定 DRAM latency 或 workload
   correction 吸收误差。
6. 临时数据、测试 scratch 和中间报告统一放在项目 `tmp/`；禁止向
   根级 `/tmp` 写入项目数据。`tmp/` 已加入 `.gitignore`。
7. 当前所有组件消融、参数对齐和最终 C4--C32 gate 固定
   `sim.interval_max_cycles=1024`。Q 是现阶段的精度超参数；除历史消融或最终独立
   鲁棒性报告外，不以其他 Q 选择组件参数，也不把跨 Q 一致性作为近期验收条件。

基线：

- gem5 源码：/data00/yinhaolang/gem5
- gem5 运行配置快照：
  /data00/yinhaolang/TSim/data/raw_v28_business_a1_sharedzipf_seed0_c32/
  W_v28_pytorch_base/config.ini
- FastSim 配置：configs/gem5-v28_1-time-epoch.cfg
- FastSim 结果：results/gem5-v4-o3-dtlb-stage3-final-tbe256/

## 1. 结论和设计约束

可以，而且应该先把 gem5 源码作为组件语义规范，再设计 FastSim。这里的
“对齐”不是逐行复制 gem5 的逐周期执行，而是对齐以下内容：

1. 状态何时分配、何时释放；
2. 请求何时可以发出、何时必须重试；
3. 响应会唤醒哪些依赖，并怎样反压 ROB、IQ、LQ、SQ 和前端；
4. Ruby 消息经过哪些状态、资源和延迟；
5. DRAM 如何映射地址、选择请求和施加时序约束。

目标定义为：在 C4、C8、C16、C32 四组中，分别对 workload 的 UOP-CPI
absolute relative error 计算 P99，每组都必须不超过 10%。把 92 个 case
混在一起计算 pooled P99 只能作为补充，不能替代逐核数 gate。吞吐量 gate
保持所有 case 不低于 5M UOP/s。

现有 v4 trace 可以支持第一轮结构修复，但不能对任意 workload 保证 P99
不超过 10%。原因不是 trace 必须带 timing oracle，而是当前 64B 记录仍缺少
若干可以合法采集的 functional facts，例如完整寄存器依赖、指令取址信息和
页表物理路径。建议增加 v5 functional sidecar；gem5 的 fetch、issue、
complete、commit tick、cache path、MESI 状态和 DTLB hit 等 timing/path
标签只能用于离线诊断和验收，禁止进入 FastSim 运行时输入。

## 2. 当前基线离目标有多远

当前结果按每个核数的 23 个 workload 计算。P99 使用线性分位数；由于每组只有
23 个样本，它已经非常接近最坏 workload，因此这个目标比 mean MAPE 严格得多。

| Cores | Mean abs. error | P99 abs. error | Max abs. error | Median throughput | Min throughput |
|---:|---:|---:|---:|---:|---:|
| 4 | 12.263% | 65.780% | 67.598% | 19.981M | 11.673M UOP/s |
| 8 | 12.637% | 61.975% | 63.507% | 18.748M | 10.545M UOP/s |
| 16 | 13.436% | 50.670% | 51.286% | 17.083M | 9.546M UOP/s |
| 32 | 14.639% | 43.034% | 43.537% | 14.485M | 8.108M UOP/s |

92-case pooled P99 为 63.876%，同样远未达标。当前最慢 case 只有 1.62 倍
运行时间预算：如果新增逻辑让其运行时间增加超过约 62%，吞吐量就会跌破 5M。

尾部误差具有明显结构，而不是随机噪声：

| Workload | C4 | C8 | C16 | C32 | 直接观察 |
|---|---:|---:|---:|---:|---|
| pytorch_base | -67.60% | -63.51% | -51.29% | -41.25% | C4 FastSim 0.399 CPI，gem5 1.232；约 0.83 CPI 未建模 |
| pytorch_heldout | -59.33% | -56.54% | -48.49% | -36.04% | 与 base 同方向，属于系统性低估 |
| memory_seq_moderate | -19.87% | -34.28% | -26.66% | -0.92% | C8 FastSim 4.325，gem5 6.581 |
| memory_random_mlp | -2.82% | +4.37% | +19.90% | +43.54% | 误差随核数翻转并增长，指向共享资源/DRAM |
| int_div_serial | +22.60% | +22.58% | +22.54% | +22.55% | 与核数无关，固定分支恢复模型是主要来源 |
| simd_sse_dense | -16.78% | -16.79% | -16.80% | -16.82% | 几乎无内存影响，指向依赖/后端语义 |

高流量 L1D miss、private-L2 miss、CHA lookup 和 branch miss 的 count WAPE
已经很低，但这只证明退休访问数和大部分稳定态 tag 结果接近，不能证明响应
延迟、瞬态一致性、请求合并、队列占用和反压正确。以 PyTorch 为例，gem5
统计中单核 rename Blocked 约 66%--69%，IQFullEvents 约 14 万次，而 FastSim
仍把大部分内存代价压缩成 interval tail correction。

另一个必须修正的结果解释是 timing certificate。当前四组分别记录了
29,666、59,841、121,888 和 245,077 个 corrected-horizon violations，
但 timing_certificate_failures 为 0，因为真正的 timing certificate 尚未启用，
而不是因为这些 epoch 已被证明安全。当前结果只能称为确定性近似，不能称为
certified exact weave。

## 3. gem5 源码审查结论

### 3.1 O3 核

主要源码：

- src/cpu/o3/BaseO3CPU.py，第 98--131、142--188、202--208 行；
- src/cpu/o3/inst_queue.cc，第 999--1004、1097--1104 行；
- src/cpu/o3/lsq_unit.cc，第 740--824 行；
- src/cpu/o3/iew.cc 和 commit.cc 的 branch squash/redirect 路径；
- src/cpu/o3/rename.cc 的 ROB/IQ/LQ/SQ/physical-register stall 路径。

捕获配置确实是 8-wide、ROB 192、IQ 64、LQ/SQ 32，且 x86 needsTSO=true。
但仅复制容量数值并不等于对齐：

- gem5 非内存 UOP 在 issue 后离开 IQ，内存 UOP 要等核心侧执行完成才离开
  IQ；对 load/atomic 这通常依赖数据响应，普通 store 则可在地址/数据生成后
  完成核心执行，但仍留在 SQ 等待 post-commit memory completion。FastSim
  当前对所有 UOP 都在 issue 时释放 IQ。
- gem5 load 在 commit 时释放 LQ；store commit 后才允许写回，并在内存完成后
  释放 SQ。needsTSO=true 时同一时刻只允许一个 store in flight。FastSim
  当前在 retire 时释放 SQ，store response 不反压 SQ。
- gem5 的 load response 会完成该内存 UOP、唤醒消费者并改变 ROB/IQ/LSQ
  占用。FastSim 先生成 lower-bound 核时间，再把 shared response 传播成
  producer extra 和一个 interval tail；它不会重新建立这些资源的占用历史。
- gem5 branch 在 execute 检测错误，经 IEW 到 commit 的 time buffer 触发
  squash/redirect。FastSim 使用 completion 加固定 16-cycle penalty，
  没有根据分支执行位置、流水级和错误路径占用计算恢复时间。
- gem5 rename 受 physical-register free list 和 serializing-before 条件约束；
  FastSim 尚未建模。

流水延迟必须按语义方程映射，不能机械复制数值。例如 gem5 opLat=1 的 UOP
在 issue 后一拍产生结果，FastSim 的 op.latency=1 已表达这一点，因此不能只因
BaseO3CPU.py 中 issueToExecuteDelay=1 就把当前 issue_to_execute 从 0 改成 1。
正确方法是用微基准对齐 fetch、issue、wake-up、writeback、commit 的相对边，
避免重复计时；iewToCommit 的退休边也应采用同样方法验证。

### 3.2 functional trace 生成器

自定义 TaoTrace 源码 src/cpu/o3/probe/tao_trace.cc 第 1602--1643 行已经能够
读取每个 UOP 的全部 architectural source/destination register class 和 index，
但输出只保留最多四个 producer distance。部分负载中四槽全满比例并不低：
int_div_serial 约 57.4%，int_alu_dense 约 6.7%，simd_sse_dense 约 4.6%。

因此完整依赖、destination register identity 和 register class 都可以作为
functional trace 扩展，不需要使用 gem5 timing tick。SIMD 的固定 -16.8%
误差不能归因于当前设置的 1-cycle SIMD latency，因为基线标签显示主要 SIMD
op class 的 issue-to-complete 也是 1 cycle；更可能的候选是被截断的依赖、
VecElem 类处理、physical-register pressure 或退休边语义。此处仍需通过
v5 trace 和部件消融确认，不能提前认定单一根因。

### 3.3 x86 DTLB/page walker

src/arch/x86/pagetable_walker.cc 第 72--93、111--133 行表明，timing mode
只有一个活动 walk，后续请求进入队列；源码中的 TODO 明确说明尚未实现
coalescing。当前 FastSim 的 4 个 walker lane 加同页 merge 与基线结构不一致。

一次 4KiB long-mode walk 还会通过内存系统读取 PML4/PDP/PD/PTE。v4 trace
只有 virtual-page token 和最终数据物理地址，没有这些页表项的物理地址。
把 page_walk_latency 调成某个常数只会在 workload 间过拟合；现有 1/8/32
cycle 消融已经显示这一点。

### 3.4 Ruby MESI_Three_Level

主要源码和配置：

- src/mem/ruby/system/Sequencer.cc，第 305--384、950--958 行；
- MESI_Three_Level-L0cache.sm、MESI_Three_Level-L1cache.sm；
- MESI_Two_Level-L2cache.sm、MESI_Two_Level-dir.sm；
- 捕获 config.ini 中每核 Sequencer max_outstanding_requests=16；
- controller number_of_TBEs=256；L0/L1 transitions_per_cycle=32，
  shared L2/directory 的关键 controller 为 4。

Sequencer 的 16 是 CPU 侧 outstanding request 上限；TBE 256 是各 Ruby
controller 的 transient transaction 容量，两者不是同一资源。当前 FastSim
把 L1/L2/LLC mshrs 都设为 256，等价于把 TBE 容量误当成 per-core request
窗口，必须拆开。

Sequencer 还按 cache line 维护 request table。同一 line 的后续请求进入 alias
列表，并在回调时按读写规则共同唤醒或重发。SLICC 协议包含 GETS、GETX、
UPGRADE、PUTX、data/ack 和多种 transient state。FastSim 当前在请求处理时
同步更新/fill private tag，缺少“请求已发出但数据/权限尚未返回”的 line
transient 状态，后续同 line 请求可能过早命中。

捕获配置的 mandatory queue latency 为 1；L0、private L1、shared L2 的
request/response latency 多为 2，directory latency 和 network link 还需逐段
组合。当前固定 L1D=4、L2=12、LLC=36、NoC one-way=12 不是该路径状态机的
等价表达。基线 Ruby 统计中 L0 hit latency 接近 1 cycle，而 PyTorch 的
Ruby miss 平均约 217 cycles，说明“稳定态 hit”和“外部 miss”必须分开建模。

### 3.5 DRAM

捕获基线是 8 个 DDR4 controller/channel，每 channel 2 ranks、每 rank 16
banks、4 bank groups，地址映射为 RoRaBaCoCh，调度为 FR-FCFS，read/write
queue 为 64/128，page policy 为 open_adaptive，并包含 tCCD、tRRD、tXAW、
读写切换等约束。

FastSim 当前只按低位选择 channel/bank，维护一个 open row、bank ready 和
channel bus，并以 tCL+tRCD+tRP 组成延迟。C32 random-memory 从轻微低估翻转
为 +43.5% 高估，是这一模型和全局事件次序共同作用的强信号；仍需 DRAM
row-hit、queue latency 和 bus utilization PMU 消融来区分二者。

## 4. 组件差距、可辨识性和优先级

| 组件 | gem5 语义 | FastSim 当前状态 | functional trace 可辨识性 | 优先级 |
|---|---|---|---|---:|
| IQ lifetime | 非内存在 issue 释放；load/atomic 等核心侧 memory completion，regular store 完成地址/数据执行后释放 | 全部在 issue 释放 | 当前 trace 可做 | P0 |
| ROB/response feedback | response 改变 wake-up、commit 和前端 stall | 仅 producer extra + interval tail | 当前 trace 可做 | P0 |
| LQ/SQ/TSO | LQ commit 释放；SQ memory completion 释放；TSO 单 store in flight | LQ/SQ retire 释放；store response 基本不阻塞 | 当前 trace 可做主体 | P0 |
| Register dependence | 全部 src/dst、rename/free-list | 最多四个 producer，无 free-list | 需要 v5 functional deps/reg sidecar | P0 |
| Branch recovery | execute 检测，经 stage 边 redirect/squash | 固定 16 cycles | committed branch 可做 recovery 边；wrong-path 占用不可精确识别 | P0 |
| Memory ordering | StoreSet、forwarding、partial-overlap stall/replay | 所有 memory event 强制每核程序序 | 地址/大小可做 forwarding；精确 speculative replay 不可辨识 | P1 |
| Sequencer | 每核 16 outstanding，按 line alias/coalesce | 用 256 TBE-like lanes | 当前 trace 可做 | P0 |
| Ruby transient | 请求/权限/数据/ack 分离，TBE 256 | 请求时同步更新 tag，粗 MESI | 当前 trace 可做 committed-request 主体 | P1 |
| Controller/NoC | SLICC transition bandwidth、vnet、link/queue path | 固定 NoC/LLC latency | 当前 trace 可做资源日历近似 | P1 |
| DRAM | RoRaBaCoCh、rank/group、FR-FCFS、完整 timing | 简化 open-row FIFO-like 模型 | physical address 足够 | P1 |
| DTLB walker | 单 active walk，无 merge，PTE 访存走 memory | 4 lanes、同页 merge、固定 latency | 队列可做；真实 PTE path 需 v5 sidecar | P2 |
| I-cache/ITLB | fetch path、wrong-path traffic | 不支持 | committed ifetch 可扩展；wrong-path 不可精确识别 | P2 |

这里的“当前 trace 可做”不表示无需改算法，而是输入中已有足够的 functional
因果信息。反之，不能从退休流唯一恢复的行为必须明确标记为 proxy，不能用
gem5 timing/path label 偷渡成输入。

## 5. 建议的 v5 functional trace

保持当前 64B v4 record 作为顺序读取的 hot stream，不把所有扩展字段塞进每个
UOP。每核增加按 UOP ordinal 关联的压缩 sidecar：

1. deps/reg sidecar：完整 source/destination class+index、超过四个的 producer
   distances、VecElem/CC/predicate class；用于依赖图和 physical-register
   free-list。
2. ifetch sidecar：architectural instruction size、fetch virtual page 和
   functionally resolved 的 physical instruction page；用于 committed-path
   I-cache/ITLB。
3. ptw sidecar：每个 translation identity 对应的 PML4/PDP/PD/PTE physical
   line 序列和 page size；FastSim 自己决定何时发生 TLB miss，sidecar 不提供
   hit/miss 或 latency。
4. sync sidecar：跨核 barrier、lock/futex、atomic 的 functional sequence
   anchor，以及必要时的 read-from identity；用于固定程序因果，不提供 cycle。

明确禁止进入运行时输入：

- fetch/issue/complete/commit tick；
- gem5 seq_num 中由错误路径造成的 speculative gap；
- cache/path class、MESI-before、coherence oracle；
- DTLB hit、walker latency、MSHR depth、bank queue depth；
- 任何直接由 baseline CPI 或 PMU 反推的 workload ID correction。

这一区分很重要：物理地址、寄存器 identity、PTE physical path 和同步顺序是
本次执行的 functional facts；cache hit、排队时间和协议路径是待预测的
microarchitectural outcomes。

## 6. 新仿真架构：gem5-aligned causal interval engine

### 6.1 每核事件驱动 O3，而非逐 cycle tick

每核维护固定容量 ring/SoA：

- ROB：dispatch、complete、retire 和 head-ready；
- IQ：ready operands、FU class、issue state；load/atomic 等 data response，
  regular store 按地址/数据执行完成释放；
- LQ/SQ：地址/大小、forwarding、commit、store-drain 和 response state；
- physical-register free list 和 producer wake-up；
- stage/FU/port 的稀疏 resource calendar。

只在以下事件跳转：dispatch window 可推进、FU completion、memory response、
branch redirect、ROB head retirement、store drain。空闲 cycle 不执行循环。
因此模型具备 gem5 O3 所需的 occupancy 和 backpressure，又保持 interval/event
粒度。

shared response 不再变成单个 interval tail。回调必须：

1. 标记 load/store completion；
2. 释放 load/atomic 的 IQ，或已完成 post-commit store 的 SQ；
3. 唤醒真实消费者；
4. 重新计算受影响 ROB 后缀的 retire/front-end bound；
5. 如产生更早或更晚的 shared request，通知因果前沿检查器。

### 6.2 自适应 causal frontier

对 core c，局部前沿 Fc 定义为：任何尚未解析的 shared response 都不可能再
改变其之前的 dispatch、issue、retire 或产生更早 shared request 的最大时间。
全局可提交前沿是所有活动核 Fc 的最小值。

每核可以乐观推进到自己的 lookahead horizon，并输出带 earliest issue 和
依赖来源的请求。协调器不按“核 0 一段、核 1 一段”的宿主执行顺序决定结果，
而是按模拟时间和以下部分序编织：

- 同一核的 register/LSQ/synchronization 因果边；
- 同一 cache line 的 coherence/transient 边；
- 同一 Sequencer/controller/output-link 的容量边；
- 同一 DRAM channel/rank/bank 的资源边。

只有全局前沿之前且 certificate 成立的状态才能提交。若响应改变了尚未提交
事件的次序，只从最近 checkpoint 回放受影响 core slice 和 conflict component，
不回放整个 epoch。若 certificate 无法证明安全，则走 canonical fallback。

这直接处理“各核 cycle 推进不同导致访存顺序不同”的问题：顺序来自模拟因果和
资源冲突，而不是宿主线程到达 barrier 的顺序，也不是强制所有内存 UOP 按退休
程序序。

### 6.3 Ruby/NoC 的稀疏事务模型

private L0 稳定态 hit 继续走本地 fast path。对 miss、write、atomic、eviction
或 permission change 才物化轻量事务：

- 每核独立 Sequencer outstanding=16；
- 每 line transient entry 保存 data-ready、permission-ready 和 waiter list；
- 每 controller TBE=256、transition bandwidth 独立建模；
- 从 SLICC 状态/动作生成紧凑的 transaction template，不创建通用消息对象；
- network 以 output-link/vnet resource calendar 表达串行化和 latency；
- response callback 直接连接每核 O3。

当前结果中 escape event 约占 memory event 的 23.2%，所以详细协议只放在
escape path 才有机会保住吞吐量。这个比例必须在新模型中继续报告，不能假设
永远不变。

### 6.4 DRAM 批处理

按 channel 收集 causal-frontier 内的请求，以小批次执行 FR-FCFS，并维护
rank/bank-group/bank、open row、read/write mode 和 timing resource calendar。
地址解码严格采用捕获的 RoRaBaCoCh。批处理结束前不需要逐 cycle tick；
只有 ACT/RD/WR/PRE/refresh 或 mode switch 产生稀疏事件。

## 7. 误差修复顺序

### Phase 0：冻结基线和可归因测量

- 固定 gem5 commit/source/config hash 和 92-case manifest；
- 验证脚本增加逐核数 P99、逐 workload signed error 和 bootstrap 区间；
- 从 gem5 stats 提取 O3 stall/occupancy、load-to-use、Ruby latency/outstanding、
  controller transition、NoC queue、DRAM row-hit/latency；
- 把 corrected-horizon violation 设为失败或强制 fallback，不能只计数后提交。

退出条件：当前结果可一键重现；每个 tail workload 都有 CPI 和组件 PMU
误差向量；无 timing label 被 converter/runtime 读取。

### Phase 1：O3 资源生命周期和 branch

- 实现 response-driven IQ/ROB/LQ/SQ 更新；
- 实现 TSO store drain 和 store response；
- 用 gem5 stage 语义生成 branch redirect，而不是固定 16；
- 接入 v5 deps/reg sidecar 和 physical-register pool；
- 对 load/store forwarding、完全覆盖和 partial overlap 建模；
- 去掉无条件 memory program-order issue，改为 LSQ 因果边。

优先验证 int_alu、fp_alu、SIMD、int_div、PyTorch C4/C8。中间 gate：
compute-only workload 不超过 5%，SIMD/int_div 不超过 10%，PyTorch 的
IQ/ROB/rename stall 方向和数量级与 gem5 一致。

### Phase 2：Sequencer、transient Ruby 和 core-memory 闭环

- 拆分 Sequencer 16、controller TBE 256 和 transition bandwidth；
- 实现 per-line alias/coalesce、data/permission 双 readiness；
- 依据 SLICC transaction template 实现 GETS/GETX/UPGRADE/PUTX/data/ack；
- 将所有响应回调接入 Phase 1 的 O3 状态；
- 对同 line、同 controller 冲突启用 causal certificate 和局部回放。

优先验证 PyTorch、memory_seq、cache_L1/L2 和 coherence probe。中间 gate：
这些负载的 Ruby mean/P50/P90 latency、outstanding histogram 和 CPI tail
同时改善；不能只改善 CPI 而恶化 PMU。

### Phase 3：NoC、DRAM 和高核数次序

- 从实际 topology/config 生成 link path 和 controller latency；
- 实现 RoRaBaCoCh、rank/bank-group、FR-FCFS、read/write batching 和 DDR4
  timing constraints；
- 扩展 conflict component 到 output link 和 DRAM channel；
- 对 C16/C32 统计 replay work、row-hit、queue latency 和 bus utilization。

优先验证 C16/C32 random、sequential、GoFeed 和业务负载。中间 gate：
random C32 和 sequential C8/C16 均进入 10%，且低核数方向不反转。

### Phase 4：DTLB、I-side 和不可辨识项的受限 proxy

- x86 walker 改为单 active walk 加队列、无同页 coalescing；
- 接入 ptw sidecar，使 PTE 请求走同一 Ruby/DRAM 路径；
- 接入 committed ifetch 的 I-cache/ITLB；
- 对 wrong-path fetch/load、StoreSet speculative pollution 只使用
  workload-independent、参数化且有上下界的 proxy。

proxy 必须单独报告贡献，并在不同 seed/heldout 上验证；不允许按 workload
名称拟合。若加入 proxy 后 P99 只在训练集改善，则回退而不是保留。

### Phase 5：性能收敛和最终 gate

- hot 64B stream + 稀疏 sidecar 顺序预取；
- ring/SoA、arena 和无分配 event descriptor；
- private-hit vectorized fast path；
- conflict bucket 按 line/controller/link/channel 分片；
- checkpoint 只保存增量，目标 replay work 不超过 accepted work 的 5%；
- 每个 phase 都跑 C4/C8/C16/C32 accuracy + throughput，禁止最后才测速。

最终 gate：

| 类别 | 必须满足 |
|---|---|
| CPI | C4、C8、C16、C32 各自 P99 absolute error ≤ 10% |
| CPI 辅助 | 每组 mean ≤ 6%，signed mean 的绝对值 ≤ 3%，报告 max 而不隐藏 |
| Functional count PMU | 高流量 branch/cache/TLB-miss WAPE ≤ 1% |
| Timing/queue PMU | O3 stall、Ruby latency/outstanding、DRAM latency/row-hit 的 mean/P90 目标 ≤ 10% |
| 因果正确性 | unresolved horizon violation=0；certificate 失败必须 fallback |
| 性能 | 92-case 每个 case ≥ 5M UOP/s；报告 min/median/P10 |
| 泛化 | seed0 只用于开发；seed1 和 heldout workload 不参与参数拟合 |

P99 必须以 workload-instance 为样本，不能用大量同一 trace 的小窗口人为扩大
样本数。窗口误差可以用于定位 phase behavior，但不替代完整 ROI CPI gate。

## 8. 吞吐量保护策略

当前最慢 case 为 8.108M UOP/s，离 5M 只允许约 62% 的运行时间增长。因此以下
实现方式不应采用：

- 每核每 cycle 扫描 ROB/IQ/LSQ；
- 每个 Ruby hop 分配 C++ 对象；
- 每个 memory event 都执行全核 barrier；
- 每个 epoch 全量复制 cache/directory；
- certificate 失败后回放全部核和整个 epoch。

性能预算应按层分配：

1. 每 UOP 核心状态更新保持 O(1)，只访问紧凑 ring；
2. private stable hit 不进入全局 weave；
3. 同 line 请求合并，Ruby 只物化一次底层事务；
4. controller/link/DRAM 使用批量 resource calendar；
5. 仅受响应影响的 ROB 后缀和 conflict component 回放；
6. 以实际 replay ratio、escape ratio、event/UOP 和 host cycles/event 作为
   每次提交的强制指标。

如果 Phase 1 完成后最慢吞吐量已经低于 6.5M，就应先优化数据布局和回放范围，
而不是继续叠加 Ruby/DRAM 细节，否则最终没有足够余量。

## 9. 与 ZSim 区分的创新点

“每核推进一段时间，再做一次全局同步”本身是 ZSim 已有思想，不构成创新。
当前 FastSim 的固定 time epoch、coarse interval tail 和未启用 certificate
也不构成可发表的新方法。

新方案可能形成创新的部分是：

1. gem5-source-derived component contracts：从 BaseO3 配置、SLICC 状态机和
   DRAM config 生成轻量事件语义，使快速模拟器的组件语义可追溯到 baseline
   源码，而不是手调 penalty。
2. adaptive causal frontier + exact-by-fallback：前沿由未决 response 的真实
   因果影响决定，不是固定 quantum；以 certificate 提交安全前缀，失败时只
   回放最小 conflict component。
3. sparse transient coalescing：只为 escape line 物化 Ruby transient，并把
   Sequencer alias、permission/data readiness 与 O3 wake-up 直接闭环。
4. causal PMU validation：不仅比较 CPI 和 count，还验证每段 CPI 差异能否由
   O3 stall、Ruby latency、NoC/DRAM queue 的守恒分解解释。

这些目前只是候选贡献。只有在 C4--C32 达到 P99 10%、最慢吞吐量仍高于 5M，
并通过 fixed-epoch、无-certificate、无-source-contract 三组消融后，才可以
作为创新点声明。

## 10. 最终建议

下一步不应继续调 page-walk、L1/L2/LLC 或 branch 的单一常数。优先实施顺序是：

1. response-driven O3 队列/退休闭环；
2. 动态 branch redirect；
3. v5 完整依赖和 register sidecar；
4. Sequencer 16 与 TBE 256 分离、per-line transient/alias；
5. causal certificate 和最小局部回放；
6. 完整 DRAM 地址映射与 FR-FCFS；
7. page-table/ifetch functional sidecar；
8. 最后才考虑受约束的 residual/proxy 校准。

如果禁止扩展 v4 functional trace 字段，Phase 1--3 仍值得做，也应显著改善
现有尾部，但不能诚实承诺任意 workload 的 P99 ≤ 10%。如果允许 v5 继续只
增加 functional facts，而不加入 timing oracle，这条目标是合理且可验证的
研究路线。

## 11. 实施检查点 A：walker、Sequencer 与 response-IQ（2026-08-02）

本检查点已经落地，不再只是方案：

1. x86 DTLB 默认改为一个 active walker；增加
   `dtlb.coalesce_misses`，捕获配置明确设为 `false`。未完成 walk 的同页请求
   作为独立 follower 排队，和 gem5 x86 walker 源码一致。
2. 增加独立的 `ruby.sequencer_max_outstanding=16`。它按完整 response lifetime
   占用，不再把 controller `number_of_TBEs=256` 当作 CPU 请求窗口；L1/L2
   TBE-like lane 和每 CHA 的 LLC lane 仍独立保留 256。
3. 修正 interval core 的 IQ release 下界：非内存 UOP 在 issue 释放，普通
   store 在核心完成释放，load/atomic 至少等核心侧 memory completion。
4. 增加 `core.response_queue_feedback=true` 的跨 interval 持久 IQ calendar：
   shared response 延长 load/atomic 的 IQ residency；64-entry IQ 满时反压
   8-wide dispatch，并把延迟沿 producer edge、completion 和 retirement tail
   传播。普通 store 不错误等待最终 memory response。
5. Sequencer/IQ 日历在 timing transaction 中预演，只在 interval commit 时
   一次性提交，避免 reweave/retry 重复消耗容量。新增每核及汇总 PMU：请求数、
   buffer-full 次数/周期/最大 outstanding，以及 IQ-full 次数/周期/最大占用。
6. 验收脚本现在使用 R/NumPy type-7 线性分位数，分别输出 C4/C8/C16/C32
   P99、最低/P10/中位吞吐量以及独立 pass/fail gate；不会再用 nearest-rank
   P90 代替 P99。

实现涉及 `interval_core.cpp`、`simulator.cpp`、`config.cpp`、公开配置/统计类型、
主程序 JSON 和 gem5 PMU 验证器。单元测试覆盖 memory-IQ lifetime、x86
non-coalescing/可选 coalescing、Sequencer 1-vs-16 容量和 response-driven IQ
反压。

### 11.1 完整 92-case 结果

最终保留的 Q=1024 结果目录为
`results/gem5-source-align-phase1-q1024-full/`；Q=2048 首轮结果保留在
`results/gem5-source-align-phase1-response-iq/` 作为消融。每个核数有相同的
23 个 workload；CPI P99 是该组 23 个 absolute relative error 的线性 P99。

| Cores | 原基线 P99 | Q=2048 P99 | 当前 Q=1024 P99 | Mean abs. | Signed mean | Min UOP/s | CPI/性能 gate |
|---:|---:|---:|---:|---:|---:|---:|:---:|
| 4 | 65.780% | 24.447% | 22.857% | 10.211% | +3.644% | 7.115M | Fail/Pass |
| 8 | 61.975% | 32.449% | 30.681% | 10.361% | +2.774% | 6.690M | Fail/Pass |
| 16 | 50.670% | 35.490% | 28.143% | 11.385% | +5.103% | 5.991M | Fail/Pass |
| 32 | 43.034% | 54.478% | 36.944% | 12.896% | +9.518% | 5.165M | Fail/Pass |

Q=1024 相比 Q=2048 同时改善四组 P99、mean error 和 IQ-full WAPE，因此作为
当前配置保留；但四组 CPI gate 仍未通过。所有 case 高于 5M，最慢 case 都是
`memory_random_mlp`，其中 C32 仅 5.165M，只有约 3.3% 余量。下一层实现必须
避免全 epoch 双重回放，并先优化 hot path。

### 11.2 逐 workload signed CPI error

正数表示 FastSim CPI 高于 gem5，负数表示低于 gem5：

| Workload | C4 | C8 | C16 | C32 |
|---|---:|---:|---:|---:|
| bvc_encoder_base | +5.16% | +4.69% | +6.38% | +10.05% |
| bvc_encoder_heldout | +3.78% | +3.56% | +5.58% | +9.62% |
| cache_L1_mixed | +2.30% | +5.98% | +6.58% | +7.16% |
| cache_L2_mixed | +7.50% | +5.45% | +16.22% | +12.38% |
| coh_readmostly_sparse | +11.34% | +10.13% | +9.56% | +9.37% |
| flink_base | +8.43% | +7.86% | +8.77% | +13.10% |
| flink_heldout | +8.57% | +7.16% | +8.87% | +13.44% |
| fp_alu_dense | -0.34% | -0.36% | -0.38% | -0.42% |
| gofeed_base | +10.82% | +11.17% | +14.21% | +22.03% |
| gofeed_heldout | +8.59% | +7.23% | +8.61% | +14.05% |
| int_alu_dense | -0.11% | -0.12% | -0.12% | -0.14% |
| int_div_serial | +22.60% | +22.58% | +22.54% | +22.55% |
| marine_base | +8.20% | +7.18% | +8.98% | +14.41% |
| marine_heldout | +8.34% | +8.72% | +12.45% | +18.71% |
| memory_random_mlp | +12.72% | +17.46% | +26.68% | +41.00% |
| memory_seq_moderate | -14.54% | -32.97% | -28.56% | -7.12% |
| mysql_base | +8.46% | +6.70% | +7.31% | +11.51% |
| mysql_heldout | +9.24% | +7.08% | +7.40% | +11.52% |
| pytorch_base | -22.93% | -19.36% | -14.32% | -7.45% |
| pytorch_heldout | -20.83% | -17.66% | -12.07% | -6.90% |
| redis_base | +9.70% | +8.57% | +9.56% | +14.38% |
| redis_heldout | +13.60% | +9.52% | +9.92% | +12.46% |
| simd_sse_dense | -16.78% | -16.79% | -16.80% | -16.82% |

response-IQ 把 `pytorch_base` 的误差从
-67.60%/-63.51%/-51.29%/-41.25% 改善到
-22.93%/-19.36%/-14.32%/-7.45%，说明缺失的 response-driven occupancy
确实是原 tail 的主因之一。相反，`memory_random_mlp` 从
-2.82%/+4.37%/+19.90%/+43.54% 恶化到
+12.72%/+17.46%/+26.68%/+41.00%，并成为 C16/C32 最坏 workload。

### 11.3 PMU 结果和当前边界

高流量 functional count 仍稳定：L1D miss、private-L2 miss、CHA lookup 和
branch miss 的 WAPE 分别约为 0.04%--0.06%、0.24%--0.29%、
0.24%--0.29% 和 0.15%--0.17%。但新 timing PMU 尚未对齐：IQ-full count
WAPE 在 C4/C8/C16/C32 分别为 50.90%/52.44%/54.07%/55.42%，pooled signed
error 为 +17.39%/+18.88%/+20.40%/+21.59%。FastSim 的计数是“dispatch slot
被满 IQ 推迟”的 proxy，而 gem5 `rename.IQFullEvents` 按 rename block/partial
progress 更新，不能把两者名称相同视为逐事件等价。

DTLB miss WAPE 约为 14.73%。这不是恢复同页 coalescing 的理由：gem5 源码确实
不 coalesce。真正的问题是 DTLB lookup 仍在 lower-bound producer pass 发生，
response feedback 没有重新安排过早发出的 translation request；v4 也没有
PTE physical path。

本次仍有 62,244/124,361/247,979/493,723 个 corrected-horizon violation，
而 timing certificate 没有启用。`timing_certificate_failures=0` 仍只表示未执行
该检查，不能解释成通过。

### 11.4 下一实施步及停止调参原则

当前闭环只完成了一半：核心 response 会改变后续 issue/dispatch，但 CHA、LLC、
DRAM 和 DTLB 已经按原 lower-bound arrival 修改了状态。高核 random 的正偏和
IQ-full 过量正是这种单向反馈的预期症状。下一提交必须依次完成：

1. 在 transaction 中用 corrected issue time 重放 shared escape request；同时
   快照 private-cache counters/state，稳定次序才提交，失败走 canonical
   conflict-component fallback。
2. 把 response callback 扩到 ROB、LQ、SQ 和 x86 TSO single-store drain，消除
   只靠 interval retirement tail 的剩余误差。
3. 在共享到达时间闭环稳定后实现 RoRaBaCoCh + FR-FCFS DDR4 calendar，优先
   处理 C16/C32 random；在此之前调 `memory_exposure` 或 DRAM 固定 latency 会
   把结构错误吸收到常数中，禁止作为精度提交。
4. compute-only 的 `int_div_serial` 固定 +22.6% 和 SIMD 固定 -16.8% 完全不随
   核数变化，另走 branch redirect 和 v5 完整 deps/register sidecar，不用内存
   参数修补。

下一检查点仍执行同一 92-case gate，并新增要求：C32 最低吞吐量不得低于 5M，
IQ-full WAPE 必须相对本检查点下降，且 random 改善不能以 PyTorch 回退为代价。

性能消融也给出两个明确结论。Q=512 把 C32 random 误差进一步降到 +33.17%，
但吞吐量只有 4.701M，不能接受；这证明 Q 是当前模型的精度/性能超参数，不能在
其他组件调优过程中同时改变。后续统一固定 Q=1024。certified parallel private preview 在数值上与
Sequencer/response-IQ 完全等价，但 C4/C32 random 只有 4.877M/4.864M，故生产
配置保持关闭；相关等价性支持和单元测试保留，供未来增大 batch 或消除 phase
barrier 后重新评估。

### 11.5 吞吐量 hot-path 优化与等价性检查

`compute_timing_feedback()` 原先在每个 core、每个 epoch 都初始化并维护两组
256-entry MSHR 最小堆。当前 MSHR calendar 在 checkpoint 起点为空；若该
interval 的 memory-event 数量上界不超过容量，堆顶必然始终保留一个零槽，
因此容量不可能造成等待。实现现在只在请求数上界超过相应容量时才实例化 L1D
或 L2 MSHR heap。这是当前 checkpoint 语义下的严格等价 fast path，不是关闭
MSHR 限制；超容量 interval 仍走原 calendar。

在 C32 `memory_random_mlp` 上，优化前后的 UOP CPI 均为
5.797294268、IQ-full 均为 8,831,235，cache/DTLB/Sequencer/O3 timing 输出也
一致；`frontier_waits` 会因宿主线程唤醒次序波动，不属于目标机结果。吞吐量由
5.165M 提升到 5.418M UOP/s，约 +4.9%。完整 92-case 表仍保留优化前数据作为
保守 gate；因为数值路径等价，无需用优化后运行覆盖精度结果。普通构建和
ASan/UBSan 测试均通过。

检查点 A 的结论是：第一阶段机制修复和性能 gate 已完成，但精度 gate 没有
完成。当前 C4/C8/C16/C32 P99 仍为
22.857%/30.681%/28.143%/36.944%，不能宣称达到目标。检查点 B 从 11.4 的
corrected-arrival transaction 开始，不再继续调固定 Q 或单一延迟常数。

## 12. ZSim 对照和检查点 B 实施规格（2026-08-03）

### 12.1 ZSim 实际解决了什么

本地审查版本为 `/data00/yinhaolang/Zsim/zsim`，remote 是
`Yang-YiFan/zsim`，commit `1b74dc5eb38eeb1ea740107fd1dd00e36bea716c`。
仓库状态干净且 `git fsck --full` 通过；它是包含 trace-driven 扩展的完整 fork，
不是缺失 bound-weave 源码的裁剪目录。关键实现位于 `contention_sim.cpp`、
`core_recorder.cpp`、`ooo_core_recorder.cpp`、`timing_event.cpp` 和
`ddr_mem.cpp`。

ZSim 的处理不是“每个内存事件全局同步一次”，而是：

1. bound phase 中各核心用 zero-load latency 独立推进到固定 phase 边界，生成
   lower-bound event time，同时已经确定 cache/coherence path；
2. weave phase 把 core/cache-bank/memory-controller 分到多个 domain，各自运行
   timestamp priority queue；只有 event parent-child 跨 domain 时才通过 crossing
   event 同步；
3. OOO recorder 把 issue、dispatch、request、response 连接成 DAG，使争用延迟
   能在 phase 内沿依赖传播；
4. phase 结束时，每核以最后完成事件的 actual time 减 lower-bound time 得到
   skew，并增加该核 `curCycle/gapCycles`，因此慢核在下一 phase 能推进的工作更少。

它的关键近似是 path/timing 分离：weave 可以改变事件的实际先后时间，但通常不
撤销 bound phase 已确定的 tag、replacement 和 coherence path。ZSim 依赖短
phase 内 path-altering interference 很少的经验，并通过计数和人工缩短 phase
控制风险；源码也明确没有对 ROB 等所有内部时钟做完整 rebase。原论文版本还
没有 contention-aware NoC weave。

### 12.2 FastSim 不能只复制 bound-weave

若 FastSim 只实现“每核推进 Q→按 lower-bound 排序→一次共享回放→增加核时钟”，
它既没有新意，也弱于 ZSim，因为目前没有同等完整的 issue/dispatch/response
事件 DAG。区别必须来自下面三项同时成立：

| 语义 | ZSim | 检查点 B |
|---|---|---|
| path-altering 次序 | 假设很少，统计后人工减小 phase | 自动识别同 line/set/controller/bank 风险，只对风险分量修复 |
| 提交规则 | bound path 已提交，weave 修 timing | corrected arrival 的冲突相对序稳定后才提交；失败可恢复 |
| frontier | 固定 phase | 由未决 response、最早可新发请求和风险距离自适应确定 |
| 核反馈 | event DAG 加 phase skew，部分内部状态近似 | gem5 语义的 ROB/IQ/LQ/SQ/store-buffer 持久状态和 response callback |
| 输入 | DBT execution-driven | 离线 functional trace；明确暴露不可辨识项，不使用 timing oracle |

这里的创新点不是 transaction 本身，而是 **certificate-guided sparse repair**：
path-preserving 区域只做一次 timing weave；只有 corrected arrival 真的跨越共享
资源顺序，且可能改变 path 时，才恢复最近 checkpoint 并重放冲突分量。

### 12.3 检查点 B 的分步提交边界

检查点 B 不一次性引入所有复杂度，按以下可独立回退的提交推进：

#### B0：跨核同 line corrected-arrival transaction

- 从每个 epoch 的 memory batch 建立跨核同 cache-line 风险分量；单核重复访问
  不构成跨核 path-order 风险。
- 初始 pass 用 lower-bound arrival，并在 transaction 中快照 LLC、directory、
  DRAM calendar、private-cache set 和所有受影响的 cache counters。
- 用 response-IQ/Sequencer 反馈计算 corrected issue time；第二 pass 必须以该
  corrected time 而不是原 lower-bound time 访问共享系统。
- 若风险 line 内的相对次序稳定，提交第二 pass；若在允许 pass 数内不稳定，
  恢复 checkpoint 并执行已有 canonical path。
- 首版为证明状态恢复和数值闭环，可以在风险 epoch 内回放完整 batch，但必须
  报告风险分量事件数与实际回放事件数，量化 replay amplification；该版本只用于
  pilot，不能在未知性能下直接替换生产配置。

#### B1：真正的 conflict-component sparse repair

- 在 B0 的证据基础上，将 undo log 和 replay 范围缩到受影响 core slice 与同
  line component；随后扩展到可能改变 replacement 的 LLC set component。
- 加入 CHA/output-link/DRAM-channel 资源边，但只有会改变 path 的边触发状态
  重放；仅改变排队时间的 path-preserving 边在事件 calendar 中重算即可。
- 目标是 `replayed_events / batch_memory_events` 在业务负载中保持稀疏，避免
  整个 epoch 双重回放。

#### B2：固定 Q 下的 causal frontier（自适应 Q 后置）

- 当前所有 workload、组件消融和参数对齐统一使用 Q=1024；以最早 unresolved
  response 可能产生的后继 shared request 为风险点，失败分量在同一 Q 内 sparse
  repair 或 canonical fallback。
- Q=512 的 C32 random 结果只作为 Q 敏感性的证据，不作为固定配置，也不用于选择
  其他微架构参数。
- adaptive frontier/Q sensitivity 推迟到 Q=1024 的 P99 和吞吐 gate 通过后，作为
  独立鲁棒性研究，不进入当前优化闭环。

#### B3：持久 ROB/LQ/SQ/TSO 和 gem5 PMU 语义

- 把当前 response-IQ calendar 扩展为 ROB completion/ordered commit、LQ
  commit release、SQ post-commit completion 和 x86 TSO single-store drain。
- rename block、IEW wake-up 和 commit stall 按 gem5 的统计触发点计数，避免把
  “dispatch slot 等待”直接命名为 `rename.IQFullEvents`。
- 完成后再进入 RoRaBaCoCh/FR-FCFS DRAM；否则 DRAM 参数会吸收核心—内存
  单向反馈的结构误差。

### 12.4 B0 验收门槛

1. transaction 开启但未触发风险分量时，CPI、cache/DTLB/O3/Sequencer PMU
   必须 bit-identical，且 replay count 为零；
2. 人工构造的跨核同 line inversion 必须触发 corrected pass，restore 后不能
   重复累计 cache/CHA/DRAM/核心 counter；
3. ASan/UBSan 和守恒检查通过；
4. 先跑 C4/C32 `memory_random_mlp`、`memory_seq_moderate`、PyTorch pilot；
5. 只有 pilot 不让任何 case 低于 5M、random 和 PyTorch 不相互回退，才把
   `interval_reweave_passes` 从 1 改为生产默认值；否则保留机制和 PMU，继续
   做 B1 稀疏化。

### 12.5 B0 实施记录和 pilot 决策

B0 已经落地，不再是只有设计：

- `src/simulator.cpp` 已建立跨核同 line 风险集，并对 LLC、directory、
  DRAM calendar、每核 private cache 和 core counter 做 transaction/restore；
- pass 0 使用 lower-bound arrival，后续 pass 真正使用 response-IQ/
  Sequencer 反馈得到的 corrected issue time；两轮不稳定时恢复后走
  canonical path；
- 增加 candidate/component/replayed/stable/fallback 等可审计 PMU，
  validator 可通过 `--interval-reweave-passes` 显式开启 pilot；
- 无跨核同 line 冲突时，pass=1 与 pass=2 的 CPI、cache/DTLB/O3/
  Sequencer 计数 bit-identical，且 replay 为零；人工冲突用例证明回退
  不会重复累加访存和 cache counter；
- 普通构建、ASan/UBSan 和守恒检查全部通过。

真实 C4/C32 两轮 pilot 保存在
`results/gem5-source-align-phase2-b0-p2-pilot/`。下表的误差是对同一
gem5 baseline 的 absolute CPI error，“A”是 Q1024 生产路径，“B0”是两轮
corrected-arrival pilot。

| Cores | Workload | A error | B0 error | 变化 | A UOP/s | B0 UOP/s | stable/fallback | replay/component |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 4 | memory_random_mlp | 12.724% | 22.086% | +9.363 pp | 7.115M | 6.690M | 1/0 | 31.3x |
| 4 | memory_seq_moderate | 14.543% | 11.844% | -2.698 pp | 9.332M | 9.220M | 1/0 | 17.2x |
| 4 | pytorch_base | 22.931% | 26.130% | +3.199 pp | 9.157M | 4.193M | 225/305 | 21.6x |
| 32 | memory_random_mlp | 41.003% | 118.132% | +77.129 pp | 5.165M | 4.939M | 1/0 | 29.5x |
| 32 | memory_seq_moderate | 7.120% | 24.623% | +17.502 pp | 6.809M | 6.350M | 1/0 | 16.5x |
| 32 | pytorch_base | 7.448% | 7.445% | -0.003 pp | 7.827M | 2.902M | 1/978 | 23.0x |

因此 B0 同时失败精度 gate 和吞吐量 gate，生产配置继续保持
`interval_reweave_passes=1`。这个反例排除了一条看似直接的路：不能对
一个风险 line 触发的整个 epoch 应用全局 corrected timestamp，因为它会
改变未被证书覆盖的 LLC set、controller 和 DRAM 顺序。即使最终回退，
整批 undo/replay 也会使高共享负载失去 5M 性能余量。

### 12.6 B1 当前实施边界

B1 不会继续增加全局 pass 数，而按下列边界替换 B0 的整批路径：

1. **路径证书**：从跨核同 line 开始，向同 LLC set 做有限闭包；只有
   corrected order 跨过可能改变 coherence/replacement path 的边才触发状态
   repair。
2. **timing/path 分离**：CHA link、Sequencer 和 DRAM queue 的排队边可重算
   completion time，但不得顺带重排未入因果闭包的 cache 访问。
3. **稀疏 undo/replay**：transaction 只记录受影响 line/set、core slice 和资源
   calendar；PMU 必须同时报告闭包大小、重放窗口和放大率。
4. **预算和退化路径**：若闭包或预测 replay 放大超过预算，当前 epoch
   安全回退 canonical，记录 deferred-repair PMU，下一个 checkpoint 局部收缩
   frontier；不允许悄悄提交未证明的 corrected path。
5. **接受顺序**：先用上述 6-case pilot 消除 B0 回归并恢复全部 case
   `>=5M`，再跑 C4/C8/C16/C32 完整 92-case gate；在此之前不调整生产
   默认。

### 12.7 B1a 实施与验证记录（2026-08-03）

B1a 已实现第一条可提交的 domain-parallel + sparse-repair 路径，但它只认证
private cache path，不等价于尚未实现的 shared timing certificate。

实现边界如下：

1. 新增与 decode producer 分离的持久 `domain_workers`，避免 domain barrier
   等待 2048-UOP 解码任务；串行 timing-feedback 仍保留为生产路径，因为把
  约 1 ms/epoch 的反馈任务单独并行化在 C32 上反而更慢。
2. 状态证书不再输出单个 epoch/core 布尔值。对每个跨核 write，从 canonical
   batch 末尾反向扫描，找到每个 `(core,private-set component)` 中第一个可能被
   invalidation 改变的访问；只有该 component 的后缀留在 canonical replay，
   其余事件可在 per-core domain 中并行 preview。
3. component 取 L1/L2 set 数较小者的共同低位，这是结构性质，不是经验启发式：
   两级 cache 的 set 数都是 2 的幂；L1 dirty victim 的 L2 writeback、L2 victim
   对 L1 的 inclusive invalidation 都不会跨出该 component。当前配置的 L2 set
   更多，因此它等价于 L1 set；反常的“小 L2”参数组合也仍保持正确。
4. epoch accessor 使用可复用的开放寻址表，L1 reverse-rank table 也跨 epoch
   复用。C32 合成 pilot 的 certificate wall time 从 0.962 s 降至 0.219 s；
   private-preview 覆盖率从 coarse core-prefix 的 32.1% 提高到 96.5%。
5. `sim.domain_min_events`（默认 2048）是收益门槛。boundary 小于该值时不做
   certificate；证书完成后 safe-event 数仍小于该值时也不进入 domain barrier。
   两者都执行已有 canonical fast path，并记录
   `private_preview_bypass_{epochs,events}`。这不会改变目标机语义。

新增可审计统计包括 partial epoch、安全/不安全事件、安全/不安全 core-slice、
bypass、certificate wall time、domain phase wall time，以及 schedule/weave/commit
分阶段 wall time。validator 新增 `--interval-private-preview`、
`--domain-workers` 和 `--domain-min-events`，全部临时转换数据仍位于项目 `tmp/`。

正确性验证：

- 单元测试覆盖无冲突 preview、response-IQ/Sequencer、同 set 冲突后缀修复、
  不同 set 继续 preview，以及串并行 timing-feedback 等价；Release 测试通过。
- ASan/UBSan 测试通过；受当前 ptrace sandbox 限制，LeakSanitizer 需设置
  `ASAN_OPTIONS=detect_leaks=0`，这不是发现了 leak 后关闭检查。
- 7 组随机差分覆盖 C4/C8/C16、0%--100% shared、16--65536 line working set
  和多个 seed；每组的 totals、逐核 counters 和 CHA 均与 canonical
  bit-identical。
- 真实 gem5 functional corpus 的完整 23 workload × C4/C8/C16/C32 共 92 个
  case，与 Q1024 canonical 的 totals、逐核 CPI/PMU 和 CHA 全部
  bit-identical（0 mismatch）。因此 B1a 不改变 CPI/PMU 精度，当前 P99
  误差也不会因该吞吐优化自动下降。

500K UOP/core、30% memory、5% shared、4096-line 合成 paired ablation：

| Cores | Canonical | Adaptive B1a | 本轮 speedup | preview / bypass event |
|---:|---:|---:|---:|---:|
| 4 | 5.037M | 5.039M | 1.000x | 0.0% / 100.0% |
| 8 | 4.697M | 4.623M | 0.984x | 0.0% / 100.0% |
| 16 | 4.332M | 4.302M | 0.993x | 30.8% / 68.2% |
| 32 | 4.000M | 4.311M | 1.078x | 92.1% / 4.0% |

C4/C8 没有进入 domain phase，domain worker 也采用 lazy launch；表中的 C8
约 1.6% 波动是独立 wall-time 单次运行噪声，不是目标机工作量变化。四组的
数值输出都与 paired canonical 完全一致；C16 基本持平，C32 在本轮加速 7.8%。

真实 `memory_random_mlp` 的 adaptive pilot 结果为：

| Cores | Adaptive UOP/s | 旧 Q1024 baseline | bypass event | domain calls | CPI signed error |
|---:|---:|---:|---:|---:|---:|
| 4 | 7.220M | 7.115M | 100.0% | 0 | +12.724% |
| 8 | 6.683M | 6.690M | 99.6% | 1 | +17.46% |
| 16 | 6.114M | 5.991M | 99.6% | 1 | +26.68% |
| 32 | 5.255M | 5.165M | 98.5% | 12 | +41.003% |

该 pilot 恢复了最紧张 random case 的 5M 吞吐 gate，但没有改善其 CPI 误差；
这正是“吞吐优化”和“gem5 精度修复”必须分开验收的例子。

最终 92-case adaptive gate 保存在
`tmp/sparse-preview-validation/real-adaptive-full/`：

| Cores | CPI mean abs. | CPI P99 abs. | Min / median UOP/s | preview / bypass event | canonical mismatch |
|---:|---:|---:|---:|---:|---:|
| 4 | 10.211% | 22.857% | 7.350M / 9.880M | 0.000% / 100.000% | 0/23 |
| 8 | 10.361% | 30.681% | 6.841M / 9.470M | 0.033% / 99.967% | 0/23 |
| 16 | 11.385% | 28.143% | 6.049M / 8.956M | 0.134% / 99.866% | 0/23 |
| 32 | 12.896% | 36.944% | 5.340M / 8.177M | 4.698% / 95.302% | 0/23 |

四组 throughput gate 全部通过，CPI P99 gate 全部失败，且所有
`canonical_fallback_epochs=0`。因此 time-epoch profile 现在默认启用
`interval_private_preview=true`，C++ 通用默认仍为 false；下一步 B1b 是
shared-event corrected-arrival 的最小因果闭包和 timing causal-slack
certificate，而不是扩大本实现的宣传范围。

## 13. 检查点 C：前端源码对齐与 causal-slack（2026-08-03）

本节覆盖 DRAM/topology repair 之后的最新实现和验证；其数值取代第 2 节的
历史基线。运行时仍只读取 functional trace，未使用 gem5 fetch/issue/complete
tick、cache-path label 或 workload 名称。

### 13.1 前端根因和实现

gem5 捕获配置的 `fetchWidth=8`、`fetchBufferSize=64`；源码路径还表明正确预测的
taken branch 会终止当前 fetch group。对 `simd_sse_dense` 的 PC 序列做离线
功能分析后，循环在 `0x402140` 跨 64B block，并通过 taken branch 回到
`0x402130`。简单 8-wide 模型预测约 112,650 个 fetch cycle；加入每次 block
切换的一个 request cycle 和一个空 refill cycle后预测 245,774，而 gem5 实际
fetch span 为 246,461。这个证据把固定 -16.8% 误差定位为 fetch-buffer 语义，
不是 SIMD FU latency。

实现新增：

- `core.fetch_buffer_bytes=64`：以 functional PC 决定 aligned fetch block；
- `core.fetch_buffer_refill_latency=1`：block 切换后保留一个完整空 cycle；
- 正确预测的 taken branch 终止当前 fetch group；
- `branch.mispredict_penalty=2`：对应 IEW→Commit 和 Commit→Fetch 两条 1-cycle
  redirect edge，不再使用 16-cycle 经验常数补偿其他缺失 stall。

定向 C4/C32 pilot 中，`int_div_serial` 从 +22.595%/+22.551% 降至
-0.086%/-0.106%，`simd_sse_dense` 从 -16.782%/-16.822% 降至
-0.258%/-0.306%。这两个修复都来自 gem5 源码和 functional PC，不是按
workload 拟合。

### 13.2 producer-consumer causal slack

response feedback 原先按以下错误规则传播依赖：consumer 继承 producer 的全部
`completion_extra`。若 producer 因响应晚 100 cycle，而 consumer 的 lower-bound
issue 本来就晚 80 cycle，旧规则仍增加 100；正确的剩余因果延迟只有 20。

当前实现改为绝对时间约束：

```text
producer_actual_complete = producer_base_complete + interval_gap
                           + producer_completion_extra
consumer_dependency_extra = max(0,
    producer_actual_complete - consumer_base_issue)
```

dispatch capacity stall 仍单独保留。单元测试构造一个 cold load 和 64-cycle
non-pipelined divide 链，证明当 FU slack 已完全覆盖响应时，添加真实 producer
edge 不再增加总 cycle。这是后续 sparse causal cone 的第一个可复用原语。

### 13.3 最新完整 92-case 结果

结果目录为 `tmp/dependency-slack-full92-v1/`，每个核数包含相同的 23 个
workload。P99 仍按 type-7 linear quantile 独立计算。

| Cores | Mean abs. | P99 abs. | Max abs. | Median abs. | Min / median UOP/s | Gate |
|---:|---:|---:|---:|---:|---:|---|
| 4 | 9.230% | 24.985% | 25.242% | 9.967% | 6.424M / 9.084M | throughput pass；CPI fail |
| 8 | 8.551% | 27.193% | 27.373% | 8.072% | 6.640M / 10.190M | throughput pass；CPI fail |
| 16 | 7.670% | 30.151% | 30.230% | 6.592% | 5.378M / 9.485M | throughput pass；CPI fail |
| 32 | 7.509% | 33.161% | 33.810% | 5.918% | 5.105M / 8.941M | throughput pass；CPI fail |

相对仅有前端修复的上一检查点，mean absolute error 分别改善 1.289、1.433、
1.231、0.896 pp；最慢吞吐量仍全部大于 5M。P99 反而增加 0.477、0.911、
0.659、0.848 pp，因为 PyTorch 的缺失 response/ROB stall 不再被错误的依赖延迟
部分掩盖。结构修复不能因为暴露出另一个缺失组件而撤回；但也不能据此宣称
P99 已改善。

逐 workload signed CPI error（正数表示 FastSim 高估 CPI）：

| Workload | C4 | C8 | C16 | C32 |
|---|---:|---:|---:|---:|
| bvc_encoder_base | +9.967% | +8.072% | +7.427% | +6.319% |
| bvc_encoder_heldout | +7.889% | +5.920% | +5.600% | +3.682% |
| cache_L1_mixed | +4.003% | +4.060% | +4.166% | +5.480% |
| cache_L2_mixed | +12.124% | +12.077% | +12.009% | +3.968% |
| coh_readmostly_sparse | +1.287% | -0.703% | -1.822% | -2.349% |
| flink_base | +11.142% | +9.419% | +8.157% | +7.498% |
| flink_heldout | +10.428% | +8.607% | +7.074% | +4.884% |
| fp_alu_dense | -0.598% | -0.615% | -0.641% | -0.685% |
| gofeed_base | +13.260% | +12.586% | +12.620% | +15.947% |
| gofeed_heldout | +13.030% | +10.788% | +9.053% | +8.154% |
| int_alu_dense | -0.110% | -0.122% | -0.127% | -0.139% |
| int_div_serial | -0.108% | -0.108% | -0.114% | -0.128% |
| marine_base | +9.150% | +6.957% | +5.629% | +4.080% |
| marine_heldout | +8.018% | +5.865% | +4.146% | +0.467% |
| memory_random_mlp | +6.443% | +4.116% | -1.960% | -6.173% |
| memory_seq_moderate | -3.013% | -11.414% | -1.942% | +8.189% |
| mysql_base | +13.935% | +11.143% | +9.693% | +9.229% |
| mysql_heldout | +13.977% | +10.685% | +8.896% | +7.184% |
| pytorch_base | -24.075% | -26.555% | -30.230% | -33.810% |
| pytorch_heldout | -25.242% | -27.373% | -29.869% | -30.861% |
| redis_base | +10.102% | +7.981% | +6.592% | +5.918% |
| redis_heldout | +14.067% | +11.173% | +8.288% | +7.186% |
| simd_sse_dense | -0.328% | -0.333% | -0.346% | -0.377% |

低计数 workload 会让 per-workload PMU P99 失真，因此下表先报告 aggregate
count-WAPE；逐 workload 明细仍保存在 summary JSON 中。

| Cores | Branch miss | L1D miss | Private-L2 miss | CHA lookup | DTLB access | DTLB miss | IQ full |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.165% | 0.043% | 0.239% | 0.239% | 8.770% | 14.734% | 49.786% |
| 8 | 0.170% | 0.048% | 0.266% | 0.266% | 8.799% | 14.737% | 50.577% |
| 16 | 0.160% | 0.052% | 0.283% | 0.283% | 8.805% | 14.741% | 52.007% |
| 32 | 0.153% | 0.059% | 0.290% | 0.290% | 8.843% | 14.735% | 52.942% |

cache/coherence count 已接近，但 DTLB 和 OoO PMU 明确未达标；因此 CPI 尾部
不能再归因于 cache miss count。

### 13.4 B3 和全局 fixed-point 的否决证据

打开现有 `core.response_rob_lsq_feedback=true` 的 C4/C32 pilot：

| Cores | Workload | CPI signed error | UOP/s | 结论 |
|---:|---|---:|---:|---|
| 4 | pytorch_base | +23.797% | 6.549M | 从低估翻为明显高估 |
| 32 | pytorch_base | +6.730% | 6.960M | CPI 接近，但不能跨 workload 泛化 |
| 4 | memory_random_mlp | +6.443% | 4.994M | 已跌破 5M |
| 32 | memory_random_mlp | -6.173% | 3.676M | 严重跌破 5M |
| 4 | gofeed_base | +13.260% | 5.906M | CPI 与 B3-off 相同，只有开销 |
| 32 | gofeed_base | +15.947% | 6.754M | CPI 与 B3-off 相同，只有开销 |

C4 PyTorch 的 IQ-full count 从生产路径的过量值下降到接近 gem5，但
`ROBFullEvents` 被按等待 UOP 重复计数，单核可达约 70 万次，而 gem5 是 rename
进入 blocked/partial-blocked 状态时计一次。当前实现既不满足 PMU 语义，也在
每 UOP 上维护和扫描 dense calendar。

另外做了两次受控反例：

1. B3 + 旧 corrected-arrival causal repair（FCFS 隔离）把 C32 PyTorch/random
   变为 +45.111%/+125.399%，吞吐量 3.381M/2.519M；
2. 固定 canonical path 并要求整 epoch arrival fixed point 的原型，把 C4
   PyTorch/random/GoFeed 变为约 +191%/+191%/+141%，吞吐量仅
   1.95M/1.59M/1.86M。

第二个原型已从源码撤回。根因是 `interval_gap` 已代表提交后的 per-core phase
shift，再把它注入每个响应并对整 epoch 求固定点会重复曝光同一 stall。结论是：
双证书本身不是问题，错误的是证书/修复边界覆盖整个 epoch。

## 14. 下一实施项 C2：sparse causal completion scoreboard

C2 不再扩展 dense B3，也不重新运行整 epoch。它只对实际被长响应改变的
producer、其 functional descendants 和 ROB head suffix 建立局部状态。

### 14.1 状态和算法

每核保留固定容量 ring，而不是逐 cycle 或全 trace 状态：

- 最近 `ROB entries` 个 UOP 的 sequence、base/actual completion、ordered
  retire 和最多四个 producer distance；
- IQ/LQ/SQ 仅保存仍未释放的 sparse response-held entry；
- `committed_phase_delta` 与每个局部节点的 residual delta 分离，保证同一响应
  不会同时进入全局 gap 和节点 delta；
- rename blocked PMU 记录状态转换和不重叠 duration，不按等待 UOP 计数。

response 到达时，以对应 load/atomic 为 seed，按程序序扫描后续 UOP；只有满足
以下任一条件才物化节点：producer residual 大于该 consumer 已有 slack、节点是
当前 ROB head/容量释放边界、或它会产生新的 memory event。未受影响 UOP 继续使用
interval lower bound。这样复杂度目标是 `O(seeds + causal-cone nodes)`，不是
`O(all UOP × ROB/IQ capacity)`。

### 14.2 双证书和 repair boundary

1. **core certificate**：所有未物化 UOP 的 producer residual 已被 lower-bound
   slack 吸收，且不会越过 ROB head、rename capacity 或 ordered commit frontier；
2. **shared-path certificate**：cone 内 corrected memory issue 没有越过同 line、
   LLC set replacement、CHA service 或 DRAM bank command 的非交换前驱；
3. 两证书都通过时，仅提交 per-core residual 和 queue timing，不回放 cache path；
4. 只有第 2 条失败时，从最早 crossing event 向该 shared component 的 resource
   successor 做 sparse repair；超预算时在固定 Q=1024 内显式回退 canonical，
   不通过改变 Q 吸收组件误差。

这与 ZSim 的区别不是“每核先跑一段再同步”——那是已有思想；区别是用
functional dependency/ROB slack 证明绝大多数 response 不需要重放，并把
path-changing repair 限定到证书失败的最小 cone。

### 14.3 实施和验收顺序

1. 先以 shadow mode 统计 seed、cone、ROB-head crossing、shared crossing 和
   replay amplification，不改变 CPI；
2. 加入固定 ring 和 absolute residual，先通过 direct dependency、independent
   MLP、跨 interval producer、ROB wrap-around 和 TSO store 单测；
3. C4/C32 跑 PyTorch、random、seq、GoFeed，要求任何 case 不低于 5M，且
   B3 的 per-UOP `ROBFullEvents` 消失；
4. 再跑 C4/C8/C16/C32 全 92-case。只有四组 P99 都下降且 PMU 不回退，才启用
   生产开关；最终目标仍是四组 P99 ≤ 10%。

C32 random 当前只有 5.105M，新增生产路径的 wall-time 预算约 2.1%。因此 shadow
统计和固定 ring 必须复用现有 per-core feedback loop；任何全 batch 第二 pass、
unordered-map-per-UOP 或 capacity array scan 都不能进入 hot path。

## 15. 检查点 C2：response-driven ROB/LSQ scoreboard（2026-08-03）

### 15.1 已实现的状态闭环

新增实验开关 `core.response_sparse_scoreboard`；它依赖
`core.response_queue_feedback=true`，并与失败的 dense
`core.response_rob_lsq_feedback` 互斥。生产配置仍明确设为 `false`，只有验证器
传入 `--response-sparse-scoreboard` 才开启。

本检查点在已有 interval feedback pass 中加入以下持久状态：

1. 每核一个容量等于 `rob_entries` 的固定 ring，entry 带全局 UOP sequence、
   actual completion 和 ordered retire；tag 使跨 epoch producer 查找不会把
   wrap-around 后的新 entry 误认为旧 producer。
2. ROB admission 由“恰好早一个 ROB window 的 UOP 已退休”约束；load 在 commit
   释放 LQ，store 在 response 后释放 SQ；`needsTSO=true` 时 store 按序单个 drain。
3. response-driven completion 先唤醒 functional producer edge，再经过 ordered
   commit 和 commit width 形成 interval tail；不再同时叠加旧的“每个 completion
   加一次 retirement-tail”近似。
4. producer distance 大于等于一个 ROB window 时，consumer 已成功 admission
   就证明 producer 已退休，因此该边被 slack 吸收，无需保存无界历史；跨 epoch
   且仍在 ROB window 内的边通过 tagged ring 查询。
5. 新增 seed、materialized UOP、absorbed edge、cross-epoch edge、ROB/LQ/SQ
   crossing 统计和 JSON/validator 输出；单元测试覆盖 capacity、ordered commit、
   LQ/SQ/TSO、跨 epoch dependency、ROB wrap-around 与守恒。

实现中发现并修正了 dense B3 过量计时的核心错误。dispatch 被 ROB/IQ 推迟
`D` cycle，并不意味着 issue 也必须额外推迟 `D`：lower-bound dispatch→issue
之间本来可能已有 FU、依赖或 translation slack。现在只传播真正越过 issue
frontier 的 residual：

```text
admitted_issue_floor = actual_dispatch + dispatch_to_issue
dispatch_residual = max(0, admitted_issue_floor - lower_bound_issue)
dependency_residual = max(dispatch_residual,
    producer_actual_complete - consumer_lower_bound_issue)
```

这保证同一 response 不会既作为 dispatch displacement，又在已有 issue slack
内重复收费。C4 PyTorch 的首个原型曾从 -24.075% 过冲到约 +23.9%；加入该
residual 规则后，最终为 +13.106%。

### 15.2 hot-path 实现和吞吐优化

该路径不分配 per-UOP map，也不扫描 ROB 容量数组：ROB 使用单调 cursor；超过
一个 ROB window 的 producer 直接由 admission invariant 吸收；结构默认
`response_retire_exposure=1` 时避免每 UOP 的浮点转换和 `ceil`。

C32 初版仍明显变慢。profile 定位到逻辑上独立的 worker 每个 UOP 都写
`timing.commit_cycle[core]`、slot cursor 和相邻 counter，导致不同核心写同一
cache line 的 false sharing。现在 worker 把这些标量复制到本地，interval 结束
时一次写回。保留的 C32 random trace 上：

- scoreboard 关闭：约 5.31--5.34M UOP/s；
- scoreboard 开启：三次为 5.219M、5.123M、5.140M UOP/s；
- timing-feedback wall time 从初版约 1.21 s 降到约 0.39--0.40 s。

因此结构闭环的增量 host 开销已经较小。完整套件中的 C32 random 单次仍只有
4.991M，低于门槛约 0.18%；独立三次中位数为约 5.14M。验收按最保守的完整
套件单次结果判定失败，不能用重复运行覆盖该失败。

### 15.3 完整 C4--C32、92-case CPI 结果

结果保存在 `tmp/c2-sparse-full92-v1/`。每个核数仍是相同 23 个 workload，
P99 为 type-7 linear quantile。

| Cores | C1 P99 | C2 mean abs. | C2 median abs. | C2 P99 | C2 max | Min UOP/s | Gate |
|---:|---:|---:|---:|---:|---:|---:|---|
| 4 | 24.985% | 7.667% | 8.511% | 13.843% | 13.865% | 6.490M | throughput pass；CPI fail |
| 8 | 27.193% | 6.898% | 6.577% | 15.315% | 16.158% | 6.911M | throughput pass；CPI fail |
| 16 | 30.151% | 5.724% | 5.576% | 12.303% | 12.386% | 5.511M | throughput pass；CPI fail |
| 32 | 33.161% | 4.602% | 4.248% | 14.182% | 15.626% | 4.991M | throughput fail；CPI fail |

相对 C1，四组 P99 分别改善 11.142、11.878、17.848 和 18.979 pp。它证明
response→completion→ordered commit→ROB/LQ/SQ capacity 的闭环是原 PyTorch
尾部误差的主要缺失组件之一，但没有达到最终 P99 10% 目标。

逐 workload signed CPI error（正数表示 FastSim CPI 高于 gem5）：

| Workload | C4 | C8 | C16 | C32 |
|---|---:|---:|---:|---:|
| bvc_encoder_base | +7.655% | +5.764% | +5.576% | +4.486% |
| bvc_encoder_heldout | +6.773% | +4.996% | +4.946% | +3.022% |
| cache_L1_mixed | +3.865% | +3.923% | +4.028% | +5.343% |
| cache_L2_mixed | +12.124% | +12.077% | +12.009% | +3.968% |
| coh_readmostly_sparse | -3.061% | -5.234% | -6.252% | -6.848% |
| flink_base | +9.777% | +8.050% | +6.766% | +6.101% |
| flink_heldout | +9.717% | +7.945% | +6.415% | +4.248% |
| fp_alu_dense | -0.598% | -0.615% | -0.641% | -0.685% |
| gofeed_base | +13.080% | +12.326% | +12.386% | +15.626% |
| gofeed_heldout | +12.897% | +10.612% | +8.833% | +8.002% |
| int_alu_dense | -0.110% | -0.122% | -0.127% | -0.139% |
| int_div_serial | -0.108% | -0.108% | -0.114% | -0.128% |
| marine_base | +8.707% | +6.577% | +5.327% | +3.672% |
| marine_heldout | +7.743% | +5.758% | +3.954% | +0.274% |
| memory_random_mlp | +4.902% | +2.570% | -3.208% | -7.422% |
| memory_seq_moderate | +2.275% | -16.158% | -9.656% | +3.734% |
| mysql_base | +13.765% | +10.984% | +9.489% | +9.062% |
| mysql_heldout | +13.762% | +10.458% | +8.563% | +6.898% |
| pytorch_base | +13.106% | +9.608% | +4.653% | -1.283% |
| pytorch_heldout | +8.511% | +6.009% | +4.205% | -2.201% |
| redis_base | +9.611% | +7.517% | +6.088% | +5.413% |
| redis_heldout | +13.865% | +10.904% | +8.062% | +6.926% |
| simd_sse_dense | -0.328% | -0.333% | -0.346% | -0.377% |

### 15.4 PMU 与 causal-cone 稀疏性审计

本检查点固定了 canonical cache/shared-memory path，只修核心 timing，所以
branch/cache/TLB/CHA functional count 与 C1 相同。下表使用 aggregate count
WAPE；LLC miss 是 FastSim LLC tag miss 对 gem5 Ruby demand miss，不与
`functional_path` proxy 混用。

| Cores | Branch miss | L1D miss | Private-L2 miss | LLC miss | CHA lookup | DTLB access | DTLB miss |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.165% | 0.043% | 0.239% | 2.958% | 0.239% | 8.770% | 14.734% |
| 8 | 0.170% | 0.048% | 0.266% | 5.693% | 0.265% | 8.799% | 14.737% |
| 16 | 0.160% | 0.052% | 0.283% | 9.735% | 0.283% | 8.805% | 14.741% |
| 32 | 0.153% | 0.059% | 0.290% | 14.680% | 0.290% | 8.843% | 14.735% |

LLC miss、DTLB access/miss 的核数相关误差尚未修复，且 C2 没有 shared-path
replay，因此不能因 CPI 变好就宣称 PMU 闭环完成。

全套统计中，每组约 71.86%--72.00% 的退休 UOP 被计为 materialized，主要来自
ordered-retire/ROB crossing；PyTorch 可接近 98.5%。这里的 materialized 是
“actual completion/retire 不等于 lower bound”的审计标签，状态本身仍放在固定
ROB ring，并未为每个 UOP 动态分配对象。但这个比例也说明当前实现仍不是
`O(seeds + small cone)` 的最终 sparse engine：它复用并遍历现有逐-UOP feedback
pass，尚未跳过 certificate 已证明无关的连续区间。

### 15.5 验证结论与下一边界

Release 单元测试、守恒检查和完整 92-case 都已通过执行；92 个 case 的 UOP、
memory-event 和 private/escape partition mismatch 均为 0。C2 暂不进入生产默认，
原因是四组 CPI P99 仍失败，且 C32 最慢单次略低于 5M。

新的 tail 已从“PyTorch 全核数严重低估”转移为多类问题：

1. C4/C32 `gofeed_base` 和 C4 Redis/MySQL/cache-L2 仍为正偏，C2 对它们改变很小，
   说明它们不应继续用 ROB response exposure 修补；需要 branch/front-end、完整
   dependency/register pressure 和 shared-controller latency 的归因消融。
2. C8/C16 `memory_seq_moderate` 出现 -16.158%/-9.656% 的核数相关负偏，而 C4/C32
   方向不同，指向 DRAM/controller arrival order、MLP 与 topology timing；需要
   shared-path certificate 后再修 RoRaBaCoCh/FR-FCFS，不能调固定 miss penalty。
3. LLC miss WAPE 从 C4 的 2.958% 增至 C32 的 14.680%，DTLB miss 约 14.7%；下一
   精度提交必须让这些 PMU 与 CPI 同时改善，不能只做核心 residual 校准。

该处提出的下一项是在 C2 ring 上加入 activity bitmap/segment certificate：跳过
没有 seed、没有 capacity crossing、且 producer residual 已被 slack 吸收的连续
UOP；只有 corrected memory issue 越过 line/set/CHA/DRAM predecessor 时，才触发
shared sparse repair。接受门槛保持不变：C4/C8/C16/C32 各自 P99 不超过 10%，
92 个 case 每个都不低于 5M UOP/s，且上述 PMU 不回退。第 19 节已完成保守的
checkpoint-segment 第一阶段；mixed checkpoint 的内部 segment 和 shared predecessor
repair 仍是后续项。

## 16. 检查点 C3：domain-parallel + certificate sparse repair（2026-08-03）

### 16.1 设计目标、Q 和 worker 契约

C3 不把 ZSim 的“每核跑固定 quantum 后统一 barrier”直接复制为精度模型。当前
跨边界的 response、同 line 顺序、容量释放和 DRAM predecessor 尚未完全闭环，
所以改变 quantum 会改变可见 MLP、事件次序和 CPI。对当前模型，Q 明确是精度
超参数；本轮组件优化、参数对齐和最终验收统一固定
`sim.interval_max_cycles=1024`。以下证书设计仍用于减少重放和限定误差传播范围：

1. 每核先生成一个带进入/退出状态摘要的 segment，摘要包含 ROB/LQ/SQ/TSO
   frontier、未决 response、branch/translation 边界和 memory issue 范围；
2. 私有 cache/core segment 可按核并行，LLC/CHA、目录 shard 和 DRAM channel 按
   明确 owner domain 推进；
3. 全局阶段只做确定性 C-way merge，并验证 state certificate 与 order/timing
   certificate；没有跨域非交换边的 segment 直接提交；
4. 证书失败时，仅沿 line/set/CHA/DRAM predecessor 与 response→ROB/LSQ 的
   causal cone 做 sparse repair，而不是重放整个窗口；
5. 若 repair cone 超预算，则在同一 Q=1024 内走 canonical fallback；adaptive
   frontier 留到当前组件优化完成后的独立实验。

若未来所有跨边 causal edge 都能通过证书、局部延长或规范 fallback 闭合，Q 可以
退化为只影响宿主 batching 的参数；这是长期目标，不是当前调优的前提，也不设置近期
跨 Q target-state hash gate。`worker` 则仍不是“一个模拟核对应一个 worker”的
微架构参数。当前 trace producer 仍是一核一线程；`sim.domain_workers` 是复用的
宿主执行池，负责已证明独立的 per-core feedback 和 DRAM-channel task。固定 Q=1024
时，改变 worker 数只能影响墙钟时间，不能影响目标结果。

与 ZSim 的关键区别是：同步边界由双证书和 causal repair 决定，而非由固定 quantum
定义；并行域是显式资源 owner，提交顺序是确定性的；工作量目标是
`O(segment summaries + violated causal cone)`，而不是每个 quantum 全局串行重放。
这同时保留 functional trace 的可辨识性边界：trace 不含的错误路径 UOP、精确寄存器
依赖和 OS/page-walk 内容只能通过增强 trace 或受限 proxy 解决，不能由 barrier 技巧
凭空恢复。

### 16.2 C3-A：严格等价 hot-path 实施

第一批先降低 C2 的宿主开销，不改变任何微架构参数或目标事件：

1. Release 构建默认启用 CMake IPO/LTO；不支持 IPO 的编译器自动给出 warning，
   `-DFASTSIM_ENABLE_IPO=OFF` 可明确关闭。关闭路径已在
   `tmp/c3-a-build-noipo/` 完成编译和结果等价检查。
2. `run_interval_weave()` 的 `BatchPending` 与 per-core `MemoryReplay` storage 跨
   epoch 保留容量，只清理本 epoch 实际接受的 event slot，去掉 resident buffer 的
   重复全量初始化和分配。
3. time-epoch 利用每核 `delta_q16` 单调性，以 `upper_bound` 直接定位 horizon 内
   最后一个 memory event；若它属于尚未达到 retire horizon 的 UOP，则按原语义扩展
   accepted UOP prefix。
4. 每核 memory stream 已按 `(issue, ordinal)` 有序，不再把全部 E 个事件收集后做
   `O(E log E)` 全局 sort；改用保持 `(issue, core, ordinal)` canonical key 的
   C-way heap merge，复杂度为 `O(E log C)`。
5. FR-FCFS 的 8 个 DRAM channel 只修改各自的 bank 和 data-bus 状态。当前复用
   domain worker 池并行足够大的 channel batch；完成后仍按 channel 编号拼接
   `service_order`，因此与旧串行实现 bit-equivalent。小 batch 保持串行，避免线程池
   barrier 反而放大开销。

新增单元测试把同一 16 核 FR-FCFS case 的 `domain_workers` 从 1 改为 8，并逐核、
逐 CHA 检查 cycles、memory penalty、LLC/DRAM PMU、fixed-point pass 和 reorder
统计完全一致。Release 与 ThreadSanitizer 测试均通过。

### 16.3 等价性和吞吐量结果

参考 trace 为 `tmp/c2-bench-trace-c32-random/manifest.txt`。每个版本交错/重复运行
5 次；下表给中位数。`old LTO` 是改动前保存的 `build-lto/fastsim`，所以 A2 对比
隔离了 source hot-path 变化，而不是把 LTO 收益混入算法收益。

| C32 random | Wall time | UOP/s | schedule/batch | FR-FCFS wall |
|---|---:|---:|---:|---:|
| old LTO | 3.529 s | 5.052M | 1.088 s | 0.793 s |
| A2：scratch + upper-bound + C-way merge | 2.997 s | 5.948M | 0.646 s | 0.771 s |
| A3：再加 channel-domain parallel | 2.978 s | 5.986M | 0.650 s | 0.746 s |

A2 的纯 source speedup 为 17.72%，其中 schedule/batch wall time 减少 40.61%；
A3 相对 A2 仅再提升 0.65%，FR-FCFS 区段减少 3.19%。这说明 channel 并行是无损的，
但当前真正的大头仍在 shared replay、scoreboard feedback 和频繁 epoch fixed-point，
不能把增加 worker 当作主要扩展性方案。

剔除 `wall_time_seconds`、throughput、host phase nanoseconds 和非确定性的
`frontier_waits` 后，C32 old/A2/A3/no-IPO 的完整 JSON hash 均为
`593785a9ddc8ce2819a6eff26cc95f1f99982e5b8932a6a0eb8c319e132514d3`；C4
PyTorch 的 old/A2/A3 hash 均为
`b7069268fdb9445d6046170acd4d0af917089bce2e12fb283fc517b431934713`。因此本检查点
没有改变 CPI、PMU、事件次序、证书或 repair 结果。

这里不能替代完整 92-case gate：当前精度结果仍是 15.3 的 C2 数据，C3-A 只是等价
加速。保留的 C32 random 已超过 5M，但仍需用最终二进制重跑 C4/C8/C16/C32 全套，
确认最慢 workload 也不低于 5M。

### 16.4 下一实施边界

C3-B1 已按第 19 节实现 checkpoint-segment activity bit 和进入/退出证书，使没有
response seed、没有 ROB/LQ/SQ crossing、没有活跃 producer crossing 的整段 UOP
进入 reduced feedback；mixed checkpoint 的内部 bitmap 尚未实现。C3-B 与当前
C2/C3-A 的 target-state hash 比较使用同一个 Q=1024；
同时继续验证不同 `domain_workers` 得到完全相同的目标状态。Q=512/2048 的敏感性
实验推迟到其他组件在 Q=1024 下完成优化、且 P99/吞吐 gate 通过以后单独进行；其
结果用于说明模型鲁棒性，不用于反向选择本轮微架构参数。

C3-C 再把 LLC set/CHA/DRAM owner state 做成显式 domain snapshot，允许各核/域并行
提出一段时间的 tentative result；全局只合并证书摘要，并对失败的 conflict component
做 sparse repair。只有在 C3-B/C 的 target-state hash、C4--C32 92-case CPI/PMU 和
5M throughput gate 同时通过后，才改变生产默认。CPI 精度的下一优先级仍是 15.5
列出的 GoFeed/Redis/MySQL 前端与依赖问题、memory-seq DRAM arrival/MLP 问题以及
LLC miss/DTLB 的核数相关误差，不能用本轮无损优化冒充精度提升。

## 17. 检查点 S1：线程/硬件核状态拆分与非阻塞 syscall 边界（2026-08-04）

### 17.1 本阶段为什么先拆状态

旧实现的 `CoreState` 同时拥有 `TraceSource`、branch predictor、interval/OoO
状态和核级计数。这在 `T=C`、永不迁移时能运行，但把“软件线程的顺序流”和“硬件
核的资源状态”错误地做成同一个对象；后续一旦加入 oversubscription、block/wakeup
或 migration，就无法回答 trace 游标跟谁走、ROB 是否排空、predictor/TLB/cache
是否保留，以及计数归线程还是归核。

S1 已把它拆成两个明确所有者：

| 状态 | 当前所有者 | 后续扩展含义 |
|---|---|---|
| thread ID、address-space ID、trace 游标、生命周期、线程功能计数 | `ThreadState` | block 后保留，migration 时随线程走 |
| branch predictor、interval/OoO/ROB 近似、核级 PMU、核时间 | `HardwareCoreState` | context switch 后仍属于物理核 |
| private TLB/cache 与 response calendar | core-indexed hardware resource | 下一阶段并入统一 core ownership，不随线程搬迁 |
| `thread -> core` 关系 | `ThreadTraceBinding` / resident binding | 当前静态，后续由 scheduler 改写 |

当前严格收敛为 `1 <= T <= C`，且映射必须 injective：一个线程静态占一个核，允许
显式放在任意空闲核；兼容 manifest 仍以 dense stream `t -> core t` 映射。未绑定核
从初始化起就是 idle/finished，不创建 trace worker。host trace worker 数等于活跃线程
数而非配置核数。此处没有 time slice、迁移、context switch cost 或调度竞争；这些
没有被折进 Q、worker 数或其他框架参数。

输出 schema 升为 `fastsim-stats-v4`，新增 `threads[]`，同时保留 `cores[]`。在本阶段
静态一线程一核，所以线程 cycles 等于其绑定核 cycles；这个等式不是未来迁移模型的
接口假设，迁移后会由 residency interval 累加线程运行时间。

### 17.2 为什么不能从 `is_serialize` 猜 syscall

gem5 的 `StaticInst::isSerializing()` 合并了 `IsSerializing`、
`IsSerializeBefore` 和 `IsSerializeAfter`；fence、CPUID 和 syscall 都可能命中。
因此仅看到 `is_serialize=1` 不能知道这是 syscall，更不能知道 syscall number、参数、
是否阻塞和唤醒对象。S1 不再把所有 serialize 行误报为 syscall。

canonical binary 升为 v5，64B record 大小不变。gem5 OpClass 都非负，v5 保留
`op_class=-1` 作为显式 syscall functional marker，并在 header feature bit 中声明；
JSONL 的正式输入字段为 `is_syscall`。现有 TaoTrace `instr_type=7 (SYS)` 作为兼容
入口。v2/v3/v4 继续可读。syscall number/arguments 没有塞入每 UOP hot stream，后续
用按 functional ordinal 锚定的稀疏 thread-event sidecar 承载。

### 17.3 `T<C`、不考虑调度开销时的 syscall 模型

本阶段只精确表达“非阻塞 gem5-SE syscall 是一个 serializing system UOP”，不伪造
Linux 内核指令流。对 syscall UOP `s`：

```text
rename(s) >= retire_actual(s-1) + 1
execute_latency(s) = core.system_latency + syscall.service_latency
fetch(s+1) >= retire(s) + syscall.restart_latency
```

其中 `system_units/system_latency` 是微架构资源参数；
`syscall.service_latency` 是 baseline 明确定义的额外 SE ABI 服务时间，默认 0，禁止
拿当前 workload 的 gem5 CPI 拟合一个通用常数；`syscall.restart_latency` 默认 1。
scalar path 只保留 service/restart 的串行兼容语义，精度验收仍要求 interval core。

关键闭环不是仅在 lower-bound pass 加一次 barrier。若 syscall 前的 load 因 LLC/DRAM
response 从 cycle 20 延迟到 cycle 100，错误做法会让 syscall 在 cycle 21 提前执行，
只在 ROB 中等到 100 再提交。现在 `ChunkUopBound` 保留 serialize-before/after 边，
response feedback 使用 `retire_actual(s-1)` 重新约束 syscall issue，并把 syscall 的
retire displacement 传给更年轻 UOP。这样 drain 边能跨过 response→ordered-retire
闭环，而不是只服从最初的 OoO lower bound。

PMU/审计新增 `serializing_uops`、`syscall_uops`、`syscall_drain_cycles`、
`syscall_service_cycles` 和 `syscall_restart_cycles`。后三项是结构原始量，可能与其他
pipeline slack 重叠，不能直接相加成 CPI attribution。

### 17.4 明确未做的部分

以下行为在 S1 中仍然不建模，也没有用固定 penalty 掩盖：

1. futex wait/wake、nanosleep、阻塞 I/O 的 block duration 和 wakeup causal edge；
2. clone/exit/join 后的 runnable-set 变化；
3. `T>C` 的 run queue、time slice、migration 和真正的 context switch；
4. migration 时 TLB/address-space tag、private cache warm state、predictor policy；
5. syscall number/argument 驱动的类别差异，以及 guest full-system kernel 指令/访存。

gem5 SE baseline 没有 guest kernel instruction stream，所以第 5 项不能靠 FastSim
“补几百条内核 UOP”恢复。若 baseline 是 gem5 FS，只有用户态 committed functional
trace 也不足以比较完整 syscall CPI；必须额外采集内核流或明确改用事件级 OS 模型。

### 17.5 实现验证和下一步

新增回归覆盖：v5 syscall JSONL/binary round-trip；syscall 等待旧 UOP retirement、
system-FU/service latency 和 restart；4 核仅绑定 2 个线程到 core 1/core 3，检查空闲
核、worker 数、thread/core 双口径计数；manifest 条目少于配置核数。Release 单元
测试以及 ASan/UBSan、ThreadSanitizer 全部通过。已有无 syscall 且 `T=C` 的输入保持
原线程到核映射；输出只增加 schema 和审计字段。

还用保留的 v4 C4 PyTorch trace 做了兼容回放：3,181,976 UOP 的 pre-S1/post-S1
目标字段（sum cycles、UOP、memory、branch/LLC/DTLB miss 与逐 CHA 请求/失效/远端
supply）完全一致，新二进制该次为 9.89M UOP/s。只取 core0、配置仍为 C4 的
`T=1<C=4` 回放产生 `[795494,0,0,0]` UOP、`[1105092,0,0,0]` cycles，且
`trace_worker_threads=1`，该次为 6.84M UOP/s。它们是功能/兼容 smoke，不替代
C4--C32 92-case 精度与最慢 workload 吞吐 gate。

下一阶段先定义稀疏 `thread-events` sidecar，最小事件为
`SYSCALL_ENTER/RETURN`、`BLOCK(resource)`、`WAKE(resource)`、`CREATE/EXIT`，全部按
`(thread_id, retired_ordinal)` 锚定而不是按宿主时间。随后增加 scheduler interface，
但仍先做 `T<=C` 的 block/wakeup；只有其 target-state hash 与 worker 数无关后，
再开放 oversubscription、time slice 和 migration cost。这个顺序避免在状态所有权和
阻塞因果边尚未稳定时直接加入一个难以验证的 Linux 调度 proxy。

## 18. 跨 ISA 输入边界与 canonical functional IR（2026-08-04）

### 18.1 gem5、DynamoRIO、Sniper、ZSim 的能力边界

“支持多个目标 ISA”“能在不同宿主机分析 trace”和“把架构指令降成 timing UOP”是
三种不同能力。四个系统都必须在某一层识别目标 ISA，但并不是都自建 decoder，也
不是都生成 timing UOP：

| 系统 | 跨 ISA 能力 | 功能执行/trace 来源 | ISA decode | 自有 timing UOP | 同一 trace 直接跨 ISA |
|---|---|---|---|---|---|
| gem5 | 强；源码树包含 x86、Arm、RISC-V、MIPS、Power、SPARC 等目标，具体 SE/FS 完整度不同 | gem5 自己取指并执行目标程序；非 KVM CPU 可与宿主 ISA 解耦 | 每种 ISA 有独立描述与 decoder，大量代码由 gem5 ISA DSL 生成 | 部分；统一执行对象是 `StaticInst`，复杂指令可展开为 micro-op，并非所有 ISA/指令都先降成同一种 UOP | 不能；不同 ISA 需要对应二进制重新执行 |
| DynamoRIO / drmemtrace | 成熟路径覆盖 x86-32/64、ARM32、AArch64；当前 RISC-V trace 路径仍不完整 | 在匹配目标 ISA 的机器上原生执行和插桩，离线 trace 可在另一宿主上用目标 decoder 分析 | DynamoRIO 自有多 ISA instruction IR/decoder | 否；原始 drmemtrace 是动态指令/访存记录，不是 ROB、端口意义的 timing UOP | 原始编码不能；`DR_ISA_REGDEPS` 可形成有损的 ISA-neutral 依赖流 |
| Sniper | 支持 x86、Arm、RISC-V 前端，但各路径成熟度和已验证微架构不同 | x86 常用 Pin/SIFT，Arm 可用 DynamoRIO，RISC-V 使用 Spike/rv8 等前端 | 统一 decoder 接口包装 XED、Capstone、rv8 等，而非从零实现所有机器码 decoder | 是；再降成 Sniper `MicroOp`，由 Nehalem、Cortex、BOOM 等 core model 指定端口/延迟 | 不能；每个 ISA 仍需单独生成带对应编码的 trace |
| ZSim | 原版基本是 x86/Nehalem 路线，不具备成熟多 ISA 后端 | Pin 原生执行并按 BBL 插桩 | 使用 Pin `INS`/寄存器信息和 XED opcode，外加自建规则 decoder | 是；`DynUop` 直接带 latency、decode cycle 和 port mask，且与 Nehalem 风格紧耦合 | 不能 |

表中的 micro-op 都是模拟器为 timing 建模定义的 IR，不等于 Intel、Arm 等厂商未公开
的真实硬件 UOP。`DR_ISA_REGDEPS` 也不是 timing UOP：它保留类别、虚拟寄存器依赖
和操作宽度，但删除原 opcode、立即数和大量精确语义，只适合可移植分析或粗粒度模型。

### 18.2 当前正式决策：运行时只接受 gem5 语义的 functional IR

当前阶段可以、也应该暂不实现 ISA decoder。FastSim 的规范运行时输入定义为
**canonical FST functional IR v5**；当前唯一经过 CPI/PMU 精度验证的 producer 是
gem5 functional exporter，所以项目文档和命令行仍简称它为“gem5 functional trace”。
这里要区分格式语义和数据来源：

```text
当前精度路径

gem5 functional execution/export
        -> gem5 JSONL / aligned Parquet
        -> vectorized offline conversion
        -> canonical FST v5
        -> FastSim interval + memory/coherence timing engine
```

FastSim hot path 从 `TraceRecord` 直接读取已经确定的 UOP 边界、gem5 OpClass、最多四条
producer distance、分支实际结果、虚拟页 token、物理地址和访存属性。它不读取机器码，
不执行 ISA decode，也不在运行时重新决定 macro-instruction 应展开成几个 UOP。这样做有
三个直接收益：

1. 当前精度优化只对齐 gem5 O3/ROB/LSQ、cache/coherence/DRAM 的 timing 语义，不被
   新 decoder 的误差混入；
2. decode/lowering 的一次性成本留在离线转换阶段，不占 5M UOP/s 的运行时预算；
3. v5 的 64-byte 动态记录和现有批量读取路径保持不变，本阶段不为未来能力扩展 hot record。

因此，本检查点不增加 trace ISA header、raw instruction bytes、静态指令字典或 runtime
decoder。这些都不是继续修复当前 gem5 CPI P99 的前置条件。

### 18.3 drmemtrace 的未来接入方式

drmemtrace 可以接入，但准确说法应是“离线转换成 FastSim canonical functional IR”，
而不是“无损转换成 gem5 生成的 trace”。运行时保持单一入口：

```text
未来 DR 路径（不进入当前实现范围）

native drmemtrace
        -> offline target-ISA decode
        -> architectural semantic instruction
        -> gem5-compatible UOP/OpClass lowering
        -> dependency-distance construction
        -> address/branch/syscall/thread-event normalization
        -> canonical FST v5（必要时由后续版本增加 sidecar）
        -> 同一个 FastSim timing engine
```

离线 adapter 至少必须解决以下差异：

| canonical 字段/语义 | 原始 drmemtrace 能否直接提供 | 离线 adapter 要做什么 |
|---|---|---|
| PC、指令长度、动态读写地址 | 基本可以 | 规范化记录并处理一条指令多个 memory operand |
| 分支类型和实际后继 | 可以由指令记录、分支记录和下一 PC 重建 | 输出完整 taken/target/next-PC contract，并验证 thread 边界 |
| gem5 UOP 边界与 `op_class` | 不能 | 用目标 ISA decoder 和明确版本的 lowering profile 生成；禁止猜固定一指令一 UOP |
| producer distances/classes | raw encoding 可经寄存器读写分析构建；`REGDEPS` 已给有损虚拟依赖 | 做每线程 dependency rename；处理 flags、partial register、隐式 operand 和跨 UOP 临时依赖 |
| atomic、fence、serialize 语义 | 需要精确 opcode/operand 信息 | 解码并映射 ordering/scope；仅靠 `REGDEPS` 类别不足以做精确一致性建模 |
| 虚拟地址 | 通常可以 | 生成稳定 virtual-page token |
| 物理地址 | 默认通常没有 | 从采集侧增加 VA->PA 信息或提供可验证的离线映射；否则只能使用 non-strict exploratory mode |
| syscall、block/wake、线程生命周期 | marker 完整度依采集选项而定 | 转成按 `(thread_id, retired_ordinal)` 锚定的 thread-event sidecar |

其中物理地址是硬边界：没有 `paddr` 的 DR trace 可以在
`trace.strict_physical_address=false` 下做虚拟地址近似实验，但其 LLC/CHA/coherence/
DRAM PMU 不得宣称与 gem5 physical baseline 等价。`DR_ISA_REGDEPS` 可以减少跨 ISA
dependency adapter 的工作量，却不足以恢复精确 opcode、原子/栅栏语义和目标微架构的
UOP 分解，因此不作为 P99 10% 精度路径的默认输入。

### 18.4 实施顺序和验收门槛

当前优先级不因跨 ISA 讨论而改变：继续用 gem5 functional trace 完成 C4--C32 的核心
response closure、shared-event sparse repair、CPI P99 和最慢 workload 5M UOP/s gate。
只有该基线稳定后才实现 DR adapter。建议顺序为：

1. 冻结 canonical v5 和 gem5 exporter 的语义测试，当前 simulator 不加 decoder；
2. 用相同 x86 程序分别采集 gem5 functional trace 和 native drmemtrace，建立离线
   instruction/UOP/dependency differential test；
3. 先完成 x86 DR-to-FST converter，再验证每 workload 的 UOP 数、OpClass、依赖、
   branch、memory-size、atomic/fence 和 syscall marker；
4. 在具有可信物理地址映射时复跑 cache/LLC/CHA/DTLB PMU；没有物理地址时只报告
   portable/exploratory 结果；
5. 最后才把 decoder/lowering 抽象扩展到 Arm、RISC-V。不同 ISA 必须重新编译并采集
   各自的 functional trace；不能把一条 x86 动态 trace 当作 Arm/RISC-V 程序执行流。

DR adapter 只有在同程序双源 differential gate 通过后，才可标记为 precision-supported。
adapter 的吞吐不计入 FastSim 仿真 UOP/s，但必须缓存静态 decode/lowering 结果，避免按
动态指令重复解码。任何未来 runtime direct-DR reader 都是独立优化项，不能改变 canonical
FST 的模拟结果。

## 19. 检查点 C3-B1：固定 Q=1024 的 response activity 证书（2026-08-04）

### 19.1 先冻结精度基线，再做无损优化

本检查点没有重采 gem5 数据，也没有改变 Q。最终实现前先用 C3-A 二进制重跑完整
23 workload × C4/C8/C16/C32，结果位于
`tmp/c3-a-full92-q1024-v1/`。CPI P99 为
13.843% / 15.315% / 12.303% / 14.182%，最慢 workload 均为
`W_v28_memory_random_mlp`，对应吞吐为
6.904M / 7.488M / 6.056M / 6.000M UOP/s。92 个 case 的 UOP、memory event 和
private/escape partition 守恒全部通过。与 C2 的公共 target 字段逐 case 比较完全
相同；因此它是固定 Q=1024 的正式输入/精度参考，而不是用本轮优化后的结果反向选参。

这里再次区分三个粒度：

| 参数 | 本轮角色 | 是否允许改变 CPI/PMU |
|---|---|---:|
| `Q=sim.interval_max_cycles=1024` | 当前精度超参数和 epoch 边界 | 当前允许；所以本轮固定不动 |
| `K=sim.chunk_instructions` | trace decode/transport microbatch | 不允许；必须 target-state 等价 |
| `sim.domain_workers`、64-UOP crossover | 宿主执行策略 | 不允许；只能改变墙钟时间 |

### 19.2 Transport K 的重新选择

`simulate` 现在也支持 `--chunk-instructions`，可在同一 FST 上直接做 K 的严格等价
A/B。对保留的 C32 random trace 各运行三次：

| K | 三次 UOP/s | 中位数 | 相对 K=2048 |
|---:|---:|---:|---:|
| 2048 | 6.008M / 6.005M / 6.005M | 6.005M | baseline |
| 4096 | 6.181M / 6.219M / 6.184M | 6.184M | +2.98% |
| 8192 | 6.179M / 6.142M / 6.141M | 6.142M | +2.28% |

K=4096 的逐核 cycles、cache/LLC/CHA/DTLB/branch PMU、response scoreboard、事件
顺序和证书结果与 K=2048 完全相同；变化只有 chunk/lookahead 数和宿主计时。因此
生产配置改为 K=4096。这个选择是当前主机上的性能 crossover，不是数据集校准参数，
也不参与微架构泛化或 CPI 寻优。

### 19.3 Checkpoint-segment activity certificate

新增 `core.response_activity_certificate`。C3-B1 先实现 checkpoint 内每核 accepted
prefix 的一个保守 activity bit，不声称已经完成任意内部子区间的 bitmap。候选必须
至少 64 UOP、没有 memory UOP，也没有 serialize-before/after 边。64 只是避免复制
固定 ROB/IQ 状态不偿失的宿主 crossover，不进入目标模型。

候选在 tentative 的 IQ heap、sequence-tagged ROB ring、dispatch/commit calendar 和
审计计数上执行 reduced transition。每个 UOP 同时验证：

1. 恰好一个 ROB window 更老的 entry 已在该 UOP 的 base dispatch 前退休；
2. dispatch width 和 IQ 最早释放时刻不会把 admitted dispatch 推过 lower bound；
3. `dispatch_to_issue` 边已被 base issue 吸收；每条跨 checkpoint 或区间内 producer
   completion 都不晚于 consumer base issue；
4. base completion 加 `execute_to_commit` 不会推迟 base retire，commit width calendar
   也不产生额外周期；
5. 出口 ROB sequence/completion/retire、IQ release、cursor、occupancy 和 absorbed/
   cross-epoch dependency 计数都能按原规则重建。

任一检查失败时，tentative 容器直接丢弃，完整 sparse response loop 从未修改的入口
状态重新执行；没有 undo、近似 penalty 或部分提交。证书成功时仍写回未来 checkpoint
需要的 IQ/ROB/dispatch/commit 出口状态，所以“跳过详细 response 计算”不等于丢弃
ROB/依赖历史。新增审计为 candidate、certified segment、certified UOP 和 fallback
segment 四项。

这一阶段与 ZSim 固定 quantum 后无条件合并不同：固定 Q 仍只是当前提交边界，是否走
reduced loop 由 target-state entry/exit certificate 决定；失败恢复同一个规范路径。
当前只消除了确定无 response 活动的 per-core 工作，尚未实现 LLC/CHA/DRAM owner
snapshot 或内部混合 segment 的 sparse repair。

### 19.4 等价性、吞吐量和覆盖率

单元回归覆盖纯计算成功证书、serialize 强制 fallback、完整 sparse dependency/queue
计数等价和配置依赖关系。Release 测试通过；ASan/UBSan 在
`ASAN_OPTIONS=detect_leaks=0` 下通过。当前容器由 ptrace 托管，LeakSanitizer 本身不能
运行，因此没有把该环境限制写成 leak 检测通过。

32 核、每核 500k UOP 的无 memory 合成流中，证书覆盖 15,999,944 UOP；三次中位
吞吐从 23.254M 提升到 26.143M UOP/s（+12.42%），target JSON 除开关、证书审计和
宿主计时外完全相同。相同 32 核流把 `domain_workers` 从 1 改为 8 时，吞吐从
11.633M 提升到 20.750M UOP/s；target state 仍完全相同，说明 worker 契约没有被
证书路径破坏。

最终完整结果位于 `tmp/c3-b-full92-q1024-v1/`：

| Cores | CPI P99 | 最低 UOP/s | 吞吐 gate | activity certified segment/UOP/fallback |
|---:|---:|---:|:---:|---:|
| 4 | 13.843% | 6.911M | PASS | 4,069 / 10,679,310 / 2 |
| 8 | 15.315% | 7.444M | PASS | 8,139 / 21,359,595 / 0 |
| 16 | 12.303% | 6.030M | PASS | 16,274 / 42,718,788 / 0 |
| 32 | 14.182% | 5.928M | PASS | 32,313 / 83,730,481 / 241 |

总计 61,038 个候选，60,795 个通过、243 个回退，认证 158,488,174 / 1,146,442,446
UOP（13.824%），候选通过率 99.602%。对 C3-A/C3-B1 的 92 份完整 JSON 做递归比较，
差异路径只有 K/证书配置、chunk/lookahead/max-resident、证书审计、宿主 phase wall、
throughput 和非确定性 `frontier_waits`；逐核 target state、CPI、所有 PMU、response
closure 和共享事件顺序零差异。该结论比只比较汇总 CPI 更强。

### 19.5 精度结论和下一实施项

C3-B1 通过了独立的 5M throughput gate，但没有也不应该改善 CPI；四个 P99 gate
仍全部失败。下一阶段继续固定 Q=1024、K=4096 和证书路径，不重采 gem5 trace，先用
现有 92-case baseline 做 component-aligned ablation：

1. 对 C4 Redis/MySQL/PyTorch/GoFeed 以及 C8/C16/C32 GoFeed 的正偏，拆分 branch/
   fetch、rename/dispatch、producer dependency、IQ/ROB response closure，优先修复
   能由现有 PC、OpClass、producer distance 和 branch outcome 识别的结构；
2. 对 C8/C16 `memory_seq_moderate` 的 -16.158%/-9.656% 负偏，检查 functional
   arrival、sequencer/MSHR、DRAM bank/bus predecessor 与可见 MLP，禁止用固定 miss
   penalty 或当前 workload scale 拟合；
3. 把 LLC miss 和 DTLB miss 的核数相关误差与 CPI 同时设 gate。只有一个组件在
   mechanism、business 和 heldout workload 上方向一致时才进入默认配置；
4. 吞吐侧下一步是把 activity bit 下沉成 mixed checkpoint 内部 segment，再实现
   LLC/CHA/DRAM owner snapshot；两者仍以完整 target-state diff 和 canonical fallback
   为验收条件。

Q=512/2048、adaptive Q 和 DR 输入继续后置；它们不能参与当前固定 Q 的组件选参。

## 20. 固定 Q=1024 的当前 SE-label 审计与 FS-label 泛化边界（2026-08-04）

### 20.1 本检查点的口径

本轮不重采 gem5 数据，使用当前代码、`sim.interval_max_cycles=1024` 和
`configs/gem5-v28_1-time-epoch.cfg` 重跑 23 workload × C4/C8/C16/C32 共 92 个
case。完整结果位于 `tmp/current-se-profile-full92-q1024-v1/`。输入合同始终是同一种
canonical functional trace；本节的 **SE-label profile** 仅表示监督和验收标签来自
gem5 SE。`dtlb.miss_model=se_atomic` 对齐 x86 SE 标签下
`Process::pTable` 同步查表和立即 TLB fill；它不能作为 FS 的默认 TLB 模型。

此前 profile 把 SE TLB miss 当作单 walker timing walk，属于 baseline 语义错误。修正
它是 source alignment，不是根据 workload CPI 拟合 penalty。未来输入仍是 functional
trace，但 FS-label profile 必须切回 `timing_walk`：FastSim 根据 functional virtual-page
stream 和 FS 配置合成 page-table walker 请求，再接入 cache/coherence/DRAM 模型；
gem5 FS 只提供离线 CPI/PMU 标签，不向 FastSim 提供 timing/event oracle。

### 20.2 当前 CPI 和吞吐量

| Cores | CPI mean / median / P90 / P99 / max | signed bias | 最低 UOP/s | Gate |
|---:|---:|---:|---:|:---:|
| 4 | 2.830% / 2.509% / 5.615% / 6.256% / 6.312% | +1.879% | 6.547M | PASS |
| 8 | 1.649% / 0.901% / 2.985% / 5.461% / 6.141% | +0.489% | 7.069M | PASS |
| 16 | 1.588% / 0.651% / 2.968% / 8.917% / 9.460% | -1.156% | 5.173M | PASS |
| 32 | 3.670% / 2.181% / 7.849% / 11.057% / 11.614% | -2.496% | 5.381M | **CPI FAIL** |

92 个 case 的 UOP、memory event 和 private/escape partition 守恒全部通过。C32 只差
1.057 percentage points 才达到 P99 10% 目标；最大尾差仍是
`memory_random_mlp=-11.614%`。`memory_seq_moderate=+9.078%` 与它方向相反，已经排除
“统一增加或减少 DRAM/cache latency”作为正确修复。

逐 workload 的 UOP CPI 有符号误差如下；C32 同时给出 gem5/FastSim CPI：

| Workload | C4 | C8 | C16 | C32 gem5 / FastSim | C32 error |
|---|---:|---:|---:|---:|---:|
| bvc_encoder_base | +2.354% | +0.477% | -0.085% | 1.373 / 1.349 | -1.721% |
| bvc_encoder_heldout | +1.415% | -0.342% | -0.347% | 1.404 / 1.375 | -2.094% |
| cache_L1_mixed | +2.912% | +2.980% | +3.146% | 0.643 / 0.672 | +4.429% |
| cache_L2_mixed | -2.203% | -2.249% | -2.166% | 1.211 / 1.115 | -7.926% |
| coh_readmostly_sparse | -4.274% | -6.141% | -6.989% | 0.658 / 0.609 | -7.542% |
| flink_base | +2.509% | +0.867% | -0.641% | 1.993 / 1.950 | -2.155% |
| flink_heldout | +2.379% | +0.720% | -0.544% | 2.345 / 2.283 | -2.644% |
| fp_alu_dense | -0.611% | -0.626% | -0.651% | 0.144 / 0.143 | -0.696% |
| gofeed_base | +4.040% | +2.024% | -0.597% | 1.612 / 1.576 | -2.181% |
| gofeed_heldout | +5.052% | +2.348% | -0.305% | 2.189 / 2.129 | -2.739% |
| int_alu_dense | -0.114% | -0.126% | -0.131% | 0.735 / 0.733 | -0.143% |
| int_div_serial | -0.108% | -0.108% | -0.114% | 0.823 / 0.822 | -0.128% |
| marine_base | +1.965% | -0.032% | -1.314% | 2.287 / 2.213 | -3.236% |
| marine_heldout | +1.714% | -0.147% | -1.789% | 2.265 / 2.151 | -5.025% |
| memory_random_mlp | -3.294% | -2.654% | -9.460% | 4.111 / 3.634 | **-11.614%** |
| memory_seq_moderate | +3.019% | +3.049% | -1.000% | 6.930 / 7.560 | **+9.078%** |
| mysql_base | +6.059% | +2.954% | +0.822% | 2.907 / 2.872 | -1.203% |
| mysql_heldout | +6.312% | +2.986% | +0.992% | 3.145 / 3.108 | -1.187% |
| pytorch_base | +4.851% | +2.369% | -2.256% | 1.365 / 1.267 | -7.220% |
| pytorch_heldout | +0.991% | -0.579% | -2.073% | 1.520 / 1.411 | -7.208% |
| redis_base | +2.834% | +0.901% | -0.552% | 1.760 / 1.725 | -2.009% |
| redis_heldout | +5.755% | +2.919% | -0.194% | 2.636 / 2.587 | -1.858% |
| simd_sse_dense | -0.328% | -0.333% | -0.346% | 0.274 / 0.273 | -0.377% |

### 20.3 当前 PMU 误差

PMU 必须同时报告 workload-equal percentage 和 count-weighted error。前者会把几十到
几百次的启动/低计数事件放大，不能用来驱动 timing 参数。按 gem5 总事件计数加权的
absolute error 如下：

| Cores | Branch miss | L1D miss | private L2 miss | CHA LLC lookup | LLC miss/tag | DTLB access | DTLB miss |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.165% | 0.043% | 0.239% | 0.239% | 0.767% | 8.770% | 0.027% |
| 8 | 0.170% | 0.048% | 0.266% | 0.266% | 0.769% | 8.799% | 0.033% |
| 16 | 0.160% | 0.052% | 0.283% | 0.283% | 0.760% | 8.805% | 0.034% |
| 32 | 0.153% | 0.059% | 0.290% | 0.290% | 0.702% | 8.843% | 0.034% |

这说明 cache/CHA/LLC/branch/DTLB-miss 的**路径计数**已经不是 CPI P99 的首要误差源。
当前 PMU 明确弱项是 DTLB access：FastSim 每条 functional memory event 计一次，而
gem5 DTB access 的 ROI/拆分访问口径更宽，产生约 8.8% 系统性少计。SE profile 中
atomic miss 不产生 timing penalty，因此该计数差当前不主导 CPI；FS page walk 会使它
变成必须先对齐的 functional-observation/latent-event 映射。

下表是最困难 C32 的逐 workload 有符号 PMU 误差。`*` 表示该 workload 至少有一项
gem5 reference 小于 1000；此时几十或几百次固定启动事件即可产生很大的百分比，必须
结合 `summary.csv` 中的原始计数解释。

| Workload | Br miss | L1 miss | L2 miss | CHA lookup | LLC miss | DTLB access | DTLB miss |
|---|---:|---:|---:|---:|---:|---:|---:|
| bvc_encoder_base | +0.92% | -0.18% | -0.52% | -0.52% | +0.02% | -1.12% | -0.05% |
| bvc_encoder_heldout | +0.33% | -0.16% | -0.49% | -0.49% | +0.03% | -7.65% | -0.03% |
| cache_L1_mixed* | -2.40% | -0.85% | -3.76% | -3.76% | 0.00% | -0.17% | -7.72% |
| cache_L2_mixed | -2.40% | -0.02% | -0.63% | -0.62% | 0.00% | -0.18% | -2.55% |
| coh_readmostly_sparse | -1.65% | -0.02% | -0.56% | -0.56% | 0.00% | -0.12% | -1.99% |
| flink_base | +0.27% | +0.03% | -0.30% | -0.30% | +0.01% | -4.01% | -0.02% |
| flink_heldout | +0.07% | -0.02% | -0.32% | -0.32% | 0.00% | -8.29% | -0.02% |
| fp_alu_dense* | +6.67% | -39.16% | -61.08% | -61.08% | 0.00% | -67.83% | -20.00% |
| gofeed_base | +0.07% | -0.07% | -0.34% | -0.34% | +1.91% | -12.60% | -0.04% |
| gofeed_heldout | +0.02% | -0.07% | -0.37% | -0.36% | +2.09% | -9.42% | -0.04% |
| int_alu_dense* | -12.50% | -63.10% | -79.05% | -79.05% | 0.00% | -82.53% | -53.77% |
| int_div_serial* | 0.00% | -39.76% | -68.65% | -68.65% | 0.00% | -50.06% | -59.67% |
| marine_base | +0.05% | -0.05% | -0.30% | -0.30% | +0.20% | -20.20% | -0.03% |
| marine_heldout | 0.00% | -0.05% | -0.26% | -0.26% | +4.87% | -23.20% | -0.03% |
| memory_random_mlp* | -8.94% | -0.02% | -0.12% | -0.12% | +0.01% | -0.13% | 0.00% |
| memory_seq_moderate* | -6.67% | -0.01% | -0.10% | -0.10% | 0.00% | -0.18% | -0.59% |
| mysql_base | +0.13% | -0.06% | -0.29% | -0.29% | +1.74% | -10.97% | -0.02% |
| mysql_heldout | +0.06% | -0.05% | -0.32% | -0.32% | +2.29% | -10.31% | -0.02% |
| pytorch_base | -2.30% | -0.10% | -0.23% | -0.23% | +0.06% | -0.51% | -0.02% |
| pytorch_heldout | -0.04% | -0.12% | -0.25% | -0.25% | +0.07% | -1.75% | -0.03% |
| redis_base | 0.00% | -0.08% | -0.34% | -0.34% | +0.02% | -0.28% | -0.04% |
| redis_heldout | -0.24% | -0.08% | -0.33% | -0.33% | 0.00% | -3.35% | -0.03% |
| simd_sse_dense* | 0.00% | -42.51% | -62.40% | -62.32% | 0.00% | -56.34% | -28.32% |

### 20.4 误差源分级结论

**已经确认的模型/配置错误：** SE 与 FS DTLB miss 语义曾被混用，现已通过显式
`se_atomic|timing_walk` 拆分。该修复只对当前 SE baseline 成立。

**当前 CPI 尾差的首要结构嫌疑：response-corrected epoch boundary。** time-epoch
先按 lower-bound issue/retire 选择 `<= horizon` 的 UOP 和 memory event；response
feedback 随后可能把它们推到 horizon 之后，但当前 checkpoint 仍提交整个 accepted
prefix。C32 random/seq 分别有 42,316/8,701 个 in-flight memory UOP，以及
14,335/14,903 次 corrected-horizon violation。启用仅审计的详细 pass 后，又分别发现
1,915,753/893,094 个 corrected issue event 越过 horizon。它尚未量化为独立 CPI
贡献，但已经证明当前 decode/issue/retire cursor 被错误地合并，足以改变下一 epoch 的
跨核 controller arrival order 和可见 MLP。

**与首要问题耦合的第二嫌疑：controller arrival + FRFCFS。** functional trace 不含
gem5 MemCtrl enqueue cycle；当前 `frfcfs_selection_window=8`、
`arrival_bucket_cycles=64`、`max_accesses_per_row=16` 是 proxy，不是直接来自 gem5
配置的目标参数。它们可用于有界近似，但在 issue/retire 边界尚未闭环时继续调这些值
会把上游到达顺序错误拟合进 DRAM scheduler。

**已排除为当前主因：** response-cone 的逐条 Issue/FU/writeback collision repair 在
C32 random 只发现 51 个 issue collision cycle、0 个 writeback collision cycle，CPI
基本不变且吞吐下降，因此详细 FU/WB 日历不是当前 P99 尾差。统一 latency scale、ACT
timing 或 row-policy sweep 也不能同时修复 random 负偏和 seq 正偏。

**尚未覆盖、但不能用当前 SE 标签拟合的组件：** 完整 I-cache/ITLB timing、FS page
walk 请求、kernel/cache pollution、中断、syscall 服务、调度/context switch、TLB
shootdown 和迁移 warm-state。FS-label 阶段不要求输入 kernel timing trace；这些行为应
建成由 functional trace 可见特征和 FS 配置驱动的 latent component。FS 标签只能约束
其总 CPI/PMU 效果，不能被当作逐事件 oracle。

### 20.5 防止 SE 数据集过拟合以及未来 FS baseline 的规则

1. 配置必须拆成显式 `gem5_se_label` 和 `gem5_fs_label` target profile；两者读取完全相同
   的 functional trace schema，区别是要预测的 baseline 语义。SE label 使用 atomic
   pTable；FS label 使用由 functional page stream 合成的 timing walker、walker
   concurrency 和 page-walk memory path。label profile/数据集 metadata 不匹配时
   fail closed。
2. 宽度、ROB/IQ/LSQ、FU、cache/TLB、NoC、DRAM timing、branch predictor 等参数只从
   gem5 `config.ini` 和对应源码语义映射。workload 名称、CPI residual 和逐 workload
   scalar 不得进入 production config。
3. Q=1024 继续冻结为当前 accuracy contract；K、worker 和 activity certificate 是宿主
   框架参数，必须通过 target-state differential 证明不改变 CPI/PMU。不得在 Q sweep 后
   选择最优 workload 结果。
4. 上述 FRFCFS window/bucket/row cap 标记为 provisional proxy。先修 in-flight suffix
   carry 和 controller-arrival closure，再决定能否由可观测结构量推导；无法推导的值不能
   用当前 23 workload 寻优后宣称微架构泛化。
5. 机制开发使用 component tests 和 C4/C8 诊断集；冻结后一次性验证 C16/C32、heldout
   和未来 FS 数据。只有 source-derived 机制在 mechanism/business/heldout 上方向一致，
   或至少不回归相反 workload，才可进入默认 profile。
6. FS 标签必须携带测量合同：ROI 边界、CPI cycles 分子、user/all-instruction 分母、PMU
   user/system scope、warmup 及核聚合方式。FastSim 可以只凭 functional trace 预测 aggregate
   FS CPI/PMU，但无法恢复标签中未观测 OS 事件的精确逐事件时间线；相同 trace/config 若因
   OS 非确定性产生不同标签，应报告条件均值/区间，不能把噪声拟合为 workload scalar。

下一实现项保持与 baseline mode 无关：拆分 decode lower-bound cursor、memory issue
cursor 和 retirement cursor；对越过 horizon 的 response-corrected suffix 不提交，携带
ROB/LSQ/dependency/未发出 memory state 到下一 epoch。完成后先做 target-state/事件顺序
守恒，再复跑同一 92-case，观察 random/seq 是否同时收敛；在此之前不再调 DRAM proxy。

### 20.6 固定 functional trace、改用 FS 标签后的模型分层

正式问题定义为：

```text
input  = canonical functional trace + microarchitecture/FS configuration
label  = gem5 FS CPI and selected PMU over the matched ROI
output = FastSim prediction of those FS labels
```

gem5 FS 不参与 FastSim 在线运行。预测模型分成两层：

```text
FS-label prediction
  = trace-driven core/cache/coherence/DRAM timing
  + functional-observable-driven latent FS effects
```

第一层继续使用当前 UOP、dependency、branch、virtual/physical address 和跨核 memory
stream。第二层只使用 functional trace 已有或可由其确定的特征，不读取 baseline timing：

1. virtual-page reuse、ASID/thread 和 access type 驱动 TLB/page-walk 生成器；
2. syscall marker/number、访问字节数和 blocking class 驱动 syscall service 模型；
3. runnable thread、同步 marker 和核心映射驱动调度/迁移状态机；
4. 上述合成事件对 cache/TLB/CHA/DRAM 的污染和 PMU 增量；
5. 不能由 trace 唯一确定的 timer interrupt/background OS 活动使用 FS configuration
   定义的全局过程，并输出不确定度，不使用 workload ID。

若当前 FST 没有某个必要观测量，应扩展 **functional record/marker schema**，而不是加入
timing trace。例如保留 syscall number、thread lifecycle、virtual-page token 仍属于
functional trace；MemCtrl enqueue cycle、page-walk completion cycle 则属于禁止输入的
timing oracle。

新 FS 数据不假定与现有 SE 数据成对，也不要求 workload 重合。每个 FS 样本独立提供
`functional trace + FS CPI/PMU label + target configuration`，FastSim 直接预测该样本的
FS 标签；禁止构造或拟合 `FS-SE residual`。DTLB miss、cache miss、CHA、branch miss 等
多目标标签用于约束 latent component，不能只靠 CPI 一个标量拟合统一 correction。
机制参数在一组 FS train workload 和一种微架构上冻结，再验证完全不同的 heldout
workload family、C16/C32 以及不同微架构/OS profile。当前 SE-label 92-case 结果仅作为
core/memory 基础层的组件诊断和防回归集，不参与 FS 差值校准，也不代表最终 FS-label
精度。

### 20.7 非配对 FS target-domain 的训练与验收

FS-label 数据集允许与 SE-label 数据集完全不同，正式映射是：

```text
(functional_trace_i, uarch_i, fs_config_i) -> (CPI_i, PMU_i)
```

这里不存在同一个 `i` 的 SE 标签要求。实现和验收遵循：

1. source-derived core/cache/NoC/DRAM 参数在 SE 和 FS target domain 间共享；SE 数据只做
   component regression，不给 FS 样本生成 pseudo-label 或 residual；
2. FS latent components 只能读取 trace-derived features，例如 page reuse/working set、
   branch entropy、dependency/MLP、syscall mix、thread/synchronization intensity 和地址共享；
3. 数据划分按 workload family，而不是随机切 trace chunk，确保测试集可为完全未见负载；
4. 额外做 leave-one-core-count-out 和可用时的 leave-one-uarch/OS-profile-out，防止把核数或
   某一套 FS 配置编码成 workload proxy；
5. 超参数只在 train/validation family 上选择，CPI P99 与 PMU gate 在 heldout family 上
   一次性报告；不能查看 heldout 后回调 DRAM window、OS penalty 或 exposure；
6. 若相同 functional observables 和配置对应多个 FS 结果，模型输出条件均值和不确定度；
   该不可约方差单独报告，不用 workload identity 消除。
