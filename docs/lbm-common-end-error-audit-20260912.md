# LBM：新默认配置下的 common-end 误差诊断（2026-09-12）

## 结论与适用范围

本次延续 Stockfish 的方法：先锁定输入身份与统计边界，再做全程阶段账本、动态指令/内存请求配对、反例窗口和单变量对照，最后才提出修复方向。

**LBM C32 的主要问题在 store 所依赖的共享内存服务及其与 commit/TSO/SQ 的时间衔接，不是刚调整的普通 load 固定延迟。** SQ 满是显著的阻塞位置，但不能据此直接扩大 SQ 或减小 SQ 释放延迟。新热点窗口中，释放后的发射间隔并未偏慢，偏慢的是释放之前的 store 服务。

当前证据支持优先修复两个相互作用的机制：

1. dirty LLC writeback 没有作为可重放的一等请求参与混合读写调度，导致大部分 FRFCFS 修复批次回退；
2. 默认路径把投影 shared-stage 得到的服务时长搬到实际 commit/TSO send 后使用，但没有据此重新验证真实到达顺序、排队与资源归属。

**尚未完成混合 RD/WB 控制器的因果干预，因此不能给这两个机制分别分配总 CPI 误差贡献，更不能承诺修复收益。** 本次只增加诊断产物和本文，没有修改生产源码、默认配置，也没有提交或推送。

本报告是 LBM 单负载诊断，不是新一轮 40-case 推广评估，也不是 workload-held-out 结论。四个核数均用于诊断；不将这四点的分位数冒充全套实验 P99。

## 1. 数据与测量口径

- 当前正式输入：`tmp/first-core-common-end-20260911/source/formal-{04,08,16,32}c-782.lbm_r/`；共 4 个配置、60 条 per-core trace。
- 当前结果：`tmp/load-ready-default40-20260912.Ydiy65/`。
- 本次诊断：`tmp/lbm-common-end-audit-20260912.YqJDXj/`，下文称 `OUT`。
- 默认配置：`gem5-fs-native-kernel.cfg` → `gem5-v28_9-fs-load-ready.cfg` → `gem5-v28_6-fs-materialized-kernel.cfg`。ordinary-load=3、response-to-ready=1；保留两阶段内核。
- 冻结生产 CLI SHA256：`f704f0bf52f95ade02a6d9eb5f988de93b290f2ca6f109b9425d2265db822e73`。
- scope 为 `user-plus-kernel`；CPI 使用 `scope_metrics` 的实际完成宏指令数，单位 cycles/macroinstruction。相对误差带方向；绝对 CPI 误差为 `abs(FastSim CPI - gem5 CPI)`。
- `first-core-target-common-end-v1`：首个核心到达 10M user-UOP 目标时共同结束；不是每核都取 10M，不用核数乘目标作分母。
- 输入是 record-bounded functional-warmup slice：warmup 建立状态但不计分；共同 barrier 后重置测量时间/计数，保留 cache、预测器、TLB、控制器、依赖/响应等状态。实际每核 warmup/measurement 数保存在 `reference-identity.json` 与源 `functional-boundary-coreN.json`。
- C32 实际测量：308,016,035 user UOP；311,919,003 mixed records；240,123,783 完成宏指令。
- 四个 baseline 数据 gate 均通过，未剔除案例。诊断不是吞吐评测；有并行分析与大文件 I/O，不使用诊断 wall time 声称性能收益。

### 为什么可以复用历史 gem5 中间状态

没有重新跑 gem5。复用的是 `tmp/lbm-full-roi-error-20260911/c32/gem5/trace/` 的原始 records/labels 与 native-response 日志，不是其旧 independent-core ROI 的 CPI/PMU 结论。

历史 FST 已删除，因此本次重新从历史 raw JSON/labels 对当前 FST 做完整逐记录校验；不能只凭旧报告的 identity=true 就复用。32 核均覆盖当前 warmup 与整个新 measurement，检查 ordinal、core/thread、microseq、PC、物理地址、size、op/CPL、源/目标数量及 11 个标志；hardware labels 同时校验动态身份、stage 顺序和 333 tick/cycle。

