# Causal retired frontend and actual rename handshake

Prepared from the actual FLAC entry-v3 guard and bounded production-parser
preflight, following the approved P4/F2 plan. This is the next runtime slice
after F2b refresh gates close; it is not released for concurrent implementation.
It reuses SprFrontend, TraceSource frontend facts and CoreContinuation. It
neither claims to complete P4 nor enables an unimplemented instruction hierarchy.

## Required causal ownership

Keep retired functional facts as input. No producer timing reservations,
native hit/source/fusion/rename timestamps, wrong-path stream, replay or global
fixed-point. Reuse source_for/begin_macro/deliver_next/accepted from SprFrontend;
keep its current retired-path approximation explicitly qualified. Do not add a
second MITE/DSB/MS calendar or physical-register model as incidental scope.

Add default-OFF core.causal_retired_frontend requiring causal_runtime,
policy-bound static resource facts and the selected retired SPR frontend.
It enables only the implemented frontend/admission composition. Its absence
preserves existing causal and legacy behavior. Full profile remains unqualified.

Retain one bounded source macro/record cursor and copies of the actual current
FFE facts, their source ordinal, PC/ASID/code identity and completeness. Validate
exact source layout and required-known facts before model state changes. No
synthesized normal macro length, timing, decoded-cache outcome or fusion result.
A source batch/warmup pause is not macro end; partial retained macros never
become cacheable complete macros. Source graph parking from F1 still controls
what younger operations may execute, without preventing older ROB progress.

The frontend produces a delivery and retains its IDQ ownership until actual
modeled rename accepts that exact record/allocation slot cost. Backend pressure
must delay consumption; a functional dispatch floor alone is insufficient.
Extend CoreContinuation minimally with an actual rename admission/reservation
handshake/publication, respecting rename width and resources in flight. Source
backend checks for ROB/IQ/LQ/SQ and in-progress instructions must guide the
reservation edge: do not move allocation to a fabricated earlier time, charge
resources twice, or release reserved capacity before the native-defined edge.

Only actual rename acknowledgement calls SprFrontend::accepted(actual_cycle).
The imported rename_to_dispatch=2 edge is then enforced before dispatch/issue.
It must remain distinct from FU/issue occupancy and response completion. Existing
zero-elimination, SA/SD store pairing, IQ domains, RAW dependency versions,
ordinary/prepare ownership and retirement clocks keep their real identities.
No instruction-count denominator changes from frontend allocation accounting.
Physical-register availability remains its existing explicitly unsupported
mechanism; destination accounting is not promoted to a free-list model.

Publish the next legal frontend/backend wake to the existing safe coordinator.
A pending frontend delivery/rename reservation participates in progress and
source lower bounds; no future frontend side effect can be committed past an
unknown earlier publication. State/work stays bounded by active source buffers,
IDQ/backend capacity and live events; backend blockage does not cause per-cycle
retry loops or retain ROI-wide per-uop history. Producer speed/chunk size may
change host work but cannot alter modeled tuples or source populations.

Preserve actual configured byte-buffer geometry and refill precedence. In the
supported no-L1I fixture domain, use its configured refill rule. A configuration
requiring L1I/physical I-request ledger/lower hierarchy remains rejected until a
real instruction-request owner/response/coherence adapter exists; do not use
an ordinary data callback or a constant as fabricated instruction readiness.
The future I-side adapter will supply a typed actual byte-ready edge at this
interface. Do not silently change the inherited modeled I-address policy or
claim it proves native physical identity. DTLB and class/translation/replacement
and other unimplemented gates remain independent. Do not lift the full broad
guard because the frontend handshake works.

Model state survives the F1 warmup cut: pending macro, byte/DSB state, IDQ,
rename reservations, dependency and cache/request state are not drained/reset.
Source completeness and actual last retirement still govern each core's local
measurement origin/end; late frontend/protocol/background work cannot extend
finished-core CPI. Real CLI capability reporting distinguishes this implemented
subset, unknown measurement throughput and still missing full-target adapters.

## Acceptance

Before code, confirm the exact production interfaces/native rename reservation
edge in a bounded preflight; report a concrete contradiction rather than enlarge
the collector/schema or guess resource ownership. Write genuine REDs for backend
blocking actual IDQ consumption, rename width and 2-cycle dispatch edge.

Use actual Simulator + production frontend facts and backend, not a parallel
mock pipeline. Test MITE/DSB/MS and partial/pseudo qualification; IDQ pressure,
ROB/IQ/LQ/SQ blocking; rename width/slot costs, legal zero/SA-SD groups; delayed
responses; source park; warmup inside macro/reservation; unequal core origins,
true EOF, detached drain. Compare entire retained streams across chunks1/2/7/128
and host delays. Direct-component tests supplement actual Simulator witnesses.
No native branch outcome or speculative traffic enters any case.

Preserve OFF/default and existing resource/coherent/early/FS/clock regressions.
Source STOP, root frozen hashes, one scoped Spec/Quality review and canonical
checks precede acceptance. Full target entry is still not rerun while I-side/
DTLB/class adapters are known missing. Full FLAC/four-case qualification and
matched-reference CPI/PMU/throughput remain separate eventual gates.
