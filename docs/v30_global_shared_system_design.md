# TCSim v30：v29 模型骨架、显式 Global Shared-System 与 Branch Replay

状态：设计方案，尚未实施代码
日期：2026-07-27
适用范围：v30 训练与推理、serial free-running、单 trace 多 GPU 窗口并行、
后续多微架构训练

## 0. 执行结论

v30 定义为：

```text
v30
= v29 model skeleton
+ explicit Global Shared-System
+ configured standalone branch-predictor replay input
```

Global Shared-System，简称 GSS，由 functional memory stream 驱动，维护每条
trace 独立、跨核心共享的微架构状态，并向模型提供 cache-residency、替换压力和
shared-system 竞争信息。

该机制的定位是：

- 对齐 gem5 的地址映射、容量、组相联、bank 和替换策略；
- 在部署时只依赖 functional trace、目标微架构配置和模型自己的历史预测；
- 显式处理超过 256/512 UOP 窗口的长期 cache 历史；
- 帮助 Redis heldout、工作集变化、核心数变化和 cache geometry 泛化；
- 不把完整 gem5/Ruby 事件系统带入推理；
- 不声称从 commit-only trace 恢复 cycle-exact cache、MSHR 或一致性瞬态。

推荐的第一阶段范围是 D-side L1D、private L2 和 shared LLC tag/replacement
状态。第一阶段不实现真实 MSHR、TLB page walk、DRAM queue、NoC 和 MESI transient
state。

v30 保留 v29 的共享主干和单 timing head。Cache proxy 先作为 memory-token
条件输入进入主干，由 attention 将其影响传播到依赖指令；不重新引入独立且可被解释为
物理 memory latency 的 MemoryHead。

分支侧采用相同的“可确定机制显式计算、不可确定 timing 交给模型”原则。正式 v30
删除 neural `branch_head` 及其 branch BCE/count loss，以独立、配置驱动的 correct-path
branch-predictor replay 产生逐分支结果，并在第一个 full-QKVR block 前注入对应 branch
token。部署 branch PMU 也直接统计 replay 结果，不再使用 neural probability。输入只能是
functional trace 驱动的 replay 输出，严禁把 gem5 `mispredicted` 标签送入模型。

理论上，GSS/branch replay 提供了 v29 当前窗口无法唯一恢复的长期机制状态，因此 v30
的可达到误差上限优于只使用 v29 特征的模型；但这不等于第一版训练结果必然更好。
有限数据下的 feature shortcut、GSS proxy 误差、真实时间训练与预测时间推理的状态偏移，
都可能造成实际退化。第一版必须把 timing 路径构造成最佳 v29 的严格超集，并通过分项
消融和 serial free-running 验证收益。

### 0.1 v30 的正式版本边界

v30 不是 v29 的普通特征增量，也不命名为 v29.1。原因是它引入了跨窗口、跨核心、
跨推理 step 持久化的权威状态，改变了数据 schema、训练输入构造、推理 resume 和
多 GPU 协调合同。

v29 保留的模型骨架：

- global-time 多核联合窗口；
- full-QKVR Transformer 主干；
- per-UOP retirement gap cycle；
- monotonic prefix/global-time scheduler；
- core-slot permutation-equivariant 输入输出；
- 原 single timing head 和主要 timing/progress loss。

这里的“v29 模型骨架”不包含 neural `branch_head`。当前 v29 的 `branch_head` 与
`gap_head` 是两个并列输出，branch probability 没有进入 timing 路径；继续保留它只会
引入精度较差的辅助梯度和重复的部署预测器。v30 正式移除：

- `branch_head`、`branch_miss_logit` 和 `branch_miss_probability`；
- branch-token BCE/Brier loss；
- neural branch-count loss；
- 使用 neural probability 累计部署 branch PMU 的路径。

v30 新增的 Global Shared-System：

- 训练期真实时间戳全局 memory-event replay；
- 每条 trace 唯一的 canonical shared-system state；
- 每核 private L1D/L2 与跨核 shared LLC；
- gem5-aligned mapping、LRU/TreePLRU 和参数化 uarch profile；
- transactional shadow preview/delta；
- committed-prefix replay、rollback 和 resume；
- v30 state schema、order-policy version 和训练 sidecar；
- 后续可扩展的 coherence/TLB/MSHR/NoC/DRAM shared-state 接口。

第一阶段 GSS 只实现 cache 子系统，不等于第一版就实现完整 shared uncore。

Branch replay 不属于 GSS 的跨核共享状态。它是每核/每线程按 committed functional
program order 递推的 predictor state，不依赖模型预测时间，也不需要 canonical shared
state、transactional shadow 或 GPU lane rollback。它可以在训练和推理前确定性地计算为
逐 branch sidecar；GSS 则仍需按真实或预测的跨核事件顺序维护共享 cache state。

### 0.2 版本与兼容性

必须独立版本化：

```text
dataset_schema = tcsim-v30-...
feature_contract = v30-global-shared-system-...
state_schema = v30-gss-cache-state-...
branch_replay_contract = v30-configured-branch-replay-...
checkpoint_model_version = v30
```

v29 checkpoint 可以用于参数初始化或冻结结构 probe，但不能直接加载后假装成 v30
checkpoint。缺少 GSS contract/state metadata 时必须硬失败，不能静默退回 v29
输入语义。

启用 branch replay 输入的 v30 checkpoint 还必须校验 predictor family、predictor
configuration hash、replay implementation version、functional trace hash 和 cold/warmup
policy。未实现的 predictor family 必须硬失败，不能静默退回 neural head。当前已完整验证
的是 `TournamentBP + SimpleBTB + ReturnAddrStack + SimpleIndirectPredictor`；标准 TAGE
仍属于后续实现范围。完整 replay 语义见
[Standalone Branch Predictor Replay 设计](branch_predictor_replay_design.md)。

## 1. 为什么需要显式 Cache Proxy

### 1.1 当前模型缺少 cache-capacity-aware 历史

当前 v29 已包含 reuse distance、set pressure、窗口 locality 和多尺度 long-history
统计，但这些统计不能确定：

- 某条 line 是否仍在目标 L1/L2/LLC 中；
- 当前访问在 set 内的替换位置；
- 某次核心数或工作集变化是否跨过 cache capacity threshold；
- 其他核心是否已经把该 line 从共享 LLC 中驱逐；
- 相同 functional trace 在不同 cache size/assoc/bank 下如何改变命中路径。

256 UOP 或 512 UOP 窗口都远小于 64 MiB LLC 对应的历史长度。单纯增大
Transformer 参数量或层数无法恢复窗口外已经丢失的 tag/replacement 状态。

### 1.2 Redis heldout 的直接动机

当前源码与 gem5 统计审计显示，Redis heldout 相比 Redis base 同时具有：

- 更大的 private state 和 shared table；
- 更高的 uniform-tail 访问比例；
- 更多到达 LLC 的访问；
- 更高的 LLC miss rate；
- 更长的 load-to-use latency；
- 额外的串行 ALU dependency 和更高的 branch miss rate。

其中 cache proxy 能直接帮助解释的是“cache miss incidence”。它不能单独解释
dependency criticality、branch penalty 和 memory latency exposure，因此预期是必要的
结构性补充，而不是 Redis heldout 全部误差的单点修复。

