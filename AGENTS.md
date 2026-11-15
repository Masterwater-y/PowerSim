# FastSim agent notes

Read [`docs/codebase-workflow.md`](docs/codebase-workflow.md) before doing repository, commit, push, or workspace-recovery work.

Local adaptation (2026-11-15): this `calibration-spr-dev` source tree incorporates
upstream `f0920cf` / `ed87ff5` with documented calibration exceptions. The user's
subsequent instruction authorizes the completed short functional probes and
requires one current version, with no multi-version compatibility layer.
`build -> build-calibration-spr` is the only active build; rebuilt `build/fastsim`
has SHA-256 `28f06fd68871e9d0199361707a697fcf1961efc9ecdb254b476e367954852dac`.
Use the current M2/v2-wait configuration with the documented local calibration
overrides. Fourteen targeted functional invocations and six CLI checks passed
(four successful runs, two required rejections); full regression,
native collection, accuracy and throughput evaluation remain unverified.
The user subsequently requested removal of archives and redundant temporary
files. All contents of local `tmp/` were deleted, including source snapshots,
historical ELF archives, logs and test fixtures; the directory is now empty.
The final test summary and JSON receipt remain in
`/home/data00/xuhaoen/workspace_agent_env/fastsim-spr-唯一版本功能测试-20261115.*`.
The five inactive service prototypes and 66 historical binaries are no longer
present in the active implementation or in local temporary archives. Rebuild
additional CMake targets before using them. Dated upstream ELF hashes and
experiment paths below remain historical source records, not current local
binaries.
The later cleanup also removed four old candidate build directories, active-build
provenance/tmp/log artifacts, stale objects for 56 removed executables, and Python
cache files. Only `build -> build-calibration-spr` remains as the active build;
its current executables, static library and CMake metadata are retained.

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
- Current calibration development branch: `calibration-spr-dev`; publish it to `origin/calibration-spr-dev`.
- `origin/fastsim-spr` belongs to another developer. Use it as a read-only reference; do not push to or rewrite it without explicit user authorization.
- Keep the development branch's upstream on `origin/calibration-spr-dev`. `FastSim` retains the historical baseline; do not push calibration work to `FastSim`, `main`, or another simulator branch.
- This is a ByteDance Codebase repository, not a GitHub repository. Do not use GitHub/`gh` workflows unless the user explicitly asks for GitHub.

## Workspace caveat

In managed agent sessions, `/data00/yinhaolang/FastSim/.git` may appear as an empty read-only directory. Never initialize an unrelated repository or force-push around this condition. Follow the temporary-metadata workflow in `docs/codebase-workflow.md` and bind this directory as the work tree.

## Retired-stream input contract

- FastSim inputs are retired instruction streams. The SPR frontend must consume only retired functional instructions and functional companions.
- Do not introduce speculative/wrong-path traces or generate shadow instruction/data traffic for the SPR frontend.
- gem5 fetch/delivery/rename timestamps, DSB hit outcomes, and stall durations are diagnostic references only; never replay them as timing inputs.
- Keep macro instructions, functional UOPs, and allocation slots distinct. Missing fusion/microcode/code identity metadata must be reported explicitly rather than guessed.

## Temporary files

- Project temporary directories,
  compiler/LTO scratch, test fixtures and isolated experiments belong under
  `/home/data00/xuhaoen/repos/simulators/minesim/tmp/`; do not create them under system `/tmp`.
- Before project commands, source `scripts/project_tmp.sh`, or invoke
  `bash scripts/project_tmp.sh COMMAND ARGS...`. It sets `TMPDIR`, `TMP` and
  `TEMP` locally without changing global shell configuration.
- Clean system `/tmp` only for verified FastSim objects. Preserve important
  version code and experiment evidence under project `tmp/` with file hashes;
  leave other projects, services and live files alone.

## Current SPR milestone and evaluation default

- Upstream historical directive (2026-11-11): **SPR-M1 — Retired Prepare baseline** names the
  frozen R0/R1 `v2-wait` executable, SHA-256
  `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`.
- Subsequent SPR CPI/PMU/throughput evaluations default to
  `configs/spr-evaluation-default.cfg`, currently selecting the immutable
  `configs/spr-m2-rename-elimination.cfg`. `configs/gem5-spr-retired.cfg` uses the
  same alias. Historical generic/versioned profiles are explicit controls.
- Upstream historical directive (2026-11-12): enable bounded MOV / zero-idiom elimination
  as **SPR-M2 — Retired Prepare + Rename Elimination**. Its executable SHA-256 is
  `0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`;
  this is the immutable model-baseline ELF, originally installed at
  `build/fastsim`. Preserve SPR-M1 at
  `tmp/spr-m1-20261111/fastsim` and its immutable profile. Explicitly set
  `core.spr_rename_elimination=false` to roll back the candidate path.
  Generic non-SPR library defaults remain off. This default decision does not
  certify incomplete native PMU evidence or the unfinished 30-pair throughput gate.
