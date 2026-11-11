# I-fetch 与预取：保守发布证书切片

## 当前无法直接推导的边界

`ensure_epoch_lookahead` 以 resident 最后一个 UOP 的 dispatch 超过 horizon 判定覆盖；`issued_memory_end` 按单调 `delta_q16` 选取连续前缀。I-fetch 却使用原始 `producer_issue_q16` 访问共享层，其值可早于包络和该 UOP 的 dispatch。切 chunk、同 UOP 的其他请求跨越 horizon 或生产者尚未生成后续 chunk 时，后续批次可能含有比上批服务时间更早的 fetch。上述 dispatch/horizon 都不能直接证明所有未来 fetch 已晚于某个预取完成边。

本切片不静默抬高 I-fetch 请求时间，也不把尚未证明的 horizon 当水位。`SprPrefetchService` 目前使用同一个单调 frontier 管入队和完成，本组件不会修复它对跨 Q 时间倒退的拒绝；因此**原 I-fetch 配置仍禁止与受限预取同时使用**。

## 独立证书检查器

`include/fastsim/spr_prefetch_publication_fence.hpp` 接收每核由外部提供、不可撤销的“所有尚未消费的 cache 相关事件不早于此时间”的模型参考时钟下界；未认证核保持 unknown，已完全结束的核可标记 finished。只有：

1. 调用者已提交相关批次/事务，且预取自己的模型响应已经到达；
2. 每个未结束核均持有证书；
3. 模型响应时间严格小于所有证书的最小值，避免无证据地决定同时刻的写入与填充顺序；

才允许请求方尝试发布。按核更新和检查成本为 O(1) 与 O(核数)，无需每周期扫描；状态可复制，和预测器、pending、cache、目录一并快照/回滚。调用方不得仅因 resident 结束而标记 core finished，还必须证明没有 unseen producer、异步一致性事务、page seed、未提交写入或其他后续 cache 状态变更。checker 不能检查证书本身是否真实，伪造证书会破坏安全性。

这是**必要检查，不是充分的端到端一致性证明**。延后到当前处理位置发布时仍须重新验证许可、目标 L2 驻留、写取消和驱逐，且不能把填充伪装成过去已发生；晚发布可能改变替换顺序并低估预取收益。同一周期可见顺序、跨核写入、TSO 回滚及未来 line 合并都要有明确单遍规则。未能得到证书时维持 pending 或以已计数方式放弃，不能生成过期 hit 或无限制囤积；排队上限造成的丢弃也须报告。

## 下一步与验证边界

要真实接入，阶段一需额外导出未生成 I-fetch 的可证明下界；阶段二合并每核 resident/unseen 的最小下界与所有其他 cache 事件，区分 request 接受时钟与可发布时钟，处理活动为空、功能同步、尾部、快照与回滚，并把已延后发布的时间交给原模型。不能仅在 `advance` 上取 `max(old,now)`，因为较早 demand 仍会从已经写入未来填充的 cache 读取数据。若无法在现有有界接口获得证书，保留互斥并转向其他机制。

独立运行 `python3 -m unittest discover -s tests -p 'test_spr_prefetch_publication_fence.py'` 一项通过，覆盖 unknown、严格同周期边界、模型响应未到、未提交事务、快照回滚、递减证书拒绝与结束。尚未接入 Simulator，也没有跨 Q 端到端、真实负载 CPI/PMU 或吞吐测试；新增核数扫描、证书维护、延迟填充对吞吐的影响未知。

## 接受顺序近似与 buffer TSO 的接线边界（2026-11-05）

允许精度损失后，不一定要证明原始时间轴上的全核水位。可定义新的单遍近似：按阶段二已接受的顺序服务每个 demand，将早于持久模型前沿的 I-fetch 或数据请求推迟至该前沿；预取也在这一模型次序服务，晚发布和推迟周期单独计数。它使用模型和功能输入，不宣称恢复 gem5 时间顺序。取指、数据和缓存事务仍各自只记一次，功能结果与模型 cache/目录状态必须基于相同的次序；需要在每次 rollback 时恢复前沿、cache、目录、预测器、预取队列及统计。必须关闭错路径取指生成，且不能在不同频率域下直接混用局部和参考时钟。

不能只在现有 `replay_prefetch_batch` 调整 `advance(now)`。`replay_memory_event` 对 private hit 返回的时延不自动包含从原请求到新服务边的等待，I-fetch 又在 `compute_timing_feedback` 使用 `exposed_cycles` 修正取指；若未一致反馈或重复反馈，会低估或重复计费。现有 runtime fixture `tests/test_spr_l1_prefetch_runtime.cpp` 明确拒绝 prefetch + I-fetch，下层真实取指的另一个现成 fixture 是 `tests/test_perf_cache_runtime_integration.cpp`，它们还没有一个同时覆盖新组合与 buffer TSO 的集成用例。

buffer TSO 是更关键的阻塞：`compute_timing_feedback` 在求得 `sq_release` 时便更新 `store_drain_ready` 和每核后续 UOP 的准入，并把相同释放边保存到 `buffered_store_publish_cycles`；随后 `publish_buffered_tso_stores` 才将缓冲写入实际发布到目录/cache。如果把实际发布简单延到 batch 末尾的模型前沿，SQ 信用可能早于被推迟的模型发布归还。未来的正确近似切片至少需要：

1. canonical replay 后，将接受次序前沿作为每核局部时间下界带入一次 `compute_timing_feedback`，在 SQ 释放和后续 store 准入确定之前使用该下界，而不是事后修改 publish 时间；
2. 在该前沿后按有界发布顺序完成 buffer store，取消同线在途预取；将应在中间完成的其他预取延迟到下一事件处理，明确记录这个预取晚发布近似；
3. 给被夹高的每个 demand 加一次相同的本地等待，在 I-fetch 的 exposed 路径验证没有双计；checkpoint 和事务回退要同时覆盖前沿、取消/完成统计、cache/目录和既有 SQ/TSO 状态；
4. 以实际 Simulator fixture 覆盖跨 Q I-fetch 倒退、same-line store/取指/预取、跨核干扰、队列容量、TSO rollback，以及 demand/取指人口与预取期初 pending + issued = completed + cancelled + 期末 pending 的同口径守恒，以及 OFF 行为。

这些改动横跨 cache replay、取指和 SQ 的反馈链、TSO 发布与事务状态，并非只放宽配置检查。当前未实现此组合，也没有其真实负载 CPI/PMU/吞吐结果；不能通过静默关闭 buffer TSO 或未反馈的简单时间夹高宣称已支持。FR-FCFS 仍需独立的一遍请求账本和排队近似，不能仅把既有多轮修复的 pass 设置为 1。
