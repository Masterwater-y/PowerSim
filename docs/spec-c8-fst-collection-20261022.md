# 八核 FST 采集与诊断清理（2026-10-22）

2026-10-28 存储状态：用户已授权回收本文旧C8 Graph500、FLAC正式来源以及SQLite
恢复短验证的FST及专属旁表；这些来源已标记`retired-not-replayable`，历史报告保留，
不能再原样回放。SQLite/Zstd正式来源仍保留，当前32GiB四负载正式矩阵亦单独保留。
删除凭据见[回收收据](../tmp/fst-retirement-20261028/receipt.json)。

本轮按用户授权推进 SQLite/Zstd rate、Graph500/FLAC/XZ speed 二进制的八核采集；用户已明确暂缓 Marian。采集正在进行，不能将队列启动或短恢复检查当作正式采集完成。实时状态以[采集队列](../tmp/spec-c8-fst-20261022/campaign-status.json)为准；此前拆分的SQLite后续队列已撤回，当前已切换为四路并行调度，各负载独立状态和宿主CPU绑定。

## 输入与流程

- 八个物理参与核、八内存通道、buffer TSO114；保留user+kernel及warmup前史。新建与实际八核配置绑定的staged KVM checkpoint，不复用四核checkpoint。
- KVM进入已有计算ROI后切O3。初始1M instruction-stop预算的八核pilot超过180秒上限；当前为500K instruction-stop warmup、600秒墙钟上限（用户授权由180秒放宽），不称预热收敛。100K user-UOP短恢复经过共同终点、身份、owner、进程/线程名册和syscall来源检查后，再采集最快核10M user-UOP的共同终点窗口。instruction-stop counts与FST实际宏/UOP数量分别记录。
- 当前全局FST上限200GB，199GB提前停止（2026-10-22用户新增授权），磁盘至少保留20GB；正式案预算10GB、短恢复8GB（含warmup前缀和100K统计窗口），并保留flush余量。最多四个工作负载并行运行gem5，统一预留在途FST容量。
- KVM quantum10us的一次新启动遇到guest IO-APIC timer panic，失败证据保留；后续使用runner默认1000us重新创建八核checkpoint。O3微架构参数不变。

## 负载状态与例外

| 负载 | 八核安排 | 当前边界 |
|---|---|---|
| SQLite rate | 八个原输入独立副本 | 并发初始化曾OOM；实验launcher改为逐个初始化到同一ROI屏障，全部就绪后共同放行。宿主峰值约2.25–2.28GB、ROI2.113GB，八副本均单线程；等待独立guest恢复验证 |
| Zstd rate | 八个原输入独立副本 | 八核checkpoint完成；当前500K/600秒、8GB短验证已与XZ/SQLite并行运行 |
| Graph500 speed | 原APSP refspeed子命令，八线程 | 10M正式来源采集及共同终点/身份/owner/进程/syscall门禁通过；FST6.902456832GB、预热168.14秒、unknown owner cycles=0。不是新FastSim精度认证 |
| FLAC speed | 原refspeed编码子命令，`-j8` | 最新短恢复在601.75秒预热超时，尚未开stat；日志保留，失败FST已回收 |
| XZ speed二进制 | 用户授权96MiB研究输入，level4，八worker加producer | 宿主八worker身份握手及原版/标记版完整压缩、解压校验通过；明确不是官方train/refspeed输入 |
| Marian speed二进制 | 八worker，允许workspace及mini-batch研究变体 | 容量阻塞：workspace64/32且batch32均约4.90GB；batch8约4.11GB；batch1仍3.90GB，无法放入当前3GiB guest。未启动明知会OOM的guest采集 |

Marian保留模型、输入文件、beam和八计算线程。分配器会按需增长，workspace不是RSS硬上限。用户已明确“本轮暂缓 Marian，先完成其余五个”；不把未采集的Marian列为完成来源。CPython未进入这次集合。

SQLite的改变只作用于统计前的独立副本初始化顺序，不改变工作负载二进制、输入或测量阶段的共同放行。专用launcher已纳入新checkpoint身份；8-child测试先复现并行初始化冲突，再验证串行初始化、八副本共同释放。不能仅凭宿主通过便认定guest已通过。

## 清理与保护

清理前FST52.909464816GB。已删除32个被当前完整来源替代的恢复/短诊断FST，共5.654991296GB；清理后约47.25GB。本轮新产生且在stat前失败的1M pilot另删除16个FST、3.141980160GB，保留失败证据；这是本轮生成后回收的空间，不重复扣减最初盘点。六组当前完整四核来源的24个FST均保留且属性未变；CPython smoke、同步/PMU回归及ALS数据保留。

只删除显式清单中的FST，保留伴随文件、配置、日志、统计和checkpoint。删除前记录SHA256、size、device/inode、mtime并再次核对；目录标记为`retired-not-replayable`。旧报告仍是历史证据，已清理源不再可原样回放。

## 验证与证据

