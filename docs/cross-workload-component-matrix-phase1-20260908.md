# 跨负载组件矩阵第一阶段：owner 链、层级语义与原生响应时间账本

日期：2026-09-08。本文接续[成对事件账本与建模优化方案](paired-event-model-plan-20260908.md)，
用于回答“正误差和负误差由哪些组件造成”。结论按请求身份、阶段边、状态语义和 owner
链建立，不按候选令 CPI 上升或下降来归因。

## 当前结论

第一阶段没有得到一个可全局调节的延迟项。四个 case 已显示至少三种不同机制：

1. TeaLeaf L1D64 的主要新线索是 pending line 可见性。密集窗内有 421 条单 line 请求在
   FastSim 被当成本地 private-cache 命中，而 gem5 是 Sequencer coalesced follower；其
   FastSim−gem5 本地尾部均值为 −104.30 cycles，364/421 条为负。
2. ASTCENC 的代表性 kernel load `seq=288822` 在两侧都走到 DRAM，路径语义相同；gem5
   issue→commit 为 1005 cycles，FastSim issue→retire 为 247 cycles。因此这条欠估来自
   服务、排队、response 可见性或 response→commit 暴露，不能靠路径分类修复。
3. Stockfish 的所选负向阶段由 user/kernel 转换、instruction fetch 和 serialize carry
   主导。`seq=1440950` 的 gem5 fetch→issue 为 875 cycles、FastSim 为 245 cycles；最大
   相邻 active progress 差异发生在 kernel serialization。它与 TeaLeaf 的 coalescing
   缺口不是同一组件。

TeaLeaf LLC32 仍保留为正误差控制：重复 user store `PC=0x4098b0` 的 FastSim
fetch→issue 为 2216/3562/4908 cycles，而 gem5 为 69 cycles，直接 gate 是 SQ-capacity
dispatch admission。这个见证说明正误差中存在队列/准入过度串行化，不说明所有正误差都
来自 store。

所以当前实施顺序是：先补齐 gem5 成功准入和实际响应时间，再按 line transaction owner
原子替换 FastSim 的 ordering、admission、visibility 和 response 职责；Stockfish 的前端/
内核序列化另建账本。生产延迟常数、默认开关和维护 manifest 均未改变。

## 四 case 稀疏矩阵

共配对 41,839 个相同 FST ordinal。`issue gate` 是 FastSim 内部直接约束，不是 gem5
误差归因；百分比只描述样本人口。

| case | 全 ROI signed residual | 配对数 | none | register producer | dispatch admission | StoreSet | sequencer/serialize |
|---|---:|---:|---:|---:|---:|---:|---:|
| TeaLeaf LLC32 C4 | +9.6487% | 4,033 | 48.20% | 35.33% | 16.09% | 0 | 0.37% |
| Stockfish C4 | +7.1759% | 17,785 | 47.39% | 28.12% | 18.50% | 2.33% | 3.66% |
| ASTCENC C4 | −14.0319% | 16,000 | 68.56% | 16.55% | 13.80% | 0 | 1.09% |
| TeaLeaf L1D64 C4 | −17.9910% | 4,021 | 47.23% | 37.53% | 15.12% | 0 | 0.12% |

同一 TeaLeaf 在相反误差符号下拥有近似的 `none/register/dispatch` 人口，证明 gate 频率
不能解释符号翻转。StoreSet 只在 Stockfish 样本中达到 2.33%，也不能作为全局首因。
过去宽泛的 `dependency` critical ledger 大多是传播链；新增直接 producer identity 后，
可以沿寄存器或 StoreSet owner 回溯到 memory response、ROB/LQ/SQ admission 或前端根。

## 四个密集窗口

| case/window | 配对数 | 单请求层级配对 | 语义不匹配 | 当前可证机制 |
|---|---:|---:|---:|---|
| TeaLeaf LLC32 core0 268087–270500 | 2,414 | 545 | 61 | SQ admission 过晚；另有 60 条 local↔coalesced |
| Stockfish core1 1440944–1442048 | 1,104 | 221 | 26 | 前端/内核 serialization 阶段欠估 |
| ASTCENC core2 286388–293100 | 6,713 | 1,308 | 141 | 已匹配 DRAM 仍明显过短；另有少量假 DRAM |
| TeaLeaf L1D64 core1 1428000–1441000 | 13,001 | 2,385 | 421 | 421 条 local↔coalesced；匹配 DRAM 也偏短 |

