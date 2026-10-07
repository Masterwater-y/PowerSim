# LBM buffer-114 大误差：权限准备门禁诊断与修复边界

状态：**诊断完成；LRU 驻留证书与 prepare/publish 生命周期均以默认关闭配置开关
落入生产，drain 常数被否**。
维护模型的默认行为、默认配置与原始结果未改变（两个新开关默认关闭即位级回退）。
新增 20 次完整 warmup/common-end replay：LBM 四核数各 baseline/no-set/LRU，
三个非 LBM C4 控制各 baseline/LRU，Graph500 C4 再做 warmup-only/measurement-only；
2026-09-22 追加 7 案例的 publish/drain 边扫描、逐 store 绑定审计，
以及 lifecycle 单独消融 + dual 双重验证。
模型变体先在隔离副本验证；无新 gem5 采集、无标量校准、无吞吐认证。

**结论先行：** buffer-114 的大误差主要不是 store buffer 数量没生效，而是
权限准备的同组冲突预筛选过度保守，让约 69%–73% 的 eligible store 重新走
串行服务。保留 LRU 驻留证明的候选将四项 LBM CPI MAE 从 **1.129515 降至
0.113737**，MAPE 从 **117.84% 降至 9.97%**，但 Graph500 C4 明显回退，
因此以**默认关闭**的配置开关 `core.buffer_tso_residence_certificate` 落入
生产（默认行为不变，回退位级一致），尚不能作为默认修复推广。C32 仍有
**+16.91%、0.254202 cycles/macro instruction** 残差，尚不能精确归因到
某个剩余组件。

## 已证实的第一层原因

buffer-114 的真实 O3 参考没有失效。本轮重新执行目标身份检查，LBM
C4/C8/C16/C32 全部通过，产物：
`tmp/lbm-buffer-tso-audit-20260921/identity.json`。
原 `collection-audit.json` 保留共同终点、native 守恒及 zero-unknown 证据。
四个 LBM 的总量和每核宏指令分母均与参考相等，参与核共 60 个；39 项报告中另有 5 项人口不完全相等，
不能把整个矩阵标成严格人口对齐。
三个非 LBM 控制也重新通过独立目标身份检查，见 `identity-controls.json`。
本次是同一 buffer-114 源上的配对诊断，非 workload-held-out 或 SPR 硬件认证。

活跃路径不是简单的“buffer 深度不足”，而是：

1. `prepare_buffer_tso_epoch()` 从 epoch 尾部反向扫描缓存组；
2. 一个普通 store 后面只要还有不同 line 映射同一 L1D 或 L2 set，
   就把该 line 放入 `buffer_tso_conflict_lines_`；
3. `buffered_tso_permission_prepare()` 拒绝这些 line 的权限准备；
4. `regular_store_response_cycle()` 对被拒绝的 store 使用
   `store_send + full_service_latency`，而不是
   `max(permission_ready, store_send + publish_latency)`；
5. `store_drain_ready` 成为下一 store 的 FIFO send 下界，SQ 满后再反压
   dispatch/retire。因此被拒绝的长服务重新进入串行临界链。

当前 L1D 是 8 路 LRU，但第 2 步是“同组存在另一 line 即拒绝”的充分安全
条件，远比真实 eviction 条件严格；它并不是把整个 cache 实现成 direct-mapped。
`buffer_tso_conflict_lines_` 又按 line 而非单个请求存储，一个出现位置失败，
同 epoch 中该 line 的其他 store 也失去准备资格。

## 完整干预证据

在 `tmp/lbm-buffer-tso-audit-20260921/source/` 隔离副本增加按
warmup/measurement 分开的观测计数，并用同一二进制比较原逻辑与 `no-set` 消融。
消融只跳过同组预筛选，保留跨核冲突、page-fill、权限末端检查和整批回退。
使用完整原始 warmup + common-end measurement，不截短、不输入 gem5 时序。

四个核数全部完成，单位为 cycles/macro instruction：

| 案例 | gem5 CPI | 原 FastSim CPI | 原相对误差 | 原 CPI 绝对误差 | no-set CPI | no-set 相对误差 | no-set CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| LBM C4 | 0.817326 | 2.068267 | +153.05% | 1.250941 | 0.771902 | −5.56% | 0.045424 |
| LBM C8 | 0.822077 | 1.880021 | +128.69% | 1.057944 | 0.785599 | −4.44% | 0.036478 |
| LBM C16 | 0.951678 | 2.057559 | +116.20% | 1.105881 | 1.033826 | +8.63% | 0.082148 |
| LBM C32 | 1.502989 | 2.606284 | +73.41% | 1.103295 | 1.804488 | +20.06% | 0.301498 |

| 四项等权聚合 | CPI MAE | MAPE |
|---|---:|---:|
| baseline | 1.129515 | 117.8386% |
| no-set 消融 | 0.116387 | 9.6717% |

C4 measurement 的 3,956,767 个 eligible store 中，原逻辑只准备
1,065,045 个，2,891,722 个重新串行化；C8 对应 7,962,571、
2,328,656、5,633,915。原逻辑这两项的 cross-core/page-fill/rollback
均为零，因而不是原生多核冲突或回退导致这个覆盖损失。
no-set 分别准备 3,939,350 和 7,880,192 个。
四项原逻辑 CPI 均精确复现已归档报告。

