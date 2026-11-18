# 功能 load 接收与事件重试：实施验证

**后续回退：** 用户已要求撤回失败实现；P0/P1/P2 的工作代码、专属配置和测试已移除并按冻结身份归档。新的默认关闭候选及验证见 [稀疏 load 批次下界](spr-sparse-load-pressure-validation-20261116.md)。下文保留原方案/实验历史，不能据此认为旧候选仍在当前代码中。


2026-11-16。P0/P1/P2 已作为默认关闭候选集成，标准构建与定向测试完成。按用户最新要求，Graph500 C4/C8 首轮验证已完成，十源吞吐任务已停止。两例候选 CPI 均恶化，P1/P2 时间核算相同；只新增 Graph500 C8 的一组吞吐对照并复用 C4 同组结果，详见 [Graph500 首轮验证](spr-functional-load-admission-retry-graph500-validation-20261116.md)。此前十源 quick CPI 已完成的结果保留在本文归档，不继续扩展。候选继续默认关闭。依据：[已授权设计](spr-functional-load-admission-retry-design-20261116.md)。

## 实现与边界

`FunctionalIssueCalendar` 维护每核 12 个物理端口、同类操作发射间隔、总 issue/load/store 槽位及稳定轮转。其资源描述来自功能操作属性和第一阶段 FU occupancy，没有读取 gem5 原生 grant、issue、response、retry 次数或 stall。搜索跳到已有预约结束／占用槽的下一事件，不扫描目标空闲周期；每个区间／槽容器的 live 上限为 65,536，超限显式失败，不丢弃成员。此上限不是硬件容量。

第一阶段保留下界，响应阶段日历接管实际预约。普通单片段、功能资格明确的 load 在成功接受之前预约 FU/issue；非内存普通 UOP 同样竞争该日历。P1 将接收槽等待放在成功发射之前；P2 对模型明确的 Sequencer 槽满记录一次失败发射，直接跳至 callback，再竞争成功发射。MSHR/翻译 readiness 属于尝试前下界，已接受 miss 不自动重试。失败发射不创建 cache/DRAM 事务、不增加退休人口。

P2 每个受支持成员最多增加一次失败预约及一次事件唤醒。这是当前容量日历上的有界近似，没有逐次复现 gem5 的全部失败尝试；拒绝、重试和唤醒计数相等是该实现的生命周期守恒，不能与原生 MemRead 发射数直接比较。P1/P2 也会重新安排非内存 UOP，精度变化应解释为整套接收与资源安排的效果。

成员接收容量继续使用既有 Sequencer calendar，请求成功后才计入原有额度生命周期；IQ/LQ、父服务、prepare/store 沿用现有路径。资源日历以修正后 dispatch 为水位，跨 chunk、proposal copy、move 和 commit 保留 live 状态；独立操作可填补早于先前长延迟操作的空位。

转发、跨线／多片段、NO_ACCESS、软件预取、MMIO、atomic、PTE／page-fault 等不进入普通 load 预约路径，沿用原行为并观察资源冲突。`functional_load_fallbacks` 统计经过非 page-walk 读事件分支的未支持成员，不能当作全部特殊操作人口。候选剩余冲突属于覆盖边界，不能写成全访问资源零冲突。

共享 cache/DRAM 仍是既有一次提交顺序，不重新分类、不重算跨核到达或行命中。`functional_shared_late_events/cycles/max_cycles` 比较修正后接受时间与本地时钟下的第一阶段共享 issue 起点，量化这一近似边界；它们是重叠事件累计量，不能加成 CPI 缺口或独立因果归因。

已完成的 Graph500 C4 回放中，P0 观察到 6,372,525 次资源冲突；P1/P2 均降至 60 次。P2 普通 load 接受 13,325,265 次，非普通读分支回退 4,618 次，拒绝／重试／唤醒各 4,889,244 次。P1/P2 的共享迟到事件均为 190,747，偏移累计 31,930,975 周期、最大 3,021 周期。冲突减少证明预约安排发生了改变，不证明 CPI 改善；共享迟到边界也没有因失败发射计数自动消失。

## 使用与回退

以下开关均默认关闭，配置和 CLI 均支持，retry 必须从属 admission：

```ini
core.functional_load_admission_audit = false
core.functional_load_admission = false
core.functional_load_retry = false
```

P0 仅开启 `functional_load_admission_audit`；P1 开启 admission、关闭 retry；P2 同时开启 admission/retry。源绑定的 M4 配置之后可包含 [候选 overlay](../tmp/sparse-load-pressure-20261116/before/configs/spr-functional-load-admission-candidate.cfg)，该文件本身不包含平台配置。使用独立最终候选 ELF：

