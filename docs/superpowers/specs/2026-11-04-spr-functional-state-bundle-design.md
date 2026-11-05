# SPR architectural boundary and physical PTE state bundle

2026-11-04. Design before implementation, continuing the user-approved two-stage
mechanism plan. This document freezes the source and ownership decisions below;
it does **not** release native edits. Exact wire layouts, digest descriptors and
per-slice file ownership must be appended before that slice's EDITSTART. Parent
checkpoint eae8db363fd75a45e0cd44d0c5c3ad95ef780acc; independent child
7e121b8a2e89e651aabf0b9bb895111b383ab427. Generic-FU runtime is the current sole
implementation task. No new four-workload capture until the necessary bundle
and its production consumers are qualified.

## Scope and evidence

MMUcontrols-v1 has passed independent fix review, canonical 26 C++/207 Python,
same original KVM checkpoint OFF/ON neutrality and four historical OFF replays.
The actual ON source has 886,912 sealed records, 8 logical commands comprising
2 retained and 6 unbound PageFault commands, no pending owner and 3/4 ready cores.
The next extension must supply real missing architectural ownership; it cannot
reclassify those six operations by PC/next ordinal or silently relax completeness.

The existing address/path inputs also lack present raw PTE words and retired
software store bytes. Sparse nonpresent leaves are not a complete physical
baseline. These are distinct capabilities from controls transport and from
later ITLB/STLB/walker timing. All current strict/full-profile gates remain.

Binding constraints: retired instruction stream plus pure functional facts;
no native ready/issue/response timestamps, hits, cleared entries, fused/allocation
results or speculative instruction stream. Preserve existing native execution,
SPR structure, calls/events/order, collection population and original reference
windows. No extra drain/checkpoint/flush/barrier, fixed-point replay or input
trimming. Old controls-v1 and all historical formats keep their meanings.

## A. Architectural boundaries and controls ownership

Introduce a separately feature-declared boundary companion and a versioned
controls owner representation: RetiredInstruction / ArchitecturalBoundary /
Unbound. IDs identify functional events within a context/lifecycle, never target
time or a cross-core scheduler order. A zero-effect accepted IRQ is still an event.

For PageFault, begin observation around the actual accepted Commit::commitHead
trap call after its unchanged retry/older-store/HTM-conversion checks. Preserve
the exact accepted fault and ThreadContext identity internally. Existing
PageFault CauseScope resolves to a boundary only through that matching explicit
accepted-invocation capability. Unrelated, nested, external or unsupported
invocations stay unbound. The unretired faulting head never becomes a hot record.

For ordinary IRQ, observe the exact accepted_interrupt object passed through
processInterrupts, after withdrawal checks. Admit actual ExternalInterrupt class
and supported long-mode redirect; SMI/NMI/INIT/SIPI/traps/HTM/debug/retry domains
remain separately unsupported until source-specific support. Do not serialize
native emptiness, readiness, acceptance duration or pending poll state.

Attach an immutable observer token to the actual X86FaultBase redirect. Preserve
it through PC cloning, frontend/IDQ/fetch/DynInst, ROM branches/restart and wrip;
wrip changes npc before entry completion. Clear successor lineage at actual
uEnd/ordinary macro exit/reset; retiring DynInst retains its own original token.
Require the token on every retained entry member, exact context/generation,
.fsem membership and .ffe span agreement. .fsem marks entry members but does not
prove their accepted-event ancestry.

Choose an observer-only **static semantic accessor for generated unconditional
Eret**, bound to an admitted source ROM template, as the terminal descriptor.
Do not identify it by mnemonic or arbitrary IsLastMicroop. Completion requires
that token-bound Eret actually retires and seals; fetch/execution or squash is
insufficient. This closes the hardware entry ROM, not the handler or later iret.