### 1.3 纯模型学习微架构泛化的可辨识性问题

当前 v29 manifest 固定接受以下硬件配置：

| 项目 | 当前值 |
|---|---:|
| Private L2 | 1 MiB/core |
| Shared LLC | 64 MiB total |
| LLC banks | 8 |
| DRAM channels | 8 |

虽然模型输入包含部分 uarch 配置字段，但训练标签中没有 cache geometry 变化，模型
无法从单一配置识别“容量减半后哪些 reuse 变成 miss”。更大模型不能解决标签中没有
干预变化的问题。

Cache proxy 可以让不同 size/assoc/bank 配置立即产生不同的结构化命中结果；模型只需
学习这些结果如何与 OOO exposure、MLP 和最终 commit cycle 组合。它提高的是结构泛化
和样本效率，但仍不能代替多微架构 timing 标签。

## 2. “与 gem5 同源”的准确边界

### 2.1 TSim 已有实现

`/data00/yinhaolang/taogen/shared/lru_banked.hh` 已提供：

- `BankedSetAssocLRU`；
- `TlbSim`；
- `PageWalkSim`；
- `MshrTracker`。

`/data00/yinhaolang/taogen/mesi_ref_sim/include/simulator.hpp` 已组合：

- 每核 L1D/L1I/L2；
- 共享 L3；
- 简化 MESI line state；
- cache/TLB/walker/MSHR 派生字段。

这套实现可以作为代码和 differential-test 的起点，但不能未经修改直接作为 TCSim
生产实现。

### 2.2 当前 TSim shadow 与真实 Ruby 的替换策略不完全一致

源码审计结果：

| 层级 | 当前 gem5 实际配置 | TSim shadow |
|---|---|---|
| L1I/L1D | 显式 `LRURP` | LRU |
| Private L2 | `RubyCache` 默认 `TreePLRURP` | LRU |
| Shared LLC | `RubyCache` 默认 `TreePLRURP` | LRU |

因此 TSim 文档中的“bit-exact”表示 gem5 probe 内的 shadow oracle 与离线 ref-sim
共享同一份 shadow 代码，而不是 shadow state 与 Ruby `CacheMemory` 事件级完全一致。

已有四个 workload 的聚合对账中，shadow 对真实 Ruby LLC load miss 的平均
acc% 约为 90.65%。这说明现有实现提供了有价值的机制信号，但不应被当作真实 Ruby
cache outcome。

### 2.3 第一阶段的同源合同

第一阶段必须对齐：

- cache line size；
- physical address 到 set/bank 的映射；
- 每层 total/per-bank size 语义；
- associativity；
- L1 LRU；
- L2/LLC TreePLRU；
- valid/tag/replacement metadata；
- 支持的 fill、touch、evict、invalidate 顺序。

下列信息无法仅凭当前 functional/commit trace做到真实同源：

- issue、request、fill 和 completion 的真实顺序；
- wrong-path access 和投机 cache pollution；
- hardware prefetch request/fill；
- Ruby transient coherence state；
- MSHR entry 的真实占用周期；
- NoC、DRAM queue 和仲裁结果。

因此正式名称应为 `gem5-aligned functional cache proxy`，而不是
`gem5 cycle-exact cache simulator`。

## 3. 第一阶段状态范围

### 3.1 每条 trace 的权威状态

每条 trace 维护一份独立状态：

```text
TraceCacheState
  uarch_profile
  core[0].L1D
  core[0].L2
  core[1].L1D
  core[1].L2
  ...
  core[C-1].L1D
  core[C-1].L2
  LLC.bank[0]
  ...
  LLC.bank[B-1]
  state_schema_version
  mapping_policy_hash
  replacement_policy_hash
```

这里的“全局状态”仅指同一条 trace 内共享 LLC 的权威状态，不是所有 trace 共用
一个状态。多条 trace 推理时，每条 trace 完全独立。

### 3.2 第一阶段模型可见字段

推荐的 per-memory-token 字段：

| 字段 | 语义 |
|---|---|
| `proxy_hit_level` | L1/L2/LLC/MEMORY/UNKNOWN |
| `l1_pre_access_position` | 访问前在 L1 set 内的替换位置 |
| `l2_pre_access_position` | 访问前在 L2 set 内的替换位置 |
| `llc_pre_access_position` | 访问前在 LLC set 内的替换位置 |
| `l1_set_residency` | 访问前有效 way 数 |
| `l2_set_residency` | 访问前有效 way 数 |
| `llc_set_residency` | 访问前有效 way 数 |
| `proxy_miss_kind` | cold/capacity-or-conflict/unknown |
| `proxy_eviction_level` | 本次 fill 引发的最高层级驱逐 |
| `other_core_recent_line` | 是否存在近期跨核同 line touch |
| `paddr_valid` | 当前物理地址是否可用于精确 set 映射 |

推荐的 per-core/global summary：

- L1/L2/LLC proxy hit-rate EWMA；
- recent LLC miss run；
- recent eviction rate；
- active-core union LLC footprint；
- per-bank occupancy/pressure 的相对统计；
- current core 相对 active-core median 的 miss-pressure rank。

模型输入不暴露 raw physical address、固定 core ID 或 nominal bank-ID embedding。
内部状态可以使用真实 tag 和 bank ID bookkeeping。

### 3.3 缺失物理地址

Cache set/bank 必须以 physical address 为准。若某条 memory UOP 缺少有效 paddr：

- `paddr_valid=0`；
- `proxy_hit_level=UNKNOWN`；
- 不允许用 vaddr 静默更新 shared LLC；
- 可以继续提供与地址 relocation 无关的 reuse/locality fallback 特征。

## 4. Canonical State 与 Transactional Shadow Delta

### 4.1 Canonical state

`canonical state` 是该 trace 唯一的权威 cache 状态，只反映 scheduler 已经真正
提交的 functional memory UOP。

它必须满足：

- 未提交窗口不能修改它；
- rejected speculative window 不能修改它；
- resume 时必须和 cursor、predicted global time 一起恢复；
- schema 或 uarch policy hash 不一致时必须拒绝恢复。

### 4.2 Shadow preview

为了给未来窗口生成 cache 特征，需要从 canonical state 出发试运行未来访存：

```text
canonical state
      |
      +-- preview future functional accesses
      |
      +-- emit per-token cache features
      |
      +-- keep changes in a temporary shadow overlay
```

preview 可以遍历完整 256-UOP 模型窗口，但不得把完整窗口写入 canonical state。

### 4.3 Transactional delta

不能为每个窗口复制完整 cache。Shadow delta 只记录被当前 preview 修改的 set：

```text
(cache_level, core_or_bank, set_id)
    old/new tags
    old/new valid bits
    old/new LRU or TreePLRU bits
```

读取时优先查询 delta；该 set 没有被修改时回退到 canonical state。实现可以选择：

- touched-set copy-on-write overlay；或
- compact undo/redo log。

禁止使用全状态深拷贝作为正式实现。

### 4.4 Commit 与 rollback

假设模型输入覆盖 UOP 0 到 255，但 scheduler 最终只提交 UOP 0 到 99：

