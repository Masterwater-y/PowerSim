# 十项 perf cache 统一计数的实施与验证记录

后续已交付近似预测数值和private-L2漏计生产修复，见
[后续实施结果](perf-cache-estimates-and-l2-repair-results-20261015.md)。本文保留首次
接线阶段的结果；严格认证字段与后续近似预测字段含义不同。

2026-10-15。已实施独立事件合同、退休资格账本、gem5/FastSim 请求观察器和维护
验证器接入，并完成真实四核采集。**完整三端 perf 等价性尚未完成**：当前十项
事件的正式值均为 null，没有新的十项 MAPE/max APE。诊断计数、来源检查通过和
硬件事件资格是不同结论。

依据[已批准设计](superpowers/specs/2026-10-15-perf-cache-threeway-alignment-design.md)
和[实施计划](superpowers/plans/2026-10-15-perf-cache-threeway-alignment.md)。
本轮没有修改延迟、缓存几何、buffer TSO 或共享服务时序参数。

## 1. 已实施的路径

| 层次 | 实现 | 行为 |
|---|---|---|
| 共同合同 | [事件字典](../configs/perf-cache-event-dictionary-v2.json)、[合同校验](../tools/perf_cache_contract.py) | 固定 SPR V1.40 十项 raw 编码、域、单位、逐事件能力；raw/per-1K-macro 分开统计 |
| L1 退休资格 | [FastSim 退休账本](../include/fastsim/perf_retirement.hpp)、gem5 对应 `perf_retirement.hh` | 三个 L1 事件分别累计资格和 unknown；mixed source 可同时满足不同事件，不再要求三项互斥相加 |
| 原生请求 | [native observer](../gem5-fs/src/mem/perf_cache_native.hh)、Sequencer、Ruby 消息、SLICC | 在线聚合 data/RFO/code/prefetch/writeback 与本层结果；独立于 committed `TraceValid`，输出 oracle-only sidecar |
| 模型请求 | [FastSim observer](../include/fastsim/perf_cache.hpp)、[报告](../include/fastsim/perf_cache_report.hpp)、`src/simulator.cpp` | 接入本端实际执行路径、取指、RFO 与 PTE 诊断；随 CoreCounters 提交/回滚；跨阶段 carry 不重复计数 |
| 独立审计 | [sidecar 审计器](../tools/audit_perf_cache.py)、[维护验证器](../tools/run_native_kernel_fastsim_validation.py) | 当前 FST/owner/边界人口独立校验、按请求原语重算、逐核与总和守恒；新增 `--perf-cache`，不回退 legacy |
| 真机探针 | [微内核](../tests/workloads/perf_cache_probe.c)、[采集工具](../tools/run_perf_cache_probe.py) | 固定核、当前线程、user-only、不继承、不 multiplex；保留 raw 指令分母和源/二进制/事件表身份 |

新命名空间是 `scope_metrics.perf_cache`，包含 physical/user/kernel/workload_task/
other_task 及逐核输出。`diagnostic_value` 是已观察子集，`value` 仅在该事件所有
必需能力通过时才可用。硬件认证状态独立保留。事件定义绑定不意味着完整模型
已经生成该事件所需的全部请求。

模型本层 L2 miss 不随最终 remote supply 消失；LLC 目录/数据状态与最终供给者
分开。PTE、脏逐出、一致性写回保留独立原语，未证明资格前不强塞到十项事件。
失败计算推演回滚；已接受目标请求后续 squash 不取消已发生请求事件。
目前尚无完整 wrong-path provider，后一个规则已做机制测试，但不构成实际请求
人口完整性的证明。

## 2. 四核真实采集和发现的缺陷

实验根目录：[本轮证据](../tmp/perf-cache-threeway-20261015/)。负载为四核
`thread-state-fs-probe`，参数 `1000 2000`，first-core 300000 measured user-UOP
共同结束，包含功能 warmup 和内核，buffer TSO=114。规模用于接线、来源、窗口和
生命周期验证，不代表 TeaLeaf 或十配置精度矩阵。

第一次采集 `fs-probe-perf-cache-v2` 中，新增 observer 错用了 Sequencer 的
`coreId()`。它是 CorePair 的旧参数，现场默认值99；观察器按物理核0–3建窗，
所以 `issued=0`，所有需求请求缺少有效身份。**该轮不能作为有效请求人口证据**，
保留为故障证据，不进入下面的有效输入集合。其 source/contract gate 可以通过，
因为所有不完整字段均未发布正式值；这不代表请求覆盖正确。

