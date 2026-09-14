# FastSim 成对事件账本与建模优化方案

日期：2026-09-08。本文接续 [当前代码审查](current-code-model-audit-20260907.md) 与
[优化决策](optimization-decisions.md)，以 gem5 FS 为唯一精度 baseline。

## 1. 结论与本轮边界

下一步应优化**事件的因果关系与状态归属**，不是继续寻找一个统一的 latency 修正常数。
“错误相互补偿”有代码和实验依据，但不能把任意 CPI 回归都解释成补偿被打破。
必须区分真实服务变化、错误约束的转移、窗口外继承状态，以及仅仅换了计时基准。

本轮完成代码复核、两个完整 ROI 的 baseline/FU-candidate 诊断重放、两个连续
10,001-record 窗口、正误差窗口与既有 gem5 补采的关联，以及固定服务的退休传播
小验证。**没有修改 C++ 模型、默认配置或开启新的修复组合；没有新 P99 或吞吐收益。**
FU、pending-fill、branch recovery、DRAM 候选的停止决定继续有效。

**后续实施更新：** 第 2.6 节已经加入显式 opt-in 的边界状态 C++ 路径、采集/净化
工具和完整 ROI 消融；第 2.7 节记录用户要求的并行跨负载 pilot。维护默认仍未改变，
但“本轮没有修改 C++”只描述最初账本阶段。该实现修复目标请求后没有通过 CPI 门禁，
跨负载结果也没有形成一致收益。

得到四项新的约束：

1. FU 的局部“收益”可能只是 load completion 更早，自己的 memory response 根本未变，
   甚至消费者和该 load 的 retire 都早于 response。不能把它当作合法收益。
2. FU 候选第一次使所选正控制窗口的 retire 落后时，该请求的服务时间反而缩短了。
   归因必须继续向 dispatch/上游状态追溯，不能先定性为该请求共享排队变差。
3. 正误差窗口中，已经找到 FastSim 走长 DRAM 路径、gem5 同一 load 很快 commit 的
   具体位置。它是核对 cache/转发/预热状态的入口，不是立即认定某一级 cache 出错。
4. load 自身 completion/response 的必要关系确实存在错误；但两个窗口的固定服务回放
   表明，错误周期之和远大于传播到窗口末端的位移。当前证据不支持优先堆叠 load wait。

## 2. 新的成对证据

### 2.1 输入、实验与关联口径

沿用冻结的 TeaLeaf LLC32 C4（signed error +9.6487%）和 L1D64 C4（−17.9910%），
每核完整 10M user UOP，native user+kernel 输入，Q=1024。这里的 CPI 是
`cycles_per_user_uop`，不是 macro-instruction CPI。

同一个二进制分别重放 FU 关闭态和原已停止候选；只为取证关闭 materialized fast kernel、
开启 attribution、在指定 core/sequence 上逐 UOP 输出。四个重放的 `scope_metrics`
（排除宿主 throughput）和 `threads` 均与上一轮相应非诊断输出逐项相同。

| 窗口 | 来源序号（含预热的 record ordinal） | 唯一配对 memory events | path 变化 | latency 变化 | baseline / FU 候选退休跨度 |
|---|---|---:|---:|---:|---:|
| LLC32 core 2 | 222884–232884 | 2,318 | 0 | 37 | 14,515 / 14,415 cycles |
| L1D64 core 1 | 2509999–2519999 | 1,836 | 0 | 11 | 4,944 / 5,065 cycles |

请求配对使用 core、源 record ordinal、line、读写/I-side/retirement 属性；本次没有
重复歧义键。**不使用 chunk 内的 `event.ordinal` 作为跨运行主键**，也不使用 PC 单独配对。
所有配对 UOP 的 PC、producer distances、load/store 属性相同。

LLC32 的完整 ROI 总 core cycles 仍为 32,576,208 → 32,747,266（+171,058），
L1D64 为 20,137,515 → 20,139,074（+1,559）。因此所选窗口的 −100/+121 cycles
也不能直接代表整段 ROI 的方向或贡献。

两组运行的 warmup barrier 分别为 LLC32 547,018/548,209，L1D64 353,502/351,510。
以下 UOP 时刻减去各自**原 ROI barrier**，不在每个窗口重新平移。预热结果有差异，
且窗口开始时一些队列状态已不同，所以“首个观测分歧”不等于全流的根因。

### 2.2 FU 提前可能放大已有的虚假 completion 提前

LLC32 core 2，load `sequence=224878`，PC `0x40983e`，line `5021064`：

| 事件（相对各自 ROI 起点） | baseline | FU 候选 |
|---|---:|---:|
| dispatch | 3706 | 3706 |
| UOP issue | 3714 | 3713 |
| memory event corrected issue | 3813 | 3813 |
| memory response | 4060 | 4060 |
| load completion / retire | 3961 / 3961 | 3960 / 3960 |
| 直接寄存器消费者 `224886` issue | 3961 | 3960 |

