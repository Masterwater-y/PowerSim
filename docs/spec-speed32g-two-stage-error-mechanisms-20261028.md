# 32 GiB speed 实验的误差机制与两阶段修复方案

日期：2026-10-28。状态：源码、文档、固定实验输出审查；补充两次中性观察和极短机制探针。**本轮没有修改模拟器或生产配置，没有改变正式预测，也没有启动新的原生采集。**现有分支恢复候选仅用于极短反例，未用于正式 ROI 的精度实验。

后续实施：用户要求优先处理 Marian/XZ 后，已完成独立 store callback/publish 修复、六项 ROI 对照和吞吐 pilot，见 [修复与结果](marian-xz-private-store-callback-repair-20261028.md)。下文保留原只读审查及九项快照的状态，不以新实验替换原聚合。

## 1. 结论与证据等级

这批误差不能用一个 miss 延迟系数、统一等待或 FR-FCFS 窗口解释。XZ、Marian 明显偏慢，Graph500 随核数增加转为偏快，FLAC 在几乎不访问 DRAM 的 C4 上仍有小幅正误差。更值得修的是 **Bound 给出的服务身份、Weave 所用的服务时刻与资源生命周期没有完全一致**，而非根据最终 CPI 调参数。

本轮得到三种不同强度的结论：

1. **已核实的本批事实：** 输入/宏指令范围、实际 SQ 容量及目标参数没有发现可解释大误差的错配；旧 FR-FCFS 拓扑缩窗已经修复。中性观察将 XZ C4、Marian C8 的模型等待优先指向 SQ 释放、内存响应及依赖传播。
2. **代码和反例确认的机制缺口：** 功能 cache 提前安装与真实 fill 可见性分离；校正 issue 后普通路径仍沿用相对服务延迟；store prepare 的保守资格与自身 canWB 生命周期没有统一；DTLB 查询时间倒退时可能看到未来 fill；response 推迟分支之后，默认路径未同步推迟正确路径恢复。FU 尾时刻预约也存在已知的虚假等待。
3. **尚不能确认的因果份额：** 缺少本批与退休关键路径配对的原生逐请求时间日志，因此不能声称“某机制解释了 X% 误差”。中性观察提供排查优先级，微型反例证明机制不成立的条件，都不等于已经量出其在本 ROI 的影响。

建议顺序是：先取少量关键服务的成对见证，优先修 store 服务角色和 SQ 释放前驱；再分别处理数据 fill 可见性、可证明的排队 slack 复用和真实 DRAM 响应容量。DTLB/分支属于独立正确性修复，不能因为局部关系错误就预报整批 CPI 改善。全部保留 Bound–Weave 框架和固定 Q，不接入另一套全流事件求解器。

## 2. 锁定本次结果，避免混入其他窗口

分析入口是用户指定的 [report.json](../tmp/spec-speed-32g-fastsim-validation-20261028-revalidated/report-snapshots/1793172926/report.json)。该快照为 **9 项确认、XZ C16 待确认**；不是四负载乘三核数的完整矩阵。后续输出不能无声替换本次集合。

| 对象 | SHA256 |
|---|---|
| 指定 report.json | 4ddd81c587fe8c52839eb5f11a2aa7ba7465d3ce0ceb6a8b9d53ce389192dc63 |
| 原执行 frozen/build/fastsim | b32b720fc3de3a2d12a1510f221616243287ef007c35800d208c813355dda20c |

原预测在 [原验证目录](../tmp/spec-speed-32g-fastsim-validation-20261028/cases/)，参考在 [原采集来源](../tmp/spec-speed-32g-c4-c8-c16-20261027-final/source/)。重评分复用了原预测，未改变模型结果，也不是新的吞吐测量。冻结清单 107 个文件自身 hash 均匹配；当前核心源码与冻结源码一致，当前存储政策的后续变化不属于时序模型变化。

下表 CPI 为 workload-task active cycles / 宏指令数；相对误差为 (FastSim/gem5−1)，绝对误差为 abs(FastSim−gem5)，单位 cycles/宏指令。

