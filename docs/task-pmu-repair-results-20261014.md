# Task PMU 范围修复与四核验证

10-15 [private-L2 低估定位](private-l2-pmu-underprediction-diagnosis-20261015.md)进一步
确认旧 native L2 上报桶存在事件定义不一致。本文原始数字保留，但“私有 L2
低估是模型偏差”的归因不完整；范围/来源门禁不认证 FastSim 字段的事件等价性。

后续 [perf 语义与单位审计](perf-pmu-semantic-audit-20261014.md)已确认：本文的原生
tag-miss 诊断不能替代 `perf_*`。新 task PMU 尚不含这些退休/请求事件；旧物理
`perf_*` 也存在单位、转发、atomic 与 store 覆盖缺口，不能据本文宣称真机 perf 对齐。

本次修复了完整物理指令流与旧 PMU 参考范围不同、导致 PMU 无法比较的问题。
新四核探针已通过逐核人口审计、原生 Ruby/SLICC 守恒、独立目标身份及维护的
native validator，PMU scored cases=1/1。修复的是采集覆盖与统计归属；没有调节
目标机时序参数，私有 L2 等模型误差仍然存在。

依据：[设计](superpowers/specs/2026-10-13-task-pmu-repair-design.md)、
[实施计划](superpowers/plans/2026-10-13-task-pmu-repair.md)。此前
[recorded scheduler 报告](recorded-scheduler-results-20261013.md)中的旧源及其
physical/legacy PMU 不会自动变成有效任务 PMU；本次重新采集了完整证据。

## 范围与实现

完整物理 FST 继续回放，用户、内核、idle、其他任务都影响硬件状态。新增
`scope_metrics.task_pmu`，主范围为 `workload-task`，归属合同为
`task-oncpu-switch-entry-v1`，与 active CPI 使用相同 TASK。原
`scope_metrics.pmu` 物理流诊断保持原名和行为，不把 idle 加进旧 PMU 定义。

gem5 从真正共同起点开始独立收集逐 owner 的退休、原始 BPred 和原生 cache
hierarchy 证据，不等待每核第一次用户指令。TASK 身份可以晚于窗口起点出现，
因此先按 owner incarnation 累计，最终再选择已确认的 TASK。
switch-entry 锚点仍属于旧 owner；原生 memory identity 只消费一次，晚返回沿用
冻结的 owner。旧 CPL 范围和新增完整范围独立守恒。

FastSim 只消费原有无时间 thread-state 功能计划，在 owner 更新前冻结记录归属，
并随访存请求跨 preview、weave、store publication 与 rollback 保留。新增 full/
task/other 三份计数随 `CoreCounters` 一起快照和恢复，接受的请求只累计一次。
非 TASK 请求照常改变 cache/predictor 状态；I-side、PTE、prepare、follower 等
不冒充 data-demand tag miss。private-preview 的真实 L2 hit 也补进新范围的 L1
tag miss，旧物理桶及目标时序不变。

gem5 的 oracle-only `task-pmu-coreN.json` 提供逐 owner 的退休/branch/native
hierarchy、共同边界、source ordinal 范围与失败/drain 诊断。独立
`tools/audit_task_pmu.py` 扫描当前 FST 并按功能 owner 重算人口；验证器逐核比较
task 与 other 的 macro/UOP/memory/branch 数，检查八个计数的 task+other=full、
逐核和聚合守恒。缓存参考只来自原生 Ruby/SLICC，未使用 path-class proxy。

当前 gem5 x86 syscall 在 FST 中折叠为一个 marker；退休计数按已验证的
25 UOP/1 control UOP 定义展开。此转换仅用于 PMU 退休人口，不添加模型指令或
延迟。若共同起点/终点截断了折叠的 syscall，独立人口审计会拒绝该输入；不能
无条件给 gem5 实际退休数补 24 以强行通过。

## 实际四核输入

