# FST v7 format and drmemtrace conversion contract

Status: implemented shared format; normative contract for gem5 TaoTrace and
future DR-to-FST adapters.

This document defines:

1. the byte-level layout and semantics of the current FastSim Trace (FST) v7
   format;
2. the portable syscall information demonstrated by a real offline
   drmemtrace collection;
3. the required mapping from drmemtrace markers into FST v7;
4. the implemented mapping from gem5 TaoTrace into the same rows;
5. the information a producer must leave invalid rather than infer;
6. the remaining work required for a complete raw drmemtrace-to-FST
   instruction adapter.

The format implementation is in `include/fastsim/types.hpp` and
`src/trace.cpp`. The empirical capability evidence is in
[`drmemtrace-portable-metadata-audit-2026-08-14.md`](drmemtrace-portable-metadata-audit-2026-08-14.md).

## 1. Scope and terminology

FST is FastSim's already-lowered functional intermediate representation. It is
not a raw ISA trace and does not contain timing/cache/coherence oracle labels.

One FST v7 file contains:

- a 72-byte file header;
- `record_count` fixed 64-byte functional records;
- when syscalls are present, one fixed 128-byte metadata row per syscall.

The 64-byte record stream remains the hot replay path. Syscall metadata is
sparse and physically appended to the **same `.fst` file**. The optional
`fastsim-functional-syscall-v2` JSONL is an audit mirror, not a replay
dependency.

All current producers and consumers run on little-endian hosts. Integer fields
below use little-endian representation. A big-endian FST reader/writer is not
currently implemented.

The words **must**, **must not**, **required**, and **invalid** are normative.

## 2. Complete FST v7 file layout

```text
offset 0
┌──────────────────────────────────────┐
│ BinaryTraceHeader             72 B  │
├──────────────────────────────────────┤
│ TraceRecord[record_count]      64 B  │
│ ...                                  │
├──────────────────────────────────────┤  metadata_offset
│ BinarySyscallMetadataV1       128 B  │
│ ... one row per syscall ...          │
└──────────────────────────────────────┘
```

Without syscall records:

```text
file_size = 72 + record_count * 64
```

With syscall records:

```text
metadata_offset = 72 + record_count * 64
file_size = metadata_offset + syscall_metadata_count * 128
```

The metadata table is not an arbitrary extension area. If feature bit 3 is
set, its offset, row count, row size, ordering, and final file size must all
match this contract.

### 2.1 Virtual-page token map companion

When virtual-page tokens must be related to syscall virtual ranges, the trace
set includes `coreN.fst.vmap`. Keeping this cold dictionary outside the hot FST
preserves the v7 file byte layout and compatibility. It is part of the
canonical trace set, not an optional audit JSON file.

The 48-byte little-endian v1 header is:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 8 | magic `FSTVMP1\0` |
| 8 | 4 | version, exactly 1 |
| 12 | 4 | header size, exactly 48 |
| 16 | 4 | entry size, exactly 32 |
| 20 | 4 | source core ID |
| 24 | 8 | source FST record count |
| 32 | 8 | mapping entry count |
| 40 | 4 | page-offset bits, currently 12 |
| 44 | 4 | reserved, zero |

Each 32-byte row is `token:uint32`, `flags:uint32`,
`first_record_ordinal:uint64`, `virtual_page:uint64`, and
`physical_page:uint64`. The flags are:

| Bit | Meaning |
|---:|---|
| 0 | `physical_page` is valid |
| 1 | initial PTE state is valid |
| 2 | initial PTE was present; valid only when bit 1 is set |
| 3 | ROI-entry page state is valid |
| 4 | ROI-entry page was present; valid only when bit 3 is set |
| 5 | this stream had a precise page fault in flight when ROI opened |

Bits 6--31 are zero. Tokens are unique, non-zero, and below bit 31. The row at
`first_record_ordinal` must carry that token and, when physical validity is
set, its hot-record physical page must match. The map must cover exactly every
token used by its FST.

Initial PTE state is a functional boundary condition, not a timing label. For
gem5 full-system collection it is read from the restored guest page tables
immediately before the first functional user record. A leaf slot beneath a
present user page-table path is known: bit 1 is set and bit 2 reports its
present bit. A virtual range hidden behind an absent upper-level entry is
unknown, not proven non-present, so both bits remain clear. Huge mappings are
normalized into 4 KiB virtual pages. Producers must not infer these bits from
the later physical address, token order, first-touch behavior, workload name,
or a page-fault timing result.

ROI-entry page state is a second functional boundary condition captured in the
process-wide ROI marker callback before any per-core measurement stream opens.
It uses the same known/unknown rules. Consumers must prefer bits 3--4 for a
measured first touch and must not reuse stale bits 1--2 when bit 3 is clear:
another core or warmup kernel activity may have changed the page state.

Bit 5 records a narrower ordering fact. A precise from-user page fault entered
before the process-wide marker and had not reached its next user commit when
the marker opened. The faulting instruction can consequently be the first
committed measurement record even though its kernel entry belongs to warmup.
This bit must be derived from collection-time exception/commit ordering, never
from “first record,” adjacency, a later page-fault log, or workload identity.
It may coexist with any ROI-entry page state; it affects fault accounting
only when the measured first touch is known non-present.

