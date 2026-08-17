# FastSim syscall 事件向量模型设计

状态：设计文档。本文记录已经实现的 per-`sysnum` 事件画像基线，以及在不改变
user-only functional trace 合约的前提下，向逐实例机器学习模型演进的方案。

相关文档：

- [`syscall-modeling-dual-cpi.md`](syscall-modeling-dual-cpi.md)：syscall、双 CPI
  和 synthetic kernel PMU 的权威合约；
- [`gem5-trace-contract.md`](gem5-trace-contract.md)：FST functional 输入合约；
- [`gem5-taotrace-cpl-syscall-patch.md`](gem5-taotrace-cpl-syscall-patch.md)：
  gem5-FS kernel-event oracle 的分类规则；
- [`accuracy-reporting-contract.md`](accuracy-reporting-contract.md)：正式准确率口径。

## 1. 结论与目标

固定 kernel build、配置和 ABI 时，Linux syscall 具有相对固定的公共入口、
`sysnum` 分派和顶层 handler，但其完整动态指令流并不固定。参数指向的对象、
fd 类型、VMA/page-cache 状态、并发、IRQ、page fault、调度、signal、seccomp、
audit 和 ptrace 都可能改变实际执行路径。

因此，user-only functional trace 到真实 CPL0 指令流不是一一映射。即使
`sysnum`、参数和返回值相同，也可能存在多个合法而不同的 kernel path：

```text
P(kernel_path | user_trace, syscall_metadata)
```

通常是多峰分布，而不是唯一结果。本文的正式目标不是“解析恢复真实内核指令”，
而是预测每个 syscall 对 FastSim 可观测量的汇总影响：active service time、PMU
事件和可选 blocked wall time。这组汇总量称为 **事件向量**。

## 2. 已实现与拟议边界

| 能力 | 状态 | 本文口径 |
|---|---|---|
| `sysnum -> mean event profile` | 已实现 | 当前确定性基线 |
| FST v7 参数、返回值和有效位 | 已实现 | 可供未来语义模型使用 |
| `mmap`/`munmap` page-fault 语义 | 已实现 | 独立于 syscall service 画像 |
| 逐实例事件向量预测 | 拟议 | 需要逐实例 oracle label |
| basic-block/path template 预测 | 可选研究项 | 只服务于 frontend 影响研究 |
| 真实 CPL0 指令和访存恢复 | 非目标 | 仅靠当前输入不可辨识 |
| 精确阻塞、唤醒和迁移 | 非目标 | 需要独立调度输入或模型 |

本文不改变 FST v7，也不允许把 gem5-FS oracle 字段写回部署输入。

## 3. 事件向量定义

对第 `i` 个动态 syscall，定义：

```text
E_i = (
  active_service_cycles,
  retired_instructions,
  retired_uops,
  branches,
  branch_misses,
  l1d_accesses,
  l1d_misses,
  l2_accesses,
  l2_misses,
  llc_accesses,
  llc_misses,
  dtlb_accesses,
  dtlb_misses,
  blocked_wall_cycles
)
```

`sysnum` 是查找键或模型输入，不是事件向量的一个事件分量。其中
`active_service_cycles` 对应当前配置字段 `service_cycles`；整组定义与
`KernelEventProfile` 和 `syscall.event_table` 的 14 个画像字段一致。

| 分量 | FastSim 语义 |
|---|---|
| `active_service_cycles` | syscall 在核上活跃执行的 service time |
| instructions/UOP/branch | synthetic syscall-kernel PMU |
| cache/TLB accesses/misses | 只增加 synthetic PMU，不代表真实访问顺序 |
| `blocked_wall_cycles` | 诊断或 makespan 分量，不进入 active CPI |

事件向量不包含 PC、basic-block 顺序、寄存器依赖或动态访存地址。它描述“总体
发生了多少”，不描述“按什么顺序发生”。

所有输出必须是非负整数，并满足至少以下守恒约束：

```text
retired_uops >= retired_instructions
branch_misses <= branches
l1d_misses <= l1d_accesses
l2_misses <= l2_accesses
llc_misses <= llc_accesses
dtlb_misses <= dtlb_accesses
```

配置加载和模型产物导入必须 fail closed；不得静默修复一个违反守恒的正式模型。
训练阶段可以投影或重参数化输出，但导出的整数画像仍须经过同一验证。

