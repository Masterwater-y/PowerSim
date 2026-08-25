# gem5 functional trace contract

FastSim consumes functional execution facts. Timing/cache/coherence oracle
columns such as `commit_tick`, `ready_tick`, `path_class`, and `coh_oracle`
are never used to drive simulation state.

The gem5-only `oracle/wrong_path.jsonl` squash/stage sidecar is governed by
[`gem5-wrong-path-oracle.md`](gem5-wrong-path-oracle.md). It is offline
attribution data and must never be converted into FST or consumed by production
FastSim inference.

The authoritative FST v7 byte layout, syscall validity rules, and expected
drmemtrace marker conversion are specified in
[`fst-v7-drmemtrace-conversion-contract.md`](fst-v7-drmemtrace-conversion-contract.md).

## Canonical frontend policy

The simulator runtime consumes the canonical FST functional IR; it does not
decode architectural instruction bytes or choose a macro-instruction-to-UOP
decomposition. The currently validated producer is the gem5 functional
exporter, so `gem5 functional trace` remains the user-facing name for this
contract. JSONL and aligned Parquet are ingestion forms, while binary FST is
the compact runtime form. The current writer emits FST v7 while retaining the
same 64-byte hot record introduced by v5.

Raw DynamoRIO/drmemtrace input is not currently accepted. A future offline
DR-to-FST adapter may target this same contract, but it must first perform
target-ISA decode, gem5-compatible UOP/OpClass lowering, dependency-distance
construction, and branch/event normalization. Because drmemtrace normally
contains virtual rather than physical data addresses, strict cache,
coherence, CHA, and DRAM comparison additionally requires a trustworthy
physical-address source. A virtual-only conversion is permitted only as an
explicit non-strict exploratory input.

This separation is intentional: decoder/lowering work is outside the
simulation hot path and outside the current gem5 CPI/PMU accuracy milestone.
Producing records with the right byte layout but guessed UOPs, OpClasses,
dependencies, or physical addresses does not satisfy this contract. See
section 18 of `gem5-source-aligned-p99-plan.md` for the cross-ISA comparison,
future conversion pipeline, and acceptance gates.

## Supported inputs

### Direct gem5 TaoTrace FST

Full-system TaoTrace writes canonical FST v7 directly. It supports both the
validated user-only mode and an opt-in native user+kernel mode. Its 64-byte
functional records and 128-byte syscall rows use the same binary contract a
drmemtrace adapter must target; FastSim does not branch on producer identity.
For Linux x86-64, TaoTrace can preserve configured raw syscall arguments,
entry and return timestamps/CPU IDs, raw RAX return bits, failure/errno, and a
maybe-blocking hint. Every optional field is validity-mask governed.

TaoTrace intentionally leaves native thread ID invalid because gem5's context
ID is not the guest OS TID. Return matching uses the unique `(CR3, user RSP,
return PC)` key and fails closed on ambiguity or when the trace ends before a
return. `syscall_capture.json` beside the trace records the producer, ABI,
argument-count map, timestamp unit/origin, and association method. In the
default user-only mode, kernel instructions remain separate oracle data. In
native mode, active CPL0 instructions enter the functional FST; event class,
classified idle, and independent kernel PMU truth remain oracle-only.

### JSONL

Use manifest format `gem5-jsonl` or convert a single file:

```bash
./build/fastsim convert-gem5 \
  --input core0.jsonl --output core0.fst --core 0 \
  --syscall-abi linux-x86_64 \
  --syscall-output core0.syscalls.jsonl
```

Each JSON object represents one retired UOP/operation. Recognized functional
fields are:

| Field | Meaning |
|---|---|
| `macro_pc` or `pc` | Architectural/macro instruction PC |
| `paddr` or `physical_address` | Physical data address used by caches/coherence/DRAM |
| `vaddr` | Virtual data address used to derive an opaque DTLB page identity |
| `address` | Legacy virtual fallback, only allowed when strict physical mode is disabled |
| `size` | Memory access bytes |
| `is_load`, `is_store`, `is_atomic` | Data access classification |
| `is_branch`, `is_branch_cond`, `is_branch_indirect` | Branch classification |
| `is_call`, `is_return` | Call/return classification |
| `branch_taken` | Committed branch direction |
| `branch_target` or `target` | Functional branch target |
| `branch_next_pc` or `next_pc` | Actual committed successor PC |
| `is_microop`, `is_last_microop` | Macro-instruction retirement boundary |
| `is_serialize` | Serializing operation marker |
| `is_syscall` | Explicit committed syscall marker; forces serializing semantics |
| `syscall_number`, `syscall_nr`, or `sysnum` | Numeric syscall identity; `syscall_nr` is the TaoTrace/DR-adapter spelling |
| `syscall_args` or `args` | Zero to six raw scalar ABI arguments |
| `syscall_retval_raw`, `syscall_retval`, or `retval_raw` | Raw return-register bits; negative JSON integers preserve two's-complement bits |
| `syscall_failed` or `failed`; `syscall_errno` or `errno` | Captured failure state and errno; errno implies `failed=true` |
| `syscall_pre_timestamp_us`, `syscall_post_timestamp_us` | Optional boundary timestamps in microseconds |
| `syscall_pre_cpu`, `syscall_post_cpu` | Optional boundary CPU IDs |
| `syscall_maybe_blocking` | Optional best-effort blocking classification, not proof of sleep |
| `thread_id`, `threadid`, or `tid` | Optional native thread identity |
| `cpl` | Optional x86 privilege level in `[0,3]`; values other than 3 encode a kernel record |
| `is_kernel` or `is_user` | Producer-neutral privilege aliases; contradictions with `cpl` are rejected |
| `op_class` | gem5 functional operation class |
| `n_src`, `n_dst` | Number of tracked source/destination registers |
| `producer_dists` | Up to four prior-UOP producer distances |
| `producer_classes` | Register class paired with each producer distance |
| `destination_class_counts` | Int/Float/Vec/CC architectural destination counts; required by the physical-register free-list model |

The same normalized fields are sufficient for the portable syscall portion of
a future drmemtrace adapter. It must group markers per thread and map them as
follows; it must not synthesize an optional value when its marker is absent:

| Portable drmemtrace record | Normalized JSONL / FST v7 field |
|---|---|
| `SYSCALL` | `syscall_nr` and one `op_class=-1` marker |
| matching syscall/function ID plus `FUNC_ARG` | `syscall_args[0..5]` and argument count |
| `FUNC_RETVAL` | `syscall_retval_raw` (exact register bits) |
| `SYSCALL_FAILED` | `syscall_failed=true`, `syscall_errno` |
| nearest same-thread `TIMESTAMP` before/after | pre/post timestamp with separate validity bits |
| nearest same-thread `CPU_ID` before/after | pre/post CPU with separate validity bits |
| `MAYBE_BLOCKING_SYSCALL` | `syscall_maybe_blocking=true` |
| trace shard/header TID | `thread_id` |

Non-returning calls such as `exit` legitimately have no return, post timestamp,
or post CPU. An adapter must leave those bits invalid. This syscall mapping is
portable across the audited x86-64, x86-32, AArch64 and ARM32 DynamoRIO paths;
the per-file ABI determines the numeric syscall table and argument semantics.
It does not make raw drmemtrace a complete FST instruction producer: ISA
decode/UOP lowering, dependencies, branch normalization and (for strict cache
accuracy) physical addresses remain separate adapter responsibilities.

`branch_taken` and an explicit successor PC are both required before a branch
is marked outcome-valid. Missing outcomes are counted in
`branches_without_outcome`; FastSim does not invent direction or target data.

### Aligned Parquet

`tools/convert_aligned_parquet.py` requires:

```text
core_id macro_pc vaddr paddr size
is_load is_store is_atomic
is_branch is_branch_cond is_branch_indirect is_call is_return
is_microop is_last_microop is_serialize
op_class n_src n_dst producer_dists producer_classes
```

`is_syscall` is optional only for backward compatibility. Without it,
syscalls cannot be distinguished from other system/serializing UOPs and only
generic `is_serialize` behavior is available.

For branch PMU replay it additionally requires all of:

```text
branch_taken branch_target branch_next_pc
```

