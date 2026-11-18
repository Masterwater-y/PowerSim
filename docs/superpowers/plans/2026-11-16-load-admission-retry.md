# 功能 load 接收与事件重试实施清单

**Goal:** 按已有诊断设计实现独立、默认关闭的接收与发射资源候选，保留关闭回退，并完成机制、同源精度和独立吞吐验证。

**Architecture:** 保持并行两阶段及一次共享提交。在响应阶段，以功能 FU 描述建立每核有界区间日历；成功接收前完成实际发射预约，明确的 Sequencer 满拒绝可触发一次失败预约及释放事件唤醒。沿用已有请求额度、父服务、IQ/LQ 生命周期，不新增重复缓存事务。

**Tech Stack:** C++17、CMake/GCC parallel LTO、既有原生 quick 评分工具。

依据：[已授权设计](../../spr-functional-load-admission-retry-design-20261116.md)。用户已授权实施，直接执行，不另设审批或提交。

**最新范围修正：** 用户要求“先用 graph500 做验证，不要其他的”。原十源吞吐任务和汇总等待进程已停止，已完成数据保留。当前只验证 Graph500 C4/C8；复用 C4 第一组 OFF/P1/P2，C8 仅补同组 3 次回放，不继续其他负载。

- [x] 阅读项目工作流、精度合同第 0/8 节、最新吞吐合同。
- [x] 保留当前工作区未提交变更；将当前完整源复制到 `tmp/load-admission-retry-20261116/source`，冻结 M4 ELF 及源哈希。
- [x] P0：增加 `core.functional_load_admission_audit`，只观察修正后的 issue、资源冲突和接收等待；定向验证审计不改变功能人口或请求数，多负载完整中性检查随 P3 验证。
- [x] P1：增加 `include/fastsim/functional_issue_calendar.hpp`。区间跳转寻找 issue/FU/共享端口空位；使用 `op_traits.hpp` 的静态能力及 producer 的 occupancy；不同窗口保留资源状态，复制/发布同步。
- [x] 在 `src/simulator.cpp` 的 `compute_core_timing_feedback_impl` 接收之前登记候选预约。第一阶段预约是下界，响应日历拥有实际预约；候选不启用旧 `response_sparse_resource_repair`。普通完整单片段 load 进入新接收路径；特殊及缺失功能属性明确计数并保留既有路径。
- [x] P2：增加 `core.functional_load_admission` 和从属 `core.functional_load_retry`。明确的 Sequencer 满拒绝消耗失败 FU/issue 预约，直接跳到 callback 唤醒，再竞争成功预约；MSHR 等待与成功 miss 不生成重试。请求额度只在成功接受后沿既有生命周期登记。
- [x] 在 `include/fastsim/config.hpp`、`src/config.cpp`、`src/main.cpp` 加解析、互斥校验、有效配置和诊断输出。新增候选 profile，M1–M4 与 evaluation alias 保持冻结。
- [x] 定向验证：无阻塞、同时唤醒、独立执行、同线跟随、延迟依赖、跨 chunk、特殊/跨线回退、复制隔离、区间大跳转、人口及事务守恒；运行直接 `fastsim_tests`。
- [x] P3：对现有 Graph500/Zstd/XZ/FLAC/Marian C4/C8 十源运行 M4/审计/P1/P2，保持逐核人口、输入和边界；目标身份单独核验，用维护 validator 的 `--source-root`、`--stats-dir` quick 模式评分。40 回放均成功，四组各 10/10 CPI scored、failed/pending 0，身份 10/10 valid；守恒与 OFF／审计中性通过。P1/P2 时间核算相同，整体 MAE 0.176258 → 0.180104、MAPE 10.726% → 10.895%；维持默认关闭。
- [x] Graph500 首轮串行吞吐：仅 C4/C8 各一组 OFF/P1/P2，复用 C4 同组结果、C8 补 3 次，全部成功。CPU 52–63/NUMA1，记录 ELF、有效配置、实际频率、准备/warmup/仿真/必要排空及全进程 monotonic wall；计时 reader 与严格 reader 完整逐核结果／配置／功能统计一致，默认和安装 M4 身份保留。P2 两例总量／总时间速率变化 −16.871%；单组结果不宣称正式 30 对验收。
- [x] 将已验证的候选源增量集成回当前工作树，检查原文件 hash 防止覆盖同期变更；保留生产 M4 ELF、默认及候选关闭入口。再次构建并直接运行 `fastsim_tests`。九个文件与冻结候选逐字节一致；构建及两个直接测试 returncode 均为 0，见 `tmp/load-admission-retry-20261116/integration-check.json`。
- [x] 在 [Graph500 首轮验证](../../spr-functional-load-admission-retry-graph500-validation-20261116.md) 记录两例 CPI、相对／绝对误差、MAE/MAPE、机制计数、守恒与吞吐；实施报告保留已有十源精度归档。明确共享顺序未重算、PMU/hardware unavailable、候选维持关闭。

回退：`core.functional_load_admission=false`、`core.functional_load_retry=false`；审计独立默认关闭。候选关闭必须与冻结 M4 预测一致。不自动切换默认、不命名为 M5、不提交或推送。
