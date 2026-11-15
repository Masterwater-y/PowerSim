# SPR 预取组件实施记录

**审查补丁注意：** `tmp/spr-l1-ready-isolation-20261105/pending-q-review-only.patch`
基于较旧隔离快照生成，`src/main.cpp` 的 hunk 会删除主线后续添加的
多组 store、I/D STLB/global/大页配置输出，并包含 `FASTSIM_TEST_*` 注入。
它仅供追踪已测原型，不能直接应用；主线未发生这些删除。若继续实施，
需基于当前主线重新整理，并验证全部事务状态和关闭路径。

**2026-11-05 就绪性修复状态：** 原受限 L1 预取路径可能把需求 miss
提前安装的 L2 标签当成已完成数据。真实 Simulator 样例中，模型需求在
周期 5 发起、周期 113 返回，候选在周期 6 即发出；这是模型时序问题，
不涉及 native OoO 时间。曾试验默认关闭的 `core.spr_l1_prefetch_ready_guard`：
按核有界记录模型尚未返回的行，在候选检查时拦截；表满时对该核
保守阻挡直到遗漏请求的最晚已知响应。模型预测 response、每核 ready 状态
与预取状态同属于现有两阶段流程，不增加逐周期运行器或收敛求解。

**该 guard 已从主工作树撤回。** 独立真实 Simulator 依赖链复现：阶段一
预测响应约为 115，但 feedback 将 issue 推迟、最终模型响应推迟到 222；
候选在约 130–131 时仍被错误放行。单纯在 feedback 后更新 ready 表只能
保护下一 Q，不能撤回同 Q 内已发出的请求。复现和日志分别在
`tmp/spr-ready-guard-review-20261105/witness.cpp` 与 `witness.log`。
需要对当前 Q 的候选建立最终响应认证，或在结果未确认时保守阻止发起；
不能将该原型视为安全保护。旧 L1 预取实验路径本身仍默认关闭，且仍有
提前标签问题。
撤回后重建的 `build/fastsim` SHA-256 为 `050b6958b5de7ccc90b640f7c7800984b7ce01b67bd452c0793ce03e70b137df`，
与 guard 接入前用于四案 store 对照的冻结二进制一致；完整主测试和专项
回归在撤回后重新运行并通过：统一构建、`fastsim_tests` 及 14 个相关
Simulator 专项均成功。

要继续接线，需要为每个请求导出唯一被接受的模型最终 callback，并在
有界 Q 内对候选之前或之后的同线需求及跨核写建立保守摘要；先前 Q 的
未完成预取也必须防止在当前 Q 冲突前错误发布。事务回退应恢复 cache、
目录、predictor、预取服务及其 callback 身份。`TimingFeedback` 现仅
导出逐事件 issue 位移，不能覆盖所有 admission 和合并分支的最终回调，
因此不能由阶段一响应和位移简单相加充当认证。具体代码位置及验证边界见
`tmp/spr-l1-ready-isolation-20261105/final-callback-interface-gap.md`。

后续只针对旧 pending 做了一组隔离真实 Simulator 测试：核 0 的预取在
模型时间 119 发出、预计 139 完成；epoch 124、模型时间 125 时核 1 的
同线访问触发冲突并注入 callback 未认证。pending 从 1 被取消到 0，事务
恢复后为 1，再保守停止预取并取消到 0；恢复前后记录的 predictor 训练
计数均为 5、待处理队列均为 0。两核的 L1/L2 access/miss、模型 cycles、
退休指令和 load 人口，以及共享 LLC access/miss 与同排序、禁止预取发起
的基线一致。日志为
`tmp/spr-l1-ready-isolation-20261105/pending-q-witness.log`。

这组测试尚未逐字段比较全部 cache 行、目录和 predictor 内部状态，也未
证明全部 PMU、异常路径或正式 FLAC 覆盖与吞吐。仅供审查的
`tmp/spr-l1-ready-isolation-20261105/pending-q-review-only.patch` 仍含测试
注入、未注册的独立测试及显式受限配置，**未合入，不应直接用于生产**。

对上述隔离 guard 做了可用性筛选：同一冻结二进制和完整新 FLAC C4 功能流，
v2 输入身份均为 `sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`；
两侧相同的受限配置均使用 serial TSO、FCFS 和自主 L1 预取，仅切换 guard。
测试注入环境变量均已从进程环境移除。逐核退休 macro/UOP/load/store 人口
相同，workload-task 合计分别为 31246569/34930034/5985619/968958。

| 指标 | guard OFF | guard ON |
|---|---:|---:|
| 模型预取发起 | 3792 | 311 |
| 模型预取完成 | 3792 | 271 |
| 未认证或冲突候选阻挡 | 0 | 554575 |
| 已发预取因冲突取消 | 0 | 40 |
| 表溢出 / 关闭回退 | 0 / 0 | 0 / 0 |
| 退休 L1 load miss 估计 | 4333 | 7564 |
| task-active CPI | 0.343750 | 0.342039 |
| 同源 gem5 CPI 的算术绝对误差（cycles/macro） | 0.061878 | 0.060167 |
| 同源 gem5 CPI 的算术相对误差 | 21.95% | 21.35% |

