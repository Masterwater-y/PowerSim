# Store prepare 信用与混合窗口的接线调查

状态：单核合成试验通过，**未合入**。当前项目仍未完成独立 prepare 信用
和所有后续 head demand 的建模。本文记录可复核的边界，不代表真实 C4
工作负载上的效果。

gem5-spr CPU Sequencer 对普通请求和 store-prepare 分别设置 outstanding
上限；FLAC 配置均为每核 16，相关控制器的 TBE 容量另为 256。prepare
容量满时，Sequencer 丢弃提示并立即返回，普通需求不因该限额阻塞。后续
committed store buffer 对每个未合并的 line 仍发送普通写请求。现有 FastSim
`ruby_sequencer_max_outstanding=16` 在 source-order 实验关闭时仍限制旧
反馈路径的非 deferred 数据请求，但旧 buffer 的 clean prepare 和最终
dirty publish 只表示一次层级访问，不能等价描述两个原生请求。

曾在 `tmp/spr-prepare-credit-slice-20261105/work/` 的独立副本中实现一个
默认关闭试验，并未修改主工作树源码。该试验限单核、FCFS、一个全为普通
不同线 store 的窗口直至 EOF、没有预热和 scheduler，且所有 head 必须晚于
全部已接受 hint 的 callback；最终反馈 send 与预测不完全相等即一次事务
回退。它不是完整 SPR store buffer，也不能服务本批四核负载。

真实 Simulator 合成 fixture 的独立构建日志在
`tmp/spr-prepare-credit-slice-20261105/prepare-build7.log`，通过日志为
`tmp/spr-prepare-credit-slice-20261105/prepare-test7.log`。该 fixture 使用
功能 store 地址及属性：

| prepare 上限 | 最大同时在途 | 接受 hint | 丢弃 hint | 普通 head | L1 访问 | DRAM 读 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 16 | 16 | 16 | 1 | 17 | 33 | 17 |
| 2 | 2 | 2 | 15 | 17 | 19 | 17 |

两行均保留 17 条退休 store 和 17 个需求 Sequencer 请求。另一夹具使 head
早于 hint callback，触发一次完整事务回退，层级人口、退休人口和最终时间
与 OFF 一致；窗口中插入 fence 时沿 OFF 路径。该结果证明独立资源账和真实
冷 head 请求在一个人造子域可接线；不证明混合负载、跨 Q、C4、CPI、PMU
或吞吐。

基于当时主工作树的精确合并预览在
`tmp/spr-prepare-credit-slice-20261105/prepare-credit-preview.patch`；其中
包含配置、计数、限域接线和 fixture，未应用。预览在现有 group 拒绝原因
和异常边界修改的基础上生成，没有用隔离副本覆盖主工作树。主工作树里
`tests/test_spr_prepare_credit_runtime.cpp` 是先前未注册的 RED 夹具，不能
将它与上述隔离版通过的 fixture 混同。

扩展到真实负载时，不能简单删除 EOF 或全 store 限制。现有每个 FST memory
event 只有一份 `event_feedback`，需要为 hint 和 head 分别保留实际层级
结果；它们还必须和其他核的 load/store、同线 alias、预热及跨 Q 未完成
服务共享事务顺序和独立信用。当前层级回放可在批内提前改变 cache 状态，
而 head 的最终发送边只有 feedback 后才确定；若晚发送跨过已处理的其他
请求，仅修改反馈时长不能撤回提前可见的写入。

放宽“最终 send 必须等于预测”的候选规则，是复用已有单遍 FR 认证思路：
若实际 admission 虽晚于预测，仍不晚于已分配的同一服务起点，并且同线
alias、失效及其他事件的接受顺序没有变化，可以保持已分配的绝对 callback
并重算相对等待；越过已分配时隙或次序无法证明时，恢复完整窗口事务并最多
回退一次。这个规则尚未接线，且还需证明 prepare 和 demand 信用在每核、
预热边界及跨 Q 的保持/撤销正确。不能使用 gem5 OoO 时间，也不能通过
逐周期运行器或整窗迭代解决。

现有 FLAC group 漏斗中，18,894 对具有普通功能 facts 的同线候选里有
18,829 对因同 Q 其他同线事件拒组；这是 group 候选的特定口径，不能外推
为所有 store 的比例，但提示简单排除混合及同线事务可能导致覆盖极低。
在取得真实混合窗口的可证明覆盖和人口数据前，停止扩大这个合成切片。

