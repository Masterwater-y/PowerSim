# Dynamic Translation Consumer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task.

**Goal:** Typed special-address evidence, mixed-page/PTE consumer and one validated Marian C4 recapture.
**Architecture:** Extend compressed dictionary schema, expose per-source-record functional facts, reuse existing DTLB and hierarchy-response machinery.
**Tech Stack:** C++17, Python3, zlib, gem5 x86 FS.
**Spec:** docs/superpowers/specs/2026-10-29-dynamic-translation-consumer-design.md

## Global Constraints
- Preserve dirty worktree, no commit or old FST deletion; gem5 build -j128.
- Actual free disk reserve30GB, globalFST400GB cap/395GB stop; new isolated case directory.
- Unknown ordinary translations fail closed; keep actual paged MMIO translation distinct from nonpaged IO/MSR.
- TraceRecord remains64bytes, old inputs preserve behavior; new xlat facts cannot be ignored.
- No latency constant fitting; same shared64-entryLRU; real PTE physical path through existing memory model.

## Review Focus
- Paged APIC geometry retained, no fabricated PTE for IO/MSR.
- Dynamic split and pending stale fill cannot resurrect old translation.
- Wrapper/slicing ordinal, zero token and multiple fragments preserve all data.
- Hierarchy PTE sequencing actually feeds bounded final CPU response.
- Disk and source/measurement population gates cannot be skipped for pilot.

### Task 1: Typed special physical facts and gzip v2
Files: shared fst_translation_facts.hpp and gem5/overlay mirrors; mem/functional_translation.hh; arch/x86/tlb.cc; writer; Python decoder/tests; patch/manifest.
Interfaces: spec enum/fields and exact v2 definition suffix; preserve old v1 decoder.
- [x] Add failing tests distinguishing paged APIC from nonpaged IO/MSR, strict coverage, mismatched address and corrupted v2.
- [x] Run RED, implement source-success-only classification and v2, run focused GREEN.
- [x] Mirror source/overlay and update patch/manifest; report exact test evidence and limitations, no build/commit.

### Task 2: C++ compressed xlat reader and source wrappers
Files: include/fastsim/fst_dynamic_translations.hpp, src/fst_dynamic_translations.cpp, trace.hpp/trace.cpp, CMakeLists.txt, dedicated tests.
Interfaces: TraceSource::dynamic_translations and current_translation_fragments per spec; consumes Task1 v1/v2 wire contract.
- [x] Add failing dynamic 2M->4K, stable token, multi/zero token, unknown/corrupt/missing sidecar and sliced replay tests.
- [x] Implement independent C++ decoding/CRC/hash/coverage and sparse per-record resolution; wrappers preserve facts.
- [x] Run GREEN + reader compatibility, no commit; provide integration interface to Task3.

### Task 3: Mixed-page DTLB and hierarchy response integration
Files: interval_core.hpp/.cpp, simulator.cpp, config.cpp, types/main counters only if necessary, tests.
Interfaces: Task2 current fragments; existing PageTableWalkPath and generation feedback.
- [x] Add failing mixed64LRU, dynamic stale-fill invalidation, ASID, multi fragment and nonpaged tests.
- [x] Implement actual keys and fail-closed selection without altering oldtrace path; run GREEN.
- [x] Add failing hierarchy physical-chain tests with bounded/pending/shared profile; integrate actual PTE addresses and feedback.
- [x] Run default build/fastsim_tests and focused CPU/traffic regression; independent review and fix.

### Task 4: Build, small smoke and Marian C4 recapture
Files: maintained storage/campaign tools only if missing capabilities, new curated result doc; isolated tmp receipts.
- [x] Inventory actual disk/globalFST usage, source command/config/checkpoint identity, old FST sizes.
- [x] Build gem5-j128; typed small dynamic probe strict coverage and newFastSim replay.
- [x] Launch only Marian C4 with full disk/time limits, fresh stats and gzip sidecar.
- [x] Identity and maintained native validator, P1/P2 mechanism arms, CPI absolute error/MAE; record all stopping/qualification conditions.

执行结果：见 ../../dynamic-translation-consumer-results-20261029.md。实施/重采/评分完成，精度门槛失败；P2保持opt-in，停止后续推广，不将任务完成当成精度通过。
