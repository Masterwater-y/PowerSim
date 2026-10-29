# SQLite：分支恢复尾部修复后的剩余 CPI 误差（2026-10-20）

当前 SQLite C4 的 active CPI 为 gem5 **1.000925**、FastSim **0.880284**，相对误差 **−12.0529%**，绝对误差 **0.120641 cycles/宏指令**。本轮没有修改生产模型、默认配置或延迟常数，而是继续做同源回放、逐事件原生取证和独立审核。

**已经确认：恢复事件的阶段仍然建模过粗；退休节奏仍明显过密。错误路径确实消耗了模型没有等价表示的后端资源。DRAM 另有与刷新周期吻合的长尾。** 其中分支恢复阶段错误有逐事件证据；退休节奏是时间落点；错误路径和刷新尚未获得全窗口、互斥的因果贡献。不能声称已经把剩余 12.05% 按原因完整分摊。

本页接续[尾部修复结果](branch-recovery-tail-fix-results-20261020.md)，替代将“所有误预测必须等执行完成后恢复”作为通用硬件规律的解释。对于 Decode 能确定目标的分支，这个说法不成立。

## 1. 来源与结果边界

沿用四核、四个标准单线程 rate 副本、8 DRAM 通道、buffer TSO 114、官方 SQLite 参数及已修复的 ROI。没有改成 OpenMP。硬件时钟为 333 ps/cycle。

本轮区分三份来源，所有比较都使用各自 gem5 参考、共同终点和宏指令分母：

| 标识 | 来源 | 用途 |
|---|---|---|
| O | `tmp/sqlite-noaccess-recapture-20261020/source/c04-708.sqlite_r` | 当前主结果、固定 FST 的配置控制 |
| Q | `tmp/sqlite-cpi-retirement-diagnosis-20261020/source-measurement/c04-708.sqlite_r` | 已有完整原生阶段记录，对照当前修复后二进制 |
| W | `tmp/sqlite-residual-after-tail-20261020/short/source-measurement/c04-708.sqlite_r` | 新增短窗口：逐分支预测/纠正、错误路径及 load 生命周期 |

O 和 Q 约为 2360 万 workload-task 宏指令；Q 配对了 **39,869,125 条已提交 UOP**，两边人口相等。W 为 **1,183,240 宏指令 / 1,998,963 UOP**。W 复用同一 checkpoint、gem5、冻结 runner 和标准负载二进制，采用 KVM → 100 万指令 warmup → stat → first-core-target common-end。新增采集约 195.83 秒，其中 detailed warmup 64.16 秒；没有重新 OS boot。

O/Q 的维护 validator 通过；硬件身份单独核验。W 的硬件身份、共同终点和人口核对通过，但完整 native-FS validator **失败**：这个短窗口恰好没有内核指令及内核访存，`kernel_records_present`、`kernel_memory_present` 两项为 false。本轮保留失败，没有放宽门禁。W 只用于有明确覆盖范围的机制诊断，不能宣称完整 FS 验证通过。

CPI 为 workload-task active cycles/宏指令；下表绝对误差也是单案 CPI MAE，单位 cycles/宏指令。O/Q 是同一负载的不同自然窗口，不合并成两个独立负载的 MAPE。

| 来源、当前恢复开启 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 / 单案 MAE |
|---|---:|---:|---:|---:|
| O：当前主结果 | 1.000925 | 0.880284 | −12.0529% | 0.120641 |
| Q：完整阶段配对源 | 1.001020 | 0.880582 | −12.0316% | 0.120438 |
| W：有限诊断，完整 FS 门禁未通过 | 0.991511 | 0.872110 | −12.0423% | 0.119401 |

| 来源 | gem5 elapsed ms | FastSim elapsed ms |
|---|---:|---:|
| O | 1.966026 | 1.729077 |
| Q | 1.966994 | 1.730350 |
| W，有限诊断 | 0.097669 | 0.085907 |

O 少算 **2,845,963.879880 active cycles**；Q 少算 **2,842,297.879880 active cycles**。elapsed 也约低估 12%，不支持把主要差额仅解释为 active/elapsed 口径切换或宏指令分母不一致；这不等于排除所有调度对资源竞争的间接影响。

