# TeaLeaf 尾部时序探索：2026-09-07 首轮证据

## 决定与研究边界

下一步聚焦 **DRAM 请求到达顺序、排队与 rank refresh 的响应时序**。本轮已完成输入核对、完整 ROI 补采、累计误差定位和关键 load 的成对见证；尚未实现新的生产模型，也没有新的 P99/吞吐收益结论。

Q 固定 1024，pending-fill 及其 load-admission/store-commit 子开关保持关闭。遵循 [优化决策与停止条件](optimization-decisions.md)。新证据支持继续研究共享 DRAM 日历；不支持统一增加 load 延迟，也不支持重新扫描 pending-fill 补偿组合。

## 1. 可比性与采集完整性

原始输入来自 `tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json`；本轮产物在 `tmp/tealeaf-tail-timing-20260907/`。

6 个冻结 case 的显式 warmup/measurement slice、线程到核心映射、CPL 分母、缓存容量、ROB 容量、配置指纹及 Q 核对通过。gem5 标签按每核 `clock_period_ticks=333` 换算目标周期；FastSim 标称 3 GHz，gem5 实际约 3.003 GHz，不能把 tick 当 cycle 或通过调频拟合误差。guest OS TID 未提供，关联采用核心、捕获 context/thread、ASID、记录 ordinal、PC/micro-PC 和动态序号。

| 冻结 case | 当前 signed CPI error |
|---|---:|
| DSE L1D64 C4 | −17.9910% |
| DSE ROB256 C8 | −17.3517% |
| DSE baseline C4 | −11.4993% |
| DSE baseline C8 | −11.2570% |
| DSE LLC32 C4 | +9.6487% |
| formal TeaLeaf C16 | −13.3682% |

DSE 使用 user-UOP CPI；formal 使用 macro CPI，未混合聚合。源 CPI 可由 `cpl_class.jsonl` 的 `(measured_ticks - idle_ticks) / clock_period_ticks` 和对应分母重建。6 个原目录均没有逐 UOP stage labels 或逐请求响应 JSONL；native-summary 不能代替时序标签。

相同核心数的跨配置比较，所有对应核心的前 4096 条测量记录指纹均不同，warmup 长度也不同。它们仍是各自 trace/config/reference 的合法准确率 case，但不能直接称为“只改变一个硬件参数”的同输入因果实验。

本轮使用原 gem5 二进制、原 ROI checkpoint、原硬件配置补采 L1D64 C4 和 LLC32 C4：

- 原/本轮 gem5 SHA256：`3355538485525218f10c545d19950ad6961de2dd28b802ba767f28092a7fc9ac`。
- 100k 用户 UOP/核小样本分别耗时 54.74 / 43.32 秒；转换后的全部 warmup + measurement FST 记录逐字节匹配原始前缀。
- 完整 10M 用户 UOP/核 JSONL 补采分别耗时 467.91 / 504.04 秒；各核 CPL 记录与原实验完全相同。
- L1D64 C4 另做有界 LSQ/ProtocolTrace/DRAM debug 重放，耗时 313.58 秒；4 个完整 FST 文件 SHA256 和全部 CPL 记录均与原实验相同。debug 只覆盖 tick `[21705779044444, 21705788394091)`。
- FastSim 使用当前二进制 `c5875a45a03c86edf8394385e00e32d669b0fe0ab8e9a71d1b28c7058f6ecaf6`。诊断需要关闭 materialized fast kernel、启用 attribution；与同二进制默认路径逐项核对了 `scope_metrics`（排除宿主吞吐）和 `threads`，包含周期、CPI、PMU，均相同。生产配置未改变。

原版 native-response-v6 JSONL **没有 admission/response 时间戳**，版本名不能作为字段能力证明。曾尝试本地另一诊断二进制，但它在该 checkpoint 的 warmup 中因 Ruby functional read 失败退出，未产生有效测量；已排除。最终请求见证来自上面通过完整 FST/CPL 一致性检查的原二进制 debug 重放。

## 2. 首段不代表完整尾部

| case | 100k/核 gem5 sum cycles | FastSim sum cycles | 短段 signed error | 完整 ROI signed error |
|---|---:|---:|---:|---:|
| L1D64 C4 | 255,877 | 273,723 | +6.9744% | −17.9910% |
| LLC32 C4 | 532,406 | 685,530 | +28.7608% | +9.6487% |

