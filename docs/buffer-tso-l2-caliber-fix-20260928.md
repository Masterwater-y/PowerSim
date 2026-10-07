# buffer-114 L2/LLC tag_miss caliber alignment (20260928)

## Root cause (already documented)

gem5 SLICC 在 buffer-114 下把 buffered-TSO permission-prepare（`pc_issuePfUPGRADE` /
`pb_issuePfGETX`，通过 L0Cache 的 prefetch queue 下发，不携带 `TraceValid`）与 demand
tag miss 记入完全不同的 native 计数：

- `hierarchy.l2.tag_misses` = 仅 demand `uu_profileMiss`（携带 `TraceValid`）
- 承诺升级（permission upgrade）走 `permissionUpgrades`
- follower 合并进 `mergedMisses`
- 远端 owner 供给进 `remoteSupplies`

FastSim 之前把 `SetAssociativeCache::probe.misses` 直接当作 `l2_tag_misses` 上报，
在 buffer-114 下 permission-prepare 侵入这个字段，导致 LBM C4 系统性 3.96× 高估。

## Fix delivered

### Timing 路径不变

`SetAssociativeCache::probe_indexed` 的 `++counters.misses` 保持不变。桶字段只是对
`CacheCounters.misses` 总量的 reporting-caliber 切片。

### 五桶分类

`include/fastsim/types.hpp` 在 `CoreCounters` 上新增：
`l2_read_tag_misses`、`l2_write_demand_misses`、`l2_permission_upgrades`、
`l2_follower_misses`、`l2_remote_supply_misses` 与对应 `llc_*`，`operator+=` 同步汇总。

### 分类点

`replay_previewed_memory_event`（`src/simulator.cpp:22665` 附近）在完成 shared-hop
之后按 `preview.private_level` + `preview.buffered_store_prepare` + `shared_timing.path`
+ `event.write||event.atomic` 分入五桶。放这里的原因：single-pass 与 reweave
`replay_shared` lambda 都会经过 `replay_previewed_memory_event`，覆盖两条 timing
路径且仅入桶一次。reweave 回滚由 `restore_counters` 复原 `cores_[core]->total`，桶
字段随之一并回滚。

### JSON 报送

`write_user_functional_pmu` / `write_user_plus_kernel_pmu` / `dump_totals` 三处已
将 `private_l2_tag_misses` 与 `llc_tag_misses` 报为 read-only bucket，同时暴露五
桶细分作 diagnostic。validator（`tools/run_native_kernel_fastsim_validation.py`）
的 `PMU_EVENT_NAMES` 早已使用 `private_l2_tag_misses`，无需修改。

## LBM C4 verification

| 指标 | FastSim | native truth | 偏差 |
|---|---|---|---|
| `private_l2_tag_misses` | 142,355 | `l2.tag_misses = 142,961` | −0.42% |
| `llc_tag_misses` | 141,281 | `llc.tag_misses = 141,939` | −0.46% |
| `l2_permission_upgrades` | 419,019 | gem5 raw `NP.L1_GETX = 423,991` | −1.17% |
| L2 桶总和 | 566,501 | `l2.misses = 566,501` | 完全守恒 |
| LLC 桶总和 | 565,316 | `llc.misses = 565,195` | +0.02% |
| CPI | 0.7728 | 0.8173 | −5.5% |

- `fastsim_tests` 全通过
- validator status=`passed`（单案例 rescore，2026-09-28）
- l1d_tag_misses 仍差 +178%（是 L1D 层的等价 caliber 问题，不在本轮 scope，另立
  followup）

## Guardrails

- `SetAssociativeCache` timing 与 `l2.misses` 总量与修复前 bit-identical
- 桶字段只加不减：`operator+=` 已加入 5+5 项
- reweave 回滚路径通过 `restore_counters`（`src/simulator.cpp:21688` 等）已经复原
  `cores_[core]->total`，桶字段自动跟随
- validator 仍走 `private_l2_tag_misses` / `llc_tag_misses` 名字，接口不破坏

## Follow-ups

- L1D 同类 caliber 修复（`l1d_read_tag_misses` 五桶）
- 全 38 案例 `first-core-target-common-end-v1` 全矩阵重跑，导出 CPI/PMU 报告
- 更新 `docs/accuracy-reporting-contract.md` 里的 tag_miss 定义指向本文档
