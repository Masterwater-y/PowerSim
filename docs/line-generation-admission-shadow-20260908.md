# Line-generation 准入影子账本与成对间隔分解（2026-09-08）

## 本轮问题与边界

本轮回答两个接入前问题：FastSim 应该用哪个 core 时刻提交同线 generation，以及当前
FastSim 与 gem5 的 follower 数量接近时，逐事件身份是否也接近。结论不能从 CPI 升降或
follower 净差推出；issue 顺序与 parent callback 生命周期会共同改变活跃区间。

新增默认关闭的 `ruby.line_generation_admission_audit`。它只接受普通、单 memory-event
data load，在每轮 core timing 完成 dependency、DTLB、Sequencer、MSHR 与 resource
calendar 计算后，取得退休前的最终 UOP issue proposal。影子路径分别用旧的单调 memory
lower bound 和最终 proposal 重建有界半开区间 `[admission, response)`，记录 leader、
follower、容量、响应继承、issue gate 与分类交换。当前 response 是把该请求原有 FastSim
service duration 平移到 proposal issue 后得到的诊断值；它没有重求 cache/DRAM 服务，
因此不能作为生产 response。

影子状态每核只保留受 `ruby.sequencer_max_outstanding` 限制的活跃 generation，不保存全流
历史，也不新增一遍全 UOP feedback。人口与 admission 分类在运行时逐轮检查守恒；跨 epoch
出现时刻回退的 proposal 直接 fail closed。

## 全窗口影子结果

四个 case 使用当前二进制并行执行。开关开/关的 `cores`、`threads`、cache/CHA、pending
fill、去掉审计对象后的 totals 以及去掉 host throughput 后的 scope metrics 逐项相同；
所以这些运行没有 CPI 或功能 PMU 变化。等价检查位于
`tmp/line-generation-admission-shadow-20260908/semantic-equivalence-final.json`。

| case | 普通 load proposal | lower-bound follower | final-issue follower | lower-only / final-only | gem5 native coalesced | final 与 native 净差 |
|---|---:|---:|---:|---:|---:|---:|
| TeaLeaf LLC32 | 72,972 | 26,775 | 9,127 | 20,127 / 2,479 | 10,693 | −1,566（−14.65%） |
| TeaLeaf L1D64 | 374,836 | 67,538 | 13,480 | 58,281 / 4,223 | 30,131 | −16,651（−55.26%） |
| ASTCENC | 134,307 | 31,864 | 18,436 | 16,572 / 3,144 | 20,919 | −2,483（−11.87%） |
| Stockfish | 123,055 | 37,615 | 19,649 | 19,052 / 1,086 | 20,549 | −900（−4.38%） |

旧 memory lower bound 在四个 case 都过度合并，不能作为 admission。最终 issue proposal
消除了大部分假 follower，但也不能直接启用：TeaLeaf L1D64 的净缺口仍为 55.26%。两端
人口有极少差异，表中 native 数字只用于规模对照，不宣称逐事件配对。

最终 issue 相对 lower bound 的分类变化有明确 workload 差异：

- TeaLeaf LLC32 的 20,127 条 lower-only follower 中，16,736 条最终胜出 gate 是 register
  producer，2,782 条是 dispatch admission；
- TeaLeaf L1D64 的 58,281 条 lower-only 中，56,711 条是 register producer；
- ASTCENC 的 lower-only 分散在 none 9,315、register 3,296、dispatch 3,235 与 Sequencer
  633，final-only 又以 none 1,562、dispatch 1,217 为主；
- Stockfish 的 lower-only 以 Sequencer 10,481 为首，其次是 none 4,235、register 3,349；
  该负载另有 24,312 条 proposal 的胜出 gate 是 StoreSet。

这些 gate 是 FastSim 内部 proposal owner，不是 gem5 误差归因。它们证明不同负载的 issue
时刻由不同组件组合形成，也修正了第一版影子输出只在 owner 采样开启时记录 gate、从而
全部误报 `none` 的审计错误。修正只记录枚举，不启用 owner 明细或改变时序。

四个 case 的 proposal 容量拒绝均为 0，最大活跃 generation 为 7/7/9/8；当前差异不能
归因于 16-entry Sequencer 容量。非单调 proposal 分别为 13/4/151/122，数量虽小，但证明
正式 coordinator 必须跨 epoch 延迟/排序，不能静默丢弃。

## 成对请求：净数量会掩盖身份交换

新增 `tools/analyze_paired_line_generation_gaps.py`，把当前默认路径的 FastSim 密集
response-frontier audit 与 gem5 v7 admission/callback ledger 按已验证 record identity
配对。每侧独立按同核同物理 line 重建半开 generation；native 推断与 gem5 coalesced
不一致的事件单列，不能进入间隔/生命周期归因。

| case | 严格成对 load | 两侧 follower | FastSim-only | gem5-only | 两侧都不是 | native oracle 分歧 |
|---|---:|---:|---:|---:|---:|---:|
| TeaLeaf LLC32 | 435 | 33 | 41 | 27 | 334 | 0 |
| TeaLeaf L1D64 | 2,168 | 149 | 191 | 272 | 1,556 | 2 |
| ASTCENC | 718 | 33 | 105 | 22 | 558 | 14 |
| Stockfish | 98 | 6 | 5 | 5 | 82 | 1 |

