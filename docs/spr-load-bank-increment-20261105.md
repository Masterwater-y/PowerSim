# SPR L1D load bank 有界资源组件

`gem5-spr/src/cpu/o3/lsq.cc:276` 使用 `(physical_address >> 2) & 15`，单 bank 每周期最多接受两个；`LSQUnit::trySendPacket` 首先检查总 load port 和 cache blocked，再检查 bank，并在 TimingReq 真正接受后才登记。资格为 `isLoad && inst->isLoad() && !isDataPrefetch && !uncacheable && !strictlyOrdered`，因此 bank 计费粒度是实际 packet 而不是退休宏或整条跨线 UOP，software prefetch、转发、不发包、未知属性不应硬判合格。

本增量新增 `include/fastsim/spr_load_bank.hpp`：稀疏保存周期×16 bank 的已接受次数，支持预测 issue 顺序倒置下插入未来/较早预约；`prune` 只可用未来绝不早于它的单调下界。它是独立组件、尚无运行时开关，未接入 IntervalCore 或第二阶段，不会改变 CPI/PMU。小测试覆盖同 bank 超额、不同 bank、未来预约、回收、复制与 reset；另将仓库 gem5 实际 `loadBankAvailable` 和 `noteLoadBank` 原方法提取到轻量 shim，20000 周期×16 次随机尝试逐项对照，通过。该测试证明局部准入规则，不证明整个流水与 gem5 等价，且尚未测试端到端吞吐。

接入前需协调全局配置和序列化，显式 opt-in 默认关闭；基于 FST 功能信息复原地址/访问属性和分片后，在现有时间窗口内的预测包接收时间调用可用检查，只有请求通过所有资源并成功接收后 `accept`。对于第一阶段已有非单调 issue，必须使用可证明的单调下界回收；不要把执行 UOP 的 issue 自动当成 gem5 实际发包周期。重放、预览和回滚应保存或延迟提交 reservation 以避免重复记账。需对 split、同线不同 offset、四核独立、不同 chunk、预测反馈、流控和原模型 OFF 不变性做集成测试，然后由统一验证测 CPI 和吞吐。相关全局配置/阶段二文件由其他并行任务负责，本增量不修改它们。

## 阶段二接线建议

1. `ChunkMemoryEvent` 在 feature 打开时保存精确 packet 地址和独立资格（包括 unknown），不能只用 `line*line_size` 代替真实偏移。对动态翻译跨片段逐段核对 packet 拆分；无法确定分片/属性时走既有路径并报告 fallback。
2. `TimingFeedback` 新增每核 `SprLoadBank` 工作副本，参照 `response_spr_l1_load_fill_ready_` 在窗口开始复制、只对最终接受前缀提交。试算、suffix 重放和回滚必须撤销其预约；若 fast block-transfer 无此资源语义，开启 feature 时回退现有 scalar 路径，报告吞吐成本。
3. 在 `compute_core_timing_feedback_impl` 的每个请求事件上将银行准入与已有 load-port、Sequencer 和 fill 信用一起组合。**不可**先占 bank 再因更晚的信用/缓存阻塞而保留错误预约；只在最终模型接收边调用 `accept`。`first_available` 不产生空周期事件。bank 若推迟已保守预约的 fill 起点，可保留较早开始的预约并延长完成边作为明确保守近似，检查冲突并计数；若组合无法保证则标记该事件走旧路径，不做整窗反复求解。
4. `actual_dispatch` 作为回收边只能在证明跨 chunk 单调且使用同一时钟后才用；否则保留到可证明边界。落地时对 `accepted_producer_issue_cycle` 和所有其他依赖该值的分支同步使用银行延迟后的时间，不能部分反馈。
5. 验收至少包含：3 个同 bank load、不同 bank、同一 cache line 不同 4B offset、split packet、多核独立、端口阻塞/请求未接受不占 bank、倒序未来预约、跨 chunk、预览取消/重试、fallback 和开关 OFF 位级不变。然后完整构建和 FastSim 测试，并用同 FST 测多负载 CPI 绝对误差、PMU 与隔离吞吐。FLAC 当前 gem5 `cacheLoadPorts=3` 且 bank 启用，属于三路受每 bank 两路约束，不得误用每 bank 一路。

## 阶段二实验接入

已增加默认关闭 `core.spr_load_bank_feedback`。该切片只处理有明确物理地址、
功能请求事实证明 ordinary、非 split、单行单 fragment 的 load；page walk、
软件预取、forwarding、atomic 和特殊请求不占本账；其他普通 load 计 fallback。
按模型第二阶段的估计准入 cycle 做每核预约，目标 3 load port 的冲突与分散
微测通过；真实目标 packet 发送时序及拒绝/重试仍是两阶段近似。当前禁止
与 fill、多个 service/retry solver、paired frontier、CPI attribution 等组合，
禁用部分 block shortcut；默认 OFF。不读取任何 gem5 OoO 事件。source 状态由
TimingFeedback 按核复制、接受后提交，dispatch 单调下界用于历史回收。

