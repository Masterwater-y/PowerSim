# FastSim 统一父服务设计修订：prepare、接管与独立完成边

日期：2026-11-10。状态：可审查设计稿，尚未实施本修订。

适用基线：`tmp/parent-service-exclusive-20261110/source/` 中的私有侧 S0/S1 隔离实现。
与[原设计](fastsim-unified-parent-service-design-20261110.md)的关系：保留单一权威记录、有界存储、两阶段和一次反馈；本稿覆盖原稿中 prepare 资格、响应边、资源生命周期和实施顺序的相关表述。

## 1. 决策

**父服务按实际事务建立，由 load、prepare、store 或预取发起均可。每次实际事务只有一份权威记录；请求角色、数据就绪、控制器可服务性、写权限和资源释放分别表达。**

保留 prepare 的生成与 store buffer / FIFO 机制。对已有记录的接管改变消费角色，不重建已经发出的下层事务，不重新收费，不用提示确认代替数据回填。

本修订优先完善私有侧闭环，不直接扩展 LLC、预取合并或延迟 Tag 发布。所有尚未验收的能力保持默认关闭；本次没有修改 C++、重新仿真或执行 native 审计。

### 1.1 已确认事实与未确认结论

| 分类 | 结论 |
|---|---|
| 冻结 gem5-spr 行为 | I+prepare 发 GETX、取得旧行数据和独占权限；S+prepare 发 UPGRADE；E/M 或已有 transient 中的 prepare 不新建同类事务 |
| 冻结 gem5-spr 行为 | SP_IE 阻塞同线 load；store 可接管 prepare，提示 credit 可先于回填完成释放 |
| 当前 FastSim S1 | 普通物理 demand-read 可建立数据父记录；写交互行保守排除；`data_ready` 来自该请求最终响应 |
| 当前 FastSim prepare | `LegacyPrepareTiming` 自己保存 pending 提示及其时间，尚未与统一父服务共享事实 |
| 尚未证明 | 此缺口占 Marian / FLAC CPI 残差的比例；修订后精度方向和宿主吞吐变化；SPR 真机是否与 gem5 的 SP_IE 阻塞策略完全相同 |

证据见[本批 prepare 设计核对](fastsim-prepare-parent-gem5-design-check-20261110.md)及[Marian / FLAC S1 分析](fastsim-marian-flac-s1-analysis-20261110.md)。前者已核对实际采集的配置与冻结 ELF；无需为本设计再采集 gem5 动态时间。

### 1.2 为什么只扩展 `ordinary` 条件不够

直接把 prepare 加入现有 `resolve_parent_service()` 会留下四类问题：

1. I 状态需要等待数据；S 状态数据已经存在，但控制器可能暂不接受 load。单个 `data_ready` 无法区分。
2. 当前父 load 的最终响应可能含该请求的容量等待、返回端口和 CPU callback；把它给所有 follower，会继承不属于它们的边。
3. prepare credit 完成、TBE/fill 释放、实际 store 写入不是同一时刻。接管若复制记录，会重复分配；若统一清空，会过早释放。
4. 现有 prepare 和统一池各自维护时间，再叠加两套等待，会形成重复约束来源。

因此先改服务事实接口和所有权，再改变等待规则。

## 2. 服务身份：事务代次与数据代次分开

```text
RequestId = {core, stream_epoch, sequence, member/fragment, role}
ServiceId = {owner_domain, pool_slot, slot_generation}
LineKey   = {owner_domain, physical_line, access_attributes}
```

记录另外保存：

- `transaction_epoch`：这一轮 fill / upgrade / writeback 的事务身份。
- `content_epoch`：所供给的功能数据版本及其有效性依据。
- `permission_epoch`：权限承诺的版本。
- `cache_cookie`：本地 Tag / way 代次引用。
- `clock_epoch`：模型时间映射的版本。

这些版本可以引用已有功能序号或控制器账本，不要求 trace 提供不存在的微架构元数据。无法建立有效映射时输出 unknown，不能凭同一物理地址猜测数据版本。