## 4. 当前确定性基线

当前实现可写为：

```text
E_hat_i = mean(E | sysnum_i)
```

即每个已知 `sysnum` 使用一个冻结的平均画像。查找顺序为：

1. `syscall.event_table` 中的 per-`sysnum` event profile；
2. 显式配置的 `syscall.event_default_profile`；
3. legacy `syscall.cost_table` 中的纯 service-cycle 值；
4. `syscall.service_latency` 标量 fallback。

当前校准器从 gem5-FS `syscall_profiles` 聚合每个 `sysnum` 的 count、active
cycles 和 PMU，然后做整数平均；它还可从 syscall class 总量导出一个显式的
class-average default profile。给定相同 trace 和配置，运行结果 bit-exact。
当前实现位置包括：

- [`include/fastsim/config.hpp`](../include/fastsim/config.hpp)：画像和查找接口；
- [`src/config.cpp`](../src/config.cpp)：配置解析与守恒检查；
- [`src/simulator.cpp`](../src/simulator.cpp)：timeline 和 PMU 注入；
- [`tools/calibrate_kernel_event_profiles.py`](../tools/calibrate_kernel_event_profiles.py)：
  oracle 校准。

这个基线能表达 syscall 类型之间的平均差异，但不能区分同一 `futex`、`read`
或 `poll` 的 fast path、active slow path 和真实阻塞实例。未知 `sysnum` 只有
在配置中显式启用并校准 default profile 时才获得 synthetic kernel PMU；否则
只允许走已有 timing fallback，不得隐式发明事件。

## 5. FastSim 运行时语义

遇到显式 syscall marker 时，FastSim 仍把它视为 serializing system op：

```text
older user work drains
  -> syscall marker/system FU
  -> active_service_cycles
  -> configured frontend restart
  -> next user instruction
```

事件向量的使用规则是：

1. `active_service_cycles` 进入 active application timeline；
2. 其余 PMU 分量进入 `synthetic_syscall_kernel`；
3. `blocked_wall_cycles` 不进入 core cycles 或 CPI；
4. synthetic cache/TLB 计数不修改 user cache、directory 或 TLB 状态；
5. page fault、IRQ、scheduler 和 idle 仍使用互斥的独立事件域，不得折入
   syscall 向量；
6. user 和 user-plus-kernel 准确率继续使用同一 trace 的 paired runs，不能从
   单次 combined run 中事后减去 syscall cycles。

这些规则使事件向量直接服务于 `CPI_user_plus_kernel` 和 PMU，同时避免伪造
不可验证的 kernel cache/coherence 因果状态。

## 6. 逐实例可学习模型

机器学习扩展写为：

```text
x_i = functional features visible at syscall boundary
E_hat_i, uncertainty_i = f_theta(x_i)
```

模型类型不是合约的一部分。GBDT、MLP、mixture-of-experts、分类器加画像表或
分位数回归都可以使用，只要它们遵守相同输入、输出、确定性和验证边界。

### 6.1 允许的输入

优先使用 FST v7 已经定义、带有效位的字段：

- ABI 和 `sysnum`；
- 最多六个 raw scalar arguments；
- raw return value、failed 和 errno；
- `maybe_blocking`；
- thread ID 和 pre/post CPU，仅在对应字段有效且调度模型明确消费时使用；
- syscall 之前、由 functional trace 可因果构造的有限上下文，例如最近 syscall
  类别、最近用户访存类别或距离上次 mapping syscall 的 record 数。

参数应先做 syscall-aware 规范化。flags/opcode 应解码为语义 bit，length/count
可做对数分桶；用户指针通常只保留 null、alignment、page/token 关系等属性，
不得把 ASLR 后的绝对地址当作 workload identity 特征。

return value 是 syscall 的 functional outcome，可以用于离线 trace replay；它
不是 kernel timing label。模型必须按 validity bit 区分“捕获到零”和“字段缺失”。

### 6.2 禁止的输入和泄漏

正式 inference 模型不得使用：

- workload 名称、benchmark ID 或按 workload 拟合的常数；
- gem5-FS kernel cycles、CPL 分类、PMU、scheduler edge 或任何 oracle 字段；
- held-out workload/core-count 的标签或汇总统计；
- syscall `post_timestamp - pre_timestamp` 作为 active service-cycle 特征。

