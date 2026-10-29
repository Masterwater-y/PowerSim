# FastSim 第二阶段工作汇报：技术框架、组件设计与实验结果

**汇报人：尹浩朗｜资料截止：2026-10-26｜面向专家评审，建议正文35–45分钟，另留问答。**

阅读路径：第1–4章介绍目标、框架与组件；第5章用完整双核例子讲解执行流程与scheduler；第6–8章报告实验、消融与竞品对照；第9–12章补充配置迁移、后续工作和专家问答。

口头汇报建议：项目需求4分钟；框架与组件14分钟；完整例子及scheduler协同10分钟；实验与竞品8分钟；配置迁移和后续工作4分钟。

本报告基于当前工作区代码、维护文档及现存实验输出整理。没有重新采集负载或测量吞吐。9月部分原始实验目录目前不在工作区，相关数值明确作为历史文档记录；10月关键结果已对照现存维护验证器输出复核。**各版本“最好结果”指有明确配置与数据范围的代表结果；实验候选、维护配置、精度最好和速度最好分别标注。**

## 1. 项目要解决什么问题

### 1.1 为什么需要 FastSim

研究一个处理器设计，不能只问“程序算出了什么”，还要问“需要多少时间、瓶颈在哪里、换一套硬件资源会怎样”。同一个程序的执行时间，受到指令依赖、乱序执行能力、分支预测、缓存、一致性、内存排队，以及线程同步和操作系统调度共同影响。

gem5 能详细描述这些机制，是本项目的微架构精度基线。但对多个工作负载、核数和硬件配置反复做详细模拟，成本很高。FastSim 的目标是：**保留影响性能的关键因果关系，用更紧凑的状态和事件计算替代逐周期推进，快速预测 CPI 与性能事件，为批量分析和配置探索提供工具。**

项目的精度关系分为三层：真实机器 perf 规定目标事件语义；gem5 提供指定配置下的独立参考；FastSim 在相同功能指令流、参数和统计范围下近似 gem5。与 gem5 接近，并不自动等于真实机器上同样准确。[S1][S2]

### 1.2 需要交付什么能力

| 使用者的问题 | FastSim需要具备的能力 | 应如何证明 |
|---|---|---|
| 某个负载为什么慢？ | 可解释的核内依赖、分支、缓存与DRAM模型 | 时序见证、逐组件事件与同源消融 |
| 核数、缓存或执行窗口变化后会怎样？ | 参数化资源及共享状态 | 绝对误差、变化方向/幅度和配置排序 |
| 同步和阻塞是否改变完成时间？ | 功能先后关系、任务owner、active/elapsed分离 | 同一功能窗口、完整的任务指令记录与边界守恒 |
| 能否比详细模拟更快地比较很多配置？ | 高吞吐回放与可复用功能输入 | 相同宿主预算下的回放速度和完整流程成本 |
| 结果可信到什么程度？ | 独立参考、来源身份、完整覆盖检查 | 不能通过时明确拒绝，不把未知当零 |

本次汇报围绕FastSim如何建模、如何组织多核计算，以及如何控制回放开销展开。实验用于检验这套技术框架的精度与吞吐表现。

### 1.3 技术框架的三个核心设计

1. **功能输入与目标硬件模型解耦。** 将指令类别、依赖、地址和分支等表示为统一FST功能流；核内执行、cache、一致性与DRAM由目标配置决定。更换硬件配置时，修改对应资源与时序模型，不按负载拟合CPI常数。
2. **核内并行计算，共享状态统一协调。** 各线程的常驻worker计算核内执行下界；coordinator按模型时间处理跨核访存与共享资源竞争。这样能并行处理大量指令，同时保留多核之间的相互影响。
3. **响应反馈与紧凑状态结合。** 访存服务时间沿依赖、队列释放和退休顺序反馈；稀疏scoreboard、资源日历、块级summary和专用热路径减少宿主开销。框架的关键取舍，是在保留主要性能约束的同时减少模拟工作量。[S6][S7]

## 2. 先把三个指标讲清楚

**CPI** 是目标处理器平均每条宏指令耗费的周期，越低通常表示程序在该机器上执行得越快。x86一条宏指令可能拆成多个微操作（UOP），因此cycles/UOP不能直接叫宏指令CPI。

**PMU事件**描述“发生了多少次某类行为”。例如退休load的L2 miss，和私有L2收到的tag-miss请求，统计对象不同：一条load可能拆成多个访存片段，多条load也可能合并共享一次填充。

**模拟器吞吐** 是宿主机每秒处理多少测量窗口内的用户UOP，衡量FastSim本身的速度；它不是目标机器的IPC，也不是目标程序的elapsed时间。

本报告统一采用以下口径，历史例外单独标注：

| 指标 | 定义 | 需要同时展示的量 |
|---|---|---|
| workload-task active CPI | 所选任务拥有CPU期间的active周期总和 / 对应退休宏指令总数 | 两侧CPI、带符号相对误差、绝对误差cycles/宏指令 |
| CPI MAPE | 各case相对误差绝对值的等权平均 | CPI MAE，即各case绝对CPI误差的等权平均 |
| CPI WAPE / signed bias | 按经验证宏指令数加权的绝对/带符号周期差，再除参考周期总量 | 与MAPE分别命名，不能互换 |
| PMU MAPE / WAPE | case等权相对误差 / 绝对计数误差总和除参考计数总和 | 原始计数、有效case数、分类覆盖情况和事件定义 |
| elapsed | 目标系统共同时间域内整段经过时间 | 不跨核求和，与active CPI独立比较 |
| 主吞吐 | 测量用户UOP / measurement宿主墙钟 / 10⁶ | 功能预热、输入哈希、端到端wall另外列出 |

“来源验证通过”“精度阈值达标”“吞吐可接受”和“硬件PMU等价”是四项独立结论。后文不会用一个passed代替全部结论。[S2]

## 3. FastSim的整体框架与技术流程

**图1｜FastSim整体框架：同步与任务控制、核内并行计算、共享访存及响应反馈。**

![FastSim整体架构](../tmp/stage2-report/assets/architecture.png)

**读图先抓住一个问题：某个核心的下一段指令，现在能不能继续模拟？** 功能计划给出需要等待的前驱，时间窗口协调器负责暂缓或放行后续指令；`RecordedScheduler`维护任务状态、CPU owner和运行时间。Bound–Weave–Feedback计算已经允许执行的指令需要多少时间，校正后的事件时刻再影响下一段指令何时可以继续。

蓝色实线表示数据与事件流，紫色虚线表示前驱约束和状态检查，绿色表示校正后的响应、事件时刻与统计结果。图中“调度控制”是几个模块共同完成的职责，不能把它全部理解成`RecordedScheduler`这个类在选择下一个运行任务。

### 3.1 输入只告诉模型“做了什么”

当前主要输入来自gem5 full-system采集，包含用户态与内核态已提交功能流，转换为FST。FST保留操作类别、PC、依赖、访存地址、分支实际方向、宏指令边界等；必要的地址映射、功能请求角色、同步和任务事实通过伴随文件提供。

gem5测得的周期、cache hit/miss、原生PMU答案不作为FastSim在线推理输入。它们由独立observer保存，只供后续评分。这种分离使“功能流可以换一个目标配置重新估时”有实现基础，也避免把参考答案直接带入预测。

FST是已经完成解码和UOP降级的中间表示，不能把原始drmemtrace直接改扩展名使用。当前有drmemtrace/FST接口规范与元数据审计，完整DR/QEMU生产适配器尚未完成。[S5]

### 3.2 Bound：先算核内时间下界

每个绑定线程的常驻宿主worker解码一批指令，用IntervalCore计算依赖和有限资源允许的最早执行时间。不同核心的核内计算可以并行；软件线程的trace状态与硬件核的时序状态分开保存。

直观上，这像一张工位预约表：操作要等输入数据就绪、执行单元有空、每周期带宽未用尽、队列仍有名额，才能启动。独立操作可以越过长延迟操作执行，最后仍按程序顺序退休。

### 3.3 Weave与Feedback：统一共享状态，再传播响应

coordinator按FastSim模型时间的确定性前沿选择各核事件，维护私有cache、共享目录/LLC、内存资源和请求顺序。访存实际服务时间返回后，再沿依赖、队列释放、写回和退休链传播延期。

关键时序关系可以写成示意形式：

```text
issue_i  >= max(前驱完成，dispatch约束，FU/端口可用时间)
done_i   >= issue_i + 本次操作/访存服务时间
retire_i >= max(done_i，前序退休，commit带宽约束)
```

这里的max表示多个约束共同决定时间，不能把每次miss延迟、分支penalty和等待周期简单相加。实现还包含不同流水级、宽度与资源生命周期；上述方程用于解释思路，不是完整算法定义。

### 3.4 为什么会快，误差又来自哪里

速度来自四点：按指令/事件跳跃求时间；各核并行计算下界；共享状态集中而紧凑；只保存影响后续工作的必要窗口状态。相较逐周期遍历流水级，工作量更多跟“真实指令与事件数”关联。

主要近似在于：规范cache/共享服务分类可能先于最终响应校正完成。最终请求到达改变后，本来可能看到不同cache驻留、合并或DRAM排队；一次反馈不保证重新求得完整共享状态固定点。完整重放可能改善闭合，却增加宿主成本甚至出现不收敛。