| LBM 核数 | measurement eligible store | 原准备覆盖率 | 原重新串行比例 | 原 L1 同组位置冲突率 | 原 L2 同组位置冲突率 | LRU 准备覆盖率 |
|---|---:|---:|---:|---:|---:|---:|
| C4 | 3,956,767 | 26.92% | 73.08% | 53.84% | 2.26% | 96.29% |
| C8 | 7,962,571 | 29.25% | 70.75% | 50.31% | 1.89% | 96.77% |
| C16 | 15,564,876 | 30.14% | 69.86% | 48.89% | 2.52% | 96.25% |
| C32 | 30,986,112 | 30.67% | 69.33% | 48.20% | 2.31% | 96.50% |

L1/L2 冲突率是逐位置、可重叠的统计，不能相加；重新串行比例还受到 line 级
标记扩散影响。原 baseline 四项的整批 rollback 均为零，page-fill 均为零，
跨核冲突只在 C32 出现 10 个 eligible 位置；不是数百万真实跨核竞争导致该损失。
no-set 四项 measurement 的 rollback epoch 数为 23/45/96/181；LRU 四项均为零。
CSV 中 `epochs`/`rollback` 按核重复记录，全局 epoch 数须除以核数，
`report.json` 已输出去重后的 `epoch_count`/`rollback_epoch_count`。

原子、split、barrier 等不满足 `tso_buffer_eligible` 的事件不在这个分母内。
测量计数与 warmup 分开保存，没有用预热事件夸大覆盖率。

这是门禁在活跃路径上的因果干预，不是把等待计数相加解释 CPI。
它足以否定“只能归为旧两阶段精度天花板、没有局部机制可修”的推断。
旧 serial 数据集上的天花板/负实验不直接约束新的 buffer-114 目标。

### 为什么 LBM 特别敏感

LBM 的这个输入包含大量普通 store，访问流反复落入同一 L1D set，逐位置
冲突约占一半，line 级禁用扩大至约七成。失去准备的 store 不只是自身延迟变长：
它的完整服务响应会成为后续 store 的 FIFO 下界，队列深度即使实际是 114，
也只是更久才满，不能恢复丢掉的权限获取并行性。

在本次隔离基线中 SQ 峰值确实为 114；候选仍为 114。因而“把 SQ 再调大”
不是已证实根因的修复。此前 serial→buffer 数字只能说明目标行为变化，
不能代替上述门禁路径证据，也不能把不同采集中的 CPI 差直接当作同一请求流
可加减的 latency 分解。

gem5 对应实现位于采集命令实际使用的
`tmp/se-fs-paired-c4-20260820/gem5-se-isolated/`，不是另一个未打补丁的 gem5 树：

- `src/cpu/o3/lsq_unit.cc::issueStorePrepareRequests()` 对 `canWB()` 的普通
  store 发送 `SoftPFExReq/STORE_PREPARE`，仍受缓存端口和特殊操作规则约束；
- 真正 WriteReq 仍受 `storeInFlight`/响应串行规则约束，准备不是乱序发布数据；
- C4 的 `stats.txt` 四核 `storePrepareIssued` 合计 **3,925,364**，
  `storePrepareBlocked` 均为零。这是 ROI 内发出计数，不与 FastSim 的
  committed-event eligible 数量逐个等同；
- gem5 的真实组相联 cache/协议处理 prepare，不存在 FastSim 这一条
  “后缀同组还有别的 line 就一律禁用准备”的 epoch 级门禁。

## 不应直接删除门禁

no-set 仅是定位工具，不能由 CPI 改善就推广：

- 末端 `PrivateHierarchy::contains()` 接受 L1 **或** L2 命中；
- `PrivateHierarchy::mark_dirty()` 只更新 L1；
- prepared line 中途从 L1 淘汰、仅留 L2 时，末端权限检查仍可能通过，
  却不能保证 FIFO publish 把脏数据正确落到驻留层；
- 中途 eviction 后同 line 再填入属于不同驻留世代，epoch 末尾“还在”
  不能证明原准备权限始终有效。

优先修复应把“同组即冲突”换成**有证明的驻留保持证书**，而不是直接把
`contains` 当作完整证书。第一片可以利用现有 L1 LRU：每个准备位置之后，
同组不同于本 line 的 distinct line 少于 associativity，才保证不被 L1 淘汰；
L2 的 TreePLRU 不能照搬 LRU 的 distinct-count 定理，先保留原保守条件。
跨核、page-fill、atomic/barrier、split store、FIFO publish 和实际权限回退继续保留。
更宽的覆盖需要显式处理 prepare/publish 的驻留世代与 eviction/dirty 所有权。

本轮已运行 `certificate_probe.cpp`：10,000 个随机 LRU 驻留检查通过；
另外构造 8 路 L1 淘汰到 L2 的反例，实际复现了 `contains=true` 但
`mark_dirty()` 后 `dirty=false`。这证明“只删门禁、靠末端 contains 保证安全”
存在具体缺口，并不证明该反例在 LBM 每个消融窗口都实际发生。

## 有证明的局部修复试验

隔离候选 `FASTSIM_BUFFER_LRU_CERTIFICATE=1`：

