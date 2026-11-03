# Line-generation 组件闭包审计（2026-09-08）

## 目的与判定边界

本轮不把 CPI 升降作为组件判据。目标是在接入新的 load-only coordinator 前，用 gem5
真实 Sequencer admission/callback tick 回答三个问题：

1. 半开活跃区间 `[admission, callback)` 能否复现 gem5 的同线 coalesced 判定；
2. 有多少已观测事件可以在不接触 store、page walk、跨核同线或同时活跃异线 cache set
   的条件下组成闭合组件；
3. 四个 workload 是否具有相同的可接入人口和主要拒绝原因。

新增工具 `tools/audit_line_generation_components.py` 将 v7 native-response ledger 按
`(core, thread_id, inst_seq_num)` 回连到 Tao trace 的物理 data line。它在真实 gem5 tick 上
重建活跃请求，并按以下域建立传递闭包：同核同 line、同核 L1D set、同核 L2 set、全局
LLC set、跨核同物理 line。store/atomic、多 line UOP、DTLB/page-walk 依赖、同 set 异线、
跨核同线以及 follower 与 native oracle 不一致都会拒绝整个关联组件。

`observed_clean` 只表示**当前 native data-response ledger 内**的组件闭合。sideband 没有
记录 instruction-fetch 请求、观测窗前活跃 generation 和未提交/被 squash 的请求，因此
该人口仍是接入上界，不能直接启用模型，也不能换算成 CPI 收益。

## 四负载结果

四个 case 并行分析；所有进入分析的 890,517 个数据事件均成功回连物理 trace，其中
589,746 条（66.23%）通过观测范围内的严格闭包。82,292 条 native coalesced load 中
59,699 条（72.55%）保留在 clean 组件。逐事件主原因计数守恒，输入和 SHA256 记录在各
case 的结果 JSON 中；汇总位于
`tmp/cross-workload-native-v7-20260908/line-generation-component-audit-matrix.json`。

| case | 完整 native 区间事件 | observed-clean 事件 | clean 比例 | clean 组件比例 | load coalesced | clean coalesced | coalesced 保留率 |
|---|---:|---:|---:|---:|---:|---:|---:|
| TeaLeaf LLC32 | 91,315 | 18,430 | 20.18% | 38.67% | 10,693 | 2,604 | 24.35% |
| TeaLeaf L1D64 | 432,387 | 337,767 | 78.12% | 84.04% | 30,131 | 19,642 | 65.19% |
| ASTCENC | 180,585 | 121,791 | 67.44% | 65.97% | 20,919 | 17,986 | 85.98% |
| Stockfish | 186,230 | 111,758 | 60.01% | 56.12% | 20,549 | 19,467 | 94.73% |

在最终 clean 人口内，四个 case 的 inferred follower 与 gem5 native coalesced **逐事件完全
一致**：分别为 2,604、19,642、17,986 和 19,467，且 inferred-only/native-only 均为 0。
这验证了 callback 同 tick 先移除旧 generation、随后请求开启新 generation 的半开区间
合同，也验证了同线 follower 不应另发 hierarchy request。

但完整观测人口并非全部一致。TeaLeaf L1D64 有 5 条、ASTCENC 有 193 条、Stockfish 有
121 条 follower-oracle 分歧；TeaLeaf LLC32 为 0。工具已把这些分歧传播到整个组件并
fail closed。它们证明仅携带已提交 data request 不足以覆盖全部 Sequencer parent；需要
继续区分观测窗前、wrong-path/squashed 和请求类型状态，不能用两边总数接近来放行。

## 组件差异

- **TeaLeaf LLC32 不是首个安全接入目标。** 18,360 条 store 加上密集的同核 L1D-set
  异线重叠，使 clean 事件只剩 20.18%。这与已经发现的 SQ/post-commit 生命周期耦合一致。
- **TeaLeaf L1D64 是首个接入目标。** 它有最高的 clean 事件比例，且保留 19,642 条已由
  gem5 证明的同线 coalesced load，足以检验 visibility/response 模型。
- **ASTCENC 适合作为 DRAM 独立控制。** 虽然 67.44% 事件通过 load-only 闭包，已有证据
  显示其主要残差在路径已经匹配后的 DRAM 服务时长；line-generation 通过不代表该误差
  会消失。
- **Stockfish 继续作为前端/内核控制。** 60.01% 事件闭合且多数 coalesced 被保留，但所选
  residual 的主导见证仍是 frontend/kernel serialization，不能归给内存模型。
- 四个窗口没有 atomic。已观测同时活跃 LLC-set 异线冲突为 0；这只约束当前 100K 诊断
  窗口，不能证明完整 ROI 中 shared callback 状态可以省略。跨核同线冲突在 L1D64、
  ASTCENC、Stockfish 中均出现，仍须显式携带或拒绝。

## 决定与下一实验

后续 Simulator 影子准入和成对间隔分解已经完成，结果见
[line-generation 准入影子账本](line-generation-admission-shadow-20260908.md)。最终 issue
proposal 比旧 memory lower bound 更接近 native 总量，但 TeaLeaf L1D64 密集窗同时存在
191 条 FastSim-only 与 272 条 gem5-only follower；issue spacing 和 parent callback
lifetime 都能独立改变大量身份。因此本节的 native clean 闭包仍是语义上界，不能直接把
coordinator 接到 final issue 上。

首个 Simulator 实验路径收紧为 TeaLeaf L1D64 上的 `observed-clean` 普通 load 组件，并
保持默认关闭。接入前仍必须完成两项代码边界：

1. 从 core timing 中提取无副作用的 admission proposal，在 dependency、DTLB、Sequencer、
   MSHR、FU 和 resource calendar 都确定后再进入 coordinator；
2. 让唯一 response 同时驱动 completion、producer-ready、资源释放和 callback fill，并
   让 shared cache/directory/DRAM 状态支持延迟提交和事务回滚。

实验验收先看逐事件 owner、generation、response、consumer 和计数守恒。TeaLeaf LLC32、
ASTCENC、Stockfish 分别作为 store/DRAM/frontend 控制。只有语义门禁通过后才并行比较
四 case 的 CPI/P99 和 host throughput。

## 复现

每个 case 使用相同命令模板，四条命令可并行执行：

```bash
python3 tools/audit_line_generation_components.py \
  --case tealeaf-l1d64 \
  --trace-dir tmp/cross-workload-native-v7-20260908/tealeaf-l1d64/trace \
  --stats tmp/cross-workload-native-v7-20260908/tealeaf-l1d64/dense-pending-fill.json \
  --output tmp/cross-workload-native-v7-20260908/tealeaf-l1d64/line-generation-component-audit.json

python3 tests/test_line_generation_component_audit.py
```

其余 case 名称为 `tealeaf-llc32`、`astcenc` 和 `stockfish`。对应结果位于各自目录下的
`line-generation-component-audit.json`。
