# v29 Memory-Gated 双 Timing Head 设计

更新时间：2026-07-24

状态：方案设计，尚未实施代码。

## 1. 结论

第一版采用一个共享 full-QKVR 主干和两个 timing 输出头：

```text
Shared full-QKVR
    |
    +-- BaseTimingHead
    |
    +-- MemoryCorrectionHead
```

两个头不分别拟合人工构造的 Base/Memory timing 标签，也不分阶段用
`total - predicted_base` 生成 Memory 标签。它们从训练开始就通过同一个真实
commit-cycle 目标端到端联合训练。

最终预测量全部使用 cycle：

```text
base_logit[i] =
    BaseTimingHead(token_state[i])

memory_correction_logit[i] =
    MemoryCorrectionHead(
        token_state[i],
        per_access_features[i],
        dependency_and_parallelism_features[i],
        core_memory_state[i]
    )

gap_cycle[i] =
    softplus(
        base_logit[i]
        + memory_mask[i] * memory_correction_logit[i]
    )

relative_commit_cycle[i] =
    FP64_cumsum(gap_cycle)[i]
```

这里的 Memory 输出是 signed correction，不解释为独立可观测的物理
memory-service latency。唯一具有严格 timing 含义的是最终的
`gap_cycle` 和 `relative_commit_cycle`。

## 2. 为什么不拆分 Base/Memory timing 标签

raw trace 只能直接观察：

```text
observed_gap =
    base_effect
    + visible_memory_effect
```

它不能从单次运行唯一恢复两个分量。如果先训练 BaseHead，再构造：

```text
memory_label =
    total_label - predicted_base
```

那么：

```text
predicted_base =
    true_base + base_error

constructed_memory_label =
    true_memory - base_error
```

BaseHead 的误差会完整进入 Memory 标签。窗口内的小幅 base bias 还会随 UOP 数
累积；对 residual 再做非负截断会产生单边偏差。因此本方案禁止：

- 为两个 timing head 人工构造两个标量真值；
- 先单独训练 BaseHead，再单独训练 MemoryHead；
- 使用 `relu(total - predicted_base)` 作为正式训练标签；
- 把两个头的输出解释为可直接验证的物理分解。

只有额外采集同一功能指令流的 ideal-memory counterfactual trace，才可能构造更
接近物理含义的 Base/Memory 成对标签；当前 raw 数据不满足这一条件。

## 3. 精确训练标签

数据预处理阶段把 gem5 tick 换算成 cycle：

```text
true_gap_cycle[i] =
    max(
        0,
        commit_tick[i] - commit_tick[i - 1]
    ) / tick_per_cycle
```

当前数据合同中：

```text
tick_per_cycle = 333
```

模型训练和推理阶段不再输出或使用 tick。每核的真实相对退休周期为：

```text
true_relative_commit_cycle[i] =
    FP64_cumsum(true_gap_cycle)[i]
```

窗口总周期和 CPI 为：

```text
window_cycles =
    sum(gap_cycle)

CPI_uop =
    window_cycles / retired_uops

CPI_macro =
    window_cycles / retired_macro_instructions
```

窗口首个 UOP 的 `true_gap_cycle` 必须读取同一核心连续 trace 中的前一个 UOP，
不能在每个 256-UOP 窗口边界重新置零。

## 4. 两个输出头的职责

### 4.1 BaseTimingHead

BaseTimingHead 对所有有效 UOP 生效。它读取现有稳定的 QKVR token state，学习
默认 timing logit，包括：

- compute、FP、SIMD 和普通指令执行；
- branch、serialize 和依赖造成的 timing 差异；
- retirement width 和同周期 commit bundle；
- load/store 的默认行为。

BaseTimingHead 可以看到 `op_class`、`mem_kind` 和已有 dependency 特征，但不读取
新增长期 memory state。这样长期工作集特征不会直接修改非访存 UOP。

### 4.2 MemoryCorrectionHead

