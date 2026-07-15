# v28 业务导向负载与数据集合同

> 2026-07-15 更新：模型输入分组、全退休分支 miss 定义、per-core ROI gate、完成核
> quiesce 和新 raw schema 以
> [`v28_1_functional_feature_and_trace_contract.md`](v28_1_functional_feature_and_trace_contract.md)
> 为准。旧 v28 raw/cache/checkpoint 不得与新合同混用。

## 1. 决策

v28 面向后续部署侧需要仿真的七类业务：Marine 在线搜广推、gofeed 在线
微服务、Flink 离线/流式大数据、MySQL、Redis、PyTorch AI 机头和 BVC
Encoder。

训练集合固定为 **16 个 workload**：9 个机制锚点加 7 个业务 base proxy。
业务 heldout 另有 7 个，不占 16 个训练配额。v27 的纯 DRAM 饱和、无限
pointer chase 和全核热点写只作为 stress suite，不进入训练、开发验证、业务
heldout，也不参与 checkpoint 选择。

所有 v28 workload 保持 cold-start 全 trace：不删除前几个窗口，不裁剪高 CPI
标签。避免异常 CPI 的方式是修正 workload 的访存占空比、MLP 和工作集，而不是
后处理标签。

## 2. 目标硬件基线

`arch_A/A1_server32` 固定为一套保守的单路 32 核服务器基线；c1/c4/c8/c16/c32
只改变活跃核心数，不随核心数缩小共享缓存或内存通道：

- 3 GHz，32 KiB L1I + 32 KiB L1D，每核 1 MiB 私有 L2；
- 8 个 LLC bank，每 bank 8 MiB，共 64 MiB、16-way；
- 8 通道 DDR4-2400，每通道 16 bank，64 B 交织；
- gem5 的 `--l3-size` 是每 bank 容量，不是 LLC 总容量。

采集器必须显式传递这些参数，且每份 raw 的 `uarch_profile.json` 必须记录
`l2.size_b=1048576`、`l3.size_b=67108864`、`l3.num_banks=8` 和
`dram.num_channels=8`。旧 A0（256 KiB L2、8 MiB 总 LLC、单通道）数据不得与
A1 数据混合训练。

## 3. ROI 语义边界

ROI 外允许：内存分配、数据初始化/first touch、线程创建、线程绑核和一次起始
barrier。ROI 内禁止：atomic、lock、barrier、spin、futex、yield、sleep、显式线程
调度以及等待其他线程进度。

业务 proxy 使用大型只读共享模型/表和每核小型私有可写状态。唯一 coherence 机制锚点
`coh_readmostly_sparse` 读取共享只读区域，并让每核低频写自己独立的 slot；slot
按 cache line 隔离，不存在两个核写同一个 C 对象、同一 cache line 或同步语义。

这意味着模型学习的是业务主体执行的 compute/cache/DRAM/普通 coherence 开销，
不包含锁竞争、原子 RMW、线程唤醒和调度等待。部署评测也必须采用相同边界。

每个核心的 `WORKBEGIN/WORKEND` 是独立边界：该核自己的 `WORKEND` 后立即停止
采集并进入 `m5_quiesce`；第 N 个核心结束 ROI 时 gem5 直接终止。主线程
`pthread_join`、futex、析构和打印均不属于仿真 ROI，也不得继续干扰尚未完成的核。

线程天然共享同一份可执行代码页，但 kernel 仍较小，不能完整代表真实服务的大代码/
动态库指令足迹。数据侧采用以下强合同：base 每核只有 64 KiB、heldout 每核 128 KiB
的请求状态和输出；业务主体读取每进程唯一的 8–64 MiB 不可变共享模型/表；base 使用
确定性的 log-binned Zipf(s≈1) 读，heldout 在更大共享表上保留 Zipf 主体并加入
1/16 均匀尾部；ROI 内禁止写共享表。采样按对数 rank 桶完成，不需要 CDF、浮点、
LUT 或拒绝循环。

## 4. 16 个训练 workload

