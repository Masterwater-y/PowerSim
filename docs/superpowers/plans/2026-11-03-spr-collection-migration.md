# Migrate the FastSim collection contract onto native SPR gem5

User authorization: migrate gem5-fs collection facilities to gem5-spr, preserve SPR target structures, work inside gem5-spr's own `yinhaolang` branch. Implement and verify directly; do not replace the target model with the old overlay.

## Baseline and invariants

- Parent FastSim branch stays `fastsim-spr`; independent gem5-spr branch is `yinhaolang`.
- Exact imported SPR state saved as `aa8ff90980ed4ad9531e2ee247361fe5ba7007e1`; `calibration-deploy` restored to `02119d1`. Nothing was pushed to gem5 upstream.
- `tmp/spr-collection-migration-20261103/protected-spr-source-sha256.json` freezes 7,363 existing non-probe `src/` files. Migration must leave them unchanged.
- Old collector producer and non-probe observation hooks already exist in SPR. Shared FST/retirement/translation headers are identical. New work therefore ports entry/configuration contracts and missing opt-in evidence, retaining SPR OpClass, FFE and store terminal reasons.
- No native timestamps, source/hit/fusion outcomes become model timing inputs. Missing kernel/task/cache evidence remains unavailable.

## Implementation

1. Save branch baseline and source protection manifest. **Done.**
2. Audit old/new probe and non-probe hooks, record parity and exclusions. **Done: no missing legacy hook.**
3. Add explicit functional retirement evidence capture independent of thread-state accounting in `gem5-spr/src/cpu/o3/probe/{TaoTrace.py,tao_trace.hh,tao_trace.cc}`. Preserve old defaults and all task/PMU qualification gates. Exercise real record classification, ROM hardware-entry and precise-fault handling; do not just create an empty `.fsem`.
4. Port a legacy-compatible warmup/common-end FS entry into `gem5-spr/configs/fastsim/`. Reuse the old collector protocol and utilities where possible, construct SPR CPUs/cache/DDR5 without old parameter overwrites, require audited profiles for task-state collection, retain static functional marker validation. The existing fixed-work entry remains available.
5. Add directed configuration/producer tests and source protection checks before building. Fail unknown identities/configuration rather than synthesizing metadata.
6. Build native gem5 with process-local GCC/Python/protobuf and affinity-based 80% job count. Run an actual bounded C4 collection through the migrated entry, audit source companions and common end, and replay with FastSim. Use maintained auditors/compilers, not a temporary accuracy scorer.
7. Verify all protected source hashes; preserve source, binary, workload and configuration identity. Run relevant regressions and default FastSim checks only if its implementation changes.
8. Commit collection work in independent `gem5-spr/yinhaolang`; store a portable overlay against the original gitlink in parent FastSim, update documentation, and push parent changes to `fastsim-spr`. Do not publish an unreachable child gitlink or push to someone else's gem5 upstream.

## Review focus

- No generic O3 setter may replace SPR widths, partitioned IQs, shared ports, allocation groups, caches or committed store mechanisms.
- Functional retirement evidence must remain independent from task ownership and native cache truth; a valid companion does not certify PMU origin qualification.
- Source core IDs and exact raw ordinal anchors must survive the legacy entry; synchronization plans must not be fabricated from native times.
- Warmup records must really be emitted and replayed, and the native common-end rule must close all declared participants.
- Unsupported kernel scheduler profiles and unavailable source facts must be reported, not silently accepted.

## Evidence ledger

- Collector audit: FST dependency/request/retirement/perf-retirement and all `taogen/shared` headers are identical. Old warmup, syscall, sync and thread-state functions remain present.
- Hook audit: commit 6/6, LSQ 2/2, Sequencer 10/10, L0 3/3 and L1 7/7 observation calls match. Old overlay includes target behavior changes and must not be applied wholesale.
- Shell Git predates `git switch`; branch creation recovered with `git checkout -b yinhaolang`. Saved baseline now exists only on requested local branch; upstream branch pointer restored, source unchanged.
- Implementation complete on child `yinhaolang`: `7231d7d2b7` plus `27ef513696`. Added only collection configs/probe evidence/query; protected source7363/7363 matches. Final native binary SHA256d28fdfd739293572c6658c7612cca318542956559daf8e11f75fa619c26510fe.
- Actual runtime caught omitted existing SPR DSB validation terminal in old serializer. Added its actual diagnostic counter and independent optional-zero legacy contract; old incomplete outputs still refuse. Marker origin now uses readonly actual producer state, including raw workid0 edge.
- Combined C4 current-runtime source:1955788records,216sync+428threadstate compiled events,644/644released,0unknown-owner; native merge passes. Full-input readiness correctly fails missing PC-only .imap on multi-ASIDcores1/2. Gap reported; no ASID redesign or fabricated maps.
- Fresh fixed-owner C4 source:strict full-reader/companion/slice/count/identity preparation passes,103originalartifacts unchanged,1368003records,66/66 compiledthread events consumed. Maintained native validator expected/discovered/passed1/1/1,failed/pending0. CPI unavailable dueproxy schedulerboundary; PMU scope native-tag diagnostic, no hardware equivalence.
- New preparation helper20directedtests pass; metadata16pass; serializer/native-contract focus42pass; entry9pass. Independent review counterexamples for companion integrity, source/hardware identity, exact manifest macro counts and workid0 resolved. Known ASID static-map absence stays fail-closed.
- Parent portable overlay regenerated against original02119d1 withreverseapplycheck; unpublished childgitlink notstaged. Critical source/tests/docs/patches only; generatedtraces/builds excluded.
