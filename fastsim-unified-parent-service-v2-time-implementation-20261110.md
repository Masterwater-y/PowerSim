# FastSim v2：实际准入时间与 prepare 运行时接入

日期：2026-11-10。实施目录：`tmp/parent-service-v2-time-exclusive-20261110/source/`。

**完成状态纠正：当前完成并集成的是 v2 私有侧等待、prepare/store 和资源准入修复，整体统一父服务方案尚未完成。** 用户要求完整修复后，不能继续以旧分阶段边界将未实现能力排除在完成状态之外。

用户要求停止后台吞吐配对、立即接入下一步完整修复。本轮不继续吞吐门禁，不启动 native 审计、CPI/PMU 评分或批量回放。原 R0/R1 与旧 v2 副本保持冻结；完成隔离验证后，15 个源码/测试/构建文件已集成至主目录，未执行 Git 提交或推送。

## 本次接入

- 共享阶段随原反馈槽输出固定大小 `ServiceTimeModel::Descriptor`：实际功能动作、事务身份、内容/权限身份、配置派生的服务分量，以及窗口前冻结的 CHA/DRAM 端口前沿。
- 在已有一次时序反馈中，按实际准入重新计算 Q16 数据就绪、权限就绪及控制器恢复边。新路径不读旧绝对响应、旧选定延迟或旧 stall 来计算这些边；输出保留 `Estimated` 证据类型。
- 支持 I→GETS、I→GETX 和 S→UPGRADE。UPGRADE 保留已有数据，不分配数据 fill 槽。E/M 无下层事务时使用本地写入与发布路径。
- Store 接管同一父服务 ID，并等待数据、权限及控制器边后发布。可选 prepare 信用释放独立于 TBE/fill 释放；信用不足时仍保留 mandatory store 请求。
- `v2-wait` 下 L1/L2 容量统一由父服务拥有：四种资源为 L1 TBE、L1 fill、L2 TBE、L2 fill；各层 TBE 按各自控制器边释放，fill 按各自数据边释放。普通 miss 不再重复使用旧两层 checkpoint 租约或另一套 SPR load fill 日历。
- L2 事务资格来自功能阶段是否实际发出下层事务；L2 驻留供给不凭最终 supplier 虚构新的 L2 miss 槽。GETS/GETX 使用所需两层资源，UPGRADE 只使用控制器槽，E/M 本地发布不新建事务。prepare 与 mandatory store 进入同一准入接口，接管保持原 ID 与 lease。
- L2 完成边从实际准入及配置分量计算，在 L1 fill 和 CPU callback 之前独立记录；未知来源的 L2 边保持 `Unknown` 与保守 coupled 生命周期，不将旧 callback 改名为新的 estimated 完成边。
- 冷侧表维护功能 L1 行安装/写入与权限转换身份；事务回退恢复侧表但不复用身份编号。父服务保存独立的功能事务编号。消费者遇到内容身份不匹配时拒绝附着。
- JSON 增加 estimated 服务、容量等待、身份回退及新增源拒绝原因，消除“接口已就绪但 estimated 始终 false”的旧描述。

## 有界近似与覆盖

采用配置派生的名义 DRAM 服务与有限端口前沿，未重建逐周期 DRAM、cache controller 或 coherence 状态机。同窗口新请求的共享排队仍由原 canonical replay 拥有；冻结摘要没有预测整个窗口内的后续共享干扰，因此新时间是模型估计。

摘要只允许配置限定的窗口范围，动态时钟、remote supply、LLC merged-memory、跨核同线访问、atomic、页状态填充及缺失身份等不进入新拆边路径。回退不删请求、不捏造缺失元数据。L1 重新安装代表本地 cache incarnation，不宣称系统级物理数据版本；无法关联的后续发布或重新安装会明确退回旧来源。

私有侧支持路径已完成运行时接线和主目录集成。整体方案仍有以下未完成项，不能因原计划将其排在 S2/S3 就称整体修复完成：

