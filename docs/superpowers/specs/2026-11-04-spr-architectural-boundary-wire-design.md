# SPR architectural boundary bundle: frozen wire and source contract

2026-11-04. Frozen before source edits under the approved two-stage plan. Parent6bddd71a4a446ced6c738252ea077e23d3e0e5e9; child7e121b8a2e89e651aabf0b9bb895111b383ab427. This contract releases only B0+B1+B2+ROM applicability once the implementation brief is issued. RAM/store-value, DMA and reference-view tasks remain separate. Source design evidence is in this plan ledger; the controlling umbrella is [functional state bundle](2026-11-04-spr-functional-state-bundle-design.md).

## 1. Feature/version decision

Verified active declarations in parent `include/fastsim`, `src/trace.cpp`, native `src/cpu/o3/probe`, and `gem5-spr/taogen/shared`: feature families occupy bits 0–17, with core facts=16 and controls=17. Searches for bit18/0x40000 found no FST feature declaration; unrelated ISA flags/working-set sizes do not collide. `tools/audit_fst_privilege.py` currently uses `(1<<18)-1` as its known mask. **Allocate bit18 `kArchitecturalBoundaryBundle`**. Root repeated the declaration collision check at freeze: only an unrelated working-set value and synthetic PC literal matched; no FST bit18 declaration exists.

A single new native option `functional_architectural_boundaries=false` enables `.archb.gz` v1, `.mmuctl.gz` v2 and `.core.gz` v2 together; require existing controls/core/retirement/frontend/static-UOP options rather than silently turn them on. Set bits 18,17,16 and prerequisite existing bits. Bit18 absent preserves existing controls-v1/core-v1/default-OFF behavior. For this release reject core-v2 or controls-v2 without bit18, and reject bit18 with either v1 companion or missing `.archb.gz`. `.ffe`, `.fsem`, `.uops`, hot records and existing filenames remain unchanged. This is a functional-input capability, not model enablement.

All integers below are explicit little-endian unsigned widths; offsets are bytes. No implicit C++ padding is on wire. Reserved bytes/unknown enum values/unsupported bits reject. `uN` means N-bit field, `zN` means N zero bytes. All rows use the existing 8-byte frame prefix `kind:u16,flags:u16=0,bytes:u32` unless explicitly stated otherwise. No native seq/pointer/tick/readiness/free-count/observed allocation outcome is serialized.

## 2. Hashes and identities

* `H32(hot)`: existing FNV-1a32 over exactly the original 64 hot bytes, seed 2166136261, prime 16777619. Every u64 hot-hash field is this value zero-extended; upper32 must be zero. `.core` keeps its existing u32 hot hash.
* `H64(bytes)`: existing FNV-1a64, seed 14695981039346656037, prime 1099511628211, modulo 2^64. `hot_hash` folds **all** original hot records, unmodified order, including warmup. Entry-member hot hash folds only the bound retained entry members in ordinal order, starting at the same seed.
* New body domains are ASCII including trailing NUL: `fst-archb-body-v1\0`, `fst-mmuctl-body-v2\0`, `fst-core-payload-v2\0`. Seed with H64(domain), then hash all nonfooter frames/tags and their exact encoded payload in actual wire order, excluding header and footer. Existing v1 domains/algorithms stay unchanged.
* Operation-list hash: H64 seeded with `fst-mmuctl-ops-v2\0`, followed by the exact encoded Write/Command frames in local index order. Empty operations have the domain seed, not zero. The same hash appears in controls Owner, archb InvocationSeal and archb Terminal when their operation list is known. No footer-to-footer hash dependency.
* Shared policy SHA256 is the maintained native source-policy digest. Each new layout has its own SHA256 of the final canonical UTF-8 layout descriptor. The curator must freeze the descriptor and its computed bytes before implementation release; do not reuse the v1 layout digest. Package manifests separately authenticate artifacts; FNV is an association/integrity check, not a cryptographic content certificate.
* Boundary identity is `(context:u32,generation:u32,event:u64)`. Generation nonzero after arming; event IDs begin at 1, monotonically increasing **within a context/generation**. Event numbering has no cross-core order meaning. UINT64_MAX is the absent ordinal sentinel; hashes accompanying absent ordinals are zero. Event 0 means no event. No “next ordinal” reconstruction.

## 3. `.archb.gz` v1

