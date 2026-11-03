# Time-Epoch Stage 2：C4–C32 实施与验证报告

日期：2026-08-01  
配置：`configs/gem5-v28_1-time-epoch.cfg`  
最终结果：`results/tcsim-v28_1-seed0-c4-c32-time-epoch-stage2-final/`

## 结论

Stage 2 已经把方案中的基础机制变成可执行代码：bulk functional-trace reader、
2048-UOP transport microbatch、真实 IQ/ROB/LQ/SQ interval core、私有 cache
preview、escape-event filtering、状态 pre-certificate、共享状态事务回滚，以及
有界 timing reweave。默认路径在完整 23 workload × C4/C8/C16/C32 上通过 UOP、
memory-event 守恒检查，并完成 PMU 对比。

有效的热路径优化显著改善了扩展性，但没有掩盖精度问题。默认配置继续使用
`Q=2048`，并关闭 `interval_private_preview` 和多轮 reweave：二者虽然已经可运行，
当前 ablation 分别受 phase barrier 和 coarse timing feedback 限制，开启后会降低
吞吐量，reweave 还会恶化 CPI。生产配置只保留经过验证不改变模拟语义的优化。

最终 CPI 仍未达到论文门槛。误差不是一个统一 scale：PyTorch 严重低估，C32
random-memory 严重高估，int-div 与 SIMD 又呈方向相反的固定偏差。当前结果应
解释为“interval/事务基础设施和吞吐量阶段完成”，不能解释为已得到精确 O3
或已认证的跨核事件顺序。

## 实施内容

- v3 binary trace 每次批量读取 4096 条记录；v2 兼容路径保持 scalar reader；
- producer chunk 从 256 调整为 2048 UOP，lookahead 从 64 调整为 8，时间 epoch
  仍为 2048 cycles，因此 decode/transport 粒度与同步粒度解耦；
- 第一个 incoming chunk 直接 move 为 resident epoch buffer，减少一次全块复制；
- operation traits 预计算为 128-entry table；
- IQ 容量由真实 issue-release 时刻控制，并以 `issue_slots` 单调 calendar 释放，
  不再用程序序索引或每 UOP 通用二叉堆；
- private L1/L2 access 从 `contains + access` 两次 tag 扫描改为一次 access；
- read/private-hit 和已有 owner 的 write/private-hit 在 directory 之前返回；
- same-line order audit 合并为一次 line-group 构建，避免全事件 Fenwick pass；
- private preview 复用永久 per-core workers；只有 miss、write、atomic、可见 L2
  eviction 进入共享 weave；
- pre-certificate 检查 atomic、已有 sharer、epoch accessor，以及跨核 write 后的
  L1/L2 set 使用，冲突时走 canonical lower-bound fallback；
- LLC、DRAM、CHA ready/counter、directory 和 private invalidation 都支持 undo；
- 多轮 reweave 可按 feedback-corrected order 事务重放，并以 LLC-set resource
  order 稳定性判定；超过上限回退 canonical order；
- 新增 preview、materialization、certificate、replay、fallback 统计和报告列。

## 受控 ablation

同一台主机、同一 C8 GoFeed functional trace 的代表性结果：

| 变更 | 中位吞吐量 | 模拟周期 | 结论 |
|---|---:|---:|---|
| Stage 1 完整集基线 | 约 12.0M UOP/s | 16,060,491 | 优化起点 |
| private/directory/cache 安全快路径 | 14.27M | 16,060,491 | 保留 |
| transport K=1024 | 18.42M | 16,060,491 | 保留 |
| transport K=2048 | 18.84M | 16,060,491 | 保留，优于 K=4096/16384 |
| + sparse same-line audit | 19.45M | 16,060,491 | 保留 |
| + 精确 IQ release | 约 19.7M | 16,066,289 | 保留；修复 IQ 语义 |
| certified preview, Q=2048 | 13.12M | 16,060,491 | 74.4% 事件被过滤，但 barrier 不合算 |
| certified preview, Q=8192 | 17.02M | 16,091,223 | 仍慢于默认路径 |
| certified preview, Q=32768 | 12.18M | 16,320,470 | state fallback 1 次且周期漂移，拒绝 |

