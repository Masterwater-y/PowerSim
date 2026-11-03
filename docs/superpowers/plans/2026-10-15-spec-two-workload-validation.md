# SPEC 双负载四核采集与验证执行计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 重新采集 LBM、Graph500 的四核功能 trace，以当前 FastSim 验证 CPI 和 cache PMU。

**Architecture:** 沿用 `scripts/run_gem5_sync_probe.py` 的 KVM → O3 warmup → stat → first-core common-end 流程。独立源码副本加入应用标记，当前 gem5、runtime 和配置冻结到实验目录；使用维护 validator，禁止自行选择 cache 真值。

**Tech Stack:** gem5 X86 MESI_Three_Level、GCC11、插桩 libgomp、FST7、Python3.11、FastSim。

**Spec:** 用户 2026-10-15 本会话授权，以及 `docs/accuracy-reporting-contract.md` §§0、8。

## Global Constraints

- 四核、ROB192/IQ64/LQ32/SQ114、buffer TSO114、全 LRU、32KiB L1、1MiB private L2、64MiB LLC；本地 runner 的双通道 DDR4、3GiB 内存，显式记录与历史八通道矩阵的差别。
- KVM 用于 boot/fast-forward；LBM checkpoint在首个team前；修订Graph在KVM单线程setup后的main-only/level0边界，O3创建首个四线程team；O3 warmup全部保留；WORKBEGIN 只重置统计；每核实际人口保留，首核达到 10M user-UOP 时共同结束。
- LBM 64³、双网格约84MB，明确为缩放 SPEC 源码研究输入，不冒充标准 rate 成绩。初始化和一个 timestep 预热，第二步打开 stat。
- Graph500 scale18/edgefactor16，使用同源码生成并校验长度的输入；KVM单线程完成读入/图构建/root，检查仅主线程且level0；O3重发主线程身份、恢复四线程并预热第一个BFS及验证，第二个BFS打开stat。
- FST 总量100,000,000,000字节；99GB提前停止；可用空间低于20GB提前停止；LBM单案30GB、Graph500单案35GB上限，按顺序运行。
- 不覆盖旧实验或删除当前回归源；guard 中止的输入不能当完整案例。checkpoint 事件用于切换，不额外落盘完整RAM checkpoint。
- active CPI、elapsed 分列；CPI 误差必须含 cycles/macro 绝对误差；新 perf 近似与原生诊断分列，缺资格不作硬件认证。

## Review Focus

1. 异步 KVM checkpoint 返回后提前出现 WORKBEGIN：按实际日志顺序拒绝。
2. Graph 输入文件大小与 scale 不符：构建后检查边数×16字节，拒绝旧2MB输入。
3. 预热到达容量上限：终止并保留失败收据，不截短 warmup 重命名成功。
4. 其他会话增加 FST：持续检查全局总量和磁盘可用量。
5. task/同步源不完整：审计失败保留原因，不伪造初始状态或用 elapsed 替代 active CPI。

### Task 1: 构建和有界采集

- [x] storage guard 覆盖全局 FST/可用空间/单案/进程清理，先验证拒绝行为。
- [x] 源码副本、ROI patch、编译命令、runtime/gem5/FastSim SHA256 写入实验清单。
- [x] host 运行检查 LBM 数值与 Graph500 验证通过；记录实际输入。
- [x] 顺序采集两负载，保留 checkpoint/warmup/stat/common-end 与空间收据。

### Task 2: 回放与报告

- [x] 合并 native oracle、生成 effective target、审计共同终点、编译功能 task/sync 计划。
- [x] 从实际 config.ini 派生 FastSim 配置并保存逐参数来源。
- [x] 独立 identity gate + 维护 native/perf-cache validator；缺失数据保持不可用。
- [x] 报告逐案 CPI 相对/绝对误差、elapsed、native cache 和 perf-cache 近似差异，汇总 MAE/MAPE/WAPE、FST大小和限制。

实际结论：两案native来源/人口门禁通过，精度偏差较大；可选perf-cache来源门禁两案均拒绝，未获得perf-like精度。Graph首轮BFS+完整验证warmup过长，修订仅处理setup；本批保留为ROI修订前对照。完整表见[实验报告](../../spec-two-cache-validation-20261015.md)。