校验结果：311,919,003 条测量记录中，311,918,973 条有 hardware stage；剩下 30 条是 core0 syscall 辅助记录。全 32 核通过。**没有比较完整 RAW companion 内容**；推理继续使用正式当前 FST 自带依赖，不把 native 响应注入 FastSim。

参考重放使用相同 checkpoint、工作负载 ELF、辅助磁盘、argv/OMP、目标配置和每核测量起点。关键哈希：

- checkpoint metadata：`a1c05c49614be8fc771bca4ad0499e21f79ea4ea0e8f6b438278b11a5748febd`；
- workload ELF：`5e101a0f92be48ed2b1fde4608d1b39ab7ad9823a7ede8bb3fb95107b34d008e`；
- auxiliary disk：`faabe2d3e0cccfdb2f82c7de5967219d99987b49b53c085d5f792c7d21cc85e3`。

采集器二进制不同，历史长重放也曾被停止；本报告的复用依据是以上 provenance 加本次完整确定性前缀配对，**不是一次新的同二进制 gem5 仿真**。

新共同结束 tick 是 `19051778228256`。截断以当前 FST 的 exclusive record bound 为准，不能只用 `commit_tick <= end`：同一 tick 可以包含首个被排除记录。历史 post-common-end admission/response 不代表新采集的 drain 真值；occupancy 右截断，服务时间分布只用结束前完全观察到的请求。

## 2. 当前误差不是统一的固定延迟偏差

| 配置 | gem5 CPI | FastSim CPI | 相对误差 | 绝对 CPI 误差 |
|---|---:|---:|---:|---:|
| C4 | 3.231335 | 3.134823 | −2.9868% | 0.096513 |
| C8 | 3.202265 | 3.261426 | +1.8475% | 0.059161 |
| C16 | 3.527754 | 3.510302 | −0.4947% | 0.017452 |
| C32 | 3.906000 | 4.200373 | +7.5364% | 0.294373 |

核数增加时误差方向改变。C32 多算 **70,685,954 core-cycles**，32 核全部偏慢；worker cores 1–31 合计多算 65,988,801，占净误差 93.35%，不能归结为 core0 的串行段或 kernel。

C16 的整体误差较小有抵消：core0 偏慢、其余 15 核偏快；按宏指令加权的 per-core absolute CPI error 为 0.066763，而配置级 absolute CPI error 为 0.017452。C32 没有这种正负抵消。

### 同一当前二进制的对照实验

所有对照都回放完整 C32 当前输入，不把 10k 窗口单独冷启动。

| C32 设置 | gem5 CPI | FastSim CPI | 相对误差 | 绝对 CPI 误差 | 相对当前的周期变化 |
|---|---:|---:|---:|---:|---:|
| 当前 ordinary3 / response1 | 3.906000 | 4.200373 | +7.5364% | 0.294373 | 0 |
| 仅恢复 ordinary4 / response0 | 3.906000 | 4.193447 | +7.3591% | 0.287446 | −1,663,270 |
| 当前 load 设置，FRFCFS window=1 | 3.906000 | 4.263652 | +9.1565% | 0.357652 | +15,194,795 |

旧 load 设置只移除了当前总周期误差的 **2.353%**，并精确复现旧结果的 scope/threads。说明 LBM 主要问题早于本次 load-ready 默认变更，应保留新默认。

FRFCFS 关闭后更差，说明现有可用部分有益；这不证明其余回退批次正确，也不支持扩大/缩小窗口作为根治办法。该对照会改变跨核交错，不能把周期差当成独立的 DRAM 延迟常数。

## 3. 全程阶段账本：误差落在哪里

以相邻退休事件间的周期为单位，按下一条退休头指令的状态进行互斥划分。这里的 `productive` 是至少有退休进展的周期，并非可独立扣除的理想基线。

| 互斥阶段 | gem5 cycles | FastSim cycles | FastSim − gem5 |
|---|---:|---:|---:|
| 尚未取指 | 1,136,844 | 120,141 | −1,016,703 |
| 已取指、未发射 | 613,605,081 | 674,695,175 | **+61,090,094** |
| 已发射 load、等待退休 | 237,376,767 | 267,661,410 | **+30,284,643** |
| 已发射 store、等待退休 | 11,801,172 | 4,242,468 | −7,558,704 |
| 已发射其他指令、等待退休 | 777,292 | 29,600 | −747,692 |
| 有退休进展 | 73,222,585 | 61,858,544 | −11,364,041 |