| 组 | workload | 作用 |
|---|---|---|
| Compute | `v28_int_alu_dense` | 整数吞吐锚点 |
| | `v28_int_div_serial` | 有限串行整数长延迟锚点 |
| | `v28_fp_alu_dense` | scalar FP multiply/add |
| | `v28_simd_sse_dense` | SSE2 packed FP；不依赖 AVX/FMA decoder |
| Cache | `v28_cache_L1_mixed` | 每核 16 KiB，计算与低频私有写混合 |
| | `v28_cache_L2_mixed` | 每核 128 KiB，作为稳定的 L2-hit 延迟锚点 |
| Memory | `v28_memory_seq_moderate` | 每核 2 MiB，多路顺序读与计算交错 |
| | `v28_memory_random_mlp` | 每核 2 MiB，4 路独立随机 load 与计算交错 |
| Coherence | `v28_coh_readmostly_sparse` | 共享只读 metadata + 每核独立低频写 slot |
| Business | `v28_marine_base` | 32 MiB 共享 embedding，Zipf gather、特征交叉、打分和数据相关分支 |
| | `v28_gofeed_base` | 8 MiB 共享内容/路由表、请求解析、hash 和私有结果写出 |
| | `v28_flink_base` | 16 MiB 共享 event/dimension 表、filter、每核 keyed state 更新 |
| | `v28_mysql_base` | 32 MiB 共享只读 buffer snapshot、有限三层索引读和每核 txn 输出 |
| | `v28_redis_base` | 16 MiB 共享只读 key/value 表、两个候选 bucket 和每核 reply 输出 |
| | `v28_pytorch_base` | 32 MiB 共享只读权重、SSE2 gather/compute 和每核 activation 输出 |
| | `v28_bvc_encoder_base` | 16 MiB 共享只读 frame/reference、SAD-like 代价和每核 tile 输出 |

`memory_seq_moderate` 和 `memory_random_mlp` 用于提供可控 DRAM 压力，但不采用
纯 load 流；每轮都有多个独立内存请求和足量计算。训练集合不包含无限 dependent
pointer chain。

## 5. 七个业务 heldout

每个部署 family 都有一个模型训练期间完全不可见的业务合理变体：

| heldout | 相对 base 的变化 | 不变约束 |
|---|---|---|
| `v28_marine_heldout` | 共享模型 32→64 MiB，加入 1/16 均匀尾部 | 多路 gather + 打分，非 chase |
| `v28_gofeed_heldout` | 共享表 8→16 MiB、请求步长和输出频率改变 | 共享只读表，无调度/IPC |
| `v28_flink_heldout` | 共享表 16→32 MiB、filter 选择率改变 | 每核 128 KiB 状态，无 barrier |
| `v28_mysql_heldout` | 共享 snapshot 32→64 MiB、Zipf 尾部提高 | 最多三层依赖，只写每核 txn |
| `v28_redis_heldout` | 共享表 16→32 MiB、value 长度和尾部提高 | 共享只读表、每核 reply，无锁 |
| `v28_pytorch_heldout` | 共享权重 32→64 MiB、算术深度改变 | SSE2，只写每核 activation |
| `v28_bvc_encoder_heldout` | 共享 frame 16→32 MiB、reference/mode 改变 | 只写每核 tile，无任务队列同步 |

这些 heldout 用于最终业务泛化报告，不用于 early stopping、loss 权重、epsilon 或
workload 参数回调。若据其结果修改模型，该轮 heldout 即失效，必须重新定义未见
变体。

## 6. 数据切分

```text
train      = 16 base workloads × {c1,c4,c8,c16,c32} × seed0
dev        = seed0 base workloads 的完整 oracle-context 行做固定 90/10 hash 切分
test-biz   = 7 heldout workloads × {c1,c4,c8,c16,c32} × seed0
deployment = 模型冻结后，再运行 seed1；不得参与训练、dev 或选 checkpoint
stress     = v27 chase/pure-DRAM/hot-write，仅报告边界，不参与模型选择
```

训练采样先均衡三个层次：机制锚点/业务 proxy 两大组、组内 workload、core count。
不能按 raw 窗口总数直接混合，否则长 trace 或 c32 resident context 会获得过高权重。

## 7. 采集放行门槛

每个 workload × core-count 必须满足：

- 每核记录数在 `[500k, 1M]`，自然 ROI 完成，不能 collector 中途截断；
- trace 中 `is_atomic == 0`；ROI 不出现同步/调度函数；
- 每个 core 的 full-trace CPI、跨核 full-trace mean 和 sampled CPI p50 均不超过 10；
- sampled CPI p99 不超过 10，`CPI >= 10` 的 chunk 比例不超过 `1%`；
- 不允许出现 sampled `CPI >= 40`；
- memory/business 类在 c16/c32 应出现可测的 LLC/DRAM 压力变化，但不能靠单链
  依赖或全核争同一 cache line 达成；
- base/heldout 以及不同机制 workload 的 normalized functional signature 不得碰撞。

这些是首次 gem5 probe 后的硬门槛。未通过时只调整该 workload 的访问占空比、
hot/cold 比例、工作集或独立 stream 数；不 clip CPI，不删除 cold prefix，也不把
失败数据写入 tensor cache。这里的 CPI 是 `cycles/committed uop`；单条指令或不足一个
chunk 的瞬时停顿不定义为 workload CPI，但任何核心的全 ROI 均值超过 10 都硬拒绝。

实现位置：`/data00/yinhaolang/TSim/workloads/v28/`；机器可读名单位于
`configs/v28_business_workloads.json`。
