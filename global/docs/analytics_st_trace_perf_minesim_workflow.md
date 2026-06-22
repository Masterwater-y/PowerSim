# analytics_st Trace/Perf/MineSim/CounterPoint Workflow

本文档用于复现单核单线程 workload 的完整验证流程：

1. 使用 DynamoRIO `drmemtrace` 采集 trace。
2. 使用 `perf stat` 对相同负载多次采集 PMU，计算均值和波动率。
3. 使用 MineSim 对 trace 进行仿真，得到仿真 PMU。
4. 使用 CounterPoint 检查当前 MineSim 模型/设计哪里不一致。
5. 报告 MineSim 仿真 PMU 与真实 perf PMU 的误差。

## 默认负载

默认负载为：

```bash
/data00/yinhaolang/simulators/archsim/workloads/analytics_st/analytics_st 8
```

该程序是单线程分析型负载，混合顺序扫描、间接访存、热点更新和整数计算。

为了固定 perf 采集时的 CPU，流程会使用：

```bash
taskset -c 0 /data00/yinhaolang/simulators/archsim/workloads/analytics_st/analytics_st 8
```

注意：trace 采集阶段直接运行 `analytics_st` 本体，不额外套 `taskset`，避免 DynamoRIO 同时记录 `taskset` 包装进程并生成额外空 trace。perf 采集阶段仍使用 `taskset -c 0`。

## 一键执行

推荐直接运行端到端脚本：

```bash
python3 /data00/yinhaolang/simulators/archsim/global/scripts/run_analytics_st_trace_perf_minesim.py \
  --iter 8 \
  --repeats 5 \
  --cpu 0 \
  --config /data00/yinhaolang/simulators/archsim/minesim/config/sapphire_rapids.cfg
```

`global` 版本脚本默认会对 `perf` 采集启用以下稳定化措施：

- 预热 `2` 次，再开始记录样本
- 使用 `setarch -R` 关闭 ASLR
- 使用 `numactl --physcpubind <cpu> --localalloc` 固定本地内存分配
- `perf_iter=0`，即默认复用 `--iter`
- 默认仍采集 `5` 次样本，再计算均值和波动率

如果需要关闭这些默认行为，可显式传：

```bash
--no-perf-disable-aslr --no-perf-local-memory --perf-warmup 0
```

如果只修改 MineSim/CounterPoint 逻辑，不想每次重新采集 DynamoRIO trace，可使用复用 trace 模式：

```bash
python3 /data00/yinhaolang/simulators/archsim/global/scripts/run_analytics_st_trace_perf_minesim.py \
  --reuse-trace \
  --iter 8 \
  --repeats 5 \
  --cpu 0 \
  --config /data00/yinhaolang/simulators/archsim/minesim/config/sapphire_rapids.cfg
```

`--reuse-trace` 会优先使用 `--trace <已有 .trace.gz>`；如果没有传 `--trace`，则自动从
`/data00/yinhaolang/simulators/archsim/global/out/analytics_st_validation_*/drmemtrace/*/trace/`
中选择最近生成的 `analytics_st` trace。旧写法 `--skip-trace --trace <file>` 仍兼容。

如果只想做 PMU 稳定性实验，不跑 trace/MineSim/CounterPoint，可使用：

```bash
python3 /data00/yinhaolang/simulators/archsim/global/scripts/run_analytics_st_trace_perf_minesim.py \
  --perf-only \
  --iter 8 \
  --repeats 5 \
  --cpu 0
```

等价的默认 `perf` 执行形态是：

```bash
perf stat -x, \
  -e cycles,instructions,branch-misses,LLC-load-misses,dTLB-load-misses \
  -- taskset -c 0 setarch x86_64 -R numactl --physcpubind 0 --localalloc \
     /data00/yinhaolang/simulators/archsim/workloads/analytics_st/analytics_st 8
```

脚本会自动完成：

- `make` 构建 `analytics_st`
- 调用 `/data00/yinhaolang/simulators/archsim/dynamorio/collect_drmemtrace.sh` 采集 trace
- 对相同 workload 运行多次 `perf stat`
- 重新编译 `minesim`
- 调用 `run_minesim_config_check.py`
- 生成 perf 均值/波动率与 MineSim/perf 误差表

## 输出目录

默认输出目录形如：

```text
/data00/yinhaolang/simulators/archsim/global/out/analytics_st_validation_YYYYMMDD_HHMMSS
```

关键文件：

- `drmemtrace/<run_name>/trace/*.trace.gz`
- `perf/perf_0.csv` ... `perf/perf_N.csv`
- `perf_stats.json`
- `minesim_counterpoint/summary.json`
- `minesim_counterpoint/report.json`
- `minesim_counterpoint/diagnosis.json`
- `minesim_counterpoint/observation.minesim.json`
- `pmu_comparison.csv`
- `pmu_comparison.json`
- `final_summary.json`

