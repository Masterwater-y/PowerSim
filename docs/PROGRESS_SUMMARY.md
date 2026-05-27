# V9.5 多核 TAO 训练数据 — 当前进度备份与下一步计划

> 备份日期：2026-05-26 23:11
> 备份目标：在执行 W7 修复（A2 MSHR + A3 4-bank LRU）+ 方案 2（i-cache probe）之前，完整冻结当前 V9.5 状态。

---

## 1. 当前进度（V9.5 已完成）

### 1.1 已落地的核心方案

| 项 | 状态 | 备注 |
|---|---|---|
| µop 级 TAO 训练数据 schema (records.micro / labels.micro) | ✅ 已稳定 | tao_trace.cc 双写 records + labels |
| 单源 detailed 投影（atomic_func 探针不参与训练） | ✅ V9.5 落地 | 解决多核 atomic↔detailed 控制流分叉 |
| LSQ writeback completeTick 修复 | ✅ V9.4 落地 | lsq_unit.cc 覆盖 completeTick = curTick() - fetchTick |
| ready_tick 字段（load/atomic 走 LSQ writeback；其他走 IEW execute；兜底退化 commit_tick） | ✅ V9.4 落地 | tao_trace.cc 写入 |
| labels 三元组：fetch_latency, execution_latency, mispredicted | ✅ V9.5 已澄清 | execution_latency = ready_tick - fetch_tick（不双计 in-order 等待） |
| 推理双累加：fetch_clock 线性、ready_clock = max(prev, fetch+exec_lat) | ✅ V9.5 已澄清 | builder + 文档已对齐 |
| Oracle 概念：训练侧/部署侧同一份 C++ 代码两次实例化 | ✅ V9.5 落地 | tao_trace.cc Oracle 内嵌 + mesi_ref_sim 独立模拟器 |
| Ruby 静态钩子 traceCacheEvent（evict / prefetch fill） | ✅ V4 落地 | RubyPrefetcherProxy + CacheMemory 注入 |
| oracle_source 字段（0=packet 真值 / 1=fallback 推断） | ✅ V8/V9 落地 | 训练时仅作内部诊断字段（对 strict-eval bit-exact），模型不消费 |

### 1.2 4 个 workload 完整一致性验证

| workload | 特性 | µop 数 | strict-eval | oracle bit-exact | LLC store_miss |
|---|---|---:|---:|---:|---:|
| W1 mt_compute_int 4×800 | ALU + 私有 | 330 497 | 100% (3 003) | 13/13 | 94.17% |
| W3 mt_coh_stress 4×800 | 全 phase L1/L2/LLC/DRAM/R_DIRTY/R_CLEAN/WB | 1 976 577 | 100% (62 683) | 13/13 | 97.73% |
| W4 mt_micro_coh 4×600 | plain volatile false-sharing / ping-pong | 181 200 | 100% (7 700) | 13/13 | 91.11% |
| W7 mt_chase_dram 4×1500 | DRAM pointer-chase 长尾 | 3 971 658 | 100% (235 631) | 13/13 | 99.96% |
| **合计** | | **6 459 932** | **100% (308 017)** | **52/52** | — |

W5（mt_microbench）在 gem5 SE+Ruby 下 pthread_cond_wait 唤醒不可靠，已删除。

### 1.3 已确认的遗留问题（diagnosis 报告完整定位）

详见 `diagnosis/pmu_diagnosis.md`，核心结论：

| 现象 | 根因 | 性质 |
|---|---|---|
| W1 LLC load_misses acc=13.92% | startup-phase i-cache fetch + TLB page walker 请求只进 ruby、不进 tao_trace probe；page walker port 直连 sequencer | gem5 配置 / probe 覆盖范围 |
| W4 LLC load_misses acc=40.03% | 同上（gap=767，与 W1 ~750 同量级） | 同上 |
| W7 LLC load_misses acc=−3323% | (1) oracle 缺 MSHR coalescing；(2) oracle L3 LRU 是单一 32768 行全相联，与 ruby 4 bank × 16-way set 的真实 L3 不对应 | oracle 模型过简化 |
| DIR_LOOKUP.SNP / CORE_SNP.ANY_ONE 66%–87% | oracle 简化 4-state MESI 目录 vs ruby MESI_Three_Level 在 fan-out 计数语义 / L2 私有层吸收 / transient race 上差异 | 已声明为"近似指标"，非 bug |
| ROI 不切分 | run_mt_mvp.py 没用 m5_work_begin / reset_stats，启动期 i-fetch / walker miss 全计入 | gem5 配置 |
| ruby prefetcher 实测全 0 | numPrefetchRequested = 0 across all 4 workloads | 已确认非误差源 |