因此，不能根据第一段 100k 的局部改善预测完整 P99。本轮随后采集了完整 ROI，而非继续在该短段调参。

完整 ROI 每约 10k 条记录采一个 FastSim 里程碑，与新 stage labels 按指令身份关联。L1D64 C4 关联 4,021 点，LLC32 C4 关联 4,033 点。FastSim 用 warmup barrier 作为固定 ROI 原点；gem5 用原每核 ROI first tick。没有按窗口重新平移。

由于缺少逐区间 idle 时间戳，gem5 在任一里程碑的累计 active cycles 只可限定在 `[max(0, elapsed - whole_ROI_idle), elapsed]`。下表列出 L1D64 C4 首个**即使扣除全 ROI idle 预算，仍至少低估 10k 周期**的采样点；不是精确首个分歧。

| 核心 | 测量记录 offset | 采样到的最大确定低估至少 |
|---|---:|---:|
| 0 | 1,499,781 | 1,517,140 cycles |
| 1 | 1,169,804 | 1,087,662 cycles |
| 2 | 3,620,635 | 927,804 cycles |
| 3 | 2,322,306 | 885,933 cycles |

core 1 的全 ROI idle 仅 3,922 周期，无法解释约百万周期的差距。在约 1M 测量记录处仍偏高，随后在 1.2M–4M 区段持续扩大低估。LLC32 C4 的全 ROI 则保持正误差，必须继续保留为反向控制。

## 3. 密集窗口与退休关键 load

选择 L1D64 C4 core 1 的原始记录 ordinal `2,509,999..2,519,999`，对应测量 offset `2,219,804..2,229,804`。10,001 条记录均匹配 PC、内存地址、CPL、ASID、core/thread 和 stage identity。以下窗口周期是端点退休时间之差，不是新的独立 ROI CPI：

| 指标 | 结果 |
|---|---:|
| gem5 窗口周期 | 10,059 |
| FastSim 窗口周期 | 4,944 |
| 差距增量 | 5,115 |
| gem5 有退休的周期 | 1,497 |
| ROB 头已 fetch、尚未 issue 的零退休周期 | 1 |
| ROB 头已 issue、尚未 commit 的零退休周期 | 8,561 |
| 其中 ROB 头为 load | 8,559（窗口的 85.09%），共 63 条 load |

这把该窗口的问题定位到已发射 load 的响应/退休阶段。它不证明整个 workload 没有 frontend 或依赖问题。也不能把 gem5 的 load `completeTick` 当成数据返回：这里使用 LSQ 发包和 Ruby Seq Done 的实际 debug 事件。

对阻塞最大的 20 条 load，用 `(core, inst_seq_num)` 找到 LSQ 成功发包，再用地址、核心、响应 tick 及回推的 admission tick 唯一关联 Seq Done；每条还关联到一个 DRAM arrival 和调度事件。这 20 条覆盖 7,071 个 load-head 零退休周期，占该窗口全部 load-head 阻塞的 82.61%。它们的 FastSim 数据事件地址也逐一匹配。

| 这 20 条关键 load | gem5 | FastSim |
|---|---:|---:|
| issue → 成功准入 | 全部 1 cycle | 此表不以 FastSim issue 代替硬件准入 |
| 19 条非 refresh 恢复见证：准入 → 响应 | 243–358 cycles，中位数 315 | 全部 161 cycles |
| 最长一条：准入 → 响应 | 1,504 cycles | 161 cycles |
| 响应 → commit | 全部 3 cycles | 上述请求 issue → retire 也为 161 cycles |

**在这组关键请求上，低估不能归因于 gem5 准入等待很长。偏差集中在请求已被接收后的共享内存响应时序。** 这是局部已验证结论，不是对所有请求或全局 MLP 的结论。

最长请求的完整见证：

- PC `0x409fce`，dynamic sequence `2529038`，物理地址 `0x1325d340`；该 load 独占 ROB 头零退休周期 1,497 个。
- 发包/准入 tick `21705785358462`；DRAM arrival `21705785363790`。
- channel 5 / rank 0 / bank 5 / row 153；该 rank refresh episode 从 `21705785363254` 到 `21705785799957`。
- 此请求在 `21705785799957` 恢复调度，与 rank refresh 恢复时刻相同；arrival → 调度等待 **1,309.81 cycles**。调度调用时刻不等于未来实际 RD 命令时刻。
- Ruby 响应 tick `21705785859294`；准入 → 响应 1,504 cycles。

