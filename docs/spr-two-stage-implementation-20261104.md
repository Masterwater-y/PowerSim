# SPR 两阶段机制对齐实施与最终验证

用户已授权按[机制设计](spr-two-stage-mechanism-alignment-design-20261104.md)和
[实施路线](superpowers/plans/2026-11-04-spr-two-stage-mechanism-alignment.md)开始实施。
本文先于本轮代码修改落盘，持续区分目标、已实现、已接入、已验证和未覆盖项。

## 本轮执行合同

- 基线父提交ad04b90，分支fastsim-spr；gem5-spr/yinhaolang提交afb85b808a。
- 在当前独立SPR分支原地实施，显式暂存文件，不覆盖已有子仓库改动或旧trace；采集扩展仅在gem5-spr/yinhaolang的probe与构建绑定中实施，不改SPR CPU结构。
- 旧路径及默认行为保留。新资源profile、核心calendar、memory runtime分别显式选择；禁止互不兼容的竞争求解器同时启用。
- 两阶段保留：第一阶段准备功能事实/依赖；第二阶段确定实际准入和持久资源状态。原生时刻、命中、融合结果不得作为模型输入。
- P0→P1→P2是主依赖；P3核内资源与P4前端/翻译的独立组件可并行，最后统一接入。不能用组件测试冒充生产路径完成。
- 原始A3失败现场保留，不恢复父循环。新的验证使用本轮独立目录、源/二进制身份与墙钟限制。
- 若旧FST缺少某机制必需功能字段，先报告具体字段和受影响子域，不从oracle补答案，也不缩小人口。

## 文件所有权与集成接口

| 工作 | 独立所有范围 | 集成要求 |
|---|---|---|
| P0 | 新spr_mechanism_profile.hpp、导入工具与对应测试 | 输出实际引用对象的typed limits/active predicates；不把DMA64当CPU资源 |
| P1 | 新core_continuation/memory_runtime；simulator、config、main、CMake由协调者统一维护 | 区分功能最早就绪/暂定排程；分相前沿；接入真实SharedSystem |
| P2 | 新spr_store_buffer.hpp/.cpp及独立回归 | 共享store credit、尾合并、4-line、独立prepare/ordinary身份；不得复用旧单demand错误生命周期 |
| P3 | 新spr_core_resources.hpp/.cpp、interval_core局部适配与回归 | 三IQ、RR、load-bank的消费/释放点；OFF中性 |
| P4/P5 | 独立frontend/translation/prefetch组件，进入时细分文件 | 使用P0参数及P1服务接口；缺失功能资格显式化 |

主时序接口以设计中的CorePublication、NotBefore/BlockedOn、AdmissionResult为准。
组件不得在未知最终准入时污染cache/计数；callback和credit释放必须携带稳定owner/generation。

## 验证清单

1. 先复核构建与默认fastsim_tests，冻结当前预测二进制和四案原始有效配置/报告。
2. 每项机制先RED→GREEN，覆盖资源守恒、异常输入、同cycle与跨chunk、旧路径兼容。
3. 接入后运行组合回归与独立审查；真实trace验证必须由生产Simulator路径触发组件，统计证实激活。
4. 准确率用相同四案C4完整FST、逐核宏人口、retired-stream-completion-v1及维护native validator；列全部样本、CPI绝对/相对误差、MAE/MAPE及缺失证据。
5. 吞吐与准确率分开测：同源/同配置/同构建方式、固定CPU/NUMA、串行交错至少三对，分别记录warmup/measurement/end-to-end和RSS；保留所有样本。
6. 没有闭合或不支持子域时如实失败；不提高fixed-point次数、不在失败后取最后一轮、不悄悄切回旧模型。

## 状态

- [x] 方案、接口边界和验证合同落盘。
- [x] 中断恢复核查：无残留回放或代码半成品；旧四案批次已结束。
- [x] 基线构建/回归及工件冻结（默认测试通过，原二进制及SHA256保存在本轮tmp目录）。
- [x] P0 typed target profile导入、参数来源、能力门控及组件审查（不会自行启用完整runtime）。
- [ ] P1 continuation/安全准入/真实cache提交：read/ALU冷窗口实际子集已接入并审查，完整FS/warmup/任务钟尚未闭合。
- [ ] P2 native store、online Sequencer与资源账：单核统一/SA-SD、延迟publication、mixed R/W与私有层WB已接入并审查；early prepare及多核写一致性仍待实施。
- [ ] P3分区IQ/FU/bank：静态整数/zero子域已接入并审查；融合、rename free-list与完整frontend组合未资格化。
- [ ] P4前端/翻译与必要功能输入。
- [ ] P5自主预取与协议/DRAM顺序。
- [ ] P6集成、独立审查、同源效果与吞吐验收。

执行细账和RED/GREEN证据保存在本计划的.superpowers/sdd工作目录；大体积结果在
tmp/spr-two-stage-implementation-20261104/，最终精简报告归档到docs。

## 输入与接入核查（进行中）

现有`fst_memory_requests.hpp` v2伴随仅保存Data/NO_ACCESS/SoftwarePrefetch及micro-PC。
`StaticInstructionInfo`有部分静态barrier/locked资格，但未提供完整动态request flags、
mask、split/HTM等资格合同。新native store组件要求调用者明确认证Ordinary；
不能仅以Data角色和非atomic热记录替代这一认证。完整合并资格目前属于
`missing_functional_input`，不是通过oracle补答案，也不意味着此前近似路径的实验人口被取消。

