# SPR store buffer、硬件预取与 TBE 的两阶段近似设计

日期：2026-11-05。状态：实施中；整体尚未实现或验证。目标是在现有 FastSim
并行两阶段框架中表达 gem5-spr 的主要资源和重叠关系，保留高吞吐，不建立
逐周期 CPU、另一套执行主循环或整窗反复收敛。本文提出的默认关闭开关与
状态结构是设计名称，不代表当前配置项已经存在。

### 独立 Sequencer prepare 信用的接线调查

新四案正式配置均为 `ruby_sequencer_max_outstanding=16`、
`ruby_sequencer_load_admission=false`、`buffer_tso_prepare_lifecycle=false`，
L1D/L2/LLC MSHR 分别为 256。虽然 source-order 准入实验关闭，原反馈路径
仍对非 deferred 数据请求使用每核 16 项 Sequencer 日历；因此不能说现有模型
完全没有 Sequencer 限流。该日历对旧 buffer prepare 也使用同一需求账，而且
不完整涵盖原生 CPU Sequencer 的 I-fetch 与 walker 人口；其排序及释放语义
只是近似。`ruby_sequencer_load_admission` 与 buffer TSO 的配置互斥明确
存在；前者先做无容量的完整 timing 提议 pass，再按预计 issue 顺序分配，
并非适合直接并入本方案的一遍实现。

gem5-spr 的 `Sequencer::makeRequest` 对普通请求和 store prepare 使用独立
计数：本案例各有 16 项，prepare 达上限时直接丢弃 hint 并返回成功，不得
阻塞普通 demand；普通同线 aliased 请求仍计入 demand outstanding。控制器
`number_of_TBEs=256` 是另一个资源，不能把这三个容量合并。原生
`LSQUnit::drainCommittedStoreBuffer` 之后仍发送普通 head 写请求；未合并
的 17 条独立 cache line 即便只有 16 个 hint 可接受，也仍需要 17 个实际
head demand，而不是仅为被丢弃的 hint 补一次 demand。

现有默认 FastSim buffer 的 clean prepare 是唯一的层级访问，之后 publish
直接在已准备行上修改状态；`replay_buffered_tso_head_demand` 要求 L1/L2
已经持权。仅将 prepare 从需求日历挪到独立日历，会遗漏全部 head demand；
将 dropped hint 当作原 issue 时普通写，则会把写入提前暴露给其他请求。
现有 group 事务也只能证明 clean prepare 与最终 publish，不能单独解决此
缺口。

候选实现是在现有阶段二窗口内区分模型 hint 与 head 两类请求，分别对接
有界 prepare 日历和现有 demand 日历，各自真实访问同一层级；按阶段一模型
预测的 ready/retire 边做一次稳定接受排序，在最终 feedback 后用同窗口
后续事件与全核未来水位认证服务次序，不成立时恢复整个事务并至多一次
回到原路径。首次切片需对未证明的多核、重排器及并行预取组合设明确范围；
不能使用 OoO 原生时间或使用反复收敛来修正。该候选接线尚未实施，难点是
为同一原始 memory event 保留两个服务结果、在退休反馈处应用 head 的
demand 信用，以及让晚于预测的 head 不向当前窗口或后续窗口泄露缓存状态。

可审查的最小接线应满足下列约束：

1. 仅在 opt-in 下，为每个合格 store 保存 `(core, sequence, fragment)`、
   hint 结果及唯一 head 结果；两个结果不能覆盖目前以 memory 索引寻址的
   单个 `event_feedback`。hint 使用的地址和属性来自 FST 功能字段。
2. 阶段二以阶段一预测的模型边一次性生成和稳定排序 typed hint/head 请求。
   同核 prepare 采用独立的固定容量完成日历；满额丢 hint，不更新 demand
   日历或层级。head 无论 hint 接受与否均访问实际私有/共享层级并占普通
   Sequencer 项；是否真的产生下层请求由模型自己的 cache 状态确定。
3. 单次 response feedback 消费 head 的真实模型服务结果并输出最终 send、
   callback；不得把 hint 的延迟直接当成 head 的完整服务。若实际 send
   越过排在其后的任一相关模型请求，或 callback 越过未消费水位，不能
   提交预测时对 cache、目录及 DRAM 的提前可见状态。对无法认证的窗口
   全部恢复入口事务、每核两类信用及统计，再走原路径最多一次。
4. 第一版应限制支持的队列、事件与时钟范围，显式记录未覆盖/回退人口；
   禁止把顺序不确定事件静默计作零等待。先用不同线和同线、满/空信用、
   跨 Q、事务回退、特殊事件与真实 C4 条件验证请求和退休人口，再单独
   测量端到端吞吐。当前候选需要对单个 memory event 进行双结果接线及
   在现有反馈内暴露 head send，尚不能据此假定只需修改一个配置开关。

新增的独立 `tests/test_spr_prepare_credit_runtime.cpp` 是未注册的 RED 夹具：
它用功能 store 构造期望的 16 个 hint 接受、1 个丢弃及 17 个 head demand，
旧模型因尚无该分账而失败。17 次提示是否确实同时在途以及后续生命周期
必须由实现的事件证据验证；目前不能把这项失败当作功能实现或性能结果。

后续在隔离源码副本中完成了极窄单核 EOF 试验：真实 Simulator 合成夹具的
16 个同时在途 hint、1 次丢弃及 17 个独立 head 请求通过，容量 2 的对照和
一次回退／OFF 等价检查也通过。它没有合入，不能覆盖真实 C4；混合 load、
多核、预热和跨 Q 仍需双事件身份、时间顺序和缓存可见性接线。细节及
为什么停止扩大该合成子域见
[prepare 信用与混合窗口调查](spr-prepare-credit-mixed-window-20261105.md)。

实施记录：已开始实验性 L1 普通 load fill 信用切片，开关
`core.spr_load_fill_feedback` 默认关闭，容量 `core.spr_l1d_fill_entries`
和 `core.spr_l2_fill_entries` 默认 16/48。该切片只对模型判断为 miss 的
经功能属性确认的普通 load 使用跨 chunk 小型信用日历；未知或特殊属性
回退原路径并单独计数。L2 的到达以 L1 hit 延迟估算，
其释放暂以端到端 callback 近似。store、硬件预取、独立 TBE 和完整同线
合并尚未接入，不能把它称为本方案的完整资源账。该开关暂不与旧的
pending-fill、固定 read 或 coalescing 等竞争求解器组合。由于现有反馈
按程序序处理而预测 issue 可倒序，实验实现使用按信用槽保存活跃时间区间
的有界预约，以允许较早请求填入已有预约之前的空档；没有逐周期推进。
输出 `spr_fill_candidates`、`spr_fill_calendar_fallbacks` 和
`spr_fill_extension_conflicts`：宿主记录达到边界时回退，或后续反馈延长
已预约区间并与既有未来预约冲突时不回滚整窗；对应压力可能低估。未评估
这些人口和准确性之前保持关闭。合成负载的 L1/L2 压力、跨 chunk、
转发排除和优化／通用执行核一致性已通过。完整 FLAC C4 10M ROI 回放
中，普通 load 候选 8856，L1/L2 容量等待、预约回退和延长冲突均为 0；
实验开关开、关的任务 CPI 均为 0.5003054926。这是补齐普通 load 功能
属性准入前的历史结果。属性准入修正后，启用 store 窗口的同源 FLAC 回放
有 8664 个候选、193 个属性不明或特殊回退，fill 等待／日历回退／预约
延长冲突均为 0。此次未观察到 fill 压力。相同二进制、FST 的单次顺序 A/B：关闭／开启
fill 和 store 窗口时，预热分别为 7.619／6.934 秒，测量分别为
53.134／53.205 秒，预热加测量分别为 60.753／60.139 秒；测量阶段
增加约 0.13%。同机还有 PMU 审计，单次结果不构成四负载吞吐验收。
最新版本的 validator 来源与人口检查已通过，但三项退休 miss 的原生
覆盖仍不完整。

