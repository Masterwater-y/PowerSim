# Store prepare 请求生命周期与两阶段接口设计

用户已批准将会话分析落成文档并开始实施（2026-10-21）。本文区分完整目标与首个可验证交付；实施进度见[计划](../plans/2026-10-21-store-request-engine.md)。

当前状态：首片组件已完成；后续已加入显式epoch闭合桥接路径，见[接入设计](2026-10-21-store-engine-integration-design.md)。完整跨epoch在线continuation与精确瞬态协议仍未完成。[首片结果](../../store-request-engine-phase1-results-20261021.md)保留其当时的验收范围。

## 1. 问题与证据

store prepare 是提前获取缓存行写权限的请求，不是 MSHR。SQ 保存 store；TBE/MSHR 跟踪协议事务。本次 gem5 buffer 扩展允许多个满足 canWB 的 prepare 同时在途，真实写仍受 FIFO 和 storeInFlight 约束。它的 prepare 经 optional queue，绕过普通 Sequencer outstanding 检查；不能把所有 prepare 都扣普通 load 的 Sequencer 槽。SPR 的对应硬件概念是 RFO，不能据此认定两者的逐周期发射规则和容量已经相同。

当前 FastSim 已有 permission-only、FIFO publish、真实 demand 和 SQ 释放，不是完全没有 store buffer 模型。缺口在于这些动作尚未由同一条真实准入时间线管理：

| 已确认的问题 | 当前位置/证据 | 影响 |
|---|---|---|
| 批量安全门禁改变目标机器行为 | `prepare_buffer_tso_epoch`、`buffered_tso_permission_prepare` | 同set后缀不能证明驻留就禁prepare；真实机器仍可准备，再发生驱逐/重新取权 |
| 完成下界代替真实准入 | `compute_core_timing_feedback` 中 permission_ready=max(canonical response, retire+propagation+service) | 没有随之重排optional queue、端口、TBE服务与cache效果 |
| 部分cache/目录状态按canonical顺序先更新 | `replay_memory_event`、`SharedSystem::access` | 后移完成时间不等于修复命中/权限/驱逐的先后关系 |
| mandatory接管依赖估算时刻 | `replay_buffered_tso_head_demand`、三种buffer_tso_role | 缺少贯穿enqueue、实际服务、接管、回调的统一身份 |
| 权限有效性偏重epoch检查 | `buffered_tso_permissions_intact` | 尚未完整处理准备后失效、队头重新取权和跨epoch回调 |
| 部分PMU按推断角色补计 | `publish_buffered_tso_stores` 的SP_IE→IM计数 | 角色时序错误可能影响计数；大偏差尚不能唯一归因于重复计数 |

XZ窗口含memmove与clear_page_rep/REP STOSQ，大量写使过严prepare门禁沿FIFO/SQ反压放大；CPU0又决定共同终点。单开证书的同源控制证明此路径敏感，不证明全部误差已经归因。详见[排查报告](../../cpi-sqlite-marian-xz-analysis-20261021.md)、[第一批修复结果](../../cpi-sqlite-marian-xz-repair-results-20261021.md)。新的按请求驻留拒绝修复已落地，但没有修复上述完整时序。

## 2. 目标与边界

1. prepare是否能发由canWB、地址/翻译、扫描规则、实际端口/队列/协议资源决定。
2. 证书只选择批处理或逐事件执行；相同仲裁下目标周期、请求及PMU应一致。
3. 一个store可没有prepare；到FIFO头时普通demand仍必须正确完成。
4. enqueue、service start、permission return、mandatory write response、SQ release是不同事件。
5. FST只提供功能事实；gem5时间/命中/错误路径不能作为FastSim输入。QEMU/DR后端可提供相同功能语义。
6. 缺失错误路径、硬件预取和其他CPU资源模型不会被此设计自动补齐；不承诺误差百分比。

本轮首个交付为独立生产库组件 `StoreRequestEngine`：有限SQ、端口、可配置容量的optional queue、有限取权事务资源，以及异步回调和事件账。它是完整MemoryRequestEngine的store生命周期基础，**尚不是现有SharedSystem的真实cache准入替代物**。先把边界做成可运行参考契约，再接两阶段；不能把组件测试称为CPI修复。

## 3. 身份与状态

沿用 `PendingOwner{core,sequence,fragment,generation}` 作为store身份；首片只支持普通、单fragment、可缓存store，不接受split/atomic/NO_ACCESS冒充普通store。已接受hint、mandatory请求和物理事务分别有独立且不复用的请求ID；事件同时记录逻辑request与物理transaction，跳过的hint也不能使用零哨兵。SQ回收后不保留无限完成ID历史，以单核递增sequence和在途身份拒绝旧callback。

生命周期：

`SQ allocated → address ready + retired → canWB → optional enqueue → service start → permission ready`

FIFO头mandatory路径：

| 当时状态 | mandatory行为 | 释放 |
|---|---|---|
| 已有有效权限 | 接受本地真实写 | 真实写response释放SQ/mandatory槽 |
| prepare在途 | 接管同一事务，禁止重复取权 | permission response只释放取权资源，真实写response才释放SQ |
| 无有效prepare/准备后被驱逐失效 | 创建真实demand取权 | 同上；不能靠旧驻留证明直接写入 |