层级比较只接受恰好一个 FastSim data event 且 gem5 `line_requests=1` 的 UOP，排除 split/
多描述符歧义。主要人口如下：

| case | FastSim path ↔ gem5 outcome | 数量 | 本地尾部均值 | 负/正样本 |
|---|---|---:|---:|---:|
| TeaLeaf LLC32 | local ↔ L1D hit | 442 | +52.57 | 110 / 317 |
| TeaLeaf LLC32 | local ↔ coalesced | 60 | +49.48 | 22 / 38 |
| TeaLeaf LLC32 | memory ↔ Ruby memory read | 29 | +142.69 | 11 / 18 |
| ASTCENC | local ↔ L1D hit | 1,107 | +72.44 | 294 / 796 |
| ASTCENC | memory ↔ Ruby memory read | 47 | +28.60 | 6 / 41 |
| TeaLeaf L1D64 | local ↔ L1D hit | 1,856 | −58.44 | 1,323 / 516 |
| TeaLeaf L1D64 | local ↔ coalesced | 421 | −104.30 | 364 / 55 |
| TeaLeaf L1D64 | memory ↔ Ruby memory read | 108 | −124.44 | 100 / 7 |

这组结果同时出现“FastSim memory、gem5 L1D hit”的过度延迟和“双方都是 DRAM、FastSim
仍过短”的欠缺延迟。全局增加或减少 cache/DRAM 常数会修一侧、破坏另一侧。

## 已实现的账本

FastSim 的 `response_frontier_audit` 新增以下有界字段：

- checkpoint interval 的额外周期与 winning cause；
- 最后决定 issue 的 gate kind、ready cycle 和 extra cycles；
- 寄存器/StoreSet producer 的 sequence、dependency slot、跨 checkpoint 标志和 producer
  completion cause。

状态只保存在当前 ROB/audit scratch 中，不参与调度，也没有新增全流 feedback traversal。
四个 case 的审计模式与 control 的 cycles、retired UOP、memory access 和 CPI 完全一致；
41,839 条样本满足 `issue_gate_extra = actual_issue - base_issue`，producer sequence 也与
FST dependency distance 一致。

外部 gem5 补丁
[`p7-external-native-timing-ledger.patch`](../patches/p7-external-native-timing-ledger.patch)
新增 `taotrace-native-response-v7`：

- 在 `Sequencer::makeRequest()` 成功进入 request table（含合法 alias）时记录 first/last
  admission tick；buffer-full 和 locked-line retry 不会被误记为准入；
- 在 `Sequencer::hitCallback()` 记录 last response tick；split Request 合并使用
  first=min、last=max；
- Request extension 与 `(ContextID, InstSeqNum)` 有界 registry 同时保存时间，跨测量边界
  的 in-flight 请求可恢复完整生命周期；
- v7 JSONL 输出三个 tick，metadata 明确来源。v6 仍可读取，但分析结果明确标记无 tick。

FastSim 配对输出会把 v7 tick 归一化为 issue→first admission、first admission→last
response、last response→commit 三段。四个密集窗口已经用明确指定的诊断 gem5 二进制
并行重采；`collection.json` 同时记录原参考 SHA 与诊断 SHA，并明确
`same_gem5_binary=false`，所以本次数据只用于逐事件语义取证，不替换冻结 CPI reference。

## v7 分段结果与首个实现范围

四窗口共获得 4,459 条严格单 data event、单 line request 的层级比较，其中 4,427 条有
完整 admission/response tick。其余 Stockfish 3 条、ASTCENC 32 条都是显式
`terminal_no_ruby`，不是普通 Ruby 请求丢失 tick。所有 timestamped load 都满足
`issue→commit = issue→admission + admission→response + response→commit`；四窗口没有
atomic 样本。store 的 response 均发生在 commit 后，因此 load 与 store 必须分账。
timestamped load 的成功 issue→admission 均为 1 cycle；该 hook 位于最终成功准入点，不能
观察此前 buffer-full/locked-line retry，所以这个结果不等于“没有准入排队”。

