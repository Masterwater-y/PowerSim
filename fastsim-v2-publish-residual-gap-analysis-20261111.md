# v2 到 publish 的剩余精度缺口

本次将 R0/R1 冻结版本变化、完整合并逻辑和 Tag 发布分别做同输入对照。所有运行保留退休后 prepare，硬件预取关闭，使用共同 FST、4 核、workload-task active CPI。逐核宏人口、功能身份、独立 native 来源及守恒检查通过。新增加的是当前 wait 和修正 merge 的八次回放，Marian 复用已完成同输入模式回放。本次没有修改生产模型。

## CPI 与误差

RE = (FastSim CPI / gem5 CPI − 1) × 100%；AE = abs(FastSim CPI − gem5 CPI)，单位 cycles/macro。

| 负载 | 模式 | gem5 CPI | FastSim CPI | RE | AE (cycles/macro) |
|---|---|---:|---:|---:|---:|
| c04-777.zstd_r | frozen-v2-wait | 1.206049 | 1.142316 | -5.28% | 0.063733 |
| c04-777.zstd_r | current-v2-wait | 1.206049 | 1.364163 | +13.11% | 0.158114 |
| c04-777.zstd_r | corrected-v2-merge | 1.206049 | 1.092102 | -9.45% | 0.113947 |
| c04-777.zstd_r | corrected-v2-publish | 1.206049 | 1.114011 | -7.63% | 0.092038 |
| c04-801.xz_s | frozen-v2-wait | 0.575297 | 0.601359 | +4.53% | 0.026062 |
| c04-801.xz_s | current-v2-wait | 0.575297 | 0.605644 | +5.27% | 0.030346 |
| c04-801.xz_s | corrected-v2-merge | 0.575297 | 0.605456 | +5.24% | 0.030159 |
| c04-801.xz_s | corrected-v2-publish | 0.575297 | 0.648483 | +12.72% | 0.073185 |
| c04-817.flac_s | frozen-v2-wait | 0.284584 | 0.318433 | +11.89% | 0.033849 |
| c04-817.flac_s | current-v2-wait | 0.284584 | 0.319879 | +12.40% | 0.035295 |
| c04-817.flac_s | corrected-v2-merge | 0.284584 | 0.325100 | +14.24% | 0.040516 |
| c04-817.flac_s | corrected-v2-publish | 0.284584 | 0.325841 | +14.50% | 0.041256 |
| c04-854.graph500_s | frozen-v2-wait | 0.966422 | 0.835896 | -13.51% | 0.130526 |
| c04-854.graph500_s | current-v2-wait | 0.966422 | 1.745513 | +80.62% | 0.779090 |
| c04-854.graph500_s | corrected-v2-merge | 0.966422 | 0.699529 | -27.62% | 0.266893 |
| c04-854.graph500_s | corrected-v2-publish | 0.966422 | 0.699532 | -27.62% | 0.266891 |
| c04-872.marian_s | frozen-v2-wait | 2.364997 | 3.068823 | +29.76% | 0.703826 |
| c04-872.marian_s | current-v2-wait | 2.364997 | 3.055769 | +29.21% | 0.690772 |
| c04-872.marian_s | corrected-v2-merge | 2.364997 | 3.387171 | +43.22% | 1.022174 |
| c04-872.marian_s | corrected-v2-publish | 2.364997 | 3.743249 | +58.28% | 1.378251 |

| 模式 | 五例 CPI MAE (cycles/macro) | MAPE |
|---|---:|---:|
| frozen-v2-wait | 0.191599 | 12.99% |
| current-v2-wait | 0.338723 | 28.12% |
| corrected-v2-merge | 0.294738 | 19.95% |
| corrected-v2-publish | 0.370324 | 24.15% |

## 各步 CPI 差值

下表为各模式间的 CPI 算术差，单位 cycles/macro；正值表示变慢，负值表示变快。变快不一定提高准确性。各项不是互斥物理 stall 的归因，反馈交互仍包含在模式差值内。

旧有误差与新增差距需要分开：Marian 的 AE 1.378251 = 冻结 v2 的 0.703826 + 新增 0.674425 cycles/macro；Graph500 的 AE 0.266891 = 0.130526 + 0.136365 cycles/macro。两例预测偏差方向一致，因此可做该算术拆分；这不是独立物理机制的周期分摊。

| 负载 | 冻结 v2→当前 wait | 当前 wait→修正 merge | 修正 merge→修正 publish | 总差值 |
|---|---:|---:|---:|---:|
| c04-777.zstd_r | +0.221846 | -0.272061 | +0.021909 | -0.028306 |
| c04-801.xz_s | +0.004285 | -0.000187 | +0.043026 | +0.047124 |
| c04-817.flac_s | +0.001446 | +0.005221 | +0.000741 | +0.007408 |
| c04-854.graph500_s | +0.909616 | -1.045984 | +0.000003 | -0.136365 |
| c04-872.marian_s | -0.013054 | +0.331402 | +0.356077 | +0.674425 |

## 冷 miss 的准入锚点见证

使用当前隔离修正 archive，构造 32 条退休记录：load A，另一个冷行的 load B，其 producer dependency 指向 A；后续另一条 load 访问 B 同行。没有 store、没有硬件预取，三种模式人口均为 32。此见证比较模型内部响应边，不是 gem5 微基准精度评分。

