# LBM cross-Q service integration design

User approval: the preceding conversation specified pending shared service/fill
ownership, retained core state, actual store send and response-owned SQ release;
the user then requested implementation and validation. This is that approved
architecture, not a new simulator or permission to change the default.

## Scope and invariants

Keep interval_weave/time_epoch, Q=1024, parallel producers and core feedback,
ordinary_load_latency=3 and load_response_to_ready=1. No global per-UOP
causal_read replacement, unconditional third whole-prefix feedback, fitted
latency, workload/PC special case, gem5 timing input or new trace collection.

Each submitted request owns a stable identity independent of resident vector
indices. A response is either pending or a selected immutable absolute time;
zero/maximum/old latency are not substitutes for an unknown response. Shared
cache, directory, dirty writeback, fill, queue and PMU effects publish together
and occur exactly once. Store line acquisition may be a DRAM read; architectural
stores are not automatically DRAM writes.

An ordinary store can retire before its service is selected. Its detached
record retains SQ/TSO ownership, address/data readiness, actual send and all
fragment responses. SQ releases once, at the last required response. AGU does
not reserve the same request resources again before actual send.

Core live fragments retain independent discovery, execution and retirement
progress. Unknown responses block their dependants and capacity owners but
do not suppress younger independent requests. Producer coverage H, shared
exclusive selection frontier F and core retirement are separate. All exact
arrivals below F and all source lower bounds must be represented; selected
responses can legitimately exceed H. Empty-UOP epochs still advance retained
services. Common-end accounting is not extended by background drain.

## Integration and rollout

The existing MixedDramController supplies persistent typed RD/WB service
selection. Shared access separates functional admission/submission from
response/fill publication. Core-local retained state or bounded live-fragment
replay consumes service identities. Fully closed fragments can keep the
existing numeric feedback kernel; open fragments must preserve the same RAW,
StoreSet, resources, WB and retirement rules.

Expose an off-by-default cross-Q experiment with controller-only,
admission-only and combined variants so mechanism effects remain separable.
Unsupported overlapping experiments fail explicitly rather than bypass guards.
No promotion while the known critical LBM requests still bypass the path.

## Required evidence

First, a two-core fixture with competing requests, a dirty LLC victim, a late
cross-Q store and a younger non-RAW SQ-blocked store must fail under the old
path. Verify response/fill visibility, SQ ownership, split last-response
release, exactly-once rollback, empty-epoch progress and generic/materialized
equivalence. Supplied exact arrival streams retain controller partition
invariance; this is distinct from full-core Q sensitivity.

Then replay the existing full LBM C32 input with bounded audit output for C13
ordinals 3127618..3137618 and C1 2M/8M. Check coverage of the known 315 DRAM
stores and 430 SQ owners, identities, actual admission, services and stage
conservation before interpreting CPI. Run the three interventions, then LBM
C4/C8/C16/C32, then 40 cases only if the narrower gates pass. Throughput is
measured separately with audits off. Accuracy reports include absolute CPI
error and CPI MAE under the current common-end user-plus-kernel contract.

Work remains in the managed FastSim work tree with a task-entry snapshot;
preserve all previous dirty changes. No commit/push/default promotion or
artifact deletion is requested.
