# LBM Projected RD/WB Experiment

Date: 2026-09-14. Status: fixed-input experiment implemented and evaluated;
production feedback integration stopped at the adverse-control gate.

Follow-up: the user subsequently authorized an opt-in production-feedback
pilot. Its implementation and completed five-case results are recorded in
[Projected RD/WB Response Feedback Pilot](projected-dram-feedback-pilot-20260914.md).
That pilot was then rejected and disabled by user decision (2026-09-14): the
opt-in switch `dram.projected_feedback` is now `false`, the code and failure
evidence are retained, and the maintained default stays off. The follow-up
supersedes the integration stop below, not these fixed-input measurements or
the failed C1 controls.

## Decision

The user requested a throughput-first repair inside the existing two-stage
engine. This experiment therefore does not require retained core continuation,
actual-arrival convergence, or a new full UOP pass. It is deliberately separate
from the strict cross-Q controller contract.

The candidate improves the selected C13 pressure window, but worsens both
existing C1 controls. It is not connected to production latency/SQ feedback
and is not promoted. This is a measured rejection of this reservation policy,
not evidence that every projected-flow mixed-controller model must fail.

## Implementation

- `MixedDramController::reserve_projected_batch` reuses mixed RD/WB timing,
  direction, capacity and page-policy logic. It reserves every supplied read,
  admits every supplied WB, and retains low-water writes across batches.
- Each channel advances independently. Later-discovered arrivals earlier than
  that channel's event cursor are clamped and explicitly counted. Previously
  selected services cannot be revoked. This is finite-lookahead scheduling,
  not a certified actual-arrival frontier or batch-partition equivalence.
- Strict `submit/advance` and projected reservation cannot be mixed on one
  instance. Controller copies preserve projected state for rollback.
- `diagnostics.projected_dram_path` captures canonical DRAM arrivals, RD
  commands/responses, and actual dirty LLC WB before optional FRFCFS repair.
  Transaction rollback discards speculative records. Only accepted batches
  are written. Empty configuration disables capture.
- `fastsim_replay_projected_dram` streams those accepted batches through the
  candidate. It preserves warmup state and outputs paired read services.
  No gem5 timing is an input to either simulator or replay.

CSV columns are `phase,batch,kind,arrival,line,core,sequence,ordinal,command,response`.
Phase 0 is functional warmup and phase 1 is measurement; all timestamps are
reference cycles. RD source fields identify the originating memory event.
For WB, `core` denotes the originating CHA, sequence/ordinal are zero, and
command/response are unavailable. The replay assigns each row a unique local
service ID; WB rows must not be mistaken for architectural store callbacks.

The capture is not a new timing path, and frozen cache classification/MSHR
arrival is retained in the offline comparison. It does not establish fill,
merge, replacement, actual store-send or SQ correctness.

## Input And Validation

Artifact directory:
`tmp/lbm-projected-dram-20260914.wZPjBN/`.

Input:
`tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/`.
The complete 32-core `first-core-target-common-end-v1` manifest was replayed,
including functional warmup, with `user-plus-kernel` scope, Q=1024,
ordinary-load=3 and response-to-ready=1. No cold hotspot replay, new gem5
collection, workload-specific timing coefficient, or default switch was used.

The source identity and formal-oracle flags pass. The full captured run's
scope (excluding host throughput) and all threads equal the frozen default.
`analysis.json` retains binary, configuration, manifest and stream hashes.
The existing native service pairing supplies evaluation labels only.

Captured population:

| Phase | RD | WB |
|---|---:|---:|
| Functional warmup | 44,348 | 0 |
| Measurement | 4,020,282 | 3,077,110 |
| Total | 4,064,630 | 3,077,110 |

Measured RD/WB counts exactly equal the corresponding data plus instruction
CHA counters. All 7,141,740 rows are admitted exactly once by replay.
All reads receive a result; 3,076,673 writes are serviced and 437 remain
pending. Writes are not force-flushed at EOF.

## Fixed-Path Results

The proposed service below replaces only the captured DRAM duration in the
old request service. No core, cache or MSHR recomputation is performed.
Native Ruby service and FastSim transport boundaries are not cycle-identical.
MAE here is **paired request-service MAE in cycles**, not CPI MAE.

| Window | Stores | gem5 Mean | Old Mean | Candidate Mean | Old Service MAE | Candidate Service MAE |
|---|---:|---:|---:|---:|---:|---:|
| C13 3,127,618..3,137,618 | 315 | 303.07 | 647.88 | 549.83 | 375.78 | 289.64 |
| C1 2,000,000..2,010,000 | 105 | 271.04 | 255.69 | 334.10 | 62.84 | 128.08 |
| C1 8,000,000..8,010,000 | 92 | 294.66 | 322.86 | 431.77 | 121.26 | 205.96 |

All 315 C13 requests match source sequence and their previously captured
canonical command wait. Of these, 191 become faster, 113 slower, and 11 are
unchanged; 18 experience late-arrival clamping. The request at sequence
3,133,677 changes from 1,614 to 811 cycles, against native 235.

These windows were already used in diagnosis and are not held-out tests.
No improvement is claimed for the 430 SQ owners: production feedback coverage
is zero. Request delay sums are overlapping and cannot be added as CPI gains.

Across all measured reads, canonical command-wait sum is 616,041,762 cycles;
candidate wait measured from the original arrival is 1,173,747,773 cycles.
There are 1,304,134 late-clamped arrivals across both phases and directions.
The adverse C1 controls prevent promoting the C13 improvement.

