# SPR retired-cache 未知事件的在途预取观察

## 已确认事实

协议 L0（L1D）和 L1（私有 L2）的 `PF_IS, Load` 将需求并入先前
在途预取；此分支不会按需求身份重新下发该层新的下级 GETS。当前 retired
cache 观察器若只有上级 miss、没有该 demand 的下级结果，则正确保留
L2/L3 unknown。历史 FLAC 样本中存在上述计数形态，但没有 typed 预取
关联，无法把 unknown 人口全数归因于该分支。

## 本次新增观察

仅 oracle 路径按 demand context/sequence 记录 L1D、私有 L2 的
`PF_IS, Load` 各层观测；保存到原生 hierarchy lifecycle，再投影到
retirement NativeFacts。`native_service_diagnostics` 增加两个 unknown
read-UOP 交叉计数和每个有限样本的 L1/L2 观察次数。事件级为 macro，
交叉计数为 measured read UOP；二者不能直接相减或宣称一一对应。没有标记
不证明未发生（例如采样前已在途的请求身份缺少该观察）。

这不改变 cache lookup、请求次序、模拟时序、retired cache 分类或 FST；
不把 native 观察馈送 FastSim。无论捕获了碰撞标记与否，原有 unknown
仍保持 unknown：下层预取事务的 hit/miss 并不能直接证明退休 load 的
SPR perf 事件资格。缺失正确处理仍是待解决问题。

组件测试：`test_spr_native_prefetch_unknown.py` 检查附加观察不会改变
unknown 及退休事件，并检查计数与样本输出；`test_native_pio_lifecycle.py`
增加真实 registry → projection 的捕获与无效 level 保护。两者通过。
后续统一增量编译 gem5（按项目要求 `-j128`、无链接超时，先保留旧二进制），
再执行小样本验证状态不变、观察标记路径和数量，之后才能考虑重采 FLAC。
这不是对全 workload 或 PMU 精度的验证。

## 构建与 smoke 状态

保留 `tmp/spr-native-prefetch-observation-20261105/gem5-opt-before`，记录 SHA256；
新 gem5 已完成 SCons/gold 的 `-j128` 增量构建，链接命令显式包含
`--threads --thread-count=128`，未设置链接超时。前两次尝试分别因 Python
动态库和 protobuf/共享头路径未配置退出，均验证旧二进制哈希恢复，最终仅在
构建进程设置局部路径，不更改全局环境。

运行 `run-smoke.py` 串行对照旧/新二进制，使用相同 C4 FLAC 参数、50k warmup、
100k ROI 及独立输出目录；启动前检查 FST 配额并在运行时监控。该 smoke 的
目标是检查 oracle 字段及功能证据能正常产生；KVM/OS 交错本身可能不同，
不能把不同进程的逐周期等同性当作已证明。等运行结束后再记录结果。

旧/新 smoke 均正常结束并验证四核 FST 与 native task-clock 的原始流 hash，
四核功能伴随读取通过。新 smoke 的 unknown read-UOP 各核为 17/135/31/324，
其中有 L1D late-prefetch 标记 1/0/27/324，有私有 L2 标记 2/0/4/0；部分
unknown 无标记，不能全部归因于预取。新旧 smoke 的退休 clock 审计均因
短窗 core1 尚无 user 退休而 `unavailable-common-clock`，无法作为 CPI/PMU
准确率比较；不得把这些内容提升为正式验证结果。新旧不同 KVM 运行的功能
时间交错不保证相同，因此不能声称已经完成逐周期中立性验证。

基于功能烟测和用户授权，启动独立 FLAC/XZ/Graph500/Marian C4 10M ROI、
500k warmup 重采；后面三案在启动前对原工作负载二进制、输入 tar、launcher
与 scheduler profile 校验冻结哈希。四案分别预留 14/14/12/12 GB FST，
原始 FST 总量加预留上界 378.11 GB，小于 395 GB；持续执行共享预约和
总量/空余空间守卫。每案记录 request、输入身份、gem5 hash、状态及日志，
不覆盖旧数据。采集成功后独立 watcher 依次执行 `prepare_spr_capture.py`、
目标身份、退休人口和原生 task PMU 审计。结果、错误和剩余 unknown 以每案
输出为准；完成这些 gate 也不等于 CPI/PMU 精度或真实硬件等价认证。

