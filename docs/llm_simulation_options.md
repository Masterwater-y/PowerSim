# LLM CPU Performance Simulation: Candidate Designs And Decision Record

Status: active design record. Last updated: 2026-07-14.

This document consolidates the currently viable LLMSim and TSim directions.
Its purpose is to distinguish implemented baselines from research prototypes and
from the recommended target architecture. A window-level aggregate CPI result is
not sufficient evidence to promote a design: each candidate must be judged on
per-core CPI, slow-core/rank behavior, closed-loop rollout, core-count OOD, and
runtime.

## 1. Shared Problem Contract

Given a binary, a typed microarchitecture profile, and per-core functional
execution traces, estimate per-core performance for fixed functional chunks.

```text
allowed input:
  static instruction identity / assembly, program-order dynamic trace,
  branch outcome, load/store/atomic/fence kind, functional address relation,
  dependency, explicit synchronization, deployment-available history, uarch
  profile

labels only:
  commit/issue/fetch tick, cache and TLB outcomes, branch mispredict outcome,
  MESI owner/sharer state, MSHR occupancy, stall reason, DRAM queue state

primary output:
  additive per-core cycles or log(cycles / retired macro)

derived outputs:
  macro CPI, uop CPI, aggregate CPI, throughput, makespan

auxiliary outputs:
  branch/cache/TLB/coherence/DRAM event counts and uncertainty
```

The deployed timing system must use fixed functional chunk boundaries. It may
use predictions to advance time and state, but it must not use current-window
ground-truth ticks to select model inputs.

## 2. Candidate Summary

| ID | Design | LLM role | Dynamic/shared-system model | Status | Main use |
| --- | --- | --- | --- | --- | --- |
| A | v22 custom-token Qwen regression | Qwen is a generic sequence backbone | Learned attention plus functional side features | Implemented baseline | Historical comparison |
| B | v24 native-BPE pseudo-assembly regression | Qwen sees code-like native tokens | Same as A | Worktree prototype | Tokenization/semantic gate only |
| C | True-assembly Coding LLM + continuous head | Static code semantic encoder | Separate dynamic and cross-core modules | Proposed | LLM semantic mainline pilot |
| D | Coding LLM direct JSON CPI/PMU generation | Both encoder and numeric generator | Usually implicit in LLM prompt | Proposed control | Test value of generative output |
| E | TSim v26 structured QKVR | No pretrained LLM | Full per-UOP local and cross-core attention | Implemented experiment | Strong non-LLM sequence baseline |
| F | TSim v27 shared-state proxy / temporal solver | No pretrained LLM | Explicit functional cacheline ownership/history proxy | Partly implemented; temporal solver is a design | Shared-state and timing baseline |
| G | Hybrid target: Coding LLM + dynamic encoder + sparse shared module | Static semantic encoder, optionally offline teacher | Explicit/sparse stateful resource interaction | Recommended | Production/research mainline |
| H | RAG-extracted typed uarch profile + tabular PM | Offline documentation/source extraction only | None beyond feature engineering | Proposed baseline | Cross-uarch lower bound and data-quality gate |

## 3. A: v22 Custom-Token Qwen Regression

```text
functional uops
  -> <UOP> plus six custom fields
  -> UopEncoder replacement embeddings
  -> Qwen3-0.6B + Q/K/V/O LoRA
  -> per-core QUERY/LOCAL hidden fusion
  -> regression head: CPI plus PMU counts
```

Current implementation:

- `model/tokenizer.py` encodes opclass, register bucket, memory kind, reuse
  distance, stride, and branch class.
- `model/llm_wrapper.py` injects UOP embeddings and gathers a query hidden state
  for each core; LOCAL tokens, side features, and optional timing features are
  fused before the head.
- `model/regression_head.py` outputs `cpi_uop`, branch miss, L1/L2/LLC miss
  counts.

Strengths:

- Existing train/eval/cache pipeline and checkpoints.
- Pure functional input boundary is well understood.
- Continuous head is suitable for CPI and PMU regression.

Limits:

- Nearly all trace tokens are newly learned symbols, so Qwen pretraining has
  little direct code-semantic benefit.
- Tail query representations collapse across cores at high core count.
- Shared LLC/DRAM/coherence effects are represented only indirectly by summary
  features and attention.