## 真实 C4 功能输入抽样与最小阻碍（2026-11-05）

读取同源 FLAC C4 测量起点后每核各 8192 条 FST 热记录，仅使用
地址、功能标志和物理地址位；脚本及结果位于
`tmp/spr-prepare-mixed-c4-investigation-20261105/`，身份和起点来自
`tmp/spr-store-group-c4-neighbor-frfcfs-20261105/on.json` 的 v2 身份记录。
各核顺序是各自退休记录序号；这些样本**不处于同一真实时刻，也不是同一
FastSim Q**，因而不能据此推断 Q 内覆盖率、跨核无竞争或时间先后。

| 核 | 纯 load 记录 | 纯 store 记录 | 不同读 line | 不同写 line | 本核读写共有 line |
| --- | ---: | ---: | ---: | ---: | ---: |
| 0 | 2383 | 298 | 21 | 20 | 0 |
| 1 | 1282 | 1038 | 11 | 5 | 5 |
| 2 | 822 | 273 | 19 | 5 | 5 |
| 3 | 624 | 39 | 157 | 6 | 0 |

该样本表明真实功能流包含混合读写和密集重复写；原隔离 fixture 的单核、
全 store、不同 line、EOF 子域不足以代表它。抽样没有读取或重放 gem5
OoO 时间、预取命中或访存结果。真实 FastSim Q 的分布尚未采集，不能把
前面的窗口结果解释为单 Q 内的请求次序。

当前隔离实现的阻碍是具体状态边界，而不是只缺一个开关：其 prepare
信用 vector 仅活在一次 EOF 窗口，不能携带跨 Q 在途 hint；一个 memory
事件只有一份 feedback，不能同时保存 hint 和 head 的结果；head 回放
会立即更新私有 cache 和共享目录，而最终退休与 send 直到 feedback 后
才得到；现有孤立试验的失败恢复只处理单核 totals。直接去掉限制后，混合
load 或其他核同线请求可能观察到模型中不应提前可见的写，或使容量提前
释放。FR-FCFS 修复目前按一个源事件对应一次层级请求组织，不能把它直接
当成 hint/head 双事件的认证器。

可继续的有限垂直切片：先显式限 FCFS、已知普通功能 facts 和未启用互斥
路径，同时保留真实 scheduler/MMIO 语义；覆盖候选 Q 的所有 store，使用
每核持久的有界 prepare 完成日历，为每条 store 保存 hint/head 两份结果；
将该 Q 的所有核普通请求、hint 和 head 组成按模型时间一次稳定排序的
事件表，在已有事务中执行。反馈后检查最终 head send 仍处于已分配次序、
服务及所有活跃核的未消费水位证书内；否则恢复共享层级、所有受影响核、
两类信用、FB shadow 和统计并最多回退一次。先做真实 Simulator C4 混合、
同线、跨 Q 和预热夹具，再对相同 FCFS 配置的完整 FLAC 测量覆盖与吞吐；
FR-FCFS 需要后续支持双事件人口，不能借 FCFS 结果声称正式配置通过。
这仍是待实现方案，不能将独立单核试验或功能样本当作 C4 验证。

### 独立 typed-event 账本原型

`tmp/spr-prepare-mixed-c4-investigation-20261105/prototype/typed_ledger.py`
是独立机制实验，不是 FastSim 主路径或 CPU/cache 模型。它把已知 Q 里的
hint、普通 load 和 head 加入有限大小的事件集合，使用稀疏完成事件堆和
每核独立 prepare/demand 信用；完成事件可跨 Q 保留。测试模型只在 head
完成事件更新一个用于观察次序的 line 版本号；不建模字节值或真实一致性。
候选批次失败后整批恢复，demand 信用已满时保守回退，不逐周期重试。

5 项测试已通过，命令及完整输出保存在同目录 `test.log`。它们覆盖四核
同线读写交错、hint 满额不阻止 demand、head/load 的共享 demand 信用满额
回退、跨 Q 未完成事件、最终 send 不匹配回退和水位单调性。此原型的
最终 send 和全局未消费事件水位由调用方作为已认证输入提供；现有
Simulator 尚未提供完整的双事件预测及其最终认证，因此结果不证明真实
C4 窗口可安全接入。实验使用状态深拷贝确保回退，生产实现必须使用现有
可还原事务及有界增量状态；尚未测量或宣称满足吞吐目标。

