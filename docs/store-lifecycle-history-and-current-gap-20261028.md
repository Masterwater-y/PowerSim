# Store 生命周期：历史修复效果、当前使用情况与剩余误差

日期：2026-10-28。本记录整理已有实现和归档实验，不新增模型修改、采集或回放。当前状态依据本轮六项最终预测的 `configuration`、现有源码及历史结果文档核对；后续实验应重新确认配置和二进制身份。

## 1. 当前结论

围绕“store 权限准备 → 数据/权限返回 → SQ 释放 → 依赖与退休”的问题，项目已经做过多轮分析、实现和真实负载验证。**完整候选已经存在，但本轮 32 GiB 数据仍使用 legacy store 时序；最新实验只额外启用了独立 store callback 修复。**

历史上的 `bounded_analytic` 尤其值得复用：它已经采用一次规范 cache/uncore 分类、一次核心前向反馈及解析 store 生命周期，符合两阶段、高吞吐方向。它在旧开发来源上改善了 Marian，也有明确的支持域和近似边界。目前没有本轮 32 GiB 来源上的复测结果，不能把当前 legacy 的大误差解释成该候选已经失败。

因此，下一步应先核验并复测现有 `bounded_analytic`，再依据同源差分决定还缺哪条机制；无需把整条 store 生命周期当成尚未实现的新功能重新设计。

## 2. 这条链的准确含义

这是一条等待传播链，不代表所有节点严格按文字顺序串行执行。普通 store 的架构退休、权限准备、实际写入和 SQ 释放必须区分。

```mermaid
flowchart LR
  E[地址与数据及执行条件就绪] --> C[架构提交建立 canWB]
  C --> P[满足资格后准备权限]
  P --> R[权限或数据服务返回]
  C --> F[按 FIFO 顺序发布 store]
  R --> F
  F --> W[实际写请求完成]
  W --> S[SQ 项释放]
  S --> Y[后续指令的容量与顺序等待解除]
  Y --> T[后续依赖与退休时序变化]
```

- `canWB` 是该 store 可进入后续写出流程的资格，不能用任意较早的 cache 访问替代。权限此前也可能已经合法存在，不能仅凭 response 早于 commit 就认定违法。
- prepare 返回与真正写请求完成不同；**prepare 返回不能直接释放 SQ**。较年轻 store 的合法准备可以与较老 store 的等待重叠，最终发布仍保留 FIFO 顺序。
- store 已经架构退休，并不意味着其 SQ 项已经释放。SQ 压力会影响后续指令；普通 store 也不是以“返回 load 数据”的方式唤醒寄存器消费者。依赖、转发、容量及顺序边各自需要明确的触发条件。
- 执行/WB 等待与 store callback 是不同阶段。已经在提交前支付的等待，不能在发布时再支付一次。

## 3. 当前究竟启用了哪些实现

核对目录：[`final-ab`](../tmp/marian-xz-service-repair-20261028/final-ab/)。以下字段在六项 `on.json` 中一致，范围为 Marian C4/C8、XZ C4/C8、Graph500 C16、FLAC C4。

| 配置或能力 | 六项最新 ON 预测 | 含义 |
|---|---|---|
| `store_timing_model` | `legacy` | 未使用 bounded 解析模型 |
| `store_request_engine` | `false` | 未使用事件参考引擎 |
| `buffer_tso_prepare_lifecycle` | `false` | 未启用旧 prepare lifecycle 候选 |
| `buffer_tso_residence_certificate` | `false` | 未启用旧驻留证书候选 |
| `store_private_callback_timing` | `true` | 只启用最新独立 callback/publish 修复；代码默认仍为 false |
| `store_forwarding` | `true` | 既有 store forwarding 正在使用 |
| `response_queue_feedback` | `true` | 既有响应与队列反馈正在使用 |
| `store_wait_relief` | `false` | 历史方案与当前 buffer 模式不兼容，配置主动关闭 |

`prepare_lifecycle=false` 不等于没有 store 模型。legacy 仍包含 SQ、FIFO、转发、prepare/publish 估计及核心反馈，只是没有采用完整候选的生命周期表达。

FR-FCFS 的到达时序约束候选也已启用；旧拓扑缩窗并非本轮未修复误差的直接解释。它与 store 生命周期模型是不同机制。

原始九项快照与最新六项 ON 实验必须区分：原始快照未启用本次独立 callback 修复，后者不是对原九项结果的整体替换。

