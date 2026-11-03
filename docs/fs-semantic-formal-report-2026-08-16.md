# FST v7 syscall-semantic C4/C8 formal report (2026-08-16)

> Superseded input-contract notice (2026-08-16): a post-report record-level
> audit found that these 120 files have v7 headers but do not set feature bit 2
> or carry the per-record `destination_class_counts` marker. The numbers below
> remain reproducible for the prior model, which did not enable per-class
> rename timing, but this dataset is no longer accepted for current formal
> timing validation. TaoTrace and both collection/data-build gates have been
> fixed; a replacement C4/C8 collection must pass complete destination-class
> coverage before this report is superseded.

## Data gate and provenance

Run root:
`tmp/taotrace-fst-v7-c4-c8-formal-v3-semantic-20260816`.

- 20 configurations: 10 workloads at C4 and C8;
- 120 per-core FST v7 streams;
- 463,570,143 functional-warmup records and 1,200,000,108 measurement
  records;
- 722,016,405 measurement instructions and 828 syscall events;
- zero trace-integrity errors and 20/20 valid oracle identities;
- 120/120 valid `.fst.vmap` files, 168,629 mappings, zero missing maps;
- syscall arguments/pre timestamps/pre CPUs cover 828/828 events; 816/828
  have a return, with the 12 missing returns all allowed blocking calls at a
  trace boundary; 43 mmap and 43 munmap events have usable return semantics;
- zero syscall-semantic violations;
- launcher exit code 0.

Every core uses `fastsim-binary-warmup-slice`. FastSim replays the exact
record-bounded prefix, crosses one common barrier, resets measurement counters
and time, and retains cache/coherence, directory, branch predictor, DTLB,
DRAM/controller, dependency, response-scoreboard, syscall mapping, and virtual
page residency state. A core may have zero local warmup records, but every
configuration has a nonzero aggregate warmup.

C4 is the calibration split. C8 is a core-count-held-out split with the same
workload names; it is not a disjoint-workload test. The C8 pipeline reads the
frozen C4 config and does not recalibrate from C8 oracle data. Workload labels
are not inference inputs.

The state selector is exact for first accesses in successful trace-visible
non-MAP_POPULATE mmap ranges. First writes outside those ranges use one shared
residual rate. C4 selected 7,383 ppm after 2,353 exact semantic candidates and
11,648 fallback-write candidates. Page-fault event training WAPE is 6.11%;
leave-one-workload-out diagnostic WAPE is 12.46%.

## CPI accuracy

APE percentiles are workload-equal Type-7 percentiles. WAPE and bias retain
the common user-record denominator.

| Split | Scope | Mean APE | P50 | P90 | P99 | WAPE | Bias | Maximum |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| C4 calibration | user | 13.54% | 11.34% | 25.19% | 40.45% | 12.87% | -12.78% | 42.14% |
| C4 calibration | user+kernel | 14.06% | 9.81% | 26.92% | 40.46% | 12.50% | -10.29% | 41.97% |
| C8 held out | user | 14.63% | 12.71% | 22.99% | 44.34% | 13.22% | -13.22% | 46.71% |
| C8 held out | user+kernel | 15.41% | 12.00% | 24.46% | 44.33% | 12.39% | -12.39% | 46.54% |

C8 per-workload APE:

| Workload | User | User+kernel |
|---|---:|---:|
| Stockfish | 20.36% | 22.00% |
| omnetpp | 14.44% | 15.13% |
| zstd | 13.43% | 8.46% |
| LBM | 2.76% | 1.86% |
| SPH | 7.04% | 11.42% |
| TeaLeaf | 12.97% | 12.57% |
| NAb | 12.46% | 11.05% |
| Graph500 | 12.27% | 6.95% |
| NAMD | 3.89% | 18.14% |
| Neutron | 46.71% | 46.54% |

