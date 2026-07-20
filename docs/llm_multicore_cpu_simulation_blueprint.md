# 基于 Coding LLM 的多核 CPU 性能仿真：完整技术方案与可行性审计

状态：历史设计与可行性审计。日期：2026-07-10。

> 预测目标、切窗和闭环推理部分已被
> [LLMSim × TCSim v29：256-macro 原生 Token 方案](llmsim_tcsim_v29_macro_native_token_design.md)
> 取代。本文仅保留背景、问题拆解与早期备选路线。

本文回答以下问题：

- 如何把 functional trace 变成真正能激活 Coding LLM 代码语义的输入；
- 数据集、窗口、标签和切分怎样设计才不泄漏；
- 应该使用 LM head 生成 JSON，还是回归头预测 CPI/cycles；
- LoRA、全量微调、SFT、RL、RAG 各自在什么位置有价值；
- 多核共享 cache、DRAM、coherence 和闭环时间推进怎样建模；
- 如何用严格消融证明收益确实来自 LLM 预训练语义；
- 当前仓库哪些资产可复用，哪些原型不能直接进入正式实验。

## 0. 结论先行

### 0.1 推荐的终局不是“LLM 直接生成 CPI JSON”

推荐架构是一个混合式、分层的 surrogate simulator：

    真实动态 macro 汇编
      -> Coding LLM 静态/局部语义编码器
      -> 每核动态 trace encoder
      -> 稀疏跨核共享资源模块
      -> 连续数值回归头
      -> per-core cycles/CPI、PMU、置信区间
      -> 预测驱动的闭环时间推进

其中：

- 汇编 mnemonic、真实寄存器、operand 形状走 Coding LLM 原生 tokenizer；
- 地址、reuse distance、stride、时间、微架构参数走连续或离散结构化 side channel；
- 大模型用于理解真实指令和基本块，不负责把浮点数逐 token 拼出来；
- shared cache、DRAM、coherence 最好保留显式状态机或稀疏 interaction model；
- 第一版使用监督回归和 LoRA，不使用 RL；
- RAG 只在离线微架构知识抽取和校验层使用，不进入每个 window 的热路径。

### 0.2 对几个关键选择的直接答案

| 问题 | 推荐答案 |
|---|---|
| 输入是否全部使用 LLM 原生 token | 真实汇编部分是；连续数值和动态状态不是。强行把所有数值变成 BPE 文本会损失数值归纳偏置 |
| LM head 还是 MLP head | 主线使用 regression head；LM/JSON 只作为同 backbone 的对照实验 |
| 预测 CPI 还是 cycles | 主标签保存可加的 per-core cycles；固定 macro/uop quantum 下回归 log cycles-per-unit，并派生 macro CPI 和 uop CPI |
| Base 还是 Instruct | 回归表示主线优先 Base；Instruct 用于 zero-shot、结构化抽取和 JSON 对照 |
| LoRA 还是全量微调 | 先做 LoRA，attention 与 MLP 都覆盖；再做最后若干层解冻消融。数据证明容量不足前不做全量微调 |
| SFT 还是 RL | 主线是监督回归；生成式对照用 completion-only SFT。RL 只有在强 SFT 后仍存在明确、不可微的闭环目标错配时才考虑 |
| 是否加 RAG | 单一已知 gem5 配置下基本无收益；跨微架构时用于把文档/RTL 抽取成经 verifier 校验的 typed profile |
| 是否直接上大模型 | 先用 1.5B/3B 做严格归因，7B 做 scaling upper bound；30B/80B 更适合作为离线 teacher，缓存基本块 embedding 后蒸馏 |

### 0.3 可行性判断

- 在固定 simulator、固定 OS/绑核策略、已知 warm-state 规则的闭世界中，训练高精度 surrogate 是可行的。
- “有限 functional window 到精确多核 CPI”在一般情形下不是确定函数。cache/predictor 初态、跨核真实重叠、memory-controller/coherence 状态缺失时，同一可见输入可以对应多个 cycles。
- Coding LLM 最可能改善的是指令语义、依赖、控制流和基本块表征；它不会凭空补出动态 cache、DRAM 和 coherence 状态。
- 当前项目已经证明 aggregate CPI 可以看起来不错，而 per-core spread、尾部和 c32 非线性扩展仍严重失真。因此下一版的核心必须同时修复数据可辨识性、每核表征和 shared-system 建模，不能只换 tokenizer。

### 0.4 相关工作给出的现实上限

