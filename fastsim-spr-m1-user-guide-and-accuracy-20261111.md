# SPR-M1 — Retired Prepare 基线：使用说明与 CPI / PMU 误差

日期：2026-11-11。本文固定使用 **SPR-M1**，即冻结 R0/R1 `v2-wait`
版本；结果范围为 **Zstd、XZ、FLAC、Graph500** 四个负载。

四负载等权 CPI **MAE = 0.063542 cycles/macro，MAPE = 8.803665%**。
三项退休 load miss 的诊断 MAPE 分别为 **38.464125%、30.591010%、
37.675434%**。独立 native L1D / private-L2 / LLC tag-miss 的诊断 MAPE
分别为 **17.943417%、15.348516%、7.826429%**。
两类 PMU 的原始计数、证据状态及 WAPE 在下文分别列出。
这些结果继承已有同输入回放；本次编写文档仅从归档结果计算四负载汇总，
没有重新采集、运行仿真、改变预测或重新测量吞吐。

## 1. 版本身份与默认配置

| 项目 | SPR-M1 固定身份 |
|---|---|
| 名称 | SPR-M1 — Retired Prepare 基线 |
| 实现来源 | 冻结 R0/R1 `v2-wait` |
| 当前工作区程序 | `/data00/yinhaolang/FastSim/build/fastsim` |
| 保留的命名程序 | `/data00/yinhaolang/FastSim/tmp/spr-m1-20261111/fastsim` |
| 二进制 SHA-256 | `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905` |
| 固定配置 | `configs/spr-m1-retired-prepare.cfg` |
| 固定配置 SHA-256 | `711561de8cbb306fcad72ae76b4ed7ab0e3ebb30601263b7467cce86519b310f` |
| 后续评测默认入口 | `configs/spr-evaluation-default.cfg` |
| 原 SPR 入口 | `configs/gem5-spr-retired.cfg`，指向相同默认入口 |
| 版本来源清单 | [spr-m1-retired-prepare.json](configs/spr-m1-retired-prepare.json) |

里程碑同时绑定程序和有效配置。当前工作区程序与保留程序的哈希一致；
今后编译可能覆盖 `build/fastsim`，因此固定基线对照建议使用保留程序。
后续模型版本使用 SPR-M2、SPR-M3 等名称，保留 SPR-M1 的配置和程序身份。
默认入口目前选择 SPR-M1；跨里程碑复现时直接指定固定配置。

## 2. “有 prepare、无预取”的具体含义

| 行为 | 设置或约定 |
|---|---|
| 退休 store prepare | `core.legacy_prepare_timing = true`，保留正常 store 权限获取 |
| 统一父服务 | `core.unified_parent_service = v2-wait` |
| 退休前 early prepare | gem5 native `sprEarlyStorePrepare = false` |
| L1D / private-L2 硬件预取 | gem5 native controller `enable_prefetch = false` |
| FastSim L1 / L2 预取服务及 follower 扩展 | 关闭 |
| FastSim LLC resident 预取迁移与软件预取 early ack | 关闭 |
| 软件预取指令 | 保留退休功能指令及其人口 |
| 输入与执行方式 | 完整退休功能流，保留现有并行两阶段框架 |

四个归档 source 的 `config.ini` 均记录了四核 L1D / private-L2
硬件预取关闭、四核退休前 early prepare 关闭。
FastSim 开关只能改变模型，不能追溯改变已有 native 采集。
新采集应明确使用 `--no-hardware-prefetch --no-early-store-prepare`，
并核验实际输出的 `config.ini`。

本文原实验条件是 C4、32GiB、2.6GHz；固定模型配置的 L1D 为 48KiB /
12 路、L1I 为 32KiB / 8 路、private L2 为 2MiB / 16 路、LLC 为
96MiB / 12 路，cache line 为 64B。
新 source 必须分别绑定自身 `config.ini` / `effective-target.json`
中的几何和时长，以及自身 `thread-state.plan`。固定配置的 310 项共同参数
与本文原回放配置一致，但这不能证明另一种硬件配置已经对齐。

## 3. 运行方法

以下命令从 `/data00/yinhaolang/FastSim` 仓库根目录执行。
所有新输出和临时文件都放在项目 `tmp/`。

### 3.1 准备程序及环境