P1核查确认原`compute_core_timing_feedback_impl`依赖窗口内已确定的memory反馈，不能
在未知响应时原地暂停。已抽取共用issue/writeback calendar；新的持久continuation
先覆盖明确声明的generic FU/load子集，未接入SPR共享FU、rename、分支恢复、翻译及
store生命周期时拒绝相应配置。该子集回归不代表真实SPR四案已改用新时序路径。

独立审查还发现并正在修复两处组件问题：IQ角色须依据routing filters与FU引用识别，
不能依据`instQueues`数组位置；StoreFacts默认值必须为Unsupported，不能默认认证Ordinary。
Generic FU失败轮询的游标修复已通过定向RED/GREEN和复审，整数端口仍只在成功时轮转。

## 必要的功能输入扩展设计

为修复store合并资格缺项，新增独立`.sreq`伴随及FST feature bit13；不改64字节热记录。
每条load/store/atomic记录必须有对应行（审查进一步确认Data角色也不能证明load可缓存），保存原始record ordinal/hash、micro-PC、物理地址、
完整request flags、context/requestor、字节数和最多64字节的byte-enable mask，及规范化
功能资格（完整/Ordinary-store/Ordinary-load/NO_ACCESS/split/masked/secure等）。Ordinary必须重用
`LSQUnit::isOrdinaryStore`的功能条件；不记录canWB、commit/offload/issue/response时刻或
原生合并结果。未取得mainReq或超出此版字段范围时明确Unknown/Unsupported，不能默认Ordinary。

伴随绑定完整原始FST的record数、逐记录内容hash及逐memory热记录hash；缺行、错序、重复、
header未完成或身份不符均失败。读取和写入采用流式单行缓冲，不把全量store表装入内存。
旧trace明确返回“无store功能资格”，兼容旧路径；新native路径单独要求该能力。
新增采集仅在collector扩展、构建和微负载回归完成后进行，并继续遵守全局400GB/395GB停止线
和实际空闲30GB底线。现有四案FST与oracle保持原件。


首次生产接入审查发现并进入修复：L1 miss后的L2必须在真实到达事件查询，不能在L1准入时
提前查询；Data load必须有独立普通可缓存资格；未接入的退休PMU观察器必须输出Unavailable/null，
不能输出Available的零；未接入的前端开关必须拒绝。`.sreq`首版因此在实际采集前扩大为每条
退休memory记录一行，增加Ordinary-load规范资格，并核对原始request flags、mask和hot op类别
是否与规范资格矛盾。schema仍不含原生时刻或命中结果。

原生重建统一使用已有`scripts/build_gem5_spr.sh`隔离工具链。先前手工调用引发libpython、
protobuf和TAOGEN_SHARED环境检查失败，记录保留；切回维护入口后，高并发首次生成参数头
出现短暂依赖竞态，重试前复核生成物，不改全局环境或删除原始工作树。


## 已取得的实际证据（不等同最终SPR验收）

- 旧路径：同一四案完整FST的基线/当前8次串行回放，逐核完整计数、time_accounting和
  scope_metrics（除host吞吐）完全一致。独立身份4/4有效，维护native验证器4/4通过。
  CPI MAE仍为0.4665106346 cycles/macro；这证明兼容性，没有宣称新机制改善误差。
- 新因果read/ALU生产子集：真实Simulator、PrivateHierarchy、SharedSystem和mixed DRAM，
  batch1/2/4一致；响应124同cycle释放信用并发下个fragment，最终退休248；L2到达反例修复后
  lookup127看到fill124，单miss/单hit并退休139。未接入的PMU输出Unavailable/null。
- 新采集字段：维护脚本重建成功。新四核FS探针（原有marked OpenMP二进制，100k共同截止、
  10k预热）完整采集并关闭；当前功能审计器读通4文件、919322条记录，新增.sreq内容/人口校验通过。
  此探针验证输入协议，不是SPEC四案新模型精度或硬件认证。
- 字段边界：Ordinary-load可覆盖已明确证明同属性、物理连续的跨line请求；本版原生producer
  未取得split完整request时保留Unknown。地址/数据依赖角色和SA/SD配对仍未声明，不能开启earlyprepare。

## Split 请求描述补齐

首轮实际C4的27条不完整请求全部带Split标志（18 read、9 write），不是随机丢行。
源码确认`SplitDataRequest::mainReq()`覆盖了基类断言接口，返回完整mask及由子请求汇总的
功能flags，但其PADDR=0只是内部占位，不能当作物理地址。后续仅在<=64字节、dense mask、
所有子请求的flags/context/requestor一致、VADDR连续且真实PADDR也与hot记录形成连续区间时
标记完整；使用`req(i)`核验物理片段，绝不使用占位PADDR。不能证明时继续Unknown，保留原件。
Ordinary-store仍排除Split；经过证明的普通load可分片。此改动只触及采集probe，不改SPR执行结构。

## 静态UOP与store依赖描述扩展设计

进一步按原生公开接口补`.uops.gz`（feature bit14），每条保留UOP一行，压缩流逐条读写。
字段为：hot ordinal/hash、decoder静态IntExecutionInfo（family、候选mask、标称结果latency，
不是实际grant/观察latency）、静态zero-idiom资格、store的统一/SA/SD角色与逻辑配对ID、
地址/数据依赖角色；逐原生source operand保留consumer index、RAW producer distance及
GPR/CF/OF/Flags/Other角色，补回旧FST按producer排序去重时丢失的operand关联。
模型仍自主分配端口、发射时刻和结果就绪时刻。静态标称值属于带源码策略指纹的target
配置/decoder描述，不能当作native执行样本或直接移植到不同target策略。

