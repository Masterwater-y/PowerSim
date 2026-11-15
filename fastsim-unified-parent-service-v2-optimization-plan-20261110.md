# FastSim 统一父服务 v2：下一步优化方案

日期：2026-11-10。状态：方案稿，本次未执行代码修改、构建或新回放。

候选基线为 `tmp/parent-service-v2-exclusive-20261110/attempt2/source/`。两个后台批次已按用户要求停止，保留 34/35 次验证及 10/30 次直接吞吐对照的成功结果；完整 v2 尚未验收。[停止记录](tmp/parent-service-v2-exclusive-20261110/stopped-by-user.json)保存批次状态。

## 1. 优化目标与优先顺序

**先完成 R0/R1 的宿主成本控制与拒绝原因诊断，再补全可重算的模型服务时间接口，扩展 prepare 接管和统一准入，最后分别验收机制、CPI 和吞吐。**

prepare 保持开启。保留原并行两阶段、一次反馈和有界近似，输入只来自退休功能流及功能 companions。gem5 OoO 时间仅作独立诊断参考；模型延迟参数遵循实际采集配置的映射合同。LLC/预取迁移、广泛合并及延迟 Tag 发布继续默认关闭。

本方案修订[原 v2 设计](fastsim-unified-parent-service-design-v2-20261110.md)中时间接口和下一轮实施顺序；[隔离实现记录](fastsim-unified-parent-service-v2-implementation-20261110.md)仍描述当前已实现范围。新增接口、近似策略及性能收益均待验证，不能视为已完成能力。

## 2. 当前证据支持的优先项

### 2.1 prepare 流量存在，拆边与附着覆盖尚未打开

下面是冻结候选首轮 `r0-v2-wait` 的实际计数，属于模型内部诊断，不是 native cache 真值或硬件 PMU。

| 负载 | prepare GETX | prepare UPGRADE | split services | prepare attaches | resource source fallbacks |
| --- | ---: | ---: | ---: | ---: | ---: |
| FLAC | 3,078 | 52 | 0 | 0 | 0 |
| Graph500 | 2 | 14 | 0 | 0 | 0 |
| Marian | 1,899 | 231 | 0 | 0 | 0 |
| XZ | 30,349 | 5,981 | 0 | 0 | 0 |

来源为 [FLAC](tmp/parent-service-v2-exclusive-20261110/attempt2/results/r0-v2-wait/c04-817.flac_s.json)、[Graph500](tmp/parent-service-v2-exclusive-20261110/attempt2/results/r0-v2-wait/c04-854.graph500_s.json)、[Marian](tmp/parent-service-v2-exclusive-20261110/attempt2/results/r0-v2-wait/c04-872.marian_s.json)、[XZ](tmp/parent-service-v2-exclusive-20261110/attempt2/results/r0-v2-wait/c04-801.xz_s.json)的 `unified_parent_service`。

这支持优先检查源资格和拆边接口。资源拒绝为零并不证明容量足够：尚未走到资源检查的请求不进入该计数。`source_fallbacks` 还包含普通读，不能用其总数除以 prepare 人口计算失败率。已有 CPI 变化也不能归因于尚未激活的 prepare 拆边机制。

### 2.2 当前准入与已选源时间不一致

当前 `simulator.cpp` 的 prepare 源资格要求：

```text
min(store_send, prepare_issue) == selected_shared_issue
and !functional_carried
```

其中 `prepare_issue` 来自实际退休时间加既有 commit-to-admission 边，并受到扫描前沿限制。`store_unified()` 还要求动作和目标有效、lower return 有效且位于 admission 与 done 之间，随后才检查服务资源容量。代码落点见[源资格入口](tmp/parent-service-v2-exclusive-20261110/attempt2/source/src/simulator.cpp)约 17529–17545 行及[拆边入口](tmp/parent-service-v2-exclusive-20261110/attempt2/source/include/fastsim/legacy_prepare_timing.hpp)约 205–238 行。

