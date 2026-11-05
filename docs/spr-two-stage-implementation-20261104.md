# SPR 两阶段机制对齐实施与最终验证

用户已授权按[机制设计](spr-two-stage-mechanism-alignment-design-20261104.md)和
[实施路线](superpowers/plans/2026-11-04-spr-two-stage-mechanism-alignment.md)开始实施。
本文先于本轮代码修改落盘，持续区分目标、已实现、已接入、已验证和未覆盖项。

当前执行优先级已收敛为[完整 FS 回放与连续计时](superpowers/specs/2026-11-04-spr-fs-execution-bridge-design.md)：当前审查缺陷已关闭；预热/线程归属/任务计时的F1运行时片已通过构造真实入口验收，首案固定 FLAC C4。新的采集协议和其他机制扩展暂不启动，尚无完整新模型四案 CPI/吞吐结论。

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
- [ ] P2 native store、online Sequencer与资源账：单核统一/SA-SD、延迟publication、mixed R/W与私有层WB已接入并审查；early prepare冷窗口子域经修复审查，完整FS、多核写及prepare的替换/升级仍待实施；A+B普通冷数据多核权限/ACK子域已接入并通过独立修复复审；完整FS、替换和early-prepare交互仍未完成。
- [ ] P3分区IQ/FU/bank：静态整数/zero及经完整preset认证的普通FP/SIMD、FloatReg forwarding子域已接入并审查；融合、rename free-list与完整frontend组合未资格化。
- [ ] P4前端/翻译：instruction-v2、data-origin-v3、core-facts-v1输入扩展已通过分项原生检查点；逻辑MMU controls-v1已通过采集对照，PageFault命令仍有未绑定缺口；架构事件边界、walker状态及完整frontend/rename runtime仍待接入。
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

### 下一功能输入：逻辑 MMU 控制及完整性

`.ixlat`只证明字节来源；`.asid`会省略同root重写，不能据此重建逻辑TLB
失效。下一步新增默认关闭的版本化控制伴随，保存完整初始CR0/CR3/CR4/EFER
功能状态、实际控制写的旧/新值以及INVLPG的VA/ASN操作数，绑定完整退休流
ordinal/hash与策略。记录逻辑命令，不能记录被清空的原生条目、命中、walk结果
或命令发生的native cycle。相同CR3重写也必须保留。

原生DynInst::updateMiscRegs在commit应用暂存值，INVLPG从DynInst::demapPage
进入CPU/MMU；这里可以建立指令所有权，再由通用MMU入口检测逻辑flush/demap。
将原生层级传播产生的重复回调与单个逻辑命令区分。正常控制写还要保持当前
源码的语义：CR3无条件flushNonGlobal，CR0.PG和CR4.PAE/PSE/PGE翻转flushAll；
不擅自用另一个硬件版本的规则替换。初始状态必须对应首个保留成员之前的
功能状态，首成员自身改变控制寄存器时不能误取其事后状态。

外部CPU/ThreadContext发起的flush、被squash/放弃指令的不可绑定效果不能在
回调时随意归到当前退休ordinal。必须有可验证的功能因果锚或明确计为未绑定
控制，使该窗口控制资格失败；普通退休操作仍可保留，不伪造顺序。旧输入
继续显示控制不可用；新控制表也不自动证明旧checkpoint初始TLB状态完整。
先验证同root写、不同root写、CR0/CR4变化、INVLPG操作数、重复层级回调、
外部/被放弃owner及首成员控制写，再跑新二进制同检查点OFF/ON完整性和中性。

后续源审计确认两项必须先落实的细节。第一，commit.cc的ppCommit通知在
updateMiscRegs之前；不能在当前退休通知回调中宣称已获得该成员最终控制效果。
应先绑定退休owner/ordinal，再在原有update之后用观察钩子封口，不移动原生
更新或截止点；最后一个控制成员的伴随footer也必须等待该owner元数据封口。
非serializing misc写还有立即应用及commit重复应用路径，不能按opcode假设一次。

第二，数据`.xlat`也有同一来源错误：原生writer.valid以退休ASID判定来源，
不相等即写mapping ID0；parent reader还禁止来源与退休不同或混合分片来源。
这会丢失已观察到的真实数据翻译。下一必要输入子任务先版本化保存数据请求
来源ASID/历史walk_root，与既有退休.asid分别绑定，保留旧版严格语义和Unknown；
再接逻辑控制表。现有四案的普通请求/static-UOP资格没有被取消，但不能扩展
成数据页表路径完整。已写成0的历史映射无法从旧输入恢复，不做离线补写。
控制逻辑去重使用明确的作用域命令身份/传播祖先，不能用参数相同或root未变
去重，因为一次逻辑命令会经MMU集合与next-level传播多次触达共享TLB。

### early prepare 审查与控制器预算

首次组合回归虽然通过，独立审查指出原生ResourceStall保留optional队首，
成功响应还会在同cycle重回首端口；修复前的年轻hint绕过及+1唤醒不等价。
已增加两个先RED的真实Simulator反例。还要区分I/no-fill的recycle：它不是
成功trigger，不自行重启首端口；Load在SP_IE的stall_and_wait则是成功转换，
消耗一次控制器预算并停放该消息，不能当成零成本resource failure反复尝试。
这些路径及多个同cycle通知目前正在修复复审，不以此前GREEN替代新证据。

新增预算字段spr.memory.l0_transitions_per_cycle从实际L0对象导入，四案按
各自冻结源码/配置重新导入均为32，策略SHA逐案匹配，commitToIEWDelay均为1；
收据qualified-target-profiles-early-prepare/summary.json。不把32写成模型魔数。
当前子域在超出已导入转换预算前失败整个窗口，尚未声称实现原生超预算延后；
这与支持的optional/mandatory/response顺序分别验收。

上述optional队列及预算修复已完成两轮独立复审：原R1与后续R2/R3均已关闭，
最新范围内无Important/Critical问题。完整构建、默认及20组C++机制目标、
100项Python来源/导入测试通过。新实际路径测试涵盖：地址/prepare早于data和
commit；hint16与ordinary16独立、17th drop消耗发送端口；同line多hint共用一次
物理服务；store takeover只归还hint信用；Load-on-SP_IE停放/唤醒只算一次
stall转换；resource head保留；完整响应后的同cycle optional优先；recycle结束
本次端口访问；跨chunk1/2/7/128及最后退休后尾部资源守恒。

冻结FastSim二进制SHA256为
a271e800fa2de015b91902d48e5f21adc6bc498a1ac2273de04f9f1a6fe189ae，
收据checkpoint-p2-early.json含源文件hash、构建/回归及逐案导入证据。本子域
显式开关core.causal_early_store_prepare默认关闭，要求完整静态策略身份与已
导入L0预算；private replacement/invalidation、共享S升级、超预算延后和完整
FS/warmup仍未支持，不以该GREEN报告四案CPI或吞吐效果。

### 后续核心功能字段合并设计（先审计，未实现）

前端/rename审计发现，现有`.ffe`的shared-allocation位把静态microfused-memory
和stack-register allocation合并了；后者仍有独立后端/IQ成员，不能都算成一个
IQ/ROB项。code_identity是decoder context和bytes的联合hash，不能由不同宏的
hash恢复原始上下文。现有.uops源operand只含class/producer距离，没有有序的
架构寄存器身份及目标pin信息；宏级128位mask又去除了临时寄存器、合并了CC/
XMM lanes及重复操作数，不能据此还原逐UOP rename/move语义。原生writer还未
填充.imap已经定义的read/write barrier及locked-RMW位，不能按“reader支持”
宣布采集完整。

下一合并功能扩展只补纯解码/架构事实：独立原始decoder context与明确Known；
已绑定的frontend ASID/fromUser；分开的静态microfused/stack分组种类；有序的
逐UOP src/dest架构regclass/index/lane/invalid/misc/temp身份和静态pin-write计数；
静态GPR/XMM move种类；方向明确的serialize-before/after、read/write barrier、
non-spec/control属性。物理寄存器、原生eliminatedMove/fused group、rename grant
和时刻仍禁止。动态ISA flatten若需要独立功能状态，必须明确来源而非在退休
用后来的状态重算。字段拟放独立版本化core-facts伴随，绑定既有宏span和热流/
策略，复用现有.uops/.ffe，不连锁改写已经验证的旧协议；旧输入保留原能力。

