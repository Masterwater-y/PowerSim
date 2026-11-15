# 冻结版 cache miss 修复实施记录

2026-11-11。**实施中；尚未完成缓存误差修复或精度/吞吐验收。**

## 隔离与临时目录

本轮源码、构建、审计、补采及候选快照均位于
`tmp/r01-cache-repair-exclusive-20261111/`。生产版本与 R0/R1 冻结版本保留。

系统 `/tmp` 的本项目清理已完成，见[清理报告](fastsim-system-tmp-cleanup-20261111.md)。
关键历史代码/证据迁至 `tmp/system-tmp-recovery-20261111/`，逐文件哈希、权限及符号链接均核对。
后续命令通过 `scripts/project_tmp.sh` 设置项目内 `TMPDIR/TMP/TEMP`。

## A0：诊断代码与预测守恒

已完成：

- `RetiredCacheMissEstimator` 增加可选诊断返回值，保留冻结版观察规则。
- 按核跟踪真实宏起点与成员 OR，处理测量边界、故障前缀丢弃与未完成尾部。
- 固定全量成员/决策/宏计数，与独立容量限制的 JSONL 样本分开；截断样本不能冒称全量。
- 记录读成员的自身服务、模型时钟、此前填充区间、决策；另记退休 store/prepare 服务来源。
  无独立 L1 数据完成边时显式 `null`，不把 LLC generation 冒称私有填充身份。
- 审计与完整 FST 身份绑定，输出文件哈希进入预测结果。
- 新建 SPR 专用离线配对工具，读取 native `spr_miss_mask/spr_unknown_mask`；
  要求维护原生验证器的来源/人口检查通过、完整 closure 以及 capture binding。
  split 的 native UOP OR 结果不能冒称每 fragment 真值，现阶段样本工具单列这类覆盖缺口。

首轮与最终 A0 诊断版本均完成原五例回放，最终版关闭诊断的五例回放也已完成，
**开启/关闭诊断时，五例全部模型预测均与冻结版一致**。
各核全量 UOP/宏/退休 load 与三层宏 OR 计数均与预测文件守恒。
`causal_frontier.frontier_waits` 和 `max_resident_chunks` 是主机生产者等待/队列水位，
仅这两个主机调度统计、墙钟/吞吐和审计元数据不属于预测一致性比较。

最终 A0 候选快照：
`tmp/r01-cache-repair-exclusive-20261111/snapshots/a0-observer-v4/`。

- 二进制 SHA-256：`ef13b322a2b619b3e0ada9a3dcf50e56ebbf892c82a0f5a494e505660a092f46`。
- 父冻结二进制 SHA-256：`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`。
- 8 个新增/修改源码、工具或测试文件，完整源码清单和 patch 哈希见快照 `identity.json`。
- 保护复查 **2,256 项未变化**；用户授权的主目录 `AGENTS.md` 临时目录规则单独排除。

证据：[最终全量守恒检查](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/a0-audit-v4/conservation.json)；
[候选身份](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/snapshots/a0-observer-v4/identity.json)；
[候选补丁](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/snapshots/a0-observer-v4/candidate.patch)。

## CPI 保护结果

四核 `workload_task` active CPI；AE 单位 cycles/macro。预测完全不变，因此沿用维护原生验证器
已记录的冻结评分，**不是新的 native 评分，也不是吞吐测量**。XZ/Marian 的既有极少量 native
unknown 与未通过完整退休事件认证的状态保留。

| 负载 | gem5 CPI | 冻结 CPI | A0 CPI | CPI AE | 相对误差 |
|---|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 1.142316 | 0.063733 | −5.28% |
| XZ | 0.575297 | 0.601359 | 0.601359 | 0.026062 | +4.53% |
| FLAC | 0.284584 | 0.318433 | 0.318433 | 0.033849 | +11.89% |
| Graph500 | 0.966422 | 0.835896 | 0.835896 | 0.130526 | −13.51% |
| Marian | 2.364997 | 3.068823 | 3.068823 | 0.703826 | +29.76% |

五负载等权 **CPI MAE 0.191599 cycles/macro；MAPE 12.994957%**。缓存误差尚未改善。

## 新增诊断事实与首个单项实验

- XZ core 0 读观察器只有 **1,627** 次填充区间安装，识别 **1,512** 次 PendingFill。
  同核退休写服务却有 **11,375** 次私有 L2 命中和 **40,634** 次更下层供给，
  即缺少 L1 驻留数据的写服务共有 **52,009** 次。原观察器只由 read 成员建立区间，
  这些潜在 store/prepare 数据填充来源未进入它的来源集合。
- Graph500 四核 PendingFill 共 **5,719,283** 次，其中 **5,506,910** 次使用局部回退时钟；
  几乎全部由自身 mask 0 改判为 L1 miss。该事实用于定位，不能直接认定每个成员是误报。
- 这些事实没有建立原五例的逐 load native 误判配对，不给未知根因分配精确贡献率。

首个行为实验只增加**退休 store/prepare 数据填充来源**，保留原影子容量及冻结响应近似。
hot L1、纯权限路径和没有匹配数据来源的 alias 不独立建立填充区间。
实验开关 `FASTSIM_RETIRED_FILL_STORE_SEEDS=1` 默认关闭；不更改 CPU timing/cache/request。
边界、权限、alias、同线新代次等定向反例、构建和标准测试已通过，五例同输入回放已完成。
这不是已验收修复，不能与完整集成成功混淆。

