# Syscall modeling and dual-CPI contract

Status: implemented and smoke-validated. This document is the shared contract
between the gem5-FS trace producer, the FastSim functional-trace consumer, and
the accuracy-reporting layer for system-call cost.

## 1. Problem statement

The current six-workload gem5-FS functional traces mix user-mode and
kernel-mode committed instructions. A PC scan of the C16 `core0.fst` files
shows kernel-space records (PC >= `0xffff800000000000`) at:

| workload | kernel-PC fraction | note |
|---|---:|---|
| 706.stockfish_r | 79.0% | low-diversity kernel spin (12,869 unique PCs, one region repeats tens of thousands of times) |
| 777.zstd_r | 14.7% | mixed |
| 854.graph500_s | 7.6% | mixed |
| 782.lbm_r | 2.7% | small (likely IRQ/timer, not syscall) |
| 811.tealeaf_s | 0.87% | small |
| 710.omnetpp_r | 0.22% | minimal |

Feeding kernel instructions into FastSim is **not** acceptable under the
deployment contract: it would make gem5-FS inputs strictly higher-resolution
than drmemtrace / gem5-SE, cause a train/inference input mismatch, leak
non-deployable kernel-implementation detail into the model, and inflate trace
size. The stockfish number also shows a second hazard: most of its kernel mass
is an **idle spin**, not syscall service, so a naive "kernel cycles = syscall
cost" folding would fabricate an enormous fake syscall cost.

## 2. Unified input contract

Every functional trace — from drmemtrace, gem5-SE, or gem5-FS — must satisfy
the same shape:

```
user-mode instr -> syscall(number) -> user-mode instr
```

The functional trace **contains only**:

- user-mode (CPL=3) instructions and their memory accesses;
- a syscall marker (`op_class = kSyscallOpClass = -1`) for each user-mode
  syscall instruction;
- the syscall number and ABI for that marker;
- when the producer captured them, up to six scalar ABI arguments, the raw
  return-register bits, failure/errno, pre/post timestamp and CPU ID,
  best-effort maybe-blocking, and thread ID. Every optional field has an
  explicit validity bit; an absent value is never converted to zero/false;
- the existing branch, register-dependency, and address information.

The functional trace **must not contain**:

- kernel-mode (CPL=0) instructions;
- kernel cache/TLB accesses;
- measured syscall wall/cycle duration;
- any gem5-FS-only scheduler / blocking / PMU-oracle signal.

This is the portable intersection verified with normal offline drmemtrace.
`TRACE_MARKER_TYPE_SYSCALL` supplies the number; explicit syscall recording
supplies selected scalar arguments, raw return and failure errno; timestamp,
CPU-ID and maybe-blocking markers supply the remaining hints. FST v7 retains
exactly this intersection. The timestamp delta is instrumented wall time, not
an active-CPL0-cycle label. The real-collection evidence and the boundary
between portable metadata and unavailable kernel truth are documented in
[the portable metadata audit](drmemtrace-portable-metadata-audit-2026-08-14.md).

## 3. gem5-FS producer changes (`TaoTrace`)

At commit, `TaoTrace` reads the privilege level via
`thread->getIsaPtr()->inUserMode()` (the idiom used by gem5's built-in
`ExeTracerRecord::traceInst`, `cpu/exetrace.cc`). Emission rules:

- **CPL=3 normal instruction** → write to functional FST as today.
- **CPL=3 syscall instruction** → write one `op_class=-1` syscall marker and
  record its syscall number (from `Rax`, already captured by
  `captureSyscallState`). The marker is emitted **before** any CPL filtering
  drops the following kernel stream.
- **CPL=0 kernel instruction** → do **not** write to functional FST.
- **Interrupt/exception entry into kernel** → do **not** write to functional
  FST and must **not** be mislabeled as a syscall marker.

The existing `writeRecordsSyscallLine` currently discards `sc.nr`; it must be
extended to persist the syscall number (see section 5). The already-captured
args, cycle duration, and sync classification stay out of the functional
stream and flow only into the oracle side (section 6).

