# Legacy buffer TSO prepare timing approximation

The per-core timing-only prepare credit calendar is enabled by default for
compatible legacy buffer TSO profiles. Set `core.legacy_prepare_timing = false`
to restore the previous timing path. Explicit `true` requires a compatible
profile; other simulator modes retain their own paths when unspecified.
`core.legacy_prepare_credits` defaults to 16.

For each eligible ordinary store, the model uses the existing replay's service
latency. A head already eligible to send takes a direct full-service estimate.
Otherwise a free prepare credit produces an estimated permission completion;
the head either waits for that estimate or uses the local publish latency. A
full calendar drops only the hint; the head retains a full-service estimate.
The store port and line window remain owned by the existing feedback model.

Per-core credits retain their source identity and completion across feedback
epochs. Trial feedback operates on a copy and the accepted pass commits it.
The `legacy_prepare_*_estimate` output counters describe timing roles only;
they are not physical cache requests or a native PMU population.

This is not an independent prepare/head hierarchy implementation. The ordinary
legacy cache and directory replay still runs once at its existing forecast time;
the new calendar does not reserve a second physical request or make late
permission changes retroactively visible. In particular, a later-batch conflict
cannot revise an already committed head estimate. Existing legacy conflict and
transaction handling remains in force. Prepare issue is not separately charged
to the CPU store-port calendar. These are accuracy limitations, not claims of
cycle-accurate coherence, and require workload-level accuracy and throughput
measurement before drawing performance conclusions.
