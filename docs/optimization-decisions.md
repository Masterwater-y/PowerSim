# FastSim 优化决策与停止条件

本文件记录当前采用、暂停和待验证的优化方向。开始 CPI/P99 或吞吐研究前先查阅此文件；已有原始结果不应因历史计划仍列为高优先级而被重复扫描。结论变更应记录新证据及被替代的决定。

## 2026-09-14 投影候选跨负载试验完成：C13 热点收益保留，端到端推广失败

按后续明确要求，新增 `dram.projected_feedback` 与独立实验配置，重新开放下方
因 C1 对照失败而暂停的生产反馈试验；维护默认仍关闭，旧 cross-Q guard 不变。
见 [生产响应反馈试验](projected-dram-feedback-pilot-20260914.md)。
候选替换原 FRFCFS 反馈步骤，真实 RD/WB 入持久投影控制器，响应进入原核心/SQ
反馈；缓存、MSHR、fill/merge 仍为 canonical 路径，不能称为实际到达或 fill 一致
修复。构建、整套测试、五配置同二进制完整共同窗口 A/B 与原始输出核验已完成。
五项 source/oracle identity 通过，off 与冻结默认的 scope/threads 全部相同，
on/off 逐核指令/UOP 人口及 RD/WB ownership 守恒。

LBM C32、C8、TeaLeaf C4、Graph500 C8、Stockfish C4 共 56 条逐核 trace，
1 项改善、4 项回退（Stockfish 变化极小）。合并 pilot MAPE 4.2618%→10.4877%，
CPI MAE 0.105855→0.345963；非 LBM 三项 MAPE 3.9751%→4.0603%，CPI MAE
0.058580→0.058962。LBM C32 CPI 4.200373→5.370057，相对误差
+7.5364%→+37.4823%，CPI 绝对误差 0.294373→1.464057；32 核未加权 CPI MAE
0.321020→1.544623。C32 ABBA 两次 on/off 各自时序完全一致，吞吐均值
4.090104M→3.532271M user-UOP/s，观测下降 13.64%；其他配置只有单次 AB，
共享宿主机测量不构成 quiet-host 准入。

C13 generic 审计与开启后的 materialized scope/threads 完全一致：同一组
315 个 DRAM store 的服务均值 647.88→461.28、服务 MAE 375.78→230.10
cycles/request，选定窗口 208,950→149,753 cycles（gem5 99,099），原 430 条
SQ owner 关系全部保留。但 C13 整核 CPI 误差仍由 +11.9942%→+40.6612%，
CPI 绝对误差 0.469645→1.592134。因此并非只有 C1 抵消了普遍的 C13 收益：
热点修好与同一核心其他区段退化可以同时发生。服务 MAE 不是 CPI MAE。

候选按用户决定否决并关闭（2026-09-14）：保留 opt-in 代码与失败证据，但
`configs/gem5-exp-projected-dram-feedback.cfg` 现设 `dram.projected_feedback = false`，
维护默认本就关闭、保持不变，不扩大 40 项矩阵、不宣称 LBM 端到端修复。关闭后
默认核验重现冻结基线：LBM C32 宏指令 CPI 4.200373（+7.5364%、绝对误差 0.294373
cycles/macro-instruction、32 核 CPI MAE 0.321020），投影批次/读/写全为 0
（`tmp/projected-feedback-disabled-20260914.no4tJO/verify.json`）。若要进一步诊断，
只能临时把开关置 true 复现被否决的试验，绝不作为交付默认。下一项先做固定请求流的
晚发现钳制/混合队列选择消融，再验证反馈后的批次变化，保留 C1 与 C13 控制；成本优化
聚焦扫描、owner 查找及重复服务求解。不按 PC/负载选取收益，不只保留加速响应，不自动
回到全核心 continuation。

## 2026-09-14 吞吐优先的投影 RD/WB 实验：热点改善，反向控制失败

用户重新明确保留两阶段近似、尽量少损失吞吐。完整核心跨 Q continuation 不再是
下一项实验的先决条件；下方历史“必须先接核心恢复”仅适用于实际到达闭合方案。
新实施见 [投影预约与真实请求流验证](lbm-projected-dram-20260914.md)。

新增独立投影批量预约 API、事务内的生产只读 RD/WB 捕获和离线重放工具。完整 C32
共 7,141,740 条真实 RD/WB，全部准入、读全部返回、末尾保留 437 个 WB。C13 的
315 个关键 store 全覆盖；固定路径服务均值 647.88→549.83 cycles，逐请求服务 MAE
375.78→289.64 cycles。但 C1 2M/8M 服务 MAE 分别 62.84→128.08、
121.26→205.96，反向门禁失败。服务 MAE 不是 CPI MAE，不将重叠等待之和换算 CPI。

以下为被上方后续授权试验更新的历史停止决定：该候选不接生产 latency/SQ 反馈，
不扩大矩阵，不开启旧 cross-Q guard。默认 capture
运行与冻结结果 scope/threads 完全相同：C32 +7.5364%，CPI 绝对误差 0.294373，
32 核未加权 CPI MAE 0.321020。构建、整套测试和控制器 ASan/UBSan 通过。
离线单次控制器耗时约 7.05s，不代表生产吞吐开销；默认关闭的 ABBA 单独记录。
默认关闭的 ABBA 均值为 4.137325M→4.113551M user-UOP/s，观测下降 0.5746%；
每版仅两次、共享宿主机且均低于 5M 门槛，不声称吞吐验收通过。该测量早于最终
离线容量边界修复；最终版重放 CSV 逐字节不变，完整 C32 默认 scope/threads 再次
等价，详见 `final-verification.json`。C1 8M 平均直接晚到钳制仅 7.08 cycles，而
服务增加 108.91 cycles，不能把退化全部归因于直接钳制；这只是分账，尚非因果消融。
下一候选先分离晚到钳制与排队策略的新增等待，并处理反向控制；不能用热点收益
绕过门禁，也不因此自动恢复昂贵的全核心闭合路线。

## 2026-09-14 LBM 继续实施：共享 memory-leg 已接混合服务，核心恢复仍未接通

见 [共享服务子路径与核心阶段拆分](lbm-cross-q-shared-memory-20260914.md)。
SharedSystem 唯一 miss 子路径已调用持久 RD/WB 控制器，带未决 MSHR、fill 可见性、
真实 dirty victim、上游 L2 owner 数量门禁与事务回滚；目前新入口仅由定向测试驱动。
既有四阶段计算已抽出并使用显式依赖，但尚无 retained core context。
构建、整套/定向测试及范围独立审查通过，维护配置未变。
全输入 C32默认 scope/threads 逐项等价：+7.536430%，CPI绝对误差0.294373，
32核未加权 CPI MAE0.321020。没有新精度收益或吞吐结论。
重链两核生产门禁仍RED，combined仍明确拒绝。下一步是保留核心状态并接入全来源
frontier/真实lookup与store send，不能解除guard、强排批尾或扩大矩阵代替关键覆盖。

## 2026-09-14 LBM 跨 Q 基础改造：共享未决状态与 owner 已落地，完整生产仍未接通

见 [跨 Q 基础状态实施与验证](lbm-cross-q-foundation-20260914.md)。新增显式默认 off
配置、SharedSystem 未决 fill 归属/不可变发布/回滚、有界资源 owner 和拆分 store
全分片 SQ 释放；旧闭合 store 服务循环已使用 group 归属。构建、整套测试、组件
sanitizer 及部分范围独立审查通过，维护默认 ordinary3/ready1、Q=1024 不变。

完整共同窗口 LBM C32 默认结果逐项等价：+7.5364%，CPI 绝对误差 0.294373，
32 核不加权 CPI MAE 0.321020，没有精度收益。两核生产验收仍为 RED；非 off
模式在执行前明确拒绝，因为四阶段可恢复核心、全来源 frontier 与混合控制器
生产调用仍未接通。不能把新增 owner API 或闭合 store 适配等同于跨 Q 修复。
不扩大 C4–C32/40 项；继续完成核心恢复，再验证 C13 的 315-store/430-owner 覆盖。

## 2026-09-12 LBM 第一批实施：组件通过，跨 Q 接入门禁未通过

见 [持久混合控制器与 store 服务证书](lbm-mixed-service-phase1-20260912.md)。新增独立
RD/WB 控制器、跨调用 ownership/方向日历/准入与返回边，已接常规测试但尚无生产
Simulator 调用者；不能称作 LBM 混合服务已接入。修复了现有 post-commit 稳定请求表
未发布、后续 FR-FCFS 改写已接受时序的真实缺陷。定向 RED/GREEN、整套测试、独立
审查及控制器 ASan/UBSan 通过，默认配置保持 ordinary3/ready1。

完整共同窗口 C32 默认重放精确等价：相对误差 +7.5364%，CPI 绝对误差 0.294373。
post-commit 同配置旧/新二进制也精确等价：均 +10.1291%，CPI 绝对误差 0.395644；
并非这次补丁新增退化，也没有 LBM CPI 收益。两组 32 核 CPI MAE（不加权）分别
0.321020、0.426127，全部核心为正误差。

31,308 个 post-commit 候选仅 1 个稳定，其余全部跨 horizon 回退。停止推广这一
有限组合，不扩 C4–C32 或全 40 项。下一项必须分离未决 service/fill owner、请求准入与
UOP 退休游标，提取可恢复核心状态并证明全来源 exclusive frontier；不能强排批末、
放宽旧 guard、用旧 latency 充当新响应或以未接入模块宣称整体修复。完整 LBM 修复仍未完成。

## 2026-09-12 用户明确选择 load-ready 为默认：40 项完成，非 Stockfish 残差回退

见[默认配置与 10 负载 × C4/C8/C16/C32 验证](load-ready-default40-20260912.md)。
按用户后续明确要求，维护别名切换到 `gem5-v28_9-fs-load-ready.cfg`，普通 load 下界
3、response-to-ready 1；历史版本不改。本条替代下方“保留候选、不切换默认”的决定，
不改变此前组件证据，也不意味着独立 held-out、参数趋势或 quiet-host 吞吐准入已经通过。

