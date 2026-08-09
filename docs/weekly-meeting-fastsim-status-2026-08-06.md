# FastSim 周会汇报：设计、关键指标与下一阶段计划

> 数据冻结：2026-08-06\
> 汇报口径：当前代码、配置和已完成的正式报告；未完成或被否决的实验不计入当前最优结果。\
> 核心约束：FastSim 在线输入只有 committed functional trace 和目标微架构配置；gem5 timing/PMU 只作为离线标签、验收和因果消融，绝不作为在线 oracle。

## 0. 一页结论

1. **FastSim 的核心价值已经成立**：同一份 functional trace 可以在不同核数和微架构配置上重复推演，显式重建分支、TLB、缓存、目录/CHA、LLC、DRAM 和 OoO timing，适合高吞吐 PMU 重放和 DSE。
2. **SE 常规 92-case 的典型精度较好，但当前默认基线还没有整体通过 production gate**：C4/C8/C16/C32 的 CPI mean 分别为 4.495%/3.533%/2.555%/2.394%；P99 为 9.169%/11.890%/11.575%/9.390%。C4 通过，C8 CPI 尾差失败，C16 同时有 CPI 和吞吐失败，C32 吞吐失败。
3. **SE 的 functional PMU 已经很准**：L1D miss WAPE 为 0.043%--0.059%，private-L2/CHA 为 0.239%--0.290%，branch miss 为 0.153%--0.170%。DTLB access 和 O3 IQ-full 仍只能作诊断。
4. **微架构 DSE 已有“有限域内有效”的直接证据**：在真正跨越 ROB/IQ/DTLB/L1D/L2 瓶颈阈值的 48-case 套件上，CPI P99 为 8.439%，speedup error P90 为 2.771%，有效方向准确率 90.0%，material pairwise 排名准确率 **93.75%（15/16）**，最低吞吐 9.641M UOP/s，整体 PASS。
5. **不能把上述结果外推为所有业务形状都已泛化**：更难的业务激励 48-case 中，CPI P99 仍为 46.364%，有效方向 77.778%，排名 87.097%。之前提到的约 44% 业务形状误差仍然存在，当前只是被明确定位到高 MLP fanout/dense 和 response lifetime/ordered-retire 闭合，而不是已经修复。
6. **FS 目前是 diagnostic failure，不是 production 结果**：two-phase functional warmup 后，6-case CPI mean/P99/max 误差为 23.590%/89.658%/93.653%，最低吞吐 3.764M UOP/s。`lbm` 的 +93.653% 是主尾差；其 DRAM read/write 数量误差已经只有 -0.31%/-1.88%，说明主因不是请求数或一组统一 DRAM latency，而是读写队列/service order、MLP 暴露和 response→SQ/ROB→ordered-retire 的闭合。

当前阶段最准确的表述是：**FastSim 已经证明了 functional PMU 重放和部分微架构相对排序能力；SE 常规负载接近可验收，但 memory tail、业务高 MLP 形状和 FS 绝对 CPI 尚未闭合。**

| 验证域 | CPI 关键结果 | PMU 关键结果 | 最低吞吐 | 排名/状态 |
|---|---|---|---:|---|
| SE 常规 92-case | 各核 mean 2.394%--4.495%；P99 9.169%--11.890% | L1D/L2/CHA/branch WAPE 约 0.04%--0.29% | 4.094M UOP/s | 仅 C4 整体 PASS |
| SE 机制阈值 48-case | mean/P99 2.262%/8.439% | L1D/L2/CHA 0.006%/0.123%/0.123% | 9.641M UOP/s | 排名 93.75%，PASS |
| SE hard business 48-case | mean/P99 14.370%/46.364% | strict PMU 全部 PASS | 5.262M UOP/s | 排名 87.097%，FAIL |
| FS C8 6-case | mean/P99/max 23.590%/89.658%/93.653% | DRAM R/W 0.533%/1.955%，其他 PMU 仍有明显缺口 | 3.764M UOP/s | 尚无 FS 跨微架构排名，FAIL |

## 1. 设计动机、创新点与总体框架