![完整窗口差额、退休密度与两种恢复阶段](../tmp/sqlite-residual-after-tail-20261020/residual-overview.svg)

## 2. 最明确的新缺陷：没有区分在哪里发现预测错误

此前修复补上模型内部的 `actual_completion + configured_penalty` 恢复尾部。Q 中 **136,936 对**模型误预测及后继指令，没有违反该内部约束。这证明尾部传播按当前模型执行，不证明这个约束对所有原生分支都正确。

新 W 记录用 **core + 原生动态序号**连接预测历史插入、纠正来源、最终提交及 FST 顺序。只有同时观察到预测与提交，才允许把“没有纠正”判成预测正确。得到 **219,676 条合格分支**：212,838 条双方正确、6,838 条双方误预测，位置没有分歧。另有 4 条边界分支缺少预测历史，不能归为已认证 hit；它们的正向纠正记录仍可观察。6,838 + 4 = **6,842**，与 native 退休分支 miss 总数守恒。这个结论覆盖 W，不升级为 O/Q 全部逐分支认证。

对相同动态误预测，定义 `Δ = 下一条正确路径指令取指 − 分支执行完成`，分别使用各自时钟原点：

| 原生纠正来源、W 中实际类别 | 配对数 | gem5 Δ 中位数（范围） | FastSim Δ 中位数（范围） | 结论 |
|---|---:|---:|---:|---|
| Commit 模块转发、DirectCond | 3,444 | +5（+3…+5） | +2（+2…+3） | 模型恢复过早 |
| Decode 纠正、DirectUncond | 3,394 | −1（均为 −1） | +2（+2…+8） | 模型错误地等到执行完成以后 |

典型条件分支为 `0x404810`，另有 `0x48be85`；Decode 纠正的直接跳转为 `0x4a1810`。

通俗地说：条件结果可能要等数据算出来，Decode 已经知道的直接目标则可以更早纠正。当前模型统一从“执行完成”出发，因此前一类少等，后一类反而多等。两类方向相反，不能把 2 全局改成 5，也不能将每个 miss 的差值相加当成 CPI 贡献。

gem5 `BPredUnit::squash()` 的源码明确区分 `from_commit` 与 Decode 来源；Decode 的检查也包括 **预测 taken 的直接条件分支的目标错误**。因此后续实现不能仅凭 DirectCond/DirectUncond 硬分两类，还要区分目标与方向错误以及信息在哪个阶段可用。表格是本窗口观察到的类别，不是全部 ISA 的固定映射。

对于本窗口的后端条件分支，执行完成 → predictor 纠正为 2 cycles，执行完成 → 正确路径 fetch 通常为 5 cycles。两者之间还包含前端状态转换/供应等约束；从表中读出的 5 不是可以直接写进配置的纯硬件常数。“Commit 模块转发”也不是等到该分支退休才恢复。

**修复方向：** 在既有两阶段框架内建立“可发现错误的阶段事件 → 控制传播 → Fetch 可接收/指令供应”的链，使用 max 合并容量和供应约束；保持来源独立，原生 tick 只做诊断。已修复的尾部传播仍是现有模型的一致性修复，但其统一执行完成起点需要进一步细化。

## 3. 最大时间差落在退休过密，而非简单 commit width 配错

Q 全窗口对同一周期内多条退休只计一次，把相邻退休边界之间的时间互斥切分：

| 时间落点 | gem5 cycles | FastSim cycles | gem5 − FastSim |
|---|---:|---:|---:|
| 有退休的周期 | 9,702,339 | 7,639,476 | +2,062,863 |
| 下一退休指令尚未取指 | 138,530 | 77,901 | +60,629 |
| 已取指，尚未最终发射 | 319,652 | 357,233 | −37,581 |
| load 已发射，尚未退休 | 13,388,683 | 12,677,201 | +711,482 |
| store 已发射，尚未退休 | 4,594 | 1,507 | +3,087 |
| 其他已发射，尚未退休 | 73,120 | 1,605 | +71,515 |
| 未知阶段 | 4 | 0 | +4 |
| 配对退休跨度合计 | 23,626,922 | 20,754,923 | +2,871,999 |

