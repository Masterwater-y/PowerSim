# Parallel zsim file preparation implementation plan

**Goal:** allow independent per-core FST files to prepare concurrently inside
the existing end-to-end interval, with unchanged functional inputs and backend.

**Design:** resize the input vector first; each worker owns one reader and one
output slot. Load configuration once before workers. Join before register-size
reduction, cache initialization and existing sequential functional warmup.
Default worker count is min(files, CPU affinity); `TRACEBENCH_PREPARE_JOBS=1`
restores serial preparation. Propagate worker exceptions before any simulation.
No model, source FST, warmup order or validation policy changes.

**Execution:** inline, use test-driven-development and executing-plans; all
scratch/builds under `tmp/e2e-throughput-20261116/zsim-parallel*`. Preserve
original serial campaign and frozen executable.

- [x] Add bounded worker helper and tests for concurrency and exception joining.
- [x] Integrate with native loader and expose actual worker count; verify serial
  and parallel prepared-input hashes/populations with synthetic inputs.
- [x] Re-run ten zsim C4/C8 cases on CPU 52–63, compare frozen serial preparation
  and end-to-end times, and preserve common input hashes and model-cycle drift.

Limits: worker count is bounded by the host CPU affinity. More concurrent
readers can increase memory usage and contend for memory/storage bandwidth;
report actual measurements rather than promise core-count-linear speedup.

Execution: all 53 tests passed against the parallel build; all ten real cases
passed population and prepared-input-hash equality with the serial campaign.
Native core/cache/contention sources and functional reader archive identities
match the old build. Model-cycle differences are retained explicitly (largest
common-completion difference 0.03782%); no bitwise model equivalence or accuracy
improvement is claimed. Fresh read-only concurrency review found no blocker.
Results: `zsim-parallel-input-preparation-20261116.{md,json,csv}`.