---

## 2. 备份内容清单

`backups/v9_5_pre_a2_a3_20260526/`

```
.
├── PROGRESS_SUMMARY.md         (本文件)
├── code/                       (16 个关键源文件)
│   ├── tao_trace.cc / .hh      gem5 detailed probe (oracle 训练侧实例)
│   ├── atomic_func_trace.cc    atomic probe (部署侧用，训练不用)
│   ├── lsq_unit.cc             V9.4 completeTick 修复
│   ├── base.hh                 V9.4 _traceRoiActive
│   ├── simulator.hpp           ref_sim oracle 部署侧实例
│   ├── ref_sim_main.cc         ref_sim 主入口
│   ├── pmu_report.py           PMU 三方对账脚本
│   ├── compare_oracle.py       Oracle bit-exact 验证
│   ├── build_micro_dataset.py  V9.5 单源投影 builder
│   ├── check_micro_alignment.py
│   ├── run_mt_mvp.py           4 核 detailed 仿真 driver
│   └── mt_*.c                  4 个 workload 源代码
├── datasets/                   (4.8 GB；4 个 workload 完整训练样本)
│   ├── w1_compute_int_4x800.samples.jsonl    250 MB
│   ├── w3_coh_stress_4x800.samples.jsonl     1.5 GB
│   ├── w4_micro_coh_4x600.samples.jsonl      134 MB
│   └── w7_chase_dram_4x1500.samples.jsonl    3.0 GB
├── m5out/                      (gem5 仿真真值)
│   ├── w{1,3,4,7}_stats.txt    Ruby controller 全部计数
│   └── w{1,3,4,7}_config.ini   gem5 配置快照
├── docs/
│   └── v2_to_v5_alignment_log.md   V2→V9.5 完整设计演进
└── diagnosis/
    └── pmu_diagnosis.md            PMU 误差根因分析
```

总占用：~5 GB。

### 飞书文档（在线版本，未在本备份内）

- 终版方案文档：https://bytedance.larkoffice.com/docx/PqRWdsGQpoBTsyxWhMocTw2Vn0f
- 原 GRBxdf 文档（待对齐，但本轮不再修改）：https://bytedance.larkoffice.com/docx/GRBxdfUypohqWuxz5FFcoJ6WnNh

---

## 3. 下一步计划（A2 MSHR + A3 4-bank LRU + 方案 2 i-cache probe）

### 3.1 目标

修复 §1.3 的三类误差：

- **W1 / W4 LLC load_misses acc 13.92% / 40.03% → ≥98%**
- **W7 LLC load_misses acc −3323% → ≥98%**
- 补充 i-cache miss 一致性（新增字段）

### 3.2 设计要点

#### A2 — MSHR coalescing (oracle 内置)

在 tao_trace.cc 的 oracle 内维护一个 `outstanding_dram_misses: set<cacheline>`：
- demand load/store 第一次命中 DRAM 时入集，PMU 计 1 次 LLC miss
- 在该 cacheline 真正完成（mem_data 返回）时移出
- 简化版退化：无完成事件时按"窗口内只算一次"近似

实现位置：tao_trace.cc 内部 + ref_sim/simulator.hpp 同步。

#### A3 — 4-bank × 16-way set-associative LRU (替换 l3_lru_)

LruSet → BankedSetAssocLRU：
- 4 banks（与 ruby `num_l3_banks=4` 对齐）
- bank index = (cl >> bank_shift) & 0x3，bank_shift 与 ruby `l3_select_low_bit` 对齐
- 每 bank: 32768/4 = 8192 lines = 512 sets × 16 ways（与 ruby `assoc=16` 对齐）
- `touch(cl)` 在 (bank, set) 内部做 16-way LRU
- l1_lru_ / l2_lru_ 可选同步改造

实现位置：tao_trace.cc/hh 内部 + ref_sim/simulator.hpp 必须同源（共享头文件）。

#### 方案 2 — i-cache probe