## 4. 历史上具体做过什么、效果如何

本节均为旧来源结果，主要为四核、八 DRAM 通道开发窗口。它们不能与本轮 32 GiB 窗口直接拼接成同一个实验集合。

相对误差定义为 `(FastSim CPI - gem5 CPI) / gem5 CPI`；绝对误差定义为 `abs(FastSim CPI - gem5 CPI)`，单位均为 cycles/宏指令。正号表示高估。

### 4.1 旧驻留证书与 prepare lifecycle

该组合允许在资格与驻留证明成立时表达 store 准备，显著改善过旧 XZ 来源；并非所有负载都得到同等收益。

| 旧来源 | gem5 CPI | legacy CPI → prepare CPI | 相对误差 legacy → prepare | CPI 绝对误差 legacy → prepare |
|---|---:|---:|---:|---:|
| XZ C4 | 1.081955043 | 1.527190422 → 1.044795614 | +41.1510% → -3.4345% | 0.445235379 → 0.037159429 |
| Marian C4 | 2.665483924 | 3.170054366 → 3.122987909 | +18.9298% → +17.1640% | 0.504570442 → 0.457503985 |

还修复过“早先不合格的同 line store，使后续合格请求也被拒绝”的问题：拒绝记录改为按具体 event 保存，必要的跨核、page-fill、权限丢失保护保留。但在被检查的另一个旧 XZ 窗口，该小修复前后预测完全相同，均为相对误差 -4.9006%、绝对误差 0.052053259。不能把组合相对 legacy 的全部收益记到这次小修复上。

该候选存在其他负载反例与资格近似，未据此推广默认。详见[第一批机制修复结果](cpi-sqlite-marian-xz-repair-results-20261021.md)。

### 4.2 StoreRequestEngine / event_reference

引擎已经实际接入生产回放路径，不只是独立组件。它区分 hint、mandatory、物理取权，表达 canWB、FIFO、权限返回、实际写完成和 SQ 释放，并在 epoch 内反馈闭合。

| 旧来源 | gem5 CPI | legacy CPI → engine CPI | 相对误差 legacy → engine | CPI 绝对误差 legacy → engine |
|---|---:|---:|---:|---:|
| SQLite C4 | 1.000924991 | 0.881132259 → 0.906444664 | -11.9682% → -9.4393% | 0.119792732 → 0.094480327 |
| XZ C4 | 1.081955043 | 1.527190422 → 1.660043563 | +41.1510% → +53.4300% | 0.445235379 → 0.578088519 |
| Marian C4 | 2.665483924 | 3.170054366 → 无有效值 | +18.9298% → 无有效值 | 0.504570442 → 无有效值 |

| 有效配对范围 | 模式 | CPI MAE | MAPE |
|---|---|---:|---:|
| SQLite/XZ 两项 | legacy | 0.282514056 | 26.5596% |
| SQLite/XZ 两项 | engine | 0.336284423 | 31.4346% |

Marian 的请求发送和服务时刻来回振荡，64 次 fixed-point 反馈未闭合，因此明确失败，没有有效 CPI。不能把失败赋零、提高迭代上限或加入人为时间下界来制造结果。

事件引擎保留为显式候选。更完整的事件表达本身不保证准确性或吞吐，旧真实负载结果不支持默认启用。详见[实际接入与闭合失败记录](store-engine-integration-results-20261021.md)。

### 4.3 bounded_analytic：已经存在的两阶段解析实现

该实现针对上一条路径的闭合成本，保持一次规范 cache/uncore 分类和一次既有核心前向遍历，在退休传播过程中解析 canWB、prepare、接管、本地写、一次重取权及 SQ 释放；不进行全窗口 fixed-point 重放。跨 Q 的 FIFO、SQ、NO_ACCESS 与核心约束已有处理，旧驻留证书不再控制 prepare 资格。

**以下对照基线是旧证书+lifecycle，不是 legacy，也不是本轮 callback 修复后的模型。**

| 旧来源 | gem5 CPI | 旧 prepare CPI → bounded CPI | 相对误差 prepare → bounded | CPI 绝对误差 prepare → bounded |
|---|---:|---:|---:|---:|
| SQLite C4 | 1.000925 | 0.878154 → 0.877033 | -12.2657% → -12.3778% | 0.122771 → 0.123892 |
| XZ C4 | 1.081955 | 1.044796 → 1.039751 | -3.4345% → -3.9007% | 0.037159 → 0.042204 |
| Marian C4 | 2.665484 | 3.122988 → 3.033738 | +17.1640% → +13.8157% | 0.457504 → 0.368254 |

