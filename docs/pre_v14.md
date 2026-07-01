# pre-v14 方案设计：CPI 主任务收敛与 functional gather pressure 特征

日期：2026-07-01

状态：设计稿。目标是在 v13 结果基础上，明确 v14 应该优先验证的改动，避免继续用 tail loss、side branch 或更大模型去掩盖输入特征和训练目标的问题。

## 1. 背景

v13 已完成：

- `Tail-aware CPI loss`
- `Gated SideMLP`
- Qwen3-0.6B 训练 8000 step
- c04/c08/c16 seedB 完整评估，c32 仍以单 workload 日志为诊断参考

正式已整理的 v13 结果见：

- `docs/eval_v13_tail_side_qwen3_0p6b_step8000_results_20260701.md`

已完成的核心结果：

| eval set | CPI mean err | CPI median err | CPI max err | max workload |
|---|---:|---:|---:|---|
| c04 seedB | 7.87% | 5.57% | 28.73% | `W_false_sharing` |
| c08 seedB | 11.00% | 6.52% | 64.94% | `W_false_sharing` |
| c16 seedB | 10.92% | 7.55% | 38.34% | `W_ads_ranking_proxy` |

与 v9/v11 对比后，当前判断是：

- v13 没有稳定优于 v9，尤其 c04/c08 退化。
- tail loss 和 gated side branch 不是无效，但没有形成可复现的整体收益。
- cache/dtlb miss 多任务头对 CPI 的收益不明确，且可能通过共享 backbone 梯度干扰 CPI。
- 当前 `L_cycles` 与 `L_cpi` 在 log-space 基本等价，不能真正约束窗口级总周期。
- `W_ads_ranking_proxy` 和 `W_false_sharing` 是两类不同问题，不能用同一种 tail guardrail 处理。

## 2. 关键问题判断

### 2.1 `W_false_sharing` 是 stress benchmark，不应主导主指标

`workloads/src/bench_false_sharing.c` 明确构造了所有线程反复写同一个 cache line 不同 word 的场景：

```c
line[slot] += i;
line[slot] ^= line[(slot + 1) % neigh_mod];
```

这会制造持续的 ownership bouncing / invalidation storm。当前 ROI CPI 随核心数急剧上升：

| core | ROI CPI |
|---:|---:|
| c04 | 7.23 |
| c08 | 16.31 |
| c16 | 29.73 |
| c32 | 63.09 |

这个量级对极端 false sharing 是合理的，但不代表普通 workload 的常态。因此 v14 不应围绕这个单点做 workload 特化。

处理方式：

- 保留 `W_false_sharing`，作为 coherence stress 分榜。
- 主榜单同时报告 `all workloads` 和 `production-like excluding stress` 两套指标。
- 不使用 workload name hardcode 校正。
- 后续如要改进，应增加强度连续的 false-sharing 变体，而不是只优化当前极端点。

### 2.2 `W_ads_ranking_proxy` 是真实服务 proxy，应作为 v14 主问题

`workloads/src/bench_ads_ranking_proxy.c` 模拟广告精排路径：

- multi-table sparse embedding gather
- feature crossing
- 小 MLP
- topK heap
- 各 worker 独立，无 mutex/atomic/共享写

它不是 false sharing 那种 pathological coherence case。它的问题是多核并发随机 gather 带来的共享 LLC / DRAM / MSHR / memory queue 拥塞。

v13 的结果：

| core | pred | ROI | err |
|---:|---:|---:|---:|
| c04 | 0.3305 | 0.4270 | 22.59% |
| c08 | 0.3980 | 0.6052 | 34.23% |
| c16 | 0.5319 | 0.8627 | 38.34% |

真实 ROI 从 c04 到 c16 放大约 `2.02x`，模型只放大约 `1.61x`。这说明模型没有充分表达并发随机 gather 的非线性共享内存拥塞。

### 2.3 前 20 个窗口不是主因

逐日志累计曲线显示：

