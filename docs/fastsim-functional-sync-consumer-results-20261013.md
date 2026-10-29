# FastSim 同步 consumer 实施、审核与 C4 验证

日期：2026-10-13。设计：[同步 consumer 设计](fastsim-functional-sync-consumer-design-20261013.md)；计划：[实施计划](superpowers/plans/2026-10-13-functional-sync-consumer.md)。

## 结果与适用范围

首片 consumer 已实现并通过机制/实际回放验证。`sync.plan` 默认关闭；开启后，在全部 marker 的校正退役边界交接 producer，通过无 oracle 的 DAG 限制跨线程继续执行。保留真实 runtime、自旋、futex 和内核记录；不调整采集端时间、硬件常数或锁赢家。

支持固定线程绑定的 parallel、implicit task 和 team barrier。迁移、nested/task/cancel、paired/近似/corrected-suffix/causal-block 模式、任意初始切片和 execution windows/DVFS 拒绝。measurement-only 可用静态不同频率；functional warmup 要求核频率等于 reference。marker 边界保守串行化、fixed trace 的等待人口以及缺少 OS schedule/visibility 边仍限制精度；这不是完整的 OS 调度或内存 HB 模型。

同步开启没有改善本次 CPI 的诊断误差。它修复了事件顺序约束；不能据误差变大就删去约束，也不能通过新增延迟常数把数字调回去。`wait_mode=unknown`，ON 输出是 elapsed timeline，CPI status 显式为 `functional-sync-elapsed-wait-mode-unknown`，不能作为正式 active CPI 认证。

## 设计审核与实现

独立审核及二次因果审核处理了以下问题：

- marker chunk 回收后，单独保存被阻塞的覆盖 frontier；lookahead 不等待尚未许可的 producer。
- 在 terminal marker 完成 ack 后才标记 finished；warmup 清零保留图状态，并为普通 warmup 尾部安装 measurement seam floor。
- 不让 master join 等待所有 worker departure；departure 才等待同代全部 arrival。
- 使用 feedback-corrected retirement 和 reference/local 时钟换算；不把 baseline 解码时间或 gem5 tick 当作 release。
- 初版前驱-only floor 会把后续请求放回已提交窗口。回归用例实际失败；改为在 marker baseline/pending corrected retirement 截断 epoch，包括 empty fast-forward，并对所有 marker 安装 admission floor。首次回放 `replay-on/` 已否决，最终证据来自 `replay-frontier-on/`。

代码：`tools/functional_sync_graph.py`、`tools/compile_functional_sync.py`、`FunctionalSyncPlan`、FST raw ordinal API、IntervalCore frontend floor 和 simulator 的 queue handshake。生产 plan 必须由 compiler 生成；runtime 的结构/anchor 检查不替代 compiler 的完整 source/lifecycle 检查。QEMU/DynamoRIO 可沿用事实协议和 consumer，本次未实现它们的采集适配器。

## 输入与证据

仅使用既有 `accepted-source/04c-tealeaf-test-10m-entry`；来源及硬件近似沿用 [原 C4 采集报告](tealeaf-4c-sync-trace-accuracy-20261012.md)。固定 512×512、OMP4、PASSIVE、buffer114、native user-plus-kernel。完整初始同步上下文，record-bounded warmup，四核统一共同终点。

FST 合计 **15,404,784,480 B（15.405 GB / 14.347 GiB）**，低于 20 GB；未新增或复制主实验 FST。warmup 201,548,375 records；测量 39,150,412 mixed UOP records，其中 user 38,964,790；macro denominator **25,183,423**，双方及全部四核分母一致。

全程 959 events / 54 regions / 53 barriers，最后一 region 仅有共同终点截断，未虚构闭合。最终 consumer 释放 **959/959**；测量 **55/55** 个事件，每个均有后续访存审计，**0** 个 corrected memory issue 早于 admission frontier；admission 相对 release 的最大额外 horizon padding **0 cycles**。测量有 **3** 次 exposed dependency waits，合计 **482,840 reference cycles**。

旧完整 FS probe 的 worker marker 在多个核上出现，compiler 明确拒绝；普通/workshare barrier 用机制和 runtime 测试覆盖。无法通过 marker 单独排除两 marker 之间的瞬时迁移，因此不声称已认证完整静态 OS 调度。

