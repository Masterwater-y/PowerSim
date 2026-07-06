# 思路 2：Coding LLM 语义激活 + Macro 汇编输入设计稿

状态：设计稿。目标是把当前 LLMSim 从"用 LLM 但完全不用它的语义先验"改成
"真正利用 coding LLM 对汇编代码的语义理解"。前提是同步完成 loss/head/切窗
反塌缩改造，否则语义先验单独上不解决 per-core CPI 塌缩。

## 1. 问题背景

### 1.1 当前方案的语义激活率

v22 及之前所有版本使用完全自定义的 special token：

```text
<UOP> + composite embedding(op_class, reg_bucket, mem_kind, rd_bucket, stride_bucket, br_type)
```

Qwen backbone 拿到这些 token 时：

- 新加 special token 数 ≈ 1470（`model/tokenizer.py`）
- Qwen 预训练的 15 万词表**几乎全部不参与**
- LLM 只提供"通用 causal attention 结构"，没有提供任何**代码语义先验**

实际验证过：Qwen3-0.6B-Base 对当前 tokenizer 输出的语义激活率 ≈ 0%。这是当前
方案里最大的浪费——付出了 0.6B backbone 的所有成本（训练 3-4 小时、推理 76ms/窗、
显存开销），但没拿到 LLM 的核心收益。

### 1.2 Macro 层 vs uop 层的语义地位

真实汇编（Qwen 见过的代码语料）由 macro 指令构成：

```asm
mov rax, [rbx+8]
imul rax, rcx
add rax, rdx
mov [rdi], rax
jne .loop
```

uop 是微架构内部分解，**不在任何公开代码语料里出现**。同一条 `mov rax, [rbx+8]`
在 x86 gem5 里展开成 2 个 uop，这个分解形式 Qwen 从没见过。

结论：**要激活 Qwen 的代码语义，必须以 macro 为输入单位**。uop 层再怎么转
汇编样式，Qwen 也认不出。

### 1.3 实测数据支撑

`scripts/tmp_macro_tokenize_probe.py` 对 c04 raw parquet 抽 macro 序列，编码
为汇编样式后用 Qwen tokenizer 实测：

| Workload | 平均 tok/macro | UNK 率 | 单 token 词覆盖率 |
|---|---:|---:|---:|
| W_phased_mix | 3.14 | 0% | 95.3% |
| W_indirect | 3.33 | 0% | 94.8% |
| W_ads_ranking_proxy | 4.59 | 0% | 86.1% |
| W_false_sharing | 5.81 | 0% | 85.0% |
| W_chase_dram | 6.39 | 0% | 81.6% |

关键：**UNK 率 0%**，所有 token 落在 Qwen 原生词表；**85-95% 单 token 词覆盖**，
说明主要语义词 (`mov`, `add`, `ld`, `seq`, `hot`, `shared`) 是 Qwen 直接
认识的原生 token，预训练 embedding 语义可用。

## 2. 输入编码设计

### 2.1 Macro 抽取

从 `aligned.parquet` 出发：

```text
macro 边界 = is_last_microop==1 或 macro_pc 变化
```

`is_macro_head` 在 `data/build_windows.py:830` 已实现。macro 抽取不需要重采
raw trace，只需在 build_windows 阶段增加一条输出路径。

实测 macro 展开系数（每 macro 平均包含的 uops）：

| Workload | mean | p90 | max |
|---|---:|---:|---:|
| W_chase_dram | 2.00 | 3 | 40 |
| W_ads_ranking_proxy | 2.09 | 3 | 71 |
| W_stream | 1.85 | 3 | 15 |
| W_false_sharing | 1.30 | 3 | 29 |
| W_phased_mix | 1.64 | 3 | 3 |
| W_indirect | 1.67 | 3 | 5 |

平均 1.3-2.1 uop/macro。**W=1024 macros 覆盖 1300-2100 uops**，信息密度比 uop
层高。少数极端展开的 macro（REP、复杂 SIMD）通过显式 tag `x{n}` 恢复
展开数。

### 2.2 单条 macro 的编码格式

```text
<mnemonic> <dst> <src> [<addr_tags>] [<macro_tags>]
```

**mnemonic**（Qwen 原生 token）：

