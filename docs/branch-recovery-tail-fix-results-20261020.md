# 分支恢复间隔传播修复与同源验证（2026-10-20）

**后续语义修正：** [SQLite 剩余误差逐事件分析](sqlite-residual-cpi-analysis-20261020.md) 已确认，Decode 可确定的目标错误能在分支执行完成前恢复；后端解析错误则有另一条控制传播链。因此本页的 `completion + penalty` 通过仅认证现有模型内部尾部传播，不能作为所有原生误预测的通用时序下界。当前主结果不变；完整模型还需区分错误检测阶段。

本轮实施前一轮已经定位的恢复尾部丢失修复：[诊断与原生短窗口见证](branch-recovery-sqlite-zstd-results-20261020.md)。范围是既有两阶段 response 路径的因果传播，不是完整 redirect/squash 动态解析模型，也未解决当前批次 canonical cache 请求的重新准入。

## 1. 实现

- 第一阶段原有 `completion + branch.mispredict_penalty` 不变；当前两案配置仍为2。
- 第二阶段发布 `recovery_ready = max(existing_frontier, saturating_add(actual_completion, configured_penalty))`，每个模型误预测都建立完整恢复边；原有 response 延迟统计仍只记录实际延迟的分支。
- 后继 fetch 沿既有最大值约束合并恢复前沿、I-side 等待及前端容量，并传播到 rename/dispatch/issue/资源释放及退休，避免给每条后继指令重复加 penalty。
- 复用现有跨 checkpoint 状态保存与恢复，不改 FST。基础解析到恢复的固定硬件下界仍是原配置；实际恢复时刻继续受动态依赖和资源影响。
- 只修改 `src/simulator.cpp` 的前沿发布逻辑、配置注释和测试/探针。`core.response_branch_recovery` 默认仍为false，没有新延迟参数、负载特判或 gem5 时钟输入。

## 2. 机制和工程验证

新增 `tests/test_branch_recovery_tail.py` 调用真实模拟器，覆盖2-cycle尾部、另一配置值7、延迟后不重复计费、连续误预测、真实checkpoint边界、预测正确控制、前端已就绪控制、零尾部、generic/fast-kernel目标周期和PMU等价。测试中的7只是验证使用配置值，不是调优或生产参数。

