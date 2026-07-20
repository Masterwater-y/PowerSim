# LLMSim × TCSim v29：历史 UOP 适配方案

状态：**已被 256-macro 原生 Token 方案取代。** 日期：2026-07-17。

新的权威设计见
[LLMSim × TCSim v29：256-macro 原生 Token 方案](llmsim_tcsim_v29_macro_native_token_design.md)。
本稿保留用于记录为什么最初考虑直接兼容 256-UOP 张量接口，但不再作为实现依据。

本文记录已废止的 256-UOP 直接适配思路。以下关于 K 必须为 UOP、逐 UOP 输出和
UOP stride scheduler 的结论均已被新的 256-macro 权威方案取代。

## 1. 审查结论

结论分成两部分：

1. **架构接口是高度兼容的**：TCSim v29 的数据集、动态/跨核上下文、逐 UOP 预测头、
   损失、全局虚拟时钟和 stride scheduler 可以原样复用。LLM 只需替换或增强
   StaticTokenEncoderV29 的静态语义入口。
2. **不是把 TCSim Transformer 整体换成 LLM**：TCSim 的 FunctionalInteractionV29
   同时负责动态跨核交互，不能被一个离线静态汇编 embedding 直接替代。正确做法是
   “Coding LLM 静态语义编码器 + TCSim 动态交互主干 + TCSim 输出头/调度器”。

正确的目标结构是：

~~~text
TCSim v29 functional cache / context / targets / losses / scheduler
  + real-assembly semantic sidecar
  + Coding-LLM static encoder
  + per-UOP semantic-to-functional alignment
  -> original FunctionalInteractionV29
  -> monotonic per-UOP timing head + branch head
~~~

下面这条描述不正确，不能作为新主线：

~~~text
256 macro instructions -> LLM -> one CPI scalar
  -> EpsilonResidentScheduler
~~~

后者改变了切窗单位、预测目标、损失和调度器，已经不是 TCSim v29 同款推理框架。

## 2. 审查所依据的 TCSim 当前实现

本方案以 2026-07-17 的 TCSim v29 代码和已完成评测为事实源，不再以较早的 MVP 文档为准。

| 层 | 权威实现 | 必须继承的契约 |
|---|---|---|
| 总体推理标准 | /data00/yinhaolang/TCSim/docs/inference_framework_standard.md | 全局虚拟时间、K=256 UOP、预测游标、stride scheduler |
| 数据契约 | /data00/yinhaolang/TCSim/tcsim/v29/contracts.py | global-time-v29-packed-3；functional-only 输入 |
| 数据构建 | /data00/yinhaolang/TCSim/tcsim/v29/builder.py | per-core mmap、共同 oracle-time grid、标签隔离 |
| 训练数据 | /data00/yinhaolang/TCSim/tcsim/v29/dataset.py | 每核当前游标起始的 K 个 UOP、sequence length 4 |
| 模型 | /data00/yinhaolang/TCSim/tcsim/v29/model.py | per-token 状态、正 gap、FP64 cumsum、独立 branch head |
| 损失 | /data00/yinhaolang/TCSim/tcsim/v29/losses.py | commit/prefix/progress/drift/branch 多项损失 |
| 自由推理 | /data00/yinhaolang/TCSim/tcsim/v29/inference.py | 只用预测游标和预测时间推进，exactly-once 消费 |
| 当前基线 | /data00/yinhaolang/TCSim/docs/v29_packed3_checkpoint_evaluation_report.md | step 59000 模型、184 条闭环 trace 的结果 |

TCSim v29 当前基线报告的关键结果是：

- seed0/seed1 的 ROI workload-macro mean error 分别为 4.849% / 4.864%；
- heldout workload 平均约 10.83%，其中 Redis heldout 约 42.33%，仍是明显缺口；
- c32 memory_seq per-core MAPE 约 7.40%，rank correlation 约 0.941；
- 2.293B UOP 全部 exactly-once 消费，无 no-progress/overshoot；
- aggregate 吞吐约 517k UOP/s；
- 神经 branch head 尚不具备部署优势，gshare 仍是必须保留的基线；
- stepwise drift 尚未形成正式通过结论。

LLMSim 的价值应由相同数据、相同调度器和相同指标下的增量来证明，不能用另一套
macro/CPI 指标绕开上述基线。

## 3. 不可变的预测与推理契约

