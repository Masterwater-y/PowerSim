# buffer-114 line-alias 抑制的 preview 阻抗 —— 首轮修复负向证据

日期：2026-09-28。前置文档：
[buffer-tso CPI 诊断下一步](buffer-tso-cpi-diagnosis-next-steps-20260924.md)、
[混合式 line coalescing](hybrid-bound-line-coalescing-design-20260924.md)、
[固定读服务 Round1 结果](hybrid-fixed-read-services-results-20260924.md)。

## 已确认的根因

buffer-114 下 gem5 会把父 LLC miss 在途期间到达的同行请求合并到同一次 miss
（native 服务时长聚于 227 周期新鲜 miss / 142 周期在途接管两档）。FastSim 当前
生产路径把这些 follower 都当作独立请求下发到 private L2 / LLC / DRAM。次生
问题：`ruby.buffer_line_coalescing` 路径实现了 sequencer line-alias 表，但被关
闭，因为启用后遇到同核连续 write 在旧 write callback 前几周期到达时，
`track_sequencer_parent` 抛 `new Ruby parent collided`。

## 首轮 collision-safe attach（2026-09-28）

`src/simulator.cpp:22228` `track_sequencer_parent` 由 throw 改为原地 attach：
读跟随保留旧父不动；写跟随若父仍是读则升级 `parent_write=true`、
`dirty_on_response=true`、按 L1 回调边推移 `callback_cycle`、`++generation`
并新推 `SequencerLineExpiry`；写跟随若父已是写则静默 no-op。call site
在 `replay_memory_event` (`src/simulator.cpp:22464`) 已经把 transaction 透传。

`cmake --build build -- -j16` 与 `./build/fastsim_tests` 均通过（`all FastSim
tests passed`）。

## 单例验证结果（buffer-line-coalescing-verify-20260928）

配置链：`tmp/buffer-line-coalescing-verify-20260928/0.cfg → tmp/buffer-tso-
diagnosis-20260924/configs/1.cfg → 9.cfg`，唯一 override 是
`ruby.buffer_line_coalescing = true`；对照来自
`tmp/buffer-tso-diagnosis-20260924/baseline/`（同 config 树，coalescing 关闭）。

### LBM C4（uarch-baseline-c04-782.lbm_r）

| 指标 | 基线（关） | Coalescing 开 | Δ |
|---|---|---|---|
| `cpi` | 0.611500 | 0.851893 | +0.240393 |
| `perf_like_cpi` | 0.7768 | 1.0822 | +0.3054 |
| `ruby_sequencer_stall_cycles` | 2934201 | 115150296 | +112.2M |
| `ruby_sequencer_coalesced_requests` | 0 | 659903 | +660K |
| `ruby_sequencer_write_aliases` | 0 | 14665 | +14665 |
| `ruby_sequencer_reissued_writes` | 0 | 261 | +261 |
| `ruby_sequencer_reissued_write_permission_conflicts` | 0 | 261 | +261 |
| `ruby_sequencer_max_line_table_entries` | 0 | 57 | +57 |
| `l2_misses` | 566502 | 566499 | −3 |
| `llc_misses` | 565195 | 565195 | 0 |
| `buffer_tso_prepares` | 3785579 | 1805520 | −1980059 |
| `buffer_tso_merged_demands` | 169723 | 215872 | +46149 |

gem5 truth（LBM C4）：`l1d_tag_misses` 334367、`private_l2` 142961、`llc` 141939。
Coalescing 开／关下 FastSim 的 L2/LLC miss 仍在 566K/565K 量级，与 gem5 差 ~4×。

### NAMD C4（uarch-baseline-c04-857.namd_s）

