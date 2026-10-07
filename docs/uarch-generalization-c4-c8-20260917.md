# FastSim C4/C8 微架构泛化离线采集计划

日期：2026-09-17。实验设计来自本次确认的四参数、四负载、C4/C8 全覆盖方案。

运行状态补充：本轮在 2026-09-17 02:17:43 因 smoke 失败停止，检查点准备
136/136、smoke 34 通过/102 失败，正式采集未开始。已确认空测量核 CPL 关闭
元数据遗漏、TeaLeaf C8 测量页表快照的 Ruby 读瞬态缺口，以及被前置检查遮住的
Stockfish J4 C8 unknown-kernel 门禁失败。修复前不要直接重启期待自动恢复；
证据、定向复现和恢复约束见
[失败排查记录](uarch-smoke-failure-analysis-20260917.md)。

## 1. 实验规模与目标

固定其他硬件参数，改变 L1D、私有 L2、共享 L3/LLC 容量及 ROB entries，验证
FastSim 对单参数变化和联合配置的预测。每个微架构配置同时采集 C4 与 C8。

| 配置类型 | 微架构配置数 | 含 C4/C8 的硬件组合数 | 四负载 case 数 |
|---|---:|---:|---:|
| 基准 | 1 | 2 | 8 |
| 单变量端点 | 8 | 16 | 64 |
| 四变量联合配置 | 8 | 16 | 64 |
| 合计 | **17** | **34** | **136** |

正式采集共 136 个 case、816 条 per-core FST。全矩阵 smoke 另有 136 个短采集
case、816 条 FST；smoke 不增加硬件组合数，也不并入正式结果。

三水平全因子需要 `3^4 × 2 × 4 = 648` 个 case；本设计减少约 79% 的正式 case。
它检查所选范围的配置泛化，不能单独支撑所有工作负载或所有二阶交互的泛化结论。

## 2. 参数矩阵

L1 明确指 **L1D**，L1I 固定为 32 KiB。L2 为每核私有容量；L3 为共享总容量。
机器可读矩阵：`configs/spec2026-uarch-generalization-c4-c8-v3.json`。

| ID | L1D KiB | L2 KiB/core | LLC MiB 总量 | ROB | 类型 |
|---|---:|---:|---:|---:|---|
| baseline | 32 | 1024 | 64 | 192 | 基准 |
| l1d16k8 | 16 | 1024 | 64 | 192 | 单变量 |
| l1d64k8 | 64 | 1024 | 64 | 192 | 单变量 |
| l2_512k8 | 32 | 512 | 64 | 192 | 单变量 |
| l2_2m8 | 32 | 2048 | 64 | 192 | 单变量 |
| llc32m | 32 | 1024 | 32 | 192 | 单变量 |
| llc128m | 32 | 1024 | 128 | 192 | 单变量 |
| rob96 | 32 | 1024 | 64 | 96 | 单变量 |
| rob256 | 32 | 1024 | 64 | 256 | 单变量 |
| j1_small_all | 16 | 512 | 32 | 96 | 联合 |
| j2 | 16 | 512 | 128 | 256 | 联合 |
| j3 | 16 | 2048 | 32 | 256 | 联合 |
| j4 | 16 | 2048 | 128 | 96 | 联合 |
| j5 | 64 | 512 | 32 | 256 | 联合 |
| j6 | 64 | 512 | 128 | 96 | 联合 |
| j7 | 64 | 2048 | 32 | 96 | 联合 |
| j8_large_all | 64 | 2048 | 128 | 256 | 联合 |