Kernel PCs cannot by themselves distinguish "syscall-service kernel" from
"IRQ/timer/idle-spin kernel". Classification uses CPL transitions plus the
entry reason (syscall instruction vs interrupt/exception vector), decided on
the gem5 side where that context exists.

## 4. Synthetic syscall cost model (FastSim)

FastSim's deployed cost selector currently consumes the syscall number; FST v7
also exposes the optional portable metadata to later semantic profiles. It
must still model the cost and the kernel-only PMU contribution. On encountering a syscall marker it
looks up a per-`sysnum` synthetic event profile and charges its service time as
an on-core, serializing system operation. The profile also supplies retired
instructions/UOPs, branch events, L1D/L2/LLC events, DTLB events, and optional
blocked wall time. These PMU fields are accumulated in a separate synthetic
kernel domain; they do not masquerade as functional user records or mutate the
user cache/TLB state.

- Static per-`sysnum` table: `sysnum -> mean active service profile`. This can
  approximate the on-core handler path, but it cannot determine whether one
  particular `futex`, `read`, or `poll` instance blocked.
- Only the **on-core active** portion of syscall service is charged into
  per-uop CPI. Off-core blocked/descheduled time is never charged to the bound
  thread's CPI (it belongs to makespan only), matching the oracle rule in
  section 6.
- The table values are **calibrated offline** from the gem5-FS oracle
  (section 6) and then frozen. Given a fixed trace + fixed table, the cost is
  fully deterministic, preserving bit-exact replay.

This subsumes the existing single `syscall_service_latency` scalar: that scalar
becomes the fallback used when a sysnum is absent from the table.

The PMU profile must obey the usual conservation constraints (`misses <=
accesses`, branch misses no greater than branches, and UOPs no fewer than
instructions). Unknown syscall numbers deliberately produce no invented
kernel PMU events: only the legacy scalar/cost-table timing fallback applies.
Arguments, return/failure and maybe-blocking may refine a semantic class, but
they do not reveal wakeup reason, scheduler edges, or the active/blocked split.
Per-instance blocked time therefore remains zero until a separately validated
scheduling model consumes suitable input; it is never copied from timestamp
delta or guessed from the syscall number.

## 5. FST v7 syscall storage

The normative byte layout and complete drmemtrace-to-FST field mapping are in
[`fst-v7-drmemtrace-conversion-contract.md`](fst-v7-drmemtrace-conversion-contract.md).

`TraceRecord` is a fixed 64-byte layout (`include/fastsim/types.hpp`,
`static_assert(sizeof(TraceRecord) == 64)`); it must not grow.

**Decision: store sysnum inline in the `address` field of the syscall marker
record only.**

Justification: the memory-address block in `simulator.cpp` is guarded by
`if (!record.is_memory()) continue;` (line ~3145) and a syscall marker is not a
memory record, so `address` is provably unused on a syscall record. The reader
already dispatches syscalls via `is_syscall()` (`op_class == -1`). Storing
sysnum in `address`:

- costs zero record growth;
- is immune to ROI re-slicing / re-indexing (unlike an index-keyed sidecar,
  which the existing `roi-slice.manifest.txt` sub-slicing would invalidate);
- requires the reader to read one extra field only inside the `is_syscall()`
  branch.

The `abi` (e.g. Linux x86-64) is a single per-trace property and is stored once
in header `reserved[3]`, not per record. FST v7 appends a sparse 128-byte table
to the **same `.fst` file**, one row per syscall. Header `reserved[0..2]` store
table offset, row count and row size, and feature bit 3 declares the table.
Each row contains:

```text
record_ordinal, syscall_ordinal, thread_id, sysnum,
args[6], retval_raw, pre/post_timestamp_us,
errno, pre/post_cpu, validity_bits, arg_count, failed, maybe_blocking
```

The duplicated `sysnum` and both ordinals are integrity anchors: the reader
fails if a row is unordered, points to a non-syscall record, or disagrees with
the inline number. Instruction-slice and warmup wrappers return the metadata of
their underlying current record, so slicing does not rewrite or desynchronize
the table. Versions 2--6 remain readable; a v7 syscall without its aligned row
is rejected.

