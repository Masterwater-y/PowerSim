# 分支恢复的跨负载影响与 SQLite 剩余误差（2026-10-20）

后续已实施：[恢复尾部传播修复与同源验证](branch-recovery-tail-fix-results-20261020.md)。本页保留修复前的开关对照和残差定位。

本轮固定当前 NO_ACCESS 修复后的生产模型，复用现有 trace，不新采集、不改变生产配置。对 Zstd 完成同源开关回放；对 SQLite 另完成开启后的完整阶段导出，并与同次原生阶段流配对。使用 systematic-debugging 的冻结来源、单变量控制及互斥周期账方法。

## 1. 同源开关结果

CPI 为 workload-task active cycles / 宏指令；绝对误差单位为 cycles/宏指令，每行的单案 MAE 等于该行绝对误差。相对误差带符号。不同模式不当作独立负载合算 MAPE。

| 输入 | response_branch_recovery | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差 / 单案MAE |
|---|---|---:|---:|---:|---:|
| SQLite 原重采源 | 关闭 | 1.000925 | 0.773267 | -22.7448% | 0.227658 |
| SQLite 原重采源 | 开启 | 1.000925 | 0.870686 | -13.0119% | 0.130239 |
| Zstd 最新 NO_ACCESS 源 | 关闭 | 1.472610 | 1.342789 | -8.8157% | 0.129822 |
| Zstd 最新 NO_ACCESS 源 | 开启 | 1.472610 | 1.394890 | -5.2777% | 0.077720 |

Zstd 开启后净增加 1,097,668 active cycles，预测 CPI 相对关闭改变 +3.8801%；误差绝对值从 8.8157% 变为 5.2777%。关闭对照精确复现已发布 NO_ACCESS release 的 CPI；有效配置仅 response_branch_recovery 一项变化。

| 输入 / 开关 | gem5 elapsed ms | FastSim elapsed ms |
|---|---:|---:|
| SQLite 原重采源 / 关闭 | 1.966026 | 1.518904 |
| SQLite 原重采源 / 开启 | 1.966026 | 1.710226 |
| Zstd 最新 NO_ACCESS 源 / 关闭 | 2.584096 | 2.356146 |
| Zstd 最新 NO_ACCESS 源 / 开启 | 2.584096 | 2.447542 |

开关同时应用于功能 warmup 和测量，净变化包含预热状态及后续交错传播，不是独立分支停顿周期和。本轮不报告新的宿主吞吐或开关开销。

## 2. SQLite 开启后的剩余时间差

下面使用原生阶段取证时的同次 FST，而非上一节 SQLite 原重采源；自然执行共同窗口略有不同，不能逐指令跨源相减。开关对照仍使用这一阶段源自身的参考与分母。

- off：gem5 1.001020140，FastSim 0.772705758，相对误差 -22.808171%，绝对误差/单案MAE 0.228314381 cycles/宏指令。
- on：gem5 1.001020140，FastSim 0.870608832，相对误差 -13.027841%，绝对误差/单案MAE 0.130411308 cycles/宏指令。

| 时间落点 | gem5 cycles | 开启后FastSim cycles | gem5 − FastSim |
|---|---:|---:|---:|
| 有退休的周期 | 9,702,339 | 7,626,425 | +2,075,914 |
| 下一退休指令尚未取指 | 138,530 | 11,216 | +127,314 |
| 已取指、尚未最终发射 | 319,652 | 290,402 | +29,250 |
| load 已发射、尚未退休 | 13,388,683 | 12,579,505 | +809,178 |
| store 已发射、尚未退休 | 4,594 | 1,488 | +3,106 |
| 其他已发射、尚未退休 | 73,120 | 1,606 | +71,514 |
| 未知阶段 | 4 | 0 | +4 |

配对退休跨度差 3,116,280 cycles；起止、EOF 与 owner 修正 -38627.120120 cycles，相加守恒到 active 差 3,077,652.879880 cycles。四核完整配对人口与关闭对照相同，阶段导出对生产目标状态中性。

这些是互斥的时间落点，不是互斥的根因贡献。“有退休的周期”差表示退休密度不同，不证明 commit width 错；不能把 load 阶段差直接改名为 DRAM 贡献。

开启后的八段退休跨度差为 377628, 392776, 386093, 402260, 415500, 399672, 377478, 364873 cycles。首段占 12.12%，仍未集中在窗口开头；这不替代 warmup 长度扫描。

## 3. 残差落点与因果证据

