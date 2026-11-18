# Graph500 服务时序与 CPI 残差：后续诊断

2026-11-16。接续 [最初诊断](graph500-cpi-diagnosis-20261116.md) 与 [失败版本回退/稀疏候选验证](spr-sparse-load-pressure-validation-20261116.md)。

后续更新：逐条审计已发现私有 preview 路径遗漏服务时间字段，见 [parent 时间与队列审计](graph500-parent-clock-and-queue-audit-20261117.md)。因此下文 admission_mismatch 不能全部解释成实际时间位移；“原始选定服务均值”还混入缺失字段的零跨度，不能作为有效服务均值。其余历史反事实结果保留。

本轮没有证明唯一根因。证据不再支持把“大量 load 重发”直接升级为主要 CPI 损失。新增审计把关注点缩到同线 load 的返回、队列持有与两阶段时间映射；统一 miss 服务太短、L1 少一拍这两种简单解释均缺少端到端支持。不能把“parent 未认证”直接改成“按其数值时间等待”。

## 输入、模型与验证边界

- 使用相同 Graph500 APSP C4/C8 完整来源、功能 warmup 和退休终点；不是 BFS。来源路径与原始命令冻结在 [freeze.json](../tmp/graph500-service-diagnosis-20261116/freeze.json)。
- 当前安装 M4 SHA-256 仍为 `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9`，默认配置、生产模型和缓存/DRAM 固定延迟均未修改。诊断副本位于 `tmp/graph500-service-diagnosis-20261116/probe-source`。
- 第一阶段探针与第二阶段聚合探针各有补丁前后哈希；第一版探针 ELF 与改动文件归档在 `phase1/`。各次 ELF、命令、配置、环境和输出另有 freeze / receipt。探针只使用现有模型时间，不读取 gem5 issue、stall 或 callback 时间作为输入。
- `observe` / `audit` 与 M4 的完整 `time_accounting` 精确一致；各成功回放检查 macro 人口及 records、memory_accesses、ruby_sequencer_requests、dtlb_accesses 相同。
- 维护 quick 入口复用保存的独立 BaseCPU task-clock 审计；observe、wait、audit、L1 对照和 parent-off 各 2/2 CPI scored，failed/pending=0。PMU 当前严格审计与硬件等价性未认证。原生 Ruby 直方图只作 raw 诊断，窗口与正式退休终点略有差别。
- 并行诊断使用 CPU 52–63 的不重叠 mask，经过 `run_high_frequency.py`。本轮未测量或验收吞吐，不将带探针的并行运行时间报告成吞吐结果。

## CPI 对照

RE=(FastSim−gem5)/gem5；AE=abs(FastSim−gem5)，单位 cycles/macro。没有通过修改固定延迟拟合或推进默认。

| 来源 | gem5 CPI | 对照 | FastSim CPI | RE | AE |
|---|---:|---|---:|---:|---:|
| C4 | 0.966422 | M4 / observe / audit | 0.839507 | -13.132% | 0.126915 |
| C4 | 0.966422 | 仅关闭 parent | 0.755329 | -21.843% | 0.211093 |
| C4 | 0.966422 | 未认证 parent 数值时间强制等待 | 1.968231 | +103.662% | 1.001809 |
| C4 | 0.966422 | L1 hit_latency 2→3，仅诊断 | 0.840361 | -13.044% | 0.126061 |
| C8 | 0.969791 | M4 / observe / audit | 0.851812 | -12.165% | 0.117979 |
| C8 | 0.969791 | 仅关闭 parent | 0.762760 | -21.348% | 0.207031 |
| C8 | 0.969791 | 未认证 parent 数值时间强制等待 | 2.029877 | +109.311% | 1.060086 |
| C8 | 0.969791 | L1 hit_latency 2→3，仅诊断 | 0.850813 | -12.268% | 0.118978 |

