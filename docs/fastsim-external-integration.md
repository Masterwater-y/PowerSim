# FastSim 使用与外部框架接入指南

本文描述当前 FastSim 可用的输入、执行模式、暂停/继续接口和输出格式，
并区分已经实现的能力与建议新增的外部控制协议。

## 1. 当前接口总览

FastSim 是 trace-driven 模拟器。一次模拟由以下三类输入共同确定：

1. 每个软件线程的功能 trace，通常是 canonical FST v7；
2. 微架构和测量配置；
3. reference frequency、初始每核频率，以及可选的运行时调频决策。

当前接入方式如下：

| 方式 | 当前可用 | 能否在窗口间动态调频 | 适用场景 |
|---|---|---:|---|
| `fastsim simulate` 普通 CLI | 是 | 否 | 离线、一次性完整模拟 |
| CLI 分窗输出 | 是 | 否；频率在启动时固定 | 固定频率的窗口轨迹、数据集生成 |
| 链接 `fastsim_lib` 的 C++ API | 是 | 是 | DVFS governor、RL 环境、在线控制器 |
| `fastsim_py` pybind11 API | 是 | 是 | Python、Gymnasium、RL controller |
| 长驻进程 IPC | 否 | 尚未实现 | Rust、Java 或远程框架的闭环控制 |

所谓“暂停”是模拟时间边界上的同步暂停：`advance()` 阻塞推进，达到指定
窗口边界后返回，此时完整模拟器状态仍保存在内存中。当前没有异步
`pause()`，不能从另一个线程中断一个正在执行的窗口，也不能把暂停状态保存到
磁盘后跨进程恢复。

## 2. 构建

命令行程序和 C++ 库目标使用同一个 CMake 工程：

```bash
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -- -j16
./build/fastsim_tests
```

主要产物是：

- `build/fastsim`：命令行程序；
- CMake target `fastsim_lib`：C++ 控制器链接的库；
- 可选的 `build-python/python/fastsim_py*.so`：Python DVFS 模块；
- `include/fastsim/*.hpp`：公开 C++ 头文件。

工程目前没有安装规则、导出的 CMake package 或稳定的二进制 ABI。外部 C++
工程应把 FastSim 作为源码依赖并一起编译。

Python 绑定默认关闭。选择一个安装了 pybind11 的 Python ≥3.8 后构建：

```bash
python3 -m pip install pybind11

cmake -S . -B build-python \
  -DCMAKE_BUILD_TYPE=Release \
  -DFASTSIM_BUILD_PYTHON=ON \
  -DFASTSIM_PYTHON_EXECUTABLE=/path/to/venv/bin/python
cmake --build build-python -- -j16
```

CMake 会从该 interpreter 查询 `sys.base_prefix` 以定位开发头文件，并调用
`python -m pybind11 --cmakedir` 定位 pybind11。这兼容旧 CMake 3.13 与 virtualenv
组合；也可以显式传递 `Python3_ROOT_DIR` 和 `pybind11_DIR`。

在源码树内直接导入：

```bash
PYTHONPATH=build-python/python python3 -c \
  'import fastsim_py; print(fastsim_py.__version__)'
```

## 3. 输入

### 3.1 微架构配置

配置文件是 `key = value` 格式，`#` 开始行内注释。一个文件可以使用一次
`config.include` 引入父配置；相对路径以当前配置文件所在目录解析，当前文件的键
覆盖父文件。

配置覆盖顺序是：

```text
SimulatorConfig 默认值
  < config.include 指向的父配置
  < 当前配置文件
  < CLI 显式覆盖项
```

主要参数族包括：

- `measurement.*`：测量范围和 native-kernel trace 语义；
- `sim.*`：核数、trace chunk、lookahead、interval scheduler 和 epoch 大小；
- `core.*`：流水线宽度、ROB/IQ/LSQ、FU 数量和延迟；
- `cache.l1i.*`、`cache.l1d.*`、`cache.l2.*`、`cache.llc.*`：缓存几何和延迟；
- `branch.*`、`dtlb.*`、`dram.*`：分支、TLB 和 DRAM 参数。

