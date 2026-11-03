# Zstd C4：NO_ACCESS 功能语义与本地 SQ 修复

本轮实施 [完整退休周期排查](zstd-cpi-retirement-diagnosis-20261019.md)的前两项：gem5 输出功能性请求角色，FastSim 保留 CDA 的 CPU/译址/SQ 责任，同时去掉虚假的 data-cache 请求，并加入响应侧 canWB / 本地扫描约束估计。没有修改时延常数、LRU 证书默认值、八通道配置或 buffer + physical-PTE 的兼容性检查。

同一新 FST 的维护验证：CPI 相对误差从 **-26.89% 降到 -8.82%**，绝对误差从 **0.395932 降到 0.129822 cycles/macro instruction**。这不是 PMU 全面修复，也不是严格的 Ruby 请求扫描重排；构建、回归及验证收据见下文实验目录。

实验目录：[tmp/noaccess-repair-20261020](../tmp/noaccess-repair-20261020/)。

面向阅读的排查过程与队列例子见 [Zstd CPI 大误差：排查过程与通俗解释](zstd-cpi-debugging-explained-20261020.md)。

## 1. 采集来源与可比性

- 标准 SPEC `777.zstd_r`，4 个单线程 rate copy，4 核；不是人为启用 OpenMP 的 speed 负载。
- 8 个内存通道、3 GiB、buffer TSO 114、LRU；cache / FU / DRAM 参数沿用同次采集的实际配置映射，没有按 CPI 调参。
- 保持已有实际压缩计算 ROI，KVM → warmup → stat；warmup 仍为 core0 1M 指令预算。本次 warmup **67.153 秒**，采集总计 **504.310 秒**，正常退出 0，无存储或时间守卫中止。
- 新 gem5 二进制重建身份绑定的 KVM checkpoint，随后恢复采集，未改写或绕过旧 checkpoint 的 binary/runner/input 身份。
- 新来源 FST 共 **3,100,169,312 bytes**。四核测量期均有 **85,404** 条 NO_ACCESS，总计 **341,616** 条。
- gem5 NT store 仍是 cacheable fallback，本轮没有增加真机 write-combining / NT 模型。
- 独立逐流检查全流 hash、core/ordinal、CDA 的 8-byte 范围及其后两条实际 data-store 成员；四核共 341,616 组三元组全部保留。其余指令、依赖、宏边界和地址未因角色分类被删除。

旧模型、功能旁路消融和组合修复使用**同一份新 FST**。控制二进制只移植新读取器以接受、校验 `.mreq`，其 timing/cache 模型冻结于本轮修改之前。它不是把不同自然执行窗口当成修复前后。历史旧源的 gem5 CPI 1.500908 与本次的 1.472610 不属于同一窗口。

证据：[采集收据](../tmp/noaccess-repair-20261020/source/c04-777.zstd_r/capture-receipt.json)、[生产角色审计](../tmp/noaccess-repair-20261020/producer-audit.json)、[硬件身份检查](../tmp/noaccess-repair-20261020/source/c04-777.zstd_r/identity-validation.json)、[参数来源](../tmp/noaccess-repair-20261020/source/c04-777.zstd_r/parameter-provenance.json)。

## 2. Producer / 输入协议

热 FST v7 记录保持 **64 bytes**，不占用已经用满的逐记录 flags 或 page-token 字段。header feature bit 6 表示具有完整的 v1 请求角色分类，强制伴随 `<trace>.mreq`。

| 内容 | 编码 |
|---|---|
| header | 56 bytes，magic `FSTMREQ1`、version 1、core、finalized flag、源记录数、稀疏行数、全流 FNV-1a64 hash、行宽 |
| sparse row | 24 bytes：原始 record ordinal、热记录 FNV-1a32 hash、micro member、role、保留字段 |
| role 0 / 缺行 | `Data`：v1 中的普通/其余请求；不证明 cacheability、无 MMIO 或其他本地语义 |
| role 1 | `NoAccess`：本轮支持的纯 store 地址检查，地址范围非空，没有数据层次请求 |
| 旧输入无 capability | 角色未知；不根据 PC、op-class=57 或 n_src 猜测 CDA |

gem5 在退休功能输出处读取 LSQ 保存的 `memReqFlags & Request::NO_ACCESS`，不输入 native latency、cache hit/miss、prepare 结果或动态调试序号。ordinal 唯一定位记录，micro member 辅助保留展开身份；reader、slice/warmup wrapper 与 upgrade 均保留该身份。

