# SQLite / Zstd 标准 rate 语义采集

2026-10-16，用户授权：先采集 SQLite、Zstd rate，删除旧 FST，沿用 KVM → warmup → stat，并避免重复 boot 和过长 warmup。

## 本次约束与步骤

1. 只读使用 `/data00/zhonghaocheng/benchmark/spec_cpu_2026` 的原始源码和输入；在本项目 `tmp/spec-rate-sqlite-zstd-20261016/` 建立独立构建副本。核验发行 MANIFEST；原安装不修改。
2. 使用 SPEC harness 构建，保留 `Spec/object.pm` 的宏、源码列表和单线程语义。只插入采集标记；不添加 OpenMP，不向 benchmark 内添加 fork，不缩小官方输入。
3. 首批四核、四个独立副本：SQLite 使用官方 refrate 的 `--memdb --size 2000 --testset main --verify`；Zstd 使用官方 refrate 第一条 `-b3 -e3 --verbose -i40 cld.tar`。这是官方输入的指定阶段研究窗口，不代表整个 benchmark 的所有输入或可发布 SPEC 成绩。
4. 外部 launcher 启动/绑核四副本；每副本在指定计算入口通知 ready 并阻塞。SQLite 标记在 main 的 test 130 前，数据装载/建表完成后；Zstd 标记在输入装载、校验 hash 和参数准备完成后，第一轮压缩前。四副本全部 ready 后，launcher 触发 KVM → O3+Ruby，随后放行。标记只用于共同入口，不改变计算算法或结果。
5. O3 warmup 初始预算为 core0 的 1,000,000 个 gem5 instruction-stop 计数（排除 NOP 和 instruction prefetch），保留全核 user+kernel 前史；最长 180 秒，超限记失败，不强行开 stat。暂未证明预热收敛。
6. stat 使用 `first-core-target-common-end-v1`：最快参与核达到 10,000,000 measured user UOP 后全核同时关闭。保留各核真实宏/UOP 人口，不单独裁剪成相同长度。
7. 四核、八通道 DDR4、buffer TSO114、LRU、原有核心/cache 参数；受当前 X86Board 3GiB 上限约束，先进行四副本内存预检，不通过则修正平台容量，不能缩小官方输入或默默 swap。
8. 全局 FST 上限 100GB，99GB 提前停止，空闲保留20GB；逐案上限20GB，顺序采集。删除旧大 FST 的 receipt 保留；小型回归输入保留。

## checkpoint 经验和选择

- [9月恢复失败及恢复门禁](tso-buffer114-baseline40-collection-20260918.md)：活跃多进程同步现场的 checkpoint 曾恢复不到 WORKBEGIN；后移/前移标记不能凭静态检查证明恢复成功，需要真实恢复门禁、二进制/配置/输入身份及失败隔离。
- [10月同步前史经验](tealeaf-4c-sync-trace-accuracy-20261012.md)：已有 OpenMP region 的迟到事件会造成前史缺失，本次 rate 保持原生单线程。
- [最近两负载流程](spec-two-cache-validation-20261015.md)：当时 checkpoint marker 仅切换 CPU，未保存 RAM；详细 warmup 曾误包含大规模初始化/验证，不再沿用。

本次保存实际 KVM checkpoint：OS 已 boot、二进制和输入已放入 guest tmpfs，但 launcher 尚未 fork 四副本。恢复后继续 KVM 执行初始化、在四副本计算入口切换 O3，然后重新 warmup。该 checkpoint 省去 boot/staging，不保存 O3 微架构热状态，不能省略详细 warmup。

首次创建后必须由同一 gem5 binary、硬件配置和输入身份恢复到共同计算入口并完成短窗口；通过后才作为可复用 checkpoint。checkpoint 和 manifest 独立保存，不随旧 FST 清理。保存点仍包含正常 OS 任务，不能表述为全系统只有一个任务。

## 审计与状态

采集后核验四副本身份、无迁移/未覆盖 owner、共同起止、native 守恒和参数映射。后续回放使用维护 validator；active CPI 与 elapsed 分开，CPI 同时报告相对误差和 cycles/macro 绝对误差，PMU 缺项明确披露。

执行记录：`../tmp/spec-rate-sqlite-zstd-20261016/`。SQLite 与 Zstd 均已完成正式采集、独立身份审计和维护 validator 回放；最终结果见文末。以下“失败”记录保留诊断背景，不作为通过数据。

### 已完成的预检与恢复故障定位

- 删除旧 TeaLeaf 与 LBM/Graph500 大 FST 共12个、22,254,851,296 bytes；其配置、统计和 oracle 保留。清理后约7.20GB FST，小型回归和 PaTGen 输入未删。
- 独立安装副本中149个发行文件哈希通过。原版与标记版均由 SPEC harness 构建；标记版明确设置 `strict_rundir_verify=0` 允许已记录的两处源码插桩，未改发行 MANIFEST，也不作为 reportable 运行。
- SQLite main 和 Zstd level3 refrate 的四副本完整宿主执行逐份与官方输出完全相同。所选计算入口总RSS分别1,056,223,232和1,020,829,696 bytes；四副本均单线程、零swap。guest另显式关闭swap。
- 实际 KVM checkpoint 首次写出约83MB。真实恢复触发 `EventQueue::schedule: when >= getCurTick()`：Ruby `startup()` 对零条cache记录仍将本队列时钟归零，而其他KVM队列保留checkpoint时间。cache.gz解压大小确认为0；不是应用同步死锁。
- 最小修复只对零记录Ruby checkpoint跳过空恢复回放；非空cache恢复逻辑保留。补丁为 `patches/gem5-ruby-empty-kvm-checkpoint.patch`；新binary已真实恢复并到达四副本计算入口及O3。gem5按192个可用核的80%，即153 jobs编译成功，新SHA256为`ae197032a7e875e1c584322b6890ee17626821c43afd413a72a5dbbce3f96e44`。构建需同时设置`TAOGEN_SHARED=<repo>/gem5-fs/taogen/shared`和Python3.11的`PYTHON_CONFIG`。
- 空间监控扫描时遇到全量测试删除临时目录，曾导致采集被停止；已复现并修复为只容忍目录消失，其他I/O错误仍拒绝，6项空间guard测试通过。
- 独立审核发现rate默认warmup=0没有WORKBEGIN可开stat；已新增明确拒绝，checkpoint-only除外。当前rate模块5项测试通过。
- 当前维护C++ build与 `fastsim_tests` 通过；Python全量最终453项，0断言失败、3个既有环境错误（缺`fastsim_py`、两项外部TCSim checkpoint wrapper缺失）、5 skips；不能表述为Python全量通过。

