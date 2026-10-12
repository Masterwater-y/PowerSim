# 功能同步 trace：OpenMP、跨采集器协议与 FastSim 建模讨论

日期：2026-10-12。状态：讨论结论与设计约束，不代表相关采集器或 FastSim 模型已经实现。

本文接续 [TeaLeaf 会话纪要](tealeaf-4c-buffer-tso-session-20261012.md)。
用户要求最终 trace 可以来自 QEMU 或 DynamoRIO；gem5 是微架构精度 baseline，
不能成为同步关系采集的唯一依赖。当前实施范围先限于 gem5 侧输出与输出验证。

## 1. 线程顺序与时间连续性

OpenMP worker 的有效计算段可能断续：等待启动、计算并行区 A、等待或休眠、计算
并行区 B。等待既可能主动自旋，也可能经 futex 被动睡眠。主线程也会在 join/barrier
等待其他参与者，不能假定它始终连续执行。

线程内部的指令顺序通常仍然连续且明确。问题是如果 trace 只保留计算指令，
`A -> 等待主线程启动 B -> B` 会变成 `A -> B`，失去跨线程的执行条件。
FastSim 可以读对指令，却把 B 放到过早的模拟时间和窗口，改变共享 cache、coherence
和 DRAM 事件的重叠关系。提前解码不是缺陷；提前让受同步约束的指令执行或修改
共享状态才是缺陷。缩小窗口不能补回遗漏的因果关系。

当前 [architecture.md](architecture.md) 的线程到核绑定是静态、单射的；
`src/simulator.cpp` 的 `ThreadRunState` 只有 runnable/finished，尚无同步等待和
OS block/wakeup/migration 模型。[native-kernel 契约](fst-native-kernel-trace.md)
明确说明：有 CPL0 指令不等于拥有 scheduler trace。

## 2. 对旧纪要结论的修正

OpenMP fork-join 与 first-core common-end 可以产生不等进度，但不能推出其他核
必然零指令。正常并行区允许多个核在首核达标前执行大量指令。当前 TeaLeaf 的
近零 worker 记录需要进一步取证，不能单凭 fork-join 定性为已确证根因。

历史四核较均衡也不能证明历史必然采用 wave 多副本。wave 设施的存在只是线索。
单问题协作 OpenMP 和多副本 rate 是不同工作负载，不应相互替换后宣称复刻成功。

## 3. 与采集器无关的功能事件

采集依赖事实而不是采集机器的时间答案。推荐事件包括：线程身份/生命周期、
并行区实例、团队参与者、worker 工作段、barrier 到达/离开和轮次、可靠的锁交接、
task 依赖，以及 ROI 开始时已经存在的同步状态。

拟议的事件字段为：

```text
thread_id, stream_id, instruction_or_record_ordinal, before_or_after,
event_kind, address_space_id, team_id, region_instance,
sync_object_id, object_incarnation, generation, participants
```

字段需要有效性和能力声明；未观测到的 OS TID、等待方式或生命周期保持未知。
线程 ID 不等于 CPU ID。对象地址必须配合地址空间及生命周期，重复 barrier 必须
区分轮次。源 architectural ordinal 在 lowering 时准确转换到 FST UOP 边界。
无需按时间戳建立所有线程的总顺序，只记录必须成立的依赖边。

推荐使用版本化稀疏 companion，不扩大每条 FST 的 64-byte 热记录。协议数据是
功能输入；gem5 commit tick、cache 结果和等待周期只能作为独立离线 oracle。

## 4. 采集路线

| 路线 | 方法 | 能力边界 |
|---|---|---|
| runtime 语义层 | libgomp/libomp 内部在团队启动、worker 入口、barrier、join 发事件 | 最完整；需要版本适配；记录插桩对执行的影响 |
| OMPT | 支持 OMPT 的 runtime 使用 parallel/implicit-task/sync-region 回调 | 必须核实实际 runtime 的支持与 callback coverage |
| DynamoRIO | drwrap 包装函数、内部执行点插桩，关联 per-thread trace | 默认 drmemtrace 不保证完整 OpenMP 同步语义；静态/内联路径需要适配 |
| QEMU TCG | 插桩 runtime 执行点或语义 marker | plugin 不自动识别 OpenMP 或 guest OS TID；system 模式需要额外身份来源 |
| OS 事件扩展 | switch/block/wakeup/migration | 是调度层输入，不替代应用依赖；采集墙钟不作为目标时间 |

仅识别 GOMP_parallel 的 call 不足以得到团队的完整动态关系。只观察 futex 也不足：
用户态自旋不一定发 syscall；wait 可以立即 EAGAIN；wake 并不能自动证明具体 waiter
已经运行。condvar 还需处理丢失通知、伪唤醒和谓词，不能机械把 signal 连到任意 wait。

