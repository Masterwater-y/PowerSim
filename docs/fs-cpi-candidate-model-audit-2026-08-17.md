# FastSim FS CPI candidate-model audit (2026-08-17)

## Scope and invariant input

This audit evaluates implemented, default-off timing candidates against the
accepted post-DTLB FS baseline.  Every formal comparison uses the same FST v7
dataset, functional warmup, 10M measurement records per core, gem5 oracle,
user-UOP denominator, and runtime provenance:

```text
dataset: tmp/taotrace-fst-v7-c4-c8-formal-v4-destclass-20260816/fst-v7
baseline: tmp/fs-cpi-dtlb-tw12-fetch1-final-20260817/summary.json
DTLB: timing_walk, one walker, no coalescing, 12-cycle service
fetch-buffer refill: 1 cycle
cases: 10 workloads at C4 plus the same 10 workloads at held-out C8
scopes: user and user-plus-kernel
```

No candidate uses a workload ID, per-case coefficient, gem5 timing label, or
online PMU oracle.  Candidate reports and derived configs are under
`tmp/residual-uarch-audit-20260817/`.

## Captured gem5 facts

The formal C4 Stockfish `config.ini` is representative of all target-identical
cases in this matrix.

- `ruby_system.l1_controllers*.Icache` and `Dcache` are 32 KiB, 8-way LRU.
- `ruby_system.l2_controllers*.cache` is 1 MiB, 8-way `TreePLRURP` with eight
  leaves.  This is FastSim's private `cache.l2`.
- Each `ruby_system.l3_controllers*.L2cache` slice is 8 MiB, 16-way
  `TreePLRURP` with sixteen leaves.  Eight slices form FastSim's 64 MiB LLC.
- The DRAM interface reports `tRAS=32 ns`, `tRTP=7.5 ns`, `tRRD=3.332 ns`,
  `tRRD_L=4.9 ns`, `tXAW=21 ns`, `activation_limit=4`, `tCCD_L=5 ns`, and
  `tCS=1.666 ns`.  At the 3 GHz target clock, the implemented compact-calendar
  values are 96/23/11/15/64/4/16/5 cycles.
- gem5 has a 64-entry read queue, 128-entry write queue, 85%/50% write-drain
  thresholds, minimum 16 reads/writes per turn, and open-adaptive page policy.

These facts do not make every FastSim switch source-equivalent.  Cache
replacement is a direct state-machine mapping.  In contrast, the optional
DRAM command constraints currently run on FastSim's reconstructed controller
arrival order and omit parts of the DDR protocol.  The committed-PC L1I sees
only retired PCs, while gem5 also sees wrong-path, refetch, and kernel fetch
requests.

## Formal dual-scope result

The isolated candidates are:

- `TreePLRU`: only private-L2 and LLC replacement change from LRU to TreePLRU;
- `L1I`: only committed-PC L1I capacity/miss state is enabled;
- `DRAMcmd`: only the eight captured optional command constraints are enabled;
- `DirectAll`: all three changes above are composed.

| Model | User mean APE | Delta | User P90 / max | User wins/losses/equal | User+kernel mean APE | Delta | User+kernel P90 / max | Wins/losses/equal |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| accepted baseline | 8.336% | — | 14.237% / 23.085% | — | 9.426% | — | 18.372% / 25.039% | — |
| TreePLRU | 8.258% | -0.077 pp | 14.237% / 23.085% | 6 / 8 / 6 | 9.330% | -0.096 pp | 18.372% / 25.039% | 3 / 11 / 6 |
| committed L1I | 8.073% | -0.262 pp | 14.201% / 22.814% | 12 / 8 / 0 | 9.056% | -0.370 pp | 18.292% / 24.776% | 17 / 3 / 0 |
| DRAM command calendar | 7.970% | -0.366 pp | 14.249% / 23.104% | 13 / 6 / 1 | 8.945% | -0.481 pp | 18.243% / 25.057% | 14 / 4 / 2 |
| all direct candidates | 7.489% | -0.846 pp | 14.183% / 22.809% | 16 / 4 / 0 | 8.733% | -0.693 pp | 18.294% / 24.771% | 16 / 4 / 0 |