修复只把新增 observer 的 issue/merge/retry 核参数改为 `m_version`，保持旧
`coreId()` 和 RubyRequest 行为。回归直接执行生产代码参数表达式，在
`coreid=99/version=0` 上观察失败后通过。overlay 已同步，canonical GCC11 /
Python3.11 / TAOGEN_SHARED 构建使用当前192核 affinity 的80%，即 `-j153`。

修复后输入：[`fs-probe-perf-cache-v2-coremap`](../tmp/perf-cache-threeway-20261015/source/fs-probe-perf-cache-v2-coremap/)。
采集正常退出，149.41秒，二进制采集前后 SHA256 一致。

| 核 | issued | merge 通知 | retry 通知 | L2 demand miss 诊断 | LLC miss 诊断 | unknown identity |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 42210 | 1060 | 5 | 187 | 8 | 88 |
| 1 | 38257 | 963 | 0 | 510 | 82 | 88 |
| 2 | 31326 | 615 | 0 | 63 | 3 | 43 |
| 3 | 30342 | 597 | 0 | 57 | 4 | 38 |

四核 `unknown_kind=0`、`ignored_hooks=0`、`duplicate_edges=0`、`overflow=0`。
data/RFO/code 均有非零事务身份和逐层事实。issued/merge/retry 是接入账本的生命周期
通知，不能直接当作任何一项硬件 PMU。

剩余 unknown identity 来自协议自行生成的 generic-prefetch/permission-prepare
请求等边沿；协议中 `Store_Prepare` 同样可使用 `PrefetchBit:Yes`。因此不能把所有
generic-prefetch 都认证成真实硬件预取，亦不能把剩余 unknown 直接忽略。
请求的 event-clock task/CPL 上下文尚不可用，子任务请求诊断也不能用零冒充完整值。

新两次 FST 分别153,753,120和153,244,640 bytes；第二次结束时，全体实际 FST
总量 **19,492,084,584 bytes（约19.49 GB）**，未超过20,000,000,000 bytes。
未复制或删除已有 FST。

## 3. 当前十项输出与误差状态

下表是修复后真实输入的 **physical scope 诊断**，不是已经同人口的硬件事件对比，
不能用这些数直接计算用户要求的 MAPE。每行正式值和误差均不可用。

| 字段 | gem5 诊断 | FastSim 诊断 | 正式值/误差 |
|---|---:|---:|---|
| `perf_retired_loads` | 54736 | 54736 | null / N/A |
| `perf_retired_stores` | 15358 | 15358 | null / N/A |
| `perf_l1d_load_hits` | 51026 | 42104 | null / N/A |
| **`perf_l1d_load_misses`** | **550** | **346** | **null / N/A** |
| `perf_l1d_load_fb_hits` | 300 | 0 | null / N/A |
| `perf_l2_accesses` | 1697 | 733 | null / N/A |
| **`perf_l2_demand_misses`** | **817** | **712** | **null / N/A** |
| `perf_l2_writebacks` | 0 | 0 | null / N/A |
| `perf_llc_accesses_core` | 1117 | 417 | null / N/A |
| **`perf_llc_misses_core`** | **97** | **77** | **null / N/A** |

具体缺口由每个事件的 `missing_model_capabilities` /
`missing_reference_capabilities` 输出，不是靠总开关隐藏：

- 退休域：ROM/REP/mask/软件预取/fault-restart 等硬件 ISA 资格尚未认证；本例
  FastSim 另有2个不完整尾宏、2个 warmup-spanning 宏，不能将完整性置真。
- L1：三个来源事件资格独立，完整 forwarding 等成员尚有未知映射；FB 的0只是
  当前模型子集的观测，不等于真机不存在 FB hit。
- L2 demand：真实 data/RFO/code 接线已验证，但完整 speculative data/code、
  接纳/合并/重试资格、事件上下文和模型实际计数边沿还未完全验证。
- LLC：另需补 L1/L2-target 预取资格、core-origin 筛选及 directory/data/remote
  supply 到目标硬件事件的映射。
- Writeback：虽已观察 private-L2 outgoing 事务，但与 `L2_TRANS.L2_WB` 的具体
  事务方向没有认证；本例零值不能作为该映射证据。