**同一旧数据可以经历多次权限事务；同一事务也可能由 prepare 创建、后来由 store 消费。** 因此数据代次、事务代次、槽复用代次和请求序号不能合成一个 `generation`。

每个控制器实际事务只有一个事实拥有者。私有 L1、私有 L2 和共享层可以有不同的局部服务记录，以 ID 引用共同下层供给；各层返回和资源不复制成整机统一时钟。

普通完成驻留 hit 不新建服务。权限升级需要事务记录，即使不需要新数据 fill。同一行的有限候选链容纳必要的不同事务代次，链长与父图深度固定有界。

## 3. 在功能副作用之前提取 `ServiceFacts`

新增分类入口位于 `replay_memory_event()` / `PrivateHierarchy::access()` 的功能变更前后边界。分类依据访问前状态与实际服务动作，不能在立即 `install()` 后反推该行原本已驻留。

```text
ServiceFacts
  request_id, line_key
  pre_access_residency / permission / transient
  content_epoch, permission_epoch, cache_cookie
  action = no_transaction | fetch_shared | fetch_exclusive
         | upgrade | attach | takeover | writeback | special
  lower_transaction_issued, lower_service_id
  allocates_tbe, allocates_data_fill
  data_edge, permission_edge, controller_edges
  selected_admission, source_issue, clock_epoch, time_quality
  supported / unknown_reason
```

`pre_access_residency` 可以使用模型已有 cache/directory 状态。模型缺少稳定权限或 transient 事实时，不能仅由 `event.write`、`private_level` 或命中结果推导 UPGRADE/GETX。

首次迁移保持原服务选择和时间不变，显式标注 `LegacyCoupled` 的完成边。随后逐路径拆出缓存数据到达、本地 controller callback 和 CPU 可见响应。无法拆开的路径保持范围限制，不能把旧 callback 改名为精确 data-ready。

时间表示必须同时包含：

```text
value + clock/domain + validity + producer
validity = unknown | lower_bound | upper_bound
         | selected_fixed | final_local | legacy_coupled
```

`selected_fixed` 表示本窗口模型选定，不表示 gem5 或硬件真实时刻；未知值不以 0 代替。

内部沿用已有 Q16 时间精度；跨核/参考时钟比较先通过已认证的 horizon 映射转到同一域，输出周期数时再按原合同取整。不能先把多条边各自向上取整，再将新增整数差当作服务开销。

## 4. 权威记录和请求引用

### 4.1 概念布局

```text
ParentService
  id, line_key, transaction_epoch
  creator_request, immutable_origin
  phase, active_consumer_role
  content_epoch, permission_epoch, cache_cookie
  parent_source_id
  admission_edge, source_issue_edge
  resident_data_edge / fetched_data_edge
  permission_edge
  controller_policy, controller_completion_edges
  leases[fixed_bound]          // service-owned TBE / fill / lower resources
  target_validity, publication_refs
  reference_counts, version

RequestSlot                    // 复用已有有界 request / event / feedback 槽
  request_id, service_id
  required_content / required_permission
  eligible / local_admission / send
  callback / consume_edge
  request_credit_or_token     // prepare credit、请求槽等自身资源
  gate_refs[fixed_bound]
```

没有永久 follower vector，也没有另外一张可独立改变行级完成时间的 pending-fill 表。请求槽保存每个请求自己的回调和资源身份，统一记录保存实际事务的边。行索引、释放索引、发布队列、cache cookie 只引用权威事实。

`phase` 首版只表达现有路径需要的少量阶段，如 `fetch_pending`、`upgrade_pending`、`store_takeover`、`ready`、`target_invalid`；控制器门由固定请求类掩码与完成边推导。它不复制 gem5 完整 SLICC 状态机，不建立逐周期协议或独立详细因果运行时。

`creator_request` 和 `immutable_origin` 在接管后仍是 prepare；`active_consumer_role` 可以变为 store。源分类与消费分类都保留，不能因接管把 prepare 流量改计成新 store 流量。

### 4.2 独立所有权

