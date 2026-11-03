# Perf cache approximate predictions and L2 repair

User approved the in-chat repair sequence and requested implementation. Continue
in the dirty FastSim workspace, preserving existing work; no commit/push or FST
copy. Apply executing-plans and TDD. Independent file domains run in parallel.

1. Emit per-event estimated_value, policy, assumptions and missing_components,
   independently of strict value/coverage/hardware validation. Test real JSON.
2. Bind actual functional inputs by content and explicit source record ranges,
   independently recomputed by Python. Input identity is not request ROI proof.
3. Repair native-data private-L2 tag-miss accounting from local lookup facts;
   cover remote supply, writes, upgrades, merges, prepare and rollback. Do not
   substitute the differently scoped perf L2 event or change timing.
4. Validate/compare estimates separately: complete compatible reference permits
   estimated accuracy despite model approximations; incomplete native reference
   permits explicitly labeled diagnostic differences only. Never pool the two.
   Unknown request task/CPL context is not a zero-valued task prediction.
5. Build/direct tests and appropriate Python suites; validate existing accepted
   four-core source through maintained entrypoint and independent identity gate.
   Compare frozen before/after target fields, report CPI absolute error/MAE and
   elapsed separately. Measure same-source paired throughput only after other
   owned workloads finish. FST budget remains <=20,000,000,000 bytes.

Root owns Python contracts/audit/validation and integration. Agents own report
emitter, L2 counters, and functional-input identity in separate files. Final fresh
review follows integration, with important findings reproduced RED then fixed.

Completed first batch: independent estimated outputs, content/explicit-record
identity with separately charged hashing, local L2 data-demand repair, separate
estimated accuracy/diagnostic comparisons, build/full regression, three-source
native validation, fresh review/fix and five paired throughput runs. Results:
[implementation evidence](../../perf-cache-estimates-and-l2-repair-results-20261015.md).
Full macro/causal request-ROI metadata and full speculation/prefetch providers
remain separate follow-up work; this batch does not claim they are implemented.