原生macro fusion只检查其实际要求的fetched ASID/fromUser等条件，不擅自增加
“全部decoder bits相同”这种更强门控；普通稳定上下文已可用的退休模式事实
保留。完整frontend source batching/IDQ/rename反压主要缺模型机制，不以缺字段
为由采集native来源或grant答案。PRF容量与前端参数另从原始实际config导入，
已有完整config的输入不需为了参数再采集。待这组功能字段和MMU输入一起资格化
后再决定下一次四案完整采集，避免反复生成不完整输入。

### 新探针暴露的原生诊断收尾缺口：PIO

data-origin-v3首个OFF探针在共同目标后等待context1/seq49188：串口COM1
状态寄存器load已经executed/completed，但native账本的Ruby admission/response、
issuanceClosed及terminal均为0。只停止了该冻结OFF进程，保留失败窗口和日志；
未启动ON、未准备成有效评分来源。新的数据origin语义在OFF未开启，但该事实
本身不能替代根因证据。

源审计确认一条匹配路径：预热pretracking只靠Ruby admission建立registry项；
PIO经RubyPort直接送device，没有此admission。LSQ在Request扩展保存closed，
退休导入却要求非零Ruby response或已证实terminal，于是生成空committed项。
同检查点调试将先核查这条具体身份在route/close/response/measurement/import的
字段；不能在退休用“completed”或PIO地址推定No-Ruby，也不能提高poll上限。

拟修复仅原生诊断观察：在实际PIO接受/设备响应处记独立bypass fragment事实，
不增加Ruby访问、命中、miss、fill或DRAM计数。完整性必须要求main issuance
closed且全部已接受Ruby及bypass片段都完成；单个PIO片段不能使混合RAM/PIO
请求提前terminal。确证全PIO后才产生独立device-bypass原因；跨预热边界的
Request事实按同一身份导入一次，不能把已观察部分再相加。同步native reason/
summary和维护审计器，PIO的retired-cache membership继续Unknown。先用实际
生产registry/extension RED→GREEN覆盖两种完成顺序、重试、split/mixed、退休后
响应及空closed反例，再在原失败检查点做修复后OFF/ON；不改SPR执行或IO时序。

### 数据翻译 v3 与 PIO 调试检查点

数据origin扩展采用已有FSTXLT1的format3（此前已是format2），默认关闭的
functional-translation-origins只改变观察/序列化。64-byte头绑定非零编译策略；
每个绑定显式保存退休ASID，字典保留实际来源/历史walk_root。非零token仍属于
原有retirement-ASID/VA4K/PA4K命名空间，换退休namespace必须新token，不能以
来源上下文替换它。同namespace token可保留不同实际来源；tokenless/direct
ASID0明确比较，旧full-source asmap不支持0的边界仍拒绝。旧format1/2保持原语义。

独立审查发现header bit0清除并缺.vmap时可跳过token验证，现已修正：构造阶段
完整预验证对每个实际非零token要求声明和独立map，包括Unknown；真实slice
反例也会拒绝。修复复审通过，完整构建、默认及21组C++检查和136项Python回归
通过。对应child提交afcbd5641f，source/native header策略为
19a89dd9fae02396aedebf21622731064d735d8ed1d60600c49a419418dfeb87。
冻结gem5二进制SHA256为3548eadba1c4475248dca8660503944a040e8cc407f2f632acf728635106fa8f；
FastSim为2ce3f8fa34d2f3a49d6bd41c6d3a12f0ec9bb891ed9249343c8538402bf1ec3f。
这些证明传输/异常输入合同，不能替代被PIO阻塞的原生OFF/ON整窗验证。

PIO根因已由未改动的冻结binary/原失败checkpoint的GDB a3见证确认。筛选ctx1/
seq49188且PC/PA精确匹配，同一Request0x2c20e240：tick4790995545490进入PIO时
enabled0/pretracking4且无response扩展；4790995555885打开测量；4790995647900
设备响应及4790995649055退休导入前均有closed=true、Ruby counts0、terminal=false。
这精确满足旧导入拒绝条件。调试在该点主动停止，只是oracle诊断前缀，从未评分。
原失败OFF保持不完整，ON未运行；GDB a1/a2的不完整诊断和失败原因也均保留。

收据checkpoint-data-origins-v3.json、data-origins-source-freeze-v3.json、
data-origins-v3-incomplete-uart-drain.json及native-uart-gdb-witness-v3-a3.json/log
分别绑定候选、源/binary、失败及具体身份见证。PIO修复后必须重做原失败状态的
完整OFF/ON来源、原生诊断守恒与独立目标身份验证，不将旧失败离线改成通过。


### 后续多核一致性生产接入：控制器状态与信用分离

只读原生协议审计确认：物理L1D/private L2/LLC分别对应L0/L1/L2；实际
MESI_Three_Level.slicc引入的是MESI_Two_Level-L2cache和dir，不能按文件名猜测。
当前运行配置每个L0/L1转换预算32、每LLC bank预算4；TBE256与L1D fill16、
private-L2 fill48是不同资源。ordinary16与prepare16也必须分别记账。LLC
SS_MB/MT_MB/MT_IIB阻塞不额外分配TBE，miss在Mem_Data归还TBE后仍等待unblock；
不能把所有阻塞期都当成一个MSHR租约。

下一系列按生产依赖推进：先给现有cache存储旁增加持久permission/transient
状态、类型化消息和原子transition；再接GETS/GETX/upgrade、owner forwarding、
ACK与unblock；随后处理private replacement及child inclusion、LLC recall与
directory/DRAM frontend ACK；最后扩展early prepare的S升级、invalidation和
replacement。每片都有实际Simulator输入到回调的RED→GREEN与独立审查，
组件孤立测试不作为删除生产拒绝门的依据。

状态归属为(controller,line,generation)，不同于退休owner/core/fragment、
Sequencer服务和hint identity。每普通owner有独立credit/callback；共享物理
事务只持一份实际fill/TBE。S升级只用TBE，不消耗缺失line fill；E到M本地转移
无需GETX。LLC的MT必须向真实owner取当前数据，不能因tag存在当成LLC hit。
ACK按发送者和generation去重，数据与unblock可交换到达但必须全部满足。

多核开放前必须将当前全局prepare_lines及L0预算改为controller域，并替换
sole-producer store-drain的发布证明。新消息源纳入MemoryRuntime安全frontier，
保持同cycle合法原子续处理及真实端口排名；超预算延至实际next-cycle事件，
不throw代替native延后、不全局扫描/固定点/任意+1修补。不得同时运行旧即时
invalidation directory和新协议两个真相源。数据only资格不证明Icache inclusion。

DRAM接入现有native_frontend receipt接口，逻辑WB接受ACK与物理服务完成分离：
两个merged逻辑WB仍独立ACK，物理WQ一份；WQ-forwarded read不能套旧正向DRAM
最短返回下界。LLC replacement TBE在原生Mem_Ack释放，后台物理写尾部不增加
已经回放结束core的CPI。原生Directory CPU路径不借DMA TBE预算。

功能输入边界保持原合同：现有.sreq/.xlat已有mask/flags/分片PA，拒绝动态翻译
的runtime门主要是adapter缺口，不泛称没有输入；atomic结果/HTM/security/设备
和逻辑MMU等尚未支持域继续拒绝整个窗口。绝不借用原生cache命中/ready/issue/
response时刻来补模型状态，也不将该设计或结构探针声称为四案精度/吞吐通过。


### core-facts v1 功能协议冻结与观察中性约束

下一输入片采用独立feature16/.core.gz v1：96-byte头绑定策略、x86 decoder/
register layout和真实ISA架构class sizes；每条hot成员128-byte row，后接有序
24-byte operand；80-byte宏关闭绑定精确匹配既有.ffe，避免为长REP缓冲整宏。
footer绑定完整hot/宏/payload数与hash。完整源流式预验证后重开回放；slice外
损坏也拒绝，Unknown分组独立统计，不因此认证完整SPR。既有.ffe/.uops/.ixlat/
.xlat及hot wire不改。额外验证pass的成本纳入最终端到端吞吐，不称为零开销。

flatten事实在原有rename规范化点、物理映射前采样，保留original-is-flat、
原始及flat class/index、重复/临时/lane和静态pin计数；不在退休时重做flatten。
frontend新观察字段与原有addressSpace/fromUser隔离，绝不为采集打开
markerObservation或改变用于原生fusion/admission的字段。静态fusion候选用
新增纯观察accessor并逐项核对原predicate；原canMacroFuseWith执行体保持原样，
不因减少代码重复而改造SPR行为。decoder context Known依赖真实byte witness，
不能以数值非零或残留PC字段冒充有效性。

