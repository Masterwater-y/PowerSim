# LLMSim 数据集 v7 设计方案

> 目标：解决 v6.1 训练集"名义 16710 sample 但 effective_n 只有 ~3500"的同质化问题，
> 让模型学到的是性能机制，而不是 workload ID。
> 设计准则：**pthread proxy app 作为训练主体 + 少量机制 microbench + 真实应用 trace 仅作 holdout**。

---

## 1. 现状诊断（基于 v6.1）

### 1.1 OOD 报告（详见 `logs/v6.1_distribution_diag.json`）

按 cross-workload NN p50 排序，识别"孤岛"（NN ≥ 3 → 没有相似负载支撑）：

| 负载 | cross_p50 | 状态 |
|---|---:|---|
| W_fp_lite | 6.604 | 孤岛 |
| W_indirect | 5.724 | 孤岛 |
| W_phased_mix | 4.545 | 孤岛 |
| W_interest_graph_recall | 3.162 | 孤岛 |
| 其余 | < 3.0 | 有邻居支撑 |

### 1.2 窗口重复率报告（详见 `logs/window_duplication_v6.1.json`）

按 effective_n（NN > 0.10 的窗口数）排序：

| 负载 | n | effective_n | 状态 |
|---|---:|---:|---|
| W_compute_int | 1198 | 2 | 灾难 |
| W_false_sharing | 1199 | 2 | 灾难 |
| W_int_div | 1199 | 9 | 灾难 |
| W_branch_storm | 1199 | 10 | 灾难 |
| W_indirect | 1198 | 86 | 严重重复 |
| W_fp_compute_dense | 1186 | 135 | 中度重复 |
| W_feed_ranking | 1189 | 122 | 中度重复 |
| W_stream | 1195 | 160 | 中度重复 |
| W_mlp_light | 1186 | 189 | 中度重复 |
| W_interest_graph_recall | 1188 | 191 | 中度重复 |
| W_chase_dram | 1197 | 451 | 较好 |
| W_fp_lite | 1186 | 657 | 较好 |
| W_ads_ctr | 1191 | 743 | 较好 |
| W_phased_mix | 1199 | 715 | 较好 |

**结论**：v6.1 总 16710 sample 的有效信息量 ≈ 3470 个独立窗口。

---

## 2. v7 数据集分层设计

| 层 | 角色 | workload 数 | 每 workload 窗口 | 是否进训练 |
|---|---|---|---|---|
| L1 机制锚点 microbench | 让模型见过极端机制 | 8-10 | ~200（去重后） | 是 |
| L2 pthread proxy app | 训练主体，覆盖业务模式 | 4 类 × 8-10 phase | ~400 | 是 |
| L3 真实应用 trace holdout | 最终泛化验证 | 3-5 | 1000+ | 否 |

总训练目标：~15000-18000 窗口 / **effective_n ≥ 8000**。

---

## 3. L1 机制 microbench（精简策略）

保留 v6.1 现有 microbench，但每个 cap 到去重后 ~200 窗口，使其作为"锚点"而非"主体"。

- 直接保留（effective_n ≥ 400）：W_chase_dram、W_phased_mix、W_fp_lite、W_ads_ctr
- 需重设计（effective_n < 200）：W_compute_int、W_false_sharing、W_branch_storm、W_int_div、W_indirect、W_mlp_light
  - 改造方式：把单一 input size 改成 5-8 个 input size 串接的 trace，引入 phase 切换
- 可暂时保留：W_stream、W_fp_compute_dense、W_feed_ranking、W_interest_graph_recall（后续被 proxy 取代）

---

## 4. L2 pthread proxy app（4 类）

通用框架：
- 单一二进制 + 宏开关编译多个变体
- pthread 启动 / 绑核 / barrier / m5op ROI 标记
- 参数化 working set / read-write ratio / stride / branch / sharing
- 每个 proxy 设计 20-40 个 phase 配置，Sobol/拉丁超立方采样

### 4.1 P_graph_recall（覆盖 W_interest_graph_recall + W_indirect 孤岛）
- CSR adjacency traversal
- Random walk
- Bitmap set/test/union（atomic 或 per-thread merge）
- BFS frontier expansion
- Sparse vector accumulate

### 4.2 P_ads_feature（覆盖 W_ads_ctr 高 rd_4 区域）
- Feature hash lookup
- Embedding gather（大 stride + cold + read-heavy）
- Rule filter（branchy + indirect call table）
- Score accumulation（store-heavy）
- Top-k 写出

### 4.3 P_ranking_mixed（覆盖 W_feed_ranking + W_fp_compute_dense 过渡区域）
- Dense GEMM-like
- Sparse gather + dense compute
- Branchy 后处理（quantile, top-k heap）
- Cross-thread reduction
- Output buffer store