首轮实验只改变观察统计，CPI、功能 cache/request 与原资格计数均不变。
退休 L1 point estimate 的增量：Zstd +121、XZ +123、FLAC +4、Graph500 +6、Marian +13；
三层中的 L2/L3 增量全部为 0。该实验没有闭合 XZ 的主要漏报，且增加了已多报负载的 L1
预测，**不满足推广条件，保持关闭**。新预测的维护原生评分正在执行，不能将本段预测增量
当作已完成的新 native 误差评分。完整候选和原始结果保留在
`snapshots/a1-store-data-seed/`、`a1-store-seed-original/`。

同时发现实现资格中需要排除 RMW/read-write 自身：它已有 read-side 区间，
不能先由自己的写成员播种再把自己判为 follower。工作副本已补该静态资格约束，
尚未重建；它不改变首轮候选的冻结身份或上述原始结果。

## 独立 native 补采与剩余工作

原五例没有封存的逐成员 native fill 日志。五例新补采已启动，输出为
`tmp/r01-cache-repair-exclusive-20261111/native-members/sources/`。
沿用原四核、1000 万 user-UOP target、50 万 warmup、关闭硬件预取和退休前 early prepare 的配置；
退休 prepare 保留。native 时钟/路径日志只由离线配对工具读取。
新源完成后冻结版与候选必须在同一新 FST 上回放，不能用新旧源误差之差当收益。

共享预算与守护已记录：启动时全项目 FST 348,475,818,314 bytes；
五例合计预留 32,000,000,000 bytes，低于 395 GB 停止线，并保留 30 GB 空间及 sidecar 预算。
各采集继续使用维护的 `trace_capture_storage.py`。

Graph500、Zstd、FLAC、Marian 新采集已封存（capture 与 prepare 均返回 0）。
XZ 采集守护记录返回 0，采集已结束，后处理与完整封存尚未完成。
Graph500 独立硬件身份核验通过，冻结二进制与 A0 快照在该同一新源上的预测全部一致；
native 样本日志 48,699 行、无省略、未截断且完整关闭。Zstd 两版同输入回放也已完成，
模型预测差异为 0。Graph500、Zstd 的维护来源评分仍在运行，成员配对尚未完成；
FLAC 两版回放在运行，Marian 两版回放尚未启动。

用户询问进度后的中断状态复查：采集后处理、来源评分、首个实验退休评分以及 FLAC 回放
进程仍存活，未重复启动。补采的目的是获取原五例缺失的逐成员路径/填充审计；
沿用已保存的 gem5 二进制与采集配置，原五例和原精度基线继续保留。
新源只作为独立参考和同源回放输入，native 动态时钟不进入 FastSim timing 模型。
目前尚无通过完整精度与吞吐验收的缓存误差修复。

待完成：新源封存/硬件身份检查/维护原生评分和成员配对；单项观察器实验验收或撤回；
基于证据继续时间/代次/数据结束边修复；forwarding 存活/覆盖修复；退休服务及私有/LLC 请求归属；
五例最终精度验收与 30 次串行吞吐配对。未合并、提交或推送。

## 退休事件语义核验的新发现

用户追问统计口径后，核对当前共享解释器与既有真机记录，确认需要优先解决语义疑点：
共享策略把 L1 PendingFill 计作 L1 miss，而 SPR 既有 flush+双 load 微内核约为
1 次 L1_MISS、1 次 FB_HIT，不支持将 follower 一律包含在硬件 L1_MISS 中。
这不证明所有特殊路径均互斥，不能直接把全量 follower 删除或相减回算。
原五例误差仍是对 gem5 内部 `spr-retired-load-miss-v2` 投影的暂定误差，
不代表真机事件精度；新采集同一解释器也不会自动修复这条共同语义偏差。
宏 OR、主预测字段与原生 total 字段目前未发现混用。
详见[口径核验](fastsim-r01-retired-cache-miss-caliber-audit-20261111.md)。
五例独立补采现已全部封存；修复顺序须先核实事件映射，再确定 follower 的推广目标。

后续用户明确指示“先不管真机的问题”。当前继续固定 gem5
`spr-retired-load-miss-v2` 为内部参考，推进同策略下的模型误判与缓存请求修复；
不因硬件映射待认证而阻塞本轮工作，不改冻结策略或把旧参考重算当修复收益。
两端共享的是服务结果到事件位的转换规则；gem5 用原生生命周期/服务消费者关联，
FastSim 用每核固定影子区间和近似 issue/response 识别消费者。
Graph500 原五例回填修正多报、XZ core 0 漏识别的证据仍适用于该内部目标。
Zstd 新源维护来源评分已返回 0，SPR 逐成员配对已启动，输出为
`tmp/r01-cache-repair-exclusive-20261111/new-zstd-a0/member-pairing.json`；
Graph500 新源维护来源报告尚未完成，不能提前发布其逐成员配对结论。
Zstd 首次成员配对随后在 FST 成员绑定阶段拒绝：
`audit member disagrees with actual functional source`。未生成可信配对报告，
后续需定位具体身份字段差异；保留来源拒绝，不绕过门禁，不把未配对样本外推成根因比例。