`convert-gem5 --syscall-output` can additionally mirror the rows as
`fastsim-functional-syscall-v2` JSONL for inspection. That file is not needed
for replay and is never the source of truth, avoiding a loose-sidecar mismatch.
The user-only pipeline ignores the metadata; user+kernel semantic modeling may
read it through `TraceSource::current_syscall_metadata()`.

## 6. gem5-FS oracle output (separate from functional trace)

gem5-FS produces accuracy truth as a **physically separate** artifact:

```
functional/
  coreN.fst              # deployment-usable, drmemtrace-equivalent
oracle/
  cpi.json               # dual CPI truth (see section 7)
  pmu.json
  per_core_cycles.json
```

- `functional/` is the only thing FastSim inference may read.
- `oracle/` is training labels / accuracy evaluation / calibration truth only.
- FastSim inference must be **programmatically prevented** from reading
  `oracle/` — a physical path separation plus a test that fails if the
  inference path references any oracle field (not convention alone).

Every measured core cycle belongs to exactly one class: `user`, `syscall`,
`page_fault`, `irq`, `scheduler`, `idle`, or `unknown_kernel`. These are time
partitions, not penalties added on top of instruction commit time. A cache-miss
stall while a syscall frame is active is already part of that frame's elapsed
cycles and is never added a second time.

`user`, `syscall`, `page_fault`, `irq`, and `scheduler` are active execution.
`idle` is elapsed core time outside the application CPI numerator. Formal data
requires `unknown_kernel=0`. Nested entries use a stack: an IRQ inside a
syscall is charged to IRQ until IRET, then attribution resumes at syscall.

The collection kernel uses `idle=poll`; its idle loop commits `PAUSE` rather
than HLT/MWAIT. gem5 decodes `F3 90` as REP-prefixed `NOP`, so TaoTrace checks
the x86 opcode directly and enters persistent idle only after the same PAUSE
site repeats 128 times with no more than 64 intervening commits. IRQ return
requires the loop to be confirmed again; 64 consecutive non-PAUSE commits also
close idle when a polling loop observes a wake without an IRQ. This keeps
isolated spinlock `cpu_relax()` calls active while preventing multi-million-cycle futex
sleep/residency tails from being reported as syscall service.

## 7. Dual-CPI definition and reporting

Both CPIs share the **same denominator**: user-mode instruction/uop count
(`N_user`). The syscall instruction itself is one user-mode instruction. The
difference is only in the numerator.

| metric | numerator | denominator | measures |
|---|---|---|---|
| `CPI_user` | user-mode cycles | `N_user` | pipeline / cache / branch model fidelity |
| `CPI_user_plus_kernel` | user + active syscall + page-fault + IRQ + scheduler cycles | `N_user` | active application CPI |

Idle and blocked wall time enter neither numerator. With formal data requiring
zero unknown cycles:

```
CPI_user_plus_kernel
  = (user + syscall + page_fault + IRQ + scheduler) / N_user
```

The older `CPI_incl` field in `oracle/cpi.json` is a compatibility diagnostic
that historically meant user+syscall only. It is not the accuracy gate; the
gate reads `oracle/kernel_events.json:cpi_user_plus_kernel`.

**Hard rule:** do not use gem5 native `numCycles / (all committed incl. kernel)`
as the second CPI. That denominator contains kernel instruction count, which
FastSim structurally cannot reproduce without leaking kernel detail. gem5's
two oracle CPIs must both use `N_user` as denominator so FastSim can match
them. Native full-system CPI may be kept as an internal sanity check but must
not enter the FastSim error gate.

FastSim reports two errors:

- `err_user` = relative error of `CPI_user` vs gem5 oracle `CPI_user`
  → quality of the pure compute model, isolated from syscalls.
- `err_user_plus_kernel` = relative error of `CPI_user_plus_kernel` vs the
  matching gem5 oracle scope.

These errors are reported independently. Subtracting two relative errors does
not isolate a physical cause; syscall, page-fault, IRQ and scheduler
event/count/cycle residuals are reported separately for that diagnosis.

