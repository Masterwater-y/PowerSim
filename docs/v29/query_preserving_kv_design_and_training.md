# v29 Query-preserving remote-K/V compression

更新时间：2026-08-02
实现状态：代码、配置、cache 审计、CPU/CUDA 微基准和 1-step DDP smoke 已完成；等待正式
60K 从头训练。

## 1. 目标与设计边界

上一版 `hierarchical_latent` 同时压缩 source 和跨核 Query，在 8 层中执行
latent-to-latent attention，再把结果广播回 UOP。它把 c32/base 吞吐量提高到约
187 K UOP/s，但 base ROI-CPI mean 退化到 4.57%，heldout 为 16.59%。完整结论见
`docs/v29/latent32_scratch_results.md`。

新后端 `query_preserving_kv` 只压缩其他核心的 K/V：

```text
local path（8/8 层）
    Q[c,0:256] -> K/V[c,0:256]

cross path（仅第 4、8 层）
    source K/V[c,0:256] -> 16 K/V anchors[c]
    target R[c,j] -> anchors[all other cores]
```

每个 target UOP `R[c,j]` 始终独立存在，直接查询所有其他核心的 anchors。实现中没有
anchor-to-anchor attention，也没有 latent broadcast。原 relation/state `cross_gate` 仍控制
cross residual；不增加额外 scalar gate，不使用 teacher，不引入 GSS timing 特征。

## 2. Hybrid K/V anchors

每个 source core 导出 16 个 K/V anchor：

1. 8 个 positional anchor：将 256 UOP 按程序顺序分为 8 个连续 patch，分别对有效 K/V
   做 masked mean；partial/tail patch 携带 validity mask。
2. 8 个 learned content anchor：8 个跨 core 共享的 seed Query 对 source K 计算一次
   attention distribution，同一组权重同时聚合 source K 和 V。

共享 seed 和固定 patch 定义不依赖 core ID，保持 core permutation equivariance。当前首版
不用 raw opcode/address 人工分组，因此不改变 cache/input contract；如果首轮仍在
coherence workload 上失败，再增加可部署的 load/store/branch/dependency sparse bypass，
不能读取 path_class 或未来 PMU label。

稳定性处理：

- content seed、source K、target R、输出 anchor K 使用独立 LayerNorm；
- 训练时两个 selected cross layer 的 anchor pooling 和 target-to-anchor attention 使用
  FP32，其他 backbone 保持 BF16；
- content softmax 在 FP32 计算并显式重新归一化，对空 mask 不产生 NaN；
- c1 batch 保留所有 anchor 参数和 norm 的零梯度边，兼容 DDP；
- 训练器继续在 forward、DDP backward/all-reduce、optimizer 和 checkpoint 保存前执行
  finite guard。

## 3. 层布局与复杂度

正式配置保留 8 个 local-QKV/FFN block，只在第 4、8 层启用 cross：

```text
layer 1  local_only
layer 2  local_only
layer 3  local_only
layer 4  query_preserving_kv
layer 5  local_only
layer 6  local_only
layer 7  local_only
layer 8  query_preserving_kv
```

c32、K=256、M=16 时，单个 cross layer 的 target-to-anchor score 元素为：

```text
32 * 256 * 31 * 16 = 4,063,232
```

即每个 target UOP 的远端 Key 数从 `31*256=7936` 降到 `31*16=496`。若把 anchor
构造按保守的 `C*M*K` 一并计入，单层为 4,194,304，较 full cross 的 65,011,712
减少 93.55%。两层合计约 839 万，仍少于上一版 latent32 八层合计的约 1232 万。

模型参数量为 112,237,044；未选择 cross 的 6 层不创建 anchor 参数。

## 4. 已完成验证

