**FastSim 当前架构审查与验证，2026-09-07；所有实验固定 Q=1024。**

本轮结论来自当前代码复跑、逐请求事件、单机制对照和 CPU 采样。没有用旧版本性能成绩证明当前优化，也没有做 Q 扫描。已有 gem5 标签和功能 trace 作为输入复用，本轮没有重跑 gem5。因此这里的“当前基线”指已冻结并实际复跑的当前工作区快照，不等于宣称找到了所有设计中的最优解。

最有证据的准确率问题是：cache 的功能状态先更新，core timing 后修正，请求准入、fill、依赖传播和 store commit 没有共享一套一致的事件生命周期。另一个有独立证据的问题是 same-PC StoreSet 代理对 Stockfish 的依赖过度约束。吞吐的主要成本是 producer、schedule、response feedback 的逐 UOP 工作；当前所谓 sparse/materialized 计数不能当成跳过了多少 UOP 的比例。

本轮产物目录为 [`tmp/architecture-evidence-20260907.hlrSNO`](../tmp/architecture-evidence-20260907.hlrSNO)。原始输出、命令、配置及摘要按 `runs/<case>/<variant>/` 保存。重要入口为 [audit.py](../tmp/architecture-evidence-20260907.hlrSNO/audit.py)、[analyze.py](../tmp/architecture-evidence-20260907.hlrSNO/analyze.py)、[bench.py](../tmp/architecture-evidence-20260907.hlrSNO/bench.py)、[分析结果](../tmp/architecture-evidence-20260907.hlrSNO/analysis.json)、[源码和二进制指纹](../tmp/architecture-evidence-20260907.hlrSNO/provenance.json)。

**1. 本轮执行范围和当前基线。**

已执行当前基线 94 case：formal 40 case，覆盖 10 workload × C4/C8/C16/C32；DSE 54 case，覆盖 3 workload × C4/C8 × 9 个硬件配置。另执行 DSE 54 case 的 load-admission 机制对照、8 case 的无扰动归因、4 case 的 same-PC 对照、3 case 的 canonical replay 控制组和 3 case 的 line-parent 对照，以及定点请求审计和性能实验。

冻结二进制 SHA-256：`42f7c689c38ce5b2b15fd17704275bc7a63f04a92eeb8d3f00f450414c5bbbb3`。配置来自当前 `gem5-fs-native-kernel.cfg` 及每个 DSE case 的硬件配置，复制到审计目录后使用；每次运行末尾显式固定 `sim.interval_max_cycles = 1024` 并检查输出。所有性能测量绑定 CPU 0–47、NUMA node 0；硬件为双路 Xeon Platinum 8457C。准确率批量任务的运行速率不用于吞吐结论。

定义 `error = (FastSim CPI / gem5 CPI - 1) × 100%`。P99 是 case 绝对百分比误差的线性插值分位数，位置为 `(N-1)×0.99`，不是请求延迟 P99，也不是大量独立 workload 的总体统计保证。

| 当前快照，固定 Q | case 数 | MAPE | 绝对误差 P99 | 最大绝对误差 |
|---|---:|---:|---:|---:|
| Formal C4–C32 | 40 | 6.8096% | 13.5930% | 13.7367% |
| DSE C4/C8 | 54 | 11.8022% | 17.6522% | 17.9910% |

Formal 沿用标签的 user-plus-kernel macro CPI；DSE 用 `sum_core_cycles / user_trace_uops`。两个集合的 CPI 原值不混用，误差分别汇总。DSE 的 user UOP 和 user+kernel trace UOP 数全部与标签相同。一个 case（TeaLeaf C4/L2 2MiB）的 macro 指令数比标签少 1；它不改变本表使用的 user-UOP 分母。PMU `retired_uops` 与 trace UOP 的差异均由 syscall marker 恢复的 `24 × syscall_uops` 解释，不能误判成 trace 区间错位。见 [计数和 PMU 对齐检查](../tmp/architecture-evidence-20260907.hlrSNO/reference-checks.json) 和 [PMU 输出代码](../src/main.cpp:347)。

DSE 的 48 个非基线配置方向判断为 39/48；只看 gem5 相对基线变化至少 1% 的配置，为 34/39。全部配置对的次序一致性为 176/216；speedup MAPE 为 2.8288%。这些指标说明绝对 CPI、尾部误差和 DSE 趋势必须分别验收。

