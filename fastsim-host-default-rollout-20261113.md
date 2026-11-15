# FastSim 宿主优化默认切换（2026-11-13）

已移除第三轮三个设计，恢复第一、二轮已验证优化，并将其安装为 `build/fastsim`。按用户要求停止重复实验，使用已有验证结果完成默认切换。

## 生效内容

- 移除 SPR 配置捕获/Sequencer 排序键、FR-FCFS 紧凑索引/FairEvent、静态指令 lookup cache；未确认收益的 borrowed views 也撤回。
- 保留输入预校验与连续缓存、翻译 GC、worker 反馈初始化、source-order 容器复用、第二轮 FR-FCFS 临时容器复用和反馈窄副本。
- `simulate` 默认预校验，通过显式参数传给输入加载器，不修改进程环境。
- SPR-M2 模型配置保持原样。PGO 仍为独立历史候选，未混入本次默认。
- 初次环境写入方案三组中位数下降 3.49%，已撤回；其源、ELF 和收据保留在 `initial-default/`。入口调整不构成已证明的回退根因。

## 已有验证与停止状态

最终候选通过九个相关测试程序、八种默认/关闭/覆盖/非法值用例，末尾损坏依赖在预校验阶段拒绝。Zstd 单次等价回放和两组完整 AB/BA 配对均为全模型字段零差异，包括 CPI、逐核人口与周期、模型 PMU、unknown/fallback 和有效模型配置。第三组因用户要求暂停而取消。

| 配对 | 第二轮净吞吐 Muser-UOP/s | 新默认净吞吐 Muser-UOP/s | 变化 |
|---|---:|---:|---:|
| 1 | 1.554779 | 1.550804 | -0.26% |
| 2 | 1.554458 | 1.558435 | +0.26% |

两组变化分别约 −0.26% 和 +0.26%，本批吞吐与第二轮相当；不宣称新的吞吐收益或 30 组稳定性认证。未将历史优化收益与本批数字相乘。

测量使用 CPU 52–63、socket/NUMA 1、performance；实际完整进程工作频率约 3.090 GHz，不是测量窗独立频率。主分母只用测量阶段并行墙钟。输入准备/预校验、初始化、前后完整性检查、warmup 与完整进程成本另存各 `.run.json` 及原始输出，不扣除 worker CPU 时间或嵌套计时。

此次直接安装已验证 ELF 与匹配静态库；安装后 SHA 与候选逐字节一致，769 个受保护输入文件与验证树相符。未重复根目录构建、测试或安装后的模型回放。

## 继承的精度证据

以下复用第二轮原报告的五负载 workload-task active CPI；本轮仅复跑 Zstd，其余四负载没有重跑。模型守恒不提升原生 PMU/硬件认证。

| 负载 | gem5 CPI | 冻结 M2 / 第二轮 CPI | CPI 绝对误差 cycles/macro | 相对误差 |
|---|---:|---:|---:|---:|
| Zstd | 1.206049 | 1.155342 | 0.050708 | -4.20% |
| XZ | 0.575297 | 0.604032 | 0.028735 | +4.99% |
| FLAC | 0.284584 | 0.293530 | 0.008946 | +3.14% |
| Graph500 | 0.966422 | 0.837365 | 0.129057 | -13.35% |
| Marian | 2.364997 | 3.044500 | 0.679503 | +28.73% |

历史五负载等权 CPI MAE 0.179390 cycles/macro，MAPE 10.89%。

## 使用与回退

普通 `build/fastsim simulate …` 默认启用受支持的二进制预校验。`--preload-validated-input false` 或 `FASTSIM_PRELOAD_VALIDATED=0` 恢复流式；CLI 优先于环境变量，非法值拒绝。通用库在环境变量未设置时保持流式。
core-functional、MMU-control、architectural-boundary 伴随文件仍不支持缓存，须显式流式回退，不能删除伴随字段。JSON 不走二进制缓存，策略日志不能单独证明 validation-excluded 吞吐。

- 默认 ELF SHA-256：`6a67a3aa5c18a7640d89e8d4b8c95d133cb9be79fe548c8835fd49e5fe1db341`。
- 冻结 M2 SHA-256：`0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`，保留于 `tmp/host-throughput-default-20261113/spr-m2-frozen`。
- GCC 11.5、Release -O3 -DNDEBUG、IPO ON、LTO 16、NATIVE OFF、无 fast-math/PGO。
- 未提交、未推送。

[完整交付与收据](fastsim-host-default-rollout-20261113.json)；[第二轮原报告](fastsim-host-throughput-phase2-20261113.md)；[三项设计消融](fastsim-host-throughput-ablation-20261113.md)。