```bash
source scripts/project_tmp.sh
export LD_LIBRARY_PATH=/opt/gcc-11/lib64:${LD_LIBRARY_PATH:-}
python3 scripts/run_high_frequency.py -- \
  tmp/load-admission-retry-20261116/final-build/fastsim simulate \
  --config SOURCE_BOUND_M4.cfg --manifest ORIGINAL_MANIFEST \
  --functional-load-admission true --functional-load-retry true
```

回退时将 admission、retry 同时设为 false，audit 可独立关闭；关闭 retry 只回到 P1。校验拒绝与旧 sparse-resource、paired-frontier、causal/block-transfer、load-bank/fill 等竞争求解器同时开启，防止重复预约。M1–M4 profiles、evaluation alias 和安装 ELF 不推进；本次不命名为 M5、不提交或推送。

## 身份与测试

| 身份 | SHA-256 |
|---|---|
| 冻结并恢复安装的 M4 ELF | `d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9` |
| 最终严格 reader 候选 ELF | `7f4fa6e98fde15c94d1e91f905882b2572c9e64c0a460aefa9af20e5db7100ab` |
| 同模型端到端计时 reader ELF | `dd7815cac8513c58ed76dbbd6de533d758110085abe1d85cfcb4bf13a40c948c` |

九个候选源文件在集成前核对原工作树哈希，集成后与 [冻结清单](../tmp/load-admission-retry-20261116/implementation-freeze.json) 完全相同。早期 ELF `19c6e3…` 的回放已停止，保留原日志但不纳入最终评分。

标准 `cmake --build build -- -j16`、直接 `./build/fastsim_tests`、`./build/fastsim_functional_load_admission_tests` 均 returncode 0。后者覆盖十亿周期区间跳转、跨 op 共享端口、同 op interval、同时唤醒带宽、独立空位、复制隔离、水位拒绝、跨 chunk、拆分回退、同线循环、load→ALU 依赖、人口／cache 访问守恒、冷 miss 不产生 retry；小容量场景拒绝、重试、唤醒各 115。集成构建候选另存，随后恢复安装 M4，见 [构建／测试／恢复收据](../tmp/load-admission-retry-20261116/integration-check.json) 和对应 `integration-0/1/2.log`。

多负载汇总额外检查每个核的宏指令、UOP 与 memory-access 人口，以及全量 memory access、Sequencer request 和 DTLB access 守恒。审计模式的完整 `time_accounting` 和功能 PMU 计数须与 OFF 相同，OFF／审计的完整 `time_accounting` 须与冻结 M4 相同。审计／候选的发射人口满足：

```text
functional_issue_observed + functional_issue_reservations
  = 全部实际执行 UOP − rename 消除 UOP + functional_load_rejections
functional_load_rejections = functional_load_retry_attempts = functional_load_wakeups
```

上述 UOP 人口包含实际执行的测量物理流，不能用吞吐分子的 measurement user UOP 替代。守恒检查针对输入与模型事务，未将它写成原生缓存命中或 PMU 精度通过。

## 已归档的多负载精度

使用维护入口 `run_native_kernel_fastsim_validation.py --validation-mode quick --source-root … --stats-dir … --expected-cases 10`，逐核 task macro／功能 UOP、来源与退休完成边界独立核对。OFF／审计／P1／P2 共 40 个回放 returncode 均为 0，四组各 10/10 CPI scored、failed/pending 均为 0，前述人口／事务及发射守恒全部成立。目标身份另用 `validate_fs_oracle_identity.py`，10/10 valid、无 mismatch；这不等于模型精度或硬件认证。

下表报告全部十例 gem5/M4/P1/P2 CPI、带符号相对误差及 `abs(FastSim−gem5)`；绝对误差／MAE 单位均为 cycles/macro。等权 MAPE 与 WAPE 不混用，并单列 C4、C8、不含 Marian 和 Graph500，保留全部恶化项。quick 复用保存的独立 task-clock 证据，不构成新的严格完整性审计；本轮 PMU 与真实 SPR perf 等价性为 unavailable，formal passes 为 0。

每例 CPI 由参与核的 task active cycles 总和除以 task 退休宏指令总和得到，不能平均逐核 CPI。表中 AE、RE 和范围内等权聚合定义为：

$$
\mathrm{AE}_i=|\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}|,\qquad
\mathrm{RE}_i=\frac{\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}}{\mathrm{CPI}_{G,i}},
$$

$$
\mathrm{MAE}=\frac{1}{N}\sum_i\mathrm{AE}_i,\qquad
\mathrm{MAPE}=\frac{100\%}{N}\sum_i|\mathrm{RE}_i|.
$$