TaoTrace and aligned-Parquet conversion emit the companion. A drmemtrace
adapter can always emit token, virtual page, and first ordinal; it sets the
physical-valid bit only when its environment has a portable physical mapping.
The current drmemtrace path has no initial process-page snapshot and therefore
leaves bits 1--5 clear. In particular, gem5 guest state must not be replaced
with Linux host `/proc/pagemap` state: those are different kernels and page
tables. A future drmemtrace-side collector may populate the same normalized
bits only if it captures the traced process boundary with a documented
ordering guarantee.
FastSim's `page_fault.syscall_semantic_model` rejects a missing or incomplete
map rather than falling back to token order or workload identity. With
`page_fault.roi_entry_page_state_model=true`, known-present pages suppress the
heuristic selector, a known-nonpresent ROI-entry page selects one
process-wide first touch, and unknown pages fall through to the existing
syscall/probability selector. A bit-5 first touch retains the handler's page
fill/cache-state effect but does not charge a new measured kernel event.
Existing v1 companions remain byte-compatible because the new semantics
consume previously reserved flag bits.

The canonical producer metadata filename is `roi-entry-page-state.json`, and
the canonical JSONL fields are `roi_entry_page_state_valid`,
`roi_entry_page_present`, and `roi_entry_inflight_page_fault`. Readers retain
the retired `measurement-pte-state.json` and `measurement_pte_*` spellings as
input-only aliases so existing FST datasets do not require recollection.

### 2.2 Address-space map companion

An FST stream may carry `coreN.fst.asmap`. This cold companion identifies the
address space of every hot record without enlarging the 64-byte record. It is
a sparse run-length encoding: producers write a row at record zero and another
row only when the address-space identity changes. A producer may retain the
single row for a one-address-space stream; a legacy stream without the
companion remains the unspecified address space zero.

The 48-byte little-endian v1 header is:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 8 | magic `FSTASM1\0` |
| 8 | 4 | version, exactly 1 |
| 12 | 4 | header size, exactly 48 |
| 16 | 4 | entry size, exactly 16 |
| 20 | 4 | source core ID |
| 24 | 8 | source FST record count |
| 32 | 8 | transition entry count |
| 40 | 8 | reserved, zero |

Each 16-byte row is `record_ordinal:uint64` followed by
`address_space_id:uint64`. IDs are non-zero, the first ordinal is zero, later
ordinals are strictly increasing and below the source record count, and two
adjacent rows must have different IDs. The header core/count and exact file
size must agree with the FST.

The numeric namespace is producer-local. gem5 TaoTrace uses the x86 CR3 page
table root with PCID/control bits removed. A drmemtrace adapter may instead use
a stable process/address-space identifier; it need not reproduce the numeric
CR3 value. Correctness requires only that equal IDs mean shared virtual/PTE
state within the trace set and different IDs mean isolated state.

Virtual-page tokens are source-stream-local, but their intern identity is
`(address_space_id, virtual_page, physical_page)`. A token must never cross an
address-space transition. Process page/PTE/residency state is keyed by
`(address_space_id, virtual_page)`. DTLB state is keyed by
`(address_space_id, token)` and an address-space transition flushes modeled
non-global translations and pending walks, matching gem5 x86 CR3-write
semantics. FastSim does not classify global translations yet, so it
conservatively flushes all modeled DTLB entries.

The current `.fst.imap` v1/v2 schemas are keyed only by virtual PC. TaoTrace
therefore suppresses imap output when one stream observes more than one
address space, and FastSim ignores a legacy PC-only imap if such a stream is
encountered. This preserves correctness at the cost of disabling speculative
I-side reconstruction for that stream until an AS-scoped imap schema exists.
In native mixed-privilege mode, gem5 may also expose committed microcode or
pseudo-instructions without a portable x86 macro length, or a macro tail whose
first micro-op was outside capture. The dynamic FST record remains normative;
TaoTrace must conservatively suppress the affected PC from `.imap` instead of
aborting the stream or publishing a partial static dependency mask.

PTE snapshots remain independently valid/unknown per address space. The
current gem5 collector snapshots one selected guest CR3 root; mappings in
other roots retain token/page identity but leave PTE state unknown. Consumers
must not copy the selected root's PTE bits into another address space or fill
them from host `/proc/pagemap`.

### 2.3 Instruction-page map companion

An FST v7 stream may carry `coreN.fst.ifmap`. This cold companion supplies the
functional instruction translation that the hot record cannot hold: an
address-space-scoped virtual instruction page maps to a physical page before a
declared committed record is decoded. It contains no fetch/request tick,
cache or ITLB outcome, retry count, speculative-path identity, or latency.
Existing FST inputs without the companion remain valid, but cannot drive a
strict physical I-side hierarchy.

The 48-byte little-endian v1 header is:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 8 | magic `FSTIFM1\0` |
| 8 | 4 | version, exactly 1 |
| 12 | 4 | header size, exactly 48 |
| 16 | 4 | entry size, exactly 32 |
| 20 | 4 | source core ID |
| 24 | 8 | source FST record count |
| 32 | 8 | mapping entry count |
| 40 | 4 | page-offset bits, exactly 12 |
| 44 | 4 | reserved, zero |

Each 32-byte row is `record_ordinal:uint64`,
`address_space_id:uint64`, `virtual_page:uint64`, and
`physical_page:uint64`. Rows are strictly ordered by
`(record_ordinal, address_space_id, virtual_page)`. A row takes effect before
its anchor record and replaces the active mapping for the same
`(address_space_id, virtual_page)`; an identical redundant replacement is
invalid. The anchor ordinal must be below the source record count and its
address-space ID must match `.fst.asmap`, which is therefore required whenever
`.fst.ifmap` is present.

TaoTrace observes completed live O3 fetch translations but serializes a page
only when a committed FST instruction consumes it. Consequently speculative
fetch footprint and request outcomes do not become runtime inputs. A normal
offline functional tracer may emit the same rows when it can resolve physical
instruction pages; otherwise it must omit `.ifmap`, not synthesize physical
identity from virtual PC. `tools/audit_fst_instruction_page_map.py --require`
validates the binary contract and reports committed-record coverage.

### 2.4 Static instruction map companion

