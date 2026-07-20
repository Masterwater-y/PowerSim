# v27: RAG-extracted Microarchitecture Conditioning + Cross-uarch Generalization

状态：设计稿。本文基于 Zhang/Hassan/Drechsler 的
**LLM-assisted Performance Estimation of Embedded Software on RISC-V Processors**
(DDECS 2025, IEEE document 11006767) 做 LLMSim 方案迁移与创新设计。

核心结论：

- 这篇论文最有价值的启发不是“让 LLM 直接预测 cycles”，而是**把 LLM 放在
  微架构知识抽取层**：用 RAG 从处理器文档和 RTL/source code 中抽取结构化
  performance parameters，再交给传统 ML/ANN 做数值预测。
- 对 LLMSim 来说，这比 v26 的 JSON 直接生成更稳：LLM 负责它擅长的文本/代码
  理解和 schema 填充，数值模型负责它擅长的回归。
- 论文声称覆盖不同 microarchitectures，但实验上是 SweRV/RSD 两个 PM
  分别评估，并没有充分证明 leave-one-uarch-out 的真泛化。LLMSim 后续的创新点
  应该把“微架构泛化”做成主目标，而不是只做每个配置各训一个模型。

## 1. 论文方法拆解

### 1.1 论文做了什么

论文的方法分四阶段：

1. **Text generation**：先让 LLM 根据少量示例列出可能影响 cycles 的处理器参数，
   例如 multiplier latency、divider latency、load/store latency、branch hit penalty。
2. **Data preprocessing and RAG**：把 RTL documentation 和 source code 分别切块，
   存 SQL + vector database；检索相关 chunk 后让 LLM 提取每个参数的具体值。
3. **Model training**：运行约 700 个程序，用 RTL 拿 reference cycles，用 Whisper
   functional simulator 拿 dynamic instruction counts；把
   `microarchitecture parameters + dynamic instruction counts` 作为 ANN 输入，
   输出 clock cycles。
4. **Model testing**：对 TACLeBench 的 10 个未见 benchmark，用 functional simulator
   得到 dynamic instruction counts，再用 PM 预测 cycles，与 RTL ground truth 比较。

实验设置：

| 项 | 论文设置 |
|---|---|
| LLM | GPT-3.5-turbo-0125 |
| RAG framework | LangChain |
| Embedding | all-MiniLM-L6-v2 |
| Vector DB | ChromaDB |
| Reranker/compressor | FlashrankRerank top-10 |
| Functional simulator | Whisper |
| RTL reference | SweRV, RSD |
| Predictor | TensorFlow ANN + random search |
| Train programs | about 700 |
| Test programs | 10 TACLeBench benchmarks |
| Reported MAPE | SweRV 2.50%, RSD 11.90% |

它的关键架构是：

```text
processor docs/source code --RAG+LLM--> typed performance parameters
program binary/source --functional sim--> dynamic instruction counts
parameters + instruction counts --ANN--> executed cycles
```

### 1.2 论文真正使用 LLM 的位置

论文没有让 LLM 做数值预测。LLM 的任务是：

- 从文档和源码中找到哪些参数可能影响性能。
- 对每个参数做 stepwise query，抽取数值。
- 当 documentation 和 source code 冲突时，用规则解决冲突：
  - 一个值为 0、另一个非 0，优先非 0；
  - 两个非 0 但不同，优先 source code；
  - 输出严格格式 `parameter = value`。

这点对 LLMSim 很重要：LLM 的强项是理解非结构化 specs/source，弱项是精确数值
回归。论文的设计正好绕开了 LLM 的数学弱点。

### 1.3 论文的局限

对 LLMSim 最相关的局限：

1. **动态行为过粗**：只用 dynamic instruction counts，不用长 trace、
   reuse distance、stride、dependency chain、跨核 sharing 等序列信息。
