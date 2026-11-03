# 近似 PMU 预测与 private-L2 漏计修复

2026-10-15。本轮实施用户批准的首批修复：发布可用近似预测，分离参考可信度与
硬件认证，修复已经确认的 private-L2 data-demand 漏计，并补功能输入内容身份。
保留两阶段模型，没有调整任何目标时序参数，没有采集或复制大型 FST。

## 1. 预测不再依赖完整认证

`scope_metrics.perf_cache` 每事件新增：

- `estimated_value`、`estimate_status`：可用近似预测与其状态。
- `estimate_policy=accepted-model-events-zero-unmodeled-v1`：当前已接受模型事件
  构成预测，未建模请求贡献暂按零处理；这不是完整性声明，也不是上下界。
- `assumptions`：宏成员/ISA、来源、遗漏请求、窗口、LLC与writeback等具体近似。
- `missing_components`：精确保留该事件 coverage 中为 false 的能力。

严格 `value`、coverage 和 `hardware_validated` 保持独立。完整兼容参考可以
评价近似模型，无需先认证模型与硬件等价；不完整的 native 参考只输出诊断差异。
两者分别聚合，不能把诊断差异列改名为 perf MAPE。

物理范围可给请求近似预测。当前缺少 event-clock owner/CPL 映射，因此
user/kernel/workload_task/other_task 的请求预测为 null/unavailable，保留其
诊断值与原因，避免将未知归属误报成任务零请求。退休事件仍按自身范围给预测。
非 interval_weave 模型也不发布陈旧或零值预测。

实现：[报告](../include/fastsim/perf_cache_report.hpp)、
[合同与估计比较](../tools/perf_cache_contract.py)、
[维护入口](../tools/run_native_kernel_fastsim_validation.py)。

## 2. 功能输入身份和窗口限制

新增 [C++内容身份](../include/fastsim/perf_cache_source.hpp)和
[独立Python重算](../tools/perf_cache_source.py)。身份按实际 FST、使用的
deps/vmap/asmap/imap/ifmap、边界内存状态、thread-state plan 内容哈希，以及
源核、源记录数量、原始半开 ordinal 范围构成。路径不参与身份；同数量不同内容
或 companion 改变会失效，要求的依赖缺失会拒绝。

CLI 使用 `--perf-cache-input-identity true`，在模拟前后独立计算并检查内容身份，
通过顶层 `functional_source_identity` 导出。完整哈希额外成本单列为
`input_identity_wall_seconds`，本次约1.3秒/四核输入，不能隐藏成免费启动成本。
该选项默认关闭；模拟器已有 measured-user-UOP/s 计时不包含这两次校验。

维护审计器再次重算当前输入，并核对 native 逐核原始 ordinal 边界和宏人口。
隐式 instruction slice 缺少精确 ordinal 时维持 incomplete，不放行估计比较。

**内容身份不证明完整宏成员或实际请求计数边沿。** 当前
`roi_edges=false`、`macro_boundary_complete=false`；源序列窗口与请求实际边沿的
差别继续明确标为近似，仅进入诊断比较。没有将 gem5 tick、PMU 或实际 miss
结果作为 FastSim 输入。前后哈希能检测正常输入变化，未宣称提供不可变文件系统
或检测变化后原样恢复的形式保证。

## 3. private-L2 已确认漏计的生产修复

新增 `CoreCounters.l2_data_demand_tag_misses`，根据本层 private-L2 lookup 在
接受回放路径计数。后续 remote supply 或共享 LLC merge 不会取消本层已发生
的 tag miss；code/PTE、buffer prepare、权限升级、本地合并和重复 carry 排除。
新计数随原有 CoreCounters 快照、回滚及聚合提交。

physical/user/task/other/per-core 的 native `private_l2_tag_misses` 统一改用
新计数，旧 `l2_read_tag_misses` 保留为窄诊断。它不替代范围更广的
`perf_l2_demand_misses`，也未改变缓存状态、服务或时序决策。

输入是同一四核探针的三次独立采集，均参数 `1000 2000`、first-core
300000 measured user-UOP共同结束、功能warmup、buffer TSO114；不是三个负载。
通过独立目标身份门禁3/3与维护 native 来源/人口门禁3/3。

| 输入 | gem5 native TASK L2 | 修复前 | 修复后 | 修复前 signed error | 修复后 signed error |
|---|---:|---:|---:|---:|---:|
| fs-probe-rom-gdb | 287 | 44 | 311 | −84.6690% | +8.3624% |
| fs-probe-task-pmu | 290 | 61 | 331 | −78.9655% | +14.1379% |
| fs-probe-perf-cache-v2-coremap | 291 | 63 | 329 | −78.3505% | +13.0584% |

三例 native private-L2 MAPE **80.6617% → 11.8529%**，WAPE
**80.6452% → 11.8664%**，max APE **84.6690% → 14.1379%**。
这是本层 data-demand 原生诊断修复，不能称为真机 `L2_RQSTS.ALL_DEMAND_MISS`
精度。没有推广到历史32/10/40案。

## 4. 三个重点 perf 字段的近似输出

