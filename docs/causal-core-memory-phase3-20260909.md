# 因果事件模型：多核共享状态与连续边界接入

日期：2026-09-09。接续 [混合指令阶段](causal-core-memory-phase2-20260909.md)。

后续已完成 [完整动态依赖重采集](fst-complete-dependencies-20260909.md)，解除本文的
依赖完整性门禁；随后 [同一前缀接入验证](causal-core-memory-prefix-gate-20260909.md)
接入旧 native-FS 域外访存策略，并通过全事件守恒。内核预热中的 Local APIC EOI
写入沿用既有本地完成抽象，没有新增设备仿真组件。
本文保留第三阶段当时的验证范围和结果。
本轮接入多核、保守一致性、连续预热、普通内核指令和非访存序列化；维护默认未切换。
机制测试与四核合成 CLI 通过。真实四核 TeaLeaf 前缀因缺少功能操作数信息拒绝运行，
没有产出可比较的 CPI。

## 已接入的事件关系

- `run_causal_multicore` 使用一个全局事件队列。ROB/IQ/LQ/SQ、FU、分支预测器、依赖、
  L1/L2 和取指前沿属于各核心；LLC generation、CHA 服务日历和 DRAM 属于共享状态。
  所有核心先暴露当前可执行事件，共享服务才能推进到未来。不存在逐核完整求解再合并结果。
- 请求与回调携带 `(core, sequence, fragment)`，共享 generation 同时保存 leader core。
  跨核同线 miss 可以合并到唯一 LLC/DRAM 服务，各自返回到原核心的私有 generation。
  同 tick 核心准入采用确定性的轮转顺序，没有 workload/PC 条件或参考时序输入。
- 一致性按物理 cache line 排队：读可共享，写须独占，等待中的写阻止后续读越过。
  peer snoop 使旧写权限失效；写请求失效 peer L1/L2，读请求降级脏副本，数据传输进入
  共享层后再授予请求方权限。原始 L1 回调释放事务占用，避免旧 demand fill 在失效后复活。
  这是一种**持有到数据响应的保守一致性近似**，不是完整 Ruby 协议或其并发时序复现。
- `WarmupInstructionTraceSource` 的边界只切换功能记录的计数归属，继续同一事件链，
  不清空 cache、ROB、SQ、依赖或预测器。预热 store 可以在测量段取指之后返回。
  每核测量周期为最后一次退休减最后一条预热记录的退休时刻；边界不要求跨核同时到达。
- 普通 kernel 标签保留，FU 分类使用 `canonical_op_class()`；内核退休/访存/分支计数
  是总计数的子集。非访存 `kSerialize` 阻止年轻指令取指，等待旧 ROB 与 SQ 实际排空
  才发射，退休后一周期恢复。没有固定 drain 补偿，也没有伪造系统调用服务成本。

`Simulator` 支持每核一个绑定，验证绑定完整且无重复，按核心排序后保留 thread ID、
core ID 与 ASID。旧单核回调 API 仍由适配器支持。实验配置入口是
[`configs/causal-multicore-prototype.cfg`](../configs/causal-multicore-prototype.cfg)。

## 统计口径

JSON 标记 `experimental-causal-multicore`，CSV 原有字段后追加 `core,leader_core,measured`。

- `causal_read` 的队列积分、MSHR、回调和 stall 诊断覆盖整个执行过程，包括预热。
  队列面积是各核之和；private MSHR/live UOP 峰值是单个私有域最大值，LLC/DRAM 是全局值。
- core/thread 的退休计数属于测量段；cache/CHA 计数按请求的功能记录归属区分预热与测量。
  因此这些 cache 计数不是按某个全局 wall-clock 区间截取的 PMU 快照。
- `measurement_begin_cycles`、`last_retire_cycles` 明示每核截点；`drained_cycle` 还包含
  尾部 store/写回传输，不能替代退休周期，也不表示 DRAM 内部 buffered writes 全部排空。
- 宿主 `measurement_wall_ns` 当前覆盖整个事件运行，包括预热，不作为纯测量段吞吐证据。
  连续边界没有旧式 warmup barrier，barrier-cycle 统计保持零。

## 最小必要验证

构建与 `fastsim_tests` 是基础门禁。新增检查仅覆盖本轮发生变化的关系：