### 7.1 How FastSim derives the two CPIs

The syscall is a serializing operation on the critical path, so its cost is
**not** an additive term that can be subtracted from the total post hoc (the
`syscall_*_cycles` counters are explicitly documented as raw, possibly
overlapping components, not a CPI decomposition). The overlap-safe derivation
is a **paired run on one binary**:

- **`CPI_user`**: run with syscall, page-fault and IRQ event profiles disabled
  (or zero-active-cycle). The syscall marker remains a bare serialization
  boundary. Numerator = the functional user timeline.
- **`CPI_user_plus_kernel`**: same binary and trace, with frozen calibrated
  syscall, first-touch page-fault and periodic IRQ profiles enabled. Numerator
  is the resulting active application timeline.

Both runs share an identical trace and therefore an identical `N_user`; the
causal difference is limited to the synthetic kernel models. Raw synthetic
service counters are useful components but are not subtracted post hoc from
the final timeline because service can overlap existing pipeline stalls.

Native full-system CPI is never used as a FastSim gate (see below).

Naming: the second metric is **user+active-kernel application CPI**. It includes
modeled syscall, page-fault and IRQ work, but excludes idle and blocked wall
time. Scheduler remains a visible oracle residual because the current
user-only input has no scheduling hook from which to infer it.

## 8. Known boundaries and exceptions

- **vDSO** (`clock_gettime`/`gettimeofday`/`getcpu`): runs entirely at CPL=3 and
  produces **no** syscall marker. This matches drmemtrace (also treats them as
  user instructions). Correct, not a bug; such calls simply carry no marker.
- **Signal handlers**: run at CPL=3 but are kernel-initiated. drmemtrace tags
  them with `KERNEL_EVENT`. Rare in SPEC; recorded as a known deviation for
  now.
- **sysnum read timing**: must read `Rax` at the CPL=3 syscall instruction
  execute (current `captureSyscallState` timing), never after kernel entry.
- **Boundary linkage**: after kernel stripping, verify the sequence
  `[last user instr before syscall][SYS marker][first user instr after sysret]`
  stays contiguous.
- **lbm wording**: lbm has 2.7% kernel PCs but likely from IRQ/timer, not
  syscall. All six workloads get uniform CPL=0 stripping; a marker is emitted
  only at a confirmed user-mode syscall instruction; whether lbm ends with zero
  markers must be verified on recollection, not assumed.

## 9. Impact on prior C16 results

The previously reported stockfish `CPI_user` error of -0.02% is dominated by
its kernel spin and is **not** a valid user-only number. After stripping,
stockfish `CPI_user` must be re-measured and is expected to change. No prior
CPI number that included kernel records should be assumed to survive stripping.

## 10. Phased implementation plan

1. **gem5 CPL classification probe (measure-only):** add `inUserMode()` +
   entry-reason classification to `TaoTrace`, emit per-core counts of
   {user, syscall-service kernel, idle/IRQ kernel} without changing record
   content. Confirms the section 6 mutually exclusive split is realizable and quantifies
   stockfish idle spin.
2. **Define oracle CPI contract:** freeze the section 7 formulas and the
   section 6 folding rules; emit `oracle/cpi.json` with both CPIs on `N_user`.
3. **FastSim sysnum plumbing + synthetic model:** read inline sysnum on syscall
   records, add the per-sysnum cost table + config, keep the scalar as
   fallback. Behind a config switch (off = current scalar behavior) so A/B uses
   one binary.
4. **Dual-CPI reporting:** add `CPI_user` / `CPI_user_plus_kernel` and their
   independent errors to the validation summary.
5. **gem5 producer CPL filter + sysnum emit:** strip CPL=0 from functional FST,
   persist sysnum in `address`.
6. **Recollect + re-measure:** rerun the six workloads, re-measure user-only
   CPI (stockfish especially), compare pre/post stripping.

Acceptance stays the same as other FastSim changes: bit-exact non-host causal
state under a config switch (off = reference), full multi-workload paired A/B,
and sanitizer-clean builds.

## 11. Implementation and smoke result

The contract is implemented in gem5-FS and FastSim:

- gem5-FS supports CPL measure-only runs with no functional records;
- `functional_user_only` strips CPL0 and emits one inline syscall marker with
  `address=sysnum`;
- oracle output is physically separated under `oracle/`;
- FastSim interval and scalar paths share one per-sysnum lookup;
- the production defaults remain `syscall.cost_model=false` and
  `syscall.event_model=false`.

The `811.tealeaf_s` c4/500K end-to-end run produced 16 `futex(202)` markers and
no kernel PC in the functional FST. Paired scalar runs consumed the same
4,097,887-UOP trace:

- model off: 0 syscall service cycles;
- model on, `202:1408`: 22,528 syscall service cycles;
- oracle syscall-service total: 22,529 cycles.

The one-cycle difference comes from integer per-instance calibration:
`22,529 / 16 = 1408.0625`.

## 12. Synthetic kernel PMU implementation slice

The first inference-side PMU slice is implemented behind
`syscall.event_model`. A frozen table entry has 15 unsigned integer fields:

```
sysnum:service_cycles:instructions:uops:branches:branch_misses:
l1d_accesses:l1d_misses:l2_accesses:l2_misses:
llc_accesses:llc_misses:dtlb_accesses:dtlb_misses:blocked_wall_cycles
```

For example:

```ini
syscall.event_model = true
syscall.event_table = 202:40:120:180:30:2:80:8:8:3:3:1:50:4:900
```

Multiple entries are comma-separated. For a known syscall, its
`service_cycles` has precedence over `syscall.cost_table`; an unknown syscall
falls back to `syscall.cost_table` and then `syscall.service_latency`.

The diagnostic `totals` object reports `user_functional_pmu`, then four mutually
exclusive synthetic domains:

- `synthetic_syscall_kernel` (implemented);
- `synthetic_page_fault_kernel` (default zero; populated by section 13);
- `synthetic_irq_kernel` (default zero; populated by section 13);
- `synthetic_kernel_total` (the sum of the three).

`user_plus_synthetic_kernel_pmu` adds the synthetic kernel event counts to the
functional user PMU counts. Its `sum_core_cycles` is the simulator's existing
core-cycle result because active syscall service was already inserted into the
time line. Adding `synthetic_kernel_total.active_cycles` again would double
count CPL0 service. `blocked_wall_cycles` remains a separate diagnostic and
enters neither core cycles nor CPI.

The x86 FS producer collapses each syscall transition macro-op into one
serial functional marker, while gem5's privilege-scoped PMU retires 25
user-decoded transition UOPs and one control operation. PMU output therefore
restores the fixed extra 24 retired UOPs and one branch per syscall marker.
This correction changes neither trace replay work nor `N_user`: the CPI
denominator remains one functional marker per syscall boundary.

A single run cannot recover overlap-safe `CPI_user` by subtracting raw syscall
cycles from total cycles. CPI accuracy must therefore continue to use the
paired runs in section 7.1: the zero-service run is the user-only prediction;
the calibrated event-profile run is the user+active-kernel prediction. PMU
errors are reported directly in two masks: functional user counters versus
`:u` oracle counters, and `user_plus_synthetic_kernel_pmu` versus `:uk` oracle
counters. Involuntary scheduling stays explicitly zero until a deployable
trace-visible predictor exists; it must not be absorbed into the syscall table.
Page-fault and IRQ contributions use their independent models in section 13.

Starting with `fastsim-stats-v5`, those `totals` fields are compatibility and
model-attribution diagnostics. Formal consumers must select an explicit
`user` or `user-plus-kernel` run and read only that report's `scope_metrics`.
The CLI/config validator rejects user-scope kernel service and rejects a
user-plus-kernel scope with no kernel service model, so a mislabeled paired
input cannot silently enter the accuracy calculation.

## 13. First-touch page-fault and periodic IRQ models

The next inference-side slice adds two independent, default-off models. Their
profiles use the same 14 fields as a syscall profile after removing `sysnum`:

```
service_cycles:instructions:uops:branches:branch_misses:
l1d_accesses:l1d_misses:l2_accesses:l2_misses:
llc_accesses:llc_misses:dtlb_accesses:dtlb_misses:blocked_wall_cycles
```

