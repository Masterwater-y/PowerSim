# SPR causal DRAM rank-refresh adapter

This is a bounded implementation slice of P5/F2 in the approved two-stage
alignment plan, after runtime checkpoint 8571191. The unchanged full FLAC
entry-v3 reaches the causal adapter guard. Exhaustive production-parser
preflight finds ten target/frontend predicates plus three competing legacy
solver selectors. Refresh is one actual enabled target mechanism, not a
reporting flag: the immutable mapped configuration supplies refresh=true,
start_tick=0, ticks_per_cycle=385, tREFI=3900000 and tRFC=295000.

## Scope and authority

Connect the existing rank-refresh recurrence to MixedDramController, which
SharedSystem::PendingMemoryState actually uses for causal native/coherent
memory. Reuse DramRankRefresh and source bank-preparation/DRAMInterface rules;
do not route through the old DramModel or borrow its completion timestamps.
Read the unchanged native refresh/FRFCFS actions needed for a concrete rule.
No new source schema, collector, native build/capture, physical baseline,
replacement, frontend/TLB, oracle, or unrelated timing-policy rewrite.

Only model inputs and imported parameters may determine service. Preserve the
persistent two-stage strict arrival/frontier contract and sparse event calendar;
no global fixed-point/replay, native refresh outcomes or time fitting. Keep
refresh OFF outputs unchanged. Unknown native entry phase stays explicitly
modeled from configured origin, never claimed native aligned or strict accuracy.
No target parameter is turned off or altered to obtain a successful run.

## Required behavior

One bounded calendar per real rank shares that rank's actual bank state. Source
preparation waits for modeled bank PRE legality, completed data/burst obligations
and configured timing; refresh closes affected rows, removes old row-hit benefit,
and blocks ACT/read/write only for that rank through its modeled ready edge.
Other eligible ranks/channels may progress. Use existing timing-domain checked
conversions and recurrence. Do not mutate refresh/bank state during candidate
probes, consume a request while all relevant ranks are unavailable, or globally
stall unaffected ranks. No fabricated store/ordinary owner for refresh.

Source-compatible event ordering at equal selection/preparation/availability
edges must be explicit. Controller selection can reserve future command/data
edges: refresh must honor those commitments and cannot retroactively change an
already returned completion. Bank-preparation FRFCFS must consider actual rank
availability and the next legal wake instead of the old all-ranks-available
assumption. A delayed rank cannot create a zero-progress same-cycle loop.

Advance only within the caller's known exclusive arrival frontier. No refresh
operation at or beyond that frontier may commit if an unseen earlier arrival
could change the ordering. Admission receipts, WQ merge/read forwarding, direction
switching, physical completion, queue-credit release and frontend ACK semantics
remain separate and conserved. Coherent/early preparation traffic uses exactly
the same actual controller path and requires no alternate memory timeline.

An idle controller must not become an infinite background workload: skip known
idle recurrence spans arithmetically without crossing the next known arrival or
other intervening service; periodic timers alone do not keep causal replay alive
after all owned requests finish. Storage and work are bounded by ranks/live
requests and actual service events, not ROI cycles or the number of idle periods.
Preserve calendars across source chunks and the F1 warmup seam. Final detached
memory/protocol activity does not extend a finished core's local CPI/task clock.

Validate refresh parameters before mutation, including checked conversions,
nonzero/representable recurrence and supported configurations. If the reusable
calendar's existing overrun/phase/advance contract contradicts a supported source
case, report the exact contradiction before expanding its contract. Never remove
an error or clamp an unsupported time to manufacture progress. The projected
approximate controller interface is outside this slice: reject unsupported
refresh composition before ownership mutation rather than silently ignoring it.

Remove only the now-implemented causal refresh incompatibility after the actual
adapter exists. Frontend/DTLB and other gates remain. Do not remove the three
competing legacy selectors in this task; root will freeze that engine-selection
configuration independently, preserving all target mechanisms/resources.
Expose actual refresh counters/phase qualification through existing diagnostics
where needed, with ordinary/demand populations unchanged. Do not call constructed
calendar witnesses full FLAC, native equivalence, accuracy or throughput acceptance.

## Tests and acceptance

Write a real RED before implementing: a strict MixedDramController refresh case
must currently reject or fail its required timing, and an actual two-core
Simulator coherent+early+refresh composition must fail the existing causal gate.
Use existing maintained fixtures/targets, no duplicate controller or fake callback.

Cover refresh OFF exact compatibility; closed/open rank preparation and data
completion; row-hit invalidation; read/write/turnaround interactions; another
rank/channel progressing while a rank is unavailable; all blocked and legal wake;
equal event edges and exclusive-frontier partition invariance; checked invalid
parameters/rejection without ownership changes; idle long spans without periodic
iteration; source chunk/host-delay/warmup state continuity and detached-tail clock
freeze through actual Simulator. Keep native frontend admission/merge/forwarding
and ordinary/coherent/early regressions. Distinguish direct-controller witnesses
from actual Simulator witnesses in the report.

After source STOP: root source hash snapshot, one independent scoped Spec/Quality
review, canonical build/default plus affected domain tests, real CLI contract and
historical full FLAC OFF compatibility if changed shared paths warrant it.
No unchanged-input full-target retry is useful while known frontend/DTLB gates
remain. Record that explicitly and proceed to the next actual adapter. Full
four-case CPI/PMU/throughput validation remains the eventual goal, not a claim
made by completing this component.
