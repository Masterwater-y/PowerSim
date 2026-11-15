# Store 摘要与现有阶段二：最小接线评审

## 实际数据位置

- `compute_core_timing_feedback_impl` 中按 UOP 计算 dispatch 时，`gate_sparse_queue` 对 `dispatch_store` 查询 `sq_ready` 和 `sq_next_slot`，将容量限制通过 `iew_to_rename + rename_to_dispatch` 投影到 dispatch。该环按 UOP 跟踪旧模型的未来释放，并不是单独的 committed buffer 或 shared budget。
- 同一 UOP 的 `retire_ready_prefix_and_drain` 得到 `actual_retire`；普通 store 随后得到 `store_send`、基于 cache feedback 的 `store_latency_cycles` 和 `sq_release`。旧路径以 `regular_store_response_cycle` 等确定释放值，最后将它写入 `sq_ready[sq_next_slot]`，并以 `store_drain_ready` 保存 TSO 顺序约束。这个旧 `sq_release` 通常是模拟响应，并非 gem5 committed buffer offload 时原 SQ 项的释放。
- `TimingFeedback` 为每核保存 `sq_ready_cycles`、`sq_next_slot`、`store_drain_ready_cycle` 和可选 `spr_store_window`。`compute_timing_feedback` 在处理前从 `response_*` 拷贝；`commit_timing_feedback` 在最终接受前缀后写回。这是跨 chunk 状态与可回滚工作副本的实际接入位置，但尚没有 committed-buffer、受限回调队列或它们的事务身份。
- 普通 store 的功能资格在 `ChunkMemoryEvent::spr_ordinary_store`，由本次 FST 的 `.sreq` complete/ordinary 等位得出；同线合并还需要与该请求绑定的 context、requestor、flags、byte mask，而当前 event 只保留 bool 和 line。缺少的事实必须从同一记录的功能 companion 传递，不能拿 OoO 回调或预测状态填充。
- `spr_store_input.hpp` 提供功能资格与 line-relative byte mask 的保守值转换；
  `spr_store_facts_chunk.hpp` 为未来 opt-in 提供按 memory-event 索引的
  per-chunk 值副本及 Known/Unknown 区分。独立测试通过，尚未接 Simulator，
  整机关闭开销未实测。地址/长度/有效字节等原则上来自功能执行，而当前
  gem5 context/requestor/flags 编码由 OoO Request 提供，Atomic/QEMU 生产者
  的等价映射仍未证明；不能使用 native 的动态 callback 或合并结果。

## 为什么不能直接调用摘要

阶段二按程序顺序逐 UOP 完成 dispatch、issue、retire，而不是按事件时间全局推进。例如 store A 的 dispatch=0、retire=100，紧随其后的 B 可以 dispatch=1。若在 A 的处理函数中立即调用 `offload(100)`，之后 B 的 `admit_sq(1)` 会使事件前沿倒退；若立即按未来回调释放，更会让 B 使用尚未可用的容量。仅在 `sq_ready` 赋值位置替换释放周期无法解决这一顺序问题。

同时，gem5 的容量是 SQ + committed buffer 共享：普通项 offload 后 SQ 槽释放，但共享预算通常保持不变，连续未发送同线 merge 才减少一个预算单位；请求回调后只回收已完成队首前缀。用每条 store 独立的旧 `sq_ready` 响应时间同时代表 SQ 和预算，会对 merge 和回调顺序重复或遗漏计费。包括无法认证的普通 store、原子和特殊操作在内的竞争者必须进入统一预算，不能仅统计摘要覆盖人口。

## 有界单遍方案候选

1. 为每核 `TimingFeedback` 副本增加一个 store 摘要和有界未来事件容器，随当前 checkpoint 快照与最终提交；容量按 SQ、ROB 及既定 chunk 上限约束，溢出显式回退。先只支持一个合格的核心子域，并要求与旧 store window、竞争 solver 互斥。
2. 在下一条 UOP 的**最终 dispatch 决定前**，只处理时间不晚于该 dispatch 查询点的已知 offload/可见/回收事件；未来事件保留，不推进到当前 UOP 的 future retirement。对 shared budget 已满的情形，按已知最近事件边界查询下一可用点，并把现有 rename 反馈延迟放在同一时钟；不得逐周期重试。
3. 退休时仅登记有身份的未来 offload；offload 时按 FIFO、line 和 byte-mask 允许合并并释放 SQ 项；被 Sequencer 接受后登记有序可见估计和最终模型回调，到达时才归还 line 信用和已完成前缀预算。所有事件的同周期先后必须固定并与 rename 可见延迟一致，且记录模型约定。
4. 允许混入但不能建模的操作时，采用显式边界：先等更早已登记操作完成，再通过原路径处理其**最终模型完成**，期间阻止会误用预算的后继；这是保守串行 fallback，单独计数和测量。不能只忽略不合格请求然后称预算完整。

