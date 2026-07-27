# v29 长历史特征改进与 60k 训练实施方案

更新时间：2026-07-24

> 后续评估表明，全局 long-history residual 会同时扰动非访存 token。新的
> memory-gated 双 timing head 方案见
> [v29_memory_gated_timing_heads_design.md](v29_memory_gated_timing_heads_design.md)。
> 本文保留为已经执行的 long-history 训练方案和实验记录。

## 1. 结论

当前优先方案不是把 256-UOP 窗口直接扩成 512，而是在保留 `K=256`
full-QKVR 主干的前提下，增加一个轻量、严格前缀化的 long-history
sidecar，并通过零初始化 residual adapter 注入模型。

这解决的是当前最明确的缺口：模型能看到局部 256 UOP 的指令、依赖和资源关系，
但不能可靠识别 1K 到 64K memory reference 尺度上的工作集、页 churn、稀有长尾和
远距离复用。Redis heldout 的误差更符合这一类条件分布偏移，而不是标签超出训练范围，
也不是模型参数量不足。

当前正式训练 cache 已建好：

| 项目 | 结果 |
|---|---:|
| 唯一训练 trace | 80 |
| train manifest 记录 | 80/80 带 sidecar |
| validation manifest 记录 | 64/64 带 sidecar |
| 新增磁盘空间 | 0.212 GiB（`du` 显示 228 MiB） |
| 首次构建耗时 | 32.8 秒，16 workers |
| 重复构建 | 幂等检查后约 0.1 秒 |
| cache 质量 | PASS，严格 prefix，future UOP = 0 |

正式 manifest：

`data/v29_long_history_dataset/manifest.json`

## 2. 为什么先不扩展到 K=512

`K=512` 会改变 token 主干和位置嵌入，并使单核 full-QK 的注意力计算量约增至
原来的 4 倍；在 c32 下，跨核 key 数也从 7,936 增至 15,872。它需要重新建立
主 cache、修改当前 `K=256` 的硬契约，并重新训练。

long-history sidecar 不把 64K 历史 token 送进 Transformer，只把滚动统计压成
每核 40 个标量。因此：

- full-QKVR 仍处理 256 个局部 UOP；
- 不增加 attention 序列长度；
- 可以直接复用现有 raw/oracle cache；
- 能先验证 Redis 误差是否确实来自长历史可观测性。

只有在 long-history 特征的 residual probe 显示 256-UOP 内仍缺少有序依赖结构时，
才进入 `K=512` 对照实验。

## 3. 新特征定义

sidecar 每 256 UOP 保存一次 checkpoint。对 cursor `c` 的查询只允许读取不晚于
`c` 的最近 checkpoint，统计范围严格为 `[0, c)`。最多产生 255 UOP 的统计滞后，
不会读取未来信息。

### 3.1 每核 32 维离线特征

在 1K、4K、16K、64K memory-reference 四个尺度上，各计算 6 个量：

| 特征 | 含义 |
|---|---|
| line unique fraction | 窗口内 distinct line 数 / memory reference 数 |
| page unique fraction | 窗口内 distinct page 数 / memory reference 数 |
| rare-line reference fraction | 只出现 1 到 2 次的 line 所占访问比例 |
| first-touch line fraction | 该窗口内首次进入整个 trace 历史的 line 比例 |
| far-reuse fraction | reuse gap 不小于当前尺度的访问比例 |
| memory density | memory reference 数 / 覆盖的 UOP span |

四个尺度共 24 维，另加 8 维：

- prefix memory-reference 总量；
- prefix distinct line 和 distinct page；
- 4K 窗口内 reuse-gap 的 p50、p90、p99；
- 4K 和 64K 页工作集相对 DTLB 容量的压力。

计数和比率在写入前归一化，sidecar 采用 float16；模型读取时转成 float32。

### 3.2 每步 8 维跨核聚合特征

