# Marian 官方 speed 采集与负载身份核对（2026-10-20）

2026-10-28 存储状态：用户已授权回收本文旧C4 Marian来源的FST及专属旁表，
配置、统计和历史报告保留，旧来源已标记`retired-not-replayable`，不能再原样回放。
当前32GiB正式矩阵另行保留，见[当前采集矩阵](spec-speed-32g-matrix-20261027.md)。
删除凭据见[回收收据](../tmp/fst-retirement-20261028/receipt.json)。

本轮最初要求为 SQLite、Zstd、Marian、CPython 各采一组 speed 数据。在核对官方负载身份后，用户明确选择：**这轮只采集 Marian 官方 speed 版**。其余三个单副本实验已停止，新生成的 FST 已按哈希收据清理，诊断和 checkpoint 记录归档。本轮正式来源集合仅包含 `c04-872.marian_s`。

## 官方负载身份核对

只读核对本机 `/data00/zhonghaocheng/benchmark/spec_cpu_2026` 的 `Docs/overview.html`、`benchspec/CPU/specspeed.bset` 和各负载 `Spec/object.pm`：

| 应用 | 官方 rate | 官方 speed | 本轮处理 |
|---|---|---|---|
| SQLite | 708.sqlite_r | 无 | 用户已排除，本轮不采集 |
| Zstd | 777.zstd_r | 无 | 用户已排除，本轮不采集 |
| CPython | 714.cpython_r | 无 | 用户已排除，本轮不采集 |
| Marian | 772.marian_r | 872.marian_s | 单副本、四物理核、四计算线程 |

前三项不能通过改名、删除 `_r`、额外加线程或直接改变 SPEC 编译宏变成官方 speed。单副本 rate 程序可以用于单任务延迟研究，但报告必须保留真实 benchmark 身份。SQLite 的官方配置含 `SQLITE_THREADSAFE=0`；CPython 含 `HAVE_PTHREAD_STUBS`；这些语义保持不变。

Marian speed 从 `772.marian_r` 的官方源码派生，但由 `872.marian_s/Spec/object.pm` 选择 speed 编译宏、输入和 `--cpu-threads`。不能拿 rate 二进制只改命令行冒充 speed。计算线程数与 `/proc` 总线程数分开记录，保留原生线程池和同步。

## 采集约束

沿用既有 checkpoint → KVM 初始化 → O3 warmup → stat 流程。checkpoint 在 OS boot 和输入 staging 后、启动应用子进程前真实保存，身份绑定 gem5、runner、载荷、输入和配置。新 runner 或二进制使用新 checkpoint。

ROI 必须位于实际计算阶段，不能重复 Zstd 结果缓冲区清零窗口的错误。Marian 初始沿用首轮 decoder `t=1` 的位置，但四线程需要逐线程到达、身份和阶段取证；rate 的单线程 gate 不能未经修改直接复用。

O3 warmup 请求预算初始为 1,000,000 instruction-stop，宿主最长180秒，KVM quantum 为10µs。正式窗口初始目标最快参与核10,000,000 measured user UOP，全核共同终点。实际人口与预热耗时另存，不把请求值当作实际值。

使用8通道、buffer TSO114及实际配置参数映射。当前 FS 平台内存上限3GiB，先做完整官方输入的宿主内存/线程预检；容量不满足时不得缩小输入或默默开启 swap。FST 总量最多100,000,000,000 bytes，99GB提前停止，磁盘至少保留20GB；顺序采集。

## 执行记录

新目录为 `tmp/spec-speed-single-task-20261020/`，原 SPEC 安装只读。已验证 Marian 源码依赖和 speed 元数据共696个发行文件与发行 MANIFEST 一致；原版和最终标记版均已由 SPEC harness 构建成功。静态 pthread 链接保留上一轮已验证的 whole-archive 修复，不改变线程算法。源码插桩见 [`spec2026-marian-speed-roi.patch`](../patches/spec2026-marian-speed-roi.patch)。

前三项没有官方 speed 的事实已反馈用户，并提出单副本替代或仅 Marian 两个选项。等待期间曾按公开说明的单副本替代假设推进准备；收到用户“仅 Marian”答复后立即停止，不能将这些尝试计入本轮成功样本。清理6个本轮 FST，共236,246,056 bytes，见 `tmp/spec-speed-single-task-20261020/cancelled-single-task-fst-retirement.json`。未删除此前有效 SQLite/Zstd 数据。

## Native speed 入口实现与验证