### 4.4 P_cache_kv（覆盖 W_false_sharing + pointer-chasing）
- Hash probe
- Pointer chasing
- LRU metadata update
- Lock striping
- Shared counter atomic 热点

每个 proxy 跑通后必须满足：`effective_n ≥ 500 / 全部 phase 配置加起来`。否则改驱动重采。

---

## 5. L3 真实应用 trace holdout（不参与训练）

候选 3-5 个：
1. 真实 ads CTR 推理 proxy（tflite 量化模型 + 自写 feature pipeline）
2. 真实图召回 proxy（开源 GNN inference 单线程导出 trace）
3. 真实 KV / cache 服务 proxy（memcached / Redis GET-heavy）
4. 真实日志聚合 proxy（fluent-bit 风格 parse + group-by）
5. 可选：Lucene-style 倒排索引查询

接入方式：单独构造 holdout windows，**不进训练**。每次新 proxy 加入前先做 OOD 距离检查（cross_p50 < 2.0），不达标则继续调 proxy。

---

## 6. 切窗去重（v6.2 已落地，v7 沿用）

为避免"1200 窗口里 1190 重复"的情况，在 `data/build_windows.py` 写入 jsonl 前增加去重：

- 用每窗口的 35 维特征向量（scalar 16 + rd_hist 9 + stride_hist 10）
- 在每个 workload 内部 z-score 归一化
- NN 距离 < THRESHOLD 视为重复，每簇只保留 1 个代表
- THRESHOLD 默认 0.05（保守，只删几乎完全一致的窗口）
- 输出 dedup 报告：原始 n / 保留 n / 重复率

CLI 新增参数：
- `--dedup-threshold FLOAT`：默认 0.05，设为 0 关闭
- `--dedup-jobs INT`：并行 workload 数

去重发生在 per-workload cap 之前，避免把代表样本误删。

---

## 7. v6.2 数据集（去重后的 v6.1）

立即可做：基于现有 v6.1 raw trace 重 build，加入去重 → v6.2。

预期：
- 总 sample 数从 16710 降到 ~4000-6000
- effective_n 从 3470 升到 ~3500-4500（去重后理论上不会再下降）
- 训练时 batch 多样性显著提升

预期不能解决：
- 孤岛问题（cross_p50 大的负载不会因为去重就被拉近）
- 整体特征空间覆盖（4 类 proxy 加入后才会改善）

---

## 8. 训练 + 评估闭环

每轮迭代固定流程：

1. `data/build_windows.py --dedup-threshold 0.05` build 训练集
2. `scripts/analyze_window_duplication.py` 检查 effective_n
3. `scripts/diagnose_v6_1_distribution.py` 检查 OOD / 孤岛
4. 不达标 → 改 proxy / microbench → 回 1
5. 达标 → DDP8 训练 → 跑 holdout 评估
6. 误差归因：误差大的窗口 → 看是孤岛（NN dist 大）还是机制错（NN dist 小但 label 偏）
7. 决定下一轮补哪类 proxy

---

## 9. 里程碑

| 时间 | 里程碑 | 验收 |
|---|---|---|
| 当前 | 实现 build_windows 去重 | 跑 v6.2 dedup 报告 |
| +1 周 | v6.2 训练 + 对比 v6.1 | DDP8 跑通，holdout 误差降低或持平 |
| +2 周 | P_graph_recall + P_cache_kv 上线 | effective_n ≥ 500 / 类 |
| +4 周 | 4 类 proxy 全部上线 → v7 训练集 | cross_p50 < 3.0 / 训练集 |
| +6 周 | 3-5 个真实应用 trace holdout | OOD 检查通过 |
| 持续 | 每月 OOD scan | 按结果补 proxy |

---

## 10. 风险与对策

| 风险 | 对策 |
|---|---|
| pthread proxy 写出来还是同质 | 每个 proxy 完成后立刻跑 dedup，effective_n < 200 改驱动 |
| gem5 启动开销大 | m5op 严格圈 ROI |
| 真实应用 hot path 提取困难 | 优先选 single-binary 形态（memcached、SQLite、wrk） |
| 去重阈值过严删掉有用样本 | 默认 0.05 偏保守，配合加权采样可进一步调 |
| 孤岛持续存在 | 优先补 P_graph_recall（同时打掉 W_indirect + W_interest_graph_recall） |

---

## 11. 当前要做的事（优先级降序）

1. 实现 `data/build_windows.py` 的窗口去重（v6.2 build）
2. 用去重后的 v6.2 跑一版 DDP8 训练，验证去重本身的收益
3. 实现 L2 P_graph_recall proxy（最高 ROI，打掉两个孤岛）
4. 实现 L2 P_cache_kv proxy
5. 实现 L2 P_ads_feature + P_ranking_mixed proxy
6. 接入 3-5 个真实应用 trace 作为 holdout