2. **微架构泛化不充分**：论文结果是 PM1(SweRV) 和 PM2(RSD)，看起来更像
   每个 microarchitecture 单独 PM；如果每个 PM 训练时 uarch 参数是常数，
   模型并没有真正学到“参数变化如何影响 cycles”。
3. **多核/一致性缺失**：目标是 embedded RISC-V cores，未覆盖 LLMSim 关心的
   c04/c08/c16/c32、多核 shared system、coherence、per-core PMU。
4. **只预测总 cycles**：没有 per-core CPI、branch miss、cache miss、TLB miss、
   coherence event 等多头输出。
5. **RAG 抽取没有形式化 verifier**：有 source-priority conflict rule，但没有
   强 schema validation、单位归一化、物理范围检查和 provenance 审计。

所以这篇文章适合作为 v27 的“结构启发”，不适合照搬成 LLMSim 最终方案。

## 2. 对 LLMSim 的直接启发

### 2.1 重新定位 LLM 的职责

v26 讨论过让 LLM 直接生成 PMU JSON，并用 RL 微调。论文给出的反向启发是：

```text
LLM should not be the numeric predictor by default.
LLM should extract and normalize microarchitectural knowledge.
```

LLMSim 中更合理的分工：

| 层 | 负责内容 | 推荐技术 |
|---|---|---|
| 微架构知识抽取 | 从 gem5 config、SimObject、RTL/doc 中抽取 cache/ROB/branch/memory 参数 | RAG + schema-constrained LLM |
| Trace 语义编码 | 把 functional trace/macro assembly 变成可泛化行为表示 | Macro Assembly / composite uop / side feats |
| 数值预测 | per-core PMU、cycles、rank/spread | MLP/GBDT/Transformer regression head |
| 可解释/诊断 | 参数缺失、source-doc 冲突、置信度 | provenance + verifier |

### 2.2 从“cfg token”升级到“typed uarch profile”

当前 LLMSim 的微架构输入非常弱：

```text
<CFG_L1D_i> <CFG_L2_i> <CFG_L3_i> <CFG_CLK_i>
```

这只表达 cache size 和 frequency 的粗 bucket。论文提示我们应该建立完整的
typed uarch profile：

```json
{
  "core": {
    "isa": "X86",
    "num_cores": 8,
    "freq_ghz": 3.0,
    "decode_width": 4,
    "issue_width": 6,
    "commit_width": 8,
    "rob_entries": 192,
    "iq_entries": 64,
    "lq_entries": 72,
    "sq_entries": 56
  },
  "branch": {
    "predictor": "tournament",
    "btb_entries": 4096,
    "ras_entries": 16,
    "mispredict_penalty_cycles": 15
  },
  "latency": {
    "int_mul": 3,
    "int_div": 20,
    "fp_add": 4,
    "fp_mul": 5,
    "load_use": 4
  },
  "cache": {
    "l1d": {"size_b": 32768, "assoc": 8, "line_b": 64, "hit_lat": 4},
    "l2":  {"size_b": 262144, "assoc": 8, "line_b": 64, "hit_lat": 12},
    "l3":  {"size_b": 8388608, "assoc": 16, "line_b": 64, "hit_lat": 40}
  },
  "mshr": {
    "l1d_entries": 16,
    "l2_entries": 32,
    "l3_entries": 64
  },
  "memory": {
    "dram_lat": 200,
    "channels": 1,
    "banks_per_channel": 16,
    "queue_window": 256
  },
  "coherence": {
    "protocol": "MESI_Three_Level",
    "directory": true,
    "remote_hit_penalty": 30
  }
}
```

v27 的第一步不是改模型，而是把 `config/uarch_profile_arch_A.json` 扩展成
可训练、可验证、可跨配置的 schema。

### 2.3 RAG 的输出应该是 JSON，不是 prompt 文本

论文让 LLM 输出 `parameter = value`。LLMSim 应进一步要求 schema JSON：