这条 load 的事件服务时间在两侧都是 247 cycles。FU 回填后，memory event 仍被
程序序 envelope 留在 3813，但 UOP 的 completion 和消费者沿另一时基提前了 1 cycle。
关闭态已早于自己的 response 99 cycles，候选变成 100 cycles。

所以这里的 −1 cycle 不是合法的 memory 加速；它揭示 producer 调度和 response
增量回填的合同不一致。代码入口是 `src/simulator.cpp` 的 memory event 构造
（约 7624）、`memory_producer_issue_q16`（3726）、`event_completion_extra`
（约 11909）与 `actual_completion`（约 11990）。UOP 和 event 的基础 issue 不同，
后者的 exposed latency 却按增量加回前者的 completion。

### 2.3 首次 retire 落后的请求，不是服务变慢

LLC32 core 2 的连续观测中：

- 首个 ROI-relative issue 变化：`223287`，254 → 253；此时 retire 都是 260。
- 首个观测到的 event latency 变化：`225146`，256 → 257；该 load retire 反而
  4259 → 4253，因为 issue 已从 4003 提前到 3996。
- 首个候选 retire 晚于 baseline：load `228842`，9645 → 9784，增加 139 cycles。
  它的 dispatch/issue 分别从 9473/9479 变成 9617/9623，均晚 144 cycles；
  event 服务时间却从 166 降至 161，即 **+144 − 5 = +139**。
- 前一条 `228841` 的 retire 是 9645 → 9627，候选还早 18 cycles；`228842`
  才把上游 issue 的延迟暴露到有序 retire。

两侧此处的 checkpoint 已分别变成 `[228744,232425]` 和 `[228793,231765]`。
这证明固定 Q 不等于固定批内事件集合；边界变化本身也不证明模型有错。
下一步要反查为何进入该位置时 dispatch 晚 144 cycles，以及前序反馈、资源和跨核
事件如何传到此处。**本轮尚未解释完整 ROI 的 +171,058 cycles，不能把上述分解
写成已找到其根因，也不能排除更早共享事件对 dispatch 的影响。**

### 2.4 正误差的首个可证里程碑：先查路径和状态

新密集关联覆盖 LLC32 core 2 的上述 10,001 records，核对 FST/原 gem5 JSON 的
PC、flags、物理地址、size、CPL、ASID、core/thread，再用真实 `micro_seq` 关联 stage。
gem5 tick period=333。全 ROI idle budget=2,789 cycles；由于没有逐段 idle 时间戳，
累计 active-time 差值只报告上下界，不伪造精确值。

首个 `FastSim retire > gem5 elapsed retire` 的位置是：

- 源 `record ordinal=223952`，gem5 `micro_seq=223953`，`inst_seq_num=318799`；
  kernel load，PC `0xffffffff81b3a2d1`，line `49497704`。
- FastSim ROI-relative fetch/dispatch/issue/response/retire：
  **579 / 1005 / 1042 / 1502 / 1502**，path=5（DRAM），服务时间 460 cycles。
- gem5 同一 UOP fetch→issue=22 cycles，issue→commit=5 cycles；commit 相对 ROI
  为 1305 cycles。即使把 gem5 累计 idle 视为 0，FastSim 也至少晚 197 cycles。
- FastSim canonical DRAM arrival/command 为 547650/547949，排队 299 cycles；
  command blocker 指向 core 2 `223847`，root 指向 core 1 `272713`。
  corrected event issue 比生成该服务的 `shared_stage_issue_cycle` 晚 445 cycles。

这是一个**路径/服务尺度明显不一致且暴露到 retire 的调查入口**。后续有界 Ruby debug
已经闭合该入口：同一请求实际发出 `ReadReq`，L0D 为 `E->E`，Sequencer 1 cycle
完成；不是 forwarding、coalescing、prefetch 或 wrong path。更早的同 PC committed
kernel load 经过 DRAM 将该行装入 core 2 私有 cache，但它位于全局 WORKBEGIN 与
core 2 第一条 measurement record 之间的 trace 空洞，现有 FST 没有记录。详见
[测量边界内存状态第一阶段](measurement-boundary-memory-state-phase1-20260908.md)。

对照负尾部：既有精确关联的 20 条 load 中，gem5 issue→admission 均为 1 cycle，
非 refresh 请求 admission→response 为 243–358（中位数 315），FastSim 为 161；
最长请求与 rank refresh 重合。见 [尾部报告](tealeaf-tail-timing-20260907.md)。
两侧需要修复的阶段可能不同，不能用一个正/负 CPI 常数相消。

新密集关联还发现两条已知 MMIO escape：`223463` 的 store 与 `223721` 的 load，
其物理地址均在配置 DRAM 容量之外。工具现将其标为 `mmio-escape-no-data-event`，
保留 pipeline 配对但不虚构数据层级 response；普通 DRAM 范围内缺事件仍报错。
这是审计覆盖修正，未为 MMIO 实现新的时序模型。

### 2.5 小验证：只修 completion 下界能传播多远

