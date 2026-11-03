# store forwarding 默认开启：全矩阵 CPI / PMU / 吞吐复核（2026-09-24）

按用户要求，在当前工作树代码版本上把 store-to-load forwarding 由默认关闭改为
**默认开启**，然后在完整矩阵上复核 CPI、PMU 与吞吐。本文只记录一次默认值翻转与
验证结果，**未调整任何延迟常数，未加任何负载相关补偿**。

机制本身见 [store-forwarding-default40-20260921.md](store-forwarding-default40-20260921.md)：
被更老且仍驻留 store queue 的 store 完全覆盖的纯 load，从转发路径 1 周期完成，
不发私有 cache / Sequencer 请求；部分覆盖保持原语义；窗口在屏障、序列化操作、
原子、locked RMW 处清空。功能 trace 不含 store 数据，因此不改变任何架构值。

## 改动清单（最小）

- `include/fastsim/config.hpp:676`：`bool store_forwarding = true;`（默认值翻转，
  唯一的模型默认变化）。
- `src/config.cpp`：删除 `store_forwarding` 必须搭配 `interval_weave` 的 **validate
  期硬抛错**。默认开启后，任何使用非 weave core model 的默认 `SimulatorConfig`
  都必须仍能通过校验；转发本就是 weave 专用的惰性杠杆，非 weave 路径不消费它。
- `src/simulator.cpp`：两处运行时门由 `core.interval`（对 interval_bound 也非空）
  收紧为 `config_.core_model == "interval_weave"`，保持机制 weave 专用，非 weave
  模型（含 interval_bound / scalar）行为位级不变。
- `include/fastsim/simulator.hpp` / `src/simulator.cpp`：窗口统计新增
  `forwarded_loads` 增量（基于逐核累计 `total.forwarded_loads` 与窗口基线），
  使 pause/resume 守恒恒等式可表达。
- `tests/test_main.cpp`：窗口 PMU 守恒恒等式由
  `memory_accesses == l1d_accesses` 更新为
  `memory_accesses == l1d_accesses + forwarded_loads`（转发 load 是访存但不查私有
  cache）。
- `tests/test_store_forwarding.py`：默认值断言由 default-off 改为 default-on，并
  校验默认与显式 `true` 一致、显式 `false` 回退后 `forwarded_loads==0`。

验证二进制（rebuild 后）sha256：
`14b5f65e5ed646e720f6111472edd1fd65016e06620b1796756d5eb8cd846a89`。

## 验证门

- `cmake --build build -- -j16`：通过。
- `./build/fastsim_tests`：all FastSim tests passed。
- `python -m unittest tests.test_store_forwarding`：OK。

## 矩阵与口径

- 全矩阵 9 个工作负载 × 4 个核数（C4/C8/C16/C32），真值来源
  `tmp/tso-buffer114-common-end-baseline40-20260918/formal/source/`，
  策略 `first-core-target-common-end-v1`。
- CPI/PMU 在与 gem5 采集一致的 **buffer-114** 几何下评分：
  `tso_store=buffer`、`tso_store_buffer_size=114`、`store_wait_relief=false`。
  gem5 CPI 是真实 full-system O3 timing 真值（见 20260921 文档铁证）。
- ON 与 OFF 仅 `core.store_forwarding` 不同，其余配置链完全一致，可直接配对。
- 39 个源目录中 `uarch-baseline-c08-857.namd_s` 的 `tao_trace/` 目录整体缺失
  （采集缺口，非模型问题），ON 与 OFF 在该例同样无法 replay。**有效矩阵 38 例**。
- cache 真值只取 native Ruby/SLICC；未使用 taotrace path-class cache 字段。
  硬件身份需另用 `tools/validate_fs_oracle_identity.py` 验证，本文不覆盖。

## CPI 精度（38 例，诊断口径）

| 范围 | n | ON MAPE | OFF MAPE | ON MAE | OFF MAE |
|---|---:|---:|---:|---:|---:|
| C4 | 10 | 7.548% | 7.562% | 0.0579 | 0.0581 |
| C8 | 9 | 6.448% | 6.475% | 0.0533 | 0.0540 |
| C16 | 10 | 6.297% | 6.386% | 0.0455 | 0.0477 |
| C32 | 9 | 8.200% | 8.286% | 0.0784 | 0.0809 |
| **全部** | **38** | **7.113%** | **7.167%** | **0.0584** | **0.0598** |

MAE 单位为 cycles per macro instruction。

