# SPR retired store values: frozen design before implementation

2026-11-04. Design and wire frozen before a later dedicated EDITSTART. The architectural bundle worker currently owns overlapping native/Trace files; no store-value source edits are released. Parent runtime6bddd71 / docscccec02; native LSQ base7e121b8a2e unchanged. This capability remains separate from controls, RAM state and model timing.

## 1. Decision and source boundary

Select default-OFF **`.mval.gz` v1**, reserve **bit 19** (`0x80000`), with one variable-length value/status row for every successfully retained hot memory-store owner. Preserve `.sreq` and `.uops` versions, bytes and semantics. A split store has one memory-owner row, referring to its actual retained SD ordinal/hash and existing static group; a nonmemory SD does not gain a hot memory flag, request or `.sreq` row.

This is an independently usable functional capability. It does not require architectural-boundary feature 18, core-v2 or MMU-controls-v2 merely because those are being developed concurrently. Required existing facts are memory-request roles (6), functional retirement (7), store requests (13) and static UOPs (14); including its own bit, the required mask is `0x860c0`. Dynamic translations (9) are additionally required to admit a noncontiguous paged mapping, according to the existing `.sreq`/`.xlat` contract. Complete raw-kernel/raw-syscall capture is required when claiming full FS closure, not invented as a dependency on boundary transport. Old sources without bit 19 remain old sources.

Repeated collision check: declarations in `include/fastsim/fst*.hpp`, the native feature declarations, maintained `src/trace.cpp` feature allowlist, preparation/functional readers and current SPR specs show 13=sreq, 14=uops, 15=ixlat, 16=core, 17=MMU controls, 18=architectural bundle. No feature-19/`0x80000`/524288 reservation was found in that bounded inventory. Root reserves19 in this contract; recheck the reconciled checkpoint immediately before EDITSTART. No live architectural source was edited.

Exact bytes are sampled at native **`LSQUnit::write` / `writeStoreData`**, before native merging, into small lifetime-owned observer state. They are not sampled from `DynInst::memData` at retirement or from RAM later. Native attempts can update an **unsealed** payload even after SA has retired. Only successful retirement of the actual final data owner seals its value; after the final retained data-owner seal, any in-window replacement is a sticky companion-integrity failure. It must not throw or stall native execution, and must not rewrite a published row. Payload production never waits for native service, permission, response, drain or another retirement.

## 2. Source-real route proof and admission distinctions