新增只读分析工具 `tools/analyze_response_retire_witness.py`，执行受限回放：
保持观测 issue/retire 下界和 issue→completion 时长；把 load completion 抬到**原有**
response；只传播四条功能寄存器依赖及有序、宽度受限的 commit。两窗零改动回放均与
原 issue/completion/retire 逐项相同，才允许解释干预结果。

| baseline 窗口 | completion 早于 response 的 load UOP | 局部差之和（不可作 CPI） | 最大局部差 | 条件回放最大 retire 位移 | 条件回放末端位移 |
|---|---:|---:|---:|---:|---:|
| LLC32 core 2 | 372 | 5,011 | 120 | 99 | 1 |
| L1D64 core 1 | 166 | 1,162 | 7 | 2 | 0 |

其中实际 retire 也早于自己 response 的 load events 分别为 68、4。
负尾部窗口已知 gem5/FastSim span 缺口为 5,115 cycles，不能拿 1,162 个重叠局部周期
解释它。另一方面，末端 0 不代表修复全局无影响：这里没有重算 FU/IQ/LSQ/StoreSet、
frontend、cache/admission/DRAM 和 checkpoint，窗口外消费者也没有传播；两窗分别有
43、2,842 条寄存器入边来自窗口外。**这些结果不是完整修复的收益预测或上下界。**

微型控制包括依赖链确实暴露延迟、老长请求掩盖年轻 load 延迟、两个并行差不能相加、
拒绝零改动不等价与稀疏窗口。该工具只用 FastSim 自身 response，gem5 timing 不进入推理。

### 2.6 第一版边界状态修复：目标路径正确，完整 CPI 仍退化

新增显式 `fastsim-binary-warmup-state-slice` 和功能状态 sidecar，只接受已提交访问的
物理地址、大小、读写类型与连续顺序；tick、path、latency、MESI 和 coherence oracle
不能进入运行输入。TaoTrace committed `mem_events` 经离线净化后，core 2 边界空洞包含
693 次可缓存 DRAM 访问并排除 6 次 MMIO，其他三核为 0。

完整回放把 `223952` 从 460-cycle DRAM 修为 FastSim 自身判定的 2-cycle L1，response/
retire 分别提前 458/456 cycles；10,001-record 窗口末端提前 1,629 cycles。但完整 ROI
cycles/user-UOP 从 0.8144051186 变为 0.8158025184，signed error 从 +9.6487% 变为
**+9.8368%（退化 0.1881 pp）**。L1D/L2/LLC miss 与 DRAM read 各减少 9 次，sum
core cycles 却增加 55,896。这是新的直接证据：修掉一条虚假 DRAM 路径会改变后续
shared order/DRAM calendar，并破坏当前模型中的补偿误差。

因此第一版只保留为默认不使用的 opt-in 机制，不扩矩阵。它解决了入口状态缺失，尚未
原子替换 memory ordering、admission、visibility 和 response，也未解释新增周期的首个
共享分歧。

### 2.7 有限跨负载 pilot：miss 减少，精度方向不一致

按用户要求并行补采并成对重放 ASTCENC baseline C4、Stockfish baseline C4 和 TeaLeaf
L1D64 C4。三个有效 case 的绝对误差变化分别为 −0.0196、+0.1467、−0.0224 pp，只有
Stockfish 改善；三组 L1D/L2/LLC/DRAM miss 都下降。加上第 2.6 节 LLC32 后，四 case
MAPE 从 12.2119% 变为 12.2327%，退化 0.0209 pp。顺序重复的总周期、逐核周期和 PMU
逐项一致，方向差异不是并行宿主噪声。

formal Graph500 没有进入精度统计：冻结 gem5 SHA 已不可用，当前二进制又在 WORKBEGIN
前输出 32.36 GB prefix 后被终止。该 case 保留失败 provenance，不允许绕过哈希门禁后
把不可比较结果加入平均值。完整数据和决定见
[跨负载试验报告](measurement-boundary-memory-state-multiload-20260908.md)。

## 3. 统一账本应记录什么

逻辑链仍是：dispatch → producer issue → Ruby admission → service → response
availability → dependent consumer → ROB-head retire。但它不是每条指令一条线性的
“耗时分项”：多个请求合并、多个 producer 汇合、store 可先 retire 后排空，需保留边。

### 3.1 三条时序轨道，不能压成一个 `issue`

1. **proposal/core-base**：基础调度、未加反馈的 UOP issue、程序序 event envelope；
2. **canonical/shared**：在哪个时刻/状态上作出的 cache/协议/DRAM 服务选择；
3. **realized/core-feedback**：最终 dispatch、数据 ready、consumer issue、retire。

每个 timestamp 显式携带单位、clock domain、origin、validity 和 provenance。
当前 `memory_events.base_issue_cycle` 不含 `interval_gap_cycles`，UOP 的
`base_issue_cycle` 输出已含它；`canonical_*` 是 reference-clock 服务时刻。
不能把所有名字带 `_cycle` 的字段统一减一个 origin。多频率时必须经过现有 reference/
local clock 映射。本轮两 case 同频，也仍分别保留 raw 和 ROI-relative 数据。