- 仅 L1 replacement 为 LRU 时放宽；
- 反向扫描每个 set，保存后缀 distinct line 的有界集合；集合最大
  associativity+1，超过此数必拒绝；
- 对 store 的 line，后缀其他 distinct line 数小于 associativity 才允许；
- 保留 L2 原预筛选，保留跨核/page-fill/特殊操作规则和真实权限检查；
- 保留 FIFO drain/SQ 114、两阶段结构、原始服务时延与缓存参数。

该证书比最精细的 LRU stack-distance 判断仍保守：统计整个后缀，而不是
只统计两次本 line 引用之间的最大间隔。它不是 workload/PC 特判。

| LBM 案例 | gem5 CPI | LRU CPI | signed 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|---:|
| C4 | 0.817326 | 0.772321 | −5.5063% | 0.045005 |
| C8 | 0.822077 | 0.757006 | −7.9155% | 0.065071 |
| C16 | 0.951678 | 1.042345 | +9.5271% | 0.090668 |
| C32 | 1.502989 | 1.757192 | +16.9131% | 0.254202 |
| 四项聚合 | — | — | MAPE **9.9655%** | CPI MAE **0.113737** |

相比 baseline，CPI MAE 降低约 89.93%，但这不是可推广的整体精度收益。
no-set 与 LRU 的结果不必单调：prepare 改变核心反馈、后续 epoch 划分和共享
到达顺序，C8/C32 在更保守候选中反而更快，不能用准备比例线性外推 CPI。

### 非 LBM 控制与停止条件

| C4 控制 | gem5 CPI | 原 CPI | 原相对误差 | 原 CPI 绝对误差 | LRU CPI | LRU 相对误差 | LRU CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Stockfish | 0.668626 | 0.664102 | −0.6767% | 0.004525 | 0.660401 | −1.2301% | 0.008225 |
| TeaLeaf | 1.004183 | 0.859986 | −14.3596% | 0.144197 | 0.860932 | −14.2654% | 0.143251 |
| Graph500 | 1.305529 | 1.252444 | −4.0662% | 0.053085 | 1.161412 | −11.0390% | 0.144117 |

| 三项等权控制聚合 | CPI MAE | MAPE |
|---|---:|---:|
| baseline | 0.067269 | 6.3675% |
| LRU | 0.098531 | 8.8448% |

Graph500 回退不能沿用旧 serial v2 的 warmup-only 结论。新做的相位消融：

| Graph500 C4 相位 | CPI | signed 相对误差 | CPI 绝对误差 |
|---|---:|---:|---:|
| 原逻辑 | 1.252444 | −4.0662% | 0.053085 |
| 只在 warmup 用 LRU | 1.248397 | −4.3762% | 0.057132 |
| 只在 measurement 用 LRU | 1.161316 | −11.0464% | 0.144213 |
| 全程 LRU | 1.161412 | −11.0390% | 0.144117 |

measurement-only 基本复现全程回退，下一项应查测量阶段的 prepare admission /
服务并行性，而不是全局关闭 warmup 修复。**停止推广，不扩成完整 39/40 项候选
矩阵，不把 LRU 设为默认，不按负载挑选开启。** 组件上更合理的证书与最终 CPI
回退可以并存，不能因此反向宣称旧的过保守门禁就是正确微架构。

## 剩余误差与 PMU 的证据边界

### C32 不是只剩一个异常 core

LRU 后 C32 总正残差为 57,595,318 core-cycles；core0 贡献 2,004,521，
只占 3.48%，32 个核心全都高估。以下是整核 CPI，不是历史退休跨度窗口：

| C32 core | gem5 CPI | 原 CPI | 原相对误差 | 原 CPI 绝对误差 | LRU CPI | LRU 相对误差 | LRU CPI 绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 3.897037 | 8.598896 | +120.65% | 4.701860 | 4.631074 | +18.84% | 0.734037 |
| 1 | 1.470160 | 2.485774 | +69.08% | 1.015614 | 1.658625 | +12.82% | 0.188465 |
| 13 | 1.416376 | 2.530693 | +78.67% | 1.114317 | 1.638373 | +15.67% | 0.221996 |

这些数字不替代旧 serial C1/C13 冻结退休跨度控制；后者须用原 trace 单独重跑，
本轮未声称通过。buffer 数据应另建同事件锚点的窗口控制。

### 已发现的建模差异，尚未量化剩余 CPI 贡献

1. gem5 prepare 在 `canWB()` 后准入，使用 optional/prefetch 路径。
   FastSim 在 canonical memory replay 上做权限准备，反馈中普通 store 的
   `deferred_store` 只由 pending-fill/shared-service 模式控制，
   buffer 资格本身并没有建立独立的 **post-retire prepare admission** 状态机。
   因此可能把准备时间放到实际可准备时点之前，尤其会影响原本低估的控制负载。
2. `Sequencer::makeRequest()` 对 `isStorePrepare()` 先入 optional queue，
   返回点在 `m_max_outstanding_requests` 的 demand 检查之前；FastSim 的该
   路径仍使用通用 memory feedback/capacity bookkeeping。两类资源寿命
   不应靠一个全局增减 `ruby_sequencer_max_outstanding` 消除差异。
