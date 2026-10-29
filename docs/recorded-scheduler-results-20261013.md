# Recorded scheduler 首片实施与四核验证

后续已完成新的 workload-task PMU 采集与统计范围修复，并重新采集四核源验证，见
[task PMU 修复结果](task-pmu-repair-results-20261014.md)。本文旧源的 physical/legacy
PMU 人口不匹配结论保持不变；不得拿后续新范围数字直接替换本文旧参考。

本次完成 fixed-C4 recorded scheduler、gem5 真实阻塞事实、认证公共终点的
task-onCPU 计数，以及现有 bound/weave 两阶段路径的接入。实施依据为
[设计](thread-state-active-elapsed-design-20261013.md)和
[实施计划](superpowers/plans/2026-10-13-recorded-scheduler.md)。这是四核小探针的机制和
开销实验；没有重新采集 TeaLeaf，也不代表大负载或严格硬件 perf 精度认证。

## 已实现的行为

- `RecordedScheduler` 分开保存逻辑任务状态、物理核 owner 与目标参考时钟计数。
  wakeup 只使任务 ready，switch-in 才改变 owner；任务阻塞不停止物理核及其他上下文。
  warmup 保留 owner，测量边界只重置计数。原生 scheduler 内核指令不重复收费。
- gem5 针对实际 kernel ELF 验证 17 处指令证据，输出实际 DEQUEUE_SLEEP 决策、
  futex 已入队和实际 selected waiter 事实。每次 `__schedule` 调用独立保留决策，
  不在容易与 wakeup 竞态的后续 switch-entry 重新猜测睡眠状态。
- 功能源保留原始 ordinal/PC/ASID/last-micro 锚点，功能计划没有源 tick。
  oracle 侧观察时钟仅用于独立 gem5 审计，FastSim 从自身校正退休时刻建立区间。
- 认证的完整 kernel stream、初始 owner、共同终点和零采集错误，允许已知 owner
  覆盖公共尾部。旧源缺少认证时仍保留 unknown，不自动升级为可用 active。
- `sync.plan` 和 `thread_state.plan` 可以组合；统一图检查 source bounds、事件域、
  source 顺序和 DAG，拒绝重复歧义边界。仍使用既有 bound/weave/feedback。
- 仅观察的 identity/wait/wake-attempt/return 事件在证据和依赖允许时不暂停 producer；
  真正的 switch、wakeup、functional-sync 及必要跨核前驱仍保留执行门控。
  所有原始锚点继续验证；不是丢弃 observation 事件。

计数定义为 `task-oncpu-switch-entry-v1`：在经过审计的 switch-entry 指令最后一个
已退休 micro-op 后切换 owner，用户、内核和 IRQ 区间跟随这个 owner。
这是 task onCPU 参考周期定义，`perf_equivalence=unverified`。未知 wake cause 不会
抹去已经证明完整的 owner 区间；它也不会被伪造为已知 futex 唤醒来源。
compiler/auditor 摘要中的 `strict_active_scope` 表示该计数合同的 owner 覆盖完整，
不表示 perf 硬件事件认证；判断 perf 等价性必须读取独立的 `perf_equivalence`。

## 实际输入与采集

输出目录：`tmp/recorded-scheduler/fs-probe-scheduler/`。
负载为 `tests/workloads/thread_state_fs_probe.c`，4 线程固定核，参数 `1000 2000`；
最快核达到 300000 measured user UOP 后关闭所有核的共同窗口。
KVM quantum=1000us，创建/绑核与未标记 startup futex 在测量前完成。
目标为 3GHz 请求值（实际 333ps 周期）、3GiB/2-channel、buffer114、全 LRU。
配置和最终目标身份由实际 `config.ini` 生成并单独验证，1/1 case valid。
模型复用冻结配置及 include chain，没有引入或调节延迟参数；既有时序近似仍存在。