- UOP 0 到 99 的实际访存按 canonical order 更新权威状态；
- UOP 100 到 255 的 preview 修改全部丢弃；
- 下一轮从新的权威状态重新 preview。

对 speculative lane：

- lane 被接受不代表其完整 256 UOP 全部提交；
- 只有 scheduler 实际消费的 functional prefix 可以更新 canonical state；
- lane 被拒绝时直接丢弃全部 shadow delta；
- 不能用 speculative lane 的最终 shadow state 覆盖 canonical state。

### 4.5 为什么 commit 阶段建议重放实际访问

Preview 的跨核次序是模型预测前的近似顺序，而 scheduler 最终消费的 per-core prefix
和预测 commit order 可能不同。最稳妥的第一版流程是：

1. shadow preview 只负责产生模型输入；
2. GPU 输出 per-UOP gap cycle；
3. scheduler 确定实际提交 prefix；
4. 收集实际提交的 memory UOP；
5. 使用模型预测的 commit cycle 排序，稳定 tie-break 使用 core slot 和 per-core UOP
   ordinal；
6. 在 canonical state 上重放这些访问。

整个过程不读取 oracle commit tick。

## 5. 跨核 Functional Order

### 5.1 不可避免的近似

真实 cache 在 request/fill 时更新，而当前 TCSim 部署输入是 committed functional
stream。多核共享 LLC 的真实 request order 又依赖正在预测的 timing，因此存在循环：

```text
cache state influences predicted timing
predicted timing influences cross-core cache order
```

第一阶段不使用第二次完整模型 forward 来消除该循环。

### 5.2 训练期顺序：真实时间戳 Teacher Forcing

训练数据已经包含 gem5 真实时间戳。训练期禁止用旧 v29 checkpoint 的 free-running
结果生成 memory order 或 cache state。应直接从真实 trace 重放。

当前 raw micro label 的时间语义是：

```text
issue_abs_tick = fetch_tick + issue_tick
ready_abs_tick = ready_tick
retire_abs_tick = commit_tick
```

其中 `issue_tick` 和 `complete_tick` 是相对 `fetch_tick` 的 delta；不能把原始
`issue_tick` 或 `complete_tick` 直接当作跨核绝对时间排序。

如果目标是尽量复原物理 memory request/fill 顺序，训练 replay 应使用：

1. `issue_abs_tick` 生成 request/check 事件；
2. `ready_abs_tick` 生成 data-return/fill 事件；
3. `commit_tick` 只用于退休边界、训练窗口和最终 timing 标签。

如果第一版只实现每条 memory UOP 一个 cache-touch 事件，优先按
`ready_abs_tick` 排序，因为 `onDataAccessComplete` 与数据返回时刻最接近；不得把这种
顺序称为 issue order。

注意：当前 `mem_events.jsonl` 中的 `request` 和 `commit` 行是在 UOP commit 路径
写出的，其 `commit_tick` 不是 data request 到达 cache 的时间。它适合复现现有
commit-ordered ref-sim，但不是最准确的物理 memory-access sequence。构造物理顺序时
必须将 `records.micro` 与 `labels.micro` 按 `(core_id, thread_id, micro_seq)` join。

### 5.3 推理期顺序：预测时间递推

推理时没有 gem5 真值，因此运行同一套 cache transition engine，但事件时间来源改为
模型预测：

- v29 模型骨架已直接给出 predicted retirement/commit cycle；
- 第一版可以用 predicted commit order 驱动 committed-prefix replay；
- 如果要让推理也使用 request/fill 双事件语义，则必须额外预测或估计 issue/ready
  cycle，不能在训练期使用真实 issue/ready、推理期却静默改成另一种未声明语义。

这属于标准的：

```text
training = teacher-forced true event time
inference = autoregressive predicted event time
```

两侧必须共用同一个 cache mapping、replacement、event transition 和 tie-break 实现；
区别只允许是事件时钟来自真值还是模型预测。必须记录 `order_policy_version` 和
`clock_source`。

### 5.4 因果与防泄漏边界

真实时间戳只能在训练数据构建器内部用于排序和推进 teacher state：

- event E 的 cache 输入必须取 E 发生前的 state；
- 不能把 E 的数值 issue/ready/commit tick 作为模型输入；
- 不能把 E 之后的 state 回填给 E；
- 不能用未来窗口的真实时间戳修正当前 canonical state；
- Ruby 的真实 hit/path/evict/prefetch 只允许作为标签或 differential metric，除非
  部署端也实现了同一事件来源。

使用真实顺序生成“访问前状态”是 causal teacher forcing；把当前或未来真实时间数值
暴露给模型才是 timing leakage。

## 6. 模型集成

### 6.1 总原则：在首个 QKVR 前进入对应 Token

GSS 和 configured branch replay 特征的主要注入位置都是第一个 full-QKVR block
之前，但使用彼此独立的动态 encoder 和 token gate：

```text
v29 static token encoder
        +
v29 dynamic/resource encoder
        +
memory_mask * GSSAccessEncoder
        +
branch_mask * BranchReplayEncoder
        +
valid_mask * BranchHistoryEncoder
        |
        v
v29-compatible shared full-QKVR trunk
        |
        v
original single timing head
        |
        v
retirement gap cycle
```

Cache/GSS 信息先进入对应 load/store/atomic token，再由 self-attention、dependency
interaction 和 cross-core interaction 传播到真正受影响的后继 UOP。这样不要求把
memory penalty 强行全部归到 memory UOP 自己的 gap，也不会无条件污染所有非访存
token。

同理，replayed miss 先进入对应 control-UOP，由主干学习它与 redirect/recovery 附近
token 的关系，而不是额外加在最终 gap 上的固定 branch penalty。需要注意，当前
Full-QKVR 的 local attention 是 `is_causal=False`；branch embedding 在同一窗口内理论上
可以影响分支之前和之后的 token，不能把它描述成天然的“只向后传播”。第一版不为此
修改整个 v29 attention mask，而是增加因果 branch-history 字段、执行窗口边界归因审计，
并保留现有 joint-window 推理合同。

相同的 functional lookahead 也适用于窗口内较晚 memory token 的 GSS feature。它不读取
未来 timing oracle，但可能让模型把较晚的机制事件错误归因到较早 gap；因此 branch/GSS
都必须做 window shift、prefix truncation 和 penalty-position audit。

### 6.2 Per-access GSS 特征：独立动态 Token Encoder

以下字段属于具体 memory UOP：

- `proxy_hit_level`；
- L1/L2/LLC pre-access replacement position；
- L1/L2/LLC set residency；
- `proxy_miss_kind`；
- `proxy_eviction_level`；
- `other_core_recent_line`；
- 当前访问对应 LLC bank 的相对压力；
- `paddr_valid`。

建议新增独立输入：

```text
gss_uop_categorical: [N, K, F_gss_cat]
gss_uop_continuous:  [N, K, F_gss_cont]
```

其中：

- `N` 是 batch 中所有 active-core rows；
- `K` 是每核 token 窗口，第一版保持 `K <= 256`；
- categorical 字段走独立 embedding；
- continuous 字段先做固定合同归一化，再走小 MLP；
- raw physical address、完整 tag、固定 bank/core ID 不进入模型。

