# gem5 DRAM 语义对齐：2026-09-07

## 结论与默认状态

**gem5 是精度 baseline。已实现并验证一个默认关闭的 DRAM 调度时刻修正，但完整 ROI 候选未通过正误差控制，不能推广，也没有新的 P99 改善结论。** Q 固定 1024，pending-fill 及其准入/store 子开关继续关闭。

本轮比上一轮多闭合了三项证据：实际 gem5 参数与运行路径审计；原二进制的三请求差分；相同到达流上的完整 DRAM 请求排序差分。以下区分“同到达流的控制器语义”和“当前生产模型的 CPI”，不能把前者的成功外推为后者。

## 1. 参考身份和实际配置

沿用冻结 `tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json`，没有改 gem5 reference、trace、ROI 或分母。原 gem5 二进制 SHA256 为 `3355538485525218f10c545d19950ad6961de2dd28b802ba767f28092a7fc9ac`。

参考配置来自 L1D64 C4 原实验的 `config.ini`：

`tmp/spec2026-uarch-exploration-v1-native-v28_2/source/sample/mesi-three-level-3GiB/4c/811.tealeaf_s/849922a27a2bfb259cb0/20260903T041808Z/config.ini`

不是采用另一个 gem5 checkout 的默认参数。控制器域实际周期为 **333 ticks/CPU cycle**，每 tick 为 1 ps。

| 项目 | 本次 gem5 | 当前 FastSim | 判断 |
|---|---:|---:|---|
| 地址映射 | RoRaBaCoCh，64 B 通道交织 | 相同计算 | 新捕获的 1,010 个地址全部匹配 channel/rank/bank/row |
| 通道 / rank 每通道 / bank 每 rank / bank group 每 rank | 8 / 2 / 16 / 4 | 8 / 2 / 16 / 4 | 几何一致；`banks_per_channel` 是旧键名，实际含义为每 rank |
| 行缓冲 | 8,192 B | 8,192 B | 一致 |
| tCL / tRCD / tRP | 14,160 ticks = 42.5225 cycles | 各 43 cycles | 整数周期近似 |
| tRAS / tRTP | 32,000 / 7,500 ticks = 96.0961 / 22.5225 cycles | 0 / 0 | 当前关闭 |
| tRRD / tRRD_L / tXAW / ACT limit | 3,332 / 4,900 / 21,000 ticks / 4 | 全部 0 | 当前关闭 |
| tCCD_L / tCS | 5,000 / 1,666 ticks = 15.0150 / 5.0030 cycles | 0 / 0 | 当前关闭 |
| tBURST | 3,332 ticks = 10.0060 cycles | 10 | 量化差异远小于本窗口的百周期缺口 |
| frontend / backend 静态延迟 | 各 10,000 ticks = 30.0300 cycles | 各 30 | gem5 两项都在 DRAM ready 后加到响应上，不能把 frontend 当成排队前的固定等待 |
| tREFI / tRFC | 7,800,000 / 350,000 ticks | 没有独立状态 | refresh 缺失仍未修复 |
| 读 / 写缓冲容量 | 64 / 128 | 64 / 128 | 数值一致，生命周期不一致 |
| page policy | open_adaptive，row cap 16 | 有界队列扫描；row cap 16 | 不能称完整等价 |

恢复关闭约束的实验使用实际 333-tick 域向上取整：tRAS=97、tRTP=23、tRRD=11、tRRD_L=15、tXAW=64、ACT limit=4、tCCD_L=16、tCS=6。已有 CL/RCD/RP/burst/frontend/backend 不变，以隔离关闭约束的影响。离线差分则直接使用原始 ticks，排除取整影响。旧注释中的 tRAS=96、tCS=5 不是实际 333-tick 时钟下的保守向上取整。

读写方向并未完全对齐：FastSim 没有独立的 tCWL、tWR、tWTR、tRTW 等读写转换字段，缓冲写服务复用了读服务日历。本次有界捕获全部为读，所以不能用这些差异解释这段尾部，也未据此增加写延迟。

