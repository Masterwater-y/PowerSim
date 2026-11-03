# 三个已采集 C8 负载的 CPI 与 PMU 验证（2026-10-22）

本轮完成 Zstd rate ×8、FLAC speed 八线程和 Graph500 speed 八线程的同源回放，每个负载比较旧 prepare/驻留证书模型与新 bounded_analytic 模型。SQLite、XZ 的 C8 正式来源尚未合格，未纳入；Marian 本轮暂缓。这是选定 ROI 的模拟器对比，不是完整 SPEC 分数或真机 perf 认证。

## 实验身份与配置

输入为完整系统采集的用户态＋内核态 FST，通过共同终点、进程范围和独立硬件身份检查。全部使用相同 case 自带配置及相同线程状态 plan；仅两个 store 模型开关组不同。CPU 为八核、ROB 192、LQ 32、有效 SQ/TSO buffer 114，前端/发射/提交宽度 8，L1D 32 KiB、私有 L2 1 MiB、共享 LLC 64 MiB，LRU，内存八通道、3 GiB，tCL/tRCD/tRP=43、tRAS=97（FastSim 核周期）。输出中的 configured_sq_entries=32 为 buffer 模式未使用的配置值，实际 sq_entries=114。

两模式均开启 response_branch_recovery；store hit 延迟为 L1D=1、L2=5，commit→IEW=1；关闭 software_prefetch_early_ack 和旧 store_request_engine。旧模型开启 prepare_lifecycle 与 residence_certificate；新模型选择 bounded_analytic 并关闭上述两个旧开关。compact uncore 为既有解析近似，不能称与 Ruby 网络逐周期等价。

独立身份检查 3/3 通过：[identity.json](../tmp/spec-c8-accuracy-20261022/identity.json)；详细参数映射：[hardware-alignment-detailed.json](../tmp/spec-c8-accuracy-20261022/hardware-alignment-detailed.json)；二进制、脚本、配置和源元数据冻结：[freeze.json](../tmp/spec-c8-accuracy-20261022/freeze.json)；最终逐报告校验：[report-receipt.json](../tmp/spec-c8-accuracy-20261022/final/report-receipt.json)。

本轮未生成或复制 FST；三个源合计 25,417,018,112 字节（25.417 GB，含预热记录）。复核全工作区 FST 为 93,851,350,384 字节（93.851 GB，按 inode 去重），低于 200 GB 限额，见 [storage-final.json](../tmp/spec-c8-accuracy-20261022/storage-final.json)。预热只用于建立状态，不进入下表测量分母。

## Active CPI

CPI 使用 workload-task 的 active cycles / 宏指令，各模拟器使用自身分母；三源两模式的指令人口匹配。相对误差=(FastSim−gem5)/gem5；绝对误差单位为 cycles/宏指令。FLAC 的时间核验可评分，但原生 PMU 人口门失败，不能称整项验证通过。

| 负载 | store 模型 | gem5 CPI | FastSim CPI | 相对误差 | CPI 绝对误差 | 原生 PMU 来源门 |
|---|---|---:|---:|---:|---:|---|
| zstd rate x8 | legacy-prepare | 1.525718 | 1.420118 | -6.921% | 0.105600 | scored |
| zstd rate x8 | bounded | 1.525718 | 1.389485 | -8.929% | 0.136233 | scored |
| flac speed | legacy-prepare | 0.449687 | 0.471905 | +4.941% | 0.022218 | source-gate-failed |
| flac speed | bounded | 0.449687 | 0.471898 | +4.939% | 0.022211 | source-gate-failed |
| graph500 speed | legacy-prepare | 2.056529 | 1.904190 | -7.408% | 0.152339 | scored |
| graph500 speed | bounded | 2.056529 | 1.838376 | -10.608% | 0.218153 | scored |

| store 模型 | CPI 样本 | CPI MAPE | CPI MAE（cycles/宏指令） | 最大 APE |
|---|---:|---:|---:|---:|
| legacy-prepare | 3 | 6.423% | 0.093386 | 7.408% |
| bounded | 3 | 8.159% | 0.125533 | 10.608% |