PRF及前端参数来自真实SimObject/config；架构class sizes来自native ISA写入
新头并在prepare中验证合并。物理容量不能直接等于free-list容量，缺乏架构
layout证据仍Unknown。完整方案及细粒度测试见工作账本core-facts brief/report；
源码/生成header依赖将增加rename和所有新transport/codec/layout来源。


### PIO 修复与 data-origin-v3 原失败检查点验证

PIO观察修复已在child18002f6cbb/yinhaolang独立提交，原SPR执行结构、队列/
端口时序和统计截止规则不改。新增v8 oracle独立bypass接受/响应/错误人口；
全PIO仅在main issuance closed且全部服务完成后给DeviceBypassComplete，混合
RAM/PIO不提前NoRuby，retired-cache仍Unknown。部分split被squash的清理问题
经一轮独立复审修正：废弃身份等已接受Ruby/bypass及尾部L1回调完成后回收，
不放宽正常退休请求的resolved。30个生产生命周期场景、55项相关Python测试、
完整父构建及默认测试通过，原有5个perf-retirement测试失败已在efdafe1隔离
基线逐项复现，未改其ownership门禁。

维护入口以153jobs重建成功，冻结gem5-pio-ledger-v2.opt的SHA256为
4e201073412fb0437e948ec89fac960df535fdbb5fbd1aeb8fcafd1518dee7c8；
策略b21b33c25cbcabef3e9909aa3c7e498f650de9825d9b72d1601fe7496da7f580。
继续使用原失败KVM检查点的全部payload，显式记录“仅观察源码delta兼容”，
不冒称与旧3548eadb二进制相同。OFF用只读GDB观察确切ctx1/seq49188/PC/PA，
随后继续完整窗口；ON用同一新binary，仅开启functional-translation-origins。

该Request的上游/响应/退休tick分别仍为4790995545490/4790995647900/
4790995649055。退休导入前bypassAccepted=1、bypassResponses=1、errors=0、
closed=true、terminalReasonMask=512，Ruby admission/response仍0。两组都完成
完整native drain（1 poll），共同截止4791203308815；每组886912records、
510202macro spans。四核bypass人口为25/20/14/28，共87，接受=响应、错误0、
pending0、native hierarchy gap0。两组维护prepare/独立target identity及native
sideband审计通过，任务钟available；这些均不是硬件perf等价认证。

68项完整对照通过：hot头/records及syscall metadata、10种未改变的功能伴随、
原生层次/任务钟/共同截止和43265项数值board统计相同；.xlat的format2→3是
明确的序列化差异。保留所有跨进程host pointer差异，原始JSON身份不称为字节
一致。原失败输出header仍0且有未刷盘尾部，初始全文件相等检查失败被保留；
后续只确认其全部已写payload前缀与修复后相同、截止tick相同，不把旧失败提升
为完整人口、周期或评分来源。

ON的data v3验证183087memory records/1079869bytes，字节mapping全部有来源，
Unknown0；historical walk-root记录16306，
本次origin/retirement跨上下文及mixed-origin激活0；跨context能力仍由构造
反例验证，不能宣称在该FS探针激活。path-known为182467records/1079095bytes；
余620records/774bytes已用生产reader逐项核查：598条IO(598B)、22条MSR(176B)，
全部非ordinary、kind非paged，没有发现普通内存请求PTE路径丢失。现有汇总把
这些特殊地址也归入leaf-only，actual_path_status保守显示incomplete；本记录
明确其类型，不伪造PTE或放宽资格门。逻辑MMU controls及完整模型仍未资格化。

证据：pio-ledger-source-freeze-v2.json、pio-checkpoint-pair-v2.json、
pio-data-observer-neutrality-v2-qualified.json、pio-original-failure-prefix-v2-qualified.json、
checkpoint-native-pio-v2.json及tmp/native-pio-lifecycle中的prepare/native-audit/
data-leaf-origins-v2收据。首次对照脚本在ON准备完成前启动的缺文件错误另存，
准备结束后重跑68项通过；没有重采、改写原生事实或跳过失败成员。


### 后续逻辑 MMU 边界所有权：异常与中断

现有.fsem只能证明某保留成员属于hardware entry或PageFault后的不完整前缀，
没有唯一异常ID/vector/实际invoke因果关系；不能离线按PC、当前或下一ordinal
给PageFault::invoke的demap任意归属。实际四案native scope也表明这不是纯假设：
XZ/FLAC分别有526530/86998条page-fault memory UOP，四案均有IRQ人口。这些
计数只证明场景活跃，不能当模型时序或功能命令所有权。

后续单独桥接片设计：在原Commit已接受同步fault、CPU::trap之前建立纯功能
事件token，作用域覆盖原PageFault::invoke效果；在真实X86FaultBase ROM重定向
绑定token并随PC/FrontendRecord/DynInst不可变传到真正退休入口。前驱必须是
已经写入且post-update seal的保留成员hash/ordinal，允许精确fault发生于partial
宏之后；后继必须是同token的实际ROM入口，不能用最近event或PC相同猜测。
原faulting head未退休，不能造一条hot记录。IRQ用独立accepted-kind及前缀证明，
不能把初次pending poll当接受；不得序列化接受条件、native grant或时刻。

第一逻辑MMU片继续将未绑定/filtered/squashed/未seal效果置为不完整。后续桥接
仍需覆盖首UOP/fetch fault、重复同PC、撤销IRQ、延迟/squash ROM、嵌套/外部reset、
入口缺头尾及溢出；不能为得通过裁切人口或改共同截止。完整源尾部只读核查：
四案16个core没有pseudo尾段；XZ core0最后为2-UOP partial，FLAC core0为1582-UOP
partial REP/微码span，二者原样保留。这不是未来新窗口或功能token完备证明。
方案依据mmu-functional-boundary-owner-audit.md，尚无该桥接实现或资格结论。


### 后续 generic FU：参数资格与消费者 forwarding

新增只读审计确认，当前causal路径已有共享12端口日历、按OpClass的generic FU
ring及issue interval，不重造调度器。已接入的8种非managed FP/SIMD采用当前
源码preset，其端口/L/II/pipelining与实际C4活动sprFUPool相符；但JSON虽保存了
完整FU descriptor，C++尚未消费或认证完整generic表。不能仅扩大OpClass白名单：
未支持的SimdMult/SimdFloatMult/Pack/Extract在旧causal traits分支会继承1cycle，
目标分别是3/4/3/3，必须先精确导入或严格验证整个实际preset，再启用该子域。
这些类目前会拒绝，不能当成已有新模式的CPI误差测量。

随后补FloatAdd的FloatReg结果规则：native fast-adder wake对FloatAdd消费者
额外0cycle、其他消费者额外1cycle。现有generic RAW只等普通Complete，没有
该消费者区分。模型从功能dependency/寄存器class与自己的事件计算，不采集
native wake时刻。区分FU nominal finish、operand ready、writeback、backend
complete和非pipelined释放；保留已实现generic失败轮转与managed成功轮转。

下一生产片先认证/消费现有generic元数据而不扩输入域，再扩静态描述已足够的
普通FP/SIMD及规范化96/97/98，最后实现FloatAdd forwarding及FloatSqrt的p0
24cycle占用。均用真实BinaryTraceSource→Simulator验证端口、II、完成/退休、
legacy参数扰动中性和chunk1/2/7/128一致。分支虽有端口0/6，恢复/前沿适配
另做，不借FU表开放错路径重建。无需为这些FU参数/依赖额外重采FST；依据
为generic-fu-runtime-alignment-audit.md，当前未实施或宣称效果通过。


### core-facts 源码与真实 ISA 布局回归（v5，原生对照待完成）

默认关闭的 feature16/.core.gz 已接通采集、严格完整源 reader、prepare、身份
指纹与拒绝丢弃伴随的转换边界。新增的是纯功能事实；完整前端/融合/物理
rename 行为尚未接入，不能将采集能力称为模型对齐完成。原生 fusion predicate
执行体未改变，观察字段与其使用的上下文字段隔离。

独立复审先修正 original/flat 寄存器关系与 slice 外完整 RAW 投影校验；真实
原生启动又暴露两个 fixture 未覆盖的问题：SimObject AttrProxy 需要只读
unproxy(owner)，整数架构类应为39而非38。后者已用实际 ISA 向量和真实头文件
常量共同证实：39/48/1/2/1/0/6/205，其中整数类包括微码临时量及枚举间隙。
新编译断言直接绑定原生头文件，禁止通过修改 ISA 或虚构头部计数来过关。

