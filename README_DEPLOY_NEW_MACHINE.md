# archsim 新机器部署 README

这份文档面向第一次在新机器上部署 `archsim` 的使用者。目标不是解释实现细节，而是把“依赖检查 -> 一键部署 -> smoke 验证 -> full 验证”跑通。

## 1. 适用环境

建议环境：

- Linux x86_64
- 可访问 GitHub 和 Sniper 依赖下载地址
- 普通登录 shell，不要在强隔离 sandbox / 受限容器里运行
- 允许 DynamoRIO / Pin / SDE 注入目标进程

不满足上面条件时，常见失败现象包括：

- `drrun -- /bin/true` 长时间不退出
- `Pin readlink:: Permission denied`
- Sniper 等不到 SIFT 连接

## 2. 依赖检查

先执行下面这段命令，确认基础工具齐全：

```bash
command -v git
command -v cmake
command -v make
command -v wget
command -v tar
command -v perf
command -v gcc || command -v gcc-11
command -v g++ || command -v g++-11
command -v python3
python3 --version
```

`bootstrap.sh` 会自动优先选择 Python `>= 3.8`。如果系统默认 `python3` 太老，但机器上有新版本，例如 `python3.11` 或 conda Python，可以显式指定：

```bash
export PYTHON=/path/to/python3.11
```

建议额外检查磁盘空间和 CPU 并行度：

```bash
df -h .
nproc
```

## 3. 一键部署

```bash
git clone <repo-url> archsim
cd archsim
./scripts/bootstrap.sh
source ./env.sh
```

如果需要显式指定并行编译核数：

```bash
./scripts/bootstrap.sh -j 32
source ./env.sh
```

如果需要显式指定 Python：

```bash
PYTHON=/path/to/python3.11 ./scripts/bootstrap.sh
source ./env.sh
```

`bootstrap.sh` 会自动完成：

- 下载并安装 DynamoRIO 到 `dynamorio/`
- 下载并构建 Sniper 到 `snipersim/`
- 应用本仓库维护的 Sniper 本地 patch 和配置文件
- 编译 MineSim
- 编译 `workloads/` 下的 microbench
- 生成本机环境文件 `env.sh`

## 4. 最小运行检查

先检查 DynamoRIO 能否注入：

```bash
source ./env.sh
timeout 30 dynamorio/bin64/drrun -- /bin/true
echo "DR exit=$?"
```

预期：

- 返回码为 `0`
- 不应长时间挂住

再检查 Sniper 最小用例：

```bash
rm -rf /tmp/archsim_sniper_check
timeout 180 ./snipersim/run-sniper \
  -n 1 \
  -d /tmp/archsim_sniper_check \
  -c xeon-platinum-8457c-spr \
  -- workloads/branch_dense/branch_dense 1

echo "Sniper exit=$?"
ls -lh /tmp/archsim_sniper_check/sim.stats.sqlite3
```

预期：

- `Sniper exit=0`
- 生成 `sim.stats.sqlite3`

## 5. 一键 smoke 验证

这一步验证完整最小闭环：

```bash
rm -rf global/out/verify_smoke
DR_TIMEOUT_SECONDS=600 ./scripts/verify_full_loop.sh --smoke
echo "smoke exit=$?"
```

smoke 会完成：

- workload 编译和 warmup
- `perf` 基准采集
- DynamoRIO `drmemtrace`
- MineSim 仿真
- CounterPoint 诊断
- Sniper 仿真
- 三方误差比较

通过后应能看到：

```bash
find global/out/verify_smoke -maxdepth 5 -type f | sort
```

关键输出通常包括：

- `global/out/verify_smoke/branch_dense/sniper/sim.stats.sqlite3`
- `global/out/verify_smoke/branch_dense/tripartite_comparison.csv`
- `global/out/verify_smoke/branch_dense/tripartite_comparison.json`
- `global/out/verify_smoke/suite_summary.json`
- `global/out/verify_smoke/suite_tripartite_comparison.csv`

## 6. full 验证

smoke 通过后，如需复现实验默认 5-workload，执行：

```bash
rm -rf global/out/verify_full_loop
DR_TIMEOUT_SECONDS=1200 ./scripts/verify_full_loop.sh --full
echo "full exit=$?"
```

默认 workload 集合：

```text
log_state,graph_walk,codec_pipeline,branch_dense,cache_bench
```

## 7. 结果怎么看

重点看这几个文件：

```bash
cat global/out/verify_smoke/suite_summary.json
cat global/out/verify_smoke/suite_tripartite_comparison.csv
```

如果跑的是 full，对应路径替换成：

```text
global/out/verify_full_loop/...
```

`suite_summary.json` 会给出每个 workload 的运行摘要；`suite_tripartite_comparison.csv` 会汇总 PMU、MineSim、Sniper 的对比结果。

## 8. 常见问题

### 8.1 `drrun` 挂住

先直接验证：

```bash
timeout 30 dynamorio/bin64/drrun -- /bin/true
```

如果这里就挂住，通常是运行环境限制了进程注入，不是 `archsim` 仓库内容本身的问题。

### 8.2 Sniper 报 Pin/SDE 相关错误

典型现象：

```text
Pin readlink:: Permission denied
```

这通常说明当前 shell 运行环境对 `/proc`、可执行文件路径解析或注入行为有限制。请改到普通登录 shell 执行，不要在强隔离容器或受限 sandbox 中运行。

### 8.3 smoke 在 trace 转换阶段失败

先检查是否生成 trace：

```bash
find global/out/verify_smoke -name '*.trace.gz' -o -name '*.trace.zip'
```

当前仓库已经固定 `drraw2trace -compress gzip`，MineSim 读取的是 `.trace.gz` 旧链路格式。

## 9. 推荐的标准流程

新机器上建议严格按这个顺序验证：

```bash
git clone <repo-url> archsim
cd archsim
./scripts/bootstrap.sh
source ./env.sh
timeout 30 dynamorio/bin64/drrun -- /bin/true
timeout 180 ./snipersim/run-sniper -n 1 -d /tmp/archsim_sniper_check -c xeon-platinum-8457c-spr -- workloads/branch_dense/branch_dense 1
DR_TIMEOUT_SECONDS=600 ./scripts/verify_full_loop.sh --smoke
```

如果这套流程通过，就可以认为这台机器具备运行 `archsim` 实验环境的条件。
