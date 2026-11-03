# 四核 private-L2 PMU 低估定位（2026-10-15）

后续已按本层lookup实施生产修复，并在三份输入验证，见
[生产修复结果](perf-cache-estimates-and-l2-repair-results-20261015.md)。下文是修复前
隔离诊断记录，保留当时的生产状态和数字。

已确认一项上报语义错误：FastSim 用只读桶 `l2_read_tag_misses` 与 gem5 原生
private-L2 的 demand tag miss 比较，排除了私有 L2 miss 后由远端供给的请求及
写 demand miss。远端供给是共享 LLC 层的结果，不能据此取消已经发生的私有
L2 tag miss。当前约 80% 的 L2 低估不能全部解释为缓存模型精度差。

本轮完成定位和隔离诊断，生产源码、二进制、时序参数及默认配置均未修改。
诊断版的桶重组用于检验原因，尚不是完整的逐层请求分类修复或硬件 perf 认证。
L1D 的低估、L2 剩余偏差和退休 load 来源覆盖仍有未解决项。

## 1. 固定输入与来源

沿用两份已有 `thread_state_fs_probe` 四核输入，参数均为 `1000 2000`，最快核
达到 300000 measured user UOP 后关闭共同窗口。它们是同一负载的不同采集，
不是两个独立负载或泛化验证集。

| 简称 | 原始 case | TASK macro / memory UOP |
|---|---|---:|
| A，当前主输入 | `tmp/perf-pmu-repair-20261014/diagnostic/fs-probe-rom-gdb` | 427054 / 51313 |
| B，前次 task 输入 | `tmp/task-pmu-repair/fs-probe-task-pmu` | 424730 / 53842 |

工作目录为 [`tmp/pmu-underprediction-diagnosis-20261015/`](../tmp/pmu-underprediction-diagnosis-20261015/)。
`source/` 仅链接原始 case，没有复制或新增 FST。缓存参考通过维护的
`run_native_kernel_fastsim_validation.py --source-root --stats-dir` 获取；未读取
TaoTrace path-class 缓存字段作为真值。范围均为 `workload-task`，归属合同为
`task-oncpu-switch-entry-v1`。独立目标身份门禁为 2/2 valid。

生产 FastSim SHA256：
`f957aa38e4ed9c9320a339a9587d76f48a93fc7ab2b12c33860fdf44948c6777`。
新旧运行的源、输出、诊断二进制指纹分别保存在维护验证结果和
`diagnostic-provenance.json`、`candidate-sha256.json`、`candidate-commands.json`。
采集端身份检查不等于全参数等价；既有 DRAM 部分参数未启用的边界继续有效，见
[参数覆盖说明](gem5-parameter-coverage.md)。本次没有调整任何延迟。

## 2. 已排除的主要原因：forwarding 数量偏多

在 A 上使用同一生产二进制、同一 FST、同一配置 include chain，仅覆盖
`core.store_forwarding=false`：

| 输出 | forwarding 开启 | forwarding 关闭 | gem5 native TASK 参考 |
|---|---:|---:|---:|
| 完整测量物理流 forwarded loads | 11211 | 0 | 此行不跨单位比较 |
| TASK L1D tag miss | 313 | 314 | 429 |
| TASK private-L2 tag miss | 44 | 44 | 287 |
| TASK LLC tag miss | 25 | 25 | 21 |

`validation-off/` 通过维护 native 和 retirement 来源/人口门禁。实验表明，关闭
forwarding 并未消除该输入的 L2 大幅低估，不能据转发数差异直接修改缓存模型。

代码确实另有转发近似：`store_forward_window` 在生产阶段维护最近若干个 store
字节区间，按 SQ 容量和 fence/atomic 边界裁剪，没有依据 consumer 的真实 store
drain 时刻淘汰；扫描也不完整处理较新部分重叠 store 的遮挡。这些是需单独验证
的语义风险，不是本次已证明的 L2 主因。消融不证明转发模型普遍正确。