SA/SD配对只读现有`DynInst::sqIt`和entry公开的instruction/dataInstruction；内部native
SeqNum只作临时关联，输出连续的逻辑group ID，二者都退休后丢弃临时映射。精确放弃事件
依照既有功能retirement标记结束残留关联；共同截止可保留未完整配对的末端，必须显式报告。
统一store仍须其全部真实RAW就绪；拆分SA/SD分别使用各自UOP的真实源依赖。所有字段不含
ready/issue/response/canWB/实际融合/实际端口或cache结果。

sidecar头绑定静态策略SHA256（由子项目SCons在本树内生成，不依赖父项目），行绑定原始
hot内容，尾绑定全量record数/hash；旧trace明确Unknown。额外operand边投影须与旧完整RAW
集合一致，不能凭新字段增删source成员。缺失/错误的策略或行必须拒绝，保留默认旧路径。

### 真实输入入口补充：kernel-only warmup

第二次重采（`native-functional-request-c4-v3`）在初始快照阶段失败，保留完整日志：
10k 指令 warmup 中尚无 CPL3 fetch，旧采集器仅在用户 fetch 时初始化 PTE，
而 BEGIN 无条件要求 initial snapshot。这与已记录的精确 ROI checkpoint CR3 并不矛盾。
修复只放宽 probe 观察入口：有 marker 序列化 CR3 时允许首次 kernel fetch 读取该根；
无此身份的旧入口仍必须等待真实用户 fetch，不猜测地址空间，不改 SPR CPU 结构。
本次失败无 measurement 窗口，不能作为任何精度/吞吐结果。

### 当前原生 credit 与 core 子路径验证（非完整 SPR 验收）

实际 `Simulator::run_causal_runtime()` 的 opt-in 路径现已消费：ordinary 请求（含 follower）16、
独立 prepare credit、L1/L2 data fill16/48、各级 TBE256、SQ+committed buffer共享112、
有序4条不同 store line；与旧 MSHR256容量代理分离。统一 store 的 committed offload、
尾部合并与非头 callback 保序释放已有真实层级测试。独立 detached drain 不延长已回放完
核心的 CPI 窗口。P3 实际 core 接入 execution/memory/store-data IQ资源、端口轮询与
load bank准入，修复 bank 局部拒绝误阻塞年轻不同 bank请求的问题。

新 ABI 下三个维护 CMake 测试（`fst_static_uops`、`spr_native_core_resources`、
`spr_native_memory_runtime`）已成功；原始反例及 source batch1/2/7/128 生命周期一致。
这些是机制结构测试，不是四个 SPECspeed 完整新路径的精度或高吞吐验收。
正在继续接入 mixed read/write权限、转发和 dirty eviction；现有原生路径遇到这些输入仍
全窗口拒绝，禁止提交部分 CPI。新 `.uops.gz` 为静态 decoder 信息和逻辑配对提供输入，
不代表 result-role forwarding、zero-idiom rename、SA/SD时序适配已经完成。

静态输入审计的 `passed` 只证明格式、完整热流绑定和 RAW 集合投影一致，
不会自动证明输入 SHA 与某次选定 target 相容。profile importer 导出预期
`spr.profile.static_uop_policy_sha256`；production integer 入口仍在结果角色转发、
zero-idiom rename 等适配完成前拒绝。静态策略 hash 的依赖闭包包括完整 x86
ISA/insts/regs 源树和相关 CPU/collector 定义；已通过修改 media decoder 源使
hash 失效的回归，防止只覆盖 regop 而遗漏 SIMD decoder。

### 新静态输入的实际 C4 roundtrip

`native-functional-request-c4-v4` 使用独立 gem5-spr/yinhaolang 的当前 collector 完成
FS 四核采集（100k first-core-user-UOP common-end、10k warmup，89.91s，guard=null）。
维护 C++审计器读完4条全流：955,703条record、580,346个macro span；109,857条read，
load eligibility未知0；87,548条write，request descriptor不完整0。普通load资格105,224、
普通store85,297，其余特殊种类仍保留原始人口，没有重分类为普通请求。
9,436个SA与9,436个SD完整匹配，partial group0；每条record均有静态UOP描述和operand
角色，原始完整RAW投影一致。策略`e52b92dbcd5f8f235f78fa4de2419a18026cacea7a3dca3974235f6da29d5051`
与本次源码和SCons生成头一致。该源与首轮v2的实际退休人口不同，禁止混用其计数作误差比较。

已使用冻结的同一采集器并行重采原四个SPECspeed C4案例（10M target、500k warmup，
原二进制/参数/32GiB与共同截止政策），输出至本轮`static-speed-c4/source/`，与旧source
完全分离。沿用维护capture命令、全局395GB停止阈值及原逐案容量reservation；四条任务只
负责采集，不启动任何A3或把新runtime尚未支持的窗口提交为成功分数。当前四条在采集中。

### 任务时钟缺口定位：重复 BEGIN 重置全局起点

v4 唯一越界观察为`core0:e1 identity`，功能锚点属于measurement，实际commit比输出的
common_start早83,980,050 ticks。native counter已在自动warmup截止打开且继续累计；
之后workload的m5 WORKBEGIN和延迟串口BEGIN又写了全局CPL起点，导致两套计时起点分离。
不是删除identity或改变退休人口可以修复。修复将functional common窗口的起点归属唯一化：
raw work-item marker仅保留ROI诊断；首次traceMeasurementBegin打开窗口，之后通知幂等；
旧无functional-warmup的ROI行为保留。必须重新采集并由原维护计时审计器验证。