因此，**bound/weave是性能取舍的框架，不是天然的精确性证明**。该思路已有zsim等先例，FastSim的具体设计需要结合核内时间计算、跨核事件顺序、共享服务和反馈传播一起分析。[S6][P3]

### 3.5 一次回放如何在框架中执行

下面是执行过程示意；具体分块、同步边界与预热处理由当前配置决定：

```text
读取目标配置与FST，建立每线程的功能状态和每核硬件状态
  ↓
各worker解码下一批指令，计算依赖、执行单元和队列约束下的时间下界
  ↓
coordinator选择当前时间窗口内可处理的事件前缀
  ↓
按确定顺序访问私有cache、目录、LLC与DRAM，计算服务结果
  ↓
将响应时间反馈到指令依赖、写回、队列释放与顺序退休
  ↓
遇到同步、唤醒或切换边界：检查前驱与校正后的事件时间
  ↓
前驱未满足：暂缓该边界之后的指令，其余可执行核心继续
前驱已满足：释放边界，更新任务状态/owner，设置后续执行时间下界
  ↓
继续下一段 Bound–Weave–Feedback，保留跨窗口状态
  ↓
输出目标CPI/PMU、宿主回放耗时，再与独立gem5参考比较
```

这套组织把线程内计算与跨核协调分开：独立计算尽量交给worker；会改变共享状态的操作由coordinator排序。宿主线程的运行先后不能直接当作目标处理器的执行先后。

### 3.6 模型精度与实现开销如何取舍

| 设计选择 | 要保留的性能影响 | 减少的宿主工作 |
|---|---|---|
| 依赖与资源日历 | 数据依赖、FU占用、带宽和队列反压 | 不逐周期遍历每个流水级 |
| 常驻worker与块级交换 | 每线程顺序与多核并行 | 减少反复创建线程和细粒度交接 |
| 紧凑cache/目录状态 | 驻留、替换、权限和远端供给 | 不复制完整Ruby消息与控制器对象 |
| 稀疏scoreboard与块末summary | 对后续指令仍有影响的完成/释放状态 | 减少窗口扫描和重复状态维护 |
| 响应反馈 | 慢访存对后继执行和退休的影响 | 在既有执行链上传播延期，避免默认全窗口反复重放 |

其中有些是实现优化，有些会改变时序近似。前者应检查目标输出保持一致，后者必须同时评估精度和吞吐；不能把两类收益混为一谈。

## 4. 组件设计：动机、技术点、优势与边界

### 4.1 FST与TraceSource：让热路径只读必要功能事实

**动机。** 大量JSON对象和重复解码会消耗回放时间；但把所有metadata塞进热记录，也会放大存储与带宽。

**机制。** FST热记录固定64字节，保存常用功能字段；较大或稀疏事实留在冷sidecar。TraceSource统一二进制、JSONL、切片、功能预热等输入接口。完整RAW依赖可由扩展保留，不因inline字段数量有限而截断长依赖。

地址按用途分开：物理地址用于cache/CHA/DRAM；虚拟页token与ASID用于DTLB与地址空间；`.imap`提供静态指令信息；`.mreq`提供NO_ACCESS等功能角色；`.fsem`绑定退休来源和精确fault前缀事实。后两者不携带参考cache答案。

**收益与边界。** 顺序解码、输入复用和backend解耦具有工程价值；但跨ISA或跨微架构时，必须重审UOP降级、依赖、物理映射和退休资格，不能假设同一中间表示天然等于任意真实CPU的微操作。源码：`types.hpp`、`trace.hpp`、`trace.cpp`。[S5][S7]

### 4.2 IntervalCore：用资源日历重建乱序执行

**动机。** 固定IPC或简单累加miss penalty，无法表达依赖链、内存并行度和窗口反压；完整复制gem5 O3又会损失吞吐。

**机制。** 显式建模流水宽度、ROB/IQ/LQ/SQ、FU数量/延迟/流水属性、load/store端口、写回带宽和顺序退休。先建立下界，再把访存实际响应传播到年轻操作及资源释放。

**技术点。** sequence-tagged稀疏scoreboard、块末状态summary、monotone IQ日历和materialized热内核减少宿主查找、分支和重复状态维护。Q是模型时间窗口，chunk是解码批大小；两者都不是目标硬件ROB容量。

**收益与边界。** 能解释“是依赖链慢还是独立操作被资源卡住”，并让参数变化作用于状态。仍缺完整wrong-path动态指令、rename/free-list及精确StoreSet回滚；有配置字段或实验代码不等于已验证能力。源码：`interval_core.cpp/.hpp`、`op_traits.hpp`。[S7]

### 4.3 预测器与前端：预测错误必须影响后续取指

**动机。** 分支数接近，不代表错误恢复时间正确；恢复尾巴在跨窗口反馈中丢失，也会系统性低估CPI。

**机制。** FastSim用自身Tournament/gshare、BTB、RAS和indirect状态预测，与功能流的实际结果比较；可选择speculative history、checkpoint及退休训练。显式启用响应侧恢复时，恢复前沿把模型分支完成及恢复约束传到后继fetch，再传播到dispatch、issue和retire，避免重复收费。

当前维护配置链已有fetch-supply、L1I及modeled lower hierarchy路径，取指流量可与data共享下层资源，但统计域分开。不能照搬8月“L1I默认关闭”的旧描述。

**收益与边界。** 给前端停顿以明确因果边，而不是按负载补CPI。功能trace仍缺真实wrong-path地址/操作，默认指令物理放置存在近似；ITLB、现代TAGE类状态机等没有普遍支持。Decode可提前发现的目标错误与后端执行解析错误也不能统一套同一恢复下界。源码：`predictor.cpp`、`interval_core.cpp`、`simulator.cpp`。[S8]

### 4.4 DTLB与PTE：地址翻译也是访存路径的一部分

**动机。** cache使用物理地址，不意味着翻译免费；page walk会占用下层cache和内存资源，跨进程状态也不能混用。

**机制。** DTLB按ASID与虚拟页token维护LRU状态，区分architectural/timing视图；固定walk模型与层级PTE请求实验路径分开。`.vmap`提供有来源的页路径，不能从未来状态补证。

**收益与边界。** 可把翻译停顿与data miss分开分析。常用兼容路径仍是固定walk近似；层级walker、same-page follower以及和其他solver的组合未全面闭合，bounded analytic当前拒绝部分组合。不能仅凭TLB项数相同宣称walk趋势已对齐。[S9]

### 4.5 私有cache、目录与共享LLC：保持可解释的共享状态

**动机。** 多核下，一个核的写操作会改变其他核的数据来源、权限和等待；每核独立cache miss模型无法表达这些关系。

**机制。** set/tag、LRU/TreePLRU、dirty victim、私有层级与共享LLC提供容量/冲突/替换状态；紧凑owner/sharer/modified目录表示共享和独占。shared path区分permission upgrade、remote supply、LLC hit、merge及unique memory service。

**收益与边界。** 比给每种负载一个miss倍率更可解释，可直接干预容量、相联度和home mapping。紧凑目录没有复制完整Ruby transient、retry、ack及消息队列；相同数值参数不等于相同协议时序。源码：`cache.cpp/.hpp`、`simulator.cpp`中的directory/shared-memory模型。[S7][S9]

### 4.6 Fill、MSHR/TBE与合并：tag存在不等于数据已经到达

**动机。** 第一个load发起miss后，同线第二个load既不能无条件当立即hit，也不应重复产生一笔独立DRAM服务。

**机制。** shared fill、pending-fill、line generation和private read-service组件分别跟踪leader/follower、请求准入、响应可用与可见性。MSHR/TBE、CPU Sequencer outstanding、store队列是不同资源，不能合成一个容量。

**技术点。** 用generation区分同一地址不同时期的服务，使用有界ledger保留跨窗口状态；preview/commit和snapshot支撑安全回滚。请求、cache-line、合并等待者和unique fill分别计数。

**收益与边界。** 为修复“过早hit”和重复服务提供直接状态，也支撑退休事件分类。但维护alias的部分pending-fill/admission修复仍默认关闭；时序中性的FB_HIT影子表不会自动修复canonical cache交错。组件存在和通过定向测试不能写成全生产路径已完成。[S9]

### 4.7 DRAM与网络：用日历近似排队和竞争

**动机。** 一个LLC miss不是固定延迟：它受channel/rank/bank/row、总线和读写切换影响。

**机制。** 地址映射与open-row状态配合bank/channel日历计算PRE/ACT/read/write服务；混合模型区分需求读与dirty writeback，提供队列、水位、turnaround和有界FR-FCFS选择。CHA/home服务和固定网络边传播共享访问。

**收益与边界。** 以紧凑状态保留访存并行、row局部性和排队解释。仍缺完整DDR refresh和完整Ruby/SimpleNetwork逐link/vnet模型；FR-FCFS配置字符串也不证明调度修复实际触发，某些C4/C8配置有效选择窗口会退为1。具体实验须读最终effective配置，不能把通用默认和runner覆盖混为一谈。源码：`mixed_dram.cpp/.hpp`、`simulator.cpp`。[S9]

