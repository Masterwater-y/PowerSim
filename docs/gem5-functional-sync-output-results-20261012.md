# gem5 功能同步输出：实现与验证结果

日期：2026-10-12。状态：Linux x86-64 / GCC 11.5 libgomp / gem5 FS 首片已实现并验证。
本片只输出功能同步信息；FastSim consumer 尚未实现，本文不报告 CPI/PMU 精度收益或
FastSim 吞吐变化。设计见 [首片设计](gem5-functional-sync-output-design-20261012.md)，
跨 QEMU/DR 的后续接口与模型边界见 [讨论文档](functional-synchronization-trace-discussion-20261012.md)。

## 1. 已实现的链路

```text
GCC 11.5 libgomp 语义边界
    -> fastsim_sync_marker 六参数 ABI（单条 nop + ret）
    -> gem5 O3 成功 Commit observer
    -> functional-sync-coreN.jsonl + metadata
    -> FST / ASMAP / functional-boundary 对账
```

- [runtime 增量 patch](../patches/libgomp-11.5-functional-sync.patch)：团队实例单调 ID、
  TLS 软件线程 ID、parallel/task begin/end、normal/final barrier arrive/depart，
  以及 `gomp_work_share_end` 的隐式 barrier。
- [独立构建工具](../tools/build_sync_libgomp.py)：GCC 11.5 源副本与配置头放在
  `tmp/`，重编全部 44 个 runtime 对象；不覆盖安装库、原 build 或原 workload binary。
- [collector 增量 patch](../patches/gem5-taotrace-functional-sync.patch)：只包含本片
  对本仓库 `gem5-fs/` 三个 TaoTrace 文件的新增改动，不夹带既有 gem5 改动。
  本片已在实际 gem5 树应用并成功重编。
- [FS runner](../scripts/run_gem5_sync_probe.py)：核实静态非 PIE ELF64 x86-64 binary、
  唯一 marker 符号，保存 binary SHA256、marker 地址和参数；输出默认关闭，
  用 `--functional-sync` 启用。`--sync-initial-context` 默认 unknown，不能默认宣称空状态。
- [验证器](../tools/validate_functional_sync.py)：检查真实 FST marker 人口、PC、
  user/non-memory/retired 锚点、ASMAP、warmup/measurement 边界、参与者、master/成员
  身份、barrier 轮次递进、未完成等待，以及共同终点。

输出文件为：

| 文件 | 语义 |
|---|---|
| `functional-sync-coreN.jsonl` | 功能事实，无 gem5 tick、等待周期、命中结果 |
| `functional-sync-metadata-coreN.json` | 源文件/记录数、事件数、能力、边界和身份类型 |
| `oracle/functional-sync-observation.jsonl` | 仅离线核查的全局观察序号和 commit tick |
| `sync-run-request.json` / `sync-run-result.json` | binary 身份、runner 参数、实际退出原因 |

FST 仍为 v7 / 64-byte 热记录；observer 不补、删、改原始功能记录。
参数从成功提交后架构寄存器读取，不在可被 squash 的 Execute 回调发事件。
功能 sidecar 与时间 oracle 分开，未来 consumer 不能把 oracle 总顺序当作目标调度。

## 2. 验证结果

所有 FS case 都是输出/覆盖诊断；完整协议不等于 formal CPI/PMU gate。
配置实证：4 个 BaseO3CPU 均为 `tsoStore=buffer / tsoStoreBufferSize=114 / SQEntries=114`，
所有配置中的 replacement_policy 均为 LRURP。没有修改 FastSim 参数来拟合结果。

| Case | 事件数 | 并行区实例 | barrier 轮数 | 各核事件数 | 判定 |
|---|---:|---:|---:|---|---|
| 四线程三轮、显式 + final barrier | 78 | 3 | 6 | 27 / 17 / 17 / 17 | valid=true, complete=true |
| 加 dynamic `omp for` 隐式 barrier（最终 runtime-v2） | 102 | 3 | 9 | 33 / 23 / 23 / 23 | valid=true, complete=true |
| 首核 3000 user-UOP 截停（最终 runtime-v2） | 6 | 1 | 1 | 6 / 0 / 0 / 0 | 允许截断时 valid=true, complete=false，1 个开区间 |
| TeaLeaf test 512×512，首核 300k user-UOP（最终 runtime-v2） | 149 | 9 | 8 | 33 / 50 / 33 / 33 | 允许截断时 valid=true, complete=false，1 个开区间 |

证据：

- [78-event 验证](../tmp/functional-sync/fs-global-on-validation.json)
- [最终 102-event 验证](../tmp/functional-sync/fs-v2-workshare-on-validation.json)
- [真实 common-end 截断验证](../tmp/functional-sync/fs-v2-common-end-validation.json)
- [TeaLeaf 同步验证](../tmp/functional-sync/tealeaf-v2-4c-validation.json)
- [最终 runtime 构建清单](../tmp/functional-sync/runtime-v2/build-manifest.json)
- [gem5 构建日志](../tmp/functional-sync/gem5-build.log)

