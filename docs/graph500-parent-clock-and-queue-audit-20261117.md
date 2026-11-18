# Graph500 parent 时间字段与队列生命周期逐条审计

2026-11-17，接续 [服务时序诊断](graph500-service-cpi-diagnosis-20261116.md)。同一诊断目录跨日继续使用：`tmp/graph500-service-diagnosis-20261116/`。

本轮发现并复现了一处具体错误：普通 load 的私有 preview 命中绕过共享 replay 时，只保存 latency，丢失模型选定的服务起止时间。L2 data-fill 后续被 parent 认证与真实 admission 比较时，拿到的是默认 `(0,0)`，从而产生原本不应因字段遗漏而发生的 fallback。隔离副本补齐字段后，一条同线 load 的漏等确实消失，但总体 CPI 收益很小，不能将这处错误写成主要残差已解决。

## 原因与实际源码链

1. `preview_memory_event()` 获得私有 L1/L2 lookup 结果，填充 `private_level`、`latency_cycles` 和 exposed latency，没有填充 `shared_stage_issue_cycle/shared_response_cycle`。
2. `materializes_shared_event()` 对普通私有 L2 命中返回 false（有 L2 eviction 等情况除外）。`run_interval_weave()` 的 `preview_epoch` 分支只补 PMU 分类，直接保留 preview 回执，因此也没有经过会填写起止时间的 `replay_previewed_memory_event()`。
3. `resolve_parent_service()` 将非 L1 普通 load 视为 data-fill，比较最终 admission 与选定服务起点。对上述回执，选定起点和响应字段仍为 0，于是误归入 `admission_mismatch`。
4. 后继 local hit 找到 UNKNOWN parent 后走 fallback；其本地响应继续进入 completion、IQ 和 Sequencer 释放链。不是队列把已有的本请求响应丢掉，而是其上游响应边没有继承这部分 parent 数据等待。

0 在模拟起点可以是合法时间，本轮没有把 0 定义成全局无效哨兵。这里 measurement 已有超过 70 万周期的模型时间，结合明确未赋值的源码路径及同一事件的实际起点，才确认 `(0,0)` 是缺失字段。

这份证据不支持把以前全部 83%/85% admission_mismatch 都解释成真实的阶段时间位移。它混合了字段遗漏和真实位移两种情况，必须分开。

## 审计方法与样本边界

- 冻结 M4、原始 C4/C8 来源、功能 warmup、完整测量人口和退休终点；默认 ELF/config/生产源码均未改动。
- 探针只在隔离 source/build 中工作；每例记录 core0 测量段前 16,384 个最终提交的普通单片段 load。proposal 中的行随反馈事务丢弃，只有 commit 后写入 CSV。逐例检查 sequence 无重复。
- 字段包括功能 sequence、物理行、lookup 层级、选定服务起止、最终 admission、parent ID/sequence/质量、parent 等待前后响应、completion/retire、IQ 与 Sequencer 释放。
- 同线联结同时匹配 parent ID、parent sequence 与物理行，避免将其他代次误配为 parent。CSV 的直接 PC 字段未收集；示例 PC 从原始功能 FST 按 source_sequence 独立读取，并核对物理行。不是 native timing 输入。
- schema：[load-edge-schema.json](../tmp/graph500-service-diagnosis-20261116/load-edge-schema.json)；完整样本分析：[reviewed-load-edges.json](../tmp/graph500-service-diagnosis-20261116/reviewed-load-edges.json)；功能 PC 联结：[functional-witness-pcs.json](../tmp/graph500-service-diagnosis-20261116/functional-witness-pcs.json)。
- 原生来源的 `full_jsonl_enabled=false`，本轮没有同成员的原生 request→callback 时间记录。以下逐条时间证据属于模型内部时序与功能身份，不能报告成“真实 CPU 提前返回多少周期”。

| core0 提交样本 | C4 | C8 |
|---|---:|---:|
| 普通 load 总样本 | 16,384 | 16,384 |
| L2 首请求样本 | 341 | 342 |
| 原路径 L2 服务时间字段缺失 | 328 | 337 |
| 补字段后 L2 时间字段缺失 | 0 | 0 |
| 原路径同代次 parent 联结 | 8,124 | 8,061 |

这里是有限前缀的样本比例，不外推成整窗口字段缺失率。

## 一条完整的模型内证据