保存的[真实两阶段反例](tmp/parent-service-v2-exclusive-20261110/attempt2/results/runtime-prepare-witness.json)中，已选下层 issue 为 5，实际 retire 为 6，最早 canWB 为 8；32 个退休 UOP 保留，产生 1 个 prepare GETX、0 个拆边服务及 2 次 source fallback。

该反例证明接口存在来源时间倒置，尚不能证明五案所有拒绝都由相等条件造成。下一轮必须逐项分解拒绝原因。

### 2.3 宿主成本控制仍有未完成项

当前记录块已采用 32 槽 COW，活跃服务可按 ID 查询；行索引、expiry 和部分释放索引仍按有界容器快照。旧 checkpoint-local 两层 MSHR 租约与新的 service-owned TBE/fill 租约尚未完成统一准入。因此，现阶段同时存在覆盖限制、索引复制成本和重复容量约束风险。

本轮后台测量未完成，五案并行数据只能用于争用诊断，不能据此发布独占宿主开销或收益百分比。

## 3. 时间接口：区分硬事实与模型估计

### 3.1 两类资格分别判断

| 类别 | 必须满足的条件 | 处理方式 |
| --- | --- | --- |
| 功能和所有权事实 | 请求人口、访问前状态、实际 GETX/UPGRADE 动作、内容/权限代次、事务 ID、资源拥有者 | 缺失则保留明确的 unknown/unsupported；禁止猜测、删请求或借用其他代次 |
| 模型时间 | prepare 准入、排队、数据/权限/controller 完成和请求回调 | 在已知功能路径上允许有声明的有界估计；区分估计值、上下界和已选定时间 |

时间近似是 FastSim 的设计选择。新的 estimated 路径应由功能身份和近似合同控制，不再把“等于旧 selected issue”当作普遍入口。`selected_fixed` 仍只表示模型曾选定，不代表硬件真实时刻；估计时间也不能升级为可证明的准入上界。

### 3.2 共享阶段输出服务分量

拟增加固定大小的服务描述，随现有反馈槽传递：

```text
功能部分：事务动作、源/目标域、事务与内容/权限代次、兼容父 ID
路径部分：配置可解释的名义传输、lookup、fill、controller 和 callback 分量
队列部分：共享拥有者提供的有界队列摘要、摘要时钟和适用范围
质量部分：每条边的生产者、有效性、estimated/selected/bound/legacy 类型
资源部分：实际分配类型、唯一拥有者和释放边引用
```

队列摘要是近似策略，不是把旧请求已承受的 stall 原样搬到新 issue。首片使用窗口前冻结状态和已有阶段可提供的有界摘要；策略不得按负载名称拟合，不增加独立共享事件推进器。

旧绝对 lower return 在新 estimated 路径中只作诊断。仍满足冻结源合同的窄路径可以继续使用旧接口，但同一事务必须选择唯一时间拥有者，不能混合新准入与旧绝对响应。

### 3.3 在一次反馈中计算固定服务边

prepare 的模型准入由既有资格与唯一资源约束归约：

\[
a_s=\max(e_s,f_s,p_s,c_s),
\]

其中 \(e_s\) 为功能/退休资格，\(f_s\) 为适用的扫描或顺序前驱，\(p_s\) 为端口准入，\(c_s\) 为硬件资源准入。随后使用固定深度的路径模板：

\[
t_v=\max_{u\in pred(v)}(t_u+\ell_{u,v}).
\]

队列摘要如何进入该模板必须明确其基准和作用节点；已经由资源前沿收费的等待不得在共享分量中再次收费。GETX 的数据边、权限边与 controller 边按已有协议映射分别计算，不能默认全等于 lower return。

计算发生在现有 replay/feedback 内，每条受支持路径只求值一次，并统一提交服务、lease、索引和统计 delta。保留原确定性功能服务选择，将共享排序冻结造成的误差声明为模型近似。若新的时间关系使数据版本、事务动作或已提交共享状态失效，必须在提交前限制该路径或执行一次有界组件回退。

禁止直接放松 `==` 后复用旧响应，也禁止使用 `max(old_ready, new_issue + old_latency)` 平移旧结果。它们无法解决旧排队结果与新准入不匹配的问题。

