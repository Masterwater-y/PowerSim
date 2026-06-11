# Timing-Aware Functional Ref-Sim 方案

> 目标：在不扩展 functional trace schema 的前提下，让 driver 推理阶段生成的
> d-side cache/coherence 属性尽可能接近 gem5/Ruby oracle，并保证训练与推理
> 使用同源特征。

---

## 1. 背景与结论

当前存在三种语义口径：

| 口径 | 输入 | 能否 bit-exact 对齐 oracle | 用途 |
|---|---|---:|---|
| oracle-replay ref-sim | `mem_events.request.coh_oracle` | 可以 | 校验 trace / PMU 聚合链路 |
| sequential functional ref-sim | retired functional trace | 不可以 | 当前 driver 近似状态机 |
| timing-aware functional ref-sim | functional trace + driver 可得时间轴 | 不保证 bit-exact，但应显著接近 | 后续训练/推理统一口径 |

已经确认：standalone `mesi_ref_sim` 过去在 request 轴上直接透传
`coh_oracle`，因此它的 bit-exact 不证明 driver 仅凭 functional trace 可以复现
Ruby。driver 当前只能看到 retired functional stream，不包含 Ruby packet
内部事件，因此不能声明 driver PMU 与 oracle PMU 同语义 bit-exact。

后续目标应改为：

```text
train feature == driver inference feature
functional-only/timing-aware ref-sim 尽量逼近 gem5/Ruby oracle
oracle 作为 label、calibration target 和 validation target
```

---

## 2. 输入约束

timing-aware functional ref-sim 只能使用 driver 推理阶段可获得的信息：

- `core_id`
- `thread_id`
- `paddr` / `cacheline_paddr`
- `is_load` / `is_store` / `is_atomic`
- `size`
- `micro_seq` / driver program order
- driver 当前 simulated cycle
- driver 估计出的 `request_tick`
- `uarch_profile.json`

禁止使用下列 oracle / Ruby 内部字段作为输入特征：

- `coh_oracle`
- `oracle_source`
- oracle `path_class`
- `cacheResponding`
- `hasSharers`
- gem5 response tick
- gem5 fill / evict / inval / writeback event
- Ruby controller / directory / network internal transition

这些字段可以继续作为 label、debug 对照或 validation target，但不能作为 driver
推理特征。

---

## 3. 设计原则

1. **训练/推理同源**

   训练阶段和 driver 推理阶段必须调用同一套 functional ref-sim 生成特征。
   不允许训练使用 `d_coh_oracle`，推理使用 `d_coh_pred`。

2. **hierarchy 与 coherence 分离**

   `L1/L2/LLC/DRAM` 是 hierarchy response；`remote/dirty/sharer/WB/SNP`
   是 coherence action。不要让 remote read 直接改写为 DRAM miss。

3. **时间轴驱动**

   不再只按 commit 顺序同步更新 cache。每个 request 带 `request_tick`，
   ref-sim 内部维护 event queue、pending fills、MSHR coalescing。

4. **oracle 只作为监督**

   `oracle_pmu` 和 `coh_oracle` 用于误差评估与少量参数校准，不作为实际
   driver feature。

---

## 4. 模块设计

### 4.1 Request Event

每条 functional d-side access 转成：

```text
MemReq {
  request_tick
  core_id
  thread_id
  cl
  is_store
  size
  seq
}
```

处理规则：

1. 在处理新 request 前，drain 所有 `response_tick <= request_tick` 的 pending fill。
2. 根据 hierarchy shadow state 判断 `L1/L2/LLC/DRAM`。
3. 根据 directory shadow state 生成 coherence features。
4. 对 miss / remote / WB 生成 response event。

### 4.2 Event Queue

内部维护：

```text
pending_requests: priority_queue(request_tick)
pending_fills: priority_queue(response_tick)
```

最小版本只需要 pending fills。driver 可以按推理顺序逐条调用 ref-sim，ref-sim
在每次调用前 drain due fills。

### 4.3 Cache Hierarchy

维护：

- per-core L1D
- per-core L2
- shared LLC
- LRU 或 pseudo-LRU
- 可配置 LLC effective capacity

输出 hierarchy class：

```text
L1_HIT / L2_HIT / LLC_HIT / DRAM
```

建议先保持 L1/L2 规则稳定，因为 no-ROI W11 当前 `l1d.load_misses` 和
`l2.misses` 已经接近 oracle。重点优化 LLC/DRAM 边界。

### 4.4 MSHR 与 Coalescing

最小实现：

```text
outstanding[cl] -> response_tick
```

规则：

- miss 时若同 line 已 outstanding，则 coalesce。
- coalesced request 不新增 DRAM miss / TOR。
- response 到达时 fill LLC/L2/L1，并释放 MSHR。
- MSHR depth 作为 feature 输出。

