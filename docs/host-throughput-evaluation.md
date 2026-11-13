# 宿主净吞吐评测与输入缓存

主吞吐为测量窗口内的 user functional UOP 数除以模拟墙钟时间。输入初始化、
预处理、来源完整性检查、输入校验和功能预热单列，完整进程墙钟也单列。
不能从并行墙钟中扣除 worker CPU 时间或采样百分比。

## 验证后预加载

2026-11-13 起，`build/fastsim simulate …` 默认启用二进制输入完整预校验和缓存。
`FASTSIM_PRELOAD_VALIDATED=0` 或 `--preload-validated-input false` 恢复流式 reader；
`1` 或 CLI `true` 显式开启。CLI 优先于环境变量，环境变量只能取 `0`/`1`，
非法值拒绝。这些设置仅作用于当前进程；通用库调用在变量未设置时仍使用流式 reader。
默认宿主版本及可追溯身份见[本轮交付](../fastsim-host-default-rollout-20261113.md)。

缓存先调用原二进制 reader 将整个来源读到 EOF，包括伴随文件的身份、人口、
动态 RAW 一致性与 footer 检查，再供正常两阶段模拟使用。这个准备阶段位于
`Simulator::run()` 之前；warmup、计分窗口、状态保留和计数重置使用原有包装器。
缓存要求一个新建且尚未消费的来源，不能在模拟途中切换或重复预加载。

缓存中前端描述按动态宏指令共享，而不是按 PC 共享。每 UOP 的完整事实、源记录
ordinal、hash、操作数和动态 producer distance 独立保留。依赖和操作数以连续
数组存储，通过原接口按当前记录提供稳定视图；内存事实和翻译片段使用稀疏载荷。
缓存不包含 gem5 时序、推测路径或额外构造的功能记录。

当前缓存不支持新的 core-functional、MMU-control 与 architectural-boundary
伴随文件，这些来源须显式关闭预加载，否则会明确拒绝。流式测量仍包含在线校验，
只能标记为 legacy validation-inclusive 吞吐。遇到损坏输入也继续拒绝，不能删除校验或
静默丢弃伴随字段来得到更高吞吐。较大来源需要额外内存，评测必须保留峰值 RSS。

日志提供：

- `HOST_INPUT_PREPARATION_SECONDS`：打开 manifest、来源及完整预解码校验的墙钟。
- `HOST_SIMULATOR_INITIALIZATION_SECONDS`：构造模拟器的墙钟。
- `HOST_VALIDATED_PRELOAD`：解析默认、环境变量和 CLI 后实际选择的预加载策略。

该标志是请求的策略；本指南的验证排除口径还要求 manifest 全部为受支持的
binary FST 来源并成功完成完整预校验。JSON reader 不经过该二进制缓存，
其测量仍包含在线读取/解析/校验，不能仅凭该标志认定 validation-excluded 吞吐。

输出中的 `input_identity_wall_seconds` 记录模拟前后的独立来源哈希时间；
`wall_time_breakdown.functional_warmup_seconds` 与 `measurement_seconds` 分别
记录功能预热和实际测量阶段。主速率读取
`scope_metrics.throughput.user_uops_per_second`。

## 配对执行

所有项目临时路径按 `scripts/project_tmp.sh` 设置。当前宿主使用
`scripts/run_high_frequency.py` 校验 CPU 52–63、socket/NUMA 1 和已有
performance 策略；宿主变化时重新核验。同一配对保持输入、来源哈希、manifest、
有效模型配置、warmup、计分/执行边界和编译选项不变，串行交替执行各版本。
2026-11-12 本轮三档都采用 Release、IPO/LTO 16 jobs，`FASTSIM_ENABLE_NATIVE=OFF`；
不能与历史 `FASTSIM_ENABLE_NATIVE=ON` 测量混为同一对照。

```bash
source scripts/project_tmp.sh
export LD_LIBRARY_PATH=/opt/gcc-11/lib64:${LD_LIBRARY_PATH:-}
python3 scripts/run_high_frequency.py -- \
  build/fastsim simulate \
  --config SOURCE_BOUND_CONFIG --manifest SOURCE_MANIFEST \
  --perf-cache-input-identity true --output OUTPUT_JSON
```

以上展示当前默认可执行文件；实际命令还必须保留来源需要的核数、测量范围、
native-kernel 等原始参数。最新完整命令保存在
`tmp/host-throughput-default-20261113/*.run.json`，具体结论见
[默认启用与等价验证报告](../fastsim-host-default-rollout-20261113.md)。
历史优化实验的命令和环境设置仍见
[第一轮报告](../fastsim-host-throughput-optimization-20261112.md)。

保留真实工作频率、运行前后核竞争快照、完整进程墙钟和每阶段成本。边界快照
不能证明持续独占；共享宿主上的少量配对属于诊断，不替代 30 组吞吐门禁。

## 模型结果守恒

可用维护工具复核本轮预测等价：

```bash
python3 tools/compare_fastsim_model_equivalence.py \
  --before FROZEN_PREDICTION_JSON --after OPTIMIZED_PREDICTION_JSON \
  --output EQUIVALENCE_RECEIPT_JSON
```

工具要求完整输入身份、有效配置、逐核 time-accounting 与模型 PMU 命名空间，
任何非宿主诊断字段的变化都会使门禁失败。它只比较同源模型预测，不选取或评分
gem5/native 参考，输出中的 CPI 差值是两版模型之间的变化。

吞吐优化需要逐值比较 CPI 的原始逐核 active cycles、macro/UOP 人口及全部模型
PMU，包括 retired-load 的点估计、原始资格 unknown 与 fallback 字段。只允许
排除宿主计时、吞吐、前沿等待次数和驻留 chunk 峰值等调度诊断，不能排除目标
时序或事件计数。

输入缓存测试目标为 `fastsim_validated_cache_tests`；构建后同时运行原
`fastsim_tests`、受影响的动态 DTLB 和 SPR frontend 测试。翻译 GC 仍在原回收
边界执行，只检查可能失去最后引用的候选键；迟到 walk 检查版本之前不得回收。
反馈初始化使用既有 worker 和同步屏障，source-order proposal 保留协调者初始化
顺序。因初始化移动，feedback/domain 子计时器的覆盖范围发生变化，不能将其与
旧版子计时器直接比较后当成净收益。

诊断环境变量 `FASTSIM_MERGE_DBG`、`FASTSIM_STORE_WINDOW_AUDIT`、
`FASTSIM_STORE_ENGINE_CLOSURE_DEBUG`、`FASTSIM_TSO_TRACE`、
`FASTSIM_TSO_LOAD_HIST`、`FASTSIM_TSO_PREPARE_HIST`、
`FASTSIM_TSO_RETIRE_HIST` 在进程启动时绑定；运行中修改环境不再改变这些诊断。

结果守恒不等于新增 native PMU 或 SPR 硬件精度认证。独立参考仍遵循
[精度报告合同](accuracy-reporting-contract.md)，不得用模型间零差值填补原生参考
覆盖或硬件事件语义的缺口。
