# Graph500：私有 preview 时间字段修复与近似 parent 时间设计

日期：2026-11-17。本文对应“接收时刻相等条件是否过严、私有 preview 字段遗漏能否修复”。修复只涉及已存在服务的时间字段；近似 parent 时间是后续设计，未启用。

## 两种原因应分别处理

当前 `resolve_parent_service` 要求最终 CPU admission 与选定服务起点加 admission delay **严格相等**，才将该服务作为 certified parent。这个条件用来声称来源时间完全一致是合理的；将它同时作为所有近似等待的唯一入口，则覆盖过窄。资源反馈能将最终 CPU admission 推迟，而选定的下层服务仍在第一阶段的时间轴上，事件身份和服务时间质量不应因此混为一谈。

另一类是确定的字段遗漏：普通私有 L2 preview 命中不进入共享回放，原路径只保留 latency，没有填 `shared_stage_issue_cycle` 和 `shared_response_cycle`。因此一个本来能算出时间的服务被拿 `(0,0)` 与最终 admission 比较，失去严格资格。它不是输入缺少退休指令，也不是需要猜测新的 L2 延迟。

## 本次实际修复

在非 carried 的普通私有 preview load 分支，补上：

```text
selected_issue_ref = ceil(pending.reference_issue_q16)
local_service = existing_private_response_latency - existing_admission_delay
selected_response_ref = selected_issue_ref + ceil(to_reference(local_service))
```

时钟转换和 admission-delay 拆分与 `replay_previewed_memory_event` 一致。较早的隔离诊断在等频输入中使用 core-local 起点；本次生产源码使用明确的 reference 起点。现有 unified parent 配置仍要求核心与 reference 等频，不放宽配置合同。

没有第二次 cache lookup、共享回放、全窗口迭代，也没有修改任何固定延迟、容量或重试参数。forwarded、atomic、写、PTE、ifetch 和 carried 请求不走新增字段分支。严格 parent 来源资格不变；最终 admission 真正发生位移的服务仍可为 UNKNOWN。

## 剩余 UNKNOWN 应怎样近似

不能仅将 `admission == selected` 改为 `>=`，也不能直接将 UNKNOWN 数值 `data_ready` 当作物理填充时间。当前该数值可来自已平移的 CPU response，包含请求自身队列反馈；直接让后继等待，容易把同一行的请求反复当成新服务，并将等待再传播回资源占用。此前“只等待 UNKNOWN 下层服务”反事实已明显过计，详见 [原始诊断](graph500-parent-clock-and-queue-audit-20261117.md)。

推荐在既有两阶段框架内分开两件事：

1. **服务身份**：维护每核、每行的有界在途代次和 owner。一个未完成代次内的同线后继复用同一个 owner，不因后继自身 admission 位移重新创建/平移整条服务；写、失效、特殊访问和跨边界不明身份应中止或降级联结。
2. **时间质量**：保留 Certified，增加显式 Estimated；无法确认服务身份的才保留 Unknown。Estimated 必须有单独的覆盖、等待和回退计数，不伪装成 strict 来源认证。
3. **纯私有服务**：在已有参数下从 owner 的最终接收起计算一次服务完成时间；后继只等待该 owner 的剩余服务，再走自身已有本地返回路径。不能对每个后继重新加完整 L2 时长。
4. **下层服务**：拆开选定排队、实际服务、返回路径与 CPU 请求自身等待。按现有 cache/互连/DRAM 模型字段建立一个有界 owner 完成估计；无法可靠分解的类别先回退，不能整体移动包含排队的 CPU response，也不能把 native OoO 时间作为输入。
5. **吞吐边界**：复用现有有界 parent 索引，每次请求常数次查询/更新，单次前向反馈；不逐周期，不创建独立详细运行时，不进行反复整窗口求解。

这是待实施设计，不是本次已生效的新机制。目标是减少“可估算但未严格认证”的覆盖缺口；UNKNOWN 比例下降本身不代表 CPI 改善。后续需分别报告身份可用率、Certified/Estimated/Unknown、提前返回关系、CPI AE/MAE 与同输入吞吐。

## 回归与隔离

新增真实 Simulator 回归：L1 只有一行，先读取 A、再读取 B 将 A 挤出 L1，随后重读仍在 L2 的 A。测试显式启用 private preview、设置 `domain_min_events=1`，检查 preview 确实执行且恰有一个 L2 hit，再检查非零起点、已有 L2 服务时长，以及与关闭 preview 的规范回放起止时间相同。

原路径在精确断言 `private-preview L2 hit lost its selected service issue clock` 处 RED。先前编译字段名错误、缺 GCC11 runtime 路径、未达到 preview 最小 batch 的 fixture 都保留日志，不计为有效 RED。额外尝试的异频用例被现有 unified parent 配置拒绝，未绕过此限制。

