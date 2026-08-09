# FastSim 微架构泛化排错日志

本文是可复现的工程日志，而不是只保留最终结论。每次排错必须记录：输入数据、代码/
配置、命令、输出目录、关键计数、假设、结论、被否决方案和下一检查点。正式精度合同固定
`Q=sim.interval_max_cycles=1024`；除专门的历史敏感性实验外，不用改变 Q 选择模型。

## 1. 稳定基线和数据合同

- functional trace：每 workload/seed/core-count 一份，只含退休功能流和 syscall/ROI
  sidecar，不含 timing/cache oracle；同一 trace 用于所有 uarch。
- CPI/PMU label：gem5 SE O3+Ruby，functional trace 与 label 的退休 UOP 差必须在每核
  32 UOP 的 ROI 边界容差内。
- 正式 PMU：L1D miss、private-L2 miss、LLC miss（diagnostic）、branch miss、CHA LLC
  lookup。IQ/ROB/DTLB 等保留为机制诊断。
- 当前生产 FastSim 配置：`configs/gem5-v28_1-time-epoch.cfg`。
- 主评估：`tools/evaluate_uarch_generalization.py`。

## 2. 已完成的历史路径

### 2.1 首批 12 workload × 16 uarch

输出：`tmp/uarch-c4-first-batch/`。

- 192 个 label/replay，CPI mean/P90/P99 为 2.504%/5.871%/15.672%。
- speedup P90=0.887%，有效 CPI 排序=90.719%（303/334）。
- 结论：相对 uarch 变化基本可用，但业务集没有跨越部分容量阈值；P99 不能通过。

### 2.2 阈值激励套件

输出：`tmp/uarch-c4-excitation-first-batch/`。

- ROB/IQ/DTLB/L1D/private-L2 阈值负载；48 cases。
- 生产路径 CPI mean/P90/P99=1.918%/4.303%/6.388%，排序=93.75%，最低吞吐
  11.067M UOP/s，总体 PASS。
- 结论：FastSim 能响应明确阈值，但该结果不能替代业务负载泛化。

### 2.3 Q 与 response boundary

- Q=256/512/1024/2048/4096/8192/16384 的历史敏感性证明现有模型会随 epoch 边界
  改变 IQ96 方向，因此 Q 是当前精度合同的一部分；从 2026-08-05 起固定 Q=1024。
- `interval_causal_timing` 在少数 IQ 点修复方向，但完整阈值集严重过修。
- `interval_corrected_suffix_carry` 在阈值集把有效方向/排序提高到 100%，但最低吞吐仅
  3.746M UOP/s，未进入生产配置。
- 结论：不能用选 Q 或全前缀重复扫描修补 response closure；需要增量 checkpoint/undo。

## 3. 业务激励首轮（2026-08-05）

输入/输出：

- matrix：`configs/business-excitation-c4.json`；
- trace/label/replay：`tmp/business-excitation-c4/`；
- report：`tmp/business-excitation-c4/evaluation/generalization-report.{json,md}`；
- 12 trace、48 gem5 label、48 FastSim replay，关联错误=0；每核 5.094M--7.742M UOP。

正式结果：CPI mean/P90/P99=16.730%/48.558%/51.988%，speedup P90=3.705%，
有效方向=77.778%，排序=87.097%（27/31），最低吞吐=10.160M UOP/s，总体 FAIL。
strict PMU WAPE：L1D=0.206%、private-L2=0.092%、branch=0.358%、CHA=0.092%。

### 3.1 已排除假设

| 假设 | 证据 | 结论 |
|---|---|---|
| ROI 太短/启动效应 | 每核至少 5.094M UOP；同一 workload 四核稳定 | 排除 |
| trace 与 label 不同 | 48/48 link errors=0；binary SHA 校验 | 排除 |
| cache/branch 事件数错误导致全部 CPI 差 | fanout L1/L2/DRAM/branch 计数基本逐项一致 | 排除为主因 |
| IntDiv/SIMD FU 参数未对齐 | gem5 config.ini 与 FastSim 均为 IntDiv latency=1、non-pipelined；SIMD=1 | 排除 |
| 只开 suffix carry 即可修复 | fanout baseline error 49.3%→47.2% | 否决 |
| 旧 causal timing 可直接生产 | fanout降到30.1%，dense基本不变；历史吞吐/完整集过修 | 否决 |

### 3.2 当前主假设：shared transient miss waiter 丢失

`gofeed_fanout_wide` baseline：

| 指标 | gem5 | FastSim |
|---|---:|---:|
| CPI | 2.12997 | 1.07993 |
| L1D misses | 524,303 | 524,278 |
| private-L2 misses | 486,194 | 485,705 |
| LLC demand/tag misses | 414,328 | 103,793 |
| DRAM reads | 103,820 | 103,793 |
| branch misses | 378,408 | 377,426 |
| IQ full events | 7,160,627 | 4,371,395 |

gem5 LLC demand miss/DRAM read=3.991，而 FastSim LLC tag miss/DRAM read=1.0。工作假设是
Ruby 把同一尚未填回 line 的 secondary request 记为 demand miss，但只产生一次 DRAM
fill；FastSim 在首个 miss 更新 tag 后把 secondary request 当成普通 LLC hit，没有继承
fill completion。两者总周期差=29,136,264，secondary miss 差=310,535，折合
93.83 cycles/request，与 shared LLC/DRAM response 路径量级一致。

这仍是需要代码/新增统计验证的强假设，不应直接用 93.83 作为拟合延迟。

### 3.3 当前主假设：response→OoO closure 不完整

生产配置为：`response_queue_feedback=true`，但 `response_rob_lsq_feedback=false`、
`response_sparse_resource_repair=false`、`interval_corrected_suffix_carry=false`。
fanout 有 485,710 escape memory events、21,592 horizon violations、398,375 跨 epoch
dependency edges、5,232,826 ROB crossings，但 response latency samples 和全部
response-critical attribution 为 0。gem5 IQ full 比 FastSim 高约 64%。

