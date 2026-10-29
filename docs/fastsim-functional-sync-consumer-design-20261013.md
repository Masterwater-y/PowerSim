# FastSim functional synchronization consumer design

日期：2026-10-13。状态：独立设计/代码审核完成，首片已实施并验证；本次用户已授权设计、审核和实施。

## 目标与边界

让现有 libgomp semantic sidecar 在 FastSim 中约束跨核的执行顺序，而不是仅检查 trace。首片支持非嵌套 parallel、implicit task、普通/workshare/final team barrier；保留全部已采集的自旋、futex 和内核指令。默认关闭。仅支持 interval_weave/time_epoch、静态一线程一核和完整初始上下文；marker 可观测的迁移、超额订阅、task/cancel、nested、任意前缀切片和未闭合且非共同终点的输入拒绝。

线程身份是 `(address_space_id, runtime_thread_id)`，不是 CPU 编号。barrier 身份是 `(address_space_id, region_instance, sync_object_id, generation)`；参与者来自 team size/member index，不凭出现顺序猜测。声明的 team size 超过物理核容量即拒绝，不能因共同终点尚未观察到多余线程而放过超额订阅。此版本不会重新选择锁竞争赢家、改变动态调度或删除原 trace 中自旋次数。OS 调度和 spin/sleep 分类未知，不能声称重现目标 OS 调度或完整内存模型。

## 输入、前置检查与无 oracle 的图

新增 `tools/compile_functional_sync.py` 将 portable JSONL 和 metadata、FST/ASMAP、functional boundary 编译成稀疏、版本化的文本 plan。复用现有 validator 的 source/phase/anchor/lifecycle 检查；增加 functional-only 模式。编译和运行均不读取 observation 的 tick 或 sequence。采集端原有带 observation 的验证仍保留，检查采集自身。

图的边包括：每个 source 的 marker 程序顺序；parallel_begin → 所有 implicit_task_begin；所有同代 arrival → 每个 departure；所有 implicit_task_end、所有 final arrival 和 master final departure → parallel_end。**parallel_end 不依赖所有 worker departure**，允许 worker 晚离开。图做拓扑排序和环检测，warmup 事件不能依赖 measurement 事件。已有 validator 在图的拓扑顺序上检查生命周期。缺少参与者、duplicate、unsupported、初始上下文 unknown 都拒绝；共同终点允许开放尾部，但已出现的 departure 仍必须有完整 arrival，不虚构闭合。

plan 保存每核完整 source record count、warmup/measurement record count，以及每个事件的 source ordinal、PC、ASID、kind、phase 和前驱 ID。它是经过验证的预处理产物；运行期仍验证范围、DAG、核数、源人口、每个实际 anchor 的 PC/ASID/retired-user 属性和阶段，不能按模型 UOP 序号定位。`TraceSource::current_source_record_ordinal()` 和 source population 在 FST reader 提供，slice/warmup wrapper 原样委托。syscall 扩展 UOP 不会移动 source ordinal。

## producer/coordinator 交接

每个 marker 的 record 完整处理后结束 producer chunk，并在 queue mutex 下暂停该核 producer。协调器视已知 marker 为 lookahead 的覆盖边界，不阻塞等待尚未许可的下一 chunk。marker 前仍可存在多个普通 chunk；marker 后没有被解码的 UOP，因此私有 cache、predictor、shared hierarchy 都不会先看到后续工作。

只有该 chunk 最后的 UOP 在反馈修正后的 retire frontier 被协调器接受后，才发布事件 readiness。使用 FastSim 的参考时钟；不能使用 baseline 解码时间、epoch horizon 或 gem5 commit_tick 代替 readiness。arrival 发布后直接允许 runtime 继续，避免阻断实际计数器更新和 futex 唤醒。departure、implicit_task_begin、parallel_end 等事件等前驱发布后允许继续。其他核以及已有 memory response 不停止。

放行时计算 `release = max(own_corrected_retire, predecessor_ready)`，以及 `admission = max(release, acknowledged_horizon)`。窗口在 marker baseline/pending corrected retirement 的精确时刻截断，避免通常的 fixed-Q 超前。每个 marker 恢复时，producer 的 frontend floor 使用 admission 转换为 local time、减已有 timing-feedback gap，再向上取整。不能让新请求回到已闭合窗口。跨线程 exposed retirement dependency wait 为 `max(0, predecessor_ready-own_corrected_retire)`，不包括所有 marker serialization/frontend displacement。没有经验 barrier/futex 延迟、synthetic MFENCE 或 cache 清空。真实 atomic/fence/store-buffer 继续用原模型；arrival 在 counter update 前，该机制不能单独证明 barrier 的所有 store 已全局可见。

## warmup、共同终点与统计

同步状态跨 warmup 统计清零保存。只有所有已发出 warmup marker 被解决后才进入原有 common warmup barrier；warmup→measurement 的非法反向依赖在编译阶段拒绝。每核 cursor 在测量结束必须恰好消费全部事件；允许最后一个 region 仍开放，不能等待未采集的未来 arrival/departure。

新增审计：model enabled、plan event count、committed/released event count、dependency waits、每核 exposed retirement dependency wait、elapsed timeline cycles 和 active-cycle sensitivity。wait_mode=unknown，不能把所有新增 gap 断言为 sleep 或 spin：保留现有模型 CPI（gap 属于阻塞前端的 elapsed cycles），另给出仅扣除该 exposed wait 的条件性 active CPI 敏感性值；正式 native active CPI 认证还需要消除 marker serialization 近似并补充 wait-state/OS schedule 信息。真实 trace 中自旋/内核指令仍计数，不会扣除。

## 验证与设计风险

