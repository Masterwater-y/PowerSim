# Native CPI store-lifecycle repair audit (2026-08-21)

## Scope

This audit uses the mixed-privilege C4/C8 10M/core FST set under
`tmp/taotrace-fst-v7-native-kernel-c4-c8-10m-20260821`.  It isolates the
regular-store path after the native CPL0 stream removed the trace-coverage
ambiguity.  No gem5 timing label, workload ID, or target CPI is consumed by
FastSim.

## Store lifecycle ledger

`core.cpi_attribution=true` now emits a conserved per-core lifecycle:

```text
address/data ready -> ordered commit -> TSO send -> response -> SQ release
```

The adjacent intervals satisfy:

```text
commit_to_sq_release = commit_to_send + send_to_response
```

The separate `hierarchy_response_before_commit` audit records when the
canonical cache replay completed before the store became commit-eligible. It
is intentionally not added to the lifecycle intervals.

Selected native results:

| case | regular stores | hierarchy response before commit | TSO-wait cycles | send retime cycles |
|---|---:|---:|---:|---:|
| Stockfish C4 | 1,732,246 | 445,130 | 33,130,999 | 286,769,886 |
| Stockfish C8 | 3,641,094 | 436,637 | 48,246,487 | 294,271,181 |
| Zstd C4 | 4,334,075 | 1,710,878 | 382,250,583 | 2,972,991,620 |
| NAMD C4 | 2,434,376 | 1,382,510 | 134,479,811 | 1,155,827,811 |
| LBM C8 | 9,064,050 | 4,338,044 | 5,912,332,653 | 45,749,348,841 |

The large raw sums overlap other work and are not additive CPI. They prove
that issue-time private-cache replay and post-commit TSO/SQ timing are
different state transitions and must not be treated as one timestamp.

## Post-commit request candidate

`core.store_post_commit_request` is a default-off experiment. It:

1. moves regular-store cache visibility to at least lower-bound ordered
   commit while atomics remain at execute time;
2. reorders the complete private/shared request batch by that timestamp;
3. replays at the sparse scoreboard's reconstructed TSO send time;
4. commits only a stable fixed point contained in the current Q=1024 epoch;
5. rolls back cache, controller, timing, and counters otherwise.

The default-off path is bit-exact: Stockfish C4 remains `0.5242552968` CPI
when the switch is disabled.

Selected candidate outcome:

| case | baseline CPI | candidate CPI | stable / candidate epochs | throughput (user UOP/s) |
|---|---:|---:|---:|---:|
| Stockfish C4 | 0.524255 | 0.523471 | 706 / 2,578 | 5.56M with attribution |
| Stockfish C8 | 0.510474 | 0.510319 | 736 / 2,531 | 6.99M |
| Zstd C4 | 0.956039 | 0.956409 | 905 / 6,408 | 3.19M |
| LBM C8 | 3.415995 | 3.424422 | 43 / 20,415 | 2.47M |

Most candidate epochs fall back because the reconstructed TSO send crosses
the fixed epoch horizon. The stable subset does not improve Stockfish CPI,
and store-heavy controls violate the 5M UOP/s gate. The candidate therefore
remains disabled and must not be presented as an accuracy improvement.

## Consequence for the repair order

The store state mismatch is real, but an in-epoch replay is not the current
Stockfish P99 solution. A future store repair needs a compact cross-epoch
deferred-request carry rather than whole-epoch replay. Because the stable
subset already shows negligible Stockfish benefit, the higher-priority P99
work remains the I-side request/refetch/retry stream and its overlap with
response-driven ROB/SQ state. The new lifecycle counters provide the guardrail
needed to ensure that work does not accidentally compensate one store timing
error with another.