```bash
source scripts/project_tmp.sh

SPR_ROOT=/data00/yinhaolang/FastSim
SPR_BIN="$SPR_ROOT/tmp/spr-m1-20261111/fastsim"

# 使用本机 GCC 的 libstdc++，避免会话环境中旧版库的符号版本冲突。
SPR_GCC_LIBDIR="$(dirname "$(g++ -print-file-name=libstdc++.so.6)")"
export LD_LIBRARY_PATH="$SPR_GCC_LIBDIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

printf '%s  %s\n' \
  dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905 \
  "$SPR_BIN" | sha256sum -c -
printf '%s  %s\n' \
  711561de8cbb306fcad72ae76b4ed7ab0e3ebb30601263b7467cce86519b310f \
  "$SPR_ROOT/configs/spr-m1-retired-prepare.cfg" | sha256sum -c -
```

### 3.2 回放一个已采集的四核负载

以本文 Zstd source 为例。运行前检查 `tmp/` 存在，随后创建独立输出目录：

```bash
ls -ld "$SPR_ROOT/tmp"
SPR_SOURCE="$SPR_ROOT/tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/c04-777.zstd_r"
SPR_OUT="$(mktemp -d "$SPR_ROOT/tmp/spr-m1-use-XXXXXXXX")"

cat > "$SPR_OUT/run.cfg" <<EOF
config.include = $SPR_ROOT/configs/spr-m1-retired-prepare.cfg
thread_state.plan = $SPR_SOURCE/thread-state.plan
EOF

"$SPR_BIN" simulate \
  --config "$SPR_OUT/run.cfg" \
  --manifest "$SPR_SOURCE/tao_trace/manifest.txt" \
  --measurement-scope user-plus-kernel \
  --native-kernel-trace true \
  --output "$SPR_OUT/c04-777.zstd_r.json" \
  > "$SPR_OUT/run.log" 2>&1

printf '结果目录：%s\n' "$SPR_OUT"
```

固定配置用于复现 SPR-M1；跟随后续默认评测时，将上面的 include 改为
`$SPR_ROOT/configs/spr-evaluation-default.cfg`。
`thread_state.plan` 是配置项，不是独立命令行参数。
原 manifest 包含各核 warmup slice 和测量人口，必须保留对应 `.fst`
及功能 companions，不要将各核截成相同指令数。

其余三个本文 source 位于同一 `sources/` 目录，case ID 分别为
`c04-801.xz_s`、`c04-817.flac_s`、`c04-854.graph500_s`。
切换时同时更换 source、plan、manifest 和输出 case ID。
对不同硬件几何的新 source，还需在本地 `run.cfg` 中加入该 source 的
参数映射；不能仅替换 manifest。

### 3.3 查看输出

FastSim 输出 JSON 和日志。本文 CPI 使用
`time_accounting.active_cycles / time_accounting.task_macro_instructions`，
即参与核 workload-task active cycles 之和除以任务宏指令数之和。
核数不能再乘一次；宏指令与 UOP 不可混用。

三个 PMU 预测值读取：

```text
scope_metrics.retired_cache.scopes.workload_task.events
  .retired_l1_load_misses.estimated_count
  .retired_l2_load_misses.estimated_count
  .retired_l3_load_misses.estimated_count
```

预测策略为 `spr-retired-load-miss-v2`，计数单位是退休宏指令事件。
保留同事件 `count/unknown/prediction_unknown` 等资格和近似诊断；
它们与完整点预测 `estimated_count` 的含义不同。
`wall_time_seconds`、`wall_time_breakdown`、`throughput` 可用于观察本次运行性能，
正式吞吐比较仍需同输入配对测量。

## 4. CPI 误差

采集使用 500K warmup、首核 10M measured user-UOP 共同采集终点
`first-core-target-common-end-v1`；慢核保留共同终点前实际采集到的全部人口。
CPI 两侧均使用 `common-start-v1` 和 `retired-stream-completion-v1`
active 计数端点。共同采集终点与逐核退休流完成计数端点是不同概念。
参考来自独立 native BaseCPU active-counter ledger。

| 负载 | gem5 CPI | SPR-M1 CPI | CPI 绝对误差（cycles/macro） | 有符号相对误差 |
|---|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.142316 | 0.063733 | −5.284424% |
| XZ | 0.575297 | 0.601359 | 0.026062 | +4.530102% |
| FLAC | 0.284584 | 0.318433 | 0.033849 | +11.894041% |
| Graph500 | 0.966422 | 0.835896 | 0.130526 | −13.506093% |

