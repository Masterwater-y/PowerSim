# 256-macro 原生 Token 方案：实现审查与初步验证

日期：2026-07-17。状态：数据契约、全 suite 结构 gate、独立训练入口、等容量语义消融
合同、真实 Qwen full-K 前后向、checkpoint 短程无标签 free rollout，以及按 workload
聚类的成对 bootstrap 门禁已完成；语义精度与 GPU/DDP 实机验证尚未完成。

> 2026-07-19 更新：第 2 点的 `S_macro < K_macro` 保守约束已被部署主线覆盖。
> 当前使用 `K_macro=S_macro=256`、无尾部 lookahead；跨窗口的同 tick group 允许下一窗口
> 以 0-cycle step 继续消费。参见主线设计第 16.8 节。

## 1. 审查结论

最新方案的主方向正确：

- LLM 输入单位必须是动态 macro，而不是 gem5 UOP 文本；
- K_macro=256；
- 输出必须是逐 macro 单调完成时间，而不是窗口 CPI 标量；
- 汇编 input_ids 使用原生 Coding-LLM tokenizer；
- timing 数值和跨核状态使用结构化 side channel；
- scheduler 只消费完整 macro，并映射回底层 UOP span。

但原方案在直接实现前有九个不完整或错误点，本轮已经修正：

1. **静态反汇编不能只做线性 objdump。** m5 magic bytes 会让 objdump 暂时失去指令边界，
   而 gem5 dynamic macro_pc 仍是合法 x86 起点。现在以所有 dynamic macro_pc 作为
   required decode seeds，对线性结果缺失的 PC 做 targeted decode，并用 ELF file bytes
   独立核验。
2. **S_macro 不能等于 K_macro。** 同一 tick 最多可退休多条 macro。S=256 会把同 tick
   group 切到窗口外，下一轮产生 0-cycle oracle target。现在要求 S<K，并保留 tie guard；
   初版 S_macro=128。
3. **不同核的第 m 条 macro 不能直接位置对齐。** 它们只是各核程序序号，不代表同一
   时间事件。初版先汇总每核 256-macro hidden，在核集合上做无 core-position code 的
   set attention，再把 core context 广播回该核的 macro。
4. **固定 U_max=8 是错误的。** 全 suite 扫描发现 `int_div_serial` 的 UOP/macro
   p99=45、p100=60。当前 schema v2 使用窗口内 ragged UOP stream + `uop_to_macro`
   segment pooling，无截断；batch 只按实际总 UOP 数 padding。
5. **架构分支不能由“macro 内任一 UOP 是 branch”判断。** `div` 的微码 macro 内有多条
   内部控制 UOP，一个 macro 可出现 7 个内部 miss marker。当前 `branch_mask` 来自静态
   x86 指令分类，内部 marker 不进入 branch label。
6. **训练样本必须按 guarded time block 连续取样。** 当前 loader 重新按 macro-end
   lookahead 做 eligibility，train/validation block hash 互斥，sequence length 4 不跨 block。
7. **单核 heldout 不能直接读 `development_heldout`。** 当前 manifest 的该 split 只有
   c4/c8/c16/c32；七个 c1 business-heldout trace 位于 `seed0_inference`。语义 gate 现在
   同时按 split 和 `workload_role=business_heldout` 过滤，避免空集合或混入 base workload。
8. **短 validation 不能按 source 顺序截前 N 个 batch。** 原顺序会让 32-batch gate 基本
   只看到第一个 workload。现在每条 trace 内先用固定 seed 打散，再在 trace 间
   round-robin，最后做无重复 rank-stride；gate 要求 7 个 workload cluster 都实际出现。
9. **单 GPU 训练 shuffle 不能依赖模型构造后的全局 RNG。** `random_init` 可能消耗不同数量
   的随机数并改变 minibatch 顺序。现在 train sampler 使用独立固定 seed，模型构造后也
   重新设置 dropout RNG；LoRA 和 timing head 还使用彼此隔离的 seed domain，避免 backbone
   构造改变 trainable 初值。初始化/order policy 都写入 provenance 并参与公平性比较。

