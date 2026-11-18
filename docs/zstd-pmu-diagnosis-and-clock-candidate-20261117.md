# Zstd PMU：诊断实施、时钟候选与原生填充见证

日期：2026-11-17。

本轮已实现中性逐成员诊断，验证一个默认关闭的时钟候选，并完成新的同源 native 短窗采集。**尚未得到可推广的 zstd PMU 精度修复**：候选改善 zstd L1，但恶化 XZ、FLAC；L2/L3 不变。没有推进默认版本或安装候选。`build/fastsim` 仍为 M5，SHA-256 `b869c0041b7ca6cf625e8aebdd6392e7c4a52be95ad1466052dc3d39e7d5f1b8`，M1–M5 配置不变。

## 实现

- `RetiredCacheMissEstimator::Audit` 记录决策、原 parent 区间、创建该 shadow 的源 ordinal 和 shadow generation。后两者是模型观察器身份，不冒充 native request-ID 或物理缓存代次。额外 owner 表仅在诊断启用时分配。
- `FASTSIM_RETIRED_FILL_AUDIT` 输出有界 JSONL 样本和独立的全人口 fragment 计数；样本截断不删除完整计数。summary 明确区分 measurement、task、kernel 和 physical core，并保留 unknown-private、caller/shadow fallback 及 final clock 不可用数量。
- CLI 要求完整 functional input identity；预测 JSON 保存样本及 summary 的大小和 SHA-256。`tools/analyze_retired_fill.py` 检查身份、哈希、闭合、重复 fragment 和完整计数守恒。只有模型确实分类为 L1 的成员才能标作 hit-to-pending；unknown 的零 mask 不当作 hit。
- 实验选项 `core.retired_cache_final_admission_clock=false` 默认关闭。开启后仅改变 PMU 观察器的 child 时间选择：使用最终 CPU admission，parent 仍保留原选定 service begin/end，避免把 child 位移重新传播成整段 fill。它不修改 CPU、cache、共享请求或两阶段调度。不支持的 event-only approximation 组合显式拒绝。
- 独立修复 task PMU 输出资格：共同终点窗口中，一个物理核可以有已知的零 workload-task 人口。现在仍要求完整 ownership、逐核 `task + other = full` 守恒，以及全局非空 task 人口；不再要求每个物理核都有正 task 计数。该修复不改计数、不改时序、不改变 workload task 集合。

源码在当前 `fastsim-spr` 工作树；隔离构建、原始备份及所有收据在 `tmp/zstd-pmu-repair-20261117/`。未提交或推送。当前源码包含实验选项，安装的冻结 M5 ELF 不包含本轮新诊断；复现应使用该目录的隔离 ELF。

完整 C4 对照使用 `frozen/fastsim`，其源码哈希记录在 `frozen/identity.json`；最终评审修正和零人口资格修复后的 ELF 为 `frozen/fastsim-reviewed`，SHA-256 `d7ea6204c29acc87bee0127f25271da71d0b5138cf537f14c103ef6b7952dcca`，用于最终 native 短窗回放。二者的身份分开保留，不把后者冒充完整 C4 测量 ELF。最终源码与已测隔离源码逐文件一致的收据为 `completion.json`。

## 完整 C4 对照

使用原四个完整 C4 输入、原 warmup 和 task plan。native 参考沿用原独立审计，仍是暂定 service-to-event 误差，不是本轮全量 fresh strict 硬件 PMU 认证。RE=`(FastSim-reference)/reference`。

| 负载 | L1 RE：M5 → 候选 | L2 RE：M5 → 候选 | L3 RE：M5 → 候选 | native unknown/事件 |
|---|---:|---:|---:|---:|
| zstd | +67.258% → +22.843% | +109.086% → +109.086% | +122.623% → +122.623% | 0 |
| XZ | −54.925% → −62.125% | +3.428% → +3.428% | +3.228% → +3.228% | 1 |
| FLAC | +7.888% → −27.449% | +10.305% → +10.305% | +24.848% → +24.848% | 0 |
| Graph500 | +9.888% → −7.130% | +0.008% → +0.008% | +0.003% → +0.003% | 0 |

XZ 误差用用户已允许的 known-positive 点参考，实际 unknown 保留，维护比较器的 unavailable 状态未改写。全部输入身份一致，并用维护的 `compare_retired_cache` 保留逐核/逐 scope 人口检查。

四例的完整 `time_accounting` 和原始退休/请求 PMU 均与 M5 相同，CPI 不变。AE 单位 cycles/macro。

| 负载 | gem5 CPI | M5/候选 CPI | CPI RE | CPI AE |
|---|---:|---:|---:|---:|
| zstd | 1.206049 | 1.185146 | −1.733% | 0.020903 |
| XZ | 0.575297 | 0.602991 | +4.814% | 0.027694 |
| FLAC | 0.284584 | 0.294976 | +3.651% | 0.010392 |
| Graph500 | 0.966422 | 0.839796 | −13.103% | 0.126626 |

C4 CPI MAE=0.046404。zstd/Graph500 另有关闭候选、打开诊断的完整回放，全部 PMU 命名空间和时间账户精确中性。完整数值与 ELF 区分见 `tmp/zstd-pmu-repair-20261117/results.json`。

**决策：不推广该时钟候选。** 不按 zstd 单例择优，也不将 Graph500 改善抵消 XZ/FLAC 回归。已在 C4 出现明确回归，因此没有继续扩展这个失败候选到 C8/C16 或吞吐验收。

