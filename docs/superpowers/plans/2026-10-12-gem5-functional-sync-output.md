# gem5 Functional Synchronization Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 输出可锚定 FST、可审计团队与 barrier 关系的 gem5 功能同步 sidecar。

**Architecture:** 独立 GCC 11.5 libgomp 在运行时语义边界调用六参数 marker；gem5 O3
Commit observer 捕获架构参数与 FST ordinal。功能数据和提交顺序 oracle 分开，Python
验证器检查真实 FST 锚点及同步生命周期。FastSim consumer 不在本片范围。

**Tech Stack:** C/libgomp、x86-64 ABI、gem5 C++/SimObject、Python 3.11/unittest。

**Spec:** `docs/gem5-functional-sync-output-design-20261012.md`。

## Global Constraints

- 使用本仓库 `gem5-fs/`，不使用旧 `/data00/yinhaolang/gem5`。
- Linux x86-64、GCC 11.5 libgomp、静态非 PIE binary、direct FST；输出默认关闭。
- 不改 FastSim 模型、不扩大 FST 64-byte 热记录、不消费 oracle 时间驱动模拟。
- 保持原编译器、runtime 安装、原 workload 和历史结果；源副本/实验在 `tmp/`。
- first-core common-end；合法边界截断不补事件，未知初始同步状态不认证为完整。
- gem5 构建约 80% CPU（本机 192 核，`-j153`）；FastSim 使用默认 build/tests gate。

## Review Focus

- 重复使用 team/object 地址必须仍区分不同 region。
- 多流文件读序不能作为跨线程观察序；使用单独 oracle 的唯一事件键。
- ROI 可在 barrier 中途结束；完整/截断/未知初始上下文要分开。
- final barrier 的线程可能在 master join 后才打印 depart；不能要求所有 depart 先于 join。
- marker 参数必须来自成功提交时架构状态，并且对应实际保留的 FST 记录。

### Task 1: Protocol validator

**Files:** Create `tools/validate_functional_sync.py`, `tests/test_functional_sync.py`。
**Interfaces:** `validate(trace_dir: Path, allow_truncated: bool=False) -> dict`；CLI
`--trace-dir PATH --output PATH [--allow-truncated]`；非完整/异常默认返回非零。

- [x] 写实际 FST/sidecar/oracle fixture，正向两线程 final barrier；负向重复 arrival、
  提前 depart、非法 ordinal/PC、错轮次、未声明 EOF 和错误 source population。
- [x] 运行 unittest，确认由于 validator 缺失失败。
- [x] 实现 schema、真实 FST 锚点、oracle 键和 lifecycle 检查，正确报告截断。
- [x] 验证全部正负 fixture；禁止 unknown/unsupported 被认证完整。

### Task 2: Runtime instrumentation and build

**Files:** Create `tools/build_sync_libgomp.py`, `patches/libgomp-11.5-functional-sync.patch`,
`tests/workloads/openmp_sync_probe.c`, `tests/workloads/sync_host_capture.c`。
**Interfaces:** 独立输出 `libgomp.a`；marker symbol `fastsim_sync_marker` 六参数 ABI；
event IDs 1/2 parallel begin/end，3/4 implicit task begin/end，5/6 barrier arrive/depart，
7/8 final barrier arrive/depart，9 unsupported。

- [x] host capture 以 strong marker 覆盖 weak no-op transport，检查真实 libgomp 事件；
  缺失插桩 runtime 时 integration gate 应拒绝“成功”。
- [x] 从 GCC 源/已配置 build 创建隔离副本，apply patch，重编所有受私有布局影响的对象。
- [x] 插入 region、TLS thread ID、主/worker begin/end、normal/final barrier 和 unsupported。
- [x] host 两轮团队复用、非对称工作、显式 barrier 检查成员数/轮次/计算结果。

### Task 3: gem5 commit observer

**Files:** Modify `gem5-fs/src/cpu/o3/probe/{TaoTrace.py,tao_trace.hh,tao_trace.cc}`；
Create `patches/gem5-taotrace-functional-sync.patch`。
**Interfaces:** `functional_sync_marker_pc=0` 默认禁用；输出 functional-sync-coreN.jsonl、
sync metadata 和 oracle/functional-sync-observation.jsonl。

- [x] 接入前验证配置要求新参数时旧 binary 拒绝。
- [x] 备份待改 gem5 文件，增加成功提交/FST 保留检查和 payload 有效性。
- [x] 编译当前 gem5；启用条件错误应 fail closed，off 不写 sidecar。
- [x] 从备份生成仅本片增量 patch，不夹带现有 gem5 改动。

### Task 4: Collection pilots and report

