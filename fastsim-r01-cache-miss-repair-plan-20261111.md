# 当前冻结版 cache miss 误差修复方案

日期：2026-11-11。状态：原始方案；用户已授权实施，当前结果见
[实施记录](fastsim-r01-cache-miss-repair-progress-20261111.md)，尚未完成全范围精度/吞吐验收。

本方案针对当前恢复的 R0/R1 `v2-wait`，覆盖三个退休 load miss 与独立 native tag miss 诊断。采用逐项修复、逐项同输入验收；已有 R4 `merge/publish` 实验不是候选基础。先补证据，再分别修观察器和功能请求路径，不把时长重算、容量和延迟 Tag 发布混合集成。

## 1. 固定基线与修复目标

### 1.1 身份与约束

- 冻结二进制 SHA-256：`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
- 主目录 `build/fastsim` 与冻结二进制一致；候选在新的隔离源码和构建目录中实施，生产源码、二进制和冻结快照均保留。
- 原始冻结源：`tmp/parent-service-v2-r01-exclusive-20261110/source/`。每个候选记录父版本、源码/二进制哈希、完整配置链、FST/companions 身份。
- 同输入集合：`tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/` 下 Zstd、XZ、FLAC、Graph500、Marian 五例，四核，`workload_task`。
- 本批硬件预取与退休前 `sprEarlyStorePrepare` 关闭；退休 store prepare、软件预取及现有功能请求行为保留。
- 继续使用并行两阶段框架及退休功能流。gem5 服务、命中、发起/响应时间仅供独立诊断，不进入 FastSim 模型输入。
- 每个行为变更单独验收。观察器、forwarding、请求计数/服务归属、cache 状态行为不得在首个候选中一起修改。

### 1.2 当前 cache 误差

表内均为有符号相对误差 `(FastSim−native)/native`。退休事件是宏指令人口，请求 tag miss 是原生请求人口，两者分别验收。

| 负载 | 退休 L1 | 退休 L2 | 退休 L3 | 请求 L1D | 请求私有 L2 | 请求 LLC |
|---|---:|---:|---:|---:|---:|---:|
| Zstd | +50.1912% | +109.0888% | +122.6227% | +22.2082% | +20.5815% | −2.4600% |
| XZ | −55.5467% | +3.4365% | +3.2281% | +7.9657% | +5.4004% | +1.0088% |
| FLAC | −20.1977% | +9.8282% | +24.8485% | +41.5823% | +35.4014% | −27.8337% |
| Graph500 | +27.9210% | +0.0104% | +0.0025% | +0.0174% | +0.0108% | +0.0032% |
| Marian | −31.5316% | +3.1412% | +2.1026% | −9.3776% | −7.8487% | −50.4651% |

原始计数、绝对计数误差和来源见[归因报告](fastsim-r01-retired-cache-miss-attribution-20261111.md)及[结构化证据](fastsim-r01-retired-cache-miss-attribution-20261111.json)。

### 1.3 CPI 保护基线

下面是四核 workload-task active CPI，AE 为 `abs(FastSim CPI−gem5 CPI)`，单位 cycles/macro。

| 负载 | gem5 CPI | 冻结 CPI | CPI AE | CPI 相对误差 |
|---|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 0.063733 | −5.28% |
| XZ | 0.575297 | 0.601359 | 0.026062 | +4.53% |
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.89% |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | −13.51% |
| Marian | 2.364997 | 3.068823 | 0.703826 | +29.76% |

五负载等权 CPI **MAE 0.191599 cycles/macro，MAPE 12.994957%**。观察器修复应保持模型 cycles/CPI 完全一致；可能改变功能请求的修复另行对照，不接受仅凭平均 CPI 改善掩盖单负载恶化。

这些是当前基线，不是新方案收益预测。本方案不预先承诺某个误差降幅。

## 2. 修复顺序与独立候选

| 顺序 | 工作 | 优先对象 | 行为范围 | 完成依据 |
|---|---|---|---|---|
| A0 | 补全可追溯的模型成员/区间审计 | Graph500、XZ core 0 | 只观察 | 开关审计前后预测、CPI、请求计数完全一致 |
| A1 | 统一观察器的时间来源与 fill 身份 | Graph500、XZ，再检查 Marian | 退休 miss 观察器 | 多计/漏计见证闭合，五例同输入回归 |
| A2 | 单独修局部 fill 消费与数据可见结束边 | 对 A0 已证实的问题 | 退休 miss 观察器 | 数据边与 callback 不混用；不改 target timing/cache |
| B1 | 增加 forwarding 的有界存活条件 | Zstd、XZ、FLAC | 功能请求路径 | 转发资格、cache/request、CPI 分别验收 |
| C1 | 修请求与退休服务的归属/计数接口 | Zstd 下层、Marian LLC | 首先计数/观察，再按证据拆功能候选 | 逐事务守恒和逐宏成员配对 |
| C2 | 修已证实的缓存请求行为偏差 | Zstd/FLAC 私有侧、Marian LLC | 每项单独功能候选 | 路径、资源与 CPI/PMU 同输入验收 |
| D | 五例集成与独立吞吐验证 | 全部 | 已分别验收的修复 | 完整覆盖、逐例误差和性能结论 |

A1/A2 内部仍需分开验证时钟、身份和结束边，不作为一个批量变更。A0 的 native 补测可与模型观察器证据建设并行；功能修复只从最后一个已验收候选继续派生。

## 3. A0：先定位每一次计数改变发生在哪里

### 3.1 需要记录的模型事实

在 `account_perf_retirement` 调用 `RetiredCacheMissEstimator::observe` 的前后，记录：

```text
功能身份：
  source_identity, core, task, raw_record_ordinal
  macro_anchor, uop_member, fragment, physical_line
  role, byte_range, flags, measurement/warmup

