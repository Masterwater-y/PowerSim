# Thread-state active/elapsed 首片实施与四核验证

本文为上一轮历史首片结果。后续已实现独立 recorded scheduler、实际 BLOCKED 证据
和认证共同终点；新四核源可以比较 task-onCPU active CPI，见
[后续实施结果](recorded-scheduler-results-20261013.md)。旧源仍按本文保持 proxy，
不会因为新代码存在而自动变成完整计数；严格 perf 和新源 PMU 精度仍未认证。

本次完成 gem5/libgomp 功能事实输出、FastSim ownership 消费和双周期报告的首片。真实四核探针已通过源文件、owner、任务 macro 分母和事件消费验证。严格 active CPI 仍不可用；此次不是 TeaLeaf 精度重测，也不是正式 PMU/吞吐量认证。

设计见 [thread-state active/elapsed](thread-state-active-elapsed-design-20261013.md)。编译、记录和回放未改变模型延迟参数。gem5 本次重编译遵循用户新规则：使用可用核心数的 80%，192 核对应 `-j153`，显式使用 GCC11 和 Python 3.11。

## 实际采集与回放

探针源码为 `tests/workloads/thread_state_fs_probe.c`，静态链接带 identity/wait/wake marker 的 libgomp；四线程固定 CPU0..3。参数为 1000 轮、每轮 work=2000，实际窗口由最快核达到 300000 measured user UOPs 关闭，未要求执行完整 1000 轮。采集保留 user-plus-kernel、buffer114、全 LRU，公共起止为 serial workbegin 与 first-core target。KVM quantum=1000us；创建和绑核在采集前完成，未标记 startup futex 与 5ms prefix 用于避开异步 KVM 切换。

有效输出目录：`tmp/thread-state-active-elapsed/fs-probe-kernel-retention/`。

- `capture-command.json`、`capture-receipt.json`：采集退出码0，未触发磁盘门或 fatal 检测。
- `tao_trace/thread-state-metadata-core*.json`：四核 read/identity/unretained-hook 错误均为0。
- `thread-state-summary.json`：4核、4任务、127个事件，FST header/core/count 校验通过；15次正常 wait return、3次 EAGAIN，无 pending request。
- `tao_trace/oracle/thread-state-accounting.json`：独立观测审计通过，记录时间只用于此离线审计。
- `replay/{manifest.txt,state.cfg,thread-state.plan,stats.json}`：FastSim 消费全部127事件，measurement 的104事件全部消费；源文件直接引用，没有复制 FST。
- `matching-model-parameters.json`：实际 config.ini 与所用冻结模型原始采集配置的3221个几何/时序字段一致。
- `mechanism-validation.json`：任务宏分母一致、每核周期分区守恒、primary CPI 不可用门通过。

共保留 2,384,449 条 raw records，其中 measurement 821,492 条、442,090 条完成 macro；被测四任务的 macro 数为415,492，两端独立计算一致。新增有效 FST 为152,610,528 bytes；保留既有 TeaLeaf 源和本轮诊断输出后的合计15,712,072,312 bytes，低于20,000,000,000 bytes。

## 时间报告与可评分范围

| 输出 | gem5 | FastSim |
|---|---:|---:|
| 公共 elapsed reference cycles | 124,083 | 100,382 |
| elapsed time | 41.319639 us | 33.427206 us |
| 四任务 active cycles proxy，跨核求和 | 337,382 | 301,233 |
| 其他上下文 cycles proxy，跨核求和 | 158,950 | 29,286 |
| unknown cycles proxy，跨核求和 | 0 | 71,009 |
| workload-task macro instructions | 415,492 | 415,492 |
| 严格 active CPI | 不可用 | 不可用 |
| CPI 绝对误差，cycles/macro | 不可评分 | 不可评分 |
| CPI 相对误差 / CPI MAE | 不可评分 | 不可评分 |

elapsed 的相对差为 -19.1009%，属于此小探针的运行时间诊断。跨核 active 的和与单一 elapsed 含义不同，不能直接相除得到任务 CPI。FastSim 每核的 active-proxy + other-context-proxy + unknown-proxy 都等于公共100,382 reference cycles；71,009 unknown 来自各核完成时间不齐的公共尾部，未把最后 owner 外推到终点。

当前 kernel switch hook 的 state 可与 wakeup 竞争，实际输出为20个 RUNNABLE、16个 UNKNOWN switch、0个可证明 BLOCKED switch。15个 pending wait 与 wake 的关联只用于诊断，0条 proven block/wake admission，0条已确认 futex wake cause。源顺序与 ownership 被正确消费，但这还不能证明 FastSim 对阻塞恢复的跨核因果时序建模完整。两端的严格 active cycles/CPI 都保持 null；FastSim 的未知尾部也使代理 active 比值不能作为相同覆盖范围的 CPI 精度比较。

PMU 未评分。需要通过维护的 native Ruby/SLICC validator，以及独立 target identity 与 scope/coverage 门后才可报告误差；本次没有从 kernel-events 代理字段取 cache truth。首片实施时尚未测量吞吐量；同日后续重复测量见下文，采集日志 wall time 和审计耗时不用于吞吐量。

