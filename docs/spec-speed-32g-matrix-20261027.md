# 四个speed负载的32GiB采集矩阵（2026-10-27）

2026-10-28 最新状态：用户明确调整实际空闲底线至30GB后，Marian C16与FLAC C16
已同时运行，单案上限仍为32GB/48GB。FLAC因策略同步操作意外中断后已从checkpoint重启；
失败尝试的日志及逐文件回收凭据保留。当前合并状态为
`tmp/spec-speed-32g-c16-recapture-20261028-parallel/matrix-status.json`。
下文50GB与Marian等待容量描述为此前启动阶段的历史记录。

2026-10-28 后续授权：已开始两路C16补采调度，并对已有10组正式来源启动4路FastSim验证。
由于实际磁盘空闲不足两案预算与50GB底线，FLAC进入采集，Marian等待容量；未降低保护。
新任务状态和独立12案来源清单见[补采与验证启动记录](spec-speed-32g-recapture-and-validation-20261028.md)。
本文件下文原矩阵的10/12完成凭据保留为原始批次结果。

2026-10-28 存储更新：按用户授权回收9组旧来源的FST及专属旁表43.41GB，
当前10组正式来源全部保留，正式矩阵仍为10/12，未启动补采。旧来源的配置和报告
保留，但已标记不可原样回放。删除收据见
[`tmp/fst-retirement-20261028/receipt.json`](../tmp/fst-retirement-20261028/receipt.json)。

当前负载集合确定为`872.marian_s`、`854.graph500_s`、`801.xz_s`、`817.flac_s`，
各采集C4/C8/C16，共12案。每案一个原生应用进程，保留其真实线程池；不是多个
单线程rate副本。当前阶段是功能来源采集，不发布新CPI/PMU精度或SPEC分数。

## 统一配置与测量边界

- 32GiB DDR4，显式`--x86-large-memory`；低区间`[0,3GiB)`、高区间`[4GiB,33GiB)`。
  两段各8通道，共16个控制器和directory；关闭部分覆盖的Ruby functional backing store。
- buffer TSO114；CPU、缓存、频率与当前维护采集入口一致，仅参与核数改变。
- 沿用最快核10,000,000 measured user-UOP目标，所有核在唯一共同终点停止；
  保留其他核真实工作，不单独裁剪或补齐。
- 各案重新建立与新生产二进制、核数、内存、launcher和输入身份绑定的checkpoint。
  不复用旧3GiB checkpoint或其他producer的checkpoint。

## 简化syscall边界修复

新增显式`--functional-syscall-microops`：新数据不折叠SYSCALL指令，而保留实际提交的
每条微操作及其依赖、分支和最后微操作标记；ABI入口参数沿用原128B旁表，锚定首条
真实微操作。FST声明feature bit8，禁止同时混入旧compact marker。共同终点不移动，
尾部执行了多少就保留多少；未完成宏指令不补成完整退休指令。

旧FST读取路径保留，其固定25条展开只用于旧marker；不把旧FLAC推导出的19条写回
旧文件。新格式读写、升级、独立检查和实际FastSim回放已覆盖1/12/19/24/25条前缀。
这些测试不替代真实FS采集的原生计数核对。格式见[FST说明](fst-v7-drmemtrace-conversion-contract.md)。

## 按负载安排预热

| 负载 | KVM下完成的工作 | 切换O3的位置 |
|---|---|---|
| Marian | 原模型、词表和输入准备，真实计算worker就绪 | 首个decoder计算入口，全体worker阻塞就绪后 |
| Graph500 | 原refspeed图输入加载、数据结构及线程池初始化 | APSP relaxation计算入口 |
| XZ | 官方输入加载和解压、编码worker初始化 | 首个编码块的实际计算入口，使用修复后的one-shot标记二进制 |
| FLAC | 官方压缩输入还原WAV、编码worker就绪 | 编码入口，全体worker就绪后 |

四者的完整`/proc`线程名册、日志flush和TTY排空均在KVM阶段完成，再切换O3。
为兼容已有检查器，串口名册的历史`post-switch-census`标签保留，但新增
`census-phase=pre-switch`声明真实位置。少量线程身份标记和放行控制仍在O3下执行，
保留在预热前史中，供独立来源检查使用。

