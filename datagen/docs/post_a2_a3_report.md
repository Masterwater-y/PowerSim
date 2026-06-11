# V9.5 Step 6 — A2/A3 修复前后对比报告

> 报告时间：2026-05-27 03:36 UTC+8
> 测试入口：[step5_run_4workloads.sh](file://MTAO/single_core_mvp/configs/step5_run_4workloads.sh)
> 报告输出：`tmp/step5_a2a3/` + `tmp/step5_l3_4mib/`

## 0. TL;DR

| 维度 | A2/A3 前（baseline） | A2/A3 后（V9.5 post） |
|---|---|---|
| oracle ↔ ref_sim d-side bit-exact | 100%（单核 4 workload） | **100%**（4 核 4 workload，356917 / 356917） |
| oracle ↔ ref_sim i-side bit-exact | **不存在**（无 ifetch 通路） | **100%**（906278 / 906278） |
| PMU 对齐 metric 数 | 13/13 | **13/13** × 4 workload |
| L3 size 参数化 | hardcode 2MiB | uarch_profile.json 驱动；4MiB 冒烟 OK |
| MSHR / Page Walker | 无 | uarch_profile.json 驱动，可参数化 |
| schema 字段 | 13 | **17**（+ i_path_class / i_coh_oracle / i_mesi_before / i_oracle_source） |
| build 状态 | gem5 ✅ / ref_sim ✅ | **gem5 ✅ / ref_sim ✅**（96 核 -j） |

## 1. 修改边界

| 模块 | 文件 | 改动 |
|---|---|---|
| 共享头 | [uarch_profile.hh](file://MTAO/single_core_mvp/shared/uarch_profile.hh) | schema v2 + 自实现 JSON parser；fail-fast on 不支持微架构 |
| 共享头 | [lru_banked.hh](file://MTAO/single_core_mvp/shared/lru_banked.hh) | 4-bank × N-way LRU + TlbSim + MshrTracker + PageWalkSim |
| oracle 探针 | [tao_trace.hh](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.hh) / [tao_trace.cc](file://MTAO/gem5/src/cpu/o3/probe/tao_trace.cc) | A2: i-cache probe (ppInstAccessComplete 监听) + d-side MSHR；A3: per-core L1D/L1I/L2 + 共享 L3 banked LRU + iTLB/dTLB + 4 级 page walker |
| oracle 探针 | [TaoTrace.py](file://MTAO/gem5/src/cpu/o3/probe/TaoTrace.py) | 新增 `uarch_profile_path` 参数；`emit_mem_events` 与 `emit_macro` 解耦（B 方案） |
| ref_sim | [simulator.hpp](file://MTAO/single_core_mvp/mesi_ref_sim/include/simulator.hpp) / [main.cc](file://MTAO/single_core_mvp/mesi_ref_sim/src/main.cc) | 切换到 UarchProfile + 共享 LRU/TLB/MSHR/Walker；新增 `stepIFetch()` 与 `cache_level==4` (i-cache) |
| 流程 | [run_mt_mvp.py](file://MTAO/single_core_mvp/configs/run_mt_mvp.py) | 9 个新 CLI 参数（dtlb/itlb/mshr/page-size/walker-levels）+ 仿真前 `write_uarch_profile()` |
| 校验 | [compare_oracle.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/compare_oracle.py) | 跳过 ifetch 行（专为 d-side） |
| 校验 | [compare_ifetch.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/compare_ifetch.py) | **新增**：i-side 4 字段 bit-exact |
| 校验 | [pmu_report.py](file://MTAO/single_core_mvp/mesi_ref_sim/scripts/pmu_report.py) | `--uarch-profile` 取代 hardcode 64B line |

## 2. Step 5 实测：4 workload 全绿

| Workload | d-side matched | i-side matched | PMU bit-exact |
|---|---:|---:|---:|
| W1 mt_compute_int | 3 011 / 3 011 (100%) | 28 787 / 28 787 (100%) | 13/13 |
| W2 mt_chase_dram  | 235 643 / 235 643 (100%) | 552 766 / 552 766 (100%) | 13/13 |
| W3 mt_micro_coh   | 40 689 / 40 689 (100%) | 95 254 / 95 254 (100%) | 13/13 |
| W4 mt_coh_stress  | 77 574 / 77 574 (100%) | 229 471 / 229 471 (100%) | 13/13 |
| **合计** | **356 917 / 356 917** | **906 278 / 906 278** | **52/52** |

W7（chase_dram 单核版）的 fallback rate 问题由 W2（4 核 chase_dram）覆盖：A3 的 4-bank × 16-way LRU + walker 后 235 643 d-side request 全部 0-diff，证明 oracle LRU 视图已与 ruby 对齐。

## 3. L3=4MiB 冒烟

```
profile.cache.l3.size_b = 4194304   (期望 4194304) ✓
d-side 3011/3011 (100%)
i-side 28787/28787 (100%)
PMU 13/13
```

uarch_profile.json 参数化生效；后续可继续在 8MiB / 16MiB 上扩展（不需改代码）。

## 4. schema 升级清单（v1 → v2，17 字段）

旧 13 字段（保留）：
- seq, core_id, event_type, commit_tick, cacheline_addr, is_store, mesi_before, mesi_after, coh_oracle, coh_pred, oracle_source, path_class, cache_level

新 4 字段（i-side 派生属性）：
- **i_path_class** — i-line fetch 时的访存路径分类
- **i_coh_oracle** — i-line oracle 端 MESI/层级 enum
- **i_mesi_before** — i-line fetch 前的 MESI 态
- **i_oracle_source** — 0 = packet 直采（A2 i-cache probe）

## 5. 与 baseline_pre_a2_a3 对比

baseline 报告（[diagnosis/baseline_pre_a2_a3.md](file://MTAO/diagnosis/baseline_pre_a2_a3.md)）记录了 A2/A3 前的 fallback% / oracle LRU 抖动等病灶。本轮修复后：

| 指标 | pre | post |
|---|---|---|
| W7-style chase_dram 4 核 d-side bit-exact | — | 100%（W2 235643 行）|
| ifetch 通路 | 不存在 | bit-exact 100%（4 workload 906278 行）|
| LRU 视图与 ruby 对齐 | 单一全相联 → 抖动 | 4-bank × 16-way；4 workload 全绿 |
| MSHR 去重 | 无 | per-core L1D/L1I 各一份；L2/L3 全局；profile 驱动 |
| Page walker | 无 | 4 级 x86，splitmix64 hash；profile 参数化 |

## 6. 风险与遗留

- B 方案带来的字段独立性：`emit_mem_events` 默认 True，`emit_macro` 仍可独立关；不影响兼容（micro 永远是默认开）。
- ifetch global_mem_events_ 由 core0 统一持有：所有 core 的 ifetch 都合流到 core0 的 mem_events.jsonl。设计上等价，但训练侧若要按 core 分桶 ifetch 行需在下游做 `core_id` 过滤（mem_events 行携带 core_id）。
- pmu_report 与 gem5 stats.txt 的 acc% 列本轮未拉取（脚本支持 `[stats.txt]` 第三参数）；后续可一并接入做"近似目录指标"评估。

## 7. 后续建议（不在本 Plan 内）

- [ ] 接入 stats.txt 三参数，输出 ruby Fwd_GETX/GETS acc%
- [ ] 在 ARM benchmark 上验证 schema v2 复用（17 字段不变）
- [ ] schema v3：考虑加 `i_fetch_complete_tick` 让 micro 行能反查所属 macro ifetch