初版复审发现非物理地址和 split 过滤缺口；对应对照运行在完成前主动终止。
修复后定向测试证明两种情况均回退，完整 `fastsim_tests` 和目标3-port微测
通过。历史四案 FST 没有 `.sreq`，缺资格时所有候选回退：XZ 3297515、FLAC
5693585、Graph500 13329354、Marian 3512768；同二进制 OFF/ON 每案 CPI
不变，该结果只证明回退路径，不说明 bank 无影响。完整伴随的新 FLAC C4
OFF/ON 尚在运行；不同实验的各项 CPI 及绝对误差以统一报告为准。

复审发现旧版在 bank 推迟请求后可能与下一周期其他请求超过总 load-port，
其尚未完成的完整 FLAC 运行已终止，不用作结果。当前修订在每核同一稀疏
日历联合检查总 load port 和已知 bank，并让 bank 资格不明的非 atomic load
仍占总端口但不猜 bank。atomic、特殊控制及其他不经过该普通 load 子域的
操作仍走原模型，因此不能据此宣称所有请求资源已完全统一；端到端回放及
吞吐仍待完成。受限模式暂不支持 sparse resource repair 和 shared service
constraints，避免事后重排使该日历失效。

联合日历使用同一稀疏 cycle 条目记录 total load-port 与 16 bank 次数；在
候选预计接收时间查找同时满足两个资源的最早空档，确认接受后一次性占额。
普通 load 的 bank 资格不明时仅使用 total port，不推测 bank。定向测试覆盖
“周期0同 bank 第3项被推到周期1，而周期1已有3个其他 bank 请求”的反例；
避免延后后超总端口。未完成的 v2 新 FLAC 对照已停止，不产生可用结果。

进一步复审：split 或功能请求事实缺失时，单个模型 cache-line fragment 不足
以证明恰是一条实际 LSQ packet。上述未知 bank 路径仅在 physical、complete、
非 split、单片 facts 可证实时占 total port；其他情况保留原路径，增加
`spr_load_port_fallbacks`，因此这部分与已知请求的端口竞争可能被低估，不能
宣称完整 packet 守恒。v3 未完成的新 FLAC 对照已主动终止；当前修改等待
与预取统一构建、回归和重新回放。

## 混合资格与跨 chunk 定向检查

`tests/test_spr_load_bank_mixed_calendar.py` 在独立组件层验证：上一 chunk 已提交的
两个同 bank 预约可随状态复制进入下一 chunk；已知是单 packet 但 bank 不确定的
请求占第三个总端口；回滚后第三个预约不残留；prune 只删除严格早于下界的周期；
同一接受序列在单 chunk 和分 chunk 下得到相同准入。四项 `test_spr_load_bank*.py`
测试通过。此测试不调用 Simulator，不能单独证明运行时的地址资格、排序和状态提交。

测试还给出反例：3 路总端口全被认证请求预约时，一个没有纳入日历的 fallback
如果实际也在该周期发出，就可能产生 4 个实际 packet；其具体 packet 数和时间
未知。故 `spr_load_port_packets` 是已证实子集，`spr_load_port_fallbacks` 是缺口
事件数，不应将两者相加解释为真实 packet 总数或声称总体端口严格守恒。

后续集成验收建议：构造同一 core 在 chunk 边界相邻的已认证请求，以及无 facts、
非物理、跨线等 fallback 与已认证请求交错的输入，分别验证提交/试算回滚、计数
口径和 fallback 覆盖率；再用能独立观测模型接收周期的测试钩子检查总端口重叠。
当前无此钩子，组件反例不声称观察到了实际负载中的超额。必须保持其他排他的
resource solver 关闭，并分开报告准确率和吞吐；在 fallback 未入账时，对实际
端口压力的影响只能描述为潜在低估，不能当成定量误差结论。

## 未认证 load 片段的通用端口代理（实验性）

新增默认关闭的 `core.spr_load_port_fallback_proxy`，仅可与现有 `spr_load_bank_feedback` 一起开启。对旧路径已计为 `spr_load_port_fallbacks` 的模型 load，若没有已知特殊属性且不是 forwarded、atomic、page walk、取指、软件预取、page-fault 旁路、跳过或已合并请求，则每个已有模型 cache-line fragment 在同一个稀疏总 load-port 日历预约一次；未知 bank 不预约任何 bank。模型仍按原路径发出每个 hierarchy 请求。认证 packet 数 `spr_load_port_packets` 和原 `spr_load_port_fallbacks` 保留原口径，新增 `spr_load_port_proxy_fragments` 和其中因端口占满而等待的 `spr_load_port_proxy_waits`。

