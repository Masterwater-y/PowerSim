# FastSim CPI 全局模型审查（2026-09-08）

## 结论与证据边界

当前最有证据支持的架构根因是：**core issue、共享层次状态变化、response 和资源释放没有由同一套事件生命周期共同决定。** 基础调度包含额外等待，后续反馈又能制造提前完成；共享服务先按基础到达序求解，core 时间随后改变，已提交的路径和资源状态未必随之重求。这能解释局部语义修复为何不稳定地改变 CPI，但不能证明全部残差都来自同一组件。

还存在第二层独立问题：DRAM 服务、StoreSet、frontend、翻译等组件的机制近似。第三层是 committed trace 无法完整提供的 wrong-path、推测资源占用和边界在飞状态。前一层不闭合时，后一层的延迟参数会吸收前一层误差；前一层闭合也不保证后一层自动准确。

本次是分析任务：核对当前源码、维护配置、原始配对结果，重建并运行现有微型诊断，没有修改生产模型或默认开关，没有运行新的 CPI 全矩阵。现有研究已取得机制证据，不能表述为所有实验毫无价值；但这些证据尚未构成新的生产 CPI 收益。

## 1. 当前实现究竟做了什么

维护入口 `configs/gem5-fs-native-kernel.cfg` 经 v28_6/v28_5/v28_4/v28_2 组合启用 interval weave、Q=1024、private preview、并行 response feedback、sparse scoreboard、monotone IQ、materialized fast kernel、L1I、modeled physical I-fetch、fetch shadow、same-PC StoreSet 和 native CPL0/CPL3 指令。

因此不能继续把当前系统描述为“没有 ROB/LSQ/依赖/I-side/kernel，只加一个 miss penalty”。`docs/architecture.md` 混有早期实现与历史实验，不能直接作为当前生产路径清单。

实际求解关系是：

1. `IntervalCoreModel::schedule()` 用基础 latency 计算 fetch/dispatch/issue/completion/retire，按程序序处理 UOP 并预约资源。
2. producer 将 data memory event 的 issue 改成 `max(raw_issue, last_memory_issue)`，但 UOP 自身仍保留 raw issue。
3. time-epoch 按基础时刻接纳前缀；private/cache/shared replay 选择路径、修改状态并计算服务。
4. response feedback 传播依赖、队列和退休延迟；生产 reweave passes=1，causal timing/response retime/corrected suffix carry 均关闭。

位置：`src/interval_core.cpp:773`、`src/simulator.cpp:7780`、`:3809`、`:16708`、`:17445`、`:12310`。这些行号对应本次工作树。

问题不是“存在两个 pass”本身，而是两遍之间的合同不成立。第一遍所谓 lower bound 含有可避免的等待；第二遍按增量只向后推，无法撤销这些等待。同时，请求的时刻变化可以改变命中、合并、替换和 DRAM 行状态，而这些变化不是给 completion 加一个差值可以表达的。

