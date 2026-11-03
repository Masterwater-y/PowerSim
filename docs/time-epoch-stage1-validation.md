# Time-Epoch Stage 1：C4–C32 验证报告

日期：2026-08-01  
配置：`configs/gem5-v28_1-time-epoch.cfg`  
结果：`results/tcsim-v28_1-seed0-c4-c32-time-epoch-stage1/`

## 结论

Stage 1 完成了真正的时间 epoch 和跨 256-UOP 微批 lookahead，并在完整的
23-workload × C4/C8/C16/C32（92 cases）上无崩溃完成。它显著减少全局同步、
提高 C4/C8 吞吐量，并实测确认约 76.77% memory event 只停留在私有层级。

但它**没有通过精度和因果认证门槛**：CPI 平均误差仍为 11.81%–13.35%，
约 88% active core prefixes 的反馈后 retire time 越过原 epoch horizon。当前实现
仍按 lower-bound issue order 更新共享队列，尚无 rollback/reweave，因此不能声称
跨核时序或 coherence order 已认证。

## 实现内容

- 新增 `sim.interval_scheduler = time_epoch`，保留旧 `frontier` 对照路径；
- `sim.chunk_instructions = 256` 只作为 decode microbatch；
- 使用公共 `[T,T+2048]` cycle epoch；
- 每核跨微批补足 lookahead，直到最后已解码 dispatch 晚于 horizon；
- 同时接受 retire prefix 与 `issue <= horizon` 的 in-flight memory prefix；
- 每轮每核最多拉取一个微批，避免串行 drain 单核 producer；
- 64-chunk producer lookahead，使 epoch N weave 与 epoch N+1 bound 重叠；
- 只对同 cache line 冲突组做反馈后顺序审计；跨 line 全序审计可配置开启；
- 输出 accepted UOP、active prefix、in-flight memory、horizon violation、
  private/escape event 和 same-line conflict 统计。

## 完整精度结果

| Cores | UOP-CPI mean | median | P90 | max | signed bias | per-core MAPE |
|---:|---:|---:|---:|---:|---:|---:|
| 4 | 11.814% | 7.176% | 20.585% | 65.569% | -4.549% | 11.814% |
| 8 | 12.015% | 6.532% | 34.380% | 59.668% | -4.729% | 12.005% |
| 16 | 12.514% | 8.429% | 25.233% | 45.774% | -0.802% | 12.451% |
| 32 | 13.349% | 13.477% | 25.338% | 45.301% | +5.922% | 13.974% |

最差项：

- C4/C8：`W_v28_pytorch_base`，分别低估 65.57%/59.67%；
- C16：`W_v28_pytorch_heldout`，低估 45.77%；
- C32：`W_v28_memory_random_mlp`，高估 45.30%。

这说明误差不是一个可用全局 scale 修正的问题：低核数的 PyTorch 严重低估，
而 C32 random-memory 严重高估。下一阶段必须修复 causal timing 和共享队列
重排，不能靠校准常数。

与同一核心模型的旧 `interval_weave/frontier` 完整 C4/C8 基线相比：

| Cores | frontier mean | time-epoch mean | frontier P90 | time-epoch P90 |
|---:|---:|---:|---:|---:|
| 4 | 11.853% | 11.814% | 19.098% | 20.585% |
| 8 | 14.990% | 12.015% | 33.666% | 34.380% |

因此 C8 mean 有 2.98 个百分点改善，但 tail error 没有改善；C4 基本持平。

## PMU 结果

下表为 count-weighted absolute error（WAPE）：

| Cores | L1D miss | private L2 miss | CHA lookup | branch miss | LLC tag vs functional path |
|---:|---:|---:|---:|---:|---:|
| 4 | 0.043% | 0.239% | 0.239% | 0.165% | 0.767% |
| 8 | 0.048% | 0.266% | 0.265% | 0.170% | 0.769% |
| 16 | 0.052% | 0.283% | 0.283% | 0.160% | 0.760% |
| 32 | 0.059% | 0.290% | 0.290% | 0.153% | 0.702% |

Aggregate PMU 的 `<1%` gate 通过，但这只证明总数准确；same-line 顺序冲突仍然
大量存在，不能据此推导 Ruby protocol 或 coherence message 顺序准确。

## 粒度、吞吐量与扩展性