当前尾部包括：TeaLeaf C4/L1D 64KiB **−17.9910%**，TeaLeaf C8/ROB256 **−17.3517%**，TeaLeaf C8/LLC128MiB **−17.1778%**，ASTCENC C4/ROB96 **−15.4799%**；formal 中 Graph500 C8 **−13.7367%**、TeaLeaf C16 **−13.3682%**、Stockfish C16 **+12.8490%**。误差方向不同，不能用统一增加 memory latency 处理。

**2. 已抓到的因果反例：未完成的 miss 暴露为后续 L1 hit。**

当前 [SetAssociativeCache::access_indexed](../src/cache.cpp:159) 在 miss 时立即写入 `valid/tag/dirty`，接口没有 fill-ready 时间。[cache replay](../src/simulator.cpp:17155) 和 [private preview](../src/simulator.cpp:5749) 都会先执行功能访问，然后获得层次响应；当前配置关闭 line coalescing。单凭 eager tag fill 还不能断言错误，因为外部时序模型可以补偿；本轮因此进一步检查了真实请求。

LBM C4、core 0、sequence 2,000,000–2,010,000 的无扰动审计包含 3,889 个 data memory event。其中 744 次 L1 hit 的 shared-stage issue 早于此前同 line miss 的 canonical fill；388 次 L1 hit 的修正后 response 早于此前同 line miss 的修正后 response。这些是局部窗口计数，不能外推成整个 workload 的发生率。

一组可复核事件如下，同一 core、同一 line `5734961`，两条都是 load：

| 事件 | sequence | shared-stage issue | canonical fill | corrected issue | corrected response |
|---|---:|---:|---:|---:|---:|
| 首个 DRAM miss | 2,000,072 | 13,368,407 | 13,368,563 | 13,370,371 | 13,370,532 |
| 随后的 L1 hit | 2,000,076 | 13,368,409 | — | 13,370,374 | 13,370,376 |

后一个请求在 shared-stage 仅晚 2 cycle，获得 2-cycle L1 latency；反馈后它仍比首个 miss 早返回 156 cycle。现有反馈保留了该次错误的 hit 路径和 latency，没有恢复其对尚未完成 fill 的依赖。这直接影响消费者可见的数据就绪时刻。见 [事件见证](../tmp/architecture-evidence-20260907.hlrSNO/frontier-witness.json) 和 [完整请求审计](../tmp/architecture-evidence-20260907.hlrSNO/runs/formal-04c-782.lbm_r/frontier/stats.json)。采样模式与普通模式的 cycles、PMU 一致。Graph500 的同一 sequence 过滤范围未产生样本，没有将其计入上述证据。

进一步的机制对照保持 Q 不变：先只关闭 private preview，确认 3 case 的 CPI 与基线一致，再启用 `ruby.sequencer_line_coalescing`。LBM C4 误差 **−6.3997% → −1.8173%**；ASTCENC C4 **−14.0319% → −10.7606%**；TeaLeaf C4/L1D64KiB **−17.9910% → +10.4244%**。这证明未完成请求的父子关系足以显著改变预测；也证明当前 coalescing 实验路径不能直接作为通用修复。它仍在现有请求时间线之上工作，TeaLeaf 出现了过度修正。见 [对照汇总](../tmp/architecture-evidence-20260907.hlrSNO/ablation-summary.json)。

建议实现显式 pending-fill 生命周期：`request → admitted → pending → fill/callback → consumer-ready`，每个 pending line 保存 generation、父请求和权限状态，跨固定 Q 边界保留。命中 pending line 时必须依赖父 fill；写权限升级另建合法依赖。不能仅在最后给 load 加一个平均等待时间。private preview 应返回带 entry-state generation 的事务计划，只有其依赖仍有效时才提交。

**3. 请求时间和 store 生命周期被拆成了两套状态。**

[producer](../src/simulator.cpp:7539) 保留原始 OoO issue，同时用 `max(issue, last_memory_issue)` 将默认 data request 时间压成程序顺序。[memory_producer_issue_q16](../src/simulator.cpp:3667) 默认让普通 load/store 使用该 envelope；反馈随后又根据依赖、队列和退休状态移动 issue。core 队列反馈是存在的，问题是它与已经更新的 cache 状态是否一致。

