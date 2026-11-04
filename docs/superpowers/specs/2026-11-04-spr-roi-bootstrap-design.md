# SPR ROI 边界初态与跨窗口同步设计

用户批准将上一轮方案落入文档后实施。范围是修复采集器的可观察性，保持原 OpenMP 二进制、WORKBEGIN、参数和同步行为。父分支fastsim-spr，子分支gem5-spr/yinhaolang。

## 问题与目标

上一轮OpenMP C4四核共同ROI边界已经存在，但core1的首次内核记录没有证明任务计时初态；core2/3在ROI前没有原生counter通知，且首次记录来自idle。不得通过新增barrier、唤醒工作线程、等待线程到齐、延后ROI或把未知状态直接标known解决。

目标：在原边界建立可独立重算的native CPU counter epoch和可由退休功能流验证的任务初态；预热中的等待/唤醒/barrier状态连续进入测量。未证明的其他初态继续拒绝。

## 方法与取舍

采用边界快照+持续观察+同步状态继承。延长/改写负载预热会改变测量人口，故不作为通用修复。只按PID或内核态初始化也无法区分调度切换阶段，故拒绝。

BaseCPU的previousState和previousCycle是protected字段，新增public的observeCycleCounter() const只读副本接口；probe在首个原生通知或首个ROI快照前建立明确的本地counter epoch，记录当前tick/cycle、previousCycle、previousState、period和来源。不调用updateCycleCounters、不改CPU字段。原生epoch仅进入oracle。pending ON尾部按既有BaseCPU通知语义计算；长Sleep/Wakeup区间不计ActiveCycles，uint32 delta按原生规则独立验证。正常ON溢出仍拒绝不可靠尾部。

任务初态优先沿用已接受的用户态首退休记录和成对account-out/in证据。对已审计5.4.49内核采用first-account-out-idle-prefix-v1功能证明：初始owner必须为PID0，完整连续退休流的首个真实计时切点必须是account-out(prev_pid=0)。若初始处于transition，首个切点应为account-in；同任务调度和IRQ不改变此推理。必须以完整FST/.fsem扫描证明所有真实out/in宏与事件逐一对应。保留原始initial-unknown和原始native快照，另附功能证明，在最终分类和计划中解析初态，不改原生counter。不能把硬件入口伪宏保存的idle返回PC当作已退休idle指令；也不能只按PID0初始化。采集中的全功能记录与hooks连续保留。若没有可信初始锚且没有足够完整前史，则保持unknown；新增快照不冒充通用任务栈证明。

功能metadata只保存任务状态、证明类别和与退休流相符的PC；counter、tick、服务时间不进入模型计划。独立审计必须从当前FST、exact-build proof与原生epoch/通知重新验证，不信任producer布尔值。旧无epoch格式继续保守兼容，不回填历史源。

## 跨ROI例子

ROI前A持锁L计算、B在futex等待，核1处于idle。WORKBEGIN时只打开测量累计，不清除B的等待关系。ROI内A释放L，使B成为Ready；之后实际sched-in才使B成为CPU owner。FastSim从保留的功能依赖计算自己的时间，不读取原生释放/切入tick。若B在ROI前已经被唤醒但尚未切入，也必须保持Ready而不是重置Running。

## 验证与边界

RED/GREEN覆盖：无首counter通知的idleROI、KVM交接State/Cycle继承、长睡眠/唤醒与pending ON尾部、坏epoch/任意kernel PC/PID伪造拒绝；warmup等待在measurement唤醒、Ready与Running分离、已有barrier依赖不被ROI清空。

重新运行原OpenMP C4（原binary/args/WORKBEGIN和300000目标），检查4/4 ASID表、所有功能事件消费、逐核宏分母与独立native账一致。固定线程C4作兼容控制。使用维护identity/native validator；CPI有资格才报告，含绝对误差和MAE。硬件perf等价和业务精度验收仍独立。

除src/cpu/base.hh新增的12行只读接口外，全部非probe SPR src保留原哈希。接口不增加数据成员、不改变时序或执行行为。父gitlink保留02119d1，通过portable patch分发子提交，不向子仓库原origin推送。阶段记录与实际证明范围在结果文档列明。

## 实施裁定

实际首PC核1是串口I/O，核2/3是硬件入口伪宏的idle返回地址，故否决PC白名单。first-out功能证明已独立审查成立，限定新采集、PID0和完整连续流；不对旧数据补写证明。编译确认BaseCPU字段是protected；采用批准方案中的最小只读接口，唯一非probe差异是base.hh的状态副本getter，保持其他源码不变。已有ROI逻辑没有清除同步状态，补回归而不作无必要的生产修改。

实际首轮新采集的三个idle核均通过，但主核这次从任务内核路径开始（initial PID1194），暴露同一证明范围的启动差异。因此新增first-account-out-task-prefix-v1：初始任务PID/实例必须与首个真实account-out的prev完全一致，并沿用完整FST/.fsem扫描门禁。相同成对切点证明适用于正PID。正PID前缀若在ROI开始非ON或未知前缀中发生Sleep，producer拒绝解析，独立审计也必须拒绝其严格计时；不借此支持尚未建模的任务halt。原始initial-unknown仍保留。首轮c4-openmp标为diagnostic-only，最终必须重新采集，不能补写旧证明。