case：`tmp/task-pmu-repair/fs-probe-task-pmu/`。
负载仍为 `tests/workloads/thread_state_fs_probe.c`，参数 `1000 2000`，4 线程固定
核；最快核达到 300000 measured user UOP 后关闭共同窗口。实际测量包含：

| 项目 | 数量 |
|---|---:|
| user / mixed FST UOP | 672085 / 843932 |
| physical macro | 454188 |
| TASK macro / native-expanded TASK UOP | 424730 / 776926 |
| TASK memory UOP / branch | 53842 / 80208 |
| other macro / memory UOP | 29458 / 16333 |
| warmup mixed UOP / macro | 1612040 / 895317 |
| 功能事件 / 实际 BLOCKED / admission | 132 / 18 / 15 |

两端逐核的上述四项任务人口完全一致。UNKNOWN switch=0、owner unknown=0、
read/identity/unretained-hook 错误均为0、pending native=0，原生 hierarchy 完整，
target drain 完成。22 个 wake cause 仍未确定，不影响已证明的 owner 计数；没有
将其伪造为已知 futex 唤醒来源。

目标身份 1/1 valid：实际 `config.ini`、canonical profile 与 effective target
匹配。buffer114、全 LRU、3GiB/2-channel、实际333ps时钟；冻结的配置 include chain
保持不变。gem5 按192个可用核心的80%，使用 `-j153`、GCC11/Python3.11 编译，
嵌入式 Python smoke 通过。

### 后续参数复核：不能声称全参数已对齐

用户追问后，直接比较实际 gem5 `config.ini` 与 FastSim 保存输出的
`configuration`，确认四核均为 buffer TSO、buffer/effective SQ=114、LQ=32、
ROB=192、IQ=64，各流水宽度=8。L1I/D各32KiB/8-way、私有L2各1MiB/8-way、
LLC八个8MiB bank共64MiB/16-way、LRU/64B line及实际333ps参考时钟相符。
FastSim `configured_sq_entries=32` 是被buffer模式覆盖前的配置值；运行使用
`effective_sq_entries()=114`，不是实际按32项运行。

但DRAM时序没有全部对齐。gem5 ps转换为333ps参考周期并向上取整后，
tCL/tRCD/tRP=43，与FastSim一致；tRAS=97、tRTP=23、tRRD=11、tRRD_L=15、
tXAW=64、tCCD_L=16、tCS=6，而本次FastSim对应值均为0、未开启这些约束。
完整refresh与Ruby时序等价性也没有实现/认证。参见
[参数覆盖说明](gem5-parameter-coverage.md)。

因此，之前的identity 1/1 valid只证明**采集端 config/effective-target/profile 的
身份一致**，并不证明FastSim全参数或全机制一致。本报告的CPI/PMU数字可复现，
归属/事件来源门控有效，但仍是当前部分对齐配置下的诊断，误差可能同时包含参数
映射缺口与模型近似，不能全部归因于模型实现。此次复核没有修改配置或重新仿真；
原始对照见 `tmp/task-pmu-repair/parameter-audit.json`。

## PMU 误差

范围均为上述 workload-task；这是一组新四核探针的 gem5 语义诊断，不是硬件
perf 认证，也不能与旧物理流 PMU 直接比较。

| 事件 | gem5 native/BPred | FastSim | signed count error | signed relative error | APE |
|---|---:|---:|---:|---:|---:|
| Branch misses | 868 | 893 | +25 | +2.8802% | 2.8802% |
| L1D tag misses | 442 | 331 | −111 | −25.1131% | 25.1131% |
| Private-L2 tag misses | 290 | 61 | −229 | −78.9655% | 78.9655% |
| LLC tag misses | 26 | 32 | +6 | +23.0769% | 23.0769% |

