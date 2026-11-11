# Replace bounded store timing with resource-bounded lifecycle

Goal: add a separately selected single-pass store lifecycle that supports finite
prepare/demand credits, store ports and cross-batch visibility. Per the latest
user instruction, preserve both existing `bounded_analytic` and legacy paths.

- Add bounded, per-core prepare and demand lifetime state keyed by functional
  store identity; prepare saturation drops only the hint. Record separate
  prepare, head, permission/callback and SQ-release edges.
- Track in-flight permission generations by physical line, so younger same-line
  requests cannot observe a future completion. Copies participate in the same
  transaction as cache, directory, resources, feedback and counters.
- Integrate with existing interval feedback and private/shared cache transactions;
  retain real store-port/window and FR-FCFS behavior. Do not use gem5 timestamps,
  a per-cycle runtime or whole-window iterative convergence.
- Exercise capacity-full demand retention, cross-core same-line visibility,
  warmup/batch persistence and transaction restore, then build and run the
  existing integration checks. Report any remaining limitations explicitly.

Status: the resource summary supports completion publication only at a certified
watermark. `SharedSystem::Transaction` now snapshots/restores the summary with
its caches and directory, and a focused dual-core probe exercises publication,
invalidation, rollback and stale generation. The actual Simulator has not yet
connected normal demand, store head, feedback commits or FR-FCFS to this state.
It still uses `estimate_nonbinding_store` and canonical cache replay; the
requested new option is not implemented. In particular `next_visible` remains
an adapter query, not a guarantee about an already-mutated cache.

The lifecycle now exposes a certified-watermark completion sweep, ordered by
modeled response and functional identity. It retains unpublished requests
across watermarks and suppresses invalidated generations. The shared transaction
probe uses this sweep; its cache access is synthetic and does not reserve the
real request at issue time. Production integration still requires separating
the existing hierarchy's service reservation from its cache/directory changes,
and connecting ordinary accesses, head feedback and FR-FCFS to that split.
The sweep alone does not make the new model available as a configuration option.
