# Marian C8 / 32GiB / 10M CPI误差来源诊断（2026-10-27）

结论：**目前最强的同源控制证据指向DRAM选择窗口退化与store生命周期近似；DTLB、同线合并和软件预取存在明确差异，但不能从累计等待量直接给它们分配CPI误差份额。** 到达时序受约束的DRAM候选与store候选组合，使CPI从3.034149降到2.828556，相对gem5仍高约7.86%；约一半原始周期差仍未闭合。本文只分析，不修改生产代码、时延常数或默认配置。

## 1. 原始15.7034%由什么组成

主口径是workload-task active cycles / 实际退休宏指令。分母两侧均为 **39,127,055**；gem5 active cycles=102,604,827.975976，FastSim=118,717,325，差 **16,112,497.024024 cycles**。gem5 CPI=2.622349880，FastSim CPI=3.034149261，相对误差+15.7034%，CPI绝对误差与本案MAE均为 **0.411799381 cycles/宏指令**。既有独立身份、130项直接参数及逐核人口核对保留，未发现总RAM、每段通道、宽度、ROB、SQ或缓存容量错配。

模型关键核是 **core1**：其本地结束=14,840,543 cycles；原生参考共同结束=12,826,966.996997 cycles，差2,013,576.003003 cycles。模型各核本地周期为 c0=14,384,872, c1=14,840,543, c2=14,666,475, c3=14,556,125, c4=14,410,264, c5=14,467,129, c6=14,429,481, c7=14,722,220。

八核本地跨度之和=116,477,109；按既有共同终点合同扩展到8×makespan，尾部补齐=2,247,235 cycles；再去掉other-task时间7,019，得到active 118,717,325。gem5相同合同中的other-task时间为10,908。差值严格满足：

```text
Δactive = 8 × Δelapsed_cycles + (gem5 inactive − model inactive)
        = 8 × 2013576.003003 + (10,908 − 7,019)
        = 16112497.024024 cycles
```

因此core1的慢点会传播到全案CPI，其他多数核的相对误差接近15.698%是共同窗口效应。它不表示八核各自独立产生同等硬件停顿；EOF补齐也是计量合同，不能删除来“修复”CPI。

## 2. 同源控制结果

全部使用原FST、thread-state plan、冻结FastSim SHA256 `911ac2738d3b58fd8e48c9a594ab45701d959ec41d26b7a37cdc4f62d4cdb973`，没有重新选择ROI或更换输入。8个成功控制均核对完全相同的内容身份，逐核macro/UOP/memory-UOP/branch人口相同，并经冻结维护入口`run_native_kernel_fastsim_validation.py --source-root ... --stats-dir ...`重评，预期1案、通过1案。rescoring自身不认证原执行二进制；本轮另保存实际执行命令、二进制/输出哈希和receipt补齐该链。

下表所有行对比相同gem5参考2.622349880；关闭DTLB等理想化干预是敏感性试验，不是相应真实硬件的精度认证。8个变体不是8个独立工作负载，不将其平均成主MAPE/MAE。

| 模式 | FastSim CPI | 相对gem5差值 | CPI绝对误差 / 单案MAE（cycles/宏指令） | 相对baseline的active cycles变化 |
|---|---:|---:|---:|---:|
| 原baseline | 3.034149261 | +15.7034% | 0.411799381 | 0 |
| 标量中性控制 | 3.034149261 | +15.7034% | 0.411799381 | +0 |
| 仅增加归因观察 | 3.034149261 | +15.7034% | 0.411799381 | +0 |
| 关闭DTLB（理想化控制） | 3.141320910 | +19.7903% | 0.518971030 | +4,193,311 |
| store certificate+lifecycle候选 | 2.923958115 | +11.5014% | 0.301608236 | -4,311,455 |
| 分支发现/恢复候选 | 2.995845688 | +14.2428% | 0.373495808 | -1,498,706 |
| DRAM窗口64，旧选择规则 | 2.783980343 | +6.1636% | 0.161630463 | -9,788,373 |
| DRAM窗口64＋到达时序约束 | 2.875779355 | +9.6642% | 0.253429475 | -6,196,548 |
| 到达约束DRAM＋store候选 | 2.828556200 | +7.8634% | 0.206206320 | -8,044,251 |

**中性观察已核对：**关闭materialized fast kernel及再开启CPI attribution后，八核周期、active/elapsed时间及task PMU均与原baseline一致，因此以下归因计数没有改变被分析结果。

## 3. 最强证据：声明FR-FCFS，实际选择窗口却为1

采集端MemCtrl使用FR-FCFS，read buffer=64。FastSim配置也声明`dram.scheduler=frfcfs`，但`frfcfs_repair_selection_window()`额外使用以下模型启发式：

