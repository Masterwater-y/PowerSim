# R0/R1 冻结恢复版：退休 cache miss 误差来源分析

日期：2026-11-11。分析对象为恢复到主工作区的 **R0/R1 冻结 `v2-wait`**，不是失败的 R4 `merge/publish`，也不是此前只修 store 重复收费的 publish。二进制 SHA-256：

`dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`

本次读取既有五负载同输入回放和维护验证器的 native 证据，未修改模型源码、配置或二进制，未新增回放或吞吐测量。逐核 macro/UOP 人口已由原验证检查；恢复版与冻结版五负载模型结果一致。本报告继续执行用户“忽视这些极少量的 unknown，也要报告”的授权，保留 unknown 和原门禁失败，不升级为 SPR 真机 PMU 认证。

## 1. 主要判断

不同负载的现象来自两类误差叠加：**回填期间的退休 load 成员识别**，以及**缓存服务路径与其到退休宏指令的投影**。它们对三个层级的作用不同。

- **Graph500：** 最集中。三个层级请求 tag miss 均在 0.02% 内，退休 L2/L3 也接近；L1 多报 1,369,794 次，代数分解中 1,369,764 次来自“模型新增回填修正减 native estimated”的差额，占净差的 99.9978%。主要检查 L1 fill 观察器及其时间/身份关联。
- **XZ：** L2/L3 约 +3%，L1 −55.55%；L1 漏报主要集中在 core 0。该核 native follower 109,211 个，模型 merged 1,512 个；不能用“全负载转发过多”一项解释这条核的漏报。
- **Marian：** 四核均少识别 L1 回填成员，L1 −31.53%；退休 L2/L3 接近，但所有 demand 的 LLC 请求 −50.47%。两个观察人口必须分开判断。
- **Zstd：** L1 +50.19%，L2 +109.09%，L3 +122.62%。这里既有私有缓存请求偏差，也有显著的退休 load 服务归属偏差。LLC 请求总数实际仅 −2.46%，所以不能说整个 L3 缓存 miss 都翻倍了。
- **FLAC：** L1 −20.20% 主要是新增修正不足；L2/L3 分别只多 103、41 次。L3 +24.85% 的百分比同时受 165 次的小参考分母影响。其请求路径和 split/retry 处理还存在独立差异。

| 负载 | 退休 L1 miss 相对误差 | 退休 L2 miss 相对误差 | 退休 L3 miss 相对误差 |
| --- | --- | --- | --- |
| Zstd | +50.1912% | +109.0888% | +122.6227% |
| XZ | -55.5467% | +3.4365% | +3.2281% |
| FLAC | -20.1977% | +9.8282% | +24.8485% |
| Graph500 | +27.9210% | +0.0104% | +0.0025% |
| Marian | -31.5316% | +3.1412% | +2.1026% |

以上均为 `(FastSim−native)/native`，正号表示多报。完整计数如下：

| 负载 | 层级 | FastSim 全量预测 | native 已知参考 | 有符号计数差 | 绝对计数误差 | 相对误差 | native unknown |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Zstd | L1 | 1,021,776 | 680,317 | +341,459 | 341,459 | +50.1912% | 0 |
| Zstd | L2 | 64,322 | 30,763 | +33,559 | 33,559 | +109.0888% | 0 |
| Zstd | L3 | 58,808 | 26,416 | +32,392 | 32,392 | +122.6227% | 0 |
| XZ | L1 | 80,809 | 181,784 | -100,975 | 100,975 | -55.5467% | 1 |
| XZ | L2 | 12,130 | 11,727 | +403 | 403 | +3.4365% | 1 |
| XZ | L3 | 11,736 | 11,369 | +367 | 367 | +3.2281% | 1 |
| FLAC | L1 | 35,611 | 44,624 | -9,013 | 9,013 | -20.1977% | 0 |
| FLAC | L2 | 1,151 | 1,048 | +103 | 103 | +9.8282% | 0 |
| FLAC | L3 | 206 | 165 | +41 | 41 | +24.8485% | 0 |
| Graph500 | L1 | 6,275,758 | 4,905,964 | +1,369,794 | 1,369,794 | +27.9210% | 0 |
| Graph500 | L2 | 277,740 | 277,711 | +29 | 29 | +0.0104% | 0 |
| Graph500 | L3 | 277,645 | 277,638 | +7 | 7 | +0.0025% | 0 |
| Marian | L1 | 179,540 | 262,223 | -82,683 | 82,683 | -31.5316% | 1 |
| Marian | L2 | 18,322 | 17,764 | +558 | 558 | +3.1412% | 1 |
| Marian | L3 | 17,142 | 16,789 | +353 | 353 | +2.1026% | 1 |