推论：cache/DRAM 事件数已生成，但 fill 返回没有完整控制 dependent ready、memory IQ
lifetime、ROB head、LQ/LSQ 和跨 Q epoch 未完成状态。

## 4. 可复现命令

生产 replay/evaluation：

```bash
/data00/yinhaolang/infer/.venv/bin/python tools/run_uarch_fastsim.py \
  --root tmp/business-excitation-c4 \
  --matrix configs/business-excitation-c4.json \
  --config configs/gem5-v28_1-time-epoch.cfg --fastsim build/fastsim --jobs 1 --force

/data00/yinhaolang/infer/.venv/bin/python tools/evaluate_uarch_generalization.py \
  --root tmp/business-excitation-c4
```

定向 response 消融：

```bash
/data00/yinhaolang/infer/.venv/bin/python tools/run_uarch_fastsim.py \
  --root tmp/business-excitation-c4 \
  --out tmp/business-excitation-c4/fastsim-suffix-diagnostic \
  --matrix configs/business-excitation-c4.json \
  --config configs/gem5-v28_1-time-epoch.cfg --fastsim build/fastsim --jobs 12 \
  --workload gofeed_fanout_wide --workload pytorch_dense_batch \
  --interval-corrected-suffix-carry

/data00/yinhaolang/infer/.venv/bin/python tools/run_uarch_fastsim.py \
  --root tmp/business-excitation-c4 \
  --out tmp/business-excitation-c4/fastsim-causal-diagnostic \
  --matrix configs/business-excitation-c4.json \
  --config configs/gem5-v28_1-time-epoch.cfg --fastsim build/fastsim --jobs 12 \
  --workload gofeed_fanout_wide --workload pytorch_dense_batch \
  --interval-causal-timing
```

fanout baseline absolute CPI error：production=49.3%、suffix=47.2%、causal=30.1%；
dense baseline 分别为20.5%、22.4%、20.4%。

## 5. 修复路线与 gate

1. 给 shared LLC 增加 transient line/MSHR waiter 观测：unique fill、merged secondary、
   waiter cycles、completion cycle；先验证主假设，不改变生产 timing。
2. secondary request 必须等待同一 fill；保留 unique DRAM read，不用常数延迟拟合。
3. fill completion 驱动 dependency ready、memory IQ、ROB/LQ/LSQ；未完成状态跨固定
   Q=1024 携带。
4. 使用增量 undo/checkpoint 控制吞吐，禁止整段重复扫描成为生产路径。
5. 定向 gate：fanout/dense 12 cases；然后原 48 cases；最后阈值 48 cases和原业务
   192 cases。任何阶段不得回退 strict PMU 或吞吐≥5M gate。

每个后续实验在本文件追加日期、代码差异、命令、输出目录、指标、结论和下一步。

## 6. 第一阶段修复：shared transient fill（2026-08-05）

### 6.1 代码语义

修复前，`SetAssociativeCache::access()` 在 LLC 首次 miss 发出 DRAM request 时立即把
tag 置为 valid；同一 fill 返回前的其他核请求会命中这个 tag，并以 LLC-hit latency
完成。现在 `SharedSystem` 维护仍在飞的 `line -> DRAM completion`：

- 首个 miss 分配一个 unique fill 和一个 LLC MSHR，并产生一次 DRAM read；
- fill 完成前到达的同线请求保留 LLC demand-miss 语义，但作为 secondary 合并，不再
  分配 DRAM request，response 等于 parent fill completion 加返回 NoC；
- 输出 `llc_unique_fills`、`llc_merged_misses`、`llc_merged_wait_cycles` 和最大 waiter；
- transaction undo 同时覆盖 transient map/expiry heap；FR-FCFS stable completion 会
  remap parent fill 及其 waiter，避免 canonical 与 repaired response 不一致；
- 已完成 fill 用完成时间小根堆淘汰，timing checkpoint 只复制当前 active state。

没有修改 Q、DRAM latency、memory exposure 或 workload-specific 参数。

### 6.2 单测和性能故障复盘

```bash
cmake --build build -j 16
build/fastsim_tests
```

全部测试通过。新增四核同线用例验证 `4 LLC misses = 1 unique fill + 3 merged misses`
且 DRAM read=1；不同线对照为 4 unique fills。原 corrected-arrival 同线用例从不稳定
fallback 变为两 pass 稳定，这是消除“过早 LLC hit”后的预期路径变化。

`build-asan/fastsim_tests` 在 `ASAN_OPTIONS=detect_leaks=0` 下也全部通过。当前执行环境
处于 ptrace 管理下，LeakSanitizer 自报“不支持 ptrace”并 fatal；这是工具环境限制，
不是测试发现的 leak，不能把该次 LSAN 启动失败记为代码 PASS，也没有据此关闭 ASan。

首版 `transient-v1` 把所有历史 line 永久保存在 map，`capture_timing_state()` 每个 epoch
复制全集，造成 O(epoch × 历史 unique line) 开销。12 路定向回归最低吞吐只有
0.154M UOP/s。该实现没有被保留；`transient-v2` 用 expiry heap 只保留 active fill，
同样 CPI/PMU 下双 baseline 隔离吞吐恢复为 10.842M/10.099M UOP/s，12-case 最低
9.151M。这个失败路径保留在这里，后续任何 checkpoint 状态都必须证明有界。

### 6.3 定向和完整回归

定向 replay：

```bash
/data00/yinhaolang/infer/.venv/bin/python tools/run_uarch_fastsim.py \
  --root tmp/business-excitation-c4 \
  --out tmp/business-excitation-c4/fastsim-transient-v2 \
  --matrix configs/business-excitation-c4.json \
  --config configs/gem5-v28_1-time-epoch.cfg --fastsim build/fastsim \
  --jobs 10 --workload gofeed_fanout_wide \
  --workload pytorch_dense_batch

/data00/yinhaolang/infer/.venv/bin/python tools/evaluate_uarch_generalization.py \
  --root tmp/business-excitation-c4 \
  --fastsim-root tmp/business-excitation-c4/fastsim-transient-v2 \
  --out tmp/business-excitation-c4/evaluation-transient-v2 \
  --workload gofeed_fanout_wide --workload pytorch_dense_batch
```