配对结论：
- 19 例绝对相对误差改善、19 例略变差，**双向、无系统性方向**。
- 平均绝对相对误差位移 **0.117 个百分点**，最大 **1.206 个百分点**。
- 最大单例仍在尾部（LBM C32：ON 17.005% vs OFF 16.841%；Graph500 在 ON 下多为
  改善方向）。总体 ON 略有微弱正向偏差（MAPE −0.054pp，MAE −0.0014）。

## PMU 精度（38 例）

| counter | 口径 | ON MAPE | OFF MAPE | ON WAPE | OFF WAPE | 来源 |
|---|---|---:|---:|---:|---:|---|
| branch_misses | proxy | 3.579% | 3.579% | 1.558% | 1.558% | kernel_events-v3 |
| l1d_tag_misses | proxy | 30.505% | 30.531% | 20.126% | 20.130% | native-ruby-slicc |
| private_l2_tag_misses | proxy | 77.557% | 77.694% | 29.710% | 29.717% | native-ruby-slicc |
| llc_tag_misses | diagnostic | 170.453% | 170.453% | 131.364% | 131.361% | native-ruby-slicc |

branch 完全不变；各级 cache miss 的 ON/OFF 差异在小数点后第二位即噪声量级。
高 PMU 误差仍是已知的 NAMD / LBM 尾部与 prepare-vs-demand 事件域问题，
与本次默认翻转无关，不在此归因或修复。

## 硬守恒不变量

ON 与 OFF 的 `retired_instructions` / `retired_uops` / `memory_uops` /
`retired_branches` 在 38 例上逐一相等，**0 处不匹配**。窗口守恒恒等式
（含 forwarded_loads）逐窗成立。

## 吞吐（非独占共享主机，对称重复）

同一二进制、同一 buffer-114 配置、同一 case manifest，ON/OFF 各重复 4 次，
单进程串行 replay（核数按 case：C4 用 4、C32 用 32），用外设计时器测墙钟。

| case | 模式 | 墙钟(s) | CV% | MUOPS/s | 转发占退休uop |
|---|---|---:|---:|---:|---:|
| c04 stockfish | on / off | 11.42 / 11.39 | 1.5 / 0.2 | 3.418 / 3.427 | 0.78% / 0 |
| c04 lbm | on / off | 14.92 / 15.40 | 0.5 / 0.7 | 2.314 / 2.241 | 11.95% / 0 |
| c04 namd | on / off | 13.14 / 13.28 | 0.3 / 1.3 | 3.149 / 3.116 | 2.59% / 0 |
| c04 tealeaf | on / off | 11.66 / 11.68 | 0.2 / 0.3 | 3.350 / 3.343 | 0.02% / 0 |
| c04 graph500 | on / off | 28.83 / 29.01 | 0.3 / 0.4 | 1.666 / 1.655 | 1.15% / 0 |
| c32 lbm | on / off | 153.36 / 155.80 | 0.8 / 0.1 | 1.924 / 1.894 | 12.11% / 0 |

- ON/OFF 墙钟比区间 **0.969–1.003**，uops/s 比区间 **0.997–1.033**。
- 转发命中占比最高的 LBM（约 12%）增益最大（c04 约 +3.3% uops/s）；其余工作负载
  在 ±1% 内。重复 CV 多在 1.5% 以下，差异与噪声同阶。
- 这些是**共享主机上的诊断吞吐**，不是独占机正式吞吐数。

## 结论

1. 默认开启 store forwarding 在 38 例有效矩阵上**精度中性、略带微弱正向**：
   CPI MAPE 7.113%（OFF 7.167%）、MAE 0.0584（OFF 0.0598），PMU 差异为噪声量级。
2. 硬守恒不变量 38/38 完全保持，非 weave 模型位级不变。
3. 吞吐中性至轻微正向（最多约 +3%，集中在高转发命中的 store-bound LBM），
   无回退。可作为默认值保留。
4. 唯一未评分例 c08 NAMD 是 `tao_trace` 采集缺口，与本改动无关；如需要该例应
   重新采集后再评。

产物：
- ON 结果 `tmp/sf-default-on-fullmatrix-20260924/full-out/`
- OFF 结果 `tmp/sf-default-on-fullmatrix-20260924/full-out-off/`
- 吞吐原始结果 `tmp/sf-default-on-fullmatrix-20260924/tp-runs/`
- 吞吐脚本与汇总 `tmp/sf-default-on-fullmatrix-20260924/throughput_harness.py`、
  `throughput_report.json`