上述只是候选，不表明现有代码已支持：当前 `MemoryReplay` 的 canonical classification/latency 在核心反馈之前生成，后续 issue 门、缓冲写路径或重新定时可能改变最终服务；还未证明能在单遍在线发出时获得不可再变化的 callback 时间。若没有该保证，要么把相关资源的服务结果变成唯一可提交的最终模型事件，要么保持功能关闭，不能先释放并事后整体反复修正。总线可见 floor 也不能取代 Ruby L0 重发、权限与一致性处理。

验收先做定向测试：A dispatch=0/retire=100 与 B dispatch=1；满预算中途发生同线 merge；四异线、同线并发与乱序 ready；callback FIFO；普通/特殊项交错；未接受发送不占额；checkpoint 裁剪和回滚；预热状态保留但统计重置；溢出与未知身份 fallback。之后在相同 FST 上独立报告模型 CPI 绝对误差、PMU、整体墙钟和回放吞吐；现有独立摘要单测并非该端到端验证。

## 稀疏事件与信用账独立切片

`include/fastsim/spr_sparse_store_credits.hpp` 在未接 Simulator 的前提下组合已提交缓冲、可见顺序和共享信用。`admit_sq(sequence, dispatch)` 申请预算；之后可提交按退休次序确定的 `schedule_offload(store, retire)`，只登记 future 事件，不移动当前前沿。下次 `advance(now)` 依次处理不晚于 `now` 的 offload，并在模型回调释放边到达时才归还 line 和共享预算。A 在 100 退休不阻止下一条 B 在 1 申请；只有两者都实际占用预算时第三项才受阻。

事件按 per-core 已知退休顺序进入单调 FIFO，offload 宽度用整数边界一次计算，不做逐周期重试；已预测的未来回调存在于另一个有界队列。`next_transition()` 只暴露已知 offload 和回收最早边；返回空值可表示回调或更早 store 仍未知，不能理解为资源空闲。队列满时 `schedule_offload` 返回 false 且不丢弃 SQ 身份，调用者需选择可计数的 fallback。入队次序、时间回退和容量均校验；一个普通 store 必须先经过该信用账申请，所有未覆盖的竞争者必须进入其他可证明的统一机制或使用已有的排他 `external_boundary`。

`send_next(now, final_model_callback)` 没有 callback 参数时拒绝发送，不释放任何信用。该参数必须是已有缓存/一致性模型提供、后来不会再改写的最终**模型估计**；此接口自身没有求解 callback，不证明 native 时间。若读取该结果本身依赖此次发送后的可变事件，则该切片不能直接套用。内部同周期按“先处理到期回收、再执行 offload、最后响应显式 send”的模型次序进行；回收至少晚于模型回调一个指定的正延迟，不能据此宣称精确的 gem5 同周期仲裁。

后续源码复核：当前批次 FR-FCFS 和响应反馈确定后、`publish_buffered_tso_stores`
调用处可以取得 `buffered_store_publish_cycles` 这一已选定的模型边，但仅覆盖
legacy buffer prepare 的部分 store，不是通用异步 callback。其值可晚于当前
窗口 horizon，必须作为跨窗口待完成票据保存，不能在登记时释放信用。最终
反馈后登记也不能反向改变本窗口已经计算的时序；如果先只用于后续窗口，须
作为明确的保守近似报告。覆盖所有普通 store 需要各自产生最终模型完成边，
不能把旧 SQ 响应直接称作 native store buffer 生命周期。

组件及整个未来队列可复制为 checkpoint 快照：跨 chunk 必须保存、试算失败必须回滚，已分配身份沿用原有防重用机制。一个尚未调度的更老 SQ store 会阻止对更年轻 store 调度 offload；这依赖调用方完整绑定每项身份和严格退休顺序。等待未支持 store 时，保守边界先要求模型共享预算清空，阻止后继占额，直至外部路径的最终模型完成；这可显著增加串行化，必须单独统计，不能视为目标精度或可接受吞吐。