```text
lanes = cores × ranks_per_channel / controllers = 8 × 2 / 16 = 1
effective_window = min(configured_window=8, lanes>1 ? lanes−1 : 1) = 1
```

这个值触发`apply_frfcfs_dram_repair()`的pressure bypass。正式输出中：

- effective selection window=1；candidate epochs=0、repaired requests=0。
- **684,132次请求全部走bypass路径**（其中data DRAM read 683,702，I-side read 430）；这些read包含由store/prepare触发的数据获取，与退休load-miss不是同一事件。
- 因而“配置参数匹配FR-FCFS”不能推出“已执行目标FR-FCFS仲裁”。真正的gem5从可用queue中按行命中及bank准备状态选请求，不按cores/ranks/channels把候选数硬限为1。

只放开缩窗并将window设为64，旧选择规则下CPI降到2.783980。但加入已有`frfcfs_causal_selection=true`、限制选择时间与请求到达关系后，CPI为 **2.875779**，相对差仍 **+9.6642%**，绝对差 **0.253429 cycles/宏指令**。更乐观的旧规则包含尚未满足到达条件的选择近似，不能把其全部收益认作真实可修复成本。

到达受约束控制减少active **6,196,548 cycles**，相当于原gap的 **38.46%**；实际重排41,160个请求。这个比例是本模型的干预敏感性，不是已经逐请求证明的gem5因果份额。仍有240个epoch回退，完整在线queue admission、page policy、refresh/RD-WB及corrected issue反馈没有由这次单案认证，不能直接推广默认。

## 4. 第二项证据：store准备、驻留与完成生命周期

原配置使用buffer TSO114，但certificate和prepare-lifecycle均关闭。原模型的prepare/head-demand专用计数为0；默认路径使用generic L1/L2 hit latency **2/6**，声明的store专用 **1/5**只在lifecycle路径生效。目标L1 store的mandatory queue→callback为1拍，L2路径为mandatory1＋request2＋response2=5拍。静态配置中写入1/5，并不意味着旧动态路径实际消费了它们。

同时开启现有certificate+lifecycle候选，CPI变成 **2.923958**，差 **+11.5014%**，绝对差/MAE **0.301608 cycles/宏指令**；active减少4,311,455 cycles，对应原gap的 **26.76%**。候选记录1,288,961个prepare、7,869,996个head-demand及1,333,268个merged demand。

这次开关同时改变资格、发起时刻、hit路径及SQ释放边，不能把收益全称为“每条store少一拍”。它只是对现有store机制敏感的证据。请求诊断也有代价：L1D tag-miss从670,055升至1,818,250，而原生为843,009；L2 tag-miss从553,513降至374,144。CPI接近不能单独推广该候选，退休缓存主资格仍未补齐。

将到达受约束DRAM和store候选组合，CPI **2.828556**，差 **+7.8634%**，绝对差/MAE **0.206206 cycles/宏指令**；active减少8,044,251 cycles，覆盖原gap **49.93%**。单独干预收益相加为10,508,003，实际组合少2,463,752，证明两者有重叠/交互。剩余约8.068M cycles、0.206206 cycles/宏指令尚未闭合。

## 5. DTLB：数量级异常成立，“29M walk等待造成15%”被控制否定

baseline功能顺序DTLB miss为18,033，timing miss为202,122，累计walk delay为29,469,613 cycles。实现分别维护功能LRU和按base earliest完成后才fill的timing LRU；同页在首个fill之前再次到达可重复排入单walker。它使用(address-space, 4KiB virtual-page token)作为键；真实x86 TLB还支持大页，base issue与corrected issue也可能不同。当前应用root的PTE资格仍unknown，没有逐次walk配对，尚不能选定是页粒度、fill可见性还是重试/请求人口的主要差异。

gem5最终统计中的八核rdMisses+wrMisses仅7,263，但它覆盖共同终点后额外1.000895104ms，只作非同窗旁证，不把它当本窗口精确参考，也不发布由此算出的DTLB主误差。

关闭DTLB后，CPI反而 **3.141321**，误差 **+19.7903%**，绝对差/MAE **0.518971 cycles/宏指令**；模型总active增加4,193,311 cycles。L2 tag miss 553,513→583,657，CHA/LLC-MSHR累计queue等待40,090,476→49,744,670，最慢核core1→core4。去掉translation pacing改变了请求序列和争用，因此不是在原关键路径上简单减29M。

这个反证既不证明DTLB正确，也不证明其影响为零；它否定了当前可加的正向归因。不能靠随意合并walk、减少延迟或扩大TLB来拟合CPI。

## 6. 预取、同线合并与前端：剩余误差的具体候选