| 两例等权 | CPI MAE | CPI MAPE |
|---|---:|---:|
| M4 / observe / audit | 0.122447 | 12.649% |
| 仅关闭 parent | 0.209062 | 21.595% |
| 强制等待 | 1.030947 | 106.486% |
| L1 一拍对照 | 0.122519 | 12.656% |

评分与完整小数见 [reviewed-accuracy.json](../tmp/graph500-service-diagnosis-20261116/reviewed-accuracy.json) 及 [scoring](../tmp/graph500-service-diagnosis-20261116/scoring)。

## 首个 miss 的服务时间没有表现为整体偏短

原生 `Sequencer::recordMissLatency` 使用 `curCycle()−issue_time`；`readCallback` 对同线 list 只在 `ruby_request=true` 的首个成员采样一次，随后 alias 成员仍逐个 callback，但不进入这张延迟直方图。Ruby、L0 controller 和 O3 均绑定 `board.clk_domain`，本批 period=385 ticks，因此这里 cycle 单位一致。

FastSim 聚合探针位于 `resolve_parent_service`，只计普通非转发 load，在 parent 等待之前分别记录 local-hit 和 data-fill 的最终接收→响应；不将第一次 FU issue 与修正后接收混用。结果如下：

| 诊断分组 | C4 样本数 | C4 平均周期 | C8 样本数 | C8 平均周期 |
|---|---:|---:|---:|---:|
| gem5 external leader | 559,143 | 101.432156 | 1,117,340 | 101.832220 |
| FastSim 普通 data-fill，修正接收→响应 | 556,374 | 106.758776 | 1,112,309 | 110.586613 |
| FastSim 同批选定服务，原始服务起点→响应（混入缺失字段，统计作废） | 556,374 | 100.568720 | 1,112,309 | 104.174169 |
| gem5 local leader | 4,168,781 | 2.998838 | 8,355,932 | 2.998900 |
| FastSim 普通 local hit，parent 等待之前 | 12,769,065 | 2.000000 | 25,530,247 | 2.000000 |

原生 external 集还含不同访问类型，FastSim 分组是普通 load，且 raw 窗口略有差别；这不是同成员正式延迟精度评分，也不能排除某一层级或尾部服务错误。但总体值不支持“将所有 miss 加一个统一延迟即可解释 12%–13% 残差”。不能将 native leader 均值与 FastSim 全体 load 的修正 issue→completion 均值直接相除。

原生本地首请求约 3 周期，与保存配置中 mandatory_queue_latency=1、Dcache.dataAccessLatency=3 以及 `Sequencer::issueRequest` 的 `service += data_latency−1` 一致。FastSim 本地响应路径为 2 周期，另有 load_response_to_ready=1；两者 CPU wakeup 起点仍需独立映射。一拍参数对照的净 CPI 影响很小且 C4/C8 符号不同，说明这处差异并非本轮主要解释，也不授权把 3 写入默认。

## 更具体的时序疑点：未认证 parent 与同线返回

代码路径：`src/simulator.cpp::resolve_parent_service` / `include/fastsim/parent_service.hpp::ParentServicePool::decide`。

data-fill 只有在第二阶段最终 admission 等于选定服务起点、响应边单调、目标行可用且没有 carried 等条件下才获得 final-local 资格。条件不成立时，数值 admission/data_ready 仍被保存，但时间语义为 UNKNOWN。后续 local hit 若命中该 parent，当前路径增加 source_fallbacks，保留原有响应，不增加 parent 等待。这不是 gem5 版本或配置哈希禁用，而是两个模型时钟的当前资格不匹配。

| load/fetch_shared 来源决策 | C4 | C8 |
|---|---:|---:|
| accepted | 92,466 | 168,143 |
| admission_mismatch | 463,900 | 944,157 |
| target_unknown | 8 | 9 |
| unsupported_action | 315 | 493 |

