# SPR-M1 派生：MOV / zero-idiom 轻量消除

候选已经在冻结 SPR-M1 的隔离源码中实现并集成到主工作区源码。2026-11-12 用户决定默认启用，当前 SPR 默认选择 [SPR-M2](spr-m2-rename-elimination-default-20261112.md)；通用非 SPR 库默认仍关闭。11 个集成文件与已验证候选逐字节一致。完整五负载的 OFF 回放与冻结版本的全部原有目标模型字段一致；运行时耗时、线程等待和宿主驻留峰值单独排除。五负载 ON 回放也已经完成，功能输入身份及逐核宏指令人口保持一致。

候选二进制 SHA-256：`0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`。冻结 SPR-M1 的 `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905` 未改动。

实现读取退休功能流的静态 UOP、指令字节及功能生产者边。资格查询限定在当前行，不构建完整物理重命名映射、引用计数或递归别名链。GPR 只接受 REX.W 的寄存器 MOV 89/8B；XMM 只接受完整 legacy 寄存器复制；zero-idiom 需要静态策略确认且无有效源生产者。缺失、形状不符、已知 pinned、分配组及非完整宏自动回退。

MOV 作为零执行服务节点保留源生产者依赖，XMM 保守取两个 lane 的就绪时间最大值。第二阶段现有 scoreboard 将修正后的 load 回填延迟传过 MOV 链；zero-idiom 切断旧值依赖。功能 UOP、分配槽、dispatch 带宽、ROB 和按序提交仍保留，IQ、issue 宽度、FU 和普通写回不收费。scalar、materialized、inactive certificate、causal block 及跨批次 ROB 导出使用同一消除标记。

保留了两项明确近似：数据就绪下界仍使用现有 dispatch + dispatch_to_issue；动态物理 pin/别名可用性未单独建模。开启 rename_free_list 或 response_rename_feedback 时，消除候选全部回到原 PRF 路径。覆盖计数不是 native 实际消除计数。

| 负载 | gem5 CPI | SPR-M1 CPI | M1 绝对误差 | M1 相对误差 | ON CPI | ON 绝对误差 | ON 相对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 0.063733 | −5.28% | 1.155342 | 0.050708 | −4.20% |
| XZ | 0.575297 | 0.601359 | 0.026062 | +4.53% | 0.604032 | 0.028735 | +4.99% |
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.89% | 0.293530 | 0.008946 | +3.14% |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | −13.51% | 0.837365 | 0.129057 | −13.35% |
| Marian | 2.364997 | 3.068823 | 0.703826 | +29.76% | 3.044500 | 0.679503 | +28.73% |

绝对误差单位均为 cycles / retired macro。五负载等权 **CPI-only** MAE 为 0.191599 → 0.179390，MAPE 为 12.99% → 10.89%。独立活跃周期/退休宏指令口径与 SPR-M1 一致；这张表不代表主退休缓存 PMU 认证。FLAC 原有绝对周期缺口缩小 73.57%；改善包括 GPR MOV、完整 XMM MOV 和 zero-idiom，不能全部归到一个端口或单一类 MOV。

FLAC 测量期全部退休功能成员中，模型消除 GPR MOV 593,924 条、XMM MOV 3,005,232 条、zero-idiom 147,829 条，形状回退 17,776 条。Graph500 仅有 2,040 条 GPR alias，XMM alias 和 zero 为零，因此本修复没有闭合其内存等待缺口。覆盖范围含 kernel 成员，不直接对应 task-only CPI 窗口的 native 消除人口。

隔离 Release 构建使用 GCC 11、`-j16` 和 `-flto=16`。完整 `fastsim_tests` 及独立消除测试通过：覆盖冷 load→140 条 MOV 跨批次依赖、双 lane 就绪、zero 断旧依赖、宽度/策略/缺失/pin 回退、partitioned/unified IQ、普通 WB 容量、scalar/materialized 等价及实际 causal block transfer 路径。构建与测试均在项目 `tmp/` 内完成；主工作区 `build/fastsim` 现安装与该候选相同的已验证 SPR-M2 执行文件，冻结 M1 仍在 `tmp/spr-m1-20261111/fastsim`。

