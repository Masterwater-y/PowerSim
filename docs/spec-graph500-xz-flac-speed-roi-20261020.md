# Graph500、XZ、FLAC 官方 speed ROI 接入（2026-10-20）

2026-10-28 存储状态：用户已授权回收本文旧C4 Graph500、XZ firstchunk、FLAC
来源的FST及专属旁表。配置、统计和历史报告保留，旧来源已标记
`retired-not-replayable`，不能再原样回放；当前32GiB正式矩阵另行保留。
删除凭据见[回收收据](../tmp/fst-retirement-20261028/receipt.json)。

用户要求新增三项负载，沿用已确认的采集流程，并限制 warmup 时间。使用本机发行版 `/data00/zhonghaocheng/benchmark/spec_cpu_2026`，原安装只读；独立副本位于 `tmp/spec-speed-gxf-20261020/spec`。复制后、插桩前核对发行 MANIFEST，共497个文件通过 SHA256 与长度验证。

## 输入、线程与 ROI

| 官方身份 | 原生并行方式 | ROI 位置与计量人口 | 本轮输入选择 |
|---|---|---|---|
| `854.graph500_s` | OpenMP，4个线程，含主线程 | BFS 完成树初始化后、遍历前；APSP 完成矩阵/边权初始化后，进入原有 relaxation 工作共享区域 | 采集官方 refspeed 第二条 APSP 命令；BFS 插桩另做正确性验证，不冒充 BFS refspeed trace |
| `801.xz_s` | C++线程，4个压缩 worker，加1个生产者主线程 | 各worker首次编码调用消费至少16KiB输入后；生产者向全部4个worker发布数据、唤醒并释放锁后，共同进入采集 | 官方 refspeed 最小输入有容量阻塞；使用官方 train 第二条40MiB/level4命令采集，单独标明输入规模 |
| `817.flac_s` | C++线程，主线程加3个 worker，共4个 | 既有编码 worker 入口；主线程创建第3个 worker 后，在工作队列锁外共同进入采集 | 官方 refspeed `working_for_your_team1.flac` 编码命令，`-j 4` |

源码补丁：[Graph500](../patches/spec2026-graph500_s-roi.patch)、[XZ](../patches/spec2026-xz_s-roi.patch)、[FLAC](../patches/spec2026-flac_s-roi.patch)。补丁以 SPEC 根目录为基准，使用项目 `workloads/spec_roi_integration` 的采集头文件。它们是带标记的研究二进制，不发布为未修改的 SPEC 成绩。

Graph500 的 APSP `parallel for` 拆成同一 `parallel` 区域内的 `for`，循环索引隐式私有、`j`私有、循环末与区域末屏障保留；没有把整个外层 `k` 循环改成持久并行区域。BFS 标记在原来两个初始化 `omp for` 的隐式屏障之后，不把初始化统计为遍历。

独立审核指出，源码拆分理论上可增加一个冗余隐式屏障。实际检查本轮GCC11原版/标记版 `apsp.o`，双方均仅引用 `GOMP_parallel`、均无 `GOMP_barrier`，当前编译器消除了冗余屏障；该证据仅适用于冻结构建，不为其他编译器作保证。

FLAC 选择官方第一组 `working_for_your_team` 编码设置，保留 `-8Vep --mid-side --replay-gain --keep-foreign-metadata --no-preserve-modtime`。其 WAV 由原版官方二进制按 `control.decode` 解码生成，未截短音频或改变采样率。

XZ 的 SPEC `tuklib_cpucores()` 实际读取 `OMP_NUM_THREADS`，但执行的是 C++线程，不是 OpenMP。因此采集 runner 新增显式 `--native-omp-num-threads`，用于 Graph500 和 XZ；Marian、FLAC 和独立 rate copy 不默认注入该环境。这个选项仅传递官方线程数入口，不替换运行时。

