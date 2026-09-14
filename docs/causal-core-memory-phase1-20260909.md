# core/memory 因果事件链第一阶段实现

日期：2026-09-09。对应[实施方案](causal-core-memory-repair-plan-20260909.md)的 A 阶段。

后续实现已扩展 FP/SIMD、普通 store、分支及截断依赖处理，见
[混合指令阶段](causal-core-memory-phase2-20260909.md)。下文保留 A 阶段的历史范围和验证结果。

## 1. 已交付的范围

新增 `core.model=causal_read`，由 `Simulator::run()` 实际执行完整的普通整数 ALU/load
事件链。该模式在构造 legacy core、private preview、SharedSystem 之前选择；整次运行
不调用旧基础调度、共享 replay 或 response feedback。维护默认配置没有切换。

这是可运行的机制实现，仍是单核、只读的实验阶段。它没有完成 TeaLeaf native-FS 所需的
store、分支、取指层次、多核 coherence 和预热边界语义；本轮没有新的 TeaLeaf CPI、
P99 或生产吞吐结论。

实现入口与组件：

- `src/simulator.cpp`：选择整次运行的 owner、复用现有 FCFS `DramModel`、生成统计和
  可选事件 CSV；旧求解路径保留作回归。
- `src/causal_read.cpp` / `include/fastsim/causal_read.hpp`：有界功能预读、前端各级宽度、
  live ROB/IQ/LQ、依赖唤醒、ready 仲裁、FU/端口、writeback/retire 和分层 cache 请求。
- `src/cache.cpp` 的现有 `prepare_lookup` / `commit_probe` / `complete_fill`：复用
  cache 几何和替换实现；miss 查询与数据安装分离，没有重写一套按地址拟合的命中模型。
- `configs/causal-read-prototype.cfg`：显式的实验入口；`tests/test_causal_read.cpp`
  从实际 Simulator 入口验证，纳入 `fastsim_tests`。

## 2. 修复的事件关系

### core 就绪与发射

功能预读只提供操作类型和 producer distance，不推进目标状态。fetch/decode/rename/
dispatch 分别受配置宽度及阶段延迟约束；dispatch 分配 ROB/IQ/LQ。所有输入 producer
完成后指令才进入 ready 集合，按年龄从能够使用 FU 的指令中选择。未就绪指令不会先
占住未来 FU 时隙。每个 cycle 的预算跨同 tick 的多次事件处理保留。

load 在实际 execute 后提交 fragment；数据返回、必要的最小 load 延迟以及 writeback
带宽共同决定 producer-ready。所有 fragment 到齐才允许 writeback；消费者由这一次
writeback 唤醒。非访存 IQ 项在 issue 后释放，load IQ 项在 writeback 释放；ROB 和 LQ
在有序 retire 释放。释放点没有通过每 UOP 的延迟增量近似回填。

### cache 与服务生命周期

L1D、L2、LLC 采用串行查询，各自使用配置的 lookup latency；LLC 还经过配置的 NoC/
CHA 服务。每层 miss 在容量允许时分配带唯一 ID 的 generation，同线后续 miss 附到
同一 generation，不再分配下层服务。普通 resident hit 可以绕过已满的 miss 表。
重试重新查询当前状态，失败的准入不重复收费或预约服务。

fill callback 才安装数据，并按当时替换状态选择 victim。L2 替换会失效对应 L1 行；
若 L2 hit 的返回途中该行已被替换，load 仍可接收只读数据，但 L1 安装被丢弃，避免
重新引入已失效的包含关系。`discarded_l1_fills` 单列这一情况。

DRAM service 仅在真实 controller admission 调用一次，复用旧 FCFS bank/rank/bus
日历。控制器容量包含已经调度、数据尚未 ready 的请求；data-ready 释放该槽位，配置
中的返回流水线和 LLC fill-response 阶段随后继续。它们不与 CPU retire 混为一刻。