| 采集事实 | 结果 |
|---|---:|
| 参与核 / 任务 | 4 / 4 |
| 功能事件 | 132 |
| 实际 BLOCKED / RUNNABLE switch | 18 / 22 |
| UNKNOWN switch | 0 |
| 已证明 block→wake→resume admission | 15 |
| 正常 wait return / EAGAIN / pending | 15 / 3 / 0 |
| read / identity / unretained-hook 错误 | 各核均 0 |
| 未知 wake cause / 已证明 causal wake source | 22 / 0 |
| warmup mixed UOP / macro | 1560485 / 866877 |
| measured user UOP / mixed UOP | 665849 / 821301 |
| physical macro / TASK macro | 442066 / 414003 |

kernel SHA256 为
`da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174`；
profile SHA256 为 `e8a734db455b808a0721a325bd42e9b5f3c23bc5e13b135401a4a276cbee293f`。
实际睡眠决策返回点 `0xffffffff81517851`、入队后点 `0xffffffff810a021a`、
selected waiter 点 `0xffffffff8109f97b`；这些地址只对该精确构建有效。
gem5 使用可用 192 核的 80%，即 `-j153`，GCC11/Python3.11 构建和嵌入式 smoke 均通过。

## Active CPI 和 elapsed

两端使用相同的四个 TASK（1190–1193）、相同公共窗口和各自的 TASK macro 分母；
两边独立计得 414003 macro。gem5 active=334141、other=166107 reference cycles；
FastSim 默认批处理 active=376198、other=30534。各核 active+other+unknown 对各自公共
elapsed 守恒，unknown 均为 0。多核 active 求和；elapsed 不求和。

| 同源模式 | gem5 active CPI | FastSim active CPI | CPI 绝对误差 cycles/macro | CPI 相对误差 |
|---|---:|---:|---:|---:|
| 默认观察批处理 | 0.807098 | 0.908684 | 0.101586 | +12.5866% |
| 所有事件均门控的消融 | 0.807098 | 0.913646 | 0.106548 | +13.2013% |

每行各为同一 case 的一种模型模式，默认模式 N=1，CPI MAE=0.101586 cycles/macro；
消融模式 N=1，MAE=0.106548。重复吞吐运行的目标计数一致，不能把重复运行当作多个
独立精度 case。批处理会改变两阶段分块，目标预测并非逐位不变的宿主优化。

默认模式 gem5 elapsed=125062 reference cycles / **41.645646us**；
FastSim elapsed=101683 / **33.860439us**，绝对误差 **7.785207us**，相对误差 **−18.6939%**。
这说明计数完整性已经补齐，但现有目标时序模型仍有误差；本次没有为缩小误差调参。

数值由维护的 `time_accounting` 和 native validator 生成，见
`tmp/recorded-scheduler/native-validation-final/cases/fs-probe-scheduler/validation.json`。
active CPI 可比较不等于严格 perf cycles 已认证。

## PMU 范围检查与未完成的精度认证

维护的 native Ruby/SLICC 审计通过结构守恒、完整 hierarchy 和 target drain；
缓存参考没有读取 TaoTrace path-class 字段。不过，本次 PMU **不出误差数字**：
完整物理源有 442066 macro，旧 user-plus-kernel native 参考只有 433160。
差额 8906 中，1542 为旧 oracle 排除的 idle macro；其余 7364 是各核第一次用户
指令前的内核前缀，`accountCplCommit()` 原本在初始化前跳过这些提交。
完整 owner 采集必须保留它们，因此不能继续沿用旧参考的人口范围。

新增 `pmu_user_instruction_population_match` / `pmu_instruction_population_match`
检查。该 case 仅 combined population 检查失败，`pmu={}`、
`pmu_validation.status=source-gate-failed`，PMU scored cases=0；整体 validator 退出 1。
CPI 和 elapsed 按各自证据独立汇总，保留 active CPI case=1 和对应 MAE。
“native 守恒通过”不能替代这项范围认证。

早期输出 `tmp/recorded-scheduler/native-validation/` 保留排查记录，其中 PMU 数字因
缺人口门控无效，已由 `native-validation-final/` 替代，不可用来判断模型精度。
补齐 PMU 需要原生参考从真正共同起点覆盖内核前缀，并输出与回放一致、可审核的
功能人口范围；不能删除必要 scheduler 指令或把未知/idle 项直接加进既有 PMU 定义。
这项 PMU 范围扩展尚未实施。

