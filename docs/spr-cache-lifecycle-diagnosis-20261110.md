# C4 关闭硬件预取后的缓存服务生命周期排查

本次排查使用 `tmp/spr-load-service-audit-20261109/` 中新增的 zstd、xz
同源采集和回放，仅增加离线关联分析。没有修改 FastSim/gem5 模型、开关或
原五个已接受 FST，没有产生新 FST。结果不能回填为原五负载窗口的精确归因。

## 已确认的执行和分类差异

1. **gem5 仍然开启 early store prepare。** 两个诊断来源各有 8 个硬件
   prefetch 开关为 false，4 个 `sprEarlyStorePrepare=true`。
   原五个正式来源也逐一核实为相同的 8 false / 4 true；其配置证据单独保留在
   `original-five-configuration-evidence.json`，没有把诊断抽样计数外推到原窗口。
   `gem5-spr/src/cpu/o3/lsq_unit.cc:1366` 的
   `issueStorePrepareRequests()` 可以在 store 尚未 canWB、甚至数据尚未就绪时
   发出 `STORE_PREPARE | PF_EXCLUSIVE` 请求。它属于 store 的数据/权限准备，
   通过 optional queue 运行，不受上述硬件预取器开关控制。

2. **FastSim 当前 prepare 近似没有重演相同的缓存生命周期。** 实际输出为
   `legacy_prepare_timing=true`、`legacy_prepare_contract=timing-only-single-cache-replay`，
   `buffer_tso_prepare_lifecycle=false`、`response_pending_fill=false`。
   `src/simulator.cpp` 的 prepare 时间计算受 store 的退休/可发送时间约束；
   它不等同 gem5 对将来才退休的 store 提前执行的 optional GETX。
   `preview_memory_event()` / `replay_memory_event()` 的普通路径调用
   `PrivateHierarchy::access()`，miss 时立即安装可命中的 tag 并更新 LRU；
   PMU shadow 的 pending interval 本身不控制该 tag 的可见性。
   进一步检查全部调用点，`retired_miss_estimators_[core].observe()` 仅在
   `account_perf_retirement()` 的 read 分支调用，排除了 instruction fetch、
   page walk，也没有将普通 store/prepare 的填充区间登记为 parent。这是
   **PMU follower 估计只覆盖读 parent 的输入缺口**；不表示整个 FastSim
   缓存系统完全没有 store 流量。

3. **gem5 存在等待后命中、而等待没有进入 SPR 分类的控制流。**
   `MESI_Three_Level-L0cache.sm` 中 `{PF_IE, SP_IE}+Load` 仅执行
   `z_stallAndWaitMandatoryQueue`，没有给该 load 记录 lookup/pending-fill。
   `SP_IE+Data_Exclusive` 写数据、关闭 TBE 并唤醒请求；随后
   `{S,E,M}+Load` 在完成回调前执行 `spr_observeDataHit`。因此该控制流可以
   把“等待 prepare 填充后才完成”的 load 投影为 mask=0 的普通 resident。
   已实现的 Sequencer alias admission 关联只在 `RequestStatus_Aliased` 时调用，
   不能自动覆盖独立进入 mandatory queue 后停等的 load。

4. **不是所有重试都丢失先前 miss。**
   `TaoTraceNativeAccessRegistry::noteHierarchyAttempt()` 传递旧 attempt 的
   sticky miss 位；公共 reducer 的 `lookup()` / `convert()` 保留已观察到的
   positive miss。问题在于没有观察到停等服务时，没有 positive 位可保留。
   现有 JSONL 没有每条 load 的 stall/reissue 全边记录，不能声称已验证所有
   实际请求的重试链。

需要分开看两种影响：native 将等待后命中改为 L1 pending-fill，只会把
该路径 mask=0 改为 mask=1，**本身不能消除 FastSim 的 L2/L3 多报**。
下层多报还涉及 FastSim 把 load 当成新的下层请求，而 gem5 由另一条
prepare/demand 服务完成填充，或两边的后续驻留状态不同。

以上第 3 点是可达的源码覆盖缺口；下述样本关联支持该解释，但 LLC 填充在等待
窗口内，并不单独证明每条 load 首次查看 L1 时的精确状态。已有日志没有逐次
L1 lookup/stall 边，因此不将相关样本全部宣称为已证实的错误标签。

## 同源抽样关联

继续使用原配对的完整 single-read、非 atomic、非 split、workload-task 宏/
cache-line 子集，line stride=16。重新通过原脚本的内容身份、capture binding、
source ordinal、owner 和 `.ffe` 宏边界检查；与旧报告逐项核对 source identity、
人口和 confusion matrix 完全一致。

下表分母是 **L3 false positives（多报条数）**，不是净误差，也不是原窗口
的误差贡献比例。填充时间始终在 native 自己的时钟域内比较。

| 原生服务与 LLC 历史 | zstd | xz |
|---|---:|---:|
| native 非 pending-fill；同核 prepare 填充发生在 load admission 到 response 内 | 526 | 645 |
| native 非 pending-fill；此前最后一次 LLC 事件为 prepare fill，等待内无新 LLC fill | 27 | 0 |
| native 非 pending-fill；此前最后一次为普通 demand fill，等待内无新 LLC fill | 465 | 8 |
| native 非 pending-fill；此前最后一次为 RFO fill，等待内无新 LLC fill | 0 | 1 |
| native L1 pending-fill；等待内有 prepare fill | 165 | 2 |
| native L1 pending-fill；等待内有普通 demand fill | 461 | 62 |
| native L1 pending-fill；等待内无新 LLC fill | 53 | 2 |
| **全部 L3 false positives** | **1,697** | **720** |