本组三例中，新模型的 CPI 精度比旧 prepare/证书模型差：MAPE 从 6.423% 到 8.159%，MAE 从 0.093386 到 0.125533 cycles/宏指令。退化主要体现在 Zstd 与 Graph500；FLAC 基本不变。这只是当前三段 ROI 的对照结论，未据此调整参数或宣布新模型更优。

## Elapsed 运行时间

以下是目标机 ROI 的 elapsed 时间，不是回放在宿主机上的耗时。所有值从 time_comparison 读取，八个核的 elapsed 不相加。

| 负载 | store 模型 | gem5 elapsed (ms) | FastSim elapsed (ms) | 相对误差 |
|---|---|---:|---:|---:|
| zstd | legacy-prepare | 2.648757 | 2.465426 | -6.921% |
| zstd | bounded | 2.648757 | 2.412249 | -8.929% |
| flac | legacy-prepare | 0.670171 | 0.703282 | +4.941% |
| flac | bounded | 0.670171 | 0.703272 | +4.939% |
| graph500 | legacy-prepare | 3.411999 | 3.159253 | -7.408% |
| graph500 | bounded | 3.411999 | 3.050060 | -10.608% |

## 主要 PMU：退休 load cache 事件（暂定）

使用维护验证器的 retired_cache 命名空间，目标事件为 MEM_LOAD_RETIRED.L1_MISS/L2_MISS/L3_MISS。这些是退休 load 的服务层级计数，不可与旧 perf_l2_demand_misses（L2_RQSTS 请求）或 native tag miss 互换。可评分的两组负载所有行均为 incomplete-evidence，参考与模型的未知资格分别列出；FLAC 人口不匹配，拒绝评分。未知不能视为 hit 或 miss，也不能把暂定误差升级为完整参考精度。硬件 ISA 资格、服务事实、窗口边界和错误路径/预取对状态的间接作用仍未完全验证。

| 负载 | store 模型 | 退休事件 | gem5 已知计数 | FastSim 已知计数 | 暂定相对误差 | gem5 未知资格 | FastSim 未知资格 |
|---|---|---|---:|---:|---:|---:|---:|
| zstd | legacy-prepare | retired_l1_load_misses | 1,081,618 | 1,170,732 | +8.239% | 145,605 | 1,110,113 |
| zstd | legacy-prepare | retired_l2_load_misses | 304,345 | 336,619 | +10.604% | 387,747 | 1,110,169 |
| zstd | legacy-prepare | retired_l3_load_misses | 62,546 | 86,514 | +38.321% | 387,905 | 1,110,275 |
| zstd | bounded | retired_l1_load_misses | 1,081,618 | 1,170,180 | +8.188% | 145,605 | 1,110,122 |
| zstd | bounded | retired_l2_load_misses | 304,345 | 336,587 | +10.594% | 387,747 | 1,110,169 |
| zstd | bounded | retired_l3_load_misses | 62,546 | 86,514 | +38.321% | 387,905 | 1,110,273 |
| flac | legacy-prepare | retired_l1_load_misses | 不可评分 | 不可评分 | n/a | 人口不匹配 | 人口不匹配 |
| flac | legacy-prepare | retired_l2_load_misses | 不可评分 | 不可评分 | n/a | 人口不匹配 | 人口不匹配 |
| flac | legacy-prepare | retired_l3_load_misses | 不可评分 | 不可评分 | n/a | 人口不匹配 | 人口不匹配 |
| flac | bounded | retired_l1_load_misses | 不可评分 | 不可评分 | n/a | 人口不匹配 | 人口不匹配 |
| flac | bounded | retired_l2_load_misses | 不可评分 | 不可评分 | n/a | 人口不匹配 | 人口不匹配 |
| flac | bounded | retired_l3_load_misses | 不可评分 | 不可评分 | n/a | 人口不匹配 | 人口不匹配 |
| graph500 | legacy-prepare | retired_l1_load_misses | 713,716 | 713,897 | +0.025% | 2,004 | 9,352 |
| graph500 | legacy-prepare | retired_l2_load_misses | 356,253 | 356,237 | -0.004% | 4,179,613 | 9,364 |
| graph500 | legacy-prepare | retired_l3_load_misses | 355,360 | 355,447 | +0.024% | 4,179,631 | 9,399 |
| graph500 | bounded | retired_l1_load_misses | 713,716 | 713,898 | +0.026% | 2,004 | 9,352 |
| graph500 | bounded | retired_l2_load_misses | 356,253 | 356,241 | -0.003% | 4,179,613 | 9,364 |
| graph500 | bounded | retired_l3_load_misses | 355,360 | 355,447 | +0.024% | 4,179,631 | 9,399 |

