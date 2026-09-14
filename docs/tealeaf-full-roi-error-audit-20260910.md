# TeaLeaf L1D64 C4：当前模型的完整 ROI 误差审计

日期：2026-09-10。对象是已包含 9 月 9 日请求起点修复的当前生产模型。
本轮只做诊断，没有修改生产源码、默认配置、FST 或生产二进制，也没有重新运行 gem5。
以下全量结果取代旧报告中不能直接比较的 FastSim checkpoint 内等待小计。

## 结论与覆盖范围

**全量比较确认：主要欠估表现为已发射 load 在 ROB 头的等待不足。**
完整四核 active cycles 少 4,370,942，CPI 误差仍为 **−17.80043344%**。
相同硬件指令流上的 issued-load-head 等待，FastSim 比 gem5 少 **3,487,465 elapsed cycles**。
考虑 gem5 全 ROI 的 407,378 idle cycles，active 等待差的保守范围为
**3,080,087–3,487,465 cycles**，相当于总 active 周期缺口的 **70.47%–79.79%**。
这是互斥阶段的账面分解，不是开启某个修复后能获得的因果收益比例。

测量人口为每核 10M user UOP，四核合计 40,000,000，加上 169,794 条 native-kernel
记录。全程匹配 **40,169,786 条硬件记录**；另有 **8 条 syscall 辅助事实**没有 gem5
O3 stage，在两端比较中明确过滤。内核实际执行的硬件指令仍全部保留。
采用 DSE 固定的 cycles/user-UOP 分母，不混入宏指令 CPI。

之前使用的 10,001 / 13,001 条密集窗口分别位于 core1 的 ordinal
2,509,999–2,519,999 / 1,428,000–1,441,000，并非起始前缀。
它们负责追踪具体请求生命周期；完整误差分布由本轮全流统计给出。

## 全量周期与中间状态

| 核心 | gem5 active cycles | FastSim cycles | FastSim − gem5 |
|---:|---:|---:|---:|
| 0 | 6,410,303 | 4,905,800 | −1,504,503 |
| 1 | 6,515,376 | 5,417,429 | −1,097,947 |
| 2 | 4,552,996 | 3,646,192 | −906,804 |
| 3 | 7,076,581 | 6,214,893 | −861,688 |
| 合计 | **24,555,256** | **20,184,314** | **−4,370,942** |

对应 CPI：gem5 **0.6138814**，FastSim **0.50460785**。

下面按 `(首条 hardware retire, 最后一条 hardware retire]`，把每个零退休周期
划入下一条 committed-path ROB 头所在的阶段。同周期退休多条指令不会重复累计。
有退休的周期另列，使整个区间严格守恒。

| 四核互斥周期类别 | gem5 | FastSim | FastSim − gem5 |
|---|---:|---:|---:|
| 下一条 committed-path 指令尚未 fetch | 558,751 | 20,014 | −538,737 |
| 已 fetch、尚未 issue | 435,048 | 81,370 | −353,678 |
| 已 issue 的 load 尚未 retire | **17,355,995** | **13,868,530** | **−3,487,465** |
| 已 issue 的 store 尚未 retire | 14,364 | 1,938 | −12,426 |
| 已 issue 的其他指令尚未 retire | 15,074 | 2,171 | −12,903 |
| 有退休的周期 | 6,583,379 | 6,209,963 | −373,416 |

gem5 表内是 elapsed cycles，包含 idle；不能把各行直接当成 active 周期贡献。
四核 idle 分别为 347,843 / 3,922 / 50,689 / 4,924 cycles。尤其 core0 的
fetch 前等待必须保留这一不确定性，不能全部归为前端模型问题。

边界单列：FastSim 首条 hardware retire 之前为 1 / 83 / 0 / 244 cycles，合计
328；尾部未覆盖为 0。gem5 只有 core2 的首条 hardware retire 之前有 23 cycles。
完整差值核对：

```text
−4,778,625（表内 elapsed 差）+ 407,378（gem5 idle）
+ 328（FastSim 首边界）− 23（gem5 首边界）= −4,370,942
```

原 FastSim `response_residual_head_gap_zero_commit_cycles` 小计为 13,964,316，
全流连续重算为 13,974,023，增加 9,707 cycles。本轮不再依赖该 checkpoint 内小计。

### 可比的 ROB 头阻塞次数

这里统计真正产生 issued-load 零退休间隔的不同动态 load 头，比直接对照两端
`ROBFullEvents` 更明确：

| 核心 | gem5 load 阻塞头数 | FastSim load 阻塞头数 | gem5 等待 cycles | FastSim 等待 cycles |
|---:|---:|---:|---:|---:|
| 0 | 59,831 | 48,277 | 4,298,601 | 3,319,898 |
| 1 | 56,608 | 43,743 | 4,863,377 | 3,831,896 |
| 2 | 31,195 | 21,467 | 2,867,318 | 2,050,638 |
| 3 | 65,986 | 51,254 | 5,326,699 | 4,666,098 |