阶段差额合计 70,687,597；加首尾边界差 −2,945、idle 口径调整 +1,302，**精确等于总差 70,685,954**。每核与 per-PC 分解也逐项守恒。

这些是状态账本，不是互相独立的根因贡献。尤其 store 已经退休但尚未完成的响应，可能通过 SQ 容量阻塞后面的 store 发射，最终记入“已取指、未发射”，并不留在 `issued_store` 一栏。

误差也不是仅在启动阶段发生。core1 在 20% 进度时还领先 gem5 35,618 周期，50% 落后 313,528、80% 落后 1,283,078，最后逐指令退休差达到 1,979,621。后者与配置 per-core cycle 差 1,979,518 相差 103，是共同结束边界的尾部，不是身份不匹配。

## 4. 真正关键的尾部窗口

全 32 核按 10k hardware-record 间距、stride=1000 扫描，不只重用旧窗口。扫描发现部分最大局部尖峰在 measurement 起点，也发现明显的中途尖峰。选取 C13 的 **source ordinal 3,127,618..3,137,618** 深入配对：

- 10,001 条记录全部是 user；含 2,012 条 load、1,873 条 store。
- gem5 99,099 周期，FastSim 208,950 周期，差 **109,851**。
- 窗口结束距新共同结束还有 **20,297,077 gem5 cycles**，不是 EOF/drain 截断。
- 全输入回放只开启该窗口的审计输出；scope 与全部 threads 精确等于当前生产结果。

### 4.1 逐请求服务配对

下表均为普通 store，配对依据动态 ID，不是仅按 PC 配对。native 服务从 Ruby admission 到最后 response；FastSim 为自己的反馈 latency，包含其模型 transport/handoff 边界，不能假定两者在 1 周期精度上完全同义。

| 窗口 | 配对 DRAM stores | gem5 服务均值 | FastSim 服务均值 | 窗口周期差 |
|---|---:|---:|---:|---:|
| C1 2,000,000..2,010,000 | 105 | 271.04 | 255.69 | −1,859 |
| C1 8,000,000..8,010,000 | 92 | 294.66 | 322.86 | +2,694 |
| **C13 3,127,618..3,137,618** | **315** | **303.07** | **647.88** | **+109,851** |

这是新默认、当前完整输入的重新计算。不能引用历史独立 ROI 中 C1 8M 窗口约 420 周期的 FastSim 均值作为本次结果。

C13 的 DRAM store 服务中位数为 gem5 245、FastSim 605；P99 为 1,220.24、1,426.84。偏差不只是一两个最大值：典型请求服务本身整体变长。1,558 个非 DRAM store 的均值仅为 1.73 vs 2.70，量级不同。

可直接复查的一条：C13 `sequence=3133677`、`PC=0x406aaf`、native `inst_seq_num=3160636`，gem5 admission tick `19045007543718`，response tick `19045007621973`，服务 **235 周期**；FastSim 对同一请求给出 **1,614 周期**。

所选 C13 stores 的 native/FS 服务和分别为 98,154 / 208,285。它们虽接近窗口周期，但包含跨窗口生命周期，**不能把其差 110,131 当成对窗口差 109,851 的精确可加解释**。

### 4.2 SQ 是传播路径，不是应直接调小的常数

C13 窗口内，FastSim 有 147,536 个“已取指、未发射”周期，全部对应 SQ blocked store heads；430 个 SQ 释放 owner 都可在完整 native store ledger 中找到。

- FastSim：同一 owner 的 SQ release → 被阻塞指令 issue，430 次均为 **3 周期**。
- gem5：同一 owner 的 response → 对应指令 issue，均值 **6.64 周期**，中位数 4；在 C1 两个窗口均值约 2.83 / 3.33。

因此，此尖峰不能由 FastSim 的“释放后多等了很久”解释。需要沿 owner 向前查为什么服务/释放变晚。也不能把 430 次中的所有 native 间隔都宣称由该 owner 单独因果决定：gem5 仍可能受其他资源或依赖约束。

