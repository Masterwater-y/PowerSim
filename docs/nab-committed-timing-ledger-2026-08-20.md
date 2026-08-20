# NAb committed timing ledger（2026-08-20）

## 结论

NAb C32 当前 user CPI 为 `0.299055691`，gem5 baseline 为
`0.372184945`，FastSim 少算 `0.073129253/UOP`，即 `23,401,363`
core-cycle，signed error 为 `-19.649%`。

本轮 ledger 排除了两个此前可疑的实现问题：

1. committed UOP、memory event 没有在 interval/time-epoch 边界丢失或重复；
2. `Q=1024` 的 corrected-horizon violation 不是主要误差源。把 Q 从 512
   改到 2048 后，边界计数按预期变化约 4 倍，但 CPI 只变化 `0.000249`。

已建模的 memory-response/ROB/IQ correction 也不是当前 23.4M-cycle 缺口的
直接来源。response correction 的逐 UOP residence 很大但高度重叠；真正落到
core completion critical path 的总增量只有 `138,408` cycle，即缺口的
`0.591%`。

**更正：raw wrong-path 或全局 frontend population 不能证明 NAb gap。** gem5
`stats.txt` 的 O3 统计窗口与 TaoTrace per-core user measurement 不同；旧分析把
raw `cacheLines/icacheStallCycles` 除以 user target 并与 FastSim 相减，口径不
成立。后续 exact-window frontend ledger 和 trace-only 因果消融已经识别出主
组件：committed x86 宏指令跨 64B Fetch block 时，旧 FastSim 只请求起始 PC
所在块，遗漏指令尾部块。补齐该字节供给后，NAb C4/C8/C16/C32 CPI APE 从
`8.76%--19.65%` 降到 `1.56%--3.52%`，覆盖 `82%--86%` 原始 gap；不需要
wrong-path trace。request admission 只解释约 0.5%，匿名 wrong-path shadow
在现有窗口中全部被隐藏。

## 实现合同

### 1. committed pipeline lower-bound ledger

`CommittedPipelineAuditCounters` 现在对每个 committed UOP 记录相邻 stage：

```text
fetch -> decode -> rename -> dispatch -> issue
      -> execute -> completion -> retire
```

同时记录 memory UOP 子集的 `issue -> completion -> retire`。每个 UOP 必须满足
stage 单调性，并守恒：

```text
fetch_to_retire == sum(all adjacent stage residence)
```

这些是逐 UOP residence 的和；不同 UOP 会重叠，不能把它们相加后当作 core CPI。

### 2. response-corrected ledger

response feedback 得到实际 issue/completion/retire 后，分别保留 base 和
corrected 的：

```text
issue_to_completion
completion_to_retire
issue_to_retire
```

并记录 issue/completion/retire delay。审计模式会禁用跳过逐 UOP accounting 的
activity-certificate host 快路径，因此 `stage_uops` 必须等于 committed UOP
总数。该开关只影响审计吞吐，不改变 target transition。

### 3. committed epoch ledger

每核记录：

- accepted prefix 和 accepted UOP；
- memory event、in-flight memory UOP；
- corrected issue 在 horizon 内/外的 exactly-once 分类；
- beyond-horizon lateness 和 sparse cross-epoch edge。

报告端同时检查：

```text
epoch.accepted_uops == committed_pipeline.uops
epoch.memory_events == within_horizon + beyond_horizon
```

### 4. 输出和审计工具

per-core 和 total JSON 均输出完整 ledger。此前 per-core JSON 静默遗漏的
response seed/completion/dependency/retire/dispatch/issue-cycle 字段也已补齐，
避免相关性分析把缺字段误当成零。

`tools/audit_fs_committed_pipeline.py` 的 schema 更新为 v4，支持按 workload/core
筛选、守恒检查、逐核 CPI gap 相关性和 Q diagnostic。v4 只对 scope-aligned
FastSim/trace signal 做相关性；raw gem5 O3 counter 只按自身 raw committed UOP
归一化，并显式报告 raw/scoped window ratio。

## NAb C32 守恒结果

正式输入为 32 核、每核约 10M user UOP，总计 `320,000,026` committed UOP。

| Ledger | Population/result | Conservation |
|---|---:|---:|
| committed stage UOP | 320,000,026 | PASS |
| response-corrected stage UOP | 320,000,026 | PASS |
| committed epoch accepted UOP | 320,000,026 | PASS |
| committed memory event | 39,808,507 | PASS |
| response-corrected memory UOP | 39,808,507 | PASS |
| epoch memory event | 39,808,507 | PASS |