Example configuration:

```ini
trace.require_virtual_page_token = true

page_fault.event_model = true
page_fault.cache_state_model = true
page_fault.probability_ppm = 250000
page_fault.background_write_probability_ppm = 300000
page_fault.allocation_syscalls = 9,12,25,28
page_fault.allocation_window_records = 262144
page_fault.allocation_probability_ppm = 750000
page_fault.allocation_write_probability_ppm = 760000
page_fault.allocation_probability_table = 9:800000:810000,12:600000:610000,25:750000:760000,28:700000:710000
page_fault.event_profile = 20:30:40:5:1:10:2:2:1:1:1:8:2:0

irq.event_model = true
irq.period_cycles = 1000000
irq.event_profile = 120:180:240:30:2:80:8:8:3:3:1:50:4:0
```

The page-fault model treats the first appearance of a valid virtual-page token
in each trace stream as a candidate. It records three trace-visible channels:

- a first touch within `allocation_window_records` records of the most recent
  `mmap`, `brk`, `mremap`, or `madvise` marker;
- a background first read outside that window;
- a background first write outside that window.

Selection uses deterministic integer parts-per-million accumulators; no PRNG,
workload ID, oracle field, or host thread order participates. The current
calibrator has eight numeric degrees of freedom: background read and write,
one global allocation rate, four allocation-syscall deviations, and one shared
allocation-write deviation. Fixed ridge penalties shrink the syscall and
write deviations and both background channels. Workload labels partition
leave-one-workload-out validation folds only and are never inference features.

A simpler two-coefficient first-read/first-write model was evaluated because
it exactly matched the four page-fault-heavy short pilots. It was rejected on
the complete 10-workload C4 set: LBM, TeaLeaf and Neutron have many trace-first
touches but zero measured faults, and leave-one-workload-out event WAPE rose
above 300%. This is direct evidence that access type alone does not reconstruct
pre-ROI page residency.

`page_fault.cache_state_model=true` functionally writes the selected page's
64 physical cache lines before its first user demand. It mutates private cache,
LLC and directory state but adds no target time or PMU counters. It is valid in
the `user` pipeline with `page_fault.event_model=false`; the paired
`user-plus-kernel` run uses the same selector/state transition and additionally
adds the calibrated page-fault service and kernel PMU. The reports expose
`page_fault_cache_state_pages` and `page_fault_cache_state_lines` so this state
cannot be mistaken for user accesses.

`page_fault.syscall_semantic_model=true` changes how the selected page is
identified. It resolves the hot-record token through `coreN.fst.vmap`, tracks
successful non-MAP_POPULATE `mmap` and successful `munmap` ranges, and selects
the first access inside a live demand-faultable range exactly. This exact path
does not use a fitted probability and fails closed when syscall fields or the
token map are incomplete.

A source-level trace may begin after startup-created anonymous mappings and
therefore cannot relate every later first write to a trace-visible `mmap`.
`page_fault.syscall_semantic_fallback_write_probability_ppm` is one frozen,
workload-independent residual rate for first writes outside exact live ranges.
The calibration probe separately conserves the first-write partition,
`exact_semantic_write_candidates + fallback_write_candidates ==
first_touch_write_candidates`, fits the one residual rate on calibration
cases, and reports leave-one-workload-out folds. Inference never reads a
workload label. This fallback is explicitly a COW/demand-zero approximation,
not syscall semantics; exact accesses, their write subset, fallback writes,
and fallback selections have separate report counters.

Page residency survives functional warmup, so a page first touched before the
measurement barrier cannot fault again in the ROI. This is essential for a
checkpoint restored at an ROI marker: without a pre-marker functional prefix
or a resident-page sidecar, an ROI-only trace cannot distinguish a resident
page that merely appears for the first time in the trace from a lazily mapped
page that really faults. Read/write type and allocation-syscall recency are
only statistical proxies for that missing state. The model therefore
represents attributable active minor-fault handling, but cannot exactly
distinguish anonymous, file-backed, huge-page, remapped, major-fault, or
pre-ROI-residency cases without additional trace state or syscall arguments
and return values.