新gem5 binary或runner变更后创建fresh checkpoint，不改写旧checkpoint身份。旧失败目录放在本次experiment的`diagnostic/`，禁止作为完成源。

### 有界预热与KVM quantum

继承的`--kvm-quantum-us 1000`使第一次真实恢复在180秒warmup上限停止。失败FST独立扫描发现core0有6,083,070个完整记录、3,577,361个完成宏指令，远超请求的100万计数；扫描只用于诊断失败源，不作为PMU oracle。

`gem5-fs/src/sim/sim_events.cc:exitSimLoop`明确将退出事件排到`when + simQuantum`，因此指令停止触发后仍执行一段O3；[上一轮文档](retired-cache-eight-channel-results-20261015.md)已记录相同的边界/收尾延后。当前将host同步quantum从1000us改为10us，目标核心、内存、cache与TSO参数不变。按新quantum创建fresh checkpoint并重做恢复门禁，保留实际warmup人口，不把请求预算冒充精确实际长度。

超时的4个FST共1,601,130,496 bytes已按哈希收据删除，失败日志、人口诊断和checkpoint保留。初始100万指令预算、180秒warmup上限不放宽。该调整改善的是采集边界和宿主成本，不是FastSim模型精度修复。

10us真实SQLite恢复门禁已完成：warmup55.09秒，core0实际1,030,442宏指令，随后正常stat及100K共同终点。正式SQLite采集warmup56.24秒、全程423.83秒，FST3,036,947,680 bytes；四核measured user UOP分别9,889,572 / 9,991,016 / 9,998,607 / 10,000,001，core3触发共同终点。owner、native目标身份和共同终点审计通过，维护FastSim validator亦通过单案来源/人口门禁；不等于精度阈值或硬件PMU认证。

### Zstd启动脚本容量与分案源版本

Zstd原始`cld.tar`为126,895,104 bytes；原base64 readfile脚本在guest init挂载tmpfs前就要写入小根盘，导致`Failed to write file`，尚未运行应用。修复为gzip+base64传输，在同一guest tmpfs解码、解压，原输入、命令和算法不变；实际新readfile约65MiB。真实shell测试验证字节一致、暂存文件清理和脚本缩小，全量452项仍只有3个既有环境错误、5 skips。

SQLite已运行的旧传输runner完整冻结在`tmp/spec-rate-sqlite-zstd-20261016/frozen-runner/scripts/run_gem5_sync_probe.py`，其内容SHA与SQLite checkpoint receipt严格相同，辅助tools链接到维护工具。`runner-source-map.json`保留原路径与快照映射，SQLite后续恢复应使用该冻结runner；Zstd使用压缩传输的新runner及自己的fresh checkpoint，不能拿当前runner的SHA冒充SQLite采集源码。

Zstd 使用自己版本的冻结 runner，当前路径为 `tmp/spec-rate-sqlite-zstd-20261016/frozen-runner-zstd/scripts/run_gem5_sync_probe.py`。新 checkpoint 已真实恢复到四副本入口/O3，短窗口已通过。SQLite详细采集结束后才启动Zstd详细采集；仅Zstd的boot/checkpoint准备与SQLite尾段重叠，不发布独占宿主吞吐结论。

### 后台迁移拒绝与采集环境约束

第一份Zstd 10M正式FST为3,098,687,456 bytes，warmup141.66秒，整体1142.47秒。它确实达到共同终点，但任务compiler拒绝：`task10: migration across physical cores lacks a certified ownership order`。此处10是OS PID，不是producer的task_id=10或应用第十个线程；源事件确认四个工作副本没有迁移，后台PID10在core1和core2执行，缺少可移植的跨核handoff顺序。

未删除调度事件、未把背景PID改成其他ID、未借oracle时间补齐功能依赖，也未放宽Python/C++固定核心限制。该批Zstd不作为通过验证的数据。源日志、oracle、失败收据和checkpoint归档于`diagnostic/zstd-background-migration/`；被替代的正式/短窗口及header FST按哈希清理，见`zstd-migration-fst-retirement.json`。

重采Zstd新增显式`--housekeeping-cpu 0`：guest在启动副本前约束全局unbound workqueue及可调整亲和性的后台任务到CPU0。固有per-CPU任务保留内核亲和性，所有OS指令仍参与物理trace；worker0与后台任务共享CPU0，另三个worker分别绑定CPU1..3，不增加未追踪的第五核。真实guest日志确认`pinned tid=10 comm=rcu_sched cpu=0`。该约束是本次采集对当前模型限制的适配，不是一般迁移建模修复。SQLite保持已通过的原始OS策略，二者OS亲和性差异随各自request和checkpoint身份披露，不将它们当作同一负载的OS消融。

创建fresh checkpoint，短窗口新增维护thread-state compiler检查，再进入正式窗口。新亲和性helper的真实子进程测试确认只修改所选proc树内任务、host父进程亲和性不变；全量453项无断言失败，仍为3个既有环境错误、5 skips。

新短窗口实际 warmup136.47秒，通过严格任务图编译，active-cycle owner可用。亲和性设置发生在保存 checkpoint 之前，因此恢复日志不应重复包含设置命令；门禁核验创建阶段UART、创建request和checkpoint的`guest_setup`身份，并保存`housekeeping-attestation.json`。这不替代正式全窗口的迁移及owner审计。

### 多进程地址空间边界