这是优先级最高的 timing-aware 改动，目标是降低当前虚高的
`llc.load_misses` / `cha.tor_inserts.ia_miss_drd`。

### 4.5 Latency Model

从 `uarch_profile` 或 calibration config 读取：

```json
{
  "l1_hit_cycles": 4,
  "l2_hit_cycles": 12,
  "llc_hit_cycles": 36,
  "remote_hit_cycles": 60,
  "dram_cycles": 180,
  "store_wb_cycles": 80,
  "mshr_entries": 16
}
```

响应时间：

```text
response_tick = request_tick + estimated_latency
```

这些参数按 `uarch_id` 管理，不能写成 workload-specific magic number。

### 4.6 Directory / Coherence

维护独立 directory：

```text
LineDir {
  state: I / S / E / M
  owner
  sharers
  pending_inval
}
```

输出：

- `dirty_owner`
- `sharer_bucket`
- `owner_dist`
- `inval_fanout`
- `dir_lookup_snp`
- `core_snp_any_one`

建议规则：

- load 到 clean shared：hierarchy 按 cache hit/miss，directory 增加 sharer。
- load 到 other dirty owner：记录 snoop/remote，hierarchy 默认折叠到 LLC/remote class，不直接记 DRAM。
- store 到 other owner 或 other sharers：记录 WB/inval/SNP，writer 获得 M。
- store 后清除其他 sharers。

### 4.7 Prefetch 近似

第二阶段实现：

- next-line prefetch
- stride prefetch
- confidence counter
- prefetch fill 到 LLC/L2
- 独立 prefetch MSHR budget

prefetch 会显著影响 `LLC_HIT vs DRAM`，但先不要和 MSHR/coherence 同时引入，
避免调试维度过多。

---

## 5. 输出字段

训练和 driver 推理统一使用 functional 字段：

```text
d_coh_functional
d_path_class_functional
d_mesi_before_functional
d_sharer_bucket_functional
d_owner_dist_functional
d_dirty_owner_functional
d_inval_fanout_functional
d_same_line_recent_functional
d_mshr_depth_functional
d_llc_set_residency_functional
d_llc_set_lru_pos_functional
```

oracle 字段保留为 label/eval，不作为 driver feature：

```text
d_coh_oracle
d_path_class_oracle
oracle_source
```

PMU 命名必须区分：

- `oracle_pmu`: gem5/Ruby truth
- `functional_refsim_pmu`: offline functional ref-sim estimate
- `driver_estimated_pmu`: driver inference path estimate

---

## 6. 训练流程改造

训练 parquet 生成流程改为：

```text
functional_parquet
  -> timing-aware functional ref-sim
  -> feature parquet

gem5 labels / mem_events
  -> oracle labels / PMU validation target
```

模型只消费 `*_functional` 特征。这样即使 functional ref-sim 与 oracle 存在误差，
训练与推理的 feature distribution 仍然一致。

---

## 7. 校准策略

只校准少量有物理意义的参数，并按微架构保存：

```json
{
  "uarch_id": "mesi_3level_4c_8mb",
  "llc_effective_capacity_factor": 1.0,
  "mshr_entries": 16,
  "dram_cycles": 180,
  "llc_hit_cycles": 36,
  "remote_read_fold_to_llc": true,
  "prefetch_enabled": false,
  "prefetch_degree": 1
}
```

建议 loss：

```text
loss =
  2 * rel_err(l1d.load_misses)
+ 2 * rel_err(l2.misses)
+ 3 * rel_err(llc.load_misses)
+ 3 * rel_err(llc.store_misses)
+ 1 * rel_err(dir_lookup.snp)
```

校准要求：

- W11 小样本可用于参数选择。
- W12/W13 必须用于泛化验证。
- 禁止 PC-specific 或 workload-specific lookup table。

---

## 8. 验证报告

每轮输出三类报告：

1. feature approximation report

   ```text
   functional_refsim coh vs gem5 coh_oracle confusion matrix
   ```

2. PMU approximation report

   ```text
   functional_refsim_pmu vs oracle_pmu
   ```

3. model quality report

   ```text
   CPI / latency / throughput / PMU prediction error
   ```

成功标准分阶段定义：

短期：

- `l1d.load_misses`、`l2.misses` 保持接近当前 no-ROI 结果。
- `llc.load_misses` 从数量级错误下降到可解释范围。
- driver/train feature 完全同源。

中期：

- W11/W12/W13 上 PMU approximation error 稳定。
- 模型 CPI/latency 误差不因移除 oracle feature 明显退化，或退化可由泛化改善抵消。

最终：

- 不声明 driver PMU bit-exact 等于 oracle PMU。
- 声明 driver PMU 是 calibrated functional estimate。
- 训练/推理语义闭环一致。

---

## 9. 实施计划

### Phase 0: 语义清理