TeaLeaf L1D64 的 follower 净缺口是 81 条，但逐事件有 463 条方向相反的错分。Stockfish
两端 follower 总数都为 11，身份却只有 6 条相同。这直接否定“总数接近即可接入”，也说明
单看 CPI 或 coalesced 总量会把互相抵消的 issue/response 错误藏起来。

对 parent 可观测且 native oracle 一致的 TeaLeaf L1D64 gem5-only 事件做局部双轴替换：

- 165 条只把 FastSim parent→child issue 间隔换成 gem5 间隔就会成为 follower；
- 92 条只把 FastSim parent 生命周期换成 gem5 callback 生命周期就会成为 follower；
- 13 条任一替换都足够。

反方向的 191 条 FastSim-only 中，190 条只需换 issue 间隔就会不再合并，1 条由 parent
生命周期解释。gem5-only 组 FastSim 间隔中位数为 9 cycles，而 gem5 中位数为 0；parent
生命周期均值则为 FastSim 62.9、gem5 140.4 cycles。两条轴都真实存在，不能只延长
response 或只提前 issue。

TeaLeaf L1D64 可分解错分事件的 child gate 中，gem5-only 有 238/270 条、FastSim-only 有
140/191 条由 register producer 胜出；但同线 generation parent 在这 461 条中没有一次是
child 的功能 producer 或 FastSim winning issue owner。因此 register 链改变了两请求的
相对到达间隔，却不是 line-generation parent 本身。TeaLeaf LLC32、ASTCENC、Stockfish
也出现双向身份交换，主要 gate 组合分别包含 register/dispatch/Sequencer、
register/dispatch/serialize 与 register/dispatch，不能用 TeaLeaf L1D64 专用规则修正。

完整分解位于
`tmp/line-generation-admission-shadow-20260908/paired-gap-decomposition-baseline.json`。
该分析是固定已提交人口上的局部反事实，不重算依赖图、cache 路径或 retire，不能加总为
CPI 贡献。

## 延迟 cache 可见性边界

`SetAssociativeCache` 与 `PrivateHierarchy` 新增无副作用 `prepare_lookup/prepare_probe`
和带 guard 的 `commit_probe`：proposal 保存 set/tag、命中状态与每 set mutation
generation；同 set 在其间发生任何已提交命中、fill、invalidate 或 dirty 更新都会让旧
proposal 失效。拒绝的 commit 不改变 demand counters、LRU/PLRU 或 dirty state。

cache transaction 同时快照并恢复 mutation generation。定向测试证明 prepare hit 不触碰
LRU、intervening fill 会拒绝旧 proposal、拒绝路径计数原子，以及事务回滚后原状态上的
proposal 可以提交。private hierarchy 在修改 L1 前同时验证所需的 L1/L2 token，避免只
提交半条 lookup。同步 `probe/access/complete_fill` 行为保持不变。

该 API 只完成 private/LLC tag 阶段的延迟提交基础。`SharedSystem` 的 directory、CHA 与
DRAM calendar 仍会在服务选择时改变状态；coordinator 也尚未接入 Simulator，因此本轮
没有精度收益。

## 当前决定与下一步

暂不把 final-issue shadow 直接变成 timing candidate。下一定位点收紧到跨负载的
parent→child issue-spacing owner：先对 TeaLeaf L1D64 的双向错分分别追溯 register
producer completion、dispatch/ROB 入场和同周期 issue width，并用 LLC32、ASTCENC、
Stockfish 的不同 gate 组合作控制。目标是解释为何 gem5 同周期准入的请求在 FastSim 被
拉开，以及为何另一些请求在 FastSim 同周期到达而 gem5 分开。

代码侧下一边界是给 `SharedSystem` 增加无副作用 service proposal 与带 generation 的原子
commit，并把唯一 response 同时交给 completion、producer-ready、资源释放和 callback
fill。只有 issue-spacing 与 service/callback 两轴都能逐事件重建、非单调跨 epoch proposal
不再 fail closed、clean component preflight 通过后，才接默认关闭的 load-only coordinator。
随后先验身份/守恒与路径分布，再验 retire 暴露，最后才比较 CPI/P99 与吞吐。

## 复现

```bash
cmake --build build -- -j16
./build/fastsim_tests
python3 tests/test_paired_line_generation_gaps.py

python3 tools/analyze_paired_line_generation_gaps.py \
  --case tealeaf-l1d64 \
    tmp/line-generation-admission-shadow-20260908/dense-tealeaf-l1d64-baseline.json \
    tmp/cross-workload-native-v7-20260908/tealeaf-l1d64/dense-pending-fill-pairs.json \
  --clock-period-ticks 333 \
  --output tmp/line-generation-admission-shadow-20260908/paired-gap-decomposition-l1d64.json
```

四负载完整命令及 config 位于
`tmp/line-generation-admission-shadow-20260908/`；正式仓库不收录这些运行产物。
