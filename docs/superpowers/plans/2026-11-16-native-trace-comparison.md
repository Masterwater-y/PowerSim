# Native trace comparison implementation plan

Goal: consume retained C4/C8 FST in native Sniper/zsim timing engines and report a common, explicit measurement scope.

Architecture: retain upstream core/cache/phase execution. Build adapters in isolated source copies under project tmp. FST provides functional data only. Native decoder substitution and unsupported dependencies are explicit capabilities; native event recording and contention are mandatory.

1. Establish native zsim build with OOOCore, TimingCache, EventRecorder and ContentionSim. Test dependency-heavy and shared-line synthetic multicore traces; require nonzero phases and event records. Fail on truncated input, unknown mapping, unsupported records and missing population.
2. Translate FST op classes/dependencies to native input uops without implementing a replacement pipeline. Preserve all supplied records, macro fragments and physical participants. Check per-core macro/UOP counts. Declare frontend port/latency approximation separately from native timing.
3. Repair Sniper trailing fragment macro accounting, record per-core model measurement endpoints and complete populations. Test a trace ending mid-macro and normal macro endpoints.
4. Define a physical retained-stream measurement alongside task-active CPI. Never compare unlike endpoints. Require source-bound independent gem5 evidence for that physical window, otherwise leave errors unavailable. Keep full raw outputs.
5. Build/smoke both adapters, run C4/C8 matrix, check provenance and populations. Throughput measurement excludes validation/preprocessing; controlled timing is a separate run after input checks.

Acceptance: runtime provenance identifies native engine and upstream/source patch identities; native zsim weave and contention execute; complete per-core input population; comparable statistics require matching scope and boundaries. Completion of a build or replay does not certify PMU or accuracy.

Execution record (2026-11-16): all five implementation steps delivered. Native zsim
retains upstream OOOCore/TimingCache/ContentionSim; Sniper retains native ROB and
records actual retired populations. The retained 10-case C4/C8 matrix completed
in all three engines, with complete per-core populations and native semantic
checksums matching. A full 660-artifact SHA256 and prelaunch inode-time gate binds
all three reads under explicitly stated local-filesystem assumptions. Review
found and resolved EOF retirement, extended-dependency capacity, predictor-PC,
serialization, source-version, participant-array and cross-engine identity issues.

See [implementation and limitations](../../native-trace-comparison-20261116.md)
and [results](../../../fastsim-native-trace-comparison-20261116.md). Saved-oracle
errors, incomplete code lengths and partially unmatched native hardware profiles
remain explicit diagnostic limits; no formal PMU/hardware or controlled host
throughput qualification is claimed.
