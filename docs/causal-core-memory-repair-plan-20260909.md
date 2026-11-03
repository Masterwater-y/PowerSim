# 下一步修复方案：闭合 core 与 memory 的事件生命周期

日期：2026-09-09。状态：实施设计；本轮没有修改生产模型或默认配置，也没有新增 CPI 实验。

后续实施更新：A 阶段已以显式 `causal_read` 模式接入 Simulator，见
[第一阶段实现](causal-core-memory-phase1-20260909.md)。上句描述设计轮次；维护默认未
切换，B–E 阶段尚未完成，不能将受控只读场景通过外推为完整 TeaLeaf 修复。
B 中的混合指令/SQ/分支与真实前缀接入已有后续实现，见
[混合指令阶段](causal-core-memory-phase2-20260909.md)；其余边界仍按下文推进。

## 1. 首选方案与范围

下一项应当交付一个**由实际就绪事件驱动、从 Simulator 入口运行的 core/memory 求解路径**。
它统一决定 issue、请求准入、服务、数据返回、消费者唤醒和资源释放，替换当前先确定
基础时序/共享路径、再累计 response 延迟的求解边界。

首个实现用受控功能流验证完整事件链；达到真实负载所需语义覆盖后再运行 TeaLeaf 等
完整 ROI。load-only 原型是开发阶段，不作为混合真实负载的局部上线方案。不承诺第一版
覆盖全部微架构，也不把这项工作扩大成复制 gem5 的逐周期流水线。

这比“再接一个 pending-fill 等待”多做了一件关键工作：既允许消除旧调度制造的额外
等待，也保证新的 consumer 不会早于自己的数据返回。这里的提前是相对旧预测，新的
求解器自身始终按因果时间前进，不回写已经生效的过去。

## 2. 为什么先修这一边界

证据详见 [全局模型审查](global-cpi-model-review-20260908.md) 和
[TeaLeaf 中间状态审计](tealeaf-intermediate-state-audit-20260908.md)。

- TeaLeaf L1D64 C4 的完整 ROI 中，data DRAM reads 仅差 +0.0861%，CPI 却低估
  17.9910%。单纯修 miss 数量不足以解释它。
- 原参考二进制的密集配对窗口中，退休跨度差 5,115 cycles，其中 4,999 落在
  issued-load-head 等待类别。这是周期账面分解，不是修复可获得的因果收益。
- 另一诊断窗口的 follower 身份是两边相同 149、FastSim-only 191、native-only 272。
  净差 81 掩盖 463 条双向错分；issue 间隔与 parent 生命周期都有独立分歧。
- 通用微型输入已证实 memory event 与 UOP 的 issue 时基不一致、数据未回却成为
  本地命中、FU 未来预约挡住合法空档，以及 load response 没有驱动依赖分支恢复。

因此不能只延长 response，也不能只把 final issue 排序。ROB 阻塞是这些事件传播后的
结果；不引入 ROB stall 系数。DRAM 动态服务本身仍有缺口，但在到达流不闭合时一起
调服务参数，会再次混合两个问题。

## 3. 实施结构

### 3.1 核心状态：功能解码与时序求解分开

复用 trace 解码、功能寄存器 producer、指令属性及硬件配置；功能预读不推进任何目标
时间或 cache 状态。新 core 状态保存有界 ROB、IQ、LQ/SQ、依赖未就绪计数、ready 队列
和未来完成事件。

dispatch 时分配容量；只有已 dispatch、操作数就绪且满足内存顺序的 UOP 才能进入
issue 选择。FU/端口/issue width 按实际选择时刻占用，不能让未就绪指令抢先预约一个
未来槽位，再挡住更早已就绪的独立指令。仲裁依据冻结的目标策略，不能任意用堆的插入
顺序代替目标优先级。

普通 load 的完成由所有必要 fragment 的数据返回和明确的 writeback 阶段决定；随后
唤醒 producer 链的消费者。转发、跨行、重试等必须有显式身份和完成规则。分支解析使用
其真正操作数/执行完成事件，恢复前沿随之推进。预测算法与不可观测 wrong-path 占用
属于另一层模型精度，不能借此声称已经与 gem5 全等。

内存顺序也不是删除全局 clamp 后任意乱序：store 地址/数据就绪分别跟踪；年轻 load
经过 LSQ 的地址冲突、转发和排序条件；未决地址按明确策略等待，或经已实现的推测/
重放机制处理。不得利用功能 trace 预先知道的年轻地址，提前修改时序可见状态。