GSS 字段不能追加到 `StaticTokenEncoderV29` 的 `per_uop_fields`。这些字段随
canonical state 和 rollout step 改变，放入 static encoder 会使现有 CPU/GPU static
token cache 失效或错误复用旧状态。

推荐计算：

```text
gss_access_hidden =
    GSSAccessProjection(
        categorical_embedding,
        normalized_continuous_features
    )

memory_mask =
    valid AND mem_kind in {load, store, atomic}
```

### 6.3 Per-core GSS 状态：第一版只做 Memory-token FiLM

Per-core GSS summary 包括：

- L1/L2/LLC proxy hit-rate EWMA；
- recent LLC miss run；
- recent eviction rate；
- private/shared working-set footprint；
- current-core miss-pressure rank；
- 跨核共享和 active-core union footprint summary。

建议新增：

```text
gss_core_features: [N, F_gss_core]
```

它不直接加到该核心全部 K 个 token，而用于调制 per-access embedding：

```text
core_condition =
    GSSCoreEncoder(gss_core_features)

gamma, beta =
    GSSCoreModulation(core_condition)

conditioned_gss_access =
    LayerNorm(
        (1 + gamma) * gss_access_hidden
        + beta
    )

input_hidden =
    base_hidden
    + memory_mask * conditioned_gss_access
```

同一 LLC miss 因而可以在不同 per-core working set、共享压力和 miss history 下得到
不同表示，但 beta/gamma 仍只作用于 memory token。

当前 v29 cross-core gate 是 core-row 级向量，会调节该核心全部 token 的 cross-core
interaction。如果第一版直接加入 GSS summary，memory pressure 仍可能间接影响 compute、
SIMD 和 branch token，重新形成 workload/core 级 shortcut。

因此第一版保持现有 v29 cross-core gate 不变：

```text
cross_gate =
    Gate(
        relation_features,
        scheduler_state
    )
```

`compact_gss_core_features` 只用于调制 `gss_access_hidden`，且最终仍受
`memory_mask` 限制。GSS-aware cross-gate 作为后续独立消融；只有 access-only + FiLM
已经证明非退化后才能启用，并应采用有界、低增益增量，而不是替换原 gate：

```text
cross_gate =
    clamp(
        existing_v29_cross_gate
        + alpha * gss_cross_gate_delta,
        0,
        1
    )
```

其中 `alpha` 从 0 初始化，且必须单独报告 compute/branch token 的回归。

### 6.4 Global GSS 状态：留在状态机内部

完整 LLC tag、replacement metadata、bank state 和跨核目录状态不进入 GPU 模型。
GSS 根据当前 memory access 查询全局状态，并把结果编译成与该访问相关的 compact
特征：

```text
global LLC state
+ current memory access
        |
        v
hit level / replacement position / residency /
bank pressure / cross-core sharing relation
        |
        v
current memory token
```

少量真正的 sample-global summary 可以保留为：

```text
gss_global_features: [B, F_gss_global]
```

例如 global LLC occupancy、active-core union footprint 和 aggregate miss pressure。
第一版只把它们用于 GSSCoreEncoder 对 memory access embedding 的 FiLM 条件，不进入
cross-gate，也不直接广播 residual 到所有 core/token。

如果后续 NoC、DRAM channel、shared MSHR pool 等状态无法合理归属于单条访问，可以
增加 LLC-bank/DRAM-channel/global-system resource token。该方案会改变 attention
合同和 variable-resource topology，属于 v30 后续阶段，不进入第一版。

### 6.5 保留 v29 Single Timing Head

第一版最终输出保持：

```text
token_state =
    FullQKVR(input_hidden)

gap_cycle =
    OriginalGapHead(token_state)
```

不增加能够直接对最终 gap 做加法的独立 `MemoryCorrectionHead`。原因是最终 head 才
读取 GSS 时：

- cache miss 信息不能参与 QKVR interaction；
- 无法影响依赖该 load 的 compute token；
- 无法影响跨核 token interaction；
- 容易把整个 memory penalty 强行堆到 memory UOP 自身；
- 容易重现 E2 中 workload 互相拉偏的问题。

### 6.6 明确禁止的注入路径

第一版禁止：

```text
hidden += GSSCoreProjection(gss_core).unsqueeze(token_dimension)
```

这种写法会把 LLC 压力同时加入 compute、SIMD、branch、serialize 和不相关 memory
token，等价于 workload/core 级 timing bias。它与此前 long-history global residual
的退化机制相同。

同时禁止：

- 把 GSS state 拼入 `chunk_summary + relation + uarch + scheduler_state` 后经现有
  `side_projection` 统一广播；
- 只把 GSS 连接到最终 MemoryHead；
- 把完整 cache tag、set contents 或 request queue 送进 attention；
- 把固定 core ID、bank ID、channel ID 当 learned identity embedding；
- 让非 memory token 直接读取 access-only cache outcome。

### 6.7 v30 第一版模型输入合同

推荐新增四组独立版本字段：

```text
GSS_UOP_CATEGORICAL_FIELDS
GSS_UOP_CONTINUOUS_FIELDS
GSS_CORE_FEATURES
GSS_GLOBAL_FEATURES
```

数据流为：

```text
base_hidden =
    static_projection
    + dynamic_projection
    + existing_non_gss_side_projection

gss_hidden =
    GSSAccessEncoder(
        gss_uop_categorical,
        gss_uop_continuous
    )

gss_hidden =
    GSSCoreFiLM(
        gss_hidden,
        gss_core_features,
        selected_gss_global_features
    )

input_hidden =
    base_hidden
    + memory_mask * gss_hidden
    + branch_mask * branch_event_hidden
    + valid_mask * branch_history_hidden

token_state =
    FullQKVR(
        input_hidden,
        existing_v29_cross_gate
    )

retirement_gap =
    OriginalSingleTimingHead(token_state)
```

必须在 feature/checkpoint metadata 中分别记录四组字段的名字、维度、归一化合同和
encoder version，不能把它们混入 v29 原字段后仅增加总维度。

Branch replay 字段单独使用 `BRANCH_REPLAY_*` 合同和 encoder version，不混入 GSS
四组字段，也不追加到 v29 static token cache。

### 6.8 初始化策略

- 训练数据和 cache state 一律由真实时间戳 replay 生成，不允许旧 v29 checkpoint
  参与 memory order 或状态构造；
- 旧 v29 权重最多只能作为参数初始化或冻结主干的结构 probe，不能作为训练数据生成器；
- 从旧 v29 参数做结构 probe 时，可以把 GSSAccessEncoder 的最终 projection
  零初始化，使初始 timing 输出严格等于所选 v29 baseline；
- `BranchReplayEncoder` 和 `BranchHistoryEncoder` 的最终 projection 同样零初始化；
- 最终从头训练时，GSS feature 直接通过独立 encoder 并入 QKVR 输入，不要求额外
  residual adapter；
- 保持原 gap head，避免同时改变主干、GSS 和 timing-head 分解。

“严格超集”要求被选作 B0 的最佳 v29 timing 路径、基础输入和原 cross-core gate 在
v30 中原样保留。删除 `branch_head` 不影响 timing forward；旧 long-history 字段是否
删除必须另做消融，不能与 GSS/branch replay 同时移除。结构等价测试必须让 v29/v30
加载相同的 shared timing weights，并将所有新增 projection 置零：