修复经过独立 scoped review；v5 父构建、默认及23个C++测试目标、183项相关
Python测试通过。初次与链接同时运行时4项测试遇到工具 Permission denied；
保留该失败，待构建完成后同一测试集183/183通过，没有修改产品源码或断言。
两项已独立基线复现的旧 fixture setup/link 失败仍单独列出，未混称全仓测试通过。

v5 原生维护构建成功；冻结 binary SHA256 为
15eb4b3bc44bdc184ed799d33ac0d2987e0fda0a4034092c11134b15b6b9744b，
策略为1c3b49b6fe9a287a986ff957e02c773180545ac513abd0efdb197ae41ce9cc4c。
此前 v3 在 instantiate 前因 proxy 导出失败、v4 ON 在 writer 构造前因错误38
校验失败，均保留完整失败输出，不提升为可评分源。当前使用原始不可变 KVM
检查点进行 v5 OFF/ON；完成后必须维护 prepare、独立目标身份/任务钟和完整
人口/统计中性对照通过，才能登记该输入能力。

证据目录：tmp/core-functional-facts-native 与
tmp/spr-two-stage-implementation-20261104；当前没有新的四案 CPI/PMU/吞吐结果。


### A+B 拒绝语义澄清（实施前裁定）

不支持成员或替换在当前转换写入 tag/victim/状态/信用/消息之前检查，拒绝该
转换且整个运行不给部分分数。该约束不要求抹掉此前合法转换的内部效果；
失败实验的诊断仍保留。此前“整个窗口任何副作用之前拒绝”表述过强，现明确
采用整案失败、转换原子的语义，不增加复制 TraceSource/全足迹扫描接口。
完整源功能一致性校验及所有未支持域门禁保持；不通过截尾、回退或部分结果
提升为成功。


### core-facts v5 原生完整窗口对照通过

v5 原始检查点 OFF/ON 均正常完成，共同截止4791203308815、drain1poll，
各886912条hot记录/510202个macro spans。维护prepare、独立target identity、
原生任务钟与native response审计均通过；76项完整对照通过，包括11种未改变
的功能伴随、完整hot人口及43265项数值board统计。跨进程host pointer诊断
原始差异完整保留，仅源证实的指针字段不作目标身份相等要求。

ON四核ISA class sizes均为39/48/1/2/1/0/6/205。normalization完整886912/886912，
operands、pins、static、frontend同样完整。decoder/allocation/fusion各Known885064、
Unknown1848；生产reader与原始.ffe逐条交叉核查：四核Unknown728/448/392/280，
全部pseudo，nonpseudo Unknown0。不虚构pseudo decoder，strict_core_facts_complete
仍false，runtime_qualified/full_spr_profile_qualified仍false。

输入任务的源码/运输/观察中性门已通过；不代表完整前端/rename行为、四案精度
或吞吐通过。child本地yinhaolang提交8ccad4329c；父仓继续保留02119d1可获取
gitlink，并发布截至该child提交的完整portable patch。

精确证据：core-facts-source-freeze-v5.json、core-facts-checkpoint-pair-v5.json、
core-facts-observer-neutrality-v5-qualified.json、两个源内spr-capture-preparation.json、
tmp/core-functional-facts-native/native-qualification-v5.json及
core-unknown-populations-v5.json。失败v3/v4和并发链接期间测试错误均保留。


### A+B 网络边界（实施前裁定）

此片允许沿用显式标记的每链路模型NoC时延近似，先接通源绑定的控制器端口
访问顺序、队列顺序/rank及逐控制器预算。仍导入实际geometry/route/queue事实，
不支持的随机化/异构拓扑拒绝。PerfectSwitch/Throttle的精确仲裁与带宽留给后续
network/service片；当前不能声称完整Ruby网络、原生时序相等或完整SPR资格。
该近似必须进入能力/输出报告，不能只藏在实现文档。


### core-facts 父程序历史四案兼容回归

8b60feb冻结父程序SHA256为b043192a977fbd358c2823c31db6fd994e0ee18898cc1f0b832b1dc4238c813c。
串行复用此前checkpoint-off-compatibility的四案manifest/config，四个完整回放
均成功；逐核预测、time_accounting及scope_metrics（剔除host throughput）均
与该历史基线一致，4/4通过。这是旧OFF路径兼容队列，不混称新paged四案队列
或新机制精度/吞吐效果。

首次比较helper误读不存在的scope键，在四个回放完成后报错；保留原日志/收据，
修正比较器读取实际scope_metrics，仅比较已有完整输出，未重放或改写结果。
权威兼容收据为core-facts-off-compatibility-v5/verification.json。


### 后续融合的 marker 上下文政策（实施前补充）

源核查确认，原生fusion比较的是原FrontendRecord的addressSpace/fromUser，
仅markerObservation开启时才写入；该开关只由CalibrationMarker/FixedWorkMarker
构造开启。新core观察字段记录实际上下文，与原字段刻意隔离。v5四核配置的
marker_pc=0、fixed_work_source_core=-1，按源码其原字段仍为0/false；未作运行期
bool转储，不称为GDB字段证实。后续模型必须导入/严格认证该source/config政策，
不能直接把观察到的ASID/fromUser不等变成新增融合屏障；active/inactive各自做
实际Simulator反例。也不增加完整decoder-context相等条件、不采原生fusion结果，
不为采集打开markerObservation。详见frontend-marker-context-policy-audit.md。


### 完整 FS 接入的生产切片（实施前设计）

只读审计以8b60feb为稳定基线，实际代码不是独立Scheduler类，而是Simulator
time_epoch、FunctionalSyncPlan、RecordedScheduler和TaskClock。四个资格化旧源
已有物理核流、warmup cuts、线程状态计划和独立native任务钟；高层functional
sync均not-requested，不能虚构OpenMP barrier/lock DAG。未知wake cause与未知
owner不同，保持命名和资格边界。

按以下片接入，不能直接删除native_kernel/warmup/plan门禁：

1. 区分TraceSource的Ready/WarmupBoundary/TrueEnd。当前causal把next=false当
   EOF，而实际warmup source在切点暂停。新adapter保留全源ordinal、部分宏、
   SA/SD与RAW、在途SQ/ROB/IQ/权限/缓存/翻译事件；不调用旧全局drained seam，
   不增加人工barrier、清空或重放。统计切点与native原生common-start参考是否
   严格同口径需单独裁定；零warmup和partial也不能偷用native tick对时。
2. 将功能边界observe(corrected retire)/release接入稀疏前沿；owner在admission
   改变，account-out/in在该成员模型退休点记账。blocked逻辑任务不停止物理
   内核流，wake也不分配CPU。最终行需已退休且必要边界已释放，之后逐核冻结
   四类任务钟；后台store/protocol仍可drain，不能加到已结束核的CPI。
3. 精确接入kernel/raw syscall、硬件入口/abandonment、方向性serial/barrier及
   命名架构事件，不能重复合成syscall/IRQ服务或按下一PC猜PageFault归属。
4. 用新core/translation/MMU输入接完整frontend/rename/walker与多核memory，
   每类producer必须发布边界；未支持的实际请求类型逐类报告。
5. 完成功能束资格后才采新四案，保留旧源并冻结新源/二进制/原生参考；完整
   窗口资格后再作维护validator的CPI/PMU和独立吞吐实验。

旧资格化四案共16个.xlat均v2，没有.ixlat/.core/拟议.mmuctl；不是新模型完整
输入。参数如FU/PRF可从现存配置导入，不能因此泛称必须全部重采。无条件locked
RMW已有request flags/角色事实，主要缺runtime；LL/SC或真正conditional request
才需独立功能结果证据。当前x86 CMPXCHG lowering在比较失败时仍写回旧值，
不能因缺success位就删掉其write，也不能把kConditional控制流位当比较结果。
是否需要新增predicate字段应按真实producer和实际激活域验证。旧四案native
summary只用于诊断：当前没有报告predicated_off/failed_store_conditional人口；
不将该诊断当模型输入或未来捕获完整性证明。

设计与真实Simulator RED矩阵详见full-fs-causal-bridge-audit.md；当前未实施
此FS桥接，也没有新的四案causal精度或吞吐结论。


### 连续预热计分起点：保留原参考，新增命名视图

原生traceMeasurementBegin在一个外部指令停止/串口事件同时打开所有核，
现有可移植metadata只有各核prefix计数，没有可自主建模的global-BEGIN因果
owner。原audit明确要求roi_begin.tick=common_start_tick。故“各核最后预热
退休”不能直接冒充原common-start参考，即使记录人口相同。