此外，语义 run contract v3 记录 tokenizer vocabulary/special-token SHA-256、backbone config
SHA-256 和 Hugging Face model commit；gate 禁止混入 `--init-trainable` 恢复运行，旧 contract
也不能被 runner 静默复用。

## 2. 当前实现

| 能力 | 文件 | 当前状态 |
|---|---|---|
| 宽字节反汇编、targeted dynamic-PC decode、ELF bytes 校验 | data/build_static_dict.py | 已实现 |
| 全 trace/core dynamic-PC 聚合重建 | data/rebuild_v29_macro_static_suite.py | 已实现 |
| packed3 UOP -> 256 macro view、macro-end targets、原生 token spans | train/macro_v29_dataset.py | 已实现 |
| v29 dynamic8/summary38/relation22/state5/uarch29 复用 | train/macro_v29_dataset.py | 已实现 |
| label/control/model input 三路隔离、free context 无标签 | train/macro_v29_dataset.py | 已实现 |
| LLM span pooling + structured side + set core mixer | model/macro_v29_model.py | late-fusion 已实现 |
| 正 gap、FP64 cumsum、prefix/progress/branch heads | model/macro_v29_model.py | 已实现 |
| commit/prefix/progress/cumulative/branch token/count loss | model/macro_v29_model.py | 已实现 |
| macro stride、UOP boundary mapping、exact finish | eval/macro_v29_scheduler.py | 已实现 |
| 模型预测上下文与无标签 free-running rollout | eval/macro_v29_rollout.py | 已实现；短程真实数据已验证 |
| Qwen base hidden / frozen / QKV-O LoRA / DDP 训练入口 | train/train_macro_v29.py | full-K 真实权重 CPU 前后向已验证；GPU/DDP 未验证 |
| 真实 Qwen tail/full-K smoke | scripts/smoke_real_qwen_macro_v29.py | frozen、LoRA、optimizer、reload 已验证 |
| real/pseudo/mnemonic-shuffle/random-init/side/LLM-only/rename | train/macro_v29_dataset.py、model/macro_v29_model.py | 等容量合同已实现 |
| split/capacity-aware 语义 gate | eval/macro_v29_semantic_gate.py | v2 已实现；逐 trace 配对 + workload bootstrap；tiny 必须 UNKNOWN |
| 严格 checkpoint reload + free rollout | eval/rollout_macro_v29_checkpoint.py | tiny 训练后 checkpoint 已验证 |
| 一键语义 gate runner | scripts/run_macro_v29_semantic_gate.sh | 已实现；真实 GPU 尚未运行 |
| 真实数据一键 gate | scripts/validate_v29_macro_native.py | 已实现 |
| 单个真实 multi-core context 微型过拟合 gate | scripts/smoke_train_v29_macro.py | 已实现 |
| guarded block、sequence length 4 与累计漂移 | train/macro_v29_dataset.py | 已实现 |
| 全 suite structure/token/branch gate | scripts/audit_v29_macro_suite.py | 已实现 |
| 单元测试 | tests/test_macro_v29_*.py、test_static_dict_parser.py | 28/28 通过 |

## 3. 真实数据初步验证

验证对象：

~~~text
raw_v28_1_business_a2_sharedzipf_seed0_c04/W_v28_int_alu_dense
Qwen/Qwen2.5-Coder-1.5B-Instruct tokenizer
K_macro=256
S_macro=128
ragged UOP side（无固定 U_max）
max_tokens=4096
~~~

结果文件：

~~~text
logs/macro_v29/int_alu_c04_initial_contract.json
~~~

关键结果：

| 检查 | 结果 |
|---|---:|
| static unique-PC coverage | 64/64，100% |
| illegal/size=0 instructions | 0 |
| 每核 UOP | 921,633 |
| 每核 macro | 778,266 |
| mean UOP/macro | 1.1842 |
| max UOP/macro | 3（该切片；全 suite p100=60，由 ragged side 无损承载） |
| max same-tick macro group | 8，低于 tie guard 128 |
| 首个 256-macro token 数 | 2,147，低于 4,096 |
| multi-core input shapes | dynamic8/summary38/relation22/state5/uarch29 全部匹配 |
| free context label keys | 空 |
| tiny real-context forward | [4,256]、finite loss、monotonic |
| oracle scheduler | 6,069 steps |
| exactly consumed macro | 3,113,064 |
| exactly consumed UOP | 3,686,532 |
| no-progress / remaining | 0 / 0 |

