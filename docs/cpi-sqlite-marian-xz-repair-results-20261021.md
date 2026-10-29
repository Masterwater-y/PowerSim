# SQLite、Marian、XZ CPI 第一批机制修复与验证（2026-10-21）

本轮承接[排查报告](cpi-sqlite-marian-xz-analysis-20261021.md)及用户“开始修复”的要求。已修复 XZ 重复 ROI 检查、LRU 准备资格按地址误伤后继请求、分支恢复发现阶段，以及软件预取的功能身份和退休 PMU 分类。**软件预取提前 CPU 确认尚未形成可用的完整时序修复，默认关闭；SQLite 的实际 cache 请求准入传播仍有缺口。** 机制回归通过不能替代真实负载精度。

## 1. 输入、配置及计量边界

- 四核、八通道、buffer TSO114，保留两阶段框架。SQLite 为四个标准 rate 副本；Marian 为一个官方 speed 进程；XZ 为官方 speed 二进制、官方 train 40MiB 输入，四 worker 与 producer 共五个任务。
- SQLite 复用原正式源；XZ 使用修正插桩后的新二进制，Marian 保持原工作负载二进制。后两者均用新生产者重新构建 checkpoint：KVM 初始化 → checkpoint → O3 warmup → stat。旧源保留，不能把新自然窗口与旧窗口之间的变化全部算作模型收益。
- CPI 为 `workload-task active cycles / macro instructions`。elapsed 独立报告；绝对误差为 cycles/macro，不是百分比。PMU 缓存参考仅来自维护验证器审核的守恒 Ruby/SLICC 摘要。
- 没有修改 branch penalty、DRAM 常数、退休宽度以拟合 CPI。prepare 候选使用采集配置的 `commitToIEWDelay=1`、L1 store 1 cycle、private-L2 store 5 cycles；L2 的 5 来自 mandatory queue 1 + request 2 + response 2。它仍不是完整的 canWB/端口/permission 实现。
- gem5 以可用 192 核的 80%（153 jobs）构建。两个新来源均受 FST 总量 100GB、空闲至少 20GB、warmup 最多 180 宿主秒保护。

| 新采集 | warmup 宿主秒 | 正式来源 FST bytes | 状态 |
|---|---:|---:|---|
| XZ | 96.75 | 3,290,219,232 | 采集、恢复及 source 审核通过 |
| Marian | 62.41 | 2,976,616,992 | 采集、恢复及 source 审核通过 |

源码、原模型和新模型二进制、输入及命令保存在[实验目录](../tmp/cpi-three-workload-repair-20261021/)。独立硬件身份验证与精度验证分开保留。

| 冻结对象 | SHA256 |
|---|---|
| 原 FastSim | `62d1f9f652621fd75260e63ffa886b3d777c300edb665331db31365ba60c76ad` |
| 最终组合 FastSim | `5b498b5ff724e623a8f124cfb582773f5357c8f0f48388f89450608d208e1a3b` |
| 新 gem5 生产者 | `766d70d04ac4f5a3af7f85e4929de809bec7d1917cc2ae667b266a873a3403de` |
| 新 XZ marked 二进制 | `864c03e7d499bff04cd6be5aefae6ff08c5643d5df4cb7fab345a898e2841abf` |

完整来源见 `producer-ready.json`、`final-source-freeze.json`、`final-validation-cases.json` 与两个 `source-driver.json`。SQLite 复用原源不要求旧 checkpoint 伪装成新生产者；新 gem5 只用于后两个重新采集的来源。

逐项[参数复核](../tmp/cpi-three-workload-repair-20261021/review/final-parameter-alignment.md)确认六配置的核数、通道、LRU、buffer114、时钟一致；SQLite旧 frozen 与本轮 frozen 的八层 include 文件逐个哈希相同。**默认模式的 store lifecycle 关闭，实际仍使用通用 hit latency L1=2/L2=6；声明了尚未激活的 store 1/5，不代表这条默认时序已经严格对齐。** prepare 才使用独立 store 1/5 和退休后发送下界，完整端口/queue/ownership仍近似。

## 2. 已落地的修复

### 2.1 XZ 的插桩只在任务入场前执行

`fastsim_native_roi_ready()` 现在先检查 TLS entered，再读取环境变量。XZ producer、worker 的调用点另有每 OS 任务的一次性保护，跨压缩 block 保持；原先的 16KiB 和 worker 到齐条件未改。

