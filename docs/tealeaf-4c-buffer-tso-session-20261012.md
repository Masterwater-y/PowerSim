# TeaLeaf 4核 buffer-TSO 对账会话纪要（2026-10-12）

本文档整理本次会话的工作内容、关键发现、诊断结论与待决方向，供后续继续或复核使用。
所有临时产物在 `tmp/` 下，源码/配置改动见下文。对话语言：中文。

## 1. 任务目标

复刻丢失的 gem5 全系统（FS）实验环境，对 FastSim 做 CPI/PMU 对账。本轮聚焦
TeaLeaf（811.tealeaf_s）在 **buffer TSO + 全层 LRU** 下的 4 核 10M 验证，并与历史
同配置实验对比。

### 用户关键约束（务必遵守）

1. 彻底抛弃旧树 `/data00/yinhaolang/gem5`，任何动作都不用它；新采集树在
   `/data00/yinhaolang/FastSim/gem5-fs`。
2. 环境照文档部署，不逐个 dry-run 猜补丁。
3. common-end 规则为**首核达标即停**（`first-core-target-common-end-v1`），非旧的全核。
4. TSO：采集端 + 消费端都用 **buffer-114**（严格对称）；消费端用真 buffer
   （`core.tso_store=buffer`），非 serial 近似。
5. cache **每层统一 LRU**（gem5 默认 L2/L3 是 TreePLRU，须全改 LRU）。
6. 构建用 **80% 核心（-j153）**。

## 2. 已完成并验证的工作

### 2.1 配置对称性确认（第一个问题）

