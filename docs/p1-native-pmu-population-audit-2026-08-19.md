# P1 native PMU population and response-boundary audit

Date: 2026-08-19
Status: diagnostic evidence and implementation contract; not an accuracy report

This document starts P1 after the fail-closed P0 contract was activated. Its
question is narrower than “which cache latency should be tuned”: do gem5's
oracle and the future FastSim model count the same committed demand population
at the same request/response boundary?

## 1. Outcome

The current dominant blocker is the timing of cache-path observation, not a
remaining exactly-once accounting loss and not the corrected cross-core map.

1. P0 now conserves committed memory UOPs, touched lines, privilege scope, and
   dTLB outcomes. All four strict-gate cases have zero unaccounted, duplicate,
   rejected, and unknown-dTLB UOPs.
2. TaoTrace still freezes a fallback cache classification when the committed
   record is emitted. In the four C4 gates, **84.32%--91.54%** of those
   fallback UOPs receive a packet callback later. The later callback is
   correctly prevented from double-counting, but its outcome is too late to
   correct the already frozen path.
3. The process-global completion maps were a genuine identity bug because O3
   `ThreadID/InstSeqNum` is CPU-local. After making those maps instance-local,
   the four before/after merged oracle files are byte-identical. The fix is
   retained, but this A/B rules it out as the numerical source of the current
   four-case residual.
4. Existing raw O3/Ruby/MemCtrl stats are not a formal comparison population.
   Their reset/end windows and speculative/request semantics differ by up to
   19.58x in retired UOPs and 13.36x in L1 accesses in this short gate.
   Calling those ratios PMU error would be invalid.

This is not another “FS path went through SE ATOMIC” defect. These are full
system O3/Ruby runs, and the late callback counter proves that a real timing
packet path existed for most fallback-attributed UOPs. The defect is that the
oracle committed the proxy before that timing outcome arrived.

## 2. Reproducible evidence

### 2.1 Strict gate identity

The current gate is:

```text
tmp/p0-percore-c4-gate-20260819/
gem5.opt sha256 = 76e7d169f24b0c9ac5afaffa8b840e5a33bf3b6c567cddb832e50f608fc746bf
```

It collected zstd, Graph500, SPH, and NAMD at C4 and 100K measurement records
per core. The launcher exited zero. Its matrix audit reports 4 cases, 16 FST
files, 1,600,013 measurement records, 71,354,206 warmup records, complete
destination classes, and zero integrity errors. All request identities use the
binary hash above and final-config effective target.

The immediately preceding control is:

```text
tmp/p0-dtlb-c4-gate-20260819/
gem5.opt sha256 = 8f876034a766f465944f281530a4c135ffad681729e91927890cb5e3f8c2d282
```

The only relevant binary difference is that the candidate makes
`pending_shared_attr_` and `pending_cpl_data_class_` per TaoTrace instance.
For every workload, `diff -q` on `oracle/kernel_events.json` reports equality.
Thus CPI, every PMU scope, the coverage ledger, and late-packet counts are all
unchanged in the observed window.

### 2.2 Response arrives after fallback classification

The v3 ledger gives direct event-order evidence:

| Workload | Fallback-attributed UOPs | Later packet callbacks | Later/fallback |
|---|---:|---:|---:|
| zstd | 891,292 | 808,419 | 90.70% |
| Graph500 | 126,158 | 114,541 | 90.79% |
| SPH | 153,334 | 140,358 | 91.54% |
| NAMD | 76,821 | 64,774 | 84.32% |

`late_packets_after_fallback` is not double counting and is not itself an
accuracy metric. It is a causal timing diagnostic: the committed UOP was
classified with the line-state fallback before the eventual timing callback
became available. A cache hierarchy can remain perfectly conserved while its
hit/miss path is wrong; exactly-once and correctness are separate properties.

The source ordering agrees with the counters:

- `TaoTrace::accountCplCommit()` freezes the commit-time scope and chooses an
  already completed packet result or the fallback path;