最干净的两类 load 证据如下：

| case / 语义 | 数量 | FastSim latency−gem5 response | FastSim tail−gem5 tail | 非服务余项 |
|---|---:|---:|---:|---:|
| TeaLeaf L1D64 local↔coalesced | 421 | −109.1 | −104.3 | +4.8 |
| TeaLeaf L1D64 memory↔DRAM | 81 | −169.1 | −170.4 | −1.3 |
| TeaLeaf LLC32 local↔coalesced | 60 | −124.6 | +49.5 | +174.1 |
| ASTCENC local↔coalesced | 46 | −29.9 | +44.9 | +74.8 |
| Stockfish local↔coalesced | 11 | −0.2 | +125.2 | +125.4 |

TeaLeaf L1D64 的同线 follower 等待与 tail 缺口方向、大小一致，支持先实现 load-only
generation ledger。相同 coalescing 服务差在另外三个窗口被前序依赖、response 后传播和
有序退休抵消，说明该局部语义修复不能被当作各 workload 的 CPI 补偿量。

DRAM 也不能统一加常数。严格匹配的 load 中，TeaLeaf LLC32、Stockfish 的 FastSim 服务
分别比 gem5 慢 36.6、55.9 cycles；ASTCENC 43 条的均值为 −4.2，但中位数是 FastSim 慢
62 cycles，另有 3 条 gem5 993–1007-cycle 长尾；只有 TeaLeaf L1D64 的 81 条形成稳定的
约 −169-cycle 缺口。DRAM 动态服务/排队因此作为第二个独立模型研究。

现有 `ruby.sequencer_line_coalescing` 不是本次候选。它把 load/store 合并并额外执行一次
response feedback；四个密集 FastSim 窗口的 cycles/user-UOP 分别上升 8.0%、2.4%、
17.3%、34.7%，已经把局部等待扩散成全窗口惩罚。新实现从默认关闭、load-only、每核
有界的 line-generation ledger 起步，不追加全流 UOP feedback traversal；先闭合唯一
leader、follower 共用 response、callback 边界和容量守恒，再接入 cache visibility。

## 第一实现切片

当前已经落地四个互不改变 simulator 行为的基础组件：

- `LineGenerationLedger` 显式维护每核活跃 generation、leader、read/write visibility、
  admission、callback 和 retry。活跃 map 受容量约束，expiry heap 不超过容量的两倍；
  future admission 只返回 deferred，不预占槽位；状态可复制用于 proposal/rollback。
- `SetAssociativeCache` 与 `PrivateHierarchy` 新增分离的 probe/completion-fill API。probe
  miss 只记录真实 demand lookup，不安装 tag 或提前选择 victim；completion-fill 使用
  callback 当时的 replacement state，继续维护 dirty victim、L1/L2 inclusion 和事务回滚。
- `LineGenerationCoordinator` 用有界 ready/event heap 按真实 admission/callback 时间推进。
  同 tick 先处理 callback；每个 generation 只调用一次 service allocator；follower 继承
  leader 的 generation 与 response，不重复 probe、shared request 或 callback。store/atomic、
  早于 callback 的 split visibility 和非单调时间均显式拒绝。
- `LoadComponentPreflight` 在任何 cache/shared mutation 前，对 `(core, private component)`、
  全局 resource component、物理同线和跨 checkpoint active generation 建立传递闭包。
  任一关联事件含 store/atomic、IFetch、page walk/seed、functional-carried、跨核同线、
  私有 set 异线或非单调 admission，整个关联组件 fail closed，并按唯一主原因守恒计数。

定向测试覆盖同线 follower、精确 callback 边界、未来请求与容量竞争、stale expiry/
heap compaction、读数据与写权限分离、probe 前不可见、callback fill、intervening touch、
dirty victim、事务恢复和跨域组件闭包。真实 `PrivateHierarchy` harness 还验证两条 leader
加一条 follower 只产生两次 demand probe，callback 前 tag 不可见，coordinator/cache/
counters 联合回滚后的事件顺序和计数完全一致。原有同步 `access/access_l2` 仍走等价路径，
完整 C++ 测试通过。

