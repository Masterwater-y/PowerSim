# FLAC C4：功能输入与 OoO 参考的验证记录

日期：2026-11-05。目标是 FastSim 输入只包含 Atomic/QEMU 可获得或可由功能事实和目标静态配置确定性推导的信息；gem5-spr OoO 单独提供 CPI/PMU 参考，不能把它的时间、资源占用或执行结果回填输入。旧 OoO 采集器即使只写功能信息，也不能证明独立功能采集器已经可用。

## 本次已执行

1. 对 `tmp/spr-two-stage-implementation-20261104/static-speed-c4-v2/source/c04-817.flac_s` 用 `tools/validate_fs_oracle_identity.py` 检查目标身份，结果 `valid=true`，收据 `tmp/atomic-input-validation-20261105/ooo-identity.json`。这只检查目标配置身份，不证明采集输入合法、窗口相同、PMU 真值或精度合格。
2. 运行 `fastsim_fst_core_functional_facts_tests`、`fastsim_mmu_controls_tests`、`fastsim_fst_instruction_translations_tests`，均通过。这些是格式、读写和绑定测试，不是 Atomic/QEMU 采集验证。
3. 使用当前 `build/fastsim`（SHA-256 `4eac6b139e88ef45a5a6cc890539c975793eef219d9312515777460aeb19e625`）、原 FLAC C4 manifest、`user-plus-kernel`、四核，加载 `tmp/atomic-input-validation-20261105/flac-latest.cfg`。该配置继承现有目标参数，并选中已接入的因果退休前端和指令侧适配器。程序退出码 1，在配置验证阶段拒绝，未回放任何指令；日志 `tmp/atomic-input-validation-20261105/flac-latest.log`。因此没有本次新路径 CPI、PMU 或吞吐数据。

复现命令（需保留本工作区的既有临时输入和配置）：

```bash
env LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim simulate \
  --config tmp/atomic-input-validation-20261105/flac-latest.cfg \
  --manifest tmp/spr-two-stage-implementation-20261104/static-speed-c4-v2/source/c04-817.flac_s/tao_trace/manifest.txt \
  --measurement-scope user-plus-kernel --cores 4 \
  --output tmp/atomic-input-validation-20261105/flac-latest.json
```

## 限制与下一步

当前配置的 DTLB 已启用，而 `src/config.cpp` 的因果执行检查仍无条件拒绝 `dtlb.enabled`；必须接入真实的因果 DTLB 行为，不能为了通过检查关闭目标 DTLB。此项是已确认阻塞，并不排除其他配置或运行时阻塞。此前完整输入清点还发现 branch、kernel、serializing、特殊地址与请求角色等人口；这些能否进入当前因果路径需要逐项完成对应适配，不能仅移除保护。

本地旧 FLAC FST 由 OoO TaoTrace 采集，缺少当前新增的 core functional、MMU-control 与 architectural bundle 等伴随；不能从另一轮执行拼接伴随。现有功能字段测试通过不构成新生产者证明。AtomicSimpleCPU 的提交观察点存在，但当前 TaoTrace 使用 O3 `DynInst`，不能直接挂到该 CPU；本机 QEMU 版本为 3.1.0，仓库内旧双 producer 设计的完成声明与后续项目报告相冲突，目前未找到可直接运行并满足新合同的 QEMU 生产者。应以实际代码和同轮输入证明为准。

### 已发现：FST 内嵌了 OoO syscall 时间戳

直接读取四个 FST header 指向的尾部 syscall 元数据：分别有 47、4、10、7 行，共 68 行，均有有效的 pre 时间戳，其中 67 行也有有效的 post 时间戳。冻结采集源码中 `emitFunctionalSyscallMarker` 和返回处理用 `curTick()` 生成这些值，`finalizeFst()` 将其写入 FST。因此不能声明当前 FST 文件只包含功能级信息。源码检索未发现 FastSim 的计时路径使用这两个字段：trace 读取/保留它们，旧路径可向 syscall 语义处理器传递整份元数据，但字段名不在该处理器或其他模型中引用；当前因果入口亦未调用该元数据接口。这是源码范围内的检查，不是运行时消融证明，不应推论所有伴随字段均已符合合同。

热记录写入的 PC、地址、实际分支去向和寄存器 RAW 距离等在语义上可由功能执行及解码推导；静态 `.uops.gz` 记录候选端口/标称延迟是固定目标策略信息，不是实际端口授权或实际延迟。其 SA/SD 逻辑配对目前从 O3 的 SQ 项关联得到，是否可从纯功能信息等价重建尚未证明。采集的多核共同结束人口也取决于该次 OoO 执行的截止时刻，不能假定 Atomic 执行将自然产生相同每核尾部。现有 `.ffe`、`.sreq`、`.xlat`、同步计划及全部实际消费者尚需完整源级和差分资格化，禁止根据本次局部检查给整个 FST 标记 Atomic 合格。

