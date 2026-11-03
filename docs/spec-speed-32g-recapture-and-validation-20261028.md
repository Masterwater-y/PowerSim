# 32GiB speed 补采与FastSim并行验证（2026-10-28）

## 最新并行采集状态：30GB实际空闲底线

用户随后明确要求同时采集并调整空闲底线。现行底线为30GB，总FST上限400GB、
395GB提前停止与32GB/48GB单案上限保持生效。两组实际producer与存储guard均已启动，
各自`capture-command.json`记录的底线均为30GB，共享预留表同时包含两个有效预算。

- Marian C16：`tmp/spec-speed-32g-c16-recapture-20261028/source/c16-872.marian_s/`，CPU0–23。
- FLAC C16：`tmp/spec-speed-32g-c16-recapture-20261028-flac-retry1/source/c16-817.flac_s/`，CPU96–119。
- 合并状态与合格来源清单：`tmp/spec-speed-32g-c16-recapture-20261028-parallel/`；
  独立publisher持续刷新`matrix-status.json`，仅在来源检查完成后纳入新来源。

本次实施发生操作错误：对旧Python guard在线同步配置时，GDB8.2.1的inferior call
报扩展寄存器错误，随后sleep因errno514异常退出，guard清理终止了原FLAC producer。
原尝试运行3171.45秒（约52分51秒），测量尚未打开；这段执行进度丢失。
已停止使用该同步方式，Marian正常启动；FLAC使用已核验的原checkpoint重新运行。
无gem5重编译或checkpoint身份放宽，当前10组正式数据保持完整。
原FLAC失败源保留日志、配置、统计、checkpoint及失败收据，64个失败FST及专属旁表
24.53GB按SHA256清单回收。原coordinator的50GB启动快照保留作历史，
其Marian已通过入场检查且没有其他待启动任务；实际两个存储guard及FLAC retry coordinator均用30GB。
事件与回收凭据见`disk-floor-update-20261028/incident.json`及
`disk-floor-update-20261028/failed-flac-retirement/receipt.json`（均在原补采目录下）。
后续同步结果见该目录的`parallel-verification.json`。

下文关于50GB及Marian等待容量的描述是此前阶段的历史记录。

详细规则、测试与环境限制见[零内核窗口修正记录](native-kernel-empty-window-validation-fix-20261028.md)。

后续验证器修正：合法测量窗口允许内核记录和内核访存均为零；仍要求原生模式、
内核计数合法、逐项指令数量一致以及来源/守恒检查。新测试复现并修正FLAC C4误拒，
也覆盖有内核执行但无内核访存的窗口。原采集与FastSim模型均不改变。
原10案队列继续执行，使用新验证器的4路重评另写入
`tmp/spec-speed-32g-fastsim-validation-20261028-revalidated/`；每案等待原回放完成，
再以维护入口的`--source-root`和`--stats-dir`重新核验已有预测。
新目录`summary.md`为修正规则后的汇总，原目录及旧冻结验证器保留作历史证据。

用户授权同时开始两项任务：补采剩余Marian/FLAC C16，对已完成的10组正式来源并行运行FastSim验证。
这是运行启动记录，最终完成数量和精度结果以各任务的机器可读状态为准。

## 补采

新目录：`tmp/spec-speed-32g-c16-recapture-20261028/`。
调度器为该目录`run_campaign.py`，复用冻结的维护入口`native_speed_campaign.py`，
两路worker分别使用CPU0–23和24–47；不覆盖原12案的失败现场、冻结配置或完成凭据。
`combined-source/`最初链接当前10组合格来源，仅在新来源通过完整检查后加入对应C16案例。

| 案例 | 内存 | 测量目标 | 预热预算 | FST上限 | 启动后状态 |
|---|---|---|---|---|---|
| Marian C16 | 32GiB | 最快核10M用户微操作、全核共同终点 | 身份齐备后core0执行500K预算 | 32GB | 等待容量 |
| FLAC C16 | 32GiB | 同上 | 同上 | 48GB | 恢复checkpoint并采集 |

使用Ruby clean-IM functional-read修复后的producer，SHA256为
`2cf2d525185e00ea4b84dfc692c487654e47092dfda5ed456639f279cdbc4e6a`。
已有两个新producer绑定的staged KVM checkpoint，其完整payload哈希与producer、runner、
工作负载及launcher哈希已重新核对。直接恢复这些checkpoint，不重复启动OS。
此前100K短验证不作为正式10M完成证明；已知另一个Ruby压力测试问题没有因此视为修复。

总FST上限400GB、395GB提前停止、50GB实际空闲底线及32GB/48GB单案上限均保留。
两案同时预留需至少130GB空闲，启动前只有约114.5GB，宿主检查后约113.7GB。
因此提交两路调度，但容量门禁只允许FLAC进入采集，Marian等待；不降低保护，也不冒称两案已同时执行。
空间满足预算后，等待案例自动进入采集。日志另有500MB上限与重复KVM MSR告警保护。

状态及凭据：

- `launch.json`、`launch-plan.json`：命令、PID、预算和启动条件。
- `checkpoint-reuse/`、`freeze.json`：checkpoint与冻结输入身份。
- `campaign-status.json`：两案实时调度状态。
- `matrix-status.json`、`combined-source/`：独立的12案合格来源清单，不改旧矩阵。
- `source/<case>/capture-status.json`、`capture-phases.jsonl`、`capture-receipt.json`：生产阶段和退出原因。

## 已有10组FastSim验证

新目录：`tmp/spec-speed-32g-fastsim-validation-20261028/`。
仅使用原正式矩阵`accepted-source/`的10组来源：四负载C4/C8及Graph500/XZ C16。
已回收的历史来源不作为本轮输入，不复制大体积FST。

使用维护的`run_native_kernel_fastsim_validation.py`，4路worker使用CPU48–95，
逐案独立进行身份、来源/范围、原生Ruby/SLICC守恒和参数来源检查。
FastSim默认采用既定FR-FCFS候选：关闭拓扑缩窗，启用到达时序约束。
启动时10/10案例的独立身份及逐案130项参数preflight已通过；维护validator先执行来源哈希与范围审计。
随后已确认实际FastSim进程开始回放，首个案例为Graph500 C4，其余任务按4路队列自主推进。
验证目录的`live-status.json`与`summary.json`/`summary.md`由独立monitor每30秒刷新；
最终结果由`completed.json`与`monitor-completed.json`记录。
补采启动时的进程、正式10M/500K参数和磁盘保护核验见
`tmp/spec-speed-32g-c16-recapture-20261028/startup-verification.json`。

C16旧参数审核器不能识别带前导零的对象路径。本轮仅在新输出写入参数审核专用INI，
保留原INI哈希，并记录逐section/key/value等价及路径命名映射；正式身份检查仍使用原采集INI。
相关凭据为各案`profile-audit-config-derivation.json`，不修改原来源或时序模型。

报告必须区分来源检查、CPI/PMU可评分性和误差：CPI主口径为workload-task active，
逐案同时展示绝对误差（cycles/宏指令）和相对误差，聚合展示MAE并区分MAPE/WAPE。
退休load L1/L2/L3 miss的完整PMU精度仍要求全量分类及参考unknown为零；缺证据不得填零。
来源检查通过不表示精度阈值、吞吐验收或SPR硬件等价通过。
本轮并行且与gem5补采共享宿主，运行耗时与吞吐只记录为本轮条件下观测值，不作安静宿主吞吐认证。
