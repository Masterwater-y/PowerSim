# Inference-Only 泛化验证负载

这 3 个负载用于模型**推理验证 / 泛化性评估**，不进入当前训练集。它们不是单一
microbench，而是更贴近字节常见业务链路的“特性负载”。设计目标：

1. 更接近真实应用，而不是只放大某一类单点瓶颈。
2. 使用 `pthread` 多线程，线程与核 `1:1` 绑定。
3. ROI 口径与现有 workload 完全一致：每个 worker 在 barrier 之后进入
   `m5_work_begin / m5_work_end`，采集端继续使用 `--require-roi`。
4. 每个负载只暴露一个 `scale` 参数控制数据规模，便于 probe 和正式采集统一。
5. 不修改训练脚本；后续只作为 inference baseline / validation workload 使用。

## 负载列表

### 1. `feed_ranking`

应用原型：信息流 / 短视频推荐排序。

每个线程独立执行 request stream：

- sparse id embedding gather：不规则只读访存
- feature interaction：中等算术强度
- rerank branch：根据 score 路径分化

覆盖特征：

- cache / TLB miss
- mixed int/fp arithmetic
- 中等分支压力
- 低跨线程耦合

`scale` 含义：

- embedding 表大小与请求数同时按比例增长

### 2. `ads_ctr`

应用原型：广告 CTR / CVR 预估服务。

每个线程独立处理 impression stream：

- sparse feature hash lookup
- dense cross feature
- calibration / filter branch

覆盖特征：

- cache-sensitive sparse lookup
- mixed arithmetic + branch
- 顺序与随机访存混合
- 低跨线程耦合

`scale` 含义：

- sparse feature 词表规模与 impression 数同时增长

### 3. `interest_graph_recall`

应用原型：兴趣图 / 关系图召回。

每个线程私有 CSR 图，执行多跳 neighbor sampling：

- 邻接表不规则访问
- 节点状态条件分支
- 轻量打分更新

覆盖特征：

- irregular memory access
- branch-heavy control flow
- TLB / LLC 压力
- 低共享、强单线程时空不规则性

`scale` 含义：

- 顶点数、边数和 walk 数随 `scale` 线性增长

## 训练隔离

当前训练脚本只链接固定 `train8`：

- `W_branch_storm`
- `W_chase_dram`
- `W_compute_int`
- `W_false_sharing`
- `W_indirect`
- `W_int_div`
- `W_phased_mix`
- `W_stream`

新加的 3 个负载不会自动进入训练。

## 构建

`workloads/Makefile` 会自动编译所有 `workloads/src/bench_*.c`：

```bash
cd /data00/yinhaolang/LLMSim/workloads
make feed_ranking ads_ctr interest_graph_recall
```

## 采集

使用独立包装脚本，保持与旧数据同 ROI/采集口径，但输出到独立目录：

```bash
cd /data00/yinhaolang/LLMSim
bash scripts/collect_infer3_500k.sh
```

默认输出目录：

- `data/raw_infer3_8c_500k`

生成后可继续：

1. `scripts/convert_trace_to_aligned_parquet.py`
2. `scripts/export_roi_stats.py`
3. `eval/eval_quota_cycles.py`

用于推理验证与结果归档。
