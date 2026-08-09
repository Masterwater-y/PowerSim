# gem5 TaoTrace CPL-filter + sysnum patch spec

Producer-side companion to `docs/syscall-modeling-dual-cpi.md`. This is the
gem5-FS change needed so the exported functional trace matches the unified
input contract (user-only stream + syscall markers with sysnum) and so the
oracle can classify kernel cycles. It targets the vendored probe at
`/data00/yinhaolang/TCSim/vendor/v29/gem5_patch/overlay/src/cpu/o3/probe/tao_trace.{cc,hh}`.

Implementation status: Phase 1 through Phase 3 are applied in
`/data00/yinhaolang/gem5-fs` and mirrored to the TCSim vendor overlay. FastSim
reads `syscall_number` (JSONL field, or `sysnum`) and both the interval and
scalar core paths use the same per-sysnum cost lookup.

## Phase 1 — measure-only CPL classification (do this first)

Goal: confirm the three-way kernel-cycle split is realizable and quantify the
stockfish idle spin, without changing any emitted record.

1. In `onCommit(inst)`, read privilege once:

   ```cpp
   const bool user = inst->tcBase()->getIsaPtr()->inUserMode();
   ```

   `inUserMode()` is the generic ISA API used by gem5's built-in
   `ExeTracerRecord::traceInst` (`src/cpu/exetrace.cc`), implemented for x86.

2. Classify the entry reason for CPL=0 spans. A span is **syscall-service**
   when its most recent CPL 3->0 transition was caused by a syscall instruction
   (`isSyscallInst` already exists); otherwise it is **IRQ/exception/idle**.
   Track the current span class in a per-core member updated on each CPL
   transition.

3. Accumulate three per-core cycle counters — `user_cycles`,
   `syscall_kernel_cycles`, `irq_idle_kernel_cycles` — using
   `curTick()`/cycle conversion already present in the probe (`ticksToCycles`).
   Write them to a new `oracle/cpl_class.jsonl` (or extend the existing diag
   sink). Emit nothing new into `records`.

Acceptance for Phase 1: on the six current workloads, the sum of the three
classes equals total committed cycles, and stockfish shows a large
`irq_idle_kernel_cycles` (its 79% kernel PCs are a spin, not syscall service).

## Phase 2 — oracle dual-CPI emission

Using the Phase 1 classification, emit `oracle/cpi.json` with both CPIs on the
**user-mode denominator** `N_user` (see dual-CPI doc section 7):

- `cpi_user   = user_cycles / N_user`
- `cpi_incl   = (user_cycles + syscall_kernel_cycles) / N_user`
- `irq_idle_kernel_cycles` is reported separately and enters neither CPI.

`N_user` = committed user-mode instruction/uop count (the syscall instruction
counts as one user instruction). Do **not** emit gem5 native
`numCycles / all-committed` as a FastSim-facing CPI; keep it only as an internal
sanity field if desired.

Also emit, per sysnum, the summed on-core `syscall_kernel_cycles` and the count
of invocations, so FastSim's per-sysnum cost table can be calibrated
(`cpi_incl - cpi_user` per sysnum).

## Phase 3 — functional FST filtering + sysnum emit

Change what enters the functional `records` stream:

1. **Strip CPL=0 from functional records.** In `onCommit`, when `!user`, skip
   `accumulateMicro(inst)` entirely — do not emit the instruction, its memory
   accesses, or its branch info into `records`. (The instruction still counts
   toward the oracle cycle classes in Phase 1.)

2. **Emit sysnum on the syscall marker.** The syscall marker is emitted from
   the CPL=3 syscall instruction (current `emitSyscallRecord` path, which runs
   before kernel entry). Extend `writeRecordsSyscallLine` to carry the number
   already captured in `PendingSyscall::nr`:

   - Add a `uint64_t sysnum` parameter to `writeRecordsSyscallLine`.
   - Add `"syscall_number":%llu` to the emitted JSON object (alongside the
     existing `"opcode"`).
   - At the call site (`emitSyscallRecord`, ~line 913) pass `sc.nr`.

   The binary FST exporter maps this to the canonical record's `address` field
   on syscall markers (op_class = -1), matching what FastSim reads. Set the
   `kFeatureSyscallMarkers` header bit when any sysnum is written.