`role=6` 来自真实协议的 store prepare GETX/UPGRADE 消息，而非读取 FST 的
路径标签。对应 prepare 消息没有原始 store sequence 身份；同核、同 line
和时间关联不能进一步证明它来自哪条 store，尤其不能排除后来被 squash 的
store。角色 1 同样须区分退休、将来退休和被 squash 的 demand。

继续用 zstd 的全部 1,697 个已绑定 L3 FP anchor 关联原生 sequence 和 squash
区间：上述“等待期间无新 LLC fill，之前为普通 demand fill”的 515 条
（普通服务 465、pending 服务 50）中，503 条的 fill identity 不在保留的
功能成员集合中，且位于同核实际 squash 区间内（453 普通、50 pending）。
其余 12 条为 10 条身份缺失、1 条来源未解析、1 条来自另一核的保留功能成员。
这证实存在退休流之外的投机 demand 填充；不能把这些填充输入 FastSim，
也不能将 503 作为原窗口的独立因果贡献。角色、sequence 与 squash 关联保存在
`c04-777.zstd_r-prior-demand-provenance.json`。

再检查等待窗口内的 461 条普通 demand 填充，459 条同样有未保留成员与实际
squash 区间证据（450 个更老 sequence、9 个更年轻 sequence）；另外 2 条为
更年轻的保留功能成员。因此 zstd 的全部 L3 FP 可以按**关联到的原生填充
来源**分组：

| 填充来源关联，非反事实贡献 | zstd L3 FP 条数 |
|---|---:|
| store prepare：等待内 691、此前已完成 30 | 721 |
| 被 squash 的普通 demand：等待内 459、此前已完成 503 | 962 |
| 其他/缺身份：10 缺身份、1 未解析、1 他核保留成员、2 更年轻保留成员 | 14 |
| 合计 | 1,697 |

这个分组覆盖了同源抽样中的全部多报，但没有完整模型 cache-set 历史或
受控消融，不能宣称这两个机制各自贡献了多少原始 PMU 净误差，或关闭它们
后误差会下降同样比例。prepare 自身也可能来自后来被 squash 的 store；
由于角色 6 没有 store identity，表中不再对它推断 squash 来源。

## 定点实例

- zstd core 3、ordinal 571792、line 532368768：native admission
  28972712204975，prepare LLC fill 28972712266190，response
  28972712271580；native mask=0，FastSim mask=7。FST 中同 line 的 store
  位于后面 34 个记录处。
- xz core 2、ordinal 2617916、line 426063280：native admission
  119958393194255，prepare LLC fill 119958393255470，response
  119958393260860；native mask=0，FastSim mask=7。同 line store 在后面
  13 个记录处。

检查的前 10 个 zstd 和前 10 个 xz L3 分歧样本，在各自前后 256 个 FST
记录范围内，均有该 load 之后的同 line store；这只是局部退休顺序事实，
不是 prepare 原始 store 的唯一身份认定。完整记录见
`tmp/spr-cache-lifecycle-diagnosis-20261110/nearby-functional-members.json`。

## 驻留、填充与替换顺序的边界

实际配置两边均使用 LRU，不能归因于 LRU/PLRU 名称选错。gem5 在 miss 时
可先分配 tag 并重置替换元数据，但 TBE/permission 阻止数据被作为 resident
命中；真实数据写入及 callback 后还有 MRU 更新。FastSim 普通 access 的
安装、可见性和 LRU 更新合并在 canonical replay 访问处。二者具有不同的
状态更新边，而不是简单“gem5 一律在完成时才分配 tag”。

此外，gem5 LRU 以 tick 为 touch 时间，同 tick 保留候选顺序；FastSim 用
严格 age 次序。这个 tie 行为差异已经在源码中确认，但没有同 set 的完整
逐事件 witness，不能给它分配当前 PMU 误差贡献。

## 小范围机制验证

仅编译并运行 `tmp/spr-cache-lifecycle-diagnosis-20261110/mechanism_witness.cpp`，
直接链接当前 `src/cache.cpp` 并包含当前公共 SPR reducer：

- 已观察 miss 后再观察 resident，最终 mask=1；
- 未观察等待、仅观察 resident，最终 mask=0；
- 已观察 pending-fill 后再观察 resident，最终 mask=1；
- 同一 child issue/response 下，没有登记 parent interval 时 shadow 给 mask=7，
  登记匹配 parent 后给 mask=1；这验证了输入覆盖缺口的影响机制，没有改生产路径；
- 两路 LRU 例子里立即 access 和延迟 complete_fill 可以分别选中 victim 0/1。

断言全部通过，结果在 `mechanism-witness.json`。最后一个例子证明 FastSim
两个 API 的语义差异，不冒充 gem5 实际 workload 的替换轨迹。没有大规模重跑
验证，也没有产生新的 CPI/PMU 精度或吞吐成绩。

## 证据与后续范围

本次证据目录：`tmp/spr-cache-lifecycle-diagnosis-20261110/`。
全体同源配对扩展结果为 `c04-*-lifecycle.json`；复现脚本为
`pair_lifecycle.py`，配置和运行计数在 `configuration-and-runtime-evidence.json`。

优先后续是补齐 observer 对 load 在 prepare 期间停等的服务绑定，区分数据
填充和 resident 权限准备，保持模拟时序不变；模型侧再用退休流可获得的
store 地址/依赖事实评估有界 prepare 近似。不得向 FastSim 输入 gem5 的
prepare 时间或 wrong-path 请求。原始数据保留，reference 修订后应另建报告，
不能将参考计数变化报告成 FastSim 精度改善。