| Source | What is proved; release implication |
|---|---|
| `gem5-spr/src/cpu/o3/lsq_unit.cc:465-524` | `insertStore` creates the SQ entry; `insertStoreData` joins the actual unpaired SA and SD, checks macro object/PC and shares `sqIdx/sqIt`. `writeStoreData` checks exact entry/SD identity and size, copies bytes, then marks data ready. Observe this actual pairing/copy; PC matching alone is insufficient. |
| `lsq_unit.hh:172-212` | SQ has data storage and distinct address/data-ready members; `clear` drops instructions/request and resets readiness. The data array is not cleared on every reuse. An observer must carry its own allocation generation/valid-byte state; stale bytes in the backing array cannot be evidence. |
| `lsq_unit.cc:2420-2456` | `write` binds the request and size. SA marks address-ready and returns before copying bytes. Unified stores copy data except block-zero, maintenance and request-atomic cases. Copy from the original valid data pointer with matching semantics at this path; never read the SA null pointer as zero. |
| `dyn_inst.cc:477-515`; `lsq.cc:804-888` | SA calls `writeMem(nullptr,...)`; SD directly calls `writeStoreData`. Unified/AMO calls go through `pushRequest`; AMO supplies null data plus a functor. Translation state can reuse a saved request or allocate a new one. `write` is reached only after successful required memory access. Missing call is not proof of zero/no effect. |
| `inst_queue.cc:1293-1302`; `lsq.cc:827-846` | Generic rescheduling resets translation flags; a subsequent attempt can allocate another request. The inspected LSQ-unit callers of this rescheduler (`2070,2100,2227,2355`) are loads, so this is not evidence that all stores take that path. Actual write/writeStoreData entries nevertheless do not provide a universal one-attempt guarantee. Refresh any same-generation unsealed write snapshot and provenance; final retained ownership selects it, not first execution. |
| `lsq_unit.cc:941-947,1899-1906` | Native blocked-store retry resends the existing packet through `writebackBlockedStore`; it does not supply a new store value. Do not recapture at retry or count a second row. This route is distinct from execution/data-attempt replacement. |
| `lsq_unit.hh:128-157`; `lsq.hh:342-349,571-583,586-620`; `lsq.cc:1176-1183` | SQ clear/destruction calls `freeLSQEntry`; request release deletes immediately only without outstanding work, otherwise packets/translation/writeback callbacks later delete it. Request destruction clears `inst->savedRequest`. The observer must detach generation identity at SQ clear and hold only already-bound scalar/byte metadata; delayed request callbacks are not a valid way to update an unrelated reused slot or prolong a value seal. |
| `lsq_unit.cc:906-937` | A split entry becomes publishable based on SD sequence and requires address/data readiness. This does not postpone SA hot emission. It proves why SA binding is distinct from final value seal. |
| `lsq_unit.cc:1541-1579` | Squashing either uncommitted address or data member removes the SQ entry and marks both members squashed, unless can-WB already prevents removal. Invalidate its observer generation; do not carry a previous pair's bytes into redispatch. A previously bound partial hot pair remains explicit incomplete/abandoned according to existing functional facts. |
| `arch/x86/insts/microop.hh:119-123`; `regop.isa:110-132,945-970`; `iew.cc:1073-1075` | StoreData is independently classified/dispatched and calls `writeStoreData` with actual integer/FP/XMM bytes. It is a register/data operation, not a second address request. `tao_trace.cc:5444-5445,5677-5685,6274-6281` derives hot memory flags from actual static memory classification and uses zero memory size for nonmemory members. |
| `tao_trace.cc:6415-6443` | Existing `.uops` group/role comes from actual SQ SA/SD pairing, with private dynamic join keys. Reuse that exact allocated group and final static descriptor. Do not allocate an independent group sequence or require pairs to be adjacent. |
| `lsq_unit.cc:950-1033,1217-1230` | Native committed stores can merge/offload or be overwrite-elided. Preserve every original retained owner/value regardless of those decisions. Those native optimizations are neither payload-selection predicates nor model input. |
| `lsq_unit.cc:1281-1319` | `memData` is allocated at writeback, possibly zero-filled, and SC failure can suppress the request afterwards. This is too late and has the wrong lifetime for universal retirement sampling. |

Special-domain decisions are explicit:

- **Ordinary stores and source-proved x86 locked writes:** admit actual resulting bytes with complete size/mask/request/pair proof. `ldstop.isa:747-754` gives `Stul` a real data expression plus `LOCKED_RMW`; `request.hh:1078-1082` defines request `isAtomic()` through separate ATOMIC flags. Do not reject every locked store as a cache-side functor. `semaphores.py:44-83` stores unconditionally for both CMPXCHG compare outcomes, using the old value on failure. These bytes suffice for its memory mutation; no blanket extra compare-success bit. Atomic ordering and consistency with model-generated reads remain separate gates.
- **Split address/data versus split request:** SA/SD pairing is not the same as `.sreq` `kStoreSplit` (multiple native request fragments). `StSplit/StSplitul`, `ldstop.isa:815-852`, construct a multiword unified store value; the mnemonic is not proof of an SA/SD pair. Use actual role and request facts.
- **NO_ACCESS:** `lsq.cc:1158-1171` installs the explicit local accessor, whose body returns a cycle without a RAM mutation. `Cda` is generated with NO_ACCESS (`ldstop.isa:802-803`). A bound source-proved NO_ACCESS path can emit typed nonmutation. Generic local access is not synonymous with this: `writebackStores:1330ff` invokes arbitrary local accessors, which may have effects. Other local/MMIO operations remain unsupported for RAM-mutation closure.
- **Predicated off / empty mask:** `executeStore:824-846` has a real false-predicate exit; `SingleDataRequest::initiateTranslation:978-995` can produce no requests and false memory-access predicate for an empty mask. Capture the final pure predicate and exact functional mask from the actual originating path if admitting nonmutation. No request, hot size zero, or default-zero SQ storage alone proves nothing. Initial v1 may support exact false predicate/empty mask only after actual integration fixtures establish no other mutation path; otherwise record Unsupported.
- **Special zero and maintenance:** `request.hh:269-270` defines `STORE_NO_DATA = CACHE_BLOCK_ZERO | CLEAN | INVALIDATE`. `write:2442-2452` sets `isAllZeros` for this composite, and writeback constructs zeros; this does not turn maintenance into software zero writes. x86 `Clflushopt/Clwb`, `ldstop.isa:804-808`, are actual CLEAN/INVALIDATE paths. V1 records separate `BlockZero` and `Maintenance` operation classes but initially admits neither as ordinary bytes nor generic nonmutation. No typed zero shortcut is released until the exact architectural range/mask/command semantics are separately qualified. An ordinary supplied value of all zero bytes remains ordinary Bytes.
- **True SC/LLSC, AtomicOpFunctor, swap, HTM, other specials:** explicit Unsupported in initial v1. Do not import native handleLockedWrite success, cache AMO return, send/merge/grant or terminal reason as timing input. Any future conditional-existence/result contract must be pure architectural semantics. Native request flags identify these domains but do not supply their complete functional mutation.
- **Masks/fragments:** `lsq.cc:1141-1156` trims inactive trailing bytes while preserving original request size/mask elsewhere; current `.sreq` completeness requires exact matching request/hot sizes and checks split packet masks (`tao_trace.cc:6711-6793`). A mask may be representable in `.mval` while its existing address descriptor is incomplete. That row cannot be admitted by guessing physical continuation or upgrading `.sreq`. Only the intersection of complete existing request/fragment facts and captured value proof is admitted; notably masked split and trimmed-size cases need explicit qualification and may remain Unsupported.

