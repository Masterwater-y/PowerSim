# LLMSim × TCSim v29：256-macro 原生 Token 方案

状态：当前权威设计。日期：2026-07-17。

在保留 Coding LLM 语义前提下压缩部署输入、以 c8 合计 `>=10K macro/s` 为目标的后续
后继主线见 [离线 LLM 语义编码 + 在线 Macro-level Qwen 设计](macro_v29_offline_semantic_online_qwen_design.md)。其代码已实现但尚未完成 8K 训练和部署验收；本文继续作为已训练的 native-token baseline 合同。

本文根据项目决策，把 LLMSim 的语义窗口从 256 UOP 修正为 **256 条动态 macro
instruction**。它保留 TCSim v29 的全局虚拟时钟、逐项单调完成时间、horizon
prefix/progress loss 和 stride scheduler，但把这些机制的基本单位从 UOP 改为 macro。

## 1. 两个直接答案

### 1.1 K 应该是什么单位

K 固定为：

~~~text
K_macro = 256 dynamic macro instructions / active core / model call
~~~

LLM 不读取 gem5 micro-op 文本，也不学习伪造的 UOP 语言。gem5 UOP 流只作为离线构建
和闭环控制的底层事实源，用来：

- 找出 macro 边界；
- 聚合每条 macro 的功能执行特征；
- 取得 macro 最后一个 UOP 的 commit tick 作为监督；
- 把 scheduler 消费的 macro 前缀映射回底层 UOP 区间；
- 更新地址、共享 line、resource equality 等功能状态和统计 UOP 吞吐。

抽查当前 packed3 的四类 trace，每条 macro 平均对应约 1.15、1.18、1.27、1.49 个 UOP。
因此 256 macro 通常覆盖约 295–382 个 UOP，数据结构必须支持变长 UOP span，不能再假设
一个模型窗口恒等于 256 UOP。

### 1.2 是否百分百使用 LLM 原生 token

需要区分“LLM tokenizer 的文本流”和“整个 timing model 的全部输入”：

| 范围 | 是否 100% 原生 token | 决策 |
|---|---|---|
| 真实汇编文本送入 Coding LLM | **是** | 只调用原模型 tokenizer，不增加 UOP/custom vocabulary |
| 动态数值、跨核关系、uarch 状态 | **否** | 使用 typed embedding/MLP/side adapter，不文本化 |
| 整个 timing model | **不是纯文本模型** | native-asm LLM + structured side channel |

这是刻意设计。真实汇编属于 Coding LLM 的预训练分布；reuse distance、stride bucket、
working-set、active-core fraction 和 cache-set equality 是结构化量。把这些量全部写成
十进制文本，虽然形式上也是“原生 token”，却会浪费上下文、破坏数值归纳偏置并增加
地址记忆和标签泄漏风险。

所以准确表述是：

> **汇编 token 流 100% 原生；完整模型输入不是 100% 文本。**

### 1.3 当前 macro-v29 实际输入是什么

新的主线实现位于 `train/macro_v29_dataset.py`、`model/macro_v29_model.py`，实际链路为：

~~~text
每核下一段 256 dynamic macro PCs
  -> validated real-x86 static dictionary
  -> 按动态顺序渲染 mnemonic + normalized operands + newline
  -> AutoTokenizer.from_pretrained(base_model), add_special_tokens=False
  -> input_ids + per-macro token spans
  -> Qwen hidden states -> instruction span mean pooling

同一批 macro 覆盖的全部 gem5 UOP functional records
  -> ragged typed fields + uop_to_macro segment pooling
  -> dynamic8 + summary38 + relation22 + state5 + uarch29

native asm hidden + structured side -> [R,256] 单调 commit time
~~~

因此当前 macro-v29 `input_ids` 已经是动态 macro 顺序，并且每个 ID 都由未经扩词的 Qwen
原生 tokenizer 产生；没有 pseudo-UOP/custom vocabulary，也没有把数值向量伪装成 token
ID。旧的 BB/CPI Phase-1 文件仍保留为历史基线，但不是 macro-v29 训练入口。

这里的“真实汇编”指由 ELF bytes 校验过的 x86 mnemonic/operands；为防止记住程序身份，
direct branch 的原始绝对 PC 会改成窗口相对 label，所以它是**语义保持的规范化汇编**，
不是原始 objdump 文本逐字复制。

