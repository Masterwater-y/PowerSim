# FastSim FS CPI 根因审查与 DTLB 修复验证（2026-08-17）

## 1. 结论与决策

本轮确认并修复了一个可复现、可由目标源码解释、且影响最大的 CPI 误差源：
**全系统运行仍使用 `se_atomic` DTLB 语义，导致 x86 timing page walker 的排队时间完全没有进入 CPI**。
Neutron 是这一缺陷的极端放大器，不是独立的 workload 特例。

修复不是给 DTLB miss 计数直接乘一个 workload 系数，而是拆成两个互不污染的状态域：

- architectural/committed 域继续给出原有 retired functional PMU 口径；
- timing 域复现目标 x86 walker 的单服务者、非合并 follower 排队，并只影响调度时间。

C4 上选择统一的 12-cycle effective walker service，并冻结到 C8。20 个正式 case
全部使用相同参数，没有 workload ID、case ID 或 per-core residual 表。

接受后的主要结果如下：

| 指标 | 原基线 | 修复后 | 变化 |
|---|---:|---:|---:|
| pooled user mean APE | 14.09% | **8.34%** | -5.75 pp |
| pooled user P90 | 25.19% | **14.24%** | -10.95 pp |
| pooled user max | 46.71% | **23.09%** | -23.62 pp |
| pooled user+kernel mean APE | 14.74% | **9.43%** | -5.31 pp |
| pooled user+kernel P90 | 26.92% | **18.37%** | -8.55 pp |
| pooled user+kernel max | 46.54% | **25.04%** | -21.50 pp |
| Neutron C4 user APE | 42.14% | **0.44%** | -41.71 pp |
| Neutron C8 user APE | 46.71% | **2.48%** | -44.23 pp |

Stockfish 仍未解决：C4/C8 user APE 为 23.09%/20.26%。本轮曾得到一个
`fetch_buffer_refill_latency=2` 的数值候选，能把 Stockfish C8 降至 2.03%，
但目标源码、gem5 stall/request 计数和跨负载回归都否定了它。该候选已撤回，
没有用错误的全局一拍去掩盖另一个组件的欠预测。

## 2. 数据、窗口与比较口径

正式数据根目录：

```text
tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7
```

验证集合是同一组 10 个 workload 的 C4 calibration 和 C8 core-count-held-out，
共 20 个 case。每核测量 10M user FST records，有 source functional warmup，
所有比较复用完全相同的 trace、warmup boundary、reference CPI 和 user-UOP
分母。C8 是核数 held-out，不是 workload held-out。

Oracle identity、FST v7 destination class、虚拟页 token 和 exact denominator gate
沿用 `fs-cpi-repair-validation-2026-08-16.md` 的已通过数据集。本轮只改变推理模型，
没有重采样 gem5，也没有改变参考答案。

最终接受结果：

```text
tmp/fs-cpi-dtlb-tw12-fetch1-final-20260817/summary.json
```

被拒绝的 fetch=2 结果保留作反证：

```text
tmp/fs-cpi-dtlb-tw12-fetch2-20260817/summary.json
```

## 3. DTLB 是主误差源的证据链

### 3.1 跨负载相关性不是单点拟合

在修改模型前，将相同 10M 窗口的 gem5 原始 O3/LSQ/TLB 统计与 FastSim
signed CPI gap（reference - prediction）对齐。相关性结果为：

| 指标 | C4 Pearson | C8 Pearson | C8 Spearman |
|---|---:|---:|---:|
| `loadToUse` mean | 0.769 | **0.875** | 0.830 |
| `loadToUse >= 90 cycles` fraction | **0.866** | **0.976** | 0.903 |
| raw DTLB misses / load | 0.716 | **0.899** | 0.564 |
| I-cache stall CPI | 0.231 | **0.054** | 0.006 |

Neutron C8 的直接观测值是：

- reference/predicted user CPI = 0.952354 / 0.507522，缺 0.444832 CPI；
- gem5 `loadToUse` mean = 66.205 cycles；
- 48.124% 的 load-to-use 样本不小于 90 cycles；
- raw DTLB misses/load = 0.60844；
- retired functional DTLB misses/load 只有 0.06315。