分析反例：全局BEGIN=100，A的最后预热/首测量/最终退休=60/120/200，B为
90/130/220。完美模型用局部起点会得到140/130 active cycles，原参考却为
100/120；差异40/10只来自定义，不是模型时序误差。更一般模型可能让A测量
退休早于B预热结束，根本不存在同时分隔所有源的global cut；不可人为等齐。

本轮保留原common-start、common elapsed和原始所有reference命名/结果。先
完成连续执行；口径不同的严格比较标measurement-start-contract-mismatch。
另设计独立source-retirement-start-v1 oracle-only视图，作为同功能窗口的
独立实验系列，不能替换、混合或重命名历史参考，也不把参考改变称为模型精度
提升。model独立计算同一功能前驱的退休切点，不读取native时刻。

非零prefix在原BEGIN之前保存已有O(1)rolling-retirement anchor，绑定N/N−1、
hot hash/source generation、counter epoch及after-record生命周期序列；新的
原生view从该锚到原retirement_stop。必须审计BEGIN之前的active/other/
transition/unknown及Sleep/Wakeup尾段，不能把raw差值盲目全算active，也不能
只裁现有BEGIN之后的intervals。未seal的同cycle锚需无延迟地补元数据或拒绝。
零prefix没有前驱，必须另证pre-first-source epoch；缺少则该view不可评分。

后续独立任务先做真实native OFF/ON和原参考中性检查，再接model/维护validator
显式active_start_policy匹配；同时保留原active_end_policy、完整population及
逐核退休冻结。新视图尚未实现或通过评分资格。完整证据/后续RED清单见
warmup-cut-reference-alignment-audit.md；原common elapsed不能由局部duration
的max/sum代替。


### Marian 等 NT 指令的目标语义

当前child8cc的MOVNTDQ/MOVNTPS/MOVNTPD源码明确采用cacheable fallback：
cda地址检查加两条8B stfp，忽略硬件NT hint。后续接入按实际request flags、
角色和该lowering判定，不能只凭MOVNT mnemonic增设绕cache写通路。该结论
来自目标源码，不代表真机SPR的NT行为，也不改变gem5-SPR实现。


### A+B 普通多核权限与信用：生产子域通过

默认关闭的core.causal_native_coherence已接入真实Simulator、现有private/shared
cache、OnlineSequencer、StoreBuffer、ProtocolCredits和共享DRAM；没有另建
cache/directory/DRAM真相或固定点重放。单核“tag hit即可写”的条件已在新子域
替换为原生权限/事务状态：E→M本地写、S升级、dirty owner转交、独立sender
ACK/data/unblock义务及generation校验。私有TBE/fill在数据及最后ACK完成时
先于L0投递释放；LLC miss TBE在Mem_Data释放，blocked/unblock不另占TBE；
Directory CPU路径不借DMA信用。各core/store及协议calendar都发布前沿，尾部
drain不延长完成核的退休计时。

范围严格限定cold ordinary CPU data、无private/LLC replacement、runtime与
target early-prepare均OFF。完整FS/warmup/context/translation、instruction
coherence、特殊/atomic/security/HTM/split、替换/回写等继续拒绝。不能把这里的
通过解释为现有四个FS负载已可切换到新完整模型。

独立初审发现两项P2并经fix1复审通过：最终ACK曾将control消息dirty=false
覆盖physical private dirty；现在DATA到达更新reserved tag内的数据属性，
ACK收尾只完成已有数据/权限义务，保持reservation不可见、victim保护与信用
生命周期。第二项是strict importer漏查links/routers；现在严格绑定同质完整
有向mesh、一router/endpoint、等clock及统一link/buffer合同。实际图34routers、
34ext、1122int，允许映射2个在当前模型子域内不产生请求的DMA端点；这不证明原生窗口无DMA。4种实际图异构修改均拒绝。正常profile
与CLI明确该拓扑限制，其他拓扑未资格化。下游NoC仍是公开的modeled-links
approximation，PerfectSwitch/Throttle/原生contention留待后续，不能称为完整
Ruby network等价。L0/private-L2直连MessageBuffer不增加网络hop。

两轮root源码冻结检查均完成。最终维护主构建-j16成功，24/24 C++测试目标、
195项相关Python回归通过；仍将两个既有fixture基线失败单独列出，未弱化
门禁。真实Simulator覆盖竞争GETX、共享读/follower、dirty owner、三sharer
升级、IS_I、SM_IL0、两种data/unblock顺序、typed容量/逐控制器预算、batch
1/2/7/128与host延迟、late unsupported与三层victim拒绝。非法packet以及当前
近似transport不产生的baseline-last为明确标注的同生产dispatcher+real-cache
补充用例，不冒称全Simulator。

冻结最终父程序SHA256为6d131adbb8f33ae677330aeb85211480f9784478b8f832b860ae4e1e125465c5。
串行复用CFv5的四个历史完整OFF窗口，cores、time_accounting与剔除host throughput
的scope_metrics均一致，4/4通过。该兼容队列不混称新paged四案或新模型精度。
当前没有新的四案causal CPI/PMU/吞吐结果。

证据：tmp/native-coherence-canonical/review-fix1-source/source-freeze.json、
checks-complete-v2.json、cpp-tests-v2.json、python-tests-v2.json、
off-four-compatibility-v2/receipt.json，以及工作账本A+B原review/fix1-review、
tmp/build-p2-coherence/fix1的RED/GREEN/actual-graph/CLI收据。协议源码摘要
416879c7299a74319a6fec544738ce8167a0d605005e570a1c3247ad108eb28e；实际图摘要
5a46f14020422b5d3bb4fc504d04654f3a39bd75b9a1c90fff1e0de8610bdfb4。

### 取指、walker 与 data 的共享资源（P4 实施前纠正）

同一实际配置中，IFETCH、CPU data、ITB/DTB walker RAM packet共用每核CPU
RubySequencer ordinary16，prepare另16。所谓private-L2 Sequencer64不存在，
64属于两个DMASequencer。O3 switch对象缺直接port键是pre-switch转储：
start CPU的I/D/walker ports在CPU/MMU takeover后交给对应O3，不能按缺键
判采集丢失，也不能按sequencer名称模糊匹配。

IFETCH不占Dcache fill16，但占共享L0 TBE256；Icache fill_capacity0是无有限
fill cap。walker RAM请求走L1D；所有来源竞争private-L2统一fill48/TBE256。
I/D walker并发分别1/4，STLB构造的walker没有接port，不应凭对象存在增加
服务槽。IFETCH与walker直接走port，不直接消耗LSQ load3/bank2；walker PTE
WriteReq也不进入CPU committed-store-buffer。IFETCH入mandatory为1cycle，
普通非IFETCH加Dcache.dataLatency−1，此配置为3cycle。

I/D数组同PA不能同时resident，未来P4须处理真实替换/dirty转交；.ixlat字节
证据不能强制每宏发IFETCH，DSB选中可能在翻译后免Icache packet。PTE path
地址也不能证明A/D更新及PTE写覆盖。详细源码/接入义务及真实Simulator RED
矩阵见instruction-and-walker-routing-audit.md；本节是源审计，不是P4已实施。


### 下一输入缺口：PTE 原始状态与软件写值

只读源码核查证实现有.xlat/.ixlat保存路径地址、leaf几何、ASID/root/PCID与
global等资格，但没有present PTE原始字，也没有.sreq软件store写入bytes。
旧.vmap及初始/measurement JSON保留presence和部分nonpresent raw leaves，
不能冒充所有root、kernel/upper-level的初始物理PTE内存镜像。MMUcontrols
的CR基线/flush命令不会自动补齐这些值。完整四案捕获仍等待必要功能束闭合。

反例：相同root/PTE地址/PA/页大小下，非terminal PML4的A=0/1会产生不同
walker写请求，现有mapping完全相同，故不能只补timing adapter。新的功能
合同需先证明相关物理PTE字节的初态/epoch、软件mutation bytes/masks/owner、
新表创建/复用与多root alias闭包，以及外部writer的排除或功能所有权。
stage1可以用纯功能路径地址并集建立PTE page inventory；这不代替初态和
mutation闭包，也不把native write-needed/hit/时刻喂给模型。不能每次walk前
注入native响应中的PTE值，覆盖模型自己的A更新。