### 1.1 为什么要做 FastSim

gem5 O3+Ruby 能提供详细时序和微架构事件，但完整 DSE 需要对每个 workload、核数和微架构组合重新运行，时间和资源成本很高。另一方面，简单的 trace penalty 或 workload-specific 拟合虽然快，但容易出现三个问题：

- 缓存、分支、DRAM 和 OoO stall 之间缺少因果闭合；
- 参数变化只改变一个 scalar，不能正确响应 ROB、IQ、容量或带宽阈值；
- 如果把 gem5 hit/miss、service order 或 timing label 放进在线输入，就失去对新微架构的预测意义。

FastSim 的目标是在两者之间建立一个可审计的中间点：

- functional trace 只采一次，同一功能流可重放多个微架构；
- 用显式状态机重建 PMU 和 memory path，而不是直接预测事件数；
- 用 time-epoch、依赖、容量和 response feedback 估计 CPI；
- 保持确定性和足够高的吞吐，使大规模 DSE 可执行；
- 每个新增机制都必须同时通过 CPI、PMU、吞吐和守恒 gate。

### 1.2 总体框架

```text
                         离线标签路径（只用于评估）
                  gem5 O3/Ruby/DRAM CPI + PMU
                                │
                                ▼
                         误差、消融与 Gate
                                ▲
                                │
functional trace ──► FST v6 ──► 每线程/每核并行前端
  committed only                 │  decode、OpClass、依赖、branch replay
  PC/分支/物理地址               ▼
  destination classes       OoO lower-bound / time epoch
                                │
目标微架构配置 ──────────────────┤
 fetch/issue/ROB/IQ/LSQ          ▼
 cache/TLB/CHA/DRAM       deterministic causal frontier
                                │
                                ▼
                  L1D/L2 → directory/CHA → LLC → DRAM
                                │
                                ▼
                sparse response feedback → dependency/IQ/LSQ/ROB
                                │
                                ▼
                    ordered retirement → CPI / PMU / 吞吐
```

软件线程状态与硬件核状态分离：trace 归属于软件线程，predictor、TLB、private cache 和 OoO 状态归属于硬件核。当前使用静态绑定；每个 active trace 有永久 producer，global coordinator 按 FastSim 自己的 modeled time 确定 shared-memory event 顺序。

FS 使用 two-phase functional warmup：先重放 WORKBEGIN 前缀以预热 FastSim 自身 predictor/TLB/cache/OoO/memory 状态，所有核到达统一宏指令边界后，只清零测量计数和测量时间，再进入 ROI。它不读取 gem5 warm-cache 或 service-order 标签。

### 1.3 当前可汇报的创新点/差异化价值

1. **严格的 functional-only 输入合同**\
   FST v6 保留功能语义、OpClass、物理地址、依赖和 destination register class，但不包含 fetch/issue/commit tick、cache hit/path、DRAM service order、workload ID。需要 v6 字段的模型 fail-closed，不能静默使用旧 trace。

2. **确定性的多核 causal replay**\
   per-core producer 并行解析，causal-frontier coordinator 统一提交共享状态；相同输入和配置得到确定结果。并行 lookahead 只影响 host 性能，不应改变 target PMU 或事件顺序。

3. **显式状态机 PMU，而非黑盒计数回归**\
   分支预测器、DTLB、L1D/private-L2、directory/CHA、LLC 和 DRAM 由目标参数和 functional event 驱动，因此同一 trace 能响应容量、bank、channel 和队列参数变化。

4. **介于 penalty model 与 cycle-accurate simulation 之间的稀疏时序闭合**\
   FastSim 保留 producer distance、FU/issue、ROB/IQ/LQ/SQ、memory response 和 ordered retirement 的关键边，只对 response causal cone 做反馈，避免逐 cycle 模拟所有状态。

5. **把“是否真的激励微架构参数”纳入泛化验收**\
   只有 gem5 中 CPI 或对应 PMU 相对 baseline 发生 material change 的 pair 才进入正式方向/排名分母；每个参数族还必须有足够的有效 case。这样不会把“参数改了但 workload 没感觉”误报为泛化成功。

