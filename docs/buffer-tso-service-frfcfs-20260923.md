# Buffer TSO 服务事务接入 FR-FCFS（2026-09-23）

> 后续按用户要求增加了默认关闭的“不回退”选项；完整覆盖下的 CPI、PMU 和
> 吞吐实验见 [不回退验证](buffer-tso-service-no-fallback-20260923.md)。本文保留
> 有界回退模式的历史结果。

本轮完成控制器接入与机制回归；仍是默认关闭的有界参考实现，尚未达到广覆盖、高吞吐生产目标。保留原路径和完整回退，不改时延参数。

## 实现与纠正

原先按 FR-FCFS 名称整 epoch 拒绝过于保守：本次 TeaLeaf/NAMD C4 的拓扑缩放选择窗口均为 1，原控制器本来就不执行重排。取消这个 blanket guard，同时把实际需要重排的控制器纳入事务。

独立父请求进入控制器，合并请求不新增缓存查找、PMU 或 DRAM 请求。控制器的父响应作为下一轮推测提示；最终准入时间和父响应同时收敛才提交。任何分类、响应或准入变化均整批恢复再回放，先前 epoch 已提交服务保持不变。控制器扫描和调度额外扣工作额度；失败也计费。

强制同 bank 的行 A/B/A 重排用例通过：4 个读形成 3 个父请求，L1 查找 4→3，第 4 个请求响应 9→117，绑定第 3 个父请求；2 次重放接受，整体 cycles 仍为 165。单次额度 42 时，在一次控制器尝试后回退，缓存 miss、DRAM reads、总周期和逐请求响应恢复基线。controller 的尝试/重排诊断可累计失败尝试，不能当成最终提交的 DRAM 流量。

`cmake --build build -- -j16` 与 `./build/fastsim_tests` 均成功；新增定向测试也通过通用/快速内核一致性检查。独立代码审查未发现具体阻断问题，要求增加的实际重排与回滚测试已补齐。

## Native pilot

范围：TeaLeaf 与 NAMD C4，user-plus-kernel，first-core-target-common-end-v1，CPI 为 cycles/macro instruction。不是完整 40-case 验收。源目录为 `tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/` 中对应两案。身份检查 2/2；三个配置均经维护的 native validator，source/replay 与 PMU scored 2/2，failed/pending=0；缓存来源是 conserved Ruby/SLICC summaries，未使用 path-class cache。proxy/diagnostic 映射不升级为严格硬件 PMU。

实验目录：`tmp/buffer-service-frfcfs-final-20260923/`，含冻结二进制哈希、identity.json、各臂 summary.{json,csv,md} 与逐案 validation.json。初测与红灯日志保留于 `tmp/buffer-service-frfcfs-20260923/`。

三个配置：baseline 关闭新开关；repair 开启、额度池 4096；repair-large 仅将额度池改为 65536。两种候选均保持累计 10% 的 UOP+访存工作额度、最多 3 次回放；工作额度不是墙钟开销百分比。

| Case | gem5 CPI | 三臂 FastSim CPI | 相对误差 | CPI 绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| 811.tealeaf_s | 1.004183 | 0.860056 | -14.3527% | 0.144128 |
| 857.namd_s | 0.479341 | 0.389645 | -18.7123% | 0.089696 |

三臂 CPI MAPE 均为 16.532511%，CPI MAE 均为 0.116912 cycles/macro。四项被评分的 PMU miss 数均不变。TeaLeaf 的 L1D access/hit 数实际下降 10（repair）和 11（repair-large），所以不能说所有 PMU 字段毫无变化；这几个合并没有改变 miss 数或最终 CPI。

### 覆盖与失败原因

| Case / 候选 | 总 epoch | 接受 epoch / 事件 | budget | horizon | conflict | special | controller | 工作额度消耗 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 811.tealeaf_s / repair | 5217 | 6 / 112 | 4761 | 183 | 253 | 14 | 0 | 1233811 |
| 857.namd_s / repair | 2681 | 0 / 0 | 2681 | 0 | 0 | 0 | 0 | 0 |
| 811.tealeaf_s / repair-large | 5217 | 8 / 2683 | 4431 | 320 | 419 | 39 | 0 | 4485197 |
| 857.namd_s / repair-large | 2681 | 0 / 0 | 2097 | 0 | 171 | 413 | 0 | 5018920 |

扩大额度池让 NAMD 开始尝试，但 175 次回放仍未提交；读写冲突和特殊事件成为实际阻断。TeaLeaf 接受事件增加到 2683，但接受事件不是合并事件，也不是获得精度收益的事件。整 epoch 回放与整 epoch 回退仍是覆盖瓶颈。

### PMU 误差（三臂相同）

| counter | mapping | finite cases | MAPE | P50 | P90 | P99 | WAPE | bias | reference source |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| branch_misses | proxy | 2/2 | 2.626406% | 2.626406% | 4.228775% | 4.589307% | 0.883650% | -0.282251% | kernel_events-v3:taotrace-retired-bpred-v1 |
| l1d_tag_misses | proxy | 2/2 | 7.283142% | 7.283142% | 10.574464% | 11.315011% | 6.626559% | 6.626559% | native-ruby-slicc:l1d.tag_misses |
| private_l2_tag_misses | proxy | 2/2 | 191.951173% | 191.951173% | 345.462879% | 380.003013% | 4.527763% | 4.527763% | native-ruby-slicc:l2.tag_misses |
| llc_tag_misses | diagnostic | 2/2 | 380.610699% | 380.610699% | 684.678595% | 753.093872% | 8.743054% | 8.743054% | native-ruby-slicc:llc.tag_misses |

## 吞吐

最终二进制，串行两个独立 ABBA 区块，每臂两次，逐次目标 scope 与对应验证输出一致。无独占宿主机保证，数值为 pilot，不能当作稳定性能收益；当前覆盖很低，也不代表全面启用服务建模的成本。单位为 M measured user UOP/s。

| Case | 候选 | 配对 baseline | 候选吞吐 | 变化 |
|---|---|---:|---:|---:|
| uarch-baseline-c04-811.tealeaf_s | repair | 3.2373 | 3.1946 | -1.32% |
| uarch-baseline-c04-811.tealeaf_s | repair-large | 3.2289 | 3.1396 | -2.77% |
| uarch-baseline-c04-857.namd_s | repair | 2.7953 | 2.7921 | -0.12% |
| uarch-baseline-c04-857.namd_s | repair-large | 2.6733 | 2.7806 | +4.01% |

## 保留限制与下一步

不改默认开关，不扩大正式矩阵，不用提高预算来宣称高吞吐目标已完成。下一步设计已审查但尚未实施：同一 epoch 的读写混合行关闭新服务合并和父身份建立，其他读继续参与；所有事件仍纳入同一次缓存/CPU/控制器事务认证，跨 epoch 在途服务与写重叠仍回退。需要独立计数可建模事件与保留旧行为的事件，不能把整批 committed events 都称为已建模服务。

I-side/page-walk 的准入账本、写权限阶段服务、稀疏依赖闭包与局部 core 重算仍未完成。既存 generic attribution 的 read/write/read 守恒异常没有在本轮修复；本轮正式 C++ 回归通过不代表该独立探针已修复。
