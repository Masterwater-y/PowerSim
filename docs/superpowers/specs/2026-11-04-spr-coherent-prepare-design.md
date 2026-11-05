# SPR coherent store prepare: first production interop

2026-11-04. Design before code, after F1 at36b3fb9. Actual FLAC full-target
entry-v2 selected NativeCoherence, disabled only competing legacy coherence,
retained target/runtime early prepare=true, and failed the explicit no-early
coherence gate before executing instructions. No capture/input/schema change
is needed for this mechanism. This task closes that combination, not every
remaining FLAC class or full SPR qualification.

## Binding source behavior

Use the source-anchored 63-line `f2-coherent-prepare-preflight.md` in the current
plan ledger and its exact L0/L1/Sequencer references. The independent child is
read-only; no SPR structure/parameter edits or native timestamps/hits/outcomes.
Retired stores provide addresses/dependencies/SA-SD; the model decides prepare
readiness, arbitration, permission and responses on its own persistent timeline.

Preserve ordinary16 and optional16 independent ownership; no hint enters the
ordinary Sequencer table. CPU StoreBuffer retains mandatory-first send ports,
source cap/drop/tokens. Optional receiver entry is CPU-send+1, without ordinary
mandatory data-array delay. L0 receiver declaration order is optional, incoming
link, mandatory; successful Valid transition restarts first port. ResourceStall
retains its head and falls through. No-fill recycle reschedules +1 and falls
through without immediately processing a younger optional head; a subsequent
successful lower-port transition can cause a normal first-port restart. Native
budget exhaustion defers by the source cycle rule; no global counter/throw,
whole-epoch replay or artificial generic progress tick.

I prepare allocates tag+TBE+fill and GETX; S prepare allocates TBE only and
UPGRADE; supported E/M/transient requests ignore as specified by native rows.
Introduce L0 SP_IE. Load waits there. Store takes over SP_IE->IM, returns
optional credit once and profiles one demand miss, retaining physical identity,
TBE/fill/traffic. Untaken exclusive fill installs E, returns fill/TBE/optional
credit, wakes waiters and does not write architectural data or call ordinary
completion. Untaken SP_IE InvOwn/InvElse ACKs without cancellation, state change
or CPU eviction; after takeover, ordinary IM invalidation behavior applies.
L1/LLC/directory must use existing real coherence states, data/ACK/unblock,
credits, physical hooks and DRAM service; permission does not complete at hint
acceptance. Preserve parent SM->SM_IL0->IM races, not a local tag-only shortcut.

## Frozen interface and ownership decisions

Add public `NativePrepareRequest` containing `PendingOwner owner`, existing
`PrepareToken token`, and `uint64_t line_address` (aligned physical byte-line
address, not line index). Add `NativeCoherence::prepare(request,cpu_send_cycle)`;
it owns +1 entry timing and validates core/alignment/unique token/generation
before enqueue. StoreBuffer owns the already accepted optional credit.

Use `NativeProtocolOrigin = std::variant<SequencerService,NativePrepareRequest>`
as `NativeProtocolMessage::origin`, replacing the ordinary-only service payload.
Existing request(service,ready) remains the ordinary adapter. Keep the physical
origin immutable through sends, forwards and takeover; route/lease/audit helpers
extract its real owner/address. No dummy SequencerService, fake ordinary credit
or rewriting of an in-flight token. Transaction state may attach a separate
real `optional<SequencerService>` for the actual Store takeover; its callback is
completed exactly once on physical response. Migrate existing message fixtures
and envelope validation to the tagged type, with old ordinary behavior intact.

Add a typed prepare-terminal hook taking the request, existing PrepareTerminal,
and cycle, returning whether the CPU StoreBuffer accepted the terminal. Only
Ignored/TakenOver/Fill apply in this domain. Optional terminal happens once;
TakenOver ends only optional credit, not the physical transaction or lease.
NativeCoherence owns coherent prepare physical lifetime, so the simulator must
not also create legacy hint Members/leases or use its global prepare_lines.
Per-core controller line ownership is authoritative. Distinct cores preparing
the same physical line are independent coherent operations; same-core duplicate
hints return independently under source ignore rules. Extend idle/validate and
end-drain counts for optional ingress/terminals, physical prepares, attached
ordinary callbacks and leases. Detached work never extends F1 frozen CPI.

