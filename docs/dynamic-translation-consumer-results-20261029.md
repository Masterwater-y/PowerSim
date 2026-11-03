# 动态翻译消费端与 Marian C4 单项采集（2026-10-29）

用户授权实施特殊地址分类、FastSim P1/P2消费端与Marian C4单项重采，保留旧FST并保护磁盘。本轮三步实施和单项采集/评分已完成；机制与来源检查通过，但Marian C4精度门槛未通过，P2不推广，停止后续矩阵重采。

2026-10-30后续定位到P2的source-order Sequencer错误纳入提交前deferred store，已修复并完成Marian同源复验，见[后续修复与诊断](translation-sequencer-diagnostics-results-20261030.md)。另确认原始stats终点比FST共同终点晚约1ms，下文DTLB等raw统计只保留为历史诊断，不作为同窗机制验收。CPI独立冻结账未受此终点差影响。下文保留本轮原始结果，不能当作修复后的P2数据。

## 采集事实与格式

`.fst.xlat.gz`升级为FSTXLT1 version2，继续映射字典去重、稀疏token更新和流式gzip level1压缩。definition在PTE地址之后追加kind、实际请求PA锚点和VA锚点；v1仍可解码。IO/MSR等成功的非分页访问具有独立类型；分页APIC保留真实叶页/PTE路径以及最终编码后的请求地址。PCI配置访问会改变VA/PA页内偏移，因此用显式VA/PA锚点，不假设低12bit相同。未知普通翻译继续保留unknown并在启用对应模型时拒绝。

KVM的physical-only MMIO请求没有VA：观察helper在读取VA之前检查hasVaddr，不对这些未进入O3 FST的请求构造翻译证据。原始KVM启动断言已复现为RED，修复后helper单测及实际FS启动均通过。源/overlay及上游patch/hash保持同步。gem5两轮编译均使用 `-j128`，最终producerSHA256为 `ed296ff1965a9f81b7cbd67799dba66fe16b5408cebf12dae364c78daed46961`。

## 消费路径

C++ reader独立流式解压并审计gzip CRC、单member/EOF、字典与关联、行hash、全热流FNV、ASID/token身份和人口守恒；只保留字典与稀疏记录。source wrapper按原始ordinal代理事实，不改变64byte热记录。

DTLB按实际叶几何建立key，大小页共用同一容量LRU。动态拆页、物理映射/PTE路径变化通过观察到的映射事实使相交旧条目和旧walk generation失效；页大小unknown不按4KiB回退。非分页成员不虚构TLB访问，分页MMIO保留翻译。PTE miss沿实际3/4级物理路径逐级进入缓存/Sequencer/DRAM，后续请求受前一PTE响应约束，data等待全部相关walk完成。跨fragment使用各自实际物理地址。

只解除已实现并测试的hierarchy与buffer/bounded、pending-fill、shared-service兼容边；仍使用scalar/transactional profile，不放开其他竞争求解器。固定 `dtlb.page_walk_latency` 不修改：物理路径本来就按实际深度提供每次walk的下界；全局固定常数的等值约束仅对synthetic模式保留。

## 小型实际FS验证

`tests/workloads/dynamic_translation_probe.c`在guest中生成2MiB大页，再用mprotect拆成4KiB。实际新producer输出：

| 指标 | 结果 |
|---|---:|
| 全热记录 | 2,022,361 |
| 内存记录 | 464,068 |
| 严格已知 / unknown | 464,068 / 0 |
| 实际21bit→12bit转移token | 512 |
| 压缩翻译sidecar | 20,428 bytes |

严格Python源绑定审计通过；C++消费端对该完整实际trace的P2 CLI回放退出码0。PTE物理请求91,329条、synthetic0、fixed fallback0；这是功能与连接验证，未将该探针配置/CPI用于工作负载精度结论。收据在 `tmp/dynamic-translation-consumer-20261029/smoke-strict-audit.json` 和 `smoke-p2.json`。

## 磁盘与单项重采

初始全项目FST约242.24GB，旧Marian C4 FST为3,754,030,688 bytes，实际空闲约65GiB。新采集独立目录，单项FST预算12GB，全局400GB上限/395GB提前停止，实际空闲底线30GB；按秒监控并停止整个producer进程组。运行上限7200秒，详细预热墙钟上限1800秒，不修改guest的500K预热预算。

旧checkpoint绑定旧producer hash，维护的完整身份校验拒绝直接复用；没有改写旧收据或绕过门禁。新建producer绑定的staged checkpoint，预留2GB加12GB FST及30GB底线。物理内存按gzwrite直接保存，避免32GiB未压缩中间文件。producer以hardlink冻结，不复制大二进制或旧checkpoint。

Marian C4参数继续使用32GiB、4 workers、buffer114、500K warmup、10M最快核共同终点，配本次新stats。流程 `run-capture.py all` 执行新checkpoint→capture→维护来源审计；root持续检查磁盘和每阶段收据。旧FST、stats、日志、checkpoint保持完整。

实际采集退出码0，墙钟889.88秒，详细预热320.68秒；新FST合计3,744,568,864 bytes。四核17,022,671条内存记录全部通过源绑定严格翻译审计，unknown为0，四个压缩sidecar共213,782 bytes。新checkpoint实际573MiB。收据在 `capture-pipeline.log`、`translation-summary.json`、`translation-audits/`；旧4个Marian C4 FST已逐文件对照旧SHA256/大小收据确认不变，见 `old-fst-retention.json`。本轮没有删除任何旧来源。

## 同源验收和停止决定

