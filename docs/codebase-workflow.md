# FastSim Codebase repository and workflow

This document is the durable handoff for future agents working in
`/data00/yinhaolang/FastSim`. It records the repository identity, validation
commands, documentation map, and the safe commit/push procedure when the local
`.git` directory is unavailable.

## 1. Canonical repository mapping

| Item | Value |
|---|---|
| Hosting service | ByteDance Codebase |
| SSH remote | `git@code.byted.org:yinhaolang/minesim.git` |
| Project branch | `FastSim` |
| Local work tree | `/data00/yinhaolang/FastSim` |
| Commit identity | `yinhaolang <yinhaolang@BYTEDANCE.COM>` |
| Last verified push when this document was created | `d03e64fe765e0e44461c26beaa9c5729c97d36e0` |

The remote repository uses separate branches for multiple simulators. At the
time of writing it includes `FastSim`, `LLMSim`, `MTAO`, `TCSim`, `TSim`,
`archsim`, `dev`, `main`, and `taogen`. FastSim changes belong on `FastSim`.
Always query the remote before relying on the recorded commit hash because it
is historical state, not a permanent branch head.

```bash
git ls-remote git@code.byted.org:yinhaolang/minesim.git refs/heads/FastSim
```

## 2. Managed-workspace `.git` behavior

Some managed agent sessions expose `FastSim/.git` as an empty read-only
directory or mount. Symptoms include:

```text
fatal: not a git repository
dr-xr-xr-x ... .git
```

Do not run `git init`, create a new history, or guess a remote in this state.
The safe approach is to clone only the remote branch metadata into the
project-owned `tmp/` directory, populate its index, and use the existing
FastSim directory as its work tree.  Do not use the host `/tmp` for FastSim
metadata or experiment artifacts.

```bash
git clone --no-checkout --single-branch --branch FastSim \
  git@code.byted.org:yinhaolang/minesim.git \
  /data00/yinhaolang/FastSim/tmp/codebase-sync

git \
  --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  reset --mixed HEAD
```

`reset --mixed HEAD` initializes the temporary index from the remote commit;
it does not replace work-tree files. Never substitute `reset --hard`.

Define the following notation mentally for later commands:

```text
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
    --work-tree=/data00/yinhaolang/FastSim <command>
```

Use the full command explicitly in automation so there is no dependence on a
shell alias or environment variable.

## 3. Inspecting and selecting changes

First inspect all modifications relative to the remote `FastSim` branch:

```bash
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  status -sb --untracked-files=all

git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  diff --stat
```

The normal critical-file scope is:

- build definitions: `CMakeLists.txt`;
- public documentation: `README.md`, curated files under `docs/`;
- model configuration: `configs/`;
- C++ implementation: `include/fastsim/`, `src/`;
- regression tests: `tests/`;
- maintained collection, conversion, audit, and validation utilities:
  `scripts/`, `tools/`;
- workload source and Makefiles when they are intentionally changed.

Do not stage these by default:

- `build*/` and `Testing/`;
- `tmp/`;
- generated workload binaries;
- bulk experiment output under `results/`;
- caches, traces, model artifacts, logs, or credentials.

Use explicit paths with `git add`; do not use `git add -A` in a mixed work
tree. Before committing, run:

```bash
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  diff --cached --stat

git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  diff --cached --check
```

## 4. Build and test gate

The normal local gate is:

```bash
cd /data00/yinhaolang/FastSim
cmake --build build -- -j16
./build/fastsim_tests
```

`ctest --test-dir build --output-on-failure` currently says that no tests are
registered. This is not a substitute for running `./build/fastsim_tests`,
which should print `all FastSim tests passed` and exit successfully.

Use sanitizer or workload-specific validation only when the change warrants
it; the commands and evidence are documented in `README.md` and the validation
documents listed below.

## 5. Commit and push

Commit through the temporary Git directory after staging explicit files:

```bash
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git \
  --work-tree=/data00/yinhaolang/FastSim \
  commit -m "<terse description>"
```

Immediately before pushing, fetch or query the remote and confirm that the
branch has not advanced unexpectedly. Push the local temporary clone's
`FastSim` branch to the same remote branch:

```bash
git -C /data00/yinhaolang/FastSim/tmp/codebase-sync push origin FastSim
```

Verify the published hash:

```bash
git ls-remote git@code.byted.org:yinhaolang/minesim.git refs/heads/FastSim
git --git-dir=/data00/yinhaolang/FastSim/tmp/codebase-sync/.git rev-parse HEAD
```

Do not create a GitHub pull request. If the user requests review integration,
use the Codebase merge-request URL returned by `git push`.

