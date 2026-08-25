# Branch-miss population audit v2 (2026-08-21)

## Decision

FastSim estimates the wrong-path UOP population present when each branch miss
resolves. `branch.population_audit` by itself remains CPI-neutral: it does not
alter fetch, decode, rename, dispatch, issue, completion, retirement, or the
fixed gem5-aligned two-cycle redirect. The separately gated
`core.fetch_supply_speculative_shadow` experiment may consume the estimate,
but production keeps that switch disabled. This separation is required
because the earlier `branch.shadow_rob` experiment converted anonymous UOPs
directly into post-squash rename delay and produced a hard
`int_div_serial` regression.

The target `BaseO3CPU` leaves `squashWidth` unset. FastSim represents that as
`branch.squash_width=0`: younger ROB entries are invalidated as one squash
operation, not drained at a finite UOP/cycle rate. Therefore the number of
squashed UOPs is useful evidence about speculative work and contention, but
is not itself an additive `squashed_uops / squash_width` CPI penalty.

## Causal estimator

For miss event `i`:

- `R_i = completion_cycle - fetch_cycle` is the branch resolution window;
- `O_i` is the number of older committed-stream UOPs dispatched by resolution
  but not retired by resolution;
- `Q_i = ROB_entries - min(ROB_entries, O_i + 1)` is resolution-time ROB
  headroom, with the resolving branch occupying one entry;
- `U_i/H_i` is actual committed UOP supply over the preceding configured
  rolling window (64 cycles in the target profile);
- `F = min(fetch_width, decode_width, rename_width)` is the hard frontend
  width ceiling.

The audit computes:

```text
supply_budget_i = min(ceil(U_i * R_i / H_i), F * R_i)
estimated_squashed_uops_i = min(Q_i, supply_budget_i)
estimated_residency_i = ceil(estimated_squashed_uops_i * R_i / 2)
```

The optional address-free Fetch shadow uses additional sufficient statistics
from exactly the same rolling window:

- `D_i` is the number of committed Fetch-block requests;
- `S_i` is their summed request-to-response service cycles.

It computes:

```text
estimated_requests_i = ceil(estimated_squashed_uops_i * D_i / U_i)
response_service_i = D_i == 0 ? 0 : ceil(S_i / D_i)
```

Requests are placed evenly inside `[fetch_cycle, completion_cycle)` and share
the persistent one-response Fetch ledger. Squash removes the anonymous ROB
population immediately. Only a response that remains outstanding beyond the
existing `completion_cycle + mispredict_penalty` recovery edge may extend
Fetch. No PC, opcode, dependency, virtual address, physical address, cache tag,
or PMU event is fabricated. A known zero request density produces zero
requests; a cold or missing UOP history fails closed.

The current branch is excluded from `U_i`. A miss without prior causal supply
history reports `history_unavailable` and estimates zero. Address-space
switches clear the rolling supply history so one process cannot donate a rate
to another. Static predicted-path records are counted as an independent
coverage diagnostic; a partial or missing `.imap` never reduces or inflates
the population estimate.

The aggregate ledger is conserved:

```text
miss_events = history_ready + history_unavailable
miss_events = predicted_path_covered + predicted_path_unavailable
estimated_squashed_uops <= rob_free_uops
estimated_squashed_uops <= supply_budget_uops
predicted_path_covered_uops <= estimated_squashed_uops
```

## Implementation surface

- configuration: `branch.population_audit` and
  `branch.population_history_cycles`;
- CLI: `--branch-population-audit` and
  `--branch-population-history-cycles`;
- output: aggregate and per-core `branch_population_audit` JSON objects;
- target profile: audit enabled with a 64-cycle history window;
- timing effect: exactly zero when only `branch.population_audit` is enabled;
  the separate Fetch-shadow candidate can expose only a recovery-surviving
  response and requires both the population audit and source-aligned Fetch
  response ledger.

The predictor constructs a static speculative path when either the existing
L1I speculative-path state or this audit requests it. Path construction is
read-only and remains separate from pipeline scheduling. It runs only after a
real predictor miss; correctly predicted branches do not construct and discard
an unused path. Fetch history is grouped by cycle rather than stored as one
deque node per UOP. On a warm-cache Stockfish C4 A/B these optimizations leave
about 3.9% audit wall-time overhead (4.37 s versus 4.21 s in the observed pair)
while preserving identical audit values.