采集应在线程本地缓冲后批量输出，避免每条事件写文件/全局日志锁。必须提供指令
锚定和可审计关联，不能靠两个日志的近似时间戳拼接。QEMU 指令执行前回调不保证
指令最终成功完成，adapter 必须处理异常和完成边界。

普通 user-mode DR 不能提供 native kernel 指令或可靠的 OS switch/wakeup。
CPU_ID/TIMESTAMP 是受插桩影响的采集调度线索，不等于未插桩程序的目标调度。
raw DR/QEMU -> FST 的 ISA decode、UOP lowering、完整寄存器依赖和物理地址仍是
独立工作；详见 [FST 转换契约](fst-v7-drmemtrace-conversion-contract.md)。

## 5. FastSim 的后续模型

首片可以保留一线程固定绑定一核，加入同步对象表和语义等待状态。普通 barrier：

```text
A_i = FastSim 预测的参与者 i 满足到达条件的时间
R = max(A_i)
每个参与者的 barrier 后工作开始时间 >= R
```

A_i 不是 decoder 读到 marker 的时刻，必须结合前序执行完成和相应内存排序。
真实 runtime 的通信、发布、唤醒服务不能随意设定一个常数，也不能与保留的 runtime
指令重复计费。软件 barrier 不等于 MFENCE/serialize/TSO store buffer。

模型在同步边界阻止后续工作执行或修改共享状态；其他核和在飞服务继续推进。
协调器应把 blocked 状态当作明确已知的 frontier 状态，避免一直索取等待线程的
下一条访存而死锁。条件满足后恢复线程，保留核上的 cache/预测器等状态。
无需因为软件 barrier 就要求宿主 producer 池做全局 barrier 或清空所有缓存。

ROI 可以从 barrier 中途开始或在参与者到齐前结束。需要初始同步状态和明确的
边界截断状态；合法的共同终点不能被补录成完整 barrier，也不能被误判为中途丢数据。

## 6. 精度与固定 trace 的限制

同步协议能修正阶段提前推进、错误重叠以及由此产生的共享资源竞争。
尚无实测证据给出 CPI/PMU 改善百分比，也未证明同步缺失是当前 TeaLeaf 误差主因。

固定 trace 已包含采集环境决定的自旋次数、锁赢家、dynamic loop 的迭代分配和 task
到 worker 的分配。补依赖不能自动重算这些执行选择。首片保留真实指令人口、补同步
约束；后续若以语义等待替换 polling 区间，需要独立处理指令/分支/访存人口与开销。

睡眠等待推进 elapsed time，但不直接计入 active CPI；自旋指令属于 active work。
不得通过 syscall 前后 timestamp 差补成 active kernel cycles。
任何后续精度报告遵守 [accuracy-reporting-contract.md](accuracy-reporting-contract.md)：
相对 CPI 误差旁必须列绝对误差 cycles/macro instruction，聚合表列 CPI MAE；
native cache 真值必须使用维护的 native-summary validator 和独立 identity gate。

## 7. 成本与验证

分别记录采集、转换、FastSim 回放成本。同步事件稀疏不保证总代价低，窗口碎片化、
宿主协调和重算可能主导成本。

```text
新增耗时 ≈ 事件数 × 单事件成本 + 窗口切分成本 + 必要重算成本
```

barrier 可通过到达计数与最大完成时间实现，释放时遍历参与者；无需线程两两全连接。
总处理复杂度可接近 O(指令数 + 同步事件数)。长工作段容易摊薄，频繁短同步可能
明显降低吞吐。现在没有可靠损失百分比，不能把设计目标当实测结果。

首先验证事件完整性、锚点、身份、轮次、参与者、ROI 初始/终止状态和 workload 结果。
再以相同插桩二进制比较输出 on/off，检查输出 observer 的目标执行不变性和宿主开销。
这不能代替未插桩/插桩 runtime 的扰动测量。FastSim 未消费事件前，不报告精度收益。

## 8. 参考资料

- [OpenMP 隐式 barrier 与 OMPT 回调](https://www.openmp.org/spec-html/5.1/openmpsu101.html)
- [GNU libgomp parallel ABI](https://gcc.gnu.org/onlinedocs/libgomp/Implementing-PARALLEL-construct.html)
- [GNU OpenMP 5.0 支持表](https://gcc.gnu.org/onlinedocs/libgomp/OpenMP-5_002e0.html)
- [DynamoRIO drwrap](https://dynamorio.org/page_drwrap.html)
- [DynamoRIO trace 格式](https://dynamorio.org/sec_drcachesim_format.html)
- [DynamoRIO scheduler 与采集调度限制](https://dynamorio.org/sec_drcachesim_sched.html)
- [QEMU TCG plugin](https://www.qemu.org/docs/master/devel/tcg-plugins.html)

上述资料用于能力核实；具体安装版本和 runtime 必须另行冻结，不以最新版文档替代
实际版本的采集证据。
