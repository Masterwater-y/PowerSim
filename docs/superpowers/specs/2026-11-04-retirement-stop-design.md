# 有限退休流的计时结束边界

用户明确批准实施：每核完成已采退休流后冻结计时，global elapsed独立取最晚完成，不按末尾任务状态继续补尾。原始采集仍使用first-core-target-common-end-v1，保留全部参与核与完整输入人口，不改负载/同步/WORKBEGIN。

## 已确认问题

c4-control-final的模型active182773，对应native106254，差76519cycles。其中核1完成36103后补25413、核2完成17156后补44360，共69773cycles计入active。旧样本尾部owner恰为idle，没有暴露这项外推。前后FastSim二进制及配置一致。

## 模型修复

TaskClock及RecordedScheduler在finish_core(core,completion)完成最后一次累计，之后所有active/other/transition/unknown计数不可再增加。finish_common只验证所有核已完成、共同终点不早于各核、关闭证据是否完整；不外推计时。未证实closure仍令complete=false，不能靠unknown=0认证。

完成位置是已保留指令真正完成退休及其必要依赖处理，沿用现有response-corrected完成点，不是读完文件或刚发射请求。计入完成前的停顿/同步，elapsed独立保持maxcompletion。JSON增加active_end_policy=retired-stream-completion-v1，并逐核输出完成时刻和四类周期，便于检查分子覆盖。

## 原生参考边界

原生现有账以common-end结束，不能无条件与新modelstop比较。为新采集补oracle-only的retirement_stop锚点：schema=gem5-retirement-stop-v1，record_ordinal为最后保留记录之后的计数，tick/cycle/active_counter/next_sequence来自现有ledger。onCommit在记录保留且功能切点已处理后更新锚点，O(1)覆盖上一个锚，不调用CPU计数更新、不改变序列/状态。

审计独立验证锚点与完整FST/fsem记录数、native epoch/更新/快照的顺序、cycle/tick频率、pending ON尾部；next_sequence允许位于后续RLE更新区间内部，不能展开巨大run。原始common-end摘要/区间仍先独立验证，再把用于CPI的native区间截到retirement_stop。保存common-end诊断与裁掉的原生尾部，elapsed仍保留共同窗口。

两端active_end_policy必须一致。缺少新锚点的旧源标为common-end-v1：同FST可检验模型补尾归零与人口/预测行为不变，但严格CPI拒绝窗口混比；不得回填原生时间、默改旧source或把简单减法作为新精度结论。新C4复采提供匹配参考。

## 验证

TDD锁定active/idle/transition/unknown各种尾部、闭合失败、重复完成、回退时间和Q16极值。真实Simulator不再把先结束核延长计时，globalelapsed及全部指令/PMU人口保持。原生测试覆盖anchor覆盖更新、Sleep/Wakeup/ONtail、RLE内部序列、target最后一条记录、畸形/缺失锚点。Python比较器拒绝旧参考/新模型混比。

同源回放旧两组C4，保存beforebinary与所有旧原件；使用维护validator和独立identity门禁。新采两组相同负载/参数的C4，实际配置映射后回放并严格评分，报告绝对CPI误差/MAE及仍未解决的OpenMP残差。并行实现各自限定文件，最终独立审查；父fastsim-spr、子yinhaolang，portable patch分发且不推子origin。