这四个组件目前没有接入 `Simulator`，也没有配置开关，因此不会改变生产或实验 CPI。
这一步只建立原子接入所需的状态合同，不能报告为精度收益。代码审查确认现有
`response_pending_fill` 修补点已经晚于旧 cache/shared 状态更新，现有
`replay_sequencer_functional_epoch` 又会执行 seed feedback、排序、canonical replay 和
第二次 feedback；二者都不能直接复用。下一切片要先把 core timing 拆成无副作用的
`prepare-admission` 和由唯一 response 驱动的 `consume-response`，再给 shared cache/
directory/DRAM 增加可延迟提交、可按事务恢复的状态。只有这两项闭合后，才能把当前
coordinator 接到默认关闭的实验路径；否则会再次形成只改 response 的 overlay。

完整 v7 矩阵位于
`tmp/cross-workload-native-v7-20260908/dense-window-owner-native-timing-matrix.json`，按
load/store 拆分的明细位于同目录 `v7-dense-split-analysis.json`。旧 v6 层级矩阵仍保留，
但不再作为 response 时间证据。

## 模型替换门禁

下一候选必须按不同机制分别通过：

1. TeaLeaf L1D64：同 line leader/follower 共享一个 generation；follower 不得在 leader
   response 前作为 local hit 可见，response 后统一 release。
2. ASTCENC：已匹配 `ruby_memory_read` 的 admission→response 分布和 response→commit
   暴露必须复现，不能通过改 path class 获得表面收益。
3. TeaLeaf LLC32：不得扩大 `PC=0x4098b0` 的 SQ admission 过度串行化，且要保留已有
   边界状态语义修复。
4. Stockfish：前端/内核 serialize 账本单独通过后再组合，不能让 memory 候选吸收这段
   residual。

组合后才检查四 case 的 signed residual、未参与选择窗口和 host throughput。CPI 只用于
最后推广门禁，不用于给单个请求或组件贴因果标签。

## 后续更新：final-issue shadow 阻止直接接入

后续默认关闭的 Simulator shadow、成对身份交换和 cache guarded-commit 实现见
[line-generation 准入影子账本](line-generation-admission-shadow-20260908.md)。它修正了
本节“尚无配置入口”的历史状态，并进一步证明 final issue 与 callback lifetime 都会改变
follower 身份；coordinator 仍未接入生产 timing。

## 复现

```bash
cmake --build build -- -j16
./build/fastsim_tests
python3 tests/test_cross_workload_component_matrix.py
python3 tests/test_tail_timing_pair_scope.py
python3 tests/test_p1_native_response_sideband.py

python3 tools/analyze_cross_workload_component_matrix.py \
  --case tealeaf-llc32 9.6487 \
    tmp/cross-workload-component-matrix-20260908/tealeaf-llc32m-owner-audit.json \
    tmp/cross-workload-component-matrix-20260908/tealeaf-llc32m-owner-pairs.json \
  --case stockfish-c4 7.1758694157 \
    tmp/cross-workload-component-matrix-20260908/stockfish-owner-audit.json \
    tmp/cross-workload-component-matrix-20260908/stockfish-owner-pairs.json \
  --case astcenc-c4 -14.0319490224 \
    tmp/cross-workload-component-matrix-20260908/astcenc-owner-audit.json \
    tmp/cross-workload-component-matrix-20260908/astcenc-owner-pairs.json \
  --case tealeaf-l1d64 -17.9910199267 \
    tmp/cross-workload-component-matrix-20260908/tealeaf-l1d64k8-owner-audit.json \
    tmp/cross-workload-component-matrix-20260908/tealeaf-l1d64k8-owner-pairs.json \
  --output tmp/cross-workload-component-matrix-20260908/component-owner-matrix.json

patch --dry-run --batch --forward -p1 -d /data00/yinhaolang \
  -i /data00/yinhaolang/FastSim/patches/p7-external-native-timing-ledger.patch
```

旧 v6 完整密集层级矩阵位于
`tmp/cross-workload-component-matrix-20260908/dense-window-owner-native-matrix.json`；v7
分段矩阵见上文路径。
临时产物不进入版本控制；文档中的计数和哈希来源于当前冻结输入。
