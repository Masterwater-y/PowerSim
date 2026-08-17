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
