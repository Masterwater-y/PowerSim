# Store demand 时延观测与服务修复实施计划

本计划实施用户已确认的顺序：实际 demand 时延观测 → 同源分类/服务排查 → 有证据的通用修复 → 回归与精度验证。设计依据为 store-chain-implementation-handoff-20261029.md、store-chain-implementation-results-20261029.md 及当前会话确认的口径修正。

约束：不调参、不特判；一次 canonical 分类和一次 sparse 前向；保留 NoAccess 屏障；不将完整 prepare 服务时间与剩余 demand 时延混比；不按 ≤5 cycles 认证 hit。未经要求不 commit。

1. 新建独立 store demand 观察器，使用与 gem5 非负 Histogram(10) 相同的二倍扩桶规则，支持逐核/逐 epoch 合并。记录 response−send、完整服务、角色和 canonical private 层级；短/长时延只称 duration class。增加四角色、扩桶/合并及跨 chunk 人口守恒测试，先失败再实现。
2. 在既有最终 timing 确定后采样，沿现有 accepted timing 的统计路径提交；JSON 输出样本数、精确总和、均值、桶宽和十桶。冻结观测前二进制，六项同源运行并比较全部既有语义输出；线程等待与 host wall 不用于时序中性判定。
3. 独立检查 canonical 服务及 DRAM scheduler 路径，维护 native validator 复核参考。若发现具体通用缺陷，先增加失败回归，再修复；没有证据不改延迟。
4. 同源逐项验证，CPI 必须同时给绝对/相对误差及 MAE/MAPE/WAPE/bias。未接受的机制、完整 PMU、held-out 和 throughput 单列，不把 validator passed 作为精度验收。
5. 将 gem5 冷流回归的同 line prepare/takeover 检查扩展至全部64对，独立 reviewer 检查最终变更，记录剩余边界。

恢复账本：tmp/store-demand-service-20261029/progress.md。在原工作树继续，保留所有既有修改；只备份本轮涉及源文件，不提交或移动历史。
