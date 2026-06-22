# MineSim 当前进度与双 Agent 上手指南

本文档面向 `botmux` 中的两个 agent，目标是让它们在最短时间内接手当前实验环境，继续推进 `MineSim / Sniper / baseline / CounterPoint` 实验闭环。

项目根目录：

```text
/data00/yinhaolang/simulators/archsim
```

## 当前正式结果

当前 `archsim` 隔离工作区内可直接读取的最新正式验证结果目录：

```text
/data00/yinhaolang/simulators/archsim/global/out/default_5workload_tripartite_oldtrace_20260527_rerun
```

关键结果：

- 默认 5-workload 旧链路整轮 wall time: `760.10s`，约 `12 分 40 秒`
- 全部 `25` 个对比点平均绝对相对误差:
  - MineSim: `50.138%`
  - Sniper: `387.685%`
- 去掉 `dtlb_load_misses` 后平均绝对相对误差:
  - MineSim: `39.678%`
  - Sniper: `50.355%`
- 仅看 `core.cycles + core.instructions + branch.misses`:
  - MineSim: `22.754%`
  - Sniper: `26.878%`

关键文件：

- `suite_summary.json`
- `suite_tripartite_comparison.csv`
- `run.log`
- `global/docs/default_5workload_oldtrace_result_analysis_20260527.md`

## 当前实验状态

已完成：

1. `MineSim Criticality Window (MCW)` observation 与 timing 模式已经接入
2. `graph_walk` 的误差已较早期版本明显下降，但仍是最坏点
3. 新增 workload：
   - `branch_dense`
   - `dep_chain`
   - `mlp_stream`
4. `cache_bench` 与上述 3 个 workload 已接入：
   - `global/scripts/run_single_core_workload_suite.py`
5. 已完成新 workload 的 trace 可运行性与指令规模标定
6. 默认 suite 已收缩为保守的 5-workload 集合：
   - `log_state`
   - `graph_walk`
   - `codec_pipeline`
   - `branch_dense`
   - `cache_bench`

未完成：

1. 基于默认 5-workload 集合的误差归因尚未完成
2. 下一轮 `MineSim` 修正尚未开始
3. `DynamoRIO release` 链路不允许用于当前 agent 主线实验
4. 若要重新启用 `dep_chain` / `mlp_stream`，需先缩小 workload 内层常量

## 协作约束

两个 agent 必须遵守以下规则：

1. 只要 `MineSim` 的语义发生变化，`CounterPoint` 必须同步更新
2. 这里的“语义变化”包括：
   - 新增或修改导出的 counters
   - 新增或修改 CPI decomposition 字段
   - 新增或修改 MCW timing / visible-hidden 逻辑
   - 新增或修改 cache / memory / branch / dependency 的解释路径
3. `CounterPoint` 的同步修改由评审者 `Agent B` 主导提出，并由 `Agent B` 审核是否完成
4. 没有完成 `CounterPoint` 同步的 `MineSim` patch，不允许进入 accepted 状态

## 当前运行状态

当前保守的默认 5-workload suite 已经可以在旧链路上完整跑通。

最新完整结果目录：

```text
/data00/yinhaolang/simulators/archsim/global/out/default_5workload_tripartite_oldtrace_20260527_rerun
```

整轮 wall time：

- `760.10s`，约 `12 分 40 秒`

链路状态总结：

- 当前默认链路仍是：
  - `dynamorio/collect_drmemtrace.sh`
- 该旧链路虽然会打印：
  - `WARNING: ... does not appear to be a valid DynamoRIO root`
  - `WARNING: cannot find ... lib32/...`
- 但对保守的默认 5-workload 集合而言，整轮实验已经验证可完成
- 旧链路实际使用的是 debug `drmemtrace` client/runtime 路径
- 当前主要耗时通常不在 workload 宿主机本体，而在：
  - `drraw2trace`
  - `Sniper`

当前真正的风险点：

- `DynamoRIO release` 链路当前明确禁用，agent 不得将其用于默认或主线实验
- `dep_chain` 与 `mlp_stream` 在当前源码下仍然不适合加入默认 suite
- 旧链路虽然可用，但 trace 成本依然高，重复全量实验要控制频次

结论：

- 默认 5-workload 集合当前已经“可跑通”，不再把“整轮跑不通”视为第一 blocker
- 当前第一优先级应转为：
  - 用已验证的旧链路做误差归因和 targeted validation