active gap 减配对跨度差得到 **−29,701.120120 cycles** 的未分配边界/范围残差。它是代数残差，没有独立测出起止、EOF、owner 各项，不能包装为独立归因。未知 4 cycles 也保留。

全记录的退休直方图显示，在有退休发生的周期，平均每周期退休 UOP 为 gem5 **4.109**、FastSim **5.219**；两边最大都是 **8**。直方图比上述跨度分区各多 4 个起始退休周期，来自每核首条边界处理，二者用途不同。

这表示模型更容易将指令积攒到一起、成批完成退休。**“有退休的周期”不是有用工作的原因分类，load 发射后到退休也不等于等待 DRAM。** 提前准备好的后继、错误路径资源缺失、阶段边界、顺序/资源释放近似都能改变这个形态。

当前实际 fetch/decode/rename/dispatch/issue/commit width 均为 8，ROB=192、LQ=32、SQ=114，两侧匹配。不能用把 commit width 改小的办法吸收差额。

## 4. 错误路径资源占用：证据充分，但贡献尚未隔离

W 原生 wrong-path observer 记录了 **3,826 个 episode**，含 551,968 条 fetched、427,735 条 renamed、268,965 条 issued 记录，73,285 条访存记录。为减少边界混淆，只看能关联到上述合格已提交误预测的子集：

| 原生错误路径活动，合格分支关联子集 | 数量 |
|---|---:|
| episode | 3,444 |
| fetched records | 529,501 |
| renamed records | 413,305 |
| dispatched records | 386,535 |
| issued records | 262,696 |
| memory records | 69,906 |
| 其中 load / store records | 48,043 / 21,863 |
| data-completed records | 29,470 |

这些是原生被取消路径的记录/阶段活动，不是额外退休宏指令，也不是自动等价的缓存请求数。

当前 FastSim 的正确路径 FST 没有这些动态指令。`fetch_supply_speculative_shadow=true` 对匿名指令取指供应作近似，不会等价恢复上述 IQ/ROB/寄存器/FU/LSQ 与数据层级活动；`branch.shadow_rob=false`，且 `squash_width=0` 的现有实现并不会产生这种后端占用。

因此，即使已提交分支 miss 的位置完全一致，原生仍可能被错误路径暂时占住资源，而模型的正确路径更早排队、执行，最后更密集地退休。这是当前退休形态的一条可信解释。**尚未做隔离该资源占用的原生反事实或有认证模型控制，不能说它已经解释了全部或确定比例的 12%。** 不能把 issued 条数除以 issue width 就当作额外周期。

面向 QEMU/DynamoRIO 的生产模型不应依赖这些原生错误路径指令。它们适合作为开发期 oracle，用来验证从可观察分支语义、容量、就绪事件和有界匿名占用导出的近似。

## 5. load 服务：L1 常数并非主线，DRAM 存在反向误差和周期长尾

W 使用原生 packet admission/response 和 Ruby/SLICC hierarchy facts，排除 alias-parent、coalescing、fragment 及生命周期不合格项，不用 path-class 补原生真值。保留 **289,101 条普通单行 load**：285,516 个 native L1 hit、3,585 个 native DRAM read。分类按 native 路径分组，未声称模型逐条都走相同层级。

| native 分组 | 量 | gem5 issue→数据 callback 中位数 / 均值 | FastSim issue→模型 callback 中位数 / 均值 |
|---|---:|---:|---:|
| L1 hit | 285,516 | 2 / 2.000 cycles | 2 / 2.078 cycles |
| DRAM read | 3,585 | 143 / 161.647 cycles | 164 / 168.186 cycles |

原生 load 的 O3 `completeTick` 是 AGU/执行阶段记录，不能拿它当数据返回，本轮用 native response。模型未导出 canonical request admission，因此这里只比较 CPU issue→callback，不能把它冒充两侧完全同义的 controller admission→service。

在这批合格 load 的互斥退休间隔中，数据返回前 native−model 为 **−32,508 cycles**，返回后到退休为 **+64,991 cycles**。前者表示该样本中模型反而等待更多，后者提示返回后的边/退休节奏还有差异。两个数都不能外推为全 ROI 原因份额；单条 callback→retire 的中位数也含顺序等待，不能当作纯固定写回延迟。

