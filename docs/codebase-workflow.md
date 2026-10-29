# FastSim Codebase repository and workflow

This document is the durable handoff for future agents working in
`/data00/yinhaolang/FastSim`. It records the repository identity, validation
commands, documentation map, and the safe commit/push procedure when the local
`.git` directory is unavailable.

## 1. Canonical repository mapping

| Item | Value |
|---|---|
| Hosting service | ByteDance Codebase |
| SSH remote | `git@code.byted.org:yinhaolang/minesim.git` |
| Project branch | `FastSim` |
| Local work tree | `/data00/yinhaolang/FastSim` |
| Commit identity | `yinhaolang <yinhaolang@BYTEDANCE.COM>` |
| Last verified push when this document was created | `d03e64fe765e0e44461c26beaa9c5729c97d36e0` |

The remote repository uses separate branches for multiple simulators. At the
time of writing it includes `FastSim`, `LLMSim`, `MTAO`, `TCSim`, `TSim`,
`archsim`, `dev`, `main`, and `taogen`. FastSim changes belong on `FastSim`.
Always query the remote before relying on the recorded commit hash because it
is historical state, not a permanent branch head.

```bash
git ls-remote git@code.byted.org:yinhaolang/minesim.git refs/heads/FastSim
```

## 2. Managed-workspace `.git` behavior

Some managed agent sessions expose `FastSim/.git` as an empty read-only
directory or mount. Symptoms include:

```text
fatal: not a git repository
dr-xr-xr-x ... .git
```

Do not run `git init`, create a new history, or guess a remote in this state.
The safe approach is to clone only the remote branch metadata into the
project-owned `tmp/` directory, populate its index, and use the existing
FastSim directory as its work tree.  Do not use the host `/tmp` for FastSim
metadata or experiment artifacts.

```bash
git clone --no-checkout --single-branch --branch FastSim \
  git@code.byted.org:yinhaolang/minesim.git \
  /data00/yinhaolang/FastSim/tmp/codebase-sync

git \
  --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  reset --mixed HEAD
```

`reset --mixed HEAD` initializes the temporary index from the remote commit;
it does not replace work-tree files. Never substitute `reset --hard`.

Define the following notation mentally for later commands:

```text
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
    --work-tree=/data00/yinhaolang/FastSim <command>
```

Use the full command explicitly in automation so there is no dependence on a
shell alias or environment variable.

## 3. Inspecting and selecting changes

First inspect all modifications relative to the remote `FastSim` branch:

```bash
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  status -sb --untracked-files=all

git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  diff --stat
```

The normal critical-file scope is:

- build definitions: `CMakeLists.txt`;
- public documentation: `README.md`, curated files under `docs/`;
- model configuration: `configs/`;
- C++ implementation: `include/fastsim/`, `src/`;
- regression tests: `tests/`;
- maintained collection, conversion, audit, and validation utilities:
  `scripts/`, `tools/`;
- workload source and Makefiles when they are intentionally changed.

Do not stage these by default:

- `build*/` and `Testing/`;
- `tmp/`;
- generated workload binaries;
- bulk experiment output under `results/`;
- caches, traces, model artifacts, logs, or credentials.

Use explicit paths with `git add`; do not use `git add -A` in a mixed work
tree. Before committing, run:

```bash
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  diff --cached --stat

git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  diff --cached --check
```

## 4. Build and test gate