8 case 的归因模式关闭 fast-kernel 特化并打开审计，cycles 和 PMU 全部与当前基线相同。[归因汇总](../tmp/architecture-evidence-20260907.hlrSNO/attribution-summary.json) 中：

| case | producer memory-order clamp 次数 | 反馈后同 line 事件逆序对 | hierarchy response 早于修正后 commit 的 store |
|---|---:|---:|---:|
| LBM C4 | 6,617,163 | 14,504,405 | 2,296,151 / 4,828,974 |
| Graph500 C8 | 4,641,918 | 5,664,665 | 419,016 / 1,956,981 |
| Stockfish C16 | 11,693,609 | 2,973,231 | 340,801 / 8,630,800 |
| TeaLeaf C16 | 5,493,358 | 9,759,955 | 981,531 / 3,636,184 |

逆序对不是“错误请求数”；多个 pair 可以涉及同一事件，read/read 逆序也未必改变状态。store 计数衡量已预测层次响应相对修正后 commit 的位置，不能当成 gem5 实际错误的次数，更不能把累计位移直接加到 CPI 上。它们和上一节逐请求反例一起，说明分离的时间线在实际输入中大量相交。

当前 [store feedback](../src/simulator.cpp:11906) 用 `store_send = max(actual_retire + admission_edge, tso_drain_ready)` 决定 SQ release；但默认 cache 的 store 请求已经在更早的 producer 时间处理过。`store_post_commit_request=false` 时 admission edge 还是 0。[本地 gem5 LSQ](../tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/cpu/o3/lsq_unit.cc:776) 则先在 commitStores 标记可写回，再由 writebackStores 发包。本地源码用于解释生命周期；没有将该 checkout 冒充每份历史标签的精确构建指纹。

建议将 store 的地址生成、forwarding 可见性、commit、TSO send、cache 权限/dirty 更新、SQ release 作为不同事件，但统一保存于同一个 store entry。修改 send time 必须同时影响 hierarchy admission 和 response，不能只移动 SQ 的 release。保持原有 LQ 在 retirement 释放的语义；这本身与本地 gem5 的 commitLoads 一致，不应为了提速随意提前释放。

还做了 `ruby.sequencer_load_admission=true` 的完整 DSE 对照。这个开关包含原始 load issue、issue-to-admission 边、准入排序与容量处理，因此其效果属于这一整套机制，不能全部归给某一个 `+1 cycle`：

| DSE 54 case | MAPE | P99 | 最大绝对误差 | 方向正确 |
|---|---:|---:|---:|---:|
| 当前基线 | 11.8022% | 17.6522% | 17.9910% | 39/48 |
| load-admission 对照 | 10.3332% | 17.1002% | 19.4810% | 40/48 |

TeaLeaf C4 baseline 从 **−11.4993% 到 −4.4538%**，但 TeaLeaf C4/LLC32MiB 从 **+9.6487% 到 +19.4810%**。平均指标改善，最坏 case 恶化，不能据此替换生产基线。正确方向是在固定 Q 内统一 admitted request 和 response 的依赖闭包，只重新处理受改变的 line、资源和消费者；Q 是批处理边界，不应决定目标请求的状态可见性。

**4. Stockfish 的正向尾误差：same-PC StoreSet 代理有很强的独立影响。**

[当前实现](../src/interval_core.cpp:1258) 看到同一 macro 中 load/store 地址重叠，就把 PC 加入已训练集合；后续[同 PC 依赖](../src/interval_core.cpp:1946)依据保守的 producer-live 条件加入。这里的训练不是一次实际 memory-order violation，集合也不是目标有限容量 SSIT/LFST 的等价实现。[本地 gem5 StoreSet](../tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/cpu/o3/store_set.cc:112) 则通过 violation 训练，并使用 SSIT/LFST、issue 清除和周期清除逻辑。

本轮 Stockfish C16 加入 14,963,658 条 same-PC 边，14,963,578 条的地址不重叠；7,485,694 个 UOP 的 base readiness 被这类边延长。地址不重叠不代表 gem5 的预测器一定不会产生假依赖，所以这项计数不是删除这些边的充分理由。