3. 一次 FastSim replay 同时承担 permission 获取、路径分类及后续 publish
   latency 反馈；native 是 prepare 和真正 demand WriteReq 两类事件，
   可能有 prepare 完成、丢弃、失效、重复或 publish 时重新获取权限。
   目前没有按这些身份配对的原生时序 ledger。

因此 C32 剩余 0.254202 CPI 的所有贡献、Graph500 首个错误准入事件、以及
网络/DRAM 到达反馈的占比仍待验证，不能全部贴成 DRAM 问题或两阶段不可修下界。
旧 `exposed_memory_penalty_cycles` 在活跃 fast-kernel 中仍是闭合残差桶，
不是可直接相加归因的 DRAM 等待；许多 attribution 计数在该路径未启用，
不能用零值排除组件。

### 绑定项定位：FIFO publish 边而非权限锚点（2026-09-22 新增）

进一步隔离实验把 `sq_release` 的真实绑定项定位到 **FIFO publish 链**，
而不是权限准备锚点。两项证据：

1. **权限退休下界是 CPI 中性的。** 在隔离候选上把
   `buffered_permission_ready` 从 pre-retire 功能包络改为
   `max(retire, full_service)`（`FASTSIM_BUFFER_PREPARE_LIFECYCLE`），
   flag OFF 位级一致。LBM C4 −5.51%→−5.36%、Graph500 C4 −11.04%→−10.85%，
   基本不动。此前反例证明的“提前准备”缺陷真实存在，但**不是** CPI 主项。
2. **逐 store 绑定审计（measurement 相位，`FASTSIM_BUFFER_BIND_AUDIT`）：**
   LBM C4 的 3,811,811 个准备 store 中 92.8% 由 publish 项
   （`store_send + publish`）决定 `sq_release`，仅 7.2% 由权限项决定，
   平均 publish=1.00 周期、平均 `store_send−retire`=98.89 周期；
   Graph500 C4 的 1,859,003 个准备 store 中 67.5% publish 绑定、
   平均 `store_send−retire`=67.95 周期。即在 `commit_to_admission=0`、
   `response_to_next=0` 的 buffer 模式下，稳态排空速率恰为
   **1 store/周期**（`sq_release_i = store_send_i + publish=1`，
   `store_send_{i+1} = max(retire, sq_release_i)`），与真实 gem5 由
   `storeInFlight` 串行化、demand WriteReq 暴露真实服务时延的行为不同。

### 全局 publish/drain 常数是过拟合，不是生产修复（2026-09-22 新增）

沿 publish 边做加性扫描（`FASTSIM_BUFFER_PUBLISH_ADD`），7 个可用案例
（LBM C4/C8/C16/C32 + Stockfish/TeaLeaf/Graph500 C4）signed 相对误差：

| 案例 | publish=1（当前） | publish=4 | publish=5 | 方向 |
|---|---:|---:|---:|---|
| LBM C4 | −5.51% | −4.79% | +0.32% | 低估，加时延变好 |
| LBM C8 | −7.92% | −7.98% | −4.35% | 低估，加时延变好 |
| LBM C16 | +9.53% | +13.03% | +15.10% | **高估，加时延变差** |
| LBM C32 | +16.91% | +17.03% | +17.79% | **高估，加时延变差** |
| Stockfish | −1.23% | +0.30% | +1.84% | 低估，加时延变好 |
| TeaLeaf | −14.27% | −14.08% | −14.10% | 与 store drain 正交，几乎不动 |
| Graph500 | −11.04% | −3.59% | +2.04% | 低估，加时延变好 |

`publish=5`（`store_lower_bound=1` 再 +4）让 LBM C4、Stockfish、Graph500
同时逼近参考，配置里 `l1d.hit_latency=2`、`minimum_load_latency=4`，看似
可从 store 协议边导出。但它 **同时把已经高估的 LBM C16/C32 推得更差**。
signed 方向明确分裂：C4 规模低估（加排空时延有益），LBM C16/C32 高估
（加排空时延有害）。七项等权 MAPE 从 9.49% 降到 7.93% 只是牺牲高核数
守门项换取 C4 收益的假象。结论：

- **任何统一的 publish/drain 常数都不能作为生产修复**，因为它对所有案例
  同向移动，而 C16/C32 需要更少而非更多的时延；这也正是被禁止的
  “借延迟凑 CPI”手法。
- 同理，**有界并发 prepare/publish 带宽模型也被否定**：带宽上限等价于加一个
  统一的排空时延，无法让 C16/C32 与 C4 同时收敛。
- **LBM C16/C32 的高估是与 store drain 边正交的跨核机制**（残差 0.254202
  CPI 落在这里），必须单独归因，不能用 store 排空项掩盖。
- TeaLeaf 的 −14% 残差同样与 store drain 无关，需另行诊断。

这轮扫描是隔离诊断二进制上的机制定位实验，不改生产模型、不设默认，也不把
任一 `publish` 常数写入配置。

### 根因定位：每条 store 缺一次“FIFO 头部 demand”（2026-09-22 新增）