Against the invalid v2 run, C8 user mean/P90/P99 changes from
16.52%/36.00%/46.81% to 14.63%/22.99%/44.34%. The major improvement is the
collapse of the broad page-fault/cache-state tail, especially zstd user APE
(35.00% to 13.43%). The remaining P99 is Neutron and is unchanged; it is not a
warmup-length or page-fault-selection failure. Graph500 user APE changes from
4.96% to 12.27%, so the semantic state transition is not a universal CPI
improvement even when its event count is accurate.

## C8 held-out PMU accuracy

The full table is shown because sparse-counter MAPE/percentiles and count-
weighted WAPE answer different questions. `retired_*` and branches prove the
scope denominator; cache-hit MAPE can be very large when a reference count is
small.

### User

| Counter | Mean APE | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|
| branch misses | 5.92% | 1.20% | 17.46% | 22.03% | 3.09% | 2.52% |
| branches | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| DTLB accesses | 4.38% | 2.19% | 13.27% | 16.26% | 5.26% | 5.26% |
| DTLB hits | 4.52% | 2.20% | 13.38% | 16.31% | 5.48% | 5.41% |
| DTLB misses | 17.26% | 2.62% | 67.73% | 78.06% | 1.63% | 1.16% |
| L1D accesses | 4.53% | 2.19% | 13.28% | 16.26% | 5.45% | 5.45% |
| L1D hits | 3.74% | 2.27% | 9.05% | 13.63% | 4.35% | 4.24% |
| L1D misses | 72.71% | 12.60% | 132.03% | 515.26% | 29.71% | 24.10% |
| L2 accesses | 72.71% | 12.60% | 132.03% | 515.26% | 29.71% | 24.10% |
| L2 hits | 14460.01% | 9.93% | 14523.50% | 131435.56% | 34.99% | 30.00% |
| L2 misses | 174.85% | 19.83% | 475.48% | 854.71% | 33.11% | 16.21% |
| LLC accesses | 174.85% | 19.83% | 475.48% | 854.71% | 33.11% | 16.21% |
| LLC hits | 999.95% | 14.81% | 1706.08% | 8213.34% | 13.78% | -11.27% |
| LLC misses | 157.29% | 45.32% | 357.95% | 824.84% | 118.21% | 118.21% |
| retired instructions | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| retired uops | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |

### User plus active kernel

| Counter | Mean APE | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|
| branch misses | 19.72% | 6.21% | 38.84% | 116.53% | 4.86% | 2.31% |
| branches | 4.94% | 0.32% | 14.51% | 31.65% | 1.15% | -0.28% |
| DTLB accesses | 4.48% | 2.13% | 13.32% | 17.61% | 5.24% | 4.84% |
| DTLB hits | 4.47% | 2.13% | 13.42% | 17.65% | 5.37% | 4.97% |
| DTLB misses | 15.04% | 4.03% | 34.66% | 73.05% | 1.98% | 1.04% |
| L1D accesses | 4.60% | 2.13% | 13.32% | 17.61% | 5.42% | 5.03% |
| L1D hits | 3.91% | 2.18% | 10.42% | 13.67% | 4.44% | 3.87% |
| L1D misses | 68.18% | 10.20% | 114.58% | 495.49% | 28.18% | 23.03% |
| L2 accesses | 68.18% | 10.20% | 114.58% | 495.49% | 28.18% | 23.03% |
| L2 hits | 5819.57% | 11.73% | 5872.54% | 52819.03% | 33.96% | 29.01% |
| L2 misses | 139.69% | 11.42% | 362.65% | 947.98% | 31.84% | 15.33% |
| LLC accesses | 139.69% | 11.42% | 362.65% | 947.98% | 31.84% | 15.33% |
| LLC hits | 115.14% | 14.05% | 133.02% | 917.10% | 14.44% | -11.52% |
| LLC misses | 178.86% | 12.80% | 404.03% | 1238.26% | 97.88% | 97.81% |
| retired instructions | 0.47% | 0.21% | 1.28% | 1.86% | 0.51% | -0.08% |
| retired uops | 0.73% | 0.34% | 2.18% | 2.99% | 0.73% | -0.14% |

The access/retirement counters are within about 0%--5.5% WAPE, while cache
misses remain inaccurate. This rules out marker or denominator corruption as
the dominant PMU issue and identifies cache replacement/coherence/state
timing as the next modeling target.

