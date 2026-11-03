# Marian/XZ：独立 store 回调修复与验证

日期：2026-10-28。状态：机制修复、C++ 回归、六项真实 ROI 对照及两项串行吞吐 pilot 完成；默认未推广。后续结果以本文结果段及链接收据为准。

## 问题与修复

本次依据 [32 GiB 机制审查](spec-speed32g-two-stage-error-mechanisms-20261028.md)，优先追踪 XZ C4 core0 和 Marian C8 core2 的 SQ、访存响应及退休依赖链。稀疏、稠密观察均沿原完整功能预热进入，未冷启动或重新平移测量窗口。热点分别确认在 clear_page_rep 和 memcpy/memmove 的 store 循环。

确认并修复两个相连的问题：

1. 目标 profile 已提供普通 store 的 L1/L2 回调时延 1/5 cycles，但 legacy 路径未启用 prepare lifecycle 时使用通用 load 路径的 2/6-cycle 服务。通用 load 时序与从 store 准入到 callback 的服务不是同一起止边界，不能直接复用；load 的 data-ready/wakeup/WB 约束仍独立保留，本片不改它们。
2. 即使独立使用 store 回调，prepared resident head 的 publish 仍使用 base completion−issue 作为最短服务。这会将 commit 前已经等待的执行/WB 拥塞再次计入 publish；新增 WB 压力反例准确复现了这一点。修复后 commit 继续等待执行/写回，publish 使用自己的 L1 store callback。

新增独立开关 core.store_private_callback_timing，默认 false；[实验配置](../configs/gem5-exp-store-private-callback.cfg) 只启用这项修复，不调整目标时延参数。它不启用 prepare lifecycle、LRU 证书、bounded/event-reference store 求解，也不改变 FR-FCFS、SQ 容量、请求准入或 FST。

普通 store 与 load、atomic、page walk、IFetch 分开处理。开关关闭保持原路径；开关开启要求显式给出两级 store_hit_latency，缺少参数会拒绝，不自行借用 load 时延。

## 参数依据与两阶段成本

目标 MESI_Three_Level 的 L0 mandatory queue 为 1 cycle，E/M 的 Store 转移通过 hh_store_hit/writeCallback 同拍响应；私有 L2 hit 为 L0 mandatory queue 1 + L0 request 2 + 私有 L2 response 2 = 5。现有 profile 已写明此映射，本次没有根据 CPI 改这些数值。

补充原生日志的有界前缀观察与其一致：XZ 样本有 30,730 个合格单请求 L1 回调为 1 cycle、42 个为 2 cycles，以及 31 个 L2 回调为 5 cycles；Marian 样本有 29,651 个合格 L1 回调为 1 cycle。额外等待仍可能来自动态资源冲突，不能将配置的最小服务理解为所有请求端到端恰好等长。这些样本没有 ISA 请求角色字段，不能冒充仅 store 的全量统计。

修复复用原 private_response_latency 和已有 SQ 反馈位置；没有新增 UOP/请求遍历、事件表、全量反馈轮次或共享层级回滚，Q 保持 1024。新增工作为现有路径中的条件选择和 store publish 下界计算，实际宿主成本仍由独立测试衡量。

## 兼容性和审查结论

- 关闭开关时，六个原输入的来源身份、时间账、线程、CHA 和非宿主吞吐 scope 指标与冻结原预测逐项相同。最终二进制 XZ C4 OFF 也保持一致。
- 最终 XZ C4 真实 ROI 的 generic/fast 来源、时间账、线程和 CHA 相同；定向测试覆盖两级服务的 generic/fast 一致性。
- 独立审查发现部分 Sequencer 实验无条件将 CPU response 减去 load wakeup 一拍，不能直接接收 store callback。因此本开关暂时明确拒绝 hierarchy_walk、Sequencer load-admission、line-coalescing、buffer service-repair 和 software-prefetch early-ack 组合；本批六项配置均未启用这些组合。旧配置不开新开关不受影响。
- 本次不推广默认；对其他未验证组合、所有工作负载或硬件等价不作认证。

## 验证记录

