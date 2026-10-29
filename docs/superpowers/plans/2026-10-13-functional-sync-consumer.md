# Functional synchronization consumer implementation plan

> Implement inline with executing-plans; user explicitly requested design, review, then implementation.

**Goal:** Consume the already collected synchronization facts as causal constraints in FastSim.
**Architecture:** Oracle-free preprocessing DAG, exact FST source anchors, marker-bounded producer handshake, corrected coordinator readiness, producer frontend floor.
**Tech Stack:** C++17; Python 3 standard library; existing FastSim tests and native validator.
**Spec:** docs/fastsim-functional-sync-consumer-design-20261013.md
**Global Constraints:** default off; preserve unrelated/untracked work; no new FST/copies; no gem5 time input; no hardware tuning; legal common-end tail retained.
**Review Focus:** causal cycles, lookahead deadlock, speculative private state, warmup seam, CPI elapsed/active distinction, late worker departure.

### Task 1: Oracle-free graph and compiler
- Add tools/functional_sync_graph.py and tools/compile_functional_sync.py; extend existing validator with functional_only without changing default observation audit.
- RED: Python graph/compiler tests for full/final barriers, cycles, late departures, migration and missing arrivals, oracle independence.
- GREEN: compile both existing probe and TeaLeaf into tmp/functional-sync-consumer; validate source scope without generating FST.

### Task 2: Sparse runtime and raw ordinal API
- Add include/fastsim/functional_sync.hpp and src/functional_sync.cpp; versioned strict plan parser, dependency readiness, event state, source ranges.
- Add TraceSource raw ordinal/population delegation and IntervalCoreModel frontend floor.
- RED/GREEN: C++ DAG parsing/readiness tests and wrapper ordinal test, including wait=0 and wait>0.

### Task 3: Coordinator integration
- Add sync.plan config (empty=off), supported-mode guards, initialization/source checks; stop producer after anchors, acknowledge at feedback-corrected retire frontier, treat stopped anchor as lookahead coverage.
- Preserve state/reset only measurement audit; verify EOF event population; output sync audit and elapsed/active sensitivity separately.
- RED/GREEN: end-to-end 2-core barrier with runtime memory between arrival/departure; rejected phase/source anchors; default OFF regression.

### Task 4: Real data validation, documentation, review
- Build and run ./build/fastsim_tests plus targeted Python tests. Verify old FS probe migration rejection and run accepted 4-core TeaLeaf with existing config/trace and a new output directory.
- Use maintained native validator and separate identity validator for source/denominator/native PMU scoring. Report CPI absolute error/MAE; wait-mode qualification; measured user-UOP throughput if run supplies it.
- Independent final code review; fix important issues; append evidence and design rulings to spec/report and ledger.

## Completion contract

Design reviewed and revised; default-off consumer implemented with causal tests and existing-data validation; report explains limits and evidence. User did not request push; keep changes reviewable in workspace.

## Execution outcome

Tasks 1–4 completed. Final exact-horizon/all-marker admission design supersedes initial predecessor-only proposal. See docs/fastsim-functional-sync-consumer-results-20261013.md and .tmp/sdd/functional-sync-consumer/ledger.md for RED/GREEN evidence, review rulings, unsupported-mode bounds and final native comparisons.
