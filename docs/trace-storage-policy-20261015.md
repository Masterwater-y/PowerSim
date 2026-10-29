# FST 容量与旧采集清理

2026-10-28 最新用户授权要求同时采集并调整空闲底线，现将实际磁盘空闲底线
从50GB降至**30,000,000,000 bytes（30GB）**；总FST上限400GB、395GB提前停止、
Marian32GB和FLAC48GB单案上限保持生效。已核对两组实际运行采集器的
`capture-command.json`均加载30GB底线，共享预留表同时持有两案预算。
当前并行状态见`tmp/spec-speed-32g-c16-recapture-20261028-parallel/matrix-status.json`。
下文50GB为历史阶段策略。

同步旧管理器内存快照时，GDB8.2.1的inferior-call扩展寄存器错误导致FLAC guard异常，
随后中断的sleep报errno514，guard清理终止了原FLAC producer；损失约52分51秒的
测量前执行。失败日志、配置、统计和checkpoint保留，64个失败FST及专属旁表
共24,534,753,280 bytes已按逐文件SHA256清单回收，当前10组正式来源共1,980个文件
属性核对未变。FLAC已从同一producer绑定checkpoint重新启动，两组新guard均用30GB底线。
事件与删除凭据位于
`tmp/spec-speed-32g-c16-recapture-20261028/disk-floor-update-20261028/`。

2026-10-28 最新授权已允许开始补采及并行FastSim验证。现行400GB/395GB/50GB保护与
Marian32GB、FLAC48GB单案上限继续生效；两案调度已提交，容量不足者等待，不绕过预留。
启动状态见[补采与验证记录](spec-speed-32g-recapture-and-validation-20261028.md)。
下文“暂不开始采集”和旧空闲数是此前准备阶段的历史记录。

2026-10-28 后续用户明确授权删除已列出的9组旧来源。已删除312个FST及专属旁表，
合计43,411,627,396 bytes（43.41GB），其中FST为42,832,768,672 bytes。
当前10组正式来源及其他保留文件共4,979个文件属性核对未变；旧配置、统计、日志、
报告和checkpoint保留。9组旧来源均已写入`fst-retirement.json`，状态为
`retired-not-replayable`；历史报告不能再作为这些旧来源目前可原样回放的凭据。
删除后全项目FST为182.53GB，实际磁盘空闲约114.62GB；仍低于两案并行预算要求的
130GB加旁表余量。没有启动补采。逐文件SHA256、删除日志和完成收据见
[`tmp/fst-retirement-20261028/`](../tmp/fst-retirement-20261028/)。

2026-10-28 用户要求提高空间上限，并明确暂不开始采集。本次将总FST上限设为
**400,000,000,000 bytes（400GB）**，395GB提前停止，仍保留5GB收尾余量和
**50GB实际磁盘空闲底线**。待补采FLAC C16的单案预算从32GB提高到48GB；
Marian C16保留32GB。该策略覆盖下述300GB及更早限制，历史冻结配置和收据不改。

两案预算已写入 `tmp/spec-speed-32g-c16-recapture-20261028-planned/cases.json`，
计划状态为`configured-not-started`、`collection_allowed=false`，没有启动任何采集。
两案并行FST预留合计80GB；按现有50GB底线，启动时需至少130GB实际空闲，
另应给旁表、日志等留余量。提高配置上限不会增加物理磁盘容量，实际空闲不足时
仍由入场检查阻止启动，不能靠降低空闲底线绕过。当前约67GB实际空闲不够此预算。

2026-10-27 用户明确授权：FST总量上限放宽为 **300,000,000,000 bytes（300GB）**。
新采集在295GB提前停止，留5GB并发收尾余量；磁盘空闲保护提高为50GB，独立覆盖
checkpoint、旁表和日志增长。多案启动前还要原子预留在途FST预算。该更新覆盖下文
200GB/100GB历史限制；旧运行的凭据和保护器快照保持原样，不追溯改写。

2026-10-22 用户明确授权：当前总量上限提高为 **200,000,000,000 bytes（200GB）**，199GB提前停止，磁盘至少保留20GB空闲。下文2026-10-15清理记录及100GB旧凭据作为历史证据保留。已启动保护器持有启动时的旧上限快照，后续新阶段读取新策略，不为切换预算重启在途producer。

2026-10-15，用户将本会话FST总量上限从20GB调整为100GB，并明确要求删除旧FST。
当时上限按十进制 **100,000,000,000 bytes** 执行，覆盖旧计划中的20GB约束；
历史报告的20GB限额、实际大小和原始receipt保留，不追溯改写。

现行配置：[trace-storage-policy.json](../configs/trace-storage-policy.json)。
当前后续有界采集使用395GB总量提前停止线，保留5GB余量，并要求磁盘至少保留30GB
可用空间。单案FST限额单独指定；400GB是全部FST总量上限，不是每案上限。

## 本次清理

清理前：373个实际FST，19,492,625,768 bytes。
删除54个FST共1,916,144,440 bytes，另删除这些FST的专属伴随文件10,286,132
bytes；合计删除162个文件，约1.926GB。
清理后：319个实际FST，**17,576,481,328 bytes（17.58GB）**。

删除集合：失败的Ruby读取重试/崩溃采集、核编号错误导致请求账本失效的首轮
采集、被当前rom-gdb替代的退休探针，以及被完整入口暖机TeaLeaf替代的早期
局部转换与smoke输入。只删除明确清单中的FST及其deps/vmap/asmap/imap/ifmap，
保留原配置、日志、统计、oracle、原manifest和源哈希。

保留当前三份PMU回归输入、完整warmup的TeaLeaf主输入、同步/调度控制组及仍在用
的thread-state输入。删除前确认没有运行中的gem5/FastSim采集或回放；逐文件
记录size、SHA256、inode和mtime，删除前再次匹配，拒绝符号链接或受保护路径。
删除后核对所有受保护输入文件属性未变。

每个清理目录新增 `fst-retirement.json`，明确标记 `retired-not-replayable`。
这些旧案例的统计和故障证据仍可查看，已删除FST的旧案例不能再原样回放；不能
因旧报告存在而继续声称当前来源审计通过。

证据：

- [清理前完整盘点](../tmp/fst-storage-20261015/inventory-before.json)
- [显式删除计划与文件哈希](../tmp/fst-storage-20261015/retirement-plan.json)
- [逐文件删除日志](../tmp/fst-storage-20261015/deletion-journal.jsonl)
- [清理完成凭据](../tmp/fst-storage-20261015/cleanup-receipt.json)

当前有界采集入口 `tmp/perf-cache-threeway-20261015/capture_bounded.py` 已读取
该配置，提供 `--case-cap-bytes` 单案限额，采集中重新盘点总量并检查磁盘余量。
本次仅完成空间清理与保护设置，没有启动新的大负载采集或产生新的CPI/PMU结果。
