# SPR 共享 STLB 有界功能状态增量

来源为当前 gem5-spr `arch/x86/tlb.cc` 的 `TLB::insert`/`lookup`，以及新 FLAC C4 的 `config.ini`。每核 I 和 D 的 next_level 指向同一 STLB；ITLB/DTLB 下级命中延迟配置分别 7/6 cycles。STLB 是 2048 条、`spr_shared_domains=true`：两域、各 128 组、每组 8 路，4K 用 VPN[7] 选域与 VPN[6:0] 选组；2M/4M 选域0，1G 选域1；所有大小在对应组竞争。gem5 明确将大页索引称为未校准代理，而非真实硬件已验证结构。

## 已授权退休顺序近似的主路径切片

在明确允许退休输入顺序近似后，
`tmp/spr-id-stlb-retired-order-20261105/work/` 首先实现默认关闭的
I/D 共享候选，审查差异在同级 `retired-order-id-stlb-review.patch`；
现已合入主工作树并注册专项测试。该模型按退休记录顺序即时填入每核共享 STLB，
只在模型产生新取指块时查模型 ITLB，同宏多个 UOP、同块新宏不重复
制造查询。可绑定功能控制清 I/D；缺少功能事实、上下文或 root 不一致
等情况标记 unknown 并保守清共享知识。下文对严格事件时间和失效闭包的
缺口仍然成立；模型命中不能当作 gem5 动态 TLB 观测。

本实验的 ITLB 仅支持 **4K、256 项、8 路、32 组**，几何固定，不支持
将其他 ITLB 几何声称为已映射；`spr_id_stlb_retired_order` 校验要求
退休前端、建模 L1I 供给和现有 D-side 代理。依据为新 FLAC 的
`config.ini` 中 `[board.processor.switch0.core.mmu.itb]` 的 `size=256`、
`itlb_4k_ways=8`、`next_level_hit_latency=7`，以及
`gem5-spr/src/arch/x86/tlb.cc:171` 的组映射和 `:573` 的下级命中延迟。
实验配置 `core.spr_itlb_stlb_hit_latency` 显式指定命中周期，默认 7，
并输出实际值和固定几何；ITLB/STLB miss 借用既有 D-side 的固定 walker
延迟，是近似，不代表真实 I-side walker 竞争或时序。

真实 Simulator 专项验证 I→D/D→I、共享组 8 路驱逐、逐核隔离、绑定
失效、跨 ASID 和缺失事实、跨页、同块和多 UOP，以及退休人口。模型
hit/miss 在最终 fetch/retire 的一组见证分别为 1199/1238 和 1242/1246；
命中延迟从 7 改为 15 时最终 fetch 延后 8 周期。`fastsim_tests` 和既有
D-side 动态层级测试通过。三组小型合成输入的 OFF 对照逐 UOP 关键时序、
主要人口与缓存计数与独立原源码一致；这不是四案全流 OFF 等价或吞吐
验收。主工作树统一构建、`fastsim_tests` 和 15 项相关专项回归均已通过；
首次专项运行因补丁漏带 CMake 注册而未启动测试，补齐注册并重新配置后
已通过。数据与完整审查差异保留在上述 tmp 目录。

该原型仍不证明 `.ixlat` 历史来源在所有同 root 的重映射下可由当前
Atomic/QEMU 边界等价生成，未解决未绑定及跨核 MMU 失效，未建 DSB
物理 lease 与完整多页/PCID/global/walker；这些限制不能靠 OoO 命中、
时间或未绑定命令数量猜补。隔离吞吐尚未测试，不能据合成测试宣布完成对齐。

## FLAC C4 退休顺序 I/D 代理对照

冻结二进制 `cdb4f1c5…` 的 OFF/ON 同一完整 v2 输入身份、预热、线程
计划、逐核退休人口，配置只改变 `core.spr_id_stlb_retired_order`；
两侧均启用原 D-side 代理，I-side STLB hit 延迟配置为 7 周期，miss 使用
现有 D-side `page_walk_latency=12` 的快速代理。gem5
参考 CPI 为 0.2818719356。由于窗口起点未严格对应，下表是诊断：

| 共享 I/D | FastSim CPI | 绝对误差 cycles/macro | 相对误差 | ITLB hit / I-side STLB hit / miss / unknown |
|---|---:|---:|---:|---:|
| OFF | 0.3197865156 | 0.0379145800 | 13.451% | 0 / 0 / 0 / 0 |
| ON | 0.3200524654 | 0.0381805298 | 13.545% | 45,650 / 0 / 468 / 96,326 |

