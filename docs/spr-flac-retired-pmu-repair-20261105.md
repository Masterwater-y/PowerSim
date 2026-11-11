# FLAC 退休缓存 PMU 修复与诊断

目标：保持退休功能输入和 FastSim 两阶段高吞吐设计，分开修复 gem5 基准观察与 FastSim 估计，不把 OoO 动态结果用作模型输入。

1. 检查现有原生样本和关联链，区分未捕获和未关联；不能证实时保留 unknown。必要时只改 gem5 OoO baseline 观察并重采。
2. 对模型 fallback 增加按宏退休合并的原因标志及与估计 miss 的交叉计数；多原因允许重叠，保留原有结果，验证 CPI、原始 PMU 和估计不变。
3. 用同一 FLAC 输入与配置回放，定位主导原因；针对已确认根因实施小范围修复。预取/合并时序近似独立评价，不用未来信息或循环收敛。
4. 用维护版 validator 比较逐事件源和人口、未知数及误差，并分别测试吞吐。最终需要零 native unknown 才能完整评分 L2/L3。

现有源的样本显示部分 L1 miss 缺少归属到该 uop 的 L2 记录，部分 L2 miss 缺少 L3 记录；缺失是观测还是关联需要进一步调查，样本是 uop 级且有数量上限，不能推断全部宏指令分布。

源码候选：`MESI_Three_Level-L1cache.sm` 的 `PF_IS, Load` 会将需求合入已有预取，只记需求 miss，不重新按需求身份发 GETS；预取 GETS 未携带需求的 Trace 身份。这可解释部分下级事件不属于需求身份的机制，但当前样本没有对应 prefetch 路径标签，不能将所有 unknown 归因于此。L3 还须核对更下一级合并和身份传播。

原始 `stats.txt` 中各 L1D（Ruby `l1_controllers`、协议 L0）控制器预取器的 `numPartialHits` 为 833/344/785/27，私有 L2（Ruby `l2_controllers`、协议 L1）分别为 27/35/10/34；源侧按 read-uop 统计的 L2+L3 unknown mask 1536 为 835/324/736/1。这些统计对象和窗口未经证明一致，数目接近只能作为“需求撞上在途预取”候选的证据，不可减数、相等归因，也不可据此将退休 L2/L3 事件归类成 hit/miss。应在该协议分支增加 baseline 专属按需求身份和行记录的 typed 观察，并验证实际服务语义；不同于将预取事务的下级 miss 无条件继承给退休需求。

## 分项回放结果

`gap-on-fallback.json` 与 `gap-on.json` 相同 FST 内容身份和模型配置，
workload-task 活跃周期均为 10007165，任务宏指令均为 31226613，退休 L1
miss 估计均为 7920。诊断添加不改变这些结果。按退休宏汇总的 fallback：

| 原因 | 宏 load | 其中 L1 miss 估计 |
|---|---:|---:|
| 无 shared-stage issue 时间，使用已有 canonical issue | 4817519 | 5377 |
| 未来 parent | 47 | 0 |
| 影子表冲突 | 3 | 3 |
| 私有层 unknown、共享层 unknown、无功能来源、无效区间 | 0 | 0 |

各原因可在同一宏中重叠；L1 miss 交叉按宏归并，不证明哪个片段导致误差。
原总 fallback 为 4817522。缺共享阶段时间不等于错误：本地 L1/L2 命中
不一定进入共享阶段，而 `shared_stage_issue_cycle` 不是通用 L1 请求起点。
因此原总 fallback 不能解释错误数量；本案仅 3 个被计为 miss 的宏直接带影子表冲突标记；这本身不足以解释
3904 的差距，也不排除冲突对其他记录的间接影响。下一步应先分别核对 L1 miss 的
模型 issue/response 窗口、同线重叠与退休资格，并独立评估自主预取。