评估器新增 repeatable `--workload`，允许对不完整的定向 replay 目录生成正式同口径报告。
12-case variant CPI mean/P90/P99=33.136%/46.440%/47.565%，speedup P90=6.775%，
排序=78.947%（15/19），最低吞吐=9.151M。

完整业务 48-case 报告：
`tmp/business-excitation-c4/evaluation-transient-v2-full/`。

| 指标 | 修复前 | transient-v2 | 变化 |
|---|---:|---:|---:|
| variant CPI mean | 16.730% | 15.718% | -1.012 pp |
| variant CPI P90 | 48.558% | 43.442% | -5.116 pp |
| variant CPI P99 | 51.988% | 47.204% | -4.784 pp |
| speedup error P90 | 3.705% | 3.324% | -0.381 pp |
| material direction | 77.778% | 77.778% | 不变 |
| material ranking | 87.097% | 87.097% | 不变 |
| LLC miss diagnostic WAPE | 31.343% | 23.671% | -7.672 pp |
| 最低吞吐 | 10.160M | 6.637M | gate 仍 PASS |

fanout baseline 从 CPI=1.07993/误差49.3% 改善为1.18573/44.3%；识别出
63,651 secondary、7,145,581 waiter cycles，IQ-full 从4.371M升至5.102M。gem5 Ruby
miss 与 unique DRAM 的差仍约310K，所以 transient window 只解释了一部分差异，不能把
Ruby 计数差全部当作 distinct waiter，更不能按差值乘常数延迟。

阈值 48-case 报告：
`tmp/uarch-c4-excitation-first-batch/evaluation-transient-v2/`。结果仍 PASS：variant
CPI mean/P90/P99=1.921%/4.282%/6.403%，speedup P90=2.651%，direction=90%，
ranking=93.75%，最低吞吐=8.855M。说明本修复没有破坏既有 ROB/IQ/cache 阈值泛化。

### 6.4 第二缺口消融

所有单 case JSON 和说明保存在
`tmp/business-excitation-c4/diagnostics/transient-v2/`。主要结果：

| 路径 | fanout CPI/误差 | dense CPI/误差 | 结论 |
|---|---:|---:|---|
| production | 1.18573 / 44.33% | 1.08168 / 20.52% | 新基线 |
| sparse resource repair | 1.18885 / 44.18% | 1.08189 / 20.50% | 几乎无效，吞吐下降 |
| 旧 dense ROB/LSQ | 1.18588 / 44.32% | 1.08176 / 20.51% | 几乎无效 |
| causal timing | 1.52145 / 28.57% | 1.08563 / 20.23% | fanout方向对但低吞吐，dense无效 |
| suffix carry | 1.44123 / 32.34% | 1.05625 / 22.39% | fanout部分改善，dense回退 |
| causal + resource | 1.50958 / 29.13% | 1.08468 / 20.30% | 没有组合收益 |

`--cpi-attribution` 不改变 CPI。fanout 有1,512,540个 response samples，escape 平均
80.76 cycles，已暴露 critical memory/dependency 4.007M/8.536M cycles；dense 有
1,179,732 samples，escape 平均111.06 cycles，critical dependency 11.405M cycles。
因此此前 production JSON 中 attribution 为0只是 `cpi_attribution=false`，不能作为
“response 未执行”的证据；真正问题是 closure 数量级仍不足。

新的排错优先级：

1. 把 gem5 的 branch squash、frontend/I-cache stall 与 FastSim response/ROB residual
   分开归因。gem5 baseline 的 committed-stream I-cache stall 总计约1.86M（fanout）/
   1.46M（dense）cycles，不足以单独解释26.20M/8.44M总 cycle gap；
   `squashedInstsExamined` 分别约20.46M/22.50M，但它是被检查 ROB entry 数，不可直接
   当 cycle 加回，只用于提示 branch-resolution/ROB interaction 很强。
2. 为每个 response seed 记录从 lower-bound completion 到 actual completion、ordered
   retire、ROB admission 和 dependent issue 的逐阶段 residual 守恒；当前只看最终
   critical dependency 总数无法判断 residual 在何处被 slack 吸收或丢失。
3. 只对发生 ROB-head crossing 的局部 suffix 建立增量 undo/replay；禁止重新启用全 epoch
   causal/suffix 作为生产解，也不调整固定 Q=1024。

## 7. Response residual、单次 retime 与 Ruby fill-response（2026-08-05）

### 7.1 逐阶段 residual 账本

`ResponseResidualCounters` 现在按 core 记录 response seed、producer completion、依赖边、
ordered retire、dispatch 和 memory issue 的移动量，并分别检查：

```text
dependency input = dependency absorbed + dependency propagated
retire input     = retire absorbed     + retire propagated
```

新增 `test_response_residual_ledger_conservation`，覆盖跨 epoch 依赖。账本是 audit-only，
`cpi_attribution=false` 时不进入热路径，也不修改 CPI/PMU。真实 Q=1024 baseline 审计如下；
cycle ledger 在不同 edge/UOP 间允许重叠，不能把各行直接相加当成 CPI：

| 指标 | fanout | dense |
|---|---:|---:|
| response seed events | 523,545 | 588,678 |
| completion extended UOPs | 17,300,311 | 20,068,729 |
| dependency input cycles | 14,297,316,236 | 9,456,603,205 |
| dependency absorbed / propagated | 394,670,125 / 13,902,646,111 | 505,015,739 / 8,951,587,466 |
| retire input cycles | 5,193,463,479 | 3,691,934,138 |
| retire absorbed / propagated | 68,315,778 / 5,125,147,701 | 67,312,025 / 3,624,622,113 |
| corrected escape issues | 275,760 / 485,705 | 354,218 / 556,785 |

两个守恒式均为 true。最后一行证明 56.8%/63.6% 的 shared escape issue 在 core closure
中被移动，但 production 没有把这个时间反馈到 CHA/LLC-MSHR/DRAM。

### 7.2 B3 单次 response/shared-queue retime