已有storage6项、rate runner6项、native runner4项测试通过。独立操作流程审核发现并修复：blocked状态仍可启动、旧成功缓存未绑定身份、短恢复只编译thread-state而未执行完整来源门禁。三个恢复安全测试RED3→GREEN3，初始化顺序测试RED1→GREEN1；复审未发现剩余P1/P2。没有修改FastSim模型或运行新的CPI/PMU精度矩阵。

- [命令及输入资格](../tmp/spec-c8-fst-20261022/campaign-commands-with-train.json)、[生产者快照](../tmp/spec-c8-fst-20261022/frozen-identity.json)、[最终输入/launcher身份](../tmp/spec-c8-fst-20261022/final-input-identity.json)、[执行记录](../tmp/spec-c8-fst-20261022/progress.md)。
- [清理计划与逐文件哈希](../tmp/spec-c8-fst-20261022/cleanup-plan.json)、[删除日志](../tmp/spec-c8-fst-20261022/deletion-journal.jsonl)、[清理完成凭据](../tmp/spec-c8-fst-20261022/cleanup-receipt.json)、[保护的四核输入](../tmp/spec-c8-fst-20261022/protected-c4-inputs.json)。
- [失败pilot回收凭据](../tmp/spec-c8-fst-20261022/failed-pilot-cleanup-receipt.json)、[500K参数冻结](../tmp/spec-c8-fst-20261022/configuration-freeze.json)、[Graph500短恢复门禁](../tmp/spec-c8-fst-20261022/recovery-smoke/c08-854.graph500_s/source-ready.json)、[Marian用户暂缓决定](../tmp/spec-c8-fst-20261022/marian-deferred-by-user.json)。
- [Graph500八核正式来源完成门禁](../tmp/spec-c8-fst-20261022/source/c08-854.graph500_s/source-ready.json)、[实时逐案进度](../tmp/spec-c8-fst-20261022/current-progress.json)。队列结束后自动追加最终逐案完成表和容量盘点，失败案保持未完成，不以历史短案替代。
- [SQLite宿主启动内存检查](../tmp/spec-c8-fst-20261022/host/sqlite/admission.json)、[XZ96MiB完整正确性](../tmp/spec-c8-fst-20261022/host/xz-research96-correctness/correctness.json)、[Marian最小batch容量失败](../tmp/spec-c8-fst-20261022/host/marian/admission.json)。

## Warmup墙钟限制调整

用户授权将详细预热墙钟上限从180秒放宽至600秒；500K instruction-stop预算、ROI、微架构及100GB FST总量上限保持原设定。四个在stat前被180秒保护停止的尝试已归档，已重启队列，按FLAC/XZ/SQLite/Zstd顺序重试；已核对FLAC实际启动命令为600秒、500K预算。Graph500已完成来源及所有八核checkpoint保留。放宽执行时间不会增加预热指令预算，也不等同于预热收敛认证。

## Warmup FST容量调整

用户授权将短验证单次FST预算从2.5GB提高到8GB，覆盖warmup前缀和100K user-UOP统计窗口；正式单次预算仍为10GB。500K指令预算、600秒墙钟限制、全局100GB/99GB提前停止及20GB空闲保留均不变。

已回收64个确认失败或被完整来源替代的短验证FST，共12.373503872GB，保留日志、配置、伴随文件和checkpoint。24个有效四核FST及Graph500八核完整来源8个FST属性均未改变。每个后续完整来源通过门禁并持久记录完成后，回收对应短验证FST，避免重复预热数据长期占用空间；清理拒绝只记录警告，不撤销有效来源。

[本次清理凭据](../tmp/spec-c8-fst-20261022/cap8-diagnostic-cleanup-receipt.json)及逐目录哈希计划/删除日志可追溯。清理安全测试7项、既有流程测试3项通过；中断清理测试确认首次删除前已有不可回放标志。FLAC/XZ/SQLite/Zstd已改为并行调度。

## 并行采集（用户要求拉满）

四个剩余负载配置为独立worker，共用单一聚合器和容量预留锁。FLAC64–79、XZ32–47、SQLite16–31、Zstd48–63为独立宿主逻辑CPU集合（本机这些编号也对应不同物理核），各自仍模拟八核、八通道。每阶段按单次预算减已写入量预留，使用同一次文件观测计算全局与逐案字节，准入上界99GB；全局100GB保护仍生效。容量不足时仅相应阶段等待；全部任务等待且无在途采集可释放容量时明确报未完成，避免无限等待。

启动审核修复了预留扫描竞态及FLAC接管失败后的未计账启动风险；并发预算测试6项、既有流程3项、清理7项通过。FLAC原采集在切换期间触发600秒预热墙钟限制（实测601.75秒），尚未开stat，6.396604416GB失败前缀已按凭据回收。目前实际并行运行XZ/SQLite/Zstd三个采集，不能将四路配置写成四个正在运行。Graph500正式来源保留。没有新CPI/PMU结果。