对 64K 尺度的 line unique、page unique、rare-line 和 memory density，计算：

- 当前 active cores 的均值，共 4 维；
- 本核值相对 active-core 均值的比例，共 4 维。

这是 permutation-invariant 的当步聚合，不需要追踪 cache/TLB/DRAM 的递归状态。

## 4. 模型改动

主模型保持 8 层 full-QKVR 和 `K=256`。新增分支为：

`40维 long history -> LayerNorm -> Linear(40,128) -> GELU -> Linear(128,960)`

最后一层权重和 bias 均初始化为 0，然后以 residual 形式加到每核 token hidden
state。这样新增分支在初始化时不会给主干引入随机偏置，但能从第一步开始学习。

| 项目 | 原 v29 | long-history v29 |
|---|---:|---:|
| 参数量 | 112,206,324 | 112,335,492 |
| 新增参数 | - | 129,168 |
| 参数增幅 | - | 0.115% |
| attention 长度 | 256 | 256 |

checkpoint 合约会记录完整 long-history 特征名、维度、尺度和查询规则。旧 v29
checkpoint 与新 cache 不允许静默混用；本轮 60k 应从头训练。watchdog 只会在
本轮新 checkpoint 内自动续训。

## 5. 运行时开销

Redis base c32、200 个真实 context、重复 3 次的 CPU context-builder 实测：

| 重复 | baseline | long history | 增幅 |
|---|---:|---:|---:|
| 1 | 4.8724 s | 4.9113 s | 0.80% |
| 2 | 4.6888 s | 4.7209 s | 0.69% |
| 3 | 4.6821 s | 4.7269 s | 0.96% |

模型只新增约 0.13M 参数的 per-core MLP，相对于 112M full-QKVR 主干很小。预计：

- 训练/推理总吞吐下降约 1% 到 3%；
- 不需要全局递归状态机；
- 不会把计算复杂度变成随 64K 历史长度增长；
- sidecar mmap 查表是常数开销。

未来若加入精确 virtual cache、TLB 或 DRAM queue，才需要独立的部署状态机；本版
不包含这些有状态机制。

## 6. 一键构建 cache

首次构建或幂等校验：

```bash
cd /data00/yinhaolang/TCSim
bash scripts/build_v29_long_history_cache.sh
```

默认设置：

- base manifest：`data/v29_global_time_dataset/manifest.json`
- 输出：`data/v29_long_history_dataset`
- splits：`train,validation`
- workers：16

需要强制重建时才使用：

```bash
bash scripts/build_v29_long_history_cache.sh --overwrite
```

## 7. 一键启动 8 卡、60k、watchdog 训练

GPU 驱动恢复且 0 到 7 号卡可用后，执行：

```bash
cd /data00/yinhaolang/TCSim
bash scripts/launch_v29_long_history_100m_60k_watchdog.sh
```

默认训练参数：

| 项目 | 值 |
|---|---|
| GPU | `0,1,2,3,4,5,6,7` |
| DDP process | 8 |
| step | 60,000 |
| AMP | BF16 |
| checkpoint interval | 500 step |
| validation interval | 1,000 step |
| watchdog interval | 60 秒 |
| stall 判定 | 3,600 秒无日志更新 |
| 最大重启 | 20 |
| 输出目录 | `ckpt/tcsim_v29_long_history_100m_8gpu_60000` |

watchdog 会：

1. 检查 `last.pt` 和 `best.pt` 的 step；
2. 自动选择 step 最大的有效 checkpoint；
3. 训练异常退出或日志 stall 后，从最新 checkpoint 恢复；
4. checkpoint 达到 60,000 step 后退出；
5. 连续 3 次没有 checkpoint 进展则停止并报错，避免无限重启。

查看状态：

```bash
tail -f logs/watchdog/tcsim_v29_long_history_100m_8gpu_60000_watch.nohup.log
tail -f logs/tcsim_v29_long_history_100m_8gpu_60000_watch.current.log
```

