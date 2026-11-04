# SPR ASID Maps and Task Clock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Repair multi-ASID static input and add independently audited gem5 task active-cycle evidence without changing SPR timing structures.

**Architecture:** Versioned ASID static maps preserve legacy compatibility. An opt-in native counter observer and paired functional lifecycle cuts establish a new explicit clock contract; model clocks are independently computed. Legacy proxies cannot become strict references without native evidence.

**Tech Stack:** C++17, gem5 probe listeners/PyBind, Python3.11, native FST readers and maintained validators.

**Spec:** docs/superpowers/specs/2026-11-04-spr-asid-task-clock-design.md

## Global Constraints

- Work in parent fastsim-spr and child gem5-spr/yinhaolang; preserve old gem5-fs and unrelated changes.
- No non-probe SPR src timing/structure edits; source protection manifest verifies this.
- Native cycles/cache/service outcomes remain oracle-only; no wrong-path inputs.
- Unknown evidence refuses scoring; hardware perf equivalence is separate.
- Explicit source/test/doc staging; portable child overlay against02119d1, no unpublished gitlink or upstream gem5 push.

## Review Focus

- ASID aliases, context return and changed same-key definitions cannot select another process's facts.
- Native counter wake/order/tail semantics must match BaseCPU, not elapsed-time guesses.
- Lifecycle cuts, macro denominators and common-end closure are independently bound to FST.
- Old certificate booleans cannot certify new counter evidence.
- Source and model clock domains/populations agree before publishing CPI errors.

### Task1: Versioned ASID maps
- [x] Add failing native and production-method tests for A→B→A, missing/invalid map and conflicts.
- [x] Update include/fastsim/trace.hpp, src/trace.cpp and Python static-map auditor for v4/feature12 and current-ASID lookups.
- [x] Update only static-map portions of gem5-spr probe and native collection option.
- [x] Run legacy/new reader and producer tests; independent review.

### Task2: Native clock source
- [x] Audit exact kernel machine-code cuts and existing native CPU counter probes.
- [x] Add plain native ledger tests before implementation; cover sleep/wake, sampled/pending tails, overflow/frequency/duplicate cuts.
- [x] Add probe-only listener/cut capture and optional profile proofs/configuration. Keep asm scheduler ownership unchanged.
- [x] Emit only functional cut facts into input; independent native clock ledger under oracle/.

### Task3: Independent model accounting and audit
- [x] Add failing lifecycle plan/model counter/population and legacy non-promotion tests.
- [x] Extend functional thread-state graph/plan with8/9 cuts and optional contract; reject timing pollution and invalid pairing.
- [x] Integrate separate eligibility accounting into corrected-retirement coordinator, preserving old proxy outputs.
- [x] Audit native ledger and exact macro populations, extend maintained time-accounting/validator under new explicit contract, retain null for incomplete evidence.

### Task4: Integrate and publish
- [x] Build FastSim default targets/tests and native gem5 using affinity80% jobs.
- [x] Capture fresh C4 sources; audit ASID maps/functional companions, task clock provenance and denominator; replay both plans.
- [x] Run maintained identity/native validation, report CPI absolute error/MAE where available and all remaining gaps.
- [x] Verify protected sources, review final diff, commit child and parent portable patch/docs; push parent fastsim-spr only.

## Verification record

Implementation and both C4 captures completed. Child commit2c8250127d; 7363 protected sources unchanged; both maintained native source validations passed. Fixed warmup CPI scored with per-core denominator167120, absolute error0.0649293921 cycles/macro; OpenMP multi-ASID maps and963events consumed, CPI unavailable due unobserved/unknown ROI clock starts. See docs/spr-asid-task-clock-repair-20261104.md and its summary.json. Parent implementation commit8a804a2f9590a63b8f44b629b7a4b78a79bfa45c was selectively published to fastsim-spr and remote HEAD independently verified. Child upstream was not pushed.