committed lower-bound residence：

| Adjacent stage | cycle/UOP |
|---|---:|
| fetch -> decode | 1.0000 |
| decode -> rename | 1.0000 |
| rename -> dispatch | 2.0144 |
| dispatch -> issue | 3.1059 |
| issue -> execute | 0.0000 |
| execute -> completion | 1.3587 |
| completion -> retire | 2.5143 |
| fetch -> retire total | 10.9933 |

资源审计没有发现可补足 gap 的 committed bottleneck：rename free-list stall 为
0；committed ROB/IQ/LQ/SQ capacity lower-bound 分别只有
`0.000092/0.001719/0.000109/0.000146 cycle/UOP`。这些数不包含 response
feedback 后的重叠 occupancy，不能与 CPI 做加法分解，但足以排除“committed
lower-bound 在边界漏记一个大容量 stall”。

response ledger 的关键结果：

| Signal | Total | per user UOP |
|---|---:|---:|
| response critical-path extension | 138,408 | 0.000433 |
| response seed cycles | 1,160,135 | 0.003625 |
| completion extension residence | 212,341,880 | 0.663568 |
| corrected issue -> retire residence | 1,345,660,506 | 4.205189 |

`completion extension residence` 是跨 UOP 重叠的面积，不是 212M 个可加 core
cycle。能改变最终 completion horizon 的守恒量是 138,408-cycle critical
extension；因此不能拿大 residence 数直接填 23.4M-cycle CPI gap。

## Q 因果对照

Q 是 FastSim time weaving 的实现分块尺度，不是目标微架构参数。

| Q | FastSim CPI | CPI gap | horizon violation | cross-epoch edge | response critical cycle |
|---:|---:|---:|---:|---:|---:|
| 512 | 0.299217713 | 0.072967232 | 158,128 | 1,197,599 | 190,255 |
| 1024 | 0.299055691 | 0.073129253 | 78,934 | 598,002 | 138,408 |
| 2048 | 0.298969073 | 0.073215872 | 39,542 | 298,828 | 110,690 |

Q 增大 4 倍后，violation/cross-epoch population 约缩小 4 倍，但 FastSim CPI
最大差仅 `0.000249`（相对 FastSim CPI 为 `0.083%`，相对 0.0731 gap 为
`0.34%`）。所以逐核 `horizon_violation/UOP` 与 gap 的高相关性是 core
activity 的共同伴随量，不能作为加周期依据。

## Raw frontend 证据为何被撤回

gem5 源码确实只在执行 `fetchCacheLine()`、完成翻译并准备 I-cache packet 后增加
`fetch.cacheLines`；处于 `IcacheWaitResponse` 的 cycle 增加
`icacheStallCycles`。但这两个 raw stat 不是 TaoTrace user oracle 的同窗口事件：

1. gem5 在 architectural `WORKBEGIN` 时 reset stats，而 TaoTrace functional
   measurement 在稍后的 process-wide serial marker 才开始；
2. 每核达到 10M user-UOP target 后，TaoTrace 立即冻结该核的 trace/CPL oracle，
   但该核仍继续执行，直到所有核达到 target 后 gem5 才 dump raw stats。

NAb 的 raw gem5 `commitStats0.numOps` 是 `368,699,979`，而同次 TaoTrace
user+kernel oracle 只有 `320,469,461` retired UOP。raw window 多出 `15.05%`，
因此 `49,909,401 raw cacheLines - 30,128,380 scoped FastSim transitions` 没有
exact population 意义，先前的 `82.66% gap` 说法撤回。旧的 raw-per-user-UOP
逐核相关性也因窗口错位无效，审计 schema v4 已将全部 raw gem5 signal 从 CPI
相关性中移除。

即使只做不能用于定量归因的 raw-UOP rate sanity check，跨负载也否定一个通用
frontend request penalty：

| Workload | CPI signed error | raw cacheLine/raw UOP - FS transition/user UOP | raw squashed/raw UOP |
|---|---:|---:|---:|
| Stockfish | -14.20% | +0.00017 | 0.01386 |
| zstd | +3.28% | +0.04019 | 0.13507 |
| NAb | -19.65% | +0.04121 | 0.01710 |
| Neutron | +4.43% | +0.04650 | 0.36599 |

NAb 的 request-rate residual 与 zstd 几乎相同、低于 Neutron，但误差方向相反。
NAb 的 scope-correct user branch miss 只有 `133,079/320,000,026`，即
`416/MUOP`；omnetpp、zstd、Neutron 分别为 `3,443/3,016/6,874/MUOP`。
所以 NAb 不是 wrong path population 特别大的负载。