Replace the unused bool access-hook context with typed `NativeProbeOrigin`
values Ordinary/StorePrepare/Takeover. L0 hint lookup does not become an ordinary
demand probe; lower-level GETX/UPGRADE retain source request profiling; takeover
profiles exactly the source-defined demand miss, including an S-origin upgrade.
Use the existing source-qualified profiling path where possible; do not invent
an extra cache transaction merely to obtain a miss counter. Software prefetch
and retired memory populations stay separate from store prepare. This internal
model provenance is not a new native PMU truth channel.

Use an explicit internal L0 dispatch result Accepted/ResourceStall/Recycle,
not a bool that conflates budget/pop/restart behavior. Existing non-L0 port
ordering and controller budgets remain unchanged. Coherent hints use the existing
protocol source/next_cycle calendar, not the old early loop/global budget.

## Dirty/data decision

Keep message/parent dirty debt and symbolic received-data lineage unchanged.
Native L0 fill copies DataBlk only; its local-modified/physical Dirty starts
false and becomes true only on an actual ordinary callback with wrote=true.
Untaken hint and ordered-FIFO callback returning wrote=false cannot invent a
write, increment data serial or inherit transport Dirty. Apply this narrow
source-conformant distinction to coherent L0 fills/takeovers and test existing
ordinary owner transfers. Other controllers retain their existing dirty rules.
No unrelated cache replacement or dirty-policy rewrite is authorized.

## Integration bounds and tests

Owned scope: spr_native_coherence.hpp/.cpp, simulator.cpp combined adapter,
config.cpp/main.cpp capability/gates, focused existing/new runtime tests.
StoreBuffer/Sequencer changes only if an actual missing interop API is proved;
cache storage changes only for a necessary narrow profile/dirty action. Root
owns CMake/docs/Git/canonical/full-case work. Native child and generic-FU source
pins are read-only. Existing noncoherent single-core early path is preserved.

Narrow coherent/early and single-core exclusions only after the combined path
exists. Preserve cold/static ordinary domain, physical-bit topology, imported
latencies/budgets, no-replacement and unsupported class/split/secure/atomic/IO/
software-prefetch/frontend/rename gates. Do not disable the FLAC target features.
No collector/schema/baseline/store-value/network/LLC replacement expansion.

TDD must include actual Simulator integration, with helper probes supplemental:
2-core same-line hints with distinct credits; optional cap16 independent of
ordinary16; I fill vs S upgrade under fill-full; load wait/store takeover before
data/commit; untaken/after-takeover remote invalidation; remote dirty data with
no invented local write; equal-cycle optional/link/mandatory arbitration,
resource-stalled head/no-fill recycle/budget deferral; delayed ordered-store
callback wrote=false; stale/duplicate responses before mutation; separate
terminal/physical/ordinary credit conservation and F1 late drain. Compare
chunks1/2/7/128 and delayed functional producer tuples. Existing ordinary-coherent
and single-core early suites must pass. Unsupported replacement fails before
protocol/tag mutation. Whole-FLAC/cold source/accuracy/throughput qualification
remain separate; this task must report the next real blocker honestly.


Source anchors (unchanged native code in gem5-spr):
`src/mem/ruby/system/Sequencer.cc:1110-1127` (separate cap/table),
`protocol/MESI_Three_Level-L0cache.sm:1409-1427,1471-1480,1531-1538`
(prepare/takeover/ignore/fill), `:352-384` (optional timing/recycle),
`:910-915,840-858` (data-only fill versus actual write dirty), and
`protocol/MESI_Three_Level-L1cache.sm:1158-1185,1367-1395,1437-1440`
(coherent acquisition/upgrade race), all protocol paths under src/mem/ruby/.
