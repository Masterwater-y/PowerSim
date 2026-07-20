# LLM 迁移到领域回归任务：论文审计与 LLMSim 设计更新

状态：设计审计稿。日期：2026-07-14。

## 1. 问题与结论

本文逐篇审计近期讨论的八篇工作，回答四个问题：

1. 它们把 LLM 的什么语义先验迁移到了什么领域；
2. 它们怎样从 hidden state 或 decoder 得到连续数值；
3. 它们是否解决了 LLMSim 已暴露的痛点；
4. 哪些做法应进入主线，哪些只能作为公平对照。

**总判断**：把预训练 LLM 迁移为领域回归器已有充分先例，且已经有
`code -> latency / memory / performance counters` 的直接工作。它支持而非否定
LLMSim 的 LLM 语义主线。但是，没有一篇工作解决了以下联合问题：真实动态
x86 macro 流、不可见的 cache/predictor/DRAM/coherence 初态、多核相互干扰、
可加 per-core cycles 和闭环 rollout。因此，LLM 应承担可验证的代码语义表征；
动态数值状态和共享资源仍必须显式建模。

当前建议的首选结构不变，但应吸收这些工作中的三项改进：

```text
ELF PC -> true assembly/basic block -> Coding LLM + semantic auxiliary tasks
                                      -> cached static embedding E_static

E_static + dynamic numeric sidecar -> local dynamic encoder
per-core local embedding + shared-state graph -> cross-core module
                                          -> continuous multi-task heads
                                          -> cycles/PMU + state rollout
```

其中需新增一个严格的生成式对照：同样的真实汇编、动态输入、group split 和
token 预算下，以受约束科学计数法 decoder 生成 cycles/PMU；先用 CE，再可选
sequence-level 数值 reward 微调。不能把它直接替换为主线，原因是它增加了解码
延迟和闭环 parse 风险，也尚未在多核 timing 上被验证。

## 2. LLMSim 的待解痛点

| 编号 | 当前痛点 | 影响 |
|---|---|---|
| P1 | `v24` pseudo assembly 由 uop 字段渲染，不能恢复真实 mnemonic、寄存器、operand 和 basic block。 | 无法证明 Coding LLM 的预训练语义实际被激活。 |
| P2 | 地址、reuse、stride、依赖距离、uarch 参数被混入 token 时缺乏连续数值归纳偏置。 | 数值相邻性和单位关系难学，token 长度失控。 |
| P3 | 当前 v22 是共享长序列加尾部 query，per-core hidden 在高 core count 处容易趋同。 | 无法表示每核差异和共享资源争用。 |
| P4 | cache/predictor/DRAM/coherence 初态部分不可见。 | 同一可见 trace 可有不同 timing，端到端 LLM 不可能消除这种不可辨识性。 |
| P5 | window 重叠、`random_split` 和真实 `commit_tick` 边界导致评测泄漏/部署偏移。 | 所有模型比较都不可信。 |
| P6 | 连续 cycles 与 token CE 的几何不一致；但纯 MSE 也可能丢失多指标相关性和尾部。 | 需要连续主线与生成式对照共同验证。 |
| P7 | 若每个动态窗口重复跑大 LLM，MIPS 和显存不满足仿真器要求。 | 必须缓存静态语义或蒸馏。 |

下文用 P1--P7 判断论文的实际可迁移性。

## 3. 论文横向结论

