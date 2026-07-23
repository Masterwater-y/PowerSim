# functional-only 多核 timing：Temporal Graph / Resident Event 方案与系统审查

日期：2026-07-10

关联材料：

- `docs/v27_catastrophic_cpi_root_cause_and_fix_plan.md`
- `docs/eval_v27_ss_tw5000_20k_seedB_c04_c08_c16_c32_summary.md`
- 当前部署循环：`eval/eval_quota_cycles.py`
- 当前 shared-state proxy：`model/shared_state.py`
- 当前结构化模型：`model/v26_kvqr.py`

本文整理 fixed chunk、快慢核时间错位、slow-resident 重用等讨论，并从正确性、并行性、吞吐、
训练稳定性和可辨识性重新审查方案。本文细化并部分修正前一份根因报告中“fixed-chunk event-driven”
的高层描述。

若当前目标是先做可验证的低复杂度 MVP，应优先阅读
`docs/v27_fixed_chunk_resident_mvp_plan.md`；本文的 temporal graph、event projection 和严格状态语义属于
后续扩展与风险审查，不是第一轮必须实现的内容。

## 1. 最终架构判断

以下三个简单方案都不能直接采用：

1. **每核同步取一个固定 K-UOP chunk**：消除了长度闭环，但快慢核 chunk 不代表相同时间，跨核上下文错位。
2. **根据预测 CPI 给每核分配不同 UOP 数**：尝试对齐时间，但恢复
   `predicted CPI -> next input length -> predicted CPI` 高增益闭环。
3. **每推进一个 fast chunk，就把所有 resident slow UOP 重新送进当前 8 层 QKVR**：语义上仍不完整，
   GPU forward 次数和重复编码成本也不可接受。

推荐的语义是：

```text
immutable functional chunks / events
        |
        v
static functional encoder（每个 UOP/chunk 只编码一次）
        |
        v
base chunk duration + sub-event interval + uncertainty
        |
        v
per-core prefix timing
        |
        v
line/resource-centric temporal graph
        |
        v
总计 2--3 次 temporal passes（1 次 base + 1--2 次同步 Jacobi refinement）
        |
        v
少量 atomic/lock/hot-line 子图的合法次序投影
        |
        v
additive cycles / makespan / exactly-once event accounting
```

slow chunk 不被复制成多个执行实体。它是一个 unique node；若它与 `F0...F19` 都可能重叠，图中产生
多条 query/relation edge。**引用可以多次，实体生成、cycle/state accounting 和正式 state commit 只能
一次；primitive target 在 rollout graph 中归一为单位权重，但可以参与多个已归一化的 prefix、relation 和
trajectory loss。**

由于当前用途是离线完整 functional trace timing 重建，生产主路径优先采用 whole-trace 或 blockwise
同步 temporal-graph solver。顺序 async frontier/event queue 仍有两个用途：

- 小 trace 的确定性 reference implementation；
- atomic、lock、barrier、极热 line 等少量关键冲突子图的最终 constraint projection。

## 2. 能保证什么，不能保证什么

### 2.1 可以设计成硬不变量的正确性

- chunk 边界完全由 functional cursor/functional landmark 决定；
- 每个 UOP 恰好属于一个 accounting chunk；
- per-core boundary time 单调，chunk cycles 可加；
- 每个 event ID 只生成一次，cycle/state accounting 只计一次，正式 state 最多 commit 一次；
- resident 的重复引用不改变正式 state；
- batch size、worker 数、遍历顺序只影响执行效率，不影响语义；
- 已提交 state 不受后续 refinement 回写；
- 显式 functional happens-before 约束不会被违反。

### 2.2 在当前输入约束下不能保证的正确性

没有部署真实时间戳，就不能观测 `predicted_time - true_time`，因而不能在线校正共同 scale bias。
更重要的是，当前 functional schema 不足以唯一恢复：

- load/store 的 issue、cache request、response、store-visible 时间；
- CAS 成功/失败和 atomic functional total order；
- load read-from 哪个 writer；
- barrier 对象、参与者、generation；
- spin load 哪一次看见 producer；
- join/start/exit 的精确 functional dependency。

因此应区分四个“正确性层级”：

| 层级 | 当前能否保证 | 含义 |
|---|---|---|
| 给定 trace 的 syntactic identity | 能 | timing 模型不增删给定 UOP/branch/outcome token；不代表新 schedule 与原 outcome 语义一致 |
| cycle accounting / exactly-once | 能 | 无丢 UOP、重复 cycle、重复 event commit |
| 显式 HB/同步约束 | 取决于 trace schema | 有 functional 注释时可保证相对约束 |
| 真实 O3/coherence/atomic 事件顺序 | 不能完全保证 | 缺少 issue/visibility/order 可观测量，只能预测分布或合法假设 |

默认产品合同应写成 **given-path timing reconstruction**，不是 cycle-exact counterfactual simulator。
如果目标是跨 uarch 改变同步相对速度后仍得到正确动态路径，固定 committed trace 本身可能无效：producer 已经
被预测为写入 flag 后，trace 中 consumer 仍可能保留原运行的多轮 spin；CAS 胜者也可能与新时间顺序矛盾。
这需要 functional executor 或额外 outcome/HB 注释，不是仅靠 timing head 能修复的问题。

## 3. 为什么 fixed chunk 仍然需要 resident/temporal graph

假设每核固定取 64 UOP：

```text
fast CPI = 1   -> chunk duration 约 64 cycles
slow CPI = 20  -> chunk duration 约 1280 cycles
```

slow 一个 chunk 期间，fast 可能完成约 20 个 chunk。因此不能把 `slow S0` 只与 `fast F0` 放在一起：

```text
S0: [------------------------------------------------]
F0: [--]
F1:     [--]
F2:         [--]
...
F19:                                            [--]
```

正确语义是：`S0` 的时间区间及其内部未决事件被 `F0...F19` 按需引用。不能把整个 `S0` 都看成
与每个 F 同时发生；例如 `S0` 后 80% 的 store 不应影响早期 F。必须至少预测 subchunk/memory landmark
的相对时间或区间。

fixed chunk 的作用只是：

- 固定 functional 取数和样本主键；
- 消除 true-time length leakage；
- 让输出不能改变下一输入的 UOP 数；
- 提供严格可加的 cycle accounting 单元。

它不是多核时间窗口，也不能成为流水线 fence。

## 4. 推荐的数据与执行层级

### 4.1 两级 chunk + event landmark

为了兼顾时序分辨率和 GPU 吞吐，使用两级层次：

1. **encoding block**：长度记为 `L_enc`，建议从 256/512 UOP 起步，用于一次性 static local encoding；
2. **timing microchunk**：建议 sweep 32/64/128 UOP，每个 encoding block 一次输出多个 additive delta；
3. **event landmark**：load/store/atomic/fence/sync、memory burst 边界以及固定 subchunk boundary。

边界只能依赖 functional 内容：

- 最大 K；
- macro/超长 microcoded macro 的明确规则；
- atomic、fence、显式 barrier/join；
- trace 尾部和 thread lifecycle。

不能因为预测 CPI 或 uncertainty 改变基础 microchunk 边界。若需要更精细处理，使用预先存在的 subchunk
输出或增加固定 refinement，避免 `uncertainty -> chunk size -> input distribution` 新闭环。