Predecessor is the exact last successfully written hot member and its later
post-update seal, including a retained partial macro prefix. Capture a pending
identity rather than falling back to an older sealed row. Add internal dynamic
macro-origin transport where needed to prove the accepted head shares that
prefix; emit the retained prefix relation, not native pointers/sequences or
wrong-path decode history. BeforeFirst needs an epoch armed before invocation,
complete prefix/omission classification and pre-effect controls baseline; current
first-bind v1 baseline alone is insufficient. Missing head/tail, nesting, true
effect inversion, omitted effects and unknown origins invalidate the capability.

Reuse reviewed MMU owner/propagation/effect-order/cutoff machinery. Preserve all
actual calls, command visits and four-register semantics; capture CR2/redirect
arguments as separate architectural boundary facts. Invocation seals on return;
entry and predecessor metadata seal separately without delaying the target.
At original common end stop admission on every participant; only existing bound
metadata can seal. Terminal callbacks do no reads, allocations or writer work.
Initially bound one active nonnested entry/context, 32 pending metadata tokens,
256 operations/owner; overflow preserves scalar counts and incompleteness.

## B. Retired software mutation values

Introduce separate default-OFF, full-source-bound store-value transport. Observe
immutable bytes in actual LSQUnit::write/writeStoreData paths, before native
merging, using exact SQ SA/SD pairing and generation. DynInst.memData is populated
at later writeback and is not a universal retirement-time source. SA can retire
before SD data/binding; support deferred metadata without additional execution.

Capture every retained store owner's typed value/nonmutation/unsupported state,
original ordinal/hash, group/role and retained SD reference where needed, size,
mask and exact bytes. Reuse validated request/fragments; never invent PA
continuation. Preserve old .sreq/.uops. Reexecuted attempts cannot freeze stale
values or overwrite an already qualified retained binding silently. Bounded
source-ordered pending rows, explicit partial pairs, full-stream hashes and
footer conservation must expose omissions. Initially reuse qualified <=64-byte
functional width; no truncation or arbitrary workload prefix cutoff.

Source CMPXCHG stores the old value on failed compare, so resulting bytes can
represent both mutations without a fabricated universal success bit. True
conditional request existence, cache-side AMO, zero/maintenance and masks need
separate exact functional predicates. Relevant unsupported mutations invalidate
PTE closure. Apply software bytes at the model's own visibility event; native
send/merge/permission/response outcomes and hardware A updates are not inputs.

## C. Physical initial state and writer closure

Select **route B**: synchronous read-only export at the tail of the existing
CaptureProcessor.switch(), after its existing switchCpus/drain/takeovers and
enterSingleEventQueueMode/sim_quantum=0, before return and subsequent simulation
resume. Do not add any target synchronization, access packet, functional walk,
memWriteback, memInvalidate or checkpoint operation. This absorbs earlier
startup/KVM-drain writes into the actual baseline instead of guessing equality
to a pre-switch checkpoint.

Qualify only the source-proved private-backing, never-executed O3, initially cold
Ruby KVM atomic_noncaching handoff with stopped workers, no pre-export hot rows,
known address map and no active host writer. Raw backing is not generally
coherent after O3 traffic (actual Ruby access_backing_store=false). Reject warm
cache restore/replay or unknown lifecycle; do not fix it by flushing. Initial
cold TLB proof is a separate source capability, not a consequence of CR/RAM or
cold Ruby. Never import native TLB contents/LRU/hits as model seeds.

Export all actual PhysicalMemory backing descriptors and bytes, preserving map,
base/length, flags, holes/remaps/alias identities and KVM mapping compatibility.
A missing/unbacked guest physical range is unavailable, not known zero. Keep an
authenticated complete disk baseline (sparse files may represent proven zeros),
with bounded buffers/resident model state and backing for evicted modified state.
The observed PTE path union is an optimization, not universal reachability proof.
New modeled pages obtain original bytes and all prior modeled software mutations,
never a later native memory sample. No arbitrary 256 MiB functional arena limit.

