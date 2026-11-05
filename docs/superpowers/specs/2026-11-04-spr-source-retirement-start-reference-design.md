# SPR source-retirement-start reference: first release

2026-11-04. Design before code; an independent oracle-only addition to the
approved mechanism plan. This is not the current architectural-bundle task.
No implementation is released by this document. Original source windows,
ledgers, native counters and reference interpretation stay unchanged.

## Release domain and decision

Release `source-retirement-start-v1` only for a nonzero full-source warmup
prefix N>0, fixed-period clock and the qualified exact-kernel task-clock
profile. The existing four C4 sources all have nonzero prefixes. N=0 is
explicitly unsupported in this release; do not add generic Linux root/GS/task
introspection to obtain it. A later release needs the independent B task,
phase and counter-bridge proofs described in the implementation document.
This does not relax the physical-baseline or initial-TLB requirements of the
functional runtime: task-clock evidence and architectural state are different.

A new native series uses the exact successful retained member N-1 as its
start and the original final `retirement_stop` as its end. Keep the original
common-start series and common elapsed separately. Changing the reference
cannot count as model improvement. Do not trim source, round a macro or SA/SD
boundary, add a barrier/drain, reset model state or import native time into
the model. The model must retire the same predecessor independently while
preserving all live core/cache/protocol/store/walker state.

## Native observation protocol

Add a default-OFF `--source-retirement-start-reference` collection option and
separate TaoTrace parameter. Require task-clock/thread-state, exact kernel
profile, full raw retirement/kernel stream and continuous functional warmup.
It must work with MMU controls OFF. Original callbacks and call order remain.
OFF performs no new sampling, registration, selection or output.

Keep one pending retained member, one rolling sealed member and one selected
start per core. Bind only after successful hot write, using original zero-based
ordinal, finalized dependency record hash, source generation and private
owning-instruction identity. No native sequence/pointer becomes a model fact.
After unchanged after-record lifecycle handling, observe the existing native
marker plus eligibility/transition/native-state/notification/clock-phase
witness. Independently listen to original CommitMiscRegsApplied to match and
seal this owner. Require the candidate and seal counter/eligibility/state/
sequence/phase to agree; otherwise new-view readiness is false with a named
reason. The numeric sample remains the after-record marker, never a later
post-update replacement. Failed, filtered, collapsed/multiple or unmatched
emissions remain unsupported for this view.

Original BEGIN selects exactly count N / ordinal N-1. A pending selected
candidate may seal later through the unchanged callback; no simulation delay,
extra event or new timestamp is permitted. Missing/overwritten/duplicate
identity and unresolved seal fail closed. Original final-retirement endpoint
also needs exact successful member/hash and later seal. Source close may occur
inside the final write; keep only the bounded selected metadata alive through
existing callbacks and finalization. Never extend the endpoint to common END,
detached response or drain completion.

Do not bootstrap, begin, snapshot, reject or rebase the old native ledger for
this new view. Use an independent observer/error set. Original raw updates and
account snapshots can precede BEGIN, while original derived intervals/halt
flags are BEGIN-gated. Thus subtraction of old totals or clipping only old
intervals is insufficient. Build the new interval from continuous raw updates
and exact lifecycle positions, including starts inside RLE and pending task
transitions. No per-instruction timestamp stream is added.

## Clock and accounting qualification

Observe oracle-only tick/current cycle/next edge/period at boundaries. Require
a fixed-period affine phase, next-edge minus tick in [0,period), complete
counter notifications and no unaccounted reset/frequency change. Do not guess
ceil/floor from configured frequency. Original CPU counter updates are never
forced. Added lazy clock reads require OFF/ON neutrality qualification.

Independently prove source/member/footer/task identities, initial eligibility
and lifecycle coverage across the selected interval. Reuse the maintained
first-cut proof when applicable; positive PID, unknown wake cause or zero
unknown cycles alone is not proof. Inspect pre-BEGIN Sleep/Wakeup/halt evidence
independently; preserve the unsupported active-task halt rule.

For each core/task, require nonnegative checked conserved categories:

`active + other + transition + unknown = independent active-counter delta`

`active-counter delta + Sleep excluded + Wakeup excluded = native cycle span`.

An unfinished but matched switch is a known transition, not zero time. Missing
notification/owner/phase or unbound crossing leaves readiness false. Freeze
all categories at local final retained retirement. Common elapsed is unchanged
and cannot be reconstructed as maximum or sum of local intervals.

Original memory/PMU observers open at original BEGIN. This reference creates
no newly anchored cache/PMU truth and proves no request-crossing completeness.
Warmup in-flight requests are preserved; no drain manufactures a clean cut.

## Artifacts and maintained consumers

Write a separate `oracle/task-clock-source-retirement-start-core<N>.json`,
schema `gem5-task-clock-source-retirement-start-v1`, `oracle_only: true`.
Freeze exact JSON types/required keys before EDITSTART. Include both policy
names, core/context/source generation, N and start/end ordinals/hashes, full
source/footer identity, kernel/profile/task set, original native ledger SHA,
original BEGIN/END association, bounded candidate/seal/selection/clock/native
witnesses, readiness statuses and named errors. N=0 yields explicit unsupported
status, never first-measured-retirement or old-BEGIN fallback. Native timing
stays out of hot/core/MMU/thread-state portable plans.

Extend maintained task-clock auditing through an explicit separate entry/path
using shared counter validation. Preserve old entries/files. The native
validator and comparison pipeline must explicitly match BOTH active-start and
active-end policies plus source/core/task/population scope. Do not pool series,
fallback across them or silently replace original reference selection.
Model boundary publication is a separate later runtime gate; native-reference
availability alone does not permit a score. Future scores require CPI absolute
error, relative error and aggregate MAE under the reporting contract.

## Required evidence before release

TDD must cover successful/failed write, pending BEGIN seal, delayed/duplicate/
changed post-update seal, final close before callback, RLE interior, same-cycle
lifecycle order, transition across cuts, Sleep/Wakeup before BEGIN, bad phase/
frequency/overflow, source/hash/footer mismatch, partial macro/SA-SD and N=0
rejection. Example BEGIN100/predecessor60/end200 retains old span100 and new
span140 as different reference series, never an accuracy gain.

Qualify real native OFF/ON on identical original checkpoint: hot/functional
populations and bytes, original reference/ledger contents, counters, common
start/end/elapsed and native statistics unchanged. Exercise both serial and
instruction BEGIN paths and actual deferred-effect changes. Helper tests are
not native neutrality. Integrate this evidence into the necessary bundle
before the next full four-case recapture; do not recapture to discover that
anchors were never emitted. Full FS runtime, functional baseline/writer
coverage, DMA/coherence, mechanism accuracy and throughput remain separate.
