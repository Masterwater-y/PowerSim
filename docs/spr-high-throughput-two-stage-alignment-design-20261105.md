# 高吞吐两阶段框架下的 gem5-spr 对齐设计

日期：2026-11-05。状态：开始实施，整体**尚未完成或验证**。基线为 FastSim
`fastsim-spr/526d6f5`，独立 `gem5-spr/yinhaolang` 本地源码，以及四个已采
SPECspeed C4 案例的实际配置。本文不启用已撤销的独立因果执行器。

实施次序调整：先独立验证 IQ 分区作为低耦合切片，再实施 store/信用
账。实验性 IQ 分区已进入第一阶段及第二阶段响应反馈，开关默认关闭；
现有统一 IQ 的块转移与空闲段证书在开关打开时暂不使用。向量访存
OpClass 分类及 IQ 峰值计数修正已重新构建，构造测试和完整测试集通过。
一次历史 FLAC 同源对照中，开关前后退休人口
相同，模型 `perf_like_cpi` 为 0.500189 / 0.501152，总墙钟 36.41 /
35.52 秒；单次运行不足以证明吞吐差异，也不是对 gem5 的精度结论。
随后最终版本重跑历史 FLAC，人口与模型值仍相同，IQ 峰值计数得到修正。
最终版本回放最新 10M 全伴随输入：预热 3,451,088 条 UOP，测量
34,906,865 条 UOP 和 31,226,679 条宏指令，模型 `perf_like_cpi`
0.500305；总墙钟 279.27 秒，模型预热和测量分别为 6.75 秒、47.13 秒。
与先前完整伴随回放人口相同，先前模型值为 0.500303；这不是同一二进制的
配对测量，不可据此判断吞吐或精度改善。由于欠缺与逐核预热结束起点匹配
的 gem5 计时参考，该 CPI 不提供严格 gem5 误差。最终结果见
`tmp/spr-iq-dev/latest-final.json`。整体机制仍未与 gem5 对齐。

## 1. 目标和边界

- 保留现有并行 `produce_thread_chunk`、窗口/分块、共享事件提交以及第二阶段
  `compute_core_timing_feedback_impl` 反馈，不新建逐周期 CPU、独立执行主循环
  或全窗口反复迭代至收敛的求解器。
- 高频数据按核/批次处理；新增结构按目标资源规模有界、跨 chunk 保留，更新
  次数与请求和必要释放数成比例。固定一次估计与一次反馈，不把 fast/slow path
  偷换为另一套精确流水线。保留现有模型作为开关关闭时的基线。
- 输入是退休流和功能事实；所需字段必须能由 Atomic 级或 QEMU 功能执行获取或
  从功能事实和固定目标配置派生。gem5 OoO 独立提供参考，不输入原生时钟、
  命中、动态 FU grant、实际融合、排队或预取结果。不补造错路径。
- 对齐参数、行为近似、实验精度和吞吐分别陈述。源/窗口不一致或 native PMU
  缺失时不能声称严格准确率；允许独立标注的诊断结果。

## 2. 实际起点

以四案 C4 [实验记录](spr-speed-c4-campaign-20261104.md) 为例：模型参数已导入
ROB512、LQ240、SQ112、L1I 32KiB、L1D 48KiB、私有 L2 2MiB 和 LLC96MiB 等；
既有退休流 SPR 前端和共享 FU 开关并不证明微架构行为等价。真实 IQ 为
128/72/48，模型总容量248；真实 store buffer 具备合并、独立 prepare 和四条
不同在飞 line，当前使用 `legacy` buffer 近似。L1 sequencer 普通/prepare
额度为16/16；Dcache fill16、私有 L2 fill48 与控制器 TBE256 是不同资源。
当前 L1/L2 MSHR256 是容量代理，不能把它直接替换为16并称已对齐。
真实 L1/L2 硬件预取启用，当前无硬件预取模型。ITLB256/8路、DTLB96/6路，
每核 I/D 连接该核 STLB2048，两者访问的上下级延迟也要按实际参数映射。

截至该历史批次文档，四案 FLAC、Marian、Graph500、XZ 的等权 CPI MAE 为
0.466511 cycles/macro、MAPE 为52.1480%；它们仅是当时同批结果，不是本方案
预测、四案重跑或当前代码新测量。来源和历史结果状态见原文。各案参数、
活跃功能和计时窗口必须重新核验，不能根据这一表直接断言误差根因。