新增实验开关 `sim.interval_response_retime`：固定 canonical cache/directory path，只把
corrected issue replay 一次；arrival order 不稳定、不可 replay 或 corrected issue 越过
当前 Q=1024 boundary 时，恢复该 epoch 的 feedback、shared timing 和 CHA queue PMU。
FR-FCFS 和 FCFS 都有路径，`test_response_timing_retime_transaction` 验证 PMU 不重复计数。
`tools/run_uarch_fastsim.py` 新增 `--interval-response-retime`。

首版只检查 arrival order，fanout 出现：

```text
unbounded shared latency: core=0 line=255401
issue=1961944491 completion=3306827945059
```

原因不是整数溢出，而是 corrected issue 已越过 epoch boundary，提交未来 shared-ready
state 后在后续 epoch 重复放大 gap。已有上界保护使运行直接失败，没有输出错误 JSON。
修复后，任何 shared issue 超过 `horizon_q16` 都整 epoch fallback；与 suffix carry 组合时，
越界请求由 entry-snapshot transaction 留在 resident suffix。

真实 trace 结果说明该路径不是生产解：retime-only 可稳定提交的 epoch 只有 fanout 143、
dense 11；绝大多数分别 7,888/7,985 个 epoch 因 boundary/order certificate fallback。
12-case 定向回归和精确当前基线的对比如下：

| 指标 | 当前 transient-v2 | response retime | 结论 |
|---|---:|---:|---|
| variant CPI mean / P90 / P99 | 33.136% / 46.440% / 47.565% | 33.112% / 46.392% / 47.624% | 无实质改善 |
| speedup error P90 | 6.775% | 6.983% | 略退化 |
| material ranking | 78.947% (15/19) | 78.947% (15/19) | 不变 |
| LLC diagnostic WAPE | 43.401% | 43.338% | 不变 |
| 最低并行回归吞吐 | 8.076M | 8.075M UOP/s | 不变 |

与 suffix carry 组合也没有增益：fanout CPI 1.44123493、dense 1.05625240 和 suffix-only
逐 bit 相同；retime 只增加额外 host work。因此配置保留但默认 `false`，不能把“实现了
shared replay”误报为“修好了 CPI”。报告和12-case输出保存在：

```text
tmp/business-excitation-c4/diagnostics/response-closure-b3/
  fastsim-current-directed{,-eval}/
  fastsim-b3-response-retime-directed{,-eval}/
```

### 7.3 resource-calendar no-go

`core.response_sparse_resource_repair=true` 的定向消融：

| workload | baseline CPI | resource CPI | 独立吞吐 | 结论 |
|---|---:|---:|---:|---|
| fanout | 1.18573 | 1.18885 | 5.68M | CPI +0.00312 |
| dense | 1.08168 | 1.08189 | 6.73M | CPI +0.00022 |

fanout 产生 17.33M issue moves，但 collision 只有 428,812 cycles；dense collision 仅
11,524 cycles。瓶颈不是 issue/writeback port calendar，默认继续 `false`。

### 7.4 Ruby fill-response service stage

gem5 `config.ini` 显式给出 directory/message admission、Garnet return path 和 L3
`l2_response_latency`，而 FastSim 原来在 `DramModel::completion` 后立即释放 LLC TBE/
MSHR。新增 `uncore.llc_fill_response_latency`，语义为：

- DRAM completion 后到 LLC tag/fill 可见及 TBE 释放的固定协议服务时间；
- parent 和全部 secondary response 一起移动；
- 不增加 DRAM request，不计入 DRAM queue PMU；
- FCFS、FR-FCFS fixed point、transient remap 和 timing replay 使用同一个 fill completion。

从 captured Ruby 配置得到统一值 8 cycles：response/message admission 1 + Garnet return 5
+ L3 `l2_response_latency` 2。不是按 workload 选择。6--48 cycle sweep 显示 CPI 随真实
fill service 增长，但 LLC Ruby miss 口径几乎不变：

| fill cycles | fanout CPI | dense CPI | fanout/dense unique fills |
|---:|---:|---:|---:|
| 0 | 1.18573 | 1.08168 | 103,793 / 235,073 |
| 8 | 1.20465 | 1.11135 | 103,793 / 235,073 |
| 18 | 1.23451 | 1.14779 | 103,793 / 235,073 |
| 36 | 1.27745 | 1.20845 | 103,793 / 235,073 |
| 48 | 1.31083 | 1.24659 | 103,793 / 235,073 |

gem5 DRAM reads 为 103,820/235,104，unique fill 已几乎逐个对齐。gem5 Ruby LLC demand
miss 与 DRAM read 的差（310,508/69,868）不是 distinct DRAM miss；它包含 Ruby
secondary/protocol tag-miss 口径，不能按差值乘一个 latency 常数加到 CPI。48 cycles 已把
dense 拉到 8.4% absolute error，fanout 仍约 38.5%，证明继续放大统一延迟会变成负载拟合。

8-cycle 12-case 回归：

| 指标 | 8-cycle 结果 | Gate/变化 |
|---|---:|---|
| variant CPI mean / P90 / P99 | 31.610% / 45.483% / 46.780% | mean -1.526 pp，仍 FAIL |
| speedup error P90 | 6.914% | PASS，较前 +0.139 pp |
| material direction / ranking | 60.000% / 78.947% | 不变，FAIL |
| L1D/L2/branch/CHA WAPE | 0.007% / 0.066% / 0.735% / 0.066% | 全 PASS |
| LLC miss diagnostic WAPE | 43.293% | diagnostic |
| 最低12路并行回归吞吐 | 8.210M UOP/s | PASS |

独立无 attribution 吞吐为 fanout 11.747M、dense 11.043M UOP/s。Q 输出仍为 1024。
该架构缺失项有效且成本很低，production 配置设为 8。完整输出：

```text
tmp/business-excitation-c4/diagnostics/response-closure-b3/
  fastsim-fill8-directed{,-eval}/
```

完整业务48-case随后完成，0 replay failure：variant CPI mean/P90/P99 从 transient-v2 的
15.718%/43.442%/47.204% 改善到 **14.370%/42.454%/46.364%**；speedup error P90
从3.324%改善到3.242%，material direction/ranking保持77.778%/87.097%，strict PMU
全部PASS，最低24路并发吞吐5.262M仍过5M gate。报告：