不要为外部实验重新拼一份不完整配置；应从仓库中与目标 gem5/trace 语义匹配的
profile 开始，仅覆盖需要扫描的参数。DVFS v1 要求：

```ini
core.model = interval_weave
sim.interval_scheduler = time_epoch
sim.interval_reweave_passes = 1
sim.interval_causal_timing = false
sim.interval_response_retime = false
sim.interval_rob_head_suffix_replay = false
sim.interval_corrected_suffix_carry = false
core.store_post_commit_request = false
```

`measurement.scope` 必须最终解析为 `user` 或 `user-plus-kernel`。推荐 CLI 每次都
显式传递 `--measurement-scope`，避免外部框架错误复用 profile。

### 3.2 FST 和 manifest

推荐运行时输入是 canonical FST v7。FST 是已经完成 UOP lowering、依赖编码、
分支归一化和物理地址记录的功能 IR，不是原始指令 trace。JSONL 可以直接读取，
aligned Parquet 需要先转换；原始 DynamoRIO/drmemtrace 当前不能直接作为严格输入。

`--manifest` 文件把逻辑线程按静态方式绑定到硬件核。最简单的 FST manifest 是：

```text
0 fastsim-binary core0.fst
1 fastsim-binary core1.fst
2 fastsim-binary core2.fst
3 fastsim-binary core3.fst
```

规则如下：

- 第一列是目标逻辑线程/初始核 ID，必须从 0 开始连续；
- entry 数量必须在 `[1, sim.cores]` 内；entry 少于核数时，其余核空闲；
- manifest 中的相对 trace 路径以 manifest 所在目录解析；
- FST header 的 source core ID 默认必须等于目标 ID；
- 普通 binary entry 可以增加第四列，显式把另一个 source core remap 到目标核；
- 每个活动线程当前永久绑定到初始核，没有迁移和 time slicing。

支持的推荐格式名及行格式为：

```text
<core> gem5-jsonl <path>
<core> fastsim-binary <path> [source-core]
<core> fastsim-binary-slice <path> <source-core> <skip-inst> <take-inst>
<core> fastsim-binary-warmup-slice <path> <source-core> <warmup-inst> <take-inst> [warmup-records take-records]
```

`fastsim-binary-warmup-slice` 先用 prefix 建立功能和微架构状态，在所有活动流到达
共同边界后清零测量时间/计数，再模拟 ROI。它不是简单丢弃 prefix。

单个 gem5 JSONL 可转换为 FST：

```bash
./build/fastsim convert-gem5 \
  --input core0.jsonl \
  --output core0.fst \
  --core 0 \
  --syscall-abi linux-x86_64
```

完整字段和 sidecar 约束见
[`gem5-trace-contract.md`](gem5-trace-contract.md) 和
[`fst-v7-drmemtrace-conversion-contract.md`](fst-v7-drmemtrace-conversion-contract.md)。

### 3.3 频率输入

频率统一使用整数 Hz：

```ini
sim.reference_frequency_hz = 3000000000
core.frequency_hz = 2400000000
# 非空时覆盖 uniform frequency，数量必须严格等于 sim.cores。
core.frequencies_hz = 3000000000,2400000000,1800000000,1500000000
```

- `sim.reference_frequency_hz` 是共享 cache、CHA、NoC、DRAM 的公共目标时间域；
- `core.frequency_hz` 是所有核的统一初始频率；
- `core.frequencies_hz` 是按 core ID 排列的初始频率数组；
- 所有频率必须在 `[1, 1000000000000]` Hz，0 Hz/clock gating 不支持。

CLI 对应覆盖项是：

```text
--reference-frequency-hz HZ
--core-frequency-hz HZ
--core-frequencies-hz HZ,...
```