### 4.2 static/dynamic 分离是硬要求

static branch 只允许：

- opcode、PC relation、register dependency；
- branch direction/target/history；
- load/store/atomic/fence 类型；
- address equality、line/page/set/bank/channel 等配置可派生 relation；
- functional cursor、thread/sync/HB 注释。

dynamic branch 承载：

- predicted virtual time / relative lag；
- previous duration、uncertainty、iteration ID；
- committed resource state；
- provisional overlap/relation summary；
- resident progress 和 state version。

当前模型把 shared-state UOP fields、core/global condition 在 8 层 attention 之前写入 token，因而其 layer KV
随动态 state 改变，不能安全复用。新结构必须先做纯 static encoder，再用小型 timing query / sparse
relation head 注入动态信息；不能直接给当前 QKVR 增加 resident cache。

### 4.3 唯一实体和状态

建议主键：

```text
chunk_id = (trace_id, core_id, start_uop, end_uop)
event_id = (trace_id, core_id, trace_uop_index, event_kind)
edge_id  = (source_event_id, target_event_id, relation_kind)
edge_message_version = (edge_id, refinement_epoch, state_version)
```

relation 主标签按 logical `edge_id` 归一一次；若对每轮 message 做 deep supervision，必须显式除以参与轮数，
不能把同一物理关系当作多份样本。

每个 chunk 至少保存：

```text
static_ref / typed summaries
functional [start_uop, end_uop)
predicted start/end interval
subchunk/event offsets and uncertainty
past/inflight/future event masks
resident/state version
status = unseen | encoded | proposed | resident | retired
```

每个 event 状态只能单调变化：

```text
unseen -> proposed/scheduled -> committed
```

refinement 可以替换未提交 proposal，不能把 committed event 退回或重复提交。

## 5. 主推的 parallel temporal-graph solver

设总 UOP 数为 `U`，固定 microchunk 数 `N ~= U/K`，memory/sync event 数为 `M`。统一记：

```text
R_refine = base 之后的 refinement 次数，首轮建议 1--2
P_total  = 1 + R_refine，首轮总 temporal passes 为 2--3
```

### 5.1 Iteration 0：批量 base timing

一次性或流式批量编码所有 functional blocks，输出：

- 每个 microchunk 的非负 boundary-cycle increment；
- 与 commit accounting 对齐的 subchunk boundary 单调 gap；
- memory request/complete/store-visible 等 event landmark 的 interval；这些 interval 可以跨 accounting chunk，
  也不要求按 UOP/commit 顺序单调，只服从 dependency、memory model、fence 和 atomic/HB 约束；
- uncertainty/quantile；
- typed compute/memory/branch/sync summary。

禁止继续用一个 scalar CPI 对窗口内所有 memory event 匀速插值。输出参数化必须支持真实 label 中可能出现的
零增量尾块或同 tick commit，不能把“严格大于零”写成数据无法满足的硬假设。

### 5.2 Per-core prefix scan

对每核 chunk delta 做 exclusive/inclusive prefix scan，得到 predicted start/end。scan 在 GPU 上是 O(N)
work 的批量操作，不需要 N 次 Python event-loop。

若显式处理 barrier/join：

```text
continuation_start_c >= max(participant_arrival) + predicted_release_cost
```

它是相对因果约束，不是真实时钟校正。必须先定义 duration 是否已经包含 wait；若 boundary delta 已包含
barrier wait，又在 projection 中加一次，就会双计。更清晰的长期定义是：

```text
finish = max(previous_finish, functional_release) + service_time
```

并分别验证 service、explicit wait 和 total 的守恒。

还必须定义各核初始相位。只有共同 ROI/WORKBEGIN 或 functional start/sync marker 时，`t=0` 才有统一含义。
若 per-core trace 起点并非同时、又没有 functional lifecycle/HB 注释，初始 offset 同样不可识别，不能默认为
精确的全零；应作为显式假设、预测分布或 OOD 状态报告。

初始 cache/TLB/predictor/resource belief 也需明确合同：cold reset、functional warmup replay、learned prior 或
OOD fallback。functional warmup 的跨核 replay order 仍受相同 timing 不可辨识限制，不能把 teacher-time warm
state 偷渡到部署。

### 5.3 Functional candidate graph + soft temporal gate

候选关系首先由 functional 信息构造，不能由真实 tick 或 true overlap 裁剪：

- same physical line / shared mapping；
- cache set / bank / DRAM channel；
- read/write/atomic relation；
- register/data dependency；
- explicit sync/HB；
- bounded local O3 halo/carry；
- 跨核 same-resource incidence 使用全局/分段索引，不能只看“其他核后续固定若干 chunks”，否则会漏掉
  1:1000 快慢比下相距很远但时间重叠的事件。

predicted interval 只用于连续的 soft-overlap/gating 和 past/overlap/future 标记。若用 hard predicted overlap 决定
edge 是否存在，轻微时钟误差会突然删除真正关系并产生 graph churn。

future functional lookahead 可以作为离线预测信息，但绝不能进入 committed state。若未来要支持在线/streaming，
还必须把 lookahead 限制纳入产品合同。

future relation message 只能影响尚未完成的 service、未提交 event 或 unfinished suffix。任何 refinement 都不得
修改 committed/past state，也不得把 proposal 移到 committed watermark 之前。

离线可用真实 event timing 计算 `true-overlap candidate recall` 审计 resource index/aggregate 是否漏关系，
但该真值不能参与部署 graph 构造或训练 candidate selection。

### 5.4 Line/resource-centric aggregation

只建 same-line pair 不足以表达 capacity/bandwidth：至少还需 set、bank/channel、sync object 等 resource。
另一方面，hot line 上 m 个事件不能显式建 O(m^2) pair：

- 按 `(resource_id, predicted interval)` 做 segmented sort/range aggregation；
- 每个 target 保留少量 exact neighbors；
- 其余用 bucket/segment summary；
- summary 包含 R/W/atomic count、occupancy、32-core bitmap、sharer/owner belief；
- atomic/lock 不可被无序平均，单独进入关键子图 projection。

context budget 溢出不能静默截断。必须产生 aggregate token 并报告 overflow；否则 false-sharing/hot-line 正是
最先丢信息的 workload。

resource 模型还必须满足容量守恒。若模型对每对重叠请求分别增加完整 penalty，可能重复计算同一份拥塞，
产生超过或低于物理资源服务率的结果。更稳定的表达是：

- event 预测 arrival、service demand、completion/visibility belief；
- line/set/bank/channel 是具有容量的 resource node；
- 串行资源在上一轮 order 固定后使用 segmented max-plus scan：

```text
finish_j = max(arrival_j, finish_{j-1}) + service_j
```

- 多 server/带宽资源使用容量约束的 queue/occupancy aggregate；
- learned head 预测难以解析的 service/residual，而不是完全替代资源守恒。

resource order 只能读取上一轮 snapshot，并在一轮内冻结；近同时事件使用 stable bucket、hysteresis 或
set-valued tie。训练默认不穿过离散 sort 求伪梯度，另行监控 order flip；需要可微时只对 soft gate/aggregate
反传。

还必须避免 base 与 resource wait 双计。只能选择并验证一种合同：

