# TeaLeaf 四核同步 trace：FastSim CPI / PMU 验证结果（2026-10-12）

最终组合 CPI：gem5 **0.369387**，FastSim **0.418455**，绝对误差 **0.049068 cycles/宏指令**，相对误差 **+13.2836%**。
原生来源/守恒与逐核指令人口检查通过；private-L2 tag miss 相对低估约 25%，仍是本案最大的非零 PMU 相对误差。
这是一项单负载、单配置的 **user-plus-kernel 诊断实验**，不是硬件 PMU 认证，也不证明同步 consumer 的精度收益。

## 1. 冻结工作范围与磁盘预算

- 负载：811.tealeaf_s，SPEC test 输入 512×512；单进程、4 个协作 OpenMP 线程，PASSIVE wait policy。
- Linux x86-64 / GCC 11.5 / 静态非 PIE / 独立插桩 libgomp；保留功能同步 sidecar。
- gem5：BaseO3CPU，ROB192/IQ64/LQ32，buffer-114/SQ114，所有层 LRU；L1I/L1D 32KiB×8-way，private L2 1MiB×8-way，LLC 8×8MiB×16-way（64MiB 总量），2 个 DDR4 内存通道、3GiB。
- 实际 core clock 为 333 ticks，频率 3.003003003 GHz；FastSim 的 reference/core Hz 取 3003003003。
- 测量从原 solver WORKBEGIN marker 开始；首核达到 10M measured user-UOP 时四核共同结束，其他核保留实际人口，无补齐/裁剪。
- FastSim 使用 frozen maintained include chain，Q=1024；仅按本次配置给定几何、频率及 tick→cycle 换算，不按误差调参。该案不是 calibration 或 held-out generalization 测试。

最终 FST 合计 **15,404,784,480 B = 15.4048 GB = 14.3468 GiB**，低于 20GB。
首轮诊断原始 FST 为 2,637,110,688 B，已无损压缩为 52,751,533 B；解压流的 SHA256 与原 trace.json 逐核一致后才删除未压缩副本。
采集 watchdog 对单案和实验目录总 FST 使用 18GB 提前停止阈值，并检查磁盘剩余空间；本次均未触发。最终原始 FST 四文件、伴随映射和 oracle 留在项目 tmp 下。

## 2. 前史边界与功能同步验证

首轮使用原 solver-setup checkpoint，出现一个 KVM 中旧 region=42 的迟到 worker departure；其 begin 没被捕获。同步验证拒绝该案，没有删掉该事件或伪造初始 snapshot。
最终只在独立 main.c/diffuse.c 源副本中，将 KVM→O3 checkpoint 提前到 main 入口、首个 OpenMP team 之前，去掉旧 checkpoint；solver 的测量 marker 未改。原源树/已安装 runtime 未覆盖。
完整应用初始化及后续 solver 前史在 O3 下捕获，作为 record-bounded warmup 回放；测量重置后保留 cache/coherence/directory、branch、DTLB、DRAM 与响应状态。

同步门禁：**959 events / 54 regions / 53 barrier generations**；valid=true、complete=false，恰有 1 个共同终点截断的 region。初始缺口没有通过。
当前 FastSim 不消费 functional-sync sidecar；回放不使用观察 oracle 的 commit ticks。两轮采集的 warmup/驻留状态不同，不能把结果变化解释为同步建模收益。

| 核 | warmup records | measured user-UOP | measured mixed-UOP | measured 宏指令 |
|---|---:|---:|---:|---:|
| 0 | 40,993,812 | 9,696,987 | 9,741,987 | 6,273,665 |
| 1 | 39,188,959 | 10,000,001 | 10,046,103 | 6,442,683 |
| 2 | 80,328,304 | 9,574,867 | 9,620,740 | 6,193,517 |
| 3 | 41,037,300 | 9,692,935 | 9,741,582 | 6,273,558 |