```text
tmp/business-excitation-c4/fastsim-fill8-full/
tmp/business-excitation-c4/evaluation-fill8-full/
```

阈值48-case用8路并发重放后总体 **PASS**：CPI mean/P90/P99=
2.262%/5.209%/8.439%，speedup P90=2.771%，material direction=90%，ranking=
93.750%（15/16），最低吞吐9.641M UOP/s。首次24路并发报告的4.780M是 host contention；
同一最慢 `baseline/W_uarch_rob160` 独立复跑为12.695M，随后用8路并发重建正式报告，
不是放宽吞吐 gate。报告：

```text
tmp/uarch-c4-excitation-first-batch/fastsim-fill8/
tmp/uarch-c4-excitation-first-batch/evaluation-fill8/
```

### 7.5 当前核心瓶颈与下一步

当前不是 DRAM 请求数、固定 Q、issue port 或单纯 ROB 容量未实现：

1. **fanout absolute CPI**：unique memory work 已对齐，但 response 对 dependency/IQ/ROB-head
   的局部 backpressure 仍被 checkpoint slack 吸收；需要以 ROB-head crossing 为锚点的
   增量 suffix closure，而不是全 epoch replay。
2. **dense ranking**：gem5 IQ32/96 在同一 workload 上分别 +4.84%/-1.79% speedup，FastSim
   方向相反；这是 workload 激励与 cache/ROB interaction 的非单调点，不能靠单调 IQ
   occupancy 常数修复。先加 seed/重复 ROI 验证标签稳定性，再修 release/dispatch interaction。
3. **LLC PMU 口径**：formal CHA lookup、private-L2 miss 和 DRAM read 已对齐；Ruby LLC
   `m_demand_misses` 保持 diagnostic，后续增加 secondary/protocol 分类报告，不把它伪装为
   unique memory traffic。

下一实现单元只 replay 第一个 response-extended ROB-head 到下一可提交 head 的局部区间，
共享状态仍沿用 canonical path；gate 顺序保持 fanout/dense 12 → 业务48 → 阈值48，Q 固定1024。

## 8. ROB-head-local suffix closure（2026-08-05）

### 8.1 实现与边界

新增实验开关 `sim.interval_rob_head_suffix_replay`，默认 `false`。它要求
`time_epoch`、`interval_reweave_passes=1` 和 `response_sparse_scoreboard=true`，并与旧的
whole-epoch response/causal retime 互斥。Q 仍固定为1024。

实现没有把“任意 completion 变晚”都当成 ROB 后压，而使用实际 capacity crossing：

```text
response-extended old ROB entry
  -> retire_cycle 挡住 younger dispatch
  -> 打开 per-core local suffix
  -> suffix 内 corrected shared issue 做 timing-only replay
  -> 下一 ROB head 在 lower-bound retire time 可提交时关闭
```

open bit 跨 Q checkpoint 保存。共享 cache/directory/transient-fill membership 仍以 canonical
functional path 为准；FR-FCFS/CHA/LLC-MSHR/DRAM 只在 corrected schedule 的事件顺序和到达
时间稳定时提交，否则恢复 first-pass timing/feedback/CHA queue。corrected issue 超过当前 Q
边界时不提交 future shared-ready state，而保留该事件的 canonical timing；open ROB suffix
继续带到下一 checkpoint。这避免了 B3 中未来 queue state 被重复复合的失控路径。

新增统计：anchor/recovery/suffix UOP/open checkpoint、candidate/noop/stable/fallback epoch、
moved/replayed shared event、boundary-clipped event 和 replay wall time。新增
`test_rob_head_local_suffix_checkpoint`，覆盖跨 Q carry 和 functional/cache PMU 守恒；全部
`fastsim_tests` 通过。

### 8.2 排错路径

第一版锚点使用 `completion_retire > base_retire`，结果 fanout 有约85% UOP、dense 几乎
全部 UOP 被标为 suffix；独立吞吐只有约5.3M UOP/s，已经不再是局部算法。修复为只有
“response-extended ROB predecessor 实际抬高 capacity dispatch”才打开 suffix。最终 baseline：

| 指标 | fanout | dense |
|---|---:|---:|
| anchor | 14,254 | 32,326 |
| suffix UOP | 7,580,657 | 19,001,875 |
| stable / fallback epoch | 3,788 / 2,652 | 4,934 / 3,304 |
| moved shared event | 90,882 | 306,240 |
| Q-boundary clipped event | 313,439 | 314,948 |

实现过程中还发现一次新增的确定性 bug：`rob_head_suffix_open` 最初用了 `vector<bool>`。
parallel feedback worker 虽然写不同 core，下标仍可能共享同一个压缩机器字，产生位级 data
race；同一 fanout 命令的 CPI 会在约0.08%范围内漂移。改为每核独立 `uint8_t` 后，两路并发
重复运行的 cycles、CPI、anchor、stable/fallback、queue cycles 全部逐值一致。生产 baseline
在该检查中本来就是一致的，因此这是新增路径自身的问题，不应归咎于宿主噪声。

### 8.3 12-case 结果与结论

定向矩阵为 baseline/core_width4/ROB96/ROB256/IQ32/IQ96 × fanout/dense，共12 cases、
8路并发。正式输出：

```text
tmp/business-excitation-c4/diagnostics/response-closure-b3/
  fastsim-rob-head-suffix-directed/
  fastsim-rob-head-suffix-directed-eval/
```

| 指标 | fill8 production | ROB-head suffix | 变化 |
|---|---:|---:|---:|
| variant CPI mean | 31.610% | 30.722% | -0.888 pp |
| variant CPI P99 | 46.780% | 46.103% | -0.677 pp |
| speedup error P90 | 6.914% | 8.341% | +1.427 pp |
| material direction | 60.000% | 60.000% | 不变 |
| ranking | 78.947% (15/19) | 78.947% (15/19) | 不变 |
| minimum concurrent throughput | 8.210M | 4.629M UOP/s | FAIL |