### 时间戳隔离与进一步字段核查

原始四核 FST 未修改。`tools/strip_fst_syscall_timestamps.py` 为每个 FST 创建新文件，验证 v7 结构，清零 syscall 元数据的 pre/post 时间值和有效位；目标已存在或输入异常时拒绝覆盖。清理结果位于 `tmp/atomic-input-validation-20261105/sanitized-flac/`，含 manifest、thread-state plan 和四个独立 FST。伴随文件通过绝对符号链接指向原始采集，移动副本或删除原始文件会使链接失效；本目录不包含参考 oracle。`timestamp-isolation-verified.json` 记录每份输入输出 SHA-256 与验证范围。

真实四核数据逐字节比较确认：完整 header 和所有热记录相同，全部 syscall 行除了两个时间值及其有效位外相同；清理后 68 行的时间有效位均为零。针对性单元测试检查原文件不变、其他字节不变、格式异常和目标已存在的失败行为。这个处理仅隔离已发现的 syscall 时间，不会自动净化所有伴随。

已有全流清点器读取清理后的四核 manifest 成功，其 `inventory.json` 与原始输入清点结果逐项相同，覆盖原有 38,554,477 条记录及该读取器访问的功能伴随；这不是完整回放或 CPI/PMU 验证。清点器输出在清理目录下。

| 内容 | 当前源码与来源核查 | 仍需证明 |
|---|---|---|
| 热流 PC、load/store 地址、分支实际去向、寄存器 RAW | 提交时输出，并按静态寄存器及已退休写者形成依赖；无 fetch/issue/complete/commit 时间字段 | 独立功能采集对异常边界、线程迁移和依赖别名的逐项一致性 |
| `.ffe` | 保存实际退休宏范围、字节、上下文哈希及静态 decoder 属性；DSB eligible 是静态资格，不是 native DSB 命中 | 不同 producer 的 decoder 上下文、macro 展开和 shared-allocation 静态资格一致 |
| `.uops.gz` | 输出静态 `StaticInst::intExecution()` 候选端口/标称延迟和操作数；store 配对借助 O3 `sqIt` | 绑定目标 decoder 策略的独立派生；SA/SD 在故障、截断下的等价配对，不把模型资源结果带进输入 |
| `.sreq` / `.mreq` | 请求标志、mask、地址和类型由 O3 已保存请求、翻译及静态指令生成；不输出完成时刻 | requestor/flags 编码和拆分请求事实能否在功能执行中获得，特殊地址、HTM、mask 等逐类验证 |
| `.xlat.gz` / `.vmap` / `.asmap` | 页映射和 PTE 快照属于 guest 功能状态；`.xlat` 当前通过 O3 请求上携带的翻译事实获得，TLB 条目可缺完整路径 | 功能生产者覆盖虚实映射、页大小、权限、异常和变更；当前数据缺新 MMU 控制伴随，不能默认完整 |
| `.vmap` ROI 边界 | 格式含一个“测量开始时先前 page fault 仍未返回用户态”标志，属于采集边界状态；本 FLAC 四核实际置位均为 0，测量 PTE 状态有效行分别为 22/0/0/0 | 要在独立运行的对应功能边界重新采集，不能照搬 OoO 多核共同时间点的状态 |
| `thread-state.plan` | 编译器拒绝 timing/oracle 字段，计划包含功能事件、依赖和源序号；模型根据自身退休时刻推进 | OS 钩子与跨核因果边在 Atomic/QEMU 可重建；执行线程/工作量和共同截止人口的跨运行可比性 |

这些是源码和现有文件的有界核查，不是 Atomic/QEMU 实际生产者验证，也不是对所有字段的证明。下一步需对未证明字段做可复现功能生产和差分，并接通当前因果路径后才能进行同窗口 CPI/PMU 验证。

### SA/SD 与 Atomic 请求、翻译可行性验证

使用清理后的 FLAC C4 全量 manifest，`tmp/atomic-input-validation-20261105/check_store_pairs.cpp` 通过生产 `BinaryTraceSource` 顺序读取完整流，仅依据每个退休宏的 `.ffe` span 和 `.uops.gz` 中 SA/SD 的角色，独立按同宏配对，再把结果与当前源的 SQ 生成 group ID 比较。core0/1/2/3 分别得到 141,708 / 285,572 / 256,121 / 297,911 对，共 **981,312 对**；不匹配、部分配对、未确定各为 0，日志 `tmp/atomic-input-validation-20261105/store-pair-differential.txt`。该实验使用了 OoO 采集的宏边界与角色作为输入、SQ 配对作为对照，故证明的是该 FLAC 数据集上的可重建性，不是 Atomic producer 端到端差分，也不能推广到未覆盖的异常或截断。