> 范围修正：首次实现同时改动资源调度并新增整 epoch 回退，未能单独验证缺失 demand
> 的 CPI 贡献。现已按用户要求撤掉这些附带改动，见
> [仅补队头 demand](buffer-tso-head-demand-only-20260923.md)。源码确认存在协议请求缺项，
> 但本文此前把 NAMD/Graph500 误差归因于该缺项的表述过强，仍须受控对照验证。

> 2026-09-23 实施更新：队头 demand 候选和小集验证已落地，见
> [实现、回退保护与实验报告](buffer-tso-head-demand-20260923.md)。源码复查将
> 下文“两类请求”细化为：immediate head 只发 demand；follower 的在途 prepare
> 可被 demand 接管；只有准备已完成的 follower 才需要额外本地请求。不能给所有
> store 都串接一次 L1 服务。候选保留默认关闭，当前小集已出现精度回退，不推广。

追 gem5 源码（`tmp/se-fs-paired-c4-20260820/gem5-se-isolated/src/cpu/o3/lsq_unit.cc`）
后，确认真实硬件对每条 buffer store 是**两类独立请求**，而 FastSim 只发了
一类：

| | prepare（取权限） | head demand WriteReq（真正发布） |
|---|---|---|
| 触发 | 还在 storeQueue FIFO 中段、`canWB()` 后 | **成为 FIFO 头部**时 |
| 队列 | Ruby **optional**（`SoftPFExReq`，`PF_EXCLUSIVE|STORE_PREPARE`） | Ruby **mandatory**（`sendPacketToCache`） |
| 串行性 | 否，与头部 demand **重叠** | 是，`storeInFlight` 一次一条，下一条等回调 `completeStore` |
| 作用 | 提前把行取成本核 exclusive（clean，不 dirty） | 真正写数据、置 modified+dirty；**SQ 在该回调才释放** |

源码依据：`writebackStores()`（lsq_unit.cc:826-971）只在头部且 `!storeInFlight`
发 demand，随后 `issueStorePrepareRequests()`（:974-1037）对 `canWB()` FIFO
前缀的其余 store 发 prepare；`completeStore()`（:1258-1315，调用点 :184-187）
在 demand 回调里清 `storeInFlight`。

**关键**：follower 靠 prepare 提前取好权限；它后来成为头部时**仍要再发一次
demand**。这次因权限已在本地（M）是一次快速 L1 命中，但仍走 mandatory 队列、
经 L0 控制器请求/响应边，既不是 0 也不是写死的大数。

FastSim 的错误根因：`buffered_tso_permission_prepare()`（simulator.cpp:5018）
让每条 eligible store 在**唯一一次** functional replay 里就以 permission-only
取权限（`replay_memory_event`:21448、`:21471-21487`），随后
`regular_store_response_cycle()`（:5025-5037）用这唯一一次 prepare 的响应直接
算 `sq_release`。即**把 follower 阶段 prepare 的完成，当成头部 demand 的完成**；
`SharedSystem`（:1521 起）也不区分 mandatory/optional 队列。

这同时解释两个方向：

- **NAMD / Graph500 低估**：prepare 在 optional 路径上被重叠、几乎免费，FastSim
  却拿它当 SQ 释放点 → SQ 占用算短 → CPI 偏低。NAMD c04 在 plain buffer 即已
  −13.5%，主缺口早于证书，正是此漏发所致。
- **统一加常数为何被否决**：之前 `FASTSIM_TSO_DEMAND_GAP`/publish 加性扫描给
  所有 store（含本应重叠的 follower prepare）都补一条 demand 边，于是 LBM
  C16/C32 把 follower 也按完整 demand 多收费而高估。这是“借延迟凑 CPI”，
  非机制修复。

**正确修法（零可调常数）**：在已默认关闭的 `core.buffer_tso_prepare_lifecycle`
开关内补完，不新增旋钮——

1. store 在 FIFO 中段发 prepare（permission-only），走 optional 资源占用、与
   头部 demand 重叠，**不释放 SQ**；
2. store 到达 FIFO 头部 edge（`store_send` 已被 `store_drain_ready` 钳到上一条
   demand 完成）时，以 `write=true / permission_only=false` **再走一次现成的
   真实访存 replay**；其耗时由实际命中 level 决定（prepare 已取权限则命中本地
   L1，自然只多 L1 的边），数字来自模型与该案例 `config.ini`，不手填；
3. `sq_release` 改由这次头部 demand 的 completion 决定，`store_drain_ready`、
   FIFO 发布、丢权限重取沿用现有链；
4. 任何开关 OFF 与当前生产逐值一致；保留跨核/page-fill/atomic/barrier/split 的
   fail-closed 回退。

本次范围：机制实现 + 小集验证（NAMD c04 应收敛向 0、LBM c16/c32 不恶化，叠加
既有控制），全 39 例与吞吐门属后续推广决策。

### cache PMU 差异不能解释成四倍真实流量

维护 native validator 对 LBM baseline 给出的四项聚合：

| counter | 映射 | 有限样本 | MAPE | WAPE | signed bias |
|---|---|---:|---:|---:|---:|
| L1D tag miss | proxy | 4/4 | 199.9046% | 212.9960% | +212.9960% |
| private-L2 tag miss | proxy | 4/4 | 296.6843% | 296.6229% | +296.6229% |
| LLC tag miss | diagnostic | 4/4 | 298.7672% | 298.8298% | +298.8298% |