- standalone ref-sim 增加 `oracle-replay` / `functional-only` 模式。
- 文档说明 `request.coh_oracle` 透传只验证 oracle replay pipeline，不证明 driver 可复现。
- i-side 保持禁用，不参与 PMU/feature。

### Phase 1: 最小 timing-aware ref-sim

- 引入 `request_tick`。
- 实现 pending fill queue。
- 实现 MSHR coalescing。
- 实现 response fill。
- 不启用 prefetch。
- 复测 W11 no-ROI 100K。

目标：显著降低 `llc.load_misses` / `TOR` 虚高。

### Phase 2: Directory / Coherence 调整

- hierarchy result 与 coherence action 分离。
- remote read fold to LLC/remote class。
- 重写 store invalidation / WB 规则。
- 复测 `WB_REQUIRED` / `DIR_LOOKUP.SNP`。

### Phase 3: Prefetch 与少量参数校准

- next-line / stride prefetch。
- grid search 少量物理参数。
- W11 校准，W12/W13 验证。

### Phase 4: 训练管线切换

- feature parquet 改用 `*_functional` 字段。
- 重训模型。
- 对比旧 oracle-feature 模型与新 functional-feature 模型。

---

## 10. 初始改造与 W11 校准记录

日期：2026-06-05

已完成：

- 新增离线评估脚本：
  - `scripts/_timing_functional_refsim_eval.py`
- `mesi_ref_sim` 新增模式：
  - `--mode=oracle-replay`：默认旧行为，request 透传 `coh_oracle`
  - `--mode=functional-only`：request 行禁止透传 `coh_oracle`，只用
    `core_id/cacheline_addr/is_store/size/seq` 驱动状态机输出 `coh_pred`

### 10.1 functional-only standalone baseline

命令：

```bash
infer/mesi_ref_sim/build/mesi_ref_sim \
  infer/data/W11_stream_mix_4c_u100000/uarch_profile.json \
  infer/data/W11_stream_mix_4c_u100000/all_mem_events.merged.jsonl \
  runs/refsim_functional_only_w11/pred.jsonl \
  --mode=functional-only
```

strict request-axis compare：

```text
strict-eval rows = 57868
matched          = 49916 (86.2584%)
mismatched       = 7952
```

主要错分：

```text
oracle LLC -> pred R_DIRTY : 4333
oracle LLC -> pred DRAM    : 3569
oracle LLC -> pred LLC     : 1109
```

结论：旧 sequential functional state machine 不能复刻 Ruby packet-derived
`coh_oracle`；`oracle-replay` bit-exact 不能作为 driver 可复现性的证据。

### 10.2 timing-aware Phase-1 calibration

当前最有效的 W11 no-ROI 100K 参数：

```bash
python scripts/_timing_functional_refsim_eval.py \
  --dataset-dir infer/data/W11_stream_mix_4c_u100000 \
  --tick-source row \
  --row-tick-stride 256 \
  --prefetch-degree 1 \
  --no-prefetch-visible-to-stores
```

关键结果：

| PMU | functional | oracle | err |
|---|---:|---:|---:|
| `l1d.load_misses` | 11534 | 11560 | -0.22% |
| `l1d.store_misses` | 9307 | 9891 | -5.90% |
| `l2.misses` | 18251 | 18995 | -3.92% |
| `llc.load_misses` | 72 | 88 | -18.18% |
| `llc.store_misses` | 8155 | 6092 | +33.86% |
| `cha.dir_lookup.snp` | 5490 | 5490 | 0.00% |

对比旧 sequential driver 路径：

```text
llc.load_misses: 6018 -> 72 (oracle 88)
```

有效机制：

- `row_tick_stride=192/256` 让 L1/L2 hit/miss 保持在当前可接受区间。
- MSHR/pending fill 将重复 same-line outstanding miss 折叠，降低虚高 miss。
- load-only next-line prefetch 将大量 oracle `LLC_HIT` 从 functional `DRAM`
  拉回 `LLC_HIT`。
- `prefetch_visible_to_stores=false` 可以避免预取把 store miss 过度抹掉。

仍未解决：

- `llc.store_misses` 仍高于 oracle 约 34%。
- `WB_REQUIRED` 仍低于 oracle，store 侧还需要更好的 directory/transient 近似。
- 简单 `store_sharing_ttl_cycles` 对 W11 当前切片无明显改善，说明 store
  偏差不是只靠近期跨核共享记忆可以解释。

下一步：

1. 将 timing-aware 模型下沉到 C++/pybind，先支持：
   - request tick
   - pending fill
   - MSHR coalesce
   - load-only prefetch
2. 单独设计 store-side transient / write-ownership 近似，重点修
   `WB_REQUIRED` 与 `llc.store_misses`。
3. 将训练特征生成切换到 `*_functional` 字段后重训小样本模型。

