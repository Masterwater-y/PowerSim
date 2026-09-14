# Projected RD/WB Response Feedback Pilot

Date: 2026-09-14. Status: opt-in integration and five-case pilot completed;
C13 selected-window benefit survives, but end-to-end promotion is rejected.

## Scope

The user explicitly requested enabling the candidate that improved the
offline C13 services and testing benefits/regressions on several workloads.
This reopens the production-feedback experiment despite the earlier C1
fixed-input rejection. It does not change the maintained default or turn the
earlier failed control gate into a pass.

The previous candidate had no production timing switch.
`dram.projected_feedback=true` now enables its response-only integration;
`configs/gem5-exp-projected-dram-feedback.cfg` selects it. The setting defaults
to false. Ordinary-load=3, response-to-ready=1, Q=1024, parallel producers and
materialized core feedback remain unchanged.

## Approximation Contract

After the canonical shared pass accepts a batch, its unique RD and actual
dirty LLC WB records enter the same persistent projected controller used by
the offline experiment. The topology-scaled selection window is identical.
All reads receive a numerical service result, low-water writes remain owned,
and per-channel late arrivals are clamped and counted. Warmup retains this
controller state at the measurement reset.

The new response plus the original return transport replaces each unique
read's core-feedback latency. This includes architectural stores whose
acquisition is RD, ordinary loads, and instruction requests. Existing core
feedback propagates it into completion and store/SQ timing. A WB never becomes
an architectural store response. Dynamic core/sequence/ordinal ownership is
checked; unmatched or duplicate owners fail instead of silently bypassing.

This is **not** an actual-arrival or fill-consistent shared-memory controller:

- Cache classification, directory state, MSHR calendars and fill visibility
  remain those of the canonical pass. Merged followers still use canonical
  fill times, not the new parent response.
- Subsequent batches can change because core responses change; an enabled
  full run need not reproduce the earlier fixed-input C13 service result.
- Old canonical cache/queue PMU remain canonical. Candidate counters are
  separate; no claim is made that canonical queue PMU describes the new
  response calendar.
- There is no actual store-send resubmission, new global UOP event solver,
  additional full core-feedback pass, or latency/CPI fitting.
- The candidate replaces read-only FRFCFS repair but retains the canonical
  controller needed by the frozen cache/MSHR path. Its cost is not a
  replacement-controller throughput result.

Capture-only behavior is unchanged. Accepted records can also be captured
while feedback is enabled; the CSV remains the canonical stream, not the
candidate output. The projected controller runs only after shared transactions
have selected the accepted batch. Rejected transactions truncate the record
buffer before it is consumed.

Configuration rejects unsupported corrected-arrival/pending-service modes,
multi-pass reweave, ROB-head suffix replay, non-reference core frequencies and
non-FRFCFS or non-separated write queues. Runtime DVFS also fails closed.
Strict cross-Q guards are unchanged.

## Validation

Build and `fastsim_tests` pass. The new integration test checks:

- Enabled feedback changes target cycles and preserves retired instructions.
- Every unique RD has one core response owner; actual WB enqueue/service/tail
  counts conserve.
- FRFCFS repair is not also applied, and there is one core feedback call per
  interval step.
- Repeats are deterministic, capture does not alter enabled timing, and
  unsupported frequency configurations are rejected.

The first build exposed an incorrect test counter name,
`interval_checkpoints`; this was corrected to the existing `interval_steps`.
Existing serial-LTRANS warnings remain.

The first pilot runner incorrectly read activation counters from `totals`
instead of `causal_frontier`. Five off runs completed, but validation stopped
before any on run. Their outputs and the failure summary are retained in the
artifact root. The corrected runner starts fresh under `validated/`, with no
simulator changes or discarded enabled result.

## Experiment

Artifact directory: `tmp/projected-feedback-20260914.52nKLm/`.
Runner: `run-pilot.py`. The measured CLI is frozen as `fastsim-measured`.
Input root: `tmp/first-core-common-end-20260911/source/`.

Five complete `first-core-target-common-end-v1` configurations, 56 per-core
traces, use functional warmup and `user-plus-kernel` scope:

- LBM C32 and C8: workload used in diagnosis, not held out.
- TeaLeaf C4, Graph500 C8, Stockfish C4: transfer controls, not a formal
  workload-held-out accuracy claim.

Each case uses the same binary/configuration/input except for the switch.
Source identity/oracle conservation flags are checked. Off/on macro and UOP
populations must match. Measured RD/WB counts must equal data plus instruction
CHA counts; pending writes preserve initial + enqueued = serviced + final.
Repeated LBM scope/thread results must match exactly.