guard 使发起/完成分别减少 91.80%/92.85%，L1 miss 估计增加 74.57%；
这个整 Q 行冲突摘要丢弃大量机会，按当前筛选条件停止，不整理或合入
生产补丁。表中 gem5 CPI 为同源先前已验证的 0.281872；此次新二进制
输出尚未独立重评分。对同源 native L1 参考 3898 的算术相对误差为
11.16%/94.05%，该参考仍标记 incomplete-evidence；L2/L3 的 native
unknown 仍使其不可评分。serial TSO/FCFS 不是正式目标配置，结果不能
推广为硬件 PMU 认证或目标精度。两侧并发进程墙钟均约 203.5 秒，不作
吞吐验收。输出、配置、二进制和日志在
`tmp/spr-l1-ready-isolation-20261105/flac-screen/`。

### 阻挡候选留队的隔离筛选

为避免整 Q 冲突 guard 直接丢弃候选，隔离原型尝试将被阻挡的候选留在
现有每核有界 predictor FIFO：后续合格需求事件触发重查，只有最终模型
callback 已到且本核 L2 仍有该行时才发起；没有逐周期重试。默认 host
过期阈值为 0，即不按时间过期；队列容量和去重沿用现有 host 上限，
不宣称这些是 gem5 原生预取队列参数。该版本只在隔离副本，未合入。

定向验证中，原误发 witness 的最终模型 callback=222，候选提前发起数
为 0；另一个真实 Simulator fixture 在后续需求事件到来后成功续发，
两侧退休 load 均为 8。先前 Q 已发 pending 的两核冲突与一次回退 fixture
仍通过。组件测试覆盖目标失效、快照恢复、有限队列溢出及显式 host
过期；这些定向结果并不证明完整路径或目标硬件等价。

完整新 FLAC C4 使用同一隔离二进制，v2 输入身份一致，逐核退休
macro/UOP/load/store 人口相同。两侧 guard 均开启、serial TSO/FCFS、
host 过期阈值均为 0，仅切换候选留队：

| 指标 | 直接丢弃 | 留队重查 |
|---|---:|---:|
| 预取发起 / 完成 | 311 / 271 | 112 / 112 |
| 被阻挡的检查次数 | 554575 | 4632981 |
| 留队候选后实际续发 | 0 | 0 |
| 队列峰值 / overflow | 2 / 0 | 64 / 4476 |
| 回放结束时仍排队 | 0 | 26 |
| 退休 L1 load miss 估计 | 7564 | 7711 |
| task-active CPI | 0.342039 | 0.344935 |
| 对同源 gem5 CPI 的算术绝对误差（cycles/macro） | 0.060167 | 0.063063 |
| 对同源 gem5 CPI 的算术相对误差 | 21.35% | 22.37% |

留队侧统计 `deferred=9100`、目标已在 L1 后出队 9101、实际续发 0、
过期 0；暖机后计数重置但队列延续，后续出队数因此可以比本 ROI 新
留队数多。阻挡次数是重复检查次数，不能当作独立候选数。留队侧 FIFO
达到容量并丢失新候选，发起/完成量和 L1 估计均未改善，CPI 增加
0.002896 cycles/macro。按筛选约束停止该 FIFO 近似，不合入。

同源 gem5 CPI 0.281872 和 L1 参考 3898 来自先前验证；本次二进制尚未
独立重评分，L1 对该参考的算术相对误差为 94.05%/97.82%，仍是
incomplete-evidence；L2/L3 native unknown 未消除。serial TSO/FCFS
不是正式目标配置，且并发墙钟不作吞吐验收。配置、二进制和输出见
`tmp/spr-l1-ready-isolation-20261105/defer-screen/`。

该撤回前试验仅允许 serial TSO、FCFS、关闭 I-fetch 下层及 L2 shared
prefetch；不代表 buffer TSO 或正式四案的预取已修复。隔离版 Simulator
已验证同窗口提前阻挡、同线被替换后再次 miss 及跨 Q 响应后放行；跨 Q
响应前阻挡只在组件验证，真实事务回退和失效链仍未证明。普通 demand 对
提前标签的行为未由该 guard 改变。此前未使用 guard 的完整负载预取数字
仅为历史诊断，必须用修复后的相同二进制重跑；也不能把 serial TSO 与
buffer TSO 不同配置的 CPI 差异归因于 guard。撤回前统一主构建、
`fastsim_tests` 及 14 个相关 Simulator 专项回归通过，但这些测试未覆盖
后来的错误 witness，不能证明实现正确。

使用冻结二进制 `fb266f48…` 与同一完整 FLAC C4 功能身份及逐核人口，
三组同为 serial TSO、FCFS、关闭下层 I-fetch 和 private preview：

| 预取模式 | FastSim CPI | 对 gem5 0.281872 的绝对误差 | 相对误差 | 模型退休 L1 load miss | 发起预取 / 未就绪拦截 |
|---|---:|---:|---:|---:|---:|
| 旧路径，guard OFF | 0.343750 | 0.061878 | 21.95% | 4,333 | 3,792 / 0 |
| 预取与 guard ON | 0.343750 | 0.061878 | 21.95% | 4,333 | 3,792 / 0 |
| guard ON，抑制预取发起 | 0.343689 | 0.061817 | 21.93% | 7,693 | 0 / 0 |