## Kernel events and throughput

C8 event/count results:

| Quantity | Mean APE | P50 | P90 | P99 | WAPE | Bias |
|---|---:|---:|---:|---:|---:|---:|
| IRQ events | 45.72% | 50.97% | 68.75% | 78.88% | 55.26% | 11.65% |
| page-fault events | 41.53% | 5.03% | 100.00% | 100.00% | 14.66% | -5.68% |
| syscall events | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% | 0.00% |
| page-fault active cycles | 42.13% | 2.48% | 100.00% | 100.00% | 16.66% | -10.99% |
| syscall active cycles | 85.89% | 13.66% | 274.34% | 460.15% | 22.45% | 3.27% |

Idle is diagnostic-only and predicted as zero; it is excluded from active
combined CPI. IRQ is a calibrated statistical process because a portable user
functional trace cannot identify asynchronous interrupt arrival boundaries.

All FastSim cases in each pipeline were replayed sequentially. Throughput is
million user uops/s:

| Split | Scope | Mean | P50 | P90 | P99 | Minimum |
|---|---|---:|---:|---:|---:|---:|
| C4 calibration | user | 9.44 | 9.48 | 10.98 | 12.11 | 5.42 |
| C4 calibration | user+kernel | 9.27 | 9.26 | 11.65 | 12.02 | 5.03 |
| C8 held out | user | 11.12 | 11.64 | 12.68 | 13.11 | 5.81 |
| C8 held out | user+kernel | 10.73 | 11.82 | 12.81 | 12.88 | 5.87 |

## Interpretation and next correction boundary

The dual marker and functional warmup are correct. Increasing the warmup
length cannot synthesize pages allocated after the measurement marker or
kernel-created cache state. The new semantic model fixes that failure for
trace-visible mappings and sharply reduces the broad C8 CPI tail, but it does
not solve two independent limits:

1. Mapping creation before the functional trace is unobserved. For example,
   C4 NAMD has 304 exact semantic candidates while C8 has only 52, despite 294
   reference page faults. Stockfish has 35 C8 faults but no exact candidates.
   A small shared fallback cannot satisfy those cases and also avoid false
   positives in LBM/TeaLeaf, which have thousands of first writes and zero
   faults. Solving this requires a portable initial VMA/page-residency
   snapshot, not a workload coefficient.
2. Neutron CPI and cache-miss PMU remain inaccurate even when page faults are
   zero. The next CPI/PMU correction must therefore target the core/cache
   timing and replacement/coherence path, using trace-visible invariants and
   no workload-specific scalar.

Canonical artifacts:

- `audit/matrix-integrity.json`, `audit/syscall-metadata.json`,
  `audit/virtual-page-map.json`, and `audit/oracle-identity.json`;
- `fst-v7/index.json`;
- `accuracy/calibration-c4/summary.{json,csv,md}` and
  `accuracy/calibration-c4/calibration.json`;
- `accuracy/held-out-c8/summary.{json,csv,md}`;
- `audit/warmup-cachelines-c4.{json,md}` and
  `audit/warmup-cachelines-c8.{json,md}`.

The invalid `tmp/taotrace-fst-v7-c4-c8-formal-v2-20260815` directory was
removed after this run passed every replacement gate.

## Post-report frontend correction audit

No post-report candidate has been promoted into the formal C4/C8 numbers
above. The following ablations explain why:

- A branch-shadow drain improves Neutron C8 user APE from 46.71% to 35.83%,
  but regresses the independent 192-case microarchitecture CPI P99 from
  10.118% to 41.343%. gem5 source shows the target's unset `squashWidth`
  performs a one-cycle full squash, so the modeled multi-cycle drain was
  source-inconsistent. FastSim now encodes unset width as zero and makes the
  experiment a no-op.
- A 32 KiB/8-way/64 B committed-PC L1I has 37.83% access WAPE and 64.98% miss
  WAPE against the ten C8 Ruby I-cache totals. Neutron produces 672 modeled
  misses versus 15,929 reference misses, so a calibrated miss latency would
  hide missing accesses rather than model them.