目标源码本身还有限定：被冻结walker只有非terminal continuation分支才发
A更新write，未发现该walker的D更新；terminal/fault分支不能按理想x86规则
凭空补写。后续按源策略建模，保留软件/原始D位，不修改gem5-SPR行为。
完整CMPXCHG实际store bytes可用于内存mutation，不能把它一概视为需要额外
success位；真正conditional request、结果一致性及atomic ordering另验。

这是已确认的输入资格缺口和后续合同设计，当前尚未实现该扩展，也未因此
启动新采集。详细证据与边界在pte-functional-state-coverage-audit.md；其
source/model一致性、bounded页面/entry/mutation预算仍需实施前冻结。


### logical-MMU controls v1：实际检查点对照通过，PageFault 缺口保留

在[先行协议设计](superpowers/specs/2026-11-04-spr-logical-mmu-controls-design.md)
之后实现默认关闭的 `--functional-mmu-controls`，新增 feature17 / `.mmuctl.gz`。
它保存首个保留成员之前的 CR0/CR3/CR4/EFER、实际控制写前后值和逻辑
flush/demap 操作数；指令所有权在成功写入热记录后绑定，在原 updateMiscRegs
之后封口。原生调用、SPR 结构、原有 stop 和计分定义保持原顺序。
传播边使用一次性显式许可，保留每次实际 TLB 调用；不按参数或 root 去重。

初审发现的三个问题均已修复并通过独立定向复审：首个绑定前的未绑定效果
不能丢弃后误报完整；控制观察在原共同终点关闭，不能延伸到响应排空阶段；
终结后的回调和 RAII 析构不得重新进入已关闭 writer。强制终结、未封口、
效果交错、未知起点等保留明确失败状态，不合成控制所有权或完整 footer。

子仓库检查点 `7e121b8a2e89e651aabf0b9bb895111b383ab427`（本地 yinhaolang），
对应源策略 `88f74f8cc412fec93dcafc72b774f0250b14aa281bf158f3a29f91baf3e443bd`；
冻结原生二进制 SHA256 为
`ec5059fe79c48955d28755b260ff93239c446e50bb5bba6690b0e0217da3d5dd`。
父仓库 gitlink 仍为可获取的 02119d1，完整 child delta 随 portable patch 分发，
反向应用检查通过，没有向 child 原 origin 推送。

同一个已保存的 KVM handoff checkpoint 恢复 OFF/ON 两次，instruction-v2、
data-origin-v3、core-facts-v1 两侧同时启用，仅 MMU controls 开关不同：

| 检查 | 实际结果 |
|---|---|
| 完整热流与宏片段 | 两侧各 886,912 records / 510,202 macro spans |
| 原共同截止 / response drain | 两侧同为 tick 4791203308815 / 1 次 drain poll |
| 全部既有功能伴随（含 .core） | 逐文件解压内容一致 |
| 原生数值统计 | 43,265 / 43,265 项一致 |
| 原生任务钟、retirement/cache 观察及终点等语义检查 | 80 项通过；102 个进程局部指针差异逐项保留，不冒充目标差异 |
| MMU 文件及初态 | 4/4 结构有效、4/4 baseline 已知 |
| 封口覆盖 | 886,912 / 886,912，无 pending owner |
| 写操作 | 2 total = 2 retained + 0 unbound |
| 逻辑命令 | 8 total = 2 retained + 6 unbound PageFault |
| 传播访问 | 32，独立于逻辑命令计数 |
| 整窗控制资格 | 3/4 核 ready；整窗 controls_ready=false |

两侧均经维护 prepare、target identity、native sideband 审计；它们证明来源、
传输和观察中性，**不证明控制完整、初始 TLB 内容、walker 状态或模型精度**。
未绑定的六条 PageFault 命令正是下一架构边界片的实际输入缺口，其他 15 类
原因在这个窗口均为零，不能把它推广成所有负载覆盖。现有普通 FST 仍可读取。

正式父构建、26 组 C++ 和 207 项相关 Python 检查通过；两项已证实的历史
独立编译 fixture 缺少旧 reader/stub 依赖，仍单列，不修改其门禁来换通过。
原生维护构建退出 0。证据：`tmp/mmuctl-native/` 下 canonical/checks-v2、
native-build-v2、native-qualification-v2；本轮主目录下 mmuctl-source-freeze-v2、
mmuctl-checkpoint-pair-v2、mmuctl-observer-neutrality-v2-qualified 收据。
完整四案新采集继续等必要功能 bundle 就绪；尚无新模型四案 CPI 或吞吐结论。

历史四案完整输入 OFF 兼容回归也已 4/4 通过：全部 `cores`、`time_accounting`
和扣除 host throughput 后的 `scope_metrics` 与上一接受检查点一致，来源 metadata
哈希前后复核。冻结父二进制为 `f240d44a5e265dadb7302e2f5e8f9c8f06e61b0814a21fd185d157f7f63997c4`；
收据在 `tmp/mmuctl-native/off-four-compatibility-v2/receipt.json`。
这是旧预测兼容性检查，不是新时序模型的精度或吞吐测量。


### 下一功能 bundle：先冻结来源设计

已将[架构边界与物理 PTE 状态设计](superpowers/specs/2026-11-04-spr-functional-state-bundle-design.md)
落盘，仍未授权该片源码修改。明确分开 PageFault/IRQ 因果所有权、软件 store
bytes、物理内存初态与全部 writer 资格。初态选择现有 switch 结束、恢复模拟之前
的只读全 backing export，不增加 drain/checkpoint；完整磁盘初态配合有界驻留，
不能把已观察 PTE 路径并集当全部可达内存或把首次看到的值当初态。具体 wire
及 ROM 静态事实资格在对应代码 EDITSTART 前固定；当前唯一实现仍为 generic FU。

ROM 核查补充：旧 core-facts 中 1,848 条 pseudo 的 decoder/allocation/fusion
Unknown 不是缺少可补采的指令 bytes；hardware-entry ROM 没有 decoded root。
下一 bundle 将显式区分 Known/N/A/Unknown，并以独立已知的 ROM allocation
规则及生成器静态分支/Eret 语义作证，不能把 dummy context 或 size(4) 填成
真实 decoder facts，也不能仅把 Unknown 置零。现有 v1 保持原义，尚未实现 v2。


### 实际来源补充：Marian 有 DMA 活动证据

只读复核既有四案、各一个 stats section，并核对各自冻结 collector 在 measurement
开启处调用 stats.reset。Marian 的 DMA controller0 mandatoryQueue、requestToDir、
responseFromDir 分别报告 4,104 条消息；IDE 报告 dmaReadTxs=64、dmaReadBytes=262,144，
dmaWriteTxs/Bytes=0。其余三案对应 IDE 计数为零，DMA 队列没有消息计数字段。
这不是源域覆盖证明：统计本身不能唯一绑定请求发起/完成与原共同截止或跨窗口
在途人口；方向与源码分支继续单独核查，不能仅按 read/write 名称猜。

因此先前 A+B 的“inactive DMA”只表示该受限模型不生成 DMA 请求，不能推广为
四案原生 DMA 未激活。DMA 读也可能影响一致性/资源，不能仅通过“未修改 PTE”
排除该机制缺项。保留原始统计和来源哈希：
`tmp/spr-two-stage-implementation-20261104/four-case-dma-activity-source-inspection.json`。
这次没有回放、没有改预测或计分，也不据此归因任何 CPI 差值；完整域继续未资格化。

源码方向确认：IDE 的 WDCC_WRITEDMA 设置 dmaRead=true，因此这里 dmaRead
是 **RAM→设备（写盘）**，不是设备把数据写入 RAM。它不直接构成 PTE DMA
写入，但实际 MESI_Two_Level directory 的 DMA_READ 可对拥有者发失效/回写，
仍是内存机制缺项。队列 m_msg_count 在 dequeue 时增加，统计又延续到原有 detached
排空；这份汇总不能把 4,104 条消息全部定位到保留指令共同截止之前。后续需要
明确功能请求因果与独立窗口证据，不能拿该计数当外部请求的时序输入。


### P3 通用 FU：生产子域审查与正式回归通过

复用原有 12 端口 NativeIssueCalendar、每 OpClass GenericFuRing 和稀疏
continuation，不新增第二套 scheduler。维护 importer 对完整有序活动 FUList、
count/OpDesc、实际物理端口图和相关 native 源字节严格认证，输出
`spr.profile.generic_fu_preset=spr-generic-fu-v1`。这是 source-verified preset，
不是可任意导入的 FU 调度表。缺失/不匹配证书拒绝选用 causal_spr_resources；
此前 opt-in 配置需重新生成 profile，默认/OFF 保持旧路径。