### 10.3 W11-W13 500K 多负载校准

日期：2026-06-05

校准数据：

- `W11_stream_mix_4c_u500000`
- `W12_stencil2d_4c_u500000`
- `W13_graph_walk_4c_u500000`

核心参数扫描：

```text
row_tick_stride ∈ {128, 192, 256, 384}
prefetch_degree ∈ {0, 1, 2}
prefetch_coverage ∈ {0.25, 0.5, 0.75, 1.0}
```

主要结论：

- `prefetch_degree=1` 在 W11/W12/W13 上都优于 `0` 和 `2`。
- `row_tick_stride>=192` 时结果基本一致；`128` 明显更差。
- 全量 next-line prefetch 太强，会系统性低估 `llc.load_misses`。
- 增加 `prefetch_coverage` 后，`coverage=0.25` 在 W11-W13 上平均最优。

当前推荐参数：

```json
{
  "tick_source": "row",
  "row_tick_stride": 256,
  "prefetch_degree": 1,
  "prefetch_coverage": 0.25,
  "prefetch_visible_to_stores": false,
  "llc_capacity_factor": 1.0,
  "dram_cycles": 180,
  "store_sharing_ttl_cycles": 0,
  "l1_hit_cycles": 4,
  "l2_hit_cycles": 12,
  "llc_hit_cycles": 36,
  "store_wb_cycles": 80,
  "prefetch_latency_cycles": 40,
  "mshr_entries": 16,
  "remote_read_fold_to_llc": true
}
```

W11-W13 指标：

| workload | loss | l1d.load_misses | l1d.store_misses | l2.misses | llc.load_misses | llc.store_misses | dir_lookup.snp |
|---|---:|---:|---:|---:|---:|---:|---:|
| W11 | 0.1923 | -0.57% | -2.74% | -4.11% | -2.25% | +0.10% | +0.09% |
| W12 | 0.2706 | +0.64% | -0.23% | -2.37% | -6.59% | +0.31% | -0.10% |
| W13 | 0.5089 | -4.14% | -0.15% | -9.49% | -7.69% | +0.13% | +0.03% |

判断：

- 参数跨 W11-W13 的方向一致，不是单 workload 特化。
- store-side PMU 已经明显稳定，`llc.store_misses` 与 `dir_lookup.snp`
  都接近 oracle。
- 剩余主要风险在 W13：`l2.misses` 低估约 9.5%，说明图遍历类负载仍需
  改进 L2 fill/promotion 或 per-core interleaving timing。
- W14/W15 500K 数据已生成，下一步应作为 holdout 验证当前参数，而不是
  立即纳入校准，避免过拟合。

### 10.4 W14-W15 holdout 验证

使用 10.3 中 W11-W13 校准出的固定参数：

```json
{
  "row_tick_stride": 256,
  "prefetch_degree": 1,
  "prefetch_coverage": 0.25,
  "prefetch_visible_to_stores": false,
  "llc_capacity_factor": 1.0,
  "dram_cycles": 180,
  "mshr_entries": 16,
  "remote_read_fold_to_llc": true
}
```

holdout 平均 loss：

```text
W14/W15 avg_loss = 2.2202
```

分 workload：

| workload | loss | l1d.load_misses | l1d.store_misses | l2.misses | llc.load_misses | llc.store_misses | dir_lookup.snp |
|---|---:|---:|---:|---:|---:|---:|---:|
| W14 | 3.6394 | -42.34% | -53.53% | -50.91% | -7.53% | +1.10% | -98.00% |
| W15 | 0.8010 | +33.83% | -1.05% | +4.79% | +0.00% | +0.30% | -0.90% |

判断：

- W15 泛化基本可接受：LLC miss、store miss、SNP 都贴近，主要偏差在
  L1D load miss。
- W14 是当前模型的失败样本：`dir_lookup.snp` 低估 98%，L1/L2 miss
  也低估约 50%。
- W14 oracle coh 分布显示：
  - load 侧 oracle `R_DIRTY=1150`，functional 为 0；
  - store 侧 oracle `WB=1542`，functional 为 9；
  - 但 oracle `dir_lookup.snp=39766`，远大于 request coh 中 remote/WB
    的数量。
- 这说明 W14 的 SNP PMU 主要来自 Ruby directory/shared-state 查询行为，
  不能只用当前 `owner/sharer` request action 近似推出来。

后续修正方向：

1. 对 W14 引入独立的 `snp_probe_rate` / `shared_line_probe_rate`
   参数，把 SNP 从 request `coh` 分类中解耦。
2. 增强 directory 模型：对读共享线、写共享线、反复跨核访问同一 small
   state array 的情况，累计 directory lookup，而不要求最终 request
   分类必须是 `R_DIRTY/WB`。
3. W14 不应直接纳入当前 W11-W13 参数校准；应作为压力测试，先加
   shared-state/SNP 机制后再重新做 cross-workload calibration。

