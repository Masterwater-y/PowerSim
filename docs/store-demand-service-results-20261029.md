# Store demand 时延观测与单次 FR-FCFS 修复（2026-10-29）

本轮接续 [接手结果](store-chain-implementation-results-20261029.md) 和用户确认的修复顺序，增加实际 demand 时延观测，并修复 bounded 路径绕过已配置 FR-FCFS 的问题。没有调整 cache/DRAM 时延常数，没有 workload 特判，没有删除 NoAccess 屏障，没有实施 S2/S3。

## 1. 实现与机制边界

[store_demand_observer.hpp](../include/fastsim/store_demand_observer.hpp) 在最终 CPU timing 确定后采样：

$$T_i=\mathrm{response}_i-\mathrm{send}_i$$

输出样本数、精确周期总和、均值，以及与 gem5 非负 `Histogram(10)` 相同的十桶二倍扩展规则。逐核、逐 epoch 合并可精确重分桶；空人口的均值为 null。按 OriginalDemand/Takeover/Publish/Reacquire、canonical private 层级分组，同时保留完整服务 recipe 与 queue/DRAM 阶段总和。

JSON 位于 `causal_frontier.bounded_store_demand_observation`。`duration_le5_gt5_not_hit_miss` 只是时长分组，不能冒充 native hit/miss。queue 与 DRAM 阶段是 canonical 参考时钟，demand 与 recipe 是 CPU 消费的周期；本轮配置各核与 Ruby 同为 333 ticks，不能把不同 DVFS 频率的原始周期混作同单位。

`shared_queue_cycles` 包含 `memory_queue_cycles`；两列不能相加。DRAM command→completion 是另一阶段观测；完整 recipe 还包含私有路径、互连、tag/协议、返回等边，因此这些列不是完整且互斥的 CPI 分解。成功的 controller 修复会同步更新观察器使用的 queue recipe，避免把新 response 与旧 FCFS queue 混在一起。

新工具 [compare_store_demand_latency.py](../tools/compare_store_demand_latency.py) 读取冻结 ST histogram 的 `samples/mean/bucket_size`，检查 hit+miss=total、桶人口守恒、单次 dump 和 core/Ruby 时钟域；输出共同桶宽下的分布。它只生成时延诊断，不选择 native cache 真值、不评分 CPI，也不认证两侧人口与边界等价。

观测阶段使用冻结二进制 `fastsim-observation`。六项与旧 `bounded-obs` 逐字段比较，仅排除新增观测、host wall/throughput 和线程等待计数，既有语义差异为零。证据：`tmp/store-demand-service-20261029/observation-neutrality.json`。

## 2. 通用修复：有证书的单次 controller 调度

原 bounded 分支逐条调用 canonical memory replay，跳过 `apply_frfcfs_dram_repair`。因此配置声明 FR-FCFS，但实际 recipe 使用立即到达顺序的 controller 预测，六项原输出均没有 FR-FCFS 候选计数。

现有 legacy repair 依赖 MSHR arrival/completion/service-order 的迭代闭合，不能直接接到 bounded。新增路径仅用于 bounded：

1. 分类前捕获共享时序入口状态；canonical cache/directory 分类仍执行一次。
2. 冻结 canonical path-ready 到达时间，只运行一次 FR-FCFS 调度。
3. 用入口每个 CHA 的 LLC-MSHR release min-heap，按到达顺序逐条证明 slot 已释放，再填入本次候选的 completion。出现任何额外 MSHR 等待即拒绝。
4. 复用原有 fill generation/可见性 preflight 与 dirty/atomic 限制；只有全部通过才提交候选 controller 状态和 response。
5. 拒绝时保留 canonical 预测，并计数 fallback；成功或拒绝后，本 store epoch 只选择一条 response feedback 路径。

bounded 分支不进入 legacy fixed-point 循环。已有 legacy 行为保留。配置仍要求 `interval_reweave_passes=1`，且排除 causal/response-retime 等竞争求解器。

