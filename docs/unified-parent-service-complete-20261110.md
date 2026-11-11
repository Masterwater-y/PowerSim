# 统一父服务：合并、LLC 与延迟 Tag 发布

本实现对应 2026-11-10 用户要求的剩余三项，接在已集成的私有侧
v2 修复之后。CPU producer 与反馈继续并行，功能共享阶段仍按选定顺序
回放，一次反馈；不增加逐周期模型、错误路径输入或整窗口收敛。

## 使用

在兼容的单次 sparse TSO 配置中：

```ini
[core]
unified_parent_service = "v2-publish"
legacy_prepare_timing = true
```

`v2-publish` 同时启用同核请求合并、LLC 服务统一和 L1/L2/LLC 完成时发布。
`v2-merge` 用于同样的服务归属与合并、保留 lookup 时安装 Tag 的增量对照。
既有 `off/observe/wait/v2-observe/v2-wait` 行为保留，默认仍为 `off`。

现有 L1/L2 预取预测器可在完整模式下启用；L2 共享预取按其现有配置
配合 `spr_l2_prefetch_follow=true`。预测器与候选队列仍是预测器状态，
在途服务、容量与发布归属迁移到统一池。

## 实际运行路径

- `HierarchyServices` 按控制器域、物理行和指令/数据身份索引服务 ID。
  数据、权限、控制器完成以及 L1/L2/LLC 租约由记录拥有；
  发布和资源堆只保存 ID。触达记录使用 journal，队列 checkpoint
  复制有界 ID 索引，回滚不重复使用代次。
- 同核 follower 做独立 L1 probe 与本地 callback，不重复 L2、LLC、
  DRAM 请求或父租约。跨核仍有各自私有服务，可引用同一 LLC fill。
  写权限不兼容时不合并；原子等特殊请求仍通过实际分类路径。
- LLC unique fill、remote fill、共享预取均在统一记录中准入、选择服务、
  发布并释放。旧 `SharedFill` 的跨 Q memory leg 也改为本体拥有时间，
  原独立 pending services map 只保留弱代次索引。
- prepare 的 CPU 反馈只读取不可变回执，维护提示 credit 与 FIFO/store
  callback。GETX、UPGRADE、接管继续开启；不在 CPU 旧池申请第二份
  L1/L2 租约。store 可对尚未发布但合法的父代次登记 dirty 意图。
- `v2-publish` miss 使用 `probe()`；L2、L1 和 LLC 各自在服务完成边
  调用 `complete_fill()`。victim/dirty/writeback 在实际发布时处理，
  失效或替换取消旧代次资格，但已发交通和资源释放继续保留。
- L1/L2 预取复用现有预测器，实际服务使用统一记录。L2 预取成为 demand
  的父服务时，只扩展该记录的 L1 发布目标与租约，不建立另一份回填表。

## 时间含义与边界

统一功能池拥有选定的 reference-clock calendar。它不是 gem5 timing
输入，也不声称等于 CPU 反馈的最终调度。回执检查代次、行、控制器、
功能序数与 clock 资格；窗口内使用已选择服务边，超出范围最多做一次
请求局部回退。已知 follower 始终引用已发父服务，不重新收费下层事务。
prepare 的合格 unique 服务在实际准入处应用既有配置时长和冻结端口前沿
有界模型，其他情况保留选定父服务或一次局部回退。

因此 Tag 发布精确对应本模型的选定服务完成边，而不是最终 CPU 时刻的
逐周期因果认证。任意跨核重新排序、DVFS、竞争实验选项仍按配置合同
拒绝或保守回退；没有扩大到独立详细 runtime。选定准入前沿、提示
候选和 ID 索引均有明确容量界。

## 验证

必要命令：

```bash
cmake --build build -- -j16
./build/fastsim_tests
./build/fastsim_hierarchy_service_tests
./build/fastsim_hierarchy_prefetch_tests
./build/fastsim_legacy_prepare_runtime_tests
./build/fastsim_parent_service_tests
./build/fastsim_shared_mixed_service_tests
```

新定向测试执行实际 Simulator 和缓存实现：同核下层唯一请求、跨核共享、
GETX/UPGRADE/接管、L2 容量满载、跨窗口、同周期发布/替换、dirty victim、
晚到填充取消、完成后的事务恢复、预取提前/完成、跨核写与 FCFS/FRFCFS。
这一轮是实现与机制验收，未恢复 CPI/native/30 次吞吐批次；不据此宣称
CPI 或宿主吞吐改善，也不认证 FST 未提供的字节级数据结果。
