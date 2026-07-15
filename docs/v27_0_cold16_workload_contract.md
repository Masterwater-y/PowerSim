# v27.0-cold16 负载与采集合同

**状态**：初版正式合同。它替代旧 raw-v27 的 16+2 定义；旧
`raw_v27_ffatomic_seed0_c{01,04,08,16,32}` 已删除；其历史审计文档仅作问题
复盘，不能用于本合同的训练或验证。

## 1. 范围

目标是预测 cold-start 条件下的 fixed functional chunk duration。每个 core 以
固定 `K=256` UOP 切 chunk；真实 commit tick 只产生 boundary-to-boundary label
与 oracle context，绝不进入模型输入或决定输入长度。

初版**不建模** atomic、lock、barrier、spin 或 happens-before。模型可学习普通
load/store 引起的 cache/coherence 行为，但不承诺同步语义。

采集合同固定为：

```text
AtomicSimpleCPU + atomic_noncaching 初始化
  -> 首个 WORKBEGIN 切换 O3 + Ruby
  -> 立即记录整个 workload ROI（不 warmup，不丢弃 prefix）
```

所以所有样本均是 cold-start；训练、验证、heldout 必须使用相同 `FF_ATOMIC=1`
和同一 ROI/scale/target-per-core 配方。

每个 workload 的每个 core 必须满足 `500,000 <= records <= 1,000,000`。
这是 workload 自身自然结束的 ROI 合同，不允许 collector 中途截断来凑数；严格
collector 会在 c01 发现任一 core 越界时删除该 workload 的 trace 并停止，绝不
进入下一 core-count 组。

采集产物的长期输入格式为每核 `*.aligned.parquet`。它只保留给 TCSim 构造
所需的 functional 字段与 `fetch_tick`/`commit_tick` label 边界；转换成功并经
per-core 文件数校验后，`records.micro.jsonl` 与 `labels.micro.jsonl` 可以删除。
TCSim 读取 parquet 时对白名单列做流式读取，真实 timing 仍只用于 label，绝不
作为模型输入。

## 2. 15 个训练负载

| 类别 | 负载 | 合同 |
|---|---|---|
| Compute | `int_alu_dense` | 独立整数 ALU 链，吞吐锚点 |
| | `int_div_serial` | 单一串行整数除法依赖链 |
| | `fp_alu_dense` | scalar FP multiply/add；不是 FMA |
| | `simd_sse_dense` | SSE2 packed FP arithmetic；不是 AVX/FMA |
| Memory | `stream_seq_L2` | 每核私有 256 KiB，逐 cache line 完整遍历 |
| | `stream_seq_DRAM` | 每核私有 16 MiB，逐 cache line 完整遍历 |
| | `random_DRAM` | 每核私有 8 MiB 随机 cache-line 访问 |
| | `chase_DRAM` | 每核私有 6 MiB、cache-line node 的 pointer ring |
| Coherence | `coh_read_share` | 所有核读同一 4 KiB 区域 |
| | `coh_write_share` | 所有核写同一 word/cache line |
| | `coh_false_share` | 各核写不同 byte、同一 cache line |
| | `coh_asym_rw` | core0 写、其余核读共享区域；无同步承诺 |
| Phase | `phase_coh_onset` | 私有计算段后进入 all-core shared-write 段 |
| Skew | `skew_hot_cold` | 25% core stream-DRAM，其余 core 轻 compute |
| Proxy mix | `ranking_mix_private` | 各核等量私有随机 gather + dense mixing + 数据相关小分支；不同 seed 仅改变访问/分支序列，快慢差来自 cold cache 与共享 LLC/DRAM 竞争 |

`stream_seq_L2` 与 `stream_seq_DRAM` 必须在 normalized functional signature 上
不同；DRAM/random/chase 工作集均按**每核**定义，不能按进程总量切分。

## 3. Heldout 与切分

`phase_coh_decay` 是 mechanism heldout，不得进入训练或
checkpoint 选择。

```text
train       = 15 workload x {c01,c04,c08,c16,c32} x seed0 x A0
dev-val     = seed0 同 15 workload x {c04,c08,c16,c32}；以完整 oracle-context
              sample 为原子单位，按固定 hash 切为 90% train / 10% dev-val
test-mech   = 1 heldout x {c01,c04,c08,c16,c32} x seed0 x A0
deployment  = 15 workload x {c01,c04,c08,c16,c32} x seed1 x A0（训练期完全隔离）
```

`dev-val` 只用于训练过程的 loss 监控和 checkpoint 选择，不是独立泛化结论。
每条 `rollout.jsonl` 行包含同一调度时刻全部 active-core 的 chunk，切分时整行
进入 train 或 dev-val，绝不拆分一个跨核 context。seed1 不得参与训练、验证、
early stopping 或 checkpoint 选择；最终部署推理仅在该 split 上报告。

旧 `activecore_train` 数据集不进入初版训练；它保留为后续 realism/proxy
训练或独立 proxy-family 泛化测试。

## 4. 采集前放行项

- 反汇编确认 serial-div、scalar-FP、SSE2 名称与实际指令一致；
- 每个 workload x core 的 trace/label 完整，boundary label telescope；
- phase 的逐段 CPI/访问范围方向正确；
- c01/c32 下 stream-L2/DRAM 不发生 functional collision；
- seed1 使用与 seed0 完全一致的 cold-start recipe，且只用于 deployment split。