40/40 完整运行及原生输入/守恒校验通过，600 条逐核 trace，0 项失败。
相同共同窗口下，对历史冻结维护默认的总体 MAPE 6.224% → 5.673%，CPI MAE
0.060409 → 0.056292；15 项改善、25 项回退。Stockfish 四项 MAPE
11.550% → 0.460%，CPI MAE 0.076914 → 0.003046；其余 36 项 MAPE
5.632% → 6.252%，CPI MAE 0.058575 → 0.062208。因此不能称为跨负载普遍改善。

本轮与历史基线的二进制不同，不包装为全 40 项同二进制 A/B；输入和已有有效参数
相同，新增参数均为 3/1。7 项既有微小宏指令分母差异保留，未强行对齐；并发吞吐只作
观测。下一步残差优先调查 NAMD、Omnet++、zstd，使用事件/阶段证据，不按 CPI 调整
负载补偿，不自动回退已经由用户选定的默认。

## 2026-09-12 Stockfish 普通 load 就绪修复：保留候选，不切换默认

见 [组件实现、完整阶段和跨负载回归](stockfish-load-ready-repair-20260912.md)。
普通 load 独立 producer-ready 下界 3，并在 sparse feedback 中落实 actual response
到消费者就绪的 1-cycle 边；保留 atomic/store 下界、same-PC 依赖和原两阶段 Q=1024。
不新增完整 UOP 扫描，不使用 PC/workload 特判或在线 gem5 timing。

共同终点 Stockfish C4/C8/C16/C32 的 MAPE 11.5497% → 0.4599%，CPI MAE
0.076914 → 0.003046。完整 18,451,982 条阶段记录配对：核心 3 两热点共 451,601
次 load→ALU，旧版全部 4 cycles，候选 451,600 次为 3，与 gem5 主体相同。

但五项跨负载对照 MAPE 7.1871% → 7.5609%，CPI MAE 0.067616 → 0.071026；
NAMD C4 相对误差 −10.4519% → −12.4339%，绝对 CPI 误差 0.065765 → 0.078236。
提供 `configs/gem5-exp-load-ready.cfg`，维护别名不变。独立 held-out、参数趋势及
quiet-host 吞吐未通过/未执行，不能默认推广，也不能为了对照 CPI 回退而否认已配对
的组件边或增加负载补偿。下一步应先定位 NAMD 反向残差。

## 2026-09-10 普通 store 接入与资源组回退：关键路径验收失败

见 [实施与完整 ROI 结果](critical-service-repair-20260910.md)。普通 store 按提交后
准入求共享服务，响应驱动 SQ/TSO；边界回退移除相应连接资源组并恢复受影响核心。
保留两阶段框架、Q=1024、并行反馈和原边界，没有补偿或 DRAM 参数调整。

TeaLeaf L1D64 C4 的共享服务覆盖从 351 增至 9,947，但原 20 个关键 DRAM 请求
**直接接入仍为 0**。完整 40,169,786 条硬件记录对齐后，load-head 缺口增加 8,828
cycles；CPI 误差 −17.8004% → −17.8458%，NUMA ABBA 吞吐下降 8.53%，scope PMU
完全不变。恢复范围达输入的 15.58%。整套测试及默认关闭完整 ROI 对照通过。

继续默认关闭 `core.response_shared_service_constraints`；不扩大验证矩阵，不以覆盖
计数或间接时间戳改变宣称主导误差修复。下一项须先支持核心中点恢复与跨 Q 请求保留，
使其他核心的早到请求能进入同一服务求解；仅扩充 eligibility 或放宽边界不能解决。
此结论更新下方“先缩小恢复范围并支持跨核”的计划：关键请求覆盖是首要验收条件。

## 2026-09-10 实际到达服务候选：小幅 CPI 收益，吞吐未通过

见 [实施与验证](shared-service-arrival-phase2-20260910.md)。按 CHA/channel/LLC set 识别
独立读资源，在原核心反馈中按实际请求重新求值完整服务日历，校验实际出口边界后
提交。失败核心工作块从原检查点恢复，不默认增加整批第二遍反馈；无 causal_read、
延迟补偿、参数调节、FST 重采或设备扩展。

最终 TeaLeaf L1D64 C4 误差 −17.8004% → −17.6786%；LLC32 C4 +10.2192% →
+9.8615%。固定 NUMA 串行 ABBA 吞吐分别下降 2.99% / 8.43%。L1D64 仅 351 个
共享请求提交了新服务，其中 213 个实际到达移动；两项回退重算范围占输入
3.56% / 7.23%。主要 miss/DRAM PMU 基本不变，仅有少量后续路径人口变化。

完整 40,169,786 条硬件记录对齐，generic 诊断与最终 materialized 候选 scope 和
服务计数精确一致。L1D64 load-head 等待增加 33,341，总周期增加 29,920；主要
load-head 缺口仍有 3,454,124 elapsed cycles。原 20 个关键 DRAM 见证的服务中位数
仍为 161 cycles，主要 owner 服务关系未覆盖。

`core.response_shared_service_constraints` 默认关闭，不推广为整体精度修复。
下一步需缩小恢复区段并支持跨核资源关系，不能放松边界或追加等待追求表面收益。

## 2026-09-10 同线读服务引用已接入受限组件，未获 CPI 收益

见 [实现与完整 ROI 验证](private-read-services-phase1-20260910.md)。原两阶段单次反馈
接入父服务引用、callback 边界和 request 容量；保留严格的组件、准入先后和跨批边界
检查。整套测试通过，generic/materialized 一致；无新完整 UOP 扫描或 causal_read。

补齐 incoming SQ release 上界的最终版，L1D64 C4 有 20,972 条 follower 实际使用
父响应，但 CPI 仍为 −17.8004%；LLC32 C4 为 +10.2192%，没有 follower 生效。两项
PMU 均不变。补界前候选的固定 NUMA ABBA 吞吐均值下降 0.78% / 1.16%；这不是
最终版的吞吐结论。最终完整 40,169,786 条硬件记录对齐确认 91,386 条 completion
和 7 条 retire 改变；后者只延后 1 cycle 并随后追平，各核最终周期不变。

保留代码和反例，`core.response_private_read_services` 默认关闭，不推广为精度修复。
下一步推进 corrected arrival、owner 共享服务及核心局部恢复；不放松证明条件或
追加等待追 CPI。12 次 FastSim（8 次初版 ABBA、2 次最终精度确认、2 次全 ROI
诊断），0 次新 gem5。

## 2026-09-10 两阶段服务修复设计：尚未实施

见 [关键路径审查与修复设计](two-stage-service-repair-design-20260910.md)。保留现有
producer、共享批处理与并行 materialized feedback；将稳定命中、在途事务完成引用、
共享资源约束分开表示。修复优先级为服务身份/可见性、到达与服务的局部闭合、替换
重复 DRAM 求解并补跨 Q 控制器状态。不是启用 causal_read 或重新打开旧 pending-fill。

新增审查发现：gem5 Sequencer 对同线 follower 也计 request 容量；现有 line ledger
不能直接用作请求容量。当前 sparse scoreboard 也不是已有增量求解器，局部核心恢复
仍需实现并测成本。修复必须同时覆盖实际 materialized 内核并报告激活、覆盖、回退和
重算工作量；没有新 CPI/吞吐收益结论。本轮仅更新设计文档，未改生产时序或重跑矩阵。

## 2026-09-10 TeaLeaf L1D64 C4：全流阶段比较完成

见 [完整 ROI 误差审计](tealeaf-full-roi-error-audit-20260910.md)。当前请求起点修复后的
模型仍为 −17.8004%；已匹配四核全部 40,169,786 条硬件测量记录，过滤 8 条无 O3
stage 的 syscall 辅助事实。隔离的精简阶段导出与当前生产 CPI/PMU、原密集审计
精确等价，不再用旧 checkpoint 内 head-gap 小计推算完整 CPI。

全量 issued-load-head 等待少 3,487,465 elapsed cycles；考虑完整 idle budget，
active 等待差为 3,080,087–3,487,465，占 4,370,942 总周期缺口的 70.47%–79.79%。
这是阶段账面差，不是干预收益。多个中后段累积欠估，不能只分析开头或一个窗口。
当前版本的独立 native follower 和同二进制 DRAM/refresh 见证仍显示服务过快。
下一步优先在原两阶段框架内处理同线响应可见性、corrected arrival 与共享服务状态
的一致性，再验证命令约束／refresh；不能直接叠加延迟或启用 causal_read。
本轮只改诊断副本和文档，生产 SHA 未变，0 次新 gem5、4 次必要 FastSim 诊断运行。

## 2026-09-09 请求起点正式修复与三个负载验证

见 [实现与必要验证](two-stage-request-origin-20260909.md)。原两阶段中合并绝对请求
下界；真实 sequencer/MSHR 阻塞投回 UOP 起点，逐片反馈和 store send 保持起点
一致。load/WB 继续启用，没有新增配置系数、热描述符、反馈轮次或 causal_read。
新增反例在旧模型失败；构建、完整测试、原 Graph500 指令及 generic/fast 对照通过。

Graph500 C8 误差 +20.9521%→−7.0640%，Stockfish C16 +17.3125%→+12.9798%；
TeaLeaf L1D64 C4 −17.3206%→−17.8004%，控制小幅恶化。Graph500 固定 NUMA
串行 ABBA 每版本两次，吞吐均值7.7916→8.1172 M user-UOP/s（+4.18%），本轮
未观察到吞吐下降，不外推为所有负载性能收益。三 case 共7次运行，无新采集／全矩阵。

此项是已实施的时间起点计算修复，整体 CPI/P99 仍未验收。下一步围绕功能排序代理
与硬件请求时刻的关系、服务路径有效性继续定位；不按 TeaLeaf 等负残差加回重复等待。

## 2026-09-09 Graph500 回归主因已定位：feedback 请求起点混用

见 [指令见证与定点消融](graph500-response-origin-regression-20260909.md)。依赖快照
精确等于旧版，服务校验快照精确等于当前；关闭 load 下界时误差为 −12.8424%，
关闭 WB 容量仍为 +20.9791%。主因是相对原始 UOP issue 计算的 extra，又加到
已被程序序 clamp 的请求起点上。真实用户态 L1 hit 见证重复加132-cycle排序等待。