6. **SE/FS 共用模型、分开验收**\
   SE 用于受控机制和跨核数回归，FS 增加 OS/runtime 前缀和共同 ROI 边界。两者共享 source-derived 微架构参数，但分别报告，FS 不从 SE 生成 pseudo-label。

### 1.4 明确边界

- committed functional trace 不包含 wrong-path UOP、地址、OpClass 和 speculative predictor update，因此 wrong-path occupancy 不能被精确恢复；
- 当前没有完整 I-cache/ITLB、page-table memory request、interrupt、调度/迁移和 OS noise；
- directory/MESI 和 DRAM 是可配置近似，不是完整 Ruby/DDR4 command state machine；
- causal frontier 只保证相对 FastSim 自身 modeled time 的确定顺序，不保证在 core timing 尚不准确时与 gem5 顺序完全相同。

## 2. 当前关键指标

本文中的 SE 指 gem5 syscall-emulation/受控 ROI 验证，FS 指 gem5 full-system workload 和共同 ROI 边界验证。两类结果分别统计，不能交叉替代。

### 2.1 统计口径

- CPI error：`|FastSim CPI - gem5 CPI| / gem5 CPI`；mean/median/P99 按 workload 等权。
- signed bias：正值表示 FastSim CPI 偏高，负值表示偏低。
- PMU WAPE：`sum(|FastSim count - gem5 count|) / sum(gem5 count)`；比低计数 workload 等权 MAPE 更能反映总事件量误差。
- throughput：FastSim release/native 路径的 simulator-only wall-time UOP/s，不包含 gem5 采集时间。
- DSE 排名：同 workload、同核数内比较两套微架构；只有 gem5 CPI 差异至少 0.5% 的 pair 进入正式分母。

### 2.2 SE：常规 92-case、C4--C32

数据集为 23 workloads × C4/C8/C16/C32，共 92/92 完成；UOP、memory event、private/escape partition 和 response-critical 守恒失败均为 0。

| Cores | CPI mean / median / P99 / max | signed bias | min / median UOP/s | Gate |
|---:|---:|---:|---:|:---:|
| 4 | 4.495% / 4.618% / 9.169% / 9.189% | +3.564% | 5.758M / 9.023M | PASS |
| 8 | 3.533% / 3.288% / 11.890% / 13.438% | +1.734% | 6.204M / 11.109M | FAIL CPI |
| 16 | 2.555% / 1.824% / 11.575% / 12.792% | +0.146% | 4.094M / 9.632M | FAIL CPI + throughput |
| 32 | 2.394% / 0.845% / 9.390% / 9.463% | -0.823% | 4.159M / 9.889M | FAIL throughput |

当前最大 residual：

- C4 `mysql_heldout`：+9.189%；
- C8 `memory_seq_moderate`：-13.438%；
- C16 `memory_seq_moderate`：-12.792%；
- C32 `memory_random_mlp`：-9.463%；同核数 `memory_seq_moderate` 为反方向的 +9.130%。

常规 92-case 中，除 memory tail 外没有 business/compute workload 超过 10%。C4/C8/C16/C32 的 business heldout mean 分别为 6.346%/3.975%/1.983%/1.499%。这与后面的“业务激励 44% 尾差”是两个不同套件，不能混为一谈。

#### SE PMU

| Cores | L1D miss | private-L2 / CHA | branch miss | DTLB access | DTLB miss | O3 IQ full | LLC functional path |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.043% | 0.239% / 0.239% | 0.165% | 8.770% | 0.027% | 45.474% | 0.929% |
| 8 | 0.048% | 0.266% / 0.266% | 0.170% | 8.799% | 0.033% | 45.721% | 1.044% |
| 16 | 0.052% | 0.283% / 0.283% | 0.160% | 8.805% | 0.034% | 46.068% | 1.080% |
| 32 | 0.059% | 0.290% / 0.290% | 0.153% | 8.843% | 0.034% | 45.618% | 1.128% |

