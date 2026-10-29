# CPI 大误差与 scheduler：同源消融诊断

日期：2026-10-16。问题：最新 LBM/Graph500 CPI 为什么远超旧报告的约10%，
是否由新增 scheduler 引起。结论：**主要偏差在关闭全部同步/任务计划后仍存在，
旧版 FastSim 在同一新输入和配置下也产生完全相同的基础周期。没有证据把主要
误差归因于近期 scheduler/退休 PMU 代码。** 已局部验证 buffer TSO 准备门禁影响，
但尚未定位全部残差的首个时序分歧，不能宣称根因已经全部查清。

## 1. 实验控制与来源

固定[八通道两案](retired-cache-eight-channel-results-20261015.md)的原始FST、
warmup/stat共同窗口、四核、8通道、3GiB、buffer TSO114、工作负载二进制。
本轮未新采集、复制或删除FST，也未修改生产模型和默认配置。

全部变体通过维护入口 `tools/run_native_kernel_fastsim_validation.py` 回放。
已有独立identity/source/common-end证据继续保留；新报告检查各自的人口和计数。
结果投影见[证据JSON](../tmp/cpi-regression-diagnosis-20261016/evidence.json)，
内含每条维护报告路径、原始错误、周期、配置及二进制SHA。
两案可并发诊断，本轮不报告宿主吞吐或优化开销。

当前FastSim SHA为`0595a690a9a39248549c5643b330d395ea86a0f70acf2f7e7ca2add01ae4373c`，
与原八通道实验完全相同。同步consumer实施前保留的
`tmp/functional-sync-consumer/fastsim-before` SHA为
`ae6a61b1f70a1409a20beeaf96aa138dbfab338a2279e66be3b946340f540986`。
旧二进制只用于基础周期同值检查；它没有当前active/elapsed输出合同，
不将其legacy CPI输出冒充新的active精度。

## 2. scheduler与同步并非一个额外的固定延迟

`RecordedScheduler` 根据自身校正退休时刻记录task owner、ready/blocked及active区间；
不会把gem5时钟作为模型输入，也没有固定切换收费。内核指令仍在物理源中执行。
与它组合的功能事件图会截断producer/epoch、约束依赖和后续请求的准入时刻，
因此关闭计划也可能通过warmup状态、分块和共享交错改变预测，不只删除等待。

四核指令流与task顺序固定；这仍是recorded replay，不是重新决定OS调度。

### 2.1 测量等待不能解释LBM的翻倍

LBM测量期门控事件为0、显式同步依赖等待为0，四核owner均为工作负载task，
gem5和FastSim的inactive/unknown均为0。两边退休宏指令均为13,213,553。
FastSim active cycles为21,410,336，gem5为10,720,527.988。
因此差异已是实际建模窗口时长差，不能解释为误把off-CPU等待加入CPI。

Graph500测量期1196个门控事件、52次显式依赖等待，但其基线CPI是低估。
“新增等待造成两个负载统一变慢”与实际误差方向不符。

### 2.2 保留scheduler，只去掉OpenMP同步

所有行使用`workload-task active cycles / 各自退休宏指令`，人口均匹配。
相对误差带符号；绝对误差单位为cycles/宏指令。每个模式各有两个负载，
不得把重复变体当成新增独立case计算全表MAPE。

| 负载 | 模式 | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差 |
|---|---|---:|---:|---:|---:|
| LBM | scheduler+OpenMP同步基线 | 0.811328 | 1.620331 | +99.7134% | 0.809003 |
| LBM | 仅scheduler | 0.811328 | 1.624584 | +100.2376% | 0.813256 |
| Graph500 | scheduler+OpenMP同步基线 | 2.269707 | 1.481147 | −34.7428% | 0.788560 |
| Graph500 | 仅scheduler | 2.269707 | 1.380550 | −39.1750% | 0.889157 |

基线两案CPI MAE为0.798782 cycles/宏指令。去掉OpenMP同步没有恢复旧精度；
对Graph500反而更差。LBM的微小差异来自warmup/分块及后续传播，不能称为
测量期新增同步等待。

### 2.3 关闭全部计划，比较独立elapsed

关闭后缺少owner，当前验证器正确将active CPI置为不可用，不补造active分母。
Graph500还触发旧physical/CPL PMU人口不匹配，该变体整体为failed，不能发布其PMU精度；
共同elapsed有独立证据，仍可作为关闭约束的诊断。

| 负载 | 模式 | gem5 elapsed(ms) | FastSim elapsed(ms) | 相对误差 | 绝对误差(ms) |
|---|---|---:|---:|---:|---:|
| LBM | 基线 | 0.892484 | 1.782410 | +99.7134% | 0.889927 |
| LBM | 无同步/任务计划 | 0.892484 | 1.797579 | +101.4131% | 0.905095 |
| Graph500 | 基线 | 2.724959 | 1.630476 | −40.1651% | 1.094483 |
| Graph500 | 无同步/任务计划 | 2.724959 | 1.522173 | −44.1396% | 1.202786 |