| 模式 | 序号 | canonical issue | canonical response | CPU issue | CPU response | 响应时长 |
|---|---:|---:|---:|---:|---:|---:|
| v2-wait | 0 | 5 | 213 | 5 | 251 | 246 |
| v2-wait | 2 | 6 | 261 | 251 | 497 | 246 |
| v2-wait | 3 | 6 | 8 | 6 | 8 | 2 |
| v2-merge | 0 | 5 | 215 | 5 | 215 | 210 |
| v2-merge | 2 | 6 | 263 | 215 | 263 | 48 |
| v2-merge | 3 | 6 | 264 | 6 | 264 | 258 |
| v2-publish | 0 | 5 | 215 | 5 | 215 | 210 |
| v2-publish | 2 | 6 | 263 | 215 | 263 | 48 |
| v2-publish | 3 | 6 | 264 | 6 | 264 | 258 |

## 实现差异与证据边界

必须区分冻结 v2 与当前二进制的 v2-wait。冻结源码 `tmp/parent-service-v2-r01-exclusive-20261110/source/src/simulator.cpp:12941` 使用最终 admission 与 selected issue 相等的严格资格判断；未在普通 load 入口调用 `ServiceTimeModel::evaluate`。当前 wait 新增实际准入处的时长重算、拆边和资源所有权。Graph500 新路径产生 267,382 个 split service，follower 等待从冻结版的 1,288,590 个、176,612,360 wait_cycles 扩大到当前 wait 的 3,087,868 个、667,006,809 wait_cycles。新模型的固定配置时长与队列摘要有明显 保守性；这里证明运行路径与等待结果改变，不宣称已把全部高估精确分摊给某个时长项。

`v2-wait` 的父服务在 CPU 实际准入处建模；`src/simulator.cpp:13100` 的完整模式改为引用 shared hierarchy 的 immutable receipt。在身份和窗口漂移检查通过时，它直接用 max(actual_admission, canonical_data_or_controller) + local callback；没有同时将新 unique 事务的服务过程迁到实际准入。新准入已经接近或超过 canonical 完成边时，新冷 miss 可能获得过短的响应。这和此前 store 旧相对时长重新收费的高估方向相反，两类错误可以同时存在。

Graph500 的原生请求人口几乎不变，而父服务选择显著变化：R0/R1 有 1,288,590 个 CPU follower 等待，完整 publish 记录 4,856,657 个 follower；其 ordinary 请求采用 canonical 完成边。旧 176,612,360 个 wait_cycles 是重叠请求等待和，不可直接当作 CPI 误差周期。新 wait_cycles=0 是响应调整计数定义下的零：完整分支多次缩短已投影响应，不能解释为所有 load 从不等待。

Marian 的 publication 会改变功能路径：修正 merge 的 task L2 tag miss 为 424,528，修正 publish 为 627,538；同 scope 独立 native 参考为 459,543。误差从 −7.62% 到 +36.56%，这些是 proxy 诊断事件。这部分增加的 miss/资源等待并不会被 store 响应接口修正自动消除。容量放大对照已有敏感性证据，但不能将其周期差或资源等待和线性相加为因果贡献。

XZ 的修正 merge→publish 同样显示 task L2 tag miss 从 58,399 增至 91,478，hierarchy resource_wait_cycles 从 8,271,868 增至 38,359,627。这与其 publication 阶段的 CPI 增量同时出现。Zstd 在 merge→publish 反而更接近 gem5，FLAC 的 publication 增量很小，Graph500 的两种 complete 模式几乎相等；不能把五例差距统一归因于延迟 Tag。

原来发现的 retired prepare 提前可见性仍是独立缺陷；不能仅凭该 store 微见证把 Graph500 的低估全归给 prepare。本次增加的纯 load 见证将普通请求的准入锚点缺口单独暴露出来。各负载剩余相对 gem5 的全部物理残差尚未完成逐事件 native 匹配，不能把模式差值直接视为严格硬件 stall 分摊。

## 为什么此前验收没有拦住

完整模式从私有 CPU 父服务改为共享权威记录，改变了服务选择、实际准入、共享容量和功能 Tag/LRU 顺序。它不是在 v2 的相同预测上仅添加观察器。原实现报告明确：完成构建与机制验收，但未恢复同输入 CPI/native 和 30 次吞吐验证。请求数、合并数、GETX/UPGRADE、资源最终释放等机制测试通过，不足以证明实际准入与各完成边在完整工作负载中一致。因此这次恶化反映的是未闭合的时间/状态合同及验收缺口，不能据此裁定统一父服务或延迟 Tag 发布必然损害精度。

修复优先级应是：统一 owner/follower 在实际准入处的有界服务边；保留父服务实际身份并让 follower 等待相同的重算完成边；同步 Tag/代次/权限、SQ callback 与容量租约的使用合同；随后核验各模式的原生功能计数和同输入 CPI。继续保留退休后 prepare、原生容量与并行两阶段，不使用 gem5 timing 作为输入，不引入逐周期 runtime 或整窗口反复收敛。

- [完整源证据、CPI 与 PMU 数据](fastsim-v2-publish-residual-gap-analysis-20261111.json)
- [新模式回放回执](tmp/publish-cpi-diagnosis-20261110/residual-modes-20261111/completion.json)
- [纯 load 见证](tmp/publish_residual_load_anchor_witness_20261111.json)
- [纯 load 调用代码](tmp/publish_residual_load_anchor_witness_20261111.cpp)
- [原实现验收范围](fastsim-unified-parent-service-complete-validation-20261110.md)
