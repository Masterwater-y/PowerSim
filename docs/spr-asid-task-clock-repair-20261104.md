# SPR 多地址空间静态表与任务周期账修复记录

2026-11-04。两项修复已经实现，并用两组新 C4 FS 采集验证。多地址空间缺表问题已修复；严格 CPI 现在有独立的原生周期证据路径，但只对计时覆盖完整的新采集开放。旧数据不会因此变成可评分。精简来源、逐核计数和哈希见 [summary.json](spr-asid-task-clock-20261104/summary.json)。

## 修改范围

外层继续使用 `fastsim-spr`，独立 `gem5-spr/` 使用 `yinhaolang`，新子提交为 `2c8250127d0e8dc377dc544ce1101c76991044a0`。gem5 修改仅涉及采集 probe 和 `configs/fastsim/`；7,363 个已跟踪的非 probe `src/` 文件与修改前哈希完全一致，SPR 前端、分区 IQ/FU、ROB、store 和 Ruby/SLICC 实现未修改。

父仓库仍保存可获取的 `02119d1` gitlink，以 `patches/spr-gem5-source-20261103.patch` 分发完整子仓库差异；反向应用检查通过。子提交没有推送到别人的 gem5 origin。其他机器按 [仓库工作流](codebase-workflow.md) 在基线创建本地分支并应用补丁。

### ASID 静态表

新增 `.imap` v4：48 字节头、72 字节行，以 `(ASID, PC)` 为键；FST feature bit12 声明此 companion。生产器跨 CR3 保留不同地址空间的表，消费者随 `.asmap` 切换，支持 A→B→A。未知 ASID 不回退到别的进程，同一键定义冲突拒绝使用，损坏／缺失 companion 拒绝整理。格式转换保留完整 v4 表及覆盖标志。

旧 v1–v3 仍可读取，但多地址空间的 PC-only 表不能通过安全覆盖认证。新增开关 `functional_asid_static_map` 在通用 producer 中默认关闭，在新的退休流采集入口开启。

**表存在不等于完整 ISA 覆盖。** 本批表是已观察指令的部分静态表，`map_complete=false`；有效行的操作数覆盖为 true。完整退休功能流资格仍由 FFE、依赖、翻译和 `.fsem` 等独立检查约束。

### 原生与模型任务周期

新增显式契约 `task-lifecycle-basecpu-active-v1`，counter domain 为 `gem5-basecpu-active-cycles-v1`。已审计的 5.4.49 内核在两个功能边界发出 account-out/account-in：

- account-out：`0xffffffff81517a0a`，在该宏退休后关闭旧任务资格。
- account-in：`0xffffffff81067eef`，在该宏退休后开启新任务资格。

同任务调度绕过这两个切点；切换中间时段单列 transition。现有汇编 switch-entry 继续用于 RecordedScheduler 与 task PMU 人口，不能代替新计时资格。

原生 probe 只观察 BaseCPU 的 ActiveCycles/Cycles/Sleeping 通知，在 `oracle/task-clock-native-coreN.json` 保存原始更新、快照与区间。独立审计重算通知顺序、睡眠／唤醒排除、周期尾部、共同窗口、完整源哈希和逐核宏指令数。初始内核上下文保持 unknown；未观察到合法切入、任务 halt、频率变化或缺失证据都拒绝严格评分。

FastSim 从退休流与功能切点独立计算模型周期，输入计划不包含原生 tick、cache 命中结果或原生服务时间。新增按核分母检查，避免总指令数相同但核间错配。硬件入口和已证明放弃的精确缺页前缀不进入任务宏分母。

旧 owner 周期保留为 proxy。旧报告内嵌的 available 标记不能绕过当前原生证据复审。task PMU 继续使用其原有 owner 人口，门禁检查功能归属完整性，不依赖周期是否 available；两个域的指令数不能混用。

## 实测结果

两组都使用独立 SPR gem5 的 FS 模式：KVM 启动，切换 O3+Ruby，串口 WORKBEGIN 开始测量，最快核达到 user-UOP 目标后四核共同截止。内核 SHA256 为 `da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174`。每组 FastSim 配置从该组实际 `config.ini` 映射 184 项参数，启用退休前端与线程计划。

