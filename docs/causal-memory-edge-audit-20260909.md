# TeaLeaf 下级内存事件与 gem5 协议核验

日期：2026-09-09。接续 [load 阶段与真实 ROI 接入](causal-load-stages-roi-20260909.md)。

后续的 [cache/权限修复与验证](causal-cache-permission-repair-20260909.md) 已完成本节
定位问题的机制接入。本篇保留修复前源码、配置和实验事实。

本轮定位了 `causal_read` 的具体事件与权限状态差异，并完成一次同输入的 DRAM 参数
对照。没有修改 C++ 求解器或维护配置；参数对照仅保存在 `tmp/`，不是已完成的机制修复。

## 已确认的事件与状态差异

### 1. 目录等待发生在本地 cache 查询之前

[`src/causal_read.cpp`](../src/causal_read.cpp) 的 `cache_requests(0)` 在
`prepare_lookup` 和 miss generation/MSHR 分配前，先把请求送入 `coherence_`。
`coherence_requests()` 对没有本地权限的请求固定安排
`2 * noc_one_way_latency + llc_service_cycles` 的 grant 等待。这次配置为 **26 cycles**。
此后实际 L2→LLC 请求与 LLC→L2 返回仍分别使用网络延迟。

已有前缀首个 DRAM 请求是 core 0、ordinal 6、fragment 0、line 4,016,571：

| 当前模型事件 | cycle |
|---|---:|
| issue / memory FU execute | 4 / 5 |
| coherence acquire / grant | 5 / 31 |
| L1 miss、分配 generation | 31 |
| private L2 miss | 32 |
| LLC miss | 56 |
| DRAM admission / media ready | 92 / 140 |
| LLC fill | 140 |
| private L2 fill / L1 fill / load data | 152 / 152 / 152 |
| writeback / retire | 152 / 154 |

gem5 普通冷读先进入 L1 mandatory queue，L1 miss 才发 GETS；私有 L2 miss 的 GETS
继续进入 LLC，LLC 决定从 memory 或 peer 获得数据及权限。这条路径中没有在查询
L1 前额外完成一次全局 request/grant 往返。当前保守 line lease 的防冲突作用与
协议消息计时混在一起，不能直接从每次请求或 CPI 上减去 26。

### 2. 命中响应、miss 下发和 fill 返回共用或缺少事件边

当前 `latency(level)` 直接返回 `CacheConfig::hit_latency`，同一个值同时用于：

- resident hit 到 reply；
- miss 分配后到下级 request arrival；
- 合并请求的最早 `lookup_ready`。

`finish_fill(1)` 随后同步调用上级 `finish_fill(0)`，私有 L2→L1 没有独立返回事件。
旧前缀中能按 `(core, sequence, fragment, line)` 成对连接的 **136 次 L2/L1 fill，
时间差全部为 0**。目标 `MESI_Three_Level-L1cache.sm` 的 `h_data_to_l0` 和
`hh_xdata_to_l0` 则使用 `l1_response_latency=2`。

因此上级 line 可见性和上级 MSHR 释放都缺少这条返回边。只增加 `l2.hit_latency`
会把时间加到下发端，无法复现返回在途期间的合并、权限变化和新请求准入。

### 3. 普通读 fill 没有获得 E 权限

当前 `exclusive_lines` 只在 write grant 或 dirty write fill 时插入。即使没有任何
共享者，普通 load fill 也不会得到独占权限，后面的 store 会重新走远端 grant 等待。

目标 LLC 的 `ISS + Mem_Data` 使用 `ex_sendExclusiveDataToGetSRequestors`，发送
`DATA_EXCLUSIVE`；私有 L2 和 L1 接收后进入 E。L1 的 `{E,M} + Store -> M`
直接执行 `hh_store_hit`，不需要权限升级。