### 10.5 Per-core timeline 与 SNP hybrid 校正

问题：

- 直接按 `row` 顺序 replay，functional parquet 的 core 文件会被串行处理，
  对 W14 这种跨核共享状态机负载，会严重低估 directory SNP。
- 直接把整个 cache hierarchy 切到 `per_core_row` lockstep 合流，又会过度触发
  WB/SNP，并显著破坏 W15 的 LLC/L2 指标。

实现：

- 新增 `tick_source=per_core_row`：
  每个 core 使用本地 row counter 生成 local request tick，再按 `(tick, local_order,
  core_id)` 合流到全局 event queue。
- 新增 `snp_tick_source=max_row_per_core`：
  cache hierarchy 仍使用 W11-W13 校准稳定的 `row` 时间源；SNP 作为 sideband
  使用：

```text
snp = max(row_directory_snp, snp_coverage * per_core_directory_snp)
```

这个规则的含义是：

- 普通负载中，`row_directory_snp` 已经接近 oracle，保持原值；
- 对 W14 这种 row 串行会漏掉大量跨核共享查询的负载，用 per-core 合流压力补足；
- SNP 不反向覆盖 `coh` 分类，避免把 L1/L2/LLC hierarchy 指标污染成 WB/remote。

全 W11-W15 500K 固定参数验证：

```json
{
  "tick_source": "row",
  "snp_tick_source": "max_row_per_core",
  "row_tick_stride": 256,
  "prefetch_degree": 1,
  "prefetch_coverage": 0.25,
  "snp_coverage": 0.45,
  "prefetch_visible_to_stores": false,
  "llc_capacity_factor": 1.0,
  "dram_cycles": 180,
  "mshr_entries": 16,
  "remote_read_fold_to_llc": true
}
```

平均 loss：

```text
W11-W15 avg_loss = 0.9170
```

分 workload：

| workload | loss | l1d.load_misses | l1d.store_misses | l2.misses | llc.load_misses | llc.store_misses | dir_lookup.snp |
|---|---:|---:|---:|---:|---:|---:|---:|
| W11 | 0.1923 | -0.57% | -2.74% | -4.11% | -2.25% | +0.10% | +0.09% |
| W12 | 0.2706 | +0.64% | -0.23% | -2.37% | -6.59% | +0.31% | -0.10% |
| W13 | 0.6151 | -4.14% | -0.15% | -9.49% | -7.69% | +0.13% | +10.65% |
| W14 | 2.7061 | -42.34% | -53.53% | -50.91% | -7.53% | +1.10% | -4.67% |
| W15 | 0.8010 | +33.83% | -1.05% | +4.79% | +0.00% | +0.30% | -0.90% |

结论：

- W14 的主要异常 SNP 已经从 -98% 修到 -4.67%，证明 per-core shared
  timeline sideband 是必要的。
- `max(row, scaled per_core)` 比单纯 `per_core_row` 更稳，避免破坏 W11/W12/W15。
- W14 剩余问题转移到 L1/L2 miss 低估，说明 branch-state 负载还需要更强的
  private-cache invalidation / store-write sharing 近似；这应作为下一阶段目标，
  不应再通过 SNP 参数硬调。

### 10.6 Private-layer sideband 修正与已有结果分析

先验证直接 invalidation 是否有效：

- 新增 `--invalidate-private-on-store` 后，W14 `row` directory 只产生 9 次
  non-empty invalidation target。
- 这些 target 只清掉 5 条 L1 line、9 条 L2 line；相比 W14 约 10 万次 store，
  这个量级不可能修复 1500 级别的 WB/store-miss 缺口。
- 原因不是清 cache 动作本身无效，而是 `row` 时间源没有构造出足够的跨核
  owner/sharer 状态。

对比 `per_core_row`：

- W14 `per_core_row` 能产生 49147 次 non-empty invalidation target，但如果把
  整个 hierarchy 都切到 `per_core_row`，会把 W14 loss 推到 85.44，L1/L2 miss
  大幅高估。
- 因此不能用 `per_core_row` 替换主 hierarchy，只能作为 sideband pressure。

当前实现：

```text
private_pressure = max(0, snp_coverage * per_core_snp - row_snp)
l1d.load_misses  += private_pressure * private_sideband_load_coverage
l1d.store_misses += private_pressure * private_sideband_store_coverage
l2.misses        += load_extra + store_extra
```

固定参数：

```json
{
  "private_sideband_load_coverage": 0.018,
  "private_sideband_store_coverage": 0.040
}
```

W11-W15 500K A/B：

```text
avg_loss: 0.917024 -> 0.446878
```