| 场景 | Token |
|---|---|
| ALU (op_class=1) | `add` / `sub` |
| Multiply (op_class=2) | `imul` |
| Divide (op_class=3) | `idiv` |
| FP (op_class 4-11) | `addsd` / `divsd` |
| SIMD int (op_class 12-24) | `padd` |
| SIMD FP (op_class 25-34) | `addps` |
| Load (is_load 或 op_class 56/58) | `ld` |
| Store (is_store 或 op_class 57/59) | `st` |
| Atomic (is_atomic) | `lock` |
| Branch cond | `jne` |
| Branch indirect | `jmp ind` |
| Call / Return | `call` / `ret` |

**寄存器**（Qwen 原生 x86 缩写）：

```text
ax bx cx dx si di bp sp r8 r9 r10 r11 r12 r13 r14 r15
```

从 `n_src`/`n_dst` 桶映射，最多 16 个桶。

**访存 pattern tag**（纯 functional 派生，部署可算）：

| 类别 | Token 选项 | 来源 |
|---|---|---|
| Stride | `same` `seq` `str` `far` `rnd` | `annotate_rd_stride` |
| Reuse | `hot` `warm` `mid` `cool` `cold` | `annotate_rd_stride` |
| Sharing | `shared` | `coh_oracle ∈ {2,3}`（多核 access 交集）|

**关键**：`L1` / `L2` / `L3` / `dram` / `tlbm` / `mispred` **不进输入**。这些
来自 gem5 微架构模拟，部署侧不可算，是训练监督目标。上一轮 probe 脚本
把它们混进 input tag 是错的。

**Macro 展开数 tag**：只在 `n_uops >= 4` 时加 `x{n}`，避免每 macro 都注解。
覆盖 REP / 复杂 SIMD 等异常展开。

### 2.3 示例

一段 chase_dram 型 macro 流的编码（不含具体地址值）：

```asm
add bx ax
ld bx bx seq hot
ld bx bx seq hot
add cx bx
ld ax bx str warm
st bx ax same hot
ld cx bx rnd cold
ld dx bx rnd cold shared
imul cx bx
jne L
call
ret
```

每条 3-6 token，全部落在 Qwen 原生词表。语义词（`seq`/`hot`/`shared`/`cold`）
在 Qwen embedding 空间里位置合理，模型看到时能激活对应先验。

### 2.4 地址数值编码问题

**不使用具体地址值**（vaddr / cacheline_addr / paddr）。理由：

- Qwen 对大数值几乎没有内在语义。`0x7fffff...` 和 `0x7fffff00` 差 256 字节，
  Qwen 看到 10 位数字序列不会自动联想到"这两个地址在同一 page"。
- 具体地址跨 workload 不迁移（每次运行地址不同），进入模型只会当噪声或
  过拟合具体值。
- Qwen 数字用 per-digit BPE，1024 需要 4 token，1000000 需要 7 token，成本高。
- 需要保留的是 **pattern**，pattern 已经通过 stride/RD/sharing tag 编码。

**可选补充**：如果需要显式 cacheline identity（观察 reuse/sharing 的精确
发生位置），使用**窗口内相对 cacheline id**（每窗重编号 0-100），
而不是绝对地址。这个是选做，不作为主路径。

### 2.5 每条序列的完整结构

Local-core 模式下，每核一条独立序列：

```text
[SYS] 系统提示 (~20 tok)

<CFG_BEGIN>
uarch=A cores=4 clk=3G
L1=32K 8way lat=4
L2=512K 8way lat=12
L3=16M 16way lat=40
DRAM lat=200
ROB=192 IQ=64 MSHR=16
<CFG_END>                          (~40 tok)

<GLOBAL_BEGIN>
uops_total=8192 macros_total=4096
shared_lines=12 hot_shared=5
<C0_STAT> mem=hi br=lo hot=[bkt3 bkt7]
<C1_STAT> mem=hi br=lo hot=[bkt3 bkt7 bkt11]
<C2_STAT> mem=md br=md hot=[bkt5]
<C3_STAT> mem=md br=hi hot=[bkt5]
<GLOBAL_END>                       (~100 tok)

<C_self_BEGIN>
uops=2048 macros=1024
mem=680 br=110 atom=0
ld_frac=0.24 st_frac=0.09          (~30 tok)

# 本核 macro 序列，W=1024 macros，每 macro 3-6 tok
add bx ax
ld bx bx seq hot
...                                (~3500-5500 tok)

<LOCAL_C_self>
<C_self_END>                       (~5 tok)
```