但是新对照只关闭 `core.store_set_same_pc_feedback`，Stockfish C16 CPI **0.758162 → 0.597708**，误差 **+12.8490% → −11.0339%**。同一改动对 TeaLeaf C4 CPI 无影响，对 ASTCENC C4 仅改变约 0.096%。Stockfish 的正向误差对这个代理高度敏感；直接关闭又产生明显低估，说明“永久 same-PC 代理”和“没有预测依赖”都不足够。

修复应使用目标有限容量的 SSIT/LFST 和实际模拟出来的冲突训练：比较尚未解析的老 store 与已经执行的年轻 load，处理 byte overlap、PC pair、LFST 失效与清除，精确保留 fence/LOCK。功能 trace 可提供地址和程序次序，issue/violation 必须由模型在线推导，不能读取 gem5 timing 当运行时 oracle，也不应通过 workload 名称调系数。离线抽取少量相同请求的 predictor 生命周期，用于验证假依赖数量和持续时间。

**5. 尚不能下结论的部分，以及为什么不能继续只校准 miss 数。**

TeaLeaf C4/L1D64KiB 的 L1D miss 数仅高于 gem5 **0.0110%**，L2 miss 仅高 **0.0725%**，CPI 却低 **17.9910%**。ASTCENC C4 的 L1D miss **−0.6435%**、L2 miss **+2.2943%**、branch miss **−0.9578%**，CPI 仍低 **14.0319%**。因此计数对齐不足以证明 response、重叠执行和依赖传播正确。

归因中的 dependency 占 response-extra 关键周期：LBM 61.19%、Graph500 76.33%、TeaLeaf C16 93.33%、Stockfish C16 97.66%。这些是 FastSim 自身额外响应延迟的分类，不是相对 gem5 的误差分解。memory 的根因经过寄存器依赖传播后也会落在 dependency 类，不能据此宣称“97% 的误差来自寄存器依赖”。

ASTCENC C4 的 response-extra 只有 413,713 cycle，而相对 gem5 的总周期缺口为 2,126,448 cycle。它的剩余误差需要继续核对 base pipeline 与 frontend 路径，不能全部归因于 shared-memory repair。branch miss 次数接近不等于 recovery、wrong-path 资源占用和预测状态生命周期一致。当前代码确实在 branch completion 后加入 recovery，并有 speculative fetch shadow；不能把它描述成“只罚 2 cycle”。下一步应对齐 branch resolve、redirect、首条正确路径 fetch/dispatch 和 wrong-path 占用，而不是任意增大 branch penalty。

DTLB miss 仍有明显计数差异，但这里的重试、合并和 timing miss 语义需要对齐。当前结果没有把 DTLB 计数差直接换算成 page-walk CPI。修正后的 FU/writeback 资源再预约也是可疑点，生产 `response_sparse_resource_repair=false`；本轮尚未测得资源超容量的逐 cycle 见证，因此不把它列成已确认的主要误差贡献。

**6. 吞吐证据与代码成本。**

对 LBM C4、Graph500 C8、ASTCENC C4、LBM C32 分别串行执行 `perf record -F 199 -e cycles:u --call-graph fp`。四次均为零丢样。下面是函数 self CPU-cycle 占比，包含进程的 warmup 和 measurement；不是 wall time，也不使用不完整调用栈的 inclusive 占比。完整输出见 [profile-summary.json](../tmp/architecture-evidence-20260907.hlrSNO/profile-summary.json)。

| 函数 | LBM C4 | Graph500 C8 | ASTCENC C4 | LBM C32 |
|---|---:|---:|---:|---:|
| response feedback fast kernel | 21.33% | 28.08% | 24.51% | 17.42% |
| IntervalCoreModel::schedule | 21.17% | 17.54% | 22.28% | 17.73% |
| produce_thread_chunk | 18.53% | 19.64% | 20.19% | 17.34% |
| 三项合计 | 61.03% | 65.26% | 66.98% | 52.49% |