对 gem5 同样区分：当前 label 的 `fetch_tick` 绝对，`issue_tick` 是相对 fetch 的
偏移；load `completeTick` 不是数据返回。旧 DRAM witness 的 `gem5_issue_tick`
实际上来自成功发送/准入，导入新账本时应明确重命名为 admission，不能直接沿用标签。

### 3.2 每个阶段的最小信息与现有缺口

| 阶段 | 必须保留的证据 | 当前可用与缺口 |
|---|---|---|
| dispatch / producer issue | opcode/FU、操作数 ready、容量阻塞者与 release、issue reservation | 有 base/actual stage 和部分 predecessor；缺所有 FU/DTLB/预测依赖的胜出边及来源 |
| Ruby admission | attempted/accepted/retry、成功占槽、合并 parent、失败理由 | corrected issue 不能普遍当准入；当前普通路径未独立输出成功 admission |
| cache / DRAM service | tag/permission generation、状态转换时刻、fill parent、arrival/command、blocker | 有 path 与 canonical DRAM 字段；缺完整 cache 状态生命周期和跨状态的可靠关联 |
| response availability | callback、CPU data-ready、split fragments、wake/WB 边 | 有 shared response 与 corrected response；两者不保证基于同一条已重求服务 |
| dependent consumer | RAW / address / store-data / predicted-memory / branch 边类型及 source ID | 有四个功能距离与第五条 StoreSet 距离；不能将预测边当已证实地址别名 |
| ROB-head retire | head-enter、eligible、实际 retire、胜出阻塞者、同周期竞争 | 有 actual retire 与部分 predecessor/cause；cause 分类本身不是完整因果链 |

事件主键建议为 `(input fingerprint, core, thread, record ordinal, event kind,
within-uop occurrence)`；另存 micro_seq、PC/micro-PC、CPL、ASID、地址/字节区间，
作为跨端身份验证。split request 有多个 fragment，coalesced demand 关联同一 parent
transaction，retry 是同一请求的 attempt，不能按同 line 就当同一次 transaction。
MMIO、辅助 syscall fact、无对应 label、窗口外 predecessor、歧义匹配都显式标记；
缺失时刻使用 null/status，而不是 0-cycle delay。

## 4. 如何计算暴露到 retire 的周期

对任一节点保留所有必要入边和胜出 blocker，而不仅记录最后一个 cause 枚举。
固定事件顺序下可以用 max-plus 关系表达 `t(v)=max(t(u)+edge_delay(u,v))`，例如：

- consumer ready 是相关 producer 的 **data-ready** 最大值，不是其 memory envelope；
- retire 至少等待本 UOP 完成、老 UOP 退休和 commit width；普通 store 的 architectural
  retire 与 SQ drain/全局可见不是同一事件；
- latency、资源占用区间与 exposed-retire delay 是三个量。队列应记录 `[admission,
  release)` 和阻塞 owner，不按整段 response latency 对所有消费者加罚。

每个候选分三个层次报告：

1. **必要关系见证**：精确指出原边是否缺失/虚假；局部差不得叫 CPI benefit。
2. **受限反事实**：零改动严格同一；替换指定 owner 的边、重算后继，报告窗口入口/出口
   frontier 和未覆盖边。固定服务回放只能筛选，不能证明全模型收益。
3. **实际候选差分**：在同一输入/ROI 上重求被影响的 admission、状态选择与服务，量
   `R_candidate − R_baseline`。同时报告 signed error 和 absolute error，以及各 core；
   不把窗口贡献、请求周期或单核末端随意相加成正式 CPI。

为识别补偿，应分别定位 baseline 对 gem5 的首个可证正/负分歧，再定位候选相对 baseline
的首个状态分歧。跨 workload/config 只比较机制，不直接按同 sequence 对齐不同 trace。
若 A/B 都有可解释的独立语义原型，可在有界、共同入口状态上比较
`R_AB−R_A−R_B+R_0` 判断非加性交互；不能把任意旧补偿开关一起开启冒充原子修复。
若顺序/路径改变，必须重求受影响子图，不能把旧下界永久固定后还宣称允许加速。

## 5. 建模替换方案：状态在所属阶段生效

### 5.1 优先级 0：补齐正误差路径分歧与入口状态

下一笔取证锁定 LLC32 core 2 的 `223952` 及其 DRAM blocker/root，不重跑完整矩阵：
取原 gem5 成功 admission、callback、实际 hit/forward/coalescing 来源和相关 line 历史；
FastSim 对应记录 tag/permission/fill owner、最后改变者、请求生成与队列阻塞者。
窗口向前扩到可以说明初始状态的地方，必要时包含预热和其他 core。

同时对负尾部既有 20 条 DRAM witness 用相同字段表达。这样才能判断应当删掉的是错误
miss、虚假队列等待还是错误入口状态，以及应当补上的是服务排队还是 refresh。
当前仍缺正侧精确 gem5 admission/response/path，未满足“已定位根因”的条件。