Required functional fields may not be null. `producer_dists` and
`producer_classes` must each be fixed-size lists of four elements. Memory rows
additionally require non-null `vaddr` and `paddr`, matching offsets within a
4-KiB base page, and a size in `[1, 65535]`. The aligned-Parquet converter
rejects cross-page memory records because one 64-byte canonical record carries
one translation. The streaming JSONL converter preserves such a committed UOP
and its physical address but deliberately omits its virtual-page token; strict
timing replay therefore rejects it by default. A branch is outcome-valid only
when all three committed outcome fields are non-null on that row.

Aligned Parquet may also carry any normalized syscall column from the JSONL
table above. `syscall_args`/`args` may contain at most six values; sparse scalar
columns may be null on non-syscall rows. The converter writes the same FST v7
validity bits and raw two's-complement values as the streaming path. If only
`is_syscall` is present, it still emits the required aligned metadata row with
no optional validity bits (and sysnum zero if no number column exists).

Use `--allow-missing-branch-outcomes` only for explicitly cache-only legacy
experiments. `--allow-missing-core-features` permits scalar-only legacy replay
but is not valid input for the interval core.
`--allow-missing-virtual-addresses` is likewise a cache-only compatibility
escape hatch; it cannot drive DTLB timing. The converter reads batches into
NumPy arrays, assigns a collision-free token to every `(virtual page,
physical page)` identity within a core trace, and writes one 64-byte
little-endian canonical record per row.

## Dual-address rule

`trace.strict_physical_address = true` is the default validation mode. Any
memory record without a physical address terminates simulation with an error;
virtual addresses are never silently treated as physical cache addresses.

With `trace.require_virtual_page_token = true`, every memory record must also
carry a virtual-page token. The DTLB and page-walk model consumes only that
opaque token. Cache tags, directory ownership, CHA selection, and DRAM mapping
consume only `address`, which remains physical. FastSim never consumes gem5's
`dtlb_hit`, path class, issue tick, or page-walk timing labels as inputs.

`trace.allow_cross_page_without_virtual_token = true` is an explicit,
default-off compatibility path for the streaming converter. It admits a
tokenless record only when `(physical_address & 4095) + size > 4096` proves
that the access crosses a base-page boundary. The UOP and all physical
cache-line accesses remain modeled, while the DTLB access is counted as
`dtlb_untracked`; FastSim does not fabricate either page identity or a TLB
outcome. Tokenless non-crossing accesses still fail closed. A future trace
schema should encode both translations and retire this compatibility path.

Setting strict mode to `false` is available for exploratory traces, but its
cache/CHA results are virtual-index approximations and should not be compared
to physical gem5 PMUs.

`is_syscall` is required to distinguish a syscall from fences, CPUID, and
other serializing instructions.  For compatibility with this repository's
TaoTrace `records.jsonl`, `instr_type == 7` is also recognized as SYS.  A bare
`is_serialize` record is never relabeled as a syscall. The inline syscall
number and FST v7 sparse metadata table are functional inputs. They do not
provide blocking/wakeup or scheduling truth; timestamp delta must not be used
as active kernel cycles.

## Canonical v7 file and 64-byte record

The `.fst` record layout is:

| Field | Type |
|---|---|
| `pc` | uint64 |
| `address` | uint64 |
| `target` | uint64 |
| `next_pc` | uint64 |
| `producer_dists[4]` | uint32[4] |
| `size` | uint16 |
| `flags` | uint16 |
| `op_class` | int16 |
| `n_src`, `n_dst` | uint8, uint8 |
| packed `producer_classes[4]` | uint8[4] |
| `virtual_page_token` (physical field name `reserved`) | uint32 |

Version 7 preserves the 64-byte v6 record. Each packed register byte keeps the
producer class in its low three bits and the corresponding Int/Float/Vec/CC
destination count in its high five bits. Bit 31 of `reserved` declares this
packing; the remaining 31 bits retain the virtual-page identity. A file-header
feature bit declares that destination classes are present. The rename
free-list model fails closed on older records instead of guessing classes from
`op_class`.

Formal timing datasets require bit 2 in the header and the per-record bit-31
packing marker on every hot record, including zero-destination and syscall
records. The four high-five-bit counts must sum to `n_dst`. Merely setting the
container version to 7 is not evidence that this metadata exists;
`tools/build_fst_v7_formal_dataset.py` rejects such nominal-v7 inputs by
default. Its `--allow-missing-destination-classes` option is only for legacy
format diagnostics and does not produce a formal timing dataset.