后两个选项互斥。建议始终写完整十进制 Hz，例如 `3000000000`；配置解析器中的
`K/M/G` 是容量式二进制后缀，`3G` 不等于十进制 3 GHz。

## 4. 普通批处理 CLI

完整模拟、不分窗：

```bash
./build/fastsim simulate \
  --measurement-scope user \
  --config configs/gem5-v28_1-fs-user.cfg \
  --manifest traces/manifest.txt \
  --core-frequency-hz 3000000000 \
  --output result.json
```

未指定 `--output` 或指定 `--output -` 时，JSON 写到 stdout；诊断信息和错误写到
stderr。成功返回 0，参数、输入或运行错误返回 1。

输出 schema 是 `fastsim-stats-v5`，顶层结构为：

```json
{
  "schema": "fastsim-stats-v5",
  "measurement_scope": "user",
  "scope_metrics": {},
  "configuration": {},
  "totals": {},
  "threads": [],
  "cores": [],
  "cha": [],
  "instruction_cha": []
}
```

外部消费者必须先检查 `schema`。正式 CPI、PMU 和吞吐指标从
`scope_metrics` 读取；`totals` 包含兼容字段和大量内部诊断，不应从多个相似字段中
自行挑选结果。常用字段是：

```text
scope_metrics.user_trace_instructions
scope_metrics.user_trace_uops
scope_metrics.sum_core_cycles
scope_metrics.cycles_per_user_uop
scope_metrics.perf_like_cpi
scope_metrics.pmu
scope_metrics.memory_hierarchy_user
scope_metrics.throughput.user_uops_per_second
configuration.reference_frequency_hz
configuration.core_frequencies_hz
```

这是一次性运行模式。调用 C++ `run()` 后不能再调用 `advance()`；开始窗口模式后也
不能切换回 `run()`。

`sum_core_cycles` 和由它得到的 aggregate CPI 是各核本地周期的求和。异频运行时
它不是物理 makespan；DVFS 控制器应同时使用每核窗口 CPI 和
`start_time_fs/end_time_fs`，不要把不同 clock domain 的周期和当作墙钟时间。

## 5. 固定频率窗口 CLI

时间窗口示例：

```bash
./build/fastsim simulate \
  --measurement-scope user \
  --config configs/gem5-v28_1-time-epoch.cfg \
  --manifest traces/manifest.txt \
  --core-frequencies-hz 3000000000,2400000000,1800000000,1500000000 \
  --window-time-ns 100000 \
  --output windows.json
```

指令窗口将 `--window-time-ns N` 替换为：

```text
--window-instructions N
```

两者互斥且必须非零。时间窗口使用模拟目标时间，不是宿主机 wall time；正常窗口
不会越过请求的时间边界，trace 结束时的最后一个窗口可以更短。指令预算是所有
活动核的 retired macro-instruction 总和，在第一个达到预算的 committed
time-epoch 边界暂停，因此可能产生 `instruction_overshoot`。

CLI 会以同一组启动频率自动运行所有窗口并生成一个完整的
`fastsim-windows-v1` 文档。它不会在输出一个窗口后等待外部输入，也不能实现闭环
DVFS。

## 6. 动态 DVFS C++ 接口

公开接口位于 `include/fastsim/simulator.hpp`：

```cpp
SimulationStats run();
SimulationWindowResult advance(const SimulationWindow& window);
void set_core_frequencies(const std::vector<std::uint64_t>& frequencies_hz);
bool finished() const;
```

典型控制器：

```cpp
#include <cstdint>
#include <utility>
#include <vector>

#include "fastsim/config.hpp"
#include "fastsim/simulator.hpp"
#include "fastsim/trace.hpp"

int main() {
    auto config = fastsim::load_simulator_config("profile.cfg");
    config.measurement_scope = fastsim::MeasurementScope::kUser;
    config.validate();

    auto traces = fastsim::open_trace_manifest(
        "manifest.txt", config.cores);
    fastsim::Simulator simulator(config, std::move(traces));

    while (!simulator.finished()) {
        const auto observation = simulator.advance(
            fastsim::SimulationWindow::simulated_time_ns(100000));

        consume_window(observation);
        if (observation.finished) break;

        const std::vector<std::uint64_t> action =
            governor_decide(observation);
        simulator.set_core_frequencies(action);
    }
}
```