重新匹配当前测量内 core1/4/13 共 **3,103,465 条 ordinary stores**，native 动态 ID 全覆盖，已观察到的 ordinary Ruby store 服务没有重叠。按新共同窗口右截断后的 inflight occupancy 分别为 **95.35%、95.44%、95.58%**；busy + gap + boundary 逐核精确守恒。这与本地 gem5 O3 的 `needsTSO/storeInFlight` 单 ordinary-store 在途实现一致。

这解释了 LBM 为什么对 store 服务误差敏感：store 服务持续占据这条串行链，延长会经 SQ 容量传播到后续指令。这里说的是该 gem5 配置的实现，不能泛化成所有 TSO 处理器的硬件规则。

### 4.3 共享服务使用的时间原点明显早于实际 send

两个时间差必须分开：

| 普通 store 窗口 | 实际 store_send − corrected_issue 均值 | 实际 store_send − projected shared-stage issue 均值 | 后者最大值 |
|---|---:|---:|---:|
| C1 2M | 793.94 | 5,727.66 | 11,512 |
| C1 8M | 966.34 | 5,095.84 | 15,087 |
| C13 新尖峰 | 3,323.46 | **33,125.94** | **102,996** |

`store_send` 根据当前代码的 `store_drain_ready - latency` 恢复；这个恒等式仅用于本次单 data-memory-event 的 ordinary store，不能直接推广到 split、atomic 或 pending/shared-service 路径。`corrected_issue` 是 core-local 响应修正后的请求时刻，不一定等于指令 issue；`shared_stage_issue` 是 reference-clock 下的 projected/effective shared-service origin，才是投影共享服务的原点。普通 data 事件保留 functional memory-order envelope，不应把其原点简单称为原始 AGU。

本 profile 的 reference/core clock 都为 3 GHz，无 DVFS 覆盖，因此这两个时钟域可以直接相减；其他频率配置必须先转换。第一个差混合 retire 与前序 TSO 等待，第二个差还包含投影到响应侧的位移，不能都称为 TSO stall。

默认 `src/simulator.cpp` 在普通 store 路径先确定实际 retire/TSO send，再设置 `sq_release = store_send + store_latency_cycles`；latency 来自之前 shared-stage feedback。**仅平移一段服务时长，不等价于把请求按真实 send 重新送入多核共享队列。** 大量跨核排队和 dirty writeback 存在时，原来的到达顺序、row hit、bank/channel owner 可能已不成立。

以上证明了原点错位和重新验证的缺口，但 33k 周期时间差本身不是多算的周期，更不能直接从 CPI 中扣除。

## 5. 为什么共享内存服务会失真

### 5.1 dirty writeback 让大部分 FRFCFS 候选无法修复

对当前二进制增加只读 reason 计数后，全程 scope/threads 与生产精确一致：

- FRFCFS candidate epochs：30,581；stable：3,265；fallback：27,316。
- **89.3234% 的候选批次因实际 dirty LLC victim → DRAM write side effect 而回退**；本次没有 alias 或 atomic 回退。
- core1 的 8M ordinal bin 中，393 个候选全部回退。
- C32 effective selection window 为 7；admitted read queue 可达 64。不能把 window=7 误称物理读队列只有 7 项。

源码 `apply_frfcfs_dram_repair` 对 materialized descriptors 做整体 `replayable` AND；一旦某请求有不可重放 dirty DRAM 写副作用，整个候选就保留 canonical 结果。`DramModel::Request` 没有 RD/WR 类型，writeback 经另一路匿名 enqueue/drain，不能安全进入当前只读 timing repair。

这里的 dirty 不是任意 private dirty eviction；被 LLC 吸收、没有真正 DRAM write 副作用的 private eviction 不属于此回退原因。也不是已回滚的 ghost side effect。

**89.32% 是路径覆盖率，不是 CPI 误差贡献率。** 强行把 `replayable` 置 true 会漏掉/重复提交写回副作用，不是修复。

### 5.2 混合读写控制器语义仍有缺口

当前控制器 bank/channel 状态、write queue 和 write mode 跨 Q 持久化；**不是每个 Q 把整个 DRAM 重置**。真正缺口是 successful FRFCFS 的 pending read set 与调度推进是局部的，且 writeback 不以同一服务身份进入调度。