`python3 -m unittest discover -s tests -p 'test_spr_*store*.py'` 七项通过，新增测试覆盖未来退休与较早 dispatch、未知 callback、按宽度计算的同周期 offload、same-line merge、异线窗口、回收顺序、排他特殊操作、队列容量以及带 future 事件的副本。它只验证独立模型边界，不证明 callback 最终性、Simulator 实际接入、性能或 CPI/PMU 改善。接线前仍需先解决上文的 final callback、cache/coherence 与 dispatch 时间轴问题。

后续补齐 `SprCommittedStoreBuffer` 的特殊/未知 store FIFO barrier：它们占同一
共享预算，需等待更老 committed 项完成且自身实际外部服务完成，期间阻止更年轻
普通项 offload 和跨 barrier 合并。当前 `test_spr_*store*.py` 九项通过；这些
仍是独立组件验证，主路径须完整登记所有 store 并处理最终模型 frontier。
该 barrier 已继续贯通 ordered pipeline 与 sparse credits；阻塞时不虚构释放
边，额外 `test_spr_sparse_special_fifo.py` 通过。实际阶段二和下层请求人口
仍未接入，不能由组件正确性推导整体时序改善。

另有 `spr_shared_store_credits.hpp` 的有界共享预算日历，以事件释放堆
允许复用已释放的较年轻 follower 信用；普通、unknown 与 special 共占预算，
组 owner 到 callback、follower 到模型 offload 才释放。独立测试通过，
但它不代替 SQ FIFO、special barrier、共享缓存发布与安全水位；阶段二接入
必须将所有竞争者入账，并随既有事务复制、回退和跨 Q 提交。

事务审查发现同一 cache set 的外层事务可能因内层事务改变 generation 而重复
记录快照；原先按正序恢复会留下中间状态。`SetAssociativeCache::restore`
现倒序撤销，已通过定向负例、完整构建和主测试。此修复不自动把仅被内层
触及的 set 加入外层 undo，故仍不支持任意嵌套回滚；真正的事务 fallback
须禁止此类嵌套或显式合并 undo。此外，现有 buffer TSO 恢复不覆盖默认
开启的 Simulator 级 perf FB-hit shadow，须快照恢复或在受限路径互斥。

## 本轮接线复核：保持关闭

现有组件还不足以构造可安全启用的 shared budget，因此本轮不增加只产生
诊断数据的桥接层，也不改变现有模拟行为。具体阻碍是：

1. `compute_core_timing_feedback_impl` 派发时用 `sq_ready` 限制每个
   `dispatch_store`，退休后再把 `sq_release` 写入同一账本。新账本必须在
   明确的 opt-in 子域内替代两处及其跨窗口拷贝，不能在旁边再计一次。
2. memory replay 在核心时序反馈前逐 memory event 对 private/shared
   hierarchy 产生访问和状态，再由阶段二计算 `store_send` 与 `sq_release`；
   受限路径之外后续还有 response/DRAM 修正与前缀选择。摘要的同线合并可以
   减少预算占用，但目前没有把被合并的事件和层级请求以同一事务身份绑定的
   接口。直接应用它会留下未对应的层级请求或不一致的请求/PMU 人口；已有
   sequencer alias 和 buffer prepare 路径有各自语义，不能直接认作这个合并。
3. `SprSparseStoreCredits::send_next` 要求不可再更改的模型 callback，
   可用的 replay 结果尚未证明是经所有发送门和最终前缀选择后的对应事务
   完成边。把较早预测当最终回调可能提前归还预算；把最终反馈结果只存到
   下一窗口则不能修正本窗口已经做出的 admission。
4. 底层 `SprCommittedStoreBuffer` 已实现特殊项 FIFO，但
   `SprOrderedStorePipeline` 和 `SprSparseStoreCredits` 仍只提供须清空
   全部旧项的排他外部边界，没有转发 `admit_special`/完成事件。直接在此
   基础上给未来普通 offload 排期，遇特殊屏障会被拒绝，现有稀疏队列将其
   视为逻辑错误；不能称其已支持普通、未知和特殊项混合。

要形成可接入的单遍切片，调用方需要一次性给出有界、可回滚的事务关联：
每个 store 的功能事实和身份；入队后不可回退的模型发送接受/完成边；
已合并或保留原请求的明确人口决策；所有未知/特殊项的 FIFO 及其最终完成；
以及与已接受前缀一起提交的跨窗口信用快照。一个候选是让 shared budget 和
层级模型在同一已接受请求流上达成合并和完成决定，再把未来回收放入有界
事件队列；该流程还需要证明不需要重新跑整个窗口，且与现有两阶段/吞吐
目标兼容。未完成这些证明前，不启用该路径，不声称已解决 store 串行、
请求人口或端到端 CPI/PMU 精度。