`scripts/run_gem5_sync_probe.py` 增加显式 `--native-workers 4 --rate-copies 1` 模式，区分物理参与核、进程副本数、原生计算线程数。既有 rate 启动器扩展 native 模式，未创建额外计算线程，也未设置 OpenMP 环境。

Marian 的现有 worker 在首次 `BeamSearch::search` 开头一次性设置亲和性，逐线程到达首个 `t=1` 后等待。启动器保持主线程和辅助线程的 CPU0 亲和性，四个计算 worker 分别固定在 CPU0..3，避免当前固定物理核模型无法认证的迁移。所有 worker ready 后切 O3；最终 `/proc` 线程名册在切换后、放行前输出。worker 执行唯一的 runtime/TID/TGID 身份标记并确认，启动器收到全部四个确认后才允许继续计算。

这个共同入口属于采集控制，只在 warmup 前执行，不能把它统计为应用自身的同步。完整输出一致证明插桩未改变结果，不证明无插桩整程序的线程交错和运行时间完全相同。本轮比较的是同一采集窗口；不以它直接发布真机 SPECspeed 分数。

身份确认的继续通道使用独立 pipe，避免某 worker 抢走另一个 worker 的首阶段放行 token。延迟一个身份标记的真实四线程测试先复现了其他线程在仅有三个身份时进入计算，增加确认通道后所有线程都必须看到四个身份，相关测试13项全部通过；独立审核复跑13项通过。项目默认 C++ build/test 也通过。本轮未修改 FastSim 模型或重编 gem5。

四个计算线程的身份不自动代表完整进程人口。采集后独立核对最终线程名册、所有原生 task-PMU owner、测量期 on-CPU owner spans、全部 syscall 元数据和后续 clone/fork。若未标记的主/辅助线程在 stat 有指令或占用时间，整体进程 scope 门禁应失败，不能将计算线程子集冒充整个进程。

原版及标记版 `872.marian_s` 均已完成官方 refspeed 第一条输入（5000句），官方 comparator 相似度均为0.9997，在0.01容差内，两个完整输出逐字节相同。原版完整宿主流程723.35秒，峰值RSS 2,976,145,408 bytes；标记版731.12秒，峰值RSS 3,071,324,160 bytes；原生总线程均6个。该时间包含完整 benchmark 和输出比较，不是 O3 warmup，未作独占宿主吞吐测量。

真实 checkpoint 及100K短恢复窗口已完成。短窗口 O3 warmup 为63.49秒，FST483,675,744 bytes。四个计算线程的身份均早于 stat；硬件身份、共同终点、完整 syscall 元数据、固定核所有权和进程人口审计通过。未标记的主/辅助线程在该测量窗口无退休UOP、无on-CPU owner ticks，unknown cycles 为0。这些是本窗口的证据，正式窗口必须独立重新审计。

宿主完整输出预检不证明整个 benchmark 能在3GiB guest中运行到结束。本轮采集首个 decoder 阶段的有界 ROI；只对实际 guest 采集窗口检查无OOM及无swap，不将它冒充完整 SPECspeed 成绩或预热收敛认证。

## 正式采集结果

正式 `c04-872.marian_s` 已完成并通过独立硬件身份、共同终点、任务所有权与进程人口审计。配置为四物理核、一个 speed 应用进程、四个计算 worker（应用总OS线程六个）、8通道、3GiB、buffer TSO114。保留 user+kernel 指令和 warmup 前史，没有未追踪的额外OS核。

| 项目 | 实测值 |
|---|---:|
| O3 warmup 宿主时间 | 63.702361 秒 |
| core0 实际 warmup 宏指令 | 1,003,539 |
| 恢复、KVM初始化、预热和正式采集合计宿主时间 | 569.866462 秒 |
| 正式 FST，四文件合计 | 2,977,506,080 bytes（2.978 GB） |
| stat user UOP，core0 / 1 / 2 / 3 | 9,739,973 / 9,755,757 / 9,751,413 / 10,000,002 |
| stat user UOP 合计 | 39,247,145 |
| workload-task 宏指令分母 | 20,982,089 |
| unknown owner cycles | 0 |

core3 触发唯一共同终点；保留真实结束人口，不独立裁剪其他核。主线程和辅助线程在正式窗口均为0退休UOP、0占用tick，四个标记 worker 覆盖本窗口的完整应用进程测量人口。