因此当前可以下的结论是：

- 已证实：NAb gap 不来自 committed cursor 丢失、Q 边界或已有 response
  correction 的可见 critical path；
- 已反证：用 raw gem5 fetch/request/squash population 直接解释 NAb gap；
- 尚未识别：剩余 gap 是 frontend exposure、未建模的 committed OoO interaction，
  还是其它 scope-aligned timing mechanism；
- 禁止做法：把 raw `cacheLines`、`icacheStallCycles` 或 overlapping residence
  拟合/相加成 CPI penalty。

## 下一步

先在 gem5/TaoTrace 内建立与每核 functional measurement 同起止边界的在线
frontend ledger，至少拆分 request、response、resume、squash/refetch 和 exposed
cycle；同时建立 portable committed-frontend lower bound。只有两边窗口、scope
和 population 对齐后，才能判断剩余项是否是 committed trace 不可辨识的
speculative/refetch residual。当前不应先实现 wrong-path penalty。

## 复现

```bash
python3 tools/audit_fs_committed_pipeline.py \
  --pipeline \
    tmp/taotrace-native-identity-c16-c32-10m-final-20260820/accuracy/held-out-c32/pipeline.json \
  --output tmp/nab-committed-timing-ledger-20260820 \
  --config \
    tmp/taotrace-native-identity-c16-c32-10m-final-20260820/accuracy/held-out-c32/user-cache-state.cfg \
  --fastsim build/fastsim \
  --workload 816.nab_s \
  --cores 32 \
  --jobs 1 \
  --force
```

Q 对照分别使用独立项目内目录
`tmp/nab-committed-timing-ledger-q{512,2048}-20260820/`，并传入
`--interval-max-cycles 512` 或 `2048`。

## Exact-window frontend ledger 实施（2026-08-20）

已实现 `taotrace-scoped-frontend-v1`，用于替换此前不对齐的 raw gem5 Fetch
统计。它不再从全局 `stats.txt` 推断，而是在真实 O3 Fetch 路径在线记录：

- Fetch 从 warmup 开始只预跟踪在途请求；TaoTrace 在每核首个 CPL 计量事件
  快照起始 population，该核达到 user-UOP target 时冻结；
- ITLB 请求创建/完成、I-cache 首次发送/retry/response/squashed response；
- redirect/squash 次数、无效同块 refetch、换块请求；
- 每周期 Fetch 状态，特别是 ITLB wait、I-cache response wait 和 retry wait；
- 测量起点在途请求和终点在途请求。

每核输出只是在已有 `kernel-events-coreN.json` 中增加一个有界汇总对象，
不改变 FST，不输出逐请求 JSONL，也不影响 gem5 时序。以下四个合同必须
同时守恒，否则 TCSim 合并和 FastSim validator 直接拒绝结果：

1. `inflight_at_start + requests_started = terminals + inflight_at_end`；
2. user-mode + kernel-mode request = requests started；
3. refetch/invalid-new/valid-block-change = requests started；
4. accepted send + rejected send = send attempts；全部 Fetch status cycle 也
   必须等于 status sample population。

其中 `user_mode_requests_started/kernel_mode_requests_started` 是 Fetch 发起时
CPU 当前特权级的诊断切分，不是假装成 committed user PMU scope；投机 I-side
请求本来就没有 committed instruction 可用于归属。正式可比边界是
`exact-cpl-first-event-to-functional-target-window`，user-only CPI 仍由
TaoTrace committed/CPL ledger 单独给出。

首次 NAb C4 100k smoke 还定位并阻止了一个真实边界缺口：旧实现从全局
serial marker 启用 frontend ledger，但 CPL ledger 从每核首个 user commit
（或更早的 from-user exception）才初始化。core2 因而多记录 `3747` 个 Fetch
status cycle；其余三核的差值为 `-6/+1/+1`。这不是模型误差，而是测量起点
不一致。当前实现已把 frontend 启用移到 `initializeCpl()`；审计器也会拒绝
任何超过 CPL measured cycles 一个 cycle 的正向越界。旧 smoke 仅作为定位
证据，不进入负载归因。

这项实现只提供证据，**不会因为存在 wrong-path 请求就自动给 FastSim 加
惩罚**。只有 NAb 与对照负载的 exact-window 数据证明某个缺失状态与 CPI
残差在方向和量级上都一致，才进入模型候选。

