# 测量边界内存状态跨负载试验

日期：2026-09-08。本文在
[测量边界内存状态第一阶段](measurement-boundary-memory-state-phase1-20260908.md)
的 TeaLeaf LLC32 C4 结果之后，按用户要求并行测试显式
`fastsim-binary-warmup-state-slice` 路径。生产 manifest、默认开关和模型代码均未改变。

## 1. 口径与执行方式

从冻结的 `case-inventory.json` 选择四个 C4 case：两个不同负载
ASTCENC/Stockfish、一个既有负误差控制 TeaLeaf L1D64，以及 formal Graph500。
四个 gem5 采集任务并行启动，输出目录隔离。每个可用 case 执行同一流程：

1. 用冻结 case 的原 gem5 命令和 checkpoint 补采 250k user-UOP/核的 committed
   `mem_events`；
2. 只保留全局 WORKBEGIN 到各核第一条 measurement record 之间的 committed 数据访问；
3. 当前 FastSim 二进制分别用原 manifest 和生成的 state manifest 重放完整 ROI；
4. CPI 误差使用 inventory 为该 case 指定的指标和 gem5 参考值；
5. 顺序重复默认/状态两侧，核对总周期、逐核周期、PMU 和退休人口。

三个 DSE case 的 gem5 binary、基础配置脚本和 checkpoint provenance 均通过。每个 case
的默认/状态两侧 user UOP、records、retired UOP 和 retired instructions 相同；顺序重复的
总周期、逐核周期和 PMU 也逐项相同。当前成对 baseline 与历史归档 baseline 有
`0.000037875`--`0.000424450` 的指标差，本试验不把工作树版本差异归到状态路径，所有
状态增量均相对同一当前二进制的默认重放计算。

## 2. 有效结果

三个有效 case 的指标都是 cycles/user-UOP。`绝对误差改善` 为正表示更接近 gem5，负值
表示退化。

| case | gem5 参考 | 默认 | 状态路径 | signed error 默认 → 状态 | 绝对误差改善 | sum core cycles 差 |
|---|---:|---:|---:|---:|---:|---:|
| ASTCENC baseline C4 | 0.3788582561 | 0.3256970587 | 0.3256227087 | −14.0319% → −14.0516% | **−0.0196 pp** | −2,974 |
| Stockfish baseline C4 | 0.3353081416 | 0.3593694160 | 0.3588775410 | +7.1759% → +7.0292% | **+0.1467 pp** | −19,675 |
| TeaLeaf L1D64 C4 | 0.6138814000 | 0.5034378750 | 0.5033006500 | −17.9910% → −18.0134% | **−0.0224 pp** | −5,489 |

只有 Stockfish 改善；ASTCENC 和 TeaLeaf L1D64 都因 CPI 继续下降而扩大原有低估。
两个真正的其他 workload 平均绝对误差从 10.6039% 降到 10.5404%，表面改善
0.0635 pp，但内部是一正一负，不能作为一致收益。把 TeaLeaf L1D64 也计入，本轮三个
有效 case 的 MAPE 从 13.0663% 降到 13.0314%，只改善 0.0349 pp，且通过率仅 1/3。

再把上一阶段 TeaLeaf LLC32 正误差控制纳入，四个 case 的 MAPE 从 12.2119% 升到
12.2327%，整体**退化 0.0209 pp**。这个小样本参与过问题选择，不能替代正式矩阵；它
已经足以否决“边界状态本身具有稳定 CPI 精度收益”。

## 3. 状态与 PMU 变化

`line touches` 是回放次数；`unique private lines` 是按核去重后再求和，不能解释为全系统
物理地址并集。

| case | 边界访问 / unique private lines | 每核访问 | L1D miss 差 | L2 miss 差 | LLC miss / DRAM read 差 |
|---|---:|---:|---:|---:|---:|
| ASTCENC baseline C4 | 693 / 46 | 0 / 0 / 693 / 0 | −7 | −6 | −5 / −5 |
| Stockfish baseline C4 | 6,498 / 543 | 969 / 2,111 / 3,418 / 0 | −96 | −284 | −223 / −223 |
| TeaLeaf L1D64 C4 | 693 / 46 | 0 / 0 / 693 / 0 | −9 | −9 | −9 / −9 |