1. 首轮定向 RED：ordinary store must consume its explicit private callback latency without enabling prepare lifecycle。
2. 首轮实现后完整 fastsim_tests 通过。
3. 补充 WB 压力 RED：store commit must retain execution/WB waits without charging them again to its private publish callback。
4. 最终构建及完整 fastsim_tests 通过；补充两级参数缺失、六个不支持组合的拒绝、load/atomic 保持原结果、SQ release 对应 callback、WB 等待不重复计费、generic/fast 对照。

构建命令为 cmake --build build -- -j16；执行 LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests。没有重新编译 gem5。

结果目录为 [本轮实验](../tmp/marian-xz-service-repair-20261028/)；其中 before 保存本次修改前的实际工作树文件和二进制，而非用 git HEAD 覆盖既有工作。candidate-final 冻结最终二进制及涉及源码，避免之后的构建漂移。task-only.diff 只包含相对于本轮 before 的差分。

## 原生诊断的使用边界及磁盘

补充诊断复用原 gem5 二进制及已有 KVM checkpoint，跳过 OS 启动，保持 32 GiB、原功能预热及任务配置。观测 wrapper 和参数单独记录，不伪装成未经观测的原 runner。两次运行的自然功能窗口已经不同于原正式来源，不能将其时间或贡献比例套到指定快照。

Marian 诊断正常完成，returncode=0，约 1,705.6 s，分配磁盘约 9.27 GB。XZ 的完整逐请求日志触及本轮设定的 9 GB 单次诊断配额而停止，returncode=-15；其 FST 未形成合格完整来源，不作完整 ROI 评分。已压缩并往返校验保留原生完整日志行，回收仅本次中断诊断的 FST/配套文件，逐文件 SHA/字节数见 retirement-receipt.json。原正式来源均保留。

两次 debug pipeline 时间窗误挂在初始化 stats.reset，早于真实测量开始，日志未提供有效流水级见证；diagnostic-window-audit.json 已明确标为不可用。生产者独立门控的 native-response 日志不受该 debug 开关影响。现有日志仍不能唯一关联本批所有关键 SQ 前驱，不能据此宣称全部误差已定位。

## 真实 ROI 结果与吞吐

正式比较继续使用原六个冻结来源：Marian C4/C8、XZ C4/C8，加 Graph500 C16 和 FLAC C4。额外原生诊断不替换这些来源。参考身份独立核验为 6/6，原预测关闭态兼容核验为 6/6。

原始预测位于 ab；最终修复预测位于 final-ab。六项最终输出均重新通过实际参数审计，每项 130/130；另逐 controller 从原 INI 推导 store callback，模型输出保留 1/5。这些检查没有把 semantic_equivalence=false 改为 true。

维护 tools/run_native_kernel_fastsim_validation.py 按原 Ruby/SLICC 来源逐配置独立进程重评分，保留原 task 宏指令数、共同终点及未知资格。退休 miss 的参考 unknown 不清零，不将其混入完整 PMU 精度。

吞吐使用最终同一二进制，CPU160–175、NUMA1，串行交错三个 OFF/ON 对照。各轮保留实际目标周期核对、测量/预热/全程 wall 和 Peak RSS；等待本轮回放、验证进程结束后才启动。输入在精度验证中已完整哈希，吞吐重复轮关闭额外的全输入独立哈希，并核对前后文件状态；不把省掉该 I/O 宣称为模型提速。共享宿主上的 pilot 不自动等于正式吞吐准入。同一二进制 OFF/ON 只衡量开关增量，未另行测量旧二进制到新二进制的全局编译差异。本文的单次回放全程 wall 不包含另做的输入独立哈希，不能当作零输入审计成本的生产流水线时延。

本轮六项 OFF/ON 共十二次维护验证均通过来源/范围/守恒检查。以下是这六项的对照子集，不替换原九项快照。

