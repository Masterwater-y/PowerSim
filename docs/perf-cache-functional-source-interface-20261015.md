# perf cache v2 的纯功能来源窗口接口

2026-10-15。状态：完整接口设计；输入内容与精确record范围的身份子集已在
[后续实施](perf-cache-estimates-and-l2-repair-results-20261015.md)落地。下述稳定宏
成员、功能关闭因果锚点和实际请求ROI协议仍未实现。记录身份不等于完整窗口
身份或请求计数边沿证明，`roi_edges`仍为false，不修改模型时序。

## 现有证据与缺口

- `BinaryTraceSource::functional_sync_layout()` 仅返回源核、总记录数、warmup 和
  measurement 记录数。不同内容的 FST 可以拥有相同整数布局，不能据此生成可配对身份。
- `WarmupInstructionTraceSource` 校验实际消费数量；context 格式另外校验 score/end
  是否落在完成退休的宏指令上。普通 warmup slice 不具备同等终点检查。
- `TraceFlag` 有 `kMicroOp` 和 `kLastMicroOp`，没有 first-micro、稳定动态宏 ID
  或首个宏的成员完整性标记。`incomplete_macros=0` 不能证明窗口开头完整。
- `ThreadStatePlan` 校验布局、功能锚点、任务身份和 owner 连续性；`CLOSURE` 证明的
  是 owner 尾段连续性，不能证明跨模拟器窗口身份或异步请求的计数上下文。
- `observe_perf_cache_request()` 当前使用源 sequence 的 eligibility 过滤诊断人口。
  “指令属于窗口”不等于“该请求的实际计数边沿落在窗口”。

`common-end-audit.json` 的 gem5 tick 是参考时序证据，不能传入 FastSim 控制窗口。
复制 `first-core-target-common-end-v1` 或同一个字符串也不能补齐上述证据。

## 最小功能窗口 sidecar

新增独立的 `functional-window-v1` companion，由功能生产者在冻结输入时生成。
它描述实际功能输入，不包含 cache/PMU/延迟/BPred prediction/outcome 标签或 gem5 tick。

| 字段 | 必需语义与验证 |
|---|---|
| `capture_id` | 功能采集生成的稳定 ID；不是从时序 oracle 复制的 common-cut 标签 |
| `protocol_id` | ROI 功能协议及版本，规定 begin/end 锚点与多核共同关闭规则 |
| `artifacts` | 每个 FST 和实际使用的 `.deps/.vmap/.asmap/.imap/.ifmap`、功能状态/边界计划文件的字节数与 SHA256；缺失与空文件显式区分 |
| `participants` | 源核到模型核的映射、任务集合；顺序规范化且禁止重复 |
| `per_core` | `source_core`、`source_records`、`warmup_begin`、`measurement_begin`、`score_end`、`execution_end`，全部采用原始源 ordinal 的半开区间 |
| `macro_boundaries` | 首尾动态宏 ID、first/last member 及已记录成员范围；允许显式 incomplete，禁止通过 last-micro 单向推断完整 |
| `anchors` | begin/end 的功能事件 ID、源 ordinal、PC、ASID、任务与 privilege，以及 before/after-record 语义；共享 barrier/关闭事件保留功能因果图 |
| `window_id` | 对版本、协议、capture ID、规范化 artifact 哈希、参与者及 ordinal/宏边界描述进行 canonical encoding 后求 SHA256 |

路径只用于定位 artifact，不参与可移植身份。不能只哈希 manifest 的路径字符串、
记录数或截断的头尾样本；不能接受未核验的 sidecar 哈希后直接置真。
边界协议仅写“共同结束”不足够，必须有各核可核验的功能锚点及关闭因果关系。

这是配对的**输入窗口身份**。它可以证明两端绑定了同一批功能输入与边界声明，
不能单独证明两端实际统计了同一个请求边沿人口。独立 native adapter 还需核验
自己的采集 begin/end 与这些功能锚点的绑定；不得由 FastSim 请求 native 计数或时刻。

## 两级交付，分别验收

