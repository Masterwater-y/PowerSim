# Conflict-Certified Sparse Weave 设计与实施计划

日期：2026-08-01  
状态：本文保留原始设计；最新实现、决策与 92-case 结果以
`gem5-source-aligned-p99-plan.md` 第 19 节为准。B1a private-set state
certificate 和 C3-B1 response activity certificate 已启用，shared timing
certificate 与论文精度门槛仍未完成。

## 1. 目标与问题定义

本方案只使用 gem5 functional trace，不使用 `issue_tick`、`commit_tick`、
`path_class`、`coh_oracle` 等时序或路径 oracle。目标是在保留显式缓存、
目录、CHA、DRAM 和 PMU 因果关系的前提下，把执行粒度从“逐 memory event”
提升到真正的多核时间 interval。

现有 `interval_weave` 有四个核心问题：

1. 每核的访存时刻会因 OoO、cache miss 和争用反馈产生不同偏移，跨核事件
   顺序不能由固定指令块保证。
2. 所有 L1/L2 hit 也进入协调器，逐事件 tag、目录和审计开销限制吞吐量。
3. 256-UOP 既是解码块又近似成为全局推进边界；实测每个全局 step 只提交
   约一个 256-UOP 块，而不是每核推进一个 interval。
4. 当前核模型虽已有 ROB/IQ/LQ/SQ、依赖、FU 和退休约束，但 memory feedback
   仍以 interval 尾部时钟偏移近似，尚无可验证的 timing certificate。

本文设计起点的完整 C4/C8 基线为：

| 模型 | C4 UOP-CPI 平均误差 | C8 UOP-CPI 平均误差 | C4/C8 中位吞吐量 |
|---|---:|---:|---:|
| scalar | 33.858% | 30.703% | 未作为新方案基线 |
| interval_bound | 23.682% | 20.180% | 未作为新方案基线 |
| interval_weave/frontier | 11.853% | 14.990% | 15.61M / 11.53M UOP/s |
| TCSim v29 本地参考 | 5.07% | 4.62% | ML rollout，不直接比较吞吐量 |

因此，单纯增加 batch 大小、加入 ROB，或复现 ZSim bound-weave 都不构成创新。
本项目的研究目标是：**面向 functional trace 的、失败时精确回退的冲突认证
稀疏 interval 仿真**。

## 2. 总体协议

```text
functional trace（每核程序序）
        │
        ▼
4096-UOP decode microbatch（仅存储/流水化单位）
        │  跨 microbatch 持续补足 lookahead
        ▼
每核 OoO preview 到公共时间区间 [T, T+Q]
        │
        ├── 私有 L1/L2 hit：本地物化 + version/path 摘要
        └── escape event：L2 miss、权限升级、可见 eviction、atomic/fence
                              │
                              ▼
按 line / LLC set / CHA / DRAM bank 构建 sparse weave
                              │
                              ▼
状态证书 + timing causal-slack 证书
                  ┌───────────┴───────────┐
                  │通过                   │失败
                  ▼                       ▼
            commit retire prefix    最小冲突分量/因果锥 replay
                                          │连续失败
                                          ▼
                               canonical event-at-time fallback
```

当前精度合同固定 `Q=1024` core cycles。由于跨 interval response/order closure
尚未完整，Q 会影响 CPI，当前阶段不自适应增减，也不与其他组件同时调优；repair
超限时在相同 Q 内使用规范 fallback。实现中 `K` 已从 256 调整为 2048，它只决定
生产者解码/传输批量，不参与全局 horizon 的选择。该调整保持模拟周期和 PMU
语义不变，仅减少 chunk 队列、复制和唤醒成本。

## 3. 每核 OoO interval preview

每核使用固定容量或可复用 ring 状态，至少包含：

- 192-entry ROB、64-entry IQ、32-entry LQ、32-entry SQ；
- dispatch/issue/commit width；
- producer-distance scoreboard 和跨 microbatch 依赖；
- 整数、乘除、浮点、SIMD、memory、system FU/port 占用；
- in-order retirement、branch recovery、serializing UOP；
- 16 个 L1D MSHR 和多个 outstanding miss；
- 后续阶段加入 L1I、ITLB/DTLB、store-load ordering。

