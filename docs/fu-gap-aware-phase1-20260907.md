# FU future-reservation 第一阶段报告

日期：2026-09-07

## 决定

第一优先级已经完成机制实现、微型差分和两个完整 ROI 控制。实现修复了旧 FU
尾时间戳不能回填空档的问题，但候选没有通过 CPI 精度门禁：TeaLeaf LLC32 C4
正误差控制恶化 **0.5758 个百分点**，超过预先设定的 0.5 pp 停止线；TeaLeaf
L1D64 C4 只改善 **0.0063 个百分点**。因此：

- 保留有界 capacity calendar、审计计数、配置入口和回归测试；
- `core.fu_gap_aware_schedule=false` 保持默认关闭；
- 不扩展到 Graph500、ASTCENC、Stockfish、formal40 或 DSE54；
- 不把审计发现的局部可提前周期相加后解释为 CPI 收益；
- 单次 wall time 只用于排除灾难性成本，不形成吞吐结论。

## 实现边界

旧调度器为每个 FU lane 保存一个尾时间戳。依赖未就绪的较老 UOP 可以先预约未来
时刻，使随后已经就绪的 UOP 看不到预约之前仍然空闲的槽位。候选为每个执行池维护
按周期计数的有界 capacity calendar，在满足既有全局 issue width、memory port、依赖
和 dispatch 下界后寻找最早可用周期。calendar 随单调 dispatch 下界丢弃历史周期，
其空间取决于未完成预约跨度，而不是 trace 长度。

`core.committed_pipeline_audit=true` 且候选关闭时，调度结果仍由旧实现决定；审计
calendar 只复现旧预约并计算同一时点是否存在更早的合法 FU 槽位。候选开启时，实际
issue 使用新 calendar。标量 core 不接受该选项，避免产生无意义配置。

涉及的入口为：

- `include/fastsim/config.hpp`、`src/config.cpp`：配置声明、解析和约束；
- `include/fastsim/interval_core.hpp`、`src/interval_core.cpp`：calendar 与调度路径；
- `include/fastsim/types.hpp`、`src/main.cpp`：审计累计与 JSON/CLI 输出；
- `configs/gem5-exp-fu-gap-aware.cfg`：显式实验 overlay；
- `tools/run_uarch_fastsim.py`：批量运行器透传；
- `tests/test_main.cpp`：微型因果差分、关闭态与审计中立性。

## 微型差分

9-UOP 精确测试先让依赖延迟的 UOP 预约整数 FU 的 cycle 30，再给出一个可在 cycle 5
执行的独立 UOP。旧模型把独立 UOP 也推到 cycle 30，最后退休为 cycle 55；gap-aware
候选把独立 UOP 放到 cycle 5，最后退休为 cycle 30。没有未来预约的控制图在开关两侧
完全相同。审计模式报告机会但不改变输出。

这证明代码修复了被审计的局部机制；它不证明真实 ROI 的退休关键路径会缩短。

## 完整 ROI 结果

两组都使用现有冻结 native user+kernel trace、4 核、Q=1024 和同一个新构建二进制。
参考 CPI 沿用固定 DSE inventory 的 `cycles_per_user_uop`。误差定义为
`(FastSim CPI / gem5 CPI - 1) * 100%`。

| Case | gem5 CPI | 关闭态 FastSim CPI | 关闭态误差 | 候选 CPI | 候选误差 | 误差变化 |
|---|---:|---:|---:|---:|---:|---:|
| TeaLeaf LLC32 C4 | 0.7427406257 | 0.8144051186 | +9.6487% | 0.8186815681 | +10.2244% | **+0.5758 pp** |
| TeaLeaf L1D64 C4 | 0.6138814000 | 0.5034378750 | −17.9910% | 0.5034768500 | −17.9847% | +0.0063 pp |

LLC32 候选总 core cycles 从 32,576,208 增至 32,747,266，增加 171,058；四核
分别增加 36,076、21,961、27,622、85,399 cycles。L1D64 总 core cycles 仅增加
1,559，四核变化为 +165、−1,990、+13,643、−10,259。两组的 UOP、指令和内存请求
人口均不变。L1D64 的 PMU 逐项相同；LLC32 只有 LLC hit/tag hit 各增加 2、remote
supply 减少 2，LLC access/miss 和 DRAM read 均不变。这些细小差异说明更改上游 issue
时刻后，后续 checkpoint 的共享请求交错也会改变。