| 事实或资源 | 唯一拥有者 | 说明 |
|---|---|---|
| 行数据到达、权限取得、控制器事务结束 | 对应域 `ParentService` | 其他路径只读或引用 |
| TBE / data-fill lease | 实际分配它的服务 | 接管保留原 lease；升级可能只有 TBE |
| prepare credit | 该提示的请求槽 | 可引用事务结束边，也可在接管/丢弃时独立完成 |
| 普通请求 token / 返回回调 | 请求槽 | follower 保留自己的准入规则 |
| store buffer / store group / FIFO | 现有 store 对象 | 父服务不能代替或额外复制这些资源 |
| DRAM / LLC 下层服务 | 下层域现有服务，迁移后为其统一记录 | 私有侧保存父 ID，不建立第二份 DRAM 日历 |

prepare credit 的释放索引保存请求 ID 和相关服务引用；它不保存另一份权限时间。提示完成后，若 TBE/fill 仍占用，服务仍存活。

## 5. prepare 状态合同

下表是本批 gem5-spr 对齐策略的目标语义；FastSim 只能用退休功能事实与模型内生时间进行有界近似。

| prepare 到达前的状态 | 实际动作 | 统一记录 | 同线 load | 后续 store |
|---|---|---|---|---|
| I，无有效数据且无兼容在途事务 | GETX，分配 TBE 和 data fill | 新 `fetch_exclusive`；旧行数据及权限各有边 | 需要正确数据版本，并满足控制器恢复边 | 可接管同一事务，或完成后使用权限 |
| S，有有效旧数据 | UPGRADE，分配 TBE，无新 data fill | 新 `upgrade`；引用已驻留数据 | gem5 SP_IE 策略下等控制器恢复；不能统计为缺数据 fill | 接管升级或等待权限后写入 |
| E/M，已有足够权限 | 提示确认/出队 | 无新下层服务 | 不因该提示新增回填等待 | 使用原写入和 FIFO 规则 |
| 已有兼容 transient | 提示确认/出队，原事务继续 | 只引用原服务；不重复 GETX/UPGRADE | 原事务的数据与控制器规则 | 是否可接管由状态与权限合同决定 |
| 非兼容 transient、未知权限或代次不明 | 原路径处理 | 保留 unknown / unsupported 原因 | 不冒用其他代次的数据 | 不猜测接管 |
| 提示 credit 满 | 丢弃可选提示 | 不制造未发出的父服务 | 没有该提示造成的新服务 | mandatory store 完整保留 |
| prepare 未发出前被取消 | 取消提示 | 不保留不存在的流量 | 无该源依赖 | 后续需要时正常发服务 |
| 发出后失效/取消发布 | 原已发事务继续收尾 | 撤销目标使用资格，保留 lease | 新请求不得消费失效代次 | 重获权限时新建必要事务 |

协议 transient 中的提示确认并不意味着其需求已满足。确认只归还可选提示资源；mandatory store 必须继续检查原服务能否提供所需数据/权限。

S 状态控制器阻塞只作用于相关域、相关行和受阻请求类，不暂停整个核或把所有 load 都绑定到某个 store callback。`controller_policy` 首版固定为已核对的 gem5-spr 对齐策略；对真机策略的变更必须有独立证据，不能按 workload 名称选择。

## 6. load 响应：数据门和控制器门分别计算

设：

- \(a_r\)：请求通过自身必要准入后的时刻；
- \(d_r\)：所需数据在目标层可用的时刻，且数据版本有效；
- \(b_r\)：该控制器对该请求类解除阻塞的时刻；
- \(\ell_r^{post}\)：解除这些约束后必须执行的本地路径。

当上述三条边先于同一个本地服务节点时：

\[
g_r=\max(a_r,d_r,b_r),\qquad
t_{\mathrm{resp},r}=g_r+\ell_r^{post}.
\]

其余并行边按原路径保留。例如仅对已审计为“响应边前直接汇合”的路径，才能用：

\[
t_{\mathrm{resp},r}
=\max(t_{\mathrm{local,min},r},d_r,b_r).
\]

不能把第二式无条件用于需要醒来后重新处理 mandatory queue、lookup 或返回端口的请求。也不能在 `b_r` 已包含相同本地动作时再次加一遍 \(\ell_r^{post}\)。

