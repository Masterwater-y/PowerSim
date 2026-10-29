# Task PMU repair implementation plan

> Use superpowers:subagent-driven-development for isolated collector/consumer
> tasks and review; root owns reporting, integration, builds and real validation.
> The user explicitly requested repair; do not add another approval gate.

**Goal:** Produce comparable task-onCPU branch/cache PMU on the real C4 source.
**Architecture:** Complete physical replay plus independent owner-selected PMU;
native producer evidence and maintained validator prove the same population.
**Tech stack:** C++17, gem5 x86 Linux, Python3.11.
**Spec:** ../specs/2026-10-13-task-pmu-repair-design.md.

## Constraints and review focus

Preserve dirty work in place with snapshots. No commit/push. Native Ruby cache
truth only; no oracle feedback. No latency tuning. Source owner before an
after-record switch owns that record. Task discovery can be later than first
measurement commit. Late response and rollback must retain request membership.
New FST<=300MB and aggregate<=20GB; gem5 builds use80% affinity cores.

## Tasks

- [x] Collector: RED prefix/owner/native-identity coverage evidence, independent
  raw-owner counters and native hierarchy with complete common-start capture;
  preserve legacy CPL semantics. Files gem5-fs/src/cpu/o3/probe/tao_trace.hh/.cc,
  applicable patch, focused collector tests. Define sidecar with root first.
- [x] Consumer: RED mixed-owner memory/branch and rollback mechanism tests;
  carry functional membership through resident/request lifetime and additive
  snapshot-safe PMU subset. Files types.hpp, simulator.cpp, main.cpp, new tests;
  root registers tests/build. Keep physical output unchanged.
- [x] Reporting: maintained native validator consumes independently conserved
  task evidence, own per-provider task populations and supported event mapping;
  missing/modified/partial source fails closed. Add parser/auditor helper only
  where useful; no duplicate scorer or proxy cache fallback.
- [x] Review/build: independent collector and integration reviews; default C++
  build/direct tests, targeted Python and appropriate full regression.
- [x] Real bounded C4: capture using corrected gem5, compile functional plan,
  independent ownership audit, hardware identity and maintained native validator;
  report per-event PMU error, active CPI/absolute error/MAE, elapsed, disk and
  any unsupported gates. Preserve old invalid output and link replacements.

## Execution ledger

Shared contracts and review decisions live in .tmp/sdd/task-pmu/ledger.md.

## 完成记录

2026-10-14：collector、consumer、独立人口审计、维护validator、review及真实bounded C4全部完成。结果见 [task-pmu-repair-results-20261014.md](../../task-pmu-repair-results-20261014.md)。native validator passed1/1，PMU scored1/1；默认C++通过，Python331项中323通过/5skip/3既有环境错误。模型时序未改；PMU范围可比不代表精度达标或硬件perf认证。
