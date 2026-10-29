# Store engine production integration plan

> 使用 superpowers:executing-plans 在当前会话执行，末尾新上下文审核；用户已要求接入，不重复审批。工作区只保留本轮最小源码差异，不自动提交或推送。

**Goal:** 新engine实际驱动SharedSystem服务并反馈到核心SQ/TSO时序，完成SQLite/XZ/Marian同源CPI验证。
**Architecture:** epoch内按时刻合并store lifecycle与其他访存，实际backend响应闭合core反馈；迭代失败明确拒绝。保留现有框架的跨epoch/transient近似并报告。
**Tech Stack:** C++17，既有两阶段模型、StoreRequestEngine、维护原生validator。
**Spec:** [设计](../specs/2026-10-21-store-engine-integration-design.md)。

## Constraints / review focus

保留dirty基线；不按负载选开关；不增加FST；不使用oracle输入；不把validator passed称精度认证；全表CPI必须包含绝对误差；资源与退休计数分离；试算需回滚cache、目录、时序及计数，拒绝无声回退。

## Task 1: bridge and feedback

- [x] 冻结dirty基线、隔离worktree、基线完整测试。
- [x] 新配置/集成契约先RED；实现epoch event bridge、SharedSystem callback和core response/SQ反馈，GREEN。
- [x] 补证书中性、并行取权、同line/旧回调和非法配置回归；完整测试、专项PMU通过。

## Task 2: validation and integration

- [x] 冻结SQLite/XZ/Marian来源与配置并独立identity核验。
- [x] 维护validator跑原基线和候选，检查可用结果的人口/source/native gate，报告active CPI及elapsed；旧default 3 passed，候选2 passed/1 failed。
- [x] 新上下文审核与修复；最小diff整合、根目录构建/完整测试与7项专项测试通过，保存证据与文档。
- [ ] Marian整体反馈振荡修复，补齐第三例有效CPI；当前明确失败，不以局部回归或旧预测代替。
- [ ] 分解XZ剩余高估并完成精度验收；本轮新候选比旧default及归档prepare候选更差。

## 结果收据

[实测报告](../../store-engine-integration-results-20261021.md)保留同一phase候选三例结果、配对两例MAPE/MAE与elapsed、Marian复现日志、归档prepare对照、源码/二进制身份和磁盘收据。接入与实验执行完成不等于三例精度验收通过。最终根目录额外活动SQ扫描优化经过SQLite完整配对中性检查与8个独立短反例逐字节复核，XZ数字仍明确归属phase二进制。