临时副本只把普通数据事件的反馈位移改为 `max(0, absolute_ready-event_origin)`，
保留 load/WB，得到 −9.0910%，减少27,520,407 cycles，占原增量86.61%。这是
单 case 归因，不是生产修复／吞吐／新 P99 验收。生产源码及二进制未改。
下一步先统一原批次内的 issue-ready/request/response 起点及服务 latency 合同，
随后处理功能顺序代理是否应成为 timing 下界；不以关闭 load 下界、残差补偿或
切换 causal_read 制造收益。无需重复跑全矩阵来定位本次已确认的计算错误。

## 2026-09-09 当前两阶段组合未通过旧 P99 尾部验证

见 [七个尾部 case 的定点验证](two-stage-tail-validation-20260909.md)。同冻结配置和
输入，修复前快照精确复现原七个 CPI；当前组合 5 个改善、2 个显著恶化。DSE 四项
绝对误差改善 0.45–1.52 pp，TeaLeaf C16 改善 0.18 pp；Graph500 C8 从 −13.7367%
变为 +20.9521%，Stockfish C16 从 +12.8490% 变为 +17.3125%。已测两个正式尾部
使 formal40 P99 下界达到 19.5326%，高于此前 13.5930%；不需要再跑全矩阵证明该
组合尚未通过尾部精度验收。

追加三个快照隔离对照：Graph500 回归在最后一轮服务校验之前已出现；C16 服务校验
实际触发，TeaLeaf／Stockfish 的绝对误差分别额外扩大 0.0756／0.0023 pp，没有精度
收益。load 返回／WB 修复在全部七项生效，但不能将内部修复计数当作收益。

本轮仅验证，17 次模拟全成功，未改模型或参数、未重采、未重复构建／测试。暂停宣称
这组改动是通用 CPI/P99 改善；下一步先解释 Graph500 新增等待的时基／服务路径及
Stockfish 的正误差，再决定具体机制修复。不按残差补偿，不因反例局部闭合就跳过尾部
验收。两路并行运行只用于精度，未新增吞吐结论。

## 2026-09-09 两阶段服务复用的填充身份与可见性修复

见 [实施与验证](two-stage-service-validity-20260909.md)。LLC fill 使用稳定 generation，
timing-only replay 在修改队列前检查父事务及 fill 边界；FR-FCFS 的同代 follower 更新
返回元数据，失效候选回退。原可选 functional reweave 不再只凭请求顺序不变认证成功。
验证覆盖跨 Q 的 fill 关联和候选回退；回调同时匹配新时间与代次，没有开启额外生产开关或完整反馈轮次。

构建和整套测试通过。TeaLeaf C4 前后 CPI、PMU、反馈次数完全相同：CPI 误差仍为
−5.4176%，固定 NUMA 的一组吞吐对照 7.0750→7.0328 M user-UOP/s（−0.60%）。
完整依赖短输入 generic/fast 均 38,157 cycles。C4 维护配置绕过这些重算分支，
`service_fill_checks=0`；这是复用条件修复与兼容性验证，不是 CPI 精度收益。

普通 feedback 的 arrival 移动仍使用原近似；跨 fill 的真实 hit/merge 路径切换尚未
实现。下一步需在功能提交前恢复受影响 cache set 并有界重算，不能将此次回退算成
修复该次误差，也不能开启更大窗口／全量重跑或补偿参数来制造提升。

## 2026-09-09 原两阶段 load 返回与写回约束修复

见 [实施与必要验证](two-stage-response-completion-20260909.md)。普通 load 的完成现在
等待当前反馈中所有 data fragments 的 response，再竞争批次内 WB 容量；跨 checkpoint
占用从既有 sparse ROB 恢复，activity/transfer 同步校验。保留热描述符和原并行反馈，
没有增加每批完整遍历，也没有开启可选完整 FU/port 重算。

构建、整套测试与 TeaLeaf 对照通过。长 C4 输入 CPI 误差 −6.1028%→−5.4176%，
PMU 基本不变；最终吞吐 6.7701→6.4896 M 用户 UOP/s（−4.14%），存在明确开销。
完整依赖短窗口 38,131→38,157 cycles，没有改善；generic/fast 结果一致。
不将一项负载的收益外推为全矩阵准入，也不把局部等待计数相加成 CPI 收益。

仍未解决移动 arrival 后原服务路径是否有效、提前 hit/merge 与 clamp 自身的额外等待。
下一项应围绕请求身份及 fill/permission/resource 边界做 descriptor 失效和有界局部修正，
不能以本次 response 下界已闭合为由宣称全生命周期正确。

## 2026-09-09 完整动态依赖接入原两阶段框架

见 [实施与验证](two-stage-dependencies-20260909.md)。第一项已完成：base schedule、
generic/materialized feedback、跨 checkpoint ROB 环及 activity/transfer 判定消费
完整 RAW，使用 chunk 稀疏扩展；四 RAW 加独立 StoreSet 的热描述符和索引不增大。
完整动态元数据优先于静态操作数补全。原并行路径与维护配置保留，不增加反馈轮次。

构建及整套测试通过。仅使用现有 TeaLeaf：旧 C4 长输入 ABBA 的目标结果一致，
用户 UOP 吞吐 6.7108→6.7189 M/s（+0.12%，不宣称加速）；同窗口旧／完整依赖
及通用／快速内核均为 38,131 cycles，cycles/user-UOP=0.953275，scope PMU 一致。
测量段确实补回 1,812 条 RAW，但该窗口 CPI 没有改善。此项证明通用依赖语义与旧路径
兼容，不是完整精度验收。下一项在原反馈遍历内修复请求／返回和 UOP 完成时基。

## 2026-09-09 用户重申吞吐目标：回到原两阶段框架修复

用户明确指出，采用原两阶段推理框架的目的就是提高吞吐；不接受以扩展细粒度
`causal_read` 替代高吞吐主路径。此次方向纠正优先于下方历史记录的“继续扩展新求解器”
计划。`causal_read` 仅保留为小规模机制差分参照，未完整对齐 gem5，不作为精度答案。

见 [原两阶段修复审查](two-stage-repair-review-20260909.md)。本轮核对当前源码和已有
证据，没有修改模拟器、配置或运行新实验。第一项应把完整动态依赖接入原 base/feedback/
fast kernel/certificate，保持四槽热记录和独立 StoreSet 边，使用稀疏扩展。第二项在
原反馈遍历内统一同请求 response/完成时基；cache 可见性、FU 空档、store 准入需要
局部日历/紧凑事务，不得默认增加全量 proposal 或切到全局逐 UOP 事件引擎。

区分原型自身修复与旧引擎缺口：原型 pre-L1 固定 26 拍没有可直接移植的旧路径收益；
旧引擎已有 syscall/kernel/域外策略。E 授权能力已存在，但与默认关闭的 line-coalescing
开关耦合，可独立审查解耦。历史 fill-only/load-admission 的吞吐与尾差负结果仍有效，
不因本次方向调整而重新默认开启。候选必须同时验收机制、CPI/PMU 与同口径吞吐。

## 2026-09-09 Cache 请求/返回与权限机制修复通过

见 [修复与验证](causal-cache-permission-repair-20260909.md)。`causal_read` 已拆开
hit response、miss request、fill response；下级本地 fill 与上级到达/释放不再同拍。
原 pre-L1 固定 26 拍等待已去除，保守 line lease 只负责排序；真正 GETS/GETX 由
私有 miss/upgrade 发起。fill 传递权限，无共享者读回 E，E→M store 本地完成，
共享升级等待 peer 失效并复用已有数据。脏 owner 的本地返回交错也保留到真实 snoop。

构建/整套测试通过，仅新增一次原前缀和一次同 TeaLeaf ROI：136 对 L2/L1 fill 全部
相隔两拍，原先多等 26 拍的 17 次 resident store 均本地完成，独立队列积分/RAW/TSO/
回调守恒通过。2,939,373 UOP、83 条扩展依赖保持；DRAM 参数与上一轮诊断相同，
ROI cycles/user-UOP 从 1.08195 变为 0.859925，全程 ROB 阻塞周期 789,310→557,225。
各级 generations 不变。reference 诊断值 0.89255，数值接近不替代正式精度门禁。

该状态替代下节“尚未修改求解器”。维护默认未切换，未重采集或扩展设备。剩余包括
测量时钟窗、理想 I-side/翻译、FCFS/refresh/write/tick 相位，以及保守的 peer 路径
和共享资源容量；下一步核验控制器队列/选择事件与测量窗，不按 CPI 残差调参。

## 2026-09-09 下级内存事件已定位，DRAM 参数一次对照完成

见 [事件边与协议审计](causal-memory-edge-audit-20260909.md)。`causal_read` 已确认：
冷 miss 在 L1 查询前多走固定 26 拍 grant 等待；hit response 与 miss 下发共用一个
延迟字段，136 对私有 L2/L1 fill 同拍、缺少目标返回两拍；无共享者读 fill 不记录 E，
前缀 17 次 resident store 仍走 26 拍权限等待。当前待修的是具体事件与状态转换。

本次采集实际使用 SimpleNetwork，无竞争直接消息路径为 4 拍；directory 的 6 拍
属于 owner invalidate，不属于冷 fetch；LLC hit response 为 2 拍，而 memory fill
response 为 1 拍。旧 interval/Garnet envelope 不能直接搬入新事件模型。

一次同二进制/同完整 TeaLeaf 输入的诊断，只按采集配置映射已支持 DRAM 参数。
2,939,373 UOP 的功能、回调及各级 miss 数保持一致，全程 ROB dispatch 阻塞周期由
364,927 增至 789,310，ROI cycles/user-UOP 由 0.67765 变为 1.08195。它证明参数会
显著改变等待和反压，不构成 CPI 精度改善；跨模拟器时钟窗及其他模型缺口仍在。
参数只在 tmp 诊断配置中，未修改 C++ 或维护默认，未重复构建/测试/采集。

