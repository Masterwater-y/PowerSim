# SQLite、Marian、XZ 的 CPI 误差排查（2026-10-21）

本轮复用此前的“固定来源与参数→核对人口→配对退休周期→定位阶段/函数→单机制对照”方法。结论的强度不同：**XZ 的保守 store prepare 准入得到强同源控制证据；SQLite 的分支错误发现阶段存在直接反例，但12%未完全归因；Marian 定位到后段拷贝，确认软件预取语义缺口，但提前CPU确认并未改善总CPI。** 不把等待指令位置、累计stall或接近的CPI当作完整因果证明。

## 1. 固定基线和计量人口

原正式来源及当前生产模型保持不变，四核、8通道、buffer TSO114、response branch recovery开启。SQLite是四个标准rate副本；Marian是一个官方speed进程；XZ是官方speed二进制、官方train第二条40MiB输入。CPI均为workload-task active cycles/宏指令，elapsed另外计量，不跨核相加。

| 原正式窗口 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| SQLite | 1.000924991 | 0.880284074 | −12.0529% | 0.120640917 |
| Marian | 2.665784226 | 3.175938392 | +19.1371% | 0.510154166 |
| XZ | 1.062180834 | 1.494112270 | +40.6646% | 0.431931435 |

表中是三个负载各一个原窗口，控制变体不增加独立样本数。生产FastSim SHA256仍为`62d1f9f652621fd75260e63ffa886b3d777c300edb665331db31365ba60c76ad`，生产`src/simulator.cpp`仍为`319d7e8cce5e7a284cf7b9a0835ccb5d0efa1147b181069a7e4c507d36a783e4`。本轮只在隔离副本观察和构建反事实，没有修改生产模型、默认开关或时延常数。

新补采的Marian/XZ诊断源与原正式源是不同的自然窗口；逐指令时间只在同次采集的FST与原生日志之间配对。原生时间、命中路径、错误路径日志不进入FastSim生产输入。缓存参考只读取维护验证器审核的守恒Ruby/SLICC摘要，不使用path-class代理。

## 2. SQLite：持续低估，分支恢复起点不统一，后继过早就绪

### 2.1 误差不是只在开头，也不是commit width配错

本轮从旧诊断的阶段二进制独立重算了39,869,125条配对UOP、七项互斥退休账、八段分布与退休直方图，全部复现。原正式源重新维护rescore通过，三个观察源的独立硬件身份3/3有效，22项解析/配对测试和8项原始数据复算检查通过。

完整阶段源Q的时间账如下。正数表示gem5花的时间更多；每个时间区间只分配一次，避免把并行等待累加成原因。

| 时间落点 | gem5 cycles | FastSim cycles | gem5−FastSim |
|---|---:|---:|---:|
| 有退休发生的周期 | 9,702,339 | 7,639,476 | +2,062,863 |
| 下条尚未取指 | 138,530 | 77,901 | +60,629 |
| 已取指、尚未最终发射 | 319,652 | 357,233 | −37,581 |
| load已发射、尚未退休 | 13,388,683 | 12,677,201 | +711,482 |
| store已发射、尚未退休 | 4,594 | 1,507 | +3,087 |
| 其他已发射、尚未退休 | 73,120 | 1,605 | +71,515 |
| 未知阶段 | 4 | 0 | +4 |

配对跨度差2,871,999cycles，Q的active差2,842,297.879880，剩余−29,701.120120是未独立拆分的起止/共同终点/范围代数残差，保留为残差。

有退休周期平均每周期退休UOP为gem54.109、FastSim5.219；两侧最大都是8。实际fetch/decode/rename/dispatch/issue/commit width都是8，ROB192、LQ32、SQ114匹配。模型把后继更密地积攒到一起完成退休，不能据此把commit width调小。

八个顺序区间的跨度差分别为348,261/365,633/357,603/367,330/384,289/365,739/348,140/335,004cycles，相对各段native跨度约11.59%–13.03%，没有离开起点后消失。没有做warmup收敛扫描，因此不能排除所有预热影响，但证据不支持把主要误差归结为一次固定冷启动费用。

### 2.2 已确认的分支机制错误：错误在什么阶段能被发现

短观察源W的合格逐分支位置一致，仍存在两类相反的恢复时间误差：

| 同一动态分支的`下一条正确路径fetch−分支执行完成` | 数量 | gem5中位数 | FastSim中位数 |
|---|---:|---:|---:|
| 后端纠正，观察到DirectCond | 3,444 | +5 cycles | +2 cycles |
| Decode纠正，观察到DirectUncond | 3,394 | −1 cycle | +2 cycles |