完整百分位、来源和 case 表保存在 `rescore-baseline/summary.md`。
例如 C4 private-L2 为 FastSim 566,498 / native 142,961，
LLC 为 565,195 / 141,939。no-set CPI 大降，但这些 miss 计数几乎不变：
C4 private-L2 566,501，LLC 565,195。这进一步支持**本轮大幅 CPI 变化来自
store 时序关系，而不是 cache miss 数骤减**。

但不能延续“所有大 gap 已证明由 inclusion/coherence 导致”的说法。源码有
更直接的域差异：gem5 `Store_Prepare` 使用 `pb_issuePfGETX`，该 optional
路径没有 demand `uu_profileDataMiss`，且没有普通 GETX 的
`TraceValid/TraceContextId/TraceInstSeqNum` 传播；native summary 明确按
`sequencer-mandatory-queue-enqueue` 归属。FastSim 当前准备仍经
`PrivateHierarchy::access()` 和普通 `shared_->access()` 计数。
有 source/conservation 认证不等于 prepare 与 demand 人口已经语义等价，
更不能把 demand miss 差直接当作 DRAM transaction 差并减去时延。

下一步应独立记录 prepare、demand、合并、失效及实际 memory-port RD/WB，
同时守恒各人口。不要把 prepare 直接并入 demand 真值，也不要用固定比例折算
旧 proxy。此项对性能之外的功耗/总能耗模型尤其重要：权限流量仍耗能，
即使对应 demand 最后表现为命中。

## 建议的修复顺序

1. **先交付覆盖观测与驻留证书，不直接放宽全部 guard。**
   将 epoch/逐核/相位的 eligible、prepared、具体拒绝原因、权限失效及
   rollback 统计做成可维护的默认无开销诊断。L1 LRU 有界证书是已实测可行的
   第一片；TreePLRU 用真实 replacement 状态或独立保守证明，不能照搬 LRU。
2. **补独立 prepare/publish 生命周期，再处理控制负载回退。**
   为普通 store 保存有界身份与 AGU-ready、retired/canWB、prepare admission/
   response、permission generation、FIFO publish、SQ release 等边；
   prepare 准入下界不能早于退休可写，publish 必须保留前序 store 与屏障顺序。
   分离 optional 准备资源和 demand 发布资源；失效时重新获取权限，而不是
   无条件使用旧 ready。对越过当前 Q 的未完成请求保留有界状态或明确回退，
   不预提交不可认证的未来 cache 状态，不复活全核心 `causal_read`。
3. **对 Graph500 measurement 首分叉和 LBM C32 剩余段做原生事件配对。**
   现有 summary 的 `full_jsonl_enabled=false`，不足以给出每个 prepare 的
   admission/response 配对。需要带 prepare 身份的有界审计采集；先固定源、
   事件字典和语义，再考虑新采集成本。本轮未启动重采，也未向 FastSim 输入
   gem5 时间。优先区分提前准备、端口/队列约束、失效重取和真实内存并行性。
4. **最后推广。**
   保留 serial-off 位级回退与原 C1/C13 控制；buffer 增加同组 1..ways/
   ways+1、重复 line、L1→L2、evict/refill、跨核、split、fence/atomic、
   SQ 2/114、warmup/reset、跨 Q 生命周期测试。完整 39 个可用案例全部重新
   评估；缺失 Zstd C32 不得伪称 baseline-40 全通过。CPI MAPE/MAE、
   逐核/尾部误差、各 native PMU 域和独立 quiet-host 吞吐门禁分别验收。

不得用 `needsTSO=false`、DRAM 系数、CPI 比例补偿、负载特判或仅看 LBM 平均数
代替修复。步骤 1 的 L1 LRU 有界证书已作为默认关闭开关落入生产（见下节），
步骤 2–4 仍未完成，故默认行为不变，也不得据此宣称全矩阵推广通过。

## 生产落地：`core.buffer_tso_residence_certificate`（默认关闭，2026-09-22）

修复顺序第 1 步中已实测可行的那一片——L1 LRU 有界驻留证书——以**默认关闭**
的配置开关落入生产维护代码，保留位级回退路径。开关关闭时行为与改动前的
buffer-TSO 完全一致；开关打开时用精确的 distinct-count 非驱逐判据替换 L1
同组预筛选的那一半，L2 半仍保守（TreePLRU 无同样的 distinct-count 证明）。

改动文件：

- `include/fastsim/config.hpp`：新增 `bool buffer_tso_residence_certificate
  = false;`（默认关闭）。
- `src/config.cpp`：解析 `core.buffer_tso_residence_certificate`；新增两条
  fail-closed 校验——要求 `core.tso_store=buffer`，且要求
  `cache.l1d.replacement=lru`（distinct-count 证明只在真 LRU 下成立）。
- `src/main.cpp`：配置 JSON dump 暴露该开关。
- `src/simulator.cpp`：`prepare_buffer_tso_epoch()` 在 `residence_certificate`
  为真时，用每核每组有界 distinct-line 计数替换 L1 冲突判据
  （`others >= associativity` 才判冲突），L2 保持原保守条件。
- `tests/test_main.cpp`：新增默认关闭、解析、以及两条 fail-closed 校验的覆盖。