## 2. macro-v29 总体架构

~~~text
每个 active core 的下 256 条动态 macro
  |
  +-- real assembly renderer
  |     -> original Coding-LLM tokenizer
  |     -> input_ids / attention_mask
  |     -> Coding LLM (+ optional LoRA)
  |     -> instruction-span hidden states
  |
  +-- macro functional builder
        -> dynamic/dependency/memory/branch/resource fields
        -> state/relation/uarch fields
        -> NumericEncoder / gated side adapters

native-token hidden + structured side conditioning
  -> macro hidden[256]
  -> thin permutation-equivariant cross-core mixer
  -> positive macro retirement gaps
  -> FP64 cumulative macro commit times
  -> horizon prefix/progress + branch heads
  -> macro stride scheduler
  -> consumed macro prefix -> underlying UOP span/state updates
~~~

Coding LLM 替换 TCSim FunctionalInteractionV29 的主要 local sequence backbone。保留的
cross-core mixer 只负责不同核之间的置换等变交互，不再重新做一套大型本地 Transformer。

## 3. 数据输入契约

### 3.1 batch 与窗口

batch 把所有 active-core rows 串接为 R 行，sample_ptr 恢复同一个全局 context 中有哪些核：

~~~text
R                 = batch 内 active-core row 总数
M                 = 256 macro
L_r               = 第 r 个核的 native-token 长度
H                 = horizon 数

input_ids          [R, L_max]
attention_mask     [R, L_max]
token_to_macro     [R, L_max]        # -1 表示非指令/填充 token
macro_token_start  [R, M]
macro_token_end    [R, M]            # 半开区间
valid_macro_mask   [R, M]
sample_ptr         [n_context + 1]
~~~

尾部不足 256 macro 时只 pad macro slots 和 token sequence。绝不截断一条指令；当前若
完整 256-macro 文本超过模型上下文会 fail closed。未来若出现真实 overflow，必须显式
实现分段编码并恢复 256 个 instruction hidden，不能静默丢掉尾部 macro。现有全 suite
全部 1,709,004,226 个滑窗的保守 token 上界最大为 2,626，低于当前 4,096 限制。

### 3.2 原生汇编 token 流

每个核按**动态执行顺序**渲染接下来的 256 条真实 macro，例如：

~~~asm
mov rax, qword ptr [rbx + 0x40]
add rcx, rax
cmp rcx, rdx
jne .L_back_0
~~~

规则如下：

- 使用 binary bytes 经独立 decoder 恢复的真实 Intel 或 AT&T 语法，整个项目固定一种；
- 使用 Coding LLM 自带 tokenizer，当前 `add_special_tokens=False`；不新增
  `<UOP_*>`、`<OPCLASS_*>` 等 token；
- 每条动态 macro 占一行，换行符作为原 tokenizer 能识别的边界；
- 保留 mnemonic、operand size、register role、small immediate 和 displacement；
- module-relative PC 不进入文本；
- direct branch 的绝对地址改为窗口内/窗口外局部 label，不保留可记忆的原始 PC；
- effective address、paddr、cache line、commit tick 和 branch_miss truth 不进入文本；
- 同一个 loop body 在动态序列中重复出现，保留真实执行顺序，使因果 LLM 能看到局部路径。

token_to_macro 和 macro_token_start/end 是 loader 的对齐元数据，不作为 token 喂给 LLM。
每条指令的语义向量由其 token span 的最后有效 token 或 attention pooling 得到，两者必须
做消融，不能用一个 basic-block mean 复制给所有指令。

### 3.3 macro 结构化侧通道

每个核窗口另有 ragged typed fields；`U_batch` 是当前 batch 中单核窗口的最大真实
UOP 总数，只用于 batch padding，不是 per-macro 截断上限：

~~~text
uop_fields          [R, U_batch, 26]
uop_valid_mask      [R, U_batch]
uop_to_macro        [R, U_batch]       # 0..255，padding=-1
dynamic_uop_fields  [R, U_batch, 8]
uop_access/flags    [R, U_batch]
uop_count           [R, 256]
state_features      [R, F_state]
relation_features   [R, F_relation]
uarch_features      [R, F_uarch]
chunk_summary       [R, F_summary]
~~~