有预取时该次 FLAC 没有触发 guard 或溢出，前两行输出相同，不能把
该样例作为预取就绪性错误已经在实际 FLAC 发生的证据。抑制发起对照保留
训练及相同 replay 排序，仅切换 `suppress_issue`。同源 Ruby L1 已知
值为 3,898、unknown 为 0，模型 L1 miss 的暂定相对误差为 11.16% /
11.16% / 97.36%；事件资格仍为 incomplete-evidence。L2/L3 native
unknown 为 963/971，不可评分。新输出未独立 native 重评分；原生参考
复用同源已验证报告，计时起点不严格相同，并发运行不用于吞吐结论。

上述三行来自现已撤回的 guard 冻结二进制，保留为受限诊断，不作为当前
主工作树已修复的证据。即使该 FLAC 没有触发错误，依赖链 witness 已证明
该试验实现会误发；整个预取路径尚未完成修复。

依据 `spr-store-prefetch-tbe-two-stage-design-20261105.md`。本增量是模型接入前的独立组件，不会改变模拟器行为、发起缓存请求或使 CPI/PMU 改善。

- `SprIpStride`：16 组 4 路、目标 PC stride，实际提取本仓库 gem5 源码运行 30 万次差分验证。
- `SprStreamPrefetch`：目标 8 项正/负单位和非单位 miss filter、四路 stream、启动和访问后延续，64B line、4KiB 页、阈值 4、startup 1，实际提取当前 gem5 源码对 25 万个事件做差分；源代码 requestIssued/requestCompleted 在本仓库当前无写入，按现有源实现保留该行为。
- `SprRubyPrefetcher`：可复制的 predictor/候选 FIFO、同队列去重、IP 开关和 L2 相邻行生成；不假设排队上限或修改任何缓存。
- `SprPrefetchInflight`：按调用方给定上限维护在途摘要，按完成时间发布；只有 issue<=请求时刻<完成时刻才能同线合并，可复制支持快照。调用方必须拥有共享额度、服务与缓存发布，摘要自身不是目标的独立资源。

验证：`python3 -m unittest discover -s tests -p 'test_spr*prefetch*.py'`、`python3 -m unittest discover -s tests -p test_spr_ip_stride.py` 全通过。前者覆盖 FIFO 去重、正负/非单位 stride、页界、snapshot、请求前与完成后不能合并、候选不能提前可见。差分限定为同样输入事件与同样时钟，并未验证 gem5/FastSim 输入事件顺序一致。

尚未实现：候选从 FST metadata 进入阶段二，层次/资源请求的发起、投机 preview 快照/回滚、完成回调时 cache/coherence 发布、跨核失效、统计和完整 A/B。共享状态 `SharedSystem::access` 会在请求时修改目录/LLC；候选若用现有常规 `PrivateHierarchy::access` 会提前安装数据，所以不能简单调用常规 demand 路径。接入需使用 `probe` 与 `complete_fill` 分离准入与发布、登记相应共享事务和 credit，并使权限证书失败时预测器/队列、inflight、目录/cache和计数一并还原。默认配置和用户结果目前保持不变。

## 私有层按完成边发布接口

`PrivateHierarchy::publish_prefetch_fill`（L1+L2）和 `publish_prefetch_fill_l2`（仅 L2，绝不安装 L1）都接收 line、issue、completion、now、permission_valid、专用 prefetch 计数和可选 private transaction：仅 `now>=completion` 且调用方确认当下 coherence 许可时，以 `complete_fill` 安装只读数据；返回 L2 驱逐及脏位让调用方走原共享目录/写回；使用专用 prefetch counter，不产生 demand lookup。`completion<issue` 拒绝。私有 cache 的事务快照可回滚此次安装；pending 摘要及共享目录的撤销由外层事务负责。permission_valid 必须由共享目录/代次验证，不能只用原发起时的状态替代；调用方需保证各事务代次只完成一次，重复调用会对现有行更新替换状态。该接口未接入 simulator，也未实现共享事务发起、取消、L2 独立预取或参考 PMU；不能视为预取已运行。

测试：test_cache_probe_fill 定向测试覆盖完成前不可见、许可失效不安装、完成时安装、L2-only 不安装 L1、脏 victim 返回、计数不计 demand、事务回滚及无效时间拒绝；直接编译 `src/cache.cpp tests/test_cache_probe_fill.cpp` 并运行通过。

## 事务/目录独立审查与后续接入点

新增 `SprPrefetchService` 为按有界容量保存的跨窗口摘要：`begin_at` 只接受
已到达的请求，未到时返回 deferred，禁止回退时间；响应在实际模型结果产生后
选择，到期前不能发布；票据在活动状态唯一，重复同一响应幂等、冲突响应拒绝，
过期票据不更新其他代。不同核同线允许各有 private 请求，SharedSystem 仍必须
自行正确合并共享物理事务；该摘要不代表额外硬件 credit。其窗口 frontier
要求接入者先证明 stage-two 事件顺序全局非降，不能以程序序/单核最近 issue
假造。局部 retry 不阻塞其他到期项；事件发布异常要求回滚整个外层 batch。