3. **Do not mislabel interrupts.** An IRQ/exception CPL 3->0 transition must not
   produce a syscall marker; only an actual syscall instruction does.

4. **Boundary linkage check.** After stripping, assert that around each syscall
   the stream is `[last CPL=3 instr][SYS marker][next CPL=3 instr]` with no
   kernel record in between, and that seq ordering stays monotonic.

## What stays out of the functional trace

Per contract, the probe must **not** put any of these into functional records:
kernel instructions, kernel cache/TLB accesses, measured syscall duration,
syscall args/retval, sync/blocking classification, scheduler events. All of
those either stay in `oracle/` (duration, per-sysnum cost) or are dropped.
`classifySyncFromSyscall` output is already collapsed to NONE/YIELD in the v2
records stream and must remain so.

## Boundary cases (from dual-CPI doc section 8)

- **vDSO** (`clock_gettime`/`gettimeofday`/`getcpu`): all CPL=3, no syscall
  marker. Correct — matches drmemtrace treating them as user instructions.
- **Signal handlers**: CPL=3 but kernel-initiated; recorded as a known
  deviation, no special handling in this phase.
- **lbm**: has 2.7% kernel PCs, likely IRQ/timer. Under uniform CPL=0 stripping
  those disappear from functional records and contribute only to
  `irq_idle_kernel_cycles`; whether lbm emits zero syscall markers is verified
  on recollection, not assumed.

## Validation after Phase 3

Recollect the six workloads and, using FastSim's paired run
(`syscall.cost_model` with a zeroed vs calibrated table, see dual-CPI doc
section 7.1), report `err_user` and `err_incl` against the Phase 2 oracle.
Re-measure stockfish `CPI_user` specifically: the prior -0.02% figure included
kernel spin and is expected to change once the stream is user-only.

## FastSim side: already implemented

- `TraceRecord::syscall_number()` / `set_syscall_number()` reuse `address` on
  syscall markers (`include/fastsim/types.hpp`).
- JSONL reader consumes `syscall_number` / `sysnum` and clears the physical
  flag (`src/trace.cpp`).
- Synthetic per-sysnum cost model behind `syscall.cost_model` +
  `syscall.cost_table` (`include/fastsim/config.hpp`, `src/config.cpp`,
  `src/interval_core.cpp`); off = current scalar behavior, verified bit-exact
  on lbm.
- Tests: `test_syscall_trace_roundtrip` (sysnum survives), `test_syscall_cost_model`
  (table override, unknown-sysnum fallback, model-off parity).

## Implemented validation

- Phase 1 measure-only mode emits no FST/JSONL records.
- The real FS ROI boundary is the serial-console
  `operation=workbegin-serial` event, so the CPL gate is opened by that hook.
- Six SPEC c4 workloads completed at 500K all-core ROI. All 24 per-core rows
  satisfy exact tick conservation.
- FS x86 `syscall` decodes as a `SYSCALL_64` macroop; the producer detects the
  first macro micro-op and emits exactly one marker per kernel entry.
- Phase 2 emits `oracle/cpl_class.jsonl`, per-core `cpi-coreN.json`, and merged
  `oracle/cpi.json`.
- Phase 3 validation on `811.tealeaf_s` c4/500K:
  - `records == oracle n_user` on all four cores;
  - zero CPL0/kernel PCs in the FST;
  - 16 syscall markers, all `sysnum=202` (`futex`);
  - syscall feature bit set only on cores containing markers;
  - no physical-address flag on syscall markers.
- Oracle aggregate: `N_user=4,097,887`, `CPI_user=0.8835087449`,
  `CPI_incl=0.8890064563`, `syscall_kernel_cycles=22,529`,
  `irq_idle_kernel_cycles=34,028`.