prepare入队成功后可在协议侧被合并/跳过；已接受的一次性hint不会因失效无限重发。被拒绝的enqueue可重试。已接受mandatory的取权若失效，协议重试保留同一个mandatory身份，旧回调仅释放自己的资源，不能授权新代次。

首片提供外部cache适配边界：`observe_permission(line)`、`invalidate(line)`；该组件不保存完整cache数组，backend必须在实际命中、填充、驱逐、失效时调用。version由组件内部维护，外部hook表示当前时刻真实发生的权限变化。返回事件只有在真实服务完成时调用，不能把未来完成预测当作已完成回调。

## 4. 资源与事件API

- `allocate`：分配SQ，满时拒绝且不改状态；`address_ready`、`retire`、`can_writeback`分别输入已发生的模型事件。
- `advance_to`：单调推进当前时刻，不擅自完成未知事务。
- `enqueue_prepare`：检查扫描前缀、canWB、端口、一次性hint、optional queue容量；enqueue成功不是TBE已分配。
- `start_prepare`：optional queue延迟后处理队头；已有权限或在途同line时跳过；实际新取权才分配有限事务槽。
- `send_head`：CPU侧接受队头mandatory并占端口/单个mandatory槽，返回DemandQueued；此时不检查命中或占TBE。
- `start_head`：mandatory queue延迟后开始实际协议服务，按当前权限命中、接管或新取权；TBE满可阻止service，不能反向阻止已经允许的CPU入队。mandatory与optional的入队和service分别建模。
- `permission_response`：按事务ID释放取权资源；只有当前有效代次可建立权限。
- `retry_head`：失效事务返回后，给仍在途mandatory重新取权，不另占SQ/mandatory槽。
- `write_response`：检查真实mandatory身份、权限与前置依赖，然后释放SQ。
- `EventSink`：流式输出请求事件；默认不积累全历史。内部仅保留SQ、活动optional队列、pending事务、被引用line状态和计数。sink只允许调用只读getter，不允许重入任何修改接口；sink异常向上传播并使实例禁止进一步修改，调用方必须终止该次运行，不能把部分接受的请求当成未接受后继续。

容量与时间由profile显式提供。optional queue容量0表示目标未设置该容量限制，不能把无界队列误当零槽。已接受hint可超过来源SQ的寿命，因此无界配置下内存随实际活动队列增长，不能宣称固定受SQ/TBE容量限制；有界宿主策略必须显式配置、单独注明与目标队列的语义差异。端口带宽、SQ容量、取权事务容量分别计量。首片只有一个核心实例；跨核coherence由外部backend送入invalidate/permission事件，不能声称已经实现多核cache协议。

所有时间加法检查溢出；正常容量拒绝、非法输入及时间溢出不留下已接受请求的资源或计数。allocate在新增line分配失败时回滚SQ；sink异常按上文终止契约处理，并非可继续的正常拒绝。保持 `initial occupancy + allocations - releases = current occupancy`。同周期仲裁顺序由调用契约明确为先处理返回/失效，再mandatory，再optional；首片检查已发optional后再发同周期mandatory的错误用法。

## 5. 两阶段接入与快速路径

阶段一 `IntervalCore` 只输出访存意图、可证明的最早时刻及依赖，不提交最终cache效果。阶段二以真实准入为唯一提交入口：预留瞬态状态/TBE，服务返回后才建立有效数据/权限；返回后按依赖推进核心。

每核公布下一个可能发出请求的时间下界；共同提交frontier还受在途回调、同步释放约束。不能处理更晚请求后才发现更早的另一核请求；未决DRAM返回不能假装已经固定。跨epoch保留身份、资源和等待者，chunk回收不能释放事务。

证书通过时可以批量推进同一状态机；失败时退到逐事件推进，不得以禁止prepare改变目标语义。已有PendingOwner、pending resource和transaction代码可复用，但现有读请求实验路径不是完整store实现。

## 6. PMU与验证

分别记录 PrepareEnqueued/Skipped、PermissionStart/Return、DemandAccepted、DemandTakeover、WriteResponse、SQRelease和Retry。一个内部prepare不增加架构退休load/store；一个真实store仍按宏边界只退休一次。native tag miss按实际协议转换计数，SP_IE→IM发生时才计其对应事件，不能按一次估计分类永久补计。资源/生命周期事件账与native/perf退休命名空间分开。

先验证双冷store重叠、同line不重复取权、队满重试、接管前后边界、完成后失效、在途失效与旧callback、跨epoch、SQ满和时间溢出。再对SQLite/XZ/Marian用维护validator比较default、单证书、单lifecycle、组合；保持原FST、参数与共同终点，每个CPI误差同时报告cycles/macro绝对差，PMU保留unknown。

若新增gem5观察，则记录源码/二进制身份并使用新绑定checkpoint；事件仅作oracle。gem5编译用可用核80%；所有FST合计<=100,000,000,000 bytes，保留活动基线。吞吐需独立安静宿主测量user UOP/s，不把审计耗时当吞吐。

## 7. 本轮验收范围

首片验收：上述生命周期组件、真实C++契约回归、现有完整测试、独立审查；记录生产主模型仍未接入的事实。下一片必须由真实SharedSystem适配器驱动状态与资源，并闭合core continuation后，才能发布新的CPI/PMU修复结果。不能用一个返回固定延迟的toy backend替代该验收。