结论：cache/CHA/branch 和 DTLB miss 数量已经闭合；DTLB access 因 gem5 会统计 functional trace 不可见的 wrong-path translation 而低估约 8.8%；O3 IQ-full 的统计语义和 response occupancy 尚未闭合，约 46% WAPE，只能作为定位指标。

### 2.3 SE：微架构泛化和 DSE 有效性

#### 当前通过 gate 的机制阈值套件

当前 fill-response=8 的默认路径在 48-case 阈值套件上结果为：

| 指标 | 当前结果 | Gate |
|---|---:|---:|
| Variant CPI mean / P90 / P99 | 2.262% / 5.209% / 8.439% | P99 ≤ 10% |
| Uarch speedup error P90 / P99 / max | 2.771% / 4.348% / 4.793% | P90 ≤ 10% |
| 有效变化方向准确率 | 90.000% | ≥ 90% |
| material pairwise CPI 排名 | **93.750%（15/16）** | ≥ 90% |
| raw pairwise 排名 | 79.412%（27/34） | diagnostic |
| 未充分激励的已测 uarch | 0 | 每个 uarch 至少 2 个有效 case |
| 最低 FastSim 吞吐 | 9.641M UOP/s | ≥ 5M |
| L1D/private-L2/CHA WAPE | 0.006% / 0.123% / 0.123% | ≤ 2% |
| 总体 | **PASS** | — |

这里的 93.75% 是当前最直接的 DSE 证据：对 gem5 中确实产生至少 0.5% CPI 差异的微架构 pair，FastSim 在 15/16 对比较中给出相同的性能排序；同时 speedup error P90 只有 2.771%。因此 FastSim 已能用于**经过验收的参数族和 material design difference 的候选筛选/排序**。

#### 是否真的改变了 workload 特征

不是只改配置名。当前采集已观察到以下真实变化：

| 参数族 | 调整范围 | gem5 中观察到的有效变化 | 当前证据等级 |
|---|---|---|---|
| ROB | 96 / 192 / 256 | ROB96 在业务 case 中造成 -11.0%/-26.4% speedup；ROB256 在 dense 中约 +5.6% | 已跨瓶颈阈值 |
| IQ | 32 / 64 / 96 | dense 中 IQ32/96 相对变化约 +4.8%/-1.8% | 已激励，但 dense 排名仍有错误 |
| DTLB | 32 / 64 / 128 entries | 80-page graph miss 为 628,897 / 210,088 / 340 | 容量 PMU 已强激励；timing page walk 仍是 proxy |
| L1D | 16KiB/4-way、32KiB、64KiB/8-way | 24KiB index 在 L1D16K 下 miss 增加 63 倍；48KiB index 在 L1D64K 下 miss 减少 96.9% | 已跨容量阈值 |
| private L2 | 512KiB / 1MiB / 2MiB | miss 最大相对变化约 528%/70% 量级 | 已跨容量阈值 |
| DRAM channel | 4 / 8 | 三个 shared workload 在 4ch 下慢约 3.0%--4.1%，方向 3/3 正确 | 有相对方向证据，controller 语义仍需修复 |
| core width、LLC 容量/bank | width4；LLC 32/64/128MiB；4/8 bank | 当前业务激励套件部分 case 仍未产生足够 material change | 不能宣称完整泛化 |

原 12 workload × 16 uarch 的 192-case 集合中，有效排序为 90.72%（303/334），speedup error P90 为 0.887%；但该集合对部分参数欠激励，绝对 CPI P99 为 15.672%，所以只作为辅助证据。

#### 业务形状套件的约 44% 误差

该问题**仍然存在**。当前 hard business-excitation 48-case 结果为：

| 指标 | 当前结果 | Gate |
|---|---:|---:|
| Variant CPI mean / P90 / P99 | 14.370% / 42.454% / **46.364%** | FAIL |
| speedup error P90 | 3.242% | PASS |
| 有效方向准确率 | 77.778% | FAIL |
| material pairwise 排名 | 87.097%（27/31） | FAIL |
| 最低吞吐 | 5.262M UOP/s | PASS |
| strict L1D/L2/branch/CHA WAPE | 0.206% / 0.092% / 0.358% / 0.092% | PASS |