### CPL-aligned C4 100k 结果

修正后的 NAb smoke 和三个并行 control 全部通过严格守恒与窗口 gate。下表的
`user Fetch req` 是请求发起时 CPU 位于用户态的诊断 population；`FS block`
来自同源 committed FST 的 64B fetch-buffer block transition，二者不能当作
同一 PMU，但其差额是 functional trace 看不到的 speculative/refetch 上界。

| workload | squash / all req | I-cache wait / measured cycles | user Fetch req | FS block | (req-FS)/req |
|---|---:|---:|---:|---:|---:|
| Stockfish | 7.10% | 22.02% | 17,467 | 15,769 | 9.72% |
| zstd | 6.14% | 14.42% | 64,200 | 56,743 | 11.62% |
| NAb | 1.63% | 61.09% | 35,237 | 19,776 | 43.88% |
| Neutron | 7.87% | 20.53% | 43,491 | 25,071 | 42.35% |

证据否定了两个过度简化的解释：

- NAb 的 redirect/squash event rate 是四者最低，不支持“常规 branch
  misprediction 特别多”这一解释；但已完成后才被 wrong path 消费的 response
  无法仅靠 terminal 类型识别，因此 `squash/req` 也不能当 wrong-path request
  比例。
- NAb 与 Neutron 的不可见 request residual 几乎相同，不能对 residual count
  直接乘固定周期。NAb 独特的是 response-wait exposure：占 measured cycles
  `61.09%`，显著高于三个 control 的 `14.42%–22.02%`。当前最强证据指向
  fetch request cadence、one-outstanding response 和 correct-path resume 的
  overlap，而不是单纯的 wrong-path 数量。

同源 FST 的现有候选反事实也排除了“直接默认开启 committed-PC L1I”：NAb
在正式配置下的 user-UOP CPI 为 `0.223406`，启用 32KiB/8-way L1I 后仅为
`0.224376`，只增加 `0.000970`；相对同窗口 gem5 `0.285111` 的
`0.061705` 缺口只覆盖 `1.57%`。再启用 reconstructed speculative path 后
CPI 为 `0.224086`，也未解决缺口。Stockfish 只增加 `0.001962`；Neutron 和
zstd 还出现非单调变化。因此在这个 100K pilot 阶段，这些 candidate 继续保持
默认关闭；后文的完整 80-case gate 取代了这一阶段性结论。

这次 p3 修改是 timing-neutral 的旁路聚合，不改变 gem5 CPI、PMU 或 FST
内容，所以**已有 FST 数据集不需要整体重采**。旧结果只是缺少新的 frontend
oracle，无法从 committed FST 离线恢复；若要把该组件扩展到完整负载矩阵，只需
针对所需 case 重跑 gem5/TaoTrace oracle。下一步应先增加 sequential-target、
post-squash 和 target-unavailable 请求原因，再决定可从 functional trace
构造的匿名 speculative-fetch shadow，而不是重采全部 FST 或添加经验常数。

## Trace-only Fetch supply 修复（2026-08-20）

exact-window ledger 之后实现了三个相互独立的 FastSim candidate：

1. `core.fetch_supply_model` 把新 Fetch block 的 request creation 放到 Fetch
   真正可运行的时点；redirect、serialize 或 32-entry fetch queue 反压期间不再
   预先完成未来 response。
2. `core.fetch_supply_static_instruction_span` 使用 portable `.fst.imap` 的静态
   x86 指令长度，请求跨 64B Fetch block 的第二块。该 sidecar 只有 ISA decode
   fact，gem5 TaoTrace 和 drmemtrace 离线模块解码都能生成，不含 wrong-path、
   timing、cache hit 或 oracle 信息。它与 request admission 分开开关，确保两条
   因果边可独立消融。
3. `core.fetch_supply_speculative_shadow` 不使用 wrong-path PC。它只按 branch
   resolution window、ROB headroom 和已观察 committed request density 产生匿名
   request token；不改 cache tag，也不把 request 数乘固定 penalty。只有仍跨过
   recovery 的在途 response 才能推迟 correct-path Fetch。

同一批 C4/每核 100k user-UOP FST 的单变量结果保存在
`tmp/fetch-supply-ablation-20260820/`：

| workload | gem5 exact user-UOP CPI | FastSim baseline | request admission only | admission + static span | baseline APE | span APE |
|---|---:|---:|---:|---:|---:|---:|
| NAb | 0.285111 | 0.223406 | 0.223716 | **0.275584** | 21.64% | **3.34%** |
| Stockfish | 0.560548 | 0.411022 | 0.411109 | 0.421889 | 26.68% | 24.74% |
| Neutron | 1.050220 | 0.666293 | 0.666870 | 0.669715 | 36.56% | 36.23% |