```text
A. base 预测 service-only，resource solver 显式增加 wait
   -> 需要可辨识的 service/wait label 或可信解析模型

B. base 预测 observed total，resource graph 只产生 bounded refinement/context
   -> 不能再把解析 queue wait 直接相加
```

当前数据没有可靠 service/wait 分解，MVP 应采用 B；A 只能在补充 event/service label 并通过守恒测试后启用。

### 5.5 固定轮同步 refinement

每轮统一执行：

```text
predicted times^r
 -> temporal/resource graph^r
 -> aggregate context^r
 -> all chunks simultaneous update
 -> damping / bounded residual
 -> predicted times^(r+1)
```

所有 node 只能读取上一轮快照，禁止按处理顺序 in-place 更新。这样 slow 会感知多个 fast 对其产生的反向
带宽/coherence 影响，同时避免“每来一个 fast 就重算 slow”的 Gauss-Seidel 顺序依赖。

slow node 在多个 target 中出现只意味着多条 unique edge；duration/resident fanout 不应直接改变其主监督次数。
固定 `R_refine=1--2`、`P_total=2--3` 使吞吐和延迟可预测。不收敛时回退 bounded stateless
baseline，不无限迭代。

必须注意跨核因果传播半径：per-core prefix scan 每轮能全局传播本核 duration 变化，但普通 relation message
通常每轮只传播一个跨核 hop。长 lock convoy、barrier cascade、atomic chain 不应指望 2--3 轮 learned
message passing 自然传播；已知串行/HB 结构应使用 max-plus/constraint projection 一次全传。对软 contention
则报告 `R_refine=0/1/2/3/5` 饱和曲线。

refinement 还应近似 contraction：

- interaction residual 相对 stateless base 有界；
- overlap/queue contribution 做容量归一化；
- 使用 damping；
- 监控每轮 signed duration change 和 order flip；
- 三轮后仍发散时回退 base/保守 resource model，而非继续迭代。

fallback 判据只能使用部署可得的 finite check、normalized residual、order flip/graph churn，不能查看真实误差。
fallback scope 优先限制在 resource component/tile；触发后在该 slab/episode 单向保持 base/last-stable 状态，
避免 refined 与 base 来回 toggle。训练和完整评估必须覆盖并报告 fallback rate 及条件误差。

### 5.6 关键子图 projection

对有明确 functional outcome/HB 的 atomic、lock、barrier，以及极热 line 的歧义事件，从全局 interval 中抽取
小子图，用确定性 event queue、max-plus constraint 或有限 beam 得到一个合法次序。普通资源仍走并行聚合。

projection 不能在所有 learned refinement 结束后任意改变时间线便直接输出，否则 graph/context 已经过期。
硬 HB/max-plus 约束最好嵌入每轮 update 后；若 final critical projection 仍造成超过阈值的时间变化，则必须执行
一次有界的 `scan -> aggregate -> update -> re-project` consistency pass。最终一步必须是 hard projection，
并报告 post-projection residual，不能在它之后再做会破坏约束的无投影 learned update。

顺序 solver 应是少量投影器和 reference，不是每个 64-UOP chunk 都触发一次完整 GPU forward 的生产主循环。

## 6. Sequential resident reference 的正确语义

whole-trace Jacobi 主路径通过 prefix/segmented scan 派生每轮 state prefix，不执行逐事件 mutable commit；
其中 exactly-once 表示每个 unique event 对 accounting/resource scan 只贡献一次。`G@W`、rollback、正式
commit 主要属于 sequential reference 和 tiled/blockwise carry。

为了实现 golden/reference engine，维护：

```text
per-core functional cursor / ready time
one active resident band per core（可覆盖多个 accounting chunks、ROB halo 和 outstanding events）
global unique future-event queue
committed watermark W
versioned committed state G@W
speculative shadow state
```

slow chunk 不能整块提交。假设：

```text
S0: store A@100 -------- load B@900 -------- end@1000
F0: [0,80]
F1:       [80,160]
```

- 等 S0 完成才提交会让 F1 看不到 A；
- S0 一进入 resident 就全部提交会让 F0 看见未来 B；
- 每次 fast 查询都 replay S0 会重复更新。

因此 A、B 各自有 event ID 和 predicted time，只在其时刻正式提交一次。chunk 是 encoding/accounting 单元，
不是 shared-state commit 单元。早期已提交事件只留在 committed state；未发生 suffix 保持 resident。

预测 interval 并不是数学安全界。若一直等待所有 interval 不重叠，uncertainty 失配时会死锁。reference engine
必须有固定 refinement 上限和 deterministic fallback；歧义顺序进入 set-valued/soft state，不能无限等待。

watermark 的“可提交”只表示在当前模型和硬约束下可提交，不是真实时间安全证明。至少要求：

- 每个 active core 的下一个未决 accounting/event frontier 已物化；
- 所有显式 program-order/HB predecessor 已提交；
- 同 resource 的潜在更早候选已进入当前 conflict component；
- ambiguous same-resource order 先投影为合法顺序或写入 set-valued state；
- 按 projected timeline 提交全局前缀，watermark 单调；
- horizon 不足或版本变化时不提交并扩展/rebuild，但达到上限后使用确定 fallback，保证进展。

## 7. 跨 chunk O3 流水线问题

fixed chunk 不能隐式表示：

```text
chunk j 完全结束 -> chunk j+1 才 fetch/issue
```

真实 O3 中，下一 chunk 的指令可能已经进入 ROB，上一 chunk 的 miss 仍在飞；memory request order 也不等于
commit order。建议：

- accounting boundary 仍按 commit 顺序单调；
- local encoder 带 past/future halo 或 bounded recurrent pipeline carry；
- 保存 outstanding dependency/memory belief，而不是每个 chunk 清空；
- memory landmark 与 commit-boundary delta 分开预测；
- macro/atomic/fence 附近采用 functional-safe boundary；
- 对超长 microcoded macro 设计独立 exceptional representation。

每个 memory/atomic event 还需要明确阶段语义及目标 uarch 允许的偏序，例如：

```text
request -> permission/linearization -> complete -> commit/store-visible
```

per-core commit boundary 单调不表示 request 或 global visibility 按程序序单调；真正约束必须来自 register/data
dependency、目标 memory model、fence 和 atomic/HB。

另一个根本限制是：当前 label 的 `commit_tick` 不是 cache request/permission/store-visible 时间。即便 oracle
commit time 可用，也不能把按 commit_tick 排序的 load/store 当作真实 coherence order。若不能离线采集这些
event timing labels，则 event order 应作为 latent/set-valued performance proxy，而非“精确 MESI 重放”。

committed trace 还缺少 wrong-path fetch/load、speculative cache pollution，以及部分硬件 prefetch/DMA 行为。
即使 committed UOP 的 landmark timing 完美，这些隐藏活动仍可能改变 cache/resource state；只能由 deployable
functional predictor proxy 和 uncertainty 表达，不能宣称 exact MESI reconstruction。

## 8. 当前 functional schema 的 P0 缺口

仓库证据：