```text
max_abs(v30_timing(new_paths_zero) - v29_timing) <= numeric_tolerance
```

该测试证明的是 v30 hypothesis class 包含 v29 timing path。冻结/初始化 probe 可以直接
复制最佳 v29 权重；正式从头训练只要求结构和初始 shared weights 可对应，不声称一个
随机初始化模型已经等于训练完成的最佳 v29。若不能满足该结构等价性，就不能把后续差异
归因于新增机制输入。

### 6.9 训练期辅助监督

允许用 gem5 的真实 cache/path 字段做低权重辅助监督，但必须满足：

- oracle 字段只进入 loss/metric；
- 不进入 cache proxy 状态更新；
- 不进入部署输入；
- 辅助 loss steady-state contribution 建议不超过总 loss 的 2%；
- 保留 auxiliary weight 为 0 的消融。

第一阶段不使用 MSHR/TLB/coherence 辅助输出控制最终 timing，也不构造独立
Base/Memory timing 标签。

### 6.10 删除 Neural Branch Head，Replay 结果作为输入

#### 6.10.1 决策依据

当前 neural branch head 的输出没有进入 `gap_head`，只用于独立 branch loss、统计和
部署 branch count。seed1 C4/C8/C16/C32 共 92 条 trace、36,587,576 个 branch 的
configured replay 审计结果为：

| 指标 | All | Heldout |
|---|---:|---:|
| Event F1 | 99.824% | 99.716% |
| Event mismatch | 0.015% | 0.034% |
| Pooled miss-count error | 0.074% | 0.334% |
| 256-UOP window exact match | 99.914% | 99.811% |
| 256-UOP window rate MAE | 0.0197 pp | 0.0323 pp |

历史 neural head 的 aggregate miss count/rate error 为 all `30.56% / 1.56 pp`、heldout
`74.56% / 4.38 pp`；configured replay 分别为 `0.074% / 0.0032 pp` 和
`0.334% / 0.0199 pp`。完整证据见
[v29 packed-3 评估报告](v29_packed3_checkpoint_evaluation_report.md)和
`logs/branch_replay_event_window_seed1_20260720_full/summary.md`。

因此 v30 不再要求 Transformer 重复学习一个已经可以由 functional trace 和 predictor
配置高精度确定的机制。删除 branch head 本身不会改变当前 timing 计算；真正可能改善
CPI/window error 的改动，是把逐 branch replay 结果注入 full-QKVR 并重新训练 timing
模型。

#### 6.10.2 输入字段与注入位置

第一版把输入分为当前 branch event 和对所有 UOP 可见的 causal prefix history。
当前 event 字段为：

```text
replay_full_miss
replay_direction_miss
replay_target_miss
replay_cold_state
```

它们只在对应 branch token 上有效。Causal prefix-history 字段为：

```text
uops_since_previous_replay_miss
branches_since_previous_replay_miss
replay_misses_last_16_branches
replay_misses_last_64_branches
previous_replay_miss_kind
```

实现稳定后可以增加：

```text
replay_btb_miss
replay_ras_miss_or_unknown
replay_indirect_miss
replay_provider
replay_confidence_or_counter_margin_bucket
```

后五项只由当前 UOP 之前的 replay 结果构造，用来覆盖 branch penalty 归属和 256-UOP
窗口边界。所有 distance/count 字段必须截断、分桶或按固定合同归一化。随机窗口只能
读取从 trace 起点连续 replay 后的 history sidecar，不能从窗口起点重新计算。

两组字段使用独立轻量 encoder，避免把 current miss 回填到非 branch token：

```text
branch_event_hidden =
    BranchReplayEncoder(current_branch_event)

branch_history_hidden =
    BranchHistoryEncoder(causal_prefix_history)

input_hidden =
    base_hidden
    + memory_mask * conditioned_gss_access
    + branch_mask * branch_event_hidden
    + valid_mask * branch_history_hidden

token_state =
    FullQKVR(input_hidden, existing_v29_cross_gate)

retirement_gap_cycle =
    OriginalSingleTimingHead(token_state)
```

不增加独立 `BranchPenaltyHead`，也不为 base/branch 构造两套 timing 标签。单一 timing
head 继续直接监督真实 per-UOP retirement gap cycle，由主干学习 replayed miss 在当前
ILP、依赖关系、ROB 压力和后续控制流中的可见代价。

删除 branch auxiliary loss 可能移除少量控制流表征的正则化作用，因此必须用“只删除
head/loss、尚未加入 replay”的隔离实验测量。但正式 v30 不为这种潜在正则化保留一个
精度较差的生产 branch head；如需诊断，只能使用不进入正式 loss/checkpoint 合同的
detached linear probe。

#### 6.10.3 因果与防泄漏合同

允许进入 replay 的信息只有 functional trace 中部署时同样可得的 branch PC、kind、
实际功能方向、target/next-PC、thread 和 committed program order，以及目标 predictor
配置。对于每个 branch，必须先使用事件前 predictor state 产生预测，再与该 branch 的
functional outcome/target 比较生成 replay miss，最后更新 predictor state。

明确禁止作为模型输入或 replay transition 条件：

- gem5 `mispredicted`；
- gem5 BTB hit、provider、预测方向或 predictor table snapshot；
- squash/wrong-path、真实 recovery latency；
- fetch/issue/ready/commit tick；
- 真实 branch penalty cycle；
- 固定 PC、core ID 或 predictor table identity embedding。

真实 `mispredicted` 只能在 replay 完成后用于 event/count/rate differential audit。由于
TCSim 是 trace-driven simulator，功能方向和 target 属于 architectural trace 事实，不是
timing oracle；但 correct-path replay 仍无法恢复 trace 中不存在的 wrong-path predictor
污染，该近似必须在报告中保留 cold/steady 分项。

#### 6.10.4 训练、推理与 PMU 合同

训练和推理必须调用同一 production replay engine，使用同一配置和初始状态策略：

```text
functional branch stream + predictor config
                    |
                    v
       configured predictor replay
                    |
      per-event + per-UOP prefix sidecar
                    |
                    +--> BranchReplay/HistoryEncoder --> Full-QKVR timing
                    |
                    +--> deterministic replay miss accumulation --> branch PMU
```

训练 sidecar 可以离线生成；推理也可以在模型 rollout 前按每核 branch ordinal 一次性
生成，因为 replay 不依赖预测的 global time。窗口随机采样和单 trace 多 GPU 只能读取
预先对齐的 sidecar，不能在窗口边界把 predictor 冷启动。PMU 字段应改名为
`replayed_branch_misses` / `replayed_branch_miss_rate`，如为兼容保留旧
`predicted_*` 名称，metadata 必须声明其来源为 configured replay。

sidecar metadata 至少包含：

```text
branch_replay_contract
predictor_family
predictor_config_hash
replay_implementation_version
functional_trace_hash
cold_or_warmup_policy
branch_ordinal_alignment_version
```