与本地 gem5 `mem_ctrl.cc` / `dram_interface.cc` 对比：

- gem5 write enqueue 会安排后续 controller event；读队列为空且写队列过低水位时可主动推进写服务。FastSim 当前 enqueue 主要依赖满容量强制 drain 或后续 read 触发，缺少对应的 read-empty 自主推进。
- **实施前源码复核更正**：gem5 `mem_ctrl.cc:807` 将 `selQueue(mem_pkt->isRead())` 传给 `DRAMInterface::doBurstAccess`；`open_adaptive` 扫描当前选中方向的队列（跨 priority），不是同时扫描读写两类队列。FastSim adaptive 扫描主要位于成功的只读 FRFCFS 路径，write drain 仍缺少对应的同方向 adaptive 扫描。读空时开始排写要求队列**严格高于**低水位；低水位以下不应被无条件清空。
- gem5 区分 RD/WR 及 write recovery/direction timing；FastSim 当前读写共用较简化的 `access_decoded` 路径，缺少完整的写类型时序。
- 本次 write queue：enqueue 3,077,110，drain 3,076,320，初始 0、最终 790，守恒；high-water switch 167,244，capacity-forced drain 4,171，turnaround 192,270。不是简单“写请求丢了”。

C13 配对 DRAM stores 的 FastSim canonical command−arrival 均值 **486.62 周期**，bank-command−arrival 均值 430.28；315 个中 28 个 immediate command blocker 明确是 writeback owner。其他 blocker 多是普通读请求或 bank 约束，不能声称每个长请求都直接被写回挡住，也不能把 canonical blocker 当成 gem5 的逐命令配对。

缺失的某些真实写时序约束补上后可能增加延迟；因此“有代码差异”不自动证明它导致当前正偏差。需要混合控制器分项干预，而不是一概把 DRAM 调快。

### 5.3 miss 数量不是当前最强解释

C32 cache-tag 计数仅作辅助诊断：

| 事件 | gem5 | FastSim | 计数差 | 相对计数差 | 语义等级 |
|---|---:|---:|---:|---:|---|
| L1D tag miss | 6,614,269 | 6,620,148 | +5,879 | +0.0889% | proxy |
| private L2 tag miss | 4,038,624 | 4,039,756 | +1,132 | +0.0280% | proxy |
| LLC tag miss | 4,015,094 | 4,016,584 | +1,490 | +0.0371% | diagnostic |

它们不等价于需求 DRAM 事务数或许可升级/合并/远端供给，也不能证明所有 cache 状态相同。但“miss 总数大幅偏多”缺乏支持，而同动态 ID 的服务时间和 SQ 传播已有直接证据。

## 6. 建议修复路径与验收

### 第一优先：补齐混合 RD/WB 服务对象及副作用事务

让真实 dirty LLC writeback 成为带稳定 service ID、类型、arrival、line、owner/generation 的请求。区分 private victim→LLC absorption 与真实 DRAM WB；replayable 不再把“包含合法可重放写回”直接等价于不可修复。

cache/directory/write-queue/bank/channel/PMU 副作用必须 exactly-once；预览、重排失败、跨 Q 重试都能一致回滚。atomic/alias 仍按各自合法边界处理，不能抹掉防护。

### 第二优先：在两阶段框架内实现持久混合队列调度

保留 time-epoch/Q=1024 的批量内核，不引入逐 UOP 的全局事件主循环。RD/WB 进入同一个持久控制器，分别保留 pending queue，共用控制器时间和方向状态；补 read-empty 写推进、读/写服务各自的同方向 adaptive page policy 和冻结 gem5 配置对应的真实 RD/WR timing。

只有已合法到达的请求可以参与 row-hit/page-policy 选择；不能读取未来 trace 来替代真实候选。跨 Q 的 pending 与 terminal state 必须可验证，不能只留 bank ready 时间而丢掉未服务请求身份。

### 第三优先：实际 commit/TSO admission → 真实服务完成 → SQ 释放

普通 store 的实际 admission 应满足 AGU/commit、前序 TSO response、sequencer/cache 资源可用等条件；共享服务返回 response，驱动 store drain、SQ release 和后续容量等待。

