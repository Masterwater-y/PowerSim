工作周期：2026年10月30日—11月12日。

2. 性能仿真模型优化
   1. **推进 FastSim 与 gem5-spr 的前端译码建模对齐。** 接通指令供给、传统译码、微操作缓存及微码服务路径，完善指令分配与队列约束，提升模型对 SPR 前端行为的表达能力。
   2. **修复执行调度与访存关键机制。** 修复共享端口的空档调度问题，避免未来预约误挡当前可用执行时隙；补齐 MOV / zero-idiom 消除及依赖传播，并改进退休 store prepare 与共享父服务的识别和等待建模。
   3. **开展 SPECCPU2026 典型负载验证。** 按 Zstd、XZ、FLAC、Graph500 四负载汇总，当前 CPI MAPE 为 **6.42%**、MAE 为 **0.054361 cycles/macro**；退休 load 的 L1/L2/LLC cache miss 暂定 MAPE 分别为 **37.62% / 30.63% / 37.68%**。

统计不包含 Marian。Cache miss 按四负载等权计算，采用退休 load 事件口径；XZ 原生参考各层仍有 1 条 unknown，因此上述缓存指标为暂定诊断误差，不代表完整 PMU 验收。

共享端口空档问题的例子：

假设 A、B 都只能使用同一个执行端口，每条只占用一个周期，其他资源均不限制。A 依赖一次尚未返回的 load，只能在第 100 周期发射；后处理的独立指令 B 在第 10 周期就已就绪。旧模型先给 A 预约第 100 周期，再把端口记成“第 101 周期才能用”，因此错误地让 B 等到第 101 周期。修复后保留实际预约区间，让 B 使用第 10 周期的空档，A 仍在第 100 周期发射。该例说明错误等待如何产生，不表示真实 FLAC 每条指令都多等 91 周期。

核验依据：

- [SPR 退休流前端与译码机制](docs/spr-retired-frontend-20261103.md)
- [FLAC 共享端口 gap-aware 修复](docs/spr-flac-c4-cpi-diagnosis-20261105.md#共享端口空档修复真实-flac-c4-同二进制验证)
- [任务周期与多地址空间修复](docs/spr-asid-task-clock-repair-20261104.md)
- [退休流结束计时修复](docs/spr-retirement-stop-repair-20261104.md)
- [统一父服务与快照优化](fastsim-unified-parent-service-v2-r01-implementation-20261110.md)
- [隔离重复计费修正](fastsim-corrected-publish-vs-v2-cpi-20261111.md)
- [四种 SPECspeed 负载验证](docs/spr-speed-c4-campaign-20261104.md)
- [最新五负载 CPI 与吞吐结果](docs/spr-rename-throughput-regression-20261112.md)
- [排除 Marian 后的四负载指标与来源](docs/spr-m2-four-workload-report-metrics-20261112.json)
- [SPR-M2 默认启用记录](docs/spr-m2-rename-elimination-default-20261112.md)