使用本次新FST与新stats，P1/P2均使用相同scalar/transactional profile和同一冻结FastSim，区别只有是否启用物理hierarchy walk。P1使用混页DTLB与既有固定walk；P2再接入真实PTE链。没有用旧统计给新FST评分，也没有把历史不同输入的CPI变化归因于页大小修复。

独立目标身份检查1/1 valid；两个arm的直接参数检查均为130/130、mismatch0；维护native validator均发现1/1、passed1、failed0、pending0、native PMU scored1，CPI指令人口匹配，active scope为workload-task。native缓存真值来自Ruby/SLICC守恒摘要，path-class不参与评分。这里的passed是来源/守恒通过，不是精度通过或SPR硬件等价认证。

| Marian C4 arm | gem5 active CPI | FastSim active CPI | 相对误差 | CPI绝对误差 |
|---|---:|---:|---:|---:|

| P1 | 4.947665 | 6.032176 | +21.92% | 1.084511 |
| P2 | 4.947665 | 6.107873 | +23.45% | 1.160208 |

| 单案聚合（各arm N=1） | CPI MAE | CPI MAPE |
|---|---:|---:|
| P1 | 1.084511 | 21.92% |
| P2 | 1.160208 | 23.45% |

评分原件：`validation-p1/summary.json`、`validation-p2/summary.json`；简表 `pilot-comparison.json`。二进制SHA256为 `e37aaa9d9d03ad2b906a253b4d9f0c8a677dbc02bc0f7cda13e8f2f9efc0386f`，运行/配置/输出指纹在各 `runs/<arm>/`。

| 机制诊断 | gem5 stats | P1 | P2 |
|---|---:|---:|---:|
| DTLB miss | 1,631 | 功能279 / timing499 | 功能279 / timing483 |
| FastSim DTLB等待周期总和 | — | 19,286 | 12,431 |
| 动态物理PTE walk | — | 0 | 483 |
| 物理PTE读请求 | — | 0 | 1,841 |
| synthetic / fixed fallback walk | — | 0 / 0 | 0 / 0 |
| PTE层次额外等待观测周期 | — | 0 | 16,002 |

DTLB miss没有对齐。四核字典都包含实际用户区21bit叶页（详见`user-leaf-definition-counts.json`，这是定义数而非访问次数）。gem5 dtb统计含投机翻译与统计/排空边界，FastSim这里是已提交路径模型；本表是诊断对照，不将不同人口的计数比值宣称严格匹配。实际叶页事实和严格覆盖已认证，不能据此宣称TLB行为及CPI都已认证。

P2相对P1的CPI绝对误差增加 **0.075697**，超过0.05停止线；对应active cycles增加712,867。直接PTE额外等待观测16,002周期不能将该变化定量闭合，缓存状态、请求时序与队列的间接作用尚待定位，不能把不同重叠阶段简单相加解释CPI。当前P1的DTLB等待总和仅19,286周期，仍有约10.21M active-cycle残差；本轮没有证实“大页修正足以解释Marian主要CPI残差”的推断。

**停止采纳和推广：** `dtlb.hierarchy_walk`继续保持显式开关；未修改维护默认配置。保留P1/P2代码、严格的新数据与完整失败精度证据；不继续其余五项重采、S2/S3、held-out或吞吐推广。没有通过调整固定延迟常数、删除unknown成员、换参考或修改分母消除误差。

## 最终验证与实施选择

- `cmake --build build -- -j16`、`LD_LIBRARY_PATH=/opt/gcc-11/lib64 ./build/fastsim_tests`通过；reader、混页DTLB、physical hierarchy三个专门C++入口通过。新增断言测试在Release下显式启用断言；初期Release可能关闭断言的问题已纠正，最终覆盖以重新执行后的结果为准。
- 独立Python wire测试23项、C++/Python内容身份25项、native validator测试13项、retirement metadata回归9项通过。producer capture/writer测试包含PCI偏移、physical-only KVM、页拆分与已知/未知混合成员。
- 因新动态文件影响预测，C++/Python功能输入身份与validator缓存指纹均纳入`.xlat.gz`；位9声明但缺文件会拒绝。静态链接测试的zlib依赖同步更新。
- 审核修复global/non-global相交替换的残留条目、跨叶new/merged计数及版本元数据生命周期。最终单条PTE依赖、真实最终响应→CPU issue/完成、同页follower等待、非空store send断言和跨两页非连续PA均在三种允许profile下验证。hierarchy+coalescing仍明确拒绝，没有放开未验证组合。
- 部署镜像、manifest新增文件/patch SHA核对通过；上游增量仍在patch0004，新文件仍用overlay。无commit/merge/push。

本轮自主选择及代价：延续既有脏工作树，代价是结果不构成干净bootstrap证明；改用v2显式VA/PA锚点修正PCI低位变化假设，代价是需要新reader和少量字典字节；保留分页APIC与非分页IO/MSR区别，避免伪造或抹去页表事件；新建hash绑定checkpoint而不弱化旧身份，代价是约573MiB和388.24秒准备；固定walk常数保持12，物理模式按实际路径深度建立下界，synthetic继续等值校验，代价是两类模式有明确不同资格条件。为保留未提交的审计证据，保留本计划工作目录，不按技能中“已有git历史后删除工作目录”的结束步骤删除它。

未认证范围：完整并发walker穷举、堆内存/正式吞吐、其他五项数据、全矩阵精度和SPR硬件等价。同地址空间且叶几何/路径不变的独立TLB失效（如INVLPG）没有由本sidecar单独表达；其对当前miss差异的贡献尚未量化。原生来源通过、翻译零unknown、机制单测通过与CPI精度通过始终分开记录。