指令控制改为：

```cpp
simulator.advance(
    fastsim::SimulationWindow::retired_instructions(100000));
```

每次 `advance()` 可以选择不同的窗口种类和大小。生命周期是：

```text
construct
   |
   v
advance(window) --同步推进--> paused + SimulationWindowResult
   ^                                      |
   |                                      v
   +----------- next advance <--- set_core_frequencies(...)
                                          |
                                          v
                                      finished
```

接口语义：

- `advance()` 返回即表示到达安全暂停边界；窗口 PMU 已经是相对上一个边界的增量；
- `set_core_frequencies()` 在当前模拟时间对全部核原子生效，并影响下一次
  `advance()`；数组数量必须严格等于 `sim.cores`；
- 没有状态重建或 trace rewind；branch、cache、coherence、DRAM、流水线和未完成
  模拟状态都留在同一个 `Simulator` 对象内；
- API 没有并发调用契约。一个外部 session 应由单个控制线程串行调用；
- 对象和 trace 是 one-shot，reset 新 episode 应重新创建 `Simulator`；
- `finished()` 或返回结果中的 `finished` 为 true 后不得再调频；
- 有 functional warmup 时，首个测量窗口必须使用 reference frequency，之后才能
  切换到非 reference frequency。

外部工程可直接链接源码 target：

```cmake
set(FASTSIM_BUILD_TESTS OFF CACHE BOOL "" FORCE)
add_subdirectory(/path/to/FastSim fastsim-build EXCLUDE_FROM_ALL)

add_executable(my_fastsim_controller controller.cpp)
target_link_libraries(my_fastsim_controller PRIVATE fastsim_lib)
```

## 7. 窗口结果格式

C++ 返回 `SimulationWindowResult`。CLI 把同一组字段编码到
`fastsim-windows-v1`：

```json
{
  "schema": "fastsim-windows-v1",
  "reference_frequency_hz": 3000000000,
  "initial_core_frequencies_hz": [3000000000],
  "window_kind": "simulated_time_ns",
  "windows": [
    {
      "window_id": 1,
      "start_time_fs": 0,
      "end_time_fs": 100000000,
      "requested_value": 100,
      "retired_instructions": 1234,
      "instruction_overshoot": 0,
      "finished": false,
      "cores": [
        {
          "core": 0,
          "frequency_hz": 3000000000,
          "cycles": 300,
          "retired_instructions": 500,
          "retired_uops": 700,
          "memory_uops": 100,
          "memory_accesses": 120,
          "retired_branches": 80,
          "retired_branch_misses": 4,
          "dtlb_accesses": 120,
          "dtlb_misses": 2,
          "cpi": 0.6,
          "uop_cpi": 0.4285714286,
          "l1d": {
            "accesses": 120,
            "hits": 100,
            "misses": 20,
            "evictions": 1,
            "writebacks": 0
          },
          "l2": {
            "accesses": 20,
            "hits": 12,
            "misses": 8,
            "evictions": 0,
            "writebacks": 0
          }
        }
      ],
      "shared": {
        "llc": {
          "accesses": 8,
          "hits": 4,
          "misses": 4,
          "evictions": 0,
          "writebacks": 0
        },
        "cha_requests": 8,
        "cha_reads": 6,
        "cha_writes": 2,
        "llc_hits": 4,
        "llc_misses": 4,
        "permission_upgrades": 1,
        "invalidations": 2,
        "remote_supplies": 0,
        "llc_unique_fills": 4,
        "llc_merged_misses": 1,
        "llc_merged_wait_cycles": 6,
        "dram_reads": 3,
        "dram_writes": 1,
        "queue_cycles": 30
      }
    }
  ]
}
```