### 主路径接入所需接口（未实施）

1. **阶段一输出与候选物化。** `BatchPending` 仍保留原始 memory event 与
   接受序；在 `prepare_buffer_tso_epoch` 之后、开始层级回放之前，仅对开启
   试验的窗口生成一个有界 typed 描述符数组。描述符应携带 `(core, source
   sequence, fragment, role, model issue)`，role 至少区分 hint、head、普通
   demand；同一源 store 的两个 role 使用同一功能事实但独立标识。head 的
   初始时间来自已有阶段一退休预测及模型 FIFO 下界，不能借原生 OoO send。
   单次稳定排序后按模型事件消费；若依赖 scheduler 的源水位尚不可证明，
   本 Q 不启用候选，仍保留原 scheduler 和 MMIO 路径。需确定每 Q 请求数量
   上限，超过上限时回退，不能按全 FST 规模构造数组。
2. **阶段二结果。** 现有 `MemoryReplay` 与 `event_feedback[core][index]`
   只能存每个源事件的一份结果。候选需单独的 per-Q side table，保存每个
   store 的 hint 接受/丢弃及 callback、head 服务结果和预测 send；普通 load
   仍用原反馈。`compute_timing_feedback` 只从 head 的真实服务结果计算该条
   store 的等待并输出最终 send，hint 不能再次扣减 feedback。若最终 send
   跨过模型中已排列的其他事务，或 source issue/final 边无法证明，则恢复
   并回退；不能通过反复重算整窗求解。
3. **信用和未来事件。** 需要每核跨 Q 的有限 prepare 信用与完成事件，
   hint 满额只丢 hint；head 和普通 load/store 使用同一 demand 信用人口。
   新 hint 到达时按事件时间释放已完成信用；不存在服务边时不能伪造
   callback。预热向测量切换时保留未到期状态，记录每核覆盖和回退原因。
   若某个 Q 无法处理特殊或未知 store，整个受影响候选走原路径，不能把
   它从资源竞争中静默删除。
4. **事务及发布。** 复用 `SharedSystem::Transaction` 的私有 cache、LLC、
   directory、DRAM、CHA、MSHR/transient 与审计前像；同时按已有 store group
   事务模式保存所有受影响核 `CoreCounters`、以 `(core,line)` 为单位保存
   FB shadow 前像，并快照新增 prepare/demand 信用、pending typed callback、
   被修改的独立统计及仅候选启用的旁表。试算 feedback、SQ、发布边保持在
   `TimingFeedback` 局部，确认后才合入；不可撤销的文件输出应在认证后。
   `publish_buffered_tso_stores` 不能对已在候选中真实执行的 head 再做第二次
   脏发布或重复记退休/层级人口；必须按 role 明确选择一次最终发布路径。
5. **跨 Q 水位与回退。** 可参照 group 路径的
   `ensure_epoch_lookahead`、每核首个未消费 memory envelope 和统一参考时钟
   取最小值，但已知 `sync_frontier_known_` 会使 lookahead 跳过对应核，预热
   的 `finished_` 也不等于永久 EOF；这些边界须令候选跳过或使用额外可证实
   的证书。事务内的最终请求和待发布 callback 必须与当前已消费请求及未来
   水位一致；任一检查失败，恢复所有状态后按旧序至多回放一次。模型允许
   时序近似，但不能把后发写入提前暴露给先前请求。
6. **配置与验证。** 第一版先显式互斥尚未接线的 FR-FCFS、group、硬件预取、
   下层 I-fetch 和 page-walk 组合，形成同配置 OFF/ON 的 FCFS 限域试验，
   而不是声称覆盖当前正式配置。测试应包含真实 Simulator 四核混合 Q、
   同线跨核访存、两个独立满额、跨 Q 与预热、scheduler/MMIO 存在时安全
   回退、权限失效以及一次回退后的 cache/目录/FB shadow/请求与退休人口。
   输出接受/丢弃 hint、真实 head、fallback 原因、独立信用高水位和耗时，
   再对完整 FLAC 测试，不能把独立原型当作准确度或吞吐结果。

### 真实 Simulator 混合接线首轮尝试及止损

