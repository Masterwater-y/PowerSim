# Estimated parent 候选：实现、回归与验证

日期：2026-11-17。用户授权在已有在途服务机制内实现近似数据就绪路径并验证吞吐。此项为默认关闭的实验候选，不推进 SPR 里程碑，也不替换安装 M4。

## 实现范围

- 配置 `core.unified_parent_estimated=true`，仅支持已有 `v2-observe` / `v2-wait`。默认 false。
- 复用 ParentServicePool 的行索引、稳定 ID、事务快照、过期队列和现有响应到资源释放链。记录仅新增一个 uint64 lower-fill generation（184→192 bytes），不引入另一套 authoritative 表。
- 只有来源身份明确、非 carried 的普通 load 可估算。支持私有 L2 与 replayable 的独立 Memory/LLC-hit；远端、特殊访问、未来来源、非法时间、无 owner 的 merged-memory 等继续回退。
- 数据估计是 `max(selected_response, final_admission + selected_span - selected_queue)`：冻结选定完成时间，同时保留排除旧排队后的服务时长下界。它是模型近似，不能声称真实物理 fill 边，也不能输入 gem5 OoO 时间。
- Estimated 和 certified 分开计数；strict source ledger 保留原资格语义。Estimated 不获得 certified TBE/fill ownership。
- 同线 L1 后继走已有 gate；私有 L2 continuation 或同 lower generation 的 merged-memory 可复用已有 Estimated owner，不重算其数据边、不附加第二份 service lease。各 CPU 请求仍有自己的 IQ/Sequencer 生命周期。

完整设计：[graph500-estimated-parent-design-20261117.md](graph500-estimated-parent-design-20261117.md)。

## 独立审查后的边界修复

独立新上下文审查发现两项 Important，均按同一次 TDD 修复流程处理：

1. `v2-observe` 原来可能复用 owner 并跳过 legacy MSHR lease，使 observe 改变接收时间。真实 Simulator 复用压力用例先 RED；修复后仅 wait 模式进行这类资源复用，observe 逐请求 admission/response 与 OFF 一致。
2. Estimated read data 可能早于原 CPU callback；若将较早的 controller/data 当作 store 权限完成、或过早清除行身份，会让 Action::None store 提前获得 no-transaction 快速路径。unit 用 owner admission40 / estimated data120 / raw callback150，store eligible130/send140，分别测试过期前和 dispatch watermark120 后，两者先 RED。修复保留原 callback 作为保守权限回退和行身份寿命；读后继仍只使用独立的 estimated data/controller gate。

较早的 pre-review ELF/源码保存在 `tmp/estimated-parent-20261117/pre-review/`。最终 review-build 与它分别记录结果和吞吐，禁止混合两种 ELF 的重复样本。

## 回归证据

- 时间估计：手算排队与无排队例子、固定 selected completion 下界、未来来源、非法区间和溢出。
- 身份/生命周期：matching/mismatching generation、完成/未来 owner、store consumer 排除、Unknown 不升级、copy-on-write 和过期回收。
- 真实 Simulator 位移用例：先一个不同线长延迟 miss，再有共享依赖的两个同线请求。修复前后后继响应 218→401，后续 dispatch219→402，memory issue220→403；Sequencer 最小释放日历也变晚。该用例证明响应约束确实进入了已有资源链。
- 真实 Simulator 复用用例实际触发 estimated_reused_fills，另核对 observe 中所有请求的接收/响应及总周期保持中性。
- 最终 `fastsim_tests`、`fastsim_parent_service_tests`、`fastsim_estimated_parent_tests` 均通过，详见 `review-green.json` 和对应日志。测试通过不代表近似模型精度通过。

## 评估方法与状态边界

完整 Graph500、ZSTD、FLAC 各 C4/C8，均使用原功能输入、source-bound 参数、warmup、宏指令人口和退休终点。OFF 对照为此前补字段路径。维护的 native quick validator 复用独立 BaseCPU 保存证据，单独输出 provisional CPI；不完成 native PMU 严格审计或硬件等价认证。六个来源另经 target identity 验证，不能用 quick scored 代替该项。