An FST v7 stream may carry `coreN.fst.imap`. This cold companion contains only
ISA-decoded executable-image facts which a normal drmemtrace module decoder
and gem5 TaoTrace can both produce. It must not contain dynamic prediction
outcomes, squashed-instruction identity, timing, cache/TLB results, physical
instruction addresses, or PMU/oracle labels. Existing FST v7 files without an
instruction map remain valid.

The 48-byte little-endian v1 header is:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 8 | magic `FSTIMP1\0` |
| 8 | 4 | version, exactly 1 |
| 12 | 4 | header size, exactly 48 |
| 16 | 4 | entry size, exactly 32 |
| 20 | 4 | source core ID |
| 24 | 8 | source FST record count |
| 32 | 8 | static instruction count |
| 40 | 4 | flags; bit 0 means complete executable scope |
| 44 | 4 | reserved, zero |

Each 32-byte row is:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 8 | virtual PC |
| 8 | 8 | architectural sequential/fallthrough PC |
| 16 | 8 | direct branch target, or zero when invalid |
| 24 | 2 | static instruction flags |
| 26 | 1 | x86 instruction length, 1--15 bytes |
| 27 | 5 | reserved, zero |

Static flag bits are branch, conditional, indirect, call, return,
direct-target-valid, and may-access-data-memory at bits 0--6 respectively.
The memory bit is a decoded ISA property only: it contains no effective or
physical address, hit/miss result, dynamic outcome, or timing. Both an ELF or
module decoder used with drmemtrace and TaoTrace's target decoder can emit it.
Rows are strictly increasing by PC. `fallthrough_pc` must equal `pc + size`;
subtypes imply branch; return implies indirect; a valid direct target requires
a non-indirect branch. A duplicate PC with different decoding is an integrity
error.

The five reserved entry bytes remain zero. In particular, `.fst.imap` v1 does
not encode target-specific micro-op count, FU assignment, dependency readiness,
or whether a speculative instruction would issue. FastSim may emit a
state-free diagnostic learned causally from earlier committed FST UOP rows,
but that runtime observation is not part of the portable map contract and must
not become a timing input when comparing with an ordinary drmemtrace stream.
A future resource-contention model requires a separate producer-neutral
macro-to-UOP lowering specification; repurposing reserved bytes or copying gem5
OpClass values into static module metadata is invalid.

Completeness bit 0 is an explicit producer assertion that every executable
instruction in the declared address-space/module scope was decoded. A map
constructed only from dynamically observed instructions must leave it clear.
FastSim may use partial rows as exact local facts, but must stop at a missing
edge; it must not reinterpret partial coverage as evidence that code does not
exist.

`tools/build_fst_instruction_map.py` converts producer-decoded JSONL to this
format and binds it to the FST core/count. `BinaryTraceSource`, functional
slice/warmup wrappers, and `tools/build_fst_v7_formal_dataset.py` validate or
preserve the companion. The current L1I diagnostic follows only exact
fallthrough and unconditional direct edges. It stops at nested conditional or
indirect control flow because the map deliberately carries no prediction
outcome.

#### v2 architectural register operands

`FSTIMP2\0` version 2 adds producer-neutral architectural register read/write
sets. The header remains 48 bytes. Entry size is 64, header flags bit 1 asserts
that every row has valid operand semantics, and the final header word is the
ISA namespace (`1` = x86-64). Version 2 must contain at least one
operand-valid row. Version 1 maps remain byte-for-byte readable and report ISA
unknown with no operand coverage.

Each v2 row is:

| Offset | Size | Meaning |
|---:|---:|---|
| 0 | 27 | v1 PC/target/flags/size prefix |
| 27 | 1 | semantic flags; bit 0 means register masks are valid |
| 28 | 4 | reserved, zero |
| 32 | 16 | 128-bit little-endian architectural read-register mask |
| 48 | 16 | 128-bit little-endian architectural write-register mask |

When semantic bit 0 is clear, both masks must be zero. Header bit 1 may be set
only when semantic bit 0 is set on every row. Empty read or write sets are
valid for an operand-decoded instruction. The JSONL adapter fields are
`read_register_ids` and `write_register_ids`; both arrays must appear together.
The decoder must include explicit and implicit **rename-dependency** operands.
For example, a call/return includes the stack pointer and a conditional branch
reads flags. Non-renameable state, decoder temporaries, and PC-relative address
constants are omitted. In particular, RIP-relative addressing does not add a
RIP dependency: the instruction PC is already an immutable property of the
static row and is not a rename-queue input.

The canonical x86-64 register IDs are:

| IDs | Registers / normalization |
|---:|---|
| 0--15 | RAX, RCX, RDX, RBX, RSP, RBP, RSI, RDI, R8--R15; byte/word/dword aliases normalize to the full GPR |
| 16 | RIP identity; reserved for namespace stability and omitted from current rename-dependency masks |
| 17 | RFLAGS; EFLAGS/FLAGS aliases normalize here |
| 18--23 | CS, SS, DS, ES, FS, GS identities; omitted from current rename-dependency masks |
| 24--31 | x87/MMX shared storage groups 0--7; x87 and MMX aliases normalize to the same ID |
| 32--63 | vector registers 0--31; XMM/YMM/ZMM aliases normalize to the same ID |
| 64--71 | K0--K7 mask registers |
| 72--127 | reserved; producers must not assign local decoder IDs here |

These are architectural dependency identities, not physical registers or
rename destinations. They do not encode target UOP count, operation class,
latency, ports, readiness, branch outcome, or cache behavior. A DynamoRIO
module decoder and gem5's ISA decoder can therefore emit the same masks
without Intel PT or a platform PMU. GNU `objdump` text alone is not considered
an authoritative implicit-operand source; the existing ELF helper continues
to emit v1 unless a real decoder supplies both operand arrays.