LBM关闭计划后更慢约0.85%；Graph500更快约6.64%，使低估加重。
scheduler/同步组合不是主要误差来源，也不是完全没有数值影响。

### 2.4 旧二进制的关键反证

将同一无计划配置、同一新FST交给同步consumer之前的旧二进制：

| 负载 | 当前无计划sum core cycles | 旧二进制sum core cycles | 当前/旧warmup barrier cycles |
|---|---:|---:|---:|
| LBM | 20,668,914 | 20,668,914 | 17,050,929 / 17,050,929 |
| Graph500 | 17,538,505 | 17,538,505 | 5,003,714 / 5,003,714 |

全部逐线程周期、warmup与测量record/macro人口也完全相等。
这证明新输入上的基础时序偏差可由旧模型复现；近期代码不是该基础偏差的必要条件。
它不证明历史所有输入都位级不变，也不对新gem5采集器作全面正确性认证。

## 3. DRAM和buffer TSO的干预结果

“旧DRAM近似”删除本轮新增的时序override，回到冻结历史profile中的零约束/旧值，
仍保留8通道及其他配置。这故意破坏当前采集的时序对齐，只用于归因，不能作修复。
“驻留证书”仅开启已有`core.buffer_tso_residence_certificate`；
“证书+生命周期”在此基础上再开启已有`core.buffer_tso_prepare_lifecycle`。
五种active可评分模式的两案均通过来源/人口门禁；这不等于候选精度准入。

| 负载 | 模式 | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差 |
|---|---|---:|---:|---:|---:|
| LBM | 旧DRAM近似 | 0.811328 | 1.577337 | +94.4142% | 0.766009 |
| LBM | 驻留证书 | 0.811328 | 1.465303 | +80.6055% | 0.653975 |
| LBM | 证书+生命周期 | 0.811328 | 1.549470 | +90.9795% | 0.738142 |
| Graph500 | 旧DRAM近似 | 2.269707 | 1.462698 | −35.5557% | 0.807010 |
| Graph500 | 驻留证书 | 2.269707 | 1.382424 | −39.0924% | 0.887283 |
| Graph500 | 证书+生命周期 | 2.269707 | 1.412342 | −37.7743% | 0.857366 |

新增tRAS等约束影响数值，但不是翻倍误差的主要来源。不能通过删除真实约束调回CPI。
驻留证书让LBM改变0.155028 CPI，仍留下0.653975绝对误差，Graph500反而退化；
不能把旧的门禁诊断直接套成新两案完整根因，也不推广这些实验开关。

对应活跃路径是`prepare_buffer_tso_epoch()`的同set保守预筛选：没有驻留证明时，
准备可能被拒绝，普通store重新承担完整服务，再沿FIFO send/response与SQ反压传播。
这条机制在[2026-09-21历史干预](lbm-buffer-tso-error-mechanism-20260921.md)中已证实；
本轮只证明该开关及其预热传播会影响新案，没有量出全部拒绝原因和逐store关键周期。
生命周期关闭时`buffer_tso_prepares`等计数未完整启用，不能拿基线中的0当作零准备。

另尝试恢复`core.store_wait_relief=true`，两案都在配置验证阶段拒绝：
`core.tso_store=buffer is incompatible with core.store_wait_relief`。
该项没有仿真结果，不是已测得无效果，也不是一个可直接恢复的修复。

## 4. 为什么“以前没有”不能直接成立

1. **大误差不是本次才出现。** 上一批双通道SPEC研究输入在退休口径修改前，LBM
   已为+240.634%、绝对误差2.357242；Graph500为−33.758%、0.798810。
   见[前批报告](spec-two-cache-validation-20261015.md)。当前LBM差异反而缩小，Graph仍相近；
   两批ROI/通道不同，不能将数值变化当作某项模型修改的单变量收益。
2. **旧平均值不是同一新输入的回归基线。** 9月30日35案的MAPE7.47%、MAE0.06176，
   来自[历史矩阵](perf-aligned-pmu-catalog-20260930.md)，不是当前两案的新窗口；
   旧计数以physical/user-plus-kernel诊断为主，未使用现在的task-onCPU active合同。
   当前LBM缩为64³、Graph为scale18/edge16并排除验证，stat在有界warmup后打开，
   目标由10M缩为5M user-UOP；线程建立/内核人口和cache前史也改变。
3. **旧低误差也有配置边界。** 9月21日同一LBM C4历史默认诊断为2.068267 vs
   0.817326（+153.05%、绝对误差1.250941），实验驻留证书可到0.772321
   （−5.5063%、绝对误差0.045005）。后续部分低误差pilot明确开启certificate/lifecycle。
   不能把这些实验候选和当前默认当成同一配置。35案原始overlay目前本地缺失，
   尚不能声称已经逐项核实整套旧矩阵的开关；本轮组合开关也未在新输入复现旧低误差。
4. **active记账不是全部解释。** LBM两边inactive/unknown均为0，关闭计划后目标
   elapsed仍约翻倍。Graph关闭OpenMP同步仍低估。不能用重新命名cycle来消掉时序偏差。