## 吞吐约束下的后续设计与新源核对

用户要求“考虑如何修复，在保持同水平吞吐量的情况下”。具体方案已写入
[有界填充修复设计](fastsim-r01-bounded-fill-repair-design-20261111.md)。
使用现有功能服务摘要、计时反馈和每核固定槽位；分开时钟、owner/代次和数据结束边，
逐项验收。特别核实：默认开启 `ruby_line_generation_admission_audit`
会使部分 `try_compute_response_inactive_segment` 批量快速路径失活，
因此不能直接把该审计依赖变为生产修复路径。
没有通用独立 L1 数据可见边的路径继续明示近似；LLC generation 不当作 L1 身份。
吞吐必须由关闭诊断的串行配对运行证明，固定复杂度本身不等于同等吞吐。

五例新源的冻结/A0 回放现已全部完成。五例模型预测差异均为 0，结构化结果封存于
`tmp/r01-cache-repair-exclusive-20261111/new-source-parity-and-spr-coverage.json`。
本次复查保护文件 **2,256 项未变化**。这是 A0 中性诊断的守恒结论，不是新修复收益。

Zstd 首次成员绑定拒绝的具体原因已确认：四条跨页 load 的模型片段位于非连续
物理页，而旧工具按首物理地址加总宽度作连续范围校验。独立工具增加使用维护
`.xlat.gz` 解码器、完整 FST 绑定和逐片段物理范围的核验；所有被审计的跨页请求
均需该证据，包括表面落在连续范围内的片段，不放宽 PC/flags/phase/closure 检查。
隔离 source/build 仅绑定已有 WORK/build，使维护解码器能使用其既有流哈希工具。
修正 Python 3.8 环境不支持 `hashlib.file_digest` 的工具兼容问题，保持 SHA-256 口径。

此外，Graph500 在来源校验通过后被 SPR 成员字段门禁拒绝。逐例核实发现：
本次五例补采使用保存的旧 collector，member 日志没有 `spr_miss_mask`、
`spr_unknown_mask`；缺失行数 Zstd 56,812、XZ 11,617、FLAC 12,329、
Graph500 42,047、Marian 24,884，分别占各例 member 的全部人口。
原生聚合退休报告存在不代表逐成员日志具备相同输出能力。
**此前“补采完成”仅表示采集/封存完成，未满足 SPR v2 逐成员配对所需字段。**
这是采集能力预检遗漏，不能用旧 source 位或聚合 estimated 数重建逐条 v2 真值。
当前五例仍没有可信 SPR 逐成员配对报告。

工具现增加早期 SPR schema 检查，在完整输入 hash/翻译扫描前拒绝旧日志；
物理片段来源、原配对和 SPR 字段覆盖的 **23 项测试通过**。
Graph500 的维护原生来源评分已返回 0；该状态不能替代缺失的成员事件资格。
后续先核实 collector 的字段输出能力，再决定最小诊断补采范围，
继续从 A0 派生单项观察器候选。生产/冻结模型未修改，模型误差和吞吐尚未改善或验收。

## T1：最终模型 issue 摘要的独立候选

用户授权“开始实施并验证”后，已从 A0 v4 单独派生
`tmp/r01-cache-repair-exclusive-20261111/t1-clock/`，未使用被拒绝的 store-seed 工作副本。
复用正常计时循环已经计算的 UOP issue，记录为最终模型 local Q16 时间。
无内存区段的批处理条件保持原样；没有最终 issue 的近似路径以 `UINT64_MAX`
标 unavailable，合法周期零独立处理。该时间仍是 UOP issue 加已有请求准入边的观察投影，
不冒称所有 split/特殊 fragment 的逐请求实际 Ruby 准入。

实验开关 `FASTSIM_RETIRED_FILL_ACTUAL_CLOCK=1` 在初始化时读取，默认关闭。
新增摘要仅给退休观察器消费；CPU timing、canonical memory displacement、
cache replay 和响应服务接口未修改。数据结束仍保留既有 response 上界近似，
该候选不是数据边、prepare 或服务身份的全量修复。
审计补充最终模型 reference issue、clock source/domain 和结束边来源。

Release 构建、标准 `fastsim_tests` 及 11 项时钟/观察器定向测试通过。
候选封存为 `snapshots/t1-final-clock-v1/`：

- 二进制 SHA-256：`97bb5a0f1fb20baee367b08416828338e36dd6be084f12b7e3e8a0606064faa5`。
- 实施父版本 A0 v4 SHA-256：`ef13b322a2b619b3e0ada9a3dcf50e56ebbf892c82a0f5a494e505660a092f46`。
- 2,256 项生产/冻结保护文件再次核对，无变化。
- 原五负载关闭修复的守恒回放和开启修复的审计回放已启动，分别位于
  `t1-off-original/` 与 `t1-on-original/`。当前尚无五例完整结果，不提前报告误差收益。

现存 gem5 collector SHA-256
`44f3229344757f3493a01c8f2972ae0158b9817ed43d643192042a99ce99ac71`
包含 `spr_miss_mask/spr_unknown_mask` 字段串，源码也已将当前 reducer 的四个字段送入 member。
已保存独立 collector 副本并启动 Graph500 小规模实际输出能力预检
`spr-collector-probe/`（四核、100,000 user-UOP target、10,000 warmup）。
字符串/源码核对不是输出能力通过；须以实际 member、完整 closure、prepare 与独立身份门禁为准。
预检共享 FST 起点 361,909,435,050 bytes，预留 500 MB，继续使用维护存储守护。
旧缺字段日志和旧精度基线保留，未将新采集或诊断时间混入吞吐测量。