一个关键约束：已有 `bounded_analytic` 是单次前向近似，但其现有快速子域要求
`TBE >= max(SQ, buffer)+2*LQ+1`、CPU-store-port >= `max(SQ,buffer)+2`。
在本批 SQ112/LQ240 下分别为593和114，而目标相关配置为 TBE256、store ports2。
因此不能直接开启，更不能通过虚增目标资源绕过 [现有检查](../src/config.cpp:685)。
之前实验型 store 求解器有64轮不动点路径，不能用于本方案。

## 3. 推荐架构：保留原两阶段，按窗口解析压力

**阶段一：每核并行生成批次。** 继续执行现有 trace、依赖、前端及 IntervalCore
准备，按现有共享资源预约模型得到初步时间；仅补充必要的、紧凑的类型/组别/
line/上下文描述，使用 `.ffe/.uops/.sreq/.xlat` 等可选功能事实。已有模型的
预测时间可用于该近似，不能被称为 gem5 的真实 issue 或 response。

**阶段二：维持原来的窗口仲裁和一次反馈。** 按既有确定性事件顺序提交 cache、
目录和 DRAM 近似；为需要的不同资源维护小容量发布/释放账，计算一次准入延迟
与合并关系，写回原 `CoreChunk`/response feedback 接口。跨窗口保留仍未到期
的最少 line/credit/queue 释放摘要。提交后的竞争顺序不因反馈重新分类或回滚。
不逐周期推进、不把每条 UOP 投入全局 heap；队列容量受窗口和目标硬件资源约束。

**明确近似：** 后续反馈可能改变 CPU 实际发起顺序，却不反向重排已分类的共享
请求。因此同线合并、预取污染、store 与 load 干扰均为固定窗口顺序下的估计；
这种一次前向方案不能声称与 gem5 的动态事件顺序严格一致。窗口边界效应通过
跨 Q 生命周期和不同 Q 实验量化，而不是增加重算次数掩盖。

## 4. 分项设计

### 4.1 第一优先：store、合并和资源压力

1. **保持原 buffer 两阶段模型与既有接口。** 加一条 opt-in 窗口解析路径，
   用已有 `CoreBoundUop`、store 请求事实及 retirement/response 反馈生成
   估计的 store 生命周期。不启用 `store_request_engine` 64-pass 求解器；
   不将已有 `bounded_analytic` 的非瓶颈条件当成目标实际参数。
2. **store 状态做有限摘要：** 每核按顺序维护最多 SQ/共享预算的条目，已提交
   项按可合并的 line、上下文、flags 和 mask 维护合并摘要；对已知地址和数据
   依赖，从第一阶段模型预测推导其就绪界。入 buffer、same-line 组合、
   offload、独立 prepare 和 write completion 用解析资源下界估计；不能把
   guest 未提供的 data value 当作已知。普通与特殊、NO_ACCESS、split、atomic
   维持各自语义与原有覆盖，未支持类型走明确声明的旧路径或独立标记，不删记录。
   prepare 可按模型估计在该指令退休前发生，但候选 store 必须来自已输入的
   退休流；不构造或扫描任何错路径指令。
3. **有界资源不能互换：** 独立记录普通 Sequencer 16、prepare16、L1D fill16、
   L2 fill48、controller TBE256、store 共享预算112、最多4条不同在飞 store
   line，以及2个 store send port。具体是否使用分别的时间释放队列/稀疏日历，
   以单遍批量实现和实测开销决定；`max()` 得出准入最早边界，每次成功准入和
   响应只各更新一次。prepare 满时按目标来源规则处理可选 hint，不能占用或
   阻塞普通请求的独立信用。参数读取每案 actual `config.ini`，禁止 workload
   专项常数或凭 CPI 调参。
   源码复核：gem5 的普通 store 先从 SQ offload 至 committed store buffer，
   此时 `completeStore` 释放原 SQ 项，随后 buffer 才择机向 Sequencer 发送；
   SQ 与 committed buffer 仍共享容量预算。相邻 store 仅在队尾未发送且
   line、context、requestor、flags 相同时合并；一次 offload 最多处理 4 项。
   一个已完成 buffer 项只有在它及其更老的队头项均完成时才能从队头回收，
   因此不能把每次返回都当作立即释放共享容量。
   因此不能把 offload 等同于 Sequencer 接收，也不能任意合并所有同线项。
   见 [gem5-spr 实现](../gem5-spr/src/cpu/o3/lsq_unit.cc:980)。