字段语义：

- `start_time_fs/end_time_fs`：相对 measurement 起点的模拟物理时间；
- `requested_value`：单位由外层 `window_kind` 或本次 C++ request 决定；
- 顶层 `retired_instructions`：本窗口所有核之和；
- `cores[*].cycles`：该核本地 clock domain 的窗口周期增量；
- `cpi`：`cycles / retired_instructions`，无退休指令时 JSON 为 `null`；
- `uop_cpi`：`cycles / retired_uops`，无退休 UOP 时 JSON 为 `null`；
- `shared.*_cycles`：reference-time domain 的周期，不是某个核的本地周期；
- cache/shared PMU 按请求实际提交到目标状态的窗口记账，可能早于所属指令退休
  的窗口，但完整运行求和守恒。

窗口 schema 只包含窗口增量，不包含 `fastsim-stats-v5` 的完整内部诊断和最终
累计报告。C++ 库目前也没有在窗口运行结束后导出完整 `SimulationStats` 的 getter。
CLI 中的 JSON writer 也尚未作为库接口导出；嵌入式控制器需要直接消费 public
struct，或在自己的 adapter 中完成序列化。Python 绑定为每个结果对象提供
`to_dict()`，其单窗口 schema 是 `fastsim-window-result-v1`。

## 8. 外部框架应如何接入

### 8.1 C++ governor

直接链接 `fastsim_lib` 是当前闭环 DVFS 的推荐方式。外部框架负责：

1. 固定 config、manifest 和 measurement scope；
2. 调用 `advance()` 获得 observation；
3. 从 CPI/PMU 产生一个长度为 `sim.cores` 的频率 action；
4. 调用 `set_core_frequencies()`；
5. 重复直到 `finished`。

频率 action 当前是任意合法 Hz；FastSim 不提供平台 OPP 表，也不建模 voltage、
功耗、切频延迟或能量。真实平台允许的频点和约束应由 governor 层检查。

### 8.2 Python：`fastsim_py`

Python 绑定直接在进程内持有 C++ `Simulator`。文件输入和动态控制示例：

```python
import fastsim_py

sim = fastsim_py.DvfsSession(
    config_path="profile.cfg",
    manifest_path="manifest.txt",
    measurement_scope="user",
    reference_frequency_hz=3_000_000_000,
    initial_core_frequencies_hz=[
        3_000_000_000,
        2_400_000_000,
        1_800_000_000,
        1_500_000_000,
    ],
)

while not sim.finished():
    observation = sim.advance_time_ns(100_000)
    consume(observation.to_dict())
    if observation.finished:
        break
    sim.set_core_frequencies(governor(observation))
```

也可以使用 instruction window 或通用窗口对象：

```python
observation = sim.advance_instructions(100_000)
observation = sim.advance(
    fastsim_py.SimulationWindow.simulated_time_ns(100_000)
)
```

结果默认是只读 typed object：

```python
observation.window_id
observation.start_time_fs
observation.end_time_fs
observation.finished
observation.cores[0].frequency_hz
observation.cores[0].cycles
observation.cores[0].cpi       # 无退休指令时为 None
observation.cores[0].l1d.misses
observation.shared.dram_reads
```

用于框架接线和 CI 的 synthetic factory 不需要 FST：

```python
sim = fastsim_py.DvfsSession.synthetic(
    cores=2,
    instructions_per_core=100_000,
    initial_core_frequencies_hz=[4_500_000_000, 1_500_000_000],
)
```

`advance()`、`advance_time_ns()` 和 `advance_instructions()` 执行期间释放 Python
GIL。session 内部用 mutex 防止数据竞争，但一个控制 episode 仍应由一个 Python
控制线程按 `advance → observe → set frequency` 串行调用；mutex 不提供异步暂停。

