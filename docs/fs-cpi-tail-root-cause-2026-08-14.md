# FS CPI P90/P99 tail root-cause report (2026-08-14)

## 1. Scope and headline

The current formal set contains 10 C4 calibration configurations and 26 held-out
C8/C16/C32 configurations.  All runs use a two-phase functional warmup and
exactly 10 million measured user FST records per core.  The held-out split is
the formal headline under `docs/accuracy-reporting-contract.md`.

| Scope | Split | cases | MAPE | P50 | P90 | P99 | maximum |
|---|---|---:|---:|---:|---:|---:|---:|
| user | calibration | 10 | 16.15% | 11.61% | 39.50% | 41.49% | 41.71% |
| user | held-out | 26 | 14.90% | 13.77% | **27.92%** | **49.08%** | 50.02% |
| user+kernel | calibration | 10 | 15.80% | 12.61% | 29.37% | 39.28% | 40.38% |
| user+kernel | held-out | 26 | 14.55% | 13.67% | **24.84%** | **47.84%** | 48.75% |

Pooling calibration and held-out cases only as a diagnostic gives user
P90/P99 37.17%/48.70% and user+kernel P90/P99 26.93%/47.48%.  These pooled
numbers do not replace the held-out headline.

The tail is concentrated rather than a uniform regression:

| Case | user error | user+kernel error | tail role |
|---|---:|---:|---|
| Neutron C4 | -41.71% | -40.38% | calibration maximum |
| Neutron C8 | -46.25% | -45.10% | held-out P99 support |
| Neutron C16 | -50.02% | -48.75% | held-out maximum/P99 support |
| zstd C4 | +39.25% | +10.29% | calibration user P90/P99 |
| zstd C8 | +35.09% | +4.33% | held-out user P90 upper support |
| zstd C16 | +20.76% | +6.22% | held-out user P90 lower support |
| SPH C4 | +0.85% | +28.15% | calibration combined tail |
| SPH C8 | -6.83% | +25.71% | held-out combined P90 upper support |
| SPH C16 | -5.93% | +23.98% | held-out combined P90 lower support |

Type-7 interpolation explains the headline quantiles.  Held-out user P90 is
the interpolation between zstd C16 and C8.  Held-out user+kernel P90 is the
interpolation between SPH C16 and C8.  Both held-out P99 values are controlled
by Neutron C8/C16.

## 2. Oracle-identity defect and validity boundary

All 36 formal result directories fail the new identity audit in the same way:

| Field | TaoTrace profile | restored gem5 target |
|---|---:|---:|
| core frequency | 4 GHz | 3 GHz |
| total LLC capacity | 2 MiB | 64 MiB |
| LLC banks | 1 | 8 |

The sampling wrapper independently parsed the command line with fallback
defaults.  The actual gem5 configuration inherited the main config's 3 GHz,
8 MiB-per-bank and 8-bank defaults, while TaoTrace's path-class LRU consumed
the wrapper's 4 GHz/2 MiB/1-bank `uarch_profile.json`.

Consequences are deliberately separated:

- gem5 cycle counts, CPL classification, user CPI and user+kernel CPI remain
  usable because the instantiated gem5 target was correct;
- retired instruction/UOP, branch and branch-miss truth is not derived from
  the cache profile;
- existing LLC path-class PMU truth was generated with the wrong capacity and
  bank geometry and must be regenerated.  L1/L2 dimensions happen to match,
  but those fields remain diagnostic until the paired artifact passes the
  identity gate;
- DTLB diagnostics remain useful because the profile's 64-entry fully
  associative DTLB matches the target, but the new identity gate rejects the
  paired PMU report as a whole.

`tools/validate_fs_oracle_identity.py` now audits this contract, and
`tools/run_kernel_event_accuracy_pipeline.py` refuses mismatched inputs before
running FastSim.  The audit artifact for this dataset is
`tmp/fs-cpi-tail-attribution-v1/formal-oracle-identity-audit.json`.

## 3. Neutron: P99 is missing frontend and speculative-window pressure

Neutron is underpredicted at every available core count, and the absolute gap
grows from 0.417 CPI at C4 to 0.453 CPI at C16.  It is not an LLC-capacity
problem:

| Case | FastSim user LLC misses | gem5 Ruby L3 misses | gem5 DRAM reads |
|---|---:|---:|---:|
| Neutron C4 | 47,666 | 70,760 | 50,045 |
| Neutron C8 | 60,403 | 77,037 | 62,581 |
| Neutron C16 | 71,592 | 94,788 | 74,725 |

The Ruby counters include kernel and speculative traffic, yet DRAM reads are
already close to the committed-user FastSim misses.  Even assigning 200 extra
cycles to every C8 L3-miss difference explains only about 0.042 CPI, below one
tenth of the 0.440 CPI gap.  Changing FastSim LLC capacity to 16 or 32 MiB and
disabling coherence left both the count and CPI effectively unchanged.

C8 gem5 O3 counters instead expose a frontend/window-heavy signature, per
measured user UOP: 0.119 I-cache stall cycles, 0.381 commit-squashed
instructions, 0.234 IQ-full events, 0.025 squashed loads and 0.009 squashed
stores.  FastSim consumes committed user UOPs only, has no I-cache timing
model, and normally gives a mispredicted branch a two-cycle recovery without
wrong-path occupancy.

The causal ablations are consistent with that diagnosis but also show why a
global scalar is unsafe:

| C8 ablation | Neutron error | zstd error | conclusion |
|---|---:|---:|---|
| baseline | -46.25% | +35.09% | opposite-signed tails |
| branch penalty 2 -> 12 | -40.72% | +40.67% | helps Neutron, worsens zstd |
| synthetic branch shadow | -35.83% | +40.96% | recovers only part of Neutron gap |
| DTLB `se_atomic` | -46.71% | +35.00% | corrects PMU, not CPI tail |

Correction order:

1. Add a set-associative L1I/ITLB fetch-state model driven by the committed PC
   stream already present in FST.  Warmup must populate this state and reset
   only measurement counters/time.
2. Replace the scalar branch penalty with branch-position-aware recovery and a
   bounded speculative occupancy state for fetch/decode/rename/ROB/IQ/LSQ.
   The approximation may use branch type, predictor result and target-visible
   functional features, but must not invent wrong-path PMU accesses.
3. If cache pollution from wrong-path and CPL0 accesses remains material, add
   an optional state-only functional sidecar containing address/order/class but
   no timing.  Replay it into cache/predictor state while excluding it from
   user retired counts and user CPI.
4. Accept the change only if Neutron C4/C8/C16 improves together and zstd,
   graph500 and the historical 92-case SE set do not regress.  A workload-ID
   correction or one global recovery constant is not acceptable.

## 4. zstd: P90 is excessive SQ/response exposure, not extra misses

zstd is overpredicted by +39.25%, +35.09%, +20.76% and +18.46% at
C4/C8/C16/C32.  Its FastSim base CPI is stable at about 0.308.  The changing
part is response-critical exposure:

| cores | reference CPI | predicted CPI | response-critical CPI | SQ-capacity CPI |
|---:|---:|---:|---:|---:|
| 4 | 0.394 | 0.549 | 0.241 | 0.194 |
| 8 | 0.389 | 0.525 | 0.217 | 0.174 |
| 16 | 0.380 | 0.459 | 0.151 | 0.104 |
| 32 | 0.395 | 0.468 | 0.160 | 0.106 |

At C8, FastSim has 114,368 LLC misses.  Actual Ruby reports 126,115 L3 misses
and 123,254 DRAM reads, so the overprediction cannot be caused by FastSim
having too many last-level misses.  The committed FST contains 114,277 cache
lines not present in the warmup prefix, almost exactly the FastSim miss count;
the cache state is internally consistent with its input.

Disabling x86 TSO reduces CPI from 0.525 to 0.338 and changes the sign of the
error to -13.07%.  This is a diagnostic localization: the excess reaches the
critical path through store response/SQ/ordered-retire edges.  It is not a
production fix because the gem5 target has `needsTSO=true`.  Increasing branch
penalty or enabling branch shadow worsens the error to about +41%.

Correction order:

1. Add a per-store ledger for address generation, commit eligibility, Ruby
   send, ownership/data response, SQ release, TSO next-store release and the
   final ordered-retire delay.  Report both raw waiting and non-overlapped
   critical exposure.
