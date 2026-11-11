# v2-publish CPI 精度雪崩：因果诊断

结论：完整层级服务在共享回放时产生了含准入/父服务等待的相对延迟，CPU 父服务接口随后修正响应边，但普通 store 排空路径仍使用修正前的完整延迟。它把旧等待重新加到最终 store_send，拖住 SQ 释放，再经 FIFO store 排空、SQ 准入、依赖与共享时间反馈放大。延迟 Tag 发布增加了容量压力与部分功能 miss，进一步放大同一收费缺口。

隔离改动仅把修正后的 CPU 响应时长传给 store 路径；原生容量、Tag 发布和退休后 prepare 全部保留。Marian CPI 从 73.953462 降至 3.743249，消除了相对当前 off 基线新增回归的 99.01%。这证明该接口缺口是主要因果因素；干预的 CPI 差值包含反馈交互，不能解释成某个独立硬件 stall 的周期占比。

## 1. 共同输入、版本与范围

使用五个 c04 FST，4 核，native `sprEarlyStorePrepare=false`、预取关闭。所有模式 `legacy_prepare_timing=true`，保留退休指令的 prepare。CPI 为 workload-task active cycles / retired macro instructions，common-start-v1 / retired-stream-completion-v1。独立硬件身份验证复用已完成的 5/5 回执，重新检查其引用的原生审计与源文件哈希；维护的比较 API 对全部新输出核验功能内容与逐核宏人口。

当前 production 二进制 SHA-256 `34a23ff427a4134affe9ef6970381389c8dbd96d6ef6ede421b84ae86f0f6d1e`。修正实验在 `tmp/publish-cpi-diagnosis-20261110/store-transfer-control/`，复制当前 archive，仅重编 simulator.cpp 并采用 `-flto=16` 链接。当前 src/simulator.cpp、build/fastsim、build/libfastsim_lib.a 哈希保持原值。此实验未集成生产模型，也不是完整修复验收或吞吐认证。

AE = abs(FastSim CPI - gem5 CPI)，单位 cycles/macro；RE = (FastSim CPI / gem5 CPI - 1) × 100%。

## 2. 回归先发生在 merge，publication 再放大

| Marian 同输入模式 | gem5 CPI | FastSim CPI | RE | AE (cycles/macro) |
|---|---:|---:|---:|---:|
| off-audit | 2.364997 | 3.040902 | +28.58% | 0.675905 |
| merge-audit | 2.364997 | 52.363298 | +2114.10% | 49.998301 |
| publish-original | 2.364997 | 73.953462 | +3027.00% | 71.588465 |

同一当前二进制的 v2-wait CPI 3.055769，RE +29.21%，AE 0.690772；改善过 Graph500 的 R0/R1 冻结二进制 `dad62e0d…` 是另一个版本，五例 MAE 0.191599、MAPE 12.99%，不能把它与当前二进制的 v2-wait 模式混同。

## 3. 守恒账：主要堵在 store 背压与 issue 之前

三个 attribution 观察器对照均与原输出 time_accounting 完全相等、人口和功能身份一致。下表是模型内部全流响应关键周期账，不等同于 workload-task CPI 的全部 active cycles；各 critical 类别在该账本内互斥。

| 模式 | critical 总周期 | memory_response | SQ capacity | dependency | instruction fetch |
|---|---:|---:|---:|---:|---:|
| off-audit | 63592053 | 37772796 | 19339183 | 6478930 | 1144 |
| merge-audit | 1265175509 | 581318813 | 608336391 | 75518362 | 1943 |
| publish-audit | 1791149550 | 882910538 | 829697877 | 78539708 | 1427 |
| merge-transfer-control | 72031833 | 41282107 | 21776443 | 8969946 | 3337 |
| publish-transfer-control | 80723434 | 48315090 | 23569156 | 8838269 | 919 |

原 publish 的零退休 head-gap 1,791,031,953 周期，其中 fetched-not-issued 1,750,641,447（97.74%）；已发出 load 的 head-gap 37,652,862（2.10%），not-fetched 69,132。大量指令已取到却发不出去，与 store 排空/SQ 背压相符。memory_response 是传播原因标签，不能把其 882,910,538 周期全部称为 head load 的 DRAM 等待；instruction fetch 的互斥 critical 仅 1,427，排除其为主要瓶颈。

4,434,270 个 store UOP 不变，send→response 重叠寿命和从 off 的 68,083,182 增长到 publish 的 1,795,855,536。TSO wait、store lifetime、resource_wait_cycles 均有重叠，不能与 critical 账相加后充当 CPI 分解。

## 4. 明确的响应边接口错误

源码链：