### 5.2 第一类原子替换：内存请求生命周期

目标不是 `completion=max(old_completion,response)` 这一末端补丁，而是让同一请求的
issue、admission、service、data-ready、资源 release 来自一条自洽生命周期：

- **producer 侧移除通用程序序 memory envelope 的所有权。** 顺序约束由真实支持的
  memory-dependence/LSQ/TSO 规则表达，不能直接无条件允许全部 load 越过所有 store。
  operand/address-ready 与 FU/AGU issue 是独立边；地址在 functional trace 中可见，
  不代表模型可以在 store AGU 完成前当作已知。
- **admission 侧决定是否占用、重试或合并。** 容量只按已准入请求的有效区间计算；
  未来预约保留空档，不能让将来的 response 把更早请求的槽位挤掉。store commit、
  send、callback、SQ release 分开。禁止另保留旧 envelope 再加一遍 admission wait。
- **cache 侧管理 pending/readable/permission 与 generation。** tag reservation 可先存在，
  数据/权限只在相应事件后可见；合并者等 parent，replacement/invalidation 使旧 generation
  失效。不能继续立即修改 valid/tag，然后只在 response 表里掩盖提前可见。
- **response 侧直接输出绝对 data-ready。** 统一用当前请求的服务结果唤醒消费者并释放
  相关容量；删除旧的跨时基 `exposed_cycles` 增量回填职责。若 arrival 改变会影响服务或
  路径，必须由 owner 重求，不能平移旧 DRAM response 冒充闭环。

上述是一份新的生命周期合同，不是同时打开目前已停止的三个独立补偿选项。
先用小组件原型测试 pending hit、真实/假别名、未来空槽、split/merge、store drain、
跨 checkpoint 与跨 core 干扰；证明能同时删除旧职责，再考虑接入主路径。

### 5.3 其他 owner 按证据接入，不抢先叠加

- **StoreSet/LSQ**：将 address-ready、store-data-ready、预测依赖、真实重叠/forward/replay
  分开。若采用有限 SSIT/LFST 风格状态，应有容量、clear/替换与最近未完成 store 的
  生命周期；不能只把 same-PC 永久集合换一个 penalty。功能 trace 不含完整错路/
  violation 事件时，训练和 replay 必须标注近似并独立验证，不能用 gem5 在线标签补全。
- **DTLB**：fill 只能在完成事件生效。`translate` 当前非单调查询下，向未来查询所做的
  全局填充不能污染随后更早时刻的查询；可由有序事件提交或可查询 generation 解决。
  修改属于 walker/TLB，不属于最终 load 的统一延迟。
- **FU**：gap-aware capacity 状态属于 issue owner。但先消除 §2.2 的虚假反馈收益，
  并解释 §2.3 上游传播；不把 FU 空档机会直接转成收益，不重开既有全矩阵。
- **branch**：正确路径边源于已解析 branch 的有效 completion；补齐必要边时，不再
  对同一次恢复重复收取旧 redirect penalty。其真实收益必须在统一 producer/response
  合同后重验，不能只增加 fetch 下界。
- **DRAM**：以真正 controller arrival 驱动有界命令/row/rank 状态；refresh 是 rank
  生命周期事件，不是给所有 load 加 tRFC。正侧先核对是否本应发这笔 DRAM 请求，
  负侧再核对已匹配请求的 queue/service。仍需检查 FR-FCFS 实际 activation。

入口对应 `src/interval_core.cpp` 的 `allocate_issue`、`translate`/
`retire_page_walks_through`、same-PC dependency 构造，`src/cache.cpp:159`
的 `access_indexed`，以及 `src/simulator.cpp` 的 event producer、
`compute_core_timing_feedback_impl`、共享 replay 与 DRAM repair。行号会随实现变化，
函数名和状态职责优先于固定行号。

## 6. 吞吐设计：离线账本丰富，在线状态有界

不建议在当前 producer/shared/feedback 之后再加第三遍完整逐 UOP closure。
既有 profile 中这三部分合计约占 64%–65% sampled CPU cycles；这是热点证据，
不是可直接移除的 wall time。新设计必须明确替代什么工作：

1. **诊断模式**按 core/sequence 输出完整事件、入边、状态来源；生产关闭，不能让
   10M UOP 的日志/全历史 hash map 变成推理依赖。本轮 JSON 和 Python 回放不是线上实现。
2. **运行模式**只保存活跃 ROB/LSQ、未完成事务和有界跨边界摘要；唯一事件 ID 可以
   是紧凑索引。静态解码/寄存器元数据复用一次；同一 data-ready 同时用于 wake 与 release。
3. **按状态变更推进，不逐周期推进**：只在 ready、admission、fill、callback、retire/
   容量释放时更新。不能单纯把所有 future request 排序后提前写 cache；应有能证明
   不再出现更早事件的提交 frontier，或可靠的事务回滚。当前 proposal 下界本身含近似，
   不能未经证明就充当安全 lookahead。