XZ最终入口放在每个worker首次编码调用已消费至少16KiB输入之后；这表示编码器实际执行了LZMA匹配与缓冲工作，不表示16KiB已经全部编码或输出。生产者先发布输入并唤醒全部4个worker，释放锁后再进入共同gate；worker在不持有队列锁的编码调用返回后进入gate。主机完整train命令的原版/标记版输出一致，独立审核未发现该输入上的锁依赖环。入口仅支持本轮单一worker池及足以启动4个worker的输入。旧线程入口版、block-init版二进制与checkpoint另存，不混用身份。

曾把ROI放在block encoder初始化之后，但实际FST表明它仍早于惰性字典触页：统计窗口各核最初100万条物理记录中，内核记录比例为83.27%–99.88%，精确页清零函数占全部记录73.92%–87.19%，表明该窗口被初始化主导。用户态函数占比无法揭示这一问题。最终firstchunk版依据实际执行阶段重采，尚未用旧版XZ的CPI结果挑选ROI。阶段检查同时统计退休user/kernel记录及精确的clear_page函数，flags bit0只表示退休，不能用来判定特权级。参见[旧入口诊断](../tmp/spec-speed-gxf-20261020/xz-first-stat-phase-diagnostic.json)。

## 共同入口与短预热

复用 Marian 的 ready → KVM/O3交接 → 最终 `/proc`名册 → 每任务身份 → 全部确认 → 独立继续通道。主线程参与计算时必须标记，不能仅将 worker 子集报告为整个进程。

`--native-workers` 在此表示被共同接纳的 OS 任务数，可以大于物理核数。XZ 5个任务共享4核，生产者先保留CPU0，第4个 worker也在CPU0，其余worker固定CPU1..3；没有减少官方4个压缩worker。每个任务仍保留独立TID和身份。Graph500、FLAC为4任务/4核。入口只执行一次，不把采集控制误记为应用同步。

流程保持：真实 staged pre-fork checkpoint → 恢复并在KVM下完成初始化/输入准备 → 计算入口切O3 → 请求1,000,000条 core0 instruction-stop warmup → stat → 任一参与核达到10,000,000 measured user UOP时全核共同结束。先做100K最快核短窗口恢复检查。warmup宿主硬上限180秒；总采集时间、KVM准备时间与O3 warmup分别记录。若warmup超限，停止并保留失败证据，不把部分窗口宣布成功。

四核、8通道、3GiB、buffer TSO114；FastSim参数必须从本案 `config.ini` 映射。保留user+kernel流和真实warmup前史。采集后检查身份、共同终点、所有权守恒、逐任务人口和实际执行函数；通过门禁不等于CPI/PMU精度达标。

FLAC原始完整WAV约553MiB，原512MiB tmpfs无法装入。因此新增受限枚举 `--staging-size 2g`，增大暂存容量上限，仍计入3GiB guest物理内存，禁用swap。该参数通过readfile内容哈希绑定checkpoint；已有checkpoint不能隐式混用。

首轮把WAV嵌入readfile，脚本约353MiB，guest在运行脚本前即报 `Failed to write file`，没有进入O3。修复为传入官方压缩音频，新增 `--prepare-workload-args`，在KVM下由同一FLAC二进制按官方decode参数生成完整WAV，再保存staged checkpoint；脚本降至约148MiB并已真实保存checkpoint。准备命令按argv引用，不执行额外shell表达式；readfile及显式guest_setup共同绑定该操作。失败空FST已按哈希收据清理，不能将producer返回0视为checkpoint成功。

FLAC宿主完整refspeed对照已通过：原版与标记版编码文件SHA256相同，编码自带 `-V` 和独立解码完整性检查通过，4个OS线程。两次完整流程各约373/377秒，属于宿主输出正确性验证，非O3 warmup或吞吐测量。Graph500及FLAC已完成的短恢复warmup分别为40.04/32.68秒；FLAC短窗口覆盖回放增益分析、残差/分区预测和官方MD5计算，四个任务均在stat前完成身份，unknown owner cycles为0。正式窗口在此基础上独立审计，不继承短窗口的通过状态。