Decision: retain as a historical baseline. Do not use improvements over this
route alone as evidence of Coding LLM semantic benefit.

## 4. B: v24 Native-BPE Pseudo-Assembly Regression

This prototype rewrites v22 UOP fields into short code-like text such as:

```text
mov rax rbx seq hot
imul rcx rdx
jne L
```

Only a small set of structural tokens remains special; the rest is tokenized by
the Coding LLM's native vocabulary. The output remains a continuous regression
head, not generated text.

Strengths:

- Tests token budget and whether native code vocabulary is easier to optimize.
- Keeps the stable continuous regression objective.
- Does not require rebuilding raw traces for a smoke experiment.

Blocking limitation:

- Current raw fields do not encode the true mnemonic, operand, register, or
  instruction bytes. The renderer derives pseudo mnemonics and pseudo register
  names from coarse fields. It is not true assembly and cannot establish that
  a Coding LLM's code pretraining helped.

Decision: use only as a tokenizer/optimization ablation. A positive result is
not a semantic claim; a negative result does not falsify true-assembly LLM
encoding.

## 5. C: True-Assembly Coding LLM With Continuous Regression

This is the recommended LLM semantic pilot and the semantic front end of the
hybrid target.

### 5.1 Inputs

Build a static instruction dictionary once per binary:

```text
binary hash + module-relative PC
  -> instruction bytes
  -> real mnemonic, operands, architectural registers, block/CFG identity
```

At runtime, attach functional dynamic annotations to the static instruction:

```text
branch direction/target class, load/store/atomic/fence, access size,
window-local line alias, reuse/stride, producer distance, sync annotation
```

Absolute addresses, workload names, run seeds, cache hit/miss outcomes, and
timing oracle values must not enter this representation.

### 5.2 Model

```text
real assembly tokens --Coding LLM--> static instruction/block embedding
                                      |
dynamic numeric sidecar --> local temporal encoder --> per-core demand vector
                                      |
                         cross-core module --> continuous cycles/PMU head
```

The Coding LLM is responsible for static code interpretation. It is not asked
to infer exact cache state from text and is not the numeric output decoder.
Static embeddings are cacheable by binary and block, reducing repeated work on
loop bodies.

### 5.3 Training

- Freeze native token embeddings and LM head.
- Train pooling, dynamic encoder, shared module, and regression heads.
- Start with LoRA on attention and MLP projections; increase rank or unfreeze
  top layers only after evidence of shared underfitting.
- Train on continuous log cycles per macro plus aggregate, centered per-core,
  rank, auxiliary-event, and rollout losses.

Decision: build after a true-assembly extraction gate and strict grouped split
are available. This is the correct experiment for the question "does Coding
LLM pretraining improve CPU performance prediction?".

## 6. D: End-to-End Generative CPI/PMU JSON

Example:

```text
trace prompt -> Coding LLM LM head -> {"c0":842,"c1":915,...}
```

Here `842` represents, for example, `round(CPI * 1000)`. This is a legitimate
research control but not the default deployment architecture.

Advantages:

- A single model can consume a human-readable trace and produce a structured
  artifact.
- Instruct models can also produce bottleneck explanations and diagnostics.

Risks:

- Next-token cross entropy treats numeric values as categories: 842 is not
  inherently closer to 843 than to 5000.
- Multi-token numbers and JSON grammar introduce parsing and decoding failure.
- Per-core output becomes autoregressive and expensive at c32.
- JSON validity does not imply numerical accuracy, additive cycle consistency,
  or correct slow-core ordering.
- A monolithic prompt still cannot observe missing cache/DRAM/coherence state.

Required fair comparison:

| Variant | Output objective |
| --- | --- |
| R1 | Same Coding LLM, continuous regression head |
| R2 | Constrained JSON generation, completion-only CE |
| R3 | JSON CE plus number-aware expected-value/ordinal loss |
| R4 | JSON generation plus a small RL stage only after supervised convergence |

All variants must use the same assembly input, split, token budget, and closed
rollout protocol. Report JSON validity, numeric error, per-core rank/spread,
rollout drift, and decode latency. RL is not a first-stage remedy for missing
state or weak supervision.

## 7. E: TSim v26 Structured QKVR

TSim is a standalone branch derived from LLMSim. It is a learned surrogate, not
a conventional deterministic timing simulator and not an LLM system.