条件方向错误需要等结果；直接目标错误可能在Decode就能发现。当前统一从执行完成开始计算恢复，导致前一类恢复偏早、后一类偏晚。gem5还允许直接条件分支的某些目标错误在Decode纠正，不能仅按DirectCond/DirectUncond硬分，也不能把统一penalty从2改成5。

先前尾部传播修复确实有效，Q中136,936个模型误预测/后继的内部尾部约束违规为0；但“模型内部传播一致”不等于恢复起点符合目标。

W恰好没有kernel记录/访存，完整FS验证为failed；它只提供身份闭合的局部分支机制证据，不进入完整精度集。

### 2.3 有证据但尚未隔离的剩余来源

- **错误路径后端资源占用。** 合格错分支关联原生取消路径含413,305条renamed、262,696条issued、69,906条访存。当前推测取指shadow不等价覆盖ROB/IQ/LSQ/FU/cache占用。它提供了资源阻塞通路，但不能把指令数除以宽度当额外周期。部署端仍只需要功能流；错误路径日志是开发验证工具，不是要求QEMU/DynamoRIO用户提供原生错误路径。
- **普通服务与长尾同时错配。** 合格DRAM读取issue→callback均值native161.65、model168.19cycles，普通服务模型反而较慢；native P99约1052、model250，长尾又缺失。48条长尾在各四分位均出现，周期约7.8µs，吻合refresh但尚无命令级因果。统一增大DRAM延迟会加重普通样本高估。
- **单独补执行→退休边效果小。** `execute_to_commit=2`只补回60,908active cycles，约原gap的2.14%；普通store lifecycle和推测历史单项控制没有改善。SQLite用户态软件预取执行计数为0，正式窗口NO_ACCESS为0，不能把Marian预取或Zstd CDA解释套用到它。

优先工作是把错误发现阶段→控制传播→正确路径fetch→真实cache请求准入→response→消费者/退休闭合。先定位首个被提前的实际请求，再检验匿名错误路径资源模型。完整12.05%的因果份额仍未划清。

详细复核：[SQLite报告](../tmp/cpi-three-workload-diagnosis-20261021/sqlite/report.md)、[机器证据](../tmp/cpi-three-workload-diagnosis-20261021/sqlite/evidence.json)。

## 3. Marian：主要正差额在后段拷贝，预取身份缺失是真的，但不是已证明的主因

### 3.1 定位到阶段，排除几个直接解释

四核模型本地结束约16.43–16.66Mcycles，原生共同结束约13.99M；四核都慢，不是一个异常核独自拉长整个结果。共同窗口扩展/范围差410,062cycles，远小于10,704,100cycles总active正差。测量期没有显式依赖等待、unknown owner为0，主/辅助线程没有测量人口。

原正式源CPU0的同序号稀疏原生事件表明：

| 原正式源CPU0区间 | native cycles | model cycles | 现象 |
|---|---:|---:|---|
| 起点至首个可配对测量事件，含主要tensor wrapper阶段 | 4,599,761.97 | 4,893,865 | 累计多约294K，约6.39% |
| 后续memmove/Transpose主要区间 | 5,986,067 | 7,360,648 | 多1,374,581，约22.96% |
| 最后主要仍为拷贝的尾段 | 约3,384,356 | 4,168,742 | 继续多约784K |

稀疏事件只能定位区间，不能称第一个全局分歧已经找到。模型时钟全部先减去warmup原点1,990,458，再与native测量起点比较。后段误差仍增加，不支持开头冷启动支配整个19%。

实际SIMD FU数量4、相关向量运算延迟1cycle与模型一致；热点`float32x4`函数还包含大量scalar callback、间接调用和栈访存，名字不是算术吞吐缺陷的证据。关闭branch recovery只减少7,249active cycles：CPI误差仍+19.1242%，绝对误差0.509808681cycles/macro，不能把巨大累计branch gate计数当主要原因。

### 3.2 软件预取的两种完成时刻被合并了

冻结ELF完整反汇编与FST对照发现原正式源执行418,286条软件预取。它们被记录成普通load/MemRead，当前request-role只区分Data/NoAccess，没有保留software-prefetch身份。

目标RubyPort接受或跳过软件预取后会提前回应CPU，复制的请求继续占用Ruby资源并在以后填充cache；Sequencer后续回调不再第二次回应CPU。FastSim却把完整数据response钳到load completion及退休上。