2. Audit the sparse response-feedback edge that converts long store responses
   into SQ-capacity critical cycles.  Preserve the target's 32-entry SQ and
   single-store TSO rule; test store-send, store-ack and SQ-release edges one at
   a time.
3. Replay state-only CPL0/wrong-path cache accesses if the corrected oracle
   shows that they warm zstd lines before committed accesses.  They must mutate
   cache state without entering user PMU/CPI.
4. Validate C4/C8/C16/C32 scaling.  A candidate must remove the approximately
   0.08--0.15 excess CPI without pushing the low-SQ-pressure workloads below
   their reference.

## 5. SPH and secondary combined tails: page-fault classifier

SPH's user-only CPI is already within 7%, but its combined CPI is overpredicted
because one global allocation probability converts too many first touches
into page faults:

| cores | reference/predicted faults | reference/predicted PF cycles | combined error |
|---:|---:|---:|---:|
| 4 | 124 / 497 | 1.89M / 6.74M | +28.15% |
| 8 | 59 / 806 | 1.18M / 10.92M | +25.71% |
| 16 | 102 / 1,426 | 2.19M / 19.33M | +23.98% |

The same global probability is unstable in both directions: Neutron has zero
reference page faults but predicts 34/56/103 at C4/C8/C16; stockfish predicts
4, 463, 3 and 3,232 faults at C4/C8/C16/C32.  This is model misspecification,
not random sample noise.

Correction order under the current `user functional trace + syscall number`
input contract:

1. Retain the allocation syscall number after arming the first-touch detector;
   the current state retains only a boolean and recency distance.
2. Fit separate hierarchical rates for syscall number, read/write first touch
   and recency bucket, with Beta/binomial shrinkage to a global fallback.
   Freeze rates on C4 calibration only.
3. Emit candidate and selected counts for every feature cell so C8/C16/C32
   errors can be attributed without reading oracle timing online.
4. Gate on page-fault event WAPE and active-cycle WAPE before looking at
   combined CPI.  SPH, stockfish, NAMD and zero-fault Neutron must all improve;
   matching only the pooled event total is insufficient.

Implementation update (2026-08-14): steps 1 and 3 are complete. FastSim now
retains allocation syscall identity and read/write recency histograms, uses
independent deterministic probability accumulators per syscall/access type,
and accepts a frozen per-syscall table with a global fallback. The calibrator
uses only eight coefficients with fixed 65,536-candidate shrinkage and chooses
the recency window by leave-one-workload-out validation; workload ID is not an
inference input.

The 10-C4 diagnostic does **not** pass step 4. Event/active-cycle WAPE is
47.60%/43.69% in-sample and 62.44%/56.99% leave-one-workload-out.
Syscall-specific rates collapse to within 69 ppm of the 647,880 ppm global
rate, so the failure is not a free per-workload table: syscall 9 itself
requires incompatible rates for zstd/Graph500 versus SPH/NAMD. LBM and
Neutron also expose thousands of trace-first touches with zero reference
faults. The hierarchical implementation remains available for auditing, but
the generated pilot profile is not a formal correction.

An exact model is impossible from syscall number alone because the trace does
not reveal page residency, prior kernel mappings or asynchronous faults.  If
the hierarchical model cannot meet the gate, extend the functional trace with
page-fault class and page token markers; do not fit a workload-specific CPI
residual.

Idle cycles are excluded from the formal user+kernel CPI numerator.  Their
current 100% error is a separate runnable/scheduler-state limitation and does
not explain the SPH combined tail.  IRQ error is also secondary to the three
tails above and must remain a separate event process.

## 6. DTLB correction

The `timing_walk` mode counted repeated accesses to an outstanding VPN as new
misses.  On Neutron C8 it produced 4,793,212 misses versus 627,685 reference;
`se_atomic` produces 640,237 (+2.0%).  C4 and C16 show the same approximately
2% residual after correction.  zstd C8 changes from +14.6% to -0.8% DTLB-miss
error.

This correction barely changes CPI and makes the Neutron underprediction
slightly larger, proving that the inflated TLB timing had been masking rather
than causing the P99 problem.  The formal pipeline now uses `se_atomic` for
functional DTLB PMU classification.  A future timing walker must coalesce
same-VPN outstanding walks and count merged accesses separately.

## 7. Evidence confidence and remaining ambiguity