### 4.8 Store队列与写入时序

**动机。** TSO要求store按程序顺序对外发布，但不要求所有准备动作完全串行。把“退休”“拿到写权限”“发布”“释放SQ”当同一时刻，会引入明显偏差。

**机制。** serial配置维持较保守的store in-flight行为；buffer TSO配置允许多个store准备权限，真正发布仍遵循FIFO。store退休后仍可能占SQ到响应结束；完整覆盖的老store可转发load，避免错误地产生cache请求。

prepare lifecycle进一步区分canWB、准备、FIFO head接管、本地写/重取权和SQ释放。驻留证书提供“不被容量淘汰”的充分证明，但其保守性和跨核失效边界不能忽略。事件参考引擎用于描述生命周期；bounded analytic在明确非绑定资源子域中，将其压成一次规范分类和解析时间递推。

**收益与边界。** 显式区分store占用资源、取得权限和对外发布的时刻，能描述写缓冲的吞吐与反压。bounded版本避免全窗口fixed-point，却冻结canonical uncore排队和驻留预测，尚非精确协议模拟，仍为opt-in。源码：`store_request_engine.cpp`、`bounded_store_timing.cpp`及`simulator.cpp`。[S3]

### 4.9 调度控制：谁负责等待、放行与状态维护

**动机。** 独立回放每核trace，可能让线程越过尚未完成的barrier；futex阻塞也不等于整个物理核停止工作。

**机制。** 输入已经记录了物理核上的功能顺序，例如“T0计算→阻塞切出→idle/内核→切回T0→继续计算”。FastSim重新计算这些边界何时发生；计划不包含gem5 tick或等待时长答案。

| 组件 | 回答的问题 | 具体工作 |
|---|---|---|
| `FunctionalSyncPlan` | 这个事件需要等谁？前驱是否已经完成？ | 保存事件图，检查前驱，计算事件释放时刻 |
| 时间窗口协调器 | 当前哪些指令可以继续？最早从什么时间继续？ | 在执行边界暂停后续前缀；满足条件后解除等待并设置执行时间下界 |
| `RecordedScheduler` | T0处于什么状态？CPU现在归谁？周期归哪个任务？ | 维护Blocked/Ready/Running与owner，检查切换合法性，累计任务运行时间 |

wakeup只把任务置为Ready；到已记录的switch-in边界被释放时，才更新owner并继续该任务之后的指令。`RecordedScheduler`本身不选择OS调度策略，也不独立完成全部暂停/放行逻辑。任务阻塞后，物理核上的idle、内核及其他任务指令仍继续回放并影响硬件状态。

**收益与边界。** 可分别核算任务active周期与共同elapsed，保留非目标任务对cache的影响；不会重复收取一笔虚构的固定调度延迟。当前是记录顺序与owner的重放，不重新选择OS调度、锁赢家或竞争结果；任意迁移、超额订阅、nested task和完整TID生命周期仍不普遍支持。源码：`functional_sync.cpp`、`recorded_scheduler.cpp`、同步compiler与auditor。[S10][S11]

### 4.10 Warmup、测量与原生PMU：保证比较的是同一批工作

**动机。** 冷cache、不同窗口、漏掉内核前缀或不一致的宏指令计数，都能制造看似很好的误差。

**机制。** record-bounded功能预热建立可观察状态；共同barrier后只重置测量计数/时间，保留cache、目录、预测器、TLB、DRAM和依赖/响应状态。采集端在最快核首次达到声明用户UOP目标且当前宏边界完整时关闭所有参与核；FastSim回放并计分该共同窗口的全部实际记录，慢核不补齐。

目标身份通过独立工具核对实际`config.ini`、effective target及profile。native validator核对逐核指令数、任务归属、独立Ruby/SLICC来源及守恒；branch来自原始退休BPred。退休load还需处理forwarding、follower、跨行、多UOP、软件预取、硬件入口和精确fault前缀。

**收益与边界。** 能拒绝错源、错事件、指令统计范围不一致；这本身是研究基础设施价值。完整分类只是事件比较的必要条件，仍不是SPR硬件等价认证；宏分母与真机INST_RETIRED的完整映射也需独立证明。[S2][S4]

## 5. 一个完整例子：双核访存、阻塞唤醒与scheduler协同

本章用同一个例子走完输入、核内计算、共享访存、响应反馈、调度控制和统计输出。重点观察：**核1的一次慢访存，怎样通过唤醒事件影响核0后续指令的执行时间。**

### 5.1 程序、初始状态与教学参数

考虑任务T0、T1分别固定在核0、核1上运行。两者先读内存并计算；随后T0等待通知，T1完成工作后唤醒T0，T0恢复执行：

```text
核0                         核1
A1: r1 = load(X)            B1: s1 = load(Z)
A2: r2 = r1 + 1             B2: s2 = s1 + 1
A3: r3 = r4 + r5            B3: s3 = s4 + s5
wait(event)                 notify(event)
A4: 恢复后继续计算          wait(next_event)
```

A2依赖A1，B2依赖B1；A3、B3的输入已经就绪，可以提前执行。X、Z是不同cache line，但映射到同一个内存通道。先计算前6条操作，再把wait、wakeup和switch接到同一时间线上。

**以下周期数是便于手算的教学设定，不是实际负载实验或生产配置。** 前6条操作分别简化为1条宏指令、1个UOP；ALU延迟1周期，load下界4周期，ROB/LQ等容量充足，每核最多退休2条/周期，省略额外退休级延迟。TLB命中，取指不产生额外请求；预热后的cache中没有X、Z。为突出跨核竞争，将下层存储简化为一个共享服务槽，每笔读占用20周期；真实DRAM模型还会考虑bank、row、总线与读写队列。wait/notify及调度路径实际还包含用户态runtime、系统调用、内核和idle指令，下面只展开关键事件，不把它们当作零成本。

本例从两任务已经建立并绑定开始，测量起点为周期0，随后使用共同终点关闭两核统计。T0等待的通知尚未产生，因此会进入真实阻塞；不是每次调用wait都被假定为阻塞。

### 5.2 输入准备：FST与调度计划分别提供什么

A1保存load类别和X的地址；A2保存ALU类别及“A1是生产者”的依赖；A3的输入来自已就绪寄存器。核1同理。物理核FST还保留wait/notify期间的内核与idle指令；`thread_state.plan`等功能计划记录初始owner、已证明的阻塞、唤醒和switch事实，并锚定到源记录。FST不写入“这次load需要20周期”或“LLC miss”标签，计划也不写入gem5的事件发生周期。

FST中的关键字段可以简化为下表。这里只展示前6条计算；wait/notify对应的其余指令仍保留在完整物理流中。

| 记录 | 操作类别 | 数据地址 | 输入依赖 | 在模型中的用途 |
|---|---|---|---|---|
| A1 | load | X | 地址输入已就绪 | 产生数据访问，等待模型服务 |
| A2 | ALU | — | A1的结果 | 等待A1完成后才能计算 |
| A3 | ALU | — | 其他已就绪寄存器 | 可与A1并行 |
| B1 | load | Z | 地址输入已就绪 | 与A1争用共享存储资源 |
| B2 | ALU | — | B1的结果 | 等待B1完成 |
| B3 | ALU | — | 其他已就绪寄存器 | 可与B1并行 |

`thread_state.plan`记录T0/T1的初始owner和经过证明的阻塞、唤醒、切换事实。它通过`FunctionalSyncPlan`接入统一事件图；若同时存在`sync.plan`，其同步前驱也在这一入口组合。图负责先后关系与执行边界，`RecordedScheduler`负责任务状态和CPU owner，两者分工不同。

### 5.3 Bound：并行求出核内时间下界

假设前端与端口约束使A1最早在周期2发出，B1在周期3发出；A3、B3分别可在周期3、4发出。暂按4周期load下界计算：

| 操作 | 下界issue | 下界完成 | 原因 |
|---|---:|---:|---|
| A1 load | 2 | 6 | 先使用4周期load下界 |
| A2 add | 6 | 7 | 等A1结果，再计算1周期 |
| A3 add | 3 | 4 | 不依赖A1，可越过A2执行 |
| B1 load | 3 | 7 | 同样使用4周期load下界 |
| B2 add | 7 | 8 | 等B1结果 |
| B3 add | 4 | 5 | 不依赖B1，可提前完成 |

此时算出的还不是最终周期：两个load真正访问哪一级、是否排队，需要共享访存模型决定。

这里的worker是宿主回放线程。哪个worker先被宿主操作系统调度，不决定目标核先访问共享内存；目标顺序由后续coordinator按模型时间协调。

### 5.4 Coordinator与Weave：加入共享服务和跨核排队

coordinator根据模型时间处理A1、B1，而不是根据哪个宿主worker先跑完决定顺序。两笔访问均在L1D、L2和LLC未命中，进入同一个共享服务槽：

| 请求 | 到达周期 | 可开始服务 | 服务区间 | 响应周期 |
|---|---:|---:|---|---:|
| A1 | 2 | 2 | [2,22) | 22 |
| B1 | 3 | 22 | [22,42) | 42 |

B1不仅承担自身20周期服务，还因为A1先占用资源而等待19周期。分别给两核加一个独立的固定miss延迟，就会漏掉这部分跨核影响。