## 6. Permanently restoring `.git`

Perform this outside a managed session if `.git` is no longer protected by a
read-only mount. Preserve all work-tree files:

```bash
cd /data00/yinhaolang

git clone --no-checkout --single-branch --branch FastSim \
  git@code.byted.org:yinhaolang/minesim.git \
  /data00/yinhaolang/FastSim/tmp/git-recovery

findmnt -T /data00/yinhaolang/FastSim/.git
```

If `findmnt` reports a dedicated read-only mount for `.git`, unmount that exact
path first from an authorized host shell:

```bash
sudo umount /data00/yinhaolang/FastSim/.git
```

Then remove only the confirmed-empty `.git` directory, install the recovered
metadata, and populate the index without touching source files:

```bash
rmdir /data00/yinhaolang/FastSim/.git
mv /data00/yinhaolang/FastSim/tmp/git-recovery/.git /data00/yinhaolang/FastSim/.git
git -C /data00/yinhaolang/FastSim reset --mixed HEAD
git -C /data00/yinhaolang/FastSim status -sb
git -C /data00/yinhaolang/FastSim remote -v
```

`rmdir` is intentional: it fails rather than deleting a non-empty directory.
Do not use `rm -rf` or `git reset --hard` for recovery.

## 7. FastSim documentation map

Start with these files rather than searching experiment output:

| Topic | Document |
|---|---|
| Normative project goal, perf→gem5→FastSim semantic contract, and acceptance order | `docs/project-goal-and-semantic-contract.md` |
| P0 baseline/measurement-contract implementation and activation gate | `docs/p0-baseline-measurement-contract-implementation.md` |
| P1 native PMU population and response-boundary audit | `docs/p1-native-pmu-population-audit-2026-08-19.md` |
| Overview, build, trace conversion, and usage | `README.md` |
| Architecture and confidence boundaries | `docs/architecture.md` |
| Trace schema and functional-only contract | `docs/gem5-trace-contract.md` |
| Validation methodology and gates | `docs/validation.md` |
| gem5 parameter coverage | `docs/gem5-parameter-coverage.md` |
| Current C4/C8 gem5/FastSim microarchitecture semantic-alignment audit | `docs/fs-gem5-uarch-semantic-alignment-audit-2026-08-18.md` |
| CPI/P99 investigation history | `docs/gem5-source-aligned-p99-plan.md` |
| Microarchitecture collection workflow | `docs/uarch-generalization-collection.md` |
| Generalization debugging record | `docs/uarch-generalization-debug-log.md` |
| FS syscall/CPL trace patch | `docs/gem5-taotrace-cpl-syscall-patch.md` |
| Dual-CPI syscall modeling | `docs/syscall-modeling-dual-cpi.md` |
| FST v7 layout and drmemtrace conversion contract | `docs/fst-v7-drmemtrace-conversion-contract.md` |
| CPI/PMU/throughput reporting contract | `docs/accuracy-reporting-contract.md` |
| Maintained v28.6 C4--C32 accuracy and throughput baseline | `docs/fastsim-v28_6-c4-c32-baseline-2026-08-27.md` |
| Current FS CPI scheme, evidence, and implementation order | `docs/fs-cpi-current-status-and-plan-2026-08-17.md` |
| guest-PTE and measurement-boundary page-fault model | `docs/gem5-initial-pte-page-fault-model-2026-08-17.md` |
| Current project-status narrative | `docs/weekly-meeting-fastsim-status-2026-08-06.md` |
| Debugging interview/checklist | `docs/fastsim-cpi-throughput-debugging-interview.md` |

For implementation work, map model ownership as follows:

- configuration parsing and defaults: `include/fastsim/config.hpp`,
  `src/config.cpp`;
- trace format and decoding: `include/fastsim/trace.hpp`, `src/trace.cpp`;
- timing, memory, and system coordination: `include/fastsim/simulator.hpp`,
  `src/simulator.cpp`;
- interval model: `include/fastsim/interval_core.hpp`,
  `src/interval_core.cpp`;
- CLI and output schema: `src/main.cpp`;
- regression coverage: `tests/test_main.cpp`;
- FS validation: `tools/validate_fs_c8.py`;
- cross-core validation: `tools/validate_tcsim_c4_c8.py`.

## 8. Historical handoff

On 2026-08-09, commit `d03e64f` (`Advance FastSim trace and timing models`)
was pushed successfully from `dc9cd96` to the remote `FastSim` branch. It
contained 32 critical code, test, tool, configuration, and documentation
files. The build succeeded and `./build/fastsim_tests` passed. This entry is
useful provenance only; always verify the current remote head before new work.