| Finding | confidence | reason |
|---|---|---|
| 36/36 PMU-profile identity mismatch | confirmed | direct request/profile comparison and wrapper source |
| SPH combined tail is page-fault overprediction | confirmed | active-cycle residual and event counts close exactly |
| `timing_walk` inflates DTLB misses | confirmed | same-input `se_atomic` ablation across C4/C8/C16 |
| zstd error propagates through SQ/TSO response closure | high | causal ledger plus sign-changing no-TSO ablation |
| exact zstd upstream edge is store response versus SQ release | unresolved | both feed the same critical ledger; needs per-store timestamps |
| Neutron contains a wrong-path memory/translation or dependent queue-pressure omission | high | raw timing-DTLB amplification, load-to-use latency and IQ-full signature; committed I-cache and state-only TLB ablations do not close CPI |
| exact Neutron split among wrong-context addresses, outstanding translations and IQ pressure | unresolved | the committed functional trace has none of the squashed addresses or issue timestamps needed to separate them |

Raw gem5 stall and squash counters are not additive cycle components.  They
identify the missing mechanism family but cannot be summed to manufacture the
0.44 CPI Neutron residual.  The proposed ledgers and single-edge ablations are
required before assigning exact percentages within that family.

## 8. Execution plan and acceptance gates

1. **P0, oracle identity:** fix the Tao wrapper to receive the actual main
   configuration, run the identity validator before promotion, and recollect
   the 36 PMU oracles.  Existing CPI/cycle labels may be retained for analysis,
   but formal paired CPI+PMU publication waits for a passing oracle.
2. **P1, Neutron P99:** committed-PC and exact static-path L1I timing are
   implemented and rejected by the full C8 audit. Do not promote another
   wrong-path timing term until a portable, workload-held-out observable can
   distinguish wrong-context address and outstanding-translation pressure.
3. **P1, zstd P90:** add the store/SQ response ledger and repair only the
   over-exposed causal edge while preserving TSO.
4. **P1, combined P90:** allocation syscall identity and the strongly-shrunk
   hierarchical classifier are implemented, but fail workload-held-out WAPE;
   collect allocation result/page-residency or explicit state-only page-fault
   markers before another model promotion attempt.
5. **P2, full rerun:** freeze on the 10 C4 calibration cases; report held-out
   26-case user and user+kernel MAPE/P50/P90/P99, every PMU counter in both
   scopes, and throughput mean/P50/P90/P99/minimum.  Keep the historical
   92-case SE set as a no-regression gate.

The target gate is held-out CPI P99 below 12% in each scope, material
cache/TLB/branch PMU WAPE at most 5% against an identity-valid oracle, and
minimum throughput at least 5 million user UOP/s.  Sparse PMU fields retain
their finite-APE/WAPE qualification from the reporting contract.  Current held-out minimum throughput already
passes at 5.32M UOP/s for user and 5.29M UOP/s for user+kernel; every timing
fix must preserve that margin.

## 9. 2026-08-16 frontend ablation closure

The fresh identity-valid C4/C8 v3 dataset keeps Neutron as the user-CPI P99
tail: 42.14% at C4 and 46.71% at C8, both underpredictions. Its reference page
fault count is zero, while functional branch, DTLB and L1D-access counts are
already close enough to exclude marker placement, functional-warmup length and
page-fault selection as the dominant cause.

Three generic frontend candidates were tested without workload identity:

| Candidate | Neutron C8 user APE | C8 mean/P90/P99 | Decision |
|---|---:|---:|---|
| current formal | 46.71% | 14.63% / 22.99% / 44.34% | baseline |
| anonymous branch-shadow drain | 35.83% | 10.52% / 20.91% / 34.34% | reject |
| committed-PC L1I, +6-cycle miss penalty | effectively unchanged | not promoted | diagnostic only |
| causal learned speculative path | effectively unchanged | not promoted | diagnostic only |

The apparently useful branch-shadow result fails the independent 192-case C4
microarchitecture matrix. Variant CPI mean/P90/P99 regresses from
3.281%/8.769%/10.118% to 6.665%/10.026%/41.343%; material direction accuracy
drops from 96.00% to 88.00%. `v28_int_div_serial` is the counterexample: the
old model turned 251,598 branch misses into 48,053,965 anonymous UOPs and
6,038,218 drain cycles even though gem5 and the baseline FastSim both have CPI
about 0.821.