真实五任务 launcher 和跨 block 回归通过，官方 train 输出与修复前逐字节相同。新正式 FST 的四核测量期 `getenv`、`__strncmp_sse2`、`__strlen_sse2` UOP 均为零。没有删除旧 trace 中的任何指令，也没有根据 CPI 选择新 ROI。

### 2.2 LRU 资格拒绝绑定到具体 store 请求

旧实现按 line 地址保存“不满足驻留证明”的结果，早先的 A 请求可能使更年轻、已经安全的 A 也被拒绝。新实现在证书模式中按当前 epoch 的具体 event 保存同 set 拒绝，跨核、page-fill 和 permission 丢失等全局安全限制仍按 line 生效。集合在 epoch 开头清空，事件压缩回收前再次清空。

两路缓存 A/B/C/A 回归先复现旧缺陷，再验证旧 A 被拒绝、年轻 A 合法，以及发送仍满足退休后 canWB 下界。默认关闭证书时不增加该集合查询。

原 XZ 正式窗口的已有 certificate+lifecycle 组合，在修复前后 CPI 都为 1.010127575，参考 1.062180834，相对误差 −4.9006%、绝对误差 0.052053259。**这次按请求修复在该窗口没有额外 CPI 收益。** 从原默认 +40.6646% 到该组合的变化主要是既有候选开关效果，不能记成本次代码收益。

### 2.3 分支恢复从实际可发现的阶段开始

模型用自己的预测方向/目标与纯功能静态直接目标识别 Decode 可纠正的目标错误。条件方向错误、间接目标、静态信息缺失或矛盾仍从后端发现。前端门禁、匿名推测压力和 response 传播使用一致起点；原恢复延迟常数保留。

目标等于 fallthrough 的零位移条件分支保留专门回归：预测 taken 且目标错误时，Decode 已能纠正最终 PC，不应仅因方向标签而强行等到后端。

原 SQLite 同源观察表明此修复只带来小幅净变化。另一个“切片/预演”准入原型被反例否定，未整合：正确后继 Fetch=239、Issue=244 时，canonical data cache 请求已在 210 发出。**内部时刻满足传播约束，不等于真实共享 cache 请求已经推迟。** 仍需能在 cache 状态变更前消费取指/分支反馈的请求管理机制。

### 2.4 软件预取身份和退休计数

FST request companion v2 增加纯功能 `SoftwarePrefetch=2`，不包含原生时间、命中路径或 oracle 标签。新版 reader 兼容 v1/v2；旧 reader 拒绝 v2。行校验和及完整 stream 校验保持。

软件预取仍退休为宏指令/UOP，但不属于架构 retired load/store。生产者、模型及独立退休审计使用同一资格规则；接受后的物理请求继续属于原生 Ruby 请求诊断，不能随 retired-load 分类一起删掉。独立 Python 审计验证 sparse 行绑定与 companion SHA，完整 stream FNV64 由强制 C++ reader 检查；不将 Python 审计宣称为另一套全流校验。

## 3. 已验证但未完成的时序问题

`core.software_prefetch_early_ack=false` 是默认值。功能身份和退休分类始终生效；默认时序仍保守地等待数据服务。

提前 CPU 确认候选尝试将 CPU ack 与 fill/MSHR/Sequencer 释放分开，但独立反例证明单独保存后台寿命不够：P(A)、P(B)、D(B)，一个 MSHR 时，P(B) 的服务被推迟到 113、填充到 226，而后继 D(B) 仍使用旧 callback，在 118 完成。这会读到尚未准备好的数据。

因此候选保持显式开关；只要 copied service 被重排，或 alias/skip 父请求已失效，就明确拒绝导出该时序结果。它没有实现在线接受/重试/回调归属，也不能作为 Marian CPI 已修复的依据。需要让后续 demand、跨 epoch alias 及所有资源释放都引用同一份更新后的父请求完成时间。

## 4. 真实负载验证

结果由 `tools/run_native_kernel_fastsim_validation.py` 生成；新模型同时启用独立 `--perf-retirement --retired-cache` 审核。default 表示保留两个 prepare 开关关闭；prepare 为显式打开 certificate+lifecycle，**没有推广为全局默认**。

新源旧语义控制只对冻结旧模型增加 v2 reader：验证相同 FST/companion 后，把软件预取按旧普通 load 处理。它用于隔离模型变化，不是新功能语义合格结果；不改写 trace，不用它认证新的退休 PMU。