统一的实施规则是有界边图：

\[
t_v=\max_{u\in pred(v)}(t_u+\ell_{u,v}).
\]

边来自已有路径的传输、端口和 callback 合同，图深度由层级与请求类型固定限制；不引入每周期事件，也不额外指定任意 `+1/+2` 校准常量。

该边图是请求路径上的固定计算模板，在已有 replay/feedback 中求值；不保存全 ROI 指令依赖图，不另建一个全局事件推进器。

### 6.1 三个可区分反例

1. **prepare 在 I 发 GETX，数据尚未到。** 提前 Tag 不能使 load 绕过 `d_r`；若还需 controller re-dispatch，继续执行一次相应局部边。
2. **prepare 在 S 发升级，旧数据已在。** `d_r` 已满足，SP_IE 策略仅通过 `b_r` 阻塞。不能把等待归类成缺数据，也不能为它新分配 data fill。
3. **没有受阻状态的驻留 read，某 store callback 尚未完成。** 只检查该 read 的数据版本、转发和实际控制器条件，不无条件等待该 store callback。

store 写入产生的新内容边与 GETX 得到的旧行数据边分开。load 需要旧行数据、同核转发数据还是已写入的新版本，由功能成员和现有 forwarding / ordering 合同决定。缺少必要身份时输出未认证覆盖，不能把 prepare 得到的旧数据当成 store 已提交的新值。

## 7. store 接管：复用事务，保留发送与写入边

接管前后服务 ID、下层 source、已分配 TBE/fill 和已选定下层时间不变。允许变化的是消费者、控制器阶段、该请求的回调及有证据支持的发布动作。

```text
Prepare accepted → GETX / UPGRADE issued
                        │
                mandatory Store arrives
                        │
           同 ServiceId 转为 store 消费阶段
              ├─ prepare credit 完成
              ├─ 原 TBE / fill 继续占用
              └─ 数据与权限满足后实际写入，再回调
```

store 的资格分成三组：

1. **prepare 资格**：功能地址/翻译可得，符合已有提示生成规则及提示 credit；不需要架构写数据已可用。
2. **mandatory 发送资格**：地址与写数据已具备、既有退休/发送合同、FIFO/顺序前驱、发送端口与请求容量；允许在权限事务尚未完成时接管。
3. **实际写入资格**：发送已发生，所需数据及写权限有效，写入端口和原发布约束已满足。

对可归约的路径：

\[
t_{\mathrm{send}}
=\max(t_{\mathrm{addr}},t_{\mathrm{store\ data}},
      t_{\mathrm{retire/send\ gate}},t_{\mathrm{FIFO}},
      t_{\mathrm{port}},t_{\mathrm{request\ capacity}}),
\]

\[
t_{\mathrm{write}}
\geq\max(t_{\mathrm{send}},t_{\mathrm{required\ line\ data}},
         t_{\mathrm{write\ permission}},t_{\mathrm{write\ port}}).
\]

实际写入后的 callback 继续使用已审计本地边。全行覆盖等专用路径是否需要旧数据由其真实协议决定，不在本设计中统一删去 GETX。

**不能把 permission-ready 一律加入 `t_send`**，否则会禁止本批协议允许的提前接管；也不能把 prepare 已使用的 GETX 完整延迟再次加到 store response。

store buffer 容量释放仍依据原合同，不能因父服务合并另加一份 blocker。detached store 不因回填自动持续占 ROB。统计区分 `eligible→send`、`send→write`、`write→callback`，才能检验 Marian 的准入与 drain 问题。

## 8. 各资源使用各自释放边

每个 service-owned lease 保存：

```text
pool / domain / kind
begin_edge
release_edge_ref + validity
allocated / released + version
```

release 可以引用 lower response、local fill、permission/controller completion 等有类型边；只有资源合同明确如此时才引用 request callback。E/M 提示无 fill lease，S 升级无 data-fill lease，I GETX 按实际分配人口持有资源。