验证（生产二进制 `build/fastsim`
sha256 `6edc987b353095fbb98038e52d52428ffc3244a8cf51156f3e25e42e2f7b3b61`）：

- `fastsim_tests` 打印 `all FastSim tests passed`。
- **回退位级一致**：7 案例（LBM C4/C8/C16/C32、Stockfish/TeaLeaf/Graph500 C4）
  开关关闭的逐核 cycles+instructions 与改动前 baseline 完全相同。
- **开启复现原型**：7 案例开启后的 CPI 与隔离原型 LRU 候选逐值一致
  （差 < 5e-7），SQ 峰值均为 114。
- **两条校验各自触发**：cert+`serial`、cert+`tree_plru` 均按预期抛错拒绝。

| 案例 | gem5 CPI | cert-ON CPI | 相对误差 | CPI 绝对误差¹ |
| --- | --- | --- | --- | --- |
| LBM C4 | 0.817326 | 0.772321 | −5.51% | 0.045005 |
| LBM C8 | 0.822077 | 0.757006 | −7.92% | 0.065071 |
| LBM C16 | 0.951678 | 1.042345 | +9.53% | 0.090667 |
| LBM C32 | 1.502989 | 1.757192 | +16.91% | 0.254203 |
| Graph500 C4 | 1.305529 | 1.161412 | −11.04% | 0.144117 |
| Stockfish C4 | 0.668626 | 0.660401 | −1.23% | 0.008225 |
| TeaLeaf C4 | 1.004183 | 0.860932 | −14.27% | 0.143251 |

¹ 单位：cycles/宏指令。

因此该开关**默认保持关闭**：Graph500 C4 与 TeaLeaf C4 明显回退，
LBM C16/C32 过预测与 TeaLeaf 残差属于尚未修复的正交机制，
不得用 drain 常数或负载特判掩盖。开关只在完成修复顺序第 2–4 步、
且全 39 案例矩阵与独立吞吐门禁分别验收后，才考虑改默认或推广。

复现验证：

```bash
# 回退位级一致 + 开启复现原型
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/run_verify.py buffer-cert-off.cfg cert-off
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/run_verify.py buffer-cert-on.cfg cert-on
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/verify_compare.py
```

## 生产落地：`core.buffer_tso_prepare_lifecycle`（默认关闭，2026-09-22）

修复顺序第 2 步的核心机制——独立 prepare/publish 生命周期与退休/canWB
准入下界——以第二个**默认关闭**、与 residence certificate **正交**的开关
`core.buffer_tso_prepare_lifecycle` 落入生产。两开关可单开、任一单开、或同开。

**为什么需要它。** 默认 buffer 路径把 store line 较早的普通访存响应
（`memory_response_cycle`）直接当作权限就绪边喂给 `regular_store_response_cycle`。
该响应可以早于退休，使一条已准备 store 在尚无资格写回时就享受权限响应
（gem5 只在 store 达到 canWB 后才发权限准备）。放宽同组门禁的 residence
certificate 放大了这个提前准备缺陷：Graph500 core 1 / seq 19521535 在退休前
121 周期就宣告权限就绪，导致 Graph500 C4 −4.07%→−11.04%、TeaLeaf −14.27% 回退。

**改动。**

- `include/fastsim/config.hpp`：新增 `bool buffer_tso_prepare_lifecycle = false;`。
- `src/config.cpp`：解析 + fail-closed 校验（要求 `tso_store=buffer`）。
- `src/main.cpp`：配置 JSON dump 暴露该开关。
- `src/simulator.cpp`：commit/TSO-send 通道对 buffer-eligible store 用
  `buffered_permission_ready = max(memory_response_cycle, actual_retire +
  store_latency_cycles)` 替代直接权限边（base 腿对称用
  `base_retire + lower_bound`）。**这是逐 store 准入下界，非借用的延迟常数**；
  FIFO publish 与 store-drain 链不变。
- `src/cache.cpp` / `include/fastsim/cache.hpp`：新增 `PrivateHierarchy::l1_resident()`；
  `buffered_tso_permissions_intact` 在开关开启时要求 **L1 驻留**而非 L1‖L2，
  修正 L2-only dirty 缺口（`publish_buffered_store` 按 `contains()`=L1‖L2 放行，
  但 `mark_dirty()` 只改 L1，L2-only 行会 publish 却不置脏）。命中即按你选择的
  **整-epoch 回退**重新串行化，不静默 publish 非脏行。

**未加什么（诚实边界）。** 未给 prepare 增加端口/MSHR 记账：`cache_store_ports=200`
在现有 trace 上不竞争，且 buffered 路径本就绕过 `DetachedStoreGroup` 的 demand
outstanding 记账，因此 prepare 占端口可证明 CPI-neutral；人为制造竞争属被禁的
凑数，故不做。

**验证**（生产二进制 `build/fastsim`
sha256 `23935901072a6a9f218184c91659d5a4f40c728c757420ad63c4cc9f9d392976`）：

- `fastsim_tests` 打印 `all FastSim tests passed`（新增默认关闭/解析/fail-closed 覆盖）。
- **回退位级一致**：7 案例开关关闭逐核 cycles+instructions 与改动前 baseline 完全相同。
- **反例结构性修正**：`buffered_permission_ready ≥ actual_retire + service > actual_retire`，
  权限不再早于退休。