| 负载 | 核数 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|
| XZ | 4 | 0.758505 | 1.007150 | +32.7809% | 0.248645 |
| XZ | 8 | 0.673482 | 0.933625 | +38.6266% | 0.260143 |
| Marian | 4 | 4.933260 | 6.005699 | +21.7389% | 1.072438 |
| Marian | 8 | 5.189270 | 6.039175 | +16.3781% | 0.849905 |
| Graph500 | 4 | 1.591453 | 1.657952 | +4.1785% | 0.066499 |
| Graph500 | 8 | 2.081304 | 1.960458 | −5.8063% | 0.120846 |
| Graph500 | 16 | 2.311802 | 2.053992 | −11.1519% | 0.257810 |
| FLAC | 4 | 0.453030 | 0.478073 | +5.5277% | 0.025042 |
| FLAC | 8 | 0.458477 | 0.482182 | +5.1703% | 0.023705 |

| 聚合范围 | CPI MAE，cycles/宏指令 | MAPE | WAPE | 有符号 bias | P99 APE |
|---|---:|---:|---:|---:|---:|
| 指定快照 9 项 | 0.325004 | 15.7066% | 13.7224% | +3.6865% | 38.1590% |

MAE、MAPE、APE 分位数按 case 等权；WAPE、bias 按通过核对的 task 宏指令数加权，分位数为 Type 7。正负误差抵消后较小的 bias 不代表精度较高。

主要退休 load miss 点估计仅 FLAC 两项和 Graph500 三项共 5 项具有本快照资格；XZ、Marian 的参考仍有 unknown，不能把已有 native tag 诊断替换成完整退休 miss 真值。目标身份、来源验证、模型精度、吞吐验收和硬件等价是独立结论；本批 hardware_equivalence 仍为 unverified。

## 3. 先排除容易误诊的来源

### 3.1 不是 SQ 32 项误映射成 114 项

逐核检查原始 config.ini、effective-target、uarch_profile 与实际 FastSim 输出，原验证目录全部 10 项的 SQEntries、配置 SQ、有效 SQ 和 buffer 容量均为 **114**。固定快照中的 9 项也一样。

本地目标 [gem5 LSQUnit](../gem5-fs/src/cpu/o3/lsq_unit.cc) 在 224–228 行要求 buffer 模式 SQEntries 等于 bufferSize；同一个 storeQueue 保持 store 到响应完成才释放，见 1258–1280 行。这里不是“32 项 SQ 后接独立 114 项队列”的目标。应检查 canWB、prepare、publish、callback、SQ release 的边，而非改变容量。

### 3.2 不能把关闭的旧分支误解成没有队列反馈

实际为 interval_weave / time_epoch、Q=1024，sparse_scoreboard 和 response_queue_feedback 开启。虽然 response_rob_lsq_feedback=false，生效的 sparse 分支仍重建 ROB ring 和 LQ/SQ 门槛，见 [simulator.cpp](../src/simulator.cpp) 13640–13758 行。完整动态依赖、load data-ready→completion 和 WB 约束也已经实现。

本批缺的是若干校正后服务/资源关系的闭合，不能提出“补上所有 RAW”“补上 ROB/SQ”或“再加一次 load 返回延迟”作为新修复。

### 3.3 FR-FCFS 已经真正开启

全部实际配置：topology_scaled_window=false、selection_window=0（使用有效 64 项窗口）、causal_selection=true、bypass=0。旧 Marian 的 window=1 根因已经不适用于这批结果。

| 负载/核数 | FR-FCFS 请求 | 模型最大 pending | 饱和选择次数 | fallback epochs |
|---|---:|---:|---:|---:|
| XZ C4 / C8 | 83,542 / 96,632 | 4 / 4 | 0 / 0 | 0 / 0 |
| Marian C4 / C8 | 403,793 / 707,453 | 19 / 64 | 0 / 439 | 0 / 180 |
| Graph500 C4 / C8 / C16 | 178,306 / 355,419 / 712,247 | 2 / 6 / 11 | 0 / 0 / 0 | 0 / 0 / 218 |
| FLAC C4 / C8 | 7 / 1,781 | 1 / 1 | 0 / 0 | 0 / 0 |

这些请求包含 I-side、prepare 等，不等于退休 load miss。fallback epochs 也不是失败请求数或误差比例。XZ 最大 pending 只有 4、C4/C8 重排仅 5/32 次；FLAC C4 仅 7 个 FR 请求，均不支持优先扩大选择窗口解释其正误差。Marian C8 的饱和及 Graph500 C16 的回退值得进一步取证。

