# SPR 退休流结束计时修复

已按用户要求实施：每核完成已保留退休流及模型已有的必要依赖处理后，立即冻结
active、inactive、transition、unknown 四类周期。共同 elapsed 仍取最晚完成核的时间，
不再把先结束核的末尾任务状态延长到该时间。完整输入人口、负载、WORKBEGIN、
同步行为以及 SPR 执行结构保持原样。

方案见[设计](superpowers/specs/2026-11-04-retirement-stop-design.md)与
[实施计划](superpowers/plans/2026-11-04-retirement-stop.md)。本文结果均为 C4 机制
探针，不能外推为业务负载、真机 perf 等价或整体精度验收。

## 修复内容

TaskClock 与 RecordedScheduler 的 `finish_core` 完成最后一次累计；
`finish_common` 只检查各核完成及关闭证据，不再补时长。关闭证据不完整仍保持
不可认证，即使 unknown 恰为零。完成点沿用模型 response-corrected retirement，
因此完成前的等待仍计入周期；读到文件末尾不代表立即结束计时。

模型输出显式 `active_end_policy=retired-stream-completion-v1`、逐核完成时刻、
四类周期及频率。比较器检查逐核周期预算：模型分类之和必须匹配本核完成时刻
及频率，原生分类之和必须匹配独立 counter delta，并核对 Sleep/Wakeup 排除量。

gem5-spr 的 probe 为每核最后一条实际保留退休记录保存 oracle-only
`retirement_stop`：记录计数、tick、cycle、active counter、next sequence。
它在功能切点处理后读取已有 ledger，只覆盖 O(1) 状态，不调用 CPU 计数更新，
不修改执行状态。独立审计先验证完整 common-end 原生账，再验证锚点与 FST/.fsem
的最后退休边界、计数更新、快照及 RLE 顺序，最后截取 CPI 参考区间。
完整原生账保留在 `common_end_diagnostic`，原生时间不进入 FastSim 模型输入。

新采集仍为 `first-core-target-common-end-v1`：所有核在共同事件关闭采集，所有
已保留记录继续处理。改变的是每核 CPI 周期分子的结束规则。旧采集缺少锚点时
保持 `common-end-v1`，与新模型混比会得到 `active-end-policy-mismatch`。
汇总也拒绝把不同结束规则合成一个 CPI MAE/MAPE。

## 同一 FST 的机制对照

原始两组数据位于 `tmp/spr-roi-bootstrap-20261104/`，本次保留旧二进制并使用
原配置、计划、manifest 重跑。完整 `cores[]` 和 `threads[]` 统计逐项一致，包含
执行周期、指令、缓存和 PMU；原件 14 项收据检查无变更。

| 原样本 | 任务宏指令 | 修复前 active cycles | 修复后 active cycles | 去掉的补尾 | 前后 elapsed cycles |
|---|---:|---:|---:|---:|---:|
| 固定线程 C4 | 174,066 | 182,773 | 113,000 | 69,773 | 61,516 / 61,516 |
| OpenMP C4 | 1,012,379 | 1,059,762 | 1,059,762 | 0 | 341,045 / 341,045 |

固定线程样本此前核1、核2分别多计 25,413 和 44,360 个 active cycles。
该对照证明补尾被移除，执行预测没有因此改变。OpenMP 没有 active 补尾，因此
这个修复没有消除其模型残差。

两组旧源均通过维护入口的来源/守恒复核，但严格 CPI 为 unavailable：旧参考没有
退休结束锚点。不能用旧参考直接相减，再把结果称作修复后的严格精度。

## 新采集的匹配边界验证

使用同一负载二进制、参数、WORKBEGIN 与同步逻辑重新采集两组 FS C4；每组从
实际 `config.ini` 映射 184 项微架构配置，独立目标身份 valid。两侧均使用
`retired-stream-completion-v1`，逐核任务宏人口一致，unknown 为零。

| 新样本 | 任务宏指令 | 计划事件消费 | gem5 CPI | FastSim CPI | 绝对误差（cycles/宏指令） | 相对误差 |
|---|---:|---:|---:|---:|---:|---:|
| 固定线程 C4 | 165,026 | 117/117 | 0.558742 | 0.638233 | 0.079491 | 14.2267% |
| OpenMP C4 | 1,013,730 | 965/965 | 0.869011 | 1.098032 | 0.229021 | 26.3541% |

| 聚合范围 | 样本数 | CPI MAE（cycles/宏指令） | CPI MAPE |
|---|---:|---:|---:|
| 上述两组匹配边界探针 | 2 | 0.154256 | 20.2904% |

本表包含全部两条新样本，没有移除尾部异常值。两次独立采集的人口不同，因此
不能把旧样本 72.02% 与新样本 14.23% 当成同源精度改善幅度。

新固定线程参考 active 为 92,207 cycles，完整 common-end 为 92,214，只裁掉
7 cycles；新 OpenMP 为 880,943 对 880,944，只裁掉 1 cycle。原生锚点用于证明
匹配结束规则，没有通过大量裁剪参考去迎合模型。

维护验证入口结果为 expected=2/discovered=2/passed=2/failed=0/pending=0，
CPI 两组均 `scored-active`。cache 真值来自守恒 Ruby/SLICC 原生摘要，未使用
path-class cache 字段。这里的 passed 认证来源、人口及守恒；没有认证精度阈值、
硬件 CPU_CLK_UNHALTED/PMU 等价或吞吐。本轮未做吞吐验收。

## 验证及复现

FastSim 默认构建、`fastsim_tests`、独立 TaskClock 测试通过；相关 Python 回归
172 项通过；原生 producer/profile/退休锚点 8 项及原有 epoch 兼容 2 项通过。
RED/GREEN 覆盖不等速核结束、四类尾部、关闭失败、原生 RLE 内部锚点及结束规则
混合聚合。独立代码审查发现的逐核周期预算门禁缺失已补回归并修复。

原始证据在 `tmp/spr-retirement-stop-20261104/`。新采集由
`capture-control-final.sh`、`capture-openmp-final.sh` 和 `validate_fresh.py` 执行。
最终统一复核使用维护入口的 `--source-root`、`--stats-dir`，输出分别在
`native-validation/` 和 `same-source-validation/`。精简结果、逐核账、来源和二进制/
配置/计划 SHA256 已保存在 [summary.json](spr-retirement-stop-20261104/summary.json)。
大体积 FST 和构建产物不加入 Git。

子项目 `gem5-spr/yinhaolang` 提交为 `9229fe5d03cb9d41963daa706796b73798a7fd1c`。
本轮只改三个 probe 文件；相对上轮 `b69ba85b05` 的 7,363 个已跟踪非 probe
源码全部不变。gem5 编译按 affinity 的 80% 使用 153 jobs 完成。
父项目继续在 `fastsim-spr`；保留可获取的 `02119d1` gitlink，以完整
`patches/spr-gem5-source-20261103.patch` 分发子项目差异，反向应用检查通过。
没有向子项目原 origin 推送。

## 后续工作

本次“回放完仍补周期”问题已修复。下一步在匹配人口及结束边界下，逐核诊断
OpenMP 的执行时序残差，再扩展真实业务 C4 验证。当前 26.35% 的 OpenMP CPI
相对误差、0.22902 cycles/宏指令的绝对误差仍需解释，不能据此声称微架构已经
完全对齐；也不能为降低误差修改退休流输入、负载同步或使用原生时长驱动模型。