这说明功能事件数和相对 speedup 已有相当基础，但高 MLP `gofeed_fanout_wide`、dense ROB/IQ 形状的 absolute response lifetime 仍未正确穿过 dependency、IQ/LSQ、ROB head 和 ordered retirement。它不是常规 92-case 的 business 回归爆炸，而是专门为触发极端微架构瓶颈而构造的 hard generalization suite。

因此周会中应表述为：**DSE 已在机制阈值套件中证明有效，但对高 MLP 业务形状的绝对 CPI 和全参数泛化仍未完成。**

### 2.4 FS：two-phase functional warmup 6-case

48/48 输入均为 canonical FST v6，UOP count 最大误差 0.001%，所有核在共同边界清零测量计数。主 CPI 按 gem5 FS `numCycles` 的共同 makespan 口径计算。

| Workload | gem5 UOP CPI | FastSim UOP CPI | CPI error | M UOP/s |
|---|---:|---:|---:|---:|
| 706.stockfish_r | 0.228176 | 0.227798 | -0.166% | 7.478 |
| 710.omnetpp_r | 0.378947 | 0.327476 | -13.583% | 7.532 |
| 777.zstd_r | 0.530796 | 0.467742 | -11.879% | 3.987 |
| 782.lbm_r | 2.572910 | 4.982523 | **+93.653%** | 3.764 |
| 811.tealeaf_s | 0.379649 | 0.327472 | -13.743% | 9.877 |
| 854.graph500_s | 1.022196 | 0.935125 | -8.518% | 6.386 |

聚合 CPI absolute error mean/P90/P99/max 为 **23.590%/53.698%/89.658%/93.653%**，最低吞吐 3.764M UOP/s。除 `lbm` 外，其余五个 workload 是 -13.7% 到 -0.2% 的低估；`lbm` 是方向相反的巨大高估。

当前 FS 只验证了一个 C8 目标微架构，尚未形成跨 FS 微架构的 material pair 和排名准确率；因此微架构泛化结论目前只属于上面的 SE 验证域。

#### FS PMU

| PMU | WAPE | 结论 |
|---|---:|---|
| L1D accesses / misses | 20.109% / 7.666% | access 定义仍需闭合，尤其 graph500 |
| private-L2 accesses / misses | 11.680% / 3.927% | miss 数量较接近 |
| CHA lookups / LLC tag misses | 3.927% / 3.628% | functional memory path 接近 |
| branch direction misses | 35.333% | wrong-path/speculative predictor update 不可见 |
| DTLB accesses / misses | 9.868% / 10.682% | FS page-walk/访问口径未闭合 |
| DRAM reads / writes | **0.533% / 1.955%** | 请求数已经非常接近，CPI 仍未收敛 |

two-phase warmup 把 DRAM read WAPE 从 3.147% 降到 0.533%，但 `lbm` CPI 只从 cold 的 +94.820% 变为 +93.653%。因此“缺少 warmup”和“DRAM request 数量错误”都已被排除为主因。

## 3. 当前识别到的关键瓶颈

### 3.1 第一优先级：DRAM read/write controller 语义不完整

基础几何和主要固定延迟已经与 FS 目标对齐：8 channel、2 rank/channel、16 bank/rank、8KiB row、`RoRaBaCoCh`，以及约 43-cycle 的 tCL/tRCD/tRP。当前主要缺口不是简单的 DRAM latency 参数：

- gem5 有独立 64-entry read queue 和 128-entry write queue，以及 85%/50% write-drain threshold、至少 16 个请求的 turnaround；
- FastSim 当前没有独立 write queue，LLC dirty eviction 会立即修改 DRAM bank/channel calendar；
- C8 topology-scaled FR-FCFS effective window 实际为 1，`lbm` 的 2,793,826 个请求全部走 bypass，没有执行当前 FR-FCFS repair/open-adaptive queue scan；
- tRAS/tRTP/tRRD/tXAW/tCCD_L、read/write turnaround、refresh 等次级 command 约束还没有完整闭合。

直接统一增加 latency 会让当前低估 workload 看起来改善，却会进一步恶化已经高估 93.653% 的 `lbm`，因此不是可接受方案。

