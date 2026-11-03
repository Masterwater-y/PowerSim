# buffer-TSO prepare 吞吐回退：修复方案 A / B（2026-09-24）

本文记录 buffer-TSO prepare-lifecycle 导致约 2× 吞吐回退的根因，以及两个候选
修复方案。用户决定**先实施方案 B 并验证**；方案 A 留作对照与后续备选。

## 背景与根因

- store forwarding 默认开启的全矩阵复核（见
  [store-forwarding-default-on-fullmatrix-20260924.md](store-forwarding-default-on-fullmatrix-20260924.md)）
  发现当前 buffer-114 路径吞吐只有约 2.2–3.4 M uops/s，而历史 serial 路径约 6M。
- 消融定位：主因是 `core.buffer_tso_prepare_lifecycle`（非 store forwarding，
  ON/OFF 吞吐几乎相同）。prepare 开启时 `domain_phase` 从约 3.0s 掉到 **0**，
  单线程 `interval_weave` 从约 3.9s 涨到约 10.6s。

机制：
- prepare 阶段提前只取写权限、不置脏；store 排到 FIFO 队首 publish 时才置脏。
- `PrivateHierarchy::mark_dirty()` 只改 L1（`src/cache.cpp:615`）。
- 若该行在 prepare 之后被降级到 L2-only，`publish_buffered_store()` 按
  `contains()`（L1‖L2）放行，但置脏无法落到 L2 → 静默非脏行（正确性 bug）。
- 现有 fail-closed 修法：安全证书要求 prepared store 在 publish 时**必须 L1
  驻留**（`src/simulator.cpp:7116-7125`）。一旦不满足，**整个 epoch 回退**到
  协调器单线程串行 replay。这压制了 domain-worker offload，是吞吐回退来源。

不能简单关掉 prepare：全矩阵配对（38 例）证明 prepare-OFF 使
CPI MAPE 7.113%→7.581%、MAE 0.0584→0.0619，30/38 例变差，ZSTD 每点约 +2.7pp。
因此修复必须**保留 prepare 的时序精度，只放开导致串行化的证书**。

三级 cache miss 在 prepare ON/OFF 下差异很小（NAMD c04 单例：L1D 差约 0.25%、
L2 差 23、LLC 相同），但非严格为零，故方案仍需全矩阵核对 miss。

## 方案 A（保留全部脏语义，推荐；暂不实施）

把 "L2-resident 的 prepared store" 在证书 / publish 中正确建模为一次
**私有 L2 命中的 head-demand 写**：L2 hit → 回填 L1 并在 L1 置脏，再发布。
- 复用已有 head-demand 时序路径；脏语义、未来 miss 数均正确。
- domain offload 恢复，且不把已发布脏行留在 L2。
- 改动集中在 preview 证书判定 + publish 路径，不动延迟常数。
- 成本：实现更复杂，需要处理 L2→L1 回填及其对替换状态的影响。

## 方案 B（置脏落到实际驻留层；先实施）

让置脏在**实际驻留层**完成，并把证书从 "L1 驻留" 退回 "私有 cache contains"：
- `PrivateHierarchy` 增加按实际驻留层置脏的能力（L1 命中改 L1，否则 L2）。
- `complete_sequencer_write` / publish 路径用该层级置脏。
- `buffered_tso_permissions_intact` 不再因 prepare 而要求 L1 驻留，统一用
  `contains()`；epoch 不再因 L2-only 而整段回退，domain offload 恢复。
- `replay_buffered_tso_head_demand` 的 L1 门相应放宽到 `contains()`（语义：
  demand 可在 L1 或 L2 命中完成）。

语义代价：已发布的脏行可能停留在 L2（而非强制回填 L1）。这与真实硬件
"写在最近拥有副本层完成"更接近，但会轻微影响后续替换 / miss，需全矩阵确认
三级 cache miss 仍对齐、CPI 不劣化。

## 验收标准（方案 B）

- `cmake --build build -- -j16` 与 `./build/fastsim_tests` 通过，Python 转发测试 OK。
- 全矩阵 38 例（c08 NAMD 采集缺口除外）：
  - CPI MAPE / MAE 不劣于 prepare-ON 基线（7.113% / 0.0584）；
  - L1D / priv-L2 / LLC tag miss 与现基线对齐（报告逐负载）；
  - 硬守恒不变量（retired inst/uops、memory_uops、branches）保持。
- 对称吞吐：墙钟 ON/OFF≈1，绝对吞吐恢复到约 6 M uops/s 量级，报告噪声。