Bit 15 (`kVirtualPageToken`) in `flags` declares that the low 31 bits of the
final uint32 field contain a nonzero virtual-page identity. This packing keeps
the 64-byte record and bulk-read throughput unchanged.

All canonical gem5 operation classes are non-negative. FST reserves `-1` for
an explicit syscall marker and, when header feature bit 4 is set, encodes a
kernel record's canonical class `N` as `-(N+2)`. This avoids expanding
the 64-byte hot record or stealing the virtual-page-token bit.  The syscall
feature bit in the file header records whether the stream contains such
markers.  On JSONL input, the original syscall op class is intentionally
replaced by the marker; FastSim routes it to the system FU.

The native mode, collection contract, idle policy, validation gates, and
cross-repository patches are documented in
[`fst-native-kernel-trace.md`](fst-native-kernel-trace.md).

The file begins with a 72-byte header containing magic `FSTRC01`, version 7,
record size, core ID, record count, and feature flags. Binary core IDs must
match their manifest entries. Versions 2 through 6 remain readable for models
that do not need newly added fields. Version-2 40-byte records lack core timing
fields; version-3 64-byte records lack a declared virtual-page token. Older
records cannot drive `core.rename_free_list=true`.

When syscall feature bit 3 is set, the 64-byte record stream is followed by
one 128-byte metadata row per syscall. Header `reserved[0]`, `[1]`, and `[2]`
contain the table offset, count, and row size; `reserved[3]` contains the
per-file Linux syscall ABI. A validity mask distinguishes unavailable fields
from captured zero/false values. The reader checks table size, ordinal order,
record alignment, and duplicated syscall number before exposing a row through
`TraceSource::current_syscall_metadata()`. FST v7 files containing syscall
markers without this table fail closed.

The optional `--syscall-output` JSONL mirror uses schema
`fastsim-functional-syscall-v2`. It exists for audit and dataset indexing;
replay reads the embedded table and does not depend on a second file.

## Integrity behavior

- Core IDs must be dense and start at zero.
- A manifest contains between one and the configured number of entries. IDs
  remain dense and zero-based on the compatibility manifest path, so `T < C`
  maps thread/stream `t` statically to core `t` and leaves cores `[T, C)` idle.
  This path uses address-space ID zero (shared/unspecified). The C++
  `ThreadTraceBinding` API supports explicit address-space identity and sparse
  core placement.
- A binary manifest may add an explicit fourth `source-core-id` field. This
  preserves header validation while allowing a captured trace to be mapped
  to a different simulated core, including controlled replicated-trace
  scaling experiments.
- `fastsim-binary-slice <path> <source-core-id> <skip-instructions>
  <take-instructions>` skips a macro-aligned prefix without changing target
  state, then exposes only the bounded measurement range.
- `fastsim-binary-warmup-slice <path> <source-core-id>
  <warmup-instructions> <take-instructions> [<warmup-records>
  <take-records>]` is the two-phase FS path. Formal FS manifests include the
  two record counts, which preserve the exact producer marker even when it
  falls between UOPs of one macro instruction. The instruction counts are
  independently checked for phase conservation. Every active stream first
  replays its functional prefix and pauses at that exact record boundary.
  Only after all streams reach the common barrier does
  FastSim reset measurement counters/time and resume the ROI. Private/shared
  cache state, directory ownership, branch predictor, DTLB, DRAM/controller
  calendars, dependency history, and response scoreboards remain resident;
  producer lookahead cannot decode ROI records before barrier release.
- Cross-cache line accesses are split into one event per touched line.
- Aligned-Parquet conversion rejects cross-page accesses. Streaming JSONL
  conversion preserves them without a virtual-page token; strict replay then
  requires the explicit default-off cross-page compatibility described above.
- Address-range overflow is rejected.
- Physical accesses beyond configured `dram.size` are rejected.
- Binary version, header size, record size, truncation, and core mismatch are
  rejected.
- A row marked as a UOP retires one UOP. Macro instructions are counted at a
  non-UOP row or `is_last_microop`.
