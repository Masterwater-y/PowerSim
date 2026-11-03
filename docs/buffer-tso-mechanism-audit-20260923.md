# Buffer TSO 首轮机制审计：已执行的排查与证据

**结论：当前 demand-only 修复仍未与 gem5 完整对齐。** 补一次队头 cache access 修复了缺失请求的一部分，但不能替代 commit→admission、optional prepare、mandatory demand、callback→下一次 admission 的独立生命周期。本轮完成了只读观测、原生重采和事件配对，修复了一个 native 日志校验器错误；没有修改物理时序、资源调度或 epoch 回退。

执行计划：[buffer-tso-mechanism-audit-plan-20260923.md](buffer-tso-mechanism-audit-plan-20260923.md)。上一版物理模型与完整 8 案例结果：[buffer-tso-head-demand-only-20260923.md](buffer-tso-head-demand-only-20260923.md)。以下结论不以 CPI 接近程度作为机制正确的判据。

## 1. 本轮实际实现

- `tools/collect_tail_timing.py` 支持现代 `command.json`、直接 `--source-case`、相对 manifest 路径，保留旧日志接口；固定 debug 参数插入后的配置脚本指纹，记录动态库环境。
- FastSim frontier sample 增加原队头、在途接管、permission estimate 后额外 demand 三种角色，记录原服务时长、估算 permission-ready 和 canonical issue/response。canonical 字段明确使用 reference clock，**不是实际 prepare admission/response**。
- 新增 `tools/audit_buffer_tso_lifecycle.py`：核对硬件、ROI/warmup、完整指令身份前缀、维护 validator 的 manifest/输出散列、普通 store 的 PA line、native thread/core、单条请求及重复样本；通过维护 native auditor 校验 JSONL 和 summary 的来源、守恒、drain 与人口一致性。
- 审计 ON/OFF 比较完整 ROI 的 CPI 分子分母、线程人口、缓存、SQ/IQ/ROB、Sequencer 和 buffer demand 计数。
- 修复 `audit_p1_native_response_sideband.py` 把所有 `response_count=0` 记录误认作 no-Ruby terminal 的问题。合法 pending store 可以先有 hierarchy 事件，稍后才有 response；无 admission 或标记为 no-Ruby terminal 的非空 hierarchy 仍拒绝。

最后一项有真实见证：NAMD core 0 的 native JSONL 第 21259 行，`inst_seq_num=801179`，已 admission=1、L1 miss/L2 hit、response=0；第 21264 行是同身份的正常 resolution，response=1。旧校验器在 commit 行提前报错。回归覆盖合法延迟响应、真正 no-Ruby terminal，以及两个非法 pending→resolution 反例。

## 2. 来源和验证边界

原始 source：`tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/` 中 NAMD、Graph500、TeaLeaf C4。完整 ROI 沿用原始 FST、warmup slice、user-plus-kernel 和 first-core common-end，每端使用自己的 macro-instruction 分母。

重采使用原 checkpoint、冻结命令和原硬件配置，仅将 user target 改为 100,000，开启 JSONL native response。它是**短窗口诊断，不替换原始完整 CPI 参考**。原 gem5 SHA 为 `74d101122e4775f3cbf0c489af403ccbddb201a9a738ec13f3125edfa51d1e7a`，重采二进制为 `468205d2b8f0cbd3ae3b23951e16ab4c48296621ab31f8187a4af14a635e398f`，所以显式使用 `--diagnostic-gem5`。没有把不同二进制默认当作等价。

两组硬件身份各 3/3 通过，几何及 Ruby latency 配置相同。nominal 3 GHz 按 gem5 tick 量化对应 333 ticks/cycle。初次启动因运行库缺 `GLIBCXX_3.4.30` 失败；r2 使用 `/root/miniconda3/lib/libstdc++.so.6.0.34`，路径和散列单列保留。没有改 gem5 二进制。

诊断配对限 core 0。身份前缀从 ordinal 0 开始，包含 warmup，逐条核对 PC、PA/size/type、branch、privilege、ASID；辅助 syscall 明确核对，不删除或移动序号。此门不声称验证全部 dependency sidecar，也不把 core 0 推广为全部核心。

| 负载 | 测量窗口的 ROI record offset（含端点） | 核对到的前缀记录数 | 单次普通 store 配对数 | FastSim 原队头 / 在途接管 / 额外 demand |
|---|---:|---:|---:|---:|
| NAMD C4 | 0–4096 | 647,676 | 64 | 64 / 0 / 0 |
| Graph500 C4 | 0–4096 | 11,396,519 | 342 | 339 / 3 / 0 |
| TeaLeaf C4 | 74,433–78,528 | 2,096,205 | 76 | 34 / 1 / 41 |