下一步优先拆分请求/返回事件、让目录等待归属实际 miss/upgrade 事务，并随 fill 传递
共享/独占权限。不能通过减固定 26 拍、增加 hit_latency 或反调 DRAM 来抵消错误。

## 2026-09-09 Load 执行边修复，真实 TeaLeaf ROI 采样接入

见 [load 阶段与 ROI 验证](causal-load-stages-roi-20260909.md)。`causal_read` 的 load
原先跳过 memory FU 的一拍执行阶段，现与 store 一样在执行后才做地址/SQ/cache
准入；实验配置 L1D 延迟按目标 mandatory-queue 到 callback 的一拍定义。源码和已有
2,168 条带 native 时刻的 TeaLeaf load 证据一致，不使用 reference tick 求解或补偿 CPI。

新增 `completion_ready` 观察事件区分数据齐备和实际 writeback；带宽仍在 writeback
时消耗，RAW 唤醒和 IQ 释放不提前。一次构建/整套测试、一次原前缀独立事件审计通过。
前缀 1,124 条 load 的 FU 边全部一拍，958 次 L1 命中服务一拍，11 条 load 另等一拍 WB。

仅增加一次此前采集的真实 ROI 运行：2,897,470 条预热 UOP 连续接入 40,000 条用户及
1,903 条内核 ROI UOP，共 2,939,373 条全部完成，83 条扩展边保留。输入指纹、精确
功能边界、人口/回调/fill 守恒通过。该结果替代“只跑通 14,560 UOP 前缀”的接入状态，
但不替代正式精度门禁：其他 cache/DRAM 服务参数和时钟窗尚未完全对齐，没有修复前后
同一 ROI 的准确率结论。下一步先逐级对齐服务参数与事件边界，不能按残差调参。

## 2026-09-09 同一前缀通过：域外策略接入与 O3 阶段映射

见 [本轮接入与配对门禁](causal-core-memory-prefix-gate-20260909.md)。既有
`allow_mmio_escape` 已接入 `causal_read` 的原 UOP/RAW/TSO/LSQ 生命周期，本地回调
不进入 cache/coherence/DRAM；没有新增设备、中断生成器或服务补偿。构建、整套测试和
同一 TeaLeaf 14,560 UOP 的独立事件守恒通过，27 条扩展边与 13 次域外访问全保留。
此结果替代下节“域外策略尚未接入、前缀未跑通”的当前状态。

已有 gem5 JSONL 与此前缀功能身份全部匹配，无需重采集。按源码修正实验配置的
dispatch 同拍发射、完成到退休两拍，并对齐目标宽度/几何；最初非访存非控制片段的
7 条计算指令 issue/完成/retire 全部匹配，完整前缀的事件守恒再次通过。
参考 cache/controller/DRAM 服务仍未完整对齐；两个前缀运行不是 CPI 精度验收。

实际 ROI 位于整个此前缀之后，工具明确拒绝把测试切点和正式 CPI 配对。
另确认 gem5 `ready_tick` 是 `fetch+completeTick`，不是操作数 ready；当前 LSQ 写回
不更新该字段，不能将 load 的旧标签等同真实 data response。下一步先闭合普通 RAM
load 执行/准入/数据返回/writeback 的时间合同，复用现有 LSQ/Sequencer 观察点；不以
错误标签或 CPI 残差替代因果证据。维护默认不切换，未证明正式 CPI 改善。

## 2026-09-09 完整动态依赖：TeaLeaf 重采集完成，既有域外访存策略尚未接入事件模型

见 [FST 完整依赖记录](fst-complete-dependencies-20260909.md)。FST 保持 64-byte 主记录，
producer 去重后前四条内联，其余进入稀疏 `.deps`；已接入采集、读写/切片/预热与
`causal_read` 的实际 writeback 唤醒。8/16/20 fan-in 的机制检查和整套测试通过。

仅重采集 TeaLeaf L1D64 C4，保留功能预热、每核 10k 用户记录，并与原采集器做一次
相同 checkpoint 对照。2,939,373 UOP 的主 FST 为 188,120,160 bytes，附件只增加
1,852 bytes（0.000984477%）。core2 的 12,192 条记录补回 12,273 条不同 RAW 边，
其中 83 条需要第五条边；说明重复 producer 也会挤掉原四槽中的有效依赖。
功能字段、旧附件、CPI/native 汇总和 16,489 项非宿主 gem5 统计保持一致。

固定各核前 2,000 宏指令的 14,560 条主记录与新采集逐字节一致，27 条扩展读取通过。
此前的缺失 operand-map 依赖门禁已解除；事件模型现在拒绝 core2 ordinal 579 的
Local APIC EOI store（物理地址 `0x20000000000020b0`）。尚未产出该前缀 CPI，
不宣称 CPI 改善，不扩大验证。此处此前直接建议新增 MMIO/中断状态模型，判断过度，
现撤回该后续计划：该访问在原采集与新采集中都存在，位于 core2 正式测量边界
（ordinal 229,364）之前。旧 native-FS 配置已经启用 `trace.allow_mmio_escape=true`，
保留域外访存 UOP 的 core/DTLB 时序，将其计入 `mmio_escape_accesses` 并绕过
cache/coherence/DRAM；新事件模型却要求关闭这一策略，并拒绝所有域外地址。
当前是已有抽象边界的接入缺口，不能据此要求增加 APIC 设备仿真，也没有证据将其列为
TeaLeaf CPI 误差根因。后续先对齐既有输入、预热/ROI 和域外访存策略，在新事件生命周期
中保留 UOP/依赖及资源释放，明确已有时序近似；不模拟设备内部或重新生成中断。
生产默认保持原状。

## 2026-09-09 后续接入：多核共享事件与连续预热

见 [第三阶段记录](causal-core-memory-phase3-20260909.md)。`causal_read` 已接入同一全局
事件队列中的多核共享 LLC/DRAM、保守一致性、连续预热边界，以及普通内核/非访存
序列化；单核限制已解除。新增机制检查与一个四核合成 CLI 验证接口和状态守恒。

真实四核 TeaLeaf 固定前缀的记录保持不变，但 core2 缺少两处 PC 的 operand map，
影响 67 条记录，其中 5 条的源寄存器计数超过四槽，存在截断风险，因此拒绝产出 CPI。
后续核对明确：源寄存器数不等于不同 producer 数，不能据此断言已经丢边。下一步补齐可证明的功能
操作数或完整动态依赖，再重跑同一前缀；不跳过记录、猜依赖或通过核间 PC 拼表宣称
完整覆盖。此结果没有证明完整 CPI 改善，维护默认继续关闭，验证不扩展全矩阵。

## 2026-09-09 后续实施：混合指令接入，验证限制为必要检查

见 [混合指令阶段](causal-core-memory-phase2-20260909.md)。`causal_read` 已扩展 FP/SIMD、
普通 store/SQ/转发/脏行写回、依赖驱动分支恢复，并以完整功能 operand map 保守补全
四槽 trace 截断的依赖。真实前缀暴露的 predictor 多 checkpoint 提交接口已修复。

前 10,000 条 TeaLeaf 宏指令（17,503 UOP）保持原始记录，已在单核冷启动实验配置跑通；
逐事件队列积分、RAW、TSO 和恢复边检查通过。旧路径同输入 core/thread 结果不变。
按用户要求，仅执行新增机制检查、必要的整套测试程序和这一真实前缀，不重复八场景 CLI、
完整矩阵或 sanitizer。该结果不代表完整 native-FS CPI 改善；多核、I-side/翻译、内核/
序列化、预热边界及 cache 写回反压仍未完成，默认推广继续关闭。

## 2026-09-09 实施：A 阶段进入 Simulator，真实负载推广仍关闭

详见 [core/memory 第一阶段实现](causal-core-memory-phase1-20260909.md)。新增显式
`core.model=causal_read`：实际就绪发射、分层 miss generation、唯一数据返回、消费者
唤醒和资源释放由同一事件循环决定，整次运行替换旧求解路径。使用功能输入和硬件
配置，无 case/PC/误差补偿。维护 native-FS 配置未切换。

当前覆盖单核普通整数 ALU/load、物理 RAM、非一致性层次和现有 FCFS DRAM；不支持的
store/branch/kernel、预热边界和多核语义明确失败，不输出混合候选 CPI。真实 TeaLeaf
接入须先完成 B 阶段，不拿当前模式运行删减输入后的 CPI 宣称泛化改善。

通用机制、独立逐周期 DAG oracle、队列/返回守恒和宿主切批/身份改名检查已进入
`fastsim_tests`。原 8 组旧模式诊断的 scope/core/thread 结果保持相等。此证据支持
A 的事件合同，不代表 gem5 完整时序、独立 workload 精度或生产吞吐通过。

## 2026-09-09：下一实现边界是可运行的 core/memory 因果闭环

详见 [下一步修复方案](causal-core-memory-repair-plan-20260909.md)。本轮是实施设计，
没有修改生产模型、默认配置或新增 CPI 实验。

实施顺序收敛为：Simulator 入口的受控 ALU/load 事件闭环 → 普通 store/TSO、取指、
共享状态与跨批连续性 → 固定服务参数的真实配对窗口 → 独立 DRAM 机制 → 未参与选型的
验证集及旧矩阵/吞吐门禁。ready、admission、服务选择与 response 共同决定时序；旧的
base issue 不能固化为下界，旧 shared replay 不能先改状态再接 coordinator。

本节替代下文“只在 TeaLeaf clean load 组件首接入”和“下一代码仅做 shared proposal”
的实施范围。load-only 是受控开发阶段；真实负载必须覆盖其干扰事件域。第一版在语义
副作用前按整次运行选择路径，不做未经证明的单请求或任意批次混用。现有 clean 人口
含 native 答案筛选，不能作为生产资格或独立精度证明。

历史反例和停止条件继续有效。第一项交付必须展示新路径在实际 Simulator 中运行并
替换旧求解调用；新增未接入模块、只修 response 下界或净 follower 数不算完成。
已用于诊断的 formal40/DSE54 仅是回归集合；新的独立 workload/window 尚待冻结。

