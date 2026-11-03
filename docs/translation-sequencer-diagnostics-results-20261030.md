# Sequencer 修复、DRAM/ROB 诊断与 G500 动态翻译采集（2026-10-30）

本轮执行用户给定的机制修复和 D4–D6 核查，保留 P1，不调整延迟常数。新发现raw gem5 stats存在common-end后约1ms执行尾部，不能据它定量认定同窗DTLB/DRAM/ST误差；冻结CPI账的终点独立核查保持有效。Sequencer 修复及 Marian 同源验证已完成；G500 新采集、16核严格来源审计及P1/P2回放评分均已完成；来源通过，精度仍未通过，P2不推广。

## 1. 已实施的修复

source-order Sequencer 预分配原来把普通 deferred store 放在提交前申请队列项，事件执行处也施加了相应门控。现在两处均使用与单遍路径相同的 deferred-store 判定：普通非原子的 store 在 post-commit/selected-service/bounded 路径中排除；PTE、load、atomic 保留正常准入。不会为了接近某个 CPI 调常数。

这项修改消除提交前多算的资源占用，保持既有 detached drain 所有权；它没有新增一套覆盖所有 detached store、load 和 prepare 的统一 post-commit Sequencer 容量模型。因此这里的请求数属于现有核心准入计数，不能直接当作 gem5 全部 CPU 请求总数。

单条 bounded store 的真实模拟回归：旧代码 P2 请求4条而应只有3条 PTE，断言失败；修复后 P1为0、P2为3。新增原子请求保留、观测时序不变、跨 epoch 标量/块摘要两种 ROB 分类回归。构造测试期间的静态锁语义和小 chunk 参数错误均已纠正，旧失败日志保留，最终 `hierarchy-sequential-test.log` 通过；一个中间验证曾与另一链接任务重叠返回126，已通过单独强制重建/执行消除歧义，失败日志保留。

## 2. Marian：相同新 trace 和同一 native 参考

输入复用 `tmp/dynamic-translation-consumer-20261029/source/c04-872.marian_s`。P1/P2都显式 bounded_analytic、typed store callback、store_wait_relief=false、scalar timing kernel；二者仅 hierarchy_walk 不同。完整来源和目标身份沿用已冻结合格输入，指纹见新运行收据；CPI使用维护的 native validator，人口为 workload-task active macro instructions，N=9,417,394。

| arm | gem5 CPI | FS CPI | 相对误差 | CPI绝对误差／单案MAE |
|---|---:|---:|---:|---:|
| P1 | 4.947665 | 6.032176 | +21.92% | 1.084511 |
| P2 修复前 | 4.947665 | 6.107873 | +23.45% | 1.160208 |
| P2 修复后 | 4.947665 | 6.046566 | +22.21% | 1.098901 |

修复前P2引用上一轮保存的同源评分，修复后两项见本轮 `validation-p1/`、`validation-p2/`。这是单案结果，不构成六项聚合或精度通过。

| 同源机制观测 | P1 | P2修复前 | P2修复后 |
|---|---:|---:|---:|
| active cycles | 56,807,382 | 57,520,249 | 56,942,894 |
| 核心准入 Sequencer requests | 3,332,116 | 5,169,748 | 3,333,957 |
| PTE读 | 0 | 1,841 | 1,841 |

修复后 Seq 差值 **1841=全部PTE读**，不再额外算入约183.5万条普通store。P1周期逐位保持原值；P2移除577,355周期，消除了原来712,867周期增量的约81%。但仍比P1多135,512周期，即0.014390 CPI；**没有达到“只差约0.002”的预期**，不能把全部剩余量归因于PTE直接等待。当前PTE直接额外等待观测为19,866周期，不等于总CPI暴露量。

尝试给P1/P2同时打开 `ruby.sequencer_load_admission` 建立相同求解器对照时，维护配置明确拒绝其与typed store callback组合；两条失败收据保留，未绕过guard，未把它们列为有效实验。当前结果证明错误准入的主要作用，不证明所有求解器差异已被完全隔离。

## 3. D4：控制器读服务分解

开启 `sim.cpi_attribution=true` 后原 `dram_request_lifecycle` 已有数据，但它按核心累计，并把共享阶段投影到修正发射时刻，且响应包含cache/NoC等后续路径，不能直接称为native控制器延迟。