四个SPECspeed输入正在使用冻结的f7abcbaec1采集器，不被后续编译覆盖。对应完整子仓库
源码已冻结为本轮独立只读worktree；后续profile若核对静态输入策略必须选这份源码。
其计时仍逐案独立检查，不能因小型探针出现此缺口就擅自修正这些原生计数。

### 活跃机制校正：当前整数 result-path 调整为空

复核实际Marian四核config：每核`resultForwarding.paths`为空，5个IntegerALU分别连接
port0/1/5/6/11，ALU/SHFT/SlowInt/BM/Branch各服务的issueInterval=1、completionDelay=0、
prfDelay=0。profile importer现逐对象记录该配置，路径调整标为`disabled_by_target`，
不再把它写成当前target必须启用的缺项；24项profile回归通过，跨核不一致拒绝。
仍未完成的整数部分是production静态descriptor绑定、zero-idiom rename与组合验证。
若后续target启用非空结果路径，必须按operand角色/consumer index和端口实现转发规则，
当前nominal形态的验收不能移植过去。

### 下一服务接口：DRAM admission 与物理完成分离

原生`MemCtrl::addToWriteQueue`在真实有容量的write queue接收后，以frontendLatency回应；
不会等DDR写物理完成。现MixedDramController只返回已选择的物理服务，不能把它当作WB ACK。
维护控制器将增加`advance_with_admissions(frontier)`，返回本次真实准入receipt与既有物理
completion两个typed集合。仍使用同一严格前沿/容量/选取状态机；无观察需求的旧`advance`
不生成receipt，无跨全流历史。验证包括低水位write仍未被选择却已接收、满队列延迟准入、
无重复receipt、不同frontier粒度的状态/时序一致、旧接口无变化。上层只可在真实receipt后
安排frontend ACK；write-queue合并/读转发和完整native LLC WB的资格单列，不凭接口存在
就宣称已对齐全部MemCtrl机制。

重复BEGIN修复的实际验证：`native-functional-request-c4-v5`重新FS采集完成0/93.41s，
维护prepare读通967,779条全量record，功能输入与目标身份通过，独立native任务时钟
`status=available`（`task-lifecycle-basecpu-active-v1` / `gem5-basecpu-active-cycles-v1`）。
SA/SD9,274对、partial group0；新策略SHA0387bbf5...bac7371与该次构建源码一致。
v4失败计时原件保留，未通过离线改写起点来凑分。此结果仍不是FastSim新runtime的CPI精度。

已完成的Graph500全量重采输入另有42,758,519条record，输入审计全部通过；维护prepare
同时给出target identity有效和native task clock available。它属于冻结f7采集器的正式
10M/500k源，与新v5小型探针不混用，后续模型效果使用同一案例自身的完整人口与原生计时。

子项目版本维护：输入扩展已在独立gem5-spr/yinhaolang提交`f7abcbaec1`，重复BEGIN观察器
修复为`454a5f4d2b`。没有推送其原origin。父仓库保留可拉取02119d1 gitlink；新完整
portable delta为`patches/spr-gem5-source-20261104.patch`，旧20261103补丁保留历史。

### 跨页请求资格：v2复用原功能翻译分片

四案全量输入均已采完且独立native任务时钟available。进一步逐行检查发现：FLAC的6210个
read、XZ的8个read及Marian的1个write，全部是8/16字节跨4KiB请求。原`.xlat.gz`已保留
真实的逐页物理地址，例如FLAC VA891ff8→PA7f99faff8，下一页VA892000→PA7f99fc000；
物理页并不连续。v1 `.sreq`要求整个物理区间连续，因而保守留下Unknown；不能把第二页
错误推为firstPA+8，也不能从oracle补地址或改旧文件。

修复新增sreq schema v2的显式`uses_paged_fragments`资格位，56B行大小保留。producer仍
逐实际req(i)核对flags/context/requestor/byte mask和虚拟连续覆盖，且每个实际packet的
物理区间必须与同条退休`.xlat`功能分片逐段相符；只有这些正证据齐备才设置Complete。
reader要求v2声明和已绑定的dynamic-translation companion，逐条核对页类型/leaf已知、
虚拟连续完整覆盖、首物理地址与hot一致，随后按独立分片保留地址。v1原件维持Unknown。
普通store仍排除Split；不通过新资格位放宽store引擎边界。模型目前的dynamic/split门控
继续保留，未来接入必须消费每个真实分片，不能只读资格位再按base重建地址。

验证先用两片物理不连续的C++/wire反例，缺少翻译、错误首地址、范围/属性缺失均拒绝；
再重采受影响的三个原负载（同10M/500k设置），不覆盖本轮已冻结v1证据。

跨页资格的源码策略闭包同时加入store request wire、共享翻译wire/writer、
`functional_translation.hh`、`lsq.hh/.cc`和`request.hh`，覆盖直接参与资格与片段产生的
定义。该SHA仍是功能输入策略声明，不能代替完整源码/二进制身份及模型机制资格审查。

v1全量新源的审计表（仅输入/计时资格，尚未运行完整新模型）：

| C4案例 | 全量records | load资格Unknown | store描述不完整 | native任务时钟 |
|---|---:|---:|---:|---|
| Graph500 | 42,758,519 | 0 | 0 | available |
| FLAC | 38,485,719 | 6,210 | 0 | available |
| XZ | 39,965,929 | 8 | 0 | available |
| Marian | 41,944,825 | 0 | 1 | available |