```text
structured uop tensor [batch, core, uop, field]
  -> per-field embeddings + MLP
  -> same-core Q/K/V self attention
  -> other-core R-query attention over K/V
  -> per-core pooling
  -> CPI and PMU head
```

The v26 input has 14 functional fields: the original six fields plus PC,
macro-position, line identity/role, same-core history, cross-core memory,
coherence proxy, and access fanout. Each UOP can therefore attend both to its
local sequence and to all other active cores' UOPs.

Strengths:

- Strong non-LLM baseline for sequence capacity and cross-core attention.
- No tokenizer or special-token ambiguity.
- Explicitly tests whether per-UOP cross-core context fixes query-head collapse.

Limits:

- No pretrained code or assembly semantic prior.
- Dense cross-core UOP attention becomes costly as core count and sequence
  length grow.
- It learns shared-system behavior from proxies rather than an explicit cache,
  coherence, or DRAM contract.

Decision: retain as the main non-LLM comparison for candidate C/G. A Coding LLM
route must beat or complement it under an identical data contract, not merely
beat v22.

## 8. F: TSim v27 Shared-State Proxy And Temporal Solver

`TSim/model/shared_state.py` contains a deployable functional-state engine.
For each cacheline it tracks a conservative owner, last writer, last touch,
sharer set, touch count, and per-core/global EMAs. It exposes bucketized
features such as remote writer, owner change, conflict kind, and time since
touch.

```text
functional memory event -> shared-state peek -> model feature
                         -> after predicted window, replay/update state
```

It does not consume gem5 miss labels, MESI oracle, MSHR depth, or true runtime
state. It is therefore a valid deployable proxy, not a cycle-accurate coherence
simulator.

The newer temporal-graph/resident-chunk design adds a stronger timing contract:

- chunks have immutable functional boundaries;
- a slow resident chunk may be referenced by several fast chunks but is
  accounted and committed exactly once;
- predicted chunk durations form per-core prefix times;
- same-line/resource/sync edges are refined in a small number of temporal
  passes.

The shared-state proxy is implemented; the full resident/temporal graph solver
is a design/MVP direction, not a completed production path.

Decision: reuse this as the initial L3 state interface for candidate G, while
keeping its limitations explicit. Do not mistake teacher-tick state construction
for a deployment-pure timing solution.

## 9. G: Recommended Hybrid Target

```text
L1 static semantic encoder:
  real assembly -> Coding LLM -> cached block/instruction embedding

L2 local dynamic encoder:
  static embedding + functional dynamic annotations + local history
  -> per-core chunk demand and base duration

L3 shared-system interaction:
  same-line/resource/sync sparse graph + shared-state engine
  -> interference, queue/ownership pressure, refined duration

L4 numeric readout:
  additive cycles, uncertainty, branch/cache/coherence auxiliary events

L5 scheduler:
  fixed functional chunks, predicted prefix time, exactly-once state commit
```

Why this is preferred:

- It assigns code semantics to the Coding LLM, where pretraining is relevant.
- It keeps continuous numerical quantities in numerical modules.
- It represents cross-core interactions sparsely, instead of forcing all c32
  UOPs into one dense prompt or all-to-all attention.
- It makes cache/coherence/DRAM assumptions testable and replaceable.
- It allows static embedding caching and a smaller online dynamic model.

The first implementation should use a Set Transformer or sparse same-line graph
over per-core/chunk summaries, plus the existing shared-state proxy. An explicit
cache/coherence/DRAM simulator is a stronger later baseline, not a prerequisite
for the first semantic pilot.

## 10. H: RAG-Extracted Uarch Profile And Tabular PM

This is not a per-window LLM route. A RAG/constrained-LLM pipeline extracts a
typed profile from gem5 configuration, source, RTL, or documentation:

```text
source/docs -> LLM extraction -> verifier -> typed uarch JSON
functional summary + uarch vector -> MLP / GBDT / FT-Transformer -> PMU/cycles
```

The verifier must normalize units, check ranges, preserve evidence/provenance,
and resolve source/config/document conflicts. The result provides:

- a cross-uarch feature contract;
- a cheap lower-bound baseline;
- a data-quality gate before introducing a long sequence model.

It does not replace trace sequence modeling or multi-core shared state.

## 11. Required Evaluation Protocol

Every promoted route must pass all of the following.