XZ、Marian 三个事件各有 1 个 native unknown；同一宏指令可在三个事件中均未知，不能将六个事件标记理解为六条独立未知 load。既有 unknown 区间检查表明，本批最大误差移动量约 0.009079 **个百分点**，不足以解释当前差距。Zstd 的 8,858 是模型旧资格 unknown，全量预测 unknown 为零；它不是 native 漏掉 8,858 个 miss。

## 2. 为什么 L1 可以很差，而 L2/L3 仍接近

### 2.1 一个底层事务可对应许多退休 L1 miss

以下是本项目共享解释器 `spr-retired-load-miss-v2` 的规则，不是通过总数相近推断的规则，也不代表已经完成真机硬件等价认证。

设 load A 发起一个冷行事务，数据尚未进入 L1 时，load B/C/D 访问同一行：

| 退休 load 的服务 | L1 miss | L2 miss | L3 miss |
| --- | --- | --- | --- |
| A 自己的冷请求，三个层级都 miss | 1 | 1 | 1 |
| B/C/D 等待 L1 PendingFill | 各 1 | 各 0 | 各 0 |
| 数据进入 L1 后的 load E | 0 | 0 | 0 |
| 完全 store forwarding 的 load | 0 | 0 | 0 |

B/C/D 的数据来自同一个在途 fill；它们没有各发一个独立的下层取数事务。于是一个底层请求可以产生四个退休 L1 miss，但只有一个退休 L2/L3 miss。模型只要把 E 错划入等待区间，或把 B 错划出等待区间，L1 会变化，下层可以保持不变。

共享实现中，`Level::PendingFill` 在 L1 置 miss 位，并排除本 fragment 的后续层级；完全转发、device、software prefetch 直接排除三个 miss。宏指令以 fragment/UOP 的 OR 归并，retry 的 sticky miss 保留。因此不同 fragment 的已证明 miss 不会因为另一个 fragment 命中而消失；请求片段数不能直接当退休宏指令数。

### 2.2 冻结模型怎样估计这种等待

`RetiredCacheMissEstimator` 每核使用默认 **4096 个直接映射槽位**，按物理 line 保存一个 `[issue,response)` fill 区间。后续同线访问落在区间内时，返回 L1 PendingFill；这个分支会覆盖该访问自己的普通 hit/miss 掩码。区间外保留其自身服务结果；没有匹配区间的 Sequencer alias 仅标记 fallback，并不证明它仍处于非驻留 L1 fill。

调用侧优先使用模型 `shared_stage_issue_cycle`，否则采用 canonical issue；response 优先用 `shared_response_cycle`，不合法时退回 `issue + replay latency`。这些是 **FastSim 的近似时钟**，不是 gem5 采集的时间。forwarding 在观察器中提前返回三个零，不创建数据 fill；store prepare、软件预取也不能冒充 demand-read fill。

由此存在几种可直接从代码确认的敏感点：

1. 用响应边结束区间，未独立保存“L1 数据何时可见”这一边；响应还未结束而数据已驻留时，会把自己的 hit 改成 PendingFill。
2. L1 局部服务缺共享时钟时，owner 和 follower 可能通过不同近似边得到区间位置；区间错位会多计或少计。
3. 以 line 加一个区间代替事务代次/服务身份；同线多代 fill、替换、失效与跨窗口时间顺序只能近似。
4. 直接映射碰撞会丢掉另一条 line 的区间；reported collision miss 不是后来漏掉多少 follower 的完整计数。
5. 完全 forwarding 与未证明 alias 的早返回/回退会改变哪些 load 能生成或消费区间。

这些说明**代码如何能产生双向误差**。本批聚合数据尚未给出逐 load 的 native/model 配对，不能据此断言 XZ 一定是区间过短、Graph500 一定是哪个响应延迟多了多少周期。

## 3. 将 L1 净差做完整的代数拆分

定义：

\[
F=\text{模型全量 estimated\_count},\quad
B=\text{模型旧资格 count},\quad
E=\text{native exact\_count},\quad
R=\text{native estimated\_count},\quad N=E+R.
\]