声明 capability 却缺文件、未 finalise、错 core/count、错序/越界行、未知 role、非零保留字段、热记录不匹配或全流 hash 不匹配均拒绝。全流校验在最后一条记录返回前完成，不依赖 wrapper 在精确终点后额外调用 EOF。输入内容身份和维护 validator 的指纹也绑定 `.mreq`。

本版本只认证 **NO_ACCESS 与其余请求** 的区分，不宣称覆盖所有 `isLocalAccess()`、MMIO、uncacheable、cache maintenance 等角色。其他后端可输出同类纯功能语义；本轮没有实现 QEMU / DynamoRIO 适配器，也不要求它们提供 gem5 timing 或 Ruby 事实。

## 3. FastSim 行为

CDA 仍经过原 CPU/FU/DTLB 路径，占用 SQ，按原宏边界退休。完成原有 size、physical-address 要求和 uint64 地址范围检查后，才跳过 data-line expansion；不产生 cache/Sequencer/MSHR/Ruby 写，不设置 dirty，不提供虚构的转发数据。转发窗口保留一个空的结构位置，因此 CDA 会使旧候选按 SQ 容量老化。

本地 store 的 detached release 使用原生源映射的 commit-to-IEW/writeback 边，并等待更老 TSO store callback；不追加 cache-service 延迟。本地完成后，下一条 data store 可在相同 LSQ writeback 周期继续。新的 `local_access` 与 `local_scan_ready_cycle` 保存在 transaction-local frontier，经最终接受的 timing proposal 提交，跨 Q 保留，不依赖审计开关。被拒绝的 timing proposal 不发布这些状态。本地旁路也保留 serialize / memory-barrier 对转发窗口的清空，不能因为跳过 data event 而绕过排序。

对更年轻的可选 prepare，响应侧估计满足：

```text
prepare admission estimate >= max(own canWB edge, preceding local completion)
permission ready estimate >= prepare admission estimate + canonical service estimate
```

完成时间 floor 一旦早于后继请求自己的 canWB，便不再增加这条本地屏障的等待；不是永久禁用后继 prepare。旧的 memory-free activity certificate 不消费 SQ 状态，因此含 NO_ACCESS 的区段拒绝走该捷径，使用已有精确 CPU timing 循环。另修正普通 store 生命周期诊断账：send→response 使用实际 `SQ release - send`，不把原 canonical service 全额再记一遍；这项只修诊断守恒。

**这里是响应侧扫描约束估计，不是严格扫描模型，也不是已经证明的原生完成时间下界。** canonical hierarchy replay 的请求准入、cache 状态和资源占用没有随本地屏障完整重排；延迟后可能换 hit/miss/排队路径，旧服务估计未必是晚发服务的真实下界。此限制必须随 CPI 结果保留。

## 4. 同源 CPI 与 elapsed

人口为 recorded-owner workload-task active cycles / task macro instructions，双方分母均为 **21,067,990**。下面每行是同一负载的模型变体，不当成三项负载计算 MAPE。绝对误差 / 单案 MAE 单位为 cycles/macro instruction。

| 变体 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 / 单案 MAE |
|---|---:|---:|---:|---:|
| 旧模型控制 | 1.472610 | 1.076678 | -26.89% | 0.395932 |
| 仅功能旁路与本地 SQ，关闭新增扫描估计的诊断二进制 | 1.472610 | 1.073441 | -27.11% | 0.399169 |
| 功能旁路 + 响应侧 canWB/扫描估计 | 1.472610 | 1.342789 | -8.82% | 0.129822 |

组合修复增加 **5,606,418 active cycles**。消融显示，仅移除虚假 CDA cache 请求并没有改善 CPI；主要数值变化来自后继 store 的 canWB/扫描约束估计。不能把该净变化升级为已经由同源 native 逐请求生命周期认证的纯扫描因果贡献。

| 时间 | gem5 | 旧模型控制 | 组合修复 |
|---|---:|---:|---:|
| elapsed milliseconds | 2.584096 | 1.889412 | 2.356146 |

CPI 使用 active，不使用 elapsed。组合修复仍有约 8.82% 低估；尚未恢复历史“7% 以内”结论。未增加另一套 DTLB，也未打开 LRU 证书。buffer + physical-PTE 路径仍受原兼容性门禁约束；真实 PTE 服务/独立 follower/未来 fill 可见性，以及 canonical cache 请求重排，仍需独立修复和验证，不能把全部剩余误差直接归给其中一项。