- `TaoTrace::accumulateMicro()` emits the committed functional record;
- `TaoTrace::onDataAccessComplete()` may arrive later and increments the late
  counter instead of accounting the same UOP twice.

This behavior is correct for P0 conservation, but insufficient as a native
cache-outcome oracle.

### 2.3 Why raw gem5 stats cannot be used as accuracy labels

`tools/audit_p1_native_pmu_populations.py` is intentionally fail-closed. It
requires a formal v3 oracle, parses the raw O3/Ruby/MemCtrl populations, and
always emits:

```text
formal_comparable = false
accuracy_metrics_emitted = false
contract = diagnostic-only-no-ape-wape
```

The current artifact is
`tmp/p0-percore-c4-gate-20260819/audit/p1-native-populations.json`.
The following values are raw `native / TaoTrace` diagnostics, not APE/WAPE:

| Workload | Retired UOP | L1D access | L1D miss | private-L2 miss | LLC miss | dTLB miss |
|---|---:|---:|---:|---:|---:|---:|
| zstd | 1.262x | 1.183x | 1.177x | 1.148x | 1.090x | 3.289x |
| Graph500 | 1.440x | 1.364x | 1.634x | 1.625x | 1.095x | 5.643x |
| SPH | 19.577x | 13.360x | 1.253x | 1.557x | 1.458x | 5.250x |
| NAMD | 6.637x | 5.640x | 7.477x | 6.568x | 6.321x | 24.511x |

These differences are expected to be contaminated by five independent
population mismatches:

1. TaoTrace measurement starts at its functional serial marker; native stats
   reset at architectural `WORKBEGIN`.
2. Each TaoTrace core freezes at its own functional-record target; shared
   controllers and raw CPU stats continue until the final core exits.
3. TaoTrace counts committed memory UOPs and touched lines; Ruby stats include
   issued, replayed, squashed, and coalesced protocol requests according to the
   controller's increment site.
4. Ruby `m_demand_misses` is a protocol/controller event. It is not
   automatically equivalent to the project's committed L1/private-L2/LLC tag
   miss definition.
5. MemCtrl read/write bursts include page walks, writebacks, and protocol
   traffic, which cannot be relabeled as committed user-demand transactions.

The extreme SPH/NAMD retired-population ratios demonstrate the window problem;
they do not demonstrate a 19.58x or 6.64x FastSim PMU error.

## 3. Required gem5 oracle design

P1 must add an oracle-only request ledger. It must not change FST input and
must never feed gem5 timing answers into FastSim inference.

### 3.1 Stable identity and populations

Every committed memory UOP needs a CPU-qualified identity and zero or more
line fragments:

```text
(cpu_id, thread_id, inst_seq_num, measurement_epoch)
    -> (fragment_id, physical_line, operation, privilege_class)
```

The ledger must keep these populations distinct:

1. committed memory UOP;
2. touched-line intent;
3. actual request admitted by Ruby;
4. store-forwarded/no-cache completion;
5. secondary/coalesced request;
6. controller lookup and response outcome;
7. unique fill allocation/completion;
8. accepted DRAM transaction.

A single counter cannot represent two of these populations. Store forwarding,
split accesses, retries, squash, and merged misses each need an explicit
terminal state.

### 3.2 Measurement end and drain

When a core reaches its functional target, the oracle must:

1. stop accepting new committed UOPs into the measurement epoch;
2. freeze the cycle and retirement denominators at the existing boundary;
3. drain only outstanding response identities already admitted before that
   boundary;
4. finalize each admitted UOP/line as packet outcome, forwarding/no-request,
   rejected-with-reason, or unknown;
5. fail the formal gate if any outcome remains unknown.

Drain time must not be added to measurement cycles. This separates “wait for
the answer to an already counted event” from “extend the performance window.”

### 3.3 Native probe points

The first implementation should be diagnostic sideband only:

- carry the CPU-qualified request identity on gem5 `Request`/`Packet` without
  modifying cache behavior;
- observe `Sequencer::hitCallback()` fields including `externalHit` and
  `was_coalesced` as Sequencer-visible response facts;
