# Functional-only translation candidate

2026-11-16 default update: the mechanism is now selected by the SPR-M3 working
profile through `spr-evaluation-default.cfg`. Generic library defaults remain
off, and frozen SPR-M1/M2 are preserved. The opt-in/pilot statements below
describe the initial rollout; current identity and the independent device-service
candidate are documented in `spr-device-io-service-20261116.md`.

Opt in with `dtlb.functional_model=true`, after the source-bound SPR profile;
the settings-only overlay is `configs/spr-functional-translation-candidate.cfg`.
It deliberately has no default-profile include. Append its settings to a
wrapper whose `config.include` names the existing source-bound effective
configuration, preserving geometry, timings and thread plans.
Disable the switch to retain the old translation path; restore the original
source-bound walker count as well when reverting a candidate overlay (the
current source baseline uses 1, while the candidate uses the native count 4).
Immutable milestones
and the evaluation default are unchanged. The previous executable is archived
under `tmp/functional-translation-20261116/fastsim-before` with SHA-256
`f0945404ca35e9031695a5edf726111bee723af807c5ce0af814e3b921f0b40c`.

The candidate consumes only functional VA/ASID/PCID/page-size/mapping facts,
functional PTE paths when present, and modeled times. No OoO timestamp, native
TLB outcome or latency trace enters the model. Missing MMU controls are an
explicit invalidation approximation, not a reason to clear STLB every record.
Known controls/boundaries and observed mapping changes invalidate state.
Global mappings remain conservatively ASID scoped; instruction-side STLB
population is unmodeled in this initial D-side candidate.
Physical-address-only records use a 4K physical-page identity proxy; paged
fragments without leaf size use a 4K leaf approximation. These identity
approximations increment `spr_dtlb_stlb_fallbacks`; VA aliases and unobserved
page-size/PCID behavior are not certified. They do not require additional traces.

4K DTLB placement follows configurable ways (6 for the current 96-entry source).
Other page sizes share the bounded pool as an approximation. The existing
2048-entry STLB implements bounded domain/set lookup. STLB hits return after
the source-bound latency and do not reserve walker slots. Both DTLB and STLB
fill at modeled completion, with mapping versions preventing stale fills.
Per-entry modeled fill times prevent future-state reuse when nominal issue
times regress; independent lookups are not forced behind a global frontier.
A finite pending budget applies backpressure;
no functional members are dropped.

True walks select an available source-configured walker. A bounded PTE-only
cache shadow estimates sequential level responses. Physical functional paths
use PTE cache-line addresses. Missing paths use `(root, level, virtual-prefix)`
structural keys, never invented physical accesses. Each tier has at most 512
sets of 8 tags, limited further by the configured cache capacity. Source cache
hit latencies and DRAM RP/RCD/CL/burst/controller terms determine service;
the legacy fixed aggregate walk delay is not used in this mode. Large pages
shorten the structural path. No CPI-derived fitted parameter is introduced.

This is a service approximation: PTE-only shadows omit interference from data
traffic, coherence and DRAM row/queue state; structural paths assume a four-level
x86 layout when the functional path is absent. Cache capacities are capped for
host cost. Known physical addresses do not make this a full shared-cache walk
simulation. Translation services do not create architectural demand accesses,
retired loads or extra trace instructions. Existing interval timing carries the
result as a readiness dependency, without a per-cycle runtime or convergence.

Configuration output identifies the walk service and invalidation policies.
`functional_walk_physical`, `functional_walk_structural`,
`functional_pending_wait_cycles`, existing STLB hits/misses and missing-control
counts preserve coverage and backpressure. These are model diagnostics, not
native PMU truth. Model accuracy and primary validation-excluded throughput
must be evaluated separately on the complete same source.

## Pilot validation

Final executable SHA-256:
`80a97cff389bdc31f14948f3d2ddeccaa87e99fa968545c915de5cee9771b601`.
Results and per-phase costs are in
`tmp/functional-translation-20261116/final/results.json` and `results.csv`.
Complete input/source identities and per-core macro populations match the
previous C8 replays. Zstd OFF is exactly neutral in task time accounting and
all four model PMU namespaces. The maintained quick validator scores Zstd and
FLAC using saved native accounting; no fresh full native audit is claimed.

| C8 case | gem5 CPI | previous CPI | candidate CPI | previous absolute error | candidate absolute error | candidate relative error |
|---|---:|---:|---:|---:|---:|---:|
| Zstd | 1.052763 | 1.190733 | 1.075338 | 0.137970 | 0.022576 | +2.14% |
| FLAC | 0.269360 | 0.274692 | 0.273608 | 0.005331 | 0.004247 | +1.58% |

Absolute errors are cycles/macro. Equal-case MAE is 0.071651 to 0.013411;
MAPE is 7.54% to 1.86%. This is a two-case pilot, not general accuracy acceptance.
One serial Zstd AB pair measured 1.999249 to 2.073289 million user UOP/s with
validated preload; full-process working frequencies were 3.0765 and 3.0907 GHz.
These are single samples, not a 30-pair throughput gate or a proven speedup.
Input preparation, integrity, initialization, warmup and process costs are
separate in JSON. FLAC has only a candidate throughput sample in this batch.

The first implementation used a global lookup-time frontier and regressed CPI.
Its outputs are preserved in the parent experiment directory; it was rejected.
The independent-lookup regression reproduced that serialization and the final
implementation uses per-entry visible times instead. The final suite covers
missing MMU controls, physical and structural paths, physical-only inputs,
unknown leaf size, mapping changes, ASID scoping, finite pending backpressure,
future-fill visibility, independent lookup overlap and independence from the
legacy fixed aggregate walk delay. Build, `fastsim_tests`, dynamic DTLB and
existing I/D STLB runtime tests passed; five PTE/STLB Python tests passed.