维护验证：[控制](../tmp/noaccess-repair-20261020/validation-control-final/cases/c04-777.zstd_r/validation.json)、[功能旁路消融](../tmp/noaccess-repair-20261020/validation-local-only/cases/c04-777.zstd_r/validation.json)、[最终组合](../tmp/noaccess-repair-20261020/validation-release/cases/c04-777.zstd_r/validation.json)。控制/消融的 existing-output rescoring 不是新的仿真或吞吐测量。

## 5. PMU：原始计数与证据缺口

主要目标继续使用退休 load cache 事件，不将 `L2_RQSTS`、LLC 请求或 tag miss 改名替代。下面是 workload-task 原始计数；暂定误差只比较当前独立观察到的子集与模型估计，**Incomplete-evidence errors**，没有硬件 perf 认证。

| 退休事件 | gem5 已观察计数 | FastSim 估计 | 暂定 APE | gem5 unknown | FastSim unknown |
|---|---:|---:|---:|---:|---:|
| L1 load miss | 585,581 | 714,689 | 22.05% | 237,928 | 549,805 |
| L2 load miss | 154,351 | 177,596 | 15.06% | 362,223 | 549,817 |
| L3 load miss | 29,424 | 45,234 | 53.73% | 362,565 | 550,024 |

unknown 不当作 0，也不直接相加推算真实 miss。原生退休边界存在缺口，服务事实及 ISA/event qualification 不完整，错误路径/预取对 cache 状态的间接影响没有完整建模。CPI 改善没有消除这些 PMU 问题。

独立保留 Ruby/SLICC **tag-miss 诊断**，与上表口径不同：

| 诊断 | gem5 | FastSim | APE |
|---|---:|---:|---:|
| L1D tag miss | 791,558 | 779,015 | 1.58% |
| private-L2 tag miss | 170,779 | 199,812 | 17.00% |
| LLC tag miss | 33,775 | 46,408 | 37.40% |

只读取维护 validator 审计的 native Ruby/SLICC 摘要，没有使用 path-class cache 代理作为真值。`passed` 表示来源/守恒门禁通过，不表示精度阈值或硬件 PMU 等价认证。

## 6. 回归与限制

测试先复现未知角色、虚假 cache/转发、遗漏结构 SQ 槽、跨 Q 审计非中性及 activity shortcut 错误接受，再逐项修复。真实 Q 使用 `interval_max_cycles`，不是只改 chunk 大小：256 组 store/local/store/store 分别验证 Q1、Q64 的 generic/materialized 与 audit on/off；纯 128 local、chunk256 验证不能取得 memory-free certificate。额外覆盖缺/错 companion、普通/特殊源记录损坏、size=0、micro member 在 upgrade 中保留、DTLB/宏人口不变。

独立审核发现的两个 Important（跨 Q 状态仅在 audit 模式提交、inactive shortcut 漏 SQ）均已修复；最后增量复核未发现新的 Critical / Important。

- 标准生产构建：`cmake --build build -- -j16`。
- 标准生产测试：`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests`。
- 输入身份 Python/C++ 对照 20 项、维护 native validator 13 项通过；同一 gem5 二进制的正常 NT-store 数据与真实坏地址 fault 回归 2 项通过。
- 无 `.mreq` 的原始 Zstd 来源复放，其 active cycles、CPI、elapsed 和 PMU 与旧默认逐值一致；它仍是角色未知的旧输入，不能视为已经补齐 NO_ACCESS。
- 本轮没有发布新的生产吞吐结论。新增全流校验与部分 certificate 回退有宿主成本，性能需在相同构建、相同输入、安静宿主上独立测量。

最终收据、二进制/配置身份、测试日志与磁盘盘点见实验目录的 [completion.json](../tmp/noaccess-repair-20261020/completion.json)，其中全部检查通过。最终工作区 FST 总量 **21,592,453,664 bytes（21.59 GB）**，磁盘剩余 **430,635,999,232 bytes（430.64 GB）**。FST 上限仍为 100,000,000,000 bytes；保留接受的旧源和新对照源，没有删除有效回归输入。SE 小探针不支持本次 FS 功能快照流程，失败尝试只保留诊断记录，不作为 producer 或精度证据。