两个完整 probe 都返回 `result=599000 expected=599000`。最终输出关闭对照使用
**同一个 binary SHA256**，同样返回正确结果，且没有同步 sidecar 或观察 oracle。
ON 与 OFF 的 FST 均保留 102 条 marker 指令。

ON/OFF 分别独立 KVM boot，不能把两者的整个 FST 字节、核间进度或模拟时间差
当作 observer 效应：实测总记录数分别为 3,052,102 / 3,015,318，调度和 kernel 前缀
不同。未用共享 checkpoint 做严格 observer 开销对照，没有给出耗时损失百分比。

TeaLeaf 的**实际测量 user-UOP 人口**为：

| 核 | user UOP |
|---|---:|
| 0 | 272,761 |
| 1 | 300,000 |
| 2 | 277,081 |
| 3 | 256,663 |

首核达标后共同关闭，未补齐其他核。该结果证明当前采集机制可以在单进程 OpenMP
中捕获四核工作与跨核同步，进一步否定“fork-join 必然使其他核零指令”的原判断。
它不是历史 10M 环境的完整复刻，也不是准确性或 throughput 测量。

## 3. 发现的采集门控问题

专用 runner 最初继承旧 `require_roi=True`。现有 TaoTrace 的 `emitGateOpen(core)`
按每个 CPU 自己的 WORKBEGIN 开门；OpenMP 主线程仅在一个核执行 WORKBEGIN，
worker 迁到其他核后便被过滤。

绑核 probe 在旧门控下只留下 27 个事件，其他核零事件；验证器明确报出
“departure before all participants arrived”，没有误判为同步采集成功。
见 [失败对照](../tmp/functional-sync/fs-pinned-on-validation.json)。

本次专用 runner 使用 `require_roi=False`，由 `functional_warmup`、全局
`traceMeasurementBegin` 和 first-core common-end 控制采集/测量范围；因此不是
删除 ROI 或补事件。旧 runner 和既有生产模型不在这次改动中。

此外，极短 probe 在默认 1ms KVM quantum 下可能在正确计算的同时漏掉用户捕获
边界；本片诊断设置 10us。保留 [默认 quantum 失败日志](../tmp/functional-sync/fs-on.log)。
门控错误的归因有代码和 probe 对照；不能单凭本轮 quantum 对照重新归因所有历史 case。

## 4. 审阅后补齐的完整性检查

- work-sharing 的隐式 barrier 直接调用 wait_start/wait_end，不能仅包装普通 barrier
  函数；已加入 work.c 路径，并以真实 dynamic `omp for` 验证。
- taskloop、普通 task、取消/可取消 barrier 和 nested level > 1 不能被认证为本片
  完整支持；实际遇到时输出 kind=9 unsupported，验证器拒绝完整认证。
- master 可以在 worker 发 departure 前复用 team，甚至改 worker 的 team_id。
  runtime 在安全 begin 点缓存 region/member/team size；后续事件使用 TLS 缓存，
  避免迟到 departure 被归给下一轮。nested unsupported 为 sticky 状态。
- master 身份与 parallel_begin 绑定，成员身份在整个 region 中保持单射。
- 每个成员只有一个 outstanding barrier；正常 barrier 未离开时不能结束 task，
  generation 必须按 GCC barrier 的 8 步递进，同一 region 只有一个 final barrier。
- 地址空间和 phase 必须与真实 `.asmap` 和功能边界相符；不能只校验 JSON 字段。
- 缺 marker、错误 ordinal、提前离开、错 schema、未知初始上下文与未声明的中途
  EOF 均失败；合法 common-end 截断需要显式 `--allow-truncated`，且不伪造 closure。

独立 code review 的重要问题已修复；最终真实 workshare 和 common-end case 也由
独立 reviewer 重新验证。

## 5. 成本与限制

最终 102-event probe 的功能 sidecar 为 **31,829 B**，FST 为 **195,347,872 B**，
即约 **0.0163%**；单独的观察 oracle 为 **12,084 B**。这些是该诊断的存储观测，
不是 FastSim 吞吐或采集耗时的承诺。

runtime marker 插桩本身会增加指令与状态访问。ON/OFF 同一个已插桩 binary 只
控制 observer，不能证明插桩相对 pristine runtime 没有扰动。精度和性能仍需后续
在同一功能工作范围、可靠边界和安静宿主环境下测量。

当前限制：

- 仅验证 Linux x86-64、静态非 PIE、GCC 11.5 libgomp、gem5 FS/direct FST。
- SE 的既有功能采集路径会尝试用 FS 物理页表方式读 SE 表；实际 pilot 被拒绝。
  [SE 前置门禁](../scripts/run_gem5_sync_se.py) 现明确退出，避免空 trace 被当作成功。