## Perf PMU 指标

默认采集以下事件：

```text
cycles,instructions,branch-misses,LLC-load-misses,dTLB-load-misses
```

这些事件会按 `counterpoint_lite/configs/simulator_mappings.json` 映射为：

- `cycles` -> `core.cycles`
- `instructions` -> `core.instructions`
- `branch-misses` -> `branch.misses`
- `LLC-load-misses` -> `cache.llc.load_misses`
- `dTLB-load-misses` -> `tlb.dtlb_load_misses`

## 波动率定义

对每个 PMU counter，脚本按多次 `perf stat` 样本计算：

```text
mean = 样本均值
stdev = 样本标准差
volatility_cv = stdev / mean
volatility_pct = volatility_cv * 100
```

`volatility_pct` 越低，说明该指标在多次运行之间越稳定。

## 误差定义

`pmu_comparison.csv` 中的误差定义为：

```text
abs_error = minesim - perf_mean
relative_error = (minesim - perf_mean) / perf_mean
relative_error_pct = relative_error * 100
```

解释：

- 正值：MineSim 高估。
- 负值：MineSim 低估。
- 接近 0：MineSim 与真实 PMU 更一致。

## CounterPoint 结果解读

`minesim_counterpoint/summary.json` 中：

- `verdict = feasible` 表示当前 MineSim 观测能被配置生成的约束模型解释。
- `verdict = infeasible` 表示至少一部分 counter 无法被当前模型解释。
- `max_normalized_violation` 是最大归一化违规程度。
- `ranked_components` 是疑似设计/模型问题的部件排序。

常见解释：

- `llc_cha` 高：LLC/CHA/目录/一致性流量建模不足。
- `l1_l2_cache` 高：L1/L2 hit/miss/writeback/prefetch 路径不完整。
- `memory` 高：DRAM read/write、LLC miss service 或 writeback 路径不完整。
- `frontend_icache` 高：I-cache miss 或前端取指路径缺失。
- `mmu_tlb` 高：TLB/page-walk/page-size 语义缺失。

## 分步执行

### 1. 构建 workload

```bash
make -C /data00/yinhaolang/simulators/archsim/workloads/analytics_st
```

### 2. 采集 drmemtrace

```bash
/data00/yinhaolang/simulators/archsim/dynamorio/collect_drmemtrace.sh \
  -o /data00/yinhaolang/simulators/archsim/global/out/manual_drmemtrace \
  -n analytics_st_manual \
  --subdir-prefix analytics_st \
  -- /data00/yinhaolang/simulators/archsim/workloads/analytics_st/analytics_st 8
```

### 3. 多次采集 perf stat

```bash
perf stat -x, \
  -e cycles,instructions,branch-misses,LLC-load-misses,dTLB-load-misses \
  -- taskset -c 0 /data00/yinhaolang/simulators/archsim/workloads/analytics_st/analytics_st 8
```

重复多次后计算均值和波动率。推荐使用一键脚本自动完成。

### 4. 运行 MineSim + CounterPoint

```bash
python3 /data00/yinhaolang/simulators/archsim/counterpoint_lite/scripts/run_minesim_config_check.py \
  --config /data00/yinhaolang/simulators/archsim/minesim/config/sapphire_rapids.cfg \
  --trace <采集到的 .trace.gz> \
  --out /data00/yinhaolang/simulators/archsim/counterpoint_lite/out/manual_minesim_counterpoint
```

### 5. 对比 MineSim PMU 和 Perf PMU

推荐直接读取一键脚本生成的：

```text
pmu_comparison.csv
pmu_comparison.json
```

## 给 AI 的下次输入模板

可以直接把下面这段发给 AI：

```text
请运行 /data00/yinhaolang/simulators/archsim/global/scripts/run_analytics_st_trace_perf_minesim.py，
参数使用 --iter 8 --repeats 5 --cpu 0 --config /data00/yinhaolang/simulators/archsim/minesim/config/sapphire_rapids.cfg。
运行完成后读取 final_summary.json、perf_stats.json、pmu_comparison.csv、
minesim_counterpoint/summary.json 和 minesim_counterpoint/diagnosis.json。
请报告：
1. trace 路径；
2. perf stat 每个 PMU 的均值、标准差和波动率；
3. MineSim 仿真 PMU；
4. MineSim 相对 perf 均值的绝对误差和百分比误差；
5. CounterPoint verdict、max_normalized_violation 和 ranked_components；
6. 根据 CounterPoint 反馈判断 MineSim 哪个微架构设计最可能有问题。
```
