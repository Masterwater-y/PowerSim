# Optional DRAM refresh implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline. User has
> explicitly authorized implementation; do not repeat an approval handoff.

**Goal:** Add default-off rank refresh and measure mechanism, CPI, PMU and host cost.

**Architecture:** Tick-precision rank calendars inside the existing compact
DRAM scheduler; immutable candidate estimates and existing two-stage feedback.
No global iteration or extra functional classification. Preserve old behavior
when disabled and preserve state through snapshots and measurement reset.

**Tech Stack:** C++17, CMake, Python3, existing gem5 controller oracle.

**Spec:** ../specs/2026-11-03-dram-refresh-design.md

## Global constraints

- Default option false; captured tREFI/tRFC, explicit tick scale and startup phase.
- No workload-specific constants, latency fitting, global iteration or default promotion.
- Preserve existing mixed workspace; snapshot touched files; no commit/push.
- Use native validator and frozen same-window sources; label physical/request/
  retirement units separately. CPI tables require absolute errors and MAE.

## Review focus

- Candidate probes must not advance another candidate's rank state.
- A blocked rank must not block an available rank on the same channel.
- PRE legality, already-scheduled data and closed-bank readiness survive refresh.
- Fractional-cycle phase, long idle gaps and epoch/state copies must not drift.
- Disabled-path neutrality and unsupported mixed-controller rejection are explicit.

## Task1: Option and calendar

Files: config.hpp/config.cpp; new dram_refresh.hpp; new test_dram_refresh.cpp.
- [x] Write production-probe refresh-boundary assertion using config keys; observe RED.
- [x] Implement explicit option/parameters and validated rank calendar; test boundary,
  closed/open banks, fractional ticks, multiple periods, copying and overflow.
- [x] Add JSON configuration provenance; disabled path retains numerical fields.

## Task2: Controller integration

Files: simulator.cpp, simulator.hpp/types.hpp and test hook/probe as needed.
- [x] Gate rank selection; preserve immutable estimates and existing pass limits.
- [x] Track issued data/PRE constraints, close rows at refresh, retain other ranks.
- [x] Test cross-rank scheduling, epoch persistence, reset and default neutrality.
- [x] Expose refresh counters with explicit lazy-observation horizon semantics.

## Task3: Oracle validation

Files: existing controller tools; experiment artifacts in tmp/dram-refresh-20261103/.
- [x] Record native refresh preparation/REF/end debug events on a short reference.
- [x] Compare boundary, idle and loaded flows with existing gem5; isolate known PRE
  and mixed-write differences instead of tuning a mean to fit.
- [x] Verify captured parameter identity and describe approximation boundaries.

## Task4: CPI and PMU

- [x] Build default targets; run fastsim_tests and relevant Python checks.
- [x] Replay off/on with same final binary and frozen P1 config/manifest; require
  off==frozen target fields, allowing only named new metadata/counters.
- [x] Score with maintained validator --source-root/--stats-dir and identity checks.
- [x] Report pilot CPI absolute/relative error and aggregate metrics, retired-load
  misses plus separately labeled request diagnostics; preserve coverage caveats.

## Task5: Throughput and review

- [x] Run alternating sequential OFF/ON paired repeats with same affinity and no
  diagnostics; save measurement, warmup and end-to-end time and host load.
- [x] Check model determinism across repeats; report throughput change and3% gate.
- [x] Fresh read-only review, address important findings, write curated report and
  implementation receipt. Keep default off; no six-case promotion if pilot worsens.

Completed2026-11-03: implementation and pilot validation finished; default remains false. CPI MAE worsened, PMU essentially unchanged, average throughput -1.85%/-3.09% with single-pair outliers beyond3%. Validation completed does not imply promotion gates passed. See ../../dram-refresh-results-20261103.md.