4. **同线合并和 store 合流：** 在现有 canonical 请求顺序中以有界 line 表估计
   未完成同线需求，保留请求信用、父事务、follower 和退出身份；合并不等于
   消失或免费。读取器选择新估算时应避免现有互斥组合；需在同一个可核对的
   阶段二资源账中同时处理 store/普通请求，不得简单解除互斥并运行两套模型。
   面积、时间及批次内跨核先后均属于估计，不能伪造 native 分类。

实施先观察 `legacy` 的 SQ 满、store 等待、line 重合和资源占用；用构造测试
验证不同 line/同 line、容量满、跨 chunk、fence、NO_ACCESS 和资源归还。先做
store 与独立信用，再把两者合流，避免一次堆叠多个机制无法解释变化。

### 4.2 核心资源：分区 IQ、端口和 load bank

- 在现有 IntervalCore 的批次日历中按 target OpClass 分类并分别统计三组 IQ
  128/72/48 的容量和释放估计；FU 仍用现有共享端口日历，增量支持简单轮询
  选择。宏/UOP/分配槽是不同单位；静态融合资格缺失时沿用明确标记的近似，
  不借用 gem5 是否实际融合的结果。
- 实验开关为 `core.spr_partitioned_iq = true`，分组容量分别由
  `core.spr_execution_iq_entries`、`core.spr_memory_iq_entries` 和
  `core.spr_store_data_iq_entries` 指定。需按目标配置设置；本次为
  128/72/48。第一阶段记录按类别分组的有界释放摘要，第二阶段把延迟的
  实际反馈更新到相应分组，摘要跨 chunk 保留。关闭开关继续使用原统一
  IQ 路径；开启时暂不使用统一 IQ 的块转移和空闲段证书，因此还需跨负载
  检验吞吐。当前模型仍是按退休 UOP 和既有预测时间计算的近似，不包含
  原生错路径或逐周期仲裁，不应称为完整复现 gem5 发射队列。
- 按物理地址和目标 bank 规则为同周期候选做小数组计数，与现有 load/store
  端口宽度一起估算局部冲突；使用第一阶段预测顺序，保留它与目标发射顺序不
  等价的限制。不要给每个动态 UOP 建全局逐拍事件。
- 第一阶段预约导致的跨阶段顺序偏差保留为模型近似，重点通过依赖链、bank
  热点和混合 OpClass 微负载核对趋势，不预设某个应用的 CPI 收益。

### 4.3 前端和翻译

- 复用已有退休路径 DSB/MITE/MS 和宽度模型，不输入 native 取指路径、分支
  结果以外的预测历史或错误路径。读取代码字节、宏边界与静态融合资格，
  用有界 lookahead 估算跨宏融合和供给；rename 反压用窗口内摘要反馈，不能
  将其称为完整 gem5 逐周期反馈。
- DTLB 在现有功能翻译与 TLB 近似基础上增量建模真实配置的集合/相联；设计
  按核共享的 I/D 下级 STLB 紧凑状态与固定服务日历，页大小、ASID 和实际目标
  PCID 行为需要先由源代码及小实验核对。失效只应用可功能绑定事件；未绑定
  的 MMU 事件保留 unknown，不按相邻 PC 猜归属。walker 可先采用现有快速
  近似，按需试验独立 PTE 访问，报告路径缺失和额外成本，不把周期精确 walker
  当目标。
- 此批新 FLAC 全流有949条 data translation leaf-only 和25个 instruction
  cross-context span；高层 functional-sync 未采集。这些是实际信息边界，不
  等于该数量的 TLB miss，预热与测量的范围需分开。可先做标注明确的现有输入
  诊断，严格完整模型覆盖与 Atomic/QEMU 等价需另行验证。

### 4.4 预取和内存

- L1/L2 启用的是不同的 Ruby 预取器配置；基于功能访存 PC、地址、读写、模型
  可见顺序和源配置，在阶段二维护小表/队列训练，产生模型自主预取；每条预算、
  排队、去重及需求冲突均计入资源/流量。先 L1 后 L2，提供关闭开关和单独人口，
  未实现部分不能算零成本或原生等价。当前 L1 的 `ip_stride=true`，L2 不是
  简单复制 L1 配置。训练不输入原生预取结果、cache 命中或未来地址。
- DRAM 保持现有 FR-FCFS/通道和刷新配置来源；不把某次开启刷新得到的结果当作
  必然改进，也不把未知刷新初相当成实测。目标参数与当前模型边可明确近似，
  不通过更改参数去抵消 CPI 误差。

