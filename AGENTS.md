# FastSim agent notes

Read [`docs/codebase-workflow.md`](docs/codebase-workflow.md) before doing repository, commit, push, or workspace-recovery work.

## Core project objective

- FastSim trades some simulation accuracy for high throughput; throughput and
  the existing parallel two-stage execution framework are foundational design
  requirements, not optional optimization work.
- Improve SPR alignment within the existing two-stage framework using bounded,
  workload-independent approximations. Do not replace it with a cycle-accurate
  or per-cycle pipeline model, a separate detailed causal runtime, or repeated
  whole-window convergence. Keep model quality and end-to-end throughput
  separately measurable on the same workload and input.
- Retired functional instruction streams are the input; information must be
  obtainable from Atomic-level or QEMU functional execution. OoO gem5 may be
  used only for independent baseline measurements, not model timing input.

## Repository identity

- Codebase repository: `git@code.byted.org:yinhaolang/minesim.git`
- Current SPR project branch: `fastsim-spr` (orphan branch, requested 2026-11-03).
- All subsequent SPR work and pushes belong on `fastsim-spr`; `FastSim` retains the historical baseline.
- The repository contains several project branches; do not push SPR work to `FastSim`, `main`, or another simulator branch.
- This is a ByteDance Codebase repository, not a GitHub repository. Do not use GitHub/`gh` workflows unless the user explicitly asks for GitHub.

## Workspace caveat

In managed agent sessions, `/data00/yinhaolang/FastSim/.git` may appear as an empty read-only directory. Never initialize an unrelated repository or force-push around this condition. Follow the temporary-metadata workflow in `docs/codebase-workflow.md` and bind this directory as the work tree.

## Retired-stream input contract

- FastSim inputs are retired instruction streams. The SPR frontend must consume only retired functional instructions and functional companions.
- Do not introduce speculative/wrong-path traces or generate shadow instruction/data traffic for the SPR frontend.
- gem5 fetch/delivery/rename timestamps, DSB hit outcomes, and stall durations are diagnostic references only; never replay them as timing inputs.
- Keep macro instructions, functional UOPs, and allocation slots distinct. Missing fusion/microcode/code identity metadata must be reported explicitly rather than guessed.

## Temporary files

- User directive (2026-11-11): all subsequent project temporary directories,
  compiler/LTO scratch, test fixtures and isolated experiments belong under
  `/data00/yinhaolang/FastSim/tmp/`; do not create them under system `/tmp`.
- Before project commands, source `scripts/project_tmp.sh`, or invoke
  `bash scripts/project_tmp.sh COMMAND ARGS...`. It sets `TMPDIR`, `TMP` and
  `TEMP` locally without changing global shell configuration.
- Clean system `/tmp` only for verified FastSim objects. Preserve important
  version code and experiment evidence under project `tmp/` with file hashes;
  leave other projects, services and live files alone.

## Current SPR milestone and evaluation default

- User directive (2026-11-11): **SPR-M1 — Retired Prepare baseline** names the
  frozen R0/R1 `v2-wait` executable, SHA-256
  `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`.
- Subsequent SPR CPI/PMU/throughput evaluations default to
  `configs/spr-evaluation-default.cfg`, currently selecting the immutable
  `configs/spr-m1-retired-prepare.cfg`. `configs/gem5-spr-retired.cfg` uses the
  same alias. Historical generic/versioned profiles are explicit controls.
- Keep retired-store permission prepare enabled (`legacy_prepare_timing=true`),
  use `unified_parent_service=v2-wait`, disable native L1D/private-L2 hardware
  prefetch and pre-retirement `sprEarlyStorePrepare`, and disable the FastSim
  prefetch service/follower extensions. Preserve functional software-prefetch
  instructions; disabling prefetch does not authorize deleting retired members.
- Bind each new source's geometry/timings and thread-state plan separately.
  Native prefetch/early-prepare settings must be verified in its `config.ini`;
  the FastSim configuration cannot retroactively change an oracle.
- Freeze milestones by executable and effective-config identity, not a Git HEAD
  alone. Advance subsequent model milestones as SPR-M2, SPR-M3, etc.

## Default validation

```bash
cmake --build build -- -j16
./build/fastsim_tests
```

`ctest --test-dir build` currently reports no registered tests, so run `fastsim_tests` directly.

## Commit scope

Prefer source, headers, tests, tools, scripts, configs, README, and curated `docs/`. Exclude `build*/`, `Testing/`, `tmp/`, generated workload binaries, and bulk `results/` unless the user explicitly requests experiment artifacts.

## Accuracy reporting

Before any CPI/PMU/throughput evaluation, outlier diagnosis, or new evaluation script, read sections 0 and 8 of [`docs/accuracy-reporting-contract.md`](docs/accuracy-reporting-contract.md).

- Native-FS cache truth must come from conserved Ruby/SLICC native summaries through `tools/run_native_kernel_fastsim_validation.py`. Never use `taotrace-path-class-v3` cache fields in `kernel-events*.json` as native truth, even if named `private_l2_tag_misses`; missing native evidence must fail closed.
- Validate target hardware identity separately with `tools/validate_fs_oracle_identity.py`; the native validator's `passed` status is not an identity, accuracy-threshold, throughput, or strict hardware-PMU certification.
- Use `--source-root` and `--stats-dir` for existing-output rescoring. Reuse the maintained validator instead of duplicating reference selection in temporary scripts.
- Keep MAPE and WAPE distinct. Correcting report sources does not change model predictions and is not a new throughput measurement. Verify source/scope/coverage before proposing a model fix for an outlier.

Always include a CPI absolute-error column alongside relative error in future accuracy reports, including conversational summaries and tail-case tables: `abs(FastSim CPI - gem5 CPI)`, in cycles per macro instruction, not percent. Aggregate tables must also show CPI MAE. Follow [`docs/accuracy-reporting-contract.md`](docs/accuracy-reporting-contract.md) for metric scope and definitions.