4. **先做组件成本账**：记录每 retired UOP 的事件数、队列/堆操作、失效重算、峰值活跃
   状态、checkpoint 复制字节和共享提交比例。声明被删除的原 pass/查询；若只增加
   工作且收益证据薄弱，停在诊断原型，不包装成高吞吐新架构。

统计传递、静态元数据复用、五组全历史有界化仍可独立优化，要求目标状态完全等价。
不要与准确率候选同时改变，以免解释不了精度或 wall time 的来源。更复杂的状态也不
自动意味着更慢：只有替换原先重复工作并实际量测后才能判断，本轮没有提速估计。

## 7. 下一轮执行门禁

1. 正侧 `223952` 的 line 生命周期和 gem5 path 已取得唯一关联；原 FastSim blocker/root
   属于虚假 DRAM 路径。下一取证点改为：移除该请求后，找到新增 55,896 core-cycles
   的首个 shared-order/response 分歧。负侧沿用已匹配请求补齐相同字段。
2. 用相同入口状态构造最小反例，确认同时需要删除和新增哪些边/状态。零改动输出严格
   同一；修复应在 owner 内替换，不追加末端常数。
3. 提交一份有界组件原型，报告必要关系、受限退休传播、真实候选的差异和成本，
   不把这三者混成一项 CPI 收益。窗口入口/出口状态未闭合时不声明完整因果解释。
4. 先通过 LLC32 正控制、L1D64 负尾部及未参与机制选择的窗口，再考虑完整 ROI /
   formal40+DSE54；已有完整 ROI 反例继续保留。沿用正控制恶化超过 0.5 pp 的停止线，
   也报告小于阈值的回归，不用平均值隐藏 P99/max 或 DSE 趋势退化。
5. 最后做固定 NUMA、同二进制、串行交错重复的端到端吞吐；计入所有辅助 pass。
   不以本轮开启诊断日志的运行时间评价生产性能。

## 8. 复现与变更范围

成对账本产物根目录：`tmp/paired-event-ledger-20260908.dZGudc/`。
核心数字保存在本文，原始输入、命令、校验与见证见 `runs.json`、`summary.json`、
`llc32-gem5-paired.json`、两份 `*-response-retire.json`；`run_probe.py` 和
`summarize.py` 保留完整诊断步骤。原 gem5 补采继续使用
`tmp/tealeaf-tail-timing-20260907/`。路径闭合和边界状态补采位于
`tmp/paired-event-path-20260908-*`，净化 sidecar 与消融结果位于
`tmp/measurement-boundary-state-20260908/`。跨负载 pilot 位于
`tmp/measurement-boundary-state-multiload-20260908/`，统一入口为 `summary.json`。

二进制 SHA256 与 FU 第一阶段相同：
`ac21ecbfb641f73324a1c3d1453e58d5480091772128ceb59faef0b639839f6f`。

```bash
cmake --build build -- -j16
./build/fastsim_tests
python3 tests/test_tail_timing_pair_scope.py
python3 tests/test_response_retire_witness.py
python3 tests/test_branch_response_recovery.py
python3 tests/test_tealeaf_cpi_diagnostics.py
python3 tests/test_measurement_boundary_memory_state.py

python3 tmp/paired-event-ledger-20260908.dZGudc/run_probe.py
python3 tools/audit_tail_timing_pairs.py \
  --collection tmp/tealeaf-tail-timing-20260907/llc32m-c04-full \
  --audit-stats tmp/paired-event-ledger-20260908.dZGudc/llc32-baseline/stats.json \
  --core 2 --output tmp/paired-event-ledger-20260908.dZGudc/llc32-gem5-paired.json
python3 tools/analyze_response_retire_witness.py \
  --audit tmp/paired-event-ledger-20260908.dZGudc/llc32-baseline/stats.json \
  --core 2 --output tmp/paired-event-ledger-20260908.dZGudc/llc32-response-retire.json
python3 tools/analyze_response_retire_witness.py \
  --audit tmp/paired-event-ledger-20260908.dZGudc/l1d64-baseline/stats.json \
  --core 1 --output tmp/paired-event-ledger-20260908.dZGudc/l1d64-response-retire.json
python3 tmp/paired-event-ledger-20260908.dZGudc/summarize.py
```

构建和 `fastsim_tests` 通过；边界状态净化测试通过。现在新增显式 opt-in trace/runtime
路径、stats audit 和工具，维护生产 manifest/默认配置未变；没有 commit/push。完整
CPI 精度门禁未通过，不能将目标请求的局部修复描述成整体收益。
跨负载 pilot 同样未通过推广门禁；生产输入和默认配置仍未改变。

## 9. 2026-09-08 实施进展：首个回归传播链已闭合

后续实施与完整数值见
[成对 frontier 首差异与 store 生命周期定位](paired-frontier-divergence-phase1-20260908.md)。
本计划第 7.1 项已完成：首个本核差异为 core2:223952 的 path 修正，首个跨核差异为
core1:276057 的 DRAM blocker 重排且方向为提前；最早有害符号翻转最终落在
core3:292582 的 SQ capacity，直接 owner 为 store 291860。