C4 core0，物理行 `542570168`。原始功能 FST 记录 844113 / 844122 / 844131 均为 PC `0x405c60`，地址依次为 `0x815bdae00/04/08`，属于同一条 cache line。

| 模型事件 | sequence | 最终接收 | 原响应 | 补字段后响应 | 原 IQ 释放 | 补字段后 IQ 释放 |
|---|---:|---:|---:|---:|---:|---:|
| L2 parent | 844113 | 722351 | 722364 | 722364 | 722365 | 722365 |
| 同线后继 load | 844122 | 722355 | 722357 | 722366 | 722358 | 722367 |
| 同线后继 load | 844131 | 722359 | 722361 | 722366 | 722362 | 722367 |

原 parent 的数值 data_ready 已是 722364，但 source quality=UNKNOWN，因为选定服务字段为 `(0,0)`。补字段后，该 parent 的选定起止成为 `(722351,722364)`，满足现有资格条件；后继变为 certified follower，使用已有 gate+local-path 规则，故响应是 722366。没有修改 13 周期 L2 延迟或 2 周期本地路径。

原与补字段版本中，两个 case 的全部 16,384 行均未发现以下自身时序违规：Sequencer 在本请求响应前释放、IQ 在本请求响应前释放、completion 早于响应+wakeup、retire 早于 completion。记录的是写入模型 calendar 的自身释放边，不是原生占用采样，也不构成完整槽位占用守恒证明；不证明跨服务 parent 资格完整。

## 最小隔离反事实：只补两条现有模型边

`FASTSIM_PREVIEW_CLOCKS=1`，仅对非 carried 的普通私有 preview load，保留：

```text
selected_admission = pending.issue（既有模型 reference clock）
selected_response  = selected_admission + 既有私有接收到响应时长
```

沿用原来的 local/reference 转换、admission-delay 拆分、parent 资格、容量和 resource 约束。没有放宽门禁，没有增加 shared replay，没有输入 gem5 时间，没有调延迟、容量或重试倍率。

实际全输入数据合同检查在原路径 RED：C4 的 341 个 L2 服务有 328 个缺少起止；补字段 C4/C8 均 GREEN。收据为 `phase6-red.json` / `phase6-green-*.json`。这是当前真实输入上的诊断回归，尚未转成生产单元测试或默认修复。

| 完整窗口统计 | C4 原→补字段 | C8 原→补字段 |
|---|---:|---:|
| certified services | 92,466 → 182,926 | 168,143 → 341,221 |
| certified followers | 1,271,140 → 1,476,846 | 2,374,397 → 2,744,156 |
| source fallbacks | 4,980,173 → 4,712,826 | 10,154,909 → 9,679,456 |

资格覆盖明显增加，但增加的重叠等待量不等于净 CPI 贡献。C8 wait_cycles 总和甚至下降，仍不能逐项相减解释 active cycles。

## 完整输入 CPI：小幅改善，主要残差仍在

维护 quick 入口复用保存的独立 BaseCPU task-clock 证据，逐核 task macro 人口相同，`retired-stream-completion-v1` 终点一致。原路径审计输出完整 `time_accounting` 与 M4 精确一致；两组各 2/2 scored，无 failed/pending。native PMU 严格审计和硬件等价性未认证。

RE=(FastSim−gem5)/gem5，AE=abs(FastSim−gem5)，AE/MAE 单位 cycles/macro。

| 来源 | gem5 CPI | 对照 | FastSim CPI | RE | AE |
|---|---:|---|---:|---:|---:|
| C4 | 0.966422 | M4 / 原路径审计 | 0.839507 | -13.132% | 0.126915 |
| C4 | 0.966422 | 仅补 preview 时间字段 | 0.839796 | -13.103% | 0.126626 |
| C8 | 0.969791 | M4 / 原路径审计 | 0.851812 | -12.165% | 0.117979 |
| C8 | 0.969791 | 仅补 preview 时间字段 | 0.854475 | -11.891% | 0.115317 |

| 两例等权 | CPI MAE | CPI MAPE |
|---|---:|---:|
| 原路径 | 0.122447 | 12.649% |
| 仅补字段 | 0.120971 | 12.497% |

补字段只消除了约 1.2% 的原 MAE，绝大多数残差仍在。不能把 parent 覆盖翻倍称为 CPI 精度问题已经修复，也没有新吞吐验收结果。

## 剩余疑点：下层服务时间与最终接收的位移