| 工作 | LLM 语义如何进入 | 数值输出 | 对 P1--P7 的价值 | 采纳级别 |
|---|---|---|---|---|
| RLM for Code | 冻结 T5Gemma encoder 读取原始代码/ONNX 文本 | 科学计数法 token decoder、约束采样、聚合 | 直接证明 code-to-metric；启发生成式对照 | 必做对照 |
| Omniwise | LoRA 后的 LLaMA 读取 HIP 源码、GPU/编译配置 | JSON counters | 直接证明代码到硬件 counter；但只有静态 kernel | 数据和对照经验 |
| Time-LLM | 数值 patch 通过 reprogrammer 对齐到冻结 LLM | 投影层连续预测 | 证明数值可经 adapter 而非文本化 | 融合层设计参考 |
| TP-BERTa | feature-name 语义 + 专门数值 magnitude embedding | `[CLS]` + MSE head | 数值/名称解耦、序无关聚合 | numeric sidecar 参考 |
| TabuLa | 文本化 row、候选 label、row-causal attention | 分类/分桶回归 token | 数据清洗、mask、防止跨表泄漏 | 数据协议参考 |
| Nova | 真正 x86 汇编、instruction hierarchy、语义对比学习 | 下游生成/检索，不做回归 | 直接解决 P1 的静态语义训练 | 强烈采纳其思路 |
| ASMA-Tune | 独立 asm encoder -> projector -> LLM | 文本生成 | 证明“独立编码器再投影”可行 | 备选语义 adapter |
| GenRe2 | 编码器表征 + 数字 decoder | CE 后以序列级数值 reward 做 RL | 修正 token CE/数值误差错位 | 生成式对照的第二阶段 |

## 4. 逐篇审计

### 4.1 Regression Language Models for Code（RLM）