TeaLeaf 最初的 0–4096 窗口没有 store。检查原生记录发现 core 0 首条 store 在 offset 74,561，因而增加了覆盖该位置的窗口；没有按 CPI 选择窗口。

## 3. 已有事件证据说明了什么

### 3.1 不能只改 cache duration：发送起点也未对齐

NAMD 的 64 条 store 均为 FastSim 原队头，native commit→Sequencer admission 均为 2 周期，FastSim retire→send 均为 0。62 条 native L1 hit 的 admission→response 是 1 周期，FastSim send→SQ release 是 2；另外 2 条 native L2 hit 为 5 周期，FastSim 为 6。

| NAMD 单 store 阶段 | gem5 | 当前 FastSim |
|---|---:|---:|
| commit/retire → demand admission/send，64 条 | 2 | 0 |
| 本地 L1 服务，62 条 | 1 | 2 |
| 上述本地 store 从 commit/retire 到响应/释放 | 3 | 2 |

这是阶段语义差异，不能把 `2→1` 当成独立修复后就宣布对齐。FastSim 代码已有 commit→Ruby=2、response→next admission=1 的普通 store 边，但当前配置的 `store_post_commit_request=false`，buffer lifecycle 没有启用这两条边。后一条边仍需按 FIFO 前驱逐项验证，不能从本表推导所有阻塞请求的延迟。

同次配置 `commitToIEWDelay=1`、L0 `mandatory_queue_latency=1`。本地 store 的源码链是 `mandatoryQueueLatency → L0 hh_store_hit/writeCallback → RubyPort same-tick schedTimingResp → LSQ completeStore`；普通 store 不经过 load consumer writeback。FastSim 当前的 `l1d.hit_latency=2` 是既有路径近似，不是已认证的独立 mandatory-store 服务值。

代码定位：`src/simulator.cpp` 的 `store_commit_to_ruby_admission_cycles()`、`store_response_to_next_admission_cycles()`、`retire_ready_prefix_and_drain`；本地 gem5 `Sequencer.cc:1218`、`MESI_Three_Level-L0cache.sm:798`、`RubyPort.cc:714`、`lsq_unit.cc:105/1258`。各边必须一起对齐，不以 CPI 变化决定取舍。

### 3.2 cache 状态也不是天然相同

Graph500 core 0 的首条测量 store（ordinal `11392422`、native seq `22533564`、PC `4232699`、physical line `3744960`），native L1 tag miss、private L2 hit、服务 5 周期；FastSim 原队头服务为 2 周期。这个差异在尚未讨论本条额外 demand 前就存在。

这证明该事件的层级/服务选择不同，**还不能确定是哪次 eviction、warmup 状态或 canonical replay 排序造成的**。下一步应追该 line 的最后驻留/淘汰事件，不应给全负载统一加时延。

### 3.3 剩余 CPI 低估不能全部归到缺一次 demand

- NAMD 首窗口 64 条均为原队头，没有额外 prepare 完成后的 demand 分支。到 offset 4096，FastSim 从 ROI 起累计 retire 为 1088 周期，native active 为 1194，仍提前 106 周期。只调整这批 cache service 不能解释所有差异。
- TeaLeaf 观测窗口已实际执行 41 次额外 demand；其中 native L1 hit 为 1 周期，FastSim 服务为 2。9 条长服务原队头在 FastSim 反而更长，例如 ordinal `2092410` 的 native memory-read 路径服务 185 周期、FastSim 257。到 offset 78528，FastSim 累计 retire 17736 周期、native active 19286，仍提前 1550 周期。服务本身偏长和总进度偏快可以并存，下一轮必须检查发射、load response→consumer、依赖链及 retire 暴露。
- Graph500 本窗口有 342 条 store，但 FastSim 只有 3 条在途接管、没有额外 demand。该窗口需要先解释原始服务和驻留差异；这不代表其他核心或完整 ROI 都没有 prepared follower。

上述累计差仅是配对 milestone 的诊断，native 此处 active 上下界相同。它不是完整 ROI CPI，也不是各阶段的可加总归因。

## 4. 哪些环节仍未获得认证

native v7 逐请求日志有 mandatory demand 的 admission/response 和原生层级事件，但没有逐 store 的 optional prepare admission、response、drop、takeover 身份。`lsq_unit.cc` 新建 prepare Request 时没有传递该 store 的 InstSeqNum；汇总 `storePrepareIssued/Blocked` 不能补全这个身份。