当前仍在实施与验收阶段，未合并、提交或推送。

### T1 首轮结果与 T2 后续候选

T1 关闭修复的五例回放完成，全部预测与冻结版完全一致。
开启修复的五例回放也已完成；L2/L3 预测均不变，L1 增量如下：

| 负载 | 冻结 L1 预测 | T1 L1 预测 | 增量 | 相对于既有 native 偏差的方向 |
|---|---:|---:|---:|---|
| Zstd | 1,021,776 | 941,746 | −80,030 | 改善多报 |
| XZ | 80,809 | 79,968 | −841 | 加重漏报 |
| FLAC | 35,611 | 39,572 | +3,961 | 改善漏报 |
| Graph500 | 6,275,758 | 6,512,626 | +236,868 | 加重多报 |
| Marian | 179,540 | 185,097 | +5,557 | 改善漏报 |

**T1 不满足逐例不恶化，保持关闭，不启动其推广吞吐验收。**
上表为同输入预测变化与既有偏差方向，维护原生新评分已启动；
不能将预测增量当作完整的新 native 精度验收。

Graph500 同源模型样本明确展示了一条 T1 自身回归：core 1、ordinal 804675、
line 531264232 的原服务区间为 `[605724,606468)`；
最终模型观察时刻为 606470。T1 因响应早于观察而补出
`[606470,607214)`，随后 ordinal 804684 的普通 local hit 从 0 改判为 L1 miss。
这是模型区间重新打开的机械见证，不是已完成 native 配对或全量根因贡献率。

已单独派生 T2 `t2-owner-clock/`：原服务的 begin/response 保留，
consumer 使用最终模型观察时间；合法的已结束服务不因晚观察而重新开窗。
没有原服务时间的路径保留 canonical 近似。CPU/cache/资源响应仍不变。
修复只针对观察接口，未重算配置 miss 时长、未播种 prepare。
已结束服务、较早乱序 consumer 与旧重复开窗反例的定向测试通过，
Release 构建/标准测试正在执行。T2 尚未完成五例回放或推广验收。

### SPR 字段能力与小预检来源失败分开保存

Graph500 小预检采集返回 0，实际输出 535 个带四个 SPR 字段的 member；
四核出现，日志 4,946 行、完整关闭、无截断/省略。
prepare 返回 1：`core 1: CPL/common end mismatch`。
该核 native CPI 的 measured `n_user=0`，CPL `first_tick=last_tick=0`；
短目标没有形成所需的完整四核 measured 来源。
失败的原 `spr-collector-probe/capability.json` 保留，不改成来源通过。

另存 `spr-collector-output-capability.json`，只证明字段输出能力，
明确 `prepared_source_passed=false`。基于已验证的输出能力重新采集
与原五例相同的完整 10M user-UOP / 500K warmup 配置，
输出 `spr-members-v2/`；每例独立 prepare/closure/来源门禁仍须通过。
新源基线与候选均须同源重放，旧聚合参考与旧日志继续保留。

### B1 coverage：独立的较新部分重叠 store 修复

从 A0 单独派生 `b1-overlap/`，开关
`FASTSIM_STORE_FORWARD_OVERLAP_GUARD=1` 默认关闭。
反向搜索遇到较新部分重叠 store 时保留普通 load cache 请求，
不再越过它从更旧的完整 store 转发；不引入多 store 字节拼接。
标准测试和四个真实 Simulator 反例通过（下半部分覆盖、上半部分覆盖、
不重叠 store、较新完整 store），人口一致。
这只闭合覆盖规则，不等于 forwarding 排空过期修复。

已封存 `snapshots/b1-overlap-v1/`，binary SHA-256
`226b9ff23c39eafcd3b22da04b23988fc77d05373c9df6e7e5ac50d88af2d4f5`；
2,256 项保护文件无变化。五例开/关回放正在执行，
其功能请求与 CPI 的变化将独立评分，不混入 T1/T2 观察器结论。

五例回放随后全部完成：关闭修复时预测差异均为 0；开启 coverage 修复后
Graph500 全部预测不变，其余四例发生功能/时序变化。
同原始 FST 的既有维护 CPI 参考下，结果如下；AE 单位 cycles/macro。
这使用新候选预测及既有同输入参考，新的完整 native/退休逐事件评分仍在执行。

| 负载 | gem5 CPI | 冻结 CPI | B1 coverage CPI | B1 CPI AE | B1 相对误差 |
|---|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 1.142316 | 0.063733 | −5.28% |
| XZ | 0.575297 | 0.601359 | 0.601687 | 0.026390 | +4.59% |
| FLAC | 0.284584 | 0.318433 | 0.319860 | 0.035276 | +12.40% |
| Graph500 | 0.966422 | 0.835896 | 0.835896 | 0.130526 | −13.51% |
| Marian | 2.364997 | 3.068823 | 3.054434 | 0.689437 | +29.15% |