新增的普通 frontier audit SQ owner 仅为每个活跃 SQ 槽位保存一个 sequence/release，
满足本计划的有界在线状态要求；离线工具负责多窗口比较，不进入生产推理。单独 post-commit
虽切断原回归方向，却使 LLC32 正误差恶化；移除 callback 串行又造成 −30.3940% 低估。
因此下一项从泛化的“shared-order 首差异”收敛为：在保留 x86 single-store-in-flight
条件下，为 store 建立唯一的 commit→admission→service→callback→SQ-release owner，
并删除当前早期 DRAM duration 在后期 TSO drain 上的重复职责。

该候选不能用一次完整 ROI 的 CPI 升降作组件正确性判断。验收顺序为：先验证 owner
唯一性、事件偏序、容量区间和每段 latency 只消费一次；再与 gem5 成对比较 store
admission、DRAM service、callback、SQ release 和直接受阻 UOP；然后用 2×2 消融或
`R_AB−R_A−R_B+R_0` 量化边界状态与 store 生命周期的非加性交互；最后才检查跨负载
CPI/P99、参数趋势和吞吐。语义层失败可以直接停止，聚合 CPI 失败只说明整套组合尚未
通过推广门禁，不能反向否定其中已经独立取证的组件修复。

## 10. 范围修正：先做跨负载组件归因，再选择原子替换

291860→292582 只证明 store/DRAM/SQ 耦合在 TeaLeaf LLC32 中真实存在。当前跨负载
pilot 对 ASTCENC、Stockfish 和 TeaLeaf L1D64 只比较了整体 cycles/PMU，没有为它们
建立同粒度的 gem5↔FastSim 事件链；所以现有证据不足以认定 store 是全局第一误差源。

下一阶段从冻结数据中分层选择至少两类正 residual 和两类负 residual，并覆盖不同的
memory intensity、store pressure、frontend/branch pressure、core count 和 cache 配置。
当前可用锚点是 TeaLeaf LLC32（+9.6487%）、Stockfish C4（+7.1759%）、ASTCENC C4
（−14.0319%）和 TeaLeaf L1D64（−17.9910%）；Graph500 新补采在 provenance 修复前
只使用已有合法 reference，不把失败采集混入比较。

每个锚点使用相同账本字段，分别统计以下组件的首分歧、出现率、signed retire exposure
和未解释残差：

1. frontend/branch fetch 与恢复；
2. FU、dependency、ROB/IQ/LQ/SQ capacity；
3. load/store ordering、forwarding、TSO admission 与 release；
4. private-cache visibility、coherence/merge 和 shared response；
5. DRAM arrival、command blocker、service 与 callback。

窗口按事件条件抽样，而不是只挑最大 CPI 误差或使用 workload 名称开关；每类既包含
关键路径事件，也包含未暴露控制。组件优先级由“跨 case 重复出现的已证语义分歧 +
retire 暴露 + 可被统一机制解释的残差”决定。单例继续作为回归测试，不再作为全局实现
顺序的充分依据。

## 11. 跨负载组件矩阵与下一实施入口

第 10 节要求的矩阵已经完成，完整记录见
[跨负载组件矩阵第一阶段](cross-workload-component-matrix-phase1-20260908.md)。四个锚点共
配对 41,839 条 UOP，并对四个不同 workload/config 窗口追加 23,232 条密集 owner-chain
样本及 4,459 条严格单 line Ruby 层级比较。

结果否定了“先扩写 TeaLeaf store 单例”的顺序：相反误差符号的 TeaLeaf 配置具有近似
issue-gate 人口；TeaLeaf L1D64 的 421 条 local↔Sequencer-coalesced 不匹配指向 pending
line visibility，ASTCENC 则在路径已经匹配 DRAM 时仍有服务时长欠估，Stockfish 所选
阶段主要是前端/内核 serialization。它们不能由一个 cache/DRAM 或 SQ 常数统一解释。

已实施两层取证基础：FastSim 的有界 issue owner/producer 链，以及外部 gem5
`taotrace-native-response-v7` 补丁。后者在成功 Sequencer admission 和 hit callback 记录
真实 tick，并由配对工具拆成 issue→admission、admission→response 和 response→commit。
旧冻结 v6 窗口不含 tick，工具会显式报告 0 条 timing samples，不做回推。四个窗口随后
已用显式诊断 gem5 二进制完成 v7 重采，原参考 SHA 和新诊断 SHA 均写入 provenance，
没有把诊断运行伪装成冻结 reference。

v7 将首个实现范围收紧为普通 data load 的有界 line-generation ledger：TeaLeaf L1D64
421 条 local↔coalesced load 的服务差与 tail 差分别为 −109.1、−104.3 cycles；81 条双方
均为 DRAM 的 load 则分别为 −169.1、−170.4 cycles。后一类属于独立 DRAM 服务问题。
另外三个窗口的同类事件会被前序链和 retire 传播抵消或反转，不能把这两个局部数值当作
CPI 加法项。