## CPI 与 PMU 对照

CPI 分母为各模拟器自己的 macro instructions。gem5 为合同要求的 native active reference；ON elapsed 与条件性值仅做诊断比较。绝对误差单位为 cycles/macro，不能解读成正式 active CPI 精度。

| 模式 | gem5 CPI | FastSim CPI | CPI 绝对误差 | CPI 相对误差 |
|---|---:|---:|---:|---:|
| OFF（原模型） | 0.369387196 | 0.418455188 | 0.049067992 | +13.2836% |
| ON（elapsed 模型，active 未认证） | 0.369387196 | 0.437835834 | 0.068448638 | +18.5303% |
| ON（仅扣除 exposed wait 的敏感性值） | 0.369387196 | 0.418662904 | 0.049275708 | +13.3399% |

单一 case 的 CPI MAE：OFF **0.049067992**；ON elapsed 诊断 **0.068448638** cycles/macro。该单 case MAPE/WAPE 的数值等于其 APE，但两者定义不同，且 ON 的 active 分类仍未认证。

ON cycles **11,026,205**，OFF **10,538,134**；条件性 CPI 只扣除 exposed retirement dependency wait，不撤销 marker serialization/cache 交错变化，也不表示完整 active CPI 下界。

PMU reference 通过维护的 native validator 选择 conserved Ruby/SLICC summaries；branch 来自 retired BPred 原始 outcome。没有采用 TaoTrace cache proxy 作原生 truth。mapping 仍为 branch/L1D/private-L2 proxy，LLC diagnostic；来源检查通过不等于 strict hardware PMU 认证。

| 事件 | gem5 reference | OFF | OFF 相对误差 | ON | ON 相对误差 |
|---|---:|---:|---:|---:|---:|
| Branch misses | 3900 | 3763 | -3.5128% | 3763 | -3.5128% |
| L1D tag misses | 500084 | 488063 | -2.4038% | 483724 | -3.2715% |
| Private-L2 tag misses | 268644 | 201419 | -25.0238% | 202821 | -24.5019% |
| LLC tag misses | 0 | 0 | +0.0000% | 0 | +0.0000% |

LLC 0/0 按合同 APE=0，WAPE=N/A，没有激活该 miss 路径，不能据此断言已验证 LLC 精度。

本次各单次实测吞吐：OFF **6.086 M user-UOP/s**；ON **5.876 M user-UOP/s**。两次串行运行同一最终 binary；这是观测值，未做多次重复/固定亲和性的正式吞吐比较，不能据此估计稳定开销。native warmup 无 privilege 分区，end-to-end user-UOP/s 不可提供。

## 验证与复现

默认 `cmake --build build -- -j16` 和 `LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` 均成功；functional validator/graph/compiler Python **27 tests** 与真实 libgomp runtime **3 tests** 成功。覆盖正常/晚 departure、generation 复用、缺 arrival/环/迁移/逆 phase 拒绝、删除/破坏 oracle 不改变 plan、raw ordinal、异频、非零 response feedback、warmup seam、末尾 marker、runtime 继续执行以及 post-boundary memory 顺序。

最终 OFF 的全部 core counters 和 CPI/PMU scope predictions 与前一冻结 baseline 完全一致。OFF/ON 两次 native validator 均 **passed 1/1**、23 checks，各核 denominator 对齐；单独 source identity validator **valid 1/1**。原配置的 NoC/参数覆盖限制仍在，未借此声称硬件参数全部精确。

最终 binary SHA256：`05090be4c52b7ca88839c5eb4f85cc5b737d0f916c0a0624335ea182e83b1c6c`。

所有实验产物位于 `tmp/functional-sync-consumer/`：`tealeaf.plan`、`tealeaf-on.cfg`、`final-provenance.json`、`final-comparison.json`、`identity.json`、`replay-frontier-on/`、`replay-frontier-off/` 和测试日志。两个回放通过 `tools/run_native_kernel_fastsim_validation.py --source-root ... --fastsim build/fastsim --config ... --expected-cases 1 --jobs 1` 生成；没有另写 reference selector。使用命令见设计文档。
