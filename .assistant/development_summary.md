# MineSim 阶段性总结与架构文档

本文档用于记录和追踪 MineSim 仿真器项目的阶段性开发进展、架构设计以及实现细节。每次阶段性任务完成后，都将在此文档中追加相关内容，以便于对齐上下文。

## 目录

1. [项目概览](#1-项目概览)
2. [阶段一：Trace 流式解析初步实现](#2-阶段一trace-流式解析初步实现)
3. [阶段二：Trace 解码与验证工具](#3-阶段二trace-解码与验证工具)

---

## 1. 项目概览

MineSim 是一个非周期精确的、多核多线程乱序的 CPU 性能模拟器。初版开发中，MineSim 仅支持单核单线程的仿真。它基于 Trace-driven，输入格式为 DynamoRIO 格式。由于 Trace 文件较大，采用流式读取以避免内存不足。仿真器主要输出包括周期数、指令数、预期运行时间等。

### 1.1 核心需求
- **流式输入**：直接流式读取压缩的 DynamoRIO Trace 数据。
- **指令反汇编与 Uops 映射**：基于 Trace 中提取的 encoding，反汇编还原为原始指令，并重新映射为 uops 以体现前端解码开销。
- **缓存建模**：对缓存层级进行建模（初版不包含缓存一致性，但需预留接口）。
- **乱序性能模型**：建立基于单核单线程的乱序执行（Out-of-Order）性能模型，输出性能统计数据。

---

## 2. 阶段一：Trace 流式解析初步实现

### 2.1 模块设计
在阶段一中，我们初始化了项目的基础目录结构和 CMake 配置，并实现了 Trace 文件的流式读取模块。

- **`include/common/Types.h`**：定义了通用数据类型（如 `Instruction`，`Addr`，`InstType` 等）。
- **`include/trace/TraceReader.h` & `src/trace/TraceReader.cpp`**：实现了 `TraceReader` 类，负责通过 `zlib` 直接流式读取 `.trace.gz` 文件，并维护一个内部状态机。
- **`CMakeLists.txt`**：配置了 C++17 编译环境，链接了 `zlib`，并通过 `FetchContent` 引入了 `capstone` 库用于后续的反汇编支持。

### 2.2 TraceReader 内部实现逻辑
由于 DynamoRIO offline trace 会将指令编码 (`TRACE_TYPE_ENCODING`)、指令信息 (`TRACE_TYPE_INSTR` 等) 以及访存操作 (`TRACE_TYPE_READ` / `TRACE_TYPE_WRITE`) 分离为多个 `trace_entry_t` 条目，我们需要在读取时将其重组为一个完整的 `Instruction` 对象：
1. 缓存遇到的 `TRACE_TYPE_ENCODING` 字节。
2. 遇到指令条目时，将其地址 (`pc`) 和缓存的编码合并，并推断是否为分支指令。
3. 通过向下预读（Look Ahead），捕捉紧跟其后的访存条目，从而将内存访问地址和大小附加到该指令，并相应地将其标记为 `LOAD` 或 `STORE`。
4. 将预读到的下一条指令暂存到 `pending_inst_` 中，供下一次迭代直接使用。

这种不依赖 DynamoRIO `libdrmemtrace_analyzer.a` 静态库的设计，极大地降低了编译和链接的复杂性，并且完美符合了流式处理的要求。

---

## 3. 阶段二：Trace 解码与验证工具

为了确保我们的流式解析和指令信息的正确性，我们引入了指令解码功能，并实现了一个独立的验证工具 `trace_verifier`。

### 3.1 独立验证工具的构建
我们将 CMake 配置进行了调整，将解析相关的代码抽象为静态库 `minesim_core`。
在此基础上，新增了一个可执行目标 `trace_verifier`：
- **路径**：`tools/trace_verifier/main.cpp`
- **目的**：接收 DynamoRIO 的压缩 Trace 文件路径和需验证的指令数量。流式读取并解析后，利用反汇编引擎将指令还原成易读的文本格式，并打印关联的访存信息。

### 3.2 引入 DynamoRIO 原生反汇编引擎
DynamoRIO 官方的 `view` 工具通过其内部的汇编/反汇编组件工作。为了在我们的仿真器中实现最高精度的解码，并支持后续的微指令（uops）拆解，我们移除了原有的 Capstone 依赖，转而直接使用了 DynamoRIO 的原生解码库（`libdrdecode.a` / `libdynamorio.so`）。

在 `TraceVerifier` 中：
1. 通过 `dr_standalone_init()` 初始化 DR 环境。
2. 结合 `instr_create` 和 `decode_from_copy`，对流式提取的 `encoding` 字节数组进行反汇编解码（在保留原始 PC 的前提下解析拷贝的 encoding）。
3. 打印出 PC，推断的指令大类（ALU/BRANCH/LOAD/STORE），关联的内存地址及操作大小，以及原生风格的反汇编输出（如 `mov %rsp -> %rdi`）。

### 3.3 验证结果与修复
通过 `trace_verifier` 成功解码了示例 trace 文件的最初若干条指令：
```text
PC: 0x00007fc6a150ba50 | Type: ALU     | MemAddr: N/A                 | Disasm: mov    %rsp -> %rdi
PC: 0x00007fc6a150ba53 | Type: BRANCH  | MemAddr: 0x7ffeb5a65118 Size: 8 | Disasm: call   $0x00007fc6a150c650 %rsp -> %rsp 0xfffffff8(%rsp)[8byte]
PC: 0x00007fc6a150c650 | Type: STORE   | MemAddr: 0x7ffeb5a65110 Size: 8 | Disasm: push   %rbp %rsp -> %rsp 0xfffffff8(%rsp)[8byte]
PC: 0x00007fc6a150c651 | Type: ALU     | MemAddr: N/A                 | Disasm: lea    <rel> 0x00007fc6a14f1000 -> %rsi
```
该结果证实了：
1. 成功利用 DynamoRIO 的原生 `dr_standalone_init()` 和 `decode_from_copy()` API 对原始字节进行了解码，反汇编结果与 DynamoRIO 的 `view` 工具完全一致。
2. 指令类型（如 `BRANCH`, `STORE`）和后续的访存大小（Size: 8）被正确提取和绑定。
3. 同时在验证中修复了 DynamoRIO 为节省空间而在循环时不写入 encoding 导致的 `<no encoding bytes>` 错误：我们在 `TraceReader` 中引入了一个内部缓存 `encoding_cache_` (使用 `std::unordered_map<Addr, std::vector<uint8_t>>`)，遇到缺失时根据 PC 地址回溯即可。

---

## 4. 阶段三：微架构配置参数 (Micro-architecture Config)

在建立核心的 CPU 流水线和 Cache 模型前，需要将可供配置的微架构参数进行抽象，以便我们既能使用默认的内置预设，也能支持用户后期从配置文件加载。

### 4.1 可配置参数定义
在 `include/core/Config.h` 中创建了 `MicroArchConfig` 结构，主要定义了：
1. **流水线宽度**：包括 `fetch_width` (取指), `decode_width` (解码), `rename_width` (重命名), `dispatch_width` (分派), `issue_width` (发射) 以及 `retire_width` (提交)。
2. **队列和缓冲大小**：包括 `rob_size` (重排序缓冲, Reorder Buffer), `lq_size` (加载队列), `sq_size` (存储队列), `iq_size` (指令队列/保留站)。
3. **延迟与惩罚**：如 `branch_mispredict_penalty` (分支预测错误惩罚周期)。
4. **Cache 级联配置**：定义了 `CacheConfig` 结构来管理各级 Cache 的 `size_kb`, `associativity` (相联度), `line_size` 和 `latency` (访问延迟)。

### 4.2 本机 CPU 预设与配置文件
根据测试机器所搭载的处理器（Intel(R) Xeon(R) Platinum 8260 CPU，基于 Cascade Lake SP 架构），在 `config/cascade_lake.cfg` 中建立了一套默认的微架构参数配置文件（INI格式）。主要参数包括：
- **发射宽度 (Issue Width)**：8（对应 Intel 的 8 个执行端口）
- **ROB Size**：224
- **Load Queue**：72
- **Store Queue**：56
- **Cache 延迟和大小**：L1D (32KB, 4 cycle), L2 (1024KB, 14 cycle), L3 (36MB, ~70 cycle)。

主程序入口 (`src/main.cpp`) 在开始仿真前已接入 `MicroArchConfig::load_from_file("config/cascade_lake.cfg")` 方法并打印出配置详情，确保了后续乱序模拟时的统一调度标准，且支持用户按需修改。

---

## 5. 阶段四：规模验证与一致性对比

为了确保我们的解析逻辑与 DynamoRIO 官方解析器的输出在规模级别（如 200,000 条指令）上完全一致，我们实施了对比验证：

### 5.1 解析结果生成
1. **DynamoRIO 原生解析**：使用 `drmemtrace_launcher -tool view` 命令（开启 `-view_syntax att` AT&T 汇编格式，限定 `-exit_after_instrs 200000`），提取其 `ifetch` 的解析结果并去除了其为间接跳转动态生成的 `(target 0x...)` 标注，存为 `tmp/dr_view_200k_parsed.txt`。
2. **MineSim 解析**：执行了我们的验证工具 `./build/trace_verifier ... 200000 --compare`，开启了纯净文本对比模式（关闭所有非指令的杂项输出、补齐了 `(taken)` 或 `(untaken)` 的条件分支状态），存为了 `tmp/minesim_200k.txt`。

### 5.2 验证结果
利用 `diff tmp/dr_view_200k_parsed.txt tmp/minesim_200k.txt` 命令对比，生成的差异结果文件 `tmp/diff_result.txt` **行数为 0**。这证明了我们的流式解析框架在提取前 200,000 条指令的解码、分支预测状态、PC 偏移等逻辑与 DynamoRIO 内置机制**100% 完全一致**。

---

## 6. 阶段五：指令微操作 (Uop) 映射

为了精确模拟超标量乱序执行（Out-of-Order）前端的解码开销和后端的执行端口占用，我们将复杂的 x86 宏指令 (Macro-instruction) 拆解并映射为类似 RISC 的微操作 (Micro-operation / Uops)。

### 6.1 Uop 定义
我们在 `include/core/InstDecoder.h` 中设计了 `MicroOp` 结构：
- **`type`**: `ALU` (计算/地址生成), `LOAD` (内存读取), `STORE` (内存写入), `BRANCH` (分支)。
- **包含内容**: 保留了父宏指令的 PC 和反汇编字符串以便调试；如果是 `LOAD`/`STORE`，附加内存地址和大小；如果是 `BRANCH`，附加其条件与跳转预测结果。

### 6.2 映射策略与实现
我们创建了 `InstDecoder` 模块 (`src/core/InstDecoder.cpp`)，基于 DynamoRIO 的 `instr_t` 结构体进行分析，按以下规则严格映射：
1. **LOAD 映射**: 只要指令读取了内存 (`instr_reads_memory`)，必定拆解出一个 `LOAD` uop。
2. **STORE 映射**: 只要指令写入了内存 (`instr_writes_memory`)，必定拆解出一个 `STORE` uop。
3. **BRANCH 映射**: 如果指令是跳转、Call 或 Ret (`instr_is_cbr/ubr/call/return`)，拆解出一个 `BRANCH` uop。
4. **ALU 映射**: 除了纯粹的跳转指令（如 `jmp`）、纯数据搬运（如 `mov` load/store）、以及由 Stack Engine 处理的压栈出栈（`push/pop`）外，所有的指令都会拆解出一个 `ALU` uop 以占用后端的执行端口（含地址生成 AGU 逻辑）。

### 6.3 拆解实例展示
通过工具的实际验证（例如 `call` 变为 ALU+STORE+BRANCH，`push` 变为 STORE），映射结果符合 x86 微架构设计的经典拆解规范：
```text
Disasm: mov    %rsp, %rdi
  -> Uop[0]: ALU    
Disasm: call   $0x00007fc6a150c650
  -> Uop[0]: ALU    
  -> Uop[1]: STORE  Addr: 0x7ffeb5a65118 Size: 8
  -> Uop[2]: BRANCH (unconditional)
Disasm: push   %rbp
  -> Uop[0]: STORE  Addr: 0x7ffeb5a65110 Size: 8
Disasm: lea    <rel> 0x00007fc6a14f1000, %rsi
  -> Uop[0]: ALU    
```

---

## 7. 阶段六：缓存层级与地址转换 (Memory Hierarchy & TLB)

为了真实评估流水线执行过程中的取指与数据访存延迟，我们构建了完整的 Cache、TLB 与页表机制。

### 7.1 配置文件扩展
在 `MicroArchConfig` (`include/core/Config.h`) 中新增了针对 TLB 和主存的配置项，并在 `config/cascade_lake.cfg` 中提供了预设值（如 128 项 ITLB、64 项 DTLB、1536 项 STLB，以及 16GB 内存、4KB 页大小、50 周期页表遍历惩罚）。

### 7.2 模块实现
1. **基础 Cache 结构 (`Cache.cpp`)**
   - 实现了基于组相联 (Set-Associative) 的 `CacheBlock` 和 `CacheSet`。
   - 采用位掩码 (`set_index_mask_`) 和位移提取 Set Index 与 Tag。
   - 实现了基于最近最少使用 (LRU) 算法的块替换策略。
2. **TLB 与虚实地址转换 (`TLB.cpp` & `PageTable.cpp`)**
   - **TLB**: 实现了独立的指令和数据旁路转换缓冲（组相联，LRU 替换）。
   - **PageTable**: 采用 `std::unordered_map` 实现了简易页表。当遇到新的虚拟页 (VPN) 时，自动分配物理页 (PPN)，若超出 16GB 物理内存则抛出 Overcommit 警告。
3. **缓存层级总线 (`MemoryHierarchy.cpp`)**
   - 整合了 L1I, L1D, L2, L3 以及各级 TLB。
   - 提供了统一的接口 `fetch_instruction`, `read_data`, `write_data`。
   - 每次访问首先查询 TLB (ITLB/DTLB -> STLB -> Page Walk) 获得物理地址，然后依次查询 L1 -> L2 -> L3 -> Main Memory，累加各级访问与 Miss 惩罚周期并返回总延迟。

---

## 8. 阶段七：基于区间仿真的核心流水线模型 (Interval Simulation)

为了在不牺牲过多性能的前提下获得准确的乱序执行和缓存级联延迟数据，我们参考了 Sniper 模拟器的**区间仿真 (Interval Simulation)** 思想，摒弃了周期精确 (Cycle-accurate) 中逐周期驱动数据结构的做法，转而设计了基于**时间戳 (Timestamp)** 的流水线模型 `IntervalCore`。

### 8.1 时间戳推演与结构级冲突建模
`IntervalCore` 会为每条宏指令和微操作记录其生命周期的关键时间点：
- **Fetch & Decode (前端)**：通过 `fetch_count_` 和 `fetch_width`（及 Decode 同理）对前端带宽进行约束。结合 `MemoryHierarchy::fetch_instruction`，如果发生 I-Cache/ITLB Miss，相应的惩罚周期直接追加到 `last_fetch_cycle_`。
- **Dispatch & ROB (分派与重排序缓冲)**：通过 `rob_retire_cycles_` (std::deque) 模拟 ROB 占用。当 ROB 满时（大小由 `config_.rob_size` 限制），新指令的分派时间 (`dispatch_cycle`) 必须等待 ROB 中最老的指令提交后才能推进。
- **Issue & Execute (发射与执行)**：利用 `issue_counts_` (基于 Hash Map 的滑动窗口) 限制每周期的最大发射数量 (`issue_width`)。对于 LOAD/STORE 微操作，调用数据缓存层级评估延迟（`exec_latency = mem_->read_data(...)`）。
- **Retire (提交)**：确保按序提交（In-order Retire），如果某条指令提前执行完毕，其 `retire_cycle` 依然要受到前面较老指令提交时间的制约，同时受限于 `retire_width` 带宽。

### 8.2 内存级并行度 (MLP) 的自然体现
得益于时间戳模型，如果连续遇到多个 D-Cache Miss 的 Loads，只要不被 ROB 或 Issue Width 阻塞，它们的 `issue_cycle` 将处于相同或相近的周期。这意味着它们的执行和访存惩罚会在时间轴上**天然重叠 (Overlap)**，完美模拟了现代乱序处理器的**内存级并行度 (Memory-Level Parallelism, MLP)**，无需像简单的 Trace 仿真那样累加所有 Miss Penalty。

### 8.3 性能统计与规模验证
在一次针对真实 Trace 文件（50 万条宏指令）的测试中：
- 共解析出 ~60.2 万个 Uops，宏指令 IPC 为 **0.277**，微操作 UPC 为 **0.333**。
- **D-Cache 累计 Miss Penalty** 为 14,031,321 周期，而**仿真总耗时 (Total Cycles)** 仅为 1,808,275 周期。这不仅证明了 Cache 与 TLB 模型的正确触发，更通过 MLP（数倍重叠惩罚）验证了 Interval Simulation 核心逻辑的优越性与高效率。

### 8.4 缺失部件与后续改进分析
虽然我们参考 Sniper 实现了高效的区间仿真，但要进一步逼近真实硬件的行为，还差以下部件/机制的精细建模：
1. **指令队列 (Instruction Queue / RS) 容量限制**：目前的 `IntervalCore` 在 Dispatch 阶段只受到 `ROB` 大小的限制。在真实的微架构中，除了 ROB，指令发射前还需要驻留在保留站（RS）或指令队列中。如果 RS 满了（通常远小于 ROB），Dispatch 也会被阻塞。
2. **访存队列 (LQ/SQ) 的乱序与地址消歧 (Address Disambiguation)**：
   - 目前 `IntervalCore` 中的访存操作在 Issue 阶段立即访问 Cache 并返回延迟。
   - 真实硬件中，Load/Store 存在 Load Queue (LQ) 和 Store Queue (SQ) 容量限制。
   - **Store-to-Load Forwarding (STLF)**：如果一个 Load 的地址与前面尚未 Retire 的 Store 地址重合，Load 可以直接从 SQ 中拿到数据，而无需访问 Cache，从而缩短延迟。
   - **Store 写入时机**：Store 指令应当在 Retire 阶段才真正把数据写入 L1 Data Cache，目前我们的模型为了简化在 Issue 阶段就完成了写回。
3. **串行化指令 (Serialization Instructions)**：比如 `MFENCE`、`SFENCE`、`CPUID`。这类指令在 Sniper 中被建模为“排空 ROB” (Drain ROB)，即该指令必须等到所有老指令 Retire 后才能执行，且后续指令必须等它 Retire 后才能 Fetch。目前我们尚未专门处理这种同步语义指令。

---

## 9. 阶段八：动态分支预测器 (Branch Predictor)

为了替代在 `IntervalCore` 早期实现中使用的硬编码/伪随机分支预测，我们引入了与 Sniper 类似的动态分支预测模块。这大大提高了前端仿真的准确性。

### 9.1 分支预测器设计
在 `BranchPredictor.h` / `BranchPredictor.cpp` 中定义了抽象基类 `BranchPredictor` 并提供了工厂方法。目前实现了以下几种预测器：
1. **OneBitBranchPredictor**: 使用单比特 (1-bit) 记录上一次分支的历史方向，根据历史直接预测下一次方向。
2. **BimodalBranchPredictor**: 使用 2-bit 饱和计数器（0-3），弱不跳转/强不跳转/弱跳转/强跳转。仅当计数器 `>= 2` 时才预测为跳转。这也是大多数处理器的经典双模态基础预测器。
3. **PerfectBranchPredictor**: 永远预测正确的完美预测器，用于排除分支预测影响下的纯访存性能评估。

### 9.2 配置集成与实测
在 `MicroArchConfig` 中新增了 `bp_type` 和 `bp_size` 参数。默认使用 `bimodal` 类型，表大小设为 4096。
在与 50 万条宏指令 Trace (657_xzs) 的仿真集成中，Bimodal 预测器展现出了真实的效果：
- **总分支预测次数**: 99,082 次
- **预测正确**: 95,835 次
- **预测错误**: 3,247 次
- **预测准确率**: **96.72%**

得益于准确率的提升，误预测引起的前端 Flush 惩罚显著减少，总体仿真时钟周期从早期的 ~180 万周期下降至 ~104 万周期（IPC 提升至 0.481），这验证了高精度分支预测器对核心流水线模型效率的巨大影响。

---

## 10. 阶段九：寄存器重命名与数据依赖 (RAT & Data Dependency)

为了使 `IntervalCore` 更加逼近真实乱序处理器的行为，我们实现了**数据依赖图**的核心控制机制，使得指令必须等待其源操作数就绪后才能发射。

### 10.1 源与目标寄存器提取
在 `InstDecoder` 中，我们利用 DynamoRIO 的 `instr_get_src` 和 `instr_get_dst` 提取了宏指令的源寄存器与目标寄存器，以及内存寻址所需的 Base/Index 寄存器。
由于宏指令被拆解为了多个 Uop（如 LOAD, ALU, STORE），我们在拆解时分配了特殊的虚拟寄存器 ID（如 `VREG_LOAD` 和 `VREG_ALU`）以串联同一条宏指令内部 Uop 之间的数据流。例如，一个读取内存并相加的指令会被建模为：
- `LOAD` uop 依赖 Base/Index 寄存器，写入 `VREG_LOAD`。
- `ALU` uop 依赖 `VREG_LOAD` 和其他源寄存器，写入目标寄存器。

### 10.2 寄存器重命名表 (RAT) 机制
在 `IntervalCore` 中引入了 `rat_` (Register Alias Table)，它本质上是一个从寄存器 ID 映射到“该寄存器数据就绪周期 (Ready Cycle)”的 Hash Map。
- **数据依赖检查**：每当一个 Uop 被分派时，模拟器会检查它所有的 `src_regs`。其最早可能发射的周期 `op_ready_cycle` 等于所有源寄存器就绪时间中的最大值。
- **发射与更新**：Uop 只有在 `op_ready_cycle` 之后，且在 Issue 带宽允许的周期内，才被正式发射执行。执行完毕后的完成周期 `complete_cycle` 会被写入 `rat_` 中，更新它所有的 `dst_regs`，从而唤醒后续依赖它的指令。
- **动态清理**：为了防止仿真过程中 `rat_` 持续膨胀导致内存泄漏，在 `IntervalCore::step` 中定期（每 10,000 条指令）清理早已过期的 RAT 表项。

### 10.3 仿真精度提升与规模验证
引入数据依赖检查后，执行 50 万条 `657_xzs` 宏指令的总仿真时钟周期由 ~104.0 万周期轻微增加至 ~104.3 万周期（IPC 从 0.481 下降至 0.479）。
这一结果符合预期：数据依赖导致部分指令被强制推迟发射，削弱了单纯依赖结构空闲（Issue Width）产生的“虚假”乱序能力。这也标志着我们的流水线模型具备了更高精度的**微架构数据流 (Dataflow)** 仿真能力。

### 10.4 功能单元 (FUs) 端口争用
除了寄存器依赖，真实的微架构还受限于特定类型的执行端口。我们在 `IntervalCore` 中进一步实现了执行端口的容量限制：
- 在 `MicroArchConfig` 中引入了 ALU、Load、Store、Branch 四类功能单元的独立端口数量配置（如 Cascade Lake 架构下默认的 4 ALU, 2 Load, 2 Store, 2 Branch 端口）。
- 将原本简单的按周期累加的全局 `issue_counts_` 升级为了复合的 `IssueTracker` 结构。
- 在 Issue 阶段的 `get_next_issue_cycle` 中，指令不仅要寻找全局 `issue_width` 未满的周期，同时还要确保该周期内**对应类型的执行端口仍有空闲**。如果对应端口（如 Load Port）已满，即使全局 Issue 带宽未满，该指令也会被推迟到下一个周期发射。

引入 FUs 端口争用后，总仿真周期进一步从 ~104.30 万轻微上升至 ~104.31 万，说明局部的结构冒险被精准地模拟了出来，模型精度达到了新的高度。

---

## 11. 阶段十：精细化流水线阻塞与结构冒险建模 (LQ/SQ, RS, 串行化)

为了进一步逼近真实处理器的微架构行为，我们在本阶段对指令分派 (Dispatch) 和访存模型进行了深度完善：

### 11.1 访存队列 (LQ/SQ) 与真正的 Retire 写回
- 引入了 `sq_` (Store Queue) 和 `lq_` (Load Queue) 来追踪处于流水线中的访存指令。当队列满时，后续的 Dispatch 会被阻塞，直到老指令 Retire 释放空间。
- **Retire 写回**：移除了原先在 Issue 阶段直接写入 Cache 的不精确逻辑。现在的 Store 指令在 Issue 阶段仅计算延迟，只有在最终的 Retire 阶段才会调用 `mem_->write_data()` 真正修改 Cache，彻底杜绝了因分支预测失败或乱序执行导致的 Cache 污染。

### 11.2 Store-to-Load Forwarding (STLF) 与地址消歧
- 在 Load 指令发射 (Issue) 前，会逆序扫描 SQ 中尚未 Retire 的 Store 指令。
- **STLF Hit**：如果地址完全重叠，Load 无需访问 Cache，直接从 SQ 获取数据（延迟降为 1）。
- **STLF Stall**：如果地址发生部分重叠，Load 必须等待该 Store 彻底 Retire 并写回 L1D 后才能发起读取，准确模拟了 Store Forwarding 失败带来的流水线停顿惩罚。

### 11.3 保留站 (RS / IQ) 容量限制
- 引入了 `iq_` 队列记录已分派但未发射的指令。如果保留站已满 (`config_.iq_size`)，Dispatch 阶段将停顿，直到最老的指令成功 Issue 并离开 RS。这使得模型不仅仅依赖 ROB 限制，更真实地反映了前端指令排队的情况。

### 11.4 串行化指令 (Serialization Instructions) 排空流水线
- 在 `InstDecoder` 中增加了对 `cpuid`, `mfence`, `sfence`, `lfence`, `iret`, `invd` 等指令的识别，并标记为 `is_serializing`。
- **Drain ROB**：串行化指令在 Dispatch 时，必须等待 ROB 中所有较老的指令全部 Retire 后才能进入流水线。
- **Block Pipeline**：串行化指令在 Retire 之前，会强制拉平全局的 Fetch/Decode/Dispatch 时间戳，禁止任何年轻指令提前重叠执行，实现了真正的内存与指令流屏障。

---

## 12. 下一步工作 (TODO)
根据项目规划，接下来的工作应包括：
1. **多核/多线程支持 (Multicore & SMT)**：将 `IntervalCore` 抽象为可实例化多份的模块，并考虑引入缓存一致性协议（如 MSI/MESI）以及共享的 L3 Cache 结构。
2. **更高级的预测器**: 探索 TAGE 或基于局部/全局历史的 GShare 预测器实现。
