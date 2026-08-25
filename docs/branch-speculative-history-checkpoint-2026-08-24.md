# Branch speculative-history checkpoint experiment (2026-08-24)

> **2026-08-25 status:** the maintained native full-system default is now
> `configs/gem5-fs-native-kernel.cfg`, which selects the v28_4 modeled-I-fetch
> profile. References below to an unchanged production profile describe the
> state of this experiment on 2026-08-24; the speculative-history candidate
> itself remains an isolated, non-default ablation.

> **Oracle correction:** the absolute branch-error values below used the old
> retirement-time `DynInst::mispredicted()` label. Decode can repair the
> predicted target before retirement, so that label omitted real BPred misses.
> The table remains a same-oracle ablation record, but its MAPE/P99 values are
> superseded by `branch-miss-oracle-repair-2026-08-24.md` and must not be used
> as current accuracy claims.

## Scope

This change implements the recoverable predictor-state timing that gem5 uses
without adding wrong-path instructions to the functional FST:

- the interval scheduler invokes the predictor at the modeled Fetch cycle;
- every dynamic branch saves the pre-lookup global/local-history state;
- histories are updated speculatively with the final predicted direction;
- a misprediction restores the checkpoint and shifts in the actual outcome;
- direction/choice counter training and indirect-history retirement are
  deferred until the branch's modeled ordered-retire cycle;
- a fully retired functional-warmup boundary drains pending training before
  measurement starts.

The model is selected by `branch.speculative_history=true`. It consumes no
wrong-path PC, timing oracle, or producer identity and does not require an
`.imap`. The validated production profile remains unchanged; the candidate is
isolated in `configs/gem5-v28_3-fs-branch-speculative-history.cfg`.

## Conserved miss attribution

The output now exposes three mutually exclusive committed-miss populations:

1. `branch_direction_only_misses`: the raw conditional prediction is wrong;
2. `branch_target_unavailable_misses`: direction selected taken, but no target
   provider was available and Fetch therefore fell through;
3. `branch_wrong_target_misses`: taken direction was correct, but the supplied
   target was wrong.

Their sum is exactly `branch_misses`. A separate
`branch_masked_direction_misses` counter records raw taken/not-taken errors
that a missing target masks in the final Fetch prediction and is not part of
the committed-miss sum.

Across the disabled 40-case control, all cases conserve and the populations
are:

| population | count | share of FastSim misses |
|---|---:|---:|
| direction only | 9,952,382 | 79.96% |
| target unavailable | 2,157,843 | 17.34% |
| wrong target | 337,221 | 2.71% |
| total | 12,447,446 | 100.00% |

There are additionally 350,017 masked raw direction disagreements. They
explain why the legacy raw direction counter plus explicit wrong-target counter
was not a disjoint decomposition of the PMU population.

## Historical same-binary 40-case result

Both variants passed all 40 C4/C8/C16/C32 native-kernel replay, oracle-source,
and conservation gates. The disabled control exactly reproduces the previous
RAS-repaired baseline.

| branch metric | disabled control | checkpoint candidate | delta |
|---|---:|---:|---:|
| MAPE | 13.651786% | 13.784581% | +0.132795 pp |
| P50 APE | 10.855170% | 11.071856% | +0.216686 pp |
| P90 APE | 32.837045% | 32.982955% | +0.145910 pp |
| P99 APE | 48.856311% | 49.020663% | +0.164351 pp |
| WAPE / signed bias | 7.020185% | 7.105208% | +0.085023 pp |
| predicted misses | 12,447,446 | 12,457,335 | +9,889 |

The candidate raised branch misses in 34 cases and lowered them in 6. It is
therefore a semantic-fidelity implementation and a useful ablation, but it is
not accepted as the default accuracy repair.

Under the legacy oracle, LBM C4 appeared especially diagnostic. The control
predicts 9,422 misses versus 6,189 legacy labels. Its conserved FastSim
populations are 4,699 direction-only,
4,576 target-unavailable, and 147 wrong-target misses. Enabling delayed
training changes the total only to 9,432. The subsequent oracle audit showed
that the apparent 52% relative-error tail was dominated by missing reference
events, so it cannot establish target-provider/BTB availability as the next
modeling priority.

## Evidence

- disabled control: `tmp/branch-spec-history-control-full40/summary.md`
- checkpoint candidate: `tmp/branch-spec-history-full40/summary.md`
- four-case pilot and mutually exclusive counters:
  `tmp/branch-spec-history-pilot/`

These are experiment artifacts and are intentionally not part of the curated
repository commit scope.