新增实验性 `core.spr_store_window_feedback`（默认关闭）接入原有 response
feedback，用有界行窗口近似普通 store 的发送重叠；行窗口默认 4，发送宽度
沿用 `core.cache_store_ports`。仅功能属性明确的普通单行 store 可进入；
属性缺失、特殊请求等继续走保序路径。窗口跨 chunk 保持状态并计数进入和
回退人口。该切片尚不实现 committed buffer 的合并、offload、共享预算、
prepare、Sequencer 别名及 TBE，不能称为完整 store buffer 对齐；开启前
须评估其与最终共享事务表整合方式。FLAC C4 单次全量回放中，1043397 次
普通 store 进入窗口、1939 次普通写回退；相比相同 FST、属性准入修复前 fill 开关开启的
原窗口路径，任务 CPI 从 0.500305493 降至 0.489617558（下降 2.14%）。
此前同源原生任务 CPI 为 0.282346215，当前绝对误差为 0.207271343
cycles/宏指令、相对误差为 +73.41%。这些数据使用已声明的任务窗口，
并未修正各核起点差异。属性准入修复后的带身份回放仍为 0.489617558；
用已审计参考和现有退休事件合同核对输入身份、任务和逐核人口后，L1
miss 估计为 7925，对照 4016（+97.34%）。[正式重评分报告](../tmp/spr-iq-dev/store-qualified-rescore/retired-cache-summary.md)
的来源与人口检查通过；其余两级仍因原生 unknown 不可评分；模型中有 2153450 次退休 load 使用
回退估计。该回退可能由影子表同槽冲突、时间顺序不确定或无效响应区间
触发，不能仅凭总数确定各原因及它们对误差的贡献。功能文件中 store 和
load 属性可以由 Atomic/QEMU 独立生成仍需单独证明。

PMU 报告区分退休 load 的 perf-like 三项与 Ruby tag 诊断；非零 native
unknown 的事件不可评分，逐事件报告允许保留其他完整事件，但不表示三项
整体完整。所属任务、流身份及完整人口仍须分别核验。当前 FLAC 的 workload-task
原生参考中 L1 unknown 为 0，L2 为 1080、L3 为 1094；后两项的误差
必须不可用。属性准入修复前的单次对照通过来源／人口校验：退休 L1 load
miss 原生 4016、模型估计 7924（+97.31%）；模型对 2239473 次 load 使用
回退估计，结果不代表硬件 perf 等价。详见
[逐事件报告](../tmp/spr-iq-dev/per-event-rescore/retired-cache-summary.md)。gem5 侧 unknown 可能由下级访问与上级 miss 数、各级结果
守恒或请求完成归属无法证明触发；现有汇总不足以定位每项具体原因，
不能把 unknown 简单计作 hit 或 miss。新增诊断属于 OoO baseline 采集，
不能混入 FastSim 的 Atomic 级 FST 输入。

## 1. 输入合同、对照和目标参数

- 输入仅为退休指令流和可由 Atomic 级或 QEMU 功能执行获得的事实：PC、
  指令类别与次序、依赖、load/store 地址及长度、字节使能、请求属性、
  地址空间／线程身份，以及预热记录。gem5 OoO 只生成 CPI、PMU 和机制
  统计等独立对照；其动态发射／完成时间、缓存命中、实际预取和 TBE
  命中不作为模型输入。当前 OoO 采集输出的功能字段能够由独立 Atomic／
  QEMU 生产者等价生成尚未全部证明；缺字段时必须明确降级范围，不以
  原生动态时序替代。
- gem5-spr 实际 FLAC 配置：SQ 和 committed buffer 共用 112 项预算；
  buffer 的不同在途 line 窗口 4；L1 CPU Sequencer 普通 outstanding 16，
  store-prepare 独立 outstanding 16；L1D fill 16、私有 L2 fill 48；
  L1/L2/LLC 控制器的 `number_of_TBEs` 在该案例分别为 256，LLC 按 bank
  独立。所有值从每案 `config.ini` 映射，不作为适配某负载的常数。
- 参数命名需遵守 gem5 的层次：`l1_controllers` 实际是最靠近核的 Ruby
  L0 控制器，`l2_controllers` 是私有 L2 的 Ruby L1 控制器，`l3_controllers`
  是共享 LLC 控制器。上面的 16 是 CPU L1 Sequencer 容量；实际配置的私有
  L2 控制器没有名为 `sequencer` 的子对象，不能写成“L2 Sequencer=64”。
- 参考模型的资源标签不应混成 MSHR：Sequencer 请求槽、每控制器 TBE、
  每 cache 的 fill credit、SQ／buffer 共享预算及不同 line 窗口是独立
  对象，可能同时持有、也可能只持有其中一部分。

## 2. 共同结构与单遍因果边界

阶段一沿用每核并行的 `IntervalCore` / chunk 构造，为退休流生成模型估计的
地址就绪、数据就绪、issue、retire 下界及紧凑请求描述；就绪只由输入功能
依赖和 FastSim 模型状态推导。store 的地址和数据 UOP 关联以功能可生成的
分组信息、寄存器依赖和访问范围表示；不能把 gem5 SQ 指针、native ready
时间或返回事件当输入。分组缺失时沿既有保守路径处理并计数。

阶段二沿用现有按窗口合并、cache/共享层服务和一次 response feedback。为
每核维持 store 队列摘要和本核 line 事务索引，为每个私有／共享控制器维护
资源占用摘要；共享 LLC 归属按实际 bank 映射。请求描述包括来源、line、
字节掩码、上下文、估计准入时刻及事务代次。数据 load、store、prepare 和
硬件预取通过同一套有界事务索引处理重合，但各自遵循对应资源规则。

在既有窗口顺序上，将需求事件与当窗口生成的**少量预取／store 事件**按
模型估计时间归并；仅对活跃事务保留按容量有界的释放摘要，不为每条 UOP
建立全局 event heap，也不逐周期推进。跨 chunk 保留未完成项及预取状态；
统计重置不清空热状态。遇到同一估计时刻，定义固定且可重复的 tie-break，
并明确这是 FastSim 近似顺序。

单遍框架的边界必须明确：阶段一预估 retire／issue，阶段二据此决定共享
cache 的一次性顺序；最终反馈有时会将 CPU 的实际 retire／issue 推迟。
这时不能声称已经按最终时间重排 cache，也不能悄悄迭代至收敛。记录
`predicted_send_before_corrected_eligible` 等偏差及其人口，影响时间向后通过
原 feedback 传播，未完成状态带入后续窗口；对已提交的共享分类不回滚。
特别是不能一边声称“store 必定在反馈后的退休时发送”，一边按较早预测
时间先行提交而不报告差异。可用 Q 敏感性和有限的 gem5 对照衡量该近似。

## 3. Store buffer 近似

### 3.1 状态和准入

每核两个有界摘要：未完成 SQ（沿用原 LQ/SQ 与 ROB feedback）和按程序次序
排列的 committed buffer。buffer 条目保存 `(line, context, requestor, flags,
byte_mask, first_seq, last_seq, issued, response, completed)`。只用字节掩码
判断覆盖与依赖，不计算或生成 load 的数值；后续控制流和访存地址沿用
退休功能轨迹已经确定的结果。

`SQ占用 + buffer条目数 <= 112`；并行数据结构共享一份预算，不额外分配
112+112。以第一阶段估计的退休、地址与数据就绪时间计算 offload 下界，
在程序序遍历中分配；保留 gem5 一次最多 offload 4 项的服务宽度，具体
批内时间用有界宽度日历估算，不循环模拟每个周期。

- 只有 gem5 定义的普通、可合并、已提交 store 才进入 buffer。atomic、
  fence、MMIO、NO_ACCESS、不可缓存、严格有序、split 和无法确认属性者
  分别按原有语义或明确的保守 fallback 处理；不能把它们全部视为普通写。
- offload 时从 SQ 摘掉原始项，但新建 buffer 项仍持有共享预算；若合并到
  原队尾则归还一个共享条目预算。offload 不等于下一级 Sequencer 接收。
- 合并仅限 buffer 队尾**未发送**且 line、context、requestor、flags
  相同的相邻普通 store；更新 mask 和最新 sequence，先后写相同字节由
  功能输入保证正确值。已发送项不与新 store 任意合并。
- buffer 完成事件可以乱序返回，但共享预算按队头连续完成的条目回收，
  不能把每次返回都当成立即释放。特殊 store 和 fence 对相关先后关系仍
  保持屏障，回放结束后不向已结束线程继续计入活动 cycle。

### 3.2 发送、准备、转发

按 buffer 程序序估计发送时刻，在保持顺序的同时允许最多 4 条**不同**
line 在途；同线多次在途按目标协议规则与 Sequencer alias 一起处理。不同
line 受 4-line 窗口、cache 发送端口及下级资源约束，不能简单等待前一个
store 的完整 callback；也不能把所有 store 变成无穷并行。