1. 两核共享 LLC miss 的唯一服务和正确核心唤醒；独立请求不会被另一核依赖链整体阻塞。
2. 写失效、脏数据供应、读写事务互斥、完整响应释放；后续 peer load 不能命中失效副本。
3. 相同功能流有无预热标记，逐事件时间线完全相同；只改变计数归属和周期截点。
4. 相同指令加 kernel 标签后时序相同；序列化等待真实旧 ROB/SQ 生命周期。
5. 四核乱序输入绑定在 Simulator 输出中保持各自核心、线程和周期身份。

另执行一个固定的四核合成 CLI：24 UOP，4 条预热、20 条测量，其中测量内核 5 UOP；
全程各 8 条 load/store。它同时经过真实 manifest、BinaryTraceSource、Warmup wrapper、
Simulator、DramModel 与 JSON/CSV 输出。逐事件独立重建 ROB/IQ/LQ/SQ、MSHR/DRAM 面积，
核对 TSO、序列化、回调、一致性事务与 core/thread/边界身份。

最终账本结果：ROB/IQ/LQ 面积 4,695 / 4,033 / 1,573，SQ 面积 3,154；
L1/L2/LLC MSHR 面积 1,310 / 1,254 / 336，DRAM 面积 192、峰值 4。
15 次一致性事务、7 次 peer 失效、5 次 peer 数据传输均完成。

该 CLI 暴露并修复了共享准入之后才应更新 DRAM 峰值、线程核心编号未写回的问题；
预热启用标志也随实际源边界设置。因这些具体问题重跑受影响检查；未扩展完整矩阵、
八组旧 CLI、随机种子或 sanitizer。

产物位于 [`tmp/causal-multicore-implementation-20260909/`](../tmp/causal-multicore-implementation-20260909/)：
`build.log`、`tests.log`、`cli-smoke/validation.json`、`audit_smoke.py`。
合成输入仅验证接口与机制，不是 held-out 工作负载或 gem5 精度证明。
最终二进制 SHA256：`85c27e56fc7985ca122ed13d45c4ee4b0d80023badce1c2f34d0df784445cddb`。

## 真实 TeaLeaf 接入的阻塞

固定使用原四核输入各前 2,000 宏指令：3,428 / 3,500 / 4,132 / 3,500 UOP，合计 14,560。
每核前 1,000 宏指令预热，后 1,000 测量。所有 64-byte 记录与原始前缀逐字节相同，
没有过滤内核、序列化或其他记录。输入身份和摘要在 `tealeaf-c4/provenance.json`。

core2 的原始前缀全部带 kernel 标签，并有 15 条序列化指令；接入上述机制后，原始
operand map 的缺口使其仍然失败：两个 PC 缺行，影响 67 条记录，其中 5 条 `n_src > 4`。
首个无法证明依赖完整的记录位于 core2 ordinal 7。源寄存器数超过四槽只表示截断风险，
不证明不同 producer 已超过四个。一个 PC 在其他核 map 可见，但另一个 kernel PC
在该次采集的四核 map 都不存在；不能据此宣布完整依赖已恢复或改写 privilege 标签。

拒绝结果记录于 `tealeaf-c4/validation.json`，`run.log` 保存错误。失败过程的 `events.csv`
只是部分事件，不能作为完整队列面积或 CPI 证据；预备的 `audit_prefix.py` 尚未在完整
真实运行上通过。没有生成该输入的 `stats.json`。

下一步先完善功能输入的可观测性：从匹配的静态机器码/解码器补齐完整 macro operand
事实，或由 trace producer 输出超过四槽的完整动态依赖。补齐后只重跑这个固定前缀。
不从 gem5 issue/response tick 猜缺失依赖，也不把缺失边当作独立指令继续执行。

## 剩余机制边界

仍未支持真实 I-side/翻译、syscall、atomic/LOCK、静态内存屏障、访存序列化、ASID
切换、DVFS 和 inclusive LLC。cache writeback 仍无有限端口/缓冲反压；DRAM 仍复用现有
FCFS/buffered-write 近似，未增加 refresh。保守一致性和宏级依赖也需后续与目标机制
逐事件核对。因此本轮是后续接入进展，尚不能回答完整 TeaLeaf CPI 已改善。