- `W_ads_ranking_proxy` 前 20 窗口偏差大，但只占约 1%，去掉后 c04/c08/c16 最终误差几乎不变。
- `W_false_sharing` c08/c16 前 20 窗口反而较准，误差主要在中后段 phase 出现。

因此 v14 不应把重点放在首窗口 warmup 修正，而应修正主体阶段的 CPI 机制建模。

## 3. v14 目标与非目标

目标：

- 让 CPI 主任务收敛更稳定，避免 cache/dtlb 辅助头拖偏 representation。
- 使用 functional trace 可直接计算的 gather / memory congestion proxy，修复 `W_ads_ranking_proxy` 的核心数缩放低估。
- 引入真正的窗口级 cycles loss，而不是当前等价于 CPI loss 的 cycles 项。
- 保持评估和部署路径不依赖 PMU oracle。
- 不使用 workload name、seed、benchmark 类别作为输入特化。

非目标：

- 不删除 `W_false_sharing`，但不让它单独决定主榜单。
- 不用 low-CPI/easy-workload guardrail 做 workload 特化。
- 不引入需要 gem5 timing/PMU 才能得到的特征。
- 不把每条 uop 展成自然语言。
- 不优先扩大到 4B；当前瓶颈不是模型参数量。

## 4. v14A：训练目标收敛，先去掉干扰项

v14A 是低风险 ablation，不需要重建数据集。

### 4.1 只训练主相关头

建议 active loss keys：

```text
cpi_uop
branch_miss
```

保留模型输出 8 维 PMU，保持 checkpoint/eval 兼容，但训练时不对 cache/dtlb 头反传 loss：

```text
active:   cpi_uop, branch_miss
inactive: l1d_ld_miss, l1d_st_miss, l2_ld_miss, l2_st_miss, llc_miss, dtlb_miss
```

原因：

- cache miss 头作为诊断有价值，但当前 miss count 误差高且 loss 权重不低。
- cache miss count 不等于 CPI；`ads_ranking_proxy` 的问题更像 miss cost / queueing cost，而不是 miss count 本身。
- 先移除多任务干扰，验证 CPI 是否恢复到 v9/v11 水平。

### 4.2 引入窗口级 cycles loss

当前 log-space cycles loss 与 CPI loss 近似等价：

```text
log_cycles_pred = log_cpi_pred + log_uops
log_cycles_label = log_cpi_label + log_uops
```

`log_uops` 抵消后，约束仍是 CPI。

v14A 应改为窗口级或 batch 内窗口级 cycles loss：

```python
pred_cycles_window = sum_i exp(pred_log_cpi_i) * uops_i
label_cycles_window = sum_i label_cpi_i * uops_i
L_cycles_window = Huber(log(pred_cycles_window), log(label_cycles_window))
```

建议初始 loss：

```text
L = 1.0 * L_cpi_core
  + 1.0 * L_cycles_window
  + 0.1 * L_branch_miss
```

先关闭：

- tail-aware CPI loss
- cache/dtlb physical constraints
- cache/dtlb PMU loss

目的不是最终否定 tail loss，而是先建立干净 baseline。

## 5. v14B：functional gather pressure 特征

v14B 需要重建 windows/cache，但不需要重新采集 raw trace 或重跑 gem5。

### 5.1 当前已有但不够的特征

当前 `data/build_windows.py::build_cross_core_features()` 已经计算：

- `random_access_pressure`
- `random_load_pressure_ncore`
- `working_set_pressure_ncore`
- `lines_per_kuop_global`
- `pages_per_kuop_global`
- `global_large_stride_rate`
- `aggregate_load_density`
- `cross_core_line_overlap`
- coherence proxy，例如 `store_owner_switch_rate`、`pairwise_writer_pressure`

这些特征可以粗略描述 memory pressure，但对 `ads_ranking_proxy` 仍不够，因为它没有区分：

- 独立随机 gather vs 地址依赖 pointer chasing
- 低复用的大表 embedding lookup vs 普通大 working set
- miss count vs 多核并发下的 miss cost / queueing cost
- load PC 集中度，即是否少数 gather PC 主导大量随机访问

