# CHANGELOG — gem5-collection

记录 FastSim 采集用 gem5 修改集的构成与来源。事实源为生产工作树
`/data00/yinhaolang/gem5-fs`（上游 `v25.1.0.1` / `c8222cc` 之上的未提交改动），
2026-09-22 导出。

## v1 — 2026-09-22（首次整合）

### 基线

- 上游 `github.com/gem5/gem5`，tag `v25.1.0.1`，commit
  `c8222cc67a399bfc01e8658dd14b30d5bfd634f9`（detached，无本地提交）。

### overlay/（9 个新增文件，上游不存在）

- `src/cpu/o3/probe/tao_trace.cc`（6410 行）— TaoTrace 核心 producer
- `src/cpu/o3/probe/tao_trace.hh`（991 行）
- `src/cpu/o3/probe/TaoTrace.py`（103 行）— SimObject 定义
- `src/mem/taotrace_response.hh`（903 行）— 原生 Ruby 响应侧车
- `src/cpu/o3/kernel_event.hh`（40 行）
- `src/cpu/o3/probe/branch_events.cc`（89 行）
- `src/cpu/o3/probe/branch_events.hh`（46 行）
- `src/cpu/o3/probe/BranchEvents.py`（7 行）
- `build_opts/X86_MESI_Three_Level`（6 行）— 上游只提供 ARM 变体

### patches/0001-fastsim-collection-upstream-hooks.patch（41 个上游文件，855+/35−）

按子系统：

- **O3（12）**：`fetch/decode/iew/commit/lsq/lsq_unit/dyn_inst`、`BaseO3CPU.py`、
  `SConscript`、`probe/SConscript` — 挂接 TaoTrace 探针、TSO store 参数、退休期
  BPred oracle、dTLB 归因等采集挂钩。
- **Ruby 协议 .sm（10）**：`MESI_Three_Level-{L0cache,L1cache,msg}`、
  `MESI_Two_Level-{L2cache,dir,msg}`、`RubySlicc_{Exports,MemControl,Types,Util}`
  — 原生层级 PMU 事件、store-prepare 事务（buffer TSO）。
- **Ruby system/slicc（7）**：`Sequencer.{cc,hh}`、`RubyPort.cc`、
  `AbstractController.cc`、`RubyRequest.hh`、`RubySlicc_Util.hh` — 原生响应侧车、
  store-prepare outstanding 计数、身份闭合。
- **KVM（3）**、**sim/system（3）**、**dev/serial（2）**、**arch/x86 faults（1）**、
  **python cache components（2）** — checkpoint/marker、CR3 边界、采集辅助。

该合并补丁**已包含 buffer TSO 的上游文件改动**（生产树默认 serial）。

### variants/tso-buffer.patch（reference-only）

`--tso-store=buffer` 机制补丁（沿用 `patches/gem5-o3-tso-store-buffer.patch`，
前缀 `gem5-fs/`）。因 buffer TSO 已并入 `0001`，重建后**不要**再 apply 本补丁。
详见 `docs/gem5-spr-tso-store-buffer-design-20260917.md`。

### 排除项

- `*.orig`（patch 备份）、`resources/`（内核/磁盘镜像，运行期输入）、`build/`
  （构建产物）。
</content>
