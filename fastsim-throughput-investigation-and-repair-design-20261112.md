# FastSim 吞吐量专题：现状证据、排查方法与两阶段修复设计

日期：2026-11-12。状态：设计完成；已核验安装版本、现有源码和历史样本，未实施模型修复，未启动新的吞吐回放。当前样本不足以定量闭合“历史 5M → 当前 200k”的全部来源。

补充：按照用户提出的三个方向，具体设计、并行边界和实施切片集中在第 12 节：输入数据预处理、共享层数据缓存/合并、减少逐记录工作。

## 1. 当前可确认的判断

两阶段框架仍然存在，但框架内每条功能记录承担的读取、校验、资源预约、状态复制和反馈工作已发生变化。两阶段不变并不保证单条指令成本不变；尤其不能保证原有批量快路径仍覆盖新 SPR 配置。当前优先检查的结构性问题是：**新机制进入常用配置后，旧快路径的适用条件没有同步扩展，供数和反馈都需要承担大量逐记录工作。**

现有证据支持以下判断：

1. 200 kIPS 不是单纯的统计幻觉。Zstd 的 M2 原始样本为 191.725 kIPS，改用测量期 user-UOP/s 仍只有 261.214 kUOP/s。
2. 低吞吐早于最新 MOV/zero-idiom 修改。相同输入的冻结 M1 已只有约 0.283 M measured user-UOP/s；最新 M2 对 M1 的 simulator 吞吐变化约 −7.01%，解释不了数量级差距。
3. 反馈物化覆盖很高。M2 的 35,329,964 条测量期功能 UOP 中，34,407,630 条进入 sparse 物化路径，占 97.389%；空闲段证书和整块转移成功覆盖均为零。
4. 当前存在明确的快路径排除条件：`spr_partitioned_iq` 和 `unified_parent_active_` 会拒绝空闲段证书；分区 IQ、buffered TSO 等条件会拒绝相关整块转移。`response_block_summary=true` 仍可减少 ROB 写回，不能理解为已经跳过逐 UOP 反馈。
5. 这个样本没有证实整窗反复收敛或全局单线程退化：reweave、因果迭代、response-retime、buffer-service passes 均为零，FR-FCFS 每个候选仅一次预测，并行反馈调用为 5,703 次。