- `model/shared_state.py:485-544` 按 `start + scalar_cpi * local_i` 重放整窗事件；
- 不同窗口分别排序，可能先正式处理较晚事件，下一窗再出现更早事件；
- `model/shared_state.py:73-76,402-405` 把 atomic 当 store；
- `model/tokenizer.py:337-345` 明确没有独立 fence flag；
- `data/build_windows.py:52-65` 当前 schema 没有 sync object/epoch、atomic outcome/read-from；
- `data/build_windows.py:1687-1688` 主要按 `(thread_id,micro_seq)` 合并；若未来有 SMT、migration 或
  context switch，仅此不足以恢复物理 core execution order；
- 当前 raw functional record 有地址和 `is_atomic/is_serialize`，没有 load value、CAS outcome 或 barrier ID。

建议增加仍然属于 functional 信息的字段：

```text
address_space_id / shared_mapping_id
fence_kind
atomic_kind / atomic_outcome
atomic_functional_order_index
read_from_or_version_id
sync_kind / sync_object_id / sync_epoch
thread_start / exit / join_target
runtime_core_id / context_switch_epoch / per-core functional order
per-object atomic modification order / explicit functional HB（若 tracer 能提供）
```

普通 load/store 的“全局总序”若来自 commit/issue tick，仍是 timing 信息，不属于 functional 注释，禁止进入
部署输入。

若这些字段不能补齐，exact atomic/memory-consistency mode 必须拒绝启动或明确降级为 probabilistic contention
mode，不能用 core ID tie-break 伪装成真实 owner/order。

## 9. Additive label 和 timestamp taint

固定 chunk 样本主键先由 functional cursor 确定，然后才能查 timing label：

```text
B[c,0] = 明确定义的 ROI begin / 前一 commit boundary
B[c,j] = chunk j 最后一条 committed UOP 的 boundary tick
delta[c,j] = B[c,j] - B[c,j-1]
```

尾部、flush、最后 store visibility 或系统 drain 是否属于目标必须明确；若属于，需要 terminal/drain event。
每核必须验证：

```text
sum_j delta[c,j] == endpoint_span[c]
```

不能继续用 `(last_tick-first_tick)/n` 作为可加 chunk label。

functional chunk ID 必须先从 records 独立生成，再 left-join timing labels，并单独报告 label completeness。
不能因为某条 record 缺 timing label 就把它从 functional trace 删除；否则 label availability 本身会改变切片。

必须做 timestamp taint audit：删除或随机打乱 eval 输入中的真实 tick 后，以下内容应完全不变：

- chunk boundary/hash；
- candidate functional graph；
- runtime model input；
- resident selection；
- state transition 和最终 prediction。

真实 tick/true overlap 只能计算 label、loss 和离线诊断；不能用 true overlap 选择训练 context，否则仍是隐蔽
teacher leakage。

## 10. 训练与 rollout 审查

### 10.1 这里不是标准 DAgger oracle

离线 timing trace 只能提供原始真实执行轨迹上同一 functional chunk ID 的 label。模型诱导了不同 event order
或 shared state 后，数据集并不能回答这个 counterfactual state 本来应该耗时多少。因此这里更准确的名称是：

```text
self-rollout / scheduled-state robustness training
```

而不是声称有任意 counterfactual oracle 的标准 DAgger。它能缓解 exposure bias，不能创造缺失的 timing 信息。

每轮应使用冻结 policy 采集并保存：

- policy/model version；
- unique chunk/context/edge IDs；
- predicted intervals 和 uncertainty；
- resident exposure；
- state hash/version；
- source=`fully_predicted | corrupted | teacher_diagnostic`。

在任何 chunk/rollout 生成前，先按 `(workload, seed, raw trace, uarch/config)` 做
train/val/calibration/test group split；四组互斥。同一 raw trace 的不同 policy rollout 不能跨 split，
checkpoint 只能由完整 fully-predicted、无 oracle state 的 grouped-val rollout 选择，不能恢复 sample-level
random val loss 选模。

fully-predicted episode 的 clock、order 和 state transition 必须从头到尾保持 predicted，不能中途用 oracle
time/state 纠正，形成部署不存在的 hybrid state。`teacher_diagnostic` 默认不进主训练；若小比例蒸馏，必须
source-conditioned，并 mask 所有由 teacher state 派生的下游输入。

rollout cache 是 off-policy snapshot：dynamic state/topology 来自冻结旧 policy。训练时 static encoder 应由
当前参数重新计算，不缓存旧 activation/KV；hard topology 可按采集快照 stop-gradient，soft gate 是否重算需
成为明确 ablation，并保存 policy/state version。

### 10.2 repeated resident 的梯度偏置

即使 slow chunk 设置 `loss_mask=0`，多个 fast target 的 cross/relation loss 仍会反传到同一个 slow embedding：

```text
预测越慢 -> resident 越久 -> query edge 越多 -> context encoder 梯度越大
```

控制措施：

- 训练图中 node 不 clone，每条 unique relation edge 只出现一次；
- forward resource aggregation保留 raw count、occupancy、service demand 和 fanout；不能用 degree mean 把
  100 个请求平均成 1 个请求；
- target degree/source exposure 校正只作用于 optimizer sample weight/context-gradient，不改变 forward
  物理计数；
- 同时优化 natural deployment-exposure risk 与 unique-node debiased risk，对极端 exposure 使用 capped
  importance weight/分层采样并报告敏感性；
- 记录 residency decile、source gradient norm 和 edge fanout；
- 必要时两阶段训练或 stop-gradient resident static encoder；
- 跨 optimizer step 不复用旧参数生成的 stale KV。

slow 自身 duration/event label 只能一次，不能通过重复 label“加强慢核”。slow 的高下游影响应由 prefix、total、
makespan 或 influence-aware trajectory loss 表达。

### 10.3 长轨迹训练可行性和 corruption

35 万 chunk 的 whole-trace 图适合生成 rollout state 和做完整验证，不适合保留全部 static encoder activation
端到端反传。首版训练应采用：

- static encoder 预训练后阶段性 freeze，或每次 optimizer update 对当前 tile 重算；
- unique target tile/graph minibatch，context node 去重；
- truncated BPTT + random functional burn-in + 明确 state detach 点；
- 4/8/16/32 及长 horizon 的 sampled prefix loss；
- low-dimensional delta graph 两遍 recompute/checkpoint；
- 报告 train tokens/s、peak memory、edge activations、recompute/checkpoint ratio。

corrupted-state 不能独立随机修改 `pred_time/lag/progress/state`，制造部署不可达的矛盾状态。应扰动历史
delta/event order，再重算 clock、interval、soft gate 和 state。至少覆盖 common scale bias、per-core skew、
order flip、state drop、uncertainty miscalibration，并区分模型应恢复和应 fallback 的 corruption。

self-rollout 还可能收敛到“内部稳定但绝对时间 scale 错误”的 fixed point。checkpoint 必须同时满足带 teacher
label 的完整 rollout accuracy 和 perturbation stability；不能只凭 refinement residual 或 self-consistency 晋级。

### 10.4 Loss

建议按 unique target chunk 归一：

```text
L = L_additive_chunk_cycle
  + alpha * L_prefix_log_spaced_to_endpoint
  + beta  * L_per_core_endpoint_and_total_core_cycles
  + gamma * L_makespan_or_sync_critical_path
  + delta * L_relation_order_or_soft_overlap
  + eta   * L_uncertainty_proper_scoring
  + zeta  * L_refinement_stability
  + rho   * L_tail_CVaR_or_groupDRO
```

注意：