| 指标 | 基线（关） | Coalescing 开 | Δ |
|---|---|---|---|
| `cpi` | 0.275485 | 0.480514 | +0.205029 |
| `perf_like_cpi` | 0.389645 | 0.679638 | +0.289993 |
| `ruby_sequencer_stall_cycles` | 85444 | 5770612 | +5.69M |
| `ruby_sequencer_coalesced_requests` | 0 | 108408 | +108K |
| `ruby_sequencer_write_aliases` | 0 | 62620 | +62620 |
| `ruby_sequencer_reissued_writes` | 0 | 6026 | +6026 |
| `ruby_sequencer_reissued_write_permission_conflicts` | 0 | 384 | +384 |
| `ruby_sequencer_max_line_table_entries` | 0 | 36 | +36 |
| `l2_misses` | 20181 | 20205 | +24 |
| `llc_misses` | 18066 | 18066 | 0 |
| `buffer_tso_prepares` | 1576085 | 18446744073709375481 | 下溢（−200604） |
| `buffer_tso_merged_demands` | 1874 | 1666 | −208 |

`buffer_tso_prepares` 出现 2^64 级下溢：published 分支
（`src/simulator.cpp:7220`）无条件 `--buffer_tso_prepares`，而 aliased 写
未走 `replay_previewed_memory_event` 中的 `++buffer_tso_prepares`
（`src/simulator.cpp:22456`），导致减少多于增加。

## 判断：修复必要，但结构上不充分

collision-safe attach 让 `ruby.buffer_line_coalescing = true` 能跑通不再
抛错，`coalesced_requests` 反映出 follower 数量（LBM 66 万、NAMD 10 万）。
但两案 CPI 都回归、并且 miss 计数没有变。分离出的机制原因：

- **preview 阶段先记账 miss**。`preview_memory_event`（`src/simulator.cpp:7696`）
  在每个 event 上都调用 `private_caches_[core]->access(...)`。private
  cache 的 miss 计数在 `SetAssociativeCache::probe_indexed` 里以
  `++counters.misses`（`src/cache.cpp:272`）递增。alias 短路只在
  `replay_memory_event` 顶部（`src/simulator.cpp:22420`），此时 follower
  已经在 preview 里递增过 L2/LLC miss。观察到的 Δ ≈ 0 与此一致。
- **write-over-read 升级新增串行化**。原地 upgrade 会推移 `callback_cycle`
  并新推 expiry，`sequencer_ready` 序列同时也没有降低 admission 竞争，
  于是 `sequencer_counters.stall_cycles`（`src/simulator.cpp:11426`,
  `14147`, `15175`）大幅上升，直接推 CPI。
- **`buffer_tso_prepares` 记账不对称**。aliased 写路径吞掉了预备递增，
  但 publish 阶段仍然递减，出现下溢。修复要么让 alias 覆盖时保留
  `permission_only` 记账、要么改成 owner-key 计数。

## 决策：回退 collision-safe attach，等待结构性重构

首轮 attach 修复的目的是替换非致命性的 throw，避免生产禁用。但从上表看：

- **miss 计数并没有向 gem5 truth 收敛**（LBM C4：566K vs 143K，Δ ≈ 0）。这是
  根因给出的收益指标——「followers 不应下发到 private L2/LLC/DRAM」。
  当前 fix 在这一维度**没有效果**。
- **CPI 双向变差**（LBM +0.24，NAMD +0.21），MAE 相对 buffer-114 baseline
  0.058 而言是灾难级。
- **计数一致性遭破坏**（`buffer_tso_prepares` 下溢）。

按项目「精度回退非小」的门槛，不合并当前 attach。改为：

1. 回退 `src/simulator.cpp:22228` 的原地 attach，把 write-over-read /
   collision 分支恢复为 throw 或静默 skip，保持
   `ruby.buffer_line_coalescing = false` 是唯一生产默认；不改变
   [Round1 结果](hybrid-fixed-read-services-results-20260924.md) 里
   已记录的既有行为。
