# Perf retirement repair design

User approved implementation and validation after the 2026-10-14 semantic audit
and staged repair proposal. Work in the current FastSim workspace; preserve
pre-existing changes. No commit, push, or destructive workspace operations.

## Goal and boundary

Repair the demonstrably incorrect retirement PMUs and expose independently
auditable physical/user/kernel/task populations in gem5 and FastSim. Keep
native SLICC diagnostics separate. Request-side L2/LLC perf estimates remain
explicit proxies until full speculative/code/prefetch/request coverage exists.
No counter name, unavailable zero, or simulator agreement certifies hardware.

The primary reference is [the semantic audit](../../perf-pmu-semantic-audit-20261014.md),
including measured SPR instruction units and current default buffer-store loss.

## Event domains

- Architectural retirement identity is not a cache line and not a gem5 UOP.
  Aggregate memory-member UOPs and line results until the enclosing macro
  completes; keep incomplete macros across chunks and do not pad common cuts.
- Ordinary and split load/store count once per architecture instruction.
  Double-read CMPS counts one retired load. RMW may have both load and store
  qualification. Do not infer absence of a read from a write qualification.
- Forwarded loads must not unconditionally become FB_HIT; the SPR scalar
  diagnostic observed them predominantly as L1 hits. Actual in-flight fill
  followers are a separate observed/modeled service class.
- Retirement counts must not depend on buffered-store prepare/publication;
  requests still affect hardware even when their retirement population differs.
- L1 data-source classification is an explicit target-bound model estimate,
  not a universal exclusive-partition theorem. Unsupported ISA/source details,
  incomplete macro/fill facts and ambiguous cases must be reported; no silent
  fallback can promote a field to strict. Special REP/gather/mask/prefetch
  semantics require explicit coverage or an unverified capability flag.

## Reporting interface

Add a versioned `scope_metrics.perf_retirement` object with field names
`perf_retired_loads`, `perf_retired_stores`, `perf_l1d_load_hits`,
`perf_l1d_load_misses`, `perf_l1d_load_fb_hits`. Supply total/physical, user,
kernel, and (when ownership is available) workload-task/other populations,
their independent macro denominators and coverage/incomplete diagnostics.
Final concrete JSON paths are recorded in the implementation report before
the collector and validator consume them.

Preserve legacy `perf_*` and native fields for compatibility, but explicitly
label legacy retirement UOP/line classifier and L2/LLC request estimates as
non-authoritative. New consumers must not silently fall back to them. No
retirement correction feeds an existing timing decision or shadow heuristic.
The new counters belong to accepted-prefix accounting and its snapshot/rollback
contract; repeated trials/line replay cannot duplicate architectural counts.

gem5 emits an independent oracle-only versioned retirement sidecar from actual
commits and service facts, complete common-start prefix and frozen owner. It
must not place cache source labels into FastSim input. Unknown source details
leave data-source fields unverified; no path-class-derived cache truth is
allowed in the native validator. Macro load/store population is independently
audited against the functional FST where the ISA contract supports it.

## Validation

Regression gates use the existing audit's seven probes plus split stores,
atomic read/write, multi-UOP/cross-chunk macros, task switch ownership, delayed
publication and rollback/reweave. No new feature may pass only a text search.
Expected independent literal units: one split load -> one retired load; one
two-load-UOP macro -> one retired load; atomic -> one load and one store;
one buffered store -> one retired store even without prepare-lifecycle modeling.

Hardware comparison uses the audited SPR raw encodings and recorded CPU identity,
current-thread user mode, repeated pinned non-multiplexed groups. The former
blanket forwarding/FB assumption must not return. Preserve measured overhead
and limit claims to the ISA/event population actually exercised.

Fresh fixed-C4 collection, if needed for new sidecars, reuses the maintained
probe launcher. New FST <=300MB, aggregate FST <=20,000,000,000 bytes. Compile
gem5 with floor(80% affinity cores), GCC11 and explicit Python3.11. A copied
baseline binary is never overwritten during its runs. Do not copy TeaLeaf FST.

Run default C++ build/tests and relevant Python tests. Reuse the maintained
native validator and separate identity gate for four-core native diagnostics.
Add a distinct retirement semantic validation mode; no synthetic perf cache
reference may be substituted for native cache truth. Report unsupported
certification/parameter gaps explicitly. Any CPI result includes absolute
cycles/macro error and aggregate MAE. Throughput uses same-source sequential
paired runs and measured user-UOP/s, not build/capture wall time.

First phase must leave physical timing/native cache predictions unchanged on
the same source/config; if it does not, investigate before accepting changes.
Old DRAM alignment limitations remain declared. This work does not promise a
MAPE improvement or a complete speculative request model.