- [SimNet](https://arxiv.org/abs/2105.05821)用真实静态指令属性和最多约
  110 条历史指令的动态状态预测 instruction latency，再累计程序周期；其最佳
  单线程模型在 SPEC CPU2017 上报告约 5.6% 平均 CPI error。它明确未解决
  multithread/multiprogram。
- [TAO](https://arxiv.org/abs/2404.10921)最接近本项目：用可复用 functional
  trace、真实 opcode/register/PC/address/branch history/access distance 和
  self-attention 多任务预测 cycles、branch/cache/TLB events，单核平均 CPI
  error 约 5.23%。它说明 functional input 可行，也说明“只给汇编文本”不够。
- [Meta LLM Compiler](https://arxiv.org/abs/2407.02524)在大量 LLVM IR 和
  assembly token 上继续预训练后才获得编译器语义；普通 code model 对 binary
  size 数值预测相关性很弱，专训模型的数值 MAPE 仍不等于 cycle-accurate。
- 汇编专用模型如 [PalmTree](https://arxiv.org/abs/2103.03809) 和
  [BinBERT](https://arxiv.org/abs/2208.06692)都额外使用 control-flow、
  def-use 或 symbolic-execution 信号。这支持真实结构和动态 sidecar，而不是
  假设 native token 自动等于程序语义。

这些结果证明 learned surrogate 有研究基础，但没有一项直接证明 32-core
shared LLC/coherence/DRAM 可以由 monolithic LLM prompt 高精度解决。多核部分
仍是本项目的主要研究风险。

## 1. 当前仓库的证据与问题定位

### 1.1 当前最好结果说明了什么

当前最可信、未混入 c32 eval raw 的完整 deployment baseline 仍是 v16：
c04/c08/c16 mean CPI error 分别为 5.61%/6.33%/10.85%，见
[v16 结果](eval_v16_tail_local_step5000_results_20260702.md)。v22 是更新的
collapse 诊断，但其 c32 train/eval 同源，不能当作严格 OOD 结果。
这里的“最可信”仍只是同一批 program family 的跨 seed/run 评估，不是
Family-OOD；新方案必须在严格 split 下重训所有 baseline。

现有 v22 fixed checkpoint 在 17 个 workload 上的 pred-vs-label 聚合误差为：

| core count | 聚合平均误差 | 全局 core-window mean APE |
|---:|---:|---:|
| 4 | 7.31% | 14.06% |
| 8 | 6.64% | 16.52% |
| 16 | 8.21% | 44.63% |
| 32 | 18.23% | 156.29% |

来源见 [v22 验证快照](v22_fixed_inference_validation_c04_c08_c16_c32_20260706.md)。
更关键的是：

- hidden pair cosine 约为 0.98–0.99；
- label spread 增大时，pred spread 只捕获约 28–31%；
- c32 的 chase_dram、phased_mix、ads_ranking_proxy、graph_recall_proxy 出现 45–64% 聚合误差；
- 误差集中在内存压力非线性放大和慢核/快核区分。

这支持一个明确判断：当前模型不是完全没有信号，而是主要学到了 workload/窗口均值和低阶统计，对真实每核状态及高核数 shared-resource amplification 建模不足。

另外两个证据也反对“直接增大参数量”：

- [已有 Qwen3-4B 结果](eval_v12_summary_qwen3_4b_step7500_results_20260630.md)
  没有优于 0.6B，推理反而慢约 3.7–4.2 倍；
- ads-ranking 的 side-feature-only 线性 probe 已达到约 0.825 residual R²，
  与 hidden probe 很接近，说明现有提升可能主要来自手工 side channel，
  而非 LLM 预训练语义。

### 1.2 当前 native_macro 不是“真实汇编”

现有 raw records 有 macro_pc、op_class、n_src/n_dst、producer_dists 等字段，但没有真实 mnemonic、instruction bytes、operand 和架构寄存器。

当前 model/tokenizer.py 的 native renderer：

- 把 op_class 人工映射到 add、mov、imul 等词；
- 从 reg bucket 模运算伪造 rax/rbx 等寄存器；
- 每个 uop 渲染一行，却称为 macro；
- 无法恢复真实立即数、addressing mode、REP、vector width 和 exact dependency。

本地审计给出了一个直接反例：

- raw trace 中 macro_pc 4204335；
- 对应 ELF 地址 0x40272f；
- objdump 得到真实指令 rep stos QWORD PTR es:[rdi],rax；
- 当前 renderer 会把其多个 uop 渲染成若干合成 mov/add/jne。

所以 v24 原型最多叫 native-BPE pseudo assembly，不能用来证明 Coding LLM 利用了真实代码语义。

### 1.3 当前数据存在正式评测不可接受的泄漏

第一类是 split 泄漏：

- train/train_lora.py 对所有窗口直接 random_split；
- 同一 run 的高度重叠窗口会同时进入 train 和 val；
- 相邻窗口共享动态指令、地址、phase 和标签状态。

第二类是边界泄漏：

- 默认 TQ builder 使用 commit_tick 真值选择 T_start/T_end；
- 每核 core_split、uop 数和 fill ratio 因真实吞吐而变化；
- 输入长度本身因此携带 CPI 信息；
- 部署时边界又由预测时间决定，形成 teacher-forcing 分布偏移。

第三类是身份捷径：

- workload、cfg_hash、绝对 PC、run seed 等字段若进入 prompt，会让模型记住程序；
- 当前 c32 数据中还有 seed-B eval pool 被并入训练增强的版本，不能再称严格 core-count OOD。

因此，随机 val 只可作为 NaN/收敛 smoke test，不能作为任何泛化结论。

### 1.4 当前 cycles 标签不严格可加

aggregate_pmu 目前用窗口内部 max(commit_tick)-min(commit_tick) 作为 cycles。这遗漏了第一条退休指令之前的间隔，相邻窗口也不能严格重建全程。

建议在 macro boundary 上存累计退休时间 T(k)，并定义半开区间：

    cycles[b,e) = (T(e) - T(b)) / tick_per_cycle

第一窗的 T(b) 使用 ROI boundary tick。这样相邻不重叠窗口的 cycles 可以精确求和，数据 gate 也能验证它们重建 ROI。

### 1.5 当前数据量大，但有效独立样本少

仓库现状：

- v23 合并集约 48,560 windows，JSON 约 7.4 GB；
- raw_trace_pool 约 582 GB；
- workload 主要是 17 个本地 proxy/microbenchmark；
- uarch_configs.yaml 只有 arch_A；
- 大量窗口来自相同 run、相同 binary 和相邻 phase。

工程 schema 也已经漂移：builder、model 和 pmu_keys.yaml 的目标集合不一致；
arch_A 物理 L3 为 8 MiB，但 legacy cfg token 仍保留 log2(KiB)=11 而非 13。
这些必须在新数据版本中先统一，否则同名实验也可能使用不同标签/配置含义。

窗口数不是有效样本数。对于泛化，独立单位是 program family、binary build、input、run seed、schedule seed、uarch 和 core/thread mix。继续增加重叠窗口的边际价值远低于增加这些独立维度。

## 2. 任务定义：先明确要模拟的量

多核“CPI”至少有三种不同含义，必须同时保存、分别报告：

1. per-core macro CPI：

       CPI_macro_i = cycles_i / retired_macro_i

2. per-core uop CPI：

       CPI_uop_i = cycles_i / retired_uop_i

3. aggregate instruction-weighted CPI：

       CPI_agg = sum(cycles_i) / sum(retired_macro_i)

它们都不等于程序 wall-clock latency。对于多线程 ROI，还要单独定义：

    makespan_cycles = global_ROI_end - global_ROI_begin
    system_throughput = sum(retired_macro_i) / makespan_cycles

推荐：

- 模型主标签为每核可加 cycles 或固定 quantum 下的 log cycles-per-unit；
- macro CPI 作为主用户指标；
- uop CPI 保留为微码展开稳定性诊断；
- aggregate CPI、makespan 和 throughput 均由原始量派生，不相互冒充。

## 3. 推荐的整体架构

### 3.1 四层分工

第一层：真实汇编语义编码。

- 输入真实 macro instruction 或 basic block；
- 使用 Coding LLM 原生 tokenizer；
- 输出 instruction/basic-block embedding；
- 静态内容按 binary build 缓存，不在每个动态窗口重复计算。

第二层：每核动态行为编码。

- 输入 basic-block embedding、动态分支方向、地址 alias、reuse、stride、dependency、重复次数和历史摘要；
- 处理连续 program-order trace；
- 输出每核 demand embedding。

第三层：多核 shared-system 建模。

- 输入每核 demand、active core、共享 cacheline/page graph、同步事件和 typed uarch；
- 使用 permutation-equivariant set transformer、稀疏 graph attention 或显式 cache/coherence/DRAM simulator；
- 只让引用同一 line/page、共享 bank/channel 或时间上可能重叠的事件交互。

第四层：数值与状态头。

- per-core cycles/CPI distribution；
- branch/cache/TLB/coherence/DRAM 辅助计数；
- shared pressure 和下一步 state proxy；
- rollout scheduler 使用预测结果推进，而不是读取真值 tick。

### 3.2 为什么不把 c32 所有汇编直接拼成一个 100K token prompt

- decoder-only attention 成本随长度近似平方增长；
- 多核重要交互通常是稀疏的，不需要所有 token 全连接；
- 现有 tail query 已经显示不同核 hidden 过度相似；
- 长 context 声称支持 128K/256K，不等于能稳定利用中间所有信息；
- 推理每个 window 都跑大模型会让 surrogate 比传统快速 simulator 还慢。

推荐的层次化 token 预算：

| 层 | 建议长度 |
|---|---:|
| 一个 basic block | 8–128 native tokens |
| 一个 target-core chunk | 256–2048 macro，约 2K–12K tokens |
| top-K interacting cores | 每核最近 64–256 macro |
| 其余核心 | 每核一个结构化 summary vector |
| cross-core module | 32–256 nodes，而不是 100K raw tokens |

### 3.3 大模型的正确使用方式

直接在线方案：

- 1.5B/3B Base 编码每个 target-core chunk；
- LoRA 适配；
- 适合第一阶段验证语义收益。

离线 teacher 方案：

- 7B、Qwen3-Coder-30B-A3B 或更大模型编码静态 basic block/function；
- 缓存 teacher embedding 或结构化语义标签；
- 蒸馏给 100M–3B 的在线 trace encoder；
- 在保留语义先验的同时降低窗口仿真成本。

对于大型 Coding LLM，这是比每个动态窗口重复读相同循环体更合理的工程落点。

## 4. 数据构建

### 4.1 不可拆分的 run_id

先按以下字段生成 run_id，再切窗口：

    program_family
    source_commit
    binary_hash
    compiler_and_flags
    input_id
    data_seed
    schedule_seed
    uarch_profile_hash
    hardware_core_count
    active_threads
    pinning_and_topology
    warm_state_policy
    simulator_commit

同一个 run_id 或同一更高层 family group 绝不能跨 split。

### 4.2 三层数据格式

静态指令字典，每个 binary 只存一次：

    module_id
    module_relative_pc
    instruction_bytes
    decoder_version
    real_mnemonic
    operand_kinds
    src_and_dst_arch_registers
    immediate_or_displacement
    branch_relative_target
    function_and_basic_block_id

动态 functional stream，每个 macro 一条：

    core_id, thread_id, program_order_seq
    static_instruction_id
    branch_taken, dynamic_target_class
    load_store_atomic_fence, size
    effective_virtual_address
    physical_line_if_functionally_available
    producer_distance
    barrier_lock_atomic_sync_event

标签流，独立保存：

    cumulative_retire_cycles
    accesses_and_misses
    branch_mispredict
    tlb_and_page_walk
    coherence_and_dram_events
    queue_and_stall_metrics
    coverage_and_provenance

禁止进入模型输入的字段：

    commit/fetch/issue/complete tick
    cache hit/miss and path_class
    MSHR occupancy
    TLB hit
    coherence owner/sharer/state
    mispredicted flag
    stall reason

### 4.3 当前 raw 能否恢复真实汇编

对于当前 workloads/bin 下的静态、非 PIE、带 debug ELF，可以：

1. 用 binary hash 和 decoder version 建 PC 到 instruction 的表；
2. 用 raw macro_pc 查真实 macro；
3. 把现有 producer_dists、vaddr、size、branch type 合并到动态记录；
4. 对 REP/微码指令按 macro boundary 聚合，而不是一 uop 一行。

这使得第一版 pilot 不必重跑全部 gem5。

面向通用程序时必须补：

- ASLR：记录 module build-id 和 module-relative offset；
- shared library：每个 module 单独建表；
- JIT/self-modifying code：首次出现时保存 instruction bytes；
- decoder 一致性：优先复用 gem5/XED/LLVM 同版本 decoder，并随机和 objdump 对照。

### 4.4 推荐的渲染格式

若使用 Instruct 模型，system prompt 固定为任务契约，而不是塞入不可验证的
“专家知识”。例如：

    You encode functional x86 traces for quantitative CPU performance modeling.
    Use only the provided assembly, functional dynamic annotations, and typed
    microarchitecture profile. Cache hits, miss outcomes, pipeline timing, and
    coherence state are not provided and must not be assumed. Preserve per-core
    identity and shared-line relations. The downstream head, not this prompt,
    predicts continuous cycle and event values.

回归主线不需要 assistant completion；取 hidden state 给连续 head。Base 模型则
用等价的 canonical task prefix，不强行套 chat template。固定 prompt 对所有样本
没有区分力，所以必须做 prompt on/off 消融，不能把 system prompt 本身当成知识增益。

示例：

    config:
      issue=6 commit=8 rob=192 iq=64
      l1d=32KiB,8way,4cy l2=512KiB,8way,12cy
      llc=16MiB,16way,40cy mshr=16/32/64 dram=200cy

    core 2 target:
    B12 repeat=17
      mov rax,[rbx+rcx*8] ; ld sz8 line=A reuse=cold stride=random dep=17
      add rdx,rax
      cmp rdx,rsi
      jne B12             ; taken

    interacting cores:
      c0 line=A read 12 write 0 recent=hot
      c5 line=A read 3 write 8 recent=hot

规范化规则：

- 保留真实 mnemonic、真实寄存器、operand kind、scale 和小常量；
- ASLR 地址替换为 module-local block label；
- 大 immediate 和大 displacement 做 signed/log bucket；
- data line 按窗口首次出现分配短 alias；
- 跨核相同物理 line 使用相同 alias；
- branch taken/target、reuse、stride、dependency 来自 functional history；
- 不输入 workload 名、路径、seed 和 cfg_hash；
- 特殊 token 只保留少量结构边界，最好不超过 8–16 个。

### 4.5 为什么数值不应全部文本化

Coding LLM 的原生 token 对 mov、imul、rax、jne 等离散语义很有价值，但对 0.842、200 cycles、64 KiB、长地址和连续时间并没有天然连续性。

建议双通道：

- textual channel：真实汇编和少量可读 category；
- numeric channel：标准化后的 uarch scalars、reuse distance、stride、count、time estimate；
- 在 chunk summary 或 head 前通过 concat、FiLM 或 cross-attention 融合。

“充分利用 LLM 语义”不等于“所有变量都必须变成 token”。后者会让数值相邻性、单位和尺度关系变得更难学。

## 5. 窗口、历史状态与闭环推进

### 5.1 正式训练窗口只依赖 functional 信息

主尺度建议：

    M in {256, 1024, 4096} retired macro instructions per target core

规则：

- eval target window 不重叠；
- train 可用 stride=M/2，但重叠窗口属于同一 cluster，并按 cluster 降权；
- history prefix 和 target region 分离；
- history 只作为输入，target region 的 cycles 才计 loss；
- split 在切窗前完成；
- 任何 commit tick 都不能决定输入边界或每核配额。

多核 race-free workload 可优先按 barrier 或逻辑 iteration 分 phase，再在 phase 内切固定 macro quantum。

### 5.2 warm state

同一指令窗口在不同 cache、TLB、predictor 和 coherence 初态下不是同一问题。至少分三种正式 track：

| track | 定义 |
|---|---|
| cold_reset | cache/TLB/predictor 清空 |
| realistic_warm | functional fast-forward 后 detailed warmup |
| carried_state | 连续 rollout，窗口间不重置 |

建议 warmup 起点为每核 100K macro；内存型 workload 还需覆盖至少约 2 倍 LLC line 数的访存。比较 warmup 前后两段 CPI/miss-rate，变化仍超过 5% 时延长，最大可到 1M macro/core。

模型或外部 simulator 必须维护：

- 每核最近 64K memory references 的 reuse state；
- 最近 4K branch outcomes/target history；
- 跨核 shared-line access/ownership proxy；
- sync/atomic history；
- 当前预测时间和 shared-resource queue state。

### 5.3 开环与闭环必须分榜

开环 window track：

- 给定固定 functional 边界；
- 评价局部 cycles/CPI 和 PMU；
- 用于模型迭代和消融。

闭环 simulator track：

- 从统一初始状态开始；
- 后续边界、core overlap 和事件顺序只由模型预测决定；
- 全程禁用真值 tick；
- 报告 1M/10M macro 累计漂移、ROI makespan 和吞吐。

当前真值时间 TQ 只能保留为 oracle diagnostic，不得作为主训练或主榜。

### 5.4 用 DAgger 式数据聚合处理 planner 分布偏移

闭环预测会改变下一个窗口的输入分布。推荐迭代监督，而不是直接上 RL：

1. 用 functional 固定边界数据训练初始模型；
2. 让模型在训练 run 上闭环 rollout；
3. 收集模型实际访问到的窗口和状态；
4. 从已有 detailed trace 查询这些边界的真值标签；
5. 合并数据重训；
6. 重复 2–4 轮，并加入 4/16/64-step cumulative loss。

这等价于让训练分布逐步接近模型自身诱导的部署分布，目标仍然是可监督的。

## 6. 多核与微架构数据覆盖

### 6.1 workload 机制矩阵

必须覆盖：

- integer、FP/SIMD、divider、不同 dependency depth 和 ILP；
- predictable/unpredictable/indirect branch、call/return；
- stream、stride、pointer chase、gather/scatter、不同 working set；
- TLB/page stride、huge page、random page；
- read-mostly、true sharing、false sharing、atomic hotspot、locks；
- barrier、queue、producer-consumer；
- homogeneous、2-program、4-program multiprogram mixes；
- 至少 6–10 个完全 sealed 的真实应用 family。

参数采样应围绕 working-set boundary、branch entropy、R/W ratio、sharing degree、sync frequency 等机制坐标，用 Sobol/LHS，而不是只改一个 scale 或 seed。

### 6.2 core/thread/topology 维度

建议：

    hardware cores H = {1,2,4,8,16,32}
    active threads T = {1, ceil(H/4), H/2, H}
    pinning = {compact, spread}
    mix = {homogeneous, 2-program, 4-program, producer-consumer}
    SMT = off in phase 1

当前 nthreads 等于 ncores、同一 kernel 填满所有核的 harness 不能支撑通用多核结论。

### 6.3 微架构采样

第一轮 8 个 pilot profile；完整设计可扩展为：

    24 train uarch
    6 validation interpolation uarch
    8 sealed extrapolation uarch

至少覆盖：

- fetch/decode/issue/commit width；
- ROB、IQ、LQ、SQ；
- int/fp/div latency 和 throughput；
- predictor family、BTB/RAS、mispredict penalty；
- L1/L2/LLC size、assoc、latency、bank；
- 各层 MSHR；
- DRAM channels、banks、latency、bandwidth、queue；
- coherence protocol、topology、remote cost。

不要做完整笛卡尔积。使用 constrained LHS 加 OFAT anchors 和 balanced incomplete block：

- 每条 functional trace 至少配 4 个 uarch；
- 每个 uarch 覆盖所有机制族；
- 关键参数都有 low/mid/high counterfactual pair；
- predictor/protocol family 可整族留出测试。

同一 trace 在不同 uarch 上的配对标签，比增加更多相邻窗口更有价值。

### 6.4 trace reuse 的边界

functional trace 跨微架构复用只在以下条件下严格成立：

- 程序 race-free 或调度被确定性 replay；
- 控制流不依赖 wall-clock、timeout 或非确定竞争；
- OS/interrupt 行为被固定或排除；
- 页映射和线程绑核规则一致。

锁竞争、data race、自旋等待和 time-based code 的动态路径可能随 uarch 改变。此类 workload 要么重新执行 functional front-end，要么明确采用 deterministic schedule contract，不能把一次 trace 当作所有架构上的真实路径。

## 7. 模型设计

### 7.1 推荐主模型

每条 target-core 样本由三部分组成：

1. typed uarch prefix；
2. 其他核心的结构化 demand/sharing summary；
3. target core 的真实 canonical assembly 和动态 sidecar。

不要把所有核的 query token 放在同一个 decoder-only 序列尾部。推荐：

- 每个核心独立、共享权重地经过 Coding LLM；
- 在每个 instruction-end 或 basic-block-end 位置取 hidden；
- instruction attention pooling 得到 chunk embedding；
- 所有 core embedding 进入一个 permutation-equivariant cross-core module；
- 回归头对每个 core 输出数值。

这样既保持核数可变，也避免现有 tail queries 都看到近似相同前缀而塌缩。

### 7.2 静态与动态双编码器

静态语义编码器：

    assembly native tokens
      -> Coding LLM
      -> per-instruction / per-basic-block embedding

动态编码器：

    static embedding
    + branch outcome
    + memory alias/reuse/stride/size
    + dependency
    + history state
      -> local temporal transformer / SSM
      -> per-core demand

优点：

- 重复 loop body 的 LLM embedding 可缓存；
- 动态长 trace 不再重复 tokenize 相同静态代码；
- 大模型和在线 simulator 可以解耦；
- 数值 sidecar 不破坏汇编原生 token 语义。

### 7.3 cross-core module

至少需要三类 edge：

- same-line edge：不同核引用同一物理 cache line；
- shared-resource edge：同一 LLC bank、memory channel、NoC path；
- temporal-overlap edge：按预测时间可能同时占用资源。

节点可以是 core chunk 或 hot memory-region event。实现优先级：

1. typed summary + Set Transformer，工程最快；
2. sparse graph attention，显式 same-line edge；
3. LLM demand predictor + deterministic shared cache/coherence/DRAM simulator，科学解释性最好。

对于 c32 chase/graph/false-sharing，第三种 hybrid 应作为强 baseline，不能只比较不同 LLM。

### 7.4 uarch conditioning

同一个 uarch 信息从两条路径进入：

- human-readable canonical prefix，帮助 Coding LLM 对齐代码和机器参数；
- normalized numeric vector，直接进入 FiLM/cross-attention/head。

numeric profile 至少分 frontend、backend、execute、branch、cache/TLB、MSHR、DRAM、coherence 八组编码。单个 cfg_hash 或四个 cache-size token 不足以学习跨架构响应。

### 7.5 模型选择

当前本地已有：

- Qwen2.5-Coder-1.5B-Instruct；
- Qwen2.5-Coder-3B Base 与 Instruct；
- Qwen3-0.6B-Base 和 Qwen3-4B。

推荐实验顺序：

| 阶段 | 模型 | 用途 |
|---|---|---|
| P0 | Qwen2.5-Coder-1.5B Base/可获得的相近 Base | 最低成本语义 gate |
| P1 | Qwen2.5-Coder-3B Base | 主 pilot |
| P2 | Qwen2.5-Coder-7B 或同级 | size scaling upper bound |
| P3 | Qwen3-Coder-30B-A3B / Next | 离线 teacher，不直接做每窗热路径 |
| 对照 | Qwen3-0.6B current、3B scratch Transformer | 分离预训练语义与参数量 |

表示回归任务优先 Base。Instruct 适合 system prompt 遵循、RAG 抽取和 JSON 生成对照，但 agentic/post-training 分数不能替代本任务证据。

### 7.6 LoRA 与解冻策略

第一版：

    backbone native embeddings: frozen
    attention q/k/v/o: LoRA rank 32
    MLP gate/up/down: LoRA rank 16 or 32
    pooling, numeric encoder, cross-core module, regression heads: full train
    new structural token rows: full train
    LM head: absent/frozen

建议从 all-linear LoRA 开始，而不是只挂 attention，因为汇编域适配可能需要 FFN 容量。rank 16/32/64 做曲线，不把单一 rank 的失败解释成“LLM 无语义”。

若出现以下证据再扩容量：

- train 和 group-val 都欠拟合；
- head-only 到 LoRA 有持续收益，但 rank 提升仍未饱和；
- 语义辅助任务也无法被低 rank 拟合。

扩容顺序：

1. LoRA rank 提升；
2. 解冻 top 25% transformer layers，学习率约为 LoRA 的 1/10；
3. 全参数微调作为 upper bound。

全量微调是否遗忘、是否过拟合必须实验判断，不能仅凭样本数断言。Embedding 尤其应保持冻结，除非有专门的 assembly continued-pretraining 证据。

QLoRA 主要用于 7B 以上模型权重装载。它不能解决长序列 activation 和 attention 成本；仍需要 bf16 compute、gradient checkpointing、FlashAttention、packing 和 token-budget batch。

## 8. Regression head 与 LM head

### 8.1 主线选择 regression head

对每核输出：

    mu_log_cycles_per_macro
    log_scale_or_quantiles
    auxiliary_event_counts
    optional_bottleneck_class

优点：

- 842 与 843 在目标空间相邻；
- 一个 forward 并行得到所有数值；
- 能直接使用 Huber、NLL、quantile、物理一致性和 counterfactual loss；
- 无 JSON parse failure；
- 可以自然输出不确定区间；
- 推理速度远高于 autoregressive JSON。

[EACL 2026 的 hidden-state numeracy 研究](https://aclanthology.org/2026.eacl-short.47/)
也观察到数值 magnitude 可由简单 probe 从 hidden 中恢复，而模型把相同数值
可靠地生成出来更难；这与本项目“LLM 表征 + 连续 readout”的分工一致。

### 8.2 为什么纯 JSON CE 不适合作为主目标

next-token cross entropy 把数字 token 当类别，不表达数值距离。把 CPI 乘 1000 变成整数可以降低格式难度，但没有解决：

- 预测 842 和 843 的 loss 不一定比 842 和 5000 更接近；
- 多 token 数字会累积 decode error；
- per-core key 顺序造成不必要的自回归依赖；
- valid JSON rate 与 CPI 精度没有必然关系；
- c32 输出更长、更慢。

数值 tokenization 与连续数值建模的研究也表明，默认离散 encoding 缺少科学回归需要的连续归纳偏置。

### 8.3 生成式方案应怎样作为公平对照

固定同一 backbone、输入、split 和 token budget，比较：

- R1：continuous MLP head；
- R2：LM head + compact JSON + completion-only CE；
- R3：LM head + CE + number-aware Wasserstein/expected-value loss；
- R4：ordinal bins + within-bin residual regression。

R2/R3 必须：

- constrained decoding；
- greedy decode，禁止温度采样；
- 输出 parse rate、numeric error 和 decode latency；
- 禁止把 chain-of-thought 当隐藏评估变量。

LM head 更适合生成瓶颈类别、简短解释或审计信息，这些可以作为小权重辅助任务，而不是精确 CPI 主通道。

## 9. 标签与损失设计

### 9.1 原始标签先于变换

dataset 永久保存 raw additive counts：

    cycles
    retired_macro
    retired_uop
    branch_count / branch_miss
    cache_and_tlb_access / miss
    remote_hit / invalidation / ownership_transfer
    dram_request / row_hit / bytes
    mutually_exclusive_stall_cycles_if_available

训练时再派生 log、ratio、rate。不要只保存变换后的 CPI 或 miss rate。

### 9.2 第一版确定性 loss

定义：

    y_i = log(cycles_i / retired_macro_i)
    p_i = predicted log cycles per macro

建议起点：

    L =
      1.00 L_core
    + 0.50 L_window
    + 0.25 L_centered
    + 0.05 L_rank
    + 0.20 L_aux
    + 0.20 L_uarch_delta
    + 0.10 L_rollout
    + 0.05 L_physics

各项：

Core magnitude：

    L_core = log-cosh(p_i - y_i)

也可使用 log-space Huber，但 delta 建议从 0.3 左右起做消融。当前 0.1 delta 很快进入常梯度区，容易让长尾和高 spread 样本驱动力不足。

Window consistency：

    C_pred = sum_i exp(p_i) * retired_macro_i
    L_window = Huber(log C_pred - log C_true)

Centered per-core：

    L_centered =
      Huber((p_i - mean_core(p)) - (y_i - mean_core(y)))

它直接监督每核相对快慢，避免 aggregate cycles 抵消。

Pairwise rank：

    L_rank =
      softplus(-sign(y_i-y_j) * (p_i-p_j) / tau)

只对真实 gap 超过阈值的 pair 计算；它只能辅助顺序，不能代替 magnitude。

Auxiliary events：

- 第一版对 log1p(count) 用 Huber；
- 稀疏零值事件可拆成 zero/nonzero BCE 加 positive-count 回归；
- 若需要概率模型，可用 Negative Binomial/Poisson deviance；
- 禁止对大量零值 miss count 用普通 MAPE。

### 9.3 概率与尾部

第二版让 head 输出 mean 和 scale，使用 Student-t 或 Gaussian NLL：

    z_i ~ StudentT(mu_i, sigma_i, nu)

为防止靠放大 sigma 逃避难样本：

- clamp log sigma；
- 加轻量 scale regularizer；
- 前若干 epoch 只训 mean；
- 在独立 group-val 上做 conformal/temperature calibration。

也可直接输出 p10/p50/p90，以 pinball loss 训练。最终报告 90%/95% coverage 和 sharpness。

### 9.4 跨微架构 paired loss

同一 trace 在 uarch a、b 上：

    L_delta =
      Huber(
        (pred_b - pred_a)
        - (label_b - label_a)
      )

它迫使模型学习 ROB、MSHR、latency、cache size 等变化如何作用于具体 trace，而不是记住两个配置的均值。

monotonic loss 只能加在严格单变量 counterfactual pair 上，并用对应 stress proxy gating。例如 DRAM latency 增大对 cold-load-heavy 窗口不应使 cycles 下降。不要把“cache 越大永远越快”作为全局硬规则。

### 9.5 rollout loss

闭环阶段对 1/4/16/64-step horizon 累计时间加入：

    L_rollout =
      sum_h alpha_h *
      Huber(log cumulative_cycles_pred(h)
            - log cumulative_cycles_true(h))

局部 window loss 低并不保证长期 simulator drift 小；这一项是正式仿真的必要目标。

### 9.6 物理与对称性

可安全使用的约束：

- cycles、count 非负；
- branch_miss 不超过 branch_count；
- 某级 miss 不超过其对应 access；
- 预测在 core permutation 后等价 permutation；
- adjacent chunk raw cycles 可加；
- fixed trace/uarch/state 的重复预测一致。

不安全的约束：

- 把所有 stall components 简单相加到 cycles，除非标签定义互斥；
- 无条件假设更大 cache、更宽 core 总会更快；
- 用微架构 oracle hit/miss 作为输入来保证约束。

第一版固定 loss 权重并记录各项梯度尺度。不要立刻复用 learned uncertainty weighting，因为它可能自动降低最难、却最关键的 c32/coherence 任务权重。

## 10. 训练策略：SFT、DAPT 与 RL

### 10.1 Stage A：语义与数据 gate

先不训练 CPI 大模型，做：

- PC 到真实反汇编正确率；
- tokenizer token/macro、截断率和 length distribution；
- frozen Coding LLM linear probe；
- real assembly、pseudo assembly、shuffled mnemonic、side-only 对照；
- metadata-only leakage probe。

zero-shot 问答能识别 memory-bound 只能说明 prompt 中有明显关键词，不能证明数值仿真可行。

### 10.2 Stage B：可选领域继续预训练

如果 frozen probe 证明原模型对汇编信号弱，可在去重的真实 assembly 上做短期 DAPT：

- next instruction/token；
- branch target/outcome；
- def-use relation；
- masked operand/register；
- basic-block adjacency；
- semantics-preserving register rename contrast；
- LLVM-mca/uiCA 或 measured basic-block throughput 辅助任务。

这比重复喂动态 loop trace 更有效。DAPT 必须保留一份通用 code/assembly validation，监控灾难遗忘。

### 10.3 Stage C：监督多任务微调

这里的 SFT 指有标签的 supervised regression fine-tuning，不是默认的
next-token instruction tuning。只有 LM-head JSON 对照才使用 completion-only CE。

课程顺序：

1. single-core、fixed uarch；
2. multi-core homogeneous；
3. mixed workload + sharing/sync；
4. multi-uarch paired samples；
5. closed-loop DAgger + rollout loss。

起始超参数：

| 项 | 建议 |
|---|---|
| optimizer | AdamW |
| LoRA learning rate | 1e-4 附近，按 1.5B/3B sweep |
| new head learning rate | 5e-4 到 1e-3 |
| partially unfrozen layer LR | LoRA LR 的 0.05–0.1 |
| warmup | 3% |
| scheduler | cosine |
| grad clip | 1.0 |
| precision | bf16 |
| batch | 按总 token 数组 batch，不按样本数 |

batch sampler 按 program family → uarch → interaction type → run → phase 分层，避免 phased_mix 或长 trace 主宰训练。

### 10.4 为什么第一版不做 RL

已有精确 cycles/PMU 标签时，监督回归直接、低方差、可微。PPO/GRPO 会：

- 把 cardinal label 退化成 sampled sequence reward；
- 增加 rollout 和 reward-normalization 方差；
- 容易 reward hacking；
- 使实验归因复杂；
- 大幅增加训练成本。

RL 适合：

- 生成优化后的汇编并用 correctness + speed 计分；
- 选择下一个要详细模拟的 uarch/sample；
- 自适应 trace sampling 或 simulator control；
- 真正不可微的全局设计选择。

它不适合替代当前 CPI regression。Preference/DPO 也只能作为配置排序辅助；把精确 cycles 只变成 winner/loser 会丢失信息。

## 11. RAG 的位置

### 11.1 推荐离线流程

    official docs / gem5 config / RTL / source
      -> retrieval
      -> LLM schema extraction
      -> deterministic verifier
      -> typed uarch profile
      -> normalized vector + canonical text
      -> versioned profile cache

每个字段包含：

    name
    value
    unit
    source_type
    source_path_or_URL
    evidence_span
    confidence
    extractor_version

verifier 负责：

- unit normalization；
- range 和 power-of-two 检查；
- config/source/doc 冲突规则；
- 相互依赖字段检查；
- 手工 override 与 provenance。

### 11.2 当前项目何时不需要 RAG

arch_A 已经来自 machine-readable YAML/JSON。这里应直接 deterministic parse，而不是让 LLM 重新猜一遍。RAG 对当前单 uarch 每个 window 都返回相同知识，不会增加区分度。

RAG 真正有价值的场景：

- 新处理器只有 PDF manual、RTL 或复杂 simulator source；
- 需要抽取 instruction latency/throughput、queue size、protocol 行为；
- 需要给预测解释附来源；
- few-shot new-uarch cold start。

### 11.3 RAG 不能解决什么

RAG 无法创造：

- 当前窗口 cache/predictor 初态；
- 跨核真实 interleaving；
- coherence owner/sharer；
- DRAM queue/row state；
- 一个从未详细模拟过的参数变化对真实 workload 的响应标签。

因此它不能替代 multi-uarch paired dataset。若检索相似训练窗口及标签，则本质上是 kNN baseline，必须让 retrieval index 只含 train split，并单独报告。

## 12. 数据 split 与 leakage 审计

### 12.1 正式榜单

| split | 隔离单位 | 能证明什么 |
|---|---|---|
| ID-run | 同程序未见 input/seed/run | 基础重复性 |
| Input-OOD | 整个输入参数区间 | 同程序输入泛化 |
| Binary-OOD | compiler/flags/build 成组 | 是否记住 PC/模板 |
| Family-OOD | leave-one-program-family-out | 跨程序语义泛化 |
| Uarch-interp | 未见但在训练凸包内 | 参数插值 |
| Uarch-extra | 极值或未见 family | 架构外推 |
| Core-count-OOD | train 1/4/8/16，test 32 | 核数扩展 |
| Joint-OOD | family + uarch + core count 全未见 | 最强结论 |
| Sealed-real | 完全未用于设计调参的真实应用 | 最终可信度 |

同一 source family 的 proxy、编译变体、input 和 seed 必须整体分组。

### 12.2 固定 leakage tests

1. 仅用 core_split、length、fill 和时间 metadata 训练 GBDT；能显著预测 CPI 则数据失败。
2. 仅用 program/config ID；正式模型不得使用这些字段。
3. 对 split 间动态 PC/op/address shingle 做 MinHash；Jaccard 大于 0.8 报警。
4. target shuffle 后必须退化到均值。
5. trace-only 与 uarch-only 两个捷径对照。
6. mnemonic shuffle 和 consistent register rename。
7. 所有 normalization、bucket、dedup、retrieval index 只在 train 拟合。
8. 对输入 schema 建 allowlist；任何 oracle 字段出现即 fail build。

去重只在 train 内进行，且只基于输入 feature。聚类后保留 cluster weight，比顺序相关的 greedy keep-first 更稳。

## 13. Baselines 与语义归因

### 13.1 必须实现的 baselines

1. global mean 与 per-uarch mean；
2. Ridge/linear on op mix、dependency、reuse/stride、sharing、uarch；
3. XGBoost/LightGBM；
4. MLP 或 FT-Transformer tabular；
5. 小型 sequence Transformer from scratch；
6. TAO-like short-context self-attention model；
7. 当前 custom-token Qwen + regression head；
8. real assembly + frozen Coding LLM + head；
9. real assembly + Coding LLM LoRA + head；
10. 同输入的 LM-head JSON SFT；
11. deterministic/analytical shared-system hybrid；
12. oracle-state upper bound，只用于测不可辨识误差下界。

单核 basic-block 还应报告 llvm-mca、uiCA/Ithemal 类基线，但明确它们不建模多核 cache/coherence。

### 13.2 证明用了预训练语义的核心消融

| 消融 | 目的 |
|---|---|
| real assembly vs current pseudo assembly | 真实代码语义是否必要 |
| real assembly vs frequency-matched mnemonic shuffle | 是否只学词频 |
| Coding LLM vs general LLM vs random-init same architecture | 预训练代码语义收益 |
| exact registers/deps removed | def-use 信息价值 |
| address alias/reuse/stride removed | 动态内存信息价值 |
| cross-core graph removed | shared-system 信息价值 |
| side-only vs asm-only vs fusion | 防止 side feature bypass |
| typed uarch vs coarse cfg token vs RAG text | 架构条件价值 |
| Base vs Instruct | post-training 影响 |
| frozen/head-only vs LoRA vs partial/full FT | 适配容量 |
| 1.5B/3B/7B under equal train tokens/GPU hours | size scaling |
| history 0/4K/16K/64K | warm-state 可辨识性 |

“LLM 语义有效”的最低验收建议：

- 在 Family-OOD 或 Joint-OOD 上；
- real-assembly Coding LLM 相对 opclass baseline 和 shuffled-mnemonic control；
- cycles WAPE 至少相对改善约 5%；
- family-level paired bootstrap 置信区间不跨 0；
- 预测对 semantics-preserving rename 稳定，对真实 dependency/branch/address 变化敏感。

zero-shot 文字解释不计入这个证明。

## 14. 指标与统计

### 14.1 主指标

Cycles/CPI：

    cycles WAPE = sum(abs(C_pred-C)) / sum(C)
    macro-CPI log-MAE
    signed bias = sum(C_pred-C) / sum(C)
    within 5% / 10% / 20%
    p50 / p90 / p95 / p99 APE

多核：

    per-core cycles WAPE
    ROI makespan relative error
    system throughput relative error
    slowest-core top-1 accuracy
    core-rank Spearman/Kendall
    pred_spread / label_spread
    spread correlation

架构探索：

    pairwise faster/slower accuracy
    uarch rank correlation
    top-k design recall
    selected-design regret

不确定度：

    NLL
    90% / 95% interval coverage
    sharpness
    error-vs-uncertainty correlation

系统：

    end-to-end MIPS
    GPU memory
    preprocessing/cache time
    energy or GPU-hours if available
    speedup versus gem5

### 14.2 聚合和置信区间

- 同时报 per-window weighted average 和 per-workload macro average；
- 主结论用 program family → run 分层 bootstrap；
- 不能把数万相关窗口当作独立样本；
- 模型比较使用同一 run 的 paired bootstrap；
- 报告最差 workload、最差机制族和 worst-core tail，不只报均值。

## 15. 分阶段实施与 gate

### Phase 0：停止错误归因，2–4 天

交付：

- 标记当前 native_macro 为 pseudo prototype；
- 修 PMU schema 单一真值；
- 新增 group split manifest；
- 输入字段 allowlist 和 leakage probe；
- cycles additive label 修复；
- 禁止正式训练使用 truth-time TQ。

Gate：

- schema 一致；
- random overlap 不跨 split；
- adjacent cycles 重建 ROI 误差小于 0.5%；
- metadata-only baseline 不应接近 full model。

### Phase 1：真实汇编 pilot，3–7 天

交付：

- ELF PC-to-disassembly table；
- macro 聚合和 canonical renderer；
- static dictionary + dynamic sidecar；
- Parquet/Arrow 索引；
- tokenizer/decoder correctness report。

Gate：

- 随机抽查 1000 macro，decoder/objdump 一致率 100%；
- 无 label/oracle 字段进入 prompt；
- 1.5B/3B token budget 截断率在设计阈值内；
- real-vs-shuffled semantic probe 有可测差异。

### Phase 2：单核语义归因，1 周

训练：

- Ridge/GBDT/TAO-like/scratch/current-Qwen/frozen-Coder/LoRA-Coder；
- fixed functional window；
- Base/Instruct 和 model-size 小矩阵。

Gate：

- Family-OOD 语义收益满足第 13.2 节；
- 若 real assembly 不优于 pseudo/shuffle，停止扩大模型，先修数据和任务。

### Phase 3：多核单 uarch，1–2 周

交付：

- per-core encoder；
- cross-core set/graph module；
- sharing/active-thread/multiprogram 数据；
- spread/rank/tail loss；
- current baseline 在同一新 split 上重训。

Gate：

- 比同 split 的 current/TAO-like/GBDT baseline 更好；
- pred/label spread ratio 接近 1，而非当前约 0.3；
- c16/c32 memory/coherence tail 明显收敛；
- aggregate 改善不能以 per-core 退化换取。

### Phase 4：多 uarch + RAG profile，2–4 周

交付：

- typed schema/verifier；
- 8 个 pilot uarch；
- paired counterfactual dataset/loss；
- leave-one-uarch-out。

Gate：

- delta-uarch correlation 大于 0.6 作为初始研究门槛；
- pairwise faster/slower accuracy 显著超过简单均值；
- test-uarch 规格可读取，但其性能标签完全隔离。

### Phase 5：闭环 simulator，1–2 周

交付：

- prediction-only scheduler；
- DAgger 数据聚合；
- multi-horizon rollout loss；
- state carry；
- makespan/throughput/MIPS benchmark。

Gate：

- 1M macro 累计 signed drift 小于 5% 作为第一目标；
- sealed workloads 的 ROI makespan median error 小于 10%、p95 小于 25% 作为 MVP 目标；
- end-to-end 至少比 detailed gem5 快一个数量级，否则进入 teacher-distillation/更小 online model。

这些绝对阈值是研究 gate，不是已被当前数据证明的承诺；最终应按目标使用场景调整。

## 16. 资源与存储预算

### 16.1 存储格式

当前 JSON 每 uop 约 KB 级，不适合扩展多 uarch。改为：

- static instruction dictionary 单独保存；
- dynamic event 使用整数列、dictionary encoding 和 Parquet/Arrow；
- label columns 独立；
- window 只存 run_id、per-core start/end、history snapshot id；
- prompt 在 loader/cache 阶段生成，不在每个 window 复制长字符串。

目标 compact raw 约 100–200 bytes/uop。精确值先用 5% pilot 实测。

### 16.2 pilot 规模

一个可执行 pilot：

    8 program families
    x 3 inputs
    x 4 uarch
    x {1,4,8 cores}
    x 2 seeds
    = 576 runs

每核约 50K macro，总计约 0.1–0.2B uop；compact raw 约 30–80 GB；每 run 100–200 个均衡窗口，约 58K–115K samples。

若 local-core prompt 平均 4K–6K token，每 epoch 约 0.25–0.7B token。以仓库已有约 100K token/s 的 8 卡量级作为粗校准，1.5B/3B LoRA 的 3–5 epoch 可先按 8–25 小时 wall time 预留，再以真实 native BPE benchmark 修正。

### 16.3 full dataset

不做全笛卡尔积。可用：

    20 families
    x 每族 48 个 balanced conditions
    x 2 seeds
    = 1920 runs

约 0.8B macro、1.2–1.6B uop；compact raw 加 sealed test/repeats/index 预留 0.3–0.7 TB。沿用 JSON 很可能膨胀到数 TB。

gem5 采集成本必须先测 5% pilot。若单 run 为 0.25–2 小时，则 576-run pilot 是 144–1152 simulator-hours；16 路并发约 9–72 小时。

### 16.4 大模型成本控制

- 0.5B/1.5B/3B 的主要瓶颈可能是长序列 activation，不是模型权重；
- 7B 使用 QLoRA 仅作为 scaling study；
- 30B/80B teacher 按 unique basic blocks 离线运行一次；
- 静态 embedding cache、KV reuse、sequence packing、FlashAttention 必须计入最终 MIPS；
- 如果 online LLM inference 不能超过传统 surrogate baseline，应蒸馏，不应为了“大参数”牺牲 simulator 目标。

## 17. 当前仓库的具体处置建议

### 17.1 保留并复用

- raw records/labels 和 aligned Parquet fast path；
- macro_pc、macro boundary、producer distance；
- functional RD/stride/history proxy；
- cross-core sharing/pressure 特征；
- ROI/PMU coverage、online planner、hidden/spread diagnostics；
- c01/c04/c08/c16 train pool 与独立 seed eval pool；
- v16 作为当前最可信 clean deployment baseline；
- v22 的失败诊断作为 per-core collapse regression test。

### 17.2 暂停作为正式路线

- model/tokenizer.py 的 pseudo native renderer；
- run_v24_native_macro.sh 的 full training；
- random window val 选择 best checkpoint；
- truth-time TQ 主训练；
- v28 pure JSON CE 作为默认主线；
- c32 seedB train/eval 同源数据上的 OOD 声称；
- 在未完成 strict split 前继续比较更大模型。

### 17.3 当前 v24 原型的阻断问题

除 pseudo assembly 外，审计还发现：

- branch bit 的 producer/renderer 定义错位，conditional/call/return 会被错误渲染；
- 没有实际 system prompt/chat template，SYS 只是新增 token；
- native path 删除 cfg/global/summary，却仍从 side_feats 绕过 LLM 注入；
- TQ 按一 position/uop 预算，native BPE 扩张后 c08/c16 大量样本超长并被静默丢弃；
- local-core tensors 已生成但训练 forward 未真正消费；
- eval 默认仍构建 v22 tokenizer，不支持 native checkpoint；
- checkpoint/cache manifest 未完整保存 inject_mode 和 tokenizer fingerprint；
- tokenizer probe 把 path_class、coherence、TLB hit、mispredicted 等 label oracle 写入了输入；
- 仓库没有已通过的 zero-shot gate 结果。

因此不要在这些问题未修复时启动 full v24；即使 loss 下降，也没有科学解释力。

## 18. 最终决策树

    Real-assembly semantic gate passes?
      no  -> 不扩大 LLM；优先 TAO-like/hybrid structured model
      yes
        |
        v
    Frozen/LoRA Coder beats shuffle + scratch on Family-OOD?
      no  -> 预训练语义未转化为性能收益；检查任务/side shortcut
      yes
        |
        v
    Cross-core module beats local-core and analytical baselines?
      no  -> 显式 shared-system simulator 为主，LLM 仅做 demand encoder
      yes
        |
        v
    Multi-uarch paired deltas generalize?
      no  -> per-uarch adapter/calibration，不声称 zero-shot uarch
      yes
        |
        v
    Closed-loop drift and MIPS both过 gate?
      no  -> DAgger、state model、distillation
      yes -> 才可称为可部署的 LLM-assisted multicore simulator

## 19. 参考依据

与 learned microarchitecture simulation 最直接：

- [SimNet: Accurate and High-Performance Computer Architecture Simulation using Deep Learning](https://arxiv.org/abs/2105.05821)
- [TAO: Re-Thinking DL-based Microarchitecture Simulation](https://arxiv.org/abs/2404.10921)
- [uiCA: Accurate Throughput Prediction of Basic Blocks](https://arxiv.org/abs/2107.14210)
- [LLVM Machine Code Analyzer documentation](https://llvm.org/docs/CommandGuide/llvm-mca.html)
- [gem5 Ruby memory-system documentation](https://www.gem5.org/documentation/general_docs/ruby/)

与汇编/代码模型语义相关：

- [Qwen2.5-Coder Technical Report](https://arxiv.org/abs/2409.12186)
- [Qwen3-Coder official release](https://qwenlm.github.io/blog/qwen3-coder/)
- [Meta Large Language Model Compiler](https://arxiv.org/abs/2407.02524)
- [PalmTree: Learning an Assembly Language Model for Instruction Embedding](https://arxiv.org/abs/2103.03809)
- [BinBERT: Binary Code Understanding with a Fine-tunable Transformer](https://arxiv.org/abs/2208.06692)
- [Nova: Generative Language Models for Assembly Code](https://arxiv.org/abs/2311.13721)

与数值输出相关：

- [xVal: A Continuous Numerical Tokenization for Scientific Language Models](https://arxiv.org/abs/2310.02989)
- [Regress, Don't Guess: A Regression-like Loss on Number Tokens](https://arxiv.org/abs/2411.02083)
- [Tokenization Counts: the Impact of Tokenization on Arithmetic](https://arxiv.org/abs/2402.14903)

与训练和长上下文相关：

- [LoRA](https://arxiv.org/abs/2106.09685)
- [QLoRA](https://arxiv.org/abs/2305.14314)
- [Lost in the Middle](https://arxiv.org/abs/2307.03172)
- [FlashAttention](https://arxiv.org/abs/2205.14135)
- [DAgger](https://arxiv.org/abs/1011.0686)
- [DeepSeekMath / GRPO](https://arxiv.org/abs/2402.03300)
- [Retrieval-Augmented Generation](https://papers.neurips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html)

## 20. 一句话方案

先把现有 macro_pc 还原成真实汇编，用 Coding LLM 做可缓存的语义编码；把地址、历史、微架构和多核共享状态保留为结构化通道；用连续回归、多任务和闭环 rollout loss 预测可加 cycles；以严格 group split、shuffle/scratch 消融证明语义收益；RAG 只做离线 uarch 抽取，RL 暂不进入 CPI 主线。