每个事件均 N=1、finite APE cases=1/1，所以各事件的 MAPE、WAPE、P50/P90/P99
均等于本行 APE，signed bias 等于本行 signed relative error；不跨事件计算一个
混合平均。Branch/L1D/private-L2 的字典映射仍为 `proxy`，LLC 为 `diagnostic`。
私有 L2 低估明显，是需要后续机制分析的模型偏差；此次通过范围门控不代表
达到了 PMU 精度目标。

原始计数、参考来源、映射、MAPE/WAPE 和指纹位于
`tmp/task-pmu-repair/native-validation-final/{summary.json,summary.csv,summary.md}`，
逐 case 为 `cases/fs-probe-task-pmu/validation.json`。原生侧具体证据为
`tao_trace/oracle/task-pmu-core*.json` 和独立 `task-pmu-audit.json`。

## Active CPI 与 elapsed

| 模式 | gem5 active CPI | FastSim active CPI | 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| 修复后新四核源 | 0.804791 | 0.898957 | 0.094166 | +11.7006% |

N=1，CPI MAE=0.094166 cycles/macro。两边各自 TASK macro 分母均为424730；
gem5 active/other=341819/169013 reference cycles，FastSim=381814/31870，unknown=0。
gem5 elapsed=127708 cycles / **42.526764us**；FastSim=103421 / **34.439193us**，
绝对误差 **8.087571us**，相对误差 **−19.0176%**。

这份源的实际窗口和 TASK 人口不同于上一轮，不能把数值变化当作精度改进。
同一份新源对修复前/后冻结 binary 的对照证明：旧 physical PMU、core cycles、
active/elapsed 和退休工作量完全相同。本次没有修复 elapsed 的关键路径时序偏差。
`perf_equivalence=unverified` 保留。

## 吞吐、测试与空间

同源、相同配置，CPU0–7 固定 affinity，before/after 串行交替，各5次。
共享宿主未独占，单次测量约0.17s，以下仅为新增统计的开销诊断：

| binary | mean M user-UOP/s | P50 | P90 | P99 | min |
|---|---:|---:|---:|---:|---:|
| 修复前 | 3.96566 | 3.96156 | 3.99742 | 4.01422 | 3.93611 |
| 修复后 | 3.88583 | 3.89615 | 3.92283 | 3.93703 | 3.84305 |

P50 变化 **−1.65%**；整体 wall P50 0.61944s → 0.62274s。
正式吞吐取 `scope_metrics.throughput.user_uops_per_second`；native warmup 未按
privilege 分割，所以不报告端到端 user-UOP/s。命令、全部原始运行及 SHA 在
`tmp/task-pmu-repair/performance/`。模型目标计数在全部重复中一致。

最终默认 C++ build 和 `fastsim_tests` 通过。新机制测试实际触发并验证：
kernel prefix、after-record owner、非 TASK cache warming、syscall 展开、4个 PTE
及 I-side 请求排除、TSO role2 跨 switch 归属、真实多 pass retry/fallback rollback，
以及 private-preview L2-hit 漏计修复的 RED/GREEN。独立审查没有剩余阻塞项。

Python 全套最终 **331项**：323 passed、5 skipped、3个既有环境错误
（缺 fastsim_py 及两个外部 TCSim wrapper），无断言失败。新 collector/auditor/
scorer 机制回归通过，涵盖旧 audit 不变而功能源变化、逐 owner 分错、缺原生证据、
不完整 drain、syscall 部分截断和不同 PMU 范围禁止混合汇总。

新 FST 为 **157188000 bytes**，低于300MB；`tmp/` 总 FST 低于20GB，最终精确值
为 **18645591160 bytes（18.65GB）**，记录在 `tmp/task-pmu-repair/evidence.json`。
最终 FastSim SHA256 为 `7655662232c388232617ed2ea35f25a191071eaf82562f5fe6ddd332c272f05b`，
冻结在 `tmp/task-pmu-repair/final/fastsim`。没有复制 TeaLeaf 大 FST，没有提交或推送。
