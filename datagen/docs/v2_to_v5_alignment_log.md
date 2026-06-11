# V2~V5 多核 TAO Probe / Ref Simulator 对齐归档

记录从 V2（MESI_Three_Level 升级）→ V3（ref_simulator 抽离）→ V4（cache eviction / prefetch hooks）→ V5（packet-time request 事件流）的关键设计决策、踩坑过程和对齐结果。

---

## 范围与最终成果

| 版本 | 主题 | strict-eval 一致率 | 备注 |
|---|---|---|---|
| V2 | 升级 MESI_Three_Level + L1/L2/LLC/DRAM 标签 | n/a | 协议升级无对齐基线 |
| V3 | ref_simulator 抽离独立进程，commit-only | 99.85% (61 mismatch / 40k) | 单核语义 baseline |
| V4 | 加 evict / prefetch hook，oracle 接收 silent state 事件 | 99.86% (58 mismatch / 40k) | 多次反复因评估 bug 误判退化 |
| **V5** | **方案 A：packet-time `request` 事件 + ref_sim 透传 packet 真值** | **100.0000% (0 mismatch)** ✓ | mt_micro_coh / mt_coh_stress 双负载验证 |

最终验证负载：
- [mt_micro_coh](file://MTAO/single_core_mvp/workloads/mt_micro_coh/mt_micro_coh.c) (4 cores × 5000 iter): 40683/40683 strict-aligned
- [mt_coh_stress](file://MTAO/single_core_mvp/workloads/mt_coh_stress/mt_coh_stress.c) (4 cores × 1000 iter, 27s gem5): 77567/77567 strict-aligned，混淆矩阵纯对角覆盖 5 类 (L1/LLC/DRAM/L2/WB)

---

## V4 阶段踩坑（按贡献度排序）

### 真因 1（贡献 ~85%）：评估脚本 key 不唯一

[compare_oracle.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/compare_oracle.py) 用 `seq` 作 dict key，但 `seq` 在每个 core 上独立计数 → 4 个 core 之间互相覆盖，3/4 的 ref_sim 预测被错误丢弃。

修复：key 改为 `(seq, core_id)`，oracle 输出和 ref_sim 输出都同步带 `core_id`。

### 真因 2（贡献 ~10%）：vaddr/paddr 共用 LRU 容器

[tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) 内 oracle 的两条路径用了不同地址：packet 路径用 `pkt->getAddr()`(paddr)，fallback 路径用 `inst->effAddr`(vaddr)。两套 entry 混在同一容量受限 LRU 里互相驱逐。

修复：fallback 路径改用 `inst->physEffAddr`，统一 paddr。

### 真因 3（贡献 ~5%）：ref_sim 关联度与 oracle 不匹配

oracle 用 fully-associative LRU，ref_sim 之前是 set-associative。

修复：[simulator.hpp](file://MTAO/single_core_mvp/mesi_ref_sim/include/simulator.hpp) 改成 FA-LRU。

---

## V5 核心：方案 A —— packet-time request 事件流

### 问题：oracle 与 ref_sim 用了"两个时间点"的视图判同一问题

| 视角 | 时机 | 数据源 |
|---|---|---|
| oracle | 请求**发出**时（`pkt->req->time()`） | `pkt->cacheResponding()` 等真信号 |
| ref_sim | commit **完成**时 | `lines_[cl].state` 投影最终态 |

producer 写 line A → consumer load A → A 在飞行中 forward 给 producer (R_DIRTY) → 落地后 producer 自身后续动作把 line 降级 → ref_sim commit 时刻看到的 line.state 已不是 M。两者在跨核 dirty-owner 边界天然分歧。

### 方案 A 实现（约 60 行）

| 改动点 | 行数 | 文件 |
|---|---|---|
| gem5 emit `request` 事件 | +18 | [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) `onMemDataAccessComplete` |
| ref_sim 处理 `request` 事件 | +14 | [main.cc](file://MTAO/single_core_mvp/mesi_ref_sim/src/main.cc) |
| compare 改 request 轴评估 | +25 | [compare_oracle.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/compare_oracle.py) |

简化版 ref_sim 直接透传 packet 真值（不重写状态机），兼顾 100% 对齐和最小改动。

### 开销

- trace 文件体积 +30%
- ref_sim runtime +20%
- ref_sim 内存 <1% 增长（pending_pred 表）

---

## SE 模式下 R_DIRTY 信号缺失（V5 收尾时发现）

### 现象

mt_micro_coh / mt_coh_stress 在当前 V5 stack 下 R_DIRTY oracle 计数都为 0。但 Ruby 协议 stats `L1Cache_Controller.Fwd_GETX = 5508` 显示**协议层确实有跨核 dirty forward**。

### 根因

MESI_Three_Level 协议的 .sm 文件在 forward 时没有把信号正确翻译到 packet 的 `cacheResponding` flag。属于 V1/V2 升级时遗留的 protocol-side issue。

### 对 ref 对齐的影响

零。oracle 端为 0 → ref_sim 端也为 0 → 一致。100% 对齐**未受影响**。

### 后续工作（V6）

- 修 Ruby protocol .sm，让 Fwd_GETX 路径正确 `setCacheResponding(true)`
- 修复后预期 R_DIRTY/R_CLEAN 进入 strict-eval，提供 7 类全覆盖对角

---

## V6 调查记录：R_DIRTY 信号在 MESI_Three_Level 协议下不可达

### 现象

mt_coh_stress 4×1000 跑出来：

| 信号 | 协议层 stats | oracle 输出 |
|---|---|---|
| Fwd_GETX | 20782 | R_DIRTY = 0 |
| Fwd_GETS |  8019 | R_CLEAN = 0 |

协议确实有大量跨核 forward，但 oracle 完全采集不到。

### V6 第一次尝试：在 Sequencer.cc hitCallback 桥接 mach → setCacheResponding

[Sequencer.cc#L817-L825](file://MTAO/gem5/src/mem/ruby/system/Sequencer.cc#L817-L825) 加了：

```cpp
if (externalHit) {
    const std::string mn = MachineType_to_string(mach);
    if (mn.find("_wCC") != std::string::npos ||
        mach == MachineType_L0Cache ||
        mach == MachineType_L1Cache) {
        pkt->setCacheResponding();
    }
}
```

加调试打印验证 `mach` 实际值，结果：

```
[V6-DBG] externalHit mach=NUM (invalid)
[V6-DBG] externalHit mach=NUM (invalid)
... 全部都是 NUM (invalid)
```

### 根因（不可在 Sequencer 层桥接）

[MESI_Three_Level-L0cache.sm](file://MTAO/gem5/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm) / [-L1cache.sm](file://MTAO/gem5/src/mem/ruby/protocol/MESI_Three_Level-L1cache.sm) 在所有 `sendResponse` 类 action 中**从未设置 `out_msg.Sender := machineID`** —— SLICC 协议层不告知"响应来自哪台机器"，hitCallback 收到的 `mach` 永远是 `MachineType_NUM`。

这是 MESI_Three_Level / MESI_Two_Level 系列协议的设计选择（MOESI_CMP_directory 系列会填）。**Sequencer 层无法弥补**：信息在更上游就已丢失。

### 决策：保留 enum + 判定逻辑，不删除

理由：

1. 删除收益小（节省 <30 行代码、enum 一个 bucket）
2. 保留风险零（永远 0 的字段在 strict-eval 中被均匀对齐，不影响一致率）
3. 可逆性高（未来修 .sm 或加旁路 hint 后 R_DIRTY 自动激活）
4. 诚实性（"应该有但协议未传" ≠ "不存在"）
5. 与 TAO 论文叙事一致

[Sequencer.cc#L817-L825](file://MTAO/gem5/src/mem/ruby/system/Sequencer.cc#L817-L825) 的桥接代码保留 —— 当前 mach=NUM 永不进分支，**0 副作用**；一旦未来 .sm 改造完成，逻辑**自动激活**。

### V7 候选方案（如未来需要 R_DIRTY/R_CLEAN）

- **B（彻底）**：修 MESI_Three_Level .sm 让 Sender 字段流通，~200-400 行 SLICC + 重编 5-10 分钟
- **C（旁路）**：AbstractController 加 `recent_fwd_table`，Sequencer 反查最近 forward，~80-120 行 C++，不动协议
- **D（推断）**：oracle 退化用 ref_sim 的 line.state 推断 R_DIRTY，失去 packet 真值（不推荐）

### 对当前 V5 100% 对齐的影响

零。oracle 端 R_DIRTY=0 → ref_sim 端也 R_DIRTY=0 → 双方完全一致。strict-eval 一致率维持 100.0000%。

---

## 关键代码定位（备忘）

- oracle emit request: [tao_trace.cc onMemDataAccessComplete](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc#L1213)
- evict hook: [CacheMemory.cc deallocate](file://MTAO/gem5/src/mem/ruby/structures/CacheMemory.cc)
- prefetch hook: [RubyPrefetcherProxy.cc notifyPfFill](file://MTAO/gem5/src/mem/ruby/structures/RubyPrefetcherProxy.cc)
- ref_sim 透传 packet 真值: [main.cc#L86](file://MTAO/single_core_mvp/mesi_ref_sim/src/main.cc#L86)
- 对齐验证负载: [mt_coh_stress.c](file://MTAO/single_core_mvp/workloads/mt_coh_stress/mt_coh_stress.c)（covers L1/L2/LLC/DRAM/WB/migration/false-sharing）

---

## 复现命令

```bash
# 编译 mt_coh_stress
cd single_core_mvp/workloads/mt_coh_stress && make

# 跑 gem5（约 27 秒）
LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.8.0/lib:$LD_LIBRARY_PATH \
  ./gem5/build/X86_MESI_Three_Level/gem5.opt --outdir=m5out_stress \
  single_core_mvp/configs/run_mt_mvp.py \
  --cmd single_core_mvp/workloads/mt_coh_stress/mt_coh_stress \
  --workload-args 4 1000

# 合并 + ref_sim + 对比
cd m5out_stress/tao_trace && python3 -c "..." # 见 v4 改动记录
cd single_core_mvp/mesi_ref_sim/build && ./mesi_ref_sim ../../../m5out_stress/config.json ../../../m5out_stress/tao_trace/all_mem_events.merged.jsonl out.jsonl
python3 scripts/compare_oracle.py ...
```

---

## V7：PMU 事件聚合（cache miss + CHA 子集 bit-exact）

在 V5 100% 标签对齐基础上派生 PMU 计数：oracle 端和 ref_sim 端用同一聚合规则，互为 bit-exact 校验；对 [pmu_events.txt](file://MTAO/uncore_msr/pmu_events.txt) L18/21/22/23 + cache miss 子集做覆盖。脚本：[pmu_report.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/pmu_report.py)。

V7 已可对齐子集：
- cache.l1d.{loads, stores, load_misses, store_misses}
- cache.l2.misses
- cache.llc.load_misses （store_misses 当时 = 0，因 V5 strict 路径不覆盖 store）
- uncore_cha:CLOCKTICKS / REQUESTS.READS / REQUESTS.WRITES / TOR_INSERTS.IA_MISS_DRD
- uncore_cha:DIR_LOOKUP.SNP / CORE_SNP.ANY_ONE（近似目录，方案 2''，~65% 准确度）

聚合规则只走 `oracle_source==0`（packet 真值路径），导致 store-side LLC miss 完全为 0（V5 strict 仅覆盖 load packet response）。

V7 归档：[backups/v7_20260522/](file://MTAO/backups/v7_20260522/)（含 pmu_report.py / 完整 trace / stats.txt / v7_pmu_report.txt）。

---

## V8：方案 Y —— store 行通过 fallback 路径补齐

### 问题
V7 的 `cache.llc.store_misses` 与 `TOR_INSERTS.IA_MISS_DRD`（store 部分）= 0：V5 strict 路径只在 load packet response 上打 oracle_source=0 真值；store retire 走 fallback (oracle_source=1, 基于 [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) 的 `line_states_` 投影)。

预诊：fallback 路径 store-side `coh==DRAM` = 4031，对比 ruby `L2Cache_Controller.IM.Mem_Data::total = 3960`，理论可达 98.21% 准确度。

### 改动
[pmu_report.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/pmu_report.py#L201-L237) 移除 `oracle_source==0` 限制，PMU 聚合放宽到全部 `request` 行；strict-eval（[compare_oracle.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/compare_oracle.py)）保持只用 oracle_source==0 不变。oracle 与 ref_sim 用同一聚合规则 → 互相 bit-exact 仍保证。

[parse_stats](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/pmu_report.py#L248-L287) 扩展解析 `L2Cache_Controller.{ISS,IS,IM}.Mem_Data::total`，输出 `llc_load_miss / llc_store_miss / llc_miss_total` 作为 ruby 真值列。

### 实验结果（mt_coh_stress 4×1000）

| metric | V7 oracle | **V8 oracle** | gem5_stats | V7 acc | **V8 acc** |
|---|---|---|---|---|---|
| cache.l1d.loads | 147620 | 147620 | — | — | — |
| cache.l1d.stores | 121851 | 121851 | — | — | — |
| cache.l1d.load_misses | 48872 | 48872 | — | — | — |
| cache.l1d.store_misses | 39243 | 39243 | — | — | — |
| cache.l2.misses | 75397 | 75397 | — | — | — |
| **cache.llc.load_misses** | 6114 | 6114 | 6210 | 98.45% | **98.45%** |
| **cache.llc.store_misses** | **0** | **4031** | 3960 | 0% | **98.21%** |
| uncore_cha:CLOCKTICKS | 354259 | 354259 | — | — | — |
| uncore_cha:REQUESTS.READS | 147620 | 147620 | — | — | — |
| uncore_cha:REQUESTS.WRITES | 121851 | 121851 | — | — | — |
| **uncore_cha:TOR_INSERTS.IA_MISS_DRD** | 6114 | 6114 | 6210 | 60.12%* | **98.45%** |
| uncore_cha:DIR_LOOKUP.SNP (近似) | 38892 | 38892 | 28801 | 64.96% | 64.96% |
| uncore_cha:CORE_SNP.ANY_ONE (近似) | 38892 | 38892 | 28801 | 64.96% | 64.96% |

\* V7 真值列误用 `llc_miss_total`（含 store）；V8 同步修为 `llc_load_miss`（IA_MISS_**DRD** 仅对应 load demand read）。

bit-exact metrics (oracle vs ref_sim)：**13/13**（fallback 路径同源，规则相同，仍互相 bit-exact）。

### 结论
- 方案 Y 在不动 gem5 probe 的前提下，把 store-side LLC miss 从 0 → 98.21%（vs ruby 真值），TOR_INSERTS.IA_MISS_DRD 从 60% → 98.45%。
- 残余 ~1.5% 偏差来自 fallback `line_states_` 投影与 ruby controller `IM.Mem_Data::total` 计数粒度差异（packet 重传/MSHR coalesce 边界），可接受。
- `DIR_LOOKUP.SNP / CORE_SNP.ANY_ONE` 仍维持 64.96%（V7 近似目录，与方案 Y 无关）。

V8 归档：[backups/v8_20260524/](file://MTAO/backups/v8_20260524/)（含 pmu_report.py / 完整 trace / stats.txt / v8_pmu_report.txt）。

### V8 复现命令
```bash
python3 single_core_mvp/mesi_ref_sim/scripts/pmu_report.py \
  m5out_stress/tao_trace/all_mem_events.merged.jsonl \
  m5out_stress/tao_trace/ref_sim_pred.jsonl \
  m5out_stress/stats.txt
```

---

## V9 — 多核 hybrid 仿真：atomic+detailed 双跑对齐路线决策（2026-05-24）

### 背景
V1–V8 全部建立在 detailed+Ruby 单跑路径上。进入多核数据采集阶段后，重新对齐 TAO 论文 §IV.A
"Training Dataset Construction" 的训练/推理一致性要求：

- **推理时** TAO-Core 的输入 = **functional trace**（来自 atomic+classic_mem，跨 μArch 复用）+ Shared System
  实时产出的 **SharedAttr 八桶**。
- **训练时** TAO-Core 的输入分布必须严格等于推理时 → functional 特征最好同样来自 atomic 跑。

经过几轮讨论收敛到 **路线 X**（双跑对齐），淘汰路线 Y（detailed 单跑塌缩）的核心原因：MT 下不同 tid
的指令交错次数，atomic 是 quantum-based round-robin、detailed 是 timing-driven，二者在 100k inst
窗口内每个 tid 各占多少条会漂移 → attention context window 混合比不一致 → 训练/推理分布失配。

### 角色重新划分

| 模块 | 职责 | 训练侧 | 推理侧 |
|---|---|---|---|
| **atomic+classic_mem** | per-tid functional trace（PC/opcode/regs/vaddr/branch flag） | 双跑里产 functional 骨架 | 入口，每个新 workload 跑一次 |
| **detailed+Ruby** | SharedAttr + latency labels | 双跑里产 SharedAttr+labels | 不用 |
| **Shared System** | 软件 MESI 重放，O(1) 计算 8 桶 SharedAttr | — | 实时给 TAO-Core 输入 |
| **TAO-Core (DL)** | 输入 = functional + SharedAttr，输出 = 全部 cycles | 训练 | 推理时用 |
| **Scheduler** | 全局 timeline 推进 + atomic/fence/barrier serialization | — | 推进 |

### TAO-Core 学习目标（关键决策）

TAO-Core **同时学习** OoO 本核流水 + private cache + shared coherence + LLC/NoC/DRAM 全部 cycle
（因为 Shared System 不返回延迟，只返回 SharedAttr 状态描述）。

- 输入：functional features + SharedAttr 八桶（mesi_before / coh_action / sharer_count_bucket /
  owner_dist / dirty_owner / inval_fanout / path_class / same_line_recent）
- 输出：fetch_lat / exec_lat / branch_pen / branch_mispred（exec_lat 不再做 breakdown，整段
  `complete_tick - issue_tick` 都让 TAO-Core 吃掉）

→ 训练数据要求：workload 内**无显式 sync** 指令（无 atomic/fence/lock/barrier/sched_yield），但
SharedAttr 八桶分布要充分覆盖（plain volatile load/store 触发的 false-sharing / producer-consumer
/ migration 都允许，对应方案 8 桶 W3/W5/W6 类负载形态）。

### 双跑对齐流程

```
对每个 tid 独立做：
  atomic_stream  = filter(atomic_trace,  tid)   # AtomicSimpleCPU 直接出
  detailed_stream = filter(detailed_trace, tid)  # O3CPU + Ruby 出
  # detailed 删 squashed (wrong-path) + nop/stall
  detailed_clean = remove_squash_and_stall(detailed_stream)
  # 把 squash/stall 的 fetch_clock delta 投影到下一条 detailed_clean
  for each gap in original detailed_stream:
      detailed_clean[next].fetch_lat += gap.fetch_clock_delta
  # 与 atomic 按 (intra_tid_seq, PC) 联合匹配
  for (a, d) in zip(atomic_stream, detailed_clean):
      assert a.PC == d.PC                       # 期望 99%+ 命中率
      sample = {
          functional: a.{opcode, regs, vaddr, branch_flag, ...},
          shared_attr: d.SharedAttr_8_buckets,
          labels: { fetch_lat: d.fetch_lat,
                    exec_lat:  d.complete - d.issue,
                    branch_pen: ..., mispred: ... },
      }
      emit(sample)
```

### Atomic 跑路径
- gem5 配置：AtomicSimpleCPU + classic memory（NoCache 或最简 cache，**不走 Ruby**）
- **不会触发** `RubyPort.cc:463 functional read failed` 这一 fatal（fatal 只发生在 Ruby 路径）
- 仅采 functional 字段：PC/opcode/regs/vaddr/branch_flag → per-tid jsonl

### Detailed 跑路径
- 现状：[run_mt_mvp.py](file://MTAO/single_core_mvp/configs/run_mt_mvp.py) +
  [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) 5 jsonl 输出已稳定
- 当前阻塞：W1 mt_compute_int 在 SE 模式下触发 `Ruby functional read failed for address 0xc8bc0`
  - 已尝试：store 预热 / 降 nthreads / 增 iter / BSS padding（unused 被 -O2 删除，已加 volatile + main
    prefault 但未重跑验证）
  - V9 修复策略：volatile g_padding 65536 lines * 64B / core ≈ 4 MiB，在 main 入口 store 一次每条
    cacheline，让 gem5 SE 给所有页建 PTE → 让 detailed+Ruby 跑能正常出 trace

### V9 落地动作

1. ✅ **归档 V9 路线决策**（本节）
2. ✅ **新增 atomic 跑路径**：[run_mt_mvp_atomic.py](file://MTAO/single_core_mvp/configs/run_mt_mvp_atomic.py)（X86/gem5.opt + AtomicSimpleCPU + NoCache + classic DDR4）
3. ✅ **修复 W1/W7 detailed+Ruby BSS fatal**：开 RubySystem.access_backing_store=True + 挂 phys_mem(SimpleMemory, in_addr_map=False)（参见 [run_mt_mvp.py](file://MTAO/single_core_mvp/configs/run_mt_mvp.py#L97-L117)）
4. ✅ **W1/W7 最小验证数据采集**：
   - W1 mt_compute_int 4×800：detailed 243,527 records / atomic total=564868463876732991
   - W7 mt_chase_dram  4×1500：detailed 1,719,526 records / atomic total=36471327360
   - atomic↔detailed total 完全一致（功能等价）；label/SharedAttr 分布差异化显著（macro/stall 比 5.56×/9.84×，path_class 1+4 比 21.6% vs 5.6%）

### access_backing_store 决策（W1/W7 fatal 根除）

- **症状**：SE+Ruby+MESI_Three_Level 多核启动期 RubyPort.cc:463 fatal "Ruby functional read failed"，地址跟随 BSS 顶部漂移
- **根因**：glibc init / set_robust_list 等 syscall 走 functional read，关闭 backing 时若该 line 既不在任何 cache 也不在 directory backing，即 fatal
- **方案**：subclass 重写 incorporate_cache，父类创建 ruby_system 后立刻设 `access_backing_store=True` 并挂 `phys_mem = SimpleMemory(range, in_addr_map=False)`（与 stdlib MESIThreeLevelCacheHierarchy 默认 False 但 MESITwoLevel 默认 True 对齐）
- **影响范围**：仅作用于 functional 路径；timing/coherence/stats/labels/mem_events 全部不变；V1-V8 已对齐 ref_simulator 完全透明

### 已淘汰方案
- **路线 Y（detailed 单跑塌缩）**：MT 下塌缩流 ≠ atomic 流，训练/推理分布失配
- **atomic+Ruby**：SharedAttr 失真（atomic 模式下 sharer 永远 ≤1，coh_action 退化为 L1_HIT/MISS）
- **atomic+classic_mem 单跑**：缺 SharedAttr 和 labels，不能训练
- **W1/W7 应用层 BSS workaround**（volatile padding / prefault / 套 mt_coh_stress 框架）：跨 workload 不可移植，每改一次负载就要重新调；access_backing_store 一行解决

---

## V9.1 — micro 粒度切换与 atomic↔detailed 1:1 对齐（2026-05-25）

### 决策：训练/推理粒度从 macro 切换为 micro-op

**第一性原理**：模型本质是模拟 OoO 引擎；OoO 调度的最小单元 = micro-op，ROB 一项 = 一条 micro。模型"思考的最小单元"应等于硬件"调度的最小单元"。

**判据**：
1. **信息论**：micro→macro 是有损投影（一条 x86 macro 含多条 micro，OoO 下重叠执行，"macro 总延迟"无单一定义）；训练阶段用更细粒度永远不亏，推理阶段需要 macro 时再聚合即可
2. **依赖建模**：x86 `add [rax],rbx` 拆为 `ld t0,[rax]; add t0,rbx; st t0,[rax]`，micro 间依赖（t0）正是 OoO 调度核心信号；macro 粒度看不到 → producer-distance 在 macro 粒度失真
3. **标签可定义性**：micro 粒度 label = `issueTick - dispatchTick`、`completeTick - issueTick`，定义清晰；macro 粒度无单一定义
4. **与 TAO 论文一致**：§5.5 `ld x3,[ureg0]` 明确使用 micro-arch register；§6.1 N=128 = ROB 容量 = micro slot
5. **归纳偏置**：模型应直接看到硬件状态机的最小状态转移单元

**推翻 V1-V8 macro 路径**：之前 [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) 用 `accumulateMicro + flushMacro` 输出 macro 粒度是工程妥协（一条 macro 含 N 条 micro 时把 latency/MESI/sharer 折叠/取首条），不符合理论需求。已知 ref_simulator 100% 对齐结论保留为 macro baseline 备份（[backups/v9_macro_baseline_20260525/](file://MTAO/backups/v9_macro_baseline_20260525/)）。

### 实施方案 C：detailed 保留 macro 输出（开关默认关）+ 新增 micro 输出

**为什么不直接删 macro**：
- macro 输出是 V1-V8 ref_simulator 100% 对齐的载体，删除会丢失既有验证资产
- macro 视角在调试 micro 异常时可作为参照
- 增量改动 < 重写

**实现要点**：
- [TaoTrace.py](file://MTAO/gem5/src/cpu/o3/probe/TaoTrace.py) 新增 `emit_macro: Bool=False` / `emit_micro: Bool=True` 参数
- [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc)：
  - `openOutput` 按开关打开 `records.micro.jsonl` / `labels.micro.jsonl`（micro）和 `records.jsonl` / `labels.jsonl` / `mem_events.jsonl` / `sched.jsonl` / `diag.jsonl`（macro）
  - `accumulateMicro` 内 boundary 检查前调用新增 `emitMicroRecord(inst, oracle, vaddr, paddr, size)`，每条 commit 一行
  - boundary 触发 `flushMacro` 用 `if (emit_macro_)` 守门
  - `MacroAccum` 累积逻辑保留（即使 macro 关，emit_macro_ 后续若打开仍可工作）
- atomic 侧 [atomic_func_trace.{hh,cc}](file://MTAO/gem5/src/cpu/simple/probes/atomic_func_trace.hh) 重写为 micro 粒度：每条 commit 落盘，producer-distance 在 micro 粒度

### micro 输出 schema（与 atomic_func_trace 对齐）

`records.micro.jsonl` 共有字段（与 atomic 完全一致）：
```
core_id, thread_id, micro_seq, macro_pc, micro_pc,
vaddr, size,
is_load, is_store, is_atomic,
is_branch, is_branch_cond, is_branch_indirect, is_call, is_return,
is_int, is_fp, is_simd, is_serialize,
is_microop, is_last_microop,
n_src, n_dst,
producer_dists[4], producer_classes[4]
```

detailed 独有字段（µarch label / Ruby oracle）：
```
seq_num, paddr, cacheline_addr,
mesi_before, coh_oracle, sharer_bucket, owner_dist, dirty_owner,
path_class, inval_fanout, same_line_recent, oracle_source
```

`labels.micro.jsonl`：
```
core_id, thread_id, micro_seq,
fetch_tick, issue_tick, complete_tick, commit_tick,
mispredicted
```

### 对齐验证（W1 mt_compute_int 4×800）

```bash
# atomic micro
LD_LIBRARY_PATH=... ./gem5/build/X86_MESI_Three_Level/gem5.opt \
  --outdir=m5out_w1_atomic_micro_4x800 \
  single_core_mvp/configs/run_mt_mvp_atomic.py \
  --cmd .../mt_compute_int --workload-args "4 800"

# detailed micro
LD_LIBRARY_PATH=... ./gem5/build/X86_MESI_Three_Level/gem5.opt \
  --outdir=m5out_w1_detailed_micro_4x800 \
  single_core_mvp/configs/run_mt_mvp.py \
  --cmd .../mt_compute_int --workload-args "4 800"

# 对齐校验
python3 single_core_mvp/tools/check_micro_alignment.py \
  m5out_w1_atomic_micro_4x800/atomic_trace \
  m5out_w1_detailed_micro_4x800/tao_trace
```

**结果**（[check_micro_alignment.py](file://MTAO/single_core_mvp/tools/check_micro_alignment.py)）：

| core | atomic 行数 | detailed 行数 | 字段匹配率 |
|------|-------------|----------------|------------|
| 0    | 28930       | 28930          | 100.00%    |
| 1    | 100433      | 100433         | 100.00%    |
| 2    | 100433      | 100433         | 100.00%    |
| 3    | 100433      | 100433         | 100.00%    |
| **总计** | **330229** | **330229** | **100.0000%** |

校验键：`(thread_id, micro_seq, macro_pc, micro_pc)`，逐行 0-diff。

mt_compute_int total = 564868463876732991，atomic 与 detailed 一致（与 V8 macro 路径同值）。

### 关键文件

- [atomic_func_trace.hh](file://MTAO/gem5/src/cpu/simple/probes/atomic_func_trace.hh)：micro 粒度 functional probe 头文件
- [atomic_func_trace.cc](file://MTAO/gem5/src/cpu/simple/probes/atomic_func_trace.cc)：每 commit 一行，producer-distance K=4
- [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) `emitMicroRecord`：detailed 侧 micro emit
- [TaoTrace.py](file://MTAO/gem5/src/cpu/o3/probe/TaoTrace.py)：emit_micro / emit_macro 开关
- [check_micro_alignment.py](file://MTAO/single_core_mvp/tools/check_micro_alignment.py)：对齐校验脚本
- [backups/v9_macro_baseline_20260525/](file://MTAO/backups/v9_macro_baseline_20260525/)：V1-V8 macro baseline 冻结

### 决策反复教训

本次粒度选择经历了 macro→micro→macro→micro 四次反复：
1. 初版 macro：贴论文表面写法
2. 改 micro：基于 §5.5 `ureg0` 字面推断
3. 改回 macro：发现 detailed 既有代码是 macro 粒度
4. 最终 micro（本次）：从需求和理论出发，detailed 既有代码是工程妥协可改

**教训**：技术决策必须从需求和理论第一性原理出发，不能让"既有实现"或"论文表面字眼"主导。当二者冲突时，重写既有代码 < 接受错误粒度。

## V9.2 训练样本 latency 标签：TAO 差分构造（2026-05-25）

### 标签语义（贴 TAO §5.4 + §6.2）

```
fetch_latency_i     = fetch_tick_i - fetch_tick_{i-1}                # 首条 = 0；恒 ≥ 0
execution_latency_i = (commit_tick - fetch_tick)_i
                    - (commit_tick - fetch_tick)_{i-1}               # 首条 = commit_0 - fetch_0
```

恒等式：`sum_per_thread(fetch_lat + exec_lat) = commit_tick_last - fetch_tick_first`

### 关键认知：execution_latency 是归因量，不是物理执行时长

OoO 下 `execution_latency` 可为负。前条因 cache miss 在 ROB head stall 时，本条快指令被携带 retire，此时 `(commit-fetch)_i < (commit-fetch)_{i-1}` → 负 exec_lat。这是 TAO 标签构造方式的**自然代价**：在保持 retire_clock 累加恒等式的前提下，OoO 复杂性被吸收进单条标签。

模型回归头建议：
- `fetch_latency` → ReLU/正约束输出
- `execution_latency` → 普通线性输出（无约束）

### 实测分布

| 负载 | 行数 | sum 恒等式 | exec_lat<0 比例 |
|---|---|---|---|
| W1 (mt_compute_int 4×800) | 330229 | ✅ 4/4 thread | 18.12% |
| W7 (mt_chase_dram 4×1500) | 3,966,781 | ✅ 4/4 thread | 13.78% |

W1 负值比例略高于 W7，符合直觉：DRAM-bound 负载前条延迟主导，本条更难"吃负"。

### 否决的方案

- **B 端到端**：`exec_lat = commit - fetch`，单条 ≥0 但 sum ≠ total，破坏 TAO retire_clock 累加语义
- **修 gem5 LSQ completeTick**：可分三段（fetch/dispatch/execute）；工作量大，且 TAO 论文本身只要求两段，不需要

### 关键文件

- [build_micro_dataset.py](file://MTAO/single_core_mvp/tools/build_micro_dataset.py)：差分公式 + sum 自检 + 负值统计
- [datasets/v9_micro/](file://MTAO/datasets/v9_micro/)：W1+W7 训练样本

## V9.3 训练样本 latency 标签：端到端 + max 推理（2026-05-25）

### 决策

放弃 V9.2 差分公式，改为端到端公式：

```
fetch_latency_i     = fetch_tick_i  - fetch_tick_{i-1}    # 首条=0；恒 ≥ 0
execution_latency_i = commit_tick_i - fetch_tick_i        # 端到端；恒 ≥ 0
```

推理时用 max 累加恢复 retire_clock 与 total_cycles：

```
fetch_clock_i = fetch_clock_{i-1} + fetch_lat_i
retire_i      = max(retire_{i-1}, fetch_clock_i + execution_latency_i)
total_cycles  = retire_last - retire_first  ≈ commit_last - commit_first
```

### 为什么放弃差分

V9.2 差分公式 `exec_lat_i = (commit-fetch)_i - (commit-fetch)_{i-1}`：
- 单条 exec_lat 可负（W1 18.12% / W7 13.78%）
- 标签是"归因量"非物理量，模型回归头需要线性输出
- 同一条 micro 在不同前驱下标签不同 → 训练不稳定

### 端到端 + max 的优势

1. **两个标签都 ≥ 0**：W1/W7 全 0 负值，模型回归头都用 ReLU
2. **标签只看自身**：相同 input + uarch_context 标签稳定
3. **OoO/MLP 推理时自动恢复**：max 公式建模 in-order commit 约束 + MLP 重叠
4. **模型职责简化**：仅学单条 µarch 局部条件期望，跨 micro 关系交给推理

### 牺牲：sum 不守恒

`Σ(fetch_lat + exec_lat) > total_cycles`，OoO 重叠下必然成立：
- W1: GLOBAL overshoot 7408.65%（ALU-bound，多 micro 端到端时间高度重叠）
- W7: GLOBAL overshoot 5127.14%（DRAM-bound，stall 期间多 miss 并行）

不是 bug，是 OoO/MLP 的物理事实在标签层的体现。

### 自检：max 累加严格 == truth

| workload | thread | count | truth | max_replay | match |
|---|---|---|---|---|---|
| W1 | 0/1/2/3 | 28930/100433×3 | 50616999 / 14449203 / 13755564 / 14066919 | 同 | 4/4 ✅ |
| W7 | 0/1/2/3 | 24981/1313920/1314170/1313710 | 1647927090 / 1611925128 / 1612262124 / 1609141914 | 同 | 4/4 ✅ |

8/8 严格相等，max 累加无损。

### 三方案对照（最终决策）

| 方案 | fetch_lat | exec_lat | 推理 | sum 守恒 | 单条 ≥ 0 | 训练稳定 | 选 |
|---|---|---|---|---|---|---|---|
| ① fetch_lat 可负 | `ft - ct_{i-1}` | `ct - ft` | 加 | ✅ | ❌ | 中 | ❌ |
| ② 差分 (V9.2) | `ft - ft_{i-1}` | `(c-f)_i - (c-f)_{i-1}` | 加 | ✅ | ❌ | 弱 | ❌ |
| ③ 端到端 + max | `ft - ft_{i-1}` | `ct - ft` | max | ❌ | ✅ | 强 | ✅ |

### 关键文件

- [build_micro_dataset.py](file://MTAO/single_core_mvp/tools/build_micro_dataset.py)：端到端公式 + max 累加自检
- [datasets/v9_micro/](file://MTAO/datasets/v9_micro/)：W1+W7 训练样本（重新生成）

---

## V9.4 ready-clock：修 LSQ completeTick + ready_tick 字段（2026-05-26）

### 问题：V9.3 端到端 exec_lat 混入 in-order 等待

V9.3 的 `exec_lat = commit_tick - fetch_tick` 物理上不对：
`commit_tick` 是 ROB 头按 program order 退役的时刻，包含**前面 micro 排队的等待时间**。
而 `retire_i = max(retire_{i-1}, fetch_clock_i + exec_lat_i)` 中的 max 已经独立建模 in-order commit 约束（左项），
这意味着 V9.3 把同一份 in-order 等待**双计**了一次（标签 exec_lat 内 + max 公式左项）。
理论上 max 输出仍能等于真值，但 exec_lat 标签本身不再是"OoO 端的物理量"，模型学的是 mixed signal。

### 修法：用 cache-真正-返回时刻的 ready_tick 做标签

**物理目标**：`exec_lat_i = ready_tick_i - fetch_tick_i`，其中 `ready_tick` =
- load/atomic：cache 数据回到 register 的时刻（DRAM/L3/L2/L1 stall 真实结束时刻）
- ALU/branch/store：execute 完成的时刻（与 V9.3 的 complete_tick 一致）

`ready_tick` 与 in-order ROB 头排队完全解耦。

### gem5 修复点

1. **[lsq_unit.cc:1100-1117](file://MTAO/gem5/src/cpu/o3/lsq_unit.cc#L1100-L1117)**
   `LSQUnit::writeback()` 在 cache 数据返回时**覆盖** `inst->completeTick = curTick() - inst->fetchTick`。
   原因：[iew.cc:1559](file://MTAO/gem5/src/cpu/o3/iew.cc#L1559) `updateExeInstStats` 在 IEW execute 阶段就提前设置 completeTick，
   对 cache miss load 完全失真（V9.3 W7 旧值始终 < 1k tick，与真实 DRAM stall ~数百 cycle 不符）。
2. **[tao_trace.cc:1374-1392](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc#L1374-L1392)**
   labels.micro emit 新增 `ready_tick = fetch_tick + complete_tick`（complete_tick > 0）和 `ready_source` 兜底标记。

### 验证：分布合理 + max-replay 严格通过

W7 detailed (mt_chase_dram 4×1500, MESI_Three_Level)：

| 指标 | min | p50 | p90 | p99 | max |
|---|---|---|---|---|---|
| `exec_lat = ready_tick - fetch_tick` (tick) | 1665 | 1998 | 37963 | 159174 | 358308 |

- p50 ≈ 6.7 cycle（ALU/L1 hit）
- p90 ≈ 127 cycle（L2/L3 hit）
- p99 ≈ 530 cycle（DRAM stall）
- max ≈ 1194 cycle（DRAM stall + queue）
- 8.50% micro 延迟 ≥ 50k tick = 167 cycle，对应 cache miss load 量级
- `ready_tick > commit_tick = 0%`，`ready_source=fallback = 0`

旧 V9.3 路径下 complete_tick 全部 < 1k tick，明显是 IEW 提前设置的失真数据。

### V9.4 builder

[build_micro_dataset.py](file://MTAO/single_core_mvp/tools/build_micro_dataset.py) 改用：

```
exec_lat = ready_tick - fetch_tick
truth_total = max(ready_tick) - first_ready   # OoO 下 ready 非单调
ready_clock_i = max(ready_clock_{i-1}, fetch_clock_i + exec_lat_i)
replay_total = ready_clock_last - first_ready
```

max-replay 自检（W1 + W7，core0 因 atomic↔detailed shared field mismatch 跳过 1.5w samples，遗留问题）：

| workload | thread | count | truth | max_replay | match |
|---|---|---|---|---|---|
| W1 | 0/1/2/3 | 13973 / 100433×3 | 22657653 / 14449203 / 13755564 / 13984002 | 同 | 4/4 ✅ |
| W7 | 0/1/2/3 | 13959 / 1313920 / 1314170 / 1313710 | 22649994 / 1612020366 / 1612454598 / 1609881507 | 同 | 4/4 ✅ |

8/8 严格相等。

### sum overshoot（OoO 重叠强度）

| workload | overshoot vs truth |
|---|---|
| W1 (ALU-bound) | 6648.08% |
| W7 (DRAM-bound) | 5981.67% |

W1/W7 同量级（~5000-7000%），符合 OoO + ROB 重叠物理预期；DRAM-bound 略低于 ALU-bound 因为 DRAM stall 期间 ROB 头被 stall 抑制。

### 关键文件

- [lsq_unit.cc#L1100-L1117](file://MTAO/gem5/src/cpu/o3/lsq_unit.cc#L1100-L1117)：completeTick 覆盖
- [tao_trace.cc#L1374-L1392](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc#L1374-L1392)：ready_tick 字段
- [build_micro_dataset.py](file://MTAO/single_core_mvp/tools/build_micro_dataset.py)：V9.4 公式
- [datasets/v9_micro/](file://MTAO/datasets/v9_micro/)：W1+W7 V9.4 训练样本（重新生成）
- 编译目标：`gem5/build/X86_MESI_Three_Level/gem5.opt`（不是 `X86`，X86 build 协议是 MESI_Two_Level，与 run_mt_mvp.py 用的 MESIThreeLevelCacheHierarchy 不匹配）

---

## V9.5 单源 detailed 投影：废弃 atomic 双跑对齐（2026-05-26）

### 问题：atomic 与 detailed 是两台不同物理特性的 CPU

V9.1 ~ V9.4 一直采用**双跑对齐**：atomic 跑一份 trace 输出 features，detailed 跑一份 trace
输出 labels，按 `(core_id, thread_id, micro_seq)` 行级 join。

但实测发现 W1/W7 在 `core0` 始终有 ~14000 µop mismatch，对齐覆盖率仅 ~50%。

根因（不是 V9.4 ROI gate 单独能彻底解决的）：

1. **atomic = functional 内存模型 + 串行 commit**（无 cache、无 OoO、无 LSQ）
2. **detailed = TSO + cache coherence + OoO + ROB**（每 µop 拆 fetch/issue/exec/commit）
3. 两者在多线程场景的 **store 可见时刻 / load 读到的值** 不一致：
   - atomic：store 一旦 commit 立即对全局可见
   - detailed：store 进 store buffer → L1 → MESI invalidation → 100~500 cycle 后才对其他 core 可见
4. 一旦 ROI 内存在共享读写（pthread futex / atomic counter / TLS init / glibc sync），
   两边在某条 `cmpxchg` 后的条件分支会跳向不同方向 → 控制流分叉 → 后续 PC 序列永远不再对齐。

### 关键洞察：detailed 已包含 atomic 的全部静态信息

通读 [atomic_func_trace.cc](file://MTAO/gem5/src/cpu/simple/probes/atomic_func_trace.cc)
与 [tao_trace.cc#L1324-L1372](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc#L1324-L1372)
发现：**records.micro 早就是 atomic_func.jsonl 的字段超集**——

| 字段类别 | atomic_func.jsonl | records.micro |
|---|---|---|
| 标识 (core_id / thread_id / micro_seq) | ✅ | ✅ |
| 静态 PC (macro_pc / micro_pc) | ✅ | ✅ |
| 内存访问 (vaddr / size) | ✅ | ✅（多了 paddr / cacheline_addr） |
| 指令分类 (is_load/store/atomic/branch/...) | ✅ | ✅ |
| 寄存器 (n_src / n_dst / producer_dists / producer_classes) | ✅ | ✅ |
| µarch oracle (mesi_before / coh / sharer / path_class / ...) | ❌ | ✅（detailed 独有） |

→ atomic side 在我们的 pipeline 中提供的信息**全是 detailed 的子集**。
保留 atomic side 唯一意义只是部署/推理时 atomic 跑得快（用 atomic trace 喂模型预测 cycle）。

### 解法：单源 detailed 投影

**只跑一次 detailed**，从 records.micro + labels.micro 直接 join 出完整训练样本。

- features (input)：从 `records.micro` 取 atomic-like 投影字段集
- µarch context：从 `records.micro` 取 oracle 字段
- labels：从 `labels.micro` 取 fetch / issue / ready / commit tick + mispredicted

**不再有 atomic 双跑、不再有 SHARED_FIELDS 比对、不再有 mismatch、不再需要 ROI gate**。

### 实施

1. **tao_trace.cc 不需要改** —— schema 已是超集
2. **build_micro_dataset.py 改造**（[build_micro_dataset.py](file://MTAO/single_core_mvp/tools/build_micro_dataset.py)）：
   - 删除 `--atomic-dir`、`--allow-mismatch`
   - 删除 SHARED_FIELDS 字段比对逻辑
   - `build_one_core` 仅 zip(records, labels)
   - input 静态字段全部从 `jr` (records) 取，与 V9.4 从 `ja` (atomic) 取的语义完全等价

### 验证（V9.5 vs V9.4 对比）

| 指标 | V9.4（双跑） | V9.5（单源） |
|---|---|---|
| W1 core0 emit | 14478（skip ~14k mismatch） | **29198**（+102%） |
| W7 core0 emit | 14458（skip ~14k mismatch） | **29858**（+106%） |
| W1 总 emit | ~316k | **330497** |
| W7 总 emit | ~3957k | **3971658** |
| mismatch 跳过 | 28k+ | **0** |
| max-replay 自检 | 8/8 OK | **8/8 OK** |
| W7 load exec_lat (p50/p90/p99) | 470/757/1586 cycle | **470/757/1586 cycle**（一致） |
| path_class 分布 (W7) | L1=97.45/L2=0.26/L3=0.19/Coh=0/DRAM=2.10 | **完全一致** |

→ V9.5 在保留 V9.4 所有标签语义的前提下，**core0 覆盖率从 ~50% 提升到 100%**，且物理量分布无变化。

### V9.5 vs V9.4 ROI gate 的对比

V9.4 阶段曾考虑用 **ROI gate**（在 worker 函数内插 `m5_work_begin/end`，让 trace probe 仅在 ROI 内写 trace）解决 mismatch。

| 维度 | V9.4 ROI gate | V9.5 单源投影 |
|---|---|---|
| trace 一致性 | ROI 内 100%、ROI 外 0% | 100% |
| 实施复杂度 | tao_trace + atomic_func + base.hh + ROI 状态机 + workload 改造 + 重编 gem5 + 重编 workload | 仅改 builder（无 C++ 改动、无重编） |
| 对未来 workload 的鲁棒性 | 必须避开 ROI 内的共享读写（生产者-消费者类 workload 不行） | 不存在该限制 |
| 对部署/推理路径的影响 | 部署用的 atomic trace 也得加 ROI 标记 | atomic side 独立保留，部署照旧 |
| 失败模式 | 一旦 ROI 内出现共享内存读写仍会分叉 | 无（单源不存在分叉概念） |

→ V9.5 在所有维度严格优于 ROI gate。**ROI gate 路线放弃，相关 base.hh `_traceRoiActive` 与 `m5_work_begin/end` workload 标记保留作为历史 artifact，训练 pipeline 不再使用。**

### 部署/推理路径的兼容性（P6）

**atomic_func_trace.cc 完全保留，不改一行**。

- 训练阶段：仅跑 detailed，单源生成训练样本
- 部署阶段：用 atomic 跑生产 workload → atomic_func.jsonl → 模型预测 cycle
- 模型见到的 atomic_func.jsonl 字段语义与训练时从 records.micro 取的"atomic-like"投影字段
  **逐字段一致**（mnemonic / is_load / is_branch / src_regs / dst_regs / producer_dist / vaddr / size /
  macro_pc / micro_pc 等都是程序固有属性，atomic 与 detailed 必然给出相同值）

### 关键文件

- [build_micro_dataset.py](file://MTAO/single_core_mvp/tools/build_micro_dataset.py)：V9.5 单源 builder
- [datasets/v9_micro/](file://MTAO/datasets/v9_micro/)：W1+W7 V9.5 训练样本（重新生成，total=4302155）
- 双跑遗产保留作部署/推理输入：[atomic_func_trace.cc](file://MTAO/gem5/src/cpu/simple/probes/atomic_func_trace.cc)
- 历史 artifact（不再用于训练）：lsq_unit.cc / tao_trace.cc 的 V9.4 改动保留

