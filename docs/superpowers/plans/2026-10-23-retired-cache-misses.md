# Retired cache miss estimates Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. The user approved this bounded design and requested implementation.

**Goal:** Report estimated retired load L1/L2/LLC misses as the three primary cache PMUs without requiring FB-hit accuracy.

**Architecture:** Add an observer-only bounded line-fill estimator, fed exclusively by accepted model memory descriptors. Aggregate its three independent miss bits at macro retirement. Keep native known/unknown observations intact and add a maintained validator mode selecting the three estimate fields. No target timing, cache replacement, input FST or native oracle is changed.

**Tech Stack:** C++17, Python 3.11, existing native validator and FST sources.

**Spec:** Approved conversation scope and docs/spr-gem5-fastsim-branch-cache-semantic-audit-20261022.md.

## Global Constraints

- The three events are SPR D1/08, D1/10, D1/20, per architectural retired load macro.
- No FB-hit acceptance gate, no per-workload tuning or full-stream fixed-point solve.
- Keep raw native source, identity, task, population and conservation gates; unavailable FLAC population is not silently repaired.
- Canonical model admission/response clocks are estimates, not native facts. Keep fallback/ambiguity counts explicit.
- The shadow never feeds timing, actual cache state or existing PMUs; warmup state survives reset, incomplete macros do not contribute.
- No new workload FST. Existing global cap 200 GB.

## Review Focus

Same-line fill boundary, OoO time reversal, bounded-table collision, mixed split macro, incomplete/warmup macro, wrong source identity, unknown vs missing estimate, and unchanged target timing.

## Tasks

- [x] Add RED tests for bounded fill classification and macro estimate accumulation; implement the pure helper and accepted-prefix observer.
- [x] Add RED contract tests for three-event estimate selection, unknown evidence retention and unchanged hard source gates; implement serialization and --retired-cache-misses.
- [x] Build and run full FastSim tests plus relevant contract/retirement regressions; compare unchanged target timing on existing sources.
- [x] Freeze binary/configs, run the maintained validator on successful C8 sources, publish three miss raw counts and provisional errors; retain FLAC rejection. Report active CPI absolute errors and elapsed if repeating CPI results.

Completed evidence: `docs/retired-cache-miss-estimates-20261023.md` and
`tmp/retired-cache-misses-20261023/final/summary.json`. Source/population gates:
Zstd and Graph500 passed, FLAC rejected as expected. All three replay outputs
preserve prior target timing and existing PMUs; native unknown service evidence
and unverified hardware equivalence remain explicit limitations.
