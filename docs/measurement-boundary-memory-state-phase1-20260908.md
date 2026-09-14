# 测量边界内存状态第一阶段

日期：2026-09-08。本文落实
[成对事件账本与建模优化方案](paired-event-model-plan-20260908.md) 中 LLC32
core 2 `223952` 的路径取证和第一版功能状态修复。参考仍是 gem5 FS；生产 manifest、
默认配置和既有模型开关均未改变。

## 1. 根因已经闭合

目标是 TeaLeaf LLC32 C4 的 kernel load：gem5 `inst_seq_num=318799`、PC
`0xffffffff81b3a2d1`、物理 line `0xbcd19a00`，对应 FastSim core 2 source
ordinal `223952`。

有界 Ruby debug 和 committed `mem_events` 给出同一条状态历史：

- 全局 WORKBEGIN 为 tick `21784639425795`；
- core 2 较早的同 PC load `inst_seq_num=314951` 在 tick `21784640765454`
  发请求，走 L0/L1/L2/Directory/DRAM，commit event 为 `21784640831388`，将
  `0xbcd19a00` 装入该核私有 cache；
- core 2 第一条被现有功能 trace 计入 measurement 的 record 直到 commit tick
  `21784643344206` 才出现。上述 load 位于这个 3,918,411-tick 空洞内，因此不在 FST；
- 目标请求在 tick `21784643769780` 到达 Ruby，实际发出 `ReadReq`，L0D 执行
  `E->E`，Sequencer 在 1 CPU cycle 后完成。它不是 store forwarding、coalescing、
  prefetch 或 wrong-path 命中。

所以 FastSim 的 460-cycle DRAM 不是该请求排队变差，而是缺失边界 cache 状态造成的
错误路径。原 DRAM blocker `core2:223847` 和 root `core1:272713` 只属于这条虚假
DRAM 路径，不能再作为 gem5 目标请求的服务根因。

## 2. 第一版实现

新增显式 manifest 类型 `fastsim-binary-warmup-state-slice`。每个活跃 stream 必须
提供 `fastsim-boundary-memory-state-v1` 文件；数据行只有：

```text
<sequence> <physical-address> <size> <R|W>
```

reader 要求 sequence 从 0 连续、拒绝额外列，并把每核事件数限制在 1,048,576。
模拟器在 warmup 统计清零后、measurement record 放行前，按当前 FastSim cache 和
coherence 状态回放这些访问。回放不推进 target time，不占用 queue/DRAM calendar，
不增加退休 UOP 或测量 PMU；stats 单独报告启用状态、访问数和覆盖 line 数。

采集工具 `tools/collect_tail_timing.py --committed-mem-events` 在一次性本地 wrapper
副本上打开 TaoTrace 已有的 committed memory stream。净化工具
`tools/build_measurement_boundary_memory_state.py` 只保留 WORKBEGIN 到该核第一条
measurement label 之间的 commit rows，丢弃 tick、path、latency、MESI、sharer、
queue 和 coherence oracle 字段，再生成 state manifest。本次找到 core 2 共 693 次
可缓存 DRAM 访问，并排除 6 次容量外 MMIO；core 0/1/3 均为 0。目标行是净化前
core 2 的第 529 次事件。

## 3. 验证结果

gem5 参考 cycles/user-UOP 为 `0.7427406257`。三次 FastSim 使用同一 FST、配置、
40,000,004 user UOP 和 6,272,951 个测量请求：

| 输入 | boundary accesses | cycles/user-UOP | signed error | sum core cycles |
|---|---:|---:|---:|---:|
| 原 baseline | 0 | 0.8144051186 | +9.6487% | 32,576,208 |
| 只种目标行 | 1 | 0.8159344184 | +9.8546% | 32,637,380 |
| 完整边界空洞 | 693 | 0.8158025184 | +9.8368% | 32,632,104 |

完整状态使 L1D/L2/LLC miss 和 DRAM read 各减少 9 次。目标 `223952` 的
issue 保持 `548060`，response 从 `548520` 变为 `548062`，retire 从 `548520`
变为 `548064`；path 从 DRAM 变为模型自身判定的 L1。该 10,001-record 审计窗口
末端 retire 从 `561533` 提前到 `559904`，局部方向正确。

完整 ROI 却比 baseline 多 55,896 core-cycles，signed error 恶化 **0.1881 pp**。
减少一条或九条错误 DRAM 请求会改变后续共享请求顺序和 DRAM 日历；当前模型中的
其他误差原先补偿了这部分正误差。单次 wall time 下降不作为吞吐结论。

按核分解后，core 2 减少 22,061 cycles，而 core 0/1/3 分别增加
12,501/30,450/35,006 cycles。只有 core 2 的 cache miss 人口变化，其他核的退化来自
共享服务交错；这把下一取证范围收敛到跨核 shared-order/response，而不是目标 load 的
私有依赖链。

另用四份 header-only sidecar 运行零事件控制。除宿主 wall-time、worker wait 次数和
新增三项 sidecar audit 字段外，`scope_metrics`、`totals`、`pending_fill`、threads、
cores、CHA、instruction CHA 和 causal target state 与 baseline 相同。
当前二进制再走原 `fastsim-binary-warmup-slice` 默认 manifest 也得到相同 target state；
新增 stats 字段为 `enabled=false, accesses=0, lines=0`。

## 4. 决定与下一步

机制和目标路径验证通过，完整 CPI 精度门禁失败。实现保留为显式 opt-in，生产 manifest
不启用，也不扩大到 formal40/DSE54。

后续按用户要求完成的有限并行跨负载 pilot 见
[跨负载试验报告](measurement-boundary-memory-state-multiload-20260908.md)：Stockfish
小幅改善，ASTCENC 和 TeaLeaf L1D64 小幅退化；连同本节 LLC32 后四 case MAPE 也退化。
Graph500 因冻结 gem5 二进制缺失而失败关闭。该结果不改变本节的推广决定。

下一步不能继续按地址补命中，或把 456 个局部退休周期当成全局收益。应把边界状态作为
owner 账本的入口状态，并原子核对请求移除后的 shared order、DRAM command state、
response 和关键消费者。具体重新推广条件是：

1. 解释 target 请求移除后新增 55,896 cycles 的首个 shared-order/response 分歧；
2. 同一实现同时通过 LLC32 正误差控制和 L1D64 负尾部，且不依赖 hit/path oracle；
3. 在线只保留活跃事务和有界入口状态，不增加完整逐 UOP feedback 遍历；
4. 通过未参与选择的窗口后，才运行完整矩阵和独立吞吐门禁。

## 5. 复现

原始一次性产物位于 `tmp/paired-event-path-20260908-boundary-mem/`，净化状态和三组
FastSim 结果位于 `tmp/measurement-boundary-state-20260908/`。

```bash
python3 tools/collect_tail_timing.py \
  --inventory tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json \
  --case dse-llc32m-c04-811.tealeaf_s \
  --out tmp/paired-event-path-20260908-boundary-mem \
  --user-uops 250000 --timeout 600 \
  --committed-mem-events --execute

python3 tools/build_measurement_boundary_memory_state.py \
  --trace-dir tmp/paired-event-path-20260908-boundary-mem/trace \
  --run-log tmp/paired-event-path-20260908-boundary-mem/run.log \
  --manifest tmp/architecture-evidence-20260907.hlrSNO/inputs/dse-llc32m-c04-811.tealeaf_s/manifest.txt \
  --output-dir tmp/measurement-boundary-state-20260908/full-gap

cmake --build build -- -j16
./build/fastsim_tests
python3 tests/test_measurement_boundary_memory_state.py
```
