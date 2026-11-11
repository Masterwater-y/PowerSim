# R0/R1 退休 cache miss 口径核验

2026-11-11。读取当前实现、维护验证器、官方事件表快照与既有真机微内核记录。
本轮未修改预测、冻结源码、模型或 gem5 二进制，未产生新的硬件测量。

主要结论：当前比较的单位、主要字段选择和同源人口已有约束，但服务到硬件事件的
语义映射仍未认证；尤其不能把 L1 PendingFill 一律归入 `MEM_LOAD_RETIRED.L1_MISS`
当作已证实的 SPR 硬件规则。

## 已核对的计算与人口口径

| 项目 | 当前实现 | 判断 |
|---|---|---|
| 单位 | 每个合格退休 load 宏指令，每层按 fragment/UOP miss 位 OR | 当前未发现逐缓存行或逐 UOP 直接相加替代宏计数 |
| 范围 | 四核 `workload_task`，维护入口核验同源身份、task 和逐核人口 | 原五例既有对照通过人口核验；新源须独立完成 |
| 模型主预测 | 模型事件的 `estimated_count` | 是完整模型点估计，不能与模型旧资格 `count` 混用 |
| 原生参考 | native 事件的 `count = exact_count + estimated_count` | 当前维护验证器字段选择正确；native 的 `estimated_count` 只是 certainty 子桶 |
| 请求 tag miss | 独立 Ruby/SLICC data-demand 请求人口 | 不能作为退休 load miss，也不能直接相减构造遗漏 load |
| 配对样本 | stride 257；目前排除 split、atomic 等不适合逐行比较的成员 | 仅诊断子集，不能代替覆盖这些成员的全量宏指标验收 |

字段选择见 [维护验证器](tools/retired_cache_contract.py#L175-L210)。
模型宏归并见 [退休账本](include/fastsim/perf_retirement.hpp#L95-L154)。
本次没有重新宣称已核验所有特殊 ISA 与真机事件的资格等价。

## 优先级最高的语义疑点：FB 与 L1 miss

当前 [共享解释器](gem5-spr/src/mem/spr_retired_load_miss.hh#L50-L103) 对
`Level::PendingFill` 置 miss 位；L1 PendingFill 同时排除下层。
[配置](configs/spr-retired-load-miss-v2.json#L13-L22) 明确给非驻留 L1 follower
`miss_mask = 1`。FastSim 影子观察器与 gem5 原生服务投影均使用这套规则。

项目 [既有真机微内核记录](docs/perf-pmu-semantic-audit-20261014.md#L73-L105)
在 SPR Xeon 8457C、用户态、无 multiplex 的测量中报告：

| flush 后两条同线 load，每轮 | 计数 |
|---|---:|
| ALL_LOADS | 2.000950 |
| L1_MISS | 0.998660 |
| FB_HIT | 1.000290 |

这条反例不支持“FB follower 必然额外产生一次 L1_MISS”的通用映射。
它也不证明所有 ISA、所有 split/retry 或所有服务条件中这两个事件都互斥；
不能据此直接删除全量 follower，或用 `L1_MISS - FB_HIT` 回算新参考。
应独立重现并确认目标 CPU/指令/数据来源条件，再确定新的事件映射。

官方事件快照对 L1_MISS 使用“至少一个 UOP missed in L1”的描述，FB_HIT 另有事件，
单靠文字不足以证明二者包含关系。快照位于
`spr-framework/configs/gem5/events/sapphirerapids_core.json`。

## 原生参考并非全量硬件精确标签

`native` 表示底层服务事实来自独立 gem5 原生观察；最终退休事件仍经过共享解释器。
`exact/estimated` 是该解释器内部的 certainty 分类，不代表已经完成 PEBS/真机等价认证。
当前 [策略](configs/spr-retired-load-miss-v2.json#L1-L11) 显式声明
`hardware_equivalence = unverified`。

既有原五例中，Graph500 native L1 reference 的 estimated 占 88.66%，FLAC 占 91.50%。
所以对照这些参考可以评价内部规则下的模型成员预测，不能直接宣称是真机退休 miss 精度。
共享同一规则会使两端具有共同的语义偏差；重新运行相同 gem5 解释器不会自动消除它。

权限、远端供给、软件预取统一三层负成员、L2/LLC follower、特殊 ISA 和 retry sticky
规则也需要各自的硬件资格证据。已有热 `prefetcht0` 微内核不能认证所有软件预取形式
和冷热路径都必然为三层非 miss。上述为待核验项，不是本轮已证实的逐负载贡献。

## unknown 与既有误差解释

用户授权保留 XZ/Marian 极少量 native unknown 的暂定差值；三个事件各有一个未知标记，
可能来自同一宏，不能当作六条独立未知 load。既有区间检查中最大相对误差移动量约
0.009079 个百分点，无法解释主要差距，但正式全分类门禁失败状态必须保留。

此前原五例误差算术和计数继续保留，含义限定为：
**同源 workload_task 宏人口中，冻结模型对 gem5 `spr-retired-load-miss-v2`
投影参考的暂定误差。**
本次纠正证据解释，不是误差下降、预测改善或新吞吐测量。

此前将 Graph500/XZ 差额主要定位在模型回填成员扩展，对内部策略仍有诊断价值；
不能将该定位直接转为“应该把更多 follower 补进真机 L1_MISS”的修复目标。
后续修复先核实服务到事件的映射，再分别验收模型 fill 判定与功能请求路径。