- Exact predictor-entry state and a causal committed-successor replay do not
  change Neutron materially. The latter replays 58.3 million speculative
  records but generates only 15 additional misses, confirming that the absent
  footprint is outside the committed dynamic graph.

An optional canonical `.fst.imap` companion is now implemented for static
instruction length, control-flow and conservative may-access-memory facts
available from both producers. It is not part of the v3 canonical dataset, and
its absence does not invalidate any FST, oracle, CPI, PMU or throughput result
in this report. Complete maps were subsequently derived from all ten workload
binaries and attached to temporary C8 pilots without recollecting traces or
oracles.

The full mapped-path candidate uses the branch predictor snapshot from before
repair, follows nested direct/conditional predictions, and optionally replays
the most recently committed page for each wrong-path memory PC into DTLB state.
The speculative accesses are reported in separate diagnostic counters and are
never added to architectural PMU. Results:

- Candidate user-CPI mean/P50/P90/P99 is
  14.267%/12.138%/22.711%/44.315%; formal is
  14.633%/12.714%/22.994%/44.337%.
- Neutron replays 57.946M instructions including 7.454M memory instructions;
  98.32% have a causal last-committed page proxy, yet only two miss in the
  state-only DTLB. Its CPI APE worsens from 46.709% to 46.716%.
- Neutron's raw gem5 timing-DTLB misses are 6.047M versus 0.628M retired
  user-PMU misses. Static memory PCs plus their last committed pages therefore
  cannot recover wrong-context addresses or repeated pending translations.
- A second causal diagnostic marks PCs already observed on multiple pages.
  Neutron's wrong-path memory share is 54.35%, but Graph500/Stockfish/TeaLeaf
  are 85.40%/78.63%/73.09%, so address instability is not a safe generic
  selector either. The finer historical page-transition rates are 28.80% for
  Neutron and 42.10% for Graph500, which also fails to explain their 9.634x
  versus 1.177x raw/retired timing-DTLB ratios.
- The C4 candidate mean/P50/P90/P99 is
  13.255%/10.091%/24.904%/40.423%, only slightly different from formal
  13.54%/11.34%/25.19%/40.45%. Neutron remains 42.148% versus 42.144%, while
  committed DTLB-miss WAPE regresses from 1.750% to 2.014%.

The comparison was repeated as a same-binary feature-off/feature-on pair. The
feature-off run reproduces the formal C4/C8 aggregates exactly. L1I static-path
state alone retains the small CPI change above and leaves committed DTLB PMU
bit-identical to formal (C4/C8 WAPE 1.750%/1.633%). Adding recent-page DTLB
state changes no reported CPI percentile, but worsens DTLB WAPE to
2.014%/1.779%. The DTLB submodel is therefore rejected independently; the L1I
submodel remains default-off because Neutron and the P99 gate are not solved.

The candidate is rejected and none of the formal numbers above change. A new
timing term based only on speculative-path volume or page instability would be
workload overfitting. The reusable runner, summarizer and raw outputs are
`scripts/run_c8_speculative_path_audit.sh`,
`tools/summarize_speculative_path_audit.py`, and
the `tmp/c{4,8}-speculative-path-instability-audit-20260816` directories.

The subsequent pre-resolution resource gate reaches the same boundary. A
causal committed-PC profile recovers 98.92%--100.00% of C8 wrong-path static
instructions and estimates macro UOP expansion plus FU mix without changing
cycles or PMU. For Neutron it estimates 109.254M path UOPs, or 92.890M after a
source-derived free-ROB cap, versus 30.446M gem5 commit-squashed and only
0.198M issued-squashed UOPs. Across workloads, ROB-capped/commit-squashed spans
0.09x--3.05x and estimated/issued-squashed spans 14x--1,745x; C4 has the same
instability. Thus even complete code/profile coverage does not reveal which
wrong-path UOPs issued and competed. The diagnostic remains state-free, and
no IQ/FU/LSQ timing term or `.fst.imap` format change is promoted.