## 5. 验证和交付

1. 冻结当前可运行两阶段基线、同一份输入/二进制/配置及模型输出；每项改动
   默认关闭，对比 OFF 必须保持旧行为。已有 FLAC 原始采集保留；无需为每个
   模型改动重采，只有功能事实缺失/不可信时才判断需否重采。
2. 先使用小型、代表性微负载做机制对照：store 多 line/合并、credit 饱和、
   依赖链、IQ分区、bank 热点、TLB失效及预取冲突；计算模型自己产生的人口和
   资源守恒。组件测试只能说明该子机制，不代表端到端准确。
3. 每次只打开一项或已解释的依赖组合，使用相同输入及配置跑全部四案，保留
   全部结果和异常。报告逐案 gem5 CPI、FastSim CPI、绝对误差（cycles/macro）、
   相对误差；聚合报告 CPI MAE、MAPE（必要时另列 WAPE），并明确对应样本。
   非同起止策略、人口或 task 集合的参考不得算严格误差；native cache 真值
   只从维护的 Ruby/SLICC 摘要和验证入口读取。注明已知同步及 source 缺口。
4. 同输入、同 CPU/NUMA/构建方式，串行交错多次对比，分别报告端到端（包含
   FST/伴随读取、初始化和预热）、预热、测量吞吐与峰值内存，保留每个样本。
   **候选预算**：每个切片四案端到端吞吐中位数相对 OFF 不低于95%，每案单独
   报告且有明显退化时调查；整合后每案是否接受由实际收益和用户目标决定。
   95% 是建议，不是已验证的项目门槛，也不能用来掩盖单案尾部或降低指令人口。
5. 机制满足微测但 CPI 或速度恶化时，可保持默认 OFF，继续分析或放弃；不要
   根据目标答案调每案常数。无需为了解决未知而重复全流审计或使用 OoO 时序。

## 6. 实施顺序和不确定项

按“基线冻结 → store/credit 窗口估计 → 合并联动 → IQ/端口/bank → 前端/TLB →
自主预取 → 联合评价”的顺序推进。每个阶段先形成可用、可回退的增量，不因后续
信息不全阻塞所有独立工作；目标参数导入与语义证据独立记录。

当前没有能端到端产出全部伴随的独立 Atomic/QEMU producer；OoO 采集的功能字段
与独立功能采集等价需要验证。更重要的是，固定窗口顺序和延迟反馈是明确精度
取舍：是否使四案误差与吞吐同时改善，只有同源消融实测才能回答。

## 7. 旧求解器历史与 FR-FCFS 单遍改造

只读 git 核对显示，`interval_causal_timing`、`interval_response_retime`、
`interval_corrected_suffix_carry`、`dram.frfcfs_passes` 已存在于旧
FastSim 的 `dc9cd96`；`store_post_commit_request` 至少存在于 `408a0f1`，
`store_request_engine` 至少存在于 `1112a81`，均早于 SPR 孤儿快照
`a0d9345`。它们是原两阶段体系中的可选旧实验，不应与后来新增的独立
逐周期因果运行器混为一谈。`526d6f5` 按用户要求回退了后者；核对该提交
删除清单，当前工作树中七个独立 runtime 头文件、七个实现文件及对应删除的
因果 runtime 测试文件均不存在。历史文档保留不表示允许恢复该运行器。

正式新 FLAC C4 的 `off.json` 中 causal timing、response retime、ROB-head
suffix、corrected-suffix、post-commit、store-request engine 均关闭，
`interval_reweave_passes=1`、`dtlb.hierarchy_walk=false`；但 legacy
FR-FCFS 的 `dram.frfcfs_passes=4` 实际在每个候选 Q 上重复求排队结果。
四案冻结输出分别为 FLAC 358 Q/716 次、Graph500 5324/10648、Marian
6335/12670、XZ 3364/6728；局部 wall 约 0.891/6.107/9.983/6.594 秒。
这是已存在的旧两阶段共享时序修复，不是已删除的独立 CA runtime，
但其反复求稳定违反当前用户对高吞吐及不做整窗收敛的要求。