The complete reports are:

```text
tmp/residual-uarch-audit-20260817/formal-treeplru/summary.json
tmp/residual-uarch-audit-20260817/formal-l1i/summary.json
tmp/residual-uarch-audit-20260817/formal-dramcmd/summary.json
tmp/residual-uarch-audit-20260817/formal-direct-align/summary.json
```

The composed result improves the mean but barely moves P90 and maximum error.
It therefore cannot be described as a tail fix.  Its remaining user APE is
22.81%/19.93% for Stockfish C4/C8, 13.55% for NAMD C4, 12.43% for NAb C8,
and 10.80% for TeaLeaf C8.  Its user-plus-kernel APE remains 24.77%/21.59%
for Stockfish, 17.93% for NAMD C8, 11.00% for SPH C8, and 11.02% for NAb C8.

## Candidate decisions

### Promote: private-L2 and LLC TreePLRU

This is an exact target-configuration correction, not a fitted latency.  The
FastSim bit tree uses the same breadth-first parent bits as gem5: a touch
points each parent away from the MRU leaf and victim lookup follows the bits.
The target associativities are powers of two and match the captured leaf
counts.  The full dual-scope matrix leaves P90 and maximum unchanged and has
no conservation failure.

`configs/gem5-v28_1-time-epoch.cfg` now uses TreePLRU for private L2 and LLC.
A directed four-way victim test covers the gem5 parent-bit orientation.  The
normal build and `fastsim_tests` pass.

A three-pair, back-to-back C4 LBM check also found no attributable throughput
regression.  LRU measured 5.022/5.057/4.787 M user UOP/s (mean 4.955 M), while
TreePLRU measured 5.041/4.980/4.998 M (mean 5.006 M).  CPI was deterministic
within each policy (2.677356 for LRU and 2.678378 for TreePLRU), but wall-clock
throughput crossed the nominal 5 M threshold in both directions.  The shared
host therefore cannot support a strict single-run 5 M verdict; the paired data
does support the narrower conclusion that TreePLRU adds no measurable slowdown.
The six raw reports are `tmp/residual-uarch-audit-20260817/throughput-back-to-
back-{lru,treeplru}-{1,2,3}.json`.

### Keep experimental: committed-PC L1I

L1I improves pooled mean APE and is especially useful for omnetpp: user APE
drops by 2.94 pp at C4 and 2.43 pp at C8.  It is not the Stockfish fix.  The
added exposed cycles cover only 1.18% of the C4 Stockfish gap and 1.59% of the
C8 gap; raw misses are 14,344/24,347, while the missing target request stream
also contains wrong-path and refetch activity.  Graph500 C8 user APE regresses
by 0.66 pp in the isolated L1I run.

Default enablement would incorrectly imply that the target I-side request
stream is represented.  Promotion requires an audit-only request-stage ledger
and a generated, non-oracle wrong-path/refetch contract, followed by the FS
dual-scope and historical SE/uarch gates.

### Keep experimental: optional DRAM command constraints

The numeric values are captured correctly, and they help memory-shaped cases:
TeaLeaf improves by 2.83/1.62 pp and LBM C8 by 1.95 pp in user scope.  However,
Stockfish C4 maximum error slightly worsens, and the combined-scope maximum
rises from 25.039% to 25.057%.

More importantly, the implementation is not yet the gem5 controller: it lacks
complete read/write bus direction timing, refresh and full DDR command rules,
and applies constraints to an uncertified reconstructed arrival order.  A
numerically beneficial partial calendar is not sufficient evidence for
default enablement.  Controller arrival/order and comparable wait ledgers
must be closed first.