Graph500 的 L2/L3 参考未知资格超过 400 万，显著大于已知 miss 数；Zstd 的 L3 参考未知资格也超过已知 miss 数。因此即便已知子集的误差很小，也不能据此宣布完整 L2/L3 PMU 准确。两负载的 FB hit 模型计数为零，L1 hit/FB hit 的分配仍有明显偏差，详见全指标表。

逐事件的暂定 MAPE、WAPE、max APE 和有效样本数由维护汇总器输出：[新模型汇总](../tmp/spec-c8-accuracy-20261022/final/bounded/retired-cache-summary.md)、[旧模型汇总](../tmp/spec-c8-accuracy-20261022/final/legacy-prepare/retired-cache-summary.md)。这些汇总仅针对不完整证据，不能混入完整参考的精度统计。

### 九项退休事件原始计数

| 负载 | store 模型 | 字段 | gem5 | FastSim | 暂定相对误差 | 参考未知 | 模型未知 |
|---|---|---|---:|---:|---:|---:|---:|
| c08-777.zstd_r | legacy-prepare | retired_l1_load_hits | 10,863,091 | 10,016,505 | -7.793% | 145,597 | 1,110,002 |
| c08-777.zstd_r | legacy-prepare | retired_l1_load_misses | 1,081,618 | 1,170,732 | +8.239% | 145,605 | 1,110,113 |
| c08-777.zstd_r | legacy-prepare | retired_l2_load_hits | 780,068 | 836,943 | +7.291% | 364,473 | 1,110,113 |
| c08-777.zstd_r | legacy-prepare | retired_l2_load_misses | 304,345 | 336,619 | +10.604% | 387,747 | 1,110,169 |
| c08-777.zstd_r | legacy-prepare | retired_l3_load_hits | 241,713 | 250,006 | +3.431% | 399,822 | 1,110,275 |
| c08-777.zstd_r | legacy-prepare | retired_l3_load_misses | 62,546 | 86,514 | +38.321% | 387,905 | 1,110,275 |
| c08-777.zstd_r | legacy-prepare | retired_load_fb_hits | 242,803 | 0 | -100.000% | 145,605 | 1,110,169 |
| c08-777.zstd_r | legacy-prepare | retired_loads | 12,221,055 | 12,221,055 | +0.000% | 120 | 0 |
| c08-777.zstd_r | legacy-prepare | retired_stores | 6,931,872 | 6,931,872 | +0.000% | 120 | 0 |
| c08-777.zstd_r | bounded | retired_l1_load_hits | 10,863,091 | 10,016,807 | -7.790% | 145,597 | 1,110,002 |
| c08-777.zstd_r | bounded | retired_l1_load_misses | 1,081,618 | 1,170,180 | +8.188% | 145,605 | 1,110,122 |
| c08-777.zstd_r | bounded | retired_l2_load_hits | 780,068 | 836,413 | +7.223% | 364,473 | 1,110,122 |
| c08-777.zstd_r | bounded | retired_l2_load_misses | 304,345 | 336,587 | +10.594% | 387,747 | 1,110,169 |
| c08-777.zstd_r | bounded | retired_l3_load_hits | 241,713 | 249,976 | +3.419% | 399,822 | 1,110,273 |
| c08-777.zstd_r | bounded | retired_l3_load_misses | 62,546 | 86,514 | +38.321% | 387,905 | 1,110,273 |
| c08-777.zstd_r | bounded | retired_load_fb_hits | 242,803 | 0 | -100.000% | 145,605 | 1,110,169 |
| c08-777.zstd_r | bounded | retired_loads | 12,221,055 | 12,221,055 | +0.000% | 120 | 0 |
| c08-777.zstd_r | bounded | retired_stores | 6,931,872 | 6,931,872 | +0.000% | 120 | 0 |
| c08-854.graph500_s | legacy-prepare | retired_l1_load_hits | 12,182,386 | 16,352,499 | +34.231% | 2,000 | 9,308 |
| c08-854.graph500_s | legacy-prepare | retired_l1_load_misses | 713,716 | 713,897 | +0.025% | 2,004 | 9,352 |
| c08-854.graph500_s | legacy-prepare | retired_l2_load_hits | 357,456 | 357,676 | +0.062% | 4,179,613 | 9,356 |
| c08-854.graph500_s | legacy-prepare | retired_l2_load_misses | 356,253 | 356,237 | -0.004% | 4,179,613 | 9,364 |
| c08-854.graph500_s | legacy-prepare | retired_l3_load_hits | 875 | 775 | -11.429% | 4,179,631 | 9,379 |
| c08-854.graph500_s | legacy-prepare | retired_l3_load_misses | 355,360 | 355,447 | +0.024% | 4,179,631 | 9,399 |
| c08-854.graph500_s | legacy-prepare | retired_load_fb_hits | 4,177,602 | 0 | -100.000% | 2,004 | 9,384 |
| c08-854.graph500_s | legacy-prepare | retired_loads | 17,075,672 | 17,075,672 | +0.000% | 32 | 0 |
| c08-854.graph500_s | legacy-prepare | retired_stores | 11,778 | 11,778 | +0.000% | 32 | 0 |
| c08-854.graph500_s | bounded | retired_l1_load_hits | 12,182,386 | 16,352,498 | +34.231% | 2,000 | 9,308 |
| c08-854.graph500_s | bounded | retired_l1_load_misses | 713,716 | 713,898 | +0.026% | 2,004 | 9,352 |
| c08-854.graph500_s | bounded | retired_l2_load_hits | 357,456 | 357,673 | +0.061% | 4,179,613 | 9,356 |
| c08-854.graph500_s | bounded | retired_l2_load_misses | 356,253 | 356,241 | -0.003% | 4,179,613 | 9,364 |
| c08-854.graph500_s | bounded | retired_l3_load_hits | 875 | 779 | -10.971% | 4,179,631 | 9,379 |
| c08-854.graph500_s | bounded | retired_l3_load_misses | 355,360 | 355,447 | +0.024% | 4,179,631 | 9,399 |
| c08-854.graph500_s | bounded | retired_load_fb_hits | 4,177,602 | 0 | -100.000% | 2,004 | 9,384 |
| c08-854.graph500_s | bounded | retired_loads | 17,075,672 | 17,075,672 | +0.000% | 32 | 0 |
| c08-854.graph500_s | bounded | retired_stores | 11,778 | 11,778 | +0.000% | 32 | 0 |