| 函数 | 开启后的退休跨度差 cycles |
|---|---:|
| sqlite3VdbeExec | +1,934,909 |
| sqlite3BtreeNext | +458,639 |
| sqlite3VdbeSerialGet | +379,494 |
| getPageNormal | +150,895 |
| moveToLeftmost | +70,763 |
| getCellInfo.part.0 | +46,232 |
| btreeInitPage | +37,664 |

`btreeParseCellPtr` 的配对跨度差从关闭时1,003,661降至开启时8,147 cycles；`sqlite3VdbeExec` 从3,028,856降至1,934,909，`sqlite3BtreeNext` 从469,266变为458,639。函数排名已经变化，不能继续用关闭配置的热点代表全部残差。

以上按最终退休 PC 定位，不代表等待只由该函数造成。剩余误差尚未完成独立因果分摊；以下明确区分已见证缺口和候选方向：

1. **已找到恢复间隔在 response 修正中丢失的局部反例。** 第一阶段 `IntervalCore` 对误预测设置 `frontend_ready >= completion + branch.mispredict_penalty`，本案配置为2；response 候选却只发布 `corrected completion` 作为 fetch 下界。同一短窗口的 branch ordinal1775284 基础完成1097764、后继基础fetch1097767，间隔3；修正后完成与后继fetch均为1098027，间隔降为0。在8192条连续审计记录的29对模型误预测中，0对提前于解析，但28对低于已配置的2-cycle恢复尾部。这证明两个阶段未完整保持模型自己的恢复规则，不是新调延迟常数；具体占全ROI多少尚未测出，不能按28对或每分支2拍直接外推。该短源不是完整阶段源；native逐分支预测结果位仍未配对，因此不宣称28个原生误预测身份已经逐项认证。见 [局部见证](../tmp/branch-recovery-crossworkload-20261020/branch-tail-witness.json)。
   **同一开关也没有完成 cache 准入重排。** 它不重放当前批次已选定的 cache/coherence 路径，不独立完成 redirect 到 Fetch 的全部传播与错误路径资源建模。这些是已确认的实现边界，但本案残差份额尚未量化。应保留既有恢复边的语义、来源和重叠关系，不能在总CPI上统一补周期。
2. **访存服务分布及请求准入有待核对。** 先前关闭开关的同次短见证中，1214 个双方 L1 load 的 issue→response 中位数均为2；13 个双方 DRAM load 则是原生186、模型164 cycles。另有相反方向的层级错分。样本小且极端样本跨起点，不能据此推成当前全 ROI 残差或统一加内存延迟。应在开启配置下匹配实际准入、返回、消费者就绪和退休。
3. **完成到退休边界确有局部差异，但不是已证明的主要来源。** 普通整数样本 native complete→retire 最小2、模型0；此前仅开启来源映射的全局两拍下界，净补97208 cycles。该对照是在 branch recovery 关闭时做的，不能与本轮净收益相加，也不能直接算它占剩余误差多少。
4. **DTLB 是模型边界，不是本案已定位主因。** 当前 DTLB 已开启，64项，使用 timing_walk；cache-visible hierarchy_walk 关闭，固定兼容 walker 仍是近似。没有本次完整窗口的配对证据将残差归给它。
5. **普通 store、NO_ACCESS 与开头冷启动不支持作为主要解释。** SQLite 本次没有 NO_ACCESS；已有独立 store lifecycle 对照净影响很小；关闭配置的八段差额均匀。上述消融与八段证据均不冒充开启配置下的独立因果份额，也不宣称已排除一切 warmup 间接效应。

## 4. 验证与限制

新三次维护验证、独立目标身份、有效配置单变量、关闭结果复现、完整人口和导出中性等共 18 项检查通过。native cache 诊断真值继续来自 conserved Ruby/SLICC summaries，未使用 path_class 替代。validator passed 不等于硬件 perf 等价或准确率阈值通过。

未修改生产源码、默认值或延迟常数；没有重采 trace。全部结果与命令在 [实验目录](../tmp/branch-recovery-crossworkload-20261020/)；[机器证据](../tmp/branch-recovery-crossworkload-20261020/evidence.json)、[完整配对](../tmp/branch-recovery-crossworkload-20261020/paired-comparison.json)、[Zstd 开启验证](../tmp/branch-recovery-crossworkload-20261020/validation-zstd-on/cases/c04-777.zstd_r/validation.json)、[SQLite 开启阶段验证](../tmp/branch-recovery-crossworkload-20261020/validation-sqlite-stage-on/cases/c04-708.sqlite_r/validation.json)。

后续优先沿开启配置中的首个持续分歧，配对 branch resolution → redirect/fetch → 实际 cache admission → response → retire。完整请求准入重排的收益仍需机制证据与正负误差控制，不以开关让单案 CPI 更近作为默认推广依据。