输入窗口身份与请求 ROI 边沿也仍是两个缺口：现有 manifest/ThreadStatePlan
只证明整数布局与 owner 连续性，不能用相同记录数或复制 gem5 tick 的方式补齐。
[纯功能窗口接口](perf-cache-functional-source-interface-20261015.md)明确了最小
内容哈希、逐核 ordinal、首尾宏成员和功能锚点，以及 QEMU/DynamoRIO 的生产责任。
这个接口尚未实现，完整功能状态/错误路径及预取 provider 也尚未交付。

用户提供的旧 MAPE 表保留为历史待复现结果；本轮没有将 old scalar、native
tag miss 桶重组结果或两端退休总数相等升级为这十项的 perf 精度。

## 4. 真机事件资格探针

现场 SPR：family6/model143，L1D48KiB/12-way，L2 2MiB/16-way，LLC每socket
99840KiB/15-way。现有 gem5 四核输入的 L1D32KiB/8-way、L2 1MiB/8-way、
LLC64MiB/16-way与之不同。以下用于验证事件行为，未与不同缓存配置或不同
动态路径的四核输入计算三端精度。

真机保留了早期 quick105次、working-set150次、writeback-control6次和方向诊断
6次读数。早期 working-set 未保留源文件快照且与编译重叠，因此又在本项目编译/
采集/审计/测试/吞吐均结束后，冻结源文件重跑150次，作为下表的当前来源。所有
批次满足 `time_enabled == time_running > 0`；新批次每次200000迭代、3次重复，
表中为每迭代中位数，未减去固定开销。

| 微内核 | retired loads | L1 miss | FB hit | L2 demand miss | LLC miss |
|---|---:|---:|---:|---:|---:|
| 热 load，4KiB | 1.000300 | 0.000020 | 0.000060 | 0.000000 | 0.000000 |
| 完整 forwarding，4KiB | 1.000360 | 0.000010 | 0.000000 | 0.000000 | 0.000000 |
| flush 后两个 load，4KiB | 2.000640 | 0.999825 | 0.999835 | 1.000010 | 1.000000 |
| 跨行 mixed load，4KiB | 1.000603 | 1.000030 | 0.000015 | 1.000000 | 1.000000 |
| `cmpsq`，4KiB | 1.000422 | 0.000023 | 0.000010 | 0.000000 | 0.000000 |
| dependent random walk，256KiB | 1.000120 | 0.999767 | 0.000025 | 0.000008 | 0.000000 |
| dependent random walk，16MiB | 1.000160 | 0.996773 | 0.001655 | 0.995348 | 0.001570 |
| dependent random walk，256MiB | 1.000225 | 0.999980 | 0.000060 | 1.028830 | 0.762058 |

这些观察支持区分宏指令、fill follower 与各层请求，不能独自证明所有特殊 ISA、
store forwarding 或推测资格。随机环会先初始化并 warmup，不能把只读测量阶段
简单当成“绝无 writeback”。补充了 flush-before-warmup 的 `walk_clean` 对照后，
16MiB 上 `L2_WB` 仍约0.98/迭代；同时记录 `L2_LINES_OUT.NON_SILENT` 和
`L2_LINES_IN`，尚不足以完成事务方向映射，继续保留未认证。

当前采集工具保存微内核源快照、二进制/编译器/命令/CPU身份，拒绝 multiplex
和迁核，按 case 与 working-set 分组。早期批次的源哈希和二进制仍保留，但不能
把后来修改的源文件冒充早期快照。新批次有完整源快照，主机仍非独占：例如
256MiB walk 的 LLC miss 从早期约0.995/迭代变为约0.762，说明容量行为存在
显著运行间变化，不能据此认证硬件等价或外推精准命中率。原始指令分母与范围见
[冻结真机读数](../tmp/perf-cache-threeway-20261015/hardware-working-set-frozen/runs.json)和
[分组摘要](../tmp/perf-cache-threeway-20261015/hardware-working-set-frozen/summary.json)。

## 5. 同源时序回归、CPI 和吞吐

冻结的旧主输入 `fs-probe-rom-gdb` 上，本轮前后可执行文件逐字段比较：所有旧
目标时序、native 和 legacy 输出一致；新命名空间及新增资格字段单独保留。
主机 wall time、吞吐、计时器及异步队列等待/驻留状态不作为目标时序差异。

修复后新采集的单例 active CPI 为 **workload-task / task-oncpu-switch-entry-v1**，
仅是该会话约定下的模拟器对比，硬件 perf 等价性未认证：