zstd 这份 smoke 需要 `allow-cross-page-without-virtual-token` 诊断豁免，FastSim
CPI 为 11.4 而 gem5 为 1.32，不能作为正式 accuracy gate；其 frontend
population 仍可用于守恒检查。

NAb 的 population 证据与 CPI 同方向、同量级：

| signal | baseline | static-span model | exact-window gem5 |
|---|---:|---:|---:|
| committed/diagnostic Fetch requests | 19,776 | 32,593 | 35,237 |
| request residual | 15,461 | 2,644 | 0 |

因此 static span 消除了 `82.9%` 的 request residual，并覆盖 `84.6%` 的 NAb
CPI gap。逐核结果排除了 aggregate 偶合：core 1/2/3 residual 分别从
`5403/3483/5541` 降到 `423/636/562`。core 0 只从 `1034` 降到 `1023`，与它
不同的动态代码路径一致。初版实现共查询 230,494 个 measured 宏指令的静态
长度，其中 4 个边界/系统记录没有静态条目，并识别出 16,898 个跨块动态宏指令；
缺失条目会 fail closed，不猜 x86 指令长度。生产实现又增加了精确的 x86 15B
最大长度 prefilter：只有 PC 位于 64B block 最后 14B 时才查 `.imap`，不改变
任何 target transition。

仅修 request-admission 时点对 NAb 只增加 `0.000310` CPI，覆盖约 0.5% gap，
所以它是正确但次要的状态机修复。匿名 shadow 在 NAb 估计 5269、实际可发
3320 个 request，3320 response-wait cycles 全部隐藏在 resolution/recovery
窗口内，CPI bit-identical；这直接否定了“wrong-path request 数乘固定周期”。

### 严格 effective-config 正式重放

100k smoke 之后在正式 10M/core 数据集上重放 C4/C8/C16/C32。旧数据的重放不能
只读取 `user-cache-state.cfg`：当时该生成配置仍写着
`dtlb.miss_model=se_atomic` 和 1-cycle latency，正式 accuracy pipeline 还通过
`pipeline.json` 施加
`timing_walk/12-cycle` 以及 1-cycle Fetch refill。漏掉这些 effective CLI
覆盖项会让 Neutron 的 DTLB walk delay 从约 3.59 亿 cycle 变成 0，所得 CPI
不是同一个 baseline，已经判为无效结果。下表全部使用 pipeline 的最终参数；
C4 baseline 重放与原报告逐位一致。生成器现已把这些最终值直接追加到 user 和
kernel effective cfg，同时保留同值 CLI 覆盖作为冗余校验；新数据不再需要拼接
cfg 和 pipeline 才能恢复 target。

| workload | cores | baseline APE | static-span APE | 覆盖原 CPI gap |
|---|---:|---:|---:|---:|
| NAb | 4 | 8.761% | **1.558%** | 82.22% |
| NAb | 8 | 12.450% | **1.751%** | 85.94% |
| NAb | 16 | 19.488% | **2.750%** | 85.89% |
| NAb | 32 | 19.649% | **3.524%** | 82.07% |
| Stockfish | 4 | 23.085% | 20.154% | 12.70% |
| Stockfish | 8 | 20.259% | 16.682% | 17.66% |
| Stockfish | 16 | 15.573% | 9.727% | 37.54% |
| Stockfish | 32 | 14.202% | 8.239% | 41.98% |
| Neutron | 4 | 0.443% | 0.486% | -9.87% |
| Neutron | 8 | 2.491% | 2.529% | -1.53% |
| Neutron | 16 | 5.301% | 5.397% | -1.82% |
| Neutron | 32 | 4.426% | 4.486% | -1.35% |

NAb 的四个核数都覆盖 82%--86% gap，且 APE 收敛到 1.56%--3.52%，所以这不是
C4 smoke 偶合。Stockfish 只有部分改善，说明 byte-span 是其误差组件之一，
不是全部；Neutron 最多恶化 0.097 个百分点，是有用的负向对照，排除了给所有
负载统一加周期的经验拟合。