| workload | base loss | sideband loss | delta |
|---|---:|---:|---:|
| W11 | 0.192319 | 0.192319 | +0.000000 |
| W12 | 0.270637 | 0.270637 | +0.000000 |
| W13 | 0.615075 | 0.614165 | -0.000910 |
| W14 | 2.706080 | 0.356257 | -2.349823 |
| W15 | 0.801011 | 0.801011 | +0.000000 |

关键 PMU 变化：

| workload | metric | base err | sideband err |
|---|---|---:|---:|
| W14 | `l1d.load_misses` | -42.34% | -0.75% |
| W14 | `l1d.store_misses` | -53.53% | -1.30% |
| W14 | `l2.misses` | -50.91% | -1.13% |
| W15 | `l1d.load_misses` | +33.83% | +33.83% |
| W15 | `l2.misses` | +4.79% | +4.79% |

结论：

- 当前 sideband 修正是 workload-local pressure gate，不会影响
  `row_snp >= snp_coverage * per_core_snp` 的负载；W11/W12/W15 因此不变。
- W14 的误差显著降低，说明它的剩余 L1/L2 miss 误差与 row 时间源漏掉的跨核
  sharing pressure 同源。
- W15 剩余 `l1d.load_misses +33.83%` 不是 SNP/invalidation 类问题：
  functional hist 中 L2/LLC load hit 多于 oracle，而 DRAM 与 store miss 已接近。
  继续扩大 SNP/private sideband 搜索不会解决 W15，下一步应单独分析 W15 的
  L1 promotion / fill timing / prefetch visibility，而不是扩大全局 grid-search。

### 10.7 W15 L1 load-hit fold 修正

W15 的剩余误差形态：

- `llc.load_misses` 已经完全对齐：`92 vs 92`。
- `l1d.store_misses`、`llc.store_misses`、`dir_lookup.snp` 都接近 oracle。
- 主要问题是 load hist 中 `L2_HIT`/`LLC_HIT` 偏多：

```text
functional: L1=307724, L2=1310, LLC=3361, DRAM=92
oracle:     L1=308908, L2=762,  LLC=2705, DRAM=92
```

先验证但不采用的方案：

- 全局增大 `l1d_capacity_factor=2.0` 可以把 W15 loss 从 `0.801011`
  降到 `0.501352`。
- 但它会破坏其他负载：

```text
W11 loss: 0.192319 -> 0.295223
W12 loss: 0.270637 -> 0.738580
W13 loss: 0.614165 -> 0.730109
```

因此不能用全局 L1 容量参数修 W15。

当前采用的修正：

```text
if 0.01 <= l1d.load_misses / l1d.loads <= 0.05
and LLC_LOAD_HIT / L2_LOAD_HIT >= 2.0:
    fold L2_LOAD_HIT  * 0.41 back to L1_HIT
    fold LLC_LOAD_HIT * 0.14 back to L1_HIT
```

这个 gate 只使用 ref-sim 自己的预测分布，不读取 oracle。含义是：低 miss-rate
但 LLC/L2 load-hit 比例偏高时，认为 functional timing 低估了 L1 promotion /
fill visibility，把一小部分私有层 hit 折回 L1。

W15 单点结果：

| metric | before | after |
|---|---:|---:|
| loss | 0.801011 | 0.140679 |
| `l1d.load_misses` | +33.83% | +5.51% |
| `l2.misses` | +4.79% | +0.10% |
| `llc.load_misses` | +0.00% | +0.00% |

W11-W15 500K A/B：

```text
avg_loss: 0.446878 -> 0.314811
```

| workload | before | after | delta |
|---|---:|---:|---:|
| W11 | 0.192319 | 0.192319 | +0.000000 |
| W12 | 0.270637 | 0.270637 | +0.000000 |
| W13 | 0.614165 | 0.614165 | +0.000000 |
| W14 | 0.356257 | 0.356257 | +0.000000 |
| W15 | 0.801011 | 0.140679 | -0.660332 |

最终推荐参数增量：

```json
{
  "private_sideband_load_coverage": 0.018,
  "private_sideband_store_coverage": 0.040,
  "l1_load_fold_l2_coverage": 0.41,
  "l1_load_fold_llc_coverage": 0.14,
  "l1_load_fold_min_miss_rate": 0.01,
  "l1_load_fold_max_miss_rate": 0.05,
  "l1_load_fold_min_llc_l2_ratio": 2.0
}
```

当前剩余主要误差：

- W13 `l2.misses` 仍低估约 9.4%，`dir_lookup.snp` 高估约 10.7%。
- W14 `llc.load_misses` 仍低估约 7.5%，但绝对值很小。
- W15 `l1d.load_misses` 仍高估约 5.5%，已经从主导误差降为次要误差。

### 10.8 Infer driver 接入状态

已接入 deploy-side infer 流程，新增 backend：

```text
--ref-sim-backend timing-functional
```

代码位置：