因此，证据支持“旧模型对新的工作负载阶段/状态产生了不同误差，并有历史配置与
统计范围变化”，不支持“已证明scheduler引入主要退化”。目前不能把全部差异量化拆成
数据规模、ROI、预热、缓存替换或内存模型各占多少。

## 5. 下一步应查什么

先固定当前FST、8通道及真实时序，不继续扫延迟常数或删除同步约束。
LBM需要按store绑定prepare是否准入、拒绝原因、FIFO send、response、SQ release，
沿退休关键路径定位首个放大的区间，再与gem5对应事件核对。
Graph500需要在相同task/动态指令窗口分开用户计算和内核执行，核对load服务完成、
依赖唤醒与退休的首个提前点；多报miss不代表等待及并行度已经正确。

现有新源以汇总证据为主，尚无覆盖上述首分叉的双方完整逐事件配对，不能凭stall累计
或PMU总数指定剩余主因。若补充观察，应只采必要的有界窗口、保留既有输入和错误方向
相反的控制案，再决定模型修复。当前结论是**主要scheduler归因被反证，部分TSO影响
已验证，完整CPI根因仍待关键路径取证**。

## 6. 旧LBM C4约5%与当前约100%的直接追问

旧9月23日store-edges报告的LBM C4为gem5 0.817326、FastSim 0.776815，
相对误差−4.96%、绝对误差0.040511 cycles/宏指令；当前为0.811328/1.620331，
+99.7134%、0.809003。gem5参考CPI接近，FastSim预测则约翻倍，不能把差距仅解释为
百分比放大、负载本身变慢或active改名。

旧pilot还明确关闭`core.store_forwarding`，当前配置为true；前一轮只恢复certificate/
lifecycle的消融并不等于恢复全部旧配置。补测固定同一新FST、当前二进制和gem5参考：

| 变体 | gem5 CPI | FastSim CPI | 相对误差 | CPI绝对误差 cycles/宏指令 |
|---|---:|---:|---:|---:|
| 当前基线 | 0.811328 | 1.620331 | +99.7134% | 0.809003 |
| 仅关闭store forwarding | 0.811328 | 1.617975 | +99.4231% | 0.806647 |
| 证书+生命周期开启，forwarding关闭 | 0.811328 | 1.562404 | +92.5736% | 0.751076 |
| 上行再组合历史DRAM近似、L2/LLC TreePLRU | 0.811328 | 1.509249 | +86.0221% | 0.697921 |

三案维护验证均passed、全部checks为true、人口匹配。证据位于实验根的
`782.lbm_r/forwarding-off`、`documented-old-flags`、`reconstructed-old-profile`，
命令及修改清单在各自request/receipt.json。最后一行刻意改变当前采集的替换/时序
匹配，只是按旧文档重建的配置族，不是准确的旧cfg复现，不作为生产修复。
这些变体不能合并作为独立负载求MAPE。

因此store forwarding及已知开关组合也不足以解释主要差距。旧5%实验的完整FST、
配置归档与对应二进制目前本地缺失，缺少“旧输入×新旧模型”的完整交叉复验。
前面保存的pre-sync二进制是同步consumer之前的版本，不是已认证的9月23日5%二进制；
它的同值检查只能反证近期scheduler归因，不能替代上述缺失对照。

准确状态仍是：已证实当前预测有大幅偏差，已排除若干主要嫌疑，尚未解释旧5%为何
变成当前约100%。新网格/编译产物、ROI阶段、任意指令预算测量起点及预热状态是待分离
变量，不能仅凭它们发生变化就声称已找到根因。尤其需要检查预算起点的在途状态与
FastSim全核warmup barrier交接是否一致；目前只有代码上的待验证假设，尚无因果实验。

### 旧并行方式的进一步核对

本地SPEC原始`782.lbm_r/Spec/object.pm:12`的bench_flags明确为
`-DSPEC_AUTO_SUPPRESS_THREADING -DSPEC_RATE`，随附simple-build脚本也使用该宏。
`lbm.c:276`仅在未定义此宏及`SPEC_SUPPRESS_OPENMP`时启用计算内核的OpenMP。
当前研究构建则显式使用`-fopenmp -DSPEC_OPENMP`、没有上述抑制宏，实采确认4个
OpenMP task。因此“标准rate LBM”与“当前单问题协作OpenMP LBM”并非同一并行定义。

但是旧低误差源使用定制`lbm_r_base.gem5-x86-linux`，不能由当前原始SPEC构建规则
倒推出其实际编译宏和launcher。现存补丁能证明旧Zstd采用fork worker wave；
不能将该证据自动推广到旧LBM。旧LBM究竟采用协作OpenMP、多个rate副本或其他
定制分工仍需原二进制/构建记录及启动参数。该缺口应优先补齐；此前将新旧LBM
默认视为同一种并行方式没有充分证据，也不能反过来宣称已证实旧LBM没有OpenMP。