| 配置 | gem5 CPI | OFF CPI | ON CPI | 相对误差 OFF → ON | CPI绝对误差 OFF → ON，cycles/宏指令 |
|---|---:|---:|---:|---:|---:|
| c04-801.xz_s | 0.758505 | 1.007150 | 0.969584 | +32.7809% → +27.8283% | 0.248645 → 0.211079 |
| c08-801.xz_s | 0.673482 | 0.933625 | 0.879998 | +38.6266% → +30.6639% | 0.260143 → 0.206516 |
| c04-872.marian_s | 4.933260 | 6.005699 | 5.868488 | +21.7389% → +18.9576% | 1.072438 → 0.935228 |
| c08-872.marian_s | 5.189270 | 6.039175 | 5.935064 | +16.3781% → +14.3718% | 0.849905 → 0.745794 |
| c16-854.graph500_s | 2.311802 | 2.053992 | 2.066661 | -11.1519% → -10.6039% | 0.257810 → 0.245141 |
| c04-817.flac_s | 0.453030 | 0.478073 | 0.478073 | +5.5277% → +5.5277% | 0.025042 → 0.025042 |

| 范围 / 开关 | CPI MAE，cycles/宏指令 | MAPE | WAPE | signed bias | P50 / P90 / P99 APE | max APE |
|---|---:|---:|---:|---:|---:|---:|
| primary_marian_xz_four / off | 0.607783 | 27.3811% | 22.7954% | +22.7954% | 27.2599% / 36.8729% / 38.4512% | 38.6266% |
| primary_marian_xz_four / on | 0.524654 | 22.9554% | 19.3593% | +19.3593% | 23.3929% / 29.8132% / 30.5788% | 30.6639% |
| all_six_with_controls / off | 0.452331 | 21.0340% | 16.6780% | +5.6791% | 19.0585% / 35.7038% / 38.3343% | 38.6266% |
| all_six_with_controls / on | 0.394800 | 17.9922% | 14.7409% | +4.2825% | 16.6647% / 29.2461% / 30.5221% | 30.6639% |

四项主要负载 MAE 降低约 13.68%，MAPE 降低 4.4257 个百分点；五个变化项均向参考靠近，FLAC C4 不变。该结果不是达到统一误差阈值的声明。

聚合直接收集维护验证器已完成的单项报告，并保存其 SHA256；不重复选择参考或为了汇总重新扫描全部 FST。见 [数值及聚合口径](../tmp/marian-xz-service-repair-20261028/comparison.json) 与 [十二项验证收据](../tmp/marian-xz-service-repair-20261028/validation-combined-receipts.json)。

| 配置 | 测量吞吐 OFF / ON，M user UOP/s | 中位吞吐变化 | 三组配对变化 | 预热 OFF / ON，s | 回放进程全程 OFF / ON，s | Peak RSS 中位数 OFF / ON，GiB |
|---|---:|---:|---:|---:|---:|---:|
| c04-801.xz_s | 3.2611 / 3.3447 | +2.56% | -27.91% / +3.32% / +1.74% | 18.322 / 17.752 | 28.347 / 27.538 | 3.159 / 3.148 |
| c08-872.marian_s | 2.2822 / 2.3468 | +2.83% | +2.81% / +2.83% / +2.87% | 18.243 / 17.954 | 35.247 / 34.508 | 4.140 / 4.189 |

十二次吞吐回放均退出成功，目标时间账、线程、CHA、非宿主吞吐 scope 指标与对应精度预测相同；输入文件前后状态相同。XZ 首组配对出现 −27.91% 的明显波动，不能将中位数 +2.56% 当作稳定加速。Marian 三组配对均约 +2.8%，是本案例 pilot 的直接观察；不推广成所有负载的性能结论。未删除或剔除首组样本，也未达到项目完整吞吐准入。

[吞吐原始样本及边界](../tmp/marian-xz-service-repair-20261028/throughput-summary.json) 包含测量 wall、功能预热、回放进程 wall 和 Peak RSS。

## 仍待解决的误差

后续历史核对见 [Store 生命周期：历史效果、当前使用情况与剩余误差](store-lifecycle-history-and-current-gap-20261028.md)。已有 `bounded_analytic` 实现覆盖了更完整的解析生命周期，但本轮六项使用 legacy，尚未在这些新来源上复测 bounded；下一步应优先审核并复用该实现，再决定需要补充哪些机制。