计数合同在全部 12 个正式 case 中通过：每个 cross-block macro 的 extra request
exactly once，response wait 等于 hidden + exposed。PC-only `.imap` 在多地址空间
core 上会被拒绝；候选只对实际需要查长度的块尾宏指令记
`fetch_supply_static_span_unavailable` 并 fail closed。NAb 四个核数只有极少缺失，
所以主结论不依赖该缺口；正式默认开启前仍应把 `.imap` 升级为 address-space
scoped schema，并对完整 10-workload matrix 做 gate。

15B prefilter 在 NAb C4 把静态 map 查询从 22,765,836 次降为 6,094,214 次，
CPI、cross-block population、request population 均逐位一致。两组交错重放的
进程 CPU time 显示当前候选约增加 5% host CPU；wall-time 受同机负载影响较大，
不能用单次历史吞吐作结论。

在完成 80-case gate 以前，三个开关均保持默认关闭：

- `fetch_supply_static_instruction_span` 是已被因果验证的强候选，但还缺完整负载
  和微架构趋势 gate，以及 address-space-scoped `.imap`；
- `fetch_supply_model` 是正确但量级很小的 request-admission 修复；
- `fetch_supply_speculative_shadow` 在现有数据中全部隐藏、CPI 不变，只保留诊断。

因此 NAb 根因已经从“泛化的 I-side 缺失”收窄为一个可移植、可守恒、无需
wrong-path trace 的 committed instruction-byte supply 缺口。Stockfish 和
Neutron 剩余误差还有其它组件，不能用 NAb 的 static-span 修复外推。

## 10-workload full gate（2026-08-20）

最终 gate 使用 10 workload × C4/C8/C16/C32 × user/user+kernel，共 80 个
candidate run；另用同一时段、同一 runner 重放 80 个 baseline control。两组均
`80/80` 完成、零失败，baseline CPI 与正式 accuracy 报告 `80/80` 逐位一致。
runner 从四个 `pipeline.json` 恢复完整 effective target，只改变
`core.fetch_supply_static_instruction_span=true`。candidate 和 control 分别保存于：

- `tmp/fetch-supply-static-span-full-gate-20260820/`；
- `tmp/fetch-supply-static-span-full-gate-baseline-20260820/`。

### CPI gate

下表每行包含 40 个 core/workload case。user scope 的 perf-like CPI 与 user-UOP
CPI 使用不同分母，但固定 trace 下 APE 分布相同；user+kernel 两种分母略有差异。

| scope / denominator | metric | baseline | static span |
|---|---|---:|---:|
| user / user UOP | mean APE | 8.86% | **6.96%** |
| user / user UOP | P50 / P90 / P99 | 7.11% / 15.97% / 21.98% | **6.65% / 12.20% / 18.80%** |
| user / user UOP | WAPE | 6.50% | **5.74%** |
| user+kernel / user UOP | mean APE | 10.57% | **8.57%** |
| user+kernel / user UOP | P50 / P90 / P99 | 11.13% / 18.64% / 23.81% | **9.98% / 15.79% / 20.75%** |
| user+kernel / user UOP | WAPE | 7.17% | **6.31%** |
| user+kernel / retired instruction | P50 / P90 / P99 | 10.86% / 18.04% / 23.48% | **9.77% / 14.78% / 20.40%** |

逐 workload 的 user CPI APE（四个 core count 的 mean）：

| workload | baseline | static span | delta |
|---|---:|---:|---:|
| Stockfish | 18.28% | **13.70%** | -4.58 pp |
| omnetpp | 10.28% | **9.54%** | -0.74 pp |
| zstd | 4.68% | 4.91% | +0.23 pp |
| LBM | 3.98% | 4.01% | +0.03 pp |
| SPH | 6.21% | 6.20% | -0.01 pp |
| TeaLeaf | 11.68% | **10.79%** | -0.90 pp |
| NAb | 15.09% | **2.40%** | -12.69 pp |
| Graph500 | 7.24% | 7.93% | +0.70 pp |
| NAMD | 8.00% | **6.91%** | -1.08 pp |
| Neutron | 3.17% | 3.22% | +0.06 pp |

NAb 在 user scope 的 C4/C8/C16/C32 APE 分别为
`1.56%/1.75%/2.75%/3.52%`；user+kernel 为
`0.25%/0.38%/1.28%/2.65%`。主要回归点是 Graph500 C8：APE 从 `3.12%`
升至 `6.47%`。该 case 新增 36,912 exposed Fetch wait cycle，但总 core cycle
反而减少约 2.58M，说明小幅 frontend 扰动改变了全局 memory/time-weave
interleaving；这不是 static byte-span 本身可直接解释的单调效应，默认开启前
必须单独关闭该非单调缺口。