则：

\[
F-N=(B-E)+\big[(F-B)-R\big].
\]

第一项检查旧资格与 native exact 的差额；第二项检查模型额外修正和 native estimated 的差额。**这是严格成立的账目恒等式，不是逐指令的 owner/follower 混淆矩阵。** B 不是纯 owner 真值，E/R 是 certainty 分类；native estimated 还可包含 split/retry、权限等路径，两个 provider 的 exact/estimated 不构成匹配的因果人口。

| 负载 | 模型旧资格 B | native exact E | B−E | 模型新增修正 F−B | native estimated R | (F−B)−R | 最终 F−N |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Zstd | 704,166 | 475,462 | +228,704 | 317,610 | 204,855 | +112,755 | +341,459 |
| XZ | 60,035 | 54,501 | +5,534 | 20,774 | 127,283 | -106,509 | -100,975 |
| FLAC | 7,618 | 3,792 | +3,826 | 27,993 | 40,832 | -12,839 | -9,013 |
| Graph500 | 556,475 | 556,445 | +30 | 5,719,283 | 4,349,519 | +1,369,764 | +1,369,794 |
| Marian | 81,486 | 79,972 | +1,514 | 98,054 | 182,251 | -84,197 | -82,683 |

这里能得到比“所有 L1 错都是 follower”更精确的判断：

- Graph500：`+1,369,794 = +30 + 1,369,764`。本负载恰好没有 native 软件预取或 split/retry，native L1 estimated 4,349,519 与 native demand follower 相等，修正项的解释最干净。
- XZ：`−100,975 = +5,534 −106,509`。native L1 estimated 与 demand follower 均为 127,283；旧资格已有小幅多报，新增修正不足主导最终漏报。
- Marian：`−82,683 = +1,514 −84,197`。native estimated 182,251 与 follower 182,250 差 1；同样是新增修正不足主导。
- FLAC：`−9,013 = +3,826 −12,839`。原生 estimated 40,832 不等于 follower 37,857，差额不能全部命名为 follower。
- Zstd：`+341,459 = +228,704 +112,755`。约 66.98% 的净差在旧资格对 native exact 的账目项中，约 33.02% 在新增修正差额中；其 L1 本来就存在明显的服务路径/资格偏差。

另外，模型 merged 总数也不总等于 `F−B`。Zstd 为 330,120 对 317,610，FLAC 为 28,088 对 27,993，因为宏 OR、已有 miss 与 alias/fallback 可以重叠。不能逐条把 merged 加到旧 count 上。

## 4. 请求路径作为独立诊断，不能替代退休事件

下表是同一 `workload_task` 内的 native Ruby/SLICC **data-demand tag miss**，来源为维护验证器已审计的 `current_task_pmu_evidence`，不是 `kernel-events` 中的 taotrace path-class。请求人口与退休 load 宏人口不同，包含不同 memory members；不把它们相减构造“遗漏 load”。

| 负载 | L1D 请求：模型 / native / 误差 | 私有 L2 请求：模型 / native / 误差 | LLC 请求：模型 / native / 误差 |
| --- | --- | --- | --- |
| Zstd | 810,811 / 663,467 / +22.2082% | 82,204 / 68,173 / +20.5815% | 62,171 / 63,739 / -2.4600% |
| XZ | 119,409 / 110,599 / +7.9657% | 59,195 / 56,162 / +5.4004% | 39,151 / 38,760 / +1.0088% |
| FLAC | 26,163 / 18,479 / +41.5823% | 4,605 / 3,401 / +35.4014% | 1,076 / 1,491 / -27.8337% |
| Graph500 | 556,590 / 556,493 / +0.0174% | 277,741 / 277,711 / +0.0108% | 277,645 / 277,636 / +0.0032% |
| Marian | 537,483 / 593,102 / -9.3776% | 423,475 / 459,543 / -7.8487% | 223,214 / 450,620 / -50.4651% |

这张表回答两件不同的事：