## 4. 分阶段实施

### 4.1 第一片：R0/R1 成本与拒绝原因

这一片优先完成，不扩展 prepare 行为范围。

- 拒绝原因按角色和 GETX/UPGRADE 分组：目标/代次未知、carried 源、准入不匹配、lower return 缺失、lower return 区间不适用、资源水位限制、硬件容量、宿主池预算、回退组件不可隔离。
- 输出互斥的“首次拒绝原因”人口，必要时另保留固定大小原因位图。prepare 分母使用实际候选数；普通读独立统计。
- 驻留完成 hit 快速返回；活跃引用按 ID 查找，避免普通 hit 进行全行候选或 prepare 扫描。
- 保留现有块 COW，优先将行索引和释放/expiry 索引迁为 touched journal 或可事务提交的增量 delta；诊断冷字段与常用热字段分开。
- 记录每窗口触碰槽、复制字节、索引操作、堆压缩和回退处理人口，控制总回退工作量；不只限制单个组件大小。

验收要求：prepare ON，响应、功能计数和资源释放中性；回滚能恢复记录与全部引用索引。宿主池满属于表示预算，不能伪装为硬件 MSHR stall。性能措施验证完成后再扩展接管覆盖。

### 4.2 第二片：接入 estimated 服务分量

在新隔离源码中增加时间质量类型、配置分量和有界摘要接口，沿用内部 Q16 时间；跨域比较携带 clock epoch，报告时才按原合同取整。首先用真实两阶段反例验证：源身份有效时，issue=5 与最终准入=8 的不匹配能够走声明的估计路径，所有服务边由新准入计算；不保留旧源时间冒充完成资格。

对缺少内容/权限代次、未知动作或无法隔离共享副作用的路径，完整保留原行为及原因。不得为了提高覆盖率批量清除 unknown。

验收还须覆盖跨窗口已接受服务、同窗口父引用、时钟映射、时间回退及一次回退后的状态恢复。组件和图深度固定有界，不建立全 ROI 依赖图或整窗收敛。

### 4.3 第三片：prepare、load 与 store 接管

| prepare 前状态 | 服务与资源 | load 约束 | store 行为 |
| --- | --- | --- | --- |
| I | GETX；一份 TBE 与 data-fill 服务 | 等正确数据边和适用 controller 边 | 接管原 ID；复用原资源与下层事务 |
| S | UPGRADE；TBE，无新 data fill | 旧数据驻留；按已核对 SP_IE 策略等待 controller | 接管升级，取得权限后写入 |
| E/M | 提示确认，无新下层事务 | 不新增提示回填等待 | 使用既有发送与写入合同 |
| 兼容 transient | 引用原服务，提示 credit 独立完成 | 使用原事务相应边 | 按状态合同接管 |
| 身份或状态未知 | 原完整路径 | 不消费未认证代次 | mandatory store 保留 |

同一事务保留 immutable origin 和 active consumer。prepare credit 的归还不释放仍占用的 TBE/fill；store 接管不再次分配原资源。

store 继续分离发送、实际写入和 callback：

\[
t_{\rm send}=\max(\text{地址/写数据资格、退休/发送门、FIFO、端口、请求容量}),
\]

\[
t_{\rm write}\geq\max(t_{\rm send},t_{\rm line\ data},t_{\rm permission},t_{\rm write\ port}).
\]

permission 不一律加入 send 门，否则会禁止提前接管。load 在数据/controller 门后执行一次所需本地路径；旧行数据与 store 写入后的新内容分开，更早的 read 不继承未来 store callback。

### 4.4 第四片：统一硬件资源准入

当前 `can_own()` 对冻结源做事后容量认证，不能代替真实准入计算。新路径在确定服务完成边前处理硬件容量，使容量不足能够延后模型准入，并通过同一模板一次计算后续边。

