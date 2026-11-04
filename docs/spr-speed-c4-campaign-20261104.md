# 四个 SPECspeed 负载的 SPR C4 实验配置、环境与结果

2026-11-04 更新：四组C4采集、FastSim回放和逐案验证均已完成（4/4），
后台正在做整批复核。来源/守恒通过不等于精度达标，Marian和FLAC存在较大误差。
本文集中记录实验配置、环境、逐案结果、运行耗时、证据位置及已知限制。
最终完成状态以本批 `completion.json` 和 `native-validation/summary.json` 为准。
启动过程的固定快照保留在 [startup-verification.json](spr-speed-c4-20261104/startup-verification.json)。

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
这里的500K是身份齐备后给核0安排的指令停止预算，到期统一开启测量，并非每核
各预热500K。FST还保留切换后的身份交接前史，各核实际预热记录/宏指令数不同；
逐核边界保存在每案functional-boundary-coreN.json和manifest中。

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
验收。逐案误差及四案等权MAE/MAPE见下方实验结果，整批复核仍在运行。

## 实验环境

环境与结果快照时间：**2026-11-04T08:38:18+00:00**。宿主机信息在本轮现场读取，不能作为真机性能对照。

| 项目 | 本批实际环境 |
|---|---|
| 宿主 CPU | 2 × Intel Xeon Platinum8457C，48核/插槽，SMT2，共192逻辑CPU |
| 宿主 RAM | 2014.75GiB（约2TiB） |
| 宿主 OS | Debian10用户空间；Linux5.15.152.bsk.15-amd64内核 |
| 工具链 | Python3.11.14；GCC11.5.0；进程内显式设置GCC/Python/protobuf运行库 |
| guest | Ubuntu18.04.2LTS，Linux5.4.49；4 vCPU，32GiB，swap关闭 |
| gem5 | gem5-spr，25.1.0.1；采集配置提交afb85b808a；实际执行文件SHA256单独冻结 |
| FastSim | fastsim-spr分支；实际预测二进制SHA256单独冻结，不以之后文档提交冒充构建版本 |
| 并发 | 四案独立并发；每案24物理核及对应24 SMT逻辑CPU |

环境明细、内核/磁盘身份、工具路径及二进制哈希：[environment.json](spr-speed-c4-20261104/environment.json)。

## 实际参数与对齐范围

四案均从各自实际config.ini导入184项参数；没有用旧gem5-fs配置替代。完整配置、
每项来源与未表示项分别保留在每案replay/effective.cfg和replay/mapping.json。

| 项目 | gem5-spr实际值 / FastSim映射 |
|---|---|
| 频率 | 标称2.6GHz；gem5周期385ticks，tick频率1THz，实际约2.597403GHz；模型配置2.6GHz |
| Fetch/Decode/Rename/Dispatch/Issue/WB/Commit宽度 | 8 / 8 / 6 / 6 / 12 / 12 / 8 |
| ROB / LQ / SQ | 512 / 240 / 112 |
| IQ | 原生128/72/48三个分区；模型容量248及共享FU代理，非分区行为等价 |
| L1I / L1D | 32KiB、8路 / 48KiB、12路；64B cache line |
| L2 / LLC | 每核2MiB、16路 / 合计96MiB、12路、8 banks |
| 数据fill/TBE | 原生L1/L2 fill credit16/48；本批模型MSHR256/256按TBE代理，不是相同资源语义 |
| 内存 | DDR5_4400_4x8，32GiB，两RAM段各16控制器，共32控制器/目录 |
| DRAM队列与调度 | 原生读/写各64、FR-FCFS、close_adaptive；模型队列配置导入，调度行为仍有近似 |
| DRAM刷新 | 本批映射开启；tREFI=3,900,000ticks，tRFC=295,000ticks |
| Store | 原生sprStoreBuffer/early prepare/4-line有序窗口；模型buffer112代理，不具有完整合并/prepare等价性 |
| 前端 | 启用退休流SPR前端；无推测路径输入；跨宏融合、反压等残余近似仍保留 |

频率取整、分区IQ、store生命周期、硬件预取和MSHR/TBE等映射差异是已知限制，
不能仅因184项导入成功就称微架构完全一致，也不能把它们未经对照直接定为本批误差根因。

## 实验结果

