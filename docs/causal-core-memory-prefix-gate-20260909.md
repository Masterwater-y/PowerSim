# 因果事件模型：既有域外策略与 gem5 阶段边界

日期：2026-09-09。接续 [完整依赖](fst-complete-dependencies-20260909.md)。

后续已完成 [load 执行阶段修复及真实 ROI 采样接入](causal-load-stages-roi-20260909.md)：
全部功能预热加每核 10,000 条用户 ROI UOP 已跑通；本文保留前缀阶段当时的证据和范围。

本轮完成既有 `allow_mmio_escape` 策略的事件路径接入，固定 TeaLeaf 四核前缀首次通过
全事件守恒检查；同时用已有 gem5 JSONL 定位、修正了实验配置的 dispatch/retire 阶段映射。
这是机制接入和局部阶段验证，**尚未证明正式 ROI CPI 改善**。

## 既有域外访存策略

[`src/causal_read.cpp`](../src/causal_read.cpp) 按配置的 RAM 范围逐 cache-line 分片。
启用 `trace.allow_mmio_escape` 时，域外片保留原 UOP、动态依赖、core/FU 时序、
提交顺序和 LQ/SQ 生命周期，走本地完成事件；不进入 cache/coherence/DRAM。
load 遵守已有的 core 最小完成边；store 在执行、退休及 TSO 发送资格成立后完成。
SQ 仍等全部片的回调释放。RAM/域外混合访问分别处理，域外 load 不伪装成 SQ 数据转发。

统计保留全部 memory UOP；`memory_accesses` 只计 RAM 片，域外片进入
`mmio_escape_accesses`。回调事件 `level=-1` 明示本地完成，JSON 明示
`mmio_escape=core-only-no-device-service`。关闭策略仍明确拒绝域外访问；零长度、
非物理地址、地址加长度溢出继续拒绝，合法最高地址不因排他结束地址溢出而错误拒绝。

没有新增 APIC、设备服务延迟或中断生成器。这是原 native-FS 抽象边界的接入；
新路径当前仍使用显式 ideal translation，不能据此宣称已重现旧路径的 DTLB 时序。
该访问的存在也没有被证明是 TeaLeaf CPI 的误差根因。

## 最少验证与结果

- 构建成功，`./build/fastsim_tests` 输出 `all FastSim tests passed`。
- 新机制检查覆盖混合 RAM/域外 store、分片回调、RAW、TSO 双向等待、SQ 释放、
  域外 load 的 core 完成边、RAM 边界分片、最高地址及非法输入。
- 同一合成事件流验证宿主切批/预热不变性；全 RAM 事件流在策略开关前后完全一致。
- 同一 TeaLeaf 前缀共 **14,560 UOP**，与新采集主记录逐字节一致，**27 条扩展依赖**
  全保留；包含 **4,132 条内核 UOP、13 次域外分片访问、15 条序列化指令**。
  独立 CSV 审计重建 ROB/IQ/LQ/SQ、各级 MSHR、DRAM 占用积分，并验证 RAW、TSO、
  序列化、各阶段实际配置宽度、唯一回调和一致性占用守恒，全部通过。

真实验证只有同一个前缀两次：一次验证域外接入，一次验证下面的 O3 阶段映射和几何配置。
本轮未重采集 gem5、未跑其他 workload、未扩展矩阵或 sanitizer，也未重复整套测试。
早先测试工具把 store 回调层级固定为 L1，不能识别合法的 `level=-1`，本轮修正了此
检查的层级选择，原有顺序与回调断言保留。

## gem5 对照与阶段修正

新增只读离线工具 [`audit_causal_prefix_reference.py`](../tools/audit_causal_prefix_reference.py)。
它校验完整依赖附件，再逐条比对 FST 与既有 JSONL 的 core/ordinal、PC、访存地址/大小、
操作类型、分支功能字段、源/目的计数、内核标记和 ASID，并校验 label 的关联身份。
不把 reference tick 送进求解器；FST 未保存的 gem5 `seq_num`/`micro_pc` 不宣称已核验。

参考为 `tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full/`。四核 14,560 条功能身份
全部匹配。配置检查发现原型 dispatch/issue/commit 为 4，而参考为 8；L1D、RAM 容量、
rank 和 bank-group 数也不同。现已在本次对照配置中按 `gem5/config.json` 对齐这些
受支持的几何参数。LLC 是 **8 个 bank，每 bank 8 MiB，共 64 MiB**，不能把每 bank
大小误当总容量。这个检查不等于 cache/controller/DRAM 服务时序已对齐。