原生资源计数不能直接相减。例如 core1，gem5 `rename.ROBFullEvents=335,559`，
FastSim `o3_rob_full_events=6,015,756`、`o3_rob_stall_cycles=4,645,857,706`。
gem5 计的是 rename 阻塞/截断调用；FastSim 计的是受 ROB release 推迟的 UOP，
stall 是重叠的位移之和。gem5 最终统一 stats dump 的区间还长于该核 target-stop
ROI。它们不是同单位、同区间的 ROB occupancy 或阻塞周期。

## 误差在完整执行过程中的分布

每核按测量记录进度分成 100 段，所有记录、段间的退休间隔均纳入，无窗口抽样。
下面展示每 10% 进度内的退休跨度差，单位 cycles；这里仍是含 idle 的 elapsed 差。

| 进度 | core0 | core1 | core2 | core3 |
|---|---:|---:|---:|---:|
| 0–10% | −26 | +28,773 | +564 | +36,740 |
| 10–20% | −601,574 | −234,425 | 0 | −20,394 |
| 20–30% | −275,589 | −217,590 | 0 | −92,452 |
| 30–40% | −253,688 | −204,425 | −172,700 | −105,324 |
| 40–50% | −236,594 | −127,262 | −201,917 | −74,432 |
| 50–60% | −103,244 | +1 | −267,696 | −121,192 |
| 60–70% | −6,366 | 0 | −200,186 | −210,723 |
| 70–80% | −153,896 | −187,309 | −77,016 | −130,545 |
| 80–90% | −123,734 | −105,873 | 0 | −68,062 |
| 90–100% | −97,636 | −53,842 | −38,519 | −80,472 |

因此欠估出现在多个中后段，不能归因于 ROI 开头，也不是所有阶段统一少一个常数。
core1 的 50–70% 和 core2 的 10–30% 基本没有新增跨度误差。

![全 ROI 累计阶段差](../tmp/tealeaf-current-error-20260910/full-roi-progress.svg)

全流按静态 PC 汇总的 load-head 等待缺口主要集中在用户计算中的几条 load：

| PC | gem5 − FastSim load-head cycles |
|---|---:|
| `0x409fce` | 2,204,084 |
| `0x40a207` | 612,760 |
| `0x40a217` | 436,357 |
| `0x40a00d` | 201,068 |
| `0x409fc8` | 80,494 |

PC 只用于选择后续请求见证，不能成为修复条件或补偿参数；这些差值同样不是逐 PC
干预后的 CPI 收益。

## 从全量等待差追到机制

### 请求数近似正确，响应时间仍不正确

复用完整 ROI 的 native summary，与本轮当前 FastSim 对照：

| 数据层级人口 | gem5 native | 当前 FastSim |
|---|---:|---:|
| committed memory UOP | 5,646,619 | 5,646,619 |
| L1D tag misses | 273,254 | 273,284 |
| private L2 tag misses | 271,555 | 271,752 |
| data DRAM read transactions | 186,893 | 187,054 |

DRAM read 数量只差 **+0.0861%**，不能解释为大量 DRAM 请求漏采。
native tag/协议计数不冒充严格硬件 PMU，也不能据此证明每条请求路径相同。

### 同线 miss 未完成时，后继 load 被当成快速本地命中

独立 v7 诊断窗口中，421 条严格单 line 普通 load 在 gem5 为 coalesced follower，
当前 FastSim 为本地命中。gem5 admission→response 平均 **111.07 cycles**，
FastSim 服务时间 **2 cycles**；对应 issue→retire 尾部差平均 **−109.23 cycles**。

机制对应：普通 private-cache `access` 在 miss 时立即安装 tag，后续访问可以看到
这个 tag；生产反馈没有启用按 parent response 等待的 pending-fill 闭合。
最近修复的 load response 下界只保证 load 不早于它自己被分配的响应，不能纠正
“这个 load 原本应该等待同线 parent，但被分配了 2-cycle hit”的服务选择。
相关入口在 `src/cache.cpp` 的 `SetAssociativeCache::access_indexed`，以及
`src/simulator.cpp` 的 `preview_memory_event`、response feedback。

同线访问间距也会随核心排程改变，因此这 421 条不能简单乘以平均延迟作为全程
CPI 缺口，更不能直接启用旧 pending-fill 实验后就宣布修复。

### DRAM 服务选择仍使用早期到达状态

同一 v7 窗口的 81 条普通 load 两端均走 DRAM。当前 FastSim 服务平均 **164 cycles**、
中位数 **161**，gem5 平均 **334.44**、中位数 **361**。
v7 的 gem5 SHA 与冻结正式参考不同，只用于独立请求机制验证，不替代全 ROI CPI。

正式参考原二进制的完整 ROI 中，另有 20 条已通过 LSQ→Sequencer→DRAM 精确关联
的 load 头见证，覆盖对应窗口 7,071 / 8,559 个 issued-load-head cycles：