同一事务的 lease 只分配一次，接管不改变分配人口。若协议转换确实需要另一个事务或资源，以独立、有依据的增量动作表示；禁止“接管必然免费”或“接管必然再付全额”的统一假设。

占用按半开区间：

\[
O_p(t)=\sum_{\lambda\in p}
\mathbf 1[a_\lambda\leq t<e_\lambda]\leq K_p.
\]

在 t 释放的资源可用于 t 的后续准入；同周期协议状态转移仍按拥有者的确定性次序解释，不能由半开区间独自决定是否已经写入新数据。

现有 S0 两层 checkpoint-local MSHR 日历首先中性迁移；不得在结构迁移时悄悄改变释放语义。下一阶段再修订已认证 lease，同时记录旧近似与新语义的差别。

`LegacyPrepareTiming::pending_` 目前按 `response<=eligible` 清理提示人口。修订必须把这里变为请求/服务 ID 引用，并分别迁移 credit 释放与权限边；接管提前归还 credit 是独立行为改动，不能与中性迁移合并验收。

## 9. 两阶段、源资格与一次反馈

### 9.1 保持现有执行框架

第一阶段核 worker 生成退休功能请求、已有 prepare/store 资格、预测时刻及候选引用。第二阶段沿既有确定性排序执行一次功能服务选择，产出 `ServiceFacts` 和有界服务/资源修改；CPU feedback 计算最终局部响应与准入。

反馈完成后统一提交服务、cache、lease、索引和统计 delta。共享拥有者写其服务，核 worker 只读冻结共享事实并修改自己核的局部试算状态；不逐请求增加共享锁，不反复重仲裁整窗。

### 9.2 时间冻结必须有资格证明

父源不能只因为“退休序号更早”就作为时间 leader。记录源的可证明 eligible/admission 区间、模型选定的 `source_issue`、clock epoch 与功能状态版本。

若 \(U_{\mathrm{accept},s}\) 是已经证明的父准入上界，可用的充分时间条件包括：

\[
U_{\mathrm{accept},s}\leq t_{\mathrm{source\ issue},s},
\qquad
t_{\mathrm{attach},r}\geq t_{\mathrm{accept},s}.
\]

同时验证数据/权限/控制器代次及实际分配资源；单靠上述时间不等式不构成功能状态证明。若只有下界，不能把下界当已接受时刻。

现有实现的 `final admission == selected + admission_delay` 是普通读路径的保守认证入口。本修订不简单放松成 `<=`；应先证明 prepare/store 对应的因果和时钟映射，必要时保留更窄范围。

父源被最终反馈否定后，不能把已经发出的时刻向后平移、同时保留原 DRAM/共享排队结果；也不能靠 `max(old_ready, corrected_issue + latency)` 掩盖来源倒置。

### 9.3 阶段范围与失败处理

- 先覆盖跨窗口已接受且时钟/代次有效的服务，以及可预先认证的同窗口关系。
- 未认证请求完整走该路径已有有界行为，保留人口与原因。
- 可能在反馈后失效的关系，只有在提交前可原子撤销完整依赖组件时才能启用；组件和 journal 固定有界，最多一次局部 legacy 回退。
- 若共享修改、CPU 依赖或 cache 副作用无法隔离，提前限制能力范围。不能对已提交的父服务局部“补时间”。

prepare 时间由退休 store 的功能地址、翻译和现有模型资格生成。保留合法的提前提示近似，但不添加 wrong-path store、gem5 LSQ 动态时刻或 speculative 流量。模型资格与真实 OoO 资格的差距单列为输入/近似覆盖限制。

## 10. API 与生命周期不变量

建议接口名，尚非已实现 API：

```text
classify_service(request, pre_state, replay_actions) → ServiceFacts
decide_service(facts, admission_certificate) → Decision + ServiceId
attach_request(id, request_id, required_edges)
take_over(id, store_request, phase_certificate)
resolve_request(id, request_gates, controller_policy) → ResponseEdges
lease_edge(id, lease_index) → typed release fact
invalidate_target(id, target, effective_edge, generation)
commit(delta) / rollback(delta)
collect(id, owner_watermark)
```

