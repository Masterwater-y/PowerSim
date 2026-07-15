# raw-v27 负载集审查、Loss 校准与完整训练集方案

**日期**：2026-07-10  
**审查对象**：`/data00/yinhaolang/TSim/data/raw_v27_ffatomic_seed0_c{01,04,08,16,32}`  
**结论**：新集合比旧集合更适合作为“机制基座”，但当前采集版本不应直接作为最终训练集，也不应完全替代旧的现实/proxy
负载。严格构建器当前正确返回 `blocked`。

## 1. 审查口径

`scripts/audit_v27_raw_dataset.py` 对每条 Parquet 做 metadata 完整性检查，并在 trace 的 9 个等距 row group 中、每核每区
抽 8192 UOP。label 只用于 CPI/spread 诊断；functional signature 不含 tick/PMU/oracle。旧集合使用完全相同口径审查：

```text
/data00/yinhaolang/TSim/data/raw_trace_pool/activecore_train/c{01,04,08,16,32}_seedA
```

因此以下新旧比例可直接比较；它们是分层样本统计，不等同于最终自然 ROI 权重。

## 2. 完整性与规模

| 项 | 当前 raw-v27 |
|---|---:|
| root | 5（c01/c04/c08/c16/c32） |
| workload | 18 = 16 train + 2 held-out |
| workload×core slice | 90，Parquet 核文件全部完整 |
| 全部 UOP | 737,922,530 |
| train UOP | 663,360,874 |
| held-out UOP | 74,561,656 |
| `K=256` train chunk（约） | 2,591,285 |
| 磁盘 | 约 977 GiB（14/59/121/257/526 GiB） |

设计稿要求 21 个 train workload。当前缺少 5 个 workload，并且是每个 core count 都缺，因此共缺 25 个 cube cell：

```text
W_int_mul_dense
W_fp_alu_dense
W_random_LLC
W_coh_read_share
W_skew_producer_amp
```

两个 held-out `W_phase_ws_shrink/W_phase_coh_decay` 均已采集，不能放入训练。

## 3. 与旧集合的定量比较

9 区域抽样分别覆盖新集合 288,479 个 chunk、旧集合 284,570 个 chunk。下表按抽样 chunk 加权：

| 指标 | 旧集合 | raw-v27 | 判断 |
|---|---:|---:|---|
| CPI `[3,10)` | 1.51% | 12.11% | 新集合显著补齐中段 |
| CPI `>=20` | 7.76% | 15.35% | 新集合慢路径更丰富 |
| CPI `>=40` | 3.86% | 6.30% | 极端不再只靠一个旧 false-sharing |
| cross-core log-std `>=0.10` | 43.34% | 49.55% | 略好 |
| cross-core log-std `>=0.25` | 19.20% | 35.62% | hot/cold、phase、coherence 明显更强 |

新集合的真实改进是：机制命名更清晰、onset/decay 方向成对、hot/cold 非均匀、多核强度曲线完整。`W_skew_hot_cold`
的 full-trace core-CPI CV 从 c04 到 c32 为 0.97/1.08/1.27/1.38，确实提供了旧集合缺少的逐核可辨识信号。

但旧集合仍有新集合没有的 branch/indirect/control-heavy 和业务 proxy 分布。纯 microbenchmark cube 不能证明对真实 functional
trace 泛化。因此最终方案应是“修复后的机制集 + 精选现实/proxy raw trace”，而不是二选一。

## 4. 当前 raw-v27 的阻断问题

### 4.1 `stream_seq_L2/DRAM` 实际是同一个实验

在 c01/c04/c08，两个 workload 的归一化地址 functional chunk digest 完全一致；其 full CPI 分别为：

| core | L2 | DRAM |
|---:|---:|---:|
| 1 | 1.52920 | 1.52923 |
| 4 | 3.03586 | 3.03625 |
| 8 | 4.04179 | 4.04182 |
| 16 | 5.17079 | 5.17146 |
| 32 | 7.677 | 7.088 |