Runs are sequential, with CPU/memory binding to NUMA node 0. LBM C32 uses
off/on/on/off; the others use one off/on pair. Shared-host load and commands
are recorded. Single AB pairs are cost observations, not stable overhead
bounds. No candidate promotion or throughput-floor pass follows merely from
these runs completing.

All five cases passed current source/oracle identity checks, with zero input
rejections or simulation failures in `validated/`. Every off run matches its
frozen maintained baseline in scope metrics excluding host throughput and all
thread results. Every on run conserves the off run's per-thread work,
scope-matched macroinstructions and user/native-kernel UOP populations.
Accuracy uses only `scope_metrics` for CPI; `causal_frontier` holds mechanism
activation, not an alternative CPI population.

Measured CLI SHA256:
`4d89f6f2b4d425821620f079ff1b04ef4a694b5acdb5e19586f9a4bcf490a1ce`.
The final build still matches this frozen binary. `verification.json` retains
source, include-chain configuration, binary, script and output hashes.

All manifests are record-bounded `fastsim-binary-warmup-slice` inputs.
The common reset excludes the warmup prefix from timing/work statistics while
retaining cache, dependency and controller state, including the projected
controller. Scope trace-work counts below are not interchangeable with PMU
retired-UOP counts.

| Case | Warmup Records/UOPs | Measured User UOPs | Measured Mixed Trace UOPs |
|---|---:|---:|---:|
| LBM C32 | 2,250,706 | 308,016,035 | 311,919,003 |
| LBM C8 | 698,020 | 70,509,070 | 71,429,649 |
| TeaLeaf C4 | 5,509,900 | 38,871,873 | 39,023,664 |
| Graph500 C8 | 36,388,636 | 63,608,431 | 74,405,454 |
| Stockfish C4 | 5,691,971 | 38,282,858 | 38,527,396 |

## End-To-End Accuracy

All CPIs below are native `user-plus-kernel` macroinstruction CPIs, not
cycles/user-UOP. Per-case CPI is total core cycles divided by total completed
scope-matched macroinstructions. CPI absolute error and CPI MAE are in cycles
per macroinstruction. No user-only pipeline or formal PMU accuracy claim is
included in this pilot.

| Case | gem5 CPI | Off CPI | On CPI | Signed Error Off -> On | Absolute CPI Error Off -> On |
|---|---:|---:|---:|---:|---:|
| LBM C32 | 3.906000 | 4.200373 | 5.370057 | +7.5364% -> +37.4823% | 0.294373 -> 1.464057 |
| LBM C8 | 3.202265 | 3.261426 | 3.291136 | +1.8475% -> +2.7753% | 0.059161 -> 0.088871 |
| TeaLeaf C4 | 1.096636 | 1.039566 | 1.034298 | -5.2041% -> -5.6844% | 0.057070 -> 0.062338 |
| Graph500 C8 | 1.828677 | 1.712414 | 1.716544 | -6.3578% -> -6.1319% | 0.116263 -> 0.112133 |
| Stockfish C4 | 0.662666 | 0.660258 | 0.660251 | -0.3634% -> -0.3645% | 0.002408 -> 0.002415 |

There is one improving configuration and four worsening ones; Stockfish's
change is negligible in magnitude. C32 off/on repeats have exactly equal
scope and thread results within each mode, so its timing-model regression is
reproducible rather than host measurement noise.

Configuration-equal aggregates, with Type-7 APE percentiles:

| Split | N | MAPE Off -> On | CPI MAE Off -> On | APE P50 Off -> On | APE P90 Off -> On | APE P99 Off -> On | Max APE Off -> On |
|---|---:|---:|---:|---:|---:|---:|---:|
| LBM diagnostic | 2 | 4.6920% -> 20.1288% | 0.176767 -> 0.776464 | 4.6920% -> 20.1288% | 6.9675% -> 34.0116% | 7.4795% -> 37.1352% | 7.5364% -> 37.4823% |
| Transfer controls | 3 | 3.9751% -> 4.0603% | 0.058580 -> 0.058962 | 5.2041% -> 5.6844% | 6.1270% -> 6.0424% | 6.3347% -> 6.1230% | 6.3578% -> 6.1319% |
| Combined pilot | 5 | 4.2618% -> 10.4877% | 0.105855 -> 0.345963 | 5.2041% -> 5.6844% | 7.0650% -> 24.9421% | 7.4893% -> 36.2282% | 7.5364% -> 37.4823% |

The pilot JSON's `cpi_wape_percent` and `cpi_signed_bias_percent` are
unweighted ratios of per-configuration CPI sums. They are diagnostic only,
not the instruction-weighted WAPE/bias required for a formal report.
No workload-held-out generalization claim follows from this selected pilot.

