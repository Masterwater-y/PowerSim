# Stockfish ordinary-load 就绪路径修复（2026-09-12）

后续决定：用户随后明确要求默认应用该配置。维护别名现已切换，并完成 10 负载 ×
C4/C8/C16/C32 验证，见[默认选择与 40 项结果](load-ready-default40-20260912.md)。
下文保留切换前的实现、证据和候选阶段决定；“不切换默认”不是当前配置状态。

状态：代码和定向反例已实现；提供显式候选配置，不切换维护默认。
Stockfish 的组件证据及四核数回归显著改善，但其他负载存在回退，尚未通过独立
held-out、参数趋势和 quiet-host 吞吐门禁。不得把本次结果称为全模型默认准入。

## 1. 实现与边界

之前同一 gem5 二进制、同一共同窗口的 native response 与 child issue 对齐证明：
当前目标 CPU 的驻留普通 load 从 issue 到 response 为 2 cycles，从 response 到
依赖 ALU issue 再需要 1 cycle。FastSim 把普通 load 和 atomic 共用的下界设为 4，
在真实存在的 same-PC store→load 预测依赖上，把 load/add/store 链从 5 拉长到 6。
不能通过删除 same-PC 边修复，也不能把 gem5 的 load `complete_tick` 当作数据就绪。

本次改动：

- `core.ordinary_load_latency`：仅 interval core 的普通非 atomic load 下界。
  缺省为未设置，回退到原 `minimum_load_latency`；atomic 仍用原值，普通 store AGU
  仍为 1 cycle。scalar/causal_read 不消费该参数，显式设置时拒绝，不静默忽略。
- `core.load_response_to_ready`：默认 0，保留旧语义；非零仅允许完整的
  interval_weave/time_epoch sparse response 路径，拒绝 causal-block/event-only
  简化路径。实际实现为：

  ```text
  ordinary_producer_ready = max(existing_producer_completion,
                               latest_ordinary_fragment_response + wakeup)
  completion = existing_shared_ready_WB_calendar(ordinary_producer_ready)
  ```

  它在原反馈循环中执行，不新增完整 UOP 扫描。实际 memory response、MSHR/Sequencer
  释放、page-walk、atomic 和 store 响应路径不因此延后。基础 FU 时序已含唤醒时不重复
  累加；请求 origin 被功能顺序推迟、或存在多个 fragment 时仍使用实际绝对响应时刻。
- 新候选 [gem5-exp-load-ready.cfg](../configs/gem5-exp-load-ready.cfg) 从维护别名继承，
  显式设置普通 load 为 3、response-to-ready 为 1。有效配置 JSON 同时导出两项。
  这是已采集目标 CPU 的时序配置，不是所有 CPU 的通用常数。

维护别名和历史版本配置均未被此次修改；Q=1024、原两阶段框架、same-PC feedback、
缓存/DRAM 参数和既有默认关闭实验保持不变。没有 workload ID、PC 特判、oracle
timing 输入或残差补偿。

## 2. 数据与口径

这是 **9 个共同终点配置、108 条逐核 trace 的有界回归**，不是新的 workload-held-out。
包含 Stockfish C4/C8/C16/C32，以及 TeaLeaf C4/C16、NAMD C4、Graph500 C8、LBM C16。
Stockfish 已用于本次归因；其他五项为已有跨负载回归，不包装为未见样本。

- 数据：`tmp/first-core-common-end-20260911/source/<case>/`。
- 规约：`first-core-target-common-end-v1`，最快核达到 10M 用户 UOP 的共同事件关闭，
  保留慢核实际人口；scope 为 `user-plus-kernel`，active 周期 / 实际退休宏指令。
- 输入包含按记录边界划分的 functional warmup，预热不计入测量；每核记录和指令边界
  使用原 `trace-scratch/functional-boundary-core*.json`，共同事件身份见
  `common-end-audit.json`。没有重采或截短 trace。
- `summarize.py` 对 9/9 配置复核原验证通过、共同事件一致、全部参与核保留、触发核
  达标、warmup 非零、user UOP 和 combined macroinstruction 逐核匹配、unknown kernel
  cycles 为零；0 个拒绝样本。旧 CPI 基线来自该 corpus 的冻结 `fastsim.json`。