本次修复解决私有 store 回调和 publish 重复等待，不能覆盖全部 store miss/prepare 生命周期。XZ 的冷 store 仍可能在 epoch 后缀保守资格下无法重叠 prepare，随后逐项支付长服务；Marian 的访存/依赖等待也仍存在。其进一步修复需要自身 canWB、prepare/head-demand 角色、resident/owner 世代及准入—释放区间的配对见证，不能直接开启旧 lifecycle/证书组合或统一缩短 DRAM。

Bound 中已有的虚假等待也不会被 Weave 的 max 反馈自动消除。本轮没有将 FU、DTLB、分支或 DRAM 容量候选混入这一片；若关键服务证据转向它们，应按独立反例和反向控制另行验证。

### 修复后残差的补充观察

随后同源中性观察确认：Marian C8 的最终 ON 预测切到 generic+CPI attribution 后，功能来源、时间账、线程、CHA 和非宿主吞吐 scope 指标仍逐项相同，见 [中性收据](../tmp/marian-xz-service-repair-20261028/residual-attribution/neutrality.json)。XZ 使用此前最终 generic/fast 等价回放。

| 模型关键核 | SQ 容量原因，cycles | 访存响应原因，cycles | 依赖原因，cycles | feedback extra 合计，cycles |
|---|---:|---:|---:|---:|
| XZ C4 core0 | 1,482,022 | 1,145,333 | 1,079,190 | 3,706,545 |
| Marian C8 core2 | 4,175,799 | 6,063,323 | 1,966,852 | 12,205,974 |

这些是模型内部反馈关键等待分类，不是 FastSim 相对 gem5 的误差贡献，不可将其相加后从 CPI 扣除。它们仍支持优先沿权限/数据服务到 SQ 释放和依赖传播的链条排查。当前净修复只使四项 MAE 减少约13.68%，不能把剩余约86.32%机械分配给上述等待类别。

剩余优先假设：合法 canWB 后的 prepare 与 FIFO publish 重叠没有准确表达；同 set 后缀的保守拒绝可能压低有效准备，反方向又有过早复用 permission 的旧近似。普通 feedback 沿 corrected_issue+saved_latency 计算响应，在服务仍被原排队约束固定时可能重复计入 slack；只有取得有效服务身份与路径稳定证明后才能认定这一实例并修复。两者都不是把所有 store 改为并行或统一减少 miss latency。

修复后 XZ C4 的 FR-FCFS 最大 pending 为3，无回退或饱和选择；Marian C8 仍有185个回退 epoch、454次饱和选择。后者是模型结构和工作量事实，不是185笔错误请求，也不提供CPI份额。不能将旧拓扑缩窗作为当前根因，或将这些计数直接当作正误差来源。

目前尚缺同一正式输入、正确测量窗口中的原生逐请求退休关键链，因而未完成剩余误差的唯一分解。尤其不能把 FU/DTLB 候选、原生请求合并数或不同窗的最终 stats 变成已确认的误差比例。

## 复现与证据索引

- [实现计划与取舍](superpowers/plans/2026-10-28-marian-xz-service-repair.md)。
- [实验配置](../configs/gem5-exp-store-private-callback.cfg)；实际六项 overlay 保存在 final-ab，各自包含原 32 GiB 几何与 owner plan。
- [默认关闭与 generic/fast 核对](../tmp/marian-xz-service-repair-20261028/compatibility-and-kernel-equality.json)。
- [目标回调参数来源](../tmp/marian-xz-service-repair-20261028/store-callback-parameter-provenance.json)、[原生回调前缀证据](../tmp/marian-xz-service-repair-20261028/native-callback-prefix-evidence.json)。
- [最终二进制/源码指纹](../tmp/marian-xz-service-repair-20261028/candidate-final/freeze.json)、[本轮源码差分](../tmp/marian-xz-service-repair-20261028/task-only.diff)、[最终测试日志](../tmp/marian-xz-service-repair-20261028/final-tests.log)。