## 容量边界

XZ官方refspeed有15000MiB与2700MiB两条输入。`spec_init()`会对两个input-size大小的缓冲区逐页触页，最小项在压缩开始前已需要至少 **5,662,310,400 bytes**，还未计入线程字典和OS。当前 `X86Board._setup_memory_ranges()` 因I/O hole显式拒绝超过3GiB。故不能通过选择较早ROI、缩短warmup或增加tmpfs解决；本轮不将缩小输入或swap作为替代。

Graph500官方BFS scale26、edgefactor16，仅输入packed-edge数组便超过3GiB。另一个官方refspeed APSP命令为 `-m 19384 -p 16000 -q apsp_NYroad_19384.bin -g 1`，其中**16000是顶点数、19384是边数**；核心距离矩阵为1,024,000,000 bytes。本轮选择这个完整官方子命令，不能外推为整个Graph500套件或BFS随机访存行为。

XZ官方train第二条命令40MiB、level4有足够压缩块产生4个worker。最初尝试train第一条40MiB、level8时，其输入不足一个96MiB块，不能验证4个惰性创建worker；该探测已停止并保留诊断。不得让小输入探针等待不存在的worker，必须按实际分块/线程创建情况配置入口任务数。

## 验证与证据入口

- [实验计划](../tmp/spec-speed-gxf-20261020/plan.md)、[发行来源核对](../tmp/spec-speed-gxf-20261020/original-source-identity.json)、[冻结二进制与脚本身份](../tmp/spec-speed-gxf-20261020/frozen-identity.json)。
- [三项采集命令及明确输入规模](../tmp/spec-speed-gxf-20261020/campaign-commands-with-train.json)、[初始输入哈希（FLAC WAV方案已替换）](../tmp/spec-speed-gxf-20261020/input-archives.json)、[FLAC最终输入与准备身份](../tmp/spec-speed-gxf-20261020/flac-preparation-identity.json)、[XZ firstchunk二进制及输入身份](../tmp/spec-speed-gxf-20261020/xz-firstchunk-frozen-identity.json)、[采集进度](../tmp/spec-speed-gxf-20261020/campaign-status.json)、[XZ refspeed容量阻塞记录](../tmp/spec-speed-gxf-20261020/capacity-blocked.json)。
- [XZ train最终firstchunk版正确性](../tmp/spec-speed-gxf-20261020/host/801.xz_s-firstchunk/correctness.json)：原版与标记版stdout完全一致，含压缩长度范围及解压SHA校验；5个OS任务均通过共同身份入口。不是refspeed或真机性能认证。
- [Graph500 APSP完整refspeed子命令正确性](../tmp/spec-speed-gxf-20261020/host/854.graph500_s/correctness.json)：原版与标记版完整距离矩阵SHA256一致，各自通过官方Dijkstra结果校验；四线程，宿主时间不作吞吐测量。
- [Graph500 BFS官方test输入正确性](../tmp/spec-speed-gxf-20261020/host/graph500-bfs-test/correctness.json)：按官方 `control.prep` 生成数据后，原版与标记版输出一致，四线程入口完成。发行包中的原始BFS文件不能代替harness指定的数据生成步骤。
- [独立审核和GOMP对象证据](../tmp/spec-speed-gxf-20261020/independent-review.json)。

新增真实5任务/4核测试先复现原入口拒绝（RED），再验证全部任务完成、CPU0确有两个任务、其余CPU均覆盖且没有死锁（GREEN）。显式线程环境与大暂存区同样先RED再GREEN。native/rate采集回归10项、输入归档安全回归4项通过；默认C++构建通过，使用该构建对应的 `/opt/gcc-11/lib64` 后 `fastsim_tests` 输出 `all FastSim tests passed`。系统旧libstdc++直接运行会报GLIBCXX版本缺失，属于宿主运行库选择，未改模型解决。