```json
{
  "name": "l1d.hit_lat",
  "value": 4,
  "unit": "cycle",
  "source": "gem5/src/mem/cache/...",
  "evidence": "latency = 4",
  "confidence": 0.91
}
```

每个字段必须带：

- `value`
- `unit`
- `source_type`: `doc`, `config`, `source`, `rtl`, `manual`
- `source_path`
- `evidence`
- `confidence`

然后由 verifier 做：

- 单位归一化：KiB/MiB/bytes、ns/cycle、GHz/tick。
- 范围检查：cache size 必须 2 的幂，latency > 0，assoc 合法。
- 冲突解决：source > config > doc；非零优先；手工 override 最高。
- 依赖检查：`line_b`、`bank_select_low_bit`、`page_size_bits` 一致。

这样 RAG 产物才能进入训练，不会把 LLM 幻觉变成模型输入。

## 3. Paper-faithful LLMSim baseline

先做一个忠实论文的 baseline，作为创新方案的下界。

### 3.1 v27a: RAG + aggregate-count PM

输入：

```text
X = [
  uarch_param_vector,
  dynamic instruction/opclass counts,
  branch/load/store counts,
  memory stride histogram,
  reuse-distance histogram,
  per-core summary features
]
```

输出：

```text
y = [
  cpi_uop,
  branch_miss,
  l1d_ld_miss,
  l1d_st_miss,
  l2_ld_miss,
  l2_st_miss,
  llc_miss
]
```

模型：

```text
MLP / XGBoost / LightGBM / FT-Transformer(tabular)
```

这一版不使用长序列 LLM backbone，只验证论文假设：

```text
structured uarch params + dynamic functional statistics
can explain a useful portion of PMU variance.
```

优点：

- 工程快。
- 可解释性强。
- 能作为 v24/v26 的强 tabular baseline。
- 能判断 LLMSim 当前误差是否主要来自“缺微架构参数”，还是来自“缺 trace 序列”。

缺点：

- 丢失指令序、依赖链、局部 burst、跨核同步等信息。
- 对 per-window/per-core high-spread CPI 可能仍然塌缩。

### 3.2 数据构建

对每个 window/core 生成一行 tabular sample：

```text
sample_id
workload
uarch_id
core_id
window_id
uarch_param_vector
side_feats
opclass_hist
rd_hist
stride_hist
branch_hist
dependency_summary
label_pmu[K]
denoms
```

其中 `uarch_param_vector` 来自 RAG/extractor 生成的 typed profile，而不是
手工 `<CFG_*>` token。

### 3.3 训练和验证

推荐三种 split：

| Split | 目的 |
|---|---|
| random window split | sanity check，不能作为泛化结论 |
| leave-one-workload-out | workload 泛化 |
| leave-one-uarch-out | 微架构泛化 |

如果 v27a 在 leave-one-uarch-out 上已经明显优于当前 cfg-token 模型，说明
RAG-derived uarch profile 是高价值输入。

## 4. LLMSim v27 主方案

### 4.1 总体结构

```text
docs/config/source/RTL
  -> RAG extractor
  -> typed uarch profile + provenance
  -> uarch encoder

functional trace / macro assembly
  -> trace encoder
  -> local-core hidden

uarch hidden + trace hidden + side feats
  -> cross-core adapter
  -> PMU regression heads
```

v27 不再把 uarch 信息压成 4 个 `<CFG_*>` token，而是并行注入：

1. **Prefix text**：给 coding LLM 的 human-readable config summary。
2. **Structured vector**：给 regression head / adapter 的 normalized numeric profile。
3. **Adapter routing**：用 uarch embedding 控制 LoRA/adapter/gating。

### 4.2 Uarch encoder

输入：typed uarch profile。

输出：

```text
uarch_hidden_global [D]
uarch_hidden_core   [N_core, D]
uarch_scalars       [F_uarch]
```

实现：

```text
numeric scalars -> log/standardize -> MLP
categorical fields -> embedding
hierarchical groups -> group encoders
concat -> UarchEncoder -> D
```