ON 的 D-side hit/miss/fallback 为 1,033/307/208，清表 85；OFF 相应
为 1,033/307/208，清表 21。unknown 包含未发新块查找但功能身份无法
确认的宏，不能作为原生 miss 或与查询次数简单相加求命中率。该案没有
模型 I-side STLB hit，当前精度诊断略差。隔离分类回放中，96,326 次
unknown 的首个失败条件全部为 `.ixlat` 显式 global 页被当前非 global
模型子域排除，逐核为 70,399 / 1,578 / 22,378 / 1,971；这是模型
覆盖限制，不是缺 `.ixlat`。首失败统计不排除这些记录同时存在其他
后续原因。分类程序与原 ON 的 v2 身份、CPI、逐核人口及原 I/D 计数一致；
宿主 wall 及 chunk 队列等待次数有变化，不是目标模型事件。证据在
`tmp/spr-id-stlb-unknown-reasons-20261105/`。OFF/ON 的任务退休 L1 load miss 估计
为 7,691/7,695，已验证同源 Ruby L1 known 为 3,898、unknown 为 0，
暂定误差 97.31%/97.41%；事件资格仍为 incomplete-evidence。L2/L3
native unknown 为 963/971，不能评分。新输出的 OFF/ON 均已通过独立
native validator 的来源与退休人口检查，不能因此声称硬件等价或精度达标；
并发时间不能用于吞吐判断。

同一冻结二进制与相同规则完成其余三案，逐案 OFF/ON 的 v2 身份、逐核
退休人口一致，配置均只差 I/D 共享开关。全部四案诊断如下：

| 负载 | gem5 CPI | OFF CPI | OFF 绝对误差 | OFF 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 | ON ITLB hit / I-STLB hit / miss / unknown |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| FLAC | 0.281872 | 0.319787 | 0.037915 | 13.45% | 0.320052 | 0.038181 | 13.55% | 45,650 / 0 / 468 / 96,326 |
| Graph500 | 0.652711 | 0.753601 | 0.100890 | 15.46% | 0.754191 | 0.101480 | 15.55% | 0 / 0 / 8 / 15,860 |
| Marian | 1.428304 | 2.925985 | 1.497681 | 104.86% | 2.915243 | 1.486939 | 104.11% | 17,290 / 0 / 969 / 41,323 |
| XZ | 0.603524 | 0.652280 | 0.048756 | 8.08% | 0.663062 | 0.059538 | 9.87% | 28,023 / 0 / 5,231 / 230,381 |
| 四案等权 | — | — | **0.421310** | **35.46%** | — | **0.421534** | **35.77%** | — |

最后一行是 CPI MAE 与 MAPE，非 WAPE。四案均没有模型 I-side STLB
hit；unknown 含非查询宏，不能由上述数字推算原生命中率或输入缺失率。
四案模型退休 L1 load miss OFF/ON 为 7,691/7,695、556,356/556,356、
81,712/81,710、60,479/60,492；native L1 的 Marian/XZ unknown 为 4/27，
不能评分，FLAC/Graph500 虽 unknown 为 0，事件资格仍是
incomplete-evidence；各案 native L2/L3 均有 unknown。四案 OFF/ON 新输出
均已通过维护的 native validator 来源及退休人口检查，但这不表示硬件
等价或误差达标。结果不支持
默认开启此切片。源码一致性、逐案身份及模型结果在
`tmp/spr-id-stlb-flac-c4-20261105/`。

同一冻结程序、CPU 112–119、四个 trace-open worker 的 FLAC 串行单次
OFF/ON 初测：端到端 198.82/197.72 秒，功能预热 8.701/8.377 秒，
模型测量 57.006/56.007 秒，输入身份计算 57.100/57.309 秒，峰值 RSS
1,569,332/1,509,516 KiB；逐核人口、v2 身份与上表一致。端到端和测量
ON/OFF 吞吐比分别为 100.56%/101.78%。同时有其他案件 native 验证
占用共享宿主，单次配对不足以证明加速或满足四案稳定吞吐门槛；结果在
`tmp/spr-id-stlb-throughput-20261105/`。

## 同 ASID global 页近似