IQ release calendar 与通用 heap 的 C8 GoFeed 输出除运行时 `frontier_waits` 外等价，
`sum_core_cycles` 和全部功能/PMU 状态相同。`perf stat` 中宿主 instructions 从
6.17B 降至 5.10B（-17.3%），host cycles 从 2.94B 降至 2.12B（-27.8%）；C8
wall time 因 coordinator 已在关键路径上而基本不变，但该优化释放了高核数 worker
资源。

多轮 reweave 是明确的负结果：在 C8 GoFeed 上，coarse interval-tail feedback
把 UOP-CPI 误差从约 +10.9% 恶化到约 +15.3%，并降低吞吐量。因此实现保留，
默认 `interval_reweave_passes=1`。在加入 event-level load-response slack 前，
不能把“发生了 replay”误写成精度创新。

## C4–C32 CPI 精度

以下是 workload-equal UOP-CPI absolute relative error：

| Cores | mean | median | P90 | max | signed bias | per-core MAPE |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 11.907% | 7.176% | 20.585% | 67.512% | -4.650% | 11.907% |
| 8 | 12.118% | 6.532% | 34.380% | 59.411% | -4.711% | 12.108% |
| 16 | 12.495% | 8.075% | 25.233% | 46.273% | -0.755% | 12.432% |
| 32 | 13.307% | 13.120% | 23.888% | 44.644% | +6.012% | 13.939% |

相对 Stage 1，mean absolute error 的变化为 C4 `+0.093pp`、C8 `+0.103pp`、
C16 `-0.019pp`、C32 `-0.042pp`。这轮没有通过调参换取吞吐量，CPI 变化主要
来自 IQ capacity 改为真实 issue-release 时刻。

## 逐 workload signed CPI error

正数表示 FastSim CPI 高于 gem5，负数表示低于 gem5：

| Workload | C4 | C8 | C16 | C32 |
|---|---:|---:|---:|---:|
| bvc_encoder_base | +2.342% | +2.364% | +4.584% | +9.622% |
| bvc_encoder_heldout | +2.467% | +2.596% | +5.197% | +11.183% |
| cache_L1_mixed | +2.260% | +6.587% | +8.414% | +13.120% |
| cache_L2_mixed | +7.839% | +5.286% | +21.399% | +17.809% |
| coh_readmostly_sparse | -7.236% | -7.277% | -7.230% | -7.159% |
| flink_base | +6.779% | +6.532% | +8.075% | +14.060% |
| flink_heldout | +7.176% | +6.212% | +8.542% | +15.521% |
| fp_alu_dense | -0.390% | -0.413% | -0.436% | -0.474% |
| gofeed_base | +10.335% | +10.954% | +13.951% | +22.958% |
| gofeed_heldout | +7.904% | +6.599% | +8.046% | +13.988% |
| int_alu_dense | -0.109% | -0.120% | -0.125% | -0.137% |
| int_div_serial | +16.116% | +16.103% | +16.075% | +16.078% |
| marine_base | -2.598% | -2.576% | +1.113% | +7.959% |
| marine_heldout | +5.076% | +5.238% | +8.018% | +14.710% |
| memory_random_mlp | -3.103% | +5.700% | +20.121% | +44.644% |
| memory_seq_moderate | -20.585% | -34.380% | -25.233% | -1.133% |
| mysql_base | +7.543% | +5.701% | +6.126% | +10.858% |
| mysql_heldout | +7.621% | +5.304% | +5.346% | +9.661% |
| pytorch_base | -67.512% | -59.411% | -41.579% | -23.888% |
| pytorch_heldout | -59.578% | -57.067% | -46.273% | -27.907% |
| redis_base | -6.333% | -6.622% | -5.089% | -0.136% |
| redis_heldout | -6.156% | -8.855% | -9.592% | -6.217% |
| simd_sse_dense | -16.804% | -16.816% | -16.824% | -16.845% |

## PMU 精度

Count-weighted absolute error（WAPE）：

| Cores | L1D miss | private L2 miss | CHA lookup | branch miss | LLC tag vs functional path |
|---:|---:|---:|---:|---:|---:|
| 4 | 0.043% | 0.239% | 0.239% | 0.165% | 0.767% |
| 8 | 0.048% | 0.266% | 0.265% | 0.170% | 0.769% |
| 16 | 0.052% | 0.283% | 0.283% | 0.160% | 0.760% |
| 32 | 0.059% | 0.290% | 0.290% | 0.153% | 0.702% |