### 3.4 时间分子没有发现足以解释大误差的偏移

9 项逐核 task 宏指令一致，unknown active cycles 为 0。共同终点严格满足：

    Δtask_active = 核数 × Δelapsed + gem5.inactive − FastSim.inactive

逐项重算残差为 0。慢核延长共同终点会影响其他核的 owner 时间；EOF 后补齐属于共同窗口合同，不能删掉来降低 CPI。当前参考 tick 换算的单案合计边界余数至多 0.048048 cycle，远小于误差周期。

测量前已有 record-bounded 功能预热，测量 barrier 保留 cache、目录、一致性、预测器、DTLB、DRAM 与依赖/响应状态，仅重置相应测量时间和计数，见 simulator.cpp 6652–6845 行。这排除“完全冷启动/起点清空 cache”的解释，但不认证两侧动态热状态完全等价。

**最终 stats.txt 不能直接当精确 ROI 的微架构参考。** 本批 9 项最终 stats 起点匹配共同起点，但终点晚于 FST 共同终点约 1.0000–1.0007 ms。最终 DTLB miss、DRAM bursts、平均时延及 pipeline stall 只能作不同窗的辅助信息。精确 CPI 使用 owner/oracle 窗口；cache 真值沿维护验证器选择的 Ruby/SLICC native summaries，不使用 kernel-events 的 path-class cache 字段。

## 4. 本轮新增证据：等待首先指向哪里

用冻结二进制及同一来源分别观察 XZ C4、Marian C8，仅改两项观察开关：关闭 materialized fast kernel 以开放现有细账，开启 cpi_attribution。未修改时序参数。

[XZ 中性核对](../tmp/speed32g-mechanism-audit-20261028/neutral-attribution/c04-801.xz_s/observation-neutrality.json) 与 [Marian 中性核对](../tmp/speed32g-mechanism-audit-20261028/neutral-attribution/c08-872.marian_s/observation-neutrality.json) 逐项确认：功能来源、时间账、记录的调度、threads、CHA、instruction CHA、非宿主吞吐 scope metrics 和每核 cycles 与原预测完全相同。观察没有成为新的精度收益或吞吐测量。

| 模型关键核 | feedback extra cycles | 归为 SQ 容量 | 归为依赖 | 归为内存响应 | 归为取指 |
|---|---:|---:|---:|---:|---:|
| XZ C4 core0 | 3,891,296 | 1,542,586 | 1,059,861 | 1,288,849 | 0 |
| Marian C8 core2 | 12,447,577 | 4,842,002 | 1,552,116 | 6,053,459 | 0 |

这是模型自身 checkpoint 反馈增量的终因分类，三列之和等于本核 extra；**不是 gem5−FastSim 误差分解，也不是可直接删除的等待**。base 中的等待没有全部进入该表。

无退休周期账提供另一种同向证据：XZ core0 共 3,614,416 个无退休周期，其中 fetched-not-issued 为 1,701,992、issued load 等待为 1,564,955、not-fetched 仅 258；Marian core2 对应为 12,441,506、3,033,997、9,206,370、853。其余为 store/非访存已 issue 等待。这支持首先沿 **SQ release 的前驱服务 → 请求准入/数据返回 → RAW/退休链** 查首个分歧，而非先改分支 penalty 或取指带宽。

ROB/SQ 累计 stall、walk delay 和请求 queue delay 会在多 UOP、多资源间重叠；不得与上表混加。特别是 Marian C8 的 ROB 累计量约 87.98B，不能当作其约 104.63M active cycles 内的互斥时间。

## 5. 候选一：把 store 资格和 SQ 释放绑定到真实服务角色

**源码事实。** simulator.cpp 7254–7381 行的 prepare 资格根据 epoch 后缀判断冲突；默认在同 set 见到另一 line 就可拒绝 prepare。这是充分但偏保守的非驱逐条件，L1 LRU 在未达到相联度的其他 distinct lines 下仍可能保持该行。它会抑制 permission overlap，延长 publish/SQ release，是 XZ、Marian 正误差的优先嫌疑。

