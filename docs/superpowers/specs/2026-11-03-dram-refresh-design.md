# Optional rank refresh for the compact DRAM controller

User authorizes design, implementation and CPI/PMU/throughput validation, with
an option that preserves the old path. No default timing change, workload
special cases, fitted latency additions, global CPU-memory iteration or gem5
full-system recapture. Current pilot: Marian C4 and G500 C16, frozen P1 sources.

Interface: `dram.refresh_enabled=false`; when enabled, require explicit
`dram.t_refi_ticks`, `dram.t_rfc_ticks`, `dram.refresh_ticks_per_cycle` and
`dram.refresh_start_tick` (default0: relative FastSim timing startup). Import
physical values from captured controller configuration; do not infer native
checkpoint phase from a filename or use native CPI as an input. Keep the timer
in integer ticks; convert readiness to core cycles with one ceiling. Current
gem5 timing startup and refresh recurrence, including its preparation-event
offset by tRP, are the version-specific reference. Powerdown is disabled in
the captured cases; mixed pending-service controllers are outside this first
implementation and must reject the option explicitly, never silently ignore it.

Each rank retains next preparation event, blocked-until timestamp, last issued
data completion and counters. Preparation gates new request selection for that
rank, respects existing bank PRE restrictions and outstanding data, closes all
rows and reserves tRFC. Other ranks remain selectable. All-closed idle ranks
need no unconditional tRP. Idle repeated refresh windows can be skipped by
arithmetic; active PRE/drain is processed once before the idle skip. State lives
inside DramModel and is copied/restored with existing timing transactions.
Candidate estimates remain immutable; advance calendars only at a certified
controller selection/commit boundary, never while probing each candidate.
No per-cycle loop, extra all-UOP pass, or increased existing repair-pass limit.

Refresh correctness and model promotion are separate. Existing double-PRE and
write-calendar approximations stay independently gated. Validate refresh event
boundaries and same-flow latency increments before pilot accuracy. R-only is
expected to increase Marian's current positive CPI error; report this effect,
do not hide it or tune the mechanism. Keep default off regardless of pilot gain.
Primary PMU reporting uses maintained native validation, the three retired-load
miss events and source labels; request/tag events remain separate diagnostics.
Report reference coverage limitations without calling this a failed review.

Acceptance: feature-off target fields equal frozen P1, unit/controller tests
pass, default build and fastsim_tests pass, two-case CPI absolute/relative errors
and MAE/MAPE/WAPE/bias reported, PMU source and populations conserved, paired
sequential throughput including measurement/warmup/end-to-end walls reported.
Throughput overhead<=3% is a measured gate, never an a-priori guarantee.