- 最终回归测试对旧冻结二进制为 **9项中4项预期失败**，修复后二进制 **9/9通过**。
- `cmake --build build -- -j16` 成功；`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 基线和修复后均打印 `all FastSim tests passed`。构建的既有 serial LTRANS 提示保留在日志，未出现编译失败。
- 原分支见证分析单测3/3通过；维护探针新增完整尾部下界检查后，10项检查全部通过，generic/fast-kernel目标结果相等。
- 先前 SQLite 原生配对短源上的8192条审计记录、29对模型误预测，恢复尾部违规 **28→0**，早于解析的取指违规维持0。native逐分支预测位未独立配对，因此该计数认证模型自己的恢复边，不声称29个原生误预测身份已全部认证。
- 独立只读代码审核无阻塞项；已采纳补充建议，为延迟后的后继取指增加精确两拍检查，防止过度收费。

测试准备中发现 `chunk_instructions=2` 仍可能被time-epoch合并成一个checkpoint；最初跨chunk测试的边界断言因此失败。已改为Q=1并核验实际checkpoint不同。最终旧二进制RED4/9、修复GREEN9/9来自修正后的同一测试，不把错误的初始fixture当成实现缺陷。

## 3. SQLite / Zstd 同源CPI

四核标准rate副本、8通道、buffer114、实际DRAM时序、原始输入/共同终点/owner及各自宏指令分母均保持不变。新二进制分别执行开关off/on，共四次维护回放，全部source/native conservation/人口检查通过；两个独立硬件身份检查通过。无新采集，无FST复制或删除。并发回放只用于准确率，宿主计时不用于吞吐结论。

表中CPI均为 workload-task active cycles/宏指令；相对误差带符号，绝对误差及单案MAE为cycles/宏指令。相同负载的多模式不是新增独立case。

| 负载 | 模式 | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差 / 单案MAE |
|---|---|---:|---:|---:|---:|
| sqlite | 关闭，修复前后精确相同 | 1.000925 | 0.773267 | -22.7448% | 0.227658 |
| sqlite | 开启，修复前 | 1.000925 | 0.870686 | -13.0119% | 0.130239 |
| sqlite | 开启，修复后 | 1.000925 | 0.880284 | -12.0529% | 0.120641 |
| zstd | 关闭，修复前后精确相同 | 1.472610 | 1.342789 | -8.8157% | 0.129822 |
| zstd | 开启，修复前 | 1.472610 | 1.394890 | -5.2777% | 0.077720 |
| zstd | 开启，修复后 | 1.472610 | 1.391824 | -5.4859% | 0.080786 |

| 开启开关的两负载集合 | case数 | CPI MAPE | CPI MAE cycles/宏指令 |
|---|---:|---:|---:|
| before | 2 | 9.1448% | 0.103980 |
| fixed | 2 | 8.7694% | 0.100713 |

SQLite 开启模式补回226,425 active cycles；Zstd减少64,585 active cycles，相对误差绝对值增加约0.2082个百分点。整体两案MAPE和MAE改善，不代表两案均改善，更不代表跨负载准确率认证。

关闭时两案的完整目标输出与冻结旧输出递归一致；仅排除已声明的宿主timer、throughput及非确定的宿主frontier等待/驻留计数。开关开启时有效配置也没有变化，因此修复前后的数值变化来自模型实现，而非参数、来源或口径更换。见 [关闭中性核对](../tmp/branch-recovery-tail-fix-20261020/off-neutrality.json)。

## 4. Elapsed 与 native PMU诊断

| 负载 | gem5 elapsed ms | 开启修复前 ms | 开启修复后 ms |
|---|---:|---:|---:|
| sqlite | 1.966026 | 1.710226 | 1.729077 |
| zstd | 2.584096 | 2.447542 | 2.442168 |

下表为workload-task原生诊断计数，cache真值来自conserved Ruby/SLICC summaries，branch来自原始BPred。它不是退休load PMU或真机perf认证；现有退休cache资格unknown边界不因本补丁自动补齐。

| 负载 | native诊断事件 | gem5计数 | 开启修复前 | 开启修复后 |
|---|---|---:|---:|---:|
| sqlite | branch_misses | 136,580 | 136,679 | 136,679 |
| sqlite | l1d_tag_misses | 73,007 | 74,876 | 74,890 |
| sqlite | private_l2_tag_misses | 71,749 | 74,038 | 74,042 |
| sqlite | llc_tag_misses | 71,570 | 73,465 | 73,465 |
| zstd | branch_misses | 185,279 | 184,867 | 184,867 |
| zstd | l1d_tag_misses | 791,558 | 777,829 | 777,961 |
| zstd | private_l2_tag_misses | 170,779 | 199,499 | 199,329 |
| zstd | llc_tag_misses | 33,775 | 46,408 | 46,408 |

## 5. 为什么Zstd没有单调改善

这是完整的warmup+measurement回放，不是冻结访存服务下的单条边反事实。Zstd warmup barrier由1,978,298变为1,985,871 cycles；测量L1/L2 tag计数亦变化，说明预热状态/后续交错和服务选择发生变化。它们证明“仅在同一固定执行上加两拍”的解释不成立，但尚无阶段消融把净减少64,585 cycles独立归给warmup、cache交错、资源重叠或已有两阶段近似。不可把该差额当成这条恢复边应当删除的证据，也不声称已完整查明这项退化。

当前实现仍不重算当前批次已提交的canonical cache选择，故局部必要边的修复并不保证全模型CPI单调变化。要定位这一小幅退化，需要固定原始来源，区分warmup/measurement干预，并检查首个改变的请求准入、服务和退休关键跨度。不能按负载选择性关闭必要恢复边或调高/调低恢复常数。

## 6. 范围与下一步

本轮完成的是**恢复尾部传播修复**及两案验证。完整动态模型仍需把分支执行解析、IEW→redirect控制传播、Fetch squash状态、指令供应和带宽约束拆成有来源的事件边，再处理实际cache请求准入一致性。当前配置的2-cycle值没有在本轮被认证为所有分支的完整硬件恢复时延。SQLite仍约12.05%低估、Zstd仍约5.49%低估，未恢复全矩阵历史精度。

默认开关继续关闭。本轮不报告宿主吞吐或开销；没有quiet-host串行交错计时证据。来源门禁、局部机制通过、两案准确率变化、默认推广和硬件perf等价是不同结论。

[机器证据](../tmp/branch-recovery-tail-fix-20261020/evidence.json) 的 29 项检查通过。[命令与收据](../tmp/branch-recovery-tail-fix-20261020/evaluation-receipts.json)、[源码增量](../tmp/branch-recovery-tail-fix-20261020/simulator.patch)、[构建日志](../tmp/branch-recovery-tail-fix-20261020/build.log)、[旧二进制RED](../tmp/branch-recovery-tail-fix-20261020/red-final.log)、[新二进制GREEN](../tmp/branch-recovery-tail-fix-20261020/green.log)、[完整测试](../tmp/branch-recovery-tail-fix-20261020/final-tests.log) 均保留。