Full memory initial state does not prove writer closure. Account for all post-cut
retired stores and separately classify native walker A updates as model-owned;
cover/reject DMA, DMA backdoor memcpy, functional proxy/readfile, host/shared
backing, reset/restore and later KVM routes. Ordinary cache writeback transporting
an already owned mutation is not another software mutation. A complete,
authenticated per-physical-page unowned-writer eligibility bitmap can retain early
touches; no current-path-only intersection or short-sample zero proof. Unknown
range/provenance/coverage/overflow is explicit incomplete. An excluded page stays
outside the admitted PTE-state domain and reaching it fails the complete case.

Native walker policy remains source-specific: A-write only on nonterminal
continuation; no invented terminal/D update. The model computes its own updates.
Raced/native-update-dependent software values need explicit replay-consistency
limits; this bundle does not promise arbitrary racy execution equivalence.

## D. ROM applicability and immutable semantics

The existing 1,848 pseudo rows with decoder/allocation/fusion Unknown are not
missing architectural instruction bytes. Hardware-entry ROM uses no decoded
Macroop; its dummy ExtMachInst/EmulEnv and artificial size(4) must never become
decoder input. Preserve current v1 Unknown meaning. Introduce explicit versioned
Known / NotApplicable / Unknown status, with disjoint masks and source origin.
Only proved no-decoded-root hardware ROM can make decoded context/root allocation/
root fusion N/A; ordinary applicable missing data remains Unknown.

Certify the separate static IndependentRomMember allocation policy, and coordinate
a generated ROM semantic kind (internal branch, conditional branch, unconditional
Eret exit, other, unsupported) with the boundary terminal descriptor. Existing
ordered operands/pins/normalization/static controls remain applicable and known;
.ffe already supplies pseudo/microcode spans and .ixlat marks byte fetch N/A.
Never sample native allocation slots, group leaders, fused results, ready/grants
or physical versions. Do not invent universal byte widths from size(4).

Target a backward-compatible core-facts v2 dispatch, retaining 128-byte rows only
if exact reserved-byte/layout validation proves it before release. Every N/A
needs actual semantic origin plus .ffe/.fsem/boundary agreement; root allocation
also needs known IndependentRomMember policy. `(known | N/A) == all` alone is
not a qualification predicate. Report all three populations and preserve partial
spans. Decoded software ROM is not hardware-entry ROM; nested/unowned/unknown ROM
must not receive blanket N/A. Source policy, maintained readers/wrappers/auditors,
prepare/copy/version gates must all bind this meaning. No .ffe/.uops/.fsem wire
change is justified just to relabel these flags.

## Integration and validation sequence

Finish generic-FU runtime independently. Then implement/review boundary ownership,
store-value transport and baseline/writer closure in sequential owned slices;
resolve remaining ROM static-fact availability before final schema release.
Reuse maintained codec/reader/Trace wrappers, audit_fst_retired_frontend entries,
prepare_spr_capture, native source fingerprint/validator and conversion refusal
boundaries. Full-source prevalidation must find corruption outside a selected
slice; EOF/cutoff never manufactures completion. Exact protocol versions/layouts,
source-policy closure and test scope are fixed before each edit release.

Required production fixtures cover first-UOP/fetch/partial/repeated-PC PageFault,
IRQ withdrawal/classes/zero effects, ROM branch/wrip/squash/Eret lineage, initial
and final cuts/nesting/overflow, ordinary/split/locked-result stores and special
paths, pre-discovery initialization, physical aliases/new roots/reuse, prior
unowned writers and incomplete/malformed artifacts. Keep current OFF behavior.

After all necessary facts and consumers pass scoped review, freeze the complete
bundle and run one bounded same-original-checkpoint OFF/ON qualification pair.
Compare full retained populations, unchanged companions, target statistics,
independent native/task-clock/identity and capability conservation. Resolve any
active unsupported domain before a new full-four capture. Final accuracy uses
complete matched four-case populations and maintained validator, CPI absolute
and relative errors plus MAE/MAPE. Throughput is separate, at least three quiet
serial interleaved pairs, reporting preparation/export/index/warmup/measurement/
end-to-end costs and RSS. No accuracy, throughput or hardware equivalence is
claimed by this design.