DRAM 分布不等价：native P99 约 **1052 cycles**、最大 1291，模型 P99 250、最大 318。native 超过 400 cycles 的合格 read 共 **48 条**，四个窗口顺序区间各 12 条，**没有一条在 ROI 之前发射**。它们不是集中在起点的跨 warmup 长等待。

进一步发现每核各 12 条，相邻 admission 间隔约 **7.695–7.879 µs**；同次 gem5 配置 `tREFI=7.8µs`、`tRFC=350ns`，当前紧凑 DRAM 模型不实现 refresh。这是吻合刷新机制的周期性证据，值得优先补充实际刷新命令时间戳后验证；现有事件不能逐条确定刷新因果，也不能把所有长尾认作 refresh。**统一提高 DRAM latency 会加重普通样本的高估，不能解决分布差异。** 原生调度/到达顺序、rank/refresh 相位、行状态也仍需区分。

排除项仍保留：28,622 条非 native packet、43,862 条 alias-parent 身份不闭合、8 条 fragment/生命周期身份不足。PC/程序顺序/原生动态序号核对并不等于独立访存物理地址认证；本轮仍未完成后者。

## 6. 哪些补偿已做控制，效果有多大

以下全部固定原始 O FST、当前修复后二进制及 recovery 开启，每行仅增加该项控制；2-cycle 执行→退休边有原生 `iewToCommitDelay` 和 tick 消费顺序依据，不是拟合值。

| 配置 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 / 单案 MAE | 相对当前增加的 active cycles |
|---|---:|---:|---:|---:|---:|
| 当前 | 1.000925 | 0.880284 | −12.0529% | 0.120641 | 0 |
| 加 `execute_to_commit=2` | 1.000925 | 0.882866 | −11.7950% | 0.118059 | +60,908 |
| 加普通 store lifecycle | 1.000925 | 0.877743 | −12.3068% | 0.123182 | −59,952 |
| 加 `branch.speculative_history=true` | 1.000925 | 0.879817 | −12.0996% | 0.121108 | −11,012 |

三项完整来源/人口/native 门禁均通过。这些净变化不是可相加的原因份额，包含 warmup 和后续资源交错变化。

执行→退休边只补回现有 gap 的约 **2.14%**，而全局选项还影响 store/特殊操作，不能直接当作逐类修复完成。普通 store lifecycle 没有改善本案。推测历史更新在 gem5 确实为 true、当前模型为 false，但在 W 开关两边的合格分支位置完全相同，完整 O CPI 也没有改善；它是语义差异，不支持作为当前主因。

部分 response 资源反馈、rename free-list 选项未开启是配置事实，不是遗漏原因已经认证；本轮没有通过批量开关搜索挑最接近的结果。各机制必须先确认与目标的事件、资源语义一致，再做独立控制。

## 7. 冷启动、调度和历史结果的解释边界

Q 每核按指令顺序八等分后合并，退休跨度差分别为 **348,261 / 365,633 / 357,603 / 367,330 / 384,289 / 365,739 / 348,140 / 335,004 cycles**。每段相对于原生该段跨度的差约 **11.59%–13.03%**，没有随离开窗口起点而消退。O/Q/W 的总体 CPI 误差也接近。

所以“不足够长导致起点冷启动压倒一切”不符合目前证据。尚未扫描 warmup 长度，不能排除所有间接预热影响；但单纯扩大窗口不应当作这次的主要修复。

当前误差热点是：`sqlite3VdbeExec` **+1,863,164**、`sqlite3BtreeNext` **+455,640**、`sqlite3VdbeSerialGet` **+335,756**、`getPageNormal` **+150,554 cycles**。它们是最终退休指令归属的定位，不是函数独立因果成本。热点 `0x48be62` 的字节 RMW 和 `0x48be66` 的 load 值得继续核对 forwarding/同线顺序；尚未完成身份和服务配对，不能先宣布 store-load alias 是根因。

本轮没有新增 scheduler 开关消融，不能声称已独立排除调度对时序的全部影响；人口、未知 owner、active/elapsed 差异不支持“只是 scheduler 统计口径错了”解释。历史其他负载的 5%–7% 也不是当前 SQLite 的同源基准，它们不能证明本窗口应自然达到同样精度。

## 8. 下一步应按事件修，不按 CPI 调参