Within each case, unweighted per-core CPI MAE changes as follows:
LBM C32 0.321020 -> 1.544623; LBM C8 0.070196 -> 0.113199;
TeaLeaf C4 0.057210 -> 0.062454; Graph500 C8 0.114576 -> 0.110347;
Stockfish C4 0.002419 -> 0.002424.

## C13 Benefit And Its Limit

A separate complete C32 enabled run uses the generic core kernel and records
core 13 sequences 3,127,618 through 3,137,618 at stride one. Its scope metrics
excluding throughput and every thread exactly match the enabled materialized
production run. This audit is excluded from throughput measurements.

The same 315 dynamic stores retain their PCs and DRAM path. All 430 previous
SQ-release owner relations are retained, but this is not proof that every
owner or all absolute send/fill times are correct. Service boundaries remain
native Ruby admission/response versus FastSim's own transport convention.

| Same 315 Stores | Native | Default Off | Prior Fixed-Input Candidate | Enabled Production |
|---|---:|---:|---:|---:|
| Mean service, cycles/request | 303.07 | 647.88 | 549.83 | 461.28 |
| Paired service MAE, cycles/request | 0 | 375.78 | 289.64 | 230.10 |

Against the old services, 219 are faster, 92 slower and 4 unchanged.
For example, store sequence 3,133,677 has native service 235 cycles:
default 1,614, prior offline candidate 811, enabled production 311.
The selected retirement window changes from 208,950 to 149,753 cycles against
native 99,099; its excess shrinks from 109,851 to 50,654 cycles.
These are diagnostic window/service measurements, not additive CPI gains.

The local benefit does not extend to the whole core. Core IDs below refer to
threads inside the C32 run, not one-core configurations:

| C32 Thread, Full Window | gem5 CPI | Off CPI | On CPI | Signed Error Off -> On | Absolute CPI Error Off -> On |
|---|---:|---:|---:|---:|---:|
| Core 1 | 3.833358 | 4.092249 | 5.247622 | +6.7536% -> +36.8936% | 0.258891 -> 1.414264 |
| Core 13 | 3.915613 | 4.385258 | 5.507746 | +11.9942% -> +40.6612% | 0.469645 -> 1.592134 |

Thus this is not simply a C1 regression cancelling an otherwise uniformly
improved C13. Even C13's full-window error grows despite its selected hotspot
improving.

## Activation And Remaining Mechanism

Measured candidate counts:

| Case | RD Responses | Ordinary Store RD | Instruction RD | Dirty LLC WB | WB Serviced | WB Pending Final |
|---|---:|---:|---:|---:|---:|---:|
| LBM C32 | 4,020,341 | 3,014,652 | 3,746 | 3,058,813 | 3,058,343 | 470 |
| LBM C8 | 986,827 | 738,978 | 2,661 | 12,251 | 11,790 | 461 |
| TeaLeaf C4 | 232,499 | 66,909 | 659 | 0 | 0 | 0 |
| Graph500 C8 | 221,122 | 56,503 | 87 | 7 | 0 | 7 |
| Stockfish C4 | 11,352 | 2,606 | 728 | 0 | 0 | 0 |

Initial pending writes are zero in these five measured runs. For each case,
initial + enqueued = serviced + final; low-water tails are not force-flushed.
RD counts equal data plus instruction CHA reads and core-feedback owners.
Every enabled run has zero original FRFCFS candidate epochs and exactly one
core-feedback call per interval step.

Within the C32 enabled run, compared with its own canonical responses,
937,044 requests get shorter latency and 1,748,935 get longer latency.
Saved latency sums to 84,405,715 request-cycles and added latency to
504,699,169. There are 728,842 late-clamped arrivals across RD/WB, totalling
218,447,794 clamp cycles. These are different accounting populations; neither
their sums nor subtraction establish a CPI contribution or causal ablation.
They also are not a request-by-request pairing of the full off and on runs.

This candidate reserves all reads of a discovered batch before later batches
arrive. Previously committed per-channel reservations cannot be revised, so
later-discovered earlier requests may be clamped. Mixed-direction policy also
changes waiting after admission. Subsequent producer batches then change
under core feedback while cache/MSHR/fill state still follows the canonical
pass. These are concrete approximation boundaries, not a demonstrated
single-cause explanation of the regression.

Even Graph500's local candidate responses are never slower than its canonical
responses in the enabled run, yet its total cycles increase. Later batch and
cache-path changes mean local latency-counter signs do not determine the
end-to-end CPI direction. Canonical PMU differences are retained in the pilot
JSON as diagnostics, not promoted to mixed-controller PMU accuracy.

## Host Throughput

Units are million measured user UOP/s, excluding functional warmup. C32 uses
the mean of its two runs per mode; other cases have one observation per mode.
The one-minute host load average at run boundaries ranges from 4.05 to 6.82.
These are sequential shared-host observations, not quiet-host overhead bounds.