- 当前 agent 明确不要使用 `release` 链路
- 当前唯一允许的默认采集链路是：
  - `dynamorio/collect_drmemtrace.sh`

## 关键目录

### 配置与代码

- `minesim/config/sapphire_rapids.cfg`
- `minesim/include/core/IntervalCore.h`
- `minesim/src/core/IntervalCore.cpp`
- `counterpoint_lite/configs/simulator_mappings.json`
- `counterpoint_lite/counterpoint_lite/minesim_model.py`
- `global/scripts/run_single_core_workload_suite.py`
- `dynamorio/collect_drmemtrace.sh`

### 实验结果

- 正式结果：
  - `global/out/default_5workload_tripartite_oldtrace_20260527_rerun`
  - `global/docs/default_5workload_oldtrace_result_analysis_20260527.md`
- 当前已验证默认 suite：
  - `global/out/default_5workload_tripartite_oldtrace_20260527_rerun`
  - `global/out/log_state_oldtrace_rerun_20260527`
- 历史实验未整体复制进 `archsim`

说明：

- 本隔离目录只复制了当前实验主线需要的结果，未复制原项目 `global/out` 下的全部历史实验
- 带 `release` 的历史结果不作为 agent 可复用默认结果

## 当前配置摘要

当前主要实验配置来自：

```text
/data00/yinhaolang/simulators/archsim/minesim/config/sapphire_rapids.cfg
```

重要配置：

- `branch_predictor.type=hybrid`
- `history_bits=12`
- `enable_mcw_stats=true`
- `enable_mcw_timing=true`
- `mcw_window_size=512`
- `mshr_capacity=10`
- `branch_flush_memory_overlap_pct=50`

## 环境变量

建议两个 agent 每次执行前都显式设置：

```bash
export SIM_ROOT=/data00/yinhaolang/simulators/archsim
export CC=gcc-11
export CXX=g++-11
export LD_LIBRARY_PATH=/opt/gcc-11/lib64:${LD_LIBRARY_PATH}
export SNIPER_ROOT=$SIM_ROOT/snipersim
export LD_LIBRARY_PATH=$SNIPER_ROOT/xed_kit/lib:$SNIPER_ROOT/lib:$SNIPER_ROOT/libtorch/lib:${LD_LIBRARY_PATH}
```

如果 `MineSim + CounterPoint` 运行报动态库问题，优先检查：

```bash
echo $LD_LIBRARY_PATH
```

## 规模约束

新负载在宿主机上的单次运行时间必须控制在 `1s` 以内。

当前默认保留的 workload 与默认规模为：

- `log_state iter=1`
- `graph_walk iter=1`
- `codec_pipeline iter=1`
- `branch_dense iter=5`
- `cache_bench iter=1`

当前默认 full suite 的已验证运行预算：

- 旧链路整轮默认 5-workload：约 `12 分 40 秒`
- 单独 `log_state` 旧链路 trace 复现：约 `84.77s`
- `codec_pipeline` 在当前整轮里属于较重 workload，`Sniper` 单段实测约 `246.28s`

执行建议：

- targeted workload 优先单独跑，不要一上来重跑 full suite
- full suite 更适合作为每 `2~3` 轮 patch 后的回归检查

这些默认值对应的 `perf instructions` 标定结果为：

- `log_state iter=1`: `55,088,329`
- `graph_walk iter=1`: `22,989,629`
- `codec_pipeline iter=1`: `258,200,178`
- `branch_dense iter=5`: `10,048,639`
- `cache_bench iter=1`: `30,120,602`

说明：

- `branch_dense` 已经被调到接近 `10M instructions`
- `log_state`、`graph_walk`、`codec_pipeline`、`cache_bench` 在当前 CLI 粒度下，`iter=1` 就已经明显高于 `10M`
- 若必须把这几个 workload 也压到 `10M` 左右，需要修改 workload 内部常量，而不是继续减小 `iter`

当前不纳入默认 suite 的 workload：

- `dep_chain iter=1`: `105,747,003`，且 `drmemtrace` 超过 `360s`
- `mlp_stream iter=1`: `207,426,111`，且 `drmemtrace` 超过 `360s`

标定结果文件：