有 ROI 前 functional stream 时应先 warmup predictor；没有时显式标记 cold start，不能
用 oracle predictor snapshot 补齐。当前审计中 steady-state event mismatch 为 `0.002%`，
cold 部分为 `0.085%`，因此 cold/warm policy 不能省略。

## 7. 训练数据与状态 Cache

### 7.1 真实时间戳重放是训练数据的唯一正式来源

训练期 cache state 的正式来源是 raw gem5 trace，不是任何旧模型预测。构建流程为：

```text
records.micro + labels.micro
        |
        +-- join by core/thread/micro_seq
        |
        +-- derive issue_abs_tick and ready_abs_tick
        |
        +-- global stable sort of memory events
        |
        +-- replay the production CacheProxyEngine
        |
        +-- emit pre-access proxy features per memory UOP
        |
        +-- write immutable training sidecar/cache
```

排序 tie-break 必须固定并版本化，例如：

```text
event_tick
event_kind: fill before/after request according to declared policy
core_id
thread_id
micro_seq
```

### 7.2 训练 sidecar

为保持 8 卡随机训练吞吐，离线为每个 UOP 保存模型真正可见的 compact 字段：

- proxy hit level；
- 各层 pre-access position；
- set residency；
- miss kind；
- eviction summary；
- compact per-core/global pressure summary；
- `paddr_valid`；
- cache schema、uarch hash、order-policy hash。

常规 DataLoader 直接 mmap sidecar，不在线恢复完整 cache，也不从 trace 起点重复重放。

Branch replay 使用独立的 immutable sidecar。它按 per-core/per-thread branch ordinal 与
UOP index 对齐，存放第 6.10 节定义的 compact replay 字段和 contract metadata。它不
需要保存完整 predictor state 的逐窗口副本，也不需要按真实时间戳做跨核排序；同一核的
committed functional order 即为唯一 replay 顺序。DataLoader 必须将 GSS sidecar 和
branch sidecar 分别校验后再组合为模型输入。

Branch causal-history 字段必须在完整 per-core replay 流上一次生成，再投影到所有 UOP。
`BranchHistoryEncoder` 可以作用于 valid non-branch token，但每个 UOP 的字段只能聚合其
program-order prefix。当前/future branch 的 `replay_full_miss` 仍只能通过对应
`branch_mask` 和 `BranchReplayEncoder` 注入，不能直接回填到更早的 UOP。由于主干保留
非因果 joint-window attention，这不构成严格的 channel-level 因果隔离；第一版必须显式
承认 functional lookahead，并通过窗口截断/位移一致性实验检查模型是否错误前移 penalty。

### 7.3 Oracle 信息的隔离

下列信息可以用于构造 teacher-forced state，但不能作为显式模型字段：

- `issue_abs_tick`；
- `ready_abs_tick`；
- `commit_tick`；
- 全局 event ordinal。

gem5 `mispredicted` 不能用于构造 branch replay 输入或更新 replay state，只能在 replay
完成后作为 differential 标签。功能性的 `branch_taken`、target 和 next-PC 可以驱动
predictor update，因为训练和部署消费的是同一条 functional trace。

下列信息只允许作为辅助标签或审计指标，默认不参与 proxy transition：

- Ruby 真实 hit/path；
- Ruby evict/invalidate；
- hardware prefetch；
- 真实 MSHR depth；
- coherence transient state。

原因是部署端没有这些真实事件。若训练 state 消费它们而推理 state 不消费，就会产生
另一种更隐蔽的 train/inference skew。

### 7.4 Teacher-forcing gap 的处理

训练使用真实时间、推理使用预测时间会产生正常的 autoregressive exposure gap，但不
应通过旧 v29 rollout 构造训练输入来解决。第一版采用：

- 对接近同 tick 的跨核事件做小范围 order-jitter augmentation；
- 对少量 set state 做受控 dropout/corruption；
- 同时报告 true-ready-order、true-commit-order 两种 replay 的特征敏感度；
- 验证轻微 event swap 对最终 CPI 的影响；
- 推理时始终由当前模型预测时间在线递推，不读取旧模型轨迹。

此外必须在同一 trace 上记录 true-order teacher GSS 与 predicted-order serial GSS 的：

- per-access hit-level mismatch；
- set/tag/replacement-state divergence；
- 首次 divergence 的 step 和原因；
- divergence 后 CPI 误差增量；
- 随核心数变化的 mismatch 曲线。

该审计用于区分“模型 timing 本身错误”和“timing 错误进一步拉偏 GSS 状态”的闭环
放大。不得只用 teacher sidecar 对 Ruby 的命中率评价 GSS。

该 exposure gap 只适用于依赖跨核预测时间排序的 GSS。Branch replay 不依赖预测时间，
训练与推理应逐事件完全同源，不应为 branch replay 引入 teacher-forced/free-running
两套状态。

### 7.5 状态 checkpoint

训练 sidecar 一次性由真实时间戳顺序生成，不需要保存每个样本的完整状态。仅为构建
任务的断点续跑和推理 resume 保存：

- 稀疏周期性 canonical checkpoint；
- checkpoint 之间的 compact event/delta log；
- schema、uarch 和 order-policy hash。

禁止按每个训练窗口保存一份完整 20 到 55 MB 状态。

## 8. 单 Trace 多 GPU 语义

当前单 trace 多 GPU 模式会从同一轮 anchor 构造多个偏移窗口。引入 cache state 后，
后续 lane 不能简单把自己的起点直接套在同一个 canonical state 上，否则会漏掉 anchor
到该 lane 起点之间的 functional cache 更新。

推荐流程：

1. CPU canonical owner 从当前 anchor 状态开始；
2. 用一次连续 shadow preview 覆盖最深 lane 所需的 functional 范围；
3. 在每个 lane 起点记录特征切片或 touched-set checkpoint；
4. 将各 lane 的小型 feature tensor 分发到各 GPU；
5. GPU 并行 forward；
6. scheduler 按 serial/speculative/unconditional 合同消费预测；
7. CPU 只重放真正提交的 memory UOP；
8. rejected lane 的 shadow 数据全部丢弃。

这使 preview 复杂度更接近：

```text
K + (D - 1) * shift
```

而不是：

```text
D * K + D copies of full cache state
```

### 8.1 状态所有权

- Canonical state 放在 CPU coordinator；
- private L1/L2 按 core 分区；
- shared LLC 按 bank 分区；
- GPU 不保存完整 tag state；
- 多 trace 并行时每条 trace 独立；
- 单 trace 内第一版无需全局 mutex，由 coordinator 串行 commit 即可。

### 8.2 对多卡扩展性的影响

Cache update 本身是短 CPU 串行段，不会改变 GPU 模型 forward 的并行性。主要风险是：

- preview 完成前 GPU 必须等待 feature tensor；
- 过多 CPU/GPU barrier；
- 每个 lane 复制完整状态；
- process worker 间传输大状态；
- 为了得到精确 cross-core issue order 做第二次模型 forward。

第一版明确禁止后四种实现。

Branch replay 不进入上述 transactional 协调路径。它可以在发起多 GPU window forward
前一次性顺序 replay，并按 UOP index 切片给各 lane；重叠窗口读取同一 immutable 结果，
不会重复更新 predictor state，也不存在 rejected lane rollback。

## 9. 状态大小与吞吐开销

