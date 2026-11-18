# C4/C8 FST 原生后端对比实现

最新吞吐口径已按用户要求改为包含预处理、排除额外校验的端到端指标，见
[当前端到端报告](../fastsim-sniper-zsim-current-20261116.md) 和
[计时合同](trace-comparison-throughput-contract-20261116.md)。下文保留原生适配结构和历史纯模拟计时说明。

新增 [Sniper interval / ROB 同输入对照](../sniper-interval-comparison-20261116.md)：
interval 在隔离副本补齐 FST 依赖、FU 类型与 EOF 排空后，新跑完整十项 C4/C8；
ROB、FastSim 与并行准备 zsim 的旧样本分别保留，不默认更换生产配置。

本实现让同一份已退休功能流进入 FastSim、Sniper 和 zsim 的时序后端。
Sniper、zsim 是独立比较对象，不预设它们正确；gem5 保存的独立测量作为参考。
修改集中于 `tools/trace_comparison/`，第三方树和构建产物隔离在项目 `tmp/`。
FastSim 的生产模型及并行两阶段框架未因本适配工作而修改。

## 输入与后端

| 项目 | 实现 | 仍有的限制 |
|---|---|---|
| FastSim | 冻结运行时 ELF、递归冻结源配置，保留现有 simulate 路径 | 当前模型自身近似仍在 |
| zsim | 原生 OOOCore、ROB、FilterCache/TimingCache、MESI、EventRecorder、ContentionSim；保留真实 phase/weave | FST 类别到 DynUop 的端口/延迟映射替代 XED 解码；核心资源仍是原生固定模型 |
| Sniper | 原生 ROB、依赖追踪、cache/coherence、drain；使用已有 SPR 类别表 | 类别表及资源聚合有近似，不能称完全未经修改的上游解码器 |

