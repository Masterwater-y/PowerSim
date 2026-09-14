# TeaLeaf CPI：gem5 / FastSim 中间状态对比

日期：2026-09-08。主例为最差的 DSE L1D64 C4；LLC32 C4 为正误差对照。
本轮读取原始结果并新增离线统计工具，没有改变模拟时序、默认配置或重采 gem5。

## 1. 结论

L1D64 C4 的欠估主要体现为 **ROB 头已经发射的 load 等待不足**。严格配对的两个
窗口，分别有 4,999 / 5,314 cycles 的两端差值落在这一类别，总退休跨度差值为
5,115 / 5,408 cycles。这里是互斥周期类别的账面分解，不是模型干预的因果贡献率。

向请求生命周期追溯，有两个独立的明确差异：

1. gem5 尚在等待同线 parent response 的 load，在 FastSim 已按 2-cycle 本地命中
   返回；同线请求的 issue 间隔和 parent 生命周期也同时不同。
2. 两端路径都为 DRAM 的普通 load，FastSim 服务时间仍明显过短；原二进制的关键
   请求见证分别定位到非 refresh 服务/排队差异及一次真实 rank refresh 长尾。

ROB 容量阻塞会随这些 load 的完成时间向后传播，因此“ROBFull 很多”本身不是独立
根因。现有两端原生 ROBFull 计数的事件单位、观测区间均不一致，不能直接相减。
全局增加 load 延迟也不能解决 TeaLeaf：LLC32 的正误差窗口存在相反的 SQ/发射前
过度等待和测量入口缺失 cache 状态。

## 2. 完整 ROI 与请求人口

L1D64 C4 为每核 10M user UOP、合计 40M，带 native kernel，Q=1024。
这里 CPI 沿用 DSE 的 cycles/user-UOP：gem5 **0.6138814**，FastSim
**0.503437875**，signed error **−17.9910%**。

| 核心 | gem5 active cycles | FastSim cycles | FastSim − gem5 |
|---:|---:|---:|---:|
| 0 | 6,410,303 | 4,893,156 | −1,517,147 |
| 1 | 6,515,376 | 5,427,714 | −1,087,662 |
| 2 | 4,552,996 | 3,626,023 | −926,973 |
| 3 | 7,076,581 | 6,190,622 | −885,959 |
| 合计 | **24,555,256** | **20,137,515** | **−4,417,741** |

按 scope 匹配的 native summary 重新汇总全部非 idle 类别：

| L1D64 C4 计数 | gem5 native | FastSim | 解释 |
|---|---:|---:|---|
| committed memory UOP | 5,646,619 | 5,646,619 | 人口一致 |
| L1D tag misses | 273,254 | 273,284 | 层级诊断计数，差 30 |
| 私有 L2 tag misses | 271,555 | 271,752 | 层级诊断计数，差 197 |
| data DRAM read transactions | 186,893 | 187,054 | 差 161，即 +0.0861% |
| Sequencer coalesced fragments | 591,467 | 默认模型未提供等价 native follower 计数 | 必须逐事件比较 |

gem5 native hierarchy incomplete UOP 为 0。tag/协议人口不能冒充严格硬件 PMU；
这些数字只能排除“大量漏掉 DRAM 请求”这一简单解释，不能证明逐事件路径或时间正确。

另核对 22 个 TeaLeaf case 的人口。ROB256 C8 的 DRAM read 为 169,298 / 169,675，
LLC128 C8 为 198,606 / 198,925，但各自 CPI 仍低估 17.3517% / 17.1778%。
这说明另外两项尾部也需要检查时序，而不能只看 miss 总量。本轮没有为这两个 C8
配置取得完整逐事件 stage 配对，不把 L1D64 的机制直接宣布为它们的已证根因。

## 3. 完整 ROI 的 ROB-head 阶段统计

新增 `tools/audit_fst_commit_gaps.py`，扫描两次原二进制完整补采的 8 个核心：
以 FST syscall auxiliary marker 过滤后的连续 hardware micro_seq 关联 stage labels；
核对 core/thread、记录守恒、时钟、CPL 区间及单调退休。分类区间为
`(first hardware commit, last hardware commit]`。只对零退休周期分类，不重复累计
同周期多个 UOP 的等待。不把 load completeTick 当成数据返回。

