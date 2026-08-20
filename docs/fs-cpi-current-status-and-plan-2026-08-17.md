# FastSim FS CPI 当前方案、证据与实施路线（2026-08-17）

本文是当前 FS 精度工作的总入口。它只汇总已有代码、同口径实验和可审计的
反例，不用 workload ID、gem5 在线 timing/PMU oracle 或逐 case 常数解释误差。
细节分别见文末链接的专项报告。

## 1. 比较基线与不可变口径

gem5 是唯一 CPI baseline。正式结果必须同时固定：

- gem5 v28.1 FS、MESI three-level、目标 C4/C8 拓扑和对应 `config.ini`；
- 相同 ROI、functional warmup 和每核 10M measured user records；
- FST v7、user-UOP denominator、user 与 user+kernel 两种 scope；
- 同一套 trace、配置 hash、gem5 stats 身份和 FastSim binary provenance。

受维护的 FS 入口是：

```text
configs/gem5-v28_1-fs-user.cfg
configs/gem5-v28_1-fs-user-plus-kernel.cfg
```

共享的 `configs/gem5-v28_1-time-epoch.cfg` 保留 `se_atomic`，只作为 SE/control
配置；FS 入口显式覆盖为 `timing_walk/12`。因此“FS 路径意外走 SE ATOMIC”这个
部署缺陷已经关闭，工具默认路径和配置 provenance 测试会阻止回退。

当前 profile identity 审计覆盖 129 个直接或派生字段，其中 121 个数值匹配，
8 个未默认启用的差异是 DDR4 command 约束。FastSim 虽已有这些候选参数，但
controller arrival/order 和完整 DDR 状态机尚未与 gem5 等价；仅填入相同数值会
制造伪对齐。因此当前结论是“受支持字段有证据地对齐”，不是“全部微架构语义
完全等价”。

## 2. 已接受并进入 FS 默认配置的模型

| 组件 | 决策 | 证据边界 |
|---|---|---|
| DTLB `timing_walk/12` | 默认开启 | 修复 Neutron 主误差；architectural/timing 双状态域守恒 |
| private-L2/LLC TreePLRU | 默认开启 | 与目标 gem5 replacement state machine 直接映射；完整 C4/C8 gate 通过 |
| scope-specific kernel/page-fault profile | 默认开启 | 新受维护 profile 与已接受 overlay 的逐 case CPI bit-identical |
| page-fault cache-state warming | 默认开启 | 保留 handler 对 cache state 的功能影响，user scope 不计 kernel service |
| frontend response ledger | 默认统计、CPI-neutral | request/response/resume aggregate 与 per-core 守恒 |

这些决策冻结在两个 FS profile 中；不依赖项目 `tmp/` 下的实验 overlay。

## 3. guest-PTE 与 measurement 边界方案

### 3.1 为什么仅延长 warmup 不够

committed functional trace 只描述最终退休的指令。它不能区分：页面在 warmup
起点已经 present、在 warmup 内被 fault-in、measurement 起点仍 nonpresent，或
page fault 已在 measurement marker 前进入内核但提交重试出现在 marker 后。
所以首触 heuristic 和仅 initial snapshot 都不具备事件身份精度。

### 3.2 已实现的数据合同

gem5 TaoTrace 从恢复后的 **guest** CR3/page table 读取两次快照：

1. functional warmup 起点的 `initial-pte-state.json`；
2. serial ROI marker 内的 `roi-entry-page-state.json`（旧采集物名为
   `measurement-pte-state.json`，读取兼容）。

producer 同时跟踪 marker 前已经接受、但尚未返回 user commit 的精确 x86
`PageFault`，在 marker 上冻结 `(core, virtual_page)`。FST `.vmap` 保持 32-byte
row，不改变 64-byte hot record：

| bit | 含义 |
|---:|---|
| 0 | physical page valid |
| 1/2 | initial PTE valid/present |
| 3/4 | ROI-entry page-state valid/present |
| 5 | measurement 开启时该 stream 有精确 in-flight page fault |