字段分组：

| Group | Examples |
|---|---|
| frontend | fetch/decode width, i-cache size/latency, branch predictor |
| backend | issue/commit width, ROB/IQ/LSQ/SQ |
| execute | ALU/mul/div/fp latency and throughput |
| memory hierarchy | L1/L2/L3 size/assoc/latency/banks/MSHR |
| TLB/page walk | DTLB/ITLB entries/assoc, walk latency |
| DRAM | channels, banks, row size, queue depth, base latency |
| coherence | protocol, directory, sharer bits, remote hit penalties |

### 4.3 Trace encoder choices

v27 should keep experiments separated:

| Variant | Trace input | Purpose |
|---|---|---|
| v27a | aggregate histograms only | paper-faithful baseline |
| v27b | current composite uop token | isolate uarch conditioning value |
| v27c | native Macro Assembly | combine v24 semantic activation + uarch conditioning |
| v27d | native Macro Assembly + JSON/RL | only after v27c is strong |

Do not mix `native macro`, `new loss`, `RAG uarch`, and `JSON RL` in one first run.
Keep attribution clean.

### 4.4 Conditioning mechanisms

Use three injection paths:

1. **Prefix conditioning**:

```text
cfg isa=x86 cores=8 clk=3G rob=192 issue=6 commit=8
l1d=32K/8/4 l2=256K/8/12 l3=8M/16/40
mshr=16/32/64 bp=tournament brpen=15 dram=200
```

2. **Vector conditioning at query/head**:

```python
query_hidden_i = query_hidden_i + uarch_proj(uarch_scalars)
pred = head(concat(query_hidden_i, uarch_hidden, side_feats_i))
```

3. **FiLM / conditional adapter**:

```python
gamma, beta = uarch_film(uarch_hidden)
h = gamma * h + beta
```

For Qwen/LoRA variants, use uarch-routed LoRA only after vector conditioning works:

```text
LoRA_delta = sum_k gate_k(uarch_hidden) * LoRA_k(h)
```

## 5. Microarchitecture generalization plan

### 5.1 Why paper is not enough

If a PM is trained and evaluated per core, uarch parameters are constants during
training. Then the model cannot learn the causal effect of:

```text
larger ROB -> more MLP hiding
higher L2 latency -> more CPI on mid-reuse loads
more MSHR -> lower stall on parallel misses
better branch predictor -> lower branch-miss penalty
```

True microarchitecture generalization requires the same workload/trace family
to be observed under multiple uarch configurations.

### 5.2 Dataset design

Create `arch_A` through `arch_H` as controlled variants.

Do not randomize everything at once. Use orthogonal groups:

| Config group | Variables | Purpose |
|---|---|---|
| cache_size | L1/L2/L3 size, assoc | footprint sensitivity |
| cache_latency | L1/L2/L3 hit latency | latency sensitivity |
| memory | DRAM latency, channels, queue depth | memory-bound behavior |
| backend_width | issue/commit width, ROB/IQ/LSQ | ILP/MLP sensitivity |
| branch | predictor quality, BTB/RAS, penalty | branch-heavy behavior |
| mshr | L1/L2/L3 MSHR entries | miss parallelism |
| coherence | protocol knobs, remote-hit penalty | shared-system behavior |

Phase order:

```text
Phase 0: arch_A only, reproduce current results.
Phase 1: arch_A/B/C/D, one variable group per config.
Phase 2: 8-12 configs, Latin-hypercube style mixed variation.
Phase 3: holdout configs never seen in training.
```

### 5.3 Pairwise counterfactual training

For microarchitecture泛化, paired data is more valuable than more random data:

```text
same workload + same input window + different uarch labels
```

Train both absolute and delta objectives:

```text
L_abs = loss(pred(uarch_a, trace), label_a)
      + loss(pred(uarch_b, trace), label_b)

L_delta = loss(
    pred(uarch_b, trace) - pred(uarch_a, trace),
    label_b - label_a
)
```