补字段后，同一模型样本仍有数值响应早于同代次 UNKNOWN parent.data_ready 的成员：C4=5,659、C8=5,645。其中下层服务 parent 分别 3,294/3,370，L2 parent 为 2,365/2,275。UNKNOWN 数值不能当作物理真值，这些不是正式“漏等次数”，而是需要继续解释的时序关系。

例如 C4 parent sequence=844260、line=542442842：选定下层服务从 722415 到 722608，最终 CPU admission 却是 722546，模型把同样 193 周期服务整体平移到 722739。后继 sequence=844269 在 722546 接收，local 响应为 722548、Sequencer 同时释放。此处字段完整，问题不同于 preview 字段遗漏，而是下层服务选择与最终队列反馈的关系。不能不加区分地使用 UNKNOWN ready 强制等待；上一轮已见这种处理严重过计。

对这一疑点的分路径诊断保留已补字段，分别只等待未认证的纯私有 L2 parent、只等待未认证的下层服务 parent。分组使用 parent 自身持有的来源类别，不借用 native 结果；future parent 和特殊/未知类别继续排除。这是路径敏感性的反事实，不是宣布其中一组数值已是正确因果修复。各组 CPI 差异不能相加。

## 复现与身份

所有命令、配置、CPU mask、环境、输入与输出路径在 `run_phase5.py` / `run_phase6.py` 及逐例 command/receipt 中。CPU 52–63、NUMA1，既有 performance 策略，未更改全局环境。probe ELF 与源码分别归档在 `phase5/`、`phase6/`，最终 source SHA 见对应 `phase*-source-identity.json`；初轮诊断编译的 lambda capture/scope 错误及修正日志亦保留，未计为通过测试。

当前工作只在隔离副本、诊断脚本和本文中；安装 M4、默认 profile、固定延迟和生产模型没有改变。对吞吐没有作新通过结论，不将有探针的并行运行时间混入历史配对吞吐样本。

## 分路径反事实结果

已补齐 preview 字段，其余模型与输入一致。两组各 2/2 quick scored，无 failed/pending；没有将反事实升级为正确时间或默认修复。

| 来源 | gem5 CPI | 对照 | FastSim CPI | RE | AE |
|---|---:|---|---:|---:|---:|
| C4 | 0.966422 | 仅等待 UNKNOWN 纯私有 L2 | 0.838242 | -13.263% | 0.128180 |
| C8 | 0.969791 | 仅等待 UNKNOWN 纯私有 L2 | 0.849612 | -12.392% | 0.120179 |
| C4 | 0.966422 | 仅等待 UNKNOWN 下层服务 | 1.965474 | +103.376% | 0.999051 |
| C8 | 0.969791 | 仅等待 UNKNOWN 下层服务 | 2.072269 | +113.682% | 1.102478 |

| 两例等权 | CPI MAE | CPI MAPE |
|---|---:|---:|
| 仅补字段 | 0.120971 | 12.497% |
| 仅等待 UNKNOWN 纯私有 L2 | 0.124180 | 12.828% |
| 仅等待 UNKNOWN 下层服务 | 1.050765 | 108.529% |

仅等待纯私有 L2 的精度未改善；只等待下层服务 parent 已重现上一轮的严重过计。实际应用数分别为 C4 L2=1,752,598 / 下层=3,468,675，C8 L2=3,536,398 / 下层=6,922,938，不是开关没有触发。这里源类型跟随 parent 自身保存，prepare/特殊/未知类别及 future parent 没有混入指定等待路径。

这一结果定位了“全部等待为何会过计”的主要敏感路径，但不能反过来证明所有原始 CPI 缺口都由这一路造成。下层资源在选定时间排程，CPU 反馈则把服务时长移到最终 admission，同时部分同线 local 成员未继承数据等待；两套时间混用的问题不能用统一 UNKNOWN 等待闭合。应该继续核对服务代际及共享服务起点与 CPU 成员占用的一致性，而不是按 CPI 调整等待系数。

本轮实际修复收益仅来自隔离副本的字段补齐；生产修复、默认推进与吞吐验收均未进行。全精度小数和状态见 [phase5-7-reviewed-accuracy.json](../tmp/graph500-service-diagnosis-20261116/phase5-7-reviewed-accuracy.json)，分路径命令/环境/ELF/收据见 phase7-freeze.json、phase7-receipts.json 和 run_phase7.py。
