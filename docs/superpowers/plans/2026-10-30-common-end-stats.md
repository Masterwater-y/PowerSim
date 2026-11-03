# Common-end native statistics implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Recapture Marian C4 and Graph500 C16 raw native statistics at their frozen functional common end without changing the producer, checkpoint, or target timing.

**Architecture:** A statistics-only wrapper executes the byte-identical frozen runner with its original `__file__`, arguments, and restore validation. At the existing measurement reset it starts a priority90 queue0 observer on the detailed CPU clock lattice. The observer detects the producer's synchronous common-end CPI file closure and dumps at the actual end tick. An independent audit extracts that block and separately checks internal window validity and old-reference equality. Original exit/drain and final raw stats remain intact. Fixed-old-tick GlobalStatEvent scheduling was tested and rejected after staged KVM restore failed to reproduce the old absolute ticks; see the ledger.

**Tech Stack:** Python 3.11; existing frozen gem5 MESI_Three_Level producer and maintained capture guard.

**Spec:** User-authorized P1 in docs/translation-sequencer-diagnostics-results-20261030.md and the conversation of 2026-10-30.

## Global Constraints

- No timing/model changes, commit, push, old-source replacement, or checkpoint receipt edits.
- Use existing checkpoint and exact frozen producer/runner. Record wrapper and helper hashes separately; never claim the wrapper was present in the old run.
- Keep global FST below 400GB; stop at 395GB; preserve 30GB free (outer early guard at 31GB).
- Reserve 12GB Marian FST and 32GB Graph500 FST plus auxiliary headroom. Compress only new xlat sidecars with existing guarded workflow.
- A failed deterministic comparison leaves the snapshot uncertified; never move the reference boundary to fit it.
- Physical stats are end-of-tick observations, not the FST's retired-demand cohort. Completion/arrival populations and outstanding requests remain separate.
- P0 counterfactuals, new FB instrumentation, model fixes, full matrix and throughput are outside this P1 slice.

## Review Focus

- schedStatEvent adds simQuantum: a real frozen-producer probe must demonstrate the compensated dump tick.
- No matching dump, duplicate matching dumps, wrong reset, extra/missing cores, incomplete boundaries or unequal FST must fail closed.
- Checkpoint validation must run unchanged; wrapper provenance must be explicit.
- Host dump callbacks must not alter frozen functional content or timing; compare start/end, counts, hashes and native summaries after real recapture.
- A global snapshot includes same-tick physical events and excludes later completions; do not claim request-cohort equivalence.

### Task 1: Snapshot wrapper and independent audit

**Files:** scripts/run_gem5_common_end_stats.py; tools/common_end_stats.py; tests/test_common_end_stats.py.

- [x] Write failing tests for exact snapshot selection, late/duplicate/malformed dumps and shifted or incomplete per-core populations.
- [x] Implement the small pure-Python boundary/snapshot helper and gem5 wrapper.
- [x] Run unit tests and a real gem5 schedule probe with nonzero quantum.

### Task 2: Guarded recapture and deterministic acceptance

**Files:** tmp/common-end-stats-20261030/ frozen wrapper/helper, launch commands, receipts, audit outputs.

- [x] Freeze code and input hashes; validate checkpoint payloads and resource/storage headroom.
- [x] Run Marian and G500 into fresh directories with the unchanged frozen runner and producer under maintained storage/wall guards.
- [x] Audit the raw dump against frozen tick/interval; compare all per-core counts and whole FST hashes. Preserve failures and all old artifacts.
- [x] Run maintained source preparation and target identity checks where applicable, keeping scoring sources separate.

### Task 3: Review and report

**Files:** docs/common-end-stats-results-20261030.md.

- [x] Run appropriate runner/measurement tests and the repository default build/direct test gate.
- [x] Review changes and evidence; document same-window native mechanism statistics without promoting cohort or hardware equivalence.
- [x] Report exact achieved and missing gates. Do not claim overall CPI accuracy or throughput acceptance.

## Execution ledger

- User explicitly authorized implementation after the P1 explanation; proceed without another approval round.
- Ruling: Work in the supplied mixed FastSim workspace, using a new isolated experiment directory and only new source files. Preserve existing edits; no commit or worktree/index manipulation.
- Ruling: Use a wrapper around the frozen runner rather than relaxing checkpoint identity validation. No checkpoint receipt change is needed.
- Task 1: Snapshot selection/refusal tests and real nonzero-quantum producer probe passed. Default build passed; direct tests needed the process-local `/opt/gcc-11/lib64` runtime and then passed.
- Ruling: Fixed absolute-tick replay is rejected after staged KVM restore produced different O3 switch ticks for both cases. Stopped both initial captures cleanly and preserved them. No snapshot was certified.
- Ruling: Use queue0 priority90 polling on the detailed clock lattice to detect the producer's synchronously closed `oracle/cpi-core0.json`. Dump immediately at actual end. Validate nonzero queues are runtime-switched-out KVM CPUs, all detailed CPUs active on queue0, and the exact unchanged detailed clock. This adds host observation cost, not target events that alter CPU or memory state. Fresh-context source review found no fatal scheduling issue under these gates.
- Ruling: Internal same-window acceptance and frozen-reference equality are separate gates. Different KVM replay cannot be promoted to the old frozen CPI/native reference. Any mismatch remains explicit; no boundary/denominator rewriting.
- Review fix: Require functional-boundary cores to equal the account population, even if both reference and recapture omit the same last boundary. Added a failing regression, then fixed it.

- Task 2: Both dynamic captures completed, exact own-window snapshots/source/target gates passed, strict translation coverage passed 4/4 and16/16 with zero unknown. Both old-source determinism gates rejected; per-core counts, full FST hashes and native summaries differ. No old source or oracle was replaced.
- Task 3: Final report and completion.json written.508 protected input/tool hashes rechecked unchanged. No model change, commit, push, accuracy rescore, throughput acceptance or hardware-PMU certification.