仅 `fs-probe-perf-cache-v2-coremap` 具有本轮所需的新原生请求账本。相同内容和
原始记录前缀已独立核验；以下是 **physical scope，预测对不完整原生诊断**。

| 字段 | FastSim近似预测 | gem5诊断 | signed difference % | 状态 |
|---|---:|---:|---:|---|
| `perf_l1d_load_misses` | 346 | 550 | −37.0909% | diagnostic_only |
| `perf_l2_demand_misses` | 712 | 817 | −12.8519% | diagnostic_only |
| `perf_llc_misses_core` | 77 | 97 | −20.6186% | diagnostic_only |

这三项当前都已提供数值及假设，不再因“未认证”隐藏预测。但参考的 ISA/source
资格、协议生成的prepare/prefetch身份、LLC数据映射，以及请求窗口边沿仍不完整，
所以这些百分比不是 perf 准确率。完整参考下的 `scored_estimate` 路径已经有
回归验证，不以虚假的覆盖标志把真实案例升级成完整参考。

## 5. 时序不变性、CPI 和 elapsed

三份冻结输入与本轮前保存的二进制重新回放对照，所有旧字段中仅51个
native `private_l2_tag_misses` 位置按预期变化。排除主机计时/吞吐与异步驻留
状态后，无其他差异；CPI、elapsed、各层缓存行为和旧窄桶均不变。

范围为 workload-task / task-oncpu-switch-entry-v1，硬件perf等价性未认证。

| 输入 | gem5 active CPI | FastSim active CPI | signed error | 绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| fs-probe-rom-gdb | 0.819819 | 0.864942 | +5.5040% | 0.045123 |
| fs-probe-task-pmu | 0.804791 | 0.898957 | +11.7006% | 0.094166 |
| fs-probe-perf-cache-v2-coremap | 0.799111 | 0.899643 | +12.5805% | 0.100532 |

CPI MAE=0.079940 cycles/macro。elapsed依次为 gem5/FastSim：
42.653637/34.405227µs、42.526764/34.439193µs、41.987637/33.860106µs。
本轮没有改善时序预测，不将计数修正宣称为 CPI 收益。

## 6. 测试、审查与证据

默认CMake构建和直接 `fastsim_tests` 通过；退休、请求runtime和integration
三个专门C++可执行文件通过。L2机制覆盖冷写、remote、LLC合并、本地合并、权限
升级、code/PTE、prepare/head、rollback和carry。

功能内容身份18项测试含SHA256已知向量及C++/Python对拍。全Python发现405项，
5项skip、3项既有环境错误：缺 `fastsim_py`；两个checkpoint recovery测试缺
外部TCSim配置。没有新的断言失败。专项近似报告、实际CLI、内容身份、维护验证
和原生来源测试均有通过证据。

独立代码审核发现非physical未知上下文仍发布0近似值的问题，已补真实serializer
失败回归后修为null/unavailable，并重新构建及运行全suite；其余范围无新增
Critical/Important问题。本批没有完整错误路径/预取provider，也未完成请求ROI
和首尾宏成员新协议；不将这些后续接口宣称为已实现。

证据根：[本轮实验](../tmp/perf-cache-estimates-20261015/)。

- [目标身份](../tmp/perf-cache-estimates-20261015/identity-validation.json)
- [native修复验证](../tmp/perf-cache-estimates-20261015/validation-final/summary.json)
- [近似预测与参考差异](../tmp/perf-cache-estimates-20261015/validation-estimates-final/perf-cache-estimates.md)
- [逐字段不变性检查](../tmp/perf-cache-estimates-20261015/invariance.json)
- [最终C++测试](../tmp/perf-cache-estimates-20261015/cpp-review-final.log)
- [全Python测试](../tmp/perf-cache-estimates-20261015/python-full-final.log)

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/perf-cache-threeway-20261015/accepted-source \
  --stats-dir tmp/perf-cache-estimates-20261015/stats-final \
  --output-dir tmp/perf-cache-estimates-20261015/validation-estimates-final \
  --expected-cases 1 --jobs 1 --perf-cache --perf-cache-estimates
```

本轮未编译gem5或新增采集，所有生产FST复用原文件；20GB上限继续有效。

## 7. 同源吞吐开销

所有本项目编译、测试、采集和审计结束后，对旧主输入做5组前后交替执行，
固定核0–7，核对二进制、FST、manifest、配置include和owner计划哈希。

| 版本 | measured user-UOP/s中位数 | 均值 | CLI进程wall中位数 |
|---|---:|---:|---:|
| 本轮前 | 3.808286M | 3.831577M | 0.650148s |
| 本轮后 | 3.872327M | 3.865366M | 0.649516s |

中位数变化+1.6816%。这是共享主机短窗口的开销诊断，
不能据此声称稳定提速或大型负载性能。该对照关闭可选的完整输入身份哈希；
启用校验时需另加本次约1.3秒的前后全内容哈希成本，不能用3.87M模拟吞吐
冒充包含该校验的端到端吞吐。完整provider的未来开销没有在本轮测量。

[吞吐原始样本、命令和指纹](../tmp/perf-cache-estimates-20261015/performance-quiet/summary.json)。