pybind11 会把 `std::invalid_argument` 映射为 Python `ValueError`，其他运行或状态
错误映射为 `RuntimeError`。Python 对象销毁时释放内存状态；当前没有 pickle、
checkpoint 或跨进程恢复。

### 8.3 Rust、Java 或远程框架

当前 `fastsim` CLI 不是交互协议，不能通过反复启动进程来模拟 pause/resume：重新
启动会丢失全部 cache、branch、coherence、DRAM 和流水线状态。

建议新增一个很薄的、长驻的 `fastsim-control` adapter，在进程内部持有一个
`Simulator`，stdin/stdout 使用逐行 JSON（NDJSON）。最小协议可以是：

```json
{"id":1,"op":"start","config":"profile.cfg","manifest":"manifest.txt"}
{"id":2,"op":"advance","window":{"kind":"simulated_time_ns","value":100000}}
{"id":3,"op":"set_frequencies","frequencies_hz":[3000000000,1800000000]}
{"id":4,"op":"advance","window":{"kind":"retired_instructions","value":100000}}
{"id":5,"op":"close"}
```

每条请求返回并立即 flush 一行：

```json
{"id":2,"ok":true,"schema":"fastsim-window-result-v1","result":{}}
```

失败返回结构化错误，进程日志只写 stderr：

```json
{"id":3,"ok":false,"error":{"code":"invalid_argument","message":"..."}}
```

这个 adapter 应复用 `SimulationWindowResult`，而不是暴露模拟器内部对象。它还应：

- 一次只处理一个 session，多个 episode 使用进程池隔离；
- 严格串行化命令，禁止 `advance` 运行中调频；
- 在 `start` response 回显 schema、有效配置、核数、reference frequency 和初始频率；
- 在每个 response 中保留 request ID、单位和 schema version；
- 把 C++ exception 映射成稳定错误码；
- 对 config、manifest、FST 记录路径或 hash，保证实验可复现。

## 9. 当前限制和接入注意事项

- 动态频率可通过 C++ API 或 `fastsim_py`；现有 CLI 仍只能固定频率自动跑完窗口；
- 暂停状态只存在于当前进程内，没有 checkpoint/restore；
- 没有异步中断、取消当前窗口或动态添加 trace；
- 没有 clock gating、线程迁移、OS 调度、voltage、power、energy 或 transition
  latency 模型；
- `fastsim_lib` 的 C++ ABI 和 public struct 目前没有独立兼容性承诺，外部 wrapper
  应与 FastSim 同版本构建；
- `fastsim_py` 是 CPython extension，需要为目标 Python major/minor 和平台重新构建；
  当前没有预构建 wheel 或 `pip install` package；
- CLI JSON 有 schema version，但窗口 JSON 当前没有 config/manifest hash；实验框架
  应在外围 manifest 中保存这些 provenance；
- 动态调频激活后，V1 不支持 causal/response retime、ROB suffix、corrected suffix
  carry 和 post-commit store 路径；
- 跨后续 DVFS 边界的 outstanding shared response 尚未保存显式物理完成 deadline，
  这是当前需要单独验证和继续修正的精度边界。

## 10. 外部接入检查表

在把一次运行作为有效结果前，外部框架至少检查：

1. 输入 config、manifest、FST 和程序版本已记录；
2. measurement scope 明确且与 trace/profile 一致；
3. manifest ID 连续，活动 trace 数不超过配置核数；
4. reference frequency 和每核初始频率均回显且符合预期；
5. 输出 `schema` 是消费者支持的版本；
6. `window_id` 单调，窗口时间连续，最后一个窗口 `finished=true`；
7. 指令窗口记录并保留 `instruction_overshoot`；
8. 所有 action 长度严格等于 `sim.cores`，不包含 0 Hz；
9. window PMU 直接作为增量消费，不再次做相邻窗口差分；
10. CPI 为 `null` 的零退休窗口不进入 CPI 误差统计。