## 用户明确要求：通用机制修复与独立泛化验证（2026-09-08）

用户要求的是通用机制修复，不接受直接补偿或对 TeaLeaf 过拟合。下文按 TeaLeaf、
ASTCENC 等命名的研究窗口只用于离线证据与回归；任何“首个接入目标”都不得解释为
运行时按 workload、某个 case 的误差符号或特定 PC 白名单选择模型路径。

后续候选必须满足：

1. **输入和参数。** 运行只消费功能 trace、硬件配置及模型自身状态。gem5 的
   issue/admission/response tick、path、stall 标签只用于离线验证；不得作为推理
   输入、延迟查表或 case 专用修正系数。不得用误差大小调整 Q 或硬件延迟。
2. **真正替换事件所有权。** 普通 load 的 operand-ready/issue、准入、唯一共享请求、
   callback/data-ready、消费者和资源释放必须属于同一条事件链。已有 canonical
   cache/shared 更新不能先执行后再叠加等待。issue 变化影响请求交错和可见状态时，
   必须重算相应服务选择；旧预测时间不能永久作为阻止合法提前的下界。
3. **状态在正确阶段生效。** lookup/replacement、容量 reservation、fill 可见性、
   coherence 和 DRAM 命令各按所属阶段推进，不能把所有状态一律延迟到最终 callback。
   同线 follower 不重复分配服务；同一 response 只消费一次；普通 store 的 commit、
   TSO send、callback 和 SQ release 独立且符合目标 ISA/微架构约束。
4. **机制门禁。** 先通过与 workload 无关的差分场景：并发/串行同线访问、独立请求、
   callback 与 admission 同 tick、容量满/释放、替换/失效、跨 checkpoint、依赖链、
   store 顺序。任何部分支持均按可证明的事件/资源语义定义，不按 CPI 是否改善定义。
5. **独立泛化。** 在选型前冻结未参与机制设计的 workload 和窗口，保持参数固定，
   分别验证 CPI、事件/状态分布及微架构变化方向。formal40/DSE54 的旧 case 因已用于
   诊断只能作为回归矩阵；仅换核心数不能宣称 workload-held-out。
6. **成本和推广。** 使用有界活动事件状态并替换旧求解工作，不在旧时序上再叠加一遍
   全 UOP 补偿遍历。语义正确性与整套模型推广分开：CPI 暂时退化不能单独否定一条
   已证的物理规则，但未过独立准确率/趋势/吞吐门禁的组合不能进入维护默认。

当前具体接入缺口仍是 core admission/response 消费与 SharedSystem 状态推进之间的
一致性。已有 ledger、coordinator、private/LLC prepare API 是构件，不是完成修复的
证据。后续实现必须展示它们在实际事件链中的调用、旧路径被替换的范围，以及独立
泛化结果；不能以新增未接入组件或 TeaLeaf 单例 CPI 改善宣布任务完成。

## 判定原则：组件语义优先于聚合 CPI

单个候选引起的 CPI 误差上升或下降，不能单独证明该组件错误或正确。FastSim 的前端、
队列、内存顺序、cache/DRAM 服务和 retire 前沿会相互改变请求交错；聚合误差还可能包含
多个方向相反的偏差。候选按以下顺序判定：

1. 先校验请求身份、事件顺序、唯一 owner、容量占用区间、状态可见性和守恒关系；违反
   已知 gem5/ISA 合同的候选可在这一层否决。
2. 再比较同一事件的 proposal、admission、service、callback、resource release 和关键
   consumer，确认候选修复了目标边，并重算了受影响的后继状态。
3. 然后检查路径条件下的 latency/queue-depth/PMU 分布以及正、负误差控制，识别候选和
   其他组件的非加性交互。
4. 最后才用 held-out CPI/P99、参数趋势和吞吐判断整套模型能否推广。聚合 CPI 可阻止
   不完整组合进入默认路径，但不能作为组件归因的唯一依据。

组件优先级也不能由一条因果见证决定。单条见证用于证明机制存在、校验账本字段和构造
回归测试；进入模型实现前，必须在多个 workload、正负 signed residual、不同 core/cache
配置中统计同一类分歧的覆盖率、方向和 retire 暴露。下一轮先完成这一分层组件矩阵，再
决定 store、load/cache、frontend 或 dependency/queue 中谁先进入原子替换。

## 2026-09-08：最终 issue proposal 可用，但双向身份交换阻止直接接入

详见 [line-generation 准入影子账本](line-generation-admission-shadow-20260908.md)。

**决定：保留默认关闭的 admission shadow，不把 follower 净差或 CPI 当作接入依据。先
闭合 parent→child issue spacing 与 parent callback lifetime 两条轴，再接 coordinator。**

- 全窗口最终 issue proposal 相比旧单调 memory lower bound 消除了大量假 follower，但
  对 gem5 native 总数仍低 4.38%–55.26%；四 case 的 Sequencer 容量拒绝均为 0。
- TeaLeaf L1D64 密集配对只有 149 条 follower 身份相同，同时有 191 条 FastSim-only 和
  272 条 gem5-only。净缺 81 条掩盖了 463 条双向错分。
- Stockfish 两端 follower 都为 11 条，逐事件只有 6 条相同，证明总量完全相同也不代表
  generation owner 正确。
- TeaLeaf L1D64 可解释的 gem5-only 中，165 条由 issue 间隔轴单独改变，92 条由 parent
  生命周期轴单独改变，13 条任一轴都足够；反向 FastSim-only 的 191 条中 190 条由 issue
  间隔轴改变。不能只延长 response，也不能只移动 issue。
- 双向错分主要经过 register-producer gate，但同线 generation parent 不是功能 producer
  或 winning issue owner；依赖链改变相对到达时间，不拥有 cache generation。

private/LLC cache 已增加无副作用 prepare 和 per-set generation guarded commit，并覆盖
事务回滚。SharedSystem directory/CHA/DRAM 尚无等价延迟提交，唯一 response consumption
也未接入；因此生产模型、默认开关和 CPI 均不变。下一取证先比较双向错分的 register/
dispatch/issue-width owner，下一代码边界是 shared service proposal/atomic commit。

## 2026-09-08：native 组件闭包审计通过，首个接入目标收紧到 TeaLeaf L1D64

详见 [line-generation 组件闭包审计](line-generation-component-audit-20260908.md)。

**决定：先在 TeaLeaf L1D64 的严格 clean 普通-load 组件接入 coordinator。TeaLeaf LLC32、
ASTCENC 和 Stockfish 分别继续作为 store/SQ、DRAM 服务、frontend/kernel 控制，不因其
clean 人口或 CPI 符号而合并归因。**

- 四个 v7 100K 诊断窗共回连 890,517 个带完整 admission/response 的数据事件；物理记录
  零缺失，逐事件主原因计数守恒。
- 最终 clean 人口的 inferred follower 与 gem5 native coalesced 逐事件完全相等：TeaLeaf
  LLC32/L1D64、ASTCENC、Stockfish 分别 2,604/19,642/17,986/19,467。
- clean 事件覆盖率分别为 20.18%/78.12%/67.44%/60.01%。TeaLeaf LLC32 的 store 与
  私有 set 耦合最重，不适合作为第一接入目标。
- clean 外仍有 5/193/121 条 follower-oracle 分歧（TeaLeaf L1D64/ASTCENC/Stockfish），
  指向未观测 speculative、跨窗 parent 或请求类型状态；这些组件已 fail closed。

该审计不包含 IFetch 请求和观测窗前 active generation，因此是接入上界，不是生产
certificate 或 CPI 收益预测。下一代码门禁仍是无副作用 admission proposal、唯一
response consumption 和 shared hierarchy 延迟事务；完成后只开默认关闭的实验路径。

## 2026-09-08：v7 分段闭合，load-only 有界事件链进入接入准备

详见[跨负载组件矩阵第一阶段](cross-workload-component-matrix-phase1-20260908.md)。

**决定：第一个模型候选只处理普通 data load 的同线 leader/follower，并以默认关闭的有界
generation ledger 实施。DRAM 动态服务、store post-commit 生命周期和 Stockfish 前端/
内核 serialization 分别建模，不合并成一个 CPI 修正。**

- TeaLeaf L1D64 的 421 条 local↔coalesced load 中，服务缺口均值 −109.1 cycles，tail
  缺口 −104.3，非服务余项仅 +4.8，支持 generation/visibility 为独立机制。
- 同一类事件在 TeaLeaf LLC32、ASTCENC、Stockfish 的 tail 分别为 +49.5、+44.9、
  +125.2 cycles；所以局部 response 修复不能按总 CPI 符号启用，也不能预估为等量收益。
- TeaLeaf L1D64 的 81 条严格匹配 DRAM load 服务缺口为 −169.1 cycles；另外三个窗口的
  匹配 DRAM 分布方向不同，ASTCENC 还有 3 条约 1000-cycle gem5 长尾。禁止全局添加
  DRAM 常数。
- store 的 Ruby response 全部位于 commit 后，必须保留独立 commit→admission→callback/
  SQ-release 生命周期。四窗口没有 atomic 样本，首轮不推断 atomic 规则。
- 现有 `ruby.sequencer_line_coalescing` 会多做 response feedback 且混合 load/store；四个
  密集窗口 cycles/user-UOP 增加 2.4%–34.7%，停止复用该路径作为候选。

首个实现的语义门禁是：每个 generation 只有一个 hierarchy leader；同线 load follower
不新增 line/DRAM request，并共用 leader response；`admission == callback` 必须开启新
generation；容量满时只在请求实际到达后等待最早 callback，不能让未来 store 预占槽位；
跨 checkpoint 只保存有界活跃状态。ledger、private-cache probe/fill、coordinator 和
component preflight 已完成；下一步按下述边界拆分 core/shared 状态后，再原子接入
completion 与 dependent-ready。聚合 CPI 仍是最后推广门禁。

