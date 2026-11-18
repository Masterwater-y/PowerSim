# 三方端到端吞吐计时合同

2026-11-18 用户更新宿主核要求：后续新仿真使用与目标核数相同数量的独立高频物理核；当前核验池为 NUMA 1 的 CPU 48–63，通过 `scripts/run_high_frequency.py --cores N` 选择。下文 52–63 描述此前比较的实际 placement，不追溯修改历史数据。

用户最新确定：后续 zsim 主吞吐报告以文件级并行预处理为准。两个矩阵运行入口
默认选择已测并行 ELF，显式设置 `TRACEBENCH_PREPARE_JOBS=auto`；缺少并行基线时
报错，不回退串行。`report_e2e.py` 默认拒绝串行 zsim 主报告；重现旧数据必须显式
加 `--historical-serial-zsim`，输出也标为历史串行对照。

zsim 后续已实现受限文件级并行准备，输入准备仍计入本合同的端到端时间。
默认线程数为文件数与 CPU affinity 的较小值，`TRACEBENCH_PREPARE_JOBS=1` 回退串行；
读取、转换的每文件顺序及原有顺序功能 warmup 均保持。
新旧结果见 [并行准备报告](../zsim-parallel-input-preparation-20261116.md)。

2026-11-16 用户最新要求：预处理计入主吞吐，额外可靠性校验不计入。
本合同覆盖当前 C4/C8 FastSim、Sniper、zsim 比较，优先于该比较此前的纯模拟主指标。

主吞吐 = FST 测量段 user UOP 数 / 从引擎启动前到最后模型完成的真实墙钟。
输入读取/解压/功能转换、分配/初始化、功能 warmup、动态模拟、同步、weave、必要排空
全部包括；所有测量物理记录仍执行。warmup 成本算入分母，warmup UOP 不算入测量分子。
附带报告 all-UOP 吞吐、纯模拟辅助速率、进程墙钟、校验成本和峰值内存。
组内主汇总使用总 user UOP / 总端到端耗时，逐负载速率及中位数/范围另列；
不以速率的算术平均代替总量/总时间。校验成本字段记录本轮前后收据核验本体，
不包含历史严格审计、框架日志分析和 JSON 读写成本。

额外 SHA256、整流内容审计、依赖/静态 UOP 投影审计和逐条语义摘要核对独立执行。
计时回放仍读取原始 FST 和全部功能伴随文件，不能复用已经转换好的 DynUop/模板来
隐藏预处理成本。安全读取所需的格式、长度和下标检查保留；解压器在实际解码时执行的
CRC 计算随读取计时，但受信回放不为了重复审计而强制消费仅含审计信息的 footer/EOF。
因此不保证计时回放再次建立完整 gzip CRC/尾部完整性，完整性由此前严格审计和未变的
内容收据保证。FastSim `.ffe.gz` 的纯审计尾部属于这一情形，全部功能描述仍被消费。
不从线程 CPU 累计时间、profile 比例或重叠子阶段时间推算校验排除量。

实现仅修改 `tools/trace_comparison/` 和项目 `tmp/e2e-throughput-20261116/` 内的
隔离构建。生产 FastSim 模型源码及安装 ELF 不因本工作而修改。
`prepare_e2e_fastsim.py` 生成带开关的隔离功能 reader：默认严格校验；仅在
`TRACEBENCH_VALIDATED_INPUT=1` 且外部收据通过时跳过重复审计。不要把这个实验开关
当作生产读取不可信输入的接口。Sniper 的私有 reader 使用同一计时/审计分界。

`e2e_launch.py` 先校验全部输入收据，再记录 monotonic 起点并启动实际引擎；
FastSim/zsim 在模型完成后发出 `[TRACEBENCH-END]`，Sniper 取全部核的
`FST-ENDPOINT` 最大宿主时间。结果格式化和后置输入审计均在终点之后。
校验收据来自保留的严格 C4/C8 原始运行，核验完整文件清单和原 inode/stat，
并绑定 manifest 与 thread-state plan。文件、目录或辅助输入改变时拒绝回放。
依赖普通本地文件系统的 ctime、稳定祖先路径和时钟；不是恶意存储防护证明。

每个引擎每项依次运行，同用 CPU 52–63、NUMA 1 和既有 performance 策略；
不修改全局 governor，不清空系统文件缓存。当前是每项一次的受控资源诊断，
不是多轮配对吞吐验收。原生模型/硬件近似差异继续单列。

构建与运行：

```bash
source scripts/project_tmp.sh
python3 tools/trace_comparison/prepare_e2e_fastsim.py \
  --output tmp/e2e-throughput-20261116/fastsim-source
cmake -S tmp/e2e-throughput-20261116/fastsim-source \
  -B tmp/e2e-throughput-20261116/fastsim-source/build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DFASTSIM_BUILD_TESTS=OFF -DFASTSIM_LTO_JOBS=16
cmake --build tmp/e2e-throughput-20261116/fastsim-source/build --target fastsim -j16
python3 tools/trace_comparison/build_native_zsim.py \
  --functional-root tmp/e2e-throughput-20261116/fastsim-source \
  --output tmp/e2e-throughput-20261116/zsim
```

Sniper 按原生适配说明准备新树 `tmp/e2e-throughput-20261116/sniper-source`，
冻结当前 reader 和 trust header，再使用 CMake `build/fst-native` 构建。
完整配置命令保留在该目录的 configure/build 日志和 CMakeCache 中。

```bash
python3 tools/trace_comparison/run_e2e_matrix.py \
  --output tmp/e2e-throughput-20261116/campaign
python3 tools/trace_comparison/report_e2e.py \
  --campaign tmp/e2e-throughput-20261116/campaign \
  --output-prefix fastsim-sniper-zsim-e2e-throughput-20261116
```

`--strict-pairs --case c04-817.flac_s` 仅用于校验拆离的模型等价验证，其严格模式
耗时包含审计，不能进入主吞吐表。原始失败、逐核差异和重复验证记录全部保留。