这仍是 **canonical 到达时间的预测**，不是实际 CPU canWB/send 的全窗口闭合；M5 的这一近似没有被消除。MSHR 证书不能由 store TBE 非绑定证明替代，且拒绝的 epoch 仍有已登记的 forecast 偏差。

## 3. 原有推断的修正

Marian 的大量 OriginalDemand 使完整 recipe 与实际 demand 等待接近，但这不证明服务偏差全部来自 cache 分类。XZ 的情况尤其不同：观测前完整 miss recipe 约88 cycles，并不意味着每个 demand 等待88 cycles；其实际 demand 总体均值约1.289 cycles，native ST 总体约1.285 cycles。

两侧总体人口仍明显不同。例如 Marian C4 的 FastSim bounded regular store 为1,832,386，native ST samples 为2,125,383。FastSim 排除 local/special 等成员；Sequencer 又按实际 Ruby request 的统计路径记录，合并请求与 warmup/终点在途还有各自边界。均值相近或变近不能作为完整 V1 验收。

现有 M7 private-L2 诊断的大量 excess 与 canonical store miss 数量接近，提示 prepare/demand 事件域差异。**没有从模型 miss 中减去 store 数，也没有把它改名为已修复分类。** 本轮只修复已经由源码及失败回归证明的 controller 配置绕过。

## 4. 证据、测试与复现

实验与二进制：`tmp/store-demand-service-20261029/`。最终二进制 `fastsim-final` SHA256：
`2eb820494912755b911fe617a649a08c366d0e81d5eae34bdce42155b473e94c`。

六项沿用冻结来源：XZ C4/C8、Marian C4/C8、FLAC C4、Graph500 C16。每次运行的 command/config/binary/output 指纹在 `tmp/store-bounded-ab-20261029/runs/demand-frfcfs-final/<case>/`；不生成新 FST。独立 identity 检查发现10项且全部 valid，冻结六项集合已明确核对。

测试覆盖：四种角色实际剩余等待、十桶扩展与合并、跨 chunk/SQ/local 人口及审计周期总和、单次 FR-FCFS、紧 MSHR 的精确 canonical fallback、分类次数与退休人口、accepted queue recipe 回归。所有行为修复均先复现失败再转绿。

- `cmake --build build -- -j16`
- `LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests`
- bounded component / integration 两个直接测试入口
- `python3 tests/test_store_demand_report.py`（3项）与功能审计测试（6项）
- frozen gem5 的冷 MOVNTDQ 与空 SQ scalar store 两个用例；现已检查全部64对同 line、同 tick 的 prepare/takeover，以及每条 line 的成功 demand 先发送。

独立审核发现一项 queue recipe 仍保留旧值的问题，已经通过负回归修复并重新验证。其他旧工作树修改不属于本轮提交范围；本轮没有 commit。

## 5. 验收边界与后续

本轮不发布完整 retired-load PMU 认证或硬件等价结论。native cache 来源仍只使用维护 validator 的 Ruby/SLICC 守恒摘要。CPI/PMU 评分、原生来源通过、吞吐结果和机制验收分别记录；任何 source-validator 的 passed 都不代表精度达标。

保留的裁决：不根据不同域比例删除 NoAccess 屏障；两侧不同人口的均值只作诊断；单次候选不能证明资源和 fill 可见性时显式 fallback。这些选择的代价是部分误差和覆盖限制继续存在，而非通过未验证的时序或人口缩减来消除误差。

S2/S3、完整 held-out 和全矩阵安静主机吞吐验收仍待后续。单核/共享写、所有 alias/dirty/incoming-MSHR 组合的穷举及实际 CPU-arrival 闭合未被本轮测试认证。

## 6. 固定绑核吞吐 pilot

Marian C4，CPU160–175，每组严格 ABBA，各 arm 两次。只比较测量段 user UOP/s，functional warmup 与输入身份验证另计，任务宏指令人口一致。

| 对照 | 最终版/对照吞吐倍率 | 最终版测量段墙钟变化 |
|---|---:|---:|
| 修复前 bounded（含中性观测） | 0.92046 | +8.64% |
| legacy | 1.09700 | −8.84% |