同 tick 已排定的 callback/release 先于新的 cache admission；所有新事件只能出现在
当前或未来。未生成的指令也由前端事件推进，不存在先提交整个旧时序批次、再发现更早
请求的接口。宿主切批不 drain 活动队列或 generation。

现有 `LineGenerationCoordinator` 继续是独立的 core-local read 原型。本实现的
generation 属于每层 cache 的 miss 合并域，不能冒充 Ruby Sequencer generation。
因此没有把它的“所有新 line 在 probe 前受同一容量限制”直接套到本路径；两者计数
也不混用。SharedSystem 的完整 coherence 分阶段接口仍属于 B 阶段。

## 3. 为什么属于通用机制修复

推理输入只有功能记录、硬件配置和自身活动状态。没有 workload 名、case ID、PC 白名单、
误差符号、gem5 timing/path 标签或 CPI 修正系数。PC 仅用于检查源提供的静态 ISA
ordering 事实；不按 PC 选择服务延迟。

测试覆盖以下独立约束：

| 类别 | 验证内容 |
|---|---|
| 依赖与乱序 | 长 producer 后的独立 ALU/load 可以先发射；消费者始终等待自己的数据/writeback；重复引用一个 producer 只唤醒一次 |
| issue/FU | 40 组随机整数 DAG、容量、latency 和 pipelining 配置，与另一套逐周期 oracle 的每条 issue/writeback 时刻相等 |
| 带宽 | fetch/decode/rename/dispatch/issue/writeback/commit 和逐 fragment load port 在所有审计场景逐周期不超发；同时完成的 8 条 ALU 受单宽 writeback/commit 限制 |
| 返回身份 | 同线 merge 引用正确 leader/generation；每个 `(source ordinal, fragment)` 恰好消费一次 data；跨行 load 等待全部 fragment |
| 可见性与重试 | callback/admission 同 tick、命中绕过满 MSHR、等待容量后重查、替换/包含关系、L2 返回中的失效 |
| 两个时间方向 | 只改变 DRAM 硬件服务参数，hit/follower 身份及后继 issue 自然重新计算；不存在旧预测时间构成的永久下界 |
| 有界状态与切批 | 1,600 条混合整数 ALU/load、两个 DRAM channel/CHA；decode batch=1 和 127 的事件 CSV 完全一致；live UOP 不超过 ROB+前端容量 |
| 身份无关性 | PC 改名及保持 cache/DRAM 几何关系的地址平移后，每条 issue 和总周期不变；关闭 CSV 审计不改变结果 |
| 统计 | ROB/IQ/LQ 及 MSHR 占用积分等于从独立事件账本重算的生命周期；退休活跃/空闲周期守恒 |
| 不支持边界 | store、branch、截断依赖、窗前未知 producer、非 RAM 地址、DTLB/coherence/FR-FCFS 等配置或输入明确失败 |

逐周期 oracle 验证内部调度合同，不是新采集的 gem5 微型差分；以上测试也不构成独立
workload CPI 泛化证明。旧 formal40/DSE54 仍是回归集合，新独立 workload/window 尚未冻结。

## 4. CLI 实际事件证据

原始文件位于 `tmp/causal-read-implementation-20260909/`，只用了新构造的功能输入。

| CLI 场景 | 实际事件 |
|---|---|
| 两个并发同线 load，随后依赖消费者和一次同线访问 | 两个 load 在 cycle 5 发射，共用 L1 generation 1；cycle 129 一起收到数据；消费者从 129 才发射；后续访问在 130 命中、134 返回 |
| 老 load 等待除法，年轻 load 独立 | 年轻 load 在 cycle 5 发射，较老 load 等除法到 45 才发射；两者各自的 L1/DRAM 请求次序随实际 issue 产生；年轻 load 的消费者等待其 cycle 129 返回 |
| store 输入 | CLI 以非零状态退出，并且不生成正式 stats.json；没有混用旧 store 时序后输出部分候选 CPI |