五例等权 MAE **0.189072**，MAPE **12.984994%**；冻结版为
0.191599 / 12.994957%。该轻微均值变化不能替代逐例验收或吞吐测量。
全物理人口 forwarded UOP 变化依次为 −9,287 / −407 / −152 / 0 / −89；
这是 `totals.forwarded_loads`，不能与任务退休宏 forwarding 表直接比较。
证据：[B1 比较](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/b1-overlap-original-comparison.json)。

### T2 与 B2 正在执行的验证

T2 Release 构建、标准测试和 11 项定向测试已完成并通过，
封存为 `snapshots/t2-split-clocks-v1/`，SHA-256
`dec0f60fcf5161c6ef863bf1c19ace6f74c489de289ba7bbd3546ec09227257a`。
五例开/关回放已启动，等待结果。

从 A0 单独派生 `b2-forward-lifetime/`，绑定 forwarding 的原 store sequence，
在已有计时循环收集 SQ 最终 release，只发布最终接受前缀的模型边。
功能请求发生前，用原 OoO producer issue 加既定 core gap 的下界检查已接受的旧 owner；
已排空则为该 load 的全部数据 fragment 恢复普通访问。
每核使用 `2 × effective SQ entries` 的直接映射 release 表；
冲突老身份仅在完整历史 release 上界已结束时允许过期，未知历史不补猜。
没有跨核锁、逐周期调度或新收敛循环。

该候选只覆盖**跨接受前缀的已知排空边**。同一前缀尚未取得最终边的 owner、
批处理未导出 store 边、未知服务均保留原近似，不能称全量 lifetime 修复。
开关 `FASTSIM_STORE_FORWARD_LIFETIME=1` 默认关闭。
构建与真实长间隔请求恢复/短间隔保留反例正在执行；
尚无 B2 精度、吞吐或完整验收结果。

T2 五例结果已收齐。CPU/CPI/cache/request 严格不变，四核宏/UOP及退休
宏 OR 全量守恒；CPI MAE 仍为 0.191599，MAPE 12.994957%。
L1 预测变化依次为 Zstd −158,139 / XZ −5,017 / FLAC −1,945 /
Graph500 −34,809 / Marian −56,971；L2 仅 Zstd +1，其他 L2/L3 均不变。
XZ、FLAC、Marian 既有漏报方向进一步减少，因此 **T2 也不推广，不启动其吞吐推广门禁**。
维护原生新评分已启动；完整计数错误需等该评分，而非由上述增量冒称。
[T2 守恒证据](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/t2-original-invariants.json)。

B2 Release 构建/标准测试和三项独立/真实 Simulator 反例通过。
长达 16,000 条普通指令的间隔后，旧窗口仍转发的 load 在新候选中恢复普通
L1D 请求；短间隔、未取得同前缀 owner 边时保留旧近似。
封存 `snapshots/b2-forward-lifetime-v1/`，二进制 SHA-256
`a35a95a666d0e5f70fc1a4caabc2620105db11524d8d4dd90fbcf1f438edd87b`，
保护核查 2,256 项无变化。五例开/关回放已启动，尚未完成精度验收。

### I1：复用原父服务解析的稳定身份

从 A0 派生 `i1-service-identity/`，在原 `resolve_parent_service` 调用处携带
选定父服务的稳定 record ID、owner sequence、实际模型准入、data-ready 上界。
只消费已有认证的普通 read 数据服务；不另建全局服务池、不全局按地址查父服务，
不将 LLC generation 或 prepare 权限服务冒称私有数据 fill。
每个内存事件的可选摘要为 5 个 uint64 加一个 bool（实际 C++ 对齐 48 bytes），
只在开关启用时分配；该字段带宽成本必须由吞吐实测验收。

明确选定的冷 owner 保留自身 initiating lookup mask，不被旧影子区间改成 follower；
认证 follower 用同一父服务的实际模型准入和 data-ready 上界识别 pending。
未取得身份的路径继续保留 A0 近似，不能用仅约 93,648 个认证服务/1,288,590 次
Graph500 follower 的来源，冒称替换了全部约 571 万影子 PendingFill。
审计记录真实模型 parent ID/sequence 和 `model_data_upper`，
独立的物理 L1 `data_ready` 仍为 null；`clock_source=3` 表示选定父服务模型摘要。

开关 `FASTSIM_RETIRED_FILL_SELECTED_PARENT=1` 默认关闭。
Release、标准测试与旧区间污染冷 owner/认证 follower/已结束边/合法零时间/
缺身份拒绝反例通过；封存 `snapshots/i1-selected-parent-v1/`，SHA-256
`a33a50d9177e165ac11bc53ac2d8076638b3e460c1658ef033ba29b52f61d26c`。
2,256 项保护文件无变化；五例开/关回放已启动，尚无推广结论。

已准备独立 `run_observer_pairs.py`，要求五例守恒与三个退休事件逐例 AE 不恶化
两个门禁通过后才运行 30 次串行 Zstd/XZ 配对；AB/BA 交替、记录主机状态和资源，
报告配对对数比的 95% 区间。
为分别量化模型吞吐与重复全输入 hash 成本，预定在两版每次调用中同样关闭
`--perf-cache-input-identity`，由已验收输入在整个 campaign 前后完整独立 SHA-256
绑定，保留每次预测守恒；这只测量**不含逐调用 hash 的模型 runtime**。
campaign 前后 hash 总开销单列，不能冒称原 CLI 端到端吞吐，也不能给缺失
privilege-partitioned warmup 的 user 端到端吞吐填入数值。
目前没有任何候选完成该 30 次配对。