这些计数通过 `<1%` gate，但只覆盖显式建模的 L1D、private L2、CHA lookup、
branch 和 functional LLC-tag 路径，不能外推为 Ruby message class 或 coherence
order 已准确。

## 吞吐量与扩展性

完整 23-workload 集的 simulator-only 中位吞吐量：

| Cores | Stage 1 | Stage 2 | 中位逐 case speedup |
|---:|---:|---:|---:|
| 4 | 17.61M | 21.51M UOP/s | 1.26× |
| 8 | 15.81M | 19.92M UOP/s | 1.27× |
| 16 | 9.89M | 17.92M UOP/s | 1.84× |
| 32 | 5.90M | 14.87M UOP/s | 2.63× |

Stage 2 的 per-case geometric-mean speedup 为 C4 `1.31×`、C8 `1.26×`、C16
`1.95×`、C32 `2.70×`。高核数收益更大，说明主要修复了 producer chunk 和
per-core core-model 开销；C8 仍未达到 23M UOP/s 阶段门槛。

最终 C8 GoFeed profile 的 aggregate-cycle 热点为：`allocate_issue` 14.8%、
worker loop 13.8%、core `schedule` 12.8%、timing feedback 8.4%、epoch append
8.1%、coordinator weave 6.5%、cache access 3.4%、same-line audit 约 3.4%。
下一轮吞吐量优化应优先消除 coordinator 中的全事件 feedback/append，而不是继续
微调宿主线程同步。

## 误差来源

1. **缺少 DTLB/page-walk timing。** 以 C4 单核 trace 诊断，PyTorch base 每核
   约 12.3K 个 functional DTLB miss，当前模型没有 DTLB/page walker；这与
   PyTorch 的 60%–68% CPI 低估同向。physical-page proxy 需要单独验证，不能直接
   用 trace 的 `dtlb_hit` oracle。
2. **load response 仍以 interval tail gap 反馈。** 当前只在 epoch 内沿 producer
   distance 传播，并把 critical tail 作为一个 core-wide gap 传到下个 epoch；它
   不能精确表达独立 miss overlap、ROB-head blocking 和 response slack。
3. **DRAM/MLP 调度过粗。** 顺序内存 C4/C8 明显低估，而 random-memory 从 C4
   `-3.1%` 翻转到 C32 `+44.6%`，说明固定 FCFS bank/channel 与 16-MSHR envelope
   没有复现 gem5 的 page walk、queue overlap 和 Ruby backpressure。
4. **前端/FU aggregate 语义仍有缺口。** int-ALU/FP microbench 已在 0.5% 内，
   但 int-div 固定高估约 16.1%，SIMD 固定低估约 16.8%。本地 gem5 FU 描述和
   issue-to-complete 诊断均不支持简单修改 op latency；需要补 fetch/decode、
   writeback/port 和 producer-ready 的 interval 约束。
5. **coherence order 尚未认证。** 约 23.23% memory event 是 shared escape，
   same-line feedback reorder 仍有千万级；默认路径只审计而不 replay。

## 守恒、测试与 Gate

92 cases 的守恒检查均为 0 mismatch：

- `interval_accepted_uops == retired_uops`；
- `batch_memory_events == memory_accesses`；
- `private + escape == batch_memory_events`。

Release 单测和 ASan/UBSan 单测全部通过。新增用例覆盖 binary bulk refill 边界、
private-preview exact equivalence，以及跨核冲突时 state-certificate fallback。

| Gate | Stage 2 结果 |
|---|---|
| C4–C32 mean UOP-CPI ≤ 6% | **Fail**：11.91%–13.31% |
| P90 ≤ 10% | **Fail**：20.59%–34.38% |
| per-core MAPE ≤ 7% | **Fail**：11.91%–13.94% |
| aggregate PMU WAPE ≤ 1% | Pass（上述已验证范围） |
| canonical serial equivalence | Partial：单元测试通过；全 workload certificate 未启用 |
| replay work ≤ 5% | Not accepted：实验 reweave 默认关闭 |
| C8 ≥ 23M UOP/s | **Fail**：完整集 19.92M UOP/s |

下一实施优先级是：event-level load-response slack 与最小 causal cone、无需
per-epoch barrier 的 chunk-ahead private preview、DTLB/page-walk interval 模型，
最后才是证书驱动的 adaptive Q。
