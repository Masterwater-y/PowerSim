# Store response frontier experiment implementation plan

> **For agentic workers:** Use superpowers:executing-plans for this authorized in-session experiment. Keep all existing changes; no commit or push is requested.

**Goal:** Test whether feeding same-core store response dependencies into canonical shared service reduces the demonstrated LBM queue overcharge, with a default-off rollback switch.

**Architecture:** Add `core.store_response_frontier = false`. A per-core memory-head merge projects ordinary-store admissions above the previous distinct store response and the available retirement lower bound. Replay private caches, shared caches, MSHRs, DRAM banks and channels together, and return service duration measured from the new admission. Retain the projected frontier across epochs and warmup; fragments of one store share an admission floor and contribute a maximum response. This is a two-stage projected approximation, not actual-arrival closed-loop simulation: report horizon crossings and late visibility explicitly.

**Tech Stack:** C++17, existing CMake build, Python integration tests.

**Spec:** `docs/lbm-error-mechanism-20260914.md`, bank-parent provenance section; user authorization for limited interfaces/state and rollback on 2026-09-15.

## Global constraints

- No scalar DRAM latency discount, benchmark identity gate, native timing input, or default enablement.
- Preserve the rejected `dram.projected_feedback` and exact `core.store_post_commit_request` paths unchanged and disabled.
- Reject incompatible replay/pending-fill/DVFS configurations; disable private preview and FRFCFS timing-only replacement for the new schedule.
- The experiment preserves per-core functional memory order. Loads can follow a store admission without waiting for its response. This ordering approximation must be reported.
- Candidate is not promotable solely from local wait reduction. Require whole-run signed and absolute CPI error, four-case CPI MAE, and throughput comparisons.
- Isolated worktree plus full dirty-file/binary snapshots are under the directory named in `tmp/lbm-store-response-current-run.txt`.

### Task 1: Mechanism and rollback contract

Files: `include/fastsim/config.hpp`, `include/fastsim/types.hpp`, `src/config.cpp`, `src/main.cpp`, `src/simulator.cpp`, `tests/test_store_response_frontier.py`.

- [x] Write and run integration tests against the old binary; require the new enabled field and predecessor-response ordering, so the old binary fails before implementation.
- [x] Add config parsing, JSON and CLI switch, validation, counters, and persistent projection state. The off branch must execute the original code.
- [x] Implement one shared replay per accepted epoch: choose the earliest projected per-core head, advance store groups using response maxima, preserve all request identities/populations, calculate own latency after admission.
- [x] Test same-core predecessor response ordering across checkpoints, fragment grouping, deterministic execution, explicit false/default equivalence, warmup state, incompatible configs, and service-duration accounting.

### Task 2: Accuracy and throughput gate

Files: generated runners/results under the experiment directory; curated results in `docs/lbm-error-mechanism-20260914.md`.

- [x] Build isolated candidate and run `fastsim_tests` plus Python mechanism tests.
- [x] Run unchanged-input LBM C4/C8/C16/C32, candidate and off. Compare `scope_metrics` and matched active-cycle gem5 CPI; preserve first-core common end and macro denominators.
- [x] Gate decision: frozen-window reruns were not executed because complete C32 and all-core-count accuracy failed; `audits.py` is preparation only.
- [x] Run available non-LBM controls if the candidate passes the primary gate; if it fails, keep it disabled and document the rejection rather than widen a losing sweep.
- [x] Independent code review and all tests complete. Promotion rejected on full-run evidence: keep implementation isolated, preserve maintained source/binary, run required maintained build/tests and four-case rollback parity checks.
- [x] Document exact switch, results, limitations, artifact paths and next decision. No claim that summed removed waits equal recoverable CPI.

**Execution disposition:** completed as a rejected experiment. Ten full runs; all four on cases regress. CPI MAE 0.116875 → 36.209032 cycles/macro-instruction. No maintained implementation was changed; no commit/push. See the final report section and the experiment `README.md`.