这组数据同时解释了两个此前看似冲突的现象：retired DTLB PMU 已经接近参考，
但 CPI 仍严重偏低。缺的是 outstanding translation follower 的**时间**，不是把
retired miss PMU 再放大 9 倍。

### 3.2 gem5 的 `loadToUse` 包含 translation wait

gem5 的 load-to-use 样本是从 load 第一次 issue 到 `wakeDependents` 的时间。
第一次 issue 发生在 translation 完成之前，因此 DTLB walk 和 walker queue
会进入该统计。它不是纯 L1D hit latency。

这也是为什么 Neutron 的长尾 load-to-use、raw DTLB miss amplification 和 CPI
gap 同向，而 committed cache miss 数量、I-cache stall CPI 不能解释同样的尾部。

### 3.3 目标 x86 walker 明确串行且不合并 follower

目标源码
`/data00/yinhaolang/gem5-fs/src/arch/x86/pagetable_walker.cc:71-93`
明确写出：

- 有 active `currStates` 时，新请求只压入队列；
- TODO 才是未来的 coalescing；当前没有 same-VPN 合并；
- 当前 walk 完成后，下一项在 `clockEdge()` 启动。

`pagetable_walker.cc:118-133` 和 `:193-226` 又确认 completed walker 被移除后，
才启动队首 follower。目标 config 是 x86 long mode 的四级页表，walker port
接 Ruby memory hierarchy。

FastSim 原方案只有两个不完整选项：

| 模式 | PMU | CPI timing | 问题 |
|---|---|---|---|
| `se_atomic` | committed miss 接近参考 | 无 page-walk service | FS CPI 严重欠预测 |
| 旧 `timing_walk` | follower 被重复算成 retired miss | 有固定 walk timing | PMU 被 timing 状态污染 |

正式 FS runner 此前默认选择 `se_atomic`，因此 Neutron 的主时序机制根本没有启用。

### 3.4 单参数 sweep 有单调、跨核结果

保持输入和所有其他参数不变，只改变统一 walker service：

| effective service | Neutron C4 CPI | Neutron C8 CPI |
|---:|---:|---:|
| 1 | — | 0.511888 |
| 2 | — | 0.522358 |
| 4 | — | 0.574566 |
| 8 | 0.796286 | 0.756254 |
| **12** | **1.004252** | **0.976002** |
| 16 | 1.227363 | 1.204782 |
| gem5 reference | 0.999879 | 0.952354 |

12 cycles只在 C4 选定，之后冻结给 C8。C8 没有重新拟合，误差为 2.48%。
这个 sweep 的单调性和 held-out 方向排除了“偶然改动了另一个计数器”的解释。

12 cycles 仍是 effective service，不是完整四级 walk 的物理分解。它必须在后续
由 per-level Ruby response ledger 取代；当前报告不把它描述为最终 page-walker
微架构。

## 4. 实现方案

### 4.1 两个状态域

`IntervalCoreModel::translate()` 现在按以下顺序工作：

1. 对 committed memory record 立即查询/更新 architectural DTLB LRU；
2. `se_atomic` 维持历史行为，保证 SE compatibility；
3. `timing_walk` 另查 timing DTLB；
4. timing miss 分配全局最早可用 walker，按目标默认不 coalesce；
5. walk 完成时只填 timing DTLB；
6. speculative path 只能改变 timing state，不能进入 retired PMU。

两个域的语义和计数如下：

| 域 | 状态 | 计数 | 是否影响 CPI |
|---|---|---|---:|
| committed architectural | `architectural_dtlb_lru_` | `dtlb_*` | 只通过既有 SE hit contract |
| delayed timing | `dtlb_lru_`, walkers, pending walks | `dtlb_timing_*` | 是 |

每个域都必须满足：

```text
accesses = hits + misses + merged_misses + untracked
```

报告同时输出 aggregate/per-core 的 `dtlb_conserved`、
`dtlb_timing_conserved` 和 raw `dtlb_timing_walk_delay_cycles`。raw delay
可以与 backend work 重叠，不能当作可加 CPI breakdown。

### 4.2 修改位置