rate四副本有不同CR3；FST及`.asmap`保留实际地址空间身份，不能把四副本当作一个共享地址空间。当前初始PTE快照仍只覆盖checkpoint marker进程的根，producer对其他CR3明确输出unknown，而不拼接不同进程页表。这是[既有初始PTE模型](gem5-initial-pte-page-fault-model-2026-08-17.md)的限制；新日志已实际观察到不同副本的non-target CR3提示。后续准确性结论须披露该缺项，不能凭cache/owner审计通过声称多进程初始PTE资格完整。

## 最终采集与回放结果

本节保留首次采集的历史结果：其中Zstd实际是结果缓冲区清零窗口，并非压缩主循环。后续ROI修复见文末，不将该窗口误标为有效压缩采样。

两案均完成 KVM → 计算入口 → 有界 O3 warmup → stat → 四核共同终点。独立硬件身份审计为2案、0不匹配；维护 native validator 的2案来源/回放/人口门禁均通过。`passed` 不表示误差小于10%，也不表示真机perf认证。

| 负载 | warmup宿主秒 | 全程宿主秒 | 正式FST GB | checkpoint MB | measured user UOP总量 |
|---|---:|---:|---:|---:|---:|
| 708.sqlite_r | 56.24 | 423.83 | 3.037 | 87.01 | 39,879,196 |
| 777.zstd_r | 135.16 | 1131.69 | 3.111 | 271.94 | 39,719,074 |

GB/MB使用十进制。最终全项目FST共14.438 GB，空闲568.2 GB；小于100 GB上限。保留当前正式源、恢复门禁及小型回归FST，旧失败/被替代FST已按收据清理。

### Active CPI 与 elapsed

scope为`workload-task`，两个指令人口均匹配；active cycle采用`task-oncpu-switch-entry-v1`，elapsed为共同窗口运行时间。以下只转录维护validator结果，没有另选oracle或重新拟合参数。

| 负载 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro | gem5 elapsed ms | FastSim elapsed ms | elapsed相对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 708.sqlite_r | 1.000286 | 0.772986 | -22.72% | 0.227301 | 1.968546 | 1.521277 | -22.72% |
| 777.zstd_r | 4.120792 | 11.405838 | +176.79% | 7.285046 | 6.837854 | 18.922499 | +176.73% |

两案CPI MAE：**3.756173 cycles/macro**。本轮只修复采集/恢复流程并使用既有模型回放，不能称为CPI模型修复或新独占宿主吞吐测量。

### 重点 cache 指标的暂定误差

以下为retired-load服务层级估计，硬件等价性未验证。`retired_l2_load_misses`并非旧request级`perf_l2_demand_misses`的同义字段；Ruby tag misses另列诊断。unknown列是未确定服务资格的候选人口，不能直接加到miss计数，也不能因为已知子集误差较小就忽略。

| 负载 | 事件 | gem5绝对计数 | FastSim绝对计数 | 相对误差 | native unknown | model unknown |
|---|---|---:|---:|---:|---:|---:|
| 708.sqlite_r | retired_l1_load_misses | 71,979 | 74,513 | +3.52% | 581,748 | 2,567,543 |
| 708.sqlite_r | retired_l2_load_misses | 71,330 | 73,808 | +3.47% | 589,108 | 2,567,543 |
| 708.sqlite_r | retired_l3_load_misses | 71,140 | 73,603 | +3.46% | 589,122 | 2,567,564 |
| 777.zstd_r | retired_l1_load_misses | 2,770 | 2,896 | +4.55% | 2,011 | 10,278 |
| 777.zstd_r | retired_l2_load_misses | 2,744 | 2,896 | +5.54% | 2,339 | 10,278 |
| 777.zstd_r | retired_l3_load_misses | 44 | 37 | -15.91% | 2,367 | 10,289 |

Zstd 当前窗口明显以写入为主：已知 retired loads 为22,488、retired stores 为11,361,522；它是第一轮压缩入口的窗口，尚不能证明代表稳态压缩。尤其 LLC retired-load miss 仅44次，7次计数差就产生15.91%误差，不能作为稳定的LLC精度结论。SQLite的对应miss为71,140次。这一限制需与unknown资格分开披露；后续若要认证Zstd稳态cache精度，应进一步定位上下文准备后的计算区间，而不是仅扩大原输入或用较小的L1/L2百分比代替资格检查。

完整hit、FB-hit、retired load/store及MAPE/WAPE见[维护retired-cache报告](../tmp/spec-rate-sqlite-zstd-20261016/validation/retired-cache-summary.md)；CPI、elapsed和native诊断见[维护汇总](../tmp/spec-rate-sqlite-zstd-20261016/validation/summary.md)。保留unknown、错误路径/预取间接影响、初始多CR3页表及DRAM/Ruby近似等证据边界，不声明全部PMU已对齐真机。

### 复用与证据入口

- checkpoint目录：`tmp/spec-rate-sqlite-zstd-20261016/checkpoints/c04-708.sqlite_r/`、`.../c04-777.zstd_r/`；载荷哈希、runner身份和真实恢复门禁见[checkpoint审计](../tmp/spec-rate-sqlite-zstd-20261016/checkpoint-final-audit.json)。
- [冻结命令](../tmp/spec-rate-sqlite-zstd-20261016/campaign-commands.json)和[再次恢复采集脚本](../tmp/spec-rate-sqlite-zstd-20261016/checkpoint-reuse.sh)绑定各案runner，新的`repeat-capture/`输出目录存在时拒绝覆盖；复用脚本只生成并检查语法，没有额外重复采集。
- [完成收据](../tmp/spec-rate-sqlite-zstd-20261016/completion.json)保留各案采集、共同终点、checkpoint、CPI/elapsed及空间快照；正式源的`parameter-provenance.json`披露DRAM配置映射及近似。
- checkpoint节省boot/staging，恢复后仍有KVM初始化和O3 warmup。更改gem5、runner、输入、硬件或guest设置后应新建checkpoint，不改写旧receipt。

## CPI 大误差追加诊断（2026-10-16）

**结论：Zstd采到了清零准备阶段，且该阶段触发了buffer TSO权限准备门禁的强悲观偏差；不能将+176.79%直接归结为指令数量小或固定冷启动。SQLite的低估在窗口扩大100倍后保持稳定；response branch recovery干预弥补约42.5%的少算周期，仍有残差。** 两案存在不同误差机制，不能用同一项参数统一调回。