结构化 UOP 由现有 per-UOP functional arrays 按 macro_end 分组并在窗口内展平。模型先编码
每个有效 UOP，再按 `uop_to_macro` 做 segment pooling；segment count 必须与 `uop_count`
逐项相等。它们不是语言语料，也不会进入 tokenizer。全 suite 审计发现
`int_div_serial` 的 UOP/macro p99=45、p100=60，因此固定 `U_max=8` 或简单扩大到 64 都不
正确：前者丢失/拒绝合法微码 macro，后者在 c32×sequence4 中产生巨大 padding 激活。
ragged 表示无损保留所有 UOP，同时只按当前 batch 的实际总 UOP 数 padding。每个 macro
另计算以下确定性 aggregate：

| 字段组 | macro 级表示 | reducer/来源 |
|---|---|---|
| 解码形态 | uop_count、op-class histogram、serialize/atomic flags | count/sum/any；不转成 UOP 文本 |
| 依赖 | producer-distance min/mean/max bucket、dependency fan-in/out | macro 内有效 UOP 聚合 |
| memory | load/store/atomic 数、size、line offset、reuse、stride、page/line 数 | 每次访问保留 fixed slots，额外做 aggregate |
| branch | kind、taken、successor delta、history、RAS depth | branch macro 的功能真值；禁止 miss truth |
| resource | set/bank/row reuse 与 pressure bucket | exact key 只用于相等性/冲突计算 |
| local history | recent working set、same-core history | 取 macro 开始前的 causal state |
| cross-core | shared-line role/fanout、set/bank/channel relation | 从当前各核功能窗口和预测 cursor 构建 |

raw PC、paddr、physical line、set/bank/channel exact ID 仍是 control-only keys，只用于
构造 dynamic8/relation22，禁止直接 embedding。`branch_mask` 必须来自静态 x86 macro
分类；微码（例如 `div`）内部控制 UOP 的 branch/miss marker 不是架构分支标签。

### 3.4 为什么不把 side channel 也转成文本

例如下面的 all-text 输入不是主线：

~~~text
mov rax,[rbx+64] taken=0 reuse=7 producer_distance=13
shared_line_fanout=3 llc_set_pressure=5 ...
~~~

它有四个问题：

1. 每条 macro 会从约数个汇编 token 膨胀到数十个 token，256-macro/c32 成本过高；
2. tokenizer 对 13、14、130 的表示不保证有连续数值关系；
3. 字段名和格式容易主导 hidden，反而削弱真实汇编预训练语义；
4. raw 数值或地址规范化稍有错误就形成 run/workload identity shortcut。

可以把 “all-text-native” 作为消融，但不能作为默认数据契约。

## 4. LLM 与数值输入如何融合

### 4.1 第一阶段：late fusion 语义 gate

先用最可解释的版本验证预训练语义是否有用：

~~~text
H_token = CodingLLM(input_ids, attention_mask)
Z_asm[m] = SpanPool(H_token[macro_token_start[m]:macro_token_end[m]])
Z_uop = StructuredUopEncoder(
    uop_fields, dynamic_uop_fields, uop_valid_mask, access/flags
)
Z_num[m] = SegmentPool(Z_uop, uop_to_macro == m)
Z_macro[m] = LayerNorm(P_asm(Z_asm[m]) + P_num(Z_num[m]) + position[m])
~~~

此阶段可冻结 LLM，只训练 projection、numeric encoder、cross-core mixer 和 heads。它能
直接与 pseudo/shuffle/random/side-only 比较，回答“原生汇编 hidden 是否带来增益”。

语义 gate v1 的变体预注册如下，不能在看到指标后改变定义：

| 变体 | 唯一允许变化 | 其余输入/容量 |
|---|---|---|
| `real` | 真实规范化 x86 汇编 + pretrained Coding LLM | 主线 |
| `pseudo` | 每条 macro 只保留 control/memory/compare/other 粗类，渲染成固定原生汇编模板 | side、split、模型不变 |
| `mnemonic_shuffle` | 在每个 256-macro 窗口内确定性打乱 mnemonic，保持 mnemonic 频率与每个位置 operands | 其余不变 |
| `random_init` | 同一个 Qwen 架构由固定 seed 随机初始化 | real tokens、训练预算不变 |
| `side_only` | 在融合点将 asm hidden 严格乘零 | 仍执行同一 backbone，宽度/参数表不缩小 |
| `llm_only` | 将 numeric/state/relation/uarch side 严格乘零 | 只测静态语义可预测性 |
| `register_rename` | 对通用显式寄存器做窗口一致的一一重命名 | 诊断语义保持稳定性，不改 rsp/rbp |

