# CHANGELOG — gem5-collection

记录 FastSim 采集用 gem5 修改集的构成与来源。事实源为生产工作树
`/data00/yinhaolang/gem5-fs`（上游 `v25.1.0.1` / `c8222cc` 之上的未提交改动），
2026-09-22 导出。

## v1 — 2026-09-22（首次整合）

### 基线

- 上游 `github.com/gem5/gem5`，tag `v25.1.0.1`，commit
  `c8222cc67a399bfc01e8658dd14b30d5bfd634f9`（detached，无本地提交）。

### overlay/（新增文件，上游不存在）

- `src/cpu/o3/probe/tao_trace.cc`（6410 行）— TaoTrace 核心 producer
- `src/cpu/o3/probe/tao_trace.hh`（991 行）
- `src/cpu/o3/probe/TaoTrace.py`（103 行）— SimObject 定义
- `src/mem/taotrace_response.hh`（903 行）— 原生 Ruby 响应侧车
- `src/cpu/o3/kernel_event.hh`（40 行）
- `src/cpu/o3/probe/branch_events.cc`（89 行）
- `src/cpu/o3/probe/branch_events.hh`（46 行）
- `src/cpu/o3/probe/BranchEvents.py`（7 行）
- `build_opts/X86_MESI_Three_Level`（6 行）— 上游只提供 ARM 变体
- `taogen/shared/uarch_profile.hh`、`taogen/shared/lru_banked.hh` — µarch oracle
  共享头，`tao_trace.hh` 以裸名 `#include`，编译期经 `TAOGEN_SHARED` 引入（见下）。

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

### 共享头依赖收敛（taogen → FastSim 唯一信源）

`overlay/src/cpu/o3/probe/tao_trace.hh` 用裸名 `#include "uarch_profile.hh"` /
`#include "lru_banked.hh"`，这两个头**不在 gem5 树内**，历史上由 probe 的
`SConscript` 经 `TAOGEN_SHARED`（默认回退到 gem5 树同级的 `../taogen/shared`）在
**编译期**引入。它们的 owner 是 minesim 仓 `taogen` 分支，但该分支自 2026-06 起
冻结（最后提交 `a3296da`，2026-06-02），后续修改（在用工作树为 2026-08-18）实际
都在 FastSim 采集工作流里发生且**未提交**——即一处未版本化的外部编译期依赖。

本次将其 2026-08-18 在用工作树版本作为**唯一信源**纳入
`overlay/taogen/shared/`，协作者按 `TAOGEN_SHARED=<GEM5>/taogen/shared` 即可闭合
依赖，**无需任何外部 taogen 目录**。sha256 见 `manifest.json` 的
`apply.overlay.shared_headers`。`probe/SConscript` 未改（仍读 `TAOGEN_SHARED`），
故 `0001` 合并补丁不变。

部署侧 `mesi_ref_sim`（与 oracle bit-exact 共用同一份头）也一并改为引用 FastSim
的这份，见其 `CMakeLists.txt` 的 `target_include_directories`（详见提交说明）。

### variants/tso-buffer.patch（reference-only）

`--tso-store=buffer` 机制补丁（沿用 `patches/gem5-o3-tso-store-buffer.patch`，
前缀 `gem5-fs/`）。因 buffer TSO 已并入 `0001`，重建后**不要**再 apply 本补丁。
详见 `docs/gem5-spr-tso-store-buffer-design-20260917.md`。

### 排除项

- `*.orig`（patch 备份）、`resources/`（内核/磁盘镜像，运行期输入）、`build/`
  （构建产物）。
</content>