| 汇总范围 | 有效 / 预期负载 | CPI MAE（cycles/macro） | CPI MAPE | 最大 CPI 绝对误差（cycles/macro） | 最大 APE |
|---|---:|---:|---:|---:|---:|
| 本文四负载，等权 | 4 / 4 | **0.063542** | **8.803665%** | 0.130526（Graph500） | 13.506093%（Graph500） |

表内数值显示六位小数，汇总使用未舍入原值。
四负载 SPR-M1 预测与真正冻结 R0/R1 程序的同输入预测逐项相同，
`SPR-M1 CPI − 冻结 R0/R1 CPI = 0`；本次命名和配置固化没有产生精度增益。
四例归档报告的 CPI 均为 `scored-active`，逐核指令人口匹配。
XZ 的整体 native validator 失败来自 PMU unknown，本文保留其独立 CPI 结果。

## 5. 三项退休 load miss 的 PMU 诊断误差

范围为 `workload_task` 的退休宏指令事件；目标事件为
`MEM_LOAD_RETIRED.L1_MISS`、`L2_MISS`、`L3_MISS`
（event `0xD1`，umask 分别为 `0x08/0x10/0x20`）。
下表 gem5 数值是独立 native 服务事实分类中的已知计数，
包含明确记录的 native service-to-event 估计。
这里不使用旧请求 tag-miss 或 `kernel-events*.json` path-class 字段替代退休事件。

### 5.1 原始计数与逐负载误差

| 负载 | 退休 load miss 事件 | gem5 参考计数 | SPR-M1 预测计数 | 绝对计数误差 | 有符号相对误差 |
|---|---|---:|---:|---:|---:|
| Zstd | L1_MISS | 680,317 | 1,021,776 | 341,459 | +50.191161% |
| Zstd | L2_MISS | 30,763 | 64,322 | 33,559 | +109.088840% |
| Zstd | L3_MISS | 26,416 | 58,808 | 32,392 | +122.622653% |
| XZ | L1_MISS | 181,784 | 80,809 | 100,975 | −55.546693% |
| XZ | L2_MISS | 11,727 | 12,130 | 403 | +3.436514% |
| XZ | L3_MISS | 11,369 | 11,736 | 367 | +3.228076% |
| FLAC | L1_MISS | 44,624 | 35,611 | 9,013 | −20.197651% |
| FLAC | L2_MISS | 1,048 | 1,151 | 103 | +9.828244% |
| FLAC | L3_MISS | 165 | 206 | 41 | +24.848485% |
| Graph500 | L1_MISS | 4,905,964 | 6,275,758 | 1,369,794 | +27.920996% |
| Graph500 | L2_MISS | 277,711 | 277,740 | 29 | +0.010443% |
| Graph500 | L3_MISS | 277,638 | 277,645 | 7 | +0.002521% |

### 5.2 四负载诊断汇总

| 事件 | 有限 APE / 预期负载 | 参考计数总和 | 预测计数总和 | MAPE（负载等权） | WAPE（参考计数加权） | 有符号计数偏差 |
|---|---:|---:|---:|---:|---:|---:|
| L1_MISS | 4 / 4 | 5,812,689 | 7,413,954 | **38.464125%** | **31.332160%** | +27.547749% |
| L2_MISS | 4 / 4 | 321,249 | 355,343 | **30.591010%** | **10.612951%** | +10.612951% |
| L3_MISS | 4 / 4 | 315,588 | 348,395 | **37.675434%** | **10.395516%** | +10.395516% |

| 事件 | P50 APE | P90 APE | P99 APE | 最大 APE |
|---|---:|---:|---:|---:|
| L1_MISS | 39.056078% | 53.940033% | 55.386027% | 55.546693%（XZ） |
| L2_MISS | 6.632379% | 79.310662% | 106.111023% | 109.088840%（Zstd） |
| L3_MISS | 14.038281% | 93.290403% | 119.689428% | 122.622653%（Zstd） |

分位数按四个未舍入 APE 排序后线性插值。Graph500 占据大部分 L2 / L3
参考计数且误差很小，因此计数加权 WAPE 明显低于负载等权 MAPE。
两者回答不同问题，应分别报告。

### 5.3 证据状态与 unknown

**上述 PMU 数字是沿用用户此前授权的暂定诊断误差，硬件等价仍为
`unverified`，不构成完整 SPR 真机 perf 精度认证。**

| 负载 | native 三事件 unknown（逐事件） | 模型旧资格 unknown（逐事件） | 模型点预测 unknown（逐事件） | 原 native validator |
|---|---:|---:|---:|---|
| Zstd | 0 | 8,858 | 0 | passed |
| XZ | 1 | 0 | 0 | failed：native membership unknown |
| FLAC | 0 | 0 | 0 | passed |
| Graph500 | 0 | 0 | 0 | passed |