反方向的近似同时存在：5171–5182、15699–15727 行的 legacy 路径可能复用 pre-retire 服务作为 permission-ready。原生 prepare 只扫描 valid 且 canWB 的 store；canWB 在 commitStores 建立，见 gem5 lsq_unit.cc 799–808、975–1036 行。因此不能只放松同 set 拒绝，就假定整个生命周期更正确。原本已有的权限或其他旧请求取得的权限可以合法提前存在，诊断中的 response-before-commit 数量本身也不能证明当前 store 违法 prepare。

**通用修复。** 保留一次 canonical cache 分类，复用 store descriptor，显式区分：

- 当前 store 自身 canWB 后的 prepare、队头 demand、尚在飞的 prepare 被队头接管、prepare 已完成后的队头 demand。
- line 的 tag、数据与写权限；从取得权限到 publish 的 resident/owner 有效性。
- 各资源实际的 admission→release 区间，及同一 SQ 条目的最终释放。

在这些前提同时有证明的服务上，用实际 replacement/owner 证据替换同 set 的一刀切拒绝；条件不成立时保留明确的未修复近似，记录拒绝原因。不能只用 L1 LRU distinct-count 代替 L2 TreePLRU、一致性失效和 owner 证明，也不能让尚未准入的未来 store 提前占名额，阻挡较早独立 load。

这是服务角色和生命周期修复，不是将 SQ 加大、将 cache latency 减一拍，或重开 certificate/lifecycle/bounded 旧组合。store 专用 hit latency 在当前 legacy 分支不消费的事实也不足以授权单独更改常数。

**预期与停止条件。** 正确解除虚假 prepare 禁止有望减少正误差；补自身 canWB 边则可能增加等待，净效果尚未测出。先证明本批关键 SQ 条目确实落入错误资格/生命周期域；若没有该见证，不进入大范围实现。资源未来预约反例、head/takeover/completed-prepare 三角色、同 set 不驱逐/真实驱逐及远端失效必须分别过差分测试。

## 6. 候选二：同一服务的身份、数据可见性和排队 slack

### 6.1 为什么计数接近，CPI 仍可能偏差很大

[cache.cpp](../src/cache.cpp) 238–245、314–320 行在 canonical miss 时安装 tag/valid；simulator.cpp 24113–24135 行先做私有 cache 分类，再求共享服务。这种功能顺序状态不自动等于校正时刻的数据已经返回。

与此同时，[退休 miss 观察器](../include/fastsim/retired_cache_miss_estimator.hpp) 是 4096 槽的旁路 fill 区间表；simulator.cpp 21935–22054 行只更新退休诊断计数，明确不反馈目标时序。它可以合并同线退休 miss 计数，而执行时序仍沿用旧服务。Graph500 C16 的 native L1 tag miss 为 1,430,934、模型 1,430,958，LLC 为 713,492、712,232，CPI 仍低估 11.1519%、绝对差 0.257810。计数接近不能推出 MLP、准入、返回或退休时间正确。

### 6.2 两个方向相反的问题要分开修

**提前可见导致偏快。** 后续功能 hit 可能实际落在前一 miss 的 fill 之前，需要依赖正确的 parent service，而不是读取未来数据。它与 Graph500 高核数低估的方向相符，但本批关键请求覆盖尚未证明。

**相对延迟整体平移可能导致偏慢。** simulator.cpp 14858–14872 行通常令 response=corrected_issue+saved_latency。设原 issue=a，服务已排在 S，返回 R；依赖将 issue 推迟到 a+d，但 a+d≤S 且服务顺序/路径完全不变，则返回仍可能是 R。旧公式变成 R+d，相当于重复计入服务前已有 slack。越过选择时刻、fill 边界、set/victim/owner 变化时，R 又不能无条件复用。

**通用修复分两片，独立验证。**

1. 先在可证明 parent/generation 的活跃服务上，补正确数据 fill 对 read completion 的必要下界。保留已有 minimum load/FU/WB 边；不改变 issue/admission、cache 分类、DRAM 选择、callback 或 slot release。现有 15282–15303 行已经有 data-ready→completion，新增工作应是绑定正确 data-ready，不能重复加 wakeup。无合格服务或下界未增加时必须严格中性。
2. 只有取得本批关键链上的重复平移见证后，才独立研究 slack 复用。资格不仅要 arrival 相对顺序稳定，还要服务选择、父代次、fill/permission 可见性、set/victim/owner 和资源区间稳定。超域失效，不能把所有相对返回替换为绝对时刻。