Header160 offsets: `0 magic[8]="FSTARB1\0"; 8 version:u16=1;10 size:u16=160;12 endian:u32=0x01020304;16 policy[32];48 layout[32];80 required_features:u64;88 context:u32;92 core:u32;96 max_active:u32=1;100 max_pending:u32=32;104 max_ops:u32=256;108 coverage:u32;112 frame_sizes[6]:u32={96,192,128,64,160,320};136 z24`.

`required_features=0x749e0` (bits5 complete dependencies,6 request roles,7 functional retirement,8 raw syscall microops,11 frontend,14 static UOPs,16 core,17 controls,18 boundary bundle). These existing bit positions were checked in their actual headers; additional declared source features remain validated by their own maintained readers. Coverage bits 0–7 respectively certify accepted-call hook, exact invocation/cause, redirected-PC token transport, all successful-commit/omission classification, hot+post-update binding, source ROM-kind accessor, common cutoff, initial epoch. Required coverage=255; unsupported actual events still invalidate readiness despite complete hook coverage.

| Kind / size | Fields following prefix, in exact order |
|---|---|
| Epoch=1 /96 | `context:u32,generation:u32,proof_flags:u32,state_known:u32,state[4]:u64,pc:u64,micro_pc:u64,hot_at_arm:u64,z16` |
| Accepted=2 /192 | `event:u64,context:u32,generation:u32,cause:u16,vector:u16,fact_flags:u32,reason_mask:u64,pc:u64,micro_pc:u64,fault_va:u64,error_code:u64,predecessor_kind:u8,carrier:u8,mode:u8,z1,prefix_flags:u32,predecessor_ordinal:u64,predecessor_hash:u64,prefix_begin:u64,prefix_end:u64,parent_event:u64,controls_pre[4]:u64,cr2_before:u64,epoch_generation:u32,known:u32,z24` |
| InvocationSeal=3 /128 | `event:u64,context:u32,generation:u32,status:u32,known:u32,redirect_micro_pc:u64,vector_arg:u64,ip_arg:u64,error_arg:u64,cr2_after:u64,controls_post[4]:u64,op_count:u32,writes:u32,commands:u32,entry_kind:u16,mode:u8,z1,op_hash:u64` |
| EntryMember=4 /64 | `event:u64,context:u32,generation:u32,ordinal:u64,hot_hash:u64,macro_begin:u64,micro_pc:u64,member_flags:u32,rom_kind:u8,z3` |
| Terminal=5 /160 | `event:u64,context:u32,generation:u32,status:u32,reason_mask:u32,seal_flags:u32,z4,predecessor_ordinal:u64,predecessor_hash:u64,entry_begin:u64,entry_end:u64,first_hash:u64,last_hash:u64,members:u64,sealed_members:u64,member_hot_hash:u64,ffe_semantic_hash:u64,ffe_code_hash:u64,control_ops:u64,control_op_hash:u64,z16` |
| Footer=6 /320 | prefix + the 23 u64 fields listed below + `reasons[16]:u64` |

Footer fields in order: `status,hot,hot_hash,body_hash,accepted,accepted_pf,accepted_irq,accepted_other,terminal_complete,terminal_partial,terminal_unsupported,suppressed_events,pending,invocation_sealed,entry_bound,member_rows,sealed_members,boundary_ops,boundary_writes,boundary_commands,event_rows,epoch_rows,na_rom_rows`.

Enums and canonical values:

* Cause: PageFault=1, ExternalInterrupt=2, NMI=3, SMI=4, INIT=5, SIPI=6, Trap=7, OtherFault=8, HTM=9, Retry=10, OtherExternal=11. Only exact PF/ExternalInterrupt and supported LongMode invocation qualify; unsupported kinds are recorded separately, never promoted by vector/name.
* Mode: Unknown=0, LongMode=1, Other=2. Vector is u16 so 0..255 are genuine and 65535 is unknown. Carrier: ordinary faulting member=0, fetch carrier=1, asynchronous no-head=2. IRQ facts must not fabricate a head.
* predecessor_kind: Unknown=0, Retained=1, BeforeFirst=2. Retained requires original successful hot anchor. BeforeFirst has absent ordinal/hash and matching armed Epoch; Unknown never qualifies. Prefix flags: `HasPartial=1,SameDynamicMacroProved=2`; no prefix means absent begin/end. Partial prefix has `[begin,end)` ending immediately after the exact predecessor, same dynamic macro origin proven internally, and matching partial ffe span.
* Accepted fact_flags: `VA=1,Error=2,CR2=4,PredecessorSealPending=8,ParentEvent=16`; known bits: `ControlsPre=1,AcceptedPC=2,ActualCause=4,Epoch=8`. Unavailable fields are canonical zero except absent ordinals/vector. Error/VA absent for ordinary IRQ. CR2 meaningful only for PF. The independent PF error has real presence, not zero-as-absent.
* entry_kind: Unknown=0, LongInterrupt=1, LongInterruptWithError=2, OtherUnsupported=3. Store the actual selected redirect micro-PC; policy binds labels to the source. Invocation status: Sealed=1, Unsupported=2. known bits: `Redirect=1,VectorArg=2,IPArg=4,ErrorArg=8,CR2After=16,ControlsPost=32,Operations=64`. IRQ ErrorArg/CR2After stay absent, not fake known zeros.
* Member flags: `ActualHead=1,GeneratedUnconditionalEret=2`. rom_kind uses core-v2 enum below. EntryMember is emitted only after successful hot write; it does not claim post-update seal.
* Terminal status: Complete=1, Partial=2, Unsupported=3. seal_flags: `Predecessor=1,Invocation=2,Head=4,ExactExit=8,AllMembersPostUpdate=16,SpanAgreement=32,PartialPrefixProven=64`. Complete requires first six and bit64 iff partial prefix. BeforeFirst's Predecessor bit means the Epoch/prefix certificate, never a fabricated row. Missing entry uses absent begin/end, zero first/last hashes and zero members; empty member hash is H64 seed.
* Reasons bit/index 0–15: InitialEpochUnknown, UnsupportedCause, UnsupportedMode, Nested, NoRedirect, PredecessorUnknown, PrefixUnknown, MissingEntryHead, EntryDiscontinuity, MissingExit, ControlMismatch, EffectOrderUnsupported, Overflow, LifecycleMismatch, PostCut, CounterOverflow. reason_mask has no unknown bits. Reason counters are diagnostics and may overlap; they do not partition events.
* Epoch proof_flags: `NoO3ExecutionInEpoch=1,BeforeResume=2,PrefixObserverArmed=4,NoLivePriorControlScope=8`; state_known=15 for CR0/CR3/CR4/EFER. Qualified epoch requires flags15, hot_at_arm=0, non-ROM initial micro-PC and the source handoff checks. Unknown epoch may be structurally emitted but cannot qualify BeforeFirst/full boundary capability.
* Footer status bits Finalized=1,Complete=2,Empty=4. Empty means zero hot AND zero accepted; zero-effect IRQ is never empty. Terminal footer required; EOF is not a completion signal.

Lifecycle wire rules: Epoch precedes accepted events/retained bundle work. Emit Accepted at actual acceptance before effects; exactly one InvocationSeal only after invocation returns with sampled immediate facts; stream EntryMember rows as they actually retire; emit exactly one Terminal when sufficient metadata resolves or original cutoff/teardown forces Partial/Unsupported. Metadata rows may interleave across events; all reference a prior Accepted. One event's EntryMembers strictly increase ordinals and cannot duplicate any hot member. At footer `accepted=accepted_pf+accepted_irq+accepted_other=terminal_complete+terminal_partial+terminal_unsupported+suppressed_events+pending`; `event_rows=accepted-suppressed_events`. Finalized normally forces pending=0 with explicit partial terminals; nonzero pending is never Complete. Overflow may suppress new event details with saturating scalar counts, never stall native work; suppressed IDs cannot appear as valid member/control owners.

## 4. `.mmuctl.gz` v2

Preserve v1 types/decoder/writer behavior behind version dispatch. New namespace/types should not overload the meaning of v1 `Member`, `Group`, `ready` or counters. Reuse existing Write96 and Command48 bytes/enums/indices/parent checks unchanged; application remains Deferred=0,Immediate=1,External=2. Invocation commands are boundary-owned with existing logical scope semantics; their values are not model timing.

Header160 retains v1 offsets: magic becomes `FSTMMU2\0`, version=2; policy/layout fields at16/48; required:u64 at80 (frozen v2 contract=1); context/coverage at88/92; limits owners/ops/diagnostics at96/100/104 remain4096/256/256; reserved108; size slots112 are `{96,64,96,48,0,320}`; trailing24 zero. Kinds remain Baseline1, Owner2, Write3, Command4, reserved/forbidden5, Footer6. Coverage255 = old63 + explicit boundary invocation64 + armed epoch128. A bound accepted-event marker is not a retained hot member.