本轮不改生产模型、不新采gem5、不新增FST；所有完整窗口变体沿用原FST/前史/共同终点及各案冻结配置，只覆盖表中实验开关，通过维护validator回放和来源/人口检查。八项完整窗口干预均通过；没有把这些反事实配置升级为目标参数对齐或真机认证，也不报告并发诊断的吞吐。输入与输出索引见[诊断证据](../tmp/rate-cpi-diagnosis-20261016/evidence.json)。

### 1. 人口、warmup与调度检查

- 两案workload-task宏指令双方精确匹配：SQLite23,635,778，Zstd19,927,209。FastSim实际消费的warmup宏指令分别4,287,680、4,302,303，保留缓存/预测器/依赖前史，统计清零不重建冷模型。并未把10M user UOP误当成10M宏指令。
- 采集几何为四核、八通道、3GiB、buffer TSO114，独立identity两案0不匹配；这不代表所有Ruby/controller/DTLB近似已经严格等价。
- 双方active owner可用、unknown cycles为0。测量期显式依赖等待计数为0；没有证据将主差异归为新增同步等待。scheduler仍会影响分块和前史，不能将其所有间接影响一并排除。
- 初始多CR3 PTE资格仍不完整；当前初始PTE/ROI-entry缺页合成模型关闭，不能凭该缺口直接认定多出了这些周期。

### 2. 短窗口与长窗口：固定冷启动不能解释主要偏差

利用已有恢复门禁的100K stat窗口，与10M正式窗口对照；各自保持100万请求warmup预算，同一负载恢复自同一checkpoint，但它们是独立运行，实际warmup和OS交错略有不同，并非逐记录相同前缀。因此只作诊断，不能替代严格收敛扫描。

| 负载 | 每最快核stat user-UOP目标 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|---:|
| 708.sqlite_r | 100,000 | 1.005315 | 0.773199 | -23.09% | 0.232116 |
| 708.sqlite_r | 10,000,000 | 1.000286 | 0.772986 | -22.72% | 0.227301 |
| 777.zstd_r | 100,000 | 2.395396 | 9.551983 | +298.76% | 7.156587 |
| 777.zstd_r | 10,000,000 | 4.120792 | 11.405838 | +176.79% | 7.285046 |

两份100K源的CPI局部门禁人口匹配、active可评分，但**整体FS验证为failed**：测量窗口没有kernel records/memory，触发`kernel_records_present`和`kernel_memory_present`。不将其PMU或整体来源作为通过样本，不取消门禁。10M两案正式CPI MAE为3.756173 cycles/macro。
窗口扩大100倍后，Zstd绝对CPI差仍约7.2 cycles/macro，SQLite约0.23；与“主要是一笔固定启动费用、增大分母即可摊薄”的解释不符。尚未独立改变warmup长度，不能量出冷启动影响上限或声称已证明预热收敛。

### 3. Zstd ROI过早：实际在BMK的memset中

标记位于`programs/benchzstd.c`的`while (!(compressionCompleted && decompressionCompleted))`之前，下一层`BMK_benchTimedFn`进入`programs/benchfn.c:BMK_benchFunction()`。后者先执行`memset(p.dstBuffers[i], 0xE5, p.dstCapacities[i])`，之后才调用initFn和真实benchFn。原“第一轮压缩调用入口”并未跳过这段准备，上一版对标记位置的判断不充分。
完整扫描core0测量FST并解析二进制符号：9,848,689个物理UOP中至少9,776,487个属于`__memset_sse2_unaligned_erms`，占99.2669%。十等分区间的主PC始终为0x4b6f9d，窗口末尾仍是清零循环；不是只在最开头出现。对应循环为四条16B `movdqa`、加地址、比较、跳转。其他核的workload-task退休人口合计只有22,488 loads、11,361,522 stores，同样显示store主导。[原始PC/分段统计](../tmp/rate-cpi-diagnosis-20261016/phase-profile.json)明确标为物理core0 UOP，不能与退休宏人口混用。
因此当前源应标为“压缩benchmark结果缓冲区清零窗口”，不能代表稳态压缩。更大stat可能最终进入压缩，但会混合不同阶段；优先应将标记定位到结果缓冲区清零、上下文准备之后的实际计算区间，并将初始化阶段单独报告。保留官方输入、命令与算法，不用额外重复计算伪造warmup。

### 4. Zstd同源干预：store门禁与翻译时序强耦合

| 模式 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| 基线 | 4.120792 | 11.405838 | +176.79% | 7.285046 |
| 仅LRU store驻留证书 | 4.120792 | 4.365992 | +5.95% | 0.245200 |
| 仅合并同页未完成walk | 4.120792 | 61.634999 | +1395.71% | 57.514207 |
| 去掉walk时序，SE翻译反事实 | 4.120792 | 63.880109 | +1450.19% | 59.759317 |
| 驻留证书+同页walk合并 | 4.120792 | 8.685393 | +110.77% | 4.564601 |

基线gem5 active cycles为82,115,877.88，FastSim为227,286,508；证书候选为87,002,028。单开关完整warmup+stat干预减少140,284,480周期，相当于原超额周期的96.63%。这包含前史和测量期传播，不能称为已逐store量出相同百分比的测量期错误。
代码路径为`prepare_buffer_tso_epoch()`的同set后缀预筛选 → `buffered_tso_permission_prepare()` → `regular_store_response_cycle()` → FIFO store drain/SQ反压。默认门禁在epoch后面出现另一条同组line就可能禁止prepare；LRU证书以实际相联度下的不同line数量证明驻留，保留准备并行性。拒绝prepare后普通store重新承担完整服务并进入串行下界。该路径及历史边界见[9月21日诊断](lbm-buffer-tso-error-mechanism-20260921.md)，本次是新Zstd源上的独立干预证据。
这是驻留门禁局部修复有效的证据。另开DTLB同页合并后误差回到+110.77%，只说明组合模型存在强耦合，不能据此否定LRU修复，也不足以证明+5.95%来自误差抵消。DTLB合并是诊断反事实，不是开启证书的前提；本次gem5的x86 walker仍将未完成请求排队，其源码明确将coalescing列为TODO，不能把合并后的FastSim直接当作同一目标的修正版。原DTLB时序miss为2,130,063，功能性miss为45,741；合并后时序miss变为45,741、累计walk等待从615,056,373降到14,926,944，整体反而更慢。累计等待可重叠，不能相加为CPI归因。
翻译改变访存到达节奏，可能改变epoch批次的同set准备资格与store串行链；这条传播链尚需本案逐epoch拒绝计数/逐请求native时序配对。证书是否默认启用，应在DTLB及其他目标参数不变的对照下决定，不能用另一实验开关的退化代替判断。历史未推广的直接依据是2026-09-21 Graph500 C4对照回退（见链接文档的“非LBM控制与停止条件”），以及prepare/publish生命周期的剩余边界；这不等于旧的过保守门禁正确，也不应成为长期搁置局部修复的理由。