### 4.1 Active CPI：同一来源内比较

SQLite 是原正式窗口；XZ、Marian 是本轮新自然窗口。每个负载仍只有一个独立窗口，三种模式不是三个独立样本。旧语义控制不能认证新退休 PMU。

| 来源 | 模式 | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---|---:|---:|---:|---:|
| SQLite原源 | 旧语义控制 | 1.000924991 | 0.880284074 | -12.0529% | 0.120640917 |
| SQLite原源 | default | 1.000924991 | 0.881132259 | -11.9682% | 0.119792732 |
| SQLite原源 | prepare | 1.000924991 | 0.878154264 | -12.2657% | 0.122770727 |
| XZ新源 | 旧语义控制 | 1.081955043 | 1.490234854 | +37.7354% | 0.408279811 |
| XZ新源 | default | 1.081955043 | 1.527190422 | +41.1510% | 0.445235379 |
| XZ新源 | prepare | 1.081955043 | 1.044795614 | -3.4345% | 0.037159429 |
| Marian新源 | 旧语义控制 | 2.665483924 | 3.187989881 | +19.6027% | 0.522505957 |
| Marian新源 | default | 2.665483924 | 3.170054366 | +18.9298% | 0.504570442 |
| Marian新源 | prepare | 2.665483924 | 3.122987909 | +17.1640% | 0.457503985 |

SQLite发现阶段修复净收益很小，误差仍约12%；prepare使它略差。XZ的prepare候选显著改善本轮窗口，但没有消除精确生命周期近似，且不是全局默认。Marian默认及prepare仍分别约+19%和+17%，不能宣布已解决。

### 4.2 Elapsed：表示目标运行时间

数值为共同终点的毫秒，不跨核相加。

| 来源 | 模式 | gem5 elapsed ms | FastSim elapsed ms | 相对误差 |
|---|---|---:|---:|---:|
| SQLite原源 | default | 1.966026 | 1.730721 | -11.9686% |
| SQLite原源 | prepare | 1.966026 | 1.724872 | -12.2661% |
| XZ新源 | default | 1.822067 | 2.571866 | +41.1510% |
| XZ新源 | prepare | 1.822067 | 1.759488 | -3.4345% |
| Marian新源 | default | 4.657838 | 5.538989 | +18.9176% |
| Marian新源 | prepare | 4.657838 | 5.456760 | +17.1522% |

### 4.3 退休 cache miss：绝对计数和不完整证据误差

以下是 `MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS` 对应的 `retired_cache` 已知子集。它们不是 `L2_RQSTS.*`、旧 `perf_l2_demand_misses` 请求事件，也不是 native tag miss。unknown 是尚不能判定资格的宏指令数量；其结果未知，不能按零处理。各事件均为 **incomplete-evidence、hardware equivalence unverified**。

| 来源/模式 | 退休事件 | gem5已知计数 | FastSim已知计数 | 暂定相对误差 | 绝对计数差 | gem5 unknown | FastSim unknown |
|---|---|---:|---:|---:|---:|---:|---:|
| SQLite/default | L1 miss | 71,887 | 74,425 | +3.5305% | 2,538 | 580,973 | 2,562,324 |
| SQLite/default | L2 miss | 71,165 | 73,656 | +3.5003% | 2,491 | 588,276 | 2,562,324 |
| SQLite/default | L3 miss | 70,992 | 73,465 | +3.4835% | 2,473 | 588,294 | 2,562,355 |
| SQLite/prepare | L1 miss | 71,887 | 74,425 | +3.5305% | 2,538 | 580,973 | 2,562,324 |
| SQLite/prepare | L2 miss | 71,165 | 73,655 | +3.4989% | 2,490 | 588,276 | 2,562,324 |
| SQLite/prepare | L3 miss | 70,992 | 73,465 | +3.4835% | 2,473 | 588,294 | 2,562,355 |
| XZ/default | L1 miss | 79,099 | 74,981 | -5.2061% | 4,118 | 903,679 | 2,642,889 |
| XZ/default | L2 miss | 35,719 | 34,729 | -2.7716% | 990 | 1,094,945 | 2,642,907 |
| XZ/default | L3 miss | 30,028 | 30,117 | +0.2964% | 89 | 1,097,561 | 2,644,750 |
| XZ/prepare | L1 miss | 79,099 | 75,685 | -4.3161% | 3,414 | 903,679 | 2,642,886 |
| XZ/prepare | L2 miss | 35,719 | 35,194 | -1.4698% | 525 | 1,094,945 | 2,642,907 |
| XZ/prepare | L3 miss | 30,028 | 30,117 | +0.2964% | 89 | 1,097,561 | 2,645,112 |
| Marian/default | L1 miss | 54,222 | 54,827 | +1.1158% | 605 | 2,192,349 | 3,052,266 |
| Marian/default | L2 miss | 15,795 | 16,041 | +1.5575% | 246 | 2,571,077 | 3,052,266 |
| Marian/default | L3 miss | 13,681 | 13,742 | +0.4459% | 61 | 2,571,191 | 3,052,326 |
| Marian/prepare | L1 miss | 54,222 | 54,839 | +1.1379% | 617 | 2,192,349 | 3,052,264 |
| Marian/prepare | L2 miss | 15,795 | 16,041 | +1.5575% | 246 | 2,571,077 | 3,052,266 |
| Marian/prepare | L3 miss | 13,681 | 13,742 | +0.4459% | 61 | 2,571,191 | 3,052,326 |