对已知会退休的普通 store，以地址就绪与可用权限为准估算可选 prepare，
允许早于退休和数据就绪，但不生成任何错误路径 store。prepare 候选只来自
输入退休流；占用独立 prepare 信用，满额按照 gem5 的 hint 丢弃规则处理，
不得额外阻塞需求请求。已拥有权限、prepare 在途被需求接管、权限失效后
重新获取应是不同状态；其中权限失效／网络顺序是当前两阶段的近似，不能
声称恢复完整一致性状态机。prepare 命中不等同 store 写回，不能直接归还
buffer 条目。

load 在模型访问时按地址／掩码检查更老的未完成 store。覆盖完整且预计数据
已就绪时计作 forwarding；部分覆盖或数据未就绪时按保守等待／既有路径
处理，不假装拿到了数值。普通跨页 load 只使用已知的分片映射；地址／属性
未知不能推断不重叠。对应数据和地址的就绪均来自模型预测的依赖，而不是
原生 O3 观察。

例：A、B、C、D 是 4 条不同 line 的普通已提交 store，E 是第五条。A–D
可以按端口和下级信用逐个进入在途窗口，不必等 A 的响应才发 B；E 在无
可用窗口前受阻。若 D 后紧跟尚未发送、同上下文同属性的 D2，则 D2 可与
队尾 D 合并并减少一项共享预算。若 C 先返回而 A 未返回，C 的完成被记录，
buffer 的共享预算仍按队头连续完成回收。

## 4. 硬件预取器近似

实施衔接：当前 `ChunkMemoryEvent` 保留 cache line，但没有预取训练所需的
PC、虚拟行和 context；`preview_memory_event` 还可能参与事务预览与回滚。
因此需要为启用预取的事件附加有界的功能元数据，并把预测器状态的提交／
回滚与现有 private transaction 对齐，再将候选纳入共享服务批次；不能在
预览时直接安装 cache line 或将预览产生的候选重复提交。

使用模型预测的访问顺序及**模型自己的** cache hit/miss、fill 可见性训练；
不从 gem5 trace 读取预取、命中或返回时间，也不读取退休流未来行地址。
当前目标的预取器是 RubyPrefetcher，具体行为见
[源码](../gem5-spr/src/mem/ruby/structures/RubyPrefetcher.cc:120)：

- L1 `ip_stride=true`：16 组×4 路的 PC/context stride 表，在虚拟行地址
  上更新，同页内相同非零 stride 达到源码的 4 样本后，生成下一条候选。
  同时保留正／负单位步长和非单位步长 miss 过滤器与 stream 的近似行为；
  `unit_filter=8`、`nonunit_filter=8`、`num_streams=4`、
  `train_misses=4`、`num_startup_pfs=1` 是目标采集配置。
- 私有 L2 `ip_stride=false`，但仍有 Ruby stride 过滤器与 stream；在源码
  对可训练 demand GETS/GETX 的 hit/miss 条件下，还尝试 `line XOR 64B`
  的相邻行。不能将其简化为“L2 无预取”或复制 L1 的 PC-stride。
- 对目标的不可缓存、strict-order、取指、软件预取、page walk、secure 等
  排除条件按相应层源码及功能属性处理；训练只来自目标实际允许的模型
  事件。未知属性不伪造普通可缓存请求。
- 目标 `cross_page=false`、`page_shift=12`，只发同一 4 KiB 页内已知映射
  的候选；同页内通过当前功能映射获得物理目标，不借用将来指令的翻译。
  映射不确定时不生成该候选并统计 `translation_unavailable`。
- 将有效候选插入原 cache/shared-memory 服务路径，单独标记硬件预取；
  其请求排队、带宽、占用的 fill/TBE、缓存安装和替换污染都要计入模型。
  已存在／排队中候选做目标式去重；target 已在途的 demand 与 prefetch
  根据事务类型合并或等待，而不是把未完成的预取当立即命中。
- L1 和 L2 训练、队列、跨 chunk 状态分别维护，不让 L1 候选同时被伪造为
  L2 demand。预取只影响模型状态、流量和性能，不计作退休指令或退休 load。

控制器 MessageBuffer 的实际 `buffer_size=0` 不能未经证据解释为硬件队列
长度 0 或固定 16。实现可用窗口化延迟消费限制宿主内存：待处理候选只能
随实际输入事件产生，队列去重后按源模型优先级处理，跨窗口保留。若为
吞吐设置额外队列上限／批量截断，必须独立配置并统计被截断项，属于模型
近似；不能宣称是 gem5 的原生容量，也不能静默丢弃或虚增需求资源。

## 5. TBE、fill 和 Sequencer 预算

统一请求生命周期按控制器层次逐级推进，每层只在预计实际到达该层时取得
本层资源，不能在上层准入时提前占用未来下级额度：

| 对象 | 本案例容量 | 预计分配 | 预计释放 |
|---|---:|---|---|
| CPU Sequencer 普通 outstanding | 16／核 | 普通 CPU 请求被接纳；同线 alias 也可占请求项 | 相应模型 callback |
| store-prepare outstanding | 16／核 | prepare hint 接受；满时 hint 被丢弃 | prepare 完成／取消 |
| L1D fill | 16／核 | L1 需分配数据 fill 的新事务 | 相关 L1 TBE 的模型释放边 |
| 私有 L2 fill | 48／核 | 私有 L2 分配 fill 的事务 | 相关 L2 TBE 的模型释放边 |
| L1 / L2 / LLC 控制器 TBE | 256／控制器 | 对应协议需要 transient 项的 line 事务 | 该层模型的事务完成边 |
| store 不同 in-flight line | 4／核 | buffer line 发送成功 | 该 line 最后一项估计响应 |

具体是否需要 TBE/fill 按可由模型 cache 状态区分的普通 miss、upgrade、
replacement、prepare 和 prefetch 类别定义；hit 不能一律占 fill，upgrade
也不能一律与数据 fill 等同。分配和释放参照
[L1 协议](../gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm:865)及
[L2 协议](../gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L1cache.sm:912)，
未覆盖的协议分支明确列为近似而非宣称完整 MESI。

同线 alias 与 TBE 不同：CPU Sequencer 在普通请求 full 检查之后，对接收的
同线请求仍创建各自的 request 项；alias 不必各自生成一个下游行事务。
因此不能把“合并后 1 个 line 事务”误当“只占 1 个 CPU Sequencer 请求槽”。
硬件预取通过控制器预取队列进入，不能一概占用 CPU 普通 Sequencer 槽；
prepare 则经 Sequencer 的独立 hint 预算。需求 hit 时也可能占 Sequencer
至 callback，但不必占 fill。请求及控制器资源各自释放并保证不会重复归还。

每层用容量受目标配置约束的小型释放日历／稀疏堆，按批事件而非逐周期
更新；资源不足时用下一个可用释放边计算阻塞，保持请求源和事务代次。
请求、follower、预取、升级及写回分别记账，不能重复将一次 miss 计成多个
line fill，也不能把可能存在的并发协议状态强行压缩成一个无区别的时间槽。
原来的 L1/L2 MSHR=256 是代理；接入上述独立资源后要审视是否替换原预约，
避免双重收费。未修改旧求解器的互斥规则前，不得同时开启两套竞争模型。

## 6. 可观察性、验证和实施顺序

每项默认关闭并保留现有 baseline；逐项记录候选、接受／合并、实际请求、
被限额等待／丢弃、完成、峰值、fallback，以及跨 chunk 未完成项。不同
人口分别守恒：store 输入 UOP 与其 offload/非普通路径；buffer 条目产生、
合并、发送、回收；Sequencer request 与 line 事务；预取候选、去重、队列、
完成及需求命中。不要使用将 `arch = no_ruby + hierarchy + coalesced`
视为普遍恒等式：UOP、fragment、line 事务和控制器请求单位不同。

1. **资源账先行**：以合成访问验证 L1 Sequencer 16／prepare 16、L1D fill
   16／L2 fill 48、各 TBE 256 的独立分配与归还、同线 alias、hit 不占 fill、
   跨 chunk 和队列饱和。用 target 实际参数，不解除 bounded_analytic 的
   非瓶颈校验去伪装支持。确认资源账与现有 MSHR 路径不双重计费。
2. **store 合流**：验证同线／异线、第四与第五个 line、发送端口、队尾
   合并、完成乱序与队头回收、完整／部分 forwarding、fence、特殊请求和
   SQ/buffer 共享容量；再运行同源 FLAC OFF/ON。prepare 分别验证接受、
   满额丢弃、接管和失效；不需要额外 FST，只需已有功能字段足够。
3. **预取**：先 L1 再 L2，测试步长方向／重复访问／置信度、地址空间、
   页边界、去重、需求追上在途、完成后命中、污染及共享资源竞争。若
   功能 trace 缺 PC、VA、PA、context 或关键属性，对该人口标注未覆盖。