当前 FastSim 仍保留三项明确近似：

1. permission-ready 用 canonical service/retire 下界估算，不是真正 optional queue 的响应。
2. 原有资源计账未区分 gem5 prepare 和 mandatory demand；本轮没有重新引入已撤销的调度改动。
3. 额外 demand 使用 canonical batch 末端缓存状态，尚未证明等价于该 demand 时刻的驻留、权限和替换次序。

因此所有配对产物均为 `full_lifecycle_verified=false`。当前证据足以否定“再跑一次相同 cache 延迟就已机制正确”，不足以认证完整 prepare/demand 协议，也不能量化三项近似分别贡献了多少完整 CPI 误差。

后续物理修改顺序已收窄：先对齐 commit→mandatory admission、store 专用 callback 与 FIFO 下一次 admission；再给 native prepare 添加同 store 身份及各阶段事件，验证 dropped/merged/ready 的状态迁移；对 Graph500 见证追 cache line 驻留世代；对 TeaLeaf/NAMD 的无 prepared-demand 区段追 load/依赖/retire。每项用单独反例验收，CPI 允许回退，不添加负载常数。

## 5. 完整 ROI 复测：本轮不改变预测

本轮审计与上一版 demand-only 的完整 ROI CPI、线程人口、核心 cache/queue/Sequencer/buffer counters 相等。经维护 validator 重评分，审计和控制均 3/3 passed、0 failed、0 pending；此 passed 是来源与守恒门，不是精度阈值或完整机制认证。CPI 绝对误差单位为 cycles/macro instruction。

| C4 负载 | gem5 CPI | FastSim CPI | signed 相对误差 | CPI 绝对误差 | 本轮 CPI 改变量 |
|---|---:|---:|---:|---:|---:|
| NAMD | 0.479341 | 0.389498 | −18.7430% | 0.089843 | 0 |
| Graph500 | 1.305529 | 1.180965 | −9.5413% | 0.124564 | 0 |
| TeaLeaf | 1.004183 | 0.860937 | −14.2650% | 0.143247 | 0 |
| 三项等权聚合 | — | — | MAPE 14.1831% | CPI MAE 0.119218 | 0 |

本轮没有重新测 LBM/Stockfish，不把新观测工具算作精度提升；完整 8 项历史表仍见上一版报告。

## 6. 验证与复现

构建与 `./build/fastsim_tests` 通过。Python 相关测试 61 项：配对门禁 15、native sideband 19、native validator 11、采集 5、既有 timing scope 11。独立审查提出的 native 守恒、线程/数据行身份、重复 sequence、旧输出散列和 C16/C32 文件名门禁均已补齐，并保存回归。

产物根目录：`tmp/buffer-tso-mechanism-audit-20260923/`。

- `source-identity.json`、`native-identity.json`：两个来源的身份。
- `native-*-r2/collection.json`、`completion.json`、`trace/oracle/`：重采命令、散列、完整 native 日志及汇总；`runtime-environment.json` 记录运行库。
- `audit-rescore/`、`control-rescore/`：完整 ROI 的维护 validator 报告与准确性表。
- `final-paired-*.json`：最终配对、身份前缀、native 源守恒、逐条 witness 和审计中性结果。
- `before/`、`changes.diff`、`fingerprints-final.json`、`build-final-2.log`、`tests-final-2.log`：本轮代码范围和验证证据。

复现单例配对（需先运行维护 validator 的 `--source-root`/`--stats-dir` 重评分，以绑定现存 stats 散列）：

```bash
python3 tools/audit_buffer_tso_lifecycle.py \
  --collection "$AUDIT_ROOT/native-857.namd_s-r2" \
  --native-result "$AUDIT_ROOT/identity-view/uarch-baseline-c04-857.namd_s" \
  --audit-stats "$AUDIT_ROOT/audit-stats/uarch-baseline-c04-857.namd_s.json" \
  --control-stats "$AUDIT_ROOT/control-stats/uarch-baseline-c04-857.namd_s.json" \
  --audit-validation "$AUDIT_ROOT/audit-rescore/cases/uarch-baseline-c04-857.namd_s/validation.json" \
  --control-validation "$AUDIT_ROOT/control-rescore/cases/uarch-baseline-c04-857.namd_s/validation.json" \
  --core 0 --output "$AUDIT_ROOT/namd-pairs-recheck.json"
```

其中 `AUDIT_ROOT` 指向上述产物根目录。identity view 只连接对应重采的 config/profile/oracle，派生 request 明确记录 diagnostic 二进制和短 target，不修改原始案例。