关闭态与审计态排除 `throughput`、wall time 和审计块后，`scope_metrics`、`threads`
及各 core 目标输出逐项相同，两组均通过。审计量到的局部机会为：

| Case | 查询 UOP | 存在更早槽位的 UOP | 可提前周期和 | 最大提前 | integer | SIMD | memory |
|---|---:|---:|---:|---:|---:|---:|---:|
| LLC32 C4 | 40,299,545 | 1,291,429 | 2,167,139 | 155 | 1,074,974 / 1,532,233 | 1,481 / 2,055 | 214,974 / 632,851 |
| L1D64 C4 | 40,169,794 | 458,543 | 569,131 | 43 | 447,931 / 551,898 | 1,698 / 1,881 | 8,914 / 15,352 |

执行池单元格是“UOP 数 / 可提前周期和”。这些周期高度重叠，也可能被依赖、ROB
retire、内存响应或共享仲裁吸收，不能相加为可回收的 core cycles。本轮的完整 ROI
结果直接表明，大量局部机会并未转化为 CPI 改善。

基线到候选的一次 wall time 变化为 LLC32 +1.62%、L1D64 +1.45%。这是没有交错、
重复和宿主噪声控制的单次观测；审计模式还执行额外双路径计算，所以两者都不作为
生产吞吐证据。

## 解释与下一步边界

局部空档回填会改变 memory UOP 的到达时刻，以及共享 cache/coherence/DRAM 中后续
请求的相对顺序。当前模型在每个 checkpoint 固化共享路径，较早的 producer issue
不会简单地等量缩短最终退休时间；它可能把某些请求移入更差的共享排队顺序。LLC32
正控制的回归说明不能把这个修复直接与 load 等待或 branch recovery 叠加后再用总 CPI
判断各自贡献。

下一轮先建立“issue 变化 → Ruby admission → response → 关键消费者 → retire”的成对
事件账本，找出 LLC32 四个 core 中新增 171,058 cycles 的首个共享分歧。只有当新的
候选能解释并改善该正误差控制，同时保留 L1D64 负尾部收益时，才进入完整矩阵。load
绝对时基、DTLB 可见性和 branch 边界仍是独立待验证问题，本轮没有量化它们的 CPI
影响。

## 验证和复现

本轮二进制 SHA256 为
`ac21ecbfb641f73324a1c3d1453e58d5480091772128ceb59faef0b639839f6f`；测试二进制
SHA256 为 `4a658d8062b1d6fdcbe105d2ca9419f68e48dabe5c96369a5a833634be144082`。

构建和测试：

```bash
cmake --build build -- -j16
./build/fastsim_tests
python3 -m py_compile tools/run_uarch_fastsim.py
```

用对应 case 的冻结 `baseline.cfg` 和 `manifest.txt` 分别运行关闭态、审计态和候选：

```bash
numactl --physcpubind=0-47 --membind=0 \
  ./build/fastsim simulate \
  --config <baseline.cfg> --manifest <manifest.txt> \
  --measurement-scope user-plus-kernel --cores 4 \
  --output <baseline-stats.json>

numactl --physcpubind=0-47 --membind=0 \
  ./build/fastsim simulate \
  --config <baseline.cfg> --manifest <manifest.txt> \
  --measurement-scope user-plus-kernel --cores 4 \
  --committed-pipeline-audit true \
  --output <audit-stats.json>

numactl --physcpubind=0-47 --membind=0 \
  ./build/fastsim simulate \
  --config <baseline.cfg> --manifest <manifest.txt> \
  --measurement-scope user-plus-kernel --cores 4 \
  --fu-gap-aware-schedule true \
  --output <candidate-stats.json>
```

本次原始结果位于 `tmp/fu-gap-aware-phase1-20260907/`，属于可清理实验产物；本报告
保存了决策所需的输入口径、关键数字和二进制指纹。