默认关闭的 `core.spr_id_stlb_allow_global` 在 I/D 共享近似已开启时，
允许功能事实已知、4K、PCID0 且 ASID/root 一致的 global 页进入同核、
同 ASID 共享表；上下文或已绑定失效仍保守清空，因此并不实现 gem5 的
跨地址空间 global 保留语义。其他不满足身份和几何的情况仍为 unknown。
真实 Simulator 专项覆盖 I→D/D→I、控制失效、ASID 切换、root 不符、
非 global 不变和退休人口。冻结二进制 `c1dff1c2…` 的 FLAC OFF/ON 只
改变该选项，完整 v2 身份和逐核退休人口一致：

| global 近似 | FastSim CPI | 对 gem5 0.281872 的绝对误差 | 相对误差 | I hit / I-STLB hit / miss / unknown |
|---|---:|---:|---:|---:|
| OFF | 0.320052 | 0.038181 | 13.55% | 45,650 / 0 / 468 / 96,326 |
| ON | 0.317689 | 0.035817 | 12.71% | 45,674 / 0 / 479 / 96,069 |

D-side hit/miss/fallback 为 1,033/307/208 → 1,036/385/127；模型退休
L1 miss 估计为 7,695 → 7,693，对同源 Ruby known 3,898 的暂定误差
97.41% → 97.36%，事件资格仍为 incomplete-evidence。native L2/L3
unknown 为 963/971，不可评分。新输出尚未独立 native 重评分，原生参考
复用同源已通过验证报告；窗口起点非严格相同，并发时间不用于吞吐判断。
开启仅减少 257 次 I unknown，仍有 96,069 次；同源诊断显示其中
95,961 次的首个限制是页大小不是 4K，另外 108 次为 walk root 不符。
前者逐核为 70,291/1,493/22,291/1,886，当前尚未区分 2M/4M/1G，
也不能排除这些记录同时存在其他不满足条件。诊断二进制与原 ON 的
身份、CPI、人口及原有 I/D 计数一致；结果位于
`tmp/spr-id-stlb-global-reasons-20261105/comparison.json`。
不能将 CPI 差异直接归因于这些事件，因 D-side
人口与整体反馈也发生变化。主工作树统一构建、主测试和 15 项相关专项
已通过；主线二进制与隔离冻结二进制 hash 不同，但两者 `src/`、`include/`
源码逐文件一致，均为 Release `-O3 -DNDEBUG`。

其余三案使用相同冻结二进制与同样的唯一开关差异完成对照；全部四案
两侧 v2 身份和逐核退休人口一致，统计口径与上节相同：

| 负载 | gem5 CPI | OFF CPI | OFF 绝对误差 | OFF 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 | I unknown OFF/ON |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| FLAC | 0.281872 | 0.320052 | 0.038181 | 13.55% | 0.317689 | 0.035817 | 12.71% | 96,326 / 96,069 |
| Graph500 | 0.652711 | 0.754191 | 0.101480 | 15.55% | 0.753935 | 0.101224 | 15.51% | 15,860 / 15,350 |
| Marian | 1.428304 | 2.915243 | 1.486939 | 104.11% | 2.939343 | 1.511038 | 105.79% | 41,323 / 40,542 |
| XZ | 0.603524 | 0.663062 | 0.059538 | 9.87% | 0.648175 | 0.044652 | 7.40% | 230,381 / 229,912 |
| 四案等权 | — | — | **0.421534** | **35.77%** | — | **0.423183** | **35.35%** | — |

四案等权 CPI MAE 上升、MAPE 下降，两者不是同一指标；Marian 误差
变大，不能宣称整体精度改善。模型 I-side STLB hit 四案均为 0；
global 仍只覆盖少数原 unknown。退休 L1 load miss 估计 OFF/ON 为
7,695/7,693、556,356/556,356、81,710/81,709、60,492/60,491。
Marian/XZ native L1 unknown 为 4/27，各案 L2/L3 native 均有 unknown，
不可完整评分；FLAC/Graph500 L1 事件资格仍为 incomplete-evidence。
该四案新输出未独立 native 重评分，参考复用前一同源已验证数据；并发
运行不作为吞吐验收。

global 选项另做 FLAC 同输入、同二进制、CPU 112–119 的串行单次
OFF/ON：端到端 199.04/198.57 秒，预热 8.401/8.368 秒，模型测量
56.622/56.301 秒，身份计算 57.088/57.122 秒，峰值 RSS
1,502,036/1,529,048 KiB；身份和逐核人口一致。端到端及模型测量
ON/OFF 吞吐比为 100.24%/100.57%；此为单次共享宿主样本，不证明加速
或四案稳定吞吐，数据在 `tmp/spr-id-stlb-global-throughput-20261105/`。

## 大页退休顺序近似