## 新的 native 同源短窗

另采集 zstd C4，保留 500k 功能 warmup，以 first-core 1M measured user UOP 的共同终点停止。所有四个物理参与核保留完整记录；实际窗口内 workload-task 集合为三个任务，另一核执行 other-task，不补造任务人口。

硬件预取八个实例、early prepare 四个实例均关闭；使用冻结的已具备 native SPR member 字段的 collector，启用原有 native fill/squash 观察。共享容量 reservation 与磁盘 guard 正常结束。新源与完整 C4 不是同一个窗口，不能合并或外推误差贡献。

独立 target identity valid。资格修复前，strict validator 正确保留失败：task PMU 输出 unavailable，虽然内容和退休缓存人口检查已通过。修复后在**未改变任何计数或时间账户**的同一来源上重新回放；维护 validator 的 OFF/候选各 1/1 passed、failed=0、pending=0。该 passed 是来源/合同/守恒门禁，不是误差阈值、硬件事件等价或吞吐认证。

| 短窗模式 | L1 RE | L2 RE | L3 RE | gem5 CPI | FastSim CPI | CPI RE | CPI AE |
|---|---:|---:|---:|---:|---:|---:|---:|
| OFF | +69.714% | +52.822% | +52.879% | 0.867310 | 0.874026 | +0.774% | 0.006715 |
| 候选 | +33.752% | +52.822% | +52.879% | 0.867310 | 0.874026 | +0.774% | 0.006715 |

参考 unknown 为零，但仍有显式 service-to-event 估计；模型 unknown 和 fallback 保留。

## 逐成员证据及边界

先验证原始样本的完整闭合与哈希，再调用维护工具的内容/捕获绑定函数。完整 model member 绑定发现 284 条抽样跨页 split 行不能按 FST 首地址连续区间直接核验，其中 210 条在测量段；native 样本没有该项不一致。不能据此猜测第二页物理地址，也没有放宽完整工具的默认拒绝。

以下诊断明确排除全部 982 条抽样 model split member-line（含 warmup/非 task）；仅绑定非 split 样本视图。完整 FST、functional companions、native capture bundle 和全部预测人口仍保留并验证；**没有裁剪主 PMU 计数或模拟输入**。采样为 line modulo 64，不是随机样本。

在相同的 7,441 条 measured workload-task 单片段 member-line 中：

| 事件/模式 | TP | FP | FN | TN | native unknown |
|---|---:|---:|---:|---:|---:|
| L1 OFF | 1,990 | 1,239 | 56 | 4,156 | 0 |
| L1 候选 | 1,855 | 689 | 191 | 4,706 | 0 |
| L2 OFF/候选 | 183 | 63 | 1 | 7,194 | 0 |
| L3 OFF/候选 | 183 | 63 | 1 | 7,194 | 0 |

候选减少 550 条 L1 FP，同时增加 135 条 FN，说明不能把删掉的 pending-fill 全算作正确修复。这里的单位是抽样 member-line，不是完整退休宏指令误差。

对 63 条 L3 FP 继续使用 native 自身时钟及真实 fill/squash 记录关联：

- 55 条：native L1 pending-fill，等待期间有普通 demand fill；该 fill 身份不在保留成员中，且落在同核实际 squash sequence 范围内。
- 7 条：此前的普通 demand fill 有相同的未保留/squash 身份证据。
- 1 条：demand 来源未解析。

这是**62/63 条该子集 FP 与被 squash 的 demand 填充相关**，不是原完整窗口 98.4% 净误差的因果贡献。没有声称受控关闭这些请求后会等量改善，也没有把 native speculative 请求、填充时间或 cache outcome 输入模型。

这为下层残差提供了比“可能是缓存参数”更具体的证据：native 的一部分退休 load 由退休流之外的服务填充/合并，而当前模型把它们当成自己的下层 miss。当前项目合同禁止新增 wrong-path/shadow 流量，因此不能用重放这些请求来修准。

## 验证与继续工作的边界

五个 C++ 目标通过：`fastsim_tests`、`fastsim_perf_retirement_tests`、新 diagnostics、clock、task-PMU-coverage 测试。Python retired-cache 22、diagnostics 5、task-PMU 15、native-kernel-PMU 13 项通过。独立评审的两项 Important 问题已分别 RED→GREEN 修复：unknown 误标 hit、event-only 组合默默回退旧时钟。初始缺 snapshot 头文件映射、GCC runtime 的测试启动错误均保留日志，修正环境后重跑通过。

所有回放开有诊断日志，不能用于宿主吞吐成绩；未运行失败候选的吞吐验收。额外完整 source/hash 检查也没有计为模拟吞吐。原 M5 默认及安装 ELF 最后重新核验未变。

下一步需要分别处理：L1 的 parent 身份与双向分类证据；跨页片段的功能输入身份；退休流不可见服务造成的近似边界。不能把本轮中性诊断、资格修复、validator passed 或单例 L1 改善写成“zstd PMU 已修好”。

主要证据位于 `tmp/zstd-pmu-repair-20261117/`：`results.json`、`landed-source.json`、`review-fixes.json`、各次 RED/GREEN 日志、`campaign/`、`native/validation-covered-{off,candidate}-final/`、`native/covered-*/spr-member-pairing.json` 和 `spr-l3-fp-fill-provenance.json`。原失败记录和首轮身份字段不完整的审计样本均保留，后者不用于最终配对。
