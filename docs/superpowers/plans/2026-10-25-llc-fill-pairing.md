# Sparse LLC Fill Pairing Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. The user approved implementation of the sparse pairing design in the preceding diagnosis.

**Goal:** Attribute sampled Zstd load miss disagreements to observed native fill sources without importing native outcomes into FastSim.

**Architecture:** An opt-in oracle-only JSONL stream records sampled physical-line fills at actual SLICC memory response, immutable originating request role/identity, FST record anchors, native service facts, and explicit squash ranges. FastSim emits a separate observer stream only for accepted memory members, keyed by raw source ordinal and line. An offline validator binds both streams to the same FST and performs temporal pairing; unknown/truncated evidence remains explicit.

**Tech Stack:** C++17, gem5 MESI_Three_Level/SLICC, Python 3.11, existing FS checkpoint/collector/native validator.

**Spec:** `docs/retired-cache-miss-estimates-20261023.md`, section Zstd LLC miss source decomposition; user instruction to implement it.

## Global Constraints

- Default off. Audit state and metadata never alter cache, queues, target cycles, original PMUs or FST input.
- Key = physical core + raw FST ordinal + physical cache-line address; never use expanded simulator UOP sequence as source ordinal.
- Fixed physical-line sampling (default stride16), bounded rows/bytes, explicit truncation and per-core closure. No extrapolated full-window attribution.
- Record fill completion, not just prepare issuance. A future fill cannot explain an earlier response; overlapping fills are a separate category.
- Classify wrong-path only with explicit squash evidence. Unobserved commit is not proof of squash.
- Restored/pre-window/unobserved cache state remains unknown unless observed; no fabricated zero miss or inherited parent miss.
- Multi-member/split observations are diagnostic members, not automatically architectural macro counts.
- Source identity, per-core population, common-end and native Ruby/SLICC conservation gates remain unchanged.
- Existing checkpoint and original FST preserved. Total FST cap200GB. Compile gem5 with floor(80% affinity CPUs), Python3.11.

## Review Focus

Prepare arriving before/after a load; line eviction/refill; split loads and ordinal shifts; unresolved/squashed origin; sampled/truncated file closure and cross-source joins.

## Task 1: Native physical fill provenance

Files: new `gem5-collection/overlay/src/mem/llc_fill_audit.hh` (mirrored to gem5-fs); existing RubyRequest, protocol message/TBE and SLICC utilities; TaoTrace functional record/measurement/squash hooks; `tests/test_llc_fill_audit.py`.

- [x] Write and observe RED tests for selected physical lines, real completion/eviction, sparse native members, role preservation and closure/cap.
- [x] Implement a standalone observer writer; propagate diagnostic role in existing protocol messages (no routing/timing change), emit actual LLC fill and eviction hooks, raw FST anchors with independent native service facts, explicit squash ranges.
- [x] Run observer/retirement/native regressions and compile gem5; preserve the prior executable with a hash receipt.

## Task 2: Accepted FastSim members and offline pairing

Files: new `include/fastsim/llc_fill_audit.hpp`, `tools/pair_llc_fill_audit.py`; modify `src/simulator.cpp`; new pairing tests and production observer test.

- [x] RED tests: future fill rejected, overlapping fill distinguished, eviction invalidates old generation, prepare/committed/squashed/PTE/code/unknown origin, wrong source/PC/line rejected, missing/truncated stream rejected.
- [x] Capture source ordinal/PC/phase in bounded diagnostic producer metadata, consume only on final accepted prefix; serialize model service/estimate fields without changing target state.
- [x] Pair against same-source native anchors, using only actual native facts and completed fill history. Report sampled member counts and unresolved evidence separately from formal PMUs.
- [x] Build full FastSim and run required direct tests plus focused Python/C++ regression suite.

## Task 3: Checkpoint-backed real validation

- [x] Freeze binaries/configurations and source snapshots; record source/space identity and preserve the existing C8 Zstd checkpoint; create/reuse a new binary-bound staged checkpoint with KVM→warmup→stat, same eight channels/buffer114 and ROI.
- [x] Capture bounded diagnostic output, replay its exact FST in FastSim, validate target identity and source/population using maintained tools. Do not change oracle counters to pass a gate.
- [x] Verify opt-in observer neutrality on a controlled source; classify sampled disagreements and publish supported/unknown attribution. A passing unit test is not workload accuracy.
- [x] Review the changes, record implementation/results in the existing diagnosis document, preserve any capture/coverage failures honestly.

## Verified outcome

Implemented observer-only native/model pairing and completed a checkpoint-backed C8 Zstd short-window diagnostic. Same-source FastSim target fields are invariant. Sampled known false positives: 77 (50 explicit squashed-demand prior fills, 24 unresolved demand, 3 committed demand). This does not repair the miss model or attribute the original full-window net error. See `docs/retired-cache-miss-estimates-20261023.md`, 2026-10-25 section, for source gates, unknown/split boundaries, accuracy and storage receipts. Independent gem5 ON/OFF sources differ; native observer neutrality is not certified by that pair.