1. Grouped split before window construction: no `run_id`, binary family,
   input/seed, or overlapping window cluster crosses train/validation/test.
2. Open-loop evaluation on fixed functional chunks: per-core cycle/CPI, PMU,
   rank, spread, and tail metrics.
3. Closed-loop rollout: no true tick in window selection or state update;
   report cumulative cycle drift, makespan, and throughput.
4. Core-count and workload-family OOD: especially c32 memory-pressure,
   coherence, and phase-heavy cases.
5. Cost: report static-cache hit rate, encode/forward/decode time, memory, and
   UOP/s.
6. Attribution ablation: scratch transformer, no-assembly, shuffled assembly,
   no-shared-state, and tabular-only baselines.

Aggregate CPI alone is insufficient. A design that has low aggregate error but
poor per-core spread or slow-core top-k accuracy is unsuitable for straggler
analysis, scheduling, or shared-system simulation.

## 12. Decision Sequence

1. Freeze v22 and TSim v26/v27 as reproducible baselines.
2. Build and audit true macro assembly recovery from binary PCs.
3. Establish grouped splits, additive cycle labels, and fixed functional chunks.
4. Run H to measure how much typed-uarch plus summaries already explain.
5. Run C against E with identical data, loss, and shared-state inputs.
6. Add the initial G sparse/shared-state module only if C/E show that local
   representations are useful but cross-core pressure remains the dominant
   error source.
7. Run D only as a controlled output-head experiment after C is stable.
8. Promote a route only if it improves sealed OOD and closed-loop metrics, not
   just random-window validation loss.

## 13. Coding LLM And Assembly-Model Selection

Code-generation benchmarks are not evidence of x86 performance-modeling skill.
For this project, a candidate must be evaluated on real disassembly, functional
dynamic sidecars, core-count OOD, and closed-loop cycle prediction. In
particular, no current public model should be assumed to infer cache, DRAM, or
coherence state from assembly text alone.

### 13.1 Recommended Candidate Matrix

| Stage | Candidate | Role | Rationale | Availability / constraint |
| --- | --- | --- | --- | --- |
| P0 | `Qwen2.5-Coder-1.5B-Instruct` | Zero-shot semantic gate | Check whether a readable x86 macro trace is recognized as compute, branch, or memory dominated | Present locally; use only for prompt/semantic inspection |
| P1 | `Qwen2.5-Coder-3B` Base | First continuous-regression pilot | Small enough for 32K-context LoRA; isolates the effect of Coding LLM embeddings from output generation | Present locally; preferred first training run |
| P2 | `deepseek-coder-6.7b-base` | Legacy cross-family assembly control | Its official training-language list explicitly contains `assembly`; a positive result is stronger evidence than a Qwen-only comparison | Original 2023 series; must obtain weights; 16K context requires local chunking |
| P3 | `Qwen2.5-Coder-7B` Base | Capacity scaling | Tests whether semantic benefit scales after P1 has passed | Must obtain weights; retain the identical input/loss/split |
| T1 | `Qwen3-Coder-480B-A35B-Instruct` | Offline static teacher | Large code-pretrained MoE suitable for cached block embeddings or semantic labels | 480B total parameters; not an online LoRA target |
| S1 | CLAP / NOVA / ASMA-Tune | Assembly-specialist control | Tests whether binary/assembly-specific pretraining beats generic code pretraining for static representation | Audit ISA coverage, license, weights, and reproducibility before use |

The local Hugging Face cache currently contains only
`Qwen2.5-Coder-1.5B-Instruct`, `Qwen2.5-Coder-3B`, and
`Qwen2.5-Coder-3B-Instruct` among the coding candidates. Model acquisition
must not silently change the tokenizer or dataset-cache schema.

### 13.2 Why The First Pilot Is Qwen2.5-Coder-3B Base

Use the Base model with a continuous head for the first performance-modeling
experiment:

```text
real x86 macro assembly + functional dynamic sidecar
  -> Qwen2.5-Coder-3B Base + LoRA
  -> local/dynamic/shared-system modules
  -> continuous cycles and PMU head
```

This is not a claim that Qwen is known to be the best assembly model. It is the
lowest-friction test because its weights are already local, it has a Base
checkpoint suitable for representation learning, and its size is compatible
with the current 32K long-context LoRA setup. The Instruct checkpoint is useful
for a zero-shot prompt gate or JSON-generation control, not the default
regression backbone.

