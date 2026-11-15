# 退休路径 I-fetch 与数据共享 Sequencer 信用

状态：默认关闭的功能切片已接入并通过专项及一次统一回归；真实四案精度、
端到端吞吐和跨 Q 未释放信用仍待验证。本切片不解决 store prepare 与 head
demand 的独立人口，也不表示完整 Ruby Sequencer 对齐。

## 目标与范围

正式四案原配置均开启 `fetch_supply_lower_hierarchy`，原数据反馈对每核
`ruby_sequencer_max_outstanding=16` 限流，却在反馈中跳过同一 Sequencer
上的 I-fetch。按既有 OFF 输出，FLAC、Graph500、Marian、XZ 分别产生
3940、731、8181、4153 个模型 I-fetch 下层请求。这些是模型人口，并非
native 请求数或精度证据。当前四案的 instruction address mode 为
`modeled`，取指物理行身份仍采用既有模型地址，不宣称精确的真实物理行。

新增 `ruby.sequencer_ifetch_admission`，默认 false。开启后，模型已经
产生并进入现有层级的退休路径 I-fetch，与同核数据请求共同预约原有
Sequencer callback 最小堆，不新增独立的 I-fetch 容量池；模型响应延迟
决定释放时刻。满额等待增加 fetch 反馈并可能影响后续 pipeline 时间。
每核接受前沿同时约束数据和 I-fetch：在已选定的窗口接受顺序中，若新的
模型请求时间早于前一已接受请求，夹到前沿并单独计数，不能把未来释放
当成过去可用。前沿和原 callback 堆随最终反馈提交到下一 Q，不做逐周期
CPU 推进或整窗迭代。

这是**固定接受顺序的时序近似**：层级的功能访问仍按现有两阶段流程先
回放，信用延迟通过 feedback 注入；它没有重排已经提交的 cache/目录状态，
也不表示复原 gem5 的真实动态接受顺序。旧数据路径仍可能把 legacy store
prepare 计入 demand；旧数据路径和物理行近似均保留。总输出及逐核输出
分别提供 I-fetch 请求、满额次数/周期和接受前沿夹高次数/周期。

首版要求 `interval_weave/time_epoch`、稀疏 response feedback、已有 I-fetch
下层路径、非零需求信用及单遍时序反馈；与 source-order Sequencer、独立
coalescing/prepare/store group、预取事务、页表 hierarchy walk、其他因果
或多次修复求解器互斥，并显式拒绝 DVFS 和推测 I-fetch。既有 private preview
及 materialized fast kernel 保持兼容。OFF 不创建接受前沿也不改变原数据
准入算法。

## 已做验证

`tests/test_spr_ifetch_sequencer_runtime.cpp` 使用真实 Simulator 和退休功能
trace：16 个不同线数据 load 占满既有 16 项 demand 信用，再产生冷 I-fetch。
它检查 I-fetch 真实层级请求与 Sequencer 人口一一对应、满额等待传到 fetch
边、退休与层级请求人口守恒；以 chunk=8/128、private preview ON/OFF 和
materialized fast kernel ON/OFF 覆盖合法组合。64 项非绑定对照不应制造
buffer-full 等待。专项结果在 `tmp/spr-ifetch-credit-20261105/restored-test.log`；
统一构建及 `fastsim_tests`、C4 store、FR、L2、STLB 和 trace-open 回归结果
在 `tmp/spr-alignment-v2-c4/ifetch-seq-*.log`。

**跨 Q 仍未单独证明。** 初始 chunk=8 的 fixture 经 checkpoint 审计发现
全部 17 UOP 实际在同一 checkpoint，不能仅凭 chunk 大小说跨 Q。将 epoch
缩短至 4 周期后 data 的最大 outstanding 只有 12；再以 ALU 拉开冷取指与
数据窗口，未观察到取指被满额信用阻塞。失败探针保存在
`tmp/spr-ifetch-credit-20261105/cross-q-probe.cpp`，不作为通过测试。
下一次验证需要同时证明前一 checkpoint 留有未释放槽、后一 checkpoint
的取指在该释放边之前尝试且实际被该边约束，而非继续调参只追求某个数字。

## 四案同源 OFF/ON 诊断

同一冻结程序 SHA-256
`75dbf73480148e07482bff4b38af383307e29f3396f09095b3541b4f0a4dd60e`，
按四案原配置、功能预热、线程计划及 MMIO 开关并行回放，OFF/ON 唯一配置
差异为该 I-fetch 开关；功能身份、任务和逐核人口均与同源旧输出一致。参考
复用此前已验证的同源 native 结果，新输出没有独立重评分。现有
`roi_edges=false`、`macro_boundary_complete=false` 限制保留；结果为既有
口径的诊断，不声称严格同起止窗口或硬件等价。

| 负载 | gem5 CPI | OFF CPI | OFF 绝对误差 | OFF 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FLAC | 0.281872 | 0.321041 | 0.039169 | +13.90% | 0.345467 | 0.063595 | +22.56% |
| Graph500 | 0.652711 | 0.754031 | 0.101320 | +15.52% | 0.754682 | 0.101971 | +15.62% |
| Marian | 1.428304 | 2.934776 | 1.506472 | +105.47% | 2.932889 | 1.504584 | +105.34% |
| XZ | 0.603524 | 0.647965 | 0.044441 | +7.36% | 0.668837 | 0.065313 | +10.82% |

