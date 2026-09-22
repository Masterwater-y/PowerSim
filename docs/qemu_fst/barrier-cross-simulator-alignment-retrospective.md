# Barrier 跨仿真器进度对齐失败复盘

## 结论

QEMU-FST 与 gem5 TaoTrace 曾尝试在 workload 中增加 OpenMP Barrier，以便让两个
producer 在同一软件进度边界结束多核采集。该方案没有建立可复用的跨仿真器
进度等价关系，且显著改变了被测程序的同步拓扑，因此不再作为生产采集合同。

现行主线恢复 `first-core-macro-common-end`：

- 两个 producer 都统计退休的 CPL3 architectural macro；
- 任意参与核先达到目标时，本 producer 内所有参与核共同结束；
- 其他核保留当时的真实宏、UOP 与 kernel population；
- 该边界只提供固定工作量的近似，不宣称两个 producer 到达相同算法阶段。

转换正确性与跨 producer 可比性分开验证。QEMU lowering 的语义正确性使用同一
raw 输入的全文件 FST parity；QEMU 与 TaoTrace 的独立执行结果只作为宏预算下的
双 producer 诊断与 FastSim 结果对比。

## 原始目标

固定宏或 UOP 预算不能证明多核 workload 的共享软件进度一致。Barrier 方案试图
让全部 worker 在 warmup、measurement begin 和 measurement end 处汇合，以获得
一个 guest-visible 的全核软件边界，并让 QEMU 与 gem5 使用同一 source marker。

实现先后包含：

- `ROI_START`、`ROI_MEASUREMENT`、`ROI_END` 三阶段 marker；
- worker participant/depth 计数与 per-core END 汇合；
- workload 内新增 OpenMP Barrier；
- gem5 checkpoint、KVM 到 O3 在线切换和 `WORKBEGIN/WORKEND`；
- QEMU plugin、gem5 probe 与 Python orchestrator 之间的多 owner shutdown；
- 为 checkpoint、aux disk、marker ELF 和 pilot 运行新增的资源准备路径。

这些机制逐步把采集边界、CPU 切换、worker 唤醒和进程退出耦合为一套状态机。

## 已证实的失败

### Barrier 改变被测执行

人为 Barrier 会新增原 workload 不存在的 futex、worker wakeup、rendezvous 和
kernel scheduling 因果边。gem5 O3 必须真实承担这些同步和内核执行成本，测到的
不再只是原 workload，而是 workload 加采集机制。

QEMU MTTCG 与 gem5 O3/Ruby 的调度和时间域不同。即使 guest marker 相同，两边
通过 Barrier 的路径与成本仍不等价，因此 Barrier 不能证明两个 producer 的
population 收敛。

### 相同 Barrier 仍未得到稳定人口

LBM C4 小窗口实验中，TaoTrace 可保持 bit-identical，而 QEMU MTTCG 的 aggregate
population 仍出现约 20--23% 波动。`--roi-y-rows=1` 的窗口还会被 libgomp worker
wakeup 延迟吞没，使部分核心在整个窗口内停留于 CPL0。

这说明 Barrier 可以控制生命周期，但不能消除 producer 时间域、OS 调度和 MTTCG
并发造成的人口差异。

### gem5 在线路径成本失控

把 checkpoint 或 CPU switch 放在 OpenMP worker 已进入 Barrier/futex 的位置，会
让 O3 从内核同步 continuation 接管。后续尝试把 handoff 移到串行位置、拆分
`switchcpu` 与 `WORKBEGIN`、增加 Atomic fast-forward 或重新制作 checkpoint，
虽然修正了部分 marker 语义错误，却没有让 Barrier 主线变得简单或可复用。

有界追踪最终证明 Ruby response 与四核 retirement 仍在推进；早期“永久丢失
kernel load response”的判断不成立。真实问题是 O3 执行额外 runtime、同步和
系统服务前缀非常昂贵，而不是需要继续修改 Ruby/LSQ。

### 生命周期 owner 过多

实验期间 shutdown 曾同时由 guest marker、TaoTrace probe、QEMU plugin 和 Python
orchestrator 决定，导致 `.capture-complete`、QMP quit、trace drain 与文件发布
相互竞争。后续虽将 shutdown 收敛到 orchestrator，仍无法消除 Barrier 本身对
执行的污染。

## 放弃的路径

以下设计不再进入主线：

- 为验证目的新增 workload OpenMP Barrier；
- per-core END、participant/depth 汇合和 `BarrierRoiController`；
- 用 checkpoint 或多 CPU 阶段绕过 Barrier 前缀；
- 把 `WORKBEGIN` 同时作为 CPU switch 和 measurement begin；
- 用 timeout 延长掩盖 checkpoint 到 `WORKBEGIN` 的执行成本；
- 以空 FST header、进程存活或单个 marker 作为采集成功；
- 将 Barrier 后仍存在的人口差异归因于 converter 并修改 Ruby/LSQ；
- 在没有天然算法 phase 的 workload 上把被动 marker 描述为严格软件进度同步。

## 恢复后的合同

`first-core-macro-common-end` 继承 FastSim
`first-core-target-common-end-v1` 的停止拓扑，但统一采用两个 producer 都能直接
观察的 CPL3 architectural macro：

1. ROI begin 后所有参与核连续记录真实 user+kernel 执行。
2. 每核只在完整退休宏边界增加 CPL3 macro 计数。
3. 第一个达到目标的核心成为 trigger core。
4. 该事件关闭本 producer 内全部参与核，其他核不追赶目标。
5. FST、CPI 和 PMU 使用共同结束时每核实际人口，不 padding、不 reslice。

该合同仍是近似：QEMU 和 gem5 的 common end 分别发生在各自时间域，不能据此
断言 PC、地址、内核路径或算法 phase 相同。双 producer 报告必须保留这一限制。

## 后续验证边界

- 宏预算主线先以 Stockfish C4 1M 完成双 producer、FST 与 FastSim replay。
- QEMU producer/lowerer 重构使用同一 raw 的 `.fst` 与全部 companion SHA256
  parity 证明语义中立。
- Barrier 实验数据只用于本复盘，不进入新主线验收或 production 晋升。
- 2026-09-19 的 `var/qemu_fst/runs/production/` 是冻结宏预算基线，不自动代表
  后续 Canonical Record 实现。