```text
/data00/yinhaolang/simulators/archsim/global/out/instruction_target_10m_20260522.json
/data00/yinhaolang/simulators/archsim/global/out/branch_dense_tiny_probe_20260522/summary.json
/data00/yinhaolang/simulators/archsim/global/out/tiny_scale_probe_20260522/summary.json
/data00/yinhaolang/simulators/archsim/global/out/five_min_probe_blocking_20260522/summary.json
```

如果 agent 后续改 workload 规模，必须重新验证：

```bash
./workloads/log_state/log_state 1
./workloads/graph_walk/graph_walk 1
./workloads/codec_pipeline/codec_pipeline 1
./workloads/branch_dense/branch_dense 5
./workloads/cache_bench/cache_bench 1
```

超过 `1s` 时，优先缩小 `iter`，不要盲目保留大 trace。

## 工作负载列表

当前目标 workload：

- `log_state`
- `graph_walk`
- `codec_pipeline`
- `cache_bench`
- `branch_dense`

当前下线 workload：

- `dep_chain`
- `mlp_stream`

按瓶颈类别划分：

- 分支：
  - `branch_dense`
  - `graph_walk`
- backend / dependency：
  - `codec_pipeline`
- MLP / memory：
  - `graph_walk`
- cache hierarchy：
  - `cache_bench`
- 混合回归：
  - `log_state`

## 一条完整实验链路

完整链路如下：

1. 构建 workload
2. `perf stat` 多次采样 baseline
3. `drmemtrace` 采集 `.trace.gz`
4. `MineSim + CounterPoint` 运行
5. `Sniper` 运行
6. 三方对比
7. 做 CPI / counter / CounterPoint 归因

注意：

- 若第 4 步改变了 `MineSim` 输出字段或 timing 语义，第 4 步与第 7 步之间必须插入：
  - 更新 `counterpoint_lite/configs/simulator_mappings.json`
  - 更新 `counterpoint_lite/counterpoint_lite/minesim_model.py`
  - 重新运行 CounterPoint 检查

## 常用命令

### 1. 编译 MineSim

```bash
cd /data00/yinhaolang/simulators/archsim/minesim
make minesim
```

### 2. 编译 workload

```bash
cd /data00/yinhaolang/simulators/archsim
make -C workloads/log_state
make -C workloads/graph_walk
make -C workloads/codec_pipeline
make -C workloads/cache_bench
make -C workloads/branch_dense
```

### 3. 手动采集 perf baseline

```bash
perf stat -x , \
  -e cycles,instructions,branch-misses,LLC-load-misses,dTLB-load-misses \
  -- /data00/yinhaolang/simulators/archsim/workloads/branch_dense/branch_dense 5
```

### 4. 手动采集 drmemtrace

```bash
/data00/yinhaolang/simulators/archsim/dynamorio/collect_drmemtrace.sh \
  -o /data00/yinhaolang/simulators/archsim/global/out/manual_trace \
  -n branch_dense \
  --subdir-prefix branch_dense \
  -- /data00/yinhaolang/simulators/archsim/workloads/branch_dense/branch_dense 5
```

### 5. 运行 MineSim + CounterPoint

```bash
LD_LIBRARY_PATH=/opt/gcc-11/lib64:$LD_LIBRARY_PATH \
python3 /data00/yinhaolang/simulators/archsim/counterpoint_lite/scripts/run_minesim_config_check.py \
  --config /data00/yinhaolang/simulators/archsim/minesim/config/sapphire_rapids.cfg \
  --trace <trace.gz> \
  --out /data00/yinhaolang/simulators/archsim/global/out/manual_minesim_counterpoint
```

### 6. 运行 Sniper

```bash
cd /data00/yinhaolang/simulators/archsim/snipersim
LD_LIBRARY_PATH=/opt/gcc-11/lib64:$(pwd)/xed_kit/lib:$(pwd)/lib:$(pwd)/libtorch/lib:$LD_LIBRARY_PATH \
./run-sniper -n 1 -d /tmp/sniper_test -c xeon-platinum-8457c-spr -- \
  /data00/yinhaolang/simulators/archsim/workloads/branch_dense/branch_dense 5
```

### 7. 运行全量 suite

```bash
cd /data00/yinhaolang/simulators/archsim
python3 /data00/yinhaolang/simulators/archsim/global/scripts/run_single_core_workload_suite.py \
  --workloads log_state,graph_walk,codec_pipeline,branch_dense,cache_bench \
  --out /data00/yinhaolang/simulators/archsim/global/out/multi_workload_tripartite_resume
```

建议：