总 warmup records=201,548,375；总 measured user-UOP=38,964,790；mixed-UOP=39,150,412。
核 1 实际达 10,000,001 user-UOP（跨阈值的原始记录保留），不是强制精确裁到 10M。common-end 与原生 committed-response drain 均验证通过。

## 3. CPI：active cycles / 各模拟器自己的宏指令分母

gem5 active cycles 排除 idle、blocked wall time 和 unknown kernel；unknown-zero 门禁通过。FastSim 使用 scope_metrics。两边各自计算分母，并核对逐核人口；不把 gem5 cycles/分母注入模型。

| 范围 | gem5 CPI | FastSim CPI | 绝对误差 cycles/宏指令 | 相对误差 | 宏指令人口匹配 |
|---|---:|---:|---:|---:|---|
| core 0 | 0.371032 | 0.414442 | 0.043410 | +11.6998% | 是 |
| core 1 | 0.359976 | 0.428199 | 0.068223 | +18.9520% | 是 |
| core 2 | 0.375836 | 0.416265 | 0.040429 | +10.7571% | 是 |
| core 3 | 0.371040 | 0.414624 | 0.043584 | +11.7464% | 是 |
| 四核组合（sum cycles / sum instructions） | 0.369387 | 0.418455 | 0.049068 | +13.2836% | 是 |

gem5 active cycles=9,302,434；FastSim cycles=10,538,134；两边组合宏指令均为 25,183,423。

| 配置数 | CPI MAE cycles/宏指令 | MAPE | P50 / P90 / P99 APE | WAPE | signed bias |
|---:|---:|---:|---|---:|---:|
| 1 | 0.049068 | 13.2836% | 13.2836% / 13.2836% / 13.2836% | 13.2836% | +13.2836% |

单配置的百分位等于该案 APE，不能作为跨负载分布。单独 user scope 未运行，不由组合 scope 拆出或复制 PMU。

## 4. PMU：gem5 原生语义诊断

cache 真值仅由维护验证器从 conserved Ruby/SLICC native-summary-core*.json 组合非 idle scope；branch 为原始 retired BPred redirect。没有使用 taotrace-path-class-v3 cache 字段。
事件定义与分类见 [PMU event dictionary](../configs/pmu-event-dictionary-v1.json)。当前 branch/L1D/private-L2 mapping 为 proxy，LLC 为 diagnostic；来源守恒通过不会提升为 strict hardware PMU。

| 事件 | gem5 | FastSim | signed Δ | 绝对计数误差 | 相对误差 | APE | mapping |
|---|---:|---:|---:|---:|---:|---:|---|
| branch_misses | 3,900 | 3,763 | -137 | 137 | -3.5128% | 3.5128% | proxy |
| l1d_tag_misses | 500,084 | 488,063 | -12,021 | 12,021 | -2.4038% | 2.4038% | proxy |
| private_l2_tag_misses | 268,644 | 201,419 | -67,225 | 67,225 | -25.0238% | 25.0238% | proxy |
| llc_tag_misses | 0 | 0 | +0 | 0 | —（0/0） | 0.0000% | diagnostic |

按契约 0/0 计 APE=0；LLC WAPE/bias 分母为零，记 N/A。**本窗口没有 LLC tag-miss 样本，不能用 0/0 证明 LLC 准确性**。这一规模和全 warmup 下，所测窗口未触发 LLC tag-miss；CPI、branch、L1D、L2 均有非零计数，但大工作集 LLC 行为仍未覆盖。

| 事件 | cases / finite-APE cases | MAPE | P50 / P90 / P99 APE | WAPE | signed bias |
|---|---|---:|---|---:|---:|
| branch_misses | 1 / 1 | 3.5128% | 3.5128% / 3.5128% / 3.5128% | 3.5128% | -3.5128% |
| l1d_tag_misses | 1 / 1 | 2.4038% | 2.4038% / 2.4038% / 2.4038% | 2.4038% | -2.4038% |
| private_l2_tag_misses | 1 / 1 | 25.0238% | 25.0238% / 25.0238% / 25.0238% | 25.0238% | -25.0238% |
| llc_tag_misses | 1 / 1 | 0.0000% | 0.0000% / 0.0000% / 0.0000% | N/A | N/A |