源码依据：原 gem5 `/data00/yinhaolang/gem5-fs/src/mem/dram_interface.cc` 的 `decodePacket`、`activateBank`、`doBurstAccess`、`Rank::processRefreshEvent`，以及 `mem_ctrl.cc` 的 `readQueueFull`、`processRespondEvent`、`doBurstAccess`。FastSim 对应 `src/simulator.cpp` 的 `DramModel` 和 `apply_frfcfs_dram_repair`。

## 2. 配置写着 FR-FCFS，不代表运行时执行 FR-FCFS

实际 gem5 选择器使用当前已到达的队列，并区分 seamless row hit、hidden bank preparation、prepped row hit 等情况。FastSim 原批处理选择器使用 row-hit-first 和有界候选，且会把“未来、但早于最早可发命令”的到达也放入选择集合。这是离线差分发现的设计差异。

**更重要的运行时事实：当前 C4/C8 根本不进入该批处理选择器。** `frfcfs_topology_scaled_window=true` 使用 `(cores × ranks / channels) - 1` 推断候选数，并把 C4/C8 压到 1 后直接 bypass，保留 canonical FCFS。这个推断不是 gem5 的硬件参数。

L1D64 C4 当前运行计数：effective selection window=1；bypass epochs=5,032；bypass requests=188,124；candidate epochs=0；FR-FCFS passes=0。因此，仅打开新选择时刻开关时，完整 CPI 完全不变。**不能将未运行的批处理选择器缺陷称为这个 C4 生产 case 的直接根因。**

gem5 的 64 项读容量包括 `readQueue + respQueue`，在 DRAM ready 后才释放；FastSim batch 只把待调度项计入该容量。新捕获中，每通道读队列加响应队列最大均为 **11**，没有达到 64。该语义缺口需要修复，但不是本窗口的容量瓶颈。debug 也没有 command-window bandwidth contention；不能凭该字段缺失就把命令总线带宽当成本窗口根因。

## 3. 新捕获和可重复的调度反例

使用原二进制重新捕获完整 10M user UOP/core，增加有界 `MemCtrl` 日志；耗时 **313.84 秒**。新捕获的四个完整 FST SHA256、全部 CPL 行与原实验相同。诊断 tick 区间沿用 `[21705779044444,21705788394091)`。

区间内有 **1,010 个 ReadReq 和 1,010 个唯一配对的 DRAM 调度事件**；没有待调度请求的首尾配对缺口，但捕获起点仍可能有已经预约的命令和在途响应，因此不假装拥有完整初始状态。另捕获了 16 个 channel/rank refresh 恢复事件。

### 三请求微型 gem5 差分

用原 gem5 二进制、相同 DDR4 参数，在独立 `PyTrafficGen → MemCtrl` 配置中运行：

| ordinal | 到达 tick | 物理地址 | 含义 | gem5 RD tick | 原选择器 RD tick | 新开关 RD tick |
|---|---:|---:|---|---:|---:|---:|
| 0 | 50,000 | 0x10000 | 控制器启动时间之后，预热 bank 1 的行 | 64,160 | 64,160 | 64,160 |
| 1 | 100,000 | 0 | 冷 bank 0 | 114,160 | 114,160 | 114,160 |
| 2 | 110,000 | 0x10000 | 稍后到达的行命中 | 117,492 | **110,000** | **117,492** |

gem5 在 tick 100,000 已选中请求 1 并预约了未来命令；请求 2 尚未到达，不能撤销这次选择。原批处理选择器却提前看到请求 2，令其抢到请求 1 前面。新实现限制选择集合在调度事件时刻，并使用 gem5 的下一事件公式 `nextBurstAt - (tRP + tRCD)`；PRE/ACT 计算也不能早于该选择事件。

该测试核对原 gem5 实测 RD/data-ready tick；返回期望值按 `processRespondEvent` 再加配置中的 frontend/backend 各 10,000 ticks，已纳入 `fastsim_tests`。首请求放在 50,000 ticks，以隔离 `MemCtrl::startup` 将 `nextBurstAt` 初始化为 `tRP+tRCD=28,320` 的状态；本测试不宣称初始化语义等价。实验键为 `dram.frfcfs_causal_selection=false`，默认关闭。它只修复批内选择事件边界，不声称实现完整 gem5 FR-FCFS、跨批全局控制器事件状态、refresh 或读响应队列容量。