自定义卡号和输出目录：

```bash
GPUS=0,1,2,3,4,5,6,7 \
OUT=ckpt/my_v29_long_history_60k \
RUN_NAME=my_v29_long_history_60k \
bash scripts/launch_v29_long_history_100m_60k_watchdog.sh
```

## 8. 预期训练时间

同一 8 卡 v29 100M 主干的历史实测为：

- 60,000 step；
- 37,293.6 秒；
- 即 10 小时 21 分 34 秒。

新增分支不扩大 attention，context-builder 实测增幅低于 1%。考虑 DataLoader、
验证、checkpoint 写盘和运行波动，本轮估计：

| 口径 | 预计时间 |
|---|---:|
| 无重启、8 卡健康 | 10.5 到 11.5 小时 |
| 含一般系统波动的保守预算 | 11 到 13 小时 |
| cache 构建 | 已完成，不计入训练时间 |

当前 `nvidia-smi` 报告无法与 NVIDIA driver 通信，因此现在不能开始可信的 8 卡
计时，也不应直接启动 watchdog。上述时间从 GPU 驱动恢复、8 卡可见并执行启动命令
时开始计算。

## 9. 当前实验顺序和验收标准

### E0：cache 和训练链路

状态：完成。

- 80 个 trace sidecar 全部 PASS；
- 严格 prefix/no-future 测试通过；
- 真实 cache 的 context、collate、模型 forward 通过；
- 正式 manifest 的 1-step CPU 训练 smoke 通过。

### E1：long-history 主实验

从头训练 60k。每 1k step 观察 validation，保存 best 和 last。

训练 sampler 使用 coverage-first 策略：

1. 第一个 44,920 step 由 8 个 rank 对 359,354 个 sequence 做一次协同的
   全局无放回遍历；为对齐 DDP，每轮最多只有 7 个 padding duplicate。
2. 剩余 15,080 step 恢复按 trace 等权的有放回抽样。
3. checkpoint resume 根据 step 还原 epoch 和本 rank 的精确 offset，不会因
   watchdog 重启而从覆盖排列头部重新抽样。

验收：

- 训练稳定，无 NaN、无 checkpoint contract mismatch；
- 吞吐相对基线下降不超过 5%；
- seed0 base 总体误差不能显著退化。

### E2：Redis heldout 定位实验

对 Redis base/heldout 的 c4、c8、c16、c32 运行相同 seed 和相同 rollout 设置，
同时报告：

- CPI 相对误差；
- long-history 特征分布和 JS divergence；
- 按 footprint、page churn、rare tail、cross-core density 分桶的 residual。

目标不是只让一个 Redis trace 变好，而是验证误差是否随机制维度系统性缩小。

### E3：ordered-feature residual probe

按固定顺序做消融：

1. baseline v29；
2. 加 memory density；
3. 加 line/page working set；
4. 加 rare/first-touch tail；
5. 加 reuse quantile/far reuse；
6. 加 active-core aggregate。

这就是 memory-seq ordered-feature residual probe：主干和训练设置固定，只逐组开放
可解释特征，观察 heldout residual 在哪一组后下降。

### E4：Redis mechanism cube

用正交机制轴构造配对数据，而不是加入 `is_redis` 或 workload ID：

- footprint/page churn：低、中、高；
- sharing：private、read-shared、write-shared；
- access/contention：sequential、random、hot-skew/conflict。

训练和 heldout 必须按机制组合切分。若收益能迁移到 MySQL、Flink、GoFeed 和
synthetic memory workloads，说明模型学到的是通用机制；若只改善 Redis 名称对应
的 trace，则判定为过拟合。

### E5：K=512 对照

只在 E1 到 E4 表明长历史统计仍无法解释有序局部 residual 时执行。它需要新主
cache、新模型位置嵌入和完整重训，不是当前第一优先级。
