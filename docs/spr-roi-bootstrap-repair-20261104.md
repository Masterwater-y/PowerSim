# SPR ROI 初态与跨窗口同步修复

本轮方案先记录于[设计](superpowers/specs/2026-11-04-spr-roi-bootstrap-design.md)与[实施计划](superpowers/plans/2026-11-04-spr-roi-bootstrap.md)，随后按用户批准实施。原OpenMP二进制、参数、WORKBEGIN和同步行为保持原样，不增加barrier、人工唤醒或移动ROI。

## 实现

周期初态与任务初态分开建立。BaseCPU新增12行const状态副本接口observeCycleCounter()，只返回previousCycle及On/Sleep分类；不增加数据成员、不调用updateCycleCounters、不修改状态。probe在首次原生通知或ROI之前记录counter_epoch，绑定读取时刻、原生上次更新周期、状态与频率。原生审计独立重算通知、尾部与Sleep/Wakeup排除，允许原生uint32 delta在长非ON区间按实际规则回绕；不把长ON溢出当成有效周期。

任务初态保留原始initial-unknown和原始native快照。对新采集另外输出first-account-out-idle-prefix-v1证明：初始owner是该核PID0，完整连续退休流的首个真实计时切点为account-out(prev0)，且任务incarnation一致。若前缀已经处于切换中，首个切点必须是account-in；同任务调度和IRQ不改变这个成对切点状态机。证明不使用原生时长，不猜测未执行路径。

消费者逐条扫描当前完整FST与.fsem，核对全部真实out/in宏完成与功能事件的一一对应，排除硬件入口伪宏及被证明放弃的前缀；校验完整流哈希、内核profile、连续采集和零失败门禁，再将有效初态提供给计划。原始unknown信息仍保留，oracle周期不进入计划。没有新证明的旧采集保持不可评分，不回填。

该方案代替了idle-PC白名单：原失败组核1首PC实际来自串口I/O，核2/3的idle返回地址属于硬件入口伪记录，不能冒充已退休idle指令。

## 同步例子与回归

ROI前A持有锁L计算，B在futex等待，核1空闲。WORKBEGIN只打开统计窗口；B的等待关系继续存在。A在ROI内解锁后，B变为Ready，实际sched-in之后才成为CPU owner。FastSim按功能依赖计算自己的时间，不使用原生唤醒/切入tick。

审查未发现现有ROI切换清除同步状态，因此没有为此修改生产调度代码。新增回归覆盖warmupwait→ROIwake→实际switch-in、warmup阶段Ready状态保留、TaskClock pendingtransition延续、barrier先到成员的许可跨ROI保留，以及真实Simulator的12条任务宏/16条物理宏人口分离。

## 验证记录

8项原生producer/profile/epoch测试通过，最终155项相关Python回归通过，FastSim默认构建与fastsim_tests通过，独立源码审查无遗留material问题。审查发现的“预热中解析idle初态产生零计数PID条目”已补RED/GREEN回归并修复。首次Python整组运行有一项GCC运行库环境错误，进程内设置LD_LIBRARY_PATH后整组通过，新增任务前缀用例后最终155项全通过，未更改全局环境。

唯一非probe源码差异是base.hh的const getter；其余7362个已跟踪非probe源码与本轮开始哈希一致。SPR前端、ROB、IQ/FU、store及Ruby/SLICC执行逻辑没有改动。

两组最终新采集均使用维护入口`--source-root`和`--stats-dir`复核，expected=2/discovered=2/passed=2/failed=0/pending=0。独立目标身份均valid；原生counter审计和模型计时均available，unknown周期为0，逐核宏指令数完全一致。参考cache来自守恒Ruby/SLICC原生摘要，没有使用path-class字段。该passed状态只认证来源与守恒，不认证误差阈值或真机PMU。

| 新采集 | 任务宏指令数 | 完整计划事件消费 | gem5 CPI | FastSim CPI | 绝对误差（cycles/宏指令） | 相对误差 |
|---|---:|---:|---:|---:|---:|---:|
| OpenMP C4 | 1,012,379 | 953/953 | 0.834907 | 1.046804 | 0.211897 | 25.3797% |
| 固定线程C4对照 | 174,066 | 136/136 | 0.610424 | 1.050021 | 0.439598 | 72.0152% |

两样本CPI MAE为**0.325747 cycles/宏指令**，MAPE为**48.6974%**，无不可评分样本。完整分布就是上述两条机制探针结果，不外推为业务负载精度。模型误差仍明显，本轮没有进行微架构调参或误差归因；两次独立采集的人口/执行路径可能不同，不能把与前批探针误差的差异归因于本次修复。硬件CPU_CLK_UNHALTED/perf等价尚未验证，吞吐未重新验收。

OpenMP逐核任务宏为`[171437,210131,280438,350373]`，核0实际使用正PID任务前缀证明，核1/2/3使用idle前缀证明。四核原始initial_known均保持false，原生快照未改写。四核静态表有效，按本次实际配置映射184项参数；原负载参数逐项比较无差异，二进制哈希与前批记录相同。

完整精简证据与二进制/配置/计划哈希见[summary.json](spr-roi-bootstrap-20261104/summary.json)。原始日志与证据保留在tmp/spr-roi-bootstrap-20261104/，大体积输入不加入Git。

实际首轮新采集的三个idle核均通过，但主核这次从任务内核路径开始（initial PID1194），暴露同一证明范围的启动差异。因此新增first-account-out-task-prefix-v1：初始任务PID/实例必须与首个真实account-out的prev完全一致，并沿用完整FST/.fsem扫描门禁。相同成对切点证明适用于正PID。正PID前缀若在ROI开始非ON或未知前缀中发生Sleep，producer拒绝解析，独立审计也必须拒绝其严格计时；不借此支持尚未建模的任务halt。原始initial-unknown仍保留。首轮c4-openmp标为diagnostic-only，最终必须重新采集，不能补写旧证明。


## 发布与剩余限制

独立gem5-spr/yinhaolang提交为b69ba85b05b1f63367017d3dcd77eb5e59ff1887。父仓库保留可获取的02119d1 gitlink，更新完整portable patch并通过反向应用检查；子仓库原origin未推送。后续代码仍在fastsim-spr维护。

任意任务初态并未被无条件认证：需要完整源、匹配实例和首个真实out证明；首个切点为in、没有足够见证、正PID未知前缀跨越未建模的休眠或其他内核版本时仍拒绝。旧源缺少新epoch/证明时不回填。下一步是独立诊断当前两组模型CPI残差，并在业务C4窗口扩大验证；不能把可评分误报为精度已对齐。
