# SPR 联合预取原型进度

用户要求按完整方案实施，直接使用新 FST 比较 CPI 和退休 miss，不做 ON/OFF。

2026-11-09 当前已接入 L1 候选的下层服务回调：本核 L2 未命中可以申请现有 LLC/DRAM 预取服务；已有同核同线 L2 预取复用下层完成时间。L1 发布等待下层完成及本地返回延迟。下层 pending 仍供 demand 跟随，保留已有 CPU 等待反馈。

同时解除 L1 发起与 L2 下层服务的互斥，增加 L2 替换对待发布 L1 的取消，以及跨批次下层数据可用时间保护。构建和新四负载回放待完成。

尚未完成完整方案：跨核服务共享、独立 fill/TBE 与 demand 竞争、L1 队列资源变化驱动、L1 脏 victim 成本，以及完整父链统一身份。现有实现不能标为完整 gem5-spr 对齐。

新轨迹位于 tmp/spr-fst-recapture-20261109/local/sources。四项均返回 0。首三项 workload-task 退休 load 及 L1/L2/L3 miss unknown 均为零；Marian 退休事件统计仍待结束。

Marian 原生统计完成：无 evidence gaps；四核人口 unknown=0，三级 miss 各自的逐核 unknown 为 0/0/1/1。因此当前不能发布四负载完整退休 miss MAPE。前三项仍可按完整事件分别比较。原生诊断采样是有上限的，现存 samples 未直接包含这两个未知成员，不能据正常 samples 推断原因。

新原型构建通过，fastsim_tests 返回 0；四项回放全部返回 0。输出位于 tmp/spr-fst-recapture-20261109/local/prefetch-full-service-prototype。

维护的 compare_retired_cache 对 FLAC/XZ/Graph500 的输入身份、任务和逐核 load 人口比较通过。Marian 因两个 unknown 成员拒绝三级 miss 完整评分。

四案的 native task-clock 比较均为 scored-active，总及逐核宏指令人口、任务和起止策略匹配。

| 负载 | FastSim CPI | gem5 CPI | 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| c04-801.xz_s | 0.633876036 | 0.601607987 | 0.032268048 | +5.3636% |
| c04-817.flac_s | 0.320604466 | 0.282676444 | 0.037928022 | +13.4175% |
| c04-854.graph500_s | 0.739135590 | 0.652365235 | 0.086770355 | +13.3009% |
| c04-872.marian_s | 3.085996878 | 1.488881599 | 1.597115279 | +107.2695% |

四案 CPI MAE：0.438520426 cycles/macro。

这是新采集、当前受限原型的结果，不是历史误差修复的因果归因，也不是完整方案的验收。