默认关闭的 `core.spr_id_stlb_allow_large` 接入 2M/4M/1G 功能映射：
模型 ITLB 的所有页型共用 256 项，4K 额外遵循每组八路，符合当前
gem5-spr 的配置与替换几何。取指块请求才扫描一次有界池；模型仍按
退休输入顺序即时填入，不包含原生预测或动态命中。各类页与 global
组合仍要求可绑定的 ASID、root、PCID 和功能映射；不满足时 unknown。
真实 Simulator 专项覆盖 I→D/D→I、2M/4M/1G、256 项共享容量、4K
八路冲突、已绑定失效、root/事实缺失、跨地址空间隔离和退休人口。
主线完整构建、主测试及 15 项相关专项回归通过。

同一隔离冻结程序 `b572dbfe…`、各案相同 v2 输入身份和逐核退休人口，
global 模型共同开启，仅改变大页选项；全部四案结果如下：

| 负载 | gem5 CPI | OFF CPI | OFF 绝对误差 | OFF 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 | I unknown OFF/ON |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| FLAC | 0.281872 | 0.317689 | 0.035817 | 12.71% | 0.320756 | 0.038884 | 13.79% | 96,069 / 70,399 |
| Graph500 | 0.652711 | 0.753935 | 0.101224 | 15.51% | 0.753659 | 0.100948 | 15.47% | 15,350 / 5,391 |
| Marian | 1.428304 | 2.939343 | 1.511038 | 105.79% | 2.963589 | 1.535285 | 107.49% | 40,542 / 12,315 |
| XZ | 0.603524 | 0.648175 | 0.044652 | 7.40% | 0.645037 | 0.041513 | 6.88% | 229,912 / 80,500 |
| 四案等权 | — | — | **0.423183** | **35.35%** | — | **0.429158** | **35.91%** | — |

绝对误差单位 cycles/macro，末行为 CPI MAE/MAPE 而非 WAPE；虽然 unknown
减少，四案等权误差反而变大，选项保持默认关闭。FLAC ON 的 70,399
次 unknown 全在 core 0，独立同源分因显示其首个失败条件均为 `.ixlat`
的 walk root 与当前退休 ASID 不符；不排除后续其他限制，也不能因此猜测
当前映射或断言采集缺失。分因程序与原大页 ON 的身份、CPI、逐核人口和
原模型计数一致，结果在 `tmp/spr-id-stlb-large-reasons-20261105/`。
unknown 是模型资格事件，
不是 native TLB miss；样本起点仍非严格对应。新输出目前没有独立 native 重评分，
原生参考复用此前通过来源/人口检查的同源报告；各案 L2/L3 原生仍有
unknown，不可完整评分。并发墙钟不能用于吞吐验收。

对 core 0 计分前缀 250 万个宏 span 的有限抽样，取到五个不同页：退休、
decode 与 Execute-origin ASID 均为 `0x81a514000`，历史 walk root 均为
`0x81a7ca000`，均 PCID0/global，其中 4 个为 2M、1 个为 4K。两 root
均按 4K 对齐，首级 PTE 地址等于历史 root 加对应 PML4 索引，不是字段
单位相差 4096 倍。源码中 TLB hit 可复用旧条目的 walk 元数据并使用当前
CR3 更新来源 ASID，global 条目可跨非 global flush 保留，故样本与跨
CR3 的合法历史 global 翻译机制一致；但缺当前 root 的对应 PTE/当前
功能翻译，无法证明当前映射等价，仍保留 unknown。该缺口属于可以由
Atomic/QEMU 的当前功能翻译或页表快照提供的输入，不应由 OoO 时序猜补。
样本证据见 `tmp/spr-itlb-root-samples-20261105/samples.json`；五页
不能代表全部 70,399 个模型事件。五个样本前后各 10,000 条退休记录内的
`.xlat` 没有覆盖相应虚页且属于当前退休 ASID 的绑定翻译，`.vmap` 也未
记录这五个精确 4K 虚页；这是有限样本而非全流缺失证明。`.asmap` 显示
旧 root 曾在该核生效、随后切换至退休 root，进一步支持历史 global
翻译的解释，但不提供当前 root 下的 present 映射内容。