### I1/B2 五例结果与保守时间边界修正

I1 和 B2 v1 的关闭修复回放均已完成，五例与冻结版预测差异全部为 0。
I1 开启后，五例 CPU/CPI/cache/request 严格不变，四核宏/UOP 和三层宏 OR
全量守恒；[守恒检查](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/i1-original-invariants.json)
通过。L1 仅 Zstd +42、Graph500 +1,594，其他事件/负载全部不变。
两例原本均多报，因此 I1 未满足逐例不恶化，保持关闭。

B2 v1 开启后，全物理人口 forwarded UOP 增量依次为
−67,591 / −313,532 / −77,512 / −482 / −24,584；
与任务退休宏 forwarding 不是相同单位。该候选恢复了一部分跨接受前缀的请求，
但五例 CPI 与退休事件的组合结果未通过推广条件。
下面沿用原始同一 FST 的封存维护 CPI 参考；新候选独立 native 评分仍在执行。
AE 单位 cycles/macro：

| 负载 | gem5 CPI | 冻结 CPI | B2 v1 CPI | 冻结 CPI AE | B2 v1 CPI AE | B2 v1 相对误差 |
|---|---:|---:|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 1.159857 | 0.063733 | 0.046193 | −3.83% |
| XZ | 0.575297 | 0.601359 | 0.602236 | 0.026062 | 0.026939 | +4.68% |
| FLAC | 0.284584 | 0.318433 | 0.319345 | 0.033849 | 0.034761 | +12.21% |
| Graph500 | 0.966422 | 0.835896 | 0.836353 | 0.130526 | 0.130069 | −13.46% |
| Marian | 2.364997 | 3.068823 | 3.098006 | 0.703826 | 0.733009 | +30.99% |
| 五例等权 | — | — | — | **0.191599** | **0.194194** | MAPE **13.036057%** |

B2 退休 L1 增量为 +6,685 / +58 / −9 / −3,448 / −46；
L2 为 −9 / −2 / +2 / −6 / −6；L3 仅 Graph500 +3，其余不变。
Zstd L1、FLAC L1/L2、Graph500 L3、Marian L1 的已有绝对次数误差恶化。
这些为新预测相对封存维护事实的**暂定**比较；XZ/Marian 每层各 1 个原生 unknown
与原拒绝状态保留，不能称完整 native 精度通过。
结构化比较核对输入身份、task、参与核、逐核/逐范围人口及来源文件哈希，
并在 `[known, known + unknown]` 两端检查绝对误差，不替 unknown 填零：
[I1 比较](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/i1-original-comparison.json)；
[B2 比较](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/b2-original-comparison.json)。

B2 v1 静态复查发现一处保守边界缺口：消费者 Q16 下界与 release 上界都向上取整，
可能将 89.99 周期的 consumer 与 90 周期的排空边视为相等，过早取消 forwarding。
工作副本已改成消费者下界向下取整、release 上界仍向上取整；
增加边前一个 Q16 单位、恰好边界和合法周期零反例，正在构建测试与封存 B2 v2。
v1 快照、原始回放与评分均保留；该修正不自动消除 v1 的上述组合回归。

warmup 边界复查：`prepare_measurement_phase` 保留绝对模型时钟、SQ/response 状态，
只推进原 gap 到共同 barrier 并重置统计；新增 release 表保留同一 reference 域，
未在边界清空，也未重用局部序号作跨阶段身份。
实际回放没有出现跨 warmup publication 的 source-order 断言或 forwarding 计数下溢。
同前缀未知 owner 与批处理未导出的排空边仍未覆盖，不能称全量 lifetime 修复。

### SPR v2 新源与吞吐脚本绑定

带 SPR 字段的 Graph500 完整新源已封存，capture/prepare 均返回 0：
41,876 个 native member，四核，48,633 行审计人口，closure 完整无截断/省略。
其中 41,874 个 member 的旧缓存诊断 `resolved=true`；这不是 workload-task 的
三事件零 unknown 认证，须以维护评分的范围/资格检查为准。
已启动冻结版/A0 在该同一新源上的回放及维护评分、严格成员配对；
不能与此前缺字段的 `native-members/` 新源混用或相减。

吞吐脚本已补齐递归 `config.include` 身份：Zstd 实际读取 **11 个配置文件**，
逐调用与 campaign 结束复核完整 include 链及 manifest，沿用维护身份工具核验
全部 FST/companions/plan。清除继承的所有 `FASTSIM_*AUDIT*` 环境变量，
两版均设置项目内 TMP；每版/负载增加一次独立、不计入 30 正式配对的进程预热。
候选二进制与精度/守恒报告的预测哈希相互绑定，检查测试 CPU 上仍存活的
模型/native 评分进程，避免与诊断回放竞争时发布吞吐结论。
脚本语法、真实 11 层配置解析和诊断环境清理检查通过。
目前 I1/B2 均未通过精度推广门禁，尚未启动其 30 次推广吞吐验收。

本轮仍在实施。未合并、提交或推送，生产与冻结模型保留。