源码中仍有关闭的旧实验循环：causal timing、response retime/ROB-head
suffix、corrected suffix、post-commit、64-pass store engine，以及启用
层次页表 walk 时的有限 replay。特别是 response-retime 头文件注释称其
非 fixed-point，但实现当前最多三轮并检查稳定，不能按注释声称单遍。
本次只改 FR-FCFS，不擅自删除其他旧选项；当前正式 FLAC 未启用这些项，
项目整体仍不能因此宣称所有可选求解器均无迭代。

实施：借用现有 bounded store 模式中的单次 FR-FCFS forecast，与 store
模式解耦。用现有 accepted batch、CHA 公平排序和每行原模型 tag-ready
生成确定性请求，调用一次保留实际读队列容量的 `schedule_frfcfs`；从
Q 入口真实 LLC slice MSHR 日历逐个检查预计到达时是否有空槽，并用此次
模型返回时刻生成退出日历。槽位或旧 fill generation/可见性证书失败，
该 Q 一次性采用原 canonical 服务和一次反馈并分别计数；不扩大 MSHR、
不漏请求或虚构容量。成功时复用现有 follower fill-generation 检查、
响应反馈及共享 DRAM/MSHR 安装，保留 cache/目录分类；调度按阶段一
预测边进行，反馈后的 issue 变化不回推另一轮重排，这是明确的时序近似。

兼容配置键 `dram.frfcfs_passes` 仍接受原值，但不再决定 FR-FCFS 调度
次数；旧 `ruby_buffer_service_repair` 实验的宿主工作预算估计仍引用该值，
不在本改动中改写。JSON 新增 `frfcfs_passes_policy` 和单遍/失败原因计数；
既有 `dram_frfcfs_stable_epochs` 保留作兼容别名，语义变为**单遍预测
认证通过**而非多轮收敛。单遍认证失败时 canonical 可退化为既有 FCFS
服务，该批不宣称经过 FR-FCFS 重排。其他独立实验若自身重复调用控制器，
其外层迭代不因此消失；正式 FLAC 配置中这些实验未开启。

验证先用真实 Simulator fixture 覆盖非绑定/绑定 LLC MSHR、读队列上限、
fallback 的 cache/请求/退休人口不变、依赖链响应反馈及 1/4 worker 确定性；
再在同源四案与保留的旧二进制比较 CPI 绝对及相对误差、可评分 PMU 和
独立 host 吞吐。原生参考的 unknown 仍需原样披露，局部测试通过不等于
完整精度或吞吐验收。

### 四案单遍 FR 同源诊断（2026-11-05）

完整构建、`fastsim_tests` 和真实 Simulator 专项测试通过；专项测试覆盖
MSHR 非绑定/绑定、原请求和退休人口、反馈、worker 数与旧配置键的兼容行为。
以下用冻结程序 `8c2702085e6f5951d29663dd3376091aa22cb77a8888a65f1242e8c957790136`
重放四案，功能身份、共享配置、任务与逐核指令人口均与此前 v2 对应输出一致；
保留预热及原线程计划，bank 和 store group 均关闭。参考复用此前同源的 native
验证，新输出没有再次经过独立 native 重评分。已知 ROI/macro 边界完整性限制
仍在，数字是所选统计口径上的诊断，不构成严格同起止窗口声明。

| 负载 | gem5 CPI | FastSim CPI | CPI 绝对误差 | 相对误差 | 候选 / 单遍通过 / 回退 |
| --- | ---: | ---: | ---: | ---: | ---: |
| FLAC | 0.281872 | 0.321041 | 0.039169 | +13.90% | 358 / 358 / 0 |
| Graph500 | 0.652711 | 0.754031 | 0.101320 | +15.52% | 5324 / 5324 / 0 |
| Marian | 1.428304 | 2.934776 | 1.506472 | +105.47% | 6335 / 6334 / 1 |
| XZ | 0.603524 | 0.647965 | 0.044441 | +7.36% | 3364 / 3364 / 0 |

四案等权 CPI MAE 为 0.422851 cycles/macro，MAPE 为 35.56%；各案 CPI
与此前 v2 输出一致，`workload_task` 退休缓存事件对象逐项一致；这不代表
模型事件已经准确。各候选 Q 的控制器调度调用数均为一次，Marian 的一次
回退由 fill 可见性证书失败触发。此次并行共享宿主，不能据计时认定吞吐
改善；退休 load 的 native L2/L3 存在 unknown 时不能计算完整事件误差。
逐核人口、完整身份、PMU 原始值和回退原因记录在
`tmp/spr-frfcfs-single-pass-v2-20261105/comparison.json`，该目录是实验
记录，不随源码提交。