新增 `dram_controller_reads`：每个controller对每个接受的DRAM读burst累计原始arrival、读命令service、DRAM data-ready及相邻差值。data-ready排除controller frontend/backend固定返回延迟与cache/NoC传输；FRFCFS使用原始请求arrival而不是后移的selected_at。值状态随候选复制/回滚、逐channel接受安装，测量起点reset；排除写队列和同步dirty writeback，包含真正读DRAM的store写分配及ifetch。pending-memory使用不同控制器，明确标记unavailable。没有保存庞大逐请求文本日志。

**终点核查发现原始stats含额外约1ms执行：此表不是同终点精度对照，不能用于验收排队或PTE流量差。** CPI采用独立冻结common-end账，保持前表结果。

| 不同终点的物理控制器读诊断 | gem5最终stats | P1共同终点 | P2修复后共同终点 |
|---|---:|---:|---:|
| DRAM read bursts | 441,068 | 404,270 | 404,278 |
| arrival→service均值，cycles | 38.9263 | 69.5716 | 69.6492 |
| service→data-ready均值，cycles | 52.5285 | 54.0000 | 54.0000 |
| arrival→data-ready均值，cycles | 91.4548 | 123.5716 | 123.6492 |

FS的404270=403918数据读+352取指读，P2为403926+352；16个controller均通过端点和相邻阶段守恒。逐controller原始值和native同controller对照在 `marian-mechanism-analysis.json`。

native来自同一source唯一stats dump的 `dram.{readBursts,totQLat,totMemAccLat}`，除333tick/cycle。gem5定义是DRAM **read burst**，不是读写混合平均。两边物理请求人口不同，native还可包含页表、投机等流量；约31cycle的排队均值差混有终点差异，**不能认证为同窗排队偏长**，更不能乘某一方请求数宣称解释了残差或作为调参依据。

实际证据：runner保留 `sim_quantum=1000000000` ticks；`src/sim/sim_events.cc` 的 `exitSimLoop` 在请求tick上再加simQuantum，O3继续运行到全局退出。Marian冻结终点59762670470370，实际退出59763671067106，差1,000,596,736ticks；G500为39181823695260→39182823716572，差1,000,021,312ticks。两项 `stats.txt:simTicks` 均精确等于actual_exit−common_start，而非functional_common_end−common_start。oracle/time-accounting的全部4/16核common_end仍绑定冻结功能终点。见 `reference-endpoint-audit.json`。

因此原始DTLB miss、ST Sequencer直方图、DRAM请求数/row hit/avgQLat也不能直接作为FST同窗对照；“G500多187k读必由PTE解释”或“Marian真实同窗ST平均184cycles”都需新同窗证据。新FST和冻结CPI/native summary可保留；不更改既有producer/runner身份、不倒减raw计数或借此调整CPI参考。严格的native控制器机制验收需后续在共同终点保存原始统计快照或修正退出尾部后再采；本轮该门槛标为未认证。不能用包含尾部的ST总数解释FST内regular store人口差，也不能把包含尾部的DTLB miss当作严格页大小验收计数。

## 4. D5：ROB槽释放者与store自身响应

新增 `rob_capacity_blockers`，在ROB容量门控处分类实际释放该槽的predecessor：unknown/other/load/store/atomic/local_no_access。跨chunk保留独立sequence，避免块摘要不写调度ring时读到旧类别。计数不改变调度；每类事件与stall周期之和分别与现有ROB满事件/原始stall计数守恒。**槽释放者可以继承更早指令的退休等待，并非最终根因。原始stall可重叠，禁止求和当作CPI。**

Marian P1：

| 槽释放者类别 | ROB容量阻塞事件 |
|---|---:|
| other | 4,337,904 |
| load | 2,274,512 |
| store | 1,591,109 |
| local_no_access | 714,404 |
| unknown / atomic | 0 / 0 |

普通store UOP共1,835,406；其中1,835,271在模型最终response前完成，1,835,270在此前退休（99.9926%）。这里response是最终SQ释放边：OriginalDemand对应完整服务返回，Publish可提前释放，并非所有角色的物理权限返回。136条在该边时或之后退休，不单凭时点认定它们由自身response阻塞。store槽释放者的1,591,109次阻塞中，1,591,072次释放者自身在该边前已经退休。

同窗OriginalDemand的L1-miss store有211,916条，属于上述普通store子集；即使将全部136条晚退休都分给这个子集，仍至少211,780条（99.9358%）在其完整服务response前退休。这是由集合包含关系得到的保守下界，不冒充新增逐miss精确配对计数。**现有数据不支持普通bounded store普遍占着ROB直到miss结束的猜测。**