新增明确枚举的普通 FP/SIMD、Bf16Cvt 和规范化 Pack/Extract/Insert 准入；
不按数值范围开放。26 种 generic canonical 类的端口、L/II/pipelining 经独立
源码/config 对照一致，legacy FP/SIMD/ALU 参数不能改变选定路径。关键规则：

| 机制 | 模型行为 |
|---|---|
| NoOp | 无 FU，L=1 |
| FloatDiv / SimdFloatDiv | p0，L/II=14/4 与 11/3，均流水 |
| SimdMult / SimdFloatMult | L=3 / 4 |
| Pack / Extract / Insert | p5 L3 / p0 L3 / p5 L1 |
| FloatAdd 的 FloatReg 结果 | 实际模型 writeback/wake 后，FloatAdd 消费者 +0，其余 FloatReg 消费者 +1 |
| 已退休 producer 的迟到消费者 | 仅保留尚未到期的一个 cycle 就绪边，不保存 ROI 历史 |
| FloatSqrt | p0 非流水占用24，与 completion/WB 及其他端口分开 |

新生产测试通过 BinaryTraceWriter→BinaryTraceSource→Simulator 激活，覆盖
上述 L/II/端口、独立与依赖流、WB 争用、producer 已退休后 source refill、
功能版本、sqrt 释放与后端完成分离、chunk1/2/7/128 和主机延迟完整调度元组
一致。分支、atomic、serialization、安全/HTM、完整物理 rename 等原资格门
保留；该片不开放完整 FS 或宣称四案新模型可运行。

独立任务审查 Spec compliant / Quality approved，无阻断发现。正式构建、
27 组 C++ 与 212 项相关 Python 全部通过。另将当前源绑定的 importer 应用于
四案既有实际 config.ini，4/4 得到同一严格 FU preset 和参与核0–3；这只是
参数/对象图检查，不会给旧 FST 换策略或升级其输入资格。收据在
`tmp/generic-fu-canonical/actual-four-static-profiles-v1/receipt.json`。
正式检查及冻结15文件源/二进制在 `tmp/generic-fu-canonical/`。

审查保留两项 minor：部分参数认证测试依赖本机未跟踪的采集 config，干净
checkout 会 skip，需要后续补便携 fixture；本次本机39项 profile 测试实际执行。
另有原已存在的 indentation/copy/conversion 及 LTO 构建诊断，不称全构建零警告。
这些不升级为效果、吞吐或硬件等价结论；本片没有原生源码/功能 wire 变更或采集。

历史四案完整输入 OFF 回归 4/4 通过：cores、time_accounting 及扣除 host
throughput 的 scope_metrics 与上一接受 MMU 检查点一致，输入 metadata 哈希
前后复核。候选冻结二进制 SHA256
`9dd82ccb9cadca8ae98134cbb847b096fd77751328a61641b497141c38dd9c48`；
收据 `tmp/generic-fu-canonical/off-four-compatibility-v1/receipt.json`。
这是旧预测兼容性，不是新 generic 路径四案精度或吞吐验证。

下一次最终采集还需单列两个前置项：DMA 的功能命令/初态/截止覆盖不能由统计
计数补回；连续 source-cut 计分的 source-retirement-start-v1 则是额外 oracle-only
参考系列，需要实际预热末成员锚、post-update seal 和预 BEGIN 生命周期证明。
[首版参考设计](superpowers/specs/2026-11-04-spr-source-retirement-start-reference-design.md)
明确只支持 N>0：四个目标源均有非零预热；N=0 保持显式 unsupported，后续
另证初始 epoch/任务/相位，不扩大首版 Linux GS/root 解析范围。它不改原参考、
不向模型输入 native 时刻，也不把换参考定义称为精度改进。两项已加入 bundle
设计依赖，避免之后又因遗漏重采四案。


### 下一实现片：架构边界与 ROM 适用性协议已冻结

[精确 wire/来源合同](superpowers/specs/2026-11-04-spr-architectural-boundary-wire-design.md)
及三份 layout 字节/SHA256 在代码前冻结：bit18 联合声明 archb-v1、controls-v2、
core-v2，旧旗标仍写 v1。特别明确 postMisc hook 在 advancePC 之前，真实 Eret
后继证明需原 advancePC 之后新增纯观察通知；不能从预测 PC 或 postMisc 猜。
Epoch bit1 只证明本次已完成 takeover 的 source generation 在 arming 前未执行
O3，不代表历史 CPU/RAM/TLB 冷态。当前片不采集、不改评分，完整 native 构建与
对照留待必要 bundle 合并后进行；其他 RAM/store/DMA/reference 前置项继续独立。


### 私有替换与 store 值：后续源码设计已落盘

[私有缓存替换 C1](superpowers/specs/2026-11-04-spr-private-replacement-design.md)
固定保留原需求、独立 victim/TBE 身份、私有回写与既有 LLC PUTX/M 服务、
实际 MRU/dirty 动作及容量压力验收；LLC 容量替换/Directory/DRAM 写前端另做。
它复用既有层次且无需重采普通功能输入，尚未开始代码。

[store 值 v1](superpowers/specs/2026-11-04-spr-store-values-design.md)预留 bit19，
记录每个退休 memory-store owner 的实际有效字节或明确状态，引用真正的 SD
成员而不新增 memory 行；SA 退休不能提前封死后来 SD 的值。STORE_NO_DATA
是多个标志的组合，不能一概当零写；NO_ACCESS 与维护、atomic 等分开。
该片也尚未开始。当前唯一实现仍为架构边界/ROM 输入，完整四案仍未重采。


### 架构输入片：首次冻结与回归，尚未验收

archb-v1 / controls-v2 / core-v2 的默认关闭实现已首次冻结，共49个实现/测试
文件，另有根项目 CMake 集成。私有7个 C++ 目标与50个 Python 测试通过；
项目完整构建退出0，31个选定 C++ 目标通过。更广的224个 Python 测试发现
1 failure / 4 errors：新 native 头文件路径依赖影响3个独立编译夹具，switch
抽取测试缺新闭包参数，未知功能测试仍使用已分配的 bit18。首次失败日志
保留在 `tmp/architectural-bundle-canonical/python-tests-v1.log`，没有排除这些
测试或宣布通过。独立审查另发现非法帧尺寸分配、事件前驱和 ROM N/A 祖先
关联需要加严；当前待统一修复与复审，不能视为该片完成。

已绑定原始源码/报告/协议/审查包与哈希快照；后续修复必须保留本次证据。
此片未做 native ISA/完整采集器构建或 OFF/ON 原生执行资格；新四案采集和
完整模型 CPI/吞吐结论仍未开放。测试夹具和合成 PF 读写闭环不替代这些门禁。


### 16 小时进度核查与执行收敛

截至2026-11-04 22:04 EST，从05:33设计提交起约16小时30分钟，已有15个后续
父仓库提交（代码、文档、可移植补丁）；仍没有完整新模型的四案 CPI/吞吐结论。
当前没有 gem5/FastSim 回放进程，A3未重启。耗时来自广泛的运行时/功能采集扩展、
逐片设计审查修复和回归，不能归结为一次编译/负载等待。当前完整FS/warmup桥接
仍被代码明确拒绝，不能用组件或默认OFF兼容结果替代最终交付。

执行收敛：当前架构输入片仅修审查指出的5项Important缺陷并复审；不释放新的
physical-baseline/mval/C1/oracle实现。先核对完整四案可执行的必需运行时闭环，
将后续完整前端/MMU能力与阶段性机制验证明确区分。缺失输入仍如实报告；不删除
门禁、不裁剪源、不修改目标参数或用原生时间绕过模型来制造可运行/精度结论。


### 架构输入源码片：修复复审与回归通过，原生门禁保留

首轮5项Important已修复，限定复审全部ADDRESSED、无新增Critical/Important。
项目完整构建退出0，31个选定C++目标与225个Python测试通过；原失败日志、原
快照与修复快照分别保留。历史FLAC完整OFF回放的cores/time_accounting及去除
host吞吐的scope_metrics与上一个验收版本完全一致。这是兼容验证，没有新的
因果FS路径精度/吞吐结果。收据在
`tmp/architectural-bundle-canonical/accepted-source-checkpoint-v2.json`。

gem5-spr/yinhaolang本地源码提交为`1ef38b90558dca4ec0b2e8c092c68ef195a50dfb`，
父项目可移植patch已更新且反向check通过；保留可fetch的02119d1 gitlink，不向
child原始origin推送。该提交是源码检查点：native ISA/完整构建、真实执行及
OFF/ON中性仍未验证，bit18保持默认关闭。下一运行时片使用原已资格化输入，
不依赖这个新采集功能；因此不以额外采集协议阻塞FS执行链接入。