| Frame | Exact fields after prefix |
|---|---|
| Baseline96 | `context:u32,generation:u32,cut_kind:u16,known:u16,proof_flags:u32,anchor_id:u64,anchor_hash:u64,state[4]:u64,epoch_reference:u64,z16` |
| Owner64 | `owner_kind:u16,flags:u16=0,reason:u32,context:u32,generation:u32,owner_id:u64,hot_hash:u64,count:u32,status:u32,op_hash:u64,causal_event:u64` |
| Footer320 | prefix + 23 u64 fields below + existing `reasons[16]:u64` |

Baseline cut_kind Unknown=0,ArmedEpoch=1,Empty=2. ArmedEpoch: known15, proof_flags copied from matching archb Epoch, anchor_id=UINT64_MAX,anchor_hash=0,epoch_reference=zero-extended generation; state exactly matches archb Epoch. Unknown cannot qualify. Empty has no events/hot and no fabricated baseline. This v2 baseline is pre-effect; v1 first-retained baseline remains unchanged.

Owner_kind RetiredInstruction=1,ArchitecturalBoundary=2,Unbound=3. Retired owner_id=original ordinal with original hot hash; boundary owner_id=event ID,hot_hash=0; unbound owner_id=monotonic diagnostic-group ID,hot_hash=0. Context/generation always explicit. `causal_event` is zero except an Unbound group's exact known accepted cause may reference an emitted Accepted without granting it ownership. reason=UINT32_MAX when absent, otherwise existing v1 Reason0..15. status bits `Sealed=1,Unsupported=2,Partial=4`; Complete qualification requires all retained owners and invocation-capable boundary owners sealed without unsupported/partial. One Owner followed immediately by count Write/Command frames, contiguous operation indexes0..count−1. No nested frame groups.

Exactly one Retired Owner per hot row, including zero effects; one Boundary Owner per emitted Accepted, including zero-effect IRQ and unsupported event markers. Unsupported event markers can have zero operations; their actual nonqualified effects remain separate Unbound groups. Proven invocation ownership with later missing entry may remain Boundary-owned but partial; it must not become ready. Partial invocation lists are explicitly partial and do not pretend InvocationSeal occurred. Controls Owner emission can wait for metadata, but retained owners are ascending ordinal, boundary owners ascending event ID, and a unified logical placement check enforces predecessor-before-boundary-before-entry; unbound diagnostics have no invented causal placement. Internal effect tickets detect inversions; no tickets go on wire.

Footer fields: `status,hot,sealed_retired,total_writes,retained_writes,boundary_writes,unbound_writes,total_commands,retained_commands,boundary_commands,unbound_commands,visits,retained_groups,boundary_groups,unbound_groups,unsupported_groups,suppressed_ops,pending_owners,hot_hash,body_hash,invocation_sealed_boundary,boundary_ops,event_rows`.

Conserve total writes/commands separately as retained+boundary+unbound; visits remain propagation visits, not commands. Unsupported/suppressed details retain scalar counts under existing fix1 rules, never count twice. Boundary groups=event_rows=archb.event_rows; invocation_sealed_boundary=archb.invocation_sealed; boundary_ops/writes/commands equal archb footer and matching event terminals. Every fully sealed invocation matches its Owner count/hash and InvocationSeal count/hash. All-source incomplete groups are visible; no promotion of six historical unbound PF commands.

## 5. `.core.gz` v2, still 128-byte members

Retain first96 member bytes exactly: existing identity, hot hash, Known/static/root fields and operands. Replace reserved offsets96–127 only in version2:

| Offset | Field |
|---|---|
|96| `not_applicable_flags:u32` |
|100| `semantic_origin:u8` |
|101| `allocation_policy:u8` |
|102| `rom_kind:u8` |
|103| `extension_known:u8` |
|104| `boundary_event:u64` |
|112| `boundary_context:u32` |
|116| `boundary_generation:u32` |
|120| `z8` |

N/A allowed bits are exactly Decoder(1), Allocation(4), Fusion(16), mask21, disjoint from known_flags. N/A only for source-proved no-decoded-root hardware ROM with validated boundary/ffe/fsem membership. Ordinary applicable absence remains Unknown. Keep canonical zeros in decoder/root/fusion fields when N/A; never set their Known bits or read dummy ExtMachInst/size4.