### 5. 为什么retired-load miss接近而CPI仍崩

本案retired-load L2 miss为gem5 2,744、FastSim 2,896（+5.54%），仅覆盖退休load服务层级；它不包含这1136万条retired stores的完整时序。独立native cache诊断则是L1D tag miss 296,239→629,336，private-L2 tag miss 2,976→629,336。两个指标计数域不同，不能相互替代；load-only指标较接近不说明store权限、合并和服务响应准确。LLC retired-load miss仅44次，还受小样本波动影响。

### 6. SQLite需要单独解释

| 模式 | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| 基线 | 1.000286 | 0.772986 | -22.72% | 0.227301 |
| 去掉walk时序 | 1.000286 | 0.771851 | -22.84% | 0.228436 |
| 关闭store forwarding | 1.000286 | 0.773423 | -22.68% | 0.226864 |
| 仅LRU store驻留证书 | 1.000286 | 0.772641 | -22.76% | 0.227645 |
| 开启已有response branch recovery | 1.000286 | 0.869519 | -13.07% | 0.130768 |

SQLite PC分段始终由`sqlite3VdbeExec`执行循环主导，十段load/store人口相近，未出现Zstd式memset采偏。TLB、转发、store门禁的上述干预影响很小，不能拿Zstd根因套用到SQLite。branch-response干预则增加2,281,635 active cycles，弥补原5,372,430.88周期缺口的42.47%。当前baseline的`core.response_branch_recovery=false`；候选在分支完成被响应依赖推迟时更新recovery frontier，并约束后续fetch不得早于该frontier。候选物理计数中137,126次misprediction有130,143次被响应推迟；累计延迟存在重叠，不能相加当作净CPI。它证明该反馈链影响显著，尚不证明候选每个分支时序已经与gem5逐条一致。
后续应对SQLite采集/配对少量native load issue→ready、branch resolve→fetch恢复、依赖链和commit间隙，定位第一个时间分歧；基线少算约537万active cycles，branch-response干预弥补约228万，仍少约309万；仅当前计数无法把剩余周期精确分配给组件。不调branch penalty、cache latency或经验常数以拟合CPI。

### 7. 修复顺序与证据边界

1. 修正Zstd计算ROI，验证实际压缩函数的PC人口后再作规模/预热收敛实验。当前源保留为store清零压力诊断，不能用重命名当作已采到压缩。
2. 对buffer TSO记录按warmup/stat分开的eligible/prepare拒绝原因、LRU驻留、FIFO send/response和SQ释放；配对少量gem5 native store时序，修补有证据的保守门禁及两阶段传递。保持114和真实DRAM约束，做SQLite/Graph500等控制，防止局部改善来自抵消。
3. 在同一正确计算阶段做warmup预算扫描及stat长度扫描，要求CPI、miss/1K-inst与代码阶段稳定；不能用宿主warmup秒数或仅增加输入尺寸证明收敛。
4. SQLite单独定位响应/依赖/branch反馈，保留尚未解释的误差；本轮没有完成生产模型修复或正式吞吐比较。

## Zstd ROI修复实施（2026-10-16）

初版把输入装载、校验及benchmark参数准备完成误认为实际计算入口，未检查下一层`BMK_benchFunction`仍有整块结果缓冲区`memset`；短采集只检查恢复、边界和owner，未检查热点PC。这是选点及采集验收的缺陷，不归因于用户输入规模。

修复补丁为[spec2026-zstd-rate-compute-roi.patch](../patches/spec2026-zstd-rate-compute-roi.patch)。原`benchzstd.c`标记移除，仅在`ZSTD_compress_frameChunk`内部、checksum之后且`remaining>0`时调用一次rate gate，再进入实际block循环。调用链已审核：benchmark清零 → initFn → `ZSTD_compress2` → CCtx/table初始化 → frame/header准备 → 新标记 → block匹配及编码。不增加预跑轮次，不修改算法、官方输入、压缩级别、重复次数或单线程语义。

独立实验目录为`tmp/zstd-compute-roi-20261016/`，只重建Zstd，SQLite源、二进制及结果保留。新标记版由SPEC harness构建；四副本完整官方命令输出均与参考逐字节相同，4×单线程、零swap，新入口总RSS为1,024,024,576 bytes。硬件、TSO、DTLB及FastSim默认参数保持原样。二进制改变后保存fresh实际checkpoint，禁止复用含旧程序的RAM快照或改写旧身份。

新增[阶段检查器](../tools/validate_roi_phase.py)：从每核完整FST测量区间解析物理retired-user UOP，以对应二进制的DWARF内联调用链核对代码阶段；记录binary、validator及源FST的SHA256。每核及其四个测量分段均要求压缩代码占比≥50%、setup占比≤10%；warmup独立报告，不用其压缩代码替代stat资格。上下文reset/init、workspace准备及memset优先归setup，不能因位于`lib/compress/`目录就算压缩。此比例是拒绝明显采偏的门禁，不是精度阈值或稳态收敛证明；owner、共同终点、native PMU及CPI另行验证。

旧Zstd短窗口实际四核均为100%清零、0%压缩，被新阶段检查拒绝；6项回归测试通过。独立审核发现并已修复“上下文初始化被误算压缩”的漏洞，并用新ELF三处真实PC复核；无剩余阻断项。新窗口先完成恢复、阶段及任务图门禁，通过后才采正式10M窗口。保持100万请求warmup预算和180秒上限，不在阶段不合格时自动扩大窗口或放宽阈值。

