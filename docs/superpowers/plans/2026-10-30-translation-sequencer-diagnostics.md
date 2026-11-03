# 翻译 Sequencer 修复与内存残差诊断执行计划

> Use superpowers:executing-plans; independent read-only audits use dispatching-parallel-agents.

用户已给出执行方案并授权开始；沿用现有脏工作树，保存改前快照，不提交、不覆盖旧实验，不调延迟常数。

- [x] 1. 核查 source-order 与单遍 deferred-store 分类，写真实模拟行为 RED 回归；统一分类并排除提交前 Sequencer 预分配与门控。保留 atomic/PTE/load 的正常准入，检查 callback 未重复。
- [x] 2. 实施纯观测 ROB 容量阻塞的 predecessor 指令类别、完成/退休/服务关系；验证开关不改变时序。核查现有 DRAM lifecycle 的人口，启用有效分解或补正确的控制器分类观察。
- [x] 3. 编译 -j16、运行 fastsim_tests 与动态翻译专门测试；冻结二进制和配置，重跑相同 Marian trace P1/P2，维护 validator 评分，报告绝对误差与单案 MAE，不能以预期 0.002 CPI 代替实测。
- [x] 4. 独立核查 L2 writeback/miss 定义及实际同人口计数，确认或否定 D6，先不改缓存机制。
- [x] 5. Sequencer 机制 gate 通过后新 producer 采集 G500 C16，保留旧 FST，空闲>=30GB、FST总量停止线395GB、配置明确预算/超时/进程组保护；严格覆盖与来源身份通过后同源 P1/P2 回放评分。
- [x] 6. fresh-context review task diff、修复重要问题、完成日志与结果文档。精度/机制/来源分别下结论，不做 S2/S3 或无关矩阵推广。

Review focus: deferred store 在预准入与事件门控两处同时排除；atomic 与 PTE 不被误排；partial shared-selected store 使用同一分类；ROB 分类必须指向实际释放容量的 predecessor 而非当前 younger；DRAM 排队观察需列 controller 与原始请求人口，不把重叠周期求和当作 CPI。

Ruling: 用户详细方案为本轮设计及执行授权，无额外确认。新的 G500 采集放在修复机制 gate 后，未要求 Marian CPI 必须先通过精度门槛；否则应将负面结果保留，不能用修复预期替换证据。

最终：实施、两案同源评分和源码审核完成；精度未采纳。追加发现rawstats含约1ms退出尾部，D4原生严格同窗门槛未认证；不以更换参考或调常数解决。详情见 docs/translation-sequencer-diagnostics-results-20261030.md。