## 吞吐量与两阶段开销

同源、同 binary、固定 host affinity CPU0–7，串行交替运行，每种模式 5 次。
共享宿主没有独占预留，测量窗口只有约 0.11–0.22s；以下为小探针诊断，不外推 TeaLeaf。
正式单位为 `scope_metrics.throughput.user_uops_per_second / 1e6`。

| 模式 | P50 M user-UOP/s | mean | P90 | P99 | min | 测量 wall P50 s |
|---|---:|---:|---:|---:|---:|---:|
| 状态建模关闭 | 6.0558 | 6.0415 | 6.0943 | 6.1093 | 5.9690 | 0.10995 |
| 全 132 事件门控 | 2.9779 | 2.9766 | 2.9822 | 2.9832 | 2.9654 | 0.22360 |
| 默认 70 observation + 62 门控 | 3.9991 | 3.9851 | 4.0185 | 4.0298 | 3.9164 | 0.16650 |

默认相对全门控提升 **34.30%**，相对关闭状态建模下降 **33.96%**。
配置 `scheduler.batch_observations = false` 选择全事件门控消融；默认 `true`。
缺少完整认证的旧计划无论该开关如何设置，都保留原门控策略。
剩余开销仍包含 62 个真实执行边界及两阶段交接，不能把全部开销归因于状态表查询。
warmup wall P50 分别为 0.40590/0.43479/0.43001s；整体 wall P50 为
0.51586/0.65873/0.59648s。native warmup 尚未按 privilege 分割，因此不输出端到端
user-UOP/s；warmup wall 和整体 wall 单列。

旧计划兼容路径另做 5 对冻结 binary/当前 binary 运行，P50 为 2.97221/2.96107 M
user-UOP/s（−0.37%，共享宿主短窗口诊断）。两者 PMU、指令数、core cycle、旧 proxy
分区完全相同。关闭状态建模的真实源 counter 对照也逐值相同，不把这次 counter
对照当作额外吞吐测量。

最终 FastSim SHA256 为 `d02c6514a2d875992437ed91751fd1b46d294d6a34dea7adeec9732a1a3c2726`；
冻结在 `tmp/recorded-scheduler/final/fastsim`。
命令、25 次原始运行与摘要位于 `tmp/recorded-scheduler/performance/`，
counter-only 对照另存 `frozen-off-counter-check.json`。

## 审核、测试与空间

独立审核发现并修复：保存 audit 不能绕过当前 functional/FST 证据、跨核 observation
前驱时间检查、warmup 后 state event 总数被重置、可用 active 仍标 proxy、case-root
与 tao_trace 路径、PMU 人口门控，以及 PMU 失败误删独立 CPI/elapsed 汇总。
外层报告缓存也重新验证当前源；旧 audit 不变而 metadata 或非事件 FST macro 标记
变化时，旧 passed 报告不得复用。FST 证据绑定 size/mtime/ctime、header 和事件记录，
不是全文件内容哈希认证；搬迁或修改源后需要重新审计。

`cmake --build build -- -j16` 和 `fastsim_tests` 通过。Python 全套 **318 项**：
310 passed、5 skipped、3 个环境错误（缺 fastsim_py、两个外部 TCSim wrapper）；
无断言失败。scheduler 模块另有独立 ASAN/UBSAN 验证。

新采集 FST 合计 **152440096 bytes**；最终 `tmp/` 全部 FST 总计
**18488267536 bytes（18.49GB）**，低于 20GB。保留已有 TeaLeaf 源，没有复制大 FST。
汇总证据与配置 include chain hashes 见 `tmp/recorded-scheduler/evidence.json`。

仍不支持任意迁移/超额订阅、TID 重用与 namespace 完整生命周期、严格 perf
unhalted/IRQ 口径和所有内核 wake cause。QEMU/DynamoRIO 生产者尚未实现；本次协议
仍以功能身份与锚点为接口，没有把 gem5 的测得时间送入 FastSim。