等权 CPI MAE 为 0.422851→0.433866 cycles/macro，MAPE 为 35.56%→38.59%。
ON 的四案逐核 I-fetch 准入数均等于各核模型 I-fetch 下层请求数，且总需求
请求增量分别恰为 3940、731、8181、4153。I-fetch buffer-full 次数分别
为 6、0、4、1；整个数据与 I-fetch 接受前沿的夹高次数为 290236、37405、
1968884、3090979，夹高周期累计为 21670240、730636、22280771、
103093539。这些周期是逐请求等待和，不能直接视为 CPI 的关键路径；但其
人口远大于取指满额次数，说明需要单独检查接受顺序近似，不能把 CPI 变化
直接归因于真实 I-fetch 信用压力。当前保持默认 OFF。

| 负载 | 退休 L1 miss 模型 OFF→ON | native known / unknown | 退休 L2 miss 模型 OFF→ON | native known / unknown | 退休 L3 miss 模型 OFF→ON | native known / unknown |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| FLAC | 7690→7692 | 3898 / 0 | 1166→1170 | 306 / 963 | 209→209 | 109 / 971 |
| Graph500 | 556357→556356 | 276155 / 0 | 278402→278398 | 62559 / 169751 | 277766→277766 | 52703 / 179114 |
| Marian | 81710→81709 | 43387 / 4 | 18696→18692 | 16461 / 1856 | 16553→16562 | 13987 / 2692 |
| XZ | 60492→60500 | 23616 / 27 | 13605→13603 | 1818 / 578 | 12729→12730 | 978 / 690 |

只有 FLAC 与 Graph500 的 L1 native unknown 为零，它们的 OFF/ON 相对误差
分别为 +97.28%→+97.33% 和 +101.47%→+101.47%，仍是资格证据不完整的
暂定值；其他列因 native unknown 非零不可评分，不将 unknown 当成 hit 或
miss。八进程并发共享宿主和 I/O，墙钟不能作为隔离吞吐结论。各案逐核
人口、完整身份、PMU fallback 及原始计数在
`tmp/spr-ifetch-seq-v2-c4-20261105/paired/comparison.json`。

## 接受前沿的范围与替代方案

源码核对显示，开启该选项后不仅 I-fetch，**每条旧数据事件**也在
`compute_core_timing_feedback_impl` 中把原有 proposed issue 夹到同一
`sequencer_acceptance_frontier`；后续 MSHR 门控还会继续推进前沿。这样
改变了原数据路径对模型 issue 逆序的处理，而不只是增加 I-fetch 信用。
每个取指事件至多贡献一次夹高，所以 FLAC、Graph500、Marian、XZ 的
非取指夹高次数分别至少为 286296、36674、1960703、3086826。这是基于
模型计数的下界，不把逐事件等待之和当作 CPI 归因。

不能直接删除数据前沿：现有 Sequencer 最小堆只保存每槽 callback，没有
保存该槽 reservation 的开始边。如果后来处理的模型请求时间早于已经预约
的未来 I-fetch，则仅凭 callback 无法判断该槽在更早时间是否空闲，可能
误判或重复使用信用。候选方案是在原两阶段反馈里将每核 demand 的 16 项
信用扩展为有界 `[admission, callback)` 区间账；I-fetch 和数据共用该账，
允许模型事件倒序到达时插入确实空闲的间隙。只按已证明的未消费事件
时间水位回收旧区间，不能用最新处理时间冒充安全水位。现有
`SprFillCalendar` 说明了有界区间预约和插空方法，但其 `finish` 允许
延长后与已预约区间重叠并计数，不可直接当作严格的 Sequencer 信用。

时长先用本模型预测；反馈若延长某段并与未来已接纳预约冲突，必须保守
丢弃整个试窗口并最多一次回退原路径，独立计数，不能静默超额或做多轮
收敛。还要有常见无冲突快速路径和固定的宿主记录上限；无安全水位、记录
溢出和时间回退均需明确计数或回退。该替代方案尚未接线；需用真实 Simulator
逆序、满额、同周期、延长冲突及跨 Q 验证后，另行评估四案误差和隔离吞吐。
在此之前该 I-fetch 选项保持默认 OFF。

## 区间账隔离原型和验证边界

`tmp/spr-ifetch-interval-credit-20261105/work/` 是独立源码快照中的实验
草稿，未迁入主工作树。它对每核共享 I/D 信用维护有界区间，以模型预测
服务时长预约；反馈延长若与已预约区间冲突、或宿主记录达到固定上限，
计划丢弃当前反馈并最多一次切回已有前沿路径。区间记录上限由现有
ROB、LQ、SQ 与 Sequencer 容量构成，满时回退，不扩大被建模硬件资源。
每次预约最坏需扫描有界区间记录，吞吐开销尚未实测。

真实 Simulator 的逆序夹具曾在旧前沿上验证非零夹高；隔离原型允许
年轻请求进入有空余信用的较早时段，保留退休和层级请求人口，且此夹具
无回退。独立的一项信用夹具在满额时发生阻塞，观测最大占用为 1，且
未丢退休或 L1 请求。以上两项在
`tests/test_spr_ifetch_interval_runtime.cpp` 的同一次运行中先通过；
**整个测试程序尚未通过**：第三项尝试制造反馈延长冲突，一次在单信用
下没有发生插空，调整为两信用后虽然出现一次插空，冲突和回退仍为零。
该负例因此无法验证回退的时序和人口守恒；并非测试通过或证明无冲突。
构建日志在 `../green-build3.log`、`../green-build4.log`。

该草稿目前只允许 FCFS 且关闭 private preview，不能覆盖正式四案的
FR-FCFS 与 preview 配置。跨 Q 水位、预热边界、反馈冲突单次回退及真实
负载吞吐都尚未验证；不能迁入主路径或宣称已修复生产配置。先停止对
合成夹具的参数试探，后续须构造有可核验反馈延长的真实路径，或在现有
两阶段约束下选择更简单且能证明信用与流量守恒的方案。