MemoryCorrectionHead 只通过硬 mask 影响 load、store 和 atomic：

```text
memory_mask =
    valid
    AND (is_load OR is_store OR is_atomic)
```

它读取：

- 共享的 `token_state`；
- memory kind、size、line/page、reuse、stride 等 per-access 特征；
- producer distance、独立访存密度等 dependency/parallelism proxy；
- per-core long-history 和 cross-core memory summary。

第一版不增加独立的 `ServiceHead * ExposureHead`，也不增加部署期 MSHR/TLB/cache
状态机。MLP、ILP 和 criticality 对最终可见 penalty 的影响由
MemoryCorrectionHead 在联合 timing loss 下隐式学习。

### 4.3 为什么使用 signed logit correction

不采用：

```text
gap_cycle =
    positive_base_cycle
    + memory_mask * positive_memory_cycle
```

这种写法容易把默认访存代价重复计算，并且只能增加、不能修正 BaseHead 的高估。

本方案采用：

```text
gap_cycle =
    softplus(
        base_logit
        + memory_mask * signed_memory_correction_logit
    )
```

MemoryCorrectionHead 可以输出正修正或负修正，最终 `softplus` 仍保证
`gap_cycle >= 0`。

## 5. 训练目标

第一版保持现有 v29 timing/progress 训练合同，不为两个新头增加独立 timing 标签。
两个头共同接受最终预测产生的现有 commit-time、prefix、progress 和 cumulative
损失。

概念上必须满足：

```text
pred_relative_commit_cycle =
    FP64_cumsum(pred_gap_cycle)

L_timing =
    ExistingV29TimingLoss(
        pred_gap_cycle,
        pred_relative_commit_cycle,
        true_gap_cycle,
        true_relative_commit_cycle
    )
```

这样所有 timing 梯度都来自真实 commit cycle，不经过 Base 预测值相减，不存在
级联伪标签误差。

## 6. path/MSHR/TLB/coherence 的使用边界

第一版不把 `path_class`、`d_mshr_depth`、`dtlb_hit` 或 `coh_oracle`：

- 作为模型输入；
- 作为部署状态；
- 作为独立 timing 分支；
- 用来构造 Base/Memory timing 标量标签；
- 用于推理时选择或缩放 memory correction。

它们只允许作为训练期低权重辅助监督，帮助 MemoryCorrectionHead 的内部
representation 区分不同 memory mechanism：

```text
L_total =
    L_timing
    + lambda_aux * L_memory_aux
```

其中：

```text
L_memory_aux =
    L_path
    + L_mshr
    + L_tlb
    + L_coherence
```

约束：

- auxiliary label 必须和 functional input 严格隔离；
- auxiliary 输出不进入最终 timing 公式；
- 推理时不读取 raw oracle；
- `lambda_aux` 以实际 loss contribution 为准，而不是只看名义系数；
- steady-state 下辅助项对总 loss 的贡献上限建议为 2%；
- 必须保留 `lambda_aux = 0` 的消融对照。

因此这些任务只提供很弱的 representation regularization，不能压过真实
commit-cycle 目标。

## 7. 长期 memory 特征的注入位置

当前 long-history 方案把 40 维长期特征投影成 960 维 residual，并加到每个
token hidden state。这会让 memory-specific context 同时影响 compute、SIMD、
branch 和 memory。

Memory-gated 方案改为：

```text
existing functional features
    -> Shared full-QKVR
    -> token_state

long-history/core-memory features
    -> small projection
    -> MemoryCorrectionHead only
```

不再把新增长期 memory state 全局 residual 注入共享 token state。原有 QKVR
主干、层数、`K=256` 和 attention 合同不变。

## 8. 初始化与训练策略

第一版保持联合训练，不使用分头预训练。推荐初始化：