四案16条全流功能审计、target identity和SA/SD完整配对检查均通过；上表Unknown仍是
独立机制入口资格缺项，不能被功能审计的passed掩盖。新源/编译/参数/采集receipt与全部
描述文件保存在本轮`static-speed-c4/`，不会替换历史基线。

### MemCtrl 前端生命周期的独立 opt-in

下一步在维护MixedDramController增加显式`enable_native_frontend()`（首次submit前）。
旧advance/projected行为保留。启用后必须使用带admission返回值的严格接口：write在有容量
时接收并产生admission+frontendLatency的外部ACK；同line待选write合并但仍各自ACK；
read先通过原生read-queue容量检查，命中待选write queue时以frontendLatency回应，不创建
虚假DDR读。read queue满不能靠forward绕过，write queue满也不能靠merge绕过。

admission将区分merged-write/forwarded-read并可携带frontend_response时刻；物理selection
completion保留原身份，不能再向已ACK的逻辑write重复回调。普通DRAM读仍等待物理完成
及原来的frontend+backend时延。新增分项守恒：admitted = 物理selected + 当前读写队列
+ merged write + forwarded read；前端响应不代表DDR物理执行完成。该API不把现有近似
row-hit-first scheduler升级为gem5完整FR-FCFS，调度/刷新机制仍单独资格化。

### 当前可用于模型验收的四案输入

v2三案重新完成整窗采集与维护prepare审计，旧v1原件保留。

| C4案例 | 选用源 | 全量records | 跨页新资格条数 | load未知 / store描述不完整 | 任务时钟 |
|---|---|---:|---:|---:|---|
| Graph500 | v1 | 42,758,519 | 0 | 0 / 0 | available |
| FLAC | v2 | 38,554,477 | 6,293 | 0 / 0 | available |
| XZ | v2 | 39,903,596 | 8 | 0 / 0 | available |
| Marian | v2 | 45,404,303 | 2 | 0 / 0 | available |

四案target identity、全流功能内容/人口和SA/SD配对通过，partial store group0。
来源清单为本轮qualified-inputs-c4-four-cases.json，逐案冻结collector二进制、源码策略和
实际人口，不混用采集版本计数。子仓库新提交94cb06d613/yinhaolang；portable patch更新
并反向check通过。新模型尚需生产适配、全量回放及精度/吞吐验收，输入通过不能替代。

### 新发现的缓存准入串行来源

Sequencer接受ordinary/follower与L0 mandatory处理必须分层。B等待transient victim时，
年轻A+4仍可作为A的alias被接收；tag/fill/TBE不能提前在CPU接收处拒绝。缓存tag在请求
时预留，fill完成该位置；原生LRURP用model-cycle时间戳及首way tie，旧age-LRU保持不变。
入口时延为mandatory_queue_latency1加dataAccessLatency3减1，共3cycle，发生在lookup
之前。下行L0 request_latency2另计，不能在lookup后重加cache hit latency。
这些参数和385tick同频域均有独立来源，实际Simulator路径与结构反例已通过；不代表完整FS窗口支持。

### 首批生产子域集成检查点

完整CMake构建、默认fastsim_tests及新增15个C++测试目标通过；参数导入、输入准备、
privilege与native验证来源共69项Python回归通过。独立审查通过当前P2扩展子域与
MemCtrl前端组件。证据保存在本轮tmp目录final-integrated-*及checkpoint-python-source-provenance.log。

当前真实路径支持冷窗口、单核统一普通store/NO_ACCESS与load混合，load字节覆盖转发、
Sequencer先接收再独立处理mandatory、请求时tag/transient victim预留、模型cycle时间戳LRU、
L0无ACK PUTX、私有L2持TBE至LLC ACK、本地inclusive InvOwn。ordinary16、fill16/48、
各层TBE256分别计账，普通follower也占ordinary credit。shared SQ+buffer112在offload时
保留，tail merge或头部完成再释放；不把offload误当释放SQ。

最终退休计时结构见证：最后retirement与common endpoint均为221，脱离退休的write
callback发生在252；CPI周期保持221，不向退休尾部补量。

限制继续fail closed：多核写/私有替换、LLC owner recall/dirty至内存、secure、SA/SD、
early prepare、动态翻译/前端、完整FS warmup与task-clock适配。MemCtrl的write接收ACK、
WQ合并与read forwarding目前仅组件通过，尚未接入该路径，不计为已覆盖机制。
整数静态descriptor与zero-idiom为下一项；完整四案新模型精度和三对吞吐验收尚未完成。

### 下一独立组件：模型自主 RubyPrefetcher

在P3静态整数适配期间，独立实现预取候选生成，不改Simulator接入或默认配置。
依据gem5-spr/src/mem/ruby/structures/RubyPrefetcher.cc及MESI_Three_Level-L0/L1cache.sm，
接口分别接受模型实际demand miss、data access、prefetch hit/partial hit、queue dequeue，
不接受native时间、命中结果或预取地址。输入pc/VA/PA/context仅为功能事实；cycle仅为
模型当前事件时间。L1 IP策略为16组4路、四样本、同页VA stride到当前物理页映射；
L1/L2 stream策略保留正/负unit filter和nonunit filter不同训练阈值、4个stream、
FIFO filter8/8、startup1、cross_page=false与排队期间dedup。L2额外adjacent候选
由controller实际TrainPrefetch谓词触发，不把L1的IP策略复制到L2。

候选持有功能trigger owner和来源类别，queued set只覆盖尚未dequeue的line；
不保存全流历史。hit/miss callbacks由模型协议事件产生。当前native源码
requestIssued/requestCompleted没有写入路径，stream匹配miss的抑制按实际代码保留，
不能按推测补全此策略。组件测试先验证候选/队列生命周期与样本/页边界，后续接入
必须再验证fill/TBE、污染与需求late merge；组件通过不提升完整P5资格。