FastSim measurement 首触规则是：known-present 不选 fault；known-nonpresent 且
bit 5 清零时选择一次 fault；bit 5 置位时不再计 measured kernel service，但保留
4 KiB page-fill/cache-state 效果；unknown 才回退到原有非 oracle 模型。跨 stream
owner 在并行 replay 前确定，不能由 host 调度决定事件归属。

该方案不采集 Linux 宿主机 `/proc/pagemap`，也不把宿主机 PTE 当作 guest 或
drmemtrace 事实。当前 DR 转换路径将可选 bits 1--5 保持 unknown。多 CR3/多地址
空间暂不建模，producer 遇到不一致时 fail closed。

### 3.3 NAMD 地址级因果验证

下表来自新采集的每核 100K records 定向窗口，只验证机制，不能与正式 10M
矩阵混为同一评分：

| NAMD | initial nonpresent | measurement nonpresent | boundary in-flight | gem5 measured `#PF` | FastSim selected |
|---|---:|---:|---:|---:|---:|
| C4 | 24 | 24 | 0 | 24 | 24 |
| C8 | 21 | 4 | 2 | 2 | 2 |

C8 修复前依据 PTE 选择 4 次，修复后选择 2 次并抑制 2 个 boundary-in-flight
事件，cache-state page fills 仍为 4。user CPI 保持 `0.325816`，inclusive CPI
从 `0.391726` 降为 `0.357111`，而 gem5 是 `0.410685`。旧模型较小的 inclusive
误差来自两个多算 page fault 对缺失 user cycles 的偶然补偿，不是更准确。

C4 的 24 个相邻 nonpresent pages 全部产生独立 architectural page faults，直接
否定“相邻页合并”方案。Stockfish pilot 的 311 个进程页全部 present，gem5
measurement `#PF=0`，因此 PTE/page-fault 不能解释其约 20% 尾部误差。

## 4. 当前正式精度与剩余组件

受维护 FS profile 的完整 20-case、双 scope 结果如下：

| workload | C4 user | C8 user | C4 user+kernel | C8 user+kernel |
|---|---:|---:|---:|---:|
| Stockfish | 23.09% | 20.26% | 25.04% | 21.88% |
| omnetpp | 10.46% | 11.02% | 11.05% | 11.76% |
| zstd | 3.60% | 4.79% | 3.42% | 1.50% |
| LBM | 7.02% | 2.78% | 5.50% | 1.93% |
| SPH | 4.67% | 6.85% | 10.95% | 11.26% |
| TeaLeaf | 6.01% | 12.58% | 5.43% | 12.44% |
| NAb | 8.76% | 12.45% | 7.40% | 11.04% |
| Graph500 | 7.46% | 3.12% | 10.74% | 0.74% |
| NAMD | 13.57% | 3.75% | 13.34% | 17.98% |
| Neutron | 0.44% | 2.49% | 0.47% | 2.70% |
| **mean** | **8.51%** | **8.01%** | **9.33%** | **9.32%** |

证据支持按组件处理，而不是增加全局 CPI scalar：

1. **Stockfish：缺失 speculative frontend/O3 request stream。** committed
   fetch response 的 98.9%/99.3% 已暴露在 C4/C8 关键路径；committed-PC L1I
   只覆盖 gap 的 1.18%/1.59%，finite committed rename free list 又产生零 stall。
   当前输入缺少 wrong-path fetch、refetch 和 speculative occupancy，无法构造
   source-equivalent I-side；固定 refill penalty 只会重复收费。
2. **NAMD C4：user timing 与 page-fault service 都不足。** 100K 因果窗口中
   fault 数量已经精确，但 24 个 handler 只预测 325,272 cycles，gem5 为
   496,307，低估 34.46%；同时 user execution 少 120,320 cycles。
3. **NAMD C8：主要是 user timing。** 两次 fault 为 27,106 versus 28,660
   cycles，仅低估 5.42%；移除 false compensation 后，user 路径仍低估约
   12.4%。正式 10M 窗口与 100K 诊断的数值不同，但都要求把 user 与 kernel
   根因分开。
4. **TeaLeaf/LBM：controller arrival/read-write service order。** partial DDR
   calendar 会改善部分均值，但 four-ACT 和 unscaled FR-FCFS 出现错误方向，
   说明当前 reconstructed arrival/order 尚不能承载完整 gem5 参数。