## Stockfish native-FST gate

The same native-kernel FST was replayed with audit on and off. C4 total cycles
were exactly `10,258,538` in both runs; retired instructions (`19,567,829`),
branch misses (`21,577`), and every per-core cycle count were also identical.
The perf-like C4 CPI therefore remains `0.5242552968`.

Observed audit summaries:

| Scope | misses | estimated UOP/miss | resolution cycles/miss | older live/miss | ROB free/miss | path event coverage | path UOP coverage | ROB-limited events |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Stockfish C4 | 21,577 | 40.78 | 12.53 | 19.12 | 171.88 | 99.85% | 65.79% | 1.63% |
| Stockfish C8 | 31,238 | 44.55 | 12.72 | 21.68 | 169.32 | 94.41% | 59.02% | 2.03% |

Most events are frontend-supply-limited rather than ROB-limited. That result
rejects a constant “flush the whole ROB” approximation for these samples.

## Twenty-case C4/C8 gate

All ten workloads at both C4 and C8 passed the native-FST validation and every
branch population ledger conserved. Because the audit is CPI-neutral, the
current accuracy distribution is unchanged: mean absolute CPI relative error
is `8.09%`, and the 20-case nearest-rank p99/max is `20.46%` at Stockfish C4
(Stockfish C8 is `17.87%`).

Population magnitude alone does not explain that tail. Across the 20 cases,
Pearson correlation between estimated UOPs/miss and absolute CPI error is
`-0.094`; resolution cycles/miss is also weakly negative at `-0.215`. Concrete
counterexamples are stronger than the aggregate coefficient:

- NAB C4 estimates `62.49` UOP/miss but has only `1.82%` CPI error;
- Zstd C4 estimates `59.13` UOP/miss and FastSim already overpredicts CPI by
  `1.38%`;
- Stockfish C4 estimates only `40.78` UOP/miss but underpredicts CPI by
  `20.46%`, equivalent to an implausible `122.29` missing cycles per branch
  miss if the whole residual were assigned to branches;
- LBM C4 would require `764.57` missing cycles/miss despite estimating just
  `27.02` wrong-path UOPs/miss.

Therefore v2 is evidence for sizing speculative work, not evidence for adding
a universal branch penalty. A direct `estimated_uops * coefficient` fit would
worsen already accurate/overpredicted cases and cannot be promoted.

## Next gate: oracle before timing

The next collection should use the existing scoped gem5 frontend ledger, or a
small timing-neutral extension of it, to obtain per-miss or bucketed ground
truth for:

1. younger UOPs resident at squash;
2. wrong-path fetch requests started, accepted, outstanding, and squashed;
3. ROB occupancy at resolution;
4. branch resolution latency and redirect-to-first-correct-fetch latency.

Fit and validate population first across all ten C4/C8 workloads. Required
checks are population MAE/bias by resolution-latency and ROB-headroom bucket,
cross-workload leave-one-out stability, and the existing hard microbenchmark
counterexamples. The history-driven Fetch-response candidate is implemented
so the oracle can be compared directly with a deterministic trace-only
prediction, but it remains disabled in the target profile until that gate
passes. Promotion still requires an observed resource effect (for example an
outstanding wrong-path fetch response or MSHR conflict) and its overlap with
the fixed redirect; a generic ROB drain term remains invalid.

## History-shadow implementation smoke (2026-08-24)

The existing Stockfish C4 native trace was replayed with the source-aligned
committed Fetch ledger held constant. Enabling only the new bounded-history
shadow produced:

| metric | ledger only | ledger + history shadow |
|---|---:|---:|
| sum core cycles | 10,259,408 | 10,259,408 |
| estimated shadow UOPs | 0 | 865,496 |
| estimated / issued shadow requests | 0 / 0 | 52,846 / 52,444 |
| hidden / recovery-exposed response cycles | 0 / 0 | 52,444 / 0 |

The complete architectural PMU object is identical between the two runs. The
result is intentionally not converted into a penalty: with the current
one-cycle committed Fetch-response prior, every anonymous response completes
inside the existing branch recovery window. This validates the overlap rule
and also proves that this first resource candidate does not yet repair the CPI
tail. A later timing promotion needs a measured recovery-surviving resource
such as an I-cache miss/MSHR tail; population alone remains insufficient.
