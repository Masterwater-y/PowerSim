# Portable drmemtrace metadata audit (2026-08-14)

Status: completed with a real four-thread workload.  The converted trace passed
the DynamoRIO invariant checker.

## 1. Scope

This audit answers what a normal deployment machine can collect with the
user-mode offline `drmemtrace` client.  The portable scope deliberately excludes:

- Intel Processor Trace and `-enable_kernel_tracing`;
- privileged hardware PMU collection;
- whole-system tracing;
- `-use_physical`, because physical-page lookup is OS- and permission-dependent.

`-record_syscall` is included.  It uses the normal syscall-boundary callbacks and
requires no special hardware or kernel patch.  Syscall numbers and argument
counts are ABI-specific, so a collector must select the table for its target OS
and architecture.  Production collection must pin and capability-check the
DynamoRIO version: this result validates the source revision and build listed
below, not every historical drmemtrace release.

## 2. Environment and workload

| Item | Value |
|---|---|
| Host | Linux `5.4.143.bsk.business.5-amd64`, x86-64 |
| CPU | Intel Xeon Platinum 8457C, 192 logical CPUs |
| DynamoRIO source | `436da3609c400dd9ca6d3dcfeebe084839180b61` |
| DynamoRIO build | `drrun 11.91.20668`, Release |
| Workload | `workloads/uarch_excitation/bin/uarch_l1_48k` |
| Arguments | `4 1 1 1234` |
| Result | `uarch_l1_48k checksum=224459780` |

The workload is an existing statically linked FastSim workload.  It allocates
and first-touches memory, creates four pthreads, changes their affinity, uses a
pthread barrier/futex, performs the user computation, and exits.

The main collection command was:

```bash
env TAO_DISABLE_M5=1 drrun -t drmemtrace -offline \
  -outdir <audit-dir>/enhanced \
  -record_syscall \
  '1|3&5|2&9|6&10|3&12|1&13|4&14|4&28|3&56|5&60|1&63|1&89|3&158|2&202|6&203|3&218|1&231|1&273|2&302|4' \
  -- workloads/uarch_excitation/bin/uarch_l1_48k 4 1 1 1234
```

The raw trace was converted with `drraw2trace`.  Analysis used the official
`basic_counts`, `syscall_mix`, `view`, and `invariant_checker` tools.  Generated
evidence is under:

```text
tmp/drmemtrace-universal-audit-20260814/
  baseline/   # default drmemtrace syscall metadata
  enhanced/   # explicit -record_syscall collection and converted trace
  measured/   # same enhanced collection wrapped by /usr/bin/time -v, then converted
```

These are experiment artifacts and are not intended for source control.

## 3. Direct trace results

The trace contains 19,964,201 fetched/executed user-mode instructions across
four threads and 44 syscalls.  The syscall mix was:

| Syscall | Number | Count |
|---|---:|---:|
| `write` | 1 | 1 |
| `fstat` | 5 | 1 |
| `mmap` | 9 | 3 |
| `mprotect` | 10 | 3 |
| `brk` | 12 | 5 |
| `rt_sigaction` | 13 | 2 |
| `rt_sigprocmask` | 14 | 1 |
| `madvise` | 28 | 3 |
| `clone` | 56 | 3 |
| `exit` | 60 | 3 |
| `uname` | 63 | 1 |
| `readlink` | 89 | 1 |
| `arch_prctl` | 158 | 1 |
| `futex` | 202 | 5 |
| `sched_setaffinity` | 203 | 4 |
| `set_tid_address` | 218 | 1 |
| `exit_group` | 231 | 1 |
| `set_robust_list` | 273 | 4 |
| `prlimit64` | 302 | 1 |

### 3.1 Marker counts

| Converted-trace record | Default collection | Explicit `-record_syscall` |
|---|---:|---:|
| syscall-number marker | 44 | 44 |
| maybe-blocking marker | 6 | 6 |
| function/syscall ID marker | 10 | 84 |
| argument marker | 30 | 138 |
| return-value marker | 5 | 40 |
| failure/errno marker | 4 | 3 |
| timestamp marker | 984 | 984 |
| CPU-ID marker | 984 | 984 |
| physical/virtual mapping marker | 0 | 0 |
| kernel event/transfer marker | 0 | 0 |
| kernel syscall trace start/end | 0 | 0 |
| context-switch start/end | 0 | 0 |
| syscall unschedule/schedule/timeout | 0 | 0 |
| core wait/idle | 0 | 0 |

The two executions differ by one failed `futex`, which is normal scheduling
nondeterminism.  It does not change the capability result.

Default Linux drmemtrace already treats `futex` specially: all five futex calls
produced six scalar argument records and a raw return value, and the failed calls
also produced `errno=11` (`EAGAIN`).  Explicit `-record_syscall` expanded this to
all 40 syscall invocations that return.  The four non-returning `exit` and
`exit_group` calls have arguments but correctly have no post-call return or
timestamp record.

### 3.2 One observed syscall sequence

The actual converted records around `readlink` were:

```text
timestamp 13431158794079332
CPU 35
syscall 89
function==syscall #89
arg0 0x4a8ea3
arg1 0x7ffcc8522e70
arg2 0x1000
function==syscall #89
return 0x46
timestamp 13431158794079344
CPU 35
```

This demonstrates that the trace contains scalar syscall arguments, the raw
return register (`0x46`, or 70 bytes), timestamps on both sides, and CPU IDs.
Pointer arguments are only pointer values: the 70 output bytes written to the
`readlink` buffer are not copied into syscall metadata.