### 3.2 第二优先级：memory response 到 OoO ordered-retire 的闭合

FastSim 已经能重建 memory event 数，但 response 如何形成可见 stall 仍不够精确：

```text
DRAM/cache response
      ↓
dependency wakeup → IQ/LQ/SQ release → ROB-head crossing → ordered retire
      ↑                                      ↓
      └──────── MLP/slack 是否吸收 ──────────┘
```

当前 interval/checkpoint 可能把 response gap 过多暴露为 critical path，也可能在高 MLP workload 中被 lower-bound prefix slack 吸收。这个问题同时解释：

- SE C8/C16 sequential tail 和 C32 random/sequential 反方向 residual；
- business fanout absolute CPI 低估约 40% 以上；
- FS `lbm` worker 普遍 memory over-exposure，再被 core 0 makespan 放大。

### 3.3 `lbm` 为什么特殊

`lbm` 是规则、持续、读写并存的浮点 streaming workload：

- DRAM read/write 强度为 13.537/6.356 requests per kUOP，write/read 为 47.0%；
- 约 97% private-L2 miss 到达 DRAM，read row-hit 只有 25.5%；
- gem5 SQ 平均占用约 97%，LSQ-full 约占 42.7% cycles；
- gem5 每 channel write queue 平均 41.55 entries，并以约 16.05 个 write 一批 drain；
- FastSim core 0 只有 12.29M UOP，却有 360.5K L2 miss，其 128.94M active cycles 中 126.17M 被记为 exposed memory cycles。

因此它对 write buffering、read priority、SQ release、TSO 顺序和 MLP/slack 极其敏感。即使忽略 core 0、改用最慢 worker 的完成时间，FastSim CPI 仍高约 46%，说明既有 worker 普遍 over-exposure，也有 core 0 尾部放大。

### 3.4 functional-only 的不可辨识边界

branch predictor 因果消融已经证明 wrong-path occupancy 是真实 CPI 组件，但 committed trace 不能唯一恢复错误路径深度、OpClass、地址、依赖和资源占用。统一 branch shadow 在常规 gate 中把 `int_div_serial` 误差从 0.108% 放大到 38.772%，因此已经否决。

同理，不能通过 workload-specific scalar、gem5 enqueue tick、row-hit label、service order 或 workload ID 修复当前尾差。这些做法会提高已见数据精度，但破坏新 workload/新微架构 DSE 的输入合同。

### 3.5 性能瓶颈

- SE C16/C32 最慢 case 只有 4.094M/4.159M UOP/s，低于 5M gate；
- FS 最慢为 3.764M UOP/s；
- 更精确的 suffix carry 曾把阈值套件方向/排名提高到 100%，但吞吐降到 3.746M UOP/s；
- 根因是 response-active epoch 的全前缀扫描、回滚和重放，而不是 trace decode 本身。

下一版需要增量 checkpoint/undo，只撤销 crossing UOP 之后的 ROB/LSQ/dependency/shared-queue 状态，不能再重扫已经认证的前缀。

## 4. 后续优化方向与验收顺序

### P0：先增加只读阶段账本，不改变 CPI

对 `lbm` 的 core 0 和 worker 分开记录：

- demand read、architectural store、dirty writeback；
- controller arrival、channel/rank/bank/row、row-hit/conflict；
- queue wait、command/data-bus wait、fill visibility；
- dependency wakeup、SQ release、ROB-head crossing 和 ordered retire；
- 每阶段 mean/P50/P90/P99 及 critical-path exposure。

目的不是新增一个总 latency，而是先找出第一次与 gem5 离线阶段 PMU 发生系统性偏离的位置。

### P1：实现 source-derived read/write controller 实验

- 使用目标 64/128 queue、85%/50% threshold、min-16 drain 和 FastSim 自己重建的 arrival；
- 将 dirty writeback service 与 architectural store response/SQ release 分开做单变量消融；
- 再补 C8 FR-FCFS selector 的 seamless row hit、hidden bank preparation、prepped row、earliest available bank；
- controller order 闭合之后，才逐项启用 tRAS/tRTP/tRRD/tXAW/tCCD_L 等 command timing。

