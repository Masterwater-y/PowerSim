# R0/R1 独立副本修复与同输入验证

日期：2026-11-11。实现和验证发生在独立副本；未合并、提交或推送。完整模式仍需显式选择，默认保持关闭。

## 1. 冻结保护与版本身份

- 源码：[独立源码目录](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/source)。
- 候选 SHA-256：`c5f4244a1dd5440f07c58be60852f79be8176636bae83c79c12b5df73635c781`。
- 原冻结 SHA-256：`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
- 原冻结源码、二进制及生产源码/二进制共 2261 项哈希/链接记录保持不变：[protected-after.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/protected-after.json)。
- 硬件/采集身份独立核验：5/5 通过，[identity.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/identity.json)。
- 最终候选七组测试全通过：[tests-final-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/tests-final-r4.json)。
- 中性迁移：5/5 严格一致；比较完整 scope（排除 host 吞吐）、逐核计数与时间、CHA、输入内容身份、调度和线程状态。证据：[neutrality-final-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/neutrality-final-r4.json)。

首轮候选 `f28a41ef…` 在 Marian 出现兼容模式漂移，已归档在 `evaluation/`。第二轮将后迁入的容量/prepare 门控限制到完整模式，恢复原 v2-wait 的 MSHR、fill、deferred-store 和冲突范围。第三轮补齐无预取分支的共享 prepare 退休准入检查，第四轮进一步将完整模式的 store callback/publish 接到已有专用 `store_hit_latency`，配置缺项直接拒绝；冻结兼容路径不改变。最终使用 `evaluation-r4/`，前三轮输出未覆盖。系统 /tmp 耗尽导致一次链接失败，随后使用隔离目录的 compiler-tmp 完成构建。

## 2. 已实现的修复

| 范围 | 候选行为 | 验证边界 |
|---|---|---|
| owner/follower | 新冷请求从最终自身准入计算完整配置路径；仅同事务、内容/权限 generation、序列和时钟一致的本核来源复用剩余等待 | 冷 owner、未来 parent、换代、纯 load 见证 |
| prepare/store | 保留退休后 GETX/UPGRADE；数据、权限、controller、写入和 callback 分离；SQ 消费最终 callback 的剩余部分，多片段取 callback 最大值；callback 使用独立配置的 store 边 | 迟到 SQ、store 接管、原数据边不可被 store 改写、专用 callback 与缺项拒绝 |
| 容量和发布 | 私有/LLC service 保存资源与释放边；未来服务可保持功能归属，不能冒充已准入 timing follower；共享容量不借用无关全局 frontier | 容量、合并、回滚、EOF 释放 |
| LLC/remote/预取 | 迁入统一服务引用、预取取消/合并、LLC 分层发布、驻留数据权限升级 | 功能/资源测试通过；本批五负载预取关闭，开启预取的 CPI 未验收 |
| 两阶段框架 | descriptor 固定深度求值，本核 receipt 纳入原 feedback snapshot；prepare 在一次 shared weave 前按 producer 退休/已提交前缀排序 | 无新增逐周期流水线或整窗收敛 |

关键实现：[bounded_service_consumer.hpp](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/source/include/fastsim/bounded_service_consumer.hpp)、[service_time_model.hpp](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/source/include/fastsim/service_time_model.hpp)、[legacy_prepare_timing.hpp](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/source/include/fastsim/legacy_prepare_timing.hpp)、[hierarchy_service.hpp](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/source/include/fastsim/hierarchy_service.hpp)、[simulator.cpp](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/source/src/simulator.cpp)。

三个候选模式的 15 次真实回放另核验完整 user/kernel 宏指令和 UOP 人口、逐核 task 宏指令人口、功能输入内容身份、退休后 prepare 开启与 EOF 无活跃服务；证据：[review-verification-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/review-verification-r4.json)。

## 3. 同输入 CPI

四核、相同 FST/companion/owner 输入，native 预取与退休前 early prepare 关闭，FastSim 退休后 prepare 保持开启。输入范围为 user-plus-kernel，本批 CPI 的 active scope 为 workload-task，使用维护验证器核验的各自 task 宏指令分母；不使用含 idle 的 measured_cycles。并行回放用于精度与功能证据，不能作为吞吐认证。

\(AE_i=|\widehat{CPI}_i-CPI_i|,\quad RE_i=100(\widehat{CPI}_i-CPI_i)/CPI_i\)。\(MAE=\frac15\sum_i AE_i,\quad MAPE=\frac15\sum_i |RE_i|\)。

| 版本 | CPI MAE（cycles/macro） | CPI MAPE |
|---|---:|---:|
| 历史 R0/R1 冻结报告 | 0.191599 | 12.99% |
| 历史漂移工作区 wait | 0.338723 | 28.12% |
| 历史仅修 store 重复计费的 publish | 0.370324 | 24.15% |
| 本轮 frozen | 0.191599 | 12.99% |
| 本轮 v2-wait | 0.191599 | 12.99% |
| 本轮 v2-merge | 3.754077 | 234.95% |
| 本轮 v2-publish | 3.774713 | 236.67% |

历史结果用于追溯；本轮冻结二进制重跑是候选的直接对照。

| 负载 | gem5 CPI | 冻结 CPI / AE / RE | 候选 wait CPI / AE / RE | 候选 merge CPI / AE / RE | 候选 publish CPI / AE / RE |
|---|---:|---:|---:|---:|---:|
| c04-777.zstd_r | 1.206049 | 1.142316 / 0.063733 / -5.28% | 1.142316 / 0.063733 / -5.28% | 2.953635 / 1.747586 / +144.90% | 3.010020 / 1.803971 / +149.58% |
| c04-801.xz_s | 0.575297 | 0.601359 / 0.026062 / +4.53% | 0.601359 / 0.026062 / +4.53% | 1.890380 / 1.315083 / +228.59% | 1.901485 / 1.326188 / +230.52% |
| c04-817.flac_s | 0.284584 | 0.318433 / 0.033849 / +11.89% | 0.318433 / 0.033849 / +11.89% | 0.466393 / 0.181809 / +63.89% | 0.468046 / 0.183462 / +64.47% |
| c04-854.graph500_s | 0.966422 | 0.835896 / 0.130526 / -13.51% | 0.835896 / 0.130526 / -13.51% | 2.288036 / 1.321614 / +136.75% | 2.288067 / 1.321644 / +136.76% |
| c04-872.marian_s | 2.364997 | 3.068823 / 0.703826 / +29.76% | 3.068823 / 0.703826 / +29.76% | 16.569292 / 14.204295 / +600.61% | 16.603297 / 14.238300 / +602.04% |

逐核 task 宏指令分母（核 0/1/2/3）；四个模式均与独立 native 参考逐核一致：

| 负载 | 核 0 / 1 / 2 / 3 | 总分母 | active end policy |
|---|---:|---:|---|
| c04-777.zstd_r | 7110568 / 2814157 / 7029016 / 7114857 | 24068598 | retired-stream-completion-v1 |
| c04-801.xz_s | 1025427 / 7379640 / 8187982 / 8264989 | 24858038 | retired-stream-completion-v1 |
| c04-817.flac_s | 4539322 / 9079052 / 8430507 / 8992018 | 31040899 | retired-stream-completion-v1 |
| c04-854.graph500_s | 7769554 / 7775437 / 7783948 / 7781279 | 31110218 | retired-stream-completion-v1 |
| c04-872.marian_s | 5534751 / 5542553 / 5732337 / 7552101 | 24361742 | retired-stream-completion-v1 |

所有 AE 单位均为 cycles/macro。逐项来源、范围、人口检查见 [repair-comparison-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/repair-comparison-r4.json)；原始回放 argv、二进制、配置和输出哈希保存在各 case 的 `.run.json`。

相对同输入冻结基线的直接验收（MAE 与 MAPE 分别比较，逐负载恶化单列）：

| 模式 | MAE 增量（cycles/macro） | MAPE 增量（百分点） | AE 恶化负载数 | 精度结论 |
|---|---:|---:|---:|---|
| v2-merge | +3.562478 | +221.95 | 5/5 | 未通过；不得宣称优化已验收 |
| v2-publish | +3.583114 | +223.68 | 5/5 | 未通过；不得宣称优化已验收 |

跨层修复候选已形成，方案仍有未闭合项，默认推广未通过全部门禁。中性迁移、机制修复和真实负载精度分别给出结果；本报告不将定向测试成功视为完整精度验收。

共享容量与 prepare 准入诊断（选择时钟中的周期，不能直接当作最终 CPU CPI 的归因量）：

| 模式 / 负载 | 创建 / 合并 | 容量等待次数 / 周期 | prepare 检查 / 延后 | EOF 活跃服务 |
|---|---:|---:|---:|---:|
| v2-merge / c04-777.zstd_r | 1043228 / 1224515 | 59277 / 9710070 | 2834581 / 2834581 | 0 |
| v2-merge / c04-801.xz_s | 232168 / 522387 | 39460 / 5934289 | 1756297 / 1756297 | 0 |
| v2-merge / c04-817.flac_s | 50120 / 147869 | 3089 / 299242 | 517663 / 517663 | 0 |
| v2-merge / c04-854.graph500_s | 835227 / 4752878 | 1 / 13 | 3129 / 3129 | 0 |
| v2-merge / c04-872.marian_s | 1139009 / 2915797 | 397865 / 347532268 | 2394322 / 2394322 | 0 |
| v2-publish / c04-777.zstd_r | 1323881 / 1357721 | 98775 / 15836334 | 2831545 / 2831545 | 0 |
| v2-publish / c04-801.xz_s | 276533 / 561490 | 44398 / 16799579 | 1770398 / 1770398 | 0 |
| v2-publish / c04-817.flac_s | 55344 / 158312 | 3650 / 348396 | 517231 / 517231 | 0 |
| v2-publish / c04-854.graph500_s | 835391 / 4755051 | 2 / 26 | 3001 / 3001 | 0 |
| v2-publish / c04-872.marian_s | 1401628 / 3198042 | 453596 / 503765838 | 2390707 / 2390707 | 0 |

## 4. Native PMU 来源与覆盖

只摘录维护入口 `run_native_kernel_fastsim_validation.py --source-root ... --stats-dir ... --retired-cache-per-event` 的结果。cache 参考来自独立 Ruby/SLICC native 审计，未使用 kernel-events 的 path-class 字段。分支与 tag miss 是 workload-task 语义诊断；不等于真机 perf 认证。

原线程调度在 Python 扫描中主要占用单核，已停止并保留中间审计目录为 `*-native-threaded-aborted`。最终审计通过独立调度适配器将 case worker 换为五个进程；原维护验证器的 run_case、来源、指纹、守恒、评分和汇总代码保持不变。20 份模拟回放全部复用，没有因审计调度改变而重新模拟。调度与工具身份：[native-process-scheduling.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/native-process-scheduling.json)。审计并行度不构成 FastSim 吞吐改善证据。

| 版本 | source/守恒通过 | native tag/branch 可评分 | 可重试 / failed |
|---|---:|---:|---:|
| frozen | 3/5 | 3/5 | 2 / 2 |
| v2-wait | 3/5 | 3/5 | 2 / 2 |
| v2-merge | 3/5 | 3/5 | 2 / 2 |
| v2-publish | 3/5 | 3/5 | 2 / 2 |

20 份审计结果均已返回。原生汇总的 pending 是失败用例仍有重试额度，不表示有审计仍在运行。原生 source gate 失败独立保留；已有 `scored-active` 且总量/逐核宏指令人口一致的 CPI 仍可独立比较。PMU 汇总仅使用通过 source gate 的负载，不能代表完整五负载主 PMU 精度。

| 版本 / 负载 | source gate 状态 | 失败原因 |
|---|---|---|
| frozen / c04-801.xz_s | failed | ValueError: native retired-cache unknown membership: core=2 scope=workload_task event=retired_l1_load_misses unknown=1 |
| frozen / c04-872.marian_s | failed | ValueError: native retired-cache unknown membership: core=1 scope=workload_task event=retired_l1_load_misses unknown=1 |
| v2-wait / c04-801.xz_s | failed | ValueError: native retired-cache unknown membership: core=2 scope=workload_task event=retired_l1_load_misses unknown=1 |
| v2-wait / c04-872.marian_s | failed | ValueError: native retired-cache unknown membership: core=1 scope=workload_task event=retired_l1_load_misses unknown=1 |
| v2-merge / c04-801.xz_s | failed | ValueError: native retired-cache unknown membership: core=2 scope=workload_task event=retired_l1_load_misses unknown=1 |
| v2-merge / c04-872.marian_s | failed | ValueError: native retired-cache unknown membership: core=1 scope=workload_task event=retired_l1_load_misses unknown=1 |
| v2-publish / c04-801.xz_s | failed | ValueError: native retired-cache unknown membership: core=2 scope=workload_task event=retired_l1_load_misses unknown=1 |
| v2-publish / c04-872.marian_s | failed | ValueError: native retired-cache unknown membership: core=1 scope=workload_task event=retired_l1_load_misses unknown=1 |

| 版本 / 事件 | 有限 APE 数 | MAPE | P50 / P90 / P99 APE | WAPE | 有符号 bias |
|---|---:|---:|---:|---:|---:|
| frozen / branch_misses | 3 | 7.40% | 2.66 / 14.55 / 17.23% | 2.64% | 2.64% |
| frozen / l1d_tag_misses | 3 | 21.27% | 22.21 / 37.71 / 41.19% | 12.53% | 12.53% |
| frozen / llc_tag_misses | 3 | 10.10% | 2.46 / 22.76 / 27.33% | 0.58% | -0.58% |
| frozen / private_l2_tag_misses | 3 | 18.66% | 20.58 / 32.44 / 35.10% | 4.37% | 4.37% |
| v2-wait / branch_misses | 3 | 7.40% | 2.66 / 14.55 / 17.23% | 2.64% | 2.64% |
| v2-wait / l1d_tag_misses | 3 | 21.27% | 22.21 / 37.71 / 41.19% | 12.53% | 12.53% |
| v2-wait / llc_tag_misses | 3 | 10.10% | 2.46 / 22.76 / 27.33% | 0.58% | -0.58% |
| v2-wait / private_l2_tag_misses | 3 | 18.66% | 20.58 / 32.44 / 35.10% | 4.37% | 4.37% |
| v2-merge / branch_misses | 3 | 7.40% | 2.66 / 14.55 / 17.23% | 2.64% | 2.64% |
| v2-merge / l1d_tag_misses | 3 | 31.58% | 26.39 / 59.94 / 67.49% | 15.16% | 15.16% |
| v2-merge / llc_tag_misses | 3 | 10.09% | 2.43 / 22.75 / 27.33% | 0.58% | -0.57% |
| v2-merge / private_l2_tag_misses | 3 | 17.52% | 18.32 / 31.04 / 33.91% | 3.92% | 3.92% |
| v2-publish / branch_misses | 3 | 7.40% | 2.66 / 14.55 / 17.23% | 2.64% | 2.64% |
| v2-publish / l1d_tag_misses | 3 | 50.91% | 56.83 / 88.05 / 95.07% | 31.89% | 31.89% |
| v2-publish / llc_tag_misses | 3 | 10.04% | 2.29 / 22.72 / 27.32% | 0.55% | -0.54% |
| v2-publish / private_l2_tag_misses | 3 | 24.29% | 35.99 / 36.69 / 36.85% | 7.56% | 7.56% |

三个退休 load miss 主事件逐项覆盖（示例采用完整 publish 候选）：

| 负载 / 事件 | 原生 reference unknown | 全量预测 | 原生计数 | AE | APE | 状态 |
|---|---:|---:|---:|---:|---:|---|
| c04-777.zstd_r / retired_l1_load_misses | 0 | 1346494 | 680317 | 666177 | 97.92% | incomplete-evidence |
| c04-777.zstd_r / retired_l2_load_misses | 0 | 316267 | 30763 | 285504 | 928.08% | incomplete-evidence |
| c04-777.zstd_r / retired_l3_load_misses | 0 | 310765 | 26416 | 284349 | 1076.43% | incomplete-evidence |
| c04-801.xz_s | — | — | — | — | — | 不可评分，见 validation.json |
| c04-817.flac_s / retired_l1_load_misses | 0 | 69269 | 44624 | 24645 | 55.23% | incomplete-evidence |
| c04-817.flac_s / retired_l2_load_misses | 0 | 20164 | 1048 | 19116 | 1824.05% | incomplete-evidence |
| c04-817.flac_s / retired_l3_load_misses | 0 | 19201 | 165 | 19036 | 11536.97% | incomplete-evidence |
| c04-854.graph500_s / retired_l1_load_misses | 0 | 5311121 | 4905964 | 405157 | 8.26% | incomplete-evidence |
| c04-854.graph500_s / retired_l2_load_misses | 0 | 282263 | 277711 | 4552 | 1.64% | incomplete-evidence |
| c04-854.graph500_s / retired_l3_load_misses | 0 | 282172 | 277638 | 4534 | 1.63% | incomplete-evidence |
| c04-872.marian_s | — | — | — | — | — | 不可评分，见 validation.json |

原生 unknown 非零的事件不计入完整主 PMU 精度；零 unknown 的可比较诊断也保留 target ISA/service 资格与硬件等价未认证标记。本报告不将不完整五负载集合聚合为新的完整主 PMU 精度。

## 5. 尚未验收的边界

共享功能/Tag 发布仍采用一次 selected-calendar replay，最终 CPU admission 由后续 feedback 得出。当前实现明确输出 `shared_final_cpu_admission_certified=false`；本核响应锚点修复不能自动证明跨核发布、跨窗 service 可见性与最终 CPU 准入完全耦合。未知来源只作完整配置路径近似，不索引为已证实 follower 来源；新时长模型仍需按协议路径独立验收，不能通过负载特判或常数拟合修正误差。

专用 store callback 配置已逐负载、逐参与核对照同次采集的 Ruby controller 字段：L1 为 L0 mandatory queue；L2 为 L0 mandatory queue + L0 request_latency + Ruby L1 l1_response_latency。本批实际配置为 L1=1、L2=12 cycles，与定向测试夹具的 1/5 分开；配置参数没有按 CPI 调整。证据：[store-callback-parameter-provenance-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/store-callback-parameter-provenance-r4.json)。

时长来源核验尚未闭合：配置路径的 DRAM 部分将 tRP/tRCD/tCL/burst/front/back 相加作为有界近似，权限完成暂与 lower data 共用近似时长；共享 publication/controller 仍含固定一周期排序边。这些不是已认证的逐协议 gem5 映射，不能以 source passed 宣称时长对齐门禁通过。本轮没有按 CPI 调整配置常数。

尚未完成 30 次全输入串行 Zstd/XZ 配对吞吐验证；本轮无吞吐提升或无回归结论。预取路径的定向功能测试不能代替开启预取的真实 CPI 验收。精度门禁结论由上表与逐负载证据决定，构建/source passed 不代表精度已改善。

## 6. 完整证据

- frozen：[summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/summary.md)；[summary.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/summary.json)；[retired-cache-summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/retired-cache-summary.md)。
- v2-wait：[summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-wait-native/summary.md)；[summary.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-wait-native/summary.json)；[retired-cache-summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-wait-native/retired-cache-summary.md)。
- v2-merge：[summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-merge-native/summary.md)；[summary.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-merge-native/summary.json)；[retired-cache-summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-merge-native/retired-cache-summary.md)。
- v2-publish：[summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-publish-native/summary.md)；[summary.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-publish-native/summary.json)；[retired-cache-summary.md](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/evaluation-r4/v2-publish-native/retired-cache-summary.md)。
- 修复设计：[fastsim-r01-v2-wait-optimization-design-20261111.md](file:///data00/yinhaolang/FastSim/fastsim-r01-v2-wait-optimization-design-20261111.md)。
- 候选冻结：[candidate-freeze-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/candidate-freeze-r4.json)。
- 源码差异：[repair-r4.patch](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/repair-r4.patch)；独立性核验：[review-verification-r4.json](file:///data00/yinhaolang/FastSim/tmp/r01-v2-repair-exclusive-20261111/review-verification-r4.json)。
- 历史残差：[fastsim-v2-publish-residual-gap-analysis-20261111.md](file:///data00/yinhaolang/FastSim/fastsim-v2-publish-residual-gap-analysis-20261111.md)。
