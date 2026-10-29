# X86 Large Memory Opt-in Implementation Plan

> Execute inline with superpowers:executing-plans. The user approved the described design and explicitly requested implementation; no repeated approval or automatic commit/push.

**Goal:** Add an explicit 32GiB-capable DDR4 FS path while keeping default and HBM components intact.
**Architecture:** Lazy-select two new gem5 stdlib subclasses behind a runner flag. Package new files in overlay and register their embedded Python sources with a separate patch.
**Tech Stack:** Python 3.11, gem5 v25.1.0.1, MESI_Three_Level, unittest, SCons.
**Spec:** `docs/superpowers/specs/2026-10-27-x86-large-memory-opt-in-design.md`

## Global constraints

- Preserve mixed worktree changes and old binaries/checkpoints; no commit/push.
- Never edit legacy `x86_board.py`, `memory.py` or `hbm.py` in the production tree.
- Default flag false; size <=3GiB uses legacy classes even when the flag is present.
- >3GiB requires the flag; active large memory rejects functional backing store and HBM.
- Build gem5 with Python 3.11 and floor(80% of affinity CPUs), minimum one.
- Keep experiments under project `tmp/`, FST total <=200GB; no accuracy scoring here.

## Review focus

- Default construction must not import optional components or rebuild controllers.
- A flag at only the Board layer must not leak multi-range logic into HBM.
- Wrong total, overlap, misaligned or interleaved input ranges fail before mutation.
- E820 high end and I/O hole must match controller address ranges.
- Checkpoint identity and packaged source registration must include the selected feature.

### Task 1: Runner policy and isolated components

**Files:** `scripts/run_gem5_sync_probe.py`; new overlay modules
`src/python/gem5/components/boards/x86_large_memory_board.py` and
`src/python/gem5/components/memory/x86_multirange_memory.py`;
`tests/test_x86_large_memory_options.py`; `tools/gem5/validate_x86_large_memory.py`.
**Interfaces:** `validate_x86_memory_options(args) -> bool`; new Board/Memory classes named in spec.

- [x] Write runner validation tests (default false, >3GiB opt-in, active backing-store rejection, small-memory legacy path); run RED.
- [x] Write real-gem5 checks for class selection, invalid ranges, controller preservation, 32GiB/N=2,8 and HBM; observe missing feature RED.
- [x] Implement policy and independent subclasses; preserve legacy files byte for byte.
- [x] Bind the requested option to checkpoint identity; run focused Python regressions GREEN.

### Task 2: Packaging and production integration

**Files:** `gem5-collection/patches/0002-fastsim-x86-large-memory-python.patch`,
`gem5-collection/manifest.json`, `gem5-collection/README.md`, `gem5-collection/CHANGELOG.md`,
local gem5 stdlib module copies and `src/python/SConscript` registration.
**Interfaces:** overlay modules compile into gem5; manifest lists ordered registration patch and feature hashes.

- [x] Add packaging identity/registration test and observe RED.
- [x] Package and install new modules without the old multirange patch; build isolated validation binary.
- [x] Run real gem5 compatibility checks and 32GiB KVM boot; preserve raw receipts.
- [x] Preserve original production binary, register/install modules in current gem5-fs and rebuild.

### Task 3: Documentation, verification and review

**Files:** runbook, this plan, design and project-owned experiment receipts.
**Interfaces:** concrete commands, default compatibility scope and explicit checkpoint/new-topology boundaries.

- [x] Run relevant Python suites and required `cmake --build build -- -j16`, `./build/fastsim_tests`.
- [x] Review default path, HBM isolation, range validation, checkpoint identity and packaging.
- [x] Record actual tests/results/limits in runbook; mark only verified tasks complete.

Ruling: implement in the user-requested current project; isolate gem5 experiments using the existing
independent copy. Do not create another worktree or commit unrelated pre-existing modifications.

Completion evidence: `tmp/x86-large-memory-opt-in-20261027/completion.json`, runbook section12.
Production builds/boot and actual staged checkpoint save passed. New-mode application restore/O3 and accuracy certification are not implied.