4. **效果与吞吐**：先固定一份 FLAC，分别做 baseline、资源账、store、
   L1 prefetch、L1+L2 的分层消融，使用同二进制、同输入、同目标配置；
   再运行既有四个 C4 负载。报告每案 CPI 绝对误差和相对误差、聚合 CPI
   MAE/MAPE、模型 PMU 与 Ruby 原生 proxy 的事件口径、已知覆盖和吞吐。
   分别记录预热、测量和完整墙钟，多次配对测量后判断吞吐门槛。建议先以
   四案吞吐几何均值至少保留 baseline 的 95% 为工程目标，并单列每案与
   尾部回退；该门槛是待确认的建议，不是已经达成的用户验收标准。
   效果没有实测前，不能承诺误差必然改善或吞吐必然达标。
5. 如需取证，可使用独立 gem5 OoO 基线的总计数和短小机制实验，不能将
   它的实际时序、动态 cache 结果或错误路径内容注入 FastSim；不需要为
   每一个模型修改重新采集。只有确认功能输入缺少不可重建字段时才改
   采集器并报告。

补充：最新 FLAC 的维护验证器在工作负载任务人口下已完成，CPI 绝对误差
0.217959 cycles/macro、相对 +77.20%；branch miss +2.14%，L1D、私有 L2、
LLC tag miss 分别 +76.48%、+400.41%、+353.91%。cache 对照来自守恒的
Ruby/SLICC 原生汇总，属于 gem5 事件代理，不是硬件退休 load PMU；
物理总体人口检查不匹配，但独立 task 人口检查通过。结果：
`tmp/spr-iq-dev/rescore/cases/c04-817.flac_s/validation.json`。

### L1 IP-stride 第一增量（2026-11-05）

新增独立 `include/fastsim/spr_ip_stride.hpp`：按本批 64B cache line、4KiB
page 的 gem5 `RubyPrefetcher::observeDataAccess` 移植 PC/context 的 16 组
4 路 LRU、4 样本门槛、步长变化、重复访问、同页停止、虚实地址转换和
secure 排除。该对象可复制，供将来 preview/rollback 保存状态。保留上游
BSD 通知。其范围仅 PC-stride 计算，不含 stream/miss filter、排队去重、
资源占用、缓存提交或与现有两阶段流水的集成，尚不生成任何实际预取。

`tests/test_spr_ip_stride.py` 用定向用例和直接提取当前 gem5-spr 源码的
300000 次随机序列对照覆盖正负步长、变化、重复、边界、context、secure
及多路冲突替换。两项测试通过；不能由此推断 CPI、PMU、吞吐或完整预取器
等价。接入之前仍须落实功能 PC/虚拟地址/属性与事务预览的提交回滚。

对新 FLAC C4 完整 FST 使用 `tools/probe_spr_ip_stride.cpp` 在每核退休程序序
读取功能事实，预热 841026/710135/693335/1206592 条 UOP 后统计测量段：
四核共 5978761 条非写 load UOP，其中 5957038 条普通 load、21723 条非普通、
未知资格 0；普通 load 的首个翻译片段均有已知页表映射。预测器给出 571848
个程序序候选。此探针没有模拟转发、实际缓存准入、预测到达顺序、队列去重、
硬件事务和在途状态，也不证明当前 OoO 采集伴随已由 Atomic/QEMU 独立生成；
不能与 gem5 原生预取数量直接比较，更不能据此宣称预取、PMU 或 CPI 已对齐。

### fill/TBE 参数独立审计（静态映射）

`tools/audit_spr_fill_mapping.py` 对给定 gem5 `config.ini` 和已生成的
FastSim JSON，按实际参与核检查 L1D/L2 `fill_capacity`、CPU Sequencer
普通请求及 prepare 信用、L1/L2/LLC 控制器 `number_of_TBEs`。普通 CPU
Sequencer 和 fill 使用对应实际配置比较，TBE 与当前 FastSim MSHR 只报告
**数值代理**；store prepare 当前无对应独立机制。缺参数、核间异构、
gem5 fill=0（不限量）或源路径不归属同一 case 均返回 `unscorable`；
填充容量或普通 Sequencer 不符返回 `mismatch`。路径绑定仅检查参与核 FST
与 config 位于同一 case，并非 source hash、目标身份、窗口、机制或精度认证，
仍需独立运行项目身份和 native 维护验证器。输出同时注明 feature 是否开启，
feature OFF 的 pass 只是当前数值配置与 native 一致。

新 FLAC C4（`bank-v4-off.json`）和历史 Graph500 C4（同批 `bank-v4-off.json`）
各自通过本静态门：四核 L1D=16、L2=48、CPU Sequencer=16，store-prepare=16
未独立表示；四核 L1/L2 控制器及八个 LLC 控制器 TBE 均为256，对应 MSHR=256
仅数值相等。两案 `core.spr_load_fill_feedback=false`，不代表资源行为对齐。
记录见 `tmp/spr-cpi-ablation-20261105/flac-fill-parameter-audit.json`、
`graph500-fill-parameter-audit.json`；两案采集不同，不合并成精度结果。

### 固定窗口 store 组的时序边界（本轮未接入）

本轮尝试用已退休功能事实与阶段一模型预测，在 canonical cache/Ruby 前为
同核同 line store 固定 owner/follower；只允许同一个已接受窗口和 producer
chunk、全核 batch 中连续、同核 UOP 连续、普通单行且身份相同的请求成组，
任何未知、特殊、NoAccess、fence 或其他核访存都截断。组选择不读取 native
OoO 时序，也不要求再做整窗迭代。阶段一预测并非阶段二实际退休：后者可能
因资源反馈分离，即使阶段一预测同周期，owner 也可能在 follower 真正能够
入 SQ 和 offload 之前已发送。因此不能把 follower 的 `actual_retire` 直接
当作已经 offload、归还共享信用的边；当预算饱和时，更不能假设整组必然能
在 owner 发送前准入。按这一不安全假设编写的本轮试验接线已撤回，默认行为
和现有预取、bank 等路径保留；独立 store 功能事实及有界摘要组件仍未接入。

后续若继续，须在保持单遍两阶段与高吞吐的前提下设计**组整体提交**：所有
组成员通过模型 SQ 信用准入并达到各自最终模型退休/offload 条件后，才能
选择该组的单次模型发送与 callback，并将每条源 store 的 SQ 释放与组 owner
保留的共享预算一致入账；特殊和未知 store 同样参与原有 SQ 预算并截断组。
任何在反馈后无法满足成组约束的情况需要事先可决定的保守路径；不能在
canonical cache/Ruby 已压掉 follower 后，静默认定 group 失效，也不能引入
逐周期仿真或新一轮整窗收敛。当前逐 UOP feedback 在处理 owner 时立即确定
其 store/SQ 时序，尚没有以有限延迟等待后续组员的联合提交接口；与固定
预测组、事务回滚及跨 Q 未消费事件的接口还需实现并测试。若引入与 OFF 不同
的 batch 排序，即使没有可合组合的 store 也可能改变 cache/coherence，须以
同排序对照和无合格组测试隔离。阶段一与二退休错位、SQ 预算饱和、跨 Q、
跨核同线访问、无 memory event 的 NoAccess/fence、组回滚与退休人口守恒是
必需的端到端测试；通过组件测试不能代替这些验证。

#### 一次受限事务回退的后续入口

一个**至多一次**的保守回退在结构上可行，区别于反复迭代求收敛，但当前尚未
实现或验证：先冻结已接受窗口的普通 store 组，快照原 shared/private 状态、
全部会变的计数和候选跟踪，做一次 canonical 组 replay 和一次两阶段反馈；
之后同时检查组整体准入/offload、FIFO 权限，以及每个组 publish 不晚于已证明
的全局安全水位。任一检查失败则还原快照，**整个窗口一次性按无组合 replay
并重算一次反馈后提交**；绝不重新选组、尝试多个参数或对同一窗口循环收敛。
不涉及候选组的窗口走原路径。成功条件和回退必须是模型功能与时序事实，不用
native OoO 决策或时间作为输入。

具体还需闭合：

1. 当前 FIFO `sq_ready[sq_next_slot]` 不能复用较年轻的 follower 已释放信用。
   需保留逻辑 SQ 准入，另建有限共享预算日历；所有普通、unknown、特殊和
   split store 都同时参加，普通组 follower 到整体 offload 边才释放共享
   预算，owner 到模型 callback 释放。二者表示不同约束，避免重复把同一预算
   加入两个竞争求解器。`SprPendingStoreGroup` 只是最多四项的组内摘要，
   不是这一全局信用日历。