### 5.5 Feedback：修正依赖、资源释放和顺序退休

A1的完成从6改为22，A2随之从7改为23；B1从7改为42，B2从8改为43。A3、B3没有数据依赖，完成时刻仍分别是4、5，不跟着机械地整体平移。

| 核心 | load最终完成 | 依赖add最终完成 | 独立add最终完成 | 这3条计算的最后退休 |
|---|---:|---:|---:|---:|
| 核0 | 22 | 23 | 4 | 23 |
| 核1 | 42 | 43 | 5 | 43 |

虽然A3在周期4已经算完，仍要等程序顺序中更老的A1、A2退休；本例在周期23可一起退休A2、A3。核1同理，在周期43退休B2、B3。**执行可以乱序，退休仍按序。** load变慢还会延后LQ/ROB名额释放；若有后续指令把队列填满，就会进一步形成反压。

### 5.6 调度控制如何决定“下一段指令能否继续”

先看核0的完整物理指令流：

```text
T0计算 → 阻塞切出 → idle / 内核执行 → 切回T0 → T0继续计算
```

这个顺序已经在输入中给出，但每个边界的模型时间要重新求。核0到达“切回T0”的边界时，后面的T0指令不能只按本核下界直接开始：还要检查核1的唤醒等必要前驱是否已经完成。

1. `FunctionalSyncPlan`保存本例恢复事件需要等待的前驱，并判断它们是否已经释放。
2. 前驱尚未满足时，协调器暂不放行该边界之后的T0指令；其他满足执行条件的核心继续推进。
3. 前驱满足、边界释放后，`RecordedScheduler`检查任务已就绪并更新owner；协调器解除后续前缀的等待，继续同一套Bound–Weave–Feedback计算。

**用一个对照就能看到它如何改变时间。** 假设核0“切回T0”事件的本地候选时刻是52，该事件在本例中必须等核1唤醒T0：

| 教学情况 | 核1唤醒T0 | 核0切入边界的本地候选时间 | T0最早可以恢复 |
|---|---:|---:|---:|
| 原例 | 48 | 52 | 52 |
| 其余影响不变，只将唤醒推迟 | 58 | 52 | 58 |

第二种情况中，核0不能仍在52恢复。唤醒这个前驱晚到了，后续T0指令也要后移。**这就是调度约束影响完成时间的地方；不只是最后把等待时间分一分类。**

上表只演示这一条约束。在真实回放中，内核和idle指令、共享访存及协调器当前时间也会影响最终放行时刻，所以58是这个简化对照的最早值，不是另一次实际仿真结果。

再对应到实现，对本例switch/wakeup等执行边界，处理过程可概括为：

```text
retire_e    = 事件锚点经过 Feedback 校正后的退休时刻
release_e   = max(retire_e, 所有执行前驱的释放时刻)
admission_e = max(release_e, coordinator 当前时刻)

若是 switch：在 admission_e 交接 CPU owner，并累计前一段时间
若是 wakeup：将目标任务置为 Ready，CPU owner 保持不变
后续取指与请求的准入时间不得早于 admission_e
解除对应 producer 的边界等待，再继续回放
```

只有事件已被观察且所有执行前驱已释放，才执行上述release。`RecordedScheduler`在边界释放后处理状态和owner；真正解除producer等待、设置后续取指与请求下界的代码在协调器中。源码对应`FunctionalSyncPlan::observe/can_release/release`和`Simulator::progress_functional_sync`。[S6][S10][S11]

### 5.7 Scheduler时间线：阻塞、唤醒与重新运行

下面回到原例：核1在48唤醒T0，核0在52切回T0。核0的前3条计算在周期23退休后进入wait路径；核1在周期43完成前3条计算后执行notify。设输入中已经记录并证明以下阻塞/唤醒/切换事实，FastSim对相关指令与功能前驱重新估时后，得到下表的示意时刻：

| 模型周期 | 功能事件 | T0状态 | 核0 owner | FastSim如何处理 |
|---:|---|---|---|---|
| 0 | 初始绑定：核0=T0，核1=T1 | Running | T0 | 分别建立逻辑任务状态和物理核owner |
| 27 | 已证明阻塞的switch：T0→idle | Blocked | idle | 先累计T0已运行区间，再切换owner；核0继续回放idle/内核流 |
| 48 | 核1的wakeup(T0) | Ready | idle | 只更新T0的就绪状态，不立即让T0运行 |
| 50 | T1进入下一轮等待，核1切到idle | Ready | idle | 结束核1的T1运行区间；核1物理流仍继续 |
| 52 | 核0的switch-in：idle→T0 | Running | T0 | 检查T0已有唤醒证据，更新owner，继续其内核返回与用户指令 |
| 57 | 恢复后的T0完成本窗口工作 | Running | T0 | 在共同终点关闭统计，核对各核owner覆盖 |

表中的27、48、50、52、57都是教学设定，表示FastSim校正后的目标时刻；不是计划提供的源tick，也不是额外收取的固定切换延迟。switch边界按`task-oncpu-switch-entry-v1`，在相应switch-entry锚点最后一个UOP退休后交接owner；已回放的调度指令不再重复收费。

```text
核0 owner：T0 [0,27) ── idle/其他 [27,52) ── T0 [52,57)
T0状态：   Running   ── Blocked [27,48) ── Ready [48,52) ── Running
核1 owner：T1 [0,50) ── idle [50,57)
```

这里有两个关键点：**wakeup不等于switch-in；任务Blocked不等于物理核停止。** 核0的idle、IRQ和调度相关指令仍经过Bound、Weave与Feedback，也可能改变cache和内存排队。只是它们按当时owner归入其他时间或相应统计范围，不被直接当作T0一直在运行。

当前scheduler重放已记录的owner切换与就绪约束，并不在周期48重新决定“马上调度谁”。T0后续指令要在匹配的切换边界及功能前驱允许后继续；实际wakeup到switch-in的目标时刻由回放时序确定。[S10][S11]

**图2｜完整教学时间线：访存竞争、任务状态与CPU owner。**

![双核例子与scheduler时间线](../tmp/stage2-report/assets/example-timeline.png)

### 5.8 跨窗口继续执行，并完成统计与验证

如果时间窗口在响应返回前结束，相关依赖和所用路径支持的未完成服务状态必须传给后续窗口，不能把load当成已经完成或重新收费。这也是跨窗口状态管理的作用；当前哪些服务能跨窗口准确保留，仍取决于启用的模型路径。

在本例完整owner覆盖的前提下：

| 核心 | 所选任务active周期 | 其他周期 | 共同elapsed周期 |
|---|---:|---:|---:|
| 核0 | 27 + (57−52) = 32 | 52−27 = 25 | 57 |
| 核1 | 50 | 57−50 = 7 | 57 |

两个任务的active周期之和为82，elapsed仍是57周期，不能把两个核的elapsed相加。T0在wait系统调用中尚未切出CPU的[23,27)仍属于其active时间；被唤醒但尚未切入的[48,52)仍不属于其运行时间。

active CPI应为`82 / (N_T0 + N_T1)`，其中N是相同统计范围内各任务的实际宏指令数，包含按owner归属的内核工作，不能只用前半段的6条指令作分母。前6条计算单独贡献2笔需求读，以及各2次L1D、L2、LLC tag miss；整个回放的计数还须加上其余用户/内核/idle指令的实际事件，并区分任务与物理范围。

宿主吞吐用实际测量用户UOP数除以宿主墙钟，不能用这57个目标周期计算。最后再与相同窗口的独立gem5输出比较CPI、elapsed和各事件；本教学例没有gem5参考，不计算精度误差。本例的每段时间都有已确认的owner；真实输入若缺少这类证据，就不能照搬本例计算active CPI。

这个例子串起了完整流程：**FST与功能计划提供指令、依赖和切换事实 → Bound计算核内下界 → Weave处理共享竞争 → Feedback传播延期 → 事件图与协调器判断边界能否释放 → RecordedScheduler更新任务状态与owner → 协调器放行后续指令，继续计算。** 最后按相同范围汇总active CPI、elapsed、PMU及宿主吞吐。真实模型中，反馈后的到达变化还可能改变后续cache路径和排队；前文所述单次规范分类近似仍然存在。

内存等待和调度等待在这里有明确区别：等待内存响应时任务仍在CPU上，属于active；已经切出CPU的阻塞等待不计入该任务的active。恢复边界被释放、T0重新拥有CPU以后，后续指令仍接着经过同一Bound–Weave–Feedback循环。

## 6. 实验结果

### 6.1 实验设置与统计口径

实验按serial、buffer TSO、scheduler三个版本分别报告，每个版本给出CPI、PMU和吞吐量。优先采用完整测试集合上的维护配置，最低误差的实验候选另列；不同版本的数据集、ROI、TSO配置和统计范围不同，不据此计算版本间加速比。

CPI同时给相对误差和绝对误差/MAE，后者单位为cycles/宏指令。吞吐量注明用户UOP、混合UOP或外部整进程计时的区别。9月部分原始目录已不在工作区，相关结果按历史文档引用；10月结果已核对现存维护输出。

### 6.2 Serial版本