### Reject for default: unscaled FR-FCFS window

Disabling topology scaling makes the C8 effective selection window eight.
All five current-FS pilot cases regress: mean APE rises by 1.806 pp.  LBM alone
changes from 2.727% to 7.746% APE.  The candidate reorders 228,285 LBM
requests and lowers CPI from 2.670578 to 2.532788, moving in the wrong
direction.  This is direct evidence that a larger lookahead on the current
functional arrival stream is not a valid proxy for gem5's controller queue.

The full-queue open-adaptive and single-precharge switches remain default-off
as well.  Historical C16/C32 gates show non-additive regressions with fill and
response closure; C4/C8 production currently bypasses that repair at effective
window one.

### Reject/no accuracy value: committed rename free list

The destination-class repaired formal dataset covers every record and all 120
cores conserve Int/Float/Vec/CC allocations.  The exact finite free list
records zero stall cycles on every core and reproduces baseline CPI bit for
bit.  Enabling it would add input requirements without accuracy benefit and
would still omit wrong-path allocations.

### Reject for default: legacy response closure candidates

The current-FS targeted runs use Stockfish C4/C8, TeaLeaf C8, NAb C8 and NAMD
C4 after the DTLB repair.  They confirm the historical gates:

- dense ROB/LSQ changes CPI by zero on Stockfish C8, TeaLeaf C8 and NAb C8,
  and worsens NAMD C4 by 0.027 pp; historical pilots also overcount ROB-full
  transitions and fail throughput;
- response retime changes Stockfish/NAb by effectively zero and regresses
  TeaLeaf by 0.213 pp because most epochs cannot certify a stable replay;
- sparse resource repair improves at most 0.212 pp in this set and sometimes
  regresses; issue/writeback port collisions are not the missing tail;
- ROB-head-local suffix improves NAMD C4 by 0.375 pp and Stockfish by less
  than 0.1 pp, but historical minimum throughput is 4.629M UOP/s;
- whole-epoch causal timing improves TeaLeaf C8 by 0.645 pp and Stockfish C8
  by 0.236 pp, far below the residual and with known cross-uarch
  over-correction;
- corrected suffix carry did not finish one C4 Stockfish diagnostic in over a
  minute and was stopped; its historical isolated minimum is 3.746M UOP/s.

`core.response_sparse_scoreboard`, block summary, memory descriptor, batch
timing encode, activity certificate, the separate write queue and fill-response
stage are already in the production path.  The negative results above apply
to the additional default-off candidates, not to those accepted components.

## Remaining bottleneck and repair order

No implemented default-off candidate removes the current tail.  The evidence
splits the remaining work into three components rather than one global scalar:

1. **I-side request generation and frontend/backend overlap.** Stockfish is
   almost unchanged by TreePLRU, DRAM timing, dense response closure and L1I
   capacity.  Build a fetch request -> L0 response -> fetch resume -> queue/ROB
   exposure ledger and generate wrong-path/refetch state from branch/static
   structure without consuming gem5 timing as an online input.
2. **Controller arrival/read-write service order.** TeaLeaf and LBM respond to
   command timing, while unscaled FR-FCFS moves LBM in the wrong direction.
   First certify FastSim controller arrivals and implement the complete
   source-derived read/write scheduler; only then enable command constraints
   one at a time.
3. **Kernel contribution and residual OoO pressure.** NAMD C8 user APE is only
   3.75% in the accepted baseline but user-plus-kernel APE is 17.98%; this
   cannot be repaired by a user-path cache switch.  SPH and TeaLeaf also have
   larger combined-scope residuals.  Their kernel-event timing/profile and
   response-to-retire ledgers need a separate gate.

The fixed 12-cycle page-walk service remains an effective model.  A per-level
Ruby page-walk request/response ledger should eventually replace it to retain
Neutron while removing Graph500 C4 overcharge, but it is no longer the largest
pooled error source.
