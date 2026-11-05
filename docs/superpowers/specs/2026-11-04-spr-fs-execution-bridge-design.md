# SPR FS execution bridge: bounded first delivery

2026-11-04. User approved the narrowed sequence after the 16-hour status review:
finish existing defects, connect full-source FS/continuous warmup/task clocks,
then use FLAC C4 as the first full-window case. This spec refines the approved
mechanism plan. No new collector schema, RAM baseline, store-value transport,
private-replacement project or independent native reference is released here.

## Required behavior

Reuse TraceSource, FunctionalSyncPlan, RecordedScheduler and TaskClock. Keep
persistent CoreContinuation + MemoryRuntime and functional-only input. No
native time/hit/grant, wrongpath, global fixedpoint/replay, ROI cropping or
silent unsupported fallback. Preserve default/OFF behavior and all existing
source/version/identity/parameter gates. No inferred target constants.

Add default-OFF core.causal_fs_execution, requiring core.causal_runtime,
interval_weave/time_epoch, static reference frequency and dense physical
streams. It enables the qualified source/accounting adapter only; it never
means full SPR support. Unsupported frontend/translation/serial/branch/atomic/
IO/prefetch classes retain explicit failures until their runtime semantics
exist. Source physical mappings can supply request identity without becoming
TLB hit/walk timing. Old functional sources are not silently upgraded.

Read Ready / WarmupBoundary / TrueEnd explicitly. A false next() with
measurement_boundary_pending() is the warmup pause, never EOF. At that pause
snapshot the exact last source ordinal/phase, call original start_measurement
once and continue bounded preparation immediately. Keep one monotonically
increasing source/model sequence and common model timeline. Retain RAW versions,
partial macros, SA/SD, ROB/IQ/SQ, stores, caches, TLB/protocol and outstanding
responses. No phase seam drain/reset/barrier or wait for another core.

Each retained envelope owns copies of its current-row facts, original ordinal,
phase and hardware-entry/precise-abandonment/task-eligibility flags. It never
keeps a pointer into a reader's mutable current-row view. Source layout and
record/macro populations must match exactly; missing ordinals/metadata or
truncation fail, not finish successfully.

At actual retirement of each core's last warmup member, establish that core's
measurement origin; the next measured member can retire before another core
finishes warmup. Zero-prefix uses the model's initial epoch for execution only;
strict zero-prefix native comparison stays unavailable without a qualified
reference. Extend existing clocks with begin_core_measurement(core,origin),
preserving old begin_measurement(origin) behavior. Account only after that
core's origin, while continuing ownership/eligibility state through warmup.
Pending account-out/in crosses the seam unchanged. Checked nonnegative deltas
and active+other+transition+unknown conservation remain mandatory.

Reuse existing functional graph and scheduler ownership. Validate all source
anchors (core,original ordinal,PC,ASID,phase). Observe only at corrected model
retirement; required execution boundaries prevent younger side effects until
release through actual model predecessors. Passive observations remain passive.
Logical blocking does not stop a physical kernel stream; waking does not assign
a CPU. Charge each macro to its pre-boundary eligibility, then apply the row's
lifecycle cut. Hardware-entry/abandoned members execute but do not invent task
macros. Do not synthesize syscall/IRQ service over retained kernel operations.

True source EOF closes input only. Finish a core after all its retained members
retire and its required final boundaries release. Freeze both clocks there;
detached stores/cache/protocol can drain without adding CPI cycles or task time.
Common closure validates coverage; it never extends short cores. Complete
full-source populations, independent owner continuity and no unresolved graph
obligation are distinct checks.

## Delivery and evidence

First runtime slice covers continuous source/clock/scheduler lifecycle, with
known existing generic/ordinary memory classes and mixed privilege where
supported. Remaining operation classes are explicit blockers for the next
slice, not excuse to invent facts or remove guards. Retain actual target
parameters; do not turn off target mechanisms to call a case qualified.

Use real Simulator::run tests, production BinaryTraceSource/Warmup wrappers,
FunctionalSyncPlan and clocks: chunk1/2/7/128, crossing load/store/RAW/partial
macro/SA-SD, unequal core origins, pending switch, blocked task continuing kernel
work, wake without ownership, source truncation, final boundary and late drain.
Selected domain suites plus default build/fastsim_tests are the validation gate.
No repeat broad source audits or native rebuild is required for source-only
runtime tests.

FLAC C4 is the first full-input entry check and eventual full-window run. Keep
its original qualified source/config/reference immutable. Maintained preparation
and profile import may produce an explicit experimental candidate configuration;
log every delta and capability. Do not score a partial prefix or change target
parameters to evade a failure. Classify actual blockers as missing runtime,
missing functional input or unsupported target domain, with exact identity.
This first delivery must reveal the remaining minimal blockers rather than
start unrelated protocols.

The new per-core source-retirement origin differs from the original common
native BEGIN. Publish active_start_policy=source-retirement-start-v1 and
active_end_policy=retired-stream-completion-v1. Until a qualified matching
native start series exists, strict CPI comparison is explicitly unavailable
(measurement-start-contract-mismatch). Report model population/cycles and
mechanism tests honestly; do not re-label old native CPI. The separate native
reference design remains future work. Any later comparable result includes
absolute CPI error and MAE alongside relative error; throughput is separately
measured, with all warmup/measurement/end-to-end samples retained.


## Minimal comparison guard within this delivery

The maintained `tools/time_accounting.py::build_time_comparison` must compare
active-start as well as its existing active-end policy. Original providers with
no start field mean `common-start-v1`; new source-retirement-start-v1 versus
that original window yields unavailable / active-start-policy-mismatch, null
CPI/errors and no fallback, even if all numeric values happen to match. Preserve
legacy old-provider results and expose both provider policy names. This small
consumer guard and tests/test_dual_time_accounting.py are part of F1; no native
reference publisher or aggregate redesign is released by it.


Recognized start policies are common-start-v1 (also the legacy missing-field
meaning) and source-retirement-start-v1; explicit unknown values fail closed.
The new source-local maximum duration can remain a named diagnostic/accounting
bound, but original common elapsed comparison stays unavailable: do not publish
max(local spans) as available original common elapsed or a scored elapsed error.
Keep original-mode elapsed fields unchanged. The same focused comparator tests
must reject such a false elapsed comparison without adding a native reference.