B2 v2 后续构建、标准测试与三项 lifetime 定向测试全部通过，封存 SHA-256
`53d228d3b95e67d08b9e8fbccb354537af891334cc68d9a96a91751c3fb3ae0e`。
保护核查 2,256 项无变化；五例开/关回放已启动。
B2 v1 的五例任务 LLC tag-miss 预测全部不变，不能声称该部分 forwarding
修复已经消除 Marian 的 LLC 请求少报。新增普通访问与 tag-miss 不是一一对应：
XZ 任务 L1D/private-L2 tag miss 反而减少 825/626；Marian 增加 736/778。
这些是功能模型预测增量，没有将其解释为 native 事务根因贡献率。

SPR v2 完整新源的 Zstd、XZ、Marian 也已封存，均 capture/prepare 返回 0，
closure 完整且四核出现。审计 member 分别为 55,787 / 27,504 / 29,023；
原日志中的 `resolved` 是旧缓存诊断资格，不能用它替代 SPR 事件资格。
冻结/A0 三例同源回放已启动，随后独立检查硬件身份和 native 来源/人口，
再由 SPR 字段单独配对。该三例来源评分暂不请求全量退休事件精度，
以便保留“来源通过但全量事件未知”与“来源失败”的区别；
完整三事件评分仍是另一个待完成门禁。FLAC 尚未收到完整封存结果。

离线工具复查发现 `pair()` 曾用旧 `resolved=false` 覆盖新的
`spr_unknown_mask`，将已有 SPR 资格的成员强行放进 unknown。
已保留原工具，另建
`source/tools/pair_spr_retired_fill_audit_v2.py`，只以 SPR 自身 unknown 位分类；
分层保留 native exact/estimated/unknown，记录与旧资格的分歧。
增加原生已知负成员、旧已知但 SPR 未知、部分 SPR 资格、原生样本遗漏四个反例，
并将原五项配对保障测试用于修正版，**9 项通过**。
已知/未知样本不会因旧资格混用被静默删除；split 仍明确排除，
因为按 physical-line 采样的日志不能证明所有 fragment 的完整 UOP OR。
尚未生成通过严格来源绑定的新 v2 配对报告。
该工具修正不改变任何模型预测或 CPI，不算模型精度收益。

### B2 v2 结果与首个通过严格绑定的 SPR 成员配对

B2 v2 五例开/关回放已完成；关闭时五例全部预测与冻结版一致。
开启后的五例预测与 B2 v1 一致，Q16 下界取整修正没有改变这批输入的结果。
五例 CPI MAE 仍为 **0.194194 cycles/macro**，MAPE **13.036057%**；
其逐例 CPI AE 与退休事件回归见上表和
[v2 原始比较](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/b2-v2-original-comparison.json)。
因此 B2 v2 继续关闭，独立维护原生评分已启动，未运行推广吞吐门禁。

SPR v2 五例完整采集均已封存，capture/prepare 返回 0。
FLAC 最后完成，19,773 个 native member，四核，56,204 行且 closure 完整；
冻结/A0 同源回放已完成，预测差异为 0，来源评分和 v2 成员配对继续执行。

Graph500 新源已通过独立硬件身份、维护来源/人口检查与全量三事件参考检查；
`workload_task` 三事件 `reference_unknown=0`，`primary_complete=true`。
仍保留原生显式服务估计和未认证硬件映射，维护事件状态仍为 `incomplete-evidence`。
冻结/A0 对该新源预测一致。此新源独立 CPI 为 gem5 **0.966441**、
冻结/A0 **0.835500**、AE **0.130941 cycles/macro**、相对误差 **−13.5488%**；
不能将其与原源 Graph500 CPI 之差当修复收益，也不并入原五例 MAE。

Graph500 v1 和修正后的 v2 严格逐成员配对均通过完整 FST/companions、
capture binding、四核人口、PC/flags/phase 和无截断 closure 门禁。
v2 采样结果：

| 层级 | TP | FP | FN | TN | unknown |
|---|---:|---:|---:|---:|---:|
| L1 | 18,070 | 8,855 | 2,053 | 10,470 | 0 |
| L2 | 513 | 568 | 568 | 37,799 | 0 |
| L3 | 513 | 568 | 568 | 37,799 | 0 |

单位为采样的非 split、非 atomic 退休 read-UOP cache-line member，
不是全量宏指令事件；不外推为全量根因比例。
L1 FP 全部落在原生普通 data 路径；FN 中 1,992 个落在原生 demand follower /
L1 PendingFill，另外 61 个为普通 data。L2/L3 的计数相抵掩盖了具体 load 的 owner 错位：
core 3、同线 `538796902`，模型把 ordinal `699625` 判为 mask 7，
把 `699634` 判为 mask 1；native 恰好相反，后者原生准入早于前者。
该事实证明需要核查准入顺序和服务角色，不能把总数接近当关联正确。

