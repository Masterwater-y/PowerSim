# SPEC CPU2026 双负载四核采集与验证

2026-10-15。两案已完成采集、FastSim回放和native来源/人口审计；精度结果见文末。
本批ROI含额外初始化/验证工作；可选perf-like来源审计的拒绝状态单列，未获得硬件perf资格。

## 输入与目标

从之前的四负载 pilot 选择 `782.lbm_r`、`854.graph500_s`。本批替代小探针作为
应用层验证输入；不是完整 SPEC 成绩，也不将同一 trace 重跑当作独立样本。

| 输入 | 规模 | checkpoint | O3 warmup | stat 起点 |
|---|---|---|---|---|
| LBM | 64×64×64、无 obstacle 文件、单进程4 OpenMP线程，双网格约84MB | `MAIN_initialize` 前、首个 team 前 | 完整初始化+第一个 timestep | 第二个 timestep 前 |
| Graph500（修订） | scale18、edgefactor16，64MB edge list、BFS使用4 OpenMP线程 | KVM单线程读入/CSR/root选择后，仅主线程、无活动region，首次BFS前 | 在O3创建workers，完整第一个BFS及验证 | 第二个BFS前 |

LBM 是**缩放后的 SPEC 源码研究负载**：保留计算内核和 OpenMP 路径，不冒充原始
200×200×130 rate 输入或标准 rate 测量。84MB working set 超过本目标64MB LLC。
Graph500 随附旧 `bfs_test.bin` 只有2MB，与命令 scale18 的64MB要求不符，因此用
同源码 `-P 1` 重新生成，并检查大小、SHA256和64次BFS验证。

两负载实际采集前，LBM标记版和同网格未标记版三步输出一致，Graph500全部64次
BFS在宿主校验成功。数据和命令见[宿主预检](../tmp/spec-two-cache-20261015/host-preflight.json)。

## 采集与回放合同

1. KVM boot/fast-forward；应用自己的 checkpoint MMIO marker 触发 KVM→O3+Ruby
   切换。LBM位于首个OpenMP team前；修订Graph500在KVM完成单线程setup，要求仅主线程、
   `omp_get_level()==0`，在O3创建首个多线程team。本次不额外写RAM checkpoint。
2. O3执行所有 warmup；FST、功能同步和owner事实从详细执行起保留。checkpoint
   后的5ms睡眠位于stat外，用于避开KVM异步退出 quantum。
3. 应用 `workbegin-serial` marker 打开测量窗口，runner reset stats；保留 cache、
   coherence、TLB、branch、DRAM及功能图状态。FastSim回放完整record-bounded warmup。
4. 首核达到10M measured user-UOP，四个参与核同时关闭，其他核保留实际人口，
   不独立截断、补齐或缩短预热。审计四核共用终点、trigger、实际UOP/macro分母。

线程使用显式 `OMP_PLACES={0},{1},{2},{3}`、`OMP_PROC_BIND=spread`、PASSIVE waits。
不依赖虚拟拓扑的 `PLACES=cores` 分组。同步/任务计划由既有 compiler 产生；发生
迁移、缺失前史、owner缺口或共同终点不守恒时拒绝，不能删事件来通过。

冻结目标：四核、3GHz请求时钟、ROB192/IQ64/LQ32、buffer TSO114/effective SQ114；
L1I/D各32KiB/8-way、private L2各1MiB/8-way、LLC8×8MiB/16-way，64B line、LRU。
沿用当前本地 runner 的3GiB/双通道DDR4；这与历史八通道矩阵不同，不能混合汇总。
实际频率和DRAM周期从本案config.ini读取；暴露的DRAM约束按ticks/core-period向上
取整，包含tRAS等，不沿用旧配置中对应的零值。refresh、controller arrival order
和compact Ruby/network envelope的近似必须单列，不能声称全机制等价。