`SprPrefetchGrantTable` 只跟踪有活跃预取的 line，记录外部分配、不可回滚
复用的 epoch 和 sharer 位图。任何该 line 的目录授权、失效、写入导致的
owner/dirty 更新都必须推进 epoch，即使 bitmap 又恢复原样；不存在该 line
时返回 unknown，而非将其视为可安装。仅等所有该 line owner 退出后允许
forget。回滚只恢复表内容，epoch 及 prefetch ticket 分配器必须置于外层
事务之外且绝不重用；已撤销的事务事件必须先经 service 活动 ticket 校验，
单靠 bitmap 或 epoch 表不能过滤从被丢弃的事务外泄的异步 callback。

`publish_spr_shared_prefetch` 在 LLC 数据模型实际返回后，调用真实
`SetAssociativeCache::complete_fill`，并将干净和脏 victim 在该完成边交给
外层目录/写回回调；发起时应使用 `probe`，禁止现有 eager `access` 把
预取 miss 提前安装到 LLC。`publish_spr_prefetch` 分别处理 L1+L2 和 L2-only
private 发布，要求外层在 private 返回边重新核验 grant 与数据新鲜度，并把
L2 驱逐转交 shared owner。LLC 完成与 private 完成是不同事件，不能用 LLC
早到边直接当作 private 可见时间。

接入 `src/simulator.cpp` 的实际点：

1. stage-one `chunk->uops.push_back(bound)` 附近对开启模式的 load 记录 PC、
   首片功能虚拟/物理映射和请求资格；通过与 `llc_audit_member` 相同的
   ResidentCoreBuffer 分段生命周期保存，预热不清预测状态，缺事实不猜测。
2. 当前 `SharedSystem::access` 的 LLC miss 调用 `llc_.access` 会立刻安装，
   不可用来发起未完成的预取。独立 `submit_pending_memory` 和
   `advance_pending_memory` 仅在 cross-Q 状态可用，普通 `access` 对 response
   未知的 transient 直接抛错；它们不能未经兼容改造便用于默认两阶段路径。
   应在现有阶段二批次中以 probe/credit 入队，真实共享模型返回后使用上述
   完成桥；同线 demand 在完成前依 pending service 合并或等待而非当作 hit。
3. 目录状态更新集中在 `state_only_private_evict`、`private_evict`、
   `complete_sequencer_write`、`access`、`state_only_llc_evict` 和
   `handle_llc_eviction`，以及 page/state seed；必须逐一标注真正提交的写入
   和无效化，不能只在单个 `entry.remove` 打钩。当前 `DirectoryEntry` 本身
   无 generation，因此不能用 `sharers_of` 单次检查证明同一 grant 未经历 ABA。
4. `SharedSystem::begin_transaction/restore` 保存 LLC、目录 undo、private cache、
   DRAM/credit 和 context counters；predictor/FIFO、service summary、grant 表及
   prefetch 专用 counters 应同时快照；全局 ticket/epoch allocator 不回滚。
   callback 抛错或权限证书失败时还原所有这些状态和写回；返回 retry 时不得
   改变任何 cache/directory/credit。调用时需使用实际选定的 response 作为
   发布时间，而非 submit_pending_memory 的 minimum-return 下界。
5. 把共享填充后生成的 dirty victim 交 `handle_llc_eviction`，private L2 victim
   交 `private_evict`；两者已有目录和写回事务路径。预取账目独立于退休
   demand/PMU，不能通过调用普通 replay 后仅修改计数名称来伪造人口。

独立审查测试：`test_spr_prefetch_grant.py` 覆盖失效→重新授权 ABA、事务
回滚后的外部分配新 epoch、未知 line、容量和超范围核；
`test_spr_prefetch_service.py` 覆盖未选响应、提前 issue、迟到 duplicate、
完成顺序、不同核同线、stale ticket、局部 retry 和容量；
`test_spr_prefetch_shared_publish.py` 使用真实 LLC 测试返回前不可见、后续
访问改变 victim、dirty WB 回调和外层回滚，`test_spr_prefetch_publish.py`
使用真实 private hierarchy 测试许可 unknown/revoked、脏 victim 和事务。
目录/写回是可控测试替身，不是实际 SharedSystem 集成验证；CPI/PMU/吞吐
均未测，预取继续不能开启。

## 受限的 L1 预取模拟器接入（待统一构建验证）

新增实验配置 `core.spr_l1_prefetch_l2_only`，默认关闭。它只对功能信息完整的普通单行 load 训练；请求 facts 不完整时跳过，并排除 secure、split 请求。候选已经在本核 L2、尚不在 L1 时按建模的 L2 响应时间发布至 L1，发布前不可见。目标不在 L2 的候选计入 `unsupported_l2_miss`，并不发往 LLC/DRAM；预测流的 `isPrefetched` 延续行为、硬件端口/TBE 竞争、完整 L2 预取及跨核目录 generation 均未实现。host queue / inflight 限额是软件资源边界，不是 gem5 硬件容量；输出区分训练、发起、完成、取消、溢出、未支持及残留。功能输入来自 PC、功能访存与功能翻译片段，能否由独立 Atomic/QEMU 采集器覆盖全部工作负载还需要单独验证。