| gem5 active CPI | FastSim active CPI | 相对误差 | 绝对误差 cycles/macro | CPI MAE（N=1） |
|---:|---:|---:|---:|---:|
| 0.799111 | 0.899643 | +12.5805% | 0.100532 | 0.100532 |

Elapsed：gem5 41.987637µs，FastSim 33.860106µs，相对误差−19.3570%。
elapsed 不是 CPI 分子。新采集具有不同的动态内核/调度人口，不能与旧主输入
不同 CPI 直接作修复收益对比；上述同源不变性才是本轮计数接入的时序回归证据。

所有本项目编译、采集、审计和测试完成后，同一旧主输入以核0–7固定 affinity，
前后交替顺序执行5对；二进制、配置 include chain、manifest、thread-state plan
与 FST 均记录哈希并在结束复核。

| 可执行文件 | measured user-UOP/s 中位数 | 均值 | 全程 wall 中位数 |
|---|---:|---:|---:|
| 本轮前 | 3.916657M | 3.847265M | 0.651187s |
| 本轮后 | 3.790901M | 3.810235M | 0.657972s |

中位数变化−3.2108%，均值约−0.9625%。这是共享主机、短窗口、包含本轮退休和
请求观察器的整体开销诊断，不能外推大型负载或尚未实现的 wrong-path provider。
吞吐单位取 `scope_metrics.throughput.user_uops_per_second`，不是 target IPC，
也不是验证器审计耗时。

## 6. 验证、审查与复现

- CMake默认构建通过；`fastsim_tests`、退休、请求runtime、请求integration四个
  C++测试可执行文件全部通过。
- 当前 `test_perf*.py` 45项全部通过；原有 native validator 13项通过。
- 全Python发现373项，5项skip、3项既有环境错误：`test_python_bindings` 缺
  `fastsim_py`；`test_checkpoint_recovery_receipt_is_strict_and_reusable` 与
  `test_checkpoint_recovery_rejects_record_output` 缺外部 TCSim checkpoint 配置。
  全量运行在最后新增少量针对性回归之前；新增/改动路径随后均有专项通过证据。
- gem5 canonical构建及启动检查通过；native standalone测试和 overlay一致性通过。
- 独立审查修复了逐核人口/范围可被总和掩盖、unknown原语伪装完整零值、缓存指纹
  遗漏FST来源三项问题，另补审计结束时sidecar变化的回归；均观察失败后通过。
  最终代码复审无新增Critical/Important发现；第二轮真实请求恢复由随后采集另证。

有效集合只含修复后案例，通过目录链接引用原文件，不复制 FST。独立身份门禁
为1/1有效；维护验证器检查包括当前FST、owner、common-end、逐核人口和原生层级。
`passed` 仅表示来源与合同通过；十项完整事件计分数仍为0。

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/perf-cache-threeway-20261015/accepted-source \
  --stats-dir tmp/perf-cache-threeway-20261015/stats \
  --output-dir tmp/perf-cache-threeway-20261015/validation-v2-accepted \
  --expected-cases 1 --jobs 1 --perf-cache --perf-retirement
```

证据入口：

- [修复后身份检查](../tmp/perf-cache-threeway-20261015/source/fs-probe-perf-cache-v2-coremap/identity-validation.json)
- [真实请求账本摘要](../tmp/perf-cache-threeway-20261015/source/fs-probe-perf-cache-v2-coremap/request-population-diagnostics.json)
- [维护验证结果](../tmp/perf-cache-threeway-20261015/validation-v2-accepted/summary.json)、[十事件覆盖报告](../tmp/perf-cache-threeway-20261015/validation-v2-accepted/perf-cache-summary.md)
- [同源吞吐](../tmp/perf-cache-threeway-20261015/performance-quiet-v2/summary.json)
- [专项Python测试](../tmp/perf-cache-threeway-20261015/perf-python-final.log)、[C++测试](../tmp/perf-cache-threeway-20261015/cpp-focused-final.log)、[全Python环境错误](../tmp/perf-cache-threeway-20261015/python-full.log)

后续仍需实现纯功能窗口/宏成员元数据和事件边沿上下文，再逐类补足错误路径、
预取与writeback provider及硬件资格矩阵。三个重点miss只有在各自来源、请求
人口、窗口和硬件映射完整后才进入正式误差报告；本轮不将这些工作标为完成。