第一轮统一使用 frozen backbone/head-only，之后再对通过的 real/control 同时增加同 rank
LoRA。所有变体必须具有相同 base model、head width、batch、max steps、token budget、
macro sequence、train source 和 heldout source；gate 程序逐项比较这些 provenance，不匹配
直接 FAIL。`random_init` 指随机初始化**模型权重**，不是随机 token。

当前 manifest 的 `development_heldout` 没有 c1 条目，但同一批七个 business-heldout c1
trace 存在于 `seed0_inference`。单核语义 gate 因此固定选择
`validation_split=seed0_inference` 且 `workload_role=business_heldout`，禁止混入其中的
base workloads。tiny backbone 只验证实验合同，gate 必须返回 UNKNOWN；只有真实 Qwen
run 才能产生语义 PASS/FAIL。

首轮阈值为 heldout commit-time WAPE：real 相对 pseudo、mnemonic-shuffle、random-init
分别至少改善 5%，相对 side-only 至少改善 3%；consistent register rename 相对 real 的
退化不超过 2%。gate report v2 还要求所有变体的 validation trace 集及标签总量完全配对，
然后按 workload 聚合 additive error/target，使用固定 seed 做 10,000 次 paired cluster
bootstrap：四个 required control 的 real 改善 95% CI 下界必须大于 0，register rename
退化的 95% CI 上界必须不超过 2%。这些仍只是首轮停止门槛，正式结论还必须补
closed-loop ROI error 和长期 drift。
每个变体少于 200 个训练 step 或 32 个 heldout validation batch 时证据状态必须为
UNKNOWN，不能因短 smoke 的偶然 WAPE 排序返回 PASS/FAIL。validation sequence 必须先在
每条 trace 内固定 seed 打散，再按 trace round-robin，避免短 eval 全来自同一 source 或只
覆盖 ROI 开头；gate 至少要求全部 7 个 heldout workload cluster 出现，runner 默认 35 个
batch（每个 workload 5 个）。训练 minibatch shuffle 使用与模型初始化解耦的固定 seed，
模型构造后重置 dropout RNG；LoRA 与 timing head 使用独立 seed domain，确保不同 backbone
构造路径不会改变 trainable 初值。初始化及 train/validation order policy 都写入各变体
provenance。正式 run 还必须记录 tokenizer vocabulary/special-token 与 backbone config 的
SHA-256、base-model commit；语义 gate 禁止混入恢复 checkpoint 的变体，也禁止复用旧
run contract。

### 4.2 主线阶段：gated side adapter

语义 gate 通过后，把 macro numeric embedding 按 token_to_macro gather 到对应 token，
在若干 LLM block 之间做门控残差或 FiLM：

~~~text
D_token[t] = Gather(Z_num, token_to_macro[t])
H_l = LLMBlock_l(H_l)
H_l = H_l + sigmoid(g_l) * SideAdapter_l(D_token)
~~~

这样：

- input_ids 仍然 100% 来自原 tokenizer；
- 不需要修改词表或训练 UOP token embedding；
- LLM 的 attention/MLP 真正参与静态语义与动态 timing context 的联合建模；
- side gate 初始化为接近 0，可从 pretrained LLM 稳定起步；
- LoRA 仍能从 timing loss 接收梯度。

不建议把 projected numeric vectors 直接伪装成 tokenizer vocabulary IDs。若使用
inputs_embeds 插入 soft tokens，必须作为单独消融，并明确它已经不是 100% 原生 token 流。

### 4.3 跨核融合

不同核不能简单串成一个因果文本，也不能把各核的第 m 条 macro 当成时间对齐事件。
每核共享同一个 LLM 编码器，先 masked-pool 得到 core summary，随后用 1–2 层
permutation-equivariant set mixer，再把 core context 广播回该核的所有 macro：

~~~text
Z_core_macro = CrossCoreMixer(
    Z_macro,
    sample_ptr,
    relation_features,
    state_features
)
~~~

core ID 和 core-position embedding 均不进入模型。交换两个同构核的输入，输出也必须
相应交换；单元测试必须直接验证该性质。

