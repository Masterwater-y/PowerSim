# 统一父服务剩余三项：实现与验收

已执行请求合并、统一 LLC 记录和延迟 Tag 发布，15 个文件已集成主目录。
完整模式为 `core.unified_parent_service = "v2-publish"`，prepare 保持开启。
本轮保留并行 CPU producer/feedback 与串行共享功能阶段、一次反馈及退休
功能输入，不启动独立详细 runtime 或整窗口收敛。

主目录 Release 构建（LTO 16）和核心/两项新定向复验均通过；
隔离 Release 的六项验收全部通过。

| 验收 | 隔离结果 | 主目录复验 |
|---|---|---|
| 全量 `fastsim_tests` | 通过 | 通过 |
| 合并、发布、GETX/UPGRADE/store 接管 | 通过 | 通过 |
| L1/L2 预取、跨 Q、跨核写取消、FRFCFS | 通过 | 通过 |
| 旧 prepare runtime | 通过 | 隔离兼容性验收 |
| 父服务池与真实两阶段 runtime | 通过 | 隔离兼容性验收 |
| shared mixed service | 通过 | 隔离兼容性验收 |

## 实际机制见证

| 输入/关系 | 实际结果 |
|---|---|
| 同核 8 次同线读取 | 8 次退休；L1 lookup/miss 8/8，L2 1 次，LLC 1 次，DRAM 1 次，7 次合并 |
| 两核同线读取 | 16 次退休，各核仍有独立私有请求，合计 L2 2 次、DRAM 1 次 |
| prepare GETX、驻留后的 UPGRADE、store 接管 | `v2-merge` 与 `v2-publish` 均执行 GETX 8 次、UPGRADE 8 次、接管 1 次 |
| 私有 L2 容量满载 | 两模式均观测 11 次资源等待；CPU 反馈不再申请第二份 L1/L2 租约 |
| L2 预取提前/完成、短 Q、FRFCFS | 各例 2 个统一私有预取服务，最终发布 2 个，无残留在途记录 |
| L1 预取 | 实际发起 1 次、统一队列完成发布 1 次 |
| 跨核写与在途预取 | 创建 2 个，完成发布 1 个，取消 1 个；已发交通和租约仍释放 |
| 同周期填充/替换、dirty victim、回滚 | 旧代次不重装；dirty victim 在完成时确定；完成后的记录与队列可恢复，ID 不重用 |

UPGRADE 见证特意等待先前 load 的数据完成；延迟发布模式下，立即跟随
未完成 load 的 store 仍可能合法走 GETX，不能用提前安装 Tag 的旧输入
强求 UPGRADE 计数。所有 mandatory store 的退休和 legacy perf-store
人口均保留。

## 唯一归属

`HierarchyServices` 记录拥有各层数据、权限、控制器完成与资源边。
行、资源堆和发布堆引用服务 ID；事务 journal 只保存触达记录，队列保存
有界 ID checkpoint。请求成员、提示 credit 和 FIFO/store callback 仍
独立存在，不能以合并减少退休人口。

同核 follower 做独立 L1 probe/callback，不重复下层请求或租约。跨核引用
LLC 父服务时保留各自私有请求。L1/L2 预取保留原预测器，实际服务与发布
迁入统一池；预取人口守恒由该池实际的创建、发布、取消事件核查。

`v2-publish` 通过 probe 与完成时填充分别处理 L1/L2/LLC；失效/替换取消
旧代次发布资格但保留已发交通和资源释放。同周期写先取消相冲突的预取
发布。prepare/store 反馈读取不可变服务回执，CPU 旧池不再重复拥有租约。

## 时间与结论边界

Tag 发布对应本模型选定 reference calendar 的完成边。CPU 最终准入用
显式代次、行、序数、clock 与窗口资格检查；合格 prepare unique 服务在
实际准入处使用既有配置时长和冻结端口前沿的有界模型，超出资格最多做
一次请求局部回退。follower 继续引用已发父服务，不重发或重复收费下层。
这不是最终 CPU 调度的逐周期因果认证。

本轮完成实现、集成、构建和机制回归；未恢复 native 审计、CPI 对照或
30 次串行吞吐配对。因此没有新增 CPI/PMU 精度或宿主吞吐收益结论。
FST 没有字节级 store 值的测试不能认证实际数据值守恒。既有默认 `off`
与旧模式保留，完整模式通过明确配置启用。

## 工件

- [实现说明](docs/unified-parent-service-complete-20261110.md)
- [隔离验收回执](tmp/parent-service-complete-exclusive-20261110/acceptance.json)
- [源码集成回执](tmp/parent-service-complete-exclusive-20261110/main-source-integration.json)
- [主目录验收回执](tmp/parent-service-complete-exclusive-20261110/main-validation.json)
- [完整补丁](tmp/parent-service-complete-exclusive-20261110/complete-runtime-integration.patch)
- [隔离最终构建日志](tmp/parent-service-complete-exclusive-20261110/complete-release-build.log)
- [主目录构建日志](tmp/parent-service-complete-exclusive-20261110/main-release-build.log)

集成前核验了 415 份冻结源码，无并发修改；复制后核验目标字节与已验收
隔离源码一致，最终再次核验 15 个集成文件无差异。文档目录沿用工作区
共享链接。未提交或推送。
