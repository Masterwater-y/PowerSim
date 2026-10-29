# 三项退休 load cache miss 近似模型

按用户确认的范围，主验收项收敛为 SPR `MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS`（D1/08、D1/10、D1/20）。不要求 FB_HIT 精度，不将它列入新的主比较与汇总。原诊断字段保留兼容；不把请求类 L2_RQSTS/LLC miss 改名为退休事件。

## 实现

[RetiredCacheMissEstimator](../include/fastsim/retired_cache_miss_estimator.hpp) 是每核固定 4096 槽的只读观察影子表。输入仅为 FastSim 自身模型已接受的数据 load 描述：物理 line、canonical 请求/响应时刻、缓存路径及明确的 forwarding/alias 标记。每个请求只有固定工作量，不做全流历史扫描或迭代闭合。

- primary load 从自身服务路径给出三个 miss 位，L2 命中只贡献 L1 miss，LLC 命中贡献 L1/L2 miss，memory 路径贡献三个 miss。
- 同 line 请求处于已有 `[issue,response)` 内时估计为 follower，不复制 primary 的三个 miss；无需输出独立 FB_HIT 计数。
- 已明确的 Sequencer alias 同样不重复计 primary miss。完全 forwarding 暂估三个 miss 均为零，记录 fallback，不能宣称该规则覆盖所有特殊 ISA。
- private-only 路径无完整时钟描述时，用原 interval gap 和逐事件 displacement 重建 canonical 请求时刻，并显式标记 fallback；不能把整段末尾的额外延迟加到早期 load。
- OoO 时间回退不会关联尚未发生的 parent；固定表碰撞、未解析路径、空响应窗口保留 fallback。碰撞后被遗忘 fill 的不确定时间上界继续保留，避免后续请求看似完整分类。
- 没有可解析的 read 服务成员或 private level 未知时，本版暂估零 miss 并标记 fallback；这不是全量补齐未知资格。下游必须保留回退数量和原始 unknown，不能仅凭数值非空认定覆盖完整。
- 表只记录 demand-read fill。store prepare 的权限响应和软件预取未证明等同数据 fill，因此不作为该表的 seed。这些省略以及 canonical 时钟近似可能影响点估计，未宣称完整事件重建。

[退休账本](../include/fastsim/perf_retirement.hpp) 只在两阶段最终接受前缀处理这些位，在宏指令完成时按成员 OR 计一次；跨 chunk、跨预热的开放宏保留，缺少完成成员的尾部不补造退休。影子表不参与 cache lookup/替换、队列、时序修正或目标周期，旧资格计数与旧 PMU 不被改写。

## 输出与维护验证

`scope_metrics.retired_cache` 的三个 miss 事件新增：

| 字段 | 含义 |
|---|---|
| `estimated_count` | 新模型的退休宏指令点估计 |
| `estimate_policy` | `bounded-line-fill-v1` |
| `estimate_fallback_loads` | 采用重建时钟、模糊路径等回退的宏 load 数；不是硬件资格缺失的穷尽证明 |
| `estimated_merged_loads` | 含被模型识别为合并 follower 成员的宏 load 数；不是 FB_HIT 精度目标 |
| `count`、`unknown` | 保留原模型正资格/未知诊断，不与点估计混为同一列 |

维护入口新增 `--retired-cache-misses`，隐含 `--retired-cache`。它只把三个 `estimated_count` 与原生退休 cache 已知观察进行比较。原生 unknown、源身份、task、宏/UOP 人口和逐核求和检查全部保留；缺 FB_HIT 精度不阻止三项 miss 报告，错源/人口不匹配仍拒绝。

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root <selected-source-root> \
  --fastsim build/fastsim --config <case-config> \
  --output-dir <new-output> --expected-cases 1 --jobs 1 \
  --max-attempts 1 --retired-cache-misses
