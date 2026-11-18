# Graph500 C4：原生与 FastSim 同成员生命周期对照

2026-11-17。实施 [短窗口诊断计划](superpowers/plans/2026-11-17-graph500-native-lifecycle.md)，接续 [parent 时间字段审计](graph500-parent-clock-and-queue-audit-20261117.md)。

## 结论

在本次新采集的 Graph500 C4 短窗口中，已直接证实：**FastSim 对 core0 同一批退休 load 的 IQ 生命周期明显低估，差异主要发生在 IQ 入队之后、请求成功接收之前。** 回调之后的 IQ 释放延迟没有表现为低估。

“合并成员占用偏短”的假设获得了同成员证据；但偏差不只出现在原生合并成员，原生首请求组也有相似的接收前寿命差异。因此，后续应检查接收前等待及反压在现有模型中的表达，不宜继续仅靠对 UNKNOWN parent 追加等待来修复。

尚未证明：这些差异可解释全部 CPI 残差；具体有多少来自返回偏早引起的排队减少、多少来自两阶段反馈移动 dispatch、或其他因素。本轮没有实现新的时序修复或推进默认。

## 采集、身份与人口

- 新来源：`tmp/graph500-native-lifecycle-20261117/sources/c04-854.graph500_s`。同一 APSP workload ELF/参数、4 个参与核、无额外核、32GiB，原生硬件预取和 `sprEarlyStorePrepare` 均关闭。
- warmup 目标为 500,000 user instructions，将测量目标缩为最快核 50,000 user UOP；core2 触发共同终点，所有核保留实际完整人口。core0 测量段为 49,359 UOP / 38,390 宏指令。
- 原生 collector 复用已有冻结 gem5 ELF，SHA-256 `7adee6210b02bdfa5a5f9d81c98c260bc91b59b9f1821b542673c45779a46402`；启用现有 `emit_native_response_jsonl` 和上游 `IQ` debug，不重新构建或改变模拟服务机制。
- 旧 staged checkpoint 的身份不匹配当前采集配置，未绕过校验；本次从 KVM 启动并切到 O3。kernel/disk 的旧缓存路径失效后，找到了 SHA-256 与历史资源相同的文件；失败启动保存在 `attempts/01-missing-resources`。
- `IQ` 日志在首次 stats reset 后开启，实际包含详细 warmup 和测量；分析只选择本轮测量中退休的对应成员，因此可保留跨边界已入队 load 的真实 IQ 入队时间。日志不是仅测量段日志，不能据其大小推算测量吞吐。
- 原生全量 response sideband 标为 `oracle_only=true/fst_input=false`，其 metadata 明确接收/返回 tick 语义。模型输入仍只有本轮功能 FST 及原有功能伴随；没有将 IQ、Ruby 回调或其他 O3 时间输入 FastSim。
- 源准备使用维护的 `tools/prepare_spr_capture.py`；独立硬件身份检查 valid=true。采集 storage receipt 正常完成，原生目标 drain 完成，未触发容量/磁盘限制。

### 逐条联结

core0 共 16,454 个测量期用户访存 UOP，本轮全部为单片段 load。native commit 数、功能 FST load 数及模型审计数均为 16,454；无缺项或多余成员。

先按同一 core 的原生退休序与功能 FST 退休序联结完整访存人口，再逐条用 IQ PC 校验；原生 16,454 个 PC 均与功能 FST 一致。模型按 source ordinal 与物理缓存行核对，集合完全相等。**模型审计 CSV 的 PC 字段未收集、仍为 0；没有宣称模型 PC 也被直接核对。**

所有原生成员满足：

```text
IQ insert ≤ first issue ≤ last issue ≤ successful acceptance
          ≤ native response ≤ IQ release
```

没有缺失 IQ admission/release 或首次 issue。全量配对行保存在 [paired-summary.pairs.jsonl](../tmp/graph500-native-lifecycle-20261117/paired-summary.pairs.jsonl)，不只保留平均值或选定样例。

## 事件语义

- 原生 IQ 插入日志紧邻 `InstructionQueue::insert()` 的实际插入；`Completing mem instruction` 日志紧邻 `clearInIQ()`。本轮使用它们的 tick 差，而非 O3PipeView 中含义不同的 complete 字段。
- 原生 acceptance 来自 Sequencer 成功入表，response 来自其 hit callback；native first issue 来自 IQ 发射事件。首次执行尝试到成功接收包含执行、地址/翻译、重试等过程，**不能全称为纯 Sequencer 阻塞**。
- 模型 IQ 释放字段与实际 calendar 写入的 `max(actual_completion, memory_response_cycle)` 一致，Sequencer 释放与该请求响应一致。
- 以下 leader/merged 分组由**原生**服务角色决定；模型比较的是完全相同的功能成员，不保证模型也将它们分类为 merged。
- 原生 CPU/Ruby clock period=385 ticks；原生 tick 差除以该值，比较模型 core cycles。

## 配对生命周期结果

以下都是同一成员集合的周期均值，不是各 provider 自选命中/合并人口的均值。

| 原生分组 | 成员数 | 阶段 | gem5 周期 | FastSim 周期 |
|---|---:|---|---:|---:|
| leader | 5,676 | IQ 入队→成功接收 | 130.284 | 29.508 |
| leader | 5,676 | 成功接收→响应 | 14.998 | 12.521 |
| leader | 5,676 | 响应→IQ 释放 | 1.002 | 1.639 |
| leader | 5,676 | IQ 总寿命 | 146.284 | 43.668 |
| merged | 10,778 | IQ 入队→成功接收 | 122.017 | 28.157 |
| merged | 10,778 | 成功接收→响应 | 45.037 | 23.127 |
| merged | 10,778 | 响应→IQ 释放 | 1.075 | 1.679 |
| merged | 10,778 | IQ 总寿命 | 168.129 | 52.964 |