新版 smoke 的 core1 未标记 unknown 样本（mask 1537）显示
`other_terminal=true`、terminal reason 512（DeviceBypassComplete）、无
Ruby admission/response，PC 为内核地址；该类型独立于在途预取，需要
硬件事件资格证据，保持 unknown。有限样本不能证明全体 135 个具有同一
来源；等待完整 ROI 与原生审计。

## 后续完整关联思路（尚未实施）

当前 L0 `pa_issuePfGETS` 和 L1 `pa_issuePrefetchGETS` 创建的预取消息未
携带后来的 demand 指令身份，现有 `PerfTransactionId` 为零时 native
observer 仅记 unidentified lookup；不能事后拿相同 line 或相近时间直接
猜测关联。若要恢复每条需求的下级服务，需要先为每次预取产生独立的、
带代次的 oracle transaction identity，贯穿本层 TBE 和下级消息；无需求
身份的事件仍须记录自己的事务。发生 `PF_IS, Load` 时再把确切的 demand
(context, seq, fragment) 连接到该在途 transaction，必要时缓存先于 demand
到达的下级结果，直到 demand response/drain 后释放。这只形成事实关联，
不能直接把 prefetch 的一次 L2/L3 miss 复制成每个退休 follower 的 PMU
miss；分片、同线重发、失效/取消、同线多核及跨 ROI 边界均需实例测试，
目标事件资格仍需独立论证。新增事务标识只用于独立 native baseline，
禁止进入 FST 或影响协议/运行调度。当前运行的四案使用已构建二进制；
本段为未来设计，不在采集中修改 collector 源或二进制。

四案启动后的 profile 静态核对：新旧 FLAC `uarch_profile.json` 顶层字段
一致；其余三案的旧 profile 中所有字段在新 profile 均相同，新 profile 的
`native_cpu` 新增 7 个更细元数据字段。为输出伴随而使用了更新过的
`fs_capture.py/collection.py`，两者文件 hash 与旧批次不同；目标完整身份
仍须对每案最终 `config.ini` 运行独立 validator，不用 profile 代替该 gate。

进一步代码审查：不能把 `(context, sequence)` 单独当 fragment 主键。L0
`RubyRequest` 和向下 `CoherenceMsg` 有 `PerfTransactionId`，由 Sequencer 对
真正下发的每个 packet 尝试单独分配并向下传递；这可能作为可证实的 demand
attempt 身份，但 zero、被合并且未下发、重试、跨 ROI、预取没有该字段等
情况要单独处理，不能伪造非零值。L0 prefetch 的 GETS 不设置该字段；私有
L2 把从 L0 收到的 GETS 按 Load 处理，所以还可能出现 L1 预取跟随更早的
L2 预取，需要**显式 prefetch→prefetch 父链**而非仅 demand→prefetch。
独立原型允许 caller 传 `(context,sequence,fragment,attempt)` 并逐层保留
父链；正式实现前必须找到并验证该组合的实际采集来源和存活边界，不能把
原型中的 synthetic fragment 字段视为现有输入事实。

首个完整输出 Graph500 的 native 原始诊断：四核共 179114 个带 unknown 的
read-UOP 观察，其中 169743 个带 L1D late-prefetch 标记，9363 个带私有
L2 late-prefetch 标记。两类标记可重叠，单位不是退休宏 load，不能相加
推断 union、不能直接替换或比较 PMU miss，也不认证硬件行为。该案已到
common-end、原始采集正常退出，prepare/identity/人口审计仍在运行。