本检查点新编译二进制串行重放原四案OFF配置，逐核预测、time_accounting和剔除宿主throughput的scope均与冻结基线逐字段一致（4/4）；新目录checkpoint-off-compatibility保留命令、二进制SHA及全部输出。这是兼容回归，不能当作新模型精度或正式吞吐实验。

自主预取候选组件现已实现，独立审查与结构测试、ASan/UBSan通过。覆盖IP/context/页边界、正负unit/nonunit阈值、stream替换/续取、dequeue结束dedup及功能owner；仍未接入cache协议，不报告全P5通过。

### 下一独立组件：FR-FCFS bank-preparation 候选裁决

当前MixedDram的row-hit-first不能代表gem5 DRAMInterface::chooseNextFRFCFS。
新增显式kBankPrepFrfcfs策略，沿用当前模型bank时间表，按原生顺序选择：
seamless row hit优先；否则可隐藏PRE/ACT的bank可胜过非seamless已开行；
否则保留首个prepped hit或最早可准备bank内首个排队包。minBankPrep保留
原生rank/bank扫描与同候选mask/hidden标志更新规则；换向min-col增加
min(tRTW,tCS)或min(tWTR,tCS)。全队列参与，禁止与旧selection_window截断混用。
该策略只更换候选裁决，旧默认与FCFS不动；尚无rank refresh/power/命令总线
完整实现，不能称整个DRAM已与gem5等价。以actual controller请求序列验证
“closed bank准备可隐藏时胜过迟到row hit”反例及frontier不变性。

### 下一生产适配：SA/SD 与 commit→IEW publication

输入已具备配对和各自RAW，后续按一个SQ项、两个IQ域实现SA/SD：地址成员
占memoryIQ并独立使用地址FU，data成员占storeDataIQ/端口4或9，只有地址
成员分配shared SQ credit。地址/数据执行可以任意先后，保留同一逻辑store
的独立ready状态；SA退休不能授权offload。lsq_unit.cc:906-938以SD的退休序号
发布split store的canWB，iew.cc:1540-1554又在writebackStores之后处理commitStores。
因此实际publication经commitToIEWDelay到达，最早offload再晚一个cycle；
当前统一store路径的立即commit/allowWriteback也要同步修正，不增加退休尾部周期。
必须由模型日历触发这两个边界，不能直接拿未来cycle调用StoreBuffer使内部时间跳前。

构造源测试覆盖SA先/data先、一个SQ/分区IQ/FU人口、SD退休前不发布、
commitToIEWDelay0/1/3与额外一个writeback扫描cycle、跨chunk配对、末端缺mate
或重复配对失败。Unified仍按所有实际RAW共同就绪；不使用原生ready时间。
精确abandonment/special/split-request/early prepare若未闭合继续拒绝全窗口，
不丢弃SA/SD，也不把两个成员各当一条内存store。

### P3 静态整数与 zero-idiom 生产子域

新增默认关闭的core.causal_static_integer：目标静态策略SHA与实际FST header
完全匹配，逐条验证执行/operand描述与完整RAW投影。family/mask/nominal latency
直接消费静态事实；family0保留generic路径，不用OpClass推断managed family。
目标ResultForwarding.paths为空，不添转发延迟；managed结果在nominal value时可用，
与WB/通知完成分开。zero-idiom在已支持的零前端延迟边界完成，绕过IQ/FU/WB，
仍占dispatch/ROB/commit与目的寄存器生命周期计数；这些计数不代表物理free-list已实现。
退休统计区分micro UOP与last-member宏人口。

真实BinaryTraceSource→Simulator测试覆盖四种zero、满IQ/WB/ROB、RAW结果就绪、
chunk1/2/7/128、策略/静态事实错误与末尾损坏拒绝。canonical完整构建、默认测试
和17个机制目标已通过。独立审查发现导入门控还须核验实际ExecutionPorts连接、
ALU对象身份、legacy intALU冲突与active FU pool的12槽要求；已补全实际shared port图、ALU连接、legacy互斥及12槽校验，
13种图/槽反例与跨核资源误共享反例先RED后GREEN；独立再审已通过。四案目标策略均已按各自冻结源码单独导入，
输入策略身份匹配；这仍不启用完整模型。

四案另有已知输入限制：高层OpenMP并行区/barrier因果标记仍不完整；普通load/store
资格与任务时钟完整不能替代该同步证据。后续验证继续单独报告，不据此声称完美输入。

### 机制效果见证（构造功能源，非四案精度）

以下来自实际Simulator路径的结构探针与冻结CSV，既不是gem5误差对比，
也不是吞吐测试。完整默认/17组测试通过后的二进制SHA为
4787f8723f1bbb490119c2c4087b5e0ad947255dc2dc12d8cadcf0d69cedc22e，
原始数据在本轮tmp/mechanism-witnesses/summary.json及同目录CSV。

| 机制 | 实际见证 | 证明范围 |
|---|---|---|
| Sequencer与mandatory分层 | A的年轻alias于cycle0接受，首响应118 | B等tag不阻塞CPU端alias接收 |
| ordinary request信用 | 17个逻辑请求完整，第17个在首callback118同cycle接受 | follower也占16信用并按真实callback归还 |
| fill与旧MSHR分域 | 旧L1 MSHR设1、typed fill设2，第二lookup3早于首响应118 | 不再把旧MSHR代理当fill/TBE同一资源 |
| store窗口 | 首callback前恰好接受4条不同line | 允许四line并发，同时阻止第五条越界 |
| 退休尾部 | 退休221、独立store callback252、一次InvOwn/零伪重试 | CPI/common endpoint保持221，不补尾量 |