- **统一合并**：在功能服务选择前确认可附着父服务，只消除重复的下层事务及服务资源动作，保留各请求自己的局部准入、回调与功能副作用。当前 `resolve_parent_service()` 主要在时序反馈中补等待，不等于该能力已实现。
- **LLC/预取所有权统一**：现有 `llc_transient_ready_`、共享 pending-memory 服务及预取账本仍未统一为一个服务事实所有者；私有侧增加 L2 lease 不代表这些拥有者已经迁移。
- **受限延迟 Tag 发布**：`SetAssociativeCache::access_indexed()` 的 miss 仍直接执行 `install()`；尚未接入按服务完成边发布 Tag 的功能路径及完整的 victim/dirty/invalidation/晚 fill 处理。时序等待不能替代这项功能状态修复。

多成员/非普通 store、无法建立身份或隔离副作用的路径仍显式使用已有回退；不宣称所有协议动作均完成拆边。整体剩余能力仍须保持两阶段、一次反馈和有界近似，不能用逐周期模型、额外完整回放或删请求代替。

prepare 保持开启，保留退休功能流、现有并行两阶段及一次反馈框架。主目录通过 `core.unified_parent_service = "v2-wait"` 选择本次机制；全局默认仍为 `off`，尚未以 CPI 与吞吐实测完成默认启用验收。

## 必要验证

隔离目录已通过 Release 构建（`FASTSIM_LTO_JOBS=16`）、`fastsim_tests`、`fastsim_parent_service_tests`、`fastsim_service_time_model_tests` 与 `fastsim_legacy_prepare_runtime_tests`。主目录 `fastsim` 和 `fastsim_tests` 的 Release 增量构建及 `./build/fastsim_tests` 也已通过。执行时使用 `LD_LIBRARY_PATH=/opt/gcc-11/lib64`，没有修改全局环境。主目录回执见同目录 `main-build.log`、`main-regression.log`。

真实 store 流已执行 8 个 GETX estimated 拆边服务；另一组先读后写输入执行 8 个 UPGRADE estimated 拆边服务。信用不足与短批次输入仍完成全部 mandatory store。

在途接管定向反例也通过：3 个退休 UOP 全部保留，`store_takeovers > 0`，接管角色为 2。接入 L2 后，该 store 的实际退休为 213、发送为 249、estimated 权限就绪为 361、SQ 响应为 464（模型周期，替代此前 L1-only 反例）。响应继续等待独立的数据/控制器边及发布，未在权限边提前释放。普通 12-store 输入的接管计数为零，不能用该输入证明在途接管；上述三指令反例补全了这一项检查。

新增必要反例通过：L2 满且 L1 有空位时 mandatory store 被统一容量约束，12 个退休 UOP 保留；GETX 两层分配次数一致；UPGRADE 的 L2 TBE 数大于 L2 fill 数。池测试分别在 L2 data=50、L2 controller=60、L1 data=90、L1 controller=100、store callback=150 时检查独立释放；拒绝试算不影响已接受的 L2 lease，接管不重新占槽。

检查曾发现并修复两处接入错误：身份开关早于 cache 构造、普通 store 在 prepare 前重复分配读类资源。组件通过不能替代运行时检查；上述计数来自修正后的实际 `Simulator` 执行。

停止记录：`tmp/parent-service-v2-time-exclusive-20261110/stopped-experiments.json`。相对冻结 R0/R1 的补丁：`tmp/parent-service-v2-time-exclusive-20261110/runtime-integration.patch`；相对原始主目录的完整补丁：同目录 `private-runtime-integration.patch`。最新隔离全量回归回执：同目录 `l2-regression.log`。

集成前逐文件验证主目录与原始冻结基线完全一致，集成后验证与隔离源码字节一致；文件与 SHA256 列于同目录 `main-source-integration.json`。

结构化回执：`tmp/parent-service-v2-time-exclusive-20261110/runtime-validation.json`。

本轮没有新吞吐或 CPI 测量，不能由机制接入推断精度或性能收益。