## 3. 根因：把最终供给路径套到所有缓存层

原生协议的层号与物理层次不同，必须逐层解释：

- `MESI_Three_Level-L1cache.sm` 对应物理 private L2。`I + Load`、`I + Store`
  均执行 `uu_profileMiss`，向 native registry 记录 level=1、outcome=tag miss。
- `MESI_Two_Level-L2cache.sm` 对应共享 LLC。远端 owner 转发执行
  `uu_profileRemoteSupply`，记录 level=2、outcome=remote supply。
- 因此同一个请求可以同时具有 **private-L2 tag miss 和 LLC remote supply**。
  各层自己的 outcome 桶互斥，不意味着跨层结果互斥。

FastSim 的 `replay_previewed_memory_event()` 在私有 L2 miss 后，依据最终
`shared_timing.path` 分桶：remote 路径放进 `l2_remote_supply_misses`；普通写
放进 `l2_write_demand_misses`。`TaskDataPmuScope` 却只把 `l2_read_tag_misses`
作为 `private_l2_tag_misses`。物理流 JSON 的同名字段也沿用只读桶。
于是确实发生过的部分私有 L2 查找失败没有进入被评分字段。

这项问题早于本轮退休计数修复。09-28 的
[L2/LLC caliber 文档](buffer-tso-l2-caliber-fix-20260928.md)将两层的五桶解释混用，
并把 read-only 输出当作通用 native tag miss。其 LBM 单例数字接近不能证明
该映射对共享写入、远端供给负载成立。10-14 task 计数继承了这个定义。
本次证据修正了“低估全部来自缓存模型”的既有归因。

## 4. 同范围分桶与隔离重组

`prepare_diagnostic.py` 将当时源码复制到 `audit-source/`，只增加随
`TaskPmuCounters` 快照/回滚的诊断计数。`audit-stats.json` 是保持原上报公式的
A 审计结果；它与生产运行的原有目标预测逐字段一致。

随后仅在隔离诊断版将 TASK private-L2 上报重组为
`read_tag_misses + write_demand_misses + remote_supply_misses`。
prepare/follower 桶没有加入。该实验检验遗漏量，不宣称解决所有协议状态与合并
人口问题；生产 `build/fastsim` 仍输出原值。

| TASK L2 分桶 | A | B |
|---|---:|---:|
| 原上报的 read tag miss | 44 | 61 |
| 被排除的 remote-supply miss | 256 | 260 |
| 被排除的 write demand miss | 11 | 10 |
| prepare 桶，诊断版仍排除 | 30 | 30 |
| follower 桶 | 0 | 0 |
| 隔离诊断版重组值 | 311 | 331 |
| gem5 native private-L2 tag miss | 287 | 290 |

| 输入 | 生产 L2 signed error | 诊断重组 signed error | 重组后绝对计数误差 |
|---|---:|---:|---:|
| A | −84.6690% | +8.3624% | 24 |
| B | −78.9655% | +14.1379% | 41 |

两例的 L2 MAPE 为 81.8173% → 11.2502%，WAPE 为 81.8024% → 11.2652%。
这是相同两份输入上的上报分类诊断，不是生产修复后的发布精度或新吞吐测量。
不可将其推广到历史 32/10/40 案或称为真机 perf 误差。

维护验证器对生产与诊断两组分别得到 2/2 passed；native 参考和全部审计人口
一致。新增的 15 个诊断字段通过 task+other=full 及逐核聚合守恒。
差异检查覆盖原有 JSON 字段：除了 TASK 及其 other/full/逐核子集的
`private_l2_tag_misses`，所有原有目标计数和时序均相同。
检查排除主机 wall time、throughput、各 `_wall_ns` 计时及异步生产队列的
`max_resident_chunks`，不把这些主机运行波动当作目标变化。

## 5. CPI 与 elapsed 控制

下表生产与隔离重组结果相同；active CPI 分母是 TASK macro instructions。

