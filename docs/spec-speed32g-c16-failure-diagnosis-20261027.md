# Speed 32GiB C16 失败定位与旧 FST 回收候选

上一轮要求先定位失败、不补采；该轮没有修改模拟器、重编 gem5 或删除 FST。
用户随后授权回收并修复，最新实施进度见末节。正式矩阵补采仍未启动。
下文容量均为十进制 GB。

## 原始失败现场

正式目录为 `tmp/spec-speed-32g-c4-c8-c16-20261027-final/`。
`matrix-completion.json` 记录 10/12 个来源合格：四负载 C4/C8 全部完成，
C16 的 Graph500/XZ 完成，Marian/FLAC 失败。主队列和有界恢复监督器均已结束。

| C16 案例 | 原始 fatal 地址 | 唯一所属 directory | 测量是否打开 | 留存 FST |
|---|---|---:|---|---:|
| Marian | `0x81f81e888` | 10 | 否 | 1,152 bytes，仅文件头 |
| FLAC | `0x8206decd8` | 11 | 否 | 0.1570 GB |

两例 `capture-receipt.json` 均记录 `guard_reason=producer-fatal`，原始日志报
`RubyPort.cc:494: Ruby functional read failed`。保护器在识别 fatal 后终止进程组，
所以 receipt 的 `returncode=-15` 是收尾动作，不是最早的故障原因。
两例 phase 凭据均止于 `switch-complete`，没有 `native-identities-ready`、
`warmup-armed` 或 `measurement-open`。

核对实际 `config.json`：RAM 高段为 `[0x100000000,0x840000000)`，两个失败地址
分别为物理地址 32.4923/32.5067 GiB，均在合法 RAM 内；按实际交织 masks
`64,128,256` 匹配，各自恰好命中一个 directory，且 DRAM range 与 directory range
一致。不存在这两个地址超出 32GiB 总容量映射或没有 directory 覆盖的情况。
成功的 Graph500 C4/C8/C16 也都已读取高于 32GiB 的页表根。
原始凭据没有 OOM、磁盘空间保护或 FST 限额触发的证据。

## 当前代码与历史机制

`RubySystem::simpleFunctionalRead()` 在没有可读缓存副本时，会搜索 controller
buffers、network 和 `Maybe_Stale` 副本；这些都未命中时返回 false。
`RubyPort::MemResponsePort::recvFunctional()` 随后报 fatal。
`MESI_Three_Level.slicc` 复用 `MESI_Two_Level-dir.sm`：目录 `I -> IM` 发出内存读取，
其中 `IM` 是 `Busy`，在数据尚未返回时，缓冲区可能还没有可读数据。

