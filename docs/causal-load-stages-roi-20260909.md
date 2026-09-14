# Load 执行阶段修复与 TeaLeaf ROI 采样接入

日期：2026-09-09。接续 [前缀配对门禁](causal-core-memory-prefix-gate-20260909.md)。

后续已完成 [下级内存事件与协议审计](causal-memory-edge-audit-20260909.md)，定位请求/
返回边和独占权限缺口，并完成一次同输入的 DRAM 参数对照；该对照不是机制修复完成。

本轮补齐 `causal_read` 中普通 load 的内存 FU 执行阶段，并将实验配置的 L1D 命中
延迟按目标控制器的准入/回调边界定义。新路径首次跑通此前采集的 TeaLeaf 四核
每核 10,000 条用户 UOP 的真实 ROI 采样，保留全部功能预热。尚未验证正式 CPI 改善。

## 机制与源码证据

此前 [`src/causal_read.cpp`](../src/causal_read.cpp) 为 store 调度
`issue + issue_to_execute + memory_FU_latency`，却为 load 调度
`issue + issue_to_execute`。在 O3 实验配置中 `issue_to_execute=0`，因此 load
在 issue 当拍就能做地址生成、检查 SQ、进入 cache，漏掉了内存 FU 的执行阶段。

现统一使用 memory FU latency（当前目标为 1）推进 load/store 的执行事件。
`issue_to_execute` 是现有额外流水线延迟；普通 ALU 的 FU latency 抽象保持原样。
这条边发生在访存准入之前，会重新决定 SQ 顺序等待、请求交错、命中/合并及后继依赖，
没有在最终 CPI 上补一拍，也没有按 PC 或 workload 判断。

本地 gem5 源码依据：

- `src/cpu/o3/inst_queue.cc::scheduleReadyInsts()` 将单拍 memory FU 指令送入
  issue-to-execute 队列，`iew.cc` 从延后一拍的队列读指令，再调用 LSQ 执行地址生成。
- `mem/ruby/system/Sequencer.cc::issueRequest()` 使用 controller mandatory-queue
  latency；采集目标的该值为 1。`MESI_Three_Level-L0cache.sm::h_load_hit` 命中时
  直接调用 `readCallback`，`RubyPort.cc::MemResponsePort::hitCallback()` 在同 tick
  返回响应，不额外增加一个端口延迟。

因此 [`causal-gem5-o3-prototype.cfg`](../configs/causal-gem5-o3-prototype.cfg) 中
`cache.l1d.hit_latency=1` 表示**准入到响应**。旧 interval 配置的端到端 load envelope
不能直接作为新事件路径的 L1 服务延迟。本轮只对齐这一已核验的目标边，其他层次仍是
原型参数，维护 native-FS 默认未切换。

已有诊断证据来自
`tmp/cross-workload-native-v7-20260908/tealeaf-l1d64/dense-core1-pairs.json`：

| 同一历史诊断窗口中的普通 load | 数量 | gem5 阶段时长 |
|---|---:|---|
| 有完整 native 准入/回调时刻 | 2,168 | issue→准入全部为 1 cycle |
| 其中 L1D 命中 | 1,666 | 准入→回调全部为 1 cycle |
| 其中 Sequencer 合并 | 421 | 回调由各自真实 generation 决定 |
| 其中 memory read | 81 | 准入→回调为 142–369 cycles |

这些时刻只用于离线核验源码定义，不进入求解器，也不作为 workload-held-out 证据。
本轮未重新采集 gem5。

## 分开记录数据可用与实际 writeback

新增观察事件 `completion_ready`，它不新增调度事件或延迟：

| 事件 | 含义 |
|---|---|
| `ready` | 所需 producer 已 writeback，操作数就绪 |
| `execute` | memory FU 执行/地址生成阶段到达 |
| L1 `hit/miss/merge` | 当前片实际准入 |
| `data` | 某一 load 片返回，包括明确标记的域外本地完成 |
| `completion_ready` | 全部片已返回，且 core 最小完成条件满足，可竞争 WB |
| `writeback` | 获得 WB 带宽，真正唤醒消费者并释放 memory IQ 项 |
| `retire` | 顺序退休并释放 ROB/LQ |

gem5 旧 `complete_tick` 仍不等同 load 数据返回或 producer writeback。对照必须使用
对应的真实 probe/callback；本轮没有改写 reference label 来制造一致。

## 必要验证

一次构建与一次 `./build/fastsim_tests` 通过。新增机制场景验证：

