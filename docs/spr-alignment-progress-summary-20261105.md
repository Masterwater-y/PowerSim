# FastSim 与 gem5-spr 对齐进度

记录时间：2026-11-05 21:58 EST。本文是工作快照；正在运行的实验结束后，结果以对应专项记录为准。

## 当前结论

FastSim 仍使用原有的并行两阶段框架；本轮已接入若干默认关闭的 SPR 资源近似，并完成四案 C4 联合诊断。联合配置的 CPI 等权 MAE 为 **0.371427 cycles/macro**，MAPE 为 **31.14%**。FLAC 相对误差为 +8.90%，但 Marian 仍为 +91.54%，XZ 的绝对误差相对未开启时变大。退休 load L1 miss 的诊断差异也很大，不能认为精度已经达标。

完整 store buffer、硬件预取、跨宏融合和 Atomic/QEMU 独立采集尚未完成。部分选项只是可选近似或隔离原型，不代表默认启用，更不代表已经与 gem5-spr 行为等价。

## 实验与输入

- 四案为 FLAC、Graph500、Marian、XZ，各自使用 `spr-native-prefetch-observation-20261105` 的新一批 gem5-spr FS C4 采集。FST 和 `.ffe`、`.uops`、`.sreq`、`.xlat`、`.ixlat`、`.core`、`.mmuctl`、`.archb` 等伴随位于各案同一次采集目录。
- 每核设置 500k user UOP 预热；测量在首个参与核达到 10M user UOP 时共同截止，不能解读为每核都有 10M 测量指令。FastSim 和 gem5 计时起点没有严格匹配；下文数值是用户允许的诊断比较，不是严格同窗口精度认证。
- 这批 FST 由 gem5-spr **OoO** 运行采集，OoO 同时提供独立 CPI/PMU 参考。FastSim 使用退休功能信息，而非 OoO 时间、命中或动态授权决定时序。原始 FST 中存在 syscall 时间戳，不能称整个文件只含 Atomic 信息；既有源码检查未发现它们被模型用于计时。
- `.sreq`、翻译及其他伴随能否由独立 Atomic/QEMU producer 端到端等价生成尚未证明；源身份验证和单项功能实验不等于这一证明。MMU 未绑定和部分翻译 unknown 仍存在。

采集记录：[四案新采集](spr-native-prefetch-observation-20261105.md)；输入合同核查：[Atomic 输入验证](spr-flac-atomic-input-validation-20261105.md)。

## 四案 CPI

下表来自同一个冻结 FastSim 二进制 `dd84a3fd…` 和各案同一 FST；ON 为同时开启 load bank、store 四行窗口和 I/D STLB 相关近似，OFF 为相应 canonical 基线。两侧保留相同预热、线程计划、MMIO 和 FR-FCFS；该组合未包含完整硬件预取、完整 store prepare/head 或本次隔离中的前端融合近似。

| 负载 | gem5 CPI | OFF CPI | OFF 绝对误差 | OFF 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| FLAC | 0.281872 | 0.321041 | 0.039169 | +13.90% | 0.306947 | 0.025075 | +8.90% |
| Graph500 | 0.652711 | 0.754031 | 0.101320 | +15.52% | 0.753361 | 0.100650 | +15.42% |
| Marian | 1.428304 | 2.934776 | 1.506472 | +105.47% | 2.735717 | 1.307413 | +91.54% |
| XZ | 0.603524 | 0.647965 | 0.044441 | +7.36% | 0.550954 | 0.052570 | −8.71% |
| 四案等权 | — | — | **0.422851** | **35.56%** | — | **0.371427** | **31.14%** |

绝对误差单位为 cycles/macro instruction；最后一行分别为 CPI MAE 和 MAPE，非 WAPE。多项开关同时改变，不能把差异归因于某一机制。逐项参数、结果与原始路径见 [联合实验记录](spr-two-stage-alignment-results-20261105.md)。

## perf-like PMU

以下为同一 ON 回放的任务退休 **宏 load** L1 miss，原生参考来自同源 Ruby/SLICC，而非将 UOP 或 ownership 请求直接当作退休宏 load。相对差异仅按已知原生事件计算。

| 负载 | gem5 L1 miss 已知数 | FastSim L1 miss | gem5 unknown | L1 miss 相对差异 | CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|
| FLAC | 3,898 | 7,692 | 0 | +97.33%，暂定 | 0.025075 |
| Graph500 | 276,155 | 556,366 | 0 | +101.47%，暂定 | 0.100650 |
| Marian | 43,387 | 81,709 | 4 | 不可完整评分 | 1.307413 |
| XZ | 23,616 | 60,460 | 27 | 不可完整评分 | 0.052570 |