## 3. Bounded producer lifecycle

Add a default-disabled observer payload/token to actual SQ allocation/paired DynInst lifetimes, with no owning reference that extends a DynInst/request's native lifetime. It contains generation, source owner/data-owner identity, last accepted unsealed byte snapshot (up to 64), functional size/mask/predicate classification and seal flags. Private seq/pointer/attempt identities may establish joins internally; none are serialized. No speculative/wrong-path instruction stream is emitted.

Lifecycle is **Allocated → AttemptSnapshot → OwnerBound → ValueSealed → RowReady → Written**, with OwnerBound and AttemptSnapshot allowed in either order. Pair/group metadata and final data binding can seal independently:

1. New SQ allocation gets a fresh observer generation. Actual `writeStoreData` or qualified unified `write` copies data observationally before it can be merged. Same generation may replace the latest **unsealed** attempt, including after retained SA binding. Capture typed nonmutation/unsupported provenance instead of reading absent pointers. New translation attempts must refresh request identity; do not splice a new address/mask with an old data attempt without their actual pairing proof.
2. Bind only after successful hot write to its ordinal and existing 32-bit hot hash. For SA, freeze owner identity/address descriptor, **not value**. The state must continue receiving actual SD attempts until that SD successfully retires. For unified stores, that same retained member is the data owner. Existing `.uops` role/group observation supplies the group; incomplete pairing does not become known through an observer-generated group.
3. At actual successful data-owner retirement, match generation and latest snapshot, then seal value/data ordinal/hash. Use the existing after-record/post-update completion hook for metadata closure if needed, registered independently of feature 18/MMU controls. It must not reread `memData`, wait for writeback, reexecute SD or sample later memory. No value replacement is legal after this seal, even if bytes happen to match; an unexpected callback is an integrity failure, not silent deduplication. Pure repeated pre-seal callbacks can refresh the candidate without changing native state.
4. Maintain owner-ordinal order in a FIFO. A later ready row waits behind an earlier bound SA until metadata is sealed. Store only bytes/scalars, never whole execution history or DynInst references. A valid split reference must be greater than its owner ordinal for this source policy; unified reference equals owner. Reference hashes/groups/roles must agree with the full stream. Missing/nonretained data owner is not fabricated from next ordinal.
5. Original global cutoff stops new hot-member admission. Already bound metadata may finish through existing callbacks for members already retained; a later SD that was **not** retained cannot complete the row. Preserve partial pairs and explicit pending/incomplete states, with no additional cycles or awaiting drain. The writer remains alive through existing bound seals; finalization then emits incomplete rows in owner order. An in-window update after final value seal taints the final capability without rewriting its row. At source close/footer, detach endpoints: later out-of-window callbacks do no reads, allocations, counters, writer work or throws, following reviewed MMU fix1 behavior.