五个来源的有效目标身份已通过独立检查。既有 native 报告中 XZ 的 core2、Marian 的 core1 各有 1 条 workload_task 的 retired_l1_load_misses unknown membership，主 PMU 门禁失败；这些未知项必须保留为失败。候选的 maintained native 复核未完成。2026-11-12 吞吐检查期间暂停了该审计；用户随后要求停止测量并报告已有结论，剩余吞吐、native 审计及后续自动任务均已终止。该审计不再后台继续，也不能视为完成。

原短前缀试验被 `thread-state.plan` 绑定的完整人口门禁拒绝，未产生吞吐样本。后续完整输入测量在用户要求停止前，完成了 Zstd 两组冻结 SPR-M1 / 候选 OFF / 候选 ON 的串行交错；第三组仅完成 ON，其单独输出未纳入吞吐比较，XZ 尚未开始。两组均保留完整原 manifest、原功能预热与原线程计划，固定 CPU 24–35，轮换执行顺序，逐核宏指令/UOP 人口和目标时间计分结果与对应精度回放一致；输入哈希扫描在吞吐运行中关闭。

Zstd 进程吞吐 ON/OFF 变化为 **−7.12%**，两组分别 −7.27%、−6.98%；ON/冻结 M1 为 **−6.26%**。simulator 吞吐 ON/OFF 为 **−7.93%**，ON/M1 为 −7.01%。候选 OFF 相对冻结 M1 的进程吞吐为 +0.93%，少量样本不据此认定性能收益。OFF 与 ON 的进程耗时中位数为 130.696 秒和 140.720 秒。结果表明开启修复存在约 7%–8% 的可测开销；这是两组完整输入诊断，未达到 30 组稳定吞吐验收，不能推广为其他四负载的吞吐结论。精度仅 XZ 小幅退化，其余四负载的整体 CPI 绝对误差缩小。详见[停止测量后的结果报告](spr-rename-throughput-regression-20261112.md)及[数值与来源证据](spr-rename-throughput-regression-20261112.json)。

开启参数：

```ini
core.spr_rename_elimination = true
core.spr_rename_policy_sha256 = 8d6243eaa281ab5dbf0942def5823ec6ad1a82757284e8ae3a3ed7a0af1ba15b
```

将第一项改为 false 即回退。源的几何、时长和线程计划仍需由各负载的 effective config 绑定。
当前默认 evaluation alias 选择 SPR-M2，`build/fastsim` 可直接配合默认 SPR 配置运行。来源绑定的历史 SPR 配置也可在末尾 include [候选开关配置](../configs/spr-rename-elimination-candidate.cfg)。SPR-M1 的不可变配置与冻结执行文件继续保留；关闭消除开关的候选 OFF 仍与 M1 原有模型字段一致。

证据与实现：

- [隔离源码](../tmp/spr-rename-elimination-20261112/source/)
- [轻量分类器](../tmp/spr-rename-elimination-20261112/source/include/fastsim/spr_rename_elimination.hpp)
- [主工作区分类器](../include/fastsim/spr_rename_elimination.hpp)
- [源码集成身份](../tmp/spr-rename-elimination-20261112/source-identity.json)
- [可移植源码差异](../tmp/spr-rename-elimination-20261112/candidate.patch)
- [完整测试记录](../tmp/spr-rename-elimination-20261112/tests-final.log)
- [模型中性检查](../tmp/spr-rename-elimination-20261112/off-neutrality.json)
- [模型合同及近似边界](../tmp/spr-rename-elimination-20261112/model-notes.md)
- [同输入回放](../tmp/spr-rename-elimination-20261112/runs/)