1. **先区分恢复检测来源。** 覆盖 Decode 可知目标错误、后端方向/间接目标解析；对上述两组真实事件验证正确路径 fetch 边，避免用统一 completion+penalty 吸收正负错误。将本轮对“所有 miss 必须等执行完成”的解释修正进入设计。
2. **核对正确路径实际准入及资源释放。** 现有两阶段 response 会移动部分阶段时间，但当前批次 canonical cache 选择并未全部重新准入。需要在首个发生变化的请求检查 admission→parent/service→callback→消费者→退休的因果闭合，不能只移动最终回调/退休。
3. **用原生错误路径验证匿名资源模型。** 独立核对容量、占用/取消时刻和实际 exposed 阻塞；生产输入继续是功能指令流，不导入 gem5 错误路径或时钟。不要把缺少错误路径字节等同于必须收完整错误路径 trace。
4. **补 DRAM 刷新取证和模型评估。** 先验证周期长尾的实际命令因果、rank/相位和资源重叠，再决定刷新事件的必要精度；与普通服务高估一起评估，不能只看长尾改善。
5. **逐类补齐退休边界后回归 SQLite/Zstd。** 保留互斥周期账和同源控制，再测吞吐；本轮没有新的 quiet-host 吞吐测量，也没有宣称修复后准确率。

## 9. 验证、审核与存储

生产 `build/fastsim` SHA256 仍为 `62d1f9f652621fd75260e63ffa886b3d777c300edb665331db31365ba60c76ad`，`src/simulator.cpp` 仍为 `319d7e8cce5e7a284cf7b9a0835ccb5d0efa1147b181069a7e4c507d36a783e4`。阶段导出只在隔离观察二进制中增加；Q/W 的生产与观察目标输出深比较完全中性，仅排除已声明的宿主 timer/吞吐/宿主调度统计。

本轮 22 项分支解析、纠正来源、流式阶段解析、边界配对、周期守恒测试通过；独立只读审核确认逐分支人口、原生动态序号和模型序号连续，没有阶段洞。审核指出原脚本把 active−span 的代数残差称为“独立守恒修正”，本轮已改为未分配边界/范围残差，数字不变。

新增 FST **590,222,560 bytes**，全工作区 FST **29,193,494,496 bytes（29.19 GB）**，低于用户 100 GB 上限；收尾盘点磁盘剩余约 **398.61 GB**。原始海量阶段文本经 FIFO 流式解析后丢弃，保留紧凑阶段及必要分支记录，没有为本轮复制完整 FST。观察导出的宿主耗时不能当作生产吞吐。

可复核入口：

- [汇总与 27 项范围/一致性检查](../tmp/sqlite-residual-after-tail-20261020/evidence.json)、[完整阶段源维护验证](../tmp/sqlite-residual-after-tail-20261020/validation-current-stage/cases/c04-708.sqlite_r/validation.json)。汇总检查通过不等于 W 完整 FS validator 通过。
- [互斥周期账](../tmp/sqlite-residual-after-tail-20261020/paired-comparison.json)、[退休密度](../tmp/sqlite-residual-after-tail-20261020/pipeline-summary.json)、[函数定位](../tmp/sqlite-residual-after-tail-20261020/symbolized-comparison.json)。
- [逐分支位置匹配与错误路径活动](../tmp/sqlite-residual-after-tail-20261020/short/branch-position-analysis.json)、[原生纠正阶段对照](../tmp/sqlite-residual-after-tail-20261020/short/redirect-analysis.json)、[load 生命周期](../tmp/sqlite-residual-after-tail-20261020/short/load-analysis.json)、[周期长尾与刷新候选](../tmp/sqlite-residual-after-tail-20261020/refresh-candidate.json)。
- [实际参数审核](../tmp/sqlite-residual-after-tail-20261020/parameter-audit.json)、[采集命令和冻结身份](../tmp/sqlite-residual-after-tail-20261020/short/freeze-measurement.json)、[短窗口未通过的完整 FS 验证](../tmp/sqlite-residual-after-tail-20261020/short/validation-current/cases/c04-708.sqlite_r/validation.json)、[22 项诊断测试](../tmp/sqlite-residual-after-tail-20261020/short/diagnostic-tests.log)。