“改善／恶化”按同一来源相对 M4 的 AE 减少／增加判定。保持任务、逐核人口与边界相同的对照用于判断候选效果；资源冲突、重试或共享偏移累计量没有被换算为独立 CPI 贡献。

| 来源 | gem5 CPI | M4 CPI / AE / RE | P1 CPI / AE / RE | P2 CPI / AE / RE |
| --- | --- | --- | --- | --- |
| c04-777.zstd_r | 1.206049 | 1.200836 / 0.005214 / -0.432% | 1.187357 / 0.018692 / -1.550% | 1.187357 / 0.018692 / -1.550% |
| c04-801.xz_s | 0.575297 | 0.603435 / 0.028138 / +4.891% | 0.599399 / 0.024102 / +4.189% | 0.599399 / 0.024102 / +4.189% |
| c04-817.flac_s | 0.284584 | 0.294890 / 0.010306 / +3.621% | 0.294322 / 0.009737 / +3.422% | 0.294322 / 0.009737 / +3.422% |
| c04-854.graph500_s | 0.966422 | 0.839507 / 0.126915 / -13.132% | 0.837612 / 0.128810 / -13.329% | 0.837612 / 0.128810 / -13.329% |
| c04-872.marian_s | 2.364997 | 3.055426 / 0.690429 / +29.194% | 3.102997 / 0.738000 / +31.205% | 3.102997 / 0.738000 / +31.205% |
| c08-777.zstd_r | 1.052763 | 1.097865 / 0.045102 / +4.284% | 1.095015 / 0.042252 / +4.013% | 1.095015 / 0.042252 / +4.013% |
| c08-801.xz_s | 0.529423 | 0.553891 / 0.024468 / +4.622% | 0.552903 / 0.023479 / +4.435% | 0.552903 / 0.023479 / +4.435% |
| c08-817.flac_s | 0.269360 | 0.274364 / 0.005004 / +1.858% | 0.274627 / 0.005267 / +1.955% | 0.274627 / 0.005267 / +1.955% |
| c08-854.graph500_s | 0.969791 | 0.851812 / 0.117979 / -12.165% | 0.844982 / 0.124809 / -12.870% | 0.844982 / 0.124809 / -12.870% |
| c08-872.marian_s | 2.144827 | 2.853856 / 0.709030 / +33.058% | 2.830722 / 0.685895 / +31.979% | 2.830722 / 0.685895 / +31.979% |

| 等权范围 | M4 MAE / MAPE | P1 MAE / MAPE | P2 MAE / MAPE |
| --- | --- | --- | --- |
| all-ten | 0.176258 / 10.726% | 0.180104 / 10.895% | 0.180104 / 10.895% |
| C4 | 0.172200 / 10.254% | 0.183868 / 10.739% | 0.183868 / 10.739% |
| C8 | 0.180316 / 11.197% | 0.176341 / 11.050% | 0.176341 / 11.050% |
| without-marian | 0.045391 / 5.626% | 0.047144 / 5.720% | 0.047144 / 5.720% |
| Graph500 | 0.122447 / 12.649% | 0.126810 / 13.099% | 0.126810 / 13.099% |

P1/P2 均改善 5 例、恶化 5 例、无不变项。共同恶化项为 C4 Zstd（AE 0.005214 → 0.018692、RE −1.550%）、C4 Graph500（0.126915 → 0.128810、−13.329%）、C4 Marian（0.690429 → 0.738000、+31.205%）、C8 FLAC（0.005004 → 0.005267、+1.955%）、C8 Graph500（0.117979 → 0.124809、−12.870%）；AE 单位均为 cycles/macro。

P1/P2 完整 `time_accounting` 相同：10/10。全十例 P2 相对 M4 的 MAE 变化为 **+0.003846 cycles/macro**，MAPE 变化为 **+0.169 个百分点**。Graph500 的资源冲突显著下降，但两例 AE 均增大；本结果不支持将“移位后的资源冲突／缺少一次失败发射占用”认定为其主要 CPI 残差的已闭合解释。重试预约在本轮没有改变最终时间核算，不意味着任何输入下重试都无成本。