静态 builder 修复前，同一 binary 有 size=0 行、最大长度仅 7，并缺失 dynamic PC
0x4020d2。修复后合法长度为 1–13；0x4020d2 通过 targeted decode 正确恢复为
movsxd rsi,DWORD PTR [rbx+0x18]。

另外使用同一个真实 c4 context 做了 60-step 微型过拟合：total loss 从 7.962 降至
1.529（ratio=0.192）。step 0 的 native backbone、numeric encoder、timing head 梯度范数
分别为 0.957、1.364、7.484，说明原生 token 语义路径、结构化数值路径和 timing head
都实际参与了优化，而不是死分支。该检查只证明最小链路可学习，不代表泛化精度。

随后对 23 个 workload、每个 workload 121 个 trace/core arrays 做了全量扫描：

| 全 suite 检查 | 结果 |
|---|---:|
| dynamic UOP / macro | 2,311,999,407 / 1,709,713,891 |
| static dynamic-PC coverage | 23/23 workload 100%，missing=0，invalid=0 |
| UOP/macro p50 / p90 / p99 / p100 | 1 / 2 / 4 / 60 |
| max same-tick macro group | 8，低于 tie guard 128 |
| 全部 256-macro 滑窗数 | 1,709,004,226 |
| 保守 native-token p50 / p90 / p99 / p100 | 2,319 / 2,431 / 2,486 / 2,626 |
| max branch-miss markers / architectural branch | 1 |
| 被排除的 `int_div` 微码内部 miss markers | 1,492,810 |

`int_div_serial` c4 的 ragged 反例 gate 同样通过：每核最大 60 UOP/macro，首个 batch 的
ragged side 为 `[4,1155,26]`，oracle exactly-once 消费 786,524 macro / 3,709,970 UOP。
其 60-step 微型过拟合 total loss 从 15.692 降至 0.712（ratio=0.045）。

在同一个真实 c4 `int_div_serial` 上，随机初始化 tiny 模型还完成了 8-step bounded
free rollout：累计消费 4,072 macro；四核 macro cursor 分别到达
1,020/1,017/1,017/1,018，对应 UOP cursor 4,746/4,837/4,772/4,773；全过程
`free_context_label_keys=[]`，每一步由预测完成时间选择 stride，且 UOP cursor 只落在完整
macro 边界。这个结果证明无 oracle 的执行机制成立，不代表预测时间准确。
同一 rollout 现在还显式累计 19,128 个被消费 UOP、356 个 memory-access UOP、336 个
架构分支和 15 个跨核 shared-line access；物理 line 只在内部 step-batch 状态中用于相等性，
报告与模型输入均不暴露 raw line ID。per-core UOP 累计与四个 UOP cursor 逐项一致。

guarded sequence 也在真实 c1 `int_div_serial` 上验证：train/validation eligible context
分别为 8,572/2,976，零重叠；4-step batch 的累计漂移 loss=0.231，不再是恒零占位。

独立训练入口在真实 c1 `int_div_serial` source 上通过 tiny CPU dry-run：train/validation
sequence 为 2,143/744，完成 1 个训练 step、1 个 validation batch、trainable-only checkpoint
保存；validation 有 1,024 个有效 macro。真实路径加载 bare `AutoModel` hidden states，避免
`AutoModelForCausalLM` 产生本任务不使用的全词表 logits；LoRA 目标为 Q/K/V/O projection。