- 三个同线 load 同时 issue，下一拍执行和准入，共用一次 miss 回调；WB 宽度为 1
  时分别延后一拍写回，消费者等待自己的 producer，而不是最早的数据返回。
- 同线依赖访问在第一次 fill 后命中，执行边与 L1 响应边各一拍。
- 配置的额外执行延迟对 load 与 ALU 都生效。

原“同 tick callback/新准入”测试保留；其独立 ALU 在 callback 前一拍完成，使年轻
load 经过新执行阶段后仍与 callback 同拍准入。没有放松资源或响应顺序断言。

固定四核前缀仅重跑一次，14,560 UOP、27 条扩展依赖和功能字段保持不变。
独立事件审计仍检查 RAW/TSO、序列化、回调、各阶段宽度、ROB/IQ/LQ/SQ、MSHR/DRAM
占用积分，并新增 load 各阶段检查：

- **1,124 条 load 的 issue→execute 全部为 1 cycle**；之后仍可因真实队列和内存顺序等待。
- **958 次 resident L1D 命中，准入→data 全部为 1 cycle**。
- 1,113 条 load 在 `completion_ready` 同拍 writeback，**11 条实际多等一拍 WB**。
  所有消费者继续等实际 writeback。不能把这 11 拍移动到 cache 服务或直接附加给 CPI。

## 真实 ROI 采样首次跑通

输入为 `tmp/fst-dependencies-20260909/tealeaf-collect/tao_trace/`，沿用原 checkpoint
采集的完整 FST 与附件。manifest 同时指定边界文件中的精确 UOP 数与宏指令数，
不再用前 1,000 宏指令的人造切点。预热期间与 ROI 连续执行，不清空 cache/队列/依赖。

| 核 | 预热 UOP | ROI 用户 UOP | ROI 内核 UOP |
|---|---:|---:|---:|
| 0 | 2,090,218 | 10,000 | 0 |
| 1 | 290,195 | 10,000 | 0 |
| 2 | 229,364 | 10,000 | 1,903 |
| 3 | 287,693 | 10,000 | 0 |
| 合计 | **2,897,470** | **40,000** | **1,903** |

一次运行完成 **2,939,373 UOP**，全部声明的动态依赖被消费，包括 **83 条附件扩展边**。
独立扫描核验主记录 SHA 与此前成对采集审计相同；逐核预热/ROI 的 UOP、宏指令、用户/
内核人口和 RAM/域外访存数均与运行结果一致。全程 **233,571 个 load 分片回调**和
**49,568 个 store 分片回调**与功能输入相符，发射/完成/退休人口相等，各级 fill 与
miss generation 数守恒。完整运行关闭 CSV，独立逐事件队列面积审计仍只在固定前缀进行。

此次核验支持**真实 ROI 功能区间接入**。它不证明原型的所有服务参数正确，也不证明
gem5 的全局测量时钟窗与 FastSim 的每核最后预热退休切点完全一致。JSON 中的
`scope_metrics.cycles_per_user_uop=0.67765` 仅保留为实验诊断值，不作为准确率或修复收益。
未运行同一完整 ROI 的修复前版本，不能从此前小前缀数字推导 CPI 改善。

剩余明确差距包括：I-side/翻译仍理想化；一致性与 store disambiguation 保守；
FRFCFS/Sequencer 尚未接入；private/LLC/DRAM 服务参数尚未全部按目标对齐，例如当前
DRAM `t_cl=22`，参考为 14,160 ticks / 333 ticks-per-cycle，需要按命令时间语义映射。
不能根据当前 CPI 偏差反调这些参数。

下一步优先把每级 cache/DRAM 的目标服务参数与事件入口/出口逐项对齐，再做一次同输入
ROI 对照；保留已修复的 FU→准入→响应→WB 因果链，不重复整套前缀测试或重新采集来
替代参数/事件定义的核验。

## 产物

[`tmp/causal-load-stages-20260909/`](../tmp/causal-load-stages-20260909/)：

- `before/`、`source-hashes.json`：源文件快照与指纹。
- `build.log` / `tests.log`：一次必要构建/测试。
- `gem5-load-stage-evidence.json`：已有 reference 的阶段分布及原文件指纹。
- `prefix/`：唯一一次当前前缀运行、CSV 和独立 `validation.json`。
- `roi-10k/`：真实功能边界 manifest、一次运行、stats 与独立 `validation.json`。
- `audit_prefix.py` / `audit_roi.py`：本轮可复现的独立验证。

没有跑其他 workload、完整矩阵、sanitizer 或设备模型扩展。