[`causal-gem5-o3-prototype.cfg`](../configs/causal-gem5-o3-prototype.cfg) 单独保存源码
支持的 O3 阶段映射，维护 native-FS 默认配置不切换：

1. `dispatch_to_issue=0`：gem5 `IEW::tick()` 先 dispatch，随后在同拍调用
   `InstQueue::scheduleReadyInsts()`。原型原先额外等一拍。
2. `execute_to_commit=2`：当前目标 `iewToCommitDelay=1`；`Commit::tick()` 先
   `commit()`，再 `markCompletedInsts()`，还须下一次提交机会。原型原先完成同拍就
   退休。新增等待只作用于退休/ROB 释放，消费者仍由实际 writeback 唤醒。
3. `issue_to_execute=0` 保持现有 FU 抽象：`opLat-1` 的 FU 完成调度加最后一拍
   issue-to-execute 已包含在目标 op latency 内，不能再给普通计算指令重复加一拍。

源码依据（本地 `/data00/yinhaolang/gem5-fs`）：`src/cpu/o3/iew.cc` 的 `tick()`，
`inst_queue.cc` 的 `scheduleReadyInsts()`，`commit.cc` 的 `tick()` 与
`setIEWQueue()`。参数来自参考 config，不按 TeaLeaf 的残差或 PC 选择。

三项宽度一起对齐后，最初连续非访存/非控制片段的 **7 条计算指令**，相对各自 fetch
的 issue/完成/retire 三元组全部与已有 gem5 标签一致。core0 前三条示例，单位 cycles：

| core0 ordinal | 原型 issue/完成/retire | 修正后 | gem5 |
|---|---|---|---|
| 0 | 5 / 6 / 6 | 4 / 5 / 7 | 4 / 5 / 7 |
| 1 | 6 / 7 / 7 | 5 / 6 / 8 | 5 / 6 / 8 |
| 2 | 7 / 8 / 8 | 6 / 7 / 9 | 6 / 7 / 9 |

这只证明初始无访存片段的阶段映射，不表示整个前缀逐周期一致，更不是泛化精度结论。
同一前缀的全事件守恒在配置修正后再次通过。

## 明确的测量与标签缺口

当前测试切点为每核前 1,000 宏指令预热、后 1,000 宏指令测量；真实 gem5 ROI 开始于
各核 ordinal **2,090,218 / 290,195 / 229,364 / 287,693**。整个当前前缀都在真实
ROI 之前。离线工具因此明确输出 `cpi_comparison_qualified=false`，不将测试切点的
统计与正式 gem5 CPI 做差。

TaoTrace `issue_tick` 和 `complete_tick` 是相对 fetch 的偏移，`ready_tick` 是
`fetch_tick + complete_tick`，**不是操作数 ready**。此外当前源码在
`IEW::updateExeInstStats()` 写 `completeTick`，`LSQUnit::writeback()` 未覆盖此字段；
因此即使采集器附近的旧注释说 load ready 是数据返回，也不能据此解释为真实 callback。
本轮明确拒绝用 load 的这个标签比对 FastSim `data`/writeback，更不能把它作为推理输入。
完整缓存准入/返回的比较仍需要现有 LSQ/Sequencer 探针的对应事件；已有 native-response
诊断主要覆盖测量段，不能填充本前缀的预热响应信息。

下一轮先闭合普通 RAM load 的执行、准入、真实数据返回、writeback 四个阶段的时间定义，
复用现有探针取证；保持同一功能工作量和参数。I-side/DTLB、FRFCFS、Sequencer 容量和
保守一致性仍是显式差距，按第一条证实的因果分歧逐项接入。不得用 load 的旧完成标签、
测试切点 CPI 或域外地址本身替代误差根因。

## 产物与复现

产物根：[`tmp/causal-mmio-escape-20260909/`](../tmp/causal-mmio-escape-20260909/)。
`build.log` / `tests.log`；`tealeaf-prefix/` 与 `tealeaf-o3-prefix/` 各自的配置、stats、
events、独立 `validation.json`；`reference-before.json` / `reference-after.json`；
`source-hashes.json` 保存本轮文件与本地 gem5 阶段源码指纹。

```bash
python3 tools/audit_causal_prefix_reference.py \
  --fst-dir tmp/causal-mmio-escape-20260909/tealeaf-prefix \
  --gem5-trace-dir tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full/trace \
  --gem5-config tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full/gem5/config.json \
  --stats tmp/causal-mmio-escape-20260909/tealeaf-o3-prefix/stats.json \
  --events tmp/causal-mmio-escape-20260909/tealeaf-o3-prefix/events.csv \
  --output tmp/causal-mmio-escape-20260909/reference-after.json
```
