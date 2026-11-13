# SPR-M1 FLAC／Graph500 CPI 原因排查

本次按 FLAC 执行语义／Graph500 父服务与背压方案实施同输入排查。
基线是冻结 R0/R1 `v2-wait`，二进制 SHA-256
`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
所有实验保持原 FLAC／Graph500 C4 功能输入、warmup 和 thread-state plan；
退休 prepare 保留，预取扩展关闭。改动只在项目 `tmp/` 的隔离副本中。

**FLAC已把主要模型敏感点定位到FloatMisc执行端口竞争，优先修复方向是
full-XMM/GPR MOV消除与producer alias；端口反事实缩小69.56%的原周期
偏差。Graph500已确认父服务认证覆盖与Sequencer背压强耦合，但剩余
0.130526 cycles/macro的native误差尚未完成因果份额分解。**
对冻结M1没有复现后续漂移版本的冷owner借旧callback问题，直接扩大
follower等待则严重过算。两个结论都不能推广成DTLB统一主因。

## 基线与证据边界

| 负载 | gem5 CPI | M1 CPI | CPI绝对误差（cycles/macro） | 相对误差 |
|---|---:|---:|---:|---:|
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.89% |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | −13.51% |

口径为 workload-task active cycles／retired macro instructions，
common-start-v1／retired-stream-completion-v1。

这两负载等权 CPI MAE 为 **0.082187 cycles/macro**，MAPE 为 **12.70%**。
五负载历史 M1 MAE=0.191599、MAPE=12.99% 属另一负载集合，不能混用。

| 假设 | 证据及控制 | 当前状态 |
|---|---|---|
| FLAC 固定端口仲裁主导当前残差 | 同端口／同延迟，仅改 round-robin | 不支持主导：总 AE 仅减少4.84%，主要改善在core0 |
| FLAC 执行、依赖或分配语义多收周期 | 中性执行计数；native MOV/zero 与当前模型路径；FloatMisc端口反事实 | 热点已缩小到FloatMisc及MOV语义；精确MOV份额未闭合 |
| FLAC 私有缓存路径差异主导core1–3残差 | 第二阶段守恒账本与逐核AE | core1/core3响应增量很小，不支持统一缓存等待主因；缓存行为差异仍存在 |
| Graph500 新冷owner借旧绝对callback提前返回 | 冻结源码与稀疏模型采样 | 147个冷样本均保留自身完整feedback时长；未复现后续漂移版本的缺陷 |
| Graph500 父服务覆盖及队列背压过于乐观 | 16/8/32容量控制、wait/observe配对、估计覆盖控制 | 已证实强模型交互；简单扩大等待严重过算，剩余native误差份额未闭合 |

所有实验逐核任务宏人口和完整功能输入身份与基线一致。诊断计数与控制
不使用 native fetch/rename/DSB/stall 时刻作为输入。实际gem5目标每核
Sequencer上限为16，因此8/32是敏感性控制，不能作为对齐配置。

### Native参考重新核验

明确选定两例，`validate_fs_oracle_identity.py` 2/2 `valid=true`；
维护的 native validator 发现2/2、通过2/2、PMU scored2/2、
failed=0、pending=0。使用 workload-task PMU 与 conserved Ruby/SLICC
native summaries；未选用 kernel-events path-class cache 字段。

| 负载 | 事件 | native gem5 | M1 | 计数绝对误差 | 相对误差 |
|---|---|---:|---:|---:|---:|
| FLAC | retired BPred miss | 27,158 | 27,706 | 548 | +2.02% |
| FLAC | L1D tag miss | 18,479 | 26,163 | 7,684 | +41.58% |
| FLAC | private L2 tag miss | 3,401 | 4,605 | 1,204 | +35.40% |
| FLAC | LLC tag miss | 1,491 | 1,076 | 415 | −27.83% |
| Graph500 | retired BPred miss | 873 | 1,026 | 153 | +17.53% |
| Graph500 | L1D tag miss | 556,493 | 556,590 | 97 | +0.0174% |
| Graph500 | private L2 tag miss | 277,711 | 277,741 | 30 | +0.0108% |
| Graph500 | LLC tag miss | 277,636 | 277,645 | 9 | +0.0032% |

这些仍是模拟器间 request/branch diagnostics：L1D/L2/BPred为proxy，
LLC为diagnostic，不能升级为真实SPR硬件PMU等价认证。
本次没有做primary retired-load cache-membership验收。

## 已完成中性响应归因

冻结二进制仅改变 `sim.cpi_attribution=true` 与
`core.response_materialized_uop_fast_kernel=false`。FLAC、Graph500 都已
完成回放，实际配置差异只有这两项；功能身份、逐核宏人口、逐核活跃周期
与冻结基线一致，总活跃周期变化均为零。

FLAC 第二阶段响应归因：

| 核 | 新增关键路径周期 | dependency | memory response | SQ capacity | I-fetch |
|---|---:|---:|---:|---:|---:|
| 0 | 463,759 | 304,873 | 66,863 | 91,988 | 35 |
| 1 | 35,496 | 35,425 | 0 | 0 | 71 |
| 2 | 158,118 | 137,954 | 2,184 | 17,914 | 66 |
| 3 | 7,193 | 6,651 | 16 | 0 | 526 |
| 总和 | 664,566 | 484,903 | 69,063 | 109,902 | 698 |

该表是本模型第二阶段增量的守恒分解，不是 FastSim−gem5 的误差分解，
也不能当成删除等待后的重算收益。core1／core3 的当前误差分别为
367,486／307,101 活跃周期，但增量账本相对很小，支持进一步检查执行
基线；不能直接相减后宣称其余周期全部属于 MOV。

## 隔离诊断与复核入口

- 工作目录：`tmp/spr-m1-cpi-diagnosis-20261112/`。
- 源副本来自 `tmp/parent-service-v2-r01-exclusive-20261110/source/`，
  保存于工作目录 `source/`，未在漂移版本上继续叠加。
- 第一份源码差异仅为诊断计数及稀疏 load attempt 采样。
  每个 OpClass 收集事件数量、依赖／翻译／FU／退休等待、目标延迟、
  static zero／managed候选及源依赖容量诊断。**该执行计数没有在功能
  warmup之后reset，覆盖完整retained功能流，含warmup；事件等待可重叠。**
  不能作为本次workload-task窗口的原子人口或CPI因果份额。
- load 采样每4096个源sequence取一条，记录模型时钟；重试／提案可能
  重复同一sequence，不当成已接受请求人口，不作为native timing input。
- 隔离 GCC11 Release／并行LTO16 构建及 `fastsim_tests` 全部通过。
  两负载真实输入的执行诊断版均保持零周期变化，实际配置差异为空，
  诊断二进制 SHA-256 为
  `58da5b6c51f0cab9f57bb42cc0dcbd76dae63218ac11aa15f424c46525fb0710`。
  该可执行文件另存为 `fastsim-instrumentation-neutral`。
- 首次回放被 agent 进程注入的旧 libstdc++ 拒绝；仅为本地子进程配置
  `/opt/gcc-11/lib64` 后成功。没有更改全局 shell 环境。
- [FLAC 中性回放回执](tmp/spr-m1-cpi-diagnosis-20261112/runs/attribution-runtime-fixed/c04-817.flac_s.run.json)
  与 [Graph500 中性回放回执](tmp/spr-m1-cpi-diagnosis-20261112/runs/attribution-runtime-fixed/c04-854.graph500_s.run.json)。
- [实验运行脚本](tmp/spr-m1-cpi-diagnosis-20261112/run_case.py)。
- [已有机制与误差分析](docs/spr-m1-three-workload-cpi-gap-analysis-20261111.md)。

所有并发回放墙钟仅为实验回执，不发布吞吐收益。后续控制使用维护评分
函数及本次重新核验的相同native证据；原输入和参考未发生写入。
额外启动的最终两项控制全量native重复扫描未产出case回执，已停止。
逐控制评分复用本次2/2完整native审计，并重新绑定104个参考文件哈希，
同时检查全部13次成功回放的逐核功能指令、UOP、memory-access和任务宏人口。
配套JSON明确将逐候选独立全量native验收标为false；本次不做默认推广验收。

## FLAC：执行热点与MOV语义

round-robin仅改变 `core.spr_shared_fu_round_robin=true`。

| 控制 | gem5 CPI | FastSim CPI | CPI绝对误差（cycles/macro） | 相对误差 |
|---|---:|---:|---:|---:|
| M1 | 0.284584 | 0.318433 | 0.033849 | +11.89% |
| round-robin | 0.284584 | 0.316793 | 0.032209 | +11.32% |
| FloatMisc unlimited ports反事实 | 0.284584 | 0.294889 | 0.010305 | +3.62% |

周期变化core0=−38,086、core1=−7,756、core2=+772、core3=−5,833，
总计−50,903。74.82%的改善在本来AE仅0.001057的core0；
core1–3仅净改善12,817周期。L1/L2功能计数有个位数变化，
不能宣称所有cache人口严格相等。固定端口选择并非主要残差来源。

中性执行热点（完整retained流，含warmup）：

| 核 | FloatMisc操作事件 | FloatMisc FU等待和（cycles） | 占本核FU等待和 | static zero候选事件 |
|---|---:|---:|---:|---:|
| 0 | 257,371 | 133,801 | 3.40% | 129,304 |
| 1 | 1,015,952 | 22,560,190 | 54.02% | 20,779 |
| 2 | 886,327 | 19,407,073 | 53.91% | 18,790 |
| 3 | 1,041,447 | 23,272,349 | 53.70% | 19,443 |

native同一次采集的switched-core物理退休计数如下；它们包含该核其他任务
和内核成员，**不是workload-task严格人口**，也不是FastSim timing input：

| 核 | native退休MOV alias | native退休zero idiom |
|---|---:|---:|
| 0 | 254,112 | 118,765 |
| 1 | 1,165,770 | 16,461 |
| 2 | 1,063,975 | 17,361 |
| 3 | 1,115,414 | 13,023 |

源码给出明确机制差异：gem5的full GPR/XMM MOV在符合静态形状、无fault、
无allocation group且源/目标物理寄存器未pin时，rename为源producer的别名，
不进入普通IQ/FU，保留allocation/ROB和producer身份；zero idiom在rename
生成常量结果并绕过IQ/FU。`Mxmmmov`使用FloatMiscOp，且仅符合静态条件时
允许full-XMM消除。FastSim目前仍走普通执行路径，managed integer接口
也明确把MOV／zero列为fallback。

因此此前“整数执行／MOV”优先级需要细化：**大热点是FloatMisc及XMM
MOV可能造成的无效端口占用，而不只是IntAlu端口仲裁。** zero候选主要
在已基本对齐的core0，不能统一解释core1–3的高估。
缺少 `.core` 功能事实，不能用FloatMiscOp代替完整MOV资格判断。

最后实施 `FASTSIM_DIAG_FLOATMISC_UNLIMITED_PORTS=1`：
仅移除所有非memory/non-syscall FloatMisc操作的shared-port需求，
保留原latency、dependency、issue width、writeback、retirement。
这是检验端口热点的反事实，不是把全部FloatMisc认定为MOV，
也不完成rename alias／allocation／IQ消除语义。

该控制最终减少730,827 active cycles，把正向周期偏差从1,050,690
降为319,863，缩小**69.56%**。改善发生在原来高估的core1–3，
并不是仲裁控制主要改善core0的同一种结果：

| 核 | gem5 CPI | M1 CPI | 反事实CPI | M1 CPI绝对误差 | 反事实CPI绝对误差 | 相对误差（反事实） |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 0.486513 | 0.485456 | 0.486546 | 0.001057 | 0.000033 | +0.0068% |
| 1 | 0.243246 | 0.283722 | 0.255856 | 0.040476 | 0.012611 | +5.18% |
| 2 | 0.261958 | 0.307139 | 0.280829 | 0.045181 | 0.018871 | +7.20% |
| 3 | 0.245600 | 0.279752 | 0.250730 | 0.034153 | 0.005130 | +2.09% |

两列CPI绝对误差单位均为cycles/macro。core1–3分别减少252,994、
221,808、260,973周期，core0增加4,948周期后反而更接近native。
这一结果已证明**当前FloatMisc端口需求及其与其他操作的共享竞争
是主要模型敏感点**；结合native full-XMM MOV绕过FU的源码，
MOV消除／producer alias是最优先的修复方向。

69.56%不是“真实MOV消除已解释69.56%误差”。该反事实也释放非MOV
FloatMisc的端口，并影响其他操作的排程；它同时作用于warmup和测量，
保留了不同的warmup排程后果。只做这一项诊断不能分离warmup状态与ROI
贡献，更不能完成完整MOV资格／IQ／allocation验收。
cache计数仅小幅改变：task L1D miss26,163→26,089，
L2 miss4,605→4,580、LLC miss1,076→1,076；
缓存计数高相对误差仍在，而CPI显著改善，进一步支持执行语义优先级。

## Graph500：认证覆盖与背压强耦合

| 控制 | gem5 CPI | FastSim CPI | CPI绝对误差（cycles/macro） | 相对误差 |
|---|---:|---:|---:|---:|
| M1，capacity16/wait | 0.966422 | 0.835896 | 0.130526 | −13.51% |
| capacity8/wait | 0.966422 | 0.830097 | 0.136325 | −14.11% |
| capacity32/wait | 0.966422 | 0.788731 | 0.177691 | −18.39% |
| capacity16/observe | 0.966422 | 0.754675 | 0.211747 | −21.91% |
| capacity8/observe | 0.966422 | 0.754382 | 0.212041 | −21.94% |
| estimated-local-parent | 0.966422 | 1.966316 | 0.999893 | +103.46% |

表中精确值以配套JSON／CSV的维护评分函数输出为准。
observe是保留服务结构但不附加父服务等待的控制；退休store prepare始终保留。

| 控制 | 最终active cycles | Sequencer事件等待和 | 已认证load服务 | follower事件 | 父等待和 |
|---|---:|---:|---:|---:|---:|
| capacity16/wait | 26,004,920 | 352,520,170 | 93,648 | 1,288,590 | 176,612,360 |
| capacity8/wait | 25,824,493 | 527,087,068 | 77,563 | 507,785 | 72,550,790 |
| capacity32/wait | 24,537,598 | 94,641,536 | 183,752 | 2,544,665 | 373,875,565 |
| estimated-local-parent | 61,172,504 | 2,561,189,349 | 71,639 | 4,982,459 | 810,038,847 |

这些事件等待可重叠，不能转成CPI份额。capacity16→8新增排队等待
174,566,898 cycles，最终active cycles反而减少180,427；
在observe模式相同容量变更只减少9,133 cycles。
差分交互为−171,294 cycles，占该容量控制总变化的94.94%。
这是**模型内部queue/parent交互**的量化证据，不能认定其占native CPI误差94.94%。

冷服务共556,682，其中93,648认证，462,720因准入不匹配被拒绝；
认证仅约16.82%。source fallback约495万次包括后续读取UNKNOWN父代，
不能与新冷owner拒绝数相加。冻结实现遇到准入不匹配保留UNKNOWN，
后续L1-local follower无法消费该父服务的完成门。

但这并不等于证明“新冷owner提前返回”。3,486条稀疏load模型样本中，
147条为冷路径，115条final issue晚于selected issue；没有冷样本满足
`final response < final issue + feedback latency`。一条明确见证：

| core/sequence | selected issue | selected callback | corrected issue | 完整自身latency | final response |
|---|---:|---:|---:|---:|---:|
| 1 / 536576 | 333,304 | 333,987 | 334,035 | 683 | 334,718 |

旧callback确实在CPU准入之前，但最终响应仍从334,035另算683，
没有借用333,987提前返回。该结论只覆盖冻结代码及样本，不能推广到后续
canonical边漂移版本，也不能从稀疏样本证明所有跨核路径正确。

原采集的 `native-summary-core*.json` 是summary-only：
`full_jsonl_enabled=false`，没有 `native-response-core*.jsonl`，
summary中也没有逐服务或分布式response latency字段。因此它能验证
native cache事件守恒，却不能逐服务对比实际准入、数据返回与controller
释放；缺少这项参考也是当前Graph500因果份额未闭合的明确证据边界。
后续应采集一小段同工作输入的native服务时长诊断，并与retired序列绑定；
这些native时刻只用于独立参考，不能进入FastSim timing input。

估计覆盖控制只给“自身实际准入晚于selected边”的冷load建立
`kEstimatedLocal`父代，保留该请求从自己准入计算的完整feedback时长。
不把估计列入certified population，不新增certified resource ownership，
不改prepare，不引入native timing input。它增加484,734个估计服务，
却多算35,167,584 active cycles，使CPI绝对误差扩大至0.999893。
因此**简单地把UNKNOWN改为已知并让所有follower等到现有完整响应，
不能修复当前Graph500**。现有端到端时长、父完成门、请求backpressure
必须作为同一个有界生命周期重新分解，不能直接补一个统一等待常数。

已有 `ruby.sequencer_load_admission=true` 被冻结版本的buffer TSO兼容性
检查拒绝，未形成有效实验；拒绝回执保留。不取消该检查、不切换store模型
来伪装成单项对齐，也不使用8/32容量拟合目标16。

## 验证、可复现性与交付

- 两份中性执行诊断保持总／逐核周期、任务宏人口、配置和功能身份不变。
- 第二阶段attribution亦在两负载保持零周期变化。
- 估计覆盖的OFF Graph500回放保持零周期变化；定向probe验证冷owner、
  提前／晚到follower及UNKNOWN分支，回归全部通过。
- FloatMisc控制的OFF FLAC回放同样保持总／逐核周期、功能身份和
  实际配置中性；最终隔离 `fastsim_tests` 全部通过。
- `build/fastsim`仍为冻结SHA `dad62e0d…`；
  `configs/spr-m1-retired-prepare.cfg` SHA为
  `711561de8cbb306fcad72ae76b4ed7ab0e3ebb30601263b7467cce86519b310f`。
  本次没有写生产源码、主构建或默认配置，也没有commit/push。
- 诊断覆盖候选单独保存为 `fastsim-parent-coverage-control`，
  SHA `11239d2d060265784e8317c9838a66b2483e80702156d3ca8e41045366aee8cf`。
- 最终FloatMisc控制二进制SHA为
  `4a14ec96144daf4a69acaa590e07d55c98aebce932bdfc31f6f0acf4ebed988a`。
- [Native核验汇总](tmp/spr-m1-cpi-diagnosis-20261112/native-validation/attribution/summary.md)；
  [身份核验](tmp/spr-m1-cpi-diagnosis-20261112/oracle-identity.json)；
  [评分脚本](tmp/spr-m1-cpi-diagnosis-20261112/analyze_diagnosis.py)；
  [定向probe](tmp/spr-m1-cpi-diagnosis-20261112/parent-coverage-probe.cpp)。
- [逐实验数据](fastsim-spr-m1-cpi-diagnosis-20261112.json)；
  [CPI对照CSV](fastsim-spr-m1-cpi-diagnosis-20261112.csv)。
- [源码完整差异](tmp/spr-m1-cpi-diagnosis-20261112/diagnostic-source.patch)；
  [参考输入重新绑定](tmp/spr-m1-cpi-diagnosis-20261112/native-reference-rebind.json)：
  两例各52个绑定参考文件哈希均保持不变。

修复优先级由此次控制收窄：FLAC先补足静态full-XMM/GPR MOV资格和
producer alias／allocation／IQ语义，再与FloatMisc热点做同输入对照；
Graph500先闭合实际准入、数据就绪、controller、callback和slot release
的有界接口及source fallback策略。DTLB/STLB仍需另行补齐，但不能从其
累计等待宣称本次两负载误差已被统一解释。