[并行配置](../tmp/spec-c8-fst-20261022/parallel-configuration.json)、[容量预留](../tmp/spec-c8-fst-20261022/parallel-storage-reservations.json)、[实际并发核验](../tmp/spec-c8-fst-20261022/parallel-live-verification.json)。

## FLAC再次放宽并重采（用户授权）

FLAC单独采用1800秒详细预热墙钟限制，500K instruction-stop预算不变。其短验证和正式来源单次FST预算均为16GB，分别含warmup前缀和各自统计窗口；整次运行墙钟上限分别3600/7200秒，避免先触发旧总时间上限。FLAC driver和验收helper使用同一独立限制文件；其它负载继续原600秒及8GB短验证/10GB正式预算。

旧FLAC600秒失败目录已归档，复用身份匹配的八核checkpoint，producer、输入、ROI、微架构均不改。旧聚合器被替换，原XZ/SQLite/Zstd worker进程保留；新FLAC worker使用独立state和原64–79宿主CPU组。已核对实际命令为1800秒、16GB、500K，仍为八核八通道buffer114。仍共享99GB准入及100GB总FST上限，后续阶段容量不足时等待，不放宽全局上限。

[FLAC独立限制](../tmp/spec-c8-fst-20261022/flac-retry-limits.json)、[启动核验](../tmp/spec-c8-fst-20261022/flac-retry1800-verification.json)。本段只证明重采启动，不代表采集成功或CPI/PMU认证。

## XZ重试（用户授权）

XZ600秒预热超时后，已回收2.780741632GB失败前缀并归档原目录。新尝试采用1800秒详细预热限制，保持500K instruction-stop预算和96MiB研究输入、八encoder worker加producer；短验证9GB、正式来源12GB单次预算，阶段总墙钟上限分别3600/7200秒，均保留warmup前缀。9GB短验证预算参考上次600秒约2.78GB的输出速率并受全局预算约束。

已核对实际新进程参数及32–47宿主CPU绑定。SQLite/Zstd/FLAC原进程保持，四个工作负载并行推进；阶段仍需通过99GB统一容量准入，全局100GB硬限制保持。新worker和审计用独立限制文件，原三路设置不变。

[XZ限制](../tmp/spec-c8-fst-20261022/xz-retry-limits.json)、[启动核验](../tmp/spec-c8-fst-20261022/xz-retry1800-verification.json)、[失败FST回收凭据](../tmp/spec-c8-fst-20261022/xz-retry1800-cleanup.json)。重启不代表采集成功或新精度结果。

## 总容量提高到200GB并启动SQLite正式采集

用户明确将全局FST上限从100GB提高至200GB。当前策略为199GB准入/提前停止、保留1GB写出余量及至少20GB空闲。工作负载单案预算及ROI/微架构不变。SQLite在核对时已自动取得旧预算并启动正式source，worker24789/producer125080保持运行；没有重复采集。

FLAC短验证已通过，等待正式source的旧预算。只重启其等待中的调度进程以读取200GB策略，复用短验证和checkpoint，原SQLite/Zstd/XZ采集不重启。已运行保护器保留其启动时100GB快照，旧capture-command和receipt不追溯改写；后续新阶段读取200GB。

[策略变更凭据](../tmp/spec-c8-fst-20261022/storage-policy200-update.json)、[现行策略](../configs/trace-storage-policy.json)。容量与保护器测试6+6项通过。

## 完成状态（自动更新）

正式来源通过 3/5；Marian按用户决定暂缓。此表只读取完整source目录的实际完成凭据，不以短窗口或进程退出码替代。

| 负载 | 完整来源门禁 | FST文件数 | FST GB | warmup秒 |
|---|---|---:|---:|---:|
| graph500 | 通过 | 8 | 6.902457 | 168.14 |
| flac | 通过 | 8 | 11.711915 | 641.66 |
| xz | 未完成/失败 | 8 | 0.000001 | unavailable |
| sqlite | 未完成/失败 | 8 | 7.994062 | 826.02 |
| zstd | 通过 | 8 | 6.802646 | 238.38 |

全局FST按保护器保守口径 88.568GB，磁盘空闲 381.28GB。失败尝试保留在completion记录；最终是否完成由逐案qualified状态决定。

[最终逐案完成凭据](../tmp/spec-c8-fst-20261022/completion.json)。未运行新FastSim CPI/PMU精度验证。

## SQLite checkpoint 与 XZ 恢复修复

后续采集修复另建冻结实验，不覆盖上述历史完成表。SQLite 改为八副本初始化及 KVM 普查完成、共同计算门尚未释放时保存；XZ 修复 APIC 在 vCPU 线程尚未初始化时触发 `pthread_kill` 的宿主崩溃。

实现、首次执行结果及证据边界见 [修复记录](sqlite-ready-checkpoint-xz-restore-fix-20261022.md)。SQLite 首次新建点触及旧的 100s 模拟运行预算，已另行重试；这不属于 detailed warmup 超时。XZ 已通过连续两次真实有界恢复，进入完整短采集审核。