**实施状态：** 有界 `LineGenerationLedger`、可事务回滚的 private-cache
probe/completion-fill API、`LineGenerationCoordinator` 和 `LoadComponentPreflight` 已实现
并通过定向测试。coordinator 保证 callback/admission 顺序、唯一 service owner、follower
共享 response 和容量守恒；preflight 在任何状态变更前按私有 set、全局资源域、同线及
active generation 做传递闭包并 fail closed。真实 private-cache harness 已证明 callback
前不可见、leader/follower demand 计数和联合回滚重放一致。

这些组件尚未接入 `Simulator`，没有新增配置入口，也没有产生 CPI 变化。下一代码门禁是
从 `compute_core_timing_feedback_impl()` 提取无副作用的 admission proposal 和单次
response consumption，并为 shared cache/directory/DRAM 提供 callback 时提交且可事务
恢复的状态。两者完成后才接默认关闭的 load-only 实验路径。禁止把 ledger 追加到已经
完成旧 cache/shared 更新的 response 修补点，也禁止复用现有额外全 UOP feedback replay。

## 2026-09-08：跨负载组件矩阵完成，优先补原生准入/响应时间账本（已由上节更新）

详见[跨负载组件矩阵第一阶段](cross-workload-component-matrix-phase1-20260908.md)。

**决定：不再以 TeaLeaf 单例或 gate 人口选择全局候选。先用 native-response-v7 重采四个
密集窗口；时间证据闭合后，优先实现 pending line leader/follower 的统一 generation 和
response 可见性。Stockfish 前端/内核 serialization 作为独立组件并行取证。**

- 41,839 条跨负载配对显示：相反误差符号的两套 TeaLeaf 配置具有近似的
  `none/register/dispatch` gate 人口；StoreSet 仅在 Stockfish 达 2.33%。因此 FastSim
  内部 gate 频率不能直接解释参考误差。
- TeaLeaf L1D64 密集窗有 421 条严格单请求语义不匹配：FastSim local private hit、gem5
  Sequencer coalesced，尾部 residual 均值 −104.30 cycles。这是当前最强的跨事件
  visibility/merge 候选，但仍要用 response tick 证明等待区间。
- ASTCENC 的代表 load 在双方都走 DRAM 时仍为 FastSim 247、gem5 1005 cycles；匹配
  DRAM 人口和 TeaLeaf L1D64 的匹配 DRAM 人口都存在负尾部，说明仅修 path class 不足。
- TeaLeaf LLC32 的重复 store 存在 SQ admission 过晚；Stockfish 所选负向阶段存在
  instruction-fetch/serialize 欠估。两者作为独立控制，不并入 memory response 常数。
- 已实现有界 issue owner 审计和外部
  [`native-response-v7` 补丁](../patches/p7-external-native-timing-ledger.patch)。v7 记录
  first/last successful admission 和 last response tick；FastSim 配对工具兼容 v6/v7。
  旧冻结输入是 v6；上节已用显式诊断二进制完成 v7 密集窗口重采。生产模型和默认开关
  仍未改变，也尚未宣称 CPI 改善。

**进入原子模型替换的条件：** 在 TeaLeaf L1D64、ASTCENC、TeaLeaf LLC32 和 Stockfish
各自窗口中闭合 issue→admission→response→commit，确认 line generation、唯一 owner、
split/alias 合并和 response release 的守恒；然后才实现模型候选并跑聚合 CPI/吞吐门禁。

## 2026-09-08：首个边界状态回归链闭合到 DRAM 时长→TSO/SQ release

详见 [成对 frontier 首差异与 store 生命周期定位](paired-frontier-divergence-phase1-20260908.md)。

**决定：保留无扰动的有界 SQ owner 审计、成对分析器和已证实的边界状态语义；当前
store 候选因生命周期合同不完整而不进入默认路径。**

core3:291860→292582 是 store/SQ 耦合存在的见证，不代表它是所有负载的主导误差。
在跨负载组件矩阵完成前，不直接把该见证扩写为生产 store 生命周期候选。

- 首个语义分叉严格位于 core2:223952 的 460-cycle DRAM→2-cycle L1；首个跨核分叉
  core1:276057 是 DRAM blocker 改变带来的 96-cycle 提前，不是回归源。
- 首次提前→落后位于 core3:292582：直接 owner store 291860 的服务时长增加 88，抵消
  已有 52-cycle 提前后使 SQ release 晚 36；291861 串行后扩大到 124。
- post-commit store 能把边界状态差值 +55,896 变为 −94,085 cycles，证明生命周期混用
  是传播通道；baseline 正误差从 +9.6487% 变为 +12.5616%，只说明它同时扰动了其他
  补偿项，不能据此判定“请求应在 commit 前还是后”的组件语义。
- 允许多条 post-commit store 不等 callback 会把 baseline 变成 −30.3940% 低估，失败
  实现已经撤回；否决依据是它违反当前 gem5/x86 的 single-store-in-flight 约束并制造
  不存在的并行度，CPI 数值只是这一语义错误的系统表现。下一候选必须同时拥有 commit、
  TSO admission、唯一 shared request、callback/SQ release 四个事件。

## 2026-09-08：边界状态跨负载 pilot 未通过推广门禁

详见 [测量边界内存状态跨负载试验](measurement-boundary-memory-state-multiload-20260908.md)。
用户要求的并行扩展已完成两个其他 workload 和 TeaLeaf L1D64 控制；Graph500 因冻结
gem5 二进制缺失而失败关闭。

**决定：继续保留显式 opt-in，维护 manifest 不启用，不再扩大矩阵。**

- Stockfish baseline C4 的绝对误差改善 0.1467 pp；ASTCENC baseline C4 和 TeaLeaf
  L1D64 C4 分别退化 0.0196/0.0224 pp。三组都减少 miss/DRAM read，但 CPI 都下降，
  所以只对原本正误差的 Stockfish 有利。
- 两个其他 workload 的小样本 MAPE 改善 0.0635 pp，但方向为 1 改善、1 退化；加上
  L1D64 后仍只有 1/3 case 改善。再纳入已完成的 LLC32 正误差控制，四 case MAPE
  退化 0.0209 pp。
- 三个有效 case 的采集 binary/config/checkpoint provenance 通过，默认/状态人口相同；
  顺序重复的总周期、逐核周期和 PMU 逐项复现。结果不是并行宿主调度噪声。
- Graph500 的冻结 SHA 为 `8ff9e8d3...`，当前路径二进制为 `33555384...`。诊断重放在
  WORKBEGIN 前已经产生 32.36 GB prefix，随即终止并清理；没有 state 或 CPI 结果，
  不进入聚合。找回精确二进制或完整重冻 reference/FST/checkpoint 前不要重试绕过 guard。

**下一步：** 维持下节确定的 shared-order/response 取证顺序。边界状态只作为 owner
账本的功能入口状态；不要按 workload 或当前误差符号启用，也不要用本次小样本平均值
替代正负控制和未参与选择窗口。

## 2026-09-08：边界状态机制保留为 opt-in，继续追 shared-order 首个分歧

详见 [测量边界内存状态第一阶段](measurement-boundary-memory-state-phase1-20260908.md)。

**决定：保留显式 `fastsim-binary-warmup-state-slice`、状态净化工具、审计和测试；维护
生产 manifest 不启用。** 后续按用户要求完成的有限跨负载 pilot 见上节；推广决定未变。

- LLC32 core 2 `223952` 的 gem5 路径已闭合：较早的 committed kernel load 在全局
  WORKBEGIN 后、该核第一条 measurement record 前经 DRAM 装入目标行；目标请求随后
  是真实 `ReadReq`、L0D `E->E`、1-cycle Sequencer response。现有 FST 过滤了前一条
  load，所以 FastSim 的 460-cycle DRAM 是缺失入口 cache 状态导致的虚假路径。
- 新 sidecar 只允许连续顺序、物理地址、大小和 R/W；拒绝额外列，最多每核 1,048,576
  次访问。回放不推进 target time、不添加退休 UOP/PMU/queue/DRAM 事件。默认输入行为
  不变，四份空 sidecar 的 target state 与 baseline 相同。
- 一次 committed `mem_events` 补采净化出 core 2 的 693 次可缓存边界访问，另排除
  6 次 MMIO；其他核为 0。
  完整回放把目标从 460-cycle DRAM 修为 2-cycle L1，retire 提前 456 cycles，审计窗口
  末端提前 1,629 cycles。
- 完整 ROI 精度仍失败：cycles/user-UOP 0.8144051186 → 0.8158025184，signed error
  +9.6487% → +9.8368%，退化 **0.1881 pp**；DRAM read 减少 9 次，但 sum core
  cycles 增加 55,896。局部必要修复再次打破了其他 shared/DRAM 误差补偿。

**下一步：** 以修复后的入口状态为账本起点，定位目标请求移除后新增 55,896 cycles
的首个 shared-order/response 分歧，再决定怎样在 owner 内原子替换 memory ordering、
admission、visibility 和 response。禁止继续按地址补命中、按误差符号启用 sidecar，或
把 456 个局部周期当作 CPI 收益。新候选仍须同时通过 LLC32 正控制、L1D64 负尾部和
未参与选择的窗口；在线状态必须有界且不能新增全流 feedback traversal。

## 2026-09-08：先做 owner 级成对账本，不再预设回归来自共享排队（已由上节更新）

详见 [成对事件账本与建模优化方案](paired-event-model-plan-20260908.md)。本轮只改文档与
离线诊断工具，生产模型和既有候选停止决定不变。

- 两个完整 ROI 的 baseline/FU 候选诊断输出复现上一轮目标状态；连续两窗共 20,002
  records。LLC32 core 2 的 load `224878` 在两侧 event issue/response 完全相同，FU
  却将 UOP completion/retire 从 3961 提前到 3960，均早于 response=4060；这不是合法收益。
- 所选 LLC32 窗口候选首次 retire 落后位于 `228842`：上游 dispatch/issue 晚 144 cycles，
  本请求服务反而短 5，retire 晚 139。不能将其直接归因为本请求共享服务变慢；仍未解释
  全 ROI 的 +171,058 cycles，需追溯更早状态和 checkpoint 传播。
