# Ruby clean fetch functional-read repair plan

> **For agentic workers:** Use superpowers:executing-plans inline; one final independent review.

**Goal:** Recycle the reviewed 197 obsolete files and repair the proven clean-fetch
functional-read failure without launching the formal twelve-case matrix.

**Architecture:** Keep MESI transitions, memory timing and measurement boundaries unchanged.
In the MESI directory, IM is reached only from clean I by Fetch and waits for memory data;
memory remains authoritative during this interval. Expose that one state's functional
read permission as Read_Only. Preserve Busy for dirty writeback and DMA intermediates.
Ship the change as a new gem5-collection patch and build the local tree using128 jobs.

**Tech Stack:** Python3.11, gem5 SLICC, C++17, SCons.

**Spec:** `docs/spec-speed32g-c16-failure-diagnosis-20261027.md` and the user instruction
to recycle and repair; earlier instruction to withhold formal recollection remains active.

## Global constraints

- Delete only the two reviewed categories; preserve logs/statistics/checkpoints and accepted sources.
- No commits/pushes or overwriting immutable campaign producer/checkpoint identities.
- No generic Busy-to-memory fallback, guessed PID/PTE, error suppression or backing-store bypass.
- Keep source validation, accuracy claims and hardware equivalence separate.
- Work in the existing authorized tree and preserve its unrelated edits.

## Review focus

- Dirty MI and DMA ID/ID_W/M_DRD/M_DWR states must retain their functional behavior.
- A writable cached owner must take priority over the directory's readable clean memory.
- Pending dirty payload in buffers/network must remain visible without reading stale RAM.
- A truly missing line must still fail; functional writes must continue to update all copies.
- Only IM's permission mapping may change; protocol transitions and action sequences stay intact.

## Tasks

- [x] Validate all197 file identities/hashes and live references, mark each source nonreplayable,
  unlink exactly the reviewed files, journal/fsync every deletion, verify10 accepted sources unchanged.
- [x] Add `tests/test_ruby_clean_fetch_functional_read.py` and a C++ test fixture that execute
  SLICC-generated directory permissions together with the actual RubySystem read/write methods.
  Demonstrate RED for clean IM and cover the review-focus cases.
- [x] Change `MESI_Two_Level-dir.sm` IM functional permission only; add patch0003 and manifest/README.
  Regenerate through the actual SLICC compiler and observe GREEN.
- [x] Build gem5 with `-j128`, preserve original frozen producer, run required FastSim build/tests
  and related native/raw-syscall compatibility tests.
- [x] Run bounded Marian/FLAC diagnostic smoke with the new producer and correctly bound fresh
  checkpoints; no formal10M capture. Verify that identities/warmup/measurement pass the old failure
  phase and maintain strict source audits where data are retained.
- [x] Obtain a final independent review, resolve material findings, record exact outcomes and limits.

## Decisions

The narrow protocol permission correction replaces the earlier historical generic Busy-directory
fallback proposal: the proof applies to clean I->IM, whereas broad fallback would need assumptions
about unrelated DMA/dirty states. Atomic/timing actions and the IM state itself remain unchanged.
This is an implementation choice under the user's repair request, not a new compatibility guarantee.

FS diagnostics use100K warmup/target and discard FST through `/dev/null`; they create new checkpoints
bound to the new producer, then check identities, warmup, measurement opening and common-end.
They cannot serve as accepted formal sources or CPI/PMU accuracy results.

Build SHA256: `2cf2d525185e00ea4b84dfc692c487654e47092dfda5ed456639f279cdbc4e6a`.
Old frozen producer hash `24eb9d90...` reverified unchanged. FastSim default tests and40 related
Python tests passed. Independent review found no blocking issue; dynamic FS results are recorded below.

First FS diagnostics reached guest native identity ACKs without the original Ruby fatal, but both
hit the1800s wall limit before measurement opened: full short validation0/2. One automatic
source-only diagnostic retry per wall-limited case now uses7200s with the same verified producer,
checkpoint,100K warmup and100K target. FST remains discarded; formal sources remain10/12.
Read `fs-live-status.json` then `fs-verification.json` in the repair experiment for actual results.
Additional50% functional-access stress still fails with an old-producer-reproducible stale read
outside clean IM; this is explicitly documented, not counted as an acceptance pass.

Final bounded FS regression2/2 passed, published in `fs-verification.json`. Both producers exited0,
reached all milestones/common-end and passed independent identity validation. Measurement wall:
Marian217.376s, FLAC1179.365s; whole diagnostic wall2675.446s/5195.437s. No formal FST was
retained or added, so formal sources stay10/12 and full FST/native conservation remains untested
for these diagnostic outputs. All implementation-plan tasks are complete within this scope.