高频 CPU52–55 给 C4、56–63 给 C8，同组串行、两组掩码不重叠。启动频率快照见 engine.log，没有连续频率遥测。每次确认 HOST_VALIDATED_PRELOAD=true；测量阶段 functional UOP/s 排除输入准备、完整性检查、初始化和 warmup，另保留各阶段成本及整个过程 wall。首次探索 ELF 每个 Graph500 三对，最终 ELF每个 Graph500 两对，对照负载各一对。探索轮有编译在 CPU0–15 上并发发生，全部样本和离群值保留；最终轮无并发构建。小样本配对不构成正式吞吐验收，也不保证 5% 上限。

## 已确认的负结果

短 C4 中，估计路径确实触发（2,402 新来源、23,314 次等待），而未处理来源候选归零；CPI 精度却明显回退。完整 Graph500 初轮也出现严重过计。实际 Graph500 的 `estimated_reused_fills=0`，说明这轮数据没有通过新增加的 private-L2/merged owner 复用分支获得收益；主要时序变化来自 L1 后继使用 Estimated gate。不能用来源覆盖提高，代替数据时间或 owner 选择正确性的证据。

预先已有的同成员短 C4 独立诊断显示：341 个可比较 uncore 服务组中，128 个组的模型 owner 与 native leader 不同；模型单组服务时长均值约188 cycles、native约191 cycles，仍有明显的接收/资源占用差异。这是后续检查 owner/接收顺序与并发的依据，不证明本轮所有过计都由该差异造成。参见 [同成员诊断](graph500-native-lifecycle-paired-diagnosis-20261117.md) 与 `tmp/graph500-native-lifecycle-20261117/retry-wait-shortfall-summary.json`。

本候选证明“把 UNKNOWN 变成可用估计并增加等待”本身不足以修复 Graph500；当前时间近似会施加过强约束。不会按 CPI 调延迟或等待倍率，也不会将失败的全量 Estimated 规则开启为默认。

完整精度与主机测量表在最终回放结束后附于下方；所有过程失败/修复日志、配置、命令、来源/ELF身份和 raw 输出均保留在 `tmp/estimated-parent-20261117/`。

## 最终 ELF 的同成员响应与资源证据

最终 ELF 对短 C4 分别执行普通 OFF/ON 与 core0 全测量段 audit OFF/ON；两组 audit 的 `time_accounting` 与各自无审计重放完全一致。将每组 49,359 个 UOP 样本中的普通单片段 load，与已有独立 native 同成员集合联结：两组均 **16,454/16,454**，直接 PC 与物理行全部匹配，无缺失/不匹配。输入仍只有功能 trace；native 时间仅用于此处独立诊断。

下表均为相同 16,454 个成员的均值，单位 cycles。native 起点是 IQ insert，模型起点是 dispatch 近似；接收前段包含准备/依赖/调度等，并非纯 Sequencer 排队。模型 IQ release 使用当前路径实际的 `max(completion,memory response)`，Sequencer release 使用当前普通请求响应；未启用 competing read/coalescing/early-ack 模式。

| 阶段 | native | OFF | Estimated ON |
|---|---:|---:|---:|
| IQ insert / dispatch → 接收 | 124.869 | 28.623 | 156.159 |
| 接收 → 响应 / Sequencer release | 34.675 | 19.560 | 55.587 |
| 响应 → IQ release | 1.050 | 1.665 | 1.272 |
| IQ insert / dispatch → IQ release | 160.593 | 49.849 | 213.018 |

对 native merged 子集（10,778 条），接收→响应均值为 native45.037、OFF23.180、ON71.544 cycles；ON 的 IQ 持有均值223.542，native168.129 cycles。新约束确实改变了真实输入的响应和资源边界，且两个主要阶段均从偏短变为偏长。不能称为“只改了 UNKNOWN 标签”或“机制没触发”。

这说明当前 `final_admission + unqueued_service` 下界仍然过强：当最终 CPU 接收被反馈推迟时，它会强制保留一段完整服务，再让后继等待。虽然不再重复平移旧 queue、也不重复创建同一个 owner，却仍不足以对齐数据可用的时间和请求并发。下一步应检验服务 owner/时间轴与接收占用的关系；这些平均数本身不构成全部 CPI 误差的因果分解，也不能用来拟合等待倍率。