**单核序列总长约 4-6k token**。

配置量纲编码用 Qwen 见过的缩写（`32K`、`512K`、`16M`），保留一点数值比例
先验；不用完整数值（32768）避免 token 膨胀。

### 2.6 全 workload cacheline bucket

跨核 sharing 信号需要**统一的 cacheline id 空间**。做法：

```text
对整个 workload 出现过的 cacheline_addr，hash 到 32 或 64 个 bucket
每个窗口的每核热点 line 用 bucket id 标注（bkt0..bkt63）
```

`<C0_STAT> hot=[bkt3 bkt7]` 和 `<C1_STAT> hot=[bkt3 bkt7 bkt11]` 相同 bucket
出现在多核热点里，等价于"这两核共享 bkt3/bkt7"。Qwen 看到相同 token
出现在两个 core 的 stat 里，attention 能捕捉这个信号。

## 3. Coding LLM 选型

### 3.1 首选：Qwen2.5-Coder-0.5B

- 参数量 0.5B，接近当前 Qwen3-0.6B-Base
- Qwen 家族，架构和 tokenizer 兼容
- 在 5.5T 代码 token 上专门训练，汇编覆盖度高
- 切换成本几乎为零：改 `WrapperConfig.base_model` 一行 + 检查 hidden size

### 3.2 备选：DeepSeek-Coder-1.3B

- 参数量 1.3B，略大
- 代码语义能力在同规模里公认较强
- 跨家族切换，`model/tokenizer.py` 和 embedding hook 需要重写
- 只在 Qwen-Coder-0.5B 效果不足时考虑

### 3.3 Base vs Instruct

- **训练用 Base**：Instruct tuning 会把 hidden state 拉向"对话式回复"方向，
  可能扭曲底层代码语义
- **手动 zero-shot 验证用 Instruct**：给一段 macro 序列问 Qwen 分析代码，
  验证语义激活是否成立

### 3.4 Zero-shot 验证（前置步骤）

在投入完整训练前，先用 Qwen2.5-Coder-0.5B-Instruct 跑一次手动实验：

- 手写一段代表性 macro 序列（memory-bound pointer chase）+ pattern tag
- 让 Qwen 输出 "what is the dominant bottleneck of this code?"
- 看响应是否能识别出 "memory bound"、"pointer chasing"、"cache miss heavy"

**通过条件**：Qwen 至少能说出 memory bound 且提到 cache 相关概念。
**不通过**：切换到 DeepSeek-Coder-1.3B 重试；仍不通过则思路 2 语义激活假设
不成立，回退到 non-LLM 路径。

## 4. 训练标签与目标

### 4.1 主标签

```text
label:          [N_core, K_pmu=7] float32
uops:           [N_core] float32
instr_retired:  [N_core] float32
core_mask:      [N_core] bool
denoms:         [N_core, 6] float32
```

`K_pmu=7` 沿用 v22：`cpi_uop`, `branch_miss`, `l1d_ld_miss`, `l1d_st_miss`,
`l2_ld_miss`, `l2_st_miss`, `llc_miss`。

`cpi_uop` 在 log 空间回归，miss counts 在 log1p 空间回归。

### 4.2 辅助监督（新增）

从 gem5 stats 派生的**语义类别标签**，让 hidden state 对齐到 Qwen 语义空间：

```text
bottleneck: memory_bound / compute_bound / branch_bound / mixed
memory_intensity: low / medium / high
branch_predictability: high / medium / low
```

派生规则（简单阈值）：

```python
if l1d_miss_rate > 0.1 and llc_miss_rate > 0.01:
    bottleneck = "memory_bound"
elif branch_miss_rate > 0.05:
    bottleneck = "branch_bound"
elif ipc > 2:
    bottleneck = "compute_bound"
else:
    bottleneck = "mixed"
```

走一个额外的分类 head，交叉熵 loss，权重 0.3。目的**不是学准分类**，是让
backbone 的 hidden state 靠近 Qwen 预训练里 "memory bound" / "branch"
这些概念的语义位置，间接改善主 PMU 回归的样本效率。

### 4.3 反塌缩 loss（必须）