### 9.1 当前 TSim LRU 实现实测

在本机使用 TSim 当前 `BankedSetAssocLRU`，配置为：

- C32；
- 每核 32 KiB L1D、8-way；
- 每核 1 MiB L2、8-way；
- 共享 64 MiB LLC、8 banks、16-way；
- 500 万次随机三级 cache event。

实测结果：

| 指标 | 结果 |
|---|---:|
| 三级 cache event 延迟 | 约 0.778 microsecond/event |
| 单 CPU 处理能力 | 约 1.285 million events/s |
| 最大 RSS | 约 55.6 MB |

该结果只覆盖 tag/LRU 主体，不代表完整 MESI unordered-map、真实 MSHR 或 DRAM
状态成本。

### 9.2 对当前 C32 推理的估算

当前 Redis-heldout C32 serial free-running 大致为：

| 阶段 | 每 forward |
|---|---:|
| Context build | 约 20.4 ms |
| Model | 约 40.8 ms |
| Scheduler | 约 0.52 ms |
| Total step | 约 62.5 ms |

按 8192 input token/forward 估算：

| Memory-token 比例 | Cache update 估算 | 相对 62.5 ms step |
|---|---:|---:|
| 约 5% | 约 0.3 到 0.6 ms | 通常低于 1% |
| 约 30% | 约 1.9 到 3.0 ms | 约 3% 到 5% |

额外 8 个 float feature 在 C32、K=256 时原始 tensor 约 0.25 MB/forward，
传输量不是主要瓶颈。

### 9.3 生产实现目标

TSim 当前使用 `std::list` 维护每个 set 的替换顺序，适合验证，不适合最终热路径。
生产实现推荐：

- flat tag array；
- compact valid bits；
- L1 LRU age/order bits；
- L2/LLC TreePLRU bits；
- touched-set overlay；
- batched C++ API；
- 无 per-access Python object。

预计纯 tag/replacement 状态可压缩到约 20 到 30 MB/trace，并比当前 list 版本更快。

如果后续加入 coherence，不应照搬 TSim 中无界的
`unordered_map<line, unordered_set<core>>`。C32 可以使用 compact owner/state 和
32-bit sharer mask，并对状态范围设置明确上界。

## 10. 微架构泛化实验

### 10.1 Cache proxy 能直接帮助的维度

- L1/L2/LLC size；
- associativity；
- LLC bank 数和 set mapping；
- 核心数变化引起的 shared LLC footprint；
- capacity/conflict miss threshold；
- private/shared cache 压力。

### 10.2 仍需要标签变化的维度

- cache hit latency；
- ROB/IQ/LQ/SQ size；
- MSHR 数；
- NoC bandwidth/latency；
- DRAM channels、queue、timing；
- branch miss 的 recovery penalty 与流水线可见性；
- issue/commit width。

已支持 predictor family 内的表大小、counter、BTB、RAS 和 indirect 配置变化由
configured replay 显式处理，不要求 neural head 从 uarch scalar 猜 miss incidence。
但是不同 predictor 对 fetch/redirect/recovery timing 的影响仍需多配置 timing 标签；未
实现的 predictor family（当前包括标准 TAGE）不能靠修改配置名自动泛化。

### 10.3 理论收益与预期边界

若 `X` 是 v29 输入、`Z` 是 GSS/branch replay 的额外机制状态、`Y` 是真实 retirement
timing，则 Bayes-optimal 风险满足：

```text
E[Var(Y | X, Z)] <= E[Var(Y | X)]
```

成立的工程前提是 v30 保留完整 `X` 路径、允许模型把新分支权重学为零，并且 `Z` 在
训练与部署具有相同语义。它只说明最佳可达到误差不会增加，不保证有限样本 SGD 得到的
test error 更低。实际收益的置信度分层如下：

| 对象 | 预期 | 主要原因或风险 |
|---|---|---|
| Branch PMU | 高置信度显著改善 | configured replay 已完成逐事件验证 |
| Branch-heavy timing | 中高概率改善 | miss incidence 近乎确定，penalty 仍由 timing head 学习 |
| Redis heldout timing | 中等概率改善 | 主要依赖 GSS；branch 不是当前 42% CPI 误差主因 |
| Memory-random | 可能改善 | miss incidence 更明确，但 MLP/DRAM latency 仍是学习问题 |
| Memory-seq/compute | 应保持非退化 | access gating 和零初始化；作为 shortcut 检查 |
| 核心数泛化 | 有潜力但风险较高 | shared state 更真实，同时 predicted-order skew 随核数增加 |
| 新微架构 | 机制泛化改善 | recovery/cache latency 映射仍需要多配置 timing 标签 |

GSS 与 branch replay 解决的是长期 cache/predictor state，不解决超过 256 UOP 的任意
dependency、ILP 或 ROB 历史。因此第一版保持 `K=256` 有利于公平归因，不应把窗口扩到
512 与机制输入同时修改；若 v30 通过后仍存在长依赖误差，再单独做 K=256/512 消融。

建议采集小规模、多配置正交训练集，而不是只在当前单一配置训练：

- L2：512 KiB、1 MiB、2 MiB；
- LLC：32 MiB、64 MiB、128 MiB；
- LLC assoc：至少两档；
- LLC banks：至少两档；
- 核心数：覆盖 C1/C4/C8/C16/C32，并保留 leave-core-count-out；
- cache latency 与 capacity 分开变化，避免模型把容量直接当延迟 ID。

正式结论使用 leave-one-uarch-out，而不是只使用 leave-workload-out。

## 11. 实施顺序

### Phase 0：语义与 differential audit

- 固化现有 v29、Redis base/heldout 和 mechanism cube 基线；
- 从 gem5 config 自动导出 cache mapping/replacement policy；
- 修正 L2/LLC TreePLRU 语义；
- 用 functional event stream 对账 proxy 与 Ruby；
- 分别报告 event-level hit-path accuracy 和 aggregate miss count；
- 固化 configured branch replay 的 event/window/cold/steady 基线及 sidecar contract；
- 不修改模型。

### Phase 1：Serial D-side Cache Proxy

- 实现 compact C++ L1D/L2/LLC state；
- 实现 canonical state；
- 实现 touched-set shadow overlay；
- 实现实际 committed-prefix replay；
- 增加 state save/resume 和 policy hash；
- 增加 state-update p50/p95/p99 统计。

### Phase 2：模型 Probe

- 使用真实时间戳 sidecar；
- 可以冻结旧 v29 主干做结构 probe，但旧模型不得参与 memory order/state 构造；
- 比较冻结的 v29 baseline 与完整 v30 candidate；
- 保持原 single timing head；
- 删除 neural branch head/loss，以 configured replay 同时提供 branch-token 输入和 PMU；
- 新增 encoder 最终投影零初始化，并验证 v30/v29 timing forward 等价；
- 第一版 GSS 只做 access encoder + memory-token FiLM，不修改 cross-core gate；
- 测试辅助监督 weight 为 0 和低权重两档；
- 先 one-step，再 serial free-running；
- 不先跑完整 60k 主训练。

### Phase 3：全量时间戳 Sidecar 与完整训练

