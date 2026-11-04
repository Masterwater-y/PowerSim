# Retired-stream Completion Clock Plan

> Execute task-by-task with TDD and independent whole-change review.

**Goal:** Freeze per-core clocks at accepted retirement completion; compare only matching native/model end policies.
**Spec:** docs/superpowers/specs/2026-11-04-retirement-stop-design.md
**Base:** parent949a9e5, childb69ba85b05; branchesfastsim-spr/yinhaolang.

## Constraints and review focus

No instruction population trim, workload/ROI/sync modification or oracle timing in model input. Keep globalelapsed. No old source mutation or invented native anchor. CounterRLE boundaries, lasttargetrecord and duplicate/negativeclosure tests required. An absent/mismatched endpolicy cannot silently score. Preserve SPR timing; child changes probe-only.

### 1. Model stop
- [x] RED: real TaskClock/RecordedScheduler unequal completion cases preserve all counts afterfinishcore.
- [x] Modify include/fastsim/task_clock.hpp and src/recorded_scheduler.cpp closure tovalidatewithoutaccrual; revise assertions for new documented semantics.
- [x] Extend stats/main JSON with active_end_policy and percore completion/counts; realSimulator regression +defaultbuild/tests.

### 2. Native stop anchor
- [x] RED: actual native ledger/probe lastretainedrecord anchor, overwrite, RLE/ONpendingtail and targetcase.
- [x] Add oracle retirement_stop only in probe/native_task_clock.hh and tao_trace hooks; preserve raw common-end accounting.
- [x] Nativebuild at affinity80%, no nonprobe changes.

### 3. Independent boundary audit
- [x] RED: validate stopanchor at update/RLE/snapshot positions, corrupt/missing/earlier-thanROI/late/recordcount cases.
- [x] Validate raw common-end evidence then clipreference tostop; outputcommon-end diagnostics and percore completions.
- [x] time_accounting comparison rejects policy mismatch; oldmissingpolicy=common-end-v1, newexplicitretiredstop mustmatch.

### 4. Same-source control and fresh strict validation
- [x] Replay unchanged old c4-control-final and c4-openmp-final; verify completepopulation/params and frozen clocks; oldreferencewithoutanchorstrictunavailable.
- [x] NewC4 control/OpenMP captures withunchangedworkload/args; prepare/map/replay/identity/nativevalidator.
- [x] Curated source-aware result/MAE, independent review, source hash guard, child commit and portable patch.
- [x] Parent implementation commit/push and remote verification.

## Ledger

Start: originalsource/baseline outputs preserved; build/fastsim copied to tmp/spr-retirement-stop-20261104/fastsim-before. Nativeanchor and Pythonauditor work delegated to separate files; root implementsmodel freeze. Userinstruction overrides old common-end padding, whilefullcommon-end inputpopulation andelapsed remain.


## Completion evidence

Implemented as designed; no workload, instruction-population or SPR timing changes.
The independent review found one P2: per-core budget was not constrained by its own
completion. Added RED/GREEN tests and checks for model frequency scaling and native
counter/Sleep/Wakeup conservation. Also added a RED/GREEN aggregation gate preventing
different active endpoint policies from entering one CPI aggregate.

Default build/fastsim_tests and standalone TaskClock passed. Related Python tests:
172 passed; native producer/profile/retirement-stop: 8 passed; prior epoch: 2 passed.
Native build used 153 jobs. All 7,363 non-probe source files match child b69ba85b05.
Child yinhaolang commit: 9229fe5d03cb9d41963daa706796b73798a7fd1c; portable patch
reverse application check passed; child origin was not pushed.

Same-source complete cores[]/threads[] stats are identical. Control active cycles
182773 -> 113000 (69773 padding removed), elapsed 61516 unchanged; OpenMP active
1059762 and elapsed 341045 unchanged. Old references lack retirement anchors:
strict CPI unavailable by design. Original 14-file artifact receipt is unchanged.

Fresh matched-endpoint native validation passed 2/2 (zero failed/pending).
Control CPI abs error 0.07949050452655948 cycles/macro, relative 14.226685609552425%;
OpenMP abs error 0.22902054787764003 cycles/macro, relative 26.354145500900744%.
Two-case MAE 0.15425552620209976 cycles/macro; MAPE 20.290415555226585%.
Source passage is not accuracy acceptance or hardware equivalence; throughput
was not evaluated. OpenMP residual remains open.

Curated report: docs/spr-retirement-stop-repair-20261104.md.
Evidence: docs/spr-retirement-stop-20261104/summary.json and
 tmp/spr-retirement-stop-20261104/ (not committed).

Published implementation commit: f982b25d64a818e536080652ec40e1aa81c66fb3 on Codebase fastsim-spr; remote hash matched after push.