2. 下一轮从 preview 层入手，参考
   [混合式 line coalescing](hybrid-bound-line-coalescing-design-20260924.md)
   §5.1「程序序 pass」：在 preview 之前维护一张 per-core
   `line_state{data_avail_cycle, present}`，follower 命中就跳过
   `private_caches_[core]->access` 与 `shared_->access`，共享父的响应
   周期与延迟。这才是能压缩 miss 计数的路径。
3. 同一轮补上 `buffer_tso_prepares` 的对称记账：只在
   permission-only 路径原路 ±1，aliased 写路径按 owner 归并。

在此之前，`ruby.buffer_line_coalescing` 保持默认关闭；LBM C4 的 buffer-114
baseline（CPI MAPE 5.16%、绝对误差 0.042）仍是当前上限。

## A/B/C 上界测量（2026-09-28，同日追加）

为避免拍脑袋选修法，先加了三个只读诊断计数器
（`preview_line_alias_intra_core_candidates` / `cross_core_candidates` /
`no_live_parent`），并配对一个持久化 shadow 表
（`preview_first_core_by_line_` + `preview_seen_lines_per_core_`）。

- **A（同核已见）**：命中曾在**本核**上出现过的 line —— sequencer 层
  line-alias 表能抑制的最大集合。
- **B（跨核已见）**：只在**其它核**上出现过 —— LLC MSHR 合并才能抑制。
- **C（no live parent）**：这是本 line 在**本次仿真中的首次出现**。

计数点：`run_private_preview` 返回后（`src/simulator.cpp` 内的
`classify_preview_line_alias_candidates`），只读遍历一次 batch。不修改
miss 计数、不修改 timing。

### LBM C4 结果（`tmp/buffer-line-coalescing-verify-20260928/abc-probe/`）

| 指标 | 值 |
|---|---|
| FastSim `l1d_misses` | 931,046 |
| FastSim `l2_misses` | 566,501 |
| FastSim `llc_misses` | 565,195 |
| gem5 truth `private_l2` | 142,961 |
| gem5 truth `llc` | 141,939 |
| **A** intra-core（其中写 204,616 / 读 317） | **204,933** |
| **B** cross-core（全部为读） | **66** |
| **C** no-live-parent（首次见 line） | **444,201** |
| Unique physical lines（persistent） | **461,467** |
| A+B+C（唯一的可分类 miss-trigger 数） | 649,200 |

Δ<sub>l2</sub> = 566,501 − 142,961 = 423,540。

- `A/Δ = 0.484`
- `B/Δ = 0.000`
- `(A+B)/Δ = 0.484`

### 理论上界结论

**preview-time line-alias 表最多抑制约 48% 的过量 miss**。就算 A+B 全部
折叠掉，FastSim 的独立唯一 line 数（461,467）仍是 gem5 truth（142,961）
的 **3.23×**。也就是说：即使一次 miss 都不重复，FastSim 仍然会记 461K
私有 L2 miss，与 gem5 差 3×。这否定了「buffer-114 未建模在途合并」是
Δ 的**充分**解释；它只解释一半。

### 修正后的机制假设

剩下的 52% Δ 不属于「同 line 的 follower 到达 in-flight 窗口」，而属于
「同一 line 在缓存驻留期结束后被再次冷 miss」。可能来源（各自需要单独
证据）：

- **A 中的 store followers 抢占 L2/LLC LRU way**：本次未合并的 store
  followers 通过 `SetAssociativeCache::install` 挤掉了本可复用的父 line；
  修 line-alias 的**主要收益不是省 miss 记账，而是恢复 gem5 一致的
  驻留期**。这需要落地 α 后再测。
- **Prepare-only 与 demand 的 install 路径分裂**：buffer-114 下 permission
  prepare 走 `access(line, write=false)` (`src/simulator.cpp:22444` 里
  的 `event.write && !permission_only`)，随后 demand 走 write 语义。
  两次触发本身可能已经把 miss 计数翻倍。需要单独统计 prepare-hit /
  prepare-miss / demand-hit / demand-miss 才能定量。
