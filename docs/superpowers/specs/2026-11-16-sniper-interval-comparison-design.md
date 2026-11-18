# Sniper interval 同输入对照

用户要求使用 Sniper interval 跑一组验证并比较。本次把“一组”解释为现有 C4/C8 各五个保留 FST，全量执行，不改变来源、人口或 warmup。主指标继续包含输入准备，排除额外可靠性审计。ROB 和其他引擎的既有输出保持原样。

直接切换 type=interval 会丢失 FST producer-distance 依赖，且继承空 drainTimer：128 宏/129 UOP 的短输入实际预测终点为零。因此在 tmp/sniper-interval-20261116 下复制冻结 Sniper 源树，只补 interval 的功能输入接口：显式依赖、末尾排空、真实 dispatch 计数、适配现有 SPR FU traits 的 interval 资源统计。沿用上游 interval window、critical-path、cache、分支和 barrier 算法，不移植 FastSim 时序引擎。

方案选择：只改配置最快但不满足同人口执行；改用 Nehalem 解码需要不存在的原始字节且改变资源映射；选择保留现有 FST traits 和外围配置、补齐 interval 接口。interval 的 FU 资源使用已有池容量/occupancy 的窗口压力近似，不能宣称论文原始 Nehalem 配置或完整 SPR 等价。

先用短尾部、跨窗口依赖链、扩展依赖、NoAccess/serialize、末尾内存操作回归；再同一 CPU 52–63 预算逐项执行十个 case。校验逐核实际 dispatch 人口、完成终点、输入收据与配置/ELF 身份。报告分别列 interval 与原 ROB、FastSim、并行准备 zsim 的吞吐和完成时间诊断；单轮测量与保存的 gem5 审计边界保持显式。
