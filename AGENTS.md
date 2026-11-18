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

- User directive (2026-11-17): select **SPR-M5 — Private Preview Clock Repair**
  as the SPR default through `configs/spr-evaluation-default.cfg`, selecting
  immutable `configs/spr-m5-private-preview-clocks.cfg`. `build/fastsim` is the
  measured retained-clock ELF, SHA-256
  `b869c0041b7ca6cf625e8aebdd6392e7c4a52be95ad1466052dc3d39e7d5f1b8`.
  Retain Estimated source/tests but explicitly set
  `core.unified_parent_estimated=false`; its active flag and numeric counters
  must remain false/zero. Keep inherited functional STLB/page walk, UART/APIC,
  rename elimination, retired-store prepare and `v2-wait` enabled. Preserve
  immutable M1-M4; the previous M4 ELF is backed up at
  `tmp/spr-m5-default-20261117/frozen-m4/fastsim`. See
  `docs/spr-m5-private-preview-clocks-default-20261117.md` for identity,
  rollback and the 12-case results. User permits ignoring the 1-2 native unknown
  members per affected event for this provisional PMU report; preserve their
  actual counts and sensitivity bounds. This default decision does not certify
  strict native PMU, hardware equivalence or throughput acceptance.

- User directive (2026-11-16): enable UART/APIC service by default as
  `configs/spr-m4-device-io.cfg` through the SPR evaluation alias. The ten
  current C4/C8 sources share the same target-platform device parameters;
  these are platform presets, not per-trace identity/version requirements.
  Use `device_io.enabled=false` for a device-only control. Preserve M1/M2/M3.
  The model ELF remains d6f5bae930dfad9782a938080f1de5e647dc44a6fc967c89dbc6f44e4eb9c5f9;
  default selection does not certify CPI thresholds, PMU or throughput.

- User directive (2026-11-16): the working SPR default advances to
  `configs/spr-m3-functional-translation.cfg`, with functional STLB/page-walk
  service enabled. Preserve immutable M1/M2. Current ELF identity and the
  independent opt-in UART/APIC device service are documented in
  `docs/spr-device-io-service-20261116.md`; device timing parameters must bind
  the actual source. This default decision is separate from CPI/PMU/throughput
  qualification. Historical M2 default statements below describe that milestone.

- User directive (2026-11-11): **SPR-M1 — Retired Prepare baseline** names the
  frozen R0/R1 `v2-wait` executable, SHA-256
  `dad62e0d67bea89cd9c69a946a2bd5e1eac89b7e9f3edf54704c0f81f13dc905`.
- Historical 2026-11-12 selection: SPR evaluations used
  `configs/spr-evaluation-default.cfg`, then selecting the immutable
  `configs/spr-m2-rename-elimination.cfg`. `configs/gem5-spr-retired.cfg` uses the
  same alias. Historical generic/versioned profiles are explicit controls.
- User directive (2026-11-12): enable bounded MOV / zero-idiom elimination
  as **SPR-M2 — Retired Prepare + Rename Elimination**. Its executable SHA-256 is
  `0b5aacd527e837771f2693ecfd8225ad2839950dfc33615944e861ea9da822b2`;
  this is the immutable model-baseline ELF, originally installed at
  `build/fastsim`. Preserve SPR-M1 at
  `tmp/spr-m1-20261111/fastsim` and its immutable profile. Explicitly set
  `core.spr_rename_elimination=false` to roll back the candidate path.
  Generic non-SPR library defaults remain off. This default decision does not
  certify incomplete native PMU evidence or the unfinished 30-pair throughput gate.
- User directive (2026-11-13): discard the third-round SPR config captures /
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

- User directive (2026-11-18): subsequent simulations use one distinct,
  qualified high-frequency physical host core per simulated core. On this
  host the qualified pool is CPU **48–63**, socket/NUMA node 1; use
  `python3 scripts/run_high_frequency.py --cores N -- COMMAND ARGS...` with
  N equal to the target core count. Verify actual affinity and avoid nested
  `taskset` masks that reduce the selected core count. C16 results measured
  with 12 host cores remain historical and must be labeled separately.


- User directive (2026-11-16, latest): subsequent zsim throughput reports use
  the file-parallel preparation implementation as the primary baseline.
  Default to `TRACEBENCH_PREPARE_JOBS=auto`; preparation remains inside
  end-to-end wall time and extra validation remains outside. The measured
  frozen ELF is `tmp/e2e-throughput-20261116/campaign-zsim-parallel/zsim-frozen`.
  Serial preparation is an explicitly labeled historical control only; never
  silently fall back to it. See `zsim-parallel-input-preparation-20261116.md`.

- User directive (2026-11-16): for the FastSim/Sniper/zsim C4/C8 comparison,
  primary throughput is end-to-end, including input preprocessing,
  initialization, functional warmup, simulation and required drain. Extra
  integrity/content/projection audits run separately. Simulation-only rates
  remain auxiliary; the older preprocessing-excluded primary policy below is
  historical for this comparison. Follow
  `docs/trace-comparison-throughput-contract-20261116.md` and use actual global
  monotonic wall spans, never subtract worker CPU time.

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
- Historical 2026-11-12 host placement used CPU 52–63 on socket/NUMA 1.
  That 12-CPU pool is superseded for new runs by the 2026-11-18 rule above;
  retain its identity when describing historical measurements.

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