Origin Unknown=0,DecodedRoot=1,HardwareEntryRom=2,UnsupportedRom=3. AllocationPolicy Unknown=0,DecodedRootRules=1,IndependentRomMember=2. RomKind Unknown=0,Other=1,InternalBranch=2,ConditionalInternalBranch=3,UnconditionalEret=4,UnsupportedControl=5. extension_known bits AllocationPolicy=1,RomKind=2,BoundaryToken=4. Unknown extension values have zero payload. Hardware ROM's independent-member policy is an immutable source rule, not native allocationSlots result. DecodedRoot has no boundary token and does not receive blanket ROM N/A (software ROM with a decoded macro remains decoded-root domain). Unsupported nested/unowned ROM remains explicit Unknown/N/A-unqualified.

Header128: first96 bytes preserve existing core header fields/offsets except magic `FSTCOR2\0`, version2, header_size128; append layout_sha256[32] at96. Row_size128,operand_size24,macro_size80,decoder_layout1,register_layout1 unchanged. Existing tag1 row+operands and tag2 ffe macro remain. Footer64, no frame prefix: magic8 `FSTCEND2`; records:u64,macros:u64,hot_hash:u64,payload_hash:u64,na_rom_rows:u64,boundary_rows:u64,unknown_applicable_rows:u64. These eight fields total64. N/A/Known/Unknown group counters are computed for every group by the full-source reader; the footer row counters are a cross-check, not a replacement. UnknownApplicable excludes properly proved N/A, but `(known|N/A)==255` alone is insufficient.

For each token-bound core row require exactly one archb EntryMember with the same token, ordinal/hash/micro_pc/macro_begin/rom_kind. `.core` boundary_rows equals archb.member_rows. `.core` na_rom_rows equals archb.na_rom_rows and is a subset of boundary_rows. Existing `.ffe` macro-byte/context checks continue on DecoderKnown rows; source-proved N/A has zero bytes and Pseudo+Microcode, no DSB eligibility. Normalization/operands/pins/static/frontend remain applicable. Partial accepted entry can have true observed ROM origin but cannot satisfy complete bundle readiness; do not erase its rows to hide that status.

## 6. Source APIs, scope and file ownership

Assign **one implementation owner for this entire slice**, after the curator records an exact EDITSTART file set. Coordinator reports GenericFU committed/pushed as `6bddd71`, with no other active implementation; this report did not inspect Git. Helpers below are proposed APIs, not existing implementations.

