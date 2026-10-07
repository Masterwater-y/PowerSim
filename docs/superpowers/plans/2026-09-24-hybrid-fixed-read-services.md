# 固定读服务关系第一轮实施计划

> 执行方式：本会话直接实施，使用 superpowers:executing-plans 与 test-driven-development。

**Goal:** 默认关闭的固定读服务候选，完成机制测试及 NAMD C4、LBM C4/C32 对照。
**Architecture:** 程序序一次分类；现有 FR-FCFS 竞争求解；owner 完成引用传播；显式旧路径回退。
**Tech Stack:** C++17、CMake、现有 Python native validator。
**Spec:** `docs/hybrid-fixed-read-services-round1-20260924.md`。

## 全局约束

- PMU 5% 为观察目标，无精度准入门槛；CPI 同时报绝对误差和 MAE。
- 不更改默认、不调常量、不删除旧实验或既有未提交工作。
- 跨 epoch 身份不因预算或失败清空；OFF 预测保持一致。
- 原生来源和硬件身份分别检查，三个冻结案例不可静默减少。

## 任务 1：身份表与机制红灯

- [x] 在 `tests/test_response_completion.cpp` 加入配置驱动的同行读测试；未实现开关时应失败。
- [x] 实现 `FixedReadServices` 独立表：begin_epoch、find、insert、invalidate、最终时间发布。
- [x] 验证旧 generation 不复用、incoming owner 不依赖旧 vector 索引。

## 任务 2：候选入口与核心响应

- [x] 增加 `ruby.fixed_read_services` 默认 false；验证互斥和支持范围；JSON 输出该配置。
- [x] 新建 `replay_fixed_read_services`，每核程序序、核间队首时间序；一次功能访问。
- [x] `MemoryReplay` 保存父事件引用和跨 epoch 响应；`TimingFeedback` 保存本轮最终响应。
- [x] 核心计算 follower 使用 `max(父响应, 自身最小完成边)`，保留 CPU 容量与 WB/RAW。
- [x] 复用 FR-FCFS，功能关系不随其时间结果重建；提交当前表的最终 callback/response。
- [x] 恢复路径一并还原表／计数／层次状态；加入覆盖、incoming、late-reference、fallback 诊断。

## 任务 3：回归与冻结

- [x] 测试 FR-FCFS owner 改序后 follower 响应、RAW 反转、回调后引用、写／同组替换、跨 epoch。
- [x] `cmake --build build -- -j16` 和 `./build/fastsim_tests` 全部运行并记录结果。
- [x] 检查 OFF 对修改前二进制目标统计逐值一致，保存配置链和源码／二进制指纹。

## 任务 4：三个案例观察

- [x] 建立冻结源子集，独立 identity 检查实际三案。
- [x] 维护 native validator 运行 before/OFF/ON；不得另写参考选取逻辑。
- [x] 同二进制 OFF/ON/ON/OFF 串行重复，核对每次目标输出并记录吞吐。
- [x] 完成 CPI、PMU、覆盖、近似事件及成本报告，更新设计文档实施状态。

## Review Focus

RAW 反转不能引入响应早于请求；同组替换／写不能复用过期 generation；
跨 epoch 指针不能悬空；重算不得重复 PMU；OFF 不得受新表或新分支影响。

完成记录：默认构建与完整测试通过；before/OFF/ON 三案 native 校验完成；12 次 ABBA replay 完成且 scope 输出一致。
结果见 `docs/hybrid-fixed-read-services-results-20260924.md`。权限回退缺少端到端触发覆盖，已在报告披露；未将该项误记为集成验证通过。