- **Graph500**：请求、退休 L2/L3 均接近，问题高度集中于 L1 观察成员扩展。
- **XZ**：请求 L1/L2 有 +7.97%/+5.40% 小偏差，LLC +1.01%；这些解释不了 L1 −55.55% 的幅度与方向。
- **Marian**：退休 L3 +2.10% 和 LLC 请求 −50.47% 可以同时发生。退休 load 仅观察 load 的服务；整个 demand 层级还观察其他数据访问及其请求合并/权限路径。不能据前者宣称 LLC 模型整体正确，也不能据后者推断退休 L3 应该也 −50%。
- **Zstd**：LLC 请求 62,171 对 63,739，退休 L3 58,808 对 26,416。模型的两个量之比约 94.59%，native 约 41.44%；该比值是投影失配的诊断信号，**不是严格的 load 在所有 LLC miss 中的份额**，因为请求/宏指令以及 fragment/合并单位不同。
- **FLAC**：请求 L1/L2 多报而 LLC 少报，退休 L1 少报而 L2/L3 多报。说明不同路径及观察投影叠加，单一“缓存容量太小”的解释不成立。

## 5. 逐负载来源与尚未闭合的物理机制

### 5.1 Graph500：L1 回填扩展多报，缓存请求结果接近

native 普通 exact L1 miss 556,445，模型旧资格 556,475，仅差 30；native demand follower 4,349,519，模型新增修正 5,719,283，差 1,369,764。

四核都出现近似同规模多报，而非某个异常核；每核差约 34 万次。native 软件预取 0、split/retry 0、转发只有 870 个宏，模型转发 3,943 个宏。后两种已观察人口远小于 137 万净差，无法直接解释主差额。native L1 estimated 占总 L1 miss 88.66%，因此模型对等待区间很敏感。

**已定位：** L1 观察器的成员扩展差额，而不是底层请求 miss 总数恶化。**尚未证明：** 哪些 load 被多算、由区间结束边偏晚还是 issue 错位/代次关联造成、这些机制各贡献多少。需要以相同核/退休序号/fragment 对齐数据可见边与模型区间；不能直接将共享响应常数调短来拟合。

### 5.2 XZ：core 0 的 follower 几乎消失，其他核小幅高估

全负载 native follower 127,283，模型 merged 20,774。core 0 的对照更决定性：

- 模型 L1 3,139，native L1 110,858，差 −107,719。
- native follower 109,211，模型 merged 1,512，差 −107,699。
- 该核旧资格对 native exact 仅差 −20，`−107,719 = −20 −107,699`。
- 其他三核 L1 差合计 +6,744，抵消一部分 core 0 漏报，得到全负载 −100,975。

因此不能描述为“四核普遍 L1 cache 太容易 hit”。需优先检查 core 0 特有的 load 时间位置、fill interval 的创建/匹配和 local service alias；原生 L2/L3 mostly exact，而 PendingFill 排除下层，解释了 L2/L3 保持小误差。

全负载模型转发 2,501,991 宏，native 679,348，确有显著转发判定差异。但 core 0 仅为 16,630 对 8,104，其他三核才贡献主要转发数量差。**同一负载存在 forwarding 偏差，不足以证明它是 core 0 的 10.77 万次 follower 漏报主因。**

### 5.3 Marian：四核一致少计回填成员，其他 LLC 人口仍有大缺口

旧资格 81,486 对 native exact 79,972，相差 +1,514；模型新增修正 98,054 对 native estimated 182,251，相差 −84,197。四核 native follower 均高于模型 merged，幅度分别约 2.24、2.19、2.06、1.93 万。

native forwarding 很多，达 3,398,424 宏，但模型为 3,369,575，接近且方向是模型更少。它不能支持“模型大量额外 forwarding 直接删掉 L1 miss”的解释。软件预取仍有 523,998 宏；硬件预取关闭不消除这些指令或退休后 prepare 对 cache 状态的间接作用。

**已定位：** L1 净差主要在回填修正不足；另有整个 LLC demand 请求大幅少报。**尚未闭合：** 相近的退休 L2/L3 是否掩盖了逐 load 相反方向误判，LLC request 差额来自哪些 store/prepare/takeover/软件预取或合并路径。需要保留请求与退休两个账本分别配对。

### 5.4 Zstd：私有缓存请求偏差与退休服务投影同时存在

L1 净多 341,459 次，其中旧资格对 native exact 多 228,704，新增修正项多 112,755。native follower 181,738，模型 merged 330,120；与此同时 L1D 请求 +22.21%、私有 L2 请求 +20.58%，证明私有路径总数已有差异。

退休 L2 的 `F−B=−1`，退休 L3 的 `F−B=0`，所以它们的翻倍**不是新增 L1 回填修正批量复制父请求 L2/L3 miss 造成的**。具体账目为：

