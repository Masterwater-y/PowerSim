# SPR 静态整数执行族与独立仲裁组件

## 两条目标路径与可用输入

当前目标 `X86CPU.py` 启用每核 `executionPorts`、整数执行族和共享 `sprFUPool`。`FUPool::usesIntegerResources` 按静态 `StaticInst::intExecution().family` 和已配置资源判断路径：family 非 None 且存在对应资源，进入 managed `issueInteger`；其余可执行操作使用 generic `getUnit`。managed 的 `ExecutionPorts::probe` 以 `(family,candidate_ports)` 索引 cursor，失败不移动，成功 `commit` 后移动；generic 的 `fuPerCapList` 则按 opClass 保持独立游标，失败全圈后再次读取首候选。两条路径共享物理端口，不能各自保存互不相见的占用。

已存在 `.uops.gz` 每记录静态事实：`kStaticExecutionKnown`、`integer_family`（0 None、1 ALU、2 SHFT、3 SlowInt、4 BM、5 Branch）、`candidate_ports`、`nominal_latency` 和 `zero_idiom`。本次 gem5 producer 从 `StaticInst` 的静态解码定义写入；这些值不包含实际授权端口、命中、开始/完成时间。文件有目标静态策略 SHA、记录序号和热记录 hash；读取侧已暴露 `TraceSource::current_static_uop_facts()`，当前 interval 调度尚未消费它们。已有 FLAC 四核均有 `.uops.gz`，但文件存在不证明独立 Atomic/QEMU producer 端到端等价。

`.ffe` 提供功能宏边界、字节、代码/语义身份和 ASID；`.core` 补充 decoder context 和部分静态融合资格。`.core.fusion_producer_family` 与 `.uops.integer_family` 不是同一字段。QEMU 可提供功能指令字节和执行记录，但不会自动提供 gem5 特定微操作展开及上述目标策略；要重建相同字段仍需与目标政策匹配的静态解码器、模式/上下文和微操作身份，并对异常、伪操作、ROM 和边界缺口单独资格化。不可把 native 具体 issue 或动态融合观察作为输入。

## 有限子域

`include/fastsim/spr_integer_family_input.hpp` 仅在 row 存在、execution-known 和策略匹配且字段自洽时分出 `Managed`、`Generic`、`StaticZero`；否则返回 `Unknown`。family=0 的 `candidate_ports` 按现有格式必须为 0，所以 generic 端口能力应从模型自己的目标 FUPool 配置获得。`StaticZero` 是功能识别，不是照搬某次 OoO 的实际消除；故障、move elimination、macro fusion 和未知身份须由现有模型独立决策或显式 fallback。目标 `Jmprel` 的当前模板遗漏了 intExecution 指定，实际进入 generic；不要按注释中期望的 branch family 推断。

`include/fastsim/spr_integer_family_arbiter.hpp` 是未接线的 managed 独立组件：模型提供 family、候选端口、资源/forwarding eligibility 和模型周期，`probe` 不改状态，`commit` 只对当前可用候选成功后占端口和前移对应游标；`reserve_generic` 可在同一日历占额并阻挡 managed。静态未知不能调用有效 family 的代理值；模型不能从 OoO 轨迹读取实际 grant。稀疏 `SprPortCalendar` 允许预测时间倒序访问，调用者只可按可证明的所有未来请求的下界 prune；cursor 仍按模型处理顺序推进，所以不是原生 issue 顺序等价。当前定长 cursor 约 24 KiB/核，须在可控快照/窗口粒度维护，不应逐 UOP 复制。

这里的端口日历只表示物理端口每周期一个 grant；target 的 FU unitBusy、每 family 的 issueInterval、结果转发路径和实际就绪须在 caller 的 `eligible` 中由模型提供。调用者不能因为不知道 eligibility 就传全 1；应报告 unknown/fallback。正式接线须与现有 `IntervalCoreModel` 端口日历合并成**同一份**资源状态，不能新建第二套独立占用；并保留每核跨 chunk 状态、提交/回滚、warmup 状态和 OFF 行为。静态 fusion 候选并不证明动态被融合；如尚无受支持的模型事件，则这个子域应拒绝或单独近似并计数。

## 验证

`python3 -m unittest discover -s tests -p 'test_spr_integer_family*.py'` 三项通过。其中差分测试直接提取并编译 gem5 `ExecutionPorts::probe` / `commit` 原方法，以固定种子覆盖 20,000 个模型周期、每周期五次 family/候选/eligibility 组合及两个 generic 端口占用；另覆盖失败不前移、跨候选/族独立、资源共享、快照和静态 unknown。该测试限定于调用顺序和 eligibility 已给定的局部仲裁，不验证 OoO 动态依赖、前端融合、整个时序、负载 CPI/PMU 或吞吐；组件尚未接入 Simulator。