已将新功能配对器本身接入离线比对器 `tools/audit_spr_functional_store_pair.cpp`，再次对同一完整人口运行，结果仍为 981,312 对、无不匹配或未闭合（`tmp/atomic-input-validation-20261105/store-pair-final-audit.txt`）。旧 FFE 不包含 OS 线程 ID，离线对照使用每核固定的上下文占位值，因此不证明跨线程身份；真实 producer 另使用退休上下文及 ASID。采集器对象已编译，但完整可执行文件和新采集尚未验证。

源码进一步说明该结果的机制基础：目标 ISA 的 `MOV_M_R` 静态展开为 `sta` + `storedata`；SPR frontend 对其要求一个地址和一个数据成员；`ExecContext::initiateStoreAddress` 与 `writeStoreData` 已为顺序执行上下文实现暂存地址并在 SD 实际执行写入。O3 的 `LSQUnit::insertStoreData` 则将 SD 关联到同一 macro/PC 中未配对的 SA。对于具备完整宏和静态展开的受支持指令，配对可按功能事实和已固定的 decoder 策略构造，不必输出 SQ 条目或其生命周期。特殊宏、异常、部分退休和上下文变化须继续显式判定，不能用 PC 相同一条规则跨动态实例猜配。

实际运行了一个 Atomic SE 小样例：使用 FLAC 同一采集二进制（SHA-256 `9fcabaacfdf0377bc189199d48bcfaed79214d1bc22317ee03c251f8d49ecdac`），静态 x86 程序在地址 `0x402000` 写入 `0x1122334455667788`、读回并比较。`Exec,TLB` 日志中同一 `0x401011` 宏依次出现 `X86StoreAddress`、`X86StoreData`，SD 触发 VA `0x402000` → PA `0x2000`，随后的 load 读回预期值。源码、独立配置、日志和可执行见 `tmp/atomic-input-validation-20261105/atomic_store_probe.S`、`atomic_se_probe.py` 与 `atomic-se-tlb/exec.trace`。调试日志中的周期只用于检查该实验，不作为 FastSim 输入。SE 地址翻译不是 FS 客体页表实验，不能据此宣布 PTE 路径或 FST companions 已生成。

对访存及翻译，`AtomicSimpleCPU::readMem/writeMem` 已有每次访问的 VA、大小、flags、byte enable，按 cache line 切分并逐片调用 `translateAtomic`；成功时 Request 上有 PA，`NO_ACCESS`、本地访问、条件写等分别有现存分支。x86 `TLB::translateAtomic` 进入共享 `translate`，FullSystem 成功映射时构造 `FunctionalTranslationExtension`，携带 VA/PA、叶映射、页大小/root/PCID 及可用路径；walker 的非 timing 路径同步执行。另一方面 Atomic 每次重用 Request，目前没有类似 O3 的退休绑定、聚合并写 `.sreq/.xlat` 的采集器，故须在每个分片复用前复制快照，在完整指令成功或精确异常边界绑定输出；不能在退休后读取被重用的 Request。SA 的功能 API 先暂存未翻译地址、真正写及翻译发生在 SD，而 O3 请求可挂在 SA 上，适配器须用宏内配对将两者语义归一。

当前结论：基础功能事实在源码与简单 Atomic SE 执行中有正面证据；**完整 FST 等价仍未通过**。特别是分片/跨页非连续 PA、掩码、NO_ACCESS、prefetch、LL/SC/条件写、设备/不可缓存、Fault 与成功片段、TLB 命中但路径未知、OS 上下文/控制状态变化，均需 Atomic FS 样例和与同一功能人口的 OoO 输出逐字段差分。Ruby 只能使用 `atomic_noncaching`，需要单独配置，不可仅把现有 OoO CPU 类替换为 Atomic；该实验没有改动 gem5 或生成生产 FST。

即使取得功能采集，严格误差计算还要求核实相同二进制和输入、执行路径、参与核/线程、预热和计分窗口及指令人口。已有 OoO 共同起点与当前 FastSim 逐核预热退休起点并非自动等价；须建立对应的独立 OoO 参考。PMU 参考须使用维护的 Ruby/SLICC 真值验证流程。若窗口或人口不能证明可比，严格 CPI/PMU 误差保持不可评分。
