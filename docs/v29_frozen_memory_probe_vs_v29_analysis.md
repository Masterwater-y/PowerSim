# v29 Frozen Memory Probe 与原始 v29 配对分析

更新时间：2026-07-24

## 1. 结论

Frozen memory probe 的方向是正确的，但改进幅度不足。

- 120 条 trace 均可与原始 v29 一一配对，真实 ROI-CPI 标签完全一致。
- Redis heldout 的 8 个 seed × core 组合全部改善，但平均只改善 0.736 个百分点。
- Redis heldout 仍有 38.3% 至 45.2% 的 ROI-CPI 误差。
- 对全部 120 条 trace，平均误差仅从 6.253% 降到 6.193%，基本持平。
- business heldout 平均改善 0.392 个百分点，但 train/base 平均退化 0.232 个百分点。
- memory gate 成功限制了全局负迁移，但也把 Redis 所需的大额修正压缩得过小。

当前结果不能证明长期特征完全无用，但可以证明：

1. 当前 40 维长期特征不足以稳定区分 Redis base 与 Redis heldout 的服务代价。
2. 当前 10,000-step probe 尚未完成 coverage-first 的第一轮覆盖。
3. 当前 correction head 不只使用长期特征，也大量使用冻结主干的 token state，因此现有结果不能单独归因于长期特征。
4. 继续直接扩大正式训练规模之前，应先完成长期特征消融和残差可分性验证。

## 2. 对比输入

原始 v29：

```text
checkpoint:
  ckpt/tcsim_v29_packed3_100m_8gpu_60k/best.pt

report:
  logs/v29_packed3_free_s256_seed0_seed1_c04_c08_c16_c32_full/report.json

checkpoint step:
  59000
```

Frozen memory probe：

```text
checkpoint:
  ckpt/tcsim_v29_frozen_memory_probe_e2_10k_seed1234/best.pt

report:
  logs/v29_frozen_probe_best9500_seed1_plus_heldout_c04_c32_s256_8gpu/report.json

checkpoint step:
  9500
```

配对范围：

```text
deployment_inference, seed=1: 92 traces
development_heldout, seed=0: 28 traces
total: 120 traces
failed: 0
ROI UOP coverage: 100%
```

原始 v29 报告使用较早的 context builder 和 retirement reconstruction
实现，Frozen probe 使用当前实现。因此这不是完全相同推理代码下的严格 A/B。
但是：

- 120 条 trace ID 全部一致；
- 120 条真实 ROI-CPI 标签逐项完全一致，最大差值为 0；
- Redis 剩余误差约为 40%，远大于协议实现差异可能造成的轻微扰动。

所以该对比足以判断方案是否解决 Redis 问题，但不应把小于约 0.1 个百分点
的变化解释为显著收益。

## 3. 总体配对结果

| 分组 | trace 数 | 原始 v29 平均误差 | Frozen probe 平均误差 | 变化 |
|---|---:|---:|---:|---:|
| 全部 | 120 | 6.253% | 6.193% | -0.059 pp |
| seed1 deployment | 92 | 4.864% | 4.911% | +0.047 pp |
| seed0 development heldout | 28 | 10.815% | 10.406% | -0.409 pp |
| train/base | 64 | 2.245% | 2.477% | +0.232 pp |
| business heldout | 56 | 10.833% | 10.441% | -0.392 pp |

120 条中：

```text
改善: 59
退化: 61
```

这说明 Frozen probe 的总体效果接近零和：它改善了一部分 heldout，同时把一部分
base workload 推离原始 v29。

## 4. Redis heldout 结果

下表为 seed0 与 seed1 的平均值。

| cores | 原始 v29 误差 | Frozen probe 误差 | 改善 | 原始 v29 到真值还缺少的 CPI | probe 实际增加的 CPI |
|---:|---:|---:|---:|---:|---:|
| 4 | 45.440% | 45.140% | 0.300 pp | 1.1790 | 0.0078 |
| 8 | 43.043% | 42.554% | 0.489 pp | 1.1421 | 0.0130 |
| 16 | 41.101% | 40.293% | 0.809 pp | 1.1001 | 0.0217 |
| 32 | 39.719% | 38.374% | 1.345 pp | 1.0464 | 0.0355 |

Probe 对 Redis 的修正方向在 8 个组合上全部正确，但只恢复了所需残差的一小部分：