新 FLAC 10M 原始 oracle：四核 workload-task 退休宏的 L1/L2/L3 miss
unknown 分别为 0/963/971。按 read-UOP 计的 unknown 与 L1D late-prefetch
交叉各核为 815/216/735/0，与私有 L2 late-prefetch 交叉为 23/1/6/0。
宏和 UOP 人口不相同，不能相减或将未知事件直接定为 hit/miss；此为
common-end 后、prepare/正式 native 审计前的原始诊断，输入/目标/人口
尚在审核。

四案最终采集进程均已正常退出，Graph500、FLAC、XZ、Marian 分别耗时
1183.37、1723.98、1768.95、1956.44 秒。采集预约均已释放，检查时
原始 FST 合计 336.54 GB、文件系统空闲 382.56 GB；此时各案源准备及
独立身份/退休人口/原生 task PMU 审计仍分别执行。Graph500 已通过源准备
和目标身份检查，提供了对应输入和模型配置供并行回放；最终 native gate
仍待完成。采集成功、能够回放和原生基准可评分是不同状态。

Graph500 随后完成 source、目标身份、retirement 和 task PMU 审计，状态为
`native-source-checked`，仍须通过维护的 native validator 才能得出同口径
回放报告。XZ 的首次 prepare 报 `boundary partial prefix ffe mismatch`：
core 2 的两个无 entry-member event 的 terminal 被 auditor 在热流第 9388 条
附近提前读取，而需要证明的 FFE 前缀分别位于 `[780562,780563)` 和
`[1472107,1472108)`。独立读取 FFE 已确认两段均存在且 Complete 位为 0。
原 auditor 在读取 terminal 时立即要求前缀验证完成，不能处理这种前读
顺序。修复推迟检查至对应 FFE 行，仍校验精确范围、partial 状态、双份
FFE 一致性以及文件结尾无未决证明；可复现实例在旧代码报错，修复后
通过。针对该路径增加的反例仅修改 archb 声明，将前缀起点扩展到前一个
FFE span，同时保持 predecessor、前缀结束位置合法并重算 body hash；
deferred join 以精确前缀不符拒绝。另一个双份前端 span 内容篡改反例由
更早的 hot/FFE 一致性检查拒绝。两反例和完整 bundle 集成测试均已通过。
该调整不会改变 FST 内容、FastSim 时序或 gem5 baseline；XZ 完整重审已
启动，当前不宣布其通过。

后续进度：FLAC 完成 source、目标身份、retirement、task PMU 四项审计。
Marian 已完成 source 和身份检查并生成回放配置，retirement 和 task PMU
审计进行中。其全流 4 个 `.sreq` 覆盖 14808133 条 memory record，普通
load 7753762、store 地址/数据 UOP 各 1464268；load eligibility 未证明
和不完整 store descriptor 均为零。数据翻译无 unknown byte mapping，但
leaf-only 1099 条；core 功能 unknown applicable 2642 条，完整 SPR profile
未 qualified。FLAC 的对应全流指标：memory 7923613、普通 load 6536273、
store 地址/数据 UOP 各 980623，load eligibility 未证明和不完整 store
descriptor 均为零；xlat leaf-only 948 条，core unknown applicable 8672 条。
这些是全流而非特定 ROI 的人口，不得误作 PMU 或精度指标。

XZ 的重审在修复前读顺序后通过完整 source 准备和目标身份核验，并生成
独立回放配置；其 retirement/task PMU 审计随后亦通过。全流 4 个 `.sreq`
含 11428488 条 memory record、普通 load 7093948、store 地址/数据 UOP
各 1946602，load eligibility 未证明和不完整 store descriptor 均为零；
数据翻译无 unknown mapping 但 leaf-only 950 条，core unknown applicable
25071 条，完整 SPR profile 未 qualified。Marian 现已完成 source、身份、
retirement、task PMU 四项审计。至此四案均已通过这四项 source 审计；准入
状态不代表维护的 native validator 评分通过、性能精度或硬件等价。