七个语义变体随后在完全相同的 c1 source 合同上各完成一次 tiny dry-run。抽取一个 train
source 与一个 `seed0_inference/business_heldout` source 时，各变体的 train/validation
sequence 均为 3,967/3,968、trainable parameter 均为 4,964,103，gate provenance mismatch
为 0。`side_only` 对任意 assembly token 改写严格不变，`llm_only` 对任意 structured side
改写严格不变；mnemonic shuffle 保持词频和原位置 operands，pseudo 不含原地址。因为这些
run 使用 tiny backbone，`macro-v29-semantic-gate-v2` 正确返回 UNKNOWN，而不是把随机的
WAPE 差异误报为语义结论。
同一 gate 还要求每个真实 Qwen 变体至少 200 个训练 step 和 32 个 validation batch；即使
模型类是真实 Qwen，1-step smoke 也只能返回 UNKNOWN。

validation report 现在按 trace 输出 additive `absolute_error`、`target_magnitude` 和
`valid_macros`。gate v2 先要求七个变体的 trace 集、每条 trace 的目标总量和有效 macro 数
逐项一致，再按 workload 聚合并做固定 seed 的 10,000 次成对 cluster bootstrap。real 除了
达到点估计阈值，对四个 required control 的 95% CI 下界还必须大于 0；register rename
相对退化的 95% CI 上界必须不超过 2%。真实训练入口的新增 7-batch dry-run 按
seed-shuffle + round-robin 顺序恰好覆盖全部 7 个 c1 business-heldout trace，每条 1,024、
合计 7,168 个有效 macro；
per-trace 总量精确复原总体 WAPE。相同 seed 独立重跑的 train loss 与完整 validation JSON
逐值相同；real/side-only/LLM-only 的全部初始 parameter 也逐 tensor 相同。run contract v3
dry-run 还成功写出 64-hex tokenizer/config fingerprint；本地 Qwen config commit 为
`2e1fd397ee46e1388853d2af2c993145b0f1098a`。正式 runner
默认取 35 个 batch，使 7 个 workload 各取 5 个时间分散的 sequence。
单测同时覆盖“总体改善但 cluster CI 跨 0”、
“变体 trace 集不一致”和“短 eval 跨 trace 轮转”三个关键路径。

最后将 1-step tiny checkpoint 严格重载（85 个 trainable tensors）后，在真实 c4
`int_div_serial` 上运行 8-step free rollout：累计消费 4,072 macro，四核 macro cursor 为
1,018/1,016/1,020/1,018，对应 UOP cursor 为 4,743/4,836/4,776/4,773，标签键仍为空。
这补齐了“训练—保存—重载—模型预测—scheduler”工程闭环，但依然不证明精度。

由于当前进程看不到 CUDA，进一步使用本地完整
`Qwen/Qwen2.5-Coder-1.5B-Instruct` 权重在 CPU 上执行真实模型门禁。首先在同一个 c1
`int_div_serial` 的完整 K=256 窗口上验证 frozen backbone：实际类为 `Qwen2Model`，输入
2,102 个原生 token / 1,197 个 UOP，输出 `[1,256]` 单调，全部 loss finite，前向约
2.84 秒。随后使用 Q/K/V/O rank-8 LoRA 运行完整 timing loss 反向：实际类为
`PeftModelForFeatureExtraction`，backbone/numeric/timing 梯度范数分别为
0.690/5.575/11.100；完成一次 optimizer step 后保存并重载 306 个 trainable tensors，
重载前后 commit-time 最大误差为 0。该证据证明真实权重和 LoRA 工程路径正确，但 CPU
单样本 smoke 仍不能替代 GPU 吞吐、DDP 或 heldout 训练结果。

## 4. 按方案书逐项完成度