| cores | 已恢复的缺失 CPI 比例 |
|---:|---:|
| 4 | 0.66% |
| 8 | 1.14% |
| 16 | 1.97% |
| 32 | 3.39% |

因此问题不是修正符号错误，而是可见修正量远远不够。

## 5. 为什么 memory gate 会限制改进幅度

当前 correction 只注入 mem_kind 为 load、store 或 atomic 的 UOP。Redis heldout 的
长期窗口平均 memory density 约为 4.44%。

如果把原始 v29 的全部缺失 CPI 都放到这些 memory UOP 上，近似需要：

| cores | 每个 memory UOP 所需附加 cycle | probe 实际附加 cycle |
|---:|---:|---:|
| 4 | 26.56 | 0.18 |
| 8 | 25.73 | 0.29 |
| 16 | 24.79 | 0.49 |
| 32 | 23.58 | 0.80 |

该计算是基于全 trace CPI 差值与 memory density 的近似归因，不是逐 UOP oracle
分解。但数量级足以说明：当前 probe 只学到了所需 penalty 的很小部分。

这也解释了两个现象：

- 全局 long-history residual 能对 Redis 产生更大的 CPI 移动；
- 全局 residual 同时会修改非访存 UOP，因而在其他 workload 上产生严重负迁移。

已有 long-history 60k 结果中：

```text
Redis heldout mean error:
  original v29: 42.326%
  frozen probe: 41.590%
  global long-history 60k: 37.613%

all-120 mean error:
  original v29: 6.253%
  frozen probe: 6.193%
  global long-history 60k: 8.803%
```

所以 memory gate 确实起到了隔离作用，但当前形式的隔离过强，无法表达
“少量 memory UOP 导致大量可见 stall”的情况。

## 6. 长期特征是否能区分 Redis base 与 heldout

对每条 trace 的长期 sidecar 做等进度采样，然后计算 40 维长期特征均值。Redis
heldout 相对于 80 条训练 trace 的分布并不明显 OOD：

```text
Redis heldout feature z-RMS:
  about 0.41

largest stable single-feature deviation:
  about 1.07 standard deviations
```

Redis heldout 的最近训练邻居始终是 Redis base，其次是 MySQL base。

在相同 core count 下，Redis base 与 Redis heldout 的标准化特征距离约为
0.13 至 0.22，但两者的原始 v29 残差相差约 1.05 至 1.12 cycle/uop。

主要特征差异如下：

| 特征 | Redis base | Redis heldout | 相对训练分布差异 |
|---|---:|---:|---:|
| memory density, 65536 | 0.0525 | 0.0444 | -0.24 sigma |
| line unique fraction, 65536 | 0.2329 | 0.2635 | +0.12 sigma |
| page unique fraction, 65536 | 0.1202 | 0.1511 | +0.21 sigma |
| rare-line reference fraction, 65536 | 0.2348 | 0.2739 | +0.14 sigma |
| DTLB pressure, 4096 | 0.6015 | 0.6109 | +0.04 sigma |
| DTLB pressure, 65536 | 0.8667 | 0.8937 | +0.07 sigma |

这些特征能看到 heldout 的访问更稀疏、工作集略大、复用更弱，但变化幅度不足以唯一
对应约 1.1 cycle/uop 的额外可见 penalty。

换句话说，当前特征主要描述访问形态，没有直接描述：

- 实际 memory service latency；
- miss 是否落在 commit critical path；
- stall 是否被其他并行请求隐藏；
- 队列、bank、channel 或 coherence backpressure 是否真正暴露。

因此“特征处于训练范围内”不等于“特征足以预测服务代价”。

## 7. 无需重跑模型的残差可分性检验

使用现有原始 v29 报告构造 trace 级目标：

```text
target residual = true ROI-CPI - original v29 predicted ROI-CPI
```

输入为 40 维长期特征均值和 core count。训练只使用 seed0 train/base，并通过
leave-one-workload-out 选择 ridge 强度；business heldout 完全不参与拟合。

结果：

```text
heldout residual actual mean:     0.1926 cycle/uop
heldout residual predicted mean:  0.0221 cycle/uop
heldout residual MAE:             0.2645 cycle/uop
prediction/target correlation:   -0.178

Redis heldout actual residual:    1.1169 cycle/uop
Redis heldout predicted residual: 0.0190 cycle/uop
```

