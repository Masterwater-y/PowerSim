# Retired-stream SPR frontend implementation plan

> Implement inline integration with independent component/metadata tasks and a final read-only review. User authorized implementation and the orphan branch; no additional approval gate is needed.

**Goal:** implement a functional, opt-in SPR frontend on `fastsim-spr`, driven exclusively by retired records and functional companions.

**Architecture:** a standalone bounded frontend calendar models instruction-byte supply, MITE/DSB/microcode supply, allocation slots and IDQ acceptance. IntervalCore retains its memory and backend calendars and provides response readiness and actual rename acceptance. A separate functional companion describes observed retired macro spans and decoder facts; no gem5 timing or predicted-path input is admitted.

**Spec:** user's 2026-11-03 directive and the source audit in this conversation. `gem5-spr/src/cpu/o3/Frontend.py`, `frontend.cc`, `frontend_fetch.cc` are the component reference. Existing speculative machinery is unavailable in the new mode. Retired-only replay is not claimed to reproduce gem5 speculative state.

## Constraints and review focus

- Branch `fastsim-spr`, orphan root, origin tracking; no further SPR pushes to `FastSim`.
- Only retired functional records: no speculative stream, shadow fetch/data traffic or producer timing as input.
- Macro instructions, functional UOPs and allocation slots are distinct.
- Missing semantic metadata is an explicit unsupported input, never an inferred microcode/fusion classification.
- Cache response calendars are reused; avoid charging legacy decode stages and SPR service stages together.
- Metadata must bind source core, record population and hot-stream identity; reject corrupt/truncated inputs and incompatible companions.
- Model buffers and metadata readers must remain bounded and state must survive epoch/measurement boundaries.
- Legacy mode remains available for its existing inputs; new mode rejects incompatible speculative flags rather than silently ignoring them.

## Tasks

- [x] Publish independent orphan source/document snapshot and bind future workflow to `fastsim-spr`.
- [x] Functional metadata: native producer, compressed retired-macro companion, strict consumer, fixtures for ordinary/fused/microcoded/pseudo/abandoned macros and corruption.
- [x] Frontend component: pure C++ config and model, byte supply, source service, DSB state, slot accounting and IDQ release at acceptance; test cold/hot/width/pressure/reset/source changes.
- [x] Integration: actual active path replaces legacy fetch/decode bandwidth when enabled; frontend readiness feeds rename, accepted rename frees IDQ credits. Report configuration and counters; derive parameters from actual component objects. Disable speculative paths in the selected profile.
- [x] Validate build/default suite, directed component and metadata tests, configuration refusal cases and an actual bounded retired trace replay. Existing archived sources without new metadata must fail clearly in SPR mode. Preserve identity/context/cache-PMU gaps in any workload report.
- [ ] Final review, fixes, commit and push only to `fastsim-spr`.

## Independent file ownership

Component task owns `include/fastsim/spr_frontend.hpp`, `src/spr_frontend.cpp`, `tests/test_spr_frontend.cpp`.
Metadata task owns `include/fastsim/retired_frontend_facts.hpp`, its reader source/tests, `trace.hpp`/`trace.cpp` adaptation and new gem5 producer files plus probe hooks. Parent owns config, IntervalCore, simulator/CLI reporting, CMake, framework mapper and documentation.

## Ledger

- Orphan root `a0d9345` published; inherited generated business executables removed from the tracked tree by a following non-forced commit, preserving local files and published history.
- Ruling: functional input is the observed retired macro stream. Any DSB/MITE/MS result is computed by FastSim; native source choices and service times are diagnostic only.
- Ruling: this work establishes an explicit retired-path component model. It does not certify speculative equivalence, backend split-IQ/ROB equivalence, complete native PMU semantics or the previously missing context tail.
- Default build, `fastsim_tests`, three standalone C++ suites, decoder-byte/macro-start/DSB-eligibility feature checks, mapper and source-snapshot tests passed. Final read-only review found no remaining critical/important defects; actual C4 producer-to-consumer validation is still required.
- Native fixes preserve precise decoder bytes across DSB templates and fused replay; omitted debug-only prefixes require a functional proof before marking the retained macro start. Missing functional group leaders fail explicitly.
- FFE-only repeated macros crossing a cache-line boundary exposed a second-block omission in integration; the regression now passes using the actual FFE length independently of the legacy static-map switch.
- Actual C4 probe capture/audits/replay completed: 1,161,067 records, four aligned score populations, modeled MITE/DSB service exercised. Audit library environment omission was reproduced and fixed by reusing runner.process_env; original failure retained, unchanged capture reused. Zstd C4 recapture with the new metadata is running as a larger integration check.
- Implementation pushed as `9683658`. Larger Zstd check exposed SIMD operand formatting in the new semantic fingerprint; replaced disassembly with ordered static fields/RegIds and exact code identity. RED/GREEN regression and all four native-source focused tests passed, final native rebuild passed; retry uses a fresh boot and keeps the failed attempt with a hash receipt.
