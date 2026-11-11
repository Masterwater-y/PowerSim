# SPR-M1 — Retired Prepare 基线

2026-11-11，用户指定的第一个 SPR 评测里程碑。简称 **SPR-M1**；
后续模型里程碑依次使用 SPR-M2、SPR-M3。名称由目标平台、里程碑序号和
核心 store 行为组成，旧称 R0/R1 `v2-wait` 保留为实现来源。

## 固定身份

| 项目 | 身份 |
|---|---|
| 主程序 | `build/fastsim`，冻结 R0/R1 `v2-wait` |
| 二进制 SHA-256 | `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905` |
| 不可改写的里程碑配置 | `configs/spr-m1-retired-prepare.cfg` |
| 配置 SHA-256 | `711561de8cbb306fcad72ae76b4ed7ab0e3ebb30601263b7467cce86519b310f` |
| 后续评测默认入口 | `configs/spr-evaluation-default.cfg` |
| 原有 SPR 入口 | `configs/gem5-spr-retired.cfg`，现指向相同默认配置 |
| 版本与来源清单 | `configs/spr-m1-retired-prepare.json` |
| 命名二进制副本 | `tmp/spr-m1-20261111/fastsim`，独立复制、只读且可执行 |

里程碑绑定二进制与有效配置，不能只用工作区 Git HEAD 标识。
当前命名没有创建 Git tag、提交或推送。

## 后续评测的共同约定

| 项目 | 默认行为 |
|---|---|
| 输入 | 完整退休功能指令流，保持现有并行两阶段执行 |
| 统一父服务 | `core.unified_parent_service = v2-wait` |
| 退休 store prepare | `core.legacy_prepare_timing = true`，保留普通 store 权限获取 |
| 退休前 early prepare | native `sprEarlyStorePrepare = false` |
| 硬件预取 | native L1D 与 private-L2 controller 的 `enable_prefetch = false` |
| FastSim 预取扩展 | L1/L2 预取服务、follower、共享预取、LLC resident 迁移与 early ack 均关闭 |
| 软件预取指令 | 保留原功能指令人口，不删除 ISA hint 指令 |
| 已否决实验候选 | T1/T2/I1/B1/B2 不进入本里程碑 |
| 参考范围 | 默认 workload-task active CPI 与三项退休 load miss |

“有 prepare、无预取”指上述精确定义。关闭的是退休前 early prepare 与自动
硬件预取，不会移除正常 store 的权限事务，也不会删除退休流中的软件预取指令。

原五例为 C4、32GiB、500K warmup、首核 10M user-UOP 共同采集终点。
本配置包含原五例共有的 310 项有效参数，没有引用 `tmp/` 中的配置文件；
唯一不固化的是逐负载的 `thread_state.plan`。
新的采集必须用自身 `config.ini` / `effective-target.json` 覆盖硬件几何、
时钟与时长映射，并绑定自身线程状态 plan。不能将另一次采集的 plan 或 oracle
带入新负载，也不能用 FastSim 的关闭开关假装已经改变 native 参考。

## 已落实的默认入口

- `configs/spr-evaluation-default.cfg` 指向固定的 SPR-M1 配置。
- `configs/gem5-spr-retired.cfg` 使用同一入口。
- `tools/run_uarch_fastsim.py` 和 `scripts/launch_spec2026_uarch_exploration_v2.sh`
  的默认评测配置改为该入口；显式 `--config` / `FASTSIM_CONFIG` 仍可用于对照实验。
- `gem5-spr/configs/fastsim/fs_capture.py` 的 native 采集默认关闭硬件预取和
  early prepare，使用与原成功五案相同的 controller/CPU 设置位置。
  `--hardware-prefetch` / `--early-store-prepare` 是显式消融开关；
  `--no-hardware-prefetch --no-early-store-prepare` 可明确写出里程碑条件。
  新采集输出 `spr-evaluation-policy.json`，实际 `config.ini` 仍是验收依据。
- `AGENTS.md` 与 `docs/codebase-workflow.md` 已记录后续评测约定。

历史通用 `gem5-fs-native-kernel.cfg` 和旧版本配置仍用于明确的历史对照。
后续 SPR 评测不再把这些文件当作隐含默认；调用旧脚本时也必须显式选择
`spr-evaluation-default.cfg`。推进新里程碑时保留 SPR-M1 不变，再更新默认别名。

## 验证与已有精度

五案原配置的 310 项共有参数与新里程碑配置逐项一致，差异均为零。
固定配置、默认别名与 SPR 入口均通过真正的 C++ 配置解析器与约束校验。
标准 `fastsim_tests` 通过；native 默认关闭、显式消融、四核/八核人口及
缺 controller 拒绝逻辑已检查，未进行新的 gem5 采集。

二进制与命名副本哈希一致。本次固化配置没有改变模型预测，没有重跑
native 精度评分或测量吞吐。继承原五案 CPI MAE **0.191599 cycles/macro**、
MAPE **12.994957%**；原 PMU unknown 与硬件等价状态保留。

- [已有完整 CPI/PMU 结果](file:///data00/yinhaolang/FastSim/fastsim-r01-retired-cache-miss-errors-including-unknown-20261111.md)
- [原恢复与同输入验证](file:///data00/yinhaolang/FastSim/fastsim-r01-main-restoration-validation-20261111.md)
- [里程碑与逐案配置身份清单](file:///data00/yinhaolang/FastSim/configs/spr-m1-retired-prepare.json)
- [配置解析检查](file:///data00/yinhaolang/FastSim/tmp/spr-m1-20261111/check-config.log)
- [标准测试日志](file:///data00/yinhaolang/FastSim/tmp/spr-m1-20261111/fastsim_tests.log)