timestamp delta 混合 active execution、blocked wait、deschedule 和采集噪声。
若未来只把它用于一个独立的 blocked-wall 模型，必须单独声明 measurement scope
并验证；在此之前 `blocked_wall_cycles` 的逐实例预测保持为零。

### 6.3 推荐的层次模型

同一 syscall 的路径通常是多峰分布，直接用单个均值回归会抹平 fast/slow path。
推荐先预测可解释的 latent path class，再预测类内事件向量：

```text
x_i
  -> P(path_class | x_i)
  -> E_hat_i(path_class), quantiles, confidence
```

例如 `futex` 可以在训练数据中形成 fast return、active contention 和可能阻塞等
类别，但类别必须由 oracle path/event 聚类定义，而不是仅凭 syscall 名称硬编码。
部署时优先使用确定性 argmax/期望值和确定性整数舍入；若研究需要采样，PRNG seed
必须由 trace identity、thread ID 和 syscall ordinal 稳定派生，不能依赖 host
调度顺序。

未知类别、未见 `sysnum`、缺失关键字段或低置信度实例必须回退到冻结的
per-`sysnum`/default profile。模型不确定性不得被隐式当成零代价。

## 7. 为什么不直接生成逐条指令

对固定 x86-64 kernel，多个 syscall 的公共入口大致共享：

```text
SYSCALL -> entry_SYSCALL_64 -> do_syscall_64
        -> per-sysnum top-level handler -> state-dependent callees
        -> exit-to-user -> SYSRET/IRET path
```

公共前后缀和顶层 dispatch 相对固定，但完整动态 call graph 并不固定。例如同一
`read(fd, buf, 4096) = 4096` 可能来自 regular file、socket 或 pipe；仅有 fd
整数不能恢复 fd table 中的对象和 callback。类似地，`futex` 是否等待取决于共享
内存值和并发，`mmap` 路径取决于 VMA 和 backing state。

因此，raw instruction seq2seq 模型只能生成“可能的”轨迹，不能恢复 ground
truth。它还会引入以下额外问题：

- 可能生成不属于真实 kernel CFG 的非法路径；
- kernel build、配置、mitigation、CPU alternatives 或 KASLR 改变代码身份；
- 没有动态 kernel address、page-table 和对象状态，无法恢复真实访存；
- 错误的访存一旦修改 cache/coherence 状态，会污染后续 user 因果路径。

如果将来确实需要研究 syscall 对 I-cache/BTB/frontend 的影响，允许增加一个
**可选 path-template 层**：

1. 固定 kernel build ID、配置、ABI、mitigation 和 CPU feature profile；
2. 把 oracle PC 规范化为 `symbol+offset` 或 basic-block ID；
3. 从真实 kernel CFG/采集轨迹建立合法模板库；
4. 模型只预测模板 ID 或合法 CFG edge sequence；
5. 从固定二进制确定性展开，并离线 lower 成 FastSim IR。

模板层仍不能自动获得 kernel data address。没有独立、验证过的地址/translation
模型时，它不得修改 data cache、TLB、directory 或 coherence 状态。正式 CPI/PMU
路径继续以事件向量为权威输出。

## 8. 训练标签与 oracle 增量

现有 `kernel-events-v2` 的 per-`sysnum` aggregate profile 足以校准均值查表，但
不足以训练逐实例模型：聚合后已经丢失参数、返回值、path class 和事件方差的
对应关系。

逐实例训练需要新增、与部署 FST 物理隔离的 oracle dataset。每行至少包含：

```text
trace/case identity              # 只用于分组、审计和 held-out 划分
kernel build/config identity
core, thread, syscall ordinal
ABI, sysnum
valid functional metadata/features
active syscall cycles
per-instance syscall PMU vector
optional normalized basic-block path/template ID
```

采集和切分必须沿用当前互斥 class stack：

- IRQ 嵌套在 syscall 内时，IRQ 区间归 IRQ，返回后才恢复 syscall；
- page fault、scheduler、idle 和 unknown_kernel 不得混入 syscall label；
- blocked/descheduled residency 不得标成 active syscall cycles；
- formal training data 要求 `unknown_kernel=0`，并通过 cycle/PMU conservation；
- functional feature row 必须能按 syscall ordinal 与 FST v7 metadata 唯一对齐。