- 新 gem5 密集配对的首个可证正误差里程碑为 kernel load `223952`：FastSim DRAM
  服务 460 cycles、gem5 同一 UOP issue→commit 仅 5。此处当时尚缺具体路径；上节
  已用后续 Ruby debug 闭合为边界空洞内前序 load 带来的 L0D hit。
- 只抬高现有 load completion 到其原 response 的固定服务回放，正/负两窗局部差之和
  5,011/1,162，末端 retire 位移仅 1/0，最大位移 99/2。零改动回放通过，但没有重求
  shared/resource/frontend 状态，因此不是完整修复的 CPI 收益或上下界。

**下一步：** 同时记录 proposal、canonical service、realized response 三条轨道，补齐
成功 admission、状态 generation、胜出 blocker 和关键消费者；在 owner 内原子替换
memory ordering/admission/visibility/response 的职责，而非堆叠旧补偿开关。
正侧路径分歧已经补齐；当前下一步以上节的 shared-order 分歧为准。未定位全流首个根因前，
不扩矩阵、不预报收益；完整门禁与吞吐约束见新方案。

## 2026-09-07 FU future-reservation 第一阶段：机制通过，精度控制失败

**决定：保留默认关闭的 gap-aware FU capacity calendar、审计与微型反例；不推广，
不跑 formal40/DSE54，也不与其他时序修复叠加。** 完整证据见
[FU future-reservation 第一阶段报告](fu-gap-aware-phase1-20260907.md)。

- 9-UOP 差分把独立 UOP 从 cycle 30 回填到 cycle 5，最终退休从 55 降至 30；无未来
  预约的控制图不变，证明候选修复了目标机制。
- `core.fu_gap_aware_schedule=false` 默认关闭。审计模式复现旧调度并只读计算机会，
  两个完整 ROI 的目标输出与关闭态逐项相同。
- LLC32 C4 审计发现 1,291,429 个 UOP、2,167,139 个重叠局部周期；L1D64 C4 为
  458,543 个 UOP、569,131 个局部周期。机会数与周期和不是退休关键路径收益。
- TeaLeaf LLC32 C4 误差从 +9.6487% 恶化为 +10.2244%，增加 **0.5758 pp**，超过
  0.5 pp 停止线；L1D64 C4 从 −17.9910% 变为 −17.9847%，只改善 **0.0063 pp**。
- UOP、指令和请求人口保持不变。候选改变后续共享请求交错；LLC32 总 core cycles
  增加 171,058，因此不能把 producer 局部提前直接解释为 CPI 缩短。

**不要重复的路径：** 把 `fu_future_reservation_cycles` 求和当作可回收 CPI；在正控制
失败后扩完整矩阵；按 workload/误差符号选择开关；将该候选与 load 等待、DTLB 或
branch repair 一起开启后归因；以一次 wall time 观测宣称吞吐通过。

**重新开启条件：** 建立 issue、Ruby admission、response、关键消费者和 retire 的成对
事件链，定位 LLC32 新增周期的首个共享分歧；新候选必须同时通过 L1D64 负尾部、LLC32
正误差控制及未参与选择的窗口，之后才允许完整矩阵和独立交错吞吐测试。

## 2026-09-07 分支恢复首轮：局部机制修复通过，正误差控制失败

**决定：保留默认关闭的实验实现、审计、测试与反例；不推广，不跑 formal40/DSE54，
不进入生产吞吐门禁。** 完整证据见
[分支恢复第一阶段报告](branch-recovery-phase1-20260907.md)。

- 实现前先完成真实见证：Graph500 C8 完整 ROI 的 26,935 个成对 miss 中有 9,058 个
  correct-path fetch 早于 corrected branch completion；连续关键窗口的固定服务最大
  retire 位移为 137 cycles。满足上一轮“无真实见证不实现”的门禁。
- `core.response_branch_recovery=false` 默认关闭。候选在已有单遍 response traversal
  中持久化 recovery frontier，只对真实 predictor miss 建边；不增加第二遍 closure，
  不叠加新 redirect 常数，也不重放当前 checkpoint 已选 cache/coherence 路径。
- 微基准的依赖 miss 修复为 `fetch >= corrected completion`；独立 miss、预测正确控制
  不变；generic/fast-kernel 目标状态相同。Graph500/ASTCENC 真实窗口必要违例从
  10/11 降为 0。
- 全 ROI pilot 中，Graph500 C8 formal 和 ASTCENC baseline C4 的绝对误差分别改善
  **1.6895/1.0106 pp**；TeaLeaf LLC32 C4 的正误差从 **+9.6487%** 恶化为
  **+10.4836%（+0.8349 pp）**。这是只增加必要下界的单调候选，不能同时修复正尾部。
- 候选关闭时，三个 pilot 的目标 `scope_metrics`（排除宿主 throughput）和 `threads`
  与冻结基线逐项相同。retired/请求/branch/DTLB 人口保持不变；候选开启后的少量
  cache/coherence 分类变化来自后续 checkpoint 交错改变，不得宣称完整层次状态等价。

**不要重复的路径：** 调 recovery 常数；按 workload 或当前误差符号选择性关闭真实
控制边；用 gated-cycle 求和冒充 CPI；在正误差控制已失败后启动完整矩阵；把后续
checkpoint 的状态变化描述成当前 checkpoint 已完成 cache/coherence closure。

**重新开启条件：** 有独立、成对事件证据定位一个能减少正误差的更早流水线或共享状态
缺口，并能与该必要控制边共同通过负误差 case、TeaLeaf LLC32 正误差控制和未参与选择
的窗口；之后才允许完整矩阵及独立交错吞吐测试。

## 2026-09-07 全局重审：先筛选，暂停继续 DRAM 探索

**本节覆盖下文“先查 arrival，再做 refresh”的研究顺序。** 用户要求避免重复失败方向，并在长验证前证明收益空间。完整证据见 [全局审查与低成本筛选](global-optimization-screening-20260907.md)。本轮生产模型和开关未改变。

- 当前 74 个 C4/C8 case 的 FR-FCFS candidate 全部为 0；只修改旁路内算法没有直接收益。
- 理想化修好 DSE 最差单 case，P99 最多下降 **0.3926 pp**；修好全部 TeaLeaf，DSE/formal 最多下降 **2.4475/0.2025 pp**。这是其他 case 不变时的覆盖上限，不是预测。
- 新查到 corrected issue 峰值 13、宽度 8，但在已有 10,001-UOP 窗口固定服务回放中，修复资源约束只增加末尾 **3 cycles**，相对 gem5 的跨度缺口为 5,115。此受限回放不构成完整模型上界；结合已有失败记录，**不重开资源日历矩阵**。
- **精度下一步改为 branch recovery 的有界见证。** 新 3-UOP 反例中，load 推迟依赖分支 completion 到 414，正确路径却在 238 fetch、243 issue；移除依赖或预测正确的对照消除该必要先后关系问题。生产 fast kernel 与审计目标输出一致。4-UOP 的“分支后另一条 load 链”显示缺失控制边可以造成错误重叠，但尚无真实 ROI/P99 收益证明。优先 Graph500 C8，附 ASTCENC 机制控制及 TeaLeaf LLC32 正误差控制；没有关键跨度与覆盖证据就不写模型修复。
- **吞吐候选：统计传递与五组历史有界化。** 已核对的统计累计指令块占四份 profile 的 3.43%–5.18% sampled CPU cycles，不等于可移除 wall time；基础调度历史有 ROB 读集证明，61,269 条实际流的 ROB 外依赖均不晚于 dispatch。先做组件原型与完全等价检查，不预报未经测量的吞吐收益。

后续候选先检查 runtime activation、P99 覆盖上限、必要事件关系、零改动对照、关键跨度、新增宿主工作和正误差控制。工具 `tools/screen_optimization_candidates.py` 只读已有结果，`tools/probe_branch_response_frontier.py` 每次只执行 3/4 UOP。完整矩阵是验收阶段，不能再拿它做第一次收益筛选。Q=1024、pending-fill/DRAM 候选默认关闭的决定保持。

## 2026-09-07：暂停 pending-fill 补偿，保留代码，默认关闭

**决定：不推广、不继续调参或扫描相同组合；保留实现、测试、反例及复现入口。**

- C++ 主开关 `core.response_pending_fill=false`；maintained native-FS 配置明确关闭主开关、load-admission 和 store-commit 子开关。
- `core.response_pending_fill_wait=true` 是主开关启用后的行为选择，主开关关闭时不激活修复。
- `configs/gem5-exp-pending-fill.cfg` 仅供显式复现，不是下一轮默认 baseline；维护脚本未引用它。
- 保持 Q=1024 和当前 `gem5-v28_6-fs-materialized-kernel.cfg` 的模型配置。关闭开关不等于物理回退二进制；目标状态等价和吞吐数据分别报告。

**已获得的证据，不再重新猜测。** 参考是同日冻结输入上重跑的当前 baseline，formal40 和 DSE54 分别聚合，不混用 macro CPI 与 user-UOP CPI。

| 指标 | 基线 | 仅 fill 等待 | fill + load admission + store lifecycle |
|---|---:|---:|---:|
| formal40 绝对误差 P99 | 13.5930% | 13.1052% | 15.6729% |
| formal40 最大绝对误差 | 13.7367% | 13.2184% | 16.5058% |
| DSE54 绝对误差 P99 | 17.6522% | 17.5539% | 22.9537% |
| DSE54 最大绝对误差 | 17.9910% | 17.7723% | 30.5978% |

窄修复 P99 仅降低 0.4878 / 0.0983 个百分点。4 个代表 case 的同二进制、串行、交错 3 次中位数表明吞吐下降 1.20%–4.72%；该范围不是 C16/C32 的成本上界。DSE direction 保持 39/48，pairwise 保持 176/216。不能继续沿用此前“预期降低 1–3 个百分点”的估计。

原因与实现边界：