为该缺口建立的默认关闭功能 walker 候选仅在隔离 gem5 副本验证，尚未
接入采集器或主分支：真实 x86 walker 在受控非 Ruby 内存中通过 4K/2M、
global、缺页、越界 root 及叶 PTE A 位不写测试。真实 MESI 三层 Ruby
冷态 OFF/ON 在无 guest 指令、无在途事务时均正常退出，目标 tick 均为
0，959 行 board 统计一致；这不能证明缓存内部状态、在途 PTE 写或真实
Ruby 功能读取失败时的行为。额外 checkpoint 在 OFF 的 drain 阶段触发
MemCtrl 断言，故未完成 checkpoint 状态比较。只有测试夹具操作合成页表
和寄存器，不能并入生产；候选也未生成 FST。证据和未完成条件见
`tmp/spr-current-root-functional-20261105/README.md`。

模型退休 L1 load miss OFF/ON 为 FLAC 7,693/7,693、Graph500
556,356/556,358、Marian 81,709/81,708、XZ 60,491/60,479。对应
native L1 known 为 3,898/276,155/43,387/23,616；Marian/XZ 的
native unknown 为 4/27，不能评分；FLAC/Graph500 虽无 L1 unknown，
事件资格仍为 incomplete-evidence。各案 native L2/L3 有 unknown，
不能报告完整的退休 miss 相对误差。

FLAC 两侧还在 CPU 128–135 顺序执行一次：端到端 199.09/200.33 秒，
预热 8.399/8.772 秒，模型测量 56.522/57.524 秒，身份计算
57.218/57.237 秒；ON/OFF 端到端与模型测量吞吐比分别约 99.38%/
98.26%。同期其他案例占用共享宿主/I/O，单次配对不构成稳定吞吐验收。

## 历史 I/D 接线复核：严格事件时间尚未闭合

本节保留退休顺序切片接入之前的源码观察；其中“当前没有调用”描述当时
状态。上节新路径已使用该功能事实，但仍未实现严格事件时间闭合。

`TraceSource::current_instruction_fetch()` 暴露与退休宏绑定的 `.ixlat`
功能字节和翻译事实，但当前 `src/interval_core.cpp` 和 `src/simulator.cpp`
没有调用它；现有取指物理地址使用 modeled/ifmap 路径，模型尚无 ITLB。
`.ixlat` 不提供 TLB hit 或 miss。其 v2 分别保存 Execute-origin 分片、
decode ASID 和退休 ASID，这些上下文可能不同；不能把历史字节来源直接
当成当前退休边界的可复现功能翻译。缺失、跨上下文及 Atomic/QEMU
等价性尚未证明的人口须单独报告并回退。

现有 D-only 代理在 stage 1 根据数据依赖准备时间处理 DTLB miss，并按
退休顺序立即向 STLB 插入。假设旧数据操作将在模型 cycle 100 才访问
页 X，而随后处理的年轻指令在 cycle 10 取指同一页，若简单共享该表，
取指可能命中未来的数据填充；替换和 pending fill 也有相同风险。
stage 2 响应反馈目前不会对 I/D 的 STLB、ITLB/DTLB pending 和失效做
统一提交或回滚，因此这个顺序不能靠现有代理认证。

模型需要用 Atomic/QEMU 可获得的功能事实建立 ITLB，再以有界单遍方式
合流每核模型 I/D 请求、fill 和功能控制事件；用安全水位及 generation
约束跨 Q、在途响应、回滚和失效。无法证明控制闭包、上下文或顺序时
必须 fallback；不能用 OoO 实际 hit/时间，或未绑定 MMU 诊断的数量、
位置猜事件归属。FLAC 的 121 条未绑定控制仍是明确的完整性限制，
不能被一个 sealed owner 的存在消除。`SprStlbPending` 是有明确前置的
组件，不自行证明 clean epoch 或提供主路径事务。

后续真实 Simulator 验证须覆盖上述 D/I 时间逆序、共享页正反向访问、
控制与在途 fill、相同周期顺序、跨窗口/预热、功能身份未知和 OFF 人口
与时序保持。当前没有满足上述条件的完整切片，停止此方向，不宣称 I/D
共享已接入。更详细的源码落点和测试前置见
`tmp/spr-shared-id-stlb-assessment-20261105.md`。

`include/fastsim/spr_stlb.hpp` 实现独立固定 2×128×8 状态，按 4K/2M/4M/1G 功能虚拟页和独立 ASID、PCID 建索引；同组 LRU，lookup 可选择更新或只读，已存在范围的重复 insert 不触碰 LRU；支持全失效、非 global 失效和仅在事件能绑定 ASID+PCID+VA 时使用的地址失效。全表可复制用于有界回滚，保存无 native hit、时间、动态 source 或时序事件。I/D 应使用同一个 per-core 对象，不跨核共享。当前编译器下 `sizeof(SprStlb)=49160` 字节，不能按每条 UOP 全表复制；实际需按 chunk 复制或增加仅修改组的有界日志，并评估吞吐。