活跃服务只保存少数有类型的时刻：CPU-ready、data-fill-visible、Sequencer callback、各级 MSHR/TBE release。CPU ack 提前不意味着 fill 或资源释放提前。保留 resident hit 快路径，复用现有 descriptor 和 sparse feedback；不新增全 UOP 事件，不全量回滚 cache，也不遍历第二遍完整反馈。

**历史限制依然有效。** [pending-fill 第一阶段](pending-fill-phase1-20260907.md) 已经有大量提前返回消失却整体关键收益很小的证据，且四个代表 case 的独立吞吐下降 1.20%–4.72%；组合 store 修复还出现未来响应错误占用容量的反例。[服务复用校验](two-stage-service-validity-20260909.md) 已经实现 FR-FCFS 修复中的父代次检查，但明确没有完成普通 feedback 后的通用路径重分类。slack certificate 原型也已存在。当前中性等待账不足以单独满足这些方向的重新开启条件：必须先有新的、当前关键服务匹配证据，不能将旧开关改名后再测同一组合。

## 7. 候选三：DRAM 容量按准入至响应计算

生产是 simulator.cpp 691 行的 DramModel::schedule_frfcfs 及 20462 行的 apply_frfcfs_dram_repair，不是 MixedDramController。

生产调度器在 775–806 行按尚未选择的 pending 数判断 read buffer，在 876–884 行选出请求即从 pending 删除；已选但未返回的 response 未进入同一容量定义。目标 [gem5 MemCtrl::readQueueFull](../gem5-fs/src/mem/mem_ctrl.cc) 166–175 行明确以 totalReadQueueSize+respQueue.size()+neededEntries 判断容量。对照 [mixed_dram.cpp](../src/mixed_dram.cpp) 123–135 行，其容量也包含 reads+read_responses，至 dram_ready 才释放。后续还需把容量差异与本批真实准入/返回时刻配对，不能由静态差异直接推导 CPI 份额。

此外，当前修复遇 atomic/dirty-victim 等不可重放 descriptor 会整 epoch fallback；最多四轮的局部时序固定点之后，core feedback 仍可能进一步移动 arrival。这些是剩余服务近似，不能由“FR-FCFS 已开启”消除。

**通用修复候选。** 在已有通道状态中维护包含已选未返回请求的容量区间/小型到期队列，使用目标 readBufferSize 与实际返回边，不修改容量参数。只增补缺失资源语义，不替换整个控制器，不重启 projected DRAM，也不增加整轮求解。先以 buffer=1 的“A 已选但未回、B 到达”反例验证准入；再检查 Marian C8 关键请求是否受到这段容量约束，以及 Graph500 C16 是否被错误回退/到达关系影响。

**预期边界。** 容量更严格通常增加等待，可能改善 Graph500 低估，也可能恶化 Marian 正误差，所以不能凭其饱和就断言净改善。Marian 的问题可能首先来自服务生成/prepare 重复等待。refresh 同样只能在原生关键请求确实受影响后研究，不向所有 miss 追加 tRFC。Mixed 控制器缺失部分目标状态，不能因其容量定义正确就整体替换生产。

## 8. 独立正确性问题：DTLB、分支与 FU

### 8.1 DTLB 的未来状态泄漏

[interval_core.cpp](../src/interval_core.cpp) 1171–1187 行按当前查询的 earliest 安装已完成 walk；1233–1243 行直接查询单份 timing LRU。earliest 来自依赖 ready，按 trace 程序序并不单调：先处理的 UOP 可能查询未来时刻，后处理的独立 UOP 反而查询过去。后者可能看到尚未发生的 fill，也可能被未来 replacement 错误驱逐。

本批 Marian C4/C8 的 functional miss 为 8,636/14,726，timing miss 为 97,051/171,954；这两套计数不是同一事件，不能把倍数当作真实 miss 误差，更不能用不同窗的最终 gem5 stats 算严格 APE。FLAC C4 两者均为零仍有正误差，DTLB 不是统一解释。

修复应按 address-space/page/generation 保存活跃 fill 的可见区间，让 lookup 按自身时刻消费状态；保存版本直到所有未决较早查询越过安全前沿，不能用最大已查询时间直接回收。walker 准入尾时刻的未来预约也需要独立验证，单个 born-time 判断不等于完整翻译时序修复。

