# SPR 有序 store 可见时序：独立前向近似

## 原生机制及证据

`gem5-spr/src/cpu/o3/lsq_unit.cc` 的 committed store buffer 有独立于 SQ 的空间和按 line 计的在途窗口，向 Sequencer 顺序发出 store，回调后按队首完成顺序释放。`gem5-spr/src/mem/ruby/system/Sequencer.cc` 对被接受的 `TSOOrderedStore` 维护每个 Sequencer 的全局 FIFO；`isOrderedStoreReady` 要求请求是队首，且同一周期只允许一个不同的 line 可见。同一 line 的连续 store 有机会共享回调，并依序应用写入。

后到队首的 line 如果已提前回调会被停放；成为队首后须重新通过 L0 发起请求。同一周期被禁止的跨行请求也须稍后再试。这些重试、权限竞争和同线合并条件都不能由单个可见下界还原。上面是 Sequencer 内接受顺序和可见规则，不能推断各核之间存在统一全局 FIFO。

## 近似组件

`include/fastsim/spr_ordered_store_visibility.hpp` 为每个模型 Sequencer 保存 `(last_line, last_visibility)`。按**模型实际接受的有序 store 顺序**单遍处理，每项使用模型预测的最终 callback-ready 周期 `ready` 和功能物理 line ID：

```
floor = last_visibility + (line != last_line ? 1 : 0)
visibility = max(ready, floor)
```

首项直接取 `ready`。时间、line 都来自 FastSim 自身模型与功能输入，不读取 OoO 的发射、回调或退休时间戳。该组件每项 O(1)、状态 O(1)，没有逐周期执行或回溯收敛。这里的 `visibility` 是**模型内部的时序下界**：只有候选时间及此前可见估计本身是严格下界时，它才可被称为原生时间的下界；当前尚无此证明。`same_line_at_prior_cycle` 仅表示模型估计存在同周期机会，不代表真的发生同一次 Ruby 回调。

## 未来接线的约束

- 只处理已获得实际模拟 Sequencer 接受资格的普通已提交有序 store，按接受顺序调用；按退休 UOP、地址排序或发射顺序代替都可能改变 FIFO。未知资格、跨行请求、原子操作和屏障必须先单独定义，不能无依据地当作普通 store。
- 每核状态独立，chunk、预热到测量期间保留 frontier；清零统计只用 `reset_counters()`。投机执行/回滚场景应以副本预览并仅在接受后提交状态，不要对失败发送占位。
- 相关模型的已知延迟反馈要在调用之前合并；状态一旦前进不能事后抬高历史项。若现有两阶段数据流不能保证候选确定且接受顺序可用，则保持该组件未接入，不能用整窗反复收敛修补。
- 此组件只约束有序可见时间的模型下界，不单独执行写入、释放 SQ/缓冲资源、分配缓存行或宣称真的得到 Ruby 权限。store buffer 的 line window、同线合并、Sequencer 信用和回调重发仍需各自建模；尤其缺失重发延迟会偏乐观。
- 需要联合现有 store 路径明确 load/store 转发和跨核一致性如何使用该下界，并检查回放开销、CPI/PMU；在此之前不能把单测通过视为端到端精度通过。

## 当前验证

`python3 -m unittest discover -s tests -p 'test_spr_ordered_store_visibility.py'`：两个测试通过。第一项覆盖乱序候选、同线和 A/B/A、边界跨 chunk 的快照/统计重置、溢出；第二项直接从本地 gem5-spr 源码抽取并编译 `Sequencer::isOrderedStoreReady`，用固定种子 20,000 项比较 FIFO 头、同周期异线禁止及得到的估计周期是否满足该判定。测试未建模 Ruby 状态机、缓存权限或重发，也未验证真实负载精度和吞吐。

## 与 committed buffer 的联合试验接口

`include/fastsim/spr_ordered_store_pipeline.hpp` 是尚未接线的独立包装。`admit_sq`/`offload` 调用已提交缓冲摘要；`send_next(now, modeled_callback_ready)` 只在缓冲实际接受发送时提交可见状态，并向有界 FIFO 保存未来回调；`advance(now)` 才按时间释放 line credit 和共享预算。回调时间再加正的 `reclaim_delay`（测试取 1），以避免在同一个估计周期立即把完成通知当成 SQ 可用；该值是模型近似，不是对 gem5 原生延迟的证明。整个 state 可复制用于跨 chunk 提交或回滚，预测未来事件不会推进当前时钟。

对其他未支持的普通或特殊 store，调用者必须按程序顺序先执行 `begin_external_boundary(now)`；该操作要求之前模型内 shared budget 已清空，随后阻止任何新 SQ admission，直到外部路径在其**最终模型回调**调用 `end_external_boundary(now)`。下个 store 不得与未知 line 的完成处于同一周期。这是故意偏保守、可能影响吞吐的隔离方案，不应声称精确还原共享 SQ。同样，必须确保更早的外部请求已完成，才可启动该包装；不能漏计这类请求占用的预算。

以上回调和回收仍只依据 FastSim 预测，只有调用者保证 callback-ready 已包含后续服务/权限反馈，且之后不再重定时，才能避免**在模型内部**提前回收。尚不能证明不会相对 native gem5 提前；若阶段二没有这样的最终时间或不能按事件前沿按序调用，应保留为未接入而非反复迭代。测试 `python3 -m unittest discover -s tests -p 'test_spr_ordered_store*.py'` 三项通过，覆盖反序 ready、两 line FIFO、合并、共享预算、同线并发、异线窗口、跨 chunk 副本和外部边界；没有端到端精度或吞吐结论。