新增`--native-warmup-after-identities`：所有worker的身份ACK齐备后，launcher在
放行实际计算前发出额外checkpoint。runner仅在此时给core0安排500,000
instruction-stop计数，达到预算后打开测量；不在刚切换O3时提前消耗预算。重复或
提前的预算事件会被拒绝。来源检查还核对该事件的顺序与tick。此计数是gem5的停止单位，不能
当作各核同量宏指令或缓存收敛证明。详细预热墙钟限制关闭，整个source保留48小时
兜底限制和容量保护；不通过缩减工作集绕过超时。

XZ统一采用最小官方refspeed子命令`cld.tar.xz 2700 ... 959312906 1042731420 8`，
不沿用旧96MiB研究输入。原压缩输入82.27MB，2700MiB的应用工作量在KVM中准备。
C16主机入场检查17个原生任务全部就绪，观测峰值RSS7.27GiB、swap0；这是内存和
线程入场检查，不是完整压缩正确性或gem5来源验收。完整命令和输入SHA在逐案JSON。

## 磁盘和并发保护

原采集使用300GB总上限、295GB提前停止。2026-10-28按用户提高上限且暂不采集的
要求，将现行总上限改为400GB、395GB提前停止；待补采Marian C16保留32GB单案预算，
FLAC C16提高到48GB。只写入`tmp/spec-speed-32g-c16-recapture-20261028-planned/`计划，
未启动采集，原冻结case与凭据保留32GB历史值。预留5GB并发收尾空间，磁盘
空闲至少50GB。并发启动还需持有原子容量预留，按实际已写入量加每案剩余预算计算，
不会把多个任务分别看到的空闲空间重复分配。checkpoint、日志等非FST增长由独立
磁盘空闲检查覆盖。异常退出的进程组必须清理完，才能保存最终凭据、释放预算；
无凭据的在途预留不会自动过期。

主队列最多4个case worker，每个worker顺序执行对应核数的来源；每案先新建checkpoint，
再恢复采集。原C4/C8/C16的单案FST预算分别12/20/32GB（含预热），上述待补采FLAC
预算另按48GB配置。空间不足时明确失败或
排队，不能以短文件冒充完成。成功和失败数据暂不自动删除；后续清理须保留逐文件
哈希凭据。保护入口见[容量策略](trace-storage-policy-20261015.md)。

## 验证及实时结果入口

gem5按用户指定`-j128`编译，嵌入Python3.11；构建环境显式提供Python/GCC运行库和
`TAOGEN_SHARED`路径。冻结二进制SHA256为
`24eb9d9098f586962c68b0d76ed91af382c5d0c5606687391d3102d9dc225093`。
首次缺库及缺共享头的构建日志保留，最终构建完成并可读取build-info。

初版12个主机ROI入场检查均已通过；正式v2队列使用更新后的launcher逐案重验。
完整FastSim构建/测试和专项回归已执行。
新FS来源须依次通过独立硬件身份、共同起止、线程归属/指令数量、整个进程覆盖及
Ruby/SLICC原生摘要守恒检查，才能写`source-ready.valid=true`。syscall未知参数
保留未知；退休缓存事件unknown和SPR硬件等价认证分别记录，不由来源通过推导。

当前正式实验：`tmp/spec-speed-32g-c4-c8-c16-20261027-final/`。
初始编译、容量盘点和第一版主机入场结果留在
`tmp/spec-speed-32g-c4-c8-c16-20261027/`；未改动前置smoke正在使用的冻结文件。
正式队列另冻结了延后启动预热预算的runner/launcher，并重新核对主机入场身份。

前置raw syscall FS检查已通过：FLAC C4、32GiB、最快核100K短窗口，实际测量user
微操作390,015，原生任务宏指令186,115；独立硬件、共同终点、线程覆盖、逐核原生
计数和摘要守恒均通过。FST约3.48GB，主要为完整预热前史；它不算正式10M完成案。

预热交接的真实FS验证随后发现：O3地址式MMIO m5op在较晚时刻读取架构寄存器，
可能读到已被后续指令覆盖的参数。GDB直接观察到checkpoint重复周期为4,816,976ns，
等于后续strcmp使用的字符串地址`0x498050`，而预期是0。该批尝试已停止，没有
形成合格正式来源；主队列和C4恢复目录仅保留为失败历史。