1. **只补输入窗口身份。** 加载并核验上述 sidecar，把 `window_id` 和完整 provenance
   保存在不可变 `TraceSource` 描述中，再传到 `SimulationStats`/v2 report。
   `source_window_identity` 可在 artifact、参与者和全部功能边界绑定通过后成立。
   没有完整边沿证据时，所有请求事件的 `roi_edges` 仍为 false，其他缺失能力也不改变。
2. **完整 request ROI。** 模型根据功能 begin/end 锚点，在自己的执行时线上产生
   coordinator-owned `Before/Open/Closed` 窗口状态；observer 读取实际计数边沿时的
   状态和 task/CPL，而不是 originating instruction owner。begin-inclusive/end-exclusive、
   同边沿次序和各层实际资格判定边沿必须固定。依赖原生时序才能确定的规则继续 unavailable。

第二级必须分别处理：ROI 前接纳、ROI 内响应；ROI 内接纳、ROI 后响应；退休前已发出的
错误路径请求；无 DynInst 的取指/预取/writeback；跨任务与 CPL 切换；prepare/head-demand；
同一请求在不同层跨越边界。不能默认所有事件都在接纳时计数。
失败 computational replay 回滚窗口状态和 observer 增量；已接受目标请求随后 squash 不撤销；
窗口关闭后的 drain 只解析状态，不追加窗口计数。carry/重放不重复生成边沿。

宏退休域另外验证首尾成员完整性和真实退休边界；补齐窗口身份不解除 ROM/REP/软件预取/
mask/fault-restart 的 ISA 资格缺口，也不提供硬件认证。

## 生产者与 FastSim 的责任

QEMU、DynamoRIO 或其他功能生产者负责稳定动态宏/成员身份、记录 ordinal、
功能 ROI/barrier/任务/privilege 锚点及冻结 artifact 清单。若还要生成错误路径请求，
需另行提供足够的架构寄存器、内存和代码状态；窗口 sidecar 本身不能恢复未知地址。
允许实际分支方向/目标，禁止预测器结果、目标 cache outcome 或响应时间。

FastSim 在启动前核验内容哈希、布局、参与者与锚点；在消费过程中校验 ordinal、
宏成员和实际接受前缀；暂停/早停不得沿用“完整窗口”的成功状态。内容哈希可在冷加载或
与已有读取合并时计算，但须明确覆盖完整 artifact，不能把额外 I/O 隐藏成免费能力。
窗口状态随现有事务快照提交/回滚；report 只导出已核验 provenance 和独立能力位。

## 精确代码入口与下一步

- `include/fastsim/trace.hpp`：`TraceSource` 新增只读
  `functional_window_provenance()`；slice/warmup wrappers 原样传递源身份并显式重算/校验区间。
  `TraceManifestEntry` 增加可选 companion 定位字段，缺省保持 unavailable。
- `src/trace.cpp`：`BinaryTraceHeader` 当前无身份字段；优先以 companion 扩展，
  在 `read_trace_manifest()` / `open_trace_manifest()` 加严格加载与 artifact/源核/范围核验，
  在 `WarmupInstructionTraceSource::next()` 校验首尾宏成员和实际结束状态。
- `include/fastsim/functional_sync.hpp` / `src/functional_sync.cpp`：功能窗口锚点与现有
  `FunctionalSyncLayout`、`ThreadStatePlan` 布局和任务图交叉核验；不把 `CLOSURE` 改名为 ROI 证明。
- `src/simulator.cpp`：构造阶段核验输入 provenance；`account_perf_retirement()` 验证实际退休窗口；
  `observe_perf_cache_request()` 的第二级改造使用本端边沿状态和上下文，保留现有诊断源过滤。
- `include/fastsim/types.hpp`、`include/fastsim/perf_cache_report.hpp`：保存 verified provenance，
  按域导出身份与 ROI 能力；未通过不改写 `accepted-model-window-v1`。

先对“同数量不同内容、sidecar 被篡改、源核重映射、slice/context 边界、开头半宏、
早停/暂停”做身份拒绝测试，再做两类跨 ROI 请求、任务切换、rollback/carry/drain 测试。
只有第一组通过时可交付输入身份；第二组及真实 provider/observer 边沿验证通过后，
才逐事件启用 `roi_edges`。本调查未增加新字段、未运行新的精度/吞吐实验。