实现先闭合每 line 唯一 generation/leader、callback 边界、无未来预约的容量准入以及
copy/rollback；线上只保留活跃 map 与 callback heap。随后在同一事务中替换 follower 的
path、private-cache visibility、completion 和 dependent-ready，不追加全流 UOP feedback
遍历。store response 位于 commit 后，继续使用单独生命周期；Stockfish 前端/内核窗口和
DRAM 动态服务也保持独立账本。逐段语义和控制窗通过后，才进入完整 CPI/吞吐矩阵。

第一代码切片已经完成 ledger 与 cache 状态边界。ledger 对未来 admission 只返回 retry，
不创建未来 reservation；callback 采用半开区间并带 generation 校验；active map 和 expiry
heap 均有硬上界。cache probe miss 不再隐式 fill，completion 时才选择 victim 并发布 tag；
原有同步 access 语义保持不变。这些 API 仍未连接 simulator，所以当前 CPI 与默认路径
完全不变。

第二代码切片已经完成独立 coordinator 与 component preflight。coordinator 用有界
event calendar 合并真实 admission/callback，同 tick callback 优先；只让唯一 leader 分配
service，follower 继承 response。它已和真实 `PrivateHierarchy` probe/fill 组成 harness，
验证 callback 可见性、唯一 demand 计数及 coordinator/cache/counters 联合回滚。preflight
则在 mutation 前按私有 set、全局资源域、同线和 active generation 建立传递闭包；遇到
store/atomic、IFetch、page walk/seed、functional-carried、跨核同线、私有 set 异线或时间
回退时整组件显式拒绝。两者都只保留有界活跃状态，不保存全流历史。

接入审查也确认它们仍不能直接放进现有 per-UOP feedback：年轻 UOP 可以先 issue，而该
循环按程序顺序访问；resource calendar 还会在 response 算完后再次推迟 issue；
`SharedSystem::access()` 会提前安装 LLC/directory 状态，现有 transaction 又整体快照全局
DRAM/CHA，无法在跨核交错后只撤销一个失败组件。下一切片因此是拆分无副作用的
`prepare-admission` 与唯一的 `consume-response`，并补齐 shared cache/directory/DRAM 的
延迟提交事务。完成后再接默认关闭的 load-only 实验开关并运行四负载矩阵。

## 12. native 组件闭包实验

后续四负载实验已先执行 admission/callback 组件闭包门禁，详见
[line-generation 组件闭包审计](line-generation-component-audit-20260908.md)。四个 v7
诊断窗共回连 890,517 个完整数据请求。严格 clean 人口中的同线 follower 与 gem5
coalesced 逐事件完全一致，验证半开 generation 边界；clean 事件比例在 TeaLeaf LLC32、
TeaLeaf L1D64、ASTCENC、Stockfish 分别为 20.18%、78.12%、67.44%、60.01%。

该结果将第一接入目标收紧为 TeaLeaf L1D64，但不把覆盖率解释为 CPI 贡献。clean 外的
follower-oracle 分歧以及缺失 IFetch/跨窗状态要求 Simulator 接入继续 fail closed；
ASTCENC 的 matched-DRAM 服务、TeaLeaf LLC32 的 store/SQ 和 Stockfish 的 frontend/kernel
仍按独立组件验收。

## 13. 最终 issue 影子准入与双轴分解

后续实现和实验见
[line-generation 准入影子账本](line-generation-admission-shadow-20260908.md)。Simulator
已能在 core timing 的 dependency、DTLB、Sequencer、MSHR 和 resource calendar 完成后
导出普通 load 的最终 issue proposal，并以默认关闭、只读、有界的双 ledger 与旧 memory
lower bound 比较。四负载开关前后的 CPI、PMU 和语义状态逐项相同。

该 proposal 不能直接升级为候选。TeaLeaf L1D64 的成对密集窗中，FastSim/gem5 follower
总量净差只有 81，逐事件却有 191 条 FastSim-only 和 272 条 gem5-only；Stockfish 两边
总数完全相同，身份仍有 5+5 条交换。局部双轴替换进一步把 TeaLeaf L1D64 的 gem5-only
拆成 issue spacing 单轴 165、parent lifetime 单轴 92、任一轴均可 13；反方向几乎全部
由 issue spacing 改变。这满足本计划“不用聚合误差掩盖耦合”的判据，也阻止只修改
response 的旧方案重新进入。

cache 侧已增加无副作用 prepare 与 per-set generation guarded commit，拒绝旧 proposal
时不改变计数或替换状态，transaction 会一起恢复 generation。仍待完成的是 SharedSystem
directory/CHA/DRAM 的 service proposal/atomic commit，以及唯一 response 对 completion、
producer-ready、资源释放和 callback fill 的一次性消费。issue 侧先追双向错分的 register
producer、dispatch 和同周期宽度 owner；跨 workload 控制通过后再接默认关闭 coordinator。