**这一部分和 v23 的 loss 改造完全一致**，见 `docs/pre_v23_anti_collapse.md`。
思路 2 单独换 tokenizer 不解决塌缩；必须打包 loss + head 改造。

## 5. Local-core 结构（必须）

W=1024 macros × 4-6 tok/macro ≈ 5k token/core。全局模式拼接：

| 核数 | 全局 token 数 |
|---|---:|
| c04 | ~20k（可）|
| c08 | ~40k（爆 32k）|
| c16 | ~80k（爆）|
| c32 | ~160k（爆）|

**c08 及以上必须走 local-core**。每核独立 backbone forward，序列长度不随
核数增长，稳定在 5-6k token。

Local-core 的信息流失（backbone 看不到别核 uop 层）通过三层补偿：

1. 共享 prefix 里的 `<Ci_STAT>` per-core aggregate 和 hot bucket
2. `<GLOBAL_...>` 跨核 side features
3. Backbone 之后的 cross-core adapter（mask-aware self-attention over cores）

代码基础：`train/dataset.py:_build_local_core_sequences` (v19) 已经实现
数据侧切分；`model/llm_wrapper.py` 的 forward 需要加 local-core 分支。

## 6. 微调策略

### 6.1 分层解冻

不是纯 LoRA，也不是全量微调。分组指定学习率：

| 参数组 | 策略 | 学习率 |
|---|---|---:|
| 新加 special token embedding (`<CFG_...>`, `<Ci_STAT>` 等) | 全量训 | 1e-3 |
| Qwen 原生 token embedding (`mov`, `add`, `ld`, `seq` 等) | **冻结** | - |
| Backbone 底层 (layer 0 to N/2) attention LoRA | LoRA rank=16 | 5e-5 |
| Backbone 高层 (layer N/2 to N) attention LoRA | LoRA rank=64 | 1e-4 |
| Backbone MLP LoRA（可选，扩容量） | LoRA rank=32 | 5e-5 |
| PMU regression head | 全量训 | 1e-3 |
| 语义分类 head | 全量训 | 1e-3 |
| Cross-core adapter | 全量训 | 1e-4 |

**关键：原生 token embedding 必须冻结**。这是思路 2 的核心资产——
`mov` 这个 token 在 Qwen 里已经有正确的语义 embedding，动了就废了。

`model/llm_wrapper.py:_unfreeze_new_embeddings` 现有的 backward hook 机制
（把旧 token 行的梯度置零）原样保留。

### 6.2 为什么不全量微调

- 全量微调 Qwen-0.6B 的 600M 参数，40k 样本量不够，会过拟合和灾难遗忘
- 全量微调会破坏我们想利用的 code embedding 语义
- 显存需要 3-4x
- LoRA 在这个任务上的容量足够——任务本质是把已有的代码理解能力路由到
  PMU 数值，是典型的 LoRA 适用场景

### 6.3 为什么不纯 LoRA

- 新 special token 必须可训练，纯 LoRA 冻结 embedding 就废了这些新 token
- Head 是新任务，必须全量训

## 7. 训练目标组合

```text
L_total = L_pmu (主任务，反塌缩改造后)
        + λ_sem · L_semantic (辅助分类，激活 Qwen 语义)
```

- `L_pmu` 权重 1.0，是主任务
- `L_semantic` 权重 0.3-0.5，不能主导
- 具体 loss 形式见 `docs/pre_v23_anti_collapse.md`

## 8. 序列长度和 token 预算

单个训练样本，4 核：

- 每核序列 4-6k token
- Backbone forward 4 次（batch dim 并行）
- 单样本总 backbone token ≈ 20k

Batch=8 一步 backbone token ≈ 160k。8×A100 下 5k 短序列 batch=32 是常规规模。

40k 训练样本 × 6 epoch ≈ 5B backbone token。训练时间估计和 v22 相当或略快
（每次 forward 短 attention 更快，但 forward 次数多）。

## 9. 部署侧影响

推理时每 window 一次 predict，产生一次时序 PMU 输出。Local-core 下：

- c04 单窗 ~4 次 backbone forward，每次 5k token
- c08 单窗 ~8 次
- c16 单窗 ~16 次
- c32 单窗 ~32 次

单次 forward 约 15-25ms（Qwen-0.6B, bf16, 5k 序列, A100），c08 单窗 ~120ms。

