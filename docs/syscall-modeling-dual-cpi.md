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
- the syscall number for that marker;
- the existing branch, register-dependency, and address information.

The functional trace **must not contain**:

- kernel-mode (CPL=0) instructions;
- kernel cache/TLB accesses;
- measured syscall wall/cycle duration;
- syscall arguments or return values;
- any gem5-FS-only scheduler / blocking / PMU-oracle signal.

This is the same information drmemtrace exposes via
`TRACE_MARKER_TYPE_SYSCALL = sysnum`. gem5-FS has strictly more information
available internally (real kernel stream, cycle-accurate service time); the
contract deliberately discards everything past the sysnum so all three
producers stay equivalent.

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

FastSim's only syscall input is the syscall number. It must still model the
cost. On encountering a syscall marker it looks up a per-`sysnum` synthetic
cost and charges it as an on-core, serializing system operation.

- Static per-`sysnum` table: `sysnum -> {service_cycles, class}` where class is
  one of `{cheap, medium, mapping, blocking}` (e.g. `getpid`/`clock_gettime`
  cheap; `mmap`/`mprotect` mapping; `futex`/`read`/`poll` blocking).
- Blocking vs non-blocking is derived **statically from the sysnum**, not from
  a per-instance marker, so the contract stays producer-independent.
- Only the **on-core active** portion of syscall service is charged into
  per-uop CPI. Off-core blocked/descheduled time is never charged to the bound
  thread's CPI (it belongs to makespan only), matching the oracle rule in
  section 6.
- The table values are **calibrated offline** from the gem5-FS oracle
  (section 6) and then frozen. Given a fixed trace + fixed table, the cost is
  fully deterministic, preserving bit-exact replay.

This subsumes the existing single `syscall_service_latency` scalar: that scalar
becomes the fallback used when a sysnum is absent from the table.

## 5. Syscall-number storage

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

The `abi` (e.g. x86-64 Linux) is a single per-trace property and is stored once
in the trace header / trace metadata, not per record.

Rejected alternative — index-keyed protobuf sidecar (`coreN.syscalls.pb` with
`record_index, syscall_number, abi`): clean separation, but `record_index` is
fragile under re-slicing and forces every producer to emit and keep a second
file in sync. Kept only as a documented fallback if inline storage is ever
found to break a producer.

`BinaryTraceWriter` already sets a `kFeatureSyscallMarkers` header bit
(`src/trace.cpp`); when the bit is set, `address` on syscall records carries a
sysnum. Legacy traces without the bit fall back to the scalar cost model.

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

Kernel cycles in the oracle are split into three classes:

1. **user-mode cycles** → numerator of `CPI_user`;
2. **syscall-service on-core kernel cycles** (triggered by a user-mode syscall
   instruction) → folded into that `sysnum`'s cost → numerator of `CPI_incl`;
3. **idle-spin / IRQ / blocked-wait cycles** → neither CPI; makespan only.

Class 3 is essential: stockfish's 79% kernel spin must land here, or it would
either poison `CPI_incl` (as fake syscall cost) or `CPI_user`.

## 7. Dual-CPI definition and reporting

Both CPIs share the **same denominator**: user-mode instruction/uop count
(`N_user`). The syscall instruction itself is one user-mode instruction. The
difference is only in the numerator.

| metric | numerator | denominator | measures |
|---|---|---|---|
| `CPI_user` | user-mode cycles | `N_user` | pipeline / cache / branch model fidelity |
| `CPI_incl` | user-mode cycles + synthetic syscall on-core cost | `N_user` | deployment-visible application CPI |

Then `CPI_incl - CPI_user = syscall_oncore_cost / N_user`, an interpretable
per-user-instruction syscall tax that can be attributed on its own.

**Hard rule:** do not use gem5 native `numCycles / (all committed incl. kernel)`
as the second CPI. That denominator contains kernel instruction count, which
FastSim structurally cannot reproduce without leaking kernel detail. gem5's
two oracle CPIs must both use `N_user` as denominator so FastSim can match
them. Native full-system CPI may be kept as an internal sanity check but must
not enter the FastSim error gate.

FastSim reports two errors:

- `err_user` = relative error of `CPI_user` vs gem5 oracle `CPI_user`
  → quality of the pure compute model, isolated from syscalls.
- `err_incl` = relative error of `CPI_incl` vs gem5 oracle `CPI_incl`.
- `err_incl - err_user` isolates the quality of the synthetic syscall model.

### 7.1 How FastSim derives the two CPIs

The syscall is a serializing operation on the critical path, so its cost is
**not** an additive term that can be subtracted from the total post hoc (the
`syscall_*_cycles` counters are explicitly documented as raw, possibly
overlapping components, not a CPI decomposition). The overlap-safe derivation
is a **paired run on one binary**:

- **`CPI_user`**: run with `syscall.cost_model=true` and every in-trace sysnum
  mapped to zero on-core service cycles (only the structural serialize-drain +
  1-cycle restart remain, i.e. the syscall behaves as a bare serialization
  point). Numerator = user-mode cycles.
- **`CPI_incl`**: same binary, same trace, with the calibrated per-sysnum table.
  Numerator = user-mode cycles + synthetic syscall on-core cost.

Both runs share an identical trace and therefore an identical `N_user`, so
`CPI_incl - CPI_user` is exactly the synthetic syscall tax. This reuses the
established same-binary paired-A/B methodology and keeps every non-syscall
causal state bit-identical between the two runs.

Native full-system CPI is never used as a FastSim gate (see below).

Naming: the second metric is **syscall-inclusive application CPI**, not
"kernel-inclusive CPI" — it contains only active syscall-service cost, never
idle/IRQ/blocked kernel execution.

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
   content. Confirms the section 6 three-way split is realizable and quantifies
   stockfish idle spin.
2. **Define oracle CPI contract:** freeze the section 7 formulas and the
   section 6 folding rules; emit `oracle/cpi.json` with both CPIs on `N_user`.
3. **FastSim sysnum plumbing + synthetic model:** read inline sysnum on syscall
   records, add the per-sysnum cost table + config, keep the scalar as
   fallback. Behind a config switch (off = current scalar behavior) so A/B uses
   one binary.
4. **Dual-CPI reporting:** add `CPI_user` / `CPI_incl` and `err_user` /
   `err_incl` to stats and the validation summary.
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
- the production default remains `syscall.cost_model=false`.

The `811.tealeaf_s` c4/500K end-to-end run produced 16 `futex(202)` markers and
no kernel PC in the functional FST. Paired scalar runs consumed the same
4,097,887-UOP trace:

- model off: 0 syscall service cycles;
- model on, `202:1408`: 22,528 syscall service cycles;
- oracle syscall-service total: 22,529 cycles.

The one-cycle difference comes from integer per-instance calibration:
`22,529 / 16 = 1408.0625`.