截至快照：采集4/4、回放4/4；逐案验证完成4/4；最终四案汇总尚未生成。
全部四案目标身份、完整进程人口检查及独立task-clock可用；高层同步标记缺失仍然保留。

本表是workload-task active CPI：分子为各核属于目标任务的active周期之和，分母
为同一测量范围内目标任务实际退休的合格宏指令之和（包含任务用户态和内核态）。
预热、其他任务、idle及transition周期不作为该CPI分子，unknown必须为零。
10M目标的单位是用户UOP，不能将其当成宏指令分母；global elapsed另行计算。

| C4负载 | gem5 CPI | FastSim CPI | 绝对误差（cycles/宏指令） | 相对误差 | 状态 |
|---|---:|---:|---:|---:|---|
| 854.graph500_s | 0.652468 | 0.754040 | 0.101572 | +15.5673% | 来源/守恒通过，可评分 |
| 817.flac_s | 0.281748 | 0.500189 | 0.218440 | +77.5303% | 来源/守恒通过，可评分 |
| 872.marian_s | 1.396630 | 2.896632 | 1.500002 | +107.4016% | 来源/守恒通过，可评分 |
| 801.xz_s | 0.568770 | 0.614799 | 0.046029 | +8.0926% | 来源/守恒通过，可评分 |

| 聚合范围 | 样本数 | CPI MAE（cycles/宏指令） | CPI MAPE |
|---|---:|---:|---:|
| 全部四案逐案报告的算术汇总 | 4 | 0.466511 | 52.1480% |

此处涵盖全部四案，是维护逐案报告的等权汇总；后台整批复核仍在运行，完成收据尚未产生。

Marian和FLAC已出现较大模型误差。来源通过只说明输入、范围、人口与原生守恒合格，
不表示精度阈值验收；尚无同源消融证明具体根因。没有过滤这两个大误差样本。

| C4负载 | FST（GB，含预热） | 原生预热墙钟(s) | 原生测量墙钟(s) | 采集端到端(s) | FastSim预热/测量(s) |
|---|---:|---:|---:|---:|---:|
| 854.graph500_s | 2.736 | 42.87 | 610.84 | 813.79 | 1.39 / 14.53 |
| 817.flac_s | 2.467 | 54.46 | 727.09 | 1195.87 | 2.22 / 13.41 |
| 872.marian_s | 2.697 | 172.57 | 865.53 | 1436.01 | 4.89 / 27.58 |
| 801.xz_s | 3.416 | 469.61 | 664.26 | 1507.84 | 10.11 / 14.85 |

这些是并发环境下的运行观测，原生预热墙钟来自guard轮询，含协议交接与身份阶段，
不能直接当作纯预热指令执行耗时。没有重复/安静主机对照，不发布正式吞吐比或加速结论。
FST总量11.315GB，四案均正常共同截止，guard_reason均为空。

精简结果、逐核周期、参数、配置/计划/预测SHA256及完整验证报告快照：
[results-snapshot.json](spr-speed-c4-20261104/results-snapshot.json)。原始证据仍在tmp，
此快照不能替代后续最终四案汇总。

## 复现入口

精确四案命令在运行目录plan.json的commands字段，原始负载命令与身份在
entry-audit.json，所有已冻结工具/输入哈希在freeze.json。正式源从KVM重新启动，
不复用旧架构checkpoint；不要在原source目录重跑覆盖证据。

```bash
# 从新的实验目录按已保存命令采集；每案结束后使用维护入口准备
python3 tools/prepare_spr_capture.py --source <新case目录> --workload-label <官方编号>
# 配置映射及分段地址校验完成后回放
./build/fastsim simulate --manifest <case>/tao_trace/manifest.txt \
  --config <case>/replay/recorded.cfg --output <case>/replay/stats.json
# 使用已有预测重评；sources/stats目录应包含完整四案链接
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root <sources目录> --stats-dir <stats目录> \
  --output-dir <新的验证输出目录> --expected-cases 4 --jobs 4 --max-attempts 1
```

独立身份验证由prepare_spr_capture调用validate_fs_oracle_identity.py；完整进程范围
另由audit_process_scope检查。cache参考只来自守恒Ruby/SLICC原生摘要，禁止将
kernel-events中的path-class cache字段当原生真值。退休load的SPR硬件PMU全量分类
与本批原生tag-miss诊断不同，本批不宣称已完成硬件PMU或真机CPI等价验证。

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