- 19 条非 refresh 请求，gem5 admission→response 为 **243–358 cycles**、中位数
  **315**；当前 FastSim 的 20 条服务为 **161–229 cycles**、中位数 **161**。
- 19 条非 refresh 的 gem5 arrival→调度等待为 **16.10–130.68 cycles**，中位数
  **87.62**。当前 FastSim 全部 20 条 canonical arrival→RD command 等待为
  **0–68 cycles**、中位数 **0**。调度调用与真正 RD command 是不同事件，不能
  把这两个统计伪装成完全同口径。
- 非 refresh 请求还都有调度→RD command 的 **85.045 cycles** 间隔。
  例如 ordinal 2,510,249，gem5 是 row-buffer hit：排队 87.625、调度→RD
  85.045，admission→response 总计 315；FastSim 同一请求排队为 0、服务为 161。
  这不是单纯把 tCL 配小了，而涉及排程时可见的队列和命令时间表。
- 19/20 条请求的 canonical DRAM arrival 比最终 corrected core request 早
  **85–885 cycles**。当前模型把服务相对延迟平移到 corrected issue，已经修复了
  请求起点重复计费及 response 下界，但没有据此重新选择共享队列／bank 状态。
  不能把两个时基的差再作为惩罚加回。

本 case 的 `frfcfs_effective_selection_window=1`，候选修复 epochs=0，bypass
epochs=5,058，service-fill checks=0。C4、8 channels、2 ranks 在当前拓扑窗口规则
下直接保留 canonical 时序。`tRAS/tRTP/tRRD/tXAW/tCCD_L/tCS` 等约束也仍关闭。
这些事实解释了为何已有相关代码并不等于当前服务选择已与真实请求到达闭合；
不能未经验证就打开所有约束，否则错误的到达次序可能进一步放大误差。

### refresh 长尾确实缺失，但不能解释全部差异

ordinal 2,514,569 的 gem5 response 为 **1,504 cycles**，DRAM 排队
**1,309.81 cycles**，恰在目标 rank refresh 恢复时获调度；当前 FastSim 为 **161**。
目标配置有 `tREFI=7,800,000 ticks`、`tRFC=350,000 ticks`，当前 DRAM 模型没有对应
refresh 日历。它是已确认的一类缺失机制，但非 refresh 的 19 条也明显过快，不能
把整体欠估都归到 refresh。

## 下一步修复方向与证据边界

先处理原两阶段框架内的 **同线响应可见性与服务选择起点一致性**：使用请求自己的
arrival/parent generation/response，使后继 load 的响应由仍在途的同线请求约束；
对会影响服务结果的共享时序状态做有限、可验证的批次修正。保持现有并行批处理，
不切换 causal_read、不按 PC 或负载补偿。

之后再用正确到达集合验证 DRAM 命令约束和 rank refresh 日历。
全量阶段统计已经给出主要差异落点，但尚未通过模型干预把 17.80% 分别分摊给
同线可见性、排队和 refresh。剩余 fetch/issue 前差异也需要结合 idle 及相应阶段
见证，不能直接扩大成前端惩罚。

## 验证与复现

产物目录：[tmp/tealeaf-current-error-20260910](../tmp/tealeaf-current-error-20260910/)。

- 新增 4 次 FastSim 诊断运行：完整 ROI 原审计 1 次、v7 短采集 control/audit
  各 1 次、完整 ROI 精简阶段导出 1 次。没有重采 FST 或运行 gem5、其他负载矩阵。
- 全阶段诊断只修改 `tmp/` 内的 `simulator-full-stage.cpp` 副本；每个已接受批次
  写出 sequence/PC/fetch/issue/completion/retire 六个整数。未接受的重试不写出，
  工作内存按批次有界。对完整记录做流式／分块分析，不保存 4,000 万条富 JSON。
- 精简导出与当前生产 audit 的配置、scope（排除主机 throughput）、threads 和
  10,001 条完整 rich audit 精确相等。最终 CPI、PMU 不变；诊断运行不用于宣称吞吐。
- 全流 FastSim sequence、PC 与 FST 精确匹配；gem5 按非 syscall 的连续 micro_seq、
  core/thread 与 FST ordinal 关联，逐条检查 fetch/issue/commit 时钟与单调性。
  四核独立重算与旧 gem5 全流工具的阶段、跨度、阻塞次数完全相等。
- 四核完整 FST SHA 再次核对，与既有同二进制 gem5 debug replay 的全文件相等证明
  一致；当前生产源码、测试源码、生产二进制 SHA 均未改变。
- 每核及四核总量都满足阶段和、边界、idle 到 active-cycle 差值的精确守恒。

主要文件：`full-roi-comparison.json`、`full-comparison-core*.json`、
`full-roi-progress.svg`、`current-dram-witnesses.json`、`native-v7-load-service.json`、
`dram-parameters.json`、`final-provenance-check.json`。离线程序、隔离插桩 diff、
构建和运行命令、日志均在同目录。生产行为未改，因此未重复构建／运行生产测试集。
