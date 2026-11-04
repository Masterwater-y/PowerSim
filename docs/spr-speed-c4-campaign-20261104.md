# 四个 SPECspeed 负载的 SPR C4 并行采集

2026-11-04 已启动四路正式采集，管理进程 PID **76871**。当前属于运行启动记录，
不是四案完成或精度通过报告。最终状态以本批 `campaign-status.json`、
`completion.json` 和维护 validator 输出为准。

| 负载 | 官方身份 | 原生任务 | 输入 | FST 上限 |
|---|---|---:|---|---:|
| Marian | `872.marian_s` | 4 计算 worker，进程其余线程单独审计 | 原 refspeed 命令/模型/词表 | 8 GB |
| Graph500 | `854.graph500_s` | 4 OpenMP 线程 | 原 refspeed APSP 命令 | 8 GB |
| XZ | `801.xz_s` | 4 压缩 worker＋1 生产者 | 当前 32GiB 矩阵的 refspeed 命令，非旧 train 探针 | 10 GB |
| FLAC | `817.flac_s` | 主线程＋3 worker | 原 refspeed 编码命令；先在 KVM 解码准备 | 14 GB |

二进制、输入和启动器直接复用已核对身份的
`tmp/spec-speed-32g-c4-c8-c16-20261027-final/cases.json` 四个 C4 条目，
不是把 rate 改名为 speed。它们是带 ROI/身份标记的研究二进制，不是正式 SPEC 成绩。
旧 gem5-fs checkpoint 和预测结果不用于新 SPR 采集。

## 配置与边界

每案使用四个原生 SPR O3 核，全核保留用户态＋内核态退休流，身份齐备后预热
500K 指令；首核达到 10M 用户 UOP 后共同截止。FastSim 使用完整已采人口，
逐核周期冻结于退休流完成，匹配独立审计的原生 retirement_stop 锚点。

现有入口 2GiB 容量不足以运行 Marian/XZ。新增显式 32GiB DDR5 大内存路径，
保持原生 DDR5_4400_4x8 时序、SPR CPU/cache 参数和默认 2GiB 路径。复用已有
分段 RAM/E820 算法，每段16通道，两段共32个控制器/目录；不能称为总16控制器。
物理 RAM 为 `[0,3GiB)` 和 `[4GiB,33GiB)`。配置实例化核验了范围、E820、
32控制器/32目录以及原生 ROB512、IQ128/72/48、LLC96MiB。

guest 在业务启动前输出内存可见性证据并检查 MemTotal 至少31GiB、高地址
System RAM 存在。该检查认证容量可见性，不是任意高地址访问/硬件等价认证。
映射从每案实际 config.ini 生成，额外使用维护生成器验证分段地址布局；FastSim
采用已有 `x86-low3g-high4g-v1` 解码，不将低段16通道错误地按32连续通道映射。

## 已知同步证据缺口

四个原业务二进制都没有 `fastsim_sync_marker`；Graph500 链接普通静态 libgomp，
没有完整高层 OpenMP barrier/并行区语义标记。因此本批不打开功能同步标记协议，
不添加空标记冒充证据。实际原生线程、锁、futex 和调度行为保持，启用独立
thread-state、task-clock 与原生任务身份；模型只能使用已采的功能事件。

内核切换/等待/唤醒证据与高层同步因果完整是不同条件。即使计时来源和 CPI
人口检查通过，也不能称高层同步已经完整对齐。逐案审计缺失或失败会明确保留，
不静默省略。另检查完整进程线程名册，不能把 Marian 四计算线程冒充完整进程，
也不能删掉 XZ 生产者。保留的 owner proxy 只证明人口覆盖，不作 active CPI 真值。

## 并发、容量与自动验证

四条流水线同时运行；每条分配24个物理核及对应24个 SMT 逻辑CPU，合计覆盖
192逻辑CPU。gem5 O3 详细阶段主要按单事件队列执行，宽 CPU 掩码不会使一个
case 获得48倍加速；本批可用的负载级并行是四路。每案结束即审计、映射、回放和
验证，其余采集继续；所有合格来源再以维护入口聚合复核 expected=4。

启动时全局 FST 约294.85GB，实际空闲约93GB。四案预留40GB FST、14GB旁表，
保留原400GB总上限/395GB停止和30GB实际空闲底线，额外35GB早停哨兵只终止
本批拥有的子进程。失败/截断源保留，不评分为正常样本，不删除旧源。

自动流程为 `prepare_spr_capture.py` → 完整进程范围审计 → 实际参数映射 →
ASID静态表审计 → FastSim回放 → `run_native_kernel_fastsim_validation.py`。
维护 validator 的 passed 只表示来源/守恒，不代表 CPI误差阈值、硬件 PMU 或吞吐
验收。最终准确率必须同时报告 CPI绝对误差和 MAE；目前尚无本批误差结果。

## 运行与代码身份

运行目录：`tmp/spr-speed-c4-20261104/`。

- `launch.json`：管理进程、启动命令与日志。
- `plan.json`、`freeze.json`、`entry-audit.json`：精确命令、输入/工具哈希及入口核验。
- `campaign-status.json`：实时阶段、各进程PID、容量、失败原因和已知缺口。
- `source/<case>/capture-status.json`、`capture-phases.jsonl`、`capture-receipt.json`：原生进度和完成收据。
- `per-case/<case>/validation/`：每案维护验证输出；`native-validation/`：最终四案汇总。
- `completion.json`：管理进程退出时的完整四案状态，不能只看存在的成功目录。

入口适配已完成14项回归；来源准备22项、进程覆盖9项、既有调度5项通过，共50项。
实际 2GiB/32GiB 配置实例化通过，独立代码审查无遗留 P1/P2 问题。
精简启动证据见 [launch-summary.json](spr-speed-c4-20261104/launch-summary.json)，
这是固定时间快照，后续运行状态须读取上述实时文件。
子仓库 `gem5-spr/yinhaolang` 配置提交为
`afb85b808a058e19bc815e034c9d2aaa57f14678`，本轮非 probe src 完全未改。
父仓库保留可获取的 `02119d1` gitlink，以完整 portable patch 分发，没有推送子 origin。

方案和计划：[设计](superpowers/specs/2026-11-04-spr-speed-c4-design.md)、
[执行计划](superpowers/plans/2026-11-04-spr-speed-c4.md)。