c32 甚至出现 L2 比 DRAM 更慢。根因在 `v27_microbench.c::kernel_stream_seq`：final scale=12 时每核只有
`12*2048*8B = 192 KiB` 的顺序地址跨度，64 MiB 分配从未被走完。必须按 cache line 遍历完整 per-core working set，并让
L2 版本重复 256 KiB、DRAM 版本覆盖大于 LLC 的集合；修复前这两个 workload 只能视为重复样本。

### 4.2 atomic 语义在 functional trace 中丢失

所有 90 个 slice 的 `is_atomic` 抽样比例都是 0；`W_coh_atomic_cas` 也只表现为 opclass 56/57 的 load/store micro-op。
这使模型无法区分 locked CAS 与普通 RMW。不能用 workload ID、PC 或 timing oracle 补洞；应修 trace exporter/static decoder，
把 lock/atomic 语义写入 functional record，并加采集验收 `W_coh_atomic_cas.atomic_frac > 0`。

### 4.3 `W_phase_ws_grow` 没产生设计中的 working-set 相变

设计目标是约 `0.5 -> 2 -> 8`。实际 c32 九区中位 CPI 为：

```text
0.449, 0.441, 0.461, 0.496, 0.480, 0.535, 0.531, 0.568, 0.439
```

没有单调增长，末端还回到起点；c16 也只有局部尖峰。当前每 phase 的有效访存次数不足以建立/越过 working set。需要按
“每 phase 至少覆盖 active set 一遍并留稳态区”的规则重写，而不是用固定 8192 次循环在 11 个 size 上摊薄。

相对地，coherence phase 是有效的：c32 onset 从约 0.254 跳到 45--66，decay 从 53--59 回落到约 0.25。

### 4.4 其余机制偏差

- `W_int_div_dense` 稳态 CPI 约 0.77--0.80，不是设计的 3--5；四条独立 divide chain 被 O3 并行。应改为串行依赖链。
- `W_chase_LLC/DRAM@c01` 为 26.85/27.06，说明 LLC 版本在有限 trace 长度内也主要是 cold miss；需增加复用轮次或缩小到
  “大于 L2、明显小于 LLC”的可遍历 working set。
- `W_coh_migratory` 没有显式 turn/token，当前更像并发 read/write share，不是严格 migratory ownership。
- 当前只有 A0；uarch 数值输入除了 core count 外没有可学习变化，不能据此声称 uarch 泛化。
- 只有 seed0；不存在合格的 seed-held-out validation。

## 5. 特征是否足够

当前代码已经从最初的 opclass+flags 扩展为 17 per-UOP + 27 chunk summary + 14 functional relation + 28 uarch 数值特征，并加入
4K/64K recent working-set、局部 first-touch PC/line ID、访问宽度/offset、xcore reader/writer/fanout。它比旧 TSim clean14 的
覆盖不少，并删除了绝对地址 hash 和 true-time shared-state 泄漏。

对当前目标，特征侧剩余 P0 不是“再加几个统计量”，而是 raw 本身缺字段：atomic、branch outcome/target、明确 sync/lifecycle；
这些必须在采集端修。后续 P1 才是 topology/NUMA、可部署的 barrier/spin detector、multi-uarch latency/bandwidth 参数。

重要边界：如果两个核的这些可见特征相同，loss 不允许使用裸 core ID 猜谁慢。当前 dataset 会生成
`functional_group_id`，绝对 loss 学等价组均值，center/listwise 只比较不同组。

## 6. Loss 与权重结论

当前实现为：

```text
L = 1.00 L_abs(log-CPI Huber, beta=.3)
  + 0.50 L_center(可辨识组、spread>=.10)
  + 0.15 L_slow(listwise KL, tau=.30)
```

resident context 使用 inverse-exposure 权重；absolute supervision 只在 first exposure；prefix/endpoint 在连续 sequence batcher
完成前为 0。

分层样本的 collapsed baseline 标度为 `L_abs=0.430, L_center=0.038, L_slow=0.248`，乘权重后为
`0.430/0.019/0.037`。辅助项合计约主项的 13%，作为首轮合理，不需要再次用 raw-cycle loss 放大远尾。约 30.0% 的抽样
context 可辨识且 spread>=.10。