2. `publish_buffered_tso_stores` 当前没有按 horizon 延迟提交；跨 Q 的未来
   发布可能提前改变 cache/目录。先证明安全水位，再只接受 publish≤水位的组；
   watermark 必须覆盖每个活跃核、未消费 producer、动态调度/同步和所有共享
   状态入口。仅在关闭 I-fetch 下层、page walk、边界种入、迁移与未认证服务的
   固定线程子域中，可研究从每核未消费事件的保守下界构造水位；不可仅用 Q
   horizon、上一次事件或局部 prefetch 时钟。对无可证明下界的核直接回退。
   也可后续实现带目录 generation、取消和事务回滚的真正延迟发布，但该路径
   尚未完成。
3. 快照不得只保存 cache/目录：还要覆盖 DRAM/credit、per-core 统计、事件
   feedback、组和共享预算状态、Sequencer 状态、perf FB-hit/deferred-install
   shadow 及其滚动窗口；或在首子域明确关闭未能回滚的功能。审计文件等不可
   撤销输出必须延后至最终提交。失败批次的模型结果和人口不得残留，宿主墙钟
   可单独累计为实际工作。
4. 使用与 OFF 完全相同的 batch 排序；group 不跨 producer chunk/Q，不跨无
   memory event 的 NoAccess/fence、特殊/unknown、其他核访问和同步边界。先用
   `prepare_buffer_tso_epoch` 判断 owner 可做 clean permission prepare；若后续
   证书失败，走上述一次无组合回退，不能让提前可见的普通写充当组事务。

预计增量：候选组扫描 O(E)，每核暂存最多四个组员、一个有界共享预算日历；
成功批次增加一次分组/证书及快照成本，失败批次最多增加一次完整 canonical
replay 与反馈，最坏接近该部分工作翻倍，且 snapshot/内存流量和墙钟尚无实测。
可先用 callback 的模型下界和已认证水位保守筛除必失败批次，减少无效试跑；
具体收益、失败率和吞吐必须由相同二进制、FST、配置的独立 ON/OFF 实测决定。
单次事务回退不会自动证明水位、资源或机制正确；未经上述接口与端到端测试
前不开放开关。

## 同线 SQ 信用占用代理（实验性切片）

新增默认关闭的 `core.spr_store_sq_join_credit_proxy`，要求同时启用现有 `core.spr_store_window_feedback`、单次 time-epoch 和稀疏反馈。对具备完整功能 `.sreq`、物理地址和同一 cache line 的普通单行 store，若模型窗口中已有相同 line、request flags、context 和 requestor 的未完成请求，且该 store 的模型发送边不早于退休，则将此条 store 的 SQ 容量释放近似为发送边。每条 store 依然各自回放 hierarchy，请求数量、原响应、TSO 发布及 drain 顺序不被该代理直接合并；窗口中的 line 信用维持到该行所有已处理请求最晚模型 callback。

属性缺失、特殊/跨行请求走既有保序路径并设置 barrier；同线 identity 混杂的窗口在释放前不能再被认证为可合并。该近似不是 gem5 store buffer 的真实共享预算、offload、请求合并或同线真实数据可见性，也不使用原生 OoO 决策。`spr_store_sq_joined` 和 `spr_store_sq_credit_saved_cycles` 分别记录使用代理的 UOP 和各 UOP 的占用差额之和；后者不是互斥周期，也不是 CPI 节省。窗口已有跨 chunk 复制和事务边界，代理不改变 cache 事务人口。其真实负载精度和吞吐仍需同源 ON/OFF 验证；初始实现和 Simulator 测试以最终构建结果为准。
# 2026-11-05：受限 L1 续流及逐行标记验证

本节为增量记录。预取仍只从本核私有 L2 已存在的数据行发出，使用功能流
训练，在模型响应完成后安装 L1；共享层未命中、完整硬件预取、FR-FCFS
以及原目标配置中的下层 I-fetch 均不在本实验支持域。两阶段和退休功能输入
未改变。仅对合格普通 load 的预取命中调用 stream 续流；特殊数据访问和
软件预取命中保守消费标记但不训练，store 最终发布清除目标行标记。

默认关闭的 `core.spr_l1_prefetch_stream_hits` 引入 set 级证书：每次已完成
的预取行在同一 cache set 发生改变后可能保守失效。进一步可选择默认关闭的
`core.spr_l1_prefetch_line_marks`：标记放入 cache line 现有 padding，随缓存
事务快照回滚；同组其他 way 的访问不再错误丢失该标记。失效、驱逐重装、
事务回退、同组其他行、普通/secure/software-prefetch 需求和退休人口已有
定向测试。CacheLine 在当前 gcc-11 构建为 16 字节，未改变原结构尺寸；这
不代表运行开销已经验收。`stream_consumed_not_used` 包含成功消费标记但
无法以合格 L1 demand 继续训练的事件；当前未按特殊类型细分，buffer TSO
发布通过 `mark_dirty` 清除的标记也不在该计数内。

同一新 FLAC C4 完整 FST（含预热）和 thread-state plan、v2 功能身份
`sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`：

| 对照 | FastSim task-active CPI | gem5 CPI | CPI 绝对误差（cycles/macro） | 相对误差 | 退休 L1 load miss 估计 |
|---|---:|---:|---:|---:|---:|
| 续流 OFF，L1 预取 ON | 0.318282 | 0.281872 | 0.036410 | 12.92% | 4315 |
| set 证书续流 ON | 0.318282 | 0.281872 | 0.036410 | 12.92% | 4314 |
| per-line 标记续流 ON | 0.318282 | 同上 | 0.036410* | 12.92%* | 4312 |

前两行是同一冻结二进制的续流 OFF/ON，并已通过维护入口 per-event
重评分和独立目标身份检查；L1 参考为 3898、unknown 为 0，估计误差分别
10.70%/10.67%，仍标记 `incomplete-evidence`，不是硬件 PMU 等价认证。
L2/L3 分别有 963/971 个 native unknown，故不评分。最后一行来自另一冻结
二进制的 set/per-line 配对；带星号的误差只用同源已核验 gem5 CPI 作算术
对照，**尚未独立重评分**。该配对逐核 macro、UOP、retired load/store 人口
相同，两个功能身份均为 complete 且一致。set/per-line 的模型发出预取分别
3786/3790、已使用标记 526/548；per-line 的 `consumed_not_used=2888`
是上述混合口径，不能解释为 2888 个确定的原生缺失命中。set 的 stale=2900
也只是同一行再次访问时发现证书过期的数量，不代表全部潜在命中。

set/per-line 并发测量模拟墙钟 65.33/56.28 秒、身份计算 49.46/49.52 秒，
外部进程全程墙钟 339.50/330.51 秒；并发、单次结果不作为吞吐验收。
结果和验证收据分别在 `tmp/spr-prefetch-line-mark-20261105/` 及
`tmp/spr-prefetch-stream-hit-20261105/`。一次总开关 OFF 消融被发现同时
改变共享 batch 排序后中断，不能用来估算纯预取效果；现正在改用保持相同
排序和需求 replay、仅抑制候选发起的受限对照。

随后完成该基础 L1 对照：两个进程使用同一个冻结二进制
`sha256:3a6c8b71ffbdaa229522a8c565c8586aba655e3574220ea6bcceaa199545d7bd`、
相同受限配置与 v2 输入身份，均关闭 stream/line，仅切换默认关闭的实验项
`core.spr_l1_prefetch_suppress_issue`。该选项保留需求排序、两阶段 replay
与 predictor 训练，将候选在访问 cache/层级前出队并计数，因此不会因候选
堵住有界队列而误导对照。模型的人口及同一排序基线保持一致。

| 候选发起 | task-active CPI | 同源 gem5 CPI | CPI 绝对误差（cycles/macro） | 相对误差 | 退休 L1 load miss 估计 |
|---|---:|---:|---:|---:|---:|
| 抑制 | 0.319644 | 0.281872 | 0.037772 | 13.40% | 7697 |
| 正常 | 0.318282 | 0.281872 | 0.036410 | 12.92% | 4315 |

抑制端候选出队 580416，层级发起/完成 0；正常端发起/完成 3785，另有
2359 次候选因目标不在本核 L2 被拒。双方训练 4897965 次，workload-task
退休 macro/UOP 为 31246569/34930034，退休 load/store 为 5985619/968958；
逐核对应人口均一致。模型 L1 估计与同源已验证 native 3898 相比的算术
相对误差为 97.46%/10.70%，均需保留上述 incomplete-evidence 限制；本次
新二进制对照尚未单独跑 per-event 重评分。两侧并发模拟墙钟 67.35/65.30 秒，
外部全程 377.22/378.78 秒，不能作为吞吐验收。配置、输出和计时见
`tmp/spr-prefetch-suppression-20261105/`。