The IRQ model emits one attributable background event per
`irq.period_cycles` of foreground active core time. The foreground base already
contains user execution, syscall service and page-fault service, but excludes
synthetic IRQ service itself; this prevents a long handler from recursively
creating extra events. IRQ service is appended once to each core's measured
time after the shared-memory frontier closes. This statistical slice therefore
does not perturb user cache/coherence state or claim to identify an exact IRQ
instruction boundary.

The report exposes `page_fault_first_touch_candidates`, first-write and
background read/write candidate counts, aggregate allocation-recency
histograms, `page_fault_allocation_by_syscall` read/write histograms, and
`page_fault_untracked_accesses` in addition to the three kernel PMU domains.
All three models remain disabled in the production profile until an independent
calibration set has a conserved kernel-events-v2 oracle. In particular, the
legacy mixed `irq_idle_kernel_cycles` residual is not a valid IRQ calibration
target.

### 13.1 Hierarchical C4 pilot (2026-08-14)

The 10-workload C4 diagnostic produced 3,518/350/0/149 selected candidates for
syscalls 9/12/25/28. Strong shrinkage consequently kept all four allocation
rates within 69 ppm of the 647,880 ppm global fallback; it did not learn a
workload lookup table. Background read/write rates shrank to 113/105 ppm.

Generalization nevertheless failed: training page-fault event/active-cycle
WAPE was 47.60%/43.69%, and leave-one-workload-out WAPE was 62.44%/56.99%.
The decisive contradictions are
trace-observable rather than optimizer noise: LBM and Neutron have zero oracle
faults despite 11,045 and 8,705 background first touches, while the same mmap
syscall-9 channel needs about an 85% rate for zstd/Graph500 but much lower rates
for SPH/NAMD. Syscall number, read/write, and recency cannot reveal mapping
flags, return success, file/anonymous backing, or whether the page faulted
before the trace warmup began.

The implementation and diagnostics are retained, but this fitted profile is
not promoted as a formal accuracy model. The next data-contract increment is
an allocation-result sidecar (syscall arguments/return and mapping identity)
or, preferably, state-only page-fault/page-residency markers from gem5. A
workload ID or fitted workload CPI residual remains forbidden.

## 14. Kernel-events-v2 oracle gate

Calibration requires a new gem5-FS artifact with schema
`tcsim-gem5-fs-kernel-events-v2`. Both `aggregate` and every `per_core` row use
these cycle fields:

```json
{
  "measured_cycles": 0,
  "n_user": 0,
  "user_cycles": 0,
  "syscall_kernel_cycles": 0,
  "page_fault_kernel_cycles": 0,
  "irq_kernel_cycles": 0,
  "scheduler_kernel_cycles": 0,
  "idle_cycles": 0,
  "unknown_kernel_cycles": 0,
  "blocked_wall_cycles": 0,
  "cpi_user": 0.0,
  "cpi_user_plus_kernel": 0.0,
  "pmu_user": {},
  "pmu_user_plus_kernel": {},
  "pmu_kernel_by_class": {
    "syscall": {}, "page_fault": {}, "irq": {},
    "scheduler": {}, "idle": {}, "unknown_kernel": {}
  },
  "syscall_profiles": []
}
```

`per_core` additionally carries a dense `core_id`. The required identities are:

```
measured_cycles
  = user + syscall + page_fault + IRQ + scheduler + idle + unknown

cpi_user
  = user_cycles / n_user

cpi_user_plus_kernel
  = (user + syscall + page_fault + IRQ + scheduler + unknown) / n_user
```

Idle and blocked wall time do not enter application CPI. Scheduler is active
CPL0 execution and therefore does; unknown is also included when a diagnostic
run permits it, while the formal gate requires unknown to be zero. Nested entry
attribution must use a stack, so an IRQ inside a syscall is charged to IRQ until
interrupt return and then resumes syscall attribution. Page fault is a
synchronous exception, not an IRQ. The standalone gate
`tools/validate_kernel_events_oracle.py` checks cycle conservation, aggregate
versus per-core sums, dual-CPI formulas, matching `:u`/`:uk` PMU fields,
per-class PMU conservation, per-sysnum syscall conservation, and the configured
maximum unknown ratio. Its default requires zero unknown cycles.