baseline fanout CPI 从1.204648提高到1.235333，absolute error 从43.443%降到42.002%；
dense 仅从1.111353提高到1.114834，absolute error 仍约18.08%。strict L1D/private-L2/
branch/CHA PMU WAPE 继续全部小于2%，证明 timing-only replay 没有污染 functional PMU。

结论：**局部 ROB crossing 是有效但次要的缺失项，不是当前主误差瓶颈**。它只能换取约
0.9 pp 的定向 mean CPI 改善，却使 speedup 误差变差、排序不变，并把并发吞吐降破5M。
因此停止业务48/阈值48扩展，production 配置保持 `false`；不能把该实验误报为修复完成。

下一步不再扩大 shared retime。优先拆分 committed-path 与 speculative-path：利用已有 branch
outcome/miss，在不生成 wrong-path trace 的前提下增加 branch-resolution 到 squash 之间的
ROB/IQ/frontend shadow occupancy，并先用 fanout/dense 的 `squashedInstsExamined`、IQ full 和
branch recovery 周期做 audit-only 守恒。只有该组件能同时改善 absolute CPI、IQ/ROB 参数
方向且保持 >5M UOP/s，才进入 timing 路径。

## 9. FST v6、rename/ROB 源级消融与 branch shadow 候选（2026-08-06）

### 9.1 Functional-only v6 输入和 response-aware free list

TaoTrace 现在按 gem5 `UnifiedRenameMap::canRename()` 的 destination operand 语义输出
`destination_class_counts=[Int,Float,Vec,CC]`。该字段来自 committed UOP 的
architectural register class，不含 fetch/issue/commit tick、cache hit 或 path oracle。
FST v6 把四类 count 打包进原 64-byte record，并用 header feature bit 声明；旧 v5 在
`core.rename_free_list` 或 `core.response_rename_feedback` 下 fail closed。

真实单核 `pytorch_dense_batch` functional-only smoke：

- 2,517,699 committed UOP；
- 2,223,065 个有 destination 的 UOP；
- Int/Float/Vec/CC operand 为 4,186,854 / 491,546 / 0 / 712,447；
- 每行 `n_dst == sum(destination_class_counts)`；
- FST header 为 version 6、feature flags 5；
- response free-list 结束时四类均 `allocated == released`、`live=0`。

C2 实现以事务副本维护每核 per-class live count、ordered-retire release queue 和 rename
width cursor；FR-FCFS/reweave 候选不会重复消耗寄存器。`core.rename_free_list` 与
`core.response_rename_feedback` 是互斥实验：前者只改 lower-bound，后者只在 C2 建模。

### 9.2 被源级消融推翻的主因假设

同一程序、同一 ROI、只改 gem5 microarchitecture：

| gem5 profile | UOP CPI | 相对 baseline |
|---|---:|---:|
| baseline ROB192 / IQ64 / phys256 | 1.65535 | — |
| physical registers 4096 | 1.60005 | -3.34% |
| ROB 4096 | 1.55908 | -5.82% |
| IQ/LQ/SQ 4096 | 1.68026 | +1.51% |
| ROB4096 + physical registers4096 | 0.78830 | -52.38% |

因此高 `fullRegistersEvents` 不是 14.1% absolute CPI gap 的单一主因；ROB、IQ 和 free-list
之间存在强瓶颈迁移，任何 count 都不能直接转换成 additive cycle。FastSim 原 response
free-list 能把 baseline 从 -13.3%推到约 +8.8%，但这是过校正，production 保持关闭。

实现排查还修正了一个确定的 source mismatch：gem5 的 ROB/IQ/LSQ free-entry 更新经
`commitToRenameDelay=1` 或 `iewToRenameDelay=1` 到 rename，再经
`renameToIEWDelay=2` 到 dispatch；FastSim原先把 release cycle 直接当 dispatch 可见。
新增 `core.commit_to_rename` / `core.iew_to_rename`，同时进入lower-bound与C2。

### 9.3 Branch shadow 的因果证据与当前边界

Tournament→TAGE-SC-L 64KiB 的 gem5反事实只改branch predictor：

| predictor | branch miss | squashed UOP | squashed load | UOP CPI |
|---|---:|---:|---:|---:|
| Tournament | 34,693 | 1,323,020 | 73,139 | 1.65535 |
| TAGE-SC-L 64KiB | 20,499 | 693,874 | 43,496 | 1.55874 |

减少14,194次miss使CPI下降0.09660，约17.1 cycles/miss；squashed UOP/load同步下降。
这证明wrong-path occupancy是剩余误差的因果组件，但wrong-path地址仍禁止进入输入。

新增默认关闭的 `branch.shadow_rob`：只用FastSim自己重放出的branch miss、该branch的
functional fetch-to-resolve跨度、目标fetch/decode/rename/commit width和当时ROB空槽，生成
匿名shadow ROB population；不生成wrong-path PC、地址、OpClass或cache访问。单核dense
smoke把 production误差从 -13.30%改善到 -8.28%，吞吐仍约6.54M UOP/s；但正式
92-case gate在35/92时停止：C4 `int_div_serial` 从0.108%误差爆到38.772%，C4 P99
达到32.327%。原因是每次miss生成接近满ROB的统一匿名深度，而真实错误路径深度取决于
不可由committed functional trace恢复的OpClass、IQ/FU占用和依赖。因此该路径已经硬否决，
`branch.shadow_rob=false`；未完成目录 `tmp/branch-shadow-full92-v1/` 不得作为完整报告。

### 9.4 Backward-edge 92-case gate：source字段正确，但叠加方式被否决

只启用 `core.iew_to_rename=1` 和 `core.commit_to_rename=1`，其余 production
开关不变，在 C4/C8/C16/C32 共 92 cases 上完成回归：

| Cores | baseline mean / P99 | backward-edge mean / P99 | min UOP/s |
|---:|---:|---:|---:|
| 4 | 2.830% / 6.256% | 4.529% / 9.427% | 6.290M |
| 8 | 1.649% / 5.461% | 3.535% / 11.963% | 6.815M |
| 16 | 1.588% / 8.917% | 2.340% / 7.460% | 4.719M |
| 32 | 3.670% / 11.057% | 2.474% / 11.364% | 4.506M |