### PMU 与 CHA gate

static span 不改变 committed branch population。关键 PMU WAPE 基本保持不变：

| event | user baseline -> candidate | user+kernel baseline -> candidate |
|---|---:|---:|
| branch miss | 3.19% -> 3.19% | 5.71% -> 5.73% |
| L1D tag miss | 5.23% -> 5.23% | 5.26% -> 5.26% |
| private-L2 tag miss | 12.03% -> 12.02% | 11.13% -> 11.14% |
| LLC tag miss | 4.25% -> 4.25% | 4.56% -> 4.55% |

相对 baseline，candidate 汇总 CHA request 只变化 `-0.016%/-0.022%`
（user/user+kernel），LLC miss 变化 `-0.0003%/-0.0011%`，unique fill 变化
`+0.0003%/+0.0017%`。因此 CPI 改善不是通过改写 data-side/CHA PMU population
换来的。

### 吞吐与默认结论

同机 control 的 80-case 总运行时间为 332.4 秒，candidate 为 337.8 秒；逐 case
吞吐比的中位数为 `0.995`。按 scope 汇总 wall time，user 吞吐约为 baseline 的
`99.1%`，user+kernel 为 `98.0%`。单 case wall time 噪声较大，不能解读其极值，
但全矩阵显示 host 开销约 1%--2%，可接受。

完整负载 gate 通过了 aggregate CPI、tail CPI、PMU 和吞吐四项。用户明确接受
Graph500 C8 的轻微、局部回退后，`core.fetch_supply_static_instruction_span` 已在
`SimulatorConfig` 和受维护的 `gem5-v28_1-time-epoch.cfg` 中默认开启。其余两个
Fetch candidate 仍保持关闭：

1. `core.fetch_supply_model=false`：request-admission 修复正确但量级很小；
2. `core.fetch_supply_speculative_shadow=false`：正式窗口中 response 全部被隐藏，
   且 committed trace 不能唯一恢复 wrong-path population。

`.imap` 已由采集端升级为 address-space-scoped lookup；PC-only 多地址空间输入会被
拒绝，缺少静态条目时模型 fail closed。Fetch 参数的跨微架构趋势仍是后续 DSE
验收项，但不再阻断当前 baseline 的 exact byte-span 默认语义。

## 默认启用后的 Stockfish/NAMD 残差审计（2026-08-20）

本节只使用同一批正式 10M/core FST、gem5 `stats.txt`/native oracle 和单变量
FastSim 消融。临时审计产物位于：

- `tmp/stockfish-namd-pte-ablation-20260820/`；
- `tmp/stockfish-namd-pipeline-audit-20260820/`；
- `tmp/stockfish-namd-sparse-scoreboard-ablation-20260820/`。

### Stockfish

static-span 已经覆盖 baseline user CPI gap 的
`12.7%/17.7%/37.5%/42.0%`（C4/C8/C16/C32），但剩余 user APE 仍为
`20.15%/16.68%/9.73%/8.25%`，且全部是 FastSim 欠预测。

branch/cache/DTLB 对账不能解释该缺口：formal user branch miss 在四个核数上均
由 FastSim 略微多算；L1D、private-L2 和 LLC miss 也普遍多算。即便把 gem5 与
FastSim 的 DTLB miss 差额全部按 12-cycle 串行收费，也只能覆盖剩余 user cycle
gap 的 `3.1%/2.5%/3.6%/5.0%`。因此不能再用 data-cache miss 或 branch PMU
计数不足解释 Stockfish。

证据指向两个仍未等价的 timing state：

1. gem5 的 I-cache stall 为每 user UOP
   `0.0551/0.0703/0.0999/0.1051` cycles，而 FastSim committed Fetch response
   exposed 为 `0.0463/0.0602/0.0883/0.0891`。两者不是可直接相减的同一 counter，
   但差额随 core count 增长，并在 C32 达到约 5.12M cycles，证明完整 I-side
   request/retry/refetch/overlap 仍未闭合。
2. FastSim 只对 committed branch predictor state 施加 2-cycle redirect；L1I、
   speculative Fetch shadow 和 branch ROB shadow 均关闭。gem5 同窗有
   `0.0130--0.0174` commit-squashed inst/user-UOP，并且 rename ROB/SQ full state
   活跃。生产 sparse scoreboard 虽然把 Stockfish APE 相对关闭时改善
   `8.50/3.87/3.62/4.25 pp`，但其 O3 diagnostic event 口径仍与 gem5 不同：
   ROB-full 多约 `3.0--4.5x`，SQ-full 只有 `0.23--0.56x`。这些重叠 counter
   不能加成周期，却证明相同的 ROB=192、SQ=32 尚未产生相同 occupancy/release
   状态机。