较小的已知子集误差不能覆盖数十万至数百万的unknown；不得把该表提升为完整cache PMU或真机perf精度。完整九项退休事件、退休load/store计数、coverage以及独立原生请求诊断保存在每组维护报告中。

| 来源 | default报告 | prepare报告 |
|---|---|---|
| SQLite | [CPI/native](../tmp/cpi-three-workload-repair-20261021/validation-sqlite-final-default/summary.md)、[退休cache](../tmp/cpi-three-workload-repair-20261021/validation-sqlite-final-default/retired-cache-summary.md)、[退休人口](../tmp/cpi-three-workload-repair-20261021/validation-sqlite-final-default/perf-retirement-summary.md) | [CPI/native](../tmp/cpi-three-workload-repair-20261021/validation-sqlite-final-prepare/summary.md)、[退休cache](../tmp/cpi-three-workload-repair-20261021/validation-sqlite-final-prepare/retired-cache-summary.md)、[退休人口](../tmp/cpi-three-workload-repair-20261021/validation-sqlite-final-prepare/perf-retirement-summary.md) |
| XZ | [CPI/native](../tmp/cpi-three-workload-repair-20261021/validation-xz-final-default/summary.md)、[退休cache](../tmp/cpi-three-workload-repair-20261021/validation-xz-final-default/retired-cache-summary.md)、[退休人口](../tmp/cpi-three-workload-repair-20261021/validation-xz-final-default/perf-retirement-summary.md) | [CPI/native](../tmp/cpi-three-workload-repair-20261021/validation-xz-final-prepare/summary.md)、[退休cache](../tmp/cpi-three-workload-repair-20261021/validation-xz-final-prepare/retired-cache-summary.md)、[退休人口](../tmp/cpi-three-workload-repair-20261021/validation-xz-final-prepare/perf-retirement-summary.md) |
| Marian | [CPI/native](../tmp/cpi-three-workload-repair-20261021/validation-marian-final-default/summary.md)、[退休cache](../tmp/cpi-three-workload-repair-20261021/validation-marian-final-default/retired-cache-summary.md)、[退休人口](../tmp/cpi-three-workload-repair-20261021/validation-marian-final-default/perf-retirement-summary.md) | [CPI/native](../tmp/cpi-three-workload-repair-20261021/validation-marian-final-prepare/summary.md)、[退休cache](../tmp/cpi-three-workload-repair-20261021/validation-marian-final-prepare/retired-cache-summary.md)、[退休人口](../tmp/cpi-three-workload-repair-20261021/validation-marian-final-prepare/perf-retirement-summary.md) |

### 4.4 原生请求诊断：保留明显退化

以下为维护验证器直接评分的 workload-task Ruby/SLICC tag miss；与上表退休事件人口不同，不能互换。这些是原生请求诊断，不是真机perf退休指标。