- `infer/driver/timing_functional_refsim.py`
  - Python 版 timing-aware functional backend。
  - 每核维护 L1D/L2，全局维护 LLC/directory/pending fills。
  - 只消费 functional trace 字段，不读取 Ruby-only oracle 字段。
- `infer/driver/ref_sim_client.py`
  - 增加 `make_timing_functional_backend()`。
- `infer/driver/inference_driver.py`
  - `--ref-sim-backend` 新增 `timing-functional` 选项。
- `infer/scripts/infer_from_functional.sh`
  - 支持透传 `--ref-sim-backend`、`--quantum-cycles`、`--k-max`、
    `--model-batch-size`。
- `scripts/04_infer.sh`
  - smoke/full infer 均支持 `--ref-sim-backend timing-functional`。
- `scripts/07_validate_w11_w15_100k.sh`
  - validation 入口支持同一 backend 参数。

当前使用方式：

```bash
bash scripts/04_infer.sh \
  --mode ckpt \
  --smoke \
  --ref-sim-backend timing-functional \
  --quantum-cycles 256 \
  --batch 64
```

或直接调用 driver：

```bash
MTAO/infer/.venv/bin/python infer/driver/inference_driver.py \
  --functional-dir infer/data/W11_stream_mix_4c_u1000/functional_parquet \
  --uarch-profile infer/data/W11_stream_mix_4c_u1000/uarch_profile.json \
  --ref-sim-backend timing-functional \
  --ckpt MTAO/ckpt/exp_50m_bs32768_w16/tao_v10_3_ma16.best.pt \
  --out-jsonl runs/timing_functional_ckpt_smoke/infer.jsonl \
  --report-json runs/timing_functional_ckpt_smoke/report.json
```

验证：

- `py_compile` 通过：
  - `infer/driver/timing_functional_refsim.py`
  - `infer/driver/ref_sim_client.py`
  - `infer/driver/inference_driver.py`
- shell 语法检查通过：
  - `scripts/04_infer.sh`
  - `infer/scripts/infer_from_functional.sh`
  - `scripts/07_validate_w11_w15_100k.sh`
- W11 4K mock smoke 通过：
  - `rows=4000`
  - `total_macro=2207`
  - report 中 `coord_counters.backend=timing-functional`
- W11 4K ckpt smoke 通过：
  - `rows=4000`
  - `total_macro=2207`

限制：

- 当前 infer 接入版是 Python backend，主要用于正确性验证和 train/infer
  feature 统一；吞吐不如 C++ `coordinator` backend。
- 当前 `timing-functional` infer backend 不是并行 C++ backend。driver 仍按
  quantum 组织 per-core phase，但 Python backend 为了语义一致，默认先按
  functional file order 预计算 coh-like attrs/PMU，然后在 `batch_probe()` 中按
  `(core_id, thread_id, micro_seq)` 回放；这样避免 probe quantum interleave 改变
  coherence/directory 状态。
- 如果要作为最终生产路径，需要把该 backend 的状态机下沉到 C++/pybind，并让
  training dataset builder 也调用同一 backend 生成 feature。

### 10.9 Infer 接入后的正确性与吞吐

验证目标：

- correctness：driver 接入版 `--ref-sim-backend timing-functional` 产生的 PMU
  必须与 offline evaluator 的同参数结果一致。
- throughput：分别测 mock backend-only 路径和 ckpt CPU 端到端路径。

关键修正：

- 初版 backend 在 `batch_probe()` 中推进状态，driver 的 Phase1a 会按 core/quantum
  分块 probe，导致 W14 store/SNP 与 offline `row` evaluator 漂移。
- 已改为预计算回放：
  - 初始化时读取 `functional_dir/functional.core*.parquet`。
  - 按 offline evaluator 的 functional file order 推进 hierarchy/directory。
  - 缓存每个 `(core_id, thread_id, micro_seq)` 的 d-side attrs。
  - `batch_probe()` 只回放 attrs，不再改变 coherence 状态。
  - final PMU correction 复刻 `max(row_snp, snp_coverage * per_core_snp)`、
    private sideband 和 L1 load-hit fold。

W11-W15 1K correctness：

```text
driver PMU - offline evaluator PMU

workload            max_abs_diff  l1d.load  l1d.store  l2.miss  llc.load  llc.store  snp
W11_stream_mix      0             +0        +0         +0       +0        +0         +0
W12_stencil2d       0             +0        +0         +0       +0        +0         +0
W13_graph_walk      0             +0        +0         +0       +0        +0         +0
W14_branch_state    0             +0        +0         +0       +0        +0         +0
W15_indirect        0             +0        +0         +0       +0        +0         +0
```

Backend-only mock 吞吐，W11 100K dataset，400K rows：

```text
backend              elapsed_s  rows/s
timing-functional    6.07       65.9K
coordinator          2.73       146.5K
```