\[
\Delta L2=+39,877-6,318=+33,559,\qquad
\Delta L3=+38,476-6,084=+32,392.
\]

模型旧资格 L3 58,808，而 native exact 20,332；native estimated 再贡献 6,084，仍远低于模型。LLC 请求总数近似相等，说明“多少 LLC miss 服务被归到退休 load”及其前序路径不同，需要逐 load 查 own demand、store/prepare 服务接管、数据/权限分离与软件预取可见性。

模型转发宏 855,374 对 native 291,257，native split/retry 171,364、permission 37,812、软件预取 138,161。这些是重要候选路径。**人口可重叠；不能用这些总数加起来恰好凑出 32,392 并宣称根因闭合。** `R-MIXED-FRAGMENT` 也是 estimated 位的通用 reason，不能把它全部解释为跨行 fragment。

### 5.5 FLAC：回填漏计与小基数的下层偏差叠加

native L1 miss 44,624，其中 estimated 40,832，占 91.50%；模型新增修正只有 27,993。因此即使旧资格多报 3,826，最终仍少 9,013。

native split/retry 397,152，是本批非常显著的路径人口；模型 forwarding 667,894 对 native 331,041，也存在大差异。宏 OR、重试 sticky miss、跨行 fragment 的资格与服务归属都要查。native estimated 40,832 与 follower 37,857 不相等，不能把全部 estimated 误差当作等待时间误差。

下层 absolute count 差为 L2 103、L3 41；L3 的参考仅 165，故 +24.85% 并不表示大量额外内存事务。独立 LLC demand 请求反而少 415 次。需要同时展示绝对数、相对数和观察人口，避免据百分比给出错误优化优先级。

### 5.6 对 forwarding 的代码级补充

当前 `store_forward_window` 在功能程序序阶段维护最近 store 的完整地址范围，按 `effective_sq_entries()` 淘汰旧 store，并在 fence/serialize/locked RMW 等边界清空。匹配到一个完整覆盖 load 的 store 后，所有该 load 的 line event 都标记 forwarded，跳过 private/Ruby 请求。**此窗口未按该 store 的实际排空 callback 过期。**

这是结构窗口与原生 SQ 服务存活之间的明确模型差异，能改变自身 miss 及后续 cache 状态。Zstd/XZ/FLAC 的 forwarded 宏数差支持进一步调查；然而聚合差不能告诉我们被额外转发的 load 原本应为 hit、PendingFill 还是 cold miss，不能直接把转发差额计为 miss 差额。XZ 的逐核反例、Marian 的转发数量对照也说明必须分别归因。

## 6. 路径人口、逐核证据与 fallback 的边界

以下路径单位均是含相应路径的退休 load **宏指令**。一条宏可含多个路径，不能纵向求和为互斥人口；模型 merged 和 native demand follower 的定义也不完全相同。

| 负载 | 模型 merged 宏数 | native demand follower 宏数 | native L1 estimated 占比 | forwarded 宏数：模型 / native | native 软件预取宏数 | native split/retry 宏数 |
| --- | --- | --- | --- | --- | --- | --- |
| Zstd | 330,120 | 181,738 | 30.11% | 855,374 / 291,257 | 138,161 | 171,364 |
| XZ | 20,774 | 127,283 | 70.02% | 2,501,991 / 679,348 | 111,647 | 512 |
| FLAC | 28,088 | 37,857 | 91.50% | 667,894 / 331,041 | 14,685 | 397,152 |
| Graph500 | 5,719,283 | 4,349,519 | 88.66% | 3,943 / 870 | 0 | 0 |
| Marian | 98,054 | 182,250 | 69.50% | 3,369,575 / 3,398,424 | 523,998 | 1,048 |

五负载均无 native `prefetch_follower`；这批采集的 40 个 L1/L2 `enable_prefetch` 配置为 false，20 个 `sprEarlyStorePrepare` 为 false。它排除本批硬件预取/退休前 early prepare 开关遗漏的解释，**不代表软件预取关闭、退休 store prepare 关闭或所有间接 cache 状态效应均不存在**。不得为拟合本表关闭退休 prepare。

逐核 L1 对照：