### 13.3 Cross-Family And Large-Model Controls

`DeepSeek-Coder` is the required cross-family control because its official
repository explicitly lists `assembly` among its supported programming
languages. The original DeepSeek-Coder series predates Qwen2.5-Coder, so it is
not a latest-model candidate: use it to test explicit assembly exposure across
model families, not as a newer baseline. This does not prove x86
microarchitecture knowledge. The official checkpoint name is
`deepseek-coder-6.7b-base` (often rounded to "7B"). Compare it directly with
Qwen using the same true macro input, dynamic features, tokenizer-budget rule,
regression head, and grouped split.

`Qwen3-Coder-480B-A35B-Instruct` has 480B total MoE parameters and 35B active
parameters, with a reported 70% code pretraining mixture and native 256K
context. It should be considered only for offline block/function embeddings,
semantic label generation, or teacher-student distillation. Active parameters
lower compute but do not remove the need to store all expert weights; it is not
an appropriate per-window BF16 LoRA model for the current eight-H20 setup.

Devstral 2 and other agentic code models are not priority candidates here. They
are optimized and reported primarily for repository-level software-engineering
agents; absent evidence of x86/disassembly pretraining, they add a confounding
agent/post-training difference rather than a clean assembly semantic control.

### 13.4 Assembly-Specialist Models

Assembly-specific models should be treated as static-embedding controls, not as
complete CPU simulators:

- PalmTree and BinBERT target instruction/binary representation, not dynamic
  PMU or cycle regression.
- CLAP aligns binary assembly with natural-language semantic descriptions and
  is useful as an embedding-teacher comparison.
- NOVA is an ICLR 2025 generative assembly/binary model, and ASMA-Tune is a
  2025 structural-semantic assembly instruction-tuning recipe. Both may be
  useful for data/representation ablations after their x86 support and released
  checkpoints are audited.

None supplies the deployment-available cache/TLB/predictor/DRAM/coherence
state required by L2/L3. A specialist model can improve static instruction
representation, but cannot replace functional history, shared-state tracking,
or the timing scheduler.

### 13.5 Promotion Gates

A model is promoted from P0/P1 only if all of the following hold relative to
TSim v26 and the v22 Qwen baseline:

1. On a sealed group split, it improves per-core cycle error and slow-core
   rank/spread, not only aggregate CPI.
2. The gain remains when workload names, absolute addresses, and run identity
   are absent from input.
3. It remains positive under closed-loop rollout with no current-window true
   tick or label-derived state feature.
4. A scratch transformer of comparable online capacity cannot reproduce the
   gain; otherwise the result is capacity, not pretrained code semantics.
5. Runtime and static-embedding cache behavior meet the intended deployment
   budget.

### 13.6 External Primary References

- Qwen2.5 and Qwen2.5-Coder family:
  <https://qwenlm.github.io/blog/qwen2.5/>
- Qwen3-Coder release and pretraining/context details:
  <https://qwenlm.github.io/blog/qwen3-coder/>
- DeepSeek-Coder training setup and supported-language list:
  <https://github.com/deepseek-ai/DeepSeek-Coder>
- DeepSeek-Coder-V2:
  <https://github.com/deepseek-ai/DeepSeek-Coder-V2>
- CLAP binary-assembly representation:
  <https://arxiv.org/abs/2402.16928>
- NOVA assembly/binary model:
  <https://openreview.net/forum?id=4ytRL3HJrq>
- ASMA-Tune assembly instruction tuning:
  <https://arxiv.org/abs/2503.11617>

## 14. Source Documents And Implementations

- LLMSim target architecture: `docs/llm_multicore_cpu_simulation_blueprint.md`
- Native-token and JSON proposal: `docs/pre_v26_native_token_json_rl.md`
- RAG/uarch proposal: `docs/pre_v27_rag_uarch_generalization.md`
- Current Qwen baseline: `model/llm_wrapper.py`, `model/regression_head.py`
- TSim structured QKVR: `/data00/yinhaolang/TSim/model/v26_kvqr.py`
- TSim shared-state engine: `/data00/yinhaolang/TSim/model/shared_state.py`
- TSim temporal graph review:
  `/data00/yinhaolang/TSim/docs/v27_functional_temporal_graph_design_review.md`
