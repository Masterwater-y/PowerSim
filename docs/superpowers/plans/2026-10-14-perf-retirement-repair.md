# Perf retirement repair implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development to implement each task with independent review. The user has explicitly requested implementation and validation; proceed continuously.

**Goal:** Repair architectural retirement PMU units and validate the new domain without promoting legacy request proxies.

**Architecture:** Separate accepted architectural retirement aggregation from per-line replay. Independent gem5 oracle and FastSim populations are joined only in offline validation. Legacy timing and native diagnostic paths remain intact.

**Tech Stack:** C++17 FastSim, gem5 O3/Ruby C++, Python3.11 validators, SPR perf_event_open diagnostics.

**Spec:** [Approved repair scope](../specs/2026-10-14-perf-retirement-repair-design.md).

## Global constraints

- Preserve current dirty FastSim branch in place; no commit/push or unrelated cleanup.
- Compile FastSim `-j16`; gem5 floor(80% affinity cores), GCC11/Python3.11.
- Native cache truth remains conserved Ruby/SLICC through the maintained validator.
- FST total <=20,000,000,000 bytes; fresh capture <=300MB; no TeaLeaf copy.
- Legacy fields are diagnostics; new unavailable fields cannot fall back to legacy.
- Counter-only changes must preserve physical timing/native cache predictions.

## Review focus

- One macro split across accepted prefixes or ending at an incomplete common cut.
- Buffered store retires in-window but publishes later, without lifecycle option.
- Multiple fragments/reweave trials revisit one architectural instruction.
- Macro members retain correct owner and privilege around switch-entry boundaries.
- New event semantics unavailable while legacy counters contain plausible values.

### Task 1: FastSim retirement ledger and output

Files: `include/fastsim/types.hpp`, a focused new retirement-ledger header if useful,
`src/simulator.cpp`, `src/main.cpp`, `CMakeLists.txt`, `tests/test_perf_retirement.cpp`.

Consumes: functional FST macro boundaries, existing owner and accepted-prefix contracts,
model-generated service facts only. Produces: versioned `scope_metrics.perf_retirement`
with the five retirement fields, scope denominators, coverage and capability labels.

- [x] Add behavioral regression tests with literal expected units from spec, run RED.
- [x] Implement separate accounting that survives chunking and rollback, without timing feedback.
- [x] Report new counts and explicit legacy/request-proxy classifications; no silent hardware certification.
- [x] Run default build/direct C++ tests and real seven-probe replay; record JSON interface and evidence.
- [x] Independent task review; fix all correctness findings.

### Task 2: gem5 independent complete retirement oracle

Files: `gem5-fs/src/cpu/o3/probe/tao_trace.{hh,cc}`, matching collection overlay,
focused helper files/tests if appropriate, `tests/test_perf_retirement_collector.py`.

Consumes: Task 1 schema and spec; actual committed DynInst architectural boundaries,
complete physical task capture and native service registry. Produces: oracle-only
per-core retirement sidecars, complete owner/privilege/macro counts and honest
cache-source coverage. Do not reuse path-class as certified cache reference.

- [x] Add collector/helper behavioral RED tests for multi-memory macro, split, atomic,
  delayed store and non-user prefix; unsupported data source stays unverified.
- [x] Implement complete-start commit ledger and independent oracle serialization.
- [x] Synchronize changed overlay logic without deleting existing task collector work.
- [x] Build gem5 with specified jobs/Python, run embedded smoke and bounded fixed-C4 capture.
- [x] Independent task review; record actual source coverage and unresolved hardware gaps.

### Task 3: Maintained validation and event dictionary

Files: `tools/run_native_kernel_fastsim_validation.py`, `tools/audit_task_pmu.py`
only if needed, a focused retirement-audit helper, tests and a versioned event dictionary.

Consumes: new simulator JSON and independent per-core oracle; actual functional bounds.
Produces: separate retirement population/semantic report, raw/rate metrics only for
matching available fields, unverified data sources and request proxies not promoted.

- [x] Write RED tests for missing sidecar, source/window/task/privilege mismatch,
  incomplete macro, duplicate counts and forbidden legacy fallback.
- [x] Audit load/store architectural units independently; preserve native cache source selection.
- [x] Bind SPR event definitions, labels, denominator and legacy migration documentation.
- [x] Run Python tests and source/identity validators on the new C4 source.
- [x] Independent review of source selection and fail-closed behavior.

### Task 4: Full validation, same-source timing/throughput and report

Files: curated `docs/perf-retirement-repair-results-20261014.md`, workflow/contract
links as needed. Evidence stays under `tmp/perf-pmu-repair-20261014/`.

- [x] Run final default C++ build/tests and relevant Python suite after all edits.
- [x] Verify hardware unit probes and corrected software probes; no true-precision claim beyond evidence.
- [x] Replay fixed-C4 source with frozen before/after binaries; compare timing and native predictions.
- [x] Run bounded sequential paired throughput measurements and report measured user-UOP/s.
- [x] Report raw/rate retirement results, coverage, CPI absolute error/MAE and elapsed separately.
- [x] Final independent whole-change review; document limitations and every nonpassing gate.

完成记录：[实施与验证报告](../../perf-retirement-repair-results-20261014.md)。主源为明确披露的 GDB 四核诊断采集；失败采集、未分类来源及环境测试错误保留，没有硬件认证声明。