**CPI。** 维护v2覆盖10个负载与C4/C8/C16/C32，共40个配置；其中33个配置的宏指令数在两侧一致，使用user+kernel、common-end与功能预热。[S12]

| 结果/配置 | 数据与范围 | CPI MAPE | CPI MAE cycles/宏指令 | 说明 |
|---|---|---:|---:|---|
| Serial：维护v2代表结果 | 9/16，10负载×C4/C8/C16/C32共40案，其中33案宏指令数一致；user+kernel、common-end、功能预热 | 5.0614% | 0.054931 | 历史维护基线；不是新workload-held-out |
| Serial：同一维护v2完整集合 | 上述全部40案，含7案宏指令计数差异 | 5.6999% | 0.055726 | 完整历史诊断；与33案同时披露 |
| Serial：发现的最低聚合实验 | 同一33案，v3 response-critical候选 | 4.9821% | 0.054126 | LBM及冻结窗口回退，明确拒绝替代v2；无独立全量PMU/速度结论 |

33个配置的统计与全部40个配置同时列出，不按误差筛选样本。其余7项存在宏指令计数差异：SPH-EXA四项、Graph500 C16、NAMD C16/C32。v3虽然聚合误差最低，但LBM及冻结窗口回退，因此没有替代维护v2。

**PMU。** 以下为原生tag-miss诊断和退休branch事件，cache参考来自conserved Ruby/SLICC，branch来自原始退休BPred；不是SPR三个退休load miss。[S12]

| 事件，40个配置 | MAPE | WAPE |
|---|---:|---:|
| Branch miss | 3.2198% | 1.7054% |
| L1D tag miss | 2.4059% | 2.0290% |
| Private-L2 tag miss | 3.4008% | 2.1627% |
| LLC tag miss | 4.5141% | 1.9662% |

branch/L1/L2映射为proxy，LLC为diagnostic，不能称为已认证的真机PMU精度。

Serial逐事件P50/P90/P99 APE分别为：branch 2.1042/9.3708/13.1128%；L1D 2.1234/4.8914/8.1627%；L2 1.5709/9.1444/17.7623%；LLC 3.0728/11.7173/19.9927%。signed bias分别为−0.6745%、+0.9871%、+1.4126%、+1.9662%，finite cases均40/40。

**吞吐量。** 下列速度记录与上面的9月精度矩阵不是同一批实验，分别展示，不拼成同一个精度—速度数据点。[S16]

| 实验 | 范围与单位 | 吞吐结果 | 说明 |
|---|---|---|---|
| Serial v28.6速度快照 | 8/27，Stockfish/LBM/Graph500×C4/C8/C16/C32，12代表case，一轮；M measured user-UOP/s | 等case几何均值8.032；工作量加权8.105 | case间串行、CPU0–47/NUMA0，共享主机；非9月33案精度的同一联合点 |
| Serial C32热内核配对 | 10负载×3对，30对；旧窗口 | work-weighted 9.071→9.229 M user-UOP/s；等负载几何增益1.6966% | 可靠的历史局部热路径优化，不能推广到所有核数 |

### 6.3 Buffer TSO版本

**CPI。** 使用与gem5一致的buffer114配置，有效测试为38个配置，目标40；缺少C32 Zstd和formal C8 NAMD。历史文档“9×4”的文字与38案表有笔误，本报告保留结果摘要和已知缺项，不自行补全名单。[S14]

| 结果/配置 | 数据与范围 | CPI MAPE | CPI MAE cycles/宏指令 | 说明 |
|---|---|---:|---:|---|
| Buffer TSO：维护代表结果 | 9/24，buffer114有效38案，目标40，C4–C32 | 7.113% | 0.0584 | 历史诊断；缺C32 Zstd、formal C8 NAMD，不能写40/40 |

**PMU。** 同样只使用原生Ruby/SLICC cache参考，不用TaoTrace path-class字段替代。

| 事件，38个配置 | MAPE | WAPE |
|---|---:|---:|
| Branch miss | 3.579% | 1.558% |
| L1D tag miss | 30.505% | 20.126% |
| Private-L2 tag miss | 77.557% | 29.710% |
| LLC tag miss | 170.453% | 131.364% |

这些是历史原生tag-miss诊断，仍有prepare/demand与服务分类缺口。CPI接近不能替代PMU验证，不能以低误差单例或另一种事件定义替换全矩阵结果。分位数和signed bias在现存摘要中不可得，记为unavailable。

9月30日旧perf层的数值存在单位、forwarding、store覆盖及task范围问题，后续已更正；9月28日LBM单例L2/LLC“低于1%”也涉及错误分桶，不采用为最佳主结果。[S15]

**吞吐量。**

| 实验 | 范围与单位 | 吞吐结果 | 说明 |
|---|---|---|---|
| Buffer114转发配对 | 6个选定case、每模式4次、共享主机串行；UOP/外部整进程wall | ON范围1.666–3.418 M UOP/s | 含warmup/进程开销，非38案总体measured user吞吐 |

五负载并发批次曾记录均值1.53–5.95M UOP/s，属于并发诊断；不取其中最快单例作为整个buffer版本速度，也不据此计算相对serial的加速比。[S14][S16]

### 6.4 Scheduler版本

**CPI概览。** scheduler与store模型是不同配置维度。下面列出该版本下选定C8三案的较优store配置，以及最新具备完整退休miss参考的两案；两组窗口不同，分别解释。[S4][S13]

| 结果/配置 | 数据与范围 | CPI MAPE | CPI MAE cycles/宏指令 | 说明 |
|---|---|---:|---:|---|
| Scheduler时期：选定C8三案的较优store方案 | 10/22，Zstd/FLAC/Graph500，legacy prepare/证书 | 6.423% | 0.093386 | CPI时间合同可评分；旧FLAC原生PMU指令计数检查失败，不能称全项通过 |
| Scheduler时期：最新完整退休miss来源 | 10/26，新Zstd rate×8与FLAC speed八线程，bounded analytic | 7.4315% | 0.04978047 | 两源身份/指令计数/来源及完整分类检查通过；N=2，不代表全SPEC或全矩阵 |

**最新C8两案的CPI与PMU明细。** 最新两源使用新观察器与不同测量窗口，不能替换进10/22旧矩阵计算“同一批结果改善”。

配置为C8、8通道、3GiB、buffer/SQ有效114、8-wide、ROB192；具体时序来自该源的配置映射。范围是同一workload-task、共同窗口和各自宏分母。新源最快核目标为2M user records，不能当完整SPEC运行。

| 负载 | gem5 active CPI | FastSim active CPI | 带符号相对误差 | CPI绝对误差 cycles/宏指令 |
|---|---:|---:|---:|---:|
| Zstd rate×8 | 1.35483688 | 1.30517986 | −3.6652% | 0.04965701 |
| FLAC speed 8 workers | 0.44565836 | 0.49556230 | +11.1978% | 0.04990394 |

N=2，CPI MAPE=7.4315%，CPI MAE=0.04978047 cycles/宏指令。按验证过的task宏指令数加权，CPI WAPE=5.2991%、signed bias=−0.4412%；case等权APE的P50/P90/P99/max为7.4315%/10.4445%/11.1225%/11.1978%（Type-7）。Zstd elapsed为494.925578→476.785737 μs；FLAC为137.092103→152.443404 μs，分别为−3.6652%、+11.1978%。

| 负载/退休load事件 | gem5计数 | FastSim预测 | 相对误差 | 绝对事件差 | 参考/模型未分类数 |
|---|---:|---:|---:|---:|---:|
| Zstd L1 miss | 256,867 | 274,251 | +6.7677% | 17,384 | 0/0 |
| Zstd L2 miss | 54,007 | 58,526 | +8.3674% | 4,519 | 0/0 |
| Zstd L3 miss | 20,867 | 25,799 | +23.6354% | 4,932 | 0/0 |
| FLAC L1 miss | 14,757 | 15,166 | +2.7716% | 409 | 0/0 |
| FLAC L2 miss | 23 | 33 | +43.4783% | 10 | 0/0 |
| FLAC L3 miss | 10 | 11 | +10.0000% | 1 | 0/0 |

| 主事件，finite cases=2/2 | MAPE | WAPE | signed bias | P50 APE | P90 APE | P99 APE | max APE |
|---|---:|---:|---:|---:|---:|---:|---:|
| L1 miss | 4.7696% | 6.5506% | +6.5506% | 4.7696% | 6.3681% | 6.7277% | 6.7677% |
| L2 miss | 25.9228% | 8.3824% | +8.3824% | 25.9228% | 39.9672% | 43.1272% | 43.4783% |
| L3 miss | 16.8177% | 23.6289% | +23.6289% | 16.8177% | 22.2719% | 23.4991% | 23.6354% |

**结果如何解读。** 两个来源的合格退休load全量参与，三个事件均已完成全部合格load的分类；它们不再是删除未知成员后的子集误差。FLAC的L2百分比大但只多10次；Zstd L3多4,932次且参考已有20,867次，是需要继续解释的实质模型残差。

模型仍采用`bounded-line-fill-v1`近似时钟/容量；Zstd保留78个近似fallback标记，FLAC为0，预测总数与已分类总数一致。报告中的状态仍为incomplete-evidence，`hardware_equivalence=unverified`：完整分类不等于所有ISA与SPR真机映射已认证，结构性宏分母也不能直接包装为认证的真机MPKI分母。[S4]