## 5. 门禁、身份和模型限制

- 最终 case set 固定为 accepted-source 下唯一一案；native validator discovered/passed/scored=1，failed/pending=0，23 项来源/守恒检查全通过；四核 user/kernel UOP 和宏指令人口匹配，LLC outcome 守恒，synthetic syscall/page-fault/IRQ 为零。
- validate_fs_oracle_identity.py 对最终配置派生的 effective-target/profile 验证 valid=true，4 个参与核完整；这不等于原运行时 profile 已全量认证，也不等于精度准入。
- 原始 collector launch profile 保存在 case/uarch_profile.json，最终 config-derived profile 在 tao_trace/uarch_profile.json；两者 SHA256 与差异保存在 profile-provenance.json。cache/TLB geometry 相同，原始频率 3 GHz 与实际 333-tick 的 3.003003003 GHz 不同。后生成的文件没有改写原始运行时证据。
- FastSim 使用 frozen include chain；几何及频率对齐，active DDR tick 转换取 ceil：tCL/tRCD/tRP=ceil(14160/333)=43，burst=ceil(3332/333)=11，front/backend=ceil(10000/333)=31。
- 附加 audit_fs_profile_identity.py 返回 FAIL（118/129）：8 项 DDR command/ACT 约束仍按维护默认关闭；另 3 项 checker 使用历史硬编码 burst=10、front/backend=30，未按当前原始 tick 做 ceil。保留失败报告，不能声称完整参数/状态机等价。未为过门禁开启已经失败的候选机制。
- Cache/network 仍是 compact approximation：L1D hit=mandatory queue 1+O3 readiness 1，L2 hit 再加 L0 request 2 与 L1 response 2；LLC compact service=12、directory=18、fill=8 为维护的折叠模型，非逐 transaction 认证。继承的 noc_one_way_latency=5 仅标为维护近似。SimpleNetwork::makeExtInLink 直接接队列，不应用 external ingress latency；直接两 switch 路径的 source routing/internal link/destination routing/external output 四条显式边各 1，controller 入队/准入另算，不能称精确网络下界为 5。旧审计文字称 Garnet 不适用于本案。
- ITLB/wrong-path occupancy 未完整建模，DTLB 为 timing_walk/12 固定服务，部分 DRAM command calendar 关闭；buffer 几何相同不等于完整 Ruby transient/lifecycle 等价。
- core0/1/3 warmup 经多个 CR3，producer 按设计抑制无 ASID 的 PC-only .imap v2，仅 core2 导出；未跨核复制或伪造映射。静态 instruction span / ordering facts 的覆盖受限。
- 附加严格 syscall entry-coverage gate 失败：483 条 syscall 元数据没有 arguments（thread_id 也不提供）。宽松结构审计无矛盾、return 缺口为零；参数缺失未补造。native 指令回放关闭 synthetic syscall/event/page-fault 模型，因此这里报告原生 CPL 周期与事件计数诊断，不认证完整 syscall semantic trace。
- 未接入同步 consumer，没有同步干预前后精度实验；不由单案误差归因 barrier/调度。未测 quiet-host 重复吞吐，不报告 throughput gain。

## 6. 证据与复现

所有实验数据保存在 `tmp/tealeaf-4c-sync-accuracy-20261012/`，未纳入 Git 的 bulk artifacts。关键入口：