## 已接受窗口的真实 store 组事务：后续接线切片

本节是后续 Simulator 接线方案，尚未实现，也不是现有 SQ 占用代理的真实合并。目标是在既有两阶段、每个已接受窗口一次有界组判断的前提下，对少量可认证的普通 store 减少 hierarchy 请求，同时让所有 store 竞争同一物理预算。允许报告清楚的接受顺序和有限等待近似；模型必须保持自身的准入、发布和人口一致，不能从 OoO gem5 借用分组结果或时间。

### 阶段与事实

1. **候选冻结。** 保持现有 OFF 路径的 batch 顺序，在线性扫描中组装每核最多 4 条的候选；仅合并同一已接受 Q、同一 producer chunk、同核连续 UOP、全核 batch 连续、同一物理 line，且 `.sreq` 证明普通单片、掩码完整、flags/context/requestor 一致的 store。其他核访存、unknown/special、atomic、fence、无 memory event 的 NoAccess、同步与任何非连续 UOP 均截断。不能将预计同周期退休当作实际准入或将早已发送的上 Q 项回头合并；已有 `SprStoreFactsChunk` 在 opt-in 下传递功能身份和 byte mask，`SprPendingStoreGroup` 可作为固定组内有界暂存，而非完整的共享预算或事务。
2. **整个组的 SQ 和共享预算。** 阶段二反馈按每条 store 的模型 dispatch、地址/数据就绪和退休处理；直到所有组员各自到达模型准入与退休/数据就绪边，且 SQ 头次序、offload 宽度及统一共享预算允许，才选择组的整体 offload / send。SQ `sq_ready` 继续限制真实逻辑 FIFO 槽位；单一、每核、跨 Q 的共享 SQ+buffer 信用摘要覆盖普通、unknown、special 和 split store，不能将同一成员在两个独立共享账本中重复扣容量。组 follower 的共享信用最早在组整体 offload 边释放，owner 信用保留至该组最终模型 callback 并受有序回收约束；实际 SQ 释放和共享信用含义、单位不同，需分别核对。特殊和未知 store 按自己的保守生命周期入共享账并形成 FIFO 边界，缺少可靠模型完成边时不得编造归还时间。
3. **一次 hierarchy 事务与人口。** owner 产生一个组 hierarchy 请求，合格 follower 不另发 hierarchy 请求；组内 byte mask 由功能事实合并，数据流依然来自已退休 FST，不产生推测路径。可以在最终组数据发布前发 clean permission prepare，但不得把 prepare 当作已提交数据或实际 store 完成。所有组员自己的退休计数保留；分别记录输入 store、已合并 follower、组 hierarchy owner、保守单发、prepare、Sequencer request、line 事务、callback 和在途结存。在统一以源 store UOP 计数的闭合范围内检查 `输入 = 合并 follower + 组 owner + 保守单发 UOP`；保守单发 UOP 可能包含多个 fragment，因此必须另行统计 hierarchy 事务，不能跨口径套用该式。
4. **一次提交或一次回退。** canonical replay 当前先于最终 response feedback，无法只靠阶段一预测证明整个组能准入。因此在候选窗口开始时将 shared/private cache、目录、DRAM/信用、Sequencer、当前核计数、feedback、组和共享预算、预取/pending 与 perf 影子状态纳入同一事务快照；先做一次候选 hierarchy replay 和一次反馈。只有检查所有组员实际模型准入/退休、组 offload、owner 权限与模型 callback、保序发布和状态完整性均满足才提交。任一检查失败恢复全部快照后，整个窗口按原单发路径回放及反馈一次；不重新选组、不多轮收敛。尚无法回滚的审计/输出必须延后，或排除此组合。无候选窗口直接走 OFF 路径，不增加候选事务快照；OFF 自身已有的 buffer TSO 事务仍按原规则执行。
5. **跨 Q 和接受次序。** 组的成员不跨 Q，已 offload 的组、预算、未完成模型 callback 和 FIFO 可见摘要随原 timing state 在 Q 间保留，不能在 Q 结尾提前归还。当前 `publish_buffered_tso_stores` 会在本批末尾改目录/cache，而后续 Q 的 fetch 或数据请求原始模型时间可能更早。可以设计明确的接受顺序单遍近似，但必须把同一模型顺序应用于所有需求、store 发布、共享 cache/目录和未来预取入口，记录夹高/延后，并确保 SQ 信用不早于采用的组 offload；不能只夹一个局部预取时钟或静默读取将来的状态。若本切片无法覆盖某入口或保证回滚，应将该 Q 直接回退。与 L2 agent 的单遍共享请求提案合流时，必须共用同一事务和可见顺序；已发预取取消只能取消未来填充，不能删除已发生流量。

### 真实 Simulator 验收

- **基本与人口：** 真实 `.sreq` 或等价功能 fixture 的 2/4 条相邻同线 store 合成一次 hierarchy owner；对照逐条 replay，保留全部退休 UOP、store、mask 与最终 cache/目录；明确比较 CPU Sequencer、L1/L2/LLC 和各级事务，不把每级事务数混为一谈。非合格组、零候选及开关 OFF 保持旧结果。
- **预算与次序：** SQ 满导致 follower 尚不能准入、组 callback 晚于 offload、跨 Q 未完成信用、同线及异线、第四/第五在途 line、乱序 callback 的有序回收；对可安全接受的固定组使共享预算状态和后续准入实际发生变化。special/unknown/split 占预算并形成 barrier。
- **冲突及回退：** 中间插入无 memory 的 fence/NoAccess、不同核对同 line 的 load/store、同组但阶段一和反馈退休错位、permission 丢失、L1/L2 驱逐、可见顺序与后续未知事件冲突、满预算及事务故障；候选被拒后只回放一次旧路径，cache/目录/DRAM/请求与退休人口、perf shadow、共享预算和跨 Q 状态应与 OFF 对应一致。不可证明未消费共享入口时回退并单独计数，不用 OoO 事件解决。
- **边界与效果：** 构造跨 chunk/Q 的已完成与未完成情况、同周期细分、所有特殊/unknown 入口、暖态延续、回滚重试、重复 ticket；统一构建与现有测试通过后，再在同一二进制/输入/配置下分离报告候选覆盖、接受/回退、hierarchy 人口、CPI 绝对及相对误差和模型/端到端吞吐。由于候选事务可能增加快照和一次回退，必须测成功与回退比例和真实墙钟；不得为某个负载引入专属常数。

候选成员必须都经过实际模型 SQ/退休与预算反馈，而现有单 UOP 即刻提交接口尚无组整体准入；共享事件的跨 Q 服务顺序和完整事务恢复也尚未闭合。以上是可编码的受限方向及验收条件，不是已经完成或已证实可用的主路径，遇到必须使用非有界循环、逐拍或不安全重排时停止并退回既有路径。

### 真实 Simulator 测试规格与接线状态

`tests/test_spr_store_group_runtime.cpp` 是尚未接入 CMake 的真实 Simulator fixture：定义两条/四条同线组的 hierarchy 请求及 L1 lookup 人口、每条退休和数据完成先于组 offload/send、owner/follower 信用边、missing/secure/context/fence 截断、其他核同线请求在接受次序中的介入、跨 producer chunk 不追溯合并、跨 Q 未完成信用以及满预算不吞请求。fixture 对接受 Q、batch rank、组身份、offload/callback/信用归还要求 audit-only 输出，因此该文件以拟议默认关闭键 `spr_store_group_merge` 和拟议 `ResponseFrontierAuditSample` 字段为接口；当前代码缺少这些接口，按预期编译失败，未运行，也不能作为已经通过的测试。FST 不携带 store 字节值，fixture 不能声称验证最终写入字节值。

对用户允许的单次接受顺序近似，最小候选可收窄为单核、FCFS、每 Q 第一个无干扰的两条合格 store，组成立前确认 SQ 和共享预算可接纳两条。owner 可先发模型 clean permission prepare；延迟到相邻 follower 真正经历模型 dispatch、retire 及数据完成后再选择组 offload/send/callback，并把 follower 信用留到 offload、owner 留到 callback。无需重现 gem5 动态顺序，也不要求多轮收敛。