**吞吐量。**

| 实验 | 范围与单位 | 吞吐结果 | 说明 |
|---|---|---|---|
| Scheduler观察事件批处理 | C4小探针、每模式5次；M measured user-UOP/s | 全门控P50 2.9779→批处理3.9991，+34.30% | 同源局部开销改进；约0.1–0.2s短窗口，不能外推应用 |
| Scheduler最新Zstd C8 | 单次归档观测；M measured user-UOP/s | 3.9257；另列用户宏MIPS=2.1868 | 不是quiet-host配对性能认证 |
| Scheduler最新FLAC C8 | 同上 | 5.7697；另列用户宏MIPS=2.7076 | 不把较快单例当整个版本吞吐 |

最新两源的计时拆账如下；完整验证还包括独立Python来源审计，未计入回放wall：


| C8归档观测 | measurement wall s | functional warmup wall s | warmup+measurement s | 输入身份哈希 s |
|---|---:|---:|---:|---:|
| Zstd | 3.944869 | 6.627749 | 10.572617 | 22.906134 |
| FLAC | 2.723805 | 18.606243 | 21.330047 | 65.804506 |

现存输出不能分出native warmup的完整用户UOP计数，主合同的端到端用户UOP/s为null。`measurement_*`、整个回放、输入哈希和独立审计分别承担不同成本，不能把其中一个当整条流程耗时。

## 7. 可以归因的改进：同源实验与反例

### 7.1 有界store解析：速度改善有配对证据

**图3｜有界store解析的配对结果：同时展示吞吐和CPI绝对误差。**

![有界store同源配对结果](../tmp/stage2-report/assets/bounded-paired.png)

固定SQLite/XZ/Marian三个C4八通道来源、同一冻结GCC11 Release binary、CPU32–47；旧prepare/证书与bounded交替各三轮，共18次串行回放。计时窗口无其他FastSim或编译，独立来源审计暂停后恢复，未按负载调延迟。[S3]

| 负载 | 旧方案M user-UOP/s中位数[min,max] | bounded中位数[min,max] | 速度增益 |
|---|---|---|---:|
| SQLite | 0.9930 [0.9888,0.9946] | 1.1586 [1.1565,1.1609] | +16.68% |
| XZ | 0.7050 [0.7027,0.7063] | 0.8924 [0.8907,0.8941] | +26.59% |
| Marian | 0.7707 [0.7605,0.7709] | 0.9345 [0.9270,0.9350] | +21.25% |

| 负载 | gem5 CPI | 旧方案CPI | bounded CPI | 旧/新相对误差 | 旧/新CPI绝对误差 cycles/宏指令 |
|---|---:|---:|---:|---|---|
| SQLite | 1.000925 | 0.878154 | 0.877033 | −12.2657% / −12.3778% | 0.122771 / 0.123892 |
| XZ | 1.081955 | 1.044796 | 1.039751 | −3.4345% / −3.9007% | 0.037159 / 0.042204 |
| Marian | 2.665484 | 3.122988 | 3.033738 | +17.1640% / +13.8157% | 0.457504 / 0.368254 |

| 同一三案，case等权 | CPI MAPE | CPI MAE cycles/宏指令 | WAPE | signed bias | P90 APE | P99 APE | max APE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 旧prepare/证书 | 10.9547% | 0.205811 | 13.0614% | +5.8690% | 16.1844% | 17.0660% | 17.1640% |
| bounded | 10.0314% | 0.178117 | 11.3416% | +3.8958% | 13.5281% | 13.7869% | 13.8157% |

这组证据支持“减少全窗口迭代并改善该三案的联合取舍”；SQLite/XZ精度略退，主要精度收益来自Marian，不是所有case都改善。

当前支持域要求TBE≥max(SQ,buffer)+2LQ+1，store接口≥max(SQ,buffer)+2，optional队列为0；四核SQ114/LQ32下门槛179/116，实配256/200满足。排队与驻留仍用预测，绑定资源通用适配、完整流式游标和精确反作用未完成。此轮退休PMU参考资格不完整，不发布成新的完整PMU主精度；Peak RSS未测，held-out和配置趋势未完成，故仍opt-in。

### 7.2 反例：新模型没有在另一组C8来源全面变好

10/22相同Zstd/FLAC/Graph500三源对照中，legacy prepare的CPI MAPE/MAE为6.423%/0.093386；bounded为8.159%/0.125533 cycles/宏指令。Zstd和Graph500退化，FLAC基本不变；该旧FLAC的CPI时间合同可评分，但原生PMU指令计数检查失败，不称整项通过。不能只展示上一组三案pilot收益，再宣称全矩阵优于旧模型。[S13]

### 7.3 Scheduler：计数和因果关系正确，不等于时序误差已消失

C4小探针的默认观察事件批处理，active CPI为gem5 0.807098、FastSim 0.908684，相对+12.5866%、绝对0.101586 cycles/宏指令；N=1的MAE同值。elapsed却为41.645646→33.860439 μs，−18.6939%。两者方向不同，正说明必须分别报告active和elapsed。[S10]

10/14新task PMU来源修复了此前内核前缀/物理范围不匹配；其active CPI 0.804791→0.898957，相对+11.7006%、绝对/MAE 0.094166 cycles/宏指令；统计新增的配对吞吐P50为3.96156→3.89615M user-UOP/s，−1.65%。来源修复没有改目标时序，也不是精度优化。[S11]

对LBM/Graph500新源的同源消融，还发现关闭全部同步/任务计划后主要elapsed偏差仍在；更早二进制在相同无计划输入上产生完全相同的基础周期。因此，现有证据不支持把主要误差简单归因于新增scheduler。它也不意味着scheduler对分块/交错完全没有影响。[S17]

## 8. 相对Sniper、zsim的价值：已有结果与公平边界

### 8.1 先承认已有技术路线

Sniper和zsim已证明高抽象多核仿真的可行性：Sniper有mechanistic interval及IW-centric核心，zsim有instruction-driven OoO、多核bound-weave和用户态虚拟化。FastSim也采用高抽象多核建模路线，具体实现采用统一FST、并行核内下界计算、确定性共享访存协调和响应反馈；应比较这套实现如何影响精度、吞吐及参数响应。[P1][P2][P3]

| 对象 | 已有强项 | 与FastSim的关系 |
|---|---|---|
| gem5详细FS O3/Ruby | 完整系统执行与较细机制参考 | 本项目的独立微架构基线；FastSim以功能回放换取效率 |
| Sniper | interval/IW-centric、共享多核快速研究 | 直接方法前作与可比工具，需要冻结具体模型/前端/版本 |
| zsim | DBT加速、详细OoO、bound-weave、大规模并行 | 两阶段共享竞争建模的直接前作，不能声称它不建多核或竞争 |
| FastSim | 统一功能IR、参数化核内/存储模型、常驻worker、共享协调与响应反馈 | 围绕指定gem5配置组织高吞吐回放；用同源实验检验精度和参数响应 |

经典工具对完整guest kernel的范围与FastSim native-kernel replay不同，但不能笼统宣称竞品没有OS/MMU相关能力；例如Virtuoso已在Sniper上扩展轻量OS功能。FastSim也没有重新执行完整Linux来产生任意新调度路径。[P4]

### 8.2 本地确有历史同源直接对照

**图4｜历史竞品对照：时间代理误差较低，吞吐各有胜负。**

![历史竞品精度与速度](../tmp/stage2-report/assets/competitor-history.png)

9/16历史文档记录了10负载×C4/C8/C16/C32共40案，三家消费相同FST冷slice，逐核UOP一致。Sniper/zsim均有较多本地FST、依赖、FU/DRAM等适配，应称“本地FST fork”。两家重放相同内核记录，但没有等同的特权模型；内核branch守恒曾放宽为警告。[S18]

其时间代理是`cores × max(core cycles) / mixed UOP`，**不是现行task-active宏CPI**。以下相对误差与绝对误差只在这份历史代理内解释：

| 历史40案 | 时间代理MAPE | 时间代理MAE cycles/UOP | P90绝对误差 cycles/UOP | 正式active宏CPI MAE cycles/宏指令 |
|---|---:|---:|---:|---|
| FastSim历史快照 | 7.26% | 0.0634 | 0.1471 | unavailable |
| Sniper FST fork | 45.19% | 0.5244 | 2.0745 | unavailable |
| zsim FST fork | 35.91% | 0.4393 | 1.4200 | unavailable |

文档记录FastSim在37/40案绝对代理误差最小；其MAE约为同表Sniper的1/8.3、zsim的1/6.9。此为历史条件下的结果，不作为当前版本、原生upstream或真机精度优势倍数。

| 目标核数 | FastSim历史中位吞吐 | Sniper FST fork | zsim FST fork |
|---|---:|---:|---:|
| C4 | 6.35 | 1.68 | 9.42 |
| C8 | 7.83 | 2.71 | 8.89 |
| C16 | 7.69 | 5.57 | 9.50 |
| C32 | 7.55 | 8.92 | 9.77 |