FastSim currently consumes v2 operands only for observability counters on the
static speculative path. They do not allocate rename/ROB/IQ state and do not
add cycles. `tools/audit_fst_static_instruction_maps.py --require-operands`
is the strict dataset gate. Timing use requires a separately validated,
producer-neutral macro-to-UOP lowering contract and an oracle-backed ablation.
The first integrated TaoTrace pilot and its limitations are recorded in
[`fst-imap-v2-operand-pilot-2026-08-17.md`](fst-imap-v2-operand-pilot-2026-08-17.md).

## 3. The 72-byte header

| Offset | Size | Type | FST v7 meaning |
|---:|---:|---|---|
| 0 | 8 | char[8] | Magic `FSTRC01\0` |
| 8 | 4 | uint32 | Version, exactly `7` for this format |
| 12 | 4 | uint32 | Header size, exactly `72` |
| 16 | 4 | uint32 | Hot record size, exactly `64` |
| 20 | 4 | uint32 | Source core/stream ID |
| 24 | 8 | uint64 | Number of 64-byte records |
| 32 | 8 | uint64 | Feature flags |
| 40 | 8 | uint64 | `metadata_offset`, or zero without syscalls |
| 48 | 8 | uint64 | `syscall_metadata_count`, or zero |
| 56 | 8 | uint64 | Metadata row size, `128`, or zero |
| 64 | 8 | uint64 | Linux syscall ABI enum |

### 3.1 Feature flags

| Bit | Name | Meaning |
|---:|---|---|
| 0 | virtual-page tokens | At least one record carries an opaque virtual-page identity |
| 1 | syscall markers | At least one record has `op_class = -1` |
| 2 | destination-class counts | Packed destination register-class counts are present |
| 3 | syscall metadata | A v1 sparse syscall metadata table is appended |
| 4 | privilege records | At least one hot record carries the negative kernel OpClass encoding |

For an FST v7 file containing any syscall, bits 1 and 3 must both be set.
There must be exactly one metadata row for every syscall record, including a
syscall for which no optional argument/return/timestamp fields were captured.
Bit 4 is valid only for gem5-FS or another producer with an authoritative
privilege source. It must match the actual kernel-record population exactly.

### 3.2 Syscall ABI values

| Value | ABI |
|---:|---|
| 0 | unknown |
| 1 | Linux x86-64 |
| 2 | Linux x86-32 |
| 3 | Linux AArch64 |
| 4 | Linux ARM32 |

The ABI is a file property. A converter must obtain it from trace/platform
metadata or an explicit option; it must not infer the ABI from a syscall
number. Syscall numbers and argument meanings are interpreted only together
with this field. Value 0 is permitted for legacy/exploratory input, but a
formal DR conversion must declare a concrete ABI.

## 4. The 64-byte hot record

| Offset | Size | Type | Meaning |
|---:|---:|---|---|
| 0 | 8 | uint64 | Macro/architectural PC |
| 8 | 8 | uint64 | Physical data address, legacy address, or syscall number |
| 16 | 8 | uint64 | Branch target |
| 24 | 8 | uint64 | Actual next PC |
| 32 | 16 | uint32[4] | Producer distances |
| 48 | 2 | uint16 | Memory-operation size |
| 50 | 2 | uint16 | Trace flags |
| 52 | 2 | int16 | Lowered operation class; `-1` is a syscall marker, values `<= -2` encode kernel OpClass `-value-2` |
| 54 | 1 | uint8 | Number of tracked source registers |
| 55 | 1 | uint8 | Number of tracked destination registers |
| 56 | 4 | uint8[4] | Producer classes, optionally packed with destination counts |
| 60 | 4 | uint32 | Virtual-page token plus destination-count marker |

The layout is guarded by `static_assert(sizeof(TraceRecord) == 64)`.

### 4.1 Trace flags

| Bit | Meaning |
|---:|---|
| 0 | retires |
| 1 | load |
| 2 | store |
| 3 | atomic |
| 4 | branch |
| 5 | conditional branch |
| 6 | indirect branch |
| 7 | call |
| 8 | return |
| 9 | branch taken |
| 10 | micro-op |
| 11 | last micro-op of the macro instruction |
| 12 | `address` is physical |
| 13 | serializing operation |
| 14 | committed branch outcome is valid |
| 15 | low 31 bits of the final uint32 contain a virtual-page token |

### 4.2 Packed register and virtual-page fields

When bit 31 of the final uint32 is set, each byte at offsets 56--59 is:

```text
bits 0..2 = producer register class; 7 means unavailable
bits 3..7 = destination count for Int/Float/Vec/CC respectively
```

The low 31 bits of the final uint32 retain the virtual-page token. The token is
meaningful only when trace flag bit 15 is set. Cache/coherence/DRAM use the
physical `address`; DTLB replay uses only the opaque token.

### 4.3 Syscall record semantics

A syscall record has:

- `op_class = -1`;
- trace flag bit 13 (`serialize`) set;
- `address = syscall_number`;
- load/store/atomic and physical-address flags clear;
- one corresponding sparse metadata row.

The syscall gateway instruction and `TRACE_MARKER_TYPE_SYSCALL` describe one
architectural event. A DR adapter must emit exactly one FST syscall record; it
must not emit a normal decoded gateway instruction and then append a second
syscall record. The syscall record retires as one user instruction. In a
portable/user-only stream the following kernel instruction stream remains
absent. In a privilege-tagged gem5-FS stream, real following kernel records
may be present; they use feature bit 4 and the negative OpClass encoding.

Other hot-record fields may retain producer-specific lowering information, but
they must never contain invented kernel UOPs or kernel memory accesses.

## 5. The 128-byte sparse syscall row

The persisted row is `BinarySyscallMetadataV1`:

| Offset | Size | Type | Meaning |
|---:|---:|---|---|
| 0 | 8 | uint64 | Absolute zero-based `record_ordinal` in this FST |
| 8 | 8 | uint64 | Dense zero-based `syscall_ordinal` in this FST |
| 16 | 8 | uint64 | Native thread ID |
| 24 | 8 | uint64 | Duplicated syscall number |
| 32 | 48 | uint64[6] | Raw scalar ABI argument registers |
| 80 | 8 | uint64 | Raw return-register bits |
| 88 | 8 | uint64 | Pre-syscall timestamp in microseconds |
| 96 | 8 | uint64 | Post-syscall timestamp in microseconds |
| 104 | 4 | uint32 | Failure errno |
| 108 | 4 | uint32 | Decoded pre-syscall logical CPU ID |
| 112 | 4 | uint32 | Decoded post-syscall logical CPU ID |
| 116 | 2 | uint16 | Optional-field validity mask |
| 118 | 1 | uint8 | Number of captured arguments, 0--6 |
| 119 | 1 | uint8 | Boolean flags |
| 120 | 8 | uint64 | Reserved, must be zero |

Raw argument and return values preserve register bits. For example, a JSON
return value of `-11` is stored as unsigned `18446744073709551605`. Pointer
arguments contain pointer values only; pointed-to buffers are not copied into
the metadata table.

### 5.1 Validity mask

| Bit | Field proven present |
|---:|---|
| 0 | argument array and `argument_count` |
| 1 | raw return value |
| 2 | failure boolean |
| 3 | errno |
| 4 | pre timestamp |
| 5 | post timestamp |
| 6 | pre CPU |
| 7 | post CPU |
| 8 | maybe-blocking classification |
| 9 | thread ID |

Validity is part of the data model, not merely an encoding optimization:

- valid + value `0` means the producer captured zero;
- invalid means the producer did not provide the field;
- a consumer must not interpret an invalid zero as a captured value;
- errno validity requires failure validity and `failed=true`;
- `argument_count` must not exceed six;
- unknown validity bits are rejected.

This distinction is required for non-returning calls such as `exit` and
`exit_group`: they can have captured arguments but legitimately lack return,
failure, post-timestamp, and post-CPU markers.

A maybe-blocking call can also lack return-side fields when the sampled guest
thread remains asleep at trace termination while another thread continues on
the same logical CPU. This is common for futex wait operations. The producer
must leave the return-side bits invalid; it must not assign the elapsed trace
time as active syscall service time or copy a return from another invocation.
Consequently, formal collection gates require complete entry-side arguments,
pre-timestamp, and pre-CPU coverage, but do not require every syscall to have a
return. `tools/audit_fst_syscall_metadata.py --require-entry-coverage` enforces
that rule and reports missing returns by syscall and warmup/measurement phase.

### 5.2 Boolean flags

| Bit | Meaning |
|---:|---|
| 0 | failed |
| 1 | maybe blocking |

A boolean flag may be consumed only if its corresponding validity bit is set.
For ordinary drmemtrace, a `MAYBE_BLOCKING_SYSCALL` marker proves only
`maybe_blocking=true`; absence of the marker means **unknown**, not false.

## 6. Optional syscall JSONL mirror

`fastsim convert-gem5 --syscall-output FILE` and the functional collector can
emit a human-readable mirror. Each row uses schema
`fastsim-functional-syscall-v2` and contains:

```json
{
  "schema": "fastsim-functional-syscall-v2",
  "event": "syscall",
  "abi": "linux-x86_64",
  "core_id": 0,
  "record_ordinal": 123,
  "syscall_ordinal": 4,
  "pc": 4198400,
  "syscall_nr": 202,
  "capture_flags": 1023,
  "thread_id": 99,
  "arg_count": 6,
  "args": [8192, 128, 0, 0, 0, 1],
  "retval_raw": 18446744073709551605,
  "failed": true,
  "errno": 11,
  "pre_timestamp_us": 1000,
  "post_timestamp_us": 1578,
  "pre_cpu": 35,
  "post_cpu": 37,
  "maybe_blocking": true,
  "timestamp_unit": "microseconds"
}
```

Optional keys are omitted when invalid. `args` contains only
`argument_count` entries and is not padded in JSON. The embedded FST table is
authoritative for replay; the JSONL exists for inspection, indexing, and hash
audits.

## 7. Portable drmemtrace capability used by FST

The real collection audit used normal offline user-mode drmemtrace, without
Intel PT, kernel PT, privileged physical-page mapping, perf, or eBPF. On a
workload with four pthreads it observed 44 syscalls and demonstrated the
following portable inputs:

- per-thread trace shards and TID;
- user-mode instruction fetch/encoding and virtual memory references;
- `TRACE_MARKER_TYPE_SYSCALL` with every syscall number;
- selected syscall parameters through `TRACE_MARKER_TYPE_FUNC_ID` followed by
  up to six `TRACE_MARKER_TYPE_FUNC_ARG` markers;
- raw return-register value through `TRACE_MARKER_TYPE_FUNC_RETVAL`;
- failure errno through `TRACE_MARKER_TYPE_SYSCALL_FAILED`;
- boundary `TRACE_MARKER_TYPE_TIMESTAMP` and
  `TRACE_MARKER_TYPE_CPU_ID` markers;
- the best-effort `TRACE_MARKER_TYPE_MAYBE_BLOCKING_SYSCALL` marker.

The explicit `-record_syscall` option controls which syscall arguments and
returns are recorded. Default Linux collection may special-case calls such as
`futex`, but an adapter must use field validity rather than assume that any
specific syscall has parameters.

The portable scope covers the audited DynamoRIO x86-64, x86-32, AArch64, and
ARM32 trace paths. ISA-specific syscall numbers and register meanings remain
separated by the header ABI.