按域和资源类型逐项迁移旧 MSHR、TBE、fill 约束，建立唯一分配与释放来源。对应旧约束切换后读取统一服务引用，不能继续独立收费；未迁移资源保持明确边界。不能因为名字不同就把 MSHR/TBE/fill 全部相加，也不能因为合并记录就删除协议实际需要的资源。

半开占用区间继续满足：

\[
O_p(t)=\sum_{\lambda\in p}\mathbf 1[a_\lambda\leq t<e_\lambda]\leq K_p.
\]

硬件资源容量与宿主池预算分别处理。共享资源由原拥有者使用有界摘要和一次提交处理，核 worker 不新增逐请求共享锁。容量路径只推进必要的有界释放前沿；无法在现有阶段内表达的情况保留范围限制。

验收覆盖容量满、同周期释放/分配、S 升级无 fill、接管不重复分配、credit 提前归还、取消发布后事务继续收尾，以及槽复用旧 ID 失效。

### 4.5 第五片：机制、精度与吞吐分别验收

先运行隔离 Release 构建、`fastsim_tests` 和必要的真实两阶段定向反例，保持 parallel LTO。机制通过后再回放五案，不自动续跑当前被停止的批次。

## 5. 验收证据与报告口径

| 门禁 | 要求 | 结果用途 |
| --- | --- | --- |
| 输入和身份 | 冻结五案及逐核宏/UOP、ROI、active scope、配置与二进制；独立 identity 核验 | 确认比较对象 |
| 中性 | off/observe 的人口、功能结果、时序及资源一致 | 确认结构迁移不改行为 |
| 机制 | 事务/资源守恒，estimated 与冻结源分开；prepare 拆边与附着有真实见证 | 确认新增机制实际执行 |
| 功能差异 | 对 wait 中的 L1/L2 差异定位首个功能序数、跨核排序、代次和事务动作 | 区分受支持排序变化与状态泄漏/重复执行；未解释则保持未验收 |
| CPI | 每案列 gem5/FastSim CPI、AE 和相对误差；聚合列 MAE 与 MAPE | 判断同输入模型误差 |
| 吞吐 | 安静宿主、同输入同绑核、v1/冻结 v2/新候选交错配对；报告重复离散度 | 判断端到端宿主成本 |

CPI 使用各自 active cycles 与宏指令分母：

\[
\mathrm{AE}_i=|\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}|,\qquad
\mathrm{MAE}=\frac1N\sum_i\mathrm{AE}_i,
\]

\[
\mathrm{RE}_i=\frac{\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}}{\mathrm{CPI}_{G,i}},
\qquad
\mathrm{MAPE}=\frac1N\sum_i|\mathrm{RE}_i|.
\]

AE/MAE 单位为 cycles/macro；RE/MAPE 报告百分比，MAPE 不与 WAPE 混用。先审查 Marian/FLAC 的 `eligible→send`、`send→write` 和 `write→callback` 唯一 blocker，再判断残差，禁止用关闭 prepare 或重复收费后的参数拟合掩盖问题。

本次按用户指示跳过 native 来源审计，后续 CPI 仅复用已保存并核对的独立参考；内部 L1/L2 差异只能用于机制诊断。不得把 path-class cache 字段当 native 真值，也不发布 native PMU 或严格硬件 perf 精度结论。[准确率合同](docs/accuracy-reporting-contract.md)第 0、8 节继续适用。

五案并行适用于精度回放和争用诊断；吞吐验收另做安静宿主配对。不得用旧宿主上的 v1 样本除以新宿主上的 v2 样本，也不将组件复制微基准外推为整体开销。本方案不预设缺少证据的收益百分比或任意性能预算。

## 6. 直接下一步

第一份实施补丁应只包含 **prepare 专属拒绝原因账本、时间质量接口骨架及 R0/R1 索引复制成本控制**。它先验证中性与宿主成本，再接入 estimated 服务分量。随后依次推进 prepare 状态/接管、统一资源准入和五案验收。

当前冻结候选与原始输出保持为比较基线；后续实施使用新的隔离目录。每片记录实际覆盖、未迁移约束和回退总工作量，完整 v2 在所有门禁闭合前继续默认关闭。