| 三项同源集合 | CPI MAE | MAPE | WAPE | signed bias |
|---|---:|---:|---:|---:|
| 旧证书+lifecycle | 0.205811 | 10.9547% | 13.0614% | +5.8690% |
| bounded_analytic | 0.178117 | 10.0314% | 11.3416% | +3.8958% |

旧开发来源中，Marian 改善，SQLite/XZ 略退。旧事件参考引擎在 Marian 的迭代失败由这个无迭代子域避开，但不能据此认定所有生命周期近似都已解决。

同一冻结二进制、固定 CPU、串行交错三轮的吞吐 pilot，相对旧 prepare 分别为 SQLite +16.68%、XZ +26.59%、Marian +21.25%。这些结果不代表相对当前 legacy 的吞吐变化，也不构成所有负载或项目完整吞吐目标的证明；该轮没有采样 Peak RSS。

详见[有界 store 解析路径实施与验证](bounded-store-timing-results-20261022.md)，原始收据位于 [`tmp/bounded-store-timing-20261021/`](../tmp/bounded-store-timing-20261021/)。

## 5. 为什么 bounded 尚未成为默认

现有生产实现只支持资源不构成瓶颈的明确子域，不能把通用组件测试视为所有生产能力已经完成。

- TBE 数量至少为 `max(SQ, tso_buffer) + 2*LQ + 1`，CPU store 接口至少为 `max(SQ, tso_buffer) + 2`，optional_entries 必须为 0。旧 SQ114/LQ32 配置的下限为 179/116，实际 256/200 满足。
- 每 UOP 最多两个 data fragment；atomic 必须满足 locked 和前后 barrier 资格。绑定 TBE/接口资源、有限 hint 队列、层级 walker 及其他竞争修复组合不在首版支持域内，不支持时明确拒绝。
- `canonical-queue-forecast-v1` 只计算一次共享排队；最终 CPU 到达改变对 uncore 竞争的反作用没有重新计算。
- 权限和驻留采用规范批末预测，可能包含实际 publish 之后的访问；完整 owner/permission generation 及精确失效仍未闭合。
- 通用绑定资源适配和完整流式游标尚未接入生产；旧验证只有三个开发来源，缺少完整保留集、配置趋势及推广矩阵。主要退休 cache miss 证据的 unknown 也没有因此消失。

这些是保留 opt-in 的具体理由；不意味着该候选从未实现，也不意味着它在本轮新来源上已经验证失败。

## 6. 最新独立 callback 修复的实际收益与残差

最新修复将普通 store 的显式 L1/L2 callback 1/5 cycles 从旧生命周期开关中解耦，并避免把提交前已支付的执行/WB 等待再次计入 prepared store 的发布服务。参数来自目标配置和协议阶段，不根据负载 CPI 拟合；没有新增核心遍历或改变 FST、Q、SQ 容量。

| 本轮 32 GiB 来源 | gem5 CPI | callback OFF CPI → ON CPI | 相对误差 OFF → ON | CPI 绝对误差 OFF → ON |
|---|---:|---:|---:|---:|
| XZ C4 | 0.758505 | 1.007150 → 0.969584 | +32.7809% → +27.8283% | 0.248645 → 0.211079 |
| XZ C8 | 0.673482 | 0.933625 → 0.879998 | +38.6266% → +30.6639% | 0.260143 → 0.206516 |
| Marian C4 | 4.933260 | 6.005699 → 5.868488 | +21.7389% → +18.9576% | 1.072438 → 0.935228 |
| Marian C8 | 5.189270 | 6.039175 → 5.935064 | +16.3781% → +14.3718% | 0.849905 → 0.745794 |

| 本轮四项主要来源 | CPI MAE | MAPE | WAPE |
|---|---:|---:|---:|
| callback OFF | 0.607783 | 27.3811% | 22.7954% |
| callback ON | 0.524654 | 22.9554% | 19.3593% |

这部分修复使四项 MAE 减少约 13.68%，仍有明显残差。Graph500 C16 的绝对误差由 0.257810 降至 0.245141，相对误差由 -11.1519% 变为 -10.6039%；FLAC C4 保持绝对误差 0.025042、相对误差 +5.5277%。六项完整表、吞吐波动、参数证据和默认关闭兼容检查见[独立 callback 修复记录](marian-xz-private-store-callback-repair-20261028.md)。