下表是 **gem5 elapsed 时间**，包括无法逐区间定位的 idle：

| gem5 互斥零退休阶段，4 核合计 | L1D64 C4 | LLC32 C4 |
|---|---:|---:|
| 下一条 committed-path UOP 尚未 fetch | 558,751 | 2,160,085 |
| 已 fetch、尚未 issue | 435,048 | 6,726,018 |
| 已 issue 的 load 尚未 commit | **17,355,995** | **15,096,187** |
| 已 issue 的 store 尚未 commit | 14,364 | 208,097 |
| 已 issue 的其他 UOP 尚未 commit | 15,074 | 50,206 |
| 有退休的周期 | 6,583,379 | 7,070,727 |
| 全 ROI idle budget（不是另一可相加分类） | 407,378 | 1,601,715 |

L1D64 core1 的 idle 仅 3,922 cycles，而该核 issued-load-head 等待为 4,863,377；
core3 分别为 4,924 / 5,326,699。负尾部的等待现象不是只出现在一个 10K 窗口。
这不区分 load 自身未响应和响应后仍不能退休，须结合第 5 节的 native callback。

两例的 core2 各有首个 hardware commit 之前 **23 cycles** 未覆盖，单列而未强行
分摊。少量 syscall facts 没有 O3 stage：L1D64 8 条，LLC32 23 条；工具明确排除，
不把它们当作硬件 UOP。FST 与 full recollection 的来源沿用此前原二进制/CPL 和
功能记录核对；此次全流扫描追加的是 micro_seq/记录数量/时钟/边界守恒，不新增
“所有 PC/地址重新逐字节验证”的声明。

FastSim 原审计输出的对应小计：L1D64 issued-load-head 为 13,848,767 cycles；
LLC32 fetched-not-issued 为 11,173,350 cycles。但源码显示 `response_residuals`
在一次 feedback 统计开始时清零，head-gap 仅在 `stage_uops != 0` 时记录，因此
部分首 UOP 的跨 checkpoint 间隔未进入这些小计。**不以这些小计计算完整 CPI 的
来源百分比。** 全 ROI idle 也不能反复从每个阶段扣除。以下密集窗口直接从相同
逐 UOP 的退休时间重算，避免这两个聚合问题。

## 4. 同一窗口的精确周期分解

主窗口：L1D64 core1、source record ordinal 2,509,999–2,519,999，共 10,001 条。
它来自原参考 gem5 二进制的完整 ROI；PC/地址/CPL/ASID/core/thread 已逐事件匹配。

| 互斥周期类别 | gem5 | FastSim | FastSim − gem5 |
|---|---:|---:|---:|
| ROB 头已 issue 的 load 尚未退休 | **8,559** | **3,560** | **−4,999** |
| 已 fetch、尚未 issue | 1 | 0 | −1 |
| 已 issue 的其他 UOP 尚未退休 | 2 | 0 | −2 |
| 尚未 fetch / store-head 等待 | 0 | 0 | 0 |
| 有退休的周期 | 1,497 | 1,384 | −113 |
| 总退休跨度 | **10,059** | **4,944** | **−5,115** |

gem5 有 **63 条** load 成为 issued-load 阻塞头，FastSim 只有 **47 条**；零退休
片段分别为 65 / 47。负误差不是“Fetch 没停够”或单纯 commit width 的表现。
这些窗口跨度是端点差，不能除以窗口记录数后替代全 ROI CPI。

独立诊断窗口：L1D64 core1、ordinal 1,428,000–1,441,000，共 13,001 条，
issued-load-head 差值 **−5,314 cycles**，总跨度差值 **−5,408 cycles**，其余差值
为 productive cycles 的 −94。该窗口使用 v7 诊断 gem5 二进制；其 SHA 与冻结
参考不同，只用于独立机制见证，不替换正式 CPI 真值。