- `include/fastsim/interval_core.hpp`：独立 LRU/walker 状态和 per-UOP timing flags；
- `include/fastsim/types.hpp`：`TranslationCounters::conserved()`、`dtlb_timing`；
- `src/interval_core.cpp`：双状态 translation 和目标 follower queue；
- `src/simulator.cpp`：两个计数域独立聚合；
- `src/main.cpp`：aggregate/per-core JSON、CLI 参数和兼容 alias；
- `tests/test_main.cpp`：cold/queued/warm/coalescing directed test；
- `tools/run_fst_v7_formal_inference.py`：FS 默认 `timing_walk/12`；
- `tools/run_kernel_event_accuracy_pipeline.py`：相同默认值和 provenance。

基础 config 仍保留 `dtlb.miss_model=se_atomic`，因为该文件也服务历史 SE 合同；
两个 FS runner 明确覆盖为 `timing_walk`。这避免把 FS page walker 偷渡进 SE。

## 5. 不变量和直接验证

### 5.1 同输入 C8 Neutron 控制实验

| 模式 | CPI | cycles | arch hit/miss | timing hit/miss | raw walk delay |
|---|---:|---:|---:|---:|---:|
| `se_atomic` | 0.507522 | 40,601,772 | 13,003,073 / 640,237 | 13,003,073 / 640,237 | 0 |
| `timing_walk/12` | 0.976002 | 78,080,146 | **13,003,073 / 640,237** | 8,832,836 / 4,810,474 | 718,293,909 |

architectural access/hit/miss 三项逐项相等，只有 timing domain 和 CPI 变化。
这证明修复不是靠篡改 retired PMU 达到 CPI 目标。

最终 20 case × 2 scope 审计结果：

```text
all_arch_equal=True all_conserved=True
```

也就是 40/40 report 的 architectural DTLB access/hit/miss 与旧 `se_atomic`
逐项相等，两个域都通过守恒式。

### 5.2 Neutron 的修复量

| Case | baseline pred | fixed pred | reference | 增加的 CPI | final APE |
|---|---:|---:|---:|---:|---:|
| C4 user | 0.578486 | 1.004252 | 0.999879 | +0.425766 | **0.44%** |
| C8 user | 0.507522 | 0.976002 | 0.952354 | +0.468480 | **2.48%** |

C4 原 signed gap 是 0.421393 CPI，C8 是 0.444832 CPI。统一 timing walker
分别恢复 101.0% 和 105.3%，C8 的 5.3% overshoot 是固定 service 模型的残差，
不是隐藏 workload correction。

## 6. 全矩阵结果

### 6.1 分 split 汇总

| Split | Scope | Mean APE | P50 | P90 | P99 | WAPE | Bias | Max |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| C4 calibration | user | **8.46%** | 6.94% | 14.52% | 22.23% | 6.92% | -5.02% | 23.09% |
| C4 calibration | user+kernel | **9.29%** | 8.89% | 14.51% | 23.99% | 7.10% | -3.68% | 25.04% |
| C8 held-out | user | **8.21%** | 6.06% | 13.33% | 19.57% | 5.59% | -4.39% | 20.26% |
| C8 held-out | user+kernel | **9.56%** | 11.15% | 18.37% | 21.53% | 5.53% | -4.58% | 21.88% |

原基线 C4/C8 user mean APE 是 13.54%/14.63%，user+kernel 是
14.06%/15.41%。Bias 从约 -10% 到 -13% 收敛到约 -3.7% 到 -5.0%，
说明主要改善不是少数 case 的无符号平均幻觉。

### 6.2 每个 workload 的 APE（原基线 → 接受修复）