### 3.2 统一调度：先生成可达事件，再推进共享时间

第一版采用串行、确定性的目标事件调度，跳过无变化周期，保留宿主侧静态解码并行。
这是易审计的正确性起点，其吞吐成本必须实测。后续跨核时序并行需要独立性或保守时间
边界证明；不预报吞吐提升，也不默认接受长期丢失现有并行收益。

事件队列同时包含每核 fetch/dispatch/issue 的下一次推进、资源释放、共享服务选择和
callback。每次处理全局最早的可达事件；新产生的事件只能处于当前或未来时刻，同 tick
的阶段顺序由目标合同定义并固定，不能仅依赖 core ID 打破所有平局。

这解决现有 coordinator 的关键接入障碍：它拒绝 `ready_cycle < now`，所以调用者不能
先推进一整批，再从反馈发现本该更早到达的请求。每核尚未生成请求的前沿也必须参与
全局推进；无法证明没有更早事件时不能提交未来共享状态。不能把旧 base issue 当成
这个证明。外部 trace 解码暂未供货只等待宿主，不增加目标停顿周期。

Q=1024 保持为宿主批处理设置。ROB、依赖、pending generation、callback、服务队列和
未释放资源跨批保留；切批不强制 drain、不重置时钟或 refresh 相位。活动状态随硬件
容量和有限事件数有界，而不是随完整 ROI 指令数增长。

### 3.3 共享状态：不同阶段各自生效

| 阶段                   | 可修改的状态                         | 必须避免的问题                              |
| -------------------- | ------------------------------ | ------------------------------------ |
| proposal / prepare   | 只读查询和版本依赖                      | 尚未准入就改 directory、替换状态或预约服务           |
| admission            | 对应队列/协议容量、请求身份、可合并关系           | 失败重试重复计数或分配；所有本地 hit 被一个 miss 表容量挡住  |
| lookup / 协议转换        | 当时可见的 tag、替换 touch、权限/失效状态     | 按未来命中结果提前产生 resident data；权限变更没有生效事件 |
| service selection    | 当前已到达队列、bank/rank/bus 日历、已承诺命令 | 看见尚未到达请求并反转已选择服务；follower 重复服务       |
| fill / data callback | 数据可见性、关联请求完成、对应服务资源释放          | 将全部状态推迟到 callback；重复 fill 或响应丢失      |
| writeback / retire   | producer-ready、有序退休及各自资源释放     | 把全部队列一律在 response 或 retire 时释放       |

复用 cache `prepare_lookup` / guarded commit / `probe` / `complete_fill`，并把
`SharedSystem::access` 的 directory、CHA、LLC、DRAM 副作用拆入上述阶段。已有
transaction/restore 用于失败恢复和差分；它不自动提供正确的阶段语义。若使用 proposal
缓存，必须验证所有读取的 line/set/directory/resource generation，失效后重新查询。

第一版串行调度可以先采用即时 prepare/commit，减少跨事件推测与大快照；不能先调用
旧 `access` 改完状态，再插入 coordinator。回滚如被使用，需覆盖协调器、core、私有/
共享层次、服务队列、外部计数器和待发布事件，不能仅恢复内部 ledger。

请求键应包含 core、稳定源 UOP/fragment 身份及请求类型；generation 还需标明所属层级/
合并域。同一个物理 line 不等于跨核共用一个 Sequencer generation。普通 load 的数据
可见性、权限和 generation 销毁条件由该层协议决定。

现有 `LineGenerationCoordinator` 是 core-local、read-only，要求 final callback 等于
read-visible，并在 probing 前应用 active capacity。这些是原型边界，不能未经目标容量
语义审计直接继承到完整 cache hit、store 或多核 coherence 路径。

### 3.4 store 与资源释放必须是首轮真实 ROI 的前置条件

普通 store 的执行完成、commit、发送、协议 callback、SQ 回收是不同事件。它可以在
内存 callback 前 commit，但 SQ 不能因此立即释放；TSO 发送限制必须独立遵守。