Delta loss forces the model to learn how uarch parameters change performance,
instead of relearning workload identity.

Recommended paired tasks:

| Pair | Expected signal |
|---|---|
| L2 latency 12 -> 24 | mid-reuse load CPI increases |
| DRAM latency 200 -> 300 | random/cold load CPI increases |
| MSHR 8 -> 32 | memory-level parallelism improves |
| ROB 128 -> 256 | long-latency overlap improves |
| branch penalty 10 -> 20 | branch-heavy CPI increases |
| L1D 32K -> 64K | small working-set miss decreases |

### 5.4 Physics/monotonic regularization

Use soft constraints, not hard rules:

```text
If only DRAM latency increases, predicted cycles should not decrease
for windows with high cold-load pressure.

If only branch penalty increases, predicted cycles should not decrease
for windows with high branch_miss label/proxy.

If only MSHR increases, memory-bound CPI should not increase too much
when outstanding miss proxy is high.
```

Example loss:

```text
L_mono = relu(pred_cycles_low_latency - pred_cycles_high_latency + margin)
         * cold_load_weight
```

This should be gated by trace features. Bigger cache can sometimes cause aliasing
or policy side effects, so monotonic constraints must be applied only where the
causal proxy is strong.

### 5.5 Leave-one-uarch-out protocol

Report:

```text
train configs: A,B,C,D,E,F
heldout config: G
test workloads: seen + leave-one-workload-out
metrics:
  per-core log-CPI MAE
  cycles WAPE
  branch/cache miss MAE
  pred spread / label spread
  delta-uarch correlation
```

`delta-uarch correlation` is essential:

```text
corr(
  pred_cycles(arch_x) - pred_cycles(arch_A),
  label_cycles(arch_x) - label_cycles(arch_A)
)
```

This catches models that predict decent absolute cycles but fail to rank
microarchitectures correctly.

## 6. Innovations beyond the paper

### 6.1 Typed uarch DSL with provenance

Paper: LLM extracts `parameter = value`.

LLMSim innovation:

```text
RAG -> typed JSON DSL -> verifier -> normalized uarch embedding
```

Every value has unit, source path, evidence, confidence, and conflict resolution.
This turns LLM extraction into auditable data engineering instead of prompt magic.

### 6.2 True cross-uarch model

Paper: PM1/PM2 are evaluated per implementation.

LLMSim innovation:

```text
one model, many uarch profiles, heldout uarch configs
```

The model should answer:

```text
Given the same trace, how would CPI/PMU change if ROB/L2/MSHR/DRAM changed?
```

### 6.3 Trace sequence + uarch conditioning

Paper uses dynamic instruction counts.

LLMSim can use:

- macro assembly sequence
- reuse distance
- stride
- dependency chain
- branch target behavior
- cross-core sharing
- warm-up context

The innovation is not “more features”; it is **matching uarch knobs to trace
stressors**:

```text
L2 latency matters only when RD/footprint reaches L2.
MSHR matters only when independent miss overlap exists.
ROB matters only when dependency graph exposes ILP/MLP.
Branch penalty matters only when branch miss proxy is high.
```

### 6.4 Counterfactual paired loss

Paper trains ordinary ANN regression.

LLMSim should train on paired architectural deltas. This is the cleanest way to
learn microarchitecture causality with limited configs.

### 6.5 Active microarchitecture data collection

Use uncertainty to choose next config to simulate:

```text
1. Train on current arch set.
2. Generate candidate uarch configs.
3. Predict with ensemble / MC dropout.
4. Pick configs with high uncertainty or high delta disagreement.
5. Run gem5 only for those configs.
```

This matters because full cross product of workloads x configs is expensive.

### 6.6 Few-shot calibration to new hardware

For a new uarch:

```text
RAG extracts profile
model predicts zero-shot
run 20-100 calibration windows
fit small bias/adapter
evaluate full workload
```

