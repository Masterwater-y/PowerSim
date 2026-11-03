# Persistent Fetch response ledger (2026-08-21)

## Decision

FastSim now has a persistent, one-response-at-a-time Fetch ledger behind the
existing `core.fetch_supply_model` experiment.  The ledger survives trace
record boundaries and branch squash, and committed requests share its response
slot with the existing address-free speculative-request estimator.

The implementation is retained, but both timing switches remain disabled in
the production profile.  A native Stockfish C4 counterfactual shows that the
committed admission correction is much too small and that every modeled
anonymous response is hidden by branch recovery.  This rules out promoting the
current estimator or replacing it with a fixed wrong-path penalty.

## Runtime contract

The ledger stores only the last accepted request and its scheduled response
cycle.  A subsequent request cannot be accepted before the previous response
plus one cycle.  It does not create:

- wrong-path instructions, PCs, addresses, dependencies, or cache tags;
- architectural retirement or branch/cache/TLB PMU events;
- a fixed per-branch or per-request CPI charge.

Committed Fetch-block transitions reserve the slot only when
`core.fetch_supply_model=true`. The optional anonymous shadow requires
`branch.population_audit` and derives its UOP population, request density, and
mean response service from the same bounded committed-history window. It no
longer uses the hard frontend-width population or lifetime cumulative request
density. It can delay the correct path only when a scheduled response remains
outstanding beyond the existing recovery edge.

The rolling history stores only per-cycle counts and response-cycle sums. It
does not replay prior PCs or addresses. A history window with zero block
requests is a known zero-density observation; cold or missing UOP history
fails closed instead of borrowing older process or phase behavior.

The aggregate and per-core reports expose:

```text
fetch_response_ledger_committed_requests
fetch_response_ledger_shadow_requests
fetch_response_ledger_responses
fetch_response_ledger_server_wait_cycles
fetch_response_ledger_conserved
```

The conservation rule is:

```text
committed requests + shadow requests = scheduled responses
```

`server_wait_cycles` is separate from response service time and counts only
delay caused by an already occupied response slot.

## Directed validation

`fastsim_tests` covers three independent edges:

1. the default-off Fetch path has unchanged cycles and all ledger counters are
   zero;
2. committed requests serialize across separate trace records;
3. with a deliberately long response, a shadow response survives branch
   recovery and delays the next committed request without creating a
   wrong-path instruction.

The default build and full test executable pass:

```text
cmake --build build -- -j16
./build/fastsim_tests
```

## Native Stockfish C4 A/B (pre-history estimator)

The following A/B predates the bounded-history refinement and is retained as
rejection evidence for the old width/cumulative-density estimator. It uses the
same privilege-tagged FST, 10M measured user records per
core, `user-plus-kernel` scope, native-kernel profile, and gem5 reference
perf-like CPI `0.6591037258`.  Artifacts are under
`tmp/fetch-response-ledger-stockfish-c4-20260821/`.

| candidate | sum core cycles | perf-like CPI | absolute CPI error | delta cycles |
|---|---:|---:|---:|---:|
| production control | 10,258,538 | 0.5242552968 | 20.4594% | 0 |
| committed ledger | 10,269,157 | 0.5247979732 | 20.3770% | +10,619 |
| committed + anonymous response shadow | 10,269,157 | 0.5247979732 | 20.3770% | +10,619 |

The committed run schedules and conserves `1,879,932` requests/responses.  It
reports zero response-slot server wait: the existing committed Fetch timeline
already serializes these responses.  Source-aligned request admission adds
only 10,619 cycles and improves Stockfish by `0.0823` percentage point, so it
does not explain the P99 residual.

The shadow run estimates 102,628 anonymous requests and accepts 100,259 within
the modeled resolution windows.  Those requests produce 78,682 cycles of
response-slot wait, but their response wait is partitioned as 100,259 hidden
cycles and zero recovery-exposed cycles.  Consequently the shadow changes CPI
by exactly zero and leaves the complete FastSim PMU object identical to the
committed-ledger run.

The adjacent exact-window gem5 frontend oracle records 1,954,265 requests,
1,947,387 ordinary responses, 6,154 squashed responses, 4,058,880
I-cache-response-wait status cycles, and 2,301,666 summed
request-to-response cycles.  These overlapping counters prove an active
timed I-side, but they do not authorize adding their difference to CPI.  The
FastSim counterfactual above demonstrates that a resident one-cycle anonymous
response does not survive recovery in this workload.

## Consequence

This implementation closes the state-lifetime bug but rejects the proposed
timing explanation.  The next evidence must be collected per branch miss (or
in jointly keyed buckets), with at least:

1. requests started and accepted before resolution;
2. response outstanding at squash;
3. response still outstanding at correct-path recovery;
4. response/retry/MSHR or port release cycle;
5. the matching trace-only resolution, ROB-headroom, and committed request
   density features.

Calibration must freeze microarchitecture-level parameters on separate
workloads and core counts, then pass workload-held-out and topology-held-out
gates.  Until a nonzero recovery-surviving state is predicted with acceptable
held-out error, FastSim must report the unsupported component rather than add
wrong-path UOPs, increase the refill latency, or fit a residual CPI constant.