## 原生分支与 cache tag miss 诊断

参考仅使用原生 Ruby/SLICC 汇总及 task PMU，未使用 path-class cache 字段。下表是单独的原生诊断域，不作为真机 perf 退休 cache 指标。FLAC 原生来源门失败，保留为不可评分。

| 负载 | store 模型 | 字段 | gem5 | FastSim | 相对误差 |
|---|---|---|---:|---:|---:|
| c08-777.zstd_r | legacy-prepare | branch_misses | 343,171 | 342,023 | -0.335% |
| c08-777.zstd_r | legacy-prepare | l1d_tag_misses | 1,506,244 | 2,145,840 | +42.463% |
| c08-777.zstd_r | legacy-prepare | llc_tag_misses | 71,314 | 95,237 | +33.546% |
| c08-777.zstd_r | legacy-prepare | private_l2_tag_misses | 335,247 | 364,479 | +8.720% |
| c08-777.zstd_r | bounded | branch_misses | 343,171 | 342,023 | -0.335% |
| c08-777.zstd_r | bounded | l1d_tag_misses | 1,506,244 | 1,636,543 | +8.651% |
| c08-777.zstd_r | bounded | llc_tag_misses | 71,314 | 95,237 | +33.546% |
| c08-777.zstd_r | bounded | private_l2_tag_misses | 335,247 | 487,836 | +45.515% |
| c08-854.graph500_s | legacy-prepare | branch_misses | 1,571 | 1,327 | -15.532% |
| c08-854.graph500_s | legacy-prepare | l1d_tag_misses | 714,202 | 714,141 | -0.009% |
| c08-854.graph500_s | legacy-prepare | llc_tag_misses | 355,360 | 355,447 | +0.024% |
| c08-854.graph500_s | legacy-prepare | private_l2_tag_misses | 356,365 | 356,252 | -0.032% |
| c08-854.graph500_s | bounded | branch_misses | 1,571 | 1,327 | -15.532% |
| c08-854.graph500_s | bounded | l1d_tag_misses | 714,202 | 714,545 | +0.048% |
| c08-854.graph500_s | bounded | llc_tag_misses | 355,360 | 355,447 | +0.024% |
| c08-854.graph500_s | bounded | private_l2_tag_misses | 356,365 | 356,414 | +0.014% |

