# Buffer TSO 不回退选项与验证（2026-09-23）

> 后续新增[局部回退参考实现](buffer-tso-service-local-fallback-20260924.md)，保留本选项；新增模式按请求降级，仍使用整 epoch 事务验证共享状态。

按用户要求新增默认关闭的诊断选项。两案每个非空访存 epoch 均完成一次服务回放并保留结果，服务层回退为零。CPI/PMU 已发生变化，但当前小集的整体 CPI 误差变大，不能据此开启默认路径。

## 使用与准确语义

```ini
ruby.buffer_service_repair = true
ruby.buffer_service_no_fallback = true
```

前提仍是 buffer TSO、非零 CPU outstanding 容量，且不与旧 alias/private-read-service 开关混用。只打开 no_fallback 而不开 repair 会报配置错误。关闭 no_fallback 后恢复原有有界回退；关闭 repair 后恢复原基线。

不回退模式每个非空访存 epoch **只执行一次完整服务回放**。忽略工作额度、特殊事件、读写冲突、超出 epoch、晚到 FU 调整和准入/响应未收敛的拒绝条件，不停止在冲突事件处，也不重跑原基线。它不是无限迭代求收敛，也不是把工作上限改成一个大数；max_passes 在此模式下不决定迭代数，max_work/work_percent 不再限制实际工作。原配置合法性限制仍保留。

缓存查找之前的 alias 决定仍共同影响 PMU 与服务反馈。取指使用自身的时序反馈账本；不会把普通 data admission 账本中的零值当作取指时间。未修复跨 epoch、读写及未收敛准入一致性：后续 CPU 准入或控制器改写响应可能与单次回放的 alias 判定不同。内部 FR-FCFS 不能完成重排时保留其 canonical schedule 并计 ignored_controller；不重跑整个服务 epoch。原控制器的一入口窗口 bypass 保持不变，本次 native 两案 ignored_controller 均为零。

新增输出位于 `causal_frontier`：`buffer_service_no_fallback_epochs`、`buffer_service_uncertified_epochs`，以及 `buffer_service_ignored_{budget,special,conflict,controller,horizon,late_issue,unstable}_epochs`。ignored 原因可以重叠，不能相加当作 epoch 总数。旧 fallback 计数只表示真实回退；committed 仅表示保留了试算结果，不能解释为已认证正确。预算不足本身是宿主资源限制，单独计数，不自动判定语义未认证。

## 回归与来源

`cmake --build build -- -j16` 和 `./build/fastsim_tests` 通过；定向测试覆盖零预算仍提交、容量移动标记未认证、读写冲突后完成全部请求、I-side 不整批回退，以及选项依赖验证。独立代码审查未发现阻断问题。原有回退测试全部保留。

baseline 和 guarded repair 在本轮两案的完整 target scope（排除 host throughput）分别与上轮逐值一致。首个新增测试先因缺失选项编译失败，加入实现后通过；这不是旧实现运行时断言红灯的证据。

诊断 pilot：TeaLeaf/NAMD C4，共 2 个配置、8 条 core trace，user-plus-kernel、first-core-target-common-end-v1。这两个已经检查过的案例不构成新的 held-out 精度验证。源目录沿用 `tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/` 中对应两案，配置 include `tmp/buffer-tso-store-edges-20260923/dual.cfg`，未修改时延常数。

独立硬件身份检查 2/2；baseline、guarded repair、no-fallback 三臂均通过维护的 native validator，passed/scored 2/2，failed/pending=0，宏指令人口匹配。缓存真值为 conserved native Ruby/SLICC summary，未使用 path-class cache。branch/L1D/private-L2 为 proxy，LLC 为 diagnostic；这些 source/replay 检查不等于模型准确或严格硬件 PMU 认证。

冻结二进制、哈希、配置、identity.json、每臂 summary.{json,csv,md}、逐案 validation.json、测试与串行 ABBA 日志均保留于 `tmp/buffer-service-no-fallback-20260923/`。

## CPI

baseline 与 guarded repair 的 CPI 相同；下面“原值”为这两臂。绝对误差单位为 cycles/macro instruction。

| Case | gem5 CPI | 原 CPI | 不回退 CPI | 原相对误差 | 新相对误差 | 原绝对误差 | 新绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 811.tealeaf_s | 1.004183 | 0.860056 | 1.225388 | -14.3527% | +22.0284% | 0.144128 | 0.221205 |
| 857.namd_s | 0.479341 | 0.389645 | 0.394087 | -18.7123% | -17.7856% | 0.089696 | 0.085254 |

