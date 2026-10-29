# StoreRequestEngine 接入与三负载验证（2026-10-21）

状态：新模块已经接入生产回放路径，显式开启 `core.store_request_engine=true`；默认旧路径保留。三例均已执行：SQLite和XZ完成来源/人口/native验证，Marian在闭合门禁失败，无有效CPI。**SQLite略有改善，XZ退化，Marian不可用；本次不具备推广默认或精度验收通过的条件。**

## 实测结果

以下全部新路径数字来自同一冻结 `fastsim-candidate-phase`，SHA256 `d72c4af3e2481abf10fc38ea5468c2c82ffec44539849786b73960471223781c`。相对误差为 `(FastSim−gem5)/gem5`；绝对误差为 `abs(FastSim CPI−gem5 CPI)`，单位cycles/macro。Marian失败不赋零，不沿用旧预测。

| 负载 | gem5 CPI | 旧default CPI | 旧相对误差 | 旧绝对误差 | 新engine CPI | 新相对误差 | 新绝对误差 |
|---|---:|---:|---:|---:|---:|---:|---:|
| sqlite | 1.000924991 | 0.881132259 | -11.9682% | 0.119792732 | 0.906444664 | -9.4393% | 0.094480327 |
| xz | 1.081955043 | 1.527190422 | +41.1510% | 0.445235379 | 1.660043563 | +53.4300% | 0.578088519 |
| marian | 2.665483924 | 3.170054366 | +18.9298% | 0.504570442 | 无有效值 | 无有效值 | 无有效值 |

只对两边均完成评分的SQLite/XZ配对集合聚合：

| 同一两例集合 | CPI MAPE | CPI MAE（cycles/macro） |
|---|---:|---:|
| 旧default | 26.5596% | 0.282514056 |
| 新engine | 31.4346% | 0.336284423 |

这不是三例平均，也不是WAPE。旧default三例都完成，候选2 passed/1 failed。validator的passed只表示所列来源/一致性检查通过，不代表误差达标。

elapsed是目标运行时间，不是host replay耗时：

| 负载 | gem5 elapsed（ms） | 旧default（ms） | 新engine（ms） | 新elapsed相对误差 |
|---|---:|---:|---:|---:|
| sqlite | 1.966026329 | 1.730720880 | 1.780440444 | -9.4396% |
| xz | 1.822066766 | 2.571865560 | 2.795596938 | +53.4300% |
| marian | 4.657838156 | 5.538989466 | 无有效值 | 无有效值 |

SQLite/XZ评分人口均为完整workload-task，双方宏指令分母分别为23,590,370和20,228,833，均相等。SQLite gem5/候选active cycles为23,612,190.879880 / 21,383,365；XZ为21,886,687.879880 / 33,580,744。Marian候选没有完成，不能核验最终预测人口或发布其elapsed。

为避免只选差的旧基线作比较，另保留**本轮之前的归档prepare候选**：其SQLite误差−12.2657%、绝对0.122770727；XZ误差−3.4345%、绝对0.037159429；Marian误差+17.1640%、绝对0.457503985。其源gem5 CPI与本表相同，但不是本次新模块测量，也不能把新XZ结果描述为超过历史最好方案。原JSON位于 `tmp/cpi-three-workload-repair-20261021/validation-{sqlite,xz,marian}-final-prepare/cases/*/validation.json`。

### Marian为什么没有数字

实际重放触发 `store request epoch did not reach a timing fixed point in 64 passes; no fallback`。同一phase二进制开启诊断后复现，优化扫描后的prefix二进制也复现。`debug-marian-phase.log`中core3、seq1528973的issue在5404604和5404606间反向变化，服务延迟250/251；seq1529005的issue在5404777和5404873间往返。部分早段store退休已固定，后续请求和退休仍变化；新诊断显示晚段普通store也参与振荡，不能归咎于NO_ACCESS单向传播或简单固定1拍相位差。