This is more realistic than assuming full RTL labels for every new core.

## 7. Implementation roadmap

### Phase A: Paper-faithful reproduction (3-5 days)

Deliverables:

1. `uarch_profile_schema.json`
2. `scripts/extract_uarch_profile_rag.py` or deterministic first version
3. `scripts/build_tabular_pmu_dataset.py`
4. v27a MLP/GBDT tabular baseline

Gate:

```text
v27a beats mean/workload baseline on cycles WAPE
v27a exposes feature importances that match intuition
```

### Phase B: Structured uarch conditioning in current LLMSim (1 week)

Deliverables:

1. Add `uarch_scalars` to windows/cache.
2. Add `UarchEncoder` in `model/llm_wrapper.py`.
3. Inject `uarch_hidden` at query/head.
4. Keep tokenizer and backbone unchanged.

Gate:

```text
Same arch_A accuracy not worse than v22/v23
Synthetic arch variants produce directional deltas
```

### Phase C: Multi-uarch data (1-2 weeks collection + training)

Deliverables:

1. `arch_A` to `arch_H` configs.
2. Paired window dataset across configs.
3. `L_delta_uarch` and optional monotonic losses.
4. Leave-one-uarch-out evaluation.

Gate:

```text
heldout-uarch cycles WAPE improves over per-uarch mean baseline
delta-uarch correlation > 0.6
per-core spread does not collapse
```

### Phase D: Combine with v24 Macro Assembly

Only after Phase B/C prove uarch conditioning helps:

```text
native Macro Assembly + RAG uarch prefix + structured uarch vector
```

This tests whether LLM code semantics and RAG microarchitecture knowledge are
complementary.

### Phase E: Few-shot new-uarch adaptation

Train:

```text
uarch_adapter = f(uarch_profile)
small calibration adapter = fit on 20-100 labeled windows
```

Evaluate:

```text
zero-shot new uarch
20-window calibration
100-window calibration
full fine-tune upper bound
```

## 8. Experiment matrix

Keep variables separated:

| Exp | Trace input | Uarch input | Model | Goal |
|---|---|---|---|---|
| E0 | current uop | 4 cfg tokens | current Qwen+LoRA | baseline |
| E1 | aggregate stats | RAG typed profile | MLP/GBDT | paper-faithful v27a |
| E2 | current uop | RAG typed profile | Qwen+UarchEncoder | uarch conditioning value |
| E3 | current uop | typed profile + paired delta | Qwen+UarchEncoder | true uarch sensitivity |
| E4 | macro assembly | typed profile | Qwen-Coder regression | combine v24 + v27 |
| E5 | macro assembly | typed profile | JSON/RL | only if E4 wins |

Required reports:

```text
same-uarch validation
leave-one-workload-out
leave-one-uarch-out
leave-one-workload-and-uarch-out
delta-uarch correlation
feature/uarch sensitivity plots
```

## 9. Practical cautions

1. **Do not trust RAG values without verifier**. LLM extraction errors become silent
   label noise in every sample for that uarch.
2. **Do not train one PM per uarch and call it generalization**. That reproduces the
   paper's limitation instead of extending it.
3. **Do not vary too many uarch knobs per first config**. Attribution becomes unclear.
4. **Do not use microarchitectural oracle labels as input**. Cache hit/miss,
   misprediction, path_class, and commit timing remain labels or diagnostics only.
5. **Do not let cfg dropout hide missing uarch features**. If the model can predict
   without uarch input, the dataset may not actually test microarchitecture sensitivity.

## 10. Recommended next step

Start with v27a and Phase B in parallel:

```text
v27a tabular PM:
  quick, interpretable, paper-faithful

v27b UarchEncoder:
  minimal change to current LLMSim, tests whether rich uarch conditioning helps
```

Do not start from JSON/RL for this paper-inspired path. The paper's main lesson is
that LLMs should extract processor knowledge, while a numeric model should predict
performance.