Source inspection resolves the contradiction. The target leaves gem5
`BaseO3CPU.squashWidth` unset. `ROB::doSquash()` therefore uses the full ROB
entry count and removes all younger instructions in one cycle. FastSim now
represents this explicitly as `branch.squash_width=0`; branch-shadow is a
zero-cost no-op for the formal target. A 192-case replay restores the original
CPI and PMU metrics exactly. Isolated `int_div_serial` replay with the option
off/on is 17.20/17.22 M user UOP/s with identical CPI and zero shadow counts.
Throughput from 30 simultaneous validation processes is not an isolated
throughput measurement and must not replace the sequential reporting rule.

Committed PCs also do not reconstruct the instruction-fetch stream. Across
the ten C8 workloads, committed-PC L1I accesses have 37.83% WAPE against Ruby
I-cache demand accesses and misses have 64.98% WAPE. Neutron sees 5,037,855
committed block accesses and only 672 modeled misses versus 9,013,805 accesses
and 15,929 misses in gem5. Adding the source-derived six-cycle private-L2
penalty changes Neutron CPI by only 0.000029.

The causally learned path model reaches the same observability boundary. On
Neutron C8 it replays 58,315,803 instruction records and 9,678,782 L1I block
accesses, but produces only 15 additional misses: learned successors remain in
the committed hot-code graph. This proves that scaling a miss penalty cannot
recover absent wrong-path code identities.

The implemented next boundary is an optional `.fst.imap` companion containing
only static ISA facts (PC, length, fallthrough, control-flow type and direct
target), plus a conservative `may-access-data-memory` bit. Both a normal
drmemtrace module decoder and TaoTrace can produce these facts without Intel
PT. FastSim follows exact sequential and direct control-flow edges from a
pre-repair predictor snapshot; indirect edges and absent instructions fail
closed instead of encoding a gem5 prediction oracle. Existing v3 FST/oracle
data remains valid and was reused: complete maps were derived from all ten
workload binaries, so neither functional traces nor oracle baselines were
recollected.

The resulting ten-workload C8 audit closes this candidate path:

| Workload | User CPI APE, formal/candidate | Path records | Path memory | Page known | Page unstable | State-only DTLB misses | gem5 raw/retired DTLB misses |
|---|---:|---:|---:|---:|---:|---:|---:|
| Stockfish | 20.36% / 20.04% | 1.758M | 0.522M | 96.33% | 78.63% | 1,237 | 3.650x |
| omnetpp | 14.44% / 11.84% | 3.544M | 1.518M | 97.06% | 30.17% | 39,156 | 2.529x |
| zstd | 13.43% / 13.43% | 17.659M | 7.169M | 99.88% | 33.39% | 2,413 | 1.823x |
| LBM | 2.76% / 2.76% | 0.065M | 0.028M | 100.00% | 38.13% | 0 | 2.406x |
| SPH | 7.04% / 6.98% | 6.239M | 2.005M | 95.17% | 27.55% | 41 | 3.817x |
| TeaLeaf | 12.97% / 12.90% | 0.263M | 0.066M | 81.40% | 73.09% | 59 | 1.996x |
| NAb | 12.46% / 12.44% | 3.196M | 1.108M | 96.14% | 13.24% | 8 | 3.002x |
| Graph500 | 12.27% / 11.68% | 15.180M | 3.447M | 99.99% | 85.40% | 11 | 1.177x |
| NAMD | 3.89% / 3.89% | 6.120M | 1.476M | 96.62% | 46.88% | 54 | 11.814x |
| Neutron | 46.71% / 46.72% | 57.946M | 7.454M | 98.32% | 54.35% | 2 | 9.634x |

`Page unstable` is causal: a PC is counted only after the committed stream has
already shown it on more than one virtual page. It is not a usable generic
selector. Graph500, Stockfish and TeaLeaf have higher unstable shares than
Neutron without its CPI tail. Weighting by the causal historical page-change
rate does not rescue the selector: Neutron is 28.80%, while Graph500 is
42.10%, despite raw/retired DTLB ratios of 9.634x and 1.177x respectively.
Replaying the most recently committed page for
each static memory PC is also ineffective: 7.454M Neutron speculative memory
instructions cause only two state-only DTLB misses and change committed DTLB
misses by -285. The real gem5 timing DTLB records 6.047M misses versus 0.628M
retired user-PMU misses, exposing activity from wrong-context addresses and/or
repeated outstanding translations that a last-committed-page proxy cannot
reconstruct.