证据：[Graph500 全量维护评分](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/evaluation/spr-v2-graph500-a0-native/native/cases/c04-854.graph500_s/validation.json)；
[严格 v2 配对](file:///data00/yinhaolang/FastSim/tmp/r01-cache-repair-exclusive-20261111/spr-v2-graph500-a0/c04-854.graph500_s.member-pairing-v2.json)。

### C0：请求角色证据建设，尚不是请求行为修复

从 A0 单独派生 `c0-request-ledger/`，增加默认关闭的
`FASTSIM_REQUEST_SERVICE_AUDIT`。在最终接受前缀中按 load、store、atomic、
software-prefetch、instruction、translation 输出服务描述符，
保留实际 shared access 给出的 LLC lookup、prepare、alias、carry 和 DRAM 描述符来源。
没有 lookup 的 forwarding/alias/特殊直接服务显式 unavailable；
没有私有 parent identity 的路径保持 null。
按核/任务的全量角色桶和有限采样 JSONL 分开；样本截断不删除全量人口。

该人口单位是**接受的功能内存 fragment**。carry 可以描述更早发生的 lookup，
alias 没有新 lookup；因此不能把描述符总数冒称唯一原生请求总数或 PMU。
本阶段用于支持 C1/C2 的角色/路径核查，不宣称已闭合 Marian 的 LLC 请求缺口。

Release parallel-LTO 构建、标准 `fastsim_tests` 和 carry/alias/unknown/截断守恒
定向测试通过。候选封存为 `snapshots/c0-request-services-v1/`，
二进制 SHA-256
`b5f38498bcd3353ccb3f35fd6296c605c886c52ecf35cc01f385e81b48c9b400`；
2,256 项生产/冻结保护文件无变化。五例诊断开/关回放已启动；
必须先证明全部预测中性，再使用角色桶进一步分析。未合并、提交或推送。

### 用户追问后台进程后的减负

用户指出后台进程过多。复查时存在 9 个 native 评分进程、10 个 FastSim 回放进程。
过度并行的原因是：已被否决的单项候选仍继续各自执行完整 native 扫描，
多组反复扫描同一原五例；B2 v2 预测已与 v1 一致仍追加独立评分。
这是验证调度过宽，不能归因于本次仍必要的新源核验。
native 评分每组当时实测约占一核，`--jobs 5` 没有表现出五核有效并行。

已停止 7 组已否决候选的收尾评分：A1 store-seed、T1、T2、I1、B1 overlap、
B2 v1、B2 v2。既有快照、完整回放、暂定比较、已完成的评分及中断日志均保留；
这些评分不再列为等待验收，不自动重启，不把中断结果改称通过。
停止后复查仅剩 **2 个 native 评分进程**，分别核验 SPR v2
Zstd/XZ/Marian 三例新源和 FLAC 新源，属于首次来源检查。
当时另有 **8 个 FastSim 回放进程**：C0 诊断开/关各 3 例，
Graph500 T1/T2 各 1 例；C0 Marian、FLAC 已结束。
这些回放用于具体新角色证据或已配对成员的时钟反例，不是已否决版本的推广验收。

新增离线 `pair_observer_against_qualified_source.py` 只用于中性观察器的成员诊断：
必须保持 CPU/cache/request 和全部人口不变，重新核对维护报告 fingerprint、
全部已绑定文件 SHA-256、完整 FST 身份/closure、capture binding、采样和预测文件。
通过维护比较函数复查逐核/逐范围宏与 UOP 人口，拒绝 forwarding/request 行为变化。
A0 同一来源的控制运行复现严格 v2 配对结果，允许的观察字段与请求变化拒绝反例通过。
该工具没有生成新的 native 评分报告或全量 PMU/吞吐验收结果，不能用来推广候选。

后续只为仍有推广可能的候选执行完整精度门禁；被否决候选用保存证据完成诊断，
不再启动整批重复评分。模型修复仍未验收，30 次串行吞吐配对尚未开始。

### 最新回放收齐后的状态

C0 五例诊断关闭与开启回放均已完成，全部模型预测与冻结版差异为 0。
完整请求描述符来源、四核 accepted raw-source closure、全量桶守恒及采样绑定检查
通过，见 `c0-original-invariants.json`。这是中性诊断建设通过，
没有改善 CPI、退休 cache miss 或 native 请求误差，吞吐仍未测量。

Graph500 新 SPR 源上的 T1/T2 单例反例回放也已完成。
同源冻结/A0、T1、T2 的 CPI 均为 0.835500049，
相对于独立 native 0.966441091 的 AE 均为 0.130941042 cycles/macro，
相对误差均为 −13.548787%。L1 绝对次数误差分别为
1,368,554 / 1,607,250 / 1,368,554；
T1 增加 238,696 次误差，T2 在该新源上的三层点估计全部未变。
这是同源新预测对已封存维护参考的比较，没有启动或冒称新的候选 native 评分。
不与原 Graph500 源的变化合并成收益。

用户再次授权关闭无意义审计后复查：七组已停止的候选评分均未存活，
FastSim 回放进程为 0；仅剩 SPR v2 Zstd/XZ/Marian 与 FLAC 两组首次来源评分，
各自一个父调度进程和一个评分进程。没有重启被拒绝候选的整批审计。
后续默认配对驱动已切换到修正 SPR 资格的 v2 工具及独立 `member-pairing-v2`
输出，避免评分结束后自动启动旧版资格混用配对；旧版工具和结果保留。

当前优化结论仍为：T1/T2/I1 未通过退休事件逐例不恶化；B1 CPI 五例均值略好
（MAE 0.189072、MAPE 12.984994%）但逐例/退休事件门禁未过；
B2 v2 MAE 0.194194、MAPE 13.036057%，劣于冻结版。
本轮还没有同时通过精度、覆盖与吞吐条件的可推广修复。
