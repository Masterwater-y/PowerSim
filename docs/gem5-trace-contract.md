# gem5 functional trace contract

FastSim consumes functional execution facts. Timing/cache/coherence oracle
columns such as `commit_tick`, `ready_tick`, `path_class`, and `coh_oracle`
are never used to drive simulation state.

## Canonical frontend policy

The simulator runtime consumes the canonical FST functional IR; it does not
decode architectural instruction bytes or choose a macro-instruction-to-UOP
decomposition. The currently validated producer is the gem5 functional
exporter, so `gem5 functional trace` remains the user-facing name for this
contract. JSONL and aligned Parquet are ingestion forms, while binary v5 is
the compact runtime form.

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

### JSONL

Use manifest format `gem5-jsonl` or convert a single file:

```bash
./build/fastsim convert-gem5 \
  --input core0.jsonl --output core0.fst --core 0
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
| `op_class` | gem5 functional operation class |
| `n_src`, `n_dst` | Number of tracked source/destination registers |
| `producer_dists` | Up to four prior-UOP producer distances |
| `producer_classes` | Register class paired with each producer distance |

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
4-KiB base page, and a size in `[1, 65535]`. Cross-page memory records are
rejected because one 64-byte canonical record carries one translation. A
branch is outcome-valid only when all three committed outcome fields are
non-null on that row.

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

Setting strict mode to `false` is available for exploratory traces, but its
cache/CHA results are virtual-index approximations and should not be compared
to physical gem5 PMUs.

`is_syscall` is required to distinguish a syscall from fences, CPUID, and
other serializing instructions.  For compatibility with this repository's
TaoTrace `records.jsonl`, `instr_type == 7` is also recognized as SYS.  A bare
`is_serialize` record is never relabeled as a syscall.  The compact stream
does not carry the syscall number or arguments; those belong in the planned
thread-event sidecar used for blocking/wakeup and scheduling semantics.

## Canonical v5 record

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
| `producer_classes[4]` | uint8[4] |
| `virtual_page_token` (physical field name `reserved`) | uint32 |

Bit 15 (`kVirtualPageToken`) in `flags` declares that the final uint32 field is
a nonzero virtual-page identity. Keeping it in the former reserved field
preserves the 64-byte record and bulk-read throughput.

All gem5 operation classes are non-negative.  Version 5 reserves the negative
`op_class` value `-1` for an explicit syscall marker.  This avoids expanding
the 64-byte hot record or stealing the virtual-page-token bit.  The syscall
feature bit in the file header records whether the stream contains such
markers.  On JSONL input, the original syscall op class is intentionally
replaced by the marker; FastSim routes it to the system FU.

The file begins with a 72-byte header containing magic `FSTRC01`, version 5,
record size, core ID, record count, and feature flags. Binary core IDs must
match their manifest entries. Versions 2, 3, and 4 remain readable. Version-2
40-byte records lack core timing fields; version-3 64-byte records lack a
declared virtual-page token. Neither can satisfy strict DTLB replay unless it
is regenerated from a source containing `vaddr`.

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
- Cross-cache line accesses are split into one event per touched line.
- Cross-page accesses are rejected at conversion until the canonical format
  can represent multiple page translations for one UOP.
- Address-range overflow is rejected.
- Physical accesses beyond configured `dram.size` are rejected.
- Binary version, header size, record size, truncation, and core mismatch are
  rejected.
- A row marked as a UOP retires one UOP. Macro instructions are counted at a
  non-UOP row or `is_last_microop`.