该切片明确拒绝下层 I-fetch、private preview、inclusive LLC、层次页表遍历、page/boundary cache seed、动态变频、cross-Q service、竞争 solver、bank/load-fill/store-window feedback prepare lifecycle、FR-FCFS 和后置因果/响应时序重排。普通 legacy buffer TSO 支持保留：缓存事务失败时同时恢复预测器、队列、在途摘要和计数。写之前取消对应行的预取，L2 淘汰时取消对应本核预取；发布时再次检查 L2 存在。FR-FCFS 重排既不能代表预取脏写回，也未证明与同批预取发布顺序一致，当前直接拒绝该配置。阶段二对输入事件按建模时间排序，运行中若时间回退直接报错；尚需多核真实 FST 验证，不宣称通用时间或完整一致性证明。运行结束遗留候选和在途项仅报告，不额外加退休周期。

恢复目标 I-fetch 配置时，不能只把预取发布时刻夹到现有 frontier：下一 Q 中的 I-fetch 原始 producer 时间可能早于此前已发布状态，随后读取未来状态。可评估利用跨核、跨 Q 尚未消费 producer 的最早可服务时间建立保守水位；若只能延后，则所有共享请求和对应响应统一使用同一服务时间，回馈到 core timing，记录推迟人口及周期，watermark 与缓存/目录/预取状态共同回滚。前提是覆盖所有共享状态入口并测试调度、跨 Q 和不同核事件；禁止使用 OoO 参考时序，也不做整窗迭代。目前没有该证明，因此真实用户配置不可静默打开预取；关闭 I-fetch 的 ON/OFF 仅为受控机制诊断，不能称为目标 SPR 精度。

## Shared miss 的独立事务摘要

`SprSharedPrefetchLedger` 是尚未接入 simulator 的有界共享行事务摘要：只接受已到达认证水位的 issue，同一 line 跨核去重；候选的后续 demand 只能拿到合并提议，不能被当作 cache hit。真实共享服务选出 response 后仍不安装缓存，待外层通过认证水位调用 completion 再由目录/缓存/写回事务发布。取消返回 ticket 供外层唤醒或重发 demand，过期 ticket 不能复用；复制快照共享不可回滚 ID 分配器。每次完成处理在容量范围内扫描，不逐周期推进；容量是 host 边界，不代表 TBE 或精确的 gem5 合并策略。

接口明确把“数据选定返回时刻”和“实际发布时刻”分开：若发布延后，缓存、目录和写回在调用方提供的当前发布边修改，不能回溯到早先 response；相应延迟必须由调用方反映给等待需求。caller 必须保证每个共享状态入口都服从同一个安全水位，并在异常时同时回滚摘要、waiter、cache、directory、DRAM、credit、写回和计数。只靠该摘要无法证明多核和跨 Q 水位，也不负责实现 grant 生成、shared miss、合并及唤醒；共享行目录的代次转变及完整 SharedSystem 调用仍需实现。

`test_spr_shared_prefetch_ledger.py` 已使用真实 LLC、独立 ledger 和假的目录/写回，验证返回前不可见、同线跨核去重与 demand 等待提议、dirty victim 回调、回滚后 ticket 不复用、cancel 后迟到响应、容量、retry/取消、时间倒退拒绝、延迟发布和异常恢复。该测试不是 SharedSystem 端到端证明，也未评估精度或吞吐；用户目标配置仍不能打开 shared miss 预取。

## 私有 L2 SLICC 预取事件适配（独立组件）

`SprL2RubyPrefetcher` 只接收模型自己的私有 L2 request/event 与缓存状态，
不读 native 命中或预取结果。针对本 FLAC 的 `ip_stride=false`、4 streams、
8/8 filters、4 次训练、1 个 startup、64B line、4KiB page、queue dedup，
模拟 `MESI_Three_Level-L1cache.sm` 中合资格 miss 的 `observeMiss`→
`observeAdjacent`、合资格 GETS/GETX hit 的 `observePfHit`→清 model
prefetched bit→`observeAdjacent`，以及 `PF_IS + Load` 在 miss/adjacent
之后的 `observePfMiss`；upgrade 不训练。资格或 hit 的 prefetched bit
未知则该次不猜，调用者需要保存/回滚 model cache 标记。队列 drop、retry、
待 eviction 和可发送是分类，不会自行变更共享 cache/目录或发请求。

本案 Ruby `randomization=false`。候选按模型生成时刻+1进入以
`(ready, 原消息序号)` 排序的事件堆；recycle 保留序号和 queued-line
去重，其他已就绪候选仍可先行。为保持吞吐，调用者必须提供**已认证的下一
资源变化下界**，recycle 一次跳到不早于该边，不对每个 target cycle 空转；
未知变化边直接拒绝调度并由调用者等待外部事件或显式 fallback。跳过的
逐周期 native retry 人口不会被该近似重建；开启 randomization 的其他配置
也未表示。票据序号在快照回滚后不复用，过期 callback 不能消费后来同线项。