Preview 必须满足“未见 UOP 不会在 horizon 内 issue”的 guard。Stage 1 通过
持续解码，直到最后已见 UOP 的单调 dispatch 下界晚于 `T+Q`；因此下一个未见
UOP 也不可能在当前 epoch 内 dispatch/issue。只看第 256 个 UOP 的 retire 时间
不满足这个条件。

每个 memory UOP 输出：

- lower-bound issue time；
- lower-bound completion/retire time；
- producer edges；
- shared response 对 issue/retire 的 slack；
- 可能逃出私有层级的路径摘要。

## 4. 稀疏共享事件物化

每核先独立访问私有 L1/L2。只有下列事件进入全局 weave：

- private L2 miss；
- store/atomic 的 ownership 或 permission 请求；
- directory-visible L2 eviction/writeback；
- inclusive LLC eviction 可能触发的反向 invalidation；
- atomic、acquire/release、fence 和显式同步锚点。

在当前 C4/C8 语料中，memory events 共 3,546,652/7,093,255，L2 miss 为
812,475/1,624,678，约占 22.9%。因此在加入少量 upgrade/eviction 后，理论上
仍可在进入全局协调器前过滤约四分之三的普通访存。这个比例只是事件缩减上限，
不是吞吐量加速承诺。

私有 hit 不立即丢弃，而是记录紧凑证书：

```text
(core, line, private-set, access type, local time range,
 line version, set version, predicted path)
```

## 5. Sparse weave 与规范串行语义

全局事件不建立一个全连接 event graph，而按非交换资源分区：

- directory cache line；
- LLC set 和 victim/version；
- home CHA/slice；
- DRAM channel/bank/row；
- atomic/fence/synchronization anchor。

不同 key 且不共享容量状态的事件可以并行。FIFO 资源使用 max-plus prefix scan；
复杂 DRAM 策略按 channel 局部重放。最终语义基准不是宿主线程执行顺序，而是一个
确定性的 canonical serial replay：

```text
(modeled issue time, functional sync sequence, core id, per-core ordinal)
```

任何优化后的 epoch 若通过证书，结果必须与该 FastSim 规范串行模型一致。
“与 FastSim 规范模型一致”和“与 gem5 CPI/PMU 一致”是两个独立验收维度。

## 6. 双证书

### 6.1 状态/路径证书

证书检查：

- 私有 hit 使用的 line version 在对应时间范围内未变化；
- directory owner/sharer 与 preview 假设一致；
- LLC set 的插入、victim、replacement version 未改变记录路径；
- invalidation、remote supply、upgrade 和 writeback 顺序未改变；
- PMU delta 与规范串行重放一致；
- atomic/fence 的 functional order 未被 timing 推测覆盖。

### 6.2 OoO timing causal-slack 证书

对每个 shared response 计算其可延迟区间。若实际争用延迟：

- 未越过依赖消费者最早 issue；
- 未改变相关 memory event 的非交换顺序；
- 未越过 ROB head/retire boundary；
- 未使 queue response 穿过同一资源的冲突事件；

则无需逐 UOP 重放。否则，从最早失效节点沿 producer、ROB retirement、
memory-order 和 shared-conflict edges 构建最小 causal cone。

## 7. Selective replay 与回退

每核每 128 UOP 保存轻量 checkpoint；共享状态采用 versioned delta/undo log。
证书失败时：

1. 取冲突图的 connected component；
2. 加入受影响核上的 causal descendants；
3. 从最近 checkpoint 重放该最小分量；
4. 若失败扩散、重复两次或 replay work 超过 epoch 工作量的 5%，对整个 epoch
   使用 canonical event-at-time replay；
5. 将下一个 `Q` 减半。

只有“证书通过”或“规范回退完成”的 epoch 才能 commit。仅统计冲突但继续提交，
不能称为 conflict-certified。

## 8. Functional trace 合约

普通 UOP 只需要程序序、op class、producer distance、分支结果和物理地址。
但是仅靠每核程序序无法证明 lock/atomic 的跨核功能顺序，因此需要补充：

- `functional_sync_seq`：只给 atomic/acquire/release/fence/lock；
- 对真实数据竞争，补充 read-from anchor，或将该执行标记为 uncertified。