- **私有 cache geometry 与 gem5 mismatch**：容量/associativity 若与
  gem5 不一致，同一 working set 的 replacement 频次自然更高。属于配置
  一致性问题。

### 修法路线（按上界与残差重排）

α 与 β 收益都被这次上界测量削弱：

- **α（preview 前 per-core line-alias）**：直接 miss 抑制上界 48%；额外
  的 LRU-restore 收益需要落地后再测。是**必要的**（尤其对 write-heavy
  的 LBM），但**不充分**。
- **β（LLC MSHR 合并）**：LBM C4 里 B ≈ 0.02%（读 66 次，写 0 次），
  收益可忽略。**不作为首选修法**。
- **γ（prepare/demand 记账合并）**：先加计数器测量。若 prepare-miss +
  demand-miss 在同 line 上重复触发大幅存在，此项优先级高于 β。
- **δ（cache geometry review）**：与 gem5 config 逐项对齐 L1D/L2/LLC 的
  size、associativity、replacement policy；这是配置事项，不是模型改动。

### 下一步

先测量 prepare-vs-demand 二次触发（γ 的前置度量），并复核 cache
geometry 与 gem5 配置的差异（δ 的前置度量）。这两个证据决定了 α 应该
单独落地还是必须与 γ/δ 配套；单独落 α 不能把 miss 计数带到 gem5 水平。

## 可复核产物

- 基线：`tmp/buffer-tso-diagnosis-20260924/baseline/cases/`
- Coalescing 开：`tmp/buffer-line-coalescing-verify-20260928/cases/`
- 配置链：`tmp/buffer-line-coalescing-verify-20260928/0.cfg`
- A/B/C probe：`tmp/buffer-line-coalescing-verify-20260928/abc-probe/lbm-c4-with-unique.json`
- 单元测试：`./build/fastsim_tests`（本次通过）

## 2026-09-28 追加：口径不匹配是主要机制

在 A/B/C 之外再加入 per-core unique-line、prepare/demand 分类
（`preview_line_alias_prepare_events` / `demand_events` /
`prepare_unique_lines` / `demand_unique_lines` /
`prepare_first_miss` / `demand_first_miss` /
`prepare_then_demand` / `demand_then_prepare`）以及
`preview_line_alias_unique_lines_per_core`。

### LBM C4 结果（`tmp/buffer-line-coalescing-verify-20260928/percore-probe/`）

| 指标 | 值 |
|---|---|
| `preview_line_alias_prepare_events` | 550,395 |
| `preview_line_alias_demand_events` | 98,805 |
| `preview_line_alias_prepare_unique_lines` | 345,790 |
| `preview_line_alias_demand_unique_lines` | 98,433 |
| `preview_line_alias_prepare_first_miss` | 345,779 |
| `preview_line_alias_demand_first_miss` | 98,422 |
| `preview_line_alias_prepare_then_demand` | 11 |
| `preview_line_alias_demand_then_prepare` | 11 |
| `unique_lines_per_core` | 128,137 / 103,729 / 105,469 / 106,932 |
| 全体 unique lines | 461,467 |

### gem5 侧口径证据

`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/uarch-baseline-c04-782.lbm_r/stats.txt`：

- `L2Cache_Controller.NP.L1_GETS::total = 142,858`（read miss）
- `L2Cache_Controller.NP.L1_GETX::total = 423,991`（write/RWITM miss）
- 合计 = **566,849** ≈ FastSim `l2_misses` 566,501（差 0.06%）
- `l2_controllersN.cache.m_demand_misses` 合计 = **572,885**
- taotrace-path-class-v3 per-core `private_l2_tag_misses` 合计 = **565,967**（同 FastSim 视角）
- native SLICC `hierarchy.l2.tag_misses = 142,961`（本仓库 native validator 的
  truth 源）
