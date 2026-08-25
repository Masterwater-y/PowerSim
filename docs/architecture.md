# Architecture

The next-generation time-epoch and conflict-certificate design is specified
in [Conflict-Certified Sparse Weave](conflict-certified-sparse-weave.md).

## Design objective

FastSim targets high-throughput, deterministic multicore PMU replay from gem5
functional traces. It does not predict cache or branch outcomes with a model:
those are derived from explicit configurable state. A separate lightweight
penalty layer estimates time without attempting cycle-accurate pipeline
simulation.

```text
gem5 functional trace/core
          │
          ▼
per-core decode + branch replay workers (parallel, bounded chunks)
          │
          ▼
known-next-event set for every active core
          │
          ▼
deterministic causal-frontier min-heap
          │
          ▼
private L1D/L2 → directory + home CHA → shared LLC → DRAM
          │
          └──────── exposed latency advances only the issuing core
```

The current implementation separates software and hardware ownership before
adding a scheduler:

```text
ThreadState                         HardwareCoreState
thread/address-space identity       predictor + interval/OoO state
functional TraceSource       ───▶   static resident-thread binding
thread lifecycle                    private TLB/cache resources (core indexed)
```

Stage 1 accepts `1 <= threads <= cores` and an injective static mapping. There
is one trace producer per active thread; unbound cores are idle. No time slice,
migration, context-switch cost, oversubscription, block/wakeup, or host-thread
count is interpreted as a target-machine parameter. The explicit
`ThreadTraceBinding` boundary retains those extension points for the later
scheduler.

## Causal-frontier streaming

Each statically bound software thread owns a permanent producer thread. A
producer sequentially decodes its trace, replays its resident hardware core's
private branch predictor, and emits bounded
chunks containing:

- retired/UOP/branch counters;
- non-memory and branch-penalty time deltas;
- ordered physical cache-line events;
- an end-of-chunk tail delta.

Chunks enter a bounded per-core queue. The shared-state coordinator maintains
exactly one next memory candidate for every active core in a min-heap ordered
by `(simulated issue time, core ID, per-core ordinal)`.

The key invariant is:

> The coordinator may commit an event only while the next uncommitted memory
> event of every active core is resident and known.

After committing a core's event, the coordinator computes its next candidate.
If that core crosses a chunk boundary, the coordinator consumes zero-memory
chunks and waits for its producer if necessary before committing a later
event from any other core.

This makes the heap minimum conservative **relative to FastSim's own modeled
issue times**. Future modeled events on a core cannot precede its resident
next event because all per-core deltas and returned memory penalties are
non-negative. It does not prove that this order matches gem5 or hardware: an
incorrect core timing model can reverse events from different cores before
they reach the heap. The mechanism does fix the narrower
fixed-instruction-epoch failure where a slow core's far-future event is
processed before a fast core's next-chunk event with an earlier timestamp.
`test_causal_frontier_skew` is a regression for exactly that case.

Host lookahead (`sim.lookahead_chunks`) affects producer/consumer overlap and
memory use only. It cannot change simulated ordering or PMUs.

## Shared-state commit path

At each globally selected memory event, the coordinator performs:

1. L1D and private L2 tag/replacement update.
2. Private-L2 victim writeback and directory removal.
3. Directory lookup and optional ownership upgrade/invalidation.
4. Home-CHA selection using gem5-compatible low cache-line bits or an
   optional XOR-folded mapping.
5. LLC tag lookup for demand fills. Permission-only upgrades do not fabricate
   an LLC demand lookup.
6. Deterministic DRAM channel/bank/open-row service and queueing. LLC dirty
   victims are buffered by default in an independent per-channel write queue:
   demand reads keep priority, the high watermark requests a minimum write
   burst, and physical-capacity pressure drains through the low-watermark
   hysteresis. This path never changes architectural store/SQ completion
   directly; the legacy immediate-write path remains available as an
   explicit differential-validation override.
7. Return-latency exposure to that core's next-event time.

Inclusive LLC invalidation is safe in this design because private cache state
is updated only at causal commit time; there is no uncommitted private-cache
preview to repair.

## Branch path

Branch replay runs independently per core because it has no cross-core state.
The implementation supports configurable Tournament or gshare direction
prediction plus set-associative BTB, causal RAS learning, and the configured
indirect target predictor. It consumes only committed functional
direction/successor data.