| Cores | epochs | UOP/epoch | UOP/active prefix | max UOP/epoch | median UOP/s |
|---:|---:|---:|---:|---:|---:|
| 4 | 15,967 | 4,787 | 2,293 | 57,344 | 17.61M |
| 8 | 16,328 | 9,362 | 2,268 | 114,688 | 15.81M |
| 16 | 17,139 | 17,838 | 2,228 | 229,376 | 9.89M |
| 32 | 18,561 | 32,942 | 2,222 | 458,752 | 5.90M |

旧 frontier 的 C4/C8 为 276,481/557,462 steps、15.61M/11.53M UOP/s。
新路径把 step 数减少 94.2%/97.1%，中位吞吐量提高约 12.8%/37.1%。这证明
256-UOP 不再是全局同步边界。但 C8 仍未达到 23M UOP/s 的阶段性能 gate，且
C16/C32 吞吐量随逐事件 shared replay 明显下降。

profile 的受控对比显示 C8 新旧路径每次均执行约 5.5B host instructions；
round-robin lookahead 和 64-chunk overlap 把 time-epoch 的 CPU 利用率从约 2.08
提高到 3.39，并消除了初版的 phase serialization。剩余热点必须从 event 数量和
shared replay 本身消除。

## Escape-event 实测

| Cores | all memory events | private-only | shared escape | escape fraction |
|---:|---:|---:|---:|---:|
| 4 | 3,546,652 | 2,722,709 | 823,943 | 23.232% |
| 8 | 7,093,255 | 5,445,723 | 1,647,532 | 23.227% |
| 16 | 14,186,688 | 10,891,732 | 3,294,956 | 23.226% |
| 32 | 28,373,195 | 21,782,984 | 6,590,211 | 23.227% |

四种核数的 escape fraction 稳定在 23.23%。如果 Stage 3 能让 workers 并行
preview 私有 cache，并通过 version certificate 保证路径有效，则全局 weave 的
输入可减少约 76.77%。当前实现只是测量了该上限，尚未跳过这些事件。

## 失败证据与正确性检查

| Cores | active prefixes | horizon violations | violation rate | in-flight memory / all memory | same-line reordered pairs |
|---:|---:|---:|---:|---:|---:|
| 4 | 33,330 | 29,414 | 88.25% | 1.36% | 2,393,340 |
| 8 | 67,412 | 59,017 | 87.55% | 1.40% | 4,813,224 |
| 16 | 137,223 | 121,183 | 88.31% | 1.42% | 9,675,647 |
| 32 | 275,156 | 241,969 | 87.94% | 1.42% | 19,359,408 |

`horizon violation` 表示 memory feedback 使已接受前缀的预测 retire time 越过
原 horizon；它不是崩溃，但在没有 timing certificate/replay 时是明确的未认证
epoch。高达约 88% 的比例说明不能先扩大 Q 再忽略反馈重排。

92 cases 的守恒检查：

- `interval_accepted_uops == retired_uops`：0 mismatch；
- `batch_memory_events == memory_accesses`：0 mismatch；
- `private + escape == batch_memory_events`：0 mismatch；
- ASan/UBSan 单元回归：通过；LeakSanitizer 因当前 ptrace 环境不可用而关闭。

全局时间可以在没有 UOP retire 时前进，因此 92 cases 共记录 3,539 个
zero-UOP epochs；这不是死锁或丢 UOP。

## Gate 判定

| Gate | 结果 |
|---|---|
| C4–C32 mean UOP-CPI ≤ 6% | **Fail**：11.81%–13.35% |
| P90 ≤ 10% | **Fail**：20.58%–34.38% |
| per-core MAPE ≤ 7% | **Fail**：11.81%–13.97% |
| aggregate PMU WAPE ≤ 1% | Pass（已验证计数范围） |
| canonical serial equivalence 0 mismatch | Not implemented |
| replay work ≤ 5% | Not implemented |
| C8 ≥ 23M UOP/s | **Fail**：15.81M UOP/s |

## 下一阶段优先级

1. 将 private L1/L2 preview 移到 per-core workers，只输出 escape event 和私有
   path/version 摘要；目标是从 coordinator 移除实测 76.77% 事件。
2. 为 private line、directory owner/sharer 和 LLC set/victim 加 transaction
   version；先实现 state certificate 和 canonical epoch rollback。
3. 用 feedback-corrected issue time 对冲突分量 reweave，消除当前约 88% 的
   horizon failure；再加入 causal-slack 以避免无必要重放。
4. 在证书正确后再做 adaptive Q。当前不能通过增大 Q 追求表面吞吐量。
5. 实现固定 ring/arena 和 bulk binary trace read，降低 5.5B host instructions
   的核心/trace 开销。