这证明当前epoch整体反馈未闭合，不足以唯一归因于某级cache、DRAM日历或某一仲裁边。下一步需记录首个非单调外部请求的path/level/资源阻塞者并提取短反例，修复core与backend请求顺序的因果闭合；不能提高pass上限、加入人为monotone floor、沿用某次试算或静默关闭prepare来制造CPI。

XZ虽完成闭合但CPI更差，独立机理回归不构成其准确性证明。当前事实只支持“真实接入但模型尚不够准”，没有完成对XZ高估来源的逐周期因果分解。

## 接入方式

IntervalCore继续产生依赖、执行与退休下界。Simulator的response阶段消费这些输出，合并真实访存与store生命周期事件；StoreRequestEngine决定optional/mandatory准入，调用实际SharedSystem服务，使用返回时间释放TBE或SQ，再反馈核心的SQ、TSO及退休时序。没有创建另一套前端，也没有读取gem5命中结果或响应时刻作为预测输入。

普通单data-fragment store交给engine；NO_ACCESS也进入同一FIFO，但不请求cache、不占TBE和cache端口。split/atomic沿既有外部通路，全部fragment完成后才解除扫描边界。hint、CPU mandatory和物理取权事务分别标识；prepare返回不释放SQ，真正write response才释放。

epoch内先试算，若核心退休/外部请求发送时刻改变，完整回滚backend、计数和PMU shadow后重算；64次不闭合则明确失败。没有关闭prepare、提高上限或用人为延迟floor强制通过。

设计与执行计划：

- [接入设计](superpowers/specs/2026-10-21-store-engine-integration-design.md)
- [实施计划](superpowers/plans/2026-10-21-store-engine-integration.md)
- [首阶段历史记录](store-request-engine-phase1-results-20261021.md)

## 实验身份与统计口径

复用SQLite `c04-708.sqlite_r`、XZ `c04-801.xz_s`、Marian `c04-872.marian_s`的冻结trace及采集边界，四核、八DRAM通道、buffer TSO，SQ=114。没有新增工作负载FST或checkpoint，没有调整ROI或缩短评分窗口。

独立 `validate_fs_oracle_identity.py` 验证3例、0 mismatch，分别保留config.ini、effective-target、profile身份。原路径和新路径均用维护入口 `tools/run_native_kernel_fastsim_validation.py --source-root ...`，原生cache参考由Ruby/SLICC审计产生，不采用path-class字段替代。

CPI统一使用 `workload-task` 的active cycles / macro instructions，契约 `task-oncpu-switch-entry-v1`；elapsed单列，不能用四核cycles之和代表运行时间。每例必须通过自身分母、逐核人口、共同终点、来源及native门禁，硬件perf等价仍未认证。

基线是本轮开始时冻结的dirty源码及二进制，不是较旧Git HEAD；隔离worktree叠加了当时实际工作源码。所有结果及命令保存在 `tmp/store-engine-integration-20261021/`，其 `cases.json` 给出完整路径，`baseline-source.json`、各候选source收据和review哈希记录版本身份。试验失败版本与最终评分版本分别保留，不能混用。

新配置在每例原default include链上显式开启engine并关闭旧prepare lifecycle，声明commit传播=1、L1/L2 store hit latency=1/5、cacheStorePorts=200、TBE=256、optional queue=0（无配置上限）。这些来自实际gem5配置，不是SPR资源认证。旧default虽声明store专用latency，旧通路仍使用generic L1/L2=2/6；新桥接实际消费1/5，因此变化不能全归因于生命周期抽象。三例使用相同规则，没有逐负载挑选开关。

## 验证中发现并修复的问题

独立审查关闭了旧private preview提前改变cache状态、回滚漏掉FB/deferred shadow、资源没有消费实际profile、同line重试重复计退休store、backend准入晚于TBE分配、有限TBE重试失去等待者以及外部请求等待没有进入核心可见延迟等问题。