The full candidate user-CPI mean/P50/P90/P99 is
14.267%/12.138%/22.711%/44.315%, versus the formal
14.633%/12.714%/22.994%/44.337%. Neutron becomes marginally worse. This is a
diagnostic result, not a promoted model. The C4 replay reaches
13.255%/10.091%/24.904%/40.423% versus formal
13.54%/11.34%/25.19%/40.45%, but Neutron remains 42.148% versus 42.144% and
committed DTLB-miss WAPE regresses from 1.750% to 2.014%.

A same-binary feature-off replay exactly reproduces every formal aggregate,
so this small difference is not code-version drift. Separating the two state
paths shows that L1I-only produces exactly the same CPI aggregates while
preserving formal DTLB PMU (C4/C8 WAPE 1.750%/1.633%). Enabling the recent-page
DTLB replay does not change CPI at reported precision and solely worsens DTLB
WAPE to 2.014%/1.779%; that submodel is rejected. The L1I path remains a
default-off diagnostic because it does not improve Neutron or the CPI P99
gate enough to justify changing the canonical input/model. Formal CPI, PMU and
throughput therefore remain unchanged. The reproducible raw outputs and
generated summaries are under
`tmp/c4-speculative-path-instability-audit-20260816` and
`tmp/c8-speculative-path-instability-audit-20260816`, produced by
`scripts/run_c8_speculative_path_audit.sh` and
`tools/summarize_speculative_path_audit.py`.

### 9.1 Pre-resolution resource-profile gate

A second state-free diagnostic tests whether the exact static path can support
IQ/FU/LSQ competition without a workload coefficient. During warmup and
measurement, FastSim causally learns each already-committed macro PC's UOP
expansion and eight target FU classes. A wrong-path PC can use only observations
available before that branch. Q16 counters retain the mean expansion and FU
mix; no target resource is allocated, no cycle is added, and no speculative
event enters architectural PMU.

Coverage is not the limiting factor. In C8, 98.92%--100.00% of statically
decoded path instructions have a causal profile. The limiting factor is issue
observability:

| Workload | Estimated path UOPs | ROB-capped upper bound | gem5 commit-squashed | gem5 issued-squashed | IQ-full events |
|---|---:|---:|---:|---:|---:|
| Stockfish | 5.538M | 3.048M | 1.040M | 0.006M | 0.002M |
| omnetpp | 6.480M | 6.392M | 18.176M | 0.206M | 0.031M |
| zstd | 27.974M | 27.669M | 17.409M | 0.210M | 0.018M |
| Graph500 | 28.631M | 27.764M | 15.654M | 0.351M | 0.416M |
| Neutron | 109.254M | 92.890M | 30.446M | 0.198M | 0.691M |

The ROB cap is source-derived: at branch resolution it subtracts older
dispatched-but-not-retired UOPs and the resolving branch from the configured
192 entries. Even after this bound, capped/commit-squashed spans about
0.09x--3.05x in C8 and 0.064x--3.03x in C4. Raw estimated/issued-squashed
spans about 14x--1,745x. Neutron's estimated memory UOPs are 7.991M versus
2.737M gem5 squashed loads+stores. Applying any of these counts as IQ/FU/LSQ
occupancy would therefore be an oracle-fitted scale, not a portable model.

`commitSquashedInsts` is only a diagnostic comparison because it also includes
non-branch squash causes. More importantly, a functional committed stream has
neither wrong-path operands/dependencies nor the issue decisions needed to
identify the 0.198M Neutron UOPs that actually competed for FUs. The resource
profile and ROB upper bound remain default-off-path diagnostics only. They do
not justify an `.fst.imap` v2: a normal drmemtrace module decoder can classify
a macro instruction, but cannot portably reproduce gem5's target-specific x86
micro-op cracking. A future timing model requires a producer-neutral canonical
macro-to-UOP lowering contract before resource competition can be promoted.