当前实现尚未开始接入：这一切片仍需要同时贯通 Q 候选与功能 facts、canonical owner/follower hierarchy 人口、所有 store 的 dispatch 前共享预算、逐 UOP 反馈中暂存 owner 直至 follower 到达、以及候选失败后的完整事务与跨 Q 提交。现有 `sq_ready` FIFO 无法单独表达组内较年轻 follower 的先释放信用；现有 feedback 的多个 Sequencer 人口入口也不能把 follower 简单标为 Sequencer alias，否则会混淆 store-buffer 合并和 Sequencer 合并。局部插入影子账或只少发一次请求都不能保证人口/信用守恒。因此按本轮范围停止在测试和具体接线记录，未打开或宣称真实 hierarchy 合并；后续需完整实现上述生产链并执行构建与 Simulator 回归。

### 本轮接线与验证状态更新

上述段落记录的是接线前状态。本轮已增加默认关闭的 `core.spr_store_group_merge` 初始实现：限单核、单次 FCFS 两阶段反馈，整个接受批次恰为同一 producer chunk 的两条相邻普通同线 store，功能侧完整 `.sreq` 用于确认资格。该限定允许早期 clean permission prepare；组内两条均经模型 dispatch 和退休、完成后，再加显式 commit-to-IEW 及非零服务延迟选择组 offload/callback。原有 SQ FIFO 与独立共享预算均保留，所有 store 包括 unknown/special 都要占预算；只有 follower 的层级请求被抑制，退休人口保留。此切片不是完整的 SPR store buffer，也不建模字节值。

Q 内首次试算后，以当前两阶段提供的 lookahead 水位及第一条未消费 memory 的功能 issue 下界检查 callback；不能证明 callback 先于未来事件则恢复 cache、目录、DRAM、计数与 FB-hit shadow，并按原有无组路径最多重放一次。权限失败时沿已有冲突路径回退；仅水位不足时保留无组路径原有权限判定。当前不支持多核、sync/recorded scheduler、固定工作、context、预热边界 cache seed、I-fetch 下层、hierarchy page walk，以及其他与本切片冲突的修复或预取模式；配置会显式拒绝，故尚不适用于本批 C4 完整 workload。

`tests/test_spr_store_group_runtime.cpp` 现已接入主测试并可通过独立目标 `fastsim_spr_store_group_runtime_tests` 运行。首轮主测试在跨 Q 回退 fixture 未构造出实际越界处失败；诊断显示 candidate=1、committed=1，属夹具自身没有满足测试条件。后续换成具备目标寄存器的功能依赖链后独立测试通过，覆盖同线一组、人口和组边、特殊/unknown/容量不足回退、不同 chunk 和实际跨 Q 回退及其 OFF 对照。尚无完整统一回归、CPI/PMU 误差或吞吐结果，不据微测声称可用于 C4 或已达到 gem5 等价。


### 单核功能预热边界补充

组窗口不得跨 warmup/measurement 边界。预热尾块的 reached-end 可能仅表示 measurement boundary，因此对组 callback 继续使用当前预热 Q 覆盖水位；超出时按未合并路径单次回退。跨边界保留 FIFO SQ、每核共享预算及其未来释放事件，只重置测量人口；输出独立的预热候选、提交、回退和边界时预算快照，供核实状态延续。真实 `WarmupInstructionTraceSource` 的独立测试 `fastsim_spr_store_group_warmup_tests` 已通过：覆盖边界前提交、边界后回退、以及 unknown store 占用信用跨边界限制 ROI dispatch。此结果仅覆盖单核，不代表 C4 或真实工作负载精度/吞吐通过；完整回归待完成。


### C4 FCFS 垂直切片与验证状态

默认关闭的 store 组现扩展到最多四核：全机每个已接受 Q 最多一个相邻同核 pair，以 `(core, sequence, event)` 确定成员；所有同 line 的其他访问触发拒组。每核拥有独立共享预算和 FIFO SQ，跨核 callback 以全核未消费事件的最早参考时钟作保守下界；预热已到边界而 ROI 尚不可见的核使该 Q 拒组或回退。已知同步 frontier 活跃时不组，继续运行原 thread-state plan，不更改 owner、wakeup 和 task-clock 语义。MMIO escape store 不产生伪 cache 事务，在其核按 special store 模型占用预算到不早于退休加 commit-to-IEW 的保守本地完成边，并维持 TSO 顺序；这不是设备时序。组事务回退对受影响核和行恢复 FB shadow，保留原 hierarchy 请求及人口。

独立 `fastsim_spr_store_group_c4_tests` 第三轮通过：真实四核不同 line 同批合组，同 line 跨核拒组，下一 Q 对端早事件导致单次回退及 OFF 等价，MMIO 和 recorded scheduler 边界守恒，逐核预算独立，以及真实四核功能预热后的单核未释放信用与 ROI dispatch 隔离。正式完整回归、真实 FLAC 同配置效果、CPI/PMU 和吞吐尚未验证。该原型当前强制 FCFS；正式 FLAC C4 baseline 使用 FR-FCFS，不能直接复用其 CPI 或称原配置已覆盖。FR-FCFS 必须另有符合单次前向两阶段约束的实现及同源 OFF/ON 测试。


### FLAC C4 FCFS 同源组开关初验

同一冻结二进制 SHA-256 `8c2702085e6f5951d29663dd3376091aa22cb77a8888a65f1242e8c957790136` 与完整 FLAC C4 FST，使用原 thread-state.plan、功能预热及 MMIO；两份输出配置唯一差别为 `spr_store_group_merge`。为满足当前组路径，两侧共同采用 FCFS，关闭 private preview、I-fetch 下层及 materialized UOP fast kernel，故不是正式 FR-FCFS baseline。完整功能身份均为 `sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`，逐核均有 `.sreq`，但 `roi_edges=false`、`macro_boundary_complete=false` 仍为输入边界限制。两侧测量期均为 31,246,569 macro、34,930,034 UOP、7,437,768 个功能 memory 事件；每核 macro/UOP 依次为 4,678,653/5,695,137、9,067,614/10,003,324、8,464,977/9,363,835、9,035,325/9,867,738；每核各有 1 个 MMIO escape，61 个 scheduler event 均释放。

| store 组开关 | task-active CPI | 同源 gem5 CPI | CPI 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| OFF | 0.319644 | 0.281872 | 0.037772 | +13.400% |
| ON | 0.318184 | 0.281872 | 0.036312 | +12.882% |

ON 测量期 candidate/committed/permission fallback/future fallback 均为 0，sync skipped 5，预热 candidate 也为 0；因此这里没有展示真实合组效果，不能将 CPI 差异归因于请求合并。开启还同时接入全 store 预算和 MMIO special 生命周期，SQ full events 从 315,384 变为 315,964，SQ stall 累计从 109,498,830 变为 102,844,933 cycles；这些重叠的资源等待不是可直接相加的 CPI 贡献，差异根因尚待定量定位。L1 tag miss 从 30,952 变为 31,050，private L2 tag miss 从 4,473 变为 4,507，属模型人口差异，不是本次新输出独立重评分的 PMU 误差。

两侧并发、绑不同 CPU：进程总墙钟 OFF/ON 198.66/197.60 s，模型测量 56.999/56.489 s，功能 identity 57.046/57.070 s；受竞争干扰不作为吞吐验收。gem5 参考来自此前同源已验证记录，新输出本次未独立运行 native validator，不能继承其 `passed`。原始输出、配置、日志和 binary hash 位于 `tmp/spr-store-group-c4-fcfs-20261105/`。


### FR-FCFS 与 store 组的单遍接线

现已实现默认关闭的受限 FR-FCFS + store 组路径：每个组候选显式从 FR 请求列表剔除 follower，保留其他核所有实际请求，单次预测 LLC MSHR/DRAM 服务后以修正 response 得到最终组 feedback，再检查全核 watermark。权限、信用、路径/可见性或水位条件不满足时，最多恢复一次候选事务并按完整原请求人口回退，不以多轮请求响应求不动点。若已接受的 FR 预测因组水位被丢弃，其目标状态和提交统计回滚，另记 discarded pass/accepted 以保留宿主工作量。动态变频仍显式拒绝。

`fastsim_spr_store_group_frfcfs_tests` 专项通过：实际合组、最终 FR callback 越界后的原人口回退及后续同线访问、已接受预测与被丢弃 pass 的统计、动态变频拒绝、四核 LLC MSHR 饱和和不可重放 atomic 处理；逐核退休人口守恒。该微测不代表完整 FR-FCFS 原生时序等价，不代表真实工作负载精度或吞吐通过。此前 FCFS 实验结论和数字不因此改变；统一回归及新二进制真实 workload 尚待完成。