| Owner area | Proposed files / source-real API insertion |
|---|---|
| Shared wire | New parent `include/fastsim/fst_architectural_boundaries.hpp` mirrored native `taogen/shared/fst_architectural_boundaries.hh`; new v2 control codec alongside v1 files; version-dispatched core codec in existing mirrored core headers. Explicit LE encoders, no struct reinterpretation for new versions. |
| Native lifetime helper | New `src/mem/functional_architectural_boundaries.hh`: immutable `BoundaryToken`, immutable internal `MacroOrigin`, bounded Observer, RAII `AcceptedInvocationScope`; registry keyed exact TC endpoint/generation. TaoTrace owns observer/writer, never DynInst. `beginAccepted(tc,actualFault,head-or-null,predecessorCertificate)` returns scope; no fault/native object retained after return. |
| Epoch | Add exported TaoTrace `armArchitecturalBoundaryEpoch()` via `probe/TaoTrace.py`; call each existing trace at **tail** of `configs/fastsim/fs_capture.py::CaptureProcessor.switch`, after existing super.switch/enterSingleEventQueueMode/sim_quantum=0. Check never-executed O3 observer witness, capture active from this cut, zero hot, no active control owner/ROM, no lifecycle conflict. A failed check emits unknown epoch/fails capability; no drain/checkpoint/extra event. Observe original CPU startup/takeover/tick lifecycle through observer-only flags. |
| Accepted calls | `o3/commit.cc`: wrap only actual accepted trap/processInterrupts, unchanged order/retry/withdrawal/HTM conversion. `cpu.cc` and `arch/x86/faults.cc`: exact fault+TC scope validation, `noteActualRedirect(token,entry,pc,arguments)` before original tc->pcState; PF demap capability activated before original demap; CR2 observed separately; `sealInvocation` after original return. Preserve CauseScope precedence for everything else. |
| Token and macro lineage | `arch/x86/pcstate.hh` copy/update/reset/advance/uEnd; `o3/frontend_record.{hh,cc}`, `fetch.cc`, `frontend_fetch.cc`, `dyn_inst.{hh,cc}`, real squash/resolve paths. Fresh decoded macro-origin attached observation-only at native expansion creation; restart copies it. Not PC equality or allocation-group outcomes. Never enter architectural equality/cache/predictor keys or checkpoints. |
| Static ROM kinds | `cpu/static_inst.hh` observer-only `functionalRomKind()` accessor (default Unknown); x86 real microop default Other; generated `isa/microops/seqop.isa` Br/BrFlags override Internal/Conditional, **unconditional Eret override UnconditionalEret**, EretFlags UnsupportedControl. Source template membership checked at redirect/lineage. No native flag/execute/prediction changes. `core_functional_facts.hh` consumes these immutable descriptors. |
| Native capture and controls | `probe/tao_trace.{hh,cc}` classify every successful commit/omission, bind exact successful fwrite, `postUpdateSeal` from existing CommitMiscRegsApplied, match ffe/fsem spans, `closeAdmission` in original all-participant common-cut loop. `mem/functional_mmu_controls.hh` tagged owners and scalar conservation, exact boundary capability exception, initial overlap checks, effect-order intervals, bounded pending/terminal handling. `.ffe/.fsem/.uops` emission meanings unchanged. |
| Parent readers | New `fst_architectural_boundary_reader.{hpp,cpp}`; existing `fst_mmu_control_reader`, `fst_core_functional_reader`, `trace.{hpp,cpp}` version dispatch. Add `current_architectural_boundary_member()` and `boundaries_before(original_ordinal)` views carrying functional identities/operations, not timing. Pending-before-first events have an explicit initial boundary view. Events without successor remain incomplete metadata, never attached to an unrelated next row. Keep current_mmu_controls v1 contract and expose a distinct v2 view. |
| Qualification | Existing maintained `audit_fst_retired_frontend.{cpp,py}`, `prepare_spr_capture.py`, native collector argument/feature checks, `generate_static_uop_policy.py`, parent CMake/native SConscript and copy/conversion refusal points. Add all new observer, PC/fault/ROM/accessor source files to policy closure. No parallel evaluator. Scope tests to codecs/production readers/hooks/real wrappers; no workload capture at this slice. |

API lifecycle: `armed -> accepted -> invocation sealed`, plus independent predecessor seal and `entry bound -> member seals -> complete`. Operations can seal before entry exists. `postUpdateSeal` resolves the **captured** pending predecessor, never chooses a newer one. One active nonnested event/context,32 pending metadata tokens,4096 retained owner payloads,256 operations/owner; diagnostics256; bounded streaming per-member records. No native object lifetime extension. Nested event records are unsupported, not silent token replacement.

## 7. All-source validation and cut rules

Preparation must scan complete originals before selecting a Slice/Warmup view: feature/header/version/policy/layout identities; every hot member exactly once in retained controls; all sparse member/boundary references; correct ffe starts/end/hashes, fsem membership/abandonment, core row projection; unique events; invocation/order/terminal and all footer conservation. Then expose original ordinals without rekeying. Copy/conversion that cannot preserve v2 and archb must refuse. No selected-slice-only checksum can certify the source.

Complete boundary requires accepted supported object/mode, exact redirect, proven predecessor or BeforeFirst, same-token first retained ROM head, all members in the same admitted ffe span, source-generated unconditional Eret actually retained and post-update sealed, original native successor leaving ROM, sealed operations and no reason. `.ffe Complete` or `.fsem bits` alone are insufficient. An event after a retained partial prefix needs the actual macro-origin proof and abandonment relation; unretired faulting head remains absent.

At original common cutoff freeze admissions on all participants. Already bound retained owners may finish original permitted post-update metadata under fix1. No new boundary invocation/effects, sampled post-cut CR state or entry members. Accepted missing head/tail/predecessor become terminal Partial; force-close preserves scalar excluded/suppressed evidence. Terminal callbacks return before reads/allocations/counter changes/writer access. Footer Complete also requires valid shared cut, zero unsupported/suppressed/pending/applicable-unknown populations and all cross-file equalities. Valid typed N/A does not waive incomplete entry lineage.

