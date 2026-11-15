# FastSim 吞吐开销实测排查：5M 与当前约 200k

日期：2026-11-12。范围：冻结 SPR-M2、Zstd C4 完整输入的运行时诊断。完成一轮低频 CPU 采样和一轮已有高频 CPU 对照；不涉及模型修改或性能推广验收。

**后续口径修正（用户指示，2026-11-12）**：主吞吐排除输入初始化和校验，
以后固定绑定高频 CPU 52–63。本文原始 measurement 已排除启动预检，
但仍包括在线伴随校验；下表 0.260M/0.999M 是历史 validation-inclusive
诊断值，新口径净吞吐尚不可用。39.9% 是完整进程 CPU 热点，包含 10.6%
启动初始化，不能计入模型净吞吐归因，也不能直接从 measurement 墙钟扣除。
原始记录保留；新规则见 [吞吐合同](docs/accuracy-reporting-contract.md#L505)。

## 1. 首要发现

**当前约 200k 的一个大项是绑到了固定低频 CPU；同一程序和输入换到已有高频 CPU，速度提高 3.84 倍。** CPU 24–35 使用 `intel_cpufreq/userspace`，实测工作频率约 0.795 GHz；CPU 52–63 的本轮工作频率约 3.091 GHz。

第二个大项是软件工作量：生产端反复读取/解压/校验伴随数据，并执行核心调度；第二阶段又物化几乎全部 UOP 来做响应反馈。**端口预约、父服务索引复制和 FR-FCFS 本体没有表现为足以单独解释数量级退化的热点。**

历史 5M 大多为 measurement **user-UOP/s**，当前约 200k 常来自旧 `mips` 宏指令字段。统一口径后的本例是 0.260M user-UOP/s，而不是 0.200M user-UOP/s。换 CPU 后约 0.999M user-UOP/s，距离 5M 的预算仍约 5 倍。历史高速二进制在本轮完整输入上的配对尚缺失，不能把这个剩余差距全部算成新模型回归。

## 2. 同输入对照与有效性

| 项目 | 原 CPU 24–35：完整采样 | CPU 52–63：计数器对照 |
|---|---:|---:|
| 进程墙钟，s | 144.292 | 37.176 |
| simulator 墙钟，s | 126.024 | 32.834 |
| 功能预热，s | 7.132 | 1.857 |
| measurement 墙钟，s | 118.892 | 30.978 |
| 旧 mixed 宏指令 `mips` | 0.191002 | 0.733095 |
| measurement user 宏指令，M/s | 0.184248 | 0.707135 |
| measurement user-UOP，M/s | 0.260219 | 0.998708 |
| 累积 CPU 时间，s（含 profiler 包装） | 484.658 | 128.237 |
| 宿主执行指令，B | 946.790 | 940.857 |
| 工作期间平均频率，GHz | 0.795 | 3.091 |

两轮测量人口完全相同：user 宏指令 21,905,561、user UOP 30,937,873、mixed UOP 35,329,964；完整预热与线程计划相同。递归比较所有输出后，差异仅为时间/吞吐、host frontier 等待和 resident chunk 高水位；目标模型输出一致。

低频采样相对既有未采样 M2 第 0 次 measurement 仅增加 0.38%，与两个已有样本处于相同量级。两轮宿主指令数量近似不变；速度变化与频率变化一致。对照同时改变 socket/NUMA 位置，且不是同 affinity 反转顺序的多轮实验，因此报告为 **绑核/频率/位置诊断**，不声称纯频率因果效应或已认证吞吐收益。

本轮低频运行平均使用约 3.37 个 CPU，高频约 3.46；仍保留 4 个生产线程、4 个 domain 线程和协调者。没有其他 FastSim 回放并行。CPU pressure 中途快照约 0.36%；实际 cgroup-v1 根和 `user.slice` 均无 CPU 配额、`nr_throttled=0`。不是容器 CPU 配额把吞吐压到 200k。

## 3. 不重叠墙钟阶段

| 阶段 | 原 CPU 秒数 | 占 measurement | 高频 CPU 秒数 |
|---|---:|---:|---:|
| schedule/batch（含暴露供数等待） | 42.777 | 35.98% | 9.678 |
| weave（共享处理 + 响应反馈） | 66.765 | 56.16% | 18.587 |
| commit | 8.982 | 7.55% | 2.572 |

原 CPU 外层三阶段闭合约 99.69%。weave 是墙钟最大区域；输入和核心调度的工作在生产线程中并行进行，未全部体现为额外可相加的墙钟。

原 CPU timing-feedback 计时 39.642s、FR-FCFS 计时 33.074s、domain 计时 32.486s **不能相加**。FR-FCFS 内部调用反馈；反馈计时入口又晚于扫描/状态准备。以下调用栈分类按每个叶样本只归入一个拥有者，避免嵌套重复计费。

## 4. CPU 热点与具体开销

47,902 个 `cycles:u` 样本，99 Hz、8 KiB DWARF stack；perf 报告 lost samples 为 0。以下分母是完整进程采样到的 user CPU 周期，包含输入初始化和 warmup，**不是 measurement 墙钟百分比**。约 8.40% 周期的叶符号未解析，但绝大部分可用祖先调用栈归属；拥有者无法归属的约 0.80%。动态库 DWARF 导出有 `.debug_ranges` 警告，因此这里报告函数/调用栈层级，不声称指令行级精确归因。

| 按调用栈互斥归属 | CPU 周期占比 |
|---|---:|
| 输入逐记录读取/解压/校验 | 29.32% |
| 生产端其他核心调度 | 21.16% |
| 逐 UOP 响应反馈 | 17.83% |
| 输入打开/全源预检 | 10.60% |
| 生产端包装/构造记录 | 8.68% |
| 共享/私有缓存服务 | 3.02% |
| 协调者其余批次处理 | 2.81% |
| 生产端端口预约 | 2.52% |
| 退休事件统计 | 1.66% |
| 反馈入口预处理/状态准备 | 0.96% |
| 其余/调用栈未闭合 | 0.80% |
| FR-FCFS（排除响应反馈） | 0.40% |
| 反馈提交 | 0.25% |

生产线程完整调用树约 61.5%：读取输入约 29.3%、`IntervalCoreModel::schedule` 约 23.6%、其余约 8.6%。这里的三个数字是嵌套调用树的拆分，不与上表再相加。

### 输入数据与逐记录校验

实际输入含 `.ffe.gz`、`.uops.gz`、`.xlat.gz`；四核压缩伴随文件约 710 MB。**没有 `.core.gz`**，因此之前代码排查发现的 `CoreFunctionalReader` 全源预检/重读风险不适用于本次测量，不能归因给它。

当前 `BinaryTraceSource::next` 自身约 10.86% CPU；`crc32_z` 自身约 5.51%；静态 UOP reader 的逐行解码、hot-record hash、stream hash、依赖投影校验在生产路径上反复执行。`validate_static_uop_projection` 每条记录创建 `expected/actual` 两个 vector，再排序/去重；调用树约 3.20%，其中 vector 扩容约 1.76%。输入构造器还执行 frontend/translation 的全源绑定检查。

第一条优化线应是**带内容身份的并行预处理和紧凑数据包**，并将已验证的静态投影结果缓存；保留完整人口、损坏拒绝和源绑定。当前 trace-open 已默认最多 4 worker，不能只增加打开线程数就宣称解决问题。应分别计入首次预处理、首次运行、重复运行成本。

### 翻译元数据和核心调度

`reclaim_translation_metadata` 自身约 **6.26%**；其实现遍历 `dynamic_dtlb_versions_`，对每个键查询三个 map。`translate_key` 在检查 TLB timing hit 前就调用 `retire_page_walks_through`，后者最后执行整表回收，即使本次没有完成的 walk。应调查“实际失效/释放键驱动的回收”，保持版本和迟到完成保护。

生产端端口预约调用树约 **2.51%**、自身约 1.99%；存在逐 cycle 探测和 vector 插入，但实测优先级明显低于输入处理和反馈。SPR frontend `begin_macro/source_for/pump` 也有工作量，适合合并同宏/同代码身份查询。

### 响应反馈与逐 UOP 工作

`compute_core_timing_feedback` 调用树约 **17.79%**，主入口及各 lambda 为实际热点。反馈 sparse 物化 34,407,630/35,329,964，约 **97.389%**；旧 activity/causal-block 快路径在分区 IQ、父服务、TSO 等新条件下退出。两阶段仍然并行，但第二阶段几乎再次处理了全部 UOP。

`getenv` 自身约 **2.34%**。即使 `FASTSIM_TSO_LOAD_HIST/FASTSIM_TSO_RETIRE_HIST` 等诊断未启用，热循环仍逐条查询环境，退休检查尤其在逐 UOP 路径上。当前进程有 151 个环境变量；可将诊断开关在初始化时绑定，避免每条记录查找。这个局部项不能被宣传为数倍收益。

对应第三条优化线：减少静态事实重算、合并同宏处理、缓存诊断开关，并为分区 IQ/父服务扩展可证明等价的块摘要；若需要新的 bounded 近似，精度和吞吐分别验收。

### 共享层与状态复制

父服务已接受分支索引累计复制约 1.207 GiB；这不是全进程内存流量。整个进程 `memmove` 自身约 0.84%，尚未证明父服务复制是主因，不能把一个 GiB 计数直接换算成百分比。共享层应优先缓存同批次可复用描述符/来源关系，继续用函数拥有者计时区分功能 replay、反馈准备和 COW。

FR-FCFS 候选/预测为 5,781/5,781、DRAM 请求 108,333，没有 fallback；整窗 reweave/causal/retime/buffer-service pass 均为零。它的 33 秒大计时包含完整响应反馈，不能认定 DRAM scheduler 自身花掉 33 秒。

## 5. 假设结论与修复优先级

| 假设 | 结论 | 实测约束 |
|---|---|---|
| A：输入解压/校验是最大 CPU 开销群 | CONFIRMED | 输入打开 + 逐记录路径合计约 39.92%；生产端输入子树约 29.3% |
| B：共享扫描/复制/反馈可单独解释主体退化 | INCONCLUSIVE | weave 墙钟最大，但复制未证实主导；嵌套 FR/feedback 不能相加 |
| C：SPR 端口/核心逐记录调度是最大 CPU 群 | REJECTED（“最大”部分） | 核心 schedule 约 23.6%，端口约 2.5%；其工作显著，但输入群更大 |
| D：主机竞争/CPU 配额导致当前 200k | REJECTED（本次主体解释） | 无 CPU 配额/节流；竞争快照很低，模型确实在使用多个线程 |
| E：低频绑核显著放大当前耗时 | CONFIRMED | 0.795→3.091 GHz；同输入 measurement 实测 3.84 倍改善 |

建议执行顺序：

1. **先固定实际 CPU 频率、socket/NUMA 和 affinity 再做专题基线**；已有高频 CPU 上本例已接近 1M user-UOP/s。此次仅更换运行 affinity，没有修改系统频率策略。
2. **输入预处理与逐记录校验**：按 core 并行、内容身份绑定、紧凑记录、投影缓存；同时解决反复小分配。
3. **翻译元数据回收与诊断开关**：针对已测整表扫描和逐记录 `getenv`，优先实现行为不变的优化。
4. **反馈工作量**：扩展新配置下的块处理，降低 97.4% 全 UOP 物化；保持退休输入和现有两阶段框架。
5. **共享缓存/合并**：在上述证据中选择能减少真实重复 descriptor/索引工作量的切片，继续细分 COW/FR-FCFS exclusive 成本。

当前 M2/M1 的既有两组同输入 simulator 吞吐增量约 -7.01%；冻结 M1 在原低频绑核上也只有约 0.283M user-UOP/s。因此最新 rename 消除不是历史数量级损失的全部来源。历史高速样本具有不同工作负载、输入格式和模型配置；本轮没有历史高速二进制同输入回放，不能给出“每个版本退化了多少倍”的闭合账单。

progress_status: **analyzed**。已完成当前开销实测诊断；未实施软件修复、未切换默认模型、未提交。保留采样与运行 receipts 供专题继续使用；本轮不是 30-pair 性能门禁，不发布新的 CPI/PMU 精度结论。

## 6. 可追溯证据

- [结构化归约与文件哈希](fastsim-throughput-cost-diagnosis-20261112.json)。
- [低频完整运行](tmp/throughput-cost-regression-20261112/m2-profile.run.json)、[低频 stats](tmp/throughput-cost-regression-20261112/m2-profile.json)。
- [高频 CPU 对照](tmp/throughput-cost-regression-20261112/m2-fast-cpus.run.json)、[高频 stats](tmp/throughput-cost-regression-20261112/m2-fast-cpus.json)。
- [CPU 互斥归属](tmp/throughput-cost-regression-20261112/m2-profile.cpu.json)、[self 热点](tmp/throughput-cost-regression-20261112/m2-profile.self.txt)、[调用树](tmp/throughput-cost-regression-20261112/m2-profile.callers.txt)。
- [主机频率策略](tmp/throughput-cost-regression-20261112/host-frequency.json)、[cgroup](tmp/throughput-cost-regression-20261112/host-cgroup.json)、[目标输出一致性](tmp/throughput-cost-regression-20261112/affinity-output-diff.json)。
- [输入伴随存在性](tmp/throughput-cost-regression-20261112/input-companions.json)。
- [输入逐记录观察](src/trace.cpp#L2390-L2409)、[静态依赖投影校验](src/fst_static_uop_reader.cpp#L5-L15)。
- [翻译整表回收](src/interval_core.cpp#L1352-L1364)、[TLB hit 前回收入口](src/interval_core.cpp#L1508-L1520)。
- [反馈快路径拒绝](src/simulator.cpp#L12440-L12452)、[块处理拒绝](src/simulator.cpp#L13971-L13991)。
- [逐记录诊断 getenv](src/simulator.cpp#L16836-L16852)、[逐 UOP 退休 getenv](src/simulator.cpp#L19105-L19118)。
- [嵌套 FR/feedback 计时](src/simulator.cpp#L22887-L22902)、[FR 内部反馈](src/simulator.cpp#L23127-L23130)。
- [历史高吞吐记录](docs/throughput-bottleneck-plan-2026-08-25.md#L405-L449)、[原专题设计](fastsim-throughput-investigation-and-repair-design-20261112.md)。