实际采集的网络是 `SimpleNetwork`。冻结 FastSim profile 中 `noc_one_way_latency=5`、
`llc.hit_latency=12`、`directory_memory_latency=18` 等是继承的紧凑时序模型；其中
网络注释描述历史 Garnet 路径，不能当成本案 SimpleNetwork 的完整等价证明。
本批将这些值及实际 controller/link/router 字段一同保留，误差包含这部分模型近似。
L1D load-hit 的2周期映射为 mandatory queue1+load callback/writeback1，private-L2
load-hit 的6周期再加 request2+response2；store-hit 对应1/5周期，不借用load边。

## 磁盘与输出

全局FST上限100,000,000,000字节，99GB提前终止，可用磁盘少于20GB终止。
单案LBM30GB、Graph50035GB，另留256MB单案停止余量；按顺序采集。
guard同时检查其他会话增加的FST。禁止覆盖旧证据，失败源不进入验证结果。
Guest大输入在512MiB tmpfs解码，避免base64临时副本耗尽根盘；tmpfs不是宿主FST预算。

开始时FST约17.61GB、磁盘可用约308GB。既有100GB授权及旧FST清理收据见
[空间策略](trace-storage-policy-20261015.md)。本批命令和收据保存在
[`tmp/spec-two-cache-20261015/`](../tmp/spec-two-cache-20261015/)。

## 校验与报告范围

- 先独立运行 `validate_fs_oracle_identity.py`，再运行维护的
  `run_native_kernel_fastsim_validation.py`；按实际capture config派生参数并保存来源。
- CPI使用审计通过的workload-task active cycles及各自macro分母，列相对误差、
  cycles/macro绝对误差，聚合列CPI MAE。elapsed单列；active缺资格则保留不可用。
- native cache只读守恒的Ruby/SLICC sidecars；perf-cache v2近似值与native诊断分开。
  重点列L1 load miss、L2 demand miss、LLC core miss原始计数、缺资格原因及可评分范围。
- 不把 `taotrace-path-class-v3` cache字段当native truth；不将all-unavailable的
  validator passed写成PMU精度通过。MAPE与WAPE分开；自然零值不因样本不足被隐藏。

## 已发现并修正的采集问题

- 首轮构建误用了 `materials/include/gem5_roi_marker.h`，只支持guest缺失的bridge，
  在checkpoint前退出。已切换为与既有成功实验一致的
  `workloads/spec_roi_integration/gem5_roi_marker.h`，含/dev/mem回退。
- 复制runner时遗漏其相对scheduler-proof helper，预检失败；已冻结该依赖。
- 大输入原来将encoded temporary放在guest根盘；新增真实shell解码/清理回归测试，
  临时文件和解码目的地改用同一tmpfs。缺失shlex导入也由改进后的独立函数测试捕获。

这些失败收据保留在experiment的`diagnostic/`，不能计为已完成SPEC采集。

## 详细预热阶段的 owner 采集修复

真实LBM采集进入O3后，在初始owner注册读取`init_task.pid`时触发Ruby functional
read fatal。GDB定位到x86功能页表遍历；第二轮RubySystem日志显示对应PML4项存在
4个Busy controller，而响应数据尚不可读。不能忽略错误或改写成任意PID。