Graph500静态构建遇到发行版自带getopt全局变量与glibc/libgomp重名；使用一致的编译宏将发行getopt及其变量改为 `spec_*` 名称，原版/标记版两边完全一致，未使用允许重复定义的链接选项。详细配置和失败/重试日志保留在实验目录。

FST沿用100,000,000,000 bytes总上限，99GB提前停止、磁盘至少保留20GB；顺序采集，单正式案预算12GB，短案2GB。旧有效回归trace保留，失败/替代FST仅在生成哈希与删除收据后清理。新的checkpoint绑定本轮runner、launcher、二进制、输入和硬件身份。

后续结果与资格应从实际完成收据更新；未生成正式trace的负载不能列入完成或精度样本数。

XZ旧block-init入口的100K短窗口warmup为99.92秒，功能thread-state验证识别5个任务、4个核且owner来源完整，但时间审计未通过：core2在测量期没有用户态退休事件，当前producer依赖CPL首次用户事件初始化clock，输出 `clock_period_ticks=1 / elapsed_status=unavailable`。全核共同marker ticks一致仍不能把该侧文件改成available。该短窗口保留为[资格缺口诊断](../tmp/spec-speed-gxf-20261020/xz-short-window-time-boundary.json)，不发布其active CPI误差。正式10M窗口必须独立验证，不能用其他核数字、config值或旧CPL代理覆盖这份缺失。

最终XZ firstchunk窗口并非完全预热稳态。各核完整窗口中，kernel物理退休记录分别约35.48%/33.22%/5.19%/3.60%，精确页清零函数分别约29.63%/3.32%/0.059%/0.060%；生产者所在CPU0开头100万条记录仍有74.11%页清零。其用户态主要执行输入memmove，worker主要执行LZMA编码。入口已跨过各worker首次真实编码调用，但没有对所有后续输入/输出页额外人工prefault，也没有证明工作集与cache/TLB达到稳态。这些剩余活动保留在user+kernel trace和统计中，不扣除为零。

## 正式采集与回放结果

三项均使用官方speed二进制；Graph500和FLAC为选定的完整refspeed子命令，XZ为官方train第二条40MiB输入。均为四核、8通道、buffer114；全核共同终点、独立硬件身份、任务人口和native来源审计通过。冻结模型未按本轮结果调参。

| 负载 / 输入 | O3 warmup秒 | FST GB | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 cycles/macro |
|---|---:|---:|---:|---:|---:|---:|
| 854.graph500_s / refspeed | 39.47 | 3.011 | 1.598213 | 1.651495 | +3.33% | 0.053282 |
| 817.flac_s / refspeed | 32.17 | 2.742 | 0.483137 | 0.523803 | +8.42% | 0.040666 |
| 801.xz_s / train | 80.51 | 3.304 | 1.062181 | 1.494112 | +40.66% | 0.431931 |

XZ的CPI误差仍为+40.66%，来源门禁通过不代表精度达标。当前未完成误差分解，不能仅凭存在缺页/清零就把全部误差归因于冷启动；没有依据CPI结果继续移动ROI或调参。

这三个有界样本的CPI MAE为 **0.175293 cycles/macro**，CPI MAPE为 **17.47%**。它不是整个SPECspeed套件或完整应用时间的精度。CPI使用workload-task active cycles；下表elapsed独立表示共同窗口的运行时间，不跨核相加。

| 负载 | gem5 elapsed ms | FastSim elapsed ms | 相对误差 |
|---|---:|---:|---:|
| 854.graph500_s | 2.660876 | 2.749586 | +3.33% |
| 817.flac_s | 0.779590 | 0.845209 | +8.42% |
| 801.xz_s | 1.867819 | 2.627359 | +40.66% |

阶段检查基于实际退休PC与绑定身份的内核符号。下表为各核统计窗口最初100万条物理记录中比例的范围；用户态主函数另见阶段收据。clear_page是精确匹配的四个页清零函数，不能把全部内核活动都解释为初始化。