The FastSim comparison is deliberately a paired run:

```bash
python3 tools/compare_kernel_event_accuracy.py \
  oracle/kernel_events.json user.json user-plus-kernel.json \
  --output accuracy.json
```

The first report must declare `measurement_scope=user` and contain zero
synthetic active cycles; the second must declare
`measurement_scope=user-plus-kernel`. Both reports must have exactly the
oracle's `n_user` trace uops. The tool reports CPI and PMU
errors independently for `user` and `user_plus_kernel`, plus syscall,
page-fault, IRQ, scheduler, idle and unknown component residuals and both host
throughputs. PMU source `taotrace-path-class-v2` consists of TaoTrace
commit/cache-path proxy counts, not direct host architectural `perf` counters.
`tools/calibrate_kernel_event_profiles.py` consumes exact class and per-sysnum
PMU when v2 attribution is present; a legacy scope-only oracle takes an
explicitly labeled common-rate fallback and is not final PMU accuracy evidence.
The generated per-sysnum table is paired with a class-average default profile,
so a syscall number first encountered in a held-out trace still conserves its
timing and PMU contribution instead of silently adding unclassified latency.
`tools/summarize_kernel_event_accuracy.py` emits separate user and user+kernel
CPI/PMU mean, P50, P90, and P99 APE, plus WAPE, bias, worst-case error and
throughput. The mandatory definitions and report layout are specified in
[`accuracy-reporting-contract.md`](accuracy-reporting-contract.md).

The complete calibration flow can be run directly from one or more completed
matrix directories:

```bash
python3 tools/run_kernel_event_accuracy_pipeline.py \
  --matrix /path/to/calibration-matrix \
  --split calibration \
  --page-fault-cache-state-model \
  --page-fault-syscall-semantic-model \
  --output-dir tmp/kernel-events-v2/formal-c04
```

For held-out core counts, freeze the generated config and prevent held-out
oracle leakage by passing it explicitly:

```bash
python3 tools/run_kernel_event_accuracy_pipeline.py \
  --matrix /path/to/held-out-matrix \
  --split held-out \
  --page-fault-cache-state-model \
  --page-fault-syscall-semantic-model \
  --kernel-config tmp/kernel-events-v2/formal-c04/kernel-events.cfg \
  --output-dir tmp/kernel-events-v2/held-out
```

When a failed original matrix has been completed by explicit retry matrices,
pass all of them with `--allow-partial-matrix`. The driver then accepts only
tasks whose sample is `completed` with return code zero, or `skipped` with the
exact reason `current successful result exists`; it ignores every other task
state and still rejects duplicate core/workload keys. The strict oracle gate is
applied to every selected result before any FastSim run.

The driver first applies the strict oracle gate, then writes scope-locked
`user.json` and `user-plus-kernel.json` FastSim reports, per-case comparisons,
aggregate JSON/CSV/Markdown,
and a provenance manifest. Runs are sequential so the reported host
throughput is not distorted by concurrent FastSim cases. Both paired runs
enable only the fail-closed cross-page compatibility path documented in
`docs/gem5-trace-contract.md`: a tokenless memory record is accepted only when
its physical offset and size prove that it crosses a 4 KiB boundary, and its
DTLB/page-fault identity remains explicitly untracked.

## 15. Recollection boundary

KVM/ROI checkpoints do not need to be rebuilt. Corrected collection begins
after checkpoint restore in the O3 sampling phase. Existing user-only FSTs and
their `CPI_user`/`:u` labels remain useful when marker and target checks pass;
corrected `CPI_user_plus_kernel`, class PMU and syscall profiles must be
recollected because a user-only trace plus syscall numbers cannot reconstruct
them. Old `kernel-v2` formal runs that charged `idle=poll` PAUSE residency to
futex are calibration diagnostics only, not final combined-CPI accuracy data.