所以 Stockfish user 残差的主类是 **speculative frontend 与 response-driven
ROB/SQ overlap**，不是一个漏掉的 PMU count。committed trace 无法唯一恢复
wrong-path PC/依赖/资源占用；可实施的下一步是先把 Fetch request→response→resume
和 store response→SQ release→rename unblock 做成同口径、非重叠 exposure ledger，
再修 sparse scoreboard 的 state transition 计数，不能提高固定 branch/refill
penalty 拟合。

Stockfish combined scope 还有独立的 page-fault 缺口。gem5 在
C4/C8/C16/C32 记录 `20/35/68/339` 次 page fault，portable syscall-semantic
模型均预测 0 次；因此 kernel gap 为 `0.344/0.544/1.191/6.107M` cycles，在 C32
占 combined 总缺口 `41.9%`。打开现已改名的 ROI-entry page-state 模型后选择
`20/35/69/339` 次，证明 event identity 可由 gem5 PTE sidecar恢复；但 combined
APE 只从 `22.20/18.47/11.71/13.35%` 降到
`20.82/17.77/10.66/11.87%`，说明固定 handler profile 与跨核/缓存 overlap 仍
不足。drmemtrace 没有该 snapshot 时把这些位明确留为 unknown，并继续走
portable fallback；FS user+kernel 默认开启不会把宿主 `/proc/pagemap` 猜成 guest
状态。

### NAMD

NAMD 必须分 user 与 kernel 两个根因。user APE 为
`12.67%/2.41%/4.12%/8.45%`；branch miss 差额按 2-cycle 收费只能覆盖至多
`0.4%` 的 user gap，DTLB 差额按 12-cycle 的极端上限也只覆盖
`2.6%/18.0%/14.5%/8.0%`。L1D miss 已接近，private-L2/LLC 在多数核数反而
明显多算。因此 user 残差同样不是 cache miss population。

NAMD 的 gem5 I-cache stall 为每 UOP `0.1051/0.0898/0.0913/0.0977`，FastSim
committed Fetch exposed 为 `0.0637/0.0641/0.0639/0.0445`；同时 gem5 SQ-full
为 `3.36/4.48/8.80/25.33M`，FastSim sparse diagnostic 仅
`0.24/0.35/0.71/1.00M`，而 FastSim ROB/IQ-full 又被显著多算。关闭 sparse
scoreboard 会把 NAMD user APE 恶化到
`17.10%/12.31%/12.60%/11.82%`，说明 response closure 的方向正确，但当前
ROB/IQ/SQ 之间存在错误补偿。这也解释了 C8/C16 很准而 C4/C32 仍差：不是一个
可跨拓扑外推的固定 latency。

combined scope 的主误差则是 page-fault/syscall/IRQ event 与 service：

| cores | combined 总缺口 | kernel 占比 | page-fault reference/predicted | page-fault active cycles reference/predicted |
|---:|---:|---:|---:|---:|
| 4 | 2.276M | 32.8% | 304 / 304 | 4.562M / 4.120M |
| 8 | 4.750M | 88.5% | 294 / 52 | 4.558M / 0.705M |
| 16 | 10.370M | 81.8% | 675 / 261 | 11.565M / 3.372M |
| 32 | 18.275M | 62.2% | 1317 / 1252 | 25.288M / 16.173M |

C8 ROI-entry page state 能把 52 修到 294，并把 combined APE 从 `16.91%` 降到
`10.87%`；但 C16/C32 边界 PTE 只选择 36/47 次，因为其余页面是在 measurement
开始后由新 allocation 变成 non-present，单次初始 snapshot 本来就看不到。
C32 即使 event count 已接近，page-fault active cycles 仍低估 9.115M，syscall
count 388/388 但 active cycles 仍低估 2.521M。这是明确的
**动态 allocation identity + topology/contention-sensitive kernel service** 缺口，
不能通过初始 PTE 或一个固定 handler latency 解决。

结论：Stockfish 优先修 frontend/ROB-SQ state-transition ledger；NAMD 优先修
measurement 期间的 allocation→first-touch fault identity 和按并发/内存响应驱动的
kernel service，同时用相同 ledger 清除 user 路径 ROB/IQ/SQ 的错误补偿。两者都
不应再调 branch、DTLB 或 data-cache scalar。
