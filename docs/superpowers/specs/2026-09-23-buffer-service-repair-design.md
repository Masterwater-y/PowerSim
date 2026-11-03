# Buffer TSO bounded service repair

The user requires a fallback and prioritizes host throughput. The previous
private-read service guards reject requests based on unrelated cores' entry
and exit bounds; copied alias latency also becomes stale after capacity stalls.

## Decision

Implement an opt-in transactional reference path, `ruby.buffer_service_repair`.
Replay memory requests in feedback-corrected order, without an additional
per-core monotonic clamp. Each pass chooses a new service or joins an outstanding
read service before probing caches. The same choice controls cache/PMU effects,
response latency, and L1/L2 MSHR consumption; followers still consume CPU request
slots. Include L1-hit parents. Commit only when all replay arrival timestamps
equal the timestamps computed using that pass's responses and resource gates.
An epoch boundary does not clear successful outstanding read services.

Use the existing hierarchy transaction to restore cache sets, directory,
shared queues and PMU, plus explicit private counters and line-table snapshots.
On failure restore the entire trial and execute the existing baseline once.
No post-hoc PMU subtraction. Failed epochs clear retained read identities and
report this approximation; they must never be described as fully modeled MSHRs.

## Work bound and rollout

Default off. Limit each trial's sum of accepted UOPs and memory events and its
pass count with configuration. Count attempted work, successful epochs/events,
budget fallbacks, horizon fallbacks, conflicting writes and discarded services.
This first implementation intentionally uses bounded whole-epoch replay to
establish transactional correctness. It is **not** the eventual sparse repair
engine and must not be enabled by default based on mechanism tests alone.
The work bound is an operation bound, not a guarantee about elapsed throughput.
`ruby.buffer_service_max_work=4096` caps both one epoch's total replay work and
the saved credit bank; `ruby.buffer_service_max_passes=3` caps attempts.
`ruby.buffer_service_work_percent=10` earns one extra replay work unit per ten
baseline UOP+memory-event units. Preflight spends one unit per memory event;
trials, including failures, spend one unit per replayed UOP and memory event.
These units count visits, not cache associativity or per-core snapshot bytes.
Zero maximum work or zero percentage is a baseline-only evaluation option.

The alternative of unconditionally replaying every epoch has unacceptable cost;
timing-only alias repair cannot make PMU consistent. Sparse dependency repair
requires ROB/LQ/SQ/MSHR/cache-set dependency closure not currently available.
Do not disguise the bounded reference implementation as that completed design.

## Semantic limits

The candidate repairs read-service reuse within the existing two-stage timing
model. It retains that model's request origins and immediate tag allocation.
Writes are not merged: any write/atomic overlapping an outstanding service on
the same line forces transactional fallback. Unrelated stores remain eligible.
Page-walk/fault and instruction-fetch events use baseline fallback. No read-only-epoch,
same-cache-set, first-miss, or unrelated-core entry-floor guards are added.
Arrivals beyond the epoch horizon fall back to avoid mutating future cache state.
Current cache/coherence approximations remain; this is not a Ruby replacement.
Both read-before-write and write-before-read overlap on one line fall back;
unrelated stores do not trigger this condition. Actual late memory-issue FU
collisions fall back because the existing feedback code has already reserved
CPU/MSHR slots before moving the issue. The follow-up
[FR-FCFS transaction](../plans/2026-09-23-buffer-service-frfcfs.md) now runs the
retimer inside the transaction and certifies parent responses as well as arrivals.
Controller failures still fall back. The remaining guards are unsupported mechanisms,
not evidence that their requests cross epoch boundaries.

## Validation

Directed tests cover shared response plus one PMU lookup, CPU slots versus
MSHR slots, capacity-delayed reclassification, L1-hit parent, corrected reorder,
cross-epoch state, conflicting write rollback, zero-budget exact baseline and
bounded attempts. Run the complete C++ test executable. Real workload native
validation and ABBA throughput must distinguish this experimental candidate
from the production default; low coverage is a failed coverage gate, not success.

## Production architecture still required

This reference path is a transaction correctness testbed, not completion of
the user's full throughput/coverage objective. The production replacement
needs the following coupled changes:

1. A decision record owns `(core, line, generation, permission stage)`, its
   response version, CPU-request slot, controller/MSHR slot and cache/PMU
   effects. Followers reference that response version; they do not own a
   copied service duration. Count a cache lookup only when that decision
   actually issues it.
2. Record changed memory admissions during the existing feedback scan. Use
   reverse dependencies and compact core checkpoints to re-evaluate only
   affected consumers and resource contenders. Include non-winning contenders:
   moving an old winner earlier can expose a previously losing gate.
3. The repair closure includes cache sets, coherence ownership, CPU slots and
   DRAM channels. FR-FCFS arbitration and shared-fill merging must produce
   the same transaction's response versions. They cannot retime parents after
   service references and PMU decisions have committed.
4. Charge discovery and rejected work as well as successful replay. Fall back
   an entire closed domain, escalating to epoch only when dependency closure
   cannot isolate it. Expose lost identities as approximation, never silent
   exactness. The current `discarded_services` counts stored table entries,
   including entries not yet lazily expired; it is not an exact count of lost
   hardware transactions.

Increasing the replay budget, removing arrival checks or treating a whole
FR-FCFS epoch as FCFS is not a substitute for those changes.