| Workload | C4 user | C8 user | C4 user+kernel | C8 user+kernel |
|---|---:|---:|---:|---:|
| Stockfish | 23.30 → **23.09** | 20.36 → **20.26** | 25.25 → **25.04** | 22.00 → **21.88** |
| omnetpp | 14.62 → **10.42** | 14.44 → **10.99** | 15.15 → **11.01** | 15.13 → **11.74** |
| zstd | 14.01 → **3.64** | 13.43 → **4.81** | 6.56 → **3.42** | 8.46 → **1.52** |
| LBM | 7.17 → **7.03** | 2.76 → **2.73** | 5.63 → **5.50** | 1.86 → **1.79** |
| SPH | 4.83 → **4.71** | 7.04 → **6.84** | 11.17 → **11.00** | 11.42 → **11.25** |
| TeaLeaf | 6.23 → **6.08** | 12.97 → **12.56** | 5.40 → **5.36** | 12.57 → **12.43** |
| NAb | 8.76 → **8.76** | 12.46 → **12.45** | 7.41 → **7.40** | 11.05 → **11.04** |
| Graph500 | 0.44 → **6.84** | 12.27 → **5.28** | 8.46 → **10.37** | 6.95 → **3.28** |
| NAMD | 13.91 → **13.57** | 3.89 → **3.75** | 13.58 → **13.34** | 18.14 → **17.98** |
| Neutron | 42.14 → **0.44** | 46.71 → **2.48** | 41.97 → **0.47** | 46.54 → **2.70** |

C4 Graph500 是明确回归：user 仍低于 7%，但 combined 从 8.46% 增至
10.37%。这与“12 cycles 是粗粒度 effective service”一致，必须保留在后续
page-walk memory-response 细化的 gate 中。

## 7. Stockfish 与 fetch-buffer 候选为什么被拒绝

### 7.1 源码事实

目标 `BaseO3CPU.fetchBufferSize=64`。gem5 在旧 block 的 fetch tick 末尾发起
pipelined I-fetch。MESI Three Level 的 resident L0-I hit 在
`MESI_Three_Level-L0cache.sm:1072-1077` 直接 `readCallback`；
`request_latency=2` 只出现在 miss 的 GETS enqueue，不是 resident-hit latency。

正式 C8 Stockfish 的 gem5 计数：

- `fetch.cacheLines` = 5,395,256；
- `icacheStallCycles` = 5,627,835；
- 比值 = 1.043 stall cycles/request；
- committed FST 64B block transitions = 4,830,519。

因此源码和计数支持一个 intervening empty cycle，不支持每个 resident block
统一两个 empty cycles。

### 7.2 三点消融暴露数值抵消

同一 C8 Stockfish 输入、同一 DTLB timing：

| refill latency | FastSim CPI | 相对 gem5 0.296816 的 APE | raw refill cycles |
|---:|---:|---:|---:|
| 0 | 0.197084 | 33.60% | 0 |
| **1（源码一致）** | **0.236684** | **20.26%** | 4,830,519 |
| 2 | 0.290799 | 2.03% | 9,661,038 |

`2` 看似很好，但 raw refill 已是 0.12076 cycles/UOP，高于 gem5 全部
I-cache stall 的 0.07035 cycles/UOP。它在 FastSim 中暴露出的增量约
0.05411 CPI，实际是在补偿别处尚未建模的欠预测。

跨负载反证更直接：若将 2 全局推广，NAb C4 从 8.76% 变成 16.64%，
Graph500 C4 从 0.44% 变成 12.76%，NAMD C8 从 3.89% 变成 14.11%。
因此它不是可接受的 target parameter，即使 pooled mean 会进一步下降。

最终保留的改动只有：

- `fetch_buffer_transitions` 和 raw refill-delay audit counters；
- `--fetch-buffer-refill-latency` 显式 CLI，便于消融；
- 全局默认仍为源码支持的 1。

### 7.3 已排除与尚未闭合的部分

Stockfish C8 accepted gap 是 0.060132 CPI。以下候选已被直接排除：

| 候选 | 证据 | 决策 |
|---|---|---|
| committed L1I capacity/miss state | cycles 只增加 74,809，gap 约 4.81M cycles | 影响不足 2% |
| ITLB | 同窗口计数和 CPI 影响可忽略 | 排除主因 |
| execute/IEW/commit backward scalar delay | 单边消融无有效变化或跨负载恶化 | 不推广 |
| finite rename free list | 正式所有 core 为 0 stall cycles | 排除 committed rename 容量 |
| wrong-path active-window 直接加法 | Neutron/NAb exact pilot 只能覆盖 gap 的 26%–47%，且 per-core 方向为负 | 不可加 |
| 每 block 再加 1 cycle | 源码/计数冲突且产生三项大回归 | 拒绝 |