联合点采用 `D = A×B×C` 的 8 点半分数设计，A/B/C/D 分别为 L1D/L2/LLC/ROB
的小、大档（编码 −1/+1）。每两个参数的四种大小搭配均出现两次。联合点自身的
二阶交互存在混叠；本轮用来验证联合配置预测，不据此独立估计全部交互系数。
设计限制见 [NIST 对分辨率 IV 的说明](https://www.itl.nist.gov/div898/handbook/pri/section3/pri3324.htm)。

固定项：3 GHz、MESI_Three_Level、3 GiB DRAM、8 个内存通道、8 个 LLC bank，
L1D/L2 为 8-way，LLC 为 16-way，cache line 为 64 B。流水宽度、IQ、LSQ、缓存
延迟、预取及替换策略保持同一基准。实际身份从最终 `config.ini` 核对。

gem5 的 `--l3-size` 是 **每 bank** 容量：总 LLC 32/64/128 MiB 对应 4/8/16 MiB；
FastSim 的 `cache.llc.size` 是总容量。不得同时改变相联度来实现小容量档。

## 3. 负载与代表性

| Workload | 候选覆盖 | 重点 |
|---|---|---|
| 706.stockfish_r | 分支密集整数搜索 | ROB、控制流及弱缓存敏感场景 |
| 731.astcenc_r | 媒体编码、计算与访存混合 | ROB、L1D/L2 |
| 811.tealeaf_s | 稀疏迭代、内存访问 | 缓存与 ROB 联合作用 |
| 854.graph500_s | 不规则图访问、共享内存 | LLC、访存并行与共享压力 |

复用现有二进制、输入和镜像映射，线程数随 C4/C8 设置；固定每种工作负载的输入和
命令。名称不能代替敏感性证据：每个参数最终应至少在两个负载上出现可辨识的
gem5 性能或原生事件变化。没有激励的维度必须标明覆盖不足，不能按 FastSim 误差
挑选或剔除 case。已有负载及配置曾参与历史诊断，不自动称为 workload-held-out；
新联合点的留出身份需按实际调参记录确认，模型在正式比较前冻结。

## 4. 采集与数据合同

以 [accuracy-reporting-contract.md](accuracy-reporting-contract.md) 和
[共同终点说明](first-core-common-end-collection-20260911.md) 为准。

1. 每个 `(profile, cores, workload)` 独立创建/复用身份一致的 ROI checkpoint，
   从该配置的 O3/Ruby 功能预热开始采集，保留 user 与 kernel records。
2. 预热记录建立状态但不计入测量；正式测量以最快核达到 **10M user UOP** 触发
   全部参与核共同结束，即 `first-core-target-common-end-v1`。慢核保留实际计数。
3. smoke 目标为 10K user UOP，仍使用相同预热和共同终点。它只检验链路与配置，
   不承担性能代表性判断。
4. 原生 gem5 FST 直接落盘，不启用逐 UOP JSONL、wrong-path oracle 或大量
   native-response 调试日志。保存完整 `.deps` 及存在的 `.imap/.vmap/.asmap`。
5. 使用 `collect_common_end_fst.finalize_capture` 整理 FST/边界；使用
   `validate_fs_oracle_identity.validate_result_identity` 检查目标身份；使用
   `audit_p1_native_response_sideband.audit_result` 检查原生来源、层级守恒和 drain。
   缺失原生证据、缺失参与核或口径不符都不能标记完成。
6. 每个配置有自己的 FST 和 oracle。采集结果使用实际分母，不把基准 FST 复制给
   其他配置，不把 gem5 时序、命中结果或 PMU 作为 FastSim 推理输入。

旧 `collect_spec2026_uarch_fs.py` 的 `all-core` 执行路径及旧 materializer 不用于
本轮正式采集；新入口只复用其配置换算/静态预检，并独立调用共同终点整理器。

## 5. 并发、磁盘与恢复

采样并发自动取以下最小值：case 数、当前 CPU affinity 可用逻辑核数，以及
`floor((MemAvailable − 64 GiB) / 12 GiB)`。本机检查时有 192 个逻辑核、约
1.9 TiB 可用内存，默认可开 **136 路 gem5 采样**。12 GiB 是规划预算，不是进程
内存硬限制；资源变化时脚本会重新计算。

KVM 检查点创建默认限制为 `min(采样并发, floor(逻辑核数/8))`，本机为 **24 路**，
覆盖约 192 个 guest vCPU；整理/哈希默认最多 8 路。三个阶段分别调度，避免
检查点创建的 vCPU 扩张和同时哈希阻塞全部采样。

按已有文件实际占用逐负载估算，正式数据约 684 GiB（0.67 TiB），加 smoke、
转换/重试和 checkpoint 的基础峰值估计约 0.90 TiB；建议预留 **1.2–1.5 TiB**。
ASTCENC 的估算样本来自旧批次，联合配置与预热可能改变记录量；这是工程预算，
不是上限。全矩阵 smoke 保留完整预热，空间不能简单按 `10K/10M` 缩放。
脚本默认按 1.5 TiB 新增预算预检，并在运行中保留至少约 1.2 TiB
可用空间；低于停止线会终止本次任务子进程并保留已完成数据。

首次运行自动经过预检、检查点准备、全量 smoke、全量正式采集、全量完成审计。
只在全部 136 个正式 case 通过数据门禁后写入完成状态。重启同一个输出目录会
核对冻结输入/参数和已有产物，跳过有效完成项，保留失败现场后重试未完成项。
不会因目录或旧 `complete` 字样存在就跳过校验，也不会自动删除历史实验。

首次整理为 FST 和伴随文件生成 SHA-256。恢复时核对全部产物的存在性、大小和
修改时间，对小文件重算哈希，并重跑身份/共同终点/原生门禁；大文件不反复全量
哈希。镜像内容 SHA-256 由现有检查点准备器记录。采集期间保持源代码、配置、
负载和镜像不变；阶段前后会检查冻结输入，变化会使该轮无法标记完成。

## 6. 用户运行入口

从仓库根目录执行：

```bash
bash scripts/collect_uarch_c4_c8_offline.sh preflight
bash scripts/collect_uarch_c4_c8_offline.sh start
bash scripts/collect_uarch_c4_c8_offline.sh status
```

`start` 脱离当前终端运行；日志和 PID 保存在输出根目录。前台运行、停止、以及
显式指定采样并发：

```bash
bash scripts/collect_uarch_c4_c8_offline.sh run
bash scripts/collect_uarch_c4_c8_offline.sh stop
bash scripts/collect_uarch_c4_c8_offline.sh start --jobs 136
```

跟踪进度可运行 `tail -f tmp/uarch-generalization-c4-c8-20260917/collector.log`。
停止或失败后，再执行相同的 `start` 即可恢复。默认每个检查点超时 4 小时、每个
采样超时 24 小时、每个整理任务超时 2 小时；只有已知 Ruby functional-read
暂态错误自动重试最多 2 次，其他失败保留日志并阻止进入下一阶段。

默认输出目录为 `tmp/uarch-generalization-c4-c8-20260917/`，可用 `--output` 指定
一个新目录；后续 `status/stop/start` 传入相同 `--output`。用 `--help` 查看资源、
超时和并发参数。系统 `python3` 可能是 3.7；入口优先使用本机已安装的
`/data00/yinhaolang/infer/.venv/bin/python`，不在线安装依赖。

## 7. 产物与后续评估

- `matrix.json`、`run-contract.json`：冻结实验定义、文件身份和运行参数。
- `preflight.json`：136 个 case、资源并发与磁盘预检结果。
- `checkpoints/`、`preparation/`：ROI checkpoint 和创建记录。
- `smoke/source/<case_id>/`：短采集与数据门禁。
- `formal/source/<case_id>/`：该配置自己的 `request.json`、`config.ini`、
  `effective-target.json`、`tao_trace/`、`oracle/`、命令和审计。
- `formal/inventory.json`、`formal/collection-finished.json`：全部正式 case 的
  状态与输入位置。只有完整集合全部通过才给出 `complete`。
- 每个 case 的 `fastsim.cfg` 和 `replay-command.json`：后续按该配置回放的入口。

本脚本交付离线输入和参考数据，不在高并发采集时测量 FastSim 吞吐。后续对每个
case 使用对应配置回放，再通过维护的 `run_native_kernel_fastsim_validation.py`
的 `--source-root/--stats-dir` 模式评分。不得用统一基准配置回放全部变体。

报告分别展示 C4/C8、单变量/联合配置、各负载的 CPI MAPE、**CPI MAE**、方向和
排序准确率、相对基准 CPI 比值误差。逐 case 同时给出相对误差和
`abs(FastSim CPI − gem5 CPI)`，单位 cycles/macro instruction。CPI、PMU 的
scope 与实际分母必须匹配；PMU 只用原生 Ruby/SLICC 来源，保留 proxy/diagnostic
分类。微小变化阈值在看 FastSim 误差前冻结。历史指标或失败点不混入正式汇总。

## 8. 交付验证

2026-09-17 已完成宿主机只读预检：192 个 affinity CPU、约 1,910 GiB 可用内存、
4,590 GiB 可用磁盘，对应采样/准备/整理并发 **136/24/8**。脚本运行时会重新计算。

18 项采集器测试、4 项共同终点测试、16 项原生 sideband 测试通过；默认构建和
`./build/fastsim_tests` 通过。另以保留语料中的 Stockfish、TeaLeaf、Graph500
各 C4/C8 共 6 个 case 只读验证了新数据门禁。后台启动与锁测试使用故意损坏的
矩阵，在进入 gem5 前失败，未启动新实验。新矩阵的真实 smoke 和正式采集由用户
运行入口执行；本次不宣称新 136 个配置已经采集通过。