- `src/simulator.cpp:278xx`：层级 follower 的回放延迟 = `max(selected_issue, parent.data, parent.controller) + L1 callback - selected_issue`；owner 同样把 hierarchy admission 的等待包含在 `shared_response - selected_issue` 中。
- `src/simulator.cpp:16502`：先复制该 `feedback.latency_cycles` 到 `store_latency_cycles`。
- `src/simulator.cpp:16957` / `resolve_parent_service():13100`：按最终 CPU admission 和父服务状态修正 `event_response_cycle`，但未同步刷新已经复制的 store latency。
- `regular_store_response_cycle():5820` / 调用 `:17875`：对不走 buffer-eligible prepare receipt 的普通 store，SQ release = store_send + 旧完整延迟。FIFO 的下一个 store_send 又依赖该 SQ response。

原 publish core0，seq7802879，同一行、一个 fragment、buffer_role=0：

| 观察边 | 周期/时长 |
|---|---:|
| canonical issue | 207215417 |
| canonical response | 207219458 |
| 旧 canonical latency | 4041 |
| corrected CPU issue | 208927552 |
| corrected CPU response | 208927554 |
| corrected response latency | 2 |
| actual retire | 208927553 |
| store_send | 209048529 |
| SQ release | 209052570 |

SQ release - store_send = 4041，严格对应旧 canonical latency；最终发送时，CPU 所选父服务等待早已结束，却仍重新支付旧等待。这是模型内部响应边不一致的直接证据，不需要猜测 native 的逐事件服务时间。core0 抽样中 272 个 regular store，有 98 个单 fragment、role=0 的 store service 大于已修正的 selected latency；这是抽样见证，不能按比例外推完整人口。第一条该类抽样为 seq1015807（241 对 2 周期）；采样 stride=4096，不能宣称它是完整流的绝对首分歧。原始及修正配对样本保存在 JSON。

## 5. 保留容量与 prepare 的因果干预

干预将 complete 模式的 store_latency 在父服务 resolve 后，按每个 write fragment 的 max(0, event_response_cycle - corrected_event_issue_cycle) 重新取最大值。原 off/wait 路径保留；deferred prepare 原本直接返回，不受父服务修正，它仍走自己的 store_receipt。实验未改变 prepare 开关、native 几何、合并或 publication。off 的 time_accounting 与 task PMU 逐项完全中性，证明孤立变更未污染旧路径。

| Marian 因果对照 | gem5 CPI | FastSim CPI | RE | AE (cycles/macro) |
|---|---:|---:|---:|---:|
| publish-original | 2.364997 | 73.953462 | +3027.00% | 71.588465 |
| publish-capacity-control | 2.364997 | 14.333975 | +506.09% | 11.968978 |
| merge-transfer-control | 2.364997 | 3.387171 | +43.22% | 1.022174 |
| publish-transfer-control | 2.364997 | 3.743249 | +58.28% | 1.378251 |
| publish-transfer-capacity-control | 2.364997 | 3.171824 | +34.12% | 0.806827 |

capacity-control 仅把 L1/L2 MSHR 和 fill 容量改成 16384，用于诊断容量敏感性。原 publish resource_waits 651,380 / resource_wait_cycles 881,688,671；放大容量后为 1,093 / 21,735，CPI 仍有显著残差。容量等待会把旧时长放大；因此扩大容量不能替代修正 store 响应传递，也不能作为正式 SPR 参数。

尤其是修正 store 传递后，原几何的 hierarchy resource_wait_cycles 反而达到 1,163,131,675，而互斥 critical 总周期降到 80,723,434。这进一步证明 resource_wait_cycles 是 canonical 请求等待的重叠和，不能直接当作 CPI 贡献；真正触发雪崩的是它经旧时长进入 store 排空的收费方式。

## 6. 五负载检验：主要雪崩可去除，Graph500 仍有另一类问题

| 负载 | gem5 CPI | 原 publish CPI | RE | AE | transfer-control CPI | RE | AE |
|---|---:|---:|---:|---:|---:|---:|---:|
| c04-777.zstd_r | 1.206049 | 2.713221 | +124.97% | 1.507172 | 1.114011 | -7.63% | 0.092038 |
| c04-801.xz_s | 0.575297 | 1.566361 | +172.27% | 0.991064 | 0.648483 | +12.72% | 0.073185 |
| c04-817.flac_s | 0.284584 | 0.438374 | +54.04% | 0.153790 | 0.325841 | +14.50% | 0.041256 |
| c04-854.graph500_s | 0.966422 | 0.699389 | -27.63% | 0.267033 | 0.699532 | -27.62% | 0.266891 |
| c04-872.marian_s | 2.364997 | 73.953462 | +3027.00% | 71.588465 | 3.743249 | +58.28% | 1.378251 |

| 五例等权版本 | CPI MAE (cycles/macro) | MAPE | CPI WAPE |
|---|---:|---:|---:|
| publish-original | 14.901505 | +681.18% | +1300.03% |
| publish-transfer-control | 0.370324 | +24.15% | +33.75% |

