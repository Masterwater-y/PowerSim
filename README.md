# MineSim

MineSim 是一个非周期精确 (non-cycle-accurate) 的、多核多线程乱序的 CPU 性能模拟器。目前项目处于初版开发阶段，仅支持单核单线程仿真。

该模拟器基于 Trace-driven 架构，支持直接流式读取 DynamoRIO 格式的压缩 trace 文件 (`.trace.gz`)，避免了将巨大 Trace 完全加载到内存中导致的 OOM 问题。通过将 x86 宏指令解析拆解为 RISC 风格的微指令 (Uop) 并引入时间戳驱动的**区间仿真 (Interval Simulation)** 模型，MineSim 能够在极高的仿真速度下，准确评估和还原现代超标量处理器中的缓存延迟、数据依赖阻塞 (RAT)、结构冒险 (执行端口/保留站限制) 以及内存级并行度 (MLP)。

---

## 🚀 项目特性与开发进度

### ✅ 已实现特性
1. **流式 Trace 解析与原生解码**：
   - 采用 `zlib` 直接流式读取 DynamoRIO 离线 trace。
   - 摒弃了第三方解码库，直接集成 DynamoRIO (`dr_standalone_init`, `decode_from_copy`) 实现对 x86 字节码的 100% 原生高精度反汇编，精准提取访存大小和分支条件。
2. **微指令 (Uop) 映射与重组**：
   - 自动将复杂的 x86 宏指令拆解为 `LOAD`, `STORE`, `ALU`, `BRANCH` 等 Uop 序列，真实反映超标量前端的解码与分派开销。
3. **缓存与内存层级 (Memory Hierarchy)**：
   - 实现了基于 LRU 替换策略的组相联 L1I / L1D / L2 / L3 缓存模型。
   - 实现了完整的虚拟地址到物理地址转换，包含 ITLB / DTLB / STLB 及简易的 Page Table (Page Walk 惩罚)。
4. **乱序执行区间仿真核心 (IntervalCore)**：
   - **时间戳模型**：抛弃了缓慢的逐周期遍历，使用时间戳跳跃推进 Fetch、Decode、Dispatch、Issue、Retire 阶段。
   - **数据依赖与寄存器重命名 (RAT)**：跟踪虚拟寄存器的就绪周期，阻塞依赖未就绪数据的后继指令发射。
   - **结构冒险与容量约束**：实现了 ROB (重排序缓冲)、IQ / RS (保留站/指令队列) 的满载阻塞逻辑，并对执行单元 (ALU/Load/Store Port) 实现了带宽限制。
   - **分支预测器**：内置 Bimodal (双模态 2-bit 饱和计数器) 与 OneBit 分支预测器，模拟预测错误时的前端 Flush 惩罚。
5. **高精度访存队列与串行化建模**：
   - **LQ / SQ 限制与 Retire 写回**：Store 操作推迟到 Retire 阶段才真实写入 Cache，防止污染。
   - **Store-to-Load Forwarding (STLF)**：Load 指令发射前可扫描 SQ，实现同地址的数据直接转发或地址部分重叠的停顿惩罚。
   - **串行化指令 (Serialization)**：支持识别 `MFENCE`, `CPUID` 等指令，并在 Dispatch 阶段强制 Drain ROB (排空流水线)。

### 📝 待办事项 (TODO)
- [ ] **多核扩展 (Multicore)**：支持多个 `IntervalCore` 实例并行，引入共享 L3 Cache 模型。
- [ ] **缓存一致性协议**：在多核扩展的基础上，引入 MSI 或 MESI 缓存一致性协议建模。
- [ ] **高级分支预测器**：实现基于局部/全局历史的 GShare 或 TAGE 分支预测器。

---

## 🛠️ 编译指南

### 环境要求
- **C++17** 兼容的编译器 (如 GCC 9+ 或 Clang)
- **CMake** (3.15+)
- **DynamoRIO**：本项目强依赖 DynamoRIO 原生库用于高精度指令解码。
- **Zlib**：用于解压 `.trace.gz` 文件。

### 配置 DynamoRIO 环境变量
在编译和运行前，请确保系统中已经安装/编译了 DynamoRIO，并配置环境变量 `DYNAMORIO_HOME` 指向其根目录。

**注意：**如果你在运行 `./build/minesim` 时遇到 `error while loading shared libraries: libdynamorio.so` 错误，说明系统的动态链接器无法找到 DynamoRIO 的运行库。你需要在运行前将 DynamoRIO 的 `lib64` 路径添加到 `LD_LIBRARY_PATH` 环境变量中。

```bash
export DYNAMORIO_HOME=/path/to/your/dynamorio
export PATH=$DYNAMORIO_HOME/bin64:$DYNAMORIO_HOME/tools/bin64:$PATH
export LD_LIBRARY_PATH=$DYNAMORIO_HOME/lib64/release:$DYNAMORIO_HOME/lib64/debug:$LD_LIBRARY_PATH
```

### 构建步骤
```bash
git clone git@code.byted.org:yinhaolang/minesim.git
cd minesim
mkdir build && cd build
cmake ..
make -j4
```

构建完成后，将生成两个主要的可执行文件：
1. `minesim`：核心的 CPU 性能仿真器主程序。
2. `trace_verifier`：用于验证和打印 Trace 反汇编与访存信息的调试工具。

---

## 📖 使用方法

### 1. 运行核心仿真器 (minesim)
`minesim` 接收 DynamoRIO 格式的离线 trace 文件进行性能仿真。

**基础运行**：
```bash
./build/minesim /path/to/drmemtrace.trace.gz
```

**限制仿真指令数**：
如果你只想仿真前 N 条宏指令，可以传递指令数量参数：
```bash
./build/minesim /path/to/drmemtrace.trace.gz 1000000
```
运行结束后，程序会打印出总仿真周期数、IPC、UPC、各级 Cache 的 Hit/Miss 率、分支预测准确率以及 STLF 转发次数等详尽的性能统计数据。

### 2. 微架构参数配置
MineSim 支持通过外部 INI 格式的配置文件动态调整微架构参数（如流水线宽度、Cache 大小、ROB/IQ 容量等）。
目前主程序代码中默认加载 `config/cascade_lake.cfg` (基于 Intel Cascade Lake SP 架构的预设)。
你可以在 `config/` 目录下复制并修改配置文件，调整后无需重新编译即可生效。

### 3. 运行 Trace 验证工具 (trace_verifier)
用于检查 Trace 文件是否损坏，或用于人工对比 DynamoRIO `view` 工具的输出以确认解析精度：
```bash
# 解析并打印前 200 条指令的反汇编、访存地址和操作大小
./build/trace_verifier /path/to/drmemtrace.trace.gz 200

# 开启纯净比对模式 (仅输出 PC 与反汇编，用于 diff 对比)
./build/trace_verifier /path/to/drmemtrace.trace.gz 200000 --compare > parsed_trace.txt
```