一个关键反例是 `W_coh_ws@c32`：原始 cross-core spread 几乎处处大于 0.10，但 primitive functional signature 对称，
可辨识 context 比例为 0。新 loss 会让它学共同 scale，不强迫模型按 core ID 复现随机仲裁结果；`W_skew_hot_cold@c32`
则为 100% 可辨识，center/rank 会完整启用。这正是避免“每个核分不开”时乱加 loss 的正确门控。

权重仍需在独立 seed validation 上报告每项裁剪前 gradient norm。若 slow top-1 不升且 gradient < 主项 5%，再提高；若辅助
gradient 超过主项或 absolute MAPE 恶化，再降低。仅凭同 seed teacher-context val 不能调最终权重。

## 7. 完整训练数据集的正确构造

### 7.1 先修采集，再构建

严格入口：

```bash
python scripts/audit_v27_raw_dataset.py \
  --sample-regions 9 --sample-uops-per-core 8192 \
  --out data/v27_raw_audit.json

python scripts/build_v27_dataset.py \
  --out data/v27_2_dataset \
  --audit-report data/v27_raw_audit.json
```

当前输出为：80 train traces、0 validation traces、10 mechanism-test traces，并因 25 个缺失 cell、3 个归一化 functional pair
collision、atomic flag 全零、ws-grow 无效、缺少独立 validation seed 而标记 `blocked`。只有显式 `--allow-provisional` 才能越过，用于 pipeline smoke，
不能用于最终结论。

### 7.2 推荐数据组成

1. **Mechanism train**：修复后的 21 workload × 5 core × A0 × seed0；保留完整自然 chunk trajectory。
2. **Realism train**：从旧 raw 中保留 branch/indirect、search/graph/ranking/MLP 等现实/proxy family，全部用新 fixed-K pipeline
   重建；不能复用 true-time 变长 window 或按 label 定义的 variant。
3. **Validation**：同 train workload 的独立 seed1，整条 trace holdout；至少覆盖 c04/c16/c32。
4. **Mechanism test**：`phase_ws_shrink/phase_coh_decay`，任何训练与调参都不可见。
5. **Uarch test**：A1 到位后按完整 uarch config holdout；同 functional trace 做 paired delta 只是辅助，不替代绝对 loss。

不要把全部旧 7.15 亿 UOP 无差别拼入。训练 sampler 按 trace 等概率，mechanism/realism 的 epoch 配比从 60/40 起做消融；
否则长 atomic/skew 或大量 proxy trace 会按 chunk 数支配梯度。

### 7.3 Artifact 与 split 合同

- fixed `K=256`，boundary label 严格 telescope；3GHz corpus 使用 333 tick/cycle，不用 333.333。
- oracle timing 只选择 context，模型 batch 不含 `T/E/delta_hat`。
- 全量默认 mmap packed cache；当前 train 部分约 259 万 chunk，不能初始化时展开为 Python dict。
- manifest 以整条 `(seed, workload, core, uarch)` trace 分 train/validation/test；禁止 sample-level random 95/5。
- resident exposure 归一；训练 trace-balanced；held-out direction 不进入 checkpoint selection。

## 8. 放行门槛

最终 build 前必须全部通过：

- [ ] 21 train + 2 held-out workload 在 c01/c04/c08/c16/c32 全 cell 完整；
- [ ] `stream_L2/DRAM` 不再 normalized-functional identical，且 c01 稳态 CPI 有明确物理分离；
- [ ] atomic CAS 的 functional `is_atomic` 非零并经反汇编/trace 对齐检查；
- [ ] ws-grow trajectory 至少在 c16/c32 呈方向正确且末/初 >=2（目标应更高）；
- [ ] int-div、chase-LLC 机制与设计区间一致；
- [ ] seed1 trace-level validation 非空；
- [ ] 每个 train trace endpoint 守恒，invalid label 比例可解释；
- [ ] single-context overfit：可辨识 sample 能拟合，完全对称 sample 的 centered/listwise 为零；
- [ ] 权重 gradient norm、centered MAE、spread ratio、slow top-1 和 aggregate/endpoint 指标同时报告。

因此答案不是“新集合一定比旧集合好”或“继续用旧集合”。准确结论是：新设计方向更好、当前实现尚未达标；修复后用它
做机制基座，再加入精选旧现实分布，才是完整训练集。