已观察到的模型关键等待如下，保留其有限含义：

| 模型关键核 | SQ 容量原因 cycles | 访存响应原因 cycles | 依赖原因 cycles |
|---|---:|---:|---:|
| XZ C4 core0 | 1,482,022 | 1,145,333 | 1,079,190 |
| Marian C8 core2 | 4,175,799 | 6,063,323 | 1,966,852 |

这些是模型内部等待分类，**不是相对 gem5 的误差贡献**。不能把它们直接扣除，也不能将剩余误差按这些计数分摊。

残差的优先假设包括：合法 prepare 与 FIFO 发布重叠不足，同 set 后缀保守拒绝过多，反方向的提前复用权限，以及 corrected issue 加上冻结 service 时延时可能重复计入排队 slack。Bound 中已有的虚假下界等待也不能由 Weave 的 max 反馈自动消除。这些假设仍需同源见证区分，尚不能给出各自占比。

补充原生诊断使用了不同自然窗口；pipeline debug 窗口又早于测量开始，不能提供本批完整配对关键链。可用的 callback 前缀观察只支持服务边界，不能升级为 store 全量误差分解。

## 7. 后续排查顺序与约束

以下为后续建议，本次整理不启动实验或切换默认。

1. **先复用现有 bounded。** 审核本轮实际配置的资源证明、FR-FCFS 组合、跨行/atomic 资格和运行支持域。当前代码的配置检查不排斥 FR-FCFS，不等于已经完成二者的真实来源兼容验证。若被拒绝，记录具体事件或条件，不放松资格来获取 CPI。
2. **明确比较基线。** 至少区分 legacy+callback OFF、legacy+callback ON 与 bounded。bounded 已消费显式 store 1/5 服务；首轮保持独立 callback 开关关闭，避免混淆收益归属。各组继承同一实际来源配置，不覆盖本轮 32 GiB、通道数、cache 几何、owner plan 或预热边界。
3. **使用同一正式输入做差分。** 优先 Marian/XZ C4/C8，保留 Graph500 C16、FLAC C4 作为控制；分别核验输入/目标身份、共同终点、宏指令和 UOP 数量，再复用维护验证器。旧四核八通道结果不能代替这一步。
4. **再定位首条剩余机制差异。** 对支持域内结果，核对 canWB、prepare、权限有效性、FIFO 发布及 SQ 释放的对应边；优先补缺失关系，避免重新实现已经存在的完整模块。
5. **保留两阶段和吞吐约束。** 不按负载、PC 或核数拟合延迟，不统一缩短 miss 服务，不把旧 fixed-point 引擎直接升级默认，不通过增加全量重放轮数闭合模型。精度与吞吐分别验收，功能预热、测量 wall、全程 wall 和 RSS 分开报告。

所有结果继续遵循[精度报告契约](accuracy-reporting-contract.md)：MAE/MAPE 按 case 等权，WAPE/bias 按已核验 task 宏指令数加权；原生 cache 参考只使用守恒的 Ruby/SLICC 证据并复用维护验证器。来源检查通过不等于精度达标，PMU unknown 不清零，`semantic_equivalence=false` 和 `hardware_equivalence=unverified` 的边界继续保留。

## 8. 证据索引

- [旧证书与 lifecycle 修复及来源差异](cpi-sqlite-marian-xz-repair-results-20261021.md)。
- [事件引擎实际接入及 Marian 闭合失败](store-engine-integration-results-20261021.md)。
- [bounded 解析实现、支持域、旧三项精度及吞吐](bounded-store-timing-results-20261022.md)。
- [本轮独立 callback 实现及六项验证](marian-xz-private-store-callback-repair-20261028.md)。
- [本轮原始九项快照分析](spec-speed32g-two-stage-error-mechanisms-20261028.md)；其 store 后续方向以本文的历史实现核对为补充。
- [本轮最终六项实际配置和输出](../tmp/marian-xz-service-repair-20261028/final-ab/)、[精度比较](../tmp/marian-xz-service-repair-20261028/comparison.json)、[维护验证收据](../tmp/marian-xz-service-repair-20261028/validation-combined-receipts.json)。
- [配置默认值与模型选择](../include/fastsim/config.hpp)、[支持域校验](../src/config.cpp)、[实际 store 回放与反馈实现](../src/simulator.cpp)。