- 所有 CPI 均取 `scope_metrics`，absolute error 单位为 cycles/macroinstruction，
  聚合按配置等权；分位数为 NumPy Type 7。这里只比较 combined pipeline，不声称
  单独验证 user pipeline。

## 3. Stockfish 完整输入结果

| 核数 | gem5 CPI | 旧 CPI | 修复 CPI | 旧相对误差 | 修复相对误差 | 旧绝对 CPI 误差 | 修复绝对 CPI 误差 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.662666 | 0.723330 | 0.660258 | +9.1545% | −0.3634% | 0.060664 | 0.002408 |
| 8 | 0.622407 | 0.690849 | 0.618793 | +10.9965% | −0.5806% | 0.068443 | 0.003614 |
| 16 | 0.680622 | 0.771027 | 0.678991 | +13.2827% | −0.2397% | 0.090405 | 0.001631 |
| 32 | 0.690507 | 0.778652 | 0.685977 | +12.7652% | −0.6560% | 0.088145 | 0.004530 |

| 4-case 聚合 | MAPE | APE P50 | APE P90 | APE P99 | CPI MAE |
|---|---:|---:|---:|---:|---:|
| 旧实现 | 11.5497% | 11.8808% | 13.1274% | 13.2672% | 0.076914 |
| 修复候选 | 0.4599% | 0.4720% | 0.6334% | 0.6538% | 0.003046 |

按实际宏指令权重计算的 WAPE 为 12.4261% → 0.5222%，signed bias 为
+12.4261% → −0.5222%。四项 CPI MAE 下降约 96.04%。这不能外推成全负载 MAPE。

## 4. 中间状态：不是仅靠 CPI 下降判定

在修复时序源码上重新加入仅输出的 stage exporter，对 C16 核心 0/3 的
**18,451,982 条测量记录**与原 gem5 sequence/PC 完整对齐。generic、materialized
以及带 exporter 的 generic 的 scope（除 host throughput）和 threads 精确相等。

| 核心 3 热点 | 次数 | gem5 load→ALU | 旧 load→ALU | 修复 load→ALU |
|---|---:|---|---|---|
| `0x4516be` | 222,225 | 全部 3 cycles | 全部 4 cycles | 全部 3 cycles |
| `0x454db9` | 229,376 | 全部 3 cycles | 全部 4 cycles | 229,375 次为 3；1 次为 6 |

两条都是标量 16-bit RMW，不是 SIMD 或 LOCK atomic。load→store completion 的
主体也从 6 恢复为 5；保留了 same-PC store 边，没有人为关闭串行约束。核心 0 的
`0x4513de`/`0x451a3e` 共 140,288 次 load→ALU 从全部 4 变为全部 3。

按逐条 ordered-retire 的互斥等待分类，elapsed span 守恒：

| 核心 | 旧 span 差（cycles） | 修复 span 差（cycles） | 旧 issued-load-head 差（cycles） | 修复 issued-load-head 差（cycles） |
|---:|---:|---:|---:|---:|
| 0 | +235,920 | −33,033 | +245,105 | +2,553 |
| 3 | +475,842 | +4,683 | +475,464 | −6,858 |

这些是窗口首尾退休之间的 span 分类，不是可相加到 CPI 的独立延迟项。部分 frontend、
其他执行和边界残差仍在；完整 stage 的少量跨迭代次序差异也仍存在，不能声称所有
gem5 中间状态相同或所有 same-PC 边均已精确重建。

## 5. 跨负载回归与默认决策

| 对照 | gem5 CPI | 旧 CPI | 修复 CPI | 旧相对误差 | 修复相对误差 | 旧绝对 CPI 误差 | 修复绝对 CPI 误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| TeaLeaf C4 | 1.096636 | 1.034734 | 1.039566 | −5.6447% | −5.2041% | 0.061902 | 0.057070 |
| TeaLeaf C16 | 0.646776 | 0.559825 | 0.560665 | −13.4438% | −13.3139% | 0.086951 | 0.086111 |
| Graph500 C8 | 1.828677 | 1.718738 | 1.712414 | −6.0120% | −6.3578% | 0.109939 | 0.116263 |
| LBM C16 | 3.527754 | 3.514233 | 3.510302 | −0.3833% | −0.4947% | 0.013521 | 0.017452 |
| NAMD C4 | 0.629211 | 0.563447 | 0.550976 | −10.4519% | −12.4339% | 0.065765 | 0.078236 |