### 相同到达流上的真实关键请求

用 1,010 个 gem5 到达事件驱动 FastSim 的独立 DRAM probe，硬件参数均使用精确 ticks：

- 对上一轮 20 条关键 load 中 **refresh 前的 9 条**，原批处理选择器的命令均提前 **172.6697 cycles**；新开关使这 9 条的 RD tick 全部与原 FS gem5 完全相同。
- 排除恰在 refresh 恢复时选中的最长请求后，其余 19 条的命令偏差中位数从 **−172.6697** 到 **0 cycles**；新范围为 **−37.5255..+42.5345 cycles**。其中存在 refresh 后的冷行访问，不能称为“19 条完全不受 refresh 影响的请求”。
- 20 条中，gem5 13 条 row hit、7 条 row miss。最长请求及其后的冷行状态支持补充 refresh 生命周期，不能只给一个 tRFC 等待常数。
- 在离线 probe 中把选择候选从 8 改为 64，不改变这些关键请求的结果；这不是 core Q 扫描。候选数为 1 的 FCFS 诊断则不同，不能拿 8 候选原选择器的 −172.67 作为当前生产 FCFS 的误差。
- 如果直接给定 gem5 的选择顺序和选择时刻，启用实际命令参数后，13 条 row hit 的 RD tick 全部匹配；有 refresh 后行状态差异的请求仍不匹配。这是有条件的日历验证，不能作为推理输入或 CPI 预测。

具体真实见证：channel 4 的请求 `0x128a9100` 在 tick `21705784040117` 已被 gem5 选中，RD 预约在 `21705784111265`。关键 load `0x1325d100` 到 tick `21705784057098` 才到达，RD 应在 `21705784114597`；旧的 8 候选批处理 probe 却让它在自己的到达 tick 就发出 RD。该请求由原 gem5 LSQ/Seq Done 及上一轮完整指令身份关联，不是人为拼出的 workload 常数。

## 4. 完整 ROI 验收：局部对齐没有通过整体门禁

两个 case 都是完整原 trace，每核 10M user UOP。表格为 signed user-UOP CPI error，不是 formal macro CPI。

| 候选 | L1D64 C4 | LLC32 C4 正误差控制 |
|---|---:|---:|
| 当前关闭状态 | −17.9910% | +9.6487% |
| 仅恢复关闭的命令约束 | −14.2309% | +17.5424% |
| 上述约束 + 新开关，但仍保持拓扑 bypass | −14.2309% | +17.5424% |
| 上述约束 + 新开关 + 关闭拓扑 bypass，实际候选数 8 | **−14.7013%** | **+14.6878%** |

实际启用后的 L1D64/LLC32 分别有 5,253/5,928 个稳定求解 epoch、10,506/11,856 个 pass，fallback 均为 0。最后一行确实运行了修正，不能把失败解释为开关未生效。

**停止：不推广、不做完整 formal40/DSE54、不继续扫描这些等待参数。** L1D64 改善 3.2897 个百分点，但正误差恶化 5.0391 个百分点。该实验表明：修正真实到达流的局部调度规则，仍不足以修正部署路径上的整体响应时间；它没有证明所有剩余误差都来自 DRAM。

### 独立吞吐复测

矩阵和 gem5 补采结束后，同一新二进制、`numactl --physcpubind=0-47 --membind=0`、串行交错各 3 次；12 次运行的目标统计均与各自 pilot 相同。比较最后一行实际激活候选与关闭状态：

| case | 关闭：M user UOP/s | 候选：M user UOP/s | 吞吐变化 | 测量段秒数：关闭 → 候选 | 外部总耗时秒数：关闭 → 候选 |
|---|---:|---:|---:|---:|---:|
| L1D64 C4 | 6.1975 | 5.5971 | **−9.6890%** | 6.4542 → 7.1466 | 7.2297 → 7.9323 |
| LLC32 C4 | 5.9540 | 4.9084 | **−17.5613%** | 6.7182 → 8.1493 | 7.0797 → 8.5818 |