merged 组 IQ 寿命相差约 115.165 周期，其中约 93.859 周期差异发生在成功接收之前。这个可加分解只针对**每成员 IQ 生命周期的阶段差**，不能解释成约 81.5% 的 CPI 残差。

原生进一步分解：leader 从 IQ 入队到首次 issue 为 27.561 周期，首次 issue 到成功接收 102.723 周期；merged 分别为 24.317 / 97.700 周期。平均 issue attempts 分别 3.431 / 3.151，仍不能把多次尝试直接换算成额外周期罚时。

### 分布而非均值单点

| 原生 merged 组阶段 | gem5 p50 / p95 | FastSim p50 / p95 |
|---|---:|---:|
| IQ 入队→成功接收 | 127 / 194 | 5 / 88 |
| 成功接收→响应 | 2 / 150 | 2 / 145 |
| IQ 总寿命 | 181 / 273 | 13 / 155 |

接收→响应中位数相同，不能据此认为服务时序无差异；全体生命周期、分布尾部与队列持有共同表明偏差。完整 p50/p95/p99/max 及全量成员见 [paired-summary.json](../tmp/graph500-native-lifecycle-20261117/paired-summary.json)。

## 并发成员及独立 miss 行

| 本轮已提供退休 load 子集 | gem5 平均 / 峰值 | FastSim 平均 / 峰值 |
|---|---:|---:|
| IQ 内并发成员数 | 70.324 / 72 | 24.992 / 72 |
| Sequencer 内并发成员数 | 15.214 / 16 | 9.779 / 16 |
| 未返回的独立 demand-miss 缓存行 | 1.868 / 3 | 2.098 / 5 |

平均值按各 provider 的 common measurement window 对半开生命周期区间积分：原生 37,413 周期，模型 32,759 周期。不是用 active CPI 分母；模型 core0 active=31,324 周期，不能混成同一个窗口。

这些是同一退休 load 子集的并发成员数，**不是包含推测、被丢弃、其他访问的完整硬件占用**。未逐项认证 `allocationGroup/iqSlots`，不能将 70.324 直接除以 72 报物理 IQ 利用率，也不能称已认证硬件 MSHR 计数。

独立 miss 行只计相应已提供首请求的生命周期，同一行重叠区间先取并集，再做时间积分。原生层级来自 Ruby/SLICC `native_hierarchy`，没有使用 proxy_path_class。原生合并成员通过单核、单片段、同物理行、同 callback tick、唯一 leader 推导服务组；6 个成员的 parent 在观察人口之外，明确保留未知。这不是原生独立 request-ID 的完整父服务认证。

模型有更多独立 miss 行重叠，与并行度偏高的假设一致；平均并发还受各自窗口长度影响，不能把这项观察当作独立的 CPI 因果证明。

## 新短窗口 CPI 与探针中性

冻结 M4 与隔离审计 ELF 的完整 `time_accounting` 精确相同，records、memory_accesses、ruby_sequencer_requests、dtlb_accesses 也一致。审计 ELF 只增加模型内观测和样本上限，没有启用 preview 字段候选或 UNKNOWN 等待。

| 新来源 | gem5 CPI | M4 / probe CPI | 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|
| Graph500 C4，50k-user-UOP 共同终点短窗 | 0.973351 | 0.814205 | -16.350% | 0.159146 |

单例 MAE=0.159146 cycles/macro，MAPE=16.350%。CPI 按 workload-task active / retired macro 统计，终点策略为 `retired-stream-completion-v1`。该短窗与此前 10M-user-UOP 来源不同，不能与旧 C4/C8 表混成修复前后结果或汇总。

维护 quick 评分两组均 1/1 CPI scored，无 failed/pending，`formal_validation=false`；没有重新运行完整严格 FST/source hash 审计、正式 PMU 或硬件等价验收。探针中性是该次同输入计时账户中性证据，不是全配置正确性证明。

## 实施、验证与后续方向

- 新增维护工具 [analyze_load_lifecycle.py](../tools/analyze_load_lifecycle.py) 与 [focused tests](../tests/test_load_lifecycle_audit.py)。覆盖 IQ 事件、首次 issue、native metadata/footer、人口/PC/物理行错配、context 冲突、未知 IQ 边界、阶段分解及半开/同线区间并集。
- 关键测试均保存 RED→GREEN 收据；最终 10 项 focused Python 测试通过。隔离 FastSim Release/LTO16 构建完成，真实同输入 M4/probe 中性比较和维护 quick 评分完成。没有运行无关的全套 C++ 回归。
- 独立只读复核确认本轮人口联结、IQ/Sequencer 时间语义和模型中性；指出的 metadata、同人口占用范围、schema cap 已修正。占用仅采用双方相同的单片段 load 集合，extra/missing 单列；缺项不作为零寿命。
- 采集/回放使用高频 CPU 52–63、NUMA1、既有 performance 策略和共享 FST reservation。采集全过程约 212 秒，包含启动/warmup/调试输出，不作为吞吐测量。所有诊断文件均在项目 tmp，未删除既有证据或改变全局环境。

本轮足以选择后续方向：**优先定位并改善 load 接收前的 IQ 持有与容量反压近似，而不是继续增加完整 FU 重发日历或统一 UNKNOWN 等待。** 在实现候选前仍需确认：当前反馈究竟将这段等待移到了 IQ 分配之前，还是因上游返回/接收过快而没有产生这段排队。两者不能只靠增加固定 IQ 持有罚时区分。

安装 M4、默认模型和固定延迟保持不变。本轮没有新的 CPI 修复收益、C8 结论或吞吐验收；已获得的是此前缺少的直接配对诊断证据。