XZ 的已知参考计数分别可能增加 0 或 1，三个误差最多变化
0.000245、0.008820、0.009079 个百分点。本文保留已知参考值计算诊断差值，
保留原失败状态，未把 unknown 改成零或宣称完整 PMU 门禁通过。
其余三例的逐事件比较仍标为 `incomplete-evidence`：
目标 ISA / service-event 资格未完成硬件认证，参考包含明确的事件估计，
退休功能流对错误路径及预取带来的间接 cache 状态影响存在建模缺口。
Zstd 另有旧资格 unknown；完整点预测为零 unknown 并不消除该证据缺项。

## 6. Native 分支与 cache tag-miss 诊断误差

本节沿用维护验证器已评分的 `workload-task` native 结果。
Cache 参考来自经过审计的 Ruby/SLICC controller actions；
分支参考来自原始退休 BPred redirect 观察。
FastSim 计数读取 `scope_metrics.task_pmu.pmu`，人口契约为
`task-oncpu-switch-entry-v1`。Cache tag-miss 的计数单位是 cache-line
需求请求事件，与第 5 节的退休 load 宏指令事件分别报告。

### 6.1 逐负载 native 计数与误差

| 负载 | native 指标 | gem5 参考计数 | SPR-M1 预测计数 | 绝对计数误差 | 有符号相对误差 |
|---|---|---:|---:|---:|---:|
| Zstd | Branch misses | 198,966 | 204,254 | 5,288 | +2.657741% |
| Zstd | L1D tag misses | 663,467 | 810,811 | 147,344 | +22.208188% |
| Zstd | Private-L2 tag misses | 68,173 | 82,204 | 14,031 | +20.581462% |
| Zstd | LLC tag misses | 63,739 | 62,171 | 1,568 | −2.460032% |
| XZ | Branch misses | 209,137 | 209,718 | 581 | +0.277808% |
| XZ | L1D tag misses | 110,599 | 119,409 | 8,810 | +7.965714% |
| XZ | Private-L2 tag misses | 56,162 | 59,195 | 3,033 | +5.400449% |
| XZ | LLC tag misses | 38,760 | 39,151 | 391 | +1.008772% |
| FLAC | Branch misses | 27,158 | 27,706 | 548 | +2.017822% |
| FLAC | L1D tag misses | 18,479 | 26,163 | 7,684 | +41.582337% |
| FLAC | Private-L2 tag misses | 3,401 | 4,605 | 1,204 | +35.401353% |
| FLAC | LLC tag misses | 1,491 | 1,076 | 415 | −27.833669% |
| Graph500 | Branch misses | 873 | 1,026 | 153 | +17.525773% |
| Graph500 | L1D tag misses | 556,493 | 556,590 | 97 | +0.017431% |
| Graph500 | Private-L2 tag misses | 277,711 | 277,741 | 30 | +0.010803% |
| Graph500 | LLC tag misses | 277,636 | 277,645 | 9 | +0.003242% |

### 6.2 四负载 native 诊断汇总

| native 指标 | 有限 APE / 预期负载 | MAPE（负载等权） | WAPE（参考计数加权） | 有符号计数偏差 |
|---|---:|---:|---:|---:|
| Branch misses | 4 / 4 | **5.619786%** | **1.506418%** | +1.506418% |
| L1D tag misses | 4 / 4 | **17.943417%** | **12.151993%** | +12.151993% |
| Private-L2 tag misses | 4 / 4 | **15.348516%** | **4.513044%** | +4.513044% |
| LLC tag misses | 4 / 4 | **7.826429%** | **0.624433%** | −0.414804% |

| native 指标 | P50 APE | P90 APE | P99 APE | 最大 APE |
|---|---:|---:|---:|---:|
| Branch misses | 2.337781% | 13.065363% | 17.079732% | 17.525773%（Graph500） |
| L1D tag misses | 15.086951% | 35.770092% | 41.001112% | 41.582337%（FLAC） |
| Private-L2 tag misses | 12.990955% | 30.955385% | 34.956756% | 35.401353%（FLAC） |
| LLC tag misses | 1.734402% | 20.221578% | 27.072460% | 27.833669%（FLAC） |