| 负载 | 核 | 模型 L1 | native L1 | L1 有符号差 | 模型 merged | native follower |
| --- | --- | --- | --- | --- | --- | --- |
| Zstd | 0 | 330,126 | 216,793 | +113,333 | 110,728 | 59,930 |
| Zstd | 1 | 51,839 | 37,565 | +14,274 | 14,407 | 7,347 |
| Zstd | 2 | 315,098 | 213,132 | +101,966 | 100,506 | 56,904 |
| Zstd | 3 | 324,713 | 212,827 | +111,886 | 104,479 | 57,557 |
| XZ | 0 | 3,139 | 110,858 | -107,719 | 1,512 | 109,211 |
| XZ | 1 | 20,811 | 17,971 | +2,840 | 4,375 | 4,113 |
| XZ | 2 | 30,195 | 28,154 | +2,041 | 7,554 | 6,884 |
| XZ | 3 | 26,664 | 24,801 | +1,863 | 7,333 | 7,075 |
| FLAC | 0 | 2,997 | 2,772 | +225 | 1,188 | 1,102 |
| FLAC | 1 | 11,236 | 13,202 | -1,966 | 9,712 | 11,910 |
| FLAC | 2 | 12,866 | 18,734 | -5,868 | 10,195 | 16,461 |
| FLAC | 3 | 8,512 | 9,916 | -1,404 | 6,993 | 8,384 |
| Graph500 | 0 | 1,564,825 | 1,225,352 | +339,473 | 1,425,833 | 1,086,360 |
| Graph500 | 1 | 1,568,083 | 1,225,189 | +342,894 | 1,429,023 | 1,086,137 |
| Graph500 | 2 | 1,572,048 | 1,227,704 | +344,344 | 1,432,812 | 1,088,477 |
| Graph500 | 3 | 1,570,802 | 1,227,719 | +343,083 | 1,431,615 | 1,088,545 |
| Marian | 0 | 34,588 | 56,700 | -22,112 | 18,405 | 40,779 |
| Marian | 1 | 35,189 | 56,800 | -21,611 | 19,018 | 40,914 |
| Marian | 2 | 38,231 | 58,529 | -20,298 | 21,774 | 42,357 |
| Marian | 3 | 71,532 | 90,194 | -18,662 | 38,857 | 58,200 |

fallback 诊断：

| 负载 | 缺共享时钟的 load 宏数 | 其中预测 L1 miss | 碰撞标记的 L1 miss | future-parent load | 缺源 load |
| --- | --- | --- | --- | --- | --- |
| Zstd | 2,757,552 | 147,298 | 337 | 65 | 8,858 |
| XZ | 2,771,834 | 52,652 | 2 | 252 | 0 |
| FLAC | 4,818,141 | 30,008 | 2 | 36 | 0 |
| Graph500 | 12,446,494 | 5,772,572 | 1,055 | 13 | 0 |
| Marian | 1,849,699 | 58,486 | 0 | 28 | 1 |

缺共享时钟非常普遍，包括正常局部 hit；代码将其标为 estimated，并使用 canonical 近似时钟。它不是把所有这类 load 都变成 miss，不能直接把 1,244 万次缺时钟当 Graph500 的错计数量。

碰撞标记的 L1 miss 总数很小，但表容量是直接映射：一次覆盖可能使后续 follower 没有区间，后续漏计未必仍带 collision 标记。因此这张表不能给碰撞误差设严格上界。必须观察 overwritten line 和后续消费才能定量排除。

现有报告的 `llc_fill_audit_requested=false`，没有保存本批完整模型逐 load 区间与原生服务配对。本报告完成计数和路径层归因；尚未完成每个模型误判到具体时序边的混淆矩阵。所有“区间偏长/偏短、哪类服务错归”的推断均保持在候选机制层。

## 7. 优化顺序：先修可独立观察的成员判定，保留冻结时间模型