`test_spr_l2_ruby_prefetcher.py` 覆盖以上动作顺序、多候选 recycle、ready
和相同时间原序号、去重、容量丢弃、未知资格、长距离时间跳转和回滚票据；
底层 `test_spr_stream_prefetch.py` 已用从 gem5 源码抽取的方法做 25 万事件
差分。这个适配器尚未接入 Simulator，也不是与 SLICC 端到端逐事件差分。
要实际产生 L2/LLC/DRAM 预取效果，仍需模型自有的 L2 prefetched-line 标记、
在途 demand 合并、队列与 fill/TBE 竞争、真实 cache/目录完成与失效代次、
脏 victim 写回、跨核跨 Q 安全水位和完整事务回滚；CPI、PMU 与吞吐尚未评估。
队列要求显式非零 host 容量，满额单独计数，不声称这是 native 队列容量。
事件堆与开放寻址去重表在构造和快照复制后预留空间；随机对照及循环复用
测试覆盖去重与队列自身无逐候选分配。底层 stream filter 仍使用 `deque`，
可能分配内存；整个组件尚无 Simulator 集成或高吞吐实测，不能视为吞吐验收。
handle 包含 owner 与 ticket，跨实例/重建或回滚旧 callback 不得推进水位。
retry 必须由影响该候选判定的所有模型事件的最早变化边驱动；line/全局资源
变化可提前唤醒，不能仅等待原 fill credit。通知可扫描 host 容量并重建堆，
大容量或多核快照的空间和耗时尚未实测。异常可能发生在 predictor 更新后，
调用方需恢复完整外围快照；组件本身不保证异常的强回滚。

### 真实 L2→共享层级接线：尚未实施

2026-11-05 再次核对后，没有可直接启用的、真实计流且不会提前公开
填充的最小现成接线。本节限定后续实施路径；当前 L2 预测器仍未进入
Simulator，不声称有 L2 预取准确率或吞吐结果。

具体源码边界如下：

1. `Simulator::replay_prefetch_batch`（`src/simulator.cpp`，当前在
   `spr_l1_prefetch_l2_only` 分支）已有按 batch 接受次序处理 demand、
   L1 预取和事务回退的入口；L2 需要独立 predictor/queue，并在该真实
   接受流中读取模型自己的 L2 hit/miss、有效功能地址和 prefetched 位。
   未知训练资格、翻译、行状态或权限必须保守跳过并分类计数。
2. `SharedSystem::access` 目前一次调用即更新目录 sharer、调用
   `llc_.access`，miss 还会即时分配或驱逐，同时更新普通 CHA/LLC 请求
   计数和 DRAM；不能直接把一个未来的硬件预取包装成普通 demand 调用。
   需要在同一个 SharedSystem 中拆出 typed `begin_prefetch`：实际申请
   已有 CHA/FCFS/DRAM、带宽及信用并记录预取请求，但响应前不安装数据；
   再由 `complete_prefetch` 在模型选定的完成边重新验证权限/代次，安装
   LLC/私有 L2，处理实际脏 victim 写回。共享物理流量照实计入，
   `PerfCacheRequestKind::HardwarePrefetchL2` 只作为独立类型，不能混入
   `DataRead` 的 demand 计数，也不能只更新 shadow 而漏掉真实服务。
3. `SharedSystem::submit_pending_memory` 虽有延迟填充原型，但要求
   `cross_q_service_mode != off`、外部已持有 L2 MSHR 和经过认证的全局
   frontier；其 `instruction/context` 标志也不能表达独立硬件预取类型。
   该路径与现有受限 L1 配置互斥，不能通过直接打开它或把预取伪装成
   demand 完成接线。独立 `SprSharedPrefetchLedger` 亦不维护 cache、
   coherence、DRAM 或 demand 等待，只能作为需要实际接入的账本候选。
4. 允许用户接受的固定窗口/接受次序近似时，可把每个服务时间夹至模型
   单调前沿；夹高的 demand 等待必须恰好一次进入现有 core timing feedback。
   不能用未来完成边立刻安装数据，也不能把队列时间当成未证明的全核
   安全水位。遇到同线 demand 时，需明确等待该在途票据，或取消填充并
   将**已发出的**预取事务继续计入资源/流量，再为 demand 发并计入自己的
   事务；不能让需求提前命中或静默抹去已发请求。相干写、驱逐和 stale
   response 必须按代次取消或拒绝发布。
5. 该首个单遍子域先限制 FCFS、下层 I-fetch 关闭、无原生 OoO 时间，
   并明确 legacy buffer TSO 的发布前沿和冲突回退；不能绕开 TSO 或把
   clamp 之后的 store 完成只写回共享 cache 而不反馈 SQ。outer transaction
   必须覆盖预测器、在途队列、私有/共享 cache、目录、DRAM/信用、
   typed 计数及前沿；失败只允许一次明确的既有回退，不做整窗反复收敛。

首个真实 Simulator fixture 应扩展 `tests/test_spr_l1_prefetch_runtime.cpp`
或邻近端到端 fixture，而不是再增加独立 predictor mock：构造可复现的 L2
miss 候选并证明确有独立 typed LLC/DRAM 请求和目标共享资源占用；同线
demand 在填充前不命中、填充后可命中；覆盖同核和跨核写取消、晚到
response、dirty victim/writeback、不同 batch 时间回退、有限队列和事务
回滚。验证统计上已发事务的 `起始 pending + issued = completed +
cancelled/drained + 结束 pending`，以及 demand/退休人口不因为预取增减；
任何已发取消仍记录下游服务。另设 feature OFF 一致性、源身份、同 FST
CPI 绝对误差与 PMU 可评分检查，并独立测宿主吞吐和回退比例。它是设计
前置，不是当前已有测试或性能结论。