这些 native 计数沿用冻结 R0/R1 对比归档中维护验证器的独立评分，
并与本文恢复程序输出逐项核对。四例 native PMU 评分均为 `scored`；
XZ 在新增退休 load membership 检查中的失败仍按第 5 节保留。
Cache 参考的 `reference_source` 为 `native-ruby-slicc:workload-task:*`，
没有回退到 path-class。分支、L1D、private-L2 的硬件映射仍为 `proxy`，
LLC 为 `diagnostic`，`hardware_pmu_formal = false`。
这些 native 误差不会改变退休 load miss 的误差或其证据状态。

## 7. 误差公式与结果复核

设 $F_i$ 为 FastSim 预测、$G_i$ 为 gem5 参考；本文 $N=4$。
百分比结果以百分数展示，下式中的 100 用于将比值换算为百分数。

$$
e_i = 100 \cdot \frac{F_i-G_i}{G_i}
$$

$$
AE_i = \left|F_i-G_i\right|
$$

$$
\mathrm{MAPE} = \frac{1}{N}\sum_{i=1}^{N}\left|e_i\right|
$$

$$
\mathrm{CPI\ MAE} = \frac{1}{N}\sum_{i=1}^{N}
\left|\mathrm{CPI}_{F,i}-\mathrm{CPI}_{G,i}\right|
$$

$$
\mathrm{WAPE} = 100 \cdot
\frac{\sum_{i=1}^{N}\left|F_i-G_i\right|}{\sum_{i=1}^{N}G_i}
$$

$$
\mathrm{Signed\ bias} = 100 \cdot
\frac{\sum_{i=1}^{N}(F_i-G_i)}{\sum_{i=1}^{N}G_i}
$$

纯文本对应关系：相对误差 = 100 × (预测 − 参考) / 参考；
绝对误差 = abs(预测 − 参考)；MAPE = 各负载绝对相对误差的算术平均；
CPI MAE = 各负载 CPI 绝对误差的算术平均；
WAPE = 100 × 绝对计数误差总和 / 参考计数总和。

CPI 的绝对误差和 MAE 单位是 cycles/macro，PMU 绝对误差单位是事件计数。
本文所有参考计数非零，12 个退休 load miss 和 16 个 native 诊断 APE 均可计算。

本文配套 [四负载完整精度数据](fastsim-spr-m1-accuracy-four-workloads-20261111.json)
保留未舍入值、四例原始计数、native / 模型 unknown、参考中的估计项、
原门禁状态和逐例预测 / 参考文件 SHA-256。
归档结果没有因本次文档生成而重新评分或覆盖。

需要对已有输出重新验证时，使用维护入口
`tools/run_native_kernel_fastsim_validation.py` 的 `--source-root`、
`--stats-dir`、`--retired-cache-misses`，并将 `--expected-cases` 设为真实
source 子集数量；该模式不运行 FastSim。四例子集需只含上述四个
`request.json` case 目录或指向它们的符号链接。
硬件身份检查另用 `tools/validate_fs_oracle_identity.py`。
XZ 仍会因 native unknown 触发完整门禁失败，不能用本文诊断授权修改验证器状态。
新的 CPI / PMU / 吞吐评测按
[精度报告契约第 0、8 节](docs/accuracy-reporting-contract.md)执行。

## 8. 可追溯材料

- [SPR-M1 里程碑记录](fastsim-spr-m1-milestone-20261111.md)
- [SPR-M1 源码与 native 补丁分发说明](docs/spr-m1-source-distribution-20261111.md)
- [SPR-M1 固定配置](configs/spr-m1-retired-prepare.cfg)
- [当前默认评测入口](configs/spr-evaluation-default.cfg)
- [四负载未舍入结果、状态与来源哈希](fastsim-spr-m1-accuracy-four-workloads-20261111.json)
- [配置与归档结果的轻量复核记录](tmp/spr-m1-20261111/guide-verification.json)
- [保留程序对应的源码文件清单](tmp/spr-m1-20261111/source-manifest.json)
- [已完成的配置解析检查](tmp/spr-m1-20261111/check-config.log)
- [已完成的标准测试日志](tmp/spr-m1-20261111/fastsim_tests.log)

完整 source 位于 `tmp/spr-no-prefetch-no-early-prepare-c4-20261110/sources/`；
本文四例预测位于 `tmp/restore-r01-main-20261111/replay/<case_id>.json`；
原参考报告位于
`tmp/r01-v2-repair-exclusive-20261111/evaluation/frozen-native/cases/<case_id>/validation.json`。
命名程序及这些实验来源保存在项目 `tmp/`，迁移或清理前需保留对应文件和哈希。
