# 十项 perf cache 指标的真机、gem5、FastSim 统一设计

状态：用户已批准并进入实施。事件合同、独立观察器和校验路径已接入；完整请求
覆盖及三端硬件资格尚未完成。本文固定用户要求的十个字段，优先处理
`perf_l1d_load_misses`、`perf_l2_demand_misses`、`perf_llc_misses_core`。
此前[退休计数设计](2026-10-14-perf-retirement-repair-design.md)只实施了第一阶段，
并明确将完整 L2/LLC 请求人口留待后续；本文补充这一目标。

## 1. 目标和验收对象

三端必须使用相同的硬件事件定义、计数单位、请求资格、计数边界和测量范围。
以 Sapphire Rapids 的官方事件定义为共同标准。gem5 提供独立硬件机制观察，
FastSim 根据自己的功能输入与模型预测事件；原生 Ruby tag miss 继续作为机制
诊断，不替代用户要求的十项指标。

区分四个结论：事件定义已绑定、参考采集完整、模型请求类型已覆盖、真机验证
已通过。定义一致不保证数值相等；数值接近不证明定义或覆盖一致。
缺少证据的事件以 null 和原因报告，不能给缺失 oracle 填0或纳入误差均值。

用户提供的历史 MAPE/max APE 是待复现的旧结果，不是本设计的新验证结果或
预设精度门槛；原样保存在 `tmp/perf-cache-threeway-20261015/requested-baseline.json`。
本次不把 10-15 private-L2 native 桶重组的误差当成这些 perf 字段的精度。

## 2. 平台、来源和十项绑定

现场主机：Intel Xeon Platinum 8457C，family=6/model=143/stepping=8。
官方表 SPR V1.40，2026-09-09，完整文件 SHA256：
`95caad89cbe4c96647a540897251e1998edc7eee8efb2dc84a5b13f7d94ce102`。
表的 URL、版本、十项原始编码及主机缓存身份保存在
[`tmp/perf-cache-threeway-20261015/`](../../../tmp/perf-cache-threeway-20261015/)。

| 字段 | 官方事件 | event / umask | 单位与域 |
|---|---|---|---|
| `perf_retired_loads` | `MEM_INST_RETIRED.ALL_LOADS` | D0 / 81 | 硬件事件定义下的退休 load 指令 |
| `perf_retired_stores` | `MEM_INST_RETIRED.ALL_STORES` | D0 / 82 | 硬件事件定义下的退休 store 指令 |
| `perf_l1d_load_hits` | `MEM_LOAD_RETIRED.L1_HIT` | D1 / 01 | 满足 L1_HIT 条件的退休 load 指令 |
| **`perf_l1d_load_misses`** | **`MEM_LOAD_RETIRED.L1_MISS`** | **D1 / 08** | **满足 L1_MISS 条件的退休 load 指令** |
| `perf_l1d_load_fb_hits` | `MEM_LOAD_RETIRED.FB_HIT` | D1 / 40 | 满足 fill-buffer 来源条件的退休 load 指令 |
| `perf_l2_accesses` | `L2_RQSTS.REFERENCES` | 24 / FF | L2 hit 或 true-miss 请求 |
| **`perf_l2_demand_misses`** | **`L2_RQSTS.ALL_DEMAND_MISS`** | **24 / 27** | **L2 demand miss 请求，包含数据读、RFO、取指** |
| `perf_l2_writebacks` | `L2_TRANS.L2_WB` | 23 / 40 | 官方定义的 L2 writeback 事务 |
| `perf_llc_accesses_core` | `LONGEST_LAT_CACHE.REFERENCE` | 2E / 4F | 核心发起、符合事件资格的可缓存 LLC 请求 |
| **`perf_llc_misses_core`** | **`LONGEST_LAT_CACHE.MISS`** | **2E / 41** | **上述请求中的 LLC miss** |