实际XZ/Marian又暴露NO_ACCESS长链：旧做法把上一轮scan下界输入下一轮，每次只传播一段。256组结构反例可稳定复现；现将本地SQ项放入同一事件FIFO，同pass推进。特殊请求到后续store统一保留真实response+1边。另修复满SQ在同拍LocalComplete之前拒绝Retire的相位问题：本地释放后同拍重试，避免平添一拍。

维护回归覆盖长链SQ114、短链SQ2、pure-local不同epoch长度、双冷store重叠、证书开关中性、takeover、同line、有限资源及特殊请求边界。256组SQ2长依赖仍有超过64次闭合预算的已知限制，保持明确失败；不能以短回归外推任意窗口/参数。

## 当前验收边界

这是epoch闭合候选，跨epoch只保留既有前驱响应下界，不具备完整在线continuation或全部hint身份延续。SharedSystem仍有提前提交tag/目录的近似；按line的busy仲裁保护engine事务，用于近似Ruby瞬态等待/合并，不能声称完整原生协议。split/atomic尚未进入新的完整生命周期。

驻留证书不再决定新路径的硬件prepare资格，但证书批处理加速器尚未实现。nested store memory audit仍是旧估算；真实SQ生命周期读取外层store_send_cycle/store_sq_release_cycle。本轮不发布新PMU精度或吞吐验收，多个独立回放并行和host采样不满足安静主机吞吐测量条件。


## 最终代码与验证收据

主工作区保留全部原dirty修改，没有自动commit/push。隔离版本采用Release、IPO关闭；根目录保留原IPO设置，二者不是逐字节相同二进制。

实测phase之后只新增两处纯host活动范围扫描优化以及诊断过滤：allocations为序号前缀，完成为FIFO前缀，只扫描head到首个未allocate；accepted hint与pending事务仍保留全生命周期。没有改变事件顺序、Wake次数、serial、目标周期或资源。该版本为 `fastsim-candidate-prefix`，SHA256 `42fd21c5b0eb074c976b09cd04075e58ef49d83464c87b3ad3ad251784cce0cf`，对应最终根目录源码。SQLite完整配对的CPI/elapsed/PMU/trace人口/checks相同，8个独立短反例输出逐字节一致；Marian同一振荡复现。XZ表中数字来自phase，未将它伪称为prefix重新计时或完整重测。

| 项目 | 结果 | 本轮证据目录中的文件 |
|---|---|---|
| 根目录最终构建 `cmake --build build -- -j16` | exit 0 | `root-build-prefix.log` |
| 根目录完整 `fastsim_tests` | exit 0，all FastSim tests passed | `root-tests-prefix.log` |
| 7个专项target：engine/noaccess/allocation及3个既有PMU回归 | 全部exit 0 | `root-prefix-targets.json`、各target日志 |
| 生命周期组件ASan/UBSan及assertions | 通过，engine源码此后未改 | `sanitizer-local.log`、source收据 |
| 独立审查与8个结构反例 | 已复核；不替代真实负载门禁 | `review.md`、`review-final-prefix-debug.sha256` |
| 三例硬件身份 | 3例，0 mismatch | `identity.json` |
| 原default重放 | 3 passed，0 failed | `baseline-{sqlite,xz,marian}` |
| 候选phase重放 | 2 passed，1 failed | `validation-{sqlite,xz,marian}-phase` |
| 报告复算 | 配对2例MAPE/MAE，Marian null | `results-phase.json`、`summarize_validation.py` |
| SQLite host优化输出中性 | 五组目标字段完全相等 | `prefix-neutrality-sqlite.json` |

最终磁盘复查：FST共52,908,914,568 bytes（52.91 GB），低于100 GB上限；空闲441,427,243,008 bytes（441.43 GB）。本轮没有新增工作负载trace或checkpoint，测试小fixture与二进制/日志保留；没有为这次验证删除活动数据。详见 `storage-final.json`。