- 在旧链路下，默认 5-workload 整轮预算按 `15 分钟` 预留
- 当前实测完整一轮约 `12 分 40 秒`
- 若机器上有残留 `drrun` / `drraw2trace` / `Sniper` 进程，整轮时间会明显劣化；重跑前应先清理残留进程

## 分析流程

两个 agent 应遵循以下归因顺序：

1. 先确认旧链路可复用
   - 优先复用已验证的旧链路和已有结果
   - 不要使用 `release` 链路
2. 跑 isolating workloads
   - `branch_dense`
   - `cache_bench`
3. 根据误差分群选择一个修复方向
   - predictor
   - dependency / backend
   - visible load stall
   - cache hierarchy
4. 做一个最小 patch
5. 重跑 targeted workload
6. 若有收益，再跑全量 suite

## CounterPoint 使用方式

CounterPoint 不是最终裁决器，而是辅助判断“哪一层解释不通”。

当前要求：

- 评审者 `Agent B` 必须检查 `MineSim` 最新输出与 `CounterPoint` DAG 是否对齐
- 只要 `MineSim` 新增了 timing/decomposition counters，必须同步更新：
  - observation mapping
  - model counters
  - DAG/rules
- 若 `MineSim` patch 已提交但 `CounterPoint` 未同步，`Agent B` 必须给出 `reject`

重点读取：

- `summary.json`
- `report.json`
- `diagnosis.json`
- `violations.csv`

解释方法：

- `feasible`
  - 当前 counter 组合大致可由模型解释
- `infeasible`
  - 至少有一部分设计路径不自洽

常见高优先级部件：

- `l1_l2_cache`
- `memory`
- `mmu_tlb`
- `frontend_icache`

## 当前推荐下一步

### 第一优先级

基于已经验证可跑通的旧链路，先完成默认 5-workload 上的误差归因与 targeted 修正。

具体做法：

- 优先读取：
  - `global/out/default_5workload_tripartite_oldtrace_20260527_rerun`
- 从 `branch_dense`、`cache_bench`、`codec_pipeline` 中先挑一个最能隔离问题的 workload
- 每轮只做一个假设、一个 patch、一个 targeted validation

### 第二优先级

跑四个 isolating workloads：

- `branch_dense`
- `dep_chain`
- `mlp_stream`
- `cache_bench`

注意：

- 当前默认 suite 不包含 `dep_chain` / `mlp_stream`
- 重新启用前，必须先缩小 workload 内层常量并重新标定指令规模

### 第三优先级

继续在旧链路上做默认 5-workload 的误差分群和 targeted validation。

约束：

- 不要把 `DynamoRIO release` 链路加入 agent 的任何默认命令
- 不要把 `release` 目录下的脚本、trace 或运行方式作为主线实验入口
- 只有在用户后续明确点名要求时，才允许重新打开 `release` 链路调试

基于误差矩阵判断 MineSim 当前主要问题属于：

- predictor
- dependency
- MLP
- cache latency

然后只改一个方向。

### 第四优先级

在每轮 `MineSim` patch 后，立即检查：

1. `observation.minesim.json` 是否导入了新增 counters
2. `model.from_minesim_config.json` 是否包含了对应 DAG counters/rules
3. `summary.json` / `diagnosis.json` 的解释是否仍然合理

## 两个 agent 的执行边界

- `Agent A`
  - 负责编译、跑实验、改代码
- `Agent B`
  - 负责归因、反驳、审稿

约束：

- 不允许两个 agent 同时改同一文件
- 每轮只允许一个主要假设
- 每轮只允许一个主要 patch
- 没有 targeted validation，不接受 patch
- 改了 MineSim 语义却不改 CounterPoint，不接受 patch
- CounterPoint 的同步修改由 `Agent B` 主导要求并审核

## 建议的阶段文件

建议 agent 在执行过程中维护：

```text
global/agent_loop/current_status.md
global/agent_loop/round_XX_summary.md
global/agent_loop/accepted_changes.md
global/agent_loop/rejected_hypotheses.md
```

## 最后提醒

目前最容易误判的一点是：

- `graph_walk` 的误差很大，但当前第一问题不是继续拍脑袋修 `graph_walk`
- 而是先把 `drmemtrace` 环境修通，再用 isolating workloads 把 branch / dependency / MLP / cache 四类误差分开

只有这样，两个 agent 才能稳定进入“基于证据的 MineSim 迭代”。
