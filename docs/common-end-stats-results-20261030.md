# 共同终点 raw stats 实施与验收（2026-10-30）

## 状态与范围

本轮实施用户授权的后续 P1：给 Marian C4、G500 C16 补采与各自 FST 功能共同终点一致的原始统计。只增加观察，不修改 FastSim/gem5 的目标时序，不修改默认模型，不 commit/push，不覆盖旧 FST、checkpoint、oracle 或旧结果。

两案新来源的精确共同终点快照、维护版来源准备、独立目标身份和4/4、16/16核严格动态翻译审计均已完成并通过；两案旧源确定性复现均未通过。最终机器收据为 `tmp/common-end-stats-20261030/dynamic-retry1/completion.json`。新采集自身同窗、复现旧冻结输入、目标身份、native 来源、模型精度、硬件 PMU 等价分别判定。任何一个检查通过都不替代其他检查。

## 为什么不能只在 runner 结束时 dump

原 runner 的 Python MAX_INSTS handler 在功能共同终点后的 drain 和全局退出完成后才获得控制。原始 Marian/G500 最终 stats 的终点分别比功能终点晚 1,000,596,736 / 1,000,021,312 ticks，约 1ms。直接在 handler 中增加 dump 仍然太晚。

此外，本 producer 的 `statistics::schedStatEvent` 自身也会增加 `simQuantum`。真实最小探针验证：quantum=1000、传入 when=9000，实际 dump 在 tick10000。仅按函数形参的字面值预约也会错窗。

## 固定旧 tick 路径的失败证据

最初在完全相同的 frozen producer、原 runner、原 checkpoint 下重采，准备补偿 quantum 后按旧共同终点预约。两案到 O3 切换时已观察到与冻结运行不同的绝对 tick：

| case | 旧 switch-begin | 本轮首次重采 switch-begin |
|---|---:|---:|
| Marian C4 | 59751618817753 | 56267610727672 |
| G500 C16 | 39169622538964 | 39176633855629 |

这证明分阶段 KVM checkpoint 不足以保证旧绝对 tick 的确定性复现。首次两案已通过各自 supervisor 受控终止，保留约 2.17GB FST、日志和中止收据，没有认证其统计。不能因此修改旧 oracle 或把旧 tick 强行当作新 FST 的终点。

## 实际采用的 runner 观察方式

新增 `scripts/run_gem5_common_end_stats.py`，由 gem5 执行该包装入口，再以原始 `__file__` 和参数运行字节不变的冻结 `run_gem5_sync_probe.py`。原 runner、producer 和 checkpoint 仍执行原有完整身份/payload 校验。包装入口及辅助工具的哈希额外记录，不冒充旧 checkpoint 的创建身份。

包装入口保留原 ROI reset，在所有详细核已启用时增加 queue0 的 Python 观察事件，优先级 90，每个详细核时钟边沿检查一次。producer 在实际 first-core target 的 commit 回调中同步冻结全部参与核，并在 `finalizeCplMeasurement()` 关闭 `oracle/cpi-core*.json`。观察事件在同 tick CPU 优先级50之后检测到新文件，立即调用 `m5.stats.dump()`。这里没有通过 global exit 或 schedStatEvent，因此没有额外 quantum 位移。

运行时必须满足：

- 详细核数量与参考配置一致，全部已启用且在 queue0，时钟周期保持相同。
- 所有非零事件队列对象只能是已经通过运行时 `switchedOut()` 确认停用的 KVM CPU；其他详细核、Ruby 和内存对象都在 queue0。
- 输出目录全新；开始观察时 CPI 终点文件不存在。
- 观察事件对齐全局 CPU 时钟边沿，不能以不对齐的 reset tick 为相位直接每333tick轮询。

真实 frozen-gem5 探针验证了 reset tick1、周期10、CPU优先级50在tick30写终点标记时，观察事件恰好在tick30 dump，simTicks=29。该观察会增加主机运行成本，不是吞吐优化，也没有通过正式吞吐验收。

## 验收门禁

`tools/common_end_stats.py --actual-window` 使用新采集本身的逐核 time-accounting 和 functional-boundary，要求所有核的共同起终点一致、关闭完整，且 dump 的 `finalTick=common_end_tick`、`simTicks=common_end-common_start`。只从完整 raw stats block 提取 `stats-common-end.txt`，最终退出 dump 原样保留。

模拟事件成功码为86、cause为 `a thread reached the max instruction count`；外部进程成功码为0。这两项独立检查。失败、截断、迟到、重复同终点 dump、错 reset 或缺核均拒绝。

`--reference` 则继续严格核对旧参考：共同窗口、逐核完整功能边界、FST文件集合和完整内容哈希。旧/新 native-summary 另外逐核比较。新数据内部同窗通过，不意味着可替换旧冻结参考。

新来源还需走维护版 `prepare_native_speed_source.py` 的 native Ruby/SLICC 守恒、共同终点、任务人口及独立目标身份检查；动态翻译使用维护审计器逐核校验。单独的 snapshot 工具不承担这些来源认证；它的FST数量检查也不替代维护入口的逐核文件、头部和内容身份校验。