该变化改善 C32 mean 和部分 memory-random case，却系统性抬高 C4/C8
business CPI；C8 P99 从 5.461% 退化到 11.963%，C16/C32 吞吐也跌破
5M gate。最明显的反例是 C8 `memory_seq_moderate`，absolute error 从
3.049% 变为 13.438%，而 C4/C8 多数 business workload 同时退化约
2--4 pp。`int_div_serial` 和 SIMD 基本不变，说明问题不是 FU latency。

结论不是 gem5 的 backward edge 不存在，而是当前 interval occupancy 已经把
ROB/IQ/LQ/SQ 保留到 retirement/issue/completion；再把 source time-buffer
delay加在释放周期和现有 `rename_to_dispatch` 前，会在这一级抽象中重复收费。
因此代码保留为 source-alignment ablation，但默认值和 production 均恢复为
0。正式证据位于：

```text
tmp/backedge-production-full92-v1/
tmp/current-se-profile-full92-q1024-v1/
```

该阶段原计划下一步只启用 `branch.shadow_rob`；9.3记录的35-case硬反例已经终止该计划，
不再扩展branch shadow，也不与free-list、response rename或backward edge组合。

### 9.5 Open-adaptive完整队列和单次预充电 gate

源级复核确认 gem5 的 page-policy 在达到 `maxAccessesPerRow` 后不会再次执行adaptive
判断；原FastSim还可能在同一请求上第二次调用 `auto_precharge()`，重复增加`tRP`。同时，
gem5 page-policy扫描完整controller queue，而FastSim selection window只是functional-only
到达相位不完备时的service-candidate证书，不能自动等同于page-policy可见范围。

实现新增两个相互独立、默认关闭的实验开关：

- `dram.frfcfs_full_queue_page_policy`：page-policy扫描完整reconstructed admitted queue；
- `dram.frfcfs_row_cap_single_precharge`：row cap已经预充电时跳过adaptive判断。

新增统计包括完整admitted queue峰值、扫描条目数、window外同-row hit/bank conflict、
row-cap/adaptive预充电次数。定向单测覆盖window外hit压过conflict、window外conflict触发
close、row cap只收一次`tRP`以及serial/parallel逐值一致；完整`fastsim_tests`和CTest均通过。

最终同一二进制、同一fill8配置的C16/C32分解矩阵如下；C4/C8 effective window为1，
走certified bypass，不执行FR-FCFS repair：

| case | legacy | single-precharge only | full queue + single-precharge |
|---|---:|---:|---:|
| C16 random | -7.259% | -7.259% | -7.051% |
| C16 sequential | -12.792% | -12.392% | -19.864% |
| C32 random | -9.463% | -9.463% | -10.012% |
| C32 sequential | +9.130% | +13.013% | +12.415% |

三组均有case低于5M UOP/s。single-precharge在C32 sequential明显过校正；完整队列在
C16 sequential产生7.47 pp退化，C32 random也退化。fill=0的补充矩阵却让sequential在
C16从-15.965%改善到-8.475%、C32从+11.785%改善到+8.451%，证明page-policy与
fill/response closure存在强非加性耦合。两个开关都保持false，不进入92-case。正式输出：

```text
tmp/open-adaptive-page-policy-memory8-final-v1/
tmp/open-adaptive-page-policy-memory8-ab-v1/
```

### 9.6 Gate身份和历史baseline口径修正

验证工具现在为每次运行生成 `run-manifest.json`，记录FastSim二进制、配置、关键源码、
验证工具、CMake cache的SHA-256及完整命令，默认禁止复用旧case输出。最终DRAM矩阵二进制
SHA-256为 `b66255520f57a1e47be989f3f658916b5e1a7a28be14a5c8a26dd2071face391`。

复核还发现：`tmp/current-se-profile-full92-q1024-v1/` 的旧92-case基线不含
`uncore.llc_fill_response_latency`，而当前production配置和
`tmp/backedge-production-full92-v1/` 使用fill latency 8；后者还同时启用了两个backward
edge。因此9.4表格支持“该组合失败”的结论，但不能继续称为严格的backward-edge单变量
证明。后续任何完整gate必须先用同一哈希二进制重建default-off baseline，再只覆盖一个开关。

下一步不再扩展page-policy selector。先在当前源码上重建可追溯的production baseline，
单独复核fill8的C4--C32 memory与92-case影响；然后将C32 random/sequential residual转向
controller arrival、response dependency slack和IQ/LQ/ROB ordered-retire事件级闭合。

### 9.7 当前default-off production候选的可追溯92-case gate

2026-08-06使用clean Release/native/IPO build重建当前default-off配置，直接运行
`fastsim_tests`并通过，随后从`build/`执行CTest，1/1通过。完整gate未使用
`--reuse-existing`，重新转换全部functional trace并运行C4/C8/C16/C32各23个workload。
本次身份为：

```text
FastSim SHA-256: b66255520f57a1e47be989f3f658916b5e1a7a28be14a5c8a26dd2071face391
config SHA-256:  23744808154b73dcda28eb34634e68b1f505d77364f7484bc7b89da82e8fa640
simulator SHA-256: 2c6a3ec72039c29187faaaa93203dbfb20fa8d53ba7e19a42eb6f3ad60f6016c
validator SHA-256: 9024d0c94b7faffc602bc74dd97028bdea99a8ff370dd7e6d41453431ebdd0cb
```

92/92 cases完成，四个core count均为23个workload；UOP、memory event、
private/escape partition以及response-critical守恒失败数全部为0。正式gate如下：

| Cores | mean / median / P99 / max CPI error | signed bias | min UOP/s | gate |
|---:|---:|---:|---:|:---:|
| 4 | 4.495% / 4.618% / 9.169% / 9.189% | +3.564% | 5.758M | PASS |
| 8 | 3.533% / 3.288% / 11.890% / 13.438% | +1.734% | 6.204M | FAIL CPI |
| 16 | 2.555% / 1.824% / 11.575% / 12.792% | +0.146% | 4.094M | FAIL CPI+throughput |
| 32 | 2.394% / 0.845% / 9.390% / 9.463% | -0.823% | 4.159M | FAIL throughput |