gem5 的 O3 模型在 ready queue 中选择可执行指令，load response 经 writeback 唤醒依赖；它的官方说明也明确指出，提前按序执行后再套 timing backend 会丢失乱序 load 交互。这里借其说明核对职责，具体目标仍以项目冻结的 gem5 为准。[gem5 O3 文档](https://www.gem5.org/documentation/general_docs/cpu_models/O3CPU)

## 2. 当前二进制仍存在的内部因果反例

构建后重放已有功能输入，所有结果写入新的 `tmp/global-cpi-review-20260908.GyueDz/`，未覆盖旧实验。

| 反例 | 本次结果 | 说明 |
|---|---|---|
| memory-clamp，5 UOP | 独立 load 的 UOP issue=5，memory issue=29，response=233，UOP completion=209；consumer 在209 issue | 同一请求有两套基础时刻，反馈差值不足以保证消费者等待 response |
| same-line，2 load | 父请求 response=221；后继被判为 L1，response=31、completion=33 | private tag 的可见性早于数据返回；最终总周期仍221，局部错误可以被退休遮蔽 |
| FU future reservation，9 UOP | 独立 ALU dispatch=4、issue=30，最终55 cycles | 当前仍使用 FU 尾时刻预约；历史合法空档见证为同图30 cycles，但30不是 gem5 实测 |
| load→mispredicted branch→correct path | branch completion=414，correct-path fetch=238 | 分支恢复未由校正后的 completion 驱动；加入后继 load 链时旧/实验 recovery 为448/624 cycles |

以上通用反馈与 fast kernel 的 cycles/PMU 相同。第四例的独立分支和预测正确控制不变。另重放 DTLB 现有微型输入，两条路径同为245 cycles；DTLB 的时间倒退可见性结论来自当前 `translate()`/`retire_page_walks_through()` 代码及此前 generation 诊断，本次没有新增 generation 探针。

不能从这些例子推算完整 ROI 的 CPI 收益。它们证明的是：目前有些事件轨迹连自身的依赖和可见性合同都不满足。构建及单元测试通过不能推翻这些反例。

## 3. 为什么局部修复反复失效

### 3.1 修复的可能是数值，而不是请求关系

本次重新读取 `tmp/line-generation-admission-shadow-20260908/paired-gap-decomposition-baseline.json`。TeaLeaf L1D64 严格配对窗口中，两边 follower 身份相同149条，FastSim-only 191条，native-only 272条。由此得到：FastSim 判为 follower 的340条中只有43.82%与 native 匹配；native 的421条 follower 只找回35.39%。这些是该有选择的窗口人口上的身份指标，不是全 ROI 估计。

净差只有81条，实际双向错分463条。Stockfish 则两端各11条 follower，只有6条相同。调整合并总数、平均 latency 或 CPI 常数，无法确定究竟哪条 consumer 应等待哪个 parent。

L1D64 的可分解 native-only 中，165条只替换 issue spacing 即改变分类，92条只替换 parent lifetime 即改变分类，13条任一轴都足够。反向191条中190条由 spacing 单轴改变。故延长全部 response 或统一提前 issue 都没有充分依据。这些是固定人口的局部反事实，不可相加为 CPI 贡献。

### 3.2 register gate 是传播位置，未必是根因组件

同线 generation parent 在该窗口461条可分解错分中，没有一次是 child 的直接 register producer 或 winning issue owner。因此“多数 gate 是 register producer”不能直接推出“寄存器依赖信息错了”。producer completion 自身可能已带有 memory、dispatch、FU 或前端误差；还要比较两条请求各自的祖先链，解释相对间距。

四个 source 槽是否截断稀有指令仍可检查，但已有 Stockfish 研究排除了“把 StoreSet 当成第五个寄存器 source”这一解释。参见 `docs/stockfish-store-set-root-cause-20260821.md:209`。

### 3.3 CPI 是相互作用后的退休结果

边界状态修复曾把一个虚假460-cycle DRAM改成2-cycle L1，但完整 LLC32 总周期增加55,896；后续已追到共享顺序和 TSO/SQ release 的传播。FU、branch recovery、DRAM、pending-fill 的局部控制也有类似的正负方向差异。该证据支持存在补偿，但任何新回归仍须定位其自身的首个分歧，不能自动用“补偿”解释。

一项服务改变会同时改变 MLP、资源占用、到达交错、替换、后继关键路径。等待周期总和、winning-gate频率、短窗口位移都不是整段 CPI 的可加分解。依据与停止线见 `docs/optimization-decisions.md`。

## 4. 哪些组件缺失或不完整

| 层次 | 当前已有 | 关键缺口与证据强度 |
|---|---|---|
| core 调度与响应 | RAW、FU、各级宽度、ROB/IQ/LQ/SQ、response scoreboard | base schedule 不是可靠 lower bound；corrected issue、completion、释放不统一。代码和本次微型证据确定，不能据此量出全局占比 |
| load/cache | 几何、replacement、private层次、LLC transient、Sequencer容量；独立 generation/coordinator 原型 | 请求顺序与 private fill 可见性不统一，leader/follower 身份错分；原型未接入 Simulator。已有跨负载逐事件证据 |
| store/内存顺序 | 地址相关、转发、same-PC StoreSet、TSO/SQ drain | 地址生成、commit、Ruby admission、callback/SQ release 分属不同路径；SSIT/LFST训练、容量、清除不完整。已有 store 传播和 Stockfish 消融证据 |
| DRAM | 几何、bank/open-row日历、读写队列、可选 FR-FCFS | C4/C8 生产受拓扑启发式影响 bypass；多项命令时序为0；refresh生命周期缺失；到达本身还可能错误。匹配路径的服务残差证明它不能全部归到 cache 合并 |
| frontend/branch | L1I、fetch supply/shadow、预测器几何、native kernel指令 | response→branch recovery不闭合；完整 speculative history/squash/wrong-path资源流缺失；I-side物理映射是模型。控制边有反例，其他项贡献待量化 |
| MMU/rename | 64项DTLB、单walker；rename候选 | 当前固定12-cycle walk；真实PTE流候选关闭，ITLB缺失，物理寄存器free-list候选关闭；DTLB存在非单调查询状态问题。不能把容量数值存在当成生命周期等价 |
| Ruby/network | owner/sharer、upgrade/remote、CHA日历、固定NoC延迟 | 完整transient、retry/ack、message buffer和link/vnet竞争不完整。是多核/参数趋势风险，当前没有证明它们各自支配CPI |
| 功能输入/系统边界 | committed CPL0/CPL3、物理地址、功能依赖；边界状态sidecar候选 | wrong-path地址、被squash请求、观测窗前active generation无法从提交流完整恢复；真实线程迁移/阻塞唤醒调度不完整。已观察到边界漏访存，其他缺口仍需区分不可观测与实现遗漏 |

依据：当前 `src/simulator.cpp`、`src/interval_core.cpp`、维护配置、`docs/gem5-parameter-coverage.md` 和同日组件矩阵。不要将 generic prefetch 缺失列成当前主因：已有目标配置审计中 prefetch 为停用。native kernel也不能按旧synthetic kernel模型归因。

## 5. 两个容易高估的“已完成”

### 5.1 observed-clean 完全匹配包含筛选条件

`tools/audit_line_generation_components.py:152` 使用 native admission/callback 重建 follower；`:161` 起把与 native coalesced 不一致的事件标成 `follower_oracle_mismatch`；`:191` 起将带任何 reason 的整个组件排除。因此 clean 集合中“follower逐事件100%相等”部分由定义保证。

这一审计仍有价值：它测量在已观测数据上需排除多少组件，以及哪些组件适合进一步诊断。但不能把筛后100%当作独立预测精度或生产证书。它还不包含 IFetch、未提交请求和窗前active parent。

后续验收须先冻结仅依赖可部署功能输入/模型状态的 eligibility规则，再对全体 eligible事件评分，同时报告coverage和reject原因；不能看到native答案后再剔除错误。C++ preflight本身不读取oracle，但其完整性仍依赖调用方提供所有干扰域，不能继承离线clean百分比作为保证。

### 5.2 有 transaction 不等于原子事件提交

`SharedSystem` 已有 transaction/restore 和 timing snapshot，不能称为“完全没有事务”。新增 cache prepare/guarded commit 也已存在并通过测试。

但 `SharedSystem::access` 仍在选服务时改变 directory、invalidations、CHA、LLC、DRAM和计数器；没有接入按实际admission准备并原子提交全部相关状态的接口。`LineGenerationCoordinator` 是独立原型，不能在旧hierarchy replay已经更新状态之后插入它，再宣布请求只提交了一次。

“原子”还不表示所有动作一律推迟到callback：admission应占用对应资源，service推进服务日历，callback使数据可见并释放相应资源，store权限按协议完成。需要统一身份和各阶段所有权，保留这些不同事件时刻。

## 6. 应替换的完整边界

建议把下一项定义为 **core与shared之间的因果事件求解器替换**，包含以下不可拆开的职责；只做shared API或只对final issue排序都不算完成。

1. **可修正的core ready/proposal。** 同时表达依赖就绪、dispatch/ROB入场、FU/issue-width约束；不能把旧的虚假base等待永久固化。收到真实response后，只重求受影响后继。
2. **有序admission与服务提交。** 对当前可达请求生成无语义副作用proposal，验证line/set/directory/resource generation后提交；失效则重求，容量阻塞不能先预约未来槽位。核内程序序不等于执行序；跨核也不能只排序一批已经算完的时间戳。
3. **统一response来源。** 普通load的completion、dependent-ready、fill和对应资源释放都引用同一个request/generation的response；必要writeback/重启延迟由所属阶段添加。split/atomic/store必须保留各自语义，不能借load规则覆盖。
4. **跨epoch在飞状态。** Q只划分宿主工作批次；晚到proposal、未完成service/callback和资源占用必须有界携带。实现必须处理未出现的更早可达事件，不能将nonmonotonic proposal丢弃当成生产成功。
5. **闭合的混合组件边界。** load-only可以作为第一个垂直切片，但IFetch/store/异线同set/同DRAM域干扰要进入certificate。拒绝或回退必须覆盖整个相连组件，发生在不可恢复副作用前，并报告覆盖率。

这里的优先级有依据：它同时消除人为增加和遗漏的边，并让DRAM、StoreSet、frontend的独立机制能够在稳定的入口/出口合同上被比较。它不保证第一版完整ROI就更准，也不意味着一次性复制gem5所有组件。

instruction-driven/bound-weave本身并未被这些结果否定。zsim的设计依赖可用的bound和受控的路径变化近似；FastSim必须为自己的工作负载验证这些前提，不能仅因都有epoch和weave命名就继承精度结论。[zsim 原论文](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf)

## 7. 如何检验这条路线而不再重复局部补丁循环

第一层检验内部合同：请求身份与人口、callback/admission同tick顺序、唯一service、容量占用/释放、fill可见性、load response→consumer、branch resolution→恢复、store commit→admission→callback。例外必须按请求类型定义。过这些检查不要求全局CPI立刻改善，但不能通过调常数绕过。

第二层检验同事件的issue-spacing和service/lifetime两轴，分path/请求类型/privilege比较，追到关键consumer和退休暴露；正常与失败人口同时报告。以L1D64为首个机制窗口，LLC32验证store/SQ与正误差，ASTCENC验证匹配DRAM服务，Stockfish验证frontend/StoreSet。它们是控制集合，不是运行时workload规则。

第三层才验整体收益：冻结相同输入与Q，完整ROI、未参与选型的窗口、formal40与DSE54分别报告；保留P99/max、正负残差、参数方向/幅度/排序。两个矩阵的macro CPI与cycles/user-UOP不能混合。跨配置采集的功能前缀可能不同，趋势解释须携带输入provenance。

宿主成本必须同时约束：复用静态解码和可复用cache组件，以有界活动状态和受影响事件替代现有重复全UOP反馈；保留已有效的并行能力。不要把无限replay当成架构闭合，也不预报未经测量的吞吐提升。最终按项目既定同二进制、固定NUMA、串行交错重复门禁验收，不用累计嵌套计时冒充wall time。

## 8. 本次验证与尚未证明的内容

- `cmake --build build -- -j16` 成功；`./build/fastsim_tests` 输出 `all FastSim tests passed`。
- 现有4类微型输入分别重放generic/fast路径，共8次；cycles/PMU一致。
- `tools/probe_branch_response_frontier.py` 完成10个微型场景，必要关系、控制组和generic/fast检查通过；`python3 tests/test_paired_line_generation_gaps.py` 的3项测试通过。
- 二进制SHA256：`8d431ac437cfd1c8f82ec94b943d89d486bb206062aa073f497e4a104b1025e6`。
- 新产物：`tmp/global-cpi-review-20260908.GyueDz/`。历史配对数字来自原始JSON重新计算，不是本次新采gem5。

尚未证明：各机制对全ROI误差的百分比分解、新求解器的精度收益与吞吐成本、推测状态不可观测造成的可达到精度下限。当前证据支持改变架构替换边界，不支持宣布某一组件解释了全部17.99%尾差。