The audited build was DynamoRIO 11.91.20668. DynamoRIO 11 and later define a
syscall `FUNC_RETVAL` as the actual return-register value and emit a separate
`SYSCALL_FAILED` marker. Older trace-format versions may use different return
semantics, including a success indicator. An adapter must check the trace
version/file type and apply the matching official semantics; it must not label
an older success bit as `retval_raw`.

## 8. Required drmemtrace-to-FST syscall mapping

### 8.1 Conversion pipeline

```text
offline drmemtrace raw shards
          │
          ▼ official drraw2trace
decoded per-thread memref/marker stream
          │
          ├── user instruction adapter ──► lowered 64-byte FST records
          │
          └── syscall state machine ─────► sparse syscall rows
                                           │
                                           ▼
                          one validated FST v7 file per stream
```

Syscall association must be performed independently per TID. Marker state from
different thread shards must never be joined, even when their timestamps or CPU
IDs are adjacent after a merged replay.

### 8.2 Marker mapping

| drmemtrace source | Normalized value | FST destination | Rule |
|---|---|---|---|
| trace architecture/file type or explicit converter option | ABI | header offset 64 | Required; never infer from sysnum |
| shard/header TID | `thread_id` | row offset 16 + validity bit 9 | Preserve native unsigned identity |
| syscall gateway instruction immediately preceding the marker | syscall PC | hot record offset 0 | Buffer/relabel the gateway; do not duplicate it |
| `TRACE_MARKER_TYPE_SYSCALL` | `syscall_nr` | hot record offset 8 and row offset 24 | Required and duplicated for integrity |
| matching `TRACE_MARKER_TYPE_FUNC_ID` | syscall parameter/return association | converter state only | ID must encode the same syscall under the DR syscall-ID convention |
| sequential `TRACE_MARKER_TYPE_FUNC_ARG` | `args[0..5]` | row offsets 32--79 + validity bit 0 | Preserve order and raw pointer-sized bits |
| `TRACE_MARKER_TYPE_FUNC_RETVAL` | `retval_raw` | row offset 80 + validity bit 1 | On compatible DR versions, preserve raw return-register bits |
| `TRACE_MARKER_TYPE_SYSCALL_FAILED` | `failed=true`, `errno` | row offset 104, flag bit 0, validity bits 2 and 3 | Associate with the same pending syscall |
| recorded return followed by a completed invocation without `SYSCALL_FAILED` | `failed=false` | flag bit 0 clear + validity bit 2 | Valid success, not a missing failure field |
| pre-boundary `TRACE_MARKER_TYPE_TIMESTAMP` | `pre_timestamp_us` | row offset 88 + validity bit 4 | Preserve marker value; 32-bit traces may be truncated by DR |
| post-boundary `TRACE_MARKER_TYPE_TIMESTAMP` | `post_timestamp_us` | row offset 96 + validity bit 5 | Absent for non-returning syscalls |
| pre/post `TRACE_MARKER_TYPE_CPU_ID` | logical CPU ID | row offsets 108/112 + validity bits 6/7 | Decode the OS CPU component; unknown marker value remains invalid |
| `TRACE_MARKER_TYPE_MAYBE_BLOCKING_SYSCALL` | `maybe_blocking=true` | flag bit 1 + validity bit 8 | Hint only; absence remains invalid |

On Linux, a raw DR CPU marker may also encode socket/node information above
the logical CPU bits. FST v7 currently stores the decoded logical CPU ID used
for boundary migration detection, not the complete raw marker. This is an
explicit normalization/loss boundary: node/socket bits are not present in the
v7 syscall row. A marker value of `INVALID_CPU_MARKER_VALUE` must leave the CPU
field invalid.

DR timestamp markers are microseconds since 1601-01-01 UTC; 32-bit traces
truncate the marker to 32 bits. FST stores the observed unsigned marker value
without translating epochs or reconstructing lost high bits. A consumer may
form a duration only when both boundaries are valid and must handle the
source-width wrap rule.

DR also exposes process identity. FST v7 stores TID in the syscall row but does
not store PID/address-space identity in this binary header. A complete adapter
must preserve the PID-to-address-space mapping in the trace binding/manifest;
the current simple manifest otherwise uses address-space ID zero. It must not
merge equal virtual addresses from different processes merely because PID is
absent from the FST row.

### 8.3 Per-thread syscall state machine

A conforming adapter must implement the equivalent of these states:

1. **Outside syscall:** retain same-thread timestamp/CPU candidates, but attach
   them as the pre boundary only when the documented marker ordering/adjacency
   proves they belong to this invocation. A stale periodic marker is not a
   syscall boundary and must leave the field invalid.
2. **Gateway pending:** buffer the user syscall-gateway instruction until its
   following `TRACE_MARKER_TYPE_SYSCALL` determines the syscall number.
3. **Syscall open:** emit/prepare one FST syscall record and one sparse row;
   assign absolute `record_ordinal` and dense `syscall_ordinal`.
4. **Arguments:** accept only function-argument markers whose syscall function
   ID matches the open syscall. Preserve marker order and reject more than six.
5. **Return:** accept the matching raw return marker; if a following
   `SYSCALL_FAILED` exists, set both failure and errno validity. Once a recorded
   invocation closes without that failure marker, record captured
   `failed=false` rather than leaving failure unknown.
6. **Post boundary:** attach only the post-return timestamp/CPU markers emitted
   for this same thread and close the row.
7. **No return:** when the thread ends or executes a non-returning syscall,
   close the row with all unavailable return/post fields invalid.

The adapter must fail closed on ambiguous function IDs, a return for the wrong
syscall, more than six arguments, duplicate ordinals, or a syscall number that
does not match the hot record. It must not repair ambiguity with workload-name
rules.

### 8.4 Timing interpretation

