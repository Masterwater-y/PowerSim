# Buffer TSO 局部回退参考实现（2026-09-24）

> 后续分析与执行顺序见[实验汇总、CPI 归因边界与下一步取证](buffer-tso-cpi-diagnosis-next-steps-20260924.md)。本报告保留本轮冻结实验结果。

新增默认关闭的局部回退选项。它缩小的是服务决定的降级范围，当前仍以整 epoch 事务重放验证共享状态；不是稀疏增量执行器，也未宣称完成高吞吐生产目标。原整批回退和强制不回退模式保留。

大额度池对照中，TeaLeaf/NAMD 分别保留 12,212/40,109 次合并，试算后整批兜底为 0/2；仍有大量 epoch 因预算预检查未尝试。CPI MAPE 从基线 16.532511% 变为 16.568341%，MAE 从 0.116912 变为 0.117256 cycles/macro，未获得精度收益。宿主吞吐分别下降 5.17%/4.37%。默认额度下 NAMD 仍完全跳过，不能把默认选项的低开销当作已建模的证明。

## 配置

```ini
ruby.buffer_service_repair = true
ruby.buffer_service_local_fallback = true
ruby.buffer_service_no_fallback = false
```

local_fallback 必须与 repair 一起开启，不能同时开启 no_fallback。默认 max_work=4096、work_percent=10、max_passes=3 没有改变。实验另外测了 local-large：仅把 max_work 改为 65536，累计工作比例仍为 10%。这是显式配置对照，不是隐藏改变默认额度。

## 实现边界

- 每个请求有单调降级标记。同一 epoch 写/原子涉及的行、特殊事件及 forwarded load 先使用旧路径；只禁用这些请求的新 alias 查找和父服务登记，不排除它们对缓存/CPU/控制器的作用。静态写行预排除仍按整行，是当前参考实现的保守部分。
- 新模型请求若最终准入、父响应、epoch 范围或核心内晚到 FU 约束失效，只将对应请求（晚到 FU 标记目前只能定位到 core）降级。降级请求使用原基线请求起点，下一遍从同一 epoch 快照完整重放。父服务被移除后，原 follower 在重放中重新分类。
- 同一次最终回放统一产生缓存、PMU、服务响应及资源记录；不拼接按行独立运行的结果，也不事后扣 PMU。特殊事件导致自身和受其时序影响的请求降级，不再按特殊事件名称直接拒绝整个 epoch。
- 最终检查所有仍保留的新服务决定。该证书只覆盖保留的新服务，降级事件仍继承旧两阶段模型的请求起点近似；它不是全局精确硬件模拟的证书。
- 已接受的跨 epoch 父响应不被当前 epoch 修改。成功的混合事务保留在途身份。全局权限/控制器错误、跨 epoch 父服务与写重叠、重试或工作额度耗尽仍可进入显式整批兜底。该兜底和预检查跳过沿用旧实现清空服务身份的近似，discarded_services 保留计数；本轮没有解决这部分跨 epoch 完整性。

## 工作额度与回退粒度

初测发现首次试算后无法支付局部修正，导致大量整批撤销。最终版本在 local 模式进入前预留最多两次回放（受 max_passes 限制），含控制器扫描/调度的保守上界；实际工作扣费与 10% 累计比例不变。控制器选择窗口为 1 时使用相应较小上界。

这会减少“试算一半才发现没钱”的浪费，但如果保守上界超过 max_work，即使某个 epoch 实际可能一遍成功，也会直接跳过。整 epoch 参考阶段仍有这个容量限制，不能靠更名为局部回退掩盖它。

新增 causal_frontier 字段：local_skipped_epochs（未试算）、local_committed_epochs、local_all_baseline_epochs、local_modeled_events、local_baseline_events、local_retained_aliases、local_preexcluded_events、local_degraded_events、local_degradation_rounds、local_max_degradation_events、local_broad_fallback_epochs，均有 buffer_service_ 前缀。降级直方图的桶为每轮新增降级事件数 1、2–4、5–16、17–64、65+。

preexcluded/degraded 包括最终失败的试算；modeled/baseline/retained_aliases 只统计成功提交。all_baseline 不能算服务覆盖。FR-FCFS stable/candidate 等旧诊断包含试算，不是最终提交服务数。

## 测试与审查

完整 `cmake --build build -- -j16`、`./build/fastsim_tests` 成功。关键回归：