- **守恒**：7 案例 retirement/UOP/memory/branch 与 baseline 精确守恒，
  每核 CPI 分母一致，SQ 峰值均 114。
- **引擎吞吐/内存**：LBM C16 顺序单跑 off/on/dual 均 ~43.7s、~5.48GB RSS，无可测开销。

**单独消融与双重验证**（关键判据：能否消除 certificate 的 Graph500/TeaLeaf 回退）：

| 案例 | gem5 CPI | cert-only 相对 | life-only 相对 | dual 相对 | dual CPI 绝对误差¹ |
| --- | --- | --- | --- | --- | --- |
| LBM C4 | 0.817326 | −5.51% | +152.6% | −5.36% | 0.04379 |
| LBM C8 | 0.822077 | −7.92% | +129.0% | −7.68% | 0.06314 |
| LBM C16 | 0.951678 | +9.53% | +116.8% | +9.81% | 0.09335 |
| LBM C32 | 1.502989 | +16.91% | +73.9% | +16.79% | 0.25230 |
| Graph500 C4 | 1.305529 | −11.04% | −4.46% | −10.85% | 0.14164 |
| Stockfish C4 | 0.668626 | −1.23% | −0.67% | −1.21% | 0.00808 |
| TeaLeaf C4 | 1.004183 | −14.27% | −13.97% | −14.13% | 0.14192 |

¹ 单位：cycles/宏指令。LBM CPI MAE：cert-only 0.113736 → dual 0.113146。

**结论。** 退休/canWB 准入下界是**正确性修复而非 CPI 驱动**：单开时对 buffer
默认 CPI 影响与原型一致（LBM 仍退化，说明它不放宽同组门禁），与 certificate
同开时几乎不改 certificate 的 CPI（Graph500 −11.04%→−10.85%，TeaLeaf
−14.27%→−14.13%，LBM MAE 0.113736→0.113146）。原因：`store_send + publish=1`
仍主导 `max()`，权限锚点很少绑定。**因此 lifecycle 修好了提前准备缺陷与
L2-only dirty 缺口，但并未消除 Graph500/TeaLeaf 回退**——该回退的主因是其他
正交机制（跨核 / store-drain 语义），不是提前准备。两开关默认皆关，
行为不变；推广仍需完成剩余机制并跑全 39 案例矩阵 + 独立吞吐门禁。

复现：

```bash
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/run_verify.py buffer-life-off.cfg life-off
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/run_verify.py buffer-life-on.cfg life-on
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/run_verify.py buffer-dual-on.cfg dual-on
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/lifecycle_compare.py
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tmp/lbm-buffer-tso-audit-20260921/conservation_check.py
```

## 产物、验证与复现


实验根：`tmp/lbm-buffer-tso-audit-20260921/`。

- `baseline/`、`no-set/`、`lru/`：原始完整 stats、相位 CSV、stdout 和
  `.run.json`（命令/二进制哈希）。`lru-warmup/`、`lru-measurement/` 为相位消融。
- `rescore-*/`：维护 validator 的原生评分及输入指纹；18+2 次 replay 的
  source/native/population checks 全通过，不能据此宣称精度或硬件 PMU 通过。
- `report.json`：从维护评分输出汇总 CPI、守恒、相位计数与 PMU，不自行选真值。
- `source/`、`build/`、`fastsim-ablation`、`fastsim-lru`：隔离代码和二进制；
  环境变量仅存在于该诊断副本，不是维护 CLI 的公开配置。
- `certificate_probe.cpp`：LRU 驻留检查和 L2-only dirty 反例。

验证：隔离 Release 构建成功；默认和 LRU 开启的 `fastsim_tests` 均打印
`all FastSim tests passed`。四项 LBM 的 retirement/UOP/memory/branch 与
baseline 精确守恒、每核 CPI 分母一致、SQ 峰值均为 114；
三个控制及两个相位消融也通过维护验证器。未进行吞吐对比或完整矩阵推广。

生产维护文件本轮新增默认关闭开关 `core.buffer_tso_residence_certificate`
（见上节），开关关闭即位级回退。落地后哈希：

- `src/simulator.cpp`：
  `20c2dfcb8421c886e74e6899437a04f7521e1b764da2f01c3f67d372c5ef525f`
- `build/fastsim`：
  `6edc987b353095fbb98038e52d52428ffc3244a8cf51156f3e25e42e2f7b3b61`

（改动前基线：`src/simulator.cpp`
`429ed793a1fae3d9e25e5feed00ada98046eef5743cfb8585f5dcc9e4984552a`、
`build/fastsim`
`694826ea10e5e0214fe933dbc8aee8b927e24638ed79ba5a71bdb3f612e29f78`。）

复现用原产物重评，不覆盖已有模拟输出：

```bash
/root/miniconda3/envs/yinhaolang/bin/python3 \
  tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/lbm-buffer-tso-audit-20260921/source-set \
  --stats-dir tmp/lbm-buffer-tso-audit-20260921/lru \
  --output-dir tmp/lbm-buffer-tso-audit-20260921/rescore-lru-repeat \
  --expected-cases 4 --jobs 4
```