当前本地 gem5 `lsq_unit.cc` 的 `commitLoads` 在提交时移除 LQ 项；`commitStores`
仅将 store 标成可 writeback；`writebackStores` 受 `needsTSO/storeInFlight` 限制，
`completeStore` 再标记完成并从队首回收、清除在飞标记。实现前须将本地源码与冻结参考
的 provenance 一并核对，并为 IQ、端口、MSHR/TBE 等分别确认释放点。不能用一条
“response 释放所有资源”的规则覆盖它们。

取指、页表访问、writeback/eviction、store/coherence 即使不是本次目标负载的主要
瓶颈，只要会改变同一 set、line 或 DRAM 资源，也必须通过同一事件域。可以保留已声明
的服务近似，不能让它们在旧 replay 中提前修改新路径正在使用的状态。

## 4. 代码落点与分批交付

以下名称是拟实施设计，不表示文件或配置已经存在。沿用入口可以保留旧路径做对照，
新模式必须在第一次语义副作用前选择，单次运行只有一个时序 owner。

| 交付          | 具体改动                                                                                                                | 进入下一步的条件                                                                       |
| ----------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| A：可运行的最小闭环  | 新事件 core/memory driver 接入 Simulator；复用功能解码；受控普通 ALU/load 流经 live ready→issue→admit→service→callback→consumer→retire | 通用反例和独立控制通过；确认实际执行新 driver，旧 schedule/shared replay/response feedback 在此范围没有调用 |
| B：真实负载所需闭环  | 增加普通 store/转发/TSO/SQ、分支恢复、I-side/现有翻译近似、共享失效和跨批连续状态；完善 SharedSystem 分阶段接口                                           | 支持能力检查覆盖整个实验输入；队列和可见性合同通过；小型混合、多核场景通过                                          |
| C：成对真实窗口    | 固定服务参数与基线输入，从连续预热/前缀运行到诊断窗，比较完整事件链；保持旧正负误差控制                                                                        | 能解释主要身份变化和首个分歧；无内部因果违例；分别报告剩余服务差及边界未知状态                                        |
| D：DRAM 独立机制 | 在上述到达合同稳定后，替换控制器的队列/选择/命令/rank-refresh 状态                                                                           | 同到达差分先通过，再用 FastSim 自己产生的到达流验证消费者和完整 ROI                                       |
| E：推广评估      | 冻结版本与参数，独立 workload/window、旧两矩阵、参数趋势和端到端吞吐                                                                          | 满足既定精度/趋势/成本门禁；通过前保持实验模式                                                       |

A 对应 `IntervalCoreModel::schedule` / producer 生成旧 bound 的边界及
`compute_core_timing_feedback_impl` 的双时基增量求解；B 对应 `SharedSystem::access`
和 `replay_shared` 的状态所有权。模块可以提取到单独源文件，但验收对象是入口到退休
的实际调用链，不能仅交付新的未接入 header 或离线 shadow。

A 可以使用显式配置的简化前端/翻译来隔离机制；该配置的通过不构成维护 native-FS
profile 的通过。B 未覆盖的 atomic、MMIO、特殊序列等必须在运行前判定不支持，或补齐
其机制；运行中发现遗漏则明确失败。第一版不做单请求或任意 batch 的新旧路径混用，
也不将部分请求执行新模型后的结果报告为完整候选 CPI。

这是对先前“只在 clean load 组件接入”的收紧。真实干扰闭包可能经同 set 和共享 DRAM
覆盖很大范围；没有完整的输入/活动状态证明，不假设存在高覆盖率隔离岛。后续如引入
组件级混合，必须另行证明整个时间与资源闭包，并在副作用前完成选择。

## 5. 第一批验收：验证机制，而非拟合 case

### 5.1 通用差分场景

| 场景                               | 必须验证的结果                                                     |
| -------------------------------- | ----------------------------------------------------------- |
| 长 load→consumer，外加独立 ALU/load    | consumer 等自己的数据；独立指令可以先发射，受真实宽度/端口约束                        |
| 同线重叠与先后访问                        | overlap 的 leader/follower 身份、唯一服务；数据返回前后分别按正确状态处理           |
| 同 tick callback/admission，容量满后重试 | 确定的阶段顺序、容量无超发、无双重服务/释放；等待区间有唯一 owner                        |
| 不同 line 同 set 的替换/失效             | lookup 与 fill 的可见性正确；stale proposal 不能提交旧路径                 |
| response 合法提前及延后                 | 两个方向都重新决定后继 issue、请求交错、路径和 retire；无旧 bound 永久挡住提前           |
| store→load 转发、独立 load、多个普通 store | 地址/数据/顺序条件生效；commit、send、callback、SQ release 分离；目标 TSO 合同成立 |
| load→误预测分支→后继 load               | 恢复受真实 branch resolution 控制；无依赖和预测正确的对照保留各自语义                |
| 跨核争用、跨宿主批、连续预热→ROI               | 不遗漏更早可达事件；在飞状态连续；仅改变宿主切分不改变目标事件结果                           |