`take_over()` 的事务后置条件：

```text
same ServiceId / lower source / immutable origin
no duplicate lower issue
no duplicate TBE or fill lease
prepare credit follows its own completion rule
store request token and callback still exist
old data edge is not overwritten by store data publication
```

回收条件同时要求：

```text
all owned leases released
AND no pending permission/controller/publication action
AND no request/child/snapshot/cache-cookie references
AND safe owner watermark permits reclamation
```

不能在 `data_ready` 到达时直接删除升级状态或接管引用。安全 watermark 是所有后续合法查询的下界，不是当前遍历到的 OoO issue，不因 chunk 结束或较晚核的访问跳跃。

失效区分目标与来源：核 0 的本地 eviction 撤销其目标使用资格，不能自动撤销核 1 共享的下层供给。真正内容/权限变更按有效边和代次传播；取消发布不取消已发出的资源区间。

## 11. 覆盖矩阵与扩展接口

| 场景 | 本修订要求 | 首版启用条件 |
|---|---|---|
| 普通 load miss / 提前 Tag hit | 数据边、局部控制器边及请求 callback 分开 | 当前读源认证重新通过 |
| prepare I/S/E/M | 按第 5 节区分真实事务 | pre-state 和服务动作可证明 |
| store 接管 / prepared write / direct / dropped | 同源复用与 mandatory 人口完整 | FIFO、资源、实际写入合同可表达 |
| 同核 forwarding、同线写后读 | 精确请求内容身份和转发边 | 原 forwarding 身份有效 |
| split / 多成员宏指令 | fragment 服务独立，宏指令归约保持 | 使用已有成员元数据 |
| atomic / locked / fence | 数据、权限和串行边同时保持 | 未认证时继续专用路径 |
| PTE / translation / ASID | PTE 数据可引用父源，walker/TLB 边独立 | 物理地址、页几何与时钟有效；本轮不修 DTLB |
| MMIO / noncacheable | 保留专用服务，不普通同线合并 | 不因统一池消失 translation 或访问成员 |
| ifetch / 软件预取 | 完成、early-ack 与数据边分开 | 后续单独验收 |
| 硬件预取 / LLC transient / remote supply | 共同下层源与接收侧 lease 分开 | 后续迁移现有拥有者，避免双表 |
| eviction / writeback / 晚 fill | 目标代次、脏写回、已发资源完整 | S3 前不改变 Tag 发布时间 |
| 跨核 / task / warmup / EOF | 物理供给与请求统计身份分开 | 跨窗口引用和统计边界已验证 |

本稿保留 S3 的 `probe/complete_fill` 扩展槽；先把准备服务、接管和释放闭合，再改 victim/LRU/dirty/Tag 发布。统一记录本身不等于完整缓存因果性修复。

## 12. 有界存储和成本预算

使用定长服务池、有限探测行索引、现有有界请求槽及 touched-slot journal。热字段只保留常用身份、状态、数据/权限边、引用和 lease；诊断和少见路径放入有界冷槽。

固定上限至少包含：

```text
K_service, K_request_ref, K_release_ref, K_publish_ref
B_line_candidates, B_parent_depth, B_leases
B_targets, B_gate_refs, B_journal, B_component
```

容量依据控制器几何、保留窗口和 pin 人口推导，不能只照搬 MSHR 数量，也不能随 ROI 长度增长。

\[
M\leq K_s S_s+K_r S_r+K_e S_e+B_j S_j.
\]

普通完成 hit 不分配服务；活跃 cookie 命中直接读 ID，候选查一次本域行索引。资源/发布索引引用记录，按有限堆或现有稀疏日历处理，宿主单事件至多 \(O(\log K_e)\)；不扫描所有 pending prepare，不逐目标周期更新。

索引 stale 节点用槽代次和版本验证，并在固定阈值内压缩。池满属于宿主表示预算，不能当硬件 MSHR stall；提交前可回退则回退，不能安全回退则显式终止该实验，不删请求。

结构统一可能减少表查询与复制；新约束和校验也可能增加成本。目前没有修订版吞吐测量，不给出收益百分比。