| 来源 | P2 接受 / 回退 | P2 拒绝=重试=唤醒 | P0 / P1 / P2 冲突 | P2 共享迟到事件 / 周期累计 / 最大 |
| --- | --- | --- | --- | --- |
| c04-777.zstd_r | 4,389,091 / 2,916,531 | 797,747 | 1,131,690 / 122,305 / 122,318 | 114,176 / 450,203,748 / 90,265 |
| c04-801.xz_s | 3,264,208 / 2,632,114 | 19,572 | 916,373 / 163,788 / 163,827 | 88,050 / 156,600,311 / 35,894 |
| c04-817.flac_s | 4,848,388 / 1,483,854 | 6,776 | 381,408 / 9,894 / 9,897 | 8,093 / 8,930,523 / 10,091 |
| c04-854.graph500_s | 13,325,265 / 4,618 | 4,889,244 | 6,372,525 / 60 / 60 | 190,747 / 31,930,975 / 3,021 |
| c04-872.marian_s | 3,151,080 / 3,906,524 | 14,348 | 1,350,002 / 166,052 / 166,065 | 488,017 / 6,119,854,000 / 114,220 |
| c08-777.zstd_r | 9,753,464 / 6,975,541 | 1,940,919 | 2,331,369 / 143,329 / 143,377 | 258,759 / 1,151,510,309 / 97,604 |
| c08-801.xz_s | 7,131,496 / 6,105,135 | 45,568 | 2,098,535 / 370,352 / 370,415 | 116,780 / 156,191,175 / 40,156 |
| c08-817.flac_s | 9,549,830 / 3,164,654 | 6,615 | 705,229 / 20,436 / 20,446 | 10,530 / 13,824,097 / 25,753 |
| c08-854.graph500_s | 26,642,268 / 8,041 | 9,397,178 | 12,105,472 / 100 / 100 | 388,548 / 65,006,492 / 3,031 |
| c08-872.marian_s | 5,870,895 / 7,709,896 | 19,371 | 2,580,353 / 322,881 / 322,890 | 837,788 / 10,353,647,637 / 118,094 |

普通单片段 load 的候选预约没有冲突；表内剩余冲突来自沿用旧安排的观察成员，不能当作整核端口已完整约束。其他负载回退读事件较多，不能将 Graph500 的小回退数量推广为全部架构覆盖。共享迟到周期累计可远大于总 active cycles，因为事件等待重叠；它们没有进入 CPI 归因或误差分母。

原始结果入口：[十源 CSV](../tmp/load-admission-retry-20261116/final/results.csv)、[结果与守恒汇总](../tmp/load-admission-retry-20261116/final/results.json)、[40 个回放收据](../tmp/load-admission-retry-20261116/final/receipts-off-audit-P1-P2.json)、[独立目标身份](../tmp/load-admission-retry-20261116/final/identity.json)、[quick 状态汇总](../tmp/load-admission-retry-20261116/final/score-status.json)。四组维护评分分别为 [OFF](../tmp/load-admission-retry-20261116/final/scoring/off/summary.json)、[审计](../tmp/load-admission-retry-20261116/final/scoring/audit/summary.json)、[P1](../tmp/load-admission-retry-20261116/final/scoring/P1/summary.json)、[P2](../tmp/load-admission-retry-20261116/final/scoring/P2/summary.json)。

## 端到端吞吐范围收缩

原计划覆盖十源、每源 3 次 OFF、3 次 P2 和 1 次 P1，共 70 个进程；用户在报告进度 22/70 时明确要求“先用 graph500 做验证，不要其他的”。该任务及原汇总等待进程已停止，停止时保留 [25 个已完成收据](../tmp/load-admission-retry-20261116/final/throughput/runs.json)，不宣称十源吞吐完成。

已完成 Graph500 C4/C8 各一组 OFF/P1/P2，共 6 个收据。C4 复用原第一组完整 3 个收据，C8 仅补同组 3 个回放；C4 原第二组未配完的数据不纳入主结果。P2 相对 OFF 的两例总量／总时间端到端速率变化为 −16.871%；reader 等价性、守恒和冻结默认／安装 ELF 身份均通过核验。各阶段墙钟及原始收据见 [Graph500 首轮验证](spr-functional-load-admission-retry-graph500-validation-20261116.md)。本轮是单组诊断，不构成 30 对正式验收。

## 候选保留决定

保留 P0/P1/P2 代码与显式候选入口，用于后续对照；SPR-M4 及安装 ELF 继续作为默认。关闭 admission/retry 的候选预测与冻结 M4 相同，审计开关也已通过十源完整时间核算中性检查。

本轮实现证明普通单片段 load 可以在原有两阶段框架中完成有界预约、拒绝及唤醒守恒，但没有取得整体 CPI 改善，Graph500 主要残差也未闭合。跨核共享提交顺序、特殊访问覆盖和多次失败尝试仍是明确近似边界；后续若扩展，应分别验证机制与同源精度，不能按原生额外发射量直接补入经验周期。
