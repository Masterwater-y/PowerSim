# perf 来源修复与证据不完整时的误差

2026-10-15。按用户明确要求，证据不完整时也先报告可计算误差，并列出缺失证据。
本轮沿用维护验证器，保留strict默认路径；没有修改FastSim时序或旧trace/oracle。

## 本轮修复

- gem5退休账本的`incomplete_macros/uops`改为只含测量窗口内实际功能成员；
  `all_history_incomplete_macros/uops`单列完整历史诊断。跨预热宏只计算测量成员，
  不将纯warmup abandonment混入测量尾部。
- 对缺失first标记，检查实际译码macro的全部前缀；只有前缀均为精确的无条件`MicroDebug`
  warn/hack/inform、且起点first已证明时才将首个提交成员识别为有效起点。
  不凭mnemonic、PC变化或空账本猜测。Graph500有限异常样本中的MOVNTDQ符合这一路径；
  不据此宣称所有历史缺口都已消失。
- 新增完整稀疏`macro-boundary-core*.jsonl`及绑定FST核、记录数量和测量起点的metadata。
  记录ROM入口、debug前缀、开放宏重启和未知起点；不包含时间、PMU或cache命中结果。
  事件不设样本上限，写入失败即拒绝。原16条有界诊断继续保留为人工排查材料。
- 独立扫描器按这些功能边界拆开前缀，核对PC、开放状态、顺序、数量、完备性及
  当前源身份；旧trace没有这些事实，保留缺失，不补造。
- 新增`--perf-cache-incomplete-errors`，将请求原语核验从退休资格失败中分离。
  共同输入、来源、核/任务集合、实际ordinal边界、原语重算和守恒继续检查。
  退休来源覆盖问题作为结构化`evidence_gaps`输出；strict默认模式仍拒绝旧案例。

## 误差如何解释

本轮误差比较FastSim的`estimated_value`与gem5 v2原生观察器的已采集计数，
不是旧path-class代理，也不替换为native tag-miss。缺失请求可能改变参考计数，
缺失宏资格可能改变退休事件成员；当前误差不能当作完整硬件perf误差或误差上下界。

维护报告同时输出：模型/参考原始计数、各自macro分母与per-1K值、绝对/相对误差、
参考unknown资格数、模型/参考能力缺项、功能边界缺口及窗口近似。完整证据误差和
**Incomplete-evidence errors**分别聚合MAPE/WAPE/max APE。非零预测/零参考保留
绝对误差及零参考案例数，百分比为不可用；不静默删除。

请求的task/CPL event-context尚不完整，L2/LLC暂比较physical范围。未知task请求的
诊断0不作为任务实测0计算误差。physical还包含其他任务与内核，不能与workload-task
的CPI或native miss表混成同一事件人口。

## 验证与证据

证据根：[本轮实验](../tmp/perf-incomplete-repair-20261015/)。原大负载输入及FastSim
预测复用`tmp/spec-two-cache-20261015/`，原失败报告保留；本轮只重新评分，
不是新的吞吐测量或模型精度改善。

- 请求独立审计、证据不完整误差、零参考、未知task范围、维护入口与摘要：12项回归通过。
- measured/完整历史尾部、debug前缀与稀疏生产：真实C++ helper回归通过。
- 功能边界扫描与错误源拒绝：3项回归通过。
- 已有及新增perf专项88项通过；默认CMake构建、设置GCC11运行库后的直接FastSim测试通过。
- 全Python432项：424通过、5跳过、3项既有环境错误。缺`fastsim_py`；两个
  checkpoint recovery测试缺外部TCSim脚本。没有断言失败。
- gem5按192个可用CPU的80%即`-j153`构建成功。最终四核MOVNTDQ采集完成：
  139.446秒、163,906,080字节FST；四核leading/discontinuity均为0，独立退休人口
  审计通过。功能边界流分别确认26,763/22,844/26,699/26,779个debug前缀。
  完整共同终点、owner及同步审计也通过；这不是硬件perf认证。
- 首次小探针遗漏`warn_once`而失败，所有源和拒绝状态保留；补齐debug-only once
  名称后重建并重新采集，没有把旧失败改成通过。既有两负载最终重新评分已完成，详见下表。

复现旧两案带证据缺项的误差：

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/spec-two-cache-20261015/source \
  --stats-dir tmp/spec-two-cache-20261015/stats \
  --output-dir tmp/perf-incomplete-repair-20261015/validation-reproduce \
  --expected-cases 2 --jobs 2 \
  --perf-cache --perf-cache-estimates --perf-cache-incomplete-errors