当前 `DramConfig` / `DramModel` 没有 rank refresh/tREFI/tRFC 状态，这是已对应到真实退休关键 load 的缺失机制。此见证不能说明剩余 19 条非 refresh 请求的延迟也由 refresh 引起。

另一个需要闭合的证据是：这 20 条 FastSim 请求的 canonical DRAM arrival → command 排队均为 0，canonical arrival 比校正后的 core issue 早 85–962 cycles；随后校正路径使用 161-cycle 响应服务时间。例如最长请求的 canonical arrival/command 均为 `2033567`，校正 issue 为 `2034080`。这些是模型内部两个时基的观测，不能直接把其差值作为额外等待加上去；需要检验请求到达顺序改变后的 bank/row/queue 状态。

## 4. 后续实现顺序与验收

1. **先做有界 DRAM 日历差分。** 使用本轮定位的地址序列、bank/rank 映射、请求到达相对顺序，分别核对非 refresh 排队/row 状态，以及 refresh 时的 rank 不可用区间。将二者分开验证；不能只补一个 tRFC 常数便解释全部 5,115 cycles。
2. **明确生命周期与时基。** 控制器应处理何时到达的请求集合、哪些 bank/row 状态可见、何时恢复服务。refresh 的相位/初始化必须来自可部署的模型状态；gem5 tick 只供离线核对，不得成为推理输入。
3. **再选候选实现。** 优先替换失真的 DRAM 求解工作，记录是否增加 pass、遍历和状态复制。保持 pending-fill 关闭，不直接增加第二遍全模型反馈；先验证微型差分及本窗口关键消费者。
4. **同时验收 L1D64 C4 和 LLC32 C4**，再扩到 ROB256 C8、baseline C4/C8、formal C16。通过后才运行 formal40 + DSE54 的 P99/max/MAPE、方向性和 heldout，并单独测固定 NUMA 串行交错吞吐。

当前没有可负责地给出的 P99 收益估计；不能把 20 条响应差或窗口 5,115 cycles 相加外推。全请求 MLP、控制器所有在途请求、wrong-path/page-walk 占用尚未闭合。本轮停止在已取得真实关键路径证据和明确下一项差分，不改变生产默认模型。

## 5. 工具、证据与验证

新增工具：

- `tools/audit_tail_timing_inputs.py`：6-case 输入/边界/时钟/标签可用性审计。
- `tools/collect_tail_timing.py`：从冻结 run.log 重建 argv，校验原二进制/checkpoint，隔离输出，支持限定目标和有界 debug；所有标签为诊断产物。
- `tools/audit_tail_timing_pairs.py`：生产/审计目标指标相等检查、按身份配对、固定 ROI 原点及 idle 上下界。
- `tools/audit_tail_dram_witness.py`：原二进制 debug 重放的完整 FST/CPL 等价检查，以及 LSQ → Seq Done → DRAM/refresh 的唯一关联。

`tools/audit_gem5_commit_gaps.py` 新增 `--clock-period-ticks` 并将所用周期写入结果；其本轮之前的改动予以保留。

主要产物：`input-audit.json`、`prefix-binary-comparison.json`、两套 `*-c04-full/paired.json`、`l1d64k8-c04-full/dense-paired.json`、`dense-gem5-gaps.json`、`dram-witness.json`。每次补采的 `collection.json` 保存准确 argv/指纹，`completion.json` 保存退出状态和耗时；失败的诊断版本也保留，避免误用。

可复核入口：

```bash
python3 tools/audit_tail_timing_inputs.py \
  --inventory tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json \
  --output tmp/tealeaf-tail-timing-20260907/input-audit.json
python3 tools/audit_tail_dram_witness.py \
  --debug-collection tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-debug \
  --stage-collection tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full \
  --output tmp/tealeaf-tail-timing-20260907/dram-witness.json
```

验证：CMake build、`./build/fastsim_tests` 通过；Python 工具已在实际采集/配对/请求见证上运行。诊断开销不作为生产吞吐收益或回退统计。本报告保留主要数值与失败边界；若清理 `tmp/`，逐事件复核仍需按保存的原输入及采集参数重新生成标签。