## 5. 预测目标与损失

### 5.1 macro cursor 与标签

对 core 的第 m 个动态 macro，记录底层 UOP 半开区间：

~~~text
macro_uop_begin[m]
macro_uop_end[m]
T_macro_end[m] = commit_tick[macro_uop_end[m] - 1]
~~~

在 oracle sample time t：

~~~text
macro_cursor(t) = first m where T_macro_end[m] > t
target_tau[j] = T_macro_end[macro_cursor(t) + j] - t
~~~

因此即使该 macro 的部分 micro-op 在 t 前已经完成，它仍作为尚未退休的完整 macro 输入；
模型预测的是架构可见的 macro retirement boundary。

### 5.2 输出

输出保持 TCSim v29 的前缀结构，但契约版本明确为 macro：

~~~text
retirement_gap_macro       [R, 256]
commit_time_macro          [R, 256]       # FP64 cumsum，非递减
branch_miss_logit          [R, 256]
branch_miss_probability    [R, 256]
commit_logits              [R, 256, H]
commit_probability         [R, 256, H]
progress_macro             [R, H]
hard_prefix_macro          [R, 256, H]
~~~

主目标绝不是一个窗口 CPI 标量。逐 macro commit time 才能支持不同核在同一 delta 下消费
不同长度的前缀，也能保留 TCSim 的 prefix/progress/drift 监督。

### 5.3 损失

沿用 v29 的结构并把计数单位改为 macro：

- smooth-L1(log1p predicted/true macro commit time)；
- horizons 16/32/64/128/256/512/1024 cycles 的 prefix BCE；
- 每个 horizon 的 completed-macro progress loss；
- sequence length 4 的累计有符号 drift；
- branch macro 的 token BCE/Brier；
- horizon 内 branch-miss count loss。

所有权重先与 v29 相同，后续只允许在固定 development split 上调节。branch head 仍需与
gshare 单独比较，不能使用 branch_miss truth 驱动 timing 推理。

## 6. macro stride scheduler

每核维护 macro_cursor，而不是任意 UOP cursor：

~~~text
candidate_r = predicted time of the S_macro-th valid macro on core r
delta = min(candidate_r over active cores)
delta = min(delta, max_step_cycles)

consume_r = maximal macro prefix with commit_time_macro <= delta
macro_cursor_r += consume_r
uop_cursor_r = macro_uop_begin[macro_cursor_r]
global_time += delta
~~~

随后把被消费 macro 按预测绝对完成时间合并，macro 内保持原 UOP 顺序，更新共享
line/resource 状态和统计；时间相同的跨核事件使用置换不变的批量更新，不能用真实
commit tick 或 core ID 排序。scheduler 永不消费半条 macro。

这里要区分两类“状态”：packed functional trace 中的 dependency/reuse/branch-history 等
因果特征已经按功能流预计算，预测 cursor 前进后自然选择新的 causal prefix/window，不能
用 oracle timing 重新计算；在线 accumulator 只负责 predicted clock/last-commit、retired
macro/UOP、架构分支、访存和跨核 line-equality 统计，并以一个 global step 为批次更新，
不暴露 raw line ID。当前版本不是 cache-residency/coherence 状态模拟器；若未来把显式
cache state 加入模型，必须作为新 contract 并与 TCSim functional-only 输入单独消融。

K_macro 与 S_macro 必须分开：

- K_macro 固定为 256，是模型 lookahead；
- S_macro 是 scheduler 选点位置，要求 1 ≤ S_macro ≤ 256；
- S_macro 必须严格小于 K_macro，并满足 K_macro - S_macro 不小于数据中同 tick
  retirement group 的最大长度；
- 初版固定 S_macro=128；必须补做 S_macro ∈ {32, 64, 128} 和
  max_step_cycles 的敏感性实验；
- S_macro=256 只可作为失败边界诊断，不能作为正式主榜。真实 c4 int_alu 的最大同 tick
  macro 组为 8，oracle 在 S=256 时会把同组切到下一窗口并产生 0-cycle target。

exactly-once 同时检查 macro 和底层 UOP：每条 macro 恰好退休一次，其 UOP span 也恰好
消费一次，最终两个 cursor 都精确到流尾。

## 7. 新数据集构建

新契约建议命名为：