所有构建、ELF、日志和回放收据位于 `tmp/private-preview-clock-fix-20261117/`；源码前件在 `before/`，隔离构建在 `build/`。默认安装 ELF 和冻结 M1–M4 配置不变，不进行默认推进或吞吐验收。

## 新候选的快速验证结果

本轮使用不含旧诊断探针的普通 Release / LTO16 隔离构建。候选 ELF SHA-256：`d7ea6df219fe440d80967a8665efe6d2067d3eb7f7a58ba26c7b5b4dec582d0c`。修复前源码构建的三个同输入重放，其完整 `time_accounting` 均与冻结 M4 一致。修复后完整 C4/C8 各一例、另一个新采集短 C4 一例，全部经维护的 quick validator 完成诊断评分，无 pending/failed；输入请求人口、task macro 逐核人口及退休终点合同一致。短 C4 不与旧完整窗口合并。

以下 RE=(FastSim−gem5)/gem5，AE=abs(FastSim−gem5)，AE/MAE 单位 cycles/macro。

| 窗口 | gem5 CPI | 版本 | FastSim CPI | RE | AE |
|---|---:|---|---:|---:|---:|
| 完整 C4 | 0.966422 | 修复前 | 0.839507 | -13.132% | 0.126915 |
| 完整 C4 | 0.966422 | 补字段 | 0.839796 | -13.103% | 0.126626 |
| 完整 C8 | 0.969791 | 修复前 | 0.851812 | -12.165% | 0.117979 |
| 完整 C8 | 0.969791 | 补字段 | 0.854475 | -11.891% | 0.115317 |
| 新短 C4 | 0.973351 | 修复前 | 0.814205 | -16.350% | 0.159146 |
| 新短 C4 | 0.973351 | 补字段 | 0.814205 | -16.350% | 0.159146 |

| 完整 C4/C8 两例等权 | CPI MAE | CPI MAPE |
|---|---:|---:|
| 修复前 | 0.122447 | 12.649% |
| 补字段 | 0.120971 | 12.497% |

### UNKNOWN 的分母

这里统计 load/fetch_shared **新服务来源候选**中的未认证比例，不是所有 load 的比例，也不是缺失 trace 的比例。C8 的候选数随反馈时序略有变化，逐版本展示真实分母。

| 窗口 | 修复前 UNKNOWN / 候选 | 修复后 UNKNOWN / 候选 | 比例变化 |
|---|---:|---:|---:|
| 完整 C4 | 464,223 / 556,689 | 373,763 / 556,689 | 83.39% → 67.14% |
| 完整 C8 | 944,659 / 1,112,802 | 771,617 / 1,112,838 | 84.89% → 69.34% |
| 新短 C4 | 2,256 / 2,742 | 1,844 / 2,742 | 82.28% → 67.25% |

覆盖率提升没有转成同等 CPI 收益。完整窗口的 MAE 仅下降约 1.21%；新短 C4 的来源覆盖增加，但 CPI 完全未变。这进一步说明该字段遗漏能修、应修，却不是主要 CPI 残差的完整解释。

### 主机成本边界

两轮均经高频 wrapper 绑定 socket/NUMA1 的 CPU52–63，重放之间使用不重叠的 52–57 / 58–63 mask。wrapper 启动频率快照记录在 engine.log；没有连续频率遥测。每轮均确认 `HOST_VALIDATED_PRELOAD=true`，输入准备、初始化、warmup 和测量分开记录。单次配对的完整 C4 测量 wall 为 20.389→20.565 s（+0.86%），C8 为 41.635→42.262 s（+1.51%）；不能将一次配对的波动作为正式吞吐验收。process wall 分别约 78.60→78.94 s、156.51→156.67 s，包含大量非测量成本，不能混用其分母。

### 验证状态与复现

- `fastsim_tests` 和 `fastsim_parent_service_tests` 均通过；RED/GREEN 与既有异频限制的说明见 [ledger](../tmp/private-preview-clock-fix-20261117/ledger.md)。
- 新上下文独立只读审查无阻塞项，边界为等频、零 admission-delay 的直接回归；其他转换/特殊路径为静态审查。见 [review](../tmp/private-preview-clock-fix-20261117/review.md)。
- 命令、CPU mask、输入/config/ELF 身份见逐例 `command.json` / `receipt.json`；维护 validator 的 full/short quick 摘要在 `scoring/`。
- 全精度结果、来源分母和主机 wall：[reviewed-results.json](../tmp/private-preview-clock-fix-20261117/reviewed-results.json)。源码 patch、已运行测试与默认身份检查：[verification-final.json](../tmp/private-preview-clock-fix-20261117/verification-final.json)。
- quick 复用保存的原生时间证据，不是重新完成 native PMU、来源严格审计、硬件等价性或吞吐认证。Estimated 路径尚未实现/启用。
- 安装 M4 ELF SHA-256 仍为 `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9`；冻结 profiles 与修复前快照逐文件一致。本次没有安装新候选或改变默认配置。