正误差控制：LLC32 core0、ordinal 268,087–270,500，共 2,414 条，窗口总跨度
FastSim 多 **2,345 cycles**，其中 **fetched-not-issued 多 2,364 cycles**；
issued-load 等待多 322，尚未 fetch 少 280，其他类别合计少 61。这里主要失配阶段
转为 issue 之前。LLC32 core2 的另一个 10,001-record 窗口则仅多 534 cycles，
再次说明不同窗口不应混成一个固定 workload 惩罚。

## 5. 从 ROB 头追到请求响应

### 5.1 同线 parent/follower 与可见性

v7 L1D64 密集窗口中，421 条严格单 line 普通 load 在 FastSim 是本地 private-cache
命中，gem5 是 Sequencer coalesced follower：

| 阶段统计 | gem5 | FastSim |
|---|---:|---:|
| admission → response / 模型服务时间均值 | **111.07 cycles** | **2.00 cycles** |
| 同一 load 的 issue→retire 尾部差均值 | 基准 | **−104.30 cycles** |

这里不是 gem5 多计一次 DRAM miss，而是已存在请求未完成时，后继 load 必须等
parent callback；FastSim 的路径/服务把其中部分等待隐藏了。

既有 generation 账本还显示，两端 follower 身份相同仅 149 条，FastSim-only 191，
gem5-only 272。gem5-only 的可分解事件中，165 条由 parent→child issue 间隔轴
改变，92 条由 parent response 生命周期轴改变，13 条任一轴都足够；反方向
191 条中 190 条由 issue 间隔改变。这个结果排除了“只统一加一段 hit latency”
就能闭合所有归属问题的解释。该分解也不能作为 CPI 收益相加。

### 5.2 匹配 DRAM 请求仍然过快

同一 v7 窗口的 **81 条普通 load**，两端都匹配 DRAM：

| admission → response / 模型服务 | gem5 | FastSim |
|---|---:|---:|
| 均值 | **334.44 cycles** | **165.37 cycles** |
| 中位数 | **361 cycles** | **161 cycles** |

同一 load 的 issue→retire 尾部均值差 **−170.40 cycles**，与服务差
−169.07 接近。store response 在 commit 后，已从这 81 条样本中排除。
最终成功 issue→admission 为 1 cycle，不代表此前没有 retry，也不用于否定所有
准入排队；不能用 O3 completeTick 代替 Ruby callback。

原二进制深尾部窗口已有 20 条精确 LSQ→Sequencer→DRAM 关联的关键 load，覆盖
7,071 / 8,559 个 load-head 阻塞周期：19 条非 refresh 请求为 gem5 243–358 cycles
（中位数 315）/ FastSim 161；另 1 条为 **1,504 / 161 cycles**。最长请求恰在
rank refresh 恢复时获调度，DRAM arrival→调度等待 1,309.81 cycles。

这 20 条 FastSim 的 canonical DRAM 排队均为 0，服务选择的 arrival 比最终 core
issue 早 85–962 cycles。说明 shared 服务选择与最终请求到达的时基/状态没有共同
闭合；不能把两个时基之差直接再加为等待，也不能把全部差异归因 refresh。

## 6. 为什么不能按 ROBFull 数量或统一内存延迟修复

L1D64 core1 原始 gem5 `rename.ROBFullEvents=335,559`，FastSim
`o3_rob_full_events=6,004,498`、`o3_rob_stall_cycles=4,554,690,601`。
后者远大于该核总周期 5,427,714，是多 UOP 重叠等待之和。

- gem5 `Rename::incrFullStat()` 统计 rename 因资源不足被阻塞/截断的调用事件，
  包含它实际执行的人口；FastSim 在每条 UOP 的 dispatch 被前驱 ROB release
  推迟时增计数，并累计位移。两者不具有可直接相减的事件单位。
- 原 gem5 stats 的 core1 `numCycles=7,081,647`，对应进程收尾时统一 dump；该核
  CPL ROI active cycles 只有 6,515,376。原生队列计数没有 per-core target-stop
  快照，不能声称它们已匹配 FastSim 的测量区间。
- ROB/IQ/LQ/SQ 最大配置容量只能证明配置，无法替代逐周期 occupancy；当前
  committed-stage labels 也不包含完整 speculative rename/ROB occupancy。