```

对已有结果重新评分使用 `--source-root` 与 `--stats-dir`；旧输出没有新估计字段时拒绝，不从 FB/旧请求计数反推。新主报告只显示三个 miss，完整原始 JSON 保留旧诊断字段。

这是模型点估计对不完整原生已知子集的暂定比较，不认证真实 SPR 全量误差。允许模型先近似，不意味着可以将参考 unknown 清零。FLAC 已知 canonical syscall 共同终点的人口缺口必须继续保留，不能为输出误差改分母或改 oracle。

## 审核与验证过程

新回归先 RED，缺少估计器与新入口时失败，再实现并 GREEN。独立只读审查发现并修复私有路径的 post-commit gap 使用错误，以及表碰撞后未保留被遗忘 fill 不确定性的问题。回归涵盖半开响应边界、未来 parent、碰撞、forwarding/alias、跨预热/未完成宏、三项输出、FB 误差不参与、错身份/人口/求和及旧输出缺少新字段。

实现前文件快照、审查收据与实验身份保存在[实验目录](../tmp/retired-cache-misses-20261023/)。无新增负载 FST 或 checkpoint；仍使用本地成功采集的 C8 Zstd rate ×8、FLAC speed、Graph500 speed，同源八通道与 buffer TSO114 配置。

2026-10-23 本轮编译与回归已完成：`cmake --build build -- -j16`、`fastsim_tests`、`fastsim_perf_retirement_tests`、`fastsim_perf_cache_runtime_tests`、`fastsim_perf_cache_integration_tests` 全部通过；新估计器/合同 4 项、相关 Python 合同/来源/计数回归 41 项通过。新增 private-clock 断言最初放在未触发 preview 的三事件用例中，默认 `domain_min_events=2048` 使其绕过目标路径；已移至明确设置 `domain_min_events=1`、并断言 `private_preview_events>0` 的现有用例，重新编译与执行通过。该调整只改变测试，未改实验二进制。

负载验证使用独立冻结的可执行文件、维护工具/字典副本和各负载原配置 include 链。三个源的 `validate_fs_oracle_identity.py` 独立检查为 3/3 有效；运行日志、工具/二进制/完整配置链哈希保存在实验目录，不用当前工作区的后来修改追溯解释旧输出。

三组重放已核对旧输出的全部既有目标字段一致：active/elapsed、旧 cache/branch PMU、缓存状态、调度与线程状态均未改变。只排除宿主秒数/纳秒、吞吐，以及 `acquire_chunk` 中因生产者队列暂空而累计的 `frontier_waits` 和宿主队列水位；FLAC 的 `frontier_waits` 69→14 属于宿主生产者调度次数，目标周期不变。本轮并行且做过进程栈诊断，不作为独立吞吐实验。

磁盘收据：FST 总量 93,851,486,008 bytes（93.85 GB），空闲 371,377,115,136 bytes，低于现行 200 GB FST 上限。未为本轮验证重新采集负载。

## C8 实测结果

维护验证器三案全部结束：2 passed、1 failed、0 pending。Zstd rate ×8、Graph500 speed 的来源/独立退休 cache 人口核对通过；FLAC speed 保留失败。`passed` 只表示这些维护门禁通过，不是 PMU 精度门槛或 SPR 硬件认证。

主比较范围为 `workload_task`，计数单位为退休 load 宏指令。下表误差为 `(模型点估计 − 原生已知计数) / 原生已知计数`。**原生已知子集没有覆盖全部服务资格，所以这些是暂定误差，不能解读为真实全量 perf 误差。**

| 负载 | 事件 | gem5 已知计数 | FastSim 估计 | 相对误差 | 绝对计数误差 | gem5 未知资格数 | 模型原资格未知数 |
|---|---|---:|---:|---:|---:|---:|---:|
| Zstd rate ×8 | L1 miss | 1,081,618 | 1,170,180 | +8.1879% | 88,562 | 145,605 | 1,110,122 |
| Zstd rate ×8 | L2 miss | 304,345 | 336,587 | +10.5939% | 32,242 | 387,747 | 1,110,169 |
| Zstd rate ×8 | LLC miss | 62,546 | 86,618 | +38.4869% | 24,072 | 387,905 | 1,110,273 |
| Graph500 speed | L1 miss | 713,716 | 713,898 | +0.0255% | 182 | 2,004 | 9,352 |
| Graph500 speed | L2 miss | 356,253 | 356,241 | −0.0034% | 12 | 4,179,613 | 9,364 |
| Graph500 speed | LLC miss | 355,360 | 355,462 | +0.0287% | 102 | 4,179,631 | 9,399 |

| 事件 | 有限 APE 案例数 | 暂定 MAPE | 暂定 WAPE | max APE |
|---|---:|---:|---:|---:|
| L1 miss | 2 | 4.1067% | 4.9430% | 8.1879% |
| L2 miss | 2 | 5.2986% | 4.8825% | 10.5939% |
| LLC miss | 2 | 19.2578% | 5.7846% | 38.4869% |

MAPE 对两案等权，WAPE 按参考计数加权，不能互换。Graph500 的 L2/LLC 百分比很小，但原生未知数均约 418 万，不能据此认定完整事件精度达到 0.03%。Zstd LLC 点估计比已知观察多 24,072，而参考仍有 387,905 条未知资格，也不能把 +38.4869% 全部归因于模型多算。Zstd 复用审计还保留 core 5 的 16 个 v2 boundary discontinuity，以及该核五个 scope 的旧 v2 退休人口资格字段缺失，共 46 个 evidence gap；独立 `retired_cache` 逐核宏/UOP 人口匹配不等于这些历史资格缺口消失。

| 负载 | 模型退休 load 数 | 使用 fallback 的宏 load | 识别为 merged 的宏 load |
|---|---:|---:|---:|
| Zstd rate ×8 | 12,221,055 | 1,110,549 | 510,651 |
| FLAC speed | 7,936,166 | 869,360 | 84,995 |
| Graph500 speed | 17,075,672 | 10,673 | 5,893,491 |

fallback/merged 可重叠；fallback 包括 forwarding、重建时钟和模糊路径，不等于全部不确定性的上界。merged 是内部去重诊断，不是新的 FB_HIT 验收项。本轮 L1/L2 点估计与旧正资格计数恰好相同；LLC 分别比旧计数增加 104、6、15。不能把新增输出描述为已经消除了既有缓存精度误差。

FLAC 三项模型估计为 **67,756 / 176 / 86**，仅作模型计数。其 core 0 独立 native 退休宏数比 canonical FST 少 1 条，维护入口报 `retired cache per-core functional population mismatch`，不输出 PMU 误差。保留原共同终点、原计数与原始失败证据，没有通过改分母或补造退休消除拒绝。

### CPI 与运行时间中性核对

本次没有调整时序模型；三案新旧 active CPI、elapsed 与所有既有目标字段逐值一致。下面重列当前时间合同结果，FLAC 的 PMU 人口失败仍单独保留，不能将整案称为全部通过。

| 负载 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI 绝对误差，cycles/宏指令 | gem5 elapsed，ms | FastSim elapsed，ms |
|---|---:|---:|---:|---:|---:|---:|
| Zstd rate ×8 | 1.525718 | 1.389485 | −8.9291% | 0.136233 | 2.648757 | 2.412249 |
| FLAC speed | 0.449687 | 0.471898 | +4.9392% | 0.022211 | 0.670171 | 0.703272 |
| Graph500 speed | 2.056529 | 1.838376 | −10.6078% | 0.218153 | 3.411999 | 3.050060 |

三条时间合同记录的 CPI MAPE 为 **8.1587%**，CPI MAE 为 **0.125533 cycles/宏指令**；没有因本次统计改动而改善或退化。elapsed 表示目标运行时间，不是宿主验证耗时。

原始证据：

- [维护入口汇总 JSON](../tmp/retired-cache-misses-20261023/final/summary.json)、[三项 miss 汇总](../tmp/retired-cache-misses-20261023/final/retired-cache-summary.md)、[计数 JSON](../tmp/retired-cache-misses-20261023/final/primary-cache-counts.json)。
- [既有目标字段中性核对](../tmp/retired-cache-misses-20261023/final/target-invariance.json)、[独立身份核验](../tmp/retired-cache-misses-20261023/identity.json)、[完整配置链哈希](../tmp/retired-cache-misses-20261023/config-chain-freeze.json)。
- [编译与测试收据](../tmp/retired-cache-misses-20261023/tests-summary.json)、[最终源/二进制哈希](../tmp/retired-cache-misses-20261023/verified-source-freeze.json)、[FLAC 原始失败](../tmp/retired-cache-misses-20261023/validation/flac/cases/c08-817.flac_s/validation.json)。

本轮完成的是三项 miss 的独立近似输出、维护比较入口和真实负载验证。原生未知服务资格、Zstd LLC 残差、FLAC 共同终点人口问题，以及 SPR 真机全量等价验证仍未解决。

## Zstd LLC miss 来源分解（2026-10-23）

本次只分析冻结输出和原 FST，没有改生产模型、重采负载或测量新 CPI/吞吐。重新独立核验目标身份通过；维护报告指纹绑定的所有当前输入哈希一致，八核 FST 的 size/mtime/ctime 与原审计收据一致。参考仍直接取自维护验证器保存的独立 retired-cache 与 Ruby/SLICC task 审计，不使用 path-class 字段。诊断产物在 [zstd-llc-miss-diagnosis-20261023](../tmp/zstd-llc-miss-diagnosis-20261023/)。

### 已确认的计数事实

| 对照域 | gem5 | FastSim | 净差额 | 相对误差 |
|---|---:|---:|---:|---:|
| 退休 load LLC miss：原生已知子集 vs 新点估计 | 62,546 | 86,618 | +24,072 | +38.4869% |
| 退休 load LLC miss：原生已知子集 vs 旧正资格计数 | 62,546 | 86,514 | +23,968 | +38.3206% |
| 独立 native LLC tag miss 诊断 | 71,314 | 95,237 | +23,923 | +33.5460% |

新估计器的总数净增 104 次，占当前净差额 0.432%。主要净差额在新增三项近似输出之前已经存在；不能归因于这次影子表引入了大规模净偏差。两个事件域的净差额只差 149 次，说明应同时检查模型缓存路径，不能仅凭 retired-cache unknown 多就结束排查。但 tag 诊断与退休 load 单位不同，149 的净差也可能包含正负抵消，**不能据此宣称已完成 99.4% 的逐事件因果归因**。

| 核 | 原生退休 LLC 已知 miss | 模型点估计 | 退休净差额 | 独立 tag miss 净差额 |
|---|---:|---:|---:|---:|
| 0 | 6,534 | 9,541 | +3,007 | +3,018 |
| 1 | 6,620 | 9,647 | +3,027 | +3,036 |
| 2 | 6,668 | 9,628 | +2,960 | +2,937 |
| 3 | 6,875 | 9,813 | +2,938 | +2,944 |
| 4 | 6,569 | 9,640 | +3,071 | +3,088 |
| 5 | 16,121 | 19,144 | +3,023 | +2,848 |
| 6 | 6,564 | 9,612 | +3,048 | +3,055 |
| 7 | 6,595 | 9,593 | +2,998 | +2,997 |

退休差额在每个副本都约 3,000，去掉 core 5 仍有 +21,049。core 5 用户预热仅 65,127 条宏指令，其他七核约 161万～179万；它在两侧的 miss 都更高，但没有贡献更大的净差。该核的 16 个 v2 boundary discontinuity 需要修复，不能拿来解释所有核的系统偏差。

### 功能覆盖将方向收窄到首次填充状态

复用维护工具 `audit_fst_warmup_cachelines.py` 的物理行提取函数，按原八核 record 边界扫描 user+kernel warmup 与 measurement：

- warmup 全局共触及 120,283 条唯一物理行。
- measurement 共触及 308,180 条唯一物理行；其中 192,516 条不在任何核的 warmup 中。
- 去掉一条位于配置 DRAM 范围之外的行（`0x2000000000002300`），剩 **192,515** 条，恰好等于 FastSim 的数据 LLC `unique_fills=192,515`。
- FastSim 测量期 LLC `evictions=0`、`writebacks=0`，LLC 64 MiB/16-way/LRU 与采集配置一致。

这强烈指向模型如何对待“功能流中首次看见的缓存行”，不支持优先通过扩大 LLC、改变 LRU 或调整 DRAM 时延消除该差额。覆盖扫描包括 NO_ACCESS/预取热记录，没有重建请求到达顺序，也没有逐行配对 native first-fill；因此上述精确总数相等仍是聚合证据，不能称为每一条真实 miss 的服务证明。它也不意味着“多预热一段就会解决”：这些行是在原测量段才出现，延长窗口会改变研究对象。

### 具体机制候选：提前 prepare 的缓存作用未完整进入规范分类

gem5 `LSQUnit::issueStorePrepareRequests()` 在允许的 buffered store 上生成独立 `STORE_PREPARE` 请求；Sequencer 将其送入 prefetch queue，L0 在 I 状态可分配块并发出 `GETX`。这是权限预取，也能带入数据，不能因为普通硬件 prefetch 关闭就认为它不存在。本次 native 全核测量统计为 **5,696,216 次 prepare issued**，其中 **264,467 次 L0 I→Store_Prepare 转换**；后者不是 LLC miss 数，也不是已测得的 load 受益量。

当前 bounded 路径先规范分类 cache/uncore，再在核心前向遍历中解析估计 prepare/接管/重取权时间（本案 estimated prepares=5,582,305）。这些时间估计没有完整地把真实提前到达的 prepare fill 放到更早的 load 分类之前。于是存在这种差异：gem5 的 prepare 已先带入 line，后续 load 命中；模型按规范功能顺序首次处理该 load 时仍将其判为 memory 路径，增加退休 LLC miss。新三项影子表仅识别 demand-read fill，也不会凭空恢复漏掉的 prepare 数据填充。

**这已经是明确的模型机制缺口，但它对 +24,072 的独立贡献尚未量化。** 旧 legacy+证书与 bounded 的 LLC tag miss 都是 95,237，说明简单切回旧证书/prepare 时序开关没有修复这一分类差异。旧输出的 `buffer_tso_prepares` 另有 unsigned 下溢，本轮不使用该字段量化 prepare 作用。

错误路径上的数据请求、真实指令/PTE 请求对缓存的提前带入、以及 warmup 前已存在的 native 状态，也可能让退休 load 在 gem5 中命中。这些都不会直接出现在完整 committed 指令流中；当前汇总没有逐行证据，不能将它们任一项宣布为唯一根因。原生仍有 387,905 条 L3 未知资格，38.4869% 继续保持暂定性质。

下一步应对额外 miss 的 `(core, ordinal, physical line)` 做稀疏服务配对：native 记录该 load 前最近一次 line fill 的来源（prepare、退休 demand、squashed demand、ifetch/PTE、窗口前状态）与完成先后；模型输出同一 load 的分类及已有 fill 预测。该 sidecar 只供验证，不能作为 FastSim 输入。先量化 prepare/错误路径/边界分别覆盖多少差额，再选择有界的一遍近似；不直接给 Zstd 减去约 3,000 miss/核，也不通过补长 warmup 或调常数凑数。

机器可读证据：[逐核分解与哈希核对](../tmp/zstd-llc-miss-diagnosis-20261023/breakdown.json)、[功能物理行覆盖](../tmp/zstd-llc-miss-diagnosis-20261023/coverage.json)、[本轮身份核验](../tmp/zstd-llc-miss-diagnosis-20261023/identity.json)。

## 稀疏 LLC fill 配对实施与首轮验证（2026-10-25）

已接通 native 实际 fill → 原始 FST 成员 → FastSim 最终接受成员的独立诊断链路。
首轮真实八核 Zstd 样本发现明确的错误路径提前填充见证；尚未修改 cache 分类或 miss 预测，不能将本轮称为 LLC 精度修复。

### 实现及证据边界

- gem5 在 LLC 收到实际内存数据、完成填充时输出物理行、来源角色和原始请求身份；实际逐出另行输出。角色沿 RubyRequest、协议消息及 TBE 保留，区分 data、RFO、code、PTE、软件预取、prepare。当前目标关闭硬件预取，硬件预取角色预留，未做完整生产验证。
- TaoTrace 输出 raw FST ordinal、PC、物理行、阶段、独立 native 服务事实及明确的 squash 序号范围。仅未见退休的请求保留 unresolved，不直接视为错误路径。流结束校验所有核的共同终点和全量 hot-record hash。
- FastSim 只对最终接受前缀输出已有分类及三项 miss 估计，同时闭合每核全量 accepted raw-record 人口。sidecar 不进入功能输入，也不反馈 cache、队列、PMU 或目标时序。
- 默认关闭；物理行号按 stride16 固定采样，最多 400 万行、1 GiB，任何省略、截断或未成功结束均拒绝配对。通用 writer 在异常析构时输出 `completed=false`。
- 离线工具只使用 admission 之前已完成且未被逐出覆盖的最近 fill。admission 同 tick 的事件保留为不确定；in-flight fill 与 future fill 单列。跨缓存行 UOP 的 native 服务是聚合事实，不能分派成逐行真值，因此排除 split 成员。
- 配对核验 FST/功能 companions 的完整内容身份、每核 accepted 人口和共同终点、每条成员实际 PC/flags/phase/物理行，以及 native 热流 hash；模型 sidecar 的 SHA/大小绑定到实际 FastSim JSON，JSON 再绑定到维护验证报告。采集后另封存 binary、runner、命令、配置、receipt、trace 元数据、线程计划和全部 oracle 文件，拒绝缺项、错案规范路径或命令未绑定的 binary/runner。

主要入口为 [native 观察器](../gem5-collection/overlay/src/mem/llc_fill_audit.hh)、[模型观察器](../include/fastsim/llc_fill_audit.hpp) 和 [离线配对工具](../tools/pair_llc_fill_audit.py)。
开启 native 用 `FASTSIM_NATIVE_LLC_AUDIT=<path>`；开启模型用 `FASTSIM_LLC_AUDIT=<path>` 且必须启用 `--perf-cache-input-identity true`。采样参数为 `FASTSIM_LLC_AUDIT_STRIDE`，两侧必须相同。配对需要已经通过维护验证且保留完整封存清单的案例。

### 首轮真实来源与中性核对

实验目录为 [llc-fill-pairing-20261025](../tmp/llc-fill-pairing-20261025/)。使用官方 Zstd rate ×8、8 核、8 内存通道、3 GiB、buffer TSO/114，沿用已确定的计算 ROI、KVM→warmup→stat 和 first-core 共同终点流程。
为控制诊断成本，测量目标缩短为首核 200 万 user UOP；这与之前 1000 万目标的原实验不同。warmup 墙钟上限仍为 900 秒，开启组实际 detailed warmup 为 243.01 秒，总采集 706.84 秒。

旧 checkpoint 严格绑定旧 gem5 binary，未绕过身份检查。先保存新 binary 对应的 staged KVM checkpoint，再并行恢复开启/关闭观察器两组；原 checkpoint 保留。两次恢复后新 checkpoint 的全部 payload SHA256 与保存收据一致。
两组维护验证均通过；开启组独立硬件身份检查为 1/1 valid，逐核功能人口、native 来源及 drain/守恒检查通过。core5 旧 v2 资格仍有 11 次 boundary discontinuity 等缺项，没有将其补零或宣称消失。

两组 gem5 恢复生成的完整功能内容身份不同，因此不能拿其计数差或宿主用时作为 gem5 观察器严格中性/开销证据。另对**同一份开启组 FST、相同 FastSim binary/config**关闭模型观察器再跑一次，所有目标字段逐值一致，包括 active/elapsed、退休与请求 PMU、cache/core/CHA；19 处差异均为宿主墙钟、吞吐、frontier wait 次数或观察文件元数据。该单次复验不是受控吞吐开销实验。

### 配对样本结果

| 项目 | sampled load-member cache-line 数 |
|---|---:|
| 测量期模型成员行 | 188,838 |
| 排除 split 成员 | 5,637 |
| native 已知资格成员 | 174,016 |
| native 未知资格成员 | 9,185 |
| 已知真阳性：双方判 LLC miss | 1,000 |
| 已知假阴性：仅 native 判 LLC miss | 8 |
| 已知假阳性：仅模型判 LLC miss | 77 |

77 个已知假阳性的最近 native fill 来源为：**50 个明确 squashed demand（64.94%）、24 个 unresolved demand（31.17%）、3 个 committed demand（3.90%）**。
这里的“来源”表示同一行在 native admission 前的实际填充祖先，不是反事实实验中该机制的独立净贡献。错误路径请求虽然不退休，也会把行提前放进 LLC；后来正确路径的 load 因而命中，而只重放 committed 流的模型可能仍按首次访问判 miss。这是当前已拿到的具体机制见证。

采样流实际记录了 5,325 次 prepare fill、429 次软件预取 fill 和 227 次 code fill，说明这些来源链路已在真实协议中接通；但本轮 77 个已知假阳性的最近 fill 没有落入这些类别，**不支持将 prepare 宣布为这组样本的主要来源，也不证明其在原长窗口中无影响**。
未知资格的 9,185 个成员中有 148 个被模型判为 LLC miss，不能归入已证明假阳性。另有 1,960 个 native 带 read 标记的测量成员未输出模型 load 行，配对入口没有发现其中存在已知资格 load 缺失；全量 accepted 源记录人口仍闭合。所有这些边界保留在 [配对 JSON](../tmp/llc-fill-pairing-20261025/pairing.json) 和 [覆盖收据](../tmp/llc-fill-pairing-20261025/sample-coverage.json)。

本表计数单位是采样的 load 成员行，下面主 PMU 是退休宏指令。不能直接相减或乘16；不能把50个见证外推为原长窗口约24,072次差额的解释比例。

### 短诊断窗口的精度记录

以下直接引用维护验证入口的 workload-task 结果；cache 参考仍为独立 native retired-cache 已知子集，逐事件 unknown 如实列出，SPR 硬件等价未认证。

| 退休 load miss | gem5 已知计数 | FastSim 点估计 | 有符号误差 | native unknown |
|---|---:|---:|---:|---:|
| L1 | 255,209 | 272,474 | +6.7650% | 35,342 |
| L2 | 54,030 | 58,502 | +8.2769% | 76,901 |
| LLC | 21,566 | 26,450 | +22.6468% | 76,983 |

| gem5 active CPI | FastSim active CPI | 有符号误差 | CPI 绝对误差，cycles/宏指令 | gem5 elapsed，ms | FastSim elapsed，ms |
|---:|---:|---:|---:|---:|---:|
| 1.371044 | 1.333303 | −2.7527% | 0.037741 | 0.496151 | 0.482494 |

只是一条短窗口记录，不与原长窗口汇总计算“改善率”。当前 22.65% 不能替代原实验的 38.49%，两者来源、窗口及 warmup 状态不同；本轮没有实施模型修复。

### 验证、审查与空间

gem5 按 affinity192 的80%即153 jobs 编译，FastSim 完整编译通过。最终 `fastsim_tests` 通过；三项专门 C++ retirement/cache runtime/integration 测试通过；本轮最终聚焦 Python 回归49/49通过，其中新观察/配对/CLI测试16项。保留了失败日志：首次 gem5 构建缺 libpython、一次直接 C++ 测试缺新 libstdc++，补齐既定运行库路径后通过，均非掩盖测试断言失败。
独立复审修复了未成功运行也可能闭合、split UOP 真值误分派、同 tick 歧义，以及封存清单漏项/别案路径的拒绝缺口；对应 RED→GREEN 回归已保留，最终复审无剩余 Critical/Important。

本次新增两份 FST 合计5.45 GB；当前仓库 FST 总量 **99.30 GB（99,301,086,904 bytes）**，低于200 GB上限，剩余磁盘约350.88 GB。未删除原成功 FST 或原 checkpoint。两份诊断 sidecar 分别约220.40/126.61 MB，均完整结束、无截断。

证据入口：[独立身份核验](../tmp/llc-fill-pairing-20261025/on-identity.json)、[维护验证](../tmp/llc-fill-pairing-20261025/validation/on/cases/c08-777.zstd_r/validation.json)、[同源目标中性](../tmp/llc-fill-pairing-20261025/same-source-target-invariance.json)、[checkpoint 复用核对](../tmp/llc-fill-pairing-20261025/checkpoint-reuse-verification.json)、[空间收据](../tmp/llc-fill-pairing-20261025/storage-after.json)、[最终聚焦回归](../tmp/llc-fill-pairing-20261025/final-focused-python.log)。

下一阶段应扩展样本和已知资格覆盖，量化错误路径提前填充在原长窗口的适用范围，再评估只依赖功能输入的一遍有界近似。当前证据不支持按负载减常数、把未知成员当命中，或从 native sidecar 向模型回灌服务结果。

## SPR 全量分类与零未知要求（2026-10-26）

用户要求三个主事件依据 SPR 真机 perf 口径全部分类、不允许 unknown。
因此本文前述含 unknown 的百分比，包括排除 Zstd 后的历史均值，仅保留为子集诊断，
不再作为全量主精度结果。当前 producer 仍未达到零未知要求，不能把这个要求写成已完成修复。
控制性规则已追加到 [报告合同第0节](accuracy-reporting-contract.md#0-mandatory-source-selection-before-evaluation)。

重新核对 Intel SPR 官方事件 JSON，版本1.40、发布日期2026-09-09；三项主事件分别为
D1/08、D1/10、D1/20。计数单位是合格退休 load 指令，而不是缓存行请求；官方对
L1/L3 miss 使用至少一个 UOP 满足条件的描述。多读和跨行指令必须按目标指令规则归并。
官方来源：[SPR core events](https://raw.githubusercontent.com/intel/perfmon/main/SPR/events/sapphirerapids_core.json)。

本次使用维护的 `tests/workloads/perf_cache_probe.c`，在现场 Xeon Platinum8457C
family6/model143/stepping8、CPU0上补测5个内核、4组事件、每组3次，共60次。
每次3个raw事件加instructions，当前线程、仅用户态、不继承，固定CPU；60/60次
`time_running == time_enabled > 0`。下表为每轮迭代的计数中位数，保留少量测量开销。

| 内核 | retired loads | L1 miss | L2 miss | L3 miss | FB hit |
|---|---:|---:|---:|---:|---:|
| 热 aligned load | 1.000061 | 0.000008 | 0.000000 | 0.000000 | 0.000017 |
| 热 store→load | 1.000072 | 0.000007 | 0.000000 | 0.000000 | 0.000000 |
| flush 后两个同址 load | 2.001435 | 0.999900 | 0.999760 | 0.999760 | 0.999740 |
| 只 flush 后半行的跨行 load | 1.001410 | 1.000170 | 0.999980 | 0.999970 | 0.000060 |
| 双读 CMPS 指令 | 1.000086 | 0.000010 | 0.000000 | 0.000000 | 0.000004 |

三项 miss 同组测量；其他组的 loads/FB 等是重复实验，不能声称是同一次执行的逐条对应。
冷行双 load 的结果支持 follower 不应直接复制 parent 的全部 miss；不等于所有合并路径
已获得逐条硬件归因。热 store→load 结果没有通过PEBS证明每次都实际转发，不能从该
内核单独推出任意部分转发、失败重试或混合 fragment 的规则。跨行及 CMPS 结果说明
不能按数据行数或读UOP数重复计退休 load。

目前缺口位于自建观测链：Ruby 原生事实 → 请求/fragment/attempt及合并关联 → 退休宏
指令资格。`classifyRetiredCache` 仍对 forwarding、coalescing、other outcome 返回未知；
source ledger 对 unsupported/boundary/unresolved 成员保守标未知。
下一步需要补齐逐路径的事实与事件映射，并用真机/原生协议对照验证，不能仅删除这些分支。
特殊指令、远端供给及测量边界仍需专项验证，本次没有完成 producer 改造、gem5 重采或
全负载全量 PMU 验收。

原始证据：[60次运行](../tmp/spr-retired-zero-unknown-20261026/hardware-runs.json)、
[计数汇总](../tmp/spr-retired-zero-unknown-20261026/hardware-summary.json)、
[硬件、二进制及测量身份](../tmp/spr-retired-zero-unknown-20261026/hardware-identity.json)。