短窗口主要在 `CopyRows`/`memmove`；正式窗口已进入后续张量计算。逐核物理 user UOP 中，两类SIMD逐元素张量算子合计占50.95%–52.31%，`memmove`合计约20.08%–20.20%，`memset`仅0.0076%–0.0092%。这证明正式窗口不再仅覆盖最初状态拷贝；不将这些比例外推为完整翻译或所有decoder步骤的占比。函数归属由真实FST PC和二进制符号表核对；debug伴随文件的映射段仅有strip-debug导致的非执行ELF section-table元数据变化，执行代码一致，证据见 `roi-phase-profile.json`。

正式期记录2次wakeup、4次sched_switch，均涉及CPU0上的后台 `rcu_sched`（PID10）及计算 worker 的抢占/恢复。没有在本窗口观察到应用计算线程的阻塞等待；因此该案验证四线程并发解码窗口，不能单独作为高频futex/屏障同步模型的认证。

## FastSim 回放结果与资格边界

通过维护入口 `tools/run_native_kernel_fastsim_validation.py --retired-cache` 串行回放：expected/discovered/passed均为1，failed/pending均为0，native PMU scored为1。缓存参考来自守恒的Ruby/SLICC native摘要，未使用path-class cache代替。另有独立identity审计1案、0不匹配。

模型和gem5二进制均未在本轮改动。回放沿用当前已验证的 `core.response_branch_recovery=true`，其余暴露的DRAM字段从本案实际 `config.ini` 映射；具体包含链和映射见下方配置及 `parameter-provenance.json`。`passed`只表示来源、回放和人口门禁通过，不代表误差达到某个阈值或对齐真机perf。

| gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---:|---:|---:|---:|
| 2.665784 | 3.175938 | +19.14% | 0.510154 |

本轮1案 CPI MAE 为 **0.510154 cycles/macro**，双方宏指令人口一致。gem5 elapsed为4.657544 ms，FastSim为5.548210 ms，误差+19.12%，绝对差0.890666 ms。CPI使用task active cycles；elapsed不跨核相加。

以下是维护报告中的退休load cache已知子集估计，**不是完整真机perf误差**；native与model仍有大量未确定统计资格的人口。unknown不能直接加进miss，也不能假定两侧已知集合完全相同。

| 事件 | gem5已知计数 | FastSim已知计数 | 暂定相对差 | native unknown | model unknown |
|---|---:|---:|---:|---:|---:|
| retired_l1_load_misses | 54,116 | 259,433 | +379.40% | 2,610,578 | 3,051,844 |
| retired_l2_load_misses | 15,690 | 203,021 | +1193.95% | 2,988,501 | 3,051,845 |
| retired_l3_load_misses | 13,665 | 200,799 | +1369.44% | 2,988,609 | 3,051,895 |

退休load/store总数则分别为6,346,648及3,066,935，双方原始总数一致。完整hit/FB-hit及native tag miss诊断保留在维护报告中，不能与上述退休load事件互换。当前不能把该组PMU差值称为完整总体精度或硬件认证，本轮未据这些不完整子集直接调整模型参数。

此外，初始PTE快照仍来自launcher地址空间，而应用四个worker共享另一个CR3；源日志明确保留non-target CR3及PTE unknown。完整功能指令和ASID采集、owner守恒与初始页表资格是不同条件，不能用前者通过宣称后者已补齐。DRAM refresh及紧凑Ruby/网络时序等既有近似随参数映射收据披露。

## 复用入口与空间

- [正式trace及manifest](../tmp/spec-speed-single-task-20261020/source/c04-872.marian_s/tao_trace/manifest.txt)、[采集命令](../tmp/spec-speed-single-task-20261020/campaign-commands.json)、[checkpoint身份收据](../tmp/spec-speed-single-task-20261020/checkpoints/c04-872.marian_s/fastsim-checkpoint.json)。恢复必须使用命令中冻结的runner和launcher，不能拿后来变动的维护脚本绕过身份核验。
- [完成收据](../tmp/spec-speed-single-task-20261020/completion.json)、[实际参数映射](../tmp/spec-speed-single-task-20261020/source/c04-872.marian_s/parameter-provenance.json)、[回放配置](../tmp/spec-speed-single-task-20261020/validation-configs/c04-872.marian_s.cfg)。
- [维护CPI/elapsed/native诊断汇总](../tmp/spec-speed-single-task-20261020/validation/c04-872.marian_s/summary.md)、[退休cache计数和unknown](../tmp/spec-speed-single-task-20261020/validation/c04-872.marian_s/retired-cache-summary.md)。

最终全项目 FST约32.98GB，磁盘空闲约350GiB，低于100GB上限并保留20GB以上余量。取消的三个串行实验FST已清理；有效旧回归源保留。checkpoint、配置、输入身份和失败诊断不因清理FST而伪造或改写。