### 12.1 性能门禁前移

[宿主成本分析](fastsim-parent-service-host-cost-20261110.md)核对了旧 S0/S1 争用样本和当前记录布局：它们不能给出 v2 开销百分比。当前池仍按候选复制容器，固定容量也不能保证累计宿主成本低。

R0 就建立普通完成 hit 快速路径与操作人口计数；R1 在完成边拆分时同步验证 touched-slot journal 和索引增量，扩展 R2/R3 前避免把重复整池复制留作默认实现。限制 fallback 的总覆盖，避免大量组件先走新路径再走 legacy。R4 用于集成与吞吐验收，性能要求不延后到 R4。

## 13. 实施顺序

保留原 S0/S1/S2/S3 概念，但把私有侧前置工作拆为以下可独立验收的修订：

| 顺序 | 改动 | 验收目标 |
|---|---|---|
| R0：服务事实与身份 | 捕获 pre-state、真实服务动作和时间来源；请求引用与 ID 打通，建立 hit 快速路径及操作计数 | prepare ON，功能、服务选择、响应、资源时间中性 |
| R1：独立完成边 | 拆出 data、permission、controller 与 request callback；迁移现有两层 lease，同步验证 journal 与索引增量 | 读响应不继承无关 callback；迁移本身中性，事务回滚与复制成本可核查 |
| R2：prepare 服务与接管 | I/S 分类、transient 引用、credit 与 TBE/fill 独立、store 接管 | 提示确认不提前释放 fill；同源不重复收费 |
| R3：统一私有准入与等待 | 相关 load 和 store 使用同一服务边，替换对应 legacy 计算来源 | 单一约束来源、FIFO/容量不重复、一次反馈资格通过 |
| R4：性能集成与验收 | 删除剩余旧行级时间事实，集成有限索引/journal 和快速路径，核查总体事件与回退成本 | 同输入精度与宿主吞吐分别验收 |
| 后续原 S2/S3 | 进一步合并下层服务、迁移 LLC/预取、受限延迟 Tag | 逐路径独立验收，默认关闭 |

R2 包含真实机制改动，不能把它并入 R0 宣称中性。R3 不是在 legacy prepare 时间外再加一个 solver，而是把已迁移的约束入口改为共同服务引用；尚未迁移的路径保持明确范围。

迁移按路径切换唯一拥有者：未迁移路径的事实仍由旧入口拥有，统一接口只引用；切换后旧入口读取统一记录，不再保留可独立改写的权限/回填时间。中性比对可以在有界审计构建保存旧值，正式路径不得长期双写两套权威时间表。

落点：

- `include/fastsim/parent_service.hpp`：当前隔离池扩展身份、完成边、引用与回收条件。
- `src/simulator.cpp`：`MemoryReplay` / `replay_memory_event` 输出事实，`resolve_parent_service` 消费事实，feedback 与事务统一提交。
- `include/fastsim/legacy_prepare_timing.hpp`：迁移 pending 项为请求/服务引用，逐步撤除对应权限时间事实。
- `include/fastsim/spr_store_window.hpp` 及相关 store 接口：保留 FIFO 和请求容量，输出 blocker 的唯一资源身份。
- config / stats / 现有定向测试：声明能力版本与范围；默认值不推广未验收功能。

继续在独立源码和 build 目录实施，记录 baseline 身份，遵守 `fastsim-spr` / Codebase 工作流。

## 14. 验证：机制闭合先于误差结论

### 14.1 必须能区别错误实现的定向测试