已有互斥关键周期账（该模型的interval retirement-frontier归因）给P1：SQ容量12,209,934、依赖10,636,051、内存响应28,723,411、取指303、ROB容量0，合计51,569,699；与其总关键周期守恒。它与raw ROB/SQ满事件有不同定义，不可互换，也未认证与gem5各stall统计同单位。故“raw ROB满次数大于SQ满次数”不能单独支持“ROB是主要CPI背压”。

## 5. D6：writeback数值吻合不能排除M7

详见 [L2计数审计](l2-writeback-accounting-audit-20261030.md)。专用 `private_l2_tag_misses` 来自 `l2_data_demand_tag_misses`，不会直接增加writeback fallback；`l2_writebacks` 是dirty replacement victim，且当初相减混用了task-demand与physical人口。native对应WriteBack transition也不记demand miss。不能把224247从专用miss里减掉，更不能凭“差75”排除分类/服务问题。只修正事件字典中漂移的文字来源，保留proxy，不改变预测或改写旧报告。

## 6. G500来源、同源评分与磁盘保护

使用同一新producer，SHA256 `ed296ff1965a9f81b7cbd67799dba66fe16b5408cebf12dae364c78daed46961`，无需重编gem5。旧checkpoint producer不同，因此新建checkpoint，旧FST和checkpoint不覆盖。

首次checkpoint命令因进程局部运行库路径缺失而在动态加载器退出127，未开始模拟；原失败目录保留。retry1仅为子进程补足Python3.11和GCC11运行库，ldd/--build-info验证后启动。新checkpoint已通过，采集以共同终点结束；source-ready、目标身份、16核全流翻译覆盖均通过。原冻结审计目录缺少hash helper导致第一次审计失败，保留该输出后，用字节一致的维护审计代码和冻结hash helper恢复，16/16通过，未放宽门槛。

旧350个来源文件及6个checkpoint文件逐项保存完整hash。启动空闲约284GB，全局FST246.11GB；单case FST预算32GB、checkpoint2GB、辅助10GB，提前停止空闲31GB、全局FST395GB、政策底线30GB。producer进程组、panic/fatal与wall-time都有监控；sidecar持续gzip压缩。新FST合计17,113,633,280bytes，16个gzip翻译文件445,062bytes，所有严格翻译unknown=0。旧350个来源文件/6个checkpoint文件及冻结producer/runner/plan逐项hash验证不变。新sidecar中按VA半区划分的user定义12bit共17,318条、21bit共3条（均在core0），kernel定义12bit517条、21bit362条，非分页特殊定义23条；这是全部预热与测量的逐核字典定义数，非访问次数或CPL人口。审计完成时空闲约266.58GB，未删除旧数据。最终状态见 `g500-final-status.json`，原pipeline因缺helper退出1与恢复postprocess退出0分别保留。

G500两个模拟均退出0，参数核查恢复后各130项全部匹配，维护native validator均1/1 passed、failed/pending为0，指令人口匹配；与来源/硬件检查分开记录。P1最初审计因硬编码switch0不能识别C16的switch00退出1；现维护工具按数值序号解析core/cache-controller，缺失或重号仍拒绝。4个新单测RED→GREEN，真实C4/C16各130项匹配。原失败收据保留，同输出hash绑定的profile-recovery-receipt记录恢复，未重新拟合参数或改预测。原并行评分launcher因P1早期guard返回1，P2的validator实际退出0；独立恢复P1的validator退出0，两个最终summary均通过。

| G500 C16 arm | gem5 CPI | FS CPI | 相对误差 | CPI绝对误差／单案MAE |
|---|---:|---:|---:|---:|
| P1 | 2.313307 | 2.053653 | -11.22% | 0.259654 |
| P2 | 2.313307 | 2.069740 | -10.53% | 0.243567 |

P2相对P1增加1,285,904 active cycles，即0.016087 CPI，绝对误差相应减少0.016087，仅弥补P1残差约6.2%；原PTE主因假说未获本次模型A/B支持，也不据此认证native PTE流量与模型一致。

G500模型内部流量结果：

| 模型观测 | P1 | P2修复后 |
|---|---:|---:|
| timing DTLB miss | 65,931 | 55,366 |
| 实际物理PTE walk | 0 | 55,366 |
| 物理PTE读 | 0 | 221,004 |
| PTE L1/L2/shared | — | 217,737 / 1,780 / 1,487 |
| 模型DRAM read bursts | 712,854 | 714,162 |
| 核心准入Sequencer请求 | 34,245,814 | 34,466,818 |