kernel build、`.config`、ABI、mitigation、CPU alternatives 和 gem5 uarch 配置必须
写入 dataset manifest。正式模型默认绑定到一个受控 kernel profile；跨 kernel
泛化属于独立研究问题，不能由一次 held-out workload 测试替代。

## 9. 训练与验证流程

### 9.1 先判断是否需要 ML

在实现模型前，先对高频 `sysnum` 统计：

- per-instance active-cycle 和各 PMU 分量的均值、分位数、变异系数；
- normalized basic-block/path 的 unique count 和条件熵；
- 加入 args、retval/errno 后能够解释的方差；
- 每个 syscall 的 top-k path coverage 和长尾质量。

如果同一 `sysnum` 的 held-out 方差很低，或可见字段不能解释剩余方差，冻结查表
通常比 ML 更可靠。只有当逐实例可见特征在 held-out workload 上稳定降低误差，
才推进 learned model。

### 9.2 必须保留的基线

所有候选模型至少与以下基线比较：

1. global default event profile；
2. 当前 per-`sysnum` mean profile；
3. 可解释的 per-`sysnum` semantic bucket，例如成功/失败或 flags 类别；
4. learned per-instance model。

训练目标可以使用多任务损失或按事件尺度归一化的损失，但正式选择依据是 held-out
端到端指标，而不是 training loss。

### 9.3 正式 gate

模型只有同时满足以下条件才可进入 maintained profile：

- workload-held-out，且 workload ID 从未作为 inference feature；
- calibration core count 上训练，在独立 core count 上验证；
- 每个向量字段报告 WAPE、bias、P50/P90/P99 APE 和 worst case；
- 报告 `CPI_user_plus_kernel` 与 user-plus-kernel PMU 的端到端误差；
- 所有输出通过整数范围、PMU 守恒和配置 fail-closed 检查；
- 缺失字段、unseen `sysnum` 和低置信度 fallback 有独立覆盖率与误差；
- 相同 trace、模型和配置重复运行 bit-exact；
- 相比 per-`sysnum` mean baseline 在 held-out 数据上有稳定收益，且不以明显恶化
  tail error 换取平均值改善。

如启用 path template，还必须额外报告合法 CFG path 比例、template top-k coverage、
path-class calibration，以及 frontend 指标相对真实 kernel oracle 的误差。

## 10. 分阶段实施建议

1. **Variance audit**：扩展 gem5-FS oracle，采集高频 syscall 的逐实例事件行；
   先证明可见特征具有预测价值。
2. **Semantic baseline**：实现离线 `sysnum + args + retval/errno` bucket，与当前均值
   表做 workload-held-out 对比。
3. **Learned vector model**：训练带置信度/分位数的逐实例模型，导出冻结整数模型
   artifact；FastSim 保留查表 fallback。
4. **Runtime integration**：在 syscall marker 处读取
   `TraceSource::current_syscall_metadata()`，只注入 event vector，不创建 CPL0
   `TraceRecord`。
5. **Formal matrix**：复用 user/user-plus-kernel paired-run pipeline，完成独立
   workload 和 core-count gate。
6. **Optional template pilot**：只有在明确需要 I-cache/BTB/frontend 效果且事件
   向量无法表达时，再研究合法 basic-block 模板；不得提前影响正式 data-cache
   或 coherence 状态。

## 11. 按研究目标选择抽象

| 研究目标 | 推荐抽象 |
|---|---|
| `CPI_user_plus_kernel`、kernel PMU | 事件向量模型 |
| syscall fast/slow path 和尾延迟 | path class + 分位数事件向量 |
| I-cache/BTB/frontend 污染 | 固定 kernel 的 basic-block template + 事件向量 |
| kernel data-cache/TLB/coherence | 增加真实 kernel state/address 输入或使用 FS 执行 |
| 精确调度、阻塞和迁移 | 独立 scheduler/wakeup 模型或 trace |
| 真实逐条 CPL0 指令恢复 | 当前 user-only 输入下不作准确性声明 |

默认推荐是：以当前 per-`sysnum` mean profile 为强基线，优先实现逐实例事件向量；
除非目标明确要求 frontend path effect，否则不生成逐条 kernel 指令。