### F1：连续源与逐核任务计时已接入，FLAC 首次实际入口被门禁拒绝

新增默认关闭 `core.causal_fs_execution`，复用原有 Binary/Warmup reader、
FunctionalSyncPlan、RecordedScheduler 和 TaskClock。预热暂停与真EOF分开，
跨切点保留ROB/依赖/SQ/缓存/在途请求；逐核最后预热成员实际退休后计时，
最终退休及必要边界释放后冻结，后台store不加尾部CPI。未修改采集器或输入
协议；未实现的操作类和目标功能仍保留门禁。

独立审查Spec/Quality通过，无阻塞问题；完整构建退出0，33个选定C++目标、
62个相关Python测试通过。正式可执行文件的真实三记录Binary/Warmup CLI检查
通过；历史FLAC完整OFF回放与前一检查点的cores/time_accounting和去除host
吞吐的scope_metrics一致。收据在
`tmp/fs-execution-bridge/canonical-v1/accepted-checkpoint.json`。

新模式明确标partial-unqualified和source-retirement-start-v1；与原共同起点
不一致时，维护比较器拒绝CPI/elapsed评分。阶段wall及measurement吞吐未知时
均为null，保留明确的end-to-end诊断；没有把局部duration最大值称为原共同
elapsed。此片没有新的精度或吞吐验收结论。

已对原资格化FLAC四核完整manifest及原始config.ini实际尝试入口，原early
prepare开关保持true，未裁剪源。调用在配置验证阶段退出1，日志为native
coherence要求no early prepare/legacy coherence等条件；其中源码明确排斥
`causal_early_store_prepare`及目标`early_store_prepare`，与当前目标开启状态
冲突。输入指令尚未执行，不产生CPI结果。完整证据在
`tmp/fs-execution-bridge/flac-entry-v1/receipt.json`。下一步应处理已经证实的
多核一致性/early-prepare接入限制，并区分旧引擎选择与真实目标参数；不靠
关闭原目标机制制造可运行结果。


### F2a：多核 early prepare 与 native coherence 接入完成，完整目标仍未运行

原目标 early prepare 保持开启，新增带类型的 prepare 物理事务身份和独立普通
store 接管身份。通过既有 StoreBuffer、OnlineSequencer 和 NativeCoherence
实现 SP_IE、独立16/16额度、S升级、load等待、接管、远端失效、三端口仲裁
及恰好一次终止/资源释放；不创建虚假的普通请求或另一套时间求解器。L0填充
保持本地clean直到真正wrote，父缓存仅在子响应Dirty时合并数据/脏状态，保留
尚未完成实际写入时的父级数据债务。默认及旧单核路径保留。

独立Spec/Quality审查通过，零Critical/Important；一个Minor为把已有审计中
同时达到ordinary16/optional16的事实补成显式断言，当前证据已确认两核都达到
且最终归零，因此不追加一轮实现。完整构建退出0，34个选定C++目标通过；
真实CLI计时/未知吞吐契约通过，历史完整FLAC旧路径的cores、time_accounting
和去除host吞吐的scope_metrics与F1检查点相同。正式构建仍有既有格式/LTO
警告，不把它称为无警告构建。验收收据：
`tmp/fs-execution-bridge/f2-canonical-v1/accepted-checkpoint.json`。

使用同一个未修改的FLAC完整manifest和entry-v2目标配置重试：退出1，耗时
约0.008秒，已越过早先early-prepare互斥限制，下一门禁为causal_runtime的
前端/翻译/旧模式适配条件。入口未执行指令，没有新CPI/PMU/吞吐结果。证据
在`tmp/fs-execution-bridge/flac-entry-v3/receipt.json`。下一步仅对实际有效
配置逐项归因：旧求解器选择可显式迁移，真实目标功能必须实现对应适配；
不关闭原目标机制，不裁剪输入，不为得到数字扩展采集协议。


### F2b：真实因果 DRAM 控制器已接入逐 rank 刷新

复用 DramRankRefresh，将逐rank刷新、行关闭、PRE/数据完成约束、可用rank
选择及blocked-rank唤醒接入实际MixedDramController；跨预热保留状态，空闲
周期算术跳过，周期计时器不持有回放生命周期。只解除已实现的刷新组合限制，
前端/DTLB和其他未实现域仍保留门禁，projected旧模式仍拒绝刷新。刷新OFF
保持既有调度。配置origin=0是模型显式起点，原生入口相位仍未知；原日历的
较严格overrun限制未扩大，不据原生fatal条件单独宣称更宽时间域可执行。

独立Spec/Quality通过，无Critical/Important；完整构建退出0，34个选定C++
目标通过，真实CLI契约通过，历史完整FLAC的旧路径cores/time_accounting/
去除host吞吐的scope_metrics与F2a相同。两条Minor（补core1对称人口断言、
私有构建警告明细）记为后续清理，当前双核生命周期/计时及正式构建日志保留；
没有无警告构建声明。验收收据为
`tmp/fs-execution-bridge/f2b-canonical-v1/accepted-checkpoint.json`。

真实Simulator使用构造的二进制及功能伴随，经生产reader/预热wrapper验证
12组chunk/hostdelay/预热组合；直接控制器另覆盖rank/channel并行、边界、
原生前端merge/forward/ACK等。这些不是采集负载或原生精度证据。刷新统计
来自连续模型控制器事件（包括预热和detached tail），不是measurement-only
计数；core CPI仍在各自最终退休处冻结。已知前端和DTLB阻塞尚未关闭，因此
不重复失败的完整入口，没有新FLAC/四案CPI/PMU/吞吐验收。

下一配置已显式准备为entry-v4，仅关闭被新因果引擎替代的三个旧求解器选择：
response_materialized_uop_fast_kernel、response_queue_feedback和
response_sparse_scoreboard；全部目标enable/geometry/delay保留，未执行。
随后继续现有SprFrontend的实际rename/IDQ握手，再处理真实I侧与DTLB接入；
不以新增采集协议或关闭目标机制替代这些运行时工作。


### F2c：退休前端已连接实际 rename 接收，完整 I 侧仍未接入

新增默认OFF的core.causal_retired_frontend，复用SprFrontend与真实FFE事实。
ROB/IQ/LQ/SQ含在途预留的检查通过后才确认实际rename、归还IDQ额度，随后
经过已导入2-cycle边进入现有dispatch分配；没有重复分配或提前释放。零惯用法
结果在实际rename时就绪，dispatch/ROB所有权仍分别跟踪；不据此证明完整
ROB/物理寄存器模型。预热切点、源图停车、partial macro、RAW/store状态和
逐核退休结束计时保持连续，后端堵塞等待实际事件，不逐周期空转。

独立Spec/Quality通过，零Critical/Important；完整构建退出0，36个选定C++
目标通过（原34个加两个已有前端目标），原CLI契约及新前端ON的实际CLI能力/
计时/null吞吐检查通过。新增CLI构造夹具首次遗漏typed resource limits而
被拒绝，补齐为现有测试同一配置后通过；原失败收据保留，未修改产品门禁。
历史完整FLAC旧路径的cores/time_accounting/scope-no-host与F2b完全一致。
收据：`tmp/fs-execution-bridge/f2c-canonical-v1/accepted-checkpoint.json`。

私有实际Binary+FFE夹具覆盖ROB/IQ/LQ/SQ压力、width、MITE/DSB/MS、partial/
pseudo、9-uop宏中间预热、非共享SA/SD、图边界、unequal origins、EOF及
晚store drain；完整模型tuple在chunks1/2/7/128及host delay下相同。独立
审查仍将新前端与early/coherent同时开启的动态证据记为Minor未覆盖，现有
对应旧开关OFF回归只证明保留，不证明这个新组合。下一I侧任务须覆盖该组合。

现有FFE的shared allocation前端位不能直接证明ROB/IQ分组语义，因此未证明
的共享组被拒绝。已有指令字节、语义与静态op/store事实可供后续源验证推导，
当前不据此要求新采集协议。Icache、物理I请求ledger/lower hierarchy、DTLB、
unsupported classes/replacement/free-list门禁仍在，retired batching仍是
既有proxy；没有完整P4/目标或四案CPI/PMU/吞吐结论。下一步为真实I侧请求
与返回所有权接入，保留显式modeled地址策略/未知native physical identity。