该字段是功能顺序，不是 cycle/tick，也不能作为 timing oracle。

## 9. 与 ZSim 的区别

[ZSim](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf) 的关键贡献是
并行 contention-free bound、短 phase 和 contention weave；其 path-altering
interference 依靠 profile，并在冲突不可忽略时手工缩短 interval。本方案复用
“bound 与共享争用分离”这一已知思想，但不把它作为创新点。

| 维度 | ZSim 风格 | 本方案目标 |
|---|---|---|
| phase | 固定/人工缩短 | 当前固定 Q=1024，由证书决定 sparse repair/fallback 范围；adaptive Q 后置 |
| 共享事件 | 记录完整/较大路径 | lazy private hit + escape event 稀疏物化 |
| path conflict | profile 后接受稀有性假设 | 每个 epoch 状态证书，失败必修复或回退 |
| timing | contention feedback/event graph | OoO causal-slack 证书 + 最小因果锥 |
| 正确性依据 | 冲突足够少 | 与规范串行模型 exact-by-fallback |
| 输入 | DBT 执行 | functional trace + 稀疏功能同步锚点 |
| PMU | 主要关注性能仿真 | PMU delta 是事务状态和证书的一部分 |

因此建议论文表述为：

> Fast, conflict-certified interval simulation for functional traces:
> exact-by-fallback shared-memory replay with sparse event materialization.

不能单独把 interval core、rollback、MVCC、temporal uncertainty 或 bound-weave
写成创新。

## 10. 实施阶段与当前状态

| Stage | 内容 | 状态 |
|---:|---|---|
| 0 | `interval_weave/frontier` 基线、C4/C8 精度和顺序审计 | 已完成 |
| 1 | 固定时间 epoch、跨 256-UOP 微批 lookahead、dispatch guard、扩展统计 | 已实现并完成 C4–C32 验证；精度 gate 未通过 |
| 2 | bulk trace、可调 transport K、稀疏审计、IQ release calendar、cache 快路径 | 已实现；当前 K=4096 |
| 3 | 并行私有 L1/L2 preview、escape-event filtering | 已实现实验路径；默认关闭 |
| 4 | state pre-certificate、cache/directory/PMU transactional undo | 部分实现；全量 version certificate 待完成 |
| 5 | LLC-set 有界事务 reweave、timing causal slack、最小 causal-cone replay | reweave 已实现但默认关闭；slack/cone 待完成 |
| 6a | 固定 Q=1024 下的 response activity 进入/退出证书与无损 reduced loop | 已实现 checkpoint-segment 第一阶段 |
| 6b | LLC/CHA/DRAM owner-domain snapshot、分量并行 replay、规范串行 fallback | 待实施；跨 Q 鲁棒性后置 |

当前代码入口：

- `sim.interval_scheduler = time_epoch`；
- 配置：`configs/gem5-v28_1-time-epoch.cfg`；
- `sim.chunk_instructions = 4096` 是当前 decode/transport microbatch；它是
  target-state invariant 的宿主框架参数，不是 Q 或微架构参数；
- `sim.interval_max_cycles = 1024` 是当前冻结的时间 epoch/精度超参数；当前模型
  尚未完成跨 interval closure，组件调优与验收不得混用其他 Q；
- `core.response_activity_certificate = true` 对足够长的 memory-free checkpoint
  segment 做进入/退出证书；证书失败不提交 tentative state，直接执行完整 sparse
  scoreboard loop；
- `sim.interval_private_preview` 控制证书式 per-core 私有 cache preview；
- `sim.interval_reweave_passes > 1` 控制事务化 feedback-corrected reweave；
- 旧 `frontier` 路径保留用于同核模型 A/B 对照。

默认验证路径会物化所有 `issue <= T+Q` 的 memory UOP，即使它仍被更老 UOP 卡在
ROB 中；这类 UOP 计入 `epoch_inflight_memory_uops`。返回延迟若把已接受前缀
推过 horizon，则增加 `epoch_corrected_horizon_violations`。该值必须在
后续由 event-level slack 证书和 replay 消除，当前非零结果明确表示尚未认证。