1. LBM 见证窗口中提前返回从 388 次到 0，但全 case 总 core cycles 只增加 93,412；逐请求等待之和为 67,643,497，两者不能相加或相除推导关键路径占比。大量局部时序错误不足以证明它支配 CPI/P99。
2. DSE 最差的 TeaLeaf L1D64 C4 仍为 −17.7723%，第二差的 TeaLeaf ROB256 C8 为 −17.3602%。Graph500 C8 的局部改善不能代表 DSE P99 的收益。
3. 原逐 UOP 工作保留，同时增加了全数据请求的 line 查询、表发布/过期堆和 checkpoint 复制。feedback 耗时增加 6.86%–11.59%；尚未独立测出 hash、堆、复制各自占比。
4. 原计划的事件状态统一没有实现。功能 cache 仍按 canonical 时刻更新；generation 是响应表生命周期标记，不是 cache replacement/coherence generation。
5. 完整 store 组合存在具体反例：容量 2 时，store 到 cycle 656 才准入，其未来 response=846 却先进入 release heap，使本可在 468 发出的独立 L1 load 等到 654。仅移动 store 时刻会留下错误的容量约束。

**不要重复的路径：** 固定当前模型再扫描 Q；扩大 pending 表或微调等待常数；以提前事件数量代替关键周期；以局部 Graph500/LBM 改善或 MAPE 掩盖 P99/max 回归；仅移动 store callback/commit 时刻便宣称统一层级事件；直接启用两遍完整反馈；把只减少数据结构成本当成已经解决剩余精度误差。

**重新开启这个方向的条件：** 有新的、与尾部退休关键路径对齐的根因证据；方案能解释 admission/response 区间、未来预约空档及 cache 状态可见性；先通过对应微型差分，再在固定 Q 的两个完整矩阵、反向误差控制和未参与选择的窗口验证；独立重复量出宿主成本。仅重跑相同配置或替换容器不满足条件。本决定暂停的是当前补偿实现，不是否定所有 pending-fill 语义研究。

证据和输入/二进制指纹见 [第一阶段完整报告](pending-fill-phase1-20260907.md)。主要数字保存在本文件和报告内，即使 `tmp/` 原始产物被清理也不会失去停止理由；原始复现仍需要报告列出的 FST、配置和 stats。

## 下一项研究：TeaLeaf 尾部关键路径与内存并行度对齐

### 2026-09-07 后续：DRAM 语义差分完成，候选保持关闭

详见 [gem5 DRAM 语义对齐](gem5-dram-semantic-alignment-20260907.md)。原 gem5 二进制补采的 1,010 个请求全部通过地址映射及完整 FST/CPL 身份核对；参考仍为 gem5。

- 当前 C4/C8 虽配置 `scheduler=frfcfs`，拓扑启发式实际把候选数压到 1 并 bypass。L1D64 C4 的 188,124 个 DRAM 请求没有进入 FR-FCFS repair。后续必须检查 runtime activation，不能只看配置名字。
- 新的 `dram.frfcfs_causal_selection=false` 修正批内选择事件的到达集合，匹配原 gem5 的三请求反例；精确 gem5 到达流下，refresh 前 9 条关键请求 RD tick 从全部提前 172.6697 cycles 到全部精确相同。这是控制器差分，不能将未运行的旧批处理选择器当作该 C4 生产 case 的直接根因。
- 仅恢复关闭约束，L1D64/LLC32 C4 signed error 为 −14.2309%/+17.5424%；再实际启用修正选择器，为 −14.7013%/+14.6878%。当前关闭状态为 −17.9910%/+9.6487%。正误差控制失败，不扩大到 formal40/DSE54，不报告新 P99 收益。
- 实际激活候选的同二进制、固定 NUMA、串行交错 3 次中位数吞吐下降 **9.6890%/17.5613%**。每 case 增加约万次现有 DRAM 求解 pass；不把内部累计计时当作净开销。
- 64 项读容量在 gem5 包含响应队列，但本窗口每通道最大仅 11 项，无容量饱和；也无 command-window contention。不能继续优先放大队列或归因命令总线带宽。

**决定：新实现/测试保留、默认关闭，`configs/gem5-exp-dram-causal-selection.cfg` 仅供复现，维护入口未引用。** Q 仍为 1024，pending-fill 仍关闭。先查部署路径如何生成 controller arrival、跨批命令状态和 feedback 时间，再做带 drain/PRE/closed-row 状态的 rank refresh 差分。禁止用 oracle 时刻作为推理输入、统一追加 tRFC/load wait，或单靠恢复非零参数宣称语义已对齐。重新推广必须通过真实到达的关键消费者、正误差控制、完整两矩阵及独立吞吐门禁。

以下保留首轮选择依据；后续顺序以上述新证据及完整 DRAM 报告为准。

**状态：2026-09-07 已实施首轮研究，取得局部退休关键路径证据，生产模型尚未改变。** 结果见 [TeaLeaf 尾部时序探索](tealeaf-tail-timing-20260907.md)。后续聚焦共享 DRAM 的请求到达顺序、排队与 rank refresh；不再按错误事件数预测 P99 收益。

首轮已验证的边界与发现：

- 6 个冻结 case 的输入/ROI/分母核对通过；原记录缺逐事件时序，已补采 L1D64 C4、LLC32 C4 的完整 ROI。跨配置测量前缀不同，不得当成同输入单参数因果比较。
- L1D64 C4 的前 100k/核误差为 +6.9744%，完整 ROI 为 −17.9910%；短前缀不足以代表尾部。
- L1D64 C4 core 1 的一个 10,001-record 窗口，gem5/FastSim 退休跨度为 10,059/4,944 cycles；8,559 个 gem5 零退休周期的 ROB 头为已 issue load。
- 原二进制有界 debug 重放的完整 FST SHA256/CPL 均与原实验相同。20 条唯一关联的关键 load 覆盖该窗口 load-head 阻塞的 82.61%；gem5 issue 到准入全部仅 1 cycle，非 refresh 请求准入到响应为 243–358 cycles（中位数 315），FastSim 均为 161。
- 最长请求的 gem5 准入到响应为 1,504 cycles，DRAM 排队 1,309.81 cycles，调度恰在对应 rank refresh 恢复时发生；当前 DRAM 模型缺少该状态。另 19 条的偏差不能一并归为 refresh。
- 这 20 条请求的 FastSim canonical DRAM 排队均为 0，到达时刻比校正 issue 早 85–962 cycles。下一项先做 DRAM 日历/row 状态差分，再决定如何替换求解工作；不能直接把时基差加为等待。

本轮未求出全请求 MLP，也未证明完整 17.99% 误差均来自 DRAM；没有新增 P99/吞吐收益估计。LLC32 C4 正误差控制仍必须同时通过。pending-fill 补偿保持暂停、默认关闭。另一诊断 gem5 二进制在该 checkpoint 的 warmup 中失败，已排除，其产物不得作为本轮证据。

选择依据是当前关闭 pending-fill 的结果：DSE 最差三项均为 TeaLeaf，L1D64 C4 为 −17.9910%、ROB256 C8 为 −17.3517%、LLC128 C8 为 −17.1778%；formal 的 TeaLeaf C16 为 −13.3682%。同一 workload 的 LLC32 C4 则为 +9.6487%，说明统一增加等待会伤害正误差配置。Stockfish 的 same-PC/StoreSet 是另一个已知问题，但单独修复它不能直接覆盖这些最差 DSE case，暂不抢占本项。

首批限定以下 case，使用现有冻结 trace/config/reference，Q 全部为 1024：

| Case ID | 用途 | 当前 signed error |
|---|---|---:|
| `dse-l1d64k8-c04-811.tealeaf_s` | 最差尾部 | −17.9910% |
| `dse-rob256-c08-811.tealeaf_s` | 第二差尾部、容量敏感性 | −17.3517% |
| `dse-baseline-c04-811.tealeaf_s` | C4 同 workload 对照 | −11.4993% |
| `dse-baseline-c08-811.tealeaf_s` | C8 同 workload 对照 | −11.2570% |
| `dse-llc32m-c04-811.tealeaf_s` | 正误差控制，防止统一加等待 | +9.6487% |
| `formal-16c-811.tealeaf_s` | formal40 尾部迁移检查，单独使用 macro CPI | −13.3682% |

研究顺序和交付：

1. **验证输入可比性。** 核对 gem5/FastSim 的配置、ROI/预热、线程映射、CPL、UOP/macro 分母和目标时钟；确认已有 gem5 逐事件记录是否覆盖匹配窗口。缺记录时明确补采需求，不能把只有总 PMU 的 case 描述成已逐事件对齐。跨配置 trace 也需检查，不能把输入差异误归为参数效应。
2. **取得有界的成对时序见证。** 用 thread/sequence/PC/memory ordinal/CPL 关联事件，对齐 dispatch、issue、admission、response、资源占用/释放和 retire。使用共同 ROI 时基，不按窗口重新平移消掉累计误差；必要时向前扩展到分歧的来源。
3. **从退休关键路径反查首个分歧。** 判定是否多发了请求、资源释放过早、响应服务时间过短，或在内存请求之前已有 frontend/依赖/执行端口差异。分别记录容量违例、关键消费者、阻塞来源及配置变化后的趋势。memory-level parallelism 是活跃请求区间的重叠，不是 miss 总数。
4. **证据成立后才选择实现。** 交付能稳定复现的微型差分、它与真实尾部的对应关系、可解释的关键周期缺口，以及候选需要增加/替代哪些运行工作。gem5 timing label 仅用于离线核对，不进入推理输入。
5. **验收。** 先过上述尾部和正误差控制，再运行 formal40 + DSE54，报告 P99/max/MAPE、正负尾部和 DSE direction/pairwise；最终用未参与机制选择的窗口确认。吞吐使用固定 NUMA 的同二进制交错串行对照，计入所有 proposal/辅助 pass，不以嵌套计时之和充当端到端成本。

停止/转向条件：没有匹配事件证据就不进入新模型实现；一个候选只改善负误差、明显恶化正误差控制时停止该候选；不能在退休关键路径上解释观测到的低估时转查更早的流水线阶段，不继续扩大等待表。此时记录“尚未定位”，不宣称已找到瓶颈。

吞吐方向独立保留为后续候选：根据当前 producer/schedule 的 CPU 采样检查静态指令信息是否重复读取，并在目标状态完全一致的前提下评估复用。它目前没有新的收益估计，也不与上述准确率实验同时修改生产路径。
