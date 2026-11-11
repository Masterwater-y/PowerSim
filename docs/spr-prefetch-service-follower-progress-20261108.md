# SPR 预取服务 follower：实施进度

此记录对应 L2 预取到 DRAM 和 demand 跟随的首片，仍在实施中。

`core.spr_l2_prefetch_follow` 默认关闭；开启时使用受限 L2 预取路径，以 LLC 或
DRAM 供给，并允许同核普通 load 跟随已发出的同线预取。候选就绪与服务完成在
相邻 demand 事件之间按模型时间处理，反馈后的需求填充预约到实际响应时间，
同线写和私有 L2 替换撤销相关预约。预测器处理已认证的 GETX miss、GETS miss、
迟到跟随与 L2 hit；未认证请求不能自动推断为 GETX。

真实 Simulator 回归覆盖额外 DRAM 去重、多 follower、跨窗口填充、LLC 供给、
无中间 demand 的队列发起、同组冲突、跨核写、GETX miss 和 FR-FCFS 配置。
含预取活动的 FR-FCFS 窗口跳过 demand-only 修复，保留控制器一遍服务结果，
该近似由 `spr_prefetch_dram_repair_skipped_epochs` 计数。

这一受限路径仍关闭 L1 预取发起；已允许 buffer TSO 和 recorded scheduler 的
实验回放，未来 store 使用逐行预取限制，而不是抬高全局前沿；这不构成对全部
跨核一致性或上下文切换的认证。队列末尾尚无未来需求驱动时，
不会据未经认证的水位提前推进；资源仍受 host 上限约束，尚未完整接入独立
fill/TBE 与需求竞争。完整的 L1→下层服务、跨核共享、buffer TSO 逐行发布、
全请求类型训练、污染与流量统计、目标参数导入和同源端到端实验仍待完成。

已有 `SprPrefetchService` 摘要增加了按 ticket、核和行查询 follower 绝对响应的接口：
对已选择的响应计算 `max(issue + local, service_ready + return)`，不改变记录、
不发布标签，也不分配 CPU 请求。调用者必须先认证权限和数据路径；该接口并不
保证跨核共享，也不保证在服务失效后可以跟随。测试覆盖多个 follower、未开始或
未选定响应、身份不匹配、取消、反馈移动 issue 和溢出。已接纳 follower 可以
保存独立的 ticket 和 ready 快照，在活跃服务记录清理后继续计算 CPU 响应；调用者
仍负责在影响该需求交付的失效或事务回滚发生时撤销引用。该快照本身不认证权限。

独立 `SprPrefetchService` 的 follower 身份和绝对响应计算已用于 Simulator，
但共享 pending 尚未覆盖所有请求源或跨层父链。首轮 Graph500 C4 FST 结果及其
同源 Ruby 请求诊断比较位于 `tmp/spr-prefetch-fst-pmu-20261108/`；ON/OFF 的
FR 修复使用情况不同，不能将差值单独归因于预取。退休 miss 模型与旧 native
参考策略版本不一致，维护比较拒绝计分。正在统一反馈路径后重跑，尚无新的
完整退休 PMU、准确性或吞吐验收。