新2M诊断源给出动态见证：29,080条预取中12,143条的Ruby callback晚于CPU退休。例如同一预取native issue1387→retire1395，但Ruby数据callback约1614.97；模型issue630→response880→retire881。两个模拟器时间原点分别处理，比较的是各自同一指令的阶段间隔。不能套用普通load的`callback≤retire`过滤规则，否则会错误丢掉合法的软件预取。

### 3.3 重要反证：移除预取CPU等待没有修好总CPI

隔离控制使用冻结ELF中**全部**预取指令的纯功能ISA map，不输入native时间或cache命中。保留canonical请求、fill与sequencer/MSHR服务释放，CPU完成及IQ/LQ释放随候选规则变化；真实接受/重试/冗余策略尚未补齐。未提供map时，完整目标输出与生产结果深比较中性。

| 原正式FST同源模式 | gem5 CPI | FastSim CPI | 相对误差 | 绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| 当前 | 2.665784226 | 3.175938392 | +19.1371% | 0.510154166 |
| 理想预取提前CPU确认反事实 | 2.665784226 | 3.195403518 | +19.8673% | 0.529619292 |

预取作为最终阻塞指令所分配的模型退休间隔从35,225,243降到162,398cycles，总active反而多408,419cycles。等待的位置和交错发生变化，不能将35M当作可消除成本，更不能声称已经解释或解决19%。语义缺陷成立，主要CPI贡献仍未证明。

后续应沿后段copy的实际请求准入→prefetch CPU ack→Ruby fill/资源释放→随后demand load重算因果；仅改最终completion不够。DTLB timing miss124,211与functional miss10,941、原生约9,949的量级差是另一个具体候选，但native后者含推测范围，缺少逐次walk配对，17.86M累计walk等待也不能直接从总gap扣除。gem5 walker不合并未完成请求，不能靠打开DTLB合并凑CPI。

新2M源误差+9.7603%、绝对差0.132536646cycles/macro，覆盖早期copy/算子而非原正式后段长copy，不能报告成19%已经改善。该源维护验证与中性观察通过、完整PC顺序配对无前后洞，236cycles未知阶段保留。首次补采因owner切换不一致在stat前被拒绝，原参数重试成功；没有放松门禁。

详细取证：[Marian报告](../tmp/cpi-three-workload-diagnosis-20261021/marian/report.md)、[机器证据](../tmp/cpi-three-workload-diagnosis-20261021/marian/evidence.json)、[独立预取审核](../tmp/cpi-three-workload-diagnosis-20261021/sqlite/prefetch-independent-review.md)。

## 4. XZ：保守store prepare准入、copy预取、共同终点放大，以及ROI重复开销

### 4.1 最慢CPU0把共同窗口拖长

原模型各核本地结束为7,889,968/5,720,183/5,109,008/5,116,503cycles，原生共同elapsed5,609,064.97。模型本地跨度和23,835,662，加共同窗口EOF扩展7,724,210，等于报告的active31,559,872；native active22,436,259.88。EOF扩展是现有共同窗口合同的一部分，不是独立硬件stall，不能直接删去来降低CPI。

中性观察中CPU0分配到memmove的退休间隔3,709,551cycles、clear_page_rep3,470,572，两者约占本地跨度91%。原源早期可配对事件已多504,226cycles。问题集中在生产者所在慢核，不能用四核平均掩盖它。

新2M诊断来源中，完整PC顺序配对无前后洞，27cycles未知阶段保留；同一批clear_page_rep的1,219,520UOP，退休间隔native634,355、model1,949,790，多1,315,435cycles。完整配对物理跨度净差仅925,380，说明其他位置有反方向误差，不能把该函数差额当互斥原因份额。

实际热点是`REP STOSQ`。该REP PC在CPU0的fetch→最后issue均值native28.54、model1443.27cycles，issue→retire没有相同放大；互斥账中“已取指、尚未最终发射”多1,146,053cycles。它指向发射前结构/顺序/翻译等待传播，不能把issueTick重试间隙全部叫作IQ等待，也不能把重叠均值相加。

### 4.2 同源控制强烈指向store prepare准入

默认预筛选只要同epoch后缀有同set另一条line便拒绝prepare；真实LRU只在足够多distinct line挤占ways时才必然驱逐。页清零和批量写容易触发过严条件，后继沿FIFO/SQ反压放大。

| 原正式FST模式 | gem5 CPI | FastSim CPI | 相对误差 | 绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| 当前基线 | 1.062180834 | 1.494112270 | +40.6646% | 0.431931435 |
| 仅L1 LRU驻留证书候选 | 1.062180834 | 1.037433961 | −2.3298% | 0.024746874 |
| 仅移除local prepare响应floor反事实 | 1.062180834 | 1.405157691 | +32.2899% | 0.342976857 |
| 仅软件预取提前CPU确认反事实 | 1.062180834 | 1.313870280 | +23.6955% | 0.251689446 |

