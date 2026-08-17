# gem5 TaoTrace kernel-event oracle implementation

Producer-side companion to `docs/syscall-modeling-dual-cpi.md`. The active
implementation lives in `/data00/yinhaolang/gem5-fs/src/cpu/o3/probe/` and is
developed from the FastSim mirror under `tmp/kernel-events-v2/gem5/`. The
exported functional trace remains user-only and deployable; all CPL0 detail is
written only to `oracle/`.

## Functional trace contract

At commit, TaoTrace decodes the x86 CPL of the instruction:

- a CPL3 normal instruction becomes one functional FST record;
- a CPL3 syscall instruction becomes one `op_class=-1` marker whose `address`
  field contains the syscall number;
- FST v7 appends one 128-byte sparse row for that marker. Entry arguments,
  timestamps/CPU, return value, failure/errno, and post timestamps/CPU are
  governed independently by validity bits;
- CPL0 instructions and their memory accesses never enter the FST;
- an interrupt or exception never fabricates a syscall marker.

The per-core target is counted in functional user records. The oracle and FST
therefore share the same `n_user`, including one marker per syscall boundary.
A core already in CPL0 when the global ROI marker opens is ignored until the
first attributable user instruction or precise from-user exception, avoiding
cross-core marker-skew tails.

The producer captures Linux x86-64 argument registers only for syscall
numbers in the configured `syscall_arg_counts` map. It associates a return
using the unique `(page-aligned CR3, user RSP, gateway PC + 2)` key across all
TaoTrace instances. A `PreCommit` observation occurs before the first returned
user instruction updates the committed rename map, preserving the kernel RAX
return value. Ambiguous, non-returning, or trace-truncated calls retain invalid
return fields. gem5 context IDs are hardware contexts, not guest TIDs, so FST
thread-ID validity remains clear. `tao_trace/syscall_capture.json` records this
capture contract and the timestamp origin.

## Mutually exclusive cycle classes

`KernelEntry` is an accepted commit-stage probe carrying the entry tick, core,
from-user flag, fault object and source. TaoTrace maintains a nested class
stack with these domains:

| Class | Entry/exit evidence | Application CPI |
|---|---|---:|
| `user` | decoded CPL3 commit | yes |
| `syscall` | confirmed user syscall boundary | yes |
| `page_fault` | exact x86 PageFault entry | yes |
| `irq` | accepted external interrupt, restored by IRET | yes |
| `scheduler` | reserved for an exact task-switch hook | yes |
| `idle` | HLT/MWAIT or poll-idle PAUSE interval | no |
| `unknown_kernel` | unmatched real privileged transition | formal error |

The classes partition elapsed ticks. They are not added on top of cache-miss
commit latency: any miss stall is already inside the active class interval.
Nested events do not double count. For example, IRQ entry pushes syscall, IRQ
ticks accrue until IRET, then syscall resumes.

gem5 control faults such as `warn fault`, `hack fault`, `inform fault`,
re-execution and syscall-retry faults do not enter the guest kernel and are
ignored by this accounting. Formal validation requires the remaining
`unknown_kernel_cycles` to be zero.

## `idle=poll` handling

The collection kernel boots with `idle=poll`, so a sleeping vCPU can execute a
PAUSE loop instead of HLT/MWAIT. Without this rule, a futex syscall can appear
to consume millions of active syscall cycles even though the core is merely
waiting for work.

gem5 currently decodes Intel `PAUSE` (`F3 90`) as a REP-prefixed `NOP`, and its
name/disassembly therefore cannot be used to recognize the instruction.
TaoTrace checks the x86 opcode and REP prefix directly. It opens a persistent
poll-idle interval only after the same PAUSE PC repeats at least 128 times with
at most 64 intervening commits. This hysteresis keeps isolated `cpu_relax()`
calls in active kernel code out of the idle class. An IRQ nests above idle;
after IRET the poll loop must be confirmed again before idle resumes.
If PAUSE disappears for more than 64 commits without an IRQ, the persistent
idle frame also closes; this covers polling loops that directly observe a wake
condition and keeps their wakeup tail in the active parent class.

The oracle records the detector name and both thresholds. Formal validation
rejects exact-PMU data without this metadata. The rule is still an explicit
approximation rather than an exact scheduler/idle hook, so the SPH futex pilot
is a semantic gate before formal collection. Idle cycles remain in
measured-cycle conservation but are excluded from `cpi_user_plus_kernel`.

## PMU scopes and conservation

The producer writes path-classified commit, branch, data-cache and DTLB proxy
counters. They are labeled `taotrace-path-class-v2`; they are not direct host
architectural `perf` counters.

Each per-core `kernel-events-coreN.json` contains:

- `pmu_user`, corresponding to the `:u` scope;
- `pmu_user_plus_kernel`, corresponding to active `:uk` with idle excluded;
- `pmu_kernel_by_class` for syscall, page fault, IRQ, scheduler, idle and
  unknown;
- `syscall_profiles`, grouped by syscall number with count, active cycles and
  PMU totals.

For every PMU field:

```
pmu_user_plus_kernel
  = pmu_user
  + syscall + page_fault + IRQ + scheduler + unknown
```

Idle PMU is retained for diagnosis but is not in the combined scope. The PMU
privilege domain follows the committed instruction's decoded CPL, independently
of the elapsed-cycle frame; this keeps user-decoded syscall transition uops in
`:u` and makes the identity exact.

## Oracle artifacts

The O3 sampling result contains:

```
oracle/
  cpl_class.jsonl            # detailed tick/commit diagnostics
  cpi-coreN.json             # compatibility per-sysnum cycle view
  cpi.json                   # compatibility merged view
  kernel-events-coreN.json   # authoritative classified truth
  kernel_events.json         # authoritative merged truth
tao_trace/
  coreN.fst                  # user-only FST v7 + sparse syscall metadata
  syscall_capture.json       # producer/ABI/argument/timestamp contract
  trace.json
```

The authoritative formulas are:

```
measured = user + syscall + page_fault + IRQ + scheduler + idle + unknown
CPI_user = user / N_user
CPI_user_plus_kernel
  = (user + syscall + page_fault + IRQ + scheduler + unknown) / N_user
```

`cpi.json:cpi_incl` is retained for legacy consumers and now follows the same
active user+kernel numerator, but the formal accuracy gate uses
`kernel_events.json:cpi_user_plus_kernel`.

## Validation and collection order

Before a formal matrix:

1. Build gem5 after all jobs using the previous binary have exited.
2. Run short zstd, NAMD and SPH-EXA pilots.
3. Require dense cores, exact FST target, `n_user == records`, cycle and PMU
   conservation, zero unknown cycles, and syscall-profile conservation.
4. Confirm SPH/NAMD poll waits moved from syscall to idle without losing IRQ or
   page-fault entries.
5. Collect a corrected 4-core calibration matrix; only then launch held-out
   8/16/32-core matrices.

Use `tools/validate_kernel_events_oracle.py` for the standalone oracle gate and
the TCSim `validate_gem5_usergate_result.py` gate for a complete result.

KVM/ROI checkpoints are reusable because all changes apply after restore in
the O3 trace/oracle phase. Old user-only FSTs remain useful for `CPI_user` when
their manifests pass, but corrected combined CPI and kernel PMU labels require
new O3 sampling. Never overwrite the old result directories; use a new matrix
name so diagnostic and formal data cannot be mixed.