FLAC、Graph500 虽无 L1 unknown，退休事件资格仍有 `incomplete-evidence`，百分比不是完整认证。Marian、XZ 的 L1 存在 unknown；四案 L2/L3 均有原生 unknown，不能报告完整误差或把 unknown 当成零。组合 ON 的新输出截至本快照尚未完成维护版 validator 的独立重评分；已启动一次四案重评分。缺失硬件预取是已确认的机制差异，但尚不能将 PMU 差异全部归因于它。

## 机制实施状态

| 项目 | 当前状态 | 主要缺口 |
|---|---|---|
| 两阶段和共享调度 | 保留并行两阶段；本批使用的 legacy FR-FCFS 已改为单次预测及有界认证，失败回退。 | 历史上默认关闭的其他实验选项仍可能多次迭代，不能据此声称全项目所有路径都是单遍。 |
| Store buffer 和合并 | 四行窗口、受限同线组及每核多组为默认关闭的可选近似；相关 Simulator 专项和主回归通过。 | 完整 offload、队头回收、独立 prepare/head 与普通请求统一信用账尚未实现。受限配置的多组实验四案 CPI 不变，不能称为有收益。 |
| Sequencer、fill、TBE | 已有数据需求信用和受限的 L1/L2 fill 近似；I-fetch 信用切片已试验。 | 普通、prepare、fill 和 TBE 是不同资源，尚未完整合流。I-fetch 首版造成明显数据请求时间夹高，保持关闭。 |
| IQ、FU、load bank | IQ 分区、integer-family 轮询和 load bank 近似已可选，有定向验证与部分消融。 | 均是基于退休流和模型时间的近似，未证明原生动态发射等价或四案吞吐达标。 |
| 翻译 | D-side 和 I/D 共享 STLB、同 ASID global 与大页近似可选，已做四案配对。 | walk root、未绑定 MMU 控制与完整 walker/失效仍有限制；模型 unknown 不等于原生 TLB miss。 |
| 前端融合 | 静态资格判定已存在；只节省 source 带宽的近似正在隔离测试。 | 未实现完整前端、rename、ROB/IQ 的融合组，隔离原型尚未合入。 |
| L1/L2 硬件预取 | 有模型自主预取的受限实验及 L2 原型。 | 尚未在正式 buffer TSO、recorded scheduler、FR-FCFS 组合下完成安全验证；L1 的早可见性问题及后续队列方案已暴露覆盖退化，相关不安全原型未合入。 |
| 退休 PMU | 宏 load、UOP、原生 Ruby/SLICC 来源已区分，unknown 保留。 | L2/L3 原生事件关联不完整；validator 通过也不等于硬件 perf 事件或精度认证。 |

专项记录：[store 多组](spr-multi-store-group-20261105.md)、[I-fetch 信用](spr-ifetch-sequencer-credit-20261105.md)、[共享 STLB](spr-shared-stlb-increment-20261105.md)、[预取](spr-prefetch-components-20261105.md)、[prepare/head](spr-prepare-credit-mixed-window-20261105.md)。

## 吞吐与验证

已执行主工作树统一构建、`fastsim_tests` 和相关机制专项；例如多核 store 组切片的主测试及 16 个相关专项通过。测试通过只说明所覆盖功能，没有证明准确率或真实机器等价。

同一联合二进制和 FLAC 输入的一次串行 OFF/ON 对照：端到端 198.50/196.82 秒，模型测量 57.178/55.931 秒，峰值 RSS 1,520,500/1,509,536 KiB。这是共享宿主上的单次样本，不能据此宣布加速或四案吞吐达标。联合四案并行运行的墙钟不能用于吞吐比较。

## 正在进行与下一步

截至本快照，FLAC 前端 source 带宽近似的同二进制 ON 已完成、OFF 正在隔离回放；同周期、满宽、跨周期、未知、异常、跨 chunk 和 source 类型切换已做定向检查。此前用于回放的冻结二进制尚未包含评分边界和同步 anchor 的最后收窄，结果仅用于覆盖诊断。随后应核实实际折扣、人口和同源 CPI，确认边界后再考虑合入；它只覆盖 source 带宽，不宣称完整融合。

联合 ON 的四案维护版 native validator 重评分正在运行，原始 FST 不被改写。本次四案目录实测约 25.8 GiB；工具多次读取部分文件，实际 I/O 总量未知。无须重复 OFF 扫描才能继续其他独立工作。

Store 方面，简单增加 FIFO 完成水位经源码核查在现有普通 owner 路径上会被已存在的串行 drain 遮盖，暂不添加无效开关；正在隔离检查受限 store 组合能否安全支持 materialized fast kernel。后续优先解决 store prepare/head 与正确可见性、自主硬件预取、PMU 原生事件缺失，以及独立功能采集；每项继续用同源消融和独立吞吐测量评价，不使用 OoO 动态时序作为 FastSim 输入。

设计原文：[两阶段对齐设计](spr-high-throughput-two-stage-alignment-design-20261105.md)；持续实验记录：[SPR 两阶段增量实验](spr-two-stage-alignment-results-20261105.md)。