官方来源：[SPR 事件表](https://raw.githubusercontent.com/intel/perfmon/main/SPR/events/sapphirerapids_core.json)、
[事件说明](https://perfmon-events.intel.com/platforms/sapphirerapids/core-events/core/)。
前五项是退休域，后五项是请求域。L2/LLC 事件会统计符合资格的推测请求；
不能为便于使用 committed trace 而将其重新定义为“只统计已退休数据访问”。
L2 REFERENCES 包含预取等请求，范围大于 demand-only；LLC 两项覆盖代码、RFO、
推测访问和面向 L1/L2 的硬件预取，不包含面向 L3 的硬件预取。
硬件 true-miss 合并规则按具体事件实现，不用 Ruby 任一层的 merge 桶统一替代。
`L2_TRANS.L2_WB` 的具体事务方向还要用 L1 脏逐出进入 L2、L2 脏逐出和一致性
写回分别验证，不能只凭名字将它等同 dirty L2 eviction，也不能用
`L2_LINES_OUT.NON_SILENT` 静默替换。

## 3. 三个重点 miss 的计数位置

### 3.1 `perf_l1d_load_misses`

在架构宏指令完成退休时，根据其实际成员的硬件事件资格累计。split load、
多访存 UOP、跨 chunk 宏边界不能变成多条退休指令。RMW、REP、软件预取、mask、
fault/restart 的资格单独验证；中断 ROM 伪宏不能直接当硬件退休 x86 指令。

gem5 从 LSQ 的实际执行、转发和 Ruby 生命周期提供成员事实；FastSim 从自己的
store/load 生命周期、L1 lookup、填充完成与 follower 关系提供估计。需要区分：
完整 store forwarding、部分覆盖后重试、真正未返回填充的 follower、热 L1 alias、
跨行 mixed source、重执行后实际供给。已有 `StoreForward` 原生标记位于完整
字节覆盖分支，可复用此事实；若转发前已有请求在途，这些请求仍属于请求域。

为三个 L1 来源事件分别保存资格/未知状态，不强制以“miss 优先”的单一枚举
代表所有硬件事件。现有 `hits+misses+fb_hits=classified_loads` 只能作为旧 scalar
映射的内部守恒，不能作为三端硬件合同。尤其不能将 store forwarding 一律计为
FB_HIT，或将所有 coalesced 计为 FB_HIT。完整 forwarding 的硬件事件映射由专项
真机内核确认后，才能补足此前 unavailable 的来源计数。

### 3.2 `perf_l2_demand_misses`

在符合资格的 L2 demand 事务获得本层结果时计数，不在指令退休回调里根据最终
hit level 推导。请求类型至少区分 data read、RFO、instruction、软件预取和各级
硬件预取，按该事件选择资格；不能将每条 store 直接当成一个 RFO。

保存 L2 自身的 tag/data/permission 状态、在途 parent/merge、接纳和重试身份。
只被 backpressure 拒绝的尝试不重复计数；目标处理器会计数的重执行事务不能
因它们拥有相同架构指令身份而全部去重。remote supply 是后续供给方式，不会
取消已经成立的 L2 demand miss。buffer prepare、head demand 是否形成一次或
多次合格事务，依据真正发出的请求及本层处理结果判断。

### 3.3 `perf_llc_misses_core`

在核心发起、满足官方资格的 LLC 事务获得 LLC 结果时计数，单独保留 originating
core 与请求类别。它不等于 DRAM read、唯一内存 fill、某个 CHA/uncore 总数，也
不保证等于 L2 demand miss。

必须分开 LLC directory/tag presence、有效数据可用性和最终 responder。Ruby 的
remote-supply outcome 与 SPR 的 LLC hit/miss 不是已经认证的一一映射：不能
仅凭“远端供给”统一判为 LLC hit 或 miss，也不能仅以 `kMemory` 作为硬件 LLC
miss 的充分且完备分类。用同 socket 干净共享、脏共享、目录命中但数据缺失等
内核验证映射；映射未确定时，该细分类保持显式未认证。

## 4. 设计选择与两个阶段的接入

仅重组旧桶可以帮助定位，但缺少完整请求人口，不能满足最终目标。推荐保留
FastSim 两阶段结构，增加独立的退休事件账本和请求事件账本；另一种完整功能/
时序后端可重建更多请求，但成本更大，不作为默认路径。

生产阶段输出架构事实、指令身份、依赖、地址空间、owner/privilege 和可用的
目标无关静态信息，实际分支方向/目标属于允许的功能事实；不携带 gem5 命中
层级、延迟、BPred 误预测标签或 PMU 结果。消费阶段根据自己的 branch、cache、coherence、prefetch 与 store/load
生命周期产生实际模型事务，再由 observer 计数，两个账本共享事件字典而不共享
gem5 的分类实现。

gem5 的 oracle-only 请求账本直接观察 LSQ、fetch、Sequencer 和各层控制器的
实际请求生命周期。需要请求身份、fragment/attempt/transaction ID、请求类型、
发起核、资格判断时的 privilege/计数上下文、计数边界、本层状态与结果。不能
只依赖 committed DynInst 的 `TraceValid`：取指、预取、被 squash 的请求和无
退休指令对应的 writeback 也要覆盖。新 observer 与旧 native registry 分离，
旧 registry 保持独立机制诊断。

FastSim 原型计算的失败尝试和 rollback 不产生目标事件；已接受的模型时间线
上真实发出的 wrong-path 请求，即使其指令随后被 squash，仍按硬件事件资格
保留。两种“推测”不能混淆。接受的 observer 增量和跨 epoch 在途身份必须随
模型事务原子提交、回滚和复用，避免 prepare/replay/carry 重计。

请求记录至少保存 `origin_core`、`request_kind`、`logical_request_id`、
`fragment_id`、`attempt_id`、`transaction_id`、`parent_transaction_id`、
`cache_level`、`event_phase`、`event_context`、`lookup_facts`、`window_id`。
字段都来自本端自身事实；这些是 observer 事件，不是要求把所有事件写成大 FST。
正常运行在线聚合，逐请求日志只用于有大小上限的取证。

## 5. committed trace 的能力边界和可移植性

当前 FST 无法完整恢复错误路径的数据地址及实际硬件预取流。增加统计加法不能
补齐这些访问。完整请求域需要 FastSim 自身的预取模型和推测执行提供者：根据
自身分支预测和可用功能状态生成取指及数据请求；未知地址必须显式计入覆盖缺口。

提供者可使用 QEMU/DynamoRIO 采集的架构寄存器/内存状态、静态代码及地址映射
等目标无关事实，按需重建执行。必须先审计功能状态是否充分，包括错误路径
load 的值和地址依赖，不能假设仅有分支 PC 或已退休地址就足够。禁止把 gem5
实际发出的错误路径地址、miss 标签、计数或服务时间回灌成 FastSim 预测。
QEMU/DynamoRIO 不需要输出目标微架构的命中结果或预取结果。

实现按能力渐进接入：先保证事件 schema 和受控内核覆盖，再增加模型产生的
speculative data 与 prefetch 请求。缺失能力期间输出独立的 committed-demand
诊断值；对应完整硬件事件的状态不得升级为 available/validated。最终验收仍是
本文十项原事件，不以更窄的代理指标替换。

## 6. 范围和测量边界

硬件首次资格验证使用固定核、当前单线程、`:u`、不继承、无 multiplex 的
可重复微内核；分组统计并记录 `time_enabled/time_running`。扩展四核时明确
四个 TID、核集合、子线程继承政策、ROI、屏障和每核实际指令数，再验证
user+kernel。既有 `task-oncpu-switch-entry-v1` 结果不自动等同 perf 的上下文切换
计数边界。

退休事件按架构退休边界过滤；请求事件按它在本端的真实计数边沿和计数上下文
过滤。禁止将所有异步请求都无条件算给原始指令 owner，再称与任务 perf 等价。
先用无 task switch 的受控窗口验证；跨切换在途请求保留 origin 与事件上下文，
专项核对硬件行为后再认证。ROI 前发出、ROI 内完成以及 ROI 内发出、ROI 后完成
分别按事件边沿处理，drain 只负责解析在途状态，不能扩大已关闭的计数窗口。

gem5/FastSim 同源精度实验继续遵守共同终点合同。三端实机实验采用同一二进制
和明确的算法 ROI/工作单元；单独记录运行间调度和动态人口差异，不能把不同
执行路径的总数差全归因于模型。raw totals 和 per-1K macro 分别报告。

硬件身份与模型几何单独核对。本机 L1D=48KiB/12-way、L2=2MiB/16-way，LLC
每 socket=99840KiB/15-way；现有四核输入 L1D=32KiB/8-way、L2=1MiB/8-way、
LLC=64MiB/16-way。旧输入适合 gem5/FastSim 回归，不能用来直接声称 SPR 数值
精度。真实 LLC 切片、替换和预取机制存在未公开或未验证项，必须记录模型近似，
不能只改容量就称微架构完全相同。

## 7. 验证矩阵和计分门禁

| 层级 | 冻结输入及控制 | 三个重点 miss 的检查 | 其余字段与失败条件 |
|---|---|---|---|
| 硬件事件可用性 | 绑定 CPU/raw 编码，小于可用 counter 数的分组 | 三个 raw 事件能调度 | 只证明可用性，不认证语义或精度 |
| L1 单位/来源 | hot/cold load、split、CMPS、完整/部分 forwarding、填充 follower、RMW、软件预取 | 已知字面退休单位；miss/follower/mixed-source 分别核对 | 不强制来源互斥分区；未知 ISA 单列 |
| L2 类型/合并 | 依赖链分别构造 L1-hot、L2-hot、越过 L2，另加 RFO、代码、预取、重复同线请求 | 本层真 miss，不受后续 LLC/remote 来源改变 | REFERENCES 范围大于 demand；重试不能复制事务 |
| LLC 来源 | LLC-hot、超 LLC、干净/脏共享、目录与数据状态不同、prefetch 请求 | 分清 LLC 结果与最终内存/remote responder | core-origin 过滤；L3 prefetch 排除政策 |
| writeback | L1 脏逐出进入 L2、L2 脏/干净逐出、核间转移、背景写回 | 对三个 miss 无错误连带补计 | 先区分官方事件的方向与事务资格；原生 hook 必须产生可审计非零，未接线输出 null |
| 请求人口 | 可控错误路径、取指、PTE、软/硬件预取、squash、取消响应 | 原生实际请求与模型请求各自分类完整 | 缺少类型则对应事件完整覆盖不通过 |
| 窗口与回滚 | 预热跨界、ROI 两端在途、task switch、epoch retry/carry | 事件只在规定边沿计一次 | 在途解析、task/subset 守恒、无 oracle 输入 |
| 多核同源 | 两份已有四核源作回归，新源证明新请求 observer | 维护入口核对定义/人口/原生依据 | 旧源没有新 sidecar 时拒绝给新字段计分 |
| 三端实机 | 相同算法、二进制、ROI、范围及匹配配置，重复测量 | 分别报告 gem5↔真机、FastSim↔真机、FastSim↔gem5 | 不把两个模拟器互相接近当硬件认证 |

真实缓存层次定位依赖工作集扫描和依赖链控制；不能只靠 `clflush` 区分 L1、
L2、LLC miss，因为 flush 可能同时移除多个层次的副本。硬件缓存容量/共享
干扰和预取行为随实验记录，测量离散度与未通过项保留。

现有维护验证器新增独立的十字段模式及 sidecar 审计器，复用来源/身份/边界
门禁；不改变 native 模式的含义，不静默读取 legacy 同名 `perf_*`。
每个字段具有独立的 definition/reference/model/hardware-validation 状态与
未认证原因，不因一个字段可用就将整组升级。

报告按字段输出 MAPE、max APE、WAPE、signed bias、绝对计数误差、有效样本数、
0-reference 项和覆盖状态。缺失 oracle 不等于0；真实0/0与非零/0按维护合同
独立处理。三个重点字段必须单列最坏案例及首次分叉请求证据，不跨事件混均值。
涉及 CPI 时同时给 active CPI 绝对误差和 MAE，并单独保留 elapsed。

## 8. 实施顺序与资源约束

1. 固定十字段事件字典、逐字段能力状态和独立验证接口，迁移消费者；旧字段
   保留为 legacy，防止新报告读到错误命名空间。
2. 优先补齐 L1 退休来源事实、独立事件资格与真机微内核验证。
3. gem5 接入独立全请求 observer，优先 L2 demand 与 LLC core miss，补非退休
   请求的身份和范围。writeback 同步按真实事务接线，不再输出未观测的0。
4. FastSim 在两阶段框架的消费端接入同定义 observer，补本端各类请求模型；
   实现失败推演与目标 wrong-path 请求的不同提交规则。
5. 完成受控三端资格验证，再运行配置匹配的四核实验和吞吐回归。

每步独立验收，性能成本按功能开关分解测量。当前约 3.87M measured user UOP/s
来自先前退休修复的主输入安静主机测量，仅是设计时历史基线；本轮实际开销
见后续实施结果。不得用 report rescore 宣称新的性能数据。

不复制已有 TeaLeaf trace，不删除历史实验；全体实际 FST 总量不得超过
20,000,000,000 bytes。设计时为19,184,545,640 bytes，采集前重新检查剩余额度；
新四核探针每案上限300MB，observer 在线聚合、诊断日志单独设上限。编译 gem5
使用当前 affinity 核数的80%向下取整，GCC11和显式Python3.11；FastSim默认
`cmake --build build -- -j16` 及直接运行 `fastsim_tests`。

## 9. 本次已完成和未完成

已现场复核十项官方编码及推测属性，保存来源 SHA256、CPU/cache 身份；十项
raw 事件分三组成功打开，没有 `<not supported>` 或 `<not counted>`。测试负载
为 `/bin/true`，因此这些结果只用于事件可用性检查，不报告性能或准确度。
此前 L1 微内核和新四核原生诊断保留原状态，不因这次绑定升级认证。

后续已按[实施计划](../plans/2026-10-15-perf-cache-threeway-alignment.md)接入新的
gem5/FastSim observer、逐字段资格与维护验证器的 `--perf-cache` 模式，并启动
有界四核真实采集和固定核真机微内核验证。新 observer 不代表完整推测/预取
请求模型已经补齐；未通过资格和覆盖门禁的字段仍为 null，尚无十字段的新
三端 MAPE/max APE。实际证据和未完成接口见
[实施结果](../../perf-cache-threeway-implementation-results-20261015.md)及
[纯功能窗口接口](../../perf-cache-functional-source-interface-20261015.md)。