| 负载 | 各核kernel记录比例 | 各核clear_page记录比例 |
|---|---:|---:|
| 854.graph500_s | 0.00%–1.02% | 0.00%–0.00% |
| 817.flac_s | 1.59%–7.46% | 0.62%–2.47% |
| 801.xz_s | 19.96%–90.64% | 0.21%–74.11% |

退休load cache主指标如下，为维护验证器发布的已知子集暂定差值；native/model仍有unknown资格，不代表完整总体或真机perf认证。

| 负载 | 事件 | gem5已知计数 | FastSim已知计数 | 暂定相对差 | native unknown | model unknown |
|---|---|---:|---:|---:|---:|---:|
| 854.graph500_s | retired_l1_load_misses | 357,907 | 357,980 | +0.02% | 753 | 3,827 |
| 854.graph500_s | retired_l2_load_misses | 179,099 | 179,204 | +0.06% | 2,097,360 | 3,827 |
| 854.graph500_s | retired_l3_load_misses | 178,376 | 178,426 | +0.03% | 2,097,381 | 3,859 |
| 817.flac_s | retired_l1_load_misses | 58,570 | 67,096 | +14.56% | 267,340 | 424,229 |
| 817.flac_s | retired_l2_load_misses | 1,693 | 2,318 | +36.92% | 373,488 | 424,276 |
| 817.flac_s | retired_l3_load_misses | 479 | 499 | +4.18% | 374,629 | 425,385 |
| 801.xz_s | retired_l1_load_misses | 83,825 | 124,821 | +48.91% | 1,050,449 | 2,708,188 |
| 801.xz_s | retired_l2_load_misses | 36,896 | 80,718 | +118.77% | 1,225,303 | 2,708,385 |
| 801.xz_s | retired_l3_load_misses | 30,849 | 75,412 | +144.46% | 1,228,225 | 2,710,510 |

原始退休load/store数量、逐核人口、执行函数占比及完整PMU字典在维护报告与完成收据中。初始页表仍是launcher CR3的快照，应用CR3的初始PTE资格没有因此补齐；DRAM refresh、紧凑Ruby/网络模型及未证明的唤醒因果仍为既有限制。真实计算入口和非清零窗口不等于预热已收敛，本轮未做warmup收敛扫描。

最终项目FST **43.000 GB**，磁盘空闲 **332.2 GiB**。清理本轮已被正式窗口替代的Graph500/FLAC pilot、XZ旧入口正式窗口及空boot FST共7,553,685,760 bytes，保留XZ短窗口的clock反例和此前有效回归trace。

[完成收据](../tmp/spec-speed-gxf-20261020/completion.json)、[pilot清理收据](../tmp/spec-speed-gxf-20261020/superseded-pilot-fst-retirement.json)。

- [854.graph500_s CPI/elapsed/native报告](../tmp/spec-speed-gxf-20261020/validation/c04-854.graph500_s/summary.md)、[退休cache报告](../tmp/spec-speed-gxf-20261020/validation/c04-854.graph500_s/retired-cache-summary.md)、[真实checkpoint身份](../tmp/spec-speed-gxf-20261020/checkpoints/c04-854.graph500_s/fastsim-checkpoint.json)。
- [817.flac_s CPI/elapsed/native报告](../tmp/spec-speed-gxf-20261020/validation/c04-817.flac_s/summary.md)、[退休cache报告](../tmp/spec-speed-gxf-20261020/validation/c04-817.flac_s/retired-cache-summary.md)、[真实checkpoint身份](../tmp/spec-speed-gxf-20261020/checkpoints/c04-817.flac_s/fastsim-checkpoint.json)。
- [801.xz_s CPI/elapsed/native报告](../tmp/spec-speed-gxf-20261020/validation/c04-801.xz_s/summary.md)、[退休cache报告](../tmp/spec-speed-gxf-20261020/validation/c04-801.xz_s/retired-cache-summary.md)、[真实checkpoint身份](../tmp/spec-speed-gxf-20261020/checkpoints-firstchunk/c04-801.xz_s/fastsim-checkpoint.json)。