前缀中有 159 次 acquire→grant 等待 26 cycles，其中：77 次 load miss、59 次 store
miss、6 次 load merge、**17 次 resident L1 store hit**。另有 1,638 次 grant 同拍返回。
一个可复现的本地权限见证为 core 2、line 49,497,663：

- ordinal 34/35 的合并读于 cycle 679 收到数据；
- ordinal 36 的 store 于 681 请求权限，707 grant，707 L1 hit，708 response；
- 这条 line 到该 store 返回为止的所有已观察事件都来自 core 2。

这些数据证明当前实现的具体路径及协议状态缺口；没有据此断言同一 ordinal 在 gem5
当时一定处于 E，也没有把 17×26 当成 CPI 贡献。修复必须让 fill 携带权限，并根据
owner/sharers 与在途事务处理 E/S/M 变化，不能把所有 resident store 当成独占命中。

## 目标网络和目录参数不能沿用旧 envelope

依据本次采集的 `config.json/config.ini` 和本地 gem5 协议源码：

- **实际是 SimpleNetwork**，26 个 switch、26 条 external link、650 条 internal
  link，完全连接。不是旧配置注释中的 Garnet。
- 独立消息经过不同 router 的无竞争网络边为 **4 cycles**：source internal
  routing 1 + internal link 1 + destination external routing 1 + external-out link 1。
  `makeExtInLink` 直接 `addInPort`；没有再使用 external-in link latency。
- `Throttle::operateVnet` 在开始传输时就按 link latency 把消息入目的队列，之后消耗
  bandwidth units。消息大小会阻塞后续传输，不能给孤立首消息再加整包串行化时长。
- directory 冷 fetch 的 `qf_queueMemoryFetchRequest` 用 `to_mem_ctrl_latency=1`；
  **`directory_latency=6` 用于 owner invalidate 分支**，不是每次冷 fetch 的服务延迟。
- MemCtrl 返回后，`AbstractController::recvTimingResp` 先对齐 Ruby `clockEdge()`，
  再以 **1 cycle** 入 directory response queue。`d_sendData` 另用 1 cycle 发向网络。
- LLC resident-hit response 用 `l2_response_latency=2`，但 memory fill 的
  `ex_sendExclusiveDataToGetSRequestors` 用 **`to_l1_latency=1`**，两条边不能混用。

`protocol-edge-ledger.json` 记录完整的孤立冷读消息边，其固定边之和为 29 cycles。
这个源码推导排除了 DRAM、排队/重试、MemCtrl→Ruby 的时钟相位对齐及前面的 memory
FU 执行，并非一个实测请求延迟，也不是要写回模型的总延迟常数。修复应逐事件实现。

## 一次 DRAM 参数对照

沿用同一四核输入及全部功能预热：**2,939,373 UOP**，其中 ROI 为 **40,000 用户 UOP
+1,903 内核 UOP**。使用同一个 FastSim 二进制。生成参数只读取采集硬件配置；
不读取 CPI、PC 或 reference 请求时刻。除支持的 DRAM 参数外，有效配置逐项相同。

| 目标参数 | 原原型配置 | 本次诊断配置 |
|---|---:|---:|
| tCL / tRCD / tRP | 22 / 22 / 22 | 43 / 43 / 43 |
| tBURST | 4 | 11 |
| tRAS / tRTP | 0 / 0 | 97 / 23 |
| tRRD / tRRD_L | 0 / 0 | 11 / 15 |
| tXAW / activation limit | 0 / 0 | 64 / 4 |
| tCCD_L / tCS | 0 / 0 | 16 / 6 |
| MemCtrl media-ready 后静态返回延迟之和 | 0 | 61 |
| max_accesses_per_row | 0 | 16 |

使用采集实际周期 **333 ticks** 向上取整。frontend/backend 各 10,000 ticks，在
两边源码中均于 media ready 后作为**一个和**加入响应事件。因此对总 20,000 ticks
取整为 61 cycles，通过累计取整分配为 frontend 字段 31、backend 字段 30；这不表示
两个分别调度的流水线阶段，也不使用残差拟合。