上表单位全为**M mixed UOP/s，simulator-only**，与前文measured user-UOP/s不同。它支持FastSim在C4/C8/C16快于该Sniper fork；zsim四档均更快，C32的Sniper也更快。**可主张的是历史对照中的更低代理误差与同量级吞吐的取舍，不是全面最快或严格Pareto支配。**

上述原始CSV和run-manifest目录目前缺失，本次只能核对curated文档与比较脚本，不能重算37/40、波动和宿主预算。9/17已有评审也指出其冷slice、UOP分母与fork范围限制；本报告采用这一收紧后的解释。[S19]

### 8.3 FastSim的技术价值

**可组合的性能模型。** 统一功能流连接核内执行、cache/coherence、DRAM与线程约束；这些组件通过请求和响应时间相互作用，能够下钻解释瓶颈，也便于替换或改进某个子模型。

**面向吞吐的执行框架。** 常驻worker并行计算核内下界，coordinator集中维护共享状态，再通过反馈传播服务延期。稀疏状态和块级处理控制模拟器本身的开销。它不是FastSim独有的方法类别，价值要落实到具体实现和同条件结果。

**已有实验支持的精度—速度取舍。** 历史定制对照显示较低的时间代理误差，吞吐与zsim处于同一数量级但更慢；局部配对实验也量出了解析store路径的收益。两类结果各自说明适用条件，不合成未经测量的全工具链优势。

**面向配置探索的参数化基础。** 宽度、窗口、执行资源和cache/内存几何可以作用于模型状态。最终还要用配置变化方向、幅度、排序和总成本证明选型价值；当前不把参数接口存在当作DSE已验证。

### 8.4 下一轮竞品对照怎样才能形成正式优势主表

固定同binary/input/ROI/功能指令流和预热，冻结upstream与fork差异；Sniper分别标interval/IW-centric，zsim标详细OoO及并行预算。逐核匹配macro/UOP/load/branch和scope；无法提供task-active范围的工具，先做共同用户态子域，而不是替它补零或假造active CPI。

同宿主、同核预算、固定NUMA/affinity，多轮交错、case串行计时；同时报告CPI相对误差、绝对误差/MAE、完整PMU、用户UOP/s、端到端wall、Peak RSS及失败case。FS/调度能力展示单独列出，不用不支持范围拉低竞品均值。再加未见配置的delta方向、幅度和排序，才足以形成当前版本的精度—速度结论。

## 9. 已知架构改物理配置，应该怎么做

这里的“物理配置”包括资源容量/数量、流水宽度、cache几何、内存拓扑和时钟。**可解析参数 → 运行时资源真正激活 → 同家族变体趋势正确 → 未见微架构成立**是四个不同层次。[S1][S9]

### 9.1 当前参数化基础与使用边界

| 改动 | 可利用的实现 | 需要防止的误判 |
|---|---|---|
| width、ROB/IQ/LQ/SQ、FU | 容量、带宽和可用时间日历 | 无wrong-path/rename模型时，大ROB收益可能失真 |
| L1D/L2/LLC容量、相联度、替换 | 实际tag状态与home/CHA映射 | 基准CPI准不证明fill/order变化趋势准 |
| DRAM channel/rank/bank、时序 | 地址映射、row/bus日历及混合队列 | 调度bypass、refresh缺口和时钟单位必须检查 |
| 预测器/DTLB已有几何 | 已实现状态机的table/entry参数 | 换容量不等于支持TAGE或全新walker |
| 核数与静态频率 | 线程/核分离、reference时间域 | 同步/调度路径或物理映射改变时trace未必可原样复用 |

### 9.2 一个可执行的配置变体流程

1. 冻结负载、二进制、输入、任务/线程和ROI；明确只评估固定功能路径，还是需要新功能执行。
2. 从目标gem5 run的最终`config.ini`和effective target映射容量、延迟与队列，记录单位和组合边。LLC总容量与每bank容量要区分；不能借用另一配置的延迟。
3. 检查FastSim对应机制实际打开且被触发，输出configured/effective值和激活计数；不能只看配置文件。
4. 每个目标配置准备身份一致的参考来源。单独验证身份、共同窗口、指令计数、native/retired服务与参数来源，再做误差计算。
5. baseline两侧取值，冻结模型，不按case调常数；同时测绝对CPI/MAE、PMU、delta、排序和吞吐。
6. 对同步/竞争敏感负载检查功能路径是否仍适用。若硬件变化会改变spin次数、锁赢家或调度，需要新功能来源，或明确限制为recorded路径条件下的估时。

趋势验证建议使用：`delta_gem5 = CPI_variant / CPI_base - 1`，FastSim同样计算；比较方向、绝对delta差、成对排序及Top-1配置的regret。只有gem5存在可辨识变化的维度才算被激励，接近零变化不能充当高准确方向证据。

项目曾设计17配置×C4/C8×4负载共136案的泛化矩阵，但9/17记录为smoke 34通过/102失败、正式采集未开始。该计划不是已完成结果，后续需要按现行合同重新确认完成状态。[S20]

## 10. 跨微架构时，哪些能复用，哪些必须重建

### 10.1 同ISA、相近OoO家族

可复用功能输入接口、依赖框架、两阶段调度和验证基础；重新映射FU分类/latency/pipelining、前端/窗口、branch、cache/coherence、TLB和DRAM。不把当前load/store固定边、walk延迟或网络常数直接拷到新CPU。

真实x86微架构的UOP划分和融合可能不同，FST的宏/UOP对应必须重审。模型已有机制可参数化的部分走配置映射；缺机制的部分实现并验证后再开放，不能以“参数可填”代替支持。

### 10.2 同ISA、机制发生明显变化

更换预测器家族、cache包含关系、coherence协议、内存控制器，或加入SMT/复杂迁移，可能需要新状态机而不是改容量。完整wrong-path、ITLB、rename、Ruby transient和refresh等现有缺口，在新机器上可能成为主导项，应建立支持矩阵并据实标为未验证/不支持。

### 10.3 跨ISA或跨功能生产者

ARM/RISC-V等需要新decode/lowering、寄存器依赖、memory ordering、atomic/barrier、异常/系统调用及退休事件规则；DR/QEMU等生产者还需要物理地址/ASID、同步与owner、功能请求角色和身份绑定。统一TraceSource降低接入耦合，却不会自动补齐这些事实。

### 10.4 泛化的最终验收

模型参数与支持集在评估前冻结，分别报告未见workload、未见配置、未见核数和未见微架构，不能统称held-out。先验证基线身份/事件语义，再验证绝对精度，最后验证配置响应与同预算决策质量。

当前有这种扩展路线和部分参数化基础；**尚无覆盖完整未见微架构、完整退休PMU及配置排序的闭环证据**，不以两个最新来源或旧40案替代。

## 11. 工作总结与后续计划

### 11.1 技术框架与实验结论

FastSim的核心是统一功能输入之上的参数化两阶段多核模型：用IntervalCore计算依赖与有限资源约束，以coordinator组织共享cache/一致性/DRAM访问，再将服务响应反馈到执行、队列释放与退休。常驻worker、稀疏scoreboard、块级summary和紧凑资源日历负责降低回放开销。

实验结果说明，这套框架已能在部分测试集合上取得较好的CPI精度，并保持较高回放吞吐；但共享访问顺序、填充可见性和部分store路径仍存在近似，PMU与新负载尾部也暴露出不足。各版本的具体CPI、PMU和速度分别见第6章，同源模型消融见第7章。下一步重点是完善这些模型约束，并验证改配置后能否正确预测性能变化。

### 11.2 后续重点工作

| 工作包 | 要解决的问题 | 明确交付/验收物 |
|---|---|---|
| A. 统一当前回归与竞品矩阵 | 历史raw缺失、口径不同、版本不一致 | 当前冻结binary/config/source、全部逐案输出、同预算三方表与尾部 |
| B. 共享服务与store因果闭合 | canonical分类/实际到达分离，部分buffer事件误差大 | 逐请求首个分歧、机制修复、反向负载控制、精度与速度配对 |
| C. 退休事件与硬件语义 | 完整分类尚不等于SPR认证 | 官方事件字典、CPU身份、无multiplex真机微核、全量资格与单位审计 |
| D. 同家族参数趋势 | 基准误差不能代替DSE可靠性 | 冻结未见配置，delta方向/幅度、排序、Top-1/regret与失败清单 |
| E. 功能源与成本 | gem5采集/存储/哈希成本可能主导小窗口 | DR/QEMU适配的可部署子域，采集/转换/预热/回放/验证/RSS完整成本 |

K次复用的总成本应写为`T_collect + T_convert + Σ(T_warmup + T_replay + T_required_validation)`，按实测说明哪些身份/审计成本可安全缓存。当前没有同ROI完整gem5成本对照，因此本报告不给FastSim相对gem5的端到端加速倍数。

### 11.3 汇报结语

本次工作的重点是FastSim技术框架的实现与完善：将核内并行计算、跨核共享状态和响应反馈组织在同一模型中，并通过紧凑状态与块级处理控制开销。实验用于说明这套框架的精度—吞吐表现和当前限制；后续将继续检验组件改进与跨配置预测能力。

## 12. 专家问答准备

