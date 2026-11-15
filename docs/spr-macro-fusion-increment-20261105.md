# SPR 退休路径跨宏融合候选组件

## 真实接线核对结论

当前不能安全地只修改前端 slot：阶段一按单 UOP 提交 source、rename、
ROB 和 IQ 后才读取后续 branch；阶段二又按 UOP 独立重建资源和反馈。
gem5 的双单 UOP 融合组共享 ROB/IQ，按成员完成情况释放并保持两个架构
宏退休。仅减少 branch 的前端槽位会使两阶段资源账不一致。

最小可实现的方向是在 producer 提交前用有限的两记录缓冲保存功能伴随，
以模型自己的 source 批次和静态事实进行原子配对，将组身份贯穿 source、
rename、分区 IQ、ROB 和退休；缺事实、异常、跨 chunk/Q 等不能证明的
情况退回逐条路径。源投递组 ID 本身不能证明两个宏在同一服务周期。
这需要真实 Simulator 对两宏退休、依赖、组成员不同完成时间、ROB/IQ
满、异常及窗口边界进行验证；目前尚未实现，也没有 CPI 或吞吐结论。
源码位置与测试范围的补充证据保存在
`tmp/spr-macro-fusion-runtime-20261105/assessment.md`。

`gem5-spr/src/cpu/o3/frontend_fetch.cc:341` 只在当前 op 是单 UOP、非 microcode、上一条在当前 fetch records 中且 producer 可用、未有 branch group、末 UOP 的 integer family 是 ALU、同地址空间/用户态、相邻 PC、同一 64B 块且 `canMacroFuseWith` 成功时组一条 allocation group。gem5 根据 decoder 源码对 family 1/2/3 分别允许 mask `0xf0fc/0xffff/0xf030`。

新增 `include/fastsim/spr_macro_fusion.hpp` 仅提供无状态三态候选判定：全部必需输入已知且各条件满足返回 Eligible；已知不满足返回 Ineligible；缺事实、无效长度/条件及 PC 溢出返回 Unknown。它消费来自功能采集的 PC/大小、ASID、特权、branch condition、producer mask、ALU 尾 UOP 等静态事实，以及 FastSim 自己推断的 source、group 和同 batch 状态；不会读取 gem5 动态融合决定。Eligible 不等价于 gem5 实际融合。组件未接入前端或更改计数。

`tests/test_spr_macro_fusion.py` 定向覆盖 PC 断续/跨 64B、ASID/特权、所有结构门、条件码、缺数据和溢出；第二项直接从 gem5-spr `static_inst.hh` 提取原生的四个 x86 静态方法，对全部 256 producer opcode × 8 ModRM.reg × 16 condition × 两种 Jcc 编码，验证提取 mask 与 `canMacroFuseWith` 一致（65536 对）。当前测试通过；只验证资格与静态 mask，不代表模型端到端精度或性能。

接入时须从 `.ffe` 验证完整宏边界/静态长度、从 `.core` 获取 `kCoreFusionKnown` 的 mask/Jcc 及上下文/特权、从 `.uops` 获取 tail `kStaticExecutionKnown` 和 integer family；无已知信息不得用 opClass 猜测。`SprFrontend` 当前 `one macro active` 且 `accepted` 后才交付下个宏，不能把退休流相邻直接视为同一 gem5 batch；需要现有两阶段有界 lookahead 的模型侧 delivery group，或明确保守估计并报告未知，不能读取 gem5 的 fetch source/时间。多 UOP producer 是否共享 allocation group 必须用模型事实和功能 group 信息建立；回滚、跨 chunk、分支、异常/宏不完整时不能破坏共享组。前端/rename/ROB/IQ/commit 的 allocation slot 必须按组共享，模型不应只减少单一前端计数；仍保留两条退休宏、各自 UOP 和 PMU population。实施后先测试 batch 内外、producer/group/microcode、多 UOP、64B、特权/ASID、缺事实与跨 chunk，再测完整 CPI/PMU/吞吐。