新 controller 调度有真实主机开销；本 pilot 中仍快于 legacy。没有将8.64%称为≤3%通过，也没有把单 case 推广到全矩阵。本轮不是 S2/S3，未声称其增量门槛已验收。原生验证在其他 CPU 并行，且没有独立认证安静主机；因此只记为固定绑核 pilot，不作为正式吞吐 acceptance。完整8次运行、输入/二进制SHA和测量段数据见 `tmp/store-demand-service-20261029/throughput-abba.json`。

## 7. 六项同源 CPI 诊断与 native 来源门禁

最终版原生验证：发现6/6，passed6，failed0，pending0，native PMU scored6；每项 instruction_population_match=true。原生验证使用维护入口默认的 Ruby/SLICC 诊断，没有以旧 path-class 替代，也没有将 retired-load unknown 清零。逐 case 当前验证完成后使用维护入口的 write_summary 聚合报告，未重选参考。legacy/bounded 数值复用交接的冻结控制；观测 arm 的时序中性另有逐字段证明。

原生摘要：[summary.json](../tmp/store-bounded-ab-20261029/validation-demand-frfcfs-final/summary.json)；完整表：[comparison](../tmp/store-bounded-ab-20261029/comparison-legacy-bounded-demand-frfcfs-final.md)。

范围：workload-task active CPI，task-oncpu-switch-entry-v1；CPI绝对误差单位均为 cycles/宏指令。以下为诊断，不将总体改善升格为每项达标或 V1 人口对齐通过。

| case | gem5 CPI | 修复前 CPI | 相对误差 | 绝对误差 | 修复后 CPI | 相对误差 | 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| c04-801.xz_s | 0.758505 | 0.729716 | -3.80% | 0.028789 | 0.736937 | -2.84% | 0.021568 |
| c08-801.xz_s | 0.673482 | 0.654812 | -2.77% | 0.018670 | 0.668721 | -0.71% | 0.004761 |
| c04-872.marian_s | 4.933260 | 6.051448 | +22.67% | 1.118188 | 5.897300 | +19.54% | 0.964040 |
| c08-872.marian_s | 5.189270 | 6.255695 | +20.55% | 1.066425 | 5.915094 | +13.99% | 0.725824 |
| c04-817.flac_s | 0.453030 | 0.478073 | +5.53% | 0.025042 | 0.478073 | +5.53% | 0.025042 |
| c16-854.graph500_s | 2.311802 | 2.096360 | -9.32% | 0.215442 | 2.066657 | -10.60% | 0.245145 |

| arm | CPI MAE | MAPE | WAPE | signed bias |
|---|---:|---:|---:|---:|
| legacy（历史冻结控制） | 0.394800 | 17.99% | 14.74% | +4.28% |
| 修复前 bounded（冻结控制） | 0.412093 | 10.77% | 12.85% | +2.91% |
| 修复后 bounded | 0.331063 | 8.87% | 11.31% | +0.51% |

MAE/MAPE 按 case 等权；WAPE/bias 按已验证 task 宏指令数加权。修复前后四项改善、一项持平、一项变差：Graph500 C16 绝对误差由0.215442增至0.245145（增加0.029703），低于0.05停止门槛，仍需继续排查；不能把这部分变化归因于其少量store，因为controller修复同时影响load服务。Marian C4相对legacy仍略差（绝对误差增加0.028812），Marian残差未消除。

| case | 最终版 native private-L2 诊断相对误差 |
|---|---:|
| c04-801.xz_s | +141.53% |
| c08-801.xz_s | +122.51% |
| c04-872.marian_s | +111.94% |
| c08-872.marian_s | +111.31% |
| c04-817.flac_s | +27.78% |
| c16-854.graph500_s | -2.26% |

上述private-L2属于已声明的native demand诊断：prepare/demand人口差异尚未消除，不据此断言容量分类已修好，也不从excess中直接扣store。六项来源验证 passed、CPI改善、PMU精度和吞吐验收是独立结论。