统一验证脚本：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  scripts/benchmark_v29_query_preserving_kv.py
```

CPU 门禁：

- partial source window forward/backward 有限；
- 所有 anchor Query/LayerNorm 参数都有有限梯度；
- 修改一个 target Query 时，其他 target 输出变化严格为 0；
- v29 interaction 的 layer backend 严格为
  `local,query,local,query`（4-layer 缩小 smoke）；
- 所有 interaction 参数均获得有限梯度。

H20、BF16、c32/K256/D960/15 heads 微基准：

| cross backend | median |
|---|---:|
| legacy full R/K/V | 2.5598 ms |
| query-preserving K/V16 | 1.2338 ms |

最终复验的单个 cross layer 加速为 2.075x；两轮实测范围为 2.07～2.13x。虽然它比
aggressive latent32 的约 0.555 ms 更慢，但新模型
只有 2 个 cross layer，而 latent32 有 8 个，所以全模型 cross 分支预算更低。

正式配置还通过了 1-step、单 H20 的 `torchrun` smoke：cache、模型构造、全参数
AdamW、warmup、BF16/FP32 forward、DDP backward、finite guard 和 checkpoint 保存全部完成，
无 NaN；该 smoke checkpoint 已删除。

## 5. 训练 cache

正式训练直接复用已构建且通过质量门禁的 cache：

```text
data/v29_global_time_dataset/manifest.json
```

启动前会自动运行 `scripts/audit_v29_query_kv_train_cache.py`。当前审计结果：

| split | traces | workloads | core counts |
|---|---:|---:|---|
| train | 80 | 16 | 1,4,8,16,32 |
| validation | 64 | 16 | 4,8,16,32 |

训练/验证只包含 base workload；正式 `development_heldout` 和
`deployment_inference` 不进入训练。重叠 cache 必须使用相同策略下互斥的 block-level
train/validation partition。

独立审计命令：

```bash
/data00/yinhaolang/infer/.venv/bin/python \
  scripts/audit_v29_query_kv_train_cache.py \
  --manifest data/v29_global_time_dataset/manifest.json
```

## 6. 一键 60K 训练

配置：`configs/v29_query_kv16_scratch_60k.yaml`。所有 112,237,044 个参数随机初始化并从
step 1 端到端更新；30K 永久保存 `step_30000.pt`，60K 写入 `last.pt`，按 validation
保存 `best.pt`。

```bash
cd /data00/yinhaolang/TCSim
bash scripts/launch_v29_query_kv16_scratch_60k_nohup.sh
```

默认输出：

```text
ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_60k
```

启动器会打印实际日志和 PID 文件。查看进度：

```bash
tail -f logs/v29_query_kv16_scratch_60k_*.nohup.log
```

先跑 5K 稳定性 pilot 时应使用独立输出目录，避免把 pilot 和正式 run 混在一起：

```bash
STEPS=5000 \
OUT=ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_pilot5k \
bash scripts/launch_v29_query_kv16_scratch_60k_nohup.sh
```

中断后严格续训：

```bash
RESUME_CKPT=ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_60k/last.pt \
OUT=ckpt/tcsim_v29_query_kv16_scratch_100m_8gpu_60k \
bash scripts/launch_v29_query_kv16_scratch_60k_nohup.sh
```

## 7. 正式门禁

不能只按 sequence validation MAE 选择最终方案。至少检查：

1. 5K 内所有 rank 无 non-finite，anchor Query 梯度持续非零；
2. 30K checkpoint 先跑 `coh_readmostly_sparse` c4/c16/c32 和 Redis/BVC/PyTorch
   heldout c4/c32；
3. c32/base workload-macro ROI-CPI mean 目标不高于 3.5%；
4. `coh_readmostly_sparse` c32 首轮目标低于 8%；
5. heldout c32 mean 首轮目标 8%～12%，Redis 不再维持约 35% 系统偏差；
6. native context + fused QRKV 的 c32/base 吞吐量目标 160～190 K UOP/s；最终数值以
   full-trace rollout 为准，不能用 attention 微基准外推为结论。