- [最终 trace metadata](../tmp/tealeaf-4c-sync-accuracy-20261012/accepted-source/04c-tealeaf-test-10m-entry/tao_trace/trace.json)
- [common-end audit](../tmp/tealeaf-4c-sync-accuracy-20261012/accepted-source/04c-tealeaf-test-10m-entry/common-end-audit.json)
- [同步验证](../tmp/tealeaf-4c-sync-accuracy-20261012/sync-entry-validation.json)
- [effective target identity](../tmp/tealeaf-4c-sync-accuracy-20261012/identity-entry.json)
- [原始/生成 profile provenance](../tmp/tealeaf-4c-sync-accuracy-20261012/accepted-source/04c-tealeaf-test-10m-entry/profile-provenance.json)
- [FastSim/native 完整 validation](../tmp/tealeaf-4c-sync-accuracy-20261012/replay-entry/cases/04c-tealeaf-test-10m-entry/validation.json)
- [预测 JSON](../tmp/tealeaf-4c-sync-accuracy-20261012/replay-entry/cases/04c-tealeaf-test-10m-entry/fastsim.json)
- [维护 summary JSON](../tmp/tealeaf-4c-sync-accuracy-20261012/replay-entry/summary.json) / [CSV](../tmp/tealeaf-4c-sync-accuracy-20261012/replay-entry/summary.csv) / [Markdown](../tmp/tealeaf-4c-sync-accuracy-20261012/replay-entry/summary.md)
- [配置](../tmp/tealeaf-4c-sync-accuracy-20261012/fastsim-entry.cfg)，include chain 各文件的哈希与 simulator 哈希由 validation fingerprint 保存。
- [独立 workload 构建清单](../tmp/tealeaf-4c-sync-accuracy-20261012/entry-build/build-manifest.json) / [checkpoint 增量 patch](../tmp/tealeaf-4c-sync-accuracy-20261012/entry-build/entry-capture.patch)
- [采集命令/退出/空间 receipt](../tmp/tealeaf-4c-sync-accuracy-20261012/collection-entry-receipt.json) / [首轮无损归档 receipt](../tmp/tealeaf-4c-sync-accuracy-20261012/initial-fst-archive.json)
- [参数审计失败报告](../tmp/tealeaf-4c-sync-accuracy-20261012/fastsim-profile-entry/summary.md) / [严格 syscall gate 失败](../tmp/tealeaf-4c-sync-accuracy-20261012/syscall-metadata-entry.log) / [诊断覆盖数据](../tmp/tealeaf-4c-sync-accuracy-20261012/syscall-metadata-entry-diagnostic.json)

```bash
python3 tools/validate_functional_sync.py \
  --trace-dir tmp/tealeaf-4c-sync-accuracy-20261012/accepted-source/04c-tealeaf-test-10m-entry/tao_trace \
  --allow-truncated --output tmp/tealeaf-4c-sync-accuracy-20261012/sync-entry-validation.json
python3 tools/validate_fs_oracle_identity.py \
  --result tmp/tealeaf-4c-sync-accuracy-20261012/accepted-source/04c-tealeaf-test-10m-entry \
  --output tmp/tealeaf-4c-sync-accuracy-20261012/identity-entry.json
LD_LIBRARY_PATH=/opt/gcc-11/lib64 python3 tools/run_native_kernel_fastsim_validation.py \
  --source-root tmp/tealeaf-4c-sync-accuracy-20261012/accepted-source \
  --fastsim build/fastsim --config tmp/tealeaf-4c-sync-accuracy-20261012/fastsim-entry.cfg \
  --output-dir tmp/tealeaf-4c-sync-accuracy-20261012/replay-entry \
  --expected-cases 1 --jobs 1
```

Collector 的实际命令在 receipt 中；run-local prepare_case.py 仅组 record-bounded manifest/metadata，调用维护 merger 和 common-end checker，不重写原始 FST、不自选 cache 参考。

本轮修正 generate_fs_effective_target.py 的无效 LLC-bank/directory 数量耦合：stdlib 从 num_l3_banks 与 get_mem_ports 独立构造，两通道配八 LLC bank 是合法配置。新增两项 CLI 正负测试 RED→GREEN；directory/channel mismatch 仍拒绝。
默认 FastSim build/C++ tests 通过；全量 Python 225 项，3 errors、5 skipped、0 failures：test_python_bindings 缺 fastsim_py；两个 test_uarch_common_end_collection checkpoint-recovery 测试缺旧 TCSim wrapper。见 [Python 日志](../tmp/tealeaf-4c-sync-accuracy-20261012/python-tests.log)。