- local delta 使用 zero-safe `log1p(pred)-log1p(true)`，并加按 per-trace/ROI robust cycle scale 无量纲化的
  linear error；
- prefix 使用 `1,2,4,...,1024,...,endpoint`、随机 functional-progress endpoint 和 sync landmark；每个
  chunk 按参与 horizon 次数归一，避免 O(T^2) 重权；
- prefix/end/total/makespan 各按对应 true span 或固定 uarch cycle scale 无量纲化，再加权并报告 raw metric
  和 gradient norm；
- pair/relation loss 按实际 valid edge 数归一，不能随 core count 二次增长；
- uncertainty 以 NLL/quantile proper score 为主；若为了 graph budget 增加 width/fanout cost，必须带 coverage
  constraint/拉格朗日调节，避免系统性过窄；
- local chunk uncertainty 不能简单相加成 prefix interval；需另建 trajectory/common-scale 或直接预测
  prefix、cross-core lag/end-time quantile，并防 quantile crossing；
- graph topology 通常 stop-gradient，时间 gate 用连续 soft weight；
- absolute additive/prefix/ROI supervision 必须保留，否则所有时间乘同一 scale 仍可能满足相对顺序；
- `L_refinement_stability` 只惩罚超阈值 amplification/non-contraction、order flip/churn，不能惩罚所有
  iteration change，否则会鼓励 interaction residual=0；
- relation/order loss 仅在有真实 issue/visibility label 或显式 functional HB/outcome 时启用；unknown/tie
  必须 mask/set-valued，不能用 commit tick/core ID 造标签；
- makespan/critical-path loss 只在共同 ROI、thread lifecycle 和 drain 合同完整时启用；
- CVaR/groupDRO 按 workload x core-count x phase/mechanism 定义 group，同时报告 natural UOP-weighted 和
  hard-group risk，并做 importance correction；
- aggregate CPI、total core-cycles 和 makespan 是不同目标，必须分开报告。

### 10.5 部分可观测性和误差下界

同一或近似 functional observable state 在不同 warmup、replacement seed、DRAM seed 下可能具有不同 timing。
相同 observable history 的 paired repeats 可估经验不可约方差；nearest-state conditional label spread 只是一项
依赖表示和距离度量的 aliasing 诊断，不应直接称为严格 Bayes floor。若方差高：

- 扩展允许的 functional history/belief state；
- 输出分布/quantile；
- 独立 calibration split；
- 将不可约误差计入置信度和部署降级；
- 不要把它解释为继续扩大模型即可解决。

隐藏 warmup/replacement/DRAM seed 只有在产品输入中确实不可得时才归入 aleatoric uncertainty；若能提供
config/initial-state signature，应先作为允许输入。calibrator 只能使用独立 calibration split 和允许的
functional/uarch/core-count 信息，禁止按 test workload 真值事后缩放；分别报告 seen/unseen workload、seed、
core-count 下 local/prefix/lag 的 50/90/99% coverage、sharpness 和 ECE。

## 11. 仍然存在的反馈环

fixed functional chunks 只消除 output-to-input-length 直接通道，不消除所有 timing feedback：

| 剩余反馈 | 风险 | 控制 |
|---|---|---|
| duration -> temporal overlap/order -> state -> duration | 物理上必要，但可能发散 | 固定轮 Jacobi、damping、bounded residual、rollout |
| duration -> soft overlap mass -> duration | fixed incidence 下“越慢 overlap 越重” | capacity norm、bounded residual、exposure-aware training |
| duration -> materialized resident references -> duration | sequential/sparse materialization 下“越慢引用越多” | logical-edge dedup、global resource aggregate |
| uncertainty -> interval 变宽 -> edge/soft mass 变多 -> duration/uncertainty | 图稠密化和全局 contention 假象 | proper scoring、coverage-constrained budget cost、aggregate |
| pred order -> hard owner/sharer -> next pred order | 离散翻转、振荡 | set-valued state、soft order、关键子图 projection |
| arrival -> hard resource sort -> queue finish -> arrival | 近同时事件在轮间翻转 | stable bucket、hysteresis、snapshot order、关键 component projection |
| adaptive K -> input distribution -> uncertainty | 重现长度闭环 | 基础 K functional-only；只增加固定 refinement |
| suffix state change -> resident duration | stale proposal 或反复重算 | version、只更新未提交 tail、固定 R |

应监控：

- normalized log-duration/relative-lag 上带 epsilon 的 Jacobi contraction ratio，以及 non-convergent
  component/fallback rate；
- overlap graph Jaccard / edge churn；
- line-order flip rate；
- duration perturbation 1/8/32/128-step amplification；
- uncertainty coverage/sharpness；
- residency/fanout decile error。

## 12. 并行正确性

并行推理不能改变模拟结果。建议：

1. 同一 epoch 的 proposals 全部读取 immutable state snapshot；
2. dynamic update 使用同步 Jacobi，不使用 worker-dependent Gauss-Seidel；
3. state update 按 resource/conflict component 分区；
4. 每条 line/resource 有 version，proposal commit 前检查版本；
5. speculative state 用 copy-on-write delta log，失败可 rollback；
6. 不同 run 天然隔离，用 block-diagonal batch；
7. 同一 run 只有无冲突 antichain 或同一 Jacobi epoch 可并行；
8. tie/order 规则确定且可复现，但歧义不能让 core ID 成为物理 owner；
9. batching、tile size、GPU 数变化做 metamorphic equivalence test。

同一 time bucket 内无法解析的冲突应作为一个 component，以 associative、commutative、idempotent 且有界的
belief/set merge 更新。stable sort key 只保证实现复现，不能决定 physical owner；只有显式 functional
outcome/order 才能解除歧义。

uncertainty 区间不是安全证明，不能因“没有安全事件”永远不推进。必须保证每个 epoch 有确定性 progress fallback，
并区分：

```text
cursor_exhausted != core_retired != system_quiesced
```

trace cursor 耗尽后仍可能有 resident event、store visibility 和 drain。

数值路径也属于并行正确性：模型可以用 BF16/FP32 预测局部 duration，但数十万 chunk/数百万 event 的
prefix、watermark 和最终 cycle 累加应使用 FP64 或 64-bit fixed-point。模型只读取归一化相对 lag，避免
绝对大数损失精度。排序使用跨实现一致的 stable key，例如 `(time_bucket, functional_order, event_id)`；
core ID 只能用于确定性展示，不得在歧义冲突中隐式决定物理 winner。

这不等于要求消费级 GPU 上所有 kernel 都用 FP64。候选实现包括 FP32 tile-local scan + FP64/64-bit
block carry、CPU/C++ 64-bit prefix，或量化 fixed-point time bucket；必须联合比较累计误差、sort 一致性和
吞吐，避免 FP64/int64 本身成为新瓶颈。

## 13. 吞吐和显存审查

### 13.1 当前基线

最新正式评估：

| core | avg forward/window | avg total/window | UOP/s | 约 UOP/window |
|---|---:|---:|---:|---:|
| c04 | 11.9 ms | 20.7 ms | 61.4k | 1,271 |
| c08 | 35.4 ms | 52.3 ms | 49.9k | 2,610 |
| c16 | 151.4 ms | 185.8 ms | 32.0k | 5,946 |
| c32 | 424.7 ms | 656.4 ms | 18.4k | 12,078 |