**Files:** Create `scripts/run_gem5_sync_probe.py` 与实施报告文档；按需创建 FS runner。

- [x] SE/FS pilot 运行实际多线程 binary；至少一组各成员与 barrier 轮次通过验证。
- [x] 相同插桩 binary 输出 on/off；检查目标结果和 FST population，记录 sidecar 大小。
- [x] 显式共同终点截断测试；FS/TeaLeaf 有限诊断如覆盖不足明确报告。
- [ ] 交替重复采集测 observer 宿主成本，与 runtime 插桩扰动和 FastSim 吞吐分开。
  未完成：独立 KVM boot 不满足严格逐值对照；本片仅报告存储成本，不给耗时百分比。
  依首片完成条件第 8 节显式列出未通过的成本测量，不作为采集链路通过的证据。
- [x] 默认 FastSim build/tests、Python tests、新增 patch 检查；最终独立审阅并修复问题。
- [x] 更新设计状态、实际验证证据、限制与复现命令，提交明确文件范围。

## Execution ledger

- 2026-10-12：用户明确要求“按照设计开始实施”，按当前会话直接执行。
- Ruling: 在已指定的 FastSim/gem5-fs checkout 实施；runtime 与实验在 tmp 中隔离，
  gem5 增量备份后导出 patch。这里的嵌套 gem5 树含大量已存在未提交改动，创建空的
  FastSim git worktree 会丢失它们，不转移或覆盖这些改动。

- Task 1: 最终 16 正负 fixture 通过；包含漏 marker 人口拒绝、延迟 worker departure 和合法截断。
- Task 2: 独立重编 44 个 libgomp 对象；host 四线程三轮团队复用、normal/final barrier 通过。
- Task 3: gem5 完整链接通过；旧 binary 新参数拒绝已记录，增量 patch 已生成。
- Ruling: SE 功能 warmup 启动触发既有 guest-PTE snapshot，SE 仿真页表不是 FS 页表，
  当前 SE pilot 被拒绝；不扩本片范围去修复旧采集器的 SE 页表路径。主要验收转到 FS。
- Investigation: FS 短 probe 在默认 1ms KVM quantum 下虽输出正确计算结果，功能流
  仅有 kernel 前缀、零 user/marker；不能当同步输出成功。独立改变 quantum 到 10us
  复查边界延迟，保留失败 pilot。

- FS 完整性：fs-global-on 实测四核 78 events、3 regions、6 barriers，验证通过。
- Root cause: 专用 runner 继承的 require_roi=True 使用 per-core WORKBEGIN gate，
  导致 worker 迁移到未打 marker 的核后 FST 与同步事件被过滤。新 runner 用
  require_roi=False，仍由 functional_warmup/全局测量 marker/common-end 控制范围。
- Review fixes: 固定成员身份、pending barrier/generation 递进、ASMAP/phase 对账；
  runtime 增加 work-share barrier 和 taskloop/cancellation unsupported；TLS 缓存
  region/member/team size，避免 master 复用 team 影响迟到的 worker departure。
- Runtime-v2: 3 项真实 host integration 通过；旧 v1 复现漏 work-share barrier、
  漏 taskloop unsupported，并在重复运行中复现迟到 departure 身份问题。

- Final FS: runtime-v2 workshare probe 102 events / 3 regions / 9 barriers，完整认证；
  同一 binary ON/OFF 计算结果及 marker 人口一致，OFF 无同步输出。
- Final truncation: first-core 3000 case 6 events；TeaLeaf 512×512 / first-core 300k
  case 149 events，四核都有实际 user-UOP；两者 allow_truncated 后 valid=true、
  complete=false，不补齐事件或其他核人口。
- Test lifetime fix: 真实 host 程序退出可杀掉尚未发 final departure 的 worker；
  strong 测试观察器仅在进程退出前等待已观察 arrival 对应的 departure，
  保留每次 join 后 master 提前复用 team 的行为，production runtime 不加等待。
- Final gates: 16 validator + 3 runtime tests 通过；FastSim build/C++ tests 通过。
  全量 Python 223 项，3 个环境 errors、5 skipped、无 failures；缺 fastsim_py
  及旧 TCSim checkpoint wrapper，不改本片之外的环境。
- Patch gates: GCC pristine source 零 fuzz dry-run 通过；gem5 备份零 fuzz apply 后
  三文件与实际树逐字节一致。独立 reviewer 复验真实 FS 和截断 case，无重要遗留项。
- Scope: SE 当前 fail closed；完全流式 sidecar 验证、严格耗时对照、FastSim consumer
  均未交付。实施/限制/复现见 docs/gem5-functional-sync-output-results-20261012.md。