- validation.json 的 `gem5_semantics`：
  `"native v5: MESI_Three_Level-L1cache uu_profileMiss action; permission upgrades are counted separately"`

### 决定性 SLICC 证据

`gem5-collection/patches/0001-fastsim-collection-upstream-hooks.patch` 显示，
本 fork 明确把原 upstream 会调用 `uu_profileMiss` 的四条写/合并/远端补给路径
拆成了独立 action：

- `uu_profileUpgrade`（原 S/SS + Store：写 upgrade，不再进 tag_miss）
- `uu_profileMergedMiss`（原 IS/ISS + L1_GETS：同 line 后续 read follower，走
  合并档，不再进 tag_miss）
- `uu_profileRemoteSupply`（原 MT + L1_GETX / L1_GETS：跨 core 远端补给，不
  再进 tag_miss）

也就是说 native SLICC 里 `l2.tag_misses` 的定义是：
**新鲜 cold demand miss 且非 write upgrade / 非同 line follower / 非远端补给**。

用这个口径重算 gem5：`NP.L1_GETS = 142,858` ≈ 142,961，能对上。
`NP.L1_GETX = 423,991` 属于 write demand miss，在这个口径下**不进
tag_miss**（对应 SLICC 里 `uu_profileUpgrade` 的分离）。

### 结论

前文假设的"同 line 被多次冷 miss"**不是**真实机制。真实机制是**口径不
匹配**：

- FastSim 的 `l2_misses` 统计**混合了** read tag miss、write demand miss、
  permission upgrade、follower、remote supply 五种。
- gem5 stats 里同口径统计是 `NP.L1_GETS + NP.L1_GETX = 566,849`，与 FastSim
  一致。
- gem5 native SLICC `hierarchy.l2.tag_misses = 142,961` 只算第一种（cold
  read demand miss）。

`preview_line_alias_prepare_events = 550,395` 与 `NP.L1_GETX = 423,991` +
merged 66 + demand 92,000 相符，这些**在 gem5 truth 定义下不应该进
tag_miss**。

### 收敛修法（读-only tag_miss 口径）

真正让 FastSim l2_misses 收敛到 143K 的必要且充分改动，是**在 FastSim 侧
把 tag_miss 计数拆成 read tag_miss / write demand miss / follower /
remote-supply / permission-upgrade 五类**，并让**上报到 native validator 的
`l2_misses` 字段只用第一类**。这既不是模型精度问题，也不是 buffer
coalescing 建模问题，而是 accounting caliber 对齐。理论上：

- 新的 read-only tag_miss 应 ≈ FastSim `demand_first_miss` 98,422 ×
  (native L1D read miss ratio / demand-only ratio)；数量级与 gem5 truth
  142,858 相同。
- 修正后 A/B 抑制的意义变小（因为 A 里 204,616 是写，B 里都是读），α/β
  的收益让位于口径重分类。
- CPI 侧 timing 与 miss 计数解耦。timing 仍走 566K 完整事件，只是**上报
  口径**改为 native-SLICC-equivalent 的 read-only tag_miss，才与 truth 一致。

α（preview line-alias）依然对 timing 有意义（省重复的 L2 access latency）；
但**不需要**为了 miss 计数收敛而落地。

### 下一步（不改代码，等确认设计）

1. 在 FastSim replay 层加 5 类分裂计数器（`l2_read_tag_misses`
   / `l2_write_demand` / `l2_followers` / `l2_remote_supplies`
   / `l2_permission_upgrades`），先诊断，不上报。
2. 全 workload 复核 `l2_read_tag_misses` 与 gem5 `NP.L1_GETS` 及 native
   `l2.tag_misses` 的一致性。
3. 若一致性 &lt; 5% MAPE：把 native validator 的 `l2_misses` 上报字段切换到
   `l2_read_tag_misses`，同时保留 `l2_misses`（旧口径）作为诊断。这不是
   模型改动，是 accounting 对齐，属于 reporting-contract 层。