Stage 2 的 preview 已能把 private L1/L2 路径留在各核，只把 miss、write、
atomic 和 directory-visible eviction 物化到共享 weave。状态 pre-certificate
保守拒绝可能被跨核 write invalidation 改变 LRU/path 的 epoch；共享系统支持
LLC、DRAM、CHA、directory、private invalidation 和 PMU 的事务回滚。单元测试
覆盖无冲突 exact-equivalence 与冲突 fallback。

但是，GoFeed C8 的 preview 在 Q=2048 时每核每 epoch 只有约 50 个访存，worker
barrier 无法摊薄；虽然共享物化事件减少 74.4%，吞吐量反而下降。Q=8192 的中位
吞吐量为 17.02M UOP/s，仍低于默认私有-hit 快路径约 19.7M；Q=32768 又降至
12.18M，并改变预测周期。因此生产配置没有为了“展示创新”强行开启负收益路径。

事务 reweave 同样保留为实验开关。当前 coarse interval-tail feedback 在 C8
GoFeed 上把 CPI 误差从约 +10.9% 恶化到约 +15.3%，同时降低吞吐量。它证明了
rollback/replay 基础设施可执行，但不是可接受的 timing certificate；默认固定
`interval_reweave_passes = 1`，下一步必须先实现 event-level response slack。

## 11. C4–C32 验证矩阵

使用同一 seed0 v28.1 functional corpus 的 23 个 workload，分别验证
C4/C8/C16/C32。每阶段保留旧路径和新路径的同核模型 controlled ablation：

```text
frontier baseline
  → time epoch
  → + fixed buffers / bulk trace
  → + private escape filtering
  → + state certificate
  → + selective replay
  → Q=1024 下完成组件对齐
  → （主 gate 通过后）独立 Q sensitivity
```

必须报告：

- UOP-CPI：mean/median/P90/max absolute error 和 signed bias；
- per-core UOP-CPI MAPE；
- L1D/L2/CHA/branch PMU WAPE；
- upgrade/invalidation/remote-supply 的直接误差；
- UOP/epoch、UOP/active-prefix、event/epoch、escape ratio；
- state/timing certificate failure、replay work、fallback rate；
- UOP/s、MIPS、宿主 instructions/UOP 和 cycles/UOP；
- C8/C4、C16/C8、C32/C16 的扩展效率。

最终门槛：

| 指标 | Gate |
|---|---:|
| C4–C32 UOP-CPI mean | ≤ 6% |
| UOP-CPI P90 | ≤ 10% |
| per-core UOP-CPI MAPE | ≤ 7% |
| aggregate PMU WAPE | ≤ 1% |
| canonical serial equivalence | stress tests 0 mismatch |
| replay work | ≤ 5% |
| C8 首轮性能门槛 | ≥ 23M UOP/s（约当前路径 2×） |
| 论文目标 | ≥ 50M UOP/s，且不牺牲上述精度 |

在同一主机、同一 workload、同一配置上运行真实 ZSim 之前，不声称吞吐量超过
ZSim；ZSim 论文中的 300 MIPS 是不同规模和部署条件，不能直接作为本地对照。

## 12. Stage 1 合成扩展性检查

100K UOP/core、30% memory、5% shared 的合成回归结果：

| 核数 | frontier steps | time-epoch steps | time-epoch UOP/step | time-epoch UOP/active prefix |
|---:|---:|---:|---:|---:|
| 4 | 1,628 | 404 | 990 | 约 560 |
| 8 | 3,132 | 530 | 1,509 | 约 560 |
| 16 | 6,254 | 746 | 2,145 | 约 560 |
| 32 | 12,455 | 1,157 | 2,766 | 约 560 |

这证明 256-UOP 已不再是全局同步边界，但还不证明精度成功。加入 round-robin
lookahead、phase overlap 和同-line 稀疏审计后，该合成测试的 time-epoch 吞吐量
比 frontier 高约 13%–35%。Stage 1 的完整 92-case 结果及失败分析见
[Stage 1 C4–C32 验证报告](time-epoch-stage1-validation.md)。Stage 2 的实现、
正负 ablation、最终 C4–C32 结果和剩余瓶颈见
[Stage 2 C4–C32 验证报告](time-epoch-stage2-validation.md)。在 event-level timing
证书完成前不能扩大默认 Q，也不能声称 canonical equivalence 已成立。