Limits: `max_value_bytes=64`; at most one live payload per actual SQ allocation (header records actual configured SQ capacity, max 4096 for v1); at most **4096 bound pending owner rows per core**. Count pending payload+pair metadata together. The observer's byte storage is at most `64*(live_SQ+4096)` plus fixed-size scalar/token records; document and measure actual allocation overhead separately. These are observer limits, not a shortened workload/ROI. On limit violation, set permanent incomplete status, flush retained pending rows as Incomplete/ObserverCap in source order and continue emitting bounded status-only rows for every subsequent retained owner. Do not drop rows, block native progress or grow a fallback log. Counter overflow rejects the artifact. Retained orphan SDs are counted and diagnosed during full-source validation; they do not generate fake memory rows.

## 4. Explicit little-endian binary contract

All integer encodings are unsigned little-endian, with checked arithmetic and explicit byte encoders/decoders; no native-struct `fwrite` ABI. Gzip contains exactly one header, rows, footer and decompressed EOF. Reject duplicate footer, concatenated payload/trailing bytes, unknown enums/bits, reserved nonzero, overflow, short reads and oversized advertised frames before allocation. Compressed-stream close/trailer errors invalidate the artifact. Filename is the FST filename plus `.mval.gz`.

### Header: 128 bytes

| Offset | Size | Field/value |
|---:|---:|---|
| 0 | 8 | ASCII `FSTMVAL1` |
| 8 | 2 | version=1 |
| 10 | 2 | header_bytes=128 |
| 12 | 4 | endian tag=`0x01020304` |
| 16 | 32 | nonzero source-policy SHA256 |
| 48 | 8 | required_features=`0x860c0` |
| 56 | 4 | core_id |
| 60 | 4 | context_id |
| 64 | 2 | row_prefix_bytes=72 |
| 66 | 2 | max_value_bytes=64 |
| 68 | 4 | max_pending_rows=4096 |
| 72 | 4 | actual configured live SQ capacity, 1..4096 |
| 76 | 4 | footer_bytes=256 |
| 80 | 4 | hot_schema=7 |
| 84 | 4 | flags=0 |
| 88 | 4 | hot_hash_domain=1 |
| 92 | 4 | source_hash_domain=1 |
| 96 | 4 | row_hash_domain=1 |
| 100 | 28 | zero reserved |

The source-policy descriptor must bind this exact layout/enum/limit text, child source identity and the admitted semantic predicates. Freeze its canonical bytes and actual SHA256 in the curated release spec before implementation; no ad hoc path-dependent hash. This audit proposes the descriptor contents/layout, not an invented digest value. Header equality and full manifest SHA256 authentication are required; policy hash is not a claim of native qualification.

### Owner row: 72-byte prefix + 0..64 payload bytes

| Offset | Size | Field |
|---:|---:|---|
| 0 | 2 | row_tag=1 |
| 2 | 2 | prefix_bytes=72 |
| 4 | 4 | frame_bytes=72+payload_bytes |
| 8 | 8 | owner_record_ordinal |
| 16 | 4 | owner_hot_hash |
| 20 | 1 | status |
| 21 | 1 | owner_role: 0 None, 1 Unified, 2 Address, 3 Data (exact `.uops` values; only 1/2 initially admitted) |
| 22 | 1 | value_kind: 0 None, 1 EnabledBytes |
| 23 | 1 | evidence flags |
| 24 | 8 | data_record_ordinal; UINT64_MAX if missing |
| 32 | 4 | data_hot_hash; must be zero when reference is missing (a present hash may also numerically be zero) |
| 36 | 4 | functional_size |
| 40 | 8 | existing static store_group; zero for Unified/unknown |
| 48 | 8 | byte_enable, bit i refers to byte offset i |
| 56 | 8 | reason_bits |
| 64 | 2 | payload_bytes |
| 66 | 1 | operation class |
| 67 | 1 | zero reserved |
| 68 | 4 | corresponding `.sreq` row hash |