## 现状

- 2026-09-24：方案 B 实施、全矩阵验证完成，并行 timing feedback 成为唯一
  路径。`sim.interval_parallel_feedback` 选项（config 字段、解析、CLI、
  JSON dump、相关 cfg/tool）已整体删除，多核时恒走 domain 并行；
  单核或单 active core 时仍在协调器执行以避免空唤醒整个 worker 池。

### 代码改动

除前述置脏/证书改法外，根因中的串行点在
`compute_timing_feedback`：原门
`interval_parallel_feedback && !demand_transaction && active_cores.size()>1`
在 prepare 开启时因必有 `demand_transaction` 而恒走协调器串行 replay。
现已删除 `!demand_transaction`，prepare 下也走
`run_parallel_timing_feedback`，各 worker 在共享候选 Transaction 内只写
`private_caches[core]` 与 `cores_[core]`。隔离论证：

- timing feedback impl 全函数体内唯一的功能性 cache 写就是 role==3 的
  head demand（普通 load 仅观察、不回放），且 head demand 经证书保证
  kLocal、owner==core，只触及按核索引的私有分片。
- 唯一跨 worker 的结构写是 `snapshot_directory()` 向同一
  `Transaction.directory_before`（unordered_map）emplace，已新增
  `SharedSystem::transaction_directory_mutex_` 保护；directory 元素本身
  在各核拥有的不同 line 上改写，互不相交，且该候选 Transaction 在
  feedback 结束后整体 restore（不提交）。

### 全矩阵精度（38 例，gate `first-core-target-common-end-v1`）

- CPI **MAPE 7.114%**、**MAE 0.0584** cyc/inst —— 与修复前 bit-identical，
  无精度劣化（c08 NAMD 仍因采集缺口 failed，与本次改动无关）。
- PMU 与修复前完全一致：branch 3.58%、L1D 30.50%、priv-L2 77.56%、
  LLC 170.45%。
- 硬守恒 38/38。
- 删除选项前的并行/串行输出逐字段比对：CPI/cycles/PMU/退休 inst/uops
  全部 bit-identical，差异仅在并行路径计账字段与吞吐字段。

### 为什么 cache-miss PMU 不像 serial-32 旧报告那样 <5%

- 旧 <5%（`docs/private-l2-pmu-source-repair-20260916.md`：L1D 2.4%、
  L2 3.4%、LLC 4.5%）同样是 native Ruby/SLICC、user-plus-kernel、MAPE，
  但参考的 gem5 几何是 **serial-32**（SQEntries=32/needsTSO），非当前
  buffer-114。
- 受控对照（`docs/buffer-tso-store-timing-20260923.md` §8.10）：当前二进制
  跑旧 serial-32 数据仍复现 ~2%，故**二进制未回归**；同 38 例对 buffer-114
  即为 30.5/77.6/170.5%。差异不是换源、不是换 scope，而是 buffer-114 下
  gem5 把"父 miss 在途时到达的同行访问"coalesce 成一次 miss，FastSim 当前
  （line-alias coalescing 关闭）把每个 follower 都计成新请求，与 CPI 低估
  同一条边，属待修的 buffer 路径建模缺口。

### 吞吐（诊断性，共享宿主机）

同条件（5 个负载并发，各约占其核数线程）串行 vs 并行对照（第 1 轮，
M uops/s）：namd c04 3.23→5.51、graph500 c32 1.31→2.53、lbm c32
2.00→2.94，增益 **1.5–1.9×**。

并行模式三次重复（5 负载并发批次下，M uops/s，spread 为
(max−min)/mean）：

| 负载 | mean | min | max | spread% | wall s | CPU% |
|---|---:|---:|---:|---:|---:|---:|
| namd c04 | 4.40 | 4.37 | 4.41 | 0.81 | 9.5 | 350 |
| graph500 c32 | 2.25 | 2.24 | 2.26 | 0.81 | 118.7 | 461 |
| lbm c32 | 2.66 | 2.65 | 2.67 | 0.56 | 111.3 | 314 |
| zstd c16 | 1.53 | 1.53 | 1.53 | 0.16 | 131.9 | 482 |
| tealeaf c08 | 5.95 | 5.94 | 5.95 | 0.21 | 13.1 | 426 |

重复噪声均 <1%。批次并发会使绝对数低于单跑（如 namd c04 单跑约
5.5M），故上表绝对值仅作同条件横向比较；结论是 prepare 时序精度保留
的同时，单核串行被消除、吞吐恢复。