证据：[paired-audit-summary.json](../tmp/estimated-parent-20261117/paired-audit-summary.json)；逐成员结果为同目录 `off-paired-members.jsonl` / `on-paired-members.jsonl`。没有将普通 load 样本直接换算为物理 IQ 槽位利用率或完整流水线占用认证。

## 最终 ELF 的主机吞吐结果

以下只使用最终 review-build 的独立 benchmark 样本，不与 pre-review、来源身份补齐或 audit 运行混合。指标为 measurement 阶段 **M user UOP/s**，分母排除输入准备/完整性处理、初始化及功能 warmup。benchmark 保留 validated preload，额外的报告身份哈希关闭；另以来源完整的运行逐例核对完全相同的 `time_accounting` 后评分。CPI 的 user-plus-kernel 宏指令口径与此吞吐分子不同，不能互换。

| 来源 | OFF 中位吞吐 | ON 中位吞吐 | 变化 | 每模式样本数 |
|---|---:|---:|---:|---:|
| c04-777.zstd_r | 1.253 | 1.218 | -2.76% | 1 |
| c04-817.flac_s | 2.108 | 2.018 | -4.25% | 1 |
| c04-854.graph500_s | 1.346 | 1.356 | +0.77% | 2 |
| c08-777.zstd_r | 1.732 | 1.699 | -1.94% | 1 |
| c08-817.flac_s | 2.755 | 2.722 | -1.20% | 1 |
| c08-854.graph500_s | 2.233 | 1.949 | -12.69% | 2 |

Graph500 C4 的 OFF 范围1.283–1.409、ON1.256–1.456 M user UOP/s，当前小样本只能认为相近，不能声称提速。C8 的 OFF2.173–2.292、ON1.942–1.956，两次 ON 均低于 OFF；中位吞吐下降约12.7%，超出拟定5%目标。其余控制只有一对，不认定为正式通过。所有离群点和前轮样本均保留，未删掉对方案不利的结果。

此前 `final-scoring/` 和 `final-short-scoring/` 对缺少输出内来源身份的 benchmark 文件拒绝评分。这些失败日志保留，未给旧预测补造身份字段；最终有效精度入口为 `identity-scoring/` 和 `short-identity-scoring/`。补齐运行中的身份处理成本独立于主机 benchmark，不使用整个 process wall 替代主吞吐分母。

## 最终来源完整的 CPI 评分

六个来源 target identity 均 valid（6/6，mismatch0）。最终 ELF 的 OFF/ON 各6例 quick diagnostic-scored，另一个短 C4 各1例 diagnostic-scored，无 failed/pending；strict pass不作为本轮结论，native PMU 与硬件等价性未认证。所有来源完整预测均与各自 benchmark 的完整 `time_accounting` 相同；同模式重复结果也一致。OFF 是上一轮补 preview 时间字段后的对照，不将其冒充未替换的安装 M4 ELF。

RE=(FastSim−gem5)/gem5；AE=abs(FastSim−gem5)，单位 cycles/macro。以下均使用维护评分入口选择的独立 BaseCPU 参考与匹配的逐核宏指令人口/退休终点。

| 来源 | gem5 CPI | OFF CPI | OFF RE | OFF AE | ON CPI | ON RE | ON AE |
|---|---:|---:|---:|---:|---:|---:|---:|
| c04-777.zstd_r | 1.206049 | 1.185146 | -1.733% | 0.020903 | 1.230893 | +2.060% | 0.024844 |
| c04-817.flac_s | 0.284584 | 0.294976 | +3.651% | 0.010392 | 0.300241 | +5.502% | 0.015656 |
| c04-854.graph500_s | 0.966422 | 0.839796 | -13.103% | 0.126626 | 1.661077 | +71.879% | 0.694655 |
| c08-777.zstd_r | 1.052763 | 1.086689 | +3.223% | 0.033927 | 1.140823 | +8.365% | 0.088060 |
| c08-817.flac_s | 0.269360 | 0.274787 | +2.015% | 0.005426 | 0.277100 | +2.873% | 0.007740 |
| c08-854.graph500_s | 0.969791 | 0.854475 | -11.891% | 0.115317 | 1.711211 | +76.452% | 0.741420 |