Statuses: `1 ExactBytes`, `2 Nonmutation`, `3 Unsupported`, `4 Incomplete`; zero/others invalid. Operation classes: `0 Unknown`, `1 Ordinary`, `2 LockedRmw`, `3 ConditionalStore`, `4 AtomicFunctor`, `5 BlockZero`, `6 Maintenance`, `7 NoAccess`, `8 OtherLocal`, `9 Htm`, `10 PredicateFalse`, `11 EmptyMask`, `12 OtherSpecial`. Classification precedence is NoAccess → PredicateFalse/EmptyMask when proved → ConditionalStore → AtomicFunctor → Htm → BlockZero → Maintenance → OtherLocal → LockedRmw → Ordinary; any contradictory flags/unknown special combinations yield OtherSpecial/Unsupported rather than guessing. Predicate/empty-mask nonmutation must still have proved actual semantics, not solely the precedence rule.

Evidence bits 0..7: OwnerBound, DataBound, ValueSealed, GroupKnown, RequestIdentityKnown, PredicateKnown, MaskKnown, NonmutationProved. These are factual evidence, not native service outcomes. For Unified with a retained data owner, data ordinal/hash equal owner. For Address, GroupKnown requires nonzero exact existing group and matching retained Data role; missing pair stays Incomplete. GroupKnown is also true for a source-proved Unified with group zero. Every row has OwnerBound. Unknown fields have canonical zero/sentinel values, never default-known.

ExactBytes requires owner role Unified/Address, DataBound/ValueSealed/GroupKnown/RequestIdentityKnown/PredicateKnown/MaskKnown, operation Ordinary or LockedRmw, size 1..64, nonempty mask wholly inside size, and no reason bits. `payload_bytes=popcount(byte_enable)`. Payload is the final supplied byte at each enabled offset in ascending offset order, **not integer significance order**; disabled bytes are omitted and do not mutate memory. Original copied SQ bytes remain opaque address-order bytes. Unmasked 1/8/16/64-byte stores therefore consume 1/8/16/64 bytes, not a fixed 64-byte array. Host/ISA byte-order assumptions of the native producer need policy qualification; the wire does not byte-swap guest data.

Nonmutation requires explicit NonmutationProved and sealed relevant semantic ownership, no payload, and class NoAccess/PredicateFalse/EmptyMask. Do not require imaginary data bytes when that operation semantically supplies none. For a split owner, preserve the actual retained SD/group reference if present; a missing required pair or unknown role is still Incomplete. Unsupported/Incomplete have no payload. Their functional size/mask/request fields are retained only when actually known, including oversized observed size; no truncation to 64 or interpreting absent bytes as zero.

Reason bits: 0 MissingSnapshot, 1 MissingDataRetirement, 2 PairUnknown, 3 RequestIncomplete, 4 SizeUnsupported, 5 MaskUnsupported, 6 ConditionalUnsupported, 7 AtomicFunctorUnsupported, 8 ZeroUnsupported, 9 MaintenanceUnsupported, 10 LocalOrMmioUnsupported, 11 HtmUnsupported, 12 PredicateUnknown, 13 GenerationMismatch, 14 PostSealMutation, 15 ObserverCap, 16 SourceOmission, 17 PartialAtCut, 18 SpecialUnsupported, 19 NoMutationProofMissing, 20 IdentityMismatch, 21 UnretainedDataOwner. Remaining bits must be zero. Unsupported requires a domain reason; Incomplete requires an evidence/lifecycle reason. Irrecoverable contradictions also taint the footer even if an earlier row was already written.

The owner population is every successfully retained hot record with `kFstStore` **or** `kFstAtomic`, including size-zero and unsupported owners. `.sreq` also has loads, but those get no `.mval` row. This conservative population exposes atomic/unusual writes rather than hiding them. An atomic owner may have existing static role None; preserve that role in its Unsupported row instead of fabricating Unified. Pure nonmemory SD hot records get no row. Require all and only these owners in strictly increasing ordinal order across the complete source, not merely the measurement region.

### Footer: 256 bytes

0..7 ASCII `FSTMVEND`; offset8 u16 version1; offset10 u16 bytes256; offset12 u32 flags (bit0 Finalized, bit1 PopulationComplete, bit2 AllValuesAdmitted; remaining zero). Offsets16..144 are successive u64 fields: `source_record_count`, `source_stream_hash`, `expected_owner_count`, `row_count`, `exact_rows`, `nonmutation_rows`, `unsupported_rows`, `incomplete_rows`, `split_owner_rows`, `missing_data_reference_rows`, `distinct_retained_data_reference_count`, `pending_rows_at_common_cut`, `unsealed_rows_at_finalization`, `payload_byte_count`, `row_stream_hash`, `first_bad_owner_ordinal` (UINT64_MAX if none), `reason_bits_or`. Offsets152..255 are104 reserved zero bytes. There are no runtime row-stream/hot-stream SHA256 fields.