Of measured reads, 651,234 are clamped, adding 253,148,264 cycles directly to
their effective arrivals. Waiting after those effective arrivals still totals
920,599,509 cycles, compared with the old 616,041,762. In the C1 2M control,
9/105 reads are clamped: mean direct clamp is 30.84 cycles, while mean service
increases by 78.42. In C1 8M, 5/92 are clamped: mean direct clamp is 7.08 cycles,
while service increases by 108.91. Removing only each request's direct clamp
would therefore not explain away the adverse controls. This is an accounting
decomposition, not a causal ablation: earlier clamping also changes the queue
and bank state seen by later requests.

The unchanged production result remains:

| Case | FastSim CPI | gem5 CPI | Signed Error | Absolute CPI Error | 32-Core CPI MAE |
|---|---:|---:|---:|---:|---:|
| C32 default, capture enabled | 4.200373 | 3.906000 | +7.5364% | 0.294373 | 0.321020 |

CPI error and unweighted per-core CPI MAE are cycles per macroinstruction.
The numerator is 1,008,609,550 core cycles and denominator 240,123,783 macro
instructions, with 308,016,035 measured user UOPs. This is compatibility, not
candidate CPI benefit.

## Cost And Tests

The first offline run spent about 7.05 seconds inside controller reservation,
with 15,706,480 event-loop iterations and 238,029,511 pending-entry checks.
This excludes CSV parsing/output and is not production throughput overhead.
The scanner is a mechanism prototype, not a proven low-cost implementation;
these costs and the failed controls preclude claiming a 3% or 5% budget pass.

Build and `fastsim_tests` pass. Controller ASan/UBSan passes with leak detection
disabled; LSan is not claimed. Directed tests cover read capacity release,
late arrival, per-channel independence, copy/rollback, low-water WB carry,
full write capacity at low=100, duplicate rejection and strict/projected API
isolation. A Simulator test checks capture does not change cycles/instructions
and captures actual dirty evictions with conserved counts.

Two development failures were corrected: the new test initially used the
wrong counter name (`instructions` instead of `retired_instructions`), then
its synthetic source lacked required virtual-page tokens. The fixture now
explicitly disables token requirements and DTLB; formal input settings were
not relaxed. Builds retain the existing serial-LTRANS warnings.

Default-off binary ABBA validation is recorded separately in
`benchmark-summary.json`; no enabled-candidate throughput result is claimed.

| Binary, Capture Off | Run 1 M User-UOP/s | Run 2 M User-UOP/s | Mean |
|---|---:|---:|---:|
| Pre-change | 4.140979 | 4.133671 | 4.137325 |
| Experiment build | 4.110855 | 4.116246 | 4.113551 |

The measured relative difference is -0.5746%. All four runs have identical
scope and thread results. Runs were sequential with CPU/memory node 0 binding,
with no other experiment launched by this session; the shared host load
average ranged approximately 3.0 to 6.7 at run boundaries. Two observations
per binary do not establish a general overhead bound. Both binaries are below
the project's 5M user-UOP/s floor in this environment.

These binary timings precede the final offline-only capacity-boundary fix and
additional diagnostic configuration rejection. Measured binaries are retained
as `fastsim-measured` and `replay-measured`; they must not be confused with a
later relink of `build/fastsim`. Final-build verification is recorded separately.

## Final-Build Verification

The final capacity-boundary correction permits forced low-water write draining
only when an actually due WB is blocked by the full write buffer. A blocked
read alone must not trigger unrelated write draining. The regression test
covers this case. Diagnostic capture also rejects additional unsupported
pending-service/replay combinations; production timing remains unchanged.

The final build, full `fastsim_tests`, and controller ASan/UBSan all pass
(`ASAN_OPTIONS=detect_leaks=0`). Final replay uses new output files,
`replayed-final.csv` and `replay-summary-final.json`, leaving the measured
artifacts intact. Its paired CSV is byte-for-byte identical to the original;
all summary fields except wall time are also identical. The controller-only
time for this repeat is about 7.03 seconds, not an end-to-end throughput result.
The capacity correction does not change any of the reported LBM pairings or
the failed control-gate decision.

The final `build/fastsim` also completes the full common-end C32 default run
with capture off. Scope metrics excluding host throughput and every thread
match `capture.json` exactly, which already matches the frozen default. CPI
and its absolute error/MAE therefore remain the compatibility row above.
This single final run is not a replacement for a final-binary ABBA experiment.
`final-verification.json` stores the repeat test commands/output, comparison
results, and final binary/source/artifact hashes. `verify-final.py` reproduces
the checks and uses exclusive output creation to preserve that record.
`git diff --check` also passes.

## Reproduction And Next Boundary

```sh
cmake --build build -- -j16
./build/fastsim_tests
./build/fastsim simulate \
  --config tmp/lbm-projected-dram-20260914.wZPjBN/capture.cfg \
  --manifest tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/tao_trace/manifest.txt \
  --measurement-scope user-plus-kernel --cores 32 --output CAPTURE.json
./build/fastsim_replay_projected_dram CONFIG REQUESTS.csv REPLAY.csv SUMMARY.json
```

Use a fresh output directory and capture path to retain earlier evidence.
`analyze.py` in the artifact directory verifies counts, current identities,
critical/control pairings and compatibility. `benchmark.py` runs sequential
NUMA-bound old/new/new/old binaries with capture disabled.

The next candidate must address the new waiting before production integration:
separate late-discovery clamping from queue-policy changes, preserve the
successful C13 effect without worsening C1, and avoid repeated pending-vector
scans. Do not add exact core continuation, reduce Q, or fit latency constants
to hide this result. Only a candidate passing these fixed-flow controls should
proceed to production feedback and end-to-end CPI/throughput validation.