目标 [x86 walker](../gem5-fs/src/arch/x86/pagetable_walker.cc) 75–84 行仍将 coalescing 列为 TODO，每次 start 分配独立 WalkerState。因此本批 coalesce=false 有依据；**不得因同页将两个真实 miss 合成一笔 walk**。只可在身份明确时复用同一个已分配请求的 retry/follower。保留真实 miss/walk 身份、页大小和未知 PTE 边界，不关 DTLB、不扩大容量、不缩小 12-cycle 常数拟合 CPI。

### 8.2 分支 response→恢复边确实缺失，但不是已证实的主要来源

本轮冻结二进制的 [3/4-UOP 探针](../tmp/speed32g-mechanism-audit-20261028/core-branch-probe/summary.json) 复现：依赖 load 的误预测 branch 在 415 才完成，正确路径已在 238 fetch，提前 177 cycles。现有局部恢复候选将 fetch 推迟至 417；带后续 load 的小图总跨度从 449 到 628。独立分支、预测正确和 fast/generic 控制均通过。

这证明默认 response_branch_recovery=false 时存在必要控制关系缺口，不证明本批 Graph500 的全部低估来自它。本批 Graph500 C4/C8/C16 的物理 branch miss 仅 423/813/1,810，不能套用旧窗口的大量分支证据。修复还需区分 Decode 可识别的错误目标与依赖执行结果的 IEW 解析，不能全部强迫等待 backend completion；后继访存跨服务边界仍需失效处理。

### 8.3 FU 尾预约会制造 base 虚假等待

interval_core.cpp 723–770 行每条 FU lane 保存尾时刻，按程序序遇到依赖迟到的旧 UOP 时可能预订未来；后续独立 UOP 无法填入此前空档。已有 9-UOP 测试证明合法 30 cycles 的图可被该近似排到 55。Weave 的 max 反馈不会自动消除 base 中的虚假等待。

但已有 gap-aware 候选在真实正误差控制上曾退化，不能直接开启。若本批匹配的退休祖先链确有这一空档，才考虑用有界预约区间替换尾时刻；其共享服务如果跨越 fill/set/DRAM 选择边界必须重新验证。它是后备方向，不因模型内累计机会很多就超过当前 SQ/访存链的排查优先级。

## 9. 在两阶段与吞吐约束内实施

### 9.1 先取最小见证，再写候选

保留当前 9 项预测、原 FST 与全部状态，不清理它们。先选 XZ C4 core0、Marian C8 core2 的少量 SQ/退休头阻塞服务，附 Graph500 C16 core11 的反向误差控制；FLAC C4 作为低 DRAM、DTLB 为零的控制。

利用已有审计入口，按固定 ordinal 范围和记录配额输出：来源身份、core、macro/member、PC、task/地址空间、memory ordinal、line、service parent/generation、base/corrected issue、canWB、准入、CPU-ready、fill、callback、资源释放及 retire。需单独记录父请求和重试，不能只按地址匹配。

本批 native full_jsonl_enabled=false，汇总无法还原这些时刻。原生侧优先检查是否可用原二进制已有有界 debug 接口取得同源数据；若必须补充旁路采集，先固定配额与来源核对，严格匹配 FST/CPL/共同边界。不同二进制或不同功能流只能作机制参考。gem5 timing 只供离线查因，绝不作为 FastSim 推理输入。本轮尚未执行这一步，也没有要求重新生成全套 FST。

短诊断窗口必须从原预热/前缀状态进入，保留共同时间原点与跨边界活跃服务；不能重新冷启动窗口或平移掉已有时间差。若首个分歧更早，应向前追溯其少量祖先，而非扩大整段日志。

### 9.2 每次仅一个必要关系

| 顺序 | 可实施单元 | 进入条件 | 必须反证的失败方式 |
|---|---|---|---|
| 1 | store 角色/canWB/权限有效性与 SQ release | 本批关键 SQ 服务有成对分歧 | 非法提前 prepare、未来 store 抢占早 load、真实驱逐后仍复用权限 |
| 2a | 已认证 parent fill 的 read completion 下界 | 当前关键 load 确实消费未来数据 | 重复 wakeup、错误代次、无合格服务也改变结果 |
| 2b | 同一有效服务中的 slack 复用，独立于 2a | 排队区间内重复平移的当前关键见证 | 越过选择/可见性边界仍复用旧服务、降低真实 minimum service |
| 3 | DRAM 准入至响应的容量区间 | 关键请求实际受到缺失容量约束 | 只调 buffer 数、将尚未准入未来请求提前计入占用 |
| 独立 | DTLB 可见性或正确解析阶段的 branch recovery | 微型必要关系与当前触发范围明确 | 同页真实 walk 被错误合并、Decode 恢复被误推迟到 IEW |