- BaseTimingHead 沿用当前 v29 timing head 的初始化方式；
- MemoryCorrectionHead 的最后一层使用零或极小初始化；
- 初始 `memory_correction_logit` 接近 0；
- 初始模型行为因此接近原 v29 base timing 路径；
- 两个头随后由同一个最终 timing loss 联合优化。

如果从旧 v29 checkpoint 做快速 probe，可以先冻结 QKVR 和 BaseTimingHead，只训练
memory correction，验证输出门控是否有价值；该 probe 不能替代最终从头训练的公平
对照。

## 9. 实验矩阵

所有主实验保持相同：

- 数据和 split；
- coverage-first sampler；
- 训练 step；
- seed；
- QKVR 主干；
- evaluator 和 free-running 参数；
- checkpoint 选择规则。

只改变 timing 输出路由：

| 实验 | 结构 | 目的 |
|---|---|---|
| E0 | 原始 v29 单 timing head | 严格基线 |
| E1 | long-history 全局 residual | 当前退化对照 |
| E2 | Base + memory-gated correction，`lambda_aux=0` | 判断输出门控本身 |
| E3 | E2 + 低权重 memory auxiliary loss | 判断弱机制监督是否有额外价值 |

主结论必须先比较 E0、E1、E2；只有 E2 有正向结果时，才使用 E3 判断辅助监督。

## 10. 验收与诊断

核心验收：

- Redis heldout C4/C8/C16/C32 的 CPI 误差显著下降；
- 全负载 macro ROI 不得相对原始 v29 系统性退化；
- compute、SIMD 和 branch 不因长期 memory 特征直接变化；
- 使用相同 evaluator 比较 best checkpoint，不能用 last checkpoint 代替；
- 报告 seed0，最佳结构再用 seed1 复验。

必须输出以下诊断量：

```text
mean(base_logit)
mean(memory_correction_logit | memory)
positive/negative correction fraction
mean(final_gap_cycle)
sum(final_gap_cycle)
auxiliary loss contribution
```

推理消融：

```text
normal:
    memory_mask as defined

memory-off:
    memory_mask = 0
```

`memory-off` 只用于诊断 Base 路径，不代表真实部署结果。

## 11. 风险边界

1. 两个头的输出不是独立物理真值。最终 commit cycle 准确不代表 Base/Memory
   数值可以单独解释。
2. hard mask 只能阻止 MemoryCorrectionHead 直接修改非访存输出；共享主干的联合
   优化仍可能产生间接影响，因此必须做 compute/SIMD 回归测试。
3. memory correction 可能学习 workload identity。需要 heldout、跨 core count 和
   mechanism trace 共同验收，不能只看 Redis 单点。
4. 低权重 auxiliary loss 也可能与 timing 冲突。若 E3 不优于 E2，应直接移除，
   不为保留物理解释而牺牲最终 CPI。
5. 如果 E2 无改善，说明问题不只是长期特征的注入位置；下一步应重新检查
   per-access 可观测性、训练机制覆盖和窗口归因，而不是继续增加输出头。

## 12. 第一版实施范围

第一版只包含：

- 保留共享 8 层 full-QKVR 和 `K=256`；
- 将原 timing 输出改为 BaseTimingHead 与 MemoryCorrectionHead；
- 用 hard memory mask 在 logit 域合并；
- 输出 per-UOP `gap_cycle`；
- FP64 前缀和得到 per-UOP `relative_commit_cycle`；
- 使用现有精确 commit-cycle 训练目标联合训练；
- path/MSHR/TLB/coherence 仅作为低权重、训练期 auxiliary supervision；
- 长期 memory 特征只进入 MemoryCorrectionHead。

第一版明确不包含：

- Base/Memory 人工拆分 timing 标签；
- 分头独立训练；
- `total - predicted_base` residual 标签；
- 独立 ServiceHead、ExposureHead 或 CriticalityHead；
- oracle 字段作为推理输入；
- 新的全局 cache/TLB/DRAM 状态机；
- 将预测 cycle 转回 tick。