The difference between pre/post timestamps is **instrumented syscall wall
time**. It can contain:

- active kernel execution;
- blocking/descheduling;
- run-queue delay and preemption;
- DynamoRIO instrumentation overhead.

It must not be copied into an active CPL0 cycle profile or added directly to
CPI. Pre/post CPU inequality is evidence of a boundary migration, but the
current static FastSim scheduler does not reconstruct the intervening
schedule. The CPU fields remain semantic hints until a separately validated
scheduler model consumes them.

### 8.5 Implemented gem5 TaoTrace mapping

The full-system x86 TaoTrace producer writes the same FST v7 header, hot
records, sparse rows, validity bits, and Linux x86-64 ABI value as the DR
mapping above. It does not translate through a producer-specific side format.

At a CPL3 syscall gateway it captures:

- `RAX` as the syscall number;
- `RDI, RSI, RDX, R10, R8, R9` as the six raw Linux x86-64 argument-register
  candidates;
- user `RSP`, page-aligned `CR3`, and `PC + 2` as a return-association key;
- the simulation timestamp in microseconds and the source FST core ID.

The O3 Execute callback keeps only a provisional entry snapshot. TaoTrace
refreshes the syscall number and all six ABI argument registers at the
`PreCommit` probe, where every older instruction has committed but this
syscall's rename-map updates have not occurred. Reading ThreadContext only at
Execute is invalid: it can mix old and new argument values when their producer
instructions are still in flight.

Arguments become valid only when the syscall number occurs in the configured
`syscall_arg_counts` map. This is the gem5 equivalent of selecting calls with
drmemtrace `-record_syscall`: unlisted calls remain number-only instead of
exposing stale ABI registers. The TCSim wrapper records the exact map in
`tao_trace/syscall_capture.json`.

The O3 commit stage exposes an observational `PreCommit` probe before
`commitHead` updates the committed rename map. On the first CPL3 instruction
whose `(CR3, RSP, PC)` uniquely matches a pending entry, TaoTrace captures
`RAX` before that user instruction can overwrite it, plus the post timestamp
and current FST core. Linux returns in `[-4095, -1]` set `failed=true` and a
positive errno. The pending table is process-global across TaoTrace instances,
so a syscall may enter and return on different simulated cores.

The association fails closed. Ambiguous matches, non-returning calls, a return
not observed before trace termination, and signal paths that never regain the
key leave return/post fields invalid. TaoTrace does not claim a guest OS TID:
gem5's context ID is a simulated hardware context, so thread ID and validity
bit 9 stay zero. Page faults, IRQs, scheduler state, idle periods, and CPL0 PMU
truth remain in the separate oracle and are never inserted into syscall
metadata. Native mode may independently emit their active CPL0 handler
instructions as privilege-tagged hot records.

gem5 timestamps use microseconds since simulation start, whereas DR timestamps
use the documented DR epoch. `syscall_capture.json` identifies the producer
and origin. Consumers may compare a valid pre/post delta within one row, but
must not compare absolute timestamp values across producers.

#### 8.5.1 C4 implementation pilot

The 2026-08-14 C4 zstd pilot under
`tmp/taotrace-fst-v7-pilot-20260814` produced four valid FST files with
27,479,510 hot records and nine syscalls. All nine rows carried arguments,
pre timestamps, and pre CPUs. Eight carried return values, failure state,
post timestamps, and post CPUs. The ninth syscall reached the trace stop before
a matching CPL3 return and correctly retained invalid post fields. Thread-ID
coverage was zero by design. Both FastSim `user` and `user-plus-kernel` modes
read and simulated the files successfully.

## 9. Information that must not be synthesized from functional FST

Normal portable user-mode drmemtrace, and the user-only portion of gem5
TaoTrace FST, do not supply:

- page-fault occurrence, address, cause, or service cycles;
- IRQ/softirq vector, handler flow, or duration;
- kernel instructions or kernel memory references;
- active CPL0 cycles or kernel PMU counts;
- reliable active/blocked/preempted decomposition of syscall wall time;
- ordinary scheduler switch/wakeup edges;
- pointed-to syscall buffer contents;
- portable physical data addresses.
- hardware wrong-path instruction identities, nested prediction outcomes, or
  speculative fetch timing.

These values must remain outside FST syscall metadata. Page-fault, IRQ,
scheduler, and active-kernel-cycle truth require a separate gem5-FS or suitably
validated host oracle/model. Intel PT-only or privileged physical-mapping
features are deliberately outside this portable contract.

## 10. Why syscall conversion is not the complete DR adapter

FST hot records require already-lowered operation classes, UOP boundaries,
register dependency distances/classes, committed branch normalization, and—
for strict cache/coherence/CHA/DRAM validation—physical addresses.

Raw drmemtrace provides instruction encodings and virtual memory references,
but it does not directly provide gem5-compatible UOP/OpClass lowering,
FastSim producer distances, or portable physical addresses. Therefore:

- the syscall metadata defined here preserves the portable DR syscall fields
  selected by this contract, with the documented logical-CPU normalization and
  external PID/address-space mapping;
- a full raw DR-to-FST converter still needs an offline ISA decoder/lowering
  stage and dependency construction;
- a virtual-only DR conversion must be explicitly marked non-strict and cannot
  claim physical cache/coherence/CHA/DRAM accuracy.

Producing the correct byte count while guessing these functional fields is not
a conforming FST conversion.

For `.fst.imap`, the offline adapter may decode mapped executable modules and
emit instruction length, fallthrough, control-flow type and direct target.
That is a separate static decoding pass; ordinary dynamic drmemtrace records
still do not reveal which of those instructions a hardware predictor fetched
and later squashed.

## 11. Writer and reader integrity rules

An FST v7 writer must:

- keep the 64-byte hot record unchanged;
- emit exactly one sparse row for each syscall, including number-only rows;
- assign monotonically increasing record ordinals and dense syscall ordinals;
- duplicate the syscall number exactly;
- zero reserved fields and unsupported flag bits;
- preserve missing-vs-zero through validity bits;
- append the metadata table only after all hot records;
- finalize header offset/count/row size and feature bits.

An FST v7 reader must reject:

- wrong magic/version/header/record size;
- truncated records or metadata;
- unexpected trailing bytes;
- metadata feature without syscall-marker feature;
- a zero metadata count when feature bit 3 is set;
- table offset or row size different from the contract;
- an unrecognized ABI enum, validity bits, boolean flags, or nonzero reserved
  data;
- unordered/out-of-range ordinals;
- metadata pointing to a non-syscall record;
- a syscall record without its row;
- disagreement between inline and duplicated syscall number;
- a kernel OpClass encoding in a stream without feature bit 4;
- feature bit 4 without any kernel record;
- errno without captured `failed=true`.

FST versions 2--6 remain readable for legacy experiments. They do not expose
the v7 sparse metadata table. A v7 syscall is never silently downgraded to a
legacy number-only record.

## 12. Current producer paths

The following implemented paths emit FST v7:

- gem5 full-system TaoTrace direct FST output, including configured syscall
  arguments and uniquely associated return metadata;
- C++ streaming JSONL conversion via `fastsim convert-gem5`;
- legacy binary migration via `fastsim upgrade-fst --input LEGACY.fst
  --output TRACE.v7.fst --syscall-abi linux-x86-64`;
- `tools/convert_aligned_parquet.py`;
- `tools/collect_functional_traces.py`, which also validates and hashes the
  human-readable syscall mirrors.

`tools/build_fst_instruction_map.py` is the implemented producer-neutral
adapter for optional decoded static facts, including v2 register operands.
Direct TaoTrace/drmemtrace module decoding into that input schema is the
remaining producer integration step; the current C4/C8 v4 formal dataset
therefore has no `.imap` companions and remains a valid v7/v1-compatible
baseline rather than being silently treated as operand-complete.

`upgrade-fst` is a compatibility migration, not metadata enrichment. For a
legacy syscall marker it preserves the inline syscall number and writes one
minimal sparse row whose optional-field validity mask is zero. Dataset-scale
migration uses `tools/build_fst_v7_formal_dataset.py`. That builder upgrades
legacy inputs but copies native FST v7 inputs byte-for-byte and derives JSONL
mirrors from their validity masks, so producer-captured metadata is never
downgraded. `tools/audit_fst_syscall_metadata.py` performs strict table and
coverage audits; dual-scope inference and denominator checking use
`tools/run_fst_v7_formal_inference.py`.

Normalized JSONL/Parquet ingestion recognizes:

```text
syscall_number | syscall_nr | sysnum
syscall_args | args
syscall_retval_raw | syscall_retval | retval_raw
syscall_failed | failed
syscall_errno | errno
syscall_pre_timestamp_us | pre_timestamp_us
syscall_post_timestamp_us | post_timestamp_us
syscall_pre_cpu | pre_cpu
syscall_post_cpu | post_cpu
syscall_maybe_blocking | maybe_blocking
thread_id | threadid | tid
```

The functional collector publishes schema
`fastsim-functional-trace-set-v3`, records the FST versions/feature flags,
embedded syscall count, sidecar schema, and sidecar SHA-256, and rejects a
partial or mismatched trace set.

## 13. Adapter acceptance checklist

Before a DR-to-FST adapter is considered usable, verify at least:

1. syscall count equals the official drmemtrace `syscall_mix` count per TID;
2. every syscall number matches both the inline FST record and sparse row;
3. argument count/order and raw return bits match `view` output;
4. failed calls retain errno and successful zero returns remain distinct from
   missing returns;
5. non-returning calls have invalid return/post fields;
6. timestamp and CPU markers never cross thread boundaries;
7. `MAYBE_BLOCKING_SYSCALL` absence remains unknown;
8. FST file-size and ordinal invariants pass the C++ reader;
9. instruction/UOP/branch/dependency conservation is validated separately
   from syscall metadata;
10. virtual-only address mode is labeled non-strict and excluded from strict
    physical PMU accuracy claims.
11. successful Linux x86-64 `mmap` rows have non-zero length and a MAP_TYPE of
    MAP_SHARED, MAP_PRIVATE, or MAP_SHARED_VALIDATE; structural validity bits
    alone do not prove that O3 captured the correct architectural arguments.
12. every tokenized stream used for syscall-semantic page-fault modeling has a
    valid `.fst.vmap` whose source core/count, token set, first ordinals, and
    available physical-page identities agree with the FST.
13. an `.fst.imap`, when present, matches the FST core/count, has ordered unique
    PCs and valid x86 geometry, and sets completeness only after module-scope
    coverage is independently audited.
14. before enabling the PTE selector, every stream is audited for valid vmap
    bits 1--5, each selected snapshot ASID has consistently ordered initial
    and measurement guest-CR3 snapshots, and both phases'
    known/present/nonpresent/unknown coverage is reported per ASID. Bit-5
    counts must also be reported. A missing producer snapshot remains unknown
    and must not be filled from another ASID or host PTEs.
15. an `.fst.asmap`, when present, matches the FST core/count and exact size,
    begins at ordinal zero, has strictly increasing transition ordinals and
    non-zero changing IDs, and no vmap token is used in two address spaces.
16. an `.fst.ifmap`, when present, requires `.fst.asmap`, matches the FST
    core/count and exact size, has strictly ordered non-redundant mapping rows,
    and every row's ASID agrees with its anchor record. Physical I-side timing
    claims additionally report committed-record mapping coverage.