时间原点改变后，重新验证受影响的共享服务顺序与资源 owner；checkpoint/generation 边界及有界回滚必须覆盖非 RAW 的 SQ/ROB/MSHR 等容量边，不能只传播数据依赖。

不要直接打开旧 `shared_store_service`/pending-fill 候选：当前实现对 dirty/carried/split 等场景仍有限制，配置还与 FRFCFS window>1 冲突。修复应替换重复的 canonical/repair 工作，不叠加第三次完整重放。

### 必须通过的机制测试与实验顺序

净因果至少需要三组干预：**controller-only**（保留 projected admission，替换混合 RD/WB 调度）、**store-admission-only**（控制器策略不变，从真实 send 重新求服务）、**combined**。三组效果有交互，不能简单相加。修正 admission 后 cache path/fill relation 可能合理变化，不能把“路径必须完全不变”设成验收条件；应检查新路径的身份、归属与守恒。

1. 微测试：混合 RD/WB 整段运行 vs 跨 Q 切分，service ID、时序和最终状态一致；read-empty/低水位边界；WR→PRE/读写方向/同方向 adaptive；dirty preview rollback exactly-once；late/split/cross-Q store admission 和真实 response owner。
2. 先回放当前 C13 新尖峰及 C1 2M/8M 反例窗口的**完整前置输入**，验证身份、队列守恒、响应→SQ owner、服务分布。不能只看局部 CPI 变好。
3. 再跑 LBM C4/C8/C16/C32，检查原来偏快、偏慢和抵消三种情况；先证明不靠新抵消掩盖错误。
4. 最后跑完整 10 workloads × C4/C8/C16/C32，报告 CPI MAE、MAPE、相对 P99、绝对 CPI tail、per-core 偏差及 workload-disjoint/trend 控制。吞吐另在安静主机顺序测量。

C13 窗口是依据当前误差后选的压力见证，不是独立测试集。它用于确保原机制问题确实被触达，不能把该窗口改善外推成全 C32 或其他负载的收益。

## 7. 产物、验证与限制

本次成功完成 **6 次完整 C32 FastSim 诊断/对照**：stage/reason、legacy-load、C1 2M、C1 8M、C13 peak、FRFCFS-off。其中 4 次输出型诊断的 scope（除 host throughput）与全部 threads 精确等于当前生产结果；两个干预保留相同计分人口。

C13 首次诊断曾被配置 preflight 拒绝：core13 选择先于 CLI `--cores 32` override 做合法性检查。保留失败记录，并用显式 `sim.cores=32` 配置重新运行成功；这不是一个被丢弃的仿真精度样本。未启动新的 gem5。

主要机器产物（均在 `OUT`）：

- `baseline.json`、`provenance.json`：当前四核数 CPI/per-core/PMU、源码和生产二进制身份。
- `reference-identity.json`、`converted/coreN.log`：本次完整新 FST ↔ 历史原始 JSON/labels 校验。
- `stage-comparison.json`、`gem5-analysis-coreN.json`、`stage-c32-analysis-coreN.json`：全阶段/per-PC 守恒账本。
- `selected-windows.json`：全程窗口扫描，不是任意挑选单个慢点。
- `store-lifetimes.json`：三核全部 ordinary store 动态 ID 与新共同窗口占用守恒。
- `witness-summary.json`、`peak-summary.json`、`*-pairs.json`：逐请求和 SQ owner 证据。
- `reason-summary.json`、`stage-c32-reasons.csv`：当前 FRFCFS 真实 fallback 原因。
- `*-validation.json`、`verification.json`：逐运行与最终交叉检查。

复现分析入口是 `baseline.py`、`identity_stages.py`、`select_windows.py`、`store_lifetimes.py`、`witnesses.py`、`peak_analysis.py`、`verify.py`。完整 identity/stage 会扫描数百 GB 历史文本并产生较大二进制中间文件；无需为查看结果重复执行。

本次没有宣称生产修复通过，也没有测得混合 RD/WB 修复的收益。最关键的可执行结论是：**以 C13 的 315 个 DRAM store 和 430 个 SQ owner 为首个验收见证，把真实写回调度、store admission 和 response/SQ 释放连成同一条可守恒的服务链；不要继续靠固定延迟、队列容量或 FRFCFS 开关拟合 LBM。**