~~~text
global-time-v29-macro-native-2
functional-only-v29-macro-ragged-prefix
~~~

复用 packed3 的 raw functional arrays、resource decoder、uarch profile、global-time grid
和标签来源，但重新构建 macro view。任意 oracle time t 的 causal state 只由
T_macro_end <= t 的完整 macro 前缀重放，不能继承 packed3 可能停在 macro 中间的 UOP
cursor/state：

1. 按 macro_end 将每核 UOP 流压成 macro_uop_begin/end；
2. 取 macro_pc 对应 binary bytes，独立反汇编并生成真实 asm；
3. 生成 256-macro 的 native input_ids、attention mask 和 token-span mapping；
4. 将 per-UOP functional/dynamic fields 无损放入 ragged stream，以 uop_to_macro 做
   segment 映射，并生成固定 aggregate；
5. 以 macro-end commit tick 生成 commit/prefix/progress 标签；
6. 构造 macro cursor 的 training/validation contexts；
7. 自由推理只从预测 macro cursor 构造 context；
8. split 继续使用 seed0 base16 的 guarded time blocks、development heldout7、已使用的
   seed1 diagnosis 和 seed2+ sealed final。

必需工件：

| 工件 | 内容 |
|---|---|
| binary/static manifest | build-id/hash、module map、decoder/version、build flags |
| static instruction table | module-relative PC、bytes、真实 asm、function/basic-block/CFG |
| dynamic macro stream | static id、uop begin/end、macro fields、memory slots |
| native token cache | input_ids、mask、macro token spans、tokenizer/prompt schema |
| label stream | macro-end commit tick、branch miss truth，仅训练/评测可见 |
| context manifest | global time、各核 macro cursor、split、sequence linkage |

LoRA 训练可缓存 tokenization 和 span mapping，不能使用 detached hidden cache。LLM 冻结后，
可预计算完整功能 macro 流的 semantic hidden，或按重复 basic block 缓存；两种部署方式
必须分别报告存储、cache hit rate 和 UOP/s。

## 8. 当前实现需要改变什么

上一版 per-UOP 适配设计不再执行。正式实现需要：

1. 不再以 packed3 的 256-UOP slice 直接作为 LLM 窗口；
2. 不再把同一 macro embedding 复制给若干 UOP 后输出逐 UOP 时间；
3. 新建 macro cursor、macro-end target 和 macro scheduler；
4. 不再使用当前 Phase 1 的单标量 WAPE 作为目标；
5. 不使用 pseudo UOP tokens、custom op-class tokens 或全字段 JSON prompt；
6. 当前 build_static_dict 的 size=0、错误 bytes 长度和 PC 规范化问题必须先修；
7. DDP metrics 必须跨 rank 聚合；
8. real/pseudo/shuffle/random/side-only 必须保持相同 backbone、width、budget 和 macro split。

正式基线应同时报告：

- 原始 TCSim v29 256-UOP 模型的现有结果；
- 使用同一 macro 数据和 scheduler 的 structured macro Transformer；
- native-asm LLM late-fusion；
- native-asm LLM gated-side-adapter；
- all-text-native 消融。

这样可以区分收益来自“macro 单位改变”、来自“更多模型容量”，还是来自 Coding LLM 的
预训练汇编语义。

## 9. 验收条件

- 256 个 macro 都有完整真实 asm 和合法 token span；
- decoder join 100%，指令长度 1–15 bytes，无 size=0；
- 不截断指令或静默丢弃多 memory-access macro；
- input_ids 全部由指定原生 tokenizer 产生，无新增 custom vocabulary；
- label/input allowlist 通过，raw PC/address/resource ID 不可学习；
- macro commit_time 非递减，horizon progress 与 oracle macro count 一致；
- macro/UOP 双重 exactly-once；
- core permutation equivariance 通过；
- real asm 优于 pseudo/shuffle/random/side-only 的预注册语义 gate；
- closed-loop 与原始 TCSim v29、structured-macro baseline 同榜比较；
- 在线/预计算模式都报告 tokenizer 长度分布、显存、吞吐和缓存成本。

只有以上条件成立，才能声称 LLMSim 使用了 LLM 原生汇编语义，并正确迁移了 TCSim 的
全局时间前缀推理框架。

当前实现进度和首轮真实数据证据见
[实现审查与初步验证](macro_v29_implementation_audit_20260717.md)。