- 没有 OS TID/调度/迁移/wakeup 真值；thread_id 是 runtime trace-local ID。
- 没有精确 spin/sleep 切分、ROI 初始同步 snapshot、task/lock/condvar 全覆盖。
  初始上下文无法独立证明为空时必须保持 unknown。
- 验证器 mmap 扫描 FST，仅把稀疏同步事件及 region 状态放入内存；尚未实现完全
  流式的同步事件验证，密集同步大 trace 仍有 O(events) 内存成本。
- 最后一轮 worker departure 可能在程序终止前未提交；不能伪造该事件，当前严格
  complete 门禁可能将这种输出拒绝或列为 partial。host 测试观察器只在进程退出前
  等回调结清，不在每次 join 阻塞 master，保留团队复用的测试能力。
- 当前没有 FastSim consumer，因此没有 CPI/PMU gain 或 FastSim throughput 结果。

## 6. 回归与复现

默认 FastSim build 和 `./build/fastsim_tests` 通过。新增 **16 个验证器测试 + 3 个
真实 runtime integration 测试**通过，包含先失败再修复的反例。
静态 libgomp 链接有 upstream OpenACC dlopen/glibc 警告，未发生功能失败。

全量 Python suite 的结果与环境缺失单列在日志中，不能写成全量绿色：
`fastsim_py` 模块未构建/不在路径；两个既有 checkpoint-recovery 测试所需的
`/data00/yinhaolang/TCSim/configs/gem5/x86_fs_kvm_boot_checkpoint_tao.py` 缺失。
最终运行 223 项，3 errors、5 skipped、0 failures；运行时设置
`LD_LIBRARY_PATH=/opt/gcc-11/lib64`。见 [全量检查日志](../tmp/functional-sync/python-tests-final4.log)。
两份增量 patch 都通过 `patch --dry-run --fuzz=0`；gem5 patch 从备份应用后与
实际修改的三个文件逐字节一致。

独立 runtime 构建（选择一个新的输出目录）：

```bash
/root/.pyenv/versions/3.11.14/bin/python3 tools/build_sync_libgomp.py \
  --source /data00/yinhaolang/pkgs/gcc-11.5.0 \
  --configured /data00/yinhaolang/pkgs/gcc-11.5.0-build/x86_64-pc-linux-gnu/libgomp \
  --output tmp/functional-sync/runtime-new --jobs 16
```

FS probe 编译（换成刚构建的 archive；保持原 artifact）：

```bash
/opt/gcc-11/bin/gcc -O2 -g -gdwarf-4 -fopenmp -static -no-pie \
  -DFASTSIM_FS_ROI \
  -Iworkloads/spec_cpu_2026/benchspec/CPU/811.tealeaf_s/src/2d \
  tests/workloads/openmp_sync_probe.c \
  tmp/functional-sync/runtime-new/libgomp.a -pthread -ldl \
  -o tmp/functional-sync/sync-probe-new
```

采集：

```bash
LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.11.14/lib \
gem5-fs/build/X86_MESI_Three_Level/gem5.opt \
  --outdir=tmp/functional-sync/fs-new \
  scripts/run_gem5_sync_probe.py \
  --workload-binary tmp/functional-sync/sync-probe-new \
  --workload-args '4 3 200 1 1' --omp-threads 4 \
  --functional-user-target 5000000 --functional-sync \
  --sync-initial-context empty --kvm-quantum-us 10 --omp-wait-policy PASSIVE
```

`empty` 只适用于已知初始状态为空、完整采集前史的这个 probe；一般应用不能直接照抄。
即使 master 已在团队外，旧 worker 的 final departure marker 仍可能尚未完成；
仅“checkpoint 在 join 之后”不足以证明 transport 前史为空。后续 TeaLeaf 10M
实验复现了这种缺口，改为在首个 OpenMP team 之前切换并捕获完整初始化，见
[四核同步 trace 与 CPI/PMU 验证](tealeaf-4c-sync-trace-accuracy-20261012.md)。验证与测试：

```bash
/root/.pyenv/versions/3.11.14/bin/python3 tools/validate_functional_sync.py \
  --trace-dir tmp/functional-sync/fs-new/tao_trace --output tmp/functional-sync/fs-new/validation.json
/root/.pyenv/versions/3.11.14/bin/python3 -m unittest discover -s tests -p test_functional_sync.py
FASTSIM_SYNC_LIBGOMP=/data00/yinhaolang/FastSim/tmp/functional-sync/runtime-new/libgomp.a \
  /root/.pyenv/versions/3.11.14/bin/python3 -m unittest discover -s tests -p test_sync_runtime.py
```

真实 common-end 诊断用 `--workload-args '4 30 200 1 1'` 和 target=3000；验证时显式
加 `--allow-truncated`。TeaLeaf 本次请求/身份见
[run request](../tmp/functional-sync/tealeaf-v2-4c/sync-run-request.json)。