开启路径新增了可观测的 10,506/11,856 次 DRAM 求解 pass；FR-FCFS 内部累计计时中位数为 2.3054/2.6929 秒。它不是端到端净增加量，不能与其他嵌套计时相加。端到端损失以上表实测为准；尚未独立拆出排序、状态复制、参数导致的 epoch 数变化各自占比。

## 5. 后续优先级和实现边界

1. **先查 FastSim 控制器到达流与状态的生成。** 按完整请求身份对齐 corrected issue、canonical tag/DRAM arrival、队列准入、命令预约、数据 ready；保留跨批状态。上一轮关键请求 canonical arrival 比校正 issue 早 85–962 cycles，且其 canonical DRAM 排队全为 0；本轮同到达流差分可复用为检查器。应先证明生成器/反馈路径在哪里改变了到达集合，而不是给所有 load 追加等待。
2. **补 refresh 的完整 rank 生命周期。** 源码是 drain、满足 PRE/在途响应条件、tRFC、关闭全部行、恢复 ACT；本次 16 个 rank 恢复时刻并不全部相同。相位必须来自可部署状态，不得导入 gem5 oracle tick。独立微型差分应包含正在服务的另一个 rank，以防把 rank refresh 错做全通道停机。
3. **控制宿主成本。** 本轮只调用现有 DRAM repair，并未增加第二遍完整核心反馈；但它仍在既有 canonical 工作之后重建/排序请求并求解多 pass。下一方案应替换重复求解工作，同时证明请求/资源生命周期正确，不能仅继续叠加补偿层。

保留代码和实验配置 `configs/gem5-exp-dram-causal-selection.cfg` 供显式复现；该配置已暂停，维护入口没有引用它。

## 6. 产物和验证

本轮产物根目录：`tmp/gem5-dram-alignment-20260907/`。

- `parameters.json`、两个 pilot 的逐 case `parameters.json`：实际配置值、取整规则、原始配置指纹；新工具还会输出运行时 FR-FCFS 计数。
- `l1d64k8-c04-debug/`：采集 argv、完整 CPL/FST、有界 debug、退出状态。
- `micro-gem5-r3/`、`future-hit.tsv`：最终三请求原 gem5 差分。初次工具运行在有限 generator 终止边界失败，已修正探针的 duration 边界。r2 还显示 tick 0 首请求受 gem5 初始 `nextBurstAt` 影响；最终将预热请求放到 50,000 ticks，所有三条 RD 均重新核对。早期目录不作为完整通过证据。
- `calendar-causal/calendar-audit.json`：新捕获指纹检查、地址映射、20 条请求与不同 probe 的命令时间；同目录保存原始请求/命令 TSV 和参数配置。
- `pilot-results.json`、`causal-pilot/`、`active-causal-pilot/`：区分参数实验、未激活开关、实际激活路径，防止把空操作当作优化。
- `default-preservation/`：C16/C32 关闭开关后的完整回归；另两个 C4 控制也已核对。四个 case 的 `cores`、`threads`、`totals`、CHA 和 `scope_metrics`（排除宿主吞吐）均与冻结当前 baseline 一致。
- `bench-results.json`：12 次独立交错吞吐测量、完整 argv、二进制指纹、目标一致性及中位数。

生产比较二进制：原 `c5875a45a03c86edf8394385e00e32d669b0fe0ab8e9a71d1b28c7058f6ecaf6`；本轮实现后 `f68c7ea21f68205be9d44312f034a409ac5833eb24c281232e8273adb83abf57`。原始参数实验使用前者；新开关、实际激活实验和后续吞吐比较使用后者。

验证通过：`cmake --build build -- -j16`、`./build/fastsim_tests`（含原 gem5 三请求期望值）、Python 编译检查、真实 1,010 请求差分、四个完整关闭状态回归、`git diff --check`。没有 commit/push。

主要工具：`tools/audit_gem5_dram_parameters.py`、`tools/audit_gem5_dram_calendar.py`、`tools/replay_dram_probe.cpp`、`tools/gem5_dram_trace_probe.py`；`tools/collect_tail_timing.py` 增加有界 MemCtrl debug 选项。这些 oracle 探针不进入生产推理路径。