- 为训练集建立 immutable timestamp-replay feature cache；
- 建立独立 immutable branch-replay sidecar，并验证 branch ordinal/UOP 对齐；
- 验证 `issue_abs_tick/ready_abs_tick/commit_tick` 语义和 join coverage；
- 使用 coverage-first sampler；
- 训练相同步数和相同数据覆盖的公平对照；
- 检查 Redis heldout 改善是否伴随其他 workload 退化；
- 执行 leave-workload、leave-seed 和 leave-uarch 验证。

Branch 与 GSS 至少保留以下可归因消融：

| 方案 | 新增机制输入 | Branch head/loss | 目的 |
|---|---|---|---|
| B0：最佳 v29 | 无 | 保留 | 正式基线 |
| B1：head removal | 无 | 删除 | 隔离辅助监督影响 |
| B2：current-event replay | 当前 branch replay event | 删除 | 测 miss incidence 输入收益 |
| B3：branch causal-history | B2 + prefix history | 删除 | 测 penalty 归属和跨窗口历史 |
| G1：GSS access-only | per-access GSS | 与所选 branch 基线一致 | 测 proxy 本体 |
| G2：GSS FiLM | G1 + per-core memory-token FiLM | 同左 | 测 core memory context |
| G3：GSS cross-gate | G2 + 有界 GSS gate delta | 同左 | 后续高风险消融 |
| BG：正式候选 | 最佳 branch + 最佳 GSS | 删除 | 最终联合方案 |

B2/B3 必须同时检查 branch-heavy、Redis heldout、compute 和 memory workload 的
CPI/window 误差，并报告 cold/steady 和窗口边界分项。不能因为 branch PMU 已接近精确，
就直接推断 timing 一定改善；当前 neural head 原本没有进入 gap 路径，timing 收益必须由
B0/B1/B2/B3 重新训练对照证明。G1/G2 通过前不运行 G3；BG 只能组合已经分别通过
非退化验收的 branch 与 GSS 版本。

### Phase 4：单 Trace 多 GPU

- 接入连续 shadow preview；
- 支持 speculative lane rollback；
- 支持 unconditional lattice 的 exact-once canonical update；
- 测量 D=2/4/8 和 shift=32/64/128/256；
- 比较 serial 与多 GPU 的状态一致性和 CPI 偏差。

### Phase 5：可选扩展

只有前四阶段通过后才考虑：

- I-cache；
- compact MESI stable state；
- TLB functional proxy；
- predicted-time MSHR；
- DRAM row-buffer/FR-FCFS virtual state。

这些扩展必须独立消融，不能一次性与 cache proxy 合并。

## 12. 验收标准

### 12.1 正确性

- 不读取 oracle commit tick、真实 hit level 或真实 queue state；
- 未提交 token 不修改 canonical state；
- rejected speculative lane 对 canonical state 零影响；
- resume 前后 cache state 与预测结果一致；
- serial 下相同 functional order 可重复；
- core-slot permutation 后结果等价置换；
- 缺失 paddr 不污染 shared LLC state；
- branch replay 在读取 gem5 `mispredicted` 前完成，逐事件保持 predict-before-update；
- branch sidecar 的 PC/kind/taken/target、branch ordinal 和 UOP index 全量对齐；
- 训练/推理 predictor config hash、replay version 和 cold/warm policy 完全一致；
- window 边界和多 GPU lane 不重置或重复更新 predictor state；
- 不支持的 predictor family 硬失败，不回退 neural head；
- 新增 encoder 为零时，v30 与 B0 的 timing tensor 在数值容差内一致；
- branch causal-history 在随机窗口边界与从 trace 起点连续 replay 的结果一致；
- true-order/predicted-order GSS divergence 有逐事件记录，不能只报告 aggregate hit rate。

### 12.2 精度

- Redis heldout C4/C8/C16/C32 同方向改善；
- Redis base、memory-random、memory-seq、compute/branch 不系统退化；
- 逐 workload、逐 core 报告 signed CPI error；
- proxy hit-path 对 Ruby 的 event-level accuracy 明显高于现有 LRU shadow；
- leave-one-uarch-out 优于只提供 uarch scalar feature 的基线；
- 改善不能依赖 workload ID、core ID 或地址 identity shortcut；
- configured replay 的 branch PMU 保持现有 event F1、window exact 和 count/rate 精度；
- B1/B2/B3 相比 B0 分别报告 timing 改善或退化，不能用 branch PMU 精度替代 CPI 验证；
- B1/B2/B3 和 G1/G2/G3 分项报告，联合 BG 不能替代单机制归因；
- GSS access-only/FiLM 通过前，不把 GSS 接入全 token cross-core gate。

### 12.3 性能

- Serial `cache_state_update_p95 < 2 ms/step`；
- Serial 总吞吐下降目标小于 3%；
- 单 trace 多 GPU 总吞吐下降目标小于 5%；
- canonical state 有固定或可审计上界；
- 不复制 `state_size * GPU_count`；
- 不把完整 cache tag/state tensor 传入 GPU；
- state feature builder 不使用 per-access Python object；
- branch sidecar 读取与 `BranchReplayEncoder`/`BranchHistoryEncoder` 的吞吐开销单独
  报告；目标是不引入 rollout-time predictor 状态同步或 GPU 间通信。

## 13. 明确的非目标

- 不在 TCSim 内运行完整 gem5/Ruby；
- 不声称 functional commit order 等价于真实 issue/fill order；
- 不把 TSim 当前 LRU shadow 直接称为真实 gem5 cache；
- 不用 cache proxy 替代 OOO/MLP/criticality 学习；
- 不用 256/512 窗口大小代替长期状态；
- 不因为 proxy 参数化就跳过多微架构训练；
- 不在第一阶段实现 path/MSHR/TLB/coherence/DRAM 的完整 timing 状态机；
- 不训练 neural branch head 去复刻已支持的 configured predictor；
- 不把 gem5 `mispredicted`、真实 recovery latency 或 wrong-path oracle 当作输入；
- 不把 correct-path replay 宣称为 cycle-exact wrong-path predictor simulation。

## 14. 最终推荐架构

```text
Functional trace + target uarch profile
                 |
        +--------+---------------------------+
        |                                    |
        v                                    v
v30 Global Shared-System          configured branch replay
canonical cache state             per-core functional order
        |                                    |
transactional shadow preview       event + per-UOP prefix features
        |                                    |
compact memory/core features       replayed branch PMU
        |                                    |
        +----------------+-------------------+
                         |
                         v
      v29 model skeleton: full-QKVR trunk
      (memory/branch gated token inputs)
                 |
       original single timing head
                 |
      predicted retirement gap cycle
                 |
                 v
          global-time scheduler
                 |
       actual committed memory prefix
                 |
                 v
       canonical cache-state replay
```

这套 v30 架构把可以确定计算的 cache mapping/replacement 交给 GSS 显式状态机，把不能从
functional trace 唯一确定的 OOO exposure、MLP、NoC/DRAM timing 和最终 commit
行为交给模型；分支 miss incidence 则交给同源 configured replay，模型只学习它的
timing 可见性。它比纯模型同时记忆长期 cache 历史和分支预测器更容易泛化，也比在推理
中嵌入完整 gem5 更符合 TCSim 的吞吐目标。
