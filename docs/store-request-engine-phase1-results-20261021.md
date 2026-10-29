# Store prepare 请求生命周期：设计与首阶段实施结果

后续更新：已新增显式epoch闭合接入候选，见[接入设计](superpowers/specs/2026-10-21-store-engine-integration-design.md)与[三负载实测结果及失败边界](store-engine-integration-results-20261021.md)。以下记录保留首片交付当时的状态。

日期：2026-10-21。状态：生命周期组件已实现；SharedSystem/IntervalCore接入尚未完成。本文不发布新的CPI、PMU精度或吞吐结果。

## 当前结论和方案

store prepare相当于先为排队的写操作“取写权限”。SQ保存尚未完成的store，TBE跟踪取权事务，两者不是同一种资源。多个prepare可以并行；真实写仍按FIFO完成。权限回来，只说明取权结束，不代表store已经写完，更不能提前释放SQ。

XZ对驻留证书敏感，是因为旧路径无法证明驻留时会关闭prepare，写密集区因此退化为逐个队头等待。证书通过后重叠恢复，CPI会大幅变化；但这不能证明prepare、驱逐、重新取权及PMU时序都已正确。完整分析见[设计](superpowers/specs/2026-10-21-store-request-engine-design.md)及其引用的既有实验。

修复目标是让证书只决定“可否批量算”，不决定“硬件能否发请求”。证书失败时应逐事件推进同一个模型。单纯把完成时间延后，不能修复此前已经更新的缓存与目录状态，因此需要统一请求身份、实际准入、服务和返回。

## 已落地的代码

新增[StoreRequestEngine接口](../include/fastsim/store_request_engine.hpp)和[实现](../src/store_request_engine.cpp)，接入生产库、完整测试及两个独立测试target：

- SQ、每周期store接口端口、optional队列、取权事务槽分别管理；容量由profile给出，不当作SPR硬件认证。
- address ready、retire、canWB分开；retire到canWB检查commit传播延迟，禁止倒退时间和越过旧store。
- mandatory的CPU入队与实际协议服务分开。TBE满可以阻塞服务，不能直接阻止允许的入队；prepare同样先入队、后占协议资源。
- hint身份、mandatory身份与物理事务身份分开。同line准备可跳过或合并，队头接管原事务，不重复分配TBE。
- permission返回只释放对应事务；真实write response才释放SQ。失效和旧代次回调不能给新请求授权，mandatory重试保留原请求身份。
- 已接受hint可活过原SQ项；仅保留活动状态，不建立随整个trace增长的完成ID集合。optional容量0明确表示无配置上限，活动队列积压仍可能占更多内存。
- 特殊store、错误core/fragment、乱序回调、重复回调和时间溢出拒绝处理；当前仅支持普通单fragment store。

组件不预测缓存命中或未来完成时刻；backend必须提供真实模型事件。当前主仿真的缓存与核心还没有调用它，所以这次代码并不改变已有负载的模型预测。

## 验证及审查

在detached worktree中覆盖当前dirty源码，冻结基线后实施；不是从较旧HEAD单独构建。既有根目录修改保留，整合只新增组件/测试并修改CMake与测试入口。

先确认新增测试失败，再实现并确认通过。覆盖双冷store并行、队头接管、同line去重、SQ/端口/optional/TBE不足、enqueue与service分离、完成前后失效、旧callback、保留mandatory重试、跨时间推进、hint超越SQ、200次请求排空无完成历史，以及非法输入与溢出。

独立审核找出并修复两项问题：

1. SQ插入后创建line失败会留下“幽灵SQ”。现在回滚插入，恢复内存后同owner重试只接受一次；独立故障注入target验证。
2. 日志sink重入可能清空外层仍要使用的状态。现在全部14个修改接口禁止sink重入；sink异常后实例禁止继续修改，保留只读诊断。独立审核验证全部入口。

隔离构建的完整FastSim测试、两个新增独立target、三个既有PMU回归均通过。新增生命周期测试另经AddressSanitizer、UndefinedBehaviorSanitizer及libstdc++ assertions检查通过。独立复核未遗留P1/P2，判定范围仅限此组件。

主工作区最终验证结果见下方收据；所有原始日志在[`tmp/store-request-engine-20261021`](../tmp/store-request-engine-20261021/)。其中`integration.patch`记录相对本轮dirty基线的既有文件修改，`baseline-source.json`记录基线身份，`review.md`保留审查反例与修复后复核。

## 尚未完成的接入

1. 用实际SharedSystem准入驱动瞬态tag、TBE、目录与服务返回，处理backend拒绝和多核仲裁。
2. 接入IntervalCore continuation、共同安全frontier和跨epoch事务，避免先修改缓存再补完成时间。
3. 验证证书批处理与逐事件参考执行的周期、请求和PMU一致。
4. 必要时补gem5请求级oracle；再运行SQLite/XZ/Marian同源四模式验证及独立吞吐测量。

这些里程碑在[实施计划](superpowers/plans/2026-10-21-store-request-engine.md)中保持未完成。不能将本轮单元回归、生产库编译成功或独立审核通过等同于CPI误差已经修复。

## 磁盘与采集

本轮没有新增FST、checkpoint或gem5采集。维护脚本统计现有FST共52,907,823,136 bytes，低于100,000,000,000 bytes上限；本轮检查时空闲约443.34 GB。活动实验trace保留。详细记录见`storage.json`。


## 主工作区最终收据

| 检查 | 结果 | 原始证据 |
|---|---|---|
| `cmake --build build -- -j16` | exit 0 | `root-build.log` |
| `LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests` | exit 0，all FastSim tests passed | `root-tests.log` |
| 新生命周期target、分配失败target | 两项exit 0 | `root-targets.json`及各自日志 |
| 既有perf cache integration/runtime、retirement | 三项exit 0 | `root-targets.json`及各自日志 |
| 新生命周期ASan/UBSan检查 | exit 0 | `sanitizer.log` |
| 独立复核 | 两项P2关闭，无遗留P1/P2 | `review.md` |
| 整合身份 | 审核源码与根目录一致，既有simulator/cache/core源码无漂移 | `integrated-source.json` |

构建使用既有GCC11工具链；根目录保留原IPO设置，构建提示LTO串行任务警告，但构建和测试成功。没有据此测量或推断吞吐变化。根目录无自动commit/push。