功能 `.xlat` 的 `address_space_id` 是 CR3 页表根地址，`pcid` 独立携带；本组件以 ASID+PCID 保守隔离，global 只影响 flush_non_global，不跨 ASID 或 PCID 猜共享。gem5 的 `concAddrPcid` 是地址 OR PCID；FS `trie.insert` 使用 `MaxBits-logBytes` 的前缀，`base/trie.hh` 会掩掉低 `logBytes` 位。对本四种页长（均至少 12 位），这个源码关系表明 PCID 所在低 12 位不参与 trie prefix 匹配；与此同时重复 insert 用 `assert(newEntry->vaddr == vpn)`。直接实例化 gem5 `Trie` 验证不同 PCID 的同页查找匹配同一条；4K 先入、重叠 2M 后入时 trie 先返回更宽 prefix，本组件已按更宽 prefix 优先处理。完整 PCID 非零或切换、全局页与其他 overlapping page-size 场景尚未微测，本组件的 ASID+PCID 保守隔离是显式偏离，不能声称完全等价。缺少功能映射、MMU 控制事件未绑定、非上述四种页长均不可静默当命中；对应上层输入应报告 unavailable/fallback，不用 native 动态信息补全。

`tests/test_spr_stlb.py` 定向验证 4K domain/set、I/D 共用、ASID 与 PCID 隔离、每组 LRU 与重复插入、2M 与 4K 同组竞争、1G、失效与对象复制回滚；另一测试直接从 gem5-spr `tlb.cc` 提取目标 location lambda，随机 200000 个地址×4 个页长比较域和组；第三个测试直接执行 gem5 Trie 验证 prefix 覆盖与 PCID 低位屏蔽，全部通过。测试只验证局部逻辑与索引，不是完整目标时序/硬件证明。

接入前置：FST 用于 ITLB/DTLB 的翻译事实、地址空间和 PCID 必须完整且来自 Atomic/QEMU 可生成信息；I/D 访问按现有模型估算顺序调用并共享对象，只有模型 walk/fill 在其可见边提交时才 insert，源/目标时间不能取自 native。失效只按已绑定功能事件及共享范围更新，预览/重算用快照或日志提交，失败回滚不得污染后续核/窗口。时序可在现有两阶段用已配置 7/6 cycles 对模型 STLB hit 加固定延迟，miss 延用已有 walker；但不应把本状态查找立刻当最终响应、掩盖 pending fill/invalidation 或引入逐拍推进。需先确定 L1 I/D 未命中路径、共享对象归属、预热状态保留、跨 chunk、代码地址与功能事件人口、slot/响应反馈及可回滚同步；计数 miss/hit/fallback 与 unknown。随后做合成压力、别名/global/PCID、地址空间切换、失效并发、I/D 冲突、预热、回滚和 OFF 不变性测试，再做端到端准确度与吞吐验收。

## 模型 pending fill / 失效代次独立切片

`include/fastsim/spr_stlb_pending.hpp` 在每核对象中组合上述 STLB、有限容量的 walker 完成优先队列和 STLB-hit 响应 ticket。`schedule_fill` 只接收调用方已确定、之后不会修改的**模型** walker 完成时间；条目只在模型时间到达后插入。命中返回按配置 hit latency 推迟的 ticket；调用方只能在 `complete_hit` 返回 `Done` 后考虑填充 L1 DTLB。每次已绑定的控制事件使用保守 `invalidate_all`，取消未完成 walker，增加代次，使此前 ticket 返回 `Invalidated`，不能填充。任一操作的模型时间早于已处理前沿时清空并返回 `TimeRegression`，此后持续 `Unknown`，直至调用方证明新的干净边界并重新初始化，不能用未来状态回答更早 lookup。

当前只接受 PCID 0、非 global、4K 页。`start_clean_epoch(..., true)` 是调用方对**控制事件完整、初始翻译状态已知为空**的证书，不检查这些事实本身；映射发生改变时，调用方须先用已绑定控制证据失效，或主动保守清空。没有证书、地址空间/功能映射未知、large page/global/非零 PCID、队列满或模型时间回退时，都须显式走旧模型并计数，不能把未处理人口算作 STLB 命中。失效方法同样依赖调用方按时消费所有相关 CR3/CR4/EFER 写及 flush/demap；它不自行读取 `.mmuctl`，不验证 L1 DTLB 自身状态，也未实现 fetch 侧和 I/D 事件时间合流。

