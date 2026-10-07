# C4/C8 Offline Collection Implementation Plan

> **For agentic workers:** Implement the approved tasks in order; use requesting-code-review before delivery. The user already requested the document and runnable script; no new design approval is needed.

**Goal:** Deliver the documented 136-case common-end offline collection pipeline for the user to run.

**Architecture:** Reuse existing hardware mapping, KVM checkpoint preparation, common-end FST promotion, identity validation, and native-sideband audit. A new collector owns scheduling, frozen provenance, exact resume checks, process cleanup, and complete-set reporting; a shell entrypoint exposes background operation.

**Tech Stack:** Python standard library (3.10+ runtime), Bash, installed gem5/TCSim tooling.

**Spec:** `docs/uarch-generalization-c4-c8-20260917.md`.

## Global Constraints

- Exactly 17 profiles × C4/C8 × 4 workloads = 136 formal cases; 8 baseline cases, 64 single-factor cases, 64 joint cases.
- Formal target 10,000,000 measured user UOP, smoke target 10,000, functional warmup, native kernel records, first-core common end.
- No new simulation or dataset deletion during implementation; user runs the delivered entrypoint.
- Keep existing unrelated workspace modifications; do not commit or push automatically.
- Default concurrency: up to 136 samples, CPU/memory constrained; separate KVM and finalization concurrency.
- Preserve maintained native source selection; never feed timing labels into FastSim.

## Task 1: Matrix and pure planning contracts

Files: new `configs/spec2026-uarch-generalization-c4-c8-v3.json`, new `tests/test_uarch_common_end_collection.py`, new `tools/collect_uarch_common_end.py`; minimal v3 static-preflight support in `tools/collect_spec2026_uarch_fs.py`.

- [x] Write failing tests for exact task coverage, wrong LLC-bank conversion, duplicate/incomplete design, CPU/memory job selection, and direct gem5 common-end command arguments.
- [x] Add the literal matrix from the spec and implement `validate_design`, `tasks_for`, `choose_jobs`, and command builders.
- [x] Run `python -m unittest discover -s tests -p test_uarch_common_end_collection.py`; fix failures without changing the agreed experiment.

## Task 2: Collection, lifecycle, and resume

Files: `tools/collect_uarch_common_end.py`, `scripts/collect_uarch_c4_c8_offline.sh`, and the same test file.

- [x] Add tests that a changed contract cannot resume, missing artifacts cannot count as complete, a timed-out child process group is terminated, and partial coverage never produces complete status.
- [x] Implement installed-resource preflight and runtime fingerprinting; use the retained common-end gem5 binary with a project-owned compatibility root for checkpoint preparation.
- [x] Prepare target-specific checkpoints with the existing runner's `prepare` action; invoke gem5 directly for capture with `any-core`, then use the maintained finalizer and source/identity auditors.
- [x] Persist commands, actual populations, target configuration, per-case replay config and inventory. Gate resume on the same contract and validated files.
- [x] Implement background `start`, foreground `run`, `status`, `stop`, retries limited to recognized transient restore failures, and disk guard. Keep all child groups owned by the collector.
- [x] Test the real subprocess wrapper with short local child fixtures; do not run a benchmark.

## Task 3: Documentation, real preflight, review and verification

Files: experiment spec above and collector tests.

- [x] Run shell syntax and Python tests, then actual `preflight` over the installed resources for all 136 cases without starting gem5 sampling.
- [x] Verify the current host default parallelism, mirror the implemented CLI/output layout in the experiment document, and record limitations accurately.
- [x] Request an independent read-only code review while running the default repository build/test gate.
- [x] Fix material findings and rerun affected checks. Deliver the document link, script link, and exact user launch/status commands.