该代理不证明 hardware LSQ packet 的数量：一个 fragment 可能对应不同数量的硬件 packet，模型没有证据推测其 bank，故不能宣称真实 packet 完整守恒。它对未知请求的端口压力既可能高估也可能低估；已知 atomic、secure、uncacheable、strict、masked、HTM、软件预取和其他已知特殊请求仍非本代理覆盖；缺失 facts 的请求按普通片段近似，无法证明它没有未知特殊属性。已认证与未认证请求共享同一宽度，不引入逐周期循环、无界收敛或 OoO 输入。OFF 保持原路径；统一构建和 `fastsim_tests` 已通过；Simulator 混合资格请求实际触发通用端口等待，跨 chunk、跨行 split、已知 secure、forwarded、缺失 facts 和请求人口等断言通过。银行稀疏日历的 4 项 Python 组件测试也通过。真实负载 CPI/PMU 和吞吐尚未评估。

## FLAC C4：bank 开启下的 fallback 代理消融（2026-11-05）

两侧使用同一冻结二进制 `tmp/spr-load-port-proxy-v2-20261105/fastsim`（SHA-256 `6551140c4510fcadcba563c75158094ae8c82f157e40ac0f384822438a323aa5`）、同一 FLAC C4 完整 FST、同一功能预热及 bank ON canonical 配置。输出配置唯一差异是 `spr_load_port_fallback_proxy`；完整 v2 功能身份相同，均为 `sha256:b394bc3aad682029db692af25759b3fddd6267fd6214c6e0c004132d14acaed7`。新 OFF 与先前同源 bank ON 输出的 `totals`、`time_accounting`、`thread_state`、`cores`、`cha` 共同字段完全一致；两次新输出没有独立经过 native validator，以下是匹配人口、使用此前已验证同源 gem5 参考的诊断，不继承旧输出的 passed 状态。身份完整不改变 `roi_edges=false`、`macro_boundary_complete=false` 的来源限制。

| fallback 代理 | FastSim task active CPI | gem5 CPI | CPI 绝对误差 (cycles/macro) | CPI 相对绝对误差 |
|---|---:|---:|---:|---:|
| OFF | 0.319261048805 | 0.281871935617 | 0.037389113188 | 13.264575% |
| ON | 0.319260568751 | 0.281871935617 | 0.037388633134 | 13.264404% |

任务宏指令数均为 31,246,502，活跃周期为 9,975,791 / 9,975,776；ON 只少 15 cycles，CPI 变化约 −0.0000004801，在本案中几乎不可见，不能据此认定精度得到有意义改善。各核退休宏指令数两侧相同，依次为 4,678,653 / 9,067,614 / 8,464,977 / 9,035,325；各核 UOP、memory 及 Sequencer 请求人口也一致，分别为 UOP 5,695,137 / 10,003,324 / 9,363,835 / 9,867,738，memory 和 Sequencer 请求各 1,733,744 / 1,956,196 / 1,812,133 / 1,935,695。L1D、L2 accesses 逐核及 LLC accesses 总数也一致。

| 核 | 端口 fallback 事件 | ON 代理 fragment | 代理 fragment 等待 | bank fallback |
|---:|---:|---:|---:|---:|
| 0 | 2,758 | 2,758 | 82 | 3,968 |
| 1 | 230,526 | 230,526 | 35,538 | 230,579 |
| 2 | 272,764 | 272,764 | 44,530 | 273,067 |
| 3 | 289,178 | 289,178 | 44,588 | 289,242 |
| 合计 | 795,226 | 795,226 | 124,738 | 796,856 |

在该输入下，代理为所有被统计的 fallback 事件各计入一个模型 fragment，约 15.69% 的代理 fragment 遇到端口等待；这不证明 fragment 与真实硬件 packet 等价，也不证明这些事件没有未知特殊属性。已认证 bank packet 两侧均 4,897,965，未推测未知 bank；原 fallback 计数没有被抹去。总端口/银行等待的逐事件累计从 37,004 增至 210,071 cycles，非互斥计数可能重叠，不能直接与退休周期或 CPI 相加，也不能仅根据汇总断言具体隐藏机制。

两次在相邻 CPU 集合 128–135 / 136–143 并行执行；OFF/ON 预热为 8.738 / 8.740 s，模型测量 56.301 / 56.150 s，测量吞吐约 0.5522 / 0.5537 MIPS，完整身份计算 56.849 / 56.857 s，进程端到端 198.16 / 198.02 s。共享资源干扰使单次运行时间仅供诊断，不能用作高吞吐验收。原始输出、配置及日志位于 `tmp/spr-load-port-proxy-v2-20261105/{off,on}.{json,cfg,log,time}`；未重复完整原生重评分。