| 可能追问 | 建议回答 |
|---|---|
| 为什么不直接用Sniper/zsim？ | 它们是成熟前作。FastSim围绕统一FST、参数化核内/存储模型、常驻worker与共享协调形成自己的实现；历史同源适配对照显示不同的精度—吞吐取舍，当前版本仍需公平复验。 |
| 你们是不是在复制gem5答案？ | 在线输入是功能事实和目标配置，gem5时间/cache答案只进入独立评分；同步/owner plan不含源tick。功能流仍固定，所以不宣称能预测任意新调度路径。 |
| CPI很准但PMU很差，为什么？ | 时序可能有误差抵消，cache请求与退休load又不是同一类事件；buffer历史正是反例，需要独立事件与因果修复，不能凭一个CPI宣布整体准确。 |
| scheduler加入后误差变大，是否设计错了？ | 不同窗口不可直接归因。同源消融显示主要偏差关闭计划后仍在，旧binary亦复现基础周期；scheduler确实能影响分块/交错，但不是已证明的全部根因。 |
| bounded是不是已经替代旧模型？ | 没有。三案pilot联合改善，但另一C8三案CPI退化；通用绑定资源、held-out和完整推广门未完成，保持opt-in。 |
| 全部分类是否说明真机PMU准？ | 只说明声明的统计范围内完整分类。硬件资格、事件规则、CPU身份和真机微核还要独立认证；最新Zstd L3仍有23.64%误差。 |
| 改cache/ROB后能直接相信结果吗？ | 参数可作用于状态是第一步；还要相同来源、配置激活、基线两侧delta和排序验证。对会改变同步路径的情形，需要新功能来源或限制反事实范围。 |
| 相比竞品是否最快？ | 历史数据不支持。zsim四档更快，Sniper在C32也更快；FastSim价值主要在较低历史代理误差与同量级吞吐，以及本项目要求的语义/审计能力。 |

## 附录A：本次证据核对与复现入口

本次只做报告整理与现存结果提取，没有改变模型、默认配置或参考计数，没有新跑gem5/FastSim性能实验。当前工作区包含尚未提交的实现；Git HEAD不能单独标识本次全部源码。随文证据清单记录关键源码/文档与现存JSON的SHA256，不把已清理FST或历史二进制冒充当前重新认证。

| 证据包 | 仓库相对路径 | 本次核对程度 |
|---|---|---|
| 最新两源维护汇总 | `tmp/spr-retired-zero-unknown-20261026/implementation/final-two-case/validation/summary.json` | 实际读取2/2、失败0、pending0、CPI/MAE、task scope、retired主namespace |
| 最新两源逐案验证与stats | 同目录`cases/<case>/validation.json`及`../stats/<case>.json` | 实际读取CPI/指令计数、三个事件原始计数和未分类数、近似fallback、measurement/warmup/哈希wall |
| 三案bounded维护提取 | `tmp/bounded-store-timing-20261021/results.json` | 实际读取CPI聚合、native/retired分域，未重选参考 |
| bounded配对计时 | 同目录`benchmark-expanded.json`、`benchmark-results.json` | 实际读取归档分布、合同与原始轮次；非新测量 |
| 历史serial/buffer/三方实验 | 下列curated docs所引9月tmp目录 | 当前原始目录缺失，只保留文档证据级别 |
| 本报告提取与核验收据 | `tmp/stage2-report/evidence-extract.json`、`source-manifest.json`、`report-verification.json` | 小体积附件，保留路径、关键数值及核验范围 |

维护验证入口为`tools/run_native_kernel_fastsim_validation.py`；现存输出重评分使用`--source-root`与`--stats-dir`，目标身份由`tools/validate_fs_oracle_identity.py`单独核对。新来源还须核对全部预期case及参数来源，不把发现到的较小集合当完整通过。

## 附录B：源码与内部文档索引

以下路径均相对ByteDance Codebase仓库`yinhaolang/minesim`的FastSim工作树；本报告不假设dirty工作区内容已推送。

| 编号 | 来源 | 支撑内容 |
|---|---|---|
| S1 | `docs/project-goal-and-semantic-contract.md` | 需求、三层基线、绝对/趋势目标；8月状态表按历史理解 |
| S2 | `docs/accuracy-reporting-contract.md` §0/§8 | 来源、事件、指令计数、MAE、统计与吞吐合同 |
| S3 | `docs/bounded-store-timing-results-20261022.md` | 解析store支持域、三源CPI与18次配对回放 |
| S4 | `docs/spr-retired-cache-observer-repair-20261026.md` | 最新C8两源、完整指令范围、分类覆盖与SPR边界 |
| S5 | `docs/fst-v7-drmemtrace-conversion-contract.md` | 功能IR、drmemtrace接口及未完成适配 |
| S6 | `include/fastsim/simulator.hpp`、`src/simulator.cpp` | 多核两阶段、共享状态、response与测量流程 |
| S7 | `include/fastsim/types.hpp`、`trace.hpp`、`interval_core.hpp`；`src/trace.cpp`、`interval_core.cpp`、`cache.cpp` | FST、核心约束与cache状态 |
| S8 | `src/predictor.cpp`；`docs/branch-recovery-tail-fix-results-20261020.md`、`sqlite-residual-cpi-analysis-20261020.md` | 预测与恢复传播及检测阶段限制 |
| S9 | `docs/gem5-parameter-coverage.md`；`pending_fill.hpp`、`mixed_dram.cpp`、`configs/gem5-fs-native-kernel.cfg` | 生效参数与未闭合机制，API覆盖不等于等价 |
| S10 | `docs/recorded-scheduler-results-20261013.md`、`fastsim-functional-sync-consumer-results-20261013.md` | 同步、owner、active/elapsed、批处理开销 |
| S11 | `docs/task-pmu-repair-results-20261014.md` | task PMU来源、前缀覆盖、计数开销与参数缺口 |
| S12 | `docs/store-wait-v2-default40-20260916.md`、`private-l2-pmu-source-repair-20260916.md`、`store-wait-v3-critical-bank-20260916.md` | serial维护/候选、原生PMU、指令计数差异 |
| S13 | `docs/spec-c8-accuracy-results-20261022.md` | 三C8同源旧/新store对照与旧FLAC指令计数失败 |
| S14 | `docs/store-forwarding-default-on-fullmatrix-20260924.md` | buffer38案CPI、PMU及外部wall速度 |
| S15 | `docs/perf-pmu-semantic-audit-20261014.md`、`buffer-tso-l2-caliber-fix-20260928.md` | 历史perf/分桶结论更正 |
| S16 | `docs/fastsim-v28_6-c4-c32-baseline-2026-08-27.md`、`buffer-tso-prepare-throughput-fix-options-20260924.md` | 旧代表集/热内核速度与buffer并发诊断 |
| S17 | `docs/cpi-regression-scheduler-ablation-20261016.md` | 新源大误差同源消融与旧binary反证 |
| S18 | `docs/sniper-zsim-fs-trace-driven-comparison.md` §0；`tools/compare_fs_trace_driven.py` | 历史三方矩阵与真实代理定义 |
| S19 | `docs/fastsim-ccfa-publication-assessment-20260917.md` §2.4 | 冷slice、UOP口径、fork与竞品解释限制 |
| S20 | `docs/uarch-generalization-c4-c8-20260917.md` | 136案设计及smoke失败/正式未开始状态 |

## 附录C：公开前作与比较口径

- [P1] [Sniper官方Interval Model](https://snipersim.org/w/Interval_Model)：mechanistic interval、窗口/依赖与miss重叠；[SC2011官方论文页](https://snipersim.org/w/Paper:Sc2011Carlson)。
- [P2] [Sniper IW-centric，TACO2014官方论文页](https://snipersim.org/w/Paper:Taco2014Carlson)：已有提高核心建模精度的后续路线，不能把Sniper一概视为早期interval配置。
- [P3] [zsim ISCA2013作者论文](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf)、[作者演讲](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.slides.pdf)：instruction-driven OoO、bound-weave和用户态虚拟化；[upstream说明](https://github.com/s5z/zsim)。
- [P4] [Virtuoso原作者论文](https://arxiv.org/abs/2403.04635)：Sniper上的轻量OS功能扩展，说明相关工作需比较具体scope。
- [P5] [gem5官方介绍](https://www.gem5.org/about/)、[gem5 v20+论文](https://arxiv.org/abs/2007.03152)：详细模拟平台及多执行模式背景。

论文MIPS、IPC误差与本报告不同宿主、规模、scope，未进行横向相除。zsim原论文的单线程平均绝对IPC相对误差为9.7%，后续[MIT官方教程](https://zsim.csail.mit.edu/tutorial/slides/intro.pdf)为8.5%；多线程11.2%是performance error。这里仅用于说明版本和指标应分清，**不与FastSim CPI MAPE排列优劣**。相应宏CPI绝对误差/MAE没有可转换原始数据，保持unavailable。

## 附录D：随文证据附件

云端文档附带三个小体积JSON文件：`evidence-extract.json`保存现存维护输出的数值与范围；`source-manifest.json`保存本次阅读的关键源码、文档和输出哈希；`report-verification.json`保存136项数值检查及核验范围。附件不包含大体积FST，不把历史原件缺失的问题包装为可重现。四张图已作为原生图片插入对应章节；框架图和例子时间线另附SVG源文件，便于放大与修改。