unsupported 或容量耗尽会 poison 本核摘要，后续返回 unknown 而非沿用可能因 fallback 请求发生替换的旧条目；调用方遇到组件看不到的翻译/控制影响也必须主动 `abandon_unknown()`。只有另一个已证明 clean 的边界可以恢复。这个规则可能使有限子域覆盖很低，但避免 fallback 后错误复用过期模型状态；不能通过猜测 clean 边界提高覆盖。

同周期的控制与响应要由集成方确定顺序：测试定义先执行失效再检查命中 ticket，原生调用顺序及全部跨核事件尚未证明。活动中的 ticket 有容量限制，调用方须完成或失效后回收；STLB 和两个 pending 集合随 checkpoint 复制/回滚；身份分配器在副本间不回退，避免弃用分支的 ticket 在恢复后重新有效。不要为每个 UOP 复制约 49 KiB 的 STLB，应在窗口层复制或采用经过验证的有界写日志。

`python3 -m unittest discover -s tests -p 'test_spr_stlb*.py'` 四项通过，其中新增测试覆盖填充前 miss、hit 响应未到、失效后旧 fill/ticket 不可用、同周期边界、时间回退、容量耗尽、每核独立及跨 chunk 快照/旧 ticket。组件尚未接入 Simulator；尚无真实负载正确率、CPI 或吞吐结论。现有 FLAC 的 121 条未绑定 MMU 命令仍使完整控制事件资格为 unknown，不能据此开启为全量已对齐路径。

## 历史两阶段接线复核（2026-11-05）

本节结论针对严格模型时间和失效闭包，早于上方已授权的退休顺序近似。

目前不能安全启用一个有真实效果的共享 I/D STLB 切片：阶段一已有 DTLB
状态与模型 walker，但未实现 ITLB miss/fill；`.ixlat` 提供功能翻译而非
ITLB 或 STLB 命中，无法直接决定共享表的填入与替换。阶段二现有的
DTLB generation 反馈只处理受限 PTE walker 的延迟，不提供 I/D 访问按
模型时间合流的共享 STLB；单独补 D-side 状态不能称为共享 I/D 模型。

`SprStlbPending` 的 clean epoch 还要求完整 MMU 控制和已知空状态证书。
既有 FLAC 仅确认采集起点 PCIDE=0，存在 121 条未绑定命令；它们的失效
时机无法按退休指令序安全重建，也不能从缺失事件推断全程无变化。
无证据时 fallback 并清除候选状态，不用 OoO 参考结果补充。

最小前置是在既有两阶段框架中用功能 `.ixlat` 建立模型自己的 ITLB，按
模型时间有界合并同核 I/D 请求和 STLB fill；仅在已认证空状态及功能
MMU 失效边界内启用，无法绑定、发生时钟回退或其他未知影响时主动回退。
pending fill、generation、预热和窗口快照必须与反馈一起提交/回滚。
该路径仍需验证输入在 Atomic/QEMU 生产者侧可获得，并报告覆盖、正确性
和吞吐；只改善 DTLB 的实验必须明确标为 D-only 代理。

## D-only 退休顺序近似首切片

`core.spr_dtlb_stlb_proxy` 默认关闭；开启仅支持 `interval_weave/time_epoch`、一次 feedback、DTLB `timing_walk` 且 `hierarchy_walk=false`。它不建 ITLB，也不声称 I/D 共享 STLB。每核在功能记录顺序上使用独立 `SprStlb`：已知 4K、PCID 0、非 global、地址空间一致且可绑定 v2 功能控制的 DTLB timing miss 才查询；命中以 6 cycles 返回且不占用旧 walker，miss 保留既有 walker，并在退休顺序中立即插入 STLB。这一即时填充会近似等待和并发，不是 gem5 时间戳或精确机制。DTLB 原本的 pending completion 和 L1 fill 路径仍由模型自身驱动。

可绑定的退休控制有操作、可绑定功能边界或地址空间切换时保守清空该核 DTLB/STLB；控制不完整、缺失或映射/页型不在子域时 STLB 回退，并计数。**不读取 unbound groups 的数量、内容或文件位置来决定模型状态**：它们可能含 OoO 相关效果，不能变成 Atomic/QEMU 输入。对忽略此类无法绑定的控制、被忽略的跨核失效、即时填充及退休顺序与真实执行顺序不同所产生的偏差，该模型不作严格失效正确性声明。无可绑定控制的记录走既有 DTLB timing，而不是伪造命中。

