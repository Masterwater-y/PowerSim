# archsim

`archsim` 是 MineSim/CounterPoint/Sniper 的可复现实验环境。仓库只保存本项目源码、配置、脚本、文档和小型补丁；DynamoRIO、Sniper 以及 Sniper 的依赖在部署时下载和编译，不提交完整第三方目录。

新机器部署说明见：[README_DEPLOY_NEW_MACHINE.md](file:///data00/yinhaolang/simulators/archsim_git/README_DEPLOY_NEW_MACHINE.md)。

## 一键部署

```bash
git clone <repo-url> archsim
cd archsim
./scripts/bootstrap.sh
source ./env.sh
```

`bootstrap.sh` 会执行：

- 下载并安装 DynamoRIO 到 `dynamorio/`
- 下载 Sniper 到 `snipersim/`，checkout 固定 ref，并应用 `patches/snipersim/`
- 编译 MineSim
- 编译 `workloads/` 下的 microbench
- 生成本机环境文件 `env.sh`

默认版本可用环境变量覆盖：

```bash
DYNAMORIO_REF=release_9.0.1 SNIPER_REF=9ccff91 ./scripts/bootstrap.sh
```

## 验证闭环

先跑 smoke，确认部署链路可用：

```bash
./scripts/verify_full_loop.sh --smoke
```

再跑完整默认 5-workload 闭环：

```bash
./scripts/verify_full_loop.sh --full
```

验证脚本会完成：

- DynamoRIO `drmemtrace` 采集
- MineSim 仿真
- CounterPoint 约束检查和问题组件排序
- Sniper 仿真
- MineSim/Sniper/PMU 三方误差比较

结果默认写到：

```text
global/out/verify_smoke
global/out/verify_full_loop
```

验证脚本默认给 DynamoRIO 采集阶段设置 `DR_TIMEOUT_SECONDS=300`，避免异常运行环境中无限挂起；在性能较慢的机器上可以调大：

```bash
DR_TIMEOUT_SECONDS=1200 ./scripts/verify_full_loop.sh --full
```

注意：DynamoRIO 和 Sniper/SDE 都需要能够注入/检查目标进程。若在带强隔离的 sandbox、bubblewrap、受限容器或禁用相关权限的环境中运行，可能出现 `drrun` 长时间不退出、`Pin readlink:: Permission denied` 或 SDE 连接超时。此时请在普通登录 shell、允许 `/proc`/`ptrace`/可执行文件读取的环境中运行验证脚本。

关键输出：

- `suite_summary.json`
- `suite_tripartite_comparison.csv`
- 每个 workload 下的 `minesim_counterpoint/diagnosis.json`

## Git 纳管边界

应提交：

- `minesim/` 源码、配置和工具源码
- `counterpoint_lite/` 源码、配置、脚本和示例
- `workloads/*/*.c`、`workloads/*/Makefile`、必要 README
- `global/scripts/`、`global/docs/`、`global/configs/`
- `scripts/`、`patches/`、根 `Makefile`、本文档

不应提交：

- `dynamorio/`
- `snipersim/`
- `_deps/`、`_build/`
- `global/out/`、`counterpoint_lite/out/`
- trace、perf、Sniper/MineSim 输出、workload 二进制、`env.sh`

`.gitignore` 已按这个边界配置。

## 默认实验集

完整验证默认使用：

```text
log_state,graph_walk,codec_pipeline,branch_dense,cache_bench
```

历史基线说明见：

```text
global/docs/botmux_minesim_handoff_and_runbook.md
global/docs/default_5workload_oldtrace_result_analysis_20260527.md
```