最近邻 Redis base 的残差只有约 0.02 至 0.06 cycle/uop。由于 Redis heldout 在长期
特征空间最接近 Redis base，一个只看这些特征且没有 heldout 标签的模型自然会给出
接近 Redis base 的修正。

该检验是 trace 级线性诊断，不是神经网络能力上界；但它明确表明当前特征与目标之间
不存在简单、稳定且可泛化的残差映射。

## 8. 训练覆盖与 checkpoint 选择问题

Frozen probe 的训练状态：

```text
train sequences: 359354
DDP world size: 8
steps per coverage-first epoch: 44920
trained steps: 10000
approximately consumed unique sequences: 80000
coverage of first epoch: 22.3%
```

Coverage-first 的 epoch 0 是全数据随机排列，每个 sequence 全局只出现一次。只有完成
44,920 step 后才覆盖全部训练 sequence；epoch 1 才开始 inverse-trace-size 的
trace-balanced replacement。

因此当前 probe：

- 没有完成一次全覆盖；
- 没有进入 trace-balanced 阶段；
- 不能作为“充分训练后的最终结论”。

同时，validation 只包含 train/base，不包含 business heldout。step 0 到 best step
9500 的 validation total 从 0.48318 降到 0.47409，相对改善约 1.88%。这说明 probe
仍在学习，但 checkpoint 选择只奖励 base 分布上的改进，不会直接奖励 Redis heldout
修正。

所以增加训练步数可能继续改善训练/验证目标，但没有证据表明它会自动放大 Redis
修正。基于当前特征重叠关系，它也可能进一步拟合 Redis base。

## 9. correction head 是否真正使用了长期特征

原始 v29 的 194 个共有 state tensor 与 Frozen checkpoint 逐项完全一致，最大差值为
0。说明冻结主干没有被意外修改。

但是 correction head 的输入为：

```text
frozen token state: 960 dimensions
projected long history: 128 dimensions
```

训练后，correction 第一层的权重统计为：

| 输入部分 | weight RMS | 相对初始化的 delta RMS | weight norm |
|---|---:|---:|---:|
| token state | 0.0545 | 0.0516 | 19.12 |
| long history | 0.0420 | 0.0382 | 5.38 |

Token block 的维度更大，而且每参数变化也更大。这说明 correction 至少大量利用了原有
token state。没有 zero-history 或 shuffled-history 消融，就不能把 Redis 的小幅改善
归因于新增长期特征。

## 10. 最可能的根因排序

1. 特征可辨识性不足：Redis base 与 heldout 的长期特征过近，但真实服务代价相差很大。
2. 输出门控过稀疏：只有约 4.44% 的 memory UOP 能承载全部额外 CPI。
3. 训练覆盖不足：10,000 step 只完成 coverage-first 第一轮的 22.3%。
4. 验证目标不观察 heldout：best checkpoint 只根据 train/base validation 选择。
5. correction 可绕过长期特征：960 维 token state 可能承担了主要修正。

这不是单一问题。当前实验同时存在“信息不足、输出表达受限、训练不足和归因不纯”。

## 11. 下一步最小实验

不建议立即重跑 120 条原始 v29；已有结果足够。

优先做一个小型消融，只评估 Redis heldout 8 条和少量控制 workload：

```text
A: Base-only, correction disabled
B: normal long history
C: zero long history
D: shuffled long history across traces
```

控制 workload 至少包含：

```text
Redis base
memory_seq_moderate
memory_random_mlp
MySQL heldout
marine heldout
```

判断标准：

- B 明显优于 C：长期特征确有增量价值；
- B 与 C 接近：主要是 token-only correction；
- D 与 B 接近：模型没有使用长期特征语义；
- B 优于 A 但 Redis 修正仍小：需要改变输出分解或增加 service/exposure 信息；
- C 也明显优于 A：当前 probe 本质上是冻结主干上的 token residual head。

完成消融后再决定是否值得把 Frozen probe 训练到 60,000 step。60,000 step 对应：

```text
1.335 coverage-first epochs
= 1 full coverage epoch
+ about 15080 trace-balanced optimizer steps
```

若长期特征消融没有显著增益，则不应把算力投入到同结构的 60,000-step 训练。