| 新采集 | 静态表与回放 | 严格任务 CPI |
|---|---|---|
| `c4-fixed-warmup-final` | 4/4 v4 表；122/122 线程事件消费；四核均有预热 | 原生独立审计 available；双方逐核分母 `[104780,31170,15585,15585]`，合计167120，unknown周期0 |
| `c4-combined-final` | 4/4 v4 表；核1、3各含2个ASID；216同步事件+747线程事件，共963/963消费 | unavailable：核1、2、3在ROI起点缺少已证明计时资格；核2、3还缺少起点原生counter观察 |

固定线程探针以 opt-in `FASTSIM_TASK_CLOCK_WARMUP` 在 WORKBEGIN 前让全部工作线程运行并发生可观察的生命周期切换，默认探针行为保持兼容。此预热提供功能证据，不向模型注入周期。OpenMP 使用原负载与采集启动方式，保留并报告未覆盖的计时起点，没有裁剪或填补。

| 可评分样本 | gem5 CPI | FastSim CPI | 绝对误差（cycles/宏指令） | 相对误差 |
|---|---:|---:|---:|---:|
| C4 固定线程预热探针 | 0.575090 | 0.640019 | 0.064929 | 11.2903% |

可评分样本数1，CPI MAE **0.064929 cycles/宏指令**，MAPE **11.2903%**。另1组明确 unavailable，CPI和误差均为null，不混入平均值。这是机制探针的当前误差，不是业务负载精度验收或精度改善对照。

两组分别通过维护入口 `run_native_kernel_fastsim_validation.py --source-root ... --stats-dir ...`，各为 expected=1/discovered=1/passed=1/failed=0/pending=0。它证明当前来源、人口、结构守恒与原生 drain 检查通过；其中一组 CPI unavailable 并不被该 passed 状态覆盖。cache 参考来自守恒的 Ruby/SLICC 原生摘要，未使用 path-class cache 字段。独立目标身份均 valid；仍未认证真机 PMU 或 CPU_CLK_UNHALTED 等价，也未重新验收吞吐。

## 验证与复用

两套模拟器均编译成功；FastSim 使用 `-j16`，gem5 使用当前亲和核数的80%。`fastsim_tests` 输出 all FastSim tests passed，新增 `fastsim_task_clock_tests` 通过。ASID读写／升级8项、原生计时/profile6项、计时审计与双时间口径46项、task PMU及维护validator28项通过；另有采集整理、特权位、producer与旧线程图等102项分组回归。分组有交叉，不把它们相加宣称独立测试总数。修复经历独立审查，内核初始资格、ROI已休眠任务、逐核人口以及内嵌报告绕过问题均补了回归。

原始数据与日志在 `tmp/spr-asid-task-clock-20261104/`；不将大体积实验文件加入仓库。早期 `c4-fixed/` 是修复前诊断采集，带 SUPERSEDED.json，不用作正式周期参考。两个最终源及原始证据保留。

新采集增加 `--thread-state --task-clock --scheduler-profile <新生成的profile>`；profile生成器需 `--task-clock` 并重新验证精确内核机器码。完成后运行：

```sh
LD_LIBRARY_PATH=/opt/gcc-11/lib64 \
/root/.pyenv/versions/3.11.14/bin/python3 tools/prepare_spr_capture.py \
  --source <采集目录>
```

请求 task-clock 而证据不全时，整理器生成可用功能输入但返回非零，状态为 `input-prepared-task-clock-unavailable`，详细原生拒绝理由在逐核 ledger 中。必须分别查看 functional_input_ready、task_clock 和 native validator 的 CPI 状态。

## 尚未解决与下一步

1. OpenMP 的严格计时需要在ROI前覆盖各核的原生counter观察和已知任务生命周期；下一轮应调整功能预热/ROI启动协议并重新采集，禁止从旧时序反推填账。
2. 5.15等其他内核尚无对应的精确机器码profile；不能复用5.4.49地址。任务halt等未支持路径继续拒绝。
3. 部分唤醒原因未知、完整跨宏融合资格、IQ分区与store/端口仲裁近似，以及原业务fixed-work布局仍需后续单独验证。这些没有通过补表或周期接口自动消失。
4. 在更长业务C4窗口上复验同人口CPI与退休load PMU。当前11.29%探针误差保留为结果，尚未归因或调参修正。