### 5.2 新增 functional trace 特征

建议新增特征全部从 functional trace 得到：

| 特征 | 作用 | trace 来源 |
|---|---|---|
| `cold_loads_per_kuop` | 冷/远复用 load 强度 | `is_load` + RD bucket |
| `indep_random_loads_per_kuop` | 独立随机 gather 强度 | load + stride/RD + 非 addr-dep |
| `addrdep_random_loads_per_kuop` | pointer chasing 强度 | load + stride/RD + addr-dep |
| `random_unique_lines_per_kuop` | 随机 load footprint | `vaddr/cacheline_addr` |
| `random_unique_pages_per_kuop` | TLB/page footprint | `vaddr >> 12` |
| `random_line_entropy_norm` | 地址随机性 | random load cacheline 序列 |
| `random_page_entropy_norm` | page 分散度 | random load page 序列 |
| `load_pc_top_frac` | 是否少数 load PC 主导 | `micro_pc/macro_pc` |
| `load_pc_entropy_norm` | load site 多样性 | load PC 序列 |
| `llc_footprint_pressure` | footprint 相对 LLC 容量 | unique lines + cfg LLC lines |
| `l2_footprint_pressure` | footprint 相对 L2 容量 | per-core unique lines + cfg L2 lines |
| `gather_pressure_ncore` | 核心主特征 | independent random load density * footprint * log1p(ncore) |
| `queue_pressure_proxy` | memory queue 拥塞 proxy | load density * gather pressure * ncore factor |

推荐公式草案：

```python
random_load = is_load and (
    rd_bucket in {RD_COLD, RD_FAR}
    or stride_bucket in {ST_P9_64, ST_M9_64, ST_LARGE}
)

indep_random_load = random_load and not addr_dep_load
addrdep_random_load = random_load and addr_dep_load

llc_footprint_pressure = squash(unique_random_lines / llc_lines, scale=1.0)
l2_footprint_pressure = squash(unique_core_lines / l2_lines, scale=1.0)

gather_pressure_ncore =
    indep_random_load_density
  * llc_footprint_pressure
  * log1p(n_core)

queue_pressure_proxy =
    aggregate_load_density
  * gather_pressure_ncore
  * squash(lines_per_kuop_global, scale=128.0)
```

其中 `addr_dep_load` 已在 per-core summary 里有类似统计，v14B 要把它用于 cross-core/global 组合。

### 5.3 接入位置

优先接入 side tensor 和 attention scalar，不优先增加离散 token：

1. 在 `model/tokenizer.py::SIDE_FEATURE_KEYS` 末尾追加新字段，保持旧字段顺序不变。
2. 在 `model/tokenizer.py::ATTN_FEATURE_KEYS` 增加少量全局/每核 scalar：
   - `GF_MEM_GATHER_PRESSURE`
   - `GF_MEM_LLC_FOOTPRINT`
   - `GF_MEM_QUEUE_PROXY`
   - `CF_MEM_INDEP_RANDOM_ROLE`
   - `CF_MEM_ADDRDEP_RANDOM_ROLE`
3. 在 `data/build_windows.py::build_cross_core_features()` 计算新 counters。
4. 给 `build_cross_core_features()` 增加可选 `cfg` 参数，用 cache config 计算 L2/LLC lines。
5. 同步修改训练构建和 eval 在线构建两个调用点：
   - `data/build_windows.py`
   - `eval/eval_quota_cycles.py`
6. 更新 cache meta `feat_version`，避免误用旧 tensor cache。

## 6. 数据重建边界

v14A：

- 不需要重建数据集。
- 复用现有 `windows.jsonl` 和 tensor cache。
- 只改 loss mask、window cycles loss、训练脚本参数。

v14B：

- 需要重建 `c01/c04/c08/c16` 的 windows 和 tensor cache。
- 不需要重新跑 raw trace/gem5。
- 原因是新增 side/attention 特征在构建 windows 时从每个窗口的 uop/address 序列计算，旧 cache 里没有这些字段。