- X 读写冲突，Y 两读仍合并：4 请求、3 次 L1 查找，X 两事件降级，Y 两事件保留。此测试在旧整批回退实现上实际失败，实施后通过。
- RAW 使一个 follower 到达失效：仅降级该事件，保留另一行合并；动态发现需要两遍，预先固定同一最终掩码只需一遍，逐请求响应、周期、L1/L2/LLC PMU 和 DRAM reads 相同。这是同引擎完整重放参考，非独立模型真值。
- 同 cache set、FR-FCFS 实际重排、I-side 全部降级的显式计数、多个 epoch 下已提交前缀响应不变、重试耗尽后的 PMU 恢复、预算预留及互斥配置。
- 独立代码审查没有发现阻断问题；测试 hook 为编译保护下的 thread-local 状态，不改变 config ABI 布局，测试清理成功和异常两条路径。

baseline、repair、no-fallback 两案完整 target scope（去掉 host throughput）逐值复现上轮结果，保证旧模式预测兼容。

## Native 范围与来源

TeaLeaf/NAMD C4，2 案、8 条 core trace，user-plus-kernel，first-core-target-common-end-v1；已检查过的小集，不是新的 held-out 或 40-case 全矩阵。源为 `tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/` 对应两案，include 配置为 `tmp/buffer-tso-store-edges-20260923/dual.cfg`，未调时延参数。

独立身份检查 2/2；五组均通过维护的 native validator，passed/scored=2、failed/pending=0、宏指令人口匹配。缓存参考为 conserved Ruby/SLICC native summaries，排除 path-class cache。branch/L1D/private-L2 为 proxy、LLC 为 diagnostic；source/replay 门禁不是精度或严格硬件 PMU 认证。

最终冻结产物、identity.json、五组 summary.{json,csv,md}、逐案 validation.json、binary.sha256、源码 fingerprints.json、测试与吞吐日志在 `tmp/buffer-service-local-final-20260924/`。预算预留前结果和红灯保留于 `tmp/buffer-service-local-20260924/`。

## CPI

| Case | 配置 | gem5 CPI | FastSim CPI | 相对误差 | 绝对误差 cycles/macro |
|---|---|---:|---:|---:|---:|
| 811.tealeaf_s | baseline | 1.004183 | 0.860056 | -14.352728% | 0.144128 |
| 857.namd_s | baseline | 0.479341 | 0.389645 | -18.712294% | 0.089696 |
| 811.tealeaf_s | repair | 1.004183 | 0.860056 | -14.352728% | 0.144128 |
| 857.namd_s | repair | 0.479341 | 0.389645 | -18.712294% | 0.089696 |
| 811.tealeaf_s | no-fallback | 1.004183 | 1.225388 | +22.028365% | 0.221205 |
| 857.namd_s | no-fallback | 0.479341 | 0.394087 | -17.785649% | 0.085254 |
| 811.tealeaf_s | local | 1.004183 | 0.860056 | -14.352728% | 0.144128 |
| 857.namd_s | local | 0.479341 | 0.389645 | -18.712294% | 0.089696 |
| 811.tealeaf_s | local-large | 1.004183 | 0.859395 | -14.418517% | 0.144788 |
| 857.namd_s | local-large | 0.479341 | 0.389617 | -18.718165% | 0.089724 |

| 配置 | CPI MAPE | CPI MAE cycles/macro |
|---|---:|---:|
| baseline | 16.532511% | 0.116912 |
| repair | 16.532511% | 0.116912 |
| no-fallback | 19.907007% | 0.153230 |
| local | 16.532511% | 0.116912 |
| local-large | 16.568341% | 0.117256 |

## 覆盖与计算

| Case / 配置 | 总 epoch | 跳过 | 提交（全旧） | 整批兜底 | 新模型事件 | 旧路径事件 | 保留合并 | 试算遍数 | 工作量 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 811.tealeaf_s / local | 5217 | 5058 | 159 (2) | 0 | 5701 | 14601 | 1013 | 290 | 355145 |
| 857.namd_s / local | 2681 | 2681 | 0 (0) | 0 | 0 | 0 | 0 | 0 | 0 |
| 811.tealeaf_s / local-large | 5221 | 4697 | 524 (2) | 0 | 82176 | 166796 | 12212 | 984 | 4498446 |
| 857.namd_s / local-large | 2680 | 2570 | 108 (0) | 2 | 128630 | 177520 | 40109 | 219 | 4986316 |