### 3.1 时间和游标

- 每条运行维护一个全局虚拟时钟 global_time。
- 每个核维护下一个尚未消费的 UOP cursor。
- 每次模型调用从每个 active core 的 cursor 读取 K=256 个功能 UOP；尾部不足时用 mask。
- 自由推理只能由预测 cursor、预测 last_commit 和功能输入构造下一步上下文。
- commit_tick、branch_miss 以及任何 oracle cache/coherence 结果只能出现在标签侧。

K=256 的单位必须是 **UOP**，不能改为 macro。宏指令语义通过 static_instruction_id
映射到 UOP，而不是通过改变调度器单位来适配 LLM。

### 3.2 逐 UOP 输出

模型必须保持与 TCSim V29ModelRunner 兼容的输出：

~~~text
retirement_gap          # [R, K]，有效 UOP 上 softplus 后为正
commit_time             # [R, K]，相对当前 global_time，FP64 cumsum 非递减
branch_miss_logit       # [R, K]，仅 branch token 计入 loss
branch_miss_probability # [R, K]
commit_logits           # [R, K, H]，H 为 horizons 数
commit_probability      # [R, K, H]
progress                # [R, H]
hard_prefix             # [R, K, H]
token_state/core_state  # horizon 输出开启时的中间状态
~~~

R 是 batch 内串接后的 active-core row 数，sample_ptr 用来恢复每个 context 的核分组；
不能假设所有样本都有固定 C 个 active core。

主目标不是窗口平均 CPI，而是每个 UOP 相对当前状态时刻的累计 commit_time。macro 数、
ROI cycles 和吞吐均由闭环中消费的 UOP 前缀确定。

### 3.3 stride scheduler

对每个 active core，取第 S 个有效 UOP 的预测 commit_time 作为候选步长，随后：

~~~text
delta = min(candidate_time_of_each_active_core)
delta = min(delta, max_step_cycles)          # 当前标准上限 1024 cycles
consume each core's maximal prefix where commit_time <= delta
global_time += delta
advance each cursor by the consumed prefix length
~~~

若某核本轮没有消费 UOP，仍可由其他核的最小候选时间推动；实现必须保留 no-progress
防护和 exactly-once 计数。

当前 v29_100m.yaml 中 target_stride 为 32，而推理标准和完整基线报告使用 256。正式实验
必须把 S 写进 run manifest，并以同一个 S 重跑基线和 LLMSim；本文建议用已报告的
S=256 作为主榜，S=32 作为调度敏感性实验，不能混报。

## 4. LLMSim 目标模型

### 4.1 职责边界

Coding LLM 负责可由 binary 静态确定的语义：

~~~text
真实指令 bytes 与汇编
mnemonic、operand kinds、register def/use、immediate/displacement
function、basic block、branch target、CFG 邻接
~~~

TCSim 的功能字段编码器继续负责每次执行会变化或 UOP 级才存在的信息：

~~~text
op class、producer distance、reuse distance、stride、macro position
load/store kind、memory size/offset、recent working set
branch history、resource class、跨核 functional relations、uarch profile
~~~

动态交互模块继续负责同一轮各核窗口之间的 local/cross-core attention。LLM 不负责猜测
cache hit/miss、coherence oracle、stall reason 或 DRAM queue。

### 4.2 UOP 对齐的静态语义融合

一条 x86 macro instruction 可能解码为多个 UOP。数据集应给每个 UOP 附加
static_instruction_id 和 micro-role；同一 macro 的 LLM embedding 被 gather 到其所有
UOP，再由 micro-role 区分各 UOP：