| Case | Off M User-UOP/s | On M User-UOP/s | Change | Measurement Seconds Off -> On | Process Seconds Off -> On |
|---|---:|---:|---:|---:|---:|
| LBM C32 | 4.090104 | 3.532271 | -13.64% | 75.308 -> 87.201 | 76.839 -> 88.703 |
| LBM C8 | 4.214468 | 3.935852 | -6.61% | 16.730 -> 17.915 | 17.198 -> 18.411 |
| TeaLeaf C4 | 6.446498 | 6.249158 | -3.06% | 6.030 -> 6.220 | 6.639 -> 6.832 |
| Graph500 C8 | 7.344244 | 7.196282 | -2.01% | 8.661 -> 8.839 | 13.120 -> 13.476 |
| Stockfish C4 | 7.949229 | 7.971908 | +0.29% | 4.816 -> 4.802 | 5.741 -> 5.715 |

Process seconds include warmup and CLI overhead. Stockfish's single-pair
+0.29% is not evidence of a stable speedup. No throughput-floor acceptance is
claimed: both LBM configurations already fall below 5M when off and become
slower when on.

C32 repeat distribution:

| Mode | N | Mean | Minimum | P50 | P90 | P99 |
|---|---:|---:|---:|---:|---:|---:|
| Off | 2 | 4.090104 | 4.086586 | 4.090104 | 4.092919 | 4.093553 |
| On | 2 | 3.532271 | 3.526538 | 3.532271 | 3.536857 | 3.537889 |

For each single-run case, mean/minimum/P50/P90/P99 equal its one rate;
`verification.json` records these without implying a sampled distribution.
The C32 first on run spends 9.391 seconds in projected reservation, excluding
owner lookup/core propagation. Canonical service computation is still
required. This explains a substantial observed cost center, but is not an
independent causal decomposition of the entire throughput loss.

## Decision And Reproduction

Candidate rejected and disabled by user decision (2026-09-14). The opt-in code
and failure evidence are retained, but `configs/gem5-exp-projected-dram-feedback.cfg`
now sets `dram.projected_feedback = false`; maintained defaults were already off
and stay unchanged. The selected C13 service and window benefit is real under
the stated approximation, but this candidate fails end-to-end accuracy and
cost promotion, so it must not ship enabled. Do not expand to the 40-case
matrix or describe the five passing executions as a model-accuracy gate pass.

Default-off verification after disabling reproduces the frozen baseline exactly:
LBM C32 macro CPI 4.200373 (+7.5364%, abs 0.294373 cycles/macro-instruction,
32-core CPI MAE 0.321020), with `projected_dram_feedback_enabled=false` and all
projected batches/reads/writes at zero
(`tmp/projected-feedback-disabled-20260914.no4tJO/verify.json`).

The next bounded experiment should separate late-discovery delay from
mixed-queue selection effects on fixed request streams, retaining C1 controls
and C13 witnesses, then repeat closed-loop verification. Cost work should
target controller scans/ownership lookup and duplicated canonical/projected
service work. Do not select requests by workload/PC, clamp changes to only
speedups, reduce Q, inject gem5 timing or automatically return to a global
UOP event solver to conceal this result.

```sh
./build/fastsim simulate \
  --config configs/gem5-exp-projected-dram-feedback.cfg \
  --manifest tmp/first-core-common-end-20260911/source/formal-32c-782.lbm_r/tao_trace/manifest.txt \
  --measurement-scope user-plus-kernel --cores 32 \
  --output NEW_OUTPUT.json
```

Using `configs/gem5-fs-native-kernel.cfg` selects the unchanged off baseline.
The experimental switch is `dram.projected_feedback`; it is now `false` in
`configs/gem5-exp-projected-dram-feedback.cfg`. Set it to `true` only to
reproduce the rejected pilot for further diagnosis, never as a shipped default.

Evidence under the artifact directory:

- `validated/pilot-summary.json`: five cases, raw commands, ABBA/AB timing,
  activation, per-case CPI/error/MAE and split aggregates.
- `validated/<case>/<index>-<mode>.json`: every full simulation output.
- `c13-audit.json`, `c13-analysis.json`: production-equivalent audit and all
  315 paired store services.
- `verification.json`: independent raw-result identity, frozen-off parity,
  per-thread work and RD/WB conservation, repeat and generic/materialized
  parity, timing distributions and hashes.
- `run-pilot.py`, `analyze-c13.py`, `verify-pilot.py`: reproducible checks;
  use fresh output paths because exclusive creation preserves earlier runs.

Final build and full `fastsim_tests` pass. No new integration sanitizer or
quiet-host performance claim is made.