c32 当前已经很慢，build/window 约 167.9 ms，forward/window 约 424.7 ms。

以 c32、K=64 估算：

- 当前约 82.8 forwards / 1M UOP；
- 单 target microchunk 一次 forward：15,625 forwards / 1M UOP，约放大 188.7 倍；
- 一次让 32 核各处理一个 K chunk：约 488 forwards / 1M UOP，仍放大 5.9 倍；
- base 后若再做两轮 full-model refinement，则 `P_total=3`，full-model 主干工作约为单 pass 的 3 倍。

因此绝不能把当前完整模型放进逐 microchunk 同步循环。

### 13.2 推荐执行结构

```text
Static stage:
  each block has L_enc=256/512 functional UOP
  pack B_blocks blocks into one large GPU forward
  -> multiple K=32/64 timing units + event keys

Dynamic stage:
  tiny timing queries
  + sparse resource/event context
  + fixed R_refine refinement

State stage:
  in-process C++/CUDA segmented aggregation / critical projection
```

static UOP encode 目标不超过 1.05 次/UOP；resident full re-encode 必须为 0。一个 `L_enc=512` block
可同时输出 8 个 64-UOP delta，但单 block 仍需约 1,953 forwards / 1M UOP，是当前 c32 调用率的 23.6 倍。
至少需要在一次 forward 中 pack `ceil(12078/512)=24` 个 blocks；例如 c32 一次 32 blocks，共 16,384
UOP，才能把 call rate 降回当前同量级。

whole-trace solver 的主成本不只是 O(N) prefix scan。若每个 memory event 属于 q 个 resource index，
每 target exact 上限为 `e_exact`、全图实际 exact 总数为 `E_exact`，总 passes 为 `P_total`，粗略 work 为：

```text
O(P_total * (N
             + sort_or_bucket(q*M)
             + q*M resource aggregation
             + E_exact relation update))
```

comparison sort 最坏近似 `O(q*M log M)`；radix/bucket/immutable partition 可接近线性，但 predicted-time
顺序每轮变化时仍可能成为主成本。必须报告 q、`E_exact`、每轮 bytes moved、有效带宽及 sort/scan/update
分项时间。

“tiny dynamic head”是设计预算，不是既成事实。需要固定可 sweep 的每 target `e_exact` 上限和固定维
resource summaries，并报告 dynamic 参数量、FLOPs/committed-UOP、relation 数 p50/p95/p99、wall-time
占比和 overflow->aggregate 比例。

### 13.3 当前代码的具体热点

- model forward 每次把 lengths 拉回 CPU，再用 Python 构造 segment；
- 每层 cross attention 对每个 core `torch.cat` 其余 core K/V；
- 当前 eval batch=1；
- 每窗有 tensor 分配、H2D、`.cpu()` 和同步；
- shared state 每窗建立 Python tuple event list 后全量 sort；
- `global_features()` 扫描所有已见 line 统计 dirty；
- `lines`、`unique_lines` 无界增长；
- Python set 表示 c32 sharer，未使用 uint32 bitmap；
- 外部 C++ mem sink 使用 JSONL/pipe，无法支持每步低延迟 query/rollback。

新调度/资源引擎长期必须为 in-process C++/pybind/torch extension，使用 SoA packed buffers、line index、
versioned transaction 和异步批量接口。复杂 MESI/TLB/MSHR 可以后置，但 event ID、relation index、
exactly-once、snapshot/commit 不能长期留在 Python 热循环。

规则状态和稀疏索引不保证 CUDA 一定优于 CPU C++；prefix/sort/aggregate 可分别选择 CPU、GPU 或 hybrid，
以 stage benchmark 和数据搬运成本决定，不能把“移到 CUDA”本身当作优化完成。

### 13.4 KV/显存

当前 `d=320`、8 层、K=64、BF16，若缓存每层 K/V，其下界为：

```text
2 * 8 * 64 * 320 * 2 bytes ~= 0.625 MiB / resident chunk
```

c32 每核一个 active resident 的纯 K/V 下界约 20 MiB/run；H-deep、B_runs 并发时约
`20 MiB * H * B_runs`。这还不含 static hidden、event arrays、mask/version、attention workspace 和 allocator
fragmentation，必须固定 slot/ring 并实测。
整条 c32 700K/core trace 的 layer KV 约 214 GiB/run，不可常驻。全 trace 只缓存低维 chunk summary，
或放 CPU pageable/mmap；GPU 仅保留 active/tile resident。pinned memory 只用于有界 staging ring，不能把整条
trace 全部 pin 住。原则上生产结构不应依赖当前 8-layer per-UOP KV。

### 13.5 Host memory、I/O 和 cold/warm 口径

GPU active memory 可以不随 trace 长度增长，但 CPU/mmap 索引必然是 O(N+M)。仅 32x700K UOP 的 22 个
int16 fields 已约 986 MB（940 MiB），尚未包含 raw columns、chunk summaries、event table、q 份 resource
index、state/version 和训练图。

必须同时报告：

- peak RSS、mmap bytes、pinned staging bytes；
- cold trace load/static encode/end-to-end；
- warm cached single-run latency；
- saturated multi-run throughput；
- page fault、I/O wait 和 cache-build time。

当前 c32 18.4k UOP/s 是包含 build/encode/forward/update 的 end-to-end 基线；新方案不能把 static build 排除后
再与它比较。所有比较固定 GPU、进程数、并发 run、cache 状态和 trace 范围。

### 13.6 Batching

优先级：

1. whole-trace/blockwise 的大批 chunk update；
2. 多个独立 run 的 block-diagonal batch；
3. 同一 run 同一 Jacobi epoch；
4. 纯 functional relation 扫描证明无冲突的 fast block coalescing。

若只有单 run 且出现 `1 fast + 31 slow`，可以一次预测连续 H 个已固定的 fast microchunks，但 H 必须由
预定义 encoding block/functional relation 决定，不能由预测 CPI 决定。

首版多 GPU 只优先做跨独立 run 的 data parallel。单 run 按 tile 分 GPU 会在每轮引入 prefix carry、hot-line/
global-channel aggregation 和 halo all-to-all；false-sharing 时通信可能主导。GPU 数量变化必须保持语义等价，
但不应预设单 run model-parallel 一定加速。

### 13.7 Hot-line 和 uncertainty 的性能退化

极端 false-sharing 时 pair graph 会爆炸；uncertainty 变宽也会让所有事件似乎重叠。必须使用 line-centric
segmented aggregate、hot-line token 和少量 exact neighbor，而不是无限 pair 或 top-M 静默丢弃。

需要报告：

- relation overflow/aggregate rate；
- hot-line component size；
- per-target exact/aggregate event 数；
- graph build/sort/scan/update 分项耗时；
- peak resident/event memory；
- refinement/rollback/invalid proposal 比例。

GPU packing 还需报告实际 token appearances，而非只按 unique ID 计算：valid-token/padding ratio、平均
`B_blocks`、batch wait、halo/retry token、CUDA-graph bucket miss。static encode `<=1.05` 次/UOP 指实际送入
GPU 的有效/重复 token accounting，不能用大量 padding 掩盖。

## 14. Whole-trace 与 tiled/blockwise

完整 trace 不必把所有动态激活常驻 GPU：