这些周期只说明当前配置下的事件关系，不是与 gem5 的 CPI 改善数字。

旧路径回归使用 2026-09-08 已有 8 组诊断结果为参考，原功能输入/配置不变，只写新的
输出文件。`scope_metrics`（去除 host throughput）、整个 `cores` 和 `threads` 逐项
相等：memory-clamp 257、FU-future 55、same-line 221、DTLB-future 245 cycles，
各自 generic/fast 两种模式均相等。旧反例本身仍存在于旧模式，不能把这项回归描述为
默认模型已经修好。

## 5. 配置、统计与复现

```bash
cmake --build build -- -j16
./build/fastsim_tests

./build/fastsim simulate \
  --config configs/causal-read-prototype.cfg \
  --manifest tmp/causal-read-implementation-20260909/generation/manifest.txt \
  --output tmp/causal-read-implementation-20260909/generation/replayed.json
```

需要逐事件输出时，在配置中指定 `core.causal_read_audit_path`。CSV 使用零起点的功能
源 record ordinal 和 fragment 作身份，与宿主 chunk 无关；审计写出是流式的，不在
模型内累计完整 ROI 事件。遇到不支持的后续记录会中止整次运行；已经写出的部分 CSV
只供定位失败，不能充当成功的 CPI 输出。

JSON 的 `causal_read.status=experimental-single-core-read-only` 标明模式范围；新
`dispatch_blocked_cycles_rob_iq_lq` 是 elapsed 区间，`queue_occupancy_cycles_rob_iq_lq`
是占用积分。不同阻塞原因可以重叠，不能相加作为 CPI。旧 O3 displacement 计数没有
更换定义，也不能与这些新字段直接比较。`fills_l1_l2_llc` 计完成的 miss generation；
其中被失效取消的 L1 安装由 `discarded_l1_fills` 单列。

配置前置检查发生在选择路径时；功能记录按有界预读验证。不先把整个 trace 常驻内存，
也不做未经证明的单请求或任意 batch 回退。当前拒绝功能 warmup/measurement handoff、
地址空间切换和 DVFS；cold start 的在飞状态可以跨任意宿主批次连续保持。

## 6. 本次检查结果

- 最终 `cmake --build build -- -j16` 成功，`./build/fastsim_tests` 输出
  `all FastSim tests passed`，包括新增的全部机制和带宽检查。
- 独立 Debug 构建启用 AddressSanitizer、UndefinedBehaviorSanitizer 和 LeakSanitizer，
  完整测试通过。首次沙箱内运行在测试通过后因 ptrace 限制无法完成泄漏检查；获准在
  沙箱外重跑后，退出码为 0，无 sanitizer 报错。
- 最终二进制重新验证 8 组 legacy 逐项回归、2 组新模式 CLI 场景和 store 拒绝行为。
  指纹、结果和范围见 [validation.json](../tmp/causal-read-implementation-20260909/validation.json)。
- 最终 `build/fastsim` SHA256：
  `de7f92b516c29a4e4fa468513a52961fd0ecf529f2cfb4f0807816f64371e9f0`。
- 新文件格式/链接及本轮涉及的 tracked diff 空白检查通过；未提交或推送代码。

## 7. 下一实现边界

B 阶段需要扩展浮点/SIMD 操作元数据、普通 store 的执行/commit/send/callback/SQ 回收、
load 转发与内存顺序、依赖分支恢复、I-side/翻译事件、跨核共享状态以及连续预热边界。
这些完成前，不把 `causal_read` 用作完整 TeaLeaf native-FS 候选，也不通过忽略不支持
记录获得一个 CPI。

现有 DRAM 服务仍是 FCFS 近似，没有新增 refresh 或宣称 FR-FCFS 对齐；controller
选择与 rank refresh 的进一步修复在入口事件合同稳定后单独验收。本阶段的 map/set/
事件堆用于建立可审计语义，生产吞吐成本和并行化仍待后续测量。