Hash domain1: hot row = existing `dependency_record_hash` FNV-1a32 (offset2166136261, prime16777619) over all **64 actual hot bytes**; `.sreq` row hash uses the same algorithm over its actual 56 bytes, with existing version validated. Source hash = existing FNV-1a64 (offset14695981039346656037, prime1099511628211) over all 64-byte hot rows in order, excluding FST header/other sections. Row hash = FNV-1a64 over each complete encoded prefix+payload in order, excluding header/footer. SHA256 uses those same respective row-byte domains. All multiply modulo 2^width. Source/payload/file hash domains must not be interchanged. Final file SHA256 and all companion identities are separately bound in preparation metadata.

Conservation: expected_owner_count=row_count=sum of four statuses; exact/nonmutation plus unsupported/incomplete account for every owner. PopulationComplete requires that equality and no source omissions/cap-induced loss; AllValuesAdmitted additionally requires no Unsupported/Incomplete, no final unsealed rows and no integrity reasons. A finalized file can legitimately describe unsupported/incomplete values; bit19 declares transport availability, not complete PTE state. `.sreq`/`.uops`/`.xlat` full-source identities and actual SD hashes/groups must still independently match. Empty store populations have a valid zero-row footer with full hot-source hash.

## 5. Readers, ownership and private fixture release

Future file ownership, after root freezes the curated spec and the active worker releases overlaps:

- New shared codec/validation header `include/fastsim/fst_store_values.hpp`, mirrored into the native portable-header packaging; new native observer helper isolated from boundary/control collectors. Explicit encoders and bounded streaming readers, no modification of old sreq/uops layout.
- Native integration: `lsq_unit.hh/.cc`, `dyn_inst.hh` only for lifetime token if needed, relevant request-attempt invalidation point in LSQ/IQ after ownership review, and TaoTrace successful-write/static-group/data-retirement/cut/finalization hooks. All overlap with architectural worker files must be reconciled serially. Do not replace that worker's post-update listener or generation machinery implicitly.
- New default-OFF `functional_store_values` TaoTrace parameter and `--functional-store-values` collection flag, complete prerequisites in constructor/config validation, feature19 only when requested, source-policy metadata and new companion packaging. Feature18 remains independent. No native counters/statistics/window changes.
- Maintained `src/trace.cpp`/reader allowlists and streaming full-source validation, a maintained Python value decoder/auditor, preparation copy/hash/capability gates and native portable-source manifest update. Consumers distinguish format validity, owner-population closure, admitted value coverage, relevant-page mutation closure and runtime readiness. Preparation may select PTE-relevant values only after validating the entire source population/footer; it cannot ignore a corrupt tail.

Private fixtures required before implementation release/qualification (not run here):

1. Actual production encoder/decoder roundtrip for sizes 1/8/16/64, interior mask compaction, empty store source, malformed variable length/trailing gzip/enum/reserved/overflow and independent hot/sreq/group/hash corruption. Verify exact offsets/endian bytes with known vectors, not only struct equality.
2. Actual LSQ helper paths for unified integer/FP, split SA-first/data-first, nonadjacent retired SD, same generation replacement **after SA bind before SD retire**, replacement after final seal, request retry/reexecution, SQ reuse/squash and SD orphan. Verify no strong execution-object retention and bounded FIFO.
3. Successful hot only, partial SA at cutoff, SD executes but never retires, source close before already-retained post-update seal, pending pair crossing warmup, cap overflow with no dropped owner/status row. Assert original source population/cycles/events remain unchanged.
4. Ordinary and locked CMPXCHG success/failure bytes, multiword StSplit distinct from SA/SD, merged/overwritten native stores retaining individual values, NO_ACCESS versus generic local accessor, true SC and AtomicFunctor unavailable, CLEAN/INVALIDATE distinct from zero, zero-size/false predicate/empty mask proof failures, split/paged/noncontiguous/masked fragment restrictions.
5. Native source integration OFF/ON on the same already authorized original checkpoint fixture, after bundle completion and explicit release: identical hot/old companions/native calls and statistics/window/population, only new sidecar/feature declaration and declared observer metadata differ. Full-source maintained validator must reconcile exact ownership/seals and capability statuses. No full-four recapture is authorized by this proposal.