所有行均经维护validator，人口匹配，控制源码/ISA map另外绑定哈希。证书候选把CPU0结束降到5,478,384cycles，是主要慢核对该路径敏感的直接证据。原有证书没有自动启用；此次不是重新修复一个已默认开启的机制。

但−2.33%不是生产修复认证：证书放宽可能暴露pre-retire permission/store-drain的另一项近似，warmup与cache交错也会变化；预取缺陷依然存在，接近的CPI可能包含抵消。移除floor违反真实canWB/扫描约束的完整要求；预取提前确认控制没有真实接受/重试，因此均不能直接采用。各控制净收益也不能相加。

下一步应逐store记录prepare准入/拒绝原因、实际canWB、permission接受、FIFO发送/响应、SQ释放和首个受阻后继，把驻留条件与生命周期一起验证，而非只切换开关追求总CPI。

### 4.3 本轮采集还留下了需要先修的插桩问题

`fastsim_native_roi_ready()`先执行4个getenv，再判断TLS entered。XZ入口条件在首次消费16KiB后反复成立，生产者及worker后续调用仍重复检查环境。原正式CPU0中getenv556,878UOP、strncmp562,494、strlen138,486；这些符号区间合计1,257,858UOP，分配到的模型退休间隔约284,476cycles。不是“gate只阻塞一次”就没有后续开销。

这是本次接入的采集实现问题，应把已入场检查前置并让调用点真正一次执行，再重建绑定身份的二进制/checkpoint与trace。两侧都执行了插桩，不能把284K直接当CPI误差；也不能修改旧trace删掉指令来掩盖问题。Marian原正式窗口中这几个符号的执行计数均为0，不能将污染外推到它。

XZ原正式源还有179,760条用户态软件预取，主要在CPU0；它与Marian共享prefetch身份缺口，但不解释kernel REP清零。ROI已跨过首次编码，却仍含后续输入页分配，clear_page在窗口中后段也出现。若研究稳定压缩，需要进一步明确阶段；扩大窗口或延长warmup不能替代修正插桩和store时序。

新2M自然诊断窗口误差+82.2851%、绝对差0.987288118cycles/macro；它更集中覆盖清零，不能当原40.66%的新结果或严格窗口长度对照。O3warmup95.06秒，始终低于180秒上限。

详细取证：[XZ报告](../tmp/cpi-three-workload-diagnosis-20261021/xz/report.md)、[机器证据](../tmp/cpi-three-workload-diagnosis-20261021/xz/evidence.json)、[配对周期账](../tmp/cpi-three-workload-diagnosis-20261021/native/xz/paired-comparison.json)。

## 5. 修复顺序与本轮完成边界

1. **XZ先修采集控制的重复执行。** 新二进制及checkpoint身份另建，保留当前源作为反例；随后沿clear_page_rep的store准备/生命周期做原生配对验证，不能以证书候选接近CPI替代语义验收。
2. **SQLite先区分错误发现阶段及真实准入传播。** 不统一增加branch penalty或DRAM常数；再隔离错误路径后端资源与刷新长尾。
3. **Marian补齐prefetch功能身份及ack/fill资源链，继续定位copy请求服务和译址。** 新原生见证确认了语义错误，反事实也证实单改CPU确认不够。下一证据应是第一个真实准入/服务父请求分歧，而非继续按最终head归因。

三项都没有得到完整、可相加的CPI原因份额。原有参数和门禁不放宽；退休cache的unknown仍明确保留，不把已知子集的百分比充当完整perf精度。没有新的吞吐结论，也没有将任何候选写回生产默认。所有新增FST均受100GB全局上限保护，原始debug文本经FIFO解析后丢弃；完整命令、哈希、失败记录和收据保留在[本轮实验目录](../tmp/cpi-three-workload-diagnosis-20261021/)中。

本轮共核对10份维护验证报告全部通过，观察版及无ISA map控制的目标输出中性；另保留Marian首轮producer失败与SQLite W完整FS失败的证据，不将失败源计入正式结果。新增有效诊断FST共2,263,996,480bytes，最终全项目FST约45.27GB，磁盘空闲约306GiB。生产源码/二进制哈希保持不变，详见[验证收据](../tmp/cpi-three-workload-diagnosis-20261021/verification.json)。