The same mechanism captured concrete memory-management semantics.  One observed
`mmap` carried all six arguments and returned virtual address
`0x7f9f624aa000`; the following `mprotect` carried its address, length and
protection arguments.  A failed `futex` carried all six arguments, returned
`-11`, and was followed by `SYSCALL_FAILED=11`.

### 3.3 Timing and migration information

Subtracting the two trace timestamps gives an as-traced syscall wall duration.
Observed examples were:

| Syscall | As-traced duration, microseconds |
|---|---:|
| `readlink` | 12 |
| `mmap` | 6--14 |
| `mprotect` | 3--5 |
| `clone` | 24--35 |
| `futex` | 2--578 |
| `sched_setaffinity` | 24--26 |

All four `sched_setaffinity` calls had different pre/post CPU IDs, as expected.
This proves that migration can be observed at a syscall boundary.  These
durations are wall time under instrumentation: they combine active kernel
execution, blocking, preemption and tracing overhead.  They must not be used as
active CPL0 cycle labels or added directly to CPI.

## 4. Portable information that is really available

For a workload that exercises the corresponding events, normal offline
drmemtrace can provide:

1. User-mode instruction fetches, instruction encodings, branches, and virtual
   memory-reference addresses and sizes.
2. PID/TID and per-thread trace shards.
3. Periodic and syscall-boundary timestamp and CPU-ID markers.
4. Every syscall number.
5. For explicitly selected syscalls: the configured number of scalar ABI
   arguments, raw return-register value, failure flag and errno.
6. A best-effort `MAYBE_BLOCKING_SYSCALL` classification.  The current built-in
   coverage is incomplete and Linux-specific, so it is a hint, not proof that a
   particular invocation slept.
7. User-visible signal/control-transfer markers and an uncompleted-instruction
   marker if those events occur.  No such event occurred in this workload.
8. Optional user function and heap-call arguments/returns through the separate
   function-tracing facility.  This is not kernel execution information.

The official format documentation for these records is in the DynamoRIO
[trace marker reference](https://dynamorio.org/namespacedynamorio_1_1drmemtrace.html)
and [function/syscall tracing documentation](https://dynamorio.org/sec_drcachesim_funcs.html).

## 5. Information that is not present

The workload without DynamoRIO consumed 182 minor page faults, four voluntary
context switches and four involuntary context switches according to
`/usr/bin/time -v`.  The measured DynamoRIO collection process consumed 1,552
minor page faults, two voluntary context switches and four involuntary context
switches; its converted trace passed the invariant checker.  Nevertheless, that
trace contained zero page-fault records (there is no ordinary user-trace
page-fault marker), zero kernel event/transfer records, zero context-switch
records, and zero unschedule/schedule records.

Therefore normal user-mode drmemtrace does **not** provide:

- minor/major page-fault occurrence, fault address, cause, service path or
  active service cycles;
- IRQ/softirq vector, handler execution, duration or attribution to this process;
- kernel instructions or kernel memory references;
- active CPL0 cycles, kernel retired instructions, cache/TLB/branch PMU events;
- a reliable split of syscall wall time into active execution, blocked time,
  run-queue time and preemption;
- exact scheduler switch/wakeup edges for ordinary native traces;
- pointed-to syscall input/output buffer contents.

Kernel PT could add syscall kernel instruction flow on supported Intel systems,
but it is explicitly outside this portable contract and still would not provide
a portable active-cycle/PMU oracle.  Physical mapping markers are also excluded
from the portable contract because access to physical mappings depends on the
host OS and permissions.

## 6. FastSim consequence

The resulting normative FST v7 layout and adapter mapping are documented in
[`fst-v7-drmemtrace-conversion-contract.md`](fst-v7-drmemtrace-conversion-contract.md).

The prior statement that drmemtrace exposes only the syscall number is too
strong.  The accurate statement is:

> Canonical FST v7 stores the syscall number inline and retains portable
> drmemtrace metadata—when actually captured—in an aligned sparse table:
> selected scalar arguments, raw return, failure/errno, maybe-blocking hint,
> timestamps, CPU IDs and thread ID.

The implementation keeps the 64-byte FST hot record unchanged and appends one
128-byte row per syscall to the same file, keyed by both `record_ordinal` and
`syscall_ordinal`:

```text
thread_id, sysnum, args[0..5], retval_raw, failed, errno,
pre_timestamp_us, post_timestamp_us, pre_cpu, post_cpu, maybe_blocking,
validity_bits
```

Header feature bit 3 plus table offset/count/row-size make truncation and
misalignment detectable. The duplicated sysnum is checked against the inline
marker. `fastsim-functional-syscall-v2` JSONL is an optional audit mirror, not
a replay dependency. Separate validity bits preserve the observed distinction
between a captured zero/false and a marker that drmemtrace did not emit (for
example, a non-returning `exit` has no return or post boundary).

This supports semantic features without workload-name fitting:

- `mmap/brk/mprotect/madvise`: range, size, flags, protection/advice and result;
- `read/write`: fd, requested byte count, actual byte count and failure;
- `futex`: operation, expected value, timeout pointer and success/failure;
- `clone`: flags and thread-creation result;
- affinity calls: requested mask pointer/size plus observed boundary migration.

The pure user-mode FastSim pipeline ignores this metadata. The
user-plus-kernel pipeline may use it to select a semantic event profile and
estimate blocking likelihood, but must continue to obtain page-fault, IRQ,
scheduler and active-kernel-cycle truth from a separate perf/eBPF or gem5-FS
oracle.