### P2：做增量 response→ordered-retire closure

- 以 ROB-head crossing 为锚点；
- 对 response causal cone 维护可回滚 prefix checkpoint/undo；
- 精确追踪 dependency、IQ/LQ/SQ release 和 ordered retirement；
- 保持 Q=1024，不通过调 Q 选择精度结果。

### P3：分层回归，不混入已否决变量

1. FS two-phase 6-case：`lbm` 必须显著收敛，其余五个 workload 不得回退；
2. SE memory directed gate：random/seq × C4/C8/C16/C32；
3. SE 完整 92-case：每个 core count CPI P99 ≤10%，最低吞吐 ≥5M，所有守恒为 0 failure；
4. 阈值 48-case：继续要求排名 ≥90%、speedup P90 ≤10%、无欠激励参数；
5. business-excitation 48-case：CPI P99、方向、排名必须同时过 gate；
6. 通过后再扩展 leave-workload-family-out、leave-core-count-out 和 leave-uarch-out 验证。

继续保持关闭：physical-register free-list、gem5 backward edge、branch shadow、whole-epoch causal replay。禁止使用统一 DRAM latency scale、workload-specific scalar 或 gem5 timing/PMU 在线输入。

### P4：独立处理 FS 输入/统计口径

- `graph500` 的 L1D access 仍低约 59%，需闭合 memory UOP、跨页拆分、page walk 和 Ruby demand 定义；
- FS branch miss WAPE 35.333%，需要明确 committed-only 可达上限，不能用 penalty 抵消；
- 修复 capture sidecar 中 fallback 4GHz/2MiB/1-bank 元数据，自动以 `request.json/config.ini` 冻结目标身份。

## 5. 周会建议口播

> FastSim 的目标不是复制一个更快的 gem5，而是只用 functional trace，在显式目标状态上重建对 DSE 有用的 CPI、PMU 和相对排序。现在 functional cache、CHA、branch PMU 已经达到亚百分比误差；常规 SE 的 CPI mean 已到 2.4%--4.5%，但 C8/C16 尾差和 C16/C32 最慢吞吐还没全部过 gate。微架构方面，真正跨过 ROB、IQ、TLB 和 cache 容量阈值的套件上，FastSim 排名准确率 93.75%、speedup P90 误差 2.77%，说明 DSE 在已验证参数域内有效。需要强调的是，高 MLP 业务形状和 FS 还没解决：业务激励 P99 仍 46.36%，FS 的 lbm 仍高估 93.65%。最新证据已经把问题从“请求数或固定 DRAM 延迟”收窄到 read/write controller service order 和 memory response 到 SQ/ROB ordered-retire 的闭合。下一阶段先加只读阶段账本，再实现独立读写队列和增量 response closure，最后依次跑 FS 6-case、SE memory gate、92-case 和两套泛化 gate。

## 6. 数据来源与可复现索引

- [SE 92-case 正式汇总](../tmp/default-off-production-full92-final-v1/summary.md)
- [微架构阈值 48-case](../tmp/uarch-c4-excitation-first-batch/evaluation-fill8/generalization-report.md)
- [hard business-excitation 48-case](../tmp/business-excitation-c4/evaluation-fill8-full/generalization-report.md)
- [FS two-phase 6-case](../tmp/fs-c8-two-phase-functional-warmup-final-v1/summary.md)
- [微架构采集与参数激励证据](uarch-generalization-collection.md)
- [微架构消融和否决记录](uarch-generalization-debug-log.md)
- [FS DRAM/`lbm` 根因审计](gem5-source-aligned-p99-plan.md)第 24 节
- [架构与 causal-frontier](architecture.md)
- [functional trace 合同](gem5-trace-contract.md)

当前 SE 92-case 身份：

```text
FastSim SHA-256: b66255520f57a1e47be989f3f658916b5e1a7a28be14a5c8a26dd2071face391
config SHA-256:  23744808154b73dcda28eb34634e68b1f505d77364f7484bc7b89da82e8fa640
```