FR+group 的 scheduler/MMIO 交叉专项补充通过：真实 6-event recorded plan 所有边界保留，最后边界后实际发生 FR 单遍同线组合并抑制一条 follower hierarchy 请求；FR 模式 MMIO special 信用、TSO 释放和请求人口测试通过。该微测仍不代替进行中的完整 FLAC C4 OFF/ON 对照。


### FLAC C4 单遍 FR-FCFS 同源全流对照与组候选漏斗

同一冻结 binary SHA-256 `d86a5e431a25a075c300a97730356a8cddf595b5eec972a0bee8f90c69e0bade`，完整 FLAC FST、原 thread-state plan、功能预热、MMIO 及 FR-FCFS，OFF/ON 输出配置唯一差别为 `spr_store_group_merge`；两侧共同关闭 private preview、I-fetch 下层和 materialized UOP fast kernel。功能身份均为 `sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`（`roi_edges=false`、`macro_boundary_complete=false`），逐核 macro/UOP/功能访存及每核 1 次 MMIO 完全一致，61 个 scheduler event 均释放；本次新输出没有独立 native 重评分。

| store 组开关 | task-active CPI | 同源 gem5 CPI | CPI 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| OFF | 0.320022 | 0.281872 | 0.038150 | +13.535% |
| ON | 0.318411 | 0.281872 | 0.036539 | +12.963% |

测量期 ON 漏斗：所检查的同核相邻 batch 对 3,978,421，其中双方为 store 97,249、同 line 46,414、第一项是该核窗口首 UOP 6；这 6 对均不满足相邻 UOP，因此 chunk、功能 facts、预算阶段以及实际 candidate/committed 均为 0；sync skipped 5。扫描在每个 Q 找到首个符合首 UOP 的 pair 后终止，前面几级是扫描人口而不是全体候选对的完整离线枚举。这个结果明确指出当前窗口首 UOP 与相邻 UOP 约束在此输入下使合组零覆盖，不应把 CPI 差异归为实际合组。每核预算等待次数/周期和水位夹高均为 0；ON 仍会为 escaped MMIO store 应用 special SQ/TSO barrier，是另一个可能改变时序的路径，是否解释 CPI 差异尚无直接证据。

OFF/ON Sequencer 请求均为 7,437,768、L1D access 均为 6,753,316；L2 access 39,892/39,896，LLC access 9,172/9,175。单遍 FR 预测 OFF/ON 分别 346/364 个 candidate 和 346/364 passes，回退均 0；它们是单遍预测，不是收敛轮数。并发总墙钟 196.96/197.19 s，模型测量 56.248/56.090 s；与专项测试编译重叠，不能作为吞吐验收。原始输出、配置、binary hash 位于 `tmp/spr-store-group-c4-frfcfs-20261105/`。


### 放宽普通 UOP 间隔后的 FLAC C4 FR-FCFS 全流对照

两侧使用相同冻结二进制 SHA-256 `9e20eba2c8194e58e1bd3b4d3c24c007daab736e072e21483e6a8105b0904f02`，完整 FST、原功能预热、thread-state plan、MMIO、单遍 FR-FCFS 与完整 v2 身份，输出配置仅组开关不同；共同关闭 private preview、I-fetch 下层与 materialized UOP fast kernel。输入身份均为 `sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`；各核 macro/UOP/功能访存、每核 1 次 MMIO 和 61 条 scheduler event 均匹配；identity 的 `roi_edges=false` 与 `macro_boundary_complete=false` 仍需披露，两个新输出未独立 native 重评分。

| store 组开关 | task-active CPI | 同源 gem5 CPI | CPI 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| OFF | 0.320022 | 0.281872 | 0.038150 | +13.535% |
| ON | 0.320486 | 0.281872 | 0.038615 | +13.699% |

测量期 ON 有 candidate 33、成功组 31、future fallback 2、permission fallback 0；warmup 分别为 4、3、1。Sequencer 请求从 7,437,768 减至 7,437,737，L1D access 从 6,753,316 减至 6,753,285，减少量均为 31，与成功组数一致。ON 没有记录共享预算等待、等待周期或水位夹高。CPI 相比同 binary OFF 增加约 0.000465 cycles/macro；由于两侧还有非合组预算及 MMIO 路径差别，不能仅凭此次对照将差异归因于 31 个合组。

在线 `bounded-gap-prefix-v2` 漏斗：同核相邻 batch 对 3,958,475，双 store 97,541，同 line 47,095，无访存/屏障 UOP 间隔 41,664，同 producer chunk 41,642，普通 store 18,961，功能 facts 可用 18,894，同 line 无其他访问且其他核无特殊边界 58，前序 store/SQ/共享预算最坏情况预检通过 33，最终提交 31。该扫描每 Q 最多选一组；无干扰和跨核特殊边界是联合条件，暂不能从现有计数判断哪一项贡献主要损失。

FR OFF/ON 的 candidate 与 single-pass 分别为 346/349，FR fallback 均为 0；ON 另有 1 个已接受的 FR forecast 因组水位回退而被丢弃，单独记账。并发进程总墙钟 207.80/196.93 s，模型测量 59.577/55.748 s；由于两侧并发和主机扰动，不构成吞吐验收。结果、配置与原始日志在 `tmp/spr-store-group-c4-neighbor-frfcfs-20261105/`。

当前 legacy buffered TSO 路径把 clean prepare 与最终直接 dirty publication 组合为一次 hierarchy 访问；它并未完整表示 gem5 中独立的 prepare hint 与后续 head demand。上面的 31 条抑制和相应计数只证明当前模型自身的请求人口关系，不代表 native prepare/head 人口等价；该机制由后续独立 prepare/head 工作处理。


### 真实候选拒绝原因的 ON 重放

计数细分版本冻结 SHA-256 `0395cc7c16ae50aba38b5e5ab8541170f5a1c9bdd33ce4abf59fb982371db28b`，使用与上一轮 ON 完全相同的 FR-FCFS 配置、完整 FLAC FST、预热、thread-state plan 和 MMIO。完整 v2 功能身份仍为 `sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`。递归比较两次输出的所有共同目标模型字段得到 0 个差异，包含 CPI、逐核人口、层级/FR 请求、31 个成功组及 2 次水位回退；宿主 `frontier_waits` 为 2571/2683，来源于等待 producer queue，连同宿主墙钟和吞吐排除在目标状态等价检查之外。

18,894 个通过功能 facts 的在线候选中：18,829 因同 Q 存在其他同线事件被拒，7 因其他核特殊边界被拒，58 通过这两项；数字按代码检查顺序守恒。后者 7 次中 serialize、NoAccess、hardware-entry 分别出现 6、2、2 次，原因可重叠不能求和；atomic、MMIO、precise-fault、memory barrier、locked 均为 0。由此可以确定主要限制是目前的同 Q 同线其他事件处理，而不是跨核特殊边界；不能据此推断这些其他同线事件都可合并，须再按读写、所属核、顺序和数据可见性分类。实验使用三项共同关闭的受限配置，未独立重评分 native 参考；单次进程墙钟 198.16 s，不是吞吐验收。原始输出和完整共同模型字段比较见 `tmp/spr-store-group-c4-rejection-frfcfs-20261105/`。


### 同线其他事件的功能形状分类

新的仅观测版本 SHA-256 `71c8038510ba40ec52e419164521765049abf012b466a9c7da655a5a8f8b6552` 在同一原 FLAC FR-FCFS ON 输入与配置上完成单次全流。完整 v2 identity 不变，与上一轮 ON 的共同目标模型字段递归比较差异为 0；宿主 wall/throughput 和 producer queue 等待次数单独排除。未重做 native 评分或吞吐验收。

在先前 18,829 个“同 Q 另有同线事件”的 pair 观察中，本核额外已认证普通 store 出现 18,659 次、本核 load 10,032 次、本核特殊/未知 499 次，类别可重叠；这些观察无额外跨核事件。跨核同线冲突在更早的 permission prefilter 检查，本次相应观察为 0。在同线事件全部为本核已认证普通 store 的 8,694 次观察中，全局 batch 连续、同 producer chunk、UOP 间隔只含安全计算，且总数恰为 3 或 4 的 pair 观察分别为 88 和 99。该统计按 pair 在线扫描，同一个运行段可重复观察；尚未验证所有 request flags/context 相等、共享预算和最终可见性，不是能直接合并的 187 个组。原始输出和共同状态比较在 `tmp/spr-store-group-c4-line-shape-frfcfs-20261105/`。