5. **SPH/TeaLeaf/NAb combined residual：kernel event 与 OoO pressure。** 需要
   event-to-resume、response-to-retire 和 committed occupancy 守恒，不能把 gem5
   PMU/stall counter 作为在线输入。

## 5. 候选模型处置

| 候选 | 当前处置 | 原因 |
|---|---|---|
| dual-PTE + exact boundary bit 5 | 实现完成，保持显式 candidate | NAMD 地址级 gate 通过；完整 held-out 矩阵尚未完成，DR 字段仍 unknown |
| committed-PC L1I | experimental/off | 请求流不完整；Stockfish 修复量不足；Graph500 C8 回归 |
| partial DRAM command calendar | experimental/off | 状态机和 arrival/order 未等价；未解决 P90/tail |
| state-only wrong path | experimental/off | committed 输入下不可辨识，pilot 未过跨负载 gate |
| unscaled FR-FCFS/full-queue proxy | 拒绝 | 当前 functional arrival stream 上多负载回归 |
| finite committed rename free list | 不默认开启 | formal 数据 stall 全为零，CPI bit-identical |
| fetch refill latency 2 | 拒绝 | 与已暴露 response wait 重复收费并造成回归 |

## 6. 实施顺序与验收门

1. 完成 dual-PTE 的全 C4/C8、双 scope held-out gate；要求 vmap bits、process
   snapshot、selected `#PF` 和 cache-state 守恒。oracle 只用于离线诊断，不能被
   FastSim inference 读取。
2. 对 NAMD 建立 committed user `dependency/FU -> response -> retire` 差额 ledger；
   对 C4 page-fault service 增加 topology、并发 fault、memory contention 等合法
   状态特征，以 C4 校准、C8 held out，禁止 workload ID。
3. 若目标是 Stockfish 的 gem5 等价 I-side，先扩展 producer contract，输出非
   timing-oracle 的 fetch/refetch/wrong-path request stream；若输入永远只有
   committed trace，只能明确接受统计近似和不可辨识边界。
4. 为 memory workload 实现并守恒 `request create -> controller enqueue ->
   select -> command/bus -> response`，随后逐项补完整 DDR4 状态机和参数。
5. 每项修复都必须冻结其他参数，报告逐 workload APE、user/user+kernel、C4/C8、
   PMU conservation 和吞吐；不得用 aggregate mean 隐藏 Stockfish/NAMD 回归，
   也不得接受两个错误互相抵消得到的 CPI。

默认开启条件是：代码/格式单元测试通过、完整矩阵改善或不回归、关键事件身份
守恒、C8 独立 holdout 通过，并且配置与 trace provenance 可从受维护文件重现。

## 7. 代码与专项证据索引

- C4/C8 全组件微架构配置与状态机语义审计：
  `docs/fs-gem5-uarch-semantic-alignment-audit-2026-08-18.md`
- guest-PTE 详细设计与 NAMD 地址证据：
  `docs/gem5-initial-pte-page-fault-model-2026-08-17.md`
- FS profile、I-side ledger 和参数 identity：
  `docs/fs-profile-frontend-repair-2026-08-17.md`
- DTLB 根因和正式修复：
  `docs/fs-cpi-dtlb-root-cause-repair-2026-08-17.md`
- 已实现候选的完整消融：
  `docs/fs-cpi-candidate-model-audit-2026-08-17.md`
- FST/drmemtrace 可携带信息边界：
  `docs/fst-v7-drmemtrace-conversion-contract.md`
- FastSim PTE 字段与规则：`include/fastsim/types.hpp`、`src/trace.cpp`、
  `src/simulator.cpp`、`src/main.cpp`；回归覆盖在 `tests/test_main.cpp`。
- gem5 producer 修改位于独立 checkout 的 `src/cpu/o3/probe/tao_trace.{cc,hh}`
  和 `src/arch/x86/faults.hh`；它不属于本 FastSim Git 仓库，本文档不声称已经
  将外部 gem5 checkout 发布到 Codebase。

所有批量 trace、oracle、编译产物和实验报告保留在项目 `tmp/`，不进入 Git。
