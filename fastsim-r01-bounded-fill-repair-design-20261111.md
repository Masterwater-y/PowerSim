# 在冻结吞吐约束内修复退休填充观察器

日期：2026-11-11。状态：具体设计；没有通过精度、吞吐验收的修复版本。

## 1. 结论与范围

采用“现有功能服务摘要 + 现有计时反馈 + 每核固定容量观察表”。
保留 R0/R1 冻结 `v2-wait` 的两阶段并行执行、CPU 服务时长、缓存状态和请求行为。
首先修观察器的消费者识别，再独立处理 forwarding 和功能请求缺口。
gem5 的动态时钟、服务结果与消费者记录只用于离线验证。

当前内部目标固定为 gem5 `spr-retired-load-miss-v2`。按用户要求，本轮不等待真机
事件映射认证，不修改共享事件规则来回算旧报告。
基线二进制 SHA-256：
`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
观察诊断父版本采用已通过五例预测守恒的 A0 快照，不能继续叠加被拒绝的
store-data-seed 或 R4 merge/publish 实验。

“固定开销”只说明复杂度受控，不能证明吞吐相同。最终还需在关闭诊断的候选上，
使用相同输入、相同参与核与主机条件完成串行配对测量。

## 2. 已核实的实现边界

| 已有事实 | 位置 | 可以如何复用 | 不能如何使用 |
|---|---|---|---|
| 每核 4096 个直接映射槽；line 与 [issue,response) | `RetiredCacheMissEstimator` | 保留容量和查找预算 | line 相同不证明填充代次相同 |
| producer issue、accepted memory displacement、旧 gap | `account_perf_retirement` / `TimingFeedback::memory_issue_extra_q16` | 构造统一定义的模型观察时间 | 位移含单调功能 envelope，不能冒称逐 load 最终 OoO issue |
| 分段 Q16 core/reference 时钟 | `PiecewiseCoreClock` | 同一 clock epoch 中转换 | 不把 local cycles 与 reference cycles直接比较 |
| 最终 OoO issue 的审计数组 | `line_generation_actual_issue_q16` | 离线诊断比较近似误差 | 默认启用该审计会绕开 `try_compute_response_inactive_segment`，不能作为生产修复依赖 |
| per-line Sequencer callback 与 generation | `SequencerLineRequest` | 已有 alias 路径传递父服务摘要 | 新 parent 的 generation 重置为 1，不能把它单独当永久唯一服务 ID |
| LLC fill generation | `SharedTimingDescriptor` | 关联 LLC 服务 | 不能冒称 L1 数据填充代次 |
| shared response / parent callback | `MemoryReplay` | 保留原响应近似、注明边类型 | callback 不证明数据此刻才进入 L1 |

当前没有覆盖所有私有路径的独立 L1 数据可见边。不能统一从 response 减一个常数
来补齐，也不能统一重算所有 miss 的完整配置时长。缺少事实的路径必须保留明确近似。

新源 A0 与冻结版的五例同源回放均已完成，五例预测差异全部为空，已封存于
`tmp/r01-cache-repair-exclusive-20261111/new-source-parity-and-spr-coverage.json`。
Zstd 首次成员绑定拒绝已定位到四条跨页 load：模型实际物理片段不在 FST 首地址的
连续物理区间。须用身份绑定的功能 `.xlat.gz` 片段校验，不能关闭来源检查。
这属于诊断工具覆盖缺口，不是已证明的观察器误判根因。
后续 Graph500 配对通过来源门禁后，在 SPR 字段校验拒绝：保存的旧 collector
生成的 member 只有旧 `source` 位，没有 `spr_miss_mask/spr_unknown_mask`。
五例分别检查的 56,812 / 11,617 / 12,329 / 42,047 / 24,884 个 member
全部缺少这两个字段（顺序：Zstd、XZ、FLAC、Graph500、Marian）。
不能由聚合数据或旧 source 位重建逐成员 v2 真值。必须先验证 collector 的输出能力；
缺字段的新源只能支持已有聚合/旧路径诊断，不能声称本轮 SPR 逐成员配对成功。
独立工具修复和 fail-closed schema 预检的 23 项测试通过；该结果不构成模型精度验收。

## 3. 固定成本的生产结构

### 3.1 服务摘要随现有反馈传递

仅在已有路径创建数据服务或 alias 时记录：

```text
owner_key = (core, source_sequence, memory_ordinal)
service_kind = demand_data / prepare_data / permission_only / prefetch_data
clock_basis = core/reference + epoch + availability
begin = 模型服务准入
data_visible = 独立数据边或显式近似
callback = 原响应边
```

沿用功能流里的 sequence/ordinal，alias 保存实际 owner 的身份；不按相同 line
重新到全局服务池查找。跨 chunk 只保存稳定整数身份，不保存可能失效的指针。
原 Sequencer generation 可作为局部版本辅助，但需与 owner_key 联合使用。
不存在的私有身份或数据边标 unavailable，不能用 LLC ID 或零值填补。

尽量复用已有 `MemoryReplay` 与 timing 数组，新增字段的数量和常驻字节必须单独记录。
禁止为每条普通 load 添加动态分配、unordered_map 查询、全池复制或事件日志。

### 3.2 每核观察表

首个身份候选仍保留 4096 槽与原映射，先隔离“身份改动”和“冲突策略改动”。
拟议槽包含 line、owner/version、clock epoch、begin、data-visible 与边来源。
优先控制在单个 64-byte 主机 cache line 内；这是布局预算，尚非实际 `sizeof` 或测量结果。

普通消费条件为：

\[
\mathrm{pending}=
\mathrm{same\_line}\land\mathrm{compatible\_service}
\land\mathrm{same\_clock\_basis}
\land(t_{\mathrm{begin}}\le t_{\mathrm{load}}<t_{\mathrm{data}}).
\]

有确切 alias 身份时必须匹配 owner/version；只有地址和区间的路径继续作为有来源标记
的近似，不能报告成精确服务关联。纯权限服务不能创建 data-fill。
数据可见后，旧服务即使仍持有 callback/容量资源，也不再产生 L1 PendingFill。
新冷 owner 不复用另一代服务的结束边。

若单独证实直接映射冲突造成遗漏，再试 2048 sets × 2 ways，总槽数仍为 4096；
单次查询至多比较两槽，替换检查至多两槽。该实验独立验收，不无限扩容。
若两个在途代次都不能保留，记录近似缺口，不能用陌生服务的区间补造 follower。

### 3.3 快速路径

1. 完全 forwarding 保持直接结果返回；不为它构造缓存填充时钟或查服务。
2. 普通 local hit 先做固定槽位探测。没有同线候选时直接返回自身结果，
   不进行额外 clock 转换或父服务检索。
3. 有同线候选才比较时间、代次和边类型；cold owner 只更新目标固定槽。
4. 诊断关闭时不维护逐成员字符串/样本输出；调试开关在运行初始化时读取。
5. 每核独立状态；不新增跨核锁。复用现有 accepted-prefix 边界更新，
   rollback 不增加全观察表复制。

不能直接把观察时间单调 clamp 成访问顺序：OoO load 的 issue 可以逆序，
强行单调化会把一种错位换成另一种错位。也不将 native tick 转换后喂给模型。

## 4. 时间修复必须逐项验证

### T1：明确时间来源与可用性

用 availability 区分“合法周期零”和“没有共享时间”。明确每条路径的 producer、
canonical、shared issue 属于哪个模型时间语义；映射至统一观察域。
先做中性元数据候选，要求事件预测与冻结行为全部一致。

然后只替换经配对证实错位的路径。现有 accepted displacement 是可用的有界近似，
不能声称它自动等于最终 OoO issue。最终 issue 已在正常快路径可得时可导出紧凑摘要；
若提取它会退化到逐 UOP 详细路径，该路径保留近似，改进摘要而非禁用批处理。

### I1：稳定服务关联

只在本来就执行的 owner/alias 创建点传递 owner_key，不迁移整个服务管理器。
覆盖同线换代、旧 callback 尚未结束但数据已可见、跨 epoch carry。
缺少关联的路径单列；不得把“当前 line 匹配”记作 identity match。

### E1：数据边与响应边分开

有已存在模型 data-return/publication 边的路径消费该边；
callback 保留原 CPU 计时用途。没有数据边的路径注明 response-upper-bound，
不统一缩短全部区间。
任何新数据边近似都以模型准入和已有路径摘要定义，常数必须有配置来源；
一项路径一项实验，避免重演 R4 的等待范围整体扩大。

### A1：角色来源与冲突

store/prepare 只有在确认产生数据填充且身份/时钟匹配时才能播种。
首轮广泛 store-data-seed 没闭合 XZ 主要漏报且加重其他例多报，继续保持关闭。
软件预取、权限服务、store takeover 与 hot alias 分别处理。
固定路数组相联仅在冲突贡献获得成员见证后尝试。

## 5. 预算与无法保证的部分

观察器目标：

| 项目 | 约束 |
|---|---|
| 常驻状态 | 每核固定容量，随 workload 长度不增长 |
| 每条普通 load | 一次直接映射探测；冲突候选最多两路比较 |
| 模型时钟转换 | 有潜在同线服务时才需要新增转换 |
| 服务摘要创建 | 只在现有 owner / alias 路径附带处理 |
| 事件调度 | 不增加逐周期推进、全窗口收敛或新的因果 runtime |
| 日志 | 关闭诊断时无 I/O、字符串构造、动态日志分配 |

即便比较次数固定，槽变大、额外字段传输或串行 retirement pass 仍可能增加主机
cache miss 和带宽开销。记录 slot/descriptor 大小、resident memory、host wall 和
分阶段耗时；不能把复杂度说明当作吞吐测量。

forwarding 过期修复涉及功能请求：额外转发失效后必须在原两阶段框架内补回访问，
这可能增加实际模拟工作，不能保证零成本。继续采用有限窗口与有界修复范围，
并把该成本与观察器成本分开验收。不能只在退休统计中补 miss 位来掩盖消失的请求。

## 6. 精度与吞吐的推广条件

每个候选都从最后通过的版本派生，单因素验证。

- 观察器候选：五例同 FST，逐核人口一致；target cycles/CPI、
  功能 cache/request 计数严格不变。三个退休事件逐例 absolute count error
  不恶化，配对样本的 FP/FN 和不可分类覆盖单列，不能外推样本比例成全量贡献。
- Graph500 的多计与 XZ core 0 的漏计同时检查，不能只用整体缩短或延长区间拟合。
- 特殊路径保留 split/RMW/retry/forwarding 资格及宏 OR，不通过删人口改善报表。
- CPI 报告保留逐例 AE，单位 cycles/macro，并显示等权 MAE。
  冻结基线 CPI MAE = 0.191599 cycles/macro、MAPE = 12.994957%；
  该数值是保护基线，不是新设计收益。
- native 评分继续用维护验证器与独立身份检查；极少量 unknown 的暂定状态保留。

吞吐测量：候选与冻结版均关闭诊断，Zstd/XZ 等同输入，串行运行，
每个比较按既定 30 次配对；AB/BA 顺序交替，记录有效亲和性与主机其他任务。
固定输入 hash、输出人口和 target prediction，预热与正式计量区分。

\[
R_i=\frac{\mathrm{MIPS}_{candidate,i}}{\mathrm{MIPS}_{frozen,i}},
\qquad
R_{\mathrm{geo}}=\exp\left(\frac1n\sum_i\log R_i\right).
\]

同时报告 wall time、模型计量 MIPS、输入身份校验开销、各负载的配对比和置信区间。
目标为吞吐不低于冻结版；本设计没有自行放宽到“允许若干百分比下降”。
置信区间跨 1 只能说明结果不确定，不能声称已证明同等吞吐。
若出现下降，先处理字段大小、时钟转换频度、重复 hash/扫描及批量路径失活；
仍不满足则不推广，保留通过精度但未通过吞吐的独立候选。

## 7. 后续交付

先完成新源严格成员绑定与同源五例基线，再从 A0 派生 T1/I1/E1 等独立候选。
基于见证决定行为修复，保留未覆盖路径和失败实验。最后独立完成 forwarding、
退休服务归属和证据所需的私有/LLC 请求修复，完成原计划全范围验收。
当前设计完成不等于缓存误差修复完成，也不等于吞吐承诺已兑现。

相关：
[原修复方案](fastsim-r01-cache-miss-repair-plan-20261111.md)、
[实施记录](fastsim-r01-cache-miss-repair-progress-20261111.md)、
[归因报告](fastsim-r01-retired-cache-miss-attribution-20261111.md)。