MAPE/MAE 按负载等权；WAPE 为按宏指令数加权的绝对周期误差除以参考周期。Marian 占原 publish 五例绝对 CPI 误差和的约 96.08%、APE 和的约 88.87%，所以一个 store 密集负载会主导总表的“雪崩”。

Graph500 在该干预下几乎不变；其低估不能由 stale store latency 解释。已有独立 32 条退休记录见证：seq4 store 的 canonical permission issue=6，actual retire=216，退休后准入下限=218，CPU permission=462；同线 seq6 load 在 268 退休。共享记录、合并与状态变更仍使用提前的 canonical 服务，仅推迟 CPU prepare/send 没有同步迁移该权威服务。该见证确认 prepare 服务可见性/时间锚点缺口，但尚未定量闭合真实 Graph500 的 CPI 残差。

## 7. Publication 的功能变化与 PMU 证据边界

| Marian 模式 | task L1D tag miss | task private L2 tag miss | task LLC tag miss |
|---|---:|---:|---:|
| off-audit | 539880 | 425161 | 223214 |
| merge-audit | 559020 | 422420 | 223214 |
| publish-original | 615937 | 627365 | 235932 |
| publish-transfer-control | 615815 | 627538 | 235787 |

原 merge→publish 的 L2 tag miss 从 422,420 到 627,365（+48.51%）；resource_wait_cycles 从 389,457,861 到 881,688,671。publication 改变了功能 cache 路径/代次取消与服务并发，它不是纯观察器。这些计数支持其放大作用；其额外周期仍和 stale latency 及 feedback 交互，不能线性归因。

上表是 FastSim 相同 scope 的内部计数对照。独立 native 误差在 native-pmu CSV/JSON，reference 仅来自守恒 Ruby/SLICC 摘要；L1/L2/branch 映射仍为 proxy、LLC 为 diagnostic，不宣称严格 perf 等价。Marian core1 与 XZ core2 的独立退休 load 参考各有一条U-BOUNDARY unknown，三个主退休 miss 不评分；不得使用旧 path-class 缓存字段替代。task PMU 的原始退休计数和 active CPI 的宏分母使用各自边界合同，不互换。

## 8. 裁定与完整修复方向

| 假说 | 裁定 |
|---|---|
| 人口或 CPI 参考口径错配导致雪崩 | 排除；功能身份、逐核宏人口与 active scope 全部通过 |
| delayed Tag publication 单独制造雪崩 | 排除；merge 已严重回归 |
| fetch 等待是主因 | 排除；互斥账及 head-gap 均不支持 |
| 未刷新 store latency 造成旧服务等待重复收费 | 确认；运行边见证与保留容量的同输入干预一致 |
| 有限容量/共享反馈放大该错误 | 确认敏感性；不是独立可相加的 CPI 周期归因 |
| CPU post-retire 门禁已消除全部 early prepare 效果 | 排除；canonical authority/follower 仍提前可见 |
| 提前 prepare 服务定量解释全部 Graph500 低估 | 未定；仅有机制见证，尚未闭合其真实 ROI 残差 |

正式修复应统一实际准入、父服务 data/permission/controller 边、mandatory store callback、SQ release 与各层容量 lease 的使用合同：等待父服务时只支付尚未完成的部分，已完成后只支付本次真实 callback；多 fragment、跨 epoch、代次失效与特殊 store 必须保留明确分支。store 服务不能把 canonical 排队时长当成新的独立服务时长。

同时修正 retired prepare 的权威准入及 follower 可见性，保证 CPU 与共享近似时钟采用同一有界准入合同。保留退休后 prepare、原生容量和两阶段并行框架；不要关闭 prepare 拟合 CPI、扩大容量掩盖重复收费、引用 gem5 timing 作为模型输入，或引入逐周期/全窗口反复收敛。当前隔离干预足以证实主因，尚不是这些生命周期问题的完整生产补丁。

## 可复核产物

- [完整 JSON 与运行样本](fastsim-v2-publish-cpi-collapse-analysis-20261110.json)
- [CPI CSV](fastsim-v2-publish-cpi-collapse-analysis-20261110-cpi.csv)
- [native PMU CSV](fastsim-v2-publish-cpi-collapse-analysis-20261110-native-pmu.csv)
- [原三版本基线](fastsim-r01-v2-wait-noearly-comparison-20261110.md)
- [中性观察器与容量对照回执](tmp/publish-cpi-diagnosis-20261110/valid-controls/completion.json)
- [store 传递干预回执](tmp/publish-cpi-diagnosis-20261110/store-transfer-control/completion.json)
- [其余四例回执](tmp/publish-cpi-diagnosis-20261110/store-transfer-control/five-case-expansion-completion.json)
- [隔离修改源码](tmp/publish-cpi-diagnosis-20261110/store-transfer-control/simulator.cpp)
- [prepare 提前可见性见证](tmp/prepare_retirement_witness_distinct_sets_20261110.json)
- [复评分脚本](tmp/score_publish_diagnosis_20261110.py)