- Upstream historical directive (2026-11-13): discard the third-round SPR config captures /
  Sequencer keys, FR-FCFS compact indices / FairEvent, and static lookup caches.
  Restore the proven first/second-round host optimizations; unconfirmed borrowed
  views are also excluded. `build/fastsim` now uses the host-optimized ordinary
  Release build with validated input preload enabled by default for `simulate`.
  Keep the same SPR-M2 model configuration; this is not SPR-M3. Freeze the old
  M2 ELF at `tmp/host-throughput-default-20261113/spr-m2-frozen`; executable and
  source identities are in `fastsim-host-default-rollout-20261113.json`.
  Use `--preload-validated-input false` or `FASTSIM_PRELOAD_VALIDATED=0` for
  streaming rollback. CLI overrides env 0/1; invalid values reject. Library
  callers without the env retain streaming behavior. Unsupported
  core/MMU/architectural-boundary companions require explicit streaming;
  never discard their fields. Historical phase3 PGO remains a separate
  source/profile-bound candidate, not the default.
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

## Host evaluation and throughput

- User directive (2026-11-12): exclude input initialization, preprocessing,
  source/integrity checks and input-validation work from the primary
  throughput denominator. Report their costs separately; keep full process
  wall time as a separate diagnostic. Functional warmup also stays separate.
- Streaming `measurement_seconds` excludes startup trace-open and
  source prechecks, but includes online companion validation. Until that
  work is moved outside the timed phase or independently excluded using
  wall-clock evidence, label this rate as legacy validation-inclusive
  throughput, not primary validation-excluded throughput. Never subtract
  worker CPU time or profile percentages from parallel wall time.
  The validated-preload path completes supported input/companion validation
  before `Simulator::run()` and permits validation-excluded measurement;
  confirm `HOST_VALIDATED_PRELOAD=true` and keep preparation, integrity,
  initialization, warmup and full-process costs separately.
- Upstream host evaluation used verified high-frequency CPU **52–63**, socket/NUMA
  node **1**, with the existing `performance` policy. Requalify local placement
  before any authorized evaluation. Use
  `python3 scripts/run_high_frequency.py -- COMMAND ARGS...`; it sources the
  project temporary-path convention and rejects unavailable/low-frequency
  cores before execution. Record actual working frequency for comparisons.
  Requalify placement explicitly if the host changes. Do not change global
  CPU governors or silently fall back to CPU 24–35.

## Default validation

```bash
source /home/data00/xuhaoen/.agent_cli_auth/env.sh
source scripts/project_tmp.sh
cmake --build build -- -j16
LD_LIBRARY_PATH="/opt/gcc-11/lib64:${LD_LIBRARY_PATH:-}" FASTSIM_PRELOAD_VALIDATED=0 ./build/fastsim_tests
```

Run the selected executables directly. Managed TRAE sessions may prepend an
older C++ runtime to `LD_LIBRARY_PATH`; explicitly select the GCC 11 runtime as
above. The 2026-11-15 short gate used `fastsim_tests --branch-only`,
`--fixed-work-only`, `--context-only` and eleven independent probe executables.
It did not run the full default gate.

## Commit scope

Prefer source, headers, tests, tools, scripts, configs, README, and curated `docs/`. Exclude `build*/`, `Testing/`, `tmp/`, generated workload binaries, and bulk `results/` unless the user explicitly requests experiment artifacts.

## Accuracy reporting

Before any CPI/PMU/throughput evaluation, outlier diagnosis, or new evaluation script, read sections 0 and 8 of [`docs/accuracy-reporting-contract.md`](docs/accuracy-reporting-contract.md).

- Native-FS cache truth must come from conserved Ruby/SLICC native summaries through `tools/run_native_kernel_fastsim_validation.py`. Never use `taotrace-path-class-v3` cache fields in `kernel-events*.json` as native truth, even if named `private_l2_tag_misses`; missing native evidence must fail closed.
- Validate target hardware identity separately with `tools/validate_fs_oracle_identity.py`; the native validator's `passed` status is not an identity, accuracy-threshold, throughput, or strict hardware-PMU certification.
- Use `--source-root` and `--stats-dir` for existing-output rescoring. Reuse the maintained validator instead of duplicating reference selection in temporary scripts.
- Keep MAPE and WAPE distinct. Correcting report sources does not change model predictions and is not a new throughput measurement. Verify source/scope/coverage before proposing a model fix for an outlier.

Always include a CPI absolute-error column alongside relative error in future accuracy reports, including conversational summaries and tail-case tables: `abs(FastSim CPI - gem5 CPI)`, in cycles per macro instruction, not percent. Aggregate tables must also show CPI MAE. Follow [`docs/accuracy-reporting-contract.md`](docs/accuracy-reporting-contract.md) for metric scope and definitions.
