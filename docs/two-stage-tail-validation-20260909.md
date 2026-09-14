# 两阶段修复：此前 P99 尾部 case 的定点验证

日期：2026-09-09。结论：**5 个 case 改善，2 个明显恶化，当前组合未通过尾部精度验收。**
这是用户要求的旧尾部复测，没有修改模拟器、配置参数或采集新 trace。

## 输入、比较和口径

选择 [此前架构审查](architecture-evidence-audit-20260907.md) 明确列出的四个 DSE 尾部
及三个正式集合正／负尾部，共七个 case，覆盖 TeaLeaf、ASTCENC、Graph500、Stockfish。
这些输入已参与问题定位，不是 workload-held-out 验证。

- `before`：完整依赖、load 返回／写回、服务复用校验这几轮修复之前的二进制，来自
  `tmp/two-stage-dependencies-20260909/before/build/fastsim`。
- `current`：本轮开始时的 `build/fastsim`，已包含上述三轮改动。
- 对三个正式 case 补 `before-service`：已有依赖和 load／WB 修复、尚无最后一轮服务
  校验的快照，来自 `tmp/two-stage-service-validity-20260909/before/build/fastsim`。

全部使用原冻结配置、Q=1024 和 record-bounded functional warmup manifest。
manifest SHA-256、各 FST 大小／mtime 均与此前输入指纹一致；17 次模拟全部成功，
每个 case 的输出配置、user／kernel trace 人口和 macro 分母在各快照间一致。
七个 `before` CPI 与此前审查结果逐项精确相同，因此没有因 baseline 漂移制造改善。
二进制／源码指纹沿用已经通过构建及整套测试的版本，本轮不重复构建或跑测试。

测量范围都是 user-plus-native-kernel。DSE 沿用 `cycles_per_user_uop`；正式集合
沿用 `perf_like_cpi`，两者分别使用自己的冻结 gem5 参考，原始 CPI 不混合汇总。
下表误差为 `(FastSim / gem5 - 1) × 100%`；“绝对误差减少”正值表示改善。

## 累计修复效果

| Case | CPI 修改前 | CPI 当前 | 误差修改前 | 误差当前 | 绝对误差减少 |
|---|---:|---:|---:|---:|---:|
| TeaLeaf L1D64 C4，DSE | 0.503438 | 0.507553 | −17.9910% | −17.3206% | +0.6704 pp |
| TeaLeaf ROB256 C8，DSE | 0.369323 | 0.371322 | −17.3517% | −16.9042% | +0.4475 pp |
| TeaLeaf LLC128 C8，DSE | 0.404954 | 0.408110 | −17.1778% | −16.5323% | +0.6455 pp |
| ASTCENC ROB96 C4，DSE | 0.323223 | 0.329034 | −15.4799% | −13.9603% | +1.5196 pp |
| Graph500 C8，正式 | 1.619823 | 2.271196 | −13.7367% | +20.9521% | **−7.2154 pp** |
| TeaLeaf C16，正式 | 0.530476 | 0.531590 | −13.3682% | −13.1862% | +0.1820 pp |
| Stockfish C16，正式 | 0.758162 | 0.788150 | +12.8490% | +17.3125% | **−4.4635 pp** |

四个 DSE 尾部均略有改善，但严重低估依然存在。正式集合的 Graph500 从低估转为
明显高估，Stockfish 的原有正误差进一步扩大；不能用改善 case 数覆盖这两个回归。

没有重跑完整 formal40 或 DSE54，也不将七个定点 case 的 P99 当作全矩阵 P99。
不过可得一个不依赖其他 case 改善程度的**正式集合 P99 下界**：40-case Type-7 P99
等于最大 APE 的 0.61 加第二大 APE 的 0.39。已测 Graph500、Stockfish 分别有
20.9521%、17.3125% 的 APE，因此新 formal40 P99 **至少 19.5326%**；此前为
13.5930%。其他未测 case 即使全部改善，也无法降低这个下界。这是下界推导，
不是新一次完整矩阵的分位数实测。

## 哪些修复实际触发

七个当前结果都触发了 load data-ready 和 WB 容量修复。C4/C8 的 FR-FCFS 选择窗口
仍为 1，服务校验没有触发；没有为了触发而修改开关或扩大选择窗口。

| Case | load data-ready 修复 UOP | 服务校验次数 | fill 可见性失效次数 |
|---|---:|---:|---:|
| TeaLeaf L1D64 C4 | 720,143 | 0 | 0 |
| TeaLeaf ROB256 C8 | 1,591,259 | 0 | 0 |
| TeaLeaf LLC128 C8 | 1,933,663 | 0 | 0 |
| ASTCENC ROB96 C4 | 1,374,062 | 0 | 0 |
| Graph500 C8 | 4,192,017 | 0 | 0 |
| TeaLeaf C16 | 2,895,254 | 314,684 | 1 |
| Stockfish C16 | 9,999,262 | 42,251 | 3 |

这些计数是模型内部诊断，不是误差贡献或可相加的 CPI 收益。

三个定点隔离对照进一步区分最后一轮校验的影响：

| Case | 服务校验修复前误差 | 当前误差 | 最后一轮导致的 core cycles 变化 |
|---|---:|---:|---:|
| Graph500 C8 | +20.9521% | +20.9521% | 0 |
| TeaLeaf C16 | −13.1106% | −13.1862% | −46,264 |
| Stockfish C16 | +17.3102% | +17.3125% | +1,224 |

Graph500 的全部回归在最后一轮之前就已出现；该隔离对照的 core／CHA／PMU 状态
完全一致。C16 的服务校验确实生效，但 TeaLeaf 的绝对误差又扩大 0.0756 pp，
Stockfish 扩大 0.0023 pp。最后一轮对这三个 case 均没有精度收益。

这一结果不证明“等待当前 response”这个必要条件本身有错。前面的时基、已选服务路径
和基础调度仍有近似，强制完成等待它们可能暴露或放大其他误差。此次只有快照隔离，
不能进一步把 Graph500 的回归全部归给某一个队列／WB 边。后续应先核对它新增等待
所依赖的请求时基和服务有效性，再独立检查 Stockfish 的过度依赖；不能按误差调延迟
系数或直接回退一个已经证明必要的先后约束来制造 CPI 接近。

## 产物与验证边界

全部产物在 `tmp/two-stage-tail-validation-20260909/`：`inventory.json` 保存 case、
配置、输入／二进制身份，`runs/*/{before,current,before-service}/` 保存命令和结果，
`summary.json`／`summary.csv` 保存数值及最后一轮隔离对照，`summarize.py` 重算并检查
配置、人口、分母及原尾部复现。

两路进程并行做准确率，每个进程分别固定 NUMA node 0 或 1；没有把并行 wall time
作为吞吐量证据。沿用原 CPI 参考，没有新增 gem5 或硬件 PMU 合格声明。仅记录各
快照的 PMU 数量用于差分，不把它们重新包装成已完成 PMU 精度验收。