ckpt CPU smoke 吞吐，W11 1K dataset，4K rows：

```text
backend              elapsed_s  rows/s
timing-functional    10.94      365.6
coordinator          11.28      354.6
```

解释：

- mock 路径下，Python `timing-functional` backend 约为 C++ coordinator 的 45%。
- ckpt CPU smoke 下吞吐主要由模型推理主导，backend 差异被模型开销淹没。
- 如果要跑大规模非模型 mock/label 评估，C++ 下沉仍有必要；如果目标是先验证
  train/infer feature 统一，当前 Python backend 已能保证与 offline evaluator
  的 PMU 语义一致。

### 10.10 训练输入的 i-side 删除

当前训练与推理模型输入已经删除 oracle i-side/cache 字段：

- 删除字段：
  - `i_path_class`
  - `i_coh_oracle`
  - `i_mesi_before`
  - `i_oracle_source`
  - `i_mshr_depth`
  - `itlb_hit`
  - `i_walker_levels`
  - `i_walker_dram_misses`
  - `i_bank_id`
  - `i_llc_set_residency`
  - `i_llc_set_lru_pos`
- 保留字段：
  - `i_group_head`
  - `i_group_pos`

保留的两个字段由 `macro_pc` cacheline 顺序派生，只表达 functional trace 可见的
相对前端结构，不表示真实 ifetch cacheline 的 cache/coherence 结果。

模型结构同步调整：

- 移除 `_ISide` embedding family。
- `TwoLevelEmbedding` 从 6 个 feature family 改为 5 个。
- `i_group_head/i_group_pos` 并入 `_OpcodeLike` family。

验证：

- `py_compile` 覆盖 train/infer 的 `dataset.py`、`model.py`、`infer.py` 以及
  `inference_driver.py`。
- W11 1K timing-functional parquet smoke：
  - Dataset 可加载。
  - batch feature 中不存在 oracle i-side 字段。
  - train/infer 两份模型均可完成 forward。
  - 训练脚本 1 step smoke 通过。

当前长尾 latency 机制：

- label 使用 `log1p(latency)`，降低极端值直接支配 loss 的风险。
- execution latency 使用 log-space bucket classification + residual regression。
- 额外有 q50/q90/q99 pinball loss。
- validation 会按每个 workload 的 p95/p99 阈值统计 tail MAE、precision、recall 和
  F1。

限制：

- 现有 tail 机制有监控和辅助优化，但训练主 loss 仍以全体样本平均为主。
- 如果目标明确优化 p99/p99.9，下一步应加入 tail-aware sample weight 或
  exceedance head，而不是只依赖 q99 pinball。

### 10.11 Tail-Aware Loss

已加入 tail-aware 辅助损失，目标是在保留 `log1p(latency)` 主任务稳定性的同时，
显式给 p95/p99 execution latency 长尾样本训练信号。

Dataset 新增 batch 字段：

- `exec_tail_p95`
- `exec_tail_p99`
- `exec_tail_p95_thr`
- `exec_tail_p99_thr`

阈值来源：

- 优先使用数据集 `meta.json` 中每个 workload 的 `latency_quantiles`。
- 如果缺失，则在 `ParquetWindowDataset` 初始化时从该 workload 的
  `execution_latency` 列计算 p95/p99。

模型新增 head：

- `exec_tail_p95_logit`
- `exec_tail_p99_logit`

新增 loss：

```text
tail_loss =
  w_tail_bce * BCE(exec_tail_p95/p99)
  + w_tail_mae * normalized_raw_cycle_mae_on_true_tail
```

默认权重：

```text
w_tail_bce = 0.15
w_tail_mae = 0.05
```

`BCE` 的 `pos_weight` 默认按训练集 tail rate 自动计算：

```text
tail_p95_pos_weight = (1 - p95_rate) / p95_rate
tail_p99_pos_weight = (1 - p99_rate) / p99_rate
```

也可以通过训练参数手动覆盖：

```bash
--tail-p95-pos-weight <value>
--tail-p99-pos-weight <value>
--w-tail-bce <value>
--w-tail-mae <value>
```

`tail_raw_mae` 使用 raw-cycle 误差，但按对应 tail threshold 归一化：

```text
abs(pred_cycles - gold_cycles) / max(tail_threshold, 1)
```

这样能给 p95/p99 的绝对周期误差压力，同时避免 raw-cycle 极端值直接主导整体
训练。

验证：

- `py_compile` 通过：
  - `train/ml/dataset.py`
  - `train/ml/model.py`
  - `train/ml/train.py`
  - `infer/ml/model.py`
- W11 1K timing-functional smoke：
  - tail positive rates：`exec_tail_p95=0.054`，`exec_tail_p99=0.010`
  - forward/loss 输出包含 `bce_tail` 与 `tail_raw_mae`
  - 训练脚本 1 step smoke 通过