| 输入 | gem5 active CPI | FastSim active CPI | signed relative error | CPI absolute error，cycles/macro |
|---|---:|---:|---:|---:|
| A | 0.819819 | 0.864942 | +5.5040% | 0.045123 |
| B | 0.804791 | 0.898957 | +11.7006% | 0.094166 |

两例 CPI MAE=0.069644 cycles/macro。

| 输入 | gem5 elapsed，μs | FastSim elapsed，μs | signed relative error |
|---|---:|---:|---:|
| A | 42.653637 | 34.405227 | −19.3381% |
| B | 42.526764 | 34.439193 | −19.0176% |

上报重组没有改善 CPI 或 elapsed。此次没有测量吞吐回归。

## 6. 尚未定位完的部分与修复边界

TASK L1D 仍为 A：313 对 429（−27.0396%），B：331 对 442（−25.1131%）；
LLC 仍为 A：25 对 21（+19.0476%），B：32 对 26（+23.0769%）。
LLC 的 read-only 输出同样需要审计写 demand 的资格，但不能把 L2 的 remote
修复直接套给 LLC；在原生 LLC 层 remote supply 本就应与 tag miss 分开。

下一步生产修复需要按**每层自身的 lookup / permission / in-flight 状态**分类，
独立保存 read/write/upgrade/merge/remote 的适用层次，再派生上报字段。先加
远端供给、冷写、权限升级、同线 follower、buffer prepare/head-demand 的最小
反例，要求明确事件字面期望和回滚守恒，然后统一 TASK 与其他上报范围。
不能只把一个最终路径的所有桶相加后称为完整协议模型。

剩余 L1/L2 偏差需要原生与 FastSim 请求身份的逐请求配对，检查首次分叉的
请求、cache 状态和生命周期。当前 source 的汇总守恒不提供这种一一对应；
native retirement 的来源覆盖不足也仍按失败关闭，不输出缺失分类的精度。
不根据总数接近或此次重组结果调节 cache/DRAM 延迟。

## 7. 复现与存储

使用 Python 3.11，FastSim 运行环境需要 `LD_LIBRARY_PATH=/opt/gcc-11/lib64`。

```bash
python3 tmp/pmu-underprediction-diagnosis-20261015/prepare_diagnostic.py --candidate
cmake -S tmp/pmu-underprediction-diagnosis-20261015/audit-source \
  -B tmp/pmu-underprediction-diagnosis-20261015/audit-build \
  -DCMAKE_BUILD_TYPE=Release -DFASTSIM_BUILD_TESTS=OFF -DFASTSIM_ENABLE_IPO=OFF
cmake --build tmp/pmu-underprediction-diagnosis-20261015/audit-build --target fastsim -- -j16
```

带 `--candidate` 生成重组版，不带则恢复原公式的分桶审计快照。两者差异保存在
`audit-classifier-before.cpp` 和 `audit-source/src/simulator.cpp`；实际重组运行命令
保存在 `candidate-commands.json`。重放应执行其中的命令并设置上述运行环境。

```bash
python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/pmu-underprediction-diagnosis-20261015/source \
  --stats-dir tmp/pmu-underprediction-diagnosis-20261015/stats-candidate \
  --output-dir tmp/pmu-underprediction-diagnosis-20261015/validation-candidate \
  --expected-cases 2 --jobs 1
python3 tmp/pmu-underprediction-diagnosis-20261015/summarize.py
```

[`diagnosis.json`](../tmp/pmu-underprediction-diagnosis-20261015/diagnosis.json)保存数值、
门禁引用及逐字段差异。目录内 `validation-current/`、`validation-candidate/` 和
`validation-off/` 保存完整维护报告，`identity.json` 保存独立身份门禁。
完成时 `tmp/` 下实际普通 `.fst` 文件合计 **19,184,545,640 bytes**，低于用户
20,000,000,000 bytes 上限；没有新增采集或复制大 trace。