当前进度：新checkpoint恢复、短窗口及正式全窗口阶段检查、来源/人口验证和两种FastSim回放均完成，结果如下。维护C++构建及测试通过；Python459项仍有3个既有环境错误、5 skips，不能表述为全量通过。

### 修复后的正式采集与验证结果

正式采集已完成，warmup 66.92秒，全程589.14秒；正式FST 3.097 GB，measured user UOP总量39,684,929。实际checkpoint 271.34 MB，载荷及冻结runner哈希复核通过，真实恢复短窗口通过。

| 核 | measured source user UOP | 压缩代码占比 | setup占比 | 四分段门禁 |
|---|---:|---:|---:|---|
| 0 | 10,000,000 | 92.898% | 0.043% | 通过 |
| 1 | 9,898,969 | 92.825% | 0.043% | 通过 |
| 2 | 9,873,211 | 92.806% | 0.043% | 通过 |
| 3 | 9,912,749 | 92.835% | 0.043% | 通过 |

阶段检查、严格任务图、active owner、共同终点、独立目标身份及native来源/人口门禁通过。这里的阶段占比按物理FST中retired user记录统计，不替代task宏人口；对含syscall替代记录的模型UOP扩展不作硬件UOP认证。压缩阶段资格通过不等于已经证明预热收敛。

| 模式 | gem5 active CPI | FastSim active CPI | 相对误差 | 绝对误差 cycles/macro | gem5 elapsed ms | FastSim elapsed ms | elapsed相对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 原配置，证书关闭 | 1.886090 | 1.073158 | -43.10% | 0.812932 | 3.294098 | 1.874343 | -43.10% |
| 仅开启LRU驻留证书 | 1.886090 | 0.987177 | -47.66% | 0.898913 | 3.294098 | 1.724194 | -47.66% |

新Zstd原配置＋既有SQLite两案CPI MAE为0.520117 cycles/macro。SQLite未重采或重跑。两种Zstd模式均保持DTLB原配置；证书仍是单独候选，没有更改生产默认，也没有按误差调延迟。旧清零窗口与新压缩窗口的指令人口不同，不能将两者CPI变化解释为同一指令流的模型修复收益。

| 模式 | retired-load miss事件 | gem5计数 | FastSim计数 | 相对误差 | native unknown | model unknown |
|---|---|---:|---:|---:|---:|---:|
| baseline | retired_l1_load_misses | 582,665 | 711,863 | +22.17% | 236,922 | 547,420 |
| baseline | retired_l2_load_misses | 154,805 | 178,570 | +15.35% | 348,000 | 547,429 |
| baseline | retired_l3_load_misses | 29,207 | 45,004 | +54.09% | 348,215 | 547,576 |
| certificate | retired_l1_load_misses | 582,665 | 711,859 | +22.17% | 236,922 | 547,420 |
| certificate | retired_l2_load_misses | 154,805 | 178,569 | +15.35% | 348,000 | 547,429 |
| certificate | retired_l3_load_misses | 29,207 | 45,004 | +54.09% | 348,215 | 547,577 |

PMU仍为证据不完整的retired-load估计，硬件perf等价性未认证；native tag-miss诊断与退休事件分开。初始多CR3页表、错误路径间接影响及Ruby/DRAM等效时序近似继续披露。完整绝对计数、MAPE/WAPE、CPI与elapsed分别见[原配置报告](../tmp/zstd-compute-roi-20261016/validation-baseline/summary.md)、[原配置retired-cache](../tmp/zstd-compute-roi-20261016/validation-baseline/retired-cache-summary.md)、[证书候选报告](../tmp/zstd-compute-roi-20261016/validation-certificate/summary.md)。
当前全项目FST 14.377 GB，空闲567.4 GB，未触及100 GB上限。不报告本次并发诊断的正式吞吐。

新源全部验证后，旧Zstd错误ROI的正式及恢复窗口共8个FST、3,695,002,496 bytes已按逐文件SHA/inode/mtime收据删除；SQLite和新源未删。旧checkpoint、二进制、oracle、日志和历史报告保留，旧目录写入`source-retirement.json`，不再作为可重放压缩输入。见[旧FST退役收据](../tmp/zstd-compute-roi-20261016/old-roi-fst-retirement.json)。

证据入口：[完成收据](../tmp/zstd-compute-roi-20261016/completion.json)、[正式窗口阶段检查](../tmp/zstd-compute-roi-20261016/source/c04-777.zstd_r/roi-phase-validation.json)、[原始源码身份](../tmp/zstd-compute-roi-20261016/original-integrity.json)、[checkpoint核验](../tmp/zstd-compute-roi-20261016/checkpoint-final-audit.json)、[当前两负载维护汇总](../tmp/zstd-compute-roi-20261016/validation-latest/summary.md)。

### 新压缩ROI仍低估43%的定位

本次沿用已接受的新压缩源、完整warmup和共同终点，没有重采FST、改变生产配置或调节延迟常数。目标身份重新核验为1案有效、0不匹配；新增两组单变量控制均经维护native验证器通过来源/人口检查。该`passed`不表示误差达标或硬件认证。

两侧workload-task宏分母均为20,976,471，gem5 active为39,563,516.879880 cycles，FastSim baseline为22,511,064，少17,052,452.879880 cycles。owner unknown为0。差距是时序预测问题，不能用宏/UOP分母替换、active/elapsed转换来消除。旧清零窗口和新压缩窗口人口不同，旧窗口证书开启后约6%的误差不能外推到新窗口。

| 同一新源上的控制 | gem5 active CPI | FastSim active CPI | 相对误差 | 绝对误差 cycles/macro | 相对baseline增加的active cycles |
|---|---:|---:|---:|---:|---:|
| baseline | 1.886090 | 1.073158 | -43.10% | 0.812932 | 0 |
| 仅开启分支response recovery | 1.886090 | 1.110540 | -41.12% | 0.775551 | 784,138 |
| 仅关闭store forwarding | 1.886090 | 1.075494 | -42.98% | 0.810596 | 49,005 |
| 仅开启LRU驻留证书，复用已有结果 | 1.886090 | 0.987177 | -47.66% | 0.898913 | -1,803,577 |