当前窗口实际有 **683,524 条software-prefetch记录**，四个PC `0xab0948/0xab094f/0xab09af/0xab09b3`均经冻结ELF定位到`__memmove_sse2_unaligned_erms`，反汇编是`prefetcht0`；相邻为`movups`、`movntdq`和`sfence`。这是拷贝流的直接功能证据，不能仅因负载名Marian就把误差解释成矩阵乘SIMD延迟。NT指令在本项目gem5中按cacheable fallback实现，不能假设真机non-temporal绕过策略就是本次参考行为。

当前默认仍将预取CPU就绪保守地绑定到数据服务；目标Ruby存在CPU提前ack与后台fill/资源释放分开的生命周期。显式开启现有early-ack控制，在本FST上被拒绝：`software-prefetch admission requires canonical reclassification after parent callback`。这是实际请求父callback与重新分类未闭合的证据；没有有效CPI，不用部分试算或历史其他源数字顶替。

原生task范围有 **3,599,904 个Sequencer coalesced fragments**，模型当前line-coalescing关闭、计数为0。这是直接结构差异；两侧admission/forwarding/prepare域尚需逐请求配对，不能将该数量换算成“多做了360万次DRAM”。同理，model data DRAM reads=683,702包含writes/prepare，而主LLC tag诊断只含326,150；+5.48%的LLC诊断不意味着所有DRAM事务和争用都只错5.48%。native总packet来源和model ownership/prepare域需另外闭合，不能直接将总服务数相除当已核验误差。

分支恢复控制CPI=2.995846，相对差+14.2428%，绝对差/MAE=0.373496 cycles/宏指令；active减少1,498,706 cycles。全案branch-miss计数相同不代表各核或发现阶段/恢复时刻相同；其总影响也不是固定penalty计数74,164的简单加减。

中性的模型归因显示，memory-response修正后暴露的94,256,170 cycles分为memory response 50,739,489、SQ capacity 25,603,924、dependency 17,912,052及I-fetch 705；core1分别为7,377,714/2,460,997/2,298,048/243。它把模型慢点定位在load response与SQ反压，而不是前端取指。**这是模型内部修正账，不是gem5−FastSim的互斥误差分解。** 比如ROB累计stall达到86.885B、TSO store累计8.406B，远超过总跨度，显然不能直接拿来分摊16.112M gap。

## 7. 来源排除、证据边界与下一步

- gem5最终`stats.txt`的origin恰好等于共同起点，但finalTick比FST共同终点晚1,000,895,104 ticks；不能把其numCycles、pipeline stall或DTLB计数直接拼入本窗口精度表。主参考仍是维护工具审核过的共同终点owner时间账及Ruby/SLICC摘要。
- unknown owner cycles=0，逐核功能人口匹配，测量期syscall为0、合成syscall/page-fault/IRQ成本为0；没有证据把本案误差归于宏指令分母、丢失参与核或合成内核重复计费。warmup中100次syscall参数未知及应用初始PTE unknown仍保留，不据此宣称完整功能语义认证。
- 三个退休cache miss的native/model各2个unknown是PMU资格问题，不是主CPI差值的计算来源；完整资格主PMU误差仍不可用。硬件等价仍`unverified`。
- 优先补DRAM真实已到达queue、window与bank/row命令选择的请求级见证，核对corrected issue→controller admission→command→callback；随后配对store prepare/demand/NO_ACCESS和SQ释放。
- 再配对memmove中的prefetch CPU ack、copied request、alias父callback和随后demand，而不是仅提前退休预取；DTLB补有效页大小、真实lookup/fill与边界状态后再判断。
- 需要为这组源补齐原生逐请求/逐退休阶段证据才能将剩余约7.86%进一步定量拆分。本案native full JSONL未开启，现有FST与摘要不能倒推出完整的原生关键路径。源历史结果用于提出假设，本轮结论来自本轮控制；没有将旧C4窗口的数值搬入新C8结论。

## 8. 复现入口

实验：`tmp/marian-c8-cpi-diagnosis-20261027/`。`experiment-plan.json`、`followup-plan.json`、`interaction-plan.json`保存精确开关；各子目录有实际command/config/receipt/output SHA及维护rescore结果。`analysis.json`保存全部本轮算术及证据索引，`identity-and-population-verification.json`核对8种控制的内容/逐核人口，且确认src/include/configs与baseline冻结副本相比无改动。

相关实现位置：`src/simulator.cpp:20453`选择窗口、`:20511`退化bypass；`src/interval_core.cpp:1190`翻译、`:1246`timing miss/walker；`src/simulator.cpp:12017`预取父callback拒绝门禁。完整旧基线见[同源回放报告](marian-c8-32g-10m-fastsim-results-20261027.md)。

后续已补[固定NUMA、串行各3次的FR-FCFS吞吐对照](marian-c8-frfcfs-throughput-20261027.md)：测量吞吐P50下降6.25%，内部模拟墙钟增加6.91%，含输入校验的进程墙钟增加2.42%。这是同一Marian源的共享宿主观测，不作为全负载默认推广结论。
