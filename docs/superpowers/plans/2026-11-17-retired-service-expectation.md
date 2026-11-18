# Retired service expectation implementation plan

**Goal:** Implement and evaluate default-off bounded and probabilistic retired-load service estimates with complete rollback to the legacy observer.

**Spec:** User approved the preceding two-layer proposal and explicitly requested implementation, validation and rollback. Existing two-stage execution, functional inputs and frozen M5 remain controlling constraints.

**Architecture:** A side observer consumes only accepted functional members, model cache service facts and model branch outcomes. It records Q16 event expectations in a separate namespace, keeping raw/legacy PMU and timing unchanged. Native traces supply independent validation only. Parameters are frozen before matrix evaluation, without workload IDs, native timing inputs, or tuning to evaluation counters.

## Tasks

- [x] 1. TDD a fixed-capacity associative service observer, interval uncertainty, functional locality/branch features and a bounded bidirectional probability layer. Cover forwarding, clock reversal, replacement, write boundaries, unknown sources and complete rollback.
- [x] 2. TDD macro correlation and Q16 aggregation: same-line members share a service; distinct uncertain lines retain Frechet lower/upper bounds with an explicit midpoint estimate. Retain warmup-spanning and abandoned/incomplete macros. Add default-off `core.retired_cache_service_model=off|bounded-v1|probabilistic-v1` and isolated report metadata; unsupported runtime paths reject.
- [x] 3. Integrate only at accepted retirement and serialize expected counts separately. Add maintained comparison support reusing existing source, scope, task and population gates. Prove legacy PMU and time-accounting neutrality and expectation conservation.
- [x] 4. Freeze source/config/ELF before running the four-workload C4/C8/C16 matrix with the two estimators. Preserve native unknown counts; report provisional errors separately from strict fresh reference validation. Use independent short native source as a mechanism control. No per-case tuning after results.
- [x] 5. Measure same-input OFF/ON host costs without diagnostic logs, using high-frequency CPU52-63 and disjoint masks. Required preparation/validation costs remain separately visible. Review, test and publish source, report and rollback instructions; default promotion requires separate evidence and authorization.

## Review focus

No synthetic cache traffic or target timing feedback. No native/event labels at inference. Fixed state and one forward pass. All captured functional members remain in the prediction population. Unknown reference members never become implicit hits. Macro OR cannot assume independent fragments. Distinguish expected counts from observed integers and host throughput from source audit cost. Current four workloads screen a prototype; passing them alone does not establish held-out-workload generalization.

**Execution:** Inline under writing-plans, executing-plans and test-driven-development. Isolated source/build/evidence under `tmp/retired-service-expectation-20261117/`. Preserve installed M5 and prior clock experiments.