## 模型自己的 source-batch 标识

`SprFrontendDelivery.model_batch_id` 已按现有 `begin_macro` / `open_batch` 实现生成：新建 source batch 时递增，续用现有 batch 时保持；属于跨多个 `batch_uops` fragment 的宏输出 0（unknown），不把所有片段误报为第一批。同 batch 表示模型自己的 source-submission 分组，**不是** gem5 的 fetch batch 或实际融合，且不能单凭 ID 跨越 source/上下文变更、缺失事实、异常边界或模型反压推断原生事件。标识不改变已存在的 cycle、slot cost、IDQ、DSB 或人口；不携带 OoO 信息，measurement counter reset 保留它。

测试：新 `test_spr_frontend_batch_identity.py` 覆盖同一批多个宏、MITE batch 满、source 切换、长宏分片返回 unknown、reset_counters、复制快照、反压和模拟不同 chunk 切分；原 `test_spr_frontend.cpp` 独立编译运行通过。通过基于修改前 `HEAD` 代码与修改后代码的 6000 随机宏差分，全部 delivery cycle/slot/source/boundary/IDQ 和计数一致（`tmp/spr-frontend-neutrality.py`，临时实验而非永久测试）。尚未进行实际 Simulator 跨 chunk 或性能测试，标识也尚未被融合候选逻辑消费。

最小接入路线：先让 stage 1 的受限 lookahead 暂存一个完整 producer macro 的末 UOP 及静态事实；只有 branch 的 model_batch_id 非零且等于 producer、边界/ASID/特权/ALU/静态条件已知时才考虑融合。根据前端源投递组、rename 接受和实际后端反压定义可撤销的组归属；模型默认对不确定情况不融合并计数 unknown。跨 chunk 保留一个 bounded producer 描述，ASID/任务切换、异常、遇到新 leader、source 变化及不完整宏时安全清理；warmup reset 不误清除仍应保留的进行中组，但对跨测量起点单独记账。随后应同时携带原始两宏及 UOP 的 group identity 到 IDQ、rename、ROB/IQ/dispatch/retire 两阶段；不能只修改 `SprFrontendDelivery.slot_cost` 而仍在其他队列按两个独立 slot 计数，不能将合并视为少退休一条指令。FST 读取器不可改动其未来状态以探测下一宏，须通过功能输入缓冲或现有 chunk 描述一次顺序读取。跨 chunk、分支/中断、source 切换、`model_batch_id=0`、严重 backpressure、已融合 producer、多 UOP microfusion 与缺失静态元数据应是必要集成测试。

`IntervalTiming.spr_frontend_model_batch_id` 现已透传该 ID，关闭 SPR frontend
时保持 0；阶段一模型可以使用它，但 simulator 尚未保存或消费该值。
`tests/test_spr_frontend_integration.cpp` 增加同宏跨 UOP 保持批次、source 切换
产生新批次的真实 FST 集成断言：已通过源码语法编译；完整链接执行等待统一
构建。此字段仅是旁路元数据，不改变时间/slot 人口。

## 接入边界补充（2026-11-05）

静态资格与 model source-batch 身份不等同于两个宏在同一 source 服务周期：
实际 rename 反压可能令同一 submission 的宏分周期交付。producer 在读取
branch 前已完成预约，不能简单令 branch slot cost 为零，否则会同时改变
IDQ 与 rename 信用。可行的 frontend-only 近似需拆开 source 带宽、IDQ 与
rename 信用，只在有界预约可证实同一模型 source 服务周期时抵扣带宽；两条
后端 UOP 与退休人口保留，未知/跨周期不猜融合。当前尚未接入；这不是完整
gem5 融合或 CPI 改善证明。独立 `spr_fusion_source_reservation.hpp` 已覆盖
显式相同服务周期、带宽满、反压、缺失事实与跨 chunk 快照；实际前端需要
先提供服务周期前瞻并在交付时原子提交，组件仍未接入主路径。