全局 FST政策保持400GB上限、395GB停止、至少30GB空闲，外层提前在31GB空闲停止；两案分别预留12GB/32GB FST并有辅助文件和wall-time限制。

## 已完成验证

- 默认 `cmake --build build -- -j16` 通过。
- `fastsim_tests` 直接启动时因系统libstdc++过旧而失败；仅为该测试进程设置 `LD_LIBRARY_PATH=/opt/gcc-11/lib64` 后通过，未修改全局环境。
- 新增8项统计选择、错误窗口/人口拒绝、实际事件码、真实gem5动态观察测试通过。
- 既有 checkpoint 测试8项、共同终点测试6项、存储guard测试8项、有界预热策略测试4项通过。
- 独立代码审核确认观察路径的适用约束；已修正双方同时缺失最后一个boundary核仍可能误通过的检查，以及误把模拟事件86当失败的问题。

## 两案结果

证据目录：`tmp/common-end-stats-20261030/dynamic-retry1/`。

| case | 新测量起点 | 精确dump / FST共同终点 | simTicks | 自身同窗 | 旧源复现 |
|---|---:|---:|---:|---|---|
| Marian C4 | 56066939594011 | 56070821853198 | 3882259187 | 通过 | 不通过：4/4核FST哈希、边界、native summary不同 |
| G500 C16 | 39146986623538 | 39150817557768 | 3830934230 | 通过 | 不通过：16/16核FST哈希、边界、native summary不同 |

Marian最终退出tick为56071822620430，仍比功能终点晚1,000,767,232ticks；G500最终退出tick为39151817579080，晚1,000,021,312ticks。这些退出尾部只进入最终dump，不进入提取的`stats-common-end.txt`。两案维护版来源准备和独立目标身份通过；Marian 4/4核、G500 16/16核严格动态翻译审计通过，全部核翻译unknown=0。新源没有替换旧oracle，也没有重评旧预测。

两案新快照的原生物理统计如下。这些只描述新采集人口，不与旧FastSim预测计算误差，不用其乘旧事件数解释旧CPI。

| 新采集原生物理统计 | Marian C4 | G500 C16 |
|---|---:|---:|
| DRAM read bursts | 403,451 | 715,328 |
| DRAM arrival→service均值，cycles | 40.1431 | 92.9544 |
| DRAM service→data-ready均值，cycles | 52.5285 | 52.5285 |
| Sequencer external LD响应样本 | 257,270 | 1,434,977 |
| Sequencer external LD响应均值，cycles | 136.5241 | 119.8261 |
| Sequencer external ST响应样本 | 212,354 | 652 |
| Sequencer external ST响应均值，cycles | 187.7745 | 22.9202 |

逐核旧/新测量宏指令数也不同，并非只有绝对tick平移。完整旧源确定性检查见`marian-determinism.json`、`g500-determinism.json`；两案严格旧源检查均明确拒绝。新快照可用于新来源的后续机制诊断，但要定量对照FastSim，必须先对新FST建立新的成对回放及评分，不能将新原始统计接到旧CPI表上。

508个受保护输入/工具文件共23,056,505,114bytes已重新完整哈希核验，全部一致。G500采集结束时全局FST为286,338,867,904bytes，空闲磁盘约242.50GB；未删除旧文件。

包装入口为显式使用，原默认采集入口未被改写。新命令形状为：

```bash
gem5.opt --outdir=<fresh-source> scripts/run_gem5_common_end_stats.py \
  --runner=<frozen-runner.py> --reference-source=<frozen-source> -- \
  <unchanged original runner arguments, including restore checkpoint>
```

这里`reference-source`提供已核验的核数/时钟和旧源身份比较上下文；不会把新运行强制截到旧绝对tick。新源自身验收用`tools/common_end_stats.py --actual-window --source ... --output ...`；旧源严格复现检查用`--reference ...`，结果独立。

## 解释边界

共同 tick 快照是全局物理统计：包含该 tick 的物理事件，与 FST 中已退休任务需求人口仍不同。first-core target 可以在某个CPU的commit循环中途关闭FST；优先级90快照在该tick所有CPU事件之后，因此也包含关闭之后同tick剩余的物理活动。这里认证的是精确tick窗口，不是raw simInsts/请求总数与FST功能前缀逐条相等。DRAM 的 readBursts、totQLat、totMemAccLat 在选择/服务burst时累计，可能记录已经决定的未来 readyTime；Sequencer 延迟直方图在响应回调完成时采样。不能把它们一概称为“终点前完成的请求”，也不能为了补齐响应而延长统计窗口。

同窗观察解决的是窗口偏差，不自动解决 PTE/投机/取指/任务过滤、跨边界请求、ST端点定义、FB转发等人口与语义差异。若新FST与旧输入不一致，新快照不能用于给旧FastSim预测做同源精度评分。

## 后续：新来源重放与分析

新来源的四组P1/P2重放、来源/人口评分及G500确定性复核已完成。严格三项退休miss资格为G500通过、Marian因native unknown=2拒绝；CPI使用通过的独立来源/人口评分。具体误差、DRAM/FB分析和限制见[新共同终点重放与分析报告](common-end-replay-analysis-results-20261030.md)。旧来源和旧预测保持不变。