这与[2026-09-17 的独立定位](uarch-smoke-failure-analysis-20260917.md)中已实测的
functional-read 可见性缺口相符。历史文档记录的修复位于另一份隔离 gem5 树；
当前 `gem5-fs/src/mem/ruby/system/RubySystem.cc` 的 Busy 路径没有那段目录回退。
不能据此宣称补丁已有完整安全性回归。
[上游对应读取路径](https://github.com/gem5/gem5/blob/develop/src/mem/ruby/system/RubySystem.cc)
用于机制对照，本次结论以本地产物和实际调试为准。

旧 Graph500 C8 的实际配置启用了 `access_backing_store=true`，新的 32GiB 配置为
false。`RubyPort.cc` 中前者直接走 `physmem.functionalAccess()`，后者走上述 Ruby
功能性读取。因此历史 3GiB 数据能通过，不代表这个 Ruby 路径已被覆盖；而新的
Graph500/XZ C16 已通过，也不能把问题概括为 16 核或高位地址必然不支持。

只做诊断的 debugger 输出位于
`tmp/spec-speed32g-c16-failure-diagnosis-20261027/`：复用原冻结 producer、runner 和
经过身份核对的 checkpoint，输出独立目录，FST 及其 deps/fsem/mreq 全部重定向
`/dev/null`，只在第一次 functional-read 失败时抓调用栈和协议状态后结束。
设有 900 秒、500MB 输出与 50GB 磁盘空闲保护。它不产生可接受来源，不补足 12 案。
FLAC 另以原冻结 producer/checkpoint 做协议日志诊断，记录在同目录的
`c16-817.flac_s/`，同样把 FST 及专属旁表重定向到 `/dev/null`；上限600秒，
若到达测量打开仍未失败则立即停止，不继续测量采集。

### Marian 动态证明

Marian 调试已在 `RubyPort.cc:494` 的 fatal 之前截停，`accessSucceeded=false`。
本次地址为 `0x81f48ce58`，与原失败地址不同；原地址 `0x81f81e888` 在这次执行中
已多次成功读取，不能将该原始地址视为固定损坏或越界位置。

GDB 调用链是 `Commit::commitInsts -> TaoTrace::onCommit -> emitThreadState ->
registerStateTask -> readThreadStateMemory -> TranslatingPortProxy::tryReadBlob ->
PortProxy::readBlobPhys -> RubyPort::recvFunctional`。观察器正在读取
`task_struct=0xffff88881f48c700` 的 PID：`pointer+0x758=0xffff88881f48ce58`，
大小4字节，对应物理地址 `0x81f48ce58`。这次直接失败的是任务字段读取，
不能把所有同名 fatal 都称为“页表快照失败”。

协议日志逐行重建同一缓存行 `0x81f48ce40`：

| tick | 事件 |
|---:|---|
| 103179790276317 | core13 对 `0x81f48ce60` 发出正常 LD，同一64B行 |
| 103179790276650 | L0Cache13（物理L1）`I -> IS` |
| 103179790277316 | L1Cache13（物理私有L2）`I -> IS` |
| 103179790279314 | L2Cache1（物理LLC）`NP -> ISS` |
| 103179790281312 | Directory9 `I -> IM`，向内存取数 |
| 103179790310949 | 观察器读PID，四个controller Busy；`Maybe_Stale=0`，没有RO/RW副本；buffer/network查找均未命中，返回false |

直接根因是观察器的同步功能性读遇到尚未返回数据的正常内存事务，而当前 Ruby
Busy 路径没有覆盖这种状态。动态证据排除了这次错误由地址映射缺口导致，
并证明该机制在测量前确实可触发。没有实施修复；一般性修复仍须覆盖脏写回与DMA，
不能无条件绕过一致性层读可能陈旧的内存。GDB 正常退出，诊断 inferior 已终止。

### FLAC 动态证明

FLAC 也复现了同一种缺口：本次失败地址 `0x81f06b458`，缓存行为
`0x81f06b440`；与原始 fatal 地址不同，仍发生在 `switch-complete` 之后，
没有到达身份齐备、预热预算启动或测量打开。

| tick | 事件 |
|---:|---|
| 58204659479931 | core2 对同一行的 `0x81f06b460` 发出 LD |
| 58204659480264 | 物理L1 `I -> IS` |
| 58204659480930 | 物理私有L2 `I -> IS` |
| 58204659482928 | 物理LLC bank1 `NP -> ISS` |
| 58204659484926 | Directory9 `I -> IM`；同tick功能性读 `0x81f06b458`，四个Busy、零Maybe_Stale，buffer/network未命中，随即fatal |

两例现在都有同一缺口的动态协议证明；Marian 另有 GDB 调用栈证明直接发起者是
线程状态观察器。FLAC 此次没有另抓调用栈，不能声称两例的具体C++调用栈逐帧相同。
两次诊断均没有产出正式 FST，未修改原12案完成凭据。FLAC fatal 后对仍在收尾的
确切进程组发送SIGTERM，收据为 `c16-817.flac_s/fatal-observed-stop.json`。
原始两次失败地址本身没有保存瞬态协议日志，以上证明来自同一冻结checkpoint和
producer的诊断执行，不能冒称恢复了原fatal瞬间的全部状态。

## 可优先回收的旧失败文件

| 旧目录（均在 `tmp/` 下） | 失败依据 | FST GB | 连同专属旁表 GB |
|---|---|---:|---:|
| `spec-c8-fst-20261022/source/c08-708.sqlite_r-warmup600-1792660235` | 预热超时，测量未打开 | 2.0396 | 2.0895 |
| `spec-c8-ready-kvm-repair-20261022/recovery-smoke/c08-801.xz_s` | 整体超时，未通过来源验收 | 3.2244 | 3.2370 |
| `spec-c8-ready-kvm-repair-sqlite-drain-20261022/recovery-smoke/c08-708.sqlite_r` | 预热超时 | 2.0588 | 2.1141 |
| 合计 | | **7.3228** | **7.4406** |

另有旧 100K 短验证候选：`spec-marian-c8-32g-20261026/recovery-smoke-current/`、
`spec-speed-gxf-20261020/recovery-smoke/` 和
`spec-c8-fst-20261022/recovery-smoke/c08-801.xz_s/`，FST 共 **5.1799GB**，
连同专属旁表 **5.2590GB**。它们是保留历史配置的已完成短验证；当前同名负载已有
新的 32GiB 10M 来源，但配置、输入或窗口不同，并非字节重复。
删除意味着放弃旧短验证原样回放，只保留诊断、配置、原生统计和报告。

两类合计 FST **12.5027GB**，连同专属旁表 **12.6996GB**。定位轮仅形成候选清单，
没有执行删除；检查时候选未发现进程持有打开文件。197个候选文件的SHA256、
size、device、inode、mtime已冻结，哈希期间逐文件属性一致，且与10个accepted来源
均无路径交集。删除前仍须重新检查进程引用和文件身份，并留下不可回放标志及删除凭据。

完整清单：正式目录中的 `deletion-candidates-review-only.json`；全项目 FST
盘点：`diagnosis-fst-inventory.json`。盘点时全部 FST **237.8676GB**。
本轮 10 个合格来源、两例新失败诊断、raw-syscall 修复短验证、退休 PMU 回归集及
PaTGen 数据保留；旧 C4/C8 正式来源仍承载历史误差报告，不因日期较早便直接删除。
此前已按凭据回收的 37.94GB 不计入本次可释放空间。

## 后续授权回收与窄范围修复（2026-10-28 UTC）

用户授权“1.回收 2.能否修复”后，已回收上述两类197个文件，共
12,699,647,792 bytes，其中FST为12,502,700,768 bytes。重新核对全部文件SHA256、
device/inode/size/mtime及打开文件引用后，先写每目录不可回放标志，再逐文件删除并
fsync日志。10个accepted来源共1,980个文件属性保持一致，原始统计、日志和checkpoint
保留。清单、逐文件凭据和完成收据在 `tmp/speed32g-fst-retirement-20261027/`。

本次修复采用比历史通用Busy-directory回退更窄的办法：`MESI_Two_Level-dir.sm`
只将IM的功能性权限从Busy改为Read_Only。进入IM的唯一转换是`I + Fetch`，
动作只向内存排入读取、弹出原请求，没有脏owner或DMA TBE；收到Memory_Data后
才进入M，因此这段期间内存始终是有效来源。时序转换和动作完全不变，MI及DMA
中间态保持Busy；RubySystem仍优先读取RW缓存副本，功能性写的目标集合不变。
该目录被MESI_Three_Level与MESI_Two_Level共用。

补丁为 `gem5-collection/patches/0003-mesi-clean-fetch-functional-read.patch`，
manifest与README已同步；在修改前的目录源码上应用补丁，结果与生产源码逐字一致。
`tests/test_ruby_clean_fetch_functional_read.py` 调用真实SLICC编译器，再编译执行生成的
目录权限和实际RubySystem读写函数。RED：干净IM读取一项失败，其余8项通过；
GREEN：9/9通过。夹具只在controller/network/memory边界使用测试替身，不将它当作
真实FS或DMA全覆盖证明。独立审核未发现阻断问题。

FastSim全量构建与直接测试通过；40项相关Python测试通过。gem5按`-j128`重建成功，
新二进制SHA256为 `2cf2d525185e00ea4b84dfc692c487654e47092dfda5ed456639f279cdbc4e6a`。
Marian/FLAC C16已创建与该二进制绑定的新checkpoint；预热请求100K、
最快核测量目标100K，FST重定向`/dev/null`，不作为正式来源、精度报告或补采结果。
构建、测试及后续FS定向验证凭据统一保存在 `tmp/ruby-clean-fetch-fix-20261028/`。
旧正式目录的冻结producer、checkpoint身份和10/12完成凭据不覆盖。

### 额外压力检查的失败边界

上游 `configs/example/ruby_mem_test.py` 的16核、8目录、128MiB、20%功能性访问、
maxloads=1000配置，新旧二进制均通过，终止tick同为1,591,217。
这是真实生成的控制器与内存回调检查，但没有复现当前目标缺口，不能替代FS定向验证。

将功能性访问提高到50%、maxloads提高到10000后，两者都失败：旧二进制先报
`Read access failed at 0x40a942`；新二进制继续到tick1,130,761，在
`0x407789`读回0而预期0x84。为区分先后两个问题，只在诊断对照的旧二进制运行中
临时开启`--suppress-func-errors`，使其越过前一个错误；它随后在**相同tick、地址和
数值**上出现同一读值不匹配。故后一个问题在旧producer中也存在，并非IM权限修改
新引入。该选项未用于任何正式来源或FS修复验证，也没有把这些失败算作通过。

该失败时日志为`num_maybe_stale=1,num_busy=2`，同一行处于LLC `MCT_I`替换及
私有cache `SINK_WB_ACK`相关阶段，与本次干净目录`I->IM`缺口不同。
本轮没有修改这些状态，未声称Ruby全部功能性读写正确性已获认证。
证据为修复目录中的 `stress-comparison.json`、`stress-after-debug.log` 和
`stress-before-suppress.log`，压力测试仍标记失败。

定向FS中的身份交接还有正常的异步事件等待：GDB只查看主队列时看不到checkpoint，
但异步队列中已存在`checkpoint`，repeat=0；FLAC现场current tick为
71,511,628,197,591，事件tick为71,512,314,920,515。`exitSimLoop()`加入simQuantum，
全局事件先排入各队列的async_queue，不能把主队列暂时没有该事件判为丢失或参数错误。
证据为 `flac-checkpoint-events.log`、`flac-checkpoint-async.log`。
本轮没有改变事件调度或量子。host阶段日志的`native-identities-ready`/`warmup-armed`
记录runner处理退出事件的时刻，不能当作guest实际ACK或魔法指令的同tick证明。

### 定向FS验证当前进度

首次两案均已越过原fatal位置并完成guest线程身份ACK；Marian进一步处理了
native-identities-ready事件并启动预热预算。两案均未再出现Ruby functional read
fatal，但在1800秒总墙钟限制内未打开测量，最终由wall-time-limit停止，
因此**首轮100K完整短验证为0/2通过**，不能将“越过原故障”写成完整FS验证通过。
起初30分钟的诊断限时估计偏短，失败凭据保留在`diagnostic/`和`fs-completion.json`。

监督器只对上述“wall-time-limit且无fatal/panic”的两案各重试一次诊断，将总墙钟
上限改为7200秒。继续使用同一新producer和经核验的新checkpoint，预热/测量目标
仍各100K，不改成更短窗口、不吞读取错误，也不产生正式FST。新输出在
`diagnostic-long/`，每案仍受50GB空闲保护及100MB单日志限制约束。
`fs-live-status.json`为当前状态；`fs-final-completion.json`保留监督器最终原始结果，
`fs-verification.json`为发布后的短验证汇总。最后者尚不存在时验证仍未结束。
诊断request元数据由实际`sync-run-request.json`派生，保留FST已丢弃与正式来源
未验收标志，再交维护的effective-target/identity工具核验；不跳过独立身份检查。
原12案正式采集结果仍为10/12，没有启动正式补采。

### 定向FS验证最终结果（2026-10-28）

延长限时的两案均已正常结束，**100K短流程与独立配置身份核验2/2通过**。
两案均有身份交接、预热启动、测量打开和共同终点；producer退出码0、
sync-run退出码86、保护器原因null，独立identity结果valid=true。
本轮没有再出现原Ruby functional read fatal。

| C16/32GiB案例 | 预热预算启动→测量打开 | 切O3→测量打开（含交接） | 测量墙钟 | 本次诊断总墙钟 |
|---|---:|---:|---:|---:|
| Marian | 21分55.5秒 | 33分49.2秒 | 3分37.4秒 | 44分35.4秒 |
| FLAC | 29分8.2秒 | 61分3.6秒 | 19分39.4秒 | 86分35.4秒 |

前三列按阶段日志相减，总墙钟取capture-receipt；包含相应退出事件的等待，
不是单独执行100K指令的耗时，也不用于声称吞吐改善。测量共同终点分别为本机
01:11:06（Marian）和01:53:08（FLAC），两组producer进程均已退出。

最终发布结果为修复目录的 `fs-verification.json`（finished=true、passed=true）；
逐案凭据位于 `diagnostic-long/<case>/capture-receipt.json`、
`capture-phases.jsonl`、`sync-run-result.json`、`identity-validation.json`。
原始监督器结果中的error保留首轮超时历史，发布结果将其放在previous_attempt_error，
不把它混作本轮错误。

这是修复后的有限FS回归，不是正式FST来源验收：FST已丢弃，未执行完整FST/原生
指令计数及Ruby摘要守恒来源检查，未评分FastSim CPI/PMU，也未证明预热收敛或
硬件等价。上节50%功能性访问压力检查的既有读值问题仍未修复。
正式10M矩阵依然10/12，没有补采。