这些见证不覆盖SA/SD、early prepare或四核写一致性。下一publication修复将
产生新的独立见证，不能拿本表的历史cycle当作固定目标常数。

本阶段最终：默认+17组C++测试、72项Python来源/配置回归通过；修正后的四案typed profile均导入成功，策略hash逐案匹配。新的bank-preparation候选策略与预取候选组件均通过独立审查及ASan/UBSan，仍保留各自生产接入限制。

### 新增确认的功能输入缺口：取指翻译

.ipages只有按退休ordinal生效的VA→PA页映射；.xlat当前仅绑定data memory成员。
ITLB miss和共享STLB/PTW服务需要完整指令页大小、root/PCID和PTE物理路径。
原生Execute成功翻译已经持有FunctionalTranslationExtension，但当前frontend
只保存VA/PA code lease，未把该纯功能路径带到退休。取指来自DSB还是MITE、
实际TLB/cache命中和任何native时间均不进入新输入。

在本轮已批准的“必要版本化功能扩展”范围内，选择在成功Execute翻译时复制
不可变功能事实，解码/DSB宏创建时快照，经过FrontendRecord→DynInst运输至
退休伴随表。退休时重新读取页表会混入后来的映射；不能拿native ifetch诊断
时间/命中日志替代。多cycle展开必须在宏开始前固定事实，不能在退休回查两个
当前code lease。所有改动只做观察/元数据运输，不更改SPR取指、TLB或cache裁决。

拟用独立版本化.ixlat.gz，声明新输入能力；每个.ffe宏span对应一行纯功能翻译
描述，绑定完整hot stream人口与hash和collector策略身份，包含1/2个真实指令
分片、页大小/root/PCID/PTE路径，pseudo/unknown单独声明。缺sidecar、错span、
错hot身份或片段覆盖错误失败；Unknown路径不得作为零延迟walk或完整支持。
旧FST、旧功能伴随表保持原资格，不离线补写。必要时按新collector策略重新采集
并保留原输入。当前还在源级设计审计，尚未实现或宣布新输入可用。

另需区分：同CR3重写/flushNonGlobal、INVLPG、PCID/控制模式的功能影响不能从
只记录root变化的.asid可靠推导。后续需绑定退休的控制写/操作数或等价功能事件，
不能序列化native实际TLB清空结果及发生cycle作为模型输入。还原checkpoint中的
TLB条目可能只证明leaf而不证明旧PTE路径，这种缺失继续显式Unknown。

取指输入设计审计已完成：MITE必须在decoder消费每个byte chunk时累计其原始
响应事实，DSB取当前验证lease的事实；宏开始时固定，不能仅在退休时查lease。
选择先实现纯功能InstructionByteWitness helper，验证跨line/page、覆盖/冲突、
不可变快照与leaf/path两级资格，再接入native观察点和新伴随表。旧checkpoint
路径缺失仍Unknown，不采用额外物理功能读取作为完整路径证明；即使叶一致也
不能证明旧native路径。该选择不会强制flush或重走原生MMU。

### SA/SD 与 publication 检查点

已接入单核冷窗口生产子域：SA/SD保留各自RAW、IQ域和端口，一个逻辑store
只分配一个shared SQ credit；SA退休后仍保留配对状态，SD退休才安排commit，
再经显式commitToIEWDelay及下一cycle扫描开放writeback。统一store同时修正。
稀疏pending publication使最后退休后的写回仍可前进，但不延长已封口CPI周期。
EOF缺mate、复用/重复group、错误role/mask/RAW/policy、unsupported请求全窗口拒绝。

独立审查发现SD继承旧integer_alu_latency/pipelined，已改为选中SPR的
OpDesc latency1/pipelined；实际BinaryTraceSource将无关旧ALU设7/非流水，
先RED再GREEN，OFF原路径保留。再审通过。完整构建、默认与18组C++机制测试、
73项Python来源/导入回归通过。四案从各自冻结源码导入publication delay均为1，
policy身份吻合；这仍不等于四案新模型CPI/吞吐验收。

纯功能instruction-byte witness helper也已完成独立修复再审：4/8-byte PTE地址
与物理0按明确Known/levels处理，未增加无根据的非零限制。源级快照与codec/native
运输仍待实现，现有四案尚未被升级为完整取指翻译/失效控制输入。

### 取指伴随表首次实际 FS 验证及上下文修复

取指观察与运输已接入成功 Execute、真实返回字节、decoder、当前 DSB lease、
PCState/DynInst 和退休 span；`.ixlat` v1 默认关闭，完整热流/FFE 身份与策略绑定。
完整构建、默认及19组C++目标通过；Python来源/资格回归94项通过，随后退休
partial-span与字节/path完整性分离的修复新增两项资格测试并通过独立复审。

冻结原生二进制与源码后，独立 C4 FS OFF/ON 各运行约95秒，两组均成功采集并
通过维护入口准备。开启组548,069个宏span中有15个双分片span、10,479个已映射
跨cacheline span，但有6个内核span报ContextMismatch，不能宣布完整输入。
两次独立KVM启动的退休人口分别945,919和912,202，不满足同人口中性对照；
这些数字不构成观察器改变目标时序的证据，也不构成观察器中性证明。