在 `tmp/spr-prepare-mixed-real-20261105/work/` 创建独立源码/构建副本，
没有覆盖主工作树的源码、配置、CMake 或 build。新增真实 Simulator RED：
17 条不同 line store 中插入普通 load，要求 16 个 hint 接受、1 个丢弃、
17 个真实 head、18 个真实 demand 及全部退休人口。旧的单核全 store 试验
在该测试失败，日志 `red-test.log`。

首轮尝试在隔离副本中加入 per-core hint callback 状态、混合 batch 的一次
回放、各核反馈检查、全核未消费事件水位和一次恢复。它可编译，但混合 RED
在 `attempt1.log` 仍失败，不能当作功能实现，更没有证明 C4 或吞吐。尝试
保持在临时目录，**未合入，也不应启用**；该原型仍有待查明的反馈和事务
完整性，不能作为生产补丁。

特别是跨 Q 时，旧 `replay_memory_event` 在 hint 发起阶段已经改变 private
cache/共享目录。若 hint callback 晚于 Q 的可证实水位，下一 Q 普通 demand
或旧路径回退仍可能观察到尚未返回的数据；仅保存每核 16 个完成时间无法
修复。安全开放跨 Q 在途 hint 至少需要按 `(core,line,generation,response)`
保留状态，使所有后续普通请求在真实响应前按模型规则等待、合并或取消，
且该状态与现有 cache/目录/Sequencer 事务一起回滚。

一种更窄的替代是只允许所有 hint 在全核未来水位前完成，但该条件的真实
覆盖目前没有可复用的逐 Q 证据；当前原型还要求整 Q 的请求不重复 line、
不经过 scheduler，而实际 FLAC 功能样本具有大量重复读写，正式 C4 又有
thread-state scheduler。样本并不同步且不是 Q，不能推断该候选的真实
覆盖比例或直接声称零覆盖。根据本轮止损条件，不继续扩展该受限原型；
后续应先完成上述跨 Q 可见性接口或取得同配置真实逐 Q 覆盖证据，再选定
可用子域，不以合成窗口通过替代完整验证。

### 现有 shared/private pending 基础核对

复核 `src/simulator.cpp` 后，现有基础不能直接把跨 Q store hint 安全接入
当前真实 Simulator。`reserve_pending_fill` 可以为共享 line 分配代次和响应
下界，但假设上游已经处理 cache/目录影响，并要求同 line 没有已有 owner；
`publish_pending_fill` 只绑定不可变的模型 response，真正的 LLC 填充由
`complete_pending_memory_fills` 在完成事件处理。两者都不单独完成 private
cache、写权限和同线竞争者的可见性处理。

`submit_pending_memory` / `advance_pending_memory` 有真实的共享控制器、
LLC 填充和事务快照基础，但目前只由测试 probe 使用；Simulator 构造明确
拒绝所有非 `off` 的 `cross_q_service_mode`，因为 core continuation 未接入。
开启该基础后，`SharedSystem::access` 遇到响应尚未确定的同线 pending
会明确抛错，不会为当前核或其他核的 load/head 自动等待或合并。已有
`PendingFillTable` 只在每核反馈中约束响应边，无法撤销 canonical 回放时
提前写入的 private cache 和目录。测试 probe 对 pending 的拒绝、代次和
共享事务恢复有覆盖，不等于真实混合 C4 已具备 continuation。

`MixedDramController::advance` 使用稀疏事件推进，不应简单说它本身是逐周期
执行；不过将它完整接入现有运行器仍需要所有核共享请求的可证实发现水位、
挂起后续 core 请求和恢复 feedback、同线权限与跨核失效及跨 Q 联合事务。
其 `advance_pending_memory` 接口也明确要求所有外部 memory/dirty eviction
的独立知识水位，Q horizon 本身不足以证明这一点。绕过构造器拒绝或额外
建立一套详细逐事件 CPU 主循环都不能算两阶段内的安全小改。

因此本轮停止接线：未修改共享源码或构建，也未声称现有 pending 能安全支持
prepare/head。未来若继续，应在现有两阶段 feedback 接口内先设计能处理
多核同线等待、权限 generation、private/目录回调与事务回退的有界摘要，并
证明它与同步、预热和普通请求的事件水位闭合；不能仅依赖 shared pending
的代次或将单元 probe 的通过当作实际 Simulator/C4 通过。