The Epoch/controls baseline is an independent pre-invocation CR certificate. It can qualify BeforeFirst only if the actual tail handoff is armed before O3 executes in the newly completed takeover generation and every prefix commit/omission is covered. **It is not RAM equivalence, writer closure, initial TLB state, or full PTE-state qualification.** Later full RAM export may use the same source cut and bind its own proof; absence of that later proof keeps the combined physical-state capability unavailable.

## 8. Binding root rulings at edit release

1. Epoch proof bit1 means **no destination O3 execution in the current completed
takeover/source generation before arming**, not a claim that the CPU object has
never run historically. Prove the actual source path: completed original KVM
handoff/takeover tail, no intervening resume/tick, no prior hot in this generation,
no current ROM entry/live MMU scope and single initial arming. Do not infer it
from zero hot alone. In-window lifecycle/rearm remains sticky unsupported. This
control-prefix certificate does not certify restored RAM/cache/TLB coldness;
those independent initial-state gates remain false in this slice. A runner
without this exact cut/witness emits unknown epoch, never fabricated proof.

2. `CommitMiscRegsApplied` is before `staticInst->advancePC` in the actual source.
Add an observer-only `CommitPCAdvanced` notification immediately **after** that
unchanged advance call and before lastCommittedSeqNum update. Require actual
Eret successor leaving ROM from this hook, plus original post-misc seal, before
complete terminal/footer. Do not infer successor from predicted/executed PC,
post-misc alone, mnemonic or any LastMicroop. Already bound terminal metadata may
finish through this original callback chain at common cut, with no extra cycle
or event. Native advance/update/stop/checker order remains unchanged.

3. The generated static accessor is the chosen ROM descriptor representation.
Preserve source macro-origin across actual clone/restart paths; never serialize
wrong-path decode ordinals or use native allocation groups as dynamic identity.
Only supported nonnested PF/ordinary ExternalInterrupt ROM ancestry can obtain
qualified N/A. A supported but cut-short entry can retain true per-member N/A
facts while bundle readiness stays false. Unsupported cause/mode/nesting or
missing ancestry uses explicit unsupported/unknown applicability, preserving all
hot rows. No decoded decoder bytes or artificial size4 become ROM facts.

4. Detail unsupported accepted events until the fixed pending/detail limits;
then scalar-suppress with explicit counts/reasons. Suppressed event IDs never
become valid wire references. Operations whose complete ownership/detail cannot
be represented remain Unbound/suppressed with exact scalar conservation, not a
successful boundary operation hash. Total retained and boundary frame counts
must match their footer buckets exactly; suppressed unbound detail differences
must equal suppressed_ops. Complete requires zero such uncertainty.

5. Core-v2 keeps the existing **u8 tags**1/2/255 (row/macro/footer); its rows do
not acquire an8-byte frame prefix. New v2 bytes use explicit LE serialization;
old v1 behavior remains intact. Core-v2 payload hash includes tags1/2 and exact
row/operand/macro bytes, excluding header, tag255 and footer. Layout descriptor
files below are authoritative byte strings, UTF-8/LF including final LF. Codec
layout_descriptor() must match exactly, not hash an alternative spelling.

6. Add separate v2 codecs/readers/views rather than reinterpret v1 Group/ready.
Existing source-policy constants describing v1 remain meaningful; declare new
architecture/core-v2/control-v2 capability constants explicitly and dispatch
qualification by actual selected version. Existing module v1 flags alone still
write v1. No decoder, stop, scheduling, allocation, clock or SPR parameter change.

7. This slice has **no workload/native build/capture release**. Root alone owns
canonical builds, later integrated native source freeze and qualification,
CMake, curated docs, commits/push/portable child patch. Complete helper/codec/
reader/source-hook validation and independent review first. Full source capture
stays held until all necessary bundle and separate reference evidence are ready.

## 9. Frozen layout byte identities

- `docs/superpowers/specs/layouts/spr-archb-v1.layout`: 9772 UTF-8 bytes, SHA256 `3622b47c0d37b08c19403e8cbcf5b8a9af29459ca07c1d6d7a37507b2ab048a0`.
- `docs/superpowers/specs/layouts/spr-mmuctl-v2.layout`: 7062 UTF-8 bytes, SHA256 `df78bcac05371ed0ea3b8497f56255323f7221e52b3ef7844f630d29acdc3d10`.
- `docs/superpowers/specs/layouts/spr-core-facts-v2.layout`: 5656 UTF-8 bytes, SHA256 `5319ee9aaefab9945cd9189a2e3686088fc2a515a038414a061d13152704aaaf`.