### 单遍 L2 垂直切片的具体提案（尚未接线）

在上述受限配置中复用 `replay_prefetch_batch` 的已接受事件序列，L2
独立预测器只观察模型里的 L2 服务结果和功能地址。预测器候选为物理字节
地址，必须先转换为现有 SharedSystem 的 cache line 单位并检验 DRAM
地址范围；无功能输入、地址或模型状态证明的候选应计数跳过。

在 `SharedSystem::access` 附近增加 typed issue/response 接口。issue
预约同一 `cha_ready_`、slice `llc_mshr_ready_` 和 `dram_` 的 FCFS
资源，并输出独立 prefetch 类型人口；它不能调用立即更新目录和 cache 的
普通 demand `access`。若选择实现 LLC 命中，也必须复用真实 CHA 服务；
保守首版可明确只接受 LLC miss，其余候选统计后跳过。issue 后保存身份、
line、实际完成时刻和失效代次，不提前安装 cache。response 仅在单调
接受前沿到达后提交 LLC 和私有 L2 填充、目录、脏 victim/writeback。

未完成时遇到同线 demand 或写，可以取消对应填充但仍保留已占用的共享
服务、信用和预取计数；demand 沿原路径独立服务。其他相干失效/驱逐须
同样阻止 stale response 生效。尚未证明的权限和回滚一致性必须保守拒绝，
不能把 unknown 当作命中或静默抹去流量。prefetch 在外层已有 buffer TSO
事务中与预测器、pending、目录、cache、共享时间线及统计一起回滚；既有
一次 conflict fallback 执行同一输入，不能引入整窗迭代。跨 batch 若接受
序列的模型时间回退，需要先定义有界 clamp 与 demand timing feedback，
不允许仅更新共享时间线而漏掉核的等待。

首个真实 Simulator fixture 基于现有功能 trace：对照 OFF/ON 的退休
demand 人口，观察 miss 候选的真实 typed LLC/DRAM 服务和共享信用，覆盖
同线 demand 在 response 前后、同线写与取消后仍保留已发流量、脏 victim、
事务回退、容量上限、跨 batch 时间回退和不支持配置。只有该测试接入并
通过后才能声称端到端预取切片存在；宿主性能和真实工作负载仍需另测。

2026-11-05 源码开发切片已接线、等待首次统一构建：
`core.spr_l2_shared_prefetch` 默认关闭，当前只接受 serial TSO、参考同频、
非 inclusive LLC、64B line 和现有 FCFS 单遍预取子域，L1 预取必须禁发；
context、fixed-work、recorded scheduling、`advance()` 和 buffer TSO
均不支持。它使用独立 typed 计数，LLC miss issue 预约现有共享资源，
response 后发布，遇同线需求时取消发布且保留已发流量。跨 batch 时间
回退采用接受顺序前沿，把 demand issue 延后并将等待写入响应反馈，记录
clamp 次数和周期。该切片不是上文 buffer TSO 回滚设计的实现，也不代表
SPR 目标配置可用。

同日单目标编译通过，真实 Simulator fixture 验证了单核需求晚到时的
L2 miss 由 3 降至 2、退休 load 人口不变；需求早到时取消填充但保留
已发 DRAM；双核 fixture 的同线写取消了另一核的在途填充；64B LLC
fixture 的脏 victim 实际进入 DRAM 写队列。诊断版运行日志为
`tmp/spr-alignment-v2-c4/l2-fixture-cross-dirty.log`。此前首版 fixture
因独立指令可乱序执行，驱动和目标需求同时发起，未等到 response；
加功能依赖链后才形成预期的 early/late 两种时序。后续补充的双核 OFF
比较和移除诊断后的源码已统一构建；`fastsim_tests`、L2 runtime fixture、
dynamic hierarchy 和 parallel trace-open 均通过，日志为
`tmp/spr-alignment-v2-c4/l2-shared-final-{build,main,runtime,dstlb,open}.log`。
跨 batch clamp 的实际时序与反馈、目标 buffer TSO、完整工作负载准确率和宿主吞吐均尚未验证；
此前不要把该实验开关作为目标配置启用。

## L1 预取最终 callback 的隔离验证

先前的 L1 ready guard 以阶段一 canonical response 判断私有 L2 数据就绪，
真实 Simulator 反例证明这一判断不安全：`gap=500` 时，模型修正后的同线
需求 callback 为 **222**，候选约在 **131** 发起；旧 guard 仍报告发出 1 次。
该 guard 已从主工作树撤回，不能使用它先前的 FLAC 数字作为修复后的结果。

仅在 `tmp/spr-l1-ready-isolation-20261105/` 的 FCFS、serial TSO 隔离副本中，
尝试了整 Q 同线保守冲突集、旧在途同线预取取消、阶段二最终 callback
记录，以及 callback 未认证时最多一次事务回退。反例中捕获的模型最终
callback 确为 **222**，候选由原来的 issued=1 改为 issued=0、blocked=1；
日志见 `final-callback-witness4.log`。这只证明该反例，不证明完整机制对齐。