| 方案要求 | 判定 | 当前证据/缺口 |
|---|---|---|
| K=256 dynamic macro，而非 UOP 文本 | 已证明 | 全 suite macro view、真实 full-K Qwen smoke |
| 汇编 `input_ids` 只用原生 tokenizer | 已证明 | vocab 不变；动态顺序 token span；全 suite token gate |
| static decode/ELF bytes/dynamic-PC join | 已证明 | 23 workload、all core 100% coverage |
| microcoded macro 无损表示 | 已证明 | ragged segment；`int_div` p100=60 |
| label/control/input 隔离 | 已证明 | allowlist、free context、真实 free rollout label keys 为空 |
| Qwen hidden + late fusion | 已证明 | 真实 `Qwen2Model` K=256 前向与 finite full loss |
| Q/K/V/O LoRA timing gradient | 已证明 | 真实 full-K backward；backbone grad norm=0.690 |
| gated in-layer side adapter | 未实现 | 当前只有 late fusion；必须在语义 gate 后实现 |
| permutation-equivariant cross-core mixer | 已证明 | core permutation 单元测试 |
| macro commit/prefix/progress/branch/cumulative loss | 已证明 | shape/loss/seq4 drift tests；真实 full-K loss |
| macro/UOP exactly-once scheduler | 已证明 | oracle full trace + model-predicted unit full finish |
| 预测 cursor 构造无 oracle context | 已证明 | bounded real-trace rollout，无 label key |
| 消费后的 functional 状态/统计更新 | 部分完成 | cursor/time、UOP/memory/branch/shared-line step-batch 累计已实现；不是 cache-residency 模拟器 |
| guarded train/validation 与 c1 family heldout | 已证明 | block 零交叉；`seed0_inference/business_heldout` 精确过滤 |
| 等容量语义消融合同 | 已证明 | 七变体、provenance fairness、tiny 强制 UNKNOWN |
| 真实 Qwen heldout 语义增益 | 未验证 | paired bootstrap 代码/合同已就绪；尚无七变体真实训练结果 |
| GPU AMP/DDP | 部分完成 | all-reduce 与无重复 eval sampler 已实现/单测；CUDA 不可见，未取得实机证据 |
| structured-macro/TCSim/closed-loop 同榜 | 未完成 | 尚无统一榜单、完整预测 rollout 与长期 drift |
| native token/hidden cache 与部署吞吐 | 未完成 | 当前在线 tokenize/forward；无 cache hit/UOP/s 报告 |

因此“实现是否完整”的严格答案仍是 **否**；但数据契约、真实权重模型接口、训练梯度、
checkpoint 和短程执行闭环已由直接证据覆盖，不再只是设计或 mock。

## 5. 当前仍未完成

本轮 PASS 是 **contract + tiny-model + semantic-control + checkpoint/rollout PASS**，不是
模型精度 PASS。

以下内容仍缺失或尚未实机验证：

1. 真实 Qwen full-K CPU 前向、LoRA 反向、optimizer 和 checkpoint reload 已通过；尚未
   完成 GPU AMP、显存峰值、吞吐和 DDP 实机验证；
2. gated in-layer side adapter；当前只实现 late-fusion；
3. tiny 训练后 checkpoint 的短程 free rollout 已通过，但尚未用真实 Qwen checkpoint 做
   完整 trace rollout、长程漂移与预测精度评估；
4. 七个等容量消融、逐 trace 公平性校验和 workload-cluster paired bootstrap 已完成，
   但真实 Qwen 七变体结果和语义 PASS/FAIL 尚未产生；
5. DDP sampler/metric all-reduce 已编码但未做多 GPU 实机验证；heldout/seed2+ 榜单和
   TCSim 基线对比仍缺失；
6. 静态 embedding/cache 的部署吞吐。

因此当前不能声称“最新方案已完整实现”或“LLM timing 有效”。可以声称的是：新的
macro 数据契约、模型接口和 scheduler 在一个真实 c4 trace 上已经形成可运行且通过
结构验证的纵向切片。

## 6. 下一轮验证顺序

1. 获得 GPU 权限后复验 AMP、显存峰值、DDP 和 checkpoint/free rollout；
2. 运行七个等容量变体的 c1 business-heldout 真实 Qwen 语义 gate，并读取 workload-cluster
   paired bootstrap 结论；
3. 扩展到完整 c4/c8 closed-loop、长期 drift 与真实 Qwen checkpoint rollout；
4. 多 GPU DDP 实机核对 sampler、all-reduce 和恢复训练；
5. 最后扩大到 seed2+、多核 heldout 和 TCSim 基线。