这些是同一负载的诊断控制，不作为四个独立工作负载聚合。分支控制弥补原active周期缺口的4.60%，关闭转发仅弥补0.29%，不能据此解释主要缺口。分支miss计数baseline为183,839，对gem5 184,411仅低0.31%，说明预测错次数接近不等于错分支的恢复时序正确。证书减少store等待，在本来低估周期的新窗口上使误差扩大；这不是证书机制错误或正确的单独判据。

#### 已发现的gem5参考时序问题：MOVNTDQ调试fault

当前gem5的`MOVNTDQ`宏在正常store成员之前包含无条件`warn_once`。`MicroDebug::execute()`每次返回fault；`M5DebugOnceFault::invoke()`的once仅限制日志打印，仍然每次推进微PC并走O3 fault路径。因此日志只打印一次，不表示只付出一次trap代价。源码链为：

- `gem5-fs/src/arch/x86/isa/insts/simd128/integer/data_transfer/move_non_temporal.py:37`：MOVNTDQ无条件warning前缀，后续按cacheable store处理；
- `gem5-fs/src/arch/x86/insts/microdebug.hh:51`：execute无条件返回fault；
- `gem5-fs/src/arch/generic/debugfaults.hh:114`：once限制debugFunc，advancePC仍每次执行；
- `gem5-fs/src/cpu/o3/commit.cc:1217`：fault先等待旧store排空，随后生成trap；`:473`使用目标`trapLatency=13`；`:773`处理trap squash。

对新源静态反汇编与FST计数，并独立核对功能macro-boundary sidecar：四核测量期各有85,404个MOVNTDQ debug前缀，共341,616个。退休MOVNTDQ宏为85,410/85,410/85,411/85,410，与debug-prefix入口数差6/6/7/6，不能将宏完成与前缀进入强行认作严格一一对应。未发现其他PC的测量期debug前缀。

native frontend在共同统计窗口记录trap-pending为1,196,336/1,196,131/1,196,256/1,196,094 cycles，合计4,784,817，占物理测量周期39,568,490的12.0925%。以每次13周期trap加状态采样/恢复一拍做源码机制交叉核查，85,404×14=1,195,656，与每核观测高度接近；不是通过CPI拟合得到14周期参数，也不是逐fault时间归因。页故障、IRQ及其他回放仍可能占余量；功能前缀不记录逐fault种类和时序。

这证明参考执行包含大量调试fault开销，强烈指向MOVNTDQ是trap等待的主因。该开销不是x86非临时store应当产生的架构异常。更大的影响还包括旧store排空、流水线清空、重新取指和并行度下降；这些不全部落在trap-pending状态内，因此12.09%既不是总CPI影响的精确贡献，也不能当作43%误差的完整解释。native frontend另记录9,888,501个IcacheWaitResponse周期，但与模型前端账本人口/重叠不同，禁止直接相减后当作另一项独立缺失周期。

下一步应先对gem5的无条件调试warning做不触发O3 trap的受控修复，保留数据访存、地址、尺寸、真实架构fault和SPEC参数；从同一checkpoint恢复重采新源，验证输出、宏/内存语义及共同窗口，再由维护验证器重放。新执行的自然共同终点可能改变，必须分别核验人口，不能声称与旧动态trace逐指令相同。Gem5仍将NT提示按cacheable处理，这一硬件近似需要继续披露，移除调试trap并不等于实现真机NT写策略。不能向FastSim加入每条MOVNTDQ固定trap罚时以追平当前参考，也不能从旧oracle中减去trap-pending周期后宣称修复完成。

当前只能确认上述参考缺陷和两个控制的净影响，尚未完成无调试fault的gem5 A/B，因此剩余误差的确切分解未证实。新源已经保留8,050,571个物理warmup记录、测量约3,968万user UOP，不能仅凭“窗口短”断言冷启动是主因；预热收敛仍未验证。Cache miss估计偏高与CPI偏低并存，同样不能仅靠miss计数确定响应延迟或重叠是否正确。

本轮诊断证据：[汇总JSON](../tmp/zstd-compute-cpi-diagnosis-20261016/analysis.json)、[MOVNTDQ功能/前端交叉核查](../tmp/zstd-compute-cpi-diagnosis-20261016/nt-evidence.json)、[分支控制报告](../tmp/zstd-compute-cpi-diagnosis-20261016/branch-response/validation/summary.md)、[转发控制报告](../tmp/zstd-compute-cpi-diagnosis-20261016/forwarding-off/validation/summary.md)、[独立目标身份](../tmp/zstd-compute-cpi-diagnosis-20261016/identity-audit.json)。没有新增FST，也不报告并发诊断的正式吞吐。

### 非临时store调试trap修复

用户确认“先修复”后，移除MOVNTDQ/MOVNTQ/MOVNTI/MOVNTPS/MOVNTPD共10种寄存器间接及RIP-relative形式的无条件`warn_once`前缀。四个ISA文件的其他非注释成员逐行对照保持一致，保留`rdip`、`cda`、store的地址、宽度及顺序；没有改变通用MicroDebug/DebugFault、O3 fault处理、FastSim参数或SPEC二进制。NT提示继续按cacheable处理，仍不是完整的硬件非临时写策略。可重放修改见[gem5补丁](../patches/gem5-ntstore-debug-trap.patch)。

新增真实编译产物上的O3 SE探针，覆盖全部10种形式。两类寻址写入不同64B缓冲区，XMM高低lane使用不同值，逐项检查16个64-bit结果，避免某种形式的正确写入掩盖另一种形式的错误。

| 集成探针 | 原gem5 | 修复gem5 |
|---|---:|---:|
| 2048轮×10条NT store的数据结果 | 通过 | 通过 |
| O3 commit trapPending cycles | 286,720 | 0 |
| MOVNTDQ寄存器间接坏地址产生真实fault | 通过 | 通过 |