仍保留 FCFS；row cap 不等于完整 `open_adaptive`；refresh、完整 write 时序、
startup 相位与 command-window 仲裁仍有缺口。整数周期映射也不是 gem5 tick 域等价。

| 观察量 | 原原型配置 | DRAM 参数对照 |
|---|---:|---:|
| 全程 L1/L2/LLC miss generations | 7,233 / 7,233 / 7,232 | 相同 |
| ROI DRAM reads | 248 | 248 |
| 全程 DRAM 请求占用积分，request·cycles | 267,809 | 599,993 |
| 全程 L1 MSHR 占用积分，entry·cycles | 795,824 | 1,569,158 |
| 全程 ROB 导致的 dispatch 阻塞周期，跨核合计 | 364,927 | 789,310 |
| 全程 ROB 阻塞 episodes | 2,782 | 3,244 |
| ROI core 0 cycles | 1,528 | 1,528 |
| ROI core 1 cycles | 10,318 | 17,896 |
| ROI core 2 cycles | 4,228 | 4,962 |
| ROI core 3 cycles | 11,032 | 18,892 |
| ROI cycles / user UOP，诊断值 | 0.67765 | 1.08195 |

二进制/输入指纹不变、非 DRAM 配置相同、功能人口/扩展依赖/回调/fill 守恒通过。
各级 miss 数相同而驻留和阻塞显著变化，支持“旧 DRAM 参数明显缩短当前模型中的等待
及反压”，不能仅靠 miss 数判断时序正确。这里的 ROB 统计包括预热，不能直接对照
gem5 ROI rename stall 计数或把阻塞周期变化当作 ROI CPI 贡献。

**没有获得正式 CPI 改善结论。** 新结果同时暴露了现有 cache/coherence 事件多计与
漏计仍在；I-side/翻译、控制器调度和跨模拟器测量时钟窗也未闭合。不要因为总 CPI
看起来接近参考，就保留已知错误或反调 DRAM 参数。

## 下一步修复边界与最少验证

优先修 cache/permission 生命周期：

1. 拆开 lookup、miss request、hit response、fill response 的事件语义；下级 fill
   释放本级资源，返回消息实际到达后才填充并释放上级 generation/MSHR。
2. 本地 tag/permission 判定后，由 miss/upgrade 的同一事务发起目录处理；保留
   对在途冲突的串行化约束，把消息等待放回拥有该消息的事件。
3. fill 明确传递共享/独占权限；实现无共享者读回 E、本地 E→M，以及 peer 请求导致
   的 downgrade/invalidate，避免延迟 fill 恢复已失效的数据或权限。

必要机制场景覆盖：冷读的逐级请求/返回及返回在途合并；独占读后本地写；跨核共享后
写入与在途 fill 的冲突。实现后一次构建/现有测试及同一 TeaLeaf ROI 对照即可。
本轮只有审计脚本/文档和诊断配置，没有源代码变更，因此未重复构建、整套测试、前缀
模拟或 gem5 采集，也未扩展到其他 workload/设备。

## 产物与复现

[`tmp/causal-memory-edge-audit-20260909/`](../tmp/causal-memory-edge-audit-20260909/)：

- `audit.py`：prepare、compare、protocol-edges 三个独立入口；prepare 生成参数时不读取标签。
- `event-edge-audit.json` / `first-cold-load.csv`：旧前缀事件的读取与连接。
- `network-controller-audit.json` / `protocol-edge-ledger.json`：目标控制器与协议分支证据。
- `dram-parameter-audit.json` / `fingerprints.json`：转换来源、边界限制与源文件/输入指纹。
- `dram-only-roi/`：一次模拟的 config、manifest、run log、stats 与 comparison/validation log。

DRAM 参数审计复用维护工具 `tools/audit_gem5_dram_parameters.py::audit`，没有使用
该旧工具针对 interval 路径生成配置的分支，避免引入无关配置变更。