机制测试覆盖：不在 arrival 停核；2 核不同到达时间；后继 memory 不先于 admission frontier；跨核反馈后的 readiness；晚 worker departure；对象复用/generation；syscall 扩展与原始 ordinal；phase seam；合法共同终点开放尾部；缺参与者/环/迁移/unknown context 拒绝；删除或任意修改 oracle 不改变 plan；默认关闭保持预测。

旧 FS probe（3 regions / 9 barriers / 102 events）实际存在 marker 线程迁移，用于拒绝用例；普通 barrier 用机制测试覆盖。TeaLeaf C4（959 events / 54 regions，最后一 region 被共同终点截断）做真实回放；复用既有 FST，新增 FST 为 0。CPI/PMU 来源按 accuracy-reporting-contract；所有相对 CPI 误差同时列绝对误差。runtime 自旋人口固定、未知调度、memory release 边不完整以及当前硬件参数近似都应明确，不以同步后的误差是否减小决定设计正确与否。

## 审核记录

独立审核发现的关键问题均在下一节记录，并落实到实现及回归用例。

### 第一轮独立审核修订

- marker 被 compact 后仍需协调器持有 `sync_frontier_known`，否则 lookahead 会阻塞在 acquire_chunk；不能只检查 resident 尾部。
- terminal marker 先完成同步 ack 再设置 finished，warmup 统计清零不删除图状态。
- 所有空 epoch/candidate 扫描跳过 blocked-empty resident；pending corrected retire 参与 next-progress 下界；无可推进前驱时显式报错，不能 host 死等。
- release 为 reference time，frontend floor 先转为本核 local time，再减本核 feedback gap；gap 审计按核保存，不混加不同频率的周期。
- `response_paired_frontier` 的 open tail 不能忽略；首片明确拒绝 paired frontier、event-only approximation 和 context execution。普通 response feedback 取 accepted marker 的 corrected end 与 committed response_commit_cycle 的较大值。
- 初轮曾采用前驱 readiness floor 保留 OoO overlap；二次因果审核否决此方案，最终使用所有 marker 的 admission floor，详见后节。

- 审核澄清：exposed retirement dependency wait 不等于所有 frontend floor displacement，也不等于可证实的 OS sleep。敏感性值仅扣除前者，不能当作完整 active CPI 下界。

- 运行 API 首片仅支持 `run()`；execution windows / DVFS 明确拒绝。静态不同核频率保留 reference/local 换算。
- 静态绑定验证只能检查 marker 观察到的 thread/core 映射；缺少 OS schedule 时，不能证明两 marker 之间从未发生迁移。
- plan 是本地 compiler 生成的受信预处理产物，运行时的结构/anchor 检查不替代 compiler 的完整 lifecycle/source 校验。不能手工删边后把 plan 作为已验证输入。

### 因果窗口二次审核（取代前驱下界方案）

前驱就绪下界不足以保证 epoch 闭合：marker 自身在 t=100 退役、前驱在 t=80 就绪，若协调器已经提交到 t=128，之后按 t=80 恢复会产生回到已提交窗口的请求。新增回归确实发现此问题；旧 replay-on 只能作为已否决实现的诊断快照。

最终方案在 resident marker 的 baseline retirement 和 pending marker 的 corrected retirement 处截断 proposed horizon，empty-epoch fast-forward 也受此限制。所有 marker（包括 arrival）恢复时前端 admission floor 为 `max(release, acknowledged_horizon)`；转换为 local time 后减既有 feedback gap。departure 的跨线程前驱仍是所有 arrival；arrival 不等待其他线程，但必须完成自身 marker 的交接。共同 warmup seam 对每个核也安装前端 floor，包括最后一个 marker 后仍有普通 warmup 指令的情形。

该首片为保持 producer/coordinator 的因果闭合，在 instrumentation marker 处保守串行化；原硬件可允许的 marker 自身前后 OoO overlap 会减少。这是模型近似，会影响 CPI，不能当作真实 barrier 固有延迟。未加入任何经验 latency 常数。corrected-suffix carry、causal-block transfer 明确拒绝；warmup 目前要求所有核等于 reference frequency，measurement-only 可用静态不同频率。运行期核对 post-boundary corrected memory issue ≥ admission frontier，并输出逐 marker 审计。

## 使用方式

```bash
python3 tools/compile_functional_sync.py \
  --trace-dir /absolute/path/to/tao_trace \
  --output /absolute/path/to/validated.plan \
  --allow-truncated
```

`--allow-truncated` 仅允许采集端声明的合法共同终点开放尾部；完整 workload exit 可省略。在原来的配置 overlay 添加 `sync.plan = /absolute/path/to/validated.plan`，再用原来的 `fastsim simulate --config ... --manifest ... --cores 4 --measurement-scope user-plus-kernel --output ...` 回放。删除该配置项即关闭模型。开启不需要 observation 文件；原有带 observation 的 validator 可独立用于采集审计。

输出 `functional_sync` 包含每个测量 marker 的 corrected retire、predecessor readiness、release、admission 和 first post-boundary memory issue（reference Q16）、全程/测量事件计数和 exposed dependency wait。`scope_metrics.perf_like_cpi_status` 显式标记 `functional-sync-elapsed-wait-mode-unknown`；不得把它认作 strict active CPI。conditional CPI 使用同一 macro denominator，仅表示把 exposed dependency wait 假设为非 active 的敏感性值，不撤销 marker serialization，也不能当作完整下界。

首片是可验证的 execution-boundary consumer。要扩展到迁移/超额订阅，需要 OS thread identity 和调度事件；要重模拟 wait population，需要 spin/sleep 分类和 runtime 协议状态；要认证完整 barrier memory HB，需要补齐 release/acquire 的 visibility 边。QEMU/DynamoRIO 可生成同样的 functional facts，经相同 compiler/consumer 进入模型；本次没有实现这些采集适配器。