结论：

- 之前 v12/v13 的 c08 数据不是错误数据。
- 只是旧数据缺少 v14B 所需的新 functional gather pressure 特征。

## 7. false sharing 后续处理

当前 `W_false_sharing` 不建议删除，但建议从主优化目标中降权或单独分榜：

| 指标 | 用途 |
|---|---|
| 主榜 all workloads | 保持历史可比 |
| production-like score | 排除或低权重 `W_false_sharing` |
| coherence stress score | 专门看 `W_false_sharing` 和未来变体 |

后续如果要真正修 false sharing，应新增强度连续的 workload 变体：

- 共享 cache line store 比例：5% / 20% / 50% / 100%
- 共享行数量：1 / 4 / 16 / 64
- padded 对照版本
- compute/memory phase 混合版本

这需要新增 workload/raw trace，属于 v14C 或后续数据扩展，不建议挡住 v14A/v14B。

## 8. 实验顺序

建议按以下顺序推进：

### v14A loss-only baseline

- active loss：`cpi_uop,branch_miss`
- 加窗口级 `cycles_window` loss
- 关闭 cache/dtlb loss
- 关闭 tail loss
- 不重建数据
- 训练 Qwen3-0.6B 8000 step

目的：

- 验证 v13 退化是否来自 tail loss / gated side / cache 多任务干扰。
- 观察 c04/c08 是否恢复到接近 v9/v11。

### v14B feature rebuild

- 加 functional gather pressure 特征
- 重建 c01/c04/c08/c16 windows/cache
- 训练同样 0.6B 8000 step
- 与 v14A/v13/v9 对比

目的：

- 修复 `W_ads_ranking_proxy` 的核心数缩放低估。
- 验证新增特征是否也改善 `feed_ranking`、`interest_graph_recall`、`search_index_proxy` 等 proxy workload。

### v14B ablation

至少做两组：

| 实验 | 目的 |
|---|---|
| no gather features | 验证收益来自新 functional 特征 |
| gather features + cache loss off | 验证 cache/dtlb 多任务是否仍有害 |

如果资源有限，优先做 `v14A` 和完整 `v14B`。

## 9. 验收标准

主指标：

- c04/c08 mean/median 不差于 v9。
- c16 不低于 v13 的现有水平。
- `W_ads_ranking_proxy` 明显改善：
  - c08 目标小于 v9 约 `23%`
  - c16 目标低于 v13 `38.34%`，至少进入 `25-30%` 区间
- easy workload 不明显退化：
  - `W_compute_int`
  - `W_branch_storm`
  - `W_int_div`
  - `W_indirect`
  - `W_mlp_light`

保护性指标：

- easy workload 误差不增加超过 1-2 pp。
- production-like score 优于 v13。
- all-workload score 同时报告，但不让 `W_false_sharing` 单项掩盖 `ads_ranking_proxy` 是否改善。

诊断指标：

- `W_ads_ranking_proxy` 的 c04->c08->c16 pred scale 更接近 ROI scale。
- `gather_pressure_ncore` 与 `ads_ranking_proxy` 的 CPI residual 正相关。
- cache miss count 误差可以作为参考，但不作为主验收。

## 10. 预期结论路径

如果 v14A 恢复 c04/c08，但 `ads_ranking_proxy` 仍差：

- 说明 v13 的 tail/side/cache 多任务组合确实有干扰。
- 下一步按 v14B 补 functional gather pressure。

如果 v14B 明显改善 `ads_ranking_proxy`：

- 说明主缺口是 functional trace 中已有但未显式表达的并发随机 gather / memory queue proxy。
- 可以继续扩展到更多 production-like proxy workload。

如果 v14B 仍无改善：

- 说明仅靠 functional trace proxy 不足以估计 memory queueing cost。
- 下一步需要考虑 shared-system simulator 或轻量 oracle teacher 生成中间监督，但这超出 v14A/v14B 范围。