通过测试注入模拟 callback 未认证后，首 Q 且没有先前在途预取的真实
Simulator 回退，与同排序的禁止预取发起对照一致：L1 access/miss 为 6/6、
L2 access/miss 为 6/6、模型 cycles 为 441、retired load 为 6。**先前 Q 已发
pending 被取消后又回滚的情形尚未验证**；跨 Q 尚未完成 callback 的阻挡、
跨核写及所有事务副作用也尚未闭合。整 Q 对同线访问保守阻断会损失部分
可行预取机会，覆盖和吞吐成本尚未评估。隔离原型含测试注入，**未合入
主工作树，也没有正式工作负载准确率或吞吐证据**。详细边界见
`tmp/spr-l1-ready-isolation-20261105/final-callback-interface-gap.md`。

## L2 批次状态快照增量（进行中）

共享 L2 预取状态已抽为 `SprL2SharedPrefetchState`。buffer TSO 批次在进入该预取 replay 时记录预测器、候选队列、pending、前沿及计数的副本，既有权限检查失败恢复 cache/目录/DRAM 后也恢复该副本；预测器的票据分配器仍跨副本共享，防止回退后重用旧票据。对已接受的同线 store/atomic，先取消待发布预取再处理到期填充；已发请求占用的共享服务不撤销。同周期及尚未处理的较早完成都保守取消，可能低估预取收益。

`tests/test_spr_l2_prefetch_transaction.py` 覆盖状态副本、预测器队列、pending、前沿、统计、票据不重用及跨核同线取消。当前 TSO 组合仍被配置检查拒绝，因此该恢复分支尚无真实 Simulator 端到端验证；接受顺序前沿如何影响 FIFO 发布和 SQ 释放仍未接入。FR-FCFS 当前的单遍修复只收集 demand 描述符，并不能保留预取和所有脏写回流量，依然拒绝组合。这些增量不是完整接线、精度改善或吞吐结论。

### FR-FCFS 未纳入预取流量的保守回退（进行中）

新增按批次记录 L2 预取期初 pending 与 issued 的有界检查：当批次期初或期末仍有 pending，或该批次有新发预取时，不采用现有仅包含 demand 的 FR-FCFS 重排结果，保留已预约预取服务的 canonical 状态；脏写回也继续触发该回退。`spr_prefetch_dram_repair_skipped_epochs` 记录这类批次，包含原有脏写回原因，不能将其解读为单独预取数量。取消预取填充不撤销已经预约的服务。回退是保守 FCFS 路径，不能称作混合请求的 FR-FCFS。

状态组件测试覆盖发出即取消、跨批次 pending 和完成后恢复无活动。使用临时复制的 `config.cpp` 在隔离二进制中放宽限制：serial TSO + FR、buffer TSO + FCFS、buffer TSO + FR 三组小型 Simulator 夹具执行既有的请求、完成、跨核写取消、脏 victim 与需求人口断言；FR 两组另检查预取流量确实触发本回退。该隔离试验没有改变主配置检查，也未验证目标 workload、完整 FIFO/SQ 时序或吞吐。

隔离的 buffer-TSO FCFS Simulator 双核 fixture 另在预取已发出后通过仅存在于临时 `simulator.cpp` 的测试注入，强制一次权限回退；注入点记录 issued=2、pending=1。回退/未注入两侧所比较的 issued=2、completed=0、pending=1、cancelled=1、trained=3、核 0 L1/L2 miss=3/3、核 0/1 cycles=1081/36、退休 load/store=3/1 一致。文件位于 `tmp/spr-prefetch-buffer-tso-implementation-20261105/rollback-probe/`。这证明该有限夹具中已发预取与 cache/计数没有重复提交；没有逐字段比较目录、DRAM、全部预测器内部状态，也尚未确定 pending 是否跨越回退窗口，因此不能称完整端到端回退通过。

### buffer TSO 的接受前沿与发布边（进行中）

L2 shared 预取回放采用接受顺序模型前沿。对于使用 legacy permission-prepare 的 buffered store，时序反馈中把 SQ 释放和 FIFO 发布边一同限制在该批共享前沿之后；因此 store drain 和后续 SQ 准入看到相同的延迟，而非只在缓存发布时单独夹高。store 实际发布时取消同线未发布预取，并将共享前沿推进到该发布时刻；队列不因取消而删除已预约的 DRAM 服务。该代码仅在 `spr_l2_shared_prefetch` 存在时生效；正常路径保留原行为。

隔离 Simulator fixture 的原始反例是批次前沿 307、两条 store 的 SQ 释放为 123/125；接线后为 307/308。之后实际发布检查发现前沿仍停在 307，第二条 store 发布于 308；补齐发布边推进后，两个检查均见前沿 308，分别对应发布 307/308。`tests/test_spr_l2_tso_frontier.py` 通过临时副本放宽配置来调用真实 Simulator、读取时序审计并检查 SQ release、FIFO 和预取发起；主线配置检查未改。这个窄夹具不证明全部跨 Q 取指、回退、资源竞争或工作负载精度；FR-FCFS 仍为保守回退，不是完整混合调度。

为评估这一保守下界，`O3QueueCounters` 新增 `spr_prefetch_store_frontier_clamps` 和 `spr_prefetch_store_frontier_cycles`，按核与汇总输出；后者为每次 SQ 释放夹高量之和，不是互斥 CPI 分解，也不是宿主吞吐。上述隔离两 store fixture 要求两项均为正。