LLC32 的正误差见证中，同一 user store PC `0x4098b0`，FastSim fetch→issue
分别为 2,216 / 3,562 / 4,908 cycles，gem5 为 69；直接 gate 是 SQ capacity。
另有已证实的测量入口 cache 状态空洞，使 FastSim 走 460-cycle DRAM、gem5 实际
1-cycle 私有命中。它们都与负尾部需要补齐的响应等待方向相反。

因此后续应按请求生命周期对齐：operand/issue → admission → 唯一 parent/服务 →
callback/data-ready → consumer/ROB retire；store 的 commit→send→callback→SQ
release 单列。优先对 L1D64 的普通 load 做有界语义闭合，再用 LLC32 防止过度
串行化。现有 pending-fill、DRAM、FU 等失败候选仍保持关闭，本轮不重扫旧组合。

**用户明确的修复约束：** 上述 workload 名仅是证据用例。实现必须对相同事件语义
使用相同状态机，不读取 workload ID、case 误差或 gem5 timing label，不采用特定 PC
白名单、固定补偿周期或按配置拟合的残差。必须替换旧的分离时序路径，并在设计前
留出的独立 workload/窗口上验证泛化；已有 formal40/DSE54 不能替代这项独立验证。
完整约束见[优化决策](optimization-decisions.md)。

## 7. 产物、验证与边界

本轮产物目录：`tmp/tealeaf-intermediate-state-20260908/`。

- `tealeaf-case-populations.json`：22 个 TeaLeaf 配置的参考 CPI、逐核周期和 native 人口。
- `full-roi-gem5-head-gaps.json`：8 核全流阶段分类、边界/idle、最长阻塞 load、守恒检查。
- `full-roi-comparison.json`：两例完整 ROI 与 FastSim checkpoint 小计，明确覆盖差异。
- `paired-rob-gaps.json`：4 个密集窗的两端互斥周期分类与跨度差守恒。
- `native-component-matrix.json`：重新核对原始 audit 哈希的单请求 path/阶段统计；
  显式区分 load/store，不使用 pending-fill 候选替代基线。

复现全流统计：

```bash
python3 tools/audit_fst_commit_gaps.py \
  --collection tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full \
  --collection tmp/tealeaf-tail-timing-20260907/llc32m-c04-full \
  --workers 4 \
  --output tmp/tealeaf-intermediate-state-20260908/full-roi-gem5-head-gaps.json

python3 tools/analyze_paired_rob_gaps.py \
  --pairs tmp/tealeaf-tail-timing-20260907/l1d64k8-c04-full/dense-paired.json \
  --pairs tmp/paired-event-ledger-20260908.dZGudc/llc32-gem5-paired.json \
  --pairs tmp/cross-workload-native-v7-20260908/tealeaf-l1d64/dense-core1-pairs.json \
  --pairs tmp/cross-workload-native-v7-20260908/tealeaf-llc32/dense-core0-pairs.json \
  --output tmp/tealeaf-intermediate-state-20260908/paired-rob-gaps.json
```

验证：`python3 tests/test_fst_commit_gaps.py` 的 3 项测试通过（100 组随机事件流对
逐周期 oracle、syscall auxiliary 关联、错误 identity/稀疏/实验配置拒绝）；CMake
build 与 `./build/fastsim_tests` 通过。两个原始 full-ROI FastSim audit/control 的
scope metrics（排除宿主计时）及 threads 再次逐项相等。

未证明：完整 17.991% 的逐组件因果百分比分摊；所有 speculative/重试/跨窗 parent
的占用；ROB256/LLC128 的具体时序根因；任何候选的正式精度或吞吐改善。
本文的全 ROI 分布、独立窗口重复和严格请求见证支持上述定位，不能替代后续的
模型语义门禁、完整矩阵及独立吞吐验收。

相关来源：[尾部原二进制见证](tealeaf-tail-timing-20260907.md)、
[native 分段账本](cross-workload-component-matrix-phase1-20260908.md)、
[generation 双向身份交换](line-generation-admission-shadow-20260908.md)、
[测量入口状态](measurement-boundary-memory-state-phase1-20260908.md)。