上述排序是排查顺序，不代表已授权修改生产默认。已有功能开关的重新组合不算新机制。任何一片无法解释真实关键分歧，均应记录尚未定位并停止该片，而非继续调参数使平均值接近。

### 9.3 工作量约束

第一阶段继续并行生成基准时序与 descriptor；第二阶段继续一次 canonical Weave，使用现有稀疏反馈。新增状态仅属于活跃服务/实际冲突域，跨 Q 保留；不在全部非访存 UOP 上新增事件对象，不额外增加完整 proposal、feedback 或全局回滚迭代。

新状态优先替换既有重复查询/预约，不简单叠加另一张全流表。是否真正便宜要测量，不能仅以 O(active) 宣称低成本：版本保留、set/owner 证明、区间扫描和到期处理都计入工作量。每项输出触发数、覆盖的关键服务、拒绝原因、最大活跃项、局部组件大小及超预算次数。超预算保留可见的原近似，不计为已修复覆盖。

不做 workload 名、PC、核数或误差符号分派；不回归 CPI 系数，不扫描 Q，不用 oracle timing 驱动预测，不统一增减 cache/walk/DRAM 延迟。目标参数只来自实际 gem5 配置和可审计的机制映射。

### 9.4 验收与预期

先过单机制微型差分：正确关系、同拍边界、跨 Q/预热边界、父代次替换、容量空档和关闭态严格中性；再过 generic/fast 一致性。随后验证上述正负误差控制和固定 9 项完整 ROI，报告逐项 CPI、相对误差及绝对误差，并同时报告 MAE、MAPE、WAPE、P99/max 和 PMU 资格/unknown。诊断用短窗口不能替代完整 ROI。

对重开历史 pending-fill/服务闭合方向，旧 formal40、DSE54 的负实验与趋势约束仍然有效；在推广默认前需通过相应历史回归及未参与选择的新窗口/配置，不能仅用这四个选择过的控制称作泛化验证。

吞吐使用固定 CPU/NUMA、同一候选二进制的关闭/开启对照、安静宿主、串行交错至少三组（如 ABBA），所有必要辅助 pass 都计时。分别报告测量段用户 UOP 吞吐、功能预热与端到端 wall、Peak RSS；当前并行宿主 wall 和观察开关开销不用于预测候选成本。即使尚未达到项目吞吐目标，也不能把已有吞吐差距当作允许继续减速的预算。

**目前可预期的是机制方向，不是收益百分比。** 正确 store overlap/slack 可能降低 XZ/Marian 偏慢，补 fill/容量/控制必要边可能抑制 Graph500 偏快；各机制存在交互和反向效应。没有当前配对见证和 A/B 结果之前，不给出修复后 CPI、P99 或吞吐提升幅度。

## 10. 审查材料

- [来源和周期核对](../tmp/speed32g-mechanism-audit-20261028/evidence-notes.md)、[机器证据矩阵](../tmp/speed32g-mechanism-audit-20261028/evidence-matrix.json)。
- [核心/前端/后端审查](../tmp/speed32g-mechanism-audit-20261028/core-notes.md)、[存储机制审查](../tmp/speed32g-mechanism-audit-20261028/memory-notes.md)。
- [原两阶段修复审查](two-stage-repair-review-20260909.md)、[服务身份校验边界](two-stage-service-validity-20260909.md)、[历史决定与负实验](optimization-decisions.md)。历史“两阶段精度上限”只是对应旧配置的经验停止结论，不是普遍不可能性证明。
- [精度报告合同](accuracy-reporting-contract.md)、[项目目标与语义合同](project-goal-and-semantic-contract.md)。

旧 Marian C8 分析使用 39,127,055 task 宏指令，当前为 17,325,950；旧 XZ 也有不同正式/诊断源及插桩历史。旧消融只能提供机制假设和停止条件，不能把其周期贡献或收益比例填进当前快照。