## 后续吞吐量测量与 scheduler 评估

针对用户关于当前吞吐量与两阶段适配的追问，使用当前 binary SHA256 `5c118a5070851c112b1fc72538a20c91534744ca7792ee39a0b8e975e7247be7` 补测。产物为 `tmp/scheduler-feasibility-20261013/{identity.json,commands.json,runs.json,summary.json}`。全部为 C4 native user-plus-kernel，串行运行、固定 host affinity0..7；目标核数仍为4。共享主机没有独占保留，起始 load average0.32/0.28/0.17，结束2.76/1.49/0.68。下表是诊断样本分布，单位 million measured user UOP/s，不是跨负载正式矩阵。

| 输入与模式 | 重复 | Mean | P50 | P90 | P99 | Min |
|---|---:|---:|---:|---:|---:|---:|
| 本节四核探针，thread-state OFF | 5 | 6.200 | 6.208 | 6.234 | 6.239 | 6.133 |
| 同一探针，thread-state ON | 5 | 3.240 | 3.247 | 3.261 | 3.265 | 3.208 |
| 既有 TeaLeaf C4，functional barrier sync ON | 3 | 5.838 | 5.830 | 5.867 | 5.876 | 5.808 |

探针 OFF/ON 交错换序执行；同一模式各次 scope predictions 一致。探针每次测量672,798 user UOP、821,492 mixed UOP，warmup1,562,957 records；OFF/ON 测量墙钟中位数0.108374/0.207204秒，含 warmup 的内部总墙钟中位数0.512117/0.637892秒。ON 吞吐中位数下降47.6971%；这只度量当前门控实现对这个短探针的影响，不能外推为未来 scheduler 的固定开销。

TeaLeaf 每次测量38,964,790 user UOP、39,150,412 mixed UOP，warmup201,548,375 records；测量墙钟中位数6.683254秒，含 warmup 的总墙钟中位数56.951358秒。三次使用既有 FST，无新增采集或复制，target identity重新验证valid1/1。此路径开启的是已有 barrier sync，不能当成 thread-state ON/OFF 对照。native warmup 未按 privilege 分区，因此不计算 end-to-end user-UOP/s。

探针首组 OFF→ON 的测量 interval_steps 为98→247，active prefixes322→709，chunks203→263；schedule-batch计时18.596→109.945ms。这支持将事件截断与批量退化作为后续优化重点，但不是对全部开销的独立因果分解。未来 scheduler 应区分可批处理的观察/记账事件和必须阻止后续执行的因果事件，避免所有 marker 都触发同样的 producer 交接。

## 审核发现与修复

真实失败源 `fs-probe-ruby-proof` 正常达到公共终点，却在三核 measurement 首次 runtime 事件处发生 owner0 与任务PID冲突。对应 FST 中也没有 idle→worker 的 switch hook。原因是旧 `emitMicroRecord` 在 CPL 尚未初始化或处于 IDLE 时过滤 measurement 内核指令，删除了恢复路径。修复仅在 thread-state 开启时保留完整内核指令流，包含 hook 的全部微操作；默认关闭行为保持。修复后新源所有 unretained-hook 计数为0，严格 compiler 与独立 auditor 均通过，没有补推缺失事件或放宽 owner 检查。

其他修复包括 kernel/profile SHA 分离、固定物理核的 owner 校验、last-micro 边界、warmup owner 继承、terminal admission 计时、公共尾部 unknown、独立任务 macro 分母，以及 read/identity/unretained 错误计数非零时拒绝输入。候选 pending wait 不能创建跨核 admission 边，EAGAIN、自唤醒、RUNNABLE/UNKNOWN 不能伪造成 BLOCKED 因果链。

默认 C++ 构建及 `fastsim_tests` 通过。Python全套303 tests：0 failures，3个既有环境错误（缺 fastsim_py、两个外部 TCSim checkpoint wrapper），5 skips；其中 graph26、dual-time18、ownership audit11、native报告11、runtime4、旧同步3、profile8通过。真实源 RED→GREEN 验证独立于这些合成测试。

## 剩余限制

首片仅支持完整 physical sources、固定任务核、静态频率的 native user-plus-kernel run；`sync.plan` 与 `thread_state.plan` 组合仍拒绝。迁移、PID复用/namespace、IRQ精确归属、严格 perf scheduler cut 和 futex 唤醒 cause 未补齐。QEMU/DynamoRIO adapter 尚未实现，但功能协议不依赖 gem5 的记录时钟。

`fs-probe-prefix` 曾在 kernel PML4 功能读处出现 Ruby fatal，同一配置未修改 Ruby 时的后续复现和本次新源正常结束，说明这是尚未定位的间歇性问题。没有使用 backing-store fallback 或推测内核偏移掩盖失败；采集监控发现 fatal/panic 后终止该失败进程。后续需要失败调用栈与 Ruby permission/buffer 证据，不能把此次正常完成当成 Ruby 问题已修复。