| 输入关系 | 必须断言 |
|---|---|
| I+prepare，数据前 load | 不提前响应；需要重派发的本地边只执行一次 |
| S+prepare，同线 load | 只受正确控制器门约束；无新 data fill |
| E/M+prepare 或 transient+prepare | 无重复下层事务；提示 credit 正确完成 |
| store 在回填前接管 | 同 ID/同 source/同 lease，credit 可先释放，写入不早于必要数据和权限 |
| store 在 prepare 完成后到达 | 不重复支付已完成 GETX/UPGRADE；原 FIFO/写端口仍在 |
| 提示 credit 满或提示被丢弃 | mandatory store 未被删去，必要时发真实服务 |
| 已驻留读、无 Busy，写 callback 未完成 | 不产生无依据的写 callback 等待 |
| fill 与接管/准入同周期 | release 和状态次序符合声明，无双占用或双释放 |
| 同线 eviction/失效后晚 fill | 旧代次不可恢复新目标，新请求不误附着 |
| warmup / chunk / clock epoch 切换 | 引用和资格有效，时间不因映射切换倒置 |
| slot 重用、事务拒绝、预算耗尽 | 无幽灵发布/释放、错误父引用或丢失请求 |

### 14.2 三种守恒分别检查

1. **功能人口与结果**：宏指令、UOP、内存成员、fragment 和输入范围一致；功能结果只在现有工件提供证据时认证。
2. **固定功能排序的 lookup 守恒**：R0/R1 中性迁移及纯响应约束，在相同功能顺序下逐项核对 L1/L2 outcome、dirty/eviction；不只检查汇总。
3. **真实事务和资源守恒**：新服务、附着、接管、提示丢弃分别计数；每个分配有唯一 owner 和释放，升级不冒充 fill。

端到端时序反馈可能改变跨核排序，进而改变功能 cache outcome。此时须追踪最早分歧及其合法调度来源；不能直接要求所有 S1–S3 计数永远等于 baseline，也不能用“只差几个”跳过审查。

上一轮 35 次回放的人口门通过，但 L1/L2 汇总门未闭合。现有 S1 不视为完成验收；修订前后保留这个问题，按上述分层检查解释，不将其静默豁免。

### 14.3 CPI 与交互

所有主对照保留 prepare ON。运行 R0/R1 中性、R2/R3 机制增量及最终集成对照，覆盖 Marian、FLAC、Graph500、XZ、Zstd 和 held-out 案例。

记录逐核 active cycles、共同终点、数据/控制器门、eligible→send、写入/callback、FIFO/port/capacity blocker ID、source 资格及 fallback 覆盖。

单条请求的局部门增量可按确定顺序计算：

\[
\Delta_j=\max(g_0,\ldots,g_j)-\max(g_0,\ldots,g_{j-1}).
\]

这些数只在该局部门上互斥。更一般的分支边图需要按同一节点的前驱或 critical predecessor 记录；累计 follower wait、credit lifetime、store blocker 和 shared queue 时间不能直接相加成为 ROI active-cycle 增量。

使用已有 native CPI 参考分析时遵循 scope 和人口合同，逐案同时列：

\[
AE_i=|\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}|
\quad[\mathrm{cycles/macro}],
\qquad
RE_i=\frac{\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}}
           {\mathrm{CPI}_{G,i}}.
\]

汇总必须列 CPI MAE，MAPE 与 WAPE 分开。新增等待正确也可能使 CPI 误差变大；没有来源和排他机制证据时不能把残差全部归给 prepare。沿用[accuracy reporting contract](docs/accuracy-reporting-contract.md)；本次按用户要求未恢复 native 来源审计。

### 14.4 宿主吞吐与推广

隔离构建后直接运行 `fastsim_tests`，再做必要定向与同输入回放。吞吐测量使用固定机器放置、相同输入/范围/线程与交错重复 A/B，分列服务分类、查询、feedback、journal 和总体 user-UOP/s；测试时避免并行采集或大构建争用。

精度方向、资源守恒和宿主吞吐分别判断。使用已有吞吐预算；没有预算时先形成可复核实测门限，不凭空承诺 1% 或 5%。满足机制、覆盖和性能门禁后才讨论默认推广。

## 15. 当前可作出的结论

修订已经把 prepare-owned fill、权限升级、控制器阻塞、store 接管和独立资源释放放入同一所有权设计。它针对的是已确认的机制覆盖与表达缺口。

是否改善 Marian / FLAC、能覆盖多少同窗口关系、能否在现有事务接口中安全局部回退、以及新增宿主成本，仍需要隔离实现和验证。本设计不以关闭 prepare 或减少输入人口获取误差改善。