| 5-case 对照聚合 | MAPE | APE P50 | APE P90 | APE P99 | CPI MAE |
|---|---:|---:|---:|---:|---:|
| 旧实现 | 7.1871% | 6.0120% | 12.2470% | 13.3241% | 0.067616 |
| 修复候选 | 7.5609% | 6.3578% | 12.9619% | 13.2787% | 0.071026 |

对照 WAPE 2.9084% → 3.0556%，signed bias −2.9084% → −3.0556%。NAMD 的
绝对误差增加 0.012471，不能用 Stockfish 收益掩盖。因此保留组件实现与明确候选，
**不更新维护别名，不加 workload 特判，不扩大矩阵宣称已准入**。

scope PMU 并非所有配置都保持不变：时序改变可以移动 epoch/cache 请求归属。
本轮没有新 strict PMU 精度结论，也没有做新微架构参数 delta/ranking 试验。
准确性运行允许与构建及其他回归并行，未做 quiet-host 顺序吞吐门禁，不能据运行
墙钟时间声称提速或维持最低吞吐。

## 6. 测试、复现与产物

行为反例经历 RED → 修复 → GREEN：驻留 load 消费者 +4 而非 +3；晚请求响应后
同周期过早可用；scalar 静默忽略新配置。新增覆盖还包括三 fragment latest return、
1-wide 写回冲突、跨 microbatch/epoch RAW、generic/materialized 等价、atomic/store
隔离、MSHR/Sequencer 在 response 而非 wakeup 释放，以及非法取值/不支持模式拒绝。

默认验证命令：

```bash
cmake --build build -- -j16
./build/fastsim_tests
```

最终构建和整套测试均通过。加入不支持模型的配置拒绝守卫后，9 项完整输入及
C16 关闭修复/generic 对照共 11/11 次复验，其 scope 和 threads 均与守卫前精确
相等；最终默认关闭 C16 仍精确复现原冻结结果。只读审查所发现的配置兼容问题
已闭合，没有未处理的 Critical/Important 问题。

显式使用修复配置：

```bash
./build/fastsim simulate --config configs/gem5-exp-load-ready.cfg \
  --manifest <对应共同窗口的manifest.txt> \
  --measurement-scope user-plus-kernel --cores 16 --output <新结果.json>
```

本轮实验目录：`tmp/stockfish-load-ready-fix-20260912.Mmqbrd/`。

- `before/`：本次改动前文件的精确快照，用于隔离已有脏工作区改动。
- `run_regression.py`、`runs/*/{config.cfg,run.log,fastsim.json,result.json}`：命令、
  frozen include、scope、CPI/绝对误差、人口校验和二进制哈希。
- `summary.json` / `summarize.py`：9-case 逐核及聚合检查；`final-parity.json` /
  `final_parity.py`：加入最终配置拒绝守卫后的逐配置精确复验。
- `gem5-chains-core{0,3}.json`、`fixed-analysis-core{0,3}.json`、`stage-summary.json`：
  完整配对链、互斥等待分类；`simulator-stage.cpp` 为输出专用副本，不编入维护二进制。
- 最终生产源码构建 SHA256：
  `f704f0bf52f95ade02a6d9eb5f988de93b290f2ca6f109b9425d2265db822e73`。
  组件候选初版 `a898ce7dc305b02cda0fe835491d1fe7d420f285eece16d070d7f20c6a87a3d3`
  与最终版仅增加不支持模型的配置拒绝守卫；时序修复相同。
- 之前 gem5 native 归因：`tmp/stockfish-tail-20260912/REPORT.md`，本轮没有新的
  gem5 仿真，没有把其标签作为 FastSim 推理输入。

后续默认准入应先解释 NAMD 反向残差，再使用独立窗口/负载、参数趋势和 quiet-host
吞吐门禁验证，不回到以 CPI 残差调整 load latency 或删除 same-PC 依赖的做法。