## 6. Remaining proof boundaries

The source establishes where ordinary bytes exist and where split pairing/lifetime must be observed. It does not prove every workload's special path is covered. The implementation must verify that its chosen observer attachments cover the exact reset/release/destruction routes above without altering them, bind actual configured SQ capacities, freeze canonical policy digest, and prove every no-effect predicate against the production call path. Masked/trailing-trimmed and special-zero semantics stay unsupported unless that evidence is added; a source comment is not qualification. Full-source actual populations, overhead and caps are unmeasured here.

Store bytes contribute **software PTE mutation inputs** only. Initial physical RAM epoch/backing authentication, all external/functional/DMA writers, hardware walker updates owned by the model, atomic order, racy load-derived value consistency and application at model visibility are separate contracts. `.mval` does not make native RAM snapshots current, certify arbitrary PTE races or move a store to native retirement/merge/send time. A relevant Unknown/Unsupported mutation prevents page-memory closure; unrelated unsupported domains remain reported and must still pass their own full-profile gates. No all-values, full-SPR, CPI or throughput qualification is claimed.


## 7. Binding root rulings and validation release

- The final value seal is the actual retained data owner, not retained SA alone.
  Same SQ/generation SD attempts may refresh the candidate after SA retention and
  before final SD retirement. The exact data ordinal/hash and existing group must
  remain source-correlated; after final data seal, an in-window new attempt is an
  integrity failure even if its bytes match. Blocked-packet retry is not an attempt.
- Footer reason_bits_or includes row reasons plus explicit source-global integrity
  reasons; require every row reason to be included and reject unknown extra bits.
  Extra global reasons are limited to GenerationMismatch/PostSealMutation/
  ObserverCap/SourceOmission/IdentityMismatch/UnretainedDataOwner. Such reasons
  prevent AllValuesAdmitted; no reader may silently use stale published bytes as
  a qualified mutation. Whole-source integrity failure is not made irrelevant by
  a page selection. Preserve status/population versus value-readiness distinction.
- Post-cut work cannot mutate a finalized source's footer or extend its population.
  Metadata seals only concern already retained members under the original callback
  chain. Detached late callbacks safely return; no observer exception changes the
  target run. Codec misuse/untrusted decoding errors still reject normally.
- STORE_NO_DATA is a composite, not a zero-write predicate. Initial v1 admits
  ordinary/qualified resulting locked bytes and proved NO_ACCESS nonmutation;
  false-predicate/empty-mask nonmutation requires actual source integration tests
  before admission. BlockZero/Maintenance/true conditional/AtomicFunctor/swap/HTM
  remain typed Unsupported. Never recover bytes from stale SQ storage or memData.
- Preserve the128-byte Header,72-byte variable row prefix and256-byte Footer,
  with actual-byte payload compaction. Removing redundant native SHA fields uses
  only reserved footer space, not an unknown zero digest pretending authentication.
  Existing native source-policy SHA and maintained full-artifact SHA256 remain.
- Every memory store/atomic hot owner gets a row, including size-zero/special;
  nonmemory SD gets no artificial memory row. Orphan SD affects pairing integrity
  and footer/source diagnostics without fabricating an owner. All original source
  identities, outside-slice corruption and compressed-stream closure are checked.
- This document is not a source/capture release. Root must issue owned-file brief
  after the architectural worker stops, then production TDD, scoped review and
  canonical checks. Native build and one bounded qualification pair wait for the
  necessary integrated bundle. No new full-four collection, model CPI/throughput,
  initial RAM, writer closure or complete PTE qualification follows from this spec.

Canonical layout descriptor: [spr-store-values-v1.layout](layouts/spr-store-values-v1.layout), UTF-8/LF including final LF, 11495 bytes, SHA256 `e01f2bd875a1272d186546b15a2f9bf78bc8ec2da787856d17180b8973f60830`. Header source policy binds the native codec/descriptor; no extra header digest slot is invented.