输出 `spr_dtlb_stlb_hits/misses/fallbacks/flushes/missing_controls`；最后一项按记录统计，不可当成遗漏 MMU 命令数。真实 FST 的 unbound 数须从采集完整性诊断独立报告，不能反馈到模型。在 `tests/test_dynamic_hierarchy.cpp` 添加实际 Simulator 交替页面、已绑定控制、缺失控制和 unbound 诊断数改变不影响模型的测试，以及不支持 hierarchy_walk 的校验。统一构建和 `fastsim_tests`、`fastsim_dynamic_hierarchy_tests` 已通过；后者执行上述 Simulator 测试。真实 FLAC 负载的覆盖、CPI 和吞吐尚在回放。

## FLAC C4 同源 D-only OFF/ON 回放（2026-11-05）

二者使用同一冻结二进制 `tmp/spr-dstlb-v2-20261105/fastsim`（SHA-256 `aee2168b519ad9ef52c4b97311e8fd641b3489d7e3d37ae11eaea26870dd7302`）及同一 canonical 配置、功能预热和完整 FST，输出配置逐项比较只有 `spr_dtlb_stlb_proxy` 不同。两侧 `functional-record-window-v2` 完整输入身份相同：`sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`，与之前的 FLAC v2 输入一致。新 OFF 与先前同源 OFF 的 `totals`、`time_accounting`、`thread_state`、`cores`、`cha` 共同字段相同；新输出没有单独通过 native validator，下表为身份及逐核人口匹配、使用已验证同源参考的诊断。功能身份的 `roi_edges=false`、`macro_boundary_complete=false` 仍须单独披露。

| D-only 代理 | FastSim task active CPI | gem5 CPI | CPI 绝对误差 (cycles/macro) | CPI 相对绝对误差 |
|---|---:|---:|---:|---:|
| OFF | 0.3210412161 | 0.2818719356 | 0.0391692805 | 13.8961% |
| ON | 0.3197865156 | 0.2818719356 | 0.0379145800 | 13.4510% |

任务宏指令分母均为 31,246,502；ON 比 OFF 降低约 0.0012547004 CPI。两侧各核退休宏指令数分别为 4,678,653 / 9,067,614 / 8,464,977 / 9,035,325，各核退休 UOP 数分别为 5,695,137 / 10,003,324 / 9,363,835 / 9,867,738；各核 memory 和 Sequencer 请求分别为 1,733,744 / 1,956,196 / 1,812,133 / 1,935,695，全部在两侧一致。cache 命中路径可改变：L2 accesses 为 39,884 / 39,885，LLC accesses 为 9,156 / 9,148（OFF / ON）。

| 核 | ON DTLB timing miss | 代理 hit | 代理 miss | 回退 | 实际清表 | 缺可绑定控制记录 |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 1,317 | 907 | 254 | 156 | 17 | 0 |
| 1 | 18 | 0 | 1 | 17 | 1 | 0 |
| 2 | 198 | 126 | 52 | 20 | 2 | 0 |
| 3 | 15 | 0 | 0 | 15 | 1 | 0 |
| 合计 | 1,548 | 1,033 | 307 | 208 | 21 | 0 |

ON 的 1,548 次 DTLB timing miss = 代理 hit + miss + fallback；这是 ON 模型人口，并非原生 STLB 命中率。OFF 的 DTLB timing miss 为 2,716，ON 缩短模型延迟后影响其他时序反馈，因此不能把 OFF 的人口当 ON 的固定分母。`missing_controls=0` 仅指本次模型访问到的当前记录均有可绑定的控制组，**不代表 MMU 采集完整**；该 FLAC 的功能伴随另有 121 条未绑定控制诊断（各核 66 / 15 / 24 / 16），本模型按输入合同不读取这些诊断或根据它们改变时序。即时插入、退休顺序与未绑定及跨核失效均为明确近似。

两次分别绑核 128–135 / 136–143 并行执行；OFF/ON 预热分别 8.336 / 8.717 s，测量分别 58.029 / 58.535 s，模型测量吞吐约 0.5358 / 0.5312 MIPS，完整身份计算 57.138 / 57.062 s，进程端到端 379.85 / 380.66 s。并发及共享机器干扰使这些单次墙钟仅供诊断，不能判定吞吐门槛或开销。原始输出、配置和日志见 `tmp/spr-dstlb-v2-20261105/{off,on}.{json,cfg,log,time}`。