| 来源/模式 | 原生诊断事件 | gem5 | FastSim | 相对误差 |
|---|---|---:|---:|---:|
| sqlite/default | L1D tag miss | 73,007 | 74,885 | +2.5724% |
| sqlite/default | private-L2 tag miss | 71,749 | 74,039 | +3.1917% |
| sqlite/default | LLC tag miss | 71,570 | 73,465 | +2.6478% |
| sqlite/prepare | L1D tag miss | 73,007 | 75,796 | +3.8202% |
| sqlite/prepare | private-L2 tag miss | 71,749 | 73,698 | +2.7164% |
| sqlite/prepare | LLC tag miss | 71,570 | 73,465 | +2.6478% |
| xz/default | L1D tag miss | 191,072 | 179,783 | -5.9082% |
| xz/default | private-L2 tag miss | 87,644 | 126,514 | +44.3499% |
| xz/default | LLC tag miss | 81,625 | 80,243 | -1.6931% |
| xz/prepare | L1D tag miss | 191,072 | 532,369 | +178.6222% |
| xz/prepare | private-L2 tag miss | 87,644 | 86,648 | -1.1364% |
| xz/prepare | LLC tag miss | 81,625 | 80,243 | -1.6931% |
| marian/default | L1D tag miss | 504,084 | 397,473 | -21.1495% |
| marian/default | private-L2 tag miss | 192,051 | 331,579 | +72.6515% |
| marian/default | LLC tag miss | 189,754 | 200,901 | +5.8744% |
| marian/prepare | L1D tag miss | 504,084 | 1,103,300 | +118.8723% |
| marian/prepare | private-L2 tag miss | 192,051 | 223,601 | +16.4279% |
| marian/prepare | LLC tag miss | 189,754 | 200,901 | +5.8744% |

**XZ prepare 的 L1D 原生 tag miss 为 +178.62%，Marian prepare 为 +118.87%。CPI接近不能掩盖这个诊断退化，因此prepare不能推广为全局默认，prepare/真实请求归属仍需排查。** 目前没有同序号完整prepare请求配对，不能仅凭这张表断言是重复计数、驱逐还是准入交错中的哪一项。

新退休资格的基础人口独立核对如下，default/prepare都与gem5完全相同；这证明功能宏单位一致，不证明服务来源和硬件perf资格完整。

| 来源 | gem5 = FastSim 退休loads | gem5 = FastSim 退休stores |
|---|---:|---:|
| sqlite | 7,192,190 | 3,100,343 |
| xz | 5,212,057 | 3,422,361 |
| marian | 5,929,626 | 3,067,813 |

### 4.5 XZ默认退化的追加消融

在同一份新XZ FST上，仅给旧语义reader控制增加branch-origin补丁，CPI从1.490234854变为1.492567169，参考1.081955043，相对误差+37.9509%、绝对误差0.410612126 cycles/macro；最终组合default为1.527190422。分支这一步仅增加0.002332314 CPI，剩余组合增加0.034623253 CPI。

因此主要退化不能归因于branch-origin；该顺序消融也不能把剩余唯一归给预取分类。新的功能分类已经阻止hint获得普通load的退休资格和store-forwarding语义，但保守默认时序仍等待完整服务，尚未完成的ack/fill链不能靠恢复错误分类来掩盖。两项之间的精确因果份额还需进一步隔离。详见[同源消融与输入证明](../tmp/cpi-three-workload-repair-20261021/branch/xz-new-source-origin-ablation.json)。

## 5. 验证与剩余工作

最终组合构建和完整 `fastsim_tests` 通过，另有 51 项 Python 回归、退休 PMU、cache runtime 与 cache integration 三个测试可执行程序通过。测试覆盖 v1/v2 兼容、损坏来源拒绝、hint 退休与原生请求人口分离、重复 ROI、分支发现阶段、store 驻留请求资格及提前确认负例。

[最终独立审核](../tmp/cpi-three-workload-repair-20261021/review/final-integration-review.md)未发现新的阻断项；确认两个 P1 分别被修复及隔离。它另行链接根库运行完整 request-role C++ 回归，并执行 31 项针对性 Python 检查；这些与上述测试有重合，不叠加成新的独立测试数量。

还没有完成：SQLite 的实际 cache 准入传播、软件预取在线请求及回调归属、精确 store canWB/permission/端口时序。错误路径资源和 DRAM refresh 长尾也没有在本轮被修复。没有新的吞吐量测量；验证任务耗时不能当 FastSim 吞吐。

本轮是第一批有回归和真实输入验证的修复；源门禁通过不代表达到 CPI 阈值或真机 perf 等价认证。未提交或推送代码，原基线与失败反例均保留。

最终存储快照：全项目 FST 为 52,907,543,152 bytes（52.91GB），低于100GB上限；空闲443.43GB。完整六组结果、独立identity、最终源码未变检查及限制见[机器验证收据](../tmp/cpi-three-workload-repair-20261021/verification.json)。