## 来源缺口与本轮报告修正

FLAC core0/owner1621 的原生 PMU 为 4,439,946 宏指令、7,881,399 UOP、293,560 分支；FST 审计为 4,439,947、7,881,405、293,561，差 1 宏指令、6 UOP、1 分支，memory UOP 一致。FST 最末记录为 canonical syscall marker。采集器声明 syscall 的 FST marker 在 first micro-op、原生宏指令在 last micro-op 计数；共同终点切进 syscall 的部分微操作时，完整 canonical 展开与原生已退休前缀不同。这是边界人口缺口，不能篡改原生计数或放宽门禁掩盖。现有时间合同使用功能流宏指令分母，匹配检查仍通过，故保留 CPI 计算值；FLAC 整项来源检查失败，不能据此宣称完全通过认证。

原验证器先检查 task 原生 PMU，再生成独立 cache 比较，导致 task 失败时无法进一步呈现独立 cache 合同的检查结果。本轮将独立 cache 比较移到 task PMU 检查前；错内容身份、错参与者和原语不守恒仍拒绝，task 来源失败仍令 case=failed、native PMU=source-gate-failed。没有改变模型、源计数、分母或预测值。FLAC 旧模型用 --source-root/--stats-dir 对冻结回放结果重评分，副本 SHA256 与原回放完全一致；重评分发现退休 cache 人口也不匹配：core0 physical/workload_task 为 4,439,947 对 4,439,946，user 为 4,309,281 对 4,309,280。退休 cache 的 UOP 人口一致，仍因宏人口不等而拒绝。不能借“允许不完整证据”跨越人口不匹配的硬门。

新旧模型的相同输入身份和相同人口差异已由维护 compare_retired_cache 再次核实：[flac-cache-population-rejection.json](../tmp/spec-c8-accuracy-20261022/reporting-fix/flac-cache-population-rejection.json)。确认同一硬失败后停止新模型重复的来源全扫描；六次原始回放与验证均已完成，新模型独立 cache 不发布比较值，不将被停止的重复审计称为通过。旧模型完整重评分失败报告：[validation.json](../tmp/spec-c8-accuracy-20261022/reporting-fix/validation/legacy-prepare/flac/cases/c08-817.flac_s/validation.json)。

其余两个负载的原生门通过仅证明来源/守恒检查通过，不证明达到精度阈值或真机硬件等价。旧十项 SPR perf-cache 请求/退休混合诊断仍保留在原报告及 CSV，FLAC 的该诊断未在本轮独立退休 cache 重评分中重算，不以新退休字段冒充旧请求字段。workload_task 的旧 L2_RQSTS/LLC 请求字段因归属资格不足为 null，不把未知归属当零，也不借用 physical scope 计数。