measurement 阶段计时给出另一侧证据：LBM C4 feedback 占 44.25%，schedule-batch 占 28.15%；ASTCENC C4 分别为 31.67%、45.19%。feedback、domain 等计时嵌套于 weave，不能把所有百分比相加。C4/C8 的有效 FR-FCFS selection window 为 1，走旁路；C32 LBM 的 FR-FCFS 显式 timer 为 3.29%。该 timer不含全部预处理排序，故不能作为 FR-FCFS 总成本的精确上界；但结合函数采样，它也不是当前最强的吞吐瓶颈证据。

有一处计数口径特别容易误导优化：[materialized_uops](../src/simulator.cpp:12142) 只统计 completion、retire 或 dispatch 有位移的 UOP；不是循环实际访问的数量。[fast-kernel 入口](../src/simulator.cpp:9345)按整个 accepted 区间累计。四个 profile 的 `response_activity_certified_uops` 都是 0，fast-kernel UOP 数等于 accepted UOP 数：例如 ASTCENC 的 materialized 仅 7,837,145，但 fast kernel 仍处理 40,141,165 UOP；LBM C32 为 324,124,074 UOP。当前实现的主要工作量仍是逐 UOP。

此外，[load-admission proposal pass](../src/simulator.cpp:13380)复制 `TimingFeedback` 后，顺序调用每个 core 的完整反馈，再执行正常反馈。`timing_feedback_wall_ns` 的起始点在 proposal 之后，单看该 timer 会漏掉这部分新增成本。不要为了修正准入顺序永久引入每 epoch 两次完整 closure。

编译器 layout 检查给出 `ChunkUopBound=144 B`、`ChunkMemoryEvent=72 B`、`SharedTimingDescriptor=208 B`。后者含大量 canonical provenance 字段，前者含多个时序和 DTLB/审计字段。它们提示拆分热字段和冷字段、减少重复写入的方向；这些 size 不是实际 DRAM 流量测量，本轮没有证明程序受宿主内存带宽限制。现有 resident ring 已避免搬移退休后缀，scratch vector 也复用容量，不能把已经解决的问题当成新瓶颈。

串行重复吞吐对照已经完成：下表每格为 3 次交错执行顺序的中位数，单位 M user-UOP/s；三个 workload 共 27 次。对照的 cycles、PMU 以及完整 `totals/cores/threads/cha/instruction_cha` 输出全部一致。见 [重复测量结果](../tmp/architecture-evidence-20260907.hlrSNO/bench-summary.json) 和 [目标状态逐项检查](../tmp/architecture-evidence-20260907.hlrSNO/bench-target-checks.json)。

| 执行方式 | LBM C4 | Graph500 C8 | ASTCENC C4 |
|---|---:|---:|---:|
| 当前基线 | 4.1082 | 8.6895 | 8.3477 |
| 关闭 parallel feedback | 3.9289 | 4.9703 | 5.5916 |
| 关闭 private preview | 4.1291 | 8.6436 | 8.3512 |

关闭 parallel feedback 分别损失 4.36%、42.80%、33.02% 的吞吐；现有并行不是应当整体删除的开销。关闭 preview 的变化为 +0.51%、−0.53%、+0.04%，与重复波动相当，没有可推广的加速证据。下一步应减少每次协作处理的数据和重复计算，保留有实测收益的并行。

另外在没有其他审计任务竞争 CPU 时，对 TeaLeaf C4 baseline/load-admission 各测 2 次，并反转执行顺序。当前基线为 **7.1393 M user-UOP/s**（7.1213–7.1573）；load-admission 为 **4.3952 M**（4.3878–4.4026），吞吐下降 **38.44%**。结合上面的 proposal-pass 代码，这条准确率候选的额外完整遍历确实有显著端到端代价；没有把它当成免费提高精度的开关。该候选会改变目标周期和 PMU，与上表的精确执行方式对照分开解释。

**7. 建议的实施顺序和验收条件。**

以下为实施前的研究顺序。2026-09-07 pending-fill 候选已完成验收并决定暂停、默认关闭；后续研究以 [优化决策记录](optimization-decisions.md) 为准，不重复执行本节原 P0 扫描。下一项是 TeaLeaf 尾部关键路径/内存并行度对齐；完整事件状态统一仍未完成。

