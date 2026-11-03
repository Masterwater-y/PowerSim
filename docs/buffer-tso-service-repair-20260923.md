# Buffer TSO 有界服务事务实现与验证（2026-09-23）

> 后续已接入 FR-FCFS 事务认证。本文保留第一阶段结果；当前机制与验证见
> [FR-FCFS 接入报告](buffer-tso-service-frfcfs-20260923.md)。原先按 scheduler
> 名称整批拒绝过于保守：这两个 C4 native 配置的有效选择窗口实际为 1。

**结论：已实现默认关闭的事务候选和回退，但完整的高吞吐、广覆盖 MSHR 方案尚未完成。native 两个负载全部因 FR-FCFS 回退，CPI/PMU 无收益；不允许据此启用默认路径。**

设计见 [设计文档](superpowers/specs/2026-09-23-buffer-service-repair-design.md)，执行记录见 [计划](superpowers/plans/2026-09-23-buffer-service-repair.md)。实验文件在 `tmp/buffer-service-repair-20260923/`。未提交或推送；保留工作区原有改动。

## 实现

- `ruby.buffer_service_repair=false` 为默认值；关闭时沿用既有路径。旧 `ruby.buffer_line_coalescing` 与 private-read-service 实验仍保留，不能与新候选同时开启。
- 每次尝试按修正后的准入时间排序，不追加 per-core 单调排序约束；合并决定在缓存查找之前发生，同时控制缓存/PMU、服务响应和 MSHR 占用。合并请求仍占 CPU outstanding 槽位。L1-hit 父请求也参与。
- 固定点通过才提交；失败恢复私有缓存、目录、LLC、DRAM 队列、私有 PMU 与 Sequencer 状态，再执行基线。没有事后扣减 PMU。
- 默认累计额度为基线 UOP+访存事件数的 10%；预检查每访存扣一单位，每次重放扣 UOP+访存数。默认额度上限/epoch 工作上限 4096，最多 3 次尝试。零额度直接回退。这个计数不是墙钟开销百分比。
- 分别输出 budget、horizon、write/permission conflict、controller、special-event、late-issue、unstable 回退计数。跨 epoch 成功状态保留；回退丢弃服务表身份并计数，属于明确的近似。

## 机制和回归

`cmake --build build -- -j16`、`./build/fastsim_tests` 通过。新增断言覆盖：4 个同线读只产生 1 次查找且共享响应；CPU 容量 2 时重新分类成 2 次查找，容量 1 时为 4 次；零/微小预算基线一致；两种读写顺序的 PMU/控制器回滚；FR-FCFS、I-side 回退；累计预算及预检查扣费。

定向探针还观察到 RAW 顺序反转经 2–3 次重放后接受，以及跨 epoch 压力下的成功/回退混合结果；这不是完整的跨 epoch 服务身份正确性证明。通用审计模式的 read/write/read 探针在 baseline 和 candidate 都有既存的 `response residual stage accounting is not conserved` 异常；本次未把它报告为通过，也没有将它归因于新候选。

审查修正了：准入记录之后的 FU 冲突、后续 FR-FCFS 改写响应、先写后读漏检、外层回退恢复废弃身份、I-side 零时间记录，以及预检查未扣预算。尚未闭合的机制采用显式回退。

## Native 两负载验证

两臂均通过独立硬件身份检查 2/2、维护的 native validator 2/2、PMU scored 2/2，failed/pending 均 0。缓存真值来自 conserved native Ruby/SLICC summaries，未使用 TaoTrace path-class。映射仍为 proxy/diagnostic；不是严格硬件 PMU 认证。范围为 user-plus-kernel/common-end，CPI 分母为 macro instructions。

| Case | gem5 CPI | baseline / candidate CPI | CPI 相对误差绝对值 | CPI 绝对误差（cycles/macro） | controller 回退 epoch |
|---|---:|---:|---:|---:|---:|
| uarch-baseline-c04-811.tealeaf_s | 1.004183 | 0.860056 | 14.3527% | 0.144128 | 5217 |
| uarch-baseline-c04-857.namd_s | 0.479341 | 0.389645 | 18.7123% | 0.089696 | 2681 |

两臂 CPI MAPE 均为 16.532511%，CPI MAE 均为 0.116912 cycles/macro。两臂全部目标 scope 指标（包括 PMU）一致；candidate committed epochs/events 均为 0。未扩大到 40 case，因为该实现对当前 FR-FCFS 配置没有覆盖。

## 吞吐

串行 ABBA，每臂两次；使用最终冻结二进制，目标 scope 与验证结果一致。没有独占宿主机保证，小差异不能当作稳定收益。下面 native 数字测量的是**全回退路径**，不能代表服务建模成本。

| Native case | baseline M measured user UOP/s | candidate M measured user UOP/s | 变化 |
|---|---:|---:|---:|
| uarch-baseline-c04-811.tealeaf_s | 3.2352 | 3.2212 | -0.43% |
| uarch-baseline-c04-857.namd_s | 2.8016 | 2.7755 | -0.93% |

另用独立同线读的 FCFS 合成负载（1 core、100 万 UOP、Q=1024、工作额度上限显式设为 16384，累计比例仍为 10%）实测生效路径。ABBA baseline 1.0252 → candidate 1.0247 M UOP/s，变化 -0.05%。这是 standalone host benchmark，没有 native accuracy 主张。

该合成 candidate 接受 16/245 epochs、65344/1000000 访存事件，产生 59888 次合并，计费工作 196032 单位。不能把未覆盖部分算为新模型的吞吐成功。

## 未完成部分

生产目标仍需稀疏依赖/资源闭包、局部 core 重算、FR-FCFS 与共享 fill 的原子提交，以及写权限阶段服务身份。整 epoch 重放只是验证统一决策和回滚的参考实现；不能通过提高预算、移除保护或悄悄改成 FCFS 来声称解决覆盖率问题。