1. static block 分批编码并缓存低维 summary；
2. 每轮全局 per-core prefix scan；
3. 全局 line/resource segmented aggregation，产生固定维 chunk context；
4. updater 按 tile 大 batch 执行；
5. 下一轮重新 scan/aggregate。

若按 predicted-time tile：

- central tile 输出，halo 只提供 context；
- halo 宽度覆盖 uncertainty 和单轮最大 duration update；
- long resident 只复制引用/summary，不复制执行实体；
- last-writer、barrier、hot-line state 用显式 prefix/carry，不能依赖有限 halo；
- tiled 与 whole-trace 小样本输出必须在容差内一致；
- halo overflow/missed relation 必须计数，不能静默裁剪。

还需定义硬的 `max_halo_events/max_halo_bytes`。宽 uncertainty 使 halo 超限时，转为 hierarchical resource
carry/aggregate 或 whole-resource scan；不能无限扩大，也不能简单截断。

完整 32x700K UOP、K=64 约 35 万 chunks。若只保存一个 d=256 BF16 chunk embedding，约 180 MB
（171 MiB）；
真正需要控制的是 event token、relation index 和训练激活，而不是 unique chunk ID 本身。

## 15. 风险矩阵

| 风险 | 严重度 | 当前状态 | 必要措施 |
|---|---|---|---|
| 同步/atomic/HB 不可识别 | Critical | 当前 schema 缺字段 | 扩 functional schema；否则明确 probabilistic mode |
| commit_tick 冒充 memory event order | Critical | 当前 teacher state 使用 | event label 或 set-valued relation，不宣称精确 coherence |
| wrong-path/prefetch/DMA resource activity 不可见 | High | committed trace 缺失 | functional predictor proxy + uncertainty，限制正确性声明 |
| resident event 重复/提前/延后提交 | Critical | 当前整窗 replay 不满足 | unique event ID + event-level exactly-once |
| fixed chunk 变成 pipeline fence | High | 新设计易犯 | halo/carry，commit accounting 与 issue landmark 分离 |
| fixed trace 与反事实 timing path 矛盾 | Critical（跨 uarch） | 未建模 | given-path 合同或 functional executor/HB outcome |
| slow->fast 单向、slow 不受 fast 影响 | High | 冻结 resident 会发生 | 同步 Jacobi 双向 refinement |
| resident exposure/gradient 偏置 | High | 新风险 | unique graph、optimizer exposure correction、分桶指标；forward count 保真 |
| duration/order/state 正反馈 | High | 不可完全消除 | bounded residual、damping、rollout、回退 baseline |
| resource penalty 不守恒/重复计拥塞 | High | 纯 pair head 易发生 | resource node、capacity queue/max-plus scan |
| refinement 无法传播长同步链 | High | 固定小 `R_refine` 的边界 | 已知 HB/serial chain 显式 projection；做 R sweep |
| uncertainty 造成图稠密/死锁 | High | 新风险 | proper calibration、aggregate、固定迭代与 progress fallback |
| hot-line O(m^2) 边爆炸 | High | c32 false-sharing 高风险 | line-centric segmented aggregate + critical queue |
| 每轮 global resource sort/communication 主导 | High | whole-trace 新风险 | immutable partition、radix/segmented scan、分项 benchmark |
| microchunk GPU 调用爆炸 | Critical | 朴素实现约 189x | static/dynamic 拆分、大 block 输出、多 run batch |
| inference cache 可用但 training cache stale/activation O(graph) | High | 未实现 | two-stage/freeze 或 tiled TBPTT/recompute |
| host O(N+M) memory/I/O/pinned pressure | High | whole-trace 新风险 | mmap/pageable + bounded staging、cold/warm gate |
| single-run multi-GPU global-resource communication | High | 尚未设计 | 首版跨-run DP；单-run all-to-all 单独 benchmark |
| stale KV/state version | High | 当前无此机制 | static-only KV、version、invalid/rollback |
| Python/C++ 语义漂移 | High | 两套 event timing 已不同 | 单一 schema/reference tests/in-process API |
| label 非 telescoping | High | 当前 per-window label | boundary delta + sum invariant |
| batch/tie/core-ID 影响结果 | High | 当前 tie 使用 core ID | Jacobi、permutation test、ambiguous state |
| 初始 per-core phase/cache/resource state 不可识别 | High | 当前通常全设 0/teacher warm state | 共同 functional marker；cold/warmup/learned prior/OOD 合同 |
| 长轨迹 FP32 prefix 精度/排序分歧 | Medium/High | 新 solver 风险 | FP64/fixed-point accumulator + stable key |
| startup/drain/lifecycle 丢失 | High | teacher TQ 只 common interval | 显式 lifecycle、terminal/drain、分段评估 |
| line/state 内存无界 | Medium/High | 当前 proxy 无 eviction | bounded state/ring/index/增量统计 |

## 16. 必须实现的不变量

建议运行时 assertion 与离线报告同时覆盖：

1. 每核 chunk 对 functional trace 恰好分区一次，无 gap/overlap。
2. chunk boundary hash 与模型输出、真实 tick、batch size 无关。
3. per-core commit/subchunk accounting boundary 有限且单调，其 gap 与 chunk total 守恒；memory
   issue/complete/visibility event 可跨 chunk，只检查各自 dependency/memory-order/HB 约束。
4. 对产品合同声明纳入目标区间的 event 集合，每个 event ID 最多一次正式 commit，duplicate/missing 均为 0；
   末次 commit 后的 store visibility、writeback、system drain 是否纳入必须预先固定。
5. context-only resident 不推进 cursor、不计主 loss、不修改正式 state。
6. refinement/retry 幂等，只修改未提交 proposal。
7. 已提交 event 的全局/每资源时间不能倒退。
8. 同一 unique edge 不因 fast query 次数重复创建。
9. explicit HB、fence、barrier、join 约束全部满足。
10. core-ID permutation 后输出相应 permutation-equivariant。
11. batch packing、worker/GPU 数、tile size 改变不改变定义结果。
12. NaN/Inf/极端 duration 有确定 fallback，scheduler 必须取得进展。
13. cursor exhausted 后完成 resident/event drain 才 retired/quiesced。
14. 删除/打乱真实 tick 不改变部署 trajectory。
15. `sum(chunk delta)` 与所定义 endpoint span 严格一致。

## 17. 测试与验收

### 17.1 最小 synthetic correctness suite

- 1 slow + 1/20/100 fast，private lines；
- slow early-store / late-store + 多个 fast chunks；
- 两核同 tick store，交换 core ID；
- ping-pong、false-sharing、migratory、producer-consumer；
- private-address 但同 cache-set/bank/channel 的容量竞争；
- 长 lock convoy / barrier cascade，验证 max-plus projection 一次传播；
- CAS success/failure、atomic total order；
- fence acquire/release/full；
- barrier、join、thread start/exit；
- chunk j miss 跨 chunk j+1 的 O3 carry；
- 0/1/K/K+1 UOP 和 staggered exit/drain；
- NaN/Inf/zero/极端 duration、宽 uncertainty；
- fast:slow=1/10/100/1000 x disjoint/hot-line。

### 17.2 Metamorphic tests