| 等权集合 | OFF MAE | ON MAE | OFF MAPE | ON MAPE |
|---|---:|---:|---:|---:|
| 完整六例 | 0.052098 | 0.262063 | 5.936% | 27.855% |
| Graph500 C4/C8 | 0.120971 | 0.718037 | 12.497% | 74.165% |

新短 C4 独立列出，不与完整窗口聚合：

| gem5 CPI | 模式 | FastSim CPI | RE | AE |
|---:|---|---:|---:|---:|
| 0.973351 | off | 0.814205 | -16.350% | 0.159146 |
| 0.973351 | on | 1.680862 | +72.688% | 0.707511 |

## 来源覆盖与实际触发

这里的候选分母是 load/fetch_shared 新服务来源；Estimated 没有改写 strict ledger 的拒绝项。下表的未处理数 = 候选−严格接受−Estimated 新来源，不能称为 native PMU unknown。复用已有 owner 的 attachment 不算新来源。

| 来源（ON） | 新来源候选 | strict接受 | Estimated | 未处理 | Estimated等待次数 | 新fill复用次数 |
|---|---:|---:|---:|---:|---:|---:|
| c04-777.zstd_r | 902,864 | 235,850 | 479,306 | 187,708 | 162,249 | 0 |
| c04-817.flac_s | 39,163 | 4,228 | 3,631 | 31,304 | 11,185 | 0 |
| c04-854.graph500_s | 556,657 | 73,176 | 483,177 | 304 | 4,388,430 | 0 |
| c08-777.zstd_r | 2,216,338 | 579,982 | 1,157,637 | 478,719 | 405,687 | 0 |
| c08-817.flac_s | 62,825 | 11,109 | 4,654 | 47,062 | 11,963 | 0 |
| c08-854.graph500_s | 1,112,814 | 147,487 | 964,781 | 546 | 8,758,979 | 0 |

## 结论与交付状态

实现/回归完成，模型候选不接受：六例 CPI AE 全部增加；Graph500两例 MAE 从0.120971升至0.718037；C8吞吐约下降12.7%。来源覆盖提升和资源等待传播都已发生，但当前服务时间近似过强，不能当作准确性修复。

不更改任何固定延迟、容量或等待倍率来拟合。默认开关 false，安装 M4 与冻结 M1-M4/config alias保持不变；没有 commit/push 或新里程碑。实验代码和回退路径保留供复查。

最终候选 ELF SHA-256：`b869c0041b7ca6cf625e8aebdd6392e7c4a52be95ad1466052dc3d39e7d5f1b8`。安装 M4 仍为 `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9`。

构建选择：使用当前脏工作区的逐文件备份与隔离快照，保留之前已授权的字段修复和其他修改；潜在版本漂移由 source/ELF 哈希、review snapshot 相等和 OFF 同输入预测等价检查约束。所有编译/LTO/fixture 均位于项目 tmp，实际 Release 链接使用 `-flto=16`。最终新上下文独立审查的两项 Important 已逐一 RED→GREEN，三套测试随后全部 GREEN；没有未处理的审查阻塞项。

证据入口：

- [全精度最终结果](../tmp/estimated-parent-20261117/reviewed-final-results.json)，包括逐样本吞吐、准备/初始化/warmup/process wall、启动频率和来源计数。
- [来源完整运行收据](../tmp/estimated-parent-20261117/identity-scoring-receipts.json)、`identity-scoring/off/summary.json` / `identity-scoring/on/summary.json`；短窗口在 `short-identity-scoring/`。
- [最终主机样本收据](../tmp/estimated-parent-20261117/final-campaign-receipts.json)，每例 `command.json` / `receipt.json` / engine.log 保留原始命令、配置、掩码和输出。
- [最终源码/ELF身份](../tmp/estimated-parent-20261117/final-source-identity.json)、[测试结果](../tmp/estimated-parent-20261117/review-green.json)、[执行记录](../tmp/estimated-parent-20261117/ledger.md)。

此前 benchmark 缺身份评分失败、初始测试构建错误和无效 audit filter 的尝试均保留，未冒充最终成功结果，也未修改原预测补造字段。