| 配置 | CPI MAPE | CPI MAE cycles/macro |
|---|---:|---:|
| baseline | 16.532511% | 0.116912 |
| repair | 16.532511% | 0.116912 |
| no-fallback | 19.907007% | 0.153230 |

TeaLeaf 从偏低变为偏高，绝对误差扩大；NAMD 小幅改善。两个案例不足以证明去掉保护的普遍收益，且 forced 结果未经一致性认证。

## 覆盖与忽略的拒绝条件

| Case | committed / 总 epoch | 接受事件 | 未认证 epoch | ignored budget | conflict | special | horizon | unstable | 工作量 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 811.tealeaf_s | 7007 / 7007 | 6182964 | 6899 | 7007 | 3183 | 150 | 6553 | 6898 | 59689907 |
| 857.namd_s | 2695 / 2695 | 9206896 | 2691 | 2695 | 2678 | 1132 | 2437 | 2691 | 72289038 |

两案 ignored_controller、ignored_late_issue 和所有实际 buffer_service fallback 计数均为零。跨 epoch 的身份不因这些保护条件清空。模型周期改变也会改变 epoch 数，不能把不同模式的 epoch 逐个对齐。

## PMU

### 被评分的 miss 计数

| Case | 事件 | 原值 | 不回退 |
|---|---|---:|---:|
| 811.tealeaf_s | branch_misses | 3684 | 3684 |
| 811.tealeaf_s | l1d_tag_misses | 479840 | 476745 |
| 811.tealeaf_s | private_l2_tag_misses | 354458 | 354455 |
| 811.tealeaf_s | llc_tag_misses | 193088 | 193088 |
| 857.namd_s | branch_misses | 50370 | 50370 |
| 857.namd_s | l1d_tag_misses | 375498 | 375705 |
| 857.namd_s | private_l2_tag_misses | 20181 | 20210 |
| 857.namd_s | llc_tag_misses | 18066 | 18066 |

### 原值误差（baseline / guarded repair）

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 7.283142% | 7.283142% | 10.574464% | 11.315011% | 6.626559% | 6.626559% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 191.951173% | 191.951173% | 345.462879% | 380.003013% | 4.527763% | 4.527763% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### 不回退误差

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 6.981123% | 6.981123% | 10.563188% | 11.369153% | 6.266541% | 6.266541% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 192.298388% | 192.298388% | 346.088544% | 380.691329% | 4.535017% | 4.535017% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### 机制观察

TeaLeaf L1D accesses 6,956,490→4,915,063；NAMD 10,761,570→8,000,823。查找抑制已大规模生效，但 miss 数变化远小于 access/hit，LLC miss 完全不变。不能将大量合并直接解释为大量 DRAM 请求被消除。

导出的 totals 诊断里，TeaLeaf 的合并请求为 1,851,001，Sequencer stall_cycles 从 22,075 增为 50,913,909；NAMD 合并请求 2,759,673，stall_cycles 从 85,444 增为 3,064,900。这些等待计数可以重叠累加，不是可直接相加的 CPI 周期归因。现有证据说明共用服务响应和 CPU outstanding 约束明显影响时序；尚未用逐请求 native 配对证明每个合并身份与延迟正确。

## 吞吐

最终冻结二进制，串行 ABBA、每臂两次。两个区块分别比较 baseline 与 guarded repair、baseline 与 no-fallback；目标 scope 每次均与对应验证结果一致。单位 M measured user UOP/s。宿主机非独占，仅报告实测 pilot。

| Case | 候选 | 配对 baseline | 候选 | 变化 |
|---|---|---:|---:|---:|
| uarch-baseline-c04-811.tealeaf_s | repair | 3.1426 | 3.1784 | +1.14% |
| uarch-baseline-c04-811.tealeaf_s | no-fallback | 3.1439 | 2.6044 | -17.16% |
| uarch-baseline-c04-857.namd_s | repair | 2.7599 | 2.7182 | -1.51% |
| uarch-baseline-c04-857.namd_s | no-fallback | 2.6311 | 2.2624 | -14.01% |

本次实验回答了“强行保留结果是否会改变预测”：会，而且覆盖已到全部非空访存 epoch。但当前单次准入判定尚未认证，不能把 NAMD 的小幅改善推广为正确性结论。保留可显式开启的选项和原回退模式，不改默认。