约 83%/85% 的候选未通过接收时刻匹配。该事实说明默认 parent 路径覆盖有限，不证明这些成员都缺少物理等待。原始服务边是选定下层服务的时钟；最终 CPU admission 经现有响应反馈移动，两者不能在不解释映射的情况下强行当作同一物理服务时间。

增加的 observe 探针只对 local、非 carried 的 UNKNOWN 查数值时间：先排除 `parent.admission > admission` 的未来 parent，再检查 `data_ready > admission`。

| 审计项 | C4 | C8 |
|---|---:|---:|
| UNKNOWN parent 命中 | 4,980,129 | 10,154,851 |
| future parent，单独排除 | 783 | 1,566 |
| 非 future 且数值 ready 尚未到达 | 4,703,218 | 9,589,658 |
| 假设等待的重叠增量周期累计 | 554,462,306 | 1,176,595,978 |

这些累计数值严重重叠，不是可补回的 CPI 周期。`wait` 反事实使用 `max(response, parent.data_ready + response−admission)`，CPI 却增至 1.97/2.03。它把不具备可信物理边界的数值时间用于等待，改变后继接收、依赖和槽位反馈；因此不是有效修复。不能从 overshoot 推导“parent 没问题”，也不能通过按比例缩短等待把它拟合回参考 CPI。

仅关闭 parent 的受控对照使 C4/C8 CPI 降至 0.755329/0.762760，MAE 从 0.122447 恶化为 0.209062。说明现有 parent 等待已承担显著反馈作用，不能因为覆盖不完整就全部关闭；也不能从这组开关结果给尚未认证的成员分配净 CPI 贡献。

原生同线 alias 在首请求 callback 的同一次循环内返回；默认模型对已认证 follower 则在 gate 后继续计算原有 local 路径，未认证者保留 fallback。首请求、合并成员、本地 lookup 与 CPU wakeup 的边应分别检查。原生 coalesced 成员计数与模型 parent followers 的资格、访问类型不同，不能把两者的数量差直接记作丢失请求。

## 无效或受限对照也保留

- C4 将 interval_target_uops 改为 64、1024，与 256 的全部时序/审计结果一致。配置确实生效，但 `time_epoch` 分支采用 interval_max_cycles，target_uops 用在另一调度分支。因此这两个运行是未作用于当前批次划分的控制，不能作为“批次大小不影响误差”的证明。
- q64 最初评分错误地给了包含 C4/C8 的来源根目录，却要求 1 case；维护入口以非零状态拒绝。原目录保存在 `scoring/q64-wrong-discovery-preserved`。改用只含 C4 的链接来源根目录重新评分，q64/q1024 均 1/1 scored、failed/pending=0。没有因此重跑或删选模型结果。
- 尝试仅关闭 parent，再开启现有 `ruby.sequencer_line_coalescing`。后者在启动时因当前 buffer TSO 不兼容而被拒绝；代码也包含额外 transactional seed/replay，不能通过去掉校验或更换整个 store 模型来伪装成默认框架内的单变量试验。失败命令与日志保留在 `line-alias/`，不计入已验证精度。包含失败 job 的并发 driver 最终以非零结束，但两个 parent-off 回放均完成；各自成功收据保留，随后用维护入口单独评分，2/2 scored，无 failed/pending。

## 当前结论与仍缺的证据

已证实：两轮重试/成功带宽候选没有精度收益；新增普通 data-fill 服务均值并非整体偏短；L1 一拍对照不能改善两例总体误差；大部分 parent 候选在接收时刻匹配处退回，直接使用其数值 ready 又会严重过计。

尚未证实：同线返回与队列持有的哪一条边造成了多少 active CPI 残差。现有证据更适合优先检查这条链，而不是继续增加 retry 罚时或统一 miss 延迟。最终闭环需要同成员、同服务代际的接收/合并/返回、IQ 与 Sequencer 持有区间及其关键依赖；原生 first-issue 延迟、前端阻塞次数和模型等待累计不能互相替代。本轮不将疑点写成已定位且已修复的根因。