最终launcher仅在O3的`native-identities-ready`交接使用gem5现有指令式m5op
（编码`0f 04 43 00`，两个参数为0）；KVM交接仍使用地址式m5op，避免在KVM执行
宿主不支持的魔法指令。此区分遵循[gem5的m5op机制说明](https://www.gem5.org/documentation/general_docs/m5ops/)。
该调整只重编launcher，不改变已按128核编译的gem5。launcher SHA256为
`4a39325f6c91aaf0a85a13a2d2afa14043e50988aee9b506dccd6f0d68e16cef`。
最终队列重新建立匹配该launcher的新checkpoint，不套用旧launcher的checkpoint身份。

前置交接检查使用同一套最终实现的Graph500 C4 checkpoint，另设100K预热请求和
100K测量目标，只改变统计预算，不改变保存的guest启动内容。其来源写在正式目录
`identity-smoke/c04-854.graph500_s/`，通过完整维护检查后才能释放12案10M来源采集。
它不进入12案完成数。当前状态以`identity-smoke-status.json`和主队列状态为准。

调度的另一个问题也已修复：`source-ready.status=preparing`表示检查仍在进行，应
继续等待，不能当成失败；已有源输出则仍拒绝覆盖。实际source和checkpoint退出
凭据都保留，未知状态不会当成成功。

失败新尝试的192个FST/专属旁表已按文件SHA256清单回收37.94GB，完整日志、配置、
原生观察及诊断留存；旧合格数据和已通过的raw syscall短验证未删除。一个失败
KVM恢复曾反复输出MSR0告警，日志采用无损压缩保留并核对解压SHA256。最终队列
另有日志保护器：单日志达到500MB或64KiB尾部内重复MSR0告警超过256次时，停止
对应guard，由guard收尾其producer进程组并记录失败，不把该尝试计为来源合格。
这项保护不表示已修复底层KVM重复告警的原因。

最终目录的`matrix-status.json`始终预期12案，`matrix-completion.json`为包含有界恢复
后的逐案汇总。`campaign-status.json`和`completion.json`保留原始主队列历史。
监督器只对“测量尚未打开且日志保护器明确识别为重复KVM MSR告警”的恢复失败，
使用同一经过身份核验的checkpoint最多重试两轮。每次失败目录和替换映射都保留；
其他来源失败不会被这个规则忽略。`accepted-source/`仅链接通过检查的10M来源。
此前`-v2`和`-c4-resume`目录是失败诊断，不能混入当前来源集合。

- `cases.json`、`frozen/cases/`：12案输入、线程数、预热、容量和实际参数。
- `freeze.json`：二进制、runner、工具、配置、共享头和输入身份。
- `host/<case>/admission.json`：完整线程名册、RSS/swap及输入哈希。
- `matrix-status.json`、`matrix-completion.json`：12案含有界恢复后的状态；
  `campaign-status.json`、`completion.json`保留主队列历史；不存在完成
  凭据时不能仅凭本文件或运行命令认定已经采集完成。
- `source/<case>/capture-status.json`、`capture-receipt.json`、`source-ready.json`：
  保护器实时状态、生产退出凭据和完整来源检查结果。
- `receipts/<case>/`：实际命令、检查和失败日志。

最终O3交接FS检查已通过：Graph500 C4/32GiB、最快核100K短窗口，实际user微操作
399,292；身份、原生计数、共同终点和摘要守恒检查全部通过。45项专项回归通过。
正式队列现已结束，`matrix-completion.json`记录10/12来源合格：四负载C4/C8全部完成，
C16的Graph500/XZ完成，Marian/FLAC在测量前因Ruby functional read失败而停止。
用户要求先定位、不补采；失败现场与旧FST回收候选见
[C16失败定位记录](spec-speed32g-c16-failure-diagnosis-20261027.md)。
没有将失败案例或短验证算入12案完成数，也尚未发布这批数据的FastSim精度评估。

后续窄范围Ruby修复已通过Marian/FLAC C16、32GiB、100K短流程与配置身份核验2/2；
两组均已结束，FST被丢弃，不计入正式来源完成数。最终修复验证和仍存在的压力测试
问题见上述C16失败定位记录；正式10M矩阵保持10/12，未补采。

早期raw syscall真实FS短验证保存在`tmp/spec-speed-32g-raw-syscall-smoke-20261027/`，
它已通过；最终O3指令式交接验证另在正式目录`identity-smoke/`。短验证，不计入12案10M完成数。后续完成情况以对应机器可读凭据为准。