测试的地址、依赖结构和硬件容量按机制构造，可遍历合法组合；没有 TeaLeaf PC、误差
符号或 gem5 response 查表。需要实际 gem5 差分的场景使用冻结目标配置；仅有内部
不变量的测试如实标为内部合同，不声称逐 tick 等价。

### 5.2 观测合同同时补齐

事件账本记录 source identity、ready/issue/admission/service/data/writeback/retire、
generation/leader、容量获取和释放、retry 原因。测量边界和累计状态独立：跨 checkpoint
保留上一个 retirement 前沿，ROI reset 不丢跨批间隔。

ROB-full 的周期数、进入 full 的次数和被阻止的 UOP 数分别统计，禁止将重叠的每 UOP
位移和当成 elapsed cycles。LQ/SQ/IQ/MSHR 同时报告占用时间分布和释放事件。
零退休周期类别保持互斥、总量守恒，并单列边界/idle/无法观测人口。

真实窗口同时报告 follower 身份混淆矩阵、issue-spacing、admission→response、
路径、队列占用及最后传播到 retire 的差异；不能只比较平均 latency、净 follower 数
或 ROBFull 原生计数。原参考完整 ROI 与另一诊断二进制分开报告。

### 5.3 独立验证与停止条件

旧 formal40/DSE54 和已研究的 TeaLeaf、ASTCENC、Stockfish 窗口都是开发/回归集合。
在选型前以 workload、输入、窗口、预热、reference SHA、trace SHA、配置和分母冻结
新的验证 manifest；本轮尚未选取或冻结未使用数据。窗口不能按新候选误差挑选，仅换
核心数也不构成 workload held-out。

旧 offline clean 集合使用 native follower 结果过滤过分歧，不能将筛后 100% 匹配作为
验收。资格检查只消费部署输入和模型状态，先冻结，再对全部支持事件评分；记录所有
拒绝、遗漏与覆盖率。若独立集合用于继续修改模型，它就转为开发集合。

任何因果/守恒失败立即修复，不能通过调 latency 消掉。在内部合同通过但正误差控制
恶化时，定位新轨迹首个分歧；不自动称其为“原有补偿被打破”，不直接启动全矩阵。
语义成立与整套模型可推广分开判定；没有完整结果前不承诺收回 17.99% 尾差。

## 6. DRAM 的后续边界与宿主成本

第一轮固定服务模型参数，是为了识别事件接线的作用，不表示现有 DRAM 已准确。
D 再处理当前已到达队列的 FR-FCFS、被 topology heuristic bypass 的激活路径、读队列
与在途响应的共同容量、实际命令约束和 rank refresh 的 drain/PRE/关闭行/恢复。
refresh 相位来自模型启动和连续预热状态；不使用 gem5 实测时刻补回某次长尾。

单组件可以离线输入相同到达流做条件差分，明确区分诊断与推理。最终到达必须由新 core/
shared 事件链自己产生。不能拿相同到达实验的通过替代全系统证明。

成本设计采用紧凑活动状态、跳过空周期、只唤醒受影响消费者、完成即回收事件，并替换
旧求解工作。若每 UOP 堆操作或共享串行部分成为主要成本，再凭 profile 压缩事件和
证明可并行的资源域；不以漏事件、只算 eligible 人口或放大 Q 获得吞吐结果。

E 的测量沿用固定 NUMA、同二进制、串行交错重复的端到端 wall time 门禁，包含新模式
所需的解码、资格检查、状态维护和输出成本。精度分别报告两个矩阵的正确分母、
MAPE/P99/max、正负尾部、参数方向/幅度/排序，不只展示 TeaLeaf 单例。

本轮核对了相关源码和已有证据，只新增实施方案及决策索引；没有运行构建、测试或
新模拟，因此没有新增模型正确性、CPI 或吞吐通过结论。
