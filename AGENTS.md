# FastSim agent notes

Read [`docs/codebase-workflow.md`](docs/codebase-workflow.md) before doing repository, commit, push, or workspace-recovery work.

## Repository identity

- Codebase repository: `git@code.byted.org:yinhaolang/minesim.git`
- FastSim branch: `FastSim`
- The repository contains several project branches; do not push FastSim work to `main` or another simulator branch.
- This is a ByteDance Codebase repository, not a GitHub repository. Do not use GitHub/`gh` workflows unless the user explicitly asks for GitHub.

## Workspace caveat

In managed agent sessions, `/data00/yinhaolang/FastSim/.git` may appear as an empty read-only directory. Never initialize an unrelated repository or force-push around this condition. Follow the temporary-metadata workflow in `docs/codebase-workflow.md` and bind this directory as the work tree.

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
