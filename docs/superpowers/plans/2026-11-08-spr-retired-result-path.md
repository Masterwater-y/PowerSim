# SPR Retired Result Path Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans; preserve the existing dirty worktree.

**Goal:** Repair production SPR miss result propagation and bounded live-service lifetime, then verify each ordered gate in the specification.

**Architecture:** Record cache facts before completion, project the same fixed-size aggregate from either native source, and explicitly close replaced or canceled live attempt identities without invalidating shared services. Preserve unknown for missing evidence.

**Tech Stack:** gem5 C++/SLICC, FastSim C++, Python auditors, real FS capture.

**Spec:** `docs/superpowers/specs/2026-11-08-spr-retired-cache-result-path-repair-design.md`

## Global Constraints

Preserve two-stage timing, retired functional inputs, per-event OR and estimation flags. Never infer a missing observation or clear unknown. Keep storage guards and independent identity/native audits. C4 32GiB smoke uses 500K warmup and 50K target; four-case gate uses 10M and 500K. Build gem5 with gold and -j128 after preserving a working binary. Hardware equivalence remains unverified.

## Review Focus

- Actual L1 callback action order, including LL/atomic: exercise real transition paths and check literal miss bits.
- Request extension versus Snapshot, fragments, forwarding and boundary: assert projected macro outcomes and fail-closed unknown.
- Replaced/canceled request with a live TBE or follower: ensure bounded index and preserve independent consumer result.
- Missing admission and unsupported ISA: retain unknown until actual evidence proves membership.
- Repeated completion and stale: no double count, preserve valid miss and separate stale property.

### Task 1: Production path regression and repairs

**Files:** `tests/test_native_pio_lifecycle.cpp`, `tests/test_native_pio_lifecycle.py`, `gem5-spr/src/mem/taotrace_response.hh`, `gem5-spr/src/cpu/o3/probe/tao_trace.cc`, `gem5-spr/src/mem/ruby/protocol/MESI_Three_Level-L0cache.sm`, `gem5-spr/src/mem/ruby/system/Sequencer.cc`, `gem5-spr/src/mem/perf_cache_native.hh` and SLICC helpers if needed.

- [ ] Add production-component regressions proving L1 hit before completion, extension projection, 70K replacement/cancel operations, live shared follower, fragment and duplicate completion; run and record expected failures.
- [ ] Repair each defect with minimal observer-only changes, including bounded lifecycle diagnostics; run focused regressions and existing direct suites.
- [ ] Preserve old gem5 binary, build new gem5 with gold -j128, build FastSim -j16 and run `./build/fastsim_tests` plus relevant production tests.

### Task 2: Actual small FS and unknown qualification

**Files:** `tools/run_spr_retired_cache_smoke.py`, existing maintained auditors, new output directory and implementation record.

- [ ] Collect all three C4 modes, audit target identity, common-end, owner and per-core population, native response conservation and per-event certainty; report actual service path coverage.
- [ ] Investigate each residual eligibility/admission unknown with raw evidence; add narrowly targeted production regression and repair only established cause, then repeat affected gates.
- [ ] Proceed only if each required core and path has evidence, zero population and event unknown, and conservation holds.

### Task 3: Neutrality, replay and four-case acceptance

**Files:** `tools/compare_spr_retired_cache_neutrality.py`, `tools/run_native_kernel_fastsim_validation.py`, `tools/run_spr_retired_cache_campaign.py`, implementation record.

- [ ] Compare old/new observers from identical staged checkpoint with full source, population, execution and native diagnostic checks.
- [ ] Replay same sources using maintained validator and check shared policy, content, window, task and per-core populations.
- [ ] Only after prior gates pass, collect Marian, Graph500, XZ and FLAC C4; verify independent identity, native conservation, coverage and per-core zero unknown. Report CPI absolute error/MAE if scoring and measure throughput separately.