- gem5 fetch stage 加 probe 监听器（监听 `Fetch.fetchedInsts` 或类似事件）
- 维护独立 i-cache LRU（容量与 ruby i-cache 对齐：32 KiB / 64B = 512 lines）
- 每条 instruction 写入 records.micro 新增字段：`i_path_class`, `i_coh_oracle`, `i_mesi_before`, `i_oracle_source`
- pmu_report 增加 `cache.i.load_misses` 计数项
- LLC load_misses 改为 d-side + i-side 之和

实现位置：tao_trace.cc/hh + ref_sim/simulator.hpp + records.micro schema + builder。

### 3.3 任务清单（完整 Plan）

#### Step 1: A3-pre 实测（baseline）— **0.5 天**

跑一个一次性脚本统计 W7 records.micro：
- oracle_source=0/1 的样本数与 path_class 分布
- coh_oracle 的实际分布（DRAM / LLC / L2 / L1 占比）
- 这是 A3 改动后判断分布迁移幅度的对照基线

#### Step 2: 设计与实现 A2 + A3 + 方案 2 — **2 天**

| 子任务 | 文件 | 行数估计 |
|---|---|---|
| BankedSetAssocLRU 类（共享头） | gem5/src/cpu/o3/probe/lru_banked.hh（新增）+ ref_sim/include 引用 | ~150 行 C++ |
| tao_trace.cc/hh 替换 l1/l2/l3_lru_ | tao_trace.cc + .hh | ~80 行修改 |
| MSHR outstanding 表（oracle + ref_sim 同源） | tao_trace.cc + simulator.hpp | ~50 行 |
| i-cache probe 注册 + i-cache LRU 维护 | tao_trace.cc + .hh | ~150 行 |
| records.micro schema 扩展（新增 4 字段） | tao_trace.cc + builder | ~30 行 |
| simulator.hpp / ref_sim main.cc 同源更新 | mesi_ref_sim | ~150 行 |
| pmu_report.py 增加 cache.i.* 项 + LLC d+i 求和 | pmu_report.py | ~30 行 |
| build_micro_dataset.py 拷贝新字段 | builder | ~10 行 |

#### Step 3: 重编 — **0.5 天**

- gem5 重编 X86_MESI_Three_Level（约 30-40 min）
- mesi_ref_sim cmake build（< 1 min）

#### Step 4: 重跑 4 workload — **1 天**

- detailed gem5 W1/W3/W4/W7（约 1 h 总计）
- builder 重新生成 4 份 samples.jsonl
- ref_sim replay
- compare_oracle bit-exact（必须仍然 13/13 + 4 个新增字段 → 至少 17/17）
- pmu_report 三方对账

#### Step 5: 输出修复后的一致性表 — **0.5 天**

- 5 张表（标签自洽 / Oracle↔probe bit-exact / PMU vs gem5 stats / path_class 分布 / 数据规模）
- 列对比 V9.5 修复前 vs A2+A3+方案2 修复后

#### Step 6: 文档更新（本地） — **0.5 天**

- backups 创建新目录 v9_5_post_a2_a3_<timestamp>
- 写 POST_FIX_SUMMARY.md 说明修复后状态
- 用户确认后再进飞书

**总工程量**：~4-5 天。

---

## 4. 风险与回退点

| 风险 | 缓解 |
|---|---|
| BankedSetAssocLRU 与 ruby 真实行为对齐困难 | 先用 ruby `config.ini` 确认 num_banks / assoc / set_count，让 oracle 同参数化 |
| oracle ↔ ref_sim bit-exact 破坏 | 通过共享头文件（lru_banked.hh）让两侧 #include 同一份实现 |
| records.micro schema 变更不兼容旧训练数据 | 加 schema_version=2 字段；backups 已留 v9_5 完整数据集 |
| W7 修复后 path_class 分布大幅变化 | Step 1 baseline 实测后，用对比表展示 |
| ROI 切分（B1）未做 → W1/W4 残余 i-fetch + walker 偏差仍存在 | 方案 2 加 i-cache probe 后，W1/W4 残余主要来自 page walker；可作为 V9.7 后续工作 |
| i-cache 真实命中行为与 oracle LRU 偏差 | 文档化为"i-cache 一致性目标 ≥95%"，与 d-cache ≥98% 区别对待 |

回退点：本次备份 `backups/v9_5_pre_a2_a3_20260526/` 完整保留 V9.5 状态，包括代码、4 GB 训练数据、stats.txt、文档、诊断报告。任何阶段失败都可整体回滚。