P2请求增量221004恰等于PTE读；短路径walk460条，221004=55366×4−460，实际页表深度已生效；synthetic和fixed fallback均为0。DRAM总读只增加1308，没有产生原先预期的187k额外读。

新native冻结摘要逐核/逐scope的已跟踪 `memory_read_transactions` 合计714174，而最终raw DRAM `readBursts`为901498，差187324。后者覆盖全局物理流量及额外1ms，前者是冻结已跟踪需求cohort；**这个跨终点/事件域的差不是PTE计数**。P2总量714162与714174接近也不认证同人口一致，因为模型总量包含PTE，native摘要是已跟踪cohort。实际native同窗PTE流量仍不能由差额重建。

## 7. 验证与边界

`cmake --build build -- -j16`、直接 `fastsim_tests`、动态reader/DTLB/hierarchy专门入口、controller observer专门入口、13项native-validator单测通过。fresh-context代码审核通过，无遗留发现。Marian纯观测版本与最小修复版P1/P2的time_accounting、thread_state、CHA和instruction CHA完全相同；实际运行证明本轮观测不改变Marian的两个arm的模拟时序；不把它扩大成全矩阵吞吐或等价证明。

当前保留P1；修复后的P2仍为显式开关，不更改默认配置。Marian残差仍未定量闭合，不根据某一原始计数或一个假设宣布根因已找到。用户给定d1b/d1c是诊断证据，本轮没有重做六项或重新采纳已作废的legacy d1-se-atomic。约0.06 CPI只是在既定miss数和服务假设下的直接串行开销估算，不是对所有间接缓存/排队效应的严格上界。

无commit/merge/push，无旧数据删除，无全矩阵、正式吞吐或硬件等价通过声明。全部收据与脚本在 `tmp/translation-sequencer-20261030/`。

## 8. 两案验收结论

本轮只覆盖两个新来源，Marian C4与G500 C16。每个arm各N=2的case-equal指标如下，不代表完整六项矩阵。

| arm | CPI MAE | CPI MAPE | CPI WAPE | signed bias | P50/P90/max APE |
|---|---:|---:|---:|---:|---:|
| P1 | 0.672083 | 16.57% | 13.38% | -4.55% | 16.57% / 20.85% / 21.92% |
| P2 | 0.671234 | 16.37% | 12.88% | -3.94% | 16.37% / 21.04% / 22.21% |

MAPE/MAE为case-equal，WAPE/bias按通过人口检查的task macro instructions加权。

两案MAE变化很小，Marian仍有约1.10 CPI残差、G500约0.24。保留P1和Sequencer语义修复，P2保持opt-in；本轮不采纳精度结论、不继续其他矩阵/S2/S3。D4已具备模型控制器观察，native同窗DRAM/DTLB/ST机制门槛因原始stats退出尾部仍未认证。后续先补同终点/cohort原生观测，再定位服务或重叠机制，不能继续把187k差额或全局ST均值作为拟合目标。

最终证据：`final-comparison.json`、`reference-endpoint-audit.json`、`marian-mechanism-analysis.json`、`g500-final-status.json`与各validation目录。来源、精度和硬件等价始终分开。

## 9. 后续 P1：共同终点 raw stats（2026-10-30）

共同终点统计包装入口已实施，Marian C4与G500 C16已从现有checkpoint完成新采集。两案raw快照与各自新FST共同终点精确同tick，维护版来源、独立目标身份及4/4、16/16核严格翻译审计通过。

**旧源确定性复现未通过**：分阶段KVM恢复后的绝对窗口、逐核指令人口与FST哈希不同。因此新快照不能替换本报告的旧oracle或用于重评本报告FastSim预测；原CPI表和旧来源原样保留。没有新的模型精度、吞吐或硬件等价通过结论。实施、失败路径和逐项验收见[共同终点stats报告](common-end-stats-results-20261030.md)。

## 10. 新共同终点来源的成对重放

已对后续P1采集的两份新FST完成四组P1/P2重放与独立同源评分，并对G500固定CPU范围复核目标状态。新结果不能与本报告旧输入聚合；新P2没有改善两案MAE，PTE A/B包含makespan与队列重排效应，不是纯PTE成本测量。详见[新来源重放与机制分析](common-end-replay-analysis-results-20261030.md)。