旧 `fstzsim` 中自定义 FstO3Core 和空 weave 路径不用于本次结果。
原生 zsim 的上游 `traceDriven` 入口只支持缓存访问 trace，不能直接承担本任务的核心时序比较；
这里提供保留原生时序类的独立驱动。上游实现见
[zsim init.cpp](https://github.com/s5z/zsim/blob/master/src/init.cpp)。
也检查了 [zsim-pt](https://github.com/efeslab/zsim-pt) 的前端方向；
现有 FST 不含完整原始指令字节，不能直接声称重用了原生 XED 解码。

输入包含每核完整 warmup 和 measurement 记录。不能为了对齐目标指令数删掉
kernel、其他上下文、软件预取、宏指令尾部片段或从属微操作。
NoAccess 记录保留其执行与依赖，只不发出数据访问；软件预取保留访问，原生 zsim
以无架构结果的 load 近似。serialize 标记进入原生时序约束，不能用替换 store 类型的方式处理。
扩展 RAW 依赖与四个内联依赖共同进入后端，当前最多 16 条直接生产者边，超出拒绝。
zsim 使用逐记录唯一的逻辑寄存器身份，避免循环复用槽位破坏长依赖。

zsim 的功能输入通过冻结的 FastSim BinaryTraceSource 库读取，未调用 FastSim 的时序预测器。
Sniper 使用单独的严格 FST reader，处理 `.deps`、`.mreq`、`.imap`、`.ffe.gz` 和合法的
syscall 元数据尾表；SPR 编码在原始内容校验后归一化。syscall 元数据中的采集时间不进入预测。
指令侧采用同一 ASID、页映射种子及物理地址函数；预测器保留虚拟 PC，I-cache 使用建模物理地址。

两个原生适配器还计算逐核功能语义 FNV64：归一化记录、完整扩展依赖、指令物理地址和长度。
报告同时要求逐核退休宏指令/UOP 数一致和语义校验和一致；总数一致不能替代逐核检查。
该校验和用于发现适配错误，不替代独立的完整采集审计。FastSim 的源校验另随 stats 保存。

最终汇总还要求跨三者的输入内容身份：重新枚举全部 FST/伴随文件，逐文件 SHA256 与
FastSim 消费时输出的身份匹配；文件及 trace 目录的 mtime/ctime 必须早于三个
prelaunch freeze.json 中最早的一份，hash 前后 stat 必须不变。这样覆盖本轮完成的运行，
同大小改写、文件替换及新增伴随文件均拒绝。该回溯校验以本地文件系统 ctime、
稳定的祖先路径和时钟为前提，不是对抗恶意存储的证明。新 runner 同时保存运行前后 stat。

## warmup、结束与统计

输入读取、完整性校验和归一化 preload 在计时外完成。warmup 只训练缓存/预测器，
不把其完成延迟带入 measurement；各物理参与核先完成 warmup 再开始详细模拟。
末尾必须执行全部已保留记录，并由原生后端排空：Sniper 读取 doCommit 退休计数，
zsim 等待真实 ROB 最后退休时间并运行事件 weave。仅“读完输入”不算完成。

三方共同项是 **retained physical completion time**：共同起点到完整物理保留流的最后完成。
参考为保存的 gem5 `retirement_stop.tick` 最大值减 `common_start_tick`；先核对参与核和
每核 stop 的 record ordinal。FastSim 取 `target-common-origin-to-last-completion`，
zsim 取原生修正后最大 core cycles，Sniper 取逐核详细计时终点减起点的最大值。
各引擎先换算秒，不直接比较不同频率的 cycle 数。

报告的归一化量是 `seconds × reference_Hz × cores / physical_macros`，
称为 elapsed macro cycle proxy。逐项显示相对时间误差与绝对 proxy 误差（cycles/macro），
C4/C8 分别给出 case-equal MAPE 和 MAE。它包含其他上下文，不是 workload-task active CPI。
FastSim 的 task-active CPI 单独通过维护入口 quick validation 报告，不能混入三方共同表。

当前参考使用保存的审计，未重新进行完整内容级审计，结果标为 provisional、formal=false。
部分 kernel/其他上下文记录缺少指令长度，默认拒绝；本轮显式启用
`--allow-incomplete-code`，使用 1 byte fallback 并保存逐核 unknown 数。
这使本轮成为有明确缺项的估计比较，不能升级为正式精度通过。

## 配置差异

缓存几何尽量按源绑定，C8 的 Sniper LLC sharing 和 directory/controller interleaving
显式设为 8，避免复用 C4 配置造成 LLC 容量翻倍。
但这并未实现三者硬件等价：zsim 仍为原生固定 OOOCore 资源和 100-cycle SimpleMemory；
Sniper 的 DRAM 常数 38ns 与既有 1228.8GB/s 带宽配置也是近似，未重新校准为本次
gem5 DDR5-4400 控制器。原生 zsim 的 predictor/端口组织也与 SPR 不同。
因此表中差异同时包含模型差异、参数差异和前端近似，不能归因成单一算法优劣。
native PMU 未在本轮作硬件等价认证。

三个 campaign 在 CPU 52–63、NUMA 1、已有 performance 策略上并发运行，逐项保存实际频率检查。
测量段 user MUPS 与完整进程墙钟分别保留；原生输入校验、preload 和功能 warmup 已移出测量。
zsim 的 DynUop 翻译预先完成，Sniper 仍在测量段构造动态 MicroOp；不同前端工作量及共享主机
竞争意味着这些速率只能作诊断，不能作为受控吞吐验收或正式加速比。

## 复现

从 FastSim 根目录执行，先 `source scripts/project_tmp.sh`。
需要现有第三方参考树、FST 源和 `build/libfastsim_lib.a`，默认路径写在脚本中。

```bash
python3 tools/trace_comparison/build_native_zsim.py
python3 tools/trace_comparison/prepare_sniper.py \
  --source tmp/sniper-zsim-spr-20261112/sniper \
  --output tmp/native-trace-adapters-20261116/sniper-source-final
```

prepare 拒绝覆盖已有树；复现到新目录时，runner 使用 `--sniper-tree` 指定其路径。
也可用 `--fastsim-binary`、`--zsim-binary` 绑定指定的冻结程序。
Sniper 构建：

```bash
cmake -S tmp/native-trace-adapters-20261116/sniper-source-final \
  -B tmp/native-trace-adapters-20261116/sniper-source-final/build/fst-native -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DSNIPER_FRONTEND=none -DSNIPER_ENABLE_TORCH=OFF \
  -DSNIPER_XED_HOME="$PWD/tmp/sniper-zsim-spr-20261112/sniper/xed_kit" \
  -DSNIPER_LINK_INTO_TREE=OFF \
  -DCMAKE_CXX_FLAGS="-I$PWD/tmp/native-trace-adapters-20261116/sniper-source-final/fst-adapter-include" \
  -DCMAKE_EXE_LINKER_FLAGS=-pthread
cmake --build tmp/native-trace-adapters-20261116/sniper-source-final/build/fst-native --target sniper -j16
```

测试：

```bash
python3 tools/trace_comparison/test_reader.py
python3 tools/trace_comparison/test_native_zsim.py
python3 tools/trace_comparison/test_sniper_fst.py
python3 tools/trace_comparison/test_elapsed_metrics.py
python3 tools/trace_comparison/test_report_matrix.py
```

分别在独立终端运行以下命令即可并行；每个输出目录必须不存在：

```bash
python3 tools/trace_comparison/run_native_matrix.py --simulator fastsim \
  --output tmp/native-trace-comparison-final-20261116/fastsim --jobs 2 --allow-incomplete-code
python3 tools/trace_comparison/run_native_matrix.py --simulator zsim \
  --output tmp/native-trace-comparison-final-20261116/zsim --jobs 2 --allow-incomplete-code
python3 tools/trace_comparison/run_native_matrix.py --simulator sniper \
  --output tmp/native-trace-comparison-final-20261116/sniper-repaired --jobs 2 --allow-incomplete-code
python3 tools/trace_comparison/source_identity.py \
  --campaign tmp/native-trace-comparison-final-20261116
python3 tools/trace_comparison/report_matrix.py \
  --campaign tmp/native-trace-comparison-final-20261116 \
  --output-prefix fastsim-native-trace-comparison-20261116
```

runner 的 freeze.json 固定二进制、配置 include 链、源 manifest 和驱动身份；
逐项 command.json、run.log、result.json 保留成功和失败。
native-build-receipt.json 保存 zsim 上游/补丁/编译来源与功能库身份；
Sniper 的 fst-adapter-patch-receipt.json 保存源改动和私有冻结 reader 身份。
本轮首次严格 framing 失败的 Sniper 目录仍保留作诊断，正式汇总只读取 `sniper-repaired`。

结果入口为 [三方结果](../fastsim-native-trace-comparison-20261116.md)，
完整明细为同名 JSON/CSV，原始输出在 `tmp/native-trace-comparison-final-20261116/`。