## 本轮吞吐量观测（补充）

以下从已完成的 bounded_analytic 回放输出提取，八核合计。计量范围为 user-plus-kernel 回放；主吞吐量分子是测量窗口内用户 UOP。批次按最多六路验证任务组织，FastSim 与 Python 来源审计存在重叠，没有逐次静默宿主环境认证；因此只作本轮并行条件下的观测，不能用于新旧模型的独立加速比。

| 负载 | 测量用户宏指令 | 测量用户 UOP | 测量时间 (s) | 功能预热 (s) | 回放合计 (s) | 用户宏指令 MIPS | 用户 MUOP/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| flac | 35,665,379 | 77,126,512 | 26.437 | 59.259 | 85.696 | 1.349 | 2.917 |
| graph500 | 39,797,487 | 79,593,563 | 20.868 | 7.361 | 28.229 | 1.907 | 3.814 |
| zstd | 41,463,378 | 78,550,027 | 20.831 | 6.752 | 27.582 | 1.990 | 3.771 |

三例用户 MUOP/s 分布：mean=3.501，P50=3.771，P90=3.806，P99=3.813，minimum=2.917（Type-7 分位数）。

MIPS=测量用户宏指令数/measurement_seconds/1e6；MUOP/s 使用 scope_metrics.throughput.user_uops_per_second。测量速度排除功能预热。回放合计包含功能预热，但不包含单列的输入身份计算（Zstd 57.271 s、Graph500 57.397 s、FLAC 235.215 s），也不包含外部 Python 来源审计；因此不是用户验证流程的完整耗时。原生预热没有权限域分解，end_to_end_user_uops_per_second 为 null，不构造端到端用户吞吐量。

[原始吞吐量提取与分布](../tmp/spec-c8-accuracy-20261022/final/throughput-observed.json)。

## 复现与验证

维护入口为 tools/run_native_kernel_fastsim_validation.py，原回放启用 --retired-cache --perf-cache --perf-cache-estimates --perf-cache-incomplete-errors；独立硬件身份使用 tools/validate_fs_oracle_identity.py。六份完整配置及命令分别保存于实验目录的 run-configs、case 回放日志和本表引用的验证报告。

数值明细：[pmu-counts.json](../tmp/spec-c8-accuracy-20261022/final/pmu-counts.json)、[pmu-counts.csv](../tmp/spec-c8-accuracy-20261022/final/pmu-counts.csv)。并行回放与 Python 来源审计存在宿主竞争，本轮不报告独立吞吐量测量。

代码构建和 fastsim_tests 通过。报告顺序修正另通过新回归及 native PMU、retired cache、task PMU 回归。回归保证失败不被掩盖及独立结果被保留，不构成硬件 PMU 精度认证。

| 负载 | store 模型 | 完整验证报告 |
|---|---|---|
| zstd | legacy-prepare | [validation.json](../tmp/spec-c8-accuracy-20261022/validation/legacy-prepare/zstd/cases/c08-777.zstd_r/validation.json) |
| zstd | bounded | [validation.json](../tmp/spec-c8-accuracy-20261022/validation/bounded/zstd/cases/c08-777.zstd_r/validation.json) |
| flac | legacy-prepare | [validation.json](../tmp/spec-c8-accuracy-20261022/validation/legacy-prepare/flac/cases/c08-817.flac_s/validation.json) |
| flac | bounded | [validation.json](../tmp/spec-c8-accuracy-20261022/validation/bounded/flac/cases/c08-817.flac_s/validation.json) |
| graph500 | legacy-prepare | [validation.json](../tmp/spec-c8-accuracy-20261022/validation/legacy-prepare/graph500/cases/c08-854.graph500_s/validation.json) |
| graph500 | bounded | [validation.json](../tmp/spec-c8-accuracy-20261022/validation/bounded/graph500/cases/c08-854.graph500_s/validation.json) |