~~~text
H_block = CodingLLM(real basic block)
e_asm_inst = Project(pool H_block over each instruction's token span)

e_func_uop = FunctionalFieldEncoder(existing v29 per-UOP fields)
e_micro = MicroRoleEncoder(
    micro_index, micro_count, is_first, is_last, op_class
)

static_tokens = LayerNorm(
    e_asm_inst[static_instruction_id] + e_func_uop + e_micro
)

hidden = FunctionalInteractionV29(
    static_tokens,
    existing dynamic fields,
    chunk summaries,
    relation fields,
    uarch/state features
)

outputs = MonotonicTimingHead(hidden) + IndependentBranchHead(hidden)
~~~

这里的 StaticTokenEncoderV29 不是简单删除：其中会随执行实例变化的 functional categorical
fields 必须保留。LLM 只替换原来“纯静态、无法表达真实汇编语义”的部分。

### 4.3 LLM 编码粒度

- 以真实 basic block 为独立编码单元，不把互不相干的 block 串成一个因果序列。
- 若为吞吐而 packing 多个 block，必须使用 block-diagonal attention 或等价隔离。
- 从每条指令对应的 token span 池化，而不是用一个 basic-block mean 复制给所有指令。
- prompt 中保留真实 mnemonic、operand type、寄存器角色和有意义的 immediate/displacement。
- 只规范化会造成地址记忆的绝对 PC/直接跳转地址；不得把所有数字都替换成同一个 token。
- 第一版建议 frozen Coding LLM；语义门槛成立后再比较 LoRA。

## 5. 数据集重新设计

### 5.1 复用 TCSim packed3，而不是重做 macro chunks

直接复用 global-time-v29-packed-3 的：

- 共同 oracle-time 采样网格和 per-core cursor；
- K=256 UOP functional windows；
- 12 个基础 functional fields、9 个 branch fields、5 个 resource fields；
- 8 个动态跨核字段、state/chunk-summary/relation/uarch fields；
- commit_time、horizon prefix/progress、branch 标签；
- sequence length 4 的短连续序列；
- raw resource keys 只供控制逻辑使用、禁止 embedding 的规则。

LLMSim 只增加语义 sidecar，不生成另一份时间/调度标签。

### 5.2 新增静态和对齐工件

| 工件 | 必需字段 | 说明 |
|---|---|---|
| run_binary_manifest | run_id、binary build-id/hash、module map、build flags、sim commit、seed/core/uarch/warm policy、split | 阻止 binary 与运行元数据泄漏 |
| static_instruction_table | module-relative PC、bytes、真实 asm、operand/def-use、function_id、bb_id、decoder provenance | 一条真实 macro 一行 |
| static_block_table | bb bytes/asm、CFG edges、instruction ids、prompt schema version | LLM 的去重编码单元 |
| static_token_pack | input ids、attention mask、每条指令 token span | 训练可缓存，和 tokenizer/model 版本绑定 |
| per-core semantic arrays | static_instruction_id、macro_instance_id、micro_index、micro_count | 长度必须与已有 UOP stream 完全一致 |

现有 macro_pc 和 macro_end 数组继续保留。构建器按动态 UOP 的 macro_pc 查静态字典，并对
每个连续 macro instance 生成 micro_index/micro_count。任何 PC 无法映射、跨 module
歧义或 macro 边界不一致都必须 fail closed，不能回退为静默的 UNKNOWN 后继续统计 gate。

### 5.3 真实反汇编验证

静态字典必须由可验证的 decoder 生成，例如 LLVM MC/XED，或能保留完整 bytes 的
objdump -w 路径。最低验证项：

1. 指令长度在 x86 合法范围 1–15 bytes，禁止 size=0；
2. 从同一地址解码的 bytes、长度和下一 PC 连续关系一致；
3. 随机抽取动态 macro_pc，与模块映射后的静态 PC 精确 join；
4. direct branch target、fallthrough 和 CFG 边一致；
5. 动态 macro 内所有 UOP 指向同一 static_instruction_id；
6. decoder 版本、binary hash、prompt schema 均进入 cache key。

不能用“parser 再解析 parser 自己的输出”作为独立验证器。

### 5.4 标签隔离

下列字段只可用于 builder、loss 或离线评测，不能进入模型输入、prompt、cache key 的
可学习部分或自由推理 context：

~~~text
commit/fetch/issue/complete tick
branch_miss truth
cache hit/miss、MESI/coherence oracle、path class
MSHR/queue occupancy、stall reason
由上述标签直接派生的计数或分桶
~~~

尤其不能把 n_branch_miss、未来窗口真实完成数等聚合后放入 chunk summary。

### 5.5 split 与防泄漏

沿用 TCSim v29 的正式划分：

- 训练与 block validation：seed0 的 base16 workload；按 65,536-cycle time block
  划分 90/10，并设置足够 guard，保证 K lookahead 和 sequence length 4 不跨 split；
- development heldout：seed0 的 heldout7，正式比较 c4/c8/c16/c32；
- seed1 已进入当前诊断和完整报告，不得再称为 untouched test；
- 最终模型选择完成后才使用 seed2+ 作为 sealed final；
- c01 可做静态语义快速诊断，但不是多核 family_ood 主榜。

窗口级 random_split、把同一 run 的重叠窗口分到 train/val、或把已反复查看的 seed1
称作 sealed test，均不合格。

## 6. 训练目标和缓存规则

### 6.1 与 TCSim 同款损失

保持 v29 当前目标和默认权重：

| 项 | 定义 | 当前权重 |
|---|---|---:|
| commit time | smooth-L1(log1p(pred), log1p(target))，逐 UOP | 1.0 |
| horizon prefix | horizons 16/32/64/128/256/512/1024 的 BCE | 0.5 |
| horizon progress | 各 horizon 已完成 UOP 数 | 0.5 |
| cumulative drift | 短连续 sequence 的有符号累计漂移 | 0.25 |
| branch token | branch token BCE/Brier | 0.1 |
| branch count | horizon 内 branch miss count | 0.1 |

branch 项必须单独报告。鉴于当前神经 branch head 弱于 gshare，branch 未改善不应被 timing
平均分掩盖，也不应在未证明前替换部署侧 gshare。

### 6.2 LLM 梯度与缓存

Frozen 阶段可以缓存静态 embedding。LoRA 阶段只能缓存反汇编、tokenization、instruction
span 和索引，batch 内对 unique basic blocks 重新前向：

~~~text
loss -> v29 heads -> FunctionalInteractionV29
     -> gathered instruction embeddings
     -> Coding LLM hidden states -> LoRA parameters
~~~

LoRA 训练期间不得读取 detached embedding cache，否则梯度不会到达 LLM。LoRA 冻结后，
部署 embedding cache 的 key 至少包含：

~~~text
binary hash/build-id + decoder version + static instruction/block id
+ base model + checkpoint/LoRA hash + tokenizer + prompt schema + pooling version
~~~

自由推理的在线路径不应重新运行 LLM；它只 lookup 已冻结的静态 instruction embedding，
再运行动态交互主干和 heads。

## 7. 公平对照和验收门槛

### 7.1 必做对照

所有对照必须使用相同 packed3 样本、训练预算、v29 dynamic context、loss 和 scheduler：

1. 原始 TCSim v29 StaticTokenEncoderV29；
2. real asm + frozen pretrained Coding LLM；
3. real asm + Coding LLM LoRA；
4. pseudo asm；
5. mnemonic/operand shuffle；
6. random-init 或 scratch static encoder；
7. side/no-asm；
8. LLM-only 诊断，不输入 dynamic/cross-core fields；
9. register rename 与无关地址改写 invariance。

side-only 的 hidden width、主干层数和训练预算必须与 real-asm 版本匹配，不能用更小模型
制造虚假增益。

### 7.2 Gate A：数据契约

- 语义数组与每核 UOP 数逐项相等；
- 静态 join 100%，无 size=0/非法长度/未知 decoder fallback；
- label denylist 与 input allowlist 同时通过；
- block split guard、run/binary 隔离和 cache provenance 通过；
- 固定样例能从 packed3 cursor 重建与 TCSim 完全相同的 functional context。

### 7.3 Gate B：接口等价

- 关闭 LLM 语义分支时，LLMSim wrapper 可加载 TCSim v29 checkpoint，并在固定 batch 上
  复现输出、loss 和 scheduler cursor trajectory；
- commit_time 对每个有效 UOP 非递减，monotonic violation 为 0；
- 自由推理 context 中无 oracle；
- 所有 UOP/macro/branch exactly-once，结束时 cursor 精确到流尾；
- S、max_step_cycles、checkpoint、dataset contract 和 cache version 全部写入结果。

### 7.4 Gate C：语义有效性

在 development heldout 上预注册主指标为逐 UOP commit-time loss、horizon progress error
和 closed-loop ROI timing error。real asm 至少应相对最佳非语义控制降低一个主 timing
指标 5%，且其他主指标无显著退化；同时用 workload-cluster bootstrap 给出置信区间。

real asm 若不优于 pseudo/shuffle/random/side，则结论只能是“当前 LLM 语义没有增益”，
不能以训练 loss 下降或单个 batch 波动判定通过。

### 7.5 Gate D：闭环和部署

- 在相同 S 下与已报告的 v29 checkpoint 同榜比较 overall/base/heldout、per-core、
  workload-macro、tail、rank、stepwise drift；
- 不能只报平均值，必须单列 Redis heldout 等长尾；
- timing 主线不得因 branch head 失败而改用真实 branch_miss；部署继续保留 gshare 对照；
- 静态 embedding cache 后，在线路径中不得有 LLM forward；
- 首轮工程门槛建议为 aggregate UOP/s 不低于同配置 TCSim runner 的 80%，并同时报告
  GPU、batch、core count、cache hit rate；最终门槛由项目吞吐目标预注册。

只有数据、接口、语义、闭环四个 gate 都通过，才可声称“LLM 成功替代 TCSim 的静态
Transformer 编码部分”。

## 8. 对当前 LLMSim Phase 0/1 smoke 的判定

当前 smoke 证明训练脚本可以运行，但**不能证明方案正确或语义有效**。审查中已发现：

- build_static_dict 产出约 3,266,447 行，其中 6,331 行 size=0，最大指令长度仅 7，
  与 x86 真实指令长度范围和预期解析行为不符；
- 静态 verifier 与 builder 同源，无法独立证明反汇编正确；
- prompt 规范化掩盖了普通 immediate，同时保留部分 direct branch PC，既损失语义又可能
  记忆地址；
- macro chunks 含由真实 branch_miss 派生的字段，并使用不等价的标量回归目标；
- Phase 1 DDP validation 未做跨 rank 聚合：c1 数据集实际 17,214 个样本，报告仅统计
  rank0 的 2,152 个样本；
- side-only 与 Qwen 分支宽度不一致，消融不公平；
- model predictor 接口仍是 stub，尚未接入 TCSim v29 自由推理；
- 本次 gate 中 real WAPE 约 0.2491、pseudo 约 0.3475、side-only 约 0.2661；
  real 相对 pseudo 较好，但相对 side-only 仅约 6.37%，且上述数据与统计问题使该数字
  不具正式效力。

因此，driver 的 PASS 只能改名为 pipeline smoke PASS。它不能进入论文表格、模型选择或
后续 LoRA 扩大训练的决策依据。loss 从 step 0 到 step 40 的下降也只能说明优化器工作，
不是方案验收。

## 9. 实施顺序

### Phase 0：冻结兼容层

1. 在 LLMSim 中 vendor 或版本锁定 TCSim v29 contracts/dataset/model/loss/inference API；
2. 做 checkpoint replay 和 fixed-batch equivalence test；
3. 做 cursor trajectory、monotonicity、oracle-denylist、exactly-once 测试。

### Phase 1：构建语义 sidecar

1. 从每个 binary 的 module-relative PC 构建可验证静态字典和 basic blocks；
2. 将每个 packed3 UOP 映射到 static_instruction_id 和 micro-role；
3. 建 token/span cache，并通过 Gate A；
4. 废止当前 v28_1_macro_chunks 作为正式训练数据。

### Phase 2：最小语义适配器

1. 先 frozen Coding LLM + instruction-span pooling + projection；
2. 保持 FunctionalInteractionV29、heads、loss 和 scheduler 不变；
3. 完成全部语义消融和 Gate B/C。

### Phase 3：LoRA 与部署

1. 只有 frozen real-asm 已优于控制组后才训练 LoRA；
2. 比较 Q/K/V/O 与 all-linear LoRA；
3. 冻结后导出带完整 provenance 的静态 embedding cache；
4. 在相同 S 和相同 184-trace/最终 sealed 集上完成 Gate D。

## 10. 代码落地边界

首版新增模块建议保持很小：

~~~text
data/build_v29_semantic_sidecar.py
data/validate_v29_semantic_sidecar.py
model/real_asm_static_encoder.py
model/tcsim_v29_llm_adapter.py
train/train_v29_semantic_adapter.py
eval/eval_v29_semantic_ablation.py
tests/test_v29_contract_equivalence.py
tests/test_v29_free_inference_no_oracle.py
~~~

旧的 build_v28_1_macro_chunks.py、dataset_phase1.py、phase1_model.py 和
eval_fixed_chunk.py 保留为历史 smoke，不能继续扩写成正式主线。正式实现首先应证明
TCSim v29 接口等价，然后只增加 semantic sidecar；任何额外更改都作为单独消融。