| Case / 配置 | 动态降级事件（含失败试算） | 降级轮次 | 单轮最大 | 1 / 2–4 / 5–16 / 17–64 / 65+ | 丢弃身份条目 |
|---|---:|---:|---:|---|---:|
| uarch-baseline-c04-811.tealeaf_s / local | 6806 | 131 | 158 | 1 / 3 / 17 / 72 / 38 | 229 |
| uarch-baseline-c04-857.namd_s / local | 0 | 0 | 0 | 0 / 0 / 0 / 0 / 0 | 1 |
| uarch-baseline-c04-811.tealeaf_s / local-large | 91757 | 460 | 1890 | 1 / 9 / 24 / 103 / 323 | 699 |
| uarch-baseline-c04-857.namd_s / local-large | 59129 | 111 | 1057 | 1 / 1 / 2 / 2 / 105 | 143 |

直方图按一次完整回放后发现的请求集合统计，不是已经构建出的独立资源依赖连通分量。当前整个 epoch 是共享状态重算范围，表中局部事件数不能当作重算成本。

## PMU

### baseline

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 7.283142% | 7.283142% | 10.574464% | 11.315011% | 6.626559% | 6.626559% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 191.951173% | 191.951173% | 345.462879% | 380.003013% | 4.527763% | 4.527763% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### repair

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 7.283142% | 7.283142% | 10.574464% | 11.315011% | 6.626559% | 6.626559% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 191.951173% | 191.951173% | 345.462879% | 380.003013% | 4.527763% | 4.527763% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### no-fallback

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 6.981123% | 6.981123% | 10.563188% | 11.369153% | 6.266541% | 6.266541% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 192.298388% | 192.298388% | 346.088544% | 380.691329% | 4.535017% | 4.535017% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### local

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 7.283142% | 7.283142% | 10.574464% | 11.315011% | 6.626559% | 6.626559% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 191.951173% | 191.951173% | 345.462879% | 380.003013% | 4.527763% | 4.527763% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### local-large

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 7.279727% | 7.279727% | 10.573306% | 11.314362% | 6.622695% | 6.622695% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 191.830733% | 191.830733% | 345.246991% | 379.765648% | 4.523857% | 4.523857% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

### 查找与 miss 数

| Case / 配置 | L1D accesses | L1D tag misses | private L2 tag misses | LLC tag misses |
|---|---:|---:|---:|---:|
| 811.tealeaf_s / baseline | 6956490 | 479840 | 354458 | 193088 |
| 857.namd_s / baseline | 10761570 | 375498 | 20181 | 18066 |
| 811.tealeaf_s / repair | 6956480 | 479840 | 354458 | 193088 |
| 857.namd_s / repair | 10761570 | 375498 | 20181 | 18066 |
| 811.tealeaf_s / no-fallback | 4915063 | 476745 | 354455 | 193088 |
| 857.namd_s / no-fallback | 8000823 | 375705 | 20210 | 18066 |
| 811.tealeaf_s / local | 6955477 | 479840 | 354458 | 193088 |
| 857.namd_s / local | 10761570 | 375498 | 20181 | 18066 |
| 811.tealeaf_s / local-large | 6944596 | 479811 | 354454 | 193088 |
| 857.namd_s / local-large | 10721501 | 375496 | 20171 | 18066 |

## 吞吐

最终冻结二进制，串行对称顺序 baseline/repair/no-fallback/local/local-large/local-large/local/no-fallback/repair/baseline，每臂两次；两负载分别执行。目标 scope 每次与对应验证结果一致。单位 M measured user UOP/s；非独占宿主机，仅为 pilot，差异较小时不能解读为稳定加速。

| Case | 配置 | 吞吐 | 相对 baseline |
|---|---|---:|---:|
| uarch-baseline-c04-811.tealeaf_s | baseline | 3.2192 | +0.00% |
| uarch-baseline-c04-811.tealeaf_s | repair | 3.1737 | -1.41% |
| uarch-baseline-c04-811.tealeaf_s | no-fallback | 2.6592 | -17.40% |
| uarch-baseline-c04-811.tealeaf_s | local | 3.1887 | -0.95% |
| uarch-baseline-c04-811.tealeaf_s | local-large | 3.0528 | -5.17% |
| uarch-baseline-c04-857.namd_s | baseline | 2.7836 | +0.00% |
| uarch-baseline-c04-857.namd_s | repair | 2.7768 | -0.24% |
| uarch-baseline-c04-857.namd_s | no-fallback | 2.2957 | -17.53% |
| uarch-baseline-c04-857.namd_s | local | 2.7508 | -1.18% |
| uarch-baseline-c04-857.namd_s | local-large | 2.6620 | -4.37% |

## 剩余范围

该选项已实现按请求降级及共享事务认证。真正的局部状态日志、增量 CPU 重算和资源依赖闭包仍未实现；整批兜底及跳过时的身份丢弃也仍是明确近似。完整高吞吐、广覆盖方案未完成，不改默认、不把机制测试或 native passed 当作生产准入。