三组状态路径都减少了部分测量期 miss，但周期方向不由 miss 数单独决定。ASTCENC 和
TeaLeaf L1D64 的总周期下降反而扩大负误差；上一阶段 LLC32 减少 9 次 DRAM read 后总周期
增加 55,896，又扩大正误差。结果继续支持 shared-order/response 补偿误差判断，也说明
不能按当前误差符号选择性启用 sidecar。

## 4. Graph500 失败审计

formal Graph500 没有可比较结果。冻结 inventory 要求的 gem5 SHA256 为
`8ff9e8d3...39b05c6`，记录路径上的当前二进制为 `33555384...7fc9ac`，原采集工具因此
按 provenance guard 拒绝运行。输出目录内的一次性诊断副本保留了这项不一致，但当前
二进制在被终止前仍未到达 WORKBEGIN：四核各输出约 4.8M--5.2M 条 prefix record，低于
冻结 manifest 的 10.4M--12.6M warmup records。

该诊断在产生 32,362,150,042 bytes、48,350,912 行 JSONL 后由 SIGINT 终止。大型无效
trace 已清理，保留 `result.json`、collection provenance、wrapper 和 `run.log`。没有运行
state builder 或两侧 FastSim，Graph500 不进入 MAPE 和方向计数。重新测试需要找回精确
冻结 gem5 二进制，或重新冻结 gem5 reference、FST、checkpoint 和 manifest 的完整 case；
仅放宽 SHA guard 不满足可比性要求。

## 5. 决定

状态输入继续保留为显式 opt-in，维护 manifest 不启用。该机制能修复已取证的边界缺失
cache 状态，也能稳定改变后续 miss 人口，但当前没有稳定 CPI 收益：四个可比较控制中只有
Stockfish 改善，ASTCENC、TeaLeaf L1D64 和 TeaLeaf LLC32 均退化。

下一步仍是定位 TeaLeaf LLC32 目标请求移除后的首个跨核 shared-order/response 分歧，
建立请求 owner、DRAM calendar、response 和关键消费者的成对账本。不要扩大 formal40/
DSE54，也不要把本轮 2-case 或 3-case MAPE 的小幅下降用于推广。

## 6. 产物与复现

统一机器可读汇总位于
`tmp/measurement-boundary-state-multiload-20260908/summary.json`，每个 case 子目录保留
`collection/`、净化 state、默认/状态 stats、重复 stats 和 `result.json`。构建统一汇总：

```bash
python3 tmp/measurement-boundary-state-multiload-20260908/build_summary.py
```

单个有效 case 的采集和状态构建沿用第一阶段命令，只替换 `--case`、原 manifest 与独立
输出目录：

```bash
python3 tools/collect_tail_timing.py \
  --inventory tmp/architecture-evidence-20260907.hlrSNO/case-inventory.json \
  --case dse-baseline-c04-731.astcenc_r \
  --out tmp/measurement-boundary-state-multiload-20260908/dse-baseline-c04-731.astcenc_r/collection \
  --user-uops 250000 --timeout 900 \
  --committed-mem-events --execute

python3 tools/build_measurement_boundary_memory_state.py \
  --trace-dir tmp/measurement-boundary-state-multiload-20260908/dse-baseline-c04-731.astcenc_r/collection/trace \
  --run-log tmp/measurement-boundary-state-multiload-20260908/dse-baseline-c04-731.astcenc_r/collection/run.log \
  --manifest tmp/architecture-evidence-20260907.hlrSNO/inputs/dse-baseline-c04-731.astcenc_r/manifest.txt \
  --output-dir tmp/measurement-boundary-state-multiload-20260908/dse-baseline-c04-731.astcenc_r/state
```