自身服务：
  own_private_level, own_miss_mask, forwarded, sequencer_alias
  parent_id/generation（存在时）, parent_role
  pre_resident, functional_carried

区间决策：
  observer_clock_domain/epoch, issue_origin
  fill_slot, fill_generation, fill_begin, fill_end, end_edge_kind
  matched/expired/future_parent/collision/overwritten/no_interval
  own_mask_before, final_mask_after, estimated_bits, fallback_reasons

宏归并：
  最终宏 miss/unknown mask，参与的功能成员
```

没有可靠事实的字段显式 unavailable，不能用 0 或猜测的 parent ID 补齐。功能 raw-record ordinal 与退休 macro 序号是两个单位，必须通过宏边界元数据映射；不能直接互换。

模型时间字段均来自现有 replay/feedback，不读取 native 动态时序。诊断关闭时无日志分配/I/O。全量计数器按固定字段累计，样本采用有界容量；被截断的日志不得宣称全量逐成员配对。

### 3.2 复用审计框架的边界

已有接口可复用：

- `include/fastsim/llc_fill_audit.hpp` 与 `llc_audit_writer.hpp`：accepted-prefix 身份、成员日志、固定采样和 closure。
- `src/simulator.cpp::account_perf_retirement`：最终接受的功能人口，宏 OR 与三层 mask。
- `tools/pair_llc_fill_audit.py`：来源、raw ordinal、PC/flags、line、warmup/共同终点及 capture binding 校验。

但是现有配对工具主要比较 L3 member，排除 split、atomic、写成员，且原判定使用旧 `source` 位；不能直接当作三个 SPR 退休 miss 的完整验收。需要在维护工具体系中新增/扩展独立的 SPR 三层配对模式，采用 native `spr_miss_mask/spr_unknown_mask` 和宏 OR，不改变既有模式的口径。

当前五例的归因证据没有绑定完整的逐成员 native fill 审计；已检查的 XZ 源目录保留的是原生聚合 sidecar，未见该独立审计文件。因此：

1. 先检查其他例是否保存了身份匹配、未截断、已封存的逐成员证据。
2. 对旧五例可立即完成模型决策审计和聚合回归，但不能靠聚合表重建不存在的 native 逐 load 真值。
3. 若需新增 native 诊断采集，形成独立新源集；冻结版和候选均在该新源上回放。新采集不能冒称原五例相同 FST，也不能将新旧误差直接相减当收益。
4. native 审计放在 oracle 侧，独立身份/来源验证；不得添加进模型 timing 输入。

XZ/Marian 少量 native unknown 按现有用户授权继续列入暂定报告，保留原门禁失败及 unknown 区间。配对模式必须分别验证来源/人口门禁与事件资格；不能把失败的验证报告改写为 `passed`。未知成员保留在未知桶，不凭空填零。

### 3.3 输出与不变量

输出每层的 TP/FP/FN/unknown，以及按决策原因、核、角色、split/retry 分组的差额。只有身份匹配且完整覆盖的组才能称为全量；采样组明确标 sampled，不外推成精确总数。

同一完整已知宏人口内：

\[
P-N=FP-FN.
\]

未知桶单列，不能隐式套用上述等式。模型决策计数、宏 OR 结果与原 15 个主事件全量预测必须守恒。审计-only 候选开关前后 CPI、native request 预测和退休 miss 预测完全一致。

## 4. A1/A2：先修 L1 观察成员，保持冻结服务时间

### 4.1 为什么优先这两例

- Graph500 L1 多报 1,369,794 次，其中账目上的新增修正差额为 1,369,764 次，旧资格对 native exact 仅差 30。三层请求 tag miss 都在 0.02% 内。
- XZ core 0 的 native follower 为 109,211，模型 merged 为 1,512；该核 L1 差 −107,719，而旧资格对 native exact 仅差 −20。
- 二者分别提供明显的多计与漏计见证，可以发现“统一缩短区间只改善一个负载、恶化另一个”的错误修复。

这些是定位优先级，不证明具体数据边已经测出。

### 4.2 独立修复项

**A1a：统一观察时间的来源和可用性。**

明确局部 hit、私有 L2 fill、LLC/DRAM fill 的 issue/ready 来自哪个模型域和 epoch。用显式 available 区分“合法周期 0”和“无共享时间”，避免仅靠非零判定时钟存在。跨域通过现有 Q16 映射转换；观察器不修改请求准入、shared response 或 CPU timing。

**A1b：区分 line、数据 fill 和服务代次。**

影子槽在现有固定容量内保存 `(core/domain, physical_line, generation, fill_kind)`。优先复用已有 Sequencer/parent generation，不迁移整个服务池；无身份路径保持明示近似。权限升级、store prepare、软件预取和 demand 数据 fill 不相互冒充。

**A2a：区分 L1 数据可见边与请求响应边。**

slot 的消费结束使用已有模型能够提供的数据可见事实/明确近似。仅在证据表明二者不同的路径上替换 callback 上界；没有可靠独立数据边时保留冻结近似并标记来源。禁止统一给所有 load 重算完整配置时长或统一平移 shared response。

**A2b：修局部 PendingFill 与 alias 消费。**

同线后来者只有在身份、代次和可见性条件匹配时算 L1 PendingFill；排除其自身未发生的下层查找。Seqencer alias 本身不证明数据未驻留；hot parent 和正在回填的 cold parent 分开。`private_level==L1` 也不独立证明数据在当前观察时刻已可见，因为它可能来自功能 lookahead。

**A2c：处理测得的碰撞影响。**

先记录被覆盖 line 与后续消费，确认容量/映射是否贡献漏报。只有贡献被证实后才尝试固定容量的小组相联等方案；不能先无限扩大表，也不能把当前 collision 标记数量当遗漏 follower 的严格上界。

### 4.3 有意义的反例与验收

定向场景应覆盖：

- cold owner → fill 内 follower → 数据可见后 hit；
- issue 恰好等于 begin/end、周期 0、跨 epoch/carry；
- 响应晚于数据可见，数据与权限完成分离；
- hot Sequencer alias、prepare 权限服务与数据 fill；
- 同线新代次、失效/替换、碰撞后访问；
- 完全/部分 forwarding、split、多读 UOP、retry sticky miss、宏 OR。

已有 `test_retired_cache_miss_estimates.py` 与 `test_spr_retired_load_miss_v2.py` 保留，补上当前真实误判的最小见证。测试证明规则，五例回归证明组合效果，二者不能替代。

每个观察器候选的 target cycles/CPI、功能 cache/request 计数必须与其父版本完全一致；退休 miss 改善用 FP/FN 或明确完整分组证明。五例每层 absolute count error 不得恶化；若改善一个负载而恶化另一个，该候选不推广，返回证据分析。

## 5. B1：独立修 forwarding 的结构窗口过期

### 5.1 已知模型差异

`store_forward_window` 按最近 store 的完整 byte range 匹配，并按 SQ 容量、fence/serialize/RMW 淘汰，不按实际排空 callback 过期。模型/native 的 forwarded load 宏数分别为：

| 负载 | 模型 | native |
|---|---:|---:|
| Zstd | 855,374 | 291,257 |
| XZ | 2,501,991 | 679,348 |
| FLAC | 667,894 | 331,041 |
| Graph500 | 3,943 | 870 |
| Marian | 3,369,575 | 3,398,424 |

数量差不是 miss 差。被额外转发的 load 原本可能 hit、PendingFill 或 cold miss，且请求变化会影响后续缓存状态。XZ 全负载转发差主要不在 core 0，不能用它代替 A1/A2 的核心问题。

### 5.2 修复方式

- 每个结构窗口项绑定原 store 身份、完整 byte range 与有界模型存活条件；保留固定 SQ 容量。
- 使用已有 store 服务的模型排空边/资格摘要判断条目是否仍可转发；若功能前瞻阶段尚无最终边，先保留候选转发意向，在现有 feedback/接受边界验证。
- 如果反馈验证失败而请求先前已被消除，必须在现有两阶段框架内通过有界修复路径补回普通访问及对应状态/计数；不能仅给退休事件加一个 miss 位。
- 数据可用、store 仍在队列、完整 byte 覆盖同时满足才可完全 forwarding。partial forwarding、跨行、原子与屏障独立处理。
- 不根据 workload 名称、native forwarding 数量或 gem5 SQ 生命周期设置参数；不把结构容量换成无限历史窗口。

这项可能影响功能请求、Tag miss、CPI，必须作为单独候选。若需要的事实在当前阶段不可获得，应明确建模近似及边界，不能将新增详细 SQ runtime 当作修复。

## 6. C1/C2：按角色闭合 Zstd 与 Marian 的请求账本

### 6.1 Zstd：先查退休下层归属，再查私有功能路径

LLC 请求误差只有 −2.46%，退休 L3 多报 32,392 次；L1 回填修正对模型 L3 的净改变为 0。应先检查 demand load 是否错误继承 store/prepare 的下层 data miss、权限路径是否冒充数据取数、软件预取/fragment/retry 是否错误归并。

每个事务记录 `initiating_role`、`consumer_role`、该消费者实际发生的 lookup level、供给关系、data/permission 分类。对有证明的子请求关联应用共享 SPR 规则；不要将父请求三层 miss 复制给所有消费者。

确认退休归属后，再独立检查 L1D +22.21%、私有 L2 +20.58% 的请求偏差。按命中/未命中/升级/合并/重试分别守恒，依据测得的规则错误修正；不能只改 Tag 统计而保留错误 cache 状态。

### 6.2 Marian：从 LLC 少报的 227,406 次请求查起

按 data-demand load、store demand、RMW 和其他实际纳入计数的请求角色拆分；prepare、软件预取等若不在该计数人口中，作为间接状态/供给来源记录，不能混入分母。进一步区分 tag miss、upgrade、merge、remote supply 和 carry/retry。

核验：

1. 是否原本应有唯一 demand transaction，却因 prepare/takeover/Seqencer alias 错误消失；
2. 是否请求已真实发生但计数遗漏、角色归属错误；
3. 是否正确的合并/远端供给被错算成 hit，或错误状态使本来独立的请求不再访问 LLC；
4. 同一 service 的 fill/release/count 是否一次，carried replay 是否重复或遗漏。

当前尚未证明上述哪项贡献多少。计数遗漏与功能路径变化必须拆成不同候选；即使退休 L3 仍接近也要独立关闭 LLC 请求缺口。

### 6.3 FLAC 的处理位置

FLAC 的 L3 relative error 较大，但 absolute error 只有 41 次；先保护这些小计数路径，再在全宏审计中处理其大量 split/retry 与 forwarding 差异。不得靠排除 split/RMW 使报告看起来改善。

### 6.4 来源要求

native request 真值继续来自维护验证器的守恒 Ruby/SLICC summary/task 证据，不取 taotrace path-class 字段作 native 真值。硬件身份、人口、来源门禁与精度阈值是不同结论。

只有证明了具体功能状态错误才进入 C2；Tag 发布时间、victim/LRU、dirty 和容量的变更各自独立开关、单独验收。该计划不预设“启用延迟 Tag 发布”作为最终必须动作。

## 7. 候选验收和最终交付

### 7.1 每一步的固定验收

1. 同输入、同参与核、同 task/边界、逐核宏/UOP/内存人口一致，源码与二进制身份可追溯。
2. 运行项目标准构建和 `fastsim_tests`；受影响的观察/forwarding/parent-service 定向测试随候选执行。冻结版已有测试失败保留并对照，不归为新增通过。
3. 使用维护原生验证入口，报告三个退休 miss 的全量预测、known native、unknown 及每层 raw count/绝对误差/相对误差。
4. native Tag miss 三层独立报告，分角色诊断不能替代总人口守恒。
5. CPI 每例提供 AE（cycles/macro）与相对误差，汇总等权 MAE/MAPE；不只看平均值。
6. 观察-only 候选要求 CPI/功能请求完全一致。功能候选要求逐例 CPI AE、cache 主事件 absolute error 不恶化并修复已证实机制；不满足即保留实验结果、停止推广，重新查证据。

第 6 条是本方案的保守候选推广条件，不是已测得结果，也不宣称有限五例证明所有负载无回归。若出现明确语义正确但当前近似组合导致退化的候选，需要进一步修复组合，不能以平均改善自动放行。

未知事件继续按用户授权发布暂定诊断，不因计数接近或 unknown 很少声称完整 PMU 认证。native 身份核验仍由 `validate_fs_oracle_identity.py` 独立完成。

### 7.2 诊断和性能分开

- 诊断回放可以启用采样日志，不把该运行墙钟当生产吞吐。
- 生产候选诊断关闭，保持固定容量、固定深度处理和原并行执行。
- 全部修复组合后，在 Zstd/XZ 等同输入上完成原约定的 30 次串行吞吐配对；吞吐结论单独给出。
- 功能修复和诊断建设可以继续推进；吞吐尚未测定的版本保留独立状态，不提前推广默认模式。

### 7.3 完成范围

最后交付应包含已验收的 L1 观察器修复、forwarding 资格修复、退休服务归属修复，以及证据确认需要的私有/LLC 请求行为修复；报告五例全量 CPI/退休 miss/native Tag miss、未知覆盖和独立吞吐。

阶段只是检查边界。不能完成 A1 就宣称所有 cache miss 已修好；也不能因为 C2 未证实而先推行整套新 service 时间/容量/Tag 发布框架。

## 8. 下一次实施的首个动作

从冻结快照建立新隔离副本，先完成 **A0 的观察-only 候选**：

1. 扩展模型成员和 fill 区间审计，保持既有预测完全一致。
2. 对 Graph500 与 XZ core 0 输出原因计数和有界反例。
3. 核验 native 逐成员证据可用性；缺失时建立独立新诊断源集，冻结/候选都用新源对照。
4. 基于具体误判见证选择 A1a/A1b/A2a/A2b 中的第一个单项修复，不预先将多计全部归为 response 太晚或将漏计全部归为 forwarding。

本轮只产出方案，未创建新的实现候选、修改模型、合并、提交或推送。

参考：[当前归因报告](fastsim-r01-retired-cache-miss-attribution-20261111.md)、[恢复验证](fastsim-r01-main-restoration-validation-20261111.md)、[准确率合同](docs/accuracy-reporting-contract.md)、[项目工作流](docs/codebase-workflow.md)。