前三项来自现有原始样本，第四项来自当前源码，第五项由样本计数与调度源码相互核对。它们说明具体检查对象，尚不能给出各机制对历史退化的贡献百分比。来源见 [证据 JSON](computer:///data00/yinhaolang/FastSim/fastsim-throughput-evidence-20261112.json)、[最新配对记录](file:///data00/yinhaolang/FastSim/docs/spr-rename-throughput-regression-20261112.md)、[证书入口](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L12436-L12462)及[整块转移入口](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L13968-L13995)。

## 2. 版本、输入与吞吐口径

### 2.1 对照身份

| 对照 | 身份 | 用途 |
|---|---|---|
| 当前生产 M2 | `build/fastsim`，SHA-256 `0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2` | 专题的默认行为基线 |
| 冻结 M1 | `tmp/spr-m1-20261111/fastsim`，SHA-256 `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905` | 分离最新消除路径增量，并保留历史精度参照 |
| 当前候选 OFF | 同一候选二进制，消除路径关闭 | 区分二进制变化与配置变化；仅用于诊断 |
| 历史高吞吐版本 | 历史文档有 5M–10M user-UOP/s 结果；本次未取得其同输入配对 | 定位长期退化，需要恢复可复现源码/二进制身份 |

当前构建缓存是 Release、`-O3 -DNDEBUG`，LTO jobs 为 16。没有证据把此次数量级退化归因于 Debug 构建。工作区存在用户已有修改，不能用重新构建后的同名程序代替上述冻结执行文件。来源：[M2 默认记录](file:///data00/yinhaolang/FastSim/docs/spr-m2-rename-elimination-default-20261112.md)、[构建缓存](file:///data00/yinhaolang/FastSim/build/CMakeCache.txt#L25-L54)。

吞吐候选应在隔离目录从已冻结、校验过的 M1 来源恢复，再重放已验证的 M2 消除修改，先证明候选与冻结 M2 的目标输出一致。当前新建目录不能直接继承“可信基线”名称；仅有 source-manifest 哈希还不等于拥有相应源码。避免在已经漂移的历史 publish/merge 分支上叠加吞吐修改。

### 2.2 当前六个样本的可比结果

来源为最新 Zstd 两组三版本串行交错测量，CPU affinity 为 24–35。完整 manifest、功能预热和线程计划一致，吞吐运行关闭了可选输入身份哈希扫描。以下每格为两次样本的中位数；这是少次重复诊断，未满足 30 组验收。

| 版本 | 旧 `mips`，MIPS | 测量期 user 宏指令，MIPS | 测量期 user-UOP/s，M | simulator 墙钟，s | process 墙钟，s |
|---|---:|---:|---:|---:|---:|
| 冻结 M1 | 0.206529 | 0.200445 | 0.283095 | 116.556 | 131.916 |
| 候选 OFF | 0.208589 | 0.202441 | 0.285913 | 115.398 | 130.696 |
| 当前 M2 / ON | 0.192053 | 0.185231 | 0.261607 | 125.334 | 140.720 |

M2/OFF 的进程吞吐变化为 −7.12%，simulator 为 −7.93%；M2/M1 分别为 −6.26%、−7.01%。这些变化按每组耗时比的中位数取倒数计算，不能简单用上表两个中位数相除代替。样本、内容指纹、原始计数与公式已保存于 [证据 JSON](computer:///data00/yinhaolang/FastSim/fastsim-throughput-evidence-20261112.json)。

原始第 0 组 M2 的真实人口为：

| 人口 | 宏指令 | 功能 UOP |
|---|---:|---:|
| 测量期全部参与核，user+kernel | 24,070,760 | 35,329,964 |
| 测量期 user | 21,905,561 | 30,937,873 |
| 测量期 kernel | 2,165,199 | 4,392,091 |
| 功能预热，未按权限拆分 | 1,796,434 | 2,551,097 |

当前旧字段 `throughput.mips` 实际为测量期 mixed 宏指令数除以包含预热的 simulator 墙钟；它不是严格的 measurement 或完整 end-to-end 口径。历史文档的 5M 大多为 measurement user-UOP/s，不能直接除以当前宏指令 MIPS。尽管如此，将本样本统一为测量期 user-UOP/s 后仍显著低于 5M，这种规范化不会消除主体性能问题。来源：[速率公式](file:///data00/yinhaolang/FastSim/src/main.cpp#L1547-L1588)、[历史口径修复](file:///data00/yinhaolang/FastSim/docs/fastsim-cpi-throughput-debugging-interview.md#L44-L56)。

新的主报表同时保留：

\[
Q_{\mathrm{user,UOP}}=\frac{N_{\mathrm{user,UOP}}}{T_{\mathrm{measurement}}},
\qquad
Q_{\mathrm{user,macro}}=\frac{N_{\mathrm{user,macro}}}{T_{\mathrm{measurement}}}.
\]

引擎端到端另报全部实际 warmup+measurement 的 mixed 工作量除以 simulator 墙钟；完整进程速率再用 process 墙钟。scored、context、user、kernel、warmup 人口分别声明，不用 `cores × target` 替代。当前 native warmup 未按权限拆分，完整 end-to-end user 速率继续保留 null，不能补造。校验/离线 native 审计时间独立于 simulator，完整进程则保留其实际包含的前处理。

如果“恢复 5MIPS”确指宏指令速率，应将宏指令 5M 与 user-UOP 5M 分列为两个目标，并明确 measurement 或 process 范围。当前 user-UOP/宏指令人口比约 1.412，二者不能互换。

## 3. 已有宿主成本分解

### 3.1 当前 M2 第 0 组

| 不重叠外层阶段 | 墙钟，s | 占 measurement |
|---|---:|---:|
| schedule/batch，包括暴露出来的供数等待 | 42.754 | 36.10% |
| weave，包括共享处理及响应反馈 | 66.411 | 56.07% |
| commit | 8.917 | 7.53% |
| 外层未覆盖部分 | 0.356 | 0.30% |
| measurement 合计 | 118.439 | 100.00% |

三阶段计时闭合 99.699%。此外，simulator 墙钟为 125.549 秒，功能预热为 7.110 秒，process 为 140.992 秒；process−simulator 的约 15.44 秒包括 trace-open、初始化、输出等，不能整体命名为读入时间。

| 嵌套诊断区域 | 墙钟，s | 解释 |
|---|---:|---|
| timing feedback | 39.633 | 未完整覆盖入口预处理及状态复制 |
| domain phase | 32.346 | 部分 worker 工作与屏障等待；不能加到 feedback 之上 |
| FR-FCFS repair | 32.875 | 包含内部 feedback；部分排序/映射前处理位于计时入口之前 |
| state certificate | 0.506 | 当前已计到的证书区域 |

**FR-FCFS 的 32.875 秒不能与 feedback 的 39.633 秒相加，也不能除以 108,333 条 DRAM 请求后声称是 DRAM 计算单请求成本。** `apply_frfcfs_dram_repair()` 会在其计时区域内调用 `compute_timing_feedback()`，因此两者重叠。下轮必须增加互斥的 self/exclusive 计时。来源：[FR 前处理和计时入口](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L22695-L22906)、[FR 内部反馈](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L23127-L23130)、[feedback 计时入口](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L20451-L20469)。

### 3.2 当前已经排除或降级的假设

| 假设 | 当前样本证据 | 处理 |
|---|---|---|
| 最新消除修改导致全部下降 | M1 本身已约 0.283 M user-UOP/s | 只归因其已测增量，长期退化单独查 |
| Debug/未优化构建 | 已安装二进制身份匹配 M2，构建缓存为 Release | 降低优先级；历史控制也需绑定编译身份 |
| 整窗反复收敛 | reweave、causal、retime、buffer-service passes 均为零 | 不作为本样本主因 |
| FR-FCFS 配置 passes=4 所以每窗算四次 | 实际 5,781 个 candidate、5,781 次预测 | 以实际工作计数为准 |
| prepare 导致完全串行 | 5,703 次并行反馈、domain phase 非零 | 查负载不均衡与串行比例，不宣称全串行 |
| 大量空 epoch 是唯一原因 | 656/8,358 空步，已有空步快路径 | 不是已证实主因；仍需区分逻辑步数与真实执行次数 |
| 当前仍是整池记录深拷贝 | 父服务已有记录块 COW、分页索引 | 查真实 detach/refcount/index 成本，不能照搬旧诊断 |

当前 nonempty epoch 平均接受约 4,587 条 mixed UOP、约 2.394 个 active core task。这不证明 epoch 粒度足够好，也不支持“几乎每条指令一个 epoch”的描述。M1 的空步比例反而更高，但速度更快，说明空步比率不能单独解释 M2 增量。

父服务已接受分支的 `copied_index_bytes` 为 1,295,751,680 bytes，约 1.207 GiB；`copied_record_blocks` 为 110,200，索引重建 54 次。它说明累计复制存在，尚未证明其占主导；也不覆盖所有失败试算和进程真实内存流量。来源：[分页 COW 实现](file:///data00/yinhaolang/FastSim/include/fastsim/service_snapshot.hpp#L12-L73)、[父服务提交计数](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L20970-L20986)。

### 3.3 数量级恢复需要多层一起改进

若 200 kIPS 和 5 MIPS 完全同口径，则：

\[
\frac{T_{\mathrm{slow}}}{T_{\mathrm{fast}}}=25,\qquad
t_{\mathrm{slow}}=5000\ \mathrm{ns/inst},\quad
t_{\mathrm{fast}}=200\ \mathrm{ns/inst}.
\]

这要求总成本下降约 96%，不是一般微优化的量级。当前第 0 组 M2 的 measurement user 成本为约 3,828 ns/UOP、5,407 ns/macro；按 user-UOP 5M 的数学目标需约 6.19 秒完成该测量人口，当前为 118.44 秒。这里的 6.19 秒是目标预算换算，不是可达性预测，也不是与历史版本的已测配对。

按当前已计到的 feedback 区域占比 33.46%，假设将其完整消除且其余时间不变，理想上限为：

\[
S=\frac{1}{1-0.3346}\approx1.503.
\]

入口未计到的复制若也减少，可以额外受益，但本算式没有测量那部分。要恢复数量级性能，必须同时解决供数、共享事件处理、反馈及跨阶段数据搬运；不能把旧版某个 2%–3% 微优化重新实现后称为专题完成。

## 4. 原因分级与排查优先级

| 层级 | 问题类型 | 当前证据与待核验点 | 修复性质 |
|---|---|---|---|
| L1 | 口径、宿主、编译和输入范围 | 旧 mips 有混合范围；历史与当前输入不同；本次仅有静态宿主快照 | 先恢复可比性 |
| L2 | 额外宿主工作、数据搬运、校验、共享索引 | `.ffe/.uops/.core` 等处理、COW detach、重复查表、串行供数等待 | 优先做目标行为不变的优化 |
| L3 | 新机制使旧快路径失效、时间跨度驱动扫描、重复反馈 | 分区 IQ/父服务的硬拒绝；SPR 端口 `++cycle` 搜索和 vector 插入 | 先扩展等价批量实现，再评估必要近似 |
| L4 | 已选择的模型复杂度与保真目标冲突 | 全 UOP 因果传播或端口细粒度预约若仍占主导 | 工作量有界的模型近似，单独精度验收 |

L4 不意味着必须放弃两阶段。可在两阶段内用固定容量状态、解析资源压力和一次反馈降低成本。不能将详细流水线、独立因果主循环或整窗重复收敛引回主路径。

三个最值得先采证的对象：

- **供数与伴随校验**：当前 `BinaryTraceSource::next()` 仍会触发静态 UOP、core functional、MMU 等伴随观察；部分构造器全源预检后再次读取同一伴随。schedule/batch 的 36.10% 只是暴露等待，必须用 producer CPU 时间确认解压、hash、projection、分配、前端日历各占多少。来源：[逐记录伴随入口](file:///data00/yinhaolang/FastSim/src/trace.cpp#L2390-L2409)、[全源预检及再打开](file:///data00/yinhaolang/FastSim/src/fst_core_functional_reader.cpp#L136-L211)。
- **反馈快路径兼容性**：以分区 IQ、TSO、父服务、消除 UOP 的实际新状态为边界，设计可证明等价的块摘要。97.389% 是已接受的 sparse 物化人口，不能直接当真实尝试工作次数；应额外记录失败尝试和所有实际扫描。来源：[反馈内核](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L13165-L13229)。
- **资源日历与复制**：SPR 端口搜寻按 `++cycle` 推进，gap-aware calendar 用排序 vector、lower_bound、插入和整理。合法但长等待是否使探测工作随目标时间跨度增长，需要 probes/UOP、max probe、移动字节数证据。存在循环不等于运行已经成为逐周期模拟器。来源：[端口预约](file:///data00/yinhaolang/FastSim/src/interval_core.cpp#L761-L870)、[日历容器](file:///data00/yinhaolang/FastSim/include/fastsim/spr_port_calendar.hpp)。

## 5. 先建立可闭合的测量设施

### 5.1 基线运行协议

先用完整原 Zstd manifest 复现 M1、M2，不沿用旧后台任务，也不修改线程计划绑定的完整人口。短 ROI 只有配套线程计划、边界和实际执行人口重新验证后才能用于 pilot；正式结论保留完整输入。

运行进程必须串行交错，固定 CPU/NUMA 放置和 compiler/runtime 环境。至少记录：

- binary SHA、源码来源及差异、有效配置 include chain 和最终配置 SHA；
- manifest、FST、功能伴随身份及验证策略，完整测量/预热/线程计划人口；
- host CPU 型号、cpuset、NUMA、并发进程、CPU time、RSS、缺页、context-switch 和调度等待；
- 从 trace-open 前到输出结束的 process wall；simulator、warmup、measurement wall；
- 每次运行的输出和原始采样，异常或中止样本独立保存。

本机 `perf` 已安装，`perf_event_paranoid=2`。这不是 PMU 或 off-CPU 采样权限已经通过的证明；首次实际调用失败时，继续用线程 CPU clocks、外层 timer、采样计数和可用的用户态事件推进，不把权限调整作为完成方案的前置要求。不要主动修改全局系统参数。

### 5.2 宿主 timer 设计

timer 采用 epoch/chunk/函数批次级、按线程收集，不在每条普通 hit 上读取高精度时钟。所有内层函数记录 inclusive 和 exclusive/self；并行 worker 记录 CPU 工作量、coordinator 记录暴露墙钟，二者不相加。

| 区域 | 应拆开的工作 |
|---|---|
| 输入准备 | identity hash、open、每类伴随预检、解压、初始化、结果序列化 |
| producer | FST decode、伴随 observe/projection、frontend、IQ/FU/calendar、chunk 物化 |
| schedule/batch | queue wait、resident 数据搬运、k-way merge、batch 元数据 |
| weave | preview、cache/directory service、parent resolve、release/expiry、FR self |
| feedback | entry scan、snapshot/detach、worker self、barrier wait、exit/commit |
| 提交/观察 | counter aggregation、retired-cache observer、thread-state 边界、资源发布 |

旧外层三阶段作为闭合对照继续保留。新增工具不能把兼容计数、必要 PMU 观察或错误检查默默移到计时之外。

### 5.3 操作工作量

每个 checkpoint 除逻辑接受人口，还记录实际尝试工作：

```text
input_records_decoded / companion_rows_validated / decompressed_bytes
scheduled_uops / visited_feedback_uops / accepted_uops
attempted_preview_events / committed_events / retried_uops
logical_epochs / physically_executed_epochs / empty_epoch_fast_skips
active_core_histogram / worker_busy_cpu_ns / barrier_wait_ns
fastpath_attempts / successful_uops / reject_reason_histogram
calendar_queries / probe_steps / max_probe / intervals_shifted
parent_lookups / hash_probes / COW_detached_bytes / index_rebuild_work
snapshot_bytes / journal_bytes / sort_items / allocations
```

实际尝试计数不能随事务 rollback 清零；已接受目标 PMU 计数则仍按原合同回滚。区别这两类计数，才能发现“结果只算一次，但宿主算了多次”。

两个核心归一化指标为：

\[
A_{\mathrm{feedback}}=
\frac{N_{\mathrm{actually\ visited\ feedback\ UOP}}}
{N_{\mathrm{accepted\ UOP}}},
\qquad
D_{\mathrm{epoch}}=
\frac{N_{\mathrm{physically\ executed\ epochs}}}
{N_{\mathrm{accepted\ UOP}}/10^6}.
\]

另报告 ns/user-UOP、ns/mixed-UOP、copied bytes/UOP、cycles probed/UOP。相同总 UOP 的运行中，这些指标比单独观察 guest CPI 更能解释宿主退化。

## 6. 定位长期退化的实验矩阵

### 6.1 版本链

优先恢复三个历史点：已证实高吞吐的旧版、`526d6f5` 两阶段恢复点、冻结 M1；当前 M2 单独列出。每个点绑定 binary/config/input-format 能力。逐区间比较，而不是沿 25 倍总差猜函数。

旧二进制若不能消费现代伴随，不得删除功能成员或伴随后称同输入结果。可选择：

1. 在当前输入读取器上移植旧 host 算法，保持完整功能人口和校验；
2. 用共同可读的真实来源比较引擎内核，再将现代伴随增量单列；
3. 在同一现有二进制中做单因素诊断消融，承认它改变了模型行为。

第 2、3 类只支持层级归因，不能替代完整 M2 修复候选的同输入验收。

### 6.2 单因素诊断

| 控制变量 | 要回答的问题 | 结果的边界 |
|---|---|---|
| 输入处理不变，分别观察 frontend/FU/IQ/feedback 的 CPU 栈 | 新机制的 self 成本在哪里 | 首轮优先采样，无需关闭机制 |
| 新旧 IQ/FU host 实现 | 快路径兼容缺口是否放大逐 UOP 工作 | 等价实现可直接比较；改变模型则只诊断 |
| 消除 ON/OFF | 最新增量来自资格解析还是改变后的 scheduling | 已有两组结果，继续采证而不重复长批次 |
| COW 与 touched-state 实现 | detach/index 是否为热点 | 同有效配置、同目标状态 |
| 原始读取与预验证紧凑输入 | 供数等待能减少多少 | 功能投影、hash/coverage/错误检测仍守恒 |
| worker 数和任务粒度 | 串行比例、屏障、负载不均衡 | 目标 core 数、目标资源参数保持 |
| 原窗口与有界候选窗口 | 固定开销和边界敏感性 | 可能改变预测；精度单独验收 |

FR-FCFS、退休 prepare、分区 IQ、retired frontend 等 ON/OFF 可作为诊断控制，但不能将关闭它们的结果直接作为生产吞吐修复。历史硬件参数不能被扩大来绕过资源门。

### 6.3 负载集合

第一轮完整 Zstd 加 XZ，分别覆盖分支/整数混合与较长等待；接着加入 FLAC、Graph500、Marian，覆盖向量/寄存器消除、同线共享、软件预取与 store 压力。另加入真实的 memory-light、cold-owner、迟到-store、有跨 chunk 依赖的 witness。

先在每个问题的最小真实窗口采证，再在完整人口复核；不重复、填充或伪造记录。目标核数扩展先 C4，再在合法现有来源上验证 C8/C16/C32，避免把多个 C4 case 并发当作单引擎吞吐提高。

## 7. 保持两阶段的修复设计

### 7.1 输入和阶段一：功能校验一次、热数据紧凑

功能输入继续覆盖完整退休流、宏边界、功能 UOP、内存成员和必要伴随。性能候选可将多个伴随对同一 hot record 的投影检查合并成一个共享验证上下文，复用已经算出的 record/code 身份；避免同一事实在各 reader 中重复解压、重复算 hash、重复建临时 vector。

若 runtime 解压/伴随观察占主导，设计**由维护验证器生成的紧凑功能输入包**：

- 包含原 FST/伴随的内容身份、policy/schema/validator 身份、全人口与边界；
- 预先做全部原有完整性、coverage、projection、尾部/gzip 错误检查；
- runtime 按核顺序读已验证的功能事实及原始记录映射，保留逐记录轻量身份和所需生命周期检查；
- 原始文件或验证版本变化必须使包失效，不能仅以文件名、mtime 或“之前通过”跳过验证；
- 包构建和缓存命中分别计时；单次完整进程必须报告真实前处理成本，不能只报告缓存命中收益；
- 验证 corrupted input、truncated footer、错误 macro/ASID 和 changed-source 的拒绝行为保持。

这是一种输入格式/host 实现优化，不将 gem5 的 OoO 时刻、DSB 结果或实际融合装进 timing 输入。来源：[trace-open 重复扫描证据](file:///data00/yinhaolang/FastSim/docs/spr-trace-open-throughput-20261105.md)、[功能伴随 reader](file:///data00/yinhaolang/FastSim/src/fst_core_functional_reader.cpp)。

producer 保留 per-core single writer，使用 chunk slab/ring、只搬运必要字段、复用解压和 operand scratch。首次采样确认旧优化仍在后才修复，避免重复实现已完成的 ASID 缓存、k-way merge、empty-epoch fast path。

### 7.2 阶段二：为 SPR 状态补齐块摘要

不是直接删除 `spr_partitioned_iq` 的拒绝条件。新块转移必须正确消费三组 IQ 容量和 release 摘要、ROB/LSQ、TSO store 释放、消除 UOP 的 allocation/执行差异及跨块依赖。

每个固定小块生成：

- 各 IQ 类别的进入/退出与释放摘要；
- 依赖边跨块入口、completion/retire 的必要边界；
- 真实内存事件及 source/member 索引；
- ROB/SQ/LQ 的容量边、serialize/fence 和父服务活跃关系；
- 必要的 macro/functional-UOP/allocation-slot 计数。

块没有新增响应种子、没有相关活跃服务和容量边时，执行已证明等价的转移。出现不确定关系时，仅相应块走现有 scalar 路径，明确计 reject reason；不能扩成整窗重算。父服务启用本身不能证明该块所有 UOP 都受父服务影响；需要可查询的相关状态/epoch 版本证书，而不是无条件放行。

对于不可等价摘要的块，再决定是否采用固定深度的响应传播近似。近似候选保留两阶段、全部功能记录和精度计分人口，记录近似覆盖及边界误差；它不进入无损优化的结果表。

### 7.3 资源日历：从周期探测转为预约边界查询

为 `SprPortCalendar` 增加最早合法空隙查询，直接跳到冲突预约的结束边，而不是对长等待逐个 `++cycle` 检查。多端口、paired store port、issue/load/store 宽度和同类 initiation interval 必须共同满足；不能取几个独立就绪时刻的 `max` 后就假设整个组合可用。

通过有限目标端口集合和固定 live-reservation 上限约束查询工作。窗口超出日历表达范围时可走明确的有界近似或原有受控路径，但不能分配随累计 guest cycle 增长的巨型数组。日历 watermark、expiry 和整理按接受边界推进，避免丢弃未来合法插空位置。

验证非流水 FU、长延迟、交错乱序插空、paired STA/STD、跨窗口预约、相同周期 tie-break；若改变了原端口选择或轮询游标，即属于模型行为变化而不是无损容器替换。

### 7.4 服务、快照和 DRAM：减少重复状态处理

延续当前分页 COW 和稳定 ID，按热点实测选择优化：

- 普通驻留 hit、没有相关 transient/owner 时快速返回；不创建完整父记录或全局锁；
- record/index/expiry/calendar 的增量修改记录在 touched-slot journal 或复用已有单写所有者中；
- 存在未提交候选时保留不可变入口状态，最终接受一次发布，失败只丢弃相关增量；
- 记录实际 detach、shared_ptr refcount、索引探测、heap stale node 和整理工作，避免“用了 COW 所以成本必然低”；
- DRAM forecast 仅处理实际受影响 channel/bank，保留原有排队、refresh、写队列和资源状态；不能仅按地址不同就断言独立；
- 保留现有 FR 的 defer-initial-feedback 优化，防止候选路径先反馈、后覆盖时又新增一遍全部反馈。

父服务准入、response、controller/callback 和容量释放仍绑定同一最终服务生命周期。吞吐修复不能重新引入 store 排空重复计费或新冷 owner 借用过早服务的精度错误。

### 7.5 并行、窗口和重试预算

保留阶段一按核并行，阶段二按既有确定性共享事件顺序处理，feedback worker 只写本核状态。独立 DRAM channel 等子任务只有在状态隔离得到证明时并行；worker 数根据实际 task/工作量摊薄屏障，不凭 target core 数无条件唤醒。

窗口优化先减少空调用、复用跨窗口状态和批次元数据。扩大窗口或合并时域可能改变预测，应按模型候选验证，而不是作为“只调 host 参数”提交。

每个 checkpoint 的尝试/恢复工作总量设上限，来源于既有固定块和目标资源预算：

\[
W_{\mathrm{checkpoint}}
\le c_{\mathrm{UOP}}N_{\mathrm{accepted}}
+c_{\mathrm{event}}N_{\mathrm{events}}+B_{\mathrm{recovery}},
\]

\[
M\le K_{\mathrm{core}}+K_{\mathrm{live\ services}}
+K_{\mathrm{resident\ chunks}}+K_{\mathrm{journal}}.
\]

约束独立于 ROI 总长和目标等待周期跨度。每个局部候选至多一次 fallback 仍不足够，还需整个 checkpoint 的恢复总量预算；耗尽后提前走稳定路径，不先支付昂贵候选再整窗回退。

## 8. 专题实施顺序和明确停止条件

| 步骤 | 交付 | 完成标准 |
|---|---|---|
| D0 冻结与复现 | M1/M2/历史控制身份、完整输入、host receipt | 确认真正可比的退化区间；不把不兼容输入伪装成同源 |
| D1 timer 与工作计数 | 三阶段闭合、各线程 CPU 栈、exclusive time、attempt counters | 当前低吞吐样本可由成本和工作量解释；残差显式保留 |
| D2 无损 host 修改 | 输入共享校验、scratch/布局、日历/索引等热点切片 | 与冻结 M2 的目标输出逐项相同，并有真实总墙钟收益 |
| D3 SPR 块摘要 | 分区 IQ/TSO/父服务适配、拒绝原因、覆盖分布 | 合法快路径有覆盖，边界/回滚/跨块 witness 通过 |
| D4 必要的有界近似 | 一次反馈、固定容量资源摘要、近似覆盖与版本配置 | 与无损候选独立；同输入 CPI/PMU/吞吐分别验收 |
| D5 完整性能验收 | Zstd/XZ 至少 30 组串行配对，五负载及合法多核扩展 | 逐案测量与端到端速率、离散度、目标人口和状态完整 |

D1 应先解决“慢在哪、为什么执行这么多工作”，不能长期只补计数而不形成可验证热点切片。D2–D4 按实测 self time 和可消除工作量排序，发现不是热点的对象就降级。专题完成以同输入吞吐目标和行为/精度门为准，不以换了一种容器或通过构建为准。

D5 不自动恢复已被用户停止的旧后台任务；新运行属于本专题的新实验，保存新的计划和身份。模型 milestone 只有用户决定推广后才推进为后续 SPR-M3 等；无损实现优化也绑定新 binary/effective config 身份。

## 9. 精度和功能门禁

### 9.1 无损 host 优化

逐核退休宏指令、功能 UOP、内存成员、分支、软件预取、task 集合和窗口完全一致。目标 cycles、cache/PMU、队列及确定性状态相同；只排除 timer、宿主调度等待及预先登记的新增 host 诊断字段。

不能仅证明 aggregate instruction count 相等。错误输入拒绝、边界检查、资源归还、EOF drain、跨 chunk 携带和 retry 状态同样需要验证。有实质并发/日历/快照修改时补定向等价测试，随后执行项目 gate：

```bash
source scripts/project_tmp.sh
cmake --build build -- -j16
./build/fastsim_tests
```

实际候选使用隔离 build 对应命令。`ctest` 当前没有注册用例，不能作为 tests 通过证据。

### 9.2 模型近似候选

冻结 M2 为当前行为比较基线，冻结 M1 同时保留为历史精度参照。每个 workload 报告 gem5 CPI、候选 CPI、带符号相对误差及：

\[
\mathrm{AE}_{\mathrm{CPI}}=
\left|\mathrm{CPI}_{\mathrm{FastSim}}-\mathrm{CPI}_{\mathrm{gem5}}\right|.
\]

聚合同时报告 CPI MAE 和 MAPE，单位为 cycles/retired macro。当前已有五负载 CPI-only 参照如下；它是历史已审计结果，不是本专题重跑：

| 基线 | CPI MAE，cycles/macro | CPI MAPE |
|---|---:|---:|
| 冻结 M1 | 0.191599 | 12.99% |
| 当前 M2 | 0.179390 | 10.89% |

来源：[五负载配对与精度记录](file:///data00/yinhaolang/FastSim/docs/spr-rename-throughput-regression-20261112.md#L15-L33)。

主退休缓存 PMU 仍要求全量分类、native unknown 为零、独立目标身份和合法计分来源。使用维护的 `tools/validate_fs_oracle_identity.py` 及 `tools/run_native_kernel_fastsim_validation.py`，既有输出通过 `--source-root`/`--stats-dir` 复核。缺失 native 证据继续非通过，不用旧 path-class 或重新命名的请求计数代替。

构建、host 等价、同输入吞吐和 native/精度认证是独立状态。无损性能证据可以先形成；native 当前的未知项不能被“优化后结果一致”升级为已认证。来源：[报告合同第 0、6、8 节](file:///data00/yinhaolang/FastSim/docs/accuracy-reporting-contract.md)。

## 10. 吞吐验收表和问题归因方式

每案报告同一实际人口下的 measurement user 宏指令/s、user-UOP/s、mixed processed-UOP/s、engine end-to-end mixed-UOP/s、process wall、峰值 RSS。两种 scope 只有存在合法独立运行时分别报告，不拼接不同运行的分子/分母。

30 组配对保留各次样本，报告均值、P50/P90/P99/min、配对耗时比及离散度；分位数使用 Type 7。多次重复是 host 性能样本，不是新增精度独立样本。主结果运行不用高开销 profiler；profile、诊断计数开关的扰动另行校验。

归因分四项发布：

1. **输入/口径变化**：实际新增工作和 numerator/denominator 改变多少；
2. **无损 host 改进**：相同目标状态下各切片及最终组合的总墙钟收益；
3. **模型复杂度选择**：改变近似后的吞吐与逐案 CPI AE/MAE/PMU 变化；
4. **未闭合残差**：未测 self time、不可恢复旧二进制、原生缺口和 host 噪声。

组件百分比不能相加，stage timer 中位数也不保证相加等于总中位数；最终以组合候选的实际端到端配对为准。不用并发 cases/hour、缩短输入或关闭 prepare 来代替单引擎恢复。

## 11. 本次证据边界与可复查入口

本次已完成：当前安装的 M1/M2 SHA 核验、Release 构建缓存核对、六个已完成 Zstd 输出与配对 receipt 的读取、真实 user/kernel/UOP 人口守恒、速率公式重算、外层阶段闭合以及当前快路径/FR/并行调度源码核对。

本次未完成：新 runtime profile、历史高速二进制同输入配对、各热点对长期退化的定量贡献、修复实现、30 组验收及 fresh native 主 PMU 认证。设计中的 5M 成本预算是目标算术，不是预测收益。

主要来源：

- [本次机器可读证据](computer:///data00/yinhaolang/FastSim/fastsim-throughput-evidence-20261112.json)：六个原始输出和 receipt 的 SHA、计数、速率和阶段归约。
- [证据提取脚本](file:///data00/yinhaolang/FastSim/tmp/throughput-design-20261112/extract_evidence.py)：只读取既有样本，不运行 FastSim。
- [最新两组 M1/OFF/ON 的原始 receipt](file:///data00/yinhaolang/FastSim/tmp/spr-rename-elimination-20261112/throughput/c04-777.zstd_r.samples.json)。
- [历史 CPI/吞吐排查与 5M 快路径记录](file:///data00/yinhaolang/FastSim/docs/fastsim-cpi-throughput-debugging-interview.md)。
- [历史全矩阵工作量及吞吐优化记录](file:///data00/yinhaolang/FastSim/docs/throughput-bottleneck-plan-2026-08-25.md)。
- [历史 prepare 并行退化及已完成修复](file:///data00/yinhaolang/FastSim/docs/buffer-tso-prepare-throughput-fix-options-20260924.md)：历史机制证据，不能直接套成本次根因。
- [trace-open 独立前处理测量](file:///data00/yinhaolang/FastSim/docs/spr-trace-open-throughput-20261105.md)：262.213→77.357 秒的单组 open 对照，仅属于该 FLAC 输入和前处理范围。
- [已有父服务宿主成本分析](file:///data00/yinhaolang/FastSim/fastsim-parent-service-host-cost-20261110.md)：部分整池描述已由当前 COW 实现取代。
- [冻结 v2-wait 的有界成本与恢复预算设计](file:///data00/yinhaolang/FastSim/fastsim-r01-v2-wait-optimization-design-20261111.md#L363-L401)。
- [项目目标及仓库工作流](file:///data00/yinhaolang/FastSim/docs/codebase-workflow.md)。

## 12. 三个方向的具体优化设计

### 12.1 统一的数据流和成本目标

三条主线共同服务于原来的两阶段执行：

```text
退休功能流与功能伴随
    ↓ 并行解压、合并校验、静态分类
功能数据包 + 只读事实字典 + 按核索引
    ↓
阶段一：按核消费紧凑描述符，生成初步时间和必要事件
    ↓
阶段二：沿原顺序处理共享状态，复用服务描述，完成一次反馈
```

预处理是输入准备，不增加新的目标时序求解阶段。功能数据包只保存可由 Atomic/QEMU 获得或由功能事实派生的数据；动态命中、服务时刻、资源准入和实际消除结果仍由模型计算。

| 主线 | 主要消除的工作 | 不能预先确定或省略的工作 |
|---|---|---|
| 输入预处理 | 重复解压、跨伴随重复 hash/projection、重复静态分类 | 动态调度状态、命中、资源压力、真实功能人口 |
| 共享缓存/合并 | 重复字典、重复查询、服务配方和候选状态的重复构建 | 每个请求的成员身份、准入/回调、必要状态转移与计数 |
| 减少逐记录工作 | 每条 UOP 的通用分支、冷字段访问、全量反馈、周期探测 | 数据依赖、排序边、资源释放、必要功能和 PMU 更新 |

目标工作量形态为：

\[
W_{\mathrm{run}}
=aN_{\mathrm{functional}}
+bN_{\mathrm{unique\ service}}
+cN_{\mathrm{changed\ events}}
+dN_{\mathrm{blocks}}+W_{\mathrm{bounded\ fallback}}.
\]

这不是通过合并抹掉功能记录。目标是使不可避免的逐记录项 \(a\) 足够小，把复杂处理绑定到真正需要服务、状态改变或反馈的事件上。各项仍可能重叠，因此该式用于工作设计，不能由各 timer 直接相加求运行时间。

### 12.2 方向一：输入数据预处理及并行边界

当前 `open_trace_manifest()` 已默认采用最多四个 worker 按核打开输入，所以单纯把串行 open 改为并行并不是新修复。下一步应消除每个核内部的重复扫描，并将可复用处理移出热循环。来源：[已有并行打开实现](file:///data00/yinhaolang/FastSim/src/trace.cpp#L3874-L3985)。

建议实现分为四个切片：

1. **统一一次验证上下文**：各 reader 不再各自重读 hot、`.ffe`、`.uops` 后做同一绑定检查；共同消费已解压的记录、宏事实和操作数视图，复用 record/code hash，输出原有 coverage 和失败诊断。
2. **独立工作并行化**：不同核、独立伴随文件的解压、独立文件完整性验证和纯静态分类可以分发；按 core/record 顺序归并验证结果。worker 总数和暂存字节数同时受限，避免为每个核的每个 sidecar 都启动一组线程。
3. **功能数据包**：保存紧凑的原始功能投影、静态字典 ID、动态依赖/内存字段、宏及任务边界和稀疏事件索引；runtime 以批次读取已验证数据，减少反复 gzip 和字段转换。
4. **预取与执行重叠**：沿已有 producer/consumer 管道准备后续 chunk，在有界队列内解码、校验、物化；每个核按原顺序发布完整批次，控制 backpressure 和驻留内存，不引入另一套执行循环。

可并行性不能仅按文件或 record 范围判断：

| 工作 | 并行方式 | 约束 |
|---|---|---|
| 不同核的输入打开和解压 | 各核独立 reader | 保持 manifest 核顺序和确定的错误选择 |
| 不同伴随的物理解压 | 独立 stream worker | 最终按宏/record 对齐，不能分别放行不完整来源 |
| 同一 gzip 文件内任意切片 | 现有文件不能直接任意拆 | 需独立压缩块或经完整验证的索引，不能伪造解压起点 |
| 静态 opcode/角色分类 | 按已解码事实并行 | 保留 code identity、decoder context、policy 和缺失资格 |
| ASID/宏连续性/生命周期验证 | 先按顺序建立边界或携带状态 | 依赖前缀状态的块不能独立假设为空 |
| 全源 rolling hash | 只对合法可组合算法做并行归约 | 现有顺序 hash 不能用“每块 hash 拼接”替代 |

功能数据包中的事实应分成两部分：

- **可重复的静态事实**：代码身份、解码上下文、宏长度、UOP 静态位置、操作类别、候选端口、寄存器角色、barrier/serialize 资格等；由只读字典共享。
- **每次执行不同的事实**：源 record ordinal、producer distance、地址和跨行成员、ASID/task/privilege、宏/UOP 边界、pin/fault/NoAccess 及当前已知性；继续逐条保留。

例如 MOV 形状资格可以缓存，但 producer distance、pin、资源模式及最终能否消除不能整体缓存为静态结论。data cache hit、DSB outcome、target issue/response 都不进入预处理包。

输出包携带源内容身份、转换器/验证器和 schema/policy 身份、完整人口与边界，并保持错误来源可追溯。输入身份或验证规则变化使缓存失效；不能用 mtime 或“过去验证过”代替内容绑定。已验证块必须完整后才能发布；未完成全源验证的来源不能宣称正式输入通过。

一次使用与多次重用分别计分：

\[
T_{\mathrm{prepare}}+R\,T_{\mathrm{packed\ run}}
<R\,T_{\mathrm{raw\ run}}
\]

才说明 \(R\) 次实验的总时间改善。首轮需报告数据包构建、首次运行、重复运行和磁盘容量；不能把一次昂贵处理移出进程后只发布缓存命中的 MIPS。

### 12.3 方向二：共享层数据缓存、查询复用和服务合并

这一方向包含三种不同的复用，实施时分别验证。

**第一种：只读数据共享。** 同一 code identity 下重复出现的静态事实只保存一次；纯地址几何解码可按有效 geometry/config 身份缓存。字典建立后只读，核 worker 使用稳定索引；采用有界核本地查找缓存或可淘汰索引，避免每条读取争用全局锁。字典 miss 回到完整分类路径，不能丢弃记录。批量预处理可暂存字典构建数据，运行时常驻缓存必须有容量预算，不能随动态 ROI 长度无限增长。

**第二种：动态查询结果复用。** 从 cache/directory/parent index 查到的 line/record 描述和请求分类可在同一合法状态视图内复用，但不能仅用 `line` 作键：

```text
查询类别 + 物理 line + 所需 core/context/request 属性
+ cache/set 或 domain 的 mutation version
+ transaction/view identity + ServiceId/generation
+ 所需的时间资格检查
```

不同查询只包含其实际依赖的键，避免为每次 hit 查询一长串证明结构。初版采用保守的域级版本，逐步细化到热点 set/line；这是有界的失效机制，不恢复 cache-residency/owner/sharer 证明链。

必须处理：同 set 的 LRU/victim 变化、写权限/dirty、远端访问、fill 完成、失效/驱逐、资源释放、candidate restore、代次复用和时间跨越。负向查询“没有活跃 parent”也要在 parent 创建后失效。COW 的候选视图变化使裸指针可能过期，因此复用稳定 ID 和视图身份，必要时重新取 record，不能持有跨提交的指针。

缓存命中通常只省去查找/分类，仍按原顺序执行该访问必要的 LRU、目录、credit 和 counter 更新。若打算跳过更新，需要另行证明对应连续区间中的操作可折叠，不能从“查到同一行”推出。

**第三种：一次服务供多个请求使用。** 沿现有 owner/follower 与 fill-generation 接口，复用稳定服务配方和必要完成边，减少重复创建、解析或复制；每个 consumer 仍有自己的成员索引、准入资格、回调、资源以及退休计数。

当前 `ParentServicePool` 是按核/controller 域拥有的；不能为了跨核缓存把私有服务强行放入全局池。共享下层服务可共享其稳定描述，但私有回调、权限及每核资源保持原归属。

| 可复用对象 | 合法复用内容 | 仍逐 consumer 计算的内容 |
|---|---|---|
| 已核验静态字典 | 静态 op/角色/端口类别 | 当前依赖、地址、资格和时间 |
| 同一 cache 状态视图 | 查找结果和分类 | 访问引起的替换/目录/统计变化 |
| 相同 service generation | 服务身份、配方、已明确的完成边 | 请求准入、剩余等待、权限/回调、资源账 |
| 同一候选入口状态 | 不可变 checkpoint 和 touched delta | 各候选修改及接受/丢弃结果 |

**“缓存数据”与“合并请求”不能混用。** 将已支持的 owner/follower 描述改为共享句柄可以是无损优化；扩大同线合并范围、改变 unique fill 数或消费者等待会改变模型预测，必须作为有界模型候选。新冷 owner 不能借用不属于自己的旧服务，store prepare 和 software-prefetch ack 也不能冒充 demand data return。

缓存的是服务配方或合法状态结果，不能直接缓存一次最终延迟：

\[
\Delta_i=
\max\!\left(0,t_{\mathrm{required\ edge}}-t_{\mathrm{admission},i}\right)
\]

只有当服务身份、时钟域和所需边资格都已成立时才可以这样求剩余等待。不同 consumer 使用各自最终准入和本地路径；未知完成边继续未知。

优化 locality 时保留 canonical event 顺序。按 line 分组后重排整个 batch，可能改变同 set 替换、coherence、CHA/NoC 和 DRAM 排队；先只做辅助索引、连续合法 run 的查找复用。跨行重排只有状态独立得到证明时才考虑。

主要接入点：[父服务解析与已有 hit 快返回](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L12893-L12962)、[稳定 ID 查询](file:///data00/yinhaolang/FastSim/include/fastsim/parent_service.hpp#L108-L124)、[服务代次发布](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L2111-L2130)、[共享 checkpoint](file:///data00/yinhaolang/FastSim/src/simulator.cpp#L2133-L2172)。

### 12.4 方向三：减少逐记录工作

方向一、二降低重复解析和访存，方向三进一步改变热循环的工作粒度。

| 当前工作模式 | 候选方式 | 保留的语义 |
|---|---|---|
| 每条 UOP 重做静态角色/指令字节判断 | 字典 ID + 少量动态资格检查 | 当前 pin/fault/依赖/缺失处理 |
| 每条记录读取所有 companion 元数据 | 紧凑 hot 字段 + 冷数据索引 | 真正需要时读取完整功能事实 |
| 后续 pass 扫所有记录找 memory/barrier/task 变化 | producer 建 event/span 索引，后续 pass 沿索引处理 | 所有实际事件和原顺序 |
| 各 pass 重建不同分类集合 | producer 一次生成共享描述及范围索引 | per-UOP、memory-member、macro 身份 |
| 所有接受 UOP 都做完整反馈 | 安全块转移 + 受影响块的现有 scalar closure | 数据依赖及 ROB/IQ/LSQ/retire 边 |
| 长时间空隙逐周期试探 | 最早预约边查询、到期事件推进 | 合法插空、paired port 和 tie-break |
| 每条 hit 建记录/碰共享索引 | 无相关服务时的轻量路径 | 本地响应、必要状态更新和计数 |
| 每条更新大统计对象/诊断输出 | 核/块局部累加，接受后归并 | 原事件范围、宏级归并和 unknown |

宏级缓存减少的是重复静态工作；功能 UOP 不能直接折叠为一个宏执行。微码、多 UOP、共享 allocation、REP、跨行成员继续分别处理。宏指令、功能 UOP 和 allocation slots 保持不同计数单位。

反馈从高频逐条扫描转向块摘要时：

1. producer 按现有固定块建立依赖入口/出口、三组 IQ release、ROB/LSQ、内存/barrier 和退休边界摘要；
2. 阶段二按响应变化标记受影响块；不受影响块只有在所有相关 carry-in 状态均被证书覆盖时才走批量转移；
3. 容量或 store 波可以继续影响后续 UOP，因此不能只更新 memory UOP；不具备块证书的区间沿原路径执行；
4. 每个块的候选探测/失败及整个 checkpoint 的恢复工作有上限，不能递归扩大成整窗重复求解。

当前已接受 UOP 的 97.389% 进入 sparse 物化路径，说明这一方向值得优先采证；但未知实际重复扫描量，不能据此承诺减掉 97% 的宿主成本。目标是把有资格的工作转为块转移，并量化实际扫描、成功覆盖和拒绝原因。

已有 `response_memory_descriptor`、batch timing encode、空步 fast path、scratch 复用和分页 COW 应继续使用。新切片只补齐新 SPR 状态下的适用性，避免再次实施已完成的优化。

### 12.5 三条主线的实施顺序

主线 1 的供数成本和主线 2/3 的引擎成本可分别设计、采证；最终配对测量保持单进程串行。

| 顺序 | 具体切片 | 首次验收指标 |
|---|---|---|
| P0 | 按解压/校验/静态分类、共享查询、反馈访问拆 CPU 与工作量 | self time 和实际操作数可追溯 |
| P1a | 同源伴随统一验证上下文、复用 hash/操作数 scratch | 重读字节、重复验证次数、producer wait 下降；拒绝行为一致 |
| P1b | 热静态字典与紧凑描述，融合 event/span 元数据生成 | 静态分类次数、bytes/UOP、逐记录分支与冷字段读取下降 |
| P2 | 合法状态视图的查询缓存、ServiceId 复用、touched-state 修改 | cache hit/miss/失效、hash probes、detach/index bytes 下降 |
| P3 | 分区 IQ/TSO/父服务适配的块转移、最早预约边查询 | 实际 feedback visits、块覆盖和 probe steps 下降 |
| P4 | 若供数仍占主导，完成可重用功能包/独立压缩块 | 首次和重复使用的 process wall、包构建和驻留预算 |
| P5 | 组合候选的完整同输入吞吐与精度验收 | measurement、engine/process end-to-end 同时报；不叠加切片收益 |

P4 与 P3 可以按热点证据调换顺序。优先统一校验与数据描述，再引入完整新格式，能减少输入迁移风险；若 profile 证实 runtime gzip 占主要 CPU，则直接前移功能包切片。

每条主线都记录“实际少做了什么”：

- 主线 1：重复解压次数、hot-stream 读取次数、跨伴随 projection/hash 次数、pack 命中和构建成本；
- 主线 2：每请求查询次数、generation-qualified 复用次数、失效原因、唯一服务/consumer 人口和复制字节；
- 主线 3：实际访问 UOP、每类特征检查、冷字节读取、scalar fallback、块转移 UOP 和日历探测；
- 组合：同输入功能人口、目标状态、CPI AE/MAE、可用主 PMU、总墙钟及内存。

本节为用户指定方向下的设计细化，未产生新的运行时性能样本。无损切片以冻结 M2 输出等价为门；改变合并或反馈近似的切片另行做模型精度验收。功能输入、退休 prepare、两阶段框架及真实软件预取成员始终保留。