- keep their names protocol-specific until source and conservation tests prove
  whether they correspond to a target L1 tag lookup, request miss, or another
  event; do not prematurely name `externalHit` an LLC miss;
- emit per-scope confusion matrices between the current fallback proxy and the
  native response fact after both are complete.

Deeper private-L2, shared-LLC, merge, fill, and DRAM events need explicit
controller/TBE/MemCtrl probes with the same request identity. Inferring them
solely from `respondingMach` is unsafe: MESI Three Level callbacks can use
default or `MachineType_NUM`, and a responder identity is not a tag lookup,
unique fill, or DRAM-accept event.

## 4. FastSim implementation after oracle closure

Only after the same-window native ledger passes should FastSim change its
cache model:

1. model request-time transient state separately from response-time fill
   visibility;
2. let secondary requests count demand lookup/miss while sharing one unique
   fill with the parent;
3. update replacement state at gem5-equivalent lookup/fill moments;
4. maintain the same
   `request -> lookup -> coherence -> LLC -> DRAM -> fill -> waiter` ledger;
5. score confusion matrices offline, while production inference uses only FST
   addresses, operation types, ordering metadata, and configured state;
6. preserve a no-oracle-input gate that rejects runtime use of native path
   labels.

This sequencing prevents two invalid shortcuts: tuning latency against a
misclassified event population, and copying gem5 answers into the deployed
trace format.

## 5. Acceptance gates

The native sideband can replace the current TaoTrace path proxy only when all
of the following pass on at least LBM, zstd, Graph500, NAMD, and Stockfish:

```text
committed UOP terminal states sum to committed memory UOPs
touched-line terminal states sum to line requests
admitted request = response + coalesced/secondary + explicit terminal reason
unique fill = completed fill + explicit outstanding-at-finalization error
unknown outcomes = 0
same start marker and per-core end identity are proven
observer enabled/disabled CPI and native stats are identical
no native response label is present in FST or FastSim runtime inputs
```

Then report user and user+kernel separately, and compare only event-dictionary
entries whose increment sites have reached `strict`. Until then, raw ratios
and fallback/native confusion are diagnostic; no cache PMU APE/WAPE is formal.

## 6. Immediate implementation order

1. Add CPU-qualified request identity and an oracle-only
   `Sequencer::hitCallback()` diagnostic sideband.
2. Add the target-stop/drain finalization state and fail on unknown admitted
   outcomes.
3. Produce same-window per-core/aggregate confusion matrices for fallback
   versus native response facts.
4. Add controller/TBE identity propagation for private-L2, LLC, merged miss,
   and unique fill.
5. Add MemCtrl accepted-transaction identity and traffic-class separation.
6. Only then modify or promote FastSim transient/fill/default cache models.

The first three steps were completed by the lifecycle v2 implementation. The
controller/TBE work then produced the v4 value-identity observer, v5 added
the missing exact hierarchy-enqueue population, and v6 preserves only the
finite preboundary in-flight/ROB ledger. Current implementation and
gate evidence are recorded in
`docs/p2-native-ruby-pmu-contract-2026-08-19.md`.

Two implementation findings changed the original plan:

1. carrying a `RequestPtr` into later SLICC actions is unsafe because the
   Sequencer can already have released the underlying raw `PacketPtr`; SLICC
   now carries only captured `(ContextID, InstSeqNum)` values;
2. `RequestStatus_Aliased` does not imply that no later cache lookup occurs,
   so v5+ count `Sequencer::issueRequest()` mandatory-queue enqueues directly
   instead of estimating them as `admissions - aliases`;
3. v5 still lost SLICC outcomes for a few requests that entered Ruby before
   measurement and retired afterward; v6 pretracks warmup admissions and
   erases every premeasurement-retired UOP so only boundary-crossing state is
   retained.

The remaining task is no longer “obtain any Ruby response.” It is to use the
conserved v6 gem5-internal populations as offline labels, quantify the current
FastSim cache-state mismatch, and update FastSim without serializing those
labels into FST.