```

所有证据缺项及完整比较结果保存在逐案`validation.json`，汇总入口为
[维护误差报告](../tmp/perf-incomplete-repair-20261015/validation-final/perf-cache-estimates.md)。

独立最终审查发现并修复逐核分母差异被宽松模式隐藏、以及严格比较忽略已声明缺项
两个问题。回归包括逐核差异相互抵消、明确参考缺项和零参考。逐案raw/rate误差及
请求unknown诊断也已补齐。旧大源没有新宏边界事实，原证据缺失继续标注。

## 旧两负载的最终误差（证据不完整）

范围为 **physical**，比较FastSim近似预测与gem5 v2原生观察计数；证据不完整，硬件等价未认证。
两案共同输入/身份/结构化来源检查及native来源门禁通过，退休资格缺口单独保留。
这些是用户要求先报告的暂定误差，不混入完整参考精度；不是native tag-miss误差表。

| workload | perf v2字段 | FastSim近似计数 | gem5观察计数 | 相对误差 | APE | 绝对计数差 |
|---|---|---:|---:|---:|---:|---:|
| c04-782.lbm_r | perf_l1d_load_misses | 146,305 | 144,648 | +1.146% | 1.146% | 1,657 |
| c04-782.lbm_r | perf_l2_demand_misses | 583,335 | 150,339 | +288.013% | 288.013% | 432,996 |
| c04-782.lbm_r | perf_llc_misses_core | 235,329 | 308,431 | -23.701% | 23.701% | 73,102 |
| c04-854.graph500_s | perf_l1d_load_misses | 732,716 | 516,766 | +41.789% | 41.789% | 215,950 |
| c04-854.graph500_s | perf_l2_demand_misses | 590,026 | 593,395 | -0.568% | 0.568% | 3,369 |
| c04-854.graph500_s | perf_llc_misses_core | 213,808 | 241,688 | -11.536% | 11.536% | 27,880 |

| 字段 | raw MAPE | raw WAPE | max APE | per-1K WAPE |
|---|---:|---:|---:|---:|
| perf_l1d_load_misses | 21.467% | 32.900% | 41.789% | 32.952% |
| perf_l2_demand_misses | 144.290% | 58.672% | 288.013% | 58.326% |
| perf_llc_misses_core | 17.618% | 18.356% | 23.701% | 18.334% |

每案使用各自同范围macro分母；MAPE按case等权，WAPE按参考计数加权。
本次同人口比较的raw与rate APE一致，WAPE因案例权重不同而不同。

| workload | L1 miss参考unknown资格数 | observer请求身份未知数 | unknown-kind原语lookup数 |
|---|---:|---:|---:|
| c04-782.lbm_r | 1,520,581 | 1,249,034 | 0 |
| c04-854.graph500_s | 157,102 | 135,071 | 0 |

上述unknown是资格/观察器诊断，不能当作漏计miss数量；请求身份未知数没有逐事件归属，
不能在L2和LLC之间重复相加，也不构成误差上下界。原语unknown-kind为0不证明事务身份完整。

逐事件证据缺口：

| 字段 | 主要模型缺项 | 主要参考缺项 |
|---|---|---|
| perf_l1d_load_misses | isa_membership, macro_units, member_service_facts, roi_edges, source_qualification | isa_membership, macro_units, member_service_facts, retirement_context, roi_edges, source_qualification |
| perf_l2_demand_misses | code_reads, event_context, merge_policy, retry_policy, rfo, roi_edges, speculative_code, speculative_data | code_reads, data_reads, event_context, merge_policy, retry_policy, rfo, speculative_code, speculative_data |
| perf_llc_misses_core | code_reads, event_context, hardware_prefetch_l1_l2, llc_data_state_mapping, merge_policy, retry_policy, rfo, roi_edges, software_prefetch, speculative_code, speculative_data | code_reads, core_origin_filter, data_reads, event_context, hardware_prefetch_l1_l2, llc_data_state_mapping, merge_policy, retry_policy, rfo, software_prefetch, speculative_code, speculative_data |

LBM仍缺完整宏边界事实，并保留warmup incomplete 2 vs FST 0的范围差异；Graph500
旧源仍保留leading incomplete 500/132/716/284及不可用的退休资格。新采集器修复没有
被倒填进这两份旧oracle。L1来源还存在forwarding/特殊ISA资格未知；L2/LLC仍缺完整
推测请求、事件时钟归属/边沿等证据，LLC另缺预取及目录/数据状态映射。
全部细项、逐核数量和raw/rate误差保存在JSON，完整十项表见
[维护报告](../tmp/perf-incomplete-repair-20261015/validation-final/perf-cache-estimates.md)。

writeback参考的诊断0不能证明真实为0；报告保留绝对差，非零模型/零参考的百分比为不可用。
L2/LLC的workload-task事件上下文仍未知，保留不可用，不拿任务诊断0计算误差。

本批只有两个研究输入，Graph500窗口包含验证代码；与历史32/10-case表的样本、ROI和
配置不同，不构造“修复前后精度改善”。两个存档预测SHA256保持不变；本轮没有新吞吐测量。
原active CPI与native结果见[原实验报告](spec-two-cache-validation-20261015.md)。

最终源审计使用的工具指纹在逐案报告中；通用跨生产者比较最后保留各自分母的兼容行为。
此后用当前维护比较函数重放已审计的两案结果，逐字段完全相同，见
[比较不变性](../tmp/perf-incomplete-repair-20261015/current-comparison-invariance.json)。

最终还用生产表达式回归复现并排除了条件`MicroDebugFlags`子类：条件不满足时它可以
正常提交，不能当作被跳过的前缀。真实MOVNTDQ采集验证使用无条件`warn_once`路径；
最后这项收紧保留该路径，并由正反例表达式测试及重新编译验证，不重采旧大负载。

全量测试的三个环境错误名称：`test_python_bindings`、
`test_checkpoint_recovery_receipt_is_strict_and_reusable`、
`test_checkpoint_recovery_rejects_record_output`。原始输出见
[最终全量测试](../tmp/perf-incomplete-repair-20261015/python-release.log)。