**如果部署要输出时序 PMU（每 workload 几万到几十万窗）**，当前 LLM
backbone 的 latency 是瓶颈。这不是思路 2 引入的问题，是当前架构就有的，
但时序输出需求让它变得关键。可能的方向：

- KV cache streaming：滑动窗口增量 forward
- 换更小模型：从零训 6-12 层 transformer，用同样的 tokenizer 和输入
- 非自回归 encoder-decoder：整 workload 一次编码，逐 Δt decode

这三个方向都不在本设计稿范围内，作为 P2 长期方向记录。

## 10. 实施顺序

**阶段 0（前置验证，1 天）**：

- 用 Qwen2.5-Coder-0.5B-Instruct 跑 zero-shot 手动验证语义激活
- **通过才进入下一阶段**；不通过评估 DeepSeek 或放弃思路 2

**阶段 1（数据管道，3-5 天）**：

- `data/build_windows.py` 加 `emit_macro_assembly` 输出路径
- 复用现有 `annotate_rd_stride` / `annotate_functional_proxies`
- 生成一个 c04 小 workload set 的样本，人工检查渲染质量
- 数据集重建：所有 workload × 所有 core count 的 windows.jsonl 重建
- Tensor cache 重建

**阶段 2（模型改造，3-5 天）**：

- `model/llm_wrapper.py` 换 base_model 到 Qwen2.5-Coder-0.5B
- 检查 hidden size 变化，调整 `local_proj` / `side_proj` / `head` 输入维度
- 加语义分类 head
- 分层解冻策略实现
- 反塌缩 loss/head 改造（依赖 v23 分析）

**阶段 3（训练验证，1 周）**：

- 小规模 smoke train（1000 step）验证 loss 下降合理
- 完整 8000 step 训练
- c04/c08/c16/c32 全 eval

**阶段 4（对照实验，1 周）**：

- 训练同规模的 non-LLM baseline（从零 6-12 层 transformer，同 tokenizer）
- 精度和 latency 对比
- 决策：LLM 值不值得留

## 11. 主要风险

1. **Zero-shot 验证失败**：Qwen-Coder 对我们的 macro + tag 编码语义激活弱。
   概率中等。缓解：DeepSeek 备选、降级到 non-LLM。

2. **语义激活但精度不改善**：Qwen 识别出 memory bound 但预测数值仍不准，
   说明瓶颈不在语义先验缺失，在 loss/head/数据覆盖。这不否定思路 2，
   但也不证明它的价值。缓解：先做 v23 反塌缩改造，再叠加思路 2。

3. **训练 latency 过高**：Qwen-Coder-0.5B forward 单核 5k token，local-core
   下 c32 需要 32 次 forward，可能 300ms+/窗。缓解：换非自回归结构或
   小模型。

4. **数据重建成本大**：改 tokenizer 后所有 windows.jsonl 和 tensor cache
   要重建。缓解：先用 c04 小 workload 验证方案再全量重建。

5. **new special token 训练不稳定**：新加的 `<Ci_STAT>`、`bkt0..bkt63` 等
   token 从零学 embedding，40k 样本可能不够。缓解：初始化用附近 Qwen
   token 的 embedding 平均值而不是随机。

## 12. 不做的事

- **不用具体地址数值**（vaddr / cacheline_addr）作为 token
- **不把 gem5 label**（path_class / mispredicted / dtlb_hit）**混进输入**
- **不做全量微调 backbone**
- **不放弃 v22 baseline**：新方案是 v23（反塌缩）+ v24（思路 2），旧 ckpt
  作为对照保留
- **不改 raw trace 采集**：所有输入信号从现有 aligned.parquet 派生

## 13. 与 v23 的关系

- **v23**：不换 base model，只做反塌缩改造（loss + head 双端）
- **v24 (本文档)**：在 v23 反塌缩基础上叠加 coding LLM + macro 汇编输入

v23 先做，因为它风险低、代价低、能独立验证塌缩问题是否可解。v24 依赖
v23 的反塌缩机制——如果 v23 显示反塌缩改造有效，v24 才有意义；如果
v23 显示塌缩根源比预期更深，v24 也治不了。

不建议跳过 v23 直接做 v24：如果 v24 训完精度改善，我们无法区分收益来自
"Qwen 语义激活"还是"反塌缩改造"。