Current trace storage policy (user update 2026-10-28): total FST <=400GB
(400,000,000,000 bytes), stop at395GB and preserve30GB free disk after the user's explicit2026-10-28 adjustment, superseding the prior50GB free reserve and300GB,200GB,100GB and older20GB total limits. The C16 recapture budgets are32GB for Marian and48GB for FLAC. Both C16 producers are running concurrently; canonical combined status is `tmp/spec-speed-32g-c16-recapture-20261028-parallel/matrix-status.json`. The original FLAC attempt was interrupted during a failed live policy synchronization; its failed FST was retired with receipts and FLAC restarted from the verified checkpoint in `tmp/spec-speed-32g-c16-recapture-20261028-flac-retry1/`. Ten existing sources retain parallel FastSim replay/revalidation. See [`spec-speed-32g-recapture-and-validation-20261028.md`](spec-speed-32g-recapture-and-validation-20261028.md). Delete
obsolete/failed/superseded FST with explicit file/hash receipts; preserve active
regression and accepted workload inputs. See
[`trace-storage-policy-20261015.md`](trace-storage-policy-20261015.md) and
[`trace-storage-policy.json`](../configs/trace-storage-policy.json).

User instruction (2026-10-13): compile gem5 using 80% of the currently
available CPU cores, rounded down (minimum one). Do not default to `-j8`.
Use the process affinity count so restricted CPU sets are respected:

```bash
gem5_jobs=$(python3 -c 'import os; print(max(1, len(os.sched_getaffinity(0)) * 80 // 100))')
scons build/X86_MESI_Three_Level/gem5.opt -j"$gem5_jobs"
```

For this workspace's gem5 Python scripts, explicitly select Python 3.11 via
`PYTHON_CONFIG=/root/.pyenv/versions/3.11.14/bin/python3-config`; embedding
Python 3.8 can compile but fails importing the current Simulator module.

The normal local gate is:

```bash
cd /data00/yinhaolang/FastSim
cmake --build build -- -j16
./build/fastsim_tests
```

The new `fastsim_perf_retirement_tests` target is registered with CTest.
The installed CTest 3.18.4 does not handle `--test-dir build` as newer versions
do; run CTest with `build/` as its working directory. The repository's required
direct gate remains `./build/fastsim_tests`,
which should print `all FastSim tests passed` and exit successfully.

Use sanitizer or workload-specific validation only when the change warrants
it; the commands and evidence are documented in `README.md` and the validation
documents listed below.

## 5. Commit and push

Commit through the temporary Git directory after staging explicit files:

```bash
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  commit -m "<terse description>"
```

Immediately before pushing, fetch or query the remote and confirm that the
branch has not advanced unexpectedly. Push the local temporary clone's
`FastSim` branch to the same remote branch:

```bash
git -C /data00/yinhaolang/FastSim/tmp/codebase-sync push origin FastSim
```

Verify the published hash:

```bash
git ls-remote git@code.byted.org:yinhaolang/minesim.git refs/heads/FastSim
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git rev-parse HEAD
```

Do not create a GitHub pull request. If the user requests review integration,
use the Codebase merge-request URL returned by `git push`.

## 6. Permanently restoring `.git`

Perform this outside a managed session if `.git` is no longer protected by a
read-only mount. Preserve all work-tree files:

```bash
cd /data00/yinhaolang

git clone --no-checkout --single-branch --branch FastSim \
  git@code.byted.org:yinhaolang/minesim.git \
  /data00/yinhaolang/FastSim/tmp/git-recovery

findmnt -T /data00/yinhaolang/FastSim/.git
```

If `findmnt` reports a dedicated read-only mount for `.git`, unmount that exact
path first from an authorized host shell:

```bash
sudo umount /data00/yinhaolang/FastSim/.git
```

Then remove only the confirmed-empty `.git` directory, install the recovered
metadata, and populate the index without touching source files:

```bash
rmdir /data00/yinhaolang/FastSim/.git
mv /data00/yinhaolang/FastSim/tmp/git-recovery/.git /data00/yinhaolang/FastSim/.git
git -C /data00/yinhaolang/FastSim reset --mixed HEAD
git -C /data00/yinhaolang/FastSim status -sb
git -C /data00/yinhaolang/FastSim remote -v
```

`rmdir` is intentional: it fails rather than deleting a non-empty directory.
Do not use `rm -rf` or `git reset --hard` for recovery.

## 7. FastSim documentation map

Start with these files rather than searching experiment output:

Before CPI/P99 or throughput experiments, read
[`optimization-decisions.md`](optimization-decisions.md) for paused approaches,
their evidence, reopening conditions, and the selected next research direction.
For every CPI/PMU/throughput report or outlier diagnosis, also read sections 0
and 8 of [`accuracy-reporting-contract.md`](accuracy-reporting-contract.md):
the source matrix, separate identity gate, maintained validator commands and
release checklist. Do not build a temporary scorer from legacy cache fields.

| Topic | Document |
|---|---|
| 有界store解析时序设计：一次cache分类、因果response游标、证书中性与生产子域 | [`superpowers/specs/2026-10-21-bounded-store-timing-design.md`](superpowers/specs/2026-10-21-bounded-store-timing-design.md) |
| 有界store解析子域实施、审核修复与三负载验证 | [`bounded-store-timing-results-20261022.md`](bounded-store-timing-results-20261022.md) |
| StoreRequestEngine实际接入、SQLite/XZ CPI对照与Marian闭合失败边界 | [`store-engine-integration-results-20261021.md`](store-engine-integration-results-20261021.md) |
| Store prepare生命周期组件、准入/返回资源分离与首片测试边界 | [`store-request-engine-phase1-results-20261021.md`](store-request-engine-phase1-results-20261021.md) |
| SQLite/Marian/XZ CPI逐阶段排查、软件预取ack/fill见证、TSO准入控制与ROI重复开销 | [`cpi-sqlite-marian-xz-analysis-20261021.md`](cpi-sqlite-marian-xz-analysis-20261021.md) |
| Graph500/XZ/FLAC 官方speed ROI、主线程人口与短预热、容量边界 | [`spec-graph500-xz-flac-speed-roi-20261020.md`](spec-graph500-xz-flac-speed-roi-20261020.md) |
| Marian official speed collection and SPEC variant identity | [`spec-marian-speed-collection-20261020.md`](spec-marian-speed-collection-20261020.md) |
| SQLite 尾部修复后约12%残差：Decode/后端恢复阶段、退休密度、错误路径和DRAM刷新候选 | [`docs/sqlite-residual-cpi-analysis-20261020.md`](sqlite-residual-cpi-analysis-20261020.md) |
| 分支恢复尾部传播修复、真实短窗口违规归零与 SQLite/Zstd 同源回归 | [`docs/branch-recovery-tail-fix-results-20261020.md`](branch-recovery-tail-fix-results-20261020.md) |
| 分支恢复对 Zstd 的同源影响、SQLite 开启后的残差账与恢复尾部缺口 | [`docs/branch-recovery-sqlite-zstd-results-20261020.md`](branch-recovery-sqlite-zstd-results-20261020.md) |
| SQLite 完整退休周期账、分支后 load 提前执行反例与同源控制 | [`docs/sqlite-cpi-retirement-diagnosis-20261020.md`](sqlite-cpi-retirement-diagnosis-20261020.md) |
| SQLite 四核新 checkpoint、NO_ACCESS 重采集与 CPI/PMU 复验 | [`docs/sqlite-noaccess-recapture-results-20261020.md`](sqlite-noaccess-recapture-results-20261020.md) |
| 当前负载盘点、SQLite/Zstd 四核 CPI 与退休 cache PMU 复验 | [`docs/current-workload-cpi-pmu-validation-20261020.md`](current-workload-cpi-pmu-validation-20261020.md) |
| Zstd CPI 大误差的排查过程、冷启动检验与 NO_ACCESS 队列语义通俗解释 | [`docs/zstd-cpi-debugging-explained-20261020.md`](zstd-cpi-debugging-explained-20261020.md) |
| NO_ACCESS 功能 companion、本地 SQ、跨 Q 回归与 Zstd 同源 CPI 改善 | [`docs/fst-no-access-repair-20261020.md`](fst-no-access-repair-20261020.md) |
| 新两案CPI大误差的scheduler、历史二进制、DRAM与buffer TSO同源消融 | [`docs/cpi-regression-scheduler-ablation-20261016.md`](cpi-regression-scheduler-ablation-20261016.md) |
| perf来源窗口修复、完整宏边界事实及证据不完整时的误差 | [`docs/perf-incomplete-evidence-repair-results-20261015.md`](perf-incomplete-evidence-repair-results-20261015.md) |
| 近似PMU输出、功能输入内容身份和private-L2漏计生产修复 | [`docs/perf-cache-estimates-and-l2-repair-results-20261015.md`](perf-cache-estimates-and-l2-repair-results-20261015.md) |
| 退休 load cache 主指标、八通道与有界预热重采集 | [`docs/retired-cache-eight-channel-results-20261015.md`](retired-cache-eight-channel-results-20261015.md) |
| 十项 SPR perf cache v2 独立观察器、真实四核采集、覆盖门禁和同源吞吐 | [`docs/perf-cache-threeway-implementation-results-20261015.md`](perf-cache-threeway-implementation-results-20261015.md) |
| perf cache v2 仍缺失的纯功能窗口身份、宏成员与事件 ROI 接口 | [`docs/perf-cache-functional-source-interface-20261015.md`](perf-cache-functional-source-interface-20261015.md) |
| perf-like PMU 文档优先级、micro/macro/line 单位、SPR 真机探针与当前缺口 | [`docs/perf-pmu-semantic-audit-20261014.md`](perf-pmu-semantic-audit-20261014.md) |
| Task PMU 完整起点、请求归属、原生证据与四核误差/开销 | [`docs/task-pmu-repair-results-20261014.md`](task-pmu-repair-results-20261014.md) |
| Recorded scheduler、完整 task-onCPU 计数、真实阻塞、同源吞吐与 PMU 人口门控 | [`docs/recorded-scheduler-results-20261013.md`](recorded-scheduler-results-20261013.md) |
| OS owner/wait 状态、active/elapsed 双周期设计与首片实施 | [`docs/thread-state-active-elapsed-design-20261013.md`](thread-state-active-elapsed-design-20261013.md) |
| 四核 thread-state 真实源与回放验证、双周期输出和未认证范围 | [`docs/thread-state-active-elapsed-results-20261013.md`](thread-state-active-elapsed-results-20261013.md) |
| FastSim 同步 consumer：无 oracle 因果图、producer 交接与精确窗口下界 | [`docs/fastsim-functional-sync-consumer-design-20261013.md`](fastsim-functional-sync-consumer-design-20261013.md) |
| 同步 consumer 实施审核、TeaLeaf C4 对照与统计限制 | [`docs/fastsim-functional-sync-consumer-results-20261013.md`](fastsim-functional-sync-consumer-results-20261013.md) |
| OpenMP 指令窗口时序、跨 QEMU/DR 同步采集协议与精度/成本边界 | [`docs/functional-synchronization-trace-discussion-20261012.md`](functional-synchronization-trace-discussion-20261012.md) |
| gem5/libgomp 功能同步输出首片设计 | [`docs/gem5-functional-sync-output-design-20261012.md`](gem5-functional-sync-output-design-20261012.md) |
| gem5 FS 同步输出实施、四核/TeaLeaf 证据、门控修正与限制 | [`docs/gem5-functional-sync-output-results-20261012.md`](gem5-functional-sync-output-results-20261012.md) |
| TeaLeaf C4 完整同步前史采集、20GB 空间预算与原生 CPI/PMU 误差 | [`docs/tealeaf-4c-sync-trace-accuracy-20261012.md`](tealeaf-4c-sync-trace-accuracy-20261012.md) |
| Two-stage (throughput-first) accuracy-ceiling verdict, structural evidence, and break-out cost | `docs/two-stage-accuracy-ceiling-20260916.md` |
| Optimization decisions, rejected candidates, and next research direction | `docs/optimization-decisions.md` |
| Post-commit store services and resource-group rollback; critical requests still uncovered, failed CPI/throughput gate | [`docs/critical-service-repair-20260910.md`](critical-service-repair-20260910.md) |
| Actual-arrival shared-service candidate, component rollback, full-ROI TeaLeaf C4 changes and failed throughput gate | [`docs/shared-service-arrival-phase2-20260910.md`](shared-service-arrival-phase2-20260910.md) |
| Batch-local private read-service implementation; activated followers, unchanged TeaLeaf CPI, ABBA cost and full-ROI retirement absorption | [`docs/private-read-services-phase1-20260910.md`](private-read-services-phase1-20260910.md) |
| Current two-stage critical-path review and proposed service-identity, incremental-feedback and DRAM repairs; restricted first slice implemented, overall design not accepted | [`docs/two-stage-service-repair-design-20260910.md`](two-stage-service-repair-design-20260910.md) |
| Current TeaLeaf L1D64 C4 full-ROI CPI, aligned ROB-head waiting and request-lifecycle evidence | [`docs/tealeaf-full-roi-error-audit-20260910.md`](tealeaf-full-roi-error-audit-20260910.md) |
| Throughput-first repair review for the existing two-stage engine; sparse dependencies and bounded feedback changes | [`docs/two-stage-repair-review-20260909.md`](two-stage-repair-review-20260909.md) |
| Complete RAW dependencies in the original two-stage engine; TeaLeaf compatibility, CPI and throughput validation | [`docs/two-stage-dependencies-20260909.md`](two-stage-dependencies-20260909.md) |
| Original two-stage load response/writeback consistency; batch WB capacity and measured TeaLeaf cost/benefit | [`docs/two-stage-response-completion-20260909.md`](two-stage-response-completion-20260909.md) |
| Two-stage service reuse: fill identity, visibility checks, rollback, and unchanged TeaLeaf C4 accuracy | [`docs/two-stage-service-validity-20260909.md`](two-stage-service-validity-20260909.md) |
| Current two-stage repairs tested on seven historical CPI tail cases; Graph500/Stockfish regressions and service-only isolation | [`docs/two-stage-tail-validation-20260909.md`](two-stage-tail-validation-20260909.md) |
| Graph500 C8 regression isolated to mixed request origins; absolute-time diagnostic and real trace witnesses | [`docs/graph500-response-origin-regression-20260909.md`](graph500-response-origin-regression-20260909.md) |
| Production request-origin repair, resource/fragment feedback, three workload checks and Graph500 ABBA throughput | [`docs/two-stage-request-origin-20260909.md`](two-stage-request-origin-20260909.md) |
| Causal cache request/return and E/S/write-permission repair, regression checks, and paired TeaLeaf diagnostic | [`docs/causal-cache-permission-repair-20260909.md`](causal-cache-permission-repair-20260909.md) |
| Causal cache request/return and permission defects, actual SimpleNetwork paths, and one TeaLeaf DRAM parameter diagnostic | [`docs/causal-memory-edge-audit-20260909.md`](causal-memory-edge-audit-20260909.md) |
| Load FU-to-admission fix, completion/WB distinction, and TeaLeaf exact functional-ROI integration | [`docs/causal-load-stages-roi-20260909.md`](causal-load-stages-roi-20260909.md) |
| Existing memory escape integration, O3 phase mapping, and fixed-prefix gem5 identity/measurement gate | [`docs/causal-core-memory-prefix-gate-20260909.md`](causal-core-memory-prefix-gate-20260909.md) |
| Complete FST dynamic dependencies, sparse storage, producer patch and TeaLeaf validation | [`docs/fst-complete-dependencies-20260909.md`](fst-complete-dependencies-20260909.md) |
| Causal multicore ownership, conservative coherence, continuous warmup and native input coverage gate | [`docs/causal-core-memory-phase3-20260909.md`](causal-core-memory-phase3-20260909.md) |
| Causal FP/SIMD, store/SQ, branch lifecycle and real functional-prefix validation | [`docs/causal-core-memory-phase2-20260909.md`](causal-core-memory-phase2-20260909.md) |
| Implemented causal ALU/load event path, mechanism tests, limits, and CLI evidence | [`docs/causal-core-memory-phase1-20260909.md`](causal-core-memory-phase1-20260909.md) |
| Historical causal solver prototype plan; production direction superseded by the two-stage repair review above | [`docs/causal-core-memory-repair-plan-20260909.md`](causal-core-memory-repair-plan-20260909.md) |
| TeaLeaf full-ROI committed-head gaps, paired ROB blocking, and native response comparison | [`docs/tealeaf-intermediate-state-audit-20260908.md`](tealeaf-intermediate-state-audit-20260908.md) |
| Final-issue admission shadow, paired spacing/lifetime decomposition, and guarded cache commit | [`docs/line-generation-admission-shadow-20260908.md`](line-generation-admission-shadow-20260908.md) |
| Cross-workload issue-owner matrix, native hierarchy comparison, and admission/response timing-ledger implementation | [`docs/cross-workload-component-matrix-phase1-20260908.md`](cross-workload-component-matrix-phase1-20260908.md) |
| Paired event evidence, compensating errors, and stage-owned modeling replacement plan | [`docs/paired-event-model-plan-20260908.md`](paired-event-model-plan-20260908.md) |
| Measurement-boundary cache-state root cause, opt-in implementation, and failed CPI gate | [`docs/measurement-boundary-memory-state-phase1-20260908.md`](measurement-boundary-memory-state-phase1-20260908.md) |
| Measurement-boundary state cross-workload pilot, deterministic repeats, and Graph500 provenance failure | [`docs/measurement-boundary-memory-state-multiload-20260908.md`](measurement-boundary-memory-state-multiload-20260908.md) |
| Paired frontier first divergence, cross-core DRAM propagation, SQ owner reversal, and rejected store ablations | [`docs/paired-frontier-divergence-phase1-20260908.md`](paired-frontier-divergence-phase1-20260908.md) |
| Current code audit: load response causality, future FU reservations, DTLB visibility, and measured throughput hotspots | [`docs/current-code-model-audit-20260907.md`](current-code-model-audit-20260907.md) |
| First-priority gap-aware FU implementation, CPI controls, and stop decision | [`docs/fu-gap-aware-phase1-20260907.md`](fu-gap-aware-phase1-20260907.md) |
| Global code review, pre-experiment benefit screening, and branch recovery counterexample | `docs/global-optimization-screening-20260907.md` |
| Current fixed-Q architecture evidence and pending-fill experiment | `docs/architecture-evidence-audit-20260907.md`, `docs/pending-fill-phase1-20260907.md` |
| Normative project goal, perf→gem5→FastSim semantic contract, and acceptance order | `docs/project-goal-and-semantic-contract.md` |
| P0 baseline/measurement-contract implementation and activation gate | `docs/p0-baseline-measurement-contract-implementation.md` |
| P1 native PMU population and response-boundary audit | `docs/p1-native-pmu-population-audit-2026-08-19.md` |
| Overview, build, trace conversion, and usage | `README.md` |
| External C++/Python DVFS inputs, window APIs, schemas, and integration | `docs/fastsim-external-integration.md` |
| Architecture and confidence boundaries | `docs/architecture.md` |
| Trace schema and functional-only contract | `docs/gem5-trace-contract.md` |
| Validation methodology and gates | `docs/validation.md` |
| gem5 parameter coverage | `docs/gem5-parameter-coverage.md` |
| X86 FS 大内存显式开关、旧配置隔离与 checkpoint 兼容边界 | `docs/gem5-x86-fs-large-memory-runbook-20261026.md` 第 12 节 |
| Marian speed 八核32GiB、10M共同终点来源与身份/人口门禁 | `docs/marian-c8-32g-10m-collection-20261027.md` |
| Marian C8/32GiB/10M同源FastSim CPI/PMU误差与分段DRAM回放资格 | `docs/marian-c8-32g-10m-fastsim-results-20261027.md` |
| Marian C8/32GiB/10M CPI来源诊断、DRAM/store控制与DTLB反证 | `docs/marian-c8-32g-10m-cpi-diagnosis-20261027.md` |
| Marian C8 FR-FCFS候选串行交错吞吐、时间分项与资源观测 | `docs/marian-c8-frfcfs-throughput-20261027.md` |
| FR-FCFS维护native默认、旧策略回退与其他C8并行误差报告 | `docs/frfcfs-native-default-and-c8-results-20261027.md` |
| FLAC syscall截断边界、固定展开规则与暂缓修复记录 | `docs/flac-c8-count-boundary-diagnosis-20261027.md` |
| C8三负载FR-FCFS暂定结果，按用户要求忽略FLAC极小计数差异 | `docs/frfcfs-c8-provisional-results-20261027.md` |
| Marian/Graph500/XZ/FLAC speed C4/C8/C16统一32GiB、实际syscall微操作及磁盘保护 | `docs/spec-speed-32g-matrix-20261027.md` |
| Current C4/C8 gem5/FastSim microarchitecture semantic-alignment audit | `docs/fs-gem5-uarch-semantic-alignment-audit-2026-08-18.md` |
| CPI/P99 investigation history | `docs/gem5-source-aligned-p99-plan.md` |
| Microarchitecture collection workflow | `docs/uarch-generalization-collection.md` |
| Generalization debugging record | `docs/uarch-generalization-debug-log.md` |
| FS syscall/CPL trace patch | `docs/gem5-taotrace-cpl-syscall-patch.md` |
| Dual-CPI syscall modeling | `docs/syscall-modeling-dual-cpi.md` |
| FST v7 layout and drmemtrace conversion contract | `docs/fst-v7-drmemtrace-conversion-contract.md` |
| CPI/PMU/throughput reporting contract | `docs/accuracy-reporting-contract.md` |
| SQLite/Marian/XZ first mechanism repairs, same-source validation and remaining timing gaps | `docs/cpi-sqlite-marian-xz-repair-results-20261021.md` |
| Native-FS cache reference selection incident and corrected 40-case evidence | `docs/private-l2-pmu-source-repair-20260916.md` |
| Maintained v28.6 C4--C32 accuracy and throughput baseline | `docs/fastsim-v28_6-c4-c32-baseline-2026-08-27.md` |
| Current FS CPI scheme, evidence, and implementation order | `docs/fs-cpi-current-status-and-plan-2026-08-17.md` |
| guest-PTE and measurement-boundary page-fault model | `docs/gem5-initial-pte-page-fault-model-2026-08-17.md` |
| Current project-status narrative | `docs/weekly-meeting-fastsim-status-2026-08-06.md` |
| Debugging interview/checklist | `docs/fastsim-cpi-throughput-debugging-interview.md` |

For implementation work, map model ownership as follows:

- configuration parsing and defaults: `include/fastsim/config.hpp`,
  `src/config.cpp`;
- trace format and decoding: `include/fastsim/trace.hpp`, `src/trace.cpp`;
- timing, memory, and system coordination: `include/fastsim/simulator.hpp`,
  `src/simulator.cpp`;
- interval model: `include/fastsim/interval_core.hpp`,
  `src/interval_core.cpp`;
- CLI and output schema: `src/main.cpp`;
- regression coverage: `tests/test_main.cpp`;
- FS validation: `tools/validate_fs_c8.py`;
- cross-core validation: `tools/validate_tcsim_c4_c8.py`.

## 8. Historical handoff

On 2026-08-09, commit `d03e64f` (`Advance FastSim trace and timing models`)
was pushed successfully from `dc9cd96` to the remote `FastSim` branch. It
contained 32 critical code, test, tool, configuration, and documentation
files. The build succeeded and `./build/fastsim_tests` passed. This entry is
useful provenance only; always verify the current remote head before new work.