1. **从冻结行为派生隔离候选，先增加观察证据。** 对齐 `(core, retired macro sequence, functional fragment)`，记录模型 own mask、interval ID/代次、begin/end、是否 forwarded/alias、最终 miss mask；native 只用于独立审计。
2. **优先处理 Graph500 和 XZ core 0 的 fill 成员判定。** 分别检查多计和少计；区分数据可见边与请求响应/资源释放边，明确局部服务的统一模型时间基点。每次只改一项，保持固定容量与有界处理，不替换整个时长模型。
3. **对 forwarding 引入有界存活条件，独立验收。** 结构容量窗口若增加服务失效/排空近似，应使用已有两阶段框架可提供的模型事实；不能回放 gem5 的 SQ 或 load 发送时刻。
4. **分别审计 Zstd 下层退休服务归属与 Marian 整体 LLC 请求差。** 拆开 own demand、prepare/store 接管、权限服务与数据服务、软件预取、split/retry 的宏归并；保留退休 prepare。
5. **每个候选做同输入计数与 CPI 回归后再继续。** 观察器本身不反馈 target timing，所以改善 miss 计数未必改变 CPI；forwarding/cache 路径变更可能同时影响 CPI，应单独验收。CPI 若进入报告，必须同时给逐负载绝对误差和等权 MAE；吞吐需另行同输入配对验证。

不得把全套 service 时间重算、容量与延迟 Tag 发布一起集成后才验收；不恢复已失败的 R4 实验路线。保留现有并行两阶段框架，不引入逐周期 pipeline、whole-window 反复收敛或 gem5 时序输入。

## 8. 来源、复核与第二种账目分解

- 原始退休误差报告：[including-unknown 报告](fastsim-r01-retired-cache-miss-errors-including-unknown-20261111.md)；计数/unknown 区间来自对应 JSON。
- 本批维护验证器证据位于 `tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/<case>/validation.json`。`current_retired_cache_evidence` 提供原生逐事件退休参考，`current_task_pmu_evidence` 提供已守恒的 native 请求诊断。
- 当前预测位于 `tmp/restore-r01-main-20261111/replay/<case>.json`；每个 prediction/native validation 的 SHA-256 及 20 个 `perf-retirement-core*.json` 的指纹均复核。
- 共享解释器：[spr_retired_load_miss.hh](gem5-spr/src/mem/spr_retired_load_miss.hh#L45-L130)；模型 [wrapper](include/fastsim/spr_retired_load_miss.hpp) 引用同一源文件。
- 模型 fill 表：[retired_cache_miss_estimator.hpp](include/fastsim/retired_cache_miss_estimator.hpp#L20-L63)。
- 调用及时间来源：[simulator.cpp](src/simulator.cpp#L24133-L24223)。
- forwarding 选择及淘汰：[simulator.cpp](src/simulator.cpp#L11171-L11208)、[store 入窗口](src/simulator.cpp#L11393-L11418)。
- 模型宏 OR、certainty、资格及 merged 计数：[perf_retirement.hpp](include/fastsim/perf_retirement.hpp#L106-L170)。
- native certainty 与 reason：[perf_retirement.hh](gem5-spr/src/cpu/o3/probe/perf_retirement.hh#L513-L531)。
- 本报告结构化证据：[attribution JSON](fastsim-r01-retired-cache-miss-attribution-20261111.json)，含逐核拆分、native 路径、来源哈希及两种全量恒等式。

令模型全量预测的 certainty 分类为 `P_exact + P_estimated = F`，另有：

\[
F-N=(P_{exact}-E)+(P_{estimated}-R).
\]

| 负载 | 层级 | 模型 exact−native exact | 模型 estimated−native estimated | 总差 |
| --- | --- | --- | --- | --- |
| Zstd | L1 | +99,256 | +242,203 | +341,459 |
| Zstd | L2 | +20,881 | +12,678 | +33,559 |
| Zstd | L3 | +19,808 | +12,584 | +32,392 |
| XZ | L1 | -32,488 | -68,487 | -100,975 |
| XZ | L2 | +365 | +38 | +403 |
| XZ | L3 | +365 | +2 | +367 |
| FLAC | L1 | -2,123 | -6,890 | -9,013 |
| FLAC | L2 | +76 | +27 | +103 |
| FLAC | L3 | +35 | +6 | +41 |
| Graph500 | L1 | -266,005 | +1,635,799 | +1,369,794 |
| Graph500 | L2 | -218 | +247 | +29 |
| Graph500 | L3 | -225 | +232 | +7 |
| Marian | L1 | -37,095 | -45,588 | -82,683 |
| Marian | L2 | +489 | +69 | +558 |
| Marian | L3 | +365 | -12 | +353 |

与第 3 节一样，这只是 certainty 分桶的完整差分。Graph500 模型自身大量事件因缺共享时钟落在 estimated，而 native 可以归 exact；所以这一分桶中 exact 的负差不能另行叫作“真实 cold miss 漏报”，也不能把它与第 3 节当成两份互不重叠的错误贡献。