本批增加窄的静态身份规则：仅当kernel SHA256为
`da3d5e7dbf696b977f635d1123e06b324af873183d90111101ced005dc64e174`，且指针恰为
`0xffffffff81e11780`时，使用该内核静态boot/idle task的恒定PID0。实际ELF的
`init_task`符号、pid偏移`0x758`、初值零已独立校验；其生命周期语义参见
[Linux v5.4.49启动路径](https://raw.githubusercontent.com/gregkh/linux/v5.4.49/init/main.c)。
其他地址、其他内核、动态idle任务仍实时读取并检查任务/PID重用；不把一次读取失败
自动降级为PID0，不修改Ruby协议时序。两项新测试由RED到GREEN，独立审核未发现
Critical/Important问题。该规则尚未消除其他动态字段的冷页表读取风险，必须以
重新采集和owner审计确认；不宣称广义Ruby功能读取问题已解决。

空间guard也经独立审核补齐了退出后的最终容量复查，以及SIGTERM时的子进程组
回收；两项实际子进程回归均先复现失败，再通过。失败/中止收据保留，禁止成功退出码
掩盖容量越限。采集器检测fatal后停止，并关闭core dump，避免失败时占用额外空间。

### 动态任务读取及最终采集配置

静态身份修复后的重采越过了原故障点，但在56.98MB处又遇到动态任务字段的同类
functional-read fatal，证实窄修复不能解决整个类别。没有扩大PID猜测规则。

最终两案显式选择gem5已有`access_backing_store`模式，配置与
`gem5-fs/configs/ruby/Ruby.py`的既有实现一致：Ruby的`phys_mem`为完整3GiB
`SimpleMemory(in_addr_map=false,kvm_map=true)`；两路原有DRAM的`kvm_map=false`，
其容量、时序及Ruby协议保留。**从boot起**便由KVM映射这份功能存储，O3切换后
继续使用同一份数据；禁止在switch时才切到一份空存储。RubyPort的既有callback
负责功能数据，Ruby/DRAM负责目标时序，避免observer为读取PID而依赖尚未返回的
Ruby数据。此模式由新runner选项`--ruby-functional-backing-store`显式开启，默认关闭。

实际`config.ini`必须独立通过`functional-memory-audit.json`检查。它与旧采集的
功能数据配置不同，需随本案披露；不能以既有身份门禁替代这项检查。新的完整采集、
owner及native PMU守恒仍是最终验证条件。

维护范围：guest staging固定512MiB tmpfs，适用于本批输入规模；实验适配器
`prepare_case.py`仍有assert，固定使用未开启优化的Python3.11；静态boot-task规则
尚未单列每次使用计数，保留kernel/collector/binary SHA及exact-ELF回归证据。
这些审核Minor未扩大为通用输入规模或任意内核支持承诺。

## 预热过长及checkpoint修订

用户指出本批warmup耗时过长。LBM实际warmup为294,776,509条记录（约18.87GB），
总采集约49.6分钟、21.21GB FST。原方案把初始化、内核清页、串行统计和整个第一步
TRT都放在O3，导致约43分钟才打开stat。第二步起点是清楚的统计边界，但这份
checkpoint过早、预热未控制成本；不能将这些工作都说成必要的cache warmup。

保留已经完整采集的LBM作为原方案对照。尚未启动的Graph500旧队列已停止，未用旧
ROI启动采集。修订版将读入、CSR构建和root选择移到KVM，并限定setup仅一个线程；
不把已有四线程池及未完成futex等待直接切入“empty”上下文。

`fastsim_serial_setup_checkpoint.h`保存请求线程数、预先建立主线程runtime身份，
关闭dynamic teams并用单线程完成setup。checkpoint前显式检查/proc任务枚举的
打开/读取/关闭错误、恰好一个OS任务、当前为进程主线程、OpenMP level为0。
这些检查不受NDEBUG移除。切换及5ms交接后重新输出相同主线程身份，恢复四线程；
workers在O3首次创建，其identity、wait和region前史全部记录，所有计数器保留。

独立设计及实现审核无Critical/Important问题；指出的测试observer数据竞争和
主线程身份tuple未核验已修复。三项真实插桩runtime测试通过：身份保持及四成员
team、已有worker池拒绝、即使单线程的嵌套region也拒绝。新Graph500宿主64次
BFS验证通过。实采的main-only KVM交接、四成员task begin及其先前identity已通过
`roi-handoff-audit.json`；功能同步、owner和共同终点审计也已通过。实验适配器曾将
主线程parallel begin(kind1)与task begin(kind3)重复计数，已依据维护compiler语义
改为分别检查唯一parallel begin和四个task begin；原始事件未修改。

此修订只减少setup的详细执行成本，**没有证明一次完整BFS是最短或已收敛的预热**。
下一步需要同一输入/ROI的预热长度收敛对照，分别比较cache/MPKI、active CPI及
elapsed；不能预先承诺固定指令数足够。源规模、终点和FST上限保持原合同。

后续实采进一步确认，Graph500的`make_bfs_tree`约在启动后7分钟已返回，之后长时间
停留在`verify_bfs_tree`对4,194,304条边的检查。将整轮验证也置于O3 warmup属于
本批成本过高的第二个原因；移动setup checkpoint仅修复第一部分，不能称warmup
优化已经完成。后续性能ROI应与正确性检查分开：正确性在宿主或KVM独立验证，
详细warmup执行计算内核，再用固定stat ROI做预热长度收敛对照。
源码已有`SKIP_VALIDATION`环境开关，本批未开启。后续可在独立正确性预检后
启用它并重新采集；需同时处理未填写`bfs_nedge`时的末尾汇总，不引用无效TEPS。
不能删除本批验证记录后沿用原cache真值。

程序默认 `NBFS=NBFS_max=64`，即最多64个root、64轮BFS；本输入宿主预检完成64轮。
本次gem5采集只完成第1轮计算与验证（warmup），第2轮打开stat，达到共同终点后停止，
没有详细执行64轮。实采总耗时3604.264秒，Graph500 FST为25,124,439,584字节；
四核warmup共343,849,306条记录，约22.01GB，约49分钟才进入stat。
这份warmup过长主要来自验证代码，不能解释为缓存达到稳态所需时间。

本批Graph500的stat标记位于第二轮BFS循环入口、分配bfs_tree之前；循环后仍含
正确性验证，实际共同终点可能覆盖它。因此本批是**BFS循环片段**，不是已证明
排除验证代码的纯BFS kernel ROI，不作为标准SPEC成绩。保持本案原始范围，作为
后续修订的对照；不能删除验证前史后继续引用原来的gem5 cache真值。

## 大负载暴露的perf退休来源审计失败

LBM已完成当前FastSim回放（319.38秒，非安静宿主吞吐对照）。可选perf-cache来源
审计拒绝：core0的`incomplete_macros=2`，独立FST扫描为0。原始sidecar保留，
失败报告见`tmp/spec-two-cache-20261015/lbm-validation/`。

独立定位与复核确认，gem5 `retirement::Ledger::owners()`会累计整个前史中已丢弃的
未完成前缀；FST扫描只能凭last-micro标志识别最终未闭合前缀，缺少native first/ROM
边界。ordinal57,908和73,199各有一个warmup ROM入口打断单UOP前缀；measurement
到101,894,310才开始，解释了native的2 macros/2 UOPs。测量内boundary_discontinuities
为0与之相容，但不能据此宣称所有scope均已独立核验。

仅保留16个ROM诊断样本且遗漏656个事件，不能拿有限样本当完整功能边界证明。
**不减2、不忽略检查、不改写oracle、不将可选perf失败改成通过**。另运行维护的
native-only validator，独立决定active CPI和Ruby/SLICC tag-miss诊断能否报告；
它的通过不修复或认证perf-like来源。

后续来源修复需要将measured-window incomplete与all-history/warmup诊断分开，
明确跨窗前缀的UOP定义，并保留完整、source-ordinal锚定的first/last/ROM功能边界；
有界诊断样本与完整性证据分开，测量样本不能被warmup耗尽。回归需覆盖warmup
abandon、measured abandon、跨窗和最终tail。本批不为通过审计而修改这些语义。

Graph500也完成回放，但可选perf-cache审计在core0拒绝`failed retirement source coverage`。
其四核perf-retirement所有scopes均为`available=false`；顶层coverage中
`observed_leading_incomplete_macros`为500/132/716/284。common-cut、owner、native drain
等布尔项为true，并不能替代退休来源资格。有限ROM诊断不能解释所有缺口；本批不将
此失败简单归为LBM的“两次warmup abandonment”，也不把它当零miss或可用估计值。

<!-- TWO_CASE_RESULTS_BEGIN -->
## 双负载对照实验结果

`workload-task`、`task-oncpu-switch-entry-v1`；两案native来源及人口门禁通过。
CPI使用active cycles/macro；通过门禁不代表达到精度阈值或通过硬件perf认证。
Graph500包含正确性验证，LBM包含完整初始化预热；本批是计算与验证拆分前的研究对照。

| workload | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| c04-782.lbm_r | 0.979595 | 3.336837 | +240.634% | 2.357242 |
| c04-854.graph500_s | 2.366306 | 1.567496 | -33.758% | 0.798810 |

两案CPI MAE **1.578026 cycles/macro**；CPI MAPE 137.196%。

| workload | gem5 elapsed ms | FastSim elapsed ms | 相对误差 | 绝对误差 ms |
|---|---:|---:|---:|---:|
| c04-782.lbm_r | 2.049716 | 6.979712 | +240.521% | 4.929997 |
| c04-854.graph500_s | 5.096393 | 3.242476 | -36.377% | 1.853916 |

多核elapsed不求和；以上是目标机统计窗口时间，不是宿主采集/回放耗时。

以下为**native tag-miss诊断，不是perf-like事件**：

| workload | 事件 | gem5计数 | FastSim计数 | APE |
|---|---|---:|---:|---:|
| c04-782.lbm_r | l1d_tag_misses | 251,913 | 843,701 | 234.918% |
| c04-782.lbm_r | private_l2_tag_misses | 144,964 | 525,101 | 262.229% |
| c04-782.lbm_r | llc_tag_misses | 116,096 | 81,569 | 29.740% |
| c04-854.graph500_s | l1d_tag_misses | 520,852 | 753,647 | 44.695% |
| c04-854.graph500_s | private_l2_tag_misses | 326,326 | 541,545 | 65.952% |
| c04-854.graph500_s | llc_tag_misses | 126,753 | 204,945 | 61.688% |

| native事件 | MAPE | WAPE | max APE |
|---|---:|---:|---:|
| l1d_tag_misses | 139.806% | 106.706% | 234.918% |
| private_l2_tag_misses | 164.090% | 126.325% | 262.229% |
| llc_tag_misses | 45.714% | 46.415% | 61.688% |

MAPE按案例等权，WAPE按参考计数加权；两者不互换。
来源为守恒的Ruby/SLICC sidecars，经维护validator评分；未使用path-class cache字段。

| workload | FastSim观测吞吐 M measured user-UOP/s | 宿主回放秒 | FST GB（十进制） |
|---|---:|---:|---:|
| c04-782.lbm_r | 2.154 | 319.378 | 21.209 |
| c04-854.graph500_s | 3.010 | 322.114 | 25.124 |

吞吐为本次观测，宿主同时有其他工作，不能当安静宿主重复实验或优化增益。
end-to-end user-UOP吞吐仍不可用：native warmup尚未按权限拆分；不与measured指标互换。
采集/回放没有修改FastSim模型；不能把报告来源修正说成预测精度改善。

完整逐案与聚合见[维护validator报告](../tmp/spec-two-cache-20261015/native-validation/summary.md)。
独立目标身份见[identity.json](../tmp/spec-two-cache-20261015/identity.json)；
逐参数映射和共同终点审计保留在各案`parameter-provenance.json`、`common-end-audit.json`。

可选perf-cache来源审计 `lbm-validation` 状态：**failed**；保留原始拒绝结果。
错误：ValueError: /home/data00/yinhaolang/FastSim/tmp/spec-two-cache-20261015/source/c04-782.lbm_r/tao_trace/oracle/perf-retirement-core0.json: physical:incomplete_macros independent functional population mismatch 2 vs 0

可选perf-cache来源审计 `graph-perf-validation` 状态：**failed**；保留原始拒绝结果。
错误：ValueError: /home/data00/yinhaolang/FastSim/tmp/spec-two-cache-20261015/source/c04-854.graph500_s/tao_trace/oracle/perf-retirement-core0.json: failed retirement source coverage

本批不能提供三方对齐的perf-like miss精度；native表不能替代此资格。
<!-- TWO_CASE_RESULTS_END -->

最终空间快照：本批两案FST合计46,333,294,848字节；全workspace FST为
64,967,191,672字节（64.967GB），低于100GB上限；采集后空闲约451GB。
已清理失败/过时诊断FST及伴随文件共95,099,520字节，清单见
[删除收据](../tmp/spec-two-cache-20261015/diagnostic-fst-retirement.json)。
当前两案和受保护回归FST保留，完整清单见
[采集后空间快照](../tmp/spec-two-cache-20261015/post-capture-fst-inventory.json)。