| 优先级 | 具体改动 | 要验证的行为 | 必须通过的验收 |
|---|---|---|---|
| P0 准确率 | 统一 pending fill、load admission、store commit/send/SQ release 的事件状态；跨 Q 保留 generation | 消灭已抓到的提前命中和父请求依赖丢失，避免只移动反馈时间 | 同 line 双 load、pending eviction、读后写权限升级、跨 epoch fill、TSO store、fence/LOCK 微型差分；再跑 formal40 + DSE54 |
| P1 准确率 | 有限容量 SSIT/LFST、violation 训练、issue/clear 生命周期，替代永久 same-PC 代理 | 避免 Stockfish 正负误差在两个粗糙极端间切换 | 对齐 sampled predictor 事件；同时约束正/负尾误差、最大误差和 DSE 次序 |
| P1 吞吐 | 让修正后的 request admission 与响应闭包增量协作，去掉 proposal + full feedback 的全量重复 | 准确率修复不必付出第二遍全 UOP 处理成本 | 相同事件顺序和目标状态摘要；报告每 accepted UOP 的 CPU 周期、总 wall time，而非只看遗漏 proposal 的 timer |
| P2 吞吐 | producer 生成可复用的静态指令视图；分离 bound/descriptor 热字段和审计字段 | 降低反复 hash lookup、描述符写入和复制 | 精确模式 cycles、PMU、core/CHA 输出一致；采样验证热点实际下降 |
| P2 吞吐 | 对满足统一时间位移条件的 segment 做精确状态转移；不满足即回退 | 扩大真正跳过逐 UOP closure 的范围 | 依赖、IQ/ROB/LQ/SQ/commit 赢家均未改变的证书；检查跨段消费者，不能用事件-only 平均缩放代替 |
| P2 定位 | ASTCENC base pipeline / branch recovery、DTLB 和资源预约的逐事件差分 | 解释上述两类内存修复后剩余的低估 | 先取得同序列、同单位的时刻见证，再实现；不按 workload 加经验 penalty |

精确 segment transfer 可以利用 `max(x+Δ,y+Δ)=max(x,y)+Δ`，但只在所有相关输入和资源赢家满足同一位移条件时成立；外部 fill、fence、容量边或消费者改变时必须回退。实现前就要设计状态摘要和判定成本，避免为“跳过”再扫描整个 segment。

全量准确率验收同时报告 MAPE、P99、max、正/负尾部及 DSE direction/pairwise order，保留 case 行结果；不能用平均误差下降覆盖最坏 case 恶化。当前 case 集已用于定位，最终推广还需要未参与机制选择的 trace/窗口。吞吐优化先要求目标状态一致，再以固定 NUMA、单 case 串行、交错顺序的重复测量判定；不要把并发批量跑分和独立单进程速度混在一起。

本轮已运行 `cmake --build build -- -j16` 和 `./build/fastsim_tests`，全部通过。交付是审查、复现实验与实施设计；机制开关的局部改善尚不构成可以替换默认架构的通用修复。

**8. 复现。**

审计目录保存了 94 case 的冻结配置、manifest、标签数值，以及 DSE 的 54 份 gem5 metrics；[input-provenance.json](../tmp/architecture-evidence-20260907.hlrSNO/input-provenance.json)记录输入 manifest 指纹和 trace 文件路径、大小及时间戳。大体积 FST/sidecar 文件没有复制，复现仍依赖这些输入可用。结束时源码、配置副本和二进制指纹与开跑时一致，见 [结束检查](../tmp/architecture-evidence-20260907.hlrSNO/end-fingerprint-check.json)。

在仓库根目录执行以下命令可重算分析或按冻结输入新增复跑结果；`reproduce` 使用独立 variant 目录：

```bash
python3 tmp/architecture-evidence-20260907.hlrSNO/analyze.py
python3 tmp/architecture-evidence-20260907.hlrSNO/audit.py --mode formal40 --variant reproduce
python3 tmp/architecture-evidence-20260907.hlrSNO/audit.py --mode dse54 --variant reproduce
python3 tmp/architecture-evidence-20260907.hlrSNO/audit.py --mode dse54 --variant reproduce-source-load --set 'ruby.sequencer_load_admission = true'
```

`analyze.py` 汇总本轮已命名的 `current/source-load` 输出；其他 variant 可直接检查各自的 `summary.json/stats.json`。`bench.py` 是本轮串行吞吐实验的完整执行程序，再次执行会更新其固定名称的 bench 输出。