- core ID permutation；
- chunk/event 遍历顺序反转；
- batch size、GPU/worker 数变化；
- K=32/64/128；
- unrelated private core 加入/删除；
- 人为复制同一 logical edge/reference 1/10/100 次，dedup 后输出不变；不同 fast target 在不同时刻引用同一
  slow event 是不同合法 edge，不能误删；
- pass 重跑、rollback、stale proposal invalidation；
- whole-trace 与不同 tile/halo；
- 去掉 timing columns 后部署结果不变。

稳定性 ablation 还需比较：soft temporal gate stop-gradient vs 允许梯度（配 gradient clipping），以及
base、last-stable、component-only 三种 fallback；分别报告精度、扰动放大、触发率和恢复行为。

### 17.3 准确率指标

保留前一报告门槛：

- `max_workload |(pred-label)/label| <25%`；
- workload absolute relative error p90 <15%；
- gross absolute cycle error，禁止 workload/core 正负抵消掩盖；
- critical workload 无随进度单调爆炸；
- normalized end-time drift p90 <5%。

新增：

- per-core prefix drift @10/50/90/100%；
- total core-cycles 与 makespan 分开；
- barrier/phase completion error；
- event-order inversion / overlap precision-recall；
- HB violation、duplicate/missing commit 必须为 0；
- residency/fanout/fast-slow ratio 分桶；
- refinement contraction、edge churn、order flip；
- uncertainty 50/90/99% coverage 和 sharpness。

### 17.4 建议性能门槛

以当前同卡 c32 `18.4k UOP/s` 为基线：

- correctness prototype：至少 0.5x，仅允许短期；
- 可替换候选：至少 1.0x；
- 优化目标：至少 2.0x，且最慢 workload 不低于旧方案 0.5x。

结构门槛：

- static encode <=1.05 次/UOP；
- resident full re-encode = 0；
- 全局 temporal passes 固定 `P_total<=3`；固定轮之后的额外 suffix/rollback refinement 平均 <=0.25 次/chunk、
  p99 <=1 次，热点单列；
- resident/event GPU memory 与完整 trace 长度不线性增长；
- UOP/s、cold/warm wall-time、stage breakdown、effective bandwidth 和 p95 workload slowdown 是主 gate；
- GPU active >=60% 仅作诊断，memory-bound sort/scan 不能靠 utilization 代替吞吐；
- CPU scheduler/state <=20%、H2D/sync <=10% 必须分别注明 cold/warm、single-run/saturated 模式；
- relation overflow 必须有 aggregate，且报告比例；
- rollback/invalid proposal <1% 只适用于 sequential/speculative suffix/critical-subgraph 路径；同步
  whole-trace Jacobi 不应为满足该指标额外引入 transaction；
- event ID、accounting、HB、commit 次数和离散合法性在 batching/tie-order 变化后必须完全一致；
- 浮点 duration/prefix 使用按 dtype 预先声明的 abs/rel 容差，最终 aggregate 另设更紧容差；不能用统一 0.5%
  掩盖离散非确定性。

这些是首轮工程 gate，不是已经由实验验证的性能承诺。

## 18. 实施顺序

### Phase 0：先冻结产品合同和语义

1. 明确当前目标是 given-path timing reconstruction，还是 counterfactual functional simulation。
2. 明确最终主指标：aggregate core-cycles、per-core timing、makespan、同步 critical path。
3. 决定能否采集 functional HB/outcome 字段和离线 event timing labels。
4. 写 chunk/event/state schema、event ID、duration/wait accounting 定义。
5. 建 timestamp taint test 和 telescoping label builder。

没有完成本阶段，不应开始新的大模型训练。

### Phase 1：static fixed-chunk baseline

1. functional-only `L_enc/K` 切片；
2. strict additive boundary labels；
3. static local encoder + 多 microchunk timing head；
4. 无 shared state、无 resident 的 per-core baseline；
5. prefix/endpoint/makespan 指标和 K sweep；
6. 先验证吞吐不会因 microchunk 崩溃。

### Phase 2：reference event semantics

1. Python/小型 C++ reference engine；
2. unique event IDs 和 event-level commit；
3. resident past/inflight/future masks；
4. versioned shadow/rollback；
5. synthetic + metamorphic correctness suite；
6. oracle event timing 注入时，reference 必须恢复定义好的 order/state。

### Phase 3：parallel temporal graph

1. whole-trace/blockwise prefix scan；
2. line/resource-centric segmented aggregation；
3. soft interval gate；
4. `R_refine=0/1/2/3` 同步 Jacobi ablation；
5. hot-line aggregate 与关键子图 projection；
6. whole-trace/reference/tiled 一致性检查。

### Phase 4：训练稳健性

1. fully predicted self-rollout；
2. frozen-policy ensemble 和 corrupted state；
3. unique-node/edge exposure normalization；
4. prefix/endpoint/makespan/uncertainty/stability loss；
5. independent seed/run calibration；
6. stateless baseline + bounded interaction residual 作为回退。

### Phase 5：生产优化

1. C++/CUDA/torch extension 的 in-process state/relation engine；
2. static block cache、fixed slot/ring、bounded pinned staging + pageable/mmap；
3. 多 run batching、CUDA graph/static buckets；
4. incremental prefix/resource carry；
5. 性能和准确率联合 Pareto gate；
6. 最后才增加更真实的 cache/TLB/MSHR resource model。

## 19. 不应做的实现捷径

- 不要把每核一个固定 chunk 当作同一真实时间窗口。
- 不要直接给当前 8 层 full-cross QKVR 加 resident 循环。
- 不要每次 fast 查询都重算、重监督或重提交 slow chunk。
- 不要冻结 slow 后只允许 `fast <- slow` 单向影响。
- 不要用 commit order 冒充 cache/coherence request order。
- 不要按 true/pred overlap 硬裁训练 candidate graph。
- 不要用 top-M 静默丢弃 hot-line 其余关系。
- 不要通过 uncertainty 动态改变基础 chunk 边界。
- 不要在 barrier total-duration label 上再重复增加 wait。
- 不要把 self-rollout 称为拥有 counterfactual oracle 的 DAgger。
- 不要在缺少 HB/outcome 时宣称精确 atomic/MESI correctness。
- 不要让 Python+JSONL 成为生产逐事件反馈路径。

## 20. 最终推荐

当前讨论可以收敛成一句话：

> 固定 functional microchunk 负责消除长度泄漏；全局 prefix timing 产生预测时间轴；
> line/resource-centric interval graph 让一个 slow chunk 被多个 fast chunk 正确引用；固定轮同步 refinement
> 处理双向干扰并消除处理顺序依赖；关键同步子图再做合法次序投影。

slow chunk 应被多个 fast target 反复“看见”，但实现形式必须是 unique node/event 的多次只读引用，而不是
把 slow UOP、loss 或 state update 复制多份。

最先需要验证的不是新模型精度，而是四个硬前提：

1. functional/HB schema 是否足以支持所声称的正确性；
2. additive label 和 event exactly-once 是否严格守恒；
3. parallel solver 是否与 reference 语义一致且不依赖 batch/tie 顺序；
4. static/dynamic 拆分后 c32 吞吐是否至少守住当前 18.4k UOP/s。

在这四个 gate 通过前，resident/temporal graph 应视为待验证的 timing reconstruction 架构，而不是已经正确的
多核时间模拟器。
