# Zstd rate C4：完整 ROI 退休周期排查

本轮按 [9 月 24 日排查流程](buffer-tso-cpi-diagnosis-next-steps-20260924.md#8-下一步工作与验收顺序)执行：固定来源与配置，先建立完整 ROI 的互斥退休周期账，再定位主要差异区段。阶段账是时间分区，不是 cache、SQ 或分支的因果贡献。

实验目录：[tmp/zstd-cpi-retirement-diagnosis-20261019](../tmp/zstd-cpi-retirement-diagnosis-20261019/)。生产模型与默认配置未修改。

## 冻结来源与受控试验

原始对照是 NT-store 调试 trap 修复后的 `zstd-ntstore-repair-20261016/source/c04-777.zstd_r`：4 个单线程 SPEC rate copy、4 核、8 通道、3 GiB、buffer TSO 114、LRU；使用既有实际压缩计算 ROI、KVM checkpoint 和 1M core0 warmup 指令预算。独立硬件身份复核通过。计分采用 recorded-owner workload-task active cycles / task macro instructions，双方分母均为 21,001,415。

下面是同一原始 FST 的配置受控试验。每行是同一负载的候选配置，不能作为多个负载聚合。CPI 绝对误差单位为 cycles/macro instruction。

| 配置 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|
| 原始默认 | 1.500908 | 1.083942 | -27.78% | 0.416967 |
| 仅开启 response branch recovery | 1.500908 | 1.125034 | -25.04% | 0.375874 |
| 仅开启 store prepare lifecycle | 1.500908 | 1.105351 | -26.35% | 0.395557 |

默认单案 CPI MAE 为 0.416967；分支候选单案 MAE 为 0.375874。分支候选经维护 native validator 验证来源、人口与守恒通过，补回 862,999 active cycles，即原 8,756,890.88 周期缺口的 9.855%。它不能单独解释主要误差，尚未改变默认开关。

store lifecycle 候选单案 MAE 为 0.395557，独立通过维护 validator，补回 449,631 active cycles，约 5.135%。使用同次目标已有来源映射的 commit-to-IEW=1、L1/store callback=1、L2/store callback=5，未调延迟常数。开关试验的净变化不能当作单个机制的因果贡献或上界，可能存在其他区段的抵消。

证据：[默认来源身份](../tmp/zstd-cpi-retirement-diagnosis-20261019/baseline-identity.json)、[分支受控验证](../tmp/zstd-cpi-retirement-diagnosis-20261019/branch-recovery-validation/cases/c04-777.zstd_r/validation.json)。验证通过不代表硬件 PMU 等价或模型精度达标。

## 导出与记账边界

FastSim 使用 `tmp/` 内独立的 `simulator-full-stage.cpp` 和诊断二进制，生产源码、生产二进制不变。阶段记录保存在 transaction-local TimingFeedback 中，仅在最终接受的批次写出；拒绝、重试、reweave 提案不写出。逐核 sequence 从 0 连续到原 FST 终点，共 48,001,418 条记录。完整原始 FST 导出的 CPI、PMU、目标周期和配置均与生产对照完全相同；主机计时、吞吐与生产队列等待次数单列，不参与目标中性检查。

原对照 FastSim 的物理退休跨度记账如下。每段由相邻退休边界定义，同一周期退休多条 UOP 不重复计入周期；取指前、取指到发射、发射到退休按区间交集互斥切分。不能把各条指令重叠的等待时长相加当成 CPI。

| 时间分区 | 周期 |
|---|---:|
| 有退休的周期 | 10,055,448 |
| 下一退休指令尚未取指 | 51,965 |
| 已取指、尚未发射 | 3,228,991 |
| load 已发射、尚未退休 | 8,912,169 |
| store 已发射、尚未退休 | 298,527 |
| 其他指令已发射、尚未退休 | 87,346 |
| 阶段未知 | 0 |
| 合计 | 22,634,446 |

四核末条退休到共同终点的 padding 是 138,430 cycles；再扣除 inactive 8,568 cycles，得到正式 active 22,764,308 cycles。物理退休跨度与 active CPI 分子不混用，账目严格守恒。证据：[导出中性](../tmp/zstd-cpi-retirement-diagnosis-20261019/export-neutrality.json)、[原对照 FastSim 全量账](../tmp/zstd-cpi-retirement-diagnosis-20261019/baseline-fastsim-ledger.json)。诊断运行不作为吞吐测量。

## gem5 阶段取证

使用原 gem5 二进制、原 checkpoint、原 frozen runner；观察包装器只在 warmup 结束后的 stats reset 开启已有 `O3PipeView` / `DynInst` 日志，不向目标系统增加事件。记录包装器与开启 tick，作为额外观察来源。正式尝试 warmup 为 66.828 秒，未增加 warmup 指令预算。

日志流经有界解析器，只保存按核归属的已退休指令阶段，不保存海量原始文本。解析器核对完整阶段组与销毁记录的动态序号，拒绝错序、截断及身份不匹配；retire=0 的错误路径记录单独计数后排除。native 时钟仅用于离线诊断，不输入 FastSim。

在途指令可能在日志开启前已取指，或在共同终点尚未释放，因此首尾覆盖需逐核单列，不能插值补阶段。native load completeTick 是地址生成阶段，不能当作数据返回。`O3PipeView` 只独立提供 core / 动态序号 / PC / microPC 与时间，不独立证明访存地址和操作数；不将这种配对升级为完整访存生命周期认证。

同一 KVM checkpoint 的再次执行不保证重现原始 FST。使用本次新产生的 FST 运行 FastSim，与同次 native 阶段关联；原 FST 上的消融保持独立。

### 收尾竞态与诊断资格

gem5 达到合法共同终点并输出 `simulation complete` 后，外层临时协调器把解析器的正常 EOF/exit=0 当作提前退出，向仍在收尾的进程组发送 SIGTERM。原 `capture-receipt.json` 保留 returncode=-15 / signal-SIGTERM，不改写为成功采集。结构检查确认四核 FST header、metadata、functional-boundary 记录数相等，边界均关闭、共同终点一致，native 未完成响应数均为零；解析器正常关闭，没有截断阶段组。

后续独立硬件身份、common-end、owner 人口和维护 native validator 检查通过；它们不覆盖采集进程退出失败。因此本次新窗口只作为**诊断取证**，不能据此宣称新增成功的正式采集或硬件认证。原始成功采集上的 CPI 与受控消融不受影响。额外证据见 [退出竞态评估](../tmp/zstd-cpi-retirement-diagnosis-20261019/observer-exit-race-assessment.json)。修复后的协调器只在解析器非零退出时中止生产者，正常 EOF 等待生产者自身结束。

## 同次完整窗口的结果

新窗口实际测量 40,451,708 条物理 UOP，逐核 PC/程序顺序与 native 阶段全量匹配，首尾均无缺记录。双方 workload-task macro 分母为 21,041,615；active 周期为 gem5 31,003,585.879880、FastSim 22,518,098。维护 validator 给出的诊断 CPI 如下。

| 窗口 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 / 单案 MAE |
|---|---:|---:|---:|---:|
| 本次阶段取证窗口 | 1.473441 | 1.070170 | -27.37% | 0.403272 |

新窗口与原窗口不能逐指令做修复前后对照；它复现了相同方向与量级的低估。两个独立阶段导出器与本次生产 FastSim 的目标统计完全一致。

| 互斥退休时间分区 | gem5 周期 | FastSim 周期 | gem5 - FastSim |
|---|---:|---:|---:|
| 有退休的周期 | 11,856,464 | 10,148,205 | +1,708,259 |
| 下一退休指令尚未取指 | 94,503 | 54,647 | +39,856 |
| 已取指、尚未最终发射（含翻译/重试） | 7,662,983 | 2,987,548 | +4,675,435 |
| load 已发射、尚未退休 | 10,439,785 | 8,814,280 | +1,625,505 |
| store 已发射、尚未退休 | 685,843 | 293,919 | +391,924 |
| 其他已发射、尚未退休 | 273,024 | 86,866 | +186,158 |
| 阶段未知 | 33 | 0 | +33 |
| 已配对退休跨度合计 | 31,012,635 | 22,385,465 | +8,627,170 |

起点、共同终点 padding 与 owner 修正合计为 -141,682.120120 cycles，加入后严格得到 active 周期差 8,485,487.879880。33 个未知阶段周期保留，不摊入已知项。17 条内核 store 在模型中没有 data-hierarchy event（其具体逃逸原因未逐条认证），另行保留角色差异清单；分区双方均以同一 FST 的功能 load/store 类型分类，不将模型缺少 cache event 伪装为输入指令不匹配。

### 冷启动不是主要集中落点

每核按测量记录顺序等分八段，四核对应段相加。退休跨度差依次为 452,691、1,004,937、590,977、2,732,712、654,655、1,341,901、1,684,524、164,773 cycles。首段只占完整跨度差 5.247%，最大差出现在第 4 段，后续第 6/7 段也有较大差额。因此当前大误差没有集中在 ROI 开头，不能归结为“指令数少、冷启动严重”。尚未做 warmup 长度扫描，不声称完全排除工作集预热的间接影响。

![八段退休跨度差与互斥阶段分区](../tmp/zstd-cpi-retirement-diagnosis-20261019/retirement-gap.svg)

### 主要落点是 NT-copy 的翻译/重试与 store 完成链

完整窗口中存在 20 段结束于同一函数 SFENCE 的 NT-copy。其内部退休跨度差合计 5,136,236 cycles，占 active 周期缺口约 60.53%；相同区段的 store 完成跨度差为 5,167,958.060 cycles。这是互不重叠的执行区段差额，不是把每条 store 的等待求和。

这些区段夹杂 8,737 条内核 UOP，逐段产物中保留计数；60.53% 是区段差额与总 active 缺口的规模比，不是已经剥离所有中断影响的纯 store 因果贡献。本次 gem5 的 NT 指令仍是 cacheable fallback，结论不认证真机 non-temporal / write-combining 行为。

core0 第一段无内核切换：native 退休跨度 300,782 cycles、FastSim 72,003 cycles；native store 完成跨度 303,751.003、模型 72,259 cycles。core0 全部 NT-store 从退休到完成的中位数约 1,581 cycles，模型约 164 cycles；该分布存在重叠，只作生命周期诊断，不另加到 CPI 差额。

`O3PipeView.issueTick` 是 **最后一次 IQ issue**，每次重试都会覆盖。因此“取指→最终发射”包含翻译等待和重试，不是纯 IQ 容量等待。本轮开始根据该字段把长间隙理解成“尚未发射”，追加原生日志已纠正这一解释；退休跨度及时间分区守恒不受影响。

短段见证（core0，独立合法共同终点，采集退出码 0）中，CDA `[sn:4398875]` 在 tick 7,660,261,904,832 加入 IQ，当 tick 立即 ready 并首次 issue；内存依赖明确为 `No dependency`。下一周期执行后没有进入实际写路径；到 tick 7,660,262,072,997 同一序号再次 issue，后续执行才出现 `Doing write`。两次 issue 间隔 **505 cycles**。源码中这对应 translation-delayed store 进入 deferred 队列，translationCompleted 后回到 ready 队列，而不是操作数没就绪或尚未发射。不能把这 505 cycles 另加到已守恒的退休账。

当前 FastSim 确实配置了 `timing_walk`、单 walker、`coalesce_misses=false`，并未退回 `se_atomic`；但 `hierarchy_walk=false`，仍使用固定 12-cycle effective walker service。gem5 walker 则将 PTE 访问送入 Ruby 层次，并串行处理已排队 follower。首个缺陷排查方向因此变为**固定 walk 服务与实际 PTE 服务/排队的差距**，而非增加一套 DTLB 或修改 LRU 证书。已有 physical-PTE pilot 的状态约束较严格，需要同设置 fixed-control 配对验证，不能只翻开开关后拿不同快路径比较。

短段中 20,148 条已完成 NT-store 有 204 条重发，占 1.013%；重发间隔中位数 34、P95 528.7、最大 564 cycles。204 条均无预测内存依赖，没有 cache-block/re-schedule 日志。这个见证确认了翻译重试与 `issueTick` 解释问题，**不能将其外推为全部 NT-copy 差额的原因**。

已确认另一处独立语义缺口是 CDA 的 `Request::NO_ACCESS`。gem5 用本地 accessor 完成地址检查，不产生普通 Ruby 写请求；`LSQUnit::issueStorePrepareRequests()` 扫描到 `main_req->isLocalAccess()` 时 **break**，不会越过它给更年轻 store 做可选 prepare。每条 `MOVNTDQ_M` 都包含 CDA，因而这会限制 NT-copy 中未来目的行的提前准备。

当前 FST 只保留 store 标志、地址和大小，无法表达这个区别。FastSim `tso_buffer_eligible` 只检查普通单行 store、非 atomic/serialize/barrier 等条件，没有相应本地请求扫描屏障。CDA 的地址翻译、真实 PTE 访问、CPU 指令及 SQ 责任都必须保留，不能通过删除 CDA 记录、清除全部 memory 语义或减少宏指令分母修复。该语义缺口的单独 CPI 贡献尚未通过受控修复量化，不将整个 NT-copy 差额归给 CDA。

### physical-PTE 试验的能力边界

尝试按现有 pilot 的状态契约构造受控试验：control 与 candidate 同时关闭 private preview 和 materialized fast kernel，使用来源明确的四级 L1 响应下界 4×2 + deferred-IQ restart 2 = 10 cycles；仅 candidate 启用 hierarchy walk。

fixed-control 正常完成并经维护 validator 检查：gem5 CPI 1.500908、FastSim 1.045325，相对误差 -30.35%、绝对误差 / 单案 MAE 0.455583。该 control 改了基线状态契约，不能把与原默认的差异全部归给固定 walker latency。

candidate 被 `src/config.cpp` 的现有兼容性检查明确拒绝：`core.tso_store=buffer` 不能和 `dtlb.hierarchy_walk` 组合。因此没有 physical-PTE candidate 的 CPI、PMU 或吞吐结果。保留配置、命令、错误日志及失败 validation，不绕过检查，也没有改成 blocking TSO 后伪称同配置修复。

已有 [模型审计](current-code-model-audit-20260907.md#4-本轮新复现dtlb-状态可以被未来查询提前推进)还记录了生产 timing DTLB 的未来 fill 可见性问题；[早期 physical-PTE pilot](tealeaf-cpi-error-repair-plan-2026-09-03.md)仅等待源 fill，尚未完整实现 gem5 不合并 follower 的独立 walk/PTE 占用，并曾因反馈反向影响源 fill 而撤回非收敛方案。不能把候选代码存在视为生产 buffer 路径已经具备这些语义。

## 修复顺序与验收

1. producer 输出功能性的请求角色，明确 normal data、translation-only / local-access 等区别，绑定 core / stream ordinal / micro member；不输出 native latency 作为 FastSim 输入。QEMU 或 DynamoRIO 的适配器可提供同类功能语义，不绑定 gem5 调试序号。先保留正确的指令、地址检查、翻译、CPU 队列和宏人口，再阻止 CDA 生成普通 cache 写流量。
2. consumer 按真实 FIFO/canWB/完成边推进可选 prepare 扫描，遇到本地请求保持扫描屏障，越过屏障的条件与 SQ 释放统一；分开 optional prepare 与 mandatory demand 的准入、父服务及资源账。用含 CDA 与真实 stfp 的 NT-copy 机制用例核对实际请求数、扫描边界和后继放行，不能仅验证最终 CPI 变大。
3. 独立补齐 buffer 模型与 cache-visible PTE 的事务兼容性，检查 source fill generation、同页 follower 的独立排队、PTE 地址及完成后可见性；不能只删除兼容性 guard 或增加反馈迭代次数。
4. 同一原始完整 FST 上先验证功能人口和诊断中性，再复算完整退休区段账、20 段 NT-copy、正负误差区段、CPI 相对/绝对误差及 PMU 来源覆盖。改善必须来自被修复的关键边；最后回归其他负载、独立测吞吐。默认不扩大 LRU 证书，也不拟合 page-walk 或 cache latency。

上述是本轮定位后的修复要求，尚未实施生产模型修改。当前可确认主要差额落在 NT-copy；本地请求 / prepare 屏障、页表服务和其他层次状态各自贡献仍待受控修复确认。

本轮机制解析与区间守恒测试共 13 项通过，覆盖动态序号错配、截断、阶段错序、错误路径排除、同周期退休、重叠区间、辅助 syscall 与未知阶段。完整输入的两种导出均与生产结果中性；未修改生产模型，因此没有把临时插桩的主机耗时当作吞吐结果，也未重复运行生产构建测试集。

结果证据：[完整配对](../tmp/zstd-cpi-retirement-diagnosis-20261019/paired-comparison.json)、[store 完成边](../tmp/zstd-cpi-retirement-diagnosis-20261019/paired-store-release-audit.json)、[导出中性](../tmp/zstd-cpi-retirement-diagnosis-20261019/measurement-export-neutrality.json)、[同次维护验证](../tmp/zstd-cpi-retirement-diagnosis-20261019/validation-measurement/cases/c04-777.zstd_r/validation.json)。

原生重试见证：[解析结果及原始行号](../tmp/zstd-cpi-retirement-diagnosis-20261019/native-issue-retry-audit.json)、[正常采集收据](../tmp/zstd-cpi-retirement-diagnosis-20261019/source-issue-witness-fixed/c04-777.zstd_r/capture-receipt.json)。能力门结果：[fixed-control](../tmp/zstd-cpi-retirement-diagnosis-20261019/dtlb-fixed-control-validation/cases/c04-777.zstd_r/validation.json)、[physical-PTE 拒绝日志](../tmp/zstd-cpi-retirement-diagnosis-20261019/dtlb-physical-validation/cases/c04-777.zstd_r/fastsim.log)。

存储收尾：已核对 SHA256、inode、mtime 并删除失败 warmup 与被替代的空日志见证 FST，共 1,470,827,872 bytes；保留原始有效输入、本次完整诊断输入及正常短见证。全项目 FST 合计 18,491,056,968 bytes（约 18.49 GB），低于 100 GB 上限，磁盘空闲约 434 GB。删除清单：[warmup](../tmp/zstd-cpi-retirement-diagnosis-20261019/retired-warmup-fst.json)、[被替代见证](../tmp/zstd-cpi-retirement-diagnosis-20261019/retired-empty-witness-fst.json)。

## 2026-10-20 实施更新

已开始落实 producer NO_ACCESS 角色、本地 SQ 与响应侧扫描约束估计，同源 Zstd 的 CPI 改善及未完成部分见 [实施与验证](fst-no-access-repair-20261020.md)。该更新不把上文的诊断取证窗口改成正式采集，也不宣称 physical-PTE 或严格 cache 准入扫描已经完成。