An optional `branch.speculative_history` mode separates Fetch-time history
updates from retire-time predictor-table training. It checkpoints and repairs
recoverable global/local history but still consumes no wrong-path records. The
mode is an explicit diagnostic candidate, not a production default: the
2026-08-24 same-oracle 40-case ablation slightly raised the miss count; see
`branch-speculative-history-checkpoint-2026-08-24.md`.

Formal branch validation requires `taotrace-retired-bpred-v1`: gem5 preserves
the original Decode/IEW redirect outcome until the responsible control
instruction retires. The former retirement-time `DynInst::mispredicted()`
comparison lost direct-target misses after Decode repaired `predPC`; its
reported MAPE/P99 is superseded by
`branch-miss-oracle-repair-2026-08-24.md`.

With the gem5 v28.1 Tournament/BTB/RAS/indirect geometry, four workload
comparisons produced 0.08%–0.94% branch-miss error. A trace without committed
successors produces no branch PMU claim.

## Relation to Zsim

Zsim's bound-weave algorithm advances cores independently for a short
simulated-time interval using zero-load memory latencies, records lower-bound
timestamps and memory-hierarchy paths, then weaves the recorded events through
a contention event graph. Contention feedback delays subsequent core clocks;
path-altering interference is audited and the interval can be shortened when
needed. Its core model is also instruction-driven and tracks dependencies,
ports, ROB occupancy, and pipeline clocks rather than charging one scalar cost
per UOP. See the [original Zsim paper](https://people.csail.mit.edu/sanchez/papers/2013.zsim.isca.pdf).

The scalar and `interval_bound` baselines have none of that reconciliation:
they commit every memory event immediately. The experimental
`interval_weave` path now implements global-time accepted prefixes and one
gather/sort per step, but it audits rather than repairs path-changing order
conflicts. It is therefore a staging point, not completed Zsim-style
reconciliation.

## Relation to TCSim

TCSim learns timing/progress from a large neural model and can represent O3
effects that the current FastSim penalty layer cannot. Its costs are model
forward throughput, checkpoint/training dependence, and workload/uarch
generalization risk.

FastSim instead makes cache, directory, CHA, DRAM, and branch state explicit:

- geometry changes do not require retraining;
- PMU causes are inspectable and deterministic;
- the C32 validation trace ran at 51.55 million UOP/s, while TCSim's local
  reports describe roughly tens of thousands of UOP/s for comparable
  single-trace ML rollout paths;
- the scalar timing path lacks dependency/ROB/MLP modeling; `interval_weave`
  adds a lower-bound dependency/ROB/resource model and approximate MSHR
  feedback, but it is not a cycle-by-cycle OoO pipeline and does not yet repair
  memory-order conflicts.

On the same 23-workload seed0 corpus, scalar FastSim's mean absolute UOP-CPI
error is 33.858%/30.703% at C4/C8, and interval-weave reaches
11.853%/14.990%. The local TCSim v29 report gives 5.07%/4.62%. TCSim advances
K=256-UOP per-core candidate windows, chooses a global virtual-time step, and
consumes each core's accepted prefix; interval-weave adopts that scheduling
contract but not TCSim's learned timing model. FastSim's state path supplies
inspectable aggregate PMUs, but it is not an "exact backbone": event order
and protocol-level coherence have not been validated.

## Experimental interval bound

Binary trace v6 retains `op_class`, `n_src/n_dst`, four
producer-distance/class pairs, per-class architectural destination counts, and
a virtual-page token alongside the physical address. The optional
`core.model=interval_bound` path
uses them in a stateful 256-UOP lower-bound model with 8-wide
dispatch/issue/commit, a 192-entry ROB, 64-entry IQ, 32-entry load/store
queues, operation latencies, shared FU capacity, in-order retirement, and
branch recovery.

This is deliberately only the core-side first stage. It still emits memory
events into the old event-at-a-time coordinator, preserves per-core memory
program order, and adds returned miss latency as serialized clock skew. Thus
it does not yet implement the global-time accepted-prefix and batch-weave
protocol below.

On all 23 seed0 workloads, it changes mean absolute UOP-CPI error from
33.858% to 23.682% at C4 and from 30.703% to 20.180% at C8. Integer ALU and FP
ALU errors fall below 0.2%, while random-memory MLP remains 74%--83% high.
This isolates the next problem: shared-memory overlap/reconciliation, not
another scalar core-width adjustment.

## Experimental global-time weave

`core.model=interval_weave` keeps the same OoO lower-bound core and changes
the coordinator:

1. Every active core exposes a resident 256-UOP candidate window.
2. The minimum per-core candidate, capped by 1024 cycles, advances one global
   virtual time.
3. Each core contributes the longest retirement prefix covered by that
   horizon; progress is variable rather than a fixed instruction epoch.
4. Accepted memory events are gathered and sorted by lower-bound issue time,
   core, and program-order ordinal, then replayed as one batch.
5. Returned latency propagates through in-window producer edges and in-order
   retirement; clock skew is applied once per accepted prefix.
6. Lower-bound order is compared with feedback-adjusted order, with global
   and same-cache-line inversion pairs reported.

The implementation also converts DDR4-2400 timings from memory-clock to
3 GHz core-cycle units. On the complete seed0 corpus, mean UOP-CPI error is
11.853% at C4 and 14.990% at C8; P90 is 19.098%/33.666%. Aggregate cache and
branch PMU WAPE remains below 0.27% in the validated scopes.

This fixes the fixed-epoch/one-event-coordination structure, but not the full
causality problem. It observed 127,426 C4 and 153,761 C8 same-line feedback
reorder pairs. Because affected cache/directory paths are not yet rolled back
and rewoven, those are explicit failed certificates rather than evidence of
correct coherence order.

## Configuration surface

The key-value configuration controls:

- simulated core count, producer chunk size, resident lookahead, interval
  target UOPs, and maximum global-time step;
- fetch/decode/rename/dispatch/issue/writeback/commit width and stage delays,
  fetch queue, ROB/IQ/LQ/SQ, load/store ports, FU count/latency/pipelining,
  L1D/L2/LLC outstanding misses, response-driven memory-IQ feedback, and
  memory-latency exposure;
- a 64-entry-compatible fully-associative DTLB with configurable lookup,
  page-walk latency/concurrency, and independently selectable same-page miss
  coalescing; the captured gem5 x86 profile uses one active timing walk,
  queues followers, and disables coalescing;
- L1D/L2/LLC size, associativity, line size, hit latency, replacement policy,
  LLC inclusion, per-core Ruby Sequencer capacity, and controller TBE-like
  miss capacity;
- coherence enable, CHA count/mapping, NoC latency, and CHA service time;
- Tournament/gshare tables, counter widths, BTB, RAS, indirect predictor, and
  mispredict penalty;
- DRAM capacity, channels, banks/channel, row size, CL/RCD/RP, burst time,
  independent read/write buffer geometry, write-drain watermarks, and
  minimum read/write turnaround bursts.

Every statistics file embeds the effective configuration.
The exact gem5-to-FastSim map, including unsupported parameters whose inputs
are not identifiable from a committed functional trace, is in
[gem5 parameter coverage](gem5-parameter-coverage.md).
The source-audited correction architecture and P99 gate are in
[the gem5 source-aligned plan](gem5-source-aligned-p99-plan.md).

## Accuracy boundary and next technical step

The aggregate-counter PMU path is the accepted component within the scopes
listed in the validation report. Scalar timing omits instruction dependencies,
functional-unit occupancy, ROB pressure, realistic load/store MLP, L1I, and
TLBs. Interval-bound adds dependencies/FUs/ROB; interval-weave adds
global-time batch feedback. Full-suite errors are 33.858%/30.703%,
23.682%/20.180%, and 11.853%/14.990%, respectively. All remain failed against
the stated accuracy gate.

The next design must operate at interval granularity:

1. Each core builds a bounded instruction/UOP window (initially K=256, as in
   TCSim) with dependency/operation-class timing, ROB/resource limits, and
   multiple outstanding memory operations.
2. **Implemented experimentally:** a global virtual-time step accepts a
   variable prefix from every core instead of serially finalizing one memory
   event at a time.
3. **Partially implemented:** shared memory is gathered and woven in batches;
   private preview and transactional path repair are not implemented.
4. Same-line races, evictions that alter recorded paths, and coherence
   ownership changes are audited; the interval shrinks or replays when the
   audit fails.
5. No accuracy claim is made until C4/C8 CPI is competitive with the TCSim
   reference and event-order/coherence-message metrics are validated directly.

The concrete execution protocol, conflict audit, and acceptance gates are in
[the interval redesign](interval-redesign.md).