各core count最大CPI residual分别是C4 `mysql_heldout` +9.189%、C8
`memory_seq_moderate` -13.438%、C16 `memory_seq_moderate` -12.792%和
C32 `memory_random_mlp` -9.463%。C32 `memory_seq_moderate`仍为反方向的
+9.130%。除memory tail外，business/compute没有超过10%的爆炸case；C4 business
base/heldout mean为6.412%/6.346%，C8为4.197%/3.975%，C16为
1.952%/1.983%，C32为0.973%/1.499%。

完整23-workload PMU的count-weighted WAPE如下：

| Cores | L1D | private L2 / CHA | branch | DTLB access | DTLB miss | O3 IQ full | LLC functional path |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 0.043% | 0.239% / 0.239% | 0.165% | 8.770% | 0.027% | 45.474% | 0.929% |
| 8 | 0.048% | 0.266% / 0.266% | 0.170% | 8.799% | 0.033% | 45.721% | 1.044% |
| 16 | 0.052% | 0.283% / 0.283% | 0.160% | 8.805% | 0.034% | 46.068% | 1.080% |
| 32 | 0.059% | 0.290% / 0.290% | 0.153% | 8.843% | 0.034% | 45.618% | 1.128% |

因此当前default-off版本是新的、身份闭合的实验基线，但不是通过production gate的
版本。与9.6所述无fill身份的历史结果相比，当前结果在C32 CPI上改善，却使C4/C8/C16
CPI gate或吞吐退化；由于二进制/配置身份不同，该差异只用于定位fill/response耦合，
不能作为单变量因果结论。正式输出位于：

```text
tmp/default-off-production-full92-final-v1/
```

下一步应在同一哈希二进制上做`llc_fill_response_latency=0/8`的完整或定向匹配消融，
并把DRAM response到dependency wakeup、IQ/LQ/ROB release和ordered retirement逐段闭合；
在此之前不提升full-queue page policy、single-precharge、free-list、backward edge或
branch shadow。

### 9.8 FS C8 lbm 尾差根因审计索引

two-phase functional warmup 后，lbm 的 DRAM read/write 误差已经收敛到
-0.31%/-1.88%，但 UOP CPI 仍为 4.982523，对 gem5 2.572910 高估 93.653%。进一步审计
确认它是当前六个 FS workload 中唯一持续同时产生大量 DRAM read/write 的规则流式形状：
13.537/6.356 requests per kUOP、write/read 47.0%、private-L2 miss 到 DRAM 约 97%、
gem5 SQ 平均占用 97%、LSQ-full 约占 42.7% cycles。gem5 每 channel 平均积累 41.55 个
write，并约每 16.05 个一批 drain；FastSim 则没有独立 write queue，LLC dirty eviction
立即修改 DRAM calendar。

当前 C8 的 topology-scaled FR-FCFS effective window 为 1；lbm 的 2,793,826 个 memory
request 全部走 bypass，没有执行 FR-FCFS repair/open-adaptive queue scan。FastSim core 0
只有 12.29M UOP，却承担 360.5K L2 miss，达到 29.32 miss/kUOP；其 128.94M active cycles
中 126.17M 被归为 exposed memory penalty，成为错误 makespan 尾部。即使改用最慢 worker
的 97.13M cycles，CPI 仍高约 46%，所以这是 worker 普遍 response over-exposure 与 core 0
尾部放大的叠加，不是单核异常可以单独删除。

完整参数对齐边界、六 workload 对比、证据等级和下一步单变量 gate 记录在
`docs/gem5-source-aligned-p99-plan.md` 第24节。当前最可信方向是独立 read/write controller
queue、dirty-writeback service order 和 response→TSO/SQ→ordered-retire ledger；在严格
单变量证明前，不声明其中任一项已是最终根因，也不使用统一 latency/workload scalar 修复。

### 9.9 独立 DRAM write queue 单变量实现与 FS 六案复验

2026-08-06 按 9.8 的根因顺序实现 default-off `dram.separate_write_queue`。LLC dirty
victim 只进入每 channel 128-entry write buffer；demand read 保持优先，write queue 超过
85% 且已完成至少 16 个 read 后，下一次 read 前按 FR-FCFS drain 16 个 write。物理队列满
时使用 50% low watermark 做有界容量恢复。该开关没有修改 architectural store response、
SQ release 或 TSO 顺序，因此是 write-service-order 单变量。

完整 two-phase FS 六案输出：

```text
tmp/fs-write-queue-full6-v1/
```

`lbm` UOP CPI 从 4.982523 降至 2.605042，对 gem5 2.572910 的误差从 +93.653%
降至 +1.249%。其余无 write 的四案 bit-identical；`graph500` 仅从 -8.518% 变为
-8.607%。六案 absolute mean/P90/P99/max 从 23.590%/53.698%/89.658%/93.653%
降至 8.204%/13.663%/13.735%/13.743%。默认关闭的新版 `lbm` 对 `totals/cores/threads/cha`
与冻结基线完全一致，CPI 仍为 4.982523。

`lbm` measurement 区间 write ledger 为：enqueue 1,300,251、drain 1,299,456、final
pending 795，满足严格守恒；81,216 次 turnaround 全部为 16-write burst，无 queue-full
强制 drain。DRAM write 六案 WAPE 从 1.955% 改善到 1.299%。

该候选仍不提升为 production 默认：六案 CPI P99 13.735% 仍高于 10%，最低吞吐
3.683M UOP/s 仍低于 5M；同时 FastSim `lbm` write row-hit 为 75.970%，高于 gem5
48.1%，说明 full write-queue FR-FCFS 仍可能过度利用 functional lower-bound arrival。
下一步是先用新增 ledger 约束 write selector/turnaround，而不是继续调统一 latency；随后再
分别处理 omnetpp/tealeaf/zstd 的独立低估和吞吐热点。