- **cache 每层 LRU**：gem5 采集端 `config.ini` 实证 L1D/L1I/L2/L3 全 = LRURP；
  FastSim 消费端经 include 链 + 覆盖，四层全 LRU。两端对称。
  - FastSim 侧：基线 [gem5-v28_1-time-epoch.cfg](file:///data00/yinhaolang/FastSim/configs/gem5-v28_1-time-epoch.cfg)
    L1=lru、L2/LLC=tree_plru；在
    [gem5-fs-native-kernel-buffer114-lru.cfg](file:///data00/yinhaolang/FastSim/configs/gem5-fs-native-kernel-buffer114-lru.cfg#L19-L20)
    显式覆盖 `cache.l2.replacement=lru` + `cache.llc.replacement=lru`。
- **buffer/serial TSO 选项确认**（更正了早先说法）：FastSim 确有 TSO store 模式，
  是一个二选一字段 `core.tso_store ∈ {serial, buffer}`：
  - 枚举 [config.hpp:18-21](file:///data00/yinhaolang/FastSim/include/fastsim/config.hpp#L18-L21)
    `TsoStoreMode{kSerial,kBuffer}`；解析 [config.cpp:361-367](file:///data00/yinhaolang/FastSim/src/config.cpp#L361-L367)。
  - **默认是 serial**（[config.hpp:678](file:///data00/yinhaolang/FastSim/include/fastsim/config.hpp#L678)），
    buffer 深度默认 114（[config.hpp:681](file:///data00/yinhaolang/FastSim/include/fastsim/config.hpp#L681)）。
  - 本轮 cfg 已显式切 buffer-114，并按 [config.cpp:563](file:///data00/yinhaolang/FastSim/src/config.cpp#L563)
    的互斥约束关掉继承的 `store_wait_relief`。
  - 采集端 `config.ini` 实证 `tsoStore=buffer / SQEntries=114 / needsTSO=true`。两端对称。

### 2.2 构建与测试

- `cmake --build build -- -j153` 全部 Built；`./build/fastsim_tests` → `all FastSim tests passed`。
- 运行环境（跑 fastsim / 工具都要）：
  ```bash
  export PYENV_ROOT=/root/.pyenv; export PATH=$PYENV_ROOT/bin:$PATH
  eval "$(pyenv init -)"; pyenv shell 3.11.14
  export LD_LIBRARY_PATH=/opt/gcc-11/lib64:/root/.pyenv/versions/3.11.14/lib:$LD_LIBRARY_PATH
  export TAOGEN_SHARED=/data00/yinhaolang/FastSim/gem5-fs/taogen/shared
  ```
  跑 gem5（KVM）需 `dangerouslyDisableSandbox`。

### 2.3 单核 2M 冒烟对账（pilot，口径修正后）

来源：`tmp/fs_tealeaf_buf114_lru/`（2 核 jsonl 2M 采集，实际只有 core0 有数据）。

- `convert-gem5` 转两核 jsonl→FST；privilege-audit 确认 core0 混合流
  （user 4,973,938 + kernel 20,627，`privilege_feature=true`）。
- **关键口径修正**：首版 manifest 把 warmup 也算进测量，CPI 口径与 gem5 的 2M
  测量窗不符。改用 `fastsim-binary-warmup-slice` manifest 显式切 warmup/measurement
  后，FastSim 测量窗 = 2,000,001 user UOP，与 gem5 oracle `n_user=2000001` 对齐。
  - warmup-slice manifest 格式（见 [trace.cpp:3216-3335](file:///data00/yinhaolang/FastSim/src/trace.cpp#L3216-L3335)
    与 [upgrade_functional_warmup_manifests.py:56-61](file:///data00/yinhaolang/FastSim/tools/upgrade_functional_warmup_manifests.py#L56-L61)）：
    `{core} fastsim-binary-warmup-slice {fst} {core} {warmup_insts} {meas_insts} {warmup_records} {meas_records}`

单核 2M 对账结果（user-plus-kernel，核 core0）：

| 项 | gem5 | FastSim |
|---|---|---|
| cycles | 3,024,209 | 3,905,946 |
| macro-inst | 1,290,485 | 1,290,485 |
| **CPI（宏指令）** | **2.34346** | **3.02673** |

- CPI 绝对误差 = **+0.68326 cycles/macro-inst**；相对 +29.16%。
- PMU（契约 source：cache 用 native-summary 组合非 idle，branch 用 retired-bpred）：
  branch_misses 930→1266（+36%）、l1d_tag_misses 24562→25704（+4.65%）、
  private_l2 10457→8295（−20.68%）、llc 10439→8295（−20.54%）；cache WAPE 11.98%。
- 诊断：CPI 过预测几乎全部来自 `exposed_memory_penalty_cycles`（3.33M/3.9M），
  不是 store buffer。按契约**未调参凑数**。
- **标注为 pilot/diagnostic**：2 核冒烟、target 2M、缺 `request.json`/
  `effective-target.json`/合并 `kernel_events.json`，未过维护版验证器 formal gate。

## 3. 4核 10M 采集：退化成单核（核心问题）

### 3.1 现象

- 首个 4 核 10M FST 采集（`tmp/fs_tealeaf_4c_buf114_lru_10M/`）**4分33秒**跑完
  （KVM 正常，用户经验 10-30 分钟是对的；早先"数小时"估计已更正）。
- 但 **core0 独自跑完 10M（FST 747MB），core1/2/3 = 72 字节（纯 header，零记录）**。
  连 warmup 阶段 core1-3 也是 0。oracle：core0 `n_user=10000000`，core1/2/3
  `n_user=0 / measured_cycles=0 / idle_cycles=0`（捕获流没有执行/自旋记录；
  后续修正：这不证明其他核实际没执行，采集门控也能造成这种现象）。
- guest `Brought up 4 CPUs` 正常，`OMP_NUM_THREADS=4` 正确传入 guest。

### 3.2 逐一排除的假设

1. ❌ **绑核**：去掉 `OMP_PROC_BIND=close`/`OMP_PLACES=cores` 后活跃核从 core0
   变 core2，仍单核。（runner 已改为默认不设 proc_bind/places，见
   [run_fs_spec_workload.py](file:///data00/yinhaolang/FastSim/tmp/run_fs_spec_workload.py)
   新增 `--omp-proc-bind`/`--omp-places`，默认空。）
2. ❌ **串行二进制**：`nm` 见 GOMP_parallel 符号；`objdump` 全binary **70 处
   `call GOMP_parallel`**，GCC 11.5 正常 OpenMP。（早先按符号名反汇编查不到是因
   `-O3` 把 kernel 内联/改名 `.omp_fn`/`.constprop`。）host 上同工具链探针
   `OMP_NUM_THREADS=4` 正常并行（num_threads=4）。
3. ❌ **线程池懒创建**：在 `diffuse.c` checkpoint 前加 OMP 预热并行区（见 3.3），
   重编 `.warm` 二进制后，core0/2/3 从"72字节纯header"变成**有 1-2 条记录**
   （证明预热确实 fork 了 4 线程、各核执行到了），但测量窗一开仍单核主导。
4. ❌ **网格太小**：换 train（2048×3072，630万单元）大网格冒烟，**依然单核**：
   core1 独自跑完 2M（141MB），core0/2/3 `n_user=0 / idle_cycles=0`。

### 3.3 已做的源码/脚本改动（均在 tmp/ 或 workloads 下，诊断用）

- [diffuse.c](file:///data00/yinhaolang/FastSim/workloads/spec_cpu_2026/benchspec/CPU/811.tealeaf_s/src/2d/diffuse.c#L36-L63)：
  checkpoint 前加 OMP 预热并行区（`#pragma omp parallel` 让 libgomp 在 KVM 阶段
  创建全部 worker）。
- 重编到独立二进制（保留原件）：
  `workloads/.../811.tealeaf_s/exe/tealeaf_base.gem5-x86-linux.warm`
  （用 [tmp/build_tealeaf_gem5.sh](file:///data00/yinhaolang/FastSim/tmp/build_tealeaf_gem5.sh)，
  `OUT=....warm bash tmp/build_tealeaf_gem5.sh`）。
- [run_fs_spec_workload.py](file:///data00/yinhaolang/FastSim/tmp/run_fs_spec_workload.py)：
  新增 `--omp-proc-bind`/`--omp-places`，默认不设（避免 fake-CPU 拓扑下 collapse 到核0）。
- 诊断辅助（未成功，readfile 机制问题）：`tmp/diag_omp_guest.py`、`tmp/omp_probe.c`。
- 下游一键对账脚本：[tmp/reconcile_4c.py](file:///data00/yinhaolang/FastSim/tmp/reconcile_4c.py)
  （组 warmup-slice manifest → simulate → 按契约算 CPI+PMU，只对进入测量的核打分）。

### 3.4 原会话根因判断（后续已撤回“确证”）

**2026-10-12 后续修正：** 以下是原会话的判断记录，不能当作已确证根因。
fork-join 与 first-core common-end 不会必然造成 worker 零指令；主线程也可能在
join/barrier 等待。当时近零 worker 记录不能直接等同于线程未执行。历史四核均衡不能证明
必然采用 wave。完整修正与后续方案见
[功能同步 trace 讨论](functional-synchronization-trace-discussion-20261012.md) 和
[gem5 输出首片设计](gem5-functional-sync-output-design-20261012.md)。

撤回的推论是：把主线程串行段与 worker 的间歇活跃，当作 first-core 规则必然
令其余核 `n_user=0` 的证明。这个推论不成立，因为并行区内各线程均能推进，
主线程也会等待；FST 零人口还可能由逐核门控过滤造成。第 8 节给出本轮实际
四核采集和门控对照，保留历史观察而不沿用这项错误归因。

### 3.5 与历史的矛盾 → 用户当前问题的由来

- 历史文档 [tealeaf-full-roi-error-audit-20260910.md](file:///data00/yinhaolang/FastSim/docs/tealeaf-full-roi-error-audit-20260910.md)：
  TeaLeaf 4 核、每核 10M user UOP、合计 40M、Q=1024、带 native-kernel，4 核 cycles
  **均衡**（6.4M/6.5M/4.6M/7.1M），CPI 用 cycles/user-UOP 口径。
- 原会话据此推断历史采用了不同多核机制；后续已收紧：**单进程 OpenMP 也可以在
  first-core 共同终点取得较均衡的四核人口**，不能由均衡数据排除这种机制。
  历史的 launcher、采集边界和线程参与情况仍需原始证据。
- 历史同配置的底层数据已被清理（`tmp/architecture-evidence-20260907.*`、
  `tmp/tealeaf-tail-timing-20260907` 等全部 GONE），只剩文档数值。

## 4. wave 多-worker 绑核机制（已向用户解释）

marker 头 [gem5_roi_marker.h](file:///data00/yinhaolang/FastSim/workloads/spec_cpu_2026/benchspec/CPU/811.tealeaf_s/src/2d/gem5_roi_marker.h)
里**已内置**一套多-worker 绑核基础设施，是历史机制的调查线索，不能单凭它的存在
认定历史四核均衡靠它实现：

- 共享控制块 `gem5_roi_wave_control{ready,start,warmed,roi_go,workers}`（L45-51）。
- 环境变量：`GEM5_WORKER_INDEX`（worker 编号）、`SIDECAR_TARGET_CPUS`
  （各 worker 绑的 CPU，`gem5_roi_worker_cpu()` 用它 setaffinity）、
  `GEM5_ROI_WAVE_CONTROL`（控制块地址）。
- 握手流程 `gem5_roi_wave_checkpoint`（L246-267）/ `gem5_roi_wave_begin`（L269-289）：
  N 个 worker 各绑一核 → checkpoint 栅栏汇合（`ready==workers`）→ worker0 代表全体
  打一次 checkpoint（KVM→O3）并放行 → workbegin 栅栏汇合（`warmed`）→ worker0 开测量
  窗（`roi_go`）→ 此后 **N 个 worker 各自满负荷跑同一份计算、各自连续累计 user UOP**，
  4 核对称推进，first-core 下几乎同时到 target → 得到均衡 4 核结果。
- 现状：基础设施就绪，但 `diffuse.c` 当前调的是简单路径
  `gem5_roi_checkpoint_once`/`gem5_roi_begin_once`，**未走 wave**。
- 启用需要：(1) 改 TeaLeaf 调 `gem5_roi_wave_*`；(2) 写 guest launcher spawn N 个
  worker、设 `GEM5_WORKER_INDEX`/`SIDECAR_TARGET_CPUS`/共享控制块；(3) 改 boot 调 launcher。
- 这是**显式多副本（类 SPEC rate）**模式——4 worker 各算各的，非 4 核协作算一个问题。
- **证据边界**：尚无证据证明历史就是用 wave 跑的；设施存在和历史均衡并不足以
  支持唯一归因。另一套 `sidecar_roi.h`（`TCSIM_NATIVE_ROI`+`SIDECAR_ROI_FD`）
  是宿主侧 launcher 协议，适合 SE 模式，不适合本 FS KVM 场景。

## 5. 用户同步顺序提问（已回答，后续要求实现采集输出）

回答与跨 QEMU/DynamoRIO 采集、FastSim 建模、精度和成本边界已整理到
[功能同步 trace 讨论](functional-synchronization-trace-discussion-20261012.md)。
用户随后要求先做 gem5 侧输出并验证，设计见
[gem5 输出首片设计](gem5-functional-sync-output-design-20261012.md)。

> 如果使用 OpenMP 这一套，说明某个线程的指令流在时间上不是连续的，如果是的话，
> 如何让 FastSim 能够识别出这种顺序？

要点（待展开）：OMP 下单个 worker 线程的指令流在时间上**确实不连续**——它在并行区活跃、
区间 barrier 休眠，是"活跃段 + 空洞"交替。需讨论：
- TaoTrace 当前如何对每核打时间戳/ordinal、如何处理 idle（PAUSE/barrier 自旋被
  当 idle 过滤，见 `docs/fst-native-kernel-trace.md` 测量边界与 idle 策略）。
- FastSim 回放是按 per-core 记录流 + interval/time-epoch 调度；跨核时间关系如何对齐
  （common_end_tick、per-core 时间戳）。
- 若要让 FastSim 识别 worker 的断续活跃并正确穿插多核时序，需要什么输入
  （每记录的全局 tick？barrier 边界？）与模型支持。

## 6. 原会话待决方向（历史选项，本轮未选择）

1. **启用 wave 多-worker 绑核**：改造 TeaLeaf + 写 launcher + 改 boot，复刻历史均衡 4 核。
2. **承认单核有效**：用已采 10M 混合流做正式单核 CPI+PMU 对账，不追求 4 核均衡。
3. **先查证历史 wave 机制**：git 历史/旧 launcher/输入规模，确认机制后再精确复刻。

（用户随后转为先理解"wave 是什么"，现又追问 OMP 时间不连续如何让 FastSim 识别。）

本轮用户已选择按同步输出设计实施；以下第 8 节记录实际完成状态。

## 7. 关键路径速查

- 采集 runner：[tmp/run_fs_spec_workload.py](file:///data00/yinhaolang/FastSim/tmp/run_fs_spec_workload.py)
- 消费 cfg：[configs/gem5-fs-native-kernel-buffer114-lru.cfg](file:///data00/yinhaolang/FastSim/configs/gem5-fs-native-kernel-buffer114-lru.cfg)
- gem5.opt：`gem5-fs/build/X86_MESI_Three_Level/gem5.opt`（含全 LRU 重编）
- TeaLeaf 源：`workloads/spec_cpu_2026/benchspec/CPU/811.tealeaf_s/src/2d/`
- 输入规模：test 512×512 / train 2048×3072 / refspeed 3072×3072（历史用哪个未定论）
- 对账契约：[docs/accuracy-reporting-contract.md](file:///data00/yinhaolang/FastSim/docs/accuracy-reporting-contract.md) 第 0、8 节
- native 真值工具：`tools/run_native_kernel_fastsim_validation.py`、`tools/validate_fs_oracle_identity.py`

## 8. 同步输出实施与本轮证据

gem5 FS 首片已完成，详见 [实施与验证报告](gem5-functional-sync-output-results-20261012.md)。
独立 GCC 11.5 libgomp 输出语义 marker，gem5 在成功提交且保留 FST 记录时捕获
六个参数，功能 sidecar 和提交时刻 oracle 分开；FastSim consumer 留待下一片。

- 四线程三轮 probe：102 events、3 regions、9 barriers，完整验证通过。
- TeaLeaf 512×512、first-core target=300k：149 events、9 regions、8 barriers；
  实际四核 measurement user-UOP 为 272,761 / 300,000 / 277,081 / 256,663。
  共同终点保留一个未闭合 region，显式允许截断时 valid=true、complete=false。
- 专用 runner 的旧 `require_roi=True` 依赖每个 CPU 自己收到 WORKBEGIN；
  主线程单次 marker 无法为其他核开门。四核 probe 复现了 worker 被过滤，
  新 runner 改由 functional warmup、全局 measurement marker 和共同终点控制。
  这解释了本轮门控对照，不重新声称已唯一确定全部历史失败的原因。
- 新增 16 个协议验证测试和 3 个真实 runtime integration 测试通过；FastSim
  build/C++ 回归通过。SE 路径因既有页表 snapshot 不兼容明确拒绝。
- 未完成严格 observer 耗时对照，也未测 FastSim 精度/吞吐收益；不能由输出
  验证通过推导模型精度提高。全量 Python 环境错误和复现命令见报告。