目标 C8 比 committed transitions 多 564,737 次 I-fetch request，说明
wrong-path/refetch/kernel request 确实缺失，但即使每次按一个 resident-hit cycle
计算也只有 0.00706 raw CPI，不能单独解释 0.06013 CPI。当前证据只能把剩余
问题定位为 **committed-only frontend request stream 与 backend/window overlap
闭合不足**，还不能有证据地在其中分配具体拍数。

下一步必须采集 request-side 状态 sidecar 或逐 request stage ledger，再分别验证
request issue、L0 response、fetch resume、queue drain 的 non-overlapped exposure。
在该证据出现前，不应添加 Stockfish 特征系数、transition-rate 系数或 workload
residual。

## 8. 其他被排除的主因

| 假设 | 直接证据 | 结论 |
|---|---|---|
| Neutron 是 LLC/DRAM miss 数量问题 | Ruby/DRAM 与 committed miss 数量接近，额外 miss 最多解释约十分之一旧 gap | 非主因 |
| Neutron 是 wrong-path 周期本身 | exact 100k C8 ceiling 仅覆盖 38.34%，加满后仍 26.38% APE | 非主因 |
| Neutron 是 committed rename/IQ metadata 缺失 | destination class 100% 覆盖，finite free-list 0 stalls | 排除 rename 容量 |
| 所有负载都是 I-cache stall | C8 I-cache-stall-CPI 与 signed gap Pearson 仅 0.054 | 排除全局解释 |
| DTLB 只影响 PMU、不影响 CPI | timing latency sweep 单调恢复 0.43–0.47 CPI，arch PMU bit-exact 不变 | 反证成立 |

## 9. 性能与回归验证

最终代码执行：

```bash
cmake --build build -- -j16
./build/fastsim_tests
python3 -m py_compile \
  tools/run_fst_v7_formal_inference.py \
  tools/run_kernel_event_accuracy_pipeline.py
```

directed tests 覆盖：cold miss、同页 follower 排队、完成后 warm hit、可选
coalescing 只改变 timing domain，以及 fetch transition raw counter。

最低吞吐 case 采用独占单进程重新测量 C4 LBM：

| 指标 | 值 |
|---|---:|
| CPI | 2.681440 |
| measurement throughput | 5.081 M user UOP/s |
| end-to-end throughput | 5.081 M user UOP/s |
| overall throughput | 5.054 M UOP/s |

该结果通过当前 5 M UOP/s gate，但余量很小；共享宿主上的四并发正式矩阵
吞吐不作为独占性能结论。

## 10. 后续修复顺序

1. 用 per-level page-walk Ruby request/response ledger 取代固定 12-cycle service，
   首先消除 C4 Graph500 的 overcharge，同时保持 Neutron C4/C8。
2. 为 I-side 增加 state-only request/refetch sidecar；它只能改变 frontend/cache
   状态和 timing，不能进入 retired user PMU。
3. 对 Stockfish 建立 fetch request → L0 response → fetch resume → queue/ROB exposure
   的守恒 ledger；没有 ledger 前不再做 refill scalar sweep。
4. 对仍超过 12% 的 Stockfish、TeaLeaf C8、NAb C8、NAMD combined 建立 committed
   dependency/memory-response residual ledger，禁止 workload-ID correction。
5. 所有下一候选继续同时报告 user 与 user+kernel；NAMD C8 已证明 combined CPI
   可能通过 kernel contribution 掩盖 user error，不能只看一个 scope。

## 11. 复现命令

```bash
python3 tools/run_fst_v7_formal_inference.py \
  --dataset tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7 \
  --output tmp/fs-cpi-dtlb-tw12-fetch1-final-20260817 \
  --fastsim build/fastsim \
  --user-config \
    tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/accuracy/calibration-c4/user-cache-state.cfg \
  --kernel-config \
    tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/accuracy/calibration-c4/kernel-events-cache-state.cfg \
  --jobs 4 --force
```

runner 的最终 provenance 应为：

```text
dtlb_miss_model=timing_walk
dtlb_page_walk_latency=12
fetch_buffer_refill_latency=1
```