来源：[论文 HTML / arXiv](https://arxiv.org/html/2509.26476)。这是目前与本项目
最接近的一篇：从 Python/C++、Triton 和 ONNX 文本预测峰值内存、kernel latency、
网络 accuracy 和跨硬件 latency。

**如何利用语义**

- 输入就是原始代码或 ONNX 的文本表示，不依赖手工图统计；作者使用冻结的
  T5Gemma encoder，认为通用语言/代码预训练已经提供可迁移的组合结构先验。
- decoder 是可训练的；冻结 encoder 既降低训练成本，也把“预训练语义是否有用”
  与 decoder 容量分开。
- 多任务时按顺序生成多个指标，后一个指标条件于前一个指标，因此可以学习诸如
  `latency` 与 `accuracy` 的相关关系，而不是多个独立 MLP head。

**如何做回归**

- 目标数被编码为 sign、exponent、mantissa 的专用 token，例如科学计数法；这避免
  为不同数据集预先设定 min/max，也覆盖宽量级目标。
- decoder 用交叉熵训练，推理用 constrained decoding 保证输出合法数；可多次采样，
  在原数值空间取 mean 或 median，亦可形成预测分布。
- 文中一个 NAS 消融中，decoder head 的 Spearman 为 0.800，高于归一化 MSE head
  的 0.717 和未归一化 head 的 0.478。这个结果只说明该数据/架构下的优势，不能
  外推成“所有 timing 回归都应生成数字”。
- 其 ICL 实验也显示：把若干标注样本塞进旗舰模型 prompt 的效果和成本都不如
  in-weight training，尤其是长代码限制了可放入的示例数。

**对 LLMSim 的启示**

1. 建立 `RLM-CPU` 对照，而不是只比较 MLP head：输入应为真实 basic block/
   macro 汇编、离散 uarch 类别和少量动态 category；输出为固定长度、受约束的
   `log cycles-per-macro`、branch/cache PMU 数字序列。
2. 不将其当主线：RLM 预测的是静态程序/图的 aggregate metric，未输入或推进
   cacheline、跨核 overlap 与历史状态，因而不解决 P3/P4/P7。
3. 其多目标自回归可作为辅助：先生成较稳定的 workload/chunk 资源类别或
   log-cycles，再生成 cache/branch 指标；但须与并行连续多头做相同数据上的消融。
4. 论文的 frozen-encoder 结果支持先做 frozen Coding LLM linear/decoder probe。
   若 probe 不通过，不应因 LoRA 更大就直接宣称有语义收益。

### 4.2 Omniwise：GPU kernel 性能计数器预测

来源：[论文 HTML / arXiv](https://arxiv.org/html/2506.20886)。作者以 HIP kernel
源码及运行环境为输入，预测带宽、cache hit rate、GFLOPs、arithmetic intensity 等
GPU counters；论文报告 MI250/MI300X 测试集上超过 90% 的预测落在 10% 相对
误差内。该结论来自预印本作者实验，尚不应视为 CPU 多核 timing 的可转移精度。

**如何利用语义与数据**

- 使用 LLaMA 3.2 3B Instruct 的参数高效微调，提示含 kernel 源码、架构标识和
  compiler flags，输出为严格 JSON。公开描述中使用 LoRA `r=24`。
- 数据不是自然 workload trace：主要来自程序化合成 kernel、LLM 生成 kernel、
  手写 kernel，并在 MI250/MI300X 与不同编译 flag 下 profile。它利用同一代码在
  多配置上的组合扩增，也做变量名替换来抑制标识符捷径。
- label 会按对应硬件峰值归一化，输出的 JSON 计数器再反归一化；这种单位化让
  多计数器共享 decoder 更稳定。

**它解决与未解决什么**

- 解决 P1 的一部分：源代码模式、thread/block 配置和编译选项确实可以映射到
  hardware counter；也是“LLM 不只会解释代码”的直接证据。
- 部分启发 P6：多计数器联合 JSON 可利用指标相关性；对跨架构应显式给架构描述，
  而不是只给 `cfg_hash`。
- 不解决 P3/P4：一个 GPU kernel 的执行模型、输入规模和硬件状态远小于多进程
  CPU 的异步共享 cache/coherence 闭环；其合成代码分布与真实 CPU workload 差异
  很大。

**对 LLMSim 的可执行改动**

- 学习其“同一静态代码 x 编译选项 x uarch”的数据笛卡尔扩增，但保留真实功能
  trace，不能用合成微基准取代应用行为。
- 把 `compiler flags`、ISA extension、core count、cache/DRAM 参数拆成可审计的
  typed uarch profile；训练/测试必须按 binary build 和 profile 分组，禁止把同一
  代码不同 flag 的近重复样本随机分到两边。
- JSON 输出只做 RLM-CPU 的对照。它必须报告 parse rate、每个字段的误差、decode
  latency 和 rollout drift，不能只报告“10% 内比例”。

### 4.3 Time-LLM：冻结 LLM 的时间序列预测

来源：[ICLR 2024 论文](https://arxiv.org/abs/2310.01728)。该工作不是代码语义
模型，但对“连续动态状态是否必须被文本化”给出了重要反例。

**模型机制**

- 将时间序列切为 patch；reprogramming layer 通过 cross-attention 将 patch
  embedding 映射到冻结 LLM 的 token embedding 空间，并使用文本 prototype
  作为可学习的语言锚点。
- Prompt-as-Prefix 提供任务与序列统计信息；LLM backbone 保持冻结；LLM 输出
  经 projection layer 得到连续 forecast，而不是让 LM head 拼出小数。

**对 LLMSim 的启示**

- P2 的正确处理是“结构化向量 -> 可学习 projector/adapter”，不是把每个地址、
  计数和 reuse distance 转成 BPE 字符串。numeric adapter 可使用标准化标量、
  log bucket、缺失掩码和单位 embedding。
- 但不要把整个动态 trace patch 送进 LLM 作为主线。时间序列没有静态代码重复的
  缓存机会；LLMSim 则有大量重复 loop body，最省算力的分工是 LLM 缓存静态语义、
  小型 local encoder 消化动态 sidecar。
- 增加一个明确的对照 `Dynamic-Reprogramming`：dynamic numeric chunks 经
  projector 接入 frozen Coding LLM，再接连续 head。若它显著优于“静态 LLM +
  数值 encoder”且 MIPS 可接受，再考虑纳入主线。

### 4.4 TP-BERTa：表格回归中的数值与名称解耦

来源：[论文 HTML / arXiv](https://arxiv.org/html/2403.01841)。它基于 RoBERTa，
在 101 个回归和 101 个分类数据集上预训练，再迁移到 145 个下游表格任务。

**模型机制**

- 数值先按每个 feature 的相对量级分桶，再以新增 magnitude token 表示；相邻
  bin 通过 triplet regularizer 拉近、远 bin 推开。数值 token 与 feature name
  token 分离。
- Intra-Feature Attention 先在单个 `name-value` 对内融合，再把每个 feature
  vector 送入共享 RoBERTa，因此避免全局 attention 错配字段和值，并做到 feature
  permutation invariant。
- 用 `[CLS]` hidden 接 dataset-specific head；回归用 MSE，预训练中的 magnitude
  regularizer 只帮助数值 embedding 形成有序几何。

**对 LLMSim 的启示与限制**

- 直接采纳“字段名/单位/值分开表示、先做字段内融合、数值编码具备相邻性”的
  原则。对于 `reuse_distance`、`stride`、`MSHR`、`LLC_size` 等字段，使用
  train-only quantile/log bucket embedding 加原始标准化标量，比纯字符串更稳。
- 不直接采用其 C4.5 target-aware binning：分桶边界由 label 指导，在我们的严格
  group/OOD 设定中必须只由训练分组拟合；更安全的是物理含义明确的 log bucket
  或 train split quantile。
- 它没有时序因果、共享状态或闭环，因此只解决 P2 的输入表示，不解决 P3--P5。

### 4.5 TabuLa：大规模表格 LLM 与数据协议

来源：[论文 HTML / arXiv](https://arxiv.org/html/2406.12031)。作者从 4M 张表、
2.1B 行构建 T4，完整微调 Llama 3-8B 做 classification 和 binned regression。

**模型和数据经验**

- 每行被序列化为 `key:value` 文本，prompt 指明目标列并列出候选 label；连续
  target 被离散为 bins，训练仍是 next-token prediction。
- 设计 row-causal tabular mask：同表前序行可见、其他表不可见，同时支持 sample
  packing 和 few-shot 行上下文。这是比普通 causal mask 更贴合数据结构的注意力
  约束。
- 论文最有价值处不是“8B 全量微调”，而是 table/row/column 三级清洗、去重、缺失
  值处理、语义字段筛选和专门的 contamination 检查。

**对 LLMSim 的启示**

- 将 `run_id` 视为 table：一个 run 的所有重叠窗口、同 binary 的相近种子和同一
  uarch profile 的派生样本必须按预先定义的 group protocol 处理，不能 `random_split`。
- 将静态字典、动态 sidecar 和 labels 分文件保存，并在 loader 中 join；prompt 中
  禁止含 `run_id`、绝对 PC、workload 名、真实 tick 或 cfg hash 等身份捷径。
- 如果研究 in-context adaptation，只允许该 run 的历史功能状态，不允许同 run 的
  timing label 或未来窗口。采用类似 block-causal mask 可实现这一限制。
- 它的 full fine-tune 依赖 8B token 量级任务数据；当前 CPU trace 数据远小于该量，
  不能以其为理由直接做全量微调。

### 4.6 Nova：真实汇编语义的层级表征

来源：[论文 HTML / arXiv](https://arxiv.org/html/2311.13721)。Nova 从 C 函数以
O0--O3 编译、strip、objdump 得到真实 x86-64 汇编，训练目标是反编译和二进制
相似性，而非 timing 回归。

**如何获得汇编语义**

- 每条 instruction 末尾引入 `[INST]` summary token；层级 attention 分为
  intra-instruction、preceding-instruction、inter-instruction 三种连接。这样既
  表示 `mnemonic/operand` 局部语义，也显式组织跨指令依赖。
- 功能对比学习把同一源函数的 source/assembly 表征拉近；优化对比学习组织 O0
  到 O3 的表征关系。最终函数 embedding 是 `[INST]` hidden 的聚合。
- 一半 attention heads 保留标准全注意力，另一半施加层级 mask，避免结构约束把
  原有语言模型能力全部覆盖。

**对 LLMSim 的直接改动**

1. P1 的数据前置条件应改为：从 ELF 的 `macro_pc` 恢复 bytes 和 objdump/Capstone
   反汇编，建立 `static_instruction_id -> {asm, basic_block, CFG edges}` 字典；
   验收为随机 1000 条与二进制反汇编逐条一致，而非“uop renderer 看起来像汇编”。
2. 对每个真实 instruction/basic block 缓存 embedding；pooling 位置选择 instruction
   end 或 block end，而不是当前统一尾部 query。
3. 引入**只使用静态工件**的语义辅助任务：相同二进制不同地址重定位、寄存器
   rename、同源不同优化级、def-use/branch-target/基本块邻接。正样本构造不能看
   timing label；负样本要按 opcode 频率匹配，避免只学词频。
4. 不必重写 Qwen 的 attention mask 作为第一实现。先用 native tokenizer + 显式
   instruction boundary token + hierarchical pooling，只有语义 gate 通过后再比较
   Nova 式半头层级 mask。

Nova 没有多核、地址动态性和 cycle 标签，所以它只解决 P1，并为 P7 的静态缓存
提供了具体实现方式。

### 4.7 ASMA-Tune：独立汇编编码器再投影给 LLM

来源：[论文 HTML / arXiv](https://arxiv.org/html/2503.11617)。它采用三段式结构：
110M 的 CLAP-ASM encoder 提取汇编结构特征，30M projector 映射到 13B LLM 的
词向量空间，LLM 输出汇编问答/解释；先预训练 projector，再联合微调 projector 和
LLM。

**可迁移经验**

- 它是“语义模块与 LLM backbone 分开”的直接先例：外部结构 encoder 不必被硬
  序列化为普通文本，projector 可以让其进入 LLM 的表示空间。
- 两阶段训练避免随机 projector 与大 LLM 同时学习造成不稳定；这和我们先验证
  static semantic probe、再做 timing SFT 的顺序一致。
- 它在预处理时保留 jump 相对地址，说明控制流关系应保留；但该地址不是动态
  effective address，更不能被误当 cache locality。

**不应直接照搬的部分**

- ASMA 的监督是 GPT 生成的描述、问答和 reasoning，目标是语义理解，不能把
  生成文本质量当作 timing 正确性。
- 用 CLAP-ASM + 13B decoder 会重复一个并行的汇编 encoder；LLMSim 第一阶段
  应先评估一个 Coding LLM native encoder 是否足够。只有它在真实汇编语义 gate
  上失败时，才增加独立 asm encoder/projector。

### 4.8 GenRe2：用 RL 对齐生成式回归的数值误差

来源：[论文 HTML / arXiv](https://arxiv.org/html/2512.06533)。这是对 RLM 的关键
补充：作者在 tabular 和 code-metric 回归中比较 pointwise head、histogram/Riemann
head、数字 CE decoder 和 RL 后的 decoder。

**机制**

- 先以 CE 训练 base-B 或科学计数法 token decoder；推理从多个候选数字取 mean/
  median。
- 将“生成完整数字”视作一条 RL trajectory，在 detokenize 后以原数值空间的
  negative MSE 作为终止 reward；用 ReMax 或 GRPO 从 CE checkpoint 做 policy
  optimization。
- 这是 token 级 CE 看不到全数字量级的直接修正：例如同样错一个 token 时，101
  和 200 相对真值 100 的误差不应相同。

**对 LLMSim 的判断**

- 它推翻了“生成式数值一定不如 MLP”的绝对说法，因此 RLM-CPU 不能只做 CE
  后草率否定；应把 CE 和 sequence-reward 两阶段作为完整生成式基线。
- 它也不改变主线选择：每个输出仍需多次 sample/decoder rollout，cost 高于一个
  continuous head；多核状态推进需要稳定、低延迟的每核输出，RL 还会增加训练
  方差和 reward 设计风险。
- 若进入实验，reward 必须直接对应仿真目标，而不是单字段 MSE：

```text
r = - [ w_core * err(log cycles_core)
       + w_window * err(log sum cycles)
       + w_spread * err(relative per-core cycles)
       + w_aux * err(PMU) ]
```

  reward 与所有归一化常数只能在训练 group 拟合；验证和 rollout 选择另设数据。
  不能在 P5 的泄漏数据上先训练 RL，因为它会更有效地记住捷径。

## 5. 对目标架构的更新

### 5.1 保留双通道，但把语义接口改为 instruction/block embedding

原先“LLM backbone 后接 side feature + head”的 v22 结构应只保留为 baseline。
建议接口如下：

```text
static dictionary:
  true x86 asm/basic block -- Coding LLM(+LoRA) --> E_static[id, d]

dynamic stream for core c:
  static_instruction_id -> lookup(E_static)
  branch/address/reuse/stride/dependency/size -> NumericEncoder
  E_static + E_dynamic + history snapshot -> LocalTraceEncoder -> z_c

system:
  {z_c}, same-line/sync/resource edges, SharedStateFeatureEngine -> GNN/SetTransformer
  -> h_c

heads:
  default: continuous cycles/PMU distribution heads
  control: constrained numeric decoder (RLM-CPU)
```

这一结构采纳 Nova 的 instruction-level summary、Time-LLM/TP-BERTa 的数值
adapter、ASMA 的 projector 可选接口，同时避免把 cache/coherence 的动态状态伪装
为“LLM 已懂”。

### 5.2 新增语义 warm-up，不用自然语言解释替代 timing 标签

在 timing SFT 之前，允许对 static dictionary 做有限的 continued pretraining/
LoRA warm-up：

- masked operand/register/branch target；
- instruction 与 basic-block 边界识别；
- def-use pair、CFG adjacency；
- same-function cross-optimization contrastive；
- semantics-preserving register rename consistency。

验收不是生成解释，而是：真实汇编相对 pseudo assembly 和 frequency-matched
mnemonic shuffle，在 frozen probe 上表现不同；rename 不应大幅改变表征，而删除
真实 dependency/branch/operand 应显著改变表征。

### 5.3 数值输入的具体规范

1. 连续值保留标准化实数通道，另加单位/type embedding、`is_missing` 和 log bucket。
2. 仅对小离散集合 token 化：opcode class、load/store/atomic、branch class、
   access size bucket、uarch category、core/thread identity。
3. 需要 token 化数值时，bin edge 只由 train groups 拟合，或使用物理预定义
   buckets；禁止 target-aware 全数据分桶。
4. `uarch profile` 走两条路：canonical text prefix 给 LLM，normalized vector 通过
   FiLM/cross-attention 给 numeric/local/cross-core module。

### 5.4 输出头的决策

| 路线 | 用途 | 训练和推理 | 是否主线 |
|---|---|---|---|
| C1 continuous multi-head | 精确 cycles/PMU、低延迟闭环 | Huber/log-cosh + additive/window/rollout loss | 是 |
| C2 distribution head | 尾部与不可辨识状态 | Student-t/Gaussian NLL，输出 mean/scale | C1 稳定后 |
| G1 RLM decoder | 公平检验“生成数值是否更好” | CE + constrained scientific-number decode + sample aggregate | 是，对照 |
| G2 GenRe2 | 检验 sequence reward 是否值得解码成本 | G1 最佳 checkpoint 后 RL | 仅 G1 有收益后 |
| JSON explanation | 瓶颈类别、审计信息 | 小权重辅助任务 | 非 timing 主通道 |

### 5.5 LoRA 计划

这些论文没有给出可直接照搬的 `r` 或 target module：Omniwise 的 `r=24` 是其
3B JSON profiler 的实现选择；TabuLa 的结果依赖 8B 全量微调和 8B tokens 数据；
ASMA 是 projector + LLM 联训。因此 LLMSim 应按以下实验而非信仰确定 LoRA：

1. frozen Coding LLM + trainable pooling/numeric/cross-core/head；
2. attention `q/k/v/o` LoRA；
3. all-linear LoRA（再加 MLP `gate/up/down`）；
4. 仅在前三者 train/group-val 都欠拟合且 semantic gate 有效时，解冻最后 N 层。

新结构 token 行、numeric encoder、pooling、cross-core module 和连续 head 保持
全量训练。rank 做 16/32/64 曲线；比较时固定 unique basic-block 数、训练 token、
wall-clock 和 group split。

## 6. 数据与评测的优先级高于模型改动

文献中最值得采纳的不是某一层 Transformer，而是“先使输入和 split 可审计”。
在任何 RLM/LoRA 改动前，必须完成：

1. **真实静态字典**：ELF PC 到反汇编、basic block、binary hash 的映射；动态
   trace 只引用 `static_instruction_id`。
2. **固定功能 chunk**：按退休 macro 数定义窗口，训练可重叠但 cluster 不跨 split；
   eval 使用非重叠 chunk。
3. **可加标签**：从累计 retirement time 定义 `cycles[b,e)`；不允许使用该窗口的
   `commit_tick` 决定输入边界。
4. **分组切分**：window 化前按 immutable run id 切分。run id 至少含程序 family、
   binary hash、compiler flags、input/data seed、schedule seed、uarch profile hash、
   core/thread/pinning、warm policy、simulator commit。
5. **三类 OOD**：held-out workload family、held-out uarch profile、joint OOD；另外
   单列 cold/warm/carried-state 和 closed-loop rollout。
6. **捷径 probe**：只给 metadata、只给 PC、只给 side feature、只给 asm、打乱
   mnemonic/operand 的对照；任一路径异常高分都阻断模型规模扩张。

## 7. 推荐实验顺序与停止条件

### Phase A：数据和静态语义 gate

- 完成真汇编静态字典和 group split。
- 在单核、固定 uarch、固定 warm track 上训练 `side-only`、`real-asm frozen probe`、
  `pseudo-asm`、`mnemonic shuffle`、`random-init`。
- 只有真实汇编 + pretrained Coding LLM 相对 pseudo/shuffle/scratch 有显著收益，
  才进入 LoRA 和跨核阶段。

### Phase B：回归头对照

- 固定同一 input 和 split 比较 C1、G1；G1 报告 parse rate、MAE/WAPE、rank、
  sample count、decode latency、显存和 MIPS。
- 只有 G1 在 Family-OOD/Joint-OOD 和实时性上不劣，才加 G2。G2 先只优化单核
  log cycles；多目标 reward 及 rollout reward 必须逐项加入并消融。

### Phase C：动态和多核

- 加 LocalTraceEncoder，比较“numeric sidecar 融合”与 Time-LLM 式
  Dynamic-Reprogramming。
- 加 sparse cross-core module/显式 SharedStateFeatureEngine，比较 local-only、
  graph、deterministic shared-system hybrid。
- 最终门槛同时包括 per-core error、window additive error、spread/rank、tail、
  rollout drift 与 MIPS；aggregate CPI 单指标不足以发布结论。

## 8. 最终决策

1. **应做 LLM 领域回归**：已有 RLM/Omniwise/Time-LLM/TP-BERTa 等工作支撑，
   当前方向具备研究与工程依据。
2. **不应把它简化为纯文本端到端**：文献也反复为数值 tokenization、结构 adapter、
   特殊 mask、数值 reward 和大量清洗数据付出额外设计；这恰好说明通用 LLM 不会
   自动获得 timing 数值与共享状态归纳偏置。
3. **LLM 最有希望解决 P1**：真实汇编、依赖、控制流、基本块语义；P2 需要
   numeric adapter，P3/P4 需要动态与共享状态模块，P5 是数据工程问题，P7 依赖
   static cache/蒸馏。
4. **主线保持 continuous hybrid**，同时把 RLM-CPU/GenRe2 建成高质量对照；若
   它在严格 OOD、rollout 和 MIPS 上全面胜出，再调整主线，而不是预设答案。

## 9. 参考文献与阅读范围

- [Regression Language Models for Code, arXiv:2509.26476](https://arxiv.org/html/2509.26476)
- [Omniwise: Predicting GPU Kernels Performance with LLMs, arXiv:2506.20886](https://arxiv.org/html/2506.20886)
- [Time-LLM, ICLR 2024](https://arxiv.org/abs/2310.01728)
- [TP-BERTa, arXiv:2403.01841](https://arxiv.org/html/2403.01841)
- [TabuLa-8B, arXiv:2406.12031](https://arxiv.org/html/2406.12031)
- [Nova, arXiv:2311.13721](https://arxiv.org/html/2311.13721)
- [ASMA-Tune, arXiv:2503.11617](https://arxiv.org/html/2503.11617)
- [GenRe2, arXiv:2512.06533](https://arxiv.org/html/2512.06533)

除 Omniwise 外，以上工作均以论文 HTML 逐节核对方法和实验设置。Omniwise 的
arXiv HTML 在本次检索中只稳定提供部分段落；其训练超参数、约 95.5 万 kernel
数据规模和 JSON schema 来自论文可解析副本/公开描述，进入复现计划前应以作者
代码或 PDF 为准。所有文中报告的指标均为作者实验结果，不应直接与 LLMSim
数据集横向比较。