源级确认两处错误等同：decoder把字节使用时的CR3强制等同于Execute完成时
已冻结的来源ASID，退休检查又把来源ASID强制等同于退休CR3。字节与已创建
记录可以跨CR3切换存活；global TLB复用的历史walk_root则是另一个独立事实。
先按版本化v2修复功能合同：分别保留每片Execute-origin ASID、decode/selection
ASID及既有FFE的退休ASID，不改写来源、不重走页表、不强制flush。来源差异
单独计数和资格化，不据此宣布逻辑MMU控制完整。v1保持原语义与原Unknown，
不离线补写。六个已丢失来源的v1记录无法反推具体旧root，必须保留缺失结论。

实际证据位于本轮tmp目录的instruction-input-source-freeze-v1.json、
instruction-input-native-pair-v1.json和instruction-input-unknown-v1.json。
delay/remap/replay的原生激活与同检查点观察器中性仍分别待验证。

为消除两次KVM启动的人口差异，下一中性探针拟在同一新SPR二进制的首次
workload checkpoint marker、尚未切换O3时保存一次KVM状态，再分别以OFF/ON
恢复；恢复后调用原有handoff/warmup处理，不重新启动guest或重复marker。
只在本轮独立实验配置中复用已有Simulator checkpoint API，绑定新checkpoint
内容hash、二进制/runner/内核/负载/参数与目标身份，不接收旧gem5-fs checkpoint，
不改变SPR架构。仍须逐核热流、原生请求/计数、共同终点和周期完全相同才能
认定中性；仅保存/恢复成功不能替代这些检查。若恢复使路径元数据缺失，继续
单独报告，不强制清空原生TLB来修饰结果。

### 下一生产接入：early store prepare 的独立所有权

现有共享SQ/buffer、普通Sequencer16信用及私有fill/TBE账已经分域；prepare
不能伪装成普通load/store或再占一份普通信用。下一步使用独立hint token与同一
物理line事务身份，在原生optional队列入口后才能分配实际资源。源证据是
Sequencer::makeRequest的StorePrepare先行分支、L0的enqueuePrefetch及SP_IE转换。

原生cap16达到上限时丢弃hint但CPU端返回Issued并消耗store发送端口；写需求
优先发送，剩余端口才发prepare。optional入口延迟1cycle，不能沿用mandatory
的entry/dataAccess延迟。L0 I态prepare需要TBE和data-fill，稳定E/M或其他
已在飞行事务则Ignored并归还hint信用。SP_IE收到store时TakenOver归还hint
信用，物理fill/TBE继续存活并归属同一个物理事务，直到真实响应；不能再发
一个GETX或DRAM请求。SP_IE收到load需等待，独立NO_ACCESS不发送prepare。

实施前先用实际Simulator组合回归覆盖before-data/commit发出、16+1的drop、
普通16信用独立、同line多hint只建立一个物理事务、store takeover零重复流量、
响应/失效/重试代际与跨chunk守恒，并验证最后退休后的hint尾部不计入CPI。
当前single-core协议仅有已资格化的状态；共享S升级、多核写一致性和不支持
控制流仍须拒绝整个窗口，不因为接上prepare就移除这些门控。

### 取指 v2 验证检查点

v2源码/live、冻结副本与编译生成策略均为
42bae5560d77f3d4275431dd76912d1c35f068d354ab6bd4cedc67192427b89b；
冻结gem5二进制SHA256为
b248921ae001c1729ab574fd9713ff147db2a50e967b82dd7803c47c6d9c731c。
实际native重建成功，FastSim完整构建、默认及19组机制目标和99项Python回归
通过；两轮独立修复审查均通过。这些是本检查点结果，之后的early-prepare
代码必须重新构建和验证，不能沿用该二进制声称已通过。

新SPR首次handoff前保存的同一KVM检查点分别恢复OFF/ON，两个完整窗口均为
967,384条记录、580,069个宏span。除声明新伴随表的feature bit外，hot header
和全部记录相同；原有10类功能伴随、native hierarchy summary、独立task-clock、
共同终点及43,290项numeric board目标统计全部相同。新伴随表仅在ON存在。
两个独立进程的诊断JSON仍有host指针不同：macroop_identity直接来自macroop
指针，logical_request_id来自Packet指针。首次逐JSON完全相等检查的8项失败
原样保留；源级确认后仅单列这两个特定诊断路径，其余目标字段全部一致。
不能把该结论扩大成这些host指针跨进程相等或硬件PMU认证。

ON的580,038个适用span字节和实际路径均完整，31个ROM pseudo明确不适用，
Unknown/leaf-only均为0；一个退休尾部partial span单独保留。6个跨上下文span
恰是原故障的三个内核PC在两核出现，均保有完整来源：core1取指/解码root
0x7cc08000、core3为0x1e0a000，退休root均为0x7ada2000。实际观察因此确认
退休等同检查丢失了有效来源；不能将历史v1的6条Unknown离线补写。
57,942个历史walk_root span另列，不等同于origin/decode/retirement差异。
逻辑MMU控制、原生delay/remap/replay覆盖、完整前端/FS和四案新模型评分仍未通过。

精确收据：instruction-input-source-freeze-v2.json、instruction-checkpoint-pair-v2.json、
instruction-observer-neutrality-v2.json（原始严格差异）、
instruction-observer-neutrality-v2-qualified.json（目标语义及完整指针差异）、
instruction-input-context-witness-v2.json；OFF/ON均经维护prepare入口资格化，
functional_input_ready与target_identity_valid均为true，独立native task-clock可用。
保存检查点的预ROI输出只有空header且清理阶段报告无ASID表，不是有效采集窗口，
从未用于评分；两个恢复后的完整窗口独立通过来源/时钟检查。