旧版在数据检查通过后，仅因非零trap周期失败，形成RED；新版两个集成测试均通过，形成GREEN。零值直接从gem5 `VectorInfo`读取，避免把文本统计因`nozero`省略的行误当作缺失或推断零。动态坏地址检查仅覆盖MOVNTDQ_M，其余形式的保留依据为非warning成员不变审计，不能宣称全部异常已逐条动态认证。独立审查发现并修复了探针输出区互相覆盖的问题，最终无重要遗留意见。

gem5在192个可用CPU上以`-j153`构建，显式指定Python3.11及`TAOGEN_SHARED`；修复版SHA256为`69dadef9d5725a244ed344103e96df315b2795681c35f36c8f193d42de4ca3f5`。FastSim默认构建及直接测试、6项ROI阶段测试、宏边界扫描和checkpoint专项测试通过。首次构建缺少共享头路径、首次GREEN检查未处理文本零值省略，失败日志保留；补齐构建环境、改为读实际VectorInfo后重新验证，没有放宽trap=0要求。

旧checkpoint严格绑定旧gem5哈希，未改写旧收据或绕过验证。为修复版重新创建相同的staged pre-fork checkpoint，随后执行恢复短窗口及正式窗口，保持原SPEC参数、四个单线程rate副本、8通道、buffer TSO114、原压缩marker、100万指令warmup预算及最快核1000万user UOP共同终点。修复后自然执行时序与共同终点可能改变，必须使用新oracle和新人口，不与旧trace混用。

修复证据：[变更范围与旧身份](../tmp/zstd-ntstore-repair-20261016/repair-plan.json)、[非warning成员不变审计](../tmp/zstd-ntstore-repair-20261016/semantic-members-audit.json)、[RED日志](../tmp/zstd-ntstore-repair-20261016/red-final-tests.log)、[GREEN日志](../tmp/zstd-ntstore-repair-20261016/green-tests.log)、[实际零值统计](../tmp/zstd-ntstore-repair-20261016/green/normal-gem5/ntstore-result.json)、[独立审查](../tmp/zstd-ntstore-repair-20261016/review.json)。

#### 修复版Zstd正式复验结果

修复版实际checkpoint为271,315,975 bytes，绑定新gem5哈希；恢复短窗口通过。正式采集504.12秒，其中O3 warmup为64.35秒，正式FST 3,072,093,856 bytes。四核measured user UOP分别为9,822,941 / 9,912,159 / 10,000,003 / 9,834,725，总量39,569,828；最快核2关闭共同窗口，保留宏边界自然超出3个UOP。压缩代码占比92.775%—92.898%，setup 0.038%—0.043%，逐核四分段门禁通过。

新源四核测量期debug前缀均为0。native物理trap-pending合计从旧源4,784,817降至2,115 cycles；剩余真实fault/IRQ/replay来源没有逐项配对归因，不强行清零。新源通过目标身份、任务归属、共同终点、native来源和逐核人口检查。两边用于active CPI的workload-task宏分母均为21,001,415，FastSim owner unknown cycles为0。

| 采集版本与FastSim配置 | gem5 active CPI | FastSim active CPI | 相对误差 | 绝对误差 cycles/macro |
|---|---:|---:|---:|---:|
| 修复前压缩ROI，默认配置，历史对照 | 1.886090 | 1.073158 | -43.10% | 0.812932 |
| 修复后新采窗口，默认配置 | 1.500908 | 1.083942 | -27.78% | 0.416967 |
| 修复后同一新源，仅开启LRU证书 | 1.500908 | 1.001608 | -33.27% | 0.499300 |

新默认报告只有1案，CPI MAE为0.416967 cycles/macro；证书是同源候选，不能与默认配置作为两个负载合并。修复前后是自然执行形成的不同窗口，不能将周期差当作逐指令配对的纯模型收益。受控SE探针证明伪trap的因果存在，新源结果证明实际负载中该前缀消失；完整CPI差距仍未解决。

| 修复后模式 | gem5 elapsed ms | FastSim elapsed ms | elapsed相对误差 |
|---|---:|---:|---:|
| 默认配置 | 2.624918 | 1.895842 | -27.78% |
| 仅开启LRU证书 | 2.624918 | 1.751894 | -33.26% |

默认配置的主要retired-load PMU仍为证据不完整的估计，不能升级为真机perf精度：

| 事件 | gem5计数 | FastSim计数 | 相对误差 | native unknown | model unknown |
|---|---:|---:|---:|---:|---:|
| retired_l1_load_misses | 592,286 | 721,635 | +21.84% | 236,777 | 539,462 |
| retired_l2_load_misses | 153,939 | 177,364 | +15.22% | 361,811 | 539,474 |
| retired_l3_load_misses | 29,897 | 45,845 | +53.34% | 362,068 | 539,673 |

独立native tag-miss诊断的APE为L1D 1.30%、private-L2 17.51%、LLC 37.36%，不替代上表退休事件。修复主要消除了参考执行的额外控制开销，没有自动修复cache服务资格、响应时序与重叠模型。分支恢复仍保持默认关闭、DTLB原配置及证书默认关闭；没有按新误差调整参数，也不报告本次并发验证的正式吞吐。SQLite未在本轮重采。

新源验证通过后，旧含NT调试trap的Zstd正式/恢复FST共8个、3,633,399,040 bytes已逐文件复核SHA256/inode/mtime并删除；旧oracle、配置、报告及checkpoint保留，源目录写入退役说明。全项目FST降为14,353,189,960 bytes，空闲约559.5 GB，低于100 GB上限。

最终证据：[完成收据](../tmp/zstd-ntstore-repair-20261016/completion.json)、[默认CPI与native报告](../tmp/zstd-ntstore-repair-20261016/validation-baseline/summary.md)、[默认retired-cache完整报告](../tmp/zstd-ntstore-repair-20261016/validation-baseline/retired-cache-summary.md)、[证书候选报告](../tmp/zstd-ntstore-repair-20261016/validation-certificate/summary.md)、[目标身份](../tmp/zstd-ntstore-repair-20261016/identity-validation.json)、[旧FST退役收据](../tmp/zstd-ntstore-repair-20261016/old-fst-retirement.json)。